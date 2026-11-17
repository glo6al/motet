"""Optional native draft receipt index; opened explicitly before serving reads."""
import hashlib
import json
import weakref

from attachments_types import AttachmentError, ID, KEY, TOKEN, canonical, destination, digest, fail, integer


VERSION_KEY = 'native_read_receipts_version'
TABLE = '''CREATE TABLE native_read_receipts (
    command_digest TEXT NOT NULL PRIMARY KEY COLLATE BINARY
        CHECK(length(command_digest)=64 AND command_digest NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK(kind IN ('draft_create','upload_reserve')),
    epoch TEXT NOT NULL CHECK(length(epoch)=64 AND epoch NOT GLOB '*[^0-9a-f]*'),
    seen_key TEXT NOT NULL UNIQUE REFERENCES seen(key) ON DELETE RESTRICT,
    target TEXT NOT NULL CHECK(length(target)=32 AND target NOT GLOB '*[^0-9a-f]*')
)'''
CLEANUP_INDEX = "CREATE INDEX native_read_pending_cleanup ON files(draft_id) WHERE state <> 'deleted'"
CLEANUP_QUERY = """SELECT EXISTS(SELECT 1 FROM files INDEXED BY native_read_pending_cleanup
                               WHERE draft_id=? AND state <> 'deleted')"""
FIND_SEEN = '''SELECT m.kind,m.epoch,m.seen_key,s.kind AS seen_kind,s.epoch AS seen_epoch,
                     m.target,s.target AS seen_target,s.fingerprint FROM native_read_receipts m
              LEFT JOIN seen s ON s.key=m.seen_key WHERE m.command_digest=?'''


def unavailable():
    fail('storage_unavailable', 'Native attachment receipts could not be verified.', 503)


def command_digest(command_key):
    if type(command_key) is not str or not KEY.fullmatch(command_key):
        unavailable()
    return hashlib.sha256(command_key.encode('ascii')).hexdigest()


def draft_binding(row):
    try:
        binding = destination(json.loads(row['destination']))
    except (AttachmentError, ValueError, TypeError):
        unavailable()
    if (binding['id'], binding['scopeId']) != (row['destination_id'], row['scope_id']) or canonical(binding) != row['destination']:
        unavailable()
    return binding


