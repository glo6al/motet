"""Owner metadata and exact retirement for native peer requests."""
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
HELD_PAGE_SIZE = 50
MAX_TIME = 253402300799
CONFIRM = 'retire_unknown_without_resend'
UNAVAILABLE = 'Motet could not match this hold to a chat.'
_STALE_HOLD = 'Motet could not verify that this chat is still on hold.'
_HELD_CURSOR_SOURCES = {
    0: ('peer_execution', 'request_id', 'request_id'),
    1: ('peer_requests', 'transport_id', 'transport_id'),
    2: ('shared_bridge_requests', 'client_id', 'transport_id'),
}


def _time(value):
    return value if type(value) in (int, float) and 0 < value <= MAX_TIME else None


def _selector(data, fields):
    keys = set(data)
    selectors = keys - fields
    if not fields <= keys or selectors not in ({'transport_id'}, {'request_id'}):
        raise peer_bridge.PeerError(400, 'Expected one exact Bridge request or transport identifier.')
    return selectors.pop()


def _hold_selector(data, fields):
    require_hold = 'require_hold' in data
    if require_hold:
        if data['require_hold'] is not True:
            raise peer_bridge.PeerError(400, 'The hold precondition must be true.')
        fields = fields | {'require_hold'}
    return _selector(data, fields), require_hold


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
    selector, require_hold = _hold_selector(data, set())
    with store.lock:
        tid, meta, record, raw, revision = _selection(store, selector, data[selector])
        try:
            row = _payload(tid, raw, meta, revision) if raw and (require_hold or revision or _candidate(meta, record)) else None
        except peer_bridge.PeerError:
            if not require_hold:
                raise
            eligible = False
        else:
            eligible = (_eligible(meta, record, row, time.time())
                        and (not require_hold or _verified_hold(meta, record, row)))
        return dict({selector: data[selector]}, transport_id=tid, agent=meta[0],
                    execution=record['execution'] if record else ('uncertain' if meta[2] is not None else 'reserved'),
                    capture=record['capture'] if record else 'pending',
                    retained_payload=raw is not None, eligible=eligible)


def _held_id(version, identifier, transport):
    return peer_bridge.digest(ascii((version, identifier, transport)).encode('ascii'))


def _held_cursor(db, cursor):
    if cursor is None:
        return MAX_TIME + 1, 2, 0
    match = re.fullmatch(r'([012]):(-?[0-9]{1,19}):([0-9a-f]{64})', cursor) if isinstance(cursor, str) else None
    if match:
        version, rowid, identity = int(match[1]), int(match[2]), match[3]
        if str(rowid) == match[2] and -(2**63) <= rowid < 2**63:
            table, key, transport = _HELD_CURSOR_SOURCES[version]
            # Resolve the anchor in its native ledger even after it leaves the current list.
            row = db.execute(f'SELECT {key},{transport},created FROM {table} WHERE rowid=?', (rowid,)).fetchone()
            if row and _held_id(version, row[0], row[1]) == identity:
                return _time(row[2]) or 0, version, rowid
    raise peer_bridge.PeerError(400, 'Invalid Bridge held cursor.')


