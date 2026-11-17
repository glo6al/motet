"""Owner attachment ledger with offline writers and a maintenance-only runtime.

This module only records state in an existing owner database. It neither proves
runtime qualification nor invokes a native transport. Production opens only a
validated maintenance ledger for ordinary capacity and bounded recovery.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import sqlite3
import threading
import uuid

from attachments_owner_guards import OwnerGuardError
from attachments_owner_marker import CLEAR, sqlite_payload_state

LINK_BYTES = OUTCOME_BYTES = 4096
PAYLOAD_BYTES = 192 * 1024
CLAUDE_COMPLETION_PROFILE = 'claude-completion-only'
CLAUDE_CLAIMS = 'owner_claude_attachment_claims'
CLAUDE_CLAIM_BYTES = 4096
FROZEN_BYTES, MUTABLE_BYTES = 64 * 1024, 128 * 1024
MAX_LINKS, MAX_HISTORY, MAX_PENDING, MAX_RATE = 10_000, 256, 8, 10
RETENTION_MS = 86_400_000
MAX_INT = 2**63 - 1
ACTIVE = {'sending', 'queued', 'awaiting_reply', 'unknown', 'expired'}
NAMESPACES = (('requests', 'id'), ('controls', 'id'), ('expired_controls', 'id'),
              ('asks', 'id'), ('request_fulfillments', 'request_id'),
              ('request_fulfillments', 'control_request_id'),
              ('peer_requests', 'transport_id'), ('shared_bridge_requests', 'transport_id'),
              ('peer_execution', 'request_id'), ('peer_execution_events', 'request_id'),
              ('peer_reply_admissions', 'request_id'))
IDENTITY_KEYS = {'version', 'owner_request_id', 'attachment_request_id', 'handoff_claim_id',
                 'owner_epoch', 'fingerprint', 'envelope_sha256', 'transport', 'profile',
                 'profile_revision', 'target_binding_digest', 'agent_id', 'payload_digest'}
LINK_KEYS = {'version', 'identity', 'admission_digest', 'revision', 'integrity', 'created_ms',
             'claim', 'attempt_id', 'turn_id', 'latest_seq', 'ack_seq', 'ack_digest',
             'terminal_acked', 'payload_pruned', 'reply_available'}
OUTCOME_KEYS = {'version', 'owner_request_id', 'admission_digest', 'seq', 'digest', 'attempt_id',
                'turn_id', 'accepted', 'started', 'execution_ended', 'not_submitted',
                'reply_captured', 'late_reply_observed', 'capture_withheld', 'conflict',
                'evidence_sequence', 'evidence_fingerprint',
                'accepted_at', 'started_at', 'ended_at', 'not_submitted_at', 'reply_at', 'late_at',
                'accepted_provenance', 'started_provenance', 'terminal_provenance',
                'reply_fingerprint', 'late_reply_fingerprint', 'state', 'reason', 'updated_ms'}
FACTS = ('accepted', 'started', 'execution_ended', 'not_submitted', 'reply_captured',
         'late_reply_observed', 'capture_withheld', 'conflict')
TIMES = ('accepted_at', 'started_at', 'ended_at', 'not_submitted_at', 'reply_at', 'late_at')
PROVENANCES = {'', 'codex_queue_accepted', 'claude_submitted', 'codex_turn_started',
               'claude_started', 'codex_turn_ended', 'claude_turn_ended',
               'correlated_reply', 'proven_not_submitted'}
STATES = {'owner_recorded', 'delivery_pending', 'queued', 'running', 'answered',
          'unknown', 'execution_ended', 'failed'}
REASONS = {'', 'dispatch_uncertain', 'native_dropped', 'reply_conflict', 'owner_payload_pruned',
           'recovery_required'}


class LedgerError(Exception):
    """Safe machine-readable fault; never includes payload or native exceptions."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise LedgerError(code)


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INT:
        fail('invalid_integer')
    return value


def text(value, scalars, byte_limit, *, nonempty=True):
    if not isinstance(value, str) or len(value) > scalars or (nonempty and not value):
        fail('invalid_text')
    try:
        raw = value.encode('utf-8', 'strict')
    except UnicodeError:
        fail('invalid_unicode')
    if len(raw) > byte_limit:
        fail('text_too_large')
    return value


def token(value, limit=128):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,%d}' % limit, value):
        fail('invalid_token')
    return value


def hex_digest(value, *, optional=False):
    if optional and value == '':
        return value
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        fail('invalid_digest')
    return value


def canonical_uuid(value, *, optional=False):
    if optional and value == '':
        return value
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            fail('invalid_uuid')
    except (ValueError, AttributeError):
        fail('invalid_uuid')
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8', 'strict')
    except (ValueError, TypeError, UnicodeError):
        fail('invalid_encoding')


def digest(domain, value):
    return hashlib.sha256(domain.encode('ascii') + b'\0' + canonical(value)).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            fail('duplicate_key')
        result[key] = value
    return result


def decode(raw, size=None):
    try:
        if isinstance(raw, str):
            raw = raw.encode('utf-8', 'strict')
        if not isinstance(raw, bytes) or (size is not None and len(raw) != size):
            fail('invalid_record_size')
        result = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                            parse_constant=lambda _: fail('invalid_encoding'))
        canonical(result)  # Also rejects decoded escaped lone surrogates.
        return result
    except (ValueError, UnicodeError, TypeError):
        fail('invalid_encoding')


