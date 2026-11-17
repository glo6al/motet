"""Offline directory/query-bound tests; real SQLite, no production construction."""
from contextlib import closing
import json
import sqlite3
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
import uuid

import test_attachments_owner_transport as fixtures
import attachments_owner_transport as m
from attachments_owner_directory import BATCH_SIZE

NOW, TURN = fixtures.NOW, fixtures.TURN


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.t = fixtures.LedgerTests()
        self.t.setUp()
        self.db, self.ledger = self.t.db, self.t.ledger
        self.directory = self.ledger.directory

    def tearDown(self):
        self.t.tearDown()

    def error(self, code, fn):
        self.t.error(code, fn)

    def entry(self, owner_id=None):
        with self.ledger._transaction():
            return self.directory.entry(owner_id or self.t.a.owner_request_id)

    def counts(self):
        with self.ledger._transaction():
            return self.directory.counts(NOW + 61_000)

    def seed(self, n, *, pending=False):
        self.t.admit(); self.t.claim(); self.t.event('ended', turn_id=TURN); self.t.ack(); self.t.prune()
        if pending:
            self.t.event('reply', turn_id=TURN, reply='Fictional late evidence')
        link, outcome, _ = self.ledger._read(self.t.a.owner_request_id)
        links, outcomes = [], []
        for i in range(1, n):
            key = str(uuid.UUID(int=i))
            l = json.loads(json.dumps(link)); o = json.loads(json.dumps(outcome))
            l['identity']['owner_request_id'] = key
            l['admission_digest'] = m.digest('watch-owner-attachment-admission-v1', l['identity'])
            o.update(owner_request_id=key, admission_digest=l['admission_digest'])
            m._seal_outcome(o)
            if not pending:
                l['ack_digest'] = o['digest']
            m._seal_link(l)
            links.append((key, m.padded(l, m.LINK_BYTES)))
            outcomes.append((key, m.padded(o, m.OUTCOME_BYTES)))
        self.db.executemany('INSERT INTO owner_attachment_links VALUES (?,?)', links)
        self.db.executemany('INSERT INTO owner_attachment_outcomes VALUES (?,?)', outcomes)
        self.db.commit()
        self.ledger.rebuild_directory()

    def trace(self, call):
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            result = call()
        finally:
            self.db.set_trace_callback(None)
        return result, statements

    def test_warm_empty_pending_constant_queries_at_10000(self):
        _, empty = self.trace(self.ledger.pending_outcomes)
        self.seed(10_000)
        result, full = self.trace(self.ledger.pending_outcomes)
        self.assertEqual(result, [])
        self.assertEqual(len(full), len(empty))
        self.assertLessEqual(len(full), 6)
        self.assertFalse(any('SELECT' in q.upper() for q in full))
        measured = []
        self.db.set_trace_callback(measured.append)
        try:
            self.error('link_capacity', lambda: self.ledger.admit(self.t.admission(), now_ms=NOW + 61_000))
        finally:
            self.db.set_trace_callback(None)
        self.assertLessEqual(len(measured), 24)
        self.assertFalse(any('SELECT owner_request_id' in q for q in measured))

    def test_empty_corrupt_key_is_quarantined_not_skipped(self):
        self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)',
                        ('', b' ' * m.OUTCOME_BYTES))
        self.db.commit()
        self.ledger.rebuild_directory()
        self.assertEqual(self.counts()[:3], (1, 1, 1))
        with self.ledger._transaction():
            self.assertTrue(self.directory.entry('').quarantined)

    def test_null_nonunique_key_withholds_directory_instead_of_skipping(self):
        self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)',
                        (None, b' ' * m.OUTCOME_BYTES))
        self.db.commit()
        self.error('directory_identity_unavailable', self.ledger.rebuild_directory)
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.assertFalse(self.db.in_transaction)

    def test_duplicate_authoritative_keys_rejected_without_record_changes(self):
        for table in ('owner_attachment_links', 'owner_attachment_outcomes'):
            for conflicting in (False, True):
                with self.subTest(table=table, conflicting=conflicting):
                    self.db.execute('DROP TABLE ' + table)
                    self.db.execute('CREATE TABLE ' + table + ' (owner_request_id TEXT, record BLOB)')
                    key = str(uuid.uuid4())
                    rows = [(key, b'a' * 4096), (key, (b'b' if conflicting else b'a') * 4096)]
                    self.db.executemany('INSERT INTO ' + table + ' VALUES (?,?)', rows)
                    self.db.commit()
                    self.error('directory_key_schema_unavailable', self.ledger.rebuild_directory)
                    self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
                    self.assertEqual(self.db.execute('SELECT * FROM ' + table).fetchall(), rows)
                    self.assertIsNone(self.directory._building)
                    self.db.execute('DROP TABLE ' + table)
                    self.db.execute('CREATE TABLE ' + table + ' (owner_request_id TEXT PRIMARY KEY, record BLOB NOT NULL CHECK(length(record)=4096))')
                    self.db.commit()
                    self.ledger.rebuild_directory()

    def test_composite_primary_key_does_not_prove_unique_owner_identity(self):
        self.db.execute('DROP TABLE owner_attachment_outcomes')
        self.db.execute('CREATE TABLE owner_attachment_outcomes (owner_request_id TEXT, record BLOB, PRIMARY KEY(owner_request_id,record))')
        key = str(uuid.uuid4())
        rows = [(key, b'a' * 4096), (key, b'b' * 4096)]
        self.db.executemany('INSERT INTO owner_attachment_outcomes VALUES (?,?)', rows)
        self.db.commit()
        self.error('directory_key_schema_unavailable', self.ledger.rebuild_directory)
        self.assertEqual(self.db.execute('SELECT * FROM owner_attachment_outcomes').fetchall(), rows)

    def test_nonbinary_key_collations_rejected_without_collapsing_or_changing_rows(self):
        self.db.create_collation('FICTIONAL_NOCASE', lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
        for table in ('owner_attachment_links', 'owner_attachment_outcomes'):
            for collation in ('NOCASE', 'RTRIM', 'FICTIONAL_NOCASE'):
                with self.subTest(table=table, collation=collation):
                    self.db.execute('DROP TABLE ' + table)
                    self.db.execute('CREATE TABLE ' + table + ' (owner_request_id TEXT PRIMARY KEY COLLATE ' + collation + ', record BLOB NOT NULL CHECK(length(record)=4096))')
                    key = 'abcdefab-abcd-4abc-8abc-abcdefabcdef'
                    self.db.execute('INSERT INTO ' + table + ' VALUES (?,?)', (key, b'a' * 4096))
                    self.db.commit()
                    before = self.db.execute('SELECT * FROM ' + table).fetchall()
                    self.error('directory_key_schema_unavailable', self.ledger.rebuild_directory)
                    self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
                    self.assertEqual(self.db.execute('SELECT * FROM ' + table).fetchall(), before)
                    self.db.execute('DROP TABLE ' + table)
                    self.db.execute('CREATE TABLE ' + table + ' (owner_request_id TEXT PRIMARY KEY, record BLOB NOT NULL CHECK(length(record)=4096))')
                    self.db.commit()
                    self.ledger.rebuild_directory()

    def test_nocase_links_cannot_hide_uppercase_orphan_outcome(self):
        self.t.admit(self.t.admission(owner_request_id='abcdefab-abcd-4abc-8abc-abcdefabcdef'))
        key = self.t.a.owner_request_id
        original = self.db.execute('SELECT * FROM owner_attachment_links').fetchone()
        self.db.execute('DROP TABLE owner_attachment_links')
        self.db.execute('CREATE TABLE owner_attachment_links (owner_request_id TEXT PRIMARY KEY COLLATE NOCASE, record BLOB NOT NULL CHECK(length(record)=4096))')
        self.db.execute('INSERT INTO owner_attachment_links VALUES (?,?)', original)
        self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)', (key.upper(), b' ' * 4096))
        self.db.commit()
        before = self.db.execute('SELECT * FROM owner_attachment_outcomes ORDER BY owner_request_id').fetchall()
        self.error('directory_key_schema_unavailable', self.ledger.rebuild_directory)
        self.assertIsNone(self.directory._building)
        self.assertEqual(self.db.execute('SELECT * FROM owner_attachment_links').fetchall(), [original])
        self.assertEqual(self.db.execute('SELECT * FROM owner_attachment_outcomes ORDER BY owner_request_id').fetchall(), before)
        self.assertEqual(len(before), 2)

    def test_binary_pk_does_not_mask_nocase_column_default(self):
        self.db.execute('DROP TABLE owner_attachment_links')
        self.db.execute('CREATE TABLE owner_attachment_links (owner_request_id TEXT COLLATE NOCASE, record BLOB NOT NULL CHECK(length(record)=4096), PRIMARY KEY(owner_request_id COLLATE BINARY))')
        rows = [('abcdefab-abcd-4abc-8abc-abcdefabcdef', b'a' * 4096),
                ('ABCDEFAB-ABCD-4ABC-8ABC-ABCDEFABCDEF', b'b' * 4096)]
        self.db.executemany('INSERT INTO owner_attachment_links VALUES (?,?)', rows)
        self.db.commit()
        self.error('directory_key_schema_unavailable', self.ledger.rebuild_directory)
        self.assertEqual(self.db.execute('SELECT * FROM owner_attachment_links').fetchall(), rows)

    def test_oversized_text_and_blob_keys_never_cross_sql_boundary(self):
        for table in ('owner_attachment_links', 'owner_attachment_outcomes'):
            for key in ('z' * 1_000_000, b'z' * 1_000_000, '🍎' * 36):
                with self.subTest(table=table, key_type=type(key).__name__, key_size=len(key)):
                    self.db.execute('INSERT INTO ' + table + ' VALUES (?,?)', (key, b' ' * 4096))
                    self.db.commit()
                    previous_factory = self.db.row_factory
                    def bounded_rows(cursor, row):
                        for value in row:
                            if isinstance(value, (str, bytes)):
                                self.assertLessEqual(len(value), 4096, 'unbounded SQL result fetched')
                        return row
                    self.db.row_factory = bounded_rows
                    tracemalloc.start()
                    try:
                        self.error('directory_identity_unavailable', self.ledger.rebuild_directory)
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                        self.db.row_factory = previous_factory
                    self.assertLess(peak, 256_000, 'unbounded Python key allocation')
                    self.assertIsNone(self.directory._building)
                    self.assertEqual(self.directory._state.entries, {})
                    self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
                    self.assertEqual(self.db.execute('SELECT length(CAST(owner_request_id AS BLOB)),length(record) FROM ' + table).fetchone(),
                                     (len(key.encode('utf-8')) if isinstance(key, str) else len(key), 4096))
                    self.db.execute('DELETE FROM ' + table)
                    self.db.commit()
                    self.ledger.rebuild_directory()

    def test_oversized_later_key_discards_partial_candidate_without_publication(self):
        self.seed(64)
        key = 'z' * 1_000_000
        self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)', (key, b' ' * 4096))
        self.db.commit()
        prior = self.directory._state
        self.directory.begin_rebuild()
        self.assertFalse(self.directory.rebuild_step())
        self.error('directory_identity_unavailable', self.directory.rebuild_step)
        self.assertIs(self.directory._state, prior)
        self.assertEqual(len(prior.entries), 64)
        self.assertTrue(all(len(k.encode('utf-8')) <= 36 for k in prior.entries))
        self.assertIsNone(self.directory._building)
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)

    def test_batched_rebuild_never_calls_full_record_reader(self):
        self.seed(130)
        self.directory.begin_rebuild()
        steps = []
        with patch.object(self.ledger, '_read', side_effect=AssertionError('per-ID scan forbidden')):
            while True:
                done, queries = self.trace(self.directory.rebuild_step)
                steps.append(queries)
                self.assertFalse(self.db.in_transaction)
                if done:
                    break
        self.assertEqual(len(steps), 3)
        for queries in steps:
            self.assertLessEqual(len(queries), 22)
            self.assertTrue(any('LIMIT 64' in q for q in queries))
            self.assertFalse(any('SELECT data FROM requests' in q for q in queries))
        self.assertEqual(self.counts()[0], 130)

    def test_rebuild_releases_owner_lock_between_batches(self):
        self.seed(65)
        self.directory.begin_rebuild()
        self.assertFalse(self.directory.rebuild_step())
        acquired = []
        def check():
            held = self.t.lock.acquire(timeout=1)
            acquired.append(held)
            if held:
                self.t.lock.release()
        thread = threading.Thread(target=check)
        thread.start(); thread.join(2)
        self.assertEqual(acquired, [True])
        self.assertTrue(self.directory.rebuild_step())

    def test_three_flags_incrementally_and_after_rebuild(self):
        self.t.admit(); self.t.claim()
        self.t.event('ended', turn_id=TURN)
        for rebuild in (False, True):
            if rebuild:
                self.ledger.rebuild_directory()
            e = self.entry()
            self.assertFalse(e.execution_unresolved)
            self.assertFalse(e.terminal_acked)
            self.assertTrue(e.capacity_pending and e.pending_ack)
            self.assertEqual(self.counts()[2], 1)
        self.t.ack()
        for rebuild in (False, True):
            if rebuild:
                self.ledger.rebuild_directory()
            e = self.entry()
            self.assertFalse(e.execution_unresolved or e.capacity_pending or e.pending_ack)
            self.assertTrue(e.terminal_acked)
        self.t.prune(); self.t.event('reply', turn_id=TURN, reply='Fictional late evidence')
        for rebuild in (False, True):
            if rebuild:
                self.ledger.rebuild_directory()
            e = self.entry()
            self.assertFalse(e.execution_unresolved or e.capacity_pending or e.payload_present)
            self.assertTrue(e.terminal_acked and e.pending_ack)
            self.assertEqual(self.counts()[2], 0)

    def test_mixed_ordinary_capacity_uses_sticky_pin_not_pending_ack(self):
        self.t.admit(); self.t.claim(); self.t.event('ended', turn_id=TURN)
        for _ in range(7):
            self.t.ordinary('unknown')
        for rebuild in (False, True):
            if rebuild:
                self.ledger.rebuild_directory()
            self.error('pending_capacity', lambda: self.ledger.admit(self.t.admission(), now_ms=NOW + 61_000))
        self.t.ack(); self.t.prune(); self.t.event('reply', turn_id=TURN, reply='Fictional late evidence')
        for rebuild in (False, True):
            if rebuild:
                self.ledger.rebuild_directory()
            with self.ledger._transaction():
                retained, history, pending, recent = self.ledger._counts(NOW + 61_000)
                self.assertEqual(pending, 7)
        self.assertEqual(self.ledger.admit(self.t.admission(), now_ms=NOW + 61_000)['state'], 'owner_recorded')

    def test_cache_cannot_be_used_under_only_python_or_deferred_lock(self):
        self.error('writer_transaction_required', lambda: self.directory.counts(NOW))
        with self.t.lock:
            self.error('writer_transaction_required', lambda: self.directory.pending_ids(1))
            self.db.execute('BEGIN')
            try:
                self.error('writer_transaction_required', lambda: self.directory.counts(NOW))
            finally:
                self.db.rollback()
        result, queries = self.trace(lambda: self.counts())
        self.assertEqual(queries[0], 'BEGIN IMMEDIATE')
        self.assertTrue(all(i > 0 for i, q in enumerate(queries) if 'data_version' in q))
        self.assertEqual(result[0], 0)

    def test_another_thread_cannot_borrow_an_active_writer_boundary(self):
        result = []
        def check():
            try:
                self.directory.counts(NOW)
            except m.LedgerError as error:
                result.append(error.code)
        with self.ledger._transaction():
            thread = threading.Thread(target=check)
            thread.start(); thread.join(2)
            self.assertEqual(result, ['writer_transaction_required'])
            self.assertEqual(self.directory.counts(NOW)[0], 0)

    def test_incremental_cache_publish_requires_own_commit_boundary(self):
        self.error('commit_boundary_required', lambda: self.directory._committed({}, (0, 0, 0)))
        with self.ledger._transaction():
            self.error('commit_boundary_required', lambda: self.directory._committed({}, (0, 0, 0)))
        self.assertEqual(self.counts()[0], 0)

    def test_external_commit_invalidates_empty_pending_until_rebuilt(self):
        with closing(sqlite3.connect(self.t.path)) as other:
            other.execute('INSERT INTO expired_controls VALUES (?)', (str(uuid.uuid4()),)); other.commit()
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.ledger.rebuild_directory()
        self.assertEqual(self.ledger.pending_outcomes(), [])

    def test_same_connection_raw_change_and_rollback_invalidate(self):
        self.db.execute('INSERT INTO expired_controls VALUES (?)', (str(uuid.uuid4()),)); self.db.rollback()
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.ledger.rebuild_directory()
        self.assertEqual(self.ledger.pending_outcomes(), [])

    def test_same_connection_schema_change_invalidates_without_dml(self):
        before = self.db.total_changes
        self.db.execute('CREATE TABLE fictional_schema_marker(id INTEGER)')
        self.assertEqual(self.db.total_changes, before)
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.ledger.rebuild_directory()
        self.assertEqual(self.ledger.pending_outcomes(), [])

    def test_external_change_between_preliminary_read_and_writer_begin(self):
        self.assertEqual(self.counts()[0], 0)  # Preliminary read is not authority.
        other = sqlite3.connect(self.t.path)
        original = fixtures.FaultConnection.execute
        armed = [True]
        def execute(connection, sql, *args, **kwargs):
            if connection is self.db and sql == 'BEGIN IMMEDIATE' and armed[0]:
                armed[0] = False
                other.execute('INSERT INTO expired_controls VALUES (?)', (str(uuid.uuid4()),)); other.commit()
            return original(connection, sql, *args, **kwargs)
        try:
            with patch.object(fixtures.FaultConnection, 'execute', execute):
                self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        finally:
            other.close()
        self.assertFalse(self.db.in_transaction)

    def test_external_id_reservation_seen_after_writer_begin(self):
        a = self.t.admission()
        other = sqlite3.connect(self.t.path)
        original = fixtures.FaultConnection.execute
        armed = [True]
        def execute(connection, sql, *args, **kwargs):
            if connection is self.db and sql == 'BEGIN IMMEDIATE' and armed[0]:
                armed[0] = False
                other.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)', (a.owner_request_id, b' ' * 4096)); other.commit()
            return original(connection, sql, *args, **kwargs)
        try:
            with patch.object(fixtures.FaultConnection, 'execute', execute):
                self.error('namespace_conflict', lambda: self.ledger.admit(a, now_ms=NOW))
        finally:
            other.close()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_writer_transaction_blocks_competing_reservation(self):
        with closing(sqlite3.connect(self.t.path, timeout=0)) as other:
            with self.ledger._transaction():
                self.directory.require_current()
                with self.assertRaises(sqlite3.OperationalError):
                    other.execute('INSERT INTO expired_controls VALUES (?)', (str(uuid.uuid4()),))
                other.rollback()
        self.assertEqual(self.counts()[0], 0)

    def test_rebuild_aborts_on_external_change_without_retry_loop(self):
        self.seed(65)
        self.directory.begin_rebuild()
        self.assertFalse(self.directory.rebuild_step())
        with closing(sqlite3.connect(self.t.path)) as other:
            other.execute('INSERT INTO expired_controls VALUES (?)', (str(uuid.uuid4()),)); other.commit()
        self.error('directory_rebuild_changed', self.directory.rebuild_step)
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.error('directory_rebuild_not_started', self.directory.rebuild_step)
        self.ledger.rebuild_directory()
        self.assertEqual(self.counts()[0], 65)

    def test_incremental_commit_wins_over_stale_rebuild_snapshot(self):
        self.seed(65)
        self.directory.begin_rebuild()
        self.assertFalse(self.directory.rebuild_step())
        self.t.admit(self.t.admission(), at=NOW + 61_000)
        self.error('directory_rebuild_changed', self.directory.rebuild_step)
        self.assertTrue(self.directory.ready)
        self.assertEqual(self.counts()[0], 66)
        self.assertEqual(self.counts()[2], 1)

    def test_one_bad_pair_is_charged_without_poisoning_other_outcomes(self):
        self.t.admit(); bad_id = self.t.a.owner_request_id
        self.db.execute('UPDATE owner_attachment_links SET record=? WHERE owner_request_id=?', (b'x' * 4096, bad_id)); self.db.commit()
        self.ledger.rebuild_directory()
        bad = self.entry(bad_id)
        self.assertTrue(bad.quarantined and bad.capacity_pending and bad.payload_present)
        self.assertFalse(bad.terminal_acked)
        good = self.ledger.admit(self.t.admission(), now_ms=NOW)
        self.assertEqual(self.counts()[2], 2)
        pending = self.ledger.pending_outcomes()
        self.assertEqual([s['owner_request_id'] for s in pending], [good['owner_request_id']])

    def test_orphan_outcome_and_namespace_collision_are_charged(self):
        key = str(uuid.uuid4())
        self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)', (key, b' ' * 4096)); self.db.commit()
        self.ledger.rebuild_directory()
        self.assertTrue(self.entry(key).quarantined)
        self.assertEqual(self.counts()[:3], (1, 1, 1))
        self.t.admit()
        self.db.execute('INSERT INTO expired_controls VALUES (?)', (self.t.a.owner_request_id,)); self.db.commit()
        self.ledger.rebuild_directory()
        self.assertTrue(self.entry().quarantined)
        self.assertEqual(self.counts()[:3], (2, 2, 2))

    def test_selected_bad_payload_quarantines_only_that_pending_entry(self):
        self.t.admit(); bad_id = self.t.a.owner_request_id
        self.db.execute('UPDATE requests SET data=? WHERE id=?', ('{}', bad_id)); self.db.commit()
        self.ledger.rebuild_directory()
        good = self.ledger.admit(self.t.admission(), now_ms=NOW)
        self.assertEqual([s['owner_request_id'] for s in self.ledger.pending_outcomes()], [good['owner_request_id']])
        self.assertTrue(self.entry(bad_id).quarantined)
        self.assertEqual(self.counts()[2], 2)

    def test_commit_failure_invalidates_cache_without_releasing_capacity(self):
        self.t.admit(); self.t.claim(); self.t.event('ended', turn_id=TURN)
        self.db.commit_fault = 'before'
        with self.assertRaises(sqlite3.OperationalError):
            self.t.ack()
        self.db.commit_fault = ''
        self.assertFalse(self.directory.ready)
        self.error('attachment_index_unavailable', self.ledger.pending_outcomes)
        self.ledger.rebuild_directory()
        self.assertTrue(self.entry().capacity_pending)
        self.assertTrue(self.entry().pending_ack)
        self.db.commit_fault = 'after'
        with self.assertRaises(sqlite3.OperationalError):
            self.t.ack()
        self.db.commit_fault = ''
        self.ledger.rebuild_directory()
        self.assertFalse(self.entry().capacity_pending)
        self.assertFalse(self.entry().pending_ack)

    def test_pending_reads_only_selected_records(self):
        self.seed(256)
        good = self.ledger.admit(self.t.admission(), now_ms=NOW + 61_000)
        with patch.object(self.ledger, '_read', wraps=self.ledger._read) as read:
            pending = self.ledger.pending_outcomes(1)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(pending[0]['owner_request_id'], good['owner_request_id'])

    def test_pending_snapshots_never_read_more_than_fifty_records(self):
        self.seed(100, pending=True)
        with patch.object(self.ledger, '_read', wraps=self.ledger._read) as read:
            result, queries = self.trace(lambda: self.ledger.pending_outcomes(50))
        self.assertEqual(len(result), 50)
        self.assertEqual(read.call_count, 50)
        self.assertLessEqual(len(queries), 750)
        self.assertEqual(self.counts()[2], 0)

    def test_directory_does_not_add_tables_or_terminal_allocations(self):
        self.t.admit(); self.t.claim()
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({t for t in tables if t.startswith('owner_attachment')},
                         {'owner_attachment_links', 'owner_attachment_outcomes'})
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        self.db.execute('PRAGMA max_page_count=' + str(pages))
        self.t.event('reply', turn_id=TURN, reply='\x01' * 16000); self.t.ack()
        self.t.prune(); self.t.event('reply', turn_id=TURN, reply='Fictional late evidence'); self.t.ack()
        self.ledger.rebuild_directory()
        self.assertEqual(self.db.execute('PRAGMA page_count').fetchone()[0], pages)
        self.assertEqual(self.counts()[:3], (1, 0, 0))

    def test_epoch_error_does_not_leave_a_writer_transaction_open(self):
        with patch.object(self.directory, 'epoch', side_effect=sqlite3.OperationalError('fictional epoch failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.ledger.pending_outcomes()
        self.assertFalse(self.db.in_transaction)
        self.assertFalse(self.ledger._writer_active)
        self.assertFalse(self.directory.ready)
        self.ledger.rebuild_directory()
        self.assertEqual(self.ledger.pending_outcomes(), [])


if __name__ == '__main__':
    unittest.main()
