"""D1 proofs over disposable SQLite files, actual worktrees, and process leases."""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import io
import multiprocessing
import os
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from attachments_native_fixture import EPOCH, FaultConnection, NativeFixture, fence, metadata
from attachments_catalogue import Catalogue, HistorySelection
from attachments_deletion_slots import COPY_PREVIEW, COPY_RECEIPT, SOURCE_RECEIPT, ZERO_ID
from attachments_materialize import Materializer, RetainedCopyCleanup, WorkspaceDirectories
from attachments_native_deletion import DisposableNativeDeletion, NativeDeletion
from attachments_native_handoff import FictionalAdapterBinding, OfflineNativeHandoff
from attachments_native_mirror import NativeOutcomeMirror
from attachments_native_receipts import NativeReadReceipts
from attachments_requests import RequestLedger
from attachments_store import AttachmentStore
from attachments_types import AttachmentError, canonical

ORIGINAL = b'%PDF-1.7\nFictional orchard document\n'


def exit_after_original(binding, context, operation, revision):
    def checkpoint(event):
        if event == 'native_deletion_physical_returned':
            os._exit(27)

    f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: context, checkpoint=checkpoint)
    f.deletion.execute(operation, revision)


def exit_after_source_close_error(binding, context, holder, target, confirmed, preview, neighbor):
    f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: context)
    rq, store = f._requests, f._store
    real_close = os.close
    try:
        with f._locks.hold(rq.resource_keys(holder['id'])) as lease:
            authority = rq.authority(holder['id'])
            f._writer.prepare_directories(context, authority, lease,
                directories=WorkspaceDirectories.from_record(rq.workspace_directories(holder['id'])))
            copy = rq.claim_copy(holder['id'], authority.operation_revision, holder['copies'][0]['id'], lease)
            opening = rq.source_handle(copy.id, lease)
            fd = opening.__enter__()

            def fail_only_target(closing):
                if closing == fd:
                    raise OSError('Fictional original descriptor close failure.')
                return real_close(closing)

            with patch.object(os, 'close', side_effect=fail_only_target):
                try:
                    opening.__exit__(None, None, None)
                except OSError:
                    pass
                else:
                    raise AssertionError('Original close fault was not reached.')
        os.fstat(fd)  # The original descriptor, and not just a mock, remains open.
        assert store.request_handles
        try:
            f.deletion.confirm(preview['id'], preview['capability'], preview['fingerprint'], 'fictional-blocked-confirm')
        except AttachmentError as exc:
            assert exc.code == 'deletion_busy'
        else:
            raise AssertionError('Confirmation granted while an original close is unproved.')
        result = f.deletion.execute(confirmed['id'], confirmed['revision'])
        assert [p['state'] for p in result['pieces']] == ['pending', 'removed']
        assert (store.files.root / 'objects' / target['copies'][0]['sourceId']).is_file()
        assert f.materialize(neighbor['id'], neighbor['copies'][0]['id']).bytes == len(ORIGINAL)
        try:
            f.close(timeout=0.1)
        except AttachmentError as exc:
            assert exc.code == 'upload_busy'
        else:
            raise AssertionError('Store close cleared an unproved original close.')
    except BaseException:
        raise
    os._exit(29)


def exit_after_workspace_close_error(binding, context, operation, revision, path):
    f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: context)
    st = os.stat(path)
    inode = st.st_dev, st.st_ino
    captured, real_close = [], os.close

    def close(fd):
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == inode and not captured:
            captured.append(fd)
            raise OSError('Fictional workspace descriptor close failure.')
        return real_close(fd)

    with patch.object(os, 'close', side_effect=close):
        result = f.deletion.execute(operation, revision)
    assert [p['state'] for p in result['pieces']] == ['removed', 'unknown']
    assert captured and os.fstat(captured[0]).st_ino == inode[1]
    assert not os.path.exists(path) and f._store.files.io_active()
    try:
        f.close(timeout=0.1)
    except AttachmentError as exc:
        assert exc.code == 'upload_busy'
    else:
        raise AssertionError('Workspace close uncertainty was lost.')
    try:
        DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: context)
    except AttachmentError:
        pass
    else:
        raise AssertionError('Workspace close uncertainty released the store lifetime.')
    os._exit(31)


