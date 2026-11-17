"""Internal Motet local retention. No provider, Send route or reading qualification."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import secrets

from attachments_catalogue import Catalogue, HistorySelection
from attachments_resources import ResourceLocks
from attachments_officer_storage import OfficerDirectRetentionProfile, DIRECT_MEDIA, validate_binding
from attachments_types import ID, OPAQUE, TOKEN, canonical, destination, digest, fail, ident, integer, now

TEXT_MEDIA = ('text/plain', 'text/markdown')
ZERO = '0' * 16


def fixed_number(value):
    if not integer(value, 0):
        fail('storage_unavailable', 'Attachment generation is unavailable.', 503)
    return '%016d' % value


@dataclass(frozen=True)
class OfficerRetentionProfile:
    id: str
    revision: str
    purpose: str
    capability_revision: str
    media_types: tuple = TEXT_MEDIA
    file_count: int = 5
    file_bytes: int = 10 * 1024 * 1024
    draft_bytes: int = 20 * 1024 * 1024

    def __post_init__(self):
        if (any(type(x) is not str or not OPAQUE.fullmatch(x) for x in (self.id, self.revision, self.capability_revision))
                or self.purpose != 'local_retention' or type(self.media_types) is not tuple
                or not 1 <= len(self.media_types) <= 2 or len(set(self.media_types)) != len(self.media_types)
                or any(x not in TEXT_MEDIA for x in self.media_types)
                or not integer(self.file_count, 1, 5) or not integer(self.file_bytes, 1, 10 * 1024 * 1024)
                or not integer(self.draft_bytes, 1, 20 * 1024 * 1024)):
            fail('profile_unavailable', 'Motet local-retention policy is unavailable.', 503)


@dataclass(frozen=True)
class SourceDependency:
    owner_epoch: str
    destination_id: str
    scope_id: str
    conversation_id: str
    message_id: str
    publication_generation: int
    membership_generation: int
    source_id: str
    source_generation: int
    access_generation: int
    deletion_generation: int
    sha256: str
    identity: tuple


def migrate(store):
    db = store.db
    if db.execute('''SELECT 1 FROM drafts d LEFT JOIN attachment_requests r ON r.id=d.admitted_request
        WHERE d.admitted_request IS NOT NULL AND (r.id IS NULL OR r.draft_id<>d.id OR r.authorized_at IS NULL) LIMIT 1''').fetchone():
        fail('schema_unavailable', 'Native attachment admissions could not be verified.', 503)
    statements = [
        '''CREATE TABLE officer_attachment_messages (
          id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL REFERENCES drafts(id),
          destination_id TEXT NOT NULL, scope_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL REFERENCES conversations(id), epoch TEXT NOT NULL,
          intent_fingerprint TEXT NOT NULL, fingerprint TEXT NOT NULL, text TEXT NOT NULL,
          model_id TEXT NOT NULL, capability_revision TEXT NOT NULL, profile TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state='retained_local'), created REAL NOT NULL,
          publication_generation TEXT NOT NULL CHECK(length(publication_generation)=16), padding TEXT NOT NULL
        ) WITHOUT ROWID''',
        '''CREATE TABLE officer_message_files (
          message_id TEXT NOT NULL REFERENCES officer_attachment_messages(id), ordinal INTEGER NOT NULL,
          source_id TEXT UNIQUE NOT NULL REFERENCES files(id), source_generation INTEGER NOT NULL,
          access_generation INTEGER NOT NULL, identity TEXT NOT NULL, digest TEXT NOT NULL,
          media TEXT NOT NULL, bytes INTEGER NOT NULL, name TEXT NOT NULL,
          preparation_kind TEXT NOT NULL CHECK(preparation_kind='text'),
          PRIMARY KEY(message_id,ordinal)
        ) WITHOUT ROWID''',
        '''CREATE TABLE conversation_files (
          conversation_id TEXT NOT NULL REFERENCES conversations(id), source_id TEXT UNIQUE NOT NULL REFERENCES files(id),
          admission_message_id TEXT NOT NULL REFERENCES officer_attachment_messages(id),
          source_generation INTEGER NOT NULL, access_generation INTEGER NOT NULL,
          membership_generation TEXT NOT NULL CHECK(length(membership_generation)=16),
          available INTEGER NOT NULL CHECK(available IN (0,1)), retained_at REAL NOT NULL,
          revoked_at TEXT NOT NULL CHECK(length(revoked_at)=16),
          cause TEXT NOT NULL CHECK(length(cause)=32), padding TEXT NOT NULL,
          PRIMARY KEY(conversation_id,source_id)
        ) WITHOUT ROWID''',
        'CREATE INDEX officer_context ON officer_attachment_messages(destination_id,scope_id,conversation_id,created,id)',
        "ALTER TABLE drafts ADD COLUMN admitted_kind TEXT CHECK(admitted_kind IN ('native','officer'))",
        'ALTER TABLE drafts ADD COLUMN admitted_message TEXT REFERENCES officer_attachment_messages(id)',
        "UPDATE drafts SET admitted_kind='native' WHERE admitted_request IS NOT NULL",
        'DROP INDEX draft_listing',
        "CREATE INDEX draft_listing ON drafts(destination_id,scope_id,created,id) WHERE state='active' AND admitted_kind IS NULL",
    ]
    for statement in statements:
        db.execute(statement)
    valid = '''((NEW.admitted_kind IS NULL AND NEW.admitted_request IS NULL AND NEW.admitted_message IS NULL)
      OR (NEW.admitted_kind='native' AND NEW.admitted_request IS NOT NULL AND NEW.admitted_message IS NULL
          AND EXISTS(SELECT 1 FROM attachment_requests r WHERE r.id=NEW.admitted_request AND r.draft_id=NEW.id AND r.authorized_at IS NOT NULL))
      OR (NEW.admitted_kind='officer' AND NEW.admitted_message IS NOT NULL AND NEW.admitted_request IS NULL
          AND EXISTS(SELECT 1 FROM officer_attachment_messages m WHERE m.id=NEW.admitted_message AND m.draft_id=NEW.id)))'''
    for action in ('INSERT', 'UPDATE'):
        db.execute('CREATE TRIGGER draft_admission_' + action.lower() + ' BEFORE ' + action +
                   " ON drafts WHEN NOT COALESCE(" + valid + ",0) BEGIN SELECT RAISE(ABORT,'invalid attachment admission'); END")


class OfficerConversations:
    """Same private ledger, internal owner-authorized caller only. No HTTP methods."""
    def __init__(self, store):
        self.store = store
        self.catalogue = Catalogue(store)

    def _selection(self, selection):
        if type(selection) is not HistorySelection or selection.kind != 'officer':
            fail('forbidden', 'Choose an exact retained Motet conversation.', 403)
        self.catalogue._selection(selection)

    def _message(self, message_id):
        row = self.store.db.execute('SELECT * FROM officer_attachment_messages WHERE id=?', (ident(message_id),)).fetchone()
        if row is None:
            fail('forbidden', 'The retained Motet message could not be authorized.', 403)
        return row

    @staticmethod
    def _matches(row, selection):
        return (row['destination_id'], row['scope_id'], row['conversation_id']) == (selection.destination_id, selection.scope_id, selection.conversation_id)

    def _sources(self, draft_id):
        return [dict(row) for row in self.store.db.execute('SELECT * FROM files WHERE draft_id=? AND member=1 ORDER BY rowid', (draft_id,))]

    def _validate(self, draft, binding, profile, sources):
        if binding != json.loads(draft['destination']) or binding['kind'] != 'officer' or profile.capability_revision != binding['capabilityRevision']:
            fail('capability_changed', 'This Motet retention context changed.')
        self._selection(HistorySelection.from_binding(binding))
        if type(profile) is OfficerDirectRetentionProfile:
            validate_binding(profile, binding)
        preparation = 'officer_direct' if type(profile) is OfficerDirectRetentionProfile else 'text'
        if (profile.file_count > self.store.limits['files'] or profile.file_bytes > self.store.limits['file_bytes']
                or profile.draft_bytes > self.store.limits['draft_bytes']):
            fail('profile_unavailable', 'Motet local-retention limits are unavailable.', 503)
        if not 1 <= len(sources) <= profile.file_count or sum(s['bytes'] for s in sources) > profile.draft_bytes:
            fail('files_required', 'Choose a bounded set of complete Motet attachments.')
        from attachments_deletion_slots import verify_source
        for source in sources:
            verify_source(self.store, source['id'])
            if (source['state'] != 'ready' or source['identity'] is None or source['deletion_generation']
                    or source['preparation_kind'] != preparation or source['media'] not in profile.media_types
                    or source['bytes'] > profile.file_bytes):
                fail('source_unavailable', 'A Motet attachment is unavailable for local retention.')

    @contextmanager
    def _reading(self, sources):
        marker = 'officer-retention:' + secrets.token_hex(16)
        with self.store._gate():
            if len(self.store.request_handles) >= self.store.limits['streams']:
                fail('resource_busy', 'Attachment reads are busy.')
            root = str(self.store.files.root)
            self.store.request_handles.add(marker)
        locks = None
        try:
            locks = ResourceLocks(root, enabled=True)
            with locks.hold(sorted('source:' + s['id'] for s in sources)):
                yield
        finally:
            try:
                if locks is not None:
                    locks.close()
            finally:
                with self.store.lock:
                    self.store.request_handles.discard(marker)

    def admit(self, draft_id, draft_capability, fence, command_key, resolved_binding, retention_profile):
        if type(retention_profile) not in (OfficerRetentionProfile, OfficerDirectRetentionProfile):
            fail('profile_unavailable', 'Motet local-retention policy is unavailable.', 503)
        binding = destination(resolved_binding)
        profile = retention_profile
        intent = digest({'draft': draft_id, 'fence': fence, 'binding': binding, 'profile': asdict(profile)})
        with self.store._gate():
            self.store._authorize(draft_id, draft_capability)
            prior = self.store._seen(command_key, 'officer_attachment_admission', intent)
            if prior:
                return self._snapshot(self._message(prior['target']))
            draft = self.store._authorize(draft_id, draft_capability, fence=fence)
            sources = self._sources(draft_id)
            self._validate(draft, binding, profile, sources)
            text = draft['text']
        with self._reading(sources):
            for source in sources:
                try:
                    self.store.files.read(source['id'], json.loads(source['identity']), source['digest'], self.store.limits['file_bytes'])
                except OSError:
                    fail('source_unavailable', 'The Motet attachment source could not be verified.')
            self.store.checkpoint('officer_sources_verified')
            with self.store._gate(), self.store._transaction():
                # Same-key contenders recover the winner before demanding a writable draft.
                self.store._authorize(draft_id, draft_capability)
                prior = self.store._seen(command_key, 'officer_attachment_admission', intent)
                if prior:
                    return self._snapshot(self._message(prior['target']))
                draft = self.store._authorize(draft_id, draft_capability, fence=fence)
                if sources != self._sources(draft_id) or draft['text'] != text:
                    fail('source_changed', 'The Motet attachment selection changed.')
                self._validate(draft, binding, profile, sources)
                if self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0] >= self.store.limits['officer_messages']:
                    fail('message_capacity', 'Motet attachment history has reached capacity.', 429)
                if self.store.db.execute('SELECT COUNT(*) FROM conversation_files').fetchone()[0] + len(sources) > self.store.limits['objects']:
                    fail('file_capacity', 'Motet attachment membership has reached capacity.', 429)
                self.store._capacity()
                message_id = secrets.token_hex(16); ts = now(self.store.clock)
                frozen = [{k: s[k] for k in ('id','generation','access_generation','identity','digest','media','bytes','name','preparation_kind')} for s in sources]
                fingerprint = digest({'intent': intent, 'text': text, 'sources': frozen})
                self.store.db.execute('''INSERT INTO officer_attachment_messages
                    (id,draft_id,destination_id,scope_id,conversation_id,epoch,intent_fingerprint,fingerprint,text,model_id,capability_revision,profile,state,created,publication_generation,padding)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (message_id,draft_id,binding['id'],binding['scopeId'],binding['conversationId'],self.store.owner_epoch,intent,fingerprint,text,
                     binding['modelId'],binding['capabilityRevision'],canonical(asdict(profile)),'retained_local',ts,fixed_number(1),'0'*1024))
                for ordinal, source in enumerate(sources):
                    self.store.db.execute('INSERT INTO officer_message_files VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (message_id,ordinal,source['id'],source['generation'],source['access_generation'],source['identity'],source['digest'],source['media'],source['bytes'],source['name'],source['preparation_kind']))
                    self.store.db.execute('INSERT INTO conversation_files VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (binding['conversationId'],source['id'],message_id,source['generation'],source['access_generation'],fixed_number(1),1,ts,ZERO,' '*32,'0'*256))
                self.store.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)', (command_key,'officer_attachment_admission',intent,message_id,self.store.owner_epoch))
                changed = self.store.db.execute("""UPDATE drafts SET admitted_kind='officer',admitted_message=?,revision=revision+1,
                    writer_hash='',recovery_hash=NULL,recovery_until=NULL,updated=?
                    WHERE id=? AND admitted_kind IS NULL AND admitted_request IS NULL AND admitted_message IS NULL""", (message_id,ts,draft_id))
                if changed.rowcount != 1:
                    fail('draft_admitted', 'This draft already belongs to an attachment admission.')
                self.store.checkpoint('officer_admission_before_commit')
                result = self._snapshot(self._message(message_id))
        self.store.checkpoint('officer_admission_committed')
        return result

    _JOIN = '''FROM officer_message_files mf
        JOIN officer_attachment_messages m ON m.id=mf.message_id
        JOIN conversation_files cf ON cf.source_id=mf.source_id AND cf.admission_message_id=m.id
        JOIN files f ON f.id=mf.source_id'''
    _COLUMNS = '''mf.*,m.destination_id,m.scope_id,m.conversation_id,m.created,m.model_id,m.publication_generation,
        cf.membership_generation,cf.source_generation AS member_source_generation,cf.access_generation AS member_access_generation,cf.available,cf.revoked_at,cf.cause,
        f.state AS source_state,f.member,f.generation AS current_generation,f.access_generation AS current_access,
        f.deletion_generation,f.media AS current_media,f.identity AS current_identity,f.digest AS current_digest,f.preparation_kind AS current_preparation'''
    _AVAILABLE = '''cf.available=1 AND f.state='ready' AND f.member=1 AND f.deletion_generation=0
        AND f.generation=mf.source_generation AND f.access_generation=mf.access_generation
        AND cf.source_generation=mf.source_generation AND cf.access_generation=mf.access_generation
        AND f.identity=mf.identity AND f.digest=mf.digest AND f.media=mf.media AND f.preparation_kind=mf.preparation_kind
        AND ((f.preparation_kind='text' AND f.media IN ('text/plain','text/markdown'))
          OR (f.preparation_kind='officer_direct' AND f.media IN ('application/pdf','image/png'))) '''

    def _available(self, row):
        return (bool(row['available']) and row['source_state'] == 'ready' and row['member'] == 1
                and row['deletion_generation'] == 0 and row['source_generation'] == row['current_generation']
                and row['access_generation'] == row['current_access']
                and row['member_source_generation'] == row['source_generation'] and row['member_access_generation'] == row['access_generation']
                and row['identity'] == row['current_identity']
                and row['digest'] == row['current_digest'] and row['current_media'] == row['media'] and row['current_preparation'] == row['preparation_kind']
                and (row['current_preparation'] == 'text' and row['media'] in TEXT_MEDIA
                     or row['current_preparation'] == 'officer_direct' and row['media'] in DIRECT_MEDIA))

    def _file_view(self, row):
        return {'id': row['source_id'], 'messageId': row['message_id'], 'name': row['name'], 'mediaType': row['media'],
                'preparationKind': row['preparation_kind'], 'previewAvailable': row['preparation_kind'] == 'text' and self._available(row),
                'bytes': row['bytes'], 'retainedAt': row['created'], 'available': bool(self._available(row)),
                'revokedAt': int(row['revoked_at']) / 1000 if int(row['revoked_at']) else None,
                'reason': row['cause'].strip() or None}

    def _snapshot(self, message):
        rows = self.store.db.execute('SELECT ' + self._COLUMNS + ' ' + self._JOIN + ' WHERE m.id=? ORDER BY mf.ordinal', (message['id'],)).fetchall()
        return {'id': message['id'], 'state': 'retained_local', 'retainedLocally': True, 'modelCallStarted': False,
                'destinationId': message['destination_id'], 'scopeId': message['scope_id'], 'conversationId': message['conversation_id'],
                'createdAt': message['created'], 'modelId': message['model_id'], 'text': message['text'],
                'files': [self._file_view(row) for row in rows]}

    def message(self, selection, message_id):
        with self.store._gate():
            self._selection(selection)
            row = self._message(message_id)
            if not self._matches(row, selection):
                fail('forbidden', 'The retained Motet message belongs to another context.', 403)
            return self._snapshot(row)

    def _page(self, selection, cursor, *, context):
        self._selection(selection)
        filters = ['officer-context' if context else 'officer-history', asdict(selection)]
        last = self.catalogue._cursor(filters, cursor)
        args = (selection.destination_id,selection.scope_id,selection.conversation_id)
        where = 'm.destination_id=? AND m.scope_id=? AND m.conversation_id=?'
        totals = self.store.db.execute('SELECT COUNT(*),COALESCE(SUM(mf.bytes),0) ' + self._JOIN + ' WHERE ' + where + ' AND ' + self._AVAILABLE, args).fetchone()
        total = self.store.db.execute('SELECT COUNT(*) ' + self._JOIN + ' WHERE ' + where, args).fetchone()[0]
        if context:
            query = ' AND ' + self._AVAILABLE + ' AND mf.source_id>? ORDER BY mf.source_id LIMIT 51'
            seek = (last[1] if cursor else '',)
        else:
            query = ' AND (m.created>? OR (m.created=? AND (m.id>? OR (m.id=? AND mf.ordinal>?)))) ORDER BY m.created,m.id,mf.ordinal LIMIT 51'
            seek = (last[0],last[0],last[1],last[1],last[2])
        rows = self.store.db.execute('SELECT ' + self._COLUMNS + ' ' + self._JOIN + ' WHERE ' + where + query, args + seek).fetchall()
        next_cursor = None
        if len(rows) > 50:
            tail = rows[49]
            key = [0,tail['source_id'],-1] if context else [tail['created'],tail['message_id'],tail['ordinal']]
            next_cursor = self.catalogue._seal('catalogue-cursor', {'selection': filters, 'last': key})
        return rows[:50], {'nextCursor': next_cursor, 'associationCount': total, 'availableCount': totals[0], 'availableBytes': totals[1]}

    def conversation_files(self, selection, *, cursor=None):
        with self.store._gate():
            rows, result = self._page(selection, cursor, context=False)
            return dict(result, items=[self._file_view(row) for row in rows])

    def _dependency(self, row):
        return SourceDependency(self.store.owner_epoch,row['destination_id'],row['scope_id'],row['conversation_id'],row['message_id'],
            int(row['publication_generation']),int(row['membership_generation']),row['source_id'],row['source_generation'],
            row['access_generation'],row['deletion_generation'],row['digest'],tuple(json.loads(row['identity'])))

    def context_sources(self, selection, *, cursor=None):
        with self.store._gate():
            rows, result = self._page(selection, cursor, context=True)
            return dict(result, items=[self._file_view(row) for row in rows], dependencies=tuple(self._dependency(row) for row in rows))

    def validate_dependencies(self, selection, dependencies):
        """Validate the supplied unique subset only, not an absent selection manifest."""
        if type(dependencies) not in (tuple,list) or len(dependencies) > 50:
            fail('invalid_dependencies', 'Use a bounded Motet attachment dependency set.', 400)
        ids = set()
        with self.store._gate():
            self._selection(selection)
            for value in dependencies:
                if (type(value) is not SourceDependency
                        or any(type(x) is not str or not ID.fullmatch(x) for x in (value.source_id,value.message_id,value.conversation_id))
                        or value.source_id in ids
                        or any(type(x) is not str or not OPAQUE.fullmatch(x) for x in (value.destination_id,value.scope_id))
                        or any(type(x) is not str or not TOKEN.fullmatch(x) for x in (value.owner_epoch,value.sha256))
                        or any(not integer(x,1) for x in (value.publication_generation,value.membership_generation,value.source_generation,value.access_generation))
                        or not integer(value.deletion_generation,0)
                        or type(value.identity) is not tuple or len(value.identity) != 5 or any(not integer(x,0,2**63-1) for x in value.identity)):
                    fail('invalid_dependencies', 'Invalid Motet attachment dependency.', 400)
                ident(value.source_id);ids.add(value.source_id)
                row = self.store.db.execute('SELECT ' + self._COLUMNS + ' ' + self._JOIN + ' WHERE mf.source_id=?', (value.source_id,)).fetchone()
                if row is None or not self._matches(row,selection) or not self._available(row) or self._dependency(row) != value:
                    fail('source_changed', 'A Motet attachment dependency changed.')
            return True

    def invalidate_source_in_transaction(self, source_id):
        """B4c participant only: source revocation must already exist in this transaction."""
        ident(source_id)
        with self.store._gate():
            if not self.store.db.in_transaction:
                fail('transaction_required', 'Attachment invalidation needs its source transaction.')
            source = self.store.db.execute('SELECT * FROM files WHERE id=?', (source_id,)).fetchone()
            if source is None:
                fail('source_changed', 'The attachment source could not be verified.')
            rows = self.store.db.execute('SELECT * FROM conversation_files WHERE source_id=?', (source_id,)).fetchall()
            if (not rows and not source['deletion_generation'] and source['access_generation'] <= 1
                    or any(source['generation'] < row['source_generation'] or source['access_generation'] < row['access_generation']
                           or not source['deletion_generation'] and source['access_generation'] <= row['access_generation'] for row in rows)):
                fail('revocation_required', 'An authoritative source revocation is required.')
            affected = []
            for row in rows:
                affected.append({'messageId':row['admission_message_id'],'conversationId':row['conversation_id']})
                if not row['available']:
                    continue
                message = self._message(row['admission_message_id'])
                self.store.db.execute("UPDATE conversation_files SET available=0,membership_generation=?,revoked_at=?,cause=?,padding='' WHERE source_id=?", (
                    fixed_number(int(row['membership_generation'])+1),fixed_number(int(now(self.store.clock)*1000)),'source_unavailable'.ljust(32),source_id))
                self.store.db.execute("UPDATE officer_attachment_messages SET publication_generation=?,padding='' WHERE id=?", (
                    fixed_number(int(message['publication_generation'])+1),message['id']))
            if self.store.db.execute('PRAGMA user_version').fetchone()[0] >= 9:
                from officer_document_ledger import invalidate_source_in_transaction
                invalidate_source_in_transaction(self.store,source_id)
            return affected
