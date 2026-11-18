"""Ordinary admission refreshes a validated directory without a scheduler tick."""
from contextlib import closing, contextmanager
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_attachments_owner_startup as fixture

owner, guards, messaging = fixture.owner, fixture.guards, fixture.messaging
uid = fixture.uid


@unittest.skipIf(os.name == 'nt', 'POSIX private owner database')
class DeliveryAdmissionTests(unittest.TestCase):
    # Reuse the disposable DB/manual scheduler fixture without inheriting its tests.
    setUp = fixture.StartupTests.setUp
    open_store = fixture.StartupTests.open_store
    close_store = fixture.StartupTests.close_store
    scheduler = fixture.StartupTests.scheduler
    bind = fixture.StartupTests.bind
    submit = fixture.StartupTests.submit
    ordinary_row = fixture.StartupTests.ordinary_row
    save = fixture.StartupTests.save
    installed_fixture = fixture.StartupTests.installed_fixture

    def assert_known_refusal(self, action):
        before = self.store.db.execute('SELECT id,data FROM requests ORDER BY id').fetchall()
        with self.assertRaises(messaging.MessageNotSubmitted) as caught:
            action()
        self.assertEqual(str(caught.exception), 'Motet could not check the message queue. Nothing was sent.')
        self.assertEqual(self.store.db.execute('SELECT id,data FROM requests ORDER BY id').fetchall(), before)

    def stale_bookkeeping(self, db=None):
        db = self.store.db if db is None else db
        with db:
            db.execute('INSERT INTO expired_controls VALUES (?)', (uid(),))

    def test_first_send_without_a_scheduler_tick_builds_and_inserts_once(self):
        instance = self.bind()
        directory = instance.ledger.directory
        self.assertFalse(directory.ready)
        with patch.object(directory, 'begin_rebuild', wraps=directory.begin_rebuild) as begins:
            accepted = self.submit('Check the fictional orchard.')
        begins.assert_called_once()
        self.assertEqual(accepted['state'], 'sending')
        self.assertEqual([row['request_id'] for row in self.store._rows()], [accepted['request_id']])
        self.assertTrue(directory.ready)

    def test_claim_maintenance_decide_then_submit_without_another_tick(self):
        instance = self.bind()
        request_id, question = uid(), 'Report on the fictional orchard.'
        self.store.claim_ask(request_id, question)
        self.workers[0].checkpoint()
        directory = instance.ledger.directory
        self.assertTrue(directory.matches(directory.epoch()))
        decision = {'kind': 'relay', 'agent': self.agent, 'question': question}
        self.store.decide_ask(request_id, decision)
        self.assertFalse(directory.matches(directory.epoch()))
        accepted = self.store.submit(self.agent, question, request_id, self.agents)
        self.assertEqual(accepted['request_id'], request_id)
        self.assertEqual(accepted['state'], 'sending')
        self.assertEqual(len(self.store._rows()), 1)

    def test_direct_send_after_bookkeeping_and_same_id_replay(self):
        instance = self.bind()
        self.workers[0].checkpoint()
        self.stale_bookkeeping()
        self.assertFalse(instance.ledger.directory.matches(instance.ledger.directory.epoch()))
        request_id, question = uid(), 'Count the fictional trees.'
        accepted = self.store.submit(self.agent, question, request_id, self.agents)
        self.assertEqual(accepted['state'], 'sending')
        self.stale_bookkeeping()
        with patch.object(instance.ledger.directory, 'begin_rebuild', side_effect=AssertionError('replay rebuilt')):
            replay = self.store.submit(self.agent, question, request_id, self.agents)
        self.assertEqual(replay, accepted)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone(), (1,))

    def test_fresh_directory_does_not_rebuild(self):
        instance = self.bind()
        self.workers[0].checkpoint()
        with patch.object(instance.ledger.directory, 'begin_rebuild', side_effect=AssertionError('fresh rebuild')):
            self.assertEqual(self.submit()['state'], 'sending')

    def test_inherited_transaction_keeps_pending_writes_and_never_begins(self):
        instance = self.bind()
        db, directory = self.store.db, instance.ledger.directory
        pending = uid()
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO expired_controls VALUES (?)', (pending,))
        try:
            with patch.object(directory, 'begin_rebuild', side_effect=AssertionError('inherited rebuild')):
                with self.assertRaises(guards.OwnerCapacityUnavailable):
                    with instance.ledger.ordinary_slots(int(time.time() * 1000)):
                        self.fail('Inherited transaction was accepted.')
            self.assertTrue(db.in_transaction)
            self.assertEqual(db.execute('SELECT id FROM expired_controls WHERE id=?', (pending,)).fetchone(), (pending,))
            with closing(sqlite3.connect(self.db_path)) as independent:
                self.assertIsNone(independent.execute('SELECT id FROM expired_controls WHERE id=?', (pending,)).fetchone())
        finally:
            db.rollback()

    def corrupt_retained_fixture(self, count):
        self.open_store('retained-' + str(count))
        owner.OwnerAttachmentLedger(self.store.db, self.store.lock)
        with self.store.db:
            self.store.db.executemany('INSERT INTO owner_attachment_links VALUES (?,?)',
                                      ((f'{index:036d}', b' ' * 4096) for index in range(count)))
        fixture.install(self.store.db)
        current = self.path.name
        self.open_store(current)
        return self.bind(allow_install=False)

    def test_batch_boundaries_charge_every_invalid_pair_conservatively(self):
        for count, steps in ((64, 2), (65, 2), (10000, 157), (10001, 157)):
            with self.subTest(count=count):
                instance = self.corrupt_retained_fixture(count)
                directory = instance.ledger.directory
                with patch.object(directory, 'rebuild_step', wraps=directory.rebuild_step) as rebuild:
                    if count <= owner.MAX_LINKS:
                        with instance.ledger.ordinary_slots(int(time.time() * 1000)) as (counts, _):
                            self.assertEqual(counts, (count, count, count))
                        self.assertTrue(directory.ready)
                    else:
                        with self.assertRaises(guards.OwnerCapacityUnavailable):
                            with instance.ledger.ordinary_slots(int(time.time() * 1000)):
                                self.fail('Oversize directory was accepted.')
                        self.assertFalse(directory.ready)
                        self.assertFalse(directory.building)
                    self.assertEqual(rebuild.call_count, steps)

    def test_no_progress_stops_after_157_steps_and_discards_candidate(self):
        instance = self.bind()
        directory = instance.ledger.directory
        with patch.object(directory, 'begin_rebuild', wraps=directory.begin_rebuild) as begins, \
                patch.object(directory, 'rebuild_step', return_value=False) as steps:
            self.assert_known_refusal(self.submit)
        begins.assert_called_once()
        self.assertEqual(steps.call_count, owner.MAX_LINKS // fixture.directories.BATCH_SIZE + 1)
        self.assertEqual(steps.call_count, 157)
        self.assertFalse(directory.ready)
        self.assertFalse(directory.building)

    def test_external_commit_between_batches_refuses_without_retry(self):
        instance = self.corrupt_retained_fixture(65)
        directory = instance.ledger.directory
        step = directory.rebuild_step
        with closing(sqlite3.connect(self.db_path)) as independent:
            calls = []

            def race():
                result = step()
                calls.append(1)
                if len(calls) == 1:
                    self.stale_bookkeeping(independent)
                return result

            with patch.object(directory, 'rebuild_step', side_effect=race), \
                    patch.object(directory, 'begin_rebuild', wraps=directory.begin_rebuild) as begins:
                self.assert_known_refusal(self.submit)
        begins.assert_called_once()
        self.assertFalse(directory.building)
        self.assertFalse(directory.ready)

    def test_external_commit_between_preflight_and_begin_is_rechecked(self):
        instance = self.bind()
        self.workers[0].checkpoint()
        writer = instance.ledger.ordinary_writer
        with closing(sqlite3.connect(self.db_path)) as independent:
            @contextmanager
            def race():
                self.stale_bookkeeping(independent)
                with writer() as proof:
                    yield proof

            with patch.object(instance.ledger, 'ordinary_writer', side_effect=race):
                self.assert_known_refusal(self.submit)
        self.assertFalse(instance.ledger.directory.ready)

    def test_schema_shadow_and_permit_fail_closed_before_yield(self):
        for corruption in ('schema', 'shadow', 'permit'):
            with self.subTest(corruption=corruption):
                self.open_store(corruption)
                instance = self.bind()
                self.workers[-1].checkpoint()
                db = self.store.db
                with db:
                    if corruption == 'schema':
                        db.execute('DROP TRIGGER ' + guards.TRIGGER_PREFIX + 'requests_insert')
                    elif corruption == 'shadow':
                        db.execute('CREATE TEMP TABLE requests (id TEXT, data TEXT)')
                    else:
                        db.execute(f"UPDATE {guards.PERMIT} SET verb='I'")
                with self.assertRaises(guards.OwnerCapacityUnavailable):
                    with instance.ledger.ordinary_slots(int(time.time() * 1000)):
                        self.fail('Broken guard was accepted.')
                self.assertFalse(db.in_transaction)
                self.assertFalse(instance.ledger.directory.building)

    def test_unbound_guard_refuses_without_insertion(self):
        self.bind()
        self.store._attachment_guard.capacity = None
        self.assert_known_refusal(self.submit)

    def test_preflight_commit_failure_is_known_not_submitted(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault):
                self.open_store('preflight-' + fault)
                instance = self.bind()
                begin = instance.ledger.directory.begin_rebuild

                def faulty_begin():
                    self.store.db.fault_commit = fault
                    return begin()

                with patch.object(instance.ledger.directory, 'begin_rebuild', side_effect=faulty_begin):
                    self.assert_known_refusal(self.submit)
                self.assertFalse(instance.ledger.directory.ready)
                self.assertFalse(instance.ledger.directory.building)

    def test_queue_commit_lost_response_stays_uncertain_and_replays(self):
        self.bind()
        self.workers[0].checkpoint()
        request_id, question = uid(), 'Inspect the fictional grove.'
        save = self.store._save

        def faulty_save(*args, **kwargs):
            self.store.db.fault_commit = 'after'
            return save(*args, **kwargs)

        with patch.object(self.store, '_save', side_effect=faulty_save):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.submit(self.agent, question, request_id, self.agents)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone(), (1,))
        replay = self.store.submit(self.agent, question, request_id, self.agents)
        self.assertEqual(replay['request_id'], request_id)
        self.assertEqual(len(self.store._rows()), 1)

    def test_post_yield_ledger_error_keeps_uncertain_guard_error_mapping(self):
        instance = self.bind()
        self.workers[0].checkpoint()
        with self.assertRaises(guards.OwnerGuardError) as caught:
            with instance.ledger.ordinary_slots(int(time.time() * 1000)):
                raise owner.LedgerError('Fictional error after admission yielded.')
        self.assertNotIsInstance(caught.exception, guards.OwnerCapacityUnavailable)

    def test_post_commit_ledger_error_is_uncertain_and_replays_insert(self):
        self.bind()
        self.workers[0].checkpoint()
        request_id, question = uid(), 'Inspect the fictional clearing.'
        save = self.store._save

        def fail_after_commit():
            raise owner.LedgerError('Fictional lost commit response.')

        def faulty_save(*args, **kwargs):
            self.store.db.after_commit = fail_after_commit
            return save(*args, **kwargs)

        with patch.object(self.store, '_save', side_effect=faulty_save):
            with self.assertRaises(messaging.MessagingError) as caught:
                self.store.submit(self.agent, question, request_id, self.agents)
        self.assertNotIsInstance(caught.exception, messaging.MessageNotSubmitted)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone(), (1,))
        self.assertEqual(self.store.submit(self.agent, question, request_id, self.agents)['request_id'], request_id)

    def test_full_capacity_includes_protected_file_request(self):
        _, protected = self.installed_fixture(pending=True)
        instance = self.bind(allow_install=False)
        for _ in range(owner.MAX_PENDING - 1):
            self.assertEqual(self.submit()['state'], 'sending')
        with self.assertRaises(messaging.MessagingError) as caught:
            self.submit()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.store._rows()), owner.MAX_PENDING - 1)
        self.assertEqual(instance.ledger.status(protected)['state'], 'owner_recorded')


if __name__ == '__main__':
    unittest.main()
