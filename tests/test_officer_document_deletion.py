"""Causal kept-original deletion proofs in disposable stores, without providers."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_types import AttachmentError
from attachments_store import AttachmentStore
from attachments_officer_storage import OfficerDirectRetentionProfile
from attachments_conversations import OfficerConversations
from attachments_service import AttachmentService, UnqualifiedResolver
from attachments_deletion_slots import SOURCE_PREVIEW, SOURCE_RECEIPT
from attachments_http import handle_http
from officer_document_deletion import OfficerDeletion, DeletionWorker, DELETING, UNKNOWN
import test_attachments_officer_storage as storage_fixtures
from test_attachments_officer_storage import PDF
from test_attachments_http import Handler, KEY
from test_attachments_foundation import EPOCH
from test_attachments_foundation import fence, metadata
import test_officer_document_ledger as ledger_fixtures
import test_officer_document_intents as intent_fixtures
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, private_folder


@unittest.skipUnless(STORE, STORE_WHY)        # on Windows too: the cleanup worker runs there
class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.f = storage_fixtures.DirectStoreTests(); self.f.setUp()
        self.store = self.f.store
        self.l = OfficerDeletion(self.store)
        self.workers = []

    def tearDown(self):
        for worker in self.workers: worker.close(30)
        self.f.tearDown()

    def add(self, raw=PDF):
        draft = self.f.draft(); upload, _ = self.f.upload(draft, raw); self.f.admit(draft)
        return upload['fileId']

    def page(self, cursor=None):
        return self.l.management('officer', self.f.binding['scopeId'], self.f.cid, cursor)

    def prepare(self, source=None, generation=0, command=None):
        return self.l.prepare(source or self.add(), 'officer', self.f.binding['scopeId'], self.f.cid,
                              generation, command or self.f.key())

    def confirm(self, prepared, command=None):
        return self.l.confirm(prepared['id'], prepared['capability'], prepared['fingerprint'], command or self.f.key())

    def status(self, operation): return self.l.status(operation['id'], operation['capability'])

    def error(self, code, action):
        with self.assertRaises(AttachmentError) as caught: action()
        self.assertEqual(caught.exception.code, code)

    def test_prepare_replay_cas_replacement_expiry_and_stale_capability(self):
        source = self.add(); used = self.store.storage()['usedBytes']; command = self.f.key()
        with mock.patch.object(self.store.files, 'read', side_effect=AssertionError('metadata opened source')):
            first = self.prepare(source, command=command)
            self.assertEqual(first, self.prepare(source, command=command))
        self.assertEqual(first['state'], 'confirm'); self.assertEqual(first['revision'], 1)
        self.assertIsNone(first['removedAt']); self.assertFalse(first['hasUnresolvedQuestion'])
        self.assertTrue(self.page()['files'][0]['available']); self.assertEqual(self.store.storage()['usedBytes'], used)
        second = self.prepare(source, generation=1)
        self.assertEqual(second['id'], first['id']); self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        self.error('forbidden', lambda: self.status(first))
        self.error('forbidden', lambda: self.confirm(first))
        self.error('deletion_changed', lambda: self.prepare(source, command=command))
        self.f.clock[0] += 301
        self.assertFalse(self.status(second)['canConfirm'])
        self.error('deletion_changed', lambda: self.confirm(second))

    def test_confirm_logically_revokes_then_owned_removal_refunds_once_preserves_provenance(self):
        source = self.add(); prepared = self.prepare(source); used = self.store.storage()['usedBytes']
        original = dict(self.store.db.execute('SELECT * FROM files WHERE id=?', (source,)).fetchone())
        old = self.f.officer.context_sources(self.f.selection)['dependencies']
        command = self.f.key(); confirmed = self.confirm(prepared, command)
        self.assertEqual(confirmed['state'], 'deleting'); self.assertTrue(confirmed['sourceUnavailable'])
        self.assertFalse(self.page()['files'][0]['available']); self.assertEqual(self.store.storage()['usedBytes'], used)
        self.assertEqual(self.confirm(prepared, command), confirmed)
        self.error('deletion_changed', lambda: self.confirm(prepared))
        self.error('deletion_started', lambda: self.prepare(source, generation=1))
        self.error('source_changed', lambda: self.f.officer.validate_dependencies(self.f.selection, old))
        self.store._cleanup(source)
        self.assertTrue((self.store.files.root / 'objects' / source).exists(), 'generic cleanup must keep admitted guard')
        self.l.execute(confirmed['id']); done = self.status(confirmed)
        self.assertEqual((done['state'], done['releasedBytes']), ('deleted', len(PDF)))
        self.assertEqual(done['removedAt'], self.f.clock[0])
        self.assertFalse((self.store.files.root / 'objects' / source).exists())
        self.assertEqual(self.store.storage()['usedBytes'], used - len(PDF))
        self.l.execute(done['id']); self.assertEqual(self.status(done), done)
        self.assertEqual(self.confirm(prepared, command), done)
        after = dict(self.store.db.execute('SELECT * FROM files WHERE id=?', (source,)).fetchone())
        for field in ('identity', 'digest', 'generation', 'member', 'state', 'needs_cleanup'):
            self.assertEqual(after[field], original[field])
        self.assertEqual(self.f.officer.conversation_files(self.f.selection)['associationCount'], 1)

    def test_prepare_and_confirm_rollback_restore_slot_and_membership(self):
        source = self.add()
        def fail_prepare(label):
            if label == 'officer_deletion_prepare_before_commit': raise RuntimeError('fictional interruption')
        with mock.patch.object(self.store, 'checkpoint', side_effect=fail_prepare), self.assertRaises(RuntimeError): self.prepare(source)
        self.assertEqual(self.page()['files'][0]['managementGeneration'], 0)
        prepared = self.prepare(source)
        def fail_confirm(label):
            if label == 'officer_deletion_confirm_before_commit': raise RuntimeError('fictional interruption')
        with mock.patch.object(self.store, 'checkpoint', side_effect=fail_confirm), self.assertRaises(RuntimeError): self.confirm(prepared)
        self.assertTrue(self.page()['files'][0]['available']); self.assertEqual(self.status(prepared)['state'], 'confirm')
        self.assertEqual(self.store.db.execute('SELECT deletion_generation FROM files').fetchone()[0], 0)

    def test_competing_lost_ack_confirmations_both_arrival_orders_have_one_receipt_and_removal(self):
        for winner in (0, 1):
            with self.subTest(winner=winner):
                p = self.prepare(); keys = [self.f.key(), self.f.key()]
                # Both owners observed this same exact prepared GET. The first
                # committed response is deliberately discarded (lost ack).
                self.assertEqual(self.status(p)['state'], 'confirm')
                self.confirm(p, keys[winner])
                discovered = next(d for d in self.page()['deletions'] if d['id'] == p['id'])
                self.assertEqual(discovered['fingerprint'], p['fingerprint'])
                self.error('deletion_changed', lambda: self.confirm(p, keys[1 - winner]))
                with mock.patch.object(self.store.files, 'remove', wraps=self.store.files.remove) as remove:
                    self.l.execute(p['id'])
                    self.assertEqual(self.confirm(p, keys[winner])['state'], 'deleted')
                    self.l.execute(p['id'])
                    self.assertEqual(remove.call_count, 1)
                self.assertEqual(self.status(p)['releasedBytes'], len(PDF))

    def test_simultaneous_same_and_different_confirm_keys_commit_once(self):
        for same_key in (False, True):
            p = self.prepare(); first = self.f.key(); keys = [first, first if same_key else self.f.key()]
            barrier = threading.Barrier(2); outcomes = []
            def confirm(command):
                barrier.wait()
                try: outcomes.append(self.confirm(p, command)['state'])
                except AttachmentError as error: outcomes.append(error.code)
            with mock.patch.object(self.store.files, 'wake_cleanup', wraps=self.store.files.wake_cleanup) as wake:
                threads = [threading.Thread(target=confirm, args=(k,)) for k in keys]
                for thread in threads: thread.start()
                for thread in threads: thread.join(2); self.assertFalse(thread.is_alive())
                self.assertEqual(outcomes.count('deleting'), 2 if same_key else 1)
                self.assertEqual(outcomes.count('deletion_changed'), 0 if same_key else 1)
                self.assertEqual(wake.call_count, 1)
            row = self.store.db.execute('SELECT access_generation,deletion_generation FROM files WHERE id=?', (p['fileId'],)).fetchone()
            self.assertEqual(tuple(row), (2, 1))

    def test_valid_checksum_cannot_bless_false_committed_receipt(self):
        p = self.prepare()
        slot = self.store.db.execute('SELECT preview FROM deletion_source_slots WHERE slot_id=?', (p['id'],)).fetchone()
        forged = SOURCE_PREVIEW.decode(slot['preview'])
        forged.update(state=3, released=1, released_bytes=forged['bytes'], handles_closed=1)
        self.store.db.execute('UPDATE deletion_source_slots SET receipt=? WHERE slot_id=?', (SOURCE_RECEIPT.encode(forged), p['id']))
        self.error('cleanup_storage_unavailable', lambda: self.status(p))
        self.assertTrue(self.store.storage()['usedBytes'])

    def test_foreign_context_native_source_and_invalid_fields_refuse(self):
        source = self.add(); cid = self.store.create_conversation('fictional-other', self.f.key())['id']
        self.error('forbidden', lambda: self.l.prepare(source, 'officer', 'fictional-other', cid, 0, self.f.key()))
        self.error('forbidden', lambda: self.l.prepare(source, 'native', self.f.binding['scopeId'], self.f.cid, 0, self.f.key()))
        self.error('invalid_request', lambda: self.prepare(source, generation=True))
        self.error('invalid_request', lambda: self.prepare('../fictional-source'))
        prepared = self.prepare(source)
        self.error('forbidden', lambda: self.l.status(prepared['id'], '0' * 64))
        self.error('deletion_changed', lambda: self.l.confirm(prepared['id'], prepared['capability'], '0' * 64, self.f.key()))
        self.store.db.execute("UPDATE files SET preparation_kind='original_relay' WHERE id=?", (source,))
        self.error('deletion_source_unavailable', lambda: self.confirm(prepared))

    def test_pagination_51_preserves_rows_and_deletes_both_sides(self):
        sources = {self.add(PDF + str(i).encode()) for i in range(51)}
        first = self.page(); second = self.page(first['nextCursor'])
        self.assertEqual((len(first['files']), len(second['files'])), (50, 1))
        self.assertEqual({f['id'] for f in first['files'] + second['files']}, sources)
        for source in (first['files'][0]['id'], second['files'][0]['id']):
            p = self.prepare(source); self.confirm(p); self.l.execute(p['id'])
        self.assertEqual(len(self.page()['files']), 50)
        self.assertEqual(len(self.page(first['nextCursor'])['files']), 1)
        cid = self.store.create_conversation('fictional-other', self.f.key())['id']
        self.f.binding.update(scopeId='fictional-other', conversationId=cid)
        self.add()
        self.error('invalid_cursor', lambda: self.l.management('officer', 'fictional-other', cid, first['nextCursor']))

    def test_five_file_admission_deletion_keeps_siblings_and_stales_old_message_snapshot(self):
        draft = self.f.draft()
        sources = [self.f.upload(draft, PDF + str(i).encode())[0]['fileId'] for i in range(5)]
        self.f.admit(draft)
        first, second = self.prepare(sources[0]), self.prepare(sources[1])
        used = self.store.storage()['usedBytes']
        self.confirm(second)
        self.assertFalse(self.status(first)['canConfirm'], 'shared publication generation changed')
        self.error('deletion_changed', lambda: self.confirm(first))
        refreshed = self.prepare(sources[0], generation=1); self.confirm(refreshed)
        self.l.execute(second['id']); self.l.execute(refreshed['id'])
        page = self.page()
        self.assertEqual(len(page['files']), 5)
        self.assertEqual(sum(f['available'] for f in page['files']), 3)
        self.assertEqual(self.store.storage()['usedBytes'], used - 2 * (len(PDF) + 1))

    def test_owner_epoch_change_revokes_old_capability_but_retains_committed_cleanup(self):
        p = self.prepare(); command = self.f.key(); self.confirm(p, command)
        self.store.close(); self.store = AttachmentStore(self.f.root, enabled=True, owner_epoch='d' * 64, clock=lambda: self.f.clock[0])
        self.f.store = self.store
        self.l = OfficerDeletion(self.store)
        self.error('forbidden', lambda: self.status(p))
        current = self.page()['deletions'][0]
        self.assertEqual(current['fingerprint'], p['fingerprint']); self.assertNotEqual(current['capability'], p['capability'])
        self.assertEqual(self.confirm(current, command)['state'], 'deleting')
        self.l.execute(p['id']); self.assertEqual(self.status(current)['state'], 'deleted')

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_changed_identity_and_symlink_left_untouched_and_charged(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                source = self.add(); p = self.prepare(source); self.confirm(p)
                path = self.store.files.root / 'objects' / source
                path.unlink()
                target = self.f.root / ('fictional-target-' + source); target.write_bytes(b'owner replacement')
                if symlink: path.symlink_to(target)
                else: path.write_bytes(b'replacement'); path.chmod(0o600)
                used = self.store.storage()['usedBytes']; self.l.execute(p['id'])
                self.assertEqual(self.status(p)['state'], 'partial_failure'); self.assertEqual(self.status(p)['reason'], 'identity_changed')
                self.assertEqual(self.store.storage()['usedBytes'], used); self.assertTrue(path.exists())

    @unittest.skipUnless(POSIX, POSIX_WHY)        # fails POSIX's folder fsync; Windows flushes through its handle
    def test_durable_absence_fsync_failure_no_refund_then_restart_recovery(self):
        source = self.add(); p = self.prepare(source); self.confirm(p); used = self.store.storage()['usedBytes']
        actual = os.fsync
        def fail_directory(fd):
            if fd == self.store.files.objects_fd: raise OSError('fictional directory sync failure')
            return actual(fd)
        with mock.patch('attachments_files.os.fsync', side_effect=fail_directory): self.l.execute(p['id'])
        self.assertFalse((self.store.files.root / 'objects' / source).exists())
        self.assertEqual(self.status(p)['state'], 'unknown'); self.assertEqual(self.store.storage()['usedBytes'], used)
        with mock.patch('attachments_files.os.fsync', side_effect=fail_directory): self.l.execute(p['id'], recover=True)
        self.assertEqual(self.status(p)['state'], 'unknown'); self.assertEqual(self.store.storage()['usedBytes'], used)
        self.store.close(); self.store = self.f.open(); self.f.store = self.store; self.l = OfficerDeletion(self.store)
        recovered = self.page()['deletions'][0]
        self.l.execute(recovered['id'], recover=True)
        self.assertEqual(self.status(recovered)['state'], 'deleted'); self.assertEqual(self.store.storage()['usedBytes'], used - len(PDF))
        self.l.execute(recovered['id'], recover=True); self.assertEqual(self.store.storage()['usedBytes'], used - len(PDF))

    def test_accounting_failure_after_unlink_keeps_attempt_marker_until_recovery(self):
        p = self.prepare(); self.confirm(p); used = self.store.storage()['usedBytes']
        def fail_result(label):
            if label == 'officer_deletion_result_before_commit': raise RuntimeError('fictional commit failure')
        with mock.patch.object(self.store, 'checkpoint', side_effect=fail_result), self.assertRaises(RuntimeError): self.l.execute(p['id'])
        self.assertEqual(self.status(p)['state'], 'unknown'); self.assertEqual(self.store.storage()['usedBytes'], used)
        self.l.execute(p['id']); self.assertEqual(self.store.storage()['usedBytes'], used)
        self.l.execute(p['id'], recover=True); self.assertEqual(self.status(p)['state'], 'deleted')

    def test_body_pin_blocks_physical_cleanup_worker_resumes_on_verified_release(self):
        source = self.add(); lease = self.store.files.buffer_lease((source,)); p = self.prepare(source); self.confirm(p)
        self.l.execute(p['id']); self.assertEqual(self.status(p)['reason'], 'source_in_use')
        worker = DeletionWorker(self.l); self.workers.append(worker)
        used = self.store.storage()['usedBytes']; self.assertEqual(self.status(p)['state'], 'deleting')
        lease.close()
        with self.store.files._io_condition:
            # Event-based observation of removal completion; no production polling.
            self.store.files._io_condition.wait_for(lambda: not (self.store.files.root / 'objects' / source).exists(), timeout=30)   # generous: slow Windows runners
        worker.close(30)
        self.assertEqual(self.status(p)['state'], 'deleted'); self.assertEqual(self.store.storage()['usedBytes'], used - len(PDF))

    def test_startup_recovery_visits_unknown_before_nonboundary_cursor(self):
        for _ in range(100): self.add()
        slots = self.store.db.execute('SELECT slot_id,source_id FROM deletion_source_slots ORDER BY slot_id').fetchall()
        source = slots[60]['source_id']; p = self.prepare(source); self.confirm(p)
        with mock.patch.object(self.store.files, 'remove', side_effect=OSError('fictional interrupted attempt')):
            self.l.execute(p['id'])
        self.assertEqual(self.status(p)['state'], 'unknown')
        self.store.db.execute("UPDATE meta SET value=? WHERE key='deletion_cursor'", (slots[74]['slot_id'],))
        # Simulate a process restart without any unrelated registry event. An
        # UNKNOWN operation is eligible only during the complete initial sweep.
        visited = threading.Event(); actual = self.store.files.remove
        def remove(*args, **kwargs):
            result = actual(*args, **kwargs)
            if args[0] == source: visited.set()
            return result
        with mock.patch.object(self.store.files, 'remove', side_effect=remove):
            worker = DeletionWorker(self.l); self.workers.append(worker)
            self.assertTrue(visited.wait(30), 'startup recovery skipped slot61 before cursor75')   # returns once visited
            worker.close(30)
        self.assertEqual(self.status(p)['state'], 'deleted')

    def test_deletion_uses_reserved_fence_space_at_actual_sqlite_full(self):
        source = self.add()
        db = self.store.db
        reserve_query = 'SELECT cleanup_reserve FROM files WHERE id=?'
        original_reserve = db.execute(reserve_query, (source,)).fetchone()[0]
        self.assertGreater(len(original_reserve), 128)
        maximum = db.execute('PRAGMA page_count').fetchone()[0]
        self.assertEqual(db.execute('PRAGMA max_page_count=%d' % maximum).fetchone()[0], maximum)

        # Demand more overflow pages than the free list can supply. Probe the
        # source row itself so the assertion holds regardless of its leaf layout.
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
        oversized_reserve = 'x' * (page_size * (free_pages + 2))
        with self.assertRaises(sqlite3.DatabaseError) as full:
            db.execute('UPDATE files SET cleanup_reserve=? WHERE id=?', (oversized_reserve, source))
        if hasattr(full.exception, 'sqlite_errorcode'):
            self.assertEqual(full.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        else:  # Python 3.9 does not expose SQLite's result code.
            self.assertEqual(str(full.exception), 'database or disk is full')
        self.assertEqual(db.execute(reserve_query, (source,)).fetchone()[0], original_reserve)

        used = self.store.storage()['usedBytes']
        prepare_key = self.f.key()
        prepared = self.prepare(source, command=prepare_key)
        self.assertEqual(prepared, self.prepare(source, command=prepare_key))
        confirm_key = self.f.key()
        confirmed = self.confirm(prepared, confirm_key)
        self.assertEqual(confirmed['state'], 'deleting')
        self.assertTrue(confirmed['sourceUnavailable'])
        self.assertEqual(db.execute(reserve_query, (source,)).fetchone()[0], original_reserve[:128])
        self.assertEqual(self.store.storage()['usedBytes'], used)

        self.l.execute(prepared['id'])
        deleted = self.status(prepared)
        self.assertEqual((deleted['state'], deleted['releasedBytes']), ('deleted', len(PDF)))
        self.assertEqual(self.confirm(prepared, confirm_key), deleted)
        self.assertFalse((self.store.files.root / 'objects' / source).exists())
        self.assertEqual(self.store.storage()['usedBytes'], used - len(PDF))
        self.assertEqual(db.execute('PRAGMA page_count').fetchone()[0], maximum)
        self.assertEqual(db.execute('PRAGMA max_page_count').fetchone()[0], maximum)

    def test_uncertain_descriptor_prevents_absent_success_and_quota_release(self):
        source = self.add(); p = self.prepare(source); self.confirm(p)
        token = object()
        with self.store.files._io_lock: self.store.files._io[token] = [source, 'read', 'uncertain']
        used = self.store.storage()['usedBytes']
        try:
            self.l.execute(p['id']); self.assertEqual(self.status(p)['state'], 'unknown')
            self.assertEqual(self.store.storage()['usedBytes'], used)
        finally:
            with self.store.files._io_lock: del self.store.files._io[token]

    def test_base_service_policy_independent_management_and_store_close_joins_worker(self):
        source = self.add(); service = AttachmentService(self.store, resolver=UnqualifiedResolver())
        worker = self.store._deletion_worker
        page = service.management(self.f.cid, 'officer', self.f.binding['scopeId'])
        self.assertEqual(page['files'][0]['id'], source)
        self.store.close(); self.assertFalse(worker.thread.is_alive())
        service.close()

    def test_refused_direct_close_preserves_worker_until_real_pin_release(self):
        source = self.add(); service = AttachmentService(self.store, resolver=UnqualifiedResolver())
        worker = self.store._deletion_worker; lease = self.store.files.buffer_lease((source,))
        try:
            self.error('upload_busy', self.store.close)
            self.assertIs(self.store._deletion_worker, worker); self.assertTrue(worker.thread.is_alive())
        finally: lease.close()
        service.close(); self.assertFalse(worker.thread.is_alive())

    def test_http_exact_management_prepare_confirm_status_and_closed_auth_boundary(self):
        source = self.add(); service = AttachmentService(self.store, resolver=UnqualifiedResolver())
        def call(method, path, data=None, headers=(), defaults=True):
            body = json.dumps(data).encode() if data is not None else b''
            extra = list(headers)
            if data is not None: extra += [('Content-Length', str(len(body))), ('Content-Type', 'application/json')]
            handler = Handler(method, '/api/attachments' + path, body, extra, defaults)
            handle_http(handler, service, KEY, 8786)
            return handler.response
        route = '/conversations/' + self.f.cid + '/management?destinationId=officer&scopeId=' + self.f.binding['scopeId']
        self.assertEqual(call('GET', route)[0], 200); self.assertEqual(call('GET', route, defaults=False)[0], 401)
        self.assertEqual(call('GET', route)[2]['Connection'], 'close')
        self.assertEqual(call('GET', route, defaults=False)[2]['Connection'], 'close')
        body = dict(destinationId='officer', scopeId=self.f.binding['scopeId'], conversationId=self.f.cid, managementGeneration=0)
        self.assertEqual(call('POST', '/files/' + source + '/deletion-intent', dict(body, path='/tmp/untrusted'), [('Idempotency-Key', self.f.key())])[0], 400)
        status, p, _ = call('POST', '/files/' + source + '/deletion-intent', body, [('Idempotency-Key', self.f.key())])
        self.assertEqual(status, 201); self.assertTrue(p['canConfirm']); self.assertFalse(p['fixture'])
        cap = [('X-Attachment-Deletion-Capability', p['capability'])]
        self.assertEqual(call('GET', '/deletions/' + p['id'], headers=cap)[0], 200)
        self.assertEqual(call('GET', '/deletions/' + p['id'], headers=cap + cap)[0], 400)
        self.assertEqual(call('POST', '/deletions/' + p['id'] + '/confirm', {'fingerprint': p['fingerprint']}, cap + [('Idempotency-Key', self.f.key())])[0], 200)


@unittest.skipUnless(STORE, STORE_WHY)  # borrows the ledger's storage setUp
class AnswerDeletionTests(unittest.TestCase):
    def setUp(self): self.f = ledger_fixtures.ExecutionTests(); self.f.setUp(); self.l = OfficerDeletion(self.f.store)
    def tearDown(self): self.f.tearDown()
    def prepare_delete(self, source):
        s = self.f.selection
        p = self.l.prepare(source, 'officer', s.scope_id, s.conversation_id, 0, self.f.f.key())
        return self.l.confirm(p['id'], p['capability'], p['fingerprint'], self.f.f.key())

    def test_deletion_before_consume_rejects_permit_and_wipes_accepted_transitive_answers(self):
        source = self.f.add(); first, _, _ = self.f.complete()
        transitive = self.f.l.prepare(self.f.selection, self.f.p, self.f.scope.id, self.f.f.key(),
                                      'Fictional governed follow-up', prior_answer_ids=(first['id'],))
        self.f.complete(transitive)
        child = self.f.prepare('Fictional child')
        claimed, permit, body = self.f.claim(child)
        self.prepare_delete(source)
        self.assertEqual(self.f.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?', (first['id'],)).fetchone()[0], bytes(65536))
        self.assertEqual(self.f.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?', (transitive['id'],)).fetchone()[0], bytes(65536))
        with self.assertRaises(AttachmentError): self.f.l.consume(permit, self.f.selection, child['id'], body.body_digest, claimed['profileDigest'])
        with self.assertRaises(AttachmentError): self.f.l.replay(self.f.selection, first['id'])

    def test_late_answer_after_confirm_cannot_publish_and_billing_is_preserved(self):
        source = self.f.add(); request = self.f.prepare(); candidate, facts = self.f.accept(request)
        before = self.f.receipt(request)
        p = self.prepare_delete(source)
        after = self.f.receipt(request)
        for k in ('state', 'reserved', 'charged', 'settled', 'usage'): self.assertEqual(before[k], after[k])
        self.assertEqual(after['availability'], 'revoked')
        self.f.error('document_answer_revoked', lambda: self.f.l.commit_answer(self.f.selection, request['id'], candidate))
        self.l.execute(p['id']); self.assertEqual(self.l.status(p['id'], p['capability'])['state'], 'deleted')

    def test_actual_sqlite_full_before_prepare_still_prepares_revokes_removes_and_replays(self):
        source = self.f.add(); request, _, _ = self.f.complete()
        for i in range(63): self.f.prepare('Fictional dependency ' + str(i))
        self.assertEqual(self.f.store.db.execute('SELECT COUNT(*) FROM officer_document_dependencies WHERE source_id=?', (source,)).fetchone()[0], 64)
        store = self.f.store
        store.db.execute('CREATE TABLE fictional_deletion_filler(id INTEGER PRIMARY KEY,payload BLOB)')
        page_count = store.db.execute('PRAGMA page_count').fetchone()[0]
        maximum = page_count + 20
        store.db.execute('PRAGMA max_page_count=%d' % maximum)
        full = False
        for _ in range(1000):
            try:
                with store._transaction(reclaim=True): store.db.execute('INSERT INTO fictional_deletion_filler(payload) VALUES(zeroblob(3000))')
            except sqlite3.DatabaseError as error:
                self.assertEqual(getattr(error, 'sqlite_errorcode', 13), 13); full = True; break
        self.assertTrue(full); self.assertEqual(store.db.execute('PRAGMA page_count').fetchone()[0], maximum)
        counts = {r[0]: store.db.execute('SELECT COUNT(*) FROM ' + r[0]).fetchone()[0]
                  for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        s = self.f.selection; command = self.f.f.key()
        p = self.l.prepare(source, 'officer', s.scope_id, s.conversation_id, 0, command)
        self.assertEqual(self.l.prepare(source, 'officer', s.scope_id, s.conversation_id, 0, command), p)
        confirm_key = self.f.f.key(); self.l.confirm(p['id'], p['capability'], p['fingerprint'], confirm_key)
        self.l.execute(p['id']); self.assertEqual(self.l.status(p['id'], p['capability'])['state'], 'deleted')
        self.assertEqual(self.l.confirm(p['id'], p['capability'], p['fingerprint'], confirm_key)['state'], 'deleted')
        self.assertEqual(store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?', (request['id'],)).fetchone()[0], bytes(65536))
        self.assertEqual(counts, {name: store.db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] for name in counts})
        self.assertEqual(store.db.execute('PRAGMA max_page_count').fetchone()[0], maximum)


@unittest.skipUnless(STORE, STORE_WHY)  # borrows the intents' storage setUp
class PendingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = intent_fixtures.IntentTests(); self.f.setUp()
        self.store = self.f.store; self.l = OfficerDeletion(self.store)
    def tearDown(self): self.f.tearDown()
    def page(self):
        selection = self.f.f.selection
        return self.l.management('officer', selection.scope_id, selection.conversation_id)
    def running(self):
        draft, _, public = self.f.prepared(); record, winner = self.f.consume(draft, public)
        message = self.f.admit(draft, record)
        return record, winner, message
    def freeze(self, record, message):
        return self.f.f.l.prepare(self.f.f.selection, self.f.f.p, self.f.f.scope.id,
            self.f.i.command(record, 'execution'), record['question'], local_message_id=message['id'])

    def test_positive_warning_requires_consumed_question_and_exact_frozen_source(self):
        record, _, message = self.running(); source = message['files'][0]['id']
        self.assertFalse(self.page()['files'][0]['hasUnresolvedQuestion'], 'consumption without a source manifest proves no source warning')
        execution = self.freeze(record, message)
        self.assertTrue(self.page()['files'][0]['hasUnresolvedQuestion'])
        self.f.f.l.claim(self.f.f.selection, execution['id'])
        unrelated = self.f.f.add()
        # Read-only warning evidence survives unavailable qualification; no
        # provider/policy operation participates in this metadata projection.
        with mock.patch('officer_document_policy.current', side_effect=AssertionError('policy needed for management')):
            files = {f['id']: f for f in self.page()['files']}
        self.assertTrue(files[source]['hasUnresolvedQuestion']); self.assertFalse(files[unrelated]['hasUnresolvedQuestion'])
        selection = self.f.f.selection
        p = self.l.prepare(source, 'officer', selection.scope_id, selection.conversation_id, 0, self.f.f.f.key())
        self.assertTrue(p['hasUnresolvedQuestion']); self.assertIsNone(p['removedAt'])

    def test_unconsumed_and_terminal_questions_do_not_assert_pending_warning(self):
        source = self.f.f.add()
        public = self.f.i.prepare_question(self.f.f.f.binding, 'Fictional pending question', [], self.f.f.f.direct_profile(), self.f.f.f.key())
        self.assertFalse(self.page()['files'][0]['hasUnresolvedQuestion'])
        record, winner = self.f.i.consume(public['requestId'], public['requestCapability'], fingerprint=public['fingerprint'])
        execution = self.f.f.l.prepare(self.f.f.selection, self.f.f.p, self.f.f.scope.id,
            self.f.i.command(record, 'execution'), record['question'])
        self.assertTrue(self.page()['files'][0]['hasUnresolvedQuestion'])
        self.f.f.complete(execution)
        self.f.i.finish(record['id'], winner)
        self.assertFalse(self.page()['files'][0]['hasUnresolvedQuestion'])

    def test_unknown_consumed_question_retains_source_bound_warning_after_revocation(self):
        record, winner, message = self.running(); execution = self.freeze(record, message)
        state, _, body = self.f.f.l.claim(self.f.f.selection, execution['id'])
        facts = ledger_fixtures.ledger.ResultFacts(execution['id'], state['claimNonce'], body.body_digest,
            state['profileDigest'], 'unknown', '', '', self.f.f.p.model_id, self.f.f.pricing.billing_revision, None, 'transport_unknown')
        self.f.f.l.record_result(self.f.f.selection, facts); self.f.i.finish(record['id'], winner, 'result_unavailable')
        source = message['files'][0]['id']; selection = self.f.f.selection
        p = self.l.prepare(source, 'officer', selection.scope_id, selection.conversation_id, 0, self.f.f.f.key())
        self.assertTrue(p['hasUnresolvedQuestion'])
        self.l.confirm(p['id'], p['capability'], p['fingerprint'], self.f.f.f.key())
        self.l.execute(p['id'])
        done = self.l.status(p['id'], p['capability'])
        self.assertEqual(done['state'], 'deleted'); self.assertTrue(done['hasUnresolvedQuestion'])
        self.assertIsNotNone(done['removedAt'])


class StartupDeletionTests(unittest.TestCase):
    def test_master_gate_and_demo_construct_no_cleanup_worker(self):
        import server
        for enabled, demo in ((False, '0'), (False, '1'), (True, '1')):
            with mock.patch.object(server, 'ATTACHMENTS_ENABLED', enabled), \
                 mock.patch.dict(os.environ, {'WATCH_DEMO': demo}), \
                 mock.patch('officer_document_deletion.DeletionWorker', side_effect=AssertionError('cleanup constructed')):
                self.assertIsNone(server.start_attachments())

    @unittest.skipUnless(STORE, STORE_WHY)
    def test_policy_fallback_and_missing_credential_keep_exact_management_available(self):
        import server
        import officer_document_policy as policy
        for mode in ('reading_off', 'missing_allowance', 'expired_allowance', 'missing_credential'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                home = Path(os.path.realpath(temporary)); state = private_folder(home / '.live-room')
                seed = AttachmentStore(state, enabled=True, owner_epoch=EPOCH)
                cid = seed.create_conversation('room', 'fictional-seed-conversation')['id']
                binding = dict(kind='officer', id='officer', scopeId='room', conversationId=cid,
                               modelId='claude-haiku-5-5', capabilityRevision='fictional-v1')
                profile = OfficerDirectRetentionProfile('fictional-direct', 'v1', 'local_retention', 'fictional-v1', binding['modelId'])
                draft = seed.create_draft(binding, 'fictional-seed-draft')
                upload = seed.reserve_upload(draft['id'], draft['capability'], fence(draft), dict(metadata(PDF), mediaType='application/pdf'),
                                             'fictional-seed-upload', preparation_kind='officer_direct', officer_profile=profile)
                draft['revision'] = upload['draftRevision']
                uploaded = seed.receive_upload(draft['id'], draft['capability'], fence(draft), upload['id'], upload['fingerprint'], io.BytesIO(PDF))
                draft['revision'] = uploaded['draftRevision']
                OfficerConversations(seed).admit(draft['id'], draft['capability'], fence(draft), 'fictional-seed-admission', binding, profile)
                seed.close()
                allowance = policy.HostAllowance(policy.EVIDENCE_ID, 'fictional-delete-budget', 'v1', '5.00')
                def make_store(*args, **kwargs):
                    kwargs['clock'] = lambda: policy.VALID_UNTIL + 1 if mode == 'expired_allowance' else policy.VALID_FROM + 1
                    return AttachmentStore(*args, **kwargs)
                def load(*args, **kwargs):
                    if mode == 'missing_allowance': policy.unavailable()
                    return allowance
                with mock.patch.object(server, 'ATTACHMENTS_ENABLED', True), \
                     mock.patch.object(server, 'OFFICER_DOCUMENTS_ENABLED', mode != 'reading_off'), \
                     mock.patch.object(server, 'OFFICER_MODEL', 'claude-haiku-5-5'), \
                     mock.patch.object(server, 'OFFICER_MODEL_ERROR', ''), \
                     mock.patch.object(server, 'OFFICER_KEY', ''), \
                     mock.patch.object(server, 'HOME', str(home)), mock.patch.object(server, 'KEY', KEY), \
                     mock.patch.dict(os.environ, {'WATCH_DEMO': '0'}), \
                     mock.patch('attachments_store.AttachmentStore', side_effect=make_store), \
                     mock.patch('officer_document_policy.load_allowance', side_effect=load), \
                     mock.patch('officer_document_transport.DirectDocumentTransport') as transport:
                    service = server.start_attachments()
                    self.assertIsNotNone(service)
                    try:
                        page = service.management(cid, 'officer', 'room')
                        self.assertEqual(page['files'][0]['id'], upload['fileId'])
                        p = service.deletion_prepare(upload['fileId'], dict(destinationId='officer', scopeId='room', conversationId=cid, managementGeneration=0), 'fictional-start-delete')
                        operation = service.deletion_confirm(p['id'], p['capability'], {'fingerprint': p['fingerprint']}, 'fictional-start-confirm')
                        self.assertTrue(operation['sourceUnavailable'])
                        self.assertFalse(service.capabilities('officer', 'room', 'claude-haiku-5-5')['available'])
                        transport.return_value.call.assert_not_called()
                    finally:
                        worker = service.store._deletion_worker
                        service.close(); self.assertFalse(worker.thread.is_alive())


if __name__ == '__main__': unittest.main()
