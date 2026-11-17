"""Protected, completion-only Claude mod claims under the native attachment gates."""
from dataclasses import asdict, dataclass
import json
import math
import os
import sqlite3
import threading
import time
import uuid

from attachments_materialize import Materializer, WorkspaceContext
from attachments_native_handoff import _NativeCopyProof, target_binding_digest
from attachments_owner_transport import (CLAUDE_CLAIMS, CLAUDE_CLAIM_BYTES, CLAUDE_COMPLETION_PROFILE, LINK_BYTES, MAX_LINKS,
    LedgerError, OwnerAttachmentLedger, canonical_uuid, decode, digest, hex_digest,
    integer, keys, padded, text)
from attachments_requests import TransportProfile
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError, canonical, destination, fail

VERSION = 'owner_claude_attachment_version'
BUSY_INDEX = 'owner_claude_attachment_busy'
DDL = {
    VERSION: f'CREATE TABLE {VERSION} (slot INTEGER PRIMARY KEY CHECK(slot=1), version INTEGER NOT NULL CHECK(version=1))',
    CLAUDE_CLAIMS: f'''CREATE TABLE {CLAUDE_CLAIMS} (
        owner_request_id TEXT PRIMARY KEY REFERENCES owner_attachment_links(owner_request_id) ON DELETE RESTRICT,
        agent_id TEXT NOT NULL, busy INTEGER NOT NULL CHECK(busy IN (0,1)),
        record BLOB NOT NULL CHECK(typeof(record)='blob' AND length(record)={CLAUDE_CLAIM_BYTES}))''',
    BUSY_INDEX: f'CREATE UNIQUE INDEX {BUSY_INDEX} ON {CLAUDE_CLAIMS}(agent_id) WHERE busy=1',
}
CLAIM_KEYS = {'version', 'identity', 'owner_request_id', 'admission_digest', 'attempt_id', 'agent_id', 'native_id',
              'enrollment_id', 'grant_id', 'project_digest', 'workspace_digest', 'context_digest', 'generation',
              'client_id', 'lifecycle_protocol', 'expires_ms', 'busy', 'first', 'collision', 'terminal_digest',
              'conflict_digest', 'checksum'}
WITNESS_KEYS = {'event_id', 'fingerprint', 'turn_digest', 'outcome', 'occurred_at'}
OUTCOMES = ('answered', 'unusable', 'aborted', 'withheld')
MAX_SAFE = 2**53 - 1
CLAIM_TTL, SESSION_LEASE = 900, 12


def _unavailable():
    fail('claude_attachment_unavailable', 'Claude attachment authority could not be verified.', 503)


def _denied():
    fail('claude_attachment_conflict', 'This Claude attachment action could not be matched.', 409)


def _names(db, names):
    bind = ','.join('?' for _ in names)
    return dict(db.execute(f'SELECT name,sql FROM main.sqlite_schema WHERE name COLLATE NOCASE IN ({bind})', names))


def _layout(db):
    names = (*DDL, 'owner_attachment_links', 'owner_attachment_outcomes', 'requests',
             'claude_mod_enrollments', 'claude_mod_grants')
    bind = ','.join('?' for _ in names)
    if db.execute(f'''SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN ({bind})
          OR (type='trigger' AND tbl_name COLLATE NOCASE IN ({bind})) LIMIT 1''', (*names, *names)).fetchone():
        _unavailable()
    installed = _names(db, tuple(DDL))
    if not installed:
        return False
    if installed != DDL or db.execute(f'SELECT slot,version FROM main.{VERSION} LIMIT 2').fetchall() != [(1, 1)]:
        _unavailable()
    if db.execute('''SELECT 1 FROM main.sqlite_schema WHERE tbl_name COLLATE NOCASE IN (?,?)
                 AND sql IS NOT NULL AND name NOT IN (?,?,?) LIMIT 1''', (VERSION, CLAUDE_CLAIMS, *DDL)).fetchone():
        _unavailable()
    return True


def _profile_link(link):
    identity = link['identity']
    return identity['transport'] == 'claude' and identity['profile'] == CLAUDE_COMPLETION_PROFILE


