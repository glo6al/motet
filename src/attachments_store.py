"""Private attachment foundation. No agent dispatch, provider or expiry worker.

Owner authentication and proof-bound destination resolution belong to the
service boundary. Recovery methods below must only be called by that boundary
following an explicit owner selection. Draft capabilities are still checked here.
"""
from contextlib import contextmanager
import base64
import hashlib
import math
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

from attachments_files import PrivateFiles
from attachments_prepare import prepare
from attachments_relay import original_relay, RELAY_FORMATS
from attachments_officer_storage import OfficerDirectRetentionProfile, prepare_original, validate_binding
from attachments_types import (AttachmentError, TOKEN, MEDIA, canonical, destination,
                               digest, fail, filename, ident, integer, key, limits, now, text, OPAQUE)

SCHEMA_VERSION = 10

# Correlated against files AS f. Protect exact committed source associations,
# never all nonmembers of a draft or an abandoned native intent.
ADMITTED_SOURCE = """EXISTS(SELECT 1 FROM drafts d WHERE d.id=f.draft_id AND (
  (d.admitted_kind='officer' AND EXISTS(
    SELECT 1 FROM officer_message_files mf JOIN officer_attachment_messages m ON m.id=mf.message_id
    WHERE mf.source_id=f.id AND m.id=d.admitted_message AND m.draft_id=d.id))
  OR (d.admitted_kind='native' AND EXISTS(
    SELECT 1 FROM request_files rf JOIN attachment_requests r ON r.id=rf.request_id
    WHERE rf.source_id=f.id AND r.id=d.admitted_request AND r.draft_id=d.id AND r.authorized_at IS NOT NULL))))"""


