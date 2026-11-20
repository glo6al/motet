"""Production WATCH HTTP, SQLite, and Claude plugin completion recovery."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

import test_claude_mod as owner_fixture
import chat_directory
import peer_bridge
import peer_lifecycle
import server


RUNNER = Path(__file__).resolve().parents[1] / 'integrations/claude-mod/tests/proven-end.integration.mjs'
ANSWER = 'Fictional answer that must stay private.'
OWNER_INPUT = 'Fictional extra owner input that must stay private.'


@unittest.skipIf(os.name == 'nt' or not shutil.which('node'), 'Requires POSIX and Node')
class ClaudeProvenEndIntegration(unittest.TestCase):
    http = owner_fixture.ClaudeModTests.http
    setUp = owner_fixture.ClaudeModTests.setUp
    tearDown = owner_fixture.ClaudeModTests.tearDown

    def reserve_requests(self, request_id, sibling, sibling_request):
        targets = {item['agent_id']: item for item in self.store.shared.directory.list_agents()['agents']}
        for agent, key in ((self.aid, request_id), (sibling, sibling_request)):
            self.store.shared.send(self.credential, agent, targets[agent]['identity_revision'],
                                   'Report the fictional status.', key, 'Fictional requester')

    def snapshot(self, request_id, sibling, sibling_request):
        with self.store.lock:
            db = self.store.db
            transport, = db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                                    (request_id,)).fetchone()
            sibling_transport, = db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                                            (sibling_request,)).fetchone()
            public = self.store.shared.result(request_id)
            return dict(
                transport=transport,
                public=public,
                lifecycle=peer_lifecycle.get(db, transport),
                raw=db.execute('SELECT data FROM requests WHERE id=?', (transport,)).fetchone()[0],
                events=db.execute('''SELECT event_id,kind,reason,occurred,before_state,after_state,fingerprint
                                     FROM peer_execution_events WHERE request_id=? ORDER BY rowid''', (transport,)).fetchall(),
                capacity=peer_lifecycle.capacity_error(db, self.aid, time.time(), peer_bridge.TTL),
                sibling_blockers=peer_lifecycle.blockers(db, sibling),
                sibling_transport=sibling_transport,
            )

    def exercise(self, mode):
        sibling_native = owner_fixture.uid()
        sibling = 'c:' + sibling_native
        server.AGENTS[sibling] = dict(runtime='Claude', name='Fictional sibling', cwd=self.project)
        transcripts = self.base / 'claude-projects' / 'fictional'; transcripts.mkdir(parents=True)
        for native, name in ((self.sid, 'Fictional target'), (sibling_native, 'Fictional sibling')):
            (transcripts / (native + '.jsonl')).write_text(json.dumps(dict(
                type='custom-title', customTitle=name, sessionId=native, cwd=self.project)) + '\n')
        codex = self.base / 'codex'; (codex / 'sessions').mkdir(parents=True)
        directory = chat_directory.ChatDirectory(str(codex), str(transcripts.parent),
            policy={'providers': ['claude'], 'history_days': 'all'}, cache_seconds=0,
            live_snapshot=lambda: server.shared_directory_snapshot(self.store))
        self.store.shared.directory = directory
        for _ in range(30):
            if directory.refresh(force=True)['complete']:
                break
        self.assertTrue(directory.status()['complete'])
        with self.store.lock, self.store.db:
            credential = peer_bridge.initialize_service(self.store.db, {'providers': ['claude'], 'history_days': 'all'})
            self.credential = peer_bridge.service_credential(self.store.db, credential['credential_id'])
        status, setup = self.http('/claude-mod-setup', {'peer_receive': True}, owner=True)
        self.assertEqual(status, 200, setup)

        request_id, sibling_request = owner_fixture.uid(), owner_fixture.uid()
        registration = None
        started, recoverable, before_end, after_end = [], [], [], []
        actual_call = self.store.mods.call

        def snapshot():
            return self.snapshot(request_id, sibling, sibling_request)

        def observe(method, data, *args, **kwargs):
            nonlocal registration
            target = data.get('native_id') == self.sid
            kind = data.get('kind') if target and method == 'event' else None
            if kind == 'completed':
                before_end.append(snapshot())
            result = actual_call(method, data, *args, **kwargs)
            if target and method == 'register' and result['lifecycle_protocol'] == 2 and registration is None:
                registration = result['client_id']
                self.reserve_requests(request_id, sibling, sibling_request)
            if kind == 'started':
                started.append(snapshot())
            if (target and method == 'poll' and data['client_id'] != registration and result['recoverable_turns']):
                recoverable.append(snapshot())
            if kind == 'completed':
                after_end.append(snapshot())
            return result

        with patch.object(self.store.mods, 'call', side_effect=observe):
            process = subprocess.run(['node', str(RUNNER)], input=json.dumps(dict(
                mode=mode, port=server.PORT, code=setup['pair_code'], owner_key=server.KEY,
                project=self.project, target=self.sid, sibling=sibling_native, request_id=request_id,
                answer=ANSWER, owner_input=OWNER_INPUT)), text=True, capture_output=True, timeout=25)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        node = json.loads(process.stdout)
        self.assertEqual(len(started), 1)
        start, = started
        receipt = node['completion']
        self.assertEqual(start['transport'], receipt['request_id'])
        self.assertEqual(start['public']['progress']['phase'], 'started')
        self.assertIsNotNone(start['public']['progress']['started_at'])
        self.assertIsNone(start['public']['progress']['ended_at'])
        self.assertIn('active request', start['capacity'])
        self.assertEqual([blocker[:2] for blocker in start['sibling_blockers']],
                         [(start['sibling_transport'], 'reserved')])
        expected_events = 2 if mode == 'lost_ack' else 1
        self.assertEqual(node['completionPosts'], expected_events)
        self.assertEqual(len(before_end), expected_events)
        self.assertEqual(len(after_end), expected_events)
        if mode != 'lost_ack':
            self.assertGreaterEqual(len(recoverable), 2, 'production polls before and after unrelated events')
            for pending in recoverable:
                progress = pending['public']['progress']
                self.assertEqual((progress['phase'], pending['lifecycle']['capture']), ('held', 'pending'))
                self.assertEqual((pending['public']['state'], pending['public']['reply']), ('unknown', ''))
                self.assertIsNotNone(progress['held_reason'])
                self.assertIsNone(progress['ended_at'])
                self.assertIn('requires recovery', pending['capacity'])
        end = snapshot()
        self.assertEqual(end['lifecycle'], after_end[-1]['lifecycle'])
        self.assertEqual(end['events'], after_end[-1]['events'])
        self.assertEqual(end['sibling_blockers'], start['sibling_blockers'])
        self.assertIsNone(end['capacity'])
        public = end['public']
        proof = end['lifecycle']['evidence']
        self.assertEqual((public['state'], public['reply']), ('unknown', ''))
        self.assertEqual(public['agent_id'], self.aid)
        self.assertEqual(public['progress']['started_at'], start['public']['progress']['started_at'])
        self.assertEqual(end['lifecycle']['capture'], 'withheld')
        self.assertEqual(proof['mod_turn'], receipt['turn_id'])
        self.assertEqual(proof['mod_attempt_id'], receipt['attempt_id'])
        self.assertEqual(proof['mod_generation'], node['originalStart']['generation'])
        self.assertNotIn('answer_digest', proof)
        for sentinel in (OWNER_INPUT, ANSWER):
            self.assertNotIn(sentinel, json.dumps(end))
        with self.store.lock:
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM shared_bridge_requests WHERE launched IS NOT NULL').fetchone(), (1,))
        if mode == 'retired_race':
            self.assertEqual(end['lifecycle'], before_end[0]['lifecycle'])
            self.assertEqual(end['lifecycle']['execution'], 'retired')
            self.assertEqual(proof['owner_retirement']['provider_outcome'], 'unknown')
            self.assertNotIn('completion_event_id', proof)
            self.assertEqual(public['progress']['phase'], 'retired')
            self.assertIsNone(public['progress']['ended_at'])
            self.assertEqual([event[1:3] for event in end['events'] if event[1] in ('retired_late_event', 'completed')],
                             [('retired_late_event', 'completed')])
            return

        self.assertEqual(end['lifecycle']['execution'], 'completed')
        self.assertEqual(public['progress']['phase'], 'ended')
        self.assertIsNone(public['progress']['held_reason'])
        self.assertIsNotNone(public['progress']['ended_at'])
        self.assertIn('reply withheld', public['progress']['summary'].lower())
        completed, = [event for event in end['events'] if event[1] == 'completed']
        self.assertEqual(completed[0], receipt['event_id'])
        self.assertEqual(proof['completion_event_id'], receipt['event_id'])
        self.assertEqual(completed[2:4], ('withheld', receipt['occurred_at']))
        self.assertEqual(proof['completion_fingerprint'], completed[-1])
        self.assertEqual((before_end[0]['lifecycle']['capture'], before_end[0]['capacity'] is not None), ('pending', True))
        if mode == 'lost_ack':
            self.assertEqual(after_end[0]['lifecycle'], after_end[1]['lifecycle'])
            self.assertEqual(after_end[0]['events'], after_end[1]['events'])
            self.assertEqual(after_end[0]['lifecycle'], before_end[1]['lifecycle'])
            self.assertIsNone(before_end[1]['capacity'])

    def test_reloaded_plugin_replays_the_original_receipt_after_committed_ack_loss(self):
        self.exercise('lost_ack')

    def test_replacement_runtime_observes_the_exact_original_turn_end(self):
        self.exercise('reloaded')

    def test_owner_retirement_wins_after_replacement_receives_recovery_proof(self):
        self.exercise('retired_race')


if __name__ == '__main__':
    unittest.main()
