"""Real private-store tests using fictional keys/files; no providers or live state."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_store import AttachmentStore
from attachments_types import AttachmentError, DEFAULT_LIMITS
from attachments_prepare import prepare
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root

EPOCH = 'a' * 64
BINDING = {'kind': 'agent', 'id': 'fictional-ada', 'scopeId': 'fictional-orchard',
           'conversationId': None, 'modelId': None, 'capabilityRevision': 'fictional-v1'}


def fence(draft, writer=None):
    return {'revision': draft['revision'], 'writerGeneration': draft['writerGeneration'],
            'writerNonce': writer if writer is not None else draft['writerNonce']}


def metadata(data, media='text/plain', name='sample.txt'):
    return {'name': name, 'mediaType': media, 'bytes': len(data), 'digest': hashlib.sha256(data).hexdigest()}


@unittest.skipUnless(STORE, STORE_WHY)
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = state_root(self.temp.name)
        self.clock = [1_800_000_000.0]
        self.store = self.open()
        self.serial = 0

    def open(self, **extra):
        return AttachmentStore(self.root, enabled=True, owner_epoch=EPOCH, clock=lambda: self.clock[0], **extra)

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.temp.cleanup()

    def key(self):
        self.serial += 1
        return 'fictional-key-%06d' % self.serial

    def draft(self):
        return self.store.create_draft(BINDING, self.key())

    def reserve(self, d, data, media='text/plain', name='sample.txt'):
        u = self.store.reserve_upload(d['id'], d['capability'], fence(d), metadata(data, media, name), self.key())
        d['revision'] = u['draftRevision']
        return u

    def upload(self, d, data=b'Fictional orchard\nLine two\n', media='text/plain'):
        u = self.reserve(d, data, media)
        out = self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(data))
        d['revision'] = out['draftRevision']
        return out

    def error(self, code, fn):
        with self.assertRaises(AttachmentError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_disabled_demo_create_no_files(self):
        for args in ({}, {'enabled': False}, {'enabled': True, 'demo': True}):
            candidate = self.root / 'does-not-exist'
            store = AttachmentStore(candidate, **args)
            self.assertFalse(candidate.exists())
            self.error('attachments_disabled', store.storage)
            store.close()

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_permissions_lock_and_restart(self):
        root = self.root / 'attachments'
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / 'ledger.sqlite3').stat().st_mode), 0o600)
        self.error('storage_unavailable', lambda: self.open())
        d = self.draft()
        self.upload(d)
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.get_draft(d['id'], d['capability'])['files'][0]['state'], 'ready')

    def test_unknown_version_rejects_without_reset(self):
        self.store.close()
        db = self.root / 'attachments' / 'ledger.sqlite3'
        con = sqlite3.connect(db)
        con.execute('PRAGMA user_version=900')
        con.commit()
        con.close()
        self.error('schema_unavailable', lambda: self.open())
        con = sqlite3.connect(db)
        self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 900)
        con.close()
        self.store = None

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_private_root_symlink_hardlink_permissions_refused(self):
        self.store.close()
        root = self.root / 'attachments'
        root.rename(self.root / 'real')
        root.symlink_to(self.root / 'real', target_is_directory=True)
        self.error('storage_unavailable', lambda: self.open())
        root.unlink()
        (self.root / 'real').rename(root)
        db = root / 'ledger.sqlite3'
        os.link(db, root / 'link')
        self.error('storage_unavailable', lambda: self.open())
        (root / 'link').unlink()
        db.chmod(0o644)
        self.error('storage_unavailable', lambda: self.open())
        db.chmod(0o600)
        self.store = self.open()

    def test_draft_idempotency_changed_fingerprint_and_no_implicit_scope(self):
        command = self.key()
        d = self.store.create_draft(BINDING, command)
        self.assertEqual(d, self.store.create_draft(BINDING, command))
        self.error('request_conflict', lambda: self.store.create_draft(dict(BINDING, id='fictional-bram'), command))
        another = self.draft()
        self.assertNotEqual(d['id'], another['id'])
        self.error('forbidden', lambda: self.store.get_draft(d['id'], another['capability']))
        self.error('invalid_request', lambda: self.store.create_draft(dict(BINDING, id='../escape'), self.key()))

    def test_explicit_resume_and_recovery_revoke_old_writer(self):
        d = self.draft()
        new = self.store.resume_draft(d['id'], d['capability'], d['revision'], d['writerGeneration'])
        self.error('forbidden', lambda: self.store.edit_text(d['id'], d['capability'], fence(d), 'old'))
        self.error('draft_conflict', lambda: self.store.edit_text(new['id'], new['capability'], fence(d), 'old'))
        recovery = self.store.recover_draft(new['id'], new['writerGeneration'])
        final = self.store.resume_draft(new['id'], recovery['recoveryCapability'], new['revision'], new['writerGeneration'])
        result = self.store.edit_text(final['id'], final['capability'], fence(final), 'new words')
        self.assertEqual(result['text'], 'new words')

    def test_recovery_token_expires_but_draft_does_not(self):
        d = self.draft()
        recovery = self.store.recover_draft(d['id'], 1)
        self.clock[0] += 301
        self.error('forbidden', lambda: self.store.resume_draft(d['id'], recovery['recoveryCapability'], 1, 1))
        self.clock[0] += 50 * 86400
        self.assertEqual(self.store.get_draft(d['id'], d['capability'])['state'], 'active')

    def test_owner_epoch_rotation_revokes_capabilities_keeps_sources(self):
        d = self.draft()
        self.upload(d)
        self.store.close()
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch='b' * 64)
        self.error('forbidden', lambda: self.store.get_draft(d['id'], d['capability']))
        recovery = self.store.recover_draft(d['id'], 2)
        current = self.store.resume_draft(d['id'], recovery['recoveryCapability'], recovery['revision'], 2)
        self.assertEqual(current['files'][0]['state'], 'ready')

    def test_text_roundtrip_original_lines_digest_immutable(self):
        d = self.draft()
        data = '# Orchard\r\n<script>fiction only</script>\n'.encode()
        u = self.upload(d, data, 'text/markdown')
        self.assertEqual(u['state'], 'ready')
        view = self.store.read_text(d['id'], d['capability'], u['fileId'])
        self.assertEqual(view['text'], data.decode())
        self.assertEqual(view['lines'][1], {'number': 2, 'text': '<script>fiction only</script>'})
        self.assertEqual(self.store.storage()['usedBytes'], len(data))
        # Rendering is a future boundary; this module returns plain strings only.
        self.assertEqual(view['kind'], 'text')
        path = self.root / 'attachments' / 'objects' / u['fileId']
        path.write_bytes(b'changed')
        self.error('source_changed', lambda: self.store.read_text(d['id'], d['capability'], u['fileId']))

    def test_unsupported_parsers_and_invalid_text_never_ready(self):
        for data, media, code in ((b'%PDF-1.7', 'application/pdf', 'preparation_unavailable'),
                                  (b'\x89PNG', 'image/png', 'preparation_unavailable'),
                                  (b'\xff\xd8', 'image/jpeg', 'preparation_unavailable'),
                                  (b'bad\xff', 'text/plain', 'invalid_text'),
                                  (b'zero\x00', 'text/markdown', 'invalid_text')):
            d = self.draft()
            u = self.upload(d, data, media)
            self.assertEqual((u['state'], u['error']), ('failed', code))
            self.error('source_unavailable', lambda: self.store.read_text(d['id'], d['capability'], u['fileId']))

    def test_upload_idempotency_count_and_changed_metadata(self):
        d, data, command = self.draft(), b'abc', self.key()
        md = metadata(data)
        u = self.store.reserve_upload(d['id'], d['capability'], fence(d), md, command)
        self.assertEqual(u, self.store.reserve_upload(d['id'], d['capability'], fence(d), md, command))
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        self.error('request_conflict', lambda: self.store.reserve_upload(d['id'], d['capability'], fence(d), metadata(b'ab'), command))
        d['revision'] = u['draftRevision']
        out = self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(data))
        class Unreadable:
            def read(self, _):
                raise AssertionError('Completed upload must not read a second body.')
        self.assertEqual(out, self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], Unreadable()))
        self.assertEqual(self.store.storage()['usedBytes'], 3)

    def test_short_oversized_wrong_digest_and_explicit_same_upload_retry(self):
        for supplied in (b'ab', b'abcd', b'xyz'):
            d = self.draft()
            u = self.reserve(d, b'abc')
            with self.assertRaises(AttachmentError):
                self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(supplied))
            self.assertEqual(self.store.storage()['usedBytes'] % 3, 0)
            out = self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(b'abc'))
            self.assertEqual(out['state'], 'ready')

    def test_no_default_expiry_after_day31_and_restart(self):
        d = self.draft()
        u = self.upload(d)
        before = self.store.storage()['usedBytes']
        self.clock[0] += 100 * 86400
        self.store.reconcile()
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertIsNone(self.store.get_draft(d['id'], d['capability'])['expiresAt'])
        self.assertIsNone(self.store.get_upload(d['id'], d['capability'], u['id'])['expiresAt'])
        self.assertEqual(self.store.read_text(d['id'], d['capability'], u['fileId'])['lineCount'], 2)

    def test_discard_reclaims_one_slot_once_and_tombstone_capacity_stays(self):
        self.store.close()
        self.store = self.open(overrides={'drafts': 2, 'seen': 6})
        a, b = self.draft(), self.draft()
        self.error('draft_capacity', self.draft)
        command = self.key()
        result = self.store.discard_draft(a['id'], a['capability'], fence(a), command)
        self.assertEqual(result['state'], 'discarded')
        self.assertEqual(result, self.store.discard_draft(a['id'], a['capability'], fence(a), command))
        self.assertEqual(self.store.storage()['activeDrafts'], 1)
        self.draft()
        self.error('draft_capacity', self.draft)
        self.store.discard_draft(b['id'], b['capability'], fence(b), self.key())
        self.error('history_capacity', self.draft)
        self.error('forbidden', lambda: self.store.edit_text(a['id'], a['capability'], fence(a), 'cannot resurrect'))

    def test_discard_verified_bytes_release_and_changed_copy_debt(self):
        a = self.draft()
        u = self.upload(a, b'abc')
        self.store.discard_draft(a['id'], a['capability'], fence(a), self.key())
        self.assertEqual(self.store.storage()['usedBytes'], 0)
        b = self.draft()
        u = self.upload(b, b'abc')
        path = self.root / 'attachments' / 'objects' / u['fileId']
        path.unlink()
        path.write_bytes(b'replacement')
        result = self.store.discard_draft(b['id'], b['capability'], fence(b), self.key())
        self.assertTrue(result['cleanupPending'])
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        self.assertEqual(path.read_bytes(), b'replacement')
        self.store.reconcile()
        self.assertEqual(self.store.storage()['usedBytes'], 3)

    def test_remove_file_does_not_affect_other_drafts(self):
        a, b = self.draft(), self.draft()
        ua, ub = self.upload(a, b'abc'), self.upload(b, b'xyz')
        self.error('forbidden', lambda: self.store.remove_file(a['id'], a['capability'], fence(a), ub['fileId']))
        self.store.remove_file(a['id'], a['capability'], fence(a), ua['fileId'])
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        self.assertEqual(self.store.read_text(b['id'], b['capability'], ub['fileId'])['text'], 'xyz')

    def test_concurrent_reservation_exact_limit(self):
        self.store.close()
        self.store = self.open(overrides={'bytes': 3})
        drafts = [self.draft(), self.draft()]
        out = []
        def reserve(d):
            try:
                out.append(self.reserve(d, b'abc')['id'])
            except AttachmentError as e:
                out.append(e.code)
        workers = [threading.Thread(target=reserve, args=(d,)) for d in drafts]
        for w in workers: w.start()
        for w in workers: w.join()
        self.assertEqual(out.count('storage_capacity'), 1)
        self.assertEqual(self.store.storage()['usedBytes'], 3)

    def test_discard_during_stream_keeps_debt_until_fd_closes(self):
        d = self.draft()
        u = self.reserve(d, b'abc')
        entered, resume = threading.Event(), threading.Event()
        class Reader:
            def read(self, _):
                entered.set()
                resume.wait(3)
                return b'abc'
        result = []
        def run():
            try:
                self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], Reader())
            except AttachmentError as e:
                result.append(e.code)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(3))
        outcome = self.store.discard_draft(d['id'], d['capability'], fence(d), self.key())
        self.assertTrue(outcome['cleanupPending'])
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        resume.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ['draft_conflict'])
        self.assertEqual(self.store.storage()['usedBytes'], 0)
        self.assertEqual(self.store.storage()['activeDrafts'], 0)

    def test_real_sqlite_page_capacity_preserves_every_discard_receipt(self):
        self.store.close()
        # Schema/index additions consume fixed pages; still saturate the actual
        # 512 KiB SQLite cap through production APIs (production cap is 128 MiB).
        self.store = self.open(overrides={'database_bytes': 524288})
        drafts = []
        while len(drafts) < 1000:
            try:
                drafts.append(self.draft())
            except AttachmentError as exc:
                self.assertIn(exc.code, ('history_capacity', 'storage_unavailable'))
                break
        self.assertGreater(len(drafts), 0)
        self.assertLess(len(drafts), 1000)  # Exercise actual SQLite pages, not draft count.
        actual_pages = self.store.db.execute('PRAGMA page_count').fetchone()[0]
        self.assertGreaterEqual(actual_pages, 20)
        commands = []
        for d in drafts:
            command = ('fictional-discard-' + str(len(commands))).ljust(128, '_')
            commands.append(command)
            result = self.store.discard_draft(d['id'], d['capability'], fence(d), command)
            self.assertEqual(result['state'], 'discarded')
            self.assertFalse(result['cleanupPending'])
        self.assertEqual(self.store.storage()['activeDrafts'], 0)
        self.store.close()
        self.store = self.open(overrides={'database_bytes': 524288})
        for d, command in zip(drafts, commands):
            self.assertEqual(self.store.discard_draft(d['id'], d['capability'], fence(d), command)['state'], 'discarded')
        self.assertEqual(self.store.storage()['activeDrafts'], 0)
        self.draft()  # Freed mutable draft payload allows admission again, not identity reuse.

    def test_bounded_reconcile_rotates_past_permanent_failure_and_survives_restart(self):
        bad = self.draft()
        good = self.draft()
        ub = self.upload(bad, b'bad')
        ug = self.upload(good, b'good')
        target = self.root / 'attachments' / 'objects' / ub['fileId']
        target.unlink()
        target.write_bytes(b'changed replacement')
        self.store.discard_draft(bad['id'], bad['capability'], fence(bad), self.key())
        # Leave the good copy pending at the real durable crash boundary.
        def crash(stage):
            if stage == 'discard_committed':
                raise RuntimeError('fictional process termination')
        self.store.checkpoint = crash
        with self.assertRaises(RuntimeError):
            self.store.discard_draft(good['id'], good['capability'], fence(good), self.key())
        self.store.checkpoint = lambda _: None
        # Start immediately before the bad identity in indexed order, making the
        # first one-row batch select the permanent failure regardless of random IDs.
        before_bad = format(int(ub['fileId'], 16) - 1, '032x')
        self.store.db.execute("UPDATE meta SET value=? WHERE key='cleanup_cursor'", (before_bad,))
        self.assertEqual(self.store.storage()['usedBytes'], 7)
        self.store.reconcile(maximum=1)
        self.assertEqual(self.store.storage()['usedBytes'], 7)
        for _ in range(3):
            self.store.reconcile(maximum=1)
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        self.assertFalse((target.parent / ug['fileId']).exists())
        self.assertEqual(target.read_bytes(), b'changed replacement')
        previous = self.store.db.execute("SELECT value FROM meta WHERE key='cleanup_cursor'").fetchone()[0]
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        for _ in range(3):
            self.store.reconcile(maximum=1)
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        # Previously deleted rows never consume candidate slots.
        candidates = self.store.db.execute('SELECT id FROM files WHERE needs_cleanup=1').fetchall()
        self.assertEqual([row[0] for row in candidates], [ub['fileId']])

    def test_startup_cleanup_batch_skips_deleted_rows_and_advances_failure_cursor(self):
        discarded = self.draft()
        ud = self.upload(discarded, b'done')
        self.store.discard_draft(discarded['id'], discarded['capability'], fence(discarded), self.key())
        good = self.draft()
        ug = self.upload(good, b'pending')
        def crash(stage):
            if stage == 'discard_committed':
                raise RuntimeError('fictional process termination')
        self.store.checkpoint = crash
        with self.assertRaises(RuntimeError):
            self.store.discard_draft(good['id'], good['capability'], fence(good), self.key())
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.storage()['usedBytes'], 0)
        self.assertEqual(self.store.storage()['cleanupPending'], 0)
        cursor = self.store.db.execute("SELECT value FROM meta WHERE key='cleanup_cursor'").fetchone()[0]
        self.assertEqual(cursor, ug['fileId'])
        self.assertNotEqual(cursor, ud['fileId'])

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_lock_replacement_and_unsafe_sidecar_fail_before_sqlite(self):
        lock = self.root / 'attachments' / 'writer.lock'
        lock.rename(lock.with_name('old-lock'))
        lock.write_bytes(b'')
        lock.chmod(0o600)
        self.error('storage_unavailable', self.store.storage)
        self.store.close()
        lock.unlink()
        lock.with_name('old-lock').rename(lock)
        sidecar = self.root / 'attachments' / 'ledger.sqlite3-journal'
        target = self.root / 'must-not-open'
        target.write_bytes(b'fictional protected bytes')
        sidecar.symlink_to(target)
        self.error('storage_unavailable', lambda: self.open())
        self.assertEqual(target.read_bytes(), b'fictional protected bytes')
        sidecar.unlink()
        self.store = self.open()

    def test_agent_and_officer_text_limits_are_distinct(self):
        d = self.draft()
        self.error('text_limit', lambda: self.store.edit_text(d['id'], d['capability'], fence(d), 'x' * 2001))
        cid = self.store.create_conversation('whole-room', self.key())['id']
        d = self.store.create_draft(dict(BINDING, kind='officer', id='officer', scopeId='whole-room', conversationId=cid, modelId='fictional-model'), self.key())
        self.assertEqual(len(self.store.edit_text(d['id'], d['capability'], fence(d), 'x' * 4000)['text']), 4000)

    def test_discard_receipt_does_not_survive_owner_epoch_as_authority(self):
        d, command = self.draft(), self.key()
        self.store.discard_draft(d['id'], d['capability'], fence(d), command)
        self.store.close()
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch='b' * 64)
        self.error('request_conflict', lambda: self.store.discard_draft(d['id'], d['capability'], fence(d), command))

    def test_crash_after_discard_commit_recovers_cleanup_without_resurrection(self):
        d = self.draft()
        self.upload(d, b'abc')
        def crash(stage):
            if stage == 'discard_committed':
                raise RuntimeError('fictional process termination')
        self.store.checkpoint = crash
        command = self.key()
        with self.assertRaises(RuntimeError):
            self.store.discard_draft(d['id'], d['capability'], fence(d), command)
        self.assertEqual(self.store.storage()['activeDrafts'], 0)
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.storage()['usedBytes'], 0)
        self.assertFalse(self.store.discard_draft(d['id'], d['capability'], fence(d), command)['cleanupPending'])

    def test_crash_after_upload_write_requires_same_explicit_retry(self):
        d = self.draft()
        u = self.reserve(d, b'abc')
        def crash(stage):
            if stage == 'upload_written':
                raise RuntimeError('fictional process termination')
        self.store.checkpoint = crash
        with self.assertRaises(RuntimeError):
            self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(b'abc'))
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.get_upload(d['id'], d['capability'], u['id'])['state'], 'failed')
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        result = self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(b'abc'))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(self.store.storage()['usedBytes'], 3)

    def test_two_receivers_cannot_write_same_upload(self):
        d = self.draft()
        u = self.reserve(d, b'abc')
        entered, resume = threading.Event(), threading.Event()
        class Reader(io.BytesIO):
            def read(self, n):
                entered.set()
                resume.wait(3)
                return super().read(n)
        failures = []
        def run():
            try:
                self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], Reader(b'abc'))
            except Exception as exc:
                failures.append(exc)
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(entered.wait(3))
        self.error('upload_busy', lambda: self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], io.BytesIO(b'abc')))
        resume.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.store.storage()['usedBytes'], 3)

    def test_takeover_fences_late_stream_without_removing_retained_draft(self):
        d = self.draft()
        u = self.reserve(d, b'abc')
        entered, resume = threading.Event(), threading.Event()
        class Reader(io.BytesIO):
            def read(self, n):
                entered.set()
                resume.wait(3)
                return super().read(n)
        result = []
        def run():
            try:
                self.store.receive_upload(d['id'], d['capability'], fence(d), u['id'], u['fingerprint'], Reader(b'abc'))
            except AttachmentError as exc:
                result.append(exc.code)
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(entered.wait(3))
        new = self.store.resume_draft(d['id'], d['capability'], d['revision'], d['writerGeneration'])
        resume.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, ['draft_conflict'])
        self.assertEqual(self.store.storage()['usedBytes'], 3)
        output = self.store.receive_upload(new['id'], new['capability'], fence(new), u['id'], u['fingerprint'], io.BytesIO(b'abc'))
        self.assertEqual(output['state'], 'ready')

    def test_oversized_file_draft_count_and_object_limits_reject_without_charge(self):
        self.store.close()
        self.store = self.open(overrides={'file_bytes': 4, 'draft_bytes': 5, 'files': 2})
        d = self.draft()
        self.error('file_too_large', lambda: self.reserve(d, b'abcde'))
        self.reserve(d, b'abc')
        self.error('draft_limit', lambda: self.reserve(d, b'abc'))
        self.reserve(d, b'ab')
        self.error('draft_limit', lambda: self.reserve(d, b''))
        self.assertEqual(self.store.storage()['usedBytes'], 5)

    def test_empty_and_sanitized_errors_do_not_expose_source(self):
        d = self.draft()
        u = self.upload(d, b'')
        self.assertEqual(self.store.read_text(d['id'], d['capability'], u['fileId'])['lineCount'], 0)
        self.error('unsupported_type', lambda: self.reserve(d, b'fictional private text', media=[]))
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    def test_officer_conversation_exact_scope_and_default_durable_identity(self):
        cid = self.store.create_conversation('whole-room', self.key())['id']
        binding = dict(BINDING, kind='officer', id='officer', scopeId='whole-room', conversationId=cid, modelId='fictional-model')
        d = self.store.create_draft(binding, self.key())
        self.upload(d)
        self.error('forbidden', lambda: self.store.create_draft(dict(binding, scopeId='different-room'), self.key()))
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.get_draft(d['id'], d['capability'])['destination']['conversationId'], cid)

    def test_data_limits_and_filename_is_not_storage_path(self):
        d = self.draft()
        u = self.reserve(d, b'abc', name='../fictional\nname.txt')
        self.assertEqual(self.store.get_draft(d['id'], d['capability'])['files'][0]['name'], '.._fictional_name.txt')
        self.assertEqual(DEFAULT_LIMITS['bytes'], 5_000_000_000)
        self.error('file_too_large', lambda: self.store.reserve_upload(d['id'], d['capability'], fence(d), dict(metadata(b''), bytes=True), self.key()))
        self.error('text_limit', lambda: self.store.edit_text(d['id'], d['capability'], fence(d), 'x' * 4001))


class PureTests(unittest.TestCase):
    def test_text_limits_invalid_encoding_and_no_markup_rendering(self):
        with self.assertRaises(AttachmentError):
            prepare(b'a\nb\nc', 'text/plain', max_bytes=100, max_lines=2)
        self.assertEqual(prepare(b'<b>x</b>', 'text/markdown', max_bytes=100, max_lines=2)['text'], '<b>x</b>')


if __name__ == '__main__':
    unittest.main()
