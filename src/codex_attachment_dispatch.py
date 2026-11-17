"""Completion-only attachment handoff to an exact local Codex Desktop chat."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import threading

from attachments_materialize import WorkspaceContext
from attachments_native_handoff import _NativeCopyProof
from attachments_requests import TransportProfile
from attachments_native_mirror import _conflict, target_binding_digest
from attachments_types import canonical, fail, now
import codex_attachment_live_evidence as evidence
import codex_new_chat
import codex_model_settings

PROFILE = 'codex-completion-only'
FLOORS = '''CREATE TABLE codex_native_attachment_floors (
    request_id TEXT PRIMARY KEY NOT NULL REFERENCES attachment_requests(id) ON DELETE RESTRICT,
    record TEXT NOT NULL CHECK(length(record)<=12000)
)'''


@dataclass(frozen=True)
class CodexAttachmentBinding:
    destination_record: str
    profile: TransportProfile
    context: WorkspaceContext

    @property
    def digest(self):
        return target_binding_digest(json.loads(self.destination_record), asdict(self.profile))


@dataclass(frozen=True)
class Target:
    thread_id: str
    project: str
    device: int
    inode: int


class DesktopQueue:
    def __init__(self, sessions):
        self.sessions = Path(sessions)
        self.root = self.sessions.parent
        self._verified = None
        self._lock = threading.Lock()

    def ready(self):
        try:
            if self.sessions != Path.home().resolve() / '.codex' / 'sessions':
                return False
            with self._lock:
                def stamps():
                    return tuple((s.st_dev, s.st_ino, s.st_size, s.st_ctime_ns, s.st_mtime_ns)
                                 for s in (p.lstat() for p in (
                                     codex_new_chat.APP / 'Contents/Info.plist',
                                     codex_new_chat.APP / 'Contents/Resources/app.asar',
                                     codex_new_chat.BINARY)))
                current = stamps()
                if current != self._verified:
                    self._verified = None
                    if not codex_new_chat._installed_build().available or current != stamps():
                        return False
                    self._verified = current
                return True
        except (OSError, ValueError):
            return False

    def selection(self, target):
        if not self.ready():
            return False
        try:
            self.prepare(target)
        except evidence.EvidenceUnavailable:
            return False
        def verified():
            if not self.ready():
                raise ValueError()

        observed = codex_model_settings._read_settings(target.thread_id, host_id='local', verify_build=verified)
        settings = observed.get('settings') or {}
        return (observed.get('state') == 'observed' and observed.get('nativeThreadId') == target.thread_id
                and observed.get('hostId') == 'local' and settings.get('model') == 'gpt-6-astra'
                and settings.get('mode') in ('default', None)
                and settings.get('modelProvider') in ('openai', None))

    def prepare(self, target):
        return evidence.prepare(str(self.root), target.thread_id, target.project, target.device, target.inode)

    def send_once(self, target, envelope):
        # The durable owner claim predates this call. The process result never
        # proves delivery and is not used as a reason to retry.
        args = [str(codex_new_chat.BINARY), 'queue', '--thread', target.thread_id, '--message', envelope]
        env = {'HOME': str(self.root.parent), 'CODEX_HOME': str(self.root),
               'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
        try:
            subprocess.run(args, cwd=target.project, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


class CodexAttachmentDispatch(_NativeCopyProof):
    def __init__(self, composition, resources, materializer):
        self.composition = composition
        self._mirror = composition.mirror
        self._requests = self._mirror.requests
        self._owner = composition.owner_runtime.ledger
        self._resources, self._materializer = resources, materializer
        self.queue = DesktopQueue(composition.messaging.sessions_dir)
        self.store = self._mirror.store
        self._lock = threading.Lock()
        self._stopped = False
        with self.store._gate():
            self._layout(self.store)

    @classmethod
    def install(cls, store):
        with store._gate(), store._transaction():
            row = store.db.execute(
                "SELECT sql FROM main.sqlite_schema WHERE name='codex_native_attachment_floors'").fetchone()
            if row is None:
                store.db.execute(FLOORS)
            cls._layout(store)

    @staticmethod
    def _layout(store):
        db = store.db
        row = db.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' "
                         "AND name='codex_native_attachment_floors'").fetchone()
        if (row is None or row[0] != FLOORS or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or db.execute("SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN "
                              "('codex_native_attachment_floors','attachment_requests') LIMIT 1").fetchone()):
            _conflict()

    def stop(self):
        with self._lock:
            self._stopped = True

    def _arm(self, expected, attempt, floor):
        a = expected.admission
        payload = {'owner': a.owner_request_id, 'admission': expected.digest,
                   'attempt': attempt, 'floor': floor.to_record()}
        record = self._floor_record(payload)
        with self.store._gate(), self.store._transaction():
            self._layout(self.store)
            if self._mirror._expected(a.attachment_request_id) != expected:
                _conflict()
            self.store.db.execute('INSERT INTO main.codex_native_attachment_floors VALUES (?,?)',
                                  (a.attachment_request_id, record))

    @staticmethod
    def _floor_record(payload):
        encoded = canonical(payload)
        return canonical({'payload': payload, 'digest': hashlib.sha256(encoded.encode()).hexdigest()})

    def offer(self, request_id, saved_copies, binding, *, enrollment):
        try:
            return self._offer(request_id, saved_copies, binding, enrollment)
        except evidence.EvidenceUnavailable:
            fail('destination_unavailable', 'The local Codex attachment destination could not be verified.', 503)

    def _offer(self, request_id, saved_copies, binding, enrollment):
        if type(binding) is not CodexAttachmentBinding or type(enrollment) is not Target:
            _conflict()
        target = enrollment
        a = json.loads(binding.destination_record)
        root = binding.context.root_identity
        if (a['id'], binding.context.root, root.device, root.inode) != (
                'x:' + target.thread_id, target.project, target.device, target.inode):
            _conflict()
        with self.composition._operation('handoff', request_id) as scope:
            keys = self._requests.resource_keys(request_id)
            with self._resources.hold(keys) as lease:
                work = self._work(request_id, lease, keys, binding, saved_copies)
                if work is None or not self.queue.selection(target):
                    return False
                floor = self.queue.prepare(target)
                verification = self._verify_files(work, saved_copies, lease)
                claim = self._requests.claim_handoff(request_id, work.authority.operation_revision,
                                                    work.authority.fingerprint, verification, lease)
                if claim.get('newClaim') is not True:
                    return False
                expected = self._claim_identity(request_id, claim, binding)
                admission = expected.admission
                if (admission.profile, admission.agent_id) != (PROFILE, a['id']):
                    _conflict()
                timestamp = int(now(self.store.clock) * 1000)
                admitted = self._owner.admit(admission, now_ms=timestamp)
                if admitted.get('new_admission') is not True:
                    return False
                self._owner_result(expected, admitted, claimed=False)
                claimed = self._owner.record_claim(admission.owner_request_id, expected.digest, now_ms=timestamp)
                if claimed.get('new_claim') is not True:
                    return False
                attempt = self._owner_result(expected, claimed, claimed=True)
                self._arm(expected, attempt, floor)
                if not self.queue.selection(target):
                    return False
                latest = self._work(request_id, lease, keys, binding, saved_copies, expected)
                verification = self._verify_files(latest, saved_copies, lease)
                evidence.revalidate(str(self.queue.root), floor)
                with self.store._gate():
                    self._check_work(request_id, lease, keys, binding, saved_copies, expected, latest, verification)
                if not self.queue.ready():
                    return False
                # Consume the one-shot, durable claim under the same lifecycle
                # lock stop uses. A call winning here may finish after stop.
                with self.composition._permission.condition, self._lock:
                    self.composition._permission._current(scope)
                    if self._stopped:
                        return False
                self.queue.send_once(target, admission.envelope)
        return True

    def observe(self, request_id):
        expected = self._mirror._expected(request_id)
        admission = expected.admission
        owner_id = admission.owner_request_id
        if (admission.transport, admission.profile) != ('codex', PROFILE):
            _conflict()
        with self.store._gate():
            self._layout(self.store)
            row = self.store.db.execute('SELECT record FROM main.codex_native_attachment_floors WHERE request_id=?',
                                        (request_id,)).fetchone()
        if row is None:
            return None
        stored = json.loads(row[0])
        if type(stored) is not dict or self._floor_record(stored.get('payload')) != row[0]:
            _conflict()
        raw = stored['payload']
        if (type(raw) is not dict or set(raw) != {'owner', 'admission', 'attempt', 'floor'}
                or raw['owner'] != owner_id or raw['admission'] != expected.digest):
            _conflict()
        link, outcome = expected.read_pair(self._owner._exact_snapshot(owner_id, expected.digest))
        attempt = raw['attempt']
        if link['claim'] != 'claimed' or link['attempt_id'] != attempt:
            _conflict()
        if not outcome['evidence_sequence']:
            if not self.queue.ready():
                return None
            try:
                floor = evidence.SourceFloor.from_record(raw['floor'])
            except evidence.EvidenceUnavailable:
                _conflict()
            if floor.thread_id != admission.agent_id[2:]:
                _conflict()
            result = evidence.observe(str(self.queue.root), floor, owner_id, admission.envelope_sha256)
            if result.state not in ('ended', 'reply'):
                return None
            with self.composition._operation('evidence', request_id) as scope:
                if self.composition._permission.expected(scope) != expected:
                    _conflict()
                scope.evidence = owner_id, expected.digest, attempt
                try:
                    evidence.verify_observation(str(self.queue.root), floor, result)
                except evidence.EvidenceUnavailable:
                    return None
                self._owner.record_outcome(owner_id, expected.digest, attempt, result.state,
                                           evidence_sequence=1, turn_id=result.turn_id, reply=result.reply or '',
                                           now_ms=int(now(self.store.clock) * 1000))
        self.composition.reconcile(request_id)
        with self._owner.lock:
            link, outcome, payload = self._owner._read(owner_id)
            if link['admission_digest'] != expected.digest or link['attempt_id'] != attempt:
                _conflict()
            reply = payload['mutable']['reply'] if payload is not None and link['reply_available'] else None
        if self._mirror._expected(request_id) != expected:
            _conflict()
        return dict(link=link, outcome=outcome, reply=reply)
