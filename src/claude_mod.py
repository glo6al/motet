"""Scoped owner and shared-v2 peer official-mod delivery. No native credentials or native IPC.

SQLite reservations precede returning a claim. Claims never replay; only status
and completion events may replay. The official host owns native input execution.
"""
import collections
import hashlib
import hmac
import json
import os
import peer_bridge
import peer_lifecycle
import peer_progress
import re
import secrets
import threading
import time
import uuid

LEASE, PAIR_TTL, TTL, RETENTION = 12, 300, 900, 86400
RESERVATION_TTL = 86400           # how long a reservation's answer can be asked for again
RESERVATION_LIMIT = 4096          # the most reservations kept; the oldest go first
COMMAND_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]{1,15}$')
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


PATH = os.path        # the platform's path rules; simulated Windows tests swap in ntpath


def project_text(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and PATH.isabs(value) and '\x00' not in value


def project(value):
    return project_text(value) and PATH.realpath(value) == value


def _network(path):
    """A folder on a network share: a Windows UNC or long-path name (\\\\server\\share, \\\\?\\...) or a drive
    letter Windows reports as mapped to a share (the same test the folder window uses for its hint). Such a folder is
    never resolved, so an unreachable server cannot hold a check up. Mounted shares elsewhere cannot be told apart
    by name; they are resolved only on the resolver thread, never under a lock."""
    import new_chat_folder_chooser_windows as windows
    return PATH.splitdrive(path)[0].startswith(('\\\\', '//')) or windows.remote_drive(path, windows.drive_type)


def _textually_inside(cwd, root):
    try:
        return PATH.commonpath((root, PATH.normpath(cwd))) == root
    except ValueError:
        return False


def folder_inside(cwd, root):
    """Whether `cwd` is an existing folder that is the canonical project `root` or inside it, links resolved.
    Touches the disk: call it only off every lock (the resolver thread does). A folder not inside by its name, or
    on a network share, is refused without any disk access; so is a folder that does not exist (realpath does not
    check that by itself), is not a directory, or whose real place cannot be found."""
    if not project_text(cwd) or not project_text(root):
        return False
    if not (cwd == root or _textually_inside(cwd, root)) or _network(cwd) or _network(root):
        return False
    try:
        real = PATH.realpath(cwd)
        return PATH.isabs(real) and PATH.commonpath((root, real)) == root and PATH.isdir(real)
    except (OSError, ValueError):
        return False


class FolderResolver:
    """Resolves working folders off every lock, so a check made under the store lock never waits on a disk.

    Each folder is resolved on its own daemon thread, at most MAX_ACTIVE at once and at most one per folder. A
    result is used while it is younger than KEEP seconds and refreshed once it is older than FRESH; until a folder's
    first result arrives it counts as outside. A resolution still running after DEADLINE seconds (a dead mount, for
    one) counts as outside and stops counting against MAX_ACTIVE, so it never holds up other folders; that folder is
    tried again only once its thread returns. At most MAX_THREADS threads exist, stuck ones included. New work is
    started under the lock whenever a folder is looked up or a resolution finishes, so no request is left waiting
    for a wake-up that never comes.

    Every resolution carries a number from one counter, taken when it starts. The cached answer serves readiness
    and listing only. Dispatching a claim needs `revalidate`, which accepts only a resolution numbered after the
    call began, so no earlier answer (whatever the clock read) can authorise it."""
    FRESH, KEEP, DEADLINE, REVALIDATE = 5.0, 60.0, 10.0, 2.0
    # If MAX_THREADS distinct folders all hang for good, no thread is left and every unresolved folder counts as
    # outside until the server restarts. That fails closed (those chats show unavailable) and needs that many dead
    # mounts at once, so it is accepted rather than spending more threads.
    MAX_PENDING, MAX_RESULTS, MAX_ACTIVE, MAX_THREADS = 256, 1024, 4, 64
    thread = threading.Thread

    def __init__(self, resolve=None, clock=time.monotonic):
        self.resolve, self.clock = resolve, clock
        self.changed = threading.Condition(threading.Lock())
        self.results, self.queue, self.queued = {}, collections.deque(), set()
        self.running = {}           # folder -> (start time, resolution number)
        self.issued = 0             # the last resolution number handed out
        self.wanted = {}            # folder -> a revalidation needs a resolution numbered above this

    def lookup(self, cwd, root):
        key = (cwd, root)
        with self.changed:
            now = self.clock()
            self._expire(now)
            hit = self.results.get(key)
            if (hit is None or now - hit[1] >= self.FRESH) and len(self.queued) < self.MAX_PENDING:
                self._queue(key)
            self._start(now)
            return hit is not None and now - hit[1] < self.KEEP and hit[0]

    def _queue(self, key):
        if key not in self.queued and key not in self.running:
            self.queued.add(key)
            self.queue.append(key)

    def _expire(self, now):
        for key, (started, number) in self.running.items():
            hit = self.results.get(key)
            if now - started >= self.DEADLINE and (hit is None or hit[2] < number):
                self._keep(key, False, started + self.DEADLINE, number)

    def _keep(self, key, inside, at, number):
        self.results.pop(key, None)
        self.results[key] = (inside, at, number)
        while len(self.results) > self.MAX_RESULTS:
            self.results.pop(next(iter(self.results)))
        self.changed.notify_all()

    def _start(self, now):
        while self.queue and len(self.running) < self.MAX_THREADS:
            if sum(1 for started, _ in self.running.values() if now - started < self.DEADLINE) >= self.MAX_ACTIVE:
                return
            key = self.queue.popleft()
            self.queued.discard(key)
            self.issued += 1
            self.running[key] = (now, self.issued)
            try:
                self.thread(target=self._work, args=(key, self.issued), name='claude-folder-resolver',
                            daemon=True).start()
            except Exception:
                # No thread could be started (the process is out of threads): that folder counts as outside now
                # and is tried again on a later lookup. Never raise into the caller, who holds the store lock.
                self.running.pop(key, None)
                self._keep(key, False, now, self.issued)
                return

    def _work(self, key, number):
        try:
            inside = bool((self.resolve or folder_inside)(*key))
        except Exception:
            inside = False
        with self.changed:
            now = self.clock()
            started, _ = self.running.pop(key, (now, number))
            self._keep(key, inside, started, number)
            if key in self.wanted and self.wanted[key] >= number:
                # A revalidation began after this resolution was numbered: resolve the folder again for it.
                self._queue(key)
            self._start(now)

    def revalidate(self, cwd, root, timeout=None):
        """For a caller that holds no lock, before dispatch: wait up to `timeout` seconds for a resolution of the
        folder numbered after this call began. Without one in time, or if it says outside, the folder is outside."""
        timeout = self.REVALIDATE if timeout is None else timeout
        key, end = (cwd, root), time.monotonic() + timeout
        with self.changed:
            floor = self.issued                     # any resolution numbered above this started after this call
            self.wanted[key] = max(self.wanted.get(key, floor), floor)
            self._queue(key)
            self._start(self.clock())
            try:
                while True:
                    self._expire(self.clock())
                    hit = self.results.get(key)
                    if hit is not None and hit[2] > floor:
                        return hit[0]
                    left = end - time.monotonic()
                    if left <= 0:
                        return False
                    self.changed.wait(left)
            finally:
                if self.wanted.get(key) == floor:
                    del self.wanted[key]

    def prime(self, cwd, root, timeout=2.0):
        """For a caller that holds no lock (registration): look the folder up, then wait up to `timeout` seconds
        for a fresh result. A folder still unresolved by then counts as outside."""
        key, end = (cwd, root), time.monotonic() + timeout
        self.lookup(cwd, root)
        with self.changed:
            while True:
                hit = self.results.get(key)
                if hit is not None and self.clock() - hit[1] < self.FRESH:
                    return hit[0]
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(left)

    def settle(self, timeout=5.0):
        """Tests: wait until every queued folder has been resolved, or has passed its deadline."""
        end = time.monotonic() + timeout
        with self.changed:
            while True:
                now = self.clock()
                self._expire(now)
                self._start(now)
                if not self.queue and all(now - started >= self.DEADLINE for started, _ in self.running.values()):
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(min(left, 0.05))


RESOLVER = FolderResolver()


def within_project(cwd, root):
    """Whether a chat's current working folder is its enrolled project or a folder inside it. No disk access.

    A Claude chat that changes directory into a subfolder stays the same chat in the same project; its transcript
    records that folder as `cwd`. `root` is the enrolled project, already canonical (`project`). The exact folder
    passes at once. A folder not inside the project by its name, or on a network share, is outside at once. Any
    other folder is answered from the resolver's last result (links resolved off the lock), so `..` or a link that
    leads out of the project, a sibling folder, another drive, or a folder not resolved yet is outside it.
    """
    if not project_text(cwd) or not project_text(root):
        return False
    if cwd == root:
        return True
    if not _textually_inside(cwd, root) or _network(cwd) or _network(root):
        return False
    return RESOLVER.lookup(cwd, root)


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
                           ('peer_envelope_limit', 'INTEGER NOT NULL DEFAULT 4000'),
                           # Connected for an earlier folder's retained outcomes only: never given a claim.
                           ('receipt_only', 'INTEGER NOT NULL DEFAULT 0'),
                           # The owner decision this connection was made under (claude_mod_decisions.latest_seq).
                           ('decision_seq', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE claude_mod_enrollments ADD COLUMN {name} {kind}')
        self.db.execute('CREATE INDEX IF NOT EXISTS claude_mod_agents ON claude_mod_enrollments(agent,active)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS claude_mod_active_agent ON claude_mod_enrollments(agent) WHERE active=1')
        # The owner's connect and disconnect decisions for each chat, ordered here, whichever plugin copy or Claude
        # window sent them: one row per chat, written only inside the store's transactions. Absent means never
        # opted out, which is how every chat stood before this table existed.
        # latest_seq is the chat's last committed ticket. issued_seq is left from the earlier per-chat ledger: it is
        # no longer written, and is read only once, to start the shared counter above every ticket it issued.
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_decisions (
            agent TEXT PRIMARY KEY, latest_seq INTEGER NOT NULL, opted_out INTEGER NOT NULL,
            opted_out_seq INTEGER NOT NULL, issued_seq INTEGER NOT NULL DEFAULT 0)''')
        if 'issued_seq' not in {r[1] for r in self.db.execute('PRAGMA table_info(claude_mod_decisions)')}:
            self.db.execute('ALTER TABLE claude_mod_decisions ADD COLUMN issued_seq INTEGER NOT NULL DEFAULT 0')
        # A pairing code holds the ticket reserved when the owner created it, so a pairing made before a later
        # opt-out cannot lift it.
        if 'decision_seq' not in {r[1] for r in self.db.execute('PRAGMA table_info(claude_mod_pairs)')}:
            self.db.execute('ALTER TABLE claude_mod_pairs ADD COLUMN decision_seq INTEGER NOT NULL DEFAULT 0')
        # Tickets come from one counter for every chat, so a command can reserve its place before it knows which
        # chat it is for. The counter starts above every ticket an earlier per-chat ledger issued or committed.
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_decision_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1), issued INTEGER NOT NULL)''')
        if not self.db.execute('SELECT 1 FROM claude_mod_decision_counter').fetchone():
            start = max(self.db.execute('SELECT COALESCE(MAX(MAX(issued_seq,latest_seq)),0) FROM claude_mod_decisions').fetchone()[0],
                        self.db.execute('SELECT COALESCE(MAX(decision_seq),0) FROM claude_mod_pairs').fetchone()[0])
            self.db.execute('INSERT INTO claude_mod_decision_counter (id,issued) VALUES (1,?)', (start,))
        # A reservation is repeatable: the same owner command (its plugin copy and its number there) always gets the
        # same ticket back, so a lost answer and a retry never give an old command a newer place.
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_reservations (
            command_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, created REAL NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS claude_mod_reservations_created ON claude_mod_reservations(created)')
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
                self.put(row, hold_observed=False)
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

    def put(self, row, *, hold_observed=False):
        peer_lifecycle.reconcile(self.db, row, hold_observed=hold_observed)
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (row['request_id'], json.dumps(row)))
        peer_progress.ensure(self.store, row)

    def uncertain(self, enrollment, reason):
        for row in self.rows():
            if row.get('mod_enrollment') == enrollment and row.get('launched') and row['state'] in ('sending', 'queued', 'awaiting_reply'):
                row.update(state='unknown', error=reason)
                self.put(row, hold_observed=True)

    def maintain(self):
        now = time.time()
        self.db.execute('DELETE FROM claude_mod_pairs WHERE expires<=?', (now,))
        self.db.execute('DELETE FROM claude_mod_setup_codes WHERE expires<=?', (now,))
        self.db.execute('DELETE FROM claude_mod_reservations WHERE created<=?', (now - RESERVATION_TTL,))
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE generation IS NOT NULL AND last_seen<?', (now - LEASE,)).fetchall():
            self.uncertain(eid, 'The Claude mod disconnected. Delivery is unknown; nothing was resubmitted.')
            self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL WHERE id=?', (eid,))
        for row in self.rows():
            if row.get('settled') or row['state'] in ('answered', 'failed'):
                continue
            if not row.get('launched') and now >= row['created'] + TTL:
                row.update(state='failed', error='This message expired before a Claude mod claimed it. Nothing was submitted.')
                self.put(row)
            # This reply deadline applies to the owner Desk.
            elif (row.get('origin') in (None, 'owner') and row.get('launched')
                    and row['state'] in ('queued', 'awaiting_reply') and now >= row['created'] + TTL):
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
            self.db.execute('INSERT INTO claude_mod_pairs (code_hash,agent,project,expires,decision_seq) VALUES (?,?,?,?,?)',
                            (digest(code), agent, item['cwd'], expiry, self.reserve()))
        return dict(agent=agent, port=port, pair_code=code, expires_at=expiry, command=f'/watch-connect {port}:{code}')

    def session(self, entry):
        # Notice negotiation is historical ACK cleanup only; polls never create
        # or claim notice prompts. Retained old clients can drain their receipts.
        result = dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], heartbeat_interval_ms=2000, lease_seconds=LEASE, peer_protocol=entry['peer_protocol'],
                      reply_notice_protocol=entry['reply_notice_protocol'], supported_reply_notice_protocol=1,
                      lifecycle_protocol=entry['lifecycle_protocol'], supported_lifecycle_protocol=2,
                      peer_envelope_limit=entry['peer_envelope_limit'], supported_peer_envelope_limit=100000,
                      folder_protocol=1, decision_protocol=1)
        if entry['grant_id']:
            result.update(grant_id=entry['grant_id'], client_id=entry['client_id'], connection_seq=entry['connection_seq'])
        return result

    def get(self, eid):
        fields = ('id', 'agent', 'project', 'generation', 'last_seen', 'active', 'grant_id', 'client_id', 'connection_seq', 'peer_protocol', 'reply_notice_protocol', 'lifecycle_protocol', 'peer_envelope_limit', 'receipt_only', 'decision_seq')
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
            # A receipt-only connection reports earlier outcomes; it does not count as a receiving chat.
            connected = sum(1 for e in entries if not self.store.worker_failed and e['last_seen'] > time.time() - LEASE
                            and not e['receipt_only']
                            and self.db.execute('SELECT 1 FROM claude_mod_grants WHERE id=? AND active=1', (e['grant_id'],)).fetchone()
                            and not self.room_veto(e['agent'], e['project']) and
                            (agents is None or e['agent'] not in agents or eligible(e['agent'], agents[e['agent']])
                             and within_project(agents[e['agent']]['cwd'], e['project'])))
            return dict(version=1, authorized=authorized, connected_chats=connected, peer_receive=bool(grant and grant[0] == 1))

    def _fail_unclaimed(self, enrollments, error):
        """Requests provably never handed to the plugin (not launched, still sending): fail them, saying so."""
        for row in self.rows():
            if row.get('mod_enrollment') in enrollments and not row.get('launched') and row['state'] == 'sending':
                row.update(state='failed', error=error)
                self.put(row)

    def _unsettled(self, entry):
        """Whether a request already handed to this enrollment, or held for its chat, is still open."""
        for row in self.rows():
            if (row.get('mod_enrollment') == entry['id'] and row.get('launched') and not row.get('settled')
                    and not peer_lifecycle.is_terminal(self.db, row['request_id'])):
                return True
        if any(state != 'reserved' for _, state, _ in peer_lifecycle.blockers(self.db, entry['agent'])):
            return True
        return self.attachment_busy(entry['agent'])

    def _receipt_only(self, eid, on):
        """Record, on the enrollment itself, whether its current connection is for receipts only."""
        self.db.execute('UPDATE claude_mod_enrollments SET receipt_only=? WHERE id=?', (1 if on else 0, eid))

    def _move_ready(self, entry):
        """A chat moves to its project folder only with nothing in flight; requests not yet handed over fail."""
        if self._unsettled(entry):
            raise ModError(409, 'This chat has an earlier Motet request still open. It moves to its project folder '
                                'once the plugin has reported that request\'s outcome from its earlier folder.',
                           'move_pending')
        self._fail_unclaimed({entry['id']}, 'This chat moved to its project folder before the request was claimed. '
                                            'Nothing was submitted.')

    def _revoke(self):
        entries = [r[0] for r in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1 AND grant_id IS NOT NULL')]
        for eid in entries:
            self.uncertain(eid, 'Automatic Claude access was revoked. Earlier input may still run; nothing was resubmitted.')
        self._fail_unclaimed(set(entries), 'Automatic Claude access was revoked before claim. Nothing was submitted.')
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

    # --- Owner decisions -------------------------------------------------------------------------------------
    # Each owner /watch-connect, /watch-setup or /watch-disconnect reserves a ticket here the moment it starts (its
    # place in the owner's order), and commits its decision later under that ticket. Tickets come from one counter for
    # every chat, so a command can reserve before it knows its chat; decisions are compared per chat. A commit takes
    # effect only if its ticket is newer than the chat's last committed one, so a command that finishes late
    # cannot undo a decision the owner made after it began. While the chat is opted out, every new connection is
    # refused, ticketed or not, before any membership or lease exists; a later committed reconnect lifts it.

    def _decision(self, agent):
        row = self.db.execute('SELECT latest_seq,opted_out,opted_out_seq FROM claude_mod_decisions WHERE agent=?',
                              (agent,)).fetchone()
        return (row[0], bool(row[1]), row[2]) if row else (0, False, 0)

    def _store_decision(self, agent, latest, opted_out, opted_out_seq):
        self.db.execute('INSERT OR REPLACE INTO claude_mod_decisions (agent,latest_seq,opted_out,opted_out_seq) '
                        'VALUES (?,?,?,?)', (agent, latest, int(opted_out), opted_out_seq))

    def _issued(self):
        return self.db.execute('SELECT issued FROM claude_mod_decision_counter WHERE id=1').fetchone()[0]

    def reserve(self, command_id=None):
        if command_id is not None:
            row = self.db.execute('SELECT seq FROM claude_mod_reservations WHERE command_id=?', (command_id,)).fetchone()
            if row:
                return row[0]
        issued = self._issued() + 1
        self.db.execute('UPDATE claude_mod_decision_counter SET issued=? WHERE id=1', (issued,))
        if command_id is not None:
            self.db.execute('INSERT INTO claude_mod_reservations (command_id,seq,created) VALUES (?,?,?)',
                            (command_id, issued, time.time()))
            # Bounded: past the limit the oldest are forgotten (a forgotten command can then only be placed afresh).
            self.db.execute('DELETE FROM claude_mod_reservations WHERE command_id IN (SELECT command_id FROM '
                            'claude_mod_reservations ORDER BY created DESC, seq DESC LIMIT -1 OFFSET ?)', (RESERVATION_LIMIT,))
        return issued

    def _valid_ticket(self, seq):
        if seq is not None and (type(seq) is not int or not 0 < seq <= self._issued()):
            raise ModError(400, 'Invalid Claude owner decision.')

    def decision_gate(self, agent, seq=None):
        """Refuse a new connection while the owner has this chat opted out; return the decision it stands on."""
        self._valid_ticket(seq)
        latest, opted_out, _ = self._decision(agent)
        if opted_out:
            raise ModError(409, 'This chat was disconnected by its owner.', 'decision_superseded')
        return latest

    def _end_connections(self, agent, reason):
        rows = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchall()
        for eid, in rows:
            self.uncertain(eid, reason)
            self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0 WHERE id=?', (eid,))
        self._fail_unclaimed({eid for eid, in rows}, 'Its owner disconnected this Claude chat before the message was '
                                                     'claimed. Nothing was submitted.')

    def connection_decision(self, entry):
        """A connection made before the owner's latest opt-out is no longer the owner's: refuse its polls."""
        _, opted_out, opted_out_seq = self._decision(entry['agent'])
        if opted_out and opted_out_seq > entry['decision_seq']:
            raise ModError(409, 'This chat was disconnected by its owner after this connection.', 'decision_superseded')

    def decision_access(self, data, token):
        """The chat named (None for a reservation that names none), for the chat's own enrollment token or the
        shared grant (which may set any chat's). A reservation names no chat: any active credential may take one."""
        reserve = isinstance(data, dict) and data.get('reserve') is True
        query = isinstance(data, dict) and data.get('query') is True
        named = isinstance(data, dict) and 'native_id' in data
        if reserve:
            schema(data, ('reserve',) + tuple(k for k in ('native_id', 'command_id', 'recover') if k in data))
            if 'command_id' in data and not (isinstance(data['command_id'], str) and COMMAND_ID.match(data['command_id'])):
                raise ModError(400, 'Invalid Claude owner decision.')
            if 'recover' in data and (data['recover'] is not True or 'command_id' not in data):
                raise ModError(400, 'Invalid Claude owner decision.')
        elif query:
            schema(data, ('native_id', 'query'))
        else:
            schema(data, ('native_id', 'opted_out') + (('decision_seq',) if isinstance(data, dict) and 'decision_seq' in data else ()))
        if (named or not reserve) and not ident(data['native_id']) or not (reserve or query) and type(data['opted_out']) is not bool:
            raise ModError(400, 'Invalid Claude owner decision.')
        agent = 'c:' + data['native_id'] if named else None
        if isinstance(token, str) and len(token) == 64:
            grant = self.db.execute('SELECT id FROM claude_mod_grants WHERE active=1 AND token_hash=?',
                                    (digest(token),)).fetchone()
            if grant:
                return agent
        row = self.db.execute('SELECT agent FROM claude_mod_enrollments WHERE active=1 AND token_hash=?',
                              (digest(token if isinstance(token, str) else ''),)).fetchone()
        if not row or agent is not None and row[0] != agent:
            raise ModError(403, 'Claude owner decision access is invalid.', 'access_denied')
        return agent

    def decide(self, agent, opted_out, seq=None):
        """Commit a decision under its reserved ticket (or a fresh one); refused when a newer one was committed."""
        if seq is None:
            seq = self.reserve()
        self._valid_ticket(seq)
        latest, _, opted_out_seq = self._decision(agent)
        if seq <= latest:
            raise ModError(409, 'A newer owner decision for this chat was already made.', 'decision_superseded')
        if opted_out:
            opted_out_seq = seq
            self._end_connections(agent, 'Its owner disconnected this Claude chat. Earlier input may still run; '
                                         'nothing was resubmitted.')
        self._store_decision(agent, seq, opted_out, opted_out_seq)
        return dict(version=1, decision_protocol=1, decision_seq=seq, opted_out=opted_out)

    def decision_call(self, data, token):
        agent = self.decision_access(data, token)
        if data.get('reserve') is True:
            if data.get('recover') is True:
                # Only the ticket this command already holds: a command whose first request never arrived is not
                # given a new, later place under the same identity.
                row = self.db.execute('SELECT seq FROM claude_mod_reservations WHERE command_id=?', (data['command_id'],)).fetchone()
                if not row:
                    raise ModError(409, 'Motet holds no reservation for this command.', 'not_reserved')
                return dict(version=1, decision_protocol=1, decision_seq=row[0], reserved=True)
            return dict(version=1, decision_protocol=1, decision_seq=self.reserve(data.get('command_id')), reserved=True)
        if data.get('query') is True:
            # The chat's latest committed decision, so a plugin copy with no decision of its own can check a stored one.
            latest, opted_out, _ = self._decision(agent)
            return dict(version=1, decision_protocol=1, decision_seq=latest, opted_out=opted_out, query=True)
        return self.decide(agent, data['opted_out'], data.get('decision_seq'))

    def registration_access(self, data, token):
        fields = ('native_id', 'project', 'client_id', 'connection_seq')
        schema(data, fields + tuple(k for k in ('peer_protocol', 'reply_notice_protocol', 'lifecycle_protocol', 'peer_envelope_limit', 'receipt_only') if isinstance(data, dict) and k in data))
        if data.get('receipt_only', True) is not True:
            raise ModError(400, 'Invalid Claude receipt-only registration.')
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
        # Automatic registration carries no ticket: refused while the owner has this chat opted out.
        decision = self.decision_gate('c:' + data['native_id'])
        protocol, seq = data.get('peer_protocol', 0), data['connection_seq']
        aid, gid = 'c:'+data['native_id'], grant[0]
        item = agents.get(aid) if isinstance(agents, dict) else None
        receipts = data.get('receipt_only') is True
        if receipts:
            # Receipt-only: reconnect this chat's existing enrollment, for an earlier folder, unchanged, to report
            # the outcomes it retained. Its folder need not hold the chat now; it never receives a claim.
            if isinstance(agents, dict) and aid in agents:
                if not local_parent(aid, item):
                    raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
            elif not (proof and proof['agent'] == aid and self.store.shared.metadata_current(proof)
                      and (proof['project'] == data['project'] or _textually_inside(data['project'], proof['project']))):
                raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
            current = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (aid,)).fetchone()
            current = self.get(current[0]) if current else None
            if not current or current['grant_id'] != gid or current['project'] != data['project']:
                raise ModError(409, 'No earlier Motet connection of this chat holds that folder.', 'receipt_context_missing')
        elif isinstance(agents, dict) and aid in agents:
            if not local_parent(aid, item):
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
            if not item.get('cwd'):
                raise ModError(409, 'Waiting for Motet to observe this local Claude project.', 'not_observed')
            if not eligible(aid, item) or (item['cwd'] != data['project']
                                           and not _textually_inside(item['cwd'], data['project'])):
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
            if item['cwd'] != data['project']:
                # The chat works in a folder inside the named project. The plugin's word is not enough: the project
                # must be the chat's recorded launch folder (its transcript's first cwd), so a parent folder or '/'
                # is never accepted, and the working folder must really be inside it.
                if not self._launch_proven(aid, data['project'], proof):
                    raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
                if not within_project(item['cwd'], data['project']):
                    # Not resolved yet, or it resolves outside: retry later rather than a final rejection.
                    raise ModError(409, 'Waiting for Motet to observe this local Claude project.', 'not_observed')
        elif (not proof or proof['agent'] != aid or proof['project'] != data['project']
              or not self.store.shared.metadata_current(proof)):
            raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
        active = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (aid,)).fetchone()
        entry = self.get(active[0]) if active else None
        if entry and not entry['grant_id']:
            raise ModError(409, 'This chat retains its manual Motet connection.', 'manual_conflict')
        if (entry and entry['grant_id'] == gid and entry['project'] != data['project']
                and _textually_inside(entry['project'], data['project'])):
            # Upgrade: the enrollment names a subfolder of the chat's launch folder, which the plugin now reports
            # (both canonical names, so their spelling settles containment). Replace it with one for the launch
            # folder, as a fresh registration would; never across hosts, and only from a newer registration
            # episode. Every check comes before any change, so a refused upgrade leaves the enrollment, its
            # connection and its requests exactly as they were. Proof that is not ready yet is retryable.
            if not self._launch_proven(aid, data['project'], proof):
                raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
            if entry['client_id'] != data['client_id'] and entry['generation']:
                raise ModError(409, 'Another Claude host currently owns this chat connection.', 'host_conflict')
            if entry['client_id'] == data['client_id'] and seq <= entry['connection_seq']:
                raise ModError(409, 'This registration episode is stale.', 'stale_connection')
            self._move_ready(entry)
            self.uncertain(entry['id'], 'This Claude enrollment moved to its project folder. Earlier input may still run; nothing was resubmitted.')
            self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL WHERE id=?', (entry['id'],))
            entry = None
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
        self._receipt_only(entry['id'], receipts)
        self.db.execute('UPDATE claude_mod_enrollments SET decision_seq=? WHERE id=?', (decision, entry['id']))
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
        """Trusted pure in-memory callback; no discovery or filesystem work (folders are resolved off the lock)."""
        current = self.room_snapshot() if self.room_snapshot else {}
        return agent in current and (not local_parent(agent, current[agent])
                                     or not within_project(current[agent].get('cwd'), project))

    def capability(self, agent, item=None):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = bool(entry and entry['generation'] and entry['last_seen'] > time.time() - LEASE and not self.store.worker_failed
                         and not entry['receipt_only'])          # receipts only: no new work is admitted
            if entry and self.room_veto(agent, entry['project']):
                ready = False
            if item is not None and entry and (not eligible(agent, item) or not within_project(item.get('cwd'), entry['project'])):
                ready = False
            return ready, entry, ('' if ready else 'Connect the official Motet plugin in this exact existing Claude chat.')

    def peer_ready(self, entry, item=None):
        """Transaction-owned facts only: no lock, filesystem, maintenance or commit."""
        if (not entry or not entry['active'] or not entry['grant_id'] or entry['peer_protocol'] != 1
                or not entry['generation'] or entry['last_seen'] <= time.time() - LEASE or self.store.worker_failed
                or entry['receipt_only']
                or self.room_veto(entry['agent'], entry['project'])):
            return False
        grant = self.db.execute('SELECT peer_receive FROM claude_mod_grants WHERE id=? AND active=1',
                                (entry['grant_id'],)).fetchone()
        return bool(grant and grant[0] == 1 and (item is None or
                    local_parent(entry['agent'], item) and within_project(item.get('cwd'), entry['project'])))

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
            return not self.store.worker_failed and bool(self.db.execute('SELECT 1 FROM claude_mod_enrollments WHERE active=1 AND generation IS NOT NULL AND receipt_only=0 AND last_seen>?', (time.time()-LEASE,)).fetchone())

    @staticmethod
    def _claim_folder(entry, item, reported, observed):
        """The folder gate for a new claim. With folder protocol 1 the host reports where the chat is: the project
        itself, or that same folder resolved for this poll, existing and inside the project. An older plugin reports
        nothing and gets the project folder only, as the stage (or, idle, the launch folder) shows it; that plugin
        itself submits only in the project folder. Anything else refuses the claim; the poll still succeeds."""
        if reported is not None:
            if reported == entry['project']:
                return True
            return bool(observed) and observed[0] == reported and observed[1] == entry['project'] and observed[2] is True
        cwd = item.get('cwd')
        return project_text(cwd) and cwd == entry['project']

    def _launch_proof(self, aid, project):
        try:
            return self.store.shared.metadata_target(aid, project)
        except Exception:
            return None

    def _launch_proven(self, aid, project, proof):
        """The chat directory's fresh proof that `project` is this chat's launch folder (first transcript cwd)."""
        return bool(isinstance(proof, dict) and proof.get('agent') == aid and proof.get('project') == project
                    and self.store.shared.metadata_current(proof))

    def _pair(self, data):
        """The unexpired pairing for this code and chat, checked before any discovery; no filesystem work."""
        schema(data, ('code', 'native_id', 'project') + (('decision_seq',) if isinstance(data, dict) and 'decision_seq' in data else ()))
        if (not isinstance(data['code'], str) or len(data['code']) > 100 or not ident(data['native_id'])
                or not project_text(data['project'])):
            raise ModError(400, 'Invalid Claude enrollment identity.')
        pair = self.db.execute('SELECT agent,project,expires,decision_seq FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),)).fetchone()
        if not pair or pair[2] <= time.time() or pair[0] != 'c:'+data['native_id']:
            raise ModError(403, 'Claude pairing code is expired, used, or does not match this chat.')
        return pair

    def enroll(self, data, proof=None, canonical=None):
        pair = self._pair(data)
        if not (project(data['project']) if canonical is None else canonical):
            raise ModError(400, 'Invalid Claude enrollment identity.')
        # The room saw the chat in pair[1] (a canonical name). A different project named by the plugin must be the
        # chat's recorded launch folder and hold pair[1]; the plugin's report alone never widens the project.
        if pair[1] != data['project'] and not (_textually_inside(pair[1], data['project'])
                                               and self._launch_proven(pair[0], data['project'], proof)):
            raise ModError(403, 'Claude pairing code is expired, used, or does not match this chat.')
        # Pairing is the owner's reconnect. It takes its place from the plugin command's reserved ticket when one is
        # sent, else from the ticket its code took when the owner created it in Motet, and it is committed here, only
        # once the code has been accepted above: a code Motet refuses leaves the chat's decision exactly as it was.
        # A pairing placed before the chat's latest opt-out is refused before anything changes.
        self._valid_ticket(data.get('decision_seq'))
        place = max(pair[3], data.get('decision_seq') or 0)
        latest, opted_out, opted_out_seq = self._decision(pair[0])
        if opted_out and place < opted_out_seq:
            raise ModError(409, 'This chat was disconnected by its owner after this pairing code was made.', 'decision_superseded')
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (pair[0],)).fetchall():
            self.uncertain(eid, 'This Claude enrollment was replaced. Earlier input may still run; nothing was resubmitted.')
        self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL WHERE agent=?', (pair[0],))
        if self.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0] >= MAX_ENROLLMENTS:
            raise ModError(429, 'Too many active Claude connections.', 'connection_limit')
        eid, token, generation = str(uuid.uuid4()), secrets.token_hex(32), str(uuid.uuid4())
        decision = self.decide(pair[0], False, place)['decision_seq'] if place > latest else latest
        self.db.execute('INSERT INTO claude_mod_enrollments (id,token_hash,agent,project,generation,last_seen,active,decision_seq) VALUES (?,?,?,?,?,?,1,?)', (eid, digest(token), pair[0], data['project'], generation, time.time(), decision))
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
        # Folder protocol 1: a poll may report the chat's current folder ('cwd'); an older plugin sends none.
        schema(data, self.fields(entry, ('generation', 'native_id', 'project', 'accept')
                                 + (('cwd',) if isinstance(data, dict) and 'cwd' in data else ())))
        self.native(entry, data)
        if data['project'] != entry['project'] or type(data['accept']) is not bool:
            raise ModError(409, 'The enrolled Claude project does not match.')
        if 'cwd' in data and not project_text(data['cwd']):
            raise ModError(400, 'Invalid Claude working folder.')

    def attachment_entry(self, agent, item):
        """Current derived owner enrollment, without maintenance or discovery."""
        if not eligible(agent, item) or self.store.worker_failed:
            return None
        row = self.db.execute('SELECT id FROM main.claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
        entry = self.get(row[0]) if row else None
        # Attachments are placed in the chat's current folder, so they keep the exact project match.
        if (not entry or entry['project'] != item['cwd'] or not ident(entry['grant_id'])
                or not ident(entry['client_id']) or not ident(entry['generation'])
                or entry['lifecycle_protocol'] != 2 or entry['last_seen'] <= time.time() - LEASE
                or entry['receipt_only'] or self.room_veto(agent, entry['project'])):
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
        # Attachments are placed in the chat's folder, so only the project folder itself may receive them. With
        # folder protocol 1 that is judged on the folder the host reports; the stage's or launch folder never
        # stands in for it. An older plugin reports none and keeps the stage's exact-project rule.
        if 'cwd' in data and data['cwd'] != entry['project']:
            return None
        if entry['receipt_only']:
            return None
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
            self.connection_decision(entry)
            self.validate_poll(entry, data)
            if data['accept'] and entry['receipt_only']:
                data = dict(data, accept=False)     # receipts only: heartbeats and outcomes, never a new claim
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
        # A chat that reports working in a folder inside its project may receive a new claim only on a resolution
        # of that reported folder started for this poll, off the lock; a cached answer, the stage's last folder or
        # the launch folder never authorises it.
        observed = None
        reported = data.get('cwd')
        if (new_rows and project_text(reported) and reported != entry['project']
                and _textually_inside(reported, entry['project']) and not _network(reported)):
            observed = (reported, entry['project'], RESOLVER.revalidate(reported, entry['project']))
        with self.store.lock, self.db:
            snapshot = agents() if callable(agents) else agents  # pure current room vetoes
            snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
            if entry['agent'] not in snapshot and proof and self.store.shared.metadata_current(proof):
                snapshot[entry['agent']] = self.metadata_item(proof)
            self.maintain()
            # Connection or grant replacement during preparation invalidates this
            # poll; retained original attempts can still recover after reconnect.
            entry = self.authenticate(token)
            self.connection_decision(entry)
            if entry['receipt_only']:
                data = dict(data, accept=False)
            result = self.poll(entry, data, snapshot, prepared, observed=observed)
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

    def poll(self, entry, data, agents, prepared, notice_proofs=None, observed=None):
        self.validate_poll(entry, data)
        self.db.execute('UPDATE claude_mod_enrollments SET last_seen=? WHERE id=?', (time.time(), entry['id']))
        rows = self.rows()
        busy = any(not peer_lifecycle.is_terminal(self.db, r['request_id']) and r.get('mod_enrollment') == entry['id'] and r.get('launched') and not r.get('settled') and r['state'] in ('queued', 'awaiting_reply') for r in rows)
        busy = busy or any(state != 'reserved' for _, state, _ in peer_lifecycle.blockers(self.db, entry['agent']))
        busy = busy or self.attachment_busy(entry['agent'])
        claim = None
        item = agents.get(entry['agent']) if isinstance(agents, dict) else None
        can_claim = local_parent(entry['agent'], item) and self._claim_folder(entry, item, data.get('cwd'), observed)
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
            # Only an explicit authenticated unknown receipt observes a hold.
            # The synthesized unknown body used for other late events is not one.
            peer_lifecycle.reconcile(self.db, row, hold_observed=kind == 'unknown')
        else:
            self.put(row, hold_observed=kind == 'unknown')
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
        if method == 'decision':
            with self.store.lock, self.db:
                self.maintain()
                return self.decision_call(data, token)
        if method == 'register':
            with self.store.lock, self.db:
                self.maintain()
                self.registration_access(data, token)
                aid = 'c:' + data['native_id']
                stored = self.db.execute('SELECT project FROM claude_mod_enrollments WHERE agent=? AND active=1 '
                                         'AND grant_id IS NOT NULL', (aid,)).fetchone()
            # Receipts only, for exactly the folder this chat's existing enrollment names: that is historical
            # evidence, so today's filesystem need not still spell it canonically (the folder may have been renamed
            # or replaced). It still receives no claim. Every other registration names a canonical folder now.
            historical = data.get('receipt_only') is True and stored is not None and stored[0] == data['project']
            if not historical and not project(data['project']):
                raise ModError(409, 'The Claude project is not canonical.', 'identity_rejected')
            snapshot = agents() if callable(agents) else agents
            proof = None
            observed = snapshot.get(aid) if isinstance(snapshot, dict) else None
            cwd = observed.get('cwd') if isinstance(observed, dict) else None
            if data.get('receipt_only') is True:
                if not isinstance(snapshot, dict) or aid not in snapshot:
                    proof = self._launch_proof(aid, None)
            elif not isinstance(snapshot, dict) or aid not in snapshot:
                try:
                    proof = self.store.shared.metadata_target(aid, data['project'])
                except Exception:
                    raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed') from None
            elif (data.get('receipt_only') is not True and project_text(cwd) and cwd != data['project']
                  and _textually_inside(cwd, data['project'])):
                # Outside the store lock: prove the launch folder and resolve the working folder.
                proof = self._launch_proof(aid, data['project'])
                RESOLVER.prime(cwd, data['project'])
            stored = stored[0] if stored else None
            if (data.get('receipt_only') is not True and project_text(stored) and stored != data['project']
                    and _textually_inside(stored, data['project'])):
                # An enrollment made from a subfolder by an earlier plugin names that subfolder; the plugin now
                # names the chat's project root. Prove the root is the launch folder before moving the enrollment.
                proof = proof or self._launch_proof(aid, data['project'])
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
        manual_move = None
        if method == 'connect':
            # Authenticate before any discovery. Only a genuine move of this manual pairing to a folder holding its
            # own is proved, off the lock; the pinned enrollment is checked again before anything changes.
            with self.store.lock, self.db:
                self.maintain()
                pinned = self.authenticate(token)
            if (not pinned['grant_id'] and isinstance(data, dict) and isinstance(data.get('project'), str)
                    and data['project'] != pinned['project'] and data.get('native_id') == pinned['agent'][2:]
                    and _textually_inside(pinned['project'], data['project']) and project(data['project'])):
                manual_move = (pinned['id'], pinned['project'], self._launch_proof(pinned['agent'], data['project']))
        proof, canonical = None, False
        if method == 'enroll':
            # Check the one-time code first; prove a wider project only when the pairing folder differs from it.
            with self.store.lock, self.db:
                self.maintain()
                pair = self._pair(data)
            canonical = project(data['project'])
            if canonical and pair[1] != data['project'] and _textually_inside(pair[1], data['project']):
                proof = self._launch_proof(pair[0], data['project'])
        with self.store.lock, self.db:
            self.maintain()
            if method == 'enroll':
                return self.enroll(data, proof, canonical)
            if method == 'setup':
                return self.setup(data, port)
            entry = self.authenticate(token)
            if method == 'connect':
                if entry['grant_id']:
                    raise ModError(409, 'Automatic Claude connections reconnect through registration.', 'register_required')
                schema(data, ('native_id', 'project') + tuple(k for k in ('receipt_only', 'decision_seq')
                                                               if isinstance(data, dict) and k in data))
                self.native(entry, data, connected=False)
                # Refused before any lease exists when the owner disconnected this chat after the command's ticket.
                decision = self.decision_gate(entry['agent'], data.get('decision_seq'))
                receipts = data.get('receipt_only', False)
                if receipts not in (True, False) or 'receipt_only' in data and receipts is not True:
                    raise ModError(400, 'Invalid Claude receipt-only connection.')
                moved = data['project'] != entry['project']
                if receipts and moved:
                    raise ModError(409, 'No earlier Motet connection of this chat holds that folder.', 'receipt_context_missing')
                # A manual pairing made from a subfolder by an earlier plugin may move to the chat's project folder:
                # only on this manual credential, only to the proven launch folder, only from a folder inside it,
                # and only with nothing in flight. It stays a manual, owner-only connection.
                if moved and not (manual_move and manual_move[:2] == (entry['id'], entry['project'])
                                  and self._launch_proven(entry['agent'], data['project'], manual_move[2])):
                    raise ModError(409, 'The enrolled Claude project does not match.')
                if moved:
                    self._move_ready(entry)
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                generation = str(uuid.uuid4())
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,last_seen=?,decision_seq=? WHERE id=?', (generation, time.time(), decision, entry['id']))
                if moved:
                    self.db.execute('UPDATE claude_mod_enrollments SET project=? WHERE id=?', (data['project'], entry['id']))
                self._receipt_only(entry['id'], receipts)
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
        if (method not in ('setup', 'register', 'enroll', 'connect', 'poll', 'event', 'disconnect', 'decision')
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
