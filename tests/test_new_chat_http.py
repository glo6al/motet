"""Strict HTTP parsing without sockets, native processes, or owner credentials."""
from email.message import Message
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_http import handle_http
from new_chat_types import NewChatError

NATIVE = '11111111-1111-4111-8111-111111111111'
PREFIX = '/api/new-chat/'


class Connection:
    def settimeout(self, value):
        pass


class Handler:
    def __init__(self, path=PREFIX + 'context', command='GET', body=b'', headers=()):
        self.path, self.command = path, command
        self.headers = Message()
        for k, v in [('Host', '127.0.0.1:18801'), ('X-Live-Room-Key', 'fictional-key'), *headers]:
            self.headers[k] = v
        self.rfile, self.connection = io.BytesIO(body), Connection()
        self.result, self.close_connection = None, False

    def send(self, status, body, content_type):
        self.result = (status, json.loads(body))


class Service:
    def __init__(self):
        self.calls = []

    def context(self):
        self.calls.append('context')
        return {'schemaVersion': 1, 'folders': []}

    def register_folder(self, data):
        self.calls.append(('register_folder', data))
        return {'schemaVersion': 1}

    def prepare(self, data):
        self.calls.append(('prepare', data))
        return {'requestId': NATIVE}

    def open(self, data):
        self.calls.append(('open', data))
        return 202, {'attemptId': NATIVE}

    def status(self, ident):
        self.calls.append(('status', ident))
        return {'attemptId': ident}

    def request_status(self, ident):
        self.calls.append(('request_status', ident))
        return {'requestId': ident, 'status': 'prepared', 'attempt': None}

    def observe(self, ident, data):
        self.calls.append(('observe', ident, data))
        return {'attemptId': ident}


