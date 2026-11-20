"""Official-mod owner HTTP/outbox contract, fictional service identities only."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
import sqlite3
import socket
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_mod as mod
import officer_messaging as messaging
import peer_lifecycle
import server


def uid():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'Owner outbox requires POSIX private storage')
class ClaudeModTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        legacy_host = patch.multiple(server, HOME=str(self.base), CLAUDE_MANAGED=None,
                                     CLAUDE_MANAGED_ENABLED=False, CLAUDE_MANAGED_FAILED=False)
        legacy_host.start()
        self.addCleanup(legacy_host.stop)
        self.project = os.path.realpath(self.temp.name)
        self.sid, self.aid = uid(), None
        self.aid = 'c:'+self.sid
        self.agents = {self.aid: {'runtime': 'Claude', 'name': 'Fictional', 'cwd': self.project}}
        self.store = messaging.MessagingStore(str(self.base/'state'), str(self.base/'sessions'))
        self.store.codex_available = False
        self.previous = server.MESSAGING, server.KEY, server.PORT, dict(server.AGENTS)
        server.MESSAGING, server.KEY = self.store, 'fictional-owner-key'
        server.AGENTS.clear(); server.AGENTS.update(self.agents)
        self.httpd = server.Server(('127.0.0.1', 0), server.H)
        server.PORT = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = ''
        self.generation = ''

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        server.MESSAGING, server.KEY, server.PORT = self.previous[:3]
        server.AGENTS.clear(); server.AGENTS.update(self.previous[3])
        self.store.close(); self.temp.cleanup()

    def http(self, path, data, owner=False, token=None, **headers):
        headers = dict(headers)
        if owner:
            headers['X-Live-Room-Key'] = server.KEY
        if token is not None:
            headers['Authorization'] = 'Bearer '+token
        conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
        conn.request('POST', path, json.dumps(data), headers)
        response = conn.getresponse()
        raw = response.read(); status = response.status
        conn.close()
        try: return status, json.loads(raw)
        except ValueError: return status, raw.decode()

    def test_mod_close_is_advertised_before_socket_eof(self):
        self.enroll()
        payload = json.dumps(dict(generation=self.generation, native_id=self.sid,
                                  project=self.project, accept=False)).encode()
        for token, success in ((self.token, True), ('fictional-invalid-token', False)):
            with self.subTest(success=success):
                with socket.create_connection(('127.0.0.1', server.PORT), timeout=3) as transport:
                    transport.sendall((f'POST /claude-mod/poll HTTP/1.1\r\nHost: 127.0.0.1:{server.PORT}\r\n'
                        f'Authorization: Bearer {token}\r\nContent-Type: application/json\r\n'
                        f'Content-Length: {len(payload)}\r\n\r\n').encode() + payload)
                    response = http.client.HTTPResponse(transport)
                    response.begin()
                    self.assertEqual(response.status == 200, success)
                    self.assertEqual(response.getheader('Connection'), 'close')
                    self.assertTrue(response.will_close)
                    response.read()
                    self.assertEqual(transport.recv(1), b'', 'server closed the advertised connection')

    def test_owner_keep_alive_and_explicit_client_close_remain_truthful(self):
        conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
        try:
            first_socket = None
            for number in range(3):
                headers = {'X-Live-Room-Key': server.KEY, 'Content-Type': 'application/json'}
                if number == 2:
                    headers['Connection'] = 'close'
                conn.request('POST', '/claude-mod-setup-status', '{}', headers)
                active_socket = conn.sock
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.will_close, number == 2)
                if number == 2:
                    self.assertEqual(response.getheader('Connection'), 'close')
                else:
                    self.assertNotEqual(response.getheader('Connection'), 'close')
                response.read()
                if number == 0:
                    first_socket = active_socket
                else:
                    self.assertIs(active_socket, first_socket, 'ordinary owner reads reuse the connection')
        finally:
            conn.close()

    def test_existing_explicit_close_header_is_not_duplicated(self):
        with patch.object(server, 'ATTACHMENTS', None):
            conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
            try:
                conn.request('GET', '/api/attachments/status', headers={'X-Live-Room-Key': server.KEY})
                response = conn.getresponse()
                self.assertGreaterEqual(response.status, 400)
                self.assertEqual(response.headers.get_all('Connection'), ['close'])
                self.assertTrue(response.will_close)
                response.read()
            finally:
                conn.close()

    def enroll(self):
        status, pair = self.http('/claude-mod-pair', {'agent': self.aid}, owner=True)
        self.assertEqual(status, 200, pair)
        self.assertEqual(pair['command'], f"/watch-connect {server.PORT}:"+pair['pair_code'])
        status, enrollment = self.http('/claude-mod/enroll', {'code': pair['pair_code'], 'native_id': self.sid, 'project': self.project})
        self.assertEqual(status, 200, enrollment)
        self.token, self.generation = enrollment['token'], enrollment['generation']
        return pair, enrollment

    def request(self, question='How is the fictional work going?', request_id=None):
        payload = dict(agent=self.aid, question=question, request_id=request_id or uid())
        status, row = self.http('/agent-message', payload, owner=True)
        self.assertEqual(status, 200, row)
        return payload, row

    def poll(self, accept=True, **override):
        return self.http('/claude-mod/poll', dict(generation=self.generation, native_id=self.sid, project=self.project, accept=accept, **override), token=self.token)

    def event(self, claim, kind, **extra):
        return self.http('/claude-mod/event', dict(generation=self.generation, native_id=self.sid, request_id=claim['request_id'], attempt_id=claim['attempt_id'], kind=kind, **extra), token=self.token)

    def answer(self, claim, turn=None, text='Fictional completion.'):
        return self.event(claim, 'answered', turn_id=turn or uid(), answer='WATCH_REPLY '+claim['request_id']+'\n'+text)

    def row(self, rid):
        return next(r for r in self.store.snapshot()['requests'] if r['request_id'] == rid)

    def reconnect(self):
        status, value = self.http('/claude-mod/connect', {'native_id': self.sid, 'project': self.project}, token=self.token)
        self.assertEqual(status, 200, value)
        self.generation = value['generation']
        return value

    def test_pair_auth_schema_native_project_expiry_and_single_use(self):
        self.assertEqual(self.http('/claude-mod-pair', {'agent':self.aid})[0], 403)
        self.assertEqual(self.http('/claude-mod-pair', {'agent':self.aid,'token':'forged'},owner=True)[0],400)
        self.assertEqual(self.http('/claude-mod-pair', {'agent':'x:'+uid()},owner=True)[0],400)
        pair, enrollment = self.enroll()
        self.assertNotIn('token', pair)
        body = dict(code=pair['pair_code'],native_id=self.sid,project=self.project)
        self.assertEqual(self.http('/claude-mod/enroll',body)[0],403)
        self.assertNotIn(enrollment['token'],json.dumps(self.store.snapshot()))
        stored = self.store.db.execute('SELECT token_hash FROM claude_mod_enrollments').fetchone()[0]
        self.assertNotEqual(stored,enrollment['token'])
        pair = self.store.mods.pair(self.aid,self.agents,server.PORT)
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code'],native_id=uid()))[0],403)
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code'],project='/fictional/wrong'))[0],403)
        self.store.db.execute('UPDATE claude_mod_pairs SET expires=0');self.store.db.commit()
        self.assertEqual(self.http('/claude-mod/enroll',dict(body,code=pair['pair_code']))[0],403)

    def test_owner_http_to_claim_to_exact_ordinary_reply_without_codex(self):
        self.enroll()
        self.assertTrue(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertTrue(self.store.snapshot()['capabilities']['claude'])
        payload, row = self.request()
        self.assertEqual(row['state'],'sending')
        status, poll = self.poll();self.assertEqual(status,200,poll)
        claim = poll['claim']
        self.assertEqual(claim['native_id'],self.sid)
        self.assertEqual(claim['envelope'],messaging.envelope(payload['request_id'],payload['question']))
        self.assertNotIn('reply tool',claim['envelope'])
        turn = uid()
        self.assertEqual(self.event(claim,'started',turn_id=turn)[1]['state'],'awaiting_reply')
        self.assertEqual(self.answer(claim,turn)[1]['state'],'answered')
        row = self.row(payload['request_id'])
        self.assertEqual((row['state'],row['reply']),('answered','Fictional completion.'))
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertIsNone(peer_lifecycle.get(self.store.db, payload['request_id']))

    def test_tick_never_dispatches_polls_or_overwrites_mod_rows(self):
        self.enroll();payload,_=self.request()
        with patch.object(self.store,'_send',side_effect=AssertionError('legacy send')), patch.object(self.store,'_poll',side_effect=AssertionError('legacy poll')):
            self.store.tick()
            self.assertEqual(self.row(payload['request_id'])['state'],'sending')
            claim=self.poll()[1]['claim'];self.answer(claim)
            self.store.tick()
        self.assertEqual(self.row(payload['request_id'])['state'],'answered')

    def test_busy_pipeline_waits_distinct_rows_and_duplicate_clicks_do_not_reclaim(self):
        self.enroll();payload,_=self.request()
        self.assertEqual(self.http('/agent-message',payload,owner=True)[0],200)
        self.assertEqual(self.http('/agent-message',dict(payload,question='Conflict'),owner=True)[0],409)
        second,_=self.request('A distinct later instruction')
        self.assertIsNone(self.poll(False)[1]['claim'])
        first=self.poll()[1]['claim']
        self.assertEqual(first['request_id'],payload['request_id'])
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertIsNone(self.poll(False)[1]['claim'])
        self.answer(first)
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])

    def test_atomic_concurrent_claim_and_owner_duplicate(self):
        self.enroll();payload,_=self.request()
        with ThreadPoolExecutor(6) as pool:
            results=list(pool.map(lambda _:self.poll(),range(12)))
        claims=[v['claim'] for status,v in results if status==200 and v['claim']]
        self.assertEqual(len(claims),1)
        self.assertEqual(len(self.store._rows()),1)
        self.assertEqual(self.http('/agent-message',payload,owner=True)[1]['request_id'],payload['request_id'])

    def test_unknown_after_started_releases_next_distinct_message_without_resubmit(self):
        self.enroll()
        first,_=self.request()
        second,_=self.request('Distinct instruction after the earlier turn ends without a reply')
        claim=self.poll()[1]['claim']
        turn=uid()
        self.assertEqual(self.event(claim,'started',turn_id=turn)[1]['state'],'awaiting_reply')
        self.assertEqual(self.event(claim,'unknown')[1]['state'],'unknown')
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.assertEqual(self.row(first['request_id']).get('reply',''),'')
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])
        self.assertEqual(self.answer(claim,turn)[1]['state'],'answered','late matching reply remains recoverable')

    def test_completion_beats_start_and_settlement_monotonic_idempotent_conflict(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];turn=uid()
        complete=self.answer(claim,turn)
        self.assertEqual(complete[0],200)
        self.assertEqual(self.answer(claim,turn),complete)
        for kind,extra in [('submitted',{}),('started',{'turn_id':turn}),('dropped',{}),('unknown',{})]:
            self.assertEqual(self.event(claim,kind,**extra)[1]['state'],'answered')
        self.assertEqual(self.answer(claim,uid())[0],409)
        self.assertEqual(self.answer(claim,turn,text='Changed answer')[0],409)
        self.assertEqual(self.row(payload['request_id'])['reply'],'Fictional completion.')

    def test_wrong_claim_origin_reply_marker_and_turn_do_not_bind(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];turn=uid()
        wrong=dict(claim,attempt_id=uid())
        self.assertEqual(self.event(wrong,'started',turn_id=turn)[0],409)
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer='Unrelated answer')[0],409)
        bad='WATCH_REPLY '+payload['request_id']+'\nWATCH_REPLY '+uid()
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer=bad)[0],409)
        self.event(claim,'started',turn_id=turn)
        self.assertEqual(self.event(claim,'submitted')[1]['state'],'awaiting_reply')
        self.assertEqual(self.event(claim,'unknown')[1]['state'],'unknown')
        self.assertEqual(self.answer(claim,uid())[0],409)
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.event(claim,'answered',turn_id=turn,answer='x'*16001)[0],400)

    def test_legacy_marker_only_completion_acknowledges_original_bound_turn(self):
        self.enroll()
        for question, suffix in (('Fictional first question.', ''), ('Fictional second question.', '\n \t\x1f\u00a0\u3000')):
            payload, _ = self.request(question)
            claim = self.poll()[1]['claim']
            turn = uid()
            answer = 'WATCH_REPLY ' + claim['request_id'] + suffix
            self.assertEqual(self.event(claim, 'answered', turn_id=turn, answer=answer)[0], 409)
            self.assertEqual(self.event(claim, 'started', turn_id=turn)[0], 200)
            self.assertEqual(self.event(claim, 'answered', turn_id=uid(), answer=answer)[0], 409)
            self.assertEqual(self.event(dict(claim, attempt_id=uid()), 'answered', turn_id=turn, answer=answer)[0], 409)
            self.assertEqual(self.row(payload['request_id'])['state'], 'awaiting_reply')
            completed = self.event(claim, 'answered', turn_id=turn, answer=answer)
            self.assertEqual(completed, (200, dict(request_id=claim['request_id'], state='unknown', turn_id=turn)))
            self.assertEqual(self.event(claim, 'answered', turn_id=turn, answer=answer), completed)
            result = self.row(payload['request_id'])
            self.assertEqual(result['reply'], '')
            self.assertIn('turn completed', result['error'])
            with self.store.lock:
                retained = next(r for r in self.store.mods.rows() if r['request_id'] == claim['request_id'])
            self.assertEqual((retained['execution_outcome'], retained['capture_disposition'], retained['settled']),
                             ('completed', 'unusable', True))

    def test_locked_storage_returns_503_then_accepts_original_completion(self):
        self.enroll()
        payload, _ = self.request()
        claim = self.poll()[1]['claim']
        turn = uid()
        self.assertEqual(self.event(claim, 'started', turn_id=turn)[0], 200)
        locker = sqlite3.connect(self.base/'state'/'officer-messages.sqlite3')
        try:
            locker.execute('BEGIN IMMEDIATE')
            failed_status, failure = self.answer(claim, turn)
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(failed_status, 503, failure)
        self.assertNotIn('locked', json.dumps(failure))
        self.assertEqual(self.row(payload['request_id'])['state'], 'awaiting_reply')
        self.assertEqual(self.answer(claim, turn)[0], 200)
        self.assertEqual(self.row(payload['request_id'])['reply'], 'Fictional completion.')

    def test_malformed_request_stays_400_and_store_value_error_is_503(self):
        self.enroll()
        with patch.object(self.store.mods, 'call', side_effect=ValueError('Fictional internal storage detail')) as call:
            malformed = [(b'{"unfinished":', {}), (b'{"text":"\xff"}', {}), (b'{}', {'Content-Length': 'invalid'})]
            for body, extra_headers in malformed:
                with self.subTest(body=body, extra_headers=extra_headers):
                    conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
                    try:
                        conn.request('POST', '/claude-mod/event', body,
                                     {'Authorization': 'Bearer '+self.token, **extra_headers})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 400)
                        self.assertEqual(json.loads(response.read())['error_code'], 'invalid_request')
                    finally:
                        conn.close()
            call.assert_not_called()
            status, failure = self.http('/claude-mod/event', {}, token=self.token)
            self.assertEqual(status, 503, failure)
            self.assertNotIn('Fictional internal storage detail', json.dumps(failure))

    def test_peer_row_never_claimed_or_exposed_as_owner_mod_history(self):
        self.enroll();payload,_=self.request()
        with self.store.lock,self.store.db:
            row=next(r for r in self.store._rows() if r['request_id']==payload['request_id'])
            row['origin']='peer';row['created']=time.time();self.store.mods.put(row)
        result=self.poll()[1]
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'],[])
        self.assertEqual(self.store.snapshot()['requests'],[])

    def test_service_auth_host_origin_generation_target_and_scope(self):
        self.enroll()
        body=dict(generation=self.generation,native_id=self.sid,project=self.project,accept=True)
        for token in ('bad',server.KEY):
            self.assertEqual(self.http('/claude-mod/poll',body,token=token)[0],403)
        self.assertEqual(self.http('/claude-mod/poll',body,owner=True)[0],403)
        for headers in ({'Host':'localhost:'+str(server.PORT)},{'Origin':'http://127.0.0.1:'+str(server.PORT)}):
            self.assertEqual(self.http('/claude-mod/poll',body,token=self.token,**headers)[0],403)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,generation=uid()),token=self.token)[0],409)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,native_id=uid()),token=self.token)[0],409)
        self.assertEqual(self.http('/claude-mod/poll',dict(body,origin='owner'),token=self.token)[0],400)
        self.assertEqual(self.http('/claude-mod/send_message',body,token=self.token)[0],403)

    def test_disconnect_reconnect_status_and_late_reply_replay_no_resubmit(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim'];old_generation=self.generation
        self.assertEqual(self.http('/claude-mod/disconnect',dict(generation=self.generation,native_id=self.sid),token=self.token)[0],200)
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.reconnect();self.assertNotEqual(old_generation,self.generation)
        result=self.poll()[1]
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'][0]['attempt_id'],claim['attempt_id'])
        self.assertNotIn('envelope',result['requests'][0])
        self.assertEqual(self.answer(claim)[1]['state'],'answered')
        later,_=self.request('Distinct new explicit request after reconnect')
        self.assertEqual(self.poll()[1]['claim']['request_id'],later['request_id'])

    def test_lost_claim_response_reconnect_never_returns_same_payload(self):
        self.enroll();payload,_=self.request();claim=self.poll()[1]['claim']
        self.assertIsNone(self.poll()[1]['claim'])
        self.reconnect()
        status,result=self.poll()
        self.assertEqual(status,200)
        self.assertIsNone(result['claim'])
        self.assertEqual(result['requests'][0]['state'],'unknown')
        self.assertEqual(result['requests'][0]['request_id'],claim['request_id'])

    def test_watch_restart_preserves_waiting_but_attempted_unknown_no_reclaim(self):
        self.enroll();first,_=self.request();claim=self.poll()[1]['claim'];second,_=self.request('Wait through restart')
        self.store.close()
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.assertEqual(self.row(second['request_id'])['state'],'sending')
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.reconnect()
        self.assertEqual(self.poll()[1]['claim']['request_id'],second['request_id'])
        self.assertEqual(self.row(first['request_id'])['state'],'unknown')
        self.answer(claim)
        self.assertEqual(self.row(first['request_id'])['state'],'answered')

    def test_unclaimed_expiry_is_terminal_but_attempted_expiry_can_reply(self):
        self.enroll();first,_=self.request();claim=self.poll()[1]['claim'];second,_=self.request('Never claimed')
        with self.store.lock,self.store.db:
            for row in self.store._rows():
                row['created']=time.time()-mod.TTL-1;self.store.mods.put(row)
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertEqual(self.row(second['request_id'])['state'],'failed')
        self.assertIn('Nothing was submitted',self.row(second['request_id'])['error'])
        self.assertEqual(self.row(first['request_id'])['state'],'expired')
        self.assertEqual(self.answer(claim)[1]['state'],'answered')

    def test_lease_lapse_requires_reconnect_never_retries_pending(self):
        self.enroll();payload,_=self.request();self.poll()
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        self.assertFalse(self.store.owner_capabilities(self.aid,self.agents)['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.poll()[0],409)
        self.reconnect();self.assertIsNone(self.poll()[1]['claim'])

    def test_repair_revokes_old_token_without_redirecting_old_waiting_request(self):
        self.enroll();payload,_=self.request();oldtoken=self.token
        self.enroll()
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=oldtoken)[0],403)
        self.assertIsNone(self.poll()[1]['claim'])
        self.assertEqual(self.row(payload['request_id'])['state'],'sending')

    def global_setup(self):
        status, pair = self.http('/claude-mod-setup', {}, owner=True)
        self.assertEqual(status, 200, pair)
        self.assertEqual(pair['command'], f'/watch-setup {server.PORT}:'+pair['pair_code'])
        status, grant = self.http('/claude-mod/setup', {'code':pair['pair_code']})
        self.assertEqual(status, 200, grant)
        return pair, grant

    def register_global(self, grant, client=None, seq=0, **overrides):
        body = dict(native_id=self.sid, project=self.project, client_id=client or uid(), connection_seq=seq)
        body.update(overrides)
        status, value = self.http('/claude-mod/register', body, token=grant['grant'])
        if status == 200:
            self.token, self.generation = value['token'], value['generation']
        return status, value, body

    def global_poll(self, client, accept=True):
        return self.http('/claude-mod/poll', dict(generation=self.generation,native_id=self.sid,
            project=self.project,accept=accept,client_id=client),token=self.token)

    def shared_progress_request(self):
        import chat_directory
        import peer_bridge
        root = self.base/'claude'/'fictional'
        root.mkdir(parents=True)
        (root/(self.sid+'.jsonl')).write_text(json.dumps(dict(type='custom-title', customTitle='Fictional',
            sessionId=self.sid, cwd=self.project))+'\n')
        codex = self.base/'codex'
        (codex/'sessions').mkdir(parents=True)
        directory = chat_directory.ChatDirectory(str(codex), str(root.parent), policy={'history_days':'all'},
            codex_sessions=str(codex/'sessions'), live_snapshot=lambda: server.shared_directory_snapshot(self.store))
        self.store.shared.directory = directory
        with self.store.lock:
            service = peer_bridge.initialize_service(self.store.db, {'history_days':'all'})
            credential = peer_bridge.service_credential(self.store.db, service['credential_id'])
        status, setup = self.http('/claude-mod-setup', {'peer_receive':True}, owner=True)
        self.assertEqual(status, 200, setup)
        status, grant = self.http('/claude-mod/setup', {'code':setup['pair_code']})
        self.assertEqual(status, 200, grant)
        status, registration, body = self.register_global(grant, peer_protocol=1, lifecycle_protocol=2)
        self.assertEqual(status, 200, registration)
        for _ in range(30):
            if directory.refresh(force=True)['complete']:
                break
        target = directory.list_agents()['agents'][0]
        sent = self.store.shared.send(credential, target['agent_id'], target['identity_revision'],
                                      'Report fictional progress.', uid(), 'Fictional requester')
        tid = self.store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                                    (sent['request_id'],)).fetchone()[0]
        return tid, sent['request_id'], body['client_id']

    def progress_event(self, claim, client, kind, **extra):
        return self.http('/claude-mod/event', dict(generation=self.generation, native_id=self.sid,
            client_id=client, request_id=claim['request_id'], attempt_id=claim['attempt_id'],
            kind=kind, **extra), token=self.token)

    def test_shared_mod_progress_tracks_claim_start_and_exact_end_only(self):
        tid, request_id, client = self.shared_progress_request()
        observed = time.time() + 60
        with patch.object(self.store, '_peer_progress_clock', lambda: observed, create=True):
            status, poll = self.global_poll(client)
            self.assertEqual(status, 200, poll)
            claim = poll['claim']
            self.assertEqual(claim['request_id'], tid)
            def progress():
                return peer_lifecycle.get(self.store.db, tid)['evidence']['progress']
            reserved = dict(version=1, claim_issued_at=observed, accepted_at=None, started_at=None,
                            last_checked_at=None, ended_at=None)
            self.assertEqual(progress(), reserved)
            observed += 1
            self.assertEqual(self.global_poll(client, accept=False)[0], 200)
            self.assertEqual(self.progress_event(claim, client, 'submitted')[0], 200)
            self.assertEqual(progress(), reserved)
            observed += 1
            self.assertEqual(self.progress_event(dict(claim, attempt_id=uid()), client, 'started', turn_id='fictional-turn')[0], 409)
            self.assertEqual(progress(), reserved)
            self.assertEqual(self.progress_event(claim, client, 'started', turn_id='fictional-turn')[0], 200)
            started = dict(reserved, started_at=observed, last_checked_at=observed)
            self.assertEqual(progress(), started)
            observed += 1
            self.assertEqual(self.progress_event(claim, client, 'started', turn_id='fictional-turn')[0], 200)
            self.assertEqual(progress(), started)
            completed = dict(turn_id='fictional-turn', outcome='answered', answer='WATCH_REPLY '+tid+'\nFictional reply.',
                             event_id=uid(), occurred_at=1)
            self.assertEqual(self.progress_event(claim, client, 'completed', **completed)[0], 200)
            ended = dict(started, last_checked_at=observed, ended_at=observed)
            self.assertEqual(progress(), ended)
            observed += 1
            self.assertEqual(self.progress_event(claim, client, 'completed', **completed)[0], 200)
            self.assertEqual(progress(), ended)
            self.assertEqual(self.store.shared.result(request_id)['reply'], 'Fictional reply.')

    def test_shared_mod_old_replays_and_bad_clock_do_not_invent_progress(self):
        tid, request_id, client = self.shared_progress_request()
        status, poll = self.global_poll(client)
        self.assertEqual(status, 200, poll)
        claim = poll['claim']
        turn = 'fictional-old-turn'
        self.assertEqual(self.progress_event(claim, client, 'started', turn_id=turn)[0], 200)
        with self.store.lock, self.store.db:
            old = peer_lifecycle.get(self.store.db, tid)['evidence']
            old.pop('progress', None)
            self.store.db.execute('UPDATE peer_execution SET evidence=? WHERE request_id=?', (json.dumps(old), tid))
        with patch.object(self.store, '_peer_progress_clock', lambda: time.time()+60, create=True):
            self.assertEqual(self.progress_event(claim, client, 'started', turn_id=turn)[0], 200)
        empty = dict(version=1, claim_issued_at=None, accepted_at=None, started_at=None,
                     last_checked_at=None, ended_at=None)
        self.assertEqual(peer_lifecycle.get(self.store.db, tid)['evidence']['progress'], empty)
        completed = dict(turn_id=turn, outcome='answered', answer='WATCH_REPLY '+tid+'\nFictional reply.',
                         event_id=uid(), occurred_at=1)
        with patch.object(self.store, '_peer_progress_clock', lambda: float('nan'), create=True):
            self.assertEqual(self.progress_event(claim, client, 'completed', **completed)[0], 200)
        self.assertEqual(self.store.shared.result(request_id)['reply'], 'Fictional reply.')
        self.assertEqual(peer_lifecycle.get(self.store.db, tid)['execution'], 'completed')
        with patch.object(self.store, '_peer_progress_clock', lambda: time.time()+60, create=True):
            self.assertEqual(self.progress_event(claim, client, 'completed', **completed)[0], 200)
        self.assertEqual(peer_lifecycle.get(self.store.db, tid)['evidence']['progress'], empty)

    def test_mod_pruned_payload_completion_settles_without_new_progress_or_body(self):
        tid, _, client = self.shared_progress_request()
        observed = time.time() + 60
        with patch.object(self.store, '_peer_progress_clock', lambda: observed, create=True):
            status, poll = self.global_poll(client)
            self.assertEqual(status, 200, poll)
            claim = poll['claim']
            self.assertEqual(self.progress_event(claim, client, 'started', turn_id='fictional-turn')[0], 200)
            previous = peer_lifecycle.get(self.store.db, tid)['evidence']['progress']
            with self.store.lock:
                self.store._prune(self.store._rows(), observed + messaging.RETENTION)
            self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (tid,)).fetchone())
            observed += 1
            completed = dict(turn_id='fictional-turn', outcome='answered', answer='WATCH_REPLY '+tid+'\nFictional late reply.',
                             event_id=uid(), occurred_at=1)
            self.assertEqual(self.progress_event(claim, client, 'completed', **completed)[0], 200)
            settled = peer_lifecycle.get(self.store.db, tid)
            self.assertEqual(settled['execution'], 'completed')
            self.assertEqual(settled['evidence']['progress'], previous)
            self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (tid,)).fetchone())

    def test_global_setup_auth_single_use_status_and_secret_classes(self):
        for path in ('/claude-mod-setup','/claude-mod-setup-status','/claude-mod-revoke'):
            self.assertEqual(self.http(path,{})[0],403)
            self.assertEqual(self.http(path,{'forged':True},owner=True)[0],400)
        pair, grant = self.global_setup()
        self.assertEqual(self.http('/claude-mod/setup',{'code':pair['pair_code']})[0],403)
        status, enrollment, body = self.register_global(grant)
        self.assertEqual(status,200,enrollment)
        for token in (server.KEY,grant['grant']):
            self.assertEqual(self.http('/claude-mod/poll',dict(generation=self.generation,native_id=self.sid,
                project=self.project,accept=True,client_id=body['client_id']),token=token)[0],403)
        self.assertEqual(self.http('/claude-mod/register',body,token=self.token)[0],403)
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=self.token)[0],409)
        self.assertEqual(self.poll()[0],400,'Derived poll requires client membership')
        snapshot=json.dumps(self.store.snapshot())
        for secret in (pair['pair_code'],grant['grant'],self.token):
            self.assertNotIn(secret,snapshot)
        self.assertEqual(self.http('/claude-mod-setup-status',{},owner=True)[1],
            {'version':1,'authorized':True,'connected_chats':1,'peer_receive':False})
        self.assertEqual(self.store.owner_capabilities(self.aid,self.agents)['claude_setup'],
            {'authorized':True,'state':'connected','peer_receive':False})
        self.assertEqual(self.http('/agent-message',dict(agent=self.aid,question='Forbidden',request_id=uid()),token=grant['grant'])[0],403)
        for path, data in (('/claude-mod/setup',{'code':pair['pair_code']}),('/claude-mod/register',body)):
            for headers in ({'Origin':'http://127.0.0.1:'+str(server.PORT)}, {'Host':'localhost:'+str(server.PORT)}):
                self.assertEqual(self.http(path,data,token=grant['grant'] if path.endswith('register') else None,**headers)[0],403)
        stored=json.dumps(list(self.store.db.execute('SELECT * FROM claude_mod_grants'))+
                          list(self.store.db.execute('SELECT * FROM claude_mod_enrollments')))
        self.assertNotIn(grant['grant'],stored)
        self.assertNotIn(self.token,stored)

    def test_global_registration_sequence_retry_host_handover_and_receipts(self):
        _,grant=self.global_setup()
        status,first,body=self.register_global(grant)
        self.assertEqual(status,200,first)
        payload,_=self.request();claim=self.global_poll(body['client_id'])[1]['claim']
        second,_=self.request('A distinct instruction after an ambiguous claim')
        with ThreadPoolExecutor(4) as pool:
            retries=list(pool.map(lambda _:self.http('/claude-mod/register',body,token=grant['grant']),range(8)))
        self.assertTrue(all(status==200 and value==first for status,value in retries))
        self.assertEqual(self.row(payload['request_id'])['state'],'queued')
        before=self.store.mods.authenticate(self.token)['last_seen']
        self.http('/claude-mod/register',body,token=grant['grant'])
        self.assertEqual(self.store.mods.authenticate(self.token)['last_seen'],before,'Lost-response retries do not extend readiness')
        self.assertEqual(self.register_global(grant)[1]['error_code'],'host_conflict')
        status,new,seqbody=self.register_global(grant,body['client_id'],1)
        self.assertEqual(status,200,new)
        self.assertEqual(new['enrollment_id'],first['enrollment_id'])
        self.assertEqual(new['token'],first['token'])
        self.assertNotEqual(new['generation'],first['generation'])
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.http('/claude-mod/register',body,token=grant['grant'])[1]['error_code'],'stale_connection')
        self.assertEqual(self.http('/claude-mod/register',seqbody,token=grant['grant'])[1],new)
        self.store.db.execute('UPDATE claude_mod_enrollments SET last_seen=0');self.store.db.commit()
        status,handoff,other=self.register_global(grant,seq=4)
        self.assertEqual(status,200,handoff)
        self.assertEqual(handoff['enrollment_id'],first['enrollment_id'])
        self.assertEqual(self.global_poll(body['client_id'])[0],409)
        nextclaim=self.global_poll(other['client_id'])[1]['claim']
        self.assertEqual(nextclaim['request_id'],second['request_id'])
        self.assertNotEqual(nextclaim['request_id'],claim['request_id'])
        late=dict(generation=self.generation,native_id=self.sid,client_id=other['client_id'],
            request_id=claim['request_id'],attempt_id=claim['attempt_id'],kind='answered',turn_id=uid(),
            answer='WATCH_REPLY '+claim['request_id']+'\nLate exact completion')
        self.assertEqual(self.http('/claude-mod/event',late,token=self.token)[1]['state'],'answered')

    def test_global_eligibility_drift_stops_claims_preserves_event_recovery(self):
        _,grant=self.global_setup()
        for flags in ({'runtime':'Codex'},{'sub':True},{'parent':'c:'+uid()},{'observed':True},{'cwd':'/fictional/wrong'}):
            server.AGENTS[self.aid]=dict(self.agents[self.aid],**flags)
            status,value,_=self.register_global(grant)
            self.assertEqual((status,value['error_code']),(409,'identity_rejected'))
        server.AGENTS.pop(self.aid)
        self.assertEqual(self.register_global(grant)[1]['error_code'],'not_observed')
        server.AGENTS.update(self.agents)
        _,_,body=self.register_global(grant)
        first,_=self.request('A completion remains recoverable after eligibility changes')
        claim=self.global_poll(body['client_id'])[1]['claim']
        payload,_=self.request()
        server.AGENTS[self.aid]['parent']='c:'+uid()
        self.assertIsNone(self.global_poll(body['client_id'])[1]['claim'])
        self.assertFalse(self.store.owner_capabilities(self.aid,dict(server.AGENTS))['actions']['queue']['available'])
        self.assertEqual(self.row(payload['request_id'])['state'],'sending')
        completed=dict(generation=self.generation,native_id=self.sid,client_id=body['client_id'],kind='answered',
            request_id=first['request_id'],attempt_id=claim['attempt_id'],turn_id=uid(),
            answer='WATCH_REPLY '+first['request_id']+'\nExact completion after eligibility drift')
        self.assertEqual(self.http('/claude-mod/event',completed,token=self.token)[1]['state'],'answered')
        self.assertIsNone(self.global_poll(body['client_id'])[1]['claim'])

    def test_global_revoke_replacement_manual_compatibility_and_no_redirect(self):
        self.enroll();manual_token=self.token
        _,grant=self.global_setup()
        self.assertEqual(self.register_global(grant)[1]['error_code'],'manual_conflict')
        self.assertEqual(self.http('/claude-mod-revoke',{},owner=True)[0],200)
        self.assertEqual(self.http('/claude-mod/connect',dict(native_id=self.sid,project=self.project),token=manual_token)[0],200)
        self.store.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL');self.store.db.commit()
        _,grant=self.global_setup();_,enrollment,body=self.register_global(grant)
        payload,_=self.request();claim=self.global_poll(body['client_id'])[1]['claim']
        queued,_=self.request('Unclaimed on revoke')
        _,newgrant=self.global_setup()
        self.assertEqual(self.register_global(grant)[0],403)
        self.assertEqual(self.global_poll(body['client_id'])[0],403)
        self.assertEqual(self.row(payload['request_id'])['state'],'unknown')
        self.assertEqual(self.row(queued['request_id'])['state'],'failed')
        self.assertEqual(self.register_global(newgrant)[0],200)
        self.assertIsNone(self.global_poll(self.store.mods.authenticate(self.token)['client_id'])[1]['claim'])
        self.assertNotEqual(self.store.mods.authenticate(self.token)['id'],enrollment['enrollment_id'])

    def test_global_setup_expiry_mint_without_revocation_and_lost_response_recovery(self):
        _,grant=self.global_setup();self.register_global(grant)
        _,pending=self.http('/claude-mod-setup',{},owner=True)
        self.assertEqual(self.http('/claude-mod-setup-status',{},owner=True)[1]['connected_chats'],1)
        self.store.db.execute('UPDATE claude_mod_setup_codes SET expires=0');self.store.db.commit()
        self.assertEqual(self.http('/claude-mod/setup',{'code':pending['pair_code']})[0],403)
        _,pending=self.http('/claude-mod-setup',{},owner=True)
        _,lost=self.http('/claude-mod/setup',{'code':pending['pair_code']})
        self.assertEqual(self.http('/claude-mod/setup',{'code':pending['pair_code']})[0],403)
        _,replacement=self.global_setup()
        self.assertEqual(self.register_global(lost)[0],403)
        self.assertEqual(self.register_global(replacement)[0],200)

    def test_additive_legacy_database_migration_preserves_manual_token_and_requests(self):
        _,enrollment=self.enroll();payload,_=self.request()
        old_token=self.token
        self.store.close()
        db=sqlite3.connect(self.base/'state'/'officer-messages.sqlite3')
        legacy=list(db.execute('SELECT id,token_hash,agent,project,generation,last_seen,active FROM claude_mod_enrollments'))
        db.execute('DROP TABLE claude_mod_enrollments')
        db.execute('''CREATE TABLE claude_mod_enrollments (id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,
            agent TEXT NOT NULL,project TEXT NOT NULL,generation TEXT,last_seen REAL NOT NULL,active INTEGER NOT NULL)''')
        db.executemany('INSERT INTO claude_mod_enrollments VALUES (?,?,?,?,?,?,?)',legacy)
        db.commit();db.close()
        self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        entry=self.store.mods.authenticate(old_token)
        self.assertIsNone(entry['grant_id'])
        self.assertEqual(entry['id'],enrollment['enrollment_id'])
        self.reconnect()
        self.assertEqual(self.poll()[1]['claim']['request_id'],payload['request_id'])

    def test_global_two_chats_same_project_restart_and_stable_registration(self):
        _,grant=self.global_setup();_,first,body=self.register_global(grant)
        firstpayload,_=self.request();self.global_poll(body['client_id'])
        waiting,_=self.request('Survive restart unclaimed')
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        status,second=self.http('/claude-mod/register',dict(body,native_id=other,client_id=uid()),token=grant['grant'])
        self.assertEqual(status,200,second)
        self.assertNotEqual(second['token'],first['token'])
        self.assertNotEqual(second['enrollment_id'],first['enrollment_id'])
        self.store.close();self.store=messaging.MessagingStore(str(self.base/'state'),str(self.base/'sessions'))
        self.store.codex_available=False;server.MESSAGING=self.store
        status,restarted=self.http('/claude-mod/register',body,token=grant['grant'])
        self.assertEqual(status,200,restarted)
        self.assertEqual(restarted['enrollment_id'],first['enrollment_id'])
        self.assertNotEqual(restarted['generation'],first['generation'])
        self.token,self.generation=restarted['token'],restarted['generation']
        self.assertEqual(self.row(firstpayload['request_id'])['state'],'unknown')
        self.assertEqual(self.global_poll(body['client_id'])[1]['claim']['request_id'],waiting['request_id'])

    def test_global_concurrent_membership_and_bounded_strict_identity(self):
        _,grant=self.global_setup()
        bodies=[dict(native_id=self.sid,project=self.project,client_id=uid(),connection_seq=0) for _ in range(8)]
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda body:self.http('/claude-mod/register',body,token=grant['grant']),bodies))
        self.assertEqual(sum(status==200 for status,_ in results),1)
        self.assertTrue(all(status==200 or status==409 and value['error_code']=='host_conflict' for status,value in results))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0],1)
        for seq in (-1,True,1.5,9007199254740992):
            self.assertEqual(self.http('/claude-mod/register',dict(bodies[0],connection_seq=seq),token=grant['grant'])[0],400)
        other=uid();server.AGENTS['c:'+other]=dict(self.agents[self.aid])
        with patch.object(mod,'MAX_ENROLLMENTS',1):
            self.assertEqual(self.http('/claude-mod/register',dict(bodies[0],native_id=other),token=grant['grant'])[0],429)


    def test_registration_missing_project_remains_retryable_same_episode(self):
        _,grant=self.global_setup()
        server.AGENTS[self.aid].pop('cwd')
        status,value,body=self.register_global(grant)
        self.assertEqual((status,value['error_code']),(409,'not_observed'))
        server.AGENTS[self.aid]['cwd']=self.project
        status,value=self.http('/claude-mod/register',body,token=grant['grant'])
        self.assertEqual(status,200,value)
        self.assertEqual(value['client_id'],body['client_id'])

    def test_owner_invalid_target_keeps_existing_validation_and_conflict_order(self):
        status,value=self.http('/agent-message',dict(agent={},question='Fictional question',request_id=uid()),owner=True)
        self.assertEqual(status,400,value)
        self.enroll();payload,_=self.request()
        status,value=self.http('/agent-message',dict(payload,agent={}),owner=True)
        self.assertEqual(status,409,value)


if __name__=='__main__':unittest.main()
