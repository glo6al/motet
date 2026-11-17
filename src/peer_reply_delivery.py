"""Retired reply notices: preserve historical receipts, never inject new prompts."""
import hmac
import json
import os
import stat
import re
import sqlite3
import time
import uuid

import peer_bridge as p

FIELDS = {'version', 'provider', 'native_id', 'source_scope', 'tool_use_id', 'request_id',
          'target_agent_id', 'target_identity_revision', 'created_at', 'args_sha256', 'observed_at', 'duplicate'}
LIVE = ('waiting_reply', 'pending')
RETIRED = 'Automatic reply notices have been removed. Retrieve saved replies with get_reply.'


def stamp(v):
    return type(v) in (int, float) and 0 < v < 253402300800


def printable(v):
    return isinstance(v, str) and 0 < len(v) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def create_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS peer_reply_hook_credentials (
                id TEXT PRIMARY KEY,secret TEXT NOT NULL,active INTEGER NOT NULL,providers TEXT NOT NULL,created REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS peer_reply_nonces (
                credential_id TEXT NOT NULL,nonce TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(credential_id,nonce))''')
    db.execute('''CREATE TABLE IF NOT EXISTS peer_reply_deliveries (
                request_id TEXT PRIMARY KEY,notice_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
                created REAL NOT NULL,checked REAL NOT NULL,data TEXT NOT NULL,pruned INTEGER NOT NULL DEFAULT 0)''')
    db.execute('CREATE INDEX IF NOT EXISTS peer_reply_live ON peer_reply_deliveries(state,checked,created)')


class ReplyDelivery:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.run_id, self.challenges = str(uuid.uuid4()), []
        with p.transaction(self.db, store.lock):
            create_schema(self.db)
            self._revoke()

    def credential(self, ident):
        # Even a restored legacy credential cannot re-enable this retired feature.
        raise p.PeerError(410, RETIRED)

    def setup(self, data, port):
        raise p.PeerError(410, RETIRED)

    def _revoke(self):
        if self.db.execute('SELECT 1 FROM peer_reply_hook_credentials WHERE active<>0 LIMIT 1').fetchone():
            self.db.execute('UPDATE peer_reply_hook_credentials SET active=0 WHERE active<>0')
        for raw, in self.db.execute("SELECT data FROM peer_reply_deliveries WHERE state IN ('waiting_reply','pending')").fetchall():
            row = json.loads(raw); row.update(state='failed', error=RETIRED); self.put(row)
        for raw, in self.db.execute("SELECT data FROM peer_reply_deliveries WHERE state='dispatching'").fetchall():
            row = json.loads(raw)
            row.update(state='unknown', error='Reply notices were removed after admission; native delivery is unknown and will not be retried.')
            self.put(row)

    def revoke(self):
        with p.transaction(self.db, self.store.lock):
            self._revoke()
        return {'revoked': True}

    def status(self):
        with self.store.lock:
            counts = dict(self.db.execute('SELECT state,COUNT(*) FROM peer_reply_deliveries GROUP BY state'))
        return dict(enabled=False, retired=True, providers=[], source_scope='native_parent', consent_version=1, notices=counts)

    def get(self, request_id):
        row = self.db.execute('SELECT data FROM peer_reply_deliveries WHERE request_id=?', (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, row):
        self.db.execute('UPDATE peer_reply_deliveries SET state=?,data=? WHERE request_id=?',
                        (row['state'], json.dumps(row), row['request_id']))

    def challenge(self, data, port):
        if (not isinstance(data, dict) or set(data) != {'credential_id', 'nonce'}
                or not p.is_uuid(data['credential_id']) or not isinstance(data['nonce'], str) or not p.HEX.fullmatch(data['nonce'])):
            raise p.PeerError(403, 'Invalid reply-hook challenge.')
        with self.store.lock:
            key = self.credential(data['credential_id']); now = time.monotonic()
            self.challenges = [(i, t) for i, t in self.challenges if t > now - 60]
            if len(self.challenges) >= 4096 or sum(i == key['id'] for i, _ in self.challenges) >= 120:
                raise p.PeerError(429, 'Reply-hook challenge limit reached.')
            self.challenges.append((key['id'], now))
            return dict(run_id=self.run_id, proof=p.mac(key['secret'], 'watch-peer-reply-server-v1', port, key['id'], data['nonce'], self.run_id))

    def authenticate(self, headers, raw, connection_id, port):
        ident = headers.get('X-Watch-Reply', '')
        at, nonce, run, signature = (headers.get(k, '') for k in ('X-Watch-Time', 'X-Watch-Nonce', 'X-Watch-Run', 'X-Watch-Signature'))
        if (ident != connection_id or not p.is_uuid(ident) or not re.fullmatch(r'[0-9]{1,12}', at)
                or abs(time.time() - int(at)) > 30 or not p.HEX.fullmatch(nonce)
                or run != self.run_id or not p.HEX.fullmatch(signature)):
            raise p.PeerError(403, 'Invalid reply-hook authentication.')
        with p.transaction(self.db, self.store.lock):
            key = self.credential(ident)
            expected = p.mac(key['secret'], 'watch-peer-reply-request-v1', port, 'POST', '/peer-reply/v1/receipt', at, nonce, run, p.digest(raw))
            if not hmac.compare_digest(expected, signature):
                raise p.PeerError(403, 'Invalid reply-hook authentication.')
            self.db.execute('DELETE FROM peer_reply_nonces WHERE created<?', (time.time() - 120,))
            if self.db.execute('SELECT COUNT(*) FROM peer_reply_nonces').fetchone()[0] >= 4096:
                raise p.PeerError(429, 'Reply-hook authentication limit reached.')
            try:
                self.db.execute('INSERT INTO peer_reply_nonces VALUES (?,?,?)', (ident, nonce, time.time()))
            except sqlite3.IntegrityError:
                raise p.PeerError(403, 'Reply-hook authentication was already used.') from None
        return key, nonce

    def validate(self, data):
        if (not isinstance(data, dict) or not FIELDS <= set(data) <= FIELDS | {'turn_id'}
                or type(data['version']) is not int or data['version'] != 1 or data['provider'] not in ('codex', 'claude')
                or data['source_scope'] != 'native_parent' or data['duplicate'] is not False
                or not p.is_uuid(data['native_id']) or not p.valid_request_id(data['request_id'])
                or not printable(data['tool_use_id']) or ('turn_id' in data and not printable(data['turn_id']))
                or not isinstance(data['target_agent_id'], str) or data['target_agent_id'][:2] not in ('x:', 'c:')
                or not p.is_uuid(data['target_agent_id'][2:]) or not stamp(data['created_at']) or not stamp(data['observed_at'])
                or any(not isinstance(data[k], str) or not p.HEX.fullmatch(data[k]) for k in ('args_sha256', 'target_identity_revision'))):
            raise p.PeerError(400, 'Invalid successful original-send receipt.')

    def ledger(self, key, receipt):
        key = self.credential(key['id'])
        row = self.db.execute('''SELECT r.target,r.target_snapshot,r.created,r.credential_id,a.args_sha256
            FROM shared_bridge_requests r JOIN peer_reply_admissions a ON a.request_id=r.client_id WHERE r.client_id=?''', (receipt['request_id'],)).fetchone()
        if (not row or receipt['provider'] not in key['providers'] or row[2] < key['created']
                or row[0] != receipt['target_agent_id'] or row[2] != receipt['created_at']
                or row[4] != receipt['args_sha256'] or json.loads(row[1])['identity_revision'] != receipt['target_identity_revision']):
            raise p.PeerError(409, 'Receipt does not match an eligible original admission.')
        p.service_credential(self.db, row[3])
        return row

    def prepare(self, source_id, expected=None):
        target = self.store.shared.source_target(source_id)
        if expected and (target['identity'] != expected['identity'] or target['project'] != expected['project']):
            raise p.PeerError(409, 'The original notice destination changed.')
        if target['provider'] == 'claude':
            proof = self.store.shared.metadata_target(source_id, target['project'])
            ready, entry, _ = self.store.mods.peer_capability(source_id, dict(runtime='Claude', cwd=target['project']))
            if (not proof or proof.get('agent') != source_id or proof.get('project') != target['project']
                    or any(proof.get('identity', {}).get(k) != target['identity'].get(k) for k in ('path', 'device', 'inode'))
                    or not ready or not entry['grant_id'] or entry['reply_notice_protocol'] != 1
                    or target['identity'].get('mod_enrollment') != entry['id']):
                raise p.PeerError(409, 'The Claude notice destination is not ready.')
            transport = dict(runtime='claude', transport='claude_mod', mod_enrollment=entry['id'], mod_project=entry['project'])
        else:
            if target['capability'] != 'reachable':
                raise p.PeerError(409, 'The Codex notice destination is not ready.')
            transport = self.store.shared._preflight(target)
        return dict(target=target, transport=transport, metadata_proof=proof if target['provider'] == 'claude' else None)

    def observe(self, key, data):
        raise p.PeerError(410, RETIRED)

    def ready(self, row):
        self.ledger(dict(id=row['hook_id']), row['receipt'])
        result = self.store.shared.result(row['request_id'])
        if result['state'] != 'answered':
            return False
        if row['ready_at'] is None:
            row['ready_at'] = time.time()
        if time.time() >= min(row['created'] + p.RETENTION, row['ready_at'] + p.TTL):
            raise p.PeerError(409, 'Reply notice expired before admission.')
        return True

    def candidates(self, agent):
        return []

    def busy(self, enrollment):
        return False

    def claim_in_transaction(self, entry, prepared):
        # Historical Claude acknowledgments remain accepted; claims never resume.
        return None

    def event_in_transaction(self, entry, data):
        raw = self.db.execute('SELECT request_id FROM peer_reply_deliveries WHERE notice_id=?', (data.get('request_id'),)).fetchone()
        if not raw:
            return None
        row = self.get(raw[0]); kind = data.get('kind')
        fields = {'generation', 'native_id', 'request_id', 'attempt_id', 'kind'} | ({'client_id'} if entry['grant_id'] else set())
        if (set(data) != fields or kind not in ('submitted', 'dropped', 'unknown')
                or not row.get('attempted_at')
                or data['attempt_id'] != row['attempt_id']):
            raise p.PeerError(409, 'Notice acknowledgment does not match its original admission.')
        if row.get('ack_binding'):
            expected = p.digest((entry['id'] + '\0' + data['attempt_id']).encode())
            if not hmac.compare_digest(row['ack_binding'], expected):
                raise p.PeerError(409, 'Notice acknowledgment does not match its original admission.')
            return dict(request_id=row['notice_id'], state=row['state'], turn_id='')
        if (row.get('mod_enrollment') != entry['id'] or row['source']['project'] != entry['project']
                or row['source']['agent_id'] != entry['agent']):
            raise p.PeerError(409, 'Notice acknowledgment does not match its original admission.')
        if row['state'] != 'queued':
            row.update(state={'submitted': 'queued', 'dropped': 'failed', 'unknown': 'unknown'}[kind], error='')
            self.put(row)
        return dict(request_id=row['notice_id'], state=row['state'], turn_id='')

    def statuses(self, entry):
        values = []
        for raw, in self.db.execute("SELECT data FROM peer_reply_deliveries WHERE state IN ('dispatching','queued','unknown','failed') ORDER BY created DESC LIMIT 256"):
            row = json.loads(raw)
            if row.get('mod_enrollment') == entry['id'] and row.get('attempt_id'):
                values.append(dict(request_id=row['notice_id'], attempt_id=row['attempt_id'], state=row['state'], turn_id='', error=row.get('error', '')))
        return values[:32]

    def tick(self):
        # Retirement and pruning are maintenance only; contention cannot block requests.
        with self.store.lock:
            try:
                with self.db:
                    self._revoke()
                    # Payload privacy expires in every state; durable IDs prevent reopening.
                    expired = self.db.execute('SELECT request_id,data FROM peer_reply_deliveries WHERE created<? AND pruned=0 LIMIT 32', (time.time() - p.RETENTION,)).fetchall()
                    for rid, raw in expired:
                        old = json.loads(raw)
                        tombstone = {k: old.get(k) for k in ('request_id','notice_id','hook_id','receipt_hash','attempt_id','attempted_at')}
                        if old.get('mod_enrollment') and old.get('attempt_id'):
                            tombstone['ack_binding'] = p.digest((old['mod_enrollment'] + '\0' + old['attempt_id']).encode())
                        tombstone['state'] = 'failed' if old['state'] in LIVE else 'unknown' if old['state'] == 'dispatching' else old['state']
                        self.put(tombstone)
                        self.db.execute('UPDATE peer_reply_deliveries SET pruned=1 WHERE request_id=?', (rid,))
            except sqlite3.OperationalError:
                self.db.rollback()


def handle_http(handler, delivery):
    handler.connection.settimeout(10)
    route, headers, port = handler.path, handler.headers, handler.server.server_address[1]
    if (route not in ('/peer-reply/v1/challenge', '/peer-reply/v1/receipt')
            or headers.get_all('Host') != ['127.0.0.1:' + str(port)] or headers.get('Origin') is not None
            or headers.get('Transfer-Encoding') is not None or len(headers.get_all('Content-Length') or []) != 1):
        handler.close_connection = True
        return handler.send(403, b'{"error":"Invalid reply-hook request."}', 'application/json')
    key, nonce = None, None
    try:
        size = int(headers['Content-Length'])
        if not 1 <= size <= (1024 if route.endswith('challenge') else 4096):
            raise p.PeerError(413, 'Reply-hook request is too large.')
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise p.PeerError(400, 'Incomplete reply-hook request.')
        def unique(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError('Duplicate reply-hook field')
                out[key] = value
            return out
        data = json.loads(raw, object_pairs_hook=unique)
        if route.endswith('/challenge'):
            result = delivery.challenge(data, port)
            handler.reply_hook_credential = data['credential_id']; handler.reply_hook_nonce = data['nonce']; handler.reply_hook_at = time.monotonic()
            return handler.send(200, json.dumps(result).encode(), 'application/json')
        if time.monotonic() - getattr(handler, 'reply_hook_at', 0) > 10:
            raise p.PeerError(403, 'Reply-hook server proof is required on this connection.')
        if any(len(headers.get_all(k) or []) != 1 for k in ('X-Watch-Reply', 'X-Watch-Time', 'X-Watch-Nonce', 'X-Watch-Run', 'X-Watch-Signature')):
            raise p.PeerError(403, 'Invalid reply-hook authentication.')
        if headers.get('X-Watch-Nonce') != getattr(handler, 'reply_hook_nonce', None):
            raise p.PeerError(403, 'Reply-hook nonce does not match this connection proof.')
        key, nonce = delivery.authenticate(headers, raw, getattr(handler, 'reply_hook_credential', None), port)
        handler.reply_hook_credential = None
        response, status = delivery.observe(key, data), 200
    except p.PeerError as error:
        response, status = {'error': str(error)}, error.status
    except sqlite3.OperationalError:
        response, status = {'error': 'Reply-notice storage is busy.'}, 503
    except Exception:
        response, status = {'error': 'Invalid or unavailable reply-hook request.'}, 400
    raw_response = json.dumps(response).encode(); extra = ()
    if key and nonce:
        extra = (('X-Watch-Signature', p.mac(key['secret'], 'watch-peer-reply-response-v1', port, delivery.run_id, nonce, status, p.digest(raw_response))),)
    handler.send(status, raw_response, 'application/json', extra)


def _offline(state_dir, action):
    """Hold the existing private lifetime lock through DB and caller filesystem work."""
    if os.name == 'nt':
        raise ValueError('Reply-hook provisioning requires POSIX private storage.')
    import fcntl
    directory = p.private_directory(state_dir, True)
    fd = os.open(os.path.join(directory, 'officer-messages.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    db, undo = None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Invalid reply-hook lifetime lock.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Stop Motet before provisioning reply hooks.') from None
        db = p.open_database(directory)
        with p.transaction(db):
            create_schema(db)
            result, undo = action(directory, db)
        return result
    except BaseException:
        if undo is not None:
            undo()  # Restore only the installer's exact writes, before releasing ownership.
        raise
    finally:
        if db is not None:
            db.close()
        os.close(fd)


def provision(state_dir, providers, port=8793, install=None):
    # Reject before creating a lock, database, credential file, or installer callback.
    raise ValueError(RETIRED)


def revoke_offline(state_dir, credential_id=None, install=None):
    def revoke(directory, db):
        if credential_id is not None and not p.is_uuid(credential_id):
            raise ValueError('Invalid reply-hook credential identifier.')
        manager = ReplyDelivery.__new__(ReplyDelivery); manager.db = db
        if credential_id is None:
            manager._revoke()
        else:
            db.execute('UPDATE peer_reply_hook_credentials SET active=0 WHERE id=?', (credential_id,))
            for raw, in db.execute("SELECT data FROM peer_reply_deliveries WHERE state IN ('waiting_reply','pending')").fetchall():
                row = json.loads(raw)
                if row['hook_id'] == credential_id:
                    row.update(state='failed', error='Automatic reply consent was revoked before admission.'); manager.put(row)
        undo_install = install() if install is not None else None
        return {'revoked': True}, undo_install if callable(undo_install) else None
    return _offline(state_dir, revoke)
