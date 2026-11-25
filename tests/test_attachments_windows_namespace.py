#!/usr/bin/env python3
"""The attachment namespace fence and owner runtime on Windows.

The fence (attachments_native_namespace) keeps the owner's attachment records and managed Claude's registry apart.
On Windows it opens its records through private_storage's no-link walk and takes its host lock with try_lock. On
macOS and Linux these tests run it, with agent messaging and the owner runtime around it, under
tests/windows_simulation.py (every private_storage call answered by the POSIX walk, os.getuid and fcntl failing
loudly). On real Windows they check what only Windows can: a second process holding the lock, junctions, an access
list inherited from the folder, and a database path in another case. Synthetic files in temporary folders only.

Run: python tests/test_attachments_windows_namespace.py
"""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import shutil
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
import attachments_native_namespace as namespace
from attachments_native_namespace import NamespaceError, NativeNamespaceFence
import officer_messaging as messaging
import server
import test_attachments_owner_guards as guard_tests
import test_attachments_owner_startup as startup_tests

SIMULATED_WHY = 'the simulation answers Windows calls with the POSIX walk'
REAL_WHY = 'requires real Windows handles, access lists, byte locks and junctions'

# A second process that holds the host lock until it reads a line: flock on POSIX, a byte lock on Windows.
HOLDER = r'''
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | getattr(os, 'O_BINARY', 0))
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('held', flush=True)
sys.stdin.readline()
'''


def inbox_as_on_windows():
    """peer_inbox reads its optional configuration only where os.name is 'posix' and returns None elsewhere; the
    simulation cannot change os.name, so it is given Windows' answer."""
    import peer_inbox
    return mock.patch.object(peer_inbox, 'load_config', return_value=None)


