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


OWNER_CLEAR = ' If that turn has stopped, the owner can clear this request.'


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
        self.assertEqual(sent['progress']['summary'], 'Waiting to be sent.')
        submitted = self.bridge.result(rid)['progress']
        self.assertIsNone(submitted['accepted_at'])
        self.assertEqual(submitted['phase'], 'unavailable')
        self.assertEqual(submitted['summary'], 'Sent. Not confirmed as accepted by the app.')
        self.observe(row, 'accepted', 5)
        for age, suffix in ((900, ''), (901, OWNER_CLEAR)):
            with self.subTest(age=age), patch.object(peer_bridge.time, 'time', return_value=row['created'] + age):
                accepted = self.bridge.result(rid)['progress']
                self.assertEqual(accepted['summary'], f"Accepted by the app at {accepted['accepted_at']} (14 minutes ago). "
                                 f'Start not observed.{suffix}')
        with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 1205):
            accepted = self.bridge.result(rid)['progress']
            busy = self.bridge._recipient_capacity(row['request_id'], row['agent'], 'submitted')
        self.assertEqual(accepted['phase'], 'accepted')
        self.assertEqual(accepted['summary'], f"Accepted by the app at {accepted['accepted_at']} (20 minutes ago). "
                         f'Start not observed.{OWNER_CLEAR}')
        self.assertEqual(busy, f'Busy with request ({rid}). Accepted 20 minutes ago, start not observed. The new request was not accepted.')
        row.update(bound_turn='fictional-turn', state='awaiting_reply')
        self.store._save(row)
        self.observe(row, 'checked', 6)
        with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 3666):
            result = self.bridge.result(rid)
        progress = result['progress']
        self.assertEqual((result['state'], progress['phase'], progress['started_at']), ('awaiting_reply', 'started', None))
        self.assertRegex(progress['last_checked_at'], r'\+00:00$')
        self.assertEqual(progress['summary'], 'Start recorded; start time unavailable. No end observed. '
                         'If that turn has stopped, the owner can clear this request.')
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
        for age, suffix in ((900, ''), (901, OWNER_CLEAR)):
            with self.subTest(age=age), patch.object(peer_bridge.time, 'time', return_value=created + age):
                progress = self.bridge.result(rid)['progress']
                self.assertIsNone(progress['claim_issued_at'])
                self.assertEqual(progress['summary'], 'Handed to the Claude plugin. Not confirmed as submitted.' + suffix)
                self.assertEqual(self.bridge._recipient_capacity(tid, agent, 'submitted'),
                                 f'Busy with request ({rid}). The new request was not accepted.')
        self.observe(row, 'claim_issued', 1)
        with patch.object(peer_bridge.time, 'time', return_value=created + 1201):
            claim = self.bridge.result(rid)['progress']
            busy = self.bridge._recipient_capacity(tid, agent, 'submitted')
        self.assertEqual(claim['phase'], 'claim_issued')
        self.assertIsNone(claim['accepted_at'])
        self.assertEqual(claim['summary'], f"Handed to the Claude plugin at {claim['claim_issued_at']} (20 minutes ago). "
                         f'Not confirmed as submitted.{OWNER_CLEAR}')
        self.assertEqual(busy, f'Busy with request ({rid}). Handed to the Claude plugin 20 minutes ago, '
                         'start not observed. The new request was not accepted.')
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
        now = row['created'] + 5
        with patch.object(peer_bridge.time, 'time', return_value=now):
            current = self.bridge.result(rid)
        self.assertEqual(current['reply'], 'Fictional answer body')
        self.assertEqual(current['progress']['phase'], 'ended')
        before = self.store.db.total_changes
        with patch.object(peer_bridge.time, 'time', return_value=now):
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

    def test_completed_withheld_is_finished_not_a_hold_in_both_public_reads(self):
        for record_time in (False, True):
            with self.subTest(record_time=record_time):
                sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
                row.update(state='unknown', settled=True, tracking_stopped=True,
                           capture_disposition='withheld', reply='', candidate_reply='', final='')
                self.store._save(row)
                if record_time:
                    self.observe(row, 'ended', 1)
                before = self.store.db.total_changes
                with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 5):
                    current = self.bridge.result(sent['request_id'])
                    listed = next(item for item in self.bridge.list_requests()['requests']
                                  if item['request_id'] == sent['request_id'])
                self.assertEqual(self.store.db.total_changes, before)
                self.assertEqual((current['state'], current['reply']), ('unknown', ''))
                self.assertEqual(listed.get('reply', ''), '')
                self.assertEqual(listed['progress'], current['progress'])
                progress = current['progress']
                self.assertEqual(progress['phase'], 'ended')
                self.assertIsNone(progress['held_reason'])
                self.assertTrue(progress['summary'].startswith('Finished; reply withheld. '))
                self.assertNotIn('clear', progress['summary'])
                self.assertNotIn('success', progress['summary'])
                self.assertEqual(progress['ended_at'] is not None, record_time)
                if not record_time:
                    self.assertIn('End recorded; end time unavailable.', progress['summary'])
                record = peer_lifecycle.get(self.store.db, row['request_id'])
                self.assertEqual((record['execution'], record['capture']), ('completed', 'withheld'))
                self.assertEqual(peer_lifecycle.blockers(self.store.db, row['agent']), [])

    def test_protected_payloads_and_misdirected_targets_omit_progress(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        rid = sent['request_id']
        for classification in ('conflict', 'uncertain'):
            with self.subTest(classification=classification), patch.object(self.store, '_peer_payload', return_value=(classification, None)):
                self.assertNotIn('progress', self.bridge.result(rid))
        self.assertIsNone(self.bridge._public_progress(row['request_id'], self.a['agent_id'], sent['target'],
                                                       row['created'], row, False, row['created']))
        target = dict(sent['target'], agent_id=self.a['agent_id'])
        self.store.db.execute('UPDATE shared_bridge_requests SET target_snapshot=? WHERE client_id=?',
                              (json.dumps(target), rid))
        self.assertNotIn('progress', self.bridge.result(rid))

    def test_mismatched_revision_or_turn_omit_progress(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        rid = sent['request_id']
        self.observe(row, 'started', 1)
        for changes in ({'identity_revision': 'a' * 64}, {'bound_turn': 'another-fictional-turn'}):
            with self.subTest(changes=changes):
                self.store.db.execute('UPDATE requests SET data=? WHERE id=?',
                                      (json.dumps(dict(row, **changes)), row['request_id']))
                self.assertNotIn('progress', self.bridge.result(rid))
                self.assertEqual(self.bridge._recipient_capacity(row['request_id'], row['agent'], 'running'),
                                 f'Busy with request ({rid}). The new request was not accepted.')

    def test_clock_before_observation_omits_elapsed_without_discarding_time(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        self.observe(row, 'started', 60)
        with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 1):
            progress = self.bridge.result(sent['request_id'])['progress']
            busy = self.bridge._recipient_capacity(row['request_id'], row['agent'], 'running')
        self.assertEqual(progress['summary'], f"Start observed at {progress['started_at']}. No end observed.")
        self.assertEqual(busy, f"Busy with request ({sent['request_id']}). Start recorded, time unavailable, "
                         'no end observed. The new request was not accepted.')

    def test_busy_uses_only_allowed_progress_phases(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        self.observe(row, 'started', 1)
        self.store.db.execute('UPDATE requests SET data=? WHERE id=?',
                              (json.dumps(dict(row, tracking_stopped=True)), row['request_id']))
        progress = self.bridge.result(sent['request_id'])['progress']
        self.assertEqual(progress['phase'], 'held')
        self.assertEqual(progress['summary'], 'Delivery or completion could not be verified. The owner must clear '
                         'this request before the recipient can take another.')
        self.assertEqual(self.bridge._recipient_capacity(row['request_id'], row['agent'], 'running'),
                         f"Busy with request ({sent['request_id']}). The new request was not accepted.")

    def test_pruned_open_codex_reports_only_the_retention_recovery_reason(self):
        sent, row = self.codex(state='awaiting_reply', bound='fictional-turn')
        self.observe(row, 'started', 1)
        now = row['created'] + peer_bridge.RETENTION + 3600
        with patch.object(peer_bridge.time, 'time', return_value=now):
            self.store._prune(self.store._rows(), now)
            before = self.store.db.total_changes
            result = self.bridge.result(sent['request_id'])
            busy = self.bridge._recipient_capacity(row['request_id'], row['agent'], 'uncertain')
            self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual((result['state'], result['reply']), ('expired', ''))
        self.assertEqual(result['progress']['phase'], 'held')
        held = ('Motet stopped tracking this request at the 24-hour limit before an end was observed. '
                'The owner must clear it before the recipient can take another.')
        self.assertEqual(result['error'], held)
        self.assertEqual(result['progress']['held_reason'], held)
        self.assertEqual(result['progress']['summary'], held)
        self.assertIsNotNone(result['progress']['started_at'])
        self.assertEqual(busy, f"Earlier delivery requires recovery for this recipient ({sent['request_id']}). "
                         'The new request was not accepted; nothing was resent.')
        self.assertLessEqual(len(busy), 200)

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
        self.assertEqual(progress['summary'], f'Start observed at {stamp} (1 day ago). No end observed. '
                         'If that turn has stopped, the owner can clear this request.')
        self.assertEqual(formatted, f"Busy with request ({sent['request_id']}). Start observed 1 day ago, "
                         'no end observed. The new request was not accepted.')
        with patch.object(peer_bridge.time, 'time', return_value=row['created'] + 1201), self.assertRaises(peer_bridge.PeerError) as denied:
            self.send()
        message = str(denied.exception)
        self.assertEqual(denied.exception.status, 429)
        self.assertIn(sent['request_id'], message)
        self.assertNotIn(row['request_id'], message)
        self.assertIn('Start observed', message)
        self.assertTrue(message.endswith('. The new request was not accepted.'))
        self.assertLessEqual(len(message), 200)


if __name__ == '__main__':
    unittest.main()
