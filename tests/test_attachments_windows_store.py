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
        big = os.stat_result((0o100600, 2 ** 63, 1, 1, 0, 0, 0, 0, 0, 0))
        with self.assertRaises(AttachmentError):
            attachments_files._id_ok(big)
        attachments_files._id_ok(os.stat_result((0o100600, 2 ** 63 - 1, 2 ** 63 - 1, 1, 0, 0, 0, 0, 0, 0)))
        with self.assertRaises(AttachmentError):
            attachments_files._id_ok(os.stat_result((0o100600, 0, 1, 1, 0, 0, 0, 0, 0, 0)))

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
        for name in ('anchored', 'open_folder', 'check_private_fd', 'try_lock', 'sqlite_companions_plain', 'close_lock'):
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


def icacls(*args):
    subprocess.run(['icacls'] + list(args), check=True, capture_output=True, timeout=30)


def junction(link, target):
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True, timeout=30)


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
