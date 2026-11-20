"""Scoped owner and shared-v2 peer official-mod delivery. No native credentials or native IPC.

SQLite reservations precede returning a claim. Claims never replay; only status
and completion events may replay. The official host owns native input execution.
"""
import hashlib
import hmac
import json
import os
import peer_bridge
import peer_lifecycle
import peer_progress
import secrets
import time
import uuid

LEASE, PAIR_TTL, TTL, RETENTION = 12, 300, 900, 86400
TRANSPORT = 'claude_mod'
PEER_UNAVAILABLE = 'Agent receiving requires explicit Motet setup and a compatible live official plugin in this exact Claude chat.'
MAX_ENROLLMENTS = 256


class ModError(Exception):
    def __init__(self, status, message, code='invalid_request'):
        self.status = status
        self.code = code
        super().__init__(message)


class ReceiptError(ModError):
    def __init__(self, status, message):
        super().__init__(status, message, 'receipt_rejected')


class EmptyReplyError(ReceiptError):
    pass


def ident(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def turn_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def reply_text(answer, request_id):
    if (not isinstance(answer, str) or len(answer) > 16000 or '\x00' in answer
            or any(0xD800 <= ord(c) <= 0xDFFF for c in answer)):
        raise ReceiptError(400, 'Invalid Claude reply.')
    lines = answer.splitlines()
    marker = 'WATCH_REPLY ' + request_id
    if [line for line in lines if 'WATCH_REPLY' in line] != [marker]:
        raise ReceiptError(409, 'Claude reply has missing or ambiguous correlation markers.')
    reply = '\n'.join(line for line in lines if line != marker).strip()
    if not reply:
        raise EmptyReplyError(409, 'Claude reply has no answer body.')
    return reply


def schema(data, required):
    if not isinstance(data, dict) or set(data) != set(required):
        raise ModError(400, 'Invalid Claude mod request fields.')


def project_text(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and os.path.isabs(value) and '\x00' not in value


def project(value):
    return project_text(value) and os.path.realpath(value) == value


def local_parent(agent, item):
    """Local observation is required; registration never creates room members."""
    return (isinstance(agent, str) and agent.startswith('c:') and ident(agent[2:])
            and isinstance(item, dict) and item.get('runtime') == 'Claude'
            and not any(item.get(k) for k in ('observed', 'sub', 'parent')))


def eligible(agent, item):
    return local_parent(agent, item) and project_text(item.get('cwd'))


class ClaudeMod:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.room_snapshot = None
        self.attachment_mount = None
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_pairs (
            code_hash TEXT PRIMARY KEY, agent TEXT NOT NULL, project TEXT NOT NULL, expires REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_enrollments (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, agent TEXT NOT NULL,
            project TEXT NOT NULL, generation TEXT, last_seen REAL NOT NULL, active INTEGER NOT NULL)''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(claude_mod_enrollments)')}
        for name, kind in (('grant_id', 'TEXT'), ('client_id', 'TEXT'), ('connection_seq', 'INTEGER'),
                           ('peer_protocol', 'INTEGER NOT NULL DEFAULT 0'),
                           ('reply_notice_protocol', 'INTEGER NOT NULL DEFAULT 0'),
                           ('lifecycle_protocol', 'INTEGER NOT NULL DEFAULT 0'),
                           ('peer_envelope_limit', 'INTEGER NOT NULL DEFAULT 4000')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE claude_mod_enrollments ADD COLUMN {name} {kind}')
        self.db.execute('CREATE INDEX IF NOT EXISTS claude_mod_agents ON claude_mod_enrollments(agent,active)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS claude_mod_active_agent ON claude_mod_enrollments(agent) WHERE active=1')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_setup_codes (
            code_hash TEXT PRIMARY KEY, expires REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_grants (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, active INTEGER NOT NULL)''')
        for table in ('claude_mod_setup_codes', 'claude_mod_grants'):
            columns = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
            if 'peer_receive' not in columns:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN peer_receive INTEGER NOT NULL DEFAULT 0')
        # Connections cannot survive a WATCH process restart. Original receipts do.
        self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0')
        for row in self.rows():
            if row.get('launched') and not row.get('settled') and row['state'] not in ('answered', 'failed'):
                row.update(state='unknown', error='Motet restarted after a Claude mod claim. It will not resubmit.')
                self.put(row)
        self.db.commit()

    def rows(self):
        return [r for r in self.store._rows() if r.get('transport') == TRANSPORT
                and (r.get('origin') in (None, 'owner') or self.peer_row(r))]

    def peer_row(self, row):
        # Origin in a mutable JSON payload alone never authorizes shared receiving.
        if (row.get('origin') != 'peer' or row.get('bridge_version') != 2
                or row.get('runtime') != 'claude' or row.get('transport') != TRANSPORT
                or row.get('mod_project') != row.get('target_project')):
            return False
        ledger = self.db.execute('SELECT target FROM shared_bridge_requests WHERE transport_id=?',
                                 (row['request_id'],)).fetchone()
        return bool(ledger and ledger[0] == row.get('agent') and ident(row.get('mod_enrollment')))

    def put(self, row):
        peer_lifecycle.reconcile(self.db, row)
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (row['request_id'], json.dumps(row)))
        peer_progress.ensure(self.store, row)

    def uncertain(self, enrollment, reason):
        for row in self.rows():
            if row.get('mod_enrollment') == enrollment and row.get('launched') and row['state'] in ('sending', 'queued', 'awaiting_reply'):
                row.update(state='unknown', error=reason)
                self.put(row)

    def maintain(self):
        now = time.time()
        self.db.execute('DELETE FROM claude_mod_pairs WHERE expires<=?', (now,))
        self.db.execute('DELETE FROM claude_mod_setup_codes WHERE expires<=?', (now,))
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE generation IS NOT NULL AND last_seen<?', (now - LEASE,)).fetchall():
            self.uncertain(eid, 'The Claude mod disconnected. Delivery is unknown; nothing was resubmitted.')
            self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL WHERE id=?', (eid,))
        for row in self.rows():
            if row.get('settled') or row['state'] in ('answered', 'failed'):
                continue
            if not row.get('launched') and now >= row['created'] + TTL:
                row.update(state='failed', error='This message expired before a Claude mod claimed it. Nothing was submitted.')
                self.put(row)
            elif row.get('launched') and row['state'] in ('queued', 'awaiting_reply') and now >= row['created'] + TTL:
                row.update(state='expired', error='No matching reply yet. Native input may still run; Motet will not resubmit.')
                self.put(row)

    def pair(self, agent, agents, port):
        item = agents.get(agent) if isinstance(agent, str) else None
        if not eligible(agent, item) or not project(item['cwd']):
            raise ModError(400, 'Choose an existing local parent Claude chat with a known project.')
        code, expiry = secrets.token_urlsafe(24), time.time() + PAIR_TTL
        with self.store.lock, self.db:
            self.maintain()
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_pairs').fetchone()[0] >= 32:
                raise ModError(429, 'Too many pending Claude pairing codes.')
            self.db.execute('DELETE FROM claude_mod_pairs WHERE agent=?', (agent,))
            self.db.execute('INSERT INTO claude_mod_pairs VALUES (?,?,?,?)', (digest(code), agent, item['cwd'], expiry))
        return dict(agent=agent, port=port, pair_code=code, expires_at=expiry, command=f'/watch-connect {port}:{code}')

    def session(self, entry):
        # Notice negotiation is historical ACK cleanup only; polls never create
        # or claim notice prompts. Retained old clients can drain their receipts.
        result = dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], heartbeat_interval_ms=2000, lease_seconds=LEASE, peer_protocol=entry['peer_protocol'],
                      reply_notice_protocol=entry['reply_notice_protocol'], supported_reply_notice_protocol=1,
                      lifecycle_protocol=entry['lifecycle_protocol'], supported_lifecycle_protocol=2,
                      peer_envelope_limit=entry['peer_envelope_limit'], supported_peer_envelope_limit=100000)
        if entry['grant_id']:
            result.update(grant_id=entry['grant_id'], client_id=entry['client_id'], connection_seq=entry['connection_seq'])
        return result

    def get(self, eid):
        fields = ('id', 'agent', 'project', 'generation', 'last_seen', 'active', 'grant_id', 'client_id', 'connection_seq', 'peer_protocol', 'reply_notice_protocol', 'lifecycle_protocol', 'peer_envelope_limit')
        row = self.db.execute('SELECT '+','.join(fields)+' FROM claude_mod_enrollments WHERE id=?', (eid,)).fetchone()
        return dict(zip(fields, row)) if row else None

    def setup_code(self, port, peer_receive=False):
        if type(peer_receive) is not bool:
            raise ModError(400, 'Invalid Claude peer receiving consent.')
        code, expiry = secrets.token_urlsafe(24), time.time() + PAIR_TTL
        with self.store.lock, self.db:
            self.maintain()
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_setup_codes').fetchone()[0] >= 32:
                raise ModError(429, 'Too many pending Claude setup codes.')
            self.db.execute('INSERT INTO claude_mod_setup_codes (code_hash,expires,peer_receive) VALUES (?,?,?)',
                            (digest(code), expiry, int(peer_receive)))
        return dict(version=1, port=port, pair_code=code, expires_at=expiry, command=f'/watch-setup {port}:{code}', peer_receive=peer_receive)

    def setup_status(self, agents=None):
        with self.store.lock, self.db:
            self.maintain()
            grant = self.db.execute('SELECT peer_receive FROM claude_mod_grants WHERE active=1').fetchone()
            authorized = bool(grant)
            entries = [self.get(r[0]) for r in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1 AND grant_id IS NOT NULL AND generation IS NOT NULL')]
            connected = sum(1 for e in entries if not self.store.worker_failed and e['last_seen'] > time.time() - LEASE
                            and self.db.execute('SELECT 1 FROM claude_mod_grants WHERE id=? AND active=1', (e['grant_id'],)).fetchone()
                            and not self.room_veto(e['agent'], e['project']) and
                            (agents is None or e['agent'] not in agents or eligible(e['agent'], agents[e['agent']])
                             and agents[e['agent']]['cwd'] == e['project']))
            return dict(version=1, authorized=authorized, connected_chats=connected, peer_receive=bool(grant and grant[0] == 1))

    def _revoke(self):
        entries = [r[0] for r in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1 AND grant_id IS NOT NULL')]
        for eid in entries:
            self.uncertain(eid, 'Automatic Claude access was revoked. Earlier input may still run; nothing was resubmitted.')
        for row in self.rows():
            if row.get('mod_enrollment') in entries and not row.get('launched') and row['state'] == 'sending':
                row.update(state='failed', error='Automatic Claude access was revoked before claim. Nothing was submitted.')
                self.put(row)
        self.db.execute('UPDATE claude_mod_grants SET active=0')
        self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL,last_seen=0 WHERE grant_id IS NOT NULL')
        self.db.execute('DELETE FROM claude_mod_setup_codes')

    def revoke(self):
        with self.store.lock, self.db:
            self._revoke()
        return dict(version=1, revoked=True)

    def setup(self, data, port):
        schema(data, ('code',))
        code = data['code']
        if not isinstance(code, str) or not 16 <= len(code) <= 100:
            raise ModError(403, 'Claude setup code is expired or used.', 'access_denied')
        row = self.db.execute('SELECT expires,peer_receive FROM claude_mod_setup_codes WHERE code_hash=?', (digest(code),)).fetchone()
        if not row or row[0] <= time.time():
            raise ModError(403, 'Claude setup code is expired or used.', 'access_denied')
        # Issuing a code is harmless; only its explicit consumption replaces access.
        self._revoke()
        gid, grant = str(uuid.uuid4()), secrets.token_hex(32)
        self.db.execute('INSERT INTO claude_mod_grants (id,token_hash,created,active,peer_receive) VALUES (?,?,?,1,?)',
                        (gid, digest(grant), time.time(), row[1]))
        return dict(version=1, grant_id=gid, grant=grant, port=port)

    def registration_access(self, data, token):
        fields = ('native_id', 'project', 'client_id', 'connection_seq')
        schema(data, fields + tuple(k for k in ('peer_protocol', 'reply_notice_protocol', 'lifecycle_protocol', 'peer_envelope_limit') if isinstance(data, dict) and k in data))
        if type(data.get('reply_notice_protocol', 0)) is not int or data.get('reply_notice_protocol', 0) not in (0, 1):
            raise ModError(400, 'Unsupported reply-notice protocol.')
        if type(data.get('lifecycle_protocol', 0)) is not int or data.get('lifecycle_protocol', 0) not in (0, 2):
            raise ModError(400, 'Unsupported Claude lifecycle protocol.')
        if type(data.get('peer_envelope_limit', 4000)) is not int or data.get('peer_envelope_limit', 4000) not in (4000, 100000):
            raise ModError(400, 'Unsupported Claude peer envelope capacity.')
        protocol = data.get('peer_protocol', 0)
        if type(protocol) is not int or protocol not in (0, 1):
            raise ModError(400, 'Unsupported Claude peer protocol.')
        if not isinstance(token, str) or len(token) != 64:
            raise ModError(403, 'Claude registration access is invalid.', 'access_denied')
        grant = self.db.execute('SELECT id,token_hash FROM claude_mod_grants WHERE active=1 AND token_hash=?', (digest(token),)).fetchone()
        if not grant or not hmac.compare_digest(grant[1], digest(token)):
            raise ModError(403, 'Claude registration access is invalid.', 'access_denied')
        seq = data['connection_seq']
        if (not ident(data['native_id']) or not ident(data['client_id']) or not project_text(data['project'])
                or type(seq) is not int or not 0 <= seq <= 9007199254740991):
            raise ModError(400, 'Invalid Claude registration identity.', 'identity_rejected')
        return grant

    @staticmethod
    def metadata_item(proof):
        return dict(runtime='Claude', cwd=proof['project'], name=proof['name'])

    def register(self, data, token, agents, proof=None):
        grant = self.registration_access(data, token)
        protocol, seq = data.get('peer_protocol', 0), data['connection_seq']
        aid, gid = 'c:'+data['native_id'], grant[0]
        item = agents.get(aid) if isinstance(agents, dict) else None
        if isinstance(agents, dict) and aid in agents:
            if not local_parent(aid, item):
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
            if not item.get('cwd'):
                raise ModError(409, 'Waiting for Motet to observe this local Claude project.', 'not_observed')
            if not eligible(aid, item) or item['cwd'] != data['project']:
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
        elif (not proof or proof['agent'] != aid or proof['project'] != data['project']
              or not self.store.shared.metadata_current(proof)):
            raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
        active = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (aid,)).fetchone()
        entry = self.get(active[0]) if active else None
        if entry and not entry['grant_id']:
            raise ModError(409, 'This chat retains its manual Motet connection.', 'manual_conflict')
        if entry and (entry['grant_id'] != gid or entry['project'] != data['project']):
            raise ModError(409, 'This chat retains a different Motet enrollment.', 'identity_rejected')
        payload = json.dumps(['watch-claude-session-v1', data['native_id'], data['project']], separators=(',', ':'), ensure_ascii=True)
        service_token = hmac.new(token.encode('ascii'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        if entry:
            same_client = entry['client_id'] == data['client_id']
            if same_client and seq < entry['connection_seq']:
                raise ModError(409, 'This registration episode is stale.', 'stale_connection')
            if not same_client and entry['generation']:
                raise ModError(409, 'Another Claude host currently owns this chat connection.', 'host_conflict')
            if same_client and seq == entry['connection_seq'] and (protocol != entry['peer_protocol'] or data.get('reply_notice_protocol', 0) != entry['reply_notice_protocol'] or data.get('lifecycle_protocol', 0) != entry['lifecycle_protocol'] or data.get('peer_envelope_limit', 4000) != entry['peer_envelope_limit']):
                raise ModError(409, 'This registration episode has a different peer protocol.', 'stale_connection')
            if not (same_client and seq == entry['connection_seq'] and entry['generation']):
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,client_id=?,connection_seq=?,last_seen=?,peer_protocol=?,reply_notice_protocol=?,lifecycle_protocol=?,peer_envelope_limit=? WHERE id=?',
                                (str(uuid.uuid4()), data['client_id'], seq, time.time(), protocol, data.get('reply_notice_protocol', 0), data.get('lifecycle_protocol', 0), data.get('peer_envelope_limit', 4000), entry['id']))
        else:
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0] >= MAX_ENROLLMENTS:
                raise ModError(429, 'Too many active Claude connections.', 'connection_limit')
            eid = str(uuid.uuid4())
            self.db.execute('''INSERT INTO claude_mod_enrollments
                (id,token_hash,agent,project,generation,last_seen,active,grant_id,client_id,connection_seq,peer_protocol,reply_notice_protocol,lifecycle_protocol,peer_envelope_limit)
                VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?)''', (eid, digest(service_token), aid, data['project'], str(uuid.uuid4()),
                                               time.time(), gid, data['client_id'], seq, protocol, data.get('reply_notice_protocol', 0), data.get('lifecycle_protocol', 0), data.get('peer_envelope_limit', 4000)))
            entry = self.get(eid)
        entry = self.get(entry['id'])
        peer_lifecycle.record_event(self.db, 'enrollment:' + entry['id'], 'registered', 'connection_episode', '', entry['generation'],
                                    event_id='register:' + entry['id'] + ':' + entry['generation'])
        return dict(self.session(entry), enrollment_id=entry['id'], token=service_token)

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) != 64:
            raise ModError(403, 'Claude mod service access is invalid.')
        row = self.db.execute('SELECT id,token_hash FROM claude_mod_enrollments WHERE active=1 AND token_hash=?', (digest(token),)).fetchone()
        if not row or not hmac.compare_digest(row[1], digest(token)):
            raise ModError(403, 'Claude mod service access is invalid.')
        return self.get(row[0])

    def native(self, entry, data, connected=True):
        if data.get('native_id') != entry['agent'][2:]:
            raise ModError(409, 'The enrolled Claude conversation does not match.')
        if connected and (not entry['generation'] or data.get('generation') != entry['generation']):
            raise ModError(409, 'Reconnect the Claude mod before receiving more messages.', 'stale_connection')
        if entry['grant_id'] and data.get('client_id') != entry['client_id']:
            raise ModError(409, 'Another Claude runtime owns this connection.', 'host_conflict')

    def fields(self, entry, fields):
        return fields + (('client_id',) if entry['grant_id'] else ())

    def room_veto(self, agent, project):
        """Trusted pure in-memory callback; no discovery or filesystem work."""
        current = self.room_snapshot() if self.room_snapshot else {}
        return agent in current and (not local_parent(agent, current[agent]) or current[agent].get('cwd') != project)

    def capability(self, agent, item=None):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = bool(entry and entry['generation'] and entry['last_seen'] > time.time() - LEASE and not self.store.worker_failed)
            if entry and self.room_veto(agent, entry['project']):
                ready = False
            if item is not None and entry and (not eligible(agent, item) or item.get('cwd') != entry['project']):
                ready = False
            return ready, entry, ('' if ready else 'Connect the official Motet plugin in this exact existing Claude chat.')

    def peer_ready(self, entry, item=None):
        """Transaction-owned facts only: no lock, filesystem, maintenance or commit."""
        if (not entry or not entry['active'] or not entry['grant_id'] or entry['peer_protocol'] != 1
                or not entry['generation'] or entry['last_seen'] <= time.time() - LEASE or self.store.worker_failed
                or self.room_veto(entry['agent'], entry['project'])):
            return False
        grant = self.db.execute('SELECT peer_receive FROM claude_mod_grants WHERE id=? AND active=1',
                                (entry['grant_id'],)).fetchone()
        return bool(grant and grant[0] == 1 and (item is None or
                    local_parent(entry['agent'], item) and item.get('cwd') == entry['project']))

    def peer_capability(self, agent, item):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = self.peer_ready(entry, item)
            return ready, entry, ('' if ready else 'Agent receiving requires explicit Motet setup and a compatible live official plugin in this exact Claude chat.')

    def any_ready(self):
        with self.store.lock, self.db:
            self.maintain()
            return not self.store.worker_failed and bool(self.db.execute('SELECT 1 FROM claude_mod_enrollments WHERE active=1 AND generation IS NOT NULL AND last_seen>?', (time.time()-LEASE,)).fetchone())

    def enroll(self, data):
        schema(data, ('code', 'native_id', 'project'))
        if not isinstance(data['code'], str) or len(data['code']) > 100 or not ident(data['native_id']) or not project(data['project']):
            raise ModError(400, 'Invalid Claude enrollment identity.')
        pair = self.db.execute('SELECT agent,project,expires FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),)).fetchone()
        if not pair or pair[2] <= time.time() or pair[:2] != ('c:'+data['native_id'], data['project']):
            raise ModError(403, 'Claude pairing code is expired, used, or does not match this chat.')
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (pair[0],)).fetchall():
            self.uncertain(eid, 'This Claude enrollment was replaced. Earlier input may still run; nothing was resubmitted.')
        self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL WHERE agent=?', (pair[0],))
        if self.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0] >= MAX_ENROLLMENTS:
            raise ModError(429, 'Too many active Claude connections.', 'connection_limit')
        eid, token, generation = str(uuid.uuid4()), secrets.token_hex(32), str(uuid.uuid4())
        self.db.execute('INSERT INTO claude_mod_enrollments (id,token_hash,agent,project,generation,last_seen,active) VALUES (?,?,?,?,?,?,1)', (eid, digest(token), pair[0], pair[1], generation, time.time()))
        self.db.execute('DELETE FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),))
        result = self.session(self.get(eid))
        return dict(result, enrollment_id=eid, token=token)

    def statuses(self, entry):
        notices = self.store.reply_delivery.statuses(entry) if hasattr(self.store, 'reply_delivery') and entry['reply_notice_protocol'] == 1 else []
        return notices + [dict(request_id=r['request_id'], attempt_id=r['mod_attempt_id'], state=r['state'], turn_id=r.get('mod_turn', ''), error=r.get('error', ''))
                for r in sorted(self.rows(), key=lambda r: r['created']) if r.get('mod_enrollment') == entry['id'] and r.get('mod_attempt_id')][-32:]

    def recoverable_turns(self, entry):
        # No message content. A post-reload observer must also match its own
        # retained receipt to this authenticated original turn.
        if entry['lifecycle_protocol'] != 2 or not entry['grant_id']:
            return []
        project_digest = digest(entry['project'])
        turns = []
        for request_id, execution, created in peer_lifecycle.blockers(self.db, entry['agent']):
            if execution not in ('uncertain', 'running'):
                continue
            admission = peer_lifecycle.exact_admission(self.db, request_id)
            if (not admission or admission[:2] != (entry['agent'], created)
                    or admission[2] is None or admission[3] != 2):
                continue
            evidence = peer_lifecycle.get(self.db, request_id)['evidence']
            if (evidence.get('launched') is not True or evidence.get('lifecycle_protocol') != 2
                    or evidence.get('project_digest') != project_digest or evidence.get('mod_enrollment') != entry['id']
                    or not ident(evidence.get('mod_attempt_id')) or not ident(evidence.get('mod_generation'))
                    or not turn_id(evidence.get('mod_turn'))):
                continue
            turns.append(dict(request_id=request_id, attempt_id=evidence['mod_attempt_id'],
                              generation=evidence['mod_generation'], turn_id=evidence['mod_turn']))
            if len(turns) == 32:
                break
        return turns

    def validate_poll(self, entry, data):
        schema(data, self.fields(entry, ('generation', 'native_id', 'project', 'accept')))
        self.native(entry, data)
        if data['project'] != entry['project'] or type(data['accept']) is not bool:
            raise ModError(409, 'The enrolled Claude project does not match.')

    def attachment_entry(self, agent, item):
        """Current derived owner enrollment, without maintenance or discovery."""
        if not eligible(agent, item) or self.store.worker_failed:
            return None
        row = self.db.execute('SELECT id FROM main.claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
        entry = self.get(row[0]) if row else None
        if (not entry or entry['project'] != item['cwd'] or not ident(entry['grant_id'])
                or not ident(entry['client_id']) or not ident(entry['generation'])
                or entry['lifecycle_protocol'] != 2 or entry['last_seen'] <= time.time() - LEASE
                or self.room_veto(agent, entry['project'])):
            return None
        grant = self.db.execute('SELECT 1 FROM main.claude_mod_grants WHERE id=? AND active=1',
                                (entry['grant_id'],)).fetchone()
        return entry if grant else None

    def attachment_claim_entry(self, mount, token, data, agents):
        """Called by the protected writer after file and namespace checks."""
        if self.attachment_mount is not mount:
            return None
        entry = self.authenticate(token)
        self.validate_poll(entry, data)
        snapshot = agents() if callable(agents) else agents
        item = snapshot.get(entry['agent']) if isinstance(snapshot, dict) else None
        if not data['accept'] or self.attachment_entry(entry['agent'], item) != entry:
            return None
        if peer_lifecycle.blockers(self.db, entry['agent']):
            return None
        for row in self.rows():
            if row['agent'] != entry['agent'] or peer_lifecycle.is_terminal(self.db, row['request_id']):
                continue
            if (row.get('launched') and not row.get('settled') and row['state'] not in ('answered', 'failed')
                    or row.get('mod_enrollment') == entry['id'] and not row.get('launched')
                    and row['state'] == 'sending' and row['created'] + TTL > time.time()):
                return None
        return entry

    def attachment_event(self, mount, token, data):
        adapter, activity = mount

        def recheck(*, native=True):
            if self.attachment_mount is not mount:
                raise ModError(409, 'Claude request does not match its original claim.')
            entry = self.authenticate(token)
            if native:
                self.native(entry, data)
                if entry['last_seen'] <= time.time() - LEASE:
                    raise ModError(409, 'Reconnect the Claude mod before reporting the original receipt.')
            return entry

        from attachments_owner_transport import LedgerError
        from attachments_types import AttachmentError
        try:
            with activity():
                with self.store.lock:
                    entry = recheck(native=False)
                    if not adapter.handles(data.get('request_id')):
                        return None
                return adapter.event(entry, data, recheck=recheck)
        except (LedgerError, AttachmentError) as error:
            raise ModError(getattr(error, 'status', 409), 'The Claude attachment receipt could not be verified.', error.code) from None

    def poll_call(self, data, token, agents):
        # Preparation can read files/directory, so it cannot run under our SQLite
        # lock or transaction. It conveys evidence, never permission to dispatch.
        with self.store.lock, self.db:
            self.maintain()
            entry = self.authenticate(token)
            self.validate_poll(entry, data)
            new_rows = [r for r in self.rows() if data['accept'] and r.get('mod_enrollment') == entry['id']
                        and not r.get('launched') and r['state'] == 'sending']
            candidates = [r for r in new_rows if self.peer_row(r)]
        snapshot = agents() if callable(agents) else agents
        proof = None
        if new_rows and (not isinstance(snapshot, dict) or entry['agent'] not in snapshot):
            try:
                proof = self.store.shared.metadata_target(entry['agent'], entry['project'])
            except Exception:
                pass  # Heartbeats and original receipts do not require new-claim evidence.
        prepared = {}
        for row in candidates:
            try:
                prepared[row['request_id']] = self.store.shared.prepare_mod_claim(row)
            except Exception:
                prepared[row['request_id']] = None
        with self.store.lock, self.db:
            snapshot = agents() if callable(agents) else agents  # pure current room vetoes
            snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
            if entry['agent'] not in snapshot and proof and self.store.shared.metadata_current(proof):
                snapshot[entry['agent']] = self.metadata_item(proof)
            self.maintain()
            # Connection or grant replacement during preparation invalidates this
            # poll; retained original attempts can still recover after reconnect.
            entry = self.authenticate(token)
            result = self.poll(entry, data, snapshot, prepared)
            mount = self.attachment_mount
        if mount is None or not data['accept'] or result['claim'] is not None or entry['lifecycle_protocol'] != 2:
            return result
        from attachments_owner_transport import LedgerError
        from attachments_types import AttachmentError
        try:
            adapter, activity = mount
            with activity():
                if self.attachment_mount is mount:
                    result['claim'] = adapter.poll(entry, recheck=lambda: self.attachment_claim_entry(mount, token, data, agents))
        except (LedgerError, AttachmentError):
            # An uncertain local or owner commit must never return a claim.
            pass
        return result

    def attachment_busy(self, agent):
        mount = self.attachment_mount
        if mount is None and self.store._attachment_guard is None:
            return False  # Never-installed state has no protected reader.
        from attachments_owner_transport import LedgerError
        from attachments_types import AttachmentError
        try:
            if mount is not None:
                return mount[0].busy(agent)
            from claude_attachment_adapter import protected_busy
            return protected_busy(self.db, agent)
        except (LedgerError, AttachmentError):
            return True

    def poll(self, entry, data, agents, prepared, notice_proofs=None):
        self.validate_poll(entry, data)
        self.db.execute('UPDATE claude_mod_enrollments SET last_seen=? WHERE id=?', (time.time(), entry['id']))
        rows = self.rows()
        busy = any(not peer_lifecycle.is_terminal(self.db, r['request_id']) and r.get('mod_enrollment') == entry['id'] and r.get('launched') and not r.get('settled') and r['state'] in ('queued', 'awaiting_reply') for r in rows)
        busy = busy or any(state != 'reserved' for _, state, _ in peer_lifecycle.blockers(self.db, entry['agent']))
        busy = busy or self.attachment_busy(entry['agent'])
        claim = None
        item = agents.get(entry['agent']) if isinstance(agents, dict) else None
        can_claim = local_parent(entry['agent'], item) and item.get('cwd') == entry['project']
        if data['accept'] and can_claim and not busy and not self.store.worker_failed:
            for row in sorted(rows, key=lambda r: r['created']):
                if (peer_lifecycle.is_terminal(self.db, row['request_id']) or row.get('mod_enrollment') != entry['id']
                        or row.get('launched') or row['state'] != 'sending'):
                    continue
                # Deadline checked inside the same serialized reservation transaction.
                if row['created'] + TTL <= time.time():
                    row.update(state='failed', error='This message expired before claim. Nothing was submitted.')
                    self.put(row)
                    continue
                if self.peer_row(row):
                    if row['request_id'] not in prepared:
                        continue  # A competing reservation was not prepared by this poll.
                    evidence = prepared[row['request_id']]
                    try:
                        if evidence is None:
                            raise ValueError('Claude peer preflight failed.')
                        self.store.shared.authorize_mod_claim_in_transaction(row, entry, evidence, item)
                    except (ValueError, peer_bridge.PeerError):
                        row.update(state='failed', error='Claude peer authorization changed before claim. Nothing was submitted.')
                        self.put(row)
                        continue
                attempt = str(uuid.uuid4())
                row.update(launched=True, state='queued', mod_attempt_id=attempt, mod_generation=entry['generation'],
                           lifecycle_protocol=entry['lifecycle_protocol'], error='')
                self.put(row)
                claim = dict(request_id=row['request_id'], attempt_id=attempt, native_id=entry['agent'][2:], generation=entry['generation'], envelope=row['envelope'], expires_at=row['created']+TTL, lifecycle_protocol=entry['lifecycle_protocol'])
                if row.get('origin') == 'peer':
                    claim['origin'] = 'peer'
                    peer_progress.observe(self.store, row, 'claim_issued')
                break
        return dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], peer_protocol=entry['peer_protocol'], reply_notice_protocol=entry['reply_notice_protocol'], lifecycle_protocol=entry['lifecycle_protocol'], supported_lifecycle_protocol=2, peer_envelope_limit=entry['peer_envelope_limit'], supported_peer_envelope_limit=100000, claim=claim, requests=self.statuses(entry), recoverable_turns=self.recoverable_turns(entry))

    def event(self, entry, data):
        self.native(entry, data)
        if hasattr(self.store, 'reply_delivery'):
            try:
                result = self.store.reply_delivery.event_in_transaction(entry, data)
            except peer_bridge.PeerError as error:
                raise ModError(error.status, str(error)) from None
            if result is not None:
                return result
        kind = data.get('kind')
        extras = {'submitted': (), 'started': ('turn_id',), 'answered': ('turn_id', 'answer'), 'dropped': (), 'unknown': (),
                  'completed': ('turn_id', 'outcome', 'answer', 'event_id', 'occurred_at')}
        if kind not in extras:
            raise ReceiptError(400, 'Invalid Claude request event.')
        if set(data) != set(self.fields(entry, ('generation', 'native_id', 'request_id', 'attempt_id', 'kind') + extras[kind])):
            raise ReceiptError(400, 'Invalid Claude event fields.')
        if self.store._attachment_reserved(data['request_id']):
            raise ReceiptError(409, 'Claude request does not match its original claim.')
        lifecycle = peer_lifecycle.get(self.db, data['request_id'])
        row = next((r for r in self.rows() if r['request_id'] == data['request_id']), None)
        payload_expired = row is None
        if lifecycle and (payload_expired or lifecycle['execution'] in ('retired', 'dropped')):
            evidence = lifecycle['evidence']
            if evidence.get('project_digest') != digest(entry['project']):
                raise ReceiptError(409, 'Original Claude project evidence is unavailable.')
            row = dict(request_id=data['request_id'], agent=lifecycle['agent'], created=lifecycle['created'],
                       runtime='claude', transport=TRANSPORT, origin='peer', bridge_version=2,
                       mod_project=entry['project'], target_project=entry['project'], launched=bool(evidence.get('launched')),
                       state='unknown', reply='', error='', **{k:v for k,v in evidence.items() if k.startswith(('mod_', 'completion_', 'answer_')) or k == 'lifecycle_protocol'})
        if (not row or row.get('mod_enrollment') != entry['id'] or row.get('mod_project') != entry['project']
                or row.get('mod_attempt_id') != data['attempt_id'] or not ident(data['attempt_id'])
                or row['agent'] != entry['agent'] or not row.get('launched')):
            raise ReceiptError(409, 'Claude request does not match its original claim.')
        if self.peer_row(row):
            launched = self.db.execute('SELECT launched FROM shared_bridge_requests WHERE transport_id=?',
                                       (row['request_id'],)).fetchone()
            if not launched or launched[0] is None or not ident(row.get('mod_generation')):
                raise ReceiptError(409, 'Claude peer claim has no original launch receipt.')
        if kind == 'completed':
            if entry['lifecycle_protocol'] != 2 or row.get('lifecycle_protocol') != 2:
                raise ModError(409, 'Claude lifecycle completion requires its original negotiated protocol.')
            if (data['outcome'] not in ('answered', 'unusable', 'aborted', 'withheld') or not ident(data['event_id'])
                    or type(data['occurred_at']) is not int or not 0 <= data['occurred_at'] <= 9007199254740991
                    or not isinstance(data['answer'], str) or data['outcome'] != 'answered' and data['answer'] != ''):
                raise ReceiptError(400, 'Invalid Claude completion outcome.')
        turn = data.get('turn_id')
        if kind in ('started', 'answered', 'completed'):
            if not turn_id(turn):
                raise ReceiptError(400, 'Invalid Claude turn identifier.')
            durable_turn = lifecycle['evidence'].get('mod_turn') if lifecycle else None
            if (row.get('mod_turn') and row['mod_turn'] != turn) or (durable_turn and durable_turn != turn):
                raise ReceiptError(409, 'Claude request is already bound to another turn.')
        if lifecycle and lifecycle['execution'] == 'retired':
            # Original identity, claim, protocol and turn checks above still apply.
            # Retirement is not a provider completion; never retain the late answer
            # or change immutable outcome evidence even on repeated v1/v2 reports.
            peer_lifecycle.record_event(self.db, row['request_id'], 'retired_late_event', kind,
                                        'retired', 'retired')
            return dict(request_id=row['request_id'], state='unknown', turn_id=row.get('mod_turn', ''),
                        retired=True, message=peer_lifecycle.RETIRE_MESSAGE)
        if lifecycle and lifecycle['execution'] == 'dropped':
            if kind in ('started', 'answered', 'completed'):
                raise ReceiptError(409, 'Claude execution contradicts a verified dropped submission.')
            # The row above is an authentication view of immutable receipts,
            # not a replacement payload. Late ACKs must neither erase a retained
            # envelope nor recreate one after pruning.
            peer_lifecycle.record_event(self.db, row['request_id'], 'dropped_late_event', kind,
                                        'dropped', 'dropped')
            return dict(request_id=row['request_id'], state='failed', turn_id='',
                        message=peer_lifecycle.DROP_MESSAGE)
        if self.peer_row(row):
            evidence = lifecycle['evidence'] if lifecycle else {}
            if kind == 'dropped' and (row.get('mod_turn') or row.get('bound_turn')
                    or row.get('execution_outcome') == 'completed' or row.get('state') in ('awaiting_reply', 'answered')
                    or evidence.get('mod_turn') or evidence.get('bound_turn')
                    or lifecycle and lifecycle['execution'] in ('running', 'completed')):
                raise ReceiptError(409, 'Claude input already has execution evidence; a pre-start drop is contradictory.')
        outcome = data.get('outcome', kind)
        answered = outcome == 'answered'
        reply = None
        if answered:
            try:
                reply = reply_text(data['answer'], row['request_id'])
            except ReceiptError as error:
                # Legacy senders emitted a marker-only answer for an already
                # bound turn. Acknowledge that exact terminal receipt as unusable.
                if kind != 'completed' and not (isinstance(error, EmptyReplyError) and
                                                (row.get('mod_turn') or durable_turn)):
                    raise
                # A valid terminal receipt already proves execution. Its answer
                # candidate can fail capture independently of that evidence.
                outcome, answered = 'unusable', False
        if answered and (row['state'] == 'answered' and row.get('reply') != reply
                         or lifecycle and lifecycle['evidence'].get('answer_digest') not in (None, digest(reply))):
            raise ReceiptError(409, 'A different Claude reply was already recorded.')
        before = lifecycle['execution'] if lifecycle else row.get('execution_outcome', row['state'])
        terminal = row.get('settled') or lifecycle and lifecycle['execution'] in peer_lifecycle.TERMINAL
        fresh_end = kind in ('answered', 'completed') and not terminal
        fresh_start = False
        withheld = row.get('capture_disposition') == 'withheld' or lifecycle and lifecycle['capture'] == 'withheld'
        fingerprint = ''
        if kind in ('answered', 'completed') and (row.get('execution_outcome') == 'dropped'
                or lifecycle and lifecycle['execution'] == 'dropped'):
            raise ReceiptError(409, 'Claude completion contradicts a verified dropped submission.')
        if kind == 'completed':
            # Connection generation is not event identity: retained events replay
            # on the current authenticated connection without resubmitting input.
            fingerprint = digest(json.dumps({k:data[k] for k in ('request_id','attempt_id','turn_id','outcome','answer','event_id','occurred_at')}, sort_keys=True))
            prior = self.db.execute('SELECT request_id,fingerprint FROM peer_execution_events WHERE event_id=?', (data['event_id'],)).fetchone()
            if prior and (prior[0] != row['request_id'] or prior[1] != fingerprint):
                raise ReceiptError(409, 'Claude completion event conflicts with its original receipt.')
            evidence = lifecycle['evidence'] if lifecycle else row
            fresh_end = fresh_end and prior is None and not (evidence.get('completion_event_id') or evidence.get('answer_event_id'))
            for prefix in ('completion', 'answer'):
                if evidence.get(prefix+'_event_id') == data['event_id'] and evidence.get(prefix+'_fingerprint') != fingerprint:
                    raise ReceiptError(409, 'Claude completion event conflicts with its retained acknowledgment.')
            if not evidence.get('completion_event_id'):
                row.update(completion_event_id=data['event_id'], completion_fingerprint=fingerprint)
            elif evidence['completion_event_id'] != data['event_id'] and not answered:
                raise ReceiptError(409, 'This attempt already has its terminal completion receipt.')
            if answered:
                if evidence.get('answer_event_id') not in (None, data['event_id']):
                    raise ReceiptError(409, 'This attempt already has its answer completion receipt.')
                row.update(answer_event_id=data['event_id'], answer_fingerprint=fingerprint)
        if answered and not withheld and not payload_expired:
            row.update(state='answered', reply=reply, error='', mod_turn=turn,
                       execution_outcome='completed', settled=True, capture_disposition='captured')
        elif kind in ('answered', 'completed'):
            row.update(execution_outcome='completed', settled=True, mod_turn=turn)
            if row['state'] != 'answered':
                disposition = 'withheld' if withheld or outcome == 'withheld' else 'unusable'
                row.update(state='unknown', reply='', capture_disposition=disposition,
                           error=('The turn completed; its reply was withheld because conversation isolation could not be verified.'
                                  if disposition == 'withheld' else
                                  'The turn completed, but no usable reply was retained. The execution slot is released.'))
        elif not terminal and row['state'] != 'answered':
            if kind == 'started' and row['state'] != 'failed':
                fresh_start = not (row.get('mod_turn') or durable_turn)
                row.update(state='awaiting_reply', mod_turn=turn, error='')
            elif kind == 'dropped' and (self.peer_row(row) or row['state'] not in ('awaiting_reply', 'expired')):
                row.update(state='failed', execution_outcome='dropped', settled=True,
                           error='Native Claude input was dropped before it started. It was not resubmitted.')
            elif kind == 'unknown' and row['state'] != 'failed':
                row.update(state='unknown', error='Claude submission could not be confirmed. Nothing was resubmitted.')
        peer_lifecycle.project_terminal(self.db, row['request_id'], row)
        if row['state'] == 'answered':
            self.store.peers.owner_warning(row)
        if payload_expired:
            peer_lifecycle.reconcile(self.db, row)
        else:
            self.put(row)
            if fresh_start or fresh_end:
                peer_progress.observe(self.store, row, 'started' if fresh_start else 'ended')
                peer_progress.observe(self.store, row, 'checked')
        after = peer_lifecycle.get(self.db, row['request_id'])
        peer_lifecycle.record_event(self.db, row['request_id'], kind, outcome, before,
                                    after['execution'] if after else row.get('execution_outcome', row['state']),
                                    event_id=data.get('event_id'), occurred=data.get('occurred_at'), fingerprint=fingerprint)
        return dict(request_id=row['request_id'], state=row['state'], turn_id=row.get('mod_turn', ''))

    def call(self, method, data, token='', agents=None, port=None):
        if callable(agents):
            self.room_snapshot = agents
        if method == 'poll':
            return self.poll_call(data, token, agents)
        if method == 'register':
            with self.store.lock, self.db:
                self.maintain()
                self.registration_access(data, token)
            if not project(data['project']):
                raise ModError(409, 'The Claude project is not canonical.', 'identity_rejected')
            snapshot = agents() if callable(agents) else agents
            aid = 'c:' + data['native_id']
            proof = None
            if not isinstance(snapshot, dict) or aid not in snapshot:
                try:
                    proof = self.store.shared.metadata_target(aid, data['project'])
                except Exception:
                    raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed') from None
            with self.store.lock, self.db:
                self.maintain()
                current = agents() if callable(agents) else agents  # pure room snapshot only
                return self.register(data, token, current, proof)
        mount = self.attachment_mount
        if method == 'event' and mount is not None:
            result = self.attachment_event(mount, token, data)
            if result is not None:
                return result
        agents = agents() if callable(agents) else agents
        with self.store.lock, self.db:
            self.maintain()
            if method == 'enroll':
                return self.enroll(data)
            if method == 'setup':
                return self.setup(data, port)
            entry = self.authenticate(token)
            if method == 'connect':
                if entry['grant_id']:
                    raise ModError(409, 'Automatic Claude connections reconnect through registration.', 'register_required')
                schema(data, ('native_id', 'project'))
                self.native(entry, data, connected=False)
                if data['project'] != entry['project']:
                    raise ModError(409, 'The enrolled Claude project does not match.')
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                generation = str(uuid.uuid4())
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,last_seen=? WHERE id=?', (generation, time.time(), entry['id']))
                return self.session(self.get(entry['id']))
            if method == 'event':
                return self.event(entry, data)
            if method == 'disconnect':
                schema(data, self.fields(entry, ('generation', 'native_id')))
                self.native(entry, data)
                self.uncertain(entry['id'], 'The Claude mod disconnected. Delivery remains unknown; nothing was resubmitted.')
                self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0 WHERE id=?', (entry['id'],))
                return {'disconnected': True}
            raise ModError(404, 'Unknown Claude mod operation.')


def handle_http(handler, store, agents):
    """Dedicated purpose-scoped service routes; owner key is never accepted."""
    handler.connection.settimeout(10)
    try:
        port = handler.server.server_address[1]
        method = handler.path.removeprefix('/claude-mod/')
        if (method not in ('setup', 'register', 'enroll', 'connect', 'poll', 'event', 'disconnect')
                or handler.headers.get_all('Host') != [f'127.0.0.1:{port}']
                or handler.headers.get('Origin') is not None or handler.headers.get('Transfer-Encoding') is not None
                or len(handler.headers.get_all('Content-Length') or []) != 1):
            raise ModError(403, 'Invalid Claude mod service request.')
        try:
            size = int(handler.headers.get('Content-Length', ''))
        except ValueError:
            raise ModError(400, 'Invalid Claude mod request size.') from None
        # JSON escaping can take up to six bytes per allowed reply codepoint.
        if size < 2 or size > (100000 if method == 'event' else 16384):
            raise ModError(400, 'Invalid Claude mod request size.')
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise ModError(400, 'Incomplete Claude mod request.')
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise ModError(400, 'Invalid Claude mod request JSON.') from None
        if not isinstance(data, dict):
            raise ModError(400, 'Invalid Claude mod request.')
        auth = handler.headers.get_all('Authorization') or []
        if method not in ('setup', 'enroll') and (len(auth) != 1 or not auth[0].startswith('Bearer ')):
            raise ModError(403, 'Claude mod service access is required.')
        if method in ('setup', 'enroll') and auth:
            raise ModError(403, 'Claude setup and pairing use one-time codes only.')
        result = store.mods.call(method, data, auth[0][7:] if auth else '', agents=agents, port=port)
        status = 200
    except ModError as error:
        status, result = error.status, {'error': str(error), 'error_code': error.code}
    except Exception:
        status, result = 503, {'error': 'Claude mod service is temporarily unavailable. Retry the same request later.'}
    handler.close_connection = True
    return handler.send(status, json.dumps(result).encode(), 'application/json')
