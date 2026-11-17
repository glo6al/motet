"""Fixed schema-7 emergency storage. No deletion commands, authority or IO.

All functions participate in AttachmentStore's existing gate/transaction. The
checksum detects corruption; possession of a decoded record grants no rights.
See docs/attachments/DELETION_SLOTS.md for the byte-offset worksheet.
"""
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
import struct

from attachments_types import fail

SOURCE_BYTES, COPY_BYTES = 2048, 512
UINT64 = 2**64 - 1
ZERO_ID, ZERO_HASH = '0' * 32, '0' * 64
_ID, _HASH = re.compile(r'[0-9a-f]{32}\Z'), re.compile(r'[0-9a-f]{64}\Z')
_MAGIC, _VERSION = b'ADSL', 1


def _invalid():
    fail('cleanup_storage_unavailable', 'Attachment cleanup storage could not be verified.', 503)


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    count: int = 1

    @property
    def width(self):
        return {'u8': 1, 'u64': 8, 'id': 16, 'hash': 32, 'identity': 40, 'pad': 1}[self.kind] * self.count


def _fields(kind, names):
    return tuple(Field(name, kind) for name in names.split())


SOURCE_FIELDS = (
    *_fields('u8', 'state cause origin_kind flags identity_present released roster_count copy_count component_result handles_closed'),
    Field('_head_padding', 'pad', 6),
    *_fields('id', 'slot_id source_id anchor_id operation_id origin_id draft_id'),
    *_fields('u64', 'preview_generation operation_generation source_generation access_generation deletion_generation policy_generation membership_generation publication_generation fenced_source_generation fenced_access_generation fenced_deletion_generation'),
    *_fields('u64', 'prepared_ms expires_ms intent_ms removed_ms'),
    Field('identity', 'identity'),
    *_fields('u64', 'bytes released_bytes'),
    *_fields('hash', 'owner_epoch context_fingerprint digest selection_fingerprint prepare_key_digest confirm_key_digest capability_digest origin_fingerprint'),
    Field('roster_ids', 'id', 5), Field('roster_generations', 'u64', 5), Field('copy_ids', 'id', 2),
)
COPY_FIELDS = (
    *_fields('u8', 'state cause role flags identity_present released charged handles_closed component_result'),
    Field('_head_padding', 'pad', 7),
    *_fields('id', 'copy_id source_id request_id anchor_id source_slot_id operation_id'),
    *_fields('u64', 'preview_generation operation_generation source_generation access_generation deletion_generation copy_generation fenced_copy_generation fenced_access_generation fenced_deletion_generation'),
    *_fields('hash', 'lease digest partial_digest selection_fingerprint parent_fingerprint'),
    Field('identity', 'identity'),
    *_fields('u64', 'bytes partial_bytes released_bytes prepared_ms intent_ms removed_ms'),
)


