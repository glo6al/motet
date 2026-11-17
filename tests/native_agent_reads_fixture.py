"""Disposable loopback HTTP fixture for the native attachment read stage."""
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import sys
import socket
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_http import handle_http

PAGE_ADA = 'x:00000000-0000-4000-8000-000000000101'
PAGE_BRAM = 'x:00000000-0000-4000-8000-000000000102'


class ResponseFault:
    """Drop one exact HTTP acknowledgement before handling or after its effect."""
    def __init__(self):
        self.lock = threading.Lock()
        self.armed = None
        self.fired = threading.Event()
        self.release = threading.Event()
        self.status = None

    def arm(self, method, route, when):
        if when not in ('before', 'after', 'pause'):
            raise ValueError('Unknown fictional acknowledgement fault.')
        with self.lock:
            if self.armed is not None:
                raise ValueError('A fictional acknowledgement fault is already armed.')
            self.armed = (method, route, when)
            self.status = None
            self.fired.clear()
            self.release.clear()

    def take(self, method, route, when, status=None):
        with self.lock:
            if self.armed != (method, route, when):
                return False
            self.armed = None
            self.status = status
            self.fired.set()
            return True


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, handler):
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), handler)

    def process_request(self, request, client_address):
        with self.active_lock:
            self.active.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.active_lock:
                self.active.discard(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.active_lock:
                self.active.discard(request)

    def server_close(self):
        with self.active_lock:
            for request in self.active:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        super().server_close()


@contextmanager
def serve_native_reads(service, owner_key, *, extra_route=None, page=None):
    """Serve the actual attachment handler; deny process connections except to this fixture."""
    fault = ResponseFault()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        timeout = 2

        def log_message(self, *_args):
            pass

        def handle_attachment(self):
            self.close_connection = True
            if fault.take(self.command, self.path, 'pause') and not fault.release.wait(3):
                return
            pause = getattr(self.server, 'page_pause', None)
            if pause and self.headers.get('X-Live-Room-Key') == owner_key:
                pause(self.command, self.path)
            if not fault.take(self.command, self.path, 'before') and not (extra_route and extra_route(self)):
                handle_http(self, service, owner_key, self.server.server_port)

        def do_GET(self):
            if page and not urlsplit(self.path).path.startswith('/api/attachments/'):
                return page(self)
            return self.handle_attachment()

        def do_POST(self):
            if page and not urlsplit(self.path).path.startswith('/api/attachments/'):
                return page(self)
            return self.handle_attachment()

        do_PATCH = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = handle_attachment

        def send(self, status, body, content_type, *, extra=()):
            if fault.take(self.command, self.path, 'after', status):
                return
            rule = getattr(self.server, 'page_fault', None)
            if rule and self.path.startswith('/api/attachments/') and rule(self.command, self.path, status):
                return
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', content_type)
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

    with ExitStack() as stack:
        server = stack.enter_context(_LocalServer(Handler))
        server.outbound_blocked = 0
        server.fault = fault

        def check(address):
            if address != server.server_address:
                server.outbound_blocked += 1
                raise OSError('Non-fixture connections are disabled.')

        connect, connect_ex, create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

        def local_connect(sock, address):
            check(address)
            return connect(sock, address)

        def local_connect_ex(sock, address):
            check(address)
            return connect_ex(sock, address)

        def local_create(address, *args, **kwargs):
            check(address)
            return create(address, *args, **kwargs)

        stack.enter_context(patch.object(socket.socket, 'connect', local_connect))
        stack.enter_context(patch.object(socket.socket, 'connect_ex', local_connect_ex))
        stack.enter_context(patch.object(socket, 'create_connection', local_create))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
        thread.start()
        try:
            yield server
        finally:
            fault.release.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            if thread.is_alive():
                raise RuntimeError('Native read fixture did not stop.')


def main():
    import argparse
    import hashlib
    import signal
    import tempfile

    from attachments_native_reads import NativeAgentReadService
    from attachments_store import AttachmentStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page', action='store_true', help='Serve the real Stage 1 page with fictional agents')
    parser.add_argument('--saved-drafts', type=int, choices=range(9), default=0)
    parser.add_argument('--fault', choices=('none', 'lost-edit', 'lost-reserve', 'hold-upload'), default='none')
    args = parser.parse_args()
    if (args.saved_drafts or args.fault != 'none') and not args.page:
        parser.error('Saved draft and browser fault modes require --page')
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    key = 'fictional-native-browser-fixture-key'
    with tempfile.TemporaryDirectory(prefix='watch-native-browser-') as directory:
        store = AttachmentStore(Path(directory).resolve(), enabled=True, owner_epoch=hashlib.sha256(key.encode()).hexdigest())
        try:
            selections = [dict(mode='current', destinationId=destination, scopeId=scope, capabilityRevision='fictional-v1',
                               formats=['application/pdf', 'image/png', 'image/jpeg'],
                               limits={'fileBytes': 1024, 'draftBytes': 2048, 'fileCount': 3})
                          for destination, scope in (((PAGE_ADA if args.page else 'fictional-ada'), 'fictional-orchard'),
                                                     ((PAGE_BRAM if args.page else 'fictional-bram'), 'fictional-grove'))]
            service = NativeAgentReadService(store, selections=selections)
            try:
                if args.page:
                    for number in range(args.saved_drafts):
                        service.create_draft(dict(destinationId=PAGE_ADA, scopeId='fictional-orchard',
                                                  conversationId=None, modelId=None, capabilityRevision='fictional-v1'),
                                             'fictional-saved-' + str(number))
                page, fault = build_page(key, stopped, args.fault) if args.page else (None, None)
                with serve_native_reads(service, key, page=page) as server:
                    server.page_fault = fault
                    if args.fault == 'hold-upload':
                        server.page_pause = pause_first_upload(stopped)
                    suffix = '/?ui=workspace#k=' + key if args.page else ''
                    print('http://127.0.0.1:%d%s' % (server.server_port, suffix), flush=True)
                    stopped.wait()
                if server.outbound_blocked:
                    raise RuntimeError('An outbound connection was refused in the native fixture.')
            finally:
                service.close()
        finally:
            store.close()


def pause_first_upload(stopped):
    """Leave the first reservation and transfer visible long enough to inspect the page."""
    lock = threading.Lock()
    routes = {'POST': r'/api/attachments/drafts/[a-f0-9]{32}/upload-intents',
              'PUT': r'/api/attachments/drafts/[a-f0-9]{32}/uploads/[a-f0-9]{32}'}

    def pause(method, path):
        with lock:
            pattern = routes.get(method)
            if not pattern or not re.fullmatch(pattern, path):
                return
            del routes[method]
        stopped.wait(2)

    return pause


def build_page(key, stopped, fault_mode):
    """The only non-attachment actions are fictional page reads and an inert text acknowledgement."""
    import server

    agents = {PAGE_ADA: 'Fictional Ada', PAGE_BRAM: 'Fictional Bram'}
    records = []
    lock = threading.Lock()
    fired = False
    def fault(method, path, status):
        nonlocal fired
        expected = {'lost-edit': ('PATCH', r'/api/attachments/drafts/[a-f0-9]{32}'),
                    'lost-reserve': ('POST', r'/api/attachments/drafts/[a-f0-9]{32}/upload-intents')}.get(fault_mode)
        with lock:
            if fired or not expected or method != expected[0] or not re.fullmatch(expected[1], path) or status >= 300:
                return False
            fired = True
            return True

    def page(request):
        request.close_connection = True
        if request.headers.get('Host') != '127.0.0.1:%d' % request.server.server_port:
            return request.send(403, b'forbidden', 'text/plain')
        parsed = urlsplit(request.path)
        query = parse_qs(parsed.query)
        route = parsed.path
        def reply(value, status=200):
            return request.send(status, json.dumps(value).encode(), 'application/json')

        if request.command == 'GET' and route == '/':
            body, csp = server.page_with_nonce(native_agent_modules=True)
            return request.send(200, body, 'text/html; charset=utf-8', extra=(('Content-Security-Policy', csp),))
        if request.command == 'GET' and route.startswith('/static/') and route[8:] in server.STATIC:
            name = route[8:]
            return request.send(200, (Path(server.HERE) / 'web/static' / name).read_bytes(), server.STATIC[name])
        if request.command == 'GET' and route == '/check':
            return request.send(200 if query.get('k') == [key] else 403, b'', 'text/plain')
        if request.command == 'GET' and route == '/events' and query.get('k') == [key]:
            request.send_response(200)
            request.send_header('Content-Type', 'text/event-stream')
            request.send_header('Cache-Control', 'no-store')
            request.end_headers()
            hello = dict(kind='hello', agent='', snap=True, boot='fictional-native', seq=-1,
                         pkey='fictional-preview', window=1200, names={}, watch={}, win=False, prefixes=['c:', 'x:'])
            try:
                rows = [hello, *(dict(kind='agent', agent=agent, name=name, runtime='Codex', old=True, ts=0,
                                     snap=True, boot=hello['boot'], seq=-1) for agent, name in agents.items())]
                for row in rows:
                    request.wfile.write(('data: ' + json.dumps(row) + '\n\n').encode())
                request.wfile.flush()
                while not stopped.wait(2):
                    request.wfile.write(b': fictional fixture heartbeat\n\n')
                    request.wfile.flush()
            except OSError:
                pass
            return
        if request.headers.get('X-Live-Room-Key') != key:
            return request.send(403, b'forbidden', 'text/plain')
        if request.command == 'GET':
            if route == '/officer/model-selection':
                return reply(dict(schemaVersion=1, selected_model='@automatic', selection_available=True,
                                  attachments_enabled=False, documents_enabled=False))
            if route == '/agent-capabilities' and query.get('agent', [None])[0] in agents:
                agent = query['agent'][0]
                return reply(dict(agent=agent, actions=dict(queue=dict(mode='queue', available=True))))
            if route == '/agent-messages':
                return reply(dict(requests=[], controls=[], available=True))
            if route == '/fixture/metadata':
                with lock:
                    return reply(dict(offline=True, faultFired=fired, ordinaryMessages=list(records),
                                      outboundBlocked=request.server.outbound_blocked))
        if request.command == 'POST' and route == '/agent-message':
            count = int(request.headers.get('Content-Length', '0'))
            if not 0 < count <= 4096:
                return reply({'error': 'Invalid fictional text message'}, 400)
            value = json.loads(request.rfile.read(count))
            if set(value) != {'agent', 'question', 'request_id'} or value['agent'] not in agents:
                return reply({'error': 'Invalid fictional text message'}, 400)
            with lock:
                records.append(value['question'])
            return reply({'state': 'not_submitted', 'error': 'Offline fixture. No agent was contacted.'})
        return reply({'available': False, 'error': 'Unavailable in the offline fixture'}, 503)

    return page, fault


if __name__ == '__main__':
    main()