class FenceChecks:
    """The same checks with Windows simulated (``windows`` is the simulation) or for real (``windows`` does nothing)."""

    def windows(self):
        raise NotImplementedError

    def private_folder(self, path):
        raise NotImplementedError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.state = self.base / 'state'
        self.private_folder(self.state)
        self.sessions = self.base / 'sessions'
        self.sessions.mkdir()
        self.store = None
        self.addCleanup(self.close_store)

    def open_store(self, state=None):
        self.close_store()
        with self.windows():
            self.store = messaging.MessagingStore(str(state or self.state), str(self.sessions))
        return self.store

    def close_store(self):
        if self.store is not None:
            with self.windows():
                self.store.close()
            self.store = None

    def fence(self, state=None):
        with self.windows():
            return NativeNamespaceFence(str(state or self.state))

    def witnessed(self):
        """An owner database with its namespace witness: the host lock file, authority and anchor records."""
        store = self.open_store()
        fence = self.fence()
        with self.windows():
            store.db.execute('PRAGMA synchronous=FULL')
            self.assertFalse(fence.detected(owner_db=store.db))
            guard = fence.initialize_owner(store.db, store.lock)
            self.assertIsNotNone(guard)
            self.assertTrue(fence.detected(owner_db=store.db))
        self.close_store()
        return fence

    def anchor(self):
        return json.loads((self.state / namespace.ANCHOR).read_bytes())

    def held_elsewhere(self, path):
        """Start a second process holding ``path``'s lock; returns it once it holds the lock."""
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(path)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is not None or holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def released(self, holder):
        holder.communicate('release\n', timeout=60)

    def opens_within(self, seconds=10):
        """Messaging starts again, retrying while Windows may still keep a closed process's byte lock a moment."""
        end = time.monotonic() + seconds
        while True:
            try:
                return self.open_store()
            except NamespaceError:
                if time.monotonic() > end:
                    raise

    def assert_refused(self, fence):
        with self.windows():
            with self.assertRaises(NamespaceError):
                with fence.owner_open():
                    pass
            with self.assertRaises((NamespaceError, OSError, ValueError)):
                messaging.MessagingStore(str(self.state), str(self.sessions)).close()

    def test_witness_is_made_and_messaging_starts_with_it_after_each_restart(self):
        fence = self.witnessed()
        with self.windows():
            database = ps.open_no_links(str(self.state / namespace.OWNER_DB), single_link=True, private=True)
            try:
                expected = namespace.descriptor_identity(database)
            finally:
                os.close(database)
        self.assertEqual(self.anchor()['db'], expected)            # the handle's volume serial and file index
        for _ in range(3):
            with self.windows():
                self.assertIsNotNone(messaging.MessagingStore._retained_namespace_before_open(str(self.state)))
            store = self.open_store()
            self.assertIsNotNone(store._attachment_guard)
            with self.windows(), store.lock:
                fence.validate_mount(store.db)
            self.close_store()
            with self.windows(), fence.host_fence():             # released when messaging closed
                pass

    def test_a_second_process_holding_the_host_lock_holds_the_fence(self):
        fence = self.witnessed()
        holder = self.held_elsewhere(self.state / namespace.HOST)
        with self.windows():
            started = time.monotonic()
            with self.assertRaises(NamespaceError):
                with fence.host_fence():
                    pass
            self.assertLess(time.monotonic() - started, 5)     # it gives up after a short retry, not a long wait
        self.assert_refused(fence)
        self.released(holder)
        self.assertIsNotNone(self.opens_within()._attachment_guard)

    def test_a_record_that_is_not_private_is_refused(self):
        fence = self.witnessed()
        raw = (self.state / namespace.ANCHOR).read_bytes()
        self.not_private(self.state / namespace.ANCHOR, raw)
        self.assert_refused(fence)
        self.make_private(self.state / namespace.ANCHOR, raw)
        self.assertIsNotNone(self.open_store()._attachment_guard)

    def test_a_link_in_place_of_a_record_or_the_state_folder_is_refused(self):
        fence = self.witnessed()
        anchor = self.state / namespace.ANCHOR
        raw = anchor.read_bytes()
        os.remove(anchor)
        elsewhere = self.base / 'elsewhere'
        self.link_record(anchor, elsewhere, raw)
        self.assert_refused(fence)
        self.unlink_record(anchor, elsewhere)
        self.make_private(anchor, raw)
        self.assertIsNotNone(self.open_store()._attachment_guard)
        self.close_store()
        moved = self.base / 'state-moved'
        os.rename(self.state, moved)
        self.link_folder(self.state, moved)
        self.assert_refused(self.fence())
        self.unlink_folder(self.state)
        os.rename(moved, self.state)
        self.assertIsNotNone(self.open_store()._attachment_guard)

    def test_a_database_path_that_differs_only_in_case_is_the_same_database(self):
        self.witnessed()
        database = str(self.state / namespace.OWNER_DB)
        other = str(self.base / 'other.sqlite3')
        with self.windows():
            self.assertTrue(namespace.same_database_path(database.upper(), database))
            self.assertTrue(namespace.same_database_path(database.replace(os.sep, '/'), database))
            self.assertFalse(namespace.same_database_path(other, database))
            self.assertFalse(namespace.same_database_path('', database))
        swapped = Path(str(self.state)[:-len('state')] + 'STATE')
        if not (swapped.exists() and os.path.samefile(swapped, self.state)):
            self.skipTest('this file system tells names apart by case')
        store = self.open_store(swapped)                        # SQLite names it STATE; the fence still accepts it
        self.assertIsNotNone(store._attachment_guard)
        name = store.db.execute('PRAGMA database_list').fetchone()[2].replace('/', os.sep)
        self.assertIn(os.sep + 'STATE' + os.sep, name)


@unittest.skipIf(os.name == 'nt', SIMULATED_WHY)
class SimulatedWindowsFenceTests(FenceChecks, unittest.TestCase):
    """The checks with Windows simulated. A POSIX symbolic link stands in for a junction and mode 0644 for an access
    list that names others; real Windows checks those for real (WindowsFenceTests)."""

    @contextmanager
    def windows(self):
        from windows_simulation import simulated_windows
        with simulated_windows(), inbox_as_on_windows():
            yield

    def private_folder(self, path):
        os.mkdir(path, 0o700)

    def not_private(self, path, raw):
        os.chmod(path, 0o644)

    def make_private(self, path, raw):
        if path.exists():
            os.chmod(path, 0o600)
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)

    def link_record(self, path, elsewhere, raw):
        elsewhere.write_bytes(raw)
        os.chmod(elsewhere, 0o600)
        os.symlink(elsewhere, path)

    def unlink_record(self, path, elsewhere):
        os.remove(path)
        os.remove(elsewhere)

    def link_folder(self, path, target):
        os.symlink(target, path, target_is_directory=True)

    def unlink_folder(self, path):
        os.remove(path)

    def test_windows_calls_are_the_ones_made(self):
        """The Windows branch reads identities from handles and never reaches for os.getuid or fcntl (the
        simulation would fail if it did); the anchor records the simulated volume serial, not fstat's st_dev."""
        from windows_simulation import SERIAL
        self.witnessed()
        self.assertEqual(self.anchor()['db'][0], SERIAL)
        self.assertEqual(self.anchor()['host'][0], SERIAL)


