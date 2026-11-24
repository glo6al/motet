#!/usr/bin/env python3
"""The private attachment store's Windows branch.

On macOS and Linux it runs under tests/windows_simulation.py: the store takes its Windows branch, every
private_storage call is answered by the POSIX walk, and os.getuid and fcntl fail loudly, so a POSIX-only call left in
the Windows branch shows up. On real Windows the WindowsStore tests check what only Windows can: junctions, access
lists inherited from a folder, a removal while another program has the file open, and a held folder keeping the
folders above it in place. Synthetic files in temporary folders only.

Run: python tests/test_attachments_windows_store.py
"""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import private_storage as ps
from attachment_host import state_root
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
import test_attachments_foundation as foundation
from test_attachments_foundation import BINDING, EPOCH, fence, metadata

DATA = b'Fictional orchard\nLine two\n'


import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import test_attachments_catalogue
import test_attachments_conversations
import test_attachments_deletion_slots
import test_attachments_http
import test_attachments_integration
import test_attachments_officer_storage
import test_attachments_preview_lifetime
import test_attachments_requests
import test_attachments_store_boundary

# Store suites whose every test runs again with the store on its Windows branch, except these. The simulation's
# switches between Windows and POSIX are process-wide, so a test with two threads would see the other's switch; those
# run for real on Windows CI instead. The rest read POSIX modes or links, or belong to delivery to agent chats, which
# stays off on Windows (NATIVE_ATTACHMENT_DELIVERY_HOST); one counts on POSIX's removal owning its descriptor.
NOT_SIMULATED = {
    'test_permissions_lock_and_restart': 'POSIX modes; real Windows checks the access list (WindowsStore)',
    'test_private_root_symlink_hardlink_permissions_refused': 'POSIX links and modes; WindowsStore covers junctions',
    'test_concurrent_reservation_exact_limit': 'two threads',
    'test_discard_during_stream_keeps_debt_until_fd_closes': 'two threads',
    'test_two_receivers_cannot_write_same_upload': 'two threads',
    'test_takeover_fences_late_stream_without_removing_retained_draft': 'two threads',
    'test_same_key_concurrency_is_one_local_message': 'two threads',
    'test_officer_excluded_nonmember_cleanup_after_actual_descriptor_closes': 'two threads',
    'test_native_excluded_nonmember_cleanup_after_actual_descriptor_closes': 'two threads',
    'test_concurrent_exact_retry_is_at_most_one_retained_message': 'two threads',
    'test_source_handles_prevent_store_close': 'a source handle for delivery to agent chats, off on Windows',
    'test_real_http_binary_end_and_completed_retry_no_body_wait': 'the HTTP server answers on its own thread',
    'test_close_fences_new_reads_and_removals_before_descriptor_teardown': 'watches POSIX descriptor teardown',
    'test_unlinked_remove_descriptor_close_failure_stays_charged': (
        "POSIX's removal owns its descriptor; on Windows the walk deletes through its own handle"),
}

SUITES = (foundation.StoreTests, test_attachments_store_boundary, test_attachments_catalogue,
          test_attachments_conversations, test_attachments_deletion_slots, test_attachments_officer_storage,
          test_attachments_preview_lifetime, test_attachments_http, test_attachments_integration,
          test_attachments_requests)


def _simulated(base):
    class Simulated(base):
        def setUp(self):
            why = NOT_SIMULATED.get(self._testMethodName)
            if why:
                self.skipTest('not simulated: ' + why)
            from windows_simulation import simulated_windows
            import attachments_service
            # No background thread under the simulation: its switches are process-wide (see windows_simulation).
            worker = mock.patch.object(attachments_service, 'CLEANUP_WORKER', False)
            worker.start()
            self.addCleanup(worker.stop)
            simulation = simulated_windows()
            self.windows_calls = simulation.__enter__()
            self.addCleanup(simulation.__exit__, None, None, None)
            super().setUp()
    Simulated.__name__ = Simulated.__qualname__ = 'SimulatedWindows' + base.__name__
    return unittest.skipIf(os.name == 'nt', 'the simulation answers Windows calls with the POSIX walk')(Simulated)


def _cases(suite):
    if isinstance(suite, type):
        return [suite]
    return [value for value in vars(suite).values() if isinstance(value, type) and issubclass(value, unittest.TestCase)
            and value.__module__ == suite.__name__ and any(n.startswith('test') for n in dir(value))]


for _suite in SUITES:
    for _case in _cases(_suite):
        _class = _simulated(_case)
        globals()[_class.__name__] = _class