def _pair(db, owner_id):
    row = db.execute('''SELECT l.record,o.record FROM main.owner_attachment_links l
        LEFT JOIN main.owner_attachment_outcomes o USING(owner_request_id) WHERE l.owner_request_id=?''',
        (owner_id,)).fetchone()
    if row is None or row[1] is None:
        _unavailable()
    return OwnerAttachmentLedger.decode_pair(owner_id, *row)


def _profile_ids(db, agent=None):
    if not _names(db, ('owner_attachment_links',)):
        return ()
    found = []
    rows = db.execute('SELECT owner_request_id,record FROM main.owner_attachment_links LIMIT ?', (MAX_LINKS + 1,))
    for n, (owner_id, raw) in enumerate(rows):
        if n == MAX_LINKS:
            _unavailable()
        # The link's checked immutable classifier must survive missing adjunct
        # artifacts and JSON spelling differences. Unreadable links cannot
        # supply a negative occupancy decision for a different target.
        link = decode(raw, LINK_BYTES)
        identity = link.get('identity') if type(link) is dict else None
        if (type(identity) is not dict or identity.get('owner_request_id') != canonical_uuid(owner_id)
                or type(identity.get('profile')) is not str or type(identity.get('agent_id')) is not str
                or link.get('admission_digest') != digest('watch-owner-attachment-admission-v1', identity)
                or link.get('integrity') != digest('watch-owner-attachment-link-v1',
                                                   {k: v for k, v in link.items() if k != 'integrity'})):
            _unavailable()
        if identity['profile'] == CLAUDE_COMPLETION_PROFILE and (agent is None or identity['agent_id'] == agent):
            found.append(owner_id)
    return tuple(found)


def _action_digest(kind, record, fingerprints):
    return digest('watch-claude-attachment-' + kind + '-v1', {
        'admission_digest': record['admission_digest'], 'attempt_id': record['attempt_id'],
        'fingerprints': sorted(fingerprints),
    })


def _encoded(record):
    value = {k: v for k, v in record.items() if k != 'checksum'}
    return padded(dict(value, checksum=digest('watch-claude-attachment-claim-v1', value)), CLAUDE_CLAIM_BYTES)


def _witness(value):
    keys(value, WITNESS_KEYS)
    canonical_uuid(value['event_id'])
    hex_digest(value['fingerprint'])
    hex_digest(value['turn_digest'])
    if value['outcome'] not in OUTCOMES or not 0 <= integer(value['occurred_at']) <= MAX_SAFE:
        _denied()


def _slot(link, outcome, row):
    if row is None or link['claim'] != 'claimed' or not _profile_link(link):
        _unavailable()
    agent, busy, raw = row
    value = OwnerAttachmentLedger._claude_slot(raw, link, link['attempt_id'])
    keys(value, CLAIM_KEYS)
    for k in ('native_id', 'enrollment_id', 'grant_id', 'generation', 'client_id'):
        canonical_uuid(value[k])
    for k in ('project_digest', 'workspace_digest', 'context_digest'):
        hex_digest(value[k])
    first, collision = value['first'], value['collision']
    if (agent != value['agent_id'] or agent != 'c:' + value['native_id'] or busy != int(value['busy'])
            or type(value['lifecycle_protocol']) is not int or value['lifecycle_protocol'] != 2
            or integer(value['expires_ms'], 1) <= link['created_ms']
            or outcome['started'] or outcome['not_submitted'] or outcome['evidence_sequence']
            or outcome['evidence_fingerprint'] or outcome['late_reply_observed']):
        _unavailable()
    if first is None:
        if (not value['busy'] or collision is not None or value['terminal_digest'] or value['conflict_digest']
                or outcome['seq'] != 2 or outcome['state'] != 'delivery_pending' or link['turn_id']
                or outcome['accepted'] or outcome['execution_ended'] or outcome['conflict']
                or outcome['reply_captured'] or link['reply_available']):
            _unavailable()
        return value
    _witness(first)
    if (value['busy'] or not outcome['accepted'] or not outcome['execution_ended'] or not link['turn_id']
            or outcome['accepted_provenance'] != 'claude_turn_ended'
            or outcome['terminal_provenance'] != 'claude_turn_ended'
            or first['turn_digest'] != digest('watch-claude-attachment-turn-v1', link['turn_id'])
            or value['terminal_digest'] != _action_digest('terminal', value, (first['fingerprint'],))
            or (outcome['reply_captured'] and first['outcome'] != 'answered')):
        _unavailable()
    if collision is None:
        if (value['conflict_digest'] or outcome['seq'] != 3 or outcome['conflict']
                or outcome['capture_withheld'] != (first['outcome'] == 'withheld')
                or outcome['state'] != ('answered' if outcome['reply_captured'] else 'execution_ended')):
            _unavailable()
    else:
        _witness(collision)
        hashes = (first['fingerprint'], collision['fingerprint'])
        if (hashes[0] == hashes[1] or value['conflict_digest'] != _action_digest('conflict', value, hashes)
                or outcome['seq'] != 4 or not outcome['conflict'] or not outcome['capture_withheld']
                or link['reply_available'] or outcome['state'] != 'execution_ended' or outcome['reason'] != 'reply_conflict'):
            _unavailable()
    return value


