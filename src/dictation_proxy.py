"""Owner-only bridge to a separately configured loopback dictation helper.

Construction and disabled/demo requests perform no filesystem or network I/O.
The dedicated helper capability is read only by this server and never returned.
"""
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
import urllib.parse
import private_storage as ps
from dictation_auth import AuthenticationError, BusyError, REQUEST_CAPACITY, request_headers, wire_json

PROTOCOL = 1
MAX_JSON = 4096
MAX_PCM = 64000
MAX_RESPONSE = 5 * 1024 * 1024
HELPER_TIMEOUT = 12
SESSION = re.compile(r'[0-9a-f]{48}')
ROUTES = frozenset(('status', 'start', 'audio', 'events', 'stop', 'cancel', 'reset'))
STATES = frozenset(('not_installed', 'loading', 'ready', 'listening', 'finishing', 'failed'))
SAFE_CODES = frozenset((
    'dependency_missing', 'runtime_version', 'model_error', 'model_missing_or_invalid',
    'worker_failed', 'worker_timeout', 'protocol_error', 'recognition_failed',
    'invalid_request', 'body_too_large', 'unknown_route', 'invalid_interval',
    'busy', 'not_running', 'unknown_session', 'ended', 'invalid_sequence',
    'invalid_audio', 'out_of_order', 'take_limit', 'invalid_control',
    'invalid_cursor', 'events_expired', 'disconnected', 'loading', 'unavailable',
))
ERROR_MESSAGES = {
    'forbidden': 'Owner authentication is required.',
    'invalid_request': 'The dictation request is invalid or incomplete.',
    'unknown_route': 'Unknown dictation route.',
    'body_too_large': 'The dictation packet is too large.',
    'not_configured': 'Local dictation has not been configured.',
    'demo': 'Dictation is unavailable in the demo.',
    'not_installed': 'Set up the optional local dictation helper first.',
    'not_running': 'The local dictation helper is not running. Start it and try again.',
    'invalid_response': 'The local dictation helper returned an invalid response.',
    'helper_authentication_failed': 'The local dictation helper credential was refused.',
    'busy': 'Another dictation request is still running. Try again shortly.',
    'events_expired': 'Transcript events expired. Keep the visible draft and start a new take.',
    'out_of_order': 'The audio sequence changed. Keep the visible draft and start a new take.',
    'ended': 'This dictation take has ended.',
    'take_limit': 'The three-minute dictation limit was reached. Start a new take.',
    'unknown_session': 'This dictation session is unavailable. Start a new take.',
}


class ProxyError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(ERROR_MESSAGES.get(code, 'Local dictation could not complete this request. Keep the visible draft.'))


def _error(code):
    return {'schemaVersion': PROTOCOL, 'error': {
        'code': code, 'message': ERROR_MESSAGES.get(code, 'Local dictation could not complete this request. Keep the visible draft.')}}


def _availability(state, code):
    return {'schemaVersion': PROTOCOL, 'state': state, 'code': code}


def _integer(value, low=0, high=9007199254740991):
    return type(value) is int and low <= value <= high


def _number(value, high=1e12):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= high


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(_):
    raise ValueError()


def _float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError()
    return value


def _int(raw):
    if len(raw) > 16:
        raise ValueError()
    return int(raw)


def _json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=_constant, parse_float=_float, parse_int=_int)


def _read_helper_key_windows(home):
    """Windows: the same read through private_storage's no-link walk. The resolved home is held, then
    .live-room, dictation and the credential are each opened by handle inside the folder above (no link or
    junction followed), each checked private on its handle; held folders cannot be renamed while open, so no name
    re-check is needed afterwards. Never creates or repairs state."""
    if not ps.folder_walk_available():
        raise ValueError()
    anchor = Path(home).resolve(strict=True)
    prefix = b'X-Dictation-Key: '
    expected = len(prefix) + 64 + 1
    with ps.open_folder(str(anchor)) as held, held.folder('.live-room', private=True) as live, \
            live.folder('dictation', private=True) as state:
        credential = state.open('header', single_link=True, private=True)
        try:
            if os.fstat(credential).st_size != expected:
                raise ValueError()
            raw = os.read(credential, expected + 1)
        finally:
            os.close(credential)
    if (len(raw) != expected or not raw.startswith(prefix) or not raw.endswith(b'\n')
            or not re.fullmatch(b'[0-9a-f]{64}', raw[len(prefix):-1])):
        raise ValueError()
    return raw[len(prefix):-1].decode('ascii')