def _held_candidates(db, after):
    return db.execute('''WITH native AS (
        SELECT 2 AS version, r.rowid AS row_number, r.client_id AS identifier, r.transport_id AS transport,
               r.target AS agent, r.created, r.launched, r.target_snapshot AS shared_snapshot, NULL AS legacy_label
        FROM shared_bridge_requests AS r
        WHERE NOT EXISTS (SELECT 1 FROM peer_inbox_requests AS i WHERE i.request_id=r.client_id)
        UNION ALL
        SELECT 1, r.rowid, r.transport_id, r.transport_id,
               CASE p.runtime WHEN 'codex' THEN 'x:'||p.session WHEN 'claude' THEN 'c:'||p.session END,
               r.created, r.launched, NULL, p.label
        FROM peer_requests AS r LEFT JOIN peer_principals AS p ON p.id=r.target
        UNION ALL
        SELECT 0, e.rowid, e.request_id, e.request_id, e.agent, e.created, NULL, NULL, NULL
        FROM peer_execution AS e
        WHERE e.execution NOT IN ('completed','dropped','retired')
          AND NOT EXISTS (SELECT 1 FROM shared_bridge_requests AS r WHERE r.transport_id=e.request_id)
          AND NOT EXISTS (SELECT 1 FROM peer_requests AS r WHERE r.transport_id=e.request_id)
          AND NOT EXISTS (SELECT 1 FROM peer_inbox_requests AS i WHERE i.request_id=e.request_id)
        ), candidates AS (
        SELECT a.*, CASE WHEN typeof(a.created) IN ('integer','real') AND a.created > 0 AND a.created <= ?
                        THEN a.created ELSE 0 END AS sort_time
        FROM native AS a LEFT JOIN peer_execution AS e ON e.request_id=a.transport
        WHERE (a.version=0 OR a.launched IS NOT NULL OR e.execution IN ('uncertain','submitted','running'))
          AND NOT CASE WHEN e.execution IN ('completed','dropped','retired') AND e.agent=a.agent
                           AND e.created=a.created AND json_valid(e.evidence)
                       THEN json_type(e.evidence)='object' ELSE 0 END
        ) SELECT version,row_number,identifier,transport,agent,created,launched,shared_snapshot,legacy_label
          FROM candidates WHERE (sort_time,version,row_number) < (?,?,?)
          ORDER BY sort_time DESC,version DESC,row_number DESC''', (MAX_TIME, *after))


def _display_name(value, provider):
    fallback = {'codex': 'Codex', 'claude': 'Claude'}.get(provider, 'Unknown recipient')
    if not isinstance(value, str) or '/' in value or '\\' in value:
        return fallback
    return ''.join(char for char in value[:120] if char.isprintable()).strip() or fallback


def _payload_agrees(row, execution, evidence):
    if row['state'] not in ('sending', 'queued', 'awaiting_reply', 'unknown', 'expired'):
        return False
    if row.get('settled') or row.get('execution_outcome') or row.get('capture_disposition') == 'captured':
        return False
    if (row['state'] in ('unknown', 'expired')) != (execution == 'uncertain'):
        return False
    if row.get('tracking_stopped') and execution != 'uncertain':
        return False
    if row.get('mod_project') and evidence.get('project_digest') not in (
            None, peer_bridge.digest(row['mod_project'].encode('utf-8'))):
        return False
    receipt_fields = ('bound_turn', 'mod_turn', 'mod_attempt_id', 'mod_generation', 'mod_enrollment',
                      'lifecycle_protocol', 'completion_event_id', 'completion_fingerprint',
                      'answer_event_id', 'answer_fingerprint')
    return all(row.get(key) in (None, '') or evidence.get(key) in (None, '', row[key]) for key in receipt_fields)


def _verified_hold(meta, record, row):
    if (not record or record['execution'] != 'uncertain'
            or record['capture'] not in ('pending', 'withheld', 'unusable')
            or _time(meta[1]) is None or _time(meta[2]) is None):
        return False
    evidence = record['evidence']
    return (isinstance(evidence, dict) and evidence.get('launched') is True
            and (row is None or _payload_agrees(row, 'uncertain', evidence)))


