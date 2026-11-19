"""Actual server routes over ephemeral HTTP, without importing the application.

Only the real handler/server classes and fixed page-rendering functions are
compiled from server.py. No observers, private state, provider or native ASR is
started. Every credential and destination in these checks is fictional.
"""
import ast
import contextlib
import gzip
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
from socketserver import TCPServer
import sys
import threading
import unittest
from unittest import mock
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import dictation_proxy

OWNER = 'fictional-dictation-server-owner'
SERVER_SOURCE = (ROOT / 'src/server.py').read_text(encoding='utf-8')


def actual_scope(mutate=lambda source: source):
    tree = ast.parse(mutate(SERVER_SOURCE))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in ('H', 'Server'):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in ('page_with_nonce', 'accepts_page_gzip'):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'UI_CSP' for target in node.targets):
            selected.append(node)
    scope = {'BaseHTTPRequestHandler': BaseHTTPRequestHandler, 'ThreadingHTTPServer': ThreadingHTTPServer,
             'TCPServer': TCPServer, 'WIN': os.name == 'nt', 'LOCK': threading.RLock(),
             'socket': socket, 'threading': threading, 'os': os, 'json': json, 'hmac': hmac,
             're': re, 'secrets': secrets, 'gzip': gzip, 'urllib': urllib,
             'HERE': str(ROOT / 'src'), 'KEY': OWNER, 'PORT': 0,
             'ATTACHMENTS_ENABLED': False, 'NATIVE_AGENT_ATTACHMENTS_ENABLED': False,
             'DICTATION_PROXY': None}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / 'src/server.py'), 'exec'), scope)
    return scope


@contextlib.contextmanager
def running(mutate=lambda source: source):
    scope = actual_scope(mutate)
    server = scope['Server'](('127.0.0.1', 0), scope['H'])
    scope['PORT'] = server.server_port
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield scope, server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
        if thread.is_alive():
            raise AssertionError('Ephemeral test server did not stop')


