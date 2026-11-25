"""Private native attachment composition and exact Claude enrollment mount."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
import threading

from attachments_materialize import Materializer
from attachments_native_handoff import FictionalAdapterBinding, OfflineNativeHandoff
from attachments_native_mirror import NativeOutcomeMirror
from attachments_native_namespace import OWNER_DB, NamespaceError, NativeNamespaceFence, same_database_path
from attachments_native_reads import NativeAgentReadService
from attachments_native_receipts import NativeReadReceipts
from attachments_owner_runtime import bind_owner_runtime
from attachments_owner_marker import sqlite_marker_available
from attachments_requests import RequestLedger, _native_mirror_layout
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError, fail

VERSION_KEY = 'native_attachment_composition_version'


def _unavailable():
    fail('native_composition_unavailable', 'The native attachment composition is unavailable.', 503)


def _layouts(store):
    shadowed = """SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN
                  ('meta','native_outcome_mirror','native_read_receipts','native_read_pending_cleanup',
                   'attachment_requests','attachment_outbox','seen','drafts','files','uploads','discard_slots') LIMIT 1"""
    if store.db.execute(shadowed).fetchone():
        _unavailable()
    version = store.db.execute('SELECT value FROM main.meta WHERE key=?', (VERSION_KEY,)).fetchone()
    mirror, receipts = _native_mirror_layout(store), NativeReadReceipts._layout(store)
    if version is not None and (version[0] != '1' or not mirror or not receipts):
        _unavailable()
    return version is not None, mirror, receipts


def _open_schema(store, *, allow_install):
    with store._gate():
        complete, mirror, receipts = _layouts(store)
        if not complete:
            if not allow_install or (not mirror or not receipts) and getattr(store, '_native_read_caches', None):
                _unavailable()
            with store._transaction():
                _layouts(store)
                NativeOutcomeMirror._install(store)
                NativeReadReceipts._install(store)
                store.db.execute('INSERT INTO main.meta VALUES (?,?)', (VERSION_KEY, '1'))
                _layouts(store)
        requests = RequestLedger(store)
        return NativeOutcomeMirror._open_committed(requests), NativeReadReceipts._open_committed(store)


def _same_root(messaging, store):
    with store._gate():
        expected = str(Path(store.files.parent) / OWNER_DB)
    with messaging.lock:
        row = messaging.db.execute('PRAGMA database_list').fetchone()
        # POSIX: the same string. Windows: the store resolves the folders above its own (``anchored``) and Windows
        # names ignore case, so the names are compared that way (``same_database_path``).
        if row is None or tuple(row[:2]) != (0, 'main') or not same_database_path(row[2], expected):
            _unavailable()


@dataclass(frozen=True)
class _FictionalQualification:
    """Internal disposable proof only. Production has no constructor caller."""

    binding: FictionalAdapterBinding
    resources: ResourceLocks
    materializer: Materializer
    boundary: object

    def __post_init__(self):
        if (type(self.binding) is not FictionalAdapterBinding or type(self.resources) is not ResourceLocks
                or type(self.materializer) is not Materializer or not callable(self.boundary)):
            _unavailable()

    def selections(self, store):
        target = json.loads(self.binding.destination_record)
        profile = self.binding.profile
        if not profile.original_relay_formats:
            return ()
        limits = store.limits_view()
        return (dict(mode='current', destinationId=target['id'], scopeId=target['scopeId'],
                     capabilityRevision=profile.capability_revision, formats=profile.original_relay_formats,
                     limits={key: limits[key] for key in ('fileBytes', 'draftBytes', 'fileCount')}),)


@dataclass
class _Operation:
    kind: str
    request_id: str
    epoch: str
    generation: int
    admission: tuple | None = None
    evidence: tuple | None = None
    ack: tuple | None = None
    claude: tuple | None = None
    claude_native: str | None = None


class _NativePermission:
    def __init__(self, messaging, mirror, qualification, *, production=False):
        self.messaging = messaging
        self.mirror = mirror
        self.binding = qualification.binding if qualification is not None else None
        self.production = production
        self.runtime = None
        self.namespace = NativeNamespaceFence(mirror.store.files.parent)
        self.condition = threading.Condition()
        self.thread_scope = threading.local()
        self.accepting = True
        self.active = 0
        self.generation = 1
        with messaging.lock:
            self.namespace.validate_mount(messaging.db)

    def _current(self, scope):
        if (scope is None or not self.accepting or scope.generation != self.generation
                or scope.epoch != self.mirror.store.owner_epoch):
            _unavailable()

    @contextmanager
    def operation(self, kind, request_id):
        with self.condition:
            if not self.accepting or getattr(self.thread_scope, 'scope', None) is not None:
                _unavailable()
            scope = _Operation(kind, request_id, self.mirror.store.owner_epoch, self.generation)
            self.thread_scope.scope = scope
            self.active += 1
        try:
            yield scope
        finally:
            with self.condition:
                self.thread_scope.scope = None
                self.active -= 1
                self.condition.notify_all()

    def check(self, action=None, *identity):
        scope = getattr(self.thread_scope, 'scope', None)

        def recheck():
            with self.condition:
                self._current(scope)
                if self.thread_scope.scope is not scope:
                    _unavailable()
                if action is None:
                    return
                if action in ('admit', 'claim') and scope.kind == 'handoff' and scope.admission == identity:
                    return
                if action == 'evidence' and scope.kind == 'evidence' and scope.evidence == identity:
                    return
                if action == 'ack' and scope.kind == 'reconcile' and scope.ack == identity:
                    return
                if (self.production and action in ('claude_claim', 'claude_terminal', 'claude_terminal_conflict')
                        and scope.kind == action and scope.claude == identity):
                    return
                _unavailable()

        recheck()
        return recheck

    def expected(self, scope):
        # The detached attachment proof must be read before taking the owner lock.
        if self.messaging.lock._is_owned():
            _unavailable()
        expected = self.mirror._expected(scope.request_id)
        admission = expected.admission
        if (admission.owner_epoch != scope.epoch
                or self.binding is not None and admission.target_binding_digest != self.binding.digest
                or self.production and (admission.transport, admission.profile) not in (
                    ('claude', 'claude-completion-only'), ('codex', 'codex-completion-only'))):
            _unavailable()
        return expected

    def bind_admission(self, owner, admission, admission_digest):
        self.check()
        scope = self.thread_scope.scope
        if (scope.kind != 'handoff' or scope.request_id != admission.attachment_request_id
                or owner.db is not self.messaging.db or owner.lock is not self.messaging.lock):
            _unavailable()
        expected = self.expected(scope)
        if expected.digest != admission_digest or expected.admission != admission:
            _unavailable()
        scope.admission = (admission.owner_request_id, admission_digest)

    @contextmanager
    def admission_fence(self, owner, owner_id):
        try:
            scope = self.thread_scope.scope
            native = getattr(scope, 'claude_native', None)
            with owner.lock:
                if native is not None and scope.kind == 'handoff':
                    self.prepare_admission()
                with self.namespace.admission(owner.db, owner_id, unmanaged_native=native):
                    yield
        except NamespaceError as error:
            fail(error.code, 'The native request namespace could not be authorized.',
                 409 if error.code == 'request_conflict' else 503)

    def prepare_admission(self):
        runtime = self.runtime
        if runtime is None:
            _unavailable()
        with self.messaging.lock:
            directory = runtime.ledger.directory
            if not directory.matches(directory.epoch()):
                runtime.refresh_directory()
            if not directory.matches(directory.epoch()):
                _unavailable()

    def stop_entry(self, handoff):
        # Close the physical permit gate before releasing the lifecycle mutex.
        with self.condition:
            if self.accepting:
                handoff.stop()
                self.accepting = False
                self.generation += 1

    def drain(self, timeout):
        with self.condition:
            if not self.condition.wait_for(lambda: self.active == 0, timeout):
                _unavailable()


class NativeAttachmentComposition:
    """Shares the original services' stores. It is never an HTTP service."""

    def __init__(self, messaging, base_service, mirror, receipts, qualification, *, claude_room=None, claude_index=None, codex_room=None):
        self.base_service = base_service
        self.mirror = mirror
        self.qualification = qualification
        self.owner_runtime = None
        self.codex_room = codex_room
        self.claude_room = claude_room
        self.claude_index = claude_index
        self.claude = None
        self.messaging = messaging
        self._handoff = None
        self._permission = None
        selections = ()
        if qualification is not None:
            if type(qualification) is not _FictionalQualification:
                _unavailable()
            self._permission = _NativePermission(messaging, mirror, qualification)
            selections = qualification.selections(mirror.store)
        elif claude_room is not None:
            self._permission = _NativePermission(messaging, mirror, None, production=True)
        self._reads = NativeAgentReadService(mirror.store, selections=selections, _receipts=receipts)

    def _bind(self, runtime):
        self.owner_runtime = runtime
        if self.qualification is not None:
            q = self.qualification
            self._handoff = OfflineNativeHandoff(self.mirror.requests, runtime.ledger, self.mirror,
                                                 q.resources, q.materializer, q.binding, q.boundary, enabled=True)

    def bind_claude(self):
        if self.claude_room is None:
            return
        from attachments_claude_service import ClaudeAttachmentService
        from claude_attachment_adapter import ClaudeAttachmentAdapter
        service = ClaudeAttachmentService(self, self.messaging, self.claude_room, self._reads.receipts, self.claude_index)
        try:
            service.adapter = ClaudeAttachmentAdapter(self.mirror.requests, self.owner_runtime.ledger, self.mirror,
                    service.resources, service.materializer, owner_scope=self.claude_owner_scope,
                    reconcile=self.reconcile, allow_install=True, enabled=True, clock=self.mirror.store.clock)
            with self.messaging.lock:
                self.claude = service
                self._permission.runtime = self.owner_runtime
                self.messaging.mods.attachment_mount = (service.adapter, self.claude_activity)
                self.owner_runtime.native_checkpoint = self.claude_checkpoint
            if self.codex_room is not None:
                from codex_attachment_dispatch import CodexAttachmentDispatch
                try:
                    service.codex = CodexAttachmentDispatch(self, service.resources, service.materializer)
                except (AttachmentError, OSError, sqlite3.Error):
                    self.messaging._native_composition_error = 'codex_attachment_setup_unavailable'
        except BaseException:
            service.resources.close()
            raise

    def claude_checkpoint(self):
        from claude_attachment_adapter import LedgerError
        try:
            with self.claude_activity():
                self.claude.adapter.reconcile_pending()
        except (AttachmentError, LedgerError):
            pass

    @contextmanager
    def claude_activity(self):
        """Pin the shared lifetime without granting any write authority."""
        if self.claude is None:
            _unavailable()
        with self.base_service.operation(), self._reads.operation():
            yield

    @contextmanager
    def claude_http_operation(self):
        with self.claude_activity():
            self._verify_schema()
            yield

    @contextmanager
    def claude_owner_scope(self, action, request_id, identity):
        with self._operation(action, request_id) as scope:
            if action == 'handoff':
                if identity != ():
                    _unavailable()
                with self.mirror.store._gate():
                    row = self.claude._row(request_id)
                    scope.claude_native = row['catalogue_destination'][2:]
                yield
                return
            expected = self._permission.expected(scope)
            admission = expected.admission
            if (action not in ('claude_claim', 'claude_terminal', 'claude_terminal_conflict')
                    or identity[:2] != (admission.owner_request_id, expected.digest)):
                _unavailable()
            scope.claude = identity
            scope.claude_native = admission.agent_id[2:]
            if action == 'claude_claim':
                owner = self.owner_runtime.ledger
                with self._permission.admission_fence(owner, admission.owner_request_id):
                    yield
            else:
                yield

    @contextmanager
    def native_read(self, destination_id, scope_id):
        with self.base_service.operation(), self._reads.operation():
            self._verify_schema()
            self._reads._scope(destination_id, scope_id)
            yield self._reads

    def _verify_schema(self):
        with self.mirror.store._gate():
            if not _layouts(self.mirror.store)[0]:
                _unavailable()
            self._reads.receipts.require(self.mirror.store)

    @contextmanager
    def _operation(self, action, request_id):
        if self._permission is None or self._handoff is None and self.claude is None:
            _unavailable()
        with self.base_service.operation(), self._permission.operation(action, request_id) as scope:
            self._verify_schema()
            yield scope

    def handoff(self, request_id, saved_copies):
        if self._handoff is None:
            _unavailable()
        with self._operation('handoff', request_id):
            return self._handoff.handoff(request_id, saved_copies)

    def record_evidence(self, request_id, attempt_id, kind, **evidence):
        if self._handoff is None:
            _unavailable()
        with self._operation('evidence', request_id) as scope:
            expected = self._permission.expected(scope)
            owner_id = expected.admission.owner_request_id
            scope.evidence = owner_id, expected.digest, attempt_id
            return self.owner_runtime.ledger.record_outcome(owner_id, expected.digest, attempt_id, kind, **evidence)

    def reconcile(self, request_id):
        with self._operation('reconcile', request_id) as scope:
            expected = self._permission.expected(scope)
            owner = self.owner_runtime.ledger

            def acknowledge(*identity):
                if identity[:2] != (expected.admission.owner_request_id, expected.digest):
                    _unavailable()
                scope.ack = identity
                try:
                    return owner.acknowledge(*identity)
                finally:
                    scope.ack = None

            return self.mirror.reconcile(request_id, owner, _acknowledge=acknowledge)

    def stop(self, timeout=35):
        active = self._handoff if self._handoff is not None else self.claude.adapter if self.claude is not None else None
        if active is not None:
            self._permission.stop_entry(active)
        if self.claude is not None:
            if self.claude.codex is not None:
                self.claude.codex.stop()
            self.owner_runtime.native_checkpoint = None
        with self._reads.condition:
            self._reads.accepting = False
        if active is not None:
            self._permission.drain(timeout)
        self._reads.close(timeout)
        if self._handoff is not None:
            self.qualification.resources.close()
        if self.claude is not None:
            self.claude.resources.close()