def _get_slot(db, owner_id):
    return db.execute(f'SELECT agent_id,busy,record FROM main.{CLAUDE_CLAIMS} WHERE owner_request_id=?',
                      (owner_id,)).fetchone()


def protected_busy(db, agent):
    """Read-only target guard. The established owner lock must already be held."""
    if not isinstance(agent, str) or not agent.startswith('c:'):
        return False
    canonical_uuid(agent[2:])
    installed = _layout(db)
    if installed:
        indexed = db.execute(f'''SELECT owner_request_id,agent_id,busy,record
            FROM main.{CLAUDE_CLAIMS} INDEXED BY {BUSY_INDEX} WHERE agent_id=? AND busy=1 LIMIT 2''',
            (agent,)).fetchall()
        if indexed:
            if len(indexed) != 1:
                _unavailable()
            owner_id, *row = indexed[0]
            link, outcome = _pair(db, owner_id)
            if not _slot(link, outcome, row)['busy']:
                _unavailable()
            return True

    # A negative index result alone cannot rule out a lost or misrouted slot.
    for owner_id in _profile_ids(db, agent):
        link, outcome = _pair(db, owner_id)
        if not _profile_link(link) or link['identity']['agent_id'] != agent:
            _unavailable()
        if not installed:
            if link['claim'] == 'claimed':
                _unavailable()
            continue
        raw = _get_slot(db, owner_id)
        if link['claim'] == 'claimed':
            if _slot(link, outcome, raw)['busy']:
                _unavailable()
        elif raw is not None:
            _unavailable()
    return False


@dataclass(frozen=True)
class ClaudeAttachmentBinding:
    destination_record: str
    profile: TransportProfile
    context: WorkspaceContext

    def __post_init__(self):
        target = destination(json.loads(self.destination_record))
        profile, context = self.profile, self.context
        if (type(profile) is not TransportProfile or type(context) is not WorkspaceContext
                or target['kind'] != 'agent' or not target['id'].startswith('c:')
                or self.destination_record != canonical(target) or profile.id != CLAUDE_COMPLETION_PROFILE
                or profile.adapter != 'claude_official' or profile.temporary_copy or not profile.original_relay_formats
                or set(profile.media_types) != set(profile.original_relay_formats)
                or profile.capability_revision != target['capabilityRevision']
                or (profile.context_fingerprint, profile.workspace_fingerprint) !=
                   (context.fingerprint, context.workspace_fingerprint)
                or not os.path.isabs(context.root) or os.path.normpath(context.root) != context.root):
            _denied()
        canonical_uuid(target['id'][2:])

    @property
    def agent(self):
        return json.loads(self.destination_record)['id']

    @property
    def digest(self):
        return target_binding_digest(json.loads(self.destination_record), asdict(self.profile))


