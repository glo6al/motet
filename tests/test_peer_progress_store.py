"""Guarded progress integration; only fictional in-memory ledgers."""
from pathlib import Path
import sys
import sqlite3
import threading
import os
import tempfile
import uuid
from unittest.mock import patch
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import peer_lifecycle as lifecycle
import peer_progress as progress
import test_peer_progress as fixtures

CREATED, EMPTY = fixtures.CREATED, fixtures.EMPTY


class StoreProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgressTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.store = SimpleNamespace(db=self.db, lock=threading.RLock(),
            _attachment_reserved=lambda request_id: False,
            _peer_progress_clock=lambda: CREATED + 5)

    def evidence(self, row):
        return lifecycle.get(self.db, row['request_id'])

    def test_own_savepoint_persists_but_outer_transaction_is_not_committed(self):
        _, row = self.fixture.admit(turn='fixture-turn')
        self.assertTrue(progress.ensure(self.store, row))
        self.assertFalse(self.db.in_transaction)
        with self.fixture.transaction():
            self.assertTrue(progress.observe(self.store, row, 'started'))
            self.assertTrue(self.db.in_transaction)
            self.assertEqual(progress.load(self.db, row['request_id'])['started_at'], CREATED + 5)
            self.db.rollback()
        self.assertEqual(progress.load(self.db, row['request_id']), EMPTY)

    def test_bad_clock_cannot_roll_back_verified_completion(self):
        _, row = self.fixture.admit(turn='fixture-turn')
        self.store._peer_progress_clock = lambda: float('nan')
        with self.fixture.transaction():
            row.update(state='answered', settled=True, execution_outcome='completed', reply='Fictional answer')
            lifecycle.reconcile(self.db, row)
            self.assertFalse(progress.observe(self.store, row, 'ended'))
            self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.evidence(row)['execution'], 'completed')
        self.assertIsNone(progress.load(self.db, row['request_id'])['ended_at'])

    def test_outer_transaction_loss_cannot_ack_a_missing_completion(self):
        for stage in ('guard', 'savepoint'):
            with self.subTest(stage=stage):
                _, row = self.fixture.admit(turn='fixture-turn')
                db = self.db
                class AbortAtSavepoint:
                    @property
                    def in_transaction(self):
                        return db.in_transaction
                    def execute(self, sql, *args):
                        if sql.startswith('SAVEPOINT'):
                            db.rollback()
                            raise sqlite3.OperationalError('Fictional whole transaction abort')
                        return db.execute(sql, *args)
                def unavailable(request_id):
                    db.rollback()
                    raise sqlite3.OperationalError('Fictional whole transaction abort')
                self.store._attachment_reserved = unavailable if stage == 'guard' else lambda request_id: False
                self.store.db = self.db if stage == 'guard' else AbortAtSavepoint()
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        with self.fixture.transaction():
                            row.update(state='answered', settled=True, execution_outcome='completed', reply='Fictional answer')
                            lifecycle.reconcile(self.db, row)
                            progress.observe(self.store, row, 'ended')
                    self.assertEqual(self.evidence(row)['execution'], 'running')
                finally:
                    self.store.db = self.db
                    self.store._attachment_reserved = lambda request_id: False

    def test_owner_reserved_and_unavailable_namespace_never_writes(self):
        _, row = self.fixture.admit(turn='fixture-turn')
        original = self.evidence(row)
        owner = dict(row, origin='owner')
        self.assertFalse(progress.ensure(self.store, owner))
        self.store._attachment_reserved = lambda request_id: True
        self.assertFalse(progress.observe(self.store, row, 'started'))
        def unavailable(request_id):
            raise RuntimeError('Fictional unavailable guard')
        self.store._attachment_reserved = unavailable
        self.assertFalse(progress.ensure(self.store, row))
        self.assertEqual(self.evidence(row), original)

    def test_lazy_migration_is_per_row_null_only_and_skips_protected_alias(self):
        _, first = self.fixture.admit(turn='fixture-first')
        _, protected = self.fixture.admit('claude', turn='fixture-protected')
        before = {r['request_id']: self.evidence(r) for r in (first, protected)}
        self.store._attachment_reserved = lambda request_id: request_id == protected['request_id']
        def no_clock():
            raise AssertionError('Migration must not observe a clock')
        self.store._peer_progress_clock = no_clock
        self.assertTrue(progress.ensure(self.store, first))
        self.assertFalse(progress.ensure(self.store, protected))
        for row in (first, protected):
            after = self.evidence(row)
            self.assertEqual((after['execution'], after['capture']),
                             (before[row['request_id']]['execution'], before[row['request_id']]['capture']))
        self.assertEqual(progress.load(self.db, first['request_id']), EMPTY)
        self.assertIn('progress', self.evidence(first)['evidence'])
        self.assertEqual(self.evidence(protected), before[protected['request_id']])

    @unittest.skipIf(os.name == 'nt', 'Private store startup requires POSIX')
    def test_startup_does_not_migrate_the_permanent_ledger(self):
        import officer_messaging
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state, sessions = root / 'state', root / 'sessions'
            sessions.mkdir()
            store = officer_messaging.MessagingStore(str(state), str(sessions))
            try:
                for _ in range(128):
                    request, client, agent = str(uuid.uuid4()), str(uuid.uuid4()), 'x:' + str(uuid.uuid4())
                    store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                        (client, agent, '{}', 'fictional digest', request, 'fictional credential', CREATED, None, ''))
                    store.db.execute('INSERT INTO peer_execution VALUES (?,?,?,?,?,?)',
                        (request, agent, CREATED, 'dropped', 'withheld', '{"launched": false}'))
                store.db.commit()
            finally:
                store.close()
            with patch.object(progress, 'ensure', wraps=progress.ensure) as ensure:
                reopened = officer_messaging.MessagingStore(str(state), str(sessions))
                try:
                    ensure.assert_not_called()
                    retained = reopened.db.execute('SELECT evidence FROM peer_execution').fetchall()
                    self.assertEqual(len(retained), 128)
                    import json
                    self.assertTrue(all('progress' not in json.loads(raw) for raw, in retained))
                finally:
                    reopened.close()

    def test_actor_or_clock_failure_does_not_replace_progress_or_original_binding(self):
        _, row = self.fixture.admit(turn='fixture-turn')
        self.assertTrue(progress.observe(self.store, row, 'started'))
        before = self.evidence(row)
        wrong = dict(row, bound_turn='another-turn')
        self.assertFalse(progress.observe(self.store, wrong, 'checked'))
        self.store._peer_progress_clock = lambda: CREATED + 1
        self.assertFalse(progress.observe(self.store, row, 'checked'))
        self.assertEqual(self.evidence(row), before)


if __name__ == '__main__':
    unittest.main()
