"""Offline one-winner handoff and shared exact attachment-copy verification."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import sqlite3
import threading
from typing import Callable

from attachments_materialize import Materializer, WorkspaceContext, WorkspaceDirectories
from attachments_native_mirror import (NativeOutcomeMirror, _ExpectedAdmission, _conflict,
                                       _expected_admission, target_binding_digest)
from attachments_owner_transport import Admission, LedgerError, OwnerAttachmentLedger, canonical_uuid
from attachments_requests import RequestAuthority, TransportProfile, VerifiedCopy, file_identity
from attachments_resources import ResourceLease, ResourceLocks
from attachments_types import AttachmentError, canonical, destination, fail, now


@dataclass(frozen=True)
class FictionalAdapterBinding:
    """One immutable fixture target, qualification and workspace."""
    destination_record: str
    profile: TransportProfile
    context: WorkspaceContext

    def __post_init__(self):
        target = destination(json.loads(self.destination_record))
        profile, context = self.profile, self.context
        if (type(profile) is not TransportProfile or type(context) is not WorkspaceContext
                or self.destination_record != canonical(target) or target['kind'] != 'agent'
                or not profile.qualification_revision.startswith('fictional-')
                or profile.temporary_copy or profile.capability_revision != target['capabilityRevision']
                or (profile.context_fingerprint, profile.workspace_fingerprint)
                != (context.fingerprint, context.workspace_fingerprint)
                or not target['id'].startswith('x:' if profile.adapter == 'codex' else 'c:')):
            _conflict()
        canonical_uuid(target['id'][2:])

    @property
    def digest(self):
        return target_binding_digest(json.loads(self.destination_record), asdict(self.profile))


@dataclass(frozen=True)
class _Work:
    authority: RequestAuthority
    copies: tuple
    directories: WorkspaceDirectories
    binding: FictionalAdapterBinding


@dataclass(frozen=True)
class _InvocationPermit:
    coordinator: object
    nonce: object
    lease: ResourceLease
    keys: tuple
    expected: _ExpectedAdmission
    attempt_id: str
    binding: FictionalAdapterBinding
    boundary: Callable[[Admission, str], object] | None
    generation: int
    saved_copies: tuple
    codex_prepared: object = None


class _NativeCopyProof:
    """Shared ATTACHMENTS-side verification. Callers still own every claim."""

    @staticmethod
    def _matching_mirror(mirror, requests):
        return type(mirror) is NativeOutcomeMirror and mirror.requests is requests

    def _lease(self, lease, keys):
        if (type(lease) is not ResourceLease or lease._manager is not self._resources
                or lease._keys != frozenset(keys)):
            fail('resource_lease_required', 'The exact attachment resource lease is required.', 409)
        lease.require(keys)

    def _work(self, request_id, lease, keys, binding, saved_copies, expected=None):
        self._lease(lease, keys)
        requests, store = self._requests, self._requests.store
        with store._gate():
            row, outbox, header, raw = self._mirror._rows(request_id)
            _, prior, _ = self._mirror._state(row, outbox, header, raw)
            if expected is None and outbox['claim_id'].strip('0'):
                return None
            if (prior is not None or outbox['owner_sequence'] != 0
                    or row['destination'] != binding.destination_record
                    or row['profile'] != canonical(asdict(binding.profile))
                    or row['cleanup_generation'] or requests._keys(request_id) != keys):
                _conflict()
            if expected is None:
                if row['stage'] != 'materialized':
                    _conflict()
            elif row['stage'] != 'handoff_claimed' or _expected_admission(row, outbox) != expected:
                _conflict()
            authority = requests.authority(request_id)
            rows = requests._copies(request_id)
            if type(saved_copies) is not tuple or not 1 <= len(rows) == len(saved_copies) <= 5:
                _conflict()
            copies = []
            for copy, saved in zip(rows, saved_copies):
                if copy is None or copy['role'] != 'workspace' or copy['state'] != 'available' or not copy['charged'] or copy['released']:
                    _conflict()
                spec = requests._intent(copy)
                if (type(saved) is not VerifiedCopy or
                        (saved.copy_id, saved.lease_id, saved.copy_generation, saved.identity, saved.sha256, saved.bytes)
                        != (spec.id, spec.lease_id, spec.copy_generation,
                            file_identity(tuple(json.loads(copy['identity']))), spec.sha256, spec.bytes)):
                    _conflict()
                copies.append(spec)
            dirs = WorkspaceDirectories.from_record(requests.workspace_directories(request_id))
            return _Work(authority, tuple(copies), dirs, binding)

    def _verify_files(self, work, saved_copies, lease):
        return self._materializer.verify_handoff(work.binding.context, work.authority, work.copies,
                                                  saved_copies, lease, directories=work.directories)

    def _claim_identity(self, request_id, claim, binding):
        expected = self._mirror._expected(request_id)
        a = expected.admission
        if (claim.get('requestId'), claim.get('ownerRequestId'), claim.get('claimId'),
                claim.get('ownerEpoch'), claim.get('fingerprint'), claim.get('envelopeDigest'),
                claim.get('envelope'), target_binding_digest(claim['destination'], claim['profile'])) != (
                a.attachment_request_id, a.owner_request_id, a.handoff_claim_id, a.owner_epoch, a.fingerprint,
                a.envelope_sha256, a.envelope, a.target_binding_digest) or a.target_binding_digest != binding.digest:
            _conflict()
        return expected

    def _owner_result(self, expected, result, *, claimed):
        owner_id = expected.admission.owner_request_id
        link, outcome = expected.read_pair(self._owner._exact_snapshot(owner_id, expected.digest))
        if (result.get('owner_request_id') != owner_id or result.get('admission_digest') != expected.digest
                or result.get('link_revision') != link['revision'] or result.get('outcome') != outcome
                or result.get('claim') != link['claim'] or result.get('attempt_id') != link['attempt_id']
                or link['claim'] != ('claimed' if claimed else 'unclaimed')
                or outcome['seq'] != (2 if claimed else 1)):
            _conflict()
        return link['attempt_id']

    def _check_work(self, request_id, lease, keys, binding, saved_copies, expected, work, verification):
        if self._work(request_id, lease, keys, binding, saved_copies, expected) != work:
            _conflict()
        row = self._requests._authority(work.authority)
        if row['epoch'] != self._requests.store.owner_epoch:
            _conflict()
        self._requests._verification(row, verification, 'handoff', self._requests._copies(request_id))
        return row


class OfflineNativeHandoff(_NativeCopyProof):
    def __init__(self, requests, owner, mirror, resources, materializer, binding, boundary=None, *,
                 enabled=False, codex_adapter=None):
        self._lock = threading.Lock()
        self._enabled = enabled is True
        self._generation = 0
        self._permits = {}
        self._codex = None
        if not self._enabled:
            return
        if codex_adapter is None:
            valid_boundary = callable(boundary)
        else:
            from codex_attachment_adapter import OfflineCodexAttachmentAdapter

            valid_boundary = boundary is None and type(codex_adapter) is OfflineCodexAttachmentAdapter
        if (type(mirror) is not NativeOutcomeMirror or mirror.requests is not requests
                or type(owner) is not OwnerAttachmentLedger or type(resources) is not ResourceLocks
                or type(materializer) is not Materializer or type(binding) is not FictionalAdapterBinding
                or not valid_boundary or owner.db is mirror.db or owner.lock is mirror.store.lock):
            _conflict()
        self._requests, self._owner, self._mirror = requests, owner, mirror
        self._resources, self._materializer = resources, materializer
        self._binding, self._boundary = binding, boundary
        self._codex = codex_adapter
        if self._codex is not None:
            self._codex._attach(self, binding)

    def stop(self):
        """Prevent new invocation; a call that won consumption may be in flight."""
        with self._lock:
            self._enabled = False
            self._generation += 1
            self._permits.clear()

    def _current(self, generation, binding):
        return self._enabled and self._generation == generation and self._binding is binding

    def handoff(self, request_id, saved_copies):
        """Only positively returned commits in this call can reach the fixture boundary."""
        with self._lock:
            if not self._enabled:
                return False
            generation, binding = self._generation, self._binding
        if self._codex is not None:
            with self._codex._operation(self, dispatch=True):
                return self._handoff(request_id, saved_copies, generation, binding)
        return self._handoff(request_id, saved_copies, generation, binding)

    def _handoff(self, request_id, saved_copies, generation, binding):
        self._owner._require_native_writer()
        # Discover exact keys in a short ledger read, then release the store
        # lock before acquiring resources. Both writers follow the resource lease.
        keys = self._requests.resource_keys(request_id)
        with self._resources.hold(keys) as lease:
            work = self._work(request_id, lease, keys, binding, saved_copies)
            if work is None:
                return False
            verification = self._verify_files(work, saved_copies, lease)
            with self._lock:
                if not self._current(generation, binding):
                    return False
            claim = self._requests.claim_handoff(request_id, work.authority.operation_revision,
                                                 work.authority.fingerprint, verification, lease)
            if claim.get('newClaim') is not True:
                return False
            expected = self._claim_identity(request_id, claim, binding)
            a = expected.admission

            owner_id = a.owner_request_id
            now_ms = int(now(self._requests.store.clock) * 1000)
            admit = self._owner.admit(a, now_ms=now_ms)
            if admit.get('new_admission') is not True:
                return False
            self._owner_result(expected, admit, claimed=False)

            owner_claim = self._owner.record_claim(owner_id, expected.digest, now_ms=now_ms)
            if owner_claim.get('new_claim') is not True:
                return False
            attempt = self._owner_result(expected, owner_claim, claimed=True)
            prepared = None
            if self._codex is not None:
                with self._lock:
                    if not self._current(generation, binding):
                        return False
                prepared = self._codex._prepare(expected, attempt, generation)
            with self._lock:
                if not self._current(generation, binding):
                    return False
                permit = _InvocationPermit(self, object(), lease, keys, expected, attempt,
                                           binding, self._boundary, generation, saved_copies, prepared)
                self._permits[permit.nonce] = permit
            return self._invoke(permit)

    def _invoke(self, permit):
        if self._codex is not None:
            self._codex._on_thread()
        with self._lock:
            if permit.coordinator is not self or self._permits.get(permit.nonce) is not permit:
                return False
        try:
            request_id = permit.expected.admission.attachment_request_id
            work = self._work(request_id, permit.lease, permit.keys, permit.binding, permit.saved_copies,
                              permit.expected)
            verification = self._verify_files(work, permit.saved_copies, permit.lease)
            with self._requests.store._gate():
                row = self._check_verified(permit, work, verification)
                with self._lock:
                    if (self._permits.get(permit.nonce) is not permit
                            or not self._current(permit.generation, permit.binding)
                            or self._boundary is not permit.boundary):
                        return False
                    if row['epoch'] != self._requests.store.owner_epoch:
                        _conflict()
                    self._lease(permit.lease, permit.keys)
                    del self._permits[permit.nonce]  # linearizes with stop before the unlocked callback
            if self._codex is not None:
                prepared = permit.codex_prepared
                self._codex._arm(prepared)
                verification = self._verify_files(work, permit.saved_copies, permit.lease)
                self._codex._check_dispatch(prepared)
                with self._requests.store._gate():
                    self._check_verified(permit, work, verification)
                self._codex._send_once(prepared, permit.expected, permit.attempt_id, permit.generation)
                return True
            try:
                permit.boundary(permit.expected.admission, permit.attempt_id)
            except Exception:
                raise AttachmentError('native_boundary_uncertain',
                                      'The fictional native boundary did not confirm return.', 503) from None
            return True
        finally:
            with self._lock:
                self._permits.pop(permit.nonce, None)

    def _check_verified(self, permit, work, verification):
        return self._check_work(permit.expected.admission.attachment_request_id, permit.lease, permit.keys,
                                permit.binding, permit.saved_copies, permit.expected, work, verification)

    def observe_codex(self, request_id):
        with self._lock:
            if not self._enabled or self._codex is None:
                return 'unavailable'
            generation, binding = self._generation, self._binding

        from codex_attachment_adapter import EvidenceUnavailable

        def publish(expected, attempt, candidate, reply):
            with self._lock:
                if not self._current(generation, binding):
                    return False
                try:
                    recorded = self._owner.record_outcome(expected.admission.owner_request_id, expected.digest,
                        attempt, candidate.kind, evidence_sequence=candidate.ordinal, turn_id=candidate.turn_id,
                        reply=reply, now_ms=int(now(self._requests.store.clock) * 1000))
                    return recorded.get('evidence') in ('recorded', 'duplicate')
                finally:
                    reply = None

        def current():
            with self._lock:
                return self._current(generation, binding)

        try:
            with self._codex._operation(self):
                self._owner._require_native_writer()
                return self._codex._observe(request_id, self._owner, self._mirror, publish, current)
        except (AttachmentError, EvidenceUnavailable, LedgerError, OSError, sqlite3.Error):
            return 'unavailable'
