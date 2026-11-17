"""Offline L1 tests: real owner schema/SQLite, fictional evidence, no transports."""
import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import attachments_owner_transport as m
import peer_bridge

NOW = 1_800_000_000_000
TURN = 'fictional-turn-1'


class FaultConnection(sqlite3.Connection):
    commit_fault = ''
    def commit(self):
        if self.commit_fault == 'before':
            raise sqlite3.OperationalError('fictional commit fault')
        super().commit()
        if self.commit_fault == 'after':
            raise sqlite3.OperationalError('fictional lost acknowledgment')


def owner_schema(db):
    peer_bridge.shared_schema(db)
    db.execute('CREATE TABLE controls(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    db.execute('CREATE TABLE expired_controls(id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE asks(id TEXT PRIMARY KEY,user_text TEXT NOT NULL,created REAL NOT NULL,decision TEXT)')
    db.execute('CREATE TABLE request_fulfillments(request_id TEXT PRIMARY KEY,control_request_id TEXT NOT NULL,confirmed_at REAL NOT NULL)')
    db.commit()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'fictional-owner.sqlite3')
        self.db = sqlite3.connect(self.path, factory=FaultConnection)
        owner_schema(self.db)
        self.lock = threading.RLock()
        self.ledger = m.OwnerAttachmentLedger(self.db, self.lock)
        self.a = self.admission()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def admission(self, **kw):
        env = 'Fictional owner attachment message. Do not run tools.'
        a = m.Admission(str(uuid.uuid4()), 'attachment-fictional', 'claim-fictional', 'e' * 64,
                        'f' * 64, hashlib.sha256(env.encode()).hexdigest(), 'codex', 'fictional-profile',
                        'v1', 'b' * 64, 'x:' + str(uuid.uuid4()), 'Review the fictional note.', env, 'Orchard')
        return replace(a, **kw)

    def admit(self, a=None, at=NOW):
        self.a = a or self.a
        return self.ledger.admit(self.a, now_ms=at)

    def status(self):
        return self.ledger.status(self.a.owner_request_id)

    def snapshot(self):
        # Fixture lookup deliberately avoids a transaction: injected commit faults
        # must strike the operation under test, not this preparatory read.
        with self.lock:
            link, outcome, _ = self.ledger._read(self.a.owner_request_id)
            return self.ledger._status(link, outcome)

    def claim(self):
        s = self.snapshot()
        return self.ledger.record_claim(self.a.owner_request_id, s['admission_digest'], now_ms=NOW + 1)

    def event(self, kind, **kw):
        s = self.snapshot()
        return self.ledger.record_outcome(self.a.owner_request_id, s['admission_digest'], s['attempt_id'], kind,
                                         evidence_sequence=kw.pop('evidence_sequence', s['outcome']['evidence_sequence'] + 1),
                                         now_ms=kw.pop('now_ms', NOW + 2), **kw)

    def ack(self, s=None):
        s = s or self.snapshot()
        return self.ledger.acknowledge(self.a.owner_request_id, s['admission_digest'], s['outcome']['seq'], s['outcome']['digest'])

    def prune(self):
        return self.ledger.prune_payload(self.a.owner_request_id, self.snapshot()['admission_digest'], now_ms=NOW + m.RETENTION_MS + 1)

    def error(self, code, fn):
        with self.assertRaises(m.LedgerError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def rows(self):
        return tuple(self.db.execute('SELECT * FROM ' + t).fetchall()
                     for t in ('owner_attachment_links', 'owner_attachment_outcomes', 'requests'))

    def ordinary(self, state='answered', *, created=None, origin=None):
        key = str(uuid.uuid4())
        row = dict(request_id=key, state=state, created=(NOW - 61_000) / 1000 if created is None else created)
        if origin:
            row['origin'] = origin
        self.db.execute('INSERT INTO requests VALUES (?,?)', (key, json.dumps(row)))
        if origin == 'peer':
            self.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,NULL)',
                            ('fictional-sender', key, 'fictional-target', 'hash', key, NOW / 1000))
        self.db.commit()
        self.ledger.rebuild_directory()  # Explicit fixture maintenance after out-of-band SQL.
        return key

    def test_ledger_construction_is_limited_to_its_owner_runtime(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        offline = {'attachments_native_mirror', 'attachments_native_handoff'}
        owner_modules = {'attachments_owner_transport', 'attachments_owner_directory', 'attachments_owner_runtime'}
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            existing_owner_module = path.stem in owner_modules and path.parent == root
            forbidden = offline | {'codex_attachment_adapter'}
            if path == root / 'attachments_native_handoff.py':
                forbidden -= {'attachments_native_mirror', 'codex_attachment_adapter'}
            if not existing_owner_module:
                forbidden.add('attachments_owner_directory')
                if path.stem not in offline or path.parent != root:
                    forbidden.add('attachments_owner_transport')
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if path == root / 'claude_attachment_adapter.py' and node.module in (
                            'attachments_owner_transport', 'attachments_native_handoff'):
                        expected = {
                            'attachments_owner_transport': ['CLAUDE_CLAIMS', 'CLAUDE_CLAIM_BYTES',
                                'CLAUDE_COMPLETION_PROFILE', 'LINK_BYTES', 'MAX_LINKS', 'LedgerError',
                                'OwnerAttachmentLedger', 'canonical_uuid', 'decode', 'digest', 'hex_digest',
                                'integer', 'keys', 'padded', 'text'],
                            'attachments_native_handoff': ['_NativeCopyProof', 'target_binding_digest'],
                        }
                        self.assertEqual([(part.name, part.asname) for part in node.names],
                                         [(name, None) for name in expected[node.module]])
                        continue
                    if path == root / 'codex_attachment_dispatch.py' and node.module in offline:
                        expected = {'attachments_native_handoff': ['_NativeCopyProof'],
                                    'attachments_native_mirror': ['_conflict', 'target_binding_digest']}
                        self.assertEqual([(part.name, part.asname) for part in node.names],
                                         [(name, None) for name in expected[node.module]])
                        continue
                    if path == root / 'codex_attachment_adapter.py' and node.module == 'attachments_owner_transport':
                        self.assertEqual([(part.name, part.asname) for part in node.names], [('digest', None)])
                        continue
                    if path.parent == root and path.name in ('claude_mod.py', 'attachments_native_composition.py',
                            'attachments_claude_service.py') and node.module == 'attachments_owner_transport':
                        self.assertEqual([(part.name, part.asname) for part in node.names], [('LedgerError', None)])
                        continue
                    if path == root / 'attachments_native_composition.py' and node.module in offline:
                        expected = {'attachments_native_handoff': ['FictionalAdapterBinding', 'OfflineNativeHandoff'],
                                    'attachments_native_mirror': ['NativeOutcomeMirror']}
                        self.assertEqual([(part.name, part.asname) for part in node.names],
                                         [(name, None) for name in expected[node.module]])
                        continue
                    self.assertNotIn(node.module, forbidden, str(path))
                elif isinstance(node, ast.Import):
                    self.assertTrue(forbidden.isdisjoint(n.name for n in node.names), str(path))
            if not existing_owner_module:
                self.assertNotIn('OwnerAttachmentLedger(', path.read_text(encoding='utf-8'), str(path))
                self.assertNotIn('OwnerAttachmentDirectory(', path.read_text(encoding='utf-8'), str(path))
            if path.parent == root and path.name in ('claude_attachment_adapter.py', 'codex_attachment_dispatch.py'):
                self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                     and node.func.id in ('OwnerAttachmentLedger', 'OfflineNativeHandoff', '_NativeCopyProof')
                                     for node in ast.walk(tree)))

    def test_schema_requires_real_owner_namespaces(self):
        db = sqlite3.connect(':memory:')
        try:
            self.error('owner_schema_missing', lambda: m.OwnerAttachmentLedger(db, threading.RLock()))
            self.assertFalse(db.in_transaction)
        finally:
            db.close()

    def test_atomic_admission_fixed_rows_and_duplicate(self):
        first = self.admit()
        before = self.rows()
        self.assertIs(first['new_admission'], True)
        self.assertEqual(self.admit(), dict(first, new_admission=False))
        self.assertEqual(self.rows(), before)
        self.assertEqual(first['outcome']['seq'], 1)
        self.assertEqual(first['state'], 'owner_recorded')
        self.assertFalse(first['reply_available'])
        self.assertEqual(len(before[0][0][1]), m.LINK_BYTES)
        self.assertEqual(len(before[1][0][1]), m.OUTCOME_BYTES)
        self.assertEqual(len(before[2][0][1].encode()), m.PAYLOAD_BYTES)

    def test_fingerprint_epoch_and_frozen_changes_conflict(self):
        self.admit()
        for changes in ({'fingerprint': 'a' * 64}, {'owner_epoch': 'new-epoch'},
                        {'handoff_claim_id': 'another'}, {'question': 'Different question'}, {'name': 'Changed'}):
            with self.subTest(changes=changes):
                self.error('identity_conflict', lambda: self.ledger.admit(replace(self.a, **changes), now_ms=NOW))

    def test_admission_rollback_at_each_insert(self):
        for table in ('owner_attachment_links', 'owner_attachment_outcomes', 'requests'):
            with self.subTest(table=table):
                self.db.execute('CREATE TEMP TRIGGER fault BEFORE INSERT ON ' + table + " BEGIN SELECT RAISE(ABORT,'fictional fault'); END")
                with self.assertRaises(sqlite3.IntegrityError):
                    self.admit()
                self.assertEqual(self.rows(), ([], [], []))
                self.db.execute('DROP TRIGGER fault')
                self.ledger.rebuild_directory()

    def test_admission_commit_failure_rollback_and_lost_ack_recovery(self):
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.admit()
        self.assertEqual(self.rows(), ([], [], []))
        self.db.commit_fault = ''
        self.ledger.rebuild_directory()
        self.db.commit_fault = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.admit()
        self.db.commit_fault = ''
        self.assertEqual(self.admit()['outcome']['seq'], 1)
        self.assertEqual(len(self.rows()[0]), 1)

    def test_namespace_collisions(self):
        inserts = {
            'requests': ('INSERT INTO requests VALUES (?,?)', ('{}',)),
            'controls': ('INSERT INTO controls VALUES (?,?)', ('{}',)),
            'expired_controls': ('INSERT INTO expired_controls VALUES (?)', ()),
            'asks': ('INSERT INTO asks VALUES (?,?,?,NULL)', ('fictional', NOW / 1000)),
            'request_fulfillments': ('INSERT INTO request_fulfillments VALUES (?,?,?)', ('another', NOW / 1000)),
            'peer_requests': ('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,NULL)', ('client', 'target', 'digest', 'placeholder', NOW / 1000)),
            'shared_bridge_requests': ('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,NULL,?)', ('target', '{}', 'hash', 'placeholder', 'credential', NOW / 1000, '')),
            'peer_execution': ('INSERT INTO peer_execution VALUES (?,?,?,?,?,?)', ('x:fictional', NOW / 1000, 'unknown', 'withheld', '{}')),
            'peer_execution_events': ('INSERT INTO peer_execution_events VALUES (?,?,?,?,?,NULL,?,?,?)', ('placeholder', 'kind', 'reason', NOW / 1000, '', '', 'hash')),
            'peer_reply_admissions': ('INSERT INTO peer_reply_admissions VALUES (?,?)', ('hash',)),
        }
        for table, (sql, tail) in inserts.items():
            with self.subTest(table=table):
                key = self.a.owner_request_id
                values = (key,) + tail
                if table == 'peer_requests':
                    values = ('sender', 'client', 'target', 'digest', key, NOW / 1000)
                elif table == 'shared_bridge_requests':
                    values = ('client', 'target', '{}', 'hash', key, 'credential', NOW / 1000, '')
                elif table == 'peer_execution_events':
                    values = ('event', key, 'kind', 'reason', NOW / 1000, '', '', 'hash')
                self.db.execute(sql, values)
                self.db.commit()
                self.error('namespace_conflict', self.admit)
                self.db.execute('DELETE FROM ' + table)
                self.db.commit()
        self.db.execute('INSERT INTO request_fulfillments VALUES (?,?,?)', ('another', self.a.owner_request_id, 0))
        self.db.commit()
        self.error('namespace_conflict', self.admit)

    def test_namespace_collision_after_admission_is_quarantined(self):
        self.admit()
        self.db.execute('INSERT INTO expired_controls VALUES (?)', (self.a.owner_request_id,))
        self.db.commit()
        self.error('namespace_conflict', self.status)

    def test_history_capacity_mixed_owner_and_linked(self):
        self.admit()
        self.claim(); self.event('ended', turn_id=TURN); self.ack()
        for _ in range(m.MAX_HISTORY - 1):
            self.ordinary()
        self.error('history_capacity', lambda: self.admit(self.admission(), at=NOW + 61_000))

    def test_pending_capacity_mixed_and_ack_release(self):
        self.admit()
        for _ in range(m.MAX_PENDING - 1):
            self.ordinary('unknown')
        self.error('pending_capacity', lambda: self.ledger.admit(self.admission(), now_ms=NOW + 61_000))
        self.claim(); self.event('ended', turn_id=TURN)
        self.error('pending_capacity', lambda: self.ledger.admit(self.admission(), now_ms=NOW + 61_000))
        self.ack()
        self.assertEqual(self.ledger.admit(self.admission(), now_ms=NOW + 61_000)['state'], 'owner_recorded')

    def test_peer_history_does_not_consume_owner_capacity(self):
        for _ in range(260):
            self.ordinary('unknown', origin='peer')
        self.assertEqual(self.admit()['state'], 'owner_recorded')

    def test_rate_limit_includes_linked_admissions(self):
        for _ in range(m.MAX_RATE):
            self.admit(self.admission()); self.claim(); self.event('not_submitted'); self.ack()
        self.error('rate_capacity', lambda: self.admit(self.admission()))
        self.assertEqual(self.admit(self.admission(), at=NOW + 60_001)['state'], 'owner_recorded')

    def test_retained_link_limit_counts_tombstones(self):
        # Exercise the same count branch with a reduced cap; constants are asserted separately.
        self.assertEqual((m.MAX_LINKS, m.MAX_HISTORY, m.MAX_PENDING), (10_000, 256, 8))
        self.admit(); self.claim(); self.event('ended', turn_id=TURN); self.ack(); self.prune()
        with patch.object(m, 'MAX_LINKS', 1):
            self.error('link_capacity', lambda: self.ledger.admit(self.admission(), now_ms=NOW + 61_000))

    def test_actual_10000_tombstones_reject_admission_without_eviction(self):
        self.admit(); self.claim(); self.event('ended', turn_id=TURN); self.ack(); self.prune()
        link, outcome, _ = self.ledger._read(self.a.owner_request_id)
        links, outcomes = [], []
        for i in range(1, m.MAX_LINKS):
            key = str(uuid.UUID(int=i))
            l = json.loads(json.dumps(link)); o = json.loads(json.dumps(outcome))
            l['identity']['owner_request_id'] = key
            l['admission_digest'] = m.digest('watch-owner-attachment-admission-v1', l['identity'])
            o.update(owner_request_id=key, admission_digest=l['admission_digest'])
            m._seal_outcome(o)
            l['ack_digest'] = o['digest']
            m._seal_link(l)
            links.append((key, m.padded(l, m.LINK_BYTES)))
            outcomes.append((key, m.padded(o, m.OUTCOME_BYTES)))
        self.db.executemany('INSERT INTO owner_attachment_links VALUES (?,?)', links)
        self.db.executemany('INSERT INTO owner_attachment_outcomes VALUES (?,?)', outcomes)
        self.db.commit()
        self.ledger.rebuild_directory()
        self.error('link_capacity', lambda: self.ledger.admit(self.admission(), now_ms=NOW + 61_000))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM owner_attachment_links').fetchone()[0], 10_000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM owner_attachment_outcomes').fetchone()[0], 10_000)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_claim_one_attempt_separate_turn_and_ack_identity(self):
        s1 = self.admit()
        s2 = self.claim()
        self.assertTrue(s2['new_claim'])
        self.assertEqual(s2['outcome']['seq'], 2)
        self.assertNotEqual(s2['attempt_id'], self.a.owner_request_id)
        self.assertEqual(s2['turn_id'], '')
        raw = self.rows()
        again = self.claim()
        self.assertFalse(again['new_claim'])
        self.assertEqual(self.rows(), raw)
        a2 = self.ack(s2)
        raw = self.rows()
        self.assertEqual(self.ack(s2), a2)
        self.assertEqual(self.rows(), raw)
        s3 = self.event('ended', turn_id=TURN)
        self.assertEqual(s3['outcome']['seq'], 3)
        self.assertEqual(s3['admission_digest'], s1['admission_digest'])
        a3 = self.ack(s3)
        raw = self.rows()
        self.assertEqual(self.ack(s3), a3)
        self.assertEqual(self.rows(), raw)
        self.assertEqual(self.ack(s2)['acknowledgment'], 'stale')
        self.assertEqual(self.rows(), raw)
        self.assertEqual(a3['outcome']['digest'], s3['outcome']['digest'])

    def test_mutators_require_exact_nonempty_admission_identity(self):
        self.admit(); self.claim()
        s = self.status()
        for invalid in (None, '', 'a' * 64):
            calls = (
                lambda: self.ledger.record_claim(self.a.owner_request_id, invalid, now_ms=NOW),
                lambda: self.ledger.record_outcome(self.a.owner_request_id, invalid, s['attempt_id'], 'unknown', evidence_sequence=1, now_ms=NOW),
                lambda: self.ledger.acknowledge(self.a.owner_request_id, invalid, s['outcome']['seq'], s['outcome']['digest']),
                lambda: self.ledger.prune_payload(self.a.owner_request_id, invalid, now_ms=NOW + m.RETENTION_MS + 1),
            )
            for call in calls:
                before = self.rows()
                with self.assertRaises(m.LedgerError):
                    call()
                self.assertEqual(self.rows(), before)

    def test_coalesced_outcomes_preserve_cumulative_facts(self):
        self.admit(); self.claim()
        self.event('accepted'); self.event('started', turn_id=TURN); self.event('ended', turn_id=TURN)
        pending = self.ledger.pending_outcomes()
        self.assertEqual(len(pending), 1)
        o = pending[0]['outcome']
        self.assertTrue(o['accepted'] and o['started'] and o['execution_ended'])
        self.assertEqual(o['seq'], 5)
        self.ack()
        self.assertEqual(self.ledger.pending_outcomes(), [])

    def test_submitted_then_dropped_is_unknown_not_failed_or_terminal(self):
        self.a = self.admission(transport='claude', agent_id='c:' + str(uuid.uuid4()))
        self.admit(); self.claim(); self.event('accepted')
        dropped = self.event('dropped')
        self.assertEqual(dropped['state'], 'unknown')
        self.assertTrue(dropped['outcome']['accepted'])
        self.assertFalse(dropped['outcome']['execution_ended'])
        self.assertFalse(dropped['outcome']['not_submitted'])
        self.error('nonexecution_conflict', lambda: self.event('not_submitted'))
        self.ack()
        self.error('payload_pinned', self.prune)
        self.event('ended', turn_id=TURN); self.ack()
        self.assertTrue(self.prune()['payload_pruned'])

    def test_old_receipt_replay_cannot_clear_newer_uncertainty(self):
        self.admit(); self.claim(); self.event('accepted', evidence_sequence=1); self.event('dropped', evidence_sequence=2)
        before = self.rows()
        self.assertEqual(self.event('accepted', evidence_sequence=1)['evidence'], 'stale_evidence')
        self.assertEqual(self.rows(), before)
        self.event('unknown', evidence_sequence=3)
        before = self.rows()
        self.event('dropped', evidence_sequence=2); self.event('unknown', evidence_sequence=3, now_ms=NOW + 999)
        self.assertEqual(self.rows(), before)
        self.event('started', turn_id=TURN, evidence_sequence=4)
        self.assertEqual(self.status()['state'], 'running')
        before = self.rows()
        self.event('unknown', evidence_sequence=3); self.event('dropped', evidence_sequence=2); self.event('accepted', evidence_sequence=1)
        self.assertEqual(self.rows(), before)

    def test_fresh_uncertainty_after_progress_is_not_a_replay(self):
        self.admit(); self.claim()
        first = self.event('unknown', evidence_sequence=1)
        self.event('started', turn_id=TURN, evidence_sequence=2)
        fresh = self.event('unknown', evidence_sequence=3)
        self.assertEqual(fresh['state'], 'unknown')
        self.assertTrue(fresh['outcome']['started'] and fresh['outcome']['accepted'])
        self.assertEqual(fresh['outcome']['evidence_sequence'], 3)
        self.assertNotEqual(first['outcome']['evidence_fingerprint'], fresh['outcome']['evidence_fingerprint'])
        before = self.rows()
        self.assertEqual(self.event('unknown', evidence_sequence=1)['evidence'], 'stale_evidence')
        self.assertEqual(self.event('unknown', evidence_sequence=3, now_ms=NOW + 100000)['evidence'], 'duplicate')
        self.assertEqual(self.rows(), before)

    def test_consecutive_identical_uncertainty_is_fresh_evidence(self):
        self.admit(); self.claim()
        first = self.event('unknown', evidence_sequence=1)
        second = self.event('unknown', evidence_sequence=2)
        self.assertEqual(second['outcome']['seq'], first['outcome']['seq'] + 1)
        self.assertEqual(second['state'], 'unknown')
        self.assertEqual(second['evidence'], 'recorded')
        self.assertEqual(second['outcome']['evidence_sequence'], 2)

    def test_conflicting_evidence_key_does_not_change_record(self):
        self.admit(); self.claim(); self.event('unknown', evidence_sequence=1)
        before = self.rows()
        self.error('evidence_conflict', lambda: self.event('accepted', evidence_sequence=1))
        self.assertEqual(self.rows(), before)
        self.event('reply', evidence_sequence=2, turn_id=TURN, reply='Fictional answer')
        before = self.rows()
        self.error('evidence_conflict', lambda: self.event('reply', evidence_sequence=2, turn_id=TURN, reply='Different answer'))
        self.error('turn_conflict', lambda: self.event('reply', evidence_sequence=2, turn_id='another', reply='Fictional answer'))
        self.assertEqual(self.rows(), before)

    def test_evidence_gap_is_rejected_then_recovered_in_order(self):
        self.admit(); self.claim(); self.event('accepted', evidence_sequence=1)
        before = self.rows()
        self.error('evidence_gap', lambda: self.event('ended', evidence_sequence=3, turn_id=TURN))
        self.assertEqual(self.rows(), before)
        self.event('unknown', evidence_sequence=2)
        self.event('ended', evidence_sequence=3, turn_id=TURN)
        self.assertTrue(self.status()['outcome']['execution_ended'])
        self.assertEqual(self.status()['outcome']['evidence_sequence'], 3)
        before = self.rows()
        self.event('unknown', evidence_sequence=2)
        self.assertEqual(self.rows(), before)

    def test_acceptance_does_not_regress_known_start_or_terminal(self):
        self.admit(); self.claim(); self.event('started', turn_id=TURN)
        self.event('unknown')
        self.assertEqual(self.event('accepted')['state'], 'running')
        self.assertTrue(self.status()['outcome']['started'])
        self.event('ended', turn_id=TURN)
        for kind in ('accepted', 'started', 'unknown', 'dropped'):
            kw = {'turn_id': TURN} if kind == 'started' else {}
            self.assertEqual(self.event(kind, **kw)['state'], 'execution_ended')
        self.assertTrue(self.status()['outcome']['accepted'])

    def test_evidence_order_is_required_not_inferred_from_callback_time(self):
        self.admit(); self.claim(); s = self.status()
        with self.assertRaises(TypeError):
            self.ledger.record_outcome(self.a.owner_request_id, s['admission_digest'], s['attempt_id'], 'unknown', now_ms=NOW)
        for sequence in (0, -1, True, 1.0, m.MAX_INT + 1):
            self.error('invalid_integer', lambda: self.event('unknown', evidence_sequence=sequence))
        self.assertEqual(self.status()['outcome']['evidence_sequence'], 0)
        self.event('unknown', evidence_sequence=1, now_ms=NOW + 100)
        self.event('started', evidence_sequence=2, now_ms=NOW, turn_id=TURN)
        self.assertEqual(self.status()['state'], 'running')

    def test_evidence_frontier_survives_restart_and_lost_commit_ack(self):
        self.admit(); self.claim(); self.event('unknown', evidence_sequence=1)
        self.db.commit_fault = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.event('started', evidence_sequence=2, turn_id=TURN)
        self.db.commit_fault = ''
        self.db.close()
        self.db = sqlite3.connect(self.path, factory=FaultConnection)
        self.ledger = m.OwnerAttachmentLedger(self.db, self.lock)
        before = self.rows()
        self.assertEqual(self.event('started', evidence_sequence=2, turn_id=TURN, now_ms=NOW + 1000)['evidence'], 'duplicate')
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.event('unknown', evidence_sequence=3)['state'], 'unknown')

    def test_claim_commit_fault_before_and_after_commit(self):
        self.admit()
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.claim()
        self.db.commit_fault = ''
        self.assertEqual(self.status()['claim'], 'unclaimed')
        self.db.commit_fault = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.claim()
        self.db.commit_fault = ''
        retained = self.status()
        self.assertEqual(retained['claim'], 'claimed')
        again = self.claim()
        self.assertEqual(again['attempt_id'], retained['attempt_id'])
        self.assertFalse(again['new_claim'])

    def test_evidence_frontier_and_result_rollback_together(self):
        self.admit(); self.claim(); self.event('unknown', evidence_sequence=1)
        before = self.rows()
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.event('started', evidence_sequence=2, turn_id=TURN)
        self.db.commit_fault = ''
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.status()['outcome']['evidence_sequence'], 1)
        self.assertEqual(self.event('started', evidence_sequence=2, turn_id=TURN)['evidence'], 'recorded')
        self.assertEqual(self.status()['state'], 'running')

    def test_late_reply_order_and_ack_preserve_both_sequences(self):
        self.admit(); self.claim(); self.event('ended', evidence_sequence=1, turn_id=TURN); self.ack(); self.prune()
        late = self.event('reply', evidence_sequence=2, turn_id=TURN, reply='Fictional late answer')
        self.ack(late)
        before = self.rows()
        self.event('reply', evidence_sequence=2, now_ms=NOW + 200, turn_id=TURN, reply='Fictional late answer')
        self.event('ended', evidence_sequence=1, turn_id=TURN)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.status()['outcome']['evidence_sequence'], 2)
        self.assertFalse(self.status()['reply_available'])
        self.assertEqual(self.rows()[2], [])

    def test_dropped_before_submission_is_still_unknown(self):
        self.admit(); self.claim()
        s = self.event('dropped')
        self.assertEqual(s['state'], 'unknown')
        self.assertFalse(s['outcome']['not_submitted'])

    def test_known_nonexecution_rejects_later_acceptance(self):
        self.admit(); self.claim(); self.event('not_submitted')
        self.error('nonexecution_conflict', lambda: self.event('accepted'))
        self.ack(); self.prune()

    def test_attempt_and_turn_cannot_be_rebound(self):
        self.admit(); self.claim(); self.event('started', turn_id=TURN)
        self.error('turn_conflict', lambda: self.event('ended', turn_id='another-turn'))
        s = self.status()
        self.error('attempt_conflict', lambda: self.ledger.record_outcome(self.a.owner_request_id,
                   s['admission_digest'], str(uuid.uuid4()), 'accepted', evidence_sequence=2, now_ms=NOW))

    def test_unknown_cannot_erase_terminal_or_reply(self):
        self.admit(); self.claim(); self.event('reply', turn_id=TURN, reply='Fictional answer')
        before = self.status()
        self.event('unknown')
        self.assertEqual(self.status()['outcome']['seq'], before['outcome']['seq'] + 1)
        self.assertTrue(self.status()['outcome']['execution_ended'])
        self.assertEqual(self.status()['state'], 'answered')
        self.assertTrue(self.status()['reply_available'])

    def test_correlated_reply_retained_and_conflict_is_bounded(self):
        self.admit(); self.claim(); self.event('ended', turn_id=TURN)
        first = self.event('reply', turn_id=TURN, reply='Fictional answer')
        self.assertTrue(first['reply_available'])
        self.assertEqual(first['state'], 'answered')
        self.event('reply', turn_id=TURN, reply='Fictional conflict')
        self.assertEqual(self.status()['state'], 'unknown')
        self.assertFalse(self.status()['reply_available'])
        raw = self.rows()
        self.event('reply', turn_id=TURN, reply='Fictional conflict', evidence_sequence=3)
        self.assertEqual(self.rows(), raw)
        self.event('reply', turn_id=TURN, reply='Another conflict')
        self.assertEqual(len(self.rows()[0]), 1)
        self.assertEqual(len(self.rows()[1]), 1)
        self.assertEqual(self.status()['outcome']['reply_fingerprint'], hashlib.sha256(b'Fictional answer').hexdigest())

    def test_exact_late_tombstone_sequence_no_payload_no_replay(self):
        self.admit(); self.claim(); self.event('accepted'); terminal = self.event('ended', turn_id=TURN)
        self.assertEqual(terminal['outcome']['seq'], 4)
        self.ack(terminal)
        self.prune()
        self.assertEqual(self.status()['outcome'], terminal['outcome'])
        before_pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        late = self.event('reply', turn_id=TURN, reply='Fictional late answer')
        self.assertEqual(late['outcome']['seq'], 5)
        self.assertTrue(late['outcome']['execution_ended'] and late['outcome']['accepted'])
        self.assertTrue(late['outcome']['late_reply_observed'] and late['outcome']['capture_withheld'])
        self.assertFalse(late['reply_available'])
        self.assertEqual(late['state'], 'execution_ended')
        self.assertTrue(late['pending_ack'])
        self.assertTrue(late['terminal_acked'])
        self.assertEqual(self.ack(terminal)['acknowledgment'], 'stale')
        self.ack(late)
        raw = self.rows()
        self.ack(late); self.event('reply', turn_id=TURN, reply='Fictional late answer', evidence_sequence=late['outcome']['evidence_sequence'])
        self.assertEqual(self.rows(), raw)
        self.assertEqual(raw[2], [])
        self.assertNotIn(b'Fictional late answer', raw[0][0][1] + raw[1][0][1])
        self.assertEqual(self.db.execute('PRAGMA page_count').fetchone()[0], before_pages)
        self.error('retained_tombstone', self.admit)
        self.error('retained_tombstone', self.claim)

    def test_late_receipt_does_not_reoccupy_pending_execution_slot(self):
        self.admit(); self.claim(); self.event('ended', turn_id=TURN); self.ack(); self.prune()
        self.event('reply', turn_id=TURN, reply='Fictional late answer')
        for _ in range(7):
            self.ordinary('unknown')
        self.assertEqual(self.ledger.admit(self.admission(), now_ms=NOW + 61_000)['state'], 'owner_recorded')

    def test_prune_reply_race_in_both_serial_orders(self):
        self.admit(); self.claim(); self.event('ended', turn_id=TURN); old = self.ack()
        self.event('reply', turn_id=TURN, reply='Fictional reply before prune')
        self.error('payload_pinned', self.prune)
        self.assertEqual(self.ack(old)['acknowledgment'], 'stale')
        self.ack(); self.prune()
        self.assertFalse(self.status()['reply_available'])
        self.assertEqual(self.status()['state'], 'execution_ended')
        self.event('reply', turn_id=TURN, reply='Fictional reply after prune')
        self.assertFalse(self.status()['reply_available'])
        self.assertEqual(self.rows()[2], [])

    def test_pending_unknown_and_noack_never_prune(self):
        self.admit()
        self.error('payload_pinned', self.prune)
        self.claim(); self.event('unknown'); self.ack()
        self.error('payload_pinned', self.prune)
        self.event('ended', turn_id=TURN)
        self.error('payload_pinned', self.prune)

    def test_corrupt_link_outcome_and_orphan_fail_closed(self):
        self.admit()
        for table in ('owner_attachment_links', 'owner_attachment_outcomes'):
            raw = self.db.execute('SELECT record FROM ' + table).fetchone()[0]
            self.db.execute('UPDATE ' + table + ' SET record=?', (b'x' * len(raw),)); self.db.commit()
            self.error('invalid_encoding', self.status)
            self.db.execute('UPDATE ' + table + ' SET record=?', (raw,)); self.db.commit()
        self.db.execute('DELETE FROM requests'); self.db.commit()
        self.error('payload_missing', self.status)
        self.error('attachment_index_unavailable', lambda: self.ledger.admit(self.admission(), now_ms=NOW))
        self.ledger.rebuild_directory()
        # One corrupt request is conservatively charged, not a global admission fault.
        self.assertEqual(self.ledger.admit(self.admission(), now_ms=NOW)['state'], 'owner_recorded')

    def test_orphan_payload_not_adopted_or_counted_as_ordinary(self):
        key = str(uuid.uuid4())
        self.db.execute('INSERT INTO requests VALUES (?,?)', (key, '{"attachment_owner_version":1}')); self.db.commit()
        self.ledger.rebuild_directory()
        self.error('orphan_payload', self.admit)

    def test_restart_preserves_claim_and_pinned_outcomes(self):
        self.admit(); first = self.claim(); self.event('unknown')
        self.db.close()
        self.db = sqlite3.connect(self.path, factory=FaultConnection)
        self.ledger = m.OwnerAttachmentLedger(self.db, self.lock)
        recovered = self.claim()
        self.assertFalse(recovered['new_claim'])
        self.assertEqual(recovered['attempt_id'], first['attempt_id'])
        self.assertEqual(recovered['state'], 'unknown')

    def test_nested_transaction_refused_without_rolling_back_caller(self):
        self.db.execute('BEGIN')
        self.error('nested_transaction', self.admit)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()

    def test_fixed_allocation_at_exact_sqlite_page_ceiling(self):
        for journal in ('delete', 'wal'):
            with self.subTest(journal=journal):
                self.db.execute('PRAGMA journal_mode=' + journal)
                self.ledger.rebuild_directory()
                # Worst JSON expansion: each C0 scalar encodes to six bytes.
                env = '\x01' * 4000
                a = self.admission(question='\x01' * 2000, envelope=env,
                                   envelope_sha256=hashlib.sha256(env.encode()).hexdigest(), name='😀' * 120)
                self.admit(a, at=NOW + 120_000)
                page_count = self.db.execute('PRAGMA page_count').fetchone()[0]
                self.db.execute('PRAGMA max_page_count=' + str(page_count))
                self.claim(); self.event('accepted'); self.event('started', turn_id='😀' * 128)
                self.event('reply', turn_id='😀' * 128, reply='\x01' * 16000)
                self.ack()
                self.assertEqual(self.db.execute('PRAGMA page_count').fetchone()[0], page_count)
                rows = self.rows()
                self.assertEqual(len(rows[0][-1][1]), 4096)
                self.assertEqual(len(rows[1][-1][1]), 4096)
                self.assertTrue(all(len(raw.encode()) == 196608 for _, raw in rows[2]))
                self.ledger.prune_payload(self.a.owner_request_id, self.status()['admission_digest'],
                                          now_ms=NOW + 120_000 + m.RETENTION_MS + 1)
                self.event('reply', turn_id='😀' * 128, reply='😀' * 16000); self.ack()
                self.assertEqual(self.db.execute('PRAGMA page_count').fetchone()[0], page_count)
                self.db.execute('PRAGMA max_page_count=1073741823')

    def test_sqlite_full_admission_has_no_partial_rows(self):
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        self.db.execute('PRAGMA max_page_count=' + str(pages))
        with self.assertRaises(sqlite3.OperationalError):
            self.admit()
        self.assertEqual(self.rows(), ([], [], []))
        self.assertFalse(self.db.in_transaction)

    def test_result_rollback_at_all_writes_and_exact_retry(self):
        self.admit(); self.claim()
        before = self.rows()
        for table in ('owner_attachment_links', 'owner_attachment_outcomes', 'requests'):
            self.db.execute('CREATE TEMP TRIGGER fault BEFORE UPDATE ON ' + table + " BEGIN SELECT RAISE(ABORT,'fictional fault'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                self.event('reply', turn_id=TURN, reply='Fictional answer')
            self.assertEqual(self.rows(), before)
            self.db.execute('DROP TRIGGER fault')
        self.event('reply', turn_id=TURN, reply='Fictional answer')
        after = self.rows()
        self.event('reply', turn_id=TURN, reply='Fictional answer', evidence_sequence=1)
        self.assertEqual(self.rows(), after)

    def test_result_commit_failure_leaves_claim_nonreplayable(self):
        self.admit(); claim = self.claim()
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.event('reply', turn_id=TURN, reply='Fictional answer')
        self.db.commit_fault = ''
        self.assertEqual(self.claim()['attempt_id'], claim['attempt_id'])
        self.assertFalse(self.claim()['new_claim'])
        self.assertEqual(self.status()['state'], 'delivery_pending')
        self.event('reply', turn_id=TURN, reply='Fictional answer')
        self.assertEqual(self.status()['state'], 'answered')

    def test_ack_and_prune_failures_do_not_mutate_outcome(self):
        self.admit(); self.claim(); terminal = self.event('ended', turn_id=TURN)
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.ack()
        self.db.commit_fault = ''
        self.assertTrue(self.status()['pending_ack'])
        self.ack()
        before = self.rows()
        self.db.execute("CREATE TEMP TRIGGER fault BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT,'fictional fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.prune()
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.status()['outcome'], terminal['outcome'])

    def test_utf8_scalar_bounds_and_unchanged_canonical_identity(self):
        env = '😀' * 4000
        self.admit(self.admission(question='😀' * 2000, envelope=env,
                                 envelope_sha256=hashlib.sha256(env.encode()).hexdigest()))
        for field, value in (('question', 'x' * 2001), ('envelope', 'x' * 4001), ('name', 'x' * 121),
                             ('question', '\ud800'), ('profile', 'x' * 65)):
            with self.subTest(field=field):
                with self.assertRaises(m.LedgerError):
                    self.ledger.admit(replace(self.admission(), **{field: value}), now_ms=NOW)
        self.assertNotEqual(m.digest('test', {'text': 'é'}), m.digest('test', {'text': 'e\u0301'}))
        self.assertNotEqual(m.digest('test', {'text': '\r\n'}), m.digest('test', {'text': '\n'}))
        self.assertEqual(m.canonical({'b': 1, 'a': 'é'}), b'{"a":"\xc3\xa9","b":1}')

    def test_closed_codec_rejects_invalid_unicode_and_shapes(self):
        for raw in (b'\xff', b'{"x":"\\ud800"}', b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(m.LedgerError):
                m.decode(raw)
        self.error('record_too_large', lambda: m.padded({'x': 'abc'}, 1))
        self.admit(); self.claim()
        self.error('invalid_text', lambda: self.event('reply', turn_id=TURN, reply='x' * 16001))
        self.error('invalid_text', lambda: self.event('started', turn_id='x' * 129))
        self.error('invalid_integer', lambda: self.event('unknown', now_ms=True))
        self.error('invalid_limit', lambda: self.ledger.pending_outcomes(51))
        self.error('invalid_limit', lambda: self.ledger.pending_outcomes(True))

    def test_sequence_exhaustion_does_not_wrap_or_lose_facts(self):
        self.admit(); self.claim()
        link, outcome, _ = self.ledger._read(self.a.owner_request_id)
        link['latest_seq'] = outcome['seq'] = m.MAX_INT
        m._seal_link(link); m._seal_outcome(outcome)
        self.db.execute('UPDATE owner_attachment_links SET record=?', (m.padded(link, m.LINK_BYTES),))
        self.db.execute('UPDATE owner_attachment_outcomes SET record=?', (m.padded(outcome, m.OUTCOME_BYTES),))
        self.db.commit()
        before = self.rows()
        self.error('invalid_integer', lambda: self.event('accepted'))
        self.assertEqual(self.rows(), before)


if __name__ == '__main__':
    unittest.main()
