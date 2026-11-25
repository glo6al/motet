"""Real composed startup, namespace and lifecycle using fictional dependencies."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from attachments_native_fixture import CONTENT, GIT, TURN, NativeFixture
from attachments_native_composition import VERSION_KEY, _FictionalQualification, _open_schema, bind_native_composition
from attachments_native_mirror import NativeOutcomeMirror
import attachments_native_namespace as namespace
from attachments_native_receipts import NativeReadReceipts
from attachments_owner_guards import LINKS, OUTCOMES, OwnerAttachmentGuard
from attachments_owner_transport import LedgerError, OwnerAttachmentLedger
from attachments_service import AttachmentService, UnqualifiedResolver
from attachments_types import AttachmentError
from native_managed_namespace_fixture import MANAGED_WHY, Adapter, ManagedError, ManagedFixture, fixture_files
from officer_messaging import MessagingStore
import server
from test_attachments_owner_startup import ManualWorker
from test_attachments_owner_guards import admission as fixture_admission
from test_native_managed_namespace import ConnectionProbe, WAIT


def _exclusive(path, raw):
    fd = os.open(path, os.O_EXCL | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        assert os.write(fd, raw) == len(raw)
        os.fsync(fd)
        return namespace._id(os.fstat(fd))
    finally:
        os.close(fd)


def provision_disposable_witnesses(fixture):
    """The product has no provisioning entry point. Use a fresh fixture only."""
    root = fixture.state
    fence = namespace.NativeNamespaceFence(root)
    assert not (root / namespace.MANAGED).exists() and not fence.detected()
    host = _exclusive(fence.host, b'')
    messaging = fixture.messaging
    with messaging.lock, fence.host_fence():
        nonce = secrets.token_hex(32)
        db = messaging.db
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        messaging._attachment_guard = OwnerAttachmentGuard.install(db, messaging.lock)
        db.execute(f'CREATE TABLE {namespace.OWNER_IDENTITY} ({namespace.IDENTITY})')
        db.execute(f'INSERT INTO {namespace.OWNER_IDENTITY} VALUES (1,?)', (nonce,))
        db.commit()
        namespace._sync_directory(root)
        authority = _exclusive(fence.authority, namespace._line({'v': 1, 'state': 'never_created', 'owner': nonce}))
        namespace._sync_directory(root)
        _exclusive(fence.anchor, namespace._line(dict(v=1, owner=nonce, db=namespace._id(fence.owner.stat()),
                                                      authority=authority, host=host, root=namespace._id(root.stat()))))
        namespace._sync_directory(root)
    return fence


class CompositionFixture(NativeFixture):
    def __init__(self, test, *, trace=None, provision=True):
        self.messaging = None
        self.trace = trace
        self.workers = []
        super().__init__(test)
        self.profile = replace(self.profile, media_types=('text/plain', 'image/png'), original_relay_formats=('image/png',))
        self.adapter_binding = replace(self.adapter_binding, profile=self.profile)
        self.fence = provision_disposable_witnesses(self) if provision else None
        fixture_files(self.state)
        self.managed = ManagedFixture(self.state, None)  # Actual production namespace path; fictional runner only.
        self.messaging.managed = self.managed.manager
        self.base = AttachmentService(self.store, resolver=UnqualifiedResolver(), officer_deletions=False)

    def open_owner(self, *, initial=False, maintenance=False):
        self.close_owner()
        if self.trace is not None:
            with patch.object(sqlite3, 'connect', side_effect=self.trace.connect):
                self.messaging = MessagingStore(str(self.state), str(self.repo))
        else:
            self.messaging = MessagingStore(str(self.state), str(self.repo))
        self.owner_lock, self.owner_db = self.messaging.lock, self.messaging.db

    def close_owner(self):
        if self.messaging is not None:
            self.messaging.close()
            self.messaging = None
            self.owner_db = None

    def close_store(self):
        if self.messaging is not None:
            self.messaging.stop_native_attachment_runtime()
        super().close_store()

    def scheduler(self, *args):
        worker = ManualWorker(*args)
        self.workers.append(worker)
        return worker

    def bind(self, *, qualify=True, boundary=None):
        q = _FictionalQualification(self.adapter_binding, self.resources, self.materializer,
                                    boundary or self.boundary) if qualify else None
        runtime = bind_native_composition(self.messaging, self.base, _qualification=q, scheduler=self.scheduler)
        self.test.assertIsNotNone(runtime)
        self.workers[-1].checkpoint()
        self.test.assertEqual(runtime.state, 'ready')
        composition = self.messaging._native_composition
        if composition is not None:
            self.mirror = composition.mirror
            self.requests = self.mirror.requests
        self.owner = runtime.ledger
        return composition

    def request_for(self, owner_id):
        draft = self.draft()
        with patch('attachments_requests.uuid.uuid4', return_value=uuid.UUID(owner_id)):
            return self.prepare(draft=draft)


class ProductConnectionProbe(ConnectionProbe):
    def connect(self, database, *args, **kwargs):
        if not kwargs.get('uri') or not str(database).endswith('?mode=rw'):
            return super().connect(database, *args, **kwargs)
        original = self.connect_original

        def connect_witnessed(_name, *a, **kw):
            return original(database, *a, uri=True, **kw)

        self.connect_original = connect_witnessed
        try:
            kwargs.pop('uri')
            return super().connect(str(database).split('?', 1)[0], *args, **kwargs)
        finally:
            self.connect_original = original


def _managed_contender(root, kind, run, request_id, permission, active, go, attempted, entered, release, pipe):
    fixture = ManagedFixture(Path(root), None)
    trace = ProductConnectionProbe(role='managed', active=active, entered=entered, release=release)
    lock_identity = namespace._id((Path(root) / namespace.HOST).stat())
    import fcntl
    real_flock = fcntl.flock

    def observed(fd, flags):
        if namespace._id(os.fstat(fd)) == lock_identity:
            attempted.set()
        return real_flock(fd, flags)

    with pipe, patch('sqlite3.connect', side_effect=trace.connect), patch('fcntl.flock', side_effect=observed):
        assert go.wait(WAIT)
        trace.armed = True
        try:
            fixture.action(kind, run, request_id, permission)
        except ManagedError as error:
            result = error.code
        else:
            result = 'committed'
        pipe.send((result, tuple(fixture.calls), fixture.claims, trace.fired))


def _flock_contender(host, pipe):
    import fcntl
    with pipe, namespace._open(Path(host)) as (fd, _):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pipe.send('held')
        else:
            pipe.send('acquired')


# The composition is the delivery of files to agent chats. On Windows it is not built (NATIVE_ATTACHMENT_DELIVERY_HOST
# keeps it off until that delivery is ported) and these tests also race managed Claude, which is Mac-only by design.
# The fence and owner runtime it rests on are tested on Windows in test_attachments_windows_namespace.py.
WHY = (MANAGED_WHY and 'Delivery to agent chats is off on Windows (not ported yet), and these tests race managed '
       'Claude, which is Mac-only by design.') or (not GIT and 'Requires Git.') or None


@unittest.skipIf(WHY, WHY)
class CompositionTests(unittest.TestCase):
    def error(self, codes, fn):
        with self.assertRaises((AttachmentError, LedgerError, ManagedError, namespace.NamespaceError)) as caught:
            fn()
        self.assertIn(caught.exception.code, (codes,) if isinstance(codes, str) else codes)

    def test_atomic_schema_group_and_ambiguous_return(self):
        f = CompositionFixture(self, provision=False)
        db = f.store.db
        version = db.execute('PRAGMA user_version').fetchone()[0]
        quota = f.store.storage()['usedBytes']
        artifacts = ("SELECT name FROM main.sqlite_schema WHERE name IN "
                     "('native_outcome_mirror','native_read_receipts','native_read_pending_cleanup')")
        for failure in ('CREATE TABLE native_outcome_mirror', 'CREATE TABLE native_read_receipts',
                        'CREATE INDEX native_read_pending_cleanup', 'INSERT INTO main.meta'):
            with self.subTest(failure=failure):
                db.update_fault = failure
                self.error('storage_unavailable', lambda: _open_schema(f.store, allow_install=True))
                self.assertEqual(db.execute(artifacts).fetchall(), [])
                self.error('native_composition_unavailable', lambda: _open_schema(f.store, allow_install=False))
        for fault in ('before', 'after'):
            db.commit_fault = fault
            writes = []
            db.set_trace_callback(writes.append)
            try:
                self.error('storage_unavailable', lambda: _open_schema(f.store, allow_install=True))
            finally:
                db.set_trace_callback(None)
            mirror_ddl = next(i for i, sql in enumerate(writes) if sql.startswith('CREATE TABLE native_outcome_mirror'))
            receipt_ddl = next(i for i, sql in enumerate(writes) if sql.startswith('CREATE TABLE native_read_receipts'))
            self.assertLess(mirror_ddl, receipt_ddl)
            self.assertEqual(len(db.execute(artifacts).fetchall()), 3 if fault == 'after' else 0)
        mirror, receipts = _open_schema(f.store, allow_install=False)
        self.assertIs(mirror.store, f.store)
        self.assertEqual(receipts.schema_version, db.execute('PRAGMA schema_version').fetchone()[0])
        self.assertIsNone(receipts.lookup('draft_create', f.binding['id'], f.binding['scopeId'], 'a' * 64, None))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)
        self.assertEqual(f.store.storage()['usedBytes'], quota)
        db.execute("CREATE TABLE fictional_external_epoch (n INT)")
        self.error('storage_unavailable', lambda: receipts.lookup('draft_create', f.binding['id'], f.binding['scopeId'], 'a' * 64, None))

    def test_legacy_optional_subsets_and_shadowed_or_partial_group_hold(self):
        for kind in ('mirror', 'receipts'):
            with self.subTest(kind=kind):
                f = CompositionFixture(self, provision=False)
                if kind == 'mirror':
                    NativeOutcomeMirror(f.requests)
                else:
                    cache = NativeReadReceipts(f.store)
                    before = cache.schema_version
                    self.error('native_composition_unavailable', lambda: _open_schema(f.store, allow_install=True))
                    self.assertEqual(f.store.db.execute('PRAGMA schema_version').fetchone()[0], before)
                    del cache
                _, cache = _open_schema(f.store, allow_install=True)
                f.store.db.execute('CREATE TEMP TABLE native_read_receipts (n INT)')
                self.error('native_composition_unavailable', lambda: _open_schema(f.store, allow_install=True))
                self.error('storage_unavailable', lambda: cache.lookup('draft_create', f.binding['id'], f.binding['scopeId'], 'a' * 64, None))
                f.store.db.execute('DROP TABLE temp.native_read_receipts')
                f.store.db.execute('DROP INDEX native_read_pending_cleanup')
                before = f.store.db.execute('PRAGMA schema_version').fetchone()[0]
                self.error('storage_unavailable', lambda: _open_schema(f.store, allow_install=True))
                self.assertEqual(f.store.db.execute('PRAGMA schema_version').fetchone()[0], before)
                self.assertEqual(f.store.db.execute('SELECT value FROM meta WHERE key=?', (VERSION_KEY,)).fetchone()[0], '1')

    def test_real_factory_gate_matrix_and_dormant_disjoint_view(self):
        f = CompositionFixture(self, provision=False)
        with ExitStack() as stack:
            for officer in (False, True):
                for master, native, demo, service in ((True, False, False, f.base), (False, True, False, f.base),
                                                       (True, True, True, f.base), (True, True, False, None)):
                    with patch.multiple(server, ATTACHMENTS_ENABLED=master, OFFICER_DOCUMENTS_ENABLED=officer,
                                        NATIVE_AGENT_ATTACHMENTS_ENABLED=native), patch.dict(os.environ, WATCH_DEMO='1' if demo else '0'):
                        self.assertIsNone(server.start_native_attachment_maintenance(f.messaging, service))
                    self.assertIsNone(f.messaging._attachment_guard)
                    self.assertIsNone(f.messaging._native_composition)
                    self.assertIsNone(f.store.db.execute('SELECT 1 FROM meta WHERE key=?', (VERSION_KEY,)).fetchone())
            stack.enter_context(patch.multiple(server, ATTACHMENTS_ENABLED=True, NATIVE_AGENT_ATTACHMENTS_ENABLED=True))
            stack.enter_context(patch.dict(os.environ, WATCH_DEMO='0'))
            with patch('attachments_native_composition.bind_owner_runtime', side_effect=lambda *a, **kw: _manual_bind(f.scheduler, *a, **kw)):
                runtime = server.start_native_attachment_maintenance(f.messaging, f.base)
            comp = f.messaging._native_composition
            self.assertIs(runtime, comp.owner_runtime)
            self.assertIs(f.base, comp.base_service)
            self.assertTrue(runtime.ledger.maintenance_only)
            self.assertTrue(comp._reads._borrowed)
            self.assertEqual(comp._reads.native_selection(f.binding['id'])['features'],
                             dict(drafts=False, send=False, files=False, deletion=False))
            self.error('native_composition_unavailable', lambda: comp.handoff('a' * 32, ()))
            self.assertIsNotNone(comp.claude)
            self.assertIsNone(comp.claude.native_selection(f.binding['id'])['context'])
            self.error('native_composition_unavailable', runtime.ledger.pending_outcomes)
            f.messaging.stop_native_attachment_runtime()
            self.assertFalse(f.store.closed)
            self.assertEqual(f.base.store.storage()['usedBytes'], 0)
            self.assertEqual(f.calls, [])

    def test_qualified_fixture_handoff_reconcile_and_borrowed_read_view(self):
        f = CompositionFixture(self)
        comp = f.bind()
        r, saved, copy = f.materialize()
        self.assertIs(f.owner.guard, f.messaging._attachment_guard)
        self.assertTrue(f.owner.maintenance_only)
        self.assertIs(f.owner.db, f.messaging.db)
        self.error('native_composition_unavailable', f.owner.pending_outcomes)
        with comp.native_read(f.binding['id'], f.binding['scopeId']) as reads:
            self.assertIs(reads.store, f.base.store)
            selection = reads.native_selection(f.binding['id'])
            self.assertTrue(selection['features']['drafts'])
            self.assertFalse(selection['features']['send'])
            self.assertIsNone(reads.native_receipt('draft_create', f.binding['id'], f.binding['scopeId'], 'a' * 64)['match'])
        self.error('forbidden', lambda: self._select_officer(comp, f))
        expected_claims = []
        with ExitStack() as stack:
            for obj, method, field in ((f.requests, 'claim_handoff', 'newClaim'), (f.owner, 'admit', 'new_admission'),
                                       (f.owner, 'record_claim', 'new_claim')):
                ordinary = getattr(obj, method)
                def track(*args, _fn=ordinary, _field=field, **kwargs):
                    result = _fn(*args, **kwargs)
                    expected_claims.append((_field, result[_field]))
                    return result
                stack.enter_context(patch.object(obj, method, side_effect=track))
            self.assertTrue(comp.handoff(r['id'], saved))
        self.assertEqual(expected_claims, [('newClaim', True), ('new_admission', True), ('new_claim', True)])
        self.assertFalse(comp.handoff(r['id'], saved))
        self.assertEqual(len(f.calls), 1)
        admission, attempt = f.calls[0]
        digest = f.mirror._expected(r['id']).digest
        self.error('native_composition_unavailable', lambda: f.owner.admit(admission, now_ms=1))
        self.error('native_composition_unavailable', lambda: f.owner.acknowledge(admission.owner_request_id, digest, 2, 'a' * 64))
        begin = int(time.time() * 1000)
        comp.record_evidence(r['id'], attempt, 'accepted', evidence_sequence=1, now_ms=begin)
        comp.record_evidence(r['id'], attempt, 'unknown', evidence_sequence=2, now_ms=begin + 1)
        comp.record_evidence(r['id'], attempt, 'ended', turn_id=TURN, evidence_sequence=3, now_ms=begin + 2)
        comp.record_evidence(r['id'], attempt, 'reply', turn_id=TURN, reply='Fictional connected finding.',
                             evidence_sequence=4, now_ms=begin + 3)
        original = f.owner.acknowledge

        def exact(*identity):
            self.assertFalse(f.store.lock._is_owned() or f.store.db.in_transaction)
            self.assertFalse(f.owner_lock._is_owned() or f.owner_db.in_transaction)
            self.assertEqual(identity[2:], (f.facts(r)[1]['seq'], f.facts(r)[1]['digest']))
            return original(*identity)

        with patch.object(f.owner, 'acknowledge', side_effect=exact) as ack:
            f.store.db.commit_fault = 'after'
            self.error('storage_unavailable', lambda: comp.reconcile(r['id']))
            ack.assert_not_called()
            self.assertTrue(f.owner_status(r)['pending_ack'])
            self.assertEqual(comp.reconcile(r['id']), ('duplicate', 'acknowledged'))
        before = f.facts(r)
        self.assertEqual(comp.reconcile(r['id']), ('applied', 'acknowledged'))
        link, facts = f.facts(r)
        self.assertGreater(link['revision'], before[0]['revision'])
        self.assertEqual(facts, before[1])
        self.assertTrue(facts['accepted'] and facts['execution_ended'] and facts['capture_withheld'])
        self.assertTrue(link['terminal_acked'])
        self.assertFalse(f.status(r)['retentionPinned'])
        self.assertEqual(f.status(r)['replyState'], 'unknown')
        self.assertNotIn(b'Fictional connected finding.', f.mirror_row(r)[0])
        self.assertEqual(copy.read_bytes(), CONTENT)
        self.assertEqual(f.store.storage()['usedBytes'], 2 * len(CONTENT))
        self.assertTrue(comp._reads.accepting)
        f.messaging.stop_native_attachment_runtime()
        self.assertFalse(comp._reads.accepting)
        self.assertFalse(f.store.closed)

    @staticmethod
    def _select_officer(comp, f):
        with comp.native_read('officer', f.binding['scopeId']):
            raise AssertionError('Officer must never select a native read view.')

    def test_stop_drains_boundary_and_withholds_unstarted_ack(self):
        for checkpoint in ('boundary', 'ack'):
            with self.subTest(checkpoint=checkpoint):
                f = CompositionFixture(self)
                entered, release = threading.Event(), threading.Event()
                def pause():
                    entered.set()
                    self.assertTrue(release.wait(5))
                def call(*args):
                    f.boundary(*args)
                    if checkpoint == 'boundary':
                        pause()
                comp = f.bind(boundary=call)
                r, saved, _ = f.materialize()
                if checkpoint == 'ack':
                    self.assertTrue(comp.handoff(r['id'], saved))
                    attempt = f.calls[0][1]
                    comp.record_evidence(r['id'], attempt, 'ended', turn_id=TURN,
                                         evidence_sequence=1, now_ms=int(time.time() * 1000))
                original_ack = f.owner.acknowledge
                def hold_ack(*args):
                    pause()
                    return original_ack(*args)
                with ExitStack() as stack, ThreadPoolExecutor(2) as pool:
                    if checkpoint == 'ack':
                        stack.enter_context(patch.object(f.owner, 'acknowledge', side_effect=hold_ack))
                    action = (lambda: comp.handoff(r['id'], saved)) if checkpoint == 'boundary' else (lambda: comp.reconcile(r['id']))
                    future = pool.submit(action)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.error('native_composition_unavailable', lambda: comp.stop(timeout=0))
                        self.assertFalse(f.store.closed)
                        self.assertEqual(f.owner_db.execute('SELECT 1').fetchone()[0], 1)
                        self.error('native_composition_unavailable', lambda: comp.handoff(r['id'], saved))
                        self.error('attachments_unavailable', lambda: self._select_officer(comp, f))
                    finally:
                        release.set()
                    if checkpoint == 'ack':
                        self.error('native_composition_unavailable', lambda: future.result(timeout=5))
                    else:
                        self.assertTrue(future.result(timeout=5))
                f.messaging.stop_native_attachment_runtime()
                self.assertEqual(len(f.calls), 1)
                if checkpoint == 'ack':
                    self.assertTrue(f.owner_status(r)['pending_ack'])
                    self.assertTrue(f.facts(r)[1]['execution_ended'])

    def test_witness_loss_holds_manager_and_owner_without_recreation(self):
        f = CompositionFixture(self)
        run = f.managed.start()
        self.assertTrue(f.fence.detected())
        old = f.fence.registry.stat()
        comp = f.bind()
        r, saved, _ = f.materialize()
        self.assertTrue(comp.handoff(r['id'], saved))
        f.close_owner()
        f.fence.anchor.unlink()
        f.fence.authority.unlink()
        self.assertTrue(f.fence.detected())
        self.error('managed_unavailable', lambda: f.managed.action('enqueue', run, str(uuid.uuid4())))
        f.fence.owner.unlink()
        with patch('officer_messaging.peer_bridge.shared_schema') as initialize:
            self.error('managed_unavailable', f.open_owner)
            initialize.assert_not_called()
        self.assertFalse(f.fence.owner.exists())
        self.assertEqual(f.fence.registry.stat().st_ino, old.st_ino)

    def test_surviving_managed_identity_blocks_owner_open_after_external_witness_loss(self):
        for replace_owner in (True, False):
            with self.subTest(replace_owner=replace_owner):
                f = CompositionFixture(self)
                f.managed.start()
                comp = f.bind()
                r, saved, _ = f.materialize()
                self.assertTrue(comp.handoff(r['id'], saved))
                self.assertEqual(f.owner_db.execute(f'SELECT owner_request_id FROM {LINKS}').fetchone(),
                                 (r['ownerRequestId'],))
                f.close_owner()
                f.fence.anchor.unlink()
                f.fence.authority.unlink()
                if replace_owner:
                    replacement = f.root / 'replacement'
                    replacement.mkdir(mode=0o700)
                    with closing(MessagingStore(str(replacement), str(f.repo))) as empty:
                        self.assertEqual(empty.db.execute('SELECT COUNT(*) FROM requests').fetchone(), (0,))
                    (replacement / namespace.OWNER_DB).replace(f.fence.owner)
                self.assertTrue(f.fence.detected())
                with closing(sqlite3.connect(f.fence.owner)) as db:
                    before = db.execute('SELECT name,sql FROM sqlite_schema ORDER BY name').fetchall()
                owner_bytes = f.fence.owner.read_bytes()
                with patch('officer_messaging.peer_bridge.shared_schema') as initialize:
                    self.error('managed_unavailable', f.open_owner)
                    initialize.assert_not_called()
                self.assertEqual(f.fence.owner.read_bytes(), owner_bytes)
                with closing(sqlite3.connect(f.fence.owner)) as db:
                    self.assertEqual(db.execute('SELECT name,sql FROM sqlite_schema ORDER BY name').fetchall(), before)
                self.assertFalse(f.fence.anchor.exists() or f.fence.authority.exists())
                self.assertEqual(len(f.calls), 1)

    def test_legacy_managed_registry_keeps_ordinary_owner_startup(self):
        f = CompositionFixture(self, provision=False)
        run = f.managed.start()
        self.assertFalse(namespace.NativeNamespaceFence(f.state).detected())
        ask_id = str(uuid.uuid4())
        question = 'Fictional ordinary question.'
        decision = {'kind': 'none'}
        self.assertIsNone(f.messaging.claim_ask(ask_id, question))
        f.messaging.decide_ask(ask_id, decision)
        f.open_owner()
        self.assertIsNone(f.messaging._attachment_guard)
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM requests').fetchone(), (0,))
        self.assertEqual(f.messaging.claim_ask(ask_id, question), decision)
        managed_id = str(uuid.uuid4())
        f.managed.action('enqueue', run, managed_id)
        self.assertEqual(f.managed.manager.lookup_message(managed_id)['requestId'], managed_id)

    def test_witnessed_sqlite_open_never_recreates_a_disappearing_file(self):
        for role in ('owner', 'registry'):
            with self.subTest(role=role):
                f = CompositionFixture(self)
                observed = []
                connect = sqlite3.connect
                path = f.fence.owner if role == 'owner' else f.fence.registry
                if role == 'owner':
                    f.close_owner()
                    action = f.open_owner
                else:
                    run = f.managed.start()
                    action = lambda: f.managed.action('enqueue', run, str(uuid.uuid4()))

                def vanish(endpoint, *a, **kw):
                    if str(endpoint) in (path.as_uri() + '?mode=rw', path.as_uri() + '?mode=rwc'):
                        observed.append(kw['uri'])
                        path.unlink()
                    return connect(endpoint, *a, **kw)

                with patch.object(sqlite3, 'connect', side_effect=vanish):
                    self.error('managed_unavailable', action)
                self.assertEqual(observed, [True])
                self.assertFalse(path.exists())
                self.assertNotIn('enqueue', f.managed.calls)

    def test_installed_off_maintains_owner_hold_and_never_acknowledges(self):
        f = CompositionFixture(self)
        run = f.managed.start()
        comp = f.bind()
        r, saved, _ = f.materialize()
        self.assertTrue(comp.handoff(r['id'], saved))
        comp.record_evidence(r['id'], f.calls[0][1], 'ended', turn_id=TURN, evidence_sequence=1,
                             now_ms=int(time.time() * 1000))
        f.open_owner()
        with patch.multiple(server, ATTACHMENTS_ENABLED=False, NATIVE_AGENT_ATTACHMENTS_ENABLED=False), patch.dict(os.environ, WATCH_DEMO='0'):
            from attachments_owner_runtime import bind_owner_runtime

            with patch('attachments_owner_runtime.bind_owner_runtime', side_effect=lambda *a, **kw: bind_owner_runtime(*a, scheduler=f.scheduler, **kw)):
                runtime = server.start_native_attachment_maintenance(f.messaging, None)
        f.workers[-1].checkpoint()
        self.assertIsNone(f.messaging._native_composition)
        self.assertTrue(runtime.ledger.status(r['ownerRequestId'])['pending_ack'])
        self.assertFalse(runtime.ledger.status(r['ownerRequestId'])['terminal_acked'])
        self.assertTrue(f.status(r)['retentionPinned'])
        self.error('owner_ledger_maintenance_only', lambda: f.mirror.reconcile(r['id'], runtime.ledger))
        self.error('request_conflict', lambda: f.managed.action('enqueue', run, r['ownerRequestId']))
        unrelated = str(uuid.uuid4())
        f.managed.action('enqueue', run, unrelated)
        with closing(sqlite3.connect(f.fence.registry)) as db:
            self.assertEqual(db.execute('SELECT id FROM messages WHERE id=?', (unrelated,)).fetchone(), (unrelated,))
        self.assertEqual(len(f.calls), 1)

    def test_managed_retains_pruned_and_partial_owner_reservations(self):
        f = CompositionFixture(self)
        run = f.managed.start()
        offline = OwnerAttachmentLedger(f.owner_db, f.owner_lock, guard=f.messaging._attachment_guard)
        ids = {}
        for kind in ('pruned', 'link', 'outcome'):
            admission = fixture_admission(f.binding['id'])
            owner_id = admission.owner_request_id
            ids[kind] = owner_id
            status = offline.admit(admission, now_ms=1000)
            digest = status['admission_digest']
            if kind == 'pruned':
                attempt = offline.record_claim(owner_id, digest, now_ms=2000)['attempt_id']
                out = offline.record_outcome(owner_id, digest, attempt, 'not_submitted',
                                             evidence_sequence=1, now_ms=3000)['outcome']
                offline.acknowledge(owner_id, digest, out['seq'], out['digest'])
                offline.prune_payload(owner_id, digest, now_ms=100_000_000)
        with f.owner_lock:
            f.owner_db.execute(f'DELETE FROM {OUTCOMES} WHERE owner_request_id=?', (ids['link'],))
            f.owner_db.execute(f'DELETE FROM {LINKS} WHERE owner_request_id=?', (ids['outcome'],))
            f.owner_db.commit()
        for kind, owner_id in ids.items():
            with self.subTest(kind=kind):
                self.error('request_conflict', lambda: f.managed.action('enqueue', run, owner_id))
                self.error('request_conflict', lambda: f.managed.action('decide', run, owner_id, str(uuid.uuid4())))
        self.assertNotIn('enqueue', f.managed.calls)
        self.assertNotIn('decision', f.managed.calls)

    def test_exact_attachment_claim_and_unshadowed_neutral_checks_in_qualified_mode(self):
        f = CompositionFixture(self)
        comp = f.bind()
        r, saved, _ = f.materialize()
        self.assertTrue(comp.handoff(r['id'], saved))
        exact, attempt = f.calls[0]
        forged = replace(exact, owner_request_id=str(uuid.uuid4()))
        with comp._operation('handoff', r['id']):
            self.error('native_composition_unavailable', lambda: f.owner.admit(forged, now_ms=1))
        self.assertIsNone(f.owner_db.execute(f'SELECT 1 FROM {LINKS} WHERE owner_request_id=?', (forged.owner_request_id,)).fetchone())
        outcome = f.owner_status(r)['outcome']
        f.owner_db.execute(f'CREATE TEMP TABLE {OUTCOMES}(owner_request_id TEXT, record BLOB)')
        with self.assertRaises(ValueError):
            comp.record_evidence(r['id'], attempt, 'accepted', evidence_sequence=1, now_ms=2)
        f.owner_db.execute(f'DROP TABLE temp.{OUTCOMES}')
        self.assertEqual(f.owner_status(r)['outcome'], outcome)

    def test_owner_admission_flock_survives_until_positive_commit(self):
        ctx = multiprocessing.get_context('spawn')
        active = ctx.Value('i', 0)
        entered, release = ctx.Event(), ctx.Event()
        trace = ProductConnectionProbe(role='native', active=active, entered=entered, release=release)
        f = CompositionFixture(self, trace=trace)
        comp = f.bind()
        r, saved, _ = f.materialize()
        read, send = ctx.Pipe(duplex=False)
        child = ctx.Process(target=_flock_contender, args=(f.fence.host, send))
        trace.armed = True
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(comp.handoff, r['id'], saved)
            try:
                self.assertTrue(entered.wait(WAIT))
                self.assertTrue(f.owner_db.in_transaction)
                child.start()
                send.close()
                self.assertTrue(read.poll(WAIT))
                self.assertEqual(read.recv(), 'held')
            finally:
                release.set()
                if child.pid is not None:
                    child.join(WAIT)
                    if child.is_alive():
                        child.kill()
                        child.join()
                read.close()
                send.close()
            self.assertTrue(future.result(timeout=WAIT))
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(active.value, 0)
        self.assertEqual(len(f.calls), 1)

    def test_failed_initialized_publication_holds_creation(self):
        f = CompositionFixture(self)
        original = namespace.NativeNamespaceFence._append

        def fail_ready(fence, record, transition):
            if transition['state'] == 'initialized':
                raise OSError('Fictional lost initialized publication.')
            return original(fence, record, transition)

        with patch.object(namespace.NativeNamespaceFence, '_append', side_effect=fail_ready, autospec=True):
            self.error('managed_unavailable', f.managed.start)
        self.assertTrue(f.fence.registry.is_file())
        self.assertNotIn('spawn', f.managed.calls)
        self.error('managed_unavailable', f.managed.start)
        self.assertIsNone(f.bind())
        self.assertEqual(f.messaging._native_composition_error, 'setup_unavailable')
        self.error('owner_ledger_maintenance_only', f.owner.pending_outcomes)

    def test_older_writer_surrogate_is_not_excluded_by_the_ordinary_flock(self):
        f = CompositionFixture(self)
        run = f.managed.start()
        comp = f.bind()
        r, saved, _ = f.materialize()
        self.assertTrue(comp.handoff(r['id'], saved))
        owner_id = r['ownerRequestId']
        # An old publisher can still acquire and use the ordinary flock after
        # the other process releases it. The witnessed fixture is not an upgrade fence.
        with f.fence.host_fence(), closing(sqlite3.connect(f.fence.registry)) as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?)', (owner_id, run, 'digest', '{}'))
            db.commit()
            self.assertEqual(db.execute('SELECT id FROM operations').fetchone(), (owner_id,))
        self.assertEqual(f.owner_db.execute(f'SELECT owner_request_id FROM {LINKS}').fetchone(), (owner_id,))

    def test_mount_requires_same_store_root_and_keeps_group_version_current(self):
        f = CompositionFixture(self, provision=False)
        other = CompositionFixture(self, provision=False)
        runtime = bind_native_composition(f.messaging, other.base, scheduler=f.scheduler)
        self.assertIsNotNone(runtime)
        self.assertIsNone(f.messaging._native_composition)
        self.assertIsNone(other.store.db.execute('SELECT 1 FROM meta WHERE key=?', (VERSION_KEY,)).fetchone())
        comp = f.bind(qualify=False)
        with f.store._gate():
            f.store.db.execute('UPDATE meta SET value=? WHERE key=?', ('2', VERSION_KEY))
        self.error('native_composition_unavailable', lambda: self._select_officer(comp, f))

    def test_product_managed_and_composed_writers_race_actual_processes(self):
        ctx = multiprocessing.get_context('spawn')
        for kind in ('enqueue', 'decide', 'new_chat'):
            for first in ('native', 'managed'):
                with self.subTest(kind=kind, first=first):
                    active = ctx.Value('i', 0)
                    native_entered, native_release = ctx.Event(), ctx.Event()
                    trace = ProductConnectionProbe(role='native', active=active, entered=native_entered, release=native_release)
                    f = CompositionFixture(self, trace=trace)
                    run = f.managed.start()
                    expected = Adapter.first_request_id(run) if kind == 'new_chat' else str(uuid.uuid4())
                    comp = f.bind()
                    r, saved, _ = f.materialize(f.request_for(expected))
                    self.assertEqual(r['ownerRequestId'], expected)
                    go, attempted, entered, release = (ctx.Event() for _ in range(4))
                    read, send = ctx.Pipe(duplex=False)
                    child = ctx.Process(target=_managed_contender, args=(f.state, kind, run, expected,
                                         str(uuid.uuid4()), active, go, attempted, entered, release, send))
                    child.start()
                    send.close()
                    native_attempted = threading.Event()
                    owner_return = threading.Event()
                    root_identity = namespace._id(f.fence.host.stat())
                    import fcntl
                    real_flock = fcntl.flock

                    def observed(fd, flags):
                        if namespace._id(os.fstat(fd)) == root_identity:
                            native_attempted.set()
                        return real_flock(fd, flags)

                    def handoff():
                        try:
                            return 'committed' if comp.handoff(r['id'], saved) else 'replayed'
                        except AttachmentError as error:
                            return error.code

                    admit = f.owner.admit

                    def observed_admit(*args, **kwargs):
                        trace.armed = True  # Only the cross-namespace reservation, not later attempt/ACK writes.
                        try:
                            result = admit(*args, **kwargs)
                        finally:
                            trace.armed = False
                        if first == 'native':
                            # Let the inverse check classify the committed key
                            # before independent attempt bookkeeping writes.
                            self.assertTrue(owner_return.wait(WAIT))
                        return result

                    with patch.object(f.owner, 'admit', side_effect=observed_admit), patch('fcntl.flock', side_effect=observed), ThreadPoolExecutor(1) as pool:
                        try:
                            if first == 'native':
                                winner = pool.submit(handoff)
                                self.assertTrue(native_entered.wait(WAIT))
                                go.set()
                                self.assertTrue(attempted.wait(WAIT))
                            else:
                                go.set()
                                self.assertTrue(entered.wait(WAIT))
                                winner = pool.submit(handoff)
                                self.assertTrue(native_attempted.wait(WAIT))
                            native_release.set()
                            release.set()
                            self.assertTrue(read.poll(WAIT))
                            managed_result, calls, claims, managed_wrote = read.recv()
                            owner_return.set()
                            native_result = winner.result(timeout=WAIT)
                        finally:
                            native_release.set()
                            owner_return.set()
                            release.set()
                            go.set()
                            child.join(WAIT)
                            if child.is_alive():
                                child.kill()
                                child.join()
                            read.close()
                    self.assertEqual(child.exitcode, 0)
                    self.assertEqual(active.value, 0)
                    self.assertEqual((native_result, managed_result),
                                     ('committed', 'request_conflict') if first == 'native'
                                     else ('request_conflict', 'committed'))
                    self.assertEqual(trace.fired, first == 'native')
                    self.assertEqual(managed_wrote, first == 'managed')
                    self.assertEqual(len(f.calls), int(first == 'native'))
                    self.assertEqual(sum(cmd in ('decision', 'enqueue') for cmd in calls), int(first == 'managed'))
                    with closing(sqlite3.connect(f.fence.registry)) as db:
                        selected = db.execute('SELECT id FROM messages WHERE id=? UNION ALL '
                                              'SELECT id FROM operations WHERE id=?', (expected, expected)).fetchall()
                    self.assertEqual(selected, [(expected,)] if first == 'managed' else [])
                    links = f.owner_db.execute(f'SELECT owner_request_id FROM {LINKS} WHERE owner_request_id=?', (expected,)).fetchall()
                    self.assertEqual(links, [(expected,)] if first == 'native' else [])
                    trace.armed = False
                    if kind != 'new_chat':
                        unrelated = str(uuid.uuid4())
                        f.managed.action(kind, run, unrelated, str(uuid.uuid4()))
                        with closing(sqlite3.connect(f.fence.registry)) as db:
                            table = 'operations' if kind == 'decide' else 'messages'
                            self.assertEqual(db.execute(f'SELECT id FROM {table} WHERE id=?', (unrelated,)).fetchone(), (unrelated,))


def _manual_bind(scheduler, *args, **kwargs):
    from attachments_owner_runtime import bind_owner_runtime
    return bind_owner_runtime(*args, scheduler=scheduler, **kwargs)


if __name__ == '__main__':
    unittest.main()
