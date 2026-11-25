"""Product Claude adapter over real temporary guarded owner and attachment stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import io
import os
from pathlib import Path
import sqlite3
from threading import local
import time
import unittest
import uuid

from attachments_native_fixture import FaultConnection, GIT, fence, metadata
from attachments_materialize import FileIdentity, Materializer, WorkspaceDirectories, copy_identity
from attachments_native_mirror import NativeOutcomeMirror
from attachments_native_namespace import OWNER_DB, NamespaceError
from attachments_owner_runtime import bind_owner_runtime
from attachments_owner_transport import (
    CLAUDE_CLAIMS, CLAUDE_COMPLETION_PROFILE, LINK_BYTES, RETENTION_MS, LedgerError,
)
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError, canonical
from claude_attachment_adapter import (
    BUSY_INDEX, VERSION, ClaudeAttachmentAdapter, ClaudeAttachmentBinding, protected_busy,
)
from test_attachments_native_composition import CompositionFixture

PDF = b'%PDF-1.4\nFictional orchard retention.\n%%EOF'


def new_id():
    return str(uuid.uuid4())


class _FaultFactory:
    connect_original = staticmethod(sqlite3.connect)

    def connect(self, database, *args, **kwargs):
        if not kwargs.get('uri') and Path(str(database)).name == OWNER_DB:
            kwargs['factory'] = FaultConnection
        return self.connect_original(database, *args, **kwargs)


class _Permission:
    """Fixture entry authority with the real product owner/host fence and exact scope."""

    def __init__(self, fixture):
        self.f = fixture
        self.thread = local()
        with fixture.messaging.lock:
            fixture.fence.validate_mount(fixture.owner_db)

    @contextmanager
    def operation(self, action, request_id, identity):
        assert not getattr(self.thread, 'scope', None)
        assert not self.f.messaging.lock._is_owned()
        self.thread.scope = (action, request_id, identity)
        try:
            if action == 'claude_claim':
                with self.admission_fence(self.f.owner, identity[0]):
                    yield
            else:
                yield
        finally:
            self.thread.scope = None

    def check(self, action, *identity):
        observed = getattr(self.thread, 'scope', None)
        if observed is None:
            raise LedgerError('test_permission_refused')
        allowed = ('admit', 'claim') if observed[0] == 'handoff' else (observed[0],)

        def guard():
            if (self.thread.scope is not observed
                    or action is not None and (action not in allowed or identity != observed[2])):
                raise LedgerError('test_permission_refused')

        guard()
        return guard

    def bind_admission(self, owner, admission, digested):
        action, request_id, _ = self.thread.scope
        expected = self.f.mirror._expected(request_id)
        if action != 'handoff' or expected.admission != admission or expected.digest != digested:
            raise LedgerError('test_permission_refused')
        self.thread.scope = action, request_id, (admission.owner_request_id, digested)

    @contextmanager
    def admission_fence(self, owner, owner_id):
        try:
            with owner.lock, self.f.fence.admission(owner.db, owner_id):
                yield
        except NamespaceError as error:
            raise AttachmentError(error.code, 'Fictional namespace unavailable.', 503) from None


@unittest.skipUnless(os.name == 'posix' and GIT, 'Requires private POSIX SQLite fixtures and Git.')
class ClaudeAttachmentAdapterTests(unittest.TestCase):
    def setUp(self):
        f = self.f = CompositionFixture(self, trace=_FaultFactory())
        f.binding = dict(f.binding, id='c:' + new_id())
        f.profile = replace(f.profile, id=CLAUDE_COMPLETION_PROFILE, adapter='claude_official',
                            media_types=('application/pdf',), original_relay_formats=('application/pdf',))
        f.mirror = NativeOutcomeMirror(f.requests)
        resources = ResourceLocks(str(f.store.files.root), enabled=True)
        f.resources = resources
        self.addCleanup(resources.close)
        f.materializer = Materializer(enabled=True, workspace_files=f.store.files,
            validate_authority=f.validate_authority, record_created=f.requests.record_copy_created,
            record_directory=f.requests.record_directory_created, record_partial=f.requests.record_partial)
        self.permission = _Permission(f)
        f.owner = bind_owner_runtime(f.messaging, allow_install=False, scheduler=f.scheduler,
                                     _native_permission=self.permission).ledger
        f.workers[-1].checkpoint()
        self.binding = ClaudeAttachmentBinding(canonical(f.binding), f.profile, f.context)
        self.entry = dict(id=new_id(), grant_id=new_id(), agent=f.binding['id'], project=f.context.root,
                          generation=new_id(), client_id=new_id(), active=1, lifecycle_protocol=2,
                          last_seen=time.time())
        self.adapter = self.mount(install=True)
        f.workers[-1].checkpoint()
        self.addCleanup(lambda: self.adapter.stop())

    def mount(self, *, install=False, reconcile=True):
        f = self.f
        return ClaudeAttachmentAdapter(f.requests, f.owner, f.mirror, f.resources, f.materializer,
            owner_scope=self.permission.operation, reconcile=self.reconcile if reconcile else None,
            allow_install=install, enabled=True)

    def reconcile(self, request_id):
        expected = self.f.mirror._expected(request_id)

        def acknowledge(*identity):
            assert identity[:2] == (expected.admission.owner_request_id, expected.digest)
            before = self.permission.thread.scope
            self.permission.thread.scope = ('ack', request_id, identity)
            try:
                return self.f.owner.acknowledge(*identity)
            finally:
                self.permission.thread.scope = before

        with self.permission.operation('reconcile', request_id, ()):
            return self.f.mirror.reconcile(request_id, self.f.owner, _acknowledge=acknowledge)

    def staged(self):
        f = self.f
        draft = f.store.create_draft(f.binding, f.key())
        draft['revision'] = f.store.edit_text(draft['id'], draft['capability'], fence(draft), 'Review the orchard.')['revision']
        upload = f.store.reserve_upload(draft['id'], draft['capability'], fence(draft),
            metadata(PDF, 'application/pdf', 'orchard.pdf'), f.key(), preparation_kind='original_relay')
        draft['revision'] = upload['draftRevision']
        draft['revision'] = f.store.receive_upload(draft['id'], draft['capability'], fence(draft), upload['id'],
                                                    upload['fingerprint'], io.BytesIO(PDF))['draftRevision']
        request = f.prepare(draft=draft)
        rid = request['id']
        with f.hold(request) as lease:
            f.authorize(request, lease)
            authority = f.requests.authority(rid)
            dirs = f.materializer.prepare_directories(f.context, authority, lease,
                directories=WorkspaceDirectories.from_record(f.requests.workspace_directories(rid)))
            spec = f.requests.claim_copy(rid, authority.operation_revision, request['copies'][0]['id'], lease)
            authority = f.requests.authority(rid)
            f.materializer.ensure_exclusion(f.context, authority, (spec,), lease)
            with f.requests.source_handle(spec.id, lease) as fd:
                saved = f.materializer.materialize(f.context, authority, spec, fd,
                    copy_identity(FileIdentity.of(os.fstat(fd))), lease, directories=dirs)
            f.requests.record_copy_result(saved, lease)
        return request, (saved,), f.repo / spec.relative_path

    def offer(self, staged=None):
        request, saved, _ = self.staged() if staged is None else staged
        self.entry['last_seen'] = time.time()
        self.assertTrue(self.adapter.offer(request['id'], saved, self.binding, enrollment=self.entry))
        return request

    def poll(self, entry=None, *, recheck=None):
        caller = self.entry if entry is None else entry
        caller['last_seen'] = time.time()
        return self.adapter.poll(caller, recheck=recheck or (lambda: caller))

    def claimed(self):
        request = self.offer()
        claim = self.poll()
        self.assertIsNotNone(claim)
        return request, claim

    def receipt(self, claim, entry=None, **overrides):
        caller = self.entry if entry is None else entry
        return dict({
            'request_id': claim['request_id'], 'attempt_id': claim['attempt_id'],
            'native_id': caller['agent'][2:], 'generation': caller['generation'], 'client_id': caller['client_id'],
            'kind': 'completed', 'turn_id': 'fictional-turn', 'event_id': new_id(), 'occurred_at': 17,
            'outcome': 'answered', 'answer': 'WATCH_REPLY ' + claim['request_id'] + '\nFictional answer.',
        }, **overrides)

    def emit(self, data, entry=None, recheck=None):
        caller = self.entry if entry is None else entry
        return self.adapter.event(caller, data, recheck=recheck or (lambda: caller))

    def state(self, request):
        return self.adapter.observe(request['id'])

    def test_one_claim_then_completion_only_cumulative_facts_and_no_generic_writer(self):
        request = self.offer()
        self.assertTrue(self.adapter.handles(request['ownerRequestId']))
        self.assertIsNone(self.poll(recheck=lambda: None))
        self.assertEqual(self.state(request)['outcome']['state'], 'owner_recorded')
        expected = self.f.mirror._expected(request['id'])
        owner_id = request['ownerRequestId']
        with self.permission.operation('handoff', request['id'], (owner_id, expected.digest)):
            with self.assertRaisesRegex(LedgerError, 'protected_claude_claim'):
                self.f.owner.record_claim(owner_id, expected.digest, now_ms=1)
        claim = self.poll()
        self.assertIsNone(self.poll())
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        self.assertEqual(set(claim), {'request_id', 'attempt_id', 'native_id', 'generation', 'envelope',
                                      'expires_at', 'lifecycle_protocol'})
        self.assertEqual((claim['request_id'], claim['lifecycle_protocol']), (owner_id, 2))
        self.assertEqual(claim['envelope'], expected.admission.envelope)
        attempt = claim['attempt_id']
        with self.permission.operation('evidence', request['id'], (owner_id, expected.digest, attempt)):
            with self.assertRaisesRegex(LedgerError, 'protected_claude_outcome'):
                self.f.owner.record_outcome(owner_id, expected.digest, attempt, 'accepted', evidence_sequence=1, now_ms=1)
        for kind in ('submitted', 'started', 'unknown', 'dropped'):
            event = {k: v for k, v in self.receipt(claim).items() if k in ('request_id', 'attempt_id',
                     'native_id', 'generation', 'client_id')}
            event.update(kind=kind)
            if kind == 'started':
                event.update(turn_id='fictional-early-turn')
            before = self.f.owner_db.total_changes
            self.assertEqual(self.emit(event), dict(request_id=owner_id, state='unknown', turn_id=''))
            self.assertEqual(self.f.owner_db.total_changes, before)
        pending = self.state(request)
        self.assertFalse(pending['outcome']['accepted'] or pending['outcome']['started'])
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        first = self.receipt(claim)
        self.assertEqual(self.emit(first), dict(request_id=owner_id, state='answered', turn_id='fictional-turn'))
        settled = self.state(request)
        self.assertFalse(self.adapter.busy(self.entry['agent']))
        self.assertEqual(settled['reply'], 'Fictional answer.')
        self.assertTrue(settled['outcome']['accepted'] and settled['outcome']['execution_ended'])
        self.assertFalse(settled['outcome']['started'])
        self.assertEqual((settled['outcome']['seq'], settled['outcome']['evidence_sequence']), (3, 0))
        before = self.f.owner_db.total_changes
        self.assertEqual(self.emit(first)['state'], 'answered')
        self.assertEqual(self.f.owner_db.total_changes, before)

    def test_event_reconnect_same_enrollment_and_conflict_by_raw_receipt_identity(self):
        request, claim = self.claimed()
        first = self.receipt(claim)
        self.emit(first)
        reconnected = dict(self.entry, generation=new_id(), client_id=new_id(), last_seen=time.time())
        replay = dict(first, generation=reconnected['generation'], client_id=reconnected['client_id'])
        before = self.f.owner_db.total_changes
        self.assertEqual(self.emit(replay, reconnected)['state'], 'answered')
        self.assertEqual(self.f.owner_db.total_changes, before)
        with self.assertRaises((AttachmentError, LedgerError)):
            self.emit(first, reconnected)
        distinct_uuid = dict(replay, event_id=new_id())
        self.assertEqual(self.emit(distinct_uuid, reconnected),
                         dict(request_id=claim['request_id'], state='unknown', turn_id=''))
        state = self.state(request)
        self.assertEqual(state['outcome']['state'], 'execution_ended')
        self.assertEqual(state['outcome']['reason'], 'reply_conflict')
        self.assertTrue(state['outcome']['accepted'] and state['outcome']['execution_ended'] and state['outcome']['conflict'])
        self.assertTrue(state['outcome']['reply_captured'])
        self.assertIsNone(state['reply'])
        before = self.f.owner_db.total_changes
        for event in (replay, distinct_uuid):
            self.assertEqual(self.emit(event, reconnected)['turn_id'], '')
        with self.assertRaises(AttachmentError):
            self.emit(dict(replay, event_id=new_id()), reconnected)
        self.assertEqual(self.f.owner_db.total_changes, before)
        slot = self.f.owner_db.execute(f'SELECT typeof(record),length(record),busy FROM {CLAUDE_CLAIMS}').fetchone()
        self.assertEqual(slot, ('blob', 4096, 0))
        self.assertEqual(self.f.owner_db.execute('SELECT COUNT(*) FROM peer_execution_events').fetchone()[0], 0)

    def test_completion_after_payload_prune_can_hold_conflict_without_new_prose(self):
        request, claim = self.claimed()
        first = self.receipt(claim)
        self.emit(first)
        old = self.state(request)
        link = old['link']
        with self.permission.operation('prune', request['id'], ()):
            pruned = self.f.owner.prune_payload(claim['request_id'], link['admission_digest'],
                                                now_ms=link['created_ms'] + RETENTION_MS + 1)
        self.assertTrue(pruned['payload_pruned'])
        before = self.f.owner_db.total_changes
        self.assertEqual(self.emit(first)['state'], 'unknown')
        self.assertEqual(self.f.owner_db.total_changes, before)
        self.assertFalse(self.f.mirror._as_of(request['id'])[0]['payload_pruned'])
        self.assertIsNone(self.state(request)['reply'])
        self.assertEqual(self.adapter.reconcile_pending(), 1)
        self.assertTrue(self.f.mirror._as_of(request['id'])[0]['payload_pruned'])
        collision = dict(first, turn_id='fictional-other-turn', occurred_at=1)
        ack = self.emit(collision)  # Same event UUID, different canonical receipt.
        self.assertEqual(ack, dict(request_id=claim['request_id'], state='unknown', turn_id=''))
        live = self.state(request)
        self.assertTrue(live['link']['payload_pruned'])
        self.assertTrue(live['outcome']['capture_withheld'])
        self.assertTrue(live['outcome']['conflict'])
        self.assertEqual(live['outcome']['turn_id'], old['outcome']['turn_id'])
        self.assertEqual(live['outcome']['reply_fingerprint'], old['outcome']['reply_fingerprint'])
        self.assertIsNone(live['reply'])
        self.assertIsNone(self.f.owner_db.execute('SELECT data FROM requests WHERE id=?', (claim['request_id'],)).fetchone())

    def test_empty_answer_and_explicit_withheld_are_ended_without_an_early_stage(self):
        for returned in ('answer_marker_only', 'withheld'):
            with self.subTest(returned=returned):
                request, claim = self.claimed()
                if returned == 'answer_marker_only':
                    event = self.receipt(claim, answer='WATCH_REPLY ' + claim['request_id'])
                else:
                    event = self.receipt(claim, outcome='withheld', answer='')
                self.assertEqual(self.emit(event)['state'], 'unknown')
                current = self.state(request)
                self.assertTrue(current['outcome']['accepted'] and current['outcome']['execution_ended'])
                self.assertFalse(current['outcome']['started'] or current['outcome']['reply_captured'])
                self.assertEqual(current['outcome']['capture_withheld'], returned == 'withheld')
                self.assertIsNone(current['reply'])

    def test_wrong_enrollment_grant_attempt_protocol_or_malformed_answer_cannot_clear_busy(self):
        request, claim = self.claimed()
        event = self.receipt(claim)
        rejected = (
            (dict(self.entry, id=new_id()), event),
            (dict(self.entry, grant_id=new_id()), event),
            (dict(self.entry, project=str(self.f.root)), event),
            (dict(self.entry, lifecycle_protocol=1), event),
            (self.entry, dict(event, attempt_id=new_id())),
            (self.entry, dict(event, kind='answered')),
            (self.entry, dict(event, occurred_at=2**53)),
            (self.entry, dict(event, turn_id='fictional\x00turn')),
            (self.entry, dict(event, answer='Missing correlation')),
            (self.entry, dict(event, answer=event['answer']+'\nWATCH_REPLY '+claim['request_id'])),
            (self.entry, dict(event, answer=event['answer']+'\ud800')),
            (self.entry, dict(event, outcome='aborted')),
        )
        before = self.f.owner_db.total_changes
        for entry, data in rejected:
            with self.subTest(entry=entry.get('lifecycle_protocol'), kind=data.get('kind')):
                with self.assertRaises((AttachmentError, LedgerError)):
                    self.emit(data, entry)
        with self.assertRaises((AttachmentError, LedgerError)):
            self.emit(event, recheck=lambda: dict(self.entry, client_id=new_id()))
        self.assertEqual(self.f.owner_db.total_changes, before)
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        self.assertFalse(self.adapter.busy('c:' + new_id()))
        self.assertEqual(self.state(request)['outcome']['seq'], 2)

    def test_reader_is_read_only_while_failed_mirror_is_repaired_in_bounded_maintenance(self):
        request, claim = self.claimed()
        first = self.receipt(claim)
        callback = self.adapter._reconcile
        self.adapter._reconcile = lambda _: (_ for _ in ()).throw(OSError('Fictional mirror loss.'))
        self.assertEqual(self.emit(first)['state'], 'answered')
        observed = self.state(request)
        self.assertEqual(observed['outcome']['seq'], 3)
        self.assertIsNone(observed['reply'])
        before = self.f.owner_db.total_changes, self.f.store.db.total_changes
        for _ in range(3):
            self.assertIsNone(self.state(request)['reply'])
            self.assertEqual(self.emit(first)['state'], 'answered')
        self.assertEqual((self.f.owner_db.total_changes, self.f.store.db.total_changes), before)
        self.adapter._reconcile = callback
        self.assertEqual(self.adapter.reconcile_pending(limit=20), 1)
        observed = self.state(request)
        self.assertEqual(observed['reply'], 'Fictional answer.')
        self.assertEqual(observed['link']['ack_seq'], 3)
        self.adapter._reconcile = lambda _: (_ for _ in ()).throw(OSError('Fictional conflict-mirror loss.'))
        self.emit(dict(first, event_id=new_id(), outcome='withheld', answer=''))
        before = self.f.owner_db.total_changes, self.f.store.db.total_changes
        current = self.state(request)
        self.assertTrue(current['outcome']['conflict'])
        self.assertIsNone(current['reply'])
        self.assertEqual((self.f.owner_db.total_changes, self.f.store.db.total_changes), before)

    def test_claim_slot_and_owner_claim_rollback_together_and_lost_return_cannot_rearm(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault):
                request = self.offer()
                self.f.owner_db.commit_fault = fault
                with self.assertRaisesRegex(AttachmentError, 'Fictional namespace unavailable'):
                    self.poll()
                self.assertIsNone(self.poll())
                committed = fault == 'after'
                self.assertEqual(self.state(request)['link']['claim'], 'claimed' if committed else 'unclaimed')
                self.assertEqual(self.adapter.busy(self.entry['agent']), committed)
                self.assertEqual(self.f.owner_db.execute(f'SELECT COUNT(*) FROM {CLAUDE_CLAIMS}').fetchone()[0], int(committed))
                self.f.workers[-1].checkpoint()

    def test_lost_terminal_commit_retries_same_receipt_without_new_durable_transition(self):
        request, claim = self.claimed()
        event = self.receipt(claim)
        self.f.owner_db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.emit(event)
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        self.assertEqual(self.state(request)['outcome']['seq'], 2)
        self.f.owner_db.commit_fault = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.emit(event)
        self.assertFalse(self.adapter.busy(self.entry['agent']))
        before = self.f.owner_db.total_changes
        self.assertEqual(self.emit(event)['state'], 'answered')
        self.assertEqual(self.f.owner_db.total_changes, before)
        self.assertIsNone(self.state(request)['reply'])
        self.assertEqual(self.adapter.reconcile_pending(), 1)
        self.assertEqual(self.state(request)['reply'], 'Fictional answer.')

    def test_file_change_and_offer_restart_are_no_new_dispatch(self):
        request, saved, path = self.staged()
        self.offer((request, saved, path))
        path.write_bytes(b'Fictional changed workspace bytes.')
        with self.assertRaises(AttachmentError):
            self.poll()
        self.assertEqual(self.state(request)['link']['claim'], 'unclaimed')
        self.adapter.stop()
        self.adapter = self.mount()
        self.assertIsNone(self.poll())
        self.assertFalse(self.adapter.offer(request['id'], saved, self.binding, enrollment=self.entry))
        self.assertEqual(self.state(request)['outcome']['state'], 'owner_recorded')

    def test_concurrent_polls_consume_only_one_offer(self):
        request = self.offer()
        entry = dict(self.entry, last_seen=time.time())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.adapter.poll, entry, recheck=lambda: entry) for _ in range(2)]
            outcomes = []
            for f in futures:
                try:
                    outcomes.append(f.result(timeout=8))
                except AttachmentError:
                    outcomes.append(None)
        self.assertEqual(sum(item is not None for item in outcomes), 1)
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        self.assertEqual(self.state(request)['outcome']['seq'], 2)

    def test_off_protected_occupancy_survives_reconnect_expiry_and_missing_layout_spelling(self):
        request, claim = self.claimed()
        self.adapter.stop()
        db = self.f.owner_db
        with self.f.owner_lock:
            before = db.total_changes
            self.assertTrue(protected_busy(db, self.entry['agent']))
            self.assertFalse(protected_busy(db, 'c:' + new_id()))
            self.assertEqual(db.total_changes, before)
            # Re-encode the same validated identity with equivalent escapes.
            raw = db.execute('SELECT record FROM owner_attachment_links WHERE owner_request_id=?',
                             (claim['request_id'],)).fetchone()[0]
            changed = raw.rstrip().replace(b'"profile":"claude-completion-only"', b'"profile":"\\u0063laude-completion-only"')
            self.assertNotEqual(changed, raw.rstrip())
            db.execute('UPDATE owner_attachment_links SET record=? WHERE owner_request_id=?',
                       (changed.ljust(LINK_BYTES, b' '), claim['request_id']))
            db.execute(f'DROP INDEX {BUSY_INDEX}')
            db.execute(f'DROP TABLE {CLAUDE_CLAIMS}')
            db.execute(f'DROP TABLE {VERSION}')
            db.commit()
            self.assertEqual(self.f.owner._read(claim['request_id'])[0]['identity']['profile'], CLAUDE_COMPLETION_PROFILE)
            with self.assertRaises((AttachmentError, LedgerError)):
                protected_busy(db, self.entry['agent'])
            self.assertFalse(protected_busy(db, 'c:' + new_id()))
        self.assertTrue(self.adapter.handles(claim['request_id']))
        self.assertFalse(self.adapter.handles(new_id()))
        with self.assertRaises((AttachmentError, LedgerError)):
            self.mount(install=True)

    def test_no_mount_or_setup_on_default_off(self):
        f = self.f
        with f.owner_lock:
            before = f.owner_db.total_changes
            schema = f.owner_db.execute('PRAGMA schema_version').fetchone()[0]
        inert = ClaudeAttachmentAdapter(None, None, None, None, None, owner_scope=None)
        self.assertFalse(inert.busy(self.entry['agent']))
        self.assertFalse(inert.handles(new_id()))
        self.assertFalse(inert.offer(None, None, None, enrollment=None))
        self.assertIsNone(inert.poll(None, recheck=lambda: None))
        self.assertEqual(inert.reconcile_pending(), 0)
        self.assertEqual(f.owner_db.total_changes, before)
        self.assertEqual(f.owner_db.execute('PRAGMA schema_version').fetchone()[0], schema)

    def test_read_with_a_working_reconcile_callback_makes_no_sqlite_changes(self):
        request, claim = self.claimed()
        before = self.f.owner_db.total_changes, self.f.store.db.total_changes
        current = self.state(request)
        self.assertEqual(current['outcome']['seq'], 2)
        self.assertIsNone(self.f.mirror._as_of(request['id']))
        self.assertEqual((self.f.owner_db.total_changes, self.f.store.db.total_changes), before)
        self.assertEqual(self.adapter.reconcile_pending(), 1)
        self.assertEqual(self.state(request)['link']['ack_seq'], 2)

    def test_positive_offer_depends_on_both_fresh_commits_even_on_lost_acknowledgement(self):
        for marker in ('attachment', 'owner'):
            with self.subTest(marker=marker):
                request, saved, _ = self.staged()
                db = self.f.store.db if marker == 'attachment' else self.f.owner_db
                db.commit_fault = 'after'
                valid_fault = AttachmentError if marker == 'attachment' else (AttachmentError, sqlite3.Error)
                with self.assertRaises(valid_fault):
                    self.adapter.offer(request['id'], saved, self.binding, enrollment=self.entry)
                self.assertIsNone(self.poll())
                self.f.workers[-1].checkpoint()
                self.assertFalse(self.adapter.offer(request['id'], saved, self.binding, enrollment=self.entry))
                self.assertIsNone(self.poll())

    def test_live_layout_and_scope_mismatch_never_become_stock_claim_clear(self):
        request, claim = self.claimed()
        db = self.f.owner_db
        with self.f.owner_lock:
            db.execute(f'CREATE TEMP TABLE {CLAUDE_CLAIMS} (owner_request_id)')
            with self.assertRaises((AttachmentError, LedgerError)):
                protected_busy(db, self.entry['agent'])
            db.execute(f'DROP TABLE temp.{CLAUDE_CLAIMS}')
            db.execute(f'UPDATE main.{CLAUDE_CLAIMS} SET agent_id=? WHERE owner_request_id=?',
                       ('c:' + new_id(), claim['request_id']))
            db.commit()
            with self.assertRaises((AttachmentError, LedgerError)):
                protected_busy(db, self.entry['agent'])
        with self.assertRaises((AttachmentError, LedgerError)):
            self.state(request)

    def test_unauthenticated_ordinary_writer_cannot_enter_protected_claim_or_reducer(self):
        request = self.offer()
        expected = self.f.mirror._expected(request['id'])
        owner_id = expected.admission.owner_request_id
        with self.permission.operation('ordinary', request['id'], ()):
            with self.f.owner.ordinary_writer():
                with self.assertRaises(LedgerError):
                    self.f.owner._claim_claude_in_transaction(owner_id, expected.digest, new_id(), b'', now_ms=1)
                with self.assertRaises(LedgerError):
                    self.f.owner._claude_terminal_in_transaction(owner_id, expected.digest, new_id(),
                        'claude_terminal', 'c'*64, b'', b'', turn_id='fictional-turn', reply='', now_ms=1)
        self.assertEqual(self.state(request)['link']['claim'], 'unclaimed')

    def test_mixed_generic_attempt_cannot_be_created_after_protected_layout_loss(self):
        request = self.offer()
        expected = self.f.mirror._expected(request['id'])
        owner_id = request['ownerRequestId']
        self.adapter.stop()
        db = self.f.owner_db
        with self.f.owner_lock:
            db.execute(f'DROP TABLE {CLAUDE_CLAIMS}')
            db.execute(f'DROP TABLE {VERSION}')
            db.commit()
            self.assertFalse(protected_busy(db, self.entry['agent']))  # The exact unclaimed owner never received an envelope.
        self.f.workers[-1].checkpoint()
        with self.permission.operation('handoff', request['id'], (owner_id, expected.digest)):
            with self.assertRaisesRegex(LedgerError, 'protected_claude_claim'):
                self.f.owner.record_claim(owner_id, expected.digest, now_ms=1)
        with self.assertRaises((AttachmentError, LedgerError)):
            self.mount(install=True)

    def test_final_claim_window_refuses_changed_ordinary_poll_priority_and_consumes_offer(self):
        request = self.offer()
        calls = []

        def recheck():
            calls.append(self.f.owner_db.in_transaction)
            return self.entry if len(calls) == 1 else None

        with self.assertRaises((AttachmentError, LedgerError)):
            self.poll(recheck=recheck)
        self.assertEqual(calls, [True, True])
        self.assertIsNone(self.poll())
        self.assertEqual(self.state(request)['link']['claim'], 'unclaimed')
        self.assertEqual(self.f.owner_db.execute(f'SELECT COUNT(*) FROM {CLAUDE_CLAIMS}').fetchone()[0], 0)

    def test_unscopable_retained_link_cannot_clear_target_when_adjunct_gone(self):
        request, claim = self.claimed()
        db = self.f.owner_db
        self.adapter.stop()
        with self.f.owner_lock:
            db.execute(f'DROP TABLE {CLAUDE_CLAIMS}')
            db.execute(f'DROP TABLE {VERSION}')
            db.execute('UPDATE owner_attachment_links SET record=? WHERE owner_request_id=?',
                       (b'invalid'.ljust(LINK_BYTES, b' '), claim['request_id']))
            db.commit()
            for target in (self.entry['agent'], 'c:' + new_id()):
                with self.assertRaises((AttachmentError, LedgerError)):
                    protected_busy(db, target)
        with self.assertRaises(AttachmentError):
            self.state(request)

    def test_initial_slot_statement_failure_closes_offer_and_rolls_back_owner_claim(self):
        request = self.offer()
        self.f.owner_db.update_fault = f'INSERT INTO main.{CLAUDE_CLAIMS}'
        with self.assertRaises((AttachmentError, sqlite3.Error)):
            self.poll()
        self.assertIsNone(self.poll())
        self.assertEqual(self.state(request)['link']['claim'], 'unclaimed')
        self.assertEqual(self.f.owner_db.execute(f'SELECT COUNT(*) FROM {CLAUDE_CLAIMS}').fetchone()[0], 0)

    def test_existing_owner_connection_reopens_with_busy_receipt_and_no_replayed_envelope(self):
        request, claim = self.claimed()
        self.adapter.stop()
        self.f.close_owner()
        self.f.open_owner()
        self.permission = _Permission(self.f)
        self.f.owner = bind_owner_runtime(self.f.messaging, allow_install=False, scheduler=self.f.scheduler,
            _native_permission=self.permission).ledger
        self.f.workers[-1].checkpoint()
        self.adapter = self.mount()
        self.assertTrue(self.adapter.busy(self.entry['agent']))
        self.assertIsNone(self.poll())
        self.assertEqual(self.state(request)['outcome']['seq'], 2)
        self.assertEqual(self.emit(self.receipt(claim))['state'], 'answered')
        self.assertFalse(self.adapter.busy(self.entry['agent']))


if __name__ == '__main__':
    unittest.main()
