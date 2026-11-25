"""Stage 2 and read-only Stage 3 over real disposable attachment lifecycles."""
import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_catalogue import HistorySelection
from attachments_materialize import DirectoryIdentity, FileIdentity, WorkspaceContext, copy_identity, relative_path
from attachments_http import LimitedReader
from attachments_native_handoff import FictionalAdapterBinding
from attachments_native_deletion import DisposableNativeDeletion
from attachments_native_reads import NativeAgentReadService
from attachments_native_receipts import command_digest
from attachments_owner_transport import LedgerError
from attachments_requests import TransportProfile, opaque, token
from attachments_service import closed
from attachments_types import AttachmentError, canonical, digest, fail, ident, integer, key
from attachments_native_fixture import GIT, NativeFixture
from native_agent_reads_fixture import serve_native_reads

KEY = 'fictional-native-browser-fixture-key'
ADA = 'x:00000000-0000-4000-8000-000000000001'
BRAM = 'fictional-bram'
SCOPE = 'fictional-orchard'
MIME = ('application/pdf', 'image/png', 'image/jpeg')
CAP = 262144
COPY_BUDGET = 64 * 1024 * 1024
ROUTES = {
    r'/native/drafts/([a-f0-9]{32})/send-intent': ('POST', 'prepare'),
    r'/native/requests/([a-f0-9]{32})/send': ('POST', 'send'),
    r'/native/receipts/send_prepare': ('GET', 'receipt'),
    r'/native/requests': ('GET', 'requests'),
    r'/native/requests/([a-f0-9]{32})': ('GET', 'request'),
    r'/native/files/([a-f0-9]{32})/details': ('GET', 'details'),
}
HEADERS = ('X-Attachment-Draft-Capability', 'X-Attachment-Request-Capability', 'Idempotency-Key')
FIXED_HEADERS = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                 ('Referrer-Policy', 'no-referrer'), ('Connection', 'close')]
DELETION_CAPABILITY = 'X-Attachment-Deletion-Capability'
DELETION_ROUTES = (
    (r'/native/selection', 'GET', 'selection'),
    (r'/files', 'GET', 'files'),
    (r'/native/files/([a-f0-9]{32})/details', 'GET', 'details'),
    (r'/native/files/management', 'GET', 'management'),
    (r'/native/receipts/deletion_prepare', 'GET', 'receipts'),
    (r'/native/files/([a-f0-9]{32})/deletion-intent', 'POST', 'prepare'),
    (r'/native/deletions/([a-f0-9]{32})', 'GET', 'status'),
    (r'/native/deletions/([a-f0-9]{32})/confirm', 'POST', 'confirm'),
    (r'/native/deletions/([a-f0-9]{32})/execute', 'POST', 'execute'),
)


def insist(condition, code='fixture_state_unverified', status=503):
    if not condition:
        fail(code, 'The exact fixture attachment state could not be verified.', status)