class FixedCodec:
    """Closed, type-preserving fixed record; future states have no executor here."""
    def __init__(self, kind, size, fields):
        self.kind, self.size, self.fields = kind, size, fields
        self.names = frozenset(f.name for f in fields if f.kind != 'pad')
        position, offsets = 8, {}
        for field in fields:
            offsets[field.name] = (position, position + field.width)
            position += field.width
        self.offsets, self.payload_end = offsets, position
        if position > size - 32:
            raise ValueError('The fixed attachment slot layout does not fit.')

    @staticmethod
    def _default(field):
        value = {'u8': 0, 'u64': 0, 'id': ZERO_ID, 'hash': ZERO_HASH, 'identity': (0,) * 5}[field.kind]
        return (value,) * field.count if field.count != 1 else value

    @staticmethod
    def _scalar(field, value):
        if field.kind in ('u8', 'u64'):
            maximum = 255 if field.kind == 'u8' else UINT64
            if type(value) is not int or not 0 <= value <= maximum:
                _invalid()
            return struct.pack('>B' if field.kind == 'u8' else '>Q', value)
        if field.kind in ('id', 'hash'):
            if type(value) is not str or not (_ID if field.kind == 'id' else _HASH).fullmatch(value):
                _invalid()
            return bytes.fromhex(value)
        if (type(value) is not tuple or len(value) != 5
                or any(type(n) is not int or not 0 <= n <= 2**63 - 1 for n in value)):
            _invalid()
        return struct.pack('>5Q', *value)

    @staticmethod
    def _validate(value):
        maximum = {'state': 6, 'cause': 2, 'origin_kind': 3, 'role': 2, 'flags': 0,
                   'identity_present': 1, 'released': 1, 'charged': 1, 'handles_closed': 1,
                   'roster_count': 5, 'copy_count': 2, 'component_result': 7}
        if any(value.get(name, 0) > limit for name, limit in maximum.items()):
            _invalid()
        if not value['identity_present'] and value['identity'] != (0,) * 5:
            _invalid()
        if value['released_bytes'] > value['bytes'] or value.get('partial_bytes', 0) > value['bytes']:
            _invalid()
        if 'roster_ids' in value:
            n, c = value['roster_count'], value['copy_count']
            if (len(set(value['roster_ids'][:n])) != n or len(set(value['copy_ids'][:c])) != c
                    or any(g == 0 for g in value['roster_generations'][:n])
                    or value['roster_ids'][n:] != (ZERO_ID,) * (5 - n)
                    or value['roster_generations'][n:] != (0,) * (5 - n)
                    or value['copy_ids'][c:] != (ZERO_ID,) * (2 - c)):
                _invalid()

    def encode(self, values):
        if type(values) is not dict or set(values) - self.names:
            _invalid()
        value = {field.name: values.get(field.name, self._default(field))
                 for field in self.fields if field.kind != 'pad'}
        raw = bytearray(self.size)
        raw[:8] = _MAGIC + bytes((_VERSION, self.kind, 0, 0))
        for field in self.fields:
            if field.kind == 'pad':
                continue
            item = value[field.name]
            if field.count != 1:
                if type(item) is not tuple or len(item) != field.count:
                    _invalid()
                encoded = b''.join(self._scalar(field, entry) for entry in item)
            else:
                encoded = self._scalar(field, item)
            start, end = self.offsets[field.name]
            raw[start:end] = encoded
        self._validate(value)
        raw[-32:] = hashlib.sha256(raw[:-32]).digest()
        return bytes(raw)

    def decode(self, raw):
        if (type(raw) is not bytes or len(raw) != self.size
                or raw[:8] != _MAGIC + bytes((_VERSION, self.kind, 0, 0))
                or not hmac.compare_digest(raw[-32:], hashlib.sha256(raw[:-32]).digest())
                or any(raw[self.payload_end:-32])):
            _invalid()
        values = {}
        for field in self.fields:
            start, end = self.offsets[field.name]
            data = raw[start:end]
            if field.kind == 'pad':
                if any(data):
                    _invalid()
                continue
            step = field.width // field.count
            pieces = [data[n:n + step] for n in range(0, len(data), step)]
            if field.kind in ('u8', 'u64'):
                decoded = tuple(struct.unpack('>B' if field.kind == 'u8' else '>Q', p)[0] for p in pieces)
            elif field.kind in ('id', 'hash'):
                decoded = tuple(p.hex() for p in pieces)
            else:
                decoded = tuple(struct.unpack('>5Q', p) for p in pieces)
                if any(n > 2**63 - 1 for entry in decoded for n in entry):
                    _invalid()
            values[field.name] = decoded if field.count != 1 else decoded[0]
        self._validate(values)
        return values


SOURCE_PREVIEW = FixedCodec(1, SOURCE_BYTES, SOURCE_FIELDS)
SOURCE_RECEIPT = FixedCodec(2, SOURCE_BYTES, SOURCE_FIELDS)
COPY_PREVIEW = FixedCodec(3, COPY_BYTES, COPY_FIELDS)
COPY_RECEIPT = FixedCodec(4, COPY_BYTES, COPY_FIELDS)
assert SOURCE_PREVIEW.payload_end == 704 and COPY_PREVIEW.payload_end == 440


def _transaction(store):
    if not store.db.in_transaction:
        _invalid()


