#!/usr/bin/env python3
"""Separately launched, owner-authenticated loopback dictation helper."""
import argparse
import hashlib
import http.client
import hmac
import io
from email.parser import BytesHeaderParser
import queue
import selectors
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

# The shared wire protocol lives with the standard-library Motet adapter.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from dictation_auth import Authenticator, AuthenticationError, request_headers, wire_json

PROTOCOL = 1


class PrefixedInput(io.RawIOBase):
    def __init__(self, connection, prefix):
        self.prefix = io.BytesIO(prefix)
        self.source = connection.makefile('rb', buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.prefix.read(len(buffer))
        if chunk:
            buffer[:len(chunk)] = chunk
            return len(chunk)
        return self.source.readinto(buffer)

    def close(self):
        self.prefix.close(); self.source.close(); super().close()


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    HEADER_SECONDS = 2.0
    MAX_PENDING = 64

    def __init__(self, port, key, service):
        if type(port) is not int or port == 8793 or not 0 <= port <= 65535:
            raise ValueError('Use a separate helper port; the Motet port is reserved.')
        if not authenticates(key, key) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('A private helper credential is required.')
        self.key, self.service = key, service
        self.slots = threading.BoundedSemaphore(8)
        self.public_slots = threading.BoundedSemaphore(4)
        self.stopped = threading.Event()
        self.incoming = queue.Queue(maxsize=self.MAX_PENDING)
        self.admitted = {}
        self.admission_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), HelperHandler)
        self.authentication = Authenticator(key, self.server_port)
        self.wake_read, self.wake_write = socket.socketpair()
        self.wake_read.setblocking(False); self.wake_write.setblocking(False)
        self.admission_thread = threading.Thread(target=self._admit, daemon=True)
        self.admission_thread.start()
        threading.Thread(target=self._expire, daemon=True).start()

    def _expire(self):
        while not self.stopped.wait(2):
            self.service.expire()

    def process_request(self, request, address):
        # Header sockets occupy one bounded selector, never authenticated workers.
        request.setblocking(False)
        item = (request, address, time.monotonic())
        try:
            self.incoming.put_nowait(item)
        except queue.Full:
            try:
                old, _, _ = self.incoming.get_nowait(); self.shutdown_request(old)
            except queue.Empty:
                pass
            try:
                self.incoming.put_nowait(item)
            except queue.Full:
                self.shutdown_request(request)
        try:
            self.wake_write.send(b'x')
        except OSError:
            pass

    def _reject(self, request, code=403):
        try:
            body = json.dumps({'schemaVersion': 1, 'error': {
                'code': 'busy' if code == 503 else 'forbidden',
                'message': 'The helper is busy.' if code == 503 else 'Helper authentication required.'}}).encode()
            request.sendall(('HTTP/1.1 %d Refused\r\nContent-Type: application/json\r\nContent-Length: %d\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src \'none\'\r\nConnection: close\r\n\r\n' % (code, len(body))).encode() + body)
        except OSError:
            pass
        self.shutdown_request(request)

    def _admit_headers(self, request, address, data):
        try:
            line, rest = data.split(b'\r\n', 1)
            method, path, version = line.decode('ascii').split(' ')
            if version not in ('HTTP/1.1', 'HTTP/1.0') or len(path) > 2048:
                raise ValueError()
            headers = BytesHeaderParser().parsebytes(rest.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n')
            local = ['127.0.0.1:%d' % self.server_port, 'localhost:%d' % self.server_port]
            hosts, origins, sites = (headers.get_all(name, []) for name in ('Host', 'Origin', 'Sec-Fetch-Site'))
            if (len(list(headers.keys())) > 64 or len(hosts) != 1 or hosts[0] not in local
                    or len(origins) > 1 or origins and origins[0] not in ['http://' + host for host in local]
                    or len(sites) > 1 or sites and sites[0] not in ('none', 'same-origin')
                    or headers.get_all('X-Dictation-Key', [])):
                raise ValueError()
            challenge = method == 'GET' and path == '/v1/challenge'
            if challenge:
                clients = headers.get_all('X-Dictation-Client-Nonce', [])
                if (len(clients) != 1 or type(clients[0]) is not str or not re.fullmatch(r'[0-9a-f]{64}', clients[0])
                        or headers.get_all('Transfer-Encoding', [])
                        or len(headers.get_all('Content-Length', [])) > 1
                        or any(value != '0' for value in headers.get_all('Content-Length', []))):
                    raise ValueError()
                digest, slot = None, self.public_slots
            else:
                digest = self.authentication.verify(headers, method, path)
                if digest is None:
                    raise ValueError()
                slot = self.slots
            if not slot.acquire(blocking=False):
                return self._reject(request, 503)
            request.setblocking(True)
            with self.admission_lock:
                self.admitted[request] = (data, digest, slot, challenge)
            try:
                super().process_request(request, address)
            except Exception:
                with self.admission_lock:
                    self.admitted.pop(request, None)
                slot.release(); self.shutdown_request(request)
        except Exception:
            # Malformed untrusted headers cannot terminate the single admission loop.
            self._reject(request)

    def _admit(self):
        pending = {}
        selector = selectors.DefaultSelector()
        selector.register(self.wake_read, selectors.EVENT_READ)
        def drop(request):
            try:
                selector.unregister(request)
            except (KeyError, ValueError):
                pass
            pending.pop(request, None); self.shutdown_request(request)
        try:
            while not self.stopped.is_set():
                while True:
                    try:
                        request, address, began = self.incoming.get_nowait()
                    except queue.Empty:
                        break
                    if len(pending) >= self.MAX_PENDING:
                        drop(next(iter(pending)))
                    pending[request] = [address, began, bytearray()]
                    selector.register(request, selectors.EVENT_READ)
                now = time.monotonic()
                for request, (_, began, _) in list(pending.items()):
                    if now - began >= self.HEADER_SECONDS:
                        drop(request)
                for key, _ in selector.select(.05):
                    request = key.fileobj
                    if request is self.wake_read:
                        try:
                            while self.wake_read.recv(4096):
                                pass
                        except OSError:
                            pass
                        continue
                    if request not in pending:
                        continue
                    address, began, data = pending[request]
                    try:
                        chunk = request.recv(32769 - len(data))
                        if not chunk:
                            drop(request); continue
                        data.extend(chunk)
                        end = data.find(b'\r\n\r\n')
                        if end < 0 and len(data) >= 32768 or time.monotonic() - began >= self.HEADER_SECONDS:
                            drop(request); continue
                        if end >= 0:
                            selector.unregister(request); pending.pop(request)
                            self._admit_headers(request, address, bytes(data))
                    except Exception:
                        drop(request)
        finally:
            for request in list(pending):
                drop(request)
            while True:
                try:
                    request, _, _ = self.incoming.get_nowait(); self.shutdown_request(request)
                except queue.Empty:
                    break
            selector.close()

    def process_request_thread(self, request, address):
        with self.admission_lock:
            record = self.admitted.get(request)
        try:
            super().process_request_thread(request, address)
        finally:
            with self.admission_lock:
                self.admitted.pop(request, None)
            if record is not None:
                record[2].release()

    def handle_error(self, request, address):
        pass

    def server_close(self):
        self.stopped.set()
        wake_write = getattr(self, 'wake_write', None)
        if wake_write is not None:
            try:
                wake_write.send(b'x')
            except OSError:
                pass
        admission = getattr(self, 'admission_thread', None)
        if admission is not None:
            admission.join(2)
        for name in ('wake_read', 'wake_write'):
            stream = getattr(self, name, None)
            if stream is not None:
                stream.close()
        self.service.close()
        super().server_close()


class HelperHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LocalDictation'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(3)
        with self.server.admission_lock:
            prefix, self.expected_digest, _, self.challenge_request = self.server.admitted[self.connection]
        self.rfile.close()
        self.rfile = io.BufferedReader(PrefixedInput(self.connection, prefix))

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
        return self.expected_digest is not None

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
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), self.expected_digest):
            raise ServiceError('forbidden', 'The request body proof did not match.', 403)
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
        if self.challenge_request:
            return self.reply(200, self.server.authentication.challenge(self.values('X-Dictation-Client-Nonce')[0]))
        # Admission verified the single-use request proof before any body or worker.
        if not self.authenticate():
            return self.error(403, 'forbidden', 'The helper owner credential is required.')
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                    or any(char in self.path for char in ('?', '#', '%'))):
                raise ServiceError('invalid_request', 'Use a canonical helper route.')
            path = parsed.path
            if self.command == 'GET':
                if self.expected_digest != hashlib.sha256(b'').hexdigest():
                    raise ServiceError('forbidden', 'The request body proof did not match.', 403)
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
    do_HEAD = dispatch
    do_PATCH = dispatch
    do_TRACE = dispatch


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
    except (StateError, OSError, UnicodeError, ValueError):
        return {'schemaVersion': 1, 'state': 'not_installed', 'code': 'helper_state_missing_or_unsafe'}
    try:
        deadline = time.monotonic() + 2
        headers = request_headers(key, port, 'GET', '/v1/status', deadline=deadline)
        status, raw = wire_json(port, 'GET', '/v1/status', headers=headers, deadline=deadline, limit=16384)
        value = json.loads(raw)
        if status == 403:
            raise AuthenticationError()
        if type(value) is not dict or value.get('schemaVersion') != 1 or 'state' not in value:
            raise ValueError()
        return value
    except AuthenticationError:
        return {'schemaVersion': 1, 'state': 'failed', 'code': 'authentication_failed'}
    except (http.client.HTTPException, ValueError):
        return {'schemaVersion': 1, 'state': 'failed', 'code': 'invalid_response'}
    except (OSError, urllib.error.URLError):
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
