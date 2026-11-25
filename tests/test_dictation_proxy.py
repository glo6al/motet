"""Owner proxy boundary and real ephemeral loopback transport; no speech runtime."""
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import dictation_proxy as proxy
from dictation_auth import Authenticator

OWNER = 'fictional-owner-key'
HELPER_KEY = 'a' * 64
SESSION = 'b' * 48
PORT = 18801


class Connection:
    def settimeout(self, value):
        pass


class Handler:
    def __init__(self, action='status', method='GET', body=b'', headers=()):
        self.path = '/api/dictation/' + action
        self.command = method
        self.headers = Message()
        for key, value in [('Host', '127.0.0.1:%d' % PORT), ('X-Live-Room-Key', OWNER), *headers]:
            self.headers[key] = value
        self.rfile = io.BytesIO(body)
        self.connection = Connection()
        self.close_connection = False
        self.result = None

    def send(self, status, body, mime, extra=()):
        self.result = status, json.loads(body)
        self.extra = extra


class Spy:
    enabled, demo = True, False

    def __init__(self):
        self.calls = []

    def request(self, action, payload=None, session=None, sequence=None, demo=False):
        self.calls.append((action, payload, session, sequence, demo))
        return 200, {'schemaVersion': 1, 'state': 'ready'}


class HttpBoundaryTests(unittest.TestCase):
    def invoke(self, handler, service=None, demo=False):
        proxy.handle_http(handler, service, OWNER, PORT, demo=demo)
        self.assertTrue(handler.close_connection)
        self.assertIn(('Cache-Control', 'no-store'), handler.extra)
        return handler.result

    def post(self, action='start', data=None, body=None, headers=()):
        raw = json.dumps({} if data is None else data).encode() if body is None else body
        return Handler(action, 'POST', raw, [('Content-Type', 'application/json'),
                                           ('Content-Length', str(len(raw))), *headers])

    def test_owner_authentication_precedes_body_and_proxy_access(self):
        class Unreadable:
            def read1(self, _):
                raise AssertionError('Read an unauthenticated body')

        class Untouchable:
            def __getattribute__(self, name):
                raise AssertionError('Consulted proxy before owner authentication')

        for method in ('GET', 'POST', 'HEAD', 'OPTIONS'):
            handler = self.post()
            handler.command = method
            handler.headers.replace_header('X-Live-Room-Key', 'wrong')
            handler.rfile = Unreadable()
            self.assertEqual(self.invoke(handler, Untouchable())[0], 403)

    def test_duplicate_headers_host_origin_and_fetch_site_are_refused(self):
        for extra in [('Host', '127.0.0.1:%d' % PORT), ('X-Live-Room-Key', OWNER),
                      ('Origin', 'https://fictional.invalid'), ('Sec-Fetch-Site', 'cross-site'),
                      ('Sec-Fetch-Site', 'same-site')]:
            self.assertEqual(self.invoke(Handler(headers=[extra]), Spy())[0], 403)
        for name, value in [('Origin', 'http://127.0.0.1:%d' % PORT), ('Sec-Fetch-Site', 'same-origin')]:
            self.assertEqual(self.invoke(Handler(headers=[(name, value), (name, value)]), Spy())[0], 403)
        handler = Handler()
        del handler.headers['X-Live-Room-Key']
        self.assertEqual(self.invoke(handler, Spy())[0], 403)

    def test_noncanonical_routes_and_unsupported_methods_never_forward(self):
        service = Spy()
        for path in ('/api/dictation/status?', '/api/dictation/status#', '/api/dictation/%73tatus',
                     'http://127.0.0.1:1/api/dictation/status', '//other.invalid/api/dictation/status',
                     '/api/dictation/status?port=1'):
            handler = Handler()
            handler.path = path
            self.assertEqual(self.invoke(handler, service)[0], 400)
        for method in ('HEAD', 'OPTIONS', 'PUT', 'DELETE', 'PATCH'):
            self.assertEqual(self.invoke(Handler(method=method), service)[0], 404)
        self.assertFalse(service.calls)

    def test_get_requires_no_body_and_the_status_route(self):
        for extra in [[('Content-Length', '1')], [('Content-Length', '0'), ('Content-Length', '0')],
                      [('Transfer-Encoding', 'chunked')]]:
            self.assertEqual(self.invoke(Handler(headers=extra), Spy())[0], 400)
        self.assertEqual(self.invoke(Handler(action='start'), Spy())[0], 404)
        self.assertEqual(self.invoke(self.post(action='status'), Spy())[0], 404)

    def test_disabled_and_demo_requests_do_not_touch_state_or_network(self):
        class Unreadable:
            def read1(self, _):
                raise AssertionError('Disabled request consumed audio')

        for enabled, built_demo, request_demo in ((False, False, False), (True, True, False), (True, False, True)):
            service = proxy.DictationProxy('fictional-home', enabled=enabled, demo=built_demo)
            with mock.patch.object(proxy, '_read_helper_key', side_effect=AssertionError('Read helper state')), \
                    mock.patch.object(proxy.http.client, 'HTTPConnection', side_effect=AssertionError('Contacted helper')):
                status, value = self.invoke(Handler(), service, demo=request_demo)
                self.assertEqual(status, 200)
                self.assertEqual(value['state'], 'disabled' if built_demo or request_demo else 'not_installed')
                handler = self.post()
                handler.rfile = Unreadable()
                self.assertEqual(self.invoke(handler, service, demo=request_demo)[0], 503)

    def test_invalid_json_content_types_and_framing_do_not_forward(self):
        service = Spy()
        for raw in (b'null', b'[]', b'{"intervalSeconds":NaN}', b'{"intervalSeconds":Infinity}',
                    b'{"intervalSeconds":1e9999}', b'{"intervalSeconds":0.25,"intervalSeconds":0.5}',
                    b'{"url":"http://other.invalid"}', b'{"port":1}', b'{"intervalSeconds":true}'):
            self.assertEqual(self.invoke(self.post(body=raw), service)[0], 400)
        for extra in [('Content-Length', '2'), ('Transfer-Encoding', 'chunked'), ('Content-Type', 'application/json')]:
            self.assertEqual(self.invoke(self.post(headers=[extra]), service)[0], 400)
        for length in ('-1', '1,1', 'x', '0000001', '0'):
            handler = self.post()
            handler.headers.replace_header('Content-Length', length)
            self.assertEqual(self.invoke(handler, service)[0], 400)
        handler = self.post()
        handler.headers.replace_header('Content-Length', str(proxy.MAX_JSON + 1))
        self.assertEqual(self.invoke(handler, service)[0], 413)
        self.assertFalse(service.calls)

    def test_body_has_a_fixed_deadline(self):
        with mock.patch.object(proxy.time, 'monotonic', side_effect=(0, 3)):
            self.assertEqual(self.invoke(self.post(), Spy())[0], 400)

    def test_audio_session_and_sequence_are_explicit_and_singleton(self):
        service = Spy()

        def audio(extra=(), raw=b'\0' * 16):
            return Handler('audio', 'POST', raw, [('Content-Type', 'application/octet-stream'),
                           ('Content-Length', str(len(raw))), ('X-Dictation-Session', SESSION),
                           ('X-Dictation-Sequence', '0'), *extra])

        self.assertEqual(self.invoke(audio(), service)[0], 200)
        self.assertEqual(service.calls[-1], ('audio', b'\0' * 16, SESSION, 0, False))
        for extra in [('X-Dictation-Session', SESSION), ('X-Dictation-Sequence', '0')]:
            self.assertEqual(self.invoke(audio([extra]), service)[0], 400)
        self.assertEqual(self.invoke(audio(raw=b'123'), service)[0], 400)
        handler = audio()
        handler.headers.replace_header('X-Dictation-Session', 'http://other.invalid')
        self.assertEqual(self.invoke(handler, service)[0], 400)

    def test_control_and_events_schema_cannot_choose_a_destination(self):
        service = Spy()
        for action in ('stop', 'cancel', 'reset'):
            self.assertEqual(self.invoke(self.post(action, {'sessionId': SESSION}), service)[0], 200)
            self.assertEqual(self.invoke(self.post(action, {'sessionId': SESSION, 'port': 1}), service)[0], 400)
        self.assertEqual(self.invoke(self.post('events', {'sessionId': SESSION, 'after': 0}), service)[0], 200)
        for after in (True, -1, '0', 9007199254740992):
            self.assertEqual(self.invoke(self.post('events', {'sessionId': SESSION, 'after': after}), service)[0], 400)

    def test_import_constructor_and_configuration_are_io_free(self):
        source = Path(proxy.__file__).read_text()
        isolated = types.ModuleType('isolated_dictation_proxy')
        with mock.patch.object(Path, 'resolve', side_effect=AssertionError('Resolved a path')), \
                mock.patch.object(os, 'open', side_effect=AssertionError('Opened state')), \
                mock.patch.object(proxy.http.client, 'HTTPConnection', side_effect=AssertionError('Connected')):
            exec(compile(source, 'isolated_dictation_proxy', 'exec'), isolated.__dict__)
            self.assertFalse(proxy.DictationProxy('fictional-home').enabled)
            self.assertTrue(proxy.from_environment('fictional-home', PORT, {'WATCH_DICTATION_ENABLED': '1'}).enabled)
            for value in ('0', '65536', 'http://other.invalid', '8793', str(PORT), 18879):
                self.assertFalse(proxy.from_environment('fictional-home', PORT, {
                    'WATCH_DICTATION_ENABLED': '1', 'WATCH_DICTATION_PORT': value}).enabled)
            self.assertTrue(proxy.from_environment('fictional-home', PORT, {'WATCH_DEMO': '1'}).demo)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def dispatch(self):
        state = self.server.test_state
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        state['calls'].append((self.command, self.path, dict(self.headers), raw))
        action = self.path.rsplit('/', 1)[-1]
        code, value = 200, {'schemaVersion': 1}
        mode = state.get('mode')
        if action == 'challenge':
            if mode in ('header_drip', 'body_drip'):
                body = json.dumps(self.server.authentication.challenge(self.headers['X-Dictation-Client-Nonce'])).encode()
                head = ('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body)).encode()
                try:
                    if mode == 'body_drip':
                        self.wfile.write(head); self.wfile.flush()
                    for byte in (head if mode == 'header_drip' else body):
                        self.wfile.write(bytes([byte])); self.wfile.flush(); threading.Event().wait(.015)
                except OSError:
                    pass
                return
            if mode == 'slow':
                threading.Event().wait(.25)
            body = json.dumps(self.server.authentication.challenge(self.headers['X-Dictation-Client-Nonce'])).encode()
            self.send_response(200);self.send_header('Content-Type', 'application/json');self.send_header('Content-Length', str(len(body)));self.send_header('Connection', 'close');self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
            return
        if mode == 'slow':
            threading.Event().wait(.25)
        if mode == 'error':
            code, value = 409, {'schemaVersion': 1, 'error': {'code': 'out_of_order',
                                      'message': 'Native failure: ' + HELPER_KEY, 'privatePath': 'private-location'}}
        elif mode == 'auth':
            code, value = 403, {'error': {'code': 'forbidden', 'message': HELPER_KEY}}
        elif action == 'status':
            value.update(state='ready', code=None, loadMs=23.5, microphone='client_owned',
                         audioFormat={'sampleRate': 16000, 'channels': 1, 'encoding': 'float32le'},
                         maxPacketBytes=64000, maxTakeSeconds=180, privateKey=HELPER_KEY, privatePath='private-location')
        elif action == 'start':
            state['epoch'] = 1
            value.update(sessionId=SESSION, epoch=1, privateKey=HELPER_KEY)
        elif action == 'audio':
            value.update(acceptedSequence=int(self.headers['X-Dictation-Sequence']), epoch=state['epoch'],
                         level={'peak': .5, 'rms': .25, 'privateKey': HELPER_KEY}, commandMs=4.5)
        elif action == 'events':
            text = HELPER_KEY if mode == 'secret' else 'A phrase while speaking.'
            value.update(sessionId=SESSION, epoch=state['epoch'], ended=False,
                         events=[{'id': 1, 'type': 'line', 'epoch': 1, 'lineId': '1', 'text': text, 'complete': False},
                                 {'id': 2, 'type': 'line', 'epoch': 1, 'lineId': '1', 'text': 'A final phrase.', 'complete': True}])
        else:
            if action == 'reset':
                state['epoch'] += 1
            value.update(sessionId=SESSION, epoch=state['epoch'], ended=action != 'reset', finalizeMs=5.25)
        if mode == 'wrong_session':
            value['sessionId'] = 'c' * 48
        if mode == 'wrong_sequence':
            value['acceptedSequence'] += 1
        body = json.dumps(value).encode()
        if mode == 'redirect':
            code, body = 302, b'{}'
        if mode == 'duplicate_json':
            body = b'{"schemaVersion":1,"schemaVersion":1}'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(proxy.MAX_RESPONSE + 1 if mode == 'oversize' else len(body)))
        if mode == 'redirect':
            self.send_header('Location', 'http://127.0.0.1:1/credential-collector')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    do_GET = dispatch
    do_POST = dispatch


class BusyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix' or proxy.ps.folder_walk_available(),
                         'Private helper state integration (POSIX, or the Windows folder walk)')
    def test_actual_pool_pressure_returns_busy_over_owner_http_without_model_failure(self):
        sys.path.insert(0, str(ROOT))
        from integrations.dictation.helper import HelperServer
        from integrations.dictation.service import DictationService
        from integrations.dictation.state import PrivateState
        from dictation_auth import REQUEST_CAPACITY
        with tempfile.TemporaryDirectory() as home:
            key = PrivateState(home).key(create=True)
            service = DictationService(lambda: None, lambda: None)
            helper = HelperServer(0, key, service)
            helper_thread = threading.Thread(target=helper.serve_forever, daemon=True)
            helper_thread.start()
            adapter = proxy.DictationProxy(home, helper.server_port, enabled=True)
            class OwnerHandler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self): proxy.handle_http(self, adapter, OWNER, self.server.server_port)
                def send(self, status, body, mime, extra=()):
                    self.send_response(status)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', str(len(body)))
                    for name, value in extra: self.send_header(name, value)
                    self.end_headers(); self.wfile.write(body)
            owner = ThreadingHTTPServer(('127.0.0.1', 0), OwnerHandler)
            owner_thread = threading.Thread(target=owner.serve_forever, daemon=True)
            owner_thread.start()
            def status():
                import http.client
                connection = http.client.HTTPConnection('127.0.0.1', owner.server_port, timeout=3)
                try:
                    connection.request('GET', '/api/dictation/status', headers={'X-Live-Room-Key': OWNER})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally: connection.close()
            try:
                with mock.patch.object(service, 'status', wraps=service.status) as model_status:
                    for name in ('public_slots', 'slots'):
                        pool = getattr(helper, name)
                        for _ in range(REQUEST_CAPACITY): pool.acquire()
                        try:
                            with self.subTest(pool=name):
                                code, value = status()
                                self.assertEqual(code, 503)
                                self.assertEqual(value['error']['code'], 'busy')
                                self.assertNotIn('state', value, 'admission pressure is not a failed model')
                                model_status.assert_not_called()
                        finally:
                            for _ in range(REQUEST_CAPACITY): pool.release()
                    code, value = status()
                    self.assertEqual((code, value['state']), (200, 'not_installed'))
                    model_status.assert_called_once()
            finally:
                owner.shutdown(); owner.server_close(); owner_thread.join(2)
                helper.shutdown(); helper.server_close(); helper_thread.join(2)
            self.assertFalse(owner_thread.is_alive())
            self.assertFalse(helper_thread.is_alive())

    def test_saturated_challenge_is_retryable_not_a_setup_error(self):
        from dictation_auth import BusyError
        adapter = proxy.DictationProxy(Path('fictional-home'), 18879, enabled=True)
        with mock.patch.object(proxy, '_read_helper_key', return_value=HELPER_KEY), \
                mock.patch.object(adapter, '_forward', side_effect=BusyError()):
            for action in ('status', 'start'):
                with self.assertRaises(proxy.ProxyError) as caught:
                    adapter.request(action, {})
                self.assertEqual((caught.exception.status, caught.exception.code), (503, 'busy'))


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.assertNotEqual(self.server.server_port, 8793)
        self.server.test_state = {'calls': [], 'epoch': 1}
        self.server.authentication = Authenticator(HELPER_KEY, self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = proxy.DictationProxy(self.home, self.server.server_port, enabled=True)
        self.key_patch = mock.patch.object(proxy, '_read_helper_key', return_value=HELPER_KEY)
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_real_transport_forwards_only_its_helper_key_and_fixed_destination(self):
        with mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://other.invalid:1', 'HTTPS_PROXY': 'http://other.invalid:1'}):
            status, value = self.service.request('status')
        self.assertEqual(status, 200)
        self.assertEqual(value['state'], 'ready')
        self.assertNotIn(HELPER_KEY, json.dumps(value))
        method, path, headers, raw = self.server.test_state['calls'][-1]
        self.assertEqual((method, path, raw), ('GET', '/v1/status', b''))
        self.assertNotIn('X-Dictation-Key', headers)
        self.assertIn('X-Dictation-Proof', headers)
        self.assertTrue(all(HELPER_KEY not in str(call[2]) for call in self.server.test_state['calls']))
        self.assertNotIn('X-Live-Room-Key', headers)
        self.assertNotIn('Origin', headers)
        self.assertNotIn('privatePath', value)

    def test_wrong_listener_and_cross_port_relay_cannot_receive_key_or_audio(self):
        for key,authority in (('c'*64,self.server.server_port),(HELPER_KEY,self.server.server_port+1)):
            self.server.test_state['calls'].clear()
            self.server.authentication=Authenticator(key,authority)
            with self.assertRaises(proxy.ProxyError) as caught:
                self.service.request('audio',b'\0'*16,SESSION,0)
            self.assertEqual(caught.exception.code,'helper_authentication_failed')
            calls=self.server.test_state['calls']
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][1],'/v1/challenge')
            self.assertEqual(calls[0][3],b'')
            self.assertNotIn('X-Dictation-Key',calls[0][2])
            self.assertNotIn('X-Dictation-Proof',calls[0][2])
            self.assertNotIn(HELPER_KEY,str(calls[0][2]))

    def test_absolute_deadline_bounds_dripping_challenge_headers_and_body(self):
        for mode in ('header_drip','body_drip'):
            self.server.test_state['mode']=mode
            began=time.monotonic()
            with mock.patch.object(proxy,'HELPER_TIMEOUT',.12):
                result=self.service.request('status')[1]
            self.assertIn(result['state'],('not_running','failed'))
            self.assertLess(time.monotonic()-began,.5)
        self.server.test_state['mode']=None
        self.assertEqual(self.service.request('status')[1]['state'],'ready')

    def test_actual_partial_final_audio_and_reset_protocol_is_preserved(self):
        _, start = self.service.request('start', {'intervalSeconds': .25})
        self.assertEqual(start, {'schemaVersion': 1, 'sessionId': SESSION, 'epoch': 1})
        _, audio = self.service.request('audio', b'\0' * 16, SESSION, 3)
        self.assertEqual(audio['acceptedSequence'], 3)
        self.assertEqual(audio['level'], {'peak': .5, 'rms': .25})
        self.assertEqual(self.server.test_state['calls'][-1][2]['X-Dictation-Session'], SESSION)
        _, events = self.service.request('events', {'sessionId': SESSION, 'after': 0})
        self.assertEqual([item['complete'] for item in events['events']], [False, True])
        self.assertEqual(events['events'][0]['text'], 'A phrase while speaking.')
        _, reset = self.service.request('reset', {'sessionId': SESSION})
        self.assertEqual(reset['epoch'], 2)
        self.assertFalse(reset['ended'])
        _, stopped = self.service.request('stop', {'sessionId': SESSION})
        self.assertTrue(stopped['ended'])

    def test_redirects_are_not_followed_and_credential_echo_is_not_returned(self):
        for mode in ('redirect', 'oversize', 'duplicate_json'):
            self.server.test_state['mode'] = mode
            with self.assertRaises(proxy.ProxyError) as caught:
                self.service.request('start', {})
            self.assertEqual(caught.exception.code, 'invalid_response')
        self.server.test_state['mode'] = 'secret'
        with self.assertRaises(proxy.ProxyError) as caught:
            self.service.request('events', {'sessionId': SESSION, 'after': 0})
        self.assertEqual(caught.exception.code, 'invalid_response')
        self.assertNotIn(HELPER_KEY, str(caught.exception))
        self.server.test_state['mode'] = None
        self.assertEqual(self.service.request('status')[1]['state'], 'ready')
        self.assertTrue(all(call[1].startswith('/v1/') for call in self.server.test_state['calls']))

    def test_safe_error_codes_survive_but_native_error_messages_do_not(self):
        self.server.test_state['mode'] = 'error'
        handler = Handler('audio', 'POST', b'\0' * 4, [('Content-Type', 'application/octet-stream'),
                          ('Content-Length', '4'), ('X-Dictation-Session', SESSION), ('X-Dictation-Sequence', '0')])
        proxy.handle_http(handler, self.service, OWNER, PORT)
        status, value = handler.result
        self.assertEqual(status, 409)
        self.assertEqual(value['error']['code'], 'out_of_order')
        self.assertNotIn(HELPER_KEY, json.dumps(value))
        self.assertNotIn('Native failure', json.dumps(value))
        self.server.test_state['mode'] = 'auth'
        self.assertEqual(self.service.request('status')[1]['code'], 'helper_authentication_failed')

    def test_responses_must_match_the_requested_session_and_audio_sequence(self):
        self.server.test_state['mode'] = 'wrong_session'
        with self.assertRaises(proxy.ProxyError) as caught:
            self.service.request('stop', {'sessionId': SESSION})
        self.assertEqual(caught.exception.code, 'invalid_response')
        self.server.test_state['mode'] = 'wrong_sequence'
        with self.assertRaises(proxy.ProxyError) as caught:
            self.service.request('audio', b'\0' * 4, SESSION, 3)
        self.assertEqual(caught.exception.code, 'invalid_response')

    def test_connection_refusal_is_not_running_and_post_is_safe(self):
        stopped = socket.socket()
        stopped.bind(('127.0.0.1', 0))
        port = stopped.getsockname()[1]
        stopped.close()
        service = proxy.DictationProxy(self.home, port, enabled=True)
        self.assertEqual(service.request('status')[1]['state'], 'not_running')
        with self.assertRaises(proxy.ProxyError) as caught:
            service.request('start', {})
        self.assertEqual(caught.exception.code, 'not_running')

    def test_helper_response_deadline_releases_its_slot(self):
        self.server.test_state['mode'] = 'slow'
        began = time.monotonic()
        with mock.patch.object(proxy, 'HELPER_TIMEOUT', .1):
            self.assertEqual(self.service.request('status')[1]['state'], 'not_running')
        self.assertLess(time.monotonic() - began, 1)
        self.server.test_state['mode'] = None
        self.assertEqual(self.service.request('status')[1]['state'], 'ready')


