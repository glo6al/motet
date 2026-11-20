"""Owner CLI tests use fictional private keys and disposable loopback servers only."""
import contextlib
import http.server
from loopback_http_fixture import LoopbackHTTPServer
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import tempfile
import sys
import time
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('watch_bridge_clear', Path(__file__).resolve().parents[1] / 'scripts/watch_bridge_clear.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
TID = '11111111-1111-4111-8111-111111111111'
PUBLIC = '6fa01ff3-20e4-47c5-91fd-6d77a48c70d7'
AGENT = 'x:22222222-2222-4222-8222-222222222222'
KEY = 'fictional-owner-test-key'


@unittest.skipIf(os.name != 'posix', 'Private owner key on POSIX host')
class ClearTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.key = self.state / 'key'; self.key.write_text(KEY+'\n'); self.key.chmod(0o600)
        self.calls = []
        self.mode = 'normal'
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.calls.append((self.path, data, self.headers.get('X-Live-Room-Key')))
                if outer.mode == 'lost':
                    self.close_connection = True
                    return
                if outer.mode == 'redirect':
                    self.send_response(302); self.send_header('Location', '/credential-leak')
                    self.send_header('Content-Length', '0'); self.end_headers(); return
                value = ({'transport_id': TID, 'agent': AGENT, 'execution': 'uncertain', 'capture': 'withheld',
                          'retained_payload': True, 'eligible': True}
                         if self.path.endswith('inspect') else
                         {'transport_id': TID, 'agent': AGENT, 'status': 'retired', 'provider_outcome': 'unknown'})
                if 'request_id' in data and outer.mode != 'missing_public':
                    value['request_id'] = TID if outer.mode == 'wrong_public' else PUBLIC
                if outer.mode == 'wrong_identity': value['transport_id'] = AGENT[2:]
                if outer.mode == 'bad_transport': value['transport_id'] = 'invalid'
                if outer.mode == 'secret_extra': value.update(error=KEY, preview=KEY, secret=KEY)
                if outer.mode == 'large': value['extra'] = 'x' * (cli.MAX_RESPONSE+1)
                if outer.mode == 'wrong_outcome': value['provider_outcome'] = 'success'
                raw = (b'{"nested":'+b'['*2000+b'0'+b']'*2000+b'}') if outer.mode == 'nested' else json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers()
                self.wfile.write(raw)

        self.server = LoopbackHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def run_cli(self, *extra, selector=('--transport-id', TID)):
        out, err = io.StringIO(), io.StringIO()
        args = ['--port', str(self.port), '--state-dir', str(self.state), *selector, *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.dict(os.environ, {'http_proxy':'http://127.0.0.1:1', 'HTTP_PROXY':'http://127.0.0.1:1'}):
            result = cli.main(args)
        self.assertNotIn(KEY, out.getvalue()+err.getvalue())
        self.assertNotIn(str(self.root), out.getvalue()+err.getvalue())
        return result, out.getvalue(), err.getvalue()

    def test_default_inspect_and_explicit_apply_have_exact_single_requests(self):
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.calls, [('/bridge-admin/v1/inspect', {'transport_id':TID}, KEY)])
        result, out, err = self.run_cli('--apply', '--expected-agent', AGENT)
        self.assertEqual(result, 0); self.assertEqual(err, '')
        self.assertEqual(self.calls[-1], ('/bridge-admin/v1/retire', {'transport_id':TID, 'agent':AGENT, 'confirm':cli.CONFIRM}, KEY))
        self.assertEqual(len(self.calls), 2)
        value = json.loads(out); self.assertEqual(value['provider_outcome'], 'unknown')
        self.assertIn('Nothing was cancelled or resent', value['explanation'])
        self.assertEqual(list(self.state.iterdir()), [self.key])  # No database/offline fallback.

    def use_real_owner_server(self, server):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        class OwnerHandler(server.H):
            def log_message(self, *args):
                pass
        self.server = LoopbackHTTPServer(('127.0.0.1', 0), OwnerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def test_cli_reaches_real_owner_route_and_retires_only_the_exact_attempt(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        import server
        from officer_messaging import MessagingStore
        import peer_lifecycle as life
        self.use_real_owner_server(server)
        store = MessagingStore(str(self.state), str(self.root / 'sessions'))
        self.addCleanup(store.close)
        db = store.db
        now = time.time()
        db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                   ('33333333-3333-4333-8333-333333333333', AGENT, '{}', 'fictional-hash', TID,
                    'fictional-credential', now, now, 'Fictional'))
        row = dict(request_id=TID, agent=AGENT, created=now, launched=True, state='unknown',
                   origin='peer', runtime='codex', bridge_version=2, bound_turn='fictional-turn',
                   capture_disposition='withheld', reply='', codex_gap_needs_envelope=True)
        db.execute('INSERT INTO requests VALUES (?,?)', (TID,json.dumps(row)))
        life.reconcile(db,row);db.commit()
        with patch.multiple(server, MESSAGING=store, KEY=KEY, PORT=self.port):
            result,out,_ = self.run_cli()
            self.assertEqual(result,0);self.assertTrue(json.loads(out)['eligible'])
            self.assertEqual(life.get(db,TID)['execution'],'uncertain')
            with patch.object(server, 'KEY', 'different-fictional-owner-key'):
                self.assertEqual(self.run_cli('--apply','--expected-agent',AGENT)[0],1)
            self.assertEqual(self.run_cli('--apply','--expected-agent','c:'+AGENT[2:])[0],1)
            self.assertEqual(life.get(db,TID)['execution'],'uncertain')
            self.assertEqual(self.run_cli('--apply','--expected-agent',AGENT)[0],0)
            self.assertEqual(life.get(db,TID)['execution'],'retired')
            self.assertEqual(life.get(db,TID)['evidence']['owner_retirement']['source'],'online_owner_decision')
            result,out,_=self.run_cli('--apply','--expected-agent',AGENT)
            self.assertEqual(result,0);self.assertEqual(json.loads(out)['status'],'already_retired')
            saved=json.loads(db.execute('SELECT data FROM requests WHERE id=?',(TID,)).fetchone()[0])
            self.assertEqual(saved['state'],'unknown');self.assertEqual(saved['reply'],'')
            self.assertNotIn('settled',saved)

    def test_busy_refusal_public_id_retires_the_exact_shared_attempt(self):
        import test_shared_bridge as fixture
        import server
        from officer_messaging import MessagingStore
        import peer_bridge as bridge
        import peer_lifecycle as life

        self.use_real_owner_server(server)
        self.sessions = str(self.root / 'sessions')
        os.mkdir(self.sessions)
        self.store = MessagingStore(str(self.state), self.sessions)
        self.addCleanup(self.store.close)
        self.store.available = True
        self.directory = fixture.FixtureDirectory()
        self.store.shared.directory = self.directory
        db = self.store.db
        credential = bridge.initialize_service(db, {'providers': ['codex', 'claude'], 'history_days': 'all'})
        auth = bridge.service_credential(db, credential['credential_id'])
        self.append = fixture.SharedTests.append.__get__(self)
        target = fixture.SharedTests.target(self, 'Fictional recipient', project=str(self.root))
        args = (auth, target['agent_id'], target['identity_revision'], 'Fictional question')
        old, rejected = fixture.uid(), fixture.uid()
        start = time.time() - 1200
        with patch.object(bridge.time, 'time', return_value=start):
            self.store.shared.send(*args, old)
        tid = db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?', (old,)).fetchone()[0]
        row = next(row for row in self.store._rows() if row['request_id'] == tid)
        row.update(state='awaiting_reply', launched=True, bound_turn='fictional-turn')
        db.execute('UPDATE shared_bridge_requests SET launched=created WHERE transport_id=?', (tid,))
        self.store._save(row)
        with self.assertRaises(bridge.PeerError) as refused:
            self.store.shared.send(*args, rejected)
        self.assertEqual(refused.exception.status, 429)
        matched = re.search(r'Busy with request \(([0-9a-f-]{36})\)', str(refused.exception))
        self.assertIsNotNone(matched)
        public = matched.group(1)
        self.assertEqual(public, old)
        self.assertNotIn(tid, str(refused.exception))
        self.assertNotEqual(public, tid)
        self.assertIsNone(db.execute('SELECT 1 FROM shared_bridge_requests WHERE client_id=?', (rejected,)).fetchone())
        with patch.multiple(server, MESSAGING=self.store, KEY=KEY, PORT=self.port):
            selector = ('--request-id', public)
            with patch.object(cli, 'owner_request', wraps=cli.owner_request) as request:
                result, out, err = self.run_cli(selector=selector)
                self.assertEqual(result, 0)
                self.assertEqual(err, '')
                request.assert_called_once_with(self.port, KEY, 'inspect', {'request_id': public})
            view = json.loads(out)
            self.assertEqual((view['request_id'], view['transport_id'], view['agent'], view['eligible']),
                             (public, tid, target['agent_id'], True))
            with patch.object(cli, 'owner_request', wraps=cli.owner_request) as request:
                result, out, err = self.run_cli('--apply', '--expected-agent', view['agent'], selector=selector)
                self.assertEqual(result, 0)
                self.assertEqual(err, '')
                request.assert_called_once_with(self.port, KEY, 'retire',
                                                {'request_id': public, 'agent': view['agent'], 'confirm': cli.CONFIRM})
            self.assertEqual(life.get(db, tid)['execution'], 'retired')
            applied = json.loads(out)
            self.assertEqual((applied['request_id'], applied['transport_id'], applied['provider_outcome']),
                             (public, tid, 'unknown'))

    def test_apply_requires_exact_identity_before_network(self):
        for extra in [('--apply',), ('--transport-id', '11111111'), ('--expected-agent','x:invalid'), ('--port','70000')]:
            with self.subTest(extra=extra), self.assertRaises(SystemExit): self.run_cli(*extra)
        self.assertEqual(self.calls, [])

    def test_public_cli_requires_one_exact_selector_and_expected_agent_to_apply(self):
        for selector in ((), ('--request-id', PUBLIC, '--transport-id', TID), ('--request-id', PUBLIC[:8]),
                         ('--request-id', PUBLIC.upper())):
            with self.subTest(selector=selector), self.assertRaises(SystemExit):
                self.run_cli(selector=selector)
        with self.assertRaises(SystemExit):
            self.run_cli('--apply', selector=('--request-id', PUBLIC))
        self.assertEqual(self.calls, [])

    def test_public_cli_validates_echoed_public_id_and_canonical_transport_in_one_response(self):
        selector = ('--request-id', PUBLIC)
        result, out, err = self.run_cli(selector=selector)
        self.assertEqual((result, err), (0, ''))
        self.assertEqual((json.loads(out)['request_id'], json.loads(out)['transport_id']), (PUBLIC, TID))
        for mode in ('missing_public', 'wrong_public', 'bad_transport', 'lost', 'redirect'):
            self.mode = mode
            before = len(self.calls)
            with self.subTest(mode=mode):
                result, out, err = self.run_cli('--apply', '--expected-agent', AGENT, selector=selector)
                self.assertEqual((result, out), (1, ''))
                self.assertIn('outcome is unknown', err)
                self.assertEqual(self.calls[before:], [
                    ('/bridge-admin/v1/retire', {'request_id': PUBLIC, 'agent': AGENT, 'confirm': cli.CONFIRM}, KEY)
                ])

    def test_lost_response_redirect_and_invalid_receipts_never_retry(self):
        for mode in ('lost', 'redirect', 'large', 'wrong_identity', 'wrong_outcome', 'nested'):
            with self.subTest(mode=mode):
                self.mode=mode; before=len(self.calls)
                result, out, err = self.run_cli('--apply','--expected-agent',AGENT)
                self.assertEqual(result,1); self.assertEqual(out,'')
                self.assertIn('outcome is unknown',err); self.assertEqual(len(self.calls),before+1)
                self.assertEqual(list(self.state.iterdir()),[self.key])

    def test_decoder_recursion_is_a_bounded_unknown_response(self):
        decode = cli.json.loads
        def limited(value, *args, **kwargs):
            if isinstance(value, str):
                raise RecursionError('fictional decoder limit')
            return decode(value, *args, **kwargs)
        with patch.object(cli.json, 'loads', side_effect=limited):
            result, out, err = self.run_cli('--apply', '--expected-agent', AGENT)
        self.assertEqual(result, 1); self.assertEqual(out, '')
        self.assertIn('outcome is unknown', err); self.assertEqual(len(self.calls), 1)

    def test_only_validated_receipt_fields_are_printed(self):
        self.mode='secret_extra'
        result,out,_=self.run_cli()
        self.assertEqual(result,0); self.assertNotIn('secret',out); self.assertNotIn('preview',out)
        self.assertEqual(set(json.loads(out)), {'transport_id','agent','execution','capture','retained_payload','eligible'})
        self.assertEqual(self.run_cli('--expected-agent','c:'+AGENT[2:])[0],1)

    def test_unavailable_server_does_not_fall_back(self):
        with patch.object(cli.http.client.HTTPConnection,'connect',side_effect=OSError('fictional error '+KEY)):
            self.assertEqual(self.run_cli('--apply','--expected-agent',AGENT)[0],1)
        self.assertEqual(self.calls,[]); self.assertEqual(list(self.state.iterdir()),[self.key])

    def test_key_and_directory_privacy_link_and_type_guards(self):
        self.assertEqual(cli.read_owner_key(self.state),KEY)
        for mode in (0o644,0o666):
            self.key.chmod(mode)
            with self.assertRaises(ValueError):cli.read_owner_key(self.state)
        self.key.chmod(0o600); self.state.chmod(0o755)
        with self.assertRaises(ValueError):cli.read_owner_key(self.state)
        self.state.chmod(0o700)
        alias=self.root/'alias'; alias.symlink_to(self.state)
        with self.assertRaises(OSError):cli.read_owner_key(alias)
        saved=self.root/'saved';self.key.rename(saved);self.key.symlink_to(saved)
        with self.assertRaises(OSError):cli.read_owner_key(self.state)
        self.key.unlink();os.link(saved,self.key)
        with self.assertRaises(ValueError):cli.read_owner_key(self.state)
        self.key.unlink();os.mkfifo(self.key,0o600)
        with self.assertRaises(ValueError):cli.read_owner_key(self.state)
        self.assertEqual(self.calls,[])

    def test_invalid_key_text_never_reaches_network_or_output(self):
        for value in (b'', b'a\nb', b'a\rb', b'a\x00b', b'\xff', b'x'*4097):
            with self.subTest(value_length=len(value)):
                self.key.write_bytes(value)
                self.assertEqual(self.run_cli()[0],1)
        self.assertEqual(self.calls,[])


if __name__ == '__main__':
    unittest.main()
