"""Retained attachment metadata and revocable text access; no provider or deletion IO."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import base64
import hashlib
import hmac
import json
import math
import secrets

from attachments_prepare import prepare
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError, OPAQUE, canonical, destination, fail, ident, integer, now

PAGE = 50
CAP_SECONDS = 300


@dataclass(frozen=True)
class HistorySelection:
    kind: str
    destination_id: str
    scope_id: str
    conversation_id: str | None

    def __post_init__(self):
        if (self.kind not in ('agent', 'officer')
                or any(type(x) is not str or not OPAQUE.fullmatch(x) for x in (self.destination_id, self.scope_id))
                or self.kind == 'agent' and self.conversation_id is not None):
            fail('invalid_request', 'Choose an exact retained attachment context.', 400)
        if self.kind == 'officer':
            ident(self.conversation_id)

    @classmethod
    def from_binding(cls, binding):
        return cls(binding['kind'], binding['id'], binding['scopeId'], binding['conversationId'])


@dataclass(frozen=True)
class PreviewSnapshot:
    capability: str
    file_id: str
    identity: str
    payload: str
    read_bytes: bool


def migrate(store):
    db = store.db
    db.execute('ALTER TABLE request_files ADD COLUMN catalogued_at REAL')
    for field in ('kind', 'destination', 'scope', 'conversation'):
        db.execute('ALTER TABLE attachment_requests ADD COLUMN catalogue_' + field + ' TEXT')
    for row in db.execute('SELECT id,destination FROM attachment_requests').fetchall():
        binding = destination(json.loads(row['destination']))
        db.execute('UPDATE attachment_requests SET catalogue_kind=?,catalogue_destination=?,catalogue_scope=?,catalogue_conversation=? WHERE id=?',
                   (binding['kind'], binding['id'], binding['scopeId'], binding['conversationId'], row['id']))
    db.execute('CREATE INDEX request_source ON request_files(source_id,request_id)')
    db.execute('CREATE INDEX request_created ON attachment_requests(created,id)')
    db.execute('CREATE INDEX request_history ON attachment_requests(catalogue_kind,catalogue_destination,catalogue_scope,catalogue_conversation,created,id)')
    # Removed/reserved/partial allocations do not prove earlier publication.
    rows = db.execute("SELECT f.request_id,f.ordinal,f.bytes,f.digest,c.identity,c.bytes AS copy_bytes,c.digest AS copy_digest,r.updated FROM request_files f JOIN workspace_copies c ON c.id=f.workspace_id JOIN attachment_requests r ON r.id=f.request_id WHERE c.request_id=f.request_id AND c.source_id=f.source_id AND c.role='workspace' AND c.state='available' AND c.charged=1 AND c.released=0").fetchall()
    for row in rows:
        value = json.loads(row['identity'])
        if (type(value) is list and len(value) == 5 and all(integer(x, 0, 2**63-1) for x in value)
                and value[2] == row['bytes'] == row['copy_bytes'] and row['digest'] == row['copy_digest']):
            db.execute('UPDATE request_files SET catalogued_at=? WHERE request_id=? AND ordinal=?',
                       (row['updated'], row['request_id'], row['ordinal']))


class Catalogue:
    def __init__(self, store):
        self.store = store

    def _seal(self, domain, value):
        data = canonical(dict(value, epoch=self.store.owner_epoch, v=1)).encode()
        tag = hmac.new(self.store.secret, domain.encode() + b'\0' + data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(data + tag).decode().rstrip('=')

    def _unseal(self, domain, token):
        try:
            if (type(token) is not str or not 1 <= len(token) <= 4096
                    or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in token)):
                raise ValueError()
            raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
            data, tag = raw[:-32], raw[-32:]
            expected = hmac.new(self.store.secret, domain.encode() + b'\0' + data, hashlib.sha256).digest()
            value = json.loads(data)
            if (not hmac.compare_digest(tag, expected) or type(value) is not dict
                    or value.get('epoch') != self.store.owner_epoch or value.get('v') != 1
                    or type(value.get('v')) is not int or self._seal(domain, value) != token):
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError):
            fail('invalid_cursor' if domain == 'catalogue-cursor' else 'forbidden',
                 'The attachment authority could not be verified.', 400 if domain == 'catalogue-cursor' else 403)

    def _cursor(self, selection, cursor):
        if cursor is None:
            return [-1, '0' * 32, -1]
        value = self._unseal('catalogue-cursor', cursor)
        last = value.get('last')
        if (set(value) != {'epoch', 'v', 'selection', 'last'} or value['selection'] != selection
                or type(last) is not list or len(last) != 3
                or type(last[0]) not in (int, float) or not math.isfinite(last[0])
                or last[0] < 0 or not integer(last[2], -1, 5)):
            fail('invalid_cursor', 'This attachment list cursor is invalid.', 400)
        ident(last[1])
        return last

    def _selection(self, selection):
        if type(selection) is not HistorySelection:
            fail('invalid_request', 'Choose an exact retained attachment context.', 400)
        db = self.store.db
        if selection.kind == 'officer':
            row = db.execute('SELECT scope FROM conversations WHERE id=?', (selection.conversation_id,)).fetchone()
            if row is None or row['scope'] != selection.scope_id:
                fail('forbidden', 'The retained conversation could not be authorized.', 403)
            current = self.store.current_conversation(selection.scope_id)
            if (selection.destination_id == 'officer' and current is not None
                    and current['id'] == selection.conversation_id):
                return
        rows = db.execute('SELECT destination FROM drafts WHERE destination_id=? AND scope_id=?',
                          (selection.destination_id, selection.scope_id))
        if any(HistorySelection.from_binding(json.loads(r['destination'])) == selection for r in rows):
            return
        row = db.execute('SELECT 1 FROM attachment_requests WHERE catalogue_kind=? AND catalogue_destination=? AND catalogue_scope=? AND catalogue_conversation IS ? LIMIT 1',
                         tuple(asdict(selection).values())).fetchone()
        if row is None:
            fail('forbidden', 'The retained attachment context could not be authorized.', 403)

    def history(self, selection, *, cursor=None):
        with self.store._gate():
            self._selection(selection)
            filters = ['history', asdict(selection)]
            last = self._cursor(filters, cursor)
            where = 'r.catalogue_kind=? AND r.catalogue_destination=? AND r.catalogue_scope=? AND r.catalogue_conversation IS ?'
            args = tuple(asdict(selection).values())
            counts = self.store.db.execute('SELECT COUNT(*) AS associations,SUM(CASE WHEN f.catalogued_at IS NOT NULL THEN 1 ELSE 0 END) AS saved FROM request_files f JOIN attachment_requests r ON r.id=f.request_id WHERE ' + where, args).fetchone()
            rows = self.store.db.execute('''SELECT f.*,r.created,r.destination,r.stage,r.epoch,r.id AS rid,
                c.state AS copy_state,c.released,o.owner_state,o.retention_pin
                FROM request_files f JOIN attachment_requests r ON r.id=f.request_id
                JOIN workspace_copies c ON c.id=f.workspace_id JOIN attachment_outbox o ON o.request_id=r.id
                WHERE ''' + where + ''' AND f.catalogued_at IS NOT NULL AND
                (r.created>? OR (r.created=? AND (r.id>? OR (r.id=? AND f.ordinal>?))))
                ORDER BY r.created,r.id,f.ordinal LIMIT 51''', args + (last[0], last[0], last[1], last[1], last[2])).fetchall()
            items = []
            for row in rows[:PAGE]:
                if HistorySelection.from_binding(json.loads(row['destination'])) != selection:
                    fail('storage_unavailable', 'Retained attachment context could not be verified.', 503)
                item = {'id': row['source_id'], 'requestId': row['rid'], 'name': row['name'],
                        'mediaType': row['media'], 'bytes': row['bytes'], 'cataloguedAt': row['catalogued_at'],
                        'savedCopyState': row['copy_state'], 'savedCopyAvailable': row['copy_state'] == 'available' and not row['released'],
                        'requestState': row['stage'], 'replyState': 'available' if row['owner_state'].strip() == 'answered' else 'unknown',
                        'retentionPinned': bool(row['retention_pin']), 'preparationKind': row['preparation_kind'],
                        'previewAvailable': False, 'sourceCapability': None}
                try:
                    cap = self.source_authority(selection, 'request', row['rid'], row['source_id'])
                    item.update(sourceCapability=cap, previewAvailable=row['preparation_kind'] == 'text')
                except AttachmentError as exc:
                    if exc.code != 'forbidden':
                        raise
                items.append(item)
            next_cursor = None
            if len(rows) > PAGE:
                tail = rows[PAGE-1]
                next_cursor = self._seal('catalogue-cursor', {'selection': filters, 'last': [tail['created'], tail['rid'], tail['ordinal']]})
            return {'items': items, 'nextCursor': next_cursor, 'associationCount': counts['saved'] or 0,
                    'unclassifiedAssociationCount': counts['associations'] - (counts['saved'] or 0),
                    'retentionAvailable': selection.kind == 'agent'}

    def storage_files(self, *, cursor=None):
        with self.store._gate():
            last = self._cursor(['storage'], cursor)
            # Every source reservation and copy allocation is counted, not only this page.
            source_bytes = self.store.db.execute('SELECT COALESCE(SUM(bytes),0) FROM reservations WHERE released=0').fetchone()[0]
            copy_bytes = self.store.db.execute('SELECT COALESCE(SUM(bytes),0) FROM workspace_copies WHERE charged=1 AND released=0').fetchone()[0]
            rows = self.store.db.execute('''SELECT f.*,r.bytes AS reserved,r.released,d.destination FROM files f
                JOIN reservations r ON r.file_id=f.id JOIN drafts d ON d.id=f.draft_id
                WHERE f.id>? ORDER BY f.id LIMIT 51''', (last[1] if cursor else '',)).fetchall()
            items = []
            for row in rows[:PAGE]:
                copies = self.store.db.execute('SELECT role,state,COUNT(*) AS count,SUM(CASE WHEN charged=1 AND released=0 THEN bytes ELSE 0 END) AS charged_bytes FROM workspace_copies WHERE source_id=? GROUP BY role,state ORDER BY role,state', (row['id'],)).fetchall()
                items.append({'id': row['id'], 'name': row['name'], 'mediaType': row['media'], 'bytes': row['bytes'],
                              'sourceState': row['state'], 'preparationKind': row['preparation_kind'],
                              'context': asdict(HistorySelection.from_binding(json.loads(row['destination']))),
                              'physicalBytes': (row['reserved'] if not row['released'] else 0) + sum(c['charged_bytes'] for c in copies),
                              'copies': [{'role': c['role'], 'state': c['state'], 'count': c['count'], 'chargedBytes': c['charged_bytes']} for c in copies],
                              'expiresAt': row['expires_at']})
            return {'items': items, 'nextCursor': self._seal('catalogue-cursor', {'selection': ['storage'], 'last': [0, rows[PAGE-1]['id'], -1]}) if len(rows) > PAGE else None,
                    'sourceCount': self.store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],
                    'sourceBytes': source_bytes, 'copyBytes': copy_bytes, 'usedBytes': source_bytes + copy_bytes,
                    'limitBytes': self.store.limits['bytes']}

    def _member(self, selection, owner_kind, owner_id, file_id):
        if type(selection) is not HistorySelection:
            fail('invalid_request', 'Choose an exact retained attachment context.', 400)
        if selection.kind == 'officer':
            conversation = self.store.db.execute('SELECT scope FROM conversations WHERE id=?', (selection.conversation_id,)).fetchone()
            if conversation is None or conversation['scope'] != selection.scope_id:
                fail('forbidden', 'The retained conversation could not be authorized.', 403)
        ident(owner_id); ident(file_id)
        source = self.store.db.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
        if (source is None or source['state'] != 'ready' or not source['member'] or source['deletion_generation']):
            fail('forbidden', 'The attachment source is unavailable.', 403)
        if owner_kind == 'draft':
            row = self.store._draft(owner_id)
            if (row['state'] != 'active' or self.store.admission_kind(row) is not None or source['draft_id'] != owner_id
                    or HistorySelection.from_binding(json.loads(row['destination'])) != selection):
                fail('forbidden', 'The attachment source is unavailable.', 403)
            owner_generation = row['generation']
        elif owner_kind == 'request':
            row = self.store.db.execute('SELECT r.destination,r.epoch,f.* FROM request_files f JOIN attachment_requests r ON r.id=f.request_id WHERE f.request_id=? AND f.source_id=? AND f.catalogued_at IS NOT NULL', (owner_id, file_id)).fetchone()
            if (row is None or row['epoch'] != self.store.owner_epoch
                    or HistorySelection.from_binding(json.loads(row['destination'])) != selection
                    or row['source_generation'] != source['generation'] or row['access_generation'] != source['access_generation']
                    or row['identity'] != source['identity'] or row['digest'] != source['digest']):
                fail('forbidden', 'The attachment source is unavailable.', 403)
            owner_generation = 0
        else:
            fail('forbidden', 'The attachment source is unavailable.', 403)
        return source, owner_generation

    def source_authority(self, selection, owner_kind, owner_id, file_id):
        """Internal only, after authenticated exact draft/history selection."""
        with self.store._gate():
            source, generation = self._member(selection, owner_kind, owner_id, file_id)
            return self._seal('catalogue-source', {'selection': asdict(selection), 'ownerKind': owner_kind, 'ownerId': owner_id,
                 'ownerGeneration': generation, 'fileId': file_id, 'sourceGeneration': source['generation'],
                 'accessGeneration': source['access_generation'], 'deletionGeneration': source['deletion_generation'],
                 'expires': now(self.store.clock) + CAP_SECONDS})

    def draft(self, draft_id, capability):
        with self.store._gate():
            row = self.store._authorize(draft_id, capability)
            result = self.store._snapshot(row)
            for item in result['files']:
                item['sourceCapability'] = None
                if self.store.admission_kind(row) is not None:
                    item['previewAvailable'] = False
                if self.store.admission_kind(row) is None and item['state'] == 'ready':
                    item['sourceCapability'] = self.source_authority(HistorySelection.from_binding(result['destination']), 'draft', draft_id, item['id'])
            return result

    def _source(self, capability, file_id):
        value = self._unseal('catalogue-source', capability)
        fields = {'epoch', 'v', 'selection', 'ownerKind', 'ownerId', 'ownerGeneration', 'fileId', 'sourceGeneration', 'accessGeneration', 'deletionGeneration', 'expires'}
        if (set(value) != fields or value['fileId'] != file_id
                or type(value['expires']) not in (int, float) or not now(self.store.clock) <= value['expires'] <= now(self.store.clock) + CAP_SECONDS):
            fail('forbidden', 'The attachment preview authority expired or changed.', 403)
        selection = HistorySelection(**value['selection'])
        source, generation = self._member(selection, value['ownerKind'], value['ownerId'], file_id)
        if (generation != value['ownerGeneration'] or source['generation'] != value['sourceGeneration']
                or source['access_generation'] != value['accessGeneration'] or source['deletion_generation'] != value['deletionGeneration']):
            fail('forbidden', 'The attachment preview authority changed.', 403)
        return dict(source)

    @contextmanager
    def _handle(self, file_id):
        # Register before releasing DB lock; store.close cannot invalidate IO.
        marker = 'preview:' + secrets.token_hex(16)
        with self.store._gate():
            if len(self.store.request_handles) >= self.store.limits['streams']:
                fail('resource_busy', 'Attachment reads are busy.')
            root = str(self.store.files.root)
            self.store.request_handles.add(marker)
        locks = None
        try:
            locks = ResourceLocks(root, enabled=True)
            with locks.hold(['source:' + file_id]):
                yield
        finally:
            try:
                if locks is not None:
                    locks.close()
            finally:
                with self.store.lock:
                    self.store.request_handles.discard(marker)

    def _read(self, source):
        try:
            return self.store.files.read(source['id'], json.loads(source['identity']), source['digest'], self.store.limits['file_bytes'])
        except OSError:
            fail('source_unavailable', 'The attachment source could not be read.', 409)

    @staticmethod
    def location(value):
        if value is None:
            return {'kind': 'text_lines', 'start': 1, 'count': 100}
        if (type(value) is not dict or set(value) != {'kind', 'start', 'count'} or value['kind'] != 'text_lines'
                or not integer(value['start'], 1, 100000) or not integer(value['count'], 1, 200)):
            fail('invalid_location', 'Choose a bounded text line location.', 400)
        return value

    def preview_snapshot(self, capability, file_id, location=None):
        ident(file_id); location = self.location(location)
        with self.store._gate():
            source = self._source(capability, file_id)
            if source['preparation_kind'] != 'text':
                return PreviewSnapshot(capability, file_id, source['identity'], canonical({'available': False, 'reason': 'Original-file previews are unavailable.'}), False)
        with self._handle(file_id):
            with self.store._gate():
                source = self._source(capability, file_id)
            data = self._read(source)
            prepared = prepare(data, source['media'], max_bytes=self.store.limits['file_bytes'], max_lines=self.store.limits['lines'])
        first = location['start'] - 1
        if first >= prepared['lineCount'] and not (first == 0 and prepared['lineCount'] == 0):
            fail('invalid_location', 'That text line is not present.', 400)
        lines = prepared['lines'][first:first + location['count']]
        # Never return an oversized line partially or pretend a clipped line is complete.
        if len(canonical(lines).encode()) > 131072:
            return PreviewSnapshot(capability, file_id, source['identity'], canonical({'available': False, 'reason': 'This text selection exceeds the preview limit.'}), True)
        payload = {'available': True, 'kind': 'text_lines', 'mediaType': source['media'], 'lineCount': prepared['lineCount'],
                   'start': location['start'], 'lines': lines, 'hasMore': first + len(lines) < prepared['lineCount']}
        return PreviewSnapshot(capability, file_id, source['identity'], canonical(payload), True)

    def revalidate_preview(self, snapshot):
        """Called inside the final publication gate, never an authority for a later send."""
        if type(snapshot) is not PreviewSnapshot:
            fail('forbidden', 'The attachment preview could not be authorized.', 403)
        source = self._source(snapshot.capability, snapshot.file_id)
        if source['identity'] != snapshot.identity:
            fail('forbidden', 'The attachment source changed.', 403)
        if snapshot.read_bytes:
            self._read(source)  # Verify exact private identity and digest again.

    def publish_preview(self, snapshot, publish):
        # Resource order is outside the database lock. Hold the final gate through
        # the bounded HTTP publication so revocation has a defined linearization.
        if type(snapshot) is not PreviewSnapshot:
            fail('forbidden', 'The attachment preview could not be authorized.', 403)
        if not snapshot.read_bytes:
            with self.store._gate():
                self.revalidate_preview(snapshot)
                return publish(json.loads(snapshot.payload))
        with self._handle(snapshot.file_id), self.store._gate():
            self.revalidate_preview(snapshot)
            return publish(json.loads(snapshot.payload))