@unittest.skipUnless(os.name == 'posix', 'Helper private state requires POSIX protections')
class PrivateKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.room = self.home / '.live-room'
        self.state = self.room / 'dictation'
        self.room.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.header = self.state / 'header'
        self.header.write_text('X-Dictation-Key: ' + HELPER_KEY + '\n')
        self.header.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_key_read_is_read_only_and_anchored(self):
        before = {str(path.relative_to(self.home)): path.stat().st_mtime_ns for path in self.home.rglob('*')}
        self.assertEqual(proxy._read_helper_key(self.home), HELPER_KEY)
        after = {str(path.relative_to(self.home)): path.stat().st_mtime_ns for path in self.home.rglob('*')}
        self.assertEqual(before, after)
        real_stat = os.stat

        def replaced(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if name == 'dictation' and kwargs.get('dir_fd') is not None:
                return type('Changed', (), {'st_dev': result.st_dev, 'st_ino': result.st_ino + 1})()
            return result

        with mock.patch.object(os, 'stat', side_effect=replaced):
            with self.assertRaises(ValueError):
                proxy._read_helper_key(self.home)

    def test_unsafe_permissions_links_and_malformed_keys_never_reach_network(self):
        service = proxy.DictationProxy(self.home, enabled=True)
        with mock.patch.object(proxy.http.client, 'HTTPConnection', side_effect=AssertionError('Unsafe state contacted helper')):
            for path in (self.room, self.state, self.header):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o755 if path.is_dir() else 0o644)
                self.assertEqual(service.request('status')[1]['state'], 'not_installed')
                path.chmod(original)
            self.header.write_text('X-Dictation-Key: wrong\n')
            self.assertEqual(service.request('status')[1]['state'], 'not_installed')
            self.header.unlink()
            self.assertEqual(service.request('status')[1]['state'], 'not_installed')
            outside = self.home / 'outside-header'
            outside.write_text('X-Dictation-Key: ' + HELPER_KEY + '\n')
            outside.chmod(0o600)
            self.header.symlink_to(outside)
            self.assertEqual(service.request('status')[1]['state'], 'not_installed')
            self.header.unlink()
            os.link(str(outside), str(self.header))
            self.assertEqual(service.request('status')[1]['state'], 'not_installed')


if __name__ == '__main__':
    unittest.main()