def bind_native_composition(messaging, base_service, *, _qualification=None, claude_room=None, codex_room=None, scheduler=None):
    """Opens optional schemas before the single guarded owner runtime starts."""
    settings = {} if scheduler is None else {'scheduler': scheduler}
    with messaging._native_composition_lock:
        with messaging._native_owner_lock:
            if messaging._native_owner_stopped:
                return None
            existing = messaging._native_composition
            if existing is not None:
                if (existing.base_service is not base_service or existing.qualification is not _qualification
                        or existing.claude_room is not claude_room or existing.codex_room is not codex_room):
                    _unavailable()
                return existing.owner_runtime
        composition = None
        legacy_managed_storage = None
        try:
            _same_root(messaging, base_service.store)
            if claude_room is not None:
                namespace = NativeNamespaceFence(base_service.store.files.parent)

                def legacy_managed_storage():
                    with messaging.lock:
                        return namespace.legacy_managed_storage(messaging.db)

                # Managed chats may first be created after this startup declined
                # files. Re-read their state before offering upgrade advice.
                base_service._native_legacy_managed_storage = legacy_managed_storage
                if legacy_managed_storage():
                    messaging._native_composition_error = 'legacy_managed_storage'
                    messaging._native_owner_error = 'legacy_managed_storage'
                    return None
            with messaging.lock:
                if not sqlite_marker_available(messaging.db):
                    # Retained protection is never reclassified as fresh storage.
                    if messaging._attachment_guard is not None:
                        messaging._attachment_guard.require_schema()
                    namespace = NativeNamespaceFence(base_service.store.files.parent)
                    if namespace.detected(owner_db=messaging.db):
                        namespace.validate_mount(messaging.db)
                    messaging._native_composition_error = 'sqlite_unavailable'
                    messaging._native_owner_error = 'sqlite_unavailable'
                    base_service.native_sqlite_available = False
                    return None
            claude_index = None
            if claude_room is not None:
                if not callable(claude_room) or _qualification is not None:
                    _unavailable()
                namespace = NativeNamespaceFence(base_service.store.files.parent)
                with messaging.lock:
                    if not namespace.detected(owner_db=messaging.db):
                        messaging._attachment_guard = namespace.initialize_owner(messaging.db, messaging.lock)
                from attachments_claude_service import ClaudePrepareIndex
                claude_index = ClaudePrepareIndex(base_service.store)
                if codex_room is not None:
                    if not callable(codex_room):
                        _unavailable()
                    from codex_attachment_dispatch import CodexAttachmentDispatch
                    try:
                        CodexAttachmentDispatch.install(base_service.store)
                    except AttachmentError:
                        # A damaged optional Codex evidence table refuses Codex
                        # without changing Claude readiness or receipt layout.
                        codex_room = None
                        messaging._native_composition_error = 'codex_attachment_setup_unavailable'
            mirror, receipts = _open_schema(base_service.store, allow_install=True)
            composition = NativeAttachmentComposition(messaging, base_service, mirror, receipts, _qualification,
                                                      claude_room=claude_room, claude_index=claude_index, codex_room=codex_room)
        except (AttachmentError, NamespaceError):
            messaging._native_composition_error = 'setup_unavailable'
            # A legacy chat may have won the race with fresh initialization.
            # Do not install a files-only SQL guard into that ordinary registry.
            # Failed or partial native installations retain the old path below.
            if legacy_managed_storage is not None:
                try:
                    if legacy_managed_storage():
                        messaging._native_composition_error = 'legacy_managed_storage'
                        messaging._native_owner_error = 'legacy_managed_storage'
                        return None
                except NamespaceError:
                    pass
        permission = composition._permission if composition is not None else None
        runtime = bind_owner_runtime(messaging, allow_install=True, _native_permission=permission, **settings)
        if composition is not None and runtime is not None:
            composition._bind(runtime)
            if claude_room is not None:
                from claude_attachment_adapter import LedgerError
                try:
                    composition.bind_claude()
                except (AttachmentError, LedgerError, NamespaceError, OSError):
                    messaging._native_composition_error = 'setup_unavailable'
            messaging._native_composition = composition
        return runtime