@dataclass(frozen=True)
class _Enrollment:
    id: str
    grant: str
    agent: str
    project: str
    generation: str
    client: str

    @classmethod
    def read(cls, entry, now=None):
        if (type(entry) is not dict or entry.get('active') != 1 or type(entry.get('lifecycle_protocol')) is not int
                or entry['lifecycle_protocol'] != 2 or not isinstance(entry.get('agent'), str)
                or not entry['agent'].startswith('c:') or not isinstance(entry.get('project'), str)):
            _denied()
        canonical_uuid(entry['agent'][2:])
        if now is not None:
            seen = entry.get('last_seen')
            if type(seen) not in (int, float) or not math.isfinite(seen) or not now - SESSION_LEASE < seen <= now:
                _denied()
        return cls(canonical_uuid(entry.get('id')), canonical_uuid(entry.get('grant_id')), entry['agent'], entry['project'],
                   canonical_uuid(entry.get('generation')), canonical_uuid(entry.get('client_id')))

    @property
    def project_digest(self):
        return digest('watch-claude-attachment-project-v1', self.project)

    def matches(self, record):
        return (self.id, self.grant, self.agent, self.project_digest) == (
            record['enrollment_id'], record['grant_id'], record['agent_id'], record['project_digest'])


@dataclass(frozen=True)
class _Offer:
    expected: object
    binding: ClaudeAttachmentBinding
    enrollment: _Enrollment
    saved: tuple
    expires_ms: int

    def matches(self, current):
        original = self.enrollment
        return (original.id, original.grant, original.agent, original.project) == (
            current.id, current.grant, current.agent, current.project)


