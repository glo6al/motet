"""Unmounted retained native deletion for an explicitly controlled disposable fixture."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading

from attachments_catalogue import Catalogue, HistorySelection
from attachments_deletion_slots import (
    COPY_PREVIEW, COPY_RECEIPT, SOURCE_PREVIEW, SOURCE_RECEIPT, ZERO_HASH, ZERO_ID,
    verify_copy, verify_source,
)
from attachments_files import private
from attachments_materialize import (
    Materializer, RetainedCopyCleanup, WorkspaceContext, WorkspaceDirectories, copy_identity, FileIdentity,
    relative_path,
)
from attachments_relay import RELAY_FORMATS
from attachments_requests import (
    CopyCleanup, NATIVE_DELETION_COHORT, RequestAuthority, RequestLedger, TransportProfile, file_identity,
)
from attachments_resources import ResourceLocks
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_types import AttachmentError, TOKEN, canonical, destination, digest, fail, ident, integer, key, now

INDEX_SOURCE = 'native_delete_copy_source'
INDEX_REQUEST = 'native_delete_copy_request'
INDEXES = {
    INDEX_SOURCE: 'CREATE INDEX native_delete_copy_source ON workspace_copies(source_id,id)',
    INDEX_REQUEST: 'CREATE INDEX native_delete_copy_request ON workspace_copies(request_id,id)',
}
PROTECTED_TABLES = (
    'meta', 'files', 'reservations', 'quota', 'drafts', 'request_files', 'attachment_requests', 'attachment_outbox',
    'workspace_copies', 'workspace_roots', 'deletion_source_slots', 'deletion_copy_slots',
)
MAX_KEYS, MAX_MEMBERS, PAGE, TTL = 16, 5, 50, 120_000
EMPTY, PREVIEW, PENDING, REMOVED, CHANGED, UNKNOWN = range(6)
FULL, PARTIAL, NEVER, UNRESOLVED = range(1, 5)
NATIVE = 2
PIECES = {PENDING: 'pending', REMOVED: 'removed', CHANGED: 'changed', UNKNOWN: 'unknown'}
REASONS = (None, 'source_in_use', 'identity_changed', 'removal_unconfirmed', 'workspace_unavailable',
           'workspace_unverified')
_LAUNCHER = object()


def _hold(code='deletion_changed', status=409):
    fail(code, 'The exact native attachment deletion could not be verified.', status)


def _layout_error():
    _hold('deletion_storage_unavailable', 503)


def _need(value):
    if not value:
        _hold()


def _digest_key(value):
    key(value)
    return hashlib.sha256(value.encode()).hexdigest()


def _path_ids(root):
    # The launcher supplies this path independently of the DB marker.
    paths = (root, root / 'attachments', root / 'attachments' / 'ledger.sqlite3',
             root / 'attachments' / 'writer.lock', root / 'attachments' / 'objects')
    return tuple(tuple(private(path.lstat(), directory=index in (0, 1, 4))[:2])
                 for index, path in enumerate(paths))


@dataclass(frozen=True)
class _LauncherBinding:
    root: str
    cohort: str
    identities: tuple
    owner_epoch: str
    base_objects: tuple
    base_fingerprint: str

    @property
    def marker(self):
        return canonical({'v': 1, 'cohort': self.cohort, 'identities': self.identities})


@dataclass(frozen=True)
class _AllocationCohort:
    store: AttachmentStore
    binding: _LauncherBinding


def _marker(db):
    row = db.execute('SELECT value FROM main.meta WHERE key=?', (NATIVE_DELETION_COHORT,)).fetchone()
    return row[0] if row else None


def _base_schema(db):
    return tuple(tuple(row) for row in db.execute(
        'SELECT name,type,tbl_name,sql FROM main.sqlite_schema ORDER BY name'))


def _verify_base(db, binding):
    if db.execute('PRAGMA main.user_version').fetchone()[0] != SCHEMA_VERSION:
        _layout_error()
    rows = _base_schema(db)
    base_names = frozenset(binding.base_objects)
    base = tuple(row for row in rows if row[0] in base_names)
    if (len(base) != len(base_names) or digest(base) != binding.base_fingerprint
            or any(name not in base_names and kind == 'trigger' and target.lower() in PROTECTED_TABLES
                   for name, kind, target, _ in rows)):
        _layout_error()
    meta = dict(db.execute("""SELECT key,value FROM main.meta
        WHERE key IN ('owner_epoch','secret','cleanup_cursor','deletion_cursor')"""))
    if (meta.get('owner_epoch') != binding.owner_epoch
            or type(meta.get('secret')) is not str or not TOKEN.fullmatch(meta['secret'])
            or any(type(meta.get(name)) is not str or len(meta[name]) != len(ZERO_ID)
                   or any(ch not in '0123456789abcdef' for ch in meta[name])
                   for name in ('cleanup_cursor', 'deletion_cursor'))):
        _layout_error()


def _layout(db, expected):
    tables = ','.join('?' for _ in PROTECTED_TABLES)
    names = tables + ',?,?'
    if db.execute(f'''SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN ({names})
        OR (type='trigger' AND tbl_name COLLATE NOCASE IN ({tables})) LIMIT 1''',
        (*PROTECTED_TABLES, *INDEXES, *PROTECTED_TABLES)).fetchone():
        _layout_error()
    found = {name: (kind, sql) for name, kind, sql in db.execute(
        'SELECT name,type,sql FROM main.sqlite_schema WHERE name IN (?,?)', tuple(INDEXES))}
    version = _marker(db)
    if expected is None:
        if found or version is not None:
            _layout_error()
    elif version != expected or found != {k: ('index', v) for k, v in INDEXES.items()}:
        _layout_error()


def _retained_layout(root, binding):
    # Refuse lost witnesses without running the ordinary writable store open.
    # This temporary read cannot qualify an installation or reserve its lock.
    # AttachmentStore must still acquire and recheck the actual lifetime lock.
    import fcntl

    attachment_root = root / 'attachments'
    lock = os.open(attachment_root / 'writer.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if tuple(private(os.fstat(lock))[:2]) != binding.identities[3] or _path_ids(root) != binding.identities:
            _layout_error()
        for sidecar in ('ledger.sqlite3-journal', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm'):
            try:
                private((attachment_root / sidecar).lstat())
            except FileNotFoundError:
                pass
        db = sqlite3.connect((attachment_root / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=0.2)
        try:
            _layout(db, binding.marker)
            _verify_base(db, binding)
        finally:
            db.close()
    finally:
        os.close(lock)


def _parse_marker(raw):
    if type(raw) is not str or len(raw) > 1024:
        _layout_error()
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'v', 'cohort', 'identities'} or type(value['v']) is not int
            or value['v'] != 1 or type(value['cohort']) is not str or not TOKEN.fullmatch(value['cohort'])
            or type(value['identities']) is not list or len(value['identities']) != 5
            or any(type(pair) is not list or len(pair) != 2 or any(not integer(n, 0, 2**63 - 1) for n in pair)
                   for pair in value['identities']) or canonical(value) != raw):
        _layout_error()
    return value['cohort']


def _tag(store, cohort, request, copy):
    value = [cohort, request['id'], request['fingerprint'], request['epoch'], copy['source_id'], copy['id'],
             copy['role'], copy['bytes'], copy['digest']]
    return hmac.new(store.secret, b'watch-native-deletion-cohort-copy-v1\0' + canonical(value).encode(),
                    hashlib.sha256).digest()[:16].hex()


def _stamp_allocations(store, cohort, request_id, copy_id):
    if type(cohort) is not _AllocationCohort or cohort.store is not store or not store.db.in_transaction:
        _layout_error()
    _layout(store.db, cohort.binding.marker)
    request = store.db.execute('SELECT * FROM attachment_requests WHERE id=?', (request_id,)).fetchone()
    copy = store.db.execute('SELECT * FROM workspace_copies WHERE id=?', (copy_id,)).fetchone()
    _need(request is not None and copy is not None and copy['request_id'] == request_id
          and request['epoch'] == store.owner_epoch and copy['role'] == 'workspace' and copy['state'] == 'planned')
    verify_copy(store, copy_id)
    row = store.db.execute('SELECT * FROM deletion_copy_slots WHERE copy_id=?', (copy_id,)).fetchone()
    values = COPY_PREVIEW.decode(row['preview']), COPY_RECEIPT.decode(row['receipt'])
    tag = _tag(store, cohort.binding.cohort, request, copy)
    _need(tag != ZERO_ID and all(v['anchor_id'] == ZERO_ID and not v['state'] for v in values))
    store.db.execute('UPDATE deletion_copy_slots SET preview=?,receipt=? WHERE copy_id=?',
                     (COPY_PREVIEW.encode(dict(values[0], anchor_id=tag)),
                      COPY_RECEIPT.encode(dict(values[1], anchor_id=tag)), copy_id))


def _exclude_confirmed_native(store, request_id):
    """Called only by the existing saved-cleanup writer on a provisioned store."""
    if not store.db.in_transaction:
        _layout_error()
    raw = _marker(store.db)
    cohort = _parse_marker(raw)
    _layout(store.db, raw)
    members = store.db.execute('SELECT source_id FROM request_files WHERE request_id=? LIMIT ?',
                               (request_id, MAX_MEMBERS + 1)).fetchall()
    _need(1 <= len(members) <= MAX_MEMBERS)
    for member in members:
        source_id = member['source_id']
        slot_id = verify_source(store, source_id)
        row = store.db.execute('SELECT receipt FROM deletion_source_slots WHERE slot_id=?', (slot_id,)).fetchone()
        value = SOURCE_RECEIPT.decode(row['receipt'])
        if value['origin_kind'] != NATIVE or not value['state']:
            continue
        _need(value['state'] in PIECES)
        actual = store.db.execute('''SELECT f.access_generation,f.deletion_generation,r.*,d.admitted_kind
                                   FROM files f JOIN drafts d ON d.id=f.draft_id
                                   JOIN attachment_requests r ON r.id=d.admitted_request WHERE f.id=?''', (source_id,)).fetchone()
        _need(actual is not None and actual['admitted_kind'] == 'native' and actual['id'] == value['origin_id']
              and actual['authorized_at'] is not None and actual['epoch'] == value['owner_epoch']
              and actual['access_generation'] == value['fenced_access_generation']
              and actual['deletion_generation'] == value['fenced_deletion_generation']
              and value['operation_id'] == slot_id and value['copy_count'] == 1)
        copy_id = value['copy_ids'][0]
        verify_copy(store, copy_id)
        copy = store.db.execute('SELECT * FROM workspace_copies WHERE id=?', (copy_id,)).fetchone()
        saved = COPY_RECEIPT.decode(store.db.execute(
            'SELECT receipt FROM deletion_copy_slots WHERE copy_id=?', (copy_id,)).fetchone()[0])
        _need(copy['source_id'] == source_id and copy['request_id'] == actual['id']
              and saved['operation_id'] == slot_id and saved['state'] in PIECES
              and saved['source_slot_id'] == slot_id
              and saved['fenced_deletion_generation'] == copy['deletion_generation']
              and hmac.compare_digest(saved['anchor_id'], _tag(store, cohort, actual, copy)))
        _hold('cleanup_owned')


class _Lifetime:
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.open = True

    @contextmanager
    def operation(self):
        with self.condition:
            if not self.open:
                _hold('attachments_disabled', 503)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def drain(self, timeout):
        with self.condition:
            self.open = False
            if not self.condition.wait_for(lambda: self.active == 0, timeout):
                _hold('deletion_busy')


def _selection(value):
    if type(value) is not HistorySelection or value.kind != 'agent' or value.destination_id == 'officer':
        _hold('forbidden', 403)
    return value


def _binding(raw):
    value = destination(json.loads(raw))
    _need(value['kind'] == 'agent' and raw == canonical(value))
    return value


class DisposableNativeDeletion:
    """Exclusive fixture launcher. Existing opens require the retained launcher binding."""

    @classmethod
    def create(cls, root, owner_epoch, *, resolve=None, checkpoint=None, overrides=None, clock=None):
        root = Path(root).absolute()
        root = root.parent.resolve(strict=True) / root.name
        root.mkdir(mode=0o700)  # Exclusive: an empty query on an old store is insufficient.
        options = {} if clock is None else {'clock': clock}
        store = AttachmentStore(root, enabled=True, owner_epoch=owner_epoch, checkpoint=checkpoint,
                                overrides=overrides, **options)
        try:
            with store._gate(), store._transaction():
                _layout(store.db, None)
                if getattr(store, '_native_read_caches', None):
                    _layout_error()
                for table in ('attachment_requests', 'request_files', 'workspace_copies'):
                    if store.db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                        _layout_error()
                base = _base_schema(store.db)
                binding = _LauncherBinding(str(root), secrets.token_hex(32), _path_ids(root), owner_epoch,
                                           tuple(row[0] for row in base), digest(base))
                for ddl in INDEXES.values():
                    store.db.execute(ddl)
                store.db.execute('INSERT INTO meta VALUES (?,?)', (NATIVE_DELETION_COHORT, binding.marker))
            return cls(_LAUNCHER, store, binding, resolve)
        except BaseException:
            store.close()
            raise

    @classmethod
    def reopen(cls, binding, owner_epoch, *, resolve=None, checkpoint=None, overrides=None, clock=None):
        if type(binding) is not _LauncherBinding or owner_epoch != binding.owner_epoch:
            _layout_error()
        root = Path(binding.root)
        try:
            if str(root.resolve(strict=True)) != binding.root or _path_ids(root) != binding.identities:
                _layout_error()
            _retained_layout(root, binding)
        except (OSError, sqlite3.Error, ImportError):
            _layout_error()
        options = {} if clock is None else {'clock': clock}
        store = AttachmentStore(root, enabled=True, owner_epoch=owner_epoch, checkpoint=checkpoint,
                                overrides=overrides, **options)
        try:
            return cls(_LAUNCHER, store, binding, resolve)
        except BaseException:
            store.close()
            raise

    def __init__(self, launcher, store, binding, resolve):
        if launcher is not _LAUNCHER or type(binding) is not _LauncherBinding or type(store) is not AttachmentStore:
            _layout_error()
        with store._gate():
            _layout(store.db, binding.marker)
            _verify_base(store.db, binding)
            _need(_path_ids(Path(binding.root)) == binding.identities and str(store.files.parent) == binding.root)
        if resolve is not None and not callable(resolve):
            _layout_error()
        self._store, self._binding, self._resolve = store, binding, resolve
        self._lifetime = _Lifetime()
        self._requests = RequestLedger(store, _deletion_cohort=_AllocationCohort(store, binding))
        self._locks = ResourceLocks(str(store.files.root), enabled=True)
        self._writer = Materializer(enabled=True, workspace_files=store.files, validate_authority=self._validate_write,
                                    record_directory=self._requests.record_directory_created,
                                    record_created=self._requests.record_copy_created,
                                    record_partial=self._requests.record_partial, checkpoint=store.checkpoint)
        self._bound_writer = self._writer
        self.deletion = NativeDeletion(self, enabled=True)

    @property
    def binding(self):
        return self._binding

    def _verified_writer(self):
        _need(self._writer is self._bound_writer and type(self._writer) is Materializer
              and self._writer._workspace_files is self._store.files)
        return self._writer

    def _validate_write(self, authority, phase, copies):
        self._verified_writer()
        with self._store._gate():
            _layout(self._store.db, self._binding.marker)
            if self._requests.authority(authority.request_id) != authority or phase == 'cleanup':
                return False
            self._enrolled_request(authority.request_id)
            return True

    def _enrolled_request(self, request_id):
        request = self._requests._request(request_id)
        copies = _bounded(self._store, 'SELECT * FROM workspace_copies INDEXED BY ' + INDEX_REQUEST +
                          ' WHERE request_id=? LIMIT ?', (request_id,), MAX_MEMBERS)
        _need(copies)
        for copy in copies:
            _copy_slots(self._store, self._binding.cohort, request, copy)

    def create_draft(self, binding, command_key):
        with self._lifetime.operation():
            return self._store.create_draft(_binding(canonical(binding)), command_key)

    def edit_text(self, draft_id, capability, fence, question):
        with self._lifetime.operation():
            return self._store.edit_text(draft_id, capability, fence, question)

    def reserve_upload(self, draft_id, capability, fence, metadata, command_key):
        with self._lifetime.operation():
            return self._store.reserve_upload(draft_id, capability, fence, metadata, command_key,
                                              preparation_kind='original_relay')

    def receive_upload(self, draft_id, capability, fence, upload_id, fingerprint, reader):
        with self._lifetime.operation():
            return self._store.receive_upload(draft_id, capability, fence, upload_id, fingerprint, reader)

    def prepare_request(self, draft_id, capability, fence, command_key, binding, profile):
        with self._lifetime.operation():
            self._verified_writer()
            _need(type(profile) is TransportProfile and not profile.temporary_copy and profile.original_relay_formats)
            prepared = self._requests.prepare_request(draft_id, capability, fence, command_key, binding, profile)
            with self._store._gate():
                self._enrolled_request(prepared['id'])
            return prepared

    def authorize_send(self, request_id, capability, revision, fingerprint, *, binding, profile):
        with self._lifetime.operation():
            self._verified_writer()
            with self._store._gate():
                self._enrolled_request(request_id)
            with self._locks.hold(self._requests.resource_keys(request_id)) as lease:
                return self._requests.authorize_send(request_id, capability, revision, fingerprint,
                    resolved_binding=binding, transport_profile=profile, resource_lease=lease)

    def _context(self, authority):
        value = self._resolve(authority) if self._resolve is not None else None
        if value is not None and (type(value) is not WorkspaceContext
                or value.fingerprint != authority.context_fingerprint
                or value.workspace_fingerprint != authority.workspace_fingerprint):
            _hold('copy_changed')
        return value

    def materialize(self, request_id, copy_id):
        """The fixture's workspace write route uses the exact D0 installation."""
        with self._lifetime.operation():
            writer = self._verified_writer()
            with self._locks.hold(self._requests.resource_keys(request_id)) as lease:
                authority = self._requests.authority(request_id)
                context = self._context(authority)
                if context is None:
                    _hold('proof_unavailable', 503)
                dirs = writer.prepare_directories(context, authority, lease,
                    directories=WorkspaceDirectories.from_record(self._requests.workspace_directories(request_id)))
                spec = self._requests.claim_copy(request_id, authority.operation_revision, copy_id, lease)
                authority = self._requests.authority(request_id)
                writer.ensure_exclusion(context, authority, (spec,), lease)
                with self._requests.source_handle(copy_id, lease) as fd:
                    retained = copy_identity(FileIdentity.of(os.fstat(fd)))
                    saved = writer.materialize(context, authority, spec, fd, retained, lease, directories=dirs)
                self._requests.record_copy_result(saved, lease)
                return saved

    def close(self, *, timeout=35):
        self._lifetime.drain(timeout)
        self._store.close()
        self._locks.close()


