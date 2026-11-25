"""L2c owner startup and maintenance on disposable SQLite, without adapters."""
import ast
import builtins
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import attachments_owner_directory as directories
import attachments_owner_guards as guards
import attachments_owner_runtime as runtime
import attachments_owner_transport as owner
import officer_messaging as messaging
import peer_bridge
import peer_lifecycle
import server
from test_attachments_owner_guards import admission, insert, install, one_thread


def uid():
    return str(uuid.uuid4())


class FaultConnection(sqlite3.Connection):
    fault_execute = None
    fault_commit = None
    after_commit = None

    def execute(self, sql, parameters=()):
        if self.fault_execute is not None:
            self.fault_execute(sql)
        return super().execute(sql, parameters)

    def commit(self):
        mode, self.fault_commit = self.fault_commit, None
        if mode == 'before':
            raise sqlite3.OperationalError('Fictional commit failure.')
        super().commit()
        callback, self.after_commit = self.after_commit, None
        if callback is not None:
            callback()
        if mode == 'after':
            raise sqlite3.OperationalError('Fictional lost commit response.')


class ManualWorker:
    def __init__(self, checkpoint, stopped):
        self.checkpoint, self.stopped = checkpoint, stopped
        self.starts, self.joins = 0, 0

    def start(self):
        self.starts += 1

    def is_alive(self):
        return self.starts > 0 and self.joins == 0

    def join(self):
        self.joins += 1


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.agent = 'x:' + uid()
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Orchard'}}
        transcript = self.sessions / f'rollout-{self.agent[2:]}.jsonl'
        transcript.write_text(json.dumps(dict(type='session_meta', payload=dict(
            id=self.agent[2:], originator='Codex Desktop', source='vscode'))) + '\n')
        self.workers = []
        self.store = None
        self.open_store('state')
        self.addCleanup(self.close_store)
        # If this slice reaches a real transport or native command, fail the test.
        for name in ('_send', '_poll', 'probe', 'start'):
            fault = patch.object(messaging.MessagingStore, name, side_effect=AssertionError(name))
            fault.start()
            self.addCleanup(fault.stop)

    def open_store(self, name):
        self.close_store()
        self.path = self.root / name
        connect = sqlite3.connect
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **k: connect(*a, factory=FaultConnection, **k)):
            self.store = messaging.MessagingStore(str(self.path), str(self.sessions))
        self.store.available = True
        self.db_path = self.path / 'officer-messages.sqlite3'

    def close_store(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def scheduler(self, checkpoint, stopped):
        worker = ManualWorker(checkpoint, stopped)
        self.workers.append(worker)
        return worker

    def bind(self, allow_install=True):
        return runtime.bind_owner_runtime(self.store, allow_install=allow_install, scheduler=self.scheduler)

    def server_bind(self, *, master, native, officer, service, demo=False):
        with ExitStack() as stack:
            for attr, value in (('ATTACHMENTS_ENABLED', master), ('NATIVE_AGENT_ATTACHMENTS_ENABLED', native),
                                ('OFFICER_DOCUMENTS_ENABLED', officer)):
                stack.enter_context(patch.object(server, attr, value))
            stack.enter_context(patch.dict(os.environ, {'WATCH_DEMO': '1' if demo else '0'}))
            bind = runtime.bind_owner_runtime
            stack.enter_context(patch.object(runtime, 'bind_owner_runtime',
                                            side_effect=lambda store, **kw: bind(store, scheduler=self.scheduler, **kw)))
            return server.start_native_attachment_maintenance(self.store, service)

    def catalog(self, db=None):
        db = self.store.db if db is None else db
        return db.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'owner_attachment_%' ORDER BY name").fetchall()

    def counts(self, instance):
        with instance.ledger._transaction():
            return instance.ledger.directory.counts(int(time.time() * 1000))

    def submit(self, text=None):
        return self.store.submit(self.agent, text or uid(), uid(), self.agents)

    def ordinary_row(self, *, age=120, **fields):
        identity = uid()
        return dict(dict(request_id=identity, agent=self.agent, state='answered', question=identity,
                         created=time.time() - age, launched=True, reply='', runtime='codex'), **fields)

    def save(self, row):
        with self.store.lock:
            self.store._save(row)

    def retained_peer(self, execution, *, age=120):
        launched = execution != 'dropped'
        row = self.ordinary_row(age=age, origin='peer', bridge_version=2, launched=launched,
                                state='unknown' if launched else 'failed', error='Fictional peer outcome.')
        if execution == 'completed':
            row.update(settled=True, bound_turn='fictional-turn', capture_disposition='withheld')
        identity = row['request_id']
        with self.store.db:
            insert(self.store.db, 'shared_bridge_requests', (uid(), self.agent, '{}', 'hash', identity,
                   'fictional', row['created'], row['created'] + 1 if launched else None, 'preview'))
            insert(self.store.db, 'requests', (identity, json.dumps(row)))
            if execution == 'retired':
                peer_lifecycle.retire(self.store.db, identity, peer_lifecycle.RETIRE_REASON)
            else:
                peer_lifecycle.reconcile(self.store.db, row)
        self.save(row)
        self.assertEqual(peer_lifecycle.get(self.store.db, identity)['execution'], execution)
        return identity

    def installed_fixture(self, tombstones=0, *, pending=False):
        # The L1 offline API produces real validated fixed records in a disposable DB.
        offline = owner.OwnerAttachmentLedger(self.store.db, self.store.lock)
        initial = int(time.time() * 1000) - 20 * owner.RETENTION_MS
        pruned = []
        for index in range(tombstones):
            now = initial + index * 60001
            attempt = admission(self.agent)
            identity = attempt.owner_request_id
            digest = offline.admit(attempt, now_ms=now)['admission_digest']
            claim = offline.record_claim(identity, digest, now_ms=now + 1)['attempt_id']
            outcome = offline.record_outcome(identity, digest, claim, 'not_submitted', now_ms=now + 2,
                                             evidence_sequence=1)['outcome']
            offline.acknowledge(identity, digest, outcome['seq'], outcome['digest'])
            offline.prune_payload(identity, digest, now_ms=now + owner.RETENTION_MS + 1)
            pruned.append(identity)
        retained = None
        if pending:
            attempt = admission(self.agent)
            retained = attempt.owner_request_id
            offline.admit(attempt, now_ms=initial + tombstones * 60001)
        current = self.path.name
        install(self.store.db)
        self.open_store(current)
        return pruned, retained

    def test_fresh_off_and_demo_return_before_optional_module_import(self):
        previous = self.store.db.execute('PRAGMA schema_version').fetchone()
        load = builtins.__import__

        def deny_native(name, *args, **kwargs):
            if name in ('attachments_owner_runtime', 'attachments_owner_transport', 'attachments_owner_directory'):
                raise AssertionError('Native ledger was imported by an inactive factory.')
            return load(name, *args, **kwargs)

        with patch.object(builtins, '__import__', side_effect=deny_native):
            for officer in (False, True):
                for master, native, service, demo in ((False, True, object(), False),
                                                      (True, False, object(), False),
                                                      (True, True, None, False),
                                                      (True, True, object(), True)):
                    self.assertIsNone(self.server_bind(master=master, native=native, officer=officer,
                                                       service=service, demo=demo))
        self.assertEqual(self.store.db.execute('PRAGMA schema_version').fetchone(), previous)
        self.assertEqual(self.catalog(), [])
        self.assertEqual(self.workers, [])
        self.installed_fixture()
        prior = self.catalog()
        with patch.object(builtins, '__import__', side_effect=deny_native):
            self.assertIsNone(self.server_bind(master=True, native=True, officer=True, service=object(), demo=True))
        self.assertEqual(self.catalog(), prior)
        self.assertIsNone(self.store._attachment_guard.capacity)
        self.assertEqual(self.workers, [])

    def test_first_install_starts_no_native_mutations_and_stops_once(self):
        if not server.NATIVE_ATTACHMENT_DELIVERY_HOST:
            # Windows: delivery to agent chats is not ported, so the server installs nothing with every switch on.
            self.assertIsNone(self.server_bind(master=True, native=True, officer=False, service=object()))
            self.assertEqual((self.catalog(), self.workers), ([], []))
            self.assertIsNone(self.store._attachment_guard)
            return
        instance = self.server_bind(master=True, native=True, officer=False, service=object())
        self.assertEqual(self.workers[0].starts, 1)
        self.assertIs(self.bind(), instance)
        instance.start()
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.workers[0].starts, 1)
        self.assertEqual([name for kind, name, _ in self.catalog() if kind == 'table'],
                         sorted((guards.LINKS, guards.OUTCOMES, guards.PERMIT)))
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        self.workers[0].checkpoint()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))
        ledger, exact = instance.ledger, uid()
        for action in (lambda: ledger.admit(admission(self.agent), now_ms=1),
                       lambda: ledger.record_claim(exact, 'a' * 64, now_ms=1),
                       lambda: ledger.record_outcome(exact, 'a' * 64, uid(), 'not_submitted', evidence_sequence=1, now_ms=1),
                       lambda: ledger.acknowledge(exact, 'a' * 64, 1, 'a' * 64),
                       lambda: ledger.prune_payload(exact, 'a' * 64, now_ms=1),
                       ledger.pending_outcomes):
            with self.assertRaisesRegex(owner.LedgerError, 'owner_ledger_maintenance_only'):
                action()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))
        self.store.stop_native_attachment_runtime()
        self.store.stop_native_attachment_runtime()
        self.assertIsNone(self.bind())
        self.assertEqual(instance.state, 'stopped')
        self.assertEqual(self.workers[0].joins, 1)
        self.assertEqual(self.store.db.execute('SELECT 1').fetchone(), (1,))

    def test_installed_off_rebuilds_in_64_id_steps_through_ordinary_writes(self):
        tombstones, protected = self.installed_fixture(130, pending=True)
        before = self.catalog()
        with patch.object(owner.OwnerAttachmentLedger, 'rebuild_directory', side_effect=AssertionError('eager rebuild')):
            instance = self.server_bind(master=False, native=False, officer=True, service=None)
        self.assertIsNotNone(instance)
        self.assertEqual(self.catalog(), before)
        self.assertEqual(instance.ledger.status(protected)['state'], 'owner_recorded')
        directory = instance.ledger.directory
        tick = self.workers[0].checkpoint
        with patch.object(owner.OwnerAttachmentLedger, 'decode_pair', wraps=owner.OwnerAttachmentLedger.decode_pair) as decode:
            previous = 0
            for expected in (64, 128, 131):
                tick()
                self.assertEqual(decode.call_count - previous, min(64, expected - previous))
                previous = expected
                if expected != 131:
                    self.assertEqual(len(directory._building.entries), expected)
                    aged = self.ordinary_row(age=messaging.RETENTION + 2)
                    self.save(aged)
                    for _ in range(3):
                        self.save(self.ordinary_row())
                    with self.store.lock:
                        self.store._prune(self.store._rows(), time.time())
                    self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?',
                                                            (aged['request_id'],)).fetchone())
                    self.assertTrue(directory.building)
        self.assertEqual(self.counts(instance), (131, 1, 1, 0))
        self.assertTrue(self.store._attachment_reserved(tombstones[0]))
        for _ in range(7):
            self.assertEqual(self.submit()['state'], 'sending')
        with self.assertRaises(messaging.MessagingError) as refused:
            self.submit()
        self.assertEqual(refused.exception.status, 429)
        self.assertEqual(self.counts(instance), (131, 1, 1, 0))
        self.assertEqual(instance.ledger.status(protected)['state'], 'owner_recorded')

    def test_atomic_create_rolls_back_each_setup_write_and_validation(self):
        writes = ('CREATE TABLE ' + guards.LINKS, 'CREATE TABLE ' + guards.OUTCOMES,
                  'CREATE TABLE ' + guards.PERMIT, 'INSERT INTO ' + guards.PERMIT,
                  *('CREATE TRIGGER ' + name + ' ' for name in guards._trigger_definitions()))
        for index, prefix in enumerate(writes):
            with self.subTest(prefix=prefix):
                self.open_store(f'fault-{index}')
                db = self.store.db
                with closing(sqlite3.connect(self.db_path)) as independent:
                    observed = []

                    def fail(sql):
                        if sql.startswith(prefix):
                            observed.append(self.catalog(independent))
                            raise sqlite3.OperationalError('Fictional setup fault.')

                    db.fault_execute = fail
                    self.assertIsNone(self.bind())
                    self.assertEqual(observed, [[]])
                    self.assertEqual(self.catalog(independent), [])
                self.assertEqual(self.catalog(), [])
                self.assertIsNone(self.store._attachment_guard)
                self.assertFalse(db.in_transaction)
        self.open_store('validation-fault')
        with patch.object(guards, '_validate_layout', side_effect=guards.OwnerGuardError('Fictional validation fault.')):
            self.assertIsNone(self.bind())
        self.assertEqual(self.catalog(), [])
        self.assertEqual(self.workers, [])

    def test_commit_fault_reinspection_does_not_restart_installation(self):
        for mode in ('before', 'after'):
            with self.subTest(mode=mode):
                self.open_store(f'commit-{mode}')
                self.store.db.fault_commit = mode
                self.assertIsNone(self.bind())
                self.assertEqual(self.store._native_owner_error, 'setup_unavailable')
                self.assertFalse(self.store.db.in_transaction)
                self.assertEqual(bool(self.catalog()), mode == 'after')
                self.assertEqual(self.store._attachment_guard is not None, mode == 'after')
                if mode == 'after':
                    with self.assertRaises(messaging.MessagingError) as no_capacity:
                        self.submit()
                    self.assertEqual(no_capacity.exception.status, 503)
                else:
                    self.assertEqual(self.submit()['state'], 'sending')
        self.assertEqual(self.workers, [])
        self.open_store('commit-after')
        existing = self.bind(allow_install=False)
        self.assertIsNotNone(existing)
        self.workers[0].checkpoint()
        self.assertEqual(self.submit()['state'], 'sending')

    def test_partial_or_altered_installed_layout_is_never_repaired(self):
        for label in ('partial', 'permit', 'trigger', 'unique'):
            with self.subTest(label=label):
                path = f'bad-{label}'
                self.open_store(path)
                db = self.store.db
                if label == 'partial':
                    db.execute(f'CREATE TABLE {guards.LINKS} ({guards.RECORD})')
                else:
                    owner.OwnerAttachmentLedger(db, self.store.lock)
                    install(db)
                    if label == 'permit':
                        db.execute(f"UPDATE {guards.PERMIT} SET verb='I'")
                    elif label == 'trigger':
                        db.execute(f'DROP TRIGGER {guards.TRIGGER_PREFIX}requests_insert')
                    else:
                        db.execute('CREATE UNIQUE INDEX fictional_extra_index ON requests(data)')
                db.commit()
                original = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
                with self.assertRaises(guards.OwnerGuardError):
                    self.open_store(path)
                with closing(sqlite3.connect(self.db_path)) as reopened:
                    self.assertEqual(reopened.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(), original)
                    if label == 'permit':
                        self.assertEqual(reopened.execute(f'SELECT verb FROM {guards.PERMIT}').fetchone(), ('I',))
                self.assertEqual(self.workers, [])

    def test_unknown_and_inherited_writers_never_rebase_stale_counts(self):
        instance = self.bind()
        db, tick = self.store.db, self.workers[0].checkpoint
        tick()
        mutations = (
            lambda: db.execute('INSERT INTO expired_controls VALUES (?)', (uid(),)),
            lambda: db.execute('CREATE TABLE fictional_schema_change (id TEXT)'),
            lambda: db.execute('CREATE TEMP TABLE fictional_temp_schema_change (id TEXT)'),
            lambda: db.execute('INSERT INTO asks VALUES (?,?,?,NULL)', (uid(), 'Fictional ask.', time.time())),
        )
        for mutate in mutations:
            with db:
                mutate()
            self.save(self.ordinary_row())
            with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
                self.counts(instance)
            tick()
            self.assertEqual(self.counts(instance), (0, 0, 0, 0))
        with closing(sqlite3.connect(self.db_path)) as independent, independent:
            independent.execute('INSERT INTO expired_controls VALUES (?)', (uid(),))
        self.save(self.ordinary_row())
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO expired_controls VALUES (?)', (uid(),))
        self.save(self.ordinary_row())  # Inherited writer. Existing caller commits it.
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))

    def test_temp_ddl_inside_an_ordinary_writer_prevents_rebase(self):
        instance = self.bind()
        db, tick = self.store.db, self.workers[0].checkpoint
        tick()
        initial = db.total_changes
        original = self.store._write_request

        def add_temp_schema(row, proof):
            original(row, proof)
            db.execute('CREATE TEMP TABLE fictional_inside_writer (id TEXT)')

        with patch.object(self.store, '_write_request', side_effect=add_temp_schema):
            self.save(self.ordinary_row())
        self.assertEqual(db.total_changes - initial, 1)
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))

    def test_external_commit_between_writer_commit_and_rebase_is_not_blessed(self):
        instance = self.bind()
        tick, db = self.workers[0].checkpoint, self.store.db
        tick()
        with closing(sqlite3.connect(self.db_path)) as independent:
            def interrupt():
                with independent:
                    independent.execute('INSERT INTO expired_controls VALUES (?)', (uid(),))
            db.after_commit = interrupt
            self.save(self.ordinary_row())
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))

    def test_last_batch_commit_cannot_publish_across_temp_ddl(self):
        self.installed_fixture(65)
        instance = self.bind(allow_install=False)
        directory, tick, db = instance.ledger.directory, self.workers[0].checkpoint, self.store.db
        tick()
        self.assertTrue(directory.building)
        db.after_commit = lambda: db.execute('CREATE TEMP TABLE fictional_after_batch (id TEXT)')
        tick()
        self.assertEqual(instance.state, 'recovery_required')
        self.assertFalse(directory.ready)
        self.assertFalse(directory.building)
        tick()
        tick()
        self.assertEqual(self.counts(instance), (65, 0, 0, 0))

    def test_mixed_peer_and_unexpected_trigger_writes_invalidate(self):
        instance = self.bind()
        db, tick = self.store.db, self.workers[0].checkpoint
        peer = self.ordinary_row(origin='peer', bridge_version=2, bound_turn='fictional-turn')
        insert(db, 'shared_bridge_requests', (uid(), self.agent, '{}', 'hash', peer['request_id'],
                                               'fictional', peer['created'], peer['created'] + 1, 'preview'))
        db.commit()
        tick()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))
        self.save(peer)
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        with db:
            db.execute('UPDATE shared_bridge_requests SET created=?', (time.time() - messaging.RETENTION - 2,))
        tick()
        with self.store.lock:
            # No explicit peer rows, but the shared bulk deletion still writes.
            self.store._prune([], time.time())
        self.assertIsNone(db.execute('SELECT data FROM requests WHERE id=?', (peer['request_id'],)).fetchone())
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        tick()
        side_effect = uid()
        db.execute(f"CREATE TEMP TRIGGER fictional_side_effect AFTER INSERT ON requests BEGIN INSERT INTO expired_controls VALUES ('{side_effect}'); END")
        tick()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))
        self.save(self.ordinary_row())
        self.assertEqual(db.execute('SELECT 1 FROM expired_controls WHERE id=?', (side_effect,)).fetchone(), (1,))
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)

    def test_unchanged_peer_history_keeps_owner_queue_available(self):
        instance = self.bind()
        db, checkpoint = self.store.db, self.workers[0].checkpoint
        peer_ids = {self.retained_peer(state) for state in ('dropped', 'retired', 'completed')}
        checkpoint()
        before = db.total_changes
        self.store.tick()
        self.assertEqual(db.total_changes, before)
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))

        rid, question = uid(), 'Check the fictional orchard.'
        first = self.store.submit(self.agent, question, rid, self.agents)
        self.assertEqual(first['state'], 'sending')
        self.assertEqual(self.store.submit(self.agent, question, rid, self.agents), first)
        self.assertEqual({row['request_id'] for row in self.store._rows()}, peer_ids | {rid})
        with self.assertRaises(messaging.MessagingError) as conflict:
            self.store.submit(self.agent, 'Different text.', rid, self.agents)
        self.assertEqual(conflict.exception.status, 409)

    def test_guarded_send_prunes_owner_rows_and_leaves_peer_expiry_to_worker(self):
        instance = self.bind()
        expired = messaging.RETENTION + 10
        peer = self.retained_peer('dropped', age=expired)
        ordinary = self.ordinary_row(age=expired)
        self.save(ordinary)
        self.workers[0].checkpoint()

        accepted = self.submit('Check the fictional orchard.')
        self.assertEqual(accepted['state'], 'sending')
        self.assertEqual({row['request_id'] for row in self.store._rows()}, {peer, accepted['request_id']})
        # Run the worker's cleanup without dispatching the accepted owner request.
        with self.store.lock:
            self.store._prune(self.store._rows(), time.time())
        self.assertEqual([row['request_id'] for row in self.store._rows()], [accepted['request_id']])
        with self.assertRaisesRegex(owner.LedgerError, 'attachment_index_unavailable'):
            self.counts(instance)
        self.workers[0].checkpoint()
        self.assertEqual(self.counts(instance), (0, 0, 0, 0))

    def test_invalidated_build_discards_candidate_and_retries_only_at_next_tick(self):
        self.installed_fixture(65)
        instance = self.bind(allow_install=False)
        directory, tick = instance.ledger.directory, self.workers[0].checkpoint
        tick()
        self.assertEqual(len(directory._building.entries), 64)
        with closing(sqlite3.connect(self.db_path)) as peer, peer:
            peer.execute('INSERT INTO expired_controls VALUES (?)', (uid(),))
        with patch.object(directory, 'begin_rebuild', wraps=directory.begin_rebuild) as begins:
            tick()
            self.assertFalse(directory.building)
            self.assertFalse(directory.ready)
            self.assertEqual(instance.state, 'recovery_required')
            begins.assert_not_called()
            tick()
            begins.assert_called_once()
            self.assertEqual(len(directory._building.entries), 64)
        tick()
        self.assertEqual(self.counts(instance), (65, 0, 0, 0))

    def test_ambiguous_ordinary_commit_discards_ready_and_in_progress_state(self):
        self.installed_fixture(65)
        instance = self.bind(allow_install=False)
        db, directory = self.store.db, instance.ledger.directory
        tick = self.workers[0].checkpoint
        tick()
        self.assertTrue(directory.building)
        row = self.ordinary_row()
        db.fault_commit = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.save(row)
        self.assertFalse(directory.building)
        self.assertFalse(directory.ready)
        self.assertEqual(db.execute('SELECT 1 FROM requests WHERE id=?', (row['request_id'],)).fetchone(), (1,))
        tick()
        tick()
        self.assertEqual(self.counts(instance), (65, 0, 0, 0))
        db.fault_commit = 'before'
        row = self.ordinary_row()
        with self.assertRaises(sqlite3.OperationalError):
            self.save(row)
        self.assertFalse(directory.ready)
        self.assertIsNone(db.execute('SELECT 1 FROM requests WHERE id=?', (row['request_id'],)).fetchone())

    def test_temporary_owner_table_shadow_cannot_publish_a_directory(self):
        _, protected = self.installed_fixture(pending=True)
        instance = self.bind(allow_install=False)
        tick = self.workers[0].checkpoint
        tick()
        self.assertEqual(self.counts(instance), (1, 1, 1, 0))
        db = self.store.db
        db.execute(f'CREATE TEMP TABLE {guards.LINKS.upper()} ({guards.RECORD})')
        tick()
        self.assertEqual(instance.state, 'recovery_required')
        self.assertFalse(instance.ledger.directory.ready)
        with self.assertRaisesRegex(guards.OwnerGuardError, 'shadowed'):
            self.counts(instance)
        db.execute(f'DROP TABLE temp.{guards.LINKS}')
        tick()
        self.assertEqual(self.counts(instance), (1, 1, 1, 0))
        self.assertEqual(instance.ledger.status(protected)['state'], 'owner_recorded')

    def test_non_neutral_slot_blocks_rebuild_and_ordinary_write(self):
        instance = self.bind()
        tick = self.workers[0].checkpoint
        tick()
        with self.store.db:
            self.store.db.execute(f"UPDATE {guards.PERMIT} SET verb='I'")
        row = self.ordinary_row()
        with self.assertRaises(guards.OwnerGuardError):
            self.save(row)
        tick()
        self.assertEqual(instance.state, 'recovery_required')
        self.assertFalse(instance.ledger.directory.ready)
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (row['request_id'],)).fetchone())

    def test_join_keeps_owner_db_open_through_in_flight_step(self):
        one_thread(self)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        close_errors = []
        step_calls = []
        method = directories.OwnerAttachmentDirectory.rebuild_step

        def gated_step(directory):
            step_calls.append(1)
            entered.set()
            if not release.wait(4):
                raise AssertionError('The test did not release the suspended batch.')
            return method(directory)

        def close():
            try:
                self.store.close()
                closed.set()
            except BaseException as error:
                close_errors.append(error)

        with patch.object(directories.OwnerAttachmentDirectory, 'rebuild_step', gated_step):
            instance = runtime.bind_owner_runtime(self.store, allow_install=True)
            self.assertTrue(entered.wait(3))
            instance.checkpoint()  # Another wakeup coalesces into the active batch.
            self.assertEqual(step_calls, [1])
            closer = threading.Thread(target=close)
            closer.start()
            try:
                self.assertTrue(instance.stopped.wait(3))
                self.assertFalse(closed.is_set())
                self.assertEqual(self.store.db.execute('SELECT 1').fetchone(), (1,))
                self.assertIsNotNone(self.store.owner_fd)
                instance.checkpoint()  # Cannot enqueue a second step behind shutdown.
            finally:
                release.set()
                closer.join(4)
        self.assertEqual(close_errors, [])
        self.assertTrue(closed.is_set())
        self.assertEqual(step_calls, [1])
        self.assertEqual(instance.state, 'stopped')
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store.db.execute('SELECT 1')
        self.store = None

    def test_server_shutdown_stops_only_native_owner_before_attachment_close(self):
        tree = ast.parse(Path(server.__file__).read_text())
        main = next(node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'")
        service = next(node for node in main.body if isinstance(node, ast.Try) and 'srv.serve_forever()' in ast.unparse(node))
        final = '\n'.join(ast.unparse(node) for node in service.finalbody)
        self.assertLess(final.index('MESSAGING.stop_native_attachment_runtime()'), final.index('ATTACHMENTS.close()'))
        self.assertNotIn('MESSAGING.close()', final)


if __name__ == '__main__':
    unittest.main()
