#!/usr/bin/env python3
"""Separately launched, owner-authenticated loopback dictation helper."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.dictation.assets import AssetsError, MODEL_BYTES, download_model, model_info, validate_model
from integrations.dictation.service import DictationService, ServiceError
from integrations.dictation.state import PrivateState, StateError, authenticates

PROTOCOL = 1


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, port, key, service):
        if type(port) is not int or port == 8793 or not 0 <= port <= 65535:
            raise ValueError('Use a separate helper port; the Motet port is reserved.')
        if not authenticates(key, key):
            raise ValueError('A private helper credential is required.')
        self.key, self.service = key, service
        self.slots = threading.BoundedSemaphore(8)
        self.stopped = threading.Event()
        super().__init__(('127.0.0.1', port), HelperHandler)
        threading.Thread(target=self._expire, daemon=True).start()

    def _expire(self):
        while not self.stopped.wait(2):
            self.service.expire()

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Never log request bodies, transcript text or authentication headers.

    def server_close(self):
        self.stopped.set()
        self.service.close()
        super().server_close()


class HelperHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LocalDictation'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        self.close_connection = True
        body = json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        for name, value in [('Content-Type', 'application/json; charset=utf-8'),
                            ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                            ('Connection', 'close'), ('X-Content-Type-Options', 'nosniff'),
                            ('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'"),
                            ('Referrer-Policy', 'no-referrer')]:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def error(self, status, code, message):
        return self.reply(status, {'schemaVersion': PROTOCOL, 'error': {'code': code, 'message': message}})

    def values(self, name):
        return self.headers.get_all(name, [])

    def authenticate(self):
        local = ['127.0.0.1:%d' % self.server.server_port, 'localhost:%d' % self.server.server_port]
        hosts, origins, keys = self.values('Host'), self.values('Origin'), self.values('X-Dictation-Key')
        sites = self.values('Sec-Fetch-Site')
        return (len(hosts) == 1 and hosts[0] in local and len(origins) <= 1
                and (not origins or origins[0] in ['http://' + host for host in local])
                and len(keys) == 1 and authenticates(keys[0], self.server.key)
                and len(sites) <= 1 and (not sites or sites[0] in ('none', 'same-origin')))

    def body(self, binary):
        lengths, types = self.values('Content-Length'), self.values('Content-Type')
        if (self.values('Transfer-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[0-9]{1,6}', lengths[0]) or int(lengths[0]) < 1):
            raise ServiceError('invalid_request', 'A bounded request body is required.')
        length = int(lengths[0])
        if length > (64000 if binary else 4096):
            raise ServiceError('body_too_large', 'The dictation packet is too large.', 413)
        acceptable = ('application/octet-stream',) if binary else ('application/json', 'application/json;charset=utf-8')
        if len(types) != 1 or types[0].lower().replace(' ', '') not in acceptable:
            raise ServiceError('invalid_request', 'Use the documented request content type.')
        raw = bytearray()
        deadline = time.monotonic() + 2
        while len(raw) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError('invalid_request', 'The audio request was incomplete.')
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(length - len(raw))
            if not chunk:
                raise ServiceError('invalid_request', 'The request body was incomplete.')
            raw.extend(chunk)
        if binary:
            return bytes(raw)

        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value

        def constant(_):
            raise ValueError()

        def finite(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number

        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)
        if type(data) is not dict:
            raise ValueError()
        return data

    def dispatch(self):
        self.close_connection = True
        # Authenticate before body consumption, service access or path-specific state.
        if not self.authenticate():
            return self.error(403, 'forbidden', 'The helper owner credential is required.')
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                    or any(char in self.path for char in ('?', '#', '%'))):
                raise ServiceError('invalid_request', 'Use a canonical helper route.')
            path = parsed.path
            if self.command == 'GET':
                if self.values('Transfer-Encoding') or len(self.values('Content-Length')) > 1 or any(
                        value != '0' for value in self.values('Content-Length')):
                    raise ServiceError('invalid_request', 'GET requests cannot contain a body.')
                if path == '/v1/status':
                    return self.reply(200, self.server.service.status())
                raise ServiceError('unknown_route', 'Unknown helper route.', 404)
            if self.command != 'POST' or path not in (
                    '/v1/start', '/v1/audio', '/v1/events', '/v1/stop', '/v1/cancel', '/v1/reset'):
                raise ServiceError('unknown_route', 'Unknown helper route.', 404)
            data = self.body(binary=path == '/v1/audio')
            service = self.server.service
            if path == '/v1/audio':
                sessions, sequences = self.values('X-Dictation-Session'), self.values('X-Dictation-Sequence')
                if len(sessions) != 1 or len(sequences) != 1 or not re.fullmatch(r'[0-9]{1,6}', sequences[0]):
                    raise ServiceError('invalid_request', 'Audio needs one session and sequence header.')
                result = service.audio(sessions[0], int(sequences[0]), data)
            elif path == '/v1/start':
                if set(data) - {'intervalSeconds'}:
                    raise ValueError()
                result = service.start(data.get('intervalSeconds', .25))
            elif path == '/v1/events':
                if set(data) != {'sessionId', 'after'}:
                    raise ValueError()
                result = service.events(data['sessionId'], data['after'])
            else:
                if set(data) != {'sessionId'}:
                    raise ValueError()
                result = service.control(data['sessionId'], path.rsplit('/', 1)[-1])
            return self.reply(200, dict(result, schemaVersion=PROTOCOL))
        except ServiceError as exc:
            return self.error(exc.status, exc.code, exc.message)
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, OSError):
            return self.error(400, 'invalid_request', 'The helper request is invalid or incomplete.')
        except Exception:
            return self.error(503, 'unavailable', 'The local helper could not complete this request.')

    do_GET = dispatch
    do_POST = dispatch
    do_OPTIONS = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch


def make_service(python, model):
    # Only the child imports Moonshine. The supervisor remains stdlib-only.
    from integrations.dictation.runtime import Runtime

    def installed():
        if not python.is_file():
            return 'dependency_missing'
        try:
            validate_model(model)
        except AssetsError:
            return 'model_missing_or_invalid'
        return None

    return DictationService(lambda: Runtime(python, model), installed)


def probe(home, port):
    try:
        key = PrivateState(home).key()
    except StateError:
        return {'schemaVersion': 1, 'state': 'not_installed', 'code': 'helper_state_missing_or_unsafe'}
    request = urllib.request.Request('http://127.0.0.1:%d/v1/status' % port,
                                     headers={'X-Dictation-Key': key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            value = json.loads(raw)
            if type(value) is not dict or value.get('schemaVersion') != 1 or 'state' not in value:
                raise ValueError()
            return value
    except urllib.error.HTTPError as exc:
        return {'schemaVersion': 1, 'state': 'failed', 'code': 'authentication_failed' if exc.code == 403 else 'invalid_response'}
    except (OSError, ValueError, urllib.error.URLError):
        return {'schemaVersion': 1, 'state': 'not_running', 'code': 'connection_unavailable'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward the capability to a redirect target.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('model-info', 'download-model', 'serve', 'status'))
    parser.add_argument('--home', type=Path, default=Path.home(), help='Existing owner or isolated preview home')
    parser.add_argument('--port', type=int, default=18879, help='Separate loopback port; 8793 is refused')
    parser.add_argument('--python', type=Path, help='Optional runtime environment interpreter')
    parser.add_argument('--model', type=Path, help='Private directory containing the pinned English model')
    parser.add_argument('--accept-bytes', type=int, help='Explicitly confirm the displayed model download size')
    parser.add_argument('--demo', action='store_true', help='Report disabled without state, download or runtime access')
    args = parser.parse_args(argv)
    if args.demo:
        print(json.dumps({'schemaVersion': 1, 'state': 'disabled', 'code': 'demo'}))
        return 0
    if not 0 <= args.port <= 65535 or args.port == 8793 or args.command == 'status' and not args.port:
        parser.error('Use a separate valid helper port; 8793 is reserved.')
    state = PrivateState(args.home)
    model = args.model or state.root / 'model'
    python = args.python or state.root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    try:
        if args.command == 'model-info':
            print(json.dumps(model_info(), indent=2))
        elif args.command == 'download-model':
            if args.accept_bytes != MODEL_BYTES:
                parser.error('Run model-info first, then explicitly confirm its bytes with --accept-bytes.')
            with state.directory(create=True):
                result = download_model(model, args.accept_bytes,
                                        progress=lambda value: print(json.dumps(value), file=sys.stderr))
            print(json.dumps(result))
        elif args.command == 'status':
            print(json.dumps(probe(args.home, args.port)))
        else:
            key = state.key(create=True)
            service = make_service(python, model)
            server = HelperServer(args.port, key, service)
            service.load()
            def terminate(signum, frame):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, terminate)
            print('Local dictation helper on 127.0.0.1:%d; owner credential required.' % server.server_port, flush=True)
            try:
                server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (StateError, AssetsError, OSError, ValueError) as exc:
        # Exceptions from state/assets are intentionally generic and contain no token or user path.
        print(json.dumps({'schemaVersion': 1, 'state': 'failed', 'code': getattr(exc, 'code', 'unavailable')}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
