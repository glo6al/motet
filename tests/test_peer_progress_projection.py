"""Public native progress is a read-only projection of exact shared receipts."""
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

import test_shared_bridge as fixtures
import peer_bridge
import peer_lifecycle
import peer_progress


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class ProjectionTests(unittest.TestCase):
    setUp = fixtures.SharedTests.setUp
    tearDown = fixtures.SharedTests.tearDown
    target = fixtures.SharedTests.target
    append = fixtures.SharedTests.append
    send = fixtures.SharedTests.send
    row = fixtures.SharedTests.row

    def observe(self, row, kind, seconds):
        self.store._peer_progress_clock = lambda: row['created'] + seconds
        self.assertTrue(peer_progress.observe(self.store, row, kind))

    def codex(self, *, state='queued', bound=''):
        result = self.send()
        row = self.row(result['request_id'])
        row.update(state=state, launched=True, bound_turn=bound)
        self.store.db.execute('UPDATE shared_bridge_requests SET launched=created WHERE transport_id=?', (row['request_id'],))
        self.store._save(row)
        return result, row

    def test_codex_acceptance_start_fallback_and_last_read_use_only_durable_facts(self):
        sent, row = self.codex()
        rid = sent['request_id']
        self.assertEqual(sent['progress']['phase'], 'waiting_dispatch')
        submitted = self.bridge.result(rid)['progress']
        self.assertIsNone(submitted['accepted_at'])
        self.assertEqual(submitted['phase'], 'unavailable')
        self.observe(row, 'accepted', 5)
        self.assertEqual(self.bridge.result(rid)['progress']['phase'], 'accepted')
        row.update(bound_turn='fictional-turn', state='awaiting_reply')
        self.store._save(row)
        self.observe(row, 'checked', 6)
        with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 3666):
            result = self.bridge.result(rid)
        progress = result['progress']
        self.assertEqual((result['state'], progress['phase'], progress['started_at']), ('awaiting_reply', 'started', None))
        self.assertRegex(progress['last_checked_at'], r'\+00:00$')
        self.assertTrue(progress['summary'].startswith('Start recorded; start time unavailable.'))
        self.assertIn('No end observed yet. Motet last read its log', progress['summary'])
        self.assertIn('(1 hour ago)', progress['summary'])
        self.assertNotIn('running', progress['summary'])

    def test_claude_checked_is_always_null_and_no_log_clause_is_emitted(self):
        agent, rid, tid = 'c:' + fixtures.uid(), fixtures.uid(), fixtures.uid()
        created = peer_bridge.time.time() - 10
        revision = 'a' * 64
        snapshot = dict(agent_id=agent, provider='claude', identity_revision=revision, project_id='b' * 64)
        row = dict(origin='peer', bridge_version=2, request_id=tid, agent=agent, created=created,
                   state='queued', runtime='claude', transport='claude_mod', launched=True,
                   identity_revision=revision, mod_enrollment=fixtures.uid(), mod_generation=fixtures.uid(),
                   mod_attempt_id=fixtures.uid(), mod_project='/fictional/project', target_project='/fictional/project')
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                              (rid, agent, json.dumps(snapshot), 'c' * 64, tid, self.credential['credential_id'],
                               created, created, 'Fictional preview'))
        self.store._save(row)
        self.observe(row, 'claim_issued', 1)
        claim = self.bridge.result(rid)['progress']
        self.assertEqual(claim['phase'], 'claim_issued')
        self.assertIsNone(claim['accepted_at'])
        self.assertIn('Native submission unconfirmed.', claim['summary'])
        row.update(mod_turn='fictional-turn', state='awaiting_reply')
        self.store._save(row)
        self.observe(row, 'started', 2)
        self.observe(row, 'checked', 3)
        progress = self.bridge.result(rid)['progress']
        self.assertEqual(progress['phase'], 'started')
        self.assertIsNone(progress['last_checked_at'])
        self.assertNotIn('last read', progress['summary'])

    def test_completed_reply_visible_only_on_get_and_malformed_progress_is_optional(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        rid = sent['request_id']
        row.update(state='answered', settled=True, reply='Fictional answer body')
        self.store._save(row)
        self.observe(row, 'ended', 1)
        current = self.bridge.result(rid)
        self.assertEqual(current['reply'], 'Fictional answer body')
        self.assertEqual(current['progress']['phase'], 'ended')
        before = self.store.db.total_changes
        listed = self.bridge.list_requests()['requests'][0]
        self.assertEqual(listed['progress'], current['progress'])
        self.assertNotIn('Fictional answer body', json.dumps(listed))
        self.assertEqual(self.store.db.total_changes, before)
        evidence = peer_lifecycle.get(self.store.db, row['request_id'])['evidence']
        evidence['progress']['extra'] = '/fictional/private/path'
        self.store.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?',
                              (json.dumps(evidence), row['request_id']))
        fallback = self.bridge.result(rid)
        self.assertEqual(fallback['reply'], 'Fictional answer body')
        self.assertNotIn('progress', fallback)
        self.assertNotIn('/fictional/private', json.dumps(fallback))

    def test_protected_payloads_and_misdirected_targets_omit_progress(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        rid = sent['request_id']
        for classification in ('conflict', 'uncertain'):
            with self.subTest(classification=classification), patch.object(self.store, '_peer_payload', return_value=(classification, None)):
                self.assertNotIn('progress', self.bridge.result(rid))
        target = dict(sent['target'], agent_id=self.a['agent_id'])
        self.store.db.execute('UPDATE shared_bridge_requests SET target_snapshot=? WHERE client_id=?',
                              (json.dumps(target), rid))
        self.assertNotIn('progress', self.bridge.result(rid))

    def test_busy_names_public_request_and_preserves_not_accepted_suffix(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        self.observe(row, 'started', 1)
        now = row['created'] + peer_bridge.RETENTION + 1
        before = self.store.db.total_changes
        with patch.object(peer_bridge.time, 'time', return_value=now):
            progress = self.bridge.result(sent['request_id'])['progress']
            formatted = self.bridge._recipient_capacity(row['request_id'], row['agent'], 'running')
        self.assertEqual(self.store.db.total_changes, before)
        stamp = datetime.fromtimestamp(int(row['created'] + 1), timezone.utc).isoformat()
        self.assertEqual((progress['phase'], progress['started_at']), ('started', stamp))
        self.assertIn(f'Start observed at {stamp} (1 day ago).', progress['summary'])
        self.assertIn('past the content retention window', progress['summary'])
        self.assertTrue(formatted.startswith(f"Busy with request ({sent['request_id']})"))
        self.assertIn('Start observed', formatted)
        with self.assertRaises(peer_bridge.PeerError) as denied:
            self.send()
        message = str(denied.exception)
        self.assertEqual(denied.exception.status, 429)
        self.assertIn(sent['request_id'], message)
        self.assertNotIn(row['request_id'], message)
        self.assertIn('Start observed', message)
        self.assertTrue(message.endswith('. The new request was not accepted.'))
        self.assertLessEqual(len(message), 512)


if __name__ == '__main__':
    unittest.main()