def padded(value, size):
    raw = canonical(value)
    if len(raw) > size:
        fail('record_too_large')
    return raw + b' ' * (size - len(raw))


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        fail('invalid_record_shape')


def _seal_link(link):
    link['integrity'] = digest('watch-owner-attachment-link-v1',
                               {k: v for k, v in link.items() if k != 'integrity'})


def _seal_outcome(outcome):
    outcome['digest'] = digest('watch-owner-attachment-outcome-v1',
                               {k: v for k, v in outcome.items() if k != 'digest'})


@dataclass(frozen=True)
class Admission:
    owner_request_id: str
    attachment_request_id: str
    handoff_claim_id: str
    owner_epoch: str
    fingerprint: str
    envelope_sha256: str
    transport: str
    profile: str
    profile_revision: str
    target_binding_digest: str
    agent_id: str
    question: str
    envelope: str
    name: str = ''

    def freeze(self):
        canonical_uuid(self.owner_request_id)
        for value in (self.attachment_request_id, self.handoff_claim_id, self.owner_epoch,
                      self.profile_revision):
            token(value)
        token(self.profile, 64)
        if self.transport not in ('codex', 'claude'):
            fail('unsupported_transport')
        prefix = 'x:' if self.transport == 'codex' else 'c:'
        if not isinstance(self.agent_id, str) or not self.agent_id.startswith(prefix):
            fail('invalid_target')
        canonical_uuid(self.agent_id[2:])
        for value in (self.fingerprint, self.envelope_sha256, self.target_binding_digest):
            hex_digest(value)
        text(self.question, 2000, 8000)
        text(self.envelope, 4000, 16000)
        if not self.question.strip() or '\x00' in self.question or '\x00' in self.envelope:
            fail('invalid_text')
        text(self.name, 120, 480, nonempty=False)
        if hashlib.sha256(self.envelope.encode('utf-8')).hexdigest() != self.envelope_sha256:
            fail('envelope_mismatch')
        frozen = dict(agent_id=self.agent_id, question=self.question, envelope=self.envelope, name=self.name)
        if len(canonical(frozen)) > FROZEN_BYTES:
            fail('record_too_large')
        identity = {k: v for k, v in asdict(self).items() if k not in ('question', 'envelope', 'name')}
        identity.update(version=1, payload_digest=digest('watch-owner-attachment-payload-v1', frozen))
        keys(identity, IDENTITY_KEYS)
        return identity, frozen


@dataclass
class OrdinaryChanges:
    changes: int = 0
    eligible: bool = True

    def record(self, cursor):
        if cursor.rowcount < 0:
            self.eligible = False
        else:
            self.changes += cursor.rowcount