def _source(store, source_id):
    row = store.db.execute('SELECT f.* FROM files f JOIN drafts d ON d.id=f.draft_id WHERE f.id=?',
                           (source_id,)).fetchone()
    if row is None:
        _invalid()
    return row


def _copy(store, copy_id):
    # A matching slot cannot establish ownership of the row it mirrors. Require
    # the native request's exact immutable member, including its role-specific
    # allocation ID, independently of the newly allocated/decoded slot bytes.
    rows = store.db.execute("""SELECT c.*,m.workspace_id,m.temporary_id,
        m.bytes AS member_bytes,m.digest AS member_digest,
        m.media AS member_media,m.name AS member_name,m.preparation_kind AS member_kind,
        f.bytes AS source_bytes,f.digest AS source_digest,
        f.media AS source_media,f.name AS source_name,f.preparation_kind AS source_kind
        FROM workspace_copies c JOIN attachment_requests r ON r.id=c.request_id
        JOIN files f ON f.id=c.source_id JOIN drafts d ON d.id=f.draft_id AND d.id=r.draft_id
        JOIN request_files m ON m.request_id=c.request_id AND m.source_id=c.source_id
        WHERE c.id=?""", (copy_id,)).fetchall()
    if len(rows) != 1:
        _invalid()
    row = rows[0]
    if (row['role'] not in ('workspace', 'temporary')
            or row['id'] != row['workspace_id' if row['role'] == 'workspace' else 'temporary_id']
            or row['bytes'] != row['member_bytes'] or row['bytes'] != row['source_bytes']
            or row['digest'] != row['member_digest'] or row['digest'] != row['source_digest']
            or any(row['member_' + name] != row['source_' + name] for name in ('media', 'name', 'kind'))):
        _invalid()
    return row


def allocate_source(store, source_id):
    _transaction(store)
    source = _source(store, source_id)
    slot_id = secrets.token_hex(16)
    value = {'slot_id': slot_id, 'source_id': source['id'], 'draft_id': source['draft_id'],
             'bytes': source['bytes'], 'digest': source['digest']}
    store.db.execute('INSERT INTO deletion_source_slots(slot_id,source_id,preview,receipt) VALUES(?,?,?,?)',
                     (slot_id, source_id, SOURCE_PREVIEW.encode(value), SOURCE_RECEIPT.encode(value)))


def verify_source(store, source_id):
    row = store.db.execute('''SELECT s.*,f.draft_id,f.bytes,f.digest FROM deletion_source_slots s
        JOIN files f ON f.id=s.source_id JOIN drafts d ON d.id=f.draft_id
        WHERE s.source_id=?''', (source_id,)).fetchone()
    if row is None:
        _invalid()
    for codec, column in ((SOURCE_PREVIEW, 'preview'), (SOURCE_RECEIPT, 'receipt')):
        value = codec.decode(row[column])
        if any(value[name] != row[name] for name in ('slot_id', 'source_id', 'draft_id', 'bytes', 'digest')):
            _invalid()
    return row['slot_id']


def allocate_copy(store, copy_id):
    _transaction(store)
    row = _copy(store, copy_id)
    source_slot = verify_source(store, row['source_id'])
    value = {'copy_id': row['id'], 'source_id': row['source_id'], 'request_id': row['request_id'],
             'source_slot_id': source_slot, 'role': 1 if row['role'] == 'workspace' else 2,
             'bytes': row['bytes'], 'digest': row['digest']}
    store.db.execute('INSERT INTO deletion_copy_slots(copy_id,preview,receipt) VALUES(?,?,?)',
                     (copy_id, COPY_PREVIEW.encode(value), COPY_RECEIPT.encode(value)))


def verify_copy(store, copy_id):
    canonical = _copy(store, copy_id)
    row = store.db.execute('SELECT * FROM deletion_copy_slots WHERE copy_id=?', (copy_id,)).fetchone()
    if row is None:
        _invalid()
    source_slot = verify_source(store, canonical['source_id'])
    for codec, column in ((COPY_PREVIEW, 'preview'), (COPY_RECEIPT, 'receipt')):
        value = codec.decode(row[column])
        expected = dict(canonical, copy_id=copy_id)
        if (any(value[name] != expected[name] for name in ('copy_id', 'source_id', 'request_id', 'bytes', 'digest'))
                or value['source_slot_id'] != source_slot
                or value['role'] != (1 if canonical['role'] == 'workspace' else 2)):
            _invalid()