def envelope(value):
    return json.dumps(dict(value, schemaVersion=1, fixture=False,
                           serverTime=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')),
                      ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def unique_pairs(items):
    result = dict(items)
    insist(len(result) == len(items), 'invalid_request', 400)
    return result


class StageFixture(NativeAgentReadService):
    def __init__(self, fixture):
        self.native = fixture
        fixture.open_store(mirror=True, epoch=hashlib.sha256(KEY.encode()).hexdigest())
        self.frozen_binding = dict(fixture.binding, id=ADA, scopeId=SCOPE, capabilityRevision='fictional-v1')
        fixture.profile = replace(fixture.profile, capability_revision=self.frozen_binding['capabilityRevision'],
                                  media_types=MIME, original_relay_formats=MIME)
        fixture.adapter_binding = FictionalAdapterBinding(canonical(self.frozen_binding), fixture.profile, fixture.context)
        self.flow = threading.RLock()
        self.commands = {}
        self.grants = {}
        self.saved = {}
        self.mode = 'pending'
        self.copy_context = True
        self.drift = None
        self.handoff = fixture.coordinator(self.boundary)
        self.phase_counts = dict(prepare=0, send=0, receipts=0, requests=0, files=0, details=0)
        super().__init__(fixture.store, selections=[
            dict(mode='current', destinationId=destination, scopeId=scope,
                 capabilityRevision='fictional-v1', formats=list(MIME),
                 limits={'fileBytes': 1024, 'draftBytes': 4096, 'fileCount': 5})
            for destination, scope in [(ADA, SCOPE), (BRAM, 'fictional-grove')]
        ])

    def close(self):
        self.handoff.stop()
        super().close()

    def native_selection(self, destination_id):
        result = super().native_selection(destination_id)
        if destination_id == ADA and result['context']:
            result['features'].update(send=True, files=True)
        return result

    def chosen(self, destination_id, scope_id):
        self._scope(destination_id, scope_id)
        insist((destination_id, scope_id) == (ADA, SCOPE), 'forbidden', 403)
        return {'destinationId': destination_id, 'scopeId': scope_id}

    def request_row(self, request_id, selection):
        row = self.native.requests._request(request_id)
        insist(row['epoch'] == self.store.owner_epoch and row['destination'] == canonical(self.frozen_binding)
               and row['catalogue_kind'] == 'agent'
               and (row['catalogue_destination'], row['catalogue_scope'], row['catalogue_conversation'])
               == (selection['destinationId'], selection['scopeId'], None), 'forbidden', 403)
        return row

    def admission(self, row):
        draft = self.store._draft(row['draft_id'])
        insist(draft['destination'] == row['destination'])
        owned = (draft['admitted_kind'], draft['admitted_request']) == ('native', row['id'])
        insist(owned and row['authorized_at'] is not None or not owned and row['authorized_at'] is None
               and draft['admitted_kind'] is None and draft['admitted_request'] is None)
        return owned

    def prepare(self, draft_id, capability, command, body):
        closed(body, {'revision', 'writerGeneration', 'writerNonce'})
        token(body['writerNonce'])
        insist(integer(body['revision'], 1) and integer(body['writerGeneration'], 1), 'invalid_request', 400)
        token(capability)
        key(command)
        selection = self.chosen(ADA, SCOPE)
        insist(self._bound_draft(draft_id) == self.frozen_binding, 'forbidden', 403)
        summed = command_digest(command)
        insist(summed not in self.commands, 'request_conflict', 409)
        insist(len(self.commands) < 64, 'request_capacity', 429)
        expected = digest({'draft': draft_id, 'fence': body, 'binding': self.frozen_binding,
                           'profile': asdict(self.native.profile)})
        self.commands[summed] = (command, draft_id, body['revision'], body['writerGeneration'], expected)
        self.phase_counts['prepare'] += 1
        if self.mode == 'mirror_slot':
            self.store.db.update_fault = 'INSERT INTO native_outcome_mirror '
        prepared = self.native.mirror.prepare_request(draft_id, capability, body, command, self.frozen_binding,
                                                       self.native.profile)
        observation = self.observation(prepared['id'], selection)
        insist(observation['request']['preparation'] == 'intent' and observation['draftAdmission'] == 'unadmitted')
        self.grants[prepared['id']] = (prepared['capability'], prepared['fingerprint'], prepared['revision'])
        return {'observation': observation, 'requestCapability': prepared['capability'],
                'fingerprint': prepared['fingerprint']}

    def receipt(self, selection, draft_id, summed):
        ident(draft_id)
        token(summed)
        self.phase_counts['receipts'] += 1
        fact = self.commands.get(summed)
        if fact is None or fact[1] != draft_id:
            return {'match': None}
        command, _, revision, generation, expected = fact
        with self.store._gate():
            seen = self.store.db.execute("SELECT * FROM seen WHERE key=?", (command,)).fetchone()
            if seen is None:
                return {'match': None}
            insist((seen['kind'], seen['epoch'], seen['fingerprint']) ==
                   ('attachment_intent', self.store.owner_epoch, expected))
            row = self.request_row(seen['target'], selection)
            insist((row['draft_id'], row['draft_revision'], row['draft_generation']) ==
                   (draft_id, revision, generation))
            try:
                self.native.mirror._as_of(row['id'])  # An intent alone need not have a valid mirror slot.
            except AttachmentError:
                return {'match': None}
        return {'match': {'draftId': draft_id, 'requestId': row['id']}}

    def send(self, request_id, capability, command, body):
        closed(body, {'revision', 'fingerprint'})
        token(capability)
        token(body['fingerprint'])
        key(command)
        insist(integer(body['revision'], 1), 'invalid_request', 400)
        selection = self.chosen(ADA, SCOPE)
        with self.store._gate():
            row = self.request_row(request_id, selection)
            insist(row['stage'] == 'intent' and self.grants.get(request_id) ==
                   (capability, body['fingerprint'], body['revision']), 'request_conflict', 409)
        del self.grants[request_id]
        self.phase_counts['send'] += 1
        ledger = self.native.requests
        published = []
        with self.native.resources.hold(ledger.resource_keys(request_id)) as lease:
            self.native.mirror.authorize_send(request_id, capability, body['revision'], body['fingerprint'],
                                              resolved_binding=self.frozen_binding, transport_profile=self.native.profile,
                                              resource_lease=lease)
            authority = ledger.authority(request_id)
            directories = self.native.materializer.prepare_directories(self.native.context, authority, lease,
                                                                          directories=self.native.dirs({'id': request_id}))
            status = ledger.request_status(request_id, capability)
            for item in status['copies']:
                authority = ledger.authority(request_id)
                copy = ledger.claim_copy(request_id, authority.operation_revision, item['id'], lease)
                authority = ledger.authority(request_id)
                self.native.materializer.ensure_exclusion(self.native.context, authority, (copy,), lease)
                split = self.mode == 'partial' and bool(published)
                materializer = self.native.materializer
                previous_checkpoint = materializer.checkpoint
                if split:
                    def fail_after_creation(point):
                        if point == 'copy_created':
                            raise OSError('Fictional interrupted local copy.')
                        return previous_checkpoint(point)
                    materializer.checkpoint = fail_after_creation
                try:
                    with ledger.source_handle(copy.id, lease) as fd:
                        saved = materializer.materialize(self.native.context, authority, copy, fd,
                                                         copy_identity(FileIdentity.of(os.fstat(fd))),
                                                         lease, directories=directories)
                    ledger.record_copy_result(saved, lease)
                except AttachmentError:
                    if not split:
                        raise
                    break
                finally:
                    materializer.checkpoint = previous_checkpoint
                self.saved[copy.id] = saved
                published.append(saved)
        if len(published) == len(status['copies']):
            # The materialization lease and all source handles are closed here.
            self.handoff.handoff(request_id, tuple(published))
            self.native.mirror.reconcile(request_id, self.native.owner)
        return {'observation': self.observation(request_id, selection)}

    def boundary(self, admission, attempt):
        self.native.boundary(admission, attempt)
        rid = admission.attachment_request_id
        if self.mode in ('queued', 'unknown', 'not_submitted'):
            self.add_owner_outcome(rid, {'queued': 'accepted'}.get(self.mode, self.mode), mirror=False)

    def add_owner_outcome(self, request_id, kind, *, mirror=True):
        with self.store._gate():
            row = self.request_row(request_id, self.chosen(ADA, SCOPE))
        context = {'id': request_id, 'ownerRequestId': row['owner_request_id']}
        if kind in ('reply', 'astral', 'escaped'):
            value = {'reply': 'Fictional correlated reply.', 'astral': '🪴' * 16000,
                     'escaped': '\u0003' * 16000}[kind]
            self.native.event(context, 'reply', turn_id='fictional-turn', reply=value)
        else:
            self.native.event(context, kind, **({'turn_id': 'fictional-turn'} if kind in ('started', 'ended') else {}))
        if mirror:
            self.native.mirror.reconcile(request_id, self.native.owner)

    def roster(self, row):
        ledger = self.native.requests
        members = ledger._members(row['id'])
        insist(1 <= len(members) <= 5)
        copies = []
        for member in members:
            copy = ledger._copy(member['workspace_id'])
            insist(member['preparation_kind'] == 'original_relay' and member['media'] in MIME and
                   copy['request_id'] == row['id'] and copy['source_id'] == member['source_id'] and
                   copy['bytes'] == member['bytes'] and copy['digest'] == member['digest'] and copy['role'] == 'workspace')
            copies.append((dict(member), dict(copy)))
        return copies

    def proof_states(self, request_id, selection, roster, budget):
        states = {}
        candidates = []
        for member, copy in roster:
            physical = copy['state']
            current = {'planned': 'not_saved', 'reserved': 'not_saved', 'writing': 'saving',
                       'partial': 'unavailable', 'cleanup_pending': 'unknown', 'cleanup_failed': 'unknown',
                       'removed': 'removed' if copy['released'] else 'unknown'}.get(physical, 'unknown')
            states[member['source_id']] = current
            if physical == 'available' and copy['charged'] and not copy['released']:
                if budget[0] > 0 and budget[1] >= copy['bytes']:
                    budget[0] -= 1
                    budget[1] -= copy['bytes']
                    candidates.append((member, copy))
        if not candidates or not self.copy_context:
            return states
        ledger = self.native.requests
        try:
            with self.native.resources.hold(ledger.resource_keys(request_id), timeout=.15) as lease:
                for member, copy in candidates:
                    with self.store._gate():
                        row = self.request_row(request_id, selection)
                        authority = ledger.authority(request_id)
                        spec = ledger._intent(ledger._copy(copy['id']))
                        directories = self.native.dirs({'id': request_id})
                        before = self.copy_fence(row, member, copy['id'])
                        saved = self.saved.get(copy['id'])
                    if saved is None or before[1] != copy:
                        continue
                    try:
                        self.native.materializer.verify_copy(self.native.context, authority, spec, saved, lease, directories=directories)
                    except AttachmentError as exc:
                        if exc.code in ('copy_changed', 'folder_changed'):
                            states[member['source_id']] = 'unavailable'
                        continue
                    if self.drift == request_id:
                        self.drift = None
                        with self.store._gate(), self.store._transaction():
                            self.store.db.execute('UPDATE workspace_copies SET generation=generation+1 WHERE id=?', (copy['id'],))
                    with self.store._gate():
                        row = self.request_row(request_id, selection)
                        if self.copy_fence(row, member, copy['id']) == before:
                            lease.require(ledger.resource_keys(request_id))
                            states[member['source_id']] = 'available'
        except (AttachmentError, OSError):
            pass
        return states

    def copy_fence(self, row, member, copy_id):
        ledger = self.native.requests
        insist(self.admission(row))
        source = self.store.db.execute('SELECT * FROM files WHERE id=?', (member['source_id'],)).fetchone()
        insist(source is not None)
        current = next((dict(r) for r in ledger._members(row['id']) if r['source_id'] == member['source_id']), None)
        insist(current == member)
        return (dict(row), dict(ledger._copy(copy_id)), dict(source), ledger.workspace_directories(row['id']))

    def current_reply(self, row, facts):
        if facts is None:
            return {'state': 'unknown', 'text': None}
        link, outcome = facts
        if not link['reply_available']:
            return {'state': 'withheld' if outcome['reply_captured'] else 'unknown', 'text': None}
        expected = self.native.mirror._expected(row['id'])
        owner = self.native.owner
        try:
            with owner.lock:
                insist(not owner.db.in_transaction)
                owner.db.execute('BEGIN')
                try:
                    live, result, payload = owner._read(row['owner_request_id'])
                    owner._match(live, expected.digest)
                    if (live['attempt_id'], result['seq'], result['digest']) != (link['attempt_id'], outcome['seq'], outcome['digest']):
                        return {'state': 'unknown', 'text': None}
                    reply = payload['mutable']['reply'] if live['reply_available'] and payload is not None else None
                finally:
                    owner.db.rollback()
        except (LedgerError, AttachmentError):
            return {'state': 'unknown', 'text': None}
        with self.store._gate():
            current = self.native.mirror._as_of(row['id'])
        if current != facts:
            return {'state': 'unknown', 'text': None}
        return {'state': 'available' if reply is not None else 'withheld', 'text': reply}

    def observation(self, request_id, selection, budget=None):
        ledger = self.native.requests
        with self.store._gate():
            row = dict(self.request_row(request_id, selection))
            admitted = self.admission(row)
            roster = self.roster(row)
            facts = self.native.mirror._as_of(request_id)
        states = self.proof_states(request_id, selection, roster, [5, COPY_BUDGET] if budget is None else budget)
        stage = row['stage']
        insist(stage in ('intent', 'send_authorized', 'materializing', 'materialized', 'handoff_claimed',
                         'owner_recorded', 'delivery_pending', 'queued', 'running', 'execution_ended', 'answered', 'failed',
                         'unknown', 'cleanup', 'cleaned'))
        preparation = stage if stage in ('intent', 'materialized', 'cleanup', 'cleaned') else (
            'preparing_delivery' if stage in ('send_authorized', 'materializing') else 'materialized')
        delivery, accepted, execution, reason = ('pending' if admitted else 'not_started'), False, 'unobserved', None
        if row['error'].strip() == 'copy_incomplete':
            reason = 'copy_failed'
        if facts is not None:
            _, outcome = facts
            accepted = outcome['accepted']
            execution = 'ended' if outcome['execution_ended'] else 'started' if outcome['started'] else 'unobserved'
            if outcome['not_submitted']:
                delivery, reason = 'not_submitted', 'proven_not_submitted'
            elif outcome['state'] == 'unknown':
                delivery, reason = 'unknown', 'dispatch_unconfirmed'
            elif accepted:
                delivery = 'queued'
        reply = self.current_reply(row, facts)
        return {**selection, 'request': {
            'id': request_id, 'draftId': row['draft_id'], 'revision': row['revision'], 'createdAt': row['created'],
            'text': row['text'], 'preparation': preparation, 'files': [
                dict(fileId=m['source_id'], name=m['name'], mediaType=m['media'], bytes=m['bytes'],
                     savedCopyState=states[m['source_id']]) for m, _ in roster],
            'delivery': delivery, 'accepted': accepted, 'execution': execution, 'reply': reply, 'reason': reason,
        }, 'draftFence': {'revision': row['draft_revision'], 'writerGeneration': row['draft_generation']},
            'draftAdmission': 'matched' if admitted else 'unadmitted'}

    def request_page(self, selection, cursor, stamp):
        cursor_kind = 'fixture-native-request-cursor'
        last = (-1, '0' * 32)
        if cursor is not None:
            value = self.catalogue._unseal(cursor_kind, cursor)
            insist(set(value) == {'selection', 'last', 'v', 'epoch'} and value['selection'] == selection
                   and type(value['last']) is list and len(value['last']) == 2
                   and type(value['last'][0]) in (int, float) and value['last'][0] >= 0, 'invalid_cursor', 400)
            ident(value['last'][1])
            last = tuple(value['last'])
        with self.store._gate():
            rows = self.store.db.execute("""SELECT id,created FROM attachment_requests
                WHERE catalogue_kind='agent' AND catalogue_destination=? AND catalogue_scope=?
                AND catalogue_conversation IS NULL AND epoch=?
                AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT 51""",
                (selection['destinationId'], selection['scopeId'], self.store.owner_epoch, last[0], *last)).fetchall()
        items, next_cursor = [], None
        work = [50, COPY_BUDGET]
        for index, row in enumerate(rows[:50]):
            with self.store._gate():
                roster = self.roster(self.request_row(row['id'], selection))
                candidates = [copy for _, copy in roster if copy['state'] == 'available' and copy['charged'] and not copy['released']]
            if len(candidates) > work[0] or sum(c['bytes'] for c in candidates) > work[1]:
                break
            item = self.observation(row['id'], selection, work)
            continuation = (self.catalogue._seal(cursor_kind, {'selection': selection, 'last': [row['created'], row['id']]})
                            if index + 1 < len(rows) else None)
            if len(encoded_page([*items, item], continuation, stamp)) > CAP:
                break
            items.append(item)
            next_cursor = continuation
        insist(not rows or bool(items))
        return encoded_page(items, next_cursor, stamp)

    def catalogue_scope(self, selection):
        with self.store._gate():
            old = self.store.db.execute("""SELECT 1 FROM attachment_requests WHERE catalogue_kind='agent'
                AND catalogue_destination=? AND catalogue_scope=? AND catalogue_conversation IS NULL AND epoch<>? LIMIT 1""",
                (selection['destinationId'], selection['scopeId'], self.store.owner_epoch)).fetchone()
            insist(old is None, 'forbidden', 403)

    def file_row(self, selection, request_id, file_id, budget):
        with self.store._gate():
            row = dict(self.request_row(request_id, selection))
            insist(self.admission(row))
            roster = [pair for pair in self.roster(row) if pair[0]['source_id'] == file_id]
            insist(len(roster) == 1 and roster[0][0]['catalogued_at'] is not None, 'forbidden', 403)
            member, _ = roster[0]
            outbox = self.store.db.execute('SELECT retention_pin FROM attachment_outbox WHERE request_id=?', (request_id,)).fetchone()
        state = self.proof_states(request_id, selection, roster, budget)[file_id]
        return dict(id=file_id, requestId=request_id, name=member['name'], bytes=member['bytes'], mediaType=member['media'],
                    cataloguedAt=member['catalogued_at'], savedCopyState=state, savedCopyAvailable=state == 'available',
                    retentionPinned=bool(outbox['retention_pin']), preparationKind='original_relay',
                    previewAvailable=False, expiresAt=None)

    def history(self, destination_id, scope_id, conversation_id, *, cursor=None):
        selection = self.chosen(destination_id, scope_id)
        insist(conversation_id is None, 'forbidden', 403)
        with self.flow:
            with self.store._gate():
                self.catalogue_scope(selection)
                page = self.catalogue.history(HistorySelection('agent', destination_id, scope_id, None), cursor=cursor)
            budget = [50, COPY_BUDGET]
            self.phase_counts['files'] += 1
            items = [self.file_row(selection, r['requestId'], r['id'], budget) for r in page['items']]
        return {**selection, 'items': items, **{k: page[k] for k in (
            'associationCount', 'unclassifiedAssociationCount', 'nextCursor')}}

    def damage(self, request_id, kind):
        with self.store._gate():
            row = self.request_row(request_id, self.chosen(ADA, SCOPE))
            _, copy = self.roster(row)[0]
        path = self.native.repo / copy['relative_path']
        if kind == 'content':
            path.write_bytes(b'Z' * copy['bytes'])
        elif kind == 'inode':
            original = path.read_bytes()
            path.rename(path.with_name('displaced-fixture'))
            path.write_bytes(original)
        elif kind == 'unlink':
            path.unlink()
        elif kind == 'parent':
            parent = path.parent
            parent.rename(parent.with_name('displaced-fixture-directory'))
            parent.mkdir(mode=0o700)
        elif kind == 'context':
            self.copy_context = False
        else:
            insist(False, 'invalid_request', 400)

    def counters(self):
        with self.store._gate():
            quota = self.store.db.execute('SELECT bytes FROM quota WHERE id=1').fetchone()[0]
            intents = self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0]
        return dict(self.phase_counts, boundary=len(self.native.calls), quota=quota, intents=intents)

    @contextmanager
    def seed_clock(self, base):
        clock = self.store.clock
        simulated = [base]

        def wall():
            return simulated[0]

        self.store.clock = wall
        try:
            with patch('attachments_materialize.time.time', wall):
                yield simulated
        finally:
            self.store.clock = clock

    def seed(self, count, kind):
        insist(integer(count, 1, 61) and kind in ('escaped', 'catalogue'))
        text = 'Fictional archive record.'
        intended = b'%PDF-1.4\nFictional archive seed.\n%%EOF'
        actual = time.time()
        # A fixture clock advances real per-minute gates without bypassing them.
        # All mutation kernels, including the filesystem verifier, see one value.
        old_mode = self.mode
        self.mode = 'queued'
        try:
            with self.seed_clock(actual + 61) as wall:
                for index in range(count):
                    if index:
                        wall[0] += 61
                    draft = self.create_draft({**self.chosen(ADA, SCOPE), 'conversationId': None, 'modelId': None,
                                               'capabilityRevision': self.frozen_binding['capabilityRevision']}, self.native.key())
                    cap = draft['capability']
                    nonce = draft['writerNonce']
                    def fence(revision):
                        return {'revision': revision, 'writerGeneration': draft['writerGeneration'], 'writerNonce': nonce}
                    d = self.mutate('edit', draft['id'], cap, {**fence(draft['revision']), 'text': text})
                    upload = self.mutate('reserve', draft['id'], cap, {**fence(d['revision']), 'name': 'orchard.pdf',
                        'mediaType': 'application/pdf', 'bytes': len(intended), 'digest': hashlib.sha256(intended).hexdigest()},
                        command_key=self.native.key())
                    ready = self.receive_upload(draft['id'], cap, fence(upload['draftRevision']), upload['id'],
                                                upload['fingerprint'], io.BytesIO(intended))
                    prep = self.prepare(draft['id'], cap, self.native.key(), fence(ready['draftRevision']))
                    request_id = prep['observation']['request']['id']
                    self.send(request_id, prep['requestCapability'], self.native.key(), {'revision': 1, 'fingerprint': prep['fingerprint']})
                    self.add_owner_outcome(request_id, 'escaped' if kind == 'escaped' else 'ended')
        finally:
            self.mode = old_mode


class DeletionFixture:
    """Own only the reviewed D1 launcher and a disposable workspace."""

    def __init__(self, stack):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='fictional-native-deletion-'))).resolve()
        self.repo = root / 'orchard'
        self.repo.mkdir(mode=0o700)
        NativeFixture.git(self, 'init', '-q')
        NativeFixture.git(self, '-c', 'user.name=Fictional', '-c', 'user.email=fixture@example.invalid',
                          'commit', '-q', '--allow-empty', '-m', 'Fictional orchard')
        git_dir, executable = self.repo / '.git', Path(GIT).resolve()
        info = git_dir / 'info'
        self.context = WorkspaceContext('b' * 64, 'c' * 64, str(self.repo), DirectoryIdentity.of(self.repo.stat()),
                                        str(git_dir), DirectoryIdentity.of(git_dir.stat()),
                                        str(info), DirectoryIdentity.of(info.stat()),
                                        str(executable), FileIdentity.of(executable.stat()))
        self.binding = dict(kind='agent', id=ADA, scopeId=SCOPE, modelId=None, conversationId=None,
                            capabilityRevision='fictional-v1')
        self.profile = TransportProfile('fictional-deletion-profile', 'v1', 'codex', 'fictional-v1',
            self.context.fingerprint, self.context.workspace_fingerprint, 'fictional-qualification', MIME,
            original_relay_formats=MIME)
        self.flow = threading.RLock()
        self.unavailable = set()
        self.seeded = {}
        self.seed_count = 0
        self.sequence = 0
        self.seed_time = 0
        self.active_seed_time = None
        self.launcher = DisposableNativeDeletion.create(root / 'state', hashlib.sha256(KEY.encode()).hexdigest(),
            resolve=self.resolve, clock=self.clock)
        stack.callback(self.launcher.close)
        self.deletion = self.launcher.deletion
        self.phase_counts = dict(prepare=0, confirm=0, execute=0, management=0, receipts=0, status=0, files=0, details=0)

    def clock(self):
        return self.active_seed_time if self.active_seed_time is not None else time.time()

    def resolve(self, authority):
        return None if authority.request_id in self.unavailable else self.context

    def command_key(self):
        self.sequence += 1
        return f'fictional-deletion-seed-{self.sequence:06d}'

    def select(self, destination_id):
        opaque(destination_id)
        current = destination_id == ADA
        if current:
            self.deletion.management(HistorySelection('agent', ADA, SCOPE, None))
        return dict(context=dict(kind='agent', destinationId=ADA, scopeId=SCOPE, modelId=None, conversationId=None)
                    if current else None, features=dict(drafts=False, send=False, files=current, deletion=current))

    def scope(self, data):
        insist((data['destinationId'], data['scopeId']) == (ADA, SCOPE), 'forbidden', 403)
        return HistorySelection('agent', ADA, SCOPE, None)

    @staticmethod
    def scope_fields(scope):
        return dict(destinationId=scope.destination_id, scopeId=scope.scope_id)

    def seed(self, count, kind):
        insist(integer(count, 1, 61) and self.seed_count + count <= 61
               and kind in ('catalogue', 'unclassified'), 'fixture_seed_limit', 400)
        data = b'%PDF-1.4\nFictional archive seed.\n%%EOF'
        rows = []
        real_time = time.time
        for _ in range(count):
            self.seed_count += 1
            self.seed_time = max(real_time(), self.seed_time) + 61
            self.active_seed_time = self.seed_time
            try:
                # The fixture alone controls the simulated wall clock while a
                # seed is active. Normal kernel rate and proof checks still run.
                with patch('attachments_materialize.time.time', self.clock):
                    rows.append(self.seed_one(data, kind))
            finally:
                self.active_seed_time = None
        return rows

    def seed_one(self, data, kind):
        launcher = self.launcher
        draft = launcher.create_draft(self.binding, self.command_key())
        draft_id, cap = draft['id'], draft['capability']

        def fence(revision):
            return dict(revision=revision, writerGeneration=draft['writerGeneration'], writerNonce=draft['writerNonce'])

        edit = launcher.edit_text(draft_id, cap, fence(draft['revision']), 'Fictional archive record.')
        upload = launcher.reserve_upload(draft_id, cap, fence(edit['revision']), {
            'name': 'orchard.pdf', 'mediaType': 'application/pdf', 'bytes': len(data),
            'digest': hashlib.sha256(data).hexdigest()}, self.command_key())
        received = launcher.receive_upload(draft_id, cap, fence(upload['draftRevision']), upload['id'],
                                           upload['fingerprint'], io.BytesIO(data))
        request = launcher.prepare_request(draft_id, cap, fence(received['draftRevision']), self.command_key(),
                                           self.binding, self.profile)
        rid = request['id']
        launcher.authorize_send(rid, request['capability'], request['revision'], request['fingerprint'],
                                binding=self.binding, profile=self.profile)
        copy = request['copies'][0]
        retained = None
        if kind == 'catalogue':
            saved = launcher.materialize(rid, copy['id'])
            retained = (self.repo / relative_path(rid, copy['id'], 'application/pdf'), saved.identity)
        self.seeded[rid] = retained
        return dict(requestId=rid, fileId=copy['sourceId'])

    def damage(self, request_id, kind):
        rid = ident(request_id)
        retained = self.seeded.get(rid)
        insist(retained is not None and kind in ('content', 'resolver_unavailable'), 'invalid_request', 400)
        if kind == 'resolver_unavailable':
            self.unavailable.add(rid)
            return
        path, identity = retained
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            insist(copy_identity(FileIdentity.of(os.fstat(fd))) == identity, 'copy_changed', 409)
            insist(os.pwrite(fd, b'Z' * identity[2], 0) == identity[2])
            os.fsync(fd)
        finally:
            os.close(fd)

    def counters(self):
        return dict(self.phase_counts, seedCount=self.seed_count, send=0, boundary=0)

    def console(self, command):
        action = command.get('action') if type(command) is dict else None
        if action == 'seed':
            closed(command, {'action', 'count', 'kind'})
            seeded = self.seed(command['count'], command['kind'])
            return dict(self.counters(), seeded=seeded)
        if action == 'damage':
            closed(command, {'action', 'requestId', 'kind'})
            self.damage(command['requestId'], command['kind'])
        else:
            insist(action == 'counters', 'invalid_request', 400)
            closed(command, {'action'})
        return self.counters()

    def operation(self, scope, value):
        wire = dict((k, value[k]) for k in ('id', 'fileId', 'destinationId', 'scopeId', 'revision', 'state',
                    'releasedBytes', 'sourceUnavailable', 'canExecute', 'canRecover', 'pieces'))
        wire['previewGeneration'] = value['managementGeneration']
        ident(wire['id'])
        ident(wire['fileId'])
        insist(all(wire[k] == v for k, v in self.scope_fields(scope).items())
               and integer(wire['previewGeneration'], 1) and integer(wire['revision'], wire['previewGeneration'])
               and integer(wire['releasedBytes']) and all(type(wire[k]) is bool for k in
                   ('sourceUnavailable', 'canExecute', 'canRecover')))
        pieces = wire['pieces']
        insist(type(pieces) is list and len(pieces) == 2)
        for piece, kind in zip(pieces, ('watch_original', 'workspace')):
            closed(piece, {'kind', 'state', 'bytes', 'chargedBytes', 'releasedBytes', 'reason'})
            insist(piece['kind'] == kind and piece['state'] in ('pending', 'removed', 'changed', 'unknown')
                   and piece['reason'] in (None, 'source_in_use', 'identity_changed', 'removal_unconfirmed',
                                            'workspace_unavailable', 'workspace_unverified', 'copy_in_use')
                   and integer(piece['bytes'], 1) and integer(piece['chargedBytes']) and integer(piece['releasedBytes'])
                   and piece['chargedBytes'] + piece['releasedBytes'] <= piece['bytes']
                   and (piece['releasedBytes'] == piece['bytes'] if piece['state'] == 'removed' else not piece['releasedBytes']))
        states = tuple(piece['state'] for piece in pieces)
        if not wire['sourceUnavailable']:
            state = 'confirm'
            insist(states == ('pending', 'pending') and wire['revision'] == wire['previewGeneration'])
        elif states == ('removed', 'removed'):
            state = 'deleted'
        elif 'changed' in states or 'removed' in states and 'unknown' in states:
            state = 'partial_failure'
        else:
            state = 'unknown' if 'unknown' in states else 'deleting'
        insist(wire['state'] == state and wire['releasedBytes'] == sum(piece['releasedBytes'] for piece in pieces)
               and (not wire['canExecute'] or wire['sourceUnavailable'] and 'pending' in states)
               and (not wire['canRecover'] or wire['sourceUnavailable'] and 'unknown' in states))
        return wire

    def read(self, action, target, fields):
        if action == 'selection':
            return self.select(fields['destinationId'])
        scope = self.scope(fields)
        selection = self.scope_fields(scope)
        self.phase_counts[action] += 1
        if action == 'files':
            insist(fields['conversationId'] == '', 'forbidden', 403)
            return dict(selection, **self.deletion.history(scope, fields.get('cursor')))
        if action == 'details':
            return dict(selection, item=self.deletion.file_details(scope, fields['requestId'], target))
        if action == 'status':
            return dict(operation=self.operation(scope, self.deletion.status(target, scope)))
        if action == 'receipts':
            file_id = ident(fields['fileId'])
            match = self.deletion.match_prepare(file_id, scope, token(fields['commandDigest']))['match']
            return dict(selection, fileId=file_id, match=None if match is None else
                        dict(operationId=ident(match['operationId']), previewGeneration=match['managementGeneration']))
        return dict(selection, **self.management(scope, fields.get('cursor')))

    def management(self, scope, cursor):
        page = self.deletion.management(scope, cursor)
        operations = {}
        for operation in page['deletions']:
            current = self.operation(scope, operation)
            insist(current['fileId'] not in operations)
            operations[current['fileId']] = current
        items = []
        for row in page['files']:
            insist(row['requestId'] is not None)
            file_id, request_id = ident(row['id']), ident(row['requestId'])
            current = operations.pop(file_id, None)
            generation = row['managementGeneration']
            insist(integer(generation) and type(row['canPrepare']) is bool and type(row['available']) is bool
                   and (current is None and row['deletionId'] is None and generation == 0 or current is not None and
                        (current['id'], current['previewGeneration']) == (row['deletionId'], generation)))
            items.append(dict(fileId=file_id, requestId=request_id, sourceAvailable=row['available'],
                managementGeneration=generation, canPrepare=row['canPrepare'], operation=None if current is None else
                dict(id=current['id'], previewGeneration=generation)))
        insist(not operations)
        return dict(items=items, nextCursor=page['nextCursor'])

    def mutate(self, action, target, body, headers):
        selection_fields = {'destinationId', 'scopeId'}
        if action == 'prepare':
            closed(body, selection_fields | {'managementGeneration'})
            scope = self.scope(body)
            generation = body['managementGeneration']
            insist(integer(generation), 'invalid_request', 400)
            command = key(headers['Idempotency-Key'])
            self.phase_counts[action] += 1
            prepared = self.deletion.prepare(target, scope, generation, command)
            insist(prepared.get('capability') is not None and prepared['canConfirm']
                   and prepared['fileId'] == target and prepared['managementGeneration'] == generation + 1,
                   'deletion_changed', 409)
            return dict(operation=self.operation(scope, prepared),
                        confirmation=dict(capability=token(prepared['capability']), fingerprint=token(prepared['fingerprint'])))

        common = selection_fields | {'fileId', 'previewGeneration', 'revision'}
        closed(body, common | ({'fingerprint'} if action == 'confirm' else {'mode'}))
        scope = self.scope(body)
        file_id, generation, expected = ident(body['fileId']), body['previewGeneration'], body['revision']
        insist(integer(generation, 1) and integer(expected, generation), 'invalid_request', 400)
        current = self.deletion.status(target, scope)
        insist((current['fileId'], current['managementGeneration'], current['revision'])
               == (file_id, generation, expected), 'deletion_changed', 409)
        if action == 'confirm':
            fingerprint = token(body['fingerprint'])
            capability = token(headers[DELETION_CAPABILITY])
            command = key(headers['Idempotency-Key'])
            insist(current['state'] == 'confirm' and current['canConfirm'] and current['fingerprint'] == fingerprint,
                   'deletion_changed', 409)
            self.phase_counts[action] += 1
            confirmed = self.deletion.confirm(target, capability, fingerprint, command)
            insist(type(confirmed['confirmedNow']) is bool)
            return dict(operation=self.operation(scope, confirmed['operation']), confirmedNow=confirmed['confirmedNow'])

        mode = body['mode']
        insist(mode in ('execute', 'recover'), 'invalid_request', 400)
        insist(current['sourceUnavailable'] and current['canRecover' if mode == 'recover' else 'canExecute'],
               'deletion_changed', 409)
        self.phase_counts[action] += 1
        return dict(operation=self.operation(scope, self.deletion.execute(target, expected, recover=mode == 'recover')))