def _bounded(store, prefix, params, maximum):
    rows = store.db.execute(prefix, (*params, maximum + 1)).fetchall()
    if len(rows) > maximum:
        _hold('deletion_roster_unavailable', 503)
    return rows


def _copy_slots(store, cohort, request, copy):
    _need(copy['request_id'] == request['id'])
    verify_copy(store, copy['id'])
    raw = store.db.execute('SELECT * FROM deletion_copy_slots WHERE copy_id=?', (copy['id'],)).fetchone()
    values = COPY_PREVIEW.decode(raw['preview']), COPY_RECEIPT.decode(raw['receipt'])
    tag = _tag(store, cohort, request, copy)
    _need(tag != ZERO_ID and all(hmac.compare_digest(value['anchor_id'], tag) for value in values))
    return values


@dataclass(frozen=True)
class _Lineage:
    source: object
    reservation: object
    request: object
    member: object
    copy: object
    selection: HistorySelection
    profile: dict
    directories: dict
    keys: tuple
    extras: tuple
    slot: str

    def parent(self, component):
        directories = self.directories if component in (FULL, PARTIAL) else None
        return digest({'context': self.profile['context_fingerprint'], 'workspace': self.profile['workspace_fingerprint'],
                       'directories': directories})


def _no_creation(copy, member, *, charged):
    return (copy['state'] == ('reserved' if charged else 'planned') and copy['generation'] == 1
            and copy['deletion_generation'] == 0 and copy['lease'] == ZERO_HASH and json.loads(copy['identity']) is None
            and not copy['partial_digest'].strip() and copy['partial_bytes'] == 2**53 - 1
            and copy['charged'] == int(charged) and not copy['released'] and member['catalogued_at'] is None)