class NativeReadReceipts:
    def __init__(self, store):
        with store._gate():
            with store._transaction():
                self._install(store)
            self._bind(store)

    @staticmethod
    def _layout(store):
        version = store.db.execute('SELECT value FROM meta WHERE key=?', (VERSION_KEY,)).fetchone()
        objects = {r['name']: (r['type'], ' '.join((r['sql'] or '').split())) for r in store.db.execute('''
            SELECT name,type,sql FROM sqlite_schema WHERE name IN ('native_read_receipts','native_read_pending_cleanup')
            OR (tbl_name='native_read_receipts' AND sql IS NOT NULL)''')}
        if version is None and not objects:
            return False
        if version is None or version[0] != '1' or objects != {
                'native_read_receipts': ('table', ' '.join(TABLE.split())),
                'native_read_pending_cleanup': ('index', ' '.join(CLEANUP_INDEX.split()))}:
            unavailable()
        return True

    @classmethod
    def _install(cls, store):
        if not store.db.in_transaction:
            unavailable()
        if not cls._layout(store):
            store.db.execute(TABLE)
            store.db.execute(CLEANUP_INDEX)
            store.db.execute('INSERT INTO meta VALUES (?,?)', (VERSION_KEY, '1'))

    @classmethod
    def _open_committed(cls, store):
        instance = cls.__new__(cls)
        instance._bind(store)
        return instance

    def _bind(self, store):
        self.store = store
        self.db = store.db
        if self.db.in_transaction or not self._layout(store):
            unavailable()
        self.schema_version = self.db.execute('PRAGMA schema_version').fetchone()[0]
        self.require(store)
        if not hasattr(store, '_native_read_caches'):
            store._native_read_caches = weakref.WeakSet()
        store._native_read_caches.add(self)

    def _version(self):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (VERSION_KEY,)).fetchone()
        return row[0] if row is not None else None

    def require(self, store):
        shadowed = """SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN
                      ('meta','native_read_receipts','seen','drafts','files','uploads','discard_slots') LIMIT 1"""
        if (self.store is not store or self.db is not store.db
                or self.db.execute('PRAGMA schema_version').fetchone()[0] != self.schema_version
                or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
                or self.db.execute(shadowed).fetchone() or self._version() != '1'):
            unavailable()

    def record(self, kind, command_key, target):
        if not self.db.in_transaction:
            unavailable()
        self.db.execute('INSERT INTO native_read_receipts VALUES (?,?,?,?,?)',
                        (command_digest(command_key), kind, self.store.owner_epoch, command_key, target))

    def check_replay(self, kind, command_key):
        if self._seen(kind, command_digest(command_key)) is None:
            unavailable()

    def _seen(self, kind, digest_value):
        row = self.db.execute(FIND_SEEN, (digest_value,)).fetchone()
        if row is None or row['kind'] != kind or row['epoch'] != self.store.owner_epoch:
            return None
        seen_kind = 'draft' if kind == 'draft_create' else 'upload'
        if (row['seen_kind'] != seen_kind or row['seen_epoch'] != row['epoch']
                or command_digest(row['seen_key']) != digest_value
                or type(row['target']) is not str or not ID.fullmatch(row['target']) or row['seen_target'] != row['target']):
            unavailable()
        return row

    def _draft(self, draft_id):
        row = self.db.execute('SELECT * FROM drafts WHERE id=?', (draft_id,)).fetchone()
        if row is None:
            unavailable()
        return row

    @staticmethod
    def _matches(binding, destination_id, scope_id):
        return (binding['kind'], binding['id'], binding['scopeId']) == ('agent', destination_id, scope_id)

    def _discard(self, destination_id, scope_id, digest_value, draft_id):
        draft = self.db.execute('SELECT * FROM drafts WHERE id=?', (draft_id,)).fetchone()
        if draft is None or not self._matches(draft_binding(draft), destination_id, scope_id):
            return None
        slot = self.db.execute('SELECT * FROM discard_slots WHERE draft_id=?', (draft_id,)).fetchone()
        if slot is None or (slot['state'] == 'terminal') != (draft['state'] == 'discarded'):
            unavailable()
        if (slot['state'] != 'terminal' or slot['epoch'] != self.store.owner_epoch
                or slot['command_key'] != digest_value):
            return None
        if (not integer(slot['generation'], 2) or slot['generation'] != draft['generation']
                or self.store.admission_kind(draft) is not None
                or any(type(slot[k]) is not str or not TOKEN.fullmatch(slot[k]) for k in ('fingerprint', 'cap_hash'))):
            unavailable()
        pending = bool(self.db.execute(CLEANUP_QUERY, (draft_id,)).fetchone()[0])
        return {'draftId': draft_id, 'writerGeneration': slot['generation'], 'cleanupPending': pending}

    def _keyed(self, kind, destination_id, scope_id, digest_value, draft_id):
        row = self._seen(kind, digest_value)
        if row is None:
            return None
        if kind == 'draft_create':
            binding = draft_binding(self._draft(row['target']))
            if binding['kind'] != 'agent' or row['fingerprint'] != digest(binding):
                unavailable()
            return {'draftId': row['target']} if self._matches(binding, destination_id, scope_id) else None
        source = self.db.execute('''SELECT u.fingerprint,f.* FROM uploads u JOIN files f ON f.id=u.file_id
                                   WHERE u.id=?''', (row['target'],)).fetchone()
        if source is None or source['preparation_kind'] != 'original_relay' or not ID.fullmatch(source['id']):
            unavailable()
        metadata = {'name': source['name'], 'mediaType': source['media'], 'bytes': source['bytes'],
                    'digest': source['digest']}
        fingerprint = digest({'draft': source['draft_id'], 'metadata': metadata, 'preparationKind': 'original_relay'})
        binding = draft_binding(self._draft(source['draft_id']))
        if binding['kind'] != 'agent' or row['fingerprint'] != fingerprint or source['fingerprint'] != fingerprint:
            unavailable()
        if source['draft_id'] != draft_id or not self._matches(binding, destination_id, scope_id):
            return None
        return {'draftId': draft_id, 'uploadId': row['target'], 'fileId': source['id']}

    def lookup(self, kind, destination_id, scope_id, digest_value, draft_id):
        with self.store._gate():
            self.db.execute('BEGIN')
            try:
                self.require(self.store)
                if kind == 'draft_discard':
                    return self._discard(destination_id, scope_id, digest_value, draft_id)
                return self._keyed(kind, destination_id, scope_id, digest_value, draft_id)
            finally:
                self.db.rollback()