def encoded_page(items, cursor, stamp):
    return json.dumps({'items': items, 'nextCursor': cursor, 'schemaVersion': 1, 'fixture': False, 'serverTime': stamp},
                      ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def route(handler, service):
    parsed = urlsplit(handler.path)
    prefix = '/api/attachments'
    if not parsed.path.startswith(prefix + '/native/'):
        return False
    path = parsed.path[len(prefix):]
    found = next(((match, spec) for pattern, spec in ROUTES.items() if (match := re.fullmatch(pattern, path))), None)
    if found is None:
        return False

    def one(name, required=True):
        values = handler.headers.get_all(name, [])
        insist(len(values) == 1 if required else len(values) <= 1, 'invalid_request', 400)
        return values[0] if values else None

    try:
        match, (method, action) = found
        key_value = one('X-Live-Room-Key', False)
        insist(key_value is not None and hmac.compare_digest(key_value.encode(), KEY.encode()), 'unauthorized', 401)
        hosts = [f'127.0.0.1:{handler.server.server_port}', f'localhost:{handler.server.server_port}']
        origin = one('Origin', False)
        insist(one('Host') in hosts and (origin is None or origin in ['http://' + host for host in hosts]), 'forbidden', 403)
        insist(handler.command == method, 'method_not_allowed', 405)
        insist(not parsed.scheme and not parsed.netloc and not parsed.fragment and '#' not in handler.path
               and not re.search(r'%(?![0-9a-fA-F]{2})', parsed.query) and one('Transfer-Encoding', False) is None,
               'invalid_request', 400)
        length = one('Content-Length', False)
        headers = {name: one(name, False) for name in HEADERS}
        permitted = {'prepare': {HEADERS[0], HEADERS[2]}, 'send': {HEADERS[1], HEADERS[2]}}.get(action, set())
        present = {name for name, value in headers.items() if value is not None}
        insist(present == permitted and all(not name.lower().startswith('x-attachment-')
               or name.lower() in {permitted.lower() for permitted in present} for name in handler.headers),
               'invalid_request', 400)
        with service.flow:
            if method == 'GET':
                insist(length in (None, '0') and one('Content-Type', False) is None, 'invalid_request', 400)
                fields = unique_pairs(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4))
                required = {'destinationId', 'scopeId'} | ({'draftId', 'commandDigest'} if action == 'receipt' else
                                                          {'requestId'} if action == 'details' else set())
                insist(required <= set(fields) <= required | ({'cursor'} if action == 'requests' else set()), 'invalid_request', 400)
                selection = service.chosen(fields['destinationId'], fields['scopeId'])
                status = 200
                if action == 'request':
                    service.phase_counts['requests'] += 1
                    value = {'observation': service.observation(match[1], selection)}
                elif action == 'requests':
                    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                    encoded = service.request_page(selection, fields.get('cursor'), stamp)
                elif action == 'details':
                    ident(fields['requestId'])
                    service.catalogue_scope(selection)
                    service.phase_counts['details'] += 1
                    value = {**selection, 'item': service.file_row(selection, fields['requestId'], match[1], [1, COPY_BUDGET])}
                else:
                    value = service.receipt(selection, fields['draftId'], fields['commandDigest'])
            else:
                insist(not parsed.query and re.fullmatch('[1-9][0-9]{0,3}', length or '') is not None and
                       one('Content-Type') == 'application/json', 'invalid_request', 400)
                reader = LimitedReader(handler, int(length), 2)
                raw = bytearray()
                while len(raw) < int(length):
                    chunk = reader.read(int(length) - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                insist(len(raw) == int(length), 'invalid_request', 400)
                data = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=unique_pairs,
                                  parse_constant=lambda _: insist(False, 'invalid_request', 400))
                if action == 'prepare':
                    value = service.prepare(match[1], headers[HEADERS[0]], headers[HEADERS[2]], data)
                    status = 201
                else:
                    value = service.send(match[1], headers[HEADERS[1]], headers[HEADERS[2]], data)
                    status = 200
            if action != 'requests':
                encoded = envelope(value)
            insist(len(encoded) <= CAP, 'response_limit', 503)
    except AttachmentError as error:
        status = error.status
        encoded = envelope({'error': {'code': error.code, 'message': 'Fixture attachment request refused.', 'retryable': False}})
    except (ValueError, TypeError, UnicodeError, RecursionError, OSError, LedgerError):
        status = 503
        encoded = envelope({'error': {'code': 'attachments_unavailable', 'message': 'Fixture attachment request refused.',
                                      'retryable': False}})
    handler.send(status, encoded, 'application/json; charset=utf-8', extra=FIXED_HEADERS)
    return True