class OwnerAttachmentLedger:
    """L1 only. Caller supplies the owner's connection and SAME RLock.

    A qualified admission detaches its attachment proof before taking the owner
    lock, then holds the namespace fence through the owner transaction. No
    owner transaction calls another store or a provider. All times are explicit.
    """
    def __init__(self, db, lock, *, guard=None, maintenance_only=False, _native_permission=None):
        self.db, self.lock = db, lock
        if _native_permission is not None and not maintenance_only:
            fail('owner_guard_required')
        if guard is not None:
            if guard.db is not db or guard.lock is not lock:
                fail('owner_guard_writer_mismatch')
            if guard.capacity is not None:
                fail('owner_guard_already_bound')
        self.guard = guard
        self.maintenance_only = maintenance_only
        self._native_permission = _native_permission
        self.directory = None
        self._writer_active = False
        self._writer_thread = None
        self._dirty_ids = set()
        if maintenance_only:
            if self.guard is None:
                fail('owner_guard_required')
            with self.lock:
                self.guard.require_unshadowed()
                self.guard.require_schema()
                self.guard.require_neutral()
        else:
            with self._transaction():
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {t for t, _ in NAMESPACES} <= tables:
                    fail('owner_schema_missing')
                db.execute('CREATE TABLE IF NOT EXISTS owner_attachment_links (owner_request_id TEXT PRIMARY KEY, record BLOB NOT NULL CHECK(length(record)=4096))')
                db.execute('CREATE TABLE IF NOT EXISTS owner_attachment_outcomes (owner_request_id TEXT PRIMARY KEY, record BLOB NOT NULL CHECK(length(record)=4096))')
        from attachments_owner_directory import OwnerAttachmentDirectory
        self.directory = OwnerAttachmentDirectory(self)
        if not maintenance_only:
            self.rebuild_directory()
        if self.guard is not None:
            with self.lock:
                if self.guard.capacity is not None:
                    fail('owner_guard_already_bound')
                self.guard.capacity = self.ordinary_slots

    def rebuild_directory(self):
        """Explicit maintenance, outside any caller transaction; each step releases its lock."""
        self.directory.begin_rebuild()
        while not self.directory.rebuild_step():
            pass  # One bounded pass; an intervening change raises instead of retrying.


    @contextmanager
    def _transaction(self, *, ordinary=False, native=None):
        with self.lock:
            if self.db.in_transaction:
                fail('nested_transaction')
            self.db.execute('BEGIN IMMEDIATE')
            self._writer_active = True
            self._writer_thread = threading.get_ident()
            self._dirty_ids = set()
            directory = self.directory
            proof = OrdinaryChanges() if ordinary else None
            before = None
            try:
                if self.maintenance_only:
                    self.guard.require_unshadowed()
                if self.guard is not None:
                    self.guard.require_schema()
                if self.maintenance_only or proof is not None:
                    self.guard.require_neutral()
                if native is not None:
                    native()
                before = directory.epoch() if directory is not None else None
                was_current = directory.matches(before) if directory is not None else False
                yield proof
                if proof is not None:
                    if not self.db.in_transaction:
                        fail('ordinary_writer_lost')
                    self.guard.require_unshadowed()
                    self.guard.require_schema()
                    self.guard.require_neutral()
                updates = {}
                if directory is not None and was_current:
                    for owner_id in self._dirty_ids:
                        link, outcome, payload = self._read(owner_id)
                        updates[owner_id] = directory.project(owner_id, link, outcome, payload is not None)
                if native is not None:
                    self.guard.require_unshadowed()
                    self.guard.require_neutral()
                    native()
                self.db.commit()
                if proof is not None:
                    after = directory.epoch()
                    if not self._dirty_ids and proof.eligible and after[1] - before[1] == proof.changes:
                        directory.rebase_ordinary(before, after)
                    elif after != before:
                        directory.abandon()
                elif directory is not None and self._dirty_ids:
                    after = directory.epoch()
                    if was_current and after[0] == before[0] and after[2:] == before[2:]:
                        directory._committed(updates, after)
                    else:
                        directory.invalidate()
                elif self.maintenance_only and directory.epoch() != before:
                    directory.abandon()  # A read-only batch must still survive its commit boundary.
            except BaseException as error:
                if proof is not None and (before is None or self.db.total_changes != before[1]
                                          or isinstance(error, (sqlite3.Error, OwnerGuardError))):
                    directory.abandon()
                elif directory is not None and (self._dirty_ids or isinstance(error, (sqlite3.Error, OwnerGuardError))):
                    directory.invalidate()
                self.db.rollback()
                raise
            finally:
                self._writer_active = False
                self._writer_thread = None
                self._dirty_ids = set()

    def ordinary_writer(self):
        return self._transaction(ordinary=self.maintenance_only)

    def _require_native_writer(self, action=None, *identity):
        if not self.maintenance_only:
            return None
        if self._native_permission is None:
            fail('owner_ledger_maintenance_only')
        return self._native_permission.check(action, *identity)

    @staticmethod
    def decode_pair(owner_id, link_raw, outcome_raw):
        """Pure fixed-record validation shared with the bounded directory scan."""
        link = decode(link_raw, LINK_BYTES)
        keys(link, LINK_KEYS)
        keys(link['identity'], IDENTITY_KEYS)
        identity = link['identity']
        if link['version'] != 1 or identity['version'] != 1 or identity['owner_request_id'] != owner_id:
            fail('corrupt_link')
        # Revalidate the complete immutable identity even if a checksum was recomputed.
        canonical_uuid(owner_id)
        for k in ('attachment_request_id', 'handoff_claim_id', 'owner_epoch', 'profile_revision'):
            token(identity[k])
        token(identity['profile'], 64)
        if identity['transport'] not in ('codex', 'claude'):
            fail('corrupt_link')
        prefix = 'x:' if identity['transport'] == 'codex' else 'c:'
        if not isinstance(identity['agent_id'], str) or not identity['agent_id'].startswith(prefix):
            fail('corrupt_link')
        canonical_uuid(identity['agent_id'][2:])
        for k in ('fingerprint', 'envelope_sha256', 'target_binding_digest', 'payload_digest'):
            hex_digest(identity[k])
        if link['admission_digest'] != digest('watch-owner-attachment-admission-v1', identity):
            fail('corrupt_link')
        if link['integrity'] != digest('watch-owner-attachment-link-v1', {k: v for k, v in link.items() if k != 'integrity'}):
            fail('corrupt_link')
        for k in ('revision', 'latest_seq'):
            integer(link[k], 1)
        for k in ('created_ms', 'ack_seq'):
            integer(link[k])
        for k in ('terminal_acked', 'payload_pruned', 'reply_available'):
            if type(link[k]) is not bool:
                fail('corrupt_link')
        if link['claim'] not in ('unclaimed', 'claimed'):
            fail('corrupt_link')
        canonical_uuid(link['attempt_id'], optional=link['claim'] == 'unclaimed')
        if link['claim'] == 'unclaimed' and link['attempt_id']:
            fail('corrupt_link')
        text(link['turn_id'], 128, 512, nonempty=False)
        hex_digest(link['ack_digest'], optional=link['ack_seq'] == 0)
        outcome = decode(outcome_raw, OUTCOME_BYTES)
        keys(outcome, OUTCOME_KEYS)
        integer(outcome['seq'], 1)
        integer(outcome['evidence_sequence'])
        hex_digest(outcome['evidence_fingerprint'], optional=outcome['evidence_sequence'] == 0)
        if (outcome['evidence_sequence'] == 0 and outcome['evidence_fingerprint']
                or outcome['evidence_sequence'] > outcome['seq']
                or link['claim'] == 'unclaimed' and outcome['evidence_sequence']):
            fail('corrupt_evidence')
        if (outcome['version'] != 1 or outcome['owner_request_id'] != owner_id
                or outcome['admission_digest'] != link['admission_digest']
                or outcome['seq'] != link['latest_seq'] or link['ack_seq'] > outcome['seq']
                or outcome['attempt_id'] != link['attempt_id'] or outcome['turn_id'] != link['turn_id']):
            fail('corrupt_outcome')
        integer(outcome['seq'], 1)
        for k in FACTS:
            if type(outcome[k]) is not bool:
                fail('corrupt_outcome')
        for k in TIMES + ('updated_ms',):
            integer(outcome[k])
        if (type(outcome['state']) is not str or outcome['state'] not in STATES
                or type(outcome['reason']) is not str or outcome['reason'] not in REASONS):
            fail('corrupt_outcome')
        for k in ('accepted_provenance', 'started_provenance', 'terminal_provenance'):
            if type(outcome[k]) is not str or outcome[k] not in PROVENANCES:
                fail('corrupt_outcome')
        for k in ('reply_fingerprint', 'late_reply_fingerprint'):
            hex_digest(outcome[k], optional=True)
        if ((outcome['started'] or outcome['execution_ended']) and not outcome['accepted']
                or outcome['reply_captured'] and not outcome['execution_ended']
                or outcome['late_reply_observed'] and not link['payload_pruned']
                or link['reply_available'] and (not outcome['reply_captured'] or outcome['conflict'])):
            fail('corrupt_outcome')
        if outcome['not_submitted'] and (outcome['accepted'] or outcome['started'] or outcome['execution_ended']):
            fail('corrupt_outcome')
        if outcome['digest'] != digest('watch-owner-attachment-outcome-v1', {k: v for k, v in outcome.items() if k != 'digest'}):
            fail('corrupt_outcome')
        if link['ack_seq'] == outcome['seq'] and link['ack_digest'] != outcome['digest']:
            fail('corrupt_ack')
        if link['terminal_acked'] and not (outcome['execution_ended'] or outcome['not_submitted']):
            fail('corrupt_ack')
        return link, outcome

    def _read(self, owner_id):
        raw = self.db.execute('SELECT record FROM owner_attachment_links WHERE owner_request_id=?', (owner_id,)).fetchone()
        if raw is None:
            fail('not_found')
        for table, column in NAMESPACES[1:]:
            if self.db.execute('SELECT 1 FROM ' + table + ' WHERE ' + column + '=? LIMIT 1', (owner_id,)).fetchone():
                fail('namespace_conflict')
        outcome_row = self.db.execute('SELECT record FROM owner_attachment_outcomes WHERE owner_request_id=?', (owner_id,)).fetchone()
        if outcome_row is None:
            fail('corrupt_outcome')
        link, outcome = self.decode_pair(owner_id, raw[0], outcome_row[0])
        identity = link['identity']
        raw = self.db.execute('SELECT data FROM requests WHERE id=?', (owner_id,)).fetchone()
        if link['payload_pruned']:
            if raw is not None or not link['terminal_acked'] or link['reply_available']:
                fail('corrupt_prune')
            payload = None
        else:
            if raw is None:
                fail('payload_missing')
            payload = decode(raw[0], PAYLOAD_BYTES)
            keys(payload, {'attachment_owner_version', 'request_id', 'frozen', 'mutable'})
            keys(payload['frozen'], {'agent_id', 'question', 'envelope', 'name'})
            keys(payload['mutable'], {'reply'})
            if payload['attachment_owner_version'] != 1 or payload['request_id'] != owner_id:
                fail('corrupt_payload')
            f = payload['frozen']
            text(f['question'], 2000, 8000)
            text(f['envelope'], 4000, 16000)
            if not f['question'].strip() or '\x00' in f['question'] or '\x00' in f['envelope']:
                fail('corrupt_payload')
            text(f['name'], 120, 480, nonempty=False)
            text(payload['mutable']['reply'], 16000, 64000, nonempty=False)
            if (digest('watch-owner-attachment-payload-v1', f) != identity['payload_digest']
                    or f['agent_id'] != identity['agent_id']
                    or hashlib.sha256(f['envelope'].encode()).hexdigest() != identity['envelope_sha256']):
                fail('corrupt_payload')
            if outcome['reply_captured']:
                if (not payload['mutable']['reply']
                        or hashlib.sha256(payload['mutable']['reply'].encode()).hexdigest() != outcome['reply_fingerprint']):
                    fail('corrupt_payload')
            elif payload['mutable']['reply']:
                fail('corrupt_payload')
        return link, outcome, payload

    def _write_link(self, link):
        self._dirty_ids.add(link['identity']['owner_request_id'])
        _seal_link(link)
        self.db.execute('UPDATE owner_attachment_links SET record=? WHERE owner_request_id=?',
                        (padded(link, LINK_BYTES), link['identity']['owner_request_id']))

    def _write(self, link, outcome, payload):
        integer(link['revision'] + 1, 1)
        link['revision'] += 1
        _seal_outcome(outcome)
        link['latest_seq'] = outcome['seq']
        self._write_link(link)
        self.db.execute('UPDATE owner_attachment_outcomes SET record=? WHERE owner_request_id=?',
                        (padded(outcome, OUTCOME_BYTES), link['identity']['owner_request_id']))
        if payload is not None:
            if len(canonical(payload['frozen'])) > FROZEN_BYTES or len(canonical(payload['mutable'])) > MUTABLE_BYTES:
                fail('record_too_large')
            body = padded(payload, PAYLOAD_BYTES).decode('utf-8')
            if self.guard is not None:
                self.guard.write_payload('U', link['identity']['owner_request_id'], body)
            else:
                self.db.execute('UPDATE requests SET data=? WHERE id=?',
                                (body, link['identity']['owner_request_id']))

    @staticmethod
    def _status(link, outcome):
        # Availability is a current link fact, not historical reply_captured.
        return dict(owner_request_id=link['identity']['owner_request_id'],
                    admission_digest=link['admission_digest'], link_revision=link['revision'],
                    outcome=outcome, state=('execution_ended' if link['payload_pruned'] and outcome['execution_ended'] else outcome['state']),
                    claim=link['claim'], attempt_id=link['attempt_id'],
                    turn_id=link['turn_id'], payload_pruned=link['payload_pruned'],
                    reply_available=link['reply_available'] and not link['payload_pruned'],
                    pending_ack=link['ack_seq'] != outcome['seq'], terminal_acked=link['terminal_acked'])

    def status(self, owner_id, admission_digest=None):
        canonical_uuid(owner_id)
        with self._transaction():
            link, outcome, _ = self._read(owner_id)
            self._match(link, admission_digest, optional=True)
            return self._status(link, outcome)

    def _exact_snapshot(self, owner_id, admission_digest):
        """Detached mirror input; validate the retained payload without returning it."""
        canonical_uuid(owner_id)
        with self._transaction():
            link, outcome, _ = self._read(owner_id)
            self._match(link, admission_digest)
            return padded(link, LINK_BYTES), padded(outcome, OUTCOME_BYTES)

    @staticmethod
    def _match(link, expected, *, optional=False):
        if optional and expected is None:
            return
        hex_digest(expected)
        if expected != link['admission_digest']:
            fail('identity_conflict')

    def _counts(self, now_ms, *, include_full_history=False):
        directory = self.directory
        retained, linked_history, pending, recent = directory.counts(now_ms)
        # Refusal at the retained cap never walks historical tombstones or payloads.
        if retained >= MAX_LINKS and not include_full_history:
            return retained, linked_history, pending, recent
        history = linked_history
        body = 'r.data' if self.guard is None else f'CASE WHEN {sqlite_payload_state("r.data")} = {CLEAR} THEN r.data END'
        rows = self.db.execute(f"""SELECT r.id,{body} FROM requests r
            WHERE NOT EXISTS (SELECT 1 FROM owner_attachment_links l WHERE l.owner_request_id=r.id)
              AND NOT EXISTS (SELECT 1 FROM owner_attachment_outcomes o WHERE o.owner_request_id=r.id)
              AND NOT EXISTS (SELECT 1 FROM peer_requests p WHERE p.transport_id=r.id)
              AND NOT EXISTS (SELECT 1 FROM shared_bridge_requests p WHERE p.transport_id=r.id)
            LIMIT ?""", (MAX_HISTORY + 1,)).fetchall()
        if len(rows) > MAX_HISTORY:
            fail('history_capacity')
        for owner_id, raw in rows:
            if raw is None:
                fail('owner_history_invalid')
            row = decode(raw)
            if not isinstance(row, dict) or 'attachment_owner_version' in row:
                fail('orphan_payload')
            if row.get('origin') == 'peer':
                fail('owner_history_invalid')  # No matching durable peer reservation.
            if (row.get('request_id') != owner_id or type(row.get('state')) is not str
                    or row['state'] not in ACTIVE | {'answered', 'failed'}):
                fail('owner_history_invalid')
            created = row.get('created')
            if type(created) not in (int, float) or not 0 <= created <= MAX_INT / 1000:
                fail('owner_history_invalid')
            history += 1
            pending += row['state'] in ACTIVE
            recent += now_ms - created * 1000 < 60_000
        return retained, history, pending, recent

    @contextmanager
    def ordinary_slots(self, now_ms):
        """Use the same exact combined counts and writer as native admission."""
        try:
            with self.ordinary_writer() as proof:
                _, history, pending, recent = self._counts(now_ms, include_full_history=True)
                yield (history, pending, recent), proof
        except LedgerError:
            raise OwnerGuardError('Owner attachment capacity is unavailable.') from None

    def admit(self, admission, *, now_ms):
        if self._native_permission is None:
            self._require_native_writer('admit')
        integer(now_ms)
        if not isinstance(admission, Admission):
            fail('invalid_admission')
        identity, frozen = admission.freeze()
        owner_id = admission.owner_request_id
        identity_hash = digest('watch-owner-attachment-admission-v1', identity)
        permission = self._native_permission
        if permission is not None:
            permission.bind_admission(self, admission, identity_hash)
        check = self._require_native_writer('admit', owner_id, identity_hash)
        fence = permission.admission_fence(self, owner_id) if permission is not None else nullcontext()
        with fence, self._transaction(native=check):
            if self.db.execute('SELECT 1 FROM owner_attachment_links WHERE owner_request_id=?', (owner_id,)).fetchone():
                link, outcome, _ = self._read(owner_id)
                self._match(link, identity_hash)
                if link['payload_pruned']:
                    fail('retained_tombstone')
                return dict(self._status(link, outcome), new_admission=False)
            for table, column in NAMESPACES + (('owner_attachment_outcomes', 'owner_request_id'),):
                if self.db.execute('SELECT 1 FROM ' + table + ' WHERE ' + column + '=? LIMIT 1', (owner_id,)).fetchone():
                    fail('namespace_conflict')
            retained, history, pending, recent = self._counts(now_ms)
            for count, maximum, code in ((retained, MAX_LINKS, 'link_capacity'), (history, MAX_HISTORY, 'history_capacity'),
                                          (pending, MAX_PENDING, 'pending_capacity'), (recent, MAX_RATE, 'rate_capacity')):
                if count >= maximum:
                    fail(code)
            link = dict(version=1, identity=identity, admission_digest=identity_hash, revision=1,
                        integrity='', created_ms=now_ms, claim='unclaimed', attempt_id='', turn_id='',
                        latest_seq=1, ack_seq=0, ack_digest='', terminal_acked=False, payload_pruned=False,
                        reply_available=False)
            outcome = dict(version=1, owner_request_id=owner_id, admission_digest=identity_hash, seq=1,
                           digest='', attempt_id='', turn_id='', state='owner_recorded', reason='', updated_ms=now_ms,
                           accepted_provenance='', started_provenance='', terminal_provenance='',
                           reply_fingerprint='', late_reply_fingerprint='',
                           evidence_sequence=0, evidence_fingerprint='')
            outcome.update({k: False for k in FACTS})
            outcome.update({k: 0 for k in TIMES})
            payload = dict(attachment_owner_version=1, request_id=owner_id, frozen=frozen, mutable=dict(reply=''))
            _seal_link(link)
            _seal_outcome(outcome)
            self._dirty_ids.add(owner_id)
            self.db.execute('INSERT INTO owner_attachment_links VALUES (?,?)', (owner_id, padded(link, LINK_BYTES)))
            self.db.execute('INSERT INTO owner_attachment_outcomes VALUES (?,?)', (owner_id, padded(outcome, OUTCOME_BYTES)))
            body = padded(payload, PAYLOAD_BYTES).decode('utf-8')
            if self.guard is not None:
                self.guard.write_payload('I', owner_id, body)
            else:
                self.db.execute('INSERT INTO requests VALUES (?,?)', (owner_id, body))
            return dict(self._status(link, outcome), new_admission=True)

    def record_claim(self, owner_id, admission_digest, *, now_ms):
        """Bookkeeping only. NOT a resource permit or permission to dispatch."""
        check = self._require_native_writer('claim', owner_id, admission_digest)
        integer(now_ms)
        with self._transaction(native=check):
            link, outcome, payload = self._read(owner_id)
            self._match(link, admission_digest)
            if link['identity']['profile'] == CLAUDE_COMPLETION_PROFILE:
                fail('protected_claude_claim')
            return self._record_claim_locked(link, outcome, payload, None, now_ms)

    def _record_claim_locked(self, link, outcome, payload, attempt, now_ms):
        if link['payload_pruned']:
            fail('retained_tombstone')
        if link['claim'] == 'claimed':
            return dict(self._status(link, outcome), new_claim=False)
        if attempt is None:
            attempt = str(uuid.uuid4())
        link.update(claim='claimed', attempt_id=attempt)
        outcome.update(attempt_id=attempt, seq=integer(outcome['seq'] + 1, 1),
                       state='delivery_pending', updated_ms=now_ms)
        self._write(link, outcome, payload)
        return dict(self._status(link, outcome), new_claim=True)

    def _claude_writer(self, action, *identity):
        if (not self.maintenance_only or self._native_permission is None or self.guard is None
                or not self.db.in_transaction or not self._writer_active or self._writer_thread != threading.get_ident()):
            fail('protected_claude_writer_required')
        self._require_native_writer(action, *identity)()

    @staticmethod
    def _claude_slot(raw, link, attempt):
        value = decode(raw, CLAUDE_CLAIM_BYTES)
        identity = link['identity']
        if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1
                or value.get('identity') != identity
                or value.get('owner_request_id') != identity['owner_request_id']
                or value.get('agent_id') != identity['agent_id'] or value.get('admission_digest') != link['admission_digest']
                or value.get('attempt_id') != attempt or type(value.get('busy')) is not bool
                or value.get('checksum') != digest('watch-claude-attachment-claim-v1',
                                                  {k: v for k, v in value.items() if k != 'checksum'})
                or identity['transport'] != 'claude' or identity['profile'] != CLAUDE_COMPLETION_PROFILE):
            fail('protected_claude_conflict')
        return value

    def _claim_claude_in_transaction(self, owner_id, admission_digest, attempt, record, *, now_ms):
        self._claude_writer('claude_claim', owner_id, admission_digest)
        canonical_uuid(attempt)
        integer(now_ms)
        link, outcome, payload = self._read(owner_id)
        self._match(link, admission_digest)
        slot = self._claude_slot(record, link, attempt)
        if (link['claim'] != 'unclaimed' or outcome['seq'] != 1 or outcome['evidence_sequence']
                or not slot['busy'] or slot.get('first') is not None or slot.get('collision') is not None
                or slot.get('terminal_digest') != '' or slot.get('conflict_digest') != ''):
            fail('protected_claude_conflict')
        result = self._record_claim_locked(link, outcome, payload, attempt, now_ms)
        self.db.execute(f'INSERT INTO main.{CLAUDE_CLAIMS} VALUES (?,?,1,?)', (owner_id, slot['agent_id'], record))
        return result

    def _claude_terminal_in_transaction(self, owner_id, admission_digest, attempt, action, action_digest,
                                        prior, replacement, *, turn_id, reply, now_ms):
        if action not in ('claude_terminal', 'claude_terminal_conflict'):
            fail('protected_claude_conflict')
        self._claude_writer(action, owner_id, admission_digest, attempt, action_digest)
        canonical_uuid(attempt)
        hex_digest(action_digest)
        integer(now_ms)
        text(turn_id, 128, 512)
        text(reply, 16000, 64000, nonempty=False)
        link, outcome, payload = self._read(owner_id)
        self._match(link, admission_digest)
        old, new = (self._claude_slot(raw, link, attempt) for raw in (prior, replacement))
        changed = {'busy', 'first', 'collision', 'terminal_digest', 'conflict_digest', 'checksum'}
        stored = self.db.execute(f'SELECT agent_id,busy,record FROM main.{CLAUDE_CLAIMS} WHERE owner_request_id=?',
                                 (owner_id,)).fetchone()
        if (stored is None or tuple(stored) != (old['agent_id'], int(old['busy']), prior)
                or link['claim'] != 'claimed' or link['attempt_id'] != attempt or outcome['evidence_sequence']
                or outcome['evidence_fingerprint'] or outcome['started'] or outcome['not_submitted']
                or new['busy'] or set(old) != set(new) or any(old[k] != new[k] for k in old.keys() - changed)):
            fail('protected_claude_conflict')
        first, collision = new['first'], new['collision']
        if action == 'claude_terminal':
            if (outcome['seq'] != 2 or outcome['execution_ended'] or link['turn_id'] or payload is None
                    or not old['busy'] or old['first'] is not None or old['collision'] is not None
                    or first is None or collision is not None or new['conflict_digest'] or new['terminal_digest'] != action_digest
                    or first['turn_digest'] != digest('watch-claude-attachment-turn-v1', turn_id)
                    or reply and first['outcome'] != 'answered'):
                fail('protected_claude_conflict')
            link['turn_id'] = outcome['turn_id'] = turn_id
            outcome.update(accepted=True, execution_ended=True, accepted_at=now_ms, ended_at=now_ms,
                           accepted_provenance='claude_turn_ended', terminal_provenance='claude_turn_ended',
                           capture_withheld=first['outcome'] == 'withheld', state='execution_ended', reason='')
            if reply:
                payload['mutable']['reply'] = reply
                link['reply_available'] = True
                outcome.update(reply_captured=True, reply_at=now_ms, state='answered',
                               reply_fingerprint=hashlib.sha256(reply.encode('utf-8')).hexdigest())
        else:
            if (outcome['seq'] != 3 or not outcome['execution_ended'] or outcome['conflict']
                    or old['busy'] or first is None or old['first'] != first or old['collision'] is not None
                    or collision is None or collision['fingerprint'] == first['fingerprint']
                    or collision['turn_digest'] != digest('watch-claude-attachment-turn-v1', turn_id)
                    or new['terminal_digest'] != old['terminal_digest'] or new['conflict_digest'] != action_digest or reply):
                fail('protected_claude_conflict')
            link['reply_available'] = False
            outcome.update(conflict=True, capture_withheld=True, state='execution_ended', reason='reply_conflict')
        outcome.update(seq=integer(outcome['seq'] + 1, 1), updated_ms=now_ms)
        self._write(link, outcome, payload)
        updated = self.db.execute(f'UPDATE main.{CLAUDE_CLAIMS} SET busy=0,record=? WHERE owner_request_id=? AND record=?',
                                  (replacement, owner_id, prior))
        if updated.rowcount != 1:
            fail('protected_claude_conflict')
        return self._status(link, outcome)

    def record_outcome(self, owner_id, admission_digest, attempt_id, kind, *, evidence_sequence, now_ms,
                       turn_id='', reply=''):
        """Trusted adapter evidence only; this module does not verify a provider.

        evidence_sequence is a replay-stable per-attempt source ordinal, never
        callback time or a counter minted on every callback. No current adapter
        is qualified to supply this stream. not_submitted requires separately
        verified nonexecution evidence; a dropped receipt alone is not proof.
        """
        check = self._require_native_writer('evidence', owner_id, admission_digest, attempt_id)
        integer(evidence_sequence, 1)
        integer(now_ms)
        canonical_uuid(attempt_id)
        if kind not in ('accepted', 'started', 'ended', 'reply', 'not_submitted', 'unknown', 'dropped'):
            fail('invalid_outcome')
        text(turn_id, 128, 512, nonempty=kind in ('started', 'ended', 'reply'))
        text(reply, 16000, 64000, nonempty=kind == 'reply')
        if kind != 'reply' and reply:
            fail('invalid_outcome')
        with self._transaction(native=check):
            link, outcome, payload = self._read(owner_id)
            self._match(link, admission_digest)
            if link['identity']['profile'] == CLAUDE_COMPLETION_PROFILE:
                fail('protected_claude_outcome')
            if link['attempt_id'] != attempt_id or link['claim'] != 'claimed':
                fail('attempt_conflict')
            if link['turn_id'] and turn_id and link['turn_id'] != turn_id:
                fail('turn_conflict')
            evidence_fingerprint = digest('watch-owner-attachment-evidence-v1', dict(
                admission_digest=link['admission_digest'], attempt_id=attempt_id, sequence=evidence_sequence,
                kind=kind, turn_id=turn_id, reply_fingerprint=hashlib.sha256(reply.encode('utf-8')).hexdigest() if kind == 'reply' else ''))
            frontier = outcome['evidence_sequence']
            if evidence_sequence == frontier:
                if evidence_fingerprint != outcome['evidence_fingerprint']:
                    fail('evidence_conflict')
                return dict(self._status(link, outcome), evidence='duplicate')
            if evidence_sequence < frontier:
                return dict(self._status(link, outcome), evidence='stale_evidence')
            if evidence_sequence != frontier + 1:
                fail('evidence_gap')
            transport = link['identity']['transport']
            if turn_id and not link['turn_id']:
                link['turn_id'] = outcome['turn_id'] = turn_id
            terminal = outcome['execution_ended'] or outcome['not_submitted']
            if kind == 'not_submitted':
                if outcome['accepted'] or outcome['started'] or outcome['execution_ended']:
                    fail('nonexecution_conflict')
                if not outcome['not_submitted']:
                    outcome.update(not_submitted=True, not_submitted_at=now_ms,
                                   terminal_provenance='proven_not_submitted', state='failed', reason='')
            elif kind in ('unknown', 'dropped'):
                if not terminal:
                    outcome.update(state='unknown', reason='native_dropped' if kind == 'dropped' else 'dispatch_uncertain',
                                   capture_withheld=True)
            else:
                if outcome['not_submitted']:
                    fail('nonexecution_conflict')
                if not outcome['accepted']:
                    provenance = ('codex_queue_accepted' if transport == 'codex' else 'claude_submitted') if kind == 'accepted' else (
                        ('codex_turn_started' if transport == 'codex' else 'claude_started') if kind == 'started' else
                        ('correlated_reply' if kind == 'reply' else ('codex_turn_ended' if transport == 'codex' else 'claude_turn_ended')))
                    outcome.update(accepted=True, accepted_at=now_ms, accepted_provenance=provenance)
                if kind == 'started' and not outcome['started']:
                    outcome.update(started=True, started_at=now_ms,
                                   started_provenance='codex_turn_started' if transport == 'codex' else 'claude_started')
                if kind in ('ended', 'reply') and not outcome['execution_ended']:
                    outcome.update(execution_ended=True, ended_at=now_ms,
                                   terminal_provenance='correlated_reply' if kind == 'reply' else
                                   ('codex_turn_ended' if transport == 'codex' else 'claude_turn_ended'))
                if kind == 'reply':
                    fingerprint = hashlib.sha256(reply.encode('utf-8')).hexdigest()
                    field = 'late_reply_fingerprint' if link['payload_pruned'] else 'reply_fingerprint'
                    if outcome[field] and outcome[field] != fingerprint:
                        outcome.update(conflict=True, capture_withheld=True)
                        link['reply_available'] = False
                    elif link['payload_pruned']:
                        if not outcome['late_reply_observed']:
                            outcome.update(late_reply_observed=True, late_reply_fingerprint=fingerprint, late_at=now_ms,
                                           capture_withheld=True)
                            if outcome['reply_fingerprint'] and outcome['reply_fingerprint'] != fingerprint:
                                outcome['conflict'] = True
                    elif not outcome['reply_captured']:
                        outcome.update(reply_captured=True, reply_fingerprint=fingerprint, reply_at=now_ms)
                        payload['mutable']['reply'] = reply
                        link['reply_available'] = True
                # Fresh evidence cannot regress stronger cumulative start/terminal facts.
                if link['payload_pruned']:
                    outcome.update(state='execution_ended', reason='owner_payload_pruned')
                elif outcome['conflict']:
                    outcome.update(state='unknown', reason='reply_conflict')
                elif link['reply_available']:
                    outcome.update(state='answered', reason='')
                elif outcome['execution_ended']:
                    outcome.update(state='execution_ended', reason='')
                elif outcome['started']:
                    outcome.update(state='running', reason='')
                else:
                    outcome.update(state='queued', reason='')
            # A new source receipt advances the mirror even when its semantic facts
            # were already known; ack never changes either independent sequence.
            outcome.update(seq=integer(outcome['seq'] + 1, 1), updated_ms=now_ms,
                           evidence_sequence=evidence_sequence, evidence_fingerprint=evidence_fingerprint)
            self._write(link, outcome, payload)
            return dict(self._status(link, outcome), evidence='recorded')

    def pending_outcomes(self, limit=50):
        self._require_native_writer('pending')
        if type(limit) is not int or not 1 <= limit <= 50:
            fail('invalid_limit')
        with self._transaction():
            result = []
            for owner_id in self.directory.pending_ids(limit):
                try:
                    link, outcome, _ = self._read(owner_id)
                except LedgerError:
                    # This request remains charged but cannot poison unrelated results.
                    self.directory.quarantine(owner_id)
                    continue
                if link['ack_seq'] != outcome['seq']:
                    result.append(self._status(link, outcome))
            return result

    def acknowledge(self, owner_id, admission_digest, sequence, outcome_digest):
        check = self._require_native_writer('ack', owner_id, admission_digest, sequence, outcome_digest)
        integer(sequence, 1)
        hex_digest(outcome_digest)
        with self._transaction(native=check):
            link, outcome, _ = self._read(owner_id)
            self._match(link, admission_digest)
            if sequence != outcome['seq'] or outcome_digest != outcome['digest']:
                return dict(self._status(link, outcome), acknowledgment='stale')
            if link['ack_seq'] != sequence:
                link.update(ack_seq=sequence, ack_digest=outcome_digest,
                            revision=integer(link['revision'] + 1, 1))
                if outcome['execution_ended'] or outcome['not_submitted']:
                    link['terminal_acked'] = True
                self._write_link(link)
            return dict(self._status(link, outcome), acknowledgment='acknowledged')

    def prune_payload(self, owner_id, admission_digest, *, now_ms):
        self._require_native_writer('prune')
        integer(now_ms)
        with self._transaction():
            link, outcome, _ = self._read(owner_id)
            self._match(link, admission_digest)
            if link['payload_pruned']:
                return self._status(link, outcome)
            if (not link['terminal_acked'] or link['ack_seq'] != outcome['seq']
                    or now_ms - link['created_ms'] <= RETENTION_MS):
                fail('payload_pinned')
            if self.guard is not None:
                self.guard.write_payload('D', owner_id)
            else:
                self.db.execute('DELETE FROM requests WHERE id=?', (owner_id,))
            link.update(payload_pruned=True, reply_available=False, revision=integer(link['revision'] + 1, 1))
            self._write_link(link)
            return self._status(link, outcome)
