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
from datetime import datetime, timedelta, timezone

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


def _iso(value):
    if value is None:
        return None
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)).isoformat(timespec='seconds')


def _when(value, now):
    stamp = _iso(value)
    if not _timestamp(now) or now < value:
        return stamp
    seconds = int(now - value)
    for size, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute'), (1, 'second')):
        if seconds >= size:
            count = seconds // size
            return f"{stamp} ({count} {unit}{'s' if count != 1 else ''} ago)"
    return stamp + ' (just now)'


def public_progress(db, transport_id, *, target, created, row, expired, now):
    """Project exact shared evidence into a bounded, metadata-only public view."""
    agent, admission_created, launched, execution, capture, _, evidence, dates = _snapshot(db, transport_id)
    provider = 'codex' if agent.startswith('x:') else 'claude'
    if (agent, admission_created, provider) != (target.get('agent_id'), created, target.get('provider')):
        raise ProgressError('Peer progress target does not match its admission.')
    if row is not None and row.get('identity_revision') != target.get('identity_revision'):
        raise ProgressError('Peer progress target revision does not match its transport.')
    turn = 'bound_turn' if provider == 'codex' else 'mod_turn'
    if execution in ('running', 'completed') or evidence.get(turn) not in (None, ''):
        keys = _binding_fields(agent, 'started', evidence)
        _check_binding(keys, evidence, {key: evidence.get(key) for key in keys})
        if row is not None and any(row.get(key) != evidence.get(key) for key in keys):
            raise ProgressError('Peer progress turn does not match its transport.')
    if execution in ('submitted', 'running', 'completed', 'retired') and (launched is None or evidence.get('launched') is not True):
        raise ProgressError('Peer progress requires the original dispatch evidence.')

    dates = {key: dates[key] for key in _FIELDS.values()}
    if provider == 'claude':
        dates['last_checked_at'] = None
    started = dates['started_at']
    ended = dates['ended_at']
    checked = dates['last_checked_at']
    reason = None
    if execution == 'retired':
        phase, summary = 'retired', 'The owner retired this request. The provider outcome is unknown.'
    elif execution == 'dropped':
        phase = 'not_sent'
        summary = 'Not sent.' if launched is None else 'The provider reported that the request did not start.'
    elif execution == 'completed':
        phase = 'ended'
        summary = f'End observed at {_when(ended, now)}.' if ended is not None else 'End recorded; end time unavailable.'
        if capture == 'withheld':
            summary += ' Reply withheld.'
        elif (expired or row is None or capture == 'unusable'
              or row.get('state') == 'unknown' and row.get('tracking_stopped')):
            summary += ' Reply unavailable.'
        elif capture == 'pending':
            summary += ' Reply verification is pending.'
    elif (expired or row is None) and execution != 'running':
        phase = 'unavailable'
        summary = ('The request is past the content retention window. No end was verified.' if expired else
                   'The request record is unavailable. No end was verified.')
    elif execution == 'uncertain' or row is not None and (row.get('state') in ('unknown', 'expired') or row.get('tracking_stopped')):
        phase = 'held'
        reason = 'Delivery or completion could not be verified. Recipient recovery is required.'
        summary = reason
    elif execution == 'running':
        phase = 'started'
        summary = (f'Start observed at {_when(started, now)}.' if started is not None else
                   'Start recorded; start time unavailable.')
        summary += ' No end observed yet.'
        if checked is not None:
            summary += f' Motet last read its log at {_when(checked, now)}.'
        if expired:
            summary += ' The request is past the content retention window.'
        elif row is None:
            summary += ' The request record is unavailable.'
    elif execution == 'submitted' and dates['accepted_at'] is not None:
        phase = 'accepted'
        summary = f'Native acceptance observed at {_when(dates["accepted_at"], now)}. Start not observed.'
    elif execution == 'submitted' and provider == 'claude':
        keys = _binding_fields(agent, 'claim_issued', evidence)
        _check_binding(keys, evidence, {key: row.get(key) for key in keys})
        phase = 'claim_issued'
        issued = dates['claim_issued_at']
        summary = (f'Claude claim issued at {_when(issued, now)}.' if issued is not None else 'Claude claim issued.')
        summary += ' Native submission unconfirmed.'
    elif execution == 'reserved' and launched is None:
        phase, summary = 'waiting_dispatch', 'Waiting for dispatch.'
    else:
        phase, summary = 'unavailable', 'Dispatch recorded. Native acceptance is unconfirmed.'
    return {'version': _VERSION, 'provider': provider, 'phase': phase,
            **{key: _iso(value) for key, value in dates.items()}, 'summary': summary, 'held_reason': reason}


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


def _store_observation(store, row, observation=None):
    """Optional metadata savepoint; callers supply already verified source facts.

    The store's normal namespace classifier remains authoritative. A progress
    failure is isolated from an outer lifecycle transaction where SQLite permits
    that; if SQLite aborted that entire transaction, never conceal the failure.
    """
    if (row.get('origin') != 'peer' or row.get('bridge_version') != 2
            or not isinstance(row.get('request_id'), str)):
        return False
    import sqlite3
    with store.lock:
        db = store.db
        outer = db.in_transaction
        try:
            if store._attachment_reserved(row['request_id']):
                return False
        except Exception:
            if outer and not db.in_transaction:
                raise
            return False  # Optional metadata cannot weaken an unavailable fence.
        point = 'peer_progress_' + uuid.uuid4().hex
        try:
            db.execute('SAVEPOINT ' + point)
        except sqlite3.Error:
            if outer and not db.in_transaction:
                raise
            return False
        try:
            current = _snapshot(db, row['request_id'])
            if (row.get('agent'), row.get('created')) != current[:2]:
                raise ProgressError('The progress caller does not match the exact admission.')
            if observation is None:
                migrate(db, row['request_id'])
            else:
                keys = _binding_fields(current[0], observation, current[-2])
                binding = {key: row.get(key) for key in keys}
                record(db, row['request_id'], observation, agent=row['agent'], created=row['created'],
                       binding=binding, fresh=True,
                       clock=getattr(store, '_peer_progress_clock', None) or time.time)
            db.execute('RELEASE SAVEPOINT ' + point)
            return True
        except (ProgressError, sqlite3.Error):
            if not db.in_transaction:
                if outer:
                    raise  # The surrounding lifecycle commit must not claim success.
                return False
            db.execute('ROLLBACK TO SAVEPOINT ' + point)
            db.execute('RELEASE SAVEPOINT ' + point)
            return False
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK TO SAVEPOINT ' + point)
                db.execute('RELEASE SAVEPOINT ' + point)
            raise


def ensure(store, row):
    """Initialize nulls only, within the guarded store write boundary."""
    return _store_observation(store, row)


def observe(store, row, observation):
    """Called only by fresh verified provider callbacks, never reads or replay."""
    if observation not in _FIELDS:
        return False
    return _store_observation(store, row, observation)
