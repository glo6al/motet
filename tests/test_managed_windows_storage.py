#!/usr/bin/env python3
"""Managed Claude and New chat private storage on Windows (step 8b.1). The feature itself stays refused on Windows
until 8b.5; these check the storage it will use.

On macOS and Linux the existing runner, manager, New chat service and inventory suites run again under
tests/windows_simulation.py: private_storage answers with the POSIX walk, fcntl and os.getuid fail if a Windows
branch reaches for them, and a held folder or file reports the Windows identity (a 32-bit volume serial that is
not os.stat's st_dev), so a folder identity still taken from os.stat would not match. On real Windows the private
state checks run against real access lists. Synthetic files in temporary folders; no Claude is started.

Run: python tests/test_managed_windows_storage.py
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import private_storage as ps
import claude_managed
import claude_managed_runner
import new_chat_inventory
import test_claude_managed as manager_tests
import test_claude_managed_runner as runner_tests
import test_new_chat_inventory as inventory_tests
import test_new_chat_service as service_tests

SIMULATED = os.name != 'nt'
REAL_WINDOWS = os.name == 'nt' and ps.folder_walk_available()


class Simulated:
    """Enter the Windows simulation before the suite's own setUp, for the whole test."""

    def setUp(self):
        from windows_simulation import simulated_windows
        stack = ExitStack()
        self.calls = stack.enter_context(simulated_windows())
        self.addCleanup(stack.close)
        super().setUp()


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedRunnerTests(Simulated, runner_tests.RunnerTests):
    """The supervisor's private state: the run folder held through the walk, its files opened or created through
    it, the ledger's companions by private_storage's rule, run.lock as private_storage's lock, the folder identity
    from a handle."""

    def setUp(self):
        super().setUp()
        with ps.open_folder(str(self.project)) as held:
            device, inode = held.identity()[:2]
        self.manifest['folder'].update(device=device, inode=inode)
        self.write('manifest.json', json.dumps(self.manifest))

    def test_the_windows_branches_are_the_ones_used(self):
        self.runner()
        for name in ('open_folder', 'try_lock', 'sqlite_companions_private', 'path_identity'):
            self.assertIn(name, self.calls)

    def test_a_folder_identity_from_os_stat_is_refused(self):
        info = self.project.stat()
        self.manifest['folder'].update(device=info.st_dev, inode=info.st_ino)
        self.write('manifest.json', json.dumps(self.manifest))
        self.assert_error('folder_changed', self.runner)


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedManagerTests(Simulated, manager_tests.Fixture):
    """The manager's registry, host and registry locks, run folder files and folder flushes."""

    def setUp(self):
        super().setUp()
        self.folder = self.folder.__class__(**dict(self.folder.__dict__, **dict(zip(
            ('device', 'inode'), new_chat_inventory.folder_identity(str(self.project))))))

    def test_refusals_before_reservation_never_hold_a_slot(self):
        refused = mock.patch.object(claude_managed.ManagedClaude, '_breakaway_refusal',
                                    return_value='fictional refusal')
        with refused:
            for _ in range(5):
                with self.assertRaises(claude_managed.ManagedError) as raised:
                    self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
                self.assertEqual(raised.exception.code, 'supervisor_detach_refused')
        self.assertTrue(self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4())))
        with self.manager._registry() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)

    def test_a_supervisor_that_could_not_start_is_not_started_not_uncertain(self):
        import dataclasses
        import claude_managed_windows

        def failed(path):
            raise claude_managed_windows.NotStarted(claude_managed_windows.NOT_STARTED)
        self.manager._test = dataclasses.replace(self.dependencies, spawn=failed)
        run_id = self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        for _ in range(2):
            with self.assertRaises(claude_managed.ManagedError) as raised:
                self.manager.launch_once(run_id, self.folder, lambda *_: None)
            self.assertEqual(raised.exception.code, 'supervisor_not_started')

    def test_an_early_exit_without_a_record_is_not_called_a_refused_detach(self):
        """Claude started and exited before its identity was proved: the run ends without a native receipt, but
        nothing recorded it as not started, so a replay is not told the detach was refused."""
        run_id = self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        self.manager.launch_once(run_id, self.folder, lambda *_: None)
        with self.manager._registry() as db:
            row = self.manager._row(db, run_id)
            row.update(state='exited', occupied=False, nativeReceipt=False)
            self.manager._save(db, row)
        self.assertIsNone(self.manager._recorded_not_started(run_id, row))

    def test_an_unrecorded_reason_still_ends_the_run_with_plain_words(self):
        import dataclasses
        import claude_managed_windows

        def refused(path):
            raise claude_managed_windows.DetachRefused(claude_managed_windows.DETACH_REFUSED)
        self.manager._test = dataclasses.replace(self.dependencies, spawn=refused)
        run_id = self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        write = claude_managed._write_private

        def full_for_the_reason(path, data):
            if path.name == claude_managed.ManagedClaude.NOT_STARTED_FILE:
                raise OSError('fictional full disk')
            return write(path, data)
        with mock.patch.object(claude_managed, '_write_private', full_for_the_reason):
            with self.assertRaises(claude_managed.ManagedError) as raised:
                self.manager.launch_once(run_id, self.folder, lambda *_: None)
        self.assertEqual(raised.exception.code, 'supervisor_not_started')
        with self.manager._registry() as db:
            row = self.manager._row(db, run_id)
        self.assertEqual((row['state'], row['occupied']), ('exited', False))

    def test_a_waiter_sees_another_callers_not_started_record_at_once(self):
        import dataclasses
        run_id = self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        request, first = self.request, []

        def recorded_first(path, command, params):
            if command == 'status' and not first:
                first.append(path.name)
                self.manager._not_started(path.name, 'supervisor_detach_refused')   # another caller, first
                raise OSError('no supervisor')
            return request(path, command, params)
        self.manager._test = dataclasses.replace(self.dependencies, spawn=lambda path: None,
                                                 request=recorded_first, launch_timeout=15)
        started = time.monotonic()
        with self.assertRaises(claude_managed.ManagedError) as raised:
            self.manager.launch_once(run_id, self.folder, lambda *_: None)
        self.assertEqual(raised.exception.code, 'supervisor_detach_refused')
        self.assertLess(time.monotonic() - started, 5)
        with self.manager._registry() as db:              # still valid, still not started
            row = self.manager._row(db, run_id)
        self.assertEqual((row['state'], row['occupied']), ('exited', False))

    def test_a_refused_detach_records_that_nothing_started(self):
        """A supervisor Windows would not let leave the server's job was ended before it ran: the run is recorded
        as ended without a native receipt and refused in plain words, also on replay, never as uncertain."""
        import dataclasses
        import claude_managed_windows

        def refused(path):
            raise claude_managed_windows.DetachRefused(claude_managed_windows.DETACH_REFUSED)
        self.manager._test = dataclasses.replace(self.dependencies, spawn=refused)
        run_id = self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        for _ in range(2):
            with self.assertRaises(claude_managed.ManagedError) as raised:
                self.manager.launch_once(run_id, self.folder, lambda *_: None)
            self.assertEqual(raised.exception.code, 'supervisor_detach_refused')
        with self.manager._registry() as db:
            row = self.manager._row(db, run_id)
        self.assertEqual((row['state'], row['occupied'], row['nativeReceipt']), ('exited', False, False))

    @unittest.skip('Threads race the registry; the simulation lets only one thread use private_storage. Lock '
                   'contention on Windows is checked by the real Windows tests below.')
    def test_atomic_capacity_across_manager_instances(self):
        pass

    @unittest.skip('Threads race the registry; the simulation lets only one thread use private_storage.')
    def test_concurrent_status_before_endpoint_preserves_persisted_startup_grace(self):
        pass

    @unittest.skip('The control socket is POSIX; Windows gets its own channel in step 8b.3.')
    def test_real_ipc_client_accepts_bounded_large_response_and_hides_credentials(self):
        pass

    def test_the_windows_branches_are_the_ones_used(self):
        self.manager.reserve(self.folder, str(manager_tests.uuid.uuid4()))
        for name in ('check_private', 'create_private_file', 'make_private_folder', 'open_folder', 'try_lock',
                     'close_lock', 'sqlite_companions_private'):
            self.assertIn(name, self.calls)


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedReadinessTests(Simulated, manager_tests.ConfigurationReadinessTests):
    """The readiness record read through the walk and checked on its handle. The machine and user identity in the
    record is still the Mac's (ioreg and the uid; Windows' is step 8b.4), so it is computed with the real uid."""

    def setUp(self):
        real, getuid = claude_managed.local_configuration_identity, os.getuid

        def mac_identity():
            with mock.patch.object(os, 'getuid', getuid):
                return real()
        patcher = mock.patch.object(claude_managed, 'local_configuration_identity', mac_identity)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Win32 job queries have no simulated answer: the server is taken as free to start a supervisor.
        breakaway = mock.patch.object(claude_managed.ManagedClaude, '_breakaway_refusal', return_value=None)
        breakaway.start()
        self.addCleanup(breakaway.stop)
        super().setUp()

    @unittest.skip('POSIX ownership by uid; on Windows an untrusted record is one whose owner or access list is '
                   'not private, as the real Windows tests check.')
    def test_untrusted_file_cannot_enable_readiness(self):
        pass


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedInventoryTests(Simulated, inventory_tests.InventoryTests):
    """Folder identities from a handle through the walk. The platform reads as Windows, so the macOS spelling step
    (F_GETPATH) is not taken."""

    def setUp(self):
        patcher = mock.patch.object(new_chat_inventory.sys, 'platform', 'win32')
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    @unittest.skip('Windows reads a folder identity from a handle opened for attributes only (by NtCreateFile, not '
                   'os.open, so the real Windows run keeps this check); macOS avoids opening a folder because that '
                   'can prompt for access, which Windows does not. The simulation opens it with os.open.')
    def test_refresh_and_get_never_open_project_or_private_directories(self):
        pass

    @unittest.skip('The simulation opens folders with POSIX read access, which a mode-000 folder refuses; the '
                   'Windows walk asks only for traversal and attributes.')
    def test_unreadable_private_anchor_does_not_refuse_an_unrelated_project(self):
        pass


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedServiceStorageTests(unittest.TestCase):
    """New chat's private folder, writer lock and journal."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='new-chat-windows-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'project').mkdir()

    def service(self):
        return service_tests.NewChatService(self.root / '.live-room', {}, lambda: [], home=self.root)

    def test_storage_is_private_locked_and_released(self):
        from windows_simulation import simulated_windows
        with simulated_windows() as calls:
            first = self.service()
            try:
                with self.assertRaises(OSError):
                    self.service()            # the writer lock is held
            finally:
                first.close()
            self.service().close()            # released on close
        for name in ('open_folder', 'try_lock', 'close_lock'):
            self.assertIn(name, calls)
        state = self.root / '.live-room' / 'new-chat'
        self.assertEqual(sorted(p.name for p in state.iterdir()), ['journal.sqlite', 'writer.lock'])
        for path in (state.parent, state):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / 'writer.lock').stat().st_mode & 0o777, 0o600)

    def test_a_state_folder_others_can_open_is_refused(self):
        from windows_simulation import simulated_windows
        (self.root / '.live-room').mkdir(mode=0o755)
        with simulated_windows():
            with self.assertRaises(OSError):
                self.service()


class CreateErrorTests(unittest.TestCase):
    def test_an_existing_name_is_file_exists_with_its_windows_code(self):
        for code in (80, 183):                # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            error = ps._create_failed(code)
            self.assertIsInstance(error, FileExistsError)
            self.assertEqual(error.winerror, code)
        error = ps._create_failed(5)          # access denied stays the plain refusal it was
        self.assertIs(type(error), OSError)
        self.assertEqual(error.winerror, 5)


@unittest.skipUnless(REAL_WINDOWS, 'requires real Windows and its folder walk')
class WindowsManagedStorageTests(unittest.TestCase):
    """On real Windows: the storage the feature will use, against real access lists. The runner is elevated, so the
    home folder here is made private for this user first."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='managed-windows-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / 'home'
        ps.make_private_folder(str(self.home))
        self.state = self.home / '.live-room'
        ps.make_private_folder(str(self.state))
        self.project = self.home / 'orchard'
        ps.make_private_folder(str(self.project))

    def folder(self):
        device, inode = new_chat_inventory.folder_identity(str(self.project))
        return claude_managed.FrozenFolder('orchard', 'revision', 'Orchard', str(self.project), device, inode)

    def manager(self):
        dependencies = claude_managed.TestDependencies(
            dict(path='C:\\fictional\\claude.exe', version=claude_managed.VERSION, sha256='0' * 64),
            lambda path: None, lambda path, command, params: {}, lambda path: {}, None)
        return claude_managed.ManagedClaude(self.state, enabled=True, configuration_ready=True,
                                            _test_dependencies=dependencies)

    def test_the_registry_and_its_locks_are_private_and_shared_with_the_fence(self):
        # The registry as reserve() makes it (the executable check that reserve() also makes is step 8b.4's).
        with self.manager()._registry(create=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        root = self.state / 'managed-claude'
        ps.check_private(str(root), directory=True)
        for name in ('registry.sqlite3', 'registry.expected', 'registry.lock'):
            ps.check_private(str(root / name), single_link=True)
        ps.check_private(str(self.state / 'managed-claude.lock'), single_link=True)
        # The host lock is the byte lock the namespace fence takes: held by the manager, the fence cannot take it.
        held = claude_managed._lock(self.state / 'managed-claude.lock')
        try:
            from attachments_native_namespace import NativeNamespaceFence, NamespaceError
            with self.assertRaises(NamespaceError):
                with NativeNamespaceFence(self.state).host_fence():
                    pass
        finally:
            claude_managed._unlock(held)
        with NativeNamespaceFence(self.state).host_fence():
            pass

    def test_the_registry_opens_again_and_existing_names_are_file_exists(self):
        for _ in range(2):        # the second create finds the host lock, root, registry.lock and expected marker
            with self.manager()._registry(create=True) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        with self.assertRaises(FileExistsError):
            ps.create_private_file(str(self.state / 'managed-claude.lock'))
        with self.assertRaises(FileExistsError):
            ps.make_private_folder(str(self.state / 'managed-claude'))

    def test_a_registry_lock_held_elsewhere_is_waited_for_then_refused(self):
        root = self.state / 'managed-claude'
        ps.make_private_folder(str(root))
        os.close(ps.create_private_file(str(root / 'registry.lock')))
        held = claude_managed._lock(root / 'registry.lock')
        try:
            with mock.patch.object(claude_managed, 'LOCK_SECONDS', .2):
                started = time.monotonic()
                with self.assertRaises(claude_managed.ManagedError):
                    claude_managed._lock(root / 'registry.lock')
                self.assertGreaterEqual(time.monotonic() - started, .2)
        finally:
            claude_managed._unlock(held)
        claude_managed._unlock(claude_managed._lock(root / 'registry.lock'))

    def test_a_readable_run_file_is_refused_by_the_runner(self):
        run = self.state / 'run'
        ps.make_private_folder(str(run))
        device, inode = new_chat_inventory.folder_identity(str(self.project))
        manifest = {'schemaVersion': 1, 'runId': '00000000-0000-4000-8000-000000000002',
                    'nativeId': '00000000-0000-4000-8000-000000000003',
                    'incarnation': '00000000-0000-4000-8000-000000000004',
                    'folder': {'id': 'fictional', 'revision': 'one', 'label': 'Fictional orchard',
                               'path': str(self.project), 'device': device, 'inode': inode},
                    'executable': {'path': 'C:\\fictional\\claude.exe', 'version': claude_managed_runner.PINNED_VERSION,
                                   'sha256': claude_managed_runner.PINNED_SHA256},
                    'profile': 'restricted_test', 'createdAt': 1000}
        for name, data in (('manifest.json', json.dumps(manifest).encode()), ('token', b'a' * 64), ('run.lock', b'')):
            fd = ps.create_private_file(str(run / name))
            os.write(fd, data)
            os.close(fd)
        root, read, token = claude_managed_runner._open_state(run)
        claude_managed_runner._close(root)
        self.assertEqual((read['runId'], token), (manifest['runId'], 'a' * 64))
        # Authenticated Users may read the token: no longer private.
        subprocess.run(['icacls', str(run / 'token'), '/grant', '*S-1-5-11:R'], check=True, capture_output=True,
                       timeout=30)
        with self.assertRaises(claude_managed_runner.RunnerError) as raised:
            claude_managed_runner._open_state(run)
        self.assertEqual(raised.exception.code, 'unsafe_state')

    def test_new_chat_storage_is_private_and_locked(self):
        first = service_tests.NewChatService(self.state, {}, lambda: [], home=self.home)
        try:
            with self.assertRaises(OSError):
                service_tests.NewChatService(self.state, {}, lambda: [], home=self.home)
        finally:
            first.close()
        service_tests.NewChatService(self.state, {}, lambda: [], home=self.home).close()
        for name in ('writer.lock', 'journal.sqlite'):
            ps.check_private(str(self.state / 'new-chat' / name), single_link=True)

    def test_folder_identity_comes_from_the_handle(self):
        """A project folder's identity is read from a handle opened by path (a project may be on a network share or
        in OneDrive): the same volume serial and file index the walk reads for a private folder."""
        fd = ps.open_no_links(str(self.project), directory=True)
        try:
            expected = tuple(ps.file_identity(fd)[:2])
        finally:
            os.close(fd)
        self.assertEqual(new_chat_inventory.folder_identity(str(self.project)), expected)
        new_chat_inventory.validate_folder(self.folder())


if __name__ == '__main__':
    unittest.main()
