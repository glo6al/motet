"""Cross-process exclusion proof with stock SQLite and fictional runner calls."""
from contextlib import ExitStack, closing, nullcontext
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_owner_guards import LINKS, OUTCOMES
from attachments_owner_transport import RETENTION_MS
from native_managed_namespace_fixture import (
    AUTHORITY, HOST_LOCK, MANAGED_EXPECTED, MANAGED_IDENTITY, MANAGED_ROOT, MANAGED_WHY,
    OWNER_DB, OWNER_EXPECTED, OWNER_IDENTITY, REGISTRY, Adapter, ManagedError, ManagedFixture, Namespace,
    bootstrap, checked_fd, native_action, native_store,
)
from test_attachments_owner_guards import admission, uid


WAIT = 12
CONFLICT = ('refused', 'request_conflict')
UNAVAILABLE = ('refused', 'managed_unavailable')
AGENT = 'x:00000000-0000-0000-0000-000000000001'
ROLE = {'native': 1, 'managed': 2}


def captured(call):
    try:
        outcome = call()
        return ('ok', outcome if isinstance(outcome, str) else getattr(outcome, 'outcome', None)
                or outcome.get('state'))
    except ManagedError as error:
        return ('refused', error.code)


class ConnectionProbe:
    """Observe actual writer boundaries and optionally fault a reservation commit."""
    def __init__(self, *, role, active, entered=None, release=None, fault=None):
        self.role = role
        self.active = active
        self.entered = entered
        self.release = release
        self.fault = fault
        self.armed = False
        self.fired = False
        self.read_sql = []
        self.connect_original = sqlite3.connect

    def connect(self, database, *args, **kwargs):
        trace = self
        main = Path(str(database)).name
        target = OWNER_DB if self.role == 'native' else REGISTRY
        if kwargs.get('uri'):
            db = self.connect_original(database, *args, **kwargs)
            self.read_sql.append((str(database), db.execute('PRAGMA query_only').fetchone()))
            db.set_trace_callback(lambda sql: self.read_sql.append(sql))
            return db
        if main != target:
            return self.connect_original(database, *args, **kwargs)

        class Connection(sqlite3.Connection):
            tracked = False
            write = False

            def execute(self, sql, *params):
                if trace.armed and sql == 'BEGIN IMMEDIATE':
                    with trace.active.get_lock():
                        assert trace.active.value == 0, 'Two actual SQLite writers overlapped.'
                        result = super().execute(sql, *params)
                        assert self.in_transaction
                        trace.active.value = ROLE[trace.role]
                        self.tracked = True
                        return result
                result = super().execute(sql, *params)
                if self.tracked and sql.startswith(('INSERT INTO owner_attachment_links',
                                                    'INSERT INTO messages', 'INSERT INTO operations')):
                    self.write = True
                return result

            def release_slot(self):
                if self.tracked:
                    with trace.active.get_lock():
                        assert trace.active.value == ROLE[trace.role] and not self.in_transaction
                        trace.active.value = 0
                    self.tracked = False

            def commit(self):
                if self.tracked and self.write and not trace.fired:
                    trace.fired = True
                    if trace.entered is not None:
                        trace.entered.set()
                        assert trace.release.wait(WAIT), 'Commit gate was never released.'
                    if trace.fault == 'before':
                        raise sqlite3.OperationalError('Fictional precommit loss.')
                    super().commit()
                    self.release_slot()
                    if trace.fault == 'after':
                        raise sqlite3.OperationalError('Fictional postcommit loss.')
                    if trace.fault == 'exit':
                        os._exit(27)  # Commit survived. No handler, response, or runner I/O follows.
                    return
                super().commit()
                self.release_slot()

            def rollback(self):
                super().rollback()
                self.release_slot()

            def close(self):
                if self.tracked:
                    self.rollback()
                super().close()

        kwargs['factory'] = Connection
        return self.connect_original(database, *args, **kwargs)