def _read_helper_key(home):
    """Read through held directory descriptors; never create or repair state."""
    if ps.WIN:
        return _read_helper_key_windows(home)
    if os.name != 'posix':
        raise ValueError()
    descriptors = []
    links = []
    try:
        # Resolve only the explicitly supplied home, including platform aliases.
        # Every helper-owned descendant is opened relative to a held parent.
        anchor = Path(home).resolve(strict=True)
        fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        anchor_info = os.fstat(fd)
        for component in ('.live-room', 'dictation'):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(child)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError()
            links.append((fd, component, child))
            fd = child
        credential = os.open('header', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(credential)
        info = os.fstat(credential)
        prefix = b'X-Dictation-Key: '
        expected = len(prefix) + 64 + 1
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size != expected):
            raise ValueError()
        raw = os.read(credential, expected + 1)
        if (len(raw) != expected or not raw.startswith(prefix) or not raw.endswith(b'\n')
                or not re.fullmatch(b'[0-9a-f]{64}', raw[len(prefix):-1])):
            raise ValueError()
        links.append((fd, 'header', credential))
        for parent, name, child in links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError()
        current_anchor = os.stat(str(anchor), follow_symlinks=False)
        if (current_anchor.st_dev, current_anchor.st_ino) != (anchor_info.st_dev, anchor_info.st_ino):
            raise ValueError()
        return raw[len(prefix):-1].decode('ascii')
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _safe_status(value):
    state = value.get('state')
    if state not in STATES:
        raise ValueError()
    code = value.get('code')
    result = {'schemaVersion': PROTOCOL, 'state': state,
              'code': code if code in SAFE_CODES else ('unavailable' if code is not None else None)}
    if value.get('loadMs') is None:
        result['loadMs'] = None
    elif _number(value.get('loadMs')):
        result['loadMs'] = value['loadMs']
    else:
        raise ValueError()
    if value.get('microphone') == 'client_owned':
        result['microphone'] = 'client_owned'
    if value.get('audioFormat') == {'sampleRate': 16000, 'channels': 1, 'encoding': 'float32le'}:
        result['audioFormat'] = {'sampleRate': 16000, 'channels': 1, 'encoding': 'float32le'}
    for name, expected in (('maxPacketBytes', MAX_PCM), ('maxTakeSeconds', 180)):
        if value.get(name) == expected and type(value.get(name)) is int:
            result[name] = expected
    return result


def _session(value):
    return type(value) is str and SESSION.fullmatch(value) is not None


def _safe_success(action, value):
    if type(value) is not dict or type(value.get('schemaVersion')) is not int or value.get('schemaVersion') != PROTOCOL:
        raise ValueError()
    if action == 'status':
        return _safe_status(value)
    epoch = value.get('epoch')
    if not _integer(epoch, 1, 1000000):
        raise ValueError()
    result = {'schemaVersion': PROTOCOL, 'epoch': epoch}
    if action == 'audio':
        if not _integer(value.get('acceptedSequence'), 0, 999999):
            raise ValueError()
        level = value.get('level')
        if type(level) is not dict or not all(_number(level.get(name), 1) for name in ('peak', 'rms')):
            raise ValueError()
        result.update(acceptedSequence=value['acceptedSequence'], level={name: level[name] for name in ('peak', 'rms')})
        if value.get('commandMs') is not None:
            if not _number(value['commandMs']):
                raise ValueError()
            result['commandMs'] = value['commandMs']
        return result
    if not _session(value.get('sessionId')):
        raise ValueError()
    result['sessionId'] = value['sessionId']
    if action == 'start':
        return result
    if type(value.get('ended')) is not bool:
        raise ValueError()
    result['ended'] = value['ended']
    if action in ('stop', 'cancel', 'reset'):
        if not _number(value.get('finalizeMs')):
            raise ValueError()
        result['finalizeMs'] = value['finalizeMs']
        return result
    events = value.get('events')
    if type(events) is not list or len(events) > 512:
        raise ValueError()
    clean = []
    prior = 0
    for event in events:
        if type(event) is not dict or not _integer(event.get('id'), 1) or event['id'] <= prior:
            raise ValueError()
        prior = event['id']
        kind = event.get('type')
        item = {'id': event['id'], 'type': kind}
        if kind == 'line':
            if (not _integer(event.get('epoch'), 1, epoch) or type(event.get('lineId')) is not str
                    or not 1 <= len(event['lineId']) <= 128 or type(event.get('text')) is not str
                    or len(event['text']) > 8192 or type(event.get('complete')) is not bool):
                raise ValueError()
            item.update({name: event[name] for name in ('epoch', 'lineId', 'text', 'complete')})
            for name in ('audioSeconds', 'decodeMs'):
                if name in event:
                    if not _number(event[name]):
                        raise ValueError()
                    item[name] = event[name]
        elif kind == 'ended':
            if not _integer(event.get('epoch'), 1, epoch):
                raise ValueError()
            item['epoch'] = event['epoch']
        elif kind == 'error':
            item.update(code=event.get('code') if event.get('code') in SAFE_CODES else 'worker_failed',
                        message='Dictation stopped. Keep the visible draft and start a new take.')
        else:
            raise ValueError()
        clean.append(item)
    result['events'] = clean
    return result


