"""A shared, explicitly claimed inbox with no native chat transport."""
import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid

import claude_channel
import peer_bridge as bridge
import private_storage


CONFIG_NAME = 'antigravity-inbox.json'
LABEL = 'Antigravity inbox'
RETENTION_MS = 24 * 60 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
MAX_OPEN = 32
MAX_TOMBSTONES = 100000
PAGE_ROWS = 50
RESPONSE_BYTES = 60000
CLEANUP_ROWS = 256
HEX = re.compile('[0-9a-f]{64}')
# Keep the server's definition of blank text in step with the adapter's trim().
TEXT_WHITESPACE = ('\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006'
                   '\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff')
RECEIVER = {'check_inbox', 'claim_inbox_message', 'answer_inbox_message'}
SCHEMAS = {
    'list_inboxes': ((), ()),
    'send_inbox_message': (('inbox_id', 'inbox_revision', 'request_id', 'text'), ('declared_sender',)),
    'get_inbox_reply': (('request_id',), ()),
    'list_inbox_requests': ((), ('cursor',)),
    'check_inbox': (('inbox_id', 'inbox_revision', 'receiver_proof'), ('cursor',)),
    'claim_inbox_message': (('inbox_id', 'inbox_revision', 'receiver_proof', 'request_id', 'claim_token'), ()),
    'answer_inbox_message': (('inbox_id', 'inbox_revision', 'receiver_proof', 'request_id', 'claim_token', 'answer'), ()),
}


