"""Mutual, request-bound HMAC authentication for the private loopback helper.

The long-lived file capability never appears on the wire. The client verifies a
server proof before building its request proof. Challenge and request use separate
connections; this authenticates requests, not connection-bound confidentiality.
"""
import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import threading
import time

AUTH = 'hmac-sha256-v1'
TTL = 10
HEADER_NAMES = ('X-Dictation-Client-Nonce', 'X-Dictation-Server-Nonce',
                'X-Dictation-Content-SHA256', 'X-Dictation-Proof')
HEX64 = re.compile(r'[0-9a-f]{64}')
SERVER_NONCE = re.compile(r'[0-9a-f]{112}')


class AuthenticationError(ValueError):
    pass


def sign(key, *fields):
    data = ('motet-dictation-hmac-v1\n' + '\n'.join(fields)).encode('ascii')
    return hmac.new(bytes.fromhex(key), data, hashlib.sha256).hexdigest()


class Authenticator:
    def __init__(self, key, port, clock=time.monotonic):
        if type(key) is not str or not HEX64.fullmatch(key) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('A valid private capability and bound loopback port are required')
        self.key, self.clock = key, clock
        self.authority = '127.0.0.1:%d' % port
        self.instance = secrets.token_hex(32)
        self.used = {}
        self.lock = threading.Lock()

    def challenge(self, client):
        if type(client) is not str or not HEX64.fullmatch(client):
            raise AuthenticationError('Invalid client nonce')
        nonce = self.instance + ('%016x' % int(self.clock() * 1000)) + secrets.token_hex(16)
        return {'schemaVersion': 1, 'authentication': AUTH, 'clientNonce': client,
                'serverNonce': nonce, 'serverProof': sign(self.key, 'server', self.authority, client, nonce)}

    def verify(self, headers, method, path):
        values = [headers.get_all(name, []) for name in HEADER_NAMES]
        if any(len(value) != 1 for value in values) or headers.get_all('X-Dictation-Key', []):
            return None
        client, nonce, digest, proof = [value[0] for value in values]
        if any(type(value) is not str for value in (client, nonce, digest, proof)):
            return None
        if (not HEX64.fullmatch(client) or not SERVER_NONCE.fullmatch(nonce)
                or not HEX64.fullmatch(digest) or not HEX64.fullmatch(proof)
                or nonce[:64] != self.instance):
            return None
        try:
            semantic = []
            for name in ('Content-Type', 'Content-Length', 'X-Dictation-Session', 'X-Dictation-Sequence'):
                found = headers.get_all(name, [])
                if len(found) > 1 or found and type(found[0]) is not str:
                    return None
                semantic.append(found[0] if found else ('0' if name == 'Content-Length' else ''))
            semantic[0] = semantic[0].lower().replace(' ', '')
            expected = sign(self.key, 'request', self.authority, client, nonce, method, path, digest, *semantic)
        except (ValueError, UnicodeError):
            return None
        if not hmac.compare_digest(proof, expected):
            return None
        now = self.clock()
        expiry = int(nonce[64:80], 16) / 1000 + TTL
        if not now <= expiry <= now + TTL + 1:
            return None
        with self.lock:
            self.used = {token: until for token, until in self.used.items() if until >= now}
            token = client + nonce
            if token in self.used:
                return None
            if len(self.used) >= 1024:
                raise OverflowError('Authentication replay fence is full')
            self.used[token] = expiry
        return digest


def wire_json(port, method, path, body=None, headers=None, deadline=None, limit=5 * 1024 * 1024):
    deadline = time.monotonic() + 2 if deadline is None else deadline
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=remaining)
    response = None
    timer = None
    try:
        connection.connect()
        transport = connection.sock
        def expired():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(max(.001, deadline - time.monotonic()), expired)
        timer.daemon = True
        timer.start()
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        types = response.headers.get_all('Content-Type', [])
        lengths = response.headers.get_all('Content-Length', [])
        if (len(types) != 1 or type(types[0]) is not str or types[0].lower().replace(' ', '') not in ('application/json', 'application/json;charset=utf-8')
                or response.headers.get_all('Transfer-Encoding', []) or response.headers.get_all('Content-Encoding', [])
                or len(lengths) != 1 or type(lengths[0]) is not str or not re.fullmatch(r'[0-9]{1,8}', lengths[0])
                or not 1 <= int(lengths[0]) <= limit):
            raise ValueError('Invalid helper framing')
        raw = bytearray()
        while len(raw) < int(lengths[0]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            # Buffered response reads still use the same absolute deadline.
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, int(lengths[0]) - len(raw)))
            if not chunk:
                raise ValueError('Incomplete helper response')
            raw.extend(chunk)
        return response.status, bytes(raw)
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def request_headers(key, port, method, path, body=None, deadline=None, extra_headers=None):
    """No credential/proof is sent to a listener lacking the server's HMAC."""
    deadline = time.monotonic() + 2 if deadline is None else deadline
    client = secrets.token_hex(32)
    status, raw = wire_json(port, 'GET', '/v1/challenge', headers={
        'X-Dictation-Client-Nonce': client, 'Connection': 'close'}, deadline=deadline, limit=2048)
    try:
        def unique(items):
            result = {}
            for name, value in items:
                if name in result:
                    raise ValueError('Repeated challenge field')
                result[name] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if (status != 200 or type(value) is not dict
                or set(value) != {'schemaVersion', 'authentication', 'clientNonce', 'serverNonce', 'serverProof'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['authentication'] != AUTH
                or value['clientNonce'] != client or type(value['serverNonce']) is not str
                or not SERVER_NONCE.fullmatch(value['serverNonce']) or type(value['serverProof']) is not str
                or not HEX64.fullmatch(value['serverProof'])
                or not hmac.compare_digest(value['serverProof'], sign(key, 'server', '127.0.0.1:%d' % port, client, value['serverNonce']))):
            raise AuthenticationError('The listener did not authenticate as the helper')
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise AuthenticationError('The listener did not authenticate as the helper') from None
    digest = hashlib.sha256(body or b'').hexdigest()
    extra = {name.lower(): str(value) for name, value in (extra_headers or {}).items()}
    semantic = [extra.get('content-type', '').lower().replace(' ', ''),
                extra.get('content-length', str(len(body or b''))),
                extra.get('x-dictation-session', ''), extra.get('x-dictation-sequence', '')]
    return dict(zip(HEADER_NAMES, (client, value['serverNonce'], digest,
                                  sign(key, 'request', '127.0.0.1:%d' % port, client, value['serverNonce'], method, path, digest, *semantic))))