del _suite, _case, _class


@unittest.skipIf(os.name == 'nt', 'the simulation answers Windows calls with the POSIX walk')
class SimulatedWindowsStoreRefusals(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HERE))
        from windows_simulation import simulated_windows
        self.simulated_windows = simulated_windows
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = state_root(self.temp.name)

    def open(self):
        return AttachmentStore(self.root, enabled=True, owner_epoch=EPOCH, clock=lambda: 1_800_000_000.0)

    def error(self, code, fn):
        with self.assertRaises(AttachmentError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_a_volume_that_is_not_ntfs_is_refused(self):
        for file_system in ('ReFS', 'FAT32', 'exFAT', None):
            with self.subTest(file_system=file_system), self.simulated_windows(file_system=file_system):
                self.error('storage_unavailable', self.open)
        self.assertEqual(os.listdir(self.root), [], 'nothing was made on a refused volume')

    def test_without_the_walk_the_host_is_refused(self):
        with self.simulated_windows(folder_walk=False):
            self.error('host_unavailable', self.open)

    def test_identities_outside_the_stored_range_are_refused(self):
        import attachments_files
        # (volume serial, file index, size, write time, creation time), as private_storage.file_identity gives them
        for refused in ((1, 2 ** 63, 0, 0, 0), (1, 0, 0, 0, 0), (2 ** 63, 1, 0, 0, 0)):
            with self.subTest(identity=refused), self.assertRaises(AttachmentError):
                attachments_files._id_ok(refused)
        self.assertEqual(attachments_files._id_ok((2 ** 32 - 1, 2 ** 63 - 1, 5, 6, 7)), [2 ** 32 - 1, 2 ** 63 - 1, 5, 6, 7])

    def test_identities_come_from_the_handle_not_from_pythons_fstat(self):
        """Since Python 3.12 Windows' fstat gives st_dev as a 64-bit serial, often above 2**63, and a value that
        differs from earlier Pythons. The store must start anyway and store the handle's 32-bit serial."""
        import json
        from windows_simulation import SERIAL
        real_fstat = os.fstat

        def fstat_312(fd):
            info = real_fstat(fd)
            fields = list(info)
            fields[2] = 0xF1D2C3B4A5968778                 # st_dev: 64 bits, top bit set, unlike the handle serial
            shown = ('st_mode', 'st_ino', 'st_dev', 'st_nlink', 'st_uid', 'st_gid', 'st_size')
            return os.stat_result(fields, {n: getattr(info, n) for n in dir(info) if n.startswith('st_') and n not in shown})
        with self.simulated_windows() as calls, mock.patch.object(os, 'fstat', fstat_312):
            store = self.open()
            try:
                d = store.create_draft(BINDING, 'fictional-key-000001')
                u = store.reserve_upload(d['id'], d['capability'], fence(d), metadata(DATA), 'fictional-key-000002')
                d['revision'] = u['draftRevision']
                out = store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(DATA))
                stored = json.loads(store.db.execute('SELECT identity FROM files WHERE id=?', (out['fileId'],)).fetchone()[0])
                self.assertEqual(stored[0], SERIAL, 'the volume part is the handle serial')
                self.assertEqual(stored[1], real_fstat(os.open(self.root / 'attachments' / 'objects' / out['fileId'],
                                                               os.O_RDONLY)).st_ino)
                self.assertEqual(store.files.root_identity[0], SERIAL)
                row = store.db.execute('SELECT identity, digest FROM files WHERE id=?', (out['fileId'],)).fetchone()
                self.assertEqual(store.files.read(out['fileId'], json.loads(row['identity']), row['digest'], 1000), DATA)
            finally:
                store.close()
        self.assertIn('file_identity', calls)

    def test_the_store_runs_on_its_windows_branch(self):
        with self.simulated_windows() as calls:
            store = self.open()
            try:
                self.assertIs(store.files._windows, True)
                d = store.create_draft(BINDING, 'fictional-key-000001')
                u = store.reserve_upload(d['id'], d['capability'], fence(d), metadata(DATA), 'fictional-key-000002')
                d['revision'] = u['draftRevision']
                store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(DATA))
            finally:
                store.close()
        for name in ('anchored', 'open_folder', 'check_private_fd', 'file_identity', 'try_lock', 'sqlite_companions_private',
                     'close_lock'):
            self.assertIn(name, calls)

    def test_posix_stat_rule_refuses_on_windows(self):
        import attachments_files
        with self.simulated_windows():
            with self.assertRaises(AttachmentError):
                attachments_files.private(os.stat(self.root), True)

    def test_a_removal_check_that_reaches_for_posix_is_caught(self):
        """The simulation runs remove's check as Windows, so a POSIX-only call in it fails there too."""
        with self.simulated_windows():
            store = self.open()
            try:
                d = store.create_draft(BINDING, 'fictional-key-000001')
                u = store.reserve_upload(d['id'], d['capability'], fence(d), metadata(DATA), 'fictional-key-000002')
                d['revision'] = u['draftRevision']
                out = store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(DATA))
                file_id = out['fileId']
                with self.assertRaises(AssertionError):
                    store.files._objects.remove(file_id, lambda fd: os.getuid() is not None)
                self.assertIn(file_id, os.listdir(self.root / 'attachments' / 'objects'), 'nothing was removed')
            finally:
                store.close()

    def test_a_link_in_place_of_an_object_is_refused_as_on_posix(self):
        """POSIX's removal meets a link with ELOOP (or 'changed' when the absence must be durable); the Windows
        branch answers the same, though it never follows the link either way."""
        import errno
        with self.simulated_windows():
            store = self.open()
            try:
                objects = self.root / 'attachments' / 'objects'
                os.symlink(objects / 'elsewhere', objects / ('a' * 32))
                with self.assertRaises(OSError) as raised:
                    store.files.remove('a' * 32, [1, 2, 3, 4, 5])
                self.assertEqual(raised.exception.errno, errno.ELOOP)
                self.assertEqual(store.files.remove('a' * 32, [1, 2, 3, 4, 5], durable_absence=True), 'changed')
                self.assertTrue(os.path.islink(objects / ('a' * 32)))
            finally:
                store.close()

    def test_the_database_is_held_while_the_store_is_open(self):
        with self.simulated_windows():
            store = self.open()
            files = store.files
            try:
                hold = files._db_hold
                self.assertIsNotNone(hold)
                self.assertTrue(os.path.samestat(os.fstat(hold), os.stat(self.root / 'attachments' / 'ledger.sqlite3')))
            finally:
                store.close()
            self.assertIsNone(files._db_hold)
            with self.assertRaises(OSError):
                os.fstat(hold)


