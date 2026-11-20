"""Additive server-observed times for existing shared peer executions.

This is not a provider proof verifier. Call record only for a new observation
from an already-verified exact source. Startup scans, historical receipt
replays, labels and timers cannot supply a new observation. Callers hold the
store lock and the normal owner-namespace write authorization. Writes require
an active caller-owned SQLite transaction. This module does not commit, change
execution or capture, retain payloads, or authorize a launch.

ProgressClockError means progress is unavailable for that observation. Future
callers must handle it separately from the already-verified provider event so a
clock failure does not roll back completion or restore an execution fence.
"""
import json
import math
import time
import uuid

import peer_lifecycle


_VERSION = 1
_MAX_UTC_SECONDS = 253402300799  # 9999-12-31T23:59:59Z
_FIELDS = {'claim_issued': 'claim_issued_at', 'accepted': 'accepted_at', 'started': 'started_at',
           'checked': 'last_checked_at', 'ended': 'ended_at'}
_CLAUDE_ATTEMPT = ('mod_enrollment', 'mod_generation', 'mod_attempt_id')
_EXECUTION = ('reserved', 'submitted', 'running', 'uncertain', *peer_lifecycle.TERMINAL)
_CAPTURE = ('pending', 'captured', 'unusable', 'withheld')


class ProgressError(ValueError):
    """Progress cannot be safely read or added to this execution."""


class ProgressClockError(ProgressError):
    """The observation clock is invalid or moved backward; no progress was written."""


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _timestamp(value):
    return type(value) in (int, float) and 0 < value <= _MAX_UTC_SECONDS and math.isfinite(value)


def _turn(value):
    if not isinstance(value, str) or not 0 < len(value) <= 128 or any(ord(char) < 32 for char in value):
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ProgressError('Peer progress evidence contains duplicate fields.')
    return result


def _evidence(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, TypeError, RecursionError):
        raise ProgressError('Peer progress evidence is invalid.') from None
    if not isinstance(value, dict):
        raise ProgressError('Peer progress evidence is invalid.')
    return value


def _snapshot(db, transport_id):
    if not _uuid(transport_id):
        raise ProgressError('Invalid peer transport identifier.')
    admission = peer_lifecycle.exact_admission(db, transport_id)
    if admission is None or admission[3] != 2:
        raise ProgressError('Progress requires one exact shared v2 peer admission.')
    agent, created, launched, _ = admission
    row = db.execute('SELECT agent,created,execution,capture,evidence FROM peer_execution WHERE request_id=?',
                     (transport_id,)).fetchone()
    if (row is None or row[:2] != (agent, created) or not isinstance(agent, str)
            or agent[:2] not in ('c:', 'x:') or not _uuid(agent[2:]) or not _timestamp(created)
            or (launched is not None and not _timestamp(launched))):
        raise ProgressError('Progress requires a durable execution matching the immutable peer admission.')
    execution, capture, raw = row[2:]
    if execution not in _EXECUTION or capture not in _CAPTURE:
        raise ProgressError('Peer progress requires recognized durable lifecycle states.')
    evidence = _evidence(raw)
    if 'progress' in evidence:
        progress = evidence['progress']
        if (not isinstance(progress, dict) or set(progress) != {'version', *_FIELDS.values()}
                or type(progress['version']) is not int or progress['version'] != _VERSION):
            raise ProgressError('Unexpected peer progress schema.')
        populated = [value for key, value in progress.items() if key != 'version' and value is not None]
        lower_bound = max(created, launched) if launched is not None else created
        if (any(not _timestamp(value) or value < lower_bound for value in populated)
                or (populated and (launched is None or evidence.get('launched') is not True))
                or (progress['ended_at'] is not None and execution != 'completed')
                or (progress['claim_issued_at'] is not None and not agent.startswith('c:'))):
            raise ProgressError('Peer progress conflicts with its durable execution.')
        for observation, field in _FIELDS.items():
            if progress[field] is not None:
                keys = _binding_fields(agent, observation, evidence)
                _check_binding(keys, evidence, {key: evidence.get(key) for key in keys})
    else:
        progress = dict.fromkeys(_FIELDS.values()) | {'version': _VERSION}
    return agent, created, launched, execution, capture, raw, evidence, progress