def deletion_route(handler, service):
    """Only the explicit fixture routes are available in deletion mode."""
    def one(name, required=True):
        values = handler.headers.get_all(name, [])
        insist(len(values) == 1 if required else len(values) <= 1, 'invalid_request', 400)
        return values[0] if values else None

    try:
        owner = one('X-Live-Room-Key', False)
        insist(owner is not None and hmac.compare_digest(owner.encode(), KEY.encode()), 'unauthorized', 401)
        port = handler.server.server_port
        host, origin = one('Host'), one('Origin', False)
        insist(host in (f'127.0.0.1:{port}', f'localhost:{port}') and origin in (None, 'http://' + host),
               'forbidden', 403)
        parsed = urlsplit(handler.path)
        prefix = '/api/attachments'
        insist(handler.path.startswith(prefix + '/') and not parsed.scheme and not parsed.netloc
               and '#' not in handler.path and not re.search(r'%(?![0-9a-fA-F]{2})', parsed.query)
               and one('Transfer-Encoding', False) is None, 'invalid_request', 400)
        path = parsed.path[len(prefix):]
        found = next(((match, method, action) for pattern, method, action in DELETION_ROUTES
                      if (match := re.fullmatch(pattern, path))), None)
        insist(found is not None, 'not_found', 404)
        match, method, action = found
        insist(handler.command == method, 'method_not_allowed', 405)
        target = match[1] if match.lastindex else None
        permitted = {'prepare': {'Idempotency-Key'}, 'confirm': {'Idempotency-Key', DELETION_CAPABILITY}}.get(action, set())
        headers = {name: one(name) for name in permitted}
        header_names = {name.lower() for name in permitted}
        insist(all((not name.lower().startswith('x-attachment-') and name.lower() != 'idempotency-key')
                   or name.lower() in header_names for name in handler.headers), 'invalid_request', 400)
        length = one('Content-Length', False)
        if method == 'GET':
            insist(length in (None, '0') and one('Content-Type', False) is None, 'invalid_request', 400)
            fields = unique_pairs(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                                             errors='strict', max_num_fields=4))
            required = {'destinationId'} if action == 'selection' else {'destinationId', 'scopeId'}
            required |= {'files': {'conversationId'}, 'details': {'requestId'},
                         'receipts': {'fileId', 'commandDigest'}}.get(action, set())
            optional = {'cursor'} if action in ('files', 'management') else set()
            insist(required <= set(fields) <= required | optional, 'invalid_request', 400)
            with service.flow:
                value = service.read(action, target, fields)
            status = 200
        else:
            insist('?' not in handler.path and re.fullmatch('[1-9][0-9]{0,3}', length or '') is not None
                   and int(length) <= 4096 and one('Content-Type') == 'application/json', 'invalid_request', 400)
            reader = LimitedReader(handler, int(length), 2)
            raw = bytearray()
            while len(raw) < int(length):
                chunk = reader.read(int(length) - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            insist(len(raw) == int(length), 'invalid_request', 400)
            body = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=unique_pairs,
                              parse_constant=lambda _: insist(False, 'invalid_request', 400))
            with service.flow:
                value = service.mutate(action, target, body, headers)
            status = 201 if action == 'prepare' else 200
        encoded = envelope(value)
        insist(len(encoded) <= CAP, 'response_limit', 503)
    except AttachmentError as error:
        status, code = error.status, error.code
        encoded = envelope({'error': {'code': code, 'message': 'Fixture attachment request refused.', 'retryable': False}})
    except (ValueError, TypeError, UnicodeError, RecursionError, OSError):
        status = 400
        encoded = envelope({'error': {'code': 'invalid_request', 'message': 'Fixture attachment request refused.',
                                      'retryable': False}})
    try:
        handler.send(status, encoded, 'application/json; charset=utf-8', extra=FIXED_HEADERS)
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


