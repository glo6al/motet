"""Exact Codex completion through the signed Bridge with fictional native I/O."""
import json
from http.client import HTTPConnection
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from test_peer_integration import CONNECTOR, RPC, SDK

import chat_directory
import officer_messaging
import peer_admin
import peer_bridge
import peer_lifecycle
import server


NODE = shutil.which('node')
OWNER_KEY = 'fictional-proven-end-owner'
PRIVATE_ANSWER = 'Fictional answer that must stay private.'


@unittest.skipIf(os.name == 'nt' or not NODE or not SDK.is_dir(),
                 'Needs POSIX, Node.js, and optional channel npm dependencies')
class CodexProvenEndIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.state = self.root / 'state'
        self.codex = self.root / 'codex'
        self.sessions = self.codex / 'sessions'
        self.claude = self.root / 'claude-projects'
        for path in (self.project, self.state, self.sessions, self.claude):
            path.mkdir(mode=0o700, parents=True)

        self.transcripts = {}
        with (self.codex / 'session_index.jsonl').open('w') as index:
            for name in ('Cedar', 'Alder'):
                native_id = str(uuid.uuid4())
                path = self.sessions / ('rollout-' + native_id + '.jsonl')
                self.append(path, 'session_meta', id=native_id, cwd=str(self.project),
                            source='vscode', agent_path='/root')
                index.write(json.dumps(dict(id=native_id, thread_name=name)) + '\n')
                self.transcripts['x:' + native_id] = path

        self.launches = self.root / 'launches.jsonl'
        self.cli = self.root / 'codex-fixture'
        self.cli.write_text(f'#!{sys.executable}\n' + '''import json
import sys

command, thread_flag, thread, message_flag, envelope = sys.argv[1:]
assert (command, thread_flag, message_flag) == ('queue', '--thread', '--message')
assert envelope.startswith('WATCH_PEER_REQUEST ')
with open(LAUNCH_LOG, 'a') as log:
    log.write(json.dumps([thread, envelope]) + '\\n')
'''.replace('LAUNCH_LOG', repr(str(self.launches))))
        self.cli.chmod(0o700)

        self.store = self.open_store()
        self.addCleanup(lambda: self.store.close())
        with self.store.db:
            credential = peer_bridge.initialize_service(self.store.db, {'history_days': 'all'})
        http = server.Server(('127.0.0.1', 0), server.H)
        self.addCleanup(http.server_close)
        self.port = http.server_port
        settings = patch.multiple(server, MESSAGING=self.store, PORT=self.port, KEY=OWNER_KEY)
        settings.start()
        self.addCleanup(settings.stop)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(http.shutdown)
        self.credential = self.state / 'fixture-credential.json'
        self.credential.write_text(json.dumps(dict(credential, port=self.port)))
        self.credential.chmod(0o600)

    @staticmethod
    def append(path, record_type, **payload):
        with path.open('a') as transcript:
            transcript.write(json.dumps(dict(type=record_type, payload=payload)) + '\n')

    def open_store(self):
        store = officer_messaging.MessagingStore(str(self.state), str(self.sessions), cli=str(self.cli),
                                                channel_root=str(self.root / 'channels'))
        store.available = store.codex_available = True
        store.shared.directory = chat_directory.ChatDirectory(
            str(self.codex), str(self.claude), policy={'history_days': 'all'}, cache_seconds=0,
            live_snapshot=lambda: {agent: {'capability': 'reachable', 'reason': 'Fictional Codex queue'}
                                   for agent in self.transcripts})
        return store

    def restart(self):
        self.store.close()
        self.store = self.open_store()
        server.MESSAGING = self.store

    def rpc(self, method, params=None, *, denied_by=None):
        env = dict(os.environ, TEST_SDK=SDK.as_uri(), TEST_ADAPTER=str(CONNECTOR / 'peer.mjs'),
                   TEST_CREDENTIAL=str(self.credential), TEST_METHOD=method,
                   TEST_PARAMS=json.dumps(params or {}))
        result = subprocess.run([NODE, '--input-type=module', '-e', RPC], env=env, cwd=self.project,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        tool = json.loads(result.stdout)
        if denied_by:
            self.assertTrue(tool.get('isError'), tool)
            self.assertIn('Peer request rejected (429)', tool['content'][0]['text'])
            self.assertIn(denied_by, tool['content'][0]['text'])
            return
        self.assertFalse(tool.get('isError'), tool)
        wrapped = json.loads(tool['content'][0]['text'])
        self.assertEqual(wrapped['origin'], 'peer')
        return wrapped['untrusted_peer_data']

    @staticmethod
    def request(target, text):
        return dict(agent_id=target['agent_id'], identity_revision=target['identity_revision'],
                    request_id=str(uuid.uuid4()), text=text)

    def row(self, request):
        raw, = self.store.db.execute('''SELECT requests.data FROM requests
            JOIN shared_bridge_requests ON requests.id=shared_bridge_requests.transport_id
            WHERE shared_bridge_requests.client_id=?''', (request['request_id'],)).fetchone()
        return json.loads(raw)

    def read_launches(self):
        return [json.loads(line) for line in self.launches.read_text().splitlines()]

    def owner_retire(self, request):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('POST', '/bridge-admin/v1/retire', json.dumps(
                dict(request_id=request['request_id'], agent=request['agent_id'], confirm=peer_admin.CONFIRM)),
                {'X-Live-Room-Key': OWNER_KEY, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200, body)
            self.assertEqual((body['status'], body['provider_outcome']), ('retired', 'unknown'))
        finally:
            connection.close()

    def test_exact_terminal_after_restart_releases_only_its_recipient(self):
        listing = self.rpc('list_agents')
        targets = {target['name']: target for target in listing['agents']}
        self.assertTrue(listing['directory']['complete'])
        self.assertEqual(set(targets), {'Cedar', 'Alder'})
        cedar = self.request(targets['Cedar'], 'Fictional Cedar request')
        alder = self.request(targets['Alder'], 'Fictional Alder request')
        for request in (cedar, alder):
            self.assertEqual(self.rpc('send_message', request)['state'], 'sending')
        self.store.tick()
        launched = self.read_launches()
        self.assertCountEqual([thread for thread, _ in launched],
                              [cedar['agent_id'][2:], alder['agent_id'][2:]])

        turns = {}
        for thread, envelope in launched:
            agent = 'x:' + thread
            turn = turns[agent] = str(uuid.uuid4())
            path = self.transcripts[agent]
            self.append(path, 'event_msg', type='task_started', turn_id=turn)
            self.append(path, 'response_item', type='message', role='user', turn_id=turn,
                        content=[dict(type='input_text', text=envelope)])
        self.store.tick()
        for request in (cedar, alder):
            self.assertEqual(self.row(request)['bound_turn'], turns[request['agent_id']])

        cedar_id, alder_id = cedar['request_id'], alder['request_id']
        cedar_transport, alder_transport = self.row(cedar)['request_id'], self.row(alder)['request_id']
        cedar_path, alder_path = (self.transcripts[request['agent_id']] for request in (cedar, alder))
        cedar_turn, alder_turn = (turns[request['agent_id']] for request in (cedar, alder))
        cedar_next = self.request(targets['Cedar'], 'Fictional next Cedar request')
        alder_next = self.request(targets['Alder'], 'Fictional next Alder request')

        # An exact ID inside an unreadable record is insufficient to prove an end.
        self.append(cedar_path, 'event_msg', type='task_complete', turn_id=cedar_turn,
                    last_agent_message='x' * officer_messaging.MAX_READ)
        self.store.tick()
        self.assertTrue(self.row(cedar).get('codex_discard_record'))
        self.assertNotEqual(peer_lifecycle.get(self.store.db, cedar_transport)['execution'], 'completed')
        self.restart()
        self.assertTrue(self.rpc('list_agents')['directory']['complete'])

        self.append(cedar_path, 'event_msg', type='task_complete', turn_id=alder_turn)
        self.append(cedar_path, 'event_msg', type='task_complete')
        self.store.tick()
        self.assertFalse(self.row(cedar).get('codex_discard_record'))
        self.rpc('send_message', cedar_next, denied_by=cedar_id)
        self.rpc('send_message', alder_next, denied_by=alder_id)
        self.assertEqual(self.read_launches(), launched)

        # The independently readable exact terminal needs no WATCH_REPLY marker.
        self.append(cedar_path, 'event_msg', type='task_complete', turn_id=cedar_turn,
                    last_agent_message=PRIVATE_ANSWER)
        self.store.tick()
        self.rpc('send_message', alder_next, denied_by=alder_id)
        self.assertEqual(self.rpc('send_message', cedar_next)['state'], 'sending')
        self.assertEqual(self.read_launches(), launched)

        answer = self.rpc('get_reply', {'request_id': cedar_id})
        history = {row['request_id']: row for row in self.rpc('list_requests')['requests']}
        self.assertEqual((answer['request_id'], answer['state'], answer['reply']), (cedar_id, 'unknown', ''))
        self.assertNotIn('reply', history[cedar_id])
        for progress in (answer['progress'], history[cedar_id]['progress']):
            self.assertEqual((progress['phase'], progress['held_reason']), ('ended', None))
            self.assertIsNotNone(progress['ended_at'])
            self.assertTrue(progress['summary'].startswith('Finished; reply withheld. '))
        ended_at = answer['progress']['ended_at']
        self.assertEqual(history[cedar_id]['progress']['ended_at'], ended_at)
        self.assertIn('reply is withheld', answer['error'])
        self.assertNotIn(PRIVATE_ANSWER, json.dumps([answer, history]))
        completed = peer_lifecycle.get(self.store.db, cedar_transport)
        self.assertEqual((completed['execution'], completed['capture']), ('completed', 'withheld'))
        self.assertEqual(completed['evidence']['bound_turn'], cedar_turn)
        self.assertNotIn(PRIVATE_ANSWER, json.dumps(self.row(cedar)))

        self.append(cedar_path, 'event_msg', type='task_complete', turn_id=cedar_turn)
        with alder_path.open('a') as transcript:
            transcript.write('{"type":"event_msg"\n')
        self.store.tick()
        duplicate = self.rpc('send_message', cedar)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['progress']['ended_at'], ended_at)

        self.rpc('send_message', alder_next, denied_by=alder_id)
        self.owner_retire(alder)
        self.append(alder_path, 'event_msg', type='task_complete', turn_id=alder_turn,
                    last_agent_message='WATCH_REPLY ' + alder_transport + '\n' + PRIVATE_ANSWER)
        self.restart()
        self.store.tick()

        restarted = self.rpc('get_reply', {'request_id': cedar_id})
        self.assertEqual(restarted['progress']['ended_at'], ended_at)
        self.assertEqual((restarted['progress']['phase'], restarted['reply']), ('ended', ''))
        retired = self.rpc('get_reply', {'request_id': alder_id})
        self.assertEqual((retired['state'], retired['reply'], retired['progress']['phase']),
                         ('unknown', '', 'retired'))
        self.assertIsNone(retired['progress']['ended_at'])
        self.assertNotIn(PRIVATE_ANSWER, json.dumps(retired))
        record = peer_lifecycle.get(self.store.db, alder_transport)
        self.assertEqual((record['execution'], record['capture']), ('retired', 'withheld'))
        self.assertFalse(peer_lifecycle.blockers(self.store.db, alder['agent_id']))
        self.assertEqual(self.read_launches(),
                         launched + [[cedar['agent_id'][2:], self.row(cedar_next)['envelope']]])


if __name__ == '__main__':
    unittest.main()
