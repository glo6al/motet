"""Explicit screen-authorized Codex questions. Importing this module does no I/O.

Queue acceptance is not delivery. Only the exact outgoing user envelope followed
by a matching completed turn can produce an answer. Uncertain sends are never retried.
"""
import copy
import errno
import hmac
import json
import math
from contextlib import contextmanager, nullcontext
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from attachments_owner_guards import (OwnerAttachmentGuard, OwnerCapacityUnavailable, OwnerGuardError,
                                      alias_state, bind_uninstalled_payload_state, check_uninstalled_peer_orphans)
from attachments_owner_marker import CLEAR, UNCERTAIN
import claude_channel
import claude_mod
import peer_bridge
import peer_inbox
import peer_lifecycle
import peer_progress
import codex_transcript
import private_storage

CLI = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
ACTIVE = ('sending', 'queued', 'awaiting_reply', 'unknown', 'expired')
MAX_RECORDS, MAX_PENDING, MAX_LINE, MAX_READ = 256, 8, 1_000_000, 2_000_000
TTL, RETENTION = 900, 86400
CODEX_REPLY_EXPIRED = 'No matching reply yet. The question may still be queued; Motet will not resend.'
CODEX_PREVIOUS_SOURCE_UNAVAILABLE = 'The original Codex transcript is temporarily unavailable. Motet will check it again.'
CODEX_NEEDS_MAC = 'Messaging Codex needs the Codex desktop app on a Mac for now.'
CODEX_CONTROLS_NEED_MAC = 'Live controls for Codex need the Codex desktop app on a Mac for now.'
CLAUDE_SETUP_HINT = 'Claude chats can be messaged once Claude is set up in Motet.'
CLAUDE_CONNECT_HINT = 'Claude chats can be messaged once one connects through the Motet plugin.'
RETAINED_STATE_UNAVAILABLE = ('Agent messaging cannot start here: its state folder holds attachment or managed-chat '
                              'records that this system cannot check safely yet.')
RETAINED_STATE_FOREIGN = ('Agent messaging cannot start here: its state folder holds attachment records made by Motet '
                          'on another computer or in another folder, and they cannot be used here.')
STATE_FOLDER_REFUSED = 'Messaging state directory must be private and owned by you.'
LOCK_REFUSED = 'Messaging lock must be a private regular file.'
DATABASE_REFUSED = 'Messaging database must be a private regular file.'
CLAUDE_OWNER_UNAVAILABLE = ("Exact named-chat delivery requires the official Motet plugin connected in this Claude chat. "
                           "Connect it before sending. Nothing was sent.")


class MessagingError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class MessageNotSubmitted(MessagingError):
    """The server rejected the message before writing a queue entry."""


class MessagingStorageUnavailable(OSError):
    """The store refused to start for a reason the owner can be shown as it is (fixed words, no paths)."""


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_question(question):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000 or '\x00' in question:
        raise MessagingError(400, 'Write a question between one and two thousand characters.')
    try:
        question.encode('utf-8')
    except UnicodeError:
        raise MessagingError(400, 'The question contains invalid text.')


def validate_target(agent, agents):
    if not isinstance(agent, str) or agent[:2] not in ('x:', 'c:') or not valid_uuid(agent[2:]):
        raise MessagingError(400, 'Select an exact local chat identifier. New named-chat delivery currently supports Codex only.')
    a = agents.get(agent)
    if not a:
        raise MessagingError(404, 'That agent is no longer on the stage.')
    expected = 'Claude' if agent.startswith('c:') else 'Codex'
    if a.get('runtime') != expected or a.get('observed') or (expected == 'Claude' and a.get('sub')):
        raise MessagingError(400, 'That target is not an eligible local parent chat. New named-chat delivery currently supports Codex only.')
    return str(a.get('name') or agent)[:120]


def envelope(request_id, question, version=2):
    # Version one remains byte-exact for replies to messages sent before this fix.
    guidance = ('This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.'
                if version == 1 else
                'This message relays the owner\'s request. Preserve its scope and authorization: '
                'an explicit work instruction authorizes that work under your existing permissions; '
                'a status question asks only for information. Do not infer additional tasks or permissions. '
                'For a progress question, inspect your own current state.')
    relay = ('The owner asks through the Officer of the Watch:' if version == 1
             else 'The owner asks through Motet:')
    return ('WATCH_REQUEST ' + request_id + '\n' + relay + '\n' + question +
            '\nReply in your final answer with WATCH_REPLY ' + request_id + ' on its own line, followed by your answer. '
            + guidance)


def outgoing_message(row):
    return row.get('envelope') or envelope(row['request_id'], row['question'], version=1)


def public(row):
    result = {k: row.get(k, '') for k in ('request_id', 'agent', 'name', 'question', 'state', 'reply', 'error')}
    created = row.get('created')
    if type(created) in (int, float) and created > 0:
        try:
            if math.isfinite(created):
                result['created'] = created
        except OverflowError:
            pass
    if isinstance(row.get('delivery_warning'), str) and row['delivery_warning']:
        result['delivery_warning'] = row['delivery_warning']
    return result


def attachment_records_checkable():
    """The attachment namespace and managed-registry records are checked through private_storage's no-link folder
    walk (POSIX descriptors, owner and mode; Windows handles, owner and access list) and its lock: wherever that walk
    is available, Windows included."""
    return private_storage.folder_walk_available()


def codex_records_checkable():
    """Codex reply tracking checks transcripts with POSIX owner and mode rules: macOS and Linux only."""
    return not private_storage.WIN and private_storage.folder_walk_available()


@contextmanager
def foreign_records_refused():
    """Windows: a refusal from the attachment namespace fence becomes RETAINED_STATE_FOREIGN, a reason shown as it
    is. Nothing on Windows makes these records yet (only delivery to agent chats installs them, and it is off there),
    so any that exist were made by Motet on another computer or in another folder, and the identities they record
    belong there. Revisit once that delivery is ported. POSIX keeps its own refusal (the generic storage reason)."""
    if not private_storage.WIN:
        yield
        return
    from attachments_native_namespace import NamespaceError
    try:
        yield
    except NamespaceError:
        raise MessagingStorageUnavailable(RETAINED_STATE_FOREIGN) from None


def private_state_folder(state_dir):
    """Make the messaging state folder if it is missing, then refuse it unless it is private to this user.

    POSIX: created 0700 (with any missing parents); it must be a real folder (not a link), owned by this user,
    with nothing for group or others. Windows: created with a protected this-user/SYSTEM access list; it must not
    be a link or junction, must be owned by this user, and its list may name only this user, SYSTEM,
    Administrators and OWNER RIGHTS. An existing folder is checked, never loosened or repaired."""
    if private_storage.WIN:
        os.makedirs(os.path.dirname(os.path.abspath(state_dir)), exist_ok=True)
        if not os.path.lexists(state_dir):
            try:
                private_storage.make_private_folder(state_dir)
            except OSError:
                if not os.path.lexists(state_dir):   # something else made it first: check that one instead
                    raise
        try:
            # The folders above are resolved once (a moved profile often uses a junction); then the walk opens each
            # folder by handle and checks this one's owner and access list on its handle.
            os.close(private_storage.open_no_links(private_storage.anchored(state_dir), directory=True, private=True))
        except OSError:
            raise OSError(STATE_FOLDER_REFUSED) from None
        return
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    try:
        private_storage.check_private(state_dir, directory=True)
    except private_storage.PrivateStorageError:
        raise OSError(STATE_FOLDER_REFUSED) from None


def private_state_file(path, create, refused):
    """A read-write descriptor for a private regular file with one name, opened without following a link.

    A refused file raises ``OSError(refused)``; a failed open raises the system's own error, as before."""
    try:
        return private_storage.open_private_rw(path, create)
    except private_storage.PrivateStorageError:
        raise OSError(refused) from None


def check_database_companions(path):
    """Windows only: SQLite makes its rollback journal by name next to the database, with the folder's
    inherited access list. Refuse a journal (or a stray -wal/-shm) that is a link, junction, not a plain file or a
    file with a second name, so SQLite never writes through one to somewhere outside the private folder. A plain leftover journal is kept: SQLite needs it to
    roll back an interrupted write."""
    if not private_storage.sqlite_companions_plain(path):
        raise OSError(DATABASE_REFUSED)


def close_owner_lock(fd):
    """Release the lifetime lock and close it. Windows may hold a lock for a while after its file closes."""
    private_storage.close_lock(fd)