def volume_serial(folder):
    """The 32-bit volume serial number of the volume ``folder`` is on, asked of Windows separately."""
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    serial = w.DWORD()
    root = os.path.splitdrive(str(folder))[0] + '\\'
    if not kernel.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0):
        raise OSError(ctypes.get_last_error(), 'GetVolumeInformationW failed')
    return serial.value


def owner_of(path):
    """The owner of ``path`` as a SID string (test helper)."""
    import ctypes
    handle = ps._win_open(str(path), ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, 7, ps._OPEN_EXISTING, 0)
    try:
        adv, kernel, w = ps._win_api()[:3]
        owner, sd = w.LPVOID(), w.LPVOID()
        if adv.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(sd)):
            return 'unreadable'
        try:
            return ps._sid_text(owner)
        finally:
            kernel.LocalFree(sd)
    finally:
        ps._win_close(handle)


def owned_by_me(path):
    try:
        ps.windows_security(str(path))
        return True
    except ps.PrivateStorageError as e:
        return e.reason != 'owner'


def icacls(*args):
    subprocess.run(['icacls'] + list(args), check=True, capture_output=True, timeout=30)


def junction(link, target):
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True, timeout=30)


class CleanupWorkerTests(unittest.TestCase):
    """The officer cleanup thread, for real (never under the simulation): on every system where the store runs, a
    confirmed deletion is finished by DeletionWorker's sweep, which removes the file with durable_absence."""

    def setUp(self):
        import test_attachments_officer_storage as storage_fixtures
        if not storage_fixtures.STORE:
            self.skipTest(storage_fixtures.STORE_WHY)
        self.f = storage_fixtures.DirectStoreTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_the_worker_sweep_removes_a_confirmed_deletion(self):
        import threading
        from officer_document_deletion import OfficerDeletion, DeletionWorker
        f, store = self.f, self.f.store
        draft = f.draft()
        upload, _ = f.upload(draft)
        f.admit(draft)
        source = upload['fileId']
        deletions = OfficerDeletion(store)
        prepared = deletions.prepare(source, 'officer', f.binding['scopeId'], f.cid, 0, f.key())
        deletions.confirm(prepared['id'], prepared['capability'], prepared['fingerprint'], f.key())
        removed, real_remove = threading.Event(), store.files.remove
        calls = []

        def remove(file_id, *args, **kwargs):
            outcome = real_remove(file_id, *args, **kwargs)
            if file_id == source:
                calls.append((kwargs.get('durable_absence'), outcome))
                removed.set()
            return outcome
        with mock.patch.object(store.files, 'remove', side_effect=remove):
            worker = DeletionWorker(deletions)
            try:
                self.assertTrue(removed.wait(5), 'the worker never reached the file')
            finally:
                worker.close(5)
        self.assertIn((True, 'removed'), calls)
        self.assertFalse((store.files.root / 'objects' / source).exists())
        self.assertEqual(deletions.status(prepared['id'], prepared['capability'])['state'], 'deleted')