def verify_request(store, request_id):
    request = store.db.execute('SELECT r.* FROM attachment_requests r JOIN drafts d ON d.id=r.draft_id WHERE r.id=?',
                               (request_id,)).fetchone()
    members = store.db.execute('SELECT * FROM request_files WHERE request_id=? ORDER BY ordinal', (request_id,)).fetchall()
    if request is None or not 1 <= len(members) <= 5 or len({m['source_id'] for m in members}) != len(members):
        _invalid()
    expected = []
    for member in members:
        source = _source(store, member['source_id'])
        if (source['draft_id'] != request['draft_id']
                or any(source[name] != member[name] for name in ('bytes', 'digest', 'media', 'name', 'preparation_kind'))):
            _invalid()
        verify_source(store, source['id'])
        for role, copy_id in (('workspace', member['workspace_id']), ('temporary', member['temporary_id'])):
            if copy_id is None and role == 'temporary':
                continue
            copy = _copy(store, copy_id)
            if copy['request_id'] != request_id or copy['role'] != role:
                _invalid()
            verify_copy(store, copy_id)
            expected.append(copy_id)
    actual = [r['id'] for r in store.db.execute('SELECT id FROM workspace_copies WHERE request_id=?', (request_id,))]
    if len(set(expected)) != len(expected) or sorted(expected) != sorted(actual):
        _invalid()


def verify_all(store):
    if (store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0]
            != store.db.execute('SELECT COUNT(*) FROM deletion_source_slots').fetchone()[0]
            or store.db.execute('SELECT COUNT(*) FROM workspace_copies').fetchone()[0]
            != store.db.execute('SELECT COUNT(*) FROM deletion_copy_slots').fetchone()[0]):
        _invalid()
    for row in store.db.execute('SELECT id FROM files'):
        verify_source(store, row['id'])
    for row in store.db.execute('SELECT id FROM workspace_copies'):
        verify_copy(store, row['id'])
    for row in store.db.execute('SELECT id FROM attachment_requests'):
        verify_request(store, row['id'])
    cursor = store.db.execute("SELECT value FROM meta WHERE key='deletion_cursor'").fetchone()
    if cursor is None or type(cursor[0]) is not str or not _ID.fullmatch(cursor[0]):
        _invalid()


def migrate(store):
    """Called inside one schema transaction; FULL must roll everything back."""
    _transaction(store)
    store.db.execute('''CREATE TABLE deletion_source_slots(
        slot_id TEXT PRIMARY KEY CHECK(length(slot_id)=32 AND slot_id NOT GLOB '*[^0-9a-f]*'),
        source_id TEXT UNIQUE NOT NULL REFERENCES files(id),
        preview BLOB NOT NULL CHECK(typeof(preview)='blob' AND length(preview)=2048),
        receipt BLOB NOT NULL CHECK(typeof(receipt)='blob' AND length(receipt)=2048)
        ) WITHOUT ROWID''')
    store.db.execute('''CREATE TABLE deletion_copy_slots(
        copy_id TEXT PRIMARY KEY REFERENCES workspace_copies(id),
        preview BLOB NOT NULL CHECK(typeof(preview)='blob' AND length(preview)=512),
        receipt BLOB NOT NULL CHECK(typeof(receipt)='blob' AND length(receipt)=512)
        ) WITHOUT ROWID''')
    for row in store.db.execute('SELECT id FROM files').fetchall():
        allocate_source(store, row['id'])
    for row in store.db.execute('SELECT id FROM workspace_copies').fetchall():
        allocate_copy(store, row['id'])
    store.db.execute('DROP INDEX file_draft')
    store.db.execute('CREATE INDEX file_draft ON files(draft_id,id)')
    store.db.execute("INSERT INTO meta(key,value) VALUES('deletion_cursor',?)", (ZERO_ID,))
    verify_all(store)