class DictationProxy:
    def __init__(self, home, port=18879, enabled=False, demo=False):
        self.home = Path(home)
        self.port = port
        self.enabled = (enabled is True and type(port) is int and 1 <= port <= 65535 and port != 8793)
        self.demo = demo is True
        self._slots = threading.BoundedSemaphore(REQUEST_CAPACITY)

    def disabled_status(self, demo=False):
        if demo or self.demo:
            return _availability('disabled', 'demo')
        return _availability('not_installed', 'not_configured')

    def request(self, action, payload=None, session=None, sequence=None, demo=False):
        if action not in ROUTES:
            raise ProxyError(404, 'unknown_route')
        if demo or self.demo or not self.enabled:
            if action == 'status':
                return 200, self.disabled_status(demo)
            raise ProxyError(503, 'demo' if demo or self.demo else 'not_configured')
        try:
            key = _read_helper_key(self.home)
        except (OSError, ValueError, UnicodeError, RuntimeError):
            if action == 'status':
                return 200, _availability('not_installed', 'helper_state_missing_or_unsafe')
            raise ProxyError(503, 'not_installed') from None
        if not self._slots.acquire(blocking=False):
            raise ProxyError(409, 'busy')
        try:
            status, value = self._forward(action, payload, session, sequence, key)
        except BusyError:
            raise ProxyError(503, 'busy') from None
        except AuthenticationError:
            if action == 'status':
                return 200, _availability('failed', 'helper_authentication_failed')
            raise ProxyError(503, 'helper_authentication_failed') from None
        except (OSError, TimeoutError, http.client.HTTPException):
            if action == 'status':
                return 200, _availability('not_running', 'connection_unavailable')
            raise ProxyError(503, 'not_running') from None
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            if action == 'status':
                return 200, _availability('failed', 'invalid_response')
            raise ProxyError(503, 'invalid_response') from None
        finally:
            self._slots.release()
        if status == 403:
            if action == 'status':
                return 200, _availability('failed', 'helper_authentication_failed')
            raise ProxyError(503, 'helper_authentication_failed')
        if status != 200:
            code = value.get('error', {}).get('code') if type(value) is dict and type(value.get('error')) is dict else None
            code = code if code in SAFE_CODES else 'unavailable'
            if code == 'busy':
                raise ProxyError(503, 'busy')
            if action == 'status':
                return 200, _availability('failed', code)
            if status not in (400, 404, 409, 413, 503):
                raise ProxyError(503, 'invalid_response')
            raise ProxyError(status, code)
        try:
            clean = _safe_success(action, value)
            if action in ('events', 'stop', 'cancel', 'reset') and clean['sessionId'] != payload['sessionId']:
                raise ValueError()
            if action == 'audio' and clean['acceptedSequence'] != sequence:
                raise ValueError()
            if key in json.dumps(clean, ensure_ascii=False):
                raise ValueError()
            return 200, clean
        except (ValueError, TypeError, OverflowError):
            if action == 'status':
                return 200, _availability('failed', 'invalid_response')
            raise ProxyError(503, 'invalid_response') from None

    def _forward(self, action, payload, session, sequence, key):
        headers = {'Accept': 'application/json', 'Connection': 'close'}
        method, body = 'GET', None
        if action != 'status':
            method = 'POST'
            if action == 'audio':
                if type(payload) is not bytes or not _session(session) or not _integer(sequence, 0, 999999):
                    raise ValueError()
                body = payload
                headers.update({'Content-Type': 'application/octet-stream', 'X-Dictation-Session': session,
                                'X-Dictation-Sequence': str(sequence)})
            else:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
                headers['Content-Type'] = 'application/json'
        deadline = time.monotonic() + HELPER_TIMEOUT
        headers.update(request_headers(key, self.port, method, '/v1/' + action, body,
                                       deadline=min(deadline, time.monotonic() + 2), extra_headers=headers))
        status, raw = wire_json(self.port, method, '/v1/' + action, body, headers,
                                deadline=deadline, limit=MAX_RESPONSE)
        return status, _json(raw)


def from_environment(home, server_port, env=None):
    values = os.environ if env is None else env
    enabled = values.get('WATCH_DICTATION_ENABLED') == '1'
    raw = values.get('WATCH_DICTATION_PORT', '18879')
    valid = type(raw) is str and re.fullmatch(r'[0-9]{1,5}', raw) is not None
    port = int(raw) if valid else 18879
    valid = valid and 1 <= port <= 65535 and port not in (8793, server_port)
    return DictationProxy(home, port, enabled=enabled and valid, demo=values.get('WATCH_DEMO') == '1')


