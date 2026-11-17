"""Owner-confirmed deletion of one retained Officer original, without providers.

Only existing fixed slots are updated. Historical associations and provenance
remain immutable. Disk removal follows committed source/publication revocation.
"""
from dataclasses import asdict
import hashlib
import hmac
import json
import threading

from attachments_catalogue import Catalogue, HistorySelection
from attachments_conversations import OfficerConversations
from attachments_deletion_slots import SOURCE_PREVIEW, SOURCE_RECEIPT, verify_source
from attachments_types import AttachmentError, TOKEN, digest, fail, ident, integer, key, now

CONFIRM, DELETING, DELETED, PARTIAL, UNKNOWN = range(1, 6)
STATES = {CONFIRM: 'confirm', DELETING: 'deleting', DELETED: 'deleted', PARTIAL: 'partial_failure', UNKNOWN: 'unknown'}
TTL_MS = 300000


def refuse(code='deletion_changed', status=409):
    fail(code, 'The exact retained file deletion could not be verified. Refresh Files.', status)


class OfficerDeletion:
    def __init__(self, store):
        self.store = store
        self.catalogue = Catalogue(store)
        self.conversations = OfficerConversations(store)

    def selection(self, destination_id, scope_id, conversation_id):
        if destination_id != 'officer': refuse('forbidden', 403)
        selection = HistorySelection('officer', destination_id, scope_id, conversation_id)
        self.conversations._selection(selection)
        return selection

    def _canonical(self, source_id, selection=None):
        """Establish ownership independently of matching slot/checksum bytes."""
        source = self.store.db.execute('SELECT * FROM files WHERE id=?', (ident(source_id),)).fetchone()
        rows = self.store.db.execute('SELECT ' + self.conversations._COLUMNS + ' ' + self.conversations._JOIN +
            ' WHERE mf.source_id=?', (source_id,)).fetchall()
        if source is None or len(rows) != 1: refuse('forbidden', 403)
        row = rows[0]
        actual = self.selection(row['destination_id'], row['scope_id'], row['conversation_id'])
        if selection is not None and actual != selection: refuse('forbidden', 403)
        draft = self.store._draft(source['draft_id'])
        message = self.conversations._message(row['message_id'])
        reservation = self.store.db.execute('SELECT * FROM reservations WHERE file_id=?', (source_id,)).fetchone()
        if (draft['admitted_kind'] != 'officer' or draft['admitted_message'] != message['id']
                or draft['admitted_request'] is not None or message['draft_id'] != draft['id']
                or HistorySelection.from_binding(json.loads(draft['destination'])) != actual
                or source['preparation_kind'] != 'officer_direct' or source['media'] not in ('application/pdf', 'image/png')
                or row['preparation_kind'] != source['preparation_kind'] or row['media'] != source['media']
                or row['bytes'] != source['bytes'] or row['digest'] != source['digest'] or row['identity'] != source['identity']
                or row['name'] != source['name'] or reservation is None or reservation['bytes'] != source['bytes']
                or self.store.db.execute('SELECT 1 FROM request_files WHERE source_id=? LIMIT 1', (source_id,)).fetchone()
                or self.store.db.execute('SELECT 1 FROM workspace_copies WHERE source_id=? LIMIT 1', (source_id,)).fetchone()):
            refuse('deletion_source_unavailable')
        slot_id = verify_source(self.store, source_id)
        return source, row, actual, slot_id

    def _slot(self, slot_id):
        row = self.store.db.execute('SELECT * FROM deletion_source_slots WHERE slot_id=?', (ident(slot_id),)).fetchone()
        if row is None: refuse('forbidden', 403)
        source, member, selection, verified = self._canonical(row['source_id'])
        if verified != slot_id: refuse()
        preview, receipt = SOURCE_PREVIEW.decode(row['preview']), SOURCE_RECEIPT.decode(row['receipt'])
        if preview['state'] not in (0, CONFIRM) or receipt['state'] not in (0, DELETING, DELETED, PARTIAL, UNKNOWN):
            refuse('cleanup_storage_unavailable', 503)
        for value in (preview, receipt):
            if not value['state']: continue
            if (value['operation_id'] != slot_id or value['origin_id'] != member['message_id']
                    or value['context_fingerprint'] != digest(asdict(selection))
                    or value['source_generation'] != member['source_generation']
                    or value['access_generation'] != member['access_generation']
                    or value['identity'] != tuple(json.loads(source['identity']))
                    or value['selection_fingerprint'] != self.fingerprint(value)
                    or value['preview_generation'] < 1 or value['operation_generation'] < value['preview_generation']):
                refuse('cleanup_storage_unavailable', 503)
        if receipt['state']:
            reservation = self.store.db.execute('SELECT * FROM reservations WHERE file_id=?', (source['id'],)).fetchone()
            if (self.fingerprint(preview) != self.fingerprint(receipt)
                    or source['access_generation'] != receipt['fenced_access_generation']
                    or source['deletion_generation'] != receipt['fenced_deletion_generation']
                    or member['available'] or receipt['fenced_access_generation'] != receipt['access_generation'] + 1
                    or receipt['fenced_deletion_generation'] != receipt['deletion_generation'] + 1
                    or receipt['released'] != reservation['released']
                    or receipt['released_bytes'] != (source['bytes'] if receipt['released'] else 0)
                    or (receipt['state'] == DELETED) != bool(receipt['released'])
                    or receipt['state'] == DELETED and not receipt['handles_closed']):
                refuse('cleanup_storage_unavailable', 503)
        return row, source, member, selection, preview, receipt

    @staticmethod
    def _snapshot(source, member):
        return digest({'source': {k: source[k] for k in ('id', 'draft_id', 'name', 'media', 'bytes', 'digest',
            'identity', 'generation', 'access_generation', 'deletion_generation', 'expiry_generation', 'state', 'member')},
            'member': {k: member[k] for k in ('message_id', 'source_generation', 'access_generation',
                'membership_generation', 'publication_generation', 'member_source_generation', 'member_access_generation',
                'destination_id', 'scope_id', 'conversation_id', 'available')}})

    @staticmethod
    def fingerprint(value):
        return digest([value[k] for k in ('slot_id', 'source_id', 'owner_epoch', 'context_fingerprint',
            'origin_fingerprint', 'preview_generation', 'prepared_ms', 'expires_ms')])

    def _capability(self, value):
        return self.store._token('officer-deletion:' + self.fingerprint(value), value['slot_id'], value['preview_generation'])

    def _auth(self, value, capability):
        if type(capability) is not str or not hmac.compare_digest(capability, self._capability(value)):
            refuse('forbidden', 403)

    def _pending_sources(self, selection):
        """Positive frozen source evidence only, never a conversation-wide guess.

        Public intent consumption is durable. A transport permit claim alone
        does not prove transmission. These read-only validators need neither
        current model policy nor credentials and never restore answer prose.
        """
        from officer_document_intents import OfficerIntents
        from officer_document_ledger import OfficerDocumentLedger
        execution = OfficerDocumentLedger(self.store)
        intents = OfficerIntents(self.store, execution, None, None)
        maximum = self.store.limits['requests']
        rows = self.store.db.execute('SELECT id FROM officer_document_intents WHERE scope_id=? AND conversation_id=? ORDER BY id LIMIT ?',
            (selection.scope_id, selection.conversation_id, maximum + 1)).fetchall()
        if len(rows) > maximum: refuse('deletion_evidence_unavailable', 503)
        sources = set()
        for row in rows:
            try:
                record, receipt = intents._record(row['id'])
                if receipt['phase'] == 'prepared' or receipt['localVacant'] or not record['question'].strip(): continue
                _, current = intents.correlated(record)
                if current is None or not (current['state'] in ('claimed', 'unknown')
                        or current['state'] == 'prepared' and receipt['phase'] == 'running'): continue
                _, frozen, _, dependencies = execution._request(selection, current['id'])
                if frozen['selection'] != asdict(selection): continue
                sources.update(dep.source_id for dep in dependencies)
            except AttachmentError:
                # Missing/corrupt/old-epoch correlation cannot establish a
                # positive warning, and must not remove management permission.
                continue
        return sources

    def _view(self, source, member, value, pending=None):
        if value['state'] not in STATES: refuse('deletion_unavailable')
        committed = value['state'] != CONFIRM
        can_confirm = (not committed and value['owner_epoch'] == self.store.owner_epoch
            and int(now(self.store.clock) * 1000) <= value['expires_ms']
            and self.conversations._available(member) and self._snapshot(source, member) == value['origin_fingerprint'])
        reason = {0: None, 1: 'source_in_use', 2: 'identity_changed', 3: 'removal_unconfirmed'}.get(value['component_result'])
        if value['component_result'] > 3: refuse()
        if pending is None:
            pending = self._pending_sources(HistorySelection('officer', member['destination_id'], member['scope_id'], member['conversation_id']))
        return dict(id=value['slot_id'], fileId=source['id'], conversationId=member['conversation_id'],
            scopeId=member['scope_id'], name=source['name'], bytes=source['bytes'],
            revision=value['operation_generation'] if committed else value['preview_generation'],
            state=STATES[value['state']], releasedBytes=value['released_bytes'], sourceUnavailable=committed,
            hasUnresolvedQuestion=source['id'] in pending,
            removedAt=value['removed_ms'] / 1000 if value['state'] == DELETED else None,
            reason=reason, canConfirm=bool(can_confirm), fingerprint=self.fingerprint(value), capability=self._capability(value))

    def management(self, destination_id, scope_id, conversation_id, cursor=None):
        with self.store._gate():
            selection = self.selection(destination_id, scope_id, conversation_id)
            filters = {'context': asdict(selection)}
            last = ''
            if cursor is not None:
                value = self.catalogue._unseal('officer-management-cursor', cursor)
                if set(value) != {'context', 'last', 'epoch', 'v'} or value['context'] != filters['context']:
                    refuse('invalid_cursor', 400)
                last = ident(value['last'])
            rows = self.store.db.execute('''SELECT cf.source_id FROM conversation_files cf
                JOIN officer_attachment_messages m ON m.id=cf.admission_message_id
                JOIN files f ON f.id=cf.source_id WHERE m.destination_id=? AND m.scope_id=? AND m.conversation_id=?
                AND f.preparation_kind='officer_direct' AND f.media IN ('application/pdf','image/png')
                AND cf.source_id>? ORDER BY cf.source_id LIMIT 51''',
                (destination_id, scope_id, conversation_id, last)).fetchall()
            files, operations = [], []
            pending = self._pending_sources(selection)
            for item in rows[:50]:
                source, member, _, slot_id = self._canonical(item['source_id'], selection)
                _, _, _, _, preview, receipt = self._slot(slot_id)
                value = receipt if receipt['state'] else preview
                files.append(dict(id=source['id'], name=source['name'], mediaType=source['media'], bytes=source['bytes'],
                    available=bool(self.conversations._available(member)), managementGeneration=preview['preview_generation'],
                    hasUnresolvedQuestion=source['id'] in pending,
                    deletionId=slot_id if value['state'] else None))
                if value['state']: operations.append(self._view(source, member, value, pending))
            next_cursor = self.catalogue._seal('officer-management-cursor', dict(filters, last=rows[49]['source_id'])) if len(rows) > 50 else None
            return dict(files=files, deletions=operations, nextCursor=next_cursor)

    def prepare(self, source_id, destination_id, scope_id, conversation_id, generation, command_key):
        key(command_key)
        if not integer(generation): refuse('invalid_request', 400)
        command = hashlib.sha256(command_key.encode()).hexdigest()
        with self.store._gate(), self.store._transaction(reclaim=True):
            selection = self.selection(destination_id, scope_id, conversation_id)
            source, member, _, slot_id = self._canonical(source_id, selection)
            _, _, _, _, preview, receipt = self._slot(slot_id)
            context = digest(asdict(selection))
            if receipt['state']: refuse('deletion_started')
            if (preview['state'] == CONFIRM and preview['preview_generation'] == generation + 1
                    and preview['prepare_key_digest'] == command and preview['context_fingerprint'] == context
                    and preview['owner_epoch'] == self.store.owner_epoch):
                return self._view(source, member, preview)
            if preview['preview_generation'] != generation: refuse()
            if not self.conversations._available(member): refuse('deletion_source_unavailable')
            timestamp = int(now(self.store.clock) * 1000)
            value = dict(preview, state=CONFIRM, cause=1, origin_kind=1, operation_id=slot_id, origin_id=member['message_id'],
                preview_generation=generation + 1, operation_generation=generation + 1, source_generation=source['generation'],
                access_generation=source['access_generation'], deletion_generation=source['deletion_generation'],
                policy_generation=source['expiry_generation'], membership_generation=int(member['membership_generation']),
                publication_generation=int(member['publication_generation']), prepared_ms=timestamp, expires_ms=timestamp + TTL_MS,
                identity_present=1, identity=tuple(json.loads(source['identity'])), owner_epoch=self.store.owner_epoch,
                context_fingerprint=context, prepare_key_digest=command, origin_fingerprint=self._snapshot(source, member))
            value['selection_fingerprint'] = self.fingerprint(value)
            value['capability_digest'] = self.store._hash(self._capability(value))
            self.store.db.execute('UPDATE deletion_source_slots SET preview=? WHERE slot_id=?', (SOURCE_PREVIEW.encode(value), slot_id))
            self.store.checkpoint('officer_deletion_prepare_before_commit')
            return self._view(source, member, value)

    def status(self, slot_id, capability):
        with self.store._gate():
            _, source, member, _, preview, receipt = self._slot(slot_id)
            value = receipt if receipt['state'] else preview
            self._auth(value, capability)
            return self._view(source, member, value)

    def confirm(self, slot_id, capability, fingerprint, command_key):
        key(command_key)
        if type(fingerprint) is not str or not TOKEN.fullmatch(fingerprint): refuse('invalid_request', 400)
        command = hashlib.sha256(command_key.encode()).hexdigest()
        with self.store._gate(), self.store._transaction(reclaim=True):
            _, source, member, _, preview, receipt = self._slot(slot_id)
            value = receipt if receipt['state'] else preview
            self._auth(value, capability)
            if self.fingerprint(value) != fingerprint: refuse()
            if receipt['state']:
                if receipt['confirm_key_digest'] != command: refuse()
                return self._view(source, member, receipt)
            if not self._view(source, member, preview)['canConfirm']: refuse()
            value = dict(preview, state=DELETING, operation_generation=preview['preview_generation'] + 1,
                confirm_key_digest=command, intent_ms=int(now(self.store.clock) * 1000),
                fenced_source_generation=source['generation'], fenced_access_generation=source['access_generation'] + 1,
                fenced_deletion_generation=source['deletion_generation'] + 1)
            # Keep provenance and indexed cleanup flags unchanged. The receipt is
            # the sole executor authority; generic admitted retention stays intact.
            self.store.db.execute('''UPDATE files SET access_generation=access_generation+1,
                deletion_generation=deletion_generation+1,cleanup_reserve=substr(cleanup_reserve,1,128) WHERE id=?''', (source['id'],))
            self.conversations.invalidate_source_in_transaction(source['id'])
            self.store.db.execute('UPDATE deletion_source_slots SET receipt=? WHERE slot_id=?', (SOURCE_RECEIPT.encode(value), slot_id))
            self.store.checkpoint('officer_deletion_confirm_before_commit')
            result = self._view(source, member, value)
        self.store.files.wake_cleanup()
        return result

    def execute(self, slot_id, *, recover=False):
        """Recorded work only. UNKNOWN is retried once on process restart."""
        with self.store._gate():
            _, source, member, _, _, receipt = self._slot(slot_id)
            if receipt['state'] != DELETING and not (recover and receipt['state'] == UNKNOWN): return
            if (source['access_generation'] != receipt['fenced_access_generation']
                    or source['deletion_generation'] != receipt['fenced_deletion_generation']
                    or receipt['identity'] != tuple(json.loads(source['identity']))
                    or self.conversations._available(member)): refuse()
            _, ownership = self.store.files.cleanup_registry(source['id'])
            outcome = 'uncertain' if ownership == 'uncertain' else 'busy' if ownership == 'busy' else None
            if outcome is None:
                # A crash/DB failure after starting IO must not look like an
                # ordinary busy retry. Only restart reconciles this durable
                # attempt marker against the original identity/durable absence.
                receipt = dict(receipt, state=UNKNOWN, component_result=3,
                               operation_generation=receipt['operation_generation'] + 1)
                with self.store._transaction(reclaim=True):
                    self.store.db.execute('UPDATE deletion_source_slots SET receipt=? WHERE slot_id=?',
                                          (SOURCE_RECEIPT.encode(receipt), slot_id))
                try:
                    outcome = self.store.files.remove(source['id'], list(receipt['identity']), receipt['digest'], durable_absence=True)
                except Exception:
                    outcome = 'uncertain'
            state, reason = (DELETED, 0) if outcome in ('removed', 'absent') else (DELETING, 1) if outcome == 'busy' else (PARTIAL, 2) if outcome == 'changed' else (UNKNOWN, 3)
            with self.store._transaction(reclaim=True):
                # IO is serialized by the store gate. No other executor can own
                # this source between physical proof and its accounting receipt.
                value = dict(receipt, state=state, component_result=reason,
                    operation_generation=receipt['operation_generation'] + int((state, reason) != (receipt['state'], receipt['component_result'])))
                if state == DELETED:
                    reservation = self.store.db.execute('SELECT * FROM reservations WHERE file_id=?', (source['id'],)).fetchone()
                    if not reservation['released']:
                        self.store.db.execute('UPDATE quota SET bytes=bytes-? WHERE id=1', (reservation['bytes'],))
                        self.store.db.execute('UPDATE reservations SET released=1 WHERE file_id=?', (source['id'],))
                    value.update(released=1, released_bytes=source['bytes'], handles_closed=1, removed_ms=int(now(self.store.clock) * 1000))
                self.store.db.execute('UPDATE deletion_source_slots SET receipt=? WHERE slot_id=?', (SOURCE_RECEIPT.encode(value), slot_id))
                self.store.checkpoint('officer_deletion_result_before_commit')

    def scan(self, *, recover=False, maximum=50, seen=None, after=None, through=None):
        """One immutable-key batch; caller bounds complete passes without timers."""
        with self.store._gate():
            cursor = self.store.db.execute("SELECT value FROM meta WHERE key='deletion_cursor'").fetchone()[0] if after is None else after
            rows = self.store.db.execute('SELECT slot_id,receipt FROM deletion_source_slots WHERE slot_id>? AND slot_id<=? ORDER BY slot_id LIMIT ?',
                (cursor, through or 'f' * 32, maximum)).fetchall()
            if not rows and after is None:
                rows = self.store.db.execute('SELECT slot_id,receipt FROM deletion_source_slots ORDER BY slot_id LIMIT ?', (maximum,)).fetchall()
            for row in rows:
                value = SOURCE_RECEIPT.decode(row['receipt'])
                already_seen = seen is not None and row['slot_id'] in seen
                if seen is not None: seen.add(row['slot_id'])
                if not already_seen and (value['state'] == DELETING or recover and value['state'] == UNKNOWN):
                    try: self.execute(row['slot_id'], recover=recover)
                    except Exception: pass  # Committed fence remains authoritative; no refund.
                with self.store._transaction(reclaim=True):
                    self.store.db.execute("UPDATE meta SET value=? WHERE key='deletion_cursor'", (row['slot_id'],))
            return len(rows)