def participant(root, role, kind, operation, run_id, permission, active, ready, go, attempted,
                entered, release, fault, send):
    root = Path(root)
    namespace = Namespace(root)
    trace = ConnectionProbe(role=role, active=active, entered=entered, release=release, fault=fault)
    fixture = None
    import fcntl
    real_flock = fcntl.flock

    def watched_lock(fd, flags):
        with checked_fd(root / HOST_LOCK) as (_, identity):
            st = os.fstat(fd)
            if [st.st_dev, st.st_ino] == identity:
                attempted.set()
        return real_flock(fd, flags)

    with send, patch('sqlite3.connect', side_effect=trace.connect), ExitStack() as stack:
        if role == 'native':
            store, ledger = stack.enter_context(native_store(root, namespace))
            chosen = admission(AGENT, owner_request_id=operation)
            run = lambda: namespace.admit(store, ledger, chosen, int(time.time() * 1000))
        else:
            fixture = ManagedFixture(root, namespace)
            if kind == 'create':
                run = lambda: fixture.manager.reserve(fixture.folder, operation)
            else:
                run = lambda: fixture.action(kind, run_id, operation, permission)
        trace.armed = True
        ready.set()
        assert go.wait(WAIT), 'Worker did not receive its start gate.'
        with patch('fcntl.flock', side_effect=watched_lock):
            outcome = captured(run)
        send.send((outcome, fixture.calls if fixture else [], fixture.claims if fixture else [],
                   fixture.turns if fixture else [], trace.fired))


