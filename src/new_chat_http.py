"""Strict owner-only HTTP boundary for the separate New chat service."""
import hmac
import json
import math
import re
import socket
import time
import urllib.parse

from new_chat_types import NewChatError

MAX_BODY = 65536


def handle_http(handler, service, owner_key, port, unavailable=None):
    """``unavailable``: the plain reason the server knows New chat did not start (service None), else the generic
    words are used."""
    handler.close_connection = True

    def reply(status, value):
        handler.connection.settimeout(2)
        try:
            return handler.send(status, json.dumps(value, allow_nan=False).encode(), 'application/json; charset=utf-8')
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            return None

    def error(status, code, message):
        return reply(status, {'schemaVersion': 1, 'error': {'code': code, 'message': message}})

    def headers(name):
        return handler.headers.get_all(name, [])

    hosts, origins, keys = headers('Host'), headers('Origin'), headers('X-Live-Room-Key')
    local = ['127.0.0.1:%d' % port, 'localhost:%d' % port]
    if (len(hosts) != 1 or hosts[0] not in local or len(origins) > 1
            or origins and origins[0] not in ['http://' + x for x in local]
            or len(keys) != 1 or not isinstance(owner_key, str) or not owner_key
            or not hmac.compare_digest(keys[0].encode(), owner_key.encode())):
        return error(403, 'forbidden', 'Owner authentication is required.')
    try:
        parsed = urllib.parse.urlsplit(handler.path)
        if (parsed.query or parsed.fragment or '?' in handler.path or '#' in handler.path or parsed.scheme or parsed.netloc
                or '%' in parsed.path or not parsed.path.startswith('/api/new-chat/')):
            raise NewChatError('invalid_request', 'Invalid New chat route.', 400)
        route = parsed.path
        if service is None:
            raise NewChatError('unavailable', unavailable or 'New chat is unavailable on this host.', 503)
        if handler.command == 'GET':
            if headers('Transfer-Encoding') or any(v != '0' for v in headers('Content-Length')) or len(headers('Content-Length')) > 1:
                raise NewChatError('invalid_request', 'GET requests cannot contain a body.', 400)
            if route == '/api/new-chat/context':
                return reply(200, service.context())
            if route == '/api/new-chat/folders':
                return reply(200, service.saved_folders())
            match = re.fullmatch(r'/api/new-chat/folders/choose/([0-9a-f-]{36})', route)
            if match:
                return reply(200, service.folder_choice_status(match[1]))
            match = re.fullmatch(r'/api/new-chat/requests/([0-9a-f-]{36})', route)
            if match:
                return reply(200, service.request_status(match[1]))
            match = re.fullmatch(r'/api/new-chat/attempts/([0-9a-f-]{36})', route)
            if match:
                return reply(200, service.status(match[1]))
            raise NewChatError('unknown_route', 'Unknown New chat route.', 404)
        if handler.command != 'POST':
            raise NewChatError('unknown_route', 'Unknown New chat route.', 404)
        lengths = headers('Content-Length')
        if (headers('Transfer-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 6
                or int(lengths[0]) < 1):
            raise NewChatError('invalid_request', 'A bounded JSON body is required.', 400)
        if int(lengths[0]) > MAX_BODY:
            raise NewChatError('body_too_large', 'The New chat request is too large.', 413)
        content_types = headers('Content-Type')
        if (len(content_types) != 1 or content_types[0].lower().replace(' ', '')
                not in ('application/json', 'application/json;charset=utf-8')):
            raise NewChatError('invalid_request', 'Use an application/json request body.', 400)

        def pairs(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError()
                out[key] = value
            return out

        def constant(_):
            raise ValueError()

        def finite(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number

        try:
            deadline = time.monotonic() + 2
            raw = bytearray()
            while len(raw) < int(lengths[0]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                handler.connection.settimeout(remaining)
                chunk = handler.rfile.read1(int(lengths[0]) - len(raw))
                if not chunk:
                    raise ValueError()
                raw.extend(chunk)
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                              parse_constant=constant, parse_float=finite)
            if type(data) is not dict:
                raise ValueError()
        except (ValueError, UnicodeError, OSError, RecursionError):
            raise NewChatError('invalid_request', 'Invalid JSON request body.', 400) from None
        match = re.fullmatch(r'/api/new-chat/folders/([0-9a-f]{64})/remove', route)
        if match:
            return reply(200, service.remove_saved_folder(match[1], data))
        if route == '/api/new-chat/folders/choose':
            forwarded = any(name.lower().startswith('x-forwarded-') or name.lower() in
                            ('forwarded', 'x-real-ip', 'via', 'cf-connecting-ip', 'true-client-ip',
                             'x-original-forwarded-for', 'x-cluster-client-ip', 'fastly-client-ip')
                            for name in handler.headers.keys())
            return reply(202, service.begin_folder_choice(data, handler.connection, forwarded=forwarded))
        match = re.fullmatch(r'/api/new-chat/folders/choose/([0-9a-f-]{36})/(cancel|accept)', route)
        if match:
            if match[2] == 'cancel':
                return reply(200, service.cancel_folder_choice(match[1], data))
            return reply(200, service.accept_folder_choice(match[1], data))
        if route == '/api/new-chat/intents':
            return reply(201, service.prepare(data))
        if route == '/api/new-chat/attempts':
            status, view = service.open(data)
            return reply(status, view)
        match = re.fullmatch(r'/api/new-chat/attempts/([0-9a-f-]{36})/observe', route)
        if match:
            return reply(200, service.observe(match[1], data))
        raise NewChatError('unknown_route', 'Unknown New chat route.', 404)
    except NewChatError as exc:
        return error(exc.status, exc.code, exc.message)
    except (ValueError, TypeError, OverflowError, UnicodeError):
        return error(400, 'invalid_request', 'Invalid New chat request.')
    except Exception:
        return error(503, 'unavailable', 'New chat is unavailable on this host.')