def deletion_console(service):
    while raw := sys.stdin.readline(4097):
        if len(raw) > 4096 or not raw.endswith('\n'):
            print(json.dumps({'error': 'invalid_request'}), flush=True)
            return
        try:
            command = json.loads(raw, object_pairs_hook=unique_pairs,
                                 parse_constant=lambda _: insist(False, 'invalid_request', 400))
            with service.flow:
                result = service.console(command)
        except AttachmentError as error:
            result = {'error': error.code}
        except (ValueError, TypeError, UnicodeError, RecursionError, OSError):
            result = {'error': 'invalid_request'}
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deletion', action='store_true', help='Use the qualified disposable deletion fixture')
    args = parser.parse_args()

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with ExitStack() as stack:
        if args.deletion:
            deletion = DeletionFixture(stack)
            with serve_native_reads(deletion, KEY, extra_route=lambda handler: deletion_route(handler, deletion)) as server:
                print(f'http://127.0.0.1:{server.server_port}', flush=True)
                deletion_console(deletion)
            insist(server.outbound_blocked == 0)
            return

        class Cleanup:
            addCleanup = stack.callback

            @staticmethod
            def assertFalse(value):
                assert not value

        service = StageFixture(NativeFixture(Cleanup()))
        stack.callback(service.close)
        with serve_native_reads(service, KEY, extra_route=lambda handler: route(handler, service)) as server:
            print(f'http://127.0.0.1:{server.server_port}', flush=True)
            for raw in sys.stdin:
                command = json.loads(raw)
                action = command['action']
                with service.flow:
                    if action == 'mode':
                        insist(command['mode'] in ('pending', 'queued', 'unknown', 'not_submitted', 'partial', 'mirror_slot'))
                        service.mode = command['mode']
                    elif action == 'outcome':
                        service.add_owner_outcome(command['requestId'], command['kind'])
                    elif action == 'damage':
                        service.damage(command['requestId'], command['kind'])
                    elif action == 'drift':
                        service.drift = ident(command['requestId'])
                    elif action == 'retire_epoch':
                        with service.store._gate(), service.store._transaction():
                            row = service.request_row(ident(command['requestId']), service.chosen(ADA, SCOPE))
                            service.store.db.execute('UPDATE attachment_requests SET epoch=? WHERE id=?', ('e' * 64, row['id']))
                    elif action == 'seed':
                        service.seed(command['count'], command['kind'])
                    else:
                        insist(action == 'counters')
                    print(json.dumps(service.counters()), flush=True)
        insist(server.outbound_blocked == 0)


if __name__ == '__main__':
    main()