def _copy_shape(copy, member):
    retained = json.loads(copy['identity'])
    if copy['state'] == 'available' and member['catalogued_at'] is not None:
        _need(copy['lease'] != ZERO_HASH and retained is not None and not copy['partial_digest'].strip())
        identity = file_identity(tuple(retained))
        _need(identity[2] == copy['bytes'])
        return FULL, identity
    if copy['state'] == 'partial':
        _need(copy['lease'] != ZERO_HASH and retained is not None and member['catalogued_at'] is None
              and TOKEN.fullmatch(copy['partial_digest']) and integer(copy['partial_bytes'], 0, copy['bytes']))
        identity = file_identity(tuple(retained))
        _need(identity[2] == copy['partial_bytes'])
        return PARTIAL, identity
    if _no_creation(copy, member, charged=True):
        return NEVER, None
    _need(copy['state'] == 'writing' and copy['lease'] != ZERO_HASH and member['catalogued_at'] is None)
    return UNRESOLVED, file_identity(tuple(retained)) if retained is not None else None


class NativeDeletion:
    """Exact retained deletion. Read methods never perform physical work."""

    def __init__(self, fixture=None, *, enabled=False):
        self._fixture = fixture if enabled is True and type(fixture) is DisposableNativeDeletion else None
        if enabled is True and self._fixture is None:
            _layout_error()
        if self._fixture is not None:
            self._store = fixture._store
            self._catalogue = Catalogue(self._store)
            self._attempted = set()

    @contextmanager
    def _operation(self):
        if self._fixture is None:
            _hold('attachments_disabled', 503)
        with self._fixture._lifetime.operation():
            self._fixture._verified_writer()
            yield

    def _layout(self):
        _layout(self._store.db, self._fixture.binding.marker)

    def _lineage(self, source_id, selection=None):
        self._layout()
        store, db = self._store, self._store.db
        source = db.execute('SELECT * FROM files WHERE id=?', (ident(source_id),)).fetchone()
        if source is None:
            _hold('forbidden', 403)
        draft = store._draft(source['draft_id'])
        if store.admission_kind(draft) != 'native' or source['preparation_kind'] != 'original_relay':
            _hold('forbidden', 403)
        owner_id = draft['admitted_request']
        scope = HistorySelection.from_binding(_binding(draft['destination']))
        _selection(scope)
        if selection is not None and _selection(selection) != scope:
            _hold('forbidden', 403)
        reservation = db.execute('SELECT * FROM reservations WHERE file_id=?', (source_id,)).fetchone()
        _need(source['media'] in RELAY_FORMATS and source['member'] == 1 and source['identity'] is not None
              and TOKEN.fullmatch(source['digest']) and reservation is not None
              and reservation['bytes'] == source['bytes'] and reservation['released'] in (0, 1))
        file_identity(tuple(json.loads(source['identity'])))
        slot = verify_source(store, source_id)

        refs = _bounded(store, 'SELECT * FROM request_files INDEXED BY request_source WHERE source_id=? LIMIT ?',
                        (source_id,), MAX_KEYS)
        reverse = _bounded(store, 'SELECT id FROM workspace_copies INDEXED BY ' + INDEX_SOURCE +
                           ' WHERE source_id=? LIMIT ?', (source_id,), MAX_KEYS)
        by_request = {row['request_id']: row for row in refs}
        _need(owner_id in by_request and len(by_request) == len(refs)
              and {row['id'] for row in reverse} == {row['workspace_id'] for row in refs})
        keys, extras, admitted = set(), [], None
        for rid, member in sorted(by_request.items()):
            row = self._fixture._requests._request(rid)
            outbox = db.execute('SELECT * FROM attachment_outbox WHERE request_id=?', (rid,)).fetchone()
            profile = json.loads(row['profile'])
            _need(row['draft_id'] == draft['id'] and row['destination'] == draft['destination']
                  and row['epoch'] == store.owner_epoch and row['cleanup_generation'] == 0
                  and row['stage'] not in ('cleanup', 'cleaned') and outbox is not None
                  and outbox['owner_request_id'] == row['owner_request_id'] and type(profile) is dict
                  and profile.get('temporary_copy') is False and type(profile.get('original_relay_formats')) is list
                  and source['media'] in profile['original_relay_formats'] and type(profile.get('workspace_fingerprint')) is str
                  and type(profile.get('context_fingerprint')) is str and TOKEN.fullmatch(profile['workspace_fingerprint'])
                  and TOKEN.fullmatch(profile['context_fingerprint']))
            _need(member['source_generation'] == source['generation'] and member['temporary_id'] is None
                  and all(member[k] == source[k] for k in ('identity', 'digest', 'name', 'media', 'bytes', 'preparation_kind')))
            all_members = _bounded(store, 'SELECT * FROM request_files WHERE request_id=? LIMIT ?', (rid,), MAX_MEMBERS)
            related = _bounded(store, 'SELECT * FROM workspace_copies INDEXED BY ' + INDEX_REQUEST +
                               ' WHERE request_id=? LIMIT ?', (rid,), MAX_MEMBERS)
            declared = {m['workspace_id']: m for m in all_members}
            _need(len(declared) == len(all_members) == len(related)
                  and len({m['source_id'] for m in all_members}) == len(all_members)
                  and not any(m['temporary_id'] is not None for m in all_members))
            keys.update(('request:' + rid, 'workspace:' + profile['workspace_fingerprint'][:32]))
            for copy in related:
                link = declared.get(copy['id'])
                _need(link is not None and link['source_id'] == copy['source_id'] and copy['role'] == 'workspace'
                      and copy['relative_path'] == relative_path(rid, copy['id'], link['media']))
                copy_preview, copy_receipt = _copy_slots(store, self._fixture.binding.cohort, row, copy)
                keys.update(('source:' + copy['source_id'], 'copy:' + copy['id']))
                if rid != owner_id:
                    _need(row['stage'] == 'intent' and row['authorized_at'] is None and outbox['claim_id'] == ZERO_HASH
                          and outbox['state'] == 'prepared' and outbox['claimed_at'] is None
                          and not outbox['owner_sequence'] and not outbox['retention_pin'] and not outbox['accepted']
                          and _no_creation(copy, link, charged=False) and not copy_preview['state'] and not copy_receipt['state']
                          and not any(json.loads(value) is not None for value in (row['request_directory'],))
                          and copy['access_generation'] == link['access_generation'])
                    extras.append((dict(link), dict(copy), copy_preview['anchor_id']))
                elif copy['id'] == member['workspace_id']:
                    _need(row['authorized_at'] is not None and copy['charged'] == 1 and copy['released'] in (0, 1))
                    admitted = row, member, copy, profile
            if len(keys) > MAX_KEYS:
                _hold('deletion_roster_unavailable', 503)
        _need(admitted is not None)
        row, member, copy, profile = admitted
        dirs = self._fixture._requests.workspace_directories(owner_id)
        WorkspaceDirectories.from_record(dirs)
        # Include the unadmitted request identity and every proven preallocated
        # copy; no mutable owner outcome belongs in this snapshot.
        extra_rows = tuple((rid, by_request[rid]['access_generation'],
                   tuple(dict(self._fixture._requests._request(rid))[field] for field in
                         ('fingerprint', 'epoch', 'destination', 'profile', 'authorized_at', 'stage')),
                   tuple(item for item in sorted(extras, key=lambda item: item[1]['id']) if item[1]['request_id'] == rid))
                   for rid in sorted(by_request) if rid != owner_id)
        return _Lineage(source, reservation, row, member, copy, scope, profile, dirs, tuple(sorted(keys)), extra_rows, slot)

    @staticmethod
    def _origin(lineage, frozen):
        s, r = lineage.source, lineage.request
        return digest({'v': 1, 'source': {k: s[k] for k in ('id', 'draft_id', 'generation', 'identity', 'name',
                       'bytes', 'digest', 'media', 'preparation_kind', 'member')},
                       'request': {k: r[k] for k in ('id', 'owner_request_id', 'fingerprint', 'epoch', 'destination', 'profile')},
                       'member': dict(lineage.member), 'keys': lineage.keys, 'extras': lineage.extras,
                       'copy': {k: frozen[k] for k in ('anchor_id', 'copy_id', 'source_generation', 'copy_generation',
                                'access_generation', 'deletion_generation', 'identity_present', 'identity', 'lease',
                                'bytes', 'digest', 'partial_bytes', 'partial_digest', 'component_result', 'parent_fingerprint')}})

    @staticmethod
    def _fingerprint(value):
        return digest([value[k] for k in ('slot_id', 'source_id', 'origin_id', 'owner_epoch', 'context_fingerprint',
                                        'origin_fingerprint', 'preview_generation', 'prepared_ms', 'expires_ms', 'prepare_key_digest')])

    def _copy_preview(self, lineage, initial, generation, timestamp):
        state, identity = _copy_shape(lineage.copy, lineage.member)
        c = lineage.copy
        return dict(initial, state=PREVIEW, cause=1, operation_id=lineage.slot,
                    preview_generation=generation, operation_generation=generation,
                    source_generation=lineage.source['generation'], copy_generation=c['generation'],
                    access_generation=c['access_generation'], deletion_generation=c['deletion_generation'],
                    identity=identity if identity is not None else (0,) * 5, identity_present=int(identity is not None),
                    lease=c['lease'], partial_digest=c['partial_digest'] if state == PARTIAL else ZERO_HASH,
                    partial_bytes=c['partial_bytes'] if state == PARTIAL else 0,
                    charged=c['charged'], parent_fingerprint=lineage.parent(state), prepared_ms=timestamp,
                    component_result=state)

    def _load(self, source_id, selection=None):
        ln = self._lineage(source_id, selection)
        db = self._store.db
        raw = db.execute('SELECT * FROM deletion_source_slots WHERE slot_id=?', (ln.slot,)).fetchone()
        p, r = SOURCE_PREVIEW.decode(raw['preview']), SOURCE_RECEIPT.decode(raw['receipt'])
        cp, cr = _copy_slots(self._store, self._fixture.binding.cohort, ln.request, ln.copy)
        s, c = ln.source, ln.copy
        _need(p['state'] in (EMPTY, PREVIEW) and (not r['state'] or r['state'] in PIECES)
              and bool(p['state']) == bool(cp['state']) and bool(r['state']) == bool(cr['state']))
        if not p['state']:
            _need(not r['state'] and s['state'] == 'ready' and not s['deletion_generation']
                  and not ln.reservation['released'] and not c['released'] and not c['deletion_generation']
                  and s['access_generation'] == ln.member['access_generation'] == c['access_generation'])
            _copy_shape(c, ln.member)
            return ln, p, r, cp, cr
        _need(p['state'] == PREVIEW and cp['state'] == PREVIEW and p['origin_kind'] == NATIVE
              and p['cause'] == cp['cause'] == 1 and p['origin_id'] == ln.request['id']
              and p['owner_epoch'] == self._store.owner_epoch and p['context_fingerprint'] == digest(asdict(ln.selection))
              and p['operation_id'] == cp['operation_id'] == ln.slot and p['roster_count'] == p['copy_count'] == 1
              and p['roster_ids'][0] == s['id'] and p['roster_generations'][0] == s['generation']
              and p['copy_ids'][0] == c['id'] and p['identity_present'] and p['identity'] == tuple(json.loads(s['identity']))
              and p['source_generation'] == cp['source_generation'] == s['generation']
              and p['access_generation'] == cp['access_generation'] == ln.member['access_generation']
              and integer(p['preview_generation'], 1) and p['preview_generation'] == cp['preview_generation']
              and p['operation_generation'] == cp['operation_generation'] == p['preview_generation']
              and p['selection_fingerprint'] == cp['selection_fingerprint'] == self._fingerprint(p)
              and hmac.compare_digest(p['capability_digest'], self._store._hash(self._capability(p))))
        if not r['state']:
            _need(s['state'] == 'ready' and not ln.reservation['released'] and not c['released']
                  and s['deletion_generation'] == p['deletion_generation'] == c['deletion_generation'] == 0
                  and s['access_generation'] == p['access_generation'] == c['access_generation'])
        else:
            _need(cp['parent_fingerprint'] == ln.parent(cp['component_result'])
                  and self._origin(ln, cp) == p['origin_fingerprint'])
            self._validate_receipts(ln, p, r, cp, cr)
        return ln, p, r, cp, cr

    @staticmethod
    def _validate_receipts(ln, p, r, cp, cr):
        s, c = ln.source, ln.copy
        smutable = {'state','released','component_result','handles_closed','operation_generation','intent_ms',
                    'removed_ms','released_bytes','confirm_key_digest','fenced_source_generation',
                    'fenced_access_generation','fenced_deletion_generation'}
        cmutable = {'state','released','component_result','handles_closed','operation_generation','intent_ms',
                    'removed_ms','released_bytes','fenced_copy_generation','fenced_access_generation','fenced_deletion_generation'}
        _need(all(r[k] == p[k] for k in p if k not in smutable)
              and all(cr[k] == cp[k] for k in cp if k not in cmutable)
              and r['state'] in PIECES and cr['state'] in PIECES
              and integer(r['operation_generation'], 1)
              and r['operation_generation'] >= cr['operation_generation'] > p['preview_generation']
              and r['confirm_key_digest'] != ZERO_HASH and r['intent_ms'] > 0
              and r['fenced_source_generation'] == s['generation']
              and p['access_generation'] + 1 == r['fenced_access_generation'] == s['access_generation']
              and p['deletion_generation'] + 1 == r['fenced_deletion_generation'] == s['deletion_generation']
              and cp['copy_generation'] + 1 == cr['fenced_copy_generation'] == c['generation']
              and cp['access_generation'] + 1 == cr['fenced_access_generation'] == c['access_generation']
              and cp['deletion_generation'] + 1 == cr['fenced_deletion_generation'] == c['deletion_generation']
              and cp['lease'] == c['lease'] and cp['charged'] == c['charged'] == 1
              and cp['identity'] == ((0,) * 5 if not cp['identity_present'] else tuple(json.loads(c['identity'])))
              and (cp['identity_present'] or json.loads(c['identity']) is None))
        expected_copy = {PENDING: 'cleanup_pending', UNKNOWN: 'cleanup_pending', CHANGED: 'cleanup_failed', REMOVED: 'removed'}
        _need(c['state'] == expected_copy[cr['state']]
              and s['state'] == ('deleted' if r['state'] == REMOVED else 'cleanup_pending')
              and r['released'] == ln.reservation['released'] and cr['released'] == c['released'])
        if cp['component_result'] == PARTIAL:
            _need(cp['identity_present'] and cp['lease'] != ZERO_HASH and ln.member['catalogued_at'] is None
                  and (cp['partial_digest'], cp['partial_bytes']) == (c['partial_digest'], c['partial_bytes'])
                  and cp['identity'][2] == cp['partial_bytes'])
        elif cp['component_result'] == FULL:
            _need(cp['identity_present'] and cp['lease'] != ZERO_HASH and ln.member['catalogued_at'] is not None
                  and cp['identity'][2] == cp['bytes'] and not c['partial_digest'].strip())
        elif cp['component_result'] == NEVER:
            _need(not cp['identity_present'] and cp['lease'] == ZERO_HASH and ln.member['catalogued_at'] is None
                  and cp['copy_generation'] == 1 and cp['deletion_generation'] == 0
                  and not c['partial_digest'].strip() and c['partial_bytes'] == 2**53 - 1)
        else:
            _need(cp['component_result'] == UNRESOLVED and cp['lease'] != ZERO_HASH and ln.member['catalogued_at'] is None
                  and cr['state'] != REMOVED)
        for proof in (r, cr):
            removed = proof['state'] == REMOVED
            _need(proof['released'] == proof['handles_closed'] == int(removed)
                  and proof['released_bytes'] == (proof['bytes'] if removed else 0)
                  and bool(proof['removed_ms']) == removed
                  and proof['component_result'] < len(REASONS))

    def _operation_row(self, operation_id, selection=None):
        row = self._store.db.execute('SELECT source_id FROM deletion_source_slots WHERE slot_id=?',
                                     (ident(operation_id),)).fetchone()
        if row is None:
            _hold('forbidden', 403)
        return self._load(row['source_id'], selection)

    def _capability(self, p):
        return self._store._token('native-deletion:' + p['selection_fingerprint'], p['slot_id'], p['preview_generation'])

    def _confirmable(self, ln, p, r, cp):
        return bool(p['state'] == PREVIEW and not r['state']
                    and p['prepared_ms'] <= int(now(self._store.clock) * 1000) <= p['expires_ms']
                    and cp == self._copy_preview(ln, cp, p['preview_generation'], p['prepared_ms'])
                    and self._origin(ln, cp) == p['origin_fingerprint'])

    def _view(self, loaded, *, grant=False):
        ln, p, r, cp, cr = loaded
        _need(p['state'])
        states = (r['state'], cr['state']) if r['state'] else (PENDING, PENDING)
        if not r['state']:
            state = 'confirm'
        elif states == (REMOVED, REMOVED):
            state = 'deleted'
        elif CHANGED in states or REMOVED in states and UNKNOWN in states:
            state = 'partial_failure'
        elif UNKNOWN in states:
            state = 'unknown'
        else:
            state = 'deleting'
        result = dict(id=ln.slot, fileId=ln.source['id'], destinationId=ln.selection.destination_id,
            scopeId=ln.selection.scope_id, revision=(r if r['state'] else p)['operation_generation'],
            managementGeneration=p['preview_generation'], state=state, fingerprint=p['selection_fingerprint'],
            releasedBytes=r['released_bytes'] + cr['released_bytes'], sourceUnavailable=bool(r['state']),
            canConfirm=self._confirmable(ln, p, r, cp), canExecute=bool(r['state'] and (
                cr['state'] == PENDING or r['state'] == PENDING and not self._store.request_handles)),
            canRecover=self._recoverable(ln, cp, r, 'source') or self._recoverable(ln, cp, cr, 'copy'),
            pieces=[dict(kind=kind, state=PIECES[s], bytes=base['bytes'], chargedBytes=charge,
                         releasedBytes=receipt['released_bytes'], reason=REASONS[receipt['component_result']] if r['state'] else None)
                    for kind, s, base, receipt, charge in zip(('watch_original', 'workspace'), states, (p, cp), (r, cr),
                        (ln.reservation['bytes'] if not ln.reservation['released'] else 0,
                         ln.copy['bytes'] if ln.copy['charged'] and not ln.copy['released'] else 0))])
        if grant:
            _need(result['canConfirm'])
            result['capability'] = self._capability(p)
        if r['state'] == PENDING and self._store.request_handles:
            result['pieces'][0]['reason'] = REASONS[1]
        return result

    @contextmanager
    def _leased(self, load):
        with self._store._gate():
            keys = load()[0].keys
        with self._fixture._locks.hold(keys) as lease:
            def current():
                loaded = load()
                _need(loaded[0].keys == keys)
                lease.require(keys)
                return loaded
            yield current, lease

    def _file_read(self, scope, request_id, file_id):
        request_id, file_id = ident(request_id), ident(file_id)
        ln, _, _, _, workspace = self._load(file_id, scope)
        request, member = ln.request, ln.member
        if (request['id'] != request_id or request['epoch'] != self._store.owner_epoch
                or tuple(request['catalogue_' + k] for k in ('kind', 'destination', 'scope', 'conversation'))
                != tuple(asdict(scope).values()) or member['catalogued_at'] is None):
            _hold('forbidden', 403)
        recorded = member['catalogued_at']
        _need(type(recorded) in (int, float) and math.isfinite(recorded) and recorded >= 0)
        outbox = self._store.db.execute('SELECT retention_pin FROM main.attachment_outbox WHERE request_id=?',
                                        (request_id,)).fetchone()
        _need(outbox is not None and outbox['retention_pin'] in (0, 1))
        return dict(id=file_id, requestId=request_id, name=member['name'], mediaType=member['media'],
                    bytes=member['bytes'], cataloguedAt=recorded,
                    savedCopyState='removed' if workspace['state'] == REMOVED and workspace['released'] else 'unknown',
                    savedCopyAvailable=False, retentionPinned=bool(outbox['retention_pin']),
                    preparationKind='original_relay', previewAvailable=False, expiresAt=None)

    def _history_counts(self, scope):
        db = self._store.db
        args = tuple(asdict(scope).values())
        where = ('r.catalogue_kind=? AND r.catalogue_destination=? AND r.catalogue_scope=? '
                 'AND r.catalogue_conversation IS ?')
        # Check the catalogue key against the independently bound draft. A
        # moved catalogue key must not silently remove a retained association.
        records = _bounded(self._store, '''SELECT r.id,r.epoch,r.destination,d.destination AS draft_destination,
            r.catalogue_kind,r.catalogue_destination,r.catalogue_scope,r.catalogue_conversation
            FROM main.attachment_requests r LEFT JOIN main.drafts d ON d.id=r.draft_id
            WHERE (''' + where + ''') OR (d.destination_id=? AND d.scope_id=?) LIMIT ?''',
            (*args, scope.destination_id, scope.scope_id), self._store.limits['requests'])
        for row in records:
            if (row['epoch'] != self._store.owner_epoch or row['destination'] != row['draft_destination']
                    or HistorySelection.from_binding(_binding(row['destination'])) != scope
                    or tuple(row['catalogue_' + k] for k in ('kind', 'destination', 'scope', 'conversation')) != args):
                _hold('forbidden', 403)
            self._fixture._enrolled_request(row['id'])
        counted = db.execute('''SELECT COUNT(*) AS associations, COUNT(m.catalogued_at) AS saved,
            COUNT(CASE WHEN c.id IS NOT NULL AND f.id IS NOT NULL AND o.request_id IS NOT NULL
                 AND d.id=f.draft_id AND d.destination=r.destination AND m.preparation_kind='original_relay'
                 AND f.member=1 AND m.source_generation=f.generation AND m.identity=f.identity
                 AND (m.catalogued_at IS NULL OR (d.admitted_kind='native' AND d.admitted_request=r.id
                      AND r.authorized_at IS NOT NULL)) THEN 1 END) AS matched
            FROM main.attachment_requests r INDEXED BY request_history
            JOIN main.request_files m ON m.request_id=r.id
            LEFT JOIN main.drafts d ON d.id=r.draft_id
            LEFT JOIN main.files f ON f.id=m.source_id
            LEFT JOIN main.attachment_outbox o ON o.request_id=r.id AND o.owner_request_id=r.owner_request_id
            LEFT JOIN main.workspace_copies c ON c.id=m.workspace_id AND c.request_id=r.id
                     AND c.source_id=m.source_id AND c.role='workspace'
            WHERE ''' + where, args).fetchone()
        _need(counted['matched'] == counted['associations'])
        return counted['saved'], counted['associations'] - counted['saved']

    def history(self, selection, cursor=None):
        """Retained Files rows and counts detached from the exact native catalogue."""
        scope = _selection(selection)
        with self._operation(), self._store._gate():
            self._layout()
            _verify_base(self._store.db, self._fixture.binding)
            counts = self._history_counts(scope)
            page = self._catalogue.history(scope, cursor=cursor)
            _need(counts == (page['associationCount'], page['unclassifiedAssociationCount']))
            items = []
            for row in page['items']:
                item = self._file_read(scope, row['requestId'], row['id'])
                _need(all(row[k] == item[k] for k in ('id', 'requestId', 'name', 'mediaType', 'bytes',
                                                      'cataloguedAt', 'preparationKind', 'retentionPinned')))
                items.append(item)
            return dict(items=items, associationCount=counts[0], unclassifiedAssociationCount=counts[1],
                        nextCursor=page['nextCursor'])

    def file_details(self, selection, request_id, file_id):
        scope = _selection(selection)
        with self._operation(), self._store._gate():
            self._layout()
            _verify_base(self._store.db, self._fixture.binding)
            return self._file_read(scope, request_id, file_id)

    def management(self, selection, cursor=None):
        scope = _selection(selection)
        with self._operation(), self._store._gate():
            self._layout()
            _verify_base(self._store.db, self._fixture.binding)
            filters = ['native-deletion-v1', asdict(scope)]
            last = self._catalogue._cursor(filters, cursor)
            rows = self._store.db.execute('''SELECT r.created,r.id,r.destination,m.ordinal,m.source_id,
                     f.name,f.bytes,f.media,f.state,f.deletion_generation
              FROM attachment_requests r INDEXED BY request_history JOIN request_files m ON m.request_id=r.id
              JOIN drafts d ON d.id=r.draft_id AND d.admitted_request=r.id AND d.admitted_kind='native'
              JOIN files f ON f.id=m.source_id AND f.draft_id=d.id AND f.member=1 AND f.preparation_kind='original_relay'
              WHERE r.catalogue_kind=? AND r.catalogue_destination=? AND r.catalogue_scope=?
              AND r.catalogue_conversation IS ? AND (r.created,r.id,m.ordinal)>(?,?,?)
              ORDER BY r.created,r.id,m.ordinal LIMIT ?''', (*asdict(scope).values(), *last, PAGE + 1)).fetchall()
            files, deletions = [], []
            for row in rows[:PAGE]:
                _need(HistorySelection.from_binding(_binding(row['destination'])) == scope)
                available = row['state'] == 'ready' and not row['deletion_generation']
                file = dict(id=row['source_id'], name=row['name'], bytes=row['bytes'], mediaType=row['media'],
                            requestId=None, available=available, canPrepare=False, managementGeneration=0, deletionId=None)
                try:
                    loaded = self._load(row['source_id'], scope)
                except AttachmentError as error:
                    if error.code not in ('deletion_changed', 'deletion_roster_unavailable', 'cleanup_storage_unavailable'):
                        raise
                else:
                    ln, p, r, _, _ = loaded
                    _need(ln.request['id'] == row['id'])
                    file.update(requestId=ln.request['id'], canPrepare=available and not r['state'],
                                managementGeneration=p['preview_generation'], deletionId=ln.slot if p['state'] else None)
                    if p['state']:
                        deletions.append(self._view(loaded))
                files.append(file)
            continuation = None
            if len(rows) > PAGE:
                tail = rows[PAGE - 1]
                continuation = self._catalogue._seal('catalogue-cursor', {
                    'selection': filters, 'last': [tail['created'], tail['id'], tail['ordinal']]})
            return dict(files=files, deletions=deletions, nextCursor=continuation)

    def status(self, operation_id, selection):
        with self._operation(), self._store._gate():
            return self._view(self._operation_row(operation_id, _selection(selection)))

    def match_prepare(self, file_id, selection, command_digest):
        if type(command_digest) is not str or not TOKEN.fullmatch(command_digest) or command_digest == ZERO_HASH:
            _hold('invalid_request', 400)
        with self._operation(), self._store._gate():
            ln, p, _, _, _ = self._load(file_id, _selection(selection))
            if p['state'] and hmac.compare_digest(p['prepare_key_digest'], command_digest):
                return {'match': {'operationId': ln.slot, 'managementGeneration': p['preview_generation']}}
            return {'match': None}

    def prepare(self, file_id, selection, management_generation, command_key):
        scope, command = _selection(selection), _digest_key(command_key)
        if not integer(management_generation):
            _hold('invalid_request', 400)
        with self._operation(), self._leased(lambda: self._load(file_id, scope)) as (current, _):
            with self._store._gate(), self._store._transaction(reclaim=True):
                loaded = current()
                ln, old, r, cp, _ = loaded
                _need(not r['state'])
                if old['state'] and old['prepare_key_digest'] == command:
                    _need(management_generation + 1 == old['preview_generation'])
                    return self._view(loaded)
                _need(management_generation == old['preview_generation'])
                generation = management_generation + 1
                _need(integer(generation, 1))
                timestamp = int(now(self._store.clock) * 1000)
                cp = self._copy_preview(ln, cp, generation, timestamp)
                source = ln.source
                p = dict(old, state=PREVIEW, cause=1, origin_kind=NATIVE, origin_id=ln.request['id'],
                    operation_id=ln.slot, owner_epoch=self._store.owner_epoch, source_generation=source['generation'],
                    access_generation=source['access_generation'], deletion_generation=source['deletion_generation'],
                    identity_present=1, identity=tuple(json.loads(source['identity'])),
                    copy_count=1, copy_ids=(ln.copy['id'], ZERO_ID), roster_count=1,
                    roster_ids=(source['id'],) + (ZERO_ID,) * 4, roster_generations=(source['generation'], 0, 0, 0, 0),
                    preview_generation=generation, operation_generation=generation, prepared_ms=timestamp,
                    expires_ms=timestamp + TTL, prepare_key_digest=command, context_fingerprint=digest(asdict(scope)),
                    origin_fingerprint=self._origin(ln, cp))
                p['selection_fingerprint'] = self._fingerprint(p)
                p['capability_digest'] = self._store._hash(self._capability(p))
                cp['selection_fingerprint'] = p['selection_fingerprint']
                self._write('source', ln.slot, 'preview', p)
                self._write('copy', ln.copy['id'], 'preview', cp)
                return self._view(current(), grant=True)

    def recover_confirmation(self, operation_id, selection, preview_generation):
        if not integer(preview_generation, 1):
            _hold('invalid_request', 400)
        with self._operation(), self._store._gate():
            loaded = self._operation_row(operation_id, _selection(selection))
            _need(loaded[1]['preview_generation'] == preview_generation)
            return self._view(loaded, grant=True)

    def _write(self, piece, identifier, column, values):
        if piece == 'source':
            table, field, codec = 'deletion_source_slots', 'slot_id', SOURCE_PREVIEW if column == 'preview' else SOURCE_RECEIPT
        else:
            table, field, codec = 'deletion_copy_slots', 'copy_id', COPY_PREVIEW if column == 'preview' else COPY_RECEIPT
        self._changed(f'UPDATE {table} SET {column}=? WHERE {field}=?', (codec.encode(values), identifier))

    def _changed(self, sql, args):
        _need(self._store.db.execute(sql, args).rowcount == 1)

    def confirm(self, operation_id, capability, fingerprint, command_key):
        command = _digest_key(command_key)
        with self._operation(), self._leased(lambda: self._operation_row(operation_id)) as (current, _):
            with self._store._gate(), self._store._transaction(reclaim=True):
                loaded = current()
                ln, p, r, cp, _ = loaded
                _need(p['state'] and hmac.compare_digest(p['capability_digest'], self._store._hash(capability))
                      and fingerprint == p['selection_fingerprint'])
                if r['state']:
                    _need(r['confirm_key_digest'] == command)
                    return dict(operation=self._view(loaded), confirmedNow=False)
                _need(self._confirmable(ln, p, r, cp))
                if self._store.request_handles:
                    _hold('deletion_busy')
                timestamp = int(now(self._store.clock) * 1000)
                common = dict(state=PENDING, component_result=0, intent_ms=timestamp,
                              operation_generation=p['operation_generation'] + 1)
                source = dict(p, **common, confirm_key_digest=command, fenced_source_generation=p['source_generation'],
                    fenced_access_generation=p['access_generation'] + 1, fenced_deletion_generation=p['deletion_generation'] + 1)
                copy = dict(cp, **common, fenced_copy_generation=cp['copy_generation'] + 1,
                    fenced_access_generation=cp['access_generation'] + 1, fenced_deletion_generation=cp['deletion_generation'] + 1)
                self._changed("""UPDATE files SET state='cleanup_pending',access_generation=access_generation+1,
                    deletion_generation=deletion_generation+1,cleanup_reserve=substr(cleanup_reserve,1,128)
                    WHERE id=? AND state='ready' AND access_generation=? AND deletion_generation=?""",
                    (ln.source['id'], p['access_generation'], p['deletion_generation']))
                self._changed("""UPDATE workspace_copies SET state='cleanup_pending',generation=generation+1,
                    access_generation=access_generation+1,deletion_generation=deletion_generation+1,padding=substr(padding,1,256)
                    WHERE id=? AND generation=? AND access_generation=? AND deletion_generation=? AND released=0""",
                    (ln.copy['id'], cp['copy_generation'], cp['access_generation'], cp['deletion_generation']))
                self._write('source', ln.slot, 'receipt', source)
                self._write('copy', ln.copy['id'], 'receipt', copy)
                view = self._view(current())
            return dict(operation=view, confirmedNow=True)

    def _attempt(self, current, piece, revision, recover):
        with self._store._gate(), self._store._transaction(reclaim=True):
            loaded = current()
            ln, p, r, cp, cr = loaded
            _need(r['state'] and r['operation_generation'] == revision)
            receipt = r if piece == 'source' else cr
            if receipt['state'] != PENDING and (not recover or not self._recoverable(ln, cp, receipt, piece)):
                return None
            if piece == 'source' and self._store.request_handles:
                return None
            self._attempted.add((ln.slot, piece))
            update = dict(state=UNKNOWN, operation_generation=revision + 1,
                          intent_ms=int(now(self._store.clock) * 1000), component_result=3)
            self._write('source', ln.slot, 'receipt', dict(r, operation_generation=revision + 1) |
                        (update if piece == 'source' else {}))
            if piece == 'copy':
                self._write('copy', ln.copy['id'], 'receipt', dict(cr, **update))
            return current()

    def _recoverable(self, ln, cp, receipt, piece):
        return (receipt['state'] == UNKNOWN and (ln.slot, piece) not in self._attempted
                and (not self._store.request_handles if piece == 'source' else
                     cp['component_result'] == NEVER or
                     cp['component_result'] in (FULL, PARTIAL) and self._fixture._resolve is not None))

    def _workspace_removal(self, loaded, current, lease):
        ln, _, r, cp, cr = loaded
        if cp['component_result'] == NEVER:
            return REMOVED, 0
        if cp['component_result'] == UNRESOLVED:
            return UNKNOWN, 5
        row, profile = ln.request, ln.profile
        authority = RequestAuthority(row['id'], row['fingerprint'], r['operation_generation'], profile['revision'],
                                     profile['context_fingerprint'], profile['workspace_fingerprint'], row['epoch'])
        context = self._fixture._context(authority)
        if context is None or any(v is None for v in ln.directories.values()):
            return UNKNOWN, 4
        with self._store._gate():
            spec = self._fixture._requests._intent(ln.copy)
        full = cp['component_result'] == FULL
        retained = RetainedCopyCleanup(spec.id, spec.lease_id, spec.copy_generation, cp['identity'],
                    cp['digest'] if full else cp['partial_digest'], cp['bytes'] if full else cp['partial_bytes'],
                    'full' if full else 'closed_partial')

        def validate(candidate, phase, specs):
            with self._store._gate():
                again = current()
                return (phase == 'cleanup' and candidate == authority and specs == (spec,)
                        and self._same_attempt(loaded, again) and self._fixture._requests._intent(again[0].copy) == spec)

        # A second authority view shares the exact lifetime registry and gives
        # only this committed attempt the cleanup phase.
        worker = Materializer(enabled=True, workspace_files=self._store.files, validate_authority=validate,
                              checkpoint=self._store.checkpoint)
        proof = worker.remove_workspace_copy(self._store.files, context, authority, spec, retained, lease,
                                              directories=WorkspaceDirectories.from_record(ln.directories))
        _need(type(proof) is CopyCleanup and proof.handles_closed is True and
              (proof.copy_id, proof.copy_generation, proof.lease_id) == (spec.id, spec.copy_generation, spec.lease_id)
              and proof.result in ('removed', 'absent'))
        return REMOVED, 0

    @staticmethod
    def _same_attempt(started, current):
        return (started[1:] == current[1:]
                and started[0].keys == current[0].keys
                and started[0].slot == current[0].slot)

    def _physical(self, loaded, current, lease, piece):
        ln, _, r, _, _ = loaded
        try:
            if piece == 'copy':
                return self._workspace_removal(loaded, current, lease)
            with self._store._gate():
                _need(self._same_attempt(loaded, current()))
                if self._store.request_handles:
                    return UNKNOWN, 1
            result = self._store.files.remove(ln.source['id'], list(r['identity']), r['digest'], durable_absence=True)
            return {'removed': (REMOVED, 0), 'absent': (REMOVED, 0), 'changed': (CHANGED, 2),
                    'busy': (UNKNOWN, 1)}.get(result, (UNKNOWN, 3))
        except AttachmentError as error:
            if error.code == 'cleanup_busy':
                return UNKNOWN, 1       # Windows: the copy is open in another program; retried later, as a busy source
            return (CHANGED, 2) if error.code == 'copy_changed' else (UNKNOWN, 3)
        except OSError:
            return UNKNOWN, 3

    def _settle(self, started, current, piece, state, reason):
        with self._store._gate(), self._store._transaction(reclaim=True):
            loaded = current()
            _need(self._same_attempt(started, loaded))
            ln, _, r, _, cr = loaded
            receipt = r if piece == 'source' else cr
            _need(receipt['state'] == UNKNOWN)
            settled = dict(receipt, state=state, component_result=reason)
            if state == REMOVED:
                count = receipt['bytes']
                if piece == 'source':
                    self._changed('UPDATE reservations SET released=1 WHERE file_id=? AND released=0 AND bytes=?',
                                  (ln.source['id'], count))
                    self._changed("UPDATE files SET state='deleted',cleanup_reserve='' WHERE id=? AND state='cleanup_pending'",
                                  (ln.source['id'],))
                else:
                    self._changed("""UPDATE workspace_copies SET state='removed',released=1,padding=''
                                    WHERE id=? AND charged=1 AND released=0 AND state='cleanup_pending'""", (ln.copy['id'],))
                self._changed('UPDATE quota SET bytes=bytes-? WHERE id=1 AND bytes>=?', (count, count))
                settled.update(released=1, handles_closed=1, released_bytes=count, removed_ms=int(now(self._store.clock) * 1000))
            elif state == CHANGED and piece == 'copy':
                self._changed("UPDATE workspace_copies SET state='cleanup_failed' WHERE id=? AND state='cleanup_pending'",
                              (ln.copy['id'],))
            self._write(piece, ln.slot if piece == 'source' else ln.copy['id'], 'receipt', settled)
            return self._view(current())

    def execute(self, operation_id, expected_revision, *, recover=False):
        if not integer(expected_revision, 1) or type(recover) is not bool:
            _hold('invalid_request', 400)
        with self._operation(), self._leased(lambda: self._operation_row(operation_id)) as (current, lease):
            with self._store._gate():
                loaded = current()
                _need(loaded[2]['state'] and loaded[2]['operation_generation'] == expected_revision)
                view = self._view(loaded)
            if not (view['canExecute'] or recover and view['canRecover']):
                return view
            for piece in ('source', 'copy'):
                started = self._attempt(current, piece, view['revision'], recover)
                if started is not None:
                    state, reason = self._physical(started, current, lease, piece)
                    self._store.checkpoint('native_deletion_physical_returned')
                    view = self._settle(started, current, piece, state, reason)
            return view
