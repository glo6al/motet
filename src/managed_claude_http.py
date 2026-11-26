"""Strict owner-only status and permission boundary for WATCH-managed Claude."""
from contextlib import nullcontext
import hmac
import json
import math
import re
import socket
import time
import urllib.parse

from new_chat_types import NewChatError
from new_chat_inventory import canonical_uuid


def _status(manager, run_id):
    raw = manager.status(run_id)
    return {"schemaVersion": 1, "runId": raw["runId"], "state": raw["state"],
            "recoveryRequired": raw["state"] == "uncertain",
            "serverTime": raw["serverTime"], "timeoutSeconds": raw["timeoutSeconds"],
            "evidenceAvailable": raw["evidenceAvailable"],
            "permissions": [{k: item[k] for k in ("id", "tool", "input", "state", "canAllow", "canDeny",
                            "createdAt", "expiresAt", "decision", "resolutionReason", "decisionState",
                            "resolvedAt", "inputAvailable")
                            if k in item} for item in raw.get("permissions", [])],
            "capabilities": {"permissions": raw["evidenceAvailable"] and raw["state"] not in ("uncertain", "exited")},
            # The owner's stop of this chat, if any (claude_managed.ManagedClaude.stop), and the messages it would fail.
            "ownerStop": raw.get("ownerStop"), "queued": raw.get("queued", 0)}

MAX_BODY = 65536


def handle_http(handler, service, owner_key, port, owner_store=None):
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
                or '%' in parsed.path or not parsed.path.startswith('/api/managed-claude/')):
            raise NewChatError('invalid_request', 'Invalid Managed Claude route.', 400)
        route = parsed.path
        if service is None or not service.enabled or service.demo:
            raise NewChatError('unavailable', 'Managed Claude is unavailable on this host.', 503)
        if handler.command == 'GET':
            if headers('Transfer-Encoding') or any(v != '0' for v in headers('Content-Length')) or len(headers('Content-Length')) > 1:
                raise NewChatError('invalid_request', 'GET requests cannot contain a body.', 400)
            match = re.fullmatch(r'/api/managed-claude/runs/([0-9a-f-]{36})', route)
            if match:
                return reply(200, _status(service, match[1]))
            raise NewChatError('unknown_route', 'Unknown Managed Claude route.', 404)
        if handler.command != 'POST':
            raise NewChatError('unknown_route', 'Unknown Managed Claude route.', 404)
        lengths = headers('Content-Length')
        if (headers('Transfer-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 6
                or int(lengths[0]) < 1):
            raise NewChatError('invalid_request', 'A bounded JSON body is required.', 400)
        if int(lengths[0]) > MAX_BODY:
            raise NewChatError('body_too_large', 'The Managed Claude request is too large.', 413)
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
        match = re.fullmatch(r'/api/managed-claude/runs/([0-9a-f-]{36})/permissions/([0-9a-f-]{36})', route)
        if match:
            if (set(data) != {'requestId', 'decision'} or not canonical_uuid(data['requestId'])
                    or data['decision'] not in ('allow', 'deny')):
                raise NewChatError('invalid_request', 'Expected requestId and an allow or deny decision.', 400)
            with owner_store.lock if owner_store is not None else nullcontext():
                if owner_store is not None and any(owner_store.db.execute(query, (data['requestId'],)).fetchone() for query in (
                        'SELECT 1 FROM requests WHERE id=?', 'SELECT 1 FROM controls WHERE id=?',
                        'SELECT 1 FROM expired_controls WHERE id=?')):
                    raise NewChatError('request_conflict', 'That identifier belongs to another owner or peer operation.', 409)
                value = service.decide(match[1], match[2], data['requestId'], data['decision'])
            # The manager returns a safe retained decision receipt, never its native callback.
            return reply(200, {'schemaVersion': 1, **{k: value[k] for k in
                ('requestId', 'permissionId', 'decision', 'state', 'decisionState', 'resolutionReason', 'resolvedAt') if k in value}})
        match = re.fullmatch(r'/api/managed-claude/runs/([0-9a-f-]{36})/stop', route)
        if match:
            # The owner's stop of a chat Motet started. Only a Claude chat ('c:') can be one; a Codex chat, or any
            # chat Motet did not start, is refused before anything is signalled.
            if (set(data) != {'requestId', 'agent'} or not canonical_uuid(data['requestId'])
                    or type(data['agent']) is not str or len(data['agent']) > 80):
                raise NewChatError('invalid_request', 'Expected requestId and the chat to stop.', 400)
            if not data['agent'].startswith('c:'):
                raise NewChatError('not_managed', 'Motet did not start this chat, so it cannot stop it.', 404)
            with owner_store.lock if owner_store is not None else nullcontext():
                if owner_store is not None and any(owner_store.db.execute(query, (data['requestId'],)).fetchone() for query in (
                        'SELECT 1 FROM requests WHERE id=?', 'SELECT 1 FROM controls WHERE id=?',
                        'SELECT 1 FROM expired_controls WHERE id=?')):
                    raise NewChatError('request_conflict', 'That identifier belongs to another owner or peer operation.', 409)
            value = service.stop(match[1], data['requestId'], data['agent'])
            return reply(200, {'schemaVersion': 1, **{k: value[k] for k in
                ('requestId', 'runId', 'agent', 'state', 'ownerStop', 'already')}})
        raise NewChatError('unknown_route', 'Unknown Managed Claude route.', 404)
    except NewChatError as exc:
        return error(exc.status, exc.code, exc.message)
    except (ValueError, TypeError, OverflowError, UnicodeError):
        return error(400, 'invalid_request', 'Invalid Managed Claude request.')
    except Exception:
        return error(503, 'unavailable', 'Managed Claude is unavailable on this host.')
