"""Strict HTTP parsing without sockets, native processes, or owner credentials."""
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_http import handle_http
from new_chat_types import NewChatError
from new_chat_service import NewChatService

NATIVE = '11111111-1111-4111-8111-111111111111'
EPOCH = '22222222-2222-4222-8222-222222222222'
FOLDER, REVISION = 'a' * 64, 'b' * 64
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

    def saved_folders(self):
        self.calls.append('saved_folders')
        return {'schemaVersion': 1, 'savedFolders': [], 'complete': True, 'unreadableCount': 0}

    def remove_saved_folder(self, ident, data):
        self.calls.append(('remove_saved_folder', ident, data))
        return {'schemaVersion': 1, 'removed': {'id': ident, 'revision': data['revision']}}

    def begin_folder_choice(self, data, connection, forwarded=False):
        self.calls.append(('begin_folder_choice', data, connection, forwarded))
        return {'schemaVersion': 1, 'requestId': NATIVE, 'state': 'pending'}

    def folder_choice_status(self, ident):
        self.calls.append(('folder_choice_status', ident))
        return {'schemaVersion': 1, 'requestId': ident, 'state': 'pending'}

    def cancel_folder_choice(self, ident, data):
        self.calls.append(('cancel_folder_choice', ident, data))
        return {'schemaVersion': 1, 'requestId': ident, 'state': 'cancelled'}

    def accept_folder_choice(self, ident, data):
        self.calls.append(('accept_folder_choice', ident, data))
        return {'schemaVersion': 1, 'requestId': ident, 'state': 'saved'}

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

    def test_chooser_routes_authenticate_before_any_probe_or_service_access(self):
        class Explosive:
            def __getattr__(self, name):
                raise AssertionError('Unauthenticated chooser reached the service')
        class Unreadable:
            def read1(self, _):
                raise AssertionError('Unauthenticated chooser consumed its body')
        for suffix, method in (('folders/choose', 'POST'),
                               ('folders/choose/' + NATIVE, 'GET'),
                               ('folders/choose/' + NATIVE + '/cancel', 'POST'),
                               ('folders/choose/' + NATIVE + '/accept', 'POST')):
            h = self.post(PREFIX + suffix) if method == 'POST' else Handler(PREFIX + suffix)
            h.headers.replace_header('X-Live-Room-Key', 'wrong')
            h.headers['Forwarded'] = ''
            h.rfile = Unreadable()
            def forbidden_header_scan():
                raise AssertionError('Unauthenticated chooser inspected forwarding headers')
            h.headers.keys = forbidden_header_scan
            self.assertEqual(self.call(h, Explosive())[0], 403)

    def test_chooser_routes_pass_real_accepted_connection_only_to_begin(self):
        service = Service()
        data = {'requestId': NATIVE, 'epoch': EPOCH, 'gesture': {'trusted': True}}
        h = self.post(PREFIX + 'folders/choose', json.dumps(data).encode())
        self.assertEqual(self.call(h, service)[0], 202)
        self.assertEqual(service.calls, [('begin_folder_choice', data, h.connection, False)])
        self.assertIs(service.calls[0][2], h.connection)
        base = PREFIX + 'folders/choose/' + NATIVE
        for handler in (Handler(base), self.post(base + '/cancel', json.dumps({'epoch': EPOCH}).encode()),
                        self.post(base + '/accept', b'{"selectionToken":"' + NATIVE.encode() + b'"}')):
            self.assertEqual(self.call(handler, service)[0], 200)
        self.assertEqual([row[0] for row in service.calls], ['begin_folder_choice', 'folder_choice_status',
                         'cancel_folder_choice', 'accept_folder_choice'])
        self.assertEqual(service.calls[2], ('cancel_folder_choice', NATIVE, {'epoch': EPOCH}))
        self.assertEqual(service.calls[3], ('accept_folder_choice', NATIVE, {'selectionToken': NATIVE}))

    def test_chooser_marks_forwarding_header_presence_without_trusting_values(self):
        data = {'requestId': NATIVE, 'epoch': EPOCH, 'gesture': {'trusted': True}}
        raw = json.dumps(data).encode()
        names = ('Forwarded', 'X-Forwarded-For', 'x-FoRwArDeD-Fictional', 'X-Real-IP', 'Via',
                 'CF-Connecting-IP', 'True-Client-IP', 'X-Original-Forwarded-For',
                 'X-Cluster-Client-IP', 'Fastly-Client-IP')
        for name in names:
            for value in ('', '127.0.0.1'):
                with self.subTest(header=name, empty=not value):
                    service = Service()
                    handler = self.post(PREFIX + 'folders/choose', raw, [(name, value)])
                    self.assertEqual(self.call(handler, service)[0], 202)
                    self.assertEqual(service.calls, [('begin_folder_choice', data, handler.connection, True)])
                    self.assertIs(service.calls[0][2], handler.connection)
        service = Service()
        handler = self.post(PREFIX + 'folders/choose', raw, [('X-Fictional', 'present')])
        self.assertEqual(self.call(handler, service)[0], 202)
        self.assertIs(service.calls[0][3], False)

    def test_chooser_rejects_cross_origin_duplicate_auth_and_read_side_effect_routes(self):
        for suffix in ('folders/choose', 'folders/choose/' + NATIVE + '/cancel',
                       'folders/choose/' + NATIVE + '/accept'):
            for headers in ((('Origin', 'https://fictional.invalid'),),
                            (('X-Live-Room-Key', 'fictional-key'),)):
                service = Service()
                self.assertEqual(self.call(self.post(PREFIX + suffix, headers=headers), service)[0], 403)
                self.assertEqual(service.calls, [])
            service = Service()
            self.assertEqual(self.call(Handler(PREFIX + suffix), service)[0], 404)
            self.assertEqual(service.calls, [])

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
                  (self.post(PREFIX + 'attempts/' + NATIVE + '/observe', b'{"action":"stop"}'), 200)]
        for h, status in routes:
            self.assertEqual(self.call(h, s)[0], status)
        self.assertEqual(len(s.calls), 6)

    def test_saved_management_routes_forward_only_owner_metadata(self):
        service = Service()
        code, listing = self.call(Handler(PREFIX + 'folders'), service)
        self.assertEqual((code, listing), (200, {'schemaVersion': 1, 'savedFolders': [], 'complete': True, 'unreadableCount': 0}))
        code, receipt = self.call(self.post(PREFIX + 'folders/' + FOLDER + '/remove',
                                          json.dumps({'revision': REVISION}).encode()), service)
        self.assertEqual((code, receipt), (200, {'schemaVersion': 1, 'removed': {'id': FOLDER, 'revision': REVISION}}))
        self.assertEqual(service.calls, ['saved_folders', ('remove_saved_folder', FOLDER, {'revision': REVISION})])

    def test_saved_management_authentication_precedes_body_and_service_access(self):
        class Explosive:
            def __getattr__(self, name):
                raise AssertionError('Unauthenticated management reached the service')
        class Unreadable:
            def read1(self, _):
                raise AssertionError('Unauthenticated management consumed its body')
        for route, method in ((PREFIX + 'folders', 'GET'), (PREFIX + 'folders/' + FOLDER + '/remove', 'POST')):
            for extra in (('Host', 'evil.test'), ('Origin', 'https://fictional.invalid'), ('X-Live-Room-Key', 'fictional-key')):
                handler = Handler(route, headers=[extra]) if method == 'GET' else self.post(route, headers=[extra])
                handler.rfile = Unreadable()
                self.assertEqual(self.call(handler, Explosive())[0], 403)

    def test_saved_management_methods_paths_and_strict_body_boundary(self):
        route = PREFIX + 'folders/' + FOLDER + '/remove'
        for handler in (Handler(route), Handler(route, 'DELETE'), Handler(PREFIX + 'folders', 'PUT'),
                        self.post(PREFIX + 'folders/' + FOLDER.upper() + '/remove'),
                        self.post(PREFIX + 'folders/' + FOLDER[:-1] + '/remove'),
                        self.post(route + '/')):
            service = Service()
            self.assertEqual(self.call(handler, service)[0], 404)
            self.assertEqual(service.calls, [])
        for raw in (b'[]', b'{', b'{"revision":"x","revision":"y"}', b'{"revision":NaN}', b'{"revision":"\xff"}'):
            service = Service()
            self.assertEqual(self.call(self.post(route, raw), service)[0], 400)
            self.assertEqual(service.calls, [])
        for suffix in ('?x=1', '#fragment', '?'):
            self.assertEqual(self.call(Handler(PREFIX + 'folders' + suffix), Service())[0], 400)
        self.assertEqual(self.call(Handler(PREFIX + 'folders', headers=[('Content-Length', '1')]), Service())[0], 400)
        oversized = self.post(route)
        oversized.headers.replace_header('Content-Length', '65537')
        self.assertEqual(self.call(oversized, Service())[0], 413)
        self.assertEqual(self.call(self.post(route, headers=[('Transfer-Encoding', 'chunked')]), Service())[0], 400)

    def test_retired_folder_registration_still_authenticates_and_never_registers(self):
        for extra in [('Host', '127.0.0.1:18801'), ('X-Live-Room-Key', 'fictional-key'),
                      ('Origin', 'https://fictional.invalid')]:
            service = Service()
            handler = self.post(PREFIX + 'folders', b'{"path":"/fictional/project"}', headers=[extra])
            self.assertEqual(self.call(handler, service)[0], 403)
            self.assertEqual(service.calls, [])
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            service = Service()
            handler = (self.post(PREFIX + 'folders', b'{"path":"/fictional/project"}') if method == 'POST'
                       else Handler(PREFIX + 'folders', method))
            self.assertEqual(self.call(handler, service)[0], 404)
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


