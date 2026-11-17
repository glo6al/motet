"""L2b uses real disposable owner SQLite and fictional rows, never a provider."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import attachments_owner_guards as guards
from attachments_owner_marker import CLEAR, MARKER, UNCERTAIN
import attachments_owner_transport as owner
import officer_messaging as messaging
import peer_admin
import peer_bridge
import peer_lifecycle
import server
from test_attachments_owner_transport import FaultConnection, owner_schema


def uid():
    return str(uuid.uuid4())


def admission(agent, **changes):
    envelope = 'Fictional attachment envelope.'
    return replace(owner.Admission(uid(), 'attachment-fixture', 'claim-fixture', 'e' * 64,
                                  'f' * 64, hashlib.sha256(envelope.encode()).hexdigest(),
                                  'codex', 'fictional-profile', 'v1', 'd' * 64, agent,
                                  'Review the fictional note.', envelope, 'Orchard'), **changes)


def install(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        guards.install_for_test(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def fixture_row(table, identity):
    now = time.time()
    values = {
        'requests': (identity, json.dumps({'request_id': identity})),
        'controls': (identity, '{}'),
        'expired_controls': (identity,),
        'asks': (identity, 'Fictional question.', now, None),
        'request_fulfillments': (uid(), identity, now),
        'peer_requests': (uid(), uid(), 'fictional-target', 'hash', identity, now, None),
        'shared_bridge_requests': (uid(), 'fictional-target', '{}', 'hash', identity, 'credential', now, None, 'preview'),
        'peer_execution': (identity, 'fictional-target', now, 'reserved', 'pending', '{}'),
        'peer_execution_events': (uid(), identity, 'kind', 'reason', now, None, '', '', 'fingerprint'),
        'peer_reply_admissions': (identity, 'hash'),
    }
    return values[table]


def insert(db, table, values, *, rowid=None, replace_row=False):
    prefix = 'INSERT OR REPLACE' if replace_row else 'INSERT'
    markers = ','.join('?' for _ in values)
    if rowid is None:
        return db.execute(f'{prefix} INTO {table} VALUES ({markers})', values)
    columns = ','.join(record[1] for record in db.execute(f'PRAGMA table_info({table})'))
    return db.execute(f'{prefix} INTO {table}(rowid,{columns}) VALUES (?,{markers})', (rowid, *values))


def held_key(db, identity, *, table=guards.LINKS):
    # Intentionally invalid fixed record. Mere presence protects the key,
    # but never authorizes status, reconciliation, or a provider operation.
    db.execute(f'INSERT INTO {table} VALUES (?,zeroblob(4096))', (identity,))


class SQLGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'fictional-owner.sqlite3')
        self.db = sqlite3.connect(self.path, factory=FaultConnection)
        self.addCleanup(self.db.close)
        owner_schema(self.db)
        self.lock = threading.RLock()
        owner.OwnerAttachmentLedger(self.db, self.lock)
        install(self.db)
        self.guard = guards.OwnerAttachmentGuard.probe(self.db, lock=self.lock)
        self.ledger = owner.OwnerAttachmentLedger(self.db, self.lock, guard=self.guard)
        self.agent = 'x:' + uid()

    def stock_connection(self):
        current = sqlite3.connect(self.path)
        self.addCleanup(current.close)
        self.assertEqual(current.execute('PRAGMA recursive_triggers').fetchone(), (0,))
        return current

    def failed_transaction(self, db, operation):
        predecessor = uid()
        db.execute('BEGIN IMMEDIATE')
        insert(db, 'asks', fixture_row('asks', predecessor))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'owner request storage protection conflict'):
            operation()
        self.assertFalse(db.in_transaction)
        self.assertIsNone(db.execute('SELECT 1 FROM asks WHERE id=?', (predecessor,)).fetchone())

    def test_ledger_binding_requires_same_connection_and_exact_writer_lock(self):
        another_db = self.stock_connection()
        with self.assertRaisesRegex(owner.LedgerError, 'owner_guard_writer_mismatch'):
            owner.OwnerAttachmentLedger(another_db, self.lock, guard=self.guard)
        with self.assertRaisesRegex(owner.LedgerError, 'owner_guard_writer_mismatch'):
            owner.OwnerAttachmentLedger(self.db, threading.RLock(), guard=self.guard)
        with self.assertRaisesRegex(owner.LedgerError, 'owner_guard_already_bound'):
            owner.OwnerAttachmentLedger(self.db, self.lock, guard=self.guard)
        self.assertEqual(self.ledger.admit(admission(self.agent), now_ms=int(time.time() * 1000))['state'], 'owner_recorded')

    def test_guard_map_matches_owner_reservations(self):
        guarded = [(table, name) for table, columns, _ in guards.SHARED_KEYS for name in columns]
        self.assertEqual(guarded, list(owner.NAMESPACES))

    def test_direct_insert_update_delete_each_protected_column(self):
        db = self.stock_connection()
        for table, protected, _ in guards.SHARED_KEYS:
            columns = [record[1] for record in db.execute(f'PRAGMA table_info({table})')]
            for column in protected:
                with self.subTest(table=table, column=column):
                    identity = uid()
                    old = list(fixture_row(table, uid()))
                    old[columns.index(column)] = identity
                    old_rowid = insert(db, table, old).lastrowid
                    new = list(fixture_row(table, uid()))
                    new_rowid = insert(db, table, new).lastrowid
                    held_key(db, identity, table=guards.OUTCOMES)
                    db.commit()
                    self.failed_transaction(db, lambda: db.execute(f'DELETE FROM {table} WHERE rowid=?', (old_rowid,)))
                    self.failed_transaction(db, lambda: db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (uid(), old_rowid)))
                    self.failed_transaction(db, lambda: db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (identity, new_rowid)))
                    target = list(fixture_row(table, uid()))
                    target[columns.index(column)] = identity
                    self.failed_transaction(db, lambda: insert(db, table, target))
                    self.assertEqual(db.execute(f'SELECT * FROM {table} WHERE rowid=?', (old_rowid,)).fetchone(), tuple(old))
                    self.assertEqual(db.execute(f'SELECT * FROM {table} WHERE rowid=?', (new_rowid,)).fetchone(), tuple(new))

    def test_replace_displacement_on_every_unique_key_and_rowid(self):
        db = self.stock_connection()
        for table, _, keys in guards.SHARED_KEYS:
            columns = [record[1] for record in db.execute(f'PRAGMA table_info({table})')]
            for key in (*keys, ('rowid',)):
                with self.subTest(table=table, key=key):
                    identity = uid()
                    original = fixture_row(table, identity)
                    old_id = insert(db, table, original).lastrowid
                    source = fixture_row(table, uid())
                    source_id = insert(db, table, source).lastrowid
                    held_key(db, identity)
                    db.commit()
                    proposed = list(fixture_row(table, uid()))
                    if key == ('rowid',):
                        rowid, key_values = old_id, (old_id,)
                    else:
                        rowid = None
                        key_values = tuple(original[columns.index(col)] for col in key)
                        for col, value in zip(key, key_values):
                            proposed[columns.index(col)] = value
                    self.failed_transaction(db, lambda: insert(db, table, proposed, rowid=rowid, replace_row=True))
                    assignments = ','.join(f'{col}=?' for col in key)
                    self.failed_transaction(db, lambda: db.execute(f'UPDATE OR REPLACE {table} SET {assignments} WHERE rowid=?', (*key_values, source_id)))
                    self.assertEqual(db.execute(f'SELECT * FROM {table} WHERE rowid=?', (old_id,)).fetchone(), original)
                    self.assertEqual(db.execute(f'SELECT * FROM {table} WHERE rowid=?', (source_id,)).fetchone(), source)

    def test_multiple_conflict_targets_cannot_replace_ordinary_neighbor_first(self):
        db = self.stock_connection()
        protected = fixture_row('shared_bridge_requests', uid())
        ordinary = fixture_row('shared_bridge_requests', uid())
        insert(db, 'shared_bridge_requests', protected)
        insert(db, 'shared_bridge_requests', ordinary)
        held_key(db, protected[4])
        db.commit()
        proposed = list(ordinary)
        proposed[0] = protected[0]
        self.failed_transaction(db, lambda: insert(db, 'shared_bridge_requests', proposed, replace_row=True))
        self.assertEqual(db.execute('SELECT * FROM shared_bridge_requests ORDER BY rowid').fetchall(), [protected, ordinary])
        # A stock ordinary-only replacement on the same installed DB succeeds.
        replacement = ordinary[:-1] + ('new preview',)
        insert(db, 'shared_bridge_requests', replacement, replace_row=True)
        db.commit()
        self.assertEqual(db.execute('SELECT preview FROM shared_bridge_requests WHERE client_id=?', (ordinary[0],)).fetchone(), ('new preview',))

    def test_nullable_and_binary_keys_keep_sqlite_conflict_semantics(self):
        db = self.stock_connection()
        removed = list(fixture_row('peer_execution_events', uid()))
        removed[0] = None
        insert(db, 'peer_execution_events', removed)
        held_key(db, removed[1])
        another_null = (None,) + fixture_row('peer_execution_events', uid())[1:]
        insert(db, 'peer_execution_events', another_null, replace_row=True)
        lower = ('fixturelower',) + fixture_row('peer_execution_events', uid())[1:]
        insert(db, 'peer_execution_events', lower)
        held_key(db, lower[1])
        upper = ('FIXTURELOWER',) + fixture_row('peer_execution_events', uid())[1:]
        insert(db, 'peer_execution_events', upper, replace_row=True)
        db.commit()
        self.assertEqual(db.execute('SELECT request_id FROM peer_execution_events ORDER BY rowid').fetchall(),
                         [(row[1],) for row in (removed, another_null, lower, upper)])

    def test_negative_explicit_rowid_is_protected_on_stock_connection(self):
        db = self.stock_connection()
        row = fixture_row('peer_execution_events', uid())
        insert(db, 'peer_execution_events', row, rowid=-1)
        held_key(db, row[1])
        db.commit()
        other = fixture_row('peer_execution_events', uid())
        self.failed_transaction(db, lambda: insert(db, 'peer_execution_events', other, rowid=-1, replace_row=True))
        self.assertEqual(db.execute('SELECT * FROM peer_execution_events WHERE rowid=-1').fetchone(), row)

    def test_unsupported_unique_and_marker_trigger_signature_refused(self):
        for index in ('CREATE UNIQUE INDEX unexpected ON controls(data)',
                      'CREATE UNIQUE INDEX unexpected ON controls(lower(id))',
                      'CREATE UNIQUE INDEX unexpected ON controls(id COLLATE NOCASE) WHERE data IS NOT NULL'):
            with self.subTest(index=index):
                self.db.execute(index)
                self.assertRaises(guards.OwnerGuardError, self.guard.require_schema)
                self.db.execute('DROP INDEX unexpected')
                self.guard.require_schema()
        triggers = guards._trigger_definitions()
        target = guards.TRIGGER_PREFIX + 'requests_insert'
        self.db.execute(f'DROP TRIGGER {target}')
        self.db.execute(triggers[target].replace("'attachment_owner_version'", "'attachment_ owner_version'"))
        self.assertRaises(guards.OwnerGuardError, self.guard.require_schema)

    def test_orphan_payload_cannot_be_created_displaced_or_pruned(self):
        db = self.stock_connection()
        identity = uid()
        # A version marker is only a protective quarantine, never authority.
        marker = '{"attachment_owner_version":null}'
        self.failed_transaction(db, lambda: insert(db, 'requests', (identity, marker)))
        # Simulate an already retained orphan before installing the guards.
        trigger = guards.TRIGGER_PREFIX + 'requests_insert'
        db.execute(f'DROP TRIGGER {trigger}')
        rowid = insert(db, 'requests', (identity, marker)).lastrowid
        db.execute(guards._trigger_definitions()[trigger])
        db.commit()
        self.assertTrue(self.guard.reserved(identity))
        self.assertEqual(self.guard.legacy_rows(), [])
        self.failed_transaction(db, lambda: insert(db, 'requests', fixture_row('requests', uid()), rowid=rowid, replace_row=True))
        self.failed_transaction(db, lambda: db.execute('DELETE FROM requests WHERE id=?', (identity,)))
        with self.assertRaisesRegex(owner.LedgerError, 'not_found'):
            self.ledger.status(identity)
        self.assertEqual(db.execute('SELECT * FROM requests').fetchall(), [(identity, marker)])

    def test_payload_permit_faults_rollback_and_commit_ack_is_idempotent(self):
        self.assertEqual(self.db.execute(f'SELECT owner_request_id,verb FROM {guards.PERMIT}').fetchall(), [(guards.NEUTRAL, '-')])
        for phase, predicate in (
            ('arm', "NEW.verb='I'"),
            ('write', None),
            ('disarm', "OLD.verb='I' AND NEW.verb='-'"),
        ):
            target = f'UPDATE ON {guards.PERMIT} WHEN {predicate}' if predicate else 'INSERT ON requests'
            self.db.execute(f"CREATE TEMP TRIGGER fault BEFORE {target} BEGIN SELECT RAISE(ABORT,'fictional fault'); END")
            self.ledger.rebuild_directory()
            with self.subTest(phase=phase), self.assertRaisesRegex(sqlite3.IntegrityError, 'fictional fault'):
                self.ledger.admit(admission(self.agent), now_ms=int(time.time() * 1000))
            self.db.execute('DROP TRIGGER fault')
            for table in (guards.LINKS, guards.OUTCOMES, 'requests'):
                self.assertEqual(self.db.execute(f'SELECT count(*) FROM {table}').fetchone(), (0,))
            self.assertEqual(self.db.execute(f'SELECT owner_request_id,verb FROM {guards.PERMIT}').fetchone(), (guards.NEUTRAL, '-'))
        value = admission(self.agent)
        now = int(time.time() * 1000)
        for fault, rows in (('before', 0), ('after', 1)):
            self.ledger.rebuild_directory()
            self.db.commit_fault = fault
            try:
                with self.subTest(fault=fault), self.assertRaises(sqlite3.OperationalError):
                    self.ledger.admit(value, now_ms=now)
            finally:
                self.db.commit_fault = ''
            self.assertFalse(self.ledger.directory.ready)
            self.assertEqual(self.db.execute(f'SELECT owner_request_id,verb FROM {guards.PERMIT}').fetchone(), (guards.NEUTRAL, '-'))
            self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone(), (rows,))
        reopened = self.stock_connection()
        lock = threading.RLock()
        guard = guards.OwnerAttachmentGuard.probe(reopened, lock=lock)
        ledger = owner.OwnerAttachmentLedger(reopened, lock, guard=guard)
        replay = ledger.admit(value, now_ms=now + 1)
        self.assertEqual(replay['owner_request_id'], value.owner_request_id)
        self.assertEqual(reopened.execute('SELECT count(*) FROM requests').fetchone(), (1,))

    def test_permit_is_single_verb_and_never_bypasses_other_namespaces(self):
        value = admission(self.agent)
        now = int(time.time() * 1000)
        self.ledger.admit(value, now_ms=now)
        identity = value.owner_request_id
        original = self.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone()
        # Fixture-only arm mimics the instant around a legitimate ledger UPDATE.
        for denied in (
            lambda: self.db.execute('DELETE FROM requests WHERE id=?', (identity,)),
            lambda: insert(self.db, 'requests', (identity, '{}'), replace_row=True),
            lambda: self.db.execute('UPDATE requests SET id=? WHERE id=?', (uid(), identity)),
            lambda: insert(self.db, 'controls', (identity, '{}')),
        ):
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute(f"UPDATE {guards.PERMIT} SET verb='U', owner_request_id=?", (identity,))
            with self.assertRaises(sqlite3.IntegrityError):
                denied()
            self.assertEqual(self.db.execute(f'SELECT owner_request_id,verb FROM {guards.PERMIT}').fetchone(), (guards.NEUTRAL, '-'))
            self.assertEqual(self.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone(), original)

    def test_fixed_storage_ceiling_delete_and_wal(self):
        for journal in ('delete', 'wal'):
            with self.subTest(journal=journal):
                self.db.execute('PRAGMA journal_mode=' + journal)
                self.ledger.rebuild_directory()
                env = '\x01' * 4000
                a = admission(self.agent, question='\x01' * 2000, envelope=env,
                              envelope_sha256=hashlib.sha256(env.encode()).hexdigest(), name='😀' * 120)
                now = int(time.time() * 1000)
                result = self.ledger.admit(a, now_ms=now)
                identity, digest = a.owner_request_id, result['admission_digest']
                pages = self.db.execute('PRAGMA page_count').fetchone()[0]
                self.db.execute(f'PRAGMA max_page_count={pages}')
                try:
                    attempt = self.ledger.record_claim(identity, digest, now_ms=now + 1)['attempt_id']
                    for n, kind, extra in ((1, 'accepted', {}), (2, 'started', {'turn_id': '😀' * 128}),
                                           (3, 'reply', {'turn_id': '😀' * 128, 'reply': '\x01' * 16000})):
                        outcome = self.ledger.record_outcome(identity, digest, attempt, kind, evidence_sequence=n, now_ms=now + n + 1, **extra)['outcome']
                    self.ledger.acknowledge(identity, digest, outcome['seq'], outcome['digest'])
                    self.assertTrue(self.ledger.prune_payload(identity, digest, now_ms=now + owner.RETENTION_MS + 1)['payload_pruned'])
                    outcome = self.ledger.record_outcome(identity, digest, attempt, 'reply', evidence_sequence=4, now_ms=now + owner.RETENTION_MS + 2,
                                                         turn_id='😀' * 128, reply='😀' * 16000)['outcome']
                    self.ledger.acknowledge(identity, digest, outcome['seq'], outcome['digest'])
                    self.assertEqual(self.db.execute('PRAGMA page_count').fetchone(), (pages,))
                    self.assertTrue(self.guard.reserved(identity))
                    self.assertIsNone(self.db.execute('SELECT 1 FROM requests WHERE id=?', (identity,)).fetchone())
                finally:
                    self.db.execute('PRAGMA max_page_count=1073741823')


@unittest.skipIf(os.name == 'nt', 'POSIX-private owner messaging storage')
class MessagingGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'
        self.sessions = Path(self.temp.name) / 'sessions'
        self.sessions.mkdir()
        self.native = uid()
        self.agent = 'x:' + self.native
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Orchard'}}
        self.transcript = self.sessions / ('rollout-' + self.native + '.jsonl')
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': self.native, 'source': 'vscode', 'originator': 'Codex Desktop'}}) + '\n')
        self.store = self.open_store()
        self.addCleanup(self.close_store)
        self.path = self.state / 'officer-messages.sqlite3'

    def open_store(self, **options):
        if getattr(self, 'without_json', False):
            connect = sqlite3.connect

            def deny_json(*args, **kwargs):
                db = connect(*args, **kwargs)
                db.set_authorizer(lambda action, first, second, schema, trigger:
                                  sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION
                                  and second in ('json_valid', 'json_type') else sqlite3.SQLITE_OK)
                return db

            with patch.object(sqlite3, 'connect', side_effect=deny_json):
                store = messaging.MessagingStore(str(self.state), str(self.sessions), **options)
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute("SELECT json_valid('{}')")
            self.assertIn('watch_owner_attachment_state', store._uninstalled_payload_state('requests.data'))
        else:
            store = messaging.MessagingStore(str(self.state), str(self.sessions), **options)
        store.available, store.reason = True, ''
        return store

    def close_store(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def installed(self, prepare=None, *, bind=True):
        owner.OwnerAttachmentLedger(self.store.db, self.store.lock)
        if prepare:
            prepare(self.store.db)
            self.store.db.commit()
        install(self.store.db)
        self.close_store()
        self.store = self.open_store()
        self.assertIsNotNone(self.store._attachment_guard)
        if bind:
            return owner.OwnerAttachmentLedger(self.store.db, self.store.lock, guard=self.store._attachment_guard)
        return None

    def new_message(self, text='Review the fictional activity.', identity=None):
        return self.store.submit(self.agent, text, identity or uid(), self.agents)

    def row(self, identity, *, state='answered', age=120, **extra):
        return dict(request_id=identity, agent=self.agent, name='Orchard', question=f'Fictional prompt {identity}',
                    state=state, reply='', error='', created=time.time() - age, launched=state != 'sending',
                    runtime='codex', **extra)

    def peer(self, *, age=1, state='quarantined'):
        identity = uid()
        row = self.row(identity, state=state, age=age, origin='peer', bridge_version=2,
                       bound_turn='fictional-peer-turn', candidate_reply='A fictional peer answer.', settled=True)
        values = (uid(), self.agent, '{}', 'hash', identity, 'credential', row['created'], row['created'] + .1, 'original preview')
        insert(self.store.db, 'shared_bridge_requests', values)
        insert(self.store.db, 'requests', (identity, json.dumps(row)))
        self.store.db.commit()
        return identity

    def mock_tick(self):
        with patch.object(self.store, '_send', return_value=('queued', '')) as send, patch.object(self.store, '_poll') as poll:
            self.store.tick()
        return send, poll

    def test_fresh_owner_has_no_native_objects_or_constructor(self):
        self.assertIsNone(self.store._attachment_guard)
        original = self.store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'owner_attachment_%'").fetchall()
        opener = self.new_message()
        launch, _ = self.mock_tick()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0]['request_id'], opener['request_id'])
        self.assertEqual(self.store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'owner_attachment_%'").fetchall(), original)
        self.assertEqual(original, [])

    def in_both_uninstalled_modes(self, exercise):
        for fallback in (False, True):
            with self.subTest(no_sqlite_json=fallback):
                self.close_store()
                self.without_json = fallback
                self.state = Path(self.temp.name) / f'state-fallback-{fallback}'
                self.path = self.state / 'officer-messages.sqlite3'
                self.store = self.open_store()
                exercise()

    def uninstalled_orphan(self, identity):
        marker = r'{"\u0061ttachment_owner_version":null,"state":"sending"}'
        insert(self.store.db, 'requests', (identity, marker))
        return marker

    def assert_no_native_schema(self, original_version):
        self.assertIsNone(self.store._attachment_guard)
        self.assertEqual(self.store.db.execute('PRAGMA schema_version').fetchone(), original_version)
        native = self.store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'owner_attachment_%'").fetchall()
        self.assertEqual(native, [])

    def test_f2_uninstalled_ask_prunes_neighbor_but_retains_orphan_alias(self):
        self.in_both_uninstalled_modes(self.check_ask_prunes_neighbor_but_retains_orphan_alias)

    def check_ask_prunes_neighbor_but_retains_orphan_alias(self):
        db = self.store.db
        version = db.execute('PRAGMA schema_version').fetchone()
        orphan, other, next_ask = uid(), uid(), uid()
        marker = self.uninstalled_orphan(orphan)
        aged = time.time() - messaging.RETENTION - 60
        db.executemany('INSERT INTO asks VALUES (?,?,?,NULL)',
                       [(identity, 'Fictional question.', aged) for identity in (orphan, other)])
        db.commit()
        self.assertIsNone(self.store.claim_ask(next_ask, 'A separate question.'))
        self.assertEqual(set(db.execute('SELECT id FROM asks')), {(orphan,), (next_ask,)})
        self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (orphan,)).fetchone(), (marker,))
        self.assert_no_native_schema(version)

    def test_f2_uninstalled_control_alias_holds_only_peer_capture(self):
        self.in_both_uninstalled_modes(self.check_control_alias_holds_only_peer_capture)

    def check_control_alias_holds_only_peer_capture(self):
        version = self.store.db.execute('PRAGMA schema_version').fetchone()
        orphan, ordinary_control = uid(), uid()
        self.uninstalled_orphan(orphan)
        insert(self.store.db, 'controls', (orphan, 'protected malformed control'))
        insert(self.store.db, 'controls', (ordinary_control, json.dumps(dict(
            request_id=ordinary_control, agent=self.agent, state='sending', created=time.time()))))
        self.store.db.commit()
        self.close_store()
        self.store = self.open_store()
        self.assertEqual([(row['request_id'], row['state']) for row in self.store.snapshot()['controls']],
                         [(ordinary_control, 'unknown')])
        self.assertEqual(self.store.db.execute('SELECT data FROM controls WHERE id=?', (orphan,)).fetchone(),
                         ('protected malformed control',))
        ordinary = self.new_message()
        peer = self.peer()
        with patch('sys.stderr') as diagnostics:
            send, poll = self.mock_tick()
        self.assertIn('Peer reply capture is held', ''.join(call.args[0] for call in diagnostics.write.call_args_list))
        self.assertEqual([call.args[0]['request_id'] for call in send.call_args_list], [ordinary['request_id']])
        self.assertIn(ordinary['request_id'], [call.args[0]['request_id'] for call in poll.call_args_list])
        held = json.loads(self.store.db.execute('SELECT data FROM requests WHERE id=?', (peer,)).fetchone()[0])
        self.assertEqual((held['state'], held['candidate_reply']), ('quarantined', 'A fictional peer answer.'))
        self.assertTrue(self.store._attachment_reconciliation_held)
        # Fixture repair only, to prove that capture recovers at the next check.
        with self.store.db:
            self.store.db.execute('DELETE FROM controls WHERE id=?', (orphan,))
            self.store.db.execute('DELETE FROM requests WHERE id=?', (orphan,))
            ordinary_row = self.row(ordinary['request_id'])
            self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(ordinary_row), ordinary['request_id']))
        self.mock_tick()
        settled = json.loads(self.store.db.execute('SELECT data FROM requests WHERE id=?', (peer,)).fetchone()[0])
        self.assertEqual((settled['state'], settled['reply']), ('answered', 'A fictional peer answer.'))
        self.assertFalse(self.store._attachment_reconciliation_held)
        self.assert_no_native_schema(version)

    def test_f2_uninstalled_fulfillment_and_both_prune_paths_keep_orphan_aliases(self):
        self.in_both_uninstalled_modes(self.check_fulfillment_and_both_prune_paths_keep_orphan_aliases)

    def check_fulfillment_and_both_prune_paths_keep_orphan_aliases(self):
        db = self.store.db
        version = db.execute('PRAGMA schema_version').fetchone()
        shared_orphan, control_orphan, aged_ordinary, current_ordinary = (uid() for _ in range(4))
        orphan_marker = self.uninstalled_orphan(shared_orphan)
        self.uninstalled_orphan(control_orphan)
        now = time.time()
        aged = now - messaging.RETENTION - 10
        shared = self.peer(age=messaging.RETENTION + 12)
        insert(db, 'shared_bridge_requests', (uid(), self.agent, '{}', 'hash', shared_orphan,
                                              'credential', aged, None, 'protected preview'))
        for identity, age in ((aged_ordinary, messaging.RETENTION + 15), (current_ordinary, 60)):
            insert(db, 'requests', (identity, json.dumps(self.row(identity, age=age))))
            insert(db, 'request_fulfillments', (identity, control_orphan, now - 30))
        db.commit()
        projected = next(row for row in self.store.snapshot()['requests'] if row['request_id'] == current_ordinary)
        self.assertNotIn('fulfillment', projected)
        self.store._prune(self.store._rows(), now)
        self.assertEqual(set(db.execute('SELECT id FROM requests')),
                         {(shared_orphan,), (control_orphan,), (current_ordinary,)})
        self.assertEqual(dict(db.execute('SELECT transport_id,preview FROM shared_bridge_requests')),
                         {shared: '', shared_orphan: 'protected preview'})
        self.assertEqual(set(db.execute('SELECT request_id,control_request_id FROM request_fulfillments')),
                         {(aged_ordinary, control_orphan), (current_ordinary, control_orphan)})
        self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (shared_orphan,)).fetchone(), (orphan_marker,))
        self.assert_no_native_schema(version)

    def test_f2_uninstalled_ask_and_managed_projections_ignore_exact_alias(self):
        self.in_both_uninstalled_modes(self.check_ask_and_managed_projections_ignore_exact_alias)

    def check_ask_and_managed_projections_ignore_exact_alias(self):
        db = self.store.db
        version = db.execute('PRAGMA schema_version').fetchone()
        orphan, ordinary, control, turn, managed_neighbor = (uid() for _ in range(5))
        self.uninstalled_orphan(orphan)
        manager = Mock()
        manager.owner_snapshot.return_value = {'requests': [{'request_id': orphan}, {'request_id': managed_neighbor}], 'available': True}
        self.store.managed = manager
        now = time.time()
        pin = dict(path=str(self.transcript), inode=self.transcript.stat().st_ino, device=self.transcript.stat().st_dev)
        row = self.row(ordinary, age=10, bound_turn=turn, **pin)
        row['question'] = 'Pause now.'
        insert(db, 'requests', (ordinary, json.dumps(row)))
        insert(db, 'asks', (ordinary, 'Tell Orchard to pause now.', now - 15,
                            json.dumps({'kind': 'relay', 'agent': self.agent, 'question': row['question']})))
        insert(db, 'asks', (orphan, 'Fictional alias during the interval.', now - 13, None))
        insert(db, 'controls', (control, json.dumps(dict(
            request_id=control, action='interrupt', agent=self.agent, state='interrupted',
            expected_turn_id=turn, created=now - 7, completed_at=now - 5, transcript=pin))))
        db.commit()
        with db:
            self.store._reconcile_fulfillments()
        rows = {row['request_id']: row for row in self.store.snapshot()['requests']}
        self.assertEqual(set(rows), {ordinary, managed_neighbor})
        self.assertEqual(rows[ordinary]['fulfillment']['control_request_id'], control)
        self.assertEqual(db.execute('SELECT id FROM asks WHERE id=?', (orphan,)).fetchone(), (orphan,))
        self.assert_no_native_schema(version)

    def in_peer_recovery_modes(self, seed, exercise):
        for mode in ('sqlite', 'no-json', 'installed'):
            with self.subTest(recovery_mode=mode):
                self.close_store()
                self.without_json = mode == 'no-json'
                self.state = Path(self.temp.name) / f'recovery-{mode}'
                self.path = self.state / 'officer-messages.sqlite3'
                self.store = self.open_store()
                facts = seed(self.store.db)
                self.store.db.commit()
                if mode == 'installed':
                    owner.OwnerAttachmentLedger(self.store.db, self.store.lock)
                    install(self.store.db)
                version = self.store.db.execute('PRAGMA schema_version').fetchone()
                self.close_store()
                self.store = self.open_store()
                self.assertEqual(self.store._attachment_guard is not None, mode == 'installed')
                exercise(facts)
                if mode != 'installed':
                    self.assert_no_native_schema(version)

    def seed_peer_admission(self, db, version, target, created, launched, body, identity=None):
        identity, client, sender = identity or uid(), uid(), uid()
        if version == 1:
            target_account = uid()
            insert(db, 'peer_principals', (target_account, 'Orchard', 'codex', target[2:],
                                          'fictional project', 'exact', '{}', 'fictional secret', 1, 1, created))
            insert(db, 'peer_requests', (sender, client, target_account, 'hash', identity, created, launched))
        else:
            insert(db, 'shared_bridge_requests', (client, target, '{}', 'hash', identity, 'credential',
                                                 created, launched, 'Fictional preview'))
        if type(body) is bytes:
            db.execute('INSERT INTO requests VALUES (?,CAST(? AS TEXT))', (identity, body))
        elif body is not None:
            insert(db, 'requests', (identity, body))
        return identity, client, sender

    def peer_result(self, version, client, sender):
        return self.store.peers.result(sender, client) if version == 1 else self.store.shared.result(client)

    def test_uncertain_peer_restart_preserves_both_namespaces_and_capacity(self):
        def seed(db):
            facts = []
            started = time.time() - 10
            for version in (1, 2):
                for launched in (None, started + 2):
                    target, old = 'x:' + uid(), launched is not None
                    body = b'{bad' if not old else b'{"x":"\xff"}'
                    identity, client, sender = self.seed_peer_admission(db, version, target, started, launched, body)
                    evidence = {'launched': True, 'bound_turn': 'fictional earlier turn'} if old else {}
                    if old:
                        insert(db, 'peer_execution', (identity, target, started, 'running', 'pending', json.dumps(evidence)))
                    facts.append((version, identity, target, client, sender, body, evidence))
            absent = self.seed_peer_admission(db, 2, 'x:' + uid(), started, None, None)[0]
            normal = uid()
            insert(db, 'requests', (normal, json.dumps(self.row(normal, state='queued', age=3))))
            neighbor = uid()
            raw = json.dumps(dict(self.row(neighbor, state='answered', age=1, origin='peer', bridge_version=2,
                                           bound_turn='healthy turn', settled=True), created=started,
                                  reply='Independent healthy peer reply.'))
            _, healthy_client, _ = self.seed_peer_admission(db, 2, self.agent, started, started+1, raw, neighbor)
            admissions = (db.execute('SELECT * FROM peer_requests ORDER BY transport_id').fetchall(),
                          db.execute('SELECT * FROM shared_bridge_requests ORDER BY transport_id').fetchall())
            return facts, absent, normal, healthy_client, admissions

        def exercise(value):
            facts, absent, normal, healthy_client, admissions = value
            for reopen in (False, True):
                if reopen:
                    self.close_store()
                    self.store = self.open_store()
                db = self.store.db
                self.assertEqual(peer_lifecycle.get(db, absent)['execution'], 'dropped')
                self.assertEqual(self.store._peer_payload(absent), ('clear', None))
                for version, identity, target, client, sender, body, evidence in facts:
                    self.assertEqual(db.execute('SELECT CAST(data AS BLOB) FROM requests WHERE id=?', (identity,)).fetchone(), (body,))
                    self.assertEqual(self.store._peer_payload(identity), ('uncertain', None))
                    record = peer_lifecycle.get(db, identity)
                    self.assertEqual((record['agent'], record['execution'], record['capture'], record['evidence']),
                                     (target, 'uncertain', 'pending', evidence))
                    self.assertEqual([item[:2] for item in peer_lifecycle.blockers(db, target)], [(identity, 'uncertain')])
                    self.assertIn(identity, peer_lifecycle.capacity_error(db, target, time.time(), messaging.TTL))
                    result = self.peer_result(version, client, sender)
                    self.assertEqual((result['state'], result['reply']), ('unknown', ''))
                self.assertIn('Four peer deliveries', peer_lifecycle.capacity_error(db, 'x:' + uid(), time.time(), messaging.TTL))
                self.assertEqual((db.execute('SELECT * FROM peer_requests ORDER BY transport_id').fetchall(),
                                  db.execute('SELECT * FROM shared_bridge_requests ORDER BY transport_id').fetchall()), admissions)
                self.assertEqual(db.execute('SELECT count(*) FROM peer_execution_events').fetchone(), (0,))
                self.assertEqual(self.store.shared.result(healthy_client)['reply'], 'Independent healthy peer reply.')
                self.assertEqual([row['request_id'] for row in self.store.snapshot()['requests']], [normal])
                send, poll = self.mock_tick()
                send.assert_not_called()
                self.assertEqual([call.args[0]['request_id'] for call in poll.call_args_list], [normal])
        self.in_peer_recovery_modes(seed, exercise)

    def test_uncertain_peer_terminal_bytes_and_existing_unknown_projection(self):
        def seed(db):
            facts = []
            started = time.time() - 20
            for version in (1, 2):
                for execution, capture, body in (('completed', 'withheld', b'{"x":"\xff"}'),
                                                  ('dropped', 'withheld', b'{bad'),
                                                  ('retired', 'withheld', b'{"x":' + b'['*3000 + b'0' + b']'*3000 + b'}'),
                                                  ('completed', 'captured', None)):
                    target, identity = 'x:' + uid(), uid()
                    if body is None:
                        peer_shape = dict(self.row(identity, origin='peer', bridge_version=version), agent=target,
                                          created=started, bound_turn='genuine-looking turn', settled=True,
                                          reply='Fictional held answer café.')
                        body = json.dumps(peer_shape, ensure_ascii=False).encode()
                    _, client, sender = self.seed_peer_admission(db, version, target, started, started+1, body, identity)
                    evidence = '{ "retained": "fictional metadata" }'
                    lifecycle = (identity, target, started, execution, capture, evidence)
                    insert(db, 'peer_execution', lifecycle)
                    facts.append((version, identity, client, sender, body, lifecycle))
            return facts

        def exercise(facts):
            db = self.store.db
            for version, identity, client, sender, body, lifecycle in facts:
                self.assertEqual(db.execute('SELECT * FROM peer_execution WHERE request_id=?', (identity,)).fetchone(), lifecycle)
                self.assertEqual(db.execute('SELECT CAST(data AS BLOB) FROM requests WHERE id=?', (identity,)).fetchone(), (body,))
                self.assertEqual(peer_lifecycle.blockers(db, lifecycle[1]), [])
                result = self.peer_result(version, client, sender)
                expected = 'failed' if lifecycle[3] == 'dropped' else 'unknown'
                self.assertEqual((result['state'], result['reply']), (expected, ''))
                if lifecycle[3] == 'retired':
                    self.assertIn('retired by the owner', result['error'])
                if lifecycle[3:5] == ('completed', 'withheld'):
                    self.assertIn('withheld', result['error'])
                self.assertEqual(db.execute('SELECT count(*) FROM peer_execution_events WHERE request_id=?', (identity,)).fetchone(), (0,))
            for version, identity, client, sender, _, lifecycle in facts:
                if lifecycle[3] == 'dropped':
                    with db:
                        db.execute('UPDATE peer_execution SET agent=? WHERE request_id=?', ('x:' + uid(), identity))
                    mismatched = self.peer_result(version, client, sender)
                    self.assertEqual((mismatched['state'], mismatched['reply']), ('unknown', ''))
        self.in_peer_recovery_modes(seed, exercise)

    def test_online_peer_admin_never_treats_an_uncertain_body_as_pruned(self):
        def seed(db):
            started = time.time() - 20
            facts = {}
            for name in ('held', 'clear', 'absent'):
                target, identity = 'x:' + uid(), uid()
                row = dict(self.row(identity, state='unknown', origin='peer', bridge_version=2), agent=target,
                           created=started, bound_turn='fictional turn', question='Fictional café.')
                body = None if name == 'absent' else json.dumps(row, ensure_ascii=name == 'clear')
                _, client, _ = self.seed_peer_admission(db, 2, target, started, started+1, body, identity)
                insert(db, 'peer_execution', (identity, target, started, 'uncertain', 'pending', '{"launched":true}'))
                facts[name] = (identity, target, client, body)
            return facts

        def exercise(facts):
            db = self.store.db
            protected, recipient, client, body = facts['held']
            before = tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                           for table in ('shared_bridge_requests', 'peer_execution', 'peer_execution_events'))
            for action in (lambda: peer_admin.inspect(self.store, {'transport_id': protected}),
                           lambda: peer_admin.retire(self.store, dict(transport_id=protected, agent=recipient,
                                                                    confirm=peer_admin.CONFIRM))):
                with self.assertRaises(peer_bridge.PeerError) as conflict:
                    action()
                self.assertEqual(conflict.exception.status, 409)
                self.assertIn('conflicts with execution evidence', str(conflict.exception))
            self.assertEqual(tuple(db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                                   for table in ('shared_bridge_requests', 'peer_execution', 'peer_execution_events')), before)
            self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (protected,)).fetchone(), (body,))
            self.assertEqual([row[0] for row in peer_lifecycle.blockers(db, recipient)], [protected])
            self.assertEqual(self.store.shared.result(client)['state'], 'unknown')
            for name in ('clear', 'absent'):
                identity, target, _, _ = facts[name]
                info = peer_admin.inspect(self.store, {'transport_id': identity})
                self.assertEqual((info['eligible'], info['retained_payload']), (True, name == 'clear'))
                result = peer_admin.retire(self.store, dict(transport_id=identity, agent=target, confirm=peer_admin.CONFIRM))
                self.assertEqual((result['status'], result['provider_outcome']), ('retired', 'unknown'))
                self.assertEqual(peer_lifecycle.blockers(db, target), [])
                if name == 'absent':
                    self.assertEqual(self.store._peer_payload(identity), ('clear', None))
        self.in_peer_recovery_modes(seed, exercise)

    def test_f3_uncertain_rows_are_held_without_killing_unrelated_work(self):
        def exercise():
            db = self.store.db
            version = db.execute('PRAGMA schema_version').fetchone()
            deep, bad_bytes, alternative_encoding, blob = (uid() for _ in range(4))
            insert(db, 'requests', (deep, '{"deep":' + '[' * 2500 + '0' + ']' * 2500 + '}'))
            db.execute('INSERT INTO requests VALUES (?,CAST(? AS TEXT))', (bad_bytes, b'{"text":"\xff"}'))
            insert(db, 'requests', (alternative_encoding, json.dumps({'request_id': alternative_encoding, 'text': 'é'}, ensure_ascii=False)))
            insert(db, 'requests', (blob, sqlite3.Binary(json.dumps(self.row(blob)).encode('ascii'))))
            db.commit()
            self.assertEqual(self.store._rows(), [])
            for exact in (deep, bad_bytes, alternative_encoding, blob):
                self.assertEqual(self.store._attachment_reservation(exact), UNCERTAIN)
                with self.assertRaises(messaging.MessagingError) as refusal:
                    self.new_message(identity=exact)
                self.assertEqual(refusal.exception.status, 503)
                self.assertIn('could not be safely classified', str(refusal.exception))
                self.assertNotIn('attachment', str(refusal.exception))
            ordinary = self.new_message()
            send, poll = self.mock_tick()
            self.assertEqual([call.args[0]['request_id'] for call in send.call_args_list], [ordinary['request_id']])
            self.assertEqual([call.args[0]['request_id'] for call in poll.call_args_list], [ordinary['request_id']])
            self.assertEqual([row['request_id'] for row in self.store.snapshot()['requests']], [ordinary['request_id']])
            self.assertEqual(set(db.execute('SELECT id FROM requests')),
                             {(deep,), (bad_bytes,), (alternative_encoding,), (blob,), (ordinary['request_id'],)})
            self.assert_no_native_schema(version)
        self.in_both_uninstalled_modes(exercise)

    def test_f3_both_uninstalled_modes_hold_large_late_marker_and_keep_large_neighbor(self):
        def exercise():
            db = self.store.db
            version = db.execute('PRAGMA schema_version').fetchone()
            ordinary, marked = uid(), uid()
            body = self.row(ordinary)
            body['fictional_padding'] = 'x' * 9900000
            raw = json.dumps(body)
            insert(db, 'requests', (ordinary, raw))
            insert(db, 'requests', (marked, raw[:-1] + r',"\u0061ttachment_owner_version":null}'))
            insert(db, 'asks', (marked, 'Fictional alias.', time.time() - messaging.RETENTION - 2, None))
            db.commit()
            self.assertEqual(self.store._attachment_reservation(ordinary), CLEAR)
            self.assertEqual(self.store._attachment_reservation(marked), MARKER)
            self.assertEqual([row['request_id'] for row in self.store._rows()], [ordinary])
            next_ask = uid()
            self.assertIsNone(self.store.claim_ask(next_ask, 'Fictional separate ask.'))
            self.assertEqual(set(db.execute('SELECT id FROM asks')), {(marked,), (next_ask,)})
            self.assert_no_native_schema(version)
        self.in_both_uninstalled_modes(exercise)

    def test_stock_mod_put_cannot_replace_linked_payload(self):
        ledger = self.installed()
        value = admission(self.agent)
        ledger.admit(value, now_ms=int(time.time() * 1000))
        identity = value.owner_request_id
        old = self.store.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone()
        ordinary = self.row(uid(), state='queued')
        self.store.db.execute('BEGIN IMMEDIATE')
        self.store.mods.put(ordinary)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.mods.put(self.row(identity, state='queued'))
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone(), old)
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (ordinary['request_id'],)).fetchone())
        with self.store.db:
            self.store.mods.put(ordinary)
        self.assertEqual([row['request_id'] for row in self.store._rows()], [ordinary['request_id']])

    def test_stock_offline_peer_retirement_cannot_mutate_linked_id(self):
        self.installed()
        protected, ordinary = uid(), uid()
        for identity in (protected, ordinary):
            row = self.row(identity, state='queued', origin='peer', bridge_version=2)
            meta = (uid(), self.agent, '{}', 'hash', identity, 'credential', row['created'], row['created'] + .1, 'preview')
            insert(self.store.db, 'shared_bridge_requests', meta)
            insert(self.store.db, 'requests', (identity, json.dumps(row)))
        held_key(self.store.db, protected)
        self.store.db.commit()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA recursive_triggers').fetchone(), (0,))
            before = db.execute('SELECT data FROM requests WHERE id=?', (protected,)).fetchone()
            with self.assertRaises(sqlite3.IntegrityError), peer_bridge.transaction(db):
                peer_lifecycle.retire(db, protected, peer_lifecycle.RETIRE_REASON, expected_agent=self.agent)
            self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (protected,)).fetchone(), before)
            self.assertIsNone(peer_lifecycle.get(db, protected))
            with peer_bridge.transaction(db):
                result = peer_lifecycle.retire(db, ordinary, peer_lifecycle.RETIRE_REASON, expected_agent=self.agent)
            self.assertEqual(result['execution'], 'retired')
            self.assertEqual(db.execute('SELECT request_id FROM peer_execution_events').fetchall(), [(ordinary,)])

    def test_constructor_and_owner_snapshot_filter_pairs_before_body_decode(self):
        link_only, outcome_only, orphan, ordinary = (uid() for _ in range(4))
        def seed(db):
            for identity in (link_only, outcome_only):
                db.execute('INSERT INTO requests VALUES (?,CAST(X\'FF\' AS TEXT))', (identity,))
            held_key(db, link_only)
            held_key(db, outcome_only, table=guards.OUTCOMES)
            insert(db, 'requests', (orphan, '{"attachment_owner_version":1,"state":"sending"}'))
            insert(db, 'requests', (ordinary, json.dumps(self.row(ordinary, state='sending'))))
        self.installed(seed, bind=False)
        rows = self.store.snapshot()['requests']
        self.assertEqual([(row['request_id'], row['state']) for row in rows], [(ordinary, 'unknown')])
        self.assertEqual(self.store.mods.rows(), [])
        for identity in (link_only, outcome_only, orphan):
            with self.subTest(identity=identity), self.assertRaises(messaging.MessagingError) as refused:
                self.new_message(identity=identity)
            self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone(), (4,))

    def test_startup_peer_conflict_refuses_before_ordinary_migration(self):
        for source in ('linked', 'orphan', 'uninstalled-orphan'):
            with self.subTest(source=source):
                if self.store is None:
                    # The preceding contradictory fixture is held. Use a separate
                    # state directory for the other startup scenario.
                    self.state = Path(self.temp.name) / ('state-' + source)
                    self.store = self.open_store()
                    self.path = self.state / 'officer-messages.sqlite3'
                if source != 'uninstalled-orphan':
                    owner.OwnerAttachmentLedger(self.store.db, self.store.lock)
                identity = self.peer(state='answered')
                if source == 'linked':
                    held_key(self.store.db, identity)
                else:
                    marker = r'{"\u0061ttachment_owner_version":1}'
                    self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (marker, identity))
                self.store.db.commit()
                if source != 'uninstalled-orphan':
                    install(self.store.db)
                before = self.store.db.execute('SELECT id,data FROM requests').fetchall()
                self.close_store()
                with patch.object(peer_lifecycle, 'migrate', wraps=peer_lifecycle.migrate) as migration:
                    with self.assertRaisesRegex(guards.OwnerGuardError, 'needs recovery before peer migration'):
                        self.open_store()
                migration.assert_not_called()
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute('SELECT id,data FROM requests').fetchall(), before)
                    if source == 'uninstalled-orphan':
                        installed = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'owner_attachment_%'").fetchall()
                        self.assertEqual(installed, [])

    def test_ordinary_owner_routes_refuse_alias_before_managed_lookup(self):
        ledger = self.installed()
        value = admission(self.agent)
        identity = value.owner_request_id
        ledger.admit(value, now_ms=int(time.time() * 1000))
        manager = Mock()
        self.store.managed = manager
        actions = (
            lambda: self.new_message(identity=identity),
            lambda: self.store.claim_ask(identity, 'Fictional question.'),
            lambda: self.store.decide_ask(identity, {'kind': 'none'}),
            lambda: self.store.submit_control('interrupt', self.agent, '', identity, self.agents),
        )
        for action in actions:
            with self.assertRaises(messaging.MessagingError) as refusal:
                action()
            self.assertEqual(refusal.exception.status, 409)
        with patch.object(server, 'MESSAGING', self.store), patch.object(server, 'claude_permission_ownership', return_value={'classification': 'ordinary'}) as ownership:
            with self.assertRaises(messaging.MessagingError) as refusal:
                server.owner_target_call('owner_action', self.agent, 'Fictional question.', identity)
        self.assertEqual(refusal.exception.status, 409)
        ownership.assert_not_called()
        self.assertEqual(manager.mock_calls, [])
        current = ledger.status(identity)
        self.assertEqual(current['state'], 'owner_recorded')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM asks').fetchone(), (0,))

    def test_stale_worker_selection_does_not_redispatch_or_overwrite(self):
        ledger = self.installed()
        value = admission(self.agent)
        identity = value.owner_request_id
        ledger.admit(value, now_ms=int(time.time() * 1000))
        ordinary = self.new_message()
        raw = self.store.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone()
        for state in ('sending', 'queued'):
            stale = self.row(identity, state=state)
            prune = self.store._prune
            with patch.object(self.store, '_prune', side_effect=lambda rows, now: [stale] + prune(rows, now)), \
                    patch.object(self.store, '_save', wraps=self.store._save) as save:
                launch, poll = self.mock_tick()
            self.assertNotIn(identity, [call.args[0]['request_id'] for call in save.call_args_list])
            self.assertNotIn(identity, [call.args[0]['request_id'] for call in poll.call_args_list])
            self.assertEqual([call.args[0]['request_id'] for call in launch.call_args_list], [ordinary['request_id']] if state == 'sending' else [])
        self.assertEqual(self.store.db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone(), raw)
        self.assertEqual(ledger.status(identity)['state'], 'owner_recorded')

    def test_save_and_final_peer_reconciliation_recheck_protected_identity(self):
        ledger = self.installed()
        value = admission(self.agent)
        identity = value.owner_request_id
        ledger.admit(value, now_ms=int(time.time() * 1000))
        ordinary = self.new_message()
        held = self.row(identity)
        with patch.object(peer_lifecycle, 'reconcile') as writes:
            with self.assertRaises(guards.OwnerGuardError):
                self.store._save(held)
            writes.assert_not_called()
        def captured(rows):
            self.assertEqual([row['request_id'] for row in rows], [ordinary['request_id']])
        original = self.store._rows
        calls = 0
        def late_snapshot():
            nonlocal calls
            calls += 1
            if calls == 2:
                return [held, *original()]
            return original()
        with patch.object(self.store.mods, 'maintain'), patch.object(self.store, '_rows', side_effect=late_snapshot), \
                patch.object(self.store.peers, 'reconcile', side_effect=captured) as reconcile:
            self.mock_tick()
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(ledger.status(identity)['state'], 'owner_recorded')

    def test_post_launch_owner_check_rejects_displaced_worker_row(self):
        self.installed()
        ordinary = self.new_message()
        identity = ordinary['request_id']
        save = self.store._save
        def changed_after_launch(row, **options):
            save(row, **options)
            if row['request_id'] == identity and row['launched'] and row['state'] == 'sending':
                self.store.db.execute('DELETE FROM requests WHERE id=?', (identity,))
                held_key(self.store.db, identity, table=guards.OUTCOMES)
                self.store.db.commit()
        with patch.object(self.store, '_save', side_effect=changed_after_launch):
            launch, poll = self.mock_tick()
        launch.assert_not_called()
        poll.assert_not_called()
        self.assertTrue(self.store._attachment_guard.reserved(identity))
        self.assertIsNone(self.store.db.execute('SELECT id FROM requests WHERE id=?', (identity,)).fetchone())

    def test_prune_excludes_both_paths_and_aged_shared_collision(self):
        ledger = self.installed()
        protected = admission(self.agent)
        now = time.time()
        identity = protected.owner_request_id
        ledger.admit(protected, now_ms=int((now - messaging.RETENTION - 2) * 1000))
        shared_id = self.peer(age=messaging.RETENTION + 2)
        collision = self.peer(age=messaging.RETENTION + 2)
        held_key(self.store.db, collision)
        ordinary = uid()
        insert(self.store.db, 'requests', (ordinary, json.dumps(self.row(ordinary, age=messaging.RETENTION + 2))))
        self.store.db.commit()
        stale = self.row(identity, age=messaging.RETENTION + 2)
        self.store._prune([stale, *self.store._rows()], now)
        self.assertEqual(set(r[0] for r in self.store.db.execute('SELECT id FROM requests')), {identity, collision})
        self.assertEqual(dict(self.store.db.execute('SELECT transport_id,preview FROM shared_bridge_requests')),
                         {shared_id: '', collision: 'original preview'})
        self.assertEqual(ledger.status(identity)['state'], 'owner_recorded')

    def test_control_alias_only_holds_peer_capture_until_rechecked(self):
        self.installed()
        unrelated = self.new_message()
        peer = self.peer()
        collision = uid()
        insert(self.store.db, 'controls', (collision, 'not JSON'))
        held_key(self.store.db, collision, table=guards.OUTCOMES)
        self.store.db.commit()
        with patch('sys.stderr') as diagnostics:
            send, poll = self.mock_tick()
        self.assertIn('Peer reply capture is held', ''.join(call.args[0] for call in diagnostics.write.call_args_list))
        self.assertTrue(self.store._attachment_reconciliation_held)
        self.assertEqual([call.args[0]['request_id'] for call in send.call_args_list], [unrelated['request_id']])
        self.assertIn(unrelated['request_id'], [call.args[0]['request_id'] for call in poll.call_args_list])
        retained = json.loads(self.store.db.execute('SELECT data FROM requests WHERE id=?', (peer,)).fetchone()[0])
        self.assertEqual((retained['state'], retained['candidate_reply']), ('quarantined', 'A fictional peer answer.'))
        self.assertEqual(self.store.snapshot()['controls'], [])
        # Fixture repair only. The actual implementation never deletes a hold.
        self.store.db.execute(f'DELETE FROM {guards.OUTCOMES} WHERE owner_request_id=?', (collision,))
        self.store.db.execute('DELETE FROM controls WHERE id=?', (collision,))
        self.store.db.commit()
        self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(self.row(unrelated['request_id'])), unrelated['request_id']))
        self.store.db.commit()
        self.mock_tick()
        self.assertFalse(self.store._attachment_reconciliation_held)
        result = json.loads(self.store.db.execute('SELECT data FROM requests WHERE id=?', (peer,)).fetchone()[0])
        self.assertEqual((result['state'], result['reply']), ('answered', 'A fictional peer answer.'))

    def test_installed_without_capacity_binding_keeps_exact_replay_and_asks(self):
        prior = self.new_message()
        self.installed(bind=False)
        self.assertEqual(self.new_message(identity=prior['request_id'])['state'], 'unknown')
        with self.assertRaises(messaging.MessagingError) as error:
            self.new_message('Another fictional question.')
        self.assertEqual(error.exception.status, 503)
        self.assertEqual([row['request_id'] for row in self.store.snapshot()['requests']], [prior['request_id']])
        self.assertIsNone(self.store.claim_ask(uid(), 'Fictional independent ask.'))

    def test_same_writer_last_slot_race_between_ordinary_and_native(self):
        ledger = self.installed()
        for _ in range(messaging.MAX_PENDING - 1):
            identity = uid()
            insert(self.store.db, 'requests', (identity, json.dumps(self.row(identity, state='unknown'))))
        self.store.db.commit()
        ledger.rebuild_directory()
        barrier = threading.Barrier(2)
        native = admission(self.agent)
        ordinary = uid()
        now = int(time.time() * 1000)
        def ordinary_call():
            barrier.wait(timeout=3)
            try:
                return 'ordinary', self.new_message(identity=ordinary)['state']
            except messaging.MessagingError as error:
                return 'ordinary', error.status
        def native_call():
            barrier.wait(timeout=3)
            try:
                return 'native', ledger.admit(native, now_ms=now)['state']
            except owner.LedgerError as error:
                return 'native', error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(ordinary_call), pool.submit(native_call)
            results = dict((first.result(timeout=5), second.result(timeout=5)))
        won = [result for result in results.values() if result in ('sending', 'owner_recorded')]
        self.assertEqual(len(won), 1)
        if results['ordinary'] == 'sending':
            self.assertIn(results['native'], ('pending_capacity', 'attachment_index_unavailable'))
        else:
            self.assertEqual(results, {'ordinary': 429, 'native': 'owner_recorded'})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone(), (messaging.MAX_PENDING,))
        ledger.rebuild_directory()
        with self.assertRaises(messaging.MessagingError) as error:
            self.new_message()
        self.assertEqual(error.exception.status, 429)

    def test_terminal_ack_releases_pending_and_late_evidence_does_not_reopen(self):
        ledger = self.installed()
        for _ in range(messaging.MAX_PENDING - 1):
            identity = uid()
            insert(self.store.db, 'requests', (identity, json.dumps(self.row(identity, state='unknown'))))
        self.store.db.commit()
        ledger.rebuild_directory()
        native = admission(self.agent)
        identity = native.owner_request_id
        now = int(time.time() * 1000)
        digest = ledger.admit(native, now_ms=now)['admission_digest']
        attempt = ledger.record_claim(identity, digest, now_ms=now + 1)['attempt_id']
        result = ledger.record_outcome(identity, digest, attempt, 'ended', evidence_sequence=1,
                                       now_ms=now + 2, turn_id='fictional-turn')['outcome']
        with self.assertRaises(messaging.MessagingError) as refusal:
            self.new_message()
        self.assertEqual(refusal.exception.status, 429)
        ledger.acknowledge(identity, digest, result['seq'], result['digest'])
        late = ledger.record_outcome(identity, digest, attempt, 'reply', evidence_sequence=2,
                                     now_ms=now + 3, turn_id='fictional-turn', reply='Fictional answer.')
        self.assertTrue(late['terminal_acked'] and late['pending_ack'])
        self.assertEqual(self.new_message()['state'], 'sending')

    def test_history_and_rate_are_combined_and_fake_peer_cannot_hide_usage(self):
        for kind, count in (('history', messaging.MAX_RECORDS - 1), ('rate', 9)):
            with self.subTest(kind=kind):
                if kind == 'rate':
                    self.close_store()
                    self.state = Path(self.temp.name) / 'second-state'
                    self.store = self.open_store()
                ledger = self.installed()
                for _ in range(count):
                    identity = uid()
                    insert(self.store.db, 'requests', (identity, json.dumps(self.row(identity, age=1 if kind == 'rate' else 120))))
                self.store.db.commit()
                ledger.rebuild_directory()
                ledger.admit(admission(self.agent), now_ms=int(time.time() * 1000))
                with self.assertRaises(messaging.MessagingError) as error:
                    self.new_message()
                self.assertEqual(error.exception.status, 429)
                self.assertIn('storage limit' if kind == 'history' else 'last minute', str(error.exception))
        faker = uid()
        row = self.row(faker, origin='peer')
        insert(self.store.db, 'requests', (faker, json.dumps(row)))
        self.store.db.commit()
        ledger.rebuild_directory()
        with self.assertRaises(messaging.MessagingError) as error:
            self.new_message()
        self.assertEqual(error.exception.status, 503)
        with self.assertRaisesRegex(owner.LedgerError, 'owner_history_invalid'):
            ledger.admit(admission(self.agent), now_ms=int(time.time() * 1000))


@unittest.skipIf(os.name == 'nt', 'POSIX-private owner messaging storage')
class LargeLegacyPeerTests(unittest.TestCase):
    def test_f3_real_shared_send_and_poll_preserve_large_rows_and_unicode(self):
        # The shared fixture patches native launch. No provider or HTTP access.
        from test_shared_bridge import SharedTests
        shared = SharedTests()
        shared.setUp()
        self.addCleanup(shared.tearDown)
        owner.OwnerAttachmentLedger(shared.store.db, shared.store.lock)
        install(shared.store.db)

        def reopen():
            shared.store.close()
            shared.store = messaging.MessagingStore(shared.state, shared.sessions)
            shared.store.available = True
            shared.store.shared.directory = shared.directory
            shared.bridge = shared.store.shared

        reopen()
        unicode_body = '😀' * peer_bridge.MAX_PEER_TEXT
        result = shared.send(text=unicode_body)
        client_id = result['request_id']
        self.assertEqual(result['state'], 'sending')
        shared.tick().assert_called_once()
        initial = shared.row(client_id)
        identity, transcript = initial['request_id'], initial['path']
        self.assertEqual(initial['question'], unicode_body)
        for pair in range(5):
            for index in (pair * 2, pair * 2 + 1):
                payload = {'type': 'task_started', 'turn_id': 'x' * 900000 + str(index)}
                event = {'type': 'event_msg', 'payload': payload}
                self.assertLess(len(json.dumps(event).encode()), messaging.MAX_LINE)
                shared.append(transcript, 'event_msg', payload)
            shared.tick().assert_not_called()
        db = shared.store.db
        body = db.execute('SELECT data FROM requests WHERE id=?', (identity,)).fetchone()[0]
        self.assertGreater(len(body.encode('ascii')), 9900000)
        self.assertEqual(shared.store._attachment_guard.reservation(identity), CLEAR)
        self.assertEqual(len(shared.row(client_id)['peer_turn_starts']), 10)
        with db:
            self.assertEqual(db.execute('UPDATE requests SET data=data WHERE id=?', (identity,)).rowcount, 1)
        late = body[:-1] + r',"\u0061ttachment_owner_version":null}'
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'owner request storage protection conflict'):
            db.execute('INSERT INTO requests VALUES (?,?)', (uid(), late))
        reopen()
        self.assertEqual(shared.row(client_id)['state'], 'queued')
        self.assertEqual(shared.row(client_id)['question'], unicode_body)
        shared.answer(shared.row(client_id), text=unicode_body)
        shared.tick().assert_not_called()
        final = shared.row(client_id)
        self.assertEqual((final['state'], final['reply']), ('answered', unicode_body))
        self.assertEqual(len(final['peer_turn_starts']), 11)
        self.assertEqual(shared.store._attachment_guard.reservation(identity), CLEAR)
        self.assertEqual(shared.bridge.result(client_id)['state'], 'unknown')  # Existing wire ceiling.


if __name__ == '__main__':
    unittest.main()