class DeletionWorker:
    """Single joined cleanup owner; no provider, timer or read-triggered work."""
    def __init__(self, ledger):
        self.ledger, self.files = ledger, ledger.store.files
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='officer-file-cleanup', daemon=True)
        self.thread.start()

    def _run(self):
        recover = True
        while not self.stop.is_set():
            generation, _ = self.files.cleanup_registry()
            try:
                self._sweep(recover=recover)
            except Exception:
                pass  # Leave durable pending work for the next registry event/restart.
            recover = False
            if not self.stop.is_set(): self.files.wait_cleanup(generation, self.stop)

    def _sweep(self, *, recover):
        """Visit both sides of the initial cursor before ending recovery.

        Fixed high water and cursor ranges provide complete wrap coverage even
        when the first batch is short; newly allocated slots do not displace an
        existing UNKNOWN record from startup recovery. Each batch is bounded.
        """
        with self.ledger.store._gate():
            cursor = self.ledger.store.db.execute("SELECT value FROM meta WHERE key='deletion_cursor'").fetchone()[0]
            upper = self.ledger.store.db.execute('SELECT MAX(slot_id) FROM deletion_source_slots').fetchone()[0]
        if upper is None: return
        seen = set()
        limit = (self.ledger.store.limits['objects'] + 49) // 50 + 1
        for after, through in ((cursor, upper), ('0' * 32, min(cursor, upper))):
            for _ in range(limit):
                if self.stop.is_set(): return
                count = self.ledger.scan(recover=recover, seen=seen, after=after, through=through)
                if count == 0: break
                with self.ledger.store._gate():
                    after = self.ledger.store.db.execute("SELECT value FROM meta WHERE key='deletion_cursor'").fetchone()[0]

    def close(self, timeout=35):
        self.stop.set()
        self.files.wake_cleanup()
        self.thread.join(timeout)
        if self.thread.is_alive(): fail('upload_busy', 'Attachment cleanup is still active.', 409)