def load(db, transport_id):
    """Read one exact shared v2 peer's versioned facts without writing."""
    return dict(_snapshot(db, transport_id)[-1])


def _require_transaction(db):
    if not db.in_transaction:
        raise ProgressError('A caller-owned transaction is required for peer progress writes.')


def _write(db, transport_id, snapshot, progress):
    agent, created, _, execution, capture, raw, evidence, _ = snapshot
    try:
        updated = json.dumps(dict(evidence, progress=progress), sort_keys=True, allow_nan=False)
    except (ValueError, RecursionError):
        raise ProgressError('Peer progress cannot preserve the existing evidence.') from None
    result = db.execute('''UPDATE peer_execution SET evidence=? WHERE request_id=? AND agent=?
                           AND created=? AND execution=? AND capture=? AND evidence=?''',
                        (updated, transport_id, agent, created, execution, capture, raw))
    if result.rowcount != 1:
        raise ProgressError('Peer execution changed before progress could be written.')


def migrate(db, transport_id):
    """Initialize one exact row idempotently without inventing historical dates."""
    _require_transaction(db)
    snapshot = _snapshot(db, transport_id)
    evidence, progress = snapshot[-2:]
    if 'progress' not in evidence:
        _write(db, transport_id, snapshot, progress)
    return dict(progress)


def _binding_fields(agent, observation, evidence):
    preturn = observation == 'accepted'
    if agent.startswith('c:'):
        keys = _CLAUDE_ATTEMPT
        if observation != 'claim_issued' and (not preturn or evidence.get('mod_turn') not in ('', None)):
            keys += ('mod_turn',)
    else:
        if observation == 'claim_issued':
            raise ProgressError('Only Claude has a dispatch claim observation.')
        keys = () if preturn and evidence.get('bound_turn') in ('', None) else ('bound_turn',)
    return keys


def _check_binding(keys, evidence, binding):
    if (not isinstance(binding, dict) or set(binding) != set(keys)
            or any(not (_uuid(binding[key]) if key in _CLAUDE_ATTEMPT else _turn(binding[key]))
                   or evidence.get(key) != binding[key] for key in keys)):
        raise ProgressError('Progress does not match the original durable provider binding.')


def record(db, transport_id, observation, *, agent, created, binding, fresh, clock=time.time):
    """Record a newly verified fact. Supply a server clock, never provider time.

    For a pre-turn Codex accept, binding is empty. A Claude claim or pre-turn
    accept supplies its original enrollment, generation and attempt. Bound
    observations also supply the exact native turn. An accept requires its own
    verified source observation. Claim issuance never fills accepted_at.
    """
    _require_transaction(db)
    if fresh is not True or not isinstance(observation, str) or observation not in _FIELDS:
        raise ProgressError('A new verified provider observation is required.')
    snapshot = _snapshot(db, transport_id)
    target, admission_created, launched, execution, _, _, evidence, progress = snapshot
    if not _timestamp(created) or (agent, created) != (target, admission_created):
        raise ProgressError('Observation identity does not match the immutable peer admission.')
    if launched is None or evidence.get('launched') is not True or execution in ('reserved', 'dropped', 'retired'):
        raise ProgressError('Progress requires a dispatched execution eligible for this observation.')
    if observation == 'ended' and execution != 'completed':
        raise ProgressError('An end observation requires exact durable completion.')
    _check_binding(_binding_fields(target, observation, evidence), evidence, binding)
    field = _FIELDS[observation]
    if field != 'last_checked_at' and progress[field] is not None:
        return dict(progress)
    if observation == 'claim_issued' and (execution != 'submitted' or evidence.get('mod_turn')):
        raise ProgressError('A new claim observation requires a submitted attempt without a native turn.')
    try:
        now = clock()
    except Exception:
        raise ProgressClockError('Peer progress observation clock is unavailable.') from None
    lower_bound = max([admission_created, launched] + [value for key, value in progress.items()
                                                      if key != 'version' and value is not None])
    if not _timestamp(now) or now < lower_bound:
        raise ProgressClockError('Peer progress observation clock is invalid or moved backward.')
    if progress[field] == now:
        return dict(progress)
    updated = dict(progress, **{field: now})
    _write(db, transport_id, snapshot, updated)
    return updated