class ClaudeAttachmentAdapter(_NativeCopyProof):
    def __init__(self, requests, owner, mirror, resources, materializer, *, owner_scope, reconcile=None,
                 allow_install=False, enabled=False, clock=time.time):
        self._lock = threading.Lock()
        self._events = threading.Lock()
        self._maintenance = threading.Lock()
        self._maintenance_cursor = ''
        self._offers = {}
        self._enabled = enabled is True
        self._mounted = False
        if not self._enabled:
            return
        if (type(owner) is not OwnerAttachmentLedger or not owner.maintenance_only
                or owner._native_permission is None or owner.guard is None
                or not self._matching_mirror(mirror, requests) or owner.db is mirror.db or owner.lock is mirror.store.lock
                or type(resources) is not ResourceLocks or resources._path != str(mirror.store.files.root)
                or type(materializer) is not Materializer or materializer._workspace_files is not mirror.store.files
                or not callable(owner_scope) or reconcile is not None and not callable(reconcile) or not callable(clock)):
            _unavailable()
        self._requests, self._owner, self._mirror = requests, owner, mirror
        self._resources, self._materializer, self._db = resources, materializer, owner.db
        self._scope, self._reconcile, self._clock = owner_scope, reconcile, clock
        with owner.lock:
            present = _layout(self._db)
            if not present:
                if allow_install is not True or _profile_ids(self._db):
                    _unavailable()
                with owner._transaction():
                    if _layout(self._db) or _profile_ids(self._db):
                        _unavailable()
                    for declaration in DDL.values():
                        self._db.execute(declaration)
                    self._db.execute(f'INSERT INTO main.{VERSION} VALUES (1,1)')
                    self._verify_mount()
            else:
                self._verify_mount()
        self._mounted = True

    def _verify_mount(self):
        self._require_layout()
        protected = set(_profile_ids(self._db))
        rows = self._db.execute(f'SELECT owner_request_id FROM main.{CLAUDE_CLAIMS} LIMIT ?',
                                (MAX_LINKS + 1,)).fetchall()
        claimed = {row[0] for row in rows}
        if len(rows) > MAX_LINKS or not claimed <= protected:
            _unavailable()
        for owner_id in protected:
            link, outcome, _ = self._owner._read(owner_id)
            if not _profile_link(link) or (link['claim'] == 'claimed') != (owner_id in claimed):
                _unavailable()
            if owner_id in claimed:
                _slot(link, outcome, _get_slot(self._db, owner_id))

    def _require_layout(self):
        if self._owner.db is not self._db or not _layout(self._db):
            _unavailable()
        self._owner.guard.require_unshadowed()
        self._owner.guard.require_schema()

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_SAFE // 1000:
            _unavailable()
        return value

    def stop(self):
        with self._lock:
            self._enabled = False
            self._offers.clear()

    def busy(self, agent):
        if not self._mounted:
            return False
        with self._owner.lock:
            return protected_busy(self._db, agent)

    def handles(self, owner_id):
        if not self._mounted:
            return False
        try:
            canonical_uuid(owner_id)
        except LedgerError:
            return False
        with self._owner.lock:
            if self._owner.db is not self._db:
                _unavailable()
            self._owner.guard.require_unshadowed()
            self._owner.guard.require_schema()
            row = self._db.execute('SELECT 1 FROM main.owner_attachment_links WHERE owner_request_id=?', (owner_id,)).fetchone()
            if row is not None and _profile_link(_pair(self._db, owner_id)[0]):
                return True  # Protected IDs remain protected if their adjunct is unavailable.
            claim_table = _names(self._db, (CLAUDE_CLAIMS,))
            if claim_table and (claim_table != {CLAUDE_CLAIMS: DDL[CLAUDE_CLAIMS]}
                                or _get_slot(self._db, owner_id) is not None):
                _unavailable()
            return False

    def _positive_admission(self, request_id, work, binding, proof, lease):
        claim = self._requests.claim_handoff(request_id, work.authority.operation_revision,
                                            work.authority.fingerprint, proof, lease)
        if claim.get('newClaim') is not True:
            return None
        return self._claim_identity(request_id, claim, binding)

    def offer(self, request_id, saved_copies, binding, *, enrollment):
        if not self._mounted:
            return False
        if type(binding) is not ClaudeAttachmentBinding:
            _denied()
        observed = _Enrollment.read(enrollment, self._now())
        if (observed.agent, observed.project) != (binding.agent, binding.context.root):
            _denied()
        reservation = object()
        target = binding.agent
        with self._owner.lock:
            self._require_layout()
            directory = self._owner.directory
            if not directory.ready or not directory.matches(directory.epoch()):
                _unavailable()
            if protected_busy(self._db, target):
                return False
        with self._lock:
            previous = self._offers.get(target)
            if (not self._enabled or previous is not None and
                    (type(previous) is not _Offer or previous.expires_ms > int(self._now() * 1000))):
                return False
            self._offers[target] = reservation
        try:
            with self._scope('handoff', request_id, ()):
                self._owner._require_native_writer()
                resource_keys = self._requests.resource_keys(request_id)
                with self._resources.hold(resource_keys) as lease:
                    work = self._work(request_id, lease, resource_keys, binding, saved_copies)
                    if work is None:
                        return False
                    proof = self._verify_files(work, saved_copies, lease)
                    with self._requests.store._gate():
                        row = self._requests._authority(work.authority)
                        expires_ms = int(min(self._now() + CLAIM_TTL, row['created'] + CLAIM_TTL) * 1000)
                    if expires_ms <= int(self._now() * 1000):
                        return False
                    with self._lock:
                        if not self._enabled or self._offers.get(target) is not reservation:
                            return False
                    expected = self._positive_admission(request_id, work, binding, proof, lease)
                    if expected is None:
                        return False
                    admission = self._owner.admit(expected.admission, now_ms=int(self._now() * 1000))
                    if admission.get('new_admission') is not True:
                        return False
                    self._owner_result(expected, admission, claimed=False)
                with self._lock:
                    if not self._enabled or self._offers.get(target) is not reservation:
                        return False
                    self._offers[target] = _Offer(expected, binding, observed, saved_copies, expires_ms)
                    return True
        finally:
            with self._lock:
                if self._offers.get(target) is reservation:
                    del self._offers[target]

    def poll(self, entry, *, recheck):
        if not self._mounted or not callable(recheck):
            return None
        observed = _Enrollment.read(entry, self._now())
        with self._lock:
            offer = self._offers.get(observed.agent)
            if (not self._enabled or type(offer) is not _Offer or not offer.matches(observed)
                    or offer.expires_ms <= int(self._now() * 1000)):
                return None
        a = offer.expected.admission
        request_id = a.attachment_request_id
        identity = (a.owner_request_id, offer.expected.digest)
        keys_held = self._requests.resource_keys(request_id)
        with self._resources.hold(keys_held) as lease:
            work = self._work(request_id, lease, keys_held, offer.binding, offer.saved, offer.expected)
            proof = self._verify_files(work, offer.saved, lease)
            with self._requests.store._gate():
                self._check_work(request_id, lease, keys_held, offer.binding, offer.saved, offer.expected, work, proof)
            with self._scope('claude_claim', request_id, identity):
                check = self._owner._require_native_writer('claude_claim', *identity)
                consumed = False

                def claim_window():
                    check()
                    ts = self._now()
                    if not proof.checked_at <= ts <= proof.expires_at or offer.expires_ms <= int(ts * 1000):
                        _denied()
                    if consumed and _Enrollment.read(recheck(), ts) != observed:
                        _denied()

                with self._owner._transaction(native=claim_window):
                    self._require_layout()
                    current_entry = recheck()
                    if current_entry is None:
                        return None
                    ts = self._now()
                    current = _Enrollment.read(current_entry, ts)
                    if (current != observed or not offer.matches(current) or protected_busy(self._db, current.agent)
                            or not proof.checked_at <= ts <= proof.expires_at or offer.expires_ms <= int(ts * 1000)):
                        return None
                    with self._lock:
                        if not self._enabled or self._offers.get(current.agent) is not offer:
                            return None
                        del self._offers[current.agent]
                        consumed = True
                    attempt = str(uuid.uuid4())
                    bound_identity, _ = a.freeze()
                    receipt = dict(version=1, identity=bound_identity, owner_request_id=a.owner_request_id,
                        admission_digest=offer.expected.digest, attempt_id=attempt, agent_id=current.agent,
                        native_id=current.agent[2:], enrollment_id=current.id, grant_id=current.grant,
                        project_digest=current.project_digest, workspace_digest=offer.binding.context.workspace_fingerprint,
                        context_digest=offer.binding.context.fingerprint, generation=current.generation, client_id=current.client,
                        lifecycle_protocol=2, expires_ms=offer.expires_ms, busy=True, first=None, collision=None,
                        terminal_digest='', conflict_digest='')
                    self._owner._claim_claude_in_transaction(*identity, attempt, _encoded(receipt), now_ms=int(ts * 1000))
                    _, _, _, retained, _ = self._linked(a.owner_request_id, offer.expected)
                    if {k: v for k, v in retained.items() if k != 'checksum'} != receipt:
                        _unavailable()
                return dict(request_id=a.owner_request_id, attempt_id=attempt, native_id=current.agent[2:],
                            generation=current.generation, envelope=a.envelope,
                            expires_at=offer.expires_ms / 1000, lifecycle_protocol=2)

    def _attachment_record(self, request_id):
        with self._requests.store._gate():
            expected = self._mirror._expected(request_id)
            profile = json.loads(self._requests._request(request_id)['profile'])
            context = (hex_digest(profile['workspace_fingerprint']), hex_digest(profile['context_fingerprint']))
            return expected, context

    def _linked(self, owner_id, expected=None, context=None):
        self._require_layout()
        link, outcome, payload = self._owner._read(owner_id)
        if not _profile_link(link):
            _denied()
        if expected is not None:
            identity, _ = expected.admission.freeze()
            if link['identity'] != identity or link['admission_digest'] != expected.digest:
                _denied()
        row = _get_slot(self._db, owner_id)
        if link['claim'] == 'unclaimed':
            if row is not None or outcome['seq'] != 1:
                _unavailable()
            return link, outcome, payload, None, None
        value = _slot(link, outcome, row)
        if context is not None and (value['workspace_digest'], value['context_digest']) != context:
            _unavailable()
        return link, outcome, payload, value, row[2]

    @staticmethod
    def _ack(link, outcome):
        return dict(request_id=link['identity']['owner_request_id'],
                    state='answered' if link['reply_available'] else 'unknown',
                    turn_id='' if outcome['conflict'] else link['turn_id'])

    @staticmethod
    def _event(entry, data, link, record):
        if record is None or not entry.matches(record) or type(data) is not dict:
            _denied()
        kind = data.get('kind')
        extras = {'submitted': (), 'started': ('turn_id',), 'unknown': (), 'dropped': (),
                  'completed': ('turn_id', 'outcome', 'answer', 'event_id', 'occurred_at')}
        if type(kind) is not str or kind not in extras:
            _denied()
        keys(data, {'generation', 'native_id', 'client_id', 'request_id', 'attempt_id', 'kind', *extras[kind]})
        if (data['generation'], data['native_id'], data['client_id'], data['request_id'], data['attempt_id']) != (
                entry.generation, entry.agent[2:], entry.client, record['owner_request_id'], record['attempt_id']):
            _denied()
        turn = data.get('turn_id')
        if kind in ('started', 'completed'):
            text(turn, 128, 512)
            if any(ord(char) < 32 for char in turn):
                _denied()
        if kind != 'completed':
            return None, ''
        event_id = canonical_uuid(data['event_id'])
        occurred = integer(data['occurred_at'])
        answer = text(data['answer'], 16000, 64000, nonempty=False)
        if (occurred > MAX_SAFE or data['outcome'] not in OUTCOMES or '\x00' in answer
                or data['outcome'] != 'answered' and answer):
            _denied()
        fingerprint = digest('watch-claude-attachment-receipt-v1', {
            'admission_digest': link['admission_digest'], **{k: data[k] for k in (
                'request_id', 'attempt_id', 'kind', 'turn_id', 'outcome', 'answer', 'event_id', 'occurred_at')},
        })
        reply = ''
        if data['outcome'] == 'answered':
            marker = 'WATCH_REPLY ' + record['owner_request_id']
            lines = answer.splitlines()
            if [line for line in lines if 'WATCH_REPLY' in line] != [marker]:
                _denied()
            reply = '\n'.join(line for line in lines if line != marker).strip()
        return dict(event_id=event_id, fingerprint=fingerprint, outcome=data['outcome'], occurred_at=occurred,
                    turn_digest=digest('watch-claude-attachment-turn-v1', turn)), reply

    def _try_reconcile(self, request_id):
        if self._reconcile is not None:
            try:
                self._reconcile(request_id)
                return True
            except (AttachmentError, LedgerError, sqlite3.Error, OSError, RuntimeError):
                pass
        return False

    def reconcile_pending(self, limit=20):
        """One ON-only retained maintenance page, with no transport authority."""
        if not self._mounted or self._reconcile is None:
            return 0
        if type(limit) is not int or not 1 <= limit <= 50:
            _denied()
        with self._maintenance:
            with self._lock:
                if not self._enabled:
                    return 0
            pending = []
            with self._owner.lock:
                self._require_layout()
                rows = self._db.execute(f'''SELECT owner_request_id FROM main.{CLAUDE_CLAIMS}
                    WHERE owner_request_id>? ORDER BY owner_request_id LIMIT ?''',
                    (self._maintenance_cursor, limit)).fetchall()
                self._maintenance_cursor = rows[-1][0] if len(rows) == limit else ''
                for owner_id, in rows:
                    try:
                        link, outcome, _, record, _ = self._linked(owner_id)
                        unacknowledged = link['ack_seq'] != outcome['seq']
                        if record is not None and (unacknowledged or link['payload_pruned']):
                            pending.append((link['identity']['attachment_request_id'], unacknowledged))
                    except (AttachmentError, LedgerError):
                        continue  # Other exact retained claims remain serviceable.
            repaired = 0
            for request_id, unacknowledged in pending:
                with self._lock:
                    if not self._enabled:
                        break
                if not unacknowledged:
                    try:
                        mirrored = self._mirror._as_of(request_id)
                        if mirrored is not None and mirrored[0]['payload_pruned']:
                            continue
                    except (AttachmentError, LedgerError):
                        continue
                repaired += self._try_reconcile(request_id)
            return repaired

    def event(self, entry, data, *, recheck):
        if not self._mounted or not callable(recheck) or type(data) is not dict:
            _denied()
        owner_id = canonical_uuid(data.get('request_id'))
        observed = _Enrollment.read(entry)
        # Choose first/collision under one adapter lock. Neither DB is held
        # when resolving the other store's detached admission.
        with self._events:
            with self._lock:
                if not self._enabled:
                    _unavailable()
            with self._owner.lock:
                live = _Enrollment.read(recheck())
                if live != observed:
                    _denied()
                link, outcome, _, old, raw = self._linked(owner_id)
                witness, reply = self._event(live, data, link, old)
                request_id = link['identity']['attachment_request_id']
                only_ack = witness is None or witness in (old['first'], old['collision'])
                if only_ack:
                    action = None
                elif old['collision'] is not None:
                    _denied()
                elif old['first'] is None:
                    action = 'claude_terminal'
                    action_digest = _action_digest('terminal', old, (witness['fingerprint'],))
                    next_slot = dict(old, busy=False, first=witness, terminal_digest=action_digest)
                else:
                    action = 'claude_terminal_conflict'
                    action_digest = _action_digest('conflict', old, (old['first']['fingerprint'], witness['fingerprint']))
                    next_slot = dict(old, collision=witness, conflict_digest=action_digest)
                    reply = ''
                digested = link['admission_digest']
                attempt = link['attempt_id']
            expected, context = self._attachment_record(request_id)
            if expected.digest != digested or expected.admission.owner_request_id != owner_id:
                _denied()
            if only_ack:
                with self._owner.lock:
                    current = _Enrollment.read(recheck())
                    link, outcome, _, latest, _ = self._linked(owner_id, expected, context)
                    repeated, _ = self._event(current, data, link, latest)
                    if current != observed or repeated != witness or repeated is not None and repeated not in (
                            latest['first'], latest['collision']):
                        _denied()
                    return self._ack(link, outcome)
            identity = owner_id, digested, attempt, action_digest
            with self._scope(action, request_id, identity):
                check = self._owner._require_native_writer(action, *identity)
                with self._owner._transaction(native=check):
                    current = _Enrollment.read(recheck())
                    link, _, _, latest, exact = self._linked(owner_id, expected, context)
                    if current != observed or exact != raw or latest != old or not current.matches(latest):
                        _denied()
                    with self._lock:
                        if not self._enabled:
                            _unavailable()
                    self._owner._claude_terminal_in_transaction(*identity[:3], action, action_digest,
                        raw, _encoded(next_slot), turn_id=data['turn_id'], reply=reply, now_ms=int(self._now() * 1000))
                    link, outcome, _, _, _ = self._linked(owner_id, expected, context)
                    ack = self._ack(link, outcome)
        self._try_reconcile(request_id)
        return ack

    def observe(self, request_id):
        """Return exact detached facts and live reply text for the bound reader."""
        with self._lock:
            if not self._mounted or not self._enabled:
                _unavailable()
        expected, context = self._attachment_record(request_id)
        owner_id = expected.admission.owner_request_id
        mirrored = self._mirror._as_of(request_id)
        with self._owner.lock:
            link, outcome, payload, _, _ = self._linked(owner_id, expected, context)
            reply = None
            if (mirrored is not None and mirrored[0]['reply_available'] and link['reply_available']
                    and not link['payload_pruned'] and payload is not None
                    and (mirrored[1]['seq'], mirrored[1]['digest'], mirrored[1]['attempt_id']) ==
                        (outcome['seq'], outcome['digest'], outcome['attempt_id'])):
                reply = payload['mutable']['reply']
        if self._attachment_record(request_id) != (expected, context):
            _unavailable()
        with self._lock:
            if not self._enabled:
                _unavailable()
        return dict(link=link, outcome=outcome, reply=reply)