# These race managed Claude (a real ManagedClaude with a fictional runner) against the owner through the fixture's own
# POSIX lock proof. Managed Claude is Mac-only by design, so on Windows they skip; the product fence's Windows
# branches are tested in test_attachments_windows_namespace.py.
@unittest.skipIf(MANAGED_WHY, MANAGED_WHY)
class NamespaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.namespace = Namespace(self.root)
        bootstrap(self.root, self.namespace)
        self.context = multiprocessing.get_context('spawn')

    def case(self):
        case = Path(tempfile.mkdtemp(dir=self.root)).resolve()
        namespace = Namespace(case)
        bootstrap(case, namespace)
        return case, namespace

    def worker(self, *, role, kind=None, root=None, operation, run_id=None, permission=None,
               active, gated=False, fault=None):
        ctx = self.context
        ready, go, attempted = (ctx.Event() for _ in range(3))
        entered, release = (ctx.Event(), ctx.Event()) if gated else (None, None)
        receive, send = ctx.Pipe(duplex=False)
        process = ctx.Process(target=participant, args=(
            str(root or self.root), role, kind, operation, run_id, permission, active, ready, go,
            attempted, entered, release, fault, send))
        process.start()
        send.close()

        def shutdown():
            if release is not None:
                release.set()
            go.set()
            process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(3)
            receive.close()
            # Retain the shared Value until the spawned process has attached.
            self.assertEqual(active.value, 0)

        self.addCleanup(shutdown)
        return process, ready, go, attempted, entered, release, receive

    def result(self, worker):
        process, *_, pipe = worker
        self.assertTrue(pipe.poll(WAIT), 'Worker did not complete under the watchdog.')
        outcome = pipe.recv()
        process.join(WAIT)
        self.assertEqual(process.exitcode, 0)
        return outcome

    def present(self, root, table, request_id, *, managed=False):
        path = root / MANAGED_ROOT / REGISTRY if managed else root / OWNER_DB
        key = 'id' if managed or table == 'requests' else 'owner_request_id'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute(f'SELECT 1 FROM {table} WHERE {key}=?', (request_id,)).fetchone() is not None

    def assert_parent_lock_held(self, root):
        import fcntl
        with checked_fd(root / HOST_LOCK) as (fd, _):
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_process_orderings_for_enqueue_decide_and_new_chat(self):
        for kind in ('enqueue', 'decide', 'new_chat'):
            for first in ('native', 'managed'):
                with self.subTest(kind=kind, first=first):
                    root, namespace = self.case()
                    manager = ManagedFixture(root, namespace)
                    run_id = manager.start()
                    request = Adapter.first_request_id(run_id) if kind == 'new_chat' else uid()
                    permission = uid()
                    active = self.context.Value('i', 0)
                    second = 'native' if first == 'managed' else 'managed'
                    left, right = (self.worker(role=role, kind=kind, root=root, operation=request, run_id=run_id,
                                               permission=permission, active=active, gated=role == first)
                                   for role in (first, second))
                    self.assertTrue(left[1].wait(WAIT) and right[1].wait(WAIT))
                    left[2].set()
                    self.assertTrue(left[4].wait(WAIT))
                    self.assertEqual(active.value, ROLE[first])
                    self.assert_parent_lock_held(root)
                    right[2].set()
                    self.assertTrue(right[3].wait(WAIT))
                    if second == 'native':
                        self.assertEqual(self.result(right), (UNAVAILABLE, [], [], [], False))
                    left[5].set()
                    winning, calls, claims, turns, wrote = self.result(left)
                    self.assertEqual(winning[0], 'ok')
                    self.assertTrue(wrote)
                    table = 'operations' if kind == 'decide' else 'messages'
                    if first == 'native':
                        losing, calls, _, _, wrote = self.result(right)
                        self.assertEqual(losing, CONFLICT)
                        self.assertEqual(calls, [])
                        self.assertFalse(wrote)
                        self.assertTrue(self.present(root, LINKS, request))
                        self.assertFalse(self.present(root, table, request, managed=True))
                    else:
                        self.assertEqual(calls.count('decision' if kind == 'decide' else 'enqueue'), 1)
                        if kind == 'new_chat':
                            self.assertEqual(claims, [request])
                            self.assertEqual(turns, [request])
                        self.assertTrue(self.present(root, table, request, managed=True))
                        self.assertFalse(self.present(root, LINKS, request))
                        self.assertEqual(captured(lambda: native_action(root, namespace, request)), CONFLICT)
                    self.assertEqual(active.value, 0)
                    self.assertEqual(captured(lambda: native_action(root, namespace, uid()))[0], 'ok')
                    self.assertEqual(captured(lambda: manager.action('enqueue', run_id, uid()))[0], 'ok')

    def test_real_process_commit_loss_both_sides(self):
        for kind in ('native', 'enqueue', 'decide'):
            for fault in ('before', 'after'):
                with self.subTest(kind=kind, fault=fault):
                    root, namespace = self.case()
                    manager = ManagedFixture(root, namespace)
                    run_id, request, permission = manager.start(), uid(), uid()
                    native = kind == 'native'
                    worker = self.worker(role='native' if native else 'managed', kind=kind, root=root,
                                         operation=request, run_id=run_id, permission=permission,
                                         active=self.context.Value('i', 0), fault=fault)
                    self.assertTrue(worker[1].wait(WAIT))
                    worker[2].set()
                    self.assertEqual(self.result(worker), (UNAVAILABLE, [], [], [], True))
                    if native:
                        self.assertEqual(self.present(root, LINKS, request), fault == 'after')
                        if fault == 'after':
                            self.assertEqual(captured(lambda: manager.action('enqueue', run_id, request)), CONFLICT)
                            self.assertEqual(captured(lambda: manager.action('decide', run_id, request, permission)), CONFLICT)
                            with native_store(root, namespace) as (_, ledger):
                                self.assertEqual(ledger.status(request)['state'], 'owner_recorded')
                        else:
                            self.assertEqual(captured(lambda: manager.action('enqueue', run_id, request))[0], 'ok')
                    else:
                        table = 'messages' if kind == 'enqueue' else 'operations'
                        self.assertEqual(self.present(root, table, request, managed=True), fault == 'after')
                        if fault == 'after':
                            before = list(manager.calls)
                            self.assertEqual(captured(lambda: native_action(root, namespace, request)), CONFLICT)
                            self.assertEqual(captured(lambda: manager.action(kind, run_id, request, permission)),
                                             ('ok', 'unknown'))
                            self.assertEqual(manager.calls, before)
                        else:
                            self.assertEqual(captured(lambda: native_action(root, namespace, request))[0], 'ok')

    def test_manager_replay_preserves_exact_binding_and_unknown(self):
        manager = ManagedFixture(self.root, self.namespace)
        run, request, permission = manager.start(), uid(), uid()
        self.assertEqual(captured(lambda: manager.action('decide', run, request, permission)), ('ok', 'written'))
        calls = list(manager.calls)
        self.assertEqual(captured(lambda: manager.action('decide', run, request, permission)), ('ok', 'written'))
        self.assertEqual(captured(lambda: manager.action('decide', run, request, permission, 'deny')), CONFLICT)
        self.assertEqual(manager.calls, calls)
        request = uid()
        manager.request_failure = True
        self.assertEqual(captured(lambda: manager.action('enqueue', run, request)), ('ok', 'unknown'))
        self.assertEqual(manager.calls.count('enqueue'), 1)
        reopened = ManagedFixture(self.root, self.namespace)
        self.assertEqual(captured(lambda: reopened.action('enqueue', run, request)), ('ok', 'unknown'))
        self.assertEqual(captured(lambda: native_action(self.root, self.namespace, request)), CONFLICT)
        self.assertEqual(reopened.calls, [])

    def test_process_exit_after_commit_requires_exact_recovery(self):
        for kind in ('native', 'enqueue', 'decide'):
            with self.subTest(kind=kind):
                root, namespace = self.case()
                fixture = ManagedFixture(root, namespace)
                run, request, permission = fixture.start(), uid(), uid()
                native = kind == 'native'
                active = self.context.Value('i', 0)
                process, ready, go, *_, pipe = self.worker(
                    role='native' if native else 'managed', kind=kind, root=root, operation=request,
                    run_id=run, permission=permission, active=active, fault='exit')
                self.assertTrue(ready.wait(WAIT))
                go.set()
                self.assertTrue(pipe.poll(WAIT))
                with self.assertRaises(EOFError):
                    pipe.recv()
                process.join(WAIT)
                self.assertEqual(process.exitcode, 27)
                self.assertEqual(active.value, 0)
                reopened = ManagedFixture(root, Namespace(root))
                if native:
                    with native_store(root, Namespace(root)) as (_, ledger):
                        self.assertEqual(ledger.status(request)['state'], 'owner_recorded')
                    self.assertEqual(captured(lambda: reopened.action('enqueue', run, request)), CONFLICT)
                else:
                    self.assertEqual(captured(lambda: native_action(root, Namespace(root), request)), CONFLICT)
                    self.assertEqual(captured(lambda: reopened.action(kind, run, request, permission)), ('ok', 'unknown'))
                self.assertEqual(reopened.calls, [])

    def test_tombstones_links_and_outcomes_remain_protected_on_reopen(self):
        private = uid()
        now = int(time.time() * 1000) - RETENTION_MS * 2
        mark = native_action(self.root, self.namespace, private, now_ms=now)
        bad = [(LINKS, uid()), (OUTCOMES, uid())]
        with native_store(self.root, self.namespace) as (store, ledger):
            digest = mark['admission_digest']
            claim = ledger.record_claim(private, digest, now_ms=now + 1)['attempt_id']
            result = ledger.record_outcome(private, digest, claim, 'not_submitted',
                                           now_ms=now + 2, evidence_sequence=1)['outcome']
            ledger.acknowledge(private, digest, result['seq'], result['digest'])
            ledger.prune_payload(private, digest, now_ms=now + RETENTION_MS + 1)
            with store.db:
                for table, request in bad:
                    store.db.execute(f'INSERT INTO {table} VALUES (?,zeroblob(4096))', (request,))
        self.assertFalse(self.present(self.root, 'requests', private))
        manager = ManagedFixture(self.root, Namespace(self.root))
        run = manager.start()
        baseline = list(manager.calls)
        for request in [private, *(request for _, request in bad)]:
            for kind in ('enqueue', 'decide'):
                self.assertEqual(captured(lambda: manager.action(kind, run, request, uid())), CONFLICT)
            self.assertFalse(self.present(self.root, 'requests', request))
        self.assertEqual(manager.calls, baseline)
        self.assertTrue(self.present(self.root, LINKS, private) and self.present(self.root, OUTCOMES, private))
        for table, request in bad:
            self.assertTrue(self.present(self.root, table, request))
        self.assertEqual(captured(lambda: manager.action('enqueue', run, uid()))[0], 'ok')

    def test_root_loss_and_empty_registry_replacement_preserve_both_histories(self):
        for kind in ('enqueue', 'decide'):
            for damage in ('root_lost', 'empty_registry'):
                with self.subTest(kind=kind, damage=damage):
                    root, namespace = self.case()
                    owner = ManagedFixture(root, namespace)
                    run, request = owner.start(), uid()
                    self.assertEqual(captured(lambda: owner.action(kind, run, request, uid()))[0], 'ok')
                    table = 'messages' if kind == 'enqueue' else 'operations'
                    self.assertTrue(self.present(root, table, request, managed=True))
                    if damage == 'root_lost':
                        shutil.rmtree(root / MANAGED_ROOT)
                        (root / MANAGED_EXPECTED).unlink()
                    else:
                        replacement, _ = self.case()
                        other = ManagedFixture(replacement, None)
                        other.manager.reserve(other.folder, uid())
                        old = root / MANAGED_ROOT / REGISTRY
                        old.rename(old.with_name('displaced-fixture.sqlite3'))
                        shutil.copyfile(replacement / MANAGED_ROOT / REGISTRY, old)
                        old.chmod(0o600)
                    namespace = Namespace(root)
                    reopened = ManagedFixture(root, namespace)
                    self.assertEqual(captured(lambda: native_action(root, namespace, request)), UNAVAILABLE)
                    self.assertEqual(captured(lambda: reopened.manager.reserve(reopened.folder, uid())), UNAVAILABLE)
                    self.assertEqual(captured(lambda: reopened.action(kind, run, request, uid())), UNAVAILABLE)
                    self.assertFalse(self.present(root, LINKS, request))
                    self.assertEqual(reopened.calls, [])
                    if damage == 'root_lost':
                        self.assertFalse((root / MANAGED_ROOT).exists())
                        self.assertFalse((root / MANAGED_EXPECTED).exists())
                    else:
                        self.assertFalse(self.present(root, table, request, managed=True))

    def test_protected_orphan_and_uncertain_exact_owner_row_do_not_alias(self):
        root = Path(tempfile.mkdtemp(dir=self.root)).resolve()
        namespace = Namespace(root)
        marked, unclear = uid(), uid()
        bootstrap(root, namespace, legacy_rows=[(marked, '{"attachment_owner_version":null}'), (unclear, '{')])
        manager = ManagedFixture(root, namespace)
        run = manager.start()
        for kind in ('enqueue', 'decide'):
            self.assertEqual(captured(lambda: manager.action(kind, run, marked, uid())), CONFLICT)
            self.assertEqual(captured(lambda: manager.action(kind, run, unclear, uid())), UNAVAILABLE)
        self.assertEqual(manager.calls, ['spawn', 'status'])
        self.assertTrue(self.present(root, 'requests', marked))
        self.assertTrue(self.present(root, 'requests', unclear))
        self.assertEqual(captured(lambda: manager.action('enqueue', run, uid()))[0], 'ok')

    def test_first_creation_holds_same_parent_flock_and_preserves_clean_absence(self):
        first = uid()
        self.assertEqual(captured(lambda: native_action(self.root, self.namespace, first))[0], 'ok')
        active = self.context.Value('i', 0)
        # Start the native participant before creation so it has an open real store.
        request = uid()
        native = self.worker(role='native', operation=request, active=active)
        self.assertTrue(native[1].wait(WAIT))
        snapshots = []

        def after_intent(stage):
            if stage == 'creating_after_sync':
                snapshots.append(stage)
                self.assertFalse((self.root / MANAGED_ROOT).exists())
                self.assert_parent_lock_held(self.root)
                native[2].set()
                self.assertTrue(native[3].wait(WAIT))
                self.assertEqual(self.result(native), (UNAVAILABLE, [], [], [], False))

        manager = ManagedFixture(self.root, Namespace(self.root, checkpoint=after_intent))
        run = manager.start()
        self.assertEqual(snapshots, ['creating_after_sync'])
        self.assertFalse(self.present(self.root, LINKS, request))
        self.assertEqual(captured(lambda: manager.action('enqueue', run, first)), CONFLICT)
        self.assertEqual(captured(lambda: manager.action('enqueue', run, request))[0], 'ok')

    def test_managed_creation_crash_checkpoints(self):
        points = {
            'creating_before_append': 'clean',
            'creating_before_sync': 'partial',
            'creating_after_sync': 'partial',
            'registry_schema_committed': 'partial',
            'managed_identity_committed': 'partial',
            'initialized_before_append': 'partial',
            'initialized_before_sync': 'complete',
            'initialized_after_sync': 'complete',
        }
        for stage, kind in points.items():
            with self.subTest(stage=stage):
                root, _ = self.case()

                def crash(checkpoint):
                    if checkpoint == stage:
                        raise OSError('Fictional initialization interruption.')

                missed = ManagedFixture(root, Namespace(root, checkpoint=crash))
                self.assertEqual(captured(lambda: missed.manager.reserve(missed.folder, uid())), UNAVAILABLE)
                self.assertEqual(missed.calls, [])
                registry = root / MANAGED_ROOT / REGISTRY
                if registry.exists():
                    with closing(sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True)) as db:
                        for table in ('runs', 'messages', 'operations'):
                            self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone(), (0,))
                before = (root / AUTHORITY).read_bytes()
                reopened = ManagedFixture(root, Namespace(root))
                request = uid()
                if kind == 'partial':
                    self.assertEqual(captured(lambda: native_action(root, Namespace(root), request)), UNAVAILABLE)
                    self.assertEqual(captured(lambda: reopened.manager.reserve(reopened.folder, uid())), UNAVAILABLE)
                    self.assertEqual((root / AUTHORITY).read_bytes(), before)
                    self.assertFalse(self.present(root, LINKS, request))
                else:
                    self.assertEqual(captured(lambda: native_action(root, Namespace(root), request))[0], 'ok')
                    self.assertEqual(captured(lambda: reopened.manager.reserve(reopened.folder, uid()))[0], 'ok')
                self.assertEqual(reopened.calls, [])

    def test_owner_witness_crash_and_missing_replaced_state(self):
        for stage in ('owner_before_commit', 'owner_after_commit', 'owner_marker_opened',
                      'owner_marker_before_sync', 'owner_marker_before_parent_sync', 'owner_initialized'):
            with self.subTest(stage=stage):
                root = Path(tempfile.mkdtemp(dir=self.root)).resolve()

                def crash(checkpoint):
                    if checkpoint == stage:
                        raise OSError('Fictional owner initialization interruption.')

                with self.assertRaises(OSError):
                    bootstrap(root, Namespace(root, checkpoint=crash))
                namespace = Namespace(root)
                complete = stage in ('owner_marker_before_sync', 'owner_marker_before_parent_sync', 'owner_initialized')
                request = uid()
                result = captured(lambda: native_action(root, namespace, request))
                self.assertEqual(result, ('ok', 'owner_recorded') if complete else UNAVAILABLE)
                self.assertEqual(self.present(root, LINKS, request), complete)
                manager = ManagedFixture(root, namespace)
                result = captured(lambda: manager.manager.reserve(manager.folder, uid()))
                self.assertEqual(result[0] if complete else result, 'ok' if complete else UNAVAILABLE)
                self.assertEqual(manager.calls, [])
        for damaged in ('missing', 'empty', 'nonce', 'marker', 'both'):
            with self.subTest(owner=damaged):
                root, namespace = self.case()
                manager = ManagedFixture(root, namespace)
                run = manager.start()
                self.assertEqual(captured(lambda: native_action(root, namespace, uid()))[0], 'ok')
                if damaged in ('missing', 'empty', 'both'):
                    (root / OWNER_DB).rename(root / 'displaced-owner.sqlite3')
                if damaged == 'empty':
                    replacement, _ = self.case()
                    shutil.copyfile(replacement / OWNER_DB, root / OWNER_DB)
                    (root / OWNER_DB).chmod(0o600)
                if damaged == 'nonce':
                    with closing(sqlite3.connect(root / OWNER_DB)) as db, db:
                        db.execute(f'UPDATE {OWNER_IDENTITY} SET nonce=?', ('0' * 64,))
                if damaged in ('marker', 'both'):
                    (root / OWNER_EXPECTED).unlink()
                request = uid()
                self.assertEqual(captured(lambda: native_action(root, Namespace(root), request)), UNAVAILABLE)
                self.assertEqual(captured(lambda: manager.action('enqueue', run, request)), UNAVAILABLE)
                self.assertEqual(captured(lambda: manager.action('decide', run, request, uid())), UNAVAILABLE)
                self.assertEqual(manager.calls, ['spawn', 'status'])
                if damaged in ('missing', 'both'):
                    self.assertFalse((root / OWNER_DB).exists())

    def test_unprovable_managed_authority_or_layout_never_certifies_absence(self):
        for damaged in ('archive', 'authority_lost', 'authority_replaced', 'authority_partial', 'marker',
                        'marker_replacement', 'symlink', 'layout', 'nonce', 'timeout'):
            with self.subTest(managed=damaged):
                root, namespace = self.case()
                manager = ManagedFixture(root, namespace)
                run = manager.start()
                registry = root / MANAGED_ROOT / REGISTRY
                expected = root / MANAGED_EXPECTED
                authority = root / AUTHORITY
                if damaged == 'archive':
                    tree = root / MANAGED_ROOT
                    tree.rename(tree.with_name('moved-managed'))
                    shutil.copytree(tree.with_name('moved-managed'), tree)
                if damaged in ('authority_lost', 'authority_replaced'):
                    authority.rename(root / 'displaced-authority')
                    if damaged == 'authority_replaced':
                        shutil.copyfile(root / 'displaced-authority', authority)
                        authority.chmod(0o600)
                if damaged == 'authority_partial':
                    with authority.open('ab') as file:
                        file.write(b'{"state":')
                if damaged == 'marker':
                    expected.unlink()
                if damaged == 'marker_replacement':
                    expected.rename(root / 'displaced-expected')
                    expected.write_bytes(b'1\n')
                    expected.chmod(0o600)
                if damaged == 'symlink':
                    registry.rename(registry.with_name('real-registry.sqlite3'))
                    registry.symlink_to(registry.with_name('real-registry.sqlite3'))
                if damaged in ('layout', 'nonce'):
                    with closing(sqlite3.connect(registry)) as db, db:
                        if damaged == 'layout':
                            db.execute('CREATE UNIQUE INDEX questionable ON messages(digest)')
                        else:
                            db.execute(f'UPDATE {MANAGED_IDENTITY} SET nonce=?', ('0' * 64,))
                open_lock = closing(sqlite3.connect(registry)) if damaged == 'timeout' else nullcontext()
                with open_lock as db:
                    if db is not None:
                        db.execute('BEGIN EXCLUSIVE')
                    self.assertEqual(captured(lambda: native_action(root, Namespace(root), uid())), UNAVAILABLE)
                self.assertEqual(manager.calls, ['spawn', 'status'])
                self.assertFalse((root / OWNER_DB).with_name(OWNER_DB + '-journal').exists())

    def test_read_only_exact_indexed_probes_and_default_adapter_absence(self):
        manager = ManagedFixture(self.root, self.namespace)
        run = manager.start()
        trace = ConnectionProbe(role='managed', active=self.context.Value('i', 0))
        request = uid()
        with patch('sqlite3.connect', side_effect=trace.connect):
            self.assertEqual(captured(lambda: native_action(self.root, self.namespace, request))[0], 'ok')
            self.assertEqual(captured(lambda: manager.action('enqueue', run, request)), CONFLICT)
        urls = [row[0] for row in trace.read_sql if type(row) is tuple]
        joined = '\n'.join(row for row in trace.read_sql if type(row) is str)
        self.assertTrue(any(url.endswith(REGISTRY + '?mode=ro') for url in urls))
        self.assertTrue(any(url.endswith(OWNER_DB + '?mode=ro') for url in urls))
        self.assertIn("SELECT 1 FROM main.messages WHERE id='" + request + "'", joined)
        self.assertIn("SELECT 1 FROM main.operations WHERE id='" + request + "'", joined)
        self.assertNotIn('BEGIN IMMEDIATE', joined)
        with closing(sqlite3.connect((self.root / MANAGED_ROOT / REGISTRY).as_uri() + '?mode=ro', uri=True)) as db:
            for table in ('messages', 'operations'):
                explain = db.execute(f'EXPLAIN QUERY PLAN SELECT 1 FROM main.{table} WHERE id=?', (request,)).fetchall()
                self.assertTrue(any('SEARCH' in row[3] and 'INDEX' in row[3] for row in explain), explain)
        self.assertEqual(manager.calls, ['spawn', 'status'])
        plain = ManagedFixture(self.root, None)
        self.assertIsNone(plain.manager._test.namespace)
        other = uid()
        self.assertEqual(captured(lambda: plain.action('enqueue', run, other))[0], 'ok')
        self.assertEqual(plain.calls.count('enqueue'), 1)


if __name__ == '__main__':
    unittest.main()