def _held_item(store, candidate, now):
    version, _, identifier, transport, agent, created, launched, snapshot, label = candidate
    selector_key = 'request_id' if version == 2 else 'transport_id'
    valid_id = peer_bridge.valid_request_id if version == 2 else peer_bridge.is_uuid
    selector = {selector_key: identifier} if version and valid_id(identifier) and peer_bridge.is_uuid(transport) else None
    if not isinstance(agent, str) or agent[:2] not in ('x:', 'c:') or not peer_bridge.is_uuid(agent[2:]):
        agent = None
    provider = {'x:': 'codex', 'c:': 'claude'}.get(agent[:2], 'unknown') if agent else 'unknown'
    item = dict(item_id=_held_id(version, identifier, transport), selector=selector, agent=agent,
                provider=provider, name=_display_name(None, provider), created_at=_time(created),
                held_since=None, episode_id=None, notice_due_at=None, group='unavailable',
                reason_code='unverifiable_record', reason=UNAVAILABLE, clearable=False, unavailable_reason=UNAVAILABLE)
    if not selector or not agent or not item['created_at'] or not _time(launched):
        return item
    try:
        tid, meta, record, raw, revision = _selection(store, selector_key, identifier)
        row = _payload(tid, raw, meta, revision) if raw else None
        evidence = record['evidence'] if record else None
        if tid != transport or meta != (agent, created, launched, version) or not isinstance(evidence, dict):
            return item
        execution, capture = record['execution'], record['capture']
        if execution in lifecycle.TERMINAL:
            return None
        # Codex authorizes the launch before its worker saves the queue outcome.
        if (provider == 'codex' and execution == 'reserved' and capture == 'pending'
                and row is not None and row['state'] == 'sending'
                and not row.get('bound_turn') and not evidence.get('bound_turn')
                and 'launched' not in evidence and _payload_agrees(row, execution, evidence)):
            return None
        if execution in ('submitted', 'running'):
            healthy = (evidence.get('launched') is True and capture in ('pending', 'withheld', 'unusable')
                       and (row is None or _payload_agrees(row, execution, evidence)))
            return None if healthy else item
        if not _verified_hold(meta, record, row):
            return item
        name = json.loads(snapshot).get('name') if version == 2 else label
        eligible = _eligible(meta, record, row, now)
    except (peer_bridge.PeerError, ValueError, TypeError, KeyError, RecursionError):
        return item
    item.update(name=_display_name(name, provider), clearable=eligible, unavailable_reason=None,
                group='held', reason_code='completion_unverified',
                reason='Motet could not confirm the message arrived or that the turn ended.')
    if provider == 'codex' and evidence.get('codex_retention_hold') == lifecycle.CODEX_RETENTION_HOLD:
        item.update(reason_code='codex_retention', reason='The message was kept for a day and no end was seen.')
    episode = lifecycle.hold_episode(record)
    if episode and episode['since'] >= launched:
        item.update(held_since=episode['since'], episode_id=episode['episode_id'], notice_due_at=episode['due_at'])
    return item


def held(store, data):
    if set(data) - {'cursor'} or 'cursor' in data and not isinstance(data['cursor'], str):
        raise peer_bridge.PeerError(400, 'Expected an optional Bridge held cursor.')
    with store.lock:
        now = time.time()
        if _time(now) is None:
            raise ValueError('Bridge clock is unavailable.')
        after = _held_cursor(store.db, data.get('cursor'))
        items, cursor, next_cursor = [], None, None
        for candidate in _held_candidates(store.db, after):
            item = _held_item(store, candidate, now)
            if item is None:
                continue
            if len(items) == HELD_PAGE_SIZE:
                next_cursor = cursor
                break
            items.append(item)
            version, rowid = candidate[:2]
            cursor = f'{version}:{rowid}:{item["item_id"]}'
    return dict(items=items, next_cursor=next_cursor, checked_at=now)


def retire(store, data):
    if data.get('confirm') != CONFIRM or not isinstance(data.get('agent'), str):
        raise peer_bridge.PeerError(400, 'Exact owner retirement confirmation is required.')
    selector, require_hold = _hold_selector(data, {'agent', 'confirm'})
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
                if require_hold and not _verified_hold(meta, previous, row):
                    raise peer_bridge.PeerError(409, _STALE_HOLD)
                if row is not None:
                    lifecycle.reconcile(db, row, meta)
                    record = lifecycle.get(db, tid)
                if require_hold and not _verified_hold(meta, record, row):
                    raise peer_bridge.PeerError(409, _STALE_HOLD)
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
        operations = {'/bridge-admin/v1/inspect': inspect, '/bridge-admin/v1/retire': retire, '/bridge-admin/v1/held': held}
        if (handler.command != 'POST' or handler.path not in operations
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
        out = operations[handler.path](store, data)
        return reply(200, out)
    except peer_bridge.PeerError as error:
        return reply(error.status, {'error': str(error)})
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return reply(503, {'error': 'Owner bridge administration is unavailable.'})
