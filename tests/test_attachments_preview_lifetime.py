"""Actual disposable descriptors: preview revocation, accounting and closure faults."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_attachments_catalogue as catalogue_fixtures
from test_attachments_foundation import fence
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


@unittest.skipUnless(STORE, STORE_WHY)
class PreviewLifetimeTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'open', 'key', 'error', 'draft', 'cap', 'publish'):
        locals()[_name] = getattr(catalogue_fixtures.CatalogueTests, _name)
    del _name
    cat = catalogue_fixtures.CatalogueTests.cat

    def test_remove_and_discard_revoke_but_keep_open_bytes_charged(self):
        for action in ('remove', 'discard'):
            with self.subTest(action=action):
                d,u=self.draft();fid=u['fileId'];cap=self.cap(d,fid)
                before=self.store.storage()['usedBytes'];read=os.read;seen=[]
                def reading(fd,count):
                    if not seen:
                        seen.append(fd)
                        if action=='remove':self.store.remove_file(d['id'],d['capability'],fence(d),fid)
                        else:self.store.discard_draft(d['id'],d['capability'],fence(d),self.key())
                        self.store.reconcile()
                        self.assertEqual(self.store.storage()['usedBytes'],before)
                        self.assertEqual(os.fstat(fd).st_nlink,1)
                        self.assertTrue(self.store.files.io_active())
                    return read(fd,count)
                with mock.patch('attachments_files.os.read',side_effect=reading):
                    snapshot=self.cat.preview_snapshot(cap,fid)
                self.error('forbidden',lambda:self.publish(snapshot))
                self.assertTrue(seen);self.assertFalse(self.store.files.io_active())
                self.store.reconcile();self.assertEqual(self.store.storage()['usedBytes'],0)
                self.store.reconcile();self.assertEqual(self.store.storage()['usedBytes'],0)
                with self.assertRaises(OSError):os.fstat(seen[0])

    def test_read_failure_with_confirmed_close_releases_pin(self):
        d,u=self.draft();cap=self.cap(d,u['fileId'])
        with mock.patch('attachments_files.os.read',side_effect=OSError('fictional read failure')):
            self.error('source_unavailable',lambda:self.cat.preview_snapshot(cap,u['fileId']))
        self.assertFalse(self.store.files.io_active())
        self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId'])
        self.assertEqual(self.store.storage()['usedBytes'],0)

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_failed_open_is_not_a_live_handle(self):
        d,u=self.draft();cap=self.cap(d,u['fileId']);opening=os.open
        def fail_source(path,*args,**kwargs):
            if path==u['fileId']:raise OSError('fictional open failure')
            return opening(path,*args,**kwargs)
        with mock.patch('attachments_files.os.open',side_effect=fail_source):
            self.error('source_unavailable',lambda:self.cat.preview_snapshot(cap,u['fileId']))
        self.assertFalse(self.store.files.io_active());self.assertFalse(self.store.request_handles)

    def test_genuinely_failed_read_close_pins_bytes_and_shutdown(self):
        d,u=self.draft();fid=u['fileId'];cap=self.cap(d,fid)
        read,close=os.read,os.close;held=[]
        def reading(fd,count):
            if not held:held.append(fd)
            return read(fd,count)
        def closing(fd):
            if held and fd==held[0]:raise OSError('fictional close failure')
            return close(fd)
        try:
            with mock.patch('attachments_files.os.read',side_effect=reading),mock.patch('attachments_files.os.close',side_effect=closing):
                self.error('source_unavailable',lambda:self.cat.preview_snapshot(cap,fid))
            self.assertTrue(self.store.files.io_active());self.assertFalse(self.store.request_handles)
            self.assertEqual(os.fstat(held[0]).st_size,len(b'Fictional orchard'))
            before=self.store.storage()['usedBytes'];db=self.store.db
            self.error('upload_busy',self.store.close);self.assertIs(self.store.db,db)
            self.store.remove_file(d['id'],d['capability'],fence(d),fid);self.store.reconcile()
            self.assertEqual(self.store.storage()['usedBytes'],before)
            self.assertEqual(os.fstat(held[0]).st_nlink,1)
            with mock.patch('attachments_files.os.open',side_effect=AssertionError('must not open another descriptor')):
                self.error('source_unavailable',lambda:self.store.files.read(fid,[],None,100))
        finally:
            # Test-only disposal of the exact known injected descriptor. Production
            # has no reset/retry-number API and keeps the uncertain pin until exit.
            if held:close(held[0])
            self.store.files._io.clear()

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_unlinked_remove_descriptor_close_failure_stays_charged(self):
        d,u=self.draft();fid=u['fileId'];before=self.store.storage()['usedBytes']
        read,close=os.read,os.close;held=[]
        def reading(fd,count):
            if not held:held.append(fd)
            return read(fd,count)
        def closing(fd):
            if held and fd==held[0]:raise OSError('fictional removal close failure')
            return close(fd)
        try:
            with mock.patch('attachments_files.os.read',side_effect=reading),mock.patch('attachments_files.os.close',side_effect=closing):
                self.store.remove_file(d['id'],d['capability'],fence(d),fid)
            self.assertEqual(os.fstat(held[0]).st_nlink,0)
            self.assertEqual(self.store.storage()['usedBytes'],before)
            self.store.reconcile();self.assertEqual(self.store.storage()['usedBytes'],before)
            self.assertTrue(self.store.files.io_active());self.error('upload_busy',self.store.close)
        finally:
            if held:close(held[0])
            self.store.files._io.clear()

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_remove_registration_blocks_new_read_before_unlink(self):
        d,u=self.draft();fid=u['fileId'];row=self.store.db.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        unlink=os.unlink;seen=[]
        def unlinking(path,*args,**kwargs):
            if path==fid:
                seen.append(path)
                self.error('source_unavailable',lambda:self.store.files.read(fid,json.loads(row['identity']),row['digest'],100))
                self.error('upload_busy',self.store.close)
            return unlink(path,*args,**kwargs)
        with mock.patch('attachments_files.os.unlink',side_effect=unlinking):
            self.store.remove_file(d['id'],d['capability'],fence(d),fid)
        self.assertEqual(seen,[fid]);self.assertFalse(self.store.files.io_active())
        self.assertEqual(self.store.storage()['usedBytes'],0)

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_close_fences_new_reads_and_removals_before_descriptor_teardown(self):
        d,u=self.draft();files=self.store.files;close=os.close;seen=[]
        def closing(fd):
            if fd==files.objects_fd and not seen:
                seen.append(fd)
                self.error('storage_unavailable',lambda:files.read(u['fileId'],[],None,100))
                self.error('storage_unavailable',lambda:files.remove(u['fileId'],None))
            return close(fd)
        with mock.patch('attachments_files.os.close',side_effect=closing):self.store.close()
        self.assertEqual(len(seen),1)


if __name__=='__main__':unittest.main()
