"""Additive progress evidence using a disposable ledger and a server clock."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import peer_bridge as bridge
import peer_lifecycle as lifecycle
import peer_progress as progress


CREATED = 1735689600  # 2025-01-01T00:00:00Z
EMPTY = dict(version=1, claim_issued_at=None, accepted_at=None, started_at=None, last_checked_at=None, ended_at=None)


def uid():
    return str(uuid.uuid4())


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        bridge.shared_schema(self.db)

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def admit(self, runtime='codex', *, launch=True, turn=None, state='queued', capture=None, reserve=True, payload=True):
        client_id, transport_id = uid(), uid()
        agent = ('c:' if runtime == 'claude' else 'x:') + uid()
        row = dict(request_id=transport_id, agent=agent, created=CREATED, origin='peer', bridge_version=2,
                   runtime=runtime, launched=launch, state=state, error='',
                   reply='Fictional captured reply' if state == 'answered' else '')
        if runtime == 'claude':
            row.update(transport='claude_mod', mod_enrollment=uid(), mod_generation=uid(), mod_attempt_id=uid(),
                       mod_project='/fictional/project', target_project='/fictional/project')
        if turn:
            row['mod_turn' if runtime == 'claude' else 'bound_turn'] = turn
        if capture:
            row['capture_disposition'] = capture
        with self.transaction():
            self.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                            (client_id, agent, '{}', 'fixture hash', transport_id, uid(), CREATED,
                             CREATED + 1 if launch else None, 'Fixture preview'))
            if payload:
                self.db.execute('INSERT INTO requests VALUES (?,?)', (transport_id, json.dumps(row)))
            if reserve:
                lifecycle.reconcile(self.db, row)
        return client_id, row

    def observe(self, row, observation, when, *, binding=None, fresh=True):
        if binding is None:
            keys = ('mod_enrollment', 'mod_generation', 'mod_attempt_id') if row['runtime'] == 'claude' else ()
            turn = 'mod_turn' if row['runtime'] == 'claude' else 'bound_turn'
            if turn in row and observation != 'claim_issued':
                keys += (turn,)
            binding = {name: row[name] for name in keys}
        return progress.record(self.db, row['request_id'], observation, agent=row['agent'], created=row['created'],
                               binding=binding, fresh=fresh, clock=when if callable(when) else lambda: when)

    def record(self, row, observation, when, **kwargs):
        with self.transaction():
            return self.observe(row, observation, when, **kwargs)

    def evidence(self, row):
        return lifecycle.get(self.db, row['request_id'])

    def test_null_migration_preserves_held_captured_and_retired_evidence(self):
        _, held = self.admit(turn='held-turn', state='unknown', capture='withheld')
        _, captured = self.admit('claude', turn='captured-turn', state='answered')
        _, retired = self.admit('claude')
        with self.transaction():
            lifecycle.retire(self.db, retired['request_id'], lifecycle.RETIRE_REASON)
        for row, identity in ((held, ('uncertain', 'withheld')), (captured, ('completed', 'captured')),
                              (retired, ('retired', 'withheld'))):
            with self.subTest(identity=identity):
                old = self.evidence(row)
                self.assertEqual((old['execution'], old['capture']), identity)
                self.assertEqual(progress.load(self.db, row['request_id']), EMPTY)
                self.assertEqual(self.evidence(row), old)
                with self.transaction():
                    self.assertEqual(progress.migrate(self.db, row['request_id']), EMPTY)
                    changes = self.db.total_changes
                    self.assertEqual(progress.migrate(self.db, row['request_id']), EMPTY)
                    self.assertEqual(self.db.total_changes, changes)
                self.assertEqual(self.evidence(row), dict(old, evidence=dict(old['evidence'], progress=EMPTY)))
        self.assertIn('owner_retirement', self.evidence(retired)['evidence'])

    def test_claude_claim_does_not_supply_native_acceptance_and_original_attempt_is_pinned(self):
        _, row = self.admit('claude')
        issued = self.record(row, 'claim_issued', CREATED + 2)
        self.assertEqual(issued, dict(EMPTY, claim_issued_at=CREATED + 2))
        for key in ('mod_enrollment', 'mod_generation', 'mod_attempt_id'):
            wrong = dict(row, **{key: uid()})
            with self.assertRaisesRegex(progress.ProgressError, 'original durable provider binding'):
                self.record(wrong, 'claim_issued', CREATED + 3)
        # Startup and a replayed authenticated receipt are not a fresh observation.
        with self.assertRaisesRegex(progress.ProgressError, 'new verified provider observation'):
            self.record(row, 'accepted', CREATED + 3, fresh=False)
        self.assertEqual(progress.load(self.db, row['request_id']), issued)
        self.assertEqual(self.evidence(row)['execution'], 'submitted')

    def test_codex_acceptance_can_precede_start_and_later_acceptance_is_also_valid(self):
        _, first = self.admit()
        accepted = self.record(first, 'accepted', CREATED + 2)
        self.assertEqual(accepted, dict(EMPTY, accepted_at=CREATED + 2))
        for key in ('started', 'checked', 'ended'):
            with self.assertRaises(progress.ProgressError):
                self.record(first, key, CREATED + 3)
        _, second = self.admit('claude', turn='bound-turn', state='awaiting_reply')
        started = self.record(second, 'started', CREATED + 4)
        self.assertEqual(started, dict(EMPTY, started_at=CREATED + 4))
        later = self.record(second, 'accepted', CREATED + 5)
        self.assertEqual(later, dict(started, accepted_at=CREATED + 5))
        self.assertIsNone(later['claim_issued_at'])

    def test_server_clock_crosses_cutoffs_without_changing_capacity_or_expiry(self):
        client_id, row = self.admit(turn='long-codex-turn', state='awaiting_reply')
        started = self.record(row, 'started', CREATED + 2)
        original = self.evidence(row)
        for offset in (899, 900, 5 * 3600, 24 * 3600 + 1):
            with self.subTest(offset=offset):
                before = lifecycle.capacity_error(self.db, row['agent'], CREATED + offset, 900)
                after = self.record(row, 'checked', CREATED + offset)
                self.assertEqual(after, dict(started, last_checked_at=CREATED + offset))
                self.assertEqual(lifecycle.capacity_error(self.db, row['agent'], CREATED + offset, 900), before)
                self.assertEqual(self.evidence(row), dict(original, evidence=dict(original['evidence'], progress=after)))
        broker = bridge.SharedBridge.__new__(bridge.SharedBridge)
        broker.store = SimpleNamespace(db=self.db, _peer_payload=lambda tid: ('clear', self.db.execute(
            'SELECT data FROM requests WHERE id=?', (tid,)).fetchone()))
        with patch.object(bridge.time, 'time', return_value=CREATED + 24 * 3600 + 1):
            projection = broker.result(client_id)
        self.assertEqual((projection['state'], projection['text_preview'], projection['reply']), ('expired', '', ''))
        self.assertNotIn('progress', projection)

    def test_duplicate_observations_keep_first_times_and_checks_only_advance_on_fresh_sources(self):
        _, row = self.admit(turn='bound-turn', state='awaiting_reply')
        first = self.record(row, 'started', CREATED + 2)
        changes = self.db.total_changes
        self.assertEqual(self.record(row, 'started', CREATED + 900), first)
        self.assertEqual(self.db.total_changes, changes)
        with self.assertRaisesRegex(progress.ProgressError, 'new verified provider observation'):
            self.record(row, 'checked', CREATED + 900, fresh=False)
        self.assertEqual(progress.load(self.db, row['request_id']), first)
        checked = self.record(row, 'checked', CREATED + 9)
        changes = self.db.total_changes
        self.assertEqual(self.record(row, 'checked', CREATED + 9), checked)
        self.assertEqual(self.db.total_changes, changes)
        with self.assertRaises(progress.ProgressClockError):
            self.record(row, 'checked', CREATED + 8)
        with self.assertRaises(progress.ProgressClockError):
            self.record(row, 'accepted', CREATED + 8)
        self.assertEqual(progress.load(self.db, row['request_id']), checked)

    def test_invalid_and_backward_clocks_write_nothing(self):
        _, row = self.admit(turn='bound-turn', state='awaiting_reply')
        for value in (CREATED, None, True, float('nan'), float('inf'), -float('inf'), 10**300, '1735689603'):
            with self.subTest(kind=type(value)):
                with self.assertRaises(progress.ProgressClockError):
                    self.record(row, 'started', value)
        def unavailable():
            raise OSError('Clock fixture failed')
        with self.assertRaises(progress.ProgressClockError):
            self.record(row, 'started', unavailable)
        self.assertEqual(progress.load(self.db, row['request_id']), EMPTY)
        self.assertNotIn('progress', self.evidence(row)['evidence'])

    def test_ended_requires_exact_completed_lifecycle_and_no_synthetic_start(self):
        _, row = self.admit('claude', turn='bound-turn', state='awaiting_reply')
        with self.assertRaisesRegex(progress.ProgressError, 'requires exact durable completion'):
            self.record(row, 'ended', CREATED + 3)
        with self.transaction():
            lifecycle.reconcile(self.db, dict(row, state='answered', reply='Fictional reply'))
            # Catch metadata clock failure separately from durable completion.
            with self.assertRaises(progress.ProgressClockError):
                self.observe(row, 'ended', float('nan'))
        self.assertEqual(self.evidence(row)['execution'], 'completed')
        self.assertEqual(progress.load(self.db, row['request_id']), EMPTY)
        wrong_turn = dict(row, mod_turn='different-turn')
        with self.assertRaisesRegex(progress.ProgressError, 'original durable provider binding'):
            self.record(wrong_turn, 'ended', CREATED + 4)
        ended = self.record(row, 'ended', CREATED + 4)
        self.assertEqual(ended, dict(EMPTY, ended_at=CREATED + 4))
        self.assertEqual(self.record(row, 'ended', CREATED + 9), ended)

    def test_retirement_and_drop_are_not_end_observations(self):
        _, retired = self.admit(turn='retired-turn', state='awaiting_reply')
        self.record(retired, 'started', CREATED + 2)
        with self.transaction():
            lifecycle.retire(self.db, retired['request_id'], lifecycle.RETIRE_REASON)
        _, dropped = self.admit(launch=False, state='failed')
        for row, state in ((retired, 'retired'), (dropped, 'dropped')):
            with self.assertRaises(progress.ProgressError):
                self.record(row, 'ended', CREATED + 3)
            self.assertEqual(self.evidence(row)['execution'], state)
            self.assertIsNone(progress.load(self.db, row['request_id'])['ended_at'])

    def test_identity_conflicts_or_missing_ownership_do_not_create_or_modify_evidence(self):
        _, row = self.admit(turn='correct-turn', state='awaiting_reply', payload=False)
        self.assertEqual(progress.load(self.db, row['request_id']), EMPTY)
        wrong = (dict(row, agent='x:' + uid()), dict(row, created=CREATED - 1), dict(row, bound_turn='foreign-turn'))
        for wrong_observation in wrong:
            with self.assertRaises(progress.ProgressError):
                self.record(wrong_observation, 'started', CREATED + 2)
        with self.transaction():
            self.db.execute('UPDATE peer_execution SET created=created-1 WHERE request_id=?', (row['request_id'],))
        with self.assertRaisesRegex(progress.ProgressError, 'durable execution matching'):
            progress.load(self.db, row['request_id'])
        with self.transaction():
            self.db.execute('UPDATE peer_execution SET created=? WHERE request_id=?', (CREATED, row['request_id']))
            self.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                            (uid(), uid(), uid(), 'fixture-digest', row['request_id'], CREATED, CREATED + 1))
        with self.assertRaisesRegex(progress.ProgressError, 'one exact shared v2'):
            self.record(row, 'started', CREATED + 2)
        _, no_lifecycle = self.admit(reserve=False)
        with self.assertRaisesRegex(progress.ProgressError, 'durable execution matching'):
            self.record(no_lifecycle, 'accepted', CREATED + 2)
        owner_id = uid()
        with self.transaction():
            self.db.execute('INSERT INTO requests VALUES (?,?)', (owner_id, '{"origin":"owner"}'))
            with self.assertRaisesRegex(progress.ProgressError, 'one exact shared v2'):
                progress.migrate(self.db, owner_id)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM peer_execution').fetchone(), (1,))
        self.assertNotIn('progress', self.evidence(row)['evidence'])

    def test_writes_require_caller_transaction_and_do_not_commit(self):
        _, row = self.admit(turn='bound-turn', state='awaiting_reply')
        for action in (lambda: progress.migrate(self.db, row['request_id']),
                       lambda: self.observe(row, 'started', CREATED + 2)):
            with self.assertRaisesRegex(progress.ProgressError, 'caller-owned transaction'):
                action()
        self.db.execute('BEGIN IMMEDIATE')
        progress.migrate(self.db, row['request_id'])
        self.observe(row, 'started', CREATED + 2)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertNotIn('progress', self.evidence(row)['evidence'])

    def test_compare_and_swap_preserves_other_evidence_if_changed_during_observation(self):
        _, row = self.admit(turn='bound-turn', state='awaiting_reply')
        original = self.evidence(row)['evidence']
        changed = dict(original, fixture_other_evidence=True)
        def clock():
            self.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?', (json.dumps(changed), row['request_id']))
            return CREATED + 2
        with self.transaction():
            with self.assertRaisesRegex(progress.ProgressError, 'changed before progress'):
                self.observe(row, 'started', clock)
        self.assertEqual(self.evidence(row)['evidence'], changed)

    def test_malformed_progress_is_neither_loaded_nor_replaced_by_migration(self):
        _, row = self.admit(turn='bound-turn')
        for invalid in (dict(EMPTY, version=True), dict(EMPTY, version=2), dict(EMPTY, started_at=float('inf')),
                        dict(EMPTY, started_at=True), dict(EMPTY, started_at=CREATED),
                        dict(EMPTY, ended_at=CREATED + 2), {'version': 1}):
            with self.transaction():
                original = dict(self.evidence(row)['evidence'], progress=invalid)
                self.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?', (json.dumps(original), row['request_id']))
                with self.assertRaises(progress.ProgressError):
                    progress.load(self.db, row['request_id'])
                with self.assertRaises(progress.ProgressError):
                    progress.migrate(self.db, row['request_id'])
                self.assertEqual(self.evidence(row)['evidence'], original)

    def test_unknown_durable_lifecycle_values_fail_closed(self):
        _, row = self.admit(turn='bound-turn')
        for execution, capture in (('fictional state', 'pending'), ('running', 'fictional capture')):
            with self.transaction():
                self.db.execute('UPDATE peer_execution SET execution=?,capture=? WHERE request_id=?',
                                (execution, capture, row['request_id']))
                with self.assertRaisesRegex(progress.ProgressError, 'recognized durable lifecycle'):
                    progress.load(self.db, row['request_id'])
                with self.assertRaisesRegex(progress.ProgressError, 'recognized durable lifecycle'):
                    self.observe(row, 'started', CREATED + 2)
                self.assertNotIn('progress', self.evidence(row)['evidence'])

    def test_populated_observations_require_their_durable_provider_binding_on_read(self):
        for runtime, field, missing in (('codex', 'started_at', 'bound_turn'),
                                        ('claude', 'last_checked_at', 'mod_turn'),
                                        ('claude', 'claim_issued_at', 'mod_generation'),
                                        ('claude', 'accepted_at', 'mod_attempt_id')):
            with self.subTest(runtime=runtime, field=field):
                _, row = self.admit(runtime, turn='recorded-turn', state='awaiting_reply')
                with self.transaction():
                    evidence = self.evidence(row)['evidence']
                    evidence.pop(missing)
                    self.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?',
                                    (json.dumps(evidence), row['request_id']))
                    self.assertEqual(progress.migrate(self.db, row['request_id']), EMPTY)
                    evidence['progress'] = dict(EMPTY, **{field: CREATED + 2})
                    self.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?',
                                    (json.dumps(evidence), row['request_id']))
                    with self.assertRaisesRegex(progress.ProgressError, 'original durable provider binding'):
                        progress.load(self.db, row['request_id'])
                    self.assertEqual(self.evidence(row)['evidence'], evidence)


if __name__ == '__main__':
    unittest.main()