class AttachmentStore:
    def __init__(self, state_dir, *, enabled=False, demo=False, owner_epoch=None,
                 overrides=None, clock=time.time, monotonic=time.monotonic, checkpoint=None):
        self.enabled = enabled is True and demo is False
        self.lock, self.db, self.files = threading.RLock(), None, None
        self.clock, self.monotonic = clock, monotonic
        self.checkpoint = checkpoint or (lambda _: None)
        self.limits = limits(overrides)
        self.streams, self.request_handles, self.closed = set(), set(), False
        if not self.enabled:
            return
        if type(owner_epoch) is not str or not TOKEN.fullmatch(owner_epoch):
            fail('invalid_configuration', 'A valid attachment owner epoch is required.', 503)
        self.owner_epoch = owner_epoch
        try:
            self.files = PrivateFiles(state_dir)
            path = self.files.database()
            self.db = sqlite3.connect(path, timeout=.2, check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA secure_delete=ON')
            self.db.execute('PRAGMA temp_store=MEMORY')
            pages = max(32, self.limits['database_bytes'] // self.db.execute('PRAGMA page_size').fetchone()[0])
            self.db.execute('PRAGMA max_page_count=%d' % pages)
            self.cleanup_headroom = min(32, max(4, pages // 16))
            self._schema()
            self._recover()
        except AttachmentError:
            self.close()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.close()
            fail('storage_unavailable', 'Private attachment storage is unavailable.', 503)

    def _schema(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, SCHEMA_VERSION):
            fail('schema_unavailable', 'Attachment storage needs a compatible server.', 503)
        if version == 0:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                fail('schema_unavailable', 'Attachment storage could not be verified.', 503)
            self.db.executescript('''BEGIN IMMEDIATE;
            CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE conversations (id TEXT PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE drafts (id TEXT PRIMARY KEY,destination TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL CHECK(state IN ('active','discarded')),revision INTEGER NOT NULL,
              generation INTEGER NOT NULL,cap_hash TEXT NOT NULL,writer_hash TEXT NOT NULL,
              recovery_hash TEXT,recovery_until REAL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE files (id TEXT PRIMARY KEY,draft_id TEXT NOT NULL REFERENCES drafts(id),
              name TEXT NOT NULL,media TEXT NOT NULL,bytes INTEGER NOT NULL CHECK(bytes>=0),digest TEXT NOT NULL,
              generation INTEGER NOT NULL,state TEXT NOT NULL,identity TEXT,error TEXT,
              member INTEGER NOT NULL CHECK(member IN (0,1)),expires_at REAL DEFAULT NULL,
              expiry_generation INTEGER NOT NULL DEFAULT 0,
              needs_cleanup INTEGER NOT NULL DEFAULT 0 CHECK(needs_cleanup IN (0,1)),
              cleanup_reserve TEXT NOT NULL);
            CREATE INDEX file_draft ON files(draft_id,member,id);
            CREATE INDEX file_cleanup ON files(needs_cleanup,id);
            CREATE TABLE uploads (id TEXT PRIMARY KEY,file_id TEXT UNIQUE NOT NULL REFERENCES files(id),
              fingerprint TEXT NOT NULL,state TEXT NOT NULL,lease TEXT,writer_generation INTEGER NOT NULL,
              last_activity REAL NOT NULL);
            CREATE TABLE reservations (file_id TEXT PRIMARY KEY REFERENCES files(id),
              bytes INTEGER NOT NULL CHECK(bytes>=0),released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0,1)));
            CREATE TABLE quota (id INTEGER PRIMARY KEY CHECK(id=1),bytes INTEGER NOT NULL CHECK(bytes>=0));
            INSERT INTO quota VALUES(1,0);
            CREATE TABLE seen (key TEXT PRIMARY KEY,kind TEXT NOT NULL,fingerprint TEXT NOT NULL,
              target TEXT NOT NULL,epoch TEXT NOT NULL);
            CREATE TABLE discard_slots (draft_id TEXT PRIMARY KEY REFERENCES drafts(id),
              state TEXT NOT NULL CHECK(state IN ('reserved','terminal')),command_key TEXT,
              fingerprint TEXT,cap_hash TEXT,generation INTEGER,epoch TEXT);
            PRAGMA user_version=1;
            COMMIT;''')
        if version in (0, 1):
            with self._transaction(reclaim=True):
                self.db.execute('ALTER TABLE drafts ADD COLUMN destination_id TEXT')
                self.db.execute('ALTER TABLE drafts ADD COLUMN scope_id TEXT')
                for draft in self.db.execute('SELECT id,destination FROM drafts').fetchall():
                    binding = json.loads(draft['destination'])
                    # Never infer an old Officer draft's model. Recovery/cleanup
                    # survives; new uploads require a fresh explicitly bound draft.
                    binding.setdefault('modelId', None)
                    self.db.execute('UPDATE drafts SET destination=?,destination_id=?,scope_id=? WHERE id=?',
                                    (canonical(binding), binding['id'], binding['scopeId'], draft['id']))
                self.db.execute('CREATE INDEX draft_listing ON drafts(destination_id,scope_id,state,created,id)')
                # Discard slots are bounded by the retained command capacity.
                # Scan them rather than moving B-tree keys during emergency cleanup.
                self.db.execute('PRAGMA user_version=2')
        if version in (0, 1, 2):
            from attachments_requests import migrate
            with self._transaction(reclaim=True):
                migrate(self)
                self.db.execute('PRAGMA user_version=3')
        if version < 4:
            with self._transaction(reclaim=True):
                self.db.execute("ALTER TABLE files ADD COLUMN preparation_kind TEXT NOT NULL DEFAULT 'text' CHECK(preparation_kind IN ('text','original_relay'))")
                self.db.execute('PRAGMA user_version=4')
        if version < 5:
            from attachments_catalogue import migrate
            with self._transaction(reclaim=True):
                migrate(self)
                self.db.execute('PRAGMA user_version=5')
        if version < 6:
            from attachments_conversations import migrate
            with self._transaction(reclaim=True):
                migrate(self)
                self.db.execute('PRAGMA user_version=6')
        from attachments_deletion_slots import migrate as migrate_deletion_slots, verify_all
        if version < 7:
            with self._transaction(reclaim=True):
                migrate_deletion_slots(self)
                self.db.execute('PRAGMA user_version=7')
        else:
            verify_all(self)
        if version < 8:
            from attachments_officer_migration import migrate as migrate_officer_originals
            migrate_officer_originals(self)
        if version < 9:
            from officer_document_ledger import migrate as migrate_officer_documents
            with self._transaction(reclaim=True):
                migrate_officer_documents(self)
        if version < 10:
            from officer_document_intents import migrate as migrate_officer_intents
            with self._transaction(reclaim=True):
                migrate_officer_intents(self)
        row = self.db.execute("SELECT value FROM meta WHERE key='secret'").fetchone()
        with self._transaction(reclaim=True):
            if row is None:
                self.secret = secrets.token_bytes(32)
                self.db.execute('INSERT INTO meta VALUES (?,?)', ('secret', self.secret.hex()))
                self.db.execute('INSERT INTO meta VALUES (?,?)', ('owner_epoch', self.owner_epoch))
                self.db.execute('INSERT INTO meta VALUES (?,?)', ('cleanup_cursor', '0' * 32))
            else:
                self.secret = bytes.fromhex(row[0])
                if len(self.secret) != 32:
                    fail('storage_unavailable', 'Attachment storage could not be verified.', 503)
                epoch = self.db.execute("SELECT value FROM meta WHERE key='owner_epoch'").fetchone()
                if epoch is None:
                    fail('storage_unavailable', 'Attachment storage could not be verified.', 503)
                if epoch[0] != self.owner_epoch:
                    self.db.execute("UPDATE meta SET value=? WHERE key='owner_epoch'", (self.owner_epoch,))
                    self.db.execute("UPDATE drafts SET generation=generation+1,revision=revision+1,cap_hash='',writer_hash='',recovery_hash=NULL,recovery_until=NULL")

    @contextmanager
    def _transaction(self, *, reclaim=False):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.files.verify_database()
            if not reclaim:
                pages = self.db.execute('PRAGMA page_count').fetchone()[0]
                free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
                maximum = self.db.execute('PRAGMA max_page_count').fetchone()[0]
                if maximum - pages + free < self.cleanup_headroom:
                    fail('history_capacity', 'Attachment metadata needs room for safe cleanup.', 429)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def _gate(self):
        with self.lock:
            if not self.enabled or self.closed or self.db is None:
                fail('attachments_disabled', 'Attachments are unavailable.', 503)
            try:
                self.files.verify_database()
                yield
            except AttachmentError:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
                fail('storage_unavailable', 'Attachment storage could not be safely used.', 503)

    def _recover(self):
        with self._gate():
            receiving = self.db.execute("SELECT f.id FROM files f WHERE (f.state='receiving' OR EXISTS(SELECT 1 FROM uploads u WHERE u.file_id=f.id AND u.state='receiving')) AND " + ADMITTED_SOURCE).fetchone()
            if receiving is not None:
                fail('storage_unavailable', 'An admitted attachment has inconsistent upload state.', 503)
            with self._transaction(reclaim=True):
                self.db.execute("UPDATE uploads SET state='interrupted',lease=NULL WHERE state='receiving'")
                self.db.execute("UPDATE files SET state='failed',error='upload_interrupted' WHERE state='receiving'")
            from attachments_requests import recover
            with self._transaction(reclaim=True):
                recover(self)
            self._reconcile_batch(100)

    def _hash(self, token):
        if type(token) is not str or not TOKEN.fullmatch(token):
            return ''
        return hashlib.sha256(token.encode()).hexdigest()

    def _token(self, kind, draft_id, generation):
        return hmac.new(self.secret, canonical([self.owner_epoch, kind, draft_id, generation]).encode(), hashlib.sha256).hexdigest()

    def _draft(self, draft_id):
        ident(draft_id)
        row = self.db.execute('SELECT * FROM drafts WHERE id=?', (draft_id,)).fetchone()
        if row is None:
            fail('forbidden', 'The draft could not be authorized.', 403)
        return row

    def admission_kind(self, row):
        kind = row['admitted_kind']
        valid = ((kind is None and row['admitted_request'] is None and row['admitted_message'] is None)
                 or (kind == 'native' and row['admitted_request'] is not None and row['admitted_message'] is None)
                 or (kind == 'officer' and row['admitted_message'] is not None and row['admitted_request'] is None))
        if not valid:
            fail('storage_unavailable', 'The attachment admission record is inconsistent.', 503)
        return kind

    def _writable(self, row):
        if self.admission_kind(row) is not None:
            fail('draft_admitted', 'This draft belongs to a sent attachment request.')

    def _authorize(self, draft_id, capability, *, fence=None, active=True):
        row = self._draft(draft_id)
        hashed = self._hash(capability)
        if not hashed or not row['cap_hash'] or not hmac.compare_digest(hashed, row['cap_hash']):
            fail('forbidden', 'The draft could not be authorized.', 403)
        if active and row['state'] != 'active':
            fail('draft_discarded', 'This draft was discarded.')
        if fence is not None:
            self._writable(row)
            if (type(fence) is not dict or set(fence) != {'revision', 'writerGeneration', 'writerNonce'}
                    or not integer(fence['revision'], 1) or not integer(fence['writerGeneration'], 1)
                    or fence['revision'] != row['revision'] or fence['writerGeneration'] != row['generation']
                    or not self._hash(fence['writerNonce'])
                    or not hmac.compare_digest(self._hash(fence['writerNonce']), row['writer_hash'])):
                fail('draft_conflict', 'This draft changed in another window.')
        return row

    def _seen(self, command_key, kind, fingerprint):
        key(command_key)
        command_digest = hashlib.sha256(command_key.encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM discard_slots WHERE command_key=? AND state='terminal' LIMIT 1", (command_digest,)).fetchone():
            fail('request_conflict', 'This operation identifier was already used differently.')
        row = self.db.execute('SELECT * FROM seen WHERE key=?', (command_key,)).fetchone()
        if row is not None and (row['kind'] != kind or row['fingerprint'] != fingerprint or row['epoch'] != self.owner_epoch):
            fail('request_conflict', 'This operation identifier was already used differently.')
        return row

    def _capacity(self, extra=1):
        n = self.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0]
        n += self.db.execute('SELECT COUNT(*) FROM discard_slots').fetchone()[0]
        if n + extra > self.limits['seen']:
            fail('history_capacity', 'Attachment history has reached its capacity.', 429)

    def _credentials(self, row):
        return {'capability': self._token('draft', row['id'], row['generation']),
                'writerNonce': self._token('writer', row['id'], row['generation'])}

    def _snapshot(self, row):
        values = []
        for f in self.db.execute('SELECT * FROM files WHERE draft_id=? AND member=1 ORDER BY rowid', (row['id'],)):
            values.append({'id': f['id'], 'name': f['name'], 'mediaType': f['media'], 'bytes': f['bytes'],
                           'state': f['state'], 'error': f['error'], 'sourceGeneration': f['generation'], 'expiresAt': None,
                           'preparationKind': f['preparation_kind'], 'previewAvailable': f['preparation_kind'] == 'text' and f['state'] == 'ready'})
        return {'id': row['id'], 'destination': json.loads(row['destination']), 'text': row['text'],
                'state': row['state'], 'revision': row['revision'], 'writerGeneration': row['generation'],
                'createdAt': row['created'], 'updatedAt': row['updated'], 'files': values, 'expiresAt': None}

    def current_conversation(self, scope_id, *, create=False):
        """Exact page mapping; a read never creates or guesses a conversation."""
        if not text(scope_id, 160) or not scope_id or any(c in scope_id for c in '/\\\x00'):
            fail('invalid_request', 'Invalid Motet scope.', 400)
        command = self._token('page-document-conversation', scope_id, 1)
        with self._gate():
            prior = self._seen(command, 'conversation', digest({'scopeId': scope_id}))
            if prior is not None:
                row = self.db.execute('SELECT scope FROM conversations WHERE id=?', (prior['target'],)).fetchone()
                if row is None or row['scope'] != scope_id:
                    fail('conversation_unavailable', 'The Motet conversation could not be restored.', 503)
                return {'id': prior['target'], 'scopeId': scope_id}
            return self.create_conversation(scope_id, command) if create else None

    def create_conversation(self, scope_id, command_key):
        if not text(scope_id, 160) or not scope_id or any(c in scope_id for c in '/\\\x00'):
            fail('invalid_request', 'Invalid Motet scope.', 400)
        fingerprint = digest({'scopeId': scope_id})
        with self._gate(), self._transaction():
            prior = self._seen(command_key, 'conversation', fingerprint)
            if prior:
                return {'id': prior['target'], 'scopeId': scope_id}
            if self.db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] >= self.limits['conversations']:
                fail('conversation_capacity', 'Attachment conversations have reached capacity.', 429)
            self._capacity()
            cid = secrets.token_hex(16)
            self.db.execute('INSERT INTO conversations VALUES(?,?,?)', (cid, scope_id, now(self.clock)))
            self.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)', (command_key, 'conversation', fingerprint, cid, self.owner_epoch))
            return {'id': cid, 'scopeId': scope_id}

    def create_draft(self, binding, command_key, *, native_receipts=None):
        binding = destination(binding)
        fingerprint = digest(binding)
        with self._gate(), self._transaction():
            if native_receipts is not None:
                if binding['kind'] != 'agent':
                    fail('destination_unavailable', 'Choose an exact native attachment destination.', 503)
                native_receipts.require(self)
            prior = self._seen(command_key, 'draft', fingerprint)
            if prior:
                if native_receipts is not None:
                    native_receipts.check_replay('draft_create', command_key)
                row = self._draft(prior['target'])
                out = self._snapshot(row)
                if row['generation'] == 1 and row['state'] == 'active' and self.admission_kind(row) is None:
                    out.update(self._credentials(row))
                else:
                    out['authorityChanged'] = True
                return out
            if binding['kind'] == 'officer':
                c = self.db.execute('SELECT scope FROM conversations WHERE id=?', (binding['conversationId'],)).fetchone()
                if c is None or c[0] != binding['scopeId']:
                    fail('forbidden', 'The Motet conversation could not be authorized.', 403)
            if self.db.execute("SELECT COUNT(*) FROM drafts WHERE state='active' AND admitted_kind IS NULL").fetchone()[0] >= self.limits['drafts']:
                fail('draft_capacity', 'Discard a saved draft before creating another.', 429)
            self._capacity(2)  # creation identity plus permanently reserved discard receipt
            did, ts = secrets.token_hex(16), now(self.clock)
            cap, writer = self._token('draft', did, 1), self._token('writer', did, 1)
            self.db.execute('INSERT INTO drafts(id,destination,state,revision,generation,cap_hash,writer_hash,created,updated) VALUES(?,?,?,1,1,?,?,?,?)',
                            (did, canonical(binding), 'active', self._hash(cap), self._hash(writer), ts, ts))
            self.db.execute('UPDATE drafts SET destination_id=?,scope_id=? WHERE id=?', (binding['id'], binding['scopeId'], did))
            self.db.execute('INSERT INTO discard_slots VALUES(?,?,?,?,?,?,?)',
                            (did, 'reserved', self._token('discard-reserve', did, 1), '0' * 64, '0' * 64, 2**53 - 1, '0' * 64))
            self.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)', (command_key, 'draft', fingerprint, did, self.owner_epoch))
            if native_receipts is not None:
                native_receipts.record('draft_create', command_key, did)
            row = self._draft(did)
            return dict(self._snapshot(row), **self._credentials(row))

    def get_draft(self, draft_id, capability):
        with self._gate():
            return self._snapshot(self._authorize(draft_id, capability))

    def recover_draft(self, draft_id, expected_generation):
        """Requires explicit authenticated owner selection at the service boundary."""
        with self._gate(), self._transaction():
            row = self._draft(draft_id)
            self._writable(row)
            if row['state'] != 'active' or not integer(expected_generation, 1) or row['generation'] != expected_generation:
                fail('draft_conflict', 'This draft cannot be recovered at that generation.')
            cap = secrets.token_hex(32)
            self.db.execute('UPDATE drafts SET recovery_hash=?,recovery_until=? WHERE id=?',
                            (self._hash(cap), now(self.clock) + 300, draft_id))
            return {'id': draft_id, 'revision': row['revision'], 'writerGeneration': row['generation'], 'recoveryCapability': cap}

    def resume_draft(self, draft_id, capability, revision, generation):
        with self._gate(), self._transaction():
            row = self._draft(draft_id)
            self._writable(row)
            hashed = self._hash(capability)
            allowed = bool(hashed and (hmac.compare_digest(hashed, row['cap_hash']) or
                           row['recovery_hash'] and row['recovery_until'] >= now(self.clock)
                           and hmac.compare_digest(hashed, row['recovery_hash'])))
            if not allowed:
                fail('forbidden', 'The draft could not be authorized.', 403)
            if row['state'] != 'active' or not integer(revision, 1) or not integer(generation, 1) or (row['revision'], row['generation']) != (revision, generation):
                fail('draft_conflict', 'This draft changed in another window.')
            g = generation + 1
            self.db.execute('UPDATE drafts SET generation=?,revision=revision+1,cap_hash=?,writer_hash=?,recovery_hash=NULL,recovery_until=NULL,updated=? WHERE id=?',
                            (g, self._hash(self._token('draft', draft_id, g)), self._hash(self._token('writer', draft_id, g)), now(self.clock), draft_id))
            row = self._draft(draft_id)
            return dict(self._snapshot(row), **self._credentials(row))

    def edit_text(self, draft_id, capability, fence, value):
        if not text(value, self.limits['text_chars']):
            fail('text_limit', 'The attachment message exceeds its text limit.', 413)
        with self._gate(), self._transaction():
            row = self._authorize(draft_id, capability, fence=fence)
            maximum = min(self.limits['text_chars'], 2000 if json.loads(row['destination'])['kind'] == 'agent' else 4000)
            if len(value) > maximum:
                fail('text_limit', 'The attachment message exceeds its text limit.', 413)
            self.db.execute('UPDATE drafts SET text=?,revision=revision+1,updated=? WHERE id=?', (value, now(self.clock), draft_id))
            return self._snapshot(self._draft(draft_id))

    def discard_draft(self, draft_id, capability, fence, command_key):
        key(command_key)
        fingerprint = digest({'id': draft_id, 'fence': fence})
        command_digest = hashlib.sha256(command_key.encode()).hexdigest()
        with self._gate():
            with self._transaction(reclaim=True):
                row = self._draft(draft_id)
                slot = self.db.execute('SELECT * FROM discard_slots WHERE draft_id=?', (draft_id,)).fetchone()
                if self.db.execute('SELECT 1 FROM seen WHERE key=?', (command_key,)).fetchone():
                    fail('request_conflict', 'This operation identifier was already used differently.')
                other = self.db.execute("SELECT draft_id FROM discard_slots WHERE command_key=? AND state='terminal' AND draft_id<>? LIMIT 1",
                                        (command_digest, draft_id)).fetchone()
                if other is not None:
                    fail('request_conflict', 'This operation identifier was already used differently.')
                if slot['state'] == 'terminal':
                    if (slot['epoch'] != self.owner_epoch or slot['command_key'] != command_digest or slot['fingerprint'] != fingerprint
                            or not self._hash(capability) or not hmac.compare_digest(slot['cap_hash'], self._hash(capability))):
                        fail('request_conflict', 'This discard identifier was already used differently.')
                else:
                    self._authorize(draft_id, capability, fence=fence)
                    self.db.execute("UPDATE discard_slots SET state='terminal',command_key=?,fingerprint=?,cap_hash=?,generation=?,epoch=? WHERE draft_id=?",
                                    (command_digest, fingerprint, row['cap_hash'], row['generation'] + 1, self.owner_epoch, draft_id))
                    self.db.execute("UPDATE drafts SET state='discarded',revision=revision+1,generation=generation+1,cap_hash='',writer_hash='',recovery_hash=NULL,recovery_until=NULL,updated=? WHERE id=?", (now(self.clock), draft_id))
                    self.db.execute("UPDATE files SET member=0,generation=generation+1,state='cleanup_pending',needs_cleanup=1,cleanup_reserve=substr(cleanup_reserve,1,128) WHERE draft_id=? AND state<>'deleted'", (draft_id,))
            self.checkpoint('discard_committed')
            for f in self.db.execute('SELECT id FROM files WHERE draft_id=?', (draft_id,)).fetchall():
                self._cleanup(f['id'])
            return self._discard_view(draft_id)

    def _discard_view(self, draft_id):
        rows = self.db.execute('SELECT state FROM files WHERE draft_id=?', (draft_id,)).fetchall()
        return {'id': draft_id, 'state': 'discarded', 'writerGeneration': self._draft(draft_id)['generation'],
                'cleanupPending': any(r[0] != 'deleted' for r in rows)}

    def reserve_upload(self, draft_id, capability, fence, metadata, command_key, *, admission_limits=None,
                       preparation_kind='text', officer_profile=None, native_receipts=None):
        if type(metadata) is not dict or set(metadata) != {'name', 'mediaType', 'bytes', 'digest'}:
            fail('invalid_request', 'Invalid upload metadata.', 400)
        if preparation_kind not in ('text', 'original_relay', 'officer_direct') or type(preparation_kind) is not str:
            fail('preparation_unavailable', 'This attachment preparation mode is unavailable.', 503)
        if preparation_kind == 'officer_direct':
            if type(officer_profile) is not OfficerDirectRetentionProfile:
                fail('profile_unavailable', 'Motet original-file retention needs an exact profile.', 503)
        elif officer_profile is not None:
            fail('profile_unavailable', 'The retention profile does not match this preparation.', 503)
        metadata = dict(metadata, name=filename(metadata['name']))
        if type(metadata['mediaType']) is not str or metadata['mediaType'] not in MEDIA:
            fail('unsupported_type', 'This attachment type is not supported.', 415)
        if not integer(metadata['bytes'], 0, self.limits['file_bytes']):
            fail('file_too_large', 'The attachment exceeds the file limit.', 413)
        if type(metadata['digest']) is not str or not TOKEN.fullmatch(metadata['digest']):
            fail('invalid_request', 'Use a SHA-256 upload digest.', 400)
        admission = {'fileBytes': self.limits['file_bytes'], 'draftBytes': self.limits['draft_bytes'], 'fileCount': self.limits['files']}
        if admission_limits is not None:
            if (type(admission_limits) is not dict or set(admission_limits) != set(admission)
                    or any(not integer(v, 1, admission[k]) for k, v in admission_limits.items())):
                fail('invalid_request', 'Invalid attachment admission limits.', 400)
            admission = dict(admission_limits)
        fingerprint_data = {'draft': draft_id, 'metadata': metadata}
        # Preserve legacy text-command identity; relay must never replay as text.
        if preparation_kind != 'text':
            fingerprint_data['preparationKind'] = preparation_kind
        if preparation_kind == 'officer_direct':
            from dataclasses import asdict
            fingerprint_data['officerProfile'] = asdict(officer_profile)
        fingerprint = digest(fingerprint_data)
        with self._gate(), self._transaction():
            if native_receipts is not None:
                if preparation_kind != 'original_relay':
                    fail('preparation_unavailable', 'Original-file relay is required.', 503)
                native_receipts.require(self)
            self._writable(self._authorize(draft_id, capability))
            binding = json.loads(self._authorize(draft_id, capability)['destination'])
            if preparation_kind == 'original_relay' and (binding['kind'] != 'agent' or metadata['mediaType'] not in RELAY_FORMATS):
                fail('preparation_unavailable', 'Original-file relay is available only to a qualified agent.', 503)
            if preparation_kind == 'officer_direct':
                validate_binding(officer_profile, binding)
                if metadata['mediaType'] not in officer_profile.media_types:
                    fail('preparation_unavailable', 'This original-file type is unavailable to Motet.', 503)
                for key, value in (('fileBytes', officer_profile.file_bytes), ('draftBytes', officer_profile.draft_bytes), ('fileCount', officer_profile.file_count)):
                    admission[key] = min(admission[key], value)
            prior = self._seen(command_key, 'upload', fingerprint)
            if prior:
                if native_receipts is not None:
                    native_receipts.check_replay('upload_reserve', command_key)
                return self._upload_view(prior['target'])
            row = self._authorize(draft_id, capability, fence=fence)
            stored_binding = json.loads(row['destination'])
            if stored_binding['kind'] == 'officer' and stored_binding.get('modelId') is None:
                fail('model_binding_unavailable', 'Create a new draft with an explicit Motet model.', 503)
            count, total = self.db.execute('SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM files WHERE draft_id=? AND member=1', (draft_id,)).fetchone()
            if (metadata['bytes'] > admission['fileBytes'] or count >= admission['fileCount']
                    or total + metadata['bytes'] > admission['draftBytes']):
                fail('draft_limit', 'This draft exceeds its attachment count or byte limit.', 413)
            if self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0] >= self.limits['objects']:
                fail('object_capacity', 'Attachment objects have reached capacity.', 429)
            if self.db.execute("SELECT COUNT(*) FROM uploads WHERE state IN ('reserved','receiving','interrupted')").fetchone()[0] >= self.limits['uploads']:
                fail('upload_capacity', 'Too many uploads are waiting.', 429)
            used = self.db.execute('SELECT bytes FROM quota WHERE id=1').fetchone()[0]
            if used + metadata['bytes'] > self.limits['bytes']:
                fail('storage_capacity', 'Attachment storage has no room for this file.', 429)
            if self.files.free_bytes() < metadata['bytes']:
                fail('disk_capacity', 'This computer has insufficient free storage.', 429)
            self._capacity()
            fid, uid, ts = secrets.token_hex(16), secrets.token_hex(16), now(self.clock)
            self.db.execute('INSERT INTO files(id,draft_id,name,media,bytes,digest,generation,state,member,cleanup_reserve,preparation_kind) VALUES(?,?,?,?,?,?,1,?,1,?,?)',
                            (fid, draft_id, metadata['name'], metadata['mediaType'], metadata['bytes'], metadata['digest'], 'uploading', '0' * 256, preparation_kind))
            from attachments_deletion_slots import allocate_source
            allocate_source(self, fid)
            self.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?)', (uid, fid, fingerprint, 'reserved', None, row['generation'], ts))
            self.db.execute('INSERT INTO reservations(file_id,bytes) VALUES(?,?)', (fid, metadata['bytes']))
            self.db.execute('UPDATE quota SET bytes=bytes+? WHERE id=1', (metadata['bytes'],))
            self.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)', (command_key, 'upload', fingerprint, uid, self.owner_epoch))
            if native_receipts is not None:
                native_receipts.record('upload_reserve', command_key, uid)
            self.db.execute('UPDATE drafts SET revision=revision+1,updated=? WHERE id=?', (ts, draft_id))
            return self._upload_view(uid)

    def _upload(self, upload_id):
        ident(upload_id)
        row = self.db.execute('SELECT u.*,f.draft_id FROM uploads u JOIN files f ON f.id=u.file_id WHERE u.id=?', (upload_id,)).fetchone()
        if row is None:
            fail('forbidden', 'The upload could not be authorized.', 403)
        return row

    def _upload_view(self, upload_id):
        u = self._upload(upload_id)
        f = self.db.execute('SELECT * FROM files WHERE id=?', (u['file_id'],)).fetchone()
        return {'id': u['id'], 'fileId': f['id'], 'fingerprint': u['fingerprint'], 'state': f['state'],
                'error': f['error'], 'sourceGeneration': f['generation'], 'draftRevision': self._draft(f['draft_id'])['revision'],
                'expiresAt': None, 'preparationKind': f['preparation_kind'],
                'previewAvailable': f['preparation_kind'] == 'text' and f['state'] == 'ready'}

    def get_upload(self, draft_id, capability, upload_id):
        with self._gate():
            self._authorize(draft_id, capability)
            u = self._upload(upload_id)
            if u['draft_id'] != draft_id:
                fail('forbidden', 'The upload could not be authorized.', 403)
            return self._upload_view(upload_id)

    def _current_stream(self, draft_id, generation, file_id, lease):
        d = self._draft(draft_id)
        u = self.db.execute('SELECT lease FROM uploads WHERE file_id=?', (file_id,)).fetchone()
        f = self.db.execute('SELECT member FROM files WHERE id=?', (file_id,)).fetchone()
        return d['state'] == 'active' and self.admission_kind(d) is None and d['generation'] == generation and f['member'] == 1 and u['lease'] == lease

    def receive_upload(self, draft_id, capability, fence, upload_id, fingerprint, reader):
        """Consume a bounded binary stream; caller enforces network socket deadlines too."""
        fd, fid, lease, created_identity = None, None, None, None
        with self._gate():
            self._writable(self._authorize(draft_id, capability))
            u = self._upload(upload_id)
            if u['draft_id'] != draft_id or fingerprint != u['fingerprint']:
                fail('request_conflict', 'The upload identity does not match.')
            f = self.db.execute('SELECT * FROM files WHERE id=?', (u['file_id'],)).fetchone()
            if u['state'] == 'complete':
                return self._upload_view(upload_id)
            d = self._authorize(draft_id, capability, fence=fence)
            if u['lease'] is not None or len(self.streams) >= self.limits['streams']:
                fail('upload_busy', 'This upload is already being written or uploads are busy.', 429)
            if not f['member']:
                fail('file_removed', 'This file was removed from the draft.')
            if f['identity'] is not None:
                current = self._owned_partial(f)
                if current is None or self.files.remove(f['id'], current) not in ('removed', 'absent'):
                    fail('source_changed', 'The incomplete upload could not be safely replaced.')
            fid, lease, generation = f['id'], secrets.token_hex(16), d['generation']
            with self._transaction():
                self.db.execute("UPDATE uploads SET state='receiving',lease=?,writer_generation=?,last_activity=? WHERE id=?", (lease, generation, now(self.clock), upload_id))
                self.db.execute("UPDATE files SET state='receiving',identity=NULL,error=NULL WHERE id=?", (fid,))
            self.streams.add(fid)
            try:
                self.checkpoint('upload_claimed')
                fd = self.files.create(fid)
                created_identity = self.files.private_fd(fd)
                with self._transaction():
                    self.db.execute('UPDATE files SET identity=? WHERE id=?', (canonical(created_identity), fid))
            except BaseException:
                self.streams.discard(fid)
                if fd is not None:
                    os.close(fd)
                with self._transaction():
                    self.db.execute("UPDATE uploads SET state='interrupted',lease=NULL WHERE id=? AND lease=?", (upload_id, lease))
                    self.db.execute("UPDATE files SET state='failed',error='upload_failed' WHERE id=?", (fid,))
                raise
        error = None
        try:
            deadline = now(self.monotonic) + 30
            received, hasher = 0, hashlib.sha256()
            while True:
                if now(self.monotonic) > deadline:
                    fail('upload_timeout', 'This upload timed out.', 422)
                with self._gate():
                    if not self._current_stream(draft_id, generation, fid, lease):
                        fail('draft_conflict', 'This upload no longer belongs to the active draft.')
                chunk = reader.read(min(65536, f['bytes'] - received + 1))
                if not isinstance(chunk, bytes):
                    fail('invalid_upload', 'The upload body is invalid.', 400)
                if not chunk:
                    break
                received += len(chunk)
                if received > f['bytes']:
                    fail('upload_size', 'The upload does not match its reserved size.', 422)
                hasher.update(chunk)
                offset = 0
                while offset < len(chunk):
                    written = os.write(fd, chunk[offset:])
                    if written <= 0:
                        raise OSError()
                    offset += written
            if received != f['bytes'] or hasher.hexdigest() != f['digest']:
                fail('upload_digest', 'The upload does not match its reserved content.', 422)
            os.fsync(fd)
            final_identity = self.files.private_fd(fd)
            if final_identity[:2] != created_identity[:2]:
                fail('source_changed', 'The upload identity changed.')
            self.checkpoint('upload_written')
            # A bounded re-read validates actual stored bytes, not just received chunks.
            data = self.files.read(fid, final_identity, f['digest'], self.limits['file_bytes'])
            try:
                self._prepare_source(data, f)
                result_state, result_error = 'ready', None
            except AttachmentError as exc:
                result_state, result_error = 'failed', exc.code
            with self._gate(), self._transaction():
                if not self._current_stream(draft_id, generation, fid, lease):
                    fail('draft_conflict', 'This upload no longer belongs to the active draft.')
                self.db.execute('UPDATE files SET state=?,identity=?,error=? WHERE id=?', (result_state, canonical(final_identity), result_error, fid))
                self.db.execute("UPDATE uploads SET state='complete',lease=NULL,last_activity=? WHERE id=?", (now(self.clock), upload_id))
                self.db.execute('UPDATE drafts SET revision=revision+1,updated=? WHERE id=?', (now(self.clock), draft_id))
                return self._upload_view(upload_id)
        except AttachmentError as exc:
            error = exc
            raise
        except (OSError, ValueError, TypeError):
            error = AttachmentError('upload_failed', 'The upload could not be completed.', 422)
            raise error from None
        finally:
            if fd is not None:
                os.close(fd)
            with self._gate():
                self.streams.discard(fid)
                with self._transaction():
                    if error is not None:
                        self.db.execute("UPDATE uploads SET state='interrupted',lease=NULL WHERE id=? AND lease=?", (upload_id, lease))
                        self.db.execute("UPDATE files SET state=CASE WHEN member=0 THEN 'cleanup_pending' ELSE 'failed' END,error=? WHERE id=? AND state='receiving'", (error.code, fid))
                self._cleanup(fid)

    def _owned_partial(self, f):
        if f['identity'] is None:
            try:
                self.files.object_stat(f['id'])
            except FileNotFoundError:
                return None
            fail('source_changed', 'An incomplete attachment identity is unknown.')
        expected = json.loads(f['identity'])
        try:
            current = self.files.object_identity(f['id'])
        except FileNotFoundError:
            return None
        if current[:2] != expected[:2] or current[2] > f['bytes']:
            fail('source_changed', 'The private attachment identity changed.')
        return current

    def _source_admitted(self, file_id):
        return self.db.execute('SELECT 1 FROM files f WHERE f.id=? AND ' + ADMITTED_SOURCE, (file_id,)).fetchone() is not None

    def _cleanup(self, file_id):
        f = self.db.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
        if f is None or f['member'] or f['state'] == 'deleted' or file_id in self.streams or self._source_admitted(file_id):
            return
        try:
            current = self._owned_partial(f)
            if current is None:
                # Absence also needs the descriptor-owner admission guard: an
                # earlier unlink may still have an uncertain open descriptor.
                outcome = self.files.remove(file_id, None)
            else:
                # Completed content requires the full recorded identity/digest. Partial
                # streams own only the pinned inode, never a verified source.
                u = self.db.execute('SELECT state FROM uploads WHERE file_id=?', (file_id,)).fetchone()
                expected = json.loads(f['identity']) if u['state'] == 'complete' else current
                outcome = self.files.remove(file_id, expected, f['digest'] if u['state'] == 'complete' else None)
        except (AttachmentError, OSError):
            outcome = 'changed'
        if outcome == 'busy':
            return
        with self._transaction(reclaim=True):
            if outcome in ('removed', 'absent'):
                reservation = self.db.execute('SELECT * FROM reservations WHERE file_id=?', (file_id,)).fetchone()
                if not reservation['released']:
                    self.db.execute('UPDATE quota SET bytes=bytes-? WHERE id=1', (reservation['bytes'],))
                    self.db.execute('UPDATE reservations SET released=1 WHERE file_id=?', (file_id,))
                self.db.execute("UPDATE files SET state='deleted',identity=NULL,error=NULL,needs_cleanup=0,cleanup_reserve='' WHERE id=?", (file_id,))
                self.db.execute("UPDATE uploads SET state='removed',lease=NULL WHERE file_id=?", (file_id,))
            else:
                self.db.execute("UPDATE files SET state='cleanup_failed',error='identity_changed',cleanup_reserve='' WHERE id=?", (file_id,))

    def remove_file(self, draft_id, capability, fence, file_id):
        ident(file_id)
        with self._gate():
            with self._transaction(reclaim=True):
                self._authorize(draft_id, capability, fence=fence)
                f = self.db.execute('SELECT * FROM files WHERE id=? AND draft_id=? AND member=1', (file_id, draft_id)).fetchone()
                if f is None:
                    fail('forbidden', 'The file could not be authorized.', 403)
                self.db.execute("UPDATE files SET member=0,generation=generation+1,state='cleanup_pending',needs_cleanup=1,cleanup_reserve=substr(cleanup_reserve,1,128) WHERE id=?", (file_id,))
                self.db.execute('UPDATE drafts SET revision=revision+1,updated=? WHERE id=?', (now(self.clock), draft_id))
            self._cleanup(file_id)
            return self._snapshot(self._draft(draft_id))

    def _prepare_source(self, data, row):
        if row['preparation_kind'] == 'officer_direct':
            return prepare_original(data, row['media'], max_bytes=self.limits['file_bytes'])
        if row['preparation_kind'] == 'original_relay':
            return original_relay(data, row['media'], max_bytes=self.limits['file_bytes'])
        if row['preparation_kind'] != 'text':
            fail('preparation_unavailable', 'This attachment preparation mode is unavailable.', 503)
        return prepare(data, row['media'], max_bytes=self.limits['file_bytes'], max_lines=self.limits['lines'])

    def retry_preparation(self, draft_id, capability, fence, file_id):
        with self._gate():
            self._authorize(draft_id, capability, fence=fence)
            f = self.db.execute('SELECT * FROM files WHERE id=? AND draft_id=? AND member=1', (ident(file_id), draft_id)).fetchone()
            if f is None or f['state'] != 'failed' or f['identity'] is None:
                fail('preparation_unavailable', 'This file cannot be prepared again.', 409)
            data = self.files.read(file_id, json.loads(f['identity']), f['digest'], self.limits['file_bytes'])
            try:
                self._prepare_source(data, f)
                state, error = 'ready', None
            except AttachmentError as exc:
                state, error = 'failed', exc.code
            with self._transaction():
                self.db.execute('UPDATE files SET state=?,error=? WHERE id=?', (state, error, file_id))
                self.db.execute('UPDATE drafts SET revision=revision+1,updated=? WHERE id=?', (now(self.clock), draft_id))
            return self._snapshot(self._draft(draft_id))

    def read_text(self, draft_id, capability, file_id):
        with self._gate():
            self._writable(self._authorize(draft_id, capability))
            f = self.db.execute('SELECT * FROM files WHERE id=? AND draft_id=? AND member=1', (ident(file_id), draft_id)).fetchone()
            if f is None or f['state'] != 'ready':
                fail('source_unavailable', 'This attachment is not available.', 409)
            if f['preparation_kind'] != 'text':
                fail('preview_unavailable', 'No preview is available for this original file.', 409)
            data = self.files.read(file_id, json.loads(f['identity']), f['digest'], self.limits['file_bytes'])
            return dict(self._prepare_source(data, f),
                        fileId=file_id, sourceGeneration=f['generation'])

    def limits_view(self):
        with self._gate():
            return {'storageBytes': self.limits['bytes'], 'fileBytes': self.limits['file_bytes'],
                    'draftBytes': self.limits['draft_bytes'], 'fileCount': self.limits['files'],
                    'agentTextCharacters': min(2000, self.limits['text_chars']),
                    'officerTextCharacters': min(4000, self.limits['text_chars']),
                    'preparationFormats': ['text/plain', 'text/markdown']}

    def _summary(self, row):
        count, size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM files WHERE draft_id=? AND member=1', (row['id'],)).fetchone()
        return {'id': row['id'], 'destination': json.loads(row['destination']), 'state': row['state'],
                'revision': row['revision'], 'writerGeneration': row['generation'],
                'createdAt': row['created'], 'updatedAt': row['updated'],
                'fileCount': count, 'totalBytes': size, 'expiresAt': None}

    def draft_summary(self, draft_id):
        """Owner-only metadata selection; never grants a writer capability."""
        with self._gate():
            return self._summary(self._draft(draft_id))

    def _cursor(self, filters, last):
        data = canonical({'v': 1, 'epoch': self.owner_epoch, 'filters': filters, 'last': last}).encode()
        tag = hmac.new(self.secret, b'attachment-draft-cursor\0' + data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(data + tag).decode().rstrip('=')

    def _parse_cursor(self, token, filters):
        try:
            if type(token) is not str or not 1 <= len(token) <= 2048 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in token):
                raise ValueError()
            raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
            if len(raw) < 33:
                raise ValueError()
            data, tag = raw[:-32], raw[-32:]
            expected = hmac.new(self.secret, b'attachment-draft-cursor\0' + data, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise ValueError()
            value = json.loads(data)
            if (type(value) is not dict or set(value) != {'v', 'epoch', 'filters', 'last'}
                    or value['v'] != 1 or type(value['v']) is not int
                    or value['epoch'] != self.owner_epoch or value['filters'] != filters
                    or type(value['last']) is not list or len(value['last']) != 2
                    or type(value['last'][0]) not in (int, float) or not math.isfinite(value['last'][0])
                    or value['last'][0] < 0):
                raise ValueError()
            ident(value['last'][1])
            if self._cursor(filters, value['last']) != token:
                raise ValueError()
            return value['last']
        except (ValueError, TypeError, UnicodeError, AttachmentError):
            fail('invalid_cursor', 'This draft list cursor is invalid.', 400)

    def list_drafts(self, *, destination_id, scope_id, cursor=None):
        for value in (destination_id, scope_id):
            if type(value) is not str or not OPAQUE.fullmatch(value):
                fail('invalid_request', 'Choose an exact draft destination and scope.', 400)
        with self._gate():
            filters = [destination_id, scope_id]
            last = self._parse_cursor(cursor, filters) if cursor is not None else [-1, '0' * 32]
            rows = self.db.execute("SELECT * FROM drafts WHERE destination_id=? AND scope_id=? AND state='active' AND admitted_kind IS NULL AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT 51",
                                   (destination_id, scope_id, last[0], last[0], last[1])).fetchall()
            items = [self._summary(row) for row in rows[:50]]
            next_cursor = self._cursor(filters, [rows[49]['created'], rows[49]['id']]) if len(rows) > 50 else None
            return {'items': items, 'nextCursor': next_cursor}

    def upload_transport_info(self, draft_id, capability, upload_id):
        with self._gate():
            self._authorize(draft_id, capability)
            upload = self._upload(upload_id)
            if upload['draft_id'] != draft_id:
                fail('forbidden', 'The upload could not be authorized.', 403)
            file = self.db.execute('SELECT bytes FROM files WHERE id=?', (upload['file_id'],)).fetchone()
            return {'id': upload['id'], 'bytes': file['bytes'], 'fingerprint': upload['fingerprint'], 'state': upload['state']}

    def storage(self):
        """Authenticated owner-only projection at the service boundary."""
        with self._gate():
            return {'usedBytes': self.db.execute('SELECT bytes FROM quota WHERE id=1').fetchone()[0],
                    'limitBytes': self.limits['bytes'],
                    'activeDrafts': self.db.execute("SELECT COUNT(*) FROM drafts WHERE state='active' AND admitted_kind IS NULL").fetchone()[0],
                    'cleanupPending': self.db.execute('SELECT COUNT(*) FROM files WHERE member=0 AND state<>?', ('deleted',)).fetchone()[0]}

    def reconcile(self, maximum=100):
        if not integer(maximum, 1, 100):
            fail('invalid_request', 'Invalid reconciliation bound.', 400)
        with self._gate():
            self._reconcile_batch(maximum)
            return self.storage()

    def _reconcile_batch(self, maximum):
        cursor = self.db.execute("SELECT value FROM meta WHERE key='cleanup_cursor'").fetchone()[0]
        rows = self.db.execute('SELECT f.id FROM files f WHERE needs_cleanup=1 AND NOT (' + ADMITTED_SOURCE + ') AND f.id>? ORDER BY f.id LIMIT ?',
                               (cursor, maximum)).fetchall()
        if not rows:
            rows = self.db.execute('SELECT f.id FROM files f WHERE needs_cleanup=1 AND NOT (' + ADMITTED_SOURCE + ') ORDER BY f.id LIMIT ?',
                                   (maximum,)).fetchall()
        for row in rows:
            self._cleanup(row['id'])
            # Persist progress even when one identity cannot be removed. A failed
            # copy must not monopolize bounded reconciliation or every restart.
            with self._transaction(reclaim=True):
                self.db.execute("UPDATE meta SET value=? WHERE key='cleanup_cursor'", (row['id'],))

    def close(self):
        worker = getattr(self, '_deletion_worker', None)
        if worker is not None:
            # A refused direct close must leave cleanup service available. Do
            # not retire its worker while a provider/preview still owns bytes.
            with self.lock:
                if self.streams or self.request_handles or self.files.io_active():
                    fail('upload_busy', 'Attachment uploads or handles are still active.', 409)
            worker.close()
            self._deletion_worker = None
        with self.lock:
            if self.streams or self.request_handles:
                fail('upload_busy', 'Attachment uploads are still active.', 409)
            if self.files is not None:
                self.files.begin_close()
            if self.db is not None:
                self.db.close()
                self.db = None
            if self.files is not None:
                self.files.close()
                self.files = None
            self.closed = True
