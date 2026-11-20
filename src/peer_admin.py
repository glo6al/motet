"""Exact owner retirement; no native I/O or peer capabilities."""
import hmac
import json
import math
import re
import socket
import sqlite3
import time
import urllib.parse

import peer_bridge
import peer_lifecycle as lifecycle

MAX_BODY = 4096
CONFIRM = 'retire_unknown_without_resend'


def _selector(data, fields):
    keys = set(data)
    selectors = keys - fields
    if not fields <= keys or selectors not in ({'transport_id'}, {'request_id'}):
        raise peer_bridge.PeerError(400, 'Expected one exact Bridge request or transport identifier.')
    return selectors.pop()


def _shared_admission(db, request_id):
    from peer_inbox import excludes_native_id

    if not peer_bridge.valid_request_id(request_id):
        raise peer_bridge.PeerError(400, 'Use an exact shared Bridge request UUID.')
    item = db.execute('''SELECT transport_id,target,created,launched,target_snapshot
                        FROM shared_bridge_requests WHERE client_id=?''', (request_id,)).fetchone()
    if not item:
        raise peer_bridge.PeerError(404, 'Shared Bridge request not found.')
    excludes_native_id(db, request_id)
    transport_id, target, created, launched, snapshot = item
    if not peer_bridge.is_uuid(transport_id):
        raise peer_bridge.PeerError(409, 'The original transport identity is unavailable.')
    try:
        snapshot = json.loads(snapshot)
    except (ValueError, TypeError, RecursionError):
        raise peer_bridge.PeerError(409, 'The original target snapshot is unavailable.') from None
    provider = {'x:': 'codex', 'c:': 'claude'}.get(target[:2]) if isinstance(target, str) else None
    if (not isinstance(snapshot, dict) or provider is None or
            snapshot.get('agent_id') != target or snapshot.get('provider') != provider or
            not all(isinstance(snapshot.get(key), str) and peer_bridge.HEX.fullmatch(snapshot[key])
                    for key in ('identity_revision', 'project_id'))):
        raise peer_bridge.PeerError(409, 'The original target snapshot conflicts with its admission.')
    return transport_id, (target, created, launched, 2), snapshot['identity_revision']


def _selection(store, selector, identifier):
    db = store.db
    admission, revision = None, None
    if selector == 'request_id':
        transport_id, admission, revision = _shared_admission(db, identifier)
    else:
        transport_id = identifier
        if not peer_bridge.is_uuid(transport_id):
            raise peer_bridge.PeerError(400, 'Use an exact canonical transport UUID.')
    count = sum(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE transport_id=?',
                           (transport_id,)).fetchone()[0]
                for table in ('shared_bridge_requests', 'peer_requests'))
    if not count:
        raise peer_bridge.PeerError(404, 'Transport request not found.')
    meta = lifecycle.metadata(db, transport_id).get(transport_id)
    if (count != 1 or not meta or (admission is not None and admission != meta)
            or not isinstance(meta[0], str) or meta[0][:2] not in ('x:', 'c:')
            or not peer_bridge.is_uuid(meta[0][2:])
            or type(meta[1]) not in (int, float) or not math.isfinite(meta[1])
            or meta[2] is not None and (type(meta[2]) not in (int, float)
                                      or not math.isfinite(meta[2]) or meta[2] < meta[1])):
        raise peer_bridge.PeerError(409, 'Exact admission metadata is required.')
    record = lifecycle.get(db, transport_id)
    if record and (record['agent'] != meta[0] or record['created'] != meta[1]):
        raise peer_bridge.PeerError(409, 'Execution identity conflicts with its admission.')
    payload_state, raw = store._peer_payload(transport_id)
    if payload_state != 'clear':
        raise peer_bridge.PeerError(409, 'Retirement conflicts with execution evidence.')
    return transport_id, meta, record, raw, revision


def _candidate(meta, record):
    return meta[2] is not None and (
        record is None or (record['execution'] in ('uncertain', 'running', 'submitted')
                           and record['capture'] != 'captured'))


def _eligible(meta, record, row, now):
    if not _candidate(meta, record):
        return False
    if row is not None:
        if (row['state'] not in ('sending', 'queued', 'awaiting_reply', 'unknown', 'expired')
                or row.get('settled') or row.get('execution_outcome') in ('completed', 'dropped')
                or row.get('capture_disposition') == 'captured'):
            return False
        if row['state'] in ('unknown', 'expired'):
            return True
    elif record and record['execution'] == 'uncertain':
        return True
    return record is not None and now >= meta[1] + peer_bridge.TTL


def _payload(transport_id, raw, meta, revision):
    try:
        row = json.loads(raw[0])
    except (ValueError, TypeError):
        raise peer_bridge.PeerError(409, 'Retained execution evidence is malformed.') from None
    if (not lifecycle.valid_payload(transport_id, row, meta)
            or revision is not None and row.get('identity_revision') != revision):
        raise peer_bridge.PeerError(409, 'Retained execution evidence is malformed.')
    return row