def strict_json(raw):
    def unique_fields(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('Duplicate JSON field.')
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique_fields)
    except RecursionError:
        raise ValueError('JSON nesting exceeds the safe limit.') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def hex_string(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def inbox_uuid(value):
    return bridge.is_uuid(value) and uuid.UUID(value).version == 4


def read_config(directory, name=CONFIG_NAME):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        claude_channel._private(info)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
            raise ValueError('Inbox configuration must be a private regular file.')
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError('Inbox configuration is too large.')
    value = strict_json(raw.decode('utf-8'))
    if (not isinstance(value, dict) or set(value) != {'version', 'inbox_id', 'label', 'receiver_key'}
            or type(value['version']) is not int or value['version'] != 1
            or not inbox_uuid(value['inbox_id']) or value['label'] != LABEL
            or not hex_string(value['receiver_key'])):
        raise ValueError('Invalid inbox configuration.')
    return value


def revision(value):
    key_hash = bridge.digest(bytes.fromhex(value['receiver_key']))
    descriptor = {'inbox_id': value['inbox_id'], 'key_hash': key_hash, 'label': value['label']}
    return bridge.digest(b'watch-inbox-revision-v1\n' + canonical(descriptor))


def read_config_file(path):
    if os.name != 'posix':
        raise OSError('Inbox configuration requires a POSIX system.')
    path = os.path.abspath(os.path.expanduser(path))
    directory = private_storage.open_no_links(os.path.dirname(path), directory=True)
    try:
        claude_channel._private(os.fstat(directory), True)
        return read_config(directory, os.path.basename(path))
    finally:
        os.close(directory)


def load_config(state_dir):
    if os.name != 'posix' or state_dir is None:
        return None
    try:
        config = read_config_file(os.path.join(state_dir, CONFIG_NAME))
        return dict(config, inbox_revision=revision(config))
    except (OSError, ValueError, claude_channel.ChannelError):
        return None


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS peer_inbox_requests (
        request_id TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, inbox_revision TEXT NOT NULL,
        payload_hash TEXT NOT NULL, credential_id TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','claimed','replied','expired')),
        claimed_at INTEGER, claim_hash TEXT, answer_hash TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS peer_inbox_payloads (
        request_id TEXT PRIMARY KEY REFERENCES peer_inbox_requests(request_id), expires_at INTEGER NOT NULL,
        text TEXT NOT NULL, text_preview TEXT NOT NULL, declared_sender TEXT, answer TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS peer_inbox_history ON peer_inbox_requests(created_at,request_id)')
    db.execute('CREATE INDEX IF NOT EXISTS peer_inbox_target ON peer_inbox_requests(inbox_id,created_at,request_id)')
    db.execute('''CREATE INDEX IF NOT EXISTS peer_inbox_open ON peer_inbox_requests(inbox_id,inbox_revision,created_at,request_id)
                  WHERE state IN ('queued','claimed')''')
    db.execute('CREATE INDEX IF NOT EXISTS peer_inbox_expiry ON peer_inbox_payloads(expires_at,request_id)')


def now_ms():
    return int(time.time() * 1000)


def cleanup(db, now=None):
    ids = db.execute('''SELECT request_id FROM peer_inbox_payloads WHERE expires_at<=?
                        ORDER BY expires_at,request_id LIMIT ?''', (now_ms() if now is None else now, CLEANUP_ROWS)).fetchall()
    db.executemany('DELETE FROM peer_inbox_payloads WHERE request_id=?', ids)
    db.executemany("UPDATE peer_inbox_requests SET state='expired' WHERE request_id=?", ids)
    return len(ids)


def excludes_native_id(db, request_id):
    if db.execute('SELECT 1 FROM peer_inbox_requests WHERE request_id=?', (request_id,)).fetchone():
        raise bridge.PeerError(409, 'This request identifier belongs to another Motet transport.')


def recent_count(db, now):
    return db.execute('SELECT COUNT(*) FROM peer_inbox_requests WHERE created_at>?', (now - RETENTION_MS,)).fetchone()[0]


def receiver_proof(key, credential_id, run_id, nonce, method, args):
    return bridge.mac(key, 'watch-inbox-receiver-v1', credential_id, run_id, nonce,
                      args['inbox_id'], args['inbox_revision'], method, bridge.digest(canonical(args)))


def fits(result):
    # Match the actual shared HTTP encoding, including the result wrapper.
    return len(json.dumps({'result': result}, ensure_ascii=False).encode('utf-8')) <= RESPONSE_BYTES


def nonempty_text(text, limit):
    return bool(text.strip(TEXT_WHITESPACE)) and len(text) <= limit and '\x00' not in text


def validate(method, args):
    required, optional = SCHEMAS[method]
    if not set(required) <= set(args) <= set(required + optional) or any(type(value) is not str for value in args.values()):
        raise bridge.PeerError(400, 'Invalid inbox parameters.')
    try:
        for value in args.values():
            value.encode('utf-8')
    except UnicodeError:
        raise bridge.PeerError(400, 'Invalid inbox text encoding.') from None
    for key in ('request_id', 'cursor'):
        if key in args and not bridge.valid_request_id(args[key]):
            raise bridge.PeerError(400, 'Invalid inbox request identifier or cursor.')
    if 'inbox_id' in args and (not inbox_uuid(args['inbox_id']) or not hex_string(args['inbox_revision'])):
        raise bridge.PeerError(400, 'Invalid inbox identity.')
    if 'claim_token' in args and not hex_string(args['claim_token']):
        raise bridge.PeerError(400, 'Invalid inbox claim token.')
    for key in ('text', 'answer'):
        if key in args and not nonempty_text(args[key], bridge.MAX_PEER_TEXT):
            raise bridge.PeerError(400, 'Inbox messages and answers require nonempty text of at most 16000 characters.')
    sender = args.get('declared_sender')
    if sender is not None and (not nonempty_text(sender, 120) or any(ord(c) < 32 or ord(c) == 127 for c in sender)):
        raise bridge.PeerError(400, 'Invalid declared inbox sender.')


_SUMMARY = ('inbox_id', 'inbox_revision', 'request_id', 'state', 'created_at', 'expires_at', 'text_preview', 'declared_sender')
_FIELDS = _SUMMARY + ('claimed_at', 'claim_hash', 'answer_hash', 'payload_hash')
_SELECT = '''SELECT r.inbox_id,r.inbox_revision,r.request_id,r.state,r.created_at,r.expires_at,p.text_preview,
                    p.declared_sender,r.claimed_at,r.claim_hash,r.answer_hash,r.payload_hash'''
_JOIN = ' FROM peer_inbox_requests r LEFT JOIN peer_inbox_payloads p ON r.request_id=p.request_id AND p.expires_at>?'


def summary(row, now, state=None):
    result = {field: row[field] for field in _SUMMARY}
    if now >= row['expires_at'] or row['state'] == 'expired':
        result.update(state='expired', text_preview='', declared_sender=None)
    else:
        if not isinstance(result['text_preview'], str):
            raise bridge.PeerError(503, 'Inbox request payload is unavailable.')
        if state is not None:
            result['state'] = state
    return result


class PeerInbox:
    methods = SCHEMAS.keys()

    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.config = load_config(getattr(store, 'state_dir', None))

    def require_target(self, args, status=409):
        if (self.config is None or args['inbox_id'] != self.config['inbox_id']
                or args['inbox_revision'] != self.config['inbox_revision']):
            raise bridge.PeerError(status, 'The selected inbox configuration is unavailable or changed.')

    def server_proof(self, port, credential_id, nonce, run_id, target):
        self.require_target(target, 403)
        return bridge.mac(self.config['receiver_key'], 'watch-inbox-server-v1', port, credential_id, nonce,
                          run_id, target['inbox_id'], target['inbox_revision'])

    def response_signature(self, port, credential_id, run_id, nonce, target, status, raw_response):
        # The HTTP handler only assigns this target after validating the inbox challenge.
        inbox_id, inbox_revision = target
        return bridge.mac(self.config['receiver_key'], 'watch-inbox-response-v1', port, credential_id, run_id,
                          nonce, inbox_id, inbox_revision, status, bridge.digest(raw_response))

    def require_receiver(self, method, args, credential_id, run_id, nonce):
        self.require_target(args, 403)
        proof = args.get('receiver_proof')
        unsigned = {key: value for key, value in args.items() if key != 'receiver_proof'}
        if (not hex_string(proof) or not hex_string(nonce) or not hmac.compare_digest(proof, receiver_proof(
                self.config['receiver_key'], credential_id, run_id, nonce, method, unsigned))):
            raise bridge.PeerError(403, 'Inbox receiver access is unavailable.')

    def rpc(self, method, args, credential_id, run_id, nonce, inbox_target):
        validate(method, args)
        if method in RECEIVER:
            if inbox_target != (args['inbox_id'], args['inbox_revision']):
                raise bridge.PeerError(403, 'Inbox receiver server proof required on this connection.')
            self.require_receiver(method, args, credential_id, run_id, nonce)
        mutation = method in ('send_inbox_message', 'claim_inbox_message', 'answer_inbox_message')
        with (bridge.transaction(self.db, self.store.lock) if mutation else self.store.lock):
            bridge.service_credential(self.db, credential_id)
            now = now_ms()
            if method == 'list_inboxes':
                if self.config is None:
                    return {'inboxes': []}
                found = {key: self.config[key] for key in ('inbox_id', 'inbox_revision', 'label')}
                found['availability'] = 'shared pull inbox configured'
                return {'inboxes': [found]}
            if method in ('list_inbox_requests', 'check_inbox'):
                return self.page(now, args.get('cursor'), receiver=method == 'check_inbox')
            if method == 'send_inbox_message':
                return self.send(args, credential_id, now)
            row = self.load(args['request_id'], now)
            if method == 'get_inbox_reply':
                result = summary(row, now)
                if result['state'] != 'replied':
                    return dict(result, answer=None)
                if not isinstance(row['answer'], str) or not nonempty_text(row['answer'], bridge.MAX_PEER_TEXT):
                    raise bridge.PeerError(503, 'Inbox answer is unavailable.')
                return dict(result, answer=row['answer'])
            return self.receive(method, args, row, now)

    def load(self, request_id, now, required=True):
        row = self.db.execute(_SELECT + ',p.text,p.answer' + _JOIN + ' WHERE r.request_id=?', (now, request_id)).fetchone()
        if row is not None:
            return dict(zip(_FIELDS + ('text', 'answer'), row))
        if required:
            raise bridge.PeerError(404, 'Inbox request not found.')
        return None

    def send(self, args, credential_id, now):
        self.require_target(args)
        inbox_id, inbox_revision, request_id = (args[key] for key in ('inbox_id', 'inbox_revision', 'request_id'))
        text, sender = args['text'], args.get('declared_sender')
        if self.db.execute('SELECT 1 FROM shared_bridge_requests WHERE client_id=?', (request_id,)).fetchone():
            raise bridge.PeerError(409, 'This request identifier belongs to another Motet transport.')
        ledger = self.db.execute('SELECT ledger_key FROM shared_bridge_service WHERE singleton=1').fetchone()
        if not ledger or not hex_string(ledger[0]):
            raise bridge.PeerError(503, 'Shared Motet service key is unavailable.')
        payload_hash = bridge.mac(ledger[0], 'watch-inbox-dedupe-v1', canonical(
            dict(inbox_id=inbox_id, inbox_revision=inbox_revision, text=text, declared_sender=sender)).decode('utf-8'))
        old = self.load(request_id, now, required=False)
        if old is not None:
            if not hmac.compare_digest(old['payload_hash'], payload_hash):
                raise bridge.PeerError(409, 'This inbox request identifier was already used for different content or selection.')
            return dict(summary(old, now), duplicate=True)
        expires_at = now + RETENTION_MS
        record = dict(inbox_id=inbox_id, inbox_revision=inbox_revision, request_id=request_id, state='queued',
                      created_at=now, expires_at=expires_at, text_preview=text[:160], declared_sender=sender)
        if not fits(dict(record, state='claimed', text=text, claimed_at=expires_at)):
            raise bridge.PeerError(400, 'Inbox message exceeds the safe claim response size.')
        if self.db.execute('SELECT COUNT(*) FROM peer_inbox_requests').fetchone()[0] >= MAX_TOMBSTONES:
            raise bridge.PeerError(429, 'Inbox request history is full; owner maintenance is required.')
        open_count = self.db.execute('''SELECT COUNT(*) FROM peer_inbox_requests
                    WHERE inbox_id=? AND expires_at>? AND state IN ('queued','claimed')''', (inbox_id, now)).fetchone()[0]
        if open_count >= MAX_OPEN:
            raise bridge.PeerError(429, 'This inbox already has the maximum number of unexpired open requests.')
        native = bridge.transport_metadata(self.db, since=(now - RETENTION_MS) / 1000)
        target = self.db.execute('SELECT COUNT(*) FROM peer_inbox_requests WHERE inbox_id=? AND created_at>?',
                                 (inbox_id, now - HOUR_MS)).fetchone()[0]
        if len(native) + recent_count(self.db, now) >= 100 or target >= 10:
            raise bridge.PeerError(429, 'Shared message rate limit exceeded.')
        self.db.execute('''INSERT INTO peer_inbox_requests VALUES (?,?,?,?,?,?,?,'queued',NULL,NULL,NULL)''',
                        (request_id, inbox_id, inbox_revision, payload_hash, credential_id, now, expires_at))
        self.db.execute('INSERT INTO peer_inbox_payloads VALUES (?,?,?,?,?,NULL)',
                        (request_id, expires_at, text, record['text_preview'], sender))
        return dict(record, duplicate=False)

    def receive(self, method, args, row, now):
        if (row['inbox_id'], row['inbox_revision']) != (args['inbox_id'], args['inbox_revision']):
            raise bridge.PeerError(409, 'This request belongs to a different inbox revision.')
        if now >= row['expires_at'] or row['state'] == 'expired':
            raise bridge.PeerError(410, 'Inbox request has expired.')
        if row['text'] is None:
            raise bridge.PeerError(503, 'Inbox request payload is unavailable.')
        claim_hash = bridge.digest(bytes.fromhex(args['claim_token']))
        matches = row['claim_hash'] is not None and hmac.compare_digest(claim_hash, row['claim_hash'])
        request_id = row['request_id']
        if method == 'claim_inbox_message':
            first = row['state'] == 'queued'
            if not (first or matches):
                raise bridge.PeerError(409, 'Inbox request is already claimed.')
            if row['state'] == 'replied':
                return dict(summary(row, now), claimed_at=row['claimed_at'])
            claimed_at = max(now, row['created_at']) if first else row['claimed_at']
            receipt = dict(summary(row, now, state='claimed'), text=row['text'], claimed_at=claimed_at)
            if not fits(receipt):
                raise bridge.PeerError(503, 'Inbox claim response exceeds safe size.')
            if first:
                self.db.execute("UPDATE peer_inbox_requests SET state='claimed',claimed_at=?,claim_hash=? WHERE request_id=?",
                                (claimed_at, claim_hash, request_id))
            return receipt
        if not matches:
            raise bridge.PeerError(409, 'The original inbox claim token is required to answer.')
        answer = args['answer']
        answer_hash = bridge.digest(answer.encode('utf-8'))
        result = summary(row, now, state='replied')
        if row['state'] == 'replied':
            if not hmac.compare_digest(answer_hash, row['answer_hash']):
                raise bridge.PeerError(409, 'This inbox request already has a different answer.')
            return dict(result, duplicate=True)
        if not fits(dict(result, answer=answer)):
            raise bridge.PeerError(400, 'Inbox answer exceeds the safe reply response size.')
        self.db.execute('UPDATE peer_inbox_payloads SET answer=? WHERE request_id=?', (answer, request_id))
        self.db.execute("UPDATE peer_inbox_requests SET state='replied',answer_hash=? WHERE request_id=?", (answer_hash, request_id))
        return dict(result, duplicate=False)

    def page(self, now, cursor=None, *, receiver=False):
        clauses, params = [], []
        if receiver:
            inbox_id, inbox_revision = self.config['inbox_id'], self.config['inbox_revision']
            clauses.extend(("r.state IN ('queued','claimed')", 'r.inbox_id=?', 'r.inbox_revision=?', 'r.expires_at>?'))
            params.extend((inbox_id, inbox_revision, now))
        if cursor is not None:
            prior = self.db.execute('SELECT created_at,inbox_id,inbox_revision FROM peer_inbox_requests WHERE request_id=?',
                                    (cursor,)).fetchone()
            if prior is None or (receiver and prior[1:] != (inbox_id, inbox_revision)):
                raise bridge.PeerError(400, 'Unknown inbox request cursor.')
            clauses.append('(r.created_at < ? OR (r.created_at=? AND r.request_id<?))')
            params.extend((prior[0], prior[0], cursor))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.db.execute(_SELECT + _JOIN + where + ' ORDER BY r.created_at DESC,r.request_id DESC LIMIT ?',
                               (now, *params, PAGE_ROWS + 1)).fetchall()
        requests = []
        for values in rows[:PAGE_ROWS]:
            item = summary(dict(zip(_FIELDS, values)), now)
            if not fits({'requests': requests + [item], 'next_cursor': item['request_id']}):
                if not requests:
                    raise bridge.PeerError(503, 'Inbox history entry exceeds safe size.')
                break
            requests.append(item)
        return {'requests': requests, 'next_cursor': requests[-1]['request_id'] if len(rows) > len(requests) else None}
