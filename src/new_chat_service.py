"""Private durable owner New chat orchestration. Native operations are injected.

Creation and UI opening each have a durable claim before the external operation.
Restart only verifies retained identities; it never replays native mutations.
Raw drafts exist only in a running worker's arguments, never in the journal.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from new_chat_inventory import KnownFolderInventory, canonical_uuid, folder_identity, public_folder, validate_folder
import private_storage as ps
from new_chat_types import (Capability, FrozenFolder, NewChatError, OpenResult, VerifiedIdentity, ExecutionResult,
                            MANAGED_PERMISSION_TIMEOUT_SECONDS)

PROVIDERS = ('codex', 'claude_code', 'antigravity', 'chatgpt_work')
WINDOW, INTENT_LIFE, RETENTION = 120, 300, 86400
_SAVED_FOLDER_ROWS = ("SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))=64 "
                      "THEN CAST(id AS BLOB) END,CASE WHEN typeof(data)='text' "
                      'THEN substr(CAST(data AS BLOB),1,65537) END FROM registered_folders')
MESSAGES = {
    'execution_unknown': 'The first instruction outcome is uncertain. It will not be sent again.',
    'owner_unavailable': 'Codex did not take ownership of this chat. The instruction was not submitted.',
    'execution_failed': 'Codex reported that this turn failed. Check the native chat.',
    'execution_attention': 'Codex needs attention in its native window.',
    'work_unavailable': 'Starting work is unavailable on this installed Codex build.',
    'disabled': 'New chat is not enabled on this Motet host.',
    'demo_unavailable': 'New chat cannot launch native applications in a demo.',
    'provider_unavailable': 'Starting a chat with this provider is not available.',
    'managed_disabled': 'Managed Claude is not enabled on this Motet host.',
    'configuration_unverified': 'The Claude configuration has not been verified.',
    'managed_unavailable': 'Managed Claude state is unavailable; recovery is required.',
    'capacity': 'The managed Claude host capacity has been reached.',
    'adapter_unavailable': 'The native New chat capability is unavailable.',
    'app_version_unsupported': 'This installed application version is not supported.',
    'unsupported_build': 'This installed application version is not supported.',
    'unsupported_platform': 'New chat currently requires the verified macOS host.',
    'native_defaults_unverified': 'Native model and permission defaults are not verified.',
    'folder_changed': 'The selected folder changed. Review its current entry.',
    'launch_outcome_unknown': 'The creation outcome is uncertain. No creation will be repeated.',
    'session_not_seen': 'No session was confirmed during this observation window.',
    'folder_mismatch': 'This chat belongs to a different folder and was not associated with the selected project.',
    'session_created_ui_open_failed': 'The chat was created, but Codex could not be opened.',
    'ui_open_outcome_unknown': 'The chat exists; opening Codex could not be confirmed.',
    'identity_unavailable': 'The exact native session could not be verified.',
    'clock_uncertain': 'The observation deadline could not be verified after a clock change.',
    'admission_unavailable': 'The session exists, but its monitor on the stage has not been confirmed.',
    'native_dispatch_rejected': 'The native application rejected this request.',
}


def _reason(code):
    code = code if code in MESSAGES else 'adapter_unavailable'
    return {'code': code, 'message': MESSAGES[code]}


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _error(code, message, status=409):
    raise NewChatError(code, message, status)


def _text(value, maximum):
    return (type(value) is str and len(value) <= maximum
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


class NewChatService:
    def __init__(self, state_dir, adapter, inventory_source, *, enabled=False, demo=False,
                 clock=time.monotonic, wall=time.time, host_id='local', host_label='This computer',
                 max_intents=128, max_active=32, max_attempts=1024, intent_rate=10, home=None,
                 folder_chooser=None):
        self.adapters = dict(adapter) if isinstance(adapter, dict) else {'codex': adapter}
        if any(provider not in PROVIDERS for provider in self.adapters):
            raise ValueError('Unknown New chat adapter provider.')
        # Preserve the original single-Codex injection seam for callers.
        self.adapter = self.adapters.get('codex')
        self.enabled, self.demo = enabled is True, demo is True
        self.folder_chooser = folder_chooser
        self.clock, self.wall = clock, wall
        self.host = {'id': host_id, 'label': host_label}
        self.max_intents, self.max_active, self.max_attempts, self.intent_rate = max_intents, max_active, max_attempts, intent_rate
        self.lock = threading.RLock()
        self.gates, self.jobs, self.deadlines, self.execution_deadlines = {}, {}, {}, {}
        self.closing, self.closed = False, False
        self._file_lock = None
        self.db = None
        self._journal_unavailable = False
        self.root = Path(state_dir).resolve() / 'new-chat'
        try:
            self._storage()
            row = self.db.execute('SELECT value FROM meta WHERE key=?', ('hmac',)).fetchone()
            if row is None:
                self.key = secrets.token_bytes(32)
                with self.db:
                    self.db.execute('INSERT INTO meta VALUES (?, ?)', ('hmac', self.key.hex()))
            else:
                self.key = bytes.fromhex(row[0])
                if len(self.key) != 32:
                    raise ValueError()
            self.inventory = KnownFolderInventory(inventory_source, self.key, state_root=self.root.parent, home=home)
            self._recover()
        except Exception:
            self._dispose()
            raise

    def _windows_storage(self):
        """Windows: the same private folder and files through private_storage. The folders are made private from the
        start (no link or junction on the way, each checked on its handle), the writer lock and the journal are
        private files with one name, and the writer lock is private_storage's byte lock, taken without waiting."""
        with ps.open_folder(str(self.root.parent.parent), writable=True) as home:
            try:
                home.mkdir(self.root.parent.name)
            except FileExistsError:
                pass
            with home.folder(self.root.parent.name, private=True, writable=True) as state:
                try:
                    state.mkdir(self.root.name)
                except FileExistsError:
                    pass
                with state.folder(self.root.name, private=True, writable=True) as folder:
                    self._file_lock = folder.open_or_create('writer.lock')
                    if not ps.try_lock(self._file_lock):
                        raise BlockingIOError('New chat is already open in another Motet.')
                    os.close(folder.open_or_create('journal.sqlite'))

    def _storage(self):
        if ps.WIN:
            self._windows_storage()
            self._database()
            return
        import fcntl
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta = self.root.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o700 or self.root.resolve() != self.root.absolute()):
            raise ValueError('New chat requires private local storage.')
        def private_file(path):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                os.close(fd)
                raise ValueError('New chat requires private local storage.')
            return fd
        self._file_lock = private_file(self.root / 'writer.lock')
        fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = private_file(self.root / 'journal.sqlite')
        os.close(fd)
        self._database()

    def _database(self):
        self.db = sqlite3.connect(self.root / 'journal.sqlite', check_same_thread=False, timeout=2)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (
              id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
              data TEXT NOT NULL, attempt_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS attempts (
              id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS registered_folders (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intent_issues (id TEXT PRIMARY KEY, issued REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS native_receipts (
              native_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE);
        ''')

    def _dispose(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._file_lock is not None:
            if ps.WIN:
                ps.close_lock(self._file_lock)       # Windows may keep a byte lock a while after a close
            else:
                os.close(self._file_lock)
            self._file_lock = None

    def _now(self):
        now, mono = self.wall(), self.clock()
        if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in (now, mono)):
            _error('clock_uncertain', MESSAGES['clock_uncertain'], 503)
        return now, mono

    def _guard(self):
        if self.closing:
            _error('service_closed', 'New chat is shutting down.', 503)
        if self.demo:
            _error('demo_unavailable', MESSAGES['demo_unavailable'], 503)
        if not self.enabled:
            _error('disabled', MESSAGES['disabled'], 503)
        self._journal_guard()

    def _journal_guard(self):
        if self._journal_unavailable:
            _error('saved_folders_unavailable',
                   'Saved project folders are unavailable. Restart Motet before trying again.', 503)

    def _adapter(self, row):
        adapter = self.adapters.get(row['provider'])
        if adapter is None:
            _error('provider_unavailable', MESSAGES['provider_unavailable'], 503)
        return adapter

    @staticmethod
    def _managed(row):
        return row.get('ownership', 'desktop') == 'watch_managed'

    def _capability(self, provider='codex'):
        with self.lock:
            self._journal_guard()
            if self.demo or not self.enabled or self.closing:
                code = 'demo_unavailable' if self.demo else 'disabled'
                return Capability(False, code, MESSAGES[code])
            try:
                adapter = self.adapters.get(provider)
                if adapter is None:
                    return Capability(False, 'provider_unavailable', MESSAGES['provider_unavailable'])
                cap = adapter.preflight()
                if (not isinstance(cap, Capability) or type(cap.available) is not bool
                        or type(cap.supports_work) is not bool
                        or type(cap.supports_blank) is not bool
                        or cap.ownership not in ('desktop', 'watch_managed')
                        or cap.ownership == 'watch_managed' and (provider != 'claude_code' or cap.supports_blank)
                        or not _text(cap.version, 200) or cap.available and not cap.version):
                    raise ValueError()
                return cap
            except Exception:
                return Capability(False, 'adapter_unavailable', MESSAGES['adapter_unavailable'])

    def _decode_registered_folder(self, ident_bytes, raw_bytes):
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('Duplicate saved folder field.')
                result[key] = value
            return result

        if type(ident_bytes) is not bytes or type(raw_bytes) is not bytes or len(raw_bytes) > 65536:
            raise ValueError('Invalid saved folder data.')
        ident = ident_bytes.decode('utf-8')
        value = json.loads(raw_bytes.decode('utf-8'), object_pairs_hook=unique)
        return self.inventory.restore_registered(ident, value)

    def _read_registered_folders(self):
        folders, unreadable = [], 0
        with self.lock:
            self._journal_guard()
            # API writes already cap the row count. Bound individual transfers
            # too, so a damaged row cannot force an unbounded JSON allocation.
            rows = self.db.execute(_SAVED_FOLDER_ROWS + ' ORDER BY id')
            for ident_bytes, raw_bytes in rows:
                try:
                    folders.append(self._decode_registered_folder(ident_bytes, raw_bytes))
                except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                    unreadable += 1
        return folders, unreadable

    def _registered_folders(self):
        return self._read_registered_folders()[0]

    def saved_folders(self):
        with self.lock:
            self._guard()
            try:
                folders, unreadable = self._read_registered_folders()
            except sqlite3.Error:
                _error('saved_folders_unavailable',
                       'Saved project folders are unavailable. Refresh the list to check.', 503)
            return {'schemaVersion': 1, 'savedFolders': [public_folder(folder) for folder in folders],
                    'complete': unreadable == 0, 'unreadableCount': unreadable}

    def remove_saved_folder(self, ident, data):
        self._guard()
        def identity(value):
            return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
        if (not identity(ident) or type(data) is not dict or set(data) != {'revision'}
                or not identity(data['revision'])):
            _error('invalid_request', 'Invalid New chat request.', 400)
        with self.lock:
            self._guard()
            try:
                with self.db:
                    row = self.db.execute(_SAVED_FOLDER_ROWS + ' WHERE id=?', (ident,)).fetchone()
                    if row is None:
                        _error('unknown_saved_folder', 'This folder is no longer saved in Motet.', 404)
                    try:
                        folder = self._decode_registered_folder(*row)
                    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                        _error('unreadable_saved_folder',
                               'This saved project folder could not be verified. Nothing was removed.', 409)
                    if not hmac.compare_digest(folder.revision, data['revision']):
                        _error('saved_folder_changed',
                               'This saved project folder changed. Refresh the list before removing it.', 409)
                    deleted = self.db.execute("DELETE FROM registered_folders WHERE id=? AND typeof(id)='text' "
                                              "AND typeof(data)='text' AND CAST(data AS BLOB)=?", (ident, row[1]))
                    if deleted.rowcount != 1:
                        _error('saved_folder_changed',
                               'This saved project folder changed. Refresh the list before removing it.', 409)
            except sqlite3.Error:
                # Older sqlite3 context managers can leave a failed COMMIT's
                # delete pending. Finish recovery before another caller enters.
                try:
                    if self.db.in_transaction:
                        self.db.rollback()
                    if self.db.in_transaction:
                        raise sqlite3.DatabaseError('Journal rollback remained pending.')
                except sqlite3.Error:
                    self._journal_unavailable = True
                    try:
                        self.db.close()
                    except sqlite3.Error:
                        pass
                    finally:
                        self.db = None
                _error('saved_folders_unavailable',
                       ('Saved project folders are unavailable. Restart Motet before trying again.'
                        if self._journal_unavailable else
                        'Saved project folders are unavailable. Refresh the list to check.'), 503)
        return {'schemaVersion': 1, 'removed': {'id': folder.id, 'revision': folder.revision}}

    def register_folder(self, data):
        self._guard()
        if type(data) is not dict or set(data) != {'path'}:
            _error('invalid_request', 'Choose one project folder to add.', 400)
        with self.lock:
            self._guard()
            folder = self.inventory.register(data['path'])
            with self.db:
                self._guard()
                validate_folder(folder)
                self.inventory.require_allowed(folder)
                if (self.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0] >= self.inventory.maximum
                        and not self.db.execute('SELECT 1 FROM registered_folders WHERE id=?', (folder.id,)).fetchone()):
                    _error('capacity', 'The saved project folder limit has been reached.', 429)
                self.db.execute('INSERT OR REPLACE INTO registered_folders VALUES (?, ?)',
                                (folder.id, json.dumps(asdict(folder), ensure_ascii=True)))
            return {'schemaVersion': 1, 'folder': dict(public_folder(folder), registered=True)}

    def _chooser(self):
        self._guard()
        if self.folder_chooser is None:
            _error('chooser_unavailable', 'The folder window is unavailable on this host.', 503)
        return self.folder_chooser

    def begin_folder_choice(self, data, connection, forwarded=False):
        return self._chooser().begin(data, connection, self._guard, forwarded=forwarded)

    def folder_choice_status(self, ident):
        return self._chooser().status(ident)

    def cancel_folder_choice(self, ident, data):
        chooser = self._chooser()
        return chooser.cancel(ident, data)

    def accept_folder_choice(self, ident, data):
        return self._chooser().accept(ident, data, self.register_folder, self._guard)

    def _inventory(self, registered):
        with self.lock:
            self._journal_guard()
            rows = self.inventory.refresh(registered)
            with self.db:
                self.db.execute('DELETE FROM folders')
                self.db.executemany('INSERT INTO folders VALUES (?, ?)',
                                    [(r['folder']['id'], json.dumps(r, ensure_ascii=True)) for r in self.inventory.snapshot()])
            return rows

    def context(self):
        with self.lock:
            self._journal_guard()
            with self.db:
                self._gc(self._now()[0])
        # Disabled/demo context has no native reads, even through inventory.
        can_register = self.enabled and not self.demo and not self.closing
        registered = self._registered_folders() if can_register else []
        registered_revisions = {folder.id: folder.revision for folder in registered}
        folders = self._inventory(registered) if can_register else []
        providers = []
        for provider in PROVIDERS:
            cap = self._capability(provider)
            available = cap.available
            code = cap.code
            providers.append({'id': provider, 'label': {'codex': 'Codex', 'claude_code': 'Claude Code',
                              'antigravity': 'Antigravity', 'chatgpt_work': 'ChatGPT Work'}[provider],
                              'available': available, 'supportsDraft': provider == 'codex',
                              'ownership': cap.ownership,
                              'supportsBlank': available and cap.supports_blank, 'supportsWork': available and cap.supports_work,
                              'unavailableReason': None if available else _reason(code)})
        with self.lock:
            self._journal_guard()
            return {'schemaVersion': 1, 'fixture': False, 'host': dict(self.host),
                    'canChooseFolders': can_register and self.folder_chooser is not None,
                    'folderChoiceEpoch': self.folder_chooser.epoch if can_register and self.folder_chooser is not None else None,
                    'folders': [dict(public_folder(f), registered=True) if registered_revisions.get(f.id) == f.revision else public_folder(f)
                                for f in folders], 'providers': providers,
                    'limits': {'observationWindowMs': WINDOW * 1000,
                               'managedPermissionTimeoutSeconds': MANAGED_PERMISSION_TIMEOUT_SECONDS}}

    @staticmethod
    def _selection(data, *, opening=False):
        expected = {'provider', 'folderId', 'folderRevision'} | ({'requestId'} if opening else set())
        if (type(data) is not dict or not expected <= data.keys()
                or data.keys() - expected - ({'draft', 'instruction', 'mode'} if opening else {'mode'})
                or data.get('provider') not in PROVIDERS
                or not _text(data.get('folderId'), 128) or not data.get('folderId')
                or not _text(data.get('folderRevision'), 128) or not data.get('folderRevision')
                or opening and not canonical_uuid(data.get('requestId'))):
            _error('invalid_request', 'Invalid New chat request.', 400)
        mode = data.get('mode', 'blank')
        if mode not in ('blank', 'work'):
            _error('invalid_mode', 'Choose blank chat or start working.', 400)
        if mode == 'work' and 'draft' in data or mode == 'blank' and 'instruction' in data:
            _error('invalid_request', 'Drafts and working instructions are separate operations.', 400)
        if opening and mode == 'work' and (not _text(data.get('instruction'), 8000)
                or not data['instruction'].strip() or '\0' in data['instruction']):
            _error('invalid_instruction', 'An instruction must contain plain text of at most 8,000 characters.', 400)
        draft = data.get('draft')
        if opening and 'draft' in data and not _text(draft, 8000):
            _error('invalid_draft', 'A draft must be plain text of at most 8,000 characters.', 400)
        if opening and draft and data['provider'] != 'codex':
            _error('draft_unsupported', 'This provider cannot receive a draft from Motet.', 422)

    def _gc(self, now):
        self.db.execute('DELETE FROM intents WHERE expires<=? AND attempt_id IS NULL', (now,))
        self.db.execute('DELETE FROM intent_issues WHERE issued<=?', (now - 60,))
        # Details become 410 tombstones at 24h; the tombstone lives another
        # bounded day, then unknown IDs still reject instead of authorizing work.
        dead = []
        for ident, expires, raw in self.db.execute('SELECT id, expires, data FROM attempts WHERE expires<=?', (now,)).fetchall():
            row = json.loads(raw)
            # Managed lifecycle ownership survives the observation journal's
            # expiry. Retain its binding (within max_attempts) fail-closed;
            # absence of a live socket is never evidence that work stopped.
            if self._managed(row) and row.get('managed'):
                continue
            self.db.execute('UPDATE attempts SET data=? WHERE id=?', ('{"expired":true}', ident))
            self.db.execute('UPDATE intents SET data=? WHERE attempt_id=?', ('{}', ident))
            if expires <= now - RETENTION:
                self.db.execute('DELETE FROM intents WHERE attempt_id=?', (ident,))
                self.db.execute('DELETE FROM native_receipts WHERE attempt_id=?', (ident,))
                self.db.execute('DELETE FROM attempts WHERE id=?', (ident,))
                dead.append(ident)
        for ident in dead:
            self.deadlines.pop(ident, None)
            self.execution_deadlines.pop(ident, None)
            self.gates.pop(ident, None)

    def prepare(self, data):
        self._selection(data)
        self._guard()
        with self.lock:
            self._guard()
            cap = self._capability(data['provider'])
            if not cap.available:
                _error(_reason(cap.code)['code'], _reason(cap.code)['message'], 503)
            if data.get('mode', 'blank') == 'work' and not cap.supports_work:
                _error('work_unavailable', MESSAGES['work_unavailable'], 503)
            if data.get('mode', 'blank') == 'blank' and not cap.supports_blank:
                _error('blank_unavailable', 'Blank chat is unavailable for this provider.', 503)
            folder = self.inventory.get(data['folderId'], data['folderRevision'], self._registered_folders())
            now, _ = self._now()
            ident = str(uuid.uuid4())
            frozen = {'provider': data['provider'], 'mode': data.get('mode', 'blank'), 'folder': asdict(folder),
                      'adapter_version': cap.version, 'ownership': cap.ownership}
            with self.db:
                self._guard()
                self._gc(now)
                if self.db.execute('SELECT count(*) FROM intents WHERE attempt_id IS NULL').fetchone()[0] >= self.max_intents:
                    _error('capacity', 'Too many prepared New chat requests.', 429)
                if self.db.execute('SELECT count(*) FROM intent_issues').fetchone()[0] >= self.intent_rate:
                    _error('rate_limited', 'New chat requests are arriving too quickly.', 429)
                self.db.execute('INSERT INTO intents VALUES (?, ?, ?, ?, NULL)',
                                (ident, now, now + INTENT_LIFE, json.dumps(frozen, ensure_ascii=True)))
                self.db.execute('INSERT INTO intent_issues VALUES (?, ?)', (ident, now))
            return {'schemaVersion': 1, 'requestId': ident, 'provider': data['provider'], 'mode': data.get('mode', 'blank'),
                    'folder': public_folder(folder), 'expiresAt': _iso(now + INTENT_LIFE), 'host': dict(self.host)}

    def _digest(self, data):
        return hmac.new(self.key, json.dumps(data, sort_keys=True, ensure_ascii=True,
                                            separators=(',', ':')).encode(), hashlib.sha256).hexdigest()

    def _load(self, ident, *, expired=False):
        row = self.db.execute('SELECT data, expires FROM attempts WHERE id=?', (ident,)).fetchone()
        if row is None:
            _error('unknown_attempt', 'This New chat request is unknown.', 404)
        now, _ = self._now()
        if not expired and now >= row[1]:
            _error('attempt_expired', 'This New chat request has expired.', 410)
        value = json.loads(row[0])
        if value.get('expired'):
            _error('attempt_expired', 'This New chat request has expired.', 410)
        return value

    def _save(self, data):
        self.db.execute('UPDATE attempts SET data=? WHERE id=?',
                        (json.dumps(data, ensure_ascii=True, allow_nan=False), data['id']))

    def _expire(self, row):
        now, mono = self._now()
        if row['observation'] != 'active':
            return
        deadline = self.deadlines.get(row['id'])
        uncertain = now < row['window_started'] or (deadline is not None and
                    abs((row['deadline'] - now) - (deadline - mono)) > 5)
        if uncertain or now >= row['deadline'] or deadline is not None and mono >= deadline:
            row.update(state='timeout', observation='complete', generation=row['generation'] + 1,
                       reason=_reason('clock_uncertain' if uncertain else 'session_not_seen'))
            self._save(row)

    def _view(self, row):
        folder = FrozenFolder(**row['folder'])
        existing = ({'id': row['native_id'], 'folderId': folder.id}
                    if row['creation'] == 'verified' and row['identity']['cwd'] == folder.path else None)
        session = ({'id': row['native_id'], 'folderId': folder.id, 'roomAgentId': row['room_id']}
                   if row['state'] == 'added' else None)
        execution = dict(row.get('execution', {'attempts': 0, 'outcome': 'not_attempted', 'turnId': None, 'reason': None}))
        execution.setdefault('receiptKind', ('native_user_message' if self._managed(row) else 'native_turn')
                             if execution.get('turnId') else None)
        return {'schemaVersion': 1, 'fixture': False, 'attemptId': row['id'], 'requestId': row['request_id'],
                'provider': row['provider'], 'mode': row.get('mode', 'blank'),
                'ownership': row.get('ownership', 'desktop'), 'managed': row.get('managed'),
                'execution': execution,
                'folder': public_folder(folder), 'state': row['state'],
                'launch': {'attempts': row['creation_attempts'], 'outcome': row['launch']},
                'creation': {'outcome': row['creation']},
                'uiOpen': {'attempts': row['ui_attempts'], 'outcome': row['ui']},
                'existingSession': existing, 'session': session, 'mismatch': row.get('mismatch'),
                'observation': row['observation'], 'createdAt': _iso(row['created']),
                'deadlineAt': _iso(row['deadline']), 'reason': row['reason'],
                'draft': {'provided': row['draft_provided'], 'disposition': row['draft_disposition']},
                # Keep waiting is offered only where the continue action can run (see _guard); Stop stays local.
                'capabilities': {'keepWaiting': row['state'] == 'timeout' and row['observation'] != 'stopped'
                                 and self.wall() + WINDOW <= row['created'] + RETENTION
                                 and self.enabled and not self.demo and not self.closing,
                                 'stopWaiting': row['observation'] == 'active', 'openExisting': False}}

    def open(self, data):
        self._selection(data, opening=True)
        self._guard()
        normalized = dict(data, mode=data.get('mode', 'blank'))
        if not normalized.get('draft'):
            normalized.pop('draft', None)
        digest, now_mono = self._digest(normalized), self._now()
        now, mono = now_mono
        with self.lock:
            self._guard()
            with self.db:
                self._gc(now)
                prior = self.db.execute('SELECT id, digest FROM attempts WHERE request_id=?', (data['requestId'],)).fetchone()
                if prior is not None:
                    prior_row = self._load(prior[0])
                    legacy = dict(normalized)
                    legacy.pop('mode', None)
                    legacy_match = (prior_row.get('mode') is None and normalized['mode'] == 'blank'
                                    and hmac.compare_digest(prior[1], self._digest(legacy)))
                    if not hmac.compare_digest(prior[1], digest) and not legacy_match:
                        _error('replay_conflict', 'This request was already used with a different selection or draft.', 409)
                    row = self._load(prior[0])
                    self._expire(row)
                    return 200, self._view(row)
                intent = self.db.execute('SELECT data, expires, attempt_id FROM intents WHERE id=?', (data['requestId'],)).fetchone()
                if intent is None or intent[1] <= now:
                    _error('intent_expired', 'Prepare a new folder review before opening a chat.', 409)
                if intent[2] is not None:
                    _error('intent_spent', 'This request has already been used.', 409)
                frozen = json.loads(intent[0])
                folder = FrozenFolder(**frozen['folder'])
                if (data['provider'] != frozen['provider'] or data['folderId'] != folder.id
                        or data['folderRevision'] != folder.revision
                        or data.get('mode', 'blank') != frozen.get('mode', 'blank')):
                    _error('intent_conflict', 'This request does not match the prepared folder review.', 409)
                if self.db.execute('SELECT count(*) FROM attempts').fetchone()[0] >= self.max_attempts:
                    _error('capacity', 'The retained New chat request limit has been reached.', 429)
                active = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM attempts WHERE expires>?', (now,))]
                for previous in active:
                    self._expire(previous)
                active = [r for r in active if r['observation'] == 'active']
                if len(active) >= self.max_active or len(self.jobs) >= self.max_active:
                    _error('capacity', 'Too many New chat requests are being observed.', 429)
                if any(r['provider'] == data['provider'] and r['folder']['id'] == folder.id for r in active):
                    _error('folder_busy', 'A New chat request for this folder is already active.', 409)
                # All preflight/selection checks precede the one durable claim.
                self.inventory.get(folder.id, folder.revision, self._registered_folders())
                cap = self._capability(frozen['provider'])
                available = (cap.available and cap.version == frozen['adapter_version']
                             and cap.ownership == frozen.get('ownership', 'desktop')
                             and (cap.supports_blank if normalized['mode'] == 'blank' else cap.supports_work))
                ident = str(uuid.uuid4())
                row = {'id': ident, 'request_id': data['requestId'], 'provider': data['provider'],
                       'folder': asdict(folder), 'adapter_version': frozen['adapter_version'],
                       'ownership': frozen.get('ownership', 'desktop'), 'managed': None,
                       'mode': data.get('mode', 'blank'),
                       'execution': {'attempts': 0, 'outcome': 'not_attempted', 'turnId': None, 'reason': None,
                                     'pending': available and data.get('mode', 'blank') == 'work'},
                       'created': now, 'window_started': now, 'deadline': now + WINDOW,
                       'generation': 0, 'observation': 'active' if available else 'complete',
                       'state': 'waiting' if available else 'unavailable', 'phase': 'claimed' if available else 'unavailable',
                       'creation_attempts': int(available), 'launch': 'unknown' if available else 'not_attempted',
                       'creation': 'unknown' if available else 'not_attempted', 'native_id': None, 'identity': None,
                       'ui_attempts': 0, 'ui': 'not_required' if frozen.get('ownership') == 'watch_managed' else 'not_attempted',
                       'room_id': None, 'mismatch': None,
                       'draft_provided': bool(data.get('draft')), 'draft_disposition': 'unverified' if data.get('draft') else 'not_provided',
                       'reason': None if available else _reason(cap.code if not cap.available else
                           'work_unavailable' if data.get('mode') == 'work' and not cap.supports_work else 'app_version_unsupported')}
                self.db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?)',
                                (ident, data['requestId'], digest, now, now + RETENTION, json.dumps(row, ensure_ascii=True)))
                self.db.execute('UPDATE intents SET attempt_id=? WHERE id=?', (ident, data['requestId']))
                self.deadlines[ident] = mono + WINDOW
                self.execution_deadlines[ident] = mono + WINDOW
                self.gates[ident] = threading.RLock()
                initial = self._view(row)
                worker = None
                if available:
                    worker = threading.Thread(target=self._dispatch, args=(ident, data.get('draft'), data.get('instruction')), daemon=True,
                                              name='watch-new-chat-create')
                    # Commit before the worker can run, but reserve its capacity slot
                    # under this same lock. Stop cannot free an unregistered worker.
                    self.db.commit()
                    self.jobs[ident] = worker
        if available:
            with self.lock:
                if self._journal_unavailable:
                    self.jobs.pop(ident, None)
                    self._journal_guard()
                if self.closing:
                    self.jobs.pop(ident, None)
                    if not self.jobs:
                        self.closed = True
                        self._dispose()
                    return 202, initial
                try:
                    worker.start()
                except RuntimeError:
                    self.jobs.pop(ident, None)
                    with self.db:
                        row = self._load(ident)
                        row['reason'] = _reason('launch_outcome_unknown')
                        row['execution']['pending'] = False
                        self._save(row)
        return 202, initial

    def _receipt(self, ident, native_id, cwd):
        if not canonical_uuid(native_id) or not _text(cwd, 4096) or not os.path.isabs(cwd):
            _error('identity_unavailable', MESSAGES['identity_unavailable'])
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            if row['native_id'] not in (None, native_id):
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            if row['native_id'] is not None and row.get('receipt_cwd') != cwd:
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            binding = {'native_identity': native_id}
            if row['provider'] != 'codex':
                binding['provider'] = row['provider']
            native_digest = self._digest(binding)
            prior = self.db.execute('SELECT attempt_id FROM native_receipts WHERE native_id=?', (native_digest,)).fetchone()
            if prior is not None and prior[0] != ident:
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            self.db.execute('INSERT OR IGNORE INTO native_receipts VALUES (?, ?)', (native_digest, ident))
            row.update(native_id=native_id, receipt_cwd=cwd, phase='receipt', launch='accepted')
            self._save(row)
        if cwd != row['folder']['path'] or self.closing:
            _error('folder_mismatch' if cwd != row['folder']['path'] else 'service_closed',
                   MESSAGES['folder_mismatch'] if cwd != row['folder']['path'] else 'New chat is shutting down.')

    def _checked_identity(self, value, row):
        folder = validate_folder(FrozenFolder(**row['folder']))
        self.inventory.require_allowed(folder)
        if (not isinstance(value, VerifiedIdentity) or value.native_id != row['native_id']
                or value.provider != row['provider']
                or value.adapter_version != row['adapter_version']
                or not canonical_uuid(value.native_id)
                or not os.path.isabs(value.cwd) or os.path.realpath(value.cwd) != value.cwd
                or not os.path.isabs(value.source_path) or os.path.realpath(value.source_path) != value.source_path):
            _error('identity_unavailable', MESSAGES['identity_unavailable'])
        source = os.stat(value.source_path, follow_symlinks=False)
        if (not stat.S_ISREG(source.st_mode) or source.st_nlink != 1
                or (source.st_dev, source.st_ino) != (value.source_device, value.source_inode)):
            _error('identity_unavailable', MESSAGES['identity_unavailable'])
        if ps.WIN:
            # The same checks; the folder identity is the handle's, as the inventory records it.
            if not stat.S_ISDIR(os.stat(value.cwd, follow_symlinks=False).st_mode):
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            device, inode = folder_identity(value.cwd)
            if value.folder_device and value.folder_device != device or value.folder_inode and value.folder_inode != inode:
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            return value
        directory = os.stat(value.cwd, follow_symlinks=False)
        if (not stat.S_ISDIR(directory.st_mode)
                or value.folder_device and value.folder_device != directory.st_dev
                or value.folder_inode and value.folder_inode != directory.st_ino):
            _error('identity_unavailable', MESSAGES['identity_unavailable'])
        return value

    def _identity(self, value, row):
        value = self._checked_identity(value, row)
        if value.cwd != row['folder']['path']:
            _error('folder_mismatch', MESSAGES['folder_mismatch'])
        return value

    def _verified_row(self, row, identity):
        self._expire(row)
        row.update(identity=asdict(identity), creation='verified')
        if identity.cwd != row['folder']['path']:
            # Only independently verified actual identity establishes mismatch;
            # a creation-RPC cwd field alone cannot establish persisted evidence.
            if row['observation'] == 'active':
                row.update(state='mismatch', observation='complete', reason=_reason('folder_mismatch'),
                           mismatch={'sessionId': identity.native_id, 'displayPath': identity.cwd,
                                     'existingRoomAgentId': None})
            row['phase'] = 'mismatch_verified'
            return False
        return True

    def _dispatch(self, ident, draft, instruction=None):
        creation_started = False
        try:
            with self.lock:
                row = self._load(ident, expired=True)
            adapter = self._adapter(row)
            folder = validate_folder(FrozenFolder(**row['folder']))
            self.inventory.require_allowed(folder)
            with self.lock:
                if self.closing or self._journal_unavailable:
                    return
                creation_started = True
            if self._managed(row):
                self._dispatch_managed(ident, adapter, folder, instruction)
                return
            identity = adapter.create_once(folder, lambda native_id, cwd: self._receipt(ident, native_id, cwd))
            with self.lock:
                row = self._load(ident, expired=True)
            identity = self._checked_identity(identity, row)
            with self.lock, self.db:
                row = self._load(ident, expired=True)
                same_folder = self._verified_row(row, identity)
                if same_folder:
                    row['phase'] = 'verified'
                self._save(row)
            if not same_folder or self.closing:
                return
            # Recheck the path and identity before claiming the separate UI open.
            identity = self._identity(adapter.verify_existing(identity.native_id, folder), row)
            with self.lock, self.db:
                if self.closing:
                    return
                row = self._load(ident, expired=True)
                row.update(ui_attempts=1, ui='unknown', phase='ui_claimed')
                self._save(row)
            result = adapter.open_existing(identity, draft)
            if not isinstance(result, OpenResult) or result.outcome not in ('accepted', 'unknown', 'rejected'):
                raise ValueError()
            with self.lock, self.db:
                row = self._load(ident, expired=True)
                row.update(ui=result.outcome, phase='ui_settled')
                if row['draft_provided']:
                    row['draft_disposition'] = 'prefill_requested' if result.outcome == 'accepted' else 'unverified'
                if result.outcome == 'rejected':
                    row.update(state='failed', reason=_reason('session_created_ui_open_failed'),
                               observation='complete' if row['observation'] != 'stopped' else 'stopped')
                elif result.outcome == 'unknown':
                    row['reason'] = _reason('ui_open_outcome_unknown')
                self._expire(row)
                self._save(row)
            if row.get('mode') == 'work' and result.outcome == 'accepted':
                outcome = adapter.start_work_once(identity, instruction,
                    lambda: self._execution_claim(ident),
                    lambda turn_id: self._execution_receipt(ident, turn_id))
                if not isinstance(outcome, ExecutionResult) or outcome.outcome not in (
                        'not_attempted', 'unknown', 'accepted', 'running', 'completed', 'failed', 'needs_attention'):
                    raise ValueError()
                with self.lock, self.db:
                    row = self._load(ident, expired=True)
                    execution = row['execution']
                    if outcome.turn_id is not None and execution['turnId'] != outcome.turn_id:
                        raise ValueError()
                    execution.update(outcome=outcome.outcome,
                        reason=_reason(outcome.code) if outcome.code else None)
                    self._save(row)
        except Exception:
            # Exceptions may occur after external side effects. Never replay or
            # publish native exception strings (which can contain drafts/paths).
            recovered = None
            with self.lock:
                retained = self._load(ident, expired=True) if self.db is not None else None
            if retained and retained['native_id'] and not self.closing:
                try:
                    recovered = self._checked_identity(self._adapter(retained).verify_existing(
                        retained['native_id'], FrozenFolder(**retained['folder'])), retained)
                except Exception:
                    pass
            with self.lock:
                if self.db is not None:
                    with self.db:
                        row = self._load(ident, expired=True)
                        code = ('session_created_ui_open_failed' if row['creation'] == 'verified' and row['ui_attempts']
                                else 'identity_unavailable' if row['native_id'] else 'launch_outcome_unknown')
                        if row['ui_attempts']:
                            row['ui'] = 'unknown'
                        row['reason'] = _reason(code)
                        if (row.get('execution', {}).get('attempts')
                                and not (self._managed(row) and row['execution'].get('turnId'))):
                            row['execution'].update(outcome='unknown', reason=_reason('execution_unknown'))
                        if recovered:
                            self._verified_row(row, recovered)
                        if not creation_started:
                            row.update(creation='not_attempted', launch='rejected', state='failed',
                                       observation='complete', reason=_reason('folder_changed'))
                        self._expire(row)
                        self._save(row)
        finally:
            draft = instruction = None
            with self.lock:
                if self.db is not None:
                    with self.db:
                        row = self._load(ident, expired=True)
                        if 'execution' in row:
                            row['execution']['pending'] = False
                            self._save(row)
                self.jobs.pop(ident, None)
                if self.closing and not self.jobs:
                    self.closed = True
                    self._dispose()

    def _dispatch_managed(self, ident, adapter, folder, instruction):
        run_id = adapter.reserve(folder, ident)
        if not canonical_uuid(run_id):
            raise ValueError()
        # This control handle is durable before launch, and is deliberately
        # independent of native creation/transcript/admission evidence.
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            if row.get('managed') is not None:
                raise ValueError()
            row.update(managed={'runId': run_id, 'state': 'starting',
                                'recoveryRequired': False, 'permissionsAvailable': False},
                       phase='managed_reserved')
            self._save(row)
        if self.closing:
            return
        adapter.launch_once(run_id, folder, lambda native_id, cwd: self._receipt(ident, native_id, cwd))
        with self.lock:
            row = self._load(ident, expired=True)
        # An accepted launch call alone is not a retained native receipt.
        if not row['native_id'] or row.get('receipt_cwd') != folder.path or self.closing:
            return
        outcome = adapter.start_managed_work_once(run_id, instruction,
            lambda: self._execution_claim(ident),
            lambda receipt_id: self._execution_receipt(ident, receipt_id))
        if (not isinstance(outcome, ExecutionResult) or outcome.outcome not in (
                'not_attempted', 'unknown', 'accepted', 'running', 'completed', 'failed', 'needs_attention')
                or outcome.receipt_kind != 'native_user_message'):
            raise ValueError()
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            execution = row['execution']
            if outcome.turn_id is not None and execution['turnId'] != outcome.turn_id:
                raise ValueError()
            if outcome.outcome in ('accepted', 'running', 'completed') and not execution['turnId']:
                raise ValueError()
            execution.update(outcome=outcome.outcome, reason=_reason(outcome.code) if outcome.code else None)
            self._save(row)
        # First-send authorization precedes transcript proof for managed work.
        identity = self._checked_identity(adapter.verify_existing(row['native_id'], folder), row)
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            if self._verified_row(row, identity):
                row['phase'] = 'verified'
            self._save(row)

    def _execution_claim(self, ident):
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            self._expire(row)
            now, mono = self._now()
            deadline = self.execution_deadlines.get(ident)
            clock_bad = (now < row['created'] or deadline is None
                         or abs((row['created'] + WINDOW - now) - (deadline - mono)) > 5)
            ready = (bool(row.get('managed')) and row['native_id']
                     and row.get('receipt_cwd') == row['folder']['path']) if self._managed(row) else (
                         row['creation'] == 'verified' and row['ui'] == 'accepted')
            if (self.closing or clock_bad or now >= row['created'] + WINDOW or mono >= deadline
                    or row.get('mode') != 'work' or row['execution']['attempts']
                    or not ready):
                _error('execution_not_claimed', 'The instruction was not submitted.', 409)
            folder = validate_folder(FrozenFolder(**row['folder']))
            self.inventory.require_allowed(folder)
            row['execution'].update(attempts=1, outcome='unknown', reason=_reason('execution_unknown'))
            self._save(row)
        # The commit is complete before this callback returns to the adapter.

    def _execution_receipt(self, ident, turn_id):
        if not canonical_uuid(turn_id):
            _error('identity_unavailable', MESSAGES['identity_unavailable'])
        with self.lock, self.db:
            row = self._load(ident, expired=True)
            if row['execution']['attempts'] != 1 or row['execution']['turnId'] not in (None, turn_id):
                _error('identity_unavailable', MESSAGES['identity_unavailable'])
            row['execution'].update(turnId=turn_id, outcome='accepted', reason=None,
                receiptKind='native_user_message' if self._managed(row) else 'native_turn')
            self._save(row)

    def _refresh_execution(self, row):
        if not self.enabled or self.demo or self.closing:
            return
        if self._managed(row):
            self._refresh_managed(row)
            return
        execution = row.get('execution')
        adapter = self.adapters.get(row['provider'])
        if (not execution or not execution.get('turnId') or not row.get('identity')
                or execution['outcome'] not in ('unknown', 'accepted', 'running')
                or not callable(getattr(adapter, 'execution_status', None))):
            return
        try:
            identity = self._identity(adapter.verify_existing(row['native_id'],
                FrozenFolder(**row['folder'])), row)
            result = adapter.execution_status(identity, execution['turnId'])
            if (isinstance(result, ExecutionResult) and result.turn_id == execution['turnId']
                    and result.outcome in ('completed', 'failed', 'needs_attention')):
                execution.update(outcome=result.outcome,
                    reason=_reason(result.code) if result.code else None)
                self._save(row)
        except Exception:
            pass

    def _refresh_managed(self, row):
        managed, execution = row.get('managed'), row.get('execution')
        if not managed:
            return
        try:
            adapter = self._adapter(row)
            snapshot = adapter.managed_status(managed['runId'])
            if (type(snapshot) is not dict or snapshot.get('runId') != managed['runId']
                    or snapshot.get('state') not in ('starting', 'ready', 'running', 'needs_attention', 'exited', 'uncertain')
                    or type(snapshot.get('recoveryRequired')) is not bool
                    or type(snapshot.get('permissionsAvailable')) is not bool):
                raise ValueError()
            row['managed'] = {key: snapshot[key] for key in ('runId', 'state', 'recoveryRequired', 'permissionsAvailable')}
            self._save(row)
            if not execution or not execution['attempts'] or execution['outcome'] in ('completed', 'failed'):
                return
            result = adapter.managed_execution_status(managed['runId'])
            if (not isinstance(result, ExecutionResult) or result.receipt_kind != 'native_user_message'
                    or result.outcome not in ('not_attempted', 'unknown', 'accepted', 'running', 'needs_attention', 'completed', 'failed')):
                return
            if result.turn_id is not None:
                if (not canonical_uuid(result.turn_id) or execution['attempts'] != 1
                        or execution['turnId'] not in (None, result.turn_id)):
                    return
                execution.update(turnId=result.turn_id, receiptKind='native_user_message')
            if result.outcome in ('accepted', 'running', 'completed') and not execution.get('turnId'):
                return
            if result.outcome == 'not_attempted':
                # A committed dispatch claim cannot become unsent because a
                # later read lacks its ledger row. Preserve uncertainty.
                return
            execution.update(outcome=result.outcome, reason=_reason(result.code) if result.code else None)
            self._save(row)
        except Exception:
            pass

    def request_status(self, request_id):
        if not canonical_uuid(request_id):
            _error('invalid_request', 'Invalid New chat identity.', 400)
        with self.lock:
            self._journal_guard()
            with self.db:
                self._gc(self._now()[0])
                previous = self.db.execute('SELECT id FROM attempts WHERE request_id=?', (request_id,)).fetchone()
                if previous is not None:
                    row = self._load(previous[0])
                    self._expire(row)
                    self._refresh_execution(row)
                    return {'schemaVersion': 1, 'requestId': request_id, 'status': 'attempted', 'attempt': self._view(row)}
                intent = self.db.execute('SELECT data, expires FROM intents WHERE id=?', (request_id,)).fetchone()
                if intent is None:
                    _error('unknown_request', 'This New chat request is unknown or expired.', 404)
                frozen = json.loads(intent[0])
                return {'schemaVersion': 1, 'requestId': request_id, 'status': 'prepared', 'attempt': None,
                        'mode': frozen.get('mode', 'blank'), 'provider': frozen['provider'],
                        'folder': public_folder(FrozenFolder(**frozen['folder'])), 'expiresAt': _iso(intent[1]),
                        'host': dict(self.host)}

    def status(self, ident):
        if not canonical_uuid(ident):
            _error('invalid_request', 'Invalid New chat identity.', 400)
        with self.lock:
            self._journal_guard()
            with self.db:
                self._gc(self._now()[0])
            with self.db:
                row = self._load(ident)
                self._expire(row)
                self._refresh_execution(row)
                return self._view(row)

    def observe(self, ident, data):
        if not canonical_uuid(ident) or type(data) is not dict or data.keys() != {'action'} or data['action'] not in ('continue', 'stop'):
            _error('invalid_request', 'Invalid observation action.', 400)
        with self.lock:
            self._journal_guard()
            self._load(ident)
            gate = self.gates.setdefault(ident, threading.RLock())
        with gate, self.lock:
            self._journal_guard()
            with self.db:
                row = self._load(ident)
                self._expire(row)
                if data['action'] == 'stop':
                    if row['state'] in ('added', 'failed', 'mismatch', 'unavailable'):
                        return self._view(row)
                    if row['observation'] != 'stopped':
                        row.update(observation='stopped', generation=row['generation'] + 1)
                else:
                    self._guard()
                    now, mono = self._now()
                    if row['state'] != 'timeout' or row['observation'] == 'stopped' or now + WINDOW > row['created'] + RETENTION:
                        _error('observation_conflict', 'This request cannot continue observation.', 409)
                    row.update(observation='active', state='waiting', window_started=now, deadline=now + WINDOW,
                               generation=row['generation'] + 1, reason=None)
                    self.deadlines[ident] = mono + WINDOW
                self._save(row)
                return self._view(row)

    def _recover(self):
        now, mono = self._now()
        with self.lock, self.db:
            self._gc(now)
            for ident, raw in self.db.execute('SELECT id, data FROM attempts WHERE expires>?', (now,)).fetchall():
                row = json.loads(raw)
                self.gates[ident] = threading.RLock()
                self.deadlines[ident] = mono + max(0, row['deadline'] - now)
                if row.get('execution'):
                    row['execution']['pending'] = False
                if row.get('execution', {}).get('attempts') and row['execution']['outcome'] in ('unknown', 'accepted', 'running'):
                    row['execution'].update(outcome='unknown', reason=_reason('execution_unknown'))
                if row['phase'] == 'claimed':
                    row['reason'] = _reason('launch_outcome_unknown')
                if row['phase'] == 'ui_claimed':
                    row.update(ui='unknown', reason=_reason('ui_open_outcome_unknown'))
                self._expire(row)
                self._save(row)
        # No mutation, metadata naming, or UI handoff is resumed here. Selected
        # identities are freshly checked by the watcher before any admission.

    def process_admissions(self, admit_callback):
        with self.lock:
            if self._journal_unavailable:
                return
            with self.db:
                if self.closing:
                    return
                now, _ = self._now()
                self._gc(now)
                if not self.enabled or self.demo:
                    return
                rows = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM attempts WHERE expires>?', (now,))]
                for row in rows:
                    self._expire(row)
                    self._refresh_execution(row)
        for row in rows:
            if row['observation'] != 'active' or not row['native_id']:
                continue
            with self.lock:
                if self._journal_unavailable:
                    return
                try:
                    folder = validate_folder(FrozenFolder(**row['folder']))
                    self.inventory.require_allowed(folder)
                    value = self._adapter(row).verify_existing(row['native_id'], folder)
                    identity = self._checked_identity(value, row)
                except Exception:
                    continue
                gate = self.gates.setdefault(row['id'], threading.RLock())
            # Stop/timeout and final admission share this gate. A stopped
            # generation cannot insert a room monitor through this service.
            with gate, self.lock:
                if self._journal_unavailable:
                    return
                with self.db:
                    current = self._load(row['id'])
                    self._expire(current)
                    if (current['observation'] != 'active' or current['generation'] != row['generation']
                            or self.closing):
                        continue
                    if not self._verified_row(current, identity):
                        self._save(current)
                        continue
                    self._save(current)
                    if not self._admission_ready(current):
                        self._save(current)
                        continue
                    publication = {'granted': False}

                    def permit():
                        # The watcher calls this after its last file/index check,
                        # immediately before its first room publication. Granting
                        # is the linearization point, not the callback's return.
                        if publication['granted']:
                            return False
                        fresh = self._load(row['id'])
                        self._expire(fresh)
                        if (fresh['observation'] != 'active' or fresh['generation'] != row['generation']
                                or not self._admission_ready(fresh) or self.closing):
                            return False
                        publication['granted'] = True
                        return True

                    try:
                        room_id = admit_callback(identity, folder, permit)
                    except Exception:
                        room_id = None
                    if not publication['granted']:
                        # permit() may have persisted expiry; never overwrite that
                        # newer row with the pre-callback snapshot.
                        continue
                    prefix = {'codex': 'x:', 'claude_code': 'c:'}.get(current['provider'])
                    if prefix is None or room_id != prefix + identity.native_id:
                        current['reason'] = _reason('admission_unavailable')
                    else:
                        current.update(identity=asdict(identity), creation='verified', room_id=room_id,
                                       state='added', observation='complete', phase='admitted', reason=None)
                    self._save(current)

    @classmethod
    def _admission_ready(cls, row):
        return (row.get('creation') == 'verified' and row.get('identity') is not None
                and (bool(row.get('managed')) and row['ui'] == 'not_required'
                     if cls._managed(row) else row['ui'] == 'accepted'))

    def close(self):
        with self.lock:
            if self.closing:
                return
            self.closing = True
            jobs = list(self.jobs.values())
        # accept holds chooser -> service lock; never reverse that order here.
        if self.folder_chooser is not None:
            self.folder_chooser.close()
        for adapter in {id(adapter): adapter for adapter in self.adapters.values()}.values():
            try:
                adapter.close()
            except Exception:
                pass
        for worker in jobs:
            if worker is not threading.current_thread() and worker.ident is not None:
                worker.join(timeout=2)
        with self.lock:
            if not self.jobs:
                self.closed = True
                self._dispose()
