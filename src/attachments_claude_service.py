"""Owner-facing attachment requests for exact local agent destinations."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
import stat

import claude_mod
import peer_lifecycle
from attachments_materialize import (DirectoryIdentity, FileIdentity, Materializer, WorkspaceContext,
                                     WorkspaceDirectories, copy_identity)
from attachments_native_namespace import NamespaceError
from attachments_native_reads import NativeAgentReadService
from attachments_native_receipts import command_digest, draft_binding
from claude_attachment_adapter import LedgerError
from attachments_relay import RELAY_FORMATS
from attachments_requests import TransportProfile, token
from attachments_resources import ResourceLocks, directory
from attachments_service import closed
from attachments_types import AttachmentError, canonical, digest, fail, ident, integer, key

PROFILE = 'claude-completion-only'
EARLIER_DELIVERY_UNRESOLVED = 'earlier_delivery_unresolved'
FORMATS = tuple(sorted(RELAY_FORMATS))
RECEIPTS = '''CREATE TABLE claude_native_prepare_receipts (
    command_digest TEXT PRIMARY KEY NOT NULL CHECK(length(command_digest)=64),
    seen_key TEXT NOT NULL UNIQUE REFERENCES seen(key) ON DELETE RESTRICT,
    request_id TEXT NOT NULL UNIQUE REFERENCES attachment_requests(id) ON DELETE RESTRICT,
    command_fingerprint TEXT NOT NULL CHECK(length(command_fingerprint)=64)
)'''
VERSION = 'claude_native_send_version'
GIT = '/usr/bin/git'


def unavailable():
    fail('destination_unavailable', 'This Claude attachment destination could not be verified.', 503)


def conflict():
    fail('request_conflict', 'This Claude attachment request could not be verified.', 409)


class ClaudePrepareIndex:
    def __init__(self, store):
        self.store = store
        with store._gate(), store._transaction():
            table, version = self._layout()
            if table is None and version is None:
                store.db.execute(RECEIPTS)
                store.db.execute('INSERT INTO main.meta VALUES (?,?)', (VERSION, '1'))
            else:
                self.require()

    def _layout(self):
        db = self.store.db
        if db.execute("""SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN
                         ('meta','seen','claude_native_prepare_receipts','attachment_requests') LIMIT 1""").fetchone():
            unavailable()
        table = db.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name='claude_native_prepare_receipts'").fetchone()
        version = db.execute('SELECT value FROM main.meta WHERE key=?', (VERSION,)).fetchone()
        return table, version

    def require(self):
        table, version = self._layout()
        if (table is None or table[0] != RECEIPTS or version is None or version[0] != '1'
                or self.store.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1):
            unavailable()

    def record(self, store, command_key, request_id, fingerprint):
        if store is not self.store or not store.db.in_transaction:
            unavailable()
        self.require()
        store.db.execute('INSERT INTO main.claude_native_prepare_receipts VALUES (?,?,?,?)',
                         (command_digest(command_key), command_key, request_id, fingerprint))

    def match(self, value):
        token(value)
        self.require()
        row = self.store.db.execute('''SELECT p.*,s.epoch,s.kind,s.target,s.fingerprint
            FROM main.claude_native_prepare_receipts p LEFT JOIN main.seen s ON s.key=p.seen_key
            WHERE p.command_digest=?''', (value,)).fetchone()
        if row is None:
            return None
        if (row['epoch'] is None or row['kind'] != 'attachment_intent'
                or row['target'] != row['request_id'] or row['fingerprint'] != row['command_fingerprint']
                or command_digest(row['seen_key']) != value):
            unavailable()
        return row['request_id'] if row['epoch'] == self.store.owner_epoch else None


def _folder(path):
    fd = directory(path)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            unavailable()
        return DirectoryIdentity.of(info)
    finally:
        os.close(fd)


def _workspace(project):
    """Limit this integration to an owned, ordinary Git checkout at the project root."""
    git, info = os.path.join(project, '.git'), os.path.join(project, '.git', 'info')
    root_identity, git_identity, info_identity = map(_folder, (project, git, info))
    if os.path.lexists(os.path.join(git, 'commondir')):
        unavailable()
    executable = os.path.realpath(GIT)
    st = os.stat(executable, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or not st.st_mode & 0o111:
        unavailable()
    executable_identity = FileIdentity.of(st)
    workspace_id = digest({'project': project, 'root': asdict(root_identity)})
    identity = digest({'workspace': workspace_id, 'git': asdict(git_identity), 'info': asdict(info_identity),
                       'executable': executable, 'executableIdentity': asdict(executable_identity)})
    return WorkspaceContext(identity, workspace_id, project, root_identity, git, git_identity,
                            info, info_identity, executable, executable_identity)


class ClaudeAttachmentService:
    def __init__(self, composition, messaging, room, receipts, index):
        self.codex = None
        self.codex_room = composition.codex_room
        self.composition, self.messaging, self.room = composition, messaging, room
        self.store, self.mirror = composition.mirror.store, composition.mirror
        self.requests = self.mirror.requests
        self.receipts = receipts
        self.index = index
        self.resources = ResourceLocks(str(self.store.files.root), enabled=True)
        self.materializer = Materializer(
            enabled=True, workspace_files=self.store.files,
            validate_authority=lambda authority, phase, copies: self.requests.authority(authority.request_id) == authority,
            record_directory=self.requests.record_directory_created,
            record_created=self.requests.record_copy_created,
            record_partial=self.requests.record_partial,
        )
        self.adapter = None

    @contextmanager
    def operation(self):
        # The composition owns the store and drains every HTTP, mod and worker operation.
        with self.composition.claude_http_operation():
            yield self

    def _current(self, destination_id):
        if not isinstance(destination_id, str) or not destination_id.startswith('c:') or not claude_mod.ident(destination_id[2:]):
            return None
        item = self.room().get(destination_id)
        if not claude_mod.eligible(destination_id, item):
            return None
        try:
            workspace = _workspace(item['cwd'])
        except (AttachmentError, OSError, ValueError):
            return None
        scope = 'station:' + destination_id + ':' + workspace.workspace_fingerprint[:32]
        with self.messaging.lock:
            current = self.room().get(destination_id)
            if not claude_mod.eligible(destination_id, current) or current['cwd'] != item['cwd']:
                return None
            try:
                with self.composition._permission.namespace.admission(self.messaging.db, '', unmanaged_native=destination_id[2:]):
                    pass
            except NamespaceError:
                return None
            entry = self.messaging.mods.attachment_entry(destination_id, current)

        return scope, workspace, entry

    def target(self, destination_id, scope_id=None):
        if isinstance(destination_id, str) and destination_id.startswith('x:'):
            selection, binding, source = self._codex_target(destination_id, scope_id)
            if binding is not None or self.codex is None:
                return selection, binding, source
            return self._retained_codex(destination_id, scope_id), None, None
        current = self._current(destination_id)
        empty = (None, None, None)
        if current is None or scope_id is not None and current[0] != scope_id:
            return empty
        scope, workspace, entry = current
        if entry is None:
            with self.store._gate():
                draft = self.store.db.execute('''SELECT id FROM main.drafts
                    WHERE destination_id=? AND scope_id=? AND state='active' ORDER BY created,id LIMIT 1''',
                    (destination_id, scope)).fetchone()
                if draft is None:
                    return empty
            return {'mode': 'retained_only', 'destinationId': destination_id, 'scopeId': scope, 'anchorDraftId': draft[0]}, None, None
        revision = digest({'workspace': workspace.fingerprint, 'protocol': 2, 'agent': destination_id,
                           'enrollment': entry['id'], 'grant': entry['grant_id']})
        limits = self.store.limits_view()
        selection = dict(mode='current', destinationId=destination_id, scopeId=scope, capabilityRevision=revision,
                         formats=FORMATS, limits={field: limits[field] for field in ('fileBytes', 'draftBytes', 'fileCount')})
        from claude_attachment_adapter import ClaudeAttachmentBinding
        profile = TransportProfile(PROFILE, '1', 'claude_official', revision, workspace.fingerprint,
                                   workspace.workspace_fingerprint, 'derived-lifecycle-2', FORMATS,
                                   original_relay_formats=FORMATS)
        binding = {'kind': 'agent', 'id': destination_id, 'scopeId': scope, 'modelId': None,
                   'conversationId': None, 'capabilityRevision': revision}
        return selection, ClaudeAttachmentBinding(canonical(binding), profile, workspace), entry

    def _codex_target(self, destination_id, scope_id):
        empty = (None, None, None)
        if self.codex is None or not claude_mod.ident(destination_id[2:]) or not self.codex.queue.ready():
            return empty
        from codex_attachment_dispatch import PROFILE as codex_profile, CodexAttachmentBinding, Target
        item = self.codex_room().get(destination_id)
        if (not item or item.get('runtime') != 'Codex' or item.get('observed') or item.get('sub')
                or item.get('parent') or item.get('gone') or not isinstance(item.get('cwd'), str)):
            return empty
        try:
            workspace = _workspace(item['cwd'])
        except (AttachmentError, OSError, ValueError):
            return empty
        scope = 'station:' + destination_id + ':' + workspace.workspace_fingerprint[:32]
        if scope_id is not None and scope_id != scope:
            return empty
        root = workspace.root_identity
        target = Target(destination_id[2:], workspace.root, root.device, root.inode)
        if not self.codex.queue.selection(target):
            return empty
        revision = digest({'workspace': workspace.fingerprint, 'profile': codex_profile, 'agent': destination_id})
        limits = self.store.limits_view()
        selection = dict(mode='current', destinationId=destination_id, scopeId=scope, capabilityRevision=revision,
                         formats=FORMATS, limits={field: limits[field] for field in ('fileBytes', 'draftBytes', 'fileCount')})
        profile = TransportProfile(codex_profile, '1', 'codex', revision, workspace.fingerprint,
                                   workspace.workspace_fingerprint, 'desktop-24614-terminal-1', FORMATS,
                                   original_relay_formats=FORMATS)
        binding = {'kind': 'agent', 'id': destination_id, 'scopeId': scope, 'modelId': None,
                   'conversationId': None, 'capabilityRevision': revision}
        return selection, CodexAttachmentBinding(canonical(binding), profile, workspace), target

    def _retained_codex(self, destination_id, scope_id):
        # Exact known scopes stay readable when the live target disappears or
        # an app update invalidates Send. Unknown scopes cannot borrow a draft.
        args = [self.store.owner_epoch, destination_id]
        scope_filter = ''
        if scope_id is not None:
            scope_filter = 'AND r.catalogue_scope=?'
            args.append(scope_id)
        with self.store._gate():
            rows = self.store.db.execute("""SELECT r.catalogue_scope, min(r.draft_id) AS draft_id
                FROM main.attachment_requests r JOIN main.drafts d ON d.id=r.draft_id
                WHERE r.epoch=? AND r.catalogue_destination=? AND r.authorized_at IS NOT NULL
                  AND r.catalogue_kind='agent' AND r.catalogue_conversation IS NULL
                  AND r.profile IS NOT NULL AND d.admitted_request=r.id """ + scope_filter + """
                GROUP BY r.catalogue_scope LIMIT 2""", args).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
            return {'mode': 'retained_only', 'destinationId': destination_id,
                    'scopeId': row['catalogue_scope'], 'anchorDraftId': row['draft_id']}

    def _adapter(self, destination_id):
        adapter = self.codex if destination_id.startswith('x:') else self.adapter
        if adapter is None:
            unavailable()
        return adapter

    def scoped_reads(self, destination_id, scope_id=None):
        selection, binding, enrollment = self.target(destination_id, scope_id)
        reads = NativeAgentReadService(self.store, selections=[selection] if selection is not None else [], _receipts=self.receipts)
        return reads, binding, enrollment

    def native_selection(self, destination_id):
        reads, _, _ = self.scoped_reads(destination_id)
        result = reads.native_selection(destination_id)
        if result['context'] is not None:
            result['features']['send'] = True
            result['completionOnly'] = True
        return result

    def draft_target(self, draft_id):
        with self.store._gate():
            row = self.store._draft(draft_id)
            binding = draft_binding(row)
        return binding

    def native_draft(self, draft_id):
        binding = self.draft_target(draft_id)
        reads, _, _ = self.scoped_reads(binding['id'], binding['scopeId'])
        return reads

    def _scope(self, destination_id, scope_id):
        if (self.codex is not None and destination_id.startswith('x:')
                and self._retained_codex(destination_id, scope_id) is not None):
            return
        reads, _, _ = self.scoped_reads(destination_id, scope_id)
        reads._scope(destination_id, scope_id)

    @staticmethod
    def selection(destination_id, scope_id):
        return {'destinationId': destination_id, 'scopeId': scope_id}

    def _row(self, request_id, destination_id=None, scope_id=None):
        row = self.requests._request(request_id)
        profile = json.loads(row['profile'])
        binding = json.loads(row['destination'])
        valid_profiles = {'c:': (PROFILE, 'claude_official'), 'x:': ('codex-completion-only', 'codex')}
        if (row['epoch'] != self.store.owner_epoch or valid_profiles.get(binding['id'][:2]) !=
                (profile.get('id'), profile.get('adapter')) or row['catalogue_kind'] != 'agent'
                or row['catalogue_conversation'] is not None or binding['kind'] != 'agent'
                or binding['modelId'] is not None or binding['conversationId'] is not None
                or (binding['id'], binding['scopeId']) != (row['catalogue_destination'], row['catalogue_scope'])
                or destination_id is not None and (binding['id'], binding['scopeId']) != (destination_id, scope_id)):
            fail('forbidden', 'The Claude attachment request scope could not be authorized.', 403)
        return row

    def prepare(self, draft_id, capability, body, command):
        closed(body, {'revision', 'writerGeneration', 'writerNonce'})
        token(body['writerNonce']); token(capability); key(command)
        if not integer(body['revision'], 1) or not integer(body['writerGeneration'], 1):
            fail('invalid_request', 'Invalid attachment writer fence.', 400)
        prior = self.draft_target(draft_id)
        _, binding, _ = self.target(prior['id'], prior['scopeId'])
        if binding is None:
            unavailable()
        resolved = json.loads(binding.destination_record)
        if resolved != prior:
            conflict()
        result = self.mirror.prepare_request(draft_id, capability, body, command, resolved, binding.profile,
                                             _prepare_index=self.index)
        return {'observation': self._observation(result['id'], prior['id'], prior['scopeId']),
                'requestCapability': result['capability'], 'fingerprint': result['fingerprint']}

    def receipt(self, destination_id, scope_id, draft_id, command_digest_value):
        ident(draft_id)
        self._scope(destination_id, scope_id)
        with self.store._gate():
            rid = self.index.match(command_digest_value)
            if rid is None:
                return {'match': None}
            row = self._row(rid, destination_id, scope_id)
            if row['draft_id'] != draft_id:
                return {'match': None}
            try:
                self.mirror._as_of(rid)
            except AttachmentError:
                return {'match': None}
            return {'match': {'draftId': draft_id, 'requestId': rid}}

    def _refuse_blocked_send(self, request_id, capability, body, destination_id):
        if not destination_id.startswith('c:'):
            return False
        with self.messaging.lock:
            blocked = any(state == 'uncertain' for _, state, _ in
                          peer_lifecycle.blockers(self.messaging.db, destination_id))
        if not blocked:
            return False
        with self.store._gate(), self.store._transaction(reclaim=True):
            row = self.requests._authorize(request_id, capability, body['revision'], body['fingerprint'])
            if row['stage'] != 'intent' or row['authorized_at'] is not None:
                conflict()
            # Exact readback proves refusal even if the HTTP response is lost.
            # Revoke the capability so an older server would also reject replay.
            self.store.db.execute("UPDATE attachment_requests SET cap_hash='',error=? WHERE id=?",
                                  (EARLIER_DELIVERY_UNRESOLVED.ljust(64), request_id))
        return True

    def send(self, request_id, capability, body, command):
        closed(body, {'revision', 'fingerprint'})
        token(capability); token(body['fingerprint']); key(command)
        if not integer(body['revision'], 1):
            fail('invalid_request', 'Invalid attachment request fence.', 400)
        with self.store._gate():
            row = self._row(request_id)
            destination_id, scope_id = row['catalogue_destination'], row['catalogue_scope']
        _, binding, enrollment = self.target(destination_id, scope_id)
        if binding is None:
            unavailable()
        self.composition._permission.prepare_admission()
        saved = []
        with self.resources.hold(self.requests.resource_keys(request_id)) as lease:
            with self.store._gate():
                row = self._row(request_id, destination_id, scope_id)
                if row['stage'] != 'intent' or row['authorized_at'] is not None:
                    conflict()
            if self._refuse_blocked_send(request_id, capability, body, destination_id):
                return {'observation': self._observation(request_id, destination_id, scope_id)}
            status = self.mirror.authorize_send(request_id, capability, body['revision'], body['fingerprint'],
                resolved_binding=json.loads(binding.destination_record), transport_profile=binding.profile,
                resource_lease=lease)
            authority = self.requests.authority(request_id)
            dirs = WorkspaceDirectories.from_record(self.requests.workspace_directories(request_id))
            dirs = self.materializer.prepare_directories(binding.context, authority, lease, directories=dirs)
            for row in status['copies']:
                authority = self.requests.authority(request_id)
                copy = self.requests.claim_copy(request_id, authority.operation_revision, row['id'], lease)
                authority = self.requests.authority(request_id)
                self.materializer.ensure_exclusion(binding.context, authority, (copy,), lease)
                with self.requests.source_handle(copy.id, lease) as fd:
                    item = self.materializer.materialize(binding.context, authority, copy, fd,
                        copy_identity(FileIdentity.of(os.fstat(fd))), lease, directories=dirs)
                    self.requests.record_copy_result(item, lease)
                saved.append(item)
        _, current, current_enrollment = self.target(destination_id, scope_id)
        if current != binding or current_enrollment is None or enrollment is None:
            unavailable()
        if destination_id.startswith('x:'):
            if current_enrollment != enrollment:
                unavailable()
        elif any(current_enrollment[field] != enrollment[field] for field in ('id', 'grant_id', 'agent', 'project')):
            unavailable()
        try:
            self._adapter(destination_id).offer(request_id, tuple(saved), binding, enrollment=enrollment)
        except LedgerError:
            # A local admission may already be committed. Only a scoped read can
            # establish the exact facts. No second offer follows an uncertain call.
            pass
        return {'observation': self._observation(request_id, destination_id, scope_id)}

    def _observation(self, request_id, destination_id, scope_id):
        selection = self.selection(destination_id, scope_id)
        with self.store._gate():
            row = dict(self._row(request_id, destination_id, scope_id))
            draft = self.store._draft(row['draft_id'])
            admitted = self.store.admission_kind(draft) == 'native' and draft['admitted_request'] == request_id
            if (admitted != (row['authorized_at'] is not None) or draft['destination'] != row['destination']):
                conflict()
            files = []
            for member in self.requests._members(request_id):
                copy = self.requests._copy(member['workspace_id'])
                if (copy['request_id'] != request_id or copy['source_id'] != member['source_id']
                        or copy['bytes'] != member['bytes'] or copy['digest'] != member['digest']
                        or copy['role'] != 'workspace' or member['preparation_kind'] != 'original_relay'
                        or member['media'] not in FORMATS):
                    conflict()
                states = {'planned': 'not_saved', 'reserved': 'not_saved', 'writing': 'saving',
                          'partial': 'unavailable', 'removed': 'removed' if copy['released'] else 'unknown'}
                files.append(dict(fileId=member['source_id'], name=member['name'], mediaType=member['media'],
                                  bytes=member['bytes'], savedCopyState=states.get(copy['state'], 'unknown')))
            if not 1 <= len(files) <= 5:
                conflict()
            facts = self.mirror._as_of(request_id)
            claimed = bool(self.store.db.execute('SELECT claimed_at FROM main.attachment_outbox WHERE request_id=?',
                                                 (request_id,)).fetchone()[0] is not None)
        reply = {'state': 'unknown', 'text': None}
        delivery, reason = ('pending' if admitted else 'not_started'), None
        accepted = False
        if row['error'].strip() == EARLIER_DELIVERY_UNRESOLVED:
            if (not destination_id.startswith('c:') or admitted or row['cap_hash'] or row['stage'] != 'intent'
                    or row['revision'] != 1 or claimed or facts is not None
                    or any(file['savedCopyState'] != 'not_saved' for file in files)):
                conflict()
            reason = EARLIER_DELIVERY_UNRESOLVED
        elif claimed:
            # Read the exact live owner pair. Missing or lagged observations never supply prose.
            try:
                live = self._adapter(destination_id).observe(request_id)
                if destination_id.startswith('x:') and live is not None:
                    facts = self.mirror._as_of(request_id)
            except (AttachmentError, LedgerError):
                live = None
            if live is not None and facts is not None and live['outcome'] != facts[1]:
                live = None
            if facts:
                _, outcome = facts
                if outcome['execution_ended'] and outcome['accepted'] and not outcome['started']:
                    accepted, delivery = True, 'completed'
                    if live and live['reply'] is not None:
                        reply = {'state': 'available', 'text': live['reply']}
                    elif outcome['reply_captured'] or outcome['capture_withheld']:
                        reply['state'] = 'withheld'
            if not accepted:
                delivery, reason = 'unknown', 'dispatch_unconfirmed'
        elif any(file['savedCopyState'] == 'unavailable' for file in files):
            reason = 'copy_failed'
        elif row['stage'] == 'materialized':
            delivery, reason = 'unknown', 'dispatch_unconfirmed'
        phase = row['stage']
        preparation = (phase if phase in ('intent', 'materialized', 'cleanup', 'cleaned') else
                       'preparing_delivery' if phase in ('send_authorized', 'materializing') else 'materialized')
        return {**selection, 'request': {
            'id': request_id, 'draftId': row['draft_id'], 'revision': row['revision'], 'createdAt': row['created'],
            'text': row['text'], 'preparation': preparation, 'files': files, 'delivery': delivery,
            'accepted': accepted, 'execution': 'ended' if accepted else 'unobserved', 'reply': reply, 'reason': reason,
        }, 'draftFence': {'revision': row['draft_revision'], 'writerGeneration': row['draft_generation']},
            'draftAdmission': 'matched' if admitted else 'unadmitted'}

    def status(self, request_id, destination_id, scope_id):
        self._scope(destination_id, scope_id)
        return {'observation': self._observation(request_id, destination_id, scope_id)}

    def history(self, destination_id, scope_id, cursor):
        self._scope(destination_id, scope_id)
        filters = ['codex-completion-only' if destination_id.startswith('x:') else PROFILE, destination_id, scope_id]
        with self.store._gate():
            after = self.store._parse_cursor(cursor, filters) if cursor is not None else [-1, '0' * 32]
            rows = self.store.db.execute('''SELECT id,created FROM main.attachment_requests
                WHERE catalogue_kind='agent' AND catalogue_destination=? AND catalogue_scope=?
                AND catalogue_conversation IS NULL AND epoch=? AND authorized_at IS NOT NULL
                AND (created>? OR (created=? AND id>?)) ORDER BY created,id LIMIT 51''',
                (destination_id, scope_id, self.store.owner_epoch, after[0], *after)).fetchall()
        size, items = 0, []
        for row in rows[:50]:
            item = self._observation(row['id'], destination_id, scope_id)
            size += len(json.dumps(item, ensure_ascii=True).encode())
            if size > 234000:
                break
            items.append(item)
        if rows and not items:
            fail('response_limit', 'This attachment response exceeds its safe limit.', 503)
        next_cursor = self.store._cursor(filters, [rows[len(items) - 1]['created'], rows[len(items) - 1]['id']]) if len(rows) > len(items) else None
        return {'items': items, 'nextCursor': next_cursor}
