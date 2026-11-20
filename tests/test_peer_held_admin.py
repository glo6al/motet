"""Owner-only Bridge held inventory over production loopback HTTP."""

import json
import os
import unittest
from unittest.mock import patch

import test_peer_admin as fixtures
from test_peer_admin import uid
import peer_admin as admin
import peer_bridge
import peer_lifecycle as lifecycle


HELD = '/bridge-admin/v1/held'
RETIRE = '/bridge-admin/v1/retire'


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class HeldAdminTests(unittest.TestCase):
    setUp = fixtures.HTTPTests.setUp
    cleanup_http = fixtures.HTTPTests.cleanup_http
    http = fixtures.HTTPTests.http

    def held(self, body=None):
        status, value = self.http(HELD, json.dumps({} if body is None else body))
        self.assertEqual(status, 200, value)
        self.assertEqual(set(value), {'items', 'next_cursor', 'checked_at'})
        self.assertEqual(value['checked_at'], self.clock.return_value)
        return value

    def add_shared(self, *, state='unknown', name='Fictional recipient', launched=True, **row_fields):
        cid, tid, agent = uid(), uid(), 'x:' + uid()
        snapshot = dict(self.snapshot, agent_id=agent, name=name, project_label='/fictional/private-project')
        row = dict(self.row, request_id=tid, agent=agent, state=state, launched=launched,
                   question='PRIVATE QUESTION', error='PRIVATE ERROR', path='/fictional/private-transcript')
        if state == 'queued' or not launched:
            row.pop('bound_turn')
        row.update(row_fields)
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                              (cid, agent, json.dumps(snapshot), 'a'*64, tid, uid(), self.created,
                               self.created + 1 if launched else None, 'PRIVATE PREVIEW'))
        self.store._save(row)
        return cid, tid, row

    def add_legacy(self):
        db = self.store.db
        tid, native = uid(), uid()
        principal = peer_bridge.enroll(db, 'Fictional legacy', 'codex', native, self.tmp.name, {})
        db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                   (uid(), uid(), principal['endpoint_id'], 'a'*64, tid, self.created, self.created + 1))
        self.store._save(dict(self.row, request_id=tid, agent='x:' + native, bridge_version=1))
        return tid

    def retire(self, item, agent=None):
        return self.http(RETIRE, json.dumps(dict(item['selector'], agent=agent or item['agent'], confirm=admin.CONFIRM)))

    def test_authentication_route_and_bounded_json(self):
        for headers in ({'X-Live-Room-Key': ''}, {'X-Live-Room-Key': 'fictional-peer-key'},
                        {'Origin': 'https://elsewhere.test'}, {'Host': 'elsewhere.test'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.http(HELD, '{', headers)[0], 403)
        self.assertEqual(self.http(HELD, '{}', {'Origin': f'http://localhost:{self.port}'})[0], 200)
        for path in (HELD + '?', HELD + '?cursor=foo', HELD + '#fragment', '/bridge-admin/v1/%68eld'):
            self.assertEqual(self.http(path, '{}')[0], 400)
        for body in ('{"cursor":null}', '{"cursor":3}', '{"cursor":"bad"}', '{"limit":25}',
                     '{"cursor":"one","cursor":"two"}', '[]', 'null', '{', '{"cursor":1e999}',
                     '{"cursor":"2:9223372036854775808:' + 'a'*64 + '"}'):
            with self.subTest(body=body):
                self.assertEqual(self.http(HELD, body)[0], 400)
        self.assertEqual(self.http(HELD, ' ' * (admin.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.http(HELD, '{}', method='GET')[0], 400)

    def test_held_open_and_exclusions_are_private_and_read_only(self):
        db = self.store.db
        held, held_tid, _ = self.add_shared(name='Fictional hold')
        active, active_tid, _ = self.add_shared(state='awaiting_reply', name='Fictional open')
        submitted, _, _ = self.add_shared(state='queued', name='/fictional/private-path-as-name')
        pruned, pruned_tid, _ = self.add_shared(name='Fictional pruned')
        legacy_tid = self.add_legacy()
        finished, _, _ = self.add_shared(settled=True, execution_outcome='completed', capture_disposition='withheld')
        dropped, _, _ = self.add_shared(state='failed', execution_outcome='dropped', bound_turn='')
        retired, retired_tid, _ = self.add_shared()
        never_launched, _, _ = self.add_shared(state='sending', launched=False)
        inbox, inbox_alias, _ = self.add_shared()
        self.store._save(dict(self.row, request_id=uid(), origin='owner', question='PRIVATE DESK'))
        with db:
            db.execute('DELETE FROM requests WHERE id IN (?,?)', (pruned_tid, legacy_tid))
            evidence = lifecycle.get(db, pruned_tid)['evidence']
            evidence['codex_retention_hold'] = lifecycle.CODEX_RETENTION_HOLD
            db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?', (json.dumps(evidence), pruned_tid))
            db.execute('INSERT INTO peer_inbox_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (inbox, uid(), 'c'*64, 'a'*64, inbox_alias, 100000, 200000, 'queued', None, None, None))
        with self.store.lock, db:
            lifecycle.retire(db, retired_tid, lifecycle.RETIRE_REASON)
        self.assertEqual({item['selector'].get('request_id') for item in self.held()['items']
                          if item['group'] == 'open'}, set())
        self.clock.return_value = self.created + peer_bridge.TTL
        before = db.total_changes
        db.execute('PRAGMA query_only=ON')
        try:
            result = self.held()
            self.assertEqual(self.held(), result)
        finally:
            db.execute('PRAGMA query_only=OFF')
        self.assertEqual(db.total_changes, before)
        items = {next(iter(item['selector'].values())): item for item in result['items']}
        self.assertEqual(set(items), {self.cid, held, active, submitted, pruned, legacy_tid})
        self.assertFalse({finished, dropped, retired, inbox, never_launched} & items.keys())
        self.assertEqual(items[held]['name'], 'Fictional hold')
        self.assertEqual(items[legacy_tid]['selector'], {'transport_id': legacy_tid})
        self.assertEqual(items[legacy_tid]['name'], 'Fictional legacy')
        self.assertEqual(items[submitted]['name'], 'Codex')
        self.assertEqual(items[pruned]['reason_code'], 'codex_retention')
        for key in (held, pruned, legacy_tid):
            self.assertEqual(items[key]['group'], 'held')
            self.assertIsNone(items[key]['held_since'])
            self.assertIsNone(items[key]['episode_id'])
            self.assertIsNone(items[key]['notice_due_at'])
        for key in (active, submitted):
            self.assertEqual(items[key]['group'], 'open')
            self.assertIsNone(items[key]['held_since'])
            self.assertEqual(items[key]['reason'], 'No end recorded; work may continue.')
        self.assertTrue(all(item['clearable'] for item in items.values()))
        for sentinel in ('PRIVATE', '/fictional/', held_tid, active_tid):
            self.assertNotIn(sentinel, json.dumps(result))

    def test_current_hold_episode_comes_from_durable_evidence(self):
        db = self.store.db
        record = lifecycle.get(db, self.tid)
        lifecycle.begin_hold_episode(record['evidence'], created=self.created, launched=self.created + 1, now=110.)
        with db:
            db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?', (json.dumps(record['evidence']), self.tid))
        self.clock.return_value = 111.
        first, = self.held()['items']
        self.assertEqual((first['held_since'], first['notice_due_at']), (110., 410.))
        self.clock.return_value = 411.
        self.assertEqual(self.held()['items'], [first])

    def test_unverifiable_rows_stay_visible_without_leaking_damaged_fields(self):
        db = self.store.db
        missing, tid_missing, _ = self.add_shared()
        bad_snapshot, _, snapshot_row = self.add_shared()
        invalid_record, tid_record, _ = self.add_shared()
        protected, tid_protected, protected_row = self.add_shared()
        changed_turn, tid_turn, row = self.add_shared()
        invalid_id, tid_id, _ = self.add_shared()
        bad_agent, _, _ = self.add_shared()
        bad_date, _, _ = self.add_shared()
        with db:
            db.execute('DELETE FROM peer_execution WHERE request_id=?', (tid_missing,))
            db.execute('UPDATE shared_bridge_requests SET target_snapshot=? WHERE client_id=?',
                       (json.dumps(dict(self.snapshot, agent_id=uid(), name='PRIVATE MISBOUND LABEL')), bad_snapshot))
            db.execute('UPDATE peer_execution SET execution=?,evidence=? WHERE request_id=?',
                       ('completed', '{PRIVATE MALFORMED', tid_record))
            db.execute('UPDATE requests SET data=? WHERE id=?',
                       (json.dumps(dict(protected_row, attachment_owner_version=None)), tid_protected))
            db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(dict(row, bound_turn='wrong-turn')), tid_turn))
            db.execute('UPDATE shared_bridge_requests SET client_id=? WHERE client_id=?', ('PRIVATE MALFORMED ID', invalid_id))
            db.execute('UPDATE shared_bridge_requests SET target=? WHERE client_id=?', ('PRIVATE MALFORMED TARGET', bad_agent))
            db.execute('UPDATE shared_bridge_requests SET created=? WHERE client_id=?', ('PRIVATE MALFORMED DATE', bad_date))
        result = self.held()
        unavailable = [row for row in result['items'] if row['group'] == 'unavailable']
        self.assertEqual(len(unavailable), 8)
        self.assertTrue(all(item['unavailable_reason'] == admin.UNAVAILABLE and not item['clearable'] for item in unavailable))
        by_id = {item['selector']['request_id']: item for item in unavailable if item['selector']}
        self.assertEqual(set(by_id), {missing, bad_snapshot, invalid_record, protected, changed_turn, bad_agent, bad_date})
        self.assertIsNone(by_id[bad_agent]['agent'])
        self.assertEqual(by_id[bad_agent]['provider'], 'unknown')
        self.assertIsNone(by_id[bad_date]['created_at'])
        self.assertEqual(by_id[bad_snapshot]['agent'], snapshot_row['agent'])
        self.assertTrue(all(len(item['item_id']) == 64 for item in unavailable))
        self.assertEqual(len({item['item_id'] for item in unavailable}), 8)
        self.assertEqual(self.held(), result)
        for sentinel in ('PRIVATE', 'wrong-turn', tid_id, invalid_id):
            self.assertNotIn(sentinel, json.dumps(result))

    def test_keyset_pagination_spans_groups_and_survives_anchor_retirement(self):
        self.clock.return_value = self.created + peer_bridge.TTL
        for index in range(52):
            self.add_shared(state='awaiting_reply' if index % 2 else 'unknown')
        self.add_legacy()
        first = self.held()
        self.assertEqual(len(first['items']), 50)
        self.assertTrue(first['next_cursor'])
        self.assertEqual({item['group'] for item in first['items']}, {'held', 'open'})
        anchor = first['items'][-1]
        self.assertEqual(self.retire(anchor)[0], 200)
        second = self.held({'cursor': first['next_cursor']})
        self.assertEqual(len(second['items']), 4)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({item['item_id'] for item in first['items'] + second['items']}), 54)
        forged = first['next_cursor'][:-64] + '0'*64
        self.assertEqual(self.http(HELD, json.dumps({'cursor': forged}))[0], 400)

    def test_orphan_blocker_is_visible_without_a_selector_and_can_anchor_a_page(self):
        db = self.store.db
        with db:
            db.execute('DELETE FROM shared_bridge_requests WHERE client_id=?', (self.cid,))
        self.assertEqual(lifecycle.blockers(db, self.agent), [(self.tid, 'uncertain', self.created)])
        before = db.total_changes
        with patch.object(self.store, '_peer_payload', side_effect=AssertionError('orphan body read')):
            orphan, = self.held()['items']
        self.assertEqual(db.total_changes, before)
        self.assertEqual((orphan['agent'], orphan['provider'], orphan['group']), (self.agent, 'codex', 'unavailable'))
        self.assertIsNone(orphan['selector'])
        self.assertIsNone(orphan['held_since'])
        self.assertFalse(orphan['clearable'])
        self.assertEqual(orphan['unavailable_reason'], admin.UNAVAILABLE)
        self.assertNotIn(self.tid, json.dumps(orphan))
        self.assertNotIn(self.cid, json.dumps(orphan))

        other, _, _ = self.add_shared()
        inbox_alias, inbox_tid, _ = self.add_shared()
        terminal, terminal_tid, _ = self.add_shared()
        with db:
            db.execute('DELETE FROM shared_bridge_requests WHERE client_id IN (?,?,?)', (other, inbox_alias, terminal))
            db.execute('INSERT INTO peer_inbox_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (inbox_tid, uid(), 'c'*64, 'a'*64, uid(), 100000, 200000, 'queued', None, None, None))
            db.execute('UPDATE peer_execution SET execution=? WHERE request_id=?', ('completed', terminal_tid))
        for _ in range(49):
            self.add_shared()
        first = self.held()
        self.assertEqual(len(first['items']), 50)
        self.assertIsNone(first['items'][-1]['selector'])
        second = self.held({'cursor': first['next_cursor']})
        self.assertEqual(second['items'], [orphan])
        self.assertIsNone(second['next_cursor'])

    def test_exact_clear_rechecks_current_evidence_after_listing(self):
        item, = self.held()['items']
        self.assertEqual(self.retire(item, 'c:' + uid())[0], 409)
        self.store._save(dict(self.row, settled=True, execution_outcome='completed', capture_disposition='withheld'))
        self.assertEqual(self.retire(item)[0], 409)
        self.assertEqual(self.held()['items'], [])
        self.assertEqual(lifecycle.get(self.store.db, self.tid)['execution'], 'completed')


if __name__ == '__main__':
    unittest.main()
