"""Offline Codex attachment adapter with one injected fictional queue boundary."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from itertools import combinations
import json
import os
import threading

from attachments_materialize import DirectoryIdentity, WorkspaceContext
from attachments_owner_transport import digest
from attachments_requests import TransportProfile
from attachments_types import AttachmentError, canonical, destination
from codex_attachment_evidence import (AttemptPin, EvidenceMap, EvidenceUnavailable, PathPin,
                                       _match_pin, _open_directory, _path, _require, _uuid)


def _inside(path, root):
    return path.startswith(root + os.sep)


@dataclass(frozen=True, repr=False)
class FictionalCodexSource:
    sandbox: PathPin
    native_root: PathPin
    sessions: PathPin
    index: PathPin
    rollout: PathPin
    project: PathPin
    map_path: str
    create: bool
    installation_id: str | None = None

    def __post_init__(self):
        pins = (self.sandbox, self.native_root, self.sessions, self.index, self.rollout, self.project)
        _require(all(type(pin) is PathPin and pin.uid == os.getuid() for pin in pins)
                 and _path(self.map_path) and type(self.create) is bool
                 and (self.installation_id is None if self.create else _uuid(self.installation_id)),
                 'fixture_source_invalid')
        root = self.sandbox.path
        native, project, mapped = self.native_root.path, self.project.path, os.path.dirname(self.map_path)
        _require(all(_inside(pin.path, root) for pin in pins[1:]) and _inside(mapped, root)
                 and self.sessions.path == os.path.join(native, 'sessions')
                 and self.index.path == os.path.join(native, 'state_5.sqlite')
                 and _inside(self.rollout.path, self.sessions.path)
                 and all(a != b and not _inside(a, b) and not _inside(b, a)
                         for a, b in combinations((native, project, mapped), 2)),
                 'fixture_source_invalid')

    def _check_sandbox(self):
        try:
            with _open_directory(self.sandbox.path) as fd:
                st = os.fstat(fd)
                _match_pin(self.sandbox, st)
                _require(st.st_mode & 0o077 == 0, 'fixture_source_invalid')
        except OSError:
            raise EvidenceUnavailable('fixture_source_unavailable') from None


@dataclass(frozen=True)
class FictionalQueueCommand:
    thread_id: str
    envelope: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class _Prepared:
    seed: object
    generation: int


class OfflineCodexAttachmentAdapter:
    def __init__(self, destination_record, profile, context, source, boundary, *, enabled=False):
        self._thread = threading.current_thread()
        self._map = self._coordinator = self._prepared = self._armed = None
        self._active = False
        if enabled is not True:
            return
        try:
            target = destination(json.loads(destination_record))
        except (AttachmentError, ValueError, TypeError):
            raise EvidenceUnavailable('fixture_target_invalid') from None
        agent = target['id']
        _require(type(source) is FictionalCodexSource and type(profile) is TransportProfile
                 and type(context) is WorkspaceContext and type(context.root_identity) is DirectoryIdentity
                 and callable(boundary)
                 and target['kind'] == 'agent' and destination_record == canonical(target)
                 and agent.startswith('x:') and _uuid(agent[2:])
                 and profile.adapter == 'codex' and profile.qualification_revision.startswith('fictional-')
                 and not profile.temporary_copy and profile.capability_revision == target['capabilityRevision']
                 and profile.context_fingerprint == context.fingerprint
                 and profile.workspace_fingerprint == context.workspace_fingerprint
                 and (source.project.path, source.project.device, source.project.inode)
                 == (context.root, context.root_identity.device, context.root_identity.inode),
                 'fixture_target_invalid')
        source._check_sandbox()
        self._binding = (destination_record, profile, context)
        self._source, self._agent, self._boundary = source, agent, boundary
        self._map = EvidenceMap(source.map_path, source.installation_id, create=source.create)

    @property
    def installation_id(self):
        self._on_thread()
        return self._map.installation_id if self._map is not None else None

    def _on_thread(self):
        _require(threading.current_thread() is self._thread, 'fixture_actor_required')

    def close(self):
        self._on_thread()
        _require(not self._active, 'fixture_adapter_busy')
        if self._map is not None:
            self._map.close()
            self._map = None

    def _attach(self, coordinator, binding):
        self._on_thread()
        _require(self._map is not None and self._coordinator is None
                 and self._binding == (binding.destination_record, binding.profile, binding.context),
                 'fixture_target_invalid')
        self._coordinator = coordinator
        self._binding_digest = binding.digest

    @contextmanager
    def _operation(self, coordinator, *, dispatch=False):
        self._on_thread()
        _require(self._map is not None and self._coordinator is coordinator, 'fixture_adapter_unavailable')
        _require(not self._active, 'fixture_adapter_busy')
        _require(not dispatch or self._map.creating, 'fixture_recovery_only')
        self._active = True
        try:
            self._source._check_sandbox()
            yield
        finally:
            self._prepared = self._armed = None
            self._active = False

    def _pin(self, expected, attempt):
        admission = expected.admission
        profile = self._binding[1]
        _require((admission.transport, admission.agent_id, admission.target_binding_digest,
                  admission.profile, admission.profile_revision)
                 == ('codex', self._agent, self._binding_digest, profile.id, profile.revision),
                 'fixture_target_invalid')
        source = self._source
        return AttemptPin(attempt, admission.owner_request_id, self._agent[2:], expected.digest,
                          self._binding_digest, admission.envelope_sha256, source.native_root, source.sessions,
                          source.index, source.rollout, source.project)

    def _prepare(self, expected, attempt, generation):
        _require(self._active and self._prepared is None and self._armed is None, 'fixture_dispatch_conflict')
        pin = self._pin(expected, attempt)
        admission = expected.admission
        _require(admission.envelope.startswith('WATCH_REQUEST ' + pin.owner_request_id + '\nAttachment relay version: 3\n'),
                 'fixture_envelope_invalid')
        prepared = _Prepared(self._map._seed(pin), generation)
        self._prepared = prepared
        return prepared

    def _arm(self, prepared):
        _require(self._active and self._prepared is prepared and prepared is not None, 'fixture_dispatch_conflict')
        self._prepared = None
        self._map._arm(prepared.seed)
        self._armed = prepared

    def _check_dispatch(self, prepared):
        _require(self._active and self._armed is prepared and prepared is not None, 'fixture_dispatch_conflict')
        self._source._check_sandbox()
        self._map._check_floor(prepared.seed, armed=True)

    def _send_once(self, prepared, expected, attempt, generation):
        _require(self._active and self._armed is prepared and prepared is not None
                 and prepared.generation == generation and prepared.seed.pin == self._pin(expected, attempt),
                 'fixture_dispatch_conflict')
        self._armed = None
        command = FictionalQueueCommand(prepared.seed.pin.thread_id, expected.admission.envelope)
        try:
            confirmed = self._boundary(command) is None
        except Exception:
            confirmed = False
        finally:
            command = None
        if not confirmed:
            raise AttachmentError('native_boundary_uncertain',
                                  'The fictional native boundary did not confirm return.', 503)

    def _observe(self, request_id, owner, mirror, publish, current):
        expected = mirror._expected(request_id)
        owner_id = expected.admission.owner_request_id
        link, outcome = expected.read_pair(owner._exact_snapshot(owner_id, expected.digest))
        _require(link['claim'] == 'claimed', 'fixture_attempt_unavailable')
        pin = self._pin(expected, link['attempt_id'])
        frontier = outcome['evidence_sequence']
        if frontier == 1:
            retained = self._map._retained_armed_emission(pin)
            self._check_committed(pin, retained, outcome)
            if not current():
                return 'unavailable'
        else:
            _require(frontier == 0 and outcome['state'] == 'delivery_pending', 'fixture_evidence_conflict')
            verified = self._map._scan_armed(pin)
            candidate = verified.observation.candidate
            if candidate is None:
                return verified.observation.status
            try:
                reply_hash = hashlib.sha256(verified.reply.encode('utf-8')).hexdigest() if verified.reply else None
                _require(reply_hash == candidate.reply_sha256 and bool(verified.reply) == (candidate.kind == 'reply'),
                         'fixture_reply_conflict')
                _require(mirror._expected(request_id) == expected, 'fixture_target_invalid')
                self._source._check_sandbox()
                self._map._recheck_armed(pin, verified)
                if not publish(expected, pin.attempt_id, candidate, verified.reply):
                    return 'unavailable'
            finally:
                verified = None
        applied, acknowledgment = mirror.reconcile(request_id, owner)
        return 'reconciled' if applied != 'stale' and acknowledgment == 'acknowledged' else 'unavailable'

    @staticmethod
    def _check_committed(pin, retained, outcome):
        fingerprint = digest('watch-owner-attachment-evidence-v1', dict(
            admission_digest=pin.admission_digest, attempt_id=pin.attempt_id, sequence=retained.ordinal,
            kind=retained.kind, turn_id=retained.turn_id, reply_fingerprint=retained.reply_sha256 or ''))
        provenance = 'correlated_reply' if retained.kind == 'reply' else 'codex_turn_ended'
        _require(outcome['evidence_fingerprint'] == fingerprint and outcome['turn_id'] == retained.turn_id
                 and outcome['accepted'] and outcome['execution_ended'] and not outcome['started']
                 and not outcome['not_submitted']
                 and outcome['accepted_provenance'] == outcome['terminal_provenance'] == provenance
                 and outcome['reply_captured'] == (retained.kind == 'reply')
                 and outcome['reply_fingerprint'] == (retained.reply_sha256 or ''),
                 'fixture_evidence_conflict')