@unittest.skipUnless(os.name == 'nt', 'requires real Windows handles, access lists and junctions')
class WindowsStore(unittest.TestCase):
    """What only real Windows can show about the store on its Windows branch."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = state_root(self.temp.name)
        self.store = None
        self.serial = 0

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.temp.cleanup()          # fails if the store left a handle open

    def open(self):
        return AttachmentStore(self.root, enabled=True, owner_epoch=EPOCH, clock=lambda: 1_800_000_000.0)

    def key(self):
        self.serial += 1
        return 'fictional-key-%06d' % self.serial

    def error(self, code, fn):
        with self.assertRaises(AttachmentError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def uploaded(self, data=DATA):
        d = self.store.create_draft(BINDING, self.key())
        u = self.store.reserve_upload(d['id'], d['capability'], fence(d), metadata(data), self.key())
        d['revision'] = u['draftRevision']
        out = self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(data))
        d['revision'] = out['draftRevision']
        return d, out['fileId']

    def test_upload_read_restart_and_remove(self):
        self.store = self.open()
        self.assertIs(self.store.files._windows, True)
        d, file_id = self.uploaded()
        row = self.store.db.execute('SELECT identity, digest FROM files WHERE id=?', (file_id,)).fetchone()
        import json
        stored = json.loads(row['identity'])
        self.assertEqual(stored[0], volume_serial(self.root), 'the 32-bit volume serial, read from a handle')
        self.assertLessEqual(stored[0], 2 ** 32 - 1)
        self.assertEqual(self.store.files.read(file_id, json.loads(row['identity']), row['digest'], 1000), DATA)
        ps.windows_security(str(self.root / 'attachments' / 'objects' / file_id))      # private and protected
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.get_draft(d['id'], d['capability'])['files'][0]['state'], 'ready')
        self.store.remove_file(d['id'], d['capability'], fence(d), file_id)
        self.store.reconcile()
        self.assertFalse((self.root / 'attachments' / 'objects' / file_id).exists())
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    def test_a_second_store_is_refused_while_the_first_holds_the_lock(self):
        self.store = self.open()
        self.error('storage_unavailable', self.open)

    def test_a_junction_in_place_of_the_storage_or_objects_folder_is_refused(self):
        self.store = self.open()
        self.store.close()
        self.store = None
        for name in ('attachments', 'attachments\\objects'):
            with self.subTest(folder=name):
                real = self.root / name
                moved = real.with_name(real.name + '-moved')
                os.rename(real, moved)
                junction(real, moved)
                try:
                    self.error('storage_unavailable', self.open)
                finally:
                    os.rmdir(real)                    # removes the junction itself, not its target
                    os.rename(moved, real)
        self.store = self.open()                      # restored: it opens again

    def test_a_file_that_follows_the_folder_access_list_is_refused(self):
        """A file made by another program inherits the folder's list (not protected), which the file rule refuses:
        here an object put in place of a stored one."""
        self.store = self.open()
        d, file_id = self.uploaded()
        path = self.root / 'attachments' / 'objects' / file_id
        os.remove(path)
        with open(path, 'wb') as out:
            out.write(DATA)
        import json
        row = self.store.db.execute('SELECT identity, digest FROM files WHERE id=?', (file_id,)).fetchone()
        with self.assertRaises(AttachmentError):
            self.store.files.read(file_id, json.loads(row['identity']), row['digest'], 1000)

    def test_a_removal_waits_while_another_program_holds_the_file(self):
        self.store = self.open()
        d, file_id = self.uploaded()
        path = self.root / 'attachments' / 'objects' / file_id
        other = os.open(str(path), os.O_RDONLY)     # the C runtime's open does not share delete
        try:
            self.store.remove_file(d['id'], d['capability'], fence(d), file_id)
            self.assertTrue(path.exists(), 'kept while it is open elsewhere')
            self.assertGreater(self.store.storage()['usedBytes'], 0, 'still charged until it is really gone')
        finally:
            os.close(other)
        for _ in range(60):
            self.store.reconcile()
            if not path.exists():
                break
            import time
            time.sleep(0.05)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    def test_the_held_storage_folder_keeps_the_folders_above_in_place(self):
        self.store = self.open()
        with self.assertRaises(OSError):
            os.rename(self.root, str(self.root) + '-moved')
        with self.assertRaises(OSError):
            os.rename(self.root / 'attachments', self.root / 'attachments-moved')
        self.store.close()
        self.store = None
        os.rename(self.root / 'attachments', self.root / 'attachments-moved')    # released on close

    def test_the_database_cannot_be_replaced_while_the_store_is_open(self):
        self.store = self.open()
        database = self.root / 'attachments' / 'ledger.sqlite3'
        with self.assertRaises(OSError):
            os.replace(database, str(database) + '-moved')

    def reopen_refused(self, make, undo):
        """Close the store, ``make`` something in its folder, check a new store refuses it, ``undo`` it, and check
        the store opens again."""
        if self.store is not None:
            self.store.close()
            self.store = None
        make()
        try:
            self.error('storage_unavailable', self.open)
        finally:
            undo()
        self.store = self.open()

    def test_a_junction_in_place_of_a_journal_or_wal_is_refused(self):
        self.store = self.open()
        folder, elsewhere = self.root / 'attachments', self.root / 'elsewhere'
        elsewhere.mkdir()
        for suffix in ('-journal', '-wal'):
            with self.subTest(suffix=suffix):
                link = folder / ('ledger.sqlite3' + suffix)
                self.reopen_refused(lambda: junction(link, elsewhere), lambda: os.rmdir(link))
        self.assertEqual(os.listdir(elsewhere), [], 'nothing was written through the junction')

    def test_a_leftover_journal_is_kept_unless_others_can_open_it(self):
        self.store = self.open()
        self.store.close()
        self.store = None
        journal = self.root / 'attachments' / 'ledger.sqlite3-journal'

        def leftover():
            # Made the way SQLite makes it: by name, the folder's list, and the token's default owner (Administrators
            # on an elevated runner). No owner fix-up.
            if not journal.exists():
                journal.write_bytes(b'')
        leftover()
        self.store = self.open()                       # a plain private leftover journal is fine

        def opened_up():
            leftover()
            icacls(str(journal), '/grant', '*S-1-1-0:(R)')
        self.reopen_refused(opened_up, lambda: journal.unlink())

    def test_a_second_name_for_the_lock_or_the_database_is_refused(self):
        self.store = self.open()
        folder = self.root / 'attachments'
        for name in ('writer.lock', 'ledger.sqlite3'):
            with self.subTest(name=name):
                second = folder / (name + '-second')
                self.reopen_refused(lambda: os.link(folder / name, second), lambda: os.remove(second))

    def test_writes_succeed_while_sqlites_own_journal_exists(self):
        """SQLite makes its journal itself, with no security attributes, inside each write transaction, and the store
        checks the companions inside those transactions. On an elevated runner (windows-latest) that journal belongs
        to Administrators. Real writes must still succeed, with no icacls fix-up anywhere."""
        self.store = self.open()
        journal = self.root / 'attachments' / 'ledger.sqlite3-journal'
        seen, check = [], self.store.files.verify_database

        def watched():
            if journal.exists():
                seen.append(owner_of(journal))
            return check()
        with mock.patch.object(self.store.files, 'verify_database', watched):
            d, file_id = self.uploaded()
            self.store.remove_file(d['id'], d['capability'], fence(d), file_id)
            self.store.reconcile()
        self.assertTrue(seen, 'some check ran while SQLite had its journal')
        print('\nWindows answer: SQLite\'s own journal was owned by %s during the store\'s checks.' % sorted(set(seen)))
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    def test_text_preview_reads_through_the_resource_locks(self):
        from attachments_catalogue import Catalogue
        self.store = self.open()
        d, file_id = self.uploaded()
        catalogue = Catalogue(self.store)
        capability = next(f['sourceCapability'] for f in catalogue.draft(d['id'], d['capability'])['files']
                          if f['id'] == file_id)
        snapshot = catalogue.preview_snapshot(capability, file_id)
        self.assertIn('Fictional orchard', str(catalogue.publish_preview(snapshot, lambda value: value)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