def handle_http(handler, proxy, owner_key, server_port, demo=False):
    handler.close_connection = True

    def reply(status, value):
        handler.connection.settimeout(2)
        try:
            return handler.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                                'application/json; charset=utf-8', extra=(('Cache-Control', 'no-store'),))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            return None

    def values(name):
        return handler.headers.get_all(name, [])

    hosts, origins, keys, sites = (values(name) for name in ('Host', 'Origin', 'X-Live-Room-Key', 'Sec-Fetch-Site'))
    local = ['127.0.0.1:%d' % server_port, 'localhost:%d' % server_port]
    if (len(hosts) != 1 or hosts[0] not in local or len(origins) > 1
            or origins and origins[0] not in ['http://' + host for host in local]
            or len(sites) > 1 or sites and sites[0] not in ('none', 'same-origin')
            or len(keys) != 1 or type(owner_key) is not str or not owner_key
            or not hmac.compare_digest(keys[0].encode(), owner_key.encode())):
        return reply(403, _error('forbidden'))
    try:
        parsed = urllib.parse.urlsplit(handler.path)
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or any(char in handler.path for char in ('?', '#', '%'))):
            raise ProxyError(400, 'invalid_request')
        action = parsed.path[len('/api/dictation/'):] if parsed.path.startswith('/api/dictation/') else None
        if action not in ROUTES or handler.command not in ('GET', 'POST'):
            raise ProxyError(404, 'unknown_route')
        if handler.command == 'GET':
            if action != 'status':
                raise ProxyError(404, 'unknown_route')
            lengths = values('Content-Length')
            if values('Transfer-Encoding') or len(lengths) > 1 or any(value != '0' for value in lengths):
                raise ProxyError(400, 'invalid_request')
            if proxy is None:
                return reply(200, _availability('disabled', 'demo') if demo else _availability('not_installed', 'not_configured'))
            status, result = proxy.request('status', demo=demo)
            return reply(status, result)
        if action == 'status':
            raise ProxyError(404, 'unknown_route')
        if proxy is None or demo or proxy.demo or not proxy.enabled:
            raise ProxyError(503, 'demo' if demo or proxy is not None and proxy.demo else 'not_configured')
        binary = action == 'audio'
        lengths, types = values('Content-Length'), values('Content-Type')
        if (values('Transfer-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[0-9]{1,6}', lengths[0]) or int(lengths[0]) < 1):
            raise ProxyError(400, 'invalid_request')
        limit = MAX_PCM if binary else MAX_JSON
        if int(lengths[0]) > limit:
            raise ProxyError(413, 'body_too_large')
        acceptable = ('application/octet-stream',) if binary else ('application/json', 'application/json;charset=utf-8')
        if len(types) != 1 or types[0].lower().replace(' ', '') not in acceptable:
            raise ProxyError(400, 'invalid_request')
        session, sequence = None, None
        if binary:
            sessions, sequences = values('X-Dictation-Session'), values('X-Dictation-Sequence')
            if (len(sessions) != 1 or not _session(sessions[0]) or len(sequences) != 1
                    or not re.fullmatch(r'[0-9]{1,6}', sequences[0])):
                raise ProxyError(400, 'invalid_request')
            session, sequence = sessions[0], int(sequences[0])
        raw = bytearray()
        deadline = time.monotonic() + 2
        while len(raw) < int(lengths[0]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProxyError(400, 'invalid_request')
            handler.connection.settimeout(remaining)
            chunk = handler.rfile.read1(int(lengths[0]) - len(raw))
            if not chunk:
                raise ProxyError(400, 'invalid_request')
            raw.extend(chunk)
        if binary:
            data = bytes(raw)
            if len(data) % 4:
                raise ProxyError(400, 'invalid_request')
        else:
            data = _json(bytes(raw))
            if type(data) is not dict:
                raise ProxyError(400, 'invalid_request')
            if action == 'start':
                if set(data) - {'intervalSeconds'} or type(data.get('intervalSeconds', .25)) not in (int, float) or data.get('intervalSeconds', .25) not in (.25, .5):
                    raise ProxyError(400, 'invalid_request')
            elif action == 'events':
                if set(data) != {'sessionId', 'after'} or not _session(data['sessionId']) or not _integer(data['after']):
                    raise ProxyError(400, 'invalid_request')
            elif set(data) != {'sessionId'} or not _session(data['sessionId']):
                raise ProxyError(400, 'invalid_request')
        status, result = proxy.request(action, data, session, sequence, demo=demo)
        return reply(status, result)
    except ProxyError as exc:
        return reply(exc.status, _error(exc.code))
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError, OSError):
        return reply(400, _error('invalid_request'))
    except Exception:
        return reply(503, _error('invalid_response'))