def inspect(store, data):
    selector = _selector(data, set())
    with store.lock:
        tid, meta, record, raw, revision = _selection(store, selector, data[selector])
        row = _payload(tid, raw, meta, revision) if raw and (revision or _candidate(meta, record)) else None
        eligible = _eligible(meta, record, row, time.time())
        return dict({selector: data[selector]}, transport_id=tid, agent=meta[0],
                    execution=record['execution'] if record else ('uncertain' if meta[2] is not None else 'reserved'),
                    capture=record['capture'] if record else 'pending',
                    retained_payload=raw is not None, eligible=eligible)


def retire(store, data):
    if data.get('confirm') != CONFIRM or not isinstance(data.get('agent'), str):
        raise peer_bridge.PeerError(400, 'Exact owner retirement confirmation is required.')
    selector = _selector(data, {'agent', 'confirm'})
    with peer_bridge.transaction(store.db, store.lock):
        db = store.db
        tid, meta, record, raw, revision = _selection(store, selector, data[selector])
        if data['agent'] != meta[0]:
            raise peer_bridge.PeerError(409, 'Recipient identity does not match its admission.')
        if meta[2] is None:
            raise peer_bridge.PeerError(409, 'Only a launched attempt can be retired online.')
        already = bool(record and record['execution'] == 'retired')
        row = _payload(tid, raw, meta, revision) if raw and (revision or not already) else None
        try:
            if not already:
                # Check both the preexisting ledger and any newer retained evidence.
                # A rejected decision rolls back reconciliation with the rest of the transaction.
                previous = record
                if row is not None:
                    lifecycle.reconcile(db, row, meta)
                    record = lifecycle.get(db, tid)
                now = time.time()
                if not (_eligible(meta, previous, row, now) and _eligible(meta, record, row, now)):
                    raise peer_bridge.PeerError(409, 'This attempt is outside the online retirement policy.')
            lifecycle.retire(db, tid, lifecycle.RETIRE_REASON,
                             expected_agent=data['agent'], source='online_owner_decision')
        except ValueError:
            raise peer_bridge.PeerError(409, 'Retirement conflicts with execution evidence.') from None
        return dict({selector: data[selector]}, transport_id=tid, agent=meta[0],
                    status='already_retired' if already else 'retired', provider_outcome='unknown',
                    message=lifecycle.RETIRE_MESSAGE)


def handle_http(handler, store, owner_key, port):
    handler.close_connection = True

    def reply(status, value):
        handler.connection.settimeout(2)
        try:
            return handler.send(status, json.dumps(value, allow_nan=False).encode(), 'application/json; charset=utf-8')
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            return None

    def headers(name):
        return handler.headers.get_all(name, [])

    hosts, origins, keys = headers('Host'), headers('Origin'), headers('X-Live-Room-Key')
    local = ['127.0.0.1:%d' % port, 'localhost:%d' % port]
    if (len(hosts) != 1 or hosts[0] not in local or len(origins) > 1
            or origins and origins[0] not in ['http://' + x for x in local]
            or len(keys) != 1 or not isinstance(owner_key, str) or not owner_key
            or not hmac.compare_digest(keys[0].encode(), owner_key.encode())):
        return reply(403, {'error': 'Owner authentication is required.'})
    try:
        parsed = urllib.parse.urlsplit(handler.path)
        if (handler.command != 'POST' or handler.path not in ('/bridge-admin/v1/inspect', '/bridge-admin/v1/retire')
                or parsed.query or parsed.fragment or parsed.scheme or parsed.netloc):
            raise peer_bridge.PeerError(400, 'Invalid owner bridge route.')
        if store is None:
            raise peer_bridge.PeerError(503, 'Owner bridge administration is unavailable.')
        lengths = headers('Content-Length')
        if (headers('Transfer-Encoding') or headers('Content-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 6
                or int(lengths[0]) < 1):
            raise peer_bridge.PeerError(400, 'A bounded JSON body is required.')
        if int(lengths[0]) > MAX_BODY:
            raise peer_bridge.PeerError(413, 'Owner bridge request is too large.')
        types = headers('Content-Type')
        if len(types) != 1 or types[0].lower().replace(' ', '') not in ('application/json', 'application/json;charset=utf-8'):
            raise peer_bridge.PeerError(400, 'Use an application/json body.')

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
            deadline, raw = time.monotonic() + 2, bytearray()
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
            raise peer_bridge.PeerError(400, 'Invalid JSON request body.') from None
        out = inspect(store, data) if handler.path.endswith('/inspect') else retire(store, data)
        return reply(200, out)
    except peer_bridge.PeerError as error:
        return reply(error.status, {'error': str(error)})
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return reply(503, {'error': 'Owner bridge administration is unavailable.'})