class MessagingStore:
    def __init__(self, state_dir, sessions_dir, cli=CLI, channel_root=None, managed=None):
        self.managed = managed  # Independent owner ledger; never fed to legacy/peer workers.
        self.state_dir = os.path.abspath(state_dir)
        self.sessions_dir, self.cli = os.path.realpath(sessions_dir), cli
        self.channel_root = channel_root or os.path.join(state_dir, "claude-channels")
        self.codex_available, self.worker_failed = None, False
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.thread, self.available, self.reason = None, False, 'Messaging has not started.'
        self._attachment_reconciliation_held = False
        self._native_owner_lock = threading.Lock()
        self._native_owner_runtime = None
        self._native_owner_error = None
        self._native_owner_stopped = False
        self._native_composition_lock = threading.Lock()
        self._native_composition = None
        self._native_composition_error = None
        private_state_folder(state_dir)
        # A second WATCH process must not launch the same persisted request. The
        # lifetime lock also protects recovery from marking another worker unknown.
        self.owner_fd = private_state_file(os.path.join(state_dir, 'officer-messages.lock'), True, LOCK_REFUSED)
        self.db = None
        try:
            if not private_storage.try_lock(self.owner_fd):
                raise BlockingIOError(errno.EWOULDBLOCK, os.strerror(errno.EWOULDBLOCK))
            path = os.path.join(state_dir, 'officer-messages.sqlite3')
            fence = self._retained_namespace_before_open(state_dir)
            with foreign_records_refused(), self.lock, \
                    (fence.owner_open() if fence is not None else nullcontext()) as expected:
                # The checked descriptor stays open while SQLite opens the same name. On Windows it does not
                # share delete, so the file cannot be renamed or replaced in between.
                fd = private_state_file(path, expected is None, DATABASE_REFUSED)
                try:
                    if expected is not None:
                        # [st_dev, st_ino] on POSIX; the handle's volume serial and file index on Windows.
                        from attachments_native_namespace import descriptor_identity
                        if descriptor_identity(fd) != expected:
                            raise OSError(DATABASE_REFUSED)
                    if private_storage.WIN:
                        check_database_companions(path)
                    if expected is not None:
                        self.db = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=rw', uri=True,
                                                  check_same_thread=False, timeout=peer_bridge.DB_TIMEOUT)
                        fence.require_owner_connection(self.db, expected)
                    else:
                        self.db = sqlite3.connect(path, check_same_thread=False, timeout=peer_bridge.DB_TIMEOUT)
                finally:
                    os.close(fd)
            self.db.execute('PRAGMA secure_delete = ON')
            if fence is None and self.db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='owner_attachment_namespace_identity'").fetchone():
                if private_storage.WIN:          # copied without its witness: see foreign_records_refused
                    raise MessagingStorageUnavailable(RETAINED_STATE_FOREIGN)
                raise OwnerGuardError('The retained owner namespace witness is unavailable.')
            self._attachment_guard = OwnerAttachmentGuard.probe(self.db, lock=self.lock)
            if self._attachment_guard is None:
                self._uninstalled_payload_state = bind_uninstalled_payload_state(self.db)
            with peer_bridge.transaction(self.db):
                if self._attachment_guard is None:
                    check_uninstalled_peer_orphans(self.db, self._uninstalled_payload_state)
                else:
                    self._attachment_guard.require_schema()
                    self._attachment_guard.check_startup()
                peer_bridge.shared_schema(self.db, peer_payload_readable=self._peer_payload_readable_at_startup)
                peer_inbox.schema(self.db)
                startup_now = peer_inbox.now_ms()
                while peer_inbox.cleanup(self.db, startup_now) == peer_inbox.CLEANUP_ROWS:
                    pass
            self.db.execute('CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS expired_controls (id TEXT PRIMARY KEY)')
            self.db.execute('CREATE TABLE IF NOT EXISTS request_fulfillments (request_id TEXT PRIMARY KEY, control_request_id TEXT NOT NULL, confirmed_at REAL NOT NULL)')
            for control in self._controls():
                if control['state'] == 'sending':
                    control.update(state='unknown', error='Motet restarted during control dispatch. It will not retry.')
                    self.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(control), control['request_id']))
            self.db.execute('CREATE TABLE IF NOT EXISTS asks (id TEXT PRIMARY KEY, user_text TEXT NOT NULL, created REAL NOT NULL, decision TEXT)')
            self.mods = claude_mod.ClaudeMod(self)
            for row in self._rows():
                if row.get('transport') == claude_mod.TRANSPORT:
                    continue
                if row['state'] == 'sending':
                    peer_meta = peer_bridge.transport_metadata(self.db, transport_ids=(row['request_id'],)).get(row['request_id'])
                    if peer_meta is not None and peer_meta['launched_at'] is None:
                        row.update(state='failed', settled=True, error='Motet restarted before peer dispatch was authorized. Nothing was sent.')
                    else:
                        row.update(state='unknown', error='Motet restarted during sending. Delivery is unknown; it will not resend.')
                    self._save(row)
            self._reconcile_fulfillments()
            self.db.commit()
            self.peers = peer_bridge.PeerBridge(self)
            self.shared = peer_bridge.SharedBridge(self)
            import peer_reply_delivery
            self.reply_delivery = peer_reply_delivery.ReplyDelivery(self)
        except BaseException:
            if self.db is not None:
                self.db.close()
            close_owner_lock(self.owner_fd)
            self.owner_fd = None
            raise

    @staticmethod
    def _retained_namespace_before_open(state_dir):
        root = Path(state_dir)

        def present(path):
            try:
                path.lstat()
                return True
            except FileNotFoundError:
                return False

        # A surviving managed identity can prove installation even when a new
        # owner file sits at the expected path and both outer files are gone.
        candidates = (root / 'native-attachment-namespace.expected', root / 'native-attachment-namespace.authority',
                      root / 'managed-claude' / 'registry.sqlite3')
        if not any(present(path) for path in candidates):
            return None
        if not attachment_records_checkable():
            # The fence opens these records through the no-link folder walk; without it, refuse rather than open the
            # database unfenced.
            raise MessagingStorageUnavailable(RETAINED_STATE_UNAVAILABLE)
        from attachments_native_namespace import NativeNamespaceFence
        # No witness (detected() False: say an old managed registry that never had an identity, and no anchor or
        # authority) means the owner side was never installed here, so there is nothing to fence and the database
        # opens as it always has, on every system. An owner database that does carry the owner identity without its
        # witness is refused below.
        with foreign_records_refused():
            fence = NativeNamespaceFence(state_dir)
            return fence if fence.detected() else None

    def claim_ask(self, request_id, user_text):
        """Claim interpretation once. A crash while planning never reruns a model."""
        if not valid_uuid(request_id):
            raise MessagingError(400, 'A valid request identifier is required.')
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 4000 or '\x00' in user_text:
            raise MessagingError(400, 'The latest request must contain between one and four thousand characters.')
        try:
            user_text.encode('utf-8')
        except UnicodeError:
            raise MessagingError(400, 'The request contains invalid text.')
        with self.lock:
            self._reject_attachment_reservation(request_id)
            self._reject_expired_control(request_id)
            old = self.db.execute('SELECT user_text, decision FROM asks WHERE id=?', (request_id,)).fetchone()
            if old:
                if old[0] != user_text:
                    raise MessagingError(409, 'That request identifier belongs to different text.')
                if old[1] is None:
                    raise MessagingError(409, 'Request interpretation already started; check status. It will not be resent.')
                return json.loads(old[1])
            now = time.time()
            with self.db:
                unreserved = self._unreserved('asks.id')
                self.db.execute(f'DELETE FROM asks WHERE created < ? AND {unreserved}', (now - RETENTION,))
                count, recent = self.db.execute(f'SELECT COUNT(*), COALESCE(SUM(created > ?),0) FROM asks WHERE {unreserved}', (now - 60,)).fetchone()
                if count >= MAX_RECORDS or recent >= 20:
                    raise MessagingError(429, 'Too many Motet requests are being kept. Try again later.')
                self.db.execute('INSERT INTO asks VALUES (?,?,?,NULL)', (request_id, user_text, now))
            return None

    def decide_ask(self, request_id, decision):
        """Durably freeze routing before submit; no changed plan on a retry."""
        if not valid_uuid(request_id) or not isinstance(decision, dict) or decision.get('kind') not in ('relay', 'none', 'clarify', 'control'):
            raise MessagingError(400, 'Invalid request decision.')
        try:
            encoded = json.dumps(decision, sort_keys=True)
        except (ValueError, TypeError):
            raise MessagingError(400, 'Invalid request decision.')
        if len(encoded) > 16000:
            raise MessagingError(400, 'The request decision is too large.')
        with self.lock:
            self._reject_attachment_reservation(request_id)
            old = self.db.execute('SELECT decision FROM asks WHERE id=?', (request_id,)).fetchone()
            if old is None:
                raise MessagingError(409, 'The request must be claimed before deciding.')
            if old[0] is not None:
                if old[0] != encoded:
                    raise MessagingError(409, 'The request already has a different decision.')
                return json.loads(old[0])
            with self.db:
                self.db.execute('UPDATE asks SET decision=? WHERE id=? AND decision IS NULL', (encoded, request_id))
            return decision

    def _reject_expired_control(self, request_id):
        # IDs alone survive retention: an old HTTP retry must never target a new turn.
        if self.db.execute('SELECT 1 FROM expired_controls WHERE id=?', (request_id,)).fetchone():
            raise MessagingError(409, 'This control request has expired and cannot be replayed. Issue a new explicit request; nothing was sent.')

    def _attachment_reservation_sql(self, column):
        guard = self._attachment_guard
        if guard is not None:
            guard.require_schema()
            return guard.reservation_state(column)
        return alias_state(column, self._uninstalled_payload_state)

    def _attachment_reservation(self, request_id):
        state = self._attachment_reservation_sql('candidate.id')
        return self.db.execute(f'SELECT {state} FROM (SELECT ? AS id) AS candidate', (request_id,)).fetchone()[0]

    def _peer_payload_readable_at_startup(self, transport_id):
        state = self._attachment_reservation(transport_id)
        if state == UNCERTAIN:
            return False
        if state != CLEAR:
            raise OwnerGuardError('An owner request needs recovery before peer migration.')
        return True

    def _peer_payload(self, transport_id):
        """Get classification and legacy body from one exact SQLite snapshot."""
        with self.lock:
            state = self._attachment_reservation_sql('candidate.id')
            selection = self.db.execute(f'''WITH selected AS (
                SELECT candidate.id,{state} AS reservation FROM (SELECT ? AS id) AS candidate
            ) SELECT reservation, CASE WHEN reservation={CLEAR} THEN
                (SELECT data FROM requests WHERE requests.id=selected.id) END FROM selected''', (transport_id,))
            reservation, data = selection.fetchone()
        if reservation == CLEAR:
            return 'clear', (data,) if data is not None else None
        return ('uncertain' if reservation == UNCERTAIN else 'conflict'), None

    def _attachment_reserved(self, request_id):
        return self._attachment_reservation(request_id) != CLEAR

    def _reject_attachment_reservation(self, request_id):
        try:
            reservation = self._attachment_reservation(request_id)
        except OwnerGuardError:
            raise MessagingError(503, 'Owner attachment protection is unavailable.') from None
        if reservation == UNCERTAIN:
            raise MessagingError(503, 'The stored owner request could not be safely classified. Owner messaging recovery is required.')
        if reservation != CLEAR:
            raise MessagingError(409, 'That identifier is protected by owner attachment storage.')

    def check_owner_request_namespace(self, request_id):
        # Also used before the server's early managed-message recovery lookup.
        with self.lock:
            self._reject_attachment_reservation(request_id)

    def _unreserved(self, column):
        if self._attachment_guard is None:
            return f'{alias_state(column, self._uninstalled_payload_state)} = {CLEAR}'
        self._attachment_guard.require_schema()
        return f'NOT {self._attachment_guard.reserved_request(column)}'

    def _controls(self):
        unreserved = self._unreserved('controls.id')
        return [json.loads(raw) for (raw,) in self.db.execute(f'SELECT data FROM controls WHERE {unreserved}')]

    def _rows(self):
        rows = []
        if self._attachment_guard is None:
            state = self._uninstalled_payload_state('requests.data')
            stored = self.db.execute(f'SELECT id,data FROM requests WHERE {state} = {CLEAR}').fetchall()
        else:
            stored = self._attachment_guard.legacy_rows()
        peer_meta = peer_bridge.transport_metadata(self.db, transport_ids=(ident for ident, _ in stored))
        peer_ids = set(peer_meta)
        for ident, raw in stored:
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get('request_id') != ident:
                    raise ValueError()
                if ident in peer_ids and (row.get('origin') != 'peer' or
                        (peer_meta[ident]['version'] == 2 and row.get('bridge_version') != 2)):
                    raise ValueError()
                if row.get('origin') == 'peer':
                    if (not self._timestamp(row.get('created')) or row.get('state') not in
                            ('sending', 'queued', 'awaiting_reply', 'quarantined', 'unknown', 'expired', 'failed', 'answered')
                            or not isinstance(row.get('agent'), str) or row['agent'][:2] not in ('x:', 'c:')
                            or not valid_uuid(row['agent'][2:]) or type(row.get('launched')) is not bool):
                        raise ValueError()
                rows.append(row)
            except (ValueError, TypeError):
                # Corrupt peer payloads must not take down owner reply tracking.
                if ident in peer_ids:
                    continue
                raise
        return rows

    def _history_outcome(self, row, kind='message'):
        observer = getattr(self, 'history_observer', None)
        if callable(observer):
            try:
                observer({key: row[key] for key in ('request_id', 'origin', 'state', 'agent', 'action', 'created', 'completed_at') if key in row}, kind)
            except Exception:
                pass  # History failure never changes delivery or triggers a retry.

    def _save(self, row, *, commit=True, ordinary_proof=None, observations=(), hold_observed=False):
        runtime = self._native_owner_runtime
        progress = {'observations': observations} if observations else {}
        if hold_observed:
            progress['hold_observed'] = True
        if commit and runtime is not None and not self.db.in_transaction:
            with runtime.ledger.ordinary_writer() as proof:
                self._write_request(row, proof, **progress)
            self._history_outcome(row)
            return
        try:
            self._write_request(row, ordinary_proof, **progress)
            if commit:
                self.db.commit()
                self._history_outcome(row)
        except BaseException:
            self.db.rollback()
            raise

    def _ordinary_row(self, row):
        return row.get('origin') != 'peer' and not peer_lifecycle.metadata(self.db, row['request_id'])

    def _write_request(self, row, proof, observations=(), *, hold_observed=False):
        if self._attachment_reserved(row['request_id']):
            raise OwnerGuardError('A protected owner request needs recovery before ordinary writeback.')
        if proof is not None and not self._ordinary_row(row):
            proof.eligible = False
        if row.get('origin') == 'peer' and row.get('runtime') == 'codex':
            previous = peer_lifecycle.get(self.db, row['request_id'])
            reason = peer_lifecycle.CODEX_RETENTION_HOLD
            if previous and previous['evidence'].get('codex_retention_hold') == reason:
                # A poll detached before pruning cannot restore content or revise the hold.
                row.update(state='unknown', error=reason, reply='', candidate_reply='')
                return
        # Reconcile the original receipts before suppressing a stale public
        # payload. A pruned terminal attempt must never regain its content.
        terminal = peer_lifecycle.project_terminal(self.db, row['request_id'], dict(row))
        retained = self.db.execute('SELECT 1 FROM requests WHERE id=?', (row['request_id'],)).fetchone() if terminal else True
        peer_lifecycle.reconcile(self.db, row, hold_observed=hold_observed)
        peer_lifecycle.project_terminal(self.db, row['request_id'], row)
        if not terminal or retained:
            identity, payload = row['request_id'], json.dumps(row)
            if proof is None:
                self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (identity, payload))
            else:
                write = self.db.execute('''INSERT OR REPLACE INTO requests SELECT ?,?
                    WHERE NOT EXISTS (SELECT 1 FROM requests WHERE id=? AND data=?)''',
                                        (identity, payload, identity, payload))
                proof.record(write)
            if row.get('origin') == 'peer' and row.get('bridge_version') == 2:
                peer_progress.ensure(self, row)
                for kind in observations:
                    peer_progress.observe(self, row, kind)

    def probe(self):
        if sys.platform != 'darwin':
            # The Codex queue command ships inside the macOS app; Claude chats are messaged through the plugin.
            return False, CODEX_NEEDS_MAC
        try:
            version = subprocess.run([self.cli, '--version'], capture_output=True, timeout=5, text=True)
            help_result = subprocess.run([self.cli, 'queue', '--help'], capture_output=True, timeout=5, text=True)
            if version.returncode or not re.search(r'codex\S*\s+\d+\.\d+', version.stdout) or help_result.returncode or not all(s in help_result.stdout for s in ('--thread', '--message')):
                return False, 'The bundled Codex executable does not advertise the required queue command.'
            return True, ''
        except (OSError, subprocess.SubprocessError):
            return False, 'The bundled Codex executable is unavailable.'

    def start(self):
        self.codex_available, self.reason = self.probe()
        self.available = self.codex_available or claude_channel.any_configured(self.channel_root)
        if self.shared.enabled:
            try:
                self.shared.ensure_directory()
            except Exception:
                # Discovery failure must not disable owner messaging. RPC retains
                # its normal retry path if setup becomes available later.
                pass
        self.thread = threading.Thread(target=self._run, daemon=True, name='watch-agent-messages')
        self.thread.start()

    def stop_native_attachment_runtime(self):
        with self._native_composition_lock, self._native_owner_lock:
            self._native_owner_stopped = True
            composition, runtime = self._native_composition, self._native_owner_runtime
        if composition is not None:
            composition.stop()
        if runtime is not None:
            runtime.stop()

    def close(self):
        self.stop.set()
        self.stop_native_attachment_runtime()
        if self.thread:
            self.thread.join(15)
        directory_thread = self.shared.directory_thread
        if directory_thread:
            directory_thread.join(2)
        if self.shared.readiness_thread:
            self.shared.readiness_thread.join(2)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('Messaging worker is still stopping; ownership retained.')
            self.db.close()
            if self.owner_fd is not None:
                close_owner_lock(self.owner_fd)
                self.owner_fd = None

    def _metadata(self, path, ident):
        try:
            selected, source, st, _ = codex_transcript.open_pinned(self.sessions_dir, path, ident)
            source.close()
            if selected != path:
                raise codex_transcript.Unavailable('The active native transcript changed.')
            return st
        except (OSError, codex_transcript.Unavailable) as error:
            raise MessagingError(400, str(error)) from None

    def _target_log(self, ident):
        try:
            path, source, st, _ = codex_transcript.resolve(self.sessions_dir, ident)
            source.close()
            return path, st
        except (OSError, codex_transcript.Unavailable) as error:
            raise MessagingError(409, str(error)) from None

    def snapshot(self):
        managed = {'requests': [], 'available': False}
        managed_error = ''
        if self.managed is not None:
            try:
                managed = self.managed.owner_snapshot()
            except Exception:
                managed_error = 'Managed Claude state is unavailable; recovery is required.'
        managed_ready = bool(managed['available'])
        claude_ready = (self.mods.any_ready() or claude_channel.any_configured(self.channel_root)) and not self.worker_failed
        codex_ready = (self.available if self.codex_available is None else self.codex_available) and not self.worker_failed
        with self.lock:
            now = time.time()
            managed_requests = [row for row in managed['requests'] if not self._attachment_reserved(row['request_id'])]
            controls = self._controls()
            retained = [r for r in controls if r['state'] == 'sending' or now - r['created'] <= RETENTION]
            return {'requests': [self._public_request(r) for r in sorted((r for r in self._rows() if r.get('origin') != 'peer'), key=lambda x: x['created'])] + managed_requests,
                    'controls': [self._public_control(r) for r in sorted(retained, key=lambda x: (x['created'], x['request_id']))],
                    'available': codex_ready or claude_ready or managed_ready,
                    'reason': '' if codex_ready or claude_ready or managed_ready else self._unavailable_reason(),
                    'managed_error': managed_error,
                    'capabilities': {'codex': codex_ready, 'claude': claude_ready or managed_ready,
                                     **({'claude_managed': managed_ready} if self.managed is not None else {})}}

    def _unavailable_reason(self):
        # Off a Mac only Codex is out of reach: say how Claude chats become reachable instead.
        if self.reason == CODEX_NEEDS_MAC:
            set_up = self.db.execute('SELECT 1 FROM claude_mod_grants WHERE active=1 UNION ALL '
                                     'SELECT 1 FROM claude_mod_enrollments WHERE active=1 LIMIT 1').fetchone()
            return CODEX_NEEDS_MAC + ' ' + (CLAUDE_CONNECT_HINT if set_up else CLAUDE_SETUP_HINT)
        return self.reason

    def owner_capabilities(self, agent, agents):
        """Advisory eligibility only; never starts native observation or resolves a turn."""
        if self.managed is not None:
            try:
                if self.managed.owns_agent(agent):
                    return self.managed.owner_capabilities(agent)
            except Exception:
                raise MessagingError(503, 'Managed Claude state is unavailable; recovery is required.') from None
        reason = ''
        try:
            validate_target(agent, agents)
        except MessagingError as error:
            reason = str(error)
        codex = not reason and agent.startswith('x:')
        queue_reason = reason
        claude_setup = None
        if isinstance(agent, str) and agent.startswith('c:'):
            status = self.mods.setup_status(agents)
            ready, entry, _ = self.mods.capability(agent, agents.get(agent, {}))
            state = ('connected' if ready else 'setup_needed' if not status['authorized']
                     else 'disconnected' if entry else 'waiting')
            claude_setup = dict(authorized=status['authorized'], state=state, peer_receive=status['peer_receive'])
        if not reason and not codex:
            queue_reason = ('' if ready else 'Waiting for this Claude chat to load or reconnect the Motet plugin.'
                            if status['authorized'] else 'Set up Claude once in Motet to connect eligible local chats.')
        elif codex and (self.worker_failed or not (self.available if self.codex_available is None else self.codex_available)):
            queue_reason = self.reason or 'Codex queue transport is unavailable.'
        if codex and not queue_reason:
            try:
                self._target_log(agent[2:])
            except MessagingError as error:
                queue_reason = str(error)
            except (OSError, ValueError, TypeError):
                queue_reason = 'The local transcript identity is unavailable or unreadable.'
        control_reason = reason
        if not reason and (not codex or agents[agent].get('sub') or agents[agent].get('parent')):
            control_reason = 'Live controls support local parent Codex desktop chats only.'
        elif not reason and sys.platform != 'darwin':
            control_reason = CODEX_CONTROLS_NEED_MAC
        result = {'agent': agent, 'actions': {
            'queue': {'available': not bool(queue_reason), 'reason': queue_reason, 'mode': 'queue'},
            'steer': {'supported': not bool(control_reason), 'reason': control_reason, 'mode': 'new_instruction'},
            'interrupt': {'supported': not bool(control_reason), 'reason': control_reason, 'mode': 'interrupt'},
            'promote_queued': {'available': False, 'reason': 'Promoting an existing queued message is unavailable.', 'mode': 'queued_promotion'}}}
        if claude_setup is not None:
            result['claude_setup'] = claude_setup
        return result

    def owner_action(self, action, agent, text, request_id, agents):
        """The authenticated owner gateway. Replay checks precede new-send eligibility.

        The ledger methods own atomic duplicate/conflict checks and dispatch preflight;
        preview capabilities are never caller authority and never gate a durable replay.
        """
        if action == 'queue':
            return self.submit(agent, text, request_id, agents)
        if action in ('steer', 'interrupt'):
            return self.submit_control(action, agent, text, request_id, agents)
        raise MessagingError(400, 'Unsupported owner action. Promoting queued messages is unavailable.')

    def submit(self, agent, question, request_id, agents):
        if not valid_uuid(request_id):
            raise MessagingError(400, 'A valid request identifier is required.')
        with self.lock:
            self._reject_attachment_reservation(request_id)
            self._reject_expired_control(request_id)
            if self.managed is not None:
                try:
                    retained = self.managed.lookup_message(request_id)
                    owned = self.managed.owns_agent(agent)
                    if retained or owned:
                        if (self.db.execute('SELECT 1 FROM requests WHERE id=?', (request_id,)).fetchone()
                                or self.db.execute('SELECT 1 FROM controls WHERE id=?', (request_id,)).fetchone()):
                            raise MessagingError(409, 'That identifier belongs to another owner or peer operation.')
                        return self.managed.owner_send(agent, question, request_id)
                except MessagingError:
                    raise
                except Exception as error:
                    from claude_managed import ManagedError
                    if isinstance(error, ManagedError):
                        raise MessagingError(error.status, str(error)) from None
                    raise MessagingError(503, 'Managed Claude state is unavailable; recovery is required.') from None
            validate_question(question)
            rows = self._rows()
            old = next((r for r in rows if r['request_id'] == request_id), None)
            if old:
                if old.get('origin') == 'peer':
                    raise MessagingError(409, 'That identifier is reserved for a different origin.')
                if old['agent'] != agent or old['question'] != question:
                    raise MessagingError(409, 'That request identifier belongs to a different question.')
                return self._public_request(old)
            if self.db.execute('SELECT 1 FROM controls WHERE id=?', (request_id,)).fetchone():
                raise MessagingError(409, 'That request identifier already belongs to a live control.')
            name = validate_target(agent, agents)
            queue = self.owner_capabilities(agent, agents)['actions']['queue']
            if not queue['available']:
                raise MessagingError(503, queue['reason'])
            now = time.time()
            guard = self._attachment_guard
            rows = [r for r in self._prune(rows, now, ordinary_only=guard is not None) if r.get('origin') != 'peer']
            active = [r for r in rows if r['state'] in ACTIVE]
            # An unresolved request is not a lock on the whole conversation.
            # A distinct explicit instruction gets its own delivery/reply identity.
            if any(r['agent'] == agent and r['question'] == question for r in active):
                raise MessagingError(409, 'This same message is still unresolved for that agent and may still run. Motet will not send it twice; no new message was sent.')
            if guard is None:
                self._check_owner_slots(len(rows), len(active), sum(r['created'] > now - 60 for r in rows))
                row = self._queue_row(agent, agents, request_id, question, name, now, active)
                self._save(row)
            else:
                row = self._queue_row(agent, agents, request_id, question, name, now, active)
                try:
                    with guard.admission(int(now * 1000)) as (counts, proof):
                        self._reject_attachment_reservation(request_id)
                        if self.db.execute('SELECT 1 FROM requests WHERE id=?', (request_id,)).fetchone():
                            raise MessagingError(409, 'That identifier is reserved for a different operation.')
                        self._check_owner_slots(*counts)
                        self._save(row, commit=False, ordinary_proof=proof)
                except OwnerCapacityUnavailable:
                    raise MessageNotSubmitted(503, 'Motet could not check the message queue. Nothing was sent.') from None
                except OwnerGuardError:
                    raise MessagingError(503, 'Owner attachment capacity is unavailable.') from None
                self._history_outcome(row)
            return self._public_request(row)

    @staticmethod
    def _check_owner_slots(history, pending, recent):
        if pending >= MAX_PENDING:
            raise MessagingError(429, 'Too many unresolved message deliveries are being tracked. Older messages may still run; no new message was sent.')
        if history >= MAX_RECORDS:
            raise MessagingError(429, 'Message history is at its storage limit. No new message was sent.')
        if recent >= 10:
            raise MessagingError(429, 'Too many messages were submitted in the last minute. No new message was sent.')

    def _queue_row(self, agent, agents, request_id, question, name, now, active):
        row = dict(request_id=request_id, agent=agent, name=name, question=question, state='sending', reply='', error='',
                   created=now, launched=False)
        if any(r['agent'] == agent for r in active):
            row['delivery_warning'] = 'When this message was submitted, older messages to this agent were unresolved and could still run. This request did not cancel them.'
        if agent.startswith('c:'):
            ready, enrollment, _ = self.mods.capability(agent, agents[agent])
            if not ready:
                raise MessagingError(503, CLAUDE_OWNER_UNAVAILABLE)
            row.update(runtime='claude', transport=claude_mod.TRANSPORT,
                       mod_enrollment=enrollment['id'], mod_project=enrollment['project'],
                       envelope=envelope(request_id, question))
        else:
            path, st = self._target_log(agent[2:])
            # Start only after a complete record; never bind a partial old line.
            with open(path, 'rb') as f:
                if st.st_size:
                    f.seek(st.st_size - 1)
                    if f.read(1) != b'\n':
                        raise MessagingError(409, 'The chat log is being written. Try again shortly.')
            row.update(runtime='codex', path=path, inode=st.st_ino, device=st.st_dev, cursor=st.st_size,
                       current_turn='', bound_turn='', final='', envelope=envelope(request_id, question))
        return row

    @staticmethod
    def _public_control(row):
        result = {k: row.get(k, '') for k in ('request_id', 'action', 'agent', 'name', 'instruction', 'state', 'error')}
        result['created'] = row['created'] if MessagingStore._timestamp(row.get('created')) else 0
        return result

    def _control_target(self, agent, agents, action='steer'):
        validate_target(agent, agents)
        if not agent.startswith('x:') or agents[agent].get('sub') or agents[agent].get('parent'):
            raise MessagingError(400, 'Live controls currently support local parent Codex desktop chats only.')
        path, identity = self._target_log(agent[2:])
        if action == 'steer' and time.time() - identity.st_mtime > 300:
            raise MessagingError(409, 'The chat log is stale; I cannot establish its current active turn.')
        with open(path, 'rb') as f:
            current = os.fstat(f.fileno())
            if (current.st_ino, current.st_dev) != (identity.st_ino, identity.st_dev):
                raise MessagingError(409, 'The chat transcript changed during control preparation.')
            meta = json.loads(f.readline(MAX_LINE + 1))['payload']
            if not stat.S_ISREG(current.st_mode) or not isinstance(meta, dict) or meta.get('id') != agent[2:]:
                raise MessagingError(409, 'The chat transcript identity changed during control preparation.')
            if meta.get('parent_thread_id') or meta.get('agent_path') not in (None, '', '/root'):
                raise MessagingError(400, 'Live controls support parent chats only.')
            offset = max(0, current.st_size - MAX_READ)
            f.seek(offset)
            if offset:
                f.readline(MAX_LINE + 1)
            raw = f.read(MAX_READ + 1)
        if not raw.endswith(b'\n') or len(raw) > MAX_READ:
            raise MessagingError(409, 'The current turn is not safely readable yet.')
        active, ended = set(), set()
        for line in raw.splitlines():
            if len(line) > MAX_LINE:
                raise MessagingError(409, 'The current turn record is too large to verify.')
            try:
                item = json.loads(line)
            except ValueError:
                raise MessagingError(409, 'The current turn log is malformed.') from None
            payload = item.get('payload') if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            kind, turn = payload.get('type'), payload.get('turn_id')
            if item.get('type') == 'event_msg' and kind in ('task_complete', 'turn_aborted', 'task_aborted'):
                if not valid_uuid(turn):
                    raise MessagingError(409, 'A terminal turn record is ambiguous; no control was sent.')
                else:
                    active.discard(turn); ended.add(turn)
            elif (item.get('type') == 'event_msg' and kind == 'task_started') or item.get('type') == 'turn_context':
                if not valid_uuid(turn):
                    raise MessagingError(409, 'The active turn identifier is missing or invalid.')
                if turn not in ended:
                    active.add(turn)
        after = self._metadata(path, agent[2:])
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns):
            raise MessagingError(409, 'The chat changed during control preparation; no control was sent.')
        if len(active) != 1:
            raise MessagingError(409, 'I cannot establish one active turn for that chat. No control was sent.')
        cwd = meta.get('cwd')
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or '\x00' in cwd:
            raise MessagingError(409, 'The chat working directory is not recorded safely.')
        return next(iter(active)), cwd, {'path': path, 'inode': after.st_ino, 'device': after.st_dev, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns}

    def submit_control(self, action, agent, instruction, request_id, agents, execute=None):
        if action not in ('steer', 'interrupt') or not valid_uuid(request_id) or not isinstance(agent, str):
            raise MessagingError(400, 'Invalid control request.')
        if action == 'steer':
            validate_question(instruction)
        elif instruction != '':
            raise MessagingError(400, 'An interrupt cannot include another instruction.')
        with self.lock:
            self._reject_attachment_reservation(request_id)
            self._reject_expired_control(request_id)
            if self.managed is not None:
                try:
                    if self.managed.lookup_message(request_id) or self.managed.owns_agent(agent):
                        raise MessagingError(409, 'Live controls are unavailable for managed Claude sessions.')
                except MessagingError:
                    raise
                except Exception:
                    raise MessagingError(503, 'Managed Claude state is unavailable; recovery is required.') from None
            existing = self.db.execute('SELECT data FROM controls WHERE id=?', (request_id,)).fetchone()
            if existing:
                old = json.loads(existing[0])
                if (old['action'], old['agent'], old['instruction']) != (action, agent, instruction):
                    raise MessagingError(409, 'That request identifier belongs to a different control.')
                return self._public_control(old)
            if self.db.execute('SELECT 1 FROM requests WHERE id=?', (request_id,)).fetchone():
                raise MessagingError(409, 'That request identifier already belongs to a queued message.')
            if sys.platform != 'darwin':
                # Live controls drive the Codex desktop app's local bridge, which only exists on a Mac. Refuse
                # before reading any transcript or saving a control, so nothing is left 'sending'.
                raise MessagingError(409, CODEX_CONTROLS_NEED_MAC)
            now = time.time()
            rows = self._controls()
            retained = [r for r in rows if now - r['created'] <= RETENTION or r['state'] == 'sending']
            if any(r['agent'] == agent and r['state'] == 'sending' for r in retained):
                raise MessagingError(409, 'A control for that chat is still being dispatched. Wait for its result.')
            if len(retained) >= MAX_RECORDS or sum(r['created'] > now - 60 for r in retained) >= 10:
                raise MessagingError(429, 'Too many live controls; try again later.')
            with self.db:
                for old in rows:
                    if old not in retained:
                        self.db.execute('INSERT OR IGNORE INTO expired_controls VALUES (?)', (old['request_id'],))
                        self.db.execute(f'DELETE FROM asks WHERE id=? AND {self._unreserved("asks.id")}', (old['request_id'],))
                        self.db.execute('DELETE FROM controls WHERE id=?', (old['request_id'],))
            try:
                turn, cwd, transcript = self._control_target(agent, agents, action)
            except (MessagingError, OSError, ValueError, KeyError, TypeError) as error:
                # Freeze preflight failure too: replay must not resolve a newer turn.
                row = dict(request_id=request_id, action=action, agent=agent, instruction=instruction,
                           name=str(agents.get(agent, {}).get('name') or agent)[:120], state='failed',
                           error=str(error) if isinstance(error, MessagingError) else 'The active turn could not be verified. No control was sent.', created=now)
                with self.db:
                    self.db.execute('INSERT INTO controls VALUES (?,?)', (request_id, json.dumps(row)))
                self._history_outcome(row, 'control')
                return self._public_control(row)
            row = dict(request_id=request_id, action=action, agent=agent, instruction=instruction,
                       name=str(agents[agent].get('name') or agent)[:120], expected_turn_id=turn, cwd=cwd, transcript=transcript,
                       state='sending', error='', created=now)
            with self.db:
                self.db.execute('INSERT INTO controls VALUES (?,?)', (request_id, json.dumps(row)))
            self._history_outcome(row, 'control')
        # The bridge may block; no room or store lock spans its I/O. Once reserved,
        # every exception is uncertain and must never trigger another dispatch.
        try:
            check = self._metadata(transcript['path'], agent[2:])
            unchanged = (check.st_ino, check.st_dev, check.st_size, check.st_mtime_ns) == (transcript['inode'], transcript['device'], transcript['size'], transcript['mtime_ns'])
        except (OSError, MessagingError):
            unchanged = False
        try:
            if not unchanged:
                outcome = {'state': 'failed', 'error': 'The chat transcript changed before dispatch. No control was sent.'}
            else:
                if execute is None:
                    from codex_desktop_control import execute
                outcome = execute(action, agent[2:], instruction=instruction, expected_turn_id=turn, cwd=cwd)
            state = outcome.get('state') if isinstance(outcome, dict) else None
            allowed = ('acknowledged', 'failed', 'unknown') if action == 'steer' else ('interrupted', 'no_op', 'failed', 'unknown')
            if state not in allowed:
                row.update(state='unknown', error='The desktop control result could not be confirmed. It will not be retried.')
            else:
                row.update(state=state, error=str(outcome.get('error') or '')[:500])
        except Exception:
            row.update(state='unknown', error='Desktop control status is unknown. It will not be retried.')
        with self.lock:
            self._reject_attachment_reservation(request_id)
            with self.db:
                row['completed_at'] = time.time()
                self.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(row), request_id))
                if row['action'] == 'interrupt' and row['state'] == 'interrupted':
                    self._reconcile_fulfillments()
        self._history_outcome(row, 'control')
        return self._public_control(row)

    def _public_request(self, row):
        result = public(row)
        evidence = self.db.execute('SELECT control_request_id FROM request_fulfillments WHERE request_id=?',
                                   (row['request_id'],)).fetchone()
        if evidence and not self._attachment_reserved(evidence[0]):
            result['fulfillment'] = {'state': 'fulfilled', 'intent': 'pause', 'via': 'interrupt',
                                     'control_request_id': evidence[0]}
        return result

    @staticmethod
    def _timestamp(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _pure_pause(text, row, owner=False):
        """A deliberately narrow grammar, never a general intent classifier.

        Both authenticated owner text and its exact frozen relay payload must
        qualify. In particular a queued/deferred owner request cannot inherit
        immediate intent from a shortened payload.
        """
        if not isinstance(text, str):
            return False
        text = text.strip()
        effect = r'(?:pause(?: (?:your|its|the)(?: current)? work)?|stop (?:working|(?:your|its|the)(?: current)? work)|halt(?: (?:your|its|the)(?: current)? work)?|interrupt(?: (?:your|its|the)(?: current)? work)?)'
        ending = r'(?: now)?[.!]?'
        if not owner:
            return re.fullmatch(effect + ending, text, re.IGNORECASE) is not None
        targets = [row.get('name'), row.get('agent'), str(row.get('agent', ''))[2:]]
        for target in targets:
            if not isinstance(target, str) or not target.strip():
                continue
            target = re.escape(target)
            patterns = [r'(?:tell|ask) ' + target + r' to ' + effect,
                        r'(?:pause|stop|halt|interrupt) ' + target + r"(?:'s)?(?: (?:current )?work)?"]
            if any(re.fullmatch(pattern + ending, text, re.IGNORECASE) for pattern in patterns):
                return True
        return False

    def _legacy_control_completion(self, control):
        """Recover only a timestamped abort of the exact pinned turn, locally.

        Read at most MAX_READ bytes after the saved complete-record boundary.
        A witness found within that budget suffices even if the log grew larger.
        Transcript content supplies evidence only, never instructions.
        """
        transcript = control.get('transcript')
        if not isinstance(transcript, dict):
            return None
        try:
            path, offset = transcript['path'], transcript['size']
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                return None
            st = self._metadata(path, control['agent'][2:])
            identity = (transcript['inode'], transcript['device'])
            if (st.st_ino, st.st_dev) != identity or st.st_size < offset:
                return None
            with open(path, 'rb') as f:
                current = os.fstat(f.fileno())
                if (current.st_ino, current.st_dev) != identity or current.st_size < offset:
                    return None
                if offset:
                    f.seek(offset - 1)
                    if f.read(1) != b'\n':
                        return None
                f.seek(offset)
                raw = f.read(MAX_READ)
            for line in raw.split(b'\n')[:-1]:
                if len(line) > MAX_LINE:
                    return None
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload')
                if (event.get('type') != 'event_msg' or not isinstance(payload, dict)
                        or payload.get('type') != 'turn_aborted' or payload.get('turn_id') != control['expected_turn_id']):
                    continue
                stamp = event.get('timestamp')
                if not isinstance(stamp, str):
                    return None
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
                ended = parsed.timestamp()
                return ended if self._timestamp(ended) and ended >= control['created'] else None
        except (MessagingError, OSError, KeyError, TypeError, ValueError, OverflowError):
            return None
        return None

    def _reconcile_fulfillments(self):
        """Annotate historical pause satisfaction, without changing delivery.

        Caller owns the store lock (or startup ownership) and transaction. The
        separate table cannot be overwritten by tick's in-flight request copy.
        Unknown routing anywhere inside the authority interval fails closed.
        """
        requests = self._rows()
        controls = self._controls()
        asks = {rid: (text, created, decision) for rid, text, created, decision in self.db.execute(
            f'SELECT id, user_text, created, decision FROM asks WHERE {self._unreserved("asks.id")}')}
        # Includes protected collisions as occupied keys only. Their evidence
        # cannot be projected or displaced by a new fulfillment.
        occupied = {rid for (rid,) in self.db.execute('SELECT request_id FROM request_fulfillments')}
        completions = {}
        for control in controls:
            if (control.get('action') != 'interrupt' or control.get('state') != 'interrupted'
                    or not valid_uuid(control.get('request_id')) or not valid_uuid(control.get('expected_turn_id'))
                    or not self._timestamp(control.get('created'))):
                continue
            ended = control.get('completed_at')
            if ended is None:
                ended = self._legacy_control_completion(control)
            if self._timestamp(ended) and ended >= control['created']:
                completions[control['request_id']] = ended
        for row in requests:
            rid, agent = row['request_id'], row.get('agent')
            source = asks.get(rid)
            if (row.get('origin') == 'peer' or rid in occupied or not valid_uuid(rid) or not source or not isinstance(agent, str) or not agent.startswith('x:')
                    or not valid_uuid(agent[2:]) or not self._timestamp(row.get('created'))
                    or not self._timestamp(source[1]) or source[1] > row['created']):
                continue
            try:
                decision = json.loads(source[2])
            except (TypeError, ValueError):
                continue
            if (not isinstance(decision, dict) or decision.get('kind') != 'relay'
                    or decision.get('agent') != agent or decision.get('question') != row.get('question')
                    or not self._pure_pause(source[0], row, owner=True) or not self._pure_pause(row.get('question'), row)):
                continue
            for control in sorted(controls, key=lambda c: completions.get(c.get('request_id'), float('inf'))):
                cid = control.get('request_id')
                ended = completions.get(cid)
                if (ended is None or control.get('agent') != agent or control['created'] <= row['created']
                        or control['created'] <= source[1]
                        or (row.get('bound_turn') and row['bound_turn'] != control['expected_turn_id'])):
                    continue
                transcript = control.get('transcript', {})
                if (not isinstance(transcript, dict) or row.get('path') != transcript.get('path')
                        or row.get('inode') != transcript.get('inode') or row.get('device') != transcript.get('device')):
                    continue
                blocked = False
                for other_id, (_, created, raw_decision) in asks.items():
                    if other_id in (rid, cid):
                        continue
                    if not self._timestamp(created):
                        blocked = True; break
                    if not source[1] <= created <= ended:
                        continue
                    try:
                        other = json.loads(raw_decision)
                    except (TypeError, ValueError):
                        blocked = True; break
                    if not isinstance(other, dict) or other.get('kind') not in ('relay', 'control', 'none', 'clarify'):
                        blocked = True; break
                    if other['kind'] in ('relay', 'control'):
                        target = other.get('agent')
                        if (not isinstance(target, str) or target[:2] not in ('x:', 'c:')
                                or not valid_uuid(target[2:]) or target == agent):
                            blocked = True; break
                        try:
                            if other['kind'] == 'relay':
                                validate_question(other.get('question'))
                            elif other.get('action') == 'steer':
                                validate_question(other.get('instruction'))
                            elif other.get('action') != 'interrupt' or other.get('instruction') != '':
                                blocked = True; break
                        except MessagingError:
                            blocked = True; break
                    elif other['kind'] == 'clarify' and (not isinstance(other.get('text'), str)
                                                         or not other['text'].strip()):
                        blocked = True; break
                if blocked:
                    continue
                # Launched peer input remains interference even if its payload
                # was malformed and isolated by _rows(). Reservation may predate
                # the authority interval, so compare durable launch time instead.
                peer_launches = peer_bridge.transport_metadata(self.db, agent_id=agent, launched_since=source[1])
                if any(not self._timestamp(meta['launched_at']) or source[1] <= meta['launched_at'] <= ended
                       for meta in peer_launches.values()):
                    continue
                # Direct message/control endpoints may have no asks provenance.
                if any(other.get('request_id') not in (rid, cid) and other.get('agent') == agent
                       and (not self._timestamp(other.get('created')) or source[1] <= other['created'] <= ended)
                       for other in requests + controls):
                    continue
                self.db.execute('INSERT OR IGNORE INTO request_fulfillments VALUES (?,?,?)', (rid, cid, ended))
                break

    def _prune(self, rows, now, *, ordinary_only=False):
        runtime = self._native_owner_runtime
        if runtime is not None and not self.db.in_transaction:
            with runtime.ledger.ordinary_writer() as proof:
                return self._prune_rows(rows, now, proof, ordinary_only=ordinary_only)
        try:
            if self._attachment_guard is not None and not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            retained = self._prune_rows(rows, now, None, ordinary_only=ordinary_only)
            self.db.commit()
            return retained
        except BaseException:
            self.db.rollback()
            raise

    def _prune_rows(self, rows, now, proof, *, ordinary_only=False):
        rows = [row for row in rows if not self._attachment_reserved(row['request_id'])]
        if ordinary_only:
            # Peer reconciliation can invalidate the directory that gates new
            # sends. Leave peer maintenance to the messaging worker.
            rows = [row for row in rows if self._ordinary_row(row)]
        for row in rows:
            if not ordinary_only:
                if proof is not None and not self._ordinary_row(row):
                    proof.eligible = False
                peer_lifecycle.reconcile(self.db, row)
        newly_pruned = set()
        for row in rows:
            if now - row['created'] > RETENTION:
                fulfillment = self._unreserved('request_fulfillments.request_id')
                control = self._unreserved('request_fulfillments.control_request_id')
                deleted = self.db.execute(f'DELETE FROM request_fulfillments WHERE request_id=? AND {fulfillment} AND {control}',
                                          (row['request_id'],))
                request = self._unreserved('requests.id')
                removed = self.db.execute(f'DELETE FROM requests WHERE id=? AND {request}', (row['request_id'],))
                if removed.rowcount and row.get('origin') == 'peer':
                    meta = peer_lifecycle.exact_admission(self.db, row['request_id'])
                    retained = peer_lifecycle.get(self.db, row['request_id'])
                    if (meta and retained and row.get('runtime') == 'codex' and retained['execution'] == 'running'
                            and peer_lifecycle.valid_payload(row['request_id'], row, meta)):
                        newly_pruned.add(row['request_id'])
                    if (meta and meta[2] is None and retained and retained['execution'] == 'reserved'
                            and not any(retained['evidence'].get(key) for key in ('launched', 'bound_turn', 'mod_turn'))
                            and self.db.execute('SELECT 1 FROM requests WHERE id=?', (row['request_id'],)).fetchone() is None):
                        # Retention may run before the ordinary dispatch tick. An
                        # immutable never-launched reservation cannot orphan a slot.
                        peer_lifecycle.reconcile(self.db, dict(request_id=row['request_id'], agent=meta[0]),
                                                 meta, missing_payload=True)
                if proof is not None:
                    proof.record(deleted)
                    proof.record(removed)
        if ordinary_only:
            return [row for row in rows if now - row['created'] <= RETENTION]
        shared = self._unreserved('shared_bridge_requests.transport_id')
        shared_removed = self.db.execute(f'''DELETE FROM requests WHERE id IN
            (SELECT transport_id FROM shared_bridge_requests WHERE created < ? AND {shared})''', (now - RETENTION,))
        preview_removed = self.db.execute(f"UPDATE shared_bridge_requests SET preview='' WHERE created < ? AND preview<>'' AND {shared}",
                                          (now - RETENTION,))
        if proof is not None and (shared_removed.rowcount != 0 or preview_removed.rowcount != 0):
            proof.eligible = False
        self._hold_pruned_codex(now, proof, newly_pruned)
        return [row for row in rows if now - row['created'] <= RETENTION]

    def _hold_pruned_codex(self, now, proof, newly_pruned):
        clear = self._unreserved('e.request_id')
        records = self.db.execute(f'''SELECT e.request_id,e.agent,e.created,e.evidence
            FROM peer_execution AS e INDEXED BY peer_execution_active_created
            WHERE e.execution='running' AND e.capture<>'captured' AND e.agent GLOB 'x:*' AND e.created < ?
                AND e.execution NOT IN ('completed','dropped','retired')
                AND NOT EXISTS (SELECT 1 FROM requests WHERE id=e.request_id) AND {clear}''', (now - RETENTION,)).fetchall()
        for ident, agent, created, raw in records:
            meta = peer_lifecycle.exact_admission(self.db, ident)
            if (not meta or meta[:2] != (agent, created) or not self._timestamp(meta[2])
                    or meta[2] < created or not valid_uuid(ident) or not valid_uuid(agent[2:])):
                continue
            try:
                evidence = json.loads(raw)
                turn = evidence.get('bound_turn') if isinstance(evidence, dict) else None
                if (not isinstance(turn, str) or not 0 < len(turn) <= 128 or any(ord(ch) < 32 for ch in turn)
                        or evidence.get('launched') is not True):
                    continue
                turn.encode('utf-8')
            except (ValueError, TypeError, RecursionError):
                continue
            evidence['codex_retention_hold'] = peer_lifecycle.CODEX_RETENTION_HOLD
            # An already absent or unreadable body does not establish when tracking stopped.
            if ident in newly_pruned:
                peer_lifecycle.begin_hold_episode(evidence, created=created, launched=meta[2], now=now)
            self.db.execute("UPDATE peer_execution SET execution='uncertain',capture='withheld',evidence=? WHERE request_id=?",
                            (json.dumps(evidence, sort_keys=True), ident))
            if proof is not None:
                proof.eligible = False

    def _send(self, row, *, observations=None):
        if row.get('transport') == claude_mod.TRANSPORT:
            return 'unknown', 'Official-mod requests are dispatched only by their atomic claim.'
        if row.get('runtime') == 'claude':
            return self._send_claude(row)
        return self._queue_codex(row, outgoing_message(row), observations=observations)

    def _queue_codex(self, row, message, *, observations=None):
        try:
            st = self._metadata(row['path'], row['agent'][2:])
            if (st.st_ino, st.st_dev) != (row['inode'], row['device']) or st.st_size < row['cursor']:
                raise MessagingError(400, 'The transcript changed before sending.')
        except (OSError, MessagingError):
            row['execution_outcome'] = 'dropped'
            return 'failed', 'The target transcript changed before sending; nothing was sent.'
        try:
            proc = subprocess.Popen([self.cli, 'queue', '--thread', row['agent'][2:], '--message', message],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            row['execution_outcome'] = 'dropped'
            return 'failed', 'Codex could not be launched; nothing was sent.'
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
            return 'unknown', 'Sending timed out. Delivery is unknown; Motet will not resend.'
        except (OSError, subprocess.SubprocessError):
            return 'unknown', 'Sending could not be confirmed after launch. Motet will not resend.'
        if code:
            return 'unknown', 'Codex returned an error after launch. Delivery is unknown; Motet will not resend.'
        if observations is not None and type(code) is int and code == 0:
            observations.append('accepted')
        return 'queued', ''

    def _apply_claude(self, row, record):
        if record['state'] == 'answered':
            row.update(state='answered', reply=record.get('reply', ''), error='')
            if row.get('origin') != 'peer' and record.get('peer_input_seen') is True:
                row['delivery_warning'] = (row.get('delivery_warning', '') + ' ' + peer_bridge.PEER_WARNING).strip()
        elif record['state'] == 'unknown':
            row.update(state='unknown', error='Claude could not confirm delivery. Motet will not resend.')
        elif row['state'] not in ('expired', 'unknown'):
            row.update(state='queued', error='')

    def _send_claude(self, row):
        if row.get('origin') == 'peer':
            return 'failed', claude_channel.PEER_IDENTITY_REASON + ' Nothing was sent.'
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
        except claude_channel.ChannelError:
            return 'failed', 'The Claude channel disappeared or changed before sending; nothing was sent. ' + claude_channel.START_HINT
        try:
            record = (claude_channel.exchange(descriptor, row['request_id'], row['envelope'], origin='peer')
                      if row.get('origin') == 'peer' else claude_channel.exchange(descriptor, row['request_id'], row['envelope']))
            self._apply_claude(row, record)
            return row['state'], row['error']
        except claude_channel.ChannelError:
            return 'unknown', 'Claude delivery could not be confirmed after sending. Motet will not resend.'

    def _poll_claude(self, row):
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
            record = (claude_channel.exchange(descriptor, row['request_id'], origin='peer')
                      if row.get('origin') == 'peer' else claude_channel.exchange(descriptor, row['request_id']))
            self._apply_claude(row, record)
        except claude_channel.ChannelError:
            row.update(state='unknown', error='The original Claude channel or its reply is unavailable. Motet will not redirect or resend.')
        if row['state'] in ('queued', 'awaiting_reply') and time.time() - row['created'] > TTL:
            row.update(state='expired', error='No matching reply yet. Motet will keep checking without resending.')

    def _previous_codex_envelope(self, row, path, st, project):
        """Authenticate the retained envelope and original pin against admission."""
        import peer_recovery

        if not isinstance(project, str) or not os.path.isabs(project):
            raise ValueError()
        project = os.path.realpath(project)
        tid = row['request_id']
        with self.lock:
            meta = peer_lifecycle.exact_admission(self.db, tid)
            record = peer_lifecycle.get(self.db, tid)
            if (not valid_uuid(tid) or not meta or not record or not self._timestamp(meta[2])
                    or not peer_lifecycle.valid_payload(tid, row, meta) or meta[2] < meta[1]
                    or (record['agent'], record['created']) != meta[:2]
                    or record['evidence'].get('bound_turn') != row['bound_turn']):
                raise ValueError()
            envelope = row['envelope']
            if meta[3] == 2:
                client_id, = self.db.execute('SELECT client_id FROM shared_bridge_requests WHERE transport_id=?', (tid,)).fetchone()
                key, = self.db.execute('SELECT ledger_key FROM shared_bridge_service WHERE singleton=1').fetchone()
                ledger = peer_recovery.load_ledger(self.db, client_id, allow_retained=True)
                if not peer_recovery.original_identity(ledger, path, st, project):
                    raise ValueError()
                return peer_recovery.envelope_proof(ledger, key, envelope)
            sender_id, target_id, digest = self.db.execute(
                'SELECT sender,target,payload_hash FROM peer_requests WHERE transport_id=?', (tid,)).fetchone()
            sender = peer_bridge.principal(self.db, sender_id, active=False)
            target = peer_bridge.principal(self.db, target_id, active=False)
            question = row['question']
            expected = peer_bridge.mac(sender['secret'], 'watch-peer-dedupe-v1', json.dumps([target_id, question], ensure_ascii=True))
            if (target['identity'] != dict(path=path, device=st.st_dev, inode=st.st_ino)
                    or target['project'] != project
                    or not hmac.compare_digest(digest, expected)
                    or envelope != peer_bridge.envelope(tid, sender['label'], question, 'codex')):
                raise ValueError()
            return envelope

    def _previous_codex_terminal(self, row):
        """Return an abort verdict for the consumed suffix, or None if incomplete."""
        import peer_recovery

        cursor, bound = row['cursor'], row['bound_turn']
        if type(cursor) is not int or cursor <= 0:
            raise ValueError()
        if row.get('codex_tracking'):
            # The live tracker validates the saved successor pin. A bounded
            # read in the original rollout cannot prove the consumed interval.
            return None
        try:
            path, source, st, header = codex_transcript.open_pinned(self.sessions_dir, row['path'], row['agent'][2:])
        except codex_transcript.TemporaryUnavailable:
            raise
        except codex_transcript.Unavailable as error:
            # The native pointer may already name a single-hop successor the
            # previous worker has not followed. Require the same bounded chain
            # proof as live tracking before checking the original consumed tail.
            probe = copy.deepcopy(row)
            continuation = codex_transcript.tracking_source(self.sessions_dir, probe, MAX_READ)
            if probe.get('codex_continuation'):
                row['codex_continuation'] = probe['codex_continuation']
            if continuation is None:
                raise BlockingIOError('Codex continuation proof is still being read.')
            continuation[0].close()
            if not probe.get('codex_continuation'):
                raise error
            # Only prefix progress is retained here. The normal live tracker
            # switches segments after the original tail proof also succeeds.
            path = row['path']
            source, st = codex_transcript.open_regular(self.sessions_dir, path)
            try:
                header = codex_transcript.header(source, row['agent'][2:])
            except BaseException:
                source.close()
                raise
        with source:
            if (st.st_dev, st.st_ino) != (row['device'], row['inode']) or st.st_size < cursor:
                raise ValueError()
            envelope = self._previous_codex_envelope(row, path, st, header.get('cwd'))
            if row.get('codex_discard_record'):
                if tuple(row['codex_discard_record']) != codex_transcript.segment_key(row):
                    raise ValueError()
                return None
            start = max(0, cursor - MAX_READ)
            source.seek(start)
            raw = source.read(cursor - start)
            expected = peer_recovery.fingerprint(st)
            current = peer_recovery.fingerprint(os.stat(path, follow_symlinks=False))
            if current[:2] != expected[:2]:
                raise ValueError()
            if current != expected or peer_recovery.fingerprint(os.fstat(source.fileno())) != expected:
                # A live write to the same pinned file calls for a fresh proof.
                raise BlockingIOError()
            if len(raw) != cursor - start or not raw.endswith(b'\n'):
                raise ValueError()

        # The server-generated transport ID was minted after the admission
        # cursor. An authenticated exact envelope anchors this suffix even
        # though older rows no longer retain the initial cursor.
        seen, aborted, incomplete, current_turn = False, False, False, None
        # The first line of a clipped window may start inside a record.
        for line in raw.split(b'\n')[1 if start else 0:-1]:
            try:
                event = peer_recovery.strict_json(line) if len(line) <= MAX_LINE else None
            except (ValueError, RecursionError):
                event = None
            if not isinstance(event, dict):
                incomplete, current_turn = True, None
                continue
            typ = event.get('type')
            if typ in ('session_meta', 'world_state', 'token_usage_record', 'inter_agent_communication_metadata'):
                continue
            if typ not in ('event_msg', 'response_item', 'turn_context'):
                incomplete |= seen
                current_turn = None
                continue
            payload = event.get('payload')
            if not isinstance(payload, dict):
                incomplete, current_turn = True, None
                continue
            kind = payload.get('type')
            context = typ == 'turn_context' or typ == 'event_msg' and kind == 'task_started'
            user_message = typ == 'response_item' and kind == 'message' and payload.get('role') == 'user'
            terminal = typ == 'event_msg' and kind in ('turn_aborted', 'task_aborted', 'task_complete')
            if user_message:
                text = codex_transcript.pure_text(payload.get('content'), 'user')
                if text not in (envelope, envelope + '\n'):
                    incomplete |= text is None and seen
                    continue
                if seen:
                    raise ValueError()
                seen = True
            elif not context and not terminal:
                continue
            try:
                turn = peer_recovery.codex_turn(payload)
            except ValueError:
                incomplete, current_turn = True, None
                continue
            if context:
                current_turn = turn
                incomplete |= seen and typ == 'event_msg' and current_turn == bound
            elif user_message:
                if turn and turn != bound:
                    raise ValueError()
                incomplete |= (turn or current_turn) != bound
            elif not turn or payload.get('turn_id') != turn:
                incomplete = True
            elif turn == bound:
                if kind == 'task_complete':
                    raise ValueError()
                incomplete |= not seen or aborted
                aborted = True
        return None if incomplete or not seen else aborted

    def _check_previous_codex_terminal(self, row):
        try:
            aborted = self._previous_codex_terminal(row)
        except (peer_bridge.PeerError, OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            if isinstance(error, (OSError, codex_transcript.TemporaryUnavailable)):
                if not row.get('error'):
                    row['error'] = CODEX_PREVIOUS_SOURCE_UNAVAILABLE
                return False
            row.update(state='unknown', capture_disposition='withheld', tracking_stopped=True,
                       reply='', candidate_reply='', final='', final_turn='',
                       error='Earlier Codex transcript records could not be verified. Reply withheld and recipient recovery is required.')
        else:
            if aborted is True:
                self._withhold_codex_peer_end(row, interrupted=True)
            elif aborted is None:
                # A historical gap can hide an already-consumed abort. Wait
                # for a new exact terminal and keep any reply text private.
                row.update(codex_gap_seen=True, capture_disposition='withheld',
                           reply='', candidate_reply='', final='', final_turn='',
                           error='Earlier Codex transcript records are incomplete. Motet will keep checking for the original turn to end.')
            elif row.get('error') == CODEX_PREVIOUS_SOURCE_UNAVAILABLE:
                row['error'] = ''
        return not row.get('tracking_stopped')

    @staticmethod
    def _withhold_codex_peer_end(row, *, interrupted=False):
        gap_seen = row.get('codex_gap_seen') is True or row.get('codex_gap_needs_envelope') is True
        if interrupted:
            row['codex_abort_seen'] = True
        row.update(settled=True, state='unknown', capture_disposition='withheld', tracking_stopped=True,
                   reply='', candidate_reply='', final='', final_turn='',
                   error=peer_lifecycle.withheld_message(gap=gap_seen, interrupted=interrupted))

    def _consume(self, row, data):
        p = data.get('payload')
        if not isinstance(p, dict):
            if row.get('origin') == 'peer' and data.get('type') in ('event_msg', 'response_item', 'turn_context'):
                self._codex_gap(row)
            return
        typ = data.get('type')
        kind = p.get('type')
        gap_seen = row.get('codex_gap_seen') is True or row.get('codex_gap_needs_envelope') is True
        if (row.get('origin') == 'peer' and typ == 'event_msg'
                and kind in ('task_complete', 'turn_aborted', 'task_aborted')
                and (gap_seen or kind != 'task_complete')
                and isinstance(row.get('bound_turn'), str) and row['bound_turn']
                and p.get('turn_id') == row['bound_turn']):
            # The exact immutable turn ended. A gap or abort leaves no safe
            # reply to capture, even if this event includes answer text.
            self._withhold_codex_peer_end(row, interrupted=kind != 'task_complete')
            return 'ended'
        if typ == 'event_msg' and kind == 'task_started':
            row['current_turn'] = p.get('turn_id') if isinstance(p.get('turn_id'), str) else ''
            if row.get('codex_gap_needs_envelope'):
                row['codex_post_gap_start'] = row['current_turn']
            if row.get('origin') == 'peer' and row['current_turn']:
                starts = row.setdefault('peer_turn_starts', [])
                if row['current_turn'] not in starts:
                    if len(starts) >= 128:
                        row['peer_context_overflow'] = True
                    starts[:] = starts[-127:] + [row['current_turn']]
        elif typ == 'turn_context' and isinstance(p.get('turn_id'), str):
            row['current_turn'] = p['turn_id']
        elif typ == 'response_item' and kind == 'message':
            content = p.get('content', [])
            if row.get('origin') == 'peer':
                text = codex_transcript.pure_text(content, p.get('role'))
                if text is None:
                    self._codex_gap(row)
                    return
            else:
                text = ''.join(c.get('text', '') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str)) if isinstance(content, list) else ''
            outgoing = outgoing_message(row)
            # The native Codex host may append exactly one record-ending LF.
            # Do not trim whitespace or change the stored message/dedupe digest.
            matches_envelope = text == outgoing or (row.get('origin') == 'peer' and text == outgoing + '\n')
            # A delayed response_item can belong to an older turn even while a
            # newer task is current. Explicit message metadata is authoritative.
            nested = p.get('internal_chat_message_metadata_passthrough')
            if nested is not None and not isinstance(nested, dict):
                if row.get('origin') == 'peer':
                    self._codex_gap(row)
                return
            explicit = []
            for source in (p, nested or {}):
                if 'turn_id' in source:
                    value = source['turn_id']
                    if not isinstance(value, str) or not value or len(value) > 128:
                        if row.get('origin') == 'peer':
                            self._codex_gap(row)
                        return
                    explicit.append(value)
            if len(set(explicit)) > 1:
                if row.get('origin') == 'peer':
                    self._codex_gap(row)
                return
            turn = explicit[0] if explicit else row['current_turn']
            if row.get('origin') == 'peer' and p.get('role') == 'user' and not matches_envelope:
                if turn and turn == row.get('bound_turn'):
                    row['peer_mixed_input'] = True
                foreign = row.setdefault('peer_foreign_turns', [])
                if not turn:
                    row['peer_mixed_input'] = True
                elif turn not in foreign:
                    if len(foreign) >= 128:
                        row['peer_context_overflow'] = True
                    foreign[:] = foreign[-127:] + [turn]
            if p.get('role') == 'user' and matches_envelope and turn:
                if row.get('origin') == 'peer' and row.get('bound_turn') and row['bound_turn'] != turn:
                    # A second native turn is not the original dispatch receipt.
                    # Preserve its immutable binding instead of crashing durable
                    # reconciliation or publishing a later turn's reply.
                    row.update(state='unknown', peer_mixed_input=True, capture_disposition='withheld',
                               tracking_stopped=True, error='The peer envelope appeared in another native turn; recovery is needed.')
                    return
                # An unreadable record can hide a turn boundary. Only an explicit
                # post-gap envelope binding (or a fresh task_started) restores
                # execution evidence. Capture remains permanently withheld.
                if row.get('codex_gap_needs_envelope'):
                    if not explicit and row.get('codex_post_gap_start') != turn:
                        return
                    row['codex_gap_seen'] = True  # Also upgrade held rows from earlier versions.
                    row['codex_gap_needs_envelope'] = False
                if row.get('origin') == 'peer':
                    if (turn not in row.get('peer_turn_starts', []) or turn in row.get('peer_foreign_turns', [])
                            or row.get('peer_context_overflow') or row.get('bound_turn') == turn):
                        row['peer_mixed_input'] = True
                new_binding = row['bound_turn'] != turn
                if new_binding:
                    row['final'], row['final_turn'] = '', ''
                row['bound_turn'] = turn
                row['state'] = 'awaiting_reply'
                if new_binding and row.get('origin') == 'peer':
                    return 'started'
            elif p.get('role') == 'assistant' and p.get('phase') == 'final_answer' and turn and turn == row['bound_turn']:
                row['final'], row['final_turn'] = text[:16000], turn
        elif typ == 'event_msg' and kind == 'task_complete' and p.get('turn_id') and p['turn_id'] == row['bound_turn']:
            if row.get('codex_gap_needs_envelope'):
                return
            answer = p.get('last_agent_message') or (row['final'] if row.get('final_turn') == row['bound_turn'] else '')
            marker = 'WATCH_REPLY ' + row['request_id']
            if row.get('origin') == 'peer':
                row['settled'] = True
                markers = [line for line in answer.splitlines() if 'WATCH_REPLY' in line] if isinstance(answer, str) else []
                if markers != [marker]:
                    row.update(state='unknown', reply='', tracking_stopped=True, error='Peer reply has missing or ambiguous correlation markers.')
                    return 'ended'
                row.update(state='quarantined', complete_offset=codex_transcript.segment(row)['cursor'],
                           complete_segment=list(codex_transcript.segment_key(row)), quarantined_at=time.time(),
                           candidate_reply='\n'.join(line for line in answer.splitlines() if line != marker).strip()[:16000], error='')
                return 'ended'
            if isinstance(answer, str) and marker in answer.splitlines():
                row.update(state='answered', reply='\n'.join(line for line in answer.splitlines() if line != marker).strip()[:16000], error='')

    @staticmethod
    def _codex_gap(row):
        row.update(peer_mixed_input=True, capture_disposition='withheld', codex_gap_seen=True,
                   codex_gap_needs_envelope=True, codex_post_gap_start='', current_turn='', state='unknown',
                   error='An unreadable transcript record was skipped; reply content will be withheld.')

    def _poll(self, row):
        if row.get('transport') == claude_mod.TRANSPORT:
            return
        if row.get('runtime') == 'claude':
            return self._poll_claude(row)
        peer = row.get('origin') == 'peer' and row.get('runtime') == 'codex'
        observations = []
        if peer and row.get('bound_turn') and not row.get('settled'):
            current = codex_transcript.segment(row)
            checkpoint = [current.get(key) for key in ('device', 'inode', 'cursor')]
            if row.get('codex_peer_terminal_checked') != checkpoint and not self._check_previous_codex_terminal(row):
                return
        try:
            opened = codex_transcript.tracking_source(self.sessions_dir, row, MAX_READ)
            if opened is None:
                return
            f, st, current = opened
            with f:
                f.seek(current['cursor'])
                data = f.read(MAX_READ)
            if row.get('error') == 'The transcript is unavailable; Motet will keep checking for a reply.':
                row['error'] = ''
            if row.get('codex_discard_record'):
                if tuple(row['codex_discard_record']) != codex_transcript.segment_key(row):
                    raise codex_transcript.Unavailable('An incomplete oversized record belongs to another Codex segment.')
                end = data.find(b'\n')
                consumed = len(data) if end < 0 else end + 1
                current['cursor'] += consumed
                if end < 0:
                    return
                row.pop('codex_discard_record', None)
                data = data[consumed:]
            lines = data.split(b'\n')
            stopped = False
            for raw in lines[:-1]:
                current['cursor'] += len(raw) + 1
                if len(raw) > MAX_LINE:
                    if row.get('origin') == 'peer':
                        self._codex_gap(row)
                    continue
                try:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        kind = self._consume(row, event)
                        if kind:
                            observations.append(kind)
                    elif row.get('origin') == 'peer':
                        self._codex_gap(row)
                except (ValueError, TypeError):
                    if row.get('origin') == 'peer':
                        self._codex_gap(row)
                if row['state'] in ('answered', 'quarantined') or row.get('tracking_stopped'):
                    stopped = True
                    break
            if not stopped and len(lines[-1]) > MAX_LINE:
                if row.get('origin') == 'peer':
                    self._codex_gap(row)
                    current['cursor'] += len(lines[-1])
                    row['codex_discard_record'] = list(codex_transcript.segment_key(row))
                    row.update(state='unknown', error='An oversized transcript record is being skipped; reply content will be withheld.')
                else:
                    row.update(state='unknown', error='A transcript record exceeded the safe limit; reply tracking stopped.')
                    row['tracking_stopped'] = True
        except codex_transcript.TemporaryUnavailable:
            row['error'] = 'The transcript is unavailable; Motet will keep checking for a reply.'
        except codex_transcript.Unavailable as error:
            row.update(state='unknown', error=str(error))
        except (OSError, MessagingError):
            row['error'] = 'The transcript is unavailable; Motet will keep checking for a reply.'
        else:
            if row.get('tracking_stopped') and not row.get('settled'):
                observations.clear()
            elif (peer and row.get('bound_turn') and not row.get('codex_gap_needs_envelope')
                  and not row.get('codex_discard_record')
                  and (current['cursor'] >= st.st_size or 'ended' in observations)):
                if 'ended' in observations:
                    observations.insert(-1, 'checked')
                else:
                    observations.append('checked')
        finally:
            if peer and row.get('bound_turn'):
                current = codex_transcript.segment(row)
                row['codex_peer_terminal_checked'] = [current.get(key) for key in ('device', 'inode', 'cursor')]
        if not peer and row['state'] in ('queued', 'awaiting_reply') and time.time() - row['created'] > TTL:
            row.update(state='expired', error=CODEX_REPLY_EXPIRED)
        return observations

    def _worker_current(self, row):
        # A detached worker selection is never sufficient to launch a protected
        # ID. Guarded launches must still name their exact durable ordinary row.
        with self.lock:
            if self._attachment_reserved(row['request_id']):
                return False
            if self._attachment_guard is None:
                return True
            raw = self.db.execute('SELECT data FROM requests WHERE id=?', (row['request_id'],)).fetchone()
            return raw is not None and json.loads(raw[0]) == row

    def _peer_capture_safe(self):
        # The stock Bridge reconciliation reads control rows directly for
        # interference evidence. Preserve its candidates until the contradiction
        # is gone instead of hiding a control and publishing a false reply.
        eligible = self._unreserved('controls.id')
        held = self.db.execute(f'SELECT 1 FROM controls WHERE NOT ({eligible}) LIMIT 1').fetchone() is not None
        if held and not self._attachment_reconciliation_held:
            print('Peer reply capture is held because a stored control alias needs owner request recovery.', file=sys.stderr)
        self._attachment_reconciliation_held = held
        return not held

    def tick(self):
        # A competing owner CLI transaction is transient; never kill the owner worker.
        with self.lock:
            try:
                with self.db:
                    self.mods.maintain()
                    peer_inbox.cleanup(self.db)
                rows = self._prune(self._rows(), time.time())
            except (sqlite3.OperationalError, OwnerGuardError):
                self.db.rollback()
                return
        for row in rows:
            if row.get('transport') == claude_mod.TRANSPORT:
                continue  # HTTP claims/events exclusively own these rows; no stale tick writeback.
            if row.get('runtime') == 'codex' and not codex_records_checkable():
                # Windows: a Codex row can only have come from a copy of a Mac's folder, since sending to Codex needs
                # the Mac app. Its sending and reply tracking check transcripts with POSIX owner and mode rules;
                # leave the row exactly as it is.
                continue
            if self.stop.is_set():
                break
            peer = row.get('origin') == 'peer'
            observations = [] if peer and row.get('bridge_version') == 2 and row.get('runtime') == 'codex' else None
            was_answered = row['state'] == 'answered'
            try:
                if not self._worker_current(row):
                    continue
                if row['state'] == 'sending' and not row['launched']:
                    if peer:
                        if row.get('bridge_version') == 2:
                            self.shared.authorize(row)
                        elif self.shared.enabled:
                            raise peer_bridge.PeerError(409, 'Legacy peer dispatch is disabled in shared mode.')
                        else:
                            self.peers.authorize(row)
                    else:
                        with self.lock:
                            row['launched'] = True
                            self._save(row)
                    if not self._worker_current(row):
                        continue
                    if observations is None:
                        row['state'], row['error'] = self._send(row)
                    else:
                        row['state'], row['error'] = self._send(row, observations=observations)
                elif row['state'] == 'sending' and row['launched']:
                    # A post-launch persistence failure cannot authorize a second send.
                    row.update(state='unknown', error='Delivery outcome could not be persisted. Motet will not resend.')
                if row['state'] in ACTIVE and row['state'] != 'sending' and not row.get('tracking_stopped'):
                    with self.lock:
                        if self._attachment_reserved(row['request_id']):
                            continue
                    observed = self._poll(row)
                    if observations is not None and observed:
                        observations.extend(observed)
            except (sqlite3.OperationalError, OwnerGuardError):
                with self.lock:
                    self.db.rollback()
                continue
            except peer_bridge.PeerRetry:
                # A partial target tail is temporary; authorization has not committed.
                continue
            except Exception as error:
                if not peer:
                    raise
                reason = 'Peer delivery could not be safely completed. Motet will not resend.'
                if not row.get('launched'):
                    reason = (str(error) if isinstance(error, peer_bridge.PeerError) else reason) + ' Nothing was sent.'
                row.update(state='unknown' if row.get('launched') else 'failed', reply='', candidate_reply='', tracking_stopped=True,
                           error=reason)
            with self.lock:
                try:
                    if self._attachment_reserved(row['request_id']):
                        continue
                    # Codex candidates are private until all owner cursors catch up.
                    if not was_answered and row['state'] == 'answered':
                        self.peers.owner_warning(row)
                    self._save(row, observations=observations or (), hold_observed=peer)
                except (sqlite3.OperationalError, OwnerGuardError):
                    self.db.rollback()
        with self.lock:
            try:
                current = [row for row in self._rows() if not self._attachment_reserved(row['request_id'])]
                if self._peer_capture_safe():
                    self.peers.reconcile(current)
                for row in current:
                    if not self._attachment_reserved(row['request_id']):
                        self._save(row)
            except (sqlite3.OperationalError, OwnerGuardError):
                self.db.rollback()

        self.reply_delivery.tick()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except sqlite3.OperationalError:
                with self.lock:
                    self.db.rollback()
            except Exception:
                # No exception text: it can contain user content or local paths.
                self.worker_failed = True
                self.available, self.reason = False, 'Reply tracking encountered an error. Restart Motet to recover safely.'
                return
            self.stop.wait(1)