def request(port, path='/', method='GET', headers=(), body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.putrequest(method, path)
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class Untouchable:
    def __getattribute__(self, name):
        raise AssertionError('An unauthorized or unsupported request consulted the helper proxy')


class RoutingSpy:
    enabled, demo = True, False

    def __init__(self):
        self.calls = []

    def request(self, action, payload=None, session=None, sequence=None, demo=False):
        self.calls.append((action, payload, session, sequence, demo))
        return 200, {'schemaVersion': 1, 'state': 'ready'}


class DictationServerTests(unittest.TestCase):
    def test_nonce_and_fixed_modules_cover_the_workspace_and_inert_demo(self):
        with running() as (scope, port):
            scope['DICTATION_PROXY'] = Untouchable()
            nonce = None
            for route in ('/', '/?demo'):
                status, body, headers = request(port, route)
                self.assertEqual(status, 200)
                self.assertEqual(body.count(b'<script nonce='), 1)
                self.assertEqual(body.count(b'</script>'), 1)
                current = re.search(rb'<script nonce="([A-Za-z0-9_-]+)"', body).group(1).decode()
                self.assertNotEqual(nonce, current)
                nonce = current
                policy = headers['Content-Security-Policy']
                self.assertIn("script-src 'nonce-%s'" % current, policy)
                self.assertIn('http://127.0.0.1:%d/dictation-worklet.js' % port, policy)
                self.assertIn('http://localhost:%d/dictation-worklet.js' % port, policy)
                self.assertIn("connect-src 'self'", policy)
                self.assertIn("frame-src 'self' about: blob: data:", policy)
                script_policy = policy.split('script-src ', 1)[1].split(';', 1)[0]
                self.assertNotIn("'self'", script_policy)
                self.assertNotIn('unsafe-eval', policy)
                positions = []
                for name in ('dictation_draft.js', 'dictation.js', 'dictation_view.js'):
                    module = (ROOT / 'src/web' / name).read_bytes()
                    self.assertEqual(body.count(module), 1, name)
                    positions.append(body.index(module))
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(body.count((ROOT / 'src/web/dictation.css').read_bytes()), 1)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['X-Frame-Options'], 'DENY')
            status, classic, _ = request(port, '/?ui=classic')
            self.assertEqual(status, 200)
            for name in ('dictation_draft.js', 'dictation.js', 'dictation_view.js', 'dictation.css'):
                self.assertNotIn((ROOT / 'src/web' / name).read_bytes(), classic)
            status, zipped, headers = request(port, '/?demo', headers=[('Accept-Encoding', 'gzip')])
            self.assertEqual((status, headers.get('Content-Encoding')), (200, 'gzip'))
            self.assertEqual(gzip.decompress(zipped).count(b'<script nonce='), 1)

    def assert_fixed_routes(self, mutate=lambda source: source):
        with running(mutate) as (_, port):
            status, body, headers = request(port, '/dictation-worklet.js')
            self.assertEqual(status, 200)
            self.assertEqual(body, (ROOT / 'src/web/dictation_worklet.js').read_bytes())
            self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
            self.assertEqual(headers['Content-Security-Policy'], "default-src 'none'")
            for route in ('/dictation-worklet.js?', '/dictation-worklet.js?other=1', '/dictation-worklet.js#fragment',
                          '/dictation-help?', '/dictation-help?other=1', '/dictation-help#fragment'):
                self.assertEqual(request(port, route)[0], 404, route)
            status, body, headers = request(port, '/dictation-help')
            self.assertEqual(status, 200)
            self.assertEqual(body, (ROOT / 'docs/DICTATION.md').read_bytes())
            self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
            self.assertEqual(headers['Content-Security-Policy'], "default-src 'none'; frame-ancestors 'none'")

    def test_exact_worklet_and_local_help_routes(self):
        self.assert_fixed_routes()

    def assert_all_method_auth(self, mutate=lambda source: source):
        with running(mutate) as (scope, port):
            scope['DICTATION_PROXY'] = Untouchable()
            for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
                for headers in ([], [('X-Live-Room-Key', 'wrong')],
                                [('X-Live-Room-Key', OWNER), ('X-Live-Room-Key', OWNER)],
                                [('X-Live-Room-Key', OWNER), ('Origin', 'https://fictional.invalid')]):
                    status, _, _ = request(port, '/api/dictation/status', method, headers)
                    self.assertEqual(status, 403, (method, len(headers)))
                if method not in ('GET', 'POST'):
                    self.assertEqual(request(port, '/api/dictation/status', method, [('X-Live-Room-Key', OWNER)])[0], 404)
            # A declared body that is never supplied proves authentication happened first.
            status, _, _ = request(port, '/api/dictation/start', 'POST',
                                   [('X-Live-Room-Key', 'wrong'), ('Content-Length', '64000'),
                                    ('Content-Type', 'application/octet-stream')])
            self.assertEqual(status, 403)

    def test_proxy_owner_authentication_covers_every_routed_method(self):
        self.assert_all_method_auth()

    def test_authenticated_dispatch_uses_only_the_dictation_boundary(self):
        with running() as (scope, port):
            service = RoutingSpy(); scope['DICTATION_PROXY'] = service
            headers = [('X-Live-Room-Key', OWNER)]
            self.assertEqual(request(port, '/api/dictation/status', headers=headers)[0], 200)
            self.assertEqual(service.calls, [('status', None, None, None, False)])
            body = b'{"intervalSeconds":0.25}'
            code, _, _ = request(port, '/api/dictation/start', 'POST', headers +
                                 [('Content-Type', 'application/json'), ('Content-Length', str(len(body)))], body)
            self.assertEqual(code, 200)
            self.assertEqual(service.calls[-1], ('start', {'intervalSeconds': .25}, None, None, False))
            count = len(service.calls)
            for route in ('/api/dictation/status?', '/api/dictation/status?port=1', '/api/dictation/%73tatus'):
                self.assertEqual(request(port, route, headers=headers)[0], 400)
            self.assertEqual(len(service.calls), count)

    def test_demo_and_disabled_routes_never_read_or_contact_a_helper(self):
        with running() as (scope, port):
            for enabled, built_demo, server_demo in ((False, False, False), (True, True, False), (True, False, True)):
                with self.subTest(enabled=enabled, built_demo=built_demo, server_demo=server_demo):
                    scope['DICTATION_PROXY'] = dictation_proxy.DictationProxy('fictional-unused-home', enabled=enabled, demo=built_demo)
                    with mock.patch.dict(os.environ, {'WATCH_DEMO': '1' if server_demo else '0'}), \
                            mock.patch.object(dictation_proxy, '_read_helper_key', side_effect=AssertionError('Read helper capability')), \
                            mock.patch.object(dictation_proxy.DictationProxy, '_forward', side_effect=AssertionError('Contacted helper')):
                        headers = [('X-Live-Room-Key', OWNER)]
                        status, body, _ = request(port, '/api/dictation/status', headers=headers)
                        self.assertEqual(status, 200)
                        self.assertEqual(json.loads(body)['state'], 'disabled' if built_demo or server_demo else 'not_installed')
                        for action in ('start', 'audio', 'events', 'stop', 'cancel', 'reset'):
                            status, _, _ = request(port, '/api/dictation/' + action, 'POST',
                                                   headers + [('Content-Length', '64000'), ('Content-Type', 'application/octet-stream')])
                            self.assertEqual(status, 503, action)

    def test_router_and_exact_asset_guards_have_causal_checks(self):
        old = "        if self.path.startswith('/api/dictation/'):\n            return self.dictation_http()\n        if self.path.startswith('/api/attachments/'):\n            return self.attachments_http()\n        self.send_error(501, \"Unsupported method\")"
        self.assertIn(old, SERVER_SOURCE)
        with self.assertRaises(AssertionError):
            self.assert_all_method_auth(lambda source: source.replace(old, '        self.send_error(501, "Unsupported method")', 1))
        old = "            if self.path != '/dictation-worklet.js':\n                return self.send(404, b'not found')\n"
        self.assertIn(old, SERVER_SOURCE)
        with self.assertRaises(AssertionError):
            self.assert_fixed_routes(lambda source: source.replace(old, '', 1))


if __name__ == '__main__':
    unittest.main()