@unittest.skipIf(os.name == 'nt', 'Saved folder management uses private POSIX storage.')
class SavedFoldersServiceHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.folder = root / 'fictional-project'
        self.folder.mkdir()
        self.service = NewChatService(root / 'private-state', {}, lambda: [], enabled=True, home=root)
        self.addCleanup(self.service.close)
        self.saved = self.service.register_folder({'path': str(self.folder)})['folder']

    def call(self, route, data=None):
        raw = json.dumps(data).encode() if data is not None else b''
        handler = Handler(route, 'POST', raw, [('Content-Type', 'application/json'), ('Content-Length', str(len(raw)))]) if data is not None else Handler(route)
        handle_http(handler, self.service, 'fictional-key', 18801)
        self.assertTrue(handler.close_connection)
        return handler.result

    def test_actual_service_partial_metadata_and_missing_target_removal_receipt(self):
        self.folder.rmdir()
        with self.service.db:
            self.service.db.execute('INSERT INTO registered_folders VALUES (?,?)', ('0' * 64, '{'))
        code, listing = self.call(PREFIX + 'folders')
        expected = {key: self.saved[key] for key in ('id', 'revision', 'label', 'displayPath')}
        self.assertEqual((code, listing), (200, {'schemaVersion': 1, 'savedFolders': [expected], 'complete': False, 'unreadableCount': 1}))
        code, receipt = self.call(PREFIX + 'folders/' + self.saved['id'] + '/remove', {'revision': self.saved['revision']})
        self.assertEqual((code, receipt), (200, {'schemaVersion': 1, 'removed': {'id': self.saved['id'], 'revision': self.saved['revision']}}))
        self.assertEqual(self.call(PREFIX + 'folders')[1], {'schemaVersion': 1, 'savedFolders': [], 'complete': False, 'unreadableCount': 1})
        self.assertFalse(self.folder.exists())

    def test_actual_service_invalid_stale_unknown_corrupt_and_database_error_envelopes(self):
        route = PREFIX + 'folders/' + self.saved['id'] + '/remove'
        for data in ({}, {'revision': True}, {'revision': 'A' * 64}, {'revision': self.saved['revision'], 'force': True}):
            code, result = self.call(route, data)
            self.assertEqual((code, result['error']['code']), (400, 'invalid_request'))
        code, result = self.call(route, {'revision': '0' * 64})
        self.assertEqual((code, result['error']['code']), (409, 'saved_folder_changed'))
        code, result = self.call(PREFIX + 'folders/' + '0' * 64 + '/remove', {'revision': self.saved['revision']})
        self.assertEqual((code, result['error']['code']), (404, 'unknown_saved_folder'))
        with self.service.db:
            self.service.db.execute('UPDATE registered_folders SET data=?', ('{',))
        code, result = self.call(route, {'revision': self.saved['revision']})
        self.assertEqual((code, result['error']['code']), (409, 'unreadable_saved_folder'))
        with self.service.db:
            self.service.db.execute('DROP TABLE registered_folders')
        for path, body in ((PREFIX + 'folders', None), (route, {'revision': self.saved['revision']})):
            self.assertEqual(self.call(path, body), (503, {'schemaVersion': 1, 'error': {
                'code': 'saved_folders_unavailable',
                'message': 'Saved project folders are unavailable. Refresh saved folders to check.'}}))


if __name__ == '__main__':
    unittest.main()