class HttpTests(unittest.TestCase):
    def call(self, handler, service=None):
        handle_http(handler, service, 'fictional-key', 18801)
        self.assertTrue(handler.close_connection)
        return handler.result

    def post(self, path=PREFIX + 'attempts', raw=b'{}', headers=()):
        return Handler(path, 'POST', raw, [('Content-Type', 'application/json'), ('Content-Length', str(len(raw))), *headers])

    def test_owner_authentication_precedes_body_and_service_lookup(self):
        class Unreadable:
            def read1(self, _):
                raise AssertionError('Unauthenticated body was consumed')
        for path in (PREFIX + 'context', PREFIX + 'attempts', PREFIX + 'folders'):
            h = self.post(path)
            h.headers.replace_header('X-Live-Room-Key', 'wrong')
            h.rfile = Unreadable()
            self.assertEqual(self.call(h)[0], 403)
        self.assertEqual(self.call(Handler())[0], 503)

    def test_duplicate_auth_host_origin_and_cross_origin_rejected(self):
        for extra in [('Host', '127.0.0.1:18801'), ('X-Live-Room-Key', 'fictional-key'),
                      ('Origin', 'https://fictional.invalid')]:
            self.assertEqual(self.call(Handler(headers=[extra]), Service())[0], 403)
        h = Handler(headers=[('Origin', 'http://127.0.0.1:18801'), ('Origin', 'http://127.0.0.1:18801')])
        self.assertEqual(self.call(h, Service())[0], 403)

    def test_all_routes_forward_only_authenticated_data(self):
        s = Service()
        routes = [(Handler(), 200), (Handler(PREFIX + 'requests/' + NATIVE), 200), (Handler(PREFIX + 'attempts/' + NATIVE), 200),
                  (self.post(PREFIX + 'intents'), 201), (self.post(), 202),
                  (self.post(PREFIX + 'folders', b'{"path":"/fictional/project"}'), 201),
                  (self.post(PREFIX + 'attempts/' + NATIVE + '/observe', b'{"action":"stop"}'), 200)]
        for h, status in routes:
            self.assertEqual(self.call(h, s)[0], status)
        self.assertEqual(len(s.calls), 7)
        self.assertIn(('register_folder', {'path': '/fictional/project'}), s.calls)

    def test_folder_registration_rejects_foreign_or_duplicate_auth_and_read_routes(self):
        for extra in [('Host', '127.0.0.1:18801'), ('X-Live-Room-Key', 'fictional-key'),
                      ('Origin', 'https://fictional.invalid')]:
            service = Service()
            handler = self.post(PREFIX + 'folders', b'{"path":"/fictional/project"}', headers=[extra])
            self.assertEqual(self.call(handler, service)[0], 403)
            self.assertEqual(service.calls, [])
        for method in ('GET', 'DELETE'):
            service = Service()
            self.assertEqual(self.call(Handler(PREFIX + 'folders', method), service)[0], 404)
            self.assertEqual(service.calls, [])

    def test_request_recovery_is_owner_only_get_and_never_launches(self):
        service = Service()
        handler = Handler(PREFIX + 'requests/' + NATIVE)
        code, result = self.call(handler, service)
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(service.calls, [('request_status', NATIVE)])
        denied = Handler(PREFIX + 'requests/' + NATIVE)
        denied.headers.replace_header('X-Live-Room-Key', 'wrong')
        self.assertEqual(self.call(denied, service)[0], 403)
        self.assertEqual(self.call(self.post(PREFIX + 'requests/' + NATIVE), service)[0], 404)
        self.assertEqual(service.calls, [('request_status', NATIVE)])

    def test_queries_fragments_encoding_and_absolute_routes_rejected(self):
        for suffix in ('context?x=1', 'context#fragment', '%63ontext', 'context?'):
            h = Handler(PREFIX + suffix)
            code = self.call(h, Service())[0]
            if suffix == 'context?':
                # A bare query delimiter is rejected too; parsing alone loses it.
                self.assertEqual(code, 400)
            else:
                self.assertEqual(code, 400)
        self.assertEqual(self.call(Handler('http://localhost:18801' + PREFIX + 'context'), Service())[0], 400)

    def test_duplicate_json_keys_nonfinite_nested_numbers_and_non_objects(self):
        for raw in [b'{"requestId":1,"requestId":2}', b'{"bad":NaN}', b'{"nested":{"bad":1e999}}',
                    b'{"bad":Infinity}', b'[]', b'null', b'{', b'{"bad":"\xff"}']:
            s = Service()
            self.assertEqual(self.call(self.post(raw=raw), s)[0], 400)
            self.assertEqual(s.calls, [])

    def test_oversize_partial_transfer_and_duplicate_lengths_rejected(self):
        h = self.post()
        h.headers.replace_header('Content-Length', '65537')
        self.assertEqual(self.call(h, Service())[0], 413)
        for extra in [('Transfer-Encoding', 'chunked'), ('Content-Length', '2')]:
            self.assertEqual(self.call(self.post(headers=[extra]), Service())[0], 400)
        h = self.post()
        h.headers.replace_header('Content-Length', '5')
        self.assertEqual(self.call(h, Service())[0], 400)
        for length in ('-1', '0', 'two', '0000001'):
            h = self.post()
            h.headers.replace_header('Content-Length', length)
            self.assertEqual(self.call(h, Service())[0], 400)

    def test_get_body_or_unreviewed_recovery_route_rejected(self):
        self.assertEqual(self.call(Handler(headers=[('Content-Length', '1')]), Service())[0], 400)
        self.assertEqual(self.call(self.post(PREFIX + 'attempts/' + NATIVE + '/open-existing'), Service())[0], 404)

    def test_error_projection_never_exposes_unexpected_exception_text(self):
        s = Service()
        s.context = lambda: (_ for _ in ()).throw(RuntimeError('/private/fictional/raw-secret'))
        status, view = self.call(Handler(), s)
        self.assertEqual(status, 503)
        self.assertNotIn('raw-secret', json.dumps(view))
        s.context = lambda: (_ for _ in ()).throw(NewChatError('folder_changed', 'Review the folder.', 409))
        self.assertEqual(self.call(Handler(), s)[0], 409)


if __name__ == '__main__':
    unittest.main()