class NativeDeletionTest(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture(self)
        self.scope = HistorySelection.from_binding(self.native.binding)
        self.profile = replace(self.native.profile, media_types=('application/pdf',),
                               original_relay_formats=('application/pdf',))
        connect = sqlite3.connect
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **k: connect(*a, factory=FaultConnection, **k)):
            self.f = DisposableNativeDeletion.create(self.native.root / 'retention', EPOCH,
                                                       resolve=lambda auth: self.native.context)
        self.addCleanup(lambda: self.f.close())
        self.bind()

    def bind(self):
        self.d = self.f.deletion
        self.db = self.f._store.db
        self.rq = self.f._requests

    def reopen(self, *, resolve=True):
        binding = self.f.binding
        self.f.close()
        self.f = DisposableNativeDeletion.reopen(binding, EPOCH,
                     resolve=(lambda auth: self.native.context) if resolve else None)
        self.bind()

    def draft(self, originals=(ORIGINAL,)):
        d = self.f.create_draft(self.native.binding, self.native.key())
        d['revision'] = self.f.edit_text(d['id'], d['capability'], fence(d), 'Read the orchard report.')['revision']
        for i, data in enumerate(originals):
            u = self.f.reserve_upload(d['id'], d['capability'], fence(d),
                metadata(data, 'application/pdf', f'orchard-{i}.pdf'), self.native.key())
            d['revision'] = u['draftRevision']
            d['revision'] = self.f.receive_upload(d['id'], d['capability'], fence(d), u['id'],
                                                   u['fingerprint'], io.BytesIO(data))['draftRevision']
        return d

    def request(self, *, draft=None, admitted=True, materialized=True):
        draft = self.draft() if draft is None else draft
        request = self.f.prepare_request(draft['id'], draft['capability'], fence(draft), self.native.key(),
                                           self.native.binding, self.profile)
        if admitted:
            self.f.authorize_send(request['id'], request['capability'], request['revision'], request['fingerprint'],
                                  binding=self.native.binding, profile=self.profile)
        if materialized:
            self.f.materialize(request['id'], request['copies'][0]['id'])
        return request

    def source(self, r):
        return r['copies'][0]['sourceId']

    def paths(self, r):
        relative = self.db.execute('SELECT relative_path FROM workspace_copies WHERE id=?',
                                   (r['copies'][0]['id'],)).fetchone()[0]
        return self.f._store.files.root / 'objects' / self.source(r), self.native.repo / relative

    def quota(self):
        return self.db.execute('SELECT bytes FROM quota WHERE id=1').fetchone()[0]

    def prepare(self, r, generation=0, key=None):
        return self.d.prepare(self.source(r), self.scope, generation, key or self.native.key())

    def confirm(self, preview, key=None):
        return self.d.confirm(preview['id'], preview['capability'], preview['fingerprint'], key or self.native.key())

    def finish(self, operation, **kw):
        return self.d.execute(operation['id'], operation['revision'], **kw)

    def test_confirm_fresh_exact_revoke_and_separate_execute(self):
        r = self.request()
        original, copy = self.paths(r)
        self.assertEqual((original.read_bytes(), copy.read_bytes()), (ORIGINAL, ORIGINAL))
        before = self.d.management(self.scope)['files'][0]
        self.assertTrue(before['available'] and before['canPrepare'])
        self.assertEqual(before['managementGeneration'], 0)
        historical = Catalogue(self.f._store).history(self.scope)
        self.assertTrue(historical['items'][0]['savedCopyAvailable'])
        preview = self.prepare(r)
        key = self.native.key()
        ack = self.confirm(preview, key)
        self.assertTrue(ack['confirmedNow'])
        operation = ack['operation']
        self.assertEqual(self.confirm(preview, key), {'operation': operation, 'confirmedNow': False})
        self.assertTrue(operation['sourceUnavailable'] and operation['canExecute'])
        self.assertEqual(operation['managementGeneration'], preview['managementGeneration'])
        self.assertGreater(operation['revision'], preview['revision'])
        with self.assertRaises(AttachmentError):
            self.rq.authority(r['id'])
        current = Catalogue(self.f._store).history(self.scope)
        self.assertEqual(current['associationCount'], historical['associationCount'])
        self.assertEqual(current['items'][0]['cataloguedAt'], historical['items'][0]['cataloguedAt'])
        self.assertFalse(current['items'][0]['savedCopyAvailable'])
        self.assertIsNone(current['items'][0]['sourceCapability'])
        self.assertTrue(original.exists() and copy.exists())
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        done = self.finish(operation)
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists() or copy.exists())
        self.assertEqual([p['chargedBytes'] for p in done['pieces']], [0, 0])
        self.assertEqual(done['releasedBytes'], 2 * len(ORIGINAL))
        self.assertEqual(self.quota(), 0)
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.assertEqual(self.finish(done), done)
        self.assertEqual(self.quota(), 0)

    def test_lost_prepare_lookup_supersession_and_scope(self):
        r = self.request()
        key = self.native.key()
        preview = self.prepare(r, key=key)
        hashed = hashlib.sha256(key.encode()).hexdigest()
        match = self.d.match_prepare(self.source(r), self.scope, hashed)
        self.assertEqual(match, {'match': {'operationId': preview['id'], 'managementGeneration': 1}})
        self.assertNotIn('capability', self.prepare(r, key=key))
        self.assertNotIn('capability', self.d.status(preview['id'], self.scope))
        recovered = self.d.recover_confirmation(preview['id'], self.scope, 1)
        self.assertEqual(recovered, preview)
        with self.assertRaises(AttachmentError):
            self.d.status(preview['id'], replace(self.scope, scope_id='fictional-other-scope'))
        new = self.prepare(r, 1)
        self.assertEqual(new['id'], preview['id'])
        self.assertEqual(new['managementGeneration'], 2)
        self.assertNotEqual(new['fingerprint'], preview['fingerprint'])
        self.assertEqual(self.d.match_prepare(self.source(r), self.scope, hashed), {'match': None})
        with self.assertRaises(AttachmentError):
            self.confirm(preview)
        with self.assertRaises(AttachmentError):
            self.d.recover_confirmation(preview['id'], self.scope, 1)
        self.assertTrue(self.confirm(new)['confirmedNow'])
        with self.assertRaises(AttachmentError):
            self.prepare(r, 2)

    def test_lost_confirm_ack_only_status_distinguishes_commit(self):
        r = self.request()
        p = self.prepare(r)
        self.db.commit_fault = 'before'
        with self.assertRaises(AttachmentError):
            self.confirm(p)
        self.assertTrue(self.d.status(p['id'], self.scope)['canConfirm'])
        self.assertTrue(all(path.exists() for path in self.paths(r)))
        key = self.native.key()
        self.db.commit_fault = 'after'
        with self.assertRaises(AttachmentError):
            self.confirm(p, key)
        read = self.d.status(p['id'], self.scope)
        self.assertEqual(read['managementGeneration'], p['managementGeneration'])
        self.assertTrue(read['sourceUnavailable'])
        self.assertNotIn('confirmedNow', read)
        self.assertTrue(all(path.exists() for path in self.paths(r)))
        self.assertEqual(self.confirm(p, key), {'operation': read, 'confirmedNow': False})
        self.assertTrue(all(path.exists() for path in self.paths(r)))

    def test_reserved_copy_positively_never_created(self):
        r = self.request(materialized=False)
        original, copy = self.paths(r)
        self.assertTrue(original.exists())
        self.assertFalse(copy.exists())
        operation = self.confirm(self.prepare(r))['operation']
        done = self.finish(operation)
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists())
        self.assertEqual(self.quota(), 0)

    def test_actual_sqlite_full_still_uses_preallocated_receipts(self):
        r = self.request()
        original, copy = self.paths(r)
        indexes = ('native_delete_copy_source', 'native_delete_copy_request')
        for index, column in zip(indexes, ('source_id', 'request_id')):
            result = self.db.execute(f'EXPLAIN QUERY PLAN SELECT id FROM workspace_copies INDEXED BY {index} WHERE {column}=? LIMIT ?',
                                     (self.source(r), 17)).fetchone()['detail']
            self.assertIn(index, result)
            self.assertIn('SEARCH', result)
        self.db.execute('CREATE TABLE fictional_filler (value BLOB)')
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        self.db.execute(f'PRAGMA max_page_count={pages + 6}')
        with self.assertRaises(sqlite3.OperationalError) as caught:
            for _ in range(pages + 7):
                self.db.execute('INSERT INTO fictional_filler VALUES (zeroblob(3900))')
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(self.db.execute('PRAGMA freelist_count').fetchone()[0], 0)
        schema = self.db.execute('PRAGMA schema_version').fetchone()[0]
        preview = self.prepare(r)
        done = self.finish(self.confirm(preview)['operation'])
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists() or copy.exists())
        self.assertEqual(self.db.execute('PRAGMA schema_version').fetchone()[0], schema)
        for table, width in (('deletion_source_slots', 2048), ('deletion_copy_slots', 512)):
            self.assertEqual(tuple(self.db.execute(f'SELECT length(preview), length(receipt) FROM {table}').fetchone()),
                             (width, width))

    def test_lost_attempt_and_accounting_preserve_charge_until_explicit_reopen(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        self.db.commit_fault = 'before'
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.assertEqual(self.d.status(operation['id'], self.scope), operation)
        self.assertTrue(original.exists() and copy.exists())
        self.db.commit_fault = 'after'
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        observed = self.d.status(operation['id'], self.scope)
        self.assertGreater(observed['revision'], operation['revision'])
        self.assertEqual(observed['pieces'][0]['state'], 'unknown')
        self.assertFalse(observed['canRecover'])
        self.assertTrue(original.exists() and copy.exists())
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        self.reopen()
        observed = self.d.status(operation['id'], self.scope)
        self.assertTrue(observed['canRecover'])
        done = self.finish(observed, recover=True)
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists() or copy.exists())

    def test_real_restart_after_unlink_reconciles_only_explicit_recovery(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        binding = self.f.binding
        self.f.close()
        child = multiprocessing.get_context('spawn').Process(
            target=exit_after_original, args=(binding, self.native.context, operation['id'], operation['revision']))
        child.start()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join(5)
            self.fail('The fictional unlink process did not exit.')
        self.assertEqual(child.exitcode, 27)
        self.assertFalse(original.exists())
        self.assertTrue(copy.exists())
        self.f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: self.native.context)
        self.bind()
        observed = self.d.status(operation['id'], self.scope)
        self.assertEqual(observed['releasedBytes'], 0)
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        self.assertTrue(observed['canRecover'])
        self.assertEqual(self.d.management(self.scope)['deletions'], [observed])
        self.assertTrue(copy.exists())
        done = self.finish(observed, recover=True)
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists() or copy.exists())
        self.assertEqual(self.quota(), 0)

    def test_failed_accounting_and_replaced_original_never_refunded(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        def lost_accounting(event):
            if event == 'native_deletion_physical_returned':
                self.db.commit_fault = 'before'
        self.f._store.checkpoint = lost_accounting
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.assertFalse(original.exists())
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        original.write_bytes(b'Fictional replacement')
        original.chmod(0o600)
        self.reopen()
        done = self.finish(self.d.status(operation['id'], self.scope), recover=True)
        self.assertEqual(done['state'], 'partial_failure')
        self.assertEqual([p['state'] for p in done['pieces']], ['changed', 'removed'])
        self.assertEqual(original.read_bytes(), b'Fictional replacement')
        self.assertFalse(copy.exists())
        self.assertEqual(self.quota(), len(ORIGINAL))

    def test_unresolved_copy_retains_charge_and_missing_resolver_does_not_block_original(self):
        for resolution in ('unresolved', 'unavailable'):
            with self.subTest(resolution=resolution):
                r = self.request(materialized=resolution != 'unresolved')
                original, copy = self.paths(r)
                if resolution == 'unresolved':
                    def crash_before_create(event):
                        if event == 'copy_created':
                            raise RuntimeError('Fictional interruption.')
                    self.f._writer.checkpoint = crash_before_create
                    with patch.object(self.f._writer, '_partial_evidence', side_effect=OSError):
                        with self.assertRaises(RuntimeError):
                            self.f.materialize(r['id'], r['copies'][0]['id'])
                    self.f._writer.checkpoint = lambda event: None
                    self.assertTrue(copy.exists())
                else:
                    self.reopen(resolve=False)
                confirmed = self.confirm(self.prepare(r))['operation']
                result = self.finish(confirmed)
                self.assertEqual([p['state'] for p in result['pieces']], ['removed', 'unknown'])
                self.assertEqual(result['pieces'][1]['reason'], 'workspace_unverified' if resolution == 'unresolved'
                                 else 'workspace_unavailable')
                self.assertEqual(result['pieces'][1]['chargedBytes'], len(ORIGINAL))
                self.assertFalse(original.exists())
                self.assertTrue(copy.exists())
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))

    def test_closed_partial_prefix_has_exact_retained_removal_authority(self):
        r = self.request(materialized=False)
        original, copy = self.paths(r)
        close_after_partial = self.f._writer.checkpoint
        def fault(event):
            if event == 'copy_created':
                raise RuntimeError('Fictional stop after file creation.')
            close_after_partial(event)
        self.f._writer.checkpoint = fault
        with self.assertRaises(RuntimeError):
            self.f.materialize(r['id'], r['copies'][0]['id'])
        self.assertEqual(copy.read_bytes(), b'')
        row = self.db.execute('SELECT state,partial_digest,partial_bytes FROM workspace_copies').fetchone()
        self.assertEqual(tuple(row), ('partial', hashlib.sha256(b'').hexdigest(), 0))
        self.reopen()
        done = self.finish(self.confirm(self.prepare(r))['operation'])
        self.assertEqual(done['state'], 'deleted')
        self.assertFalse(original.exists() or copy.exists())
        self.assertEqual(self.quota(), 0)

    def test_changed_workspace_hardlink_is_preserved_and_neighbor_unchanged(self):
        first, neighbor = self.request(), self.request()
        original, copy = self.paths(first)
        other = self.paths(neighbor)
        operation = self.confirm(self.prepare(first))['operation']
        hardlink = self.native.repo / 'fictional-hardlink.pdf'
        os.link(copy, hardlink)
        result = self.finish(operation)
        self.assertEqual(result['state'], 'partial_failure')
        self.assertEqual([p['state'] for p in result['pieces']], ['removed', 'changed'])
        self.assertFalse(original.exists())
        self.assertEqual((copy.read_bytes(), hardlink.read_bytes()), (ORIGINAL, ORIGINAL))
        self.assertTrue(all(path.read_bytes() == ORIGINAL for path in other))
        self.assertEqual(self.quota(), 3 * len(ORIGINAL))

    def test_failed_original_close_delays_original_deletion_and_preserves_other_workspace_work(self):
        holder = self.request(materialized=False)
        target, unconfirmed = self.request(), self.request()
        neighbor = self.request(materialized=False)
        confirmed = self.confirm(self.prepare(target))['operation']
        preview = self.prepare(unconfirmed)
        binding = self.f.binding
        self.f.close()
        child = multiprocessing.get_context('spawn').Process(target=exit_after_source_close_error,
            args=(binding, self.native.context, holder, target, confirmed, preview, neighbor))
        child.start()
        child.join(12)
        if child.is_alive():
            child.kill()
            child.join(5)
            self.fail('The original-close test process did not exit.')
        self.assertEqual(child.exitcode, 29)
        self.f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: self.native.context)
        self.bind()
        result = self.d.status(confirmed['id'], self.scope)
        self.assertEqual([p['state'] for p in result['pieces']], ['pending', 'removed'])
        self.assertEqual(result['releasedBytes'], len(ORIGINAL))
        self.assertEqual(self.finish(result)['state'], 'deleted')
        self.assertTrue(self.confirm(preview)['confirmedNow'])

    def test_extra_intent_generic_cleanup_and_native_confirm_first_commit_wins(self):
        for generic_first in (True, False):
            with self.subTest(generic_first=generic_first):
                draft = self.draft()
                extra = self.request(draft=draft, admitted=False, materialized=False)
                request = self.request(draft=draft)
                original, copy = self.paths(request)
                preview = self.prepare(request)
                if generic_first:
                    stopped, resume, errors = threading.Event(), threading.Event(), []
                    real_hold = self.f._locks.hold

                    @contextmanager
                    def hold(keys):
                        stopped.set()
                        if not resume.wait(5):
                            raise RuntimeError('Fictional test lease pause timed out.')
                        with real_hold(keys) as lease:
                            yield lease

                    def confirm():
                        try:
                            self.confirm(preview)
                        except Exception as exc:
                            errors.append(exc)

                    with patch.object(self.f._locks, 'hold', side_effect=hold):
                        worker = threading.Thread(target=confirm)
                        worker.start()
                        try:
                            self.assertTrue(stopped.wait(5))
                            result = self.rq.begin_saved_cleanup(extra['id'], extra['capability'], extra['revision'])
                            self.assertEqual(result['state'], 'cleanup')
                        finally:
                            resume.set()
                            worker.join(5)
                    self.assertEqual([e.code for e in errors], ['deletion_changed'])
                    self.assertTrue(original.exists() and copy.exists())
                else:
                    confirmed = self.confirm(preview)['operation']
                    with self.assertRaises(AttachmentError) as caught:
                        self.rq.begin_saved_cleanup(extra['id'], extra['capability'], extra['revision'])
                    self.assertEqual(caught.exception.code, 'cleanup_owned')
                    with self.f._locks.hold(self.rq.resource_keys(extra['id'])) as lease:
                        with self.assertRaises(AttachmentError):
                            self.rq.authorize_send(extra['id'], extra['capability'], extra['revision'], extra['fingerprint'],
                                resolved_binding=self.native.binding, transport_profile=self.profile, resource_lease=lease)
                    self.assertEqual(self.finish(confirmed)['state'], 'deleted')
                    self.assertFalse(original.exists() or copy.exists())
                    self.assertEqual(self.rq.request_status(extra['id'], extra['capability'])['state'], 'intent')

    def test_detached_prepare_can_read_privately_but_cannot_publish_after_revocation(self):
        draft = self.draft()
        r = self.request(draft=draft, admitted=False, materialized=False)
        original, copy = self.paths(r)
        inode = original.stat().st_ino
        detached, allow_read, opened, allow_close = (threading.Event() for _ in range(4))
        errors = []
        reader = self.rq._read_sources
        real_read = os.read
        key = self.native.key()

        def paused(sources):
            if threading.current_thread() is thread:
                detached.set()
                if not allow_read.wait(5):
                    raise RuntimeError('Fictional test read pause timed out.')
            return reader(sources)

        def read(fd, size):
            if threading.current_thread() is thread and os.fstat(fd).st_ino == inode:
                opened.set()
                if not allow_close.wait(5):
                    raise RuntimeError('Fictional test close pause timed out.')
            return real_read(fd, size)

        def prepare():
            try:
                self.f.prepare_request(draft['id'], draft['capability'], fence(draft), key,
                                       self.native.binding, self.profile)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=prepare)
        with patch.object(self.rq, '_read_sources', side_effect=paused), patch.object(os, 'read', side_effect=read):
            thread.start()
            try:
                self.assertTrue(detached.wait(5))
                self.f.authorize_send(r['id'], r['capability'], r['revision'], r['fingerprint'],
                                      binding=self.native.binding, profile=self.profile)
                self.f.materialize(r['id'], r['copies'][0]['id'])
                confirmed = self.confirm(self.prepare(r))['operation']
                allow_read.set()
                self.assertTrue(opened.wait(5))
                self.assertTrue(self.f._store.files.io_active())
                got = self.finish(confirmed)
                self.assertEqual([p['state'] for p in got['pieces']], ['unknown', 'removed'])
                self.assertEqual(got['pieces'][0]['reason'], 'source_in_use')
                self.assertTrue(original.exists())
                self.assertFalse(copy.exists())
            finally:
                allow_read.set()
                allow_close.set()
                thread.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AttachmentError)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0], 1)
        self.assertIsNone(self.db.execute('SELECT target FROM seen WHERE key=?', (key,)).fetchone())
        self.reopen()
        self.assertEqual(self.finish(self.d.status(confirmed['id'], self.scope), recover=True)['state'], 'deleted')

    def test_actual_unknown_handoff_and_late_terminal_owner_facts_survive_removal(self):
        calls = []
        def uncertain(admission, attempt):
            self.assertFalse(self.f._store.db.in_transaction)
            self.assertFalse(self.native.owner_db.in_transaction)
            calls.append((admission, attempt))
            raise RuntimeError('Fictional recipient return was lost.')

        adapter = FictionalAdapterBinding(canonical(self.native.binding), self.profile, self.native.context)
        owner = self.native.owner
        mirror = NativeOutcomeMirror(self.rq)
        coordinator = OfflineNativeHandoff(self.rq, owner, mirror, self.f._locks, self.f._verified_writer(),
                                           adapter, uncertain, enabled=True)
        self.addCleanup(coordinator.stop)
        draft = self.draft()
        with self.f._lifetime.operation():
            r = mirror.prepare_request(draft['id'], draft['capability'], fence(draft), self.native.key(),
                                       self.native.binding, self.profile)
            with self.f._store._gate():
                self.f._enrolled_request(r['id'])
            with self.f._locks.hold(self.rq.resource_keys(r['id'])) as lease:
                mirror.authorize_send(r['id'], r['capability'], r['revision'], r['fingerprint'],
                                      resolved_binding=self.native.binding, transport_profile=self.profile,
                                      resource_lease=lease)
        saved = self.f.materialize(r['id'], r['copies'][0]['id'])
        with self.f._lifetime.operation(), self.assertRaises(AttachmentError) as caught:
            coordinator.handoff(r['id'], (saved,))
        self.assertEqual(caught.exception.code, 'native_boundary_uncertain')
        self.assertEqual(len(calls), 1)
        with self.f._lifetime.operation():
            self.assertFalse(coordinator.handoff(r['id'], (saved,)))
        owner_status = owner.status(r['ownerRequestId'])
        pair_digest, attempt = owner_status['admission_digest'], owner_status['attempt_id']

        def event(kind, ordinal, **kwargs):
            owner.record_outcome(r['ownerRequestId'], pair_digest, attempt, kind,
                evidence_sequence=ordinal, now_ms=int(time.time() * 1000) + ordinal, **kwargs)
            mirror.reconcile(r['id'], owner)

        event('unknown', 1)
        p = self.prepare(r)
        self.assertEqual(owner.status(r['ownerRequestId'])['outcome']['state'], 'unknown')
        confirmed = self.confirm(p)['operation']
        event('started', 2, turn_id='fictional-turn')
        event('reply', 3, turn_id='fictional-turn', reply='A fictional orchard answer.')
        identity = owner._exact_snapshot(r['ownerRequestId'], pair_digest)
        facts = mirror._as_of(r['id'])
        outbox = tuple(self.db.execute('SELECT * FROM attachment_outbox').fetchone())
        result = self.finish(confirmed)
        self.assertEqual(result['state'], 'deleted')
        self.assertEqual(owner._exact_snapshot(r['ownerRequestId'], pair_digest), identity)
        self.assertEqual(mirror._as_of(r['id']), facts)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM attachment_outbox').fetchone()), outbox)
        self.assertEqual(len(calls), 1)

    def test_refuses_old_root_unbound_writer_and_untagged_allocation_replay(self):
        existing = self.native.root / 'legacy-retention'
        existing.mkdir(mode=0o700)
        legacy = AttachmentStore(existing, enabled=True, owner_epoch=EPOCH)
        legacy.close()
        with self.assertRaises(FileExistsError):
            DisposableNativeDeletion.create(existing, EPOCH)
        with self.assertRaises(AttachmentError):
            DisposableNativeDeletion.reopen(str(existing), EPOCH)
        with self.assertRaises(AttachmentError):
            NativeDeletion(enabled=True)
        with self.assertRaises(AttachmentError):
            NativeDeletion().management(self.scope)

        draft = self.draft()
        key = self.native.key()
        args = (draft['id'], draft['capability'], fence(draft), key, self.native.binding, self.profile)
        with patch.object(self.f, '_writer', Materializer(enabled=True)):
            with self.assertRaises(AttachmentError):
                self.f.prepare_request(*args)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0], 0)

        legacy_writer = RequestLedger(self.f._store)
        r = legacy_writer.prepare_request(*args)
        row = self.db.execute('SELECT preview,receipt FROM deletion_copy_slots').fetchone()
        self.assertEqual(COPY_PREVIEW.decode(row['preview'])['anchor_id'], ZERO_ID)
        self.assertEqual(COPY_RECEIPT.decode(row['receipt'])['anchor_id'], ZERO_ID)
        with self.assertRaises(AttachmentError):
            self.f.prepare_request(*args)
        with self.assertRaises(AttachmentError):
            self.f.authorize_send(r['id'], r['capability'], r['revision'], r['fingerprint'],
                                  binding=self.native.binding, profile=self.profile)
        self.assertEqual(tuple(self.db.execute('SELECT preview,receipt FROM deletion_copy_slots').fetchone()), tuple(row))

    def test_binding_witness_loss_refuses_before_writable_store_open(self):
        request = self.request()
        preview = self.prepare(request)
        binding = self.f.binding
        with self.assertRaises(AttachmentError):
            DisposableNativeDeletion.reopen(binding, EPOCH)
        self.assertTrue(self.d.status(preview['id'], self.scope)['canConfirm'])
        with self.assertRaises(AttachmentError):
            DisposableNativeDeletion.reopen(binding, '9' * 64)
        with self.assertRaises(AttachmentError):
            DisposableNativeDeletion.reopen(replace(binding, cohort='8' * 64), EPOCH)
        self.db.execute("DROP INDEX native_delete_copy_source")
        path = self.f._store.files.root / 'ledger.sqlite3'
        self.f.close()
        with patch('attachments_native_deletion.AttachmentStore', side_effect=AssertionError('Writable open ran.')):
            with self.assertRaises(AttachmentError):
                DisposableNativeDeletion.reopen(binding, EPOCH)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='native_delete_copy_source'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_copies').fetchone()[0], 1)

    def test_cache_and_temp_index_are_never_silently_invalidated(self):
        cache = NativeReadReceipts(self.f._store)
        self.assertTrue(cache.require(self.f._store) is None)
        with self.assertRaises(FileExistsError):
            DisposableNativeDeletion.create(self.f.binding.root, EPOCH)
        with self.assertRaises(AttachmentError):
            DisposableNativeDeletion.reopen(self.f.binding, EPOCH)
        self.assertTrue(cache.require(self.f._store) is None)
        r = self.request()
        self.db.execute('CREATE TEMP TABLE deletion_copy_slots (copy_id TEXT)')
        with self.assertRaises(AttachmentError):
            self.prepare(r)
        self.assertTrue(all(path.exists() for path in self.paths(r)))
        self.db.execute('DROP TABLE temp.deletion_copy_slots')
        self.assertTrue(self.prepare(r)['canConfirm'])

    def test_transplanted_tag_and_paired_reverse_roster_detect_foreign_metadata(self):
        first, neighbor = self.request(), self.request()
        bad_id, donor_id = (r['copies'][0]['id'] for r in (first, neighbor))
        saved = self.db.execute('SELECT preview,receipt FROM deletion_copy_slots WHERE copy_id=?', (bad_id,)).fetchone()
        donor = self.db.execute('SELECT preview FROM deletion_copy_slots WHERE copy_id=?', (donor_id,)).fetchone()[0]
        tag = COPY_PREVIEW.decode(donor)['anchor_id']
        self.db.execute('UPDATE deletion_copy_slots SET preview=?,receipt=? WHERE copy_id=?',
            (COPY_PREVIEW.encode(dict(COPY_PREVIEW.decode(saved[0]), anchor_id=tag)),
             COPY_RECEIPT.encode(dict(COPY_RECEIPT.decode(saved[1]), anchor_id=tag)), bad_id))
        with self.assertRaises(AttachmentError):
            self.prepare(first)
        files = {row['id']: row for row in self.d.management(self.scope)['files']}
        self.assertTrue(files[self.source(first)]['available'])
        self.assertFalse(files[self.source(first)]['canPrepare'])
        self.assertTrue(files[self.source(neighbor)]['canPrepare'])
        self.db.execute('UPDATE deletion_copy_slots SET preview=?,receipt=? WHERE copy_id=?', (*saved, bad_id))

        self.db.execute('UPDATE workspace_copies SET source_id=? WHERE id=?', (self.source(first), donor_id))
        for r in (first, neighbor):
            with self.assertRaises(AttachmentError):
                self.prepare(r)
            self.assertTrue(all(path.exists() for path in self.paths(r)))
        self.assertEqual(self.quota(), 4 * len(ORIGINAL))

    def test_bank_of_extra_intents_cannot_expand_exact_resource_union(self):
        draft = self.draft()
        extras = [self.request(draft=draft, admitted=False, materialized=False) for _ in range(7)]
        admitted = self.request(draft=draft)
        with self.assertRaises(AttachmentError) as caught:
            self.prepare(admitted)
        self.assertEqual(caught.exception.code, 'deletion_roster_unavailable')
        self.assertEqual(self.db.execute('SELECT count(*) FROM attachment_requests').fetchone()[0], len(extras) + 1)
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        self.assertTrue(all(path.exists() for path in self.paths(admitted)))

    def test_expired_and_physically_stale_previews_require_explicit_supersession(self):
        r = self.request(materialized=False)
        first = self.prepare(r)
        self.f.materialize(r['id'], r['copies'][0]['id'])
        status = self.d.status(first['id'], self.scope)
        self.assertFalse(status['canConfirm'])
        with self.assertRaises(AttachmentError):
            self.confirm(first)
        fixed = time.time()
        self.f._store.clock = lambda: fixed
        second = self.prepare(r, 1)
        self.f._store.clock = lambda: fixed + 180
        self.assertFalse(self.d.status(first['id'], self.scope)['canConfirm'])
        with self.assertRaises(AttachmentError):
            self.confirm(second)
        third = self.prepare(r, 2)
        self.assertEqual(third['managementGeneration'], 3)
        self.assertEqual(self.finish(self.confirm(third)['operation'])['state'], 'deleted')

    def test_post_io_moved_operation_generation_cannot_release_charge(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        def moved(event):
            if event != 'native_deletion_physical_returned':
                return
            self.assertFalse(self.db.in_transaction)
            slot = operation['id']
            before = SOURCE_RECEIPT.decode(self.db.execute(
                'SELECT receipt FROM deletion_source_slots WHERE slot_id=?', (slot,)).fetchone()[0])
            encoded = SOURCE_RECEIPT.encode(dict(before, operation_generation=before['operation_generation'] + 1))
            self.db.execute('UPDATE deletion_source_slots SET receipt=? WHERE slot_id=?', (encoded, slot))
        self.f._store.checkpoint = moved
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.assertFalse(original.exists())
        self.assertTrue(copy.exists())
        status = self.d.status(operation['id'], self.scope)
        self.assertEqual(status['pieces'][0]['state'], 'unknown')
        self.assertEqual(status['releasedBytes'], 0)
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))

    def test_wrong_workspace_parent_and_descriptor_publication_refused(self):
        r = self.request(materialized=False)
        original, copy = self.paths(r)
        saved = self.f.materialize(r['id'], r['copies'][0]['id'])
        row = self.rq._copy(saved.copy_id)
        spec = self.rq._intent(row)
        authority = self.rq.authority(r['id'])
        dirs = WorkspaceDirectories.from_record(self.rq.workspace_directories(r['id']))
        retained = RetainedCopyCleanup(saved.copy_id, saved.lease_id, saved.copy_generation,
                                       saved.identity, saved.sha256, saved.bytes, 'full')
        with self.f._locks.hold(self.rq.resource_keys(r['id'])) as lease:
            with self.assertRaises(AttachmentError):
                self.f._writer.verify_copy(self.native.context, authority, spec, retained, lease, directories=dirs)
            with self.assertRaises(AttachmentError):
                self.f._writer.remove_workspace_copy(self.f._store.files, self.native.context, authority, spec,
                                                      retained, lease, directories=dirs)
        confirmed = self.confirm(self.prepare(r))['operation']
        displaced = copy.parent.with_name('fictional-displaced')
        copy.parent.rename(displaced)
        copy.parent.mkdir(mode=0o700)
        copy.write_bytes(b'Fictional foreign file.')
        copy.chmod(0o600)
        kept = self.finish(confirmed)
        self.assertEqual([p['state'] for p in kept['pieces']], ['removed', 'changed'])
        self.assertFalse(original.exists())
        self.assertEqual(copy.read_bytes(), b'Fictional foreign file.')
        self.assertEqual((displaced / copy.name).read_bytes(), ORIGINAL)
        self.assertEqual(self.quota(), len(ORIGINAL))

    def test_actual_workspace_close_failure_quarantines_until_process_exit(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        binding = self.f.binding
        self.f.close()
        child = multiprocessing.get_context('spawn').Process(target=exit_after_workspace_close_error,
            args=(binding, self.native.context, operation['id'], operation['revision'], str(copy)))
        child.start()
        child.join(12)
        if child.is_alive():
            child.kill()
            child.join(5)
            self.fail('The workspace-close process did not exit.')
        self.assertEqual(child.exitcode, 31)
        self.assertFalse(original.exists() or copy.exists())
        self.f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: self.native.context)
        self.bind()
        state = self.d.status(operation['id'], self.scope)
        self.assertEqual([p['state'] for p in state['pieces']], ['removed', 'unknown'])
        self.assertEqual(state['releasedBytes'], len(ORIGINAL))
        self.assertEqual(self.quota(), len(ORIGINAL))
        self.assertEqual(self.finish(state, recover=True)['state'], 'deleted')
        self.assertEqual(self.quota(), 0)

    def test_lost_accounting_ack_does_not_refund_again(self):
        r = self.request()
        original, copy = self.paths(r)
        operation = self.confirm(self.prepare(r))['operation']
        def fault(event):
            if event == 'native_deletion_physical_returned':
                self.db.commit_fault = 'after'
        self.f._store.checkpoint = fault
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.assertFalse(original.exists())
        self.assertTrue(copy.exists())
        observed = self.d.status(operation['id'], self.scope)
        self.assertEqual(observed['pieces'][0]['state'], 'removed')
        self.assertEqual(self.quota(), len(ORIGINAL))
        with self.assertRaises(AttachmentError):
            self.finish(operation)
        self.reopen()
        done = self.finish(observed)
        self.assertEqual(done['state'], 'deleted')
        self.assertEqual(self.finish(done, recover=True)['releasedBytes'], 2 * len(ORIGINAL))
        self.assertEqual(self.quota(), 0)

    def test_shutdown_during_physical_attempt_refuses_new_entry_and_drains(self):
        r = self.request()
        operation = self.confirm(self.prepare(r))['operation']
        paused, leave, errors = threading.Event(), threading.Event(), []

        def checkpoint(event):
            if event == 'native_deletion_physical_returned':
                paused.set()
                if not leave.wait(5):
                    raise RuntimeError('Fictional shutdown pause timed out.')

        def execute():
            try:
                self.finish(operation)
            except Exception as exc:
                errors.append(exc)

        self.f._store.checkpoint = checkpoint
        active = threading.Thread(target=execute)
        active.start()
        try:
            self.assertTrue(paused.wait(5))
            with self.assertRaises(AttachmentError) as caught:
                self.f.close(timeout=0.02)
            self.assertEqual(caught.exception.code, 'deletion_busy')
            with self.assertRaises(AttachmentError) as caught:
                self.d.status(operation['id'], self.scope)
            self.assertEqual(caught.exception.code, 'attachments_disabled')
            self.assertFalse(self.f._store.closed)
            self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        finally:
            leave.set()
            active.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(active.is_alive())
        self.assertEqual(self.quota(), 0)
        self.f.close()
        self.assertTrue(self.f._store.closed)

    def test_retained_cleanup_rejects_wrong_digest_and_lease(self):
        for field in ('sha256', 'lease_id'):
            with self.subTest(field=field):
                r = self.request()
                original, copy = self.paths(r)
                op = self.confirm(self.prepare(r))['operation']
                remove = Materializer.remove_workspace_copy

                def wrong(materializer, files, context, authority, spec, saved, lease, *, directories):
                    return remove(materializer, files, context, authority, spec,
                                  replace(saved, **{field: '9' * 64}), lease, directories=directories)

                with patch.object(Materializer, 'remove_workspace_copy', wrong):
                    outcome = self.finish(op)
                self.assertEqual([p['state'] for p in outcome['pieces']], ['removed', 'changed'])
                self.assertFalse(original.exists())
                self.assertEqual(copy.read_bytes(), ORIGINAL)
                self.assertEqual(outcome['releasedBytes'], len(ORIGINAL))

    def test_regular_foreign_workspace_file_is_left_intact_and_charged(self):
        r = self.request()
        original, copy = self.paths(r)
        confirmed = self.confirm(self.prepare(r))['operation']
        replacement = copy.with_name('fictional-replacement.pdf')
        replacement.write_bytes(b'Fictional unrelated replacement.')
        replacement.chmod(0o600)
        replacement.replace(copy)
        result = self.finish(confirmed)
        self.assertEqual([p['state'] for p in result['pieces']], ['removed', 'changed'])
        self.assertFalse(original.exists())
        self.assertEqual(copy.read_bytes(), b'Fictional unrelated replacement.')
        self.assertEqual(self.quota(), len(ORIGINAL))

    def test_same_inode_change_at_unlink_checkpoint_is_left_intact_and_charged(self):
        baseline = self.quota()
        for changed_at in ('checkpoint', 'authorization'):
            with self.subTest(changed_at=changed_at):
                r = self.request()
                original, copy = self.paths(r)
                confirmed = self.confirm(self.prepare(r))['operation']
                inode = copy.stat().st_ino
                changed = ORIGINAL[:-1] + b'!'
                reached = []
                mutations = []

                def mutate():
                    copy.write_bytes(changed)
                    self.assertEqual(copy.stat().st_ino, inode)
                    self.assertEqual(copy.stat().st_size, len(ORIGINAL))
                    mutations.append(changed_at)

                def checkpoint(event):
                    if event == 'cleanup_before_unlink':
                        reached.append(event)
                        if changed_at == 'checkpoint':
                            mutate()

                authorize = Materializer._authorize

                def authorize_then_change(worker, context, authority, lease, phase, copies=()):
                    authorize(worker, context, authority, lease, phase, copies)
                    if phase == 'cleanup' and reached and changed_at == 'authorization':
                        mutate()

                self.f._store.checkpoint = checkpoint
                with patch.object(Materializer, '_authorize', authorize_then_change):
                    result = self.finish(confirmed)
                self.assertEqual(len(reached), 1)
                self.assertEqual(mutations, [changed_at])
                self.assertEqual([p['state'] for p in result['pieces']], ['removed', 'changed'])
                self.assertFalse(original.exists())
                self.assertEqual(copy.read_bytes(), changed)
                self.assertEqual(copy.stat().st_ino, inode)
                self.assertEqual(result['pieces'][1]['chargedBytes'], len(ORIGINAL))
                self.assertEqual(result['releasedBytes'], len(ORIGINAL))
                baseline += len(ORIGINAL)
                self.assertEqual(self.quota(), baseline)

    def test_scope_keyset_paginates_many_unrelated_real_requests(self):
        requests = [self.request(materialized=False) for _ in range(51)]
        next_page = self.d.management(self.scope)
        self.assertEqual(len(next_page['files']), 50)
        cursor = next_page['nextCursor']
        self.assertIsNotNone(cursor)
        last = self.d.management(self.scope, cursor)
        self.assertIsNone(last['nextCursor'])
        self.assertEqual(len(last['files']), 1)
        file_ids = [item['id'] for item in next_page['files'] + last['files']]
        self.assertEqual(len(set(file_ids)), 51)
        self.assertEqual(set(file_ids), {self.source(r) for r in requests})
        with self.assertRaises(AttachmentError):
            self.d.management(replace(self.scope, scope_id='fictional-other-scope'), cursor)
        self.assertEqual(self.finish(self.confirm(self.prepare(requests[11]))['operation'])['state'], 'deleted')

    def test_replaced_database_inode_cannot_reopen_even_with_original_records(self):
        r = self.request()
        operation = self.confirm(self.prepare(r))['operation']
        binding = self.f.binding
        database = self.f._store.files.root / 'ledger.sqlite3'
        moved = database.with_name('fictional-original.sqlite3')
        self.f.close()
        database.rename(moved)
        database.write_bytes(moved.read_bytes())
        database.chmod(0o600)
        with patch('attachments_native_deletion.AttachmentStore', side_effect=AssertionError('Writable open ran.')):
            with self.assertRaises(AttachmentError):
                DisposableNativeDeletion.reopen(binding, EPOCH)
        moved.replace(database)
        self.f = DisposableNativeDeletion.reopen(binding, EPOCH, resolve=lambda auth: self.native.context)
        self.bind()
        self.assertEqual(self.d.status(operation['id'], self.scope), operation)

    def test_partial_generic_fence_is_already_an_authority_conflict(self):
        r = self.request()
        p = self.prepare(r)
        self.db.execute('UPDATE attachment_requests SET cleanup_generation=1 WHERE id=?', (r['id'],))
        with self.assertRaises(AttachmentError):
            self.confirm(p)
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))
        self.assertTrue(all(path.exists() for path in self.paths(r)))

    def test_original_png_uses_same_exact_receipts_and_physical_accounting(self):
        self.profile = replace(self.profile, media_types=('image/png',), original_relay_formats=('image/png',))
        png = b'\x89PNG\r\n\x1a\nFictional orchard pixels'
        d = self.f.create_draft(self.native.binding, self.native.key())
        d['revision'] = self.f.edit_text(d['id'], d['capability'], fence(d), 'Review the fictional image.')['revision']
        u = self.f.reserve_upload(d['id'], d['capability'], fence(d),
                                  metadata(png, 'image/png', 'orchard.png'), self.native.key())
        d['revision'] = u['draftRevision']
        d['revision'] = self.f.receive_upload(d['id'], d['capability'], fence(d), u['id'],
                                             u['fingerprint'], io.BytesIO(png))['draftRevision']
        r = self.request(draft=d)
        self.assertEqual(self.d.management(self.scope)['files'][0]['mediaType'], 'image/png')
        self.assertTrue(all(path.read_bytes() == png for path in self.paths(r)))
        done = self.finish(self.confirm(self.prepare(r))['operation'])
        self.assertEqual(done['releasedBytes'], 2 * len(png))
        self.assertFalse(any(path.exists() for path in self.paths(r)))

    def test_downgraded_base_cannot_migrate_on_existing_only_reopen(self):
        r = self.request()
        self.prepare(r)
        binding = self.f.binding
        path = self.f._store.files.root / 'ledger.sqlite3'
        self.assertEqual(self.db.execute('SELECT count(*) FROM officer_document_intents').fetchone()[0], 0)
        self.db.execute('DROP TABLE officer_document_intents')
        self.db.execute('PRAGMA user_version=9')
        self.f.close()
        with patch('attachments_native_deletion.AttachmentStore', side_effect=AssertionError('Writable open ran.')):
            with self.assertRaises(AttachmentError):
                DisposableNativeDeletion.reopen(binding, EPOCH)
        with closing(sqlite3.connect(path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='officer_document_intents'").fetchone())
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)
            db.execute('PRAGMA user_version=10')
        with patch('attachments_native_deletion.AttachmentStore', side_effect=AssertionError('Writable open ran.')):
            with self.assertRaises(AttachmentError):
                DisposableNativeDeletion.reopen(binding, EPOCH)

    def test_missing_cohort_marker_cannot_reopen_generic_cleanup_after_confirm(self):
        draft = self.draft()
        extra = self.request(draft=draft, admitted=False, materialized=False)
        r = self.request(draft=draft)
        confirmed = self.confirm(self.prepare(r))['operation']
        self.assertTrue(confirmed['sourceUnavailable'])
        self.db.execute("DELETE FROM meta WHERE key='native_deletion_cohort_v1'")
        with self.assertRaises(AttachmentError):
            self.rq.begin_saved_cleanup(extra['id'], extra['capability'], extra['revision'])
        result = self.rq.request_status(extra['id'], extra['capability'])
        self.assertEqual(result['state'], 'intent')
        self.assertEqual(result['revision'], extra['revision'])
        self.assertTrue(all(path.exists() for path in self.paths(r)))
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))

    def test_never_created_copy_proof_does_not_depend_on_neighbor_creating_parent(self):
        first, neighbor = self.request(materialized=False), self.request(materialized=False)
        confirmed = self.confirm(self.prepare(first))['operation']
        self.f.materialize(neighbor['id'], neighbor['copies'][0]['id'])
        self.assertTrue(all(path.read_bytes() == ORIGINAL for path in self.paths(neighbor)))
        self.assertEqual(self.finish(confirmed)['state'], 'deleted')
        self.assertTrue(all(path.read_bytes() == ORIGINAL for path in self.paths(neighbor)))
        self.assertEqual(self.quota(), 2 * len(ORIGINAL))

    def test_retained_history_details_and_management_match_exact_catalogue_association(self):
        first, neighbor = self.request(), self.request()
        unclassified = self.request(materialized=False)
        page = self.d.history(self.scope)
        self.assertEqual((page['associationCount'], page['unclassifiedAssociationCount']), (2, 1))
        self.assertIsNone(page['nextCursor'])
        associations = {row['requestId']: row for row in page['items']}
        field_names = {'id', 'requestId', 'name', 'bytes', 'mediaType', 'cataloguedAt',
                       'savedCopyState', 'savedCopyAvailable', 'retentionPinned',
                       'preparationKind', 'previewAvailable', 'expiresAt'}
        for r in (first, neighbor):
            row = associations[r['id']]
            self.assertEqual(set(row), field_names)
            self.assertEqual(row, self.d.file_details(self.scope, r['id'], self.source(r)))
            self.assertEqual(row['savedCopyState'], 'unknown')
            self.assertFalse(row['savedCopyAvailable'] or row['previewAvailable'])
            expected_pin = self.db.execute('SELECT retention_pin FROM attachment_outbox WHERE request_id=?', (r['id'],)).fetchone()[0]
            self.assertEqual(row['retentionPinned'], bool(expected_pin))
            self.assertTrue(all(path.exists() for path in self.paths(r)))
        self.assertEqual(self.quota(), 6 * len(ORIGINAL))
        index = {r['id']: r for r in self.d.management(self.scope)['files']}
        self.assertEqual({index[self.source(r)]['requestId'] for r in (first, neighbor, unclassified)},
                         {r['id'] for r in (first, neighbor, unclassified)})
        with self.assertRaises(AttachmentError):
            self.d.file_details(self.scope, first['id'], self.source(neighbor))
        with self.assertRaises(AttachmentError):
            self.d.file_details(self.scope, unclassified['id'], self.source(unclassified))

        op = self.confirm(self.prepare(first))['operation']
        self.assertEqual(self.d.file_details(self.scope, first['id'], self.source(first)), associations[first['id']])
        after = self.finish(op)
        self.assertEqual(after['state'], 'deleted')
        self.assertEqual(self.d.file_details(self.scope, first['id'], self.source(first)),
                         dict(associations[first['id']], savedCopyState='removed'))
        current = self.d.history(self.scope)
        self.assertEqual((current['associationCount'], current['unclassifiedAssociationCount']), (2, 1))
        self.assertEqual({row['requestId']: row for row in current['items']}[neighbor['id']], associations[neighbor['id']])
        self.assertEqual(self.quota(), 4 * len(ORIGINAL))

    def test_history_refuses_retired_epoch_even_for_unclassified_association(self):
        current, unclassified = self.request(), self.request(materialized=False)
        with self.assertRaises(AttachmentError) as denied:
            self.d.history(replace(self.scope, scope_id='fictional-unrecorded'))
        self.assertEqual(denied.exception.code, 'forbidden')
        self.db.execute('UPDATE attachment_requests SET epoch=? WHERE id=?', ('e' * 64, unclassified['id']))
        with self.assertRaises(AttachmentError) as retired:
            self.d.history(self.scope)
        self.assertEqual(retired.exception.code, 'forbidden')
        good = self.d.file_details(self.scope, current['id'], self.source(current))
        self.assertFalse(good['savedCopyAvailable'])

    def test_history_refuses_catalogue_count_when_retained_association_is_invalid(self):
        first, second = self.request(), self.request()
        self.db.execute('UPDATE workspace_copies SET source_id=? WHERE id=?',
                        (self.source(first), second['copies'][0]['id']))
        with self.assertRaises(AttachmentError):
            self.d.history(self.scope)
        with self.assertRaises(AttachmentError):
            self.d.file_details(self.scope, second['id'], self.source(second))
        self.assertEqual(self.quota(), 4 * len(ORIGINAL))

    def test_history_refuses_scope_key_drift_and_untagged_unclassified_copy(self):
        r = self.request()
        hidden = self.request(materialized=False)
        self.db.execute('UPDATE attachment_requests SET catalogue_scope=? WHERE id=?', ('fictional-other', hidden['id']))
        with self.assertRaises(AttachmentError) as changed:
            self.d.history(self.scope)
        self.assertEqual(changed.exception.code, 'forbidden')
        self.db.execute('UPDATE attachment_requests SET catalogue_scope=? WHERE id=?', (self.scope.scope_id, hidden['id']))
        self.assertEqual(self.d.history(self.scope)['unclassifiedAssociationCount'], 1)
        cid = hidden['copies'][0]['id']
        values = self.db.execute('SELECT preview,receipt FROM deletion_copy_slots WHERE copy_id=?', (cid,)).fetchone()
        self.db.execute('UPDATE deletion_copy_slots SET preview=?,receipt=? WHERE copy_id=?',
            (COPY_PREVIEW.encode(dict(COPY_PREVIEW.decode(values[0]), anchor_id=ZERO_ID)),
             COPY_RECEIPT.encode(dict(COPY_RECEIPT.decode(values[1]), anchor_id=ZERO_ID)), cid))
        with self.assertRaises(AttachmentError):
            self.d.history(self.scope)
        self.assertEqual(self.d.file_details(self.scope, r['id'], self.source(r))['id'], self.source(r))
