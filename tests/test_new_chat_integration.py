"""New chat room/HTTP integration with fictional native metadata only."""
import http.client
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import server
from new_chat_types import Capability, ExecutionResult, FrozenFolder, OpenResult, VerifiedIdentity


@unittest.skipIf(os.name == 'nt', 'Pinned native metadata requires POSIX')
class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.folder = self.home / 'fictional-project'
        self.folder.mkdir()
        self.sessions = self.home / '.codex' / 'sessions'
        self.sessions.mkdir(parents=True)
        self.ident = str(uuid.uuid4())
        self.aid = 'x:' + self.ident
        self.path = self.sessions / ('rollout-' + self.ident + '.jsonl')
        self.meta = dict(id=self.ident, cwd=str(self.folder), source='appServer')
        self.path.write_text(json.dumps(dict(type='session_meta', payload=self.meta)) + '\n')
        st, fs = self.path.stat(), self.folder.stat()
        self.identity = VerifiedIdentity(self.ident, str(self.folder), str(self.path), st.st_dev, st.st_ino,
                                         'fixture', fs.st_dev, fs.st_ino)
        self.frozen = FrozenFolder('fictional-id', '1', 'Fictional project', str(self.folder), fs.st_dev, fs.st_ino)
        self.tails = {}
        self.patches = [patch.object(server, 'HOME', str(self.home)), patch.object(server, 'AGENTS', {}),
                        patch.object(server, 'LINKS', {}), patch.object(server, 'LOG', []),
                        patch.object(server, 'TITLES', {}), patch.object(server, 'HISTORY', None),
                        patch.object(server, 'HISTORY_BASELINED', False)]
        for p in self.patches: p.start()
        self.index()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def index(self, cwd=None, archived=0):
        with closing(sqlite3.connect(self.home / '.codex' / 'state_5.sqlite')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
            db.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?)',
                       (self.ident, str(self.path), cwd or str(self.folder), archived, 'appServer'))
            db.commit()

    def test_blank_identity_admits_once_without_message_or_activity(self):
        self.assertEqual(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True), self.aid)
        before = list(server.LOG)
        reader = self.tails[str(self.path)]
        self.assertEqual(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True), self.aid)
        self.assertIs(self.tails[str(self.path)], reader)
        self.assertEqual(server.LOG, before)
        self.assertEqual(server.AGENTS[self.aid]['cwd'], str(self.folder))
        self.assertTrue(server.LOG)
        self.assertEqual({event['kind'] for event in server.LOG}, {'agent'})
        self.assertEqual(len(server.AGENTS), 1)

    def test_source_replaced_after_receipt_is_not_admitted(self):
        replacement = self.path.with_suffix('.replacement')
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)

    def test_folder_replaced_after_intent_is_not_admitted(self):
        self.folder.rename(self.home / 'former-folder')
        self.folder.mkdir()
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)

    def test_wrong_index_folder_and_archived_identity_do_not_enter_room(self):
        self.index(cwd=str(self.home))
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.index(archived=1)
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)

    def test_existing_conflicting_monitor_is_not_overwritten(self):
        server.AGENTS[self.aid] = dict(runtime='Codex', cwd=str(self.home))
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertEqual(server.AGENTS[self.aid]['cwd'], str(self.home))
        self.assertFalse(self.tails)

    def test_saved_parent_without_live_row_is_not_resurrected_as_new_root(self):
        parent = 'x:' + str(uuid.uuid4())
        saved = {'parent': parent, 'seen': 1}
        server.LINKS[self.aid] = dict(saved)
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertEqual(server.LINKS[self.aid], saved)
        self.assertFalse(server.AGENTS)
        self.assertFalse(server.LOG)
        self.assertFalse(self.tails)

    def test_guard_rechecks_metadata_before_publication(self):
        with patch.object(server, 'CodexTail', wraps=server.CodexTail) as constructor:
            real_constructor = constructor._mock_wraps
            def replace_then_construct(path, identity_guard):
                changed = dict(self.meta, id=str(uuid.uuid4()))
                self.path.write_text(json.dumps(dict(type='session_meta', payload=changed)) + '\n')
                return real_constructor(path, identity_guard=identity_guard)
            constructor.side_effect = replace_then_construct
            self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)
        self.assertFalse(self.tails)

    def test_replacement_after_guard_before_tail_initialization_never_publishes(self):
        real_constructor = server.CodexTail
        def swap_after_guard(path, identity_guard):
            def guarded(meta):
                valid = identity_guard(meta)
                replacement = self.path.with_suffix('.replacement')
                replacement.write_bytes(self.path.read_bytes())
                replacement.replace(self.path)
                return valid
            return real_constructor(path, identity_guard=guarded)
        with patch.object(server, 'CodexTail', side_effect=swap_after_guard):
            self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)
        self.assertFalse(server.LOG)
        self.assertFalse(self.tails)

    def test_authenticated_http_to_durable_native_receipt_to_room_with_fake_adapter(self):
        self.http_launch_flow('blank')

    def test_authenticated_work_first_turn_and_read_only_request_recovery(self):
        self.http_launch_flow('work')

    def http_launch_flow(self, mode):
        from new_chat_service import NewChatService
        ident = str(uuid.uuid4())
        new_path = self.sessions / ('rollout-' + ident + '.jsonl')
        opened = threading.Event()
        owner = self
        class Adapter:
            creates = 0
            starts = 0
            proof = None
            def preflight(self):
                return Capability(True, version='fixture', supports_work=True)
            def create_once(self, folder, receipt):
                self.creates += 1
                receipt(ident, folder.path)
                new_path.write_text(json.dumps(dict(type='session_meta', payload=dict(
                    id=ident, cwd=folder.path, source='appServer'))) + '\n')
                with closing(sqlite3.connect(owner.home / '.codex' / 'state_5.sqlite')) as db:
                    db.execute('INSERT INTO threads VALUES(?,?,?,?,?)',
                               (ident, str(new_path), folder.path, 0, 'appServer'))
                    db.commit()
                info = new_path.stat()
                self.proof = VerifiedIdentity(ident, folder.path, str(new_path), info.st_dev,
                    info.st_ino, 'fixture', folder.device, folder.inode)
                return self.proof
            def verify_existing(self, native_id, folder):
                return self.proof if native_id == ident else None
            def open_existing(self, identity, draft):
                owner.assertEqual(draft, 'Fictional draft; leave unsent.' if mode == 'blank' else None)
                opened.set()
                return OpenResult('accepted')
            def start_work_once(self, identity, instruction, claim, receipt):
                owner.assertEqual(instruction, 'Name three fictional apple stands.')
                owner.assertEqual(identity, self.proof)
                claim()
                self.starts += 1
                turn = str(uuid.uuid4())
                receipt(turn)
                return ExecutionResult('accepted', turn)
            def execution_status(self, identity, turn):
                return ExecutionResult('completed', turn)
            def close(self):
                pass
        adapter = Adapter()
        store = NewChatService(self.home / 'private-state', adapter, lambda: [self.identity], enabled=True)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        def request(method, route, data=None):
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            conn.request(method, route, json.dumps(data) if data is not None else None,
                {'X-Live-Room-Key': 'fictional-key', 'Content-Type': 'application/json'})
            response = conn.getresponse()
            code, raw = response.status, response.read()
            conn.close()
            return code, json.loads(raw)
        try:
            with patch.object(server, 'NEW_CHAT', store), patch.object(server, 'PORT', httpd.server_port), \
                    patch.object(server, 'KEY', 'fictional-key'):
                thread.start()
                _, context = request('GET', '/api/new-chat/context')
                folder = context['folders'][0]
                selected = dict(provider='codex', folderId=folder['id'], folderRevision=folder['revision'], mode=mode)
                code, intent = request('POST', '/api/new-chat/intents', selected)
                self.assertEqual(code, 201)
                payload = dict(selected, requestId=intent['requestId'])
                payload.update({'draft': 'Fictional draft; leave unsent.'} if mode == 'blank' else
                               {'instruction': 'Name three fictional apple stands.'})
                code, attempt = request('POST', '/api/new-chat/attempts', payload)
                self.assertEqual(code, 202)
                self.assertIsNone(attempt['session'])
                self.assertTrue(opened.wait(2))
                # Drain the fake worker before invoking the watcher-owned admission.
                with store.lock:
                    jobs = list(store.jobs.values())
                for job in jobs: job.join(2)
                store.process_admissions(lambda proof, frozen, permit: server.admit_new_chat(proof, frozen, self.tails, permit))
                code, final = request('GET', '/api/new-chat/attempts/' + attempt['attemptId'])
                self.assertEqual((code, final['state']), (200, 'added'))
                self.assertEqual(final['session']['roomAgentId'], 'x:' + ident)
                code, replay = request('POST', '/api/new-chat/attempts', payload)
                self.assertEqual((code, replay['attemptId']), (200, attempt['attemptId']))
                self.assertEqual(adapter.creates, 1)
                code, recovered = request('GET', '/api/new-chat/requests/' + intent['requestId'])
                self.assertEqual((code, recovered['status']), (200, 'attempted'))
                recovered = recovered['attempt']
                self.assertEqual(recovered['attemptId'], attempt['attemptId'])
                self.assertEqual(adapter.starts, 1 if mode == 'work' else 0)
                if mode == 'work':
                    self.assertEqual(recovered['execution']['outcome'], 'completed')
                    self.assertIsNotNone(recovered['execution']['turnId'])
                self.assertNotIn('Name three fictional apple stands.', json.dumps(recovered))
                self.assertNotIn('Fictional draft; leave unsent.', json.dumps(final))
                self.assertEqual({e['kind'] for e in server.LOG}, {'agent'})
        finally:
            httpd.shutdown(); httpd.server_close()
            if thread.is_alive(): thread.join(2)
            store.close()

    def test_expired_publication_permit_leaves_no_monitor_or_event(self):
        self.assertIsNone(server.admit_new_chat(self.identity, self.frozen, self.tails, lambda: False))
        self.assertFalse(server.AGENTS)
        self.assertFalse(server.LOG)
        self.assertFalse(self.tails)

    def test_inventory_uses_verified_native_cwd_not_display_metadata(self):
        server.AGENTS[self.aid] = dict(runtime='Codex', cwd='/fictional/forged', project='Not authority')
        with patch.object(server.sys, 'platform', 'darwin'):
            values = server.new_chat_inventory()
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0].cwd, str(self.folder))
            self.index(archived=1)
            self.assertEqual(server.new_chat_inventory(), [])


class DisabledHTTPTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(server, 'NEW_CHAT', None), patch.object(server, 'KEY', 'fictional-owner-key')]
        for p in self.patches: p.start()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        self.portpatch = patch.object(server, 'PORT', self.httpd.server_port)
        self.portpatch.start()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        self.portpatch.stop()
        for p in reversed(self.patches): p.stop()

    def request(self, method, path, body=None, key='fictional-owner-key'):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=3)
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {'X-Live-Room-Key': key})
        response = connection.getresponse()
        status, cache = response.status, response.getheader('Cache-Control')
        data = response.read()
        connection.close()
        return status, cache, data

    def test_missing_service_namespace_requires_owner_before_reporting_unavailable(self):
        for method, path, body in [('GET', '/api/new-chat/context', None),
                                   ('POST', '/api/new-chat/attempts', {})]:
            self.assertEqual(self.request(method, path, body, key='wrong')[0], 403)
            status, cache, data = self.request(method, path, body)
            self.assertEqual(status, 503)
            self.assertEqual(cache, 'no-store')
            self.assertIsInstance(json.loads(data), dict)

    def test_new_chat_is_enabled_in_the_release(self):
        self.assertIs(server.NEW_CHAT_ENABLED, True)

    def test_rollout_gate_is_not_controlled_by_environment(self):
        for enabled, env_value in ((False, '1'), (True, '0')):
            with self.subTest(enabled=enabled), patch.object(server, 'NEW_CHAT_ENABLED', enabled), \
                    patch.dict(os.environ, {'NEW_CHAT_ENABLED': env_value, 'WATCH_NEW_CHAT': env_value}), \
                    patch('new_chat_service.NewChatService') as service, patch('codex_new_chat.Adapter'), \
                    patch.object(server.sys, 'platform', 'darwin'):
                server.start_new_chat()
                self.assertIs(service.call_args.kwargs['enabled'], enabled)

    def test_unsupported_platform_never_constructs_storage_or_adapter(self):
        with patch.object(server.sys, 'platform', 'win32'), \
                patch('new_chat_service.NewChatService') as service, patch('codex_new_chat.Adapter') as adapter:
            self.assertIsNone(server.start_new_chat())
            service.assert_not_called()
            adapter.assert_not_called()

    def test_dedicated_demo_mode_forces_independent_service_guard(self):
        with patch.dict(os.environ, {'WATCH_DEMO': '1'}), \
                patch('new_chat_service.NewChatService') as service, patch('codex_new_chat.Adapter'), \
                patch.object(server.sys, 'platform', 'darwin'):
            server.start_new_chat()
            self.assertIs(service.call_args.kwargs['demo'], True)


if __name__ == '__main__':
    unittest.main()
