#!/usr/bin/env python3
"""Private fixture inbox and real authenticated loopback HTTP, with no native agents."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
import officer_messaging
import peer_bridge as bridge
import peer_inbox as inbox
import setup_antigravity_inbox as setup


def uid():
    return str(uuid.uuid4())


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        bridge.handle_shared_http(self, self.server.fixture.store.shared)

    def send(self, status, raw, content_type, extra=()):
        if self.path == '/bridge/v2/rpc' and self.server.fixture.drop_rpc.is_set():
            self.server.fixture.drop_rpc.clear()
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)


@unittest.skipUnless(os.name == 'posix', 'POSIX private state')
class InboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.path = self.state / inbox.CONFIG_NAME
        self.config = {'version': 1, 'inbox_id': uid(), 'label': 'Antigravity inbox',
                       'receiver_key': secrets.token_hex(32)}
        self.set_config(self.config)
        self.store = None
        self.restart()
        self.addCleanup(self.close_store)
        self.credential = bridge.initialize_service(self.store.db, {'providers': ['codex', 'claude']})
        self.drop_rpc = threading.Event()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.fixture = self
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)

    def close_store(self):
        if self.store is not None:
            self.store.close()

    def close_http(self):
        self.http.shutdown()
        self.thread.join(3)
        self.http.server_close()

    def restart(self):
        self.close_store()
        self.store = officer_messaging.MessagingStore(str(self.state), str(self.sessions))
        self.store.shared.directory_factory = Mock(side_effect=AssertionError('Inbox initialized native discovery'))
        self.store.shared._preflight = Mock(side_effect=AssertionError('Inbox preflighted native transport'))

    def set_config(self, config):
        self.path.write_text(json.dumps(config), encoding='utf-8')
        self.path.chmod(0o600)

    def mailbox(self, config=None):
        config = config or self.config
        return {'inbox_id': config['inbox_id'], 'inbox_revision': inbox.revision(config)}

    def proof(self, method, params, ident, run, nonce, config):
        raw = json.dumps(params, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        input_bytes = '\n'.join(('watch-inbox-receiver-v1', ident, run, nonce, params['inbox_id'],
                                 params['inbox_revision'], method, hashlib.sha256(raw).hexdigest())).encode()
        return hmac.new(bytes.fromhex(config['receiver_key']), input_bytes, hashlib.sha256).hexdigest()

    def receiver_mac(self, config, *parts):
        message = '\n'.join(map(str, parts)).encode('utf-8')
        return hmac.new(bytes.fromhex(config['receiver_key']), message, hashlib.sha256).hexdigest()

    def rpc(self, method, args=None, *, recv=None, credential=None, proof_context=None, raw=None,
            receiver_challenge=True, challenge_fields=None, change_nonce=False):
        credential = credential or self.credential
        ident, key = credential['credential_id'], credential['secret']
        port = self.http.server_port
        nonce = secrets.token_hex(32)
        scope = self.mailbox(recv) if recv is not None and receiver_challenge else {}
        arguments = dict(args or {})
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            challenge_args = dict(credential_id=ident, nonce=nonce, **scope)
            challenge_args.update(challenge_fields or {})
            conn.request('POST', '/bridge/v2/challenge', json.dumps(challenge_args))
            response = conn.getresponse()
            challenge = json.loads(response.read())
            if response.status != 200:
                return response.status, challenge
            run = challenge['run_id']
            self.assertEqual(challenge['proof'], bridge.mac(key, 'watch-bridge-server-v2', port, ident, nonce, run))
            if scope:
                self.assertEqual(set(challenge), {'run_id', 'proof', 'inbox_proof'})
                self.assertEqual(challenge['inbox_proof'], self.receiver_mac(recv, 'watch-inbox-server-v1',
                                 port, ident, nonce, run, scope['inbox_id'], scope['inbox_revision']))
            else:
                self.assertEqual(set(challenge), {'run_id', 'proof'})
            if change_nonce:
                nonce = secrets.token_hex(32)
            if recv is not None:
                arguments.update(self.mailbox(recv))
                proof_parts = dict(ident=ident, run=run, nonce=nonce)
                proof_parts.update(proof_context or {})
                arguments['receiver_proof'] = self.proof(method, arguments, **proof_parts, config=recv)
            body = raw or json.dumps({'method': method, 'params': arguments}, ensure_ascii=True).encode()
            stamp = str(int(time.time()))
            signature = bridge.mac(key, 'watch-bridge-request-v2', port, 'POST', '/bridge/v2/rpc', stamp, nonce, run,
                                   bridge.digest(body))
            conn.request('POST', '/bridge/v2/rpc', body, headers={'X-Watch-Bridge': ident, 'X-Watch-Time': stamp,
                         'X-Watch-Nonce': nonce, 'X-Watch-Run': run, 'X-Watch-Signature': signature})
            response = conn.getresponse()
            data = response.read()
            self.assertEqual(response.getheader('X-Watch-Signature'), bridge.mac(key, 'watch-bridge-response-v2', port,
                             run, nonce, response.status, bridge.digest(data)))
            if scope and not change_nonce:
                expected = self.receiver_mac(recv, 'watch-inbox-response-v1', port, ident, run, nonce, scope['inbox_id'],
                                             scope['inbox_revision'], response.status, hashlib.sha256(data).hexdigest())
                self.assertEqual(response.getheader('X-Watch-Inbox-Signature'), expected)
            else:
                self.assertIsNone(response.getheader('X-Watch-Inbox-Signature'))
            return response.status, json.loads(data)
        finally:
            conn.close()

    def result(self, method, args=None, **kwargs):
        status, body = self.rpc(method, args, **kwargs)
        self.assertEqual(status, 200, body)
        return body['result']

    def send_args(self, request_id=None, text='Fixture question? 🧪'):
        return dict(self.mailbox(), request_id=request_id or uid(), text=text, declared_sender='Example sender')

    def claim_args(self, request_id, token=None):
        return {'request_id': request_id, 'claim_token': token or secrets.token_hex(32)}

    def test_round_trip_no_native_paths_or_token_leaks(self):
        self.assertEqual(self.result('list_inboxes'), {'inboxes': [dict(self.mailbox(), label='Antigravity inbox',
                         availability='shared pull inbox configured')]})
        args = self.send_args()
        sent = self.result('send_inbox_message', args)
        self.assertEqual((sent['state'], sent['duplicate']), ('queued', False))
        self.assertEqual(sent['expires_at'] - sent['created_at'], 86400000)
        self.assertEqual(self.result('send_inbox_message', args), dict(sent, duplicate=True))
        self.assertEqual(self.rpc('send_inbox_message', dict(args, text='changed'))[0], 409)
        listing = self.result('check_inbox', recv=self.config)
        self.assertEqual(listing['requests'][0]['text_preview'], args['text'])
        self.assertNotIn('text', listing['requests'][0])
        claim = self.claim_args(args['request_id'])
        first = self.result('claim_inbox_message', claim, recv=self.config)
        self.assertEqual((first['text'], first['state']), (args['text'], 'claimed'))
        self.assertEqual(self.result('claim_inbox_message', claim, recv=self.config), first)
        self.assertEqual(self.rpc('claim_inbox_message', self.claim_args(args['request_id']), recv=self.config)[0], 409)
        answer = dict(claim, answer='Answer ✓')
        replied = self.result('answer_inbox_message', answer, recv=self.config)
        token_hash = bridge.digest(bytes.fromhex(claim['claim_token']))
        self.assertEqual((replied['state'], replied['duplicate']), ('replied', False))
        self.assertEqual(self.result('answer_inbox_message', answer, recv=self.config), dict(replied, duplicate=True))
        self.assertEqual(self.rpc('answer_inbox_message', dict(answer, answer='different'), recv=self.config)[0], 409)
        recovered = self.result('claim_inbox_message', claim, recv=self.config)
        self.assertEqual(recovered, {key: value for key, value in dict(first, state='replied').items() if key != 'text'})
        self.assertEqual(self.rpc('claim_inbox_message', self.claim_args(args['request_id']), recv=self.config)[0], 409)
        retrieved = self.result('get_inbox_reply', {'request_id': args['request_id']})
        self.assertEqual((retrieved['state'], retrieved['answer']), ('replied', 'Answer ✓'))
        requests = self.result('list_inbox_requests')['requests']
        self.assertNotIn('answer', requests[0])
        self.assertEqual(self.result('check_inbox', recv=self.config)['requests'], [])
        serialized = json.dumps([first, retrieved, listing, requests, replied, recovered])
        self.assertNotIn(claim['claim_token'], serialized)
        self.assertNotIn(token_hash, serialized)
        self.assertNotIn(self.config['receiver_key'], serialized)
        self.assertEqual(self.store.db.execute('SELECT claim_hash FROM peer_inbox_requests').fetchone()[0], token_hash)
        for table in ('requests', 'peer_execution', 'shared_bridge_requests'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.store.shared.directory_factory.assert_not_called()
        self.store.shared._preflight.assert_not_called()

    def test_auth_binds_proof_and_rejects_duplicate_json(self):
        with bridge.transaction(self.store.db, self.store.lock):
            other = bridge.issue_service_credential(self.store.db)
        args = self.mailbox()
        self.assertEqual(self.rpc('check_inbox', args)[0], 400)
        self.assertEqual(self.rpc('check_inbox', dict(args, receiver_proof=secrets.token_hex(32)))[0], 403)
        for context in ({'ident': other['credential_id']}, {'run': uid()}, {'nonce': secrets.token_hex(32)}):
            with self.subTest(context=list(context)):
                self.assertEqual(self.rpc('check_inbox', recv=self.config, proof_context=context)[0], 403)
        self.assertEqual(self.result('check_inbox', recv=self.config, credential=other)['requests'], [])
        self.assertEqual(self.rpc('list_inboxes', raw=b'{"method":"list_agents","method":"list_inboxes","params":{}}')[0], 400)
        self.assertEqual(self.rpc('check_inbox', raw=b'{"method":"check_inbox","params":{"cursor":"x","cursor":"y"}}')[0], 400)
        self.assertEqual(self.rpc('check_inbox', recv=self.config,
                                  raw=b'{"method":"check_inbox","params":{"cursor":"x","cursor":"y"}}')[0], 400)
        self.store.db.execute('UPDATE shared_bridge_credentials SET active=0 WHERE id=?', (other['credential_id'],))
        self.store.db.commit()
        self.assertEqual(self.rpc('check_inbox', recv=self.config, credential=other)[0], 403)

    def test_receiver_challenge_scope_and_nonce_required_before_claim(self):
        sent = self.result('send_inbox_message', self.send_args())
        args = self.claim_args(sent['request_id'])
        self.assertEqual(self.rpc('claim_inbox_message', args, recv=self.config, receiver_challenge=False)[0], 403)
        self.assertEqual(self.rpc('claim_inbox_message', args, recv=self.config, change_nonce=True)[0], 403)
        self.assertEqual(self.result('get_inbox_reply', {'request_id': sent['request_id']})['state'], 'queued')
        self.assertEqual(self.result('claim_inbox_message', args, recv=self.config)['state'], 'claimed')
        self.assertEqual(self.rpc('claim_inbox_message', self.claim_args(sent['request_id']), recv=self.config)[0], 409)

    def test_receiver_challenge_authenticates_exact_context_and_legacy_four_stay_unscoped(self):
        scope = self.mailbox()
        ident, nonce, port = self.credential['credential_id'], secrets.token_hex(32), self.http.server_port
        result = self.store.shared.challenge(dict(scope, credential_id=ident, nonce=nonce), port)
        parts = [port, ident, nonce, result['run_id'], scope['inbox_id'], scope['inbox_revision']]
        self.assertEqual(result['inbox_proof'], self.receiver_mac(self.config, 'watch-inbox-server-v1', *parts))
        for index, altered in enumerate((port + 1, uid(), secrets.token_hex(32), uid(), uid(), 'c' * 64)):
            different = parts[:index] + [altered] + parts[index + 1:]
            self.assertNotEqual(result['inbox_proof'], self.receiver_mac(self.config, 'watch-inbox-server-v1', *different))
        for fields in ({'inbox_id': scope['inbox_id']}, {'inbox_revision': scope['inbox_revision']},
                       dict(scope, inbox_id=uid()), dict(scope, inbox_revision='a' * 64)):
            with self.subTest(fields=list(fields)):
                self.assertEqual(self.rpc('check_inbox', challenge_fields=fields)[0], 403)
        self.store.shared.directory = Mock()
        self.store.shared.directory.list_agents.return_value = {'agents': [], 'next_cursor': None}
        self.assertEqual(self.result('list_agents'), {'agents': [], 'next_cursor': None})
        self.assertEqual(self.rpc('send_message')[0], 400)
        self.assertEqual(self.rpc('get_reply', {'request_id': uid()})[0], 404)
        self.assertEqual(self.result('list_requests'), {'requests': [], 'next_cursor': None})

    def test_malformed_encoding_and_deep_config_disable_only_inbox(self):
        config_text = json.dumps(self.config)
        cases = (config_text.encode('utf-16'), config_text.encode('utf-32'), b'[' * 2047 + b'0' + b']' * 2047)
        for raw in cases:
            with self.subTest(size=len(raw)):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    inbox.read_config_file(self.path)
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(setup.main(['status', '--state-dir', str(self.state)]), 1)
                self.assertEqual(json.loads(output.getvalue())['state'], 'unavailable')
                self.restart()
                self.assertEqual(self.result('list_inboxes'), {'inboxes': []})
                self.assertEqual(self.rpc('check_inbox', recv=self.config)[0], 403)
                self.store.shared.directory_factory = None
                self.assertEqual(self.result('list_requests'), {'requests': [], 'next_cursor': None})
                self.assertEqual(self.path.read_bytes(), raw)
        self.set_config(self.config)
        self.restart()
        self.assertEqual(self.result('check_inbox', recv=self.config)['requests'], [])

    def test_claim_race_and_lost_ack_recovery_after_restart(self):
        args = self.send_args(text='An unclaimed fixture body')
        self.result('send_inbox_message', args)
        tokens = [self.claim_args(args['request_id']) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda claim: self.rpc('claim_inbox_message', claim, recv=self.config), tokens))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(index for index, (status, _) in enumerate(results) if status == 200)
        self.assertEqual(self.rpc('answer_inbox_message', dict(tokens[1 - winner], answer='forged'), recv=self.config)[0], 409)
        lost = self.send_args(text='Separate committed body')
        self.result('send_inbox_message', lost)
        claim = self.claim_args(lost['request_id'])
        self.drop_rpc.set()
        with self.assertRaises(http.client.RemoteDisconnected):
            self.rpc('claim_inbox_message', claim, recv=self.config)
        self.restart()
        page = self.result('check_inbox', recv=self.config)
        self.assertEqual({entry['state'] for entry in page['requests']}, {'claimed'})
        new_claim = self.claim_args(lost['request_id'])
        self.assertEqual(self.rpc('claim_inbox_message', new_claim, recv=self.config)[0], 409)
        self.assertEqual(self.rpc('answer_inbox_message', dict(new_claim, answer='foreign'), recv=self.config)[0], 409)
        self.assertEqual(self.result('claim_inbox_message', claim, recv=self.config)['text'], lost['text'])

    def test_removed_replaced_and_restored_revision(self):
        queued, delivered = self.send_args(), self.send_args()
        for args in (queued, delivered):
            self.result('send_inbox_message', args)
        pending = self.claim_args(queued['request_id'])
        already_claimed = self.claim_args(delivered['request_id'])
        self.result('claim_inbox_message', already_claimed, recv=self.config)
        self.path.unlink()
        self.restart()
        self.assertEqual(self.result('list_inboxes'), {'inboxes': []})
        self.assertEqual(self.result('get_inbox_reply', {'request_id': queued['request_id']})['state'], 'queued')
        self.assertEqual(self.rpc('check_inbox', recv=self.config)[0], 403)
        self.assertEqual(self.rpc('send_inbox_message', queued)[0], 409)
        replacement = dict(self.config, receiver_key=secrets.token_hex(32))
        self.set_config(replacement)
        self.restart()
        self.assertNotEqual(self.result('list_inboxes')['inboxes'][0]['inbox_revision'], self.mailbox()['inbox_revision'])
        self.assertEqual(self.result('check_inbox', recv=replacement)['requests'], [])
        self.assertEqual(self.rpc('claim_inbox_message', pending, recv=self.config)[0], 403)
        self.assertEqual(self.rpc('claim_inbox_message', pending, recv=replacement)[0], 409)
        self.assertEqual(self.rpc('answer_inbox_message', dict(already_claimed, answer='foreign'), recv=replacement)[0], 409)
        self.set_config(self.config)
        self.restart()
        self.assertEqual(self.result('claim_inbox_message', pending, recv=self.config)['state'], 'claimed')
        self.assertEqual(self.result('answer_inbox_message', dict(already_claimed, answer='original'), recv=self.config)['state'], 'replied')

    def test_exact_expiration_startup_and_idle_deletion(self):
        now = inbox.now_ms()
        with patch.object(inbox, 'now_ms', return_value=now):
            sent = self.result('send_inbox_message', self.send_args(text='Expiring private message'))
        with patch.object(inbox, 'now_ms', return_value=now + 10):
            second = self.result('send_inbox_message', self.send_args(text='Another expiring message'))
        claim = self.claim_args(sent['request_id'])
        self.result('claim_inbox_message', claim, recv=self.config)
        with patch.object(inbox, 'now_ms', return_value=sent['expires_at'] - 1):
            self.result('answer_inbox_message', dict(claim, answer='Expiring reply'), recv=self.config)
        with patch.object(inbox, 'now_ms', return_value=sent['expires_at']):
            hidden = self.result('get_inbox_reply', {'request_id': sent['request_id']})
            self.assertEqual((hidden['state'], hidden['text_preview'], hidden['declared_sender'], hidden['answer']),
                             ('expired', '', None, None))
            self.assertEqual(self.rpc('claim_inbox_message', claim, recv=self.config)[0], 410)
            self.assertEqual(self.rpc('answer_inbox_message', dict(claim, answer='Expiring reply'), recv=self.config)[0], 410)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_inbox_payloads').fetchone()[0], 2)
            self.store.tick()
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_inbox_payloads').fetchone()[0], 1)
        with patch.object(inbox, 'now_ms', return_value=second['expires_at']):
            self.restart()
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_inbox_payloads').fetchone()[0], 0)
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_inbox_requests WHERE state=\'expired\'').fetchone()[0], 2)
            self.assertNotIn('Expiring', json.dumps(self.result('list_inbox_requests')))

    def test_startup_securely_deletes_preexisting_expired_payload_bytes(self):
        # Reproduce an existing database populated by an older connection.
        self.store.db.execute('PRAGMA secure_delete = OFF')
        self.assertEqual(self.store.db.execute('PRAGMA secure_delete').fetchone(), (0,))
        path = self.state / 'officer-messages.sqlite3'
        body = 'Fixture body to erase ' + uid()
        answer = 'Fixture answer to erase ' + uid()
        sent = self.result('send_inbox_message', self.send_args(text=body))
        claim = self.claim_args(sent['request_id'])
        self.result('claim_inbox_message', claim, recv=self.config)
        self.result('answer_inbox_message', dict(claim, answer=answer), recv=self.config)
        before = path.read_bytes()
        self.assertIn(body.encode(), before)
        self.assertIn(answer.encode(), before)
        with patch.object(inbox, 'now_ms', return_value=sent['expires_at']):
            self.restart()
        self.assertEqual(self.store.db.execute('PRAGMA secure_delete').fetchone(), (1,))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM peer_inbox_payloads').fetchone(), (0,))
        after = path.read_bytes()
        self.assertNotIn(body.encode(), after)
        self.assertNotIn(answer.encode(), after)
        self.assertEqual(self.store.db.execute('SELECT state FROM peer_inbox_requests').fetchone(), ('expired',))

    def test_encoded_limits_reject_before_mutation_and_page_by_bytes(self):
        self.assertEqual(self.rpc('send_inbox_message', self.send_args(text='\ufeff'))[0], 400)
        too_large = self.send_args(text='body' + '\x01' * 10000)
        self.assertEqual(self.rpc('send_inbox_message', too_large)[0], 400)
        self.assertEqual(self.rpc('get_inbox_reply', {'request_id': too_large['request_id']})[0], 404)
        acceptable = self.send_args(text='body' + '\x01' * 9700)
        sent = self.result('send_inbox_message', acceptable)
        claim = self.claim_args(sent['request_id'])
        self.assertEqual(self.result('claim_inbox_message', claim, recv=self.config)['text'], acceptable['text'])
        self.assertEqual(self.rpc('answer_inbox_message', dict(claim, answer='reply' + '\x01' * 10000), recv=self.config)[0], 400)
        self.assertEqual(self.result('get_inbox_reply', {'request_id': sent['request_id']})['state'], 'claimed')
        answer = 'reply' + '\x01' * 9700
        self.result('answer_inbox_message', dict(claim, answer=answer), recv=self.config)
        self.assertEqual(self.result('get_inbox_reply', {'request_id': sent['request_id']})['answer'], answer)
        self.seed_inboxes(69, state='replied', preview='\x01' * 160, sender='🧪' * 120)
        first = self.result('list_inbox_requests')
        self.assertLess(len(first['requests']), 50)
        self.assertTrue(first['next_cursor'])
        self.assertTrue(inbox.fits(first))
        page = first
        ids = [item['request_id'] for item in first['requests']]
        for _ in range(3):
            if page['next_cursor'] is None:
                break
            page = self.result('list_inbox_requests', {'cursor': page['next_cursor']})
            self.assertTrue(inbox.fits(page))
            ids.extend(item['request_id'] for item in page['requests'])
        self.assertEqual(len(ids), 70)
        self.assertEqual(len(set(ids)), 70)
        self.assertIsNone(page['next_cursor'])
        self.assertEqual(self.rpc('list_inbox_requests', {'cursor': uid()})[0], 400)

    def seed_inboxes(self, count, *, state='queued', preview='Fixture preview', sender=None, created=None):
        created = inbox.now_ms() if created is None else created
        mailbox = self.mailbox()
        with bridge.transaction(self.store.db, self.store.lock):
            for _ in range(count):
                request_id, expires = uid(), created + inbox.RETENTION_MS
                self.store.db.execute('INSERT INTO peer_inbox_requests VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL)',
                                      (request_id, mailbox['inbox_id'], mailbox['inbox_revision'], 'a' * 64,
                                       self.credential['credential_id'], created, expires, state))
                self.store.db.execute('INSERT INTO peer_inbox_payloads VALUES (?,?,?,?,?,NULL)',
                                      (request_id, expires, 'Fixture body', preview, sender))

    def seed_native(self, count):
        ids = [uid() for _ in range(count)]
        with bridge.transaction(self.store.db, self.store.lock):
            for request_id in ids:
                self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,NULL,?)',
                                      (request_id, 'x:' + uid(), '{}', 'a' * 64, uid(), self.credential['credential_id'],
                                       time.time(), 'Fixture native preview'))
        return ids

    def test_combined_quotas_native_uuid_exclusion_and_unchanged_native_read(self):
        self.store.shared.directory_factory = None
        legacy_request = self.seed_native(99)[0]
        self.assertEqual(self.rpc('send_inbox_message', self.send_args(legacy_request))[0], 409)
        inbox_args = self.send_args()
        self.result('send_inbox_message', inbox_args)
        self.assertEqual(self.rpc('send_inbox_message', self.send_args())[0], 429)
        native_args = dict(agent_id='x:' + uid(), identity_revision='b' * 64, text='Fixture native body', request_id=inbox_args['request_id'])
        self.assertEqual(self.rpc('send_message', native_args)[0], 409)
        self.store.shared._resolve = Mock(return_value=dict(native_args, identity={}, project='/example/project',
                                             provider='codex', name='Fixture chat', project_id='a' * 64, project_label='Fixture'))
        self.store.shared._preflight = Mock(return_value={'runtime': 'codex', 'transport': 'codex_native'})
        with patch.object(bridge.peer_lifecycle, 'capacity_error', return_value=None):
            self.assertEqual(self.rpc('send_message', dict(native_args, request_id=uid()))[0], 429)
        self.assertEqual(self.rpc('get_reply', {'request_id': inbox_args['request_id']})[0], 404)
        self.assertEqual(self.rpc('get_inbox_reply', {'request_id': legacy_request})[0], 404)
        self.assertFalse(any(item['request_id'] == inbox_args['request_id'] for item in self.result('list_requests')['requests']))

    def test_native_reservation_rechecks_uuid_after_inbox_admits_it(self):
        self.store.shared.directory_factory = None
        args = self.send_args()
        native_args = dict(agent_id='x:' + uid(), identity_revision='b' * 64, text='Fixture native body', request_id=args['request_id'])
        self.store.shared._resolve = Mock(return_value=dict(native_args, identity={}, project='/example/project',
                                             provider='codex', name='Fixture chat', project_id='a' * 64, project_label='Fixture'))
        waiting, admitted = threading.Event(), threading.Event()

        def allow_preflight(_):
            waiting.set()
            self.assertTrue(admitted.wait(5))
            return {'runtime': 'codex', 'transport': 'codex_native'}

        self.store.shared._preflight = allow_preflight
        with ThreadPoolExecutor(max_workers=1) as pool:
            native = pool.submit(self.rpc, 'send_message', native_args)
            try:
                self.assertTrue(waiting.wait(5))
                self.result('send_inbox_message', args)
            finally:
                admitted.set()
            self.assertEqual(native.result(timeout=5)[0], 409)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0], 0)

    def test_open_hourly_and_tombstone_limits(self):
        self.seed_inboxes(32, created=inbox.now_ms() - 2 * inbox.HOUR_MS)
        status, body = self.rpc('send_inbox_message', self.send_args())
        self.assertEqual(status, 429)
        self.assertIn('unexpired open', body['error'])
        self.store.db.execute("UPDATE peer_inbox_requests SET state='replied'")
        self.store.db.commit()
        self.seed_inboxes(10, state='replied')
        status, body = self.rpc('send_inbox_message', self.send_args())
        self.assertEqual(status, 429)
        self.assertIn('rate limit', body['error'])
        with patch.object(inbox, 'MAX_TOMBSTONES', 42):
            status, body = self.rpc('send_inbox_message', self.send_args())
            self.assertEqual(status, 429)
            self.assertIn('history is full', body['error'])


@unittest.skipUnless(setup.SUPPORTED, 'macOS and Linux private state')
class SetupTests(unittest.TestCase):
    def test_setup_preserves_identity_and_key_without_database_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            path = state / inbox.CONFIG_NAME

            def run(action):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = setup.main([action, '--state-dir', str(state)])
                return status, json.loads(output.getvalue())

            self.assertEqual(run('status'), (0, {'state': 'not_configured'}))
            status, registered = run('init')
            self.assertEqual(status, 0)
            self.assertEqual(set(registered), {'state', 'inbox_id', 'inbox_revision', 'label'})
            original = path.read_bytes()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(run('init'), (0, registered))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(state.iterdir()), [path])
            outside = state / 'unrelated fixture'
            path.rename(outside)
            path.symlink_to(outside)
            self.assertEqual(run('init')[0], 1)
            self.assertTrue(path.is_symlink())
            self.assertEqual(outside.read_bytes(), original)
            path.unlink()
            outside.rename(path)
            os.link(path, outside)
            self.assertEqual(run('status')[0], 1)
            self.assertEqual(outside.read_bytes(), original)

    def test_setup_reports_unsupported_exclusive_rename_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            with patch.object(setup, 'rename_exclusive', side_effect=setup.InstallError('Exclusive rename unavailable.')):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(setup.main(['init', '--state-dir', str(state)]), 1)
            self.assertEqual(json.loads(output.getvalue()), {'state': 'unavailable', 'message': 'Exclusive rename unavailable.'})
            self.assertEqual(list(state.iterdir()), [])


class PlatformTests(unittest.TestCase):
    def test_non_posix_inbox_is_disabled_without_io_and_native_list_still_dispatches(self):
        with closing(sqlite3.connect(':memory:')) as db, db:
            bridge.shared_schema(db)
            credential = bridge.issue_service_credential(db)
            store = SimpleNamespace(db=db, lock=threading.RLock(), state_dir='/example/state')
            directory = Mock()
            native = {'agents': [], 'next_cursor': None}
            directory.list_agents.return_value = native
            with patch.object(inbox.os, 'name', 'nt'), patch.object(inbox.claude_channel, '_directory',
                    side_effect=AttributeError('POSIX directory flags unavailable')) as open_dir:
                shared = bridge.SharedBridge(store, directory)
                self.assertIsNone(shared.inbox.config)
                with self.assertRaisesRegex(OSError, 'requires a POSIX system'):
                    inbox.read_config_file('/example/state/antigravity-inbox.json')
                result = shared.rpc(credential['credential_id'], {'method': 'list_agents', 'params': {}})
                self.assertIs(result, native)
                directory.list_agents.assert_called_once_with()
                open_dir.assert_not_called()


if __name__ == '__main__':
    unittest.main()