def junction(link, target):
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True, timeout=30)


@unittest.skipUnless(os.name == 'nt' and ps.folder_walk_available(), REAL_WHY)
class WindowsFenceTests(FenceChecks, unittest.TestCase):
    """The checks on real NTFS, in state folders made private and owned by this user (the runner is elevated, so a
    plain new folder would belong to Administrators)."""

    def windows(self):
        return nullcontext()

    def private_folder(self, path):
        ps.make_private_folder(str(path))

    def not_private(self, path, raw):
        """Write the record again as a plain new file: it follows the folder's access list (inherited, not
        protected). The owner is set to this user, so only the access list is what is refused."""
        os.remove(path)
        path.write_bytes(raw)
        subprocess.run('icacls "%s" /setowner "%%USERDOMAIN%%\\%%USERNAME%%"' % path, shell=True, check=True,
                       capture_output=True, timeout=30)
        with self.assertRaises(ps.PrivateStorageError) as raised:
            ps.windows_security(str(path))
        self.assertEqual(raised.exception.reason, 'access')

    def make_private(self, path, raw):
        if path.exists():
            os.remove(path)
        fd = ps.create_private_file(str(path))
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)

    def link_record(self, path, elsewhere, raw):
        elsewhere.mkdir()
        junction(path, elsewhere)

    def unlink_record(self, path, elsewhere):
        os.rmdir(path)
        os.rmdir(elsewhere)

    def link_folder(self, path, target):
        junction(path, target)

    def unlink_folder(self, path):
        os.rmdir(path)


def _simulated(base, skips):
    """``base`` run again with private_storage simulating Windows, delivery to agent chats off as on Windows
    (NATIVE_ATTACHMENT_DELIVERY_HOST) and sys.platform reporting Windows."""
    class Simulated(base):
        def setUp(self):
            why = skips.get(self._testMethodName)
            if why:
                self.skipTest('not simulated: ' + why)
            from windows_simulation import simulated_windows
            for patcher in (mock.patch.object(sys, 'platform', 'win32'), inbox_as_on_windows(),
                            mock.patch.object(server, 'NATIVE_ATTACHMENT_DELIVERY_HOST', False)):
                patcher.start()
                self.addCleanup(patcher.stop)
            simulation = simulated_windows()
            simulation.__enter__()
            self.addCleanup(simulation.__exit__, None, None, None)
            super().setUp()
    Simulated.__name__ = Simulated.__qualname__ = 'SimulatedWindows' + base.__name__
    return unittest.skipIf(os.name == 'nt', SIMULATED_WHY)(Simulated)


# The owner guard and runtime suites again on the Windows branch. Their tests that need the tick to send to a Codex
# chat, or a second thread, skip themselves there (codex_tick, one_thread); on real Windows they run as they are.
SimulatedWindowsMessagingGuardTests = _simulated(guard_tests.MessagingGuardTests, {})
SimulatedWindowsStartupTests = _simulated(startup_tests.StartupTests, {})


class ImportBoundaryTests(unittest.TestCase):
    def test_the_fence_owner_runtime_and_messaging_do_not_import_managed_claude(self):
        """Managed Claude is Mac-only by design; nothing on the fence and owner path may import it."""
        code = ('import sys; sys.path.insert(0, %r); import attachments_native_namespace, attachments_owner_runtime, '
                'attachments_owner_guards, attachments_owner_directory, attachments_owner_marker, '
                'attachments_native_composition, officer_messaging; '
                "print('claude_managed' in sys.modules)" % str(HERE.parent / 'src'))
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), 'False')


if __name__ == '__main__':
    unittest.main()
