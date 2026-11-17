"""Explicit offline mirror; it never obtains reply text or dispatches a request."""
from dataclasses import dataclass
import hashlib
import json

from attachments_owner_transport import (
    Admission, FACTS, LINK_BYTES, OUTCOME_BYTES, TIMES, OwnerAttachmentLedger,
    canonical as owner_canonical, decode, digest, keys, padded,
)
from attachments_requests import (NATIVE_MIRROR_SCHEMA, NATIVE_MIRROR_VERSION, RequestLedger,
                                  _fixed, _native_mirror_layout)
from attachments_types import canonical, destination, fail, now

HEADER_BYTES = 4096
SLOT_BYTES = HEADER_BYTES + LINK_BYTES + OUTCOME_BYTES
EMPTY_PAIR = bytes(LINK_BYTES + OUTCOME_BYTES)
HEADER_KEYS = {'version', 'request_id', 'owner_id', 'epoch', 'fingerprint', 'frozen_digest'}
PROVENANCE_FIELDS = ('accepted_provenance', 'started_provenance', 'terminal_provenance')
FROZEN_FACTS = TIMES + PROVENANCE_FIELDS + ('reply_fingerprint', 'late_reply_fingerprint',
                                           'attempt_id', 'turn_id')


def _conflict():
    fail('mirror_conflict', 'Native attachment evidence no longer matches its retained identity.', 409)


def target_binding_digest(destination_record, profile_record):
    return hashlib.sha256(owner_canonical({'destination': destination_record, 'profile': profile_record})).hexdigest()


@dataclass(frozen=True)
class _ExpectedAdmission:
    admission: Admission
    identity: bytes
    digest: str

    def read_pair(self, pair):
        if (type(pair) is not tuple or len(pair) != 2
                or any(type(record) is not bytes for record in pair)):
            _conflict()
        link, outcome = OwnerAttachmentLedger.decode_pair(self.admission.owner_request_id, *pair)
        if (owner_canonical(link['identity']) != self.identity or link['admission_digest'] != self.digest
                or type(link['version']) is not int or type(outcome['version']) is not int
                or pair != (padded(link, LINK_BYTES), padded(outcome, OUTCOME_BYTES))
                or (link['payload_pruned'] and (not link['terminal_acked'] or link['reply_available']))):
            _conflict()
        return link, outcome


def _expected_admission(row, outbox):
    binding = _slot_identity(row, outbox)
    claim = outbox['claim_id']
    if row['authorized_at'] is None or not claim.strip('0') or outbox['claimed_at'] is None:
        _conflict()
    target = destination(json.loads(row['destination']))
    profile = json.loads(row['profile'])
    adapter = {'codex': 'codex', 'claude_official': 'claude'}.get(profile['adapter'])
    if (target['kind'] != 'agent' or adapter is None
            or target['capabilityRevision'] != profile['capability_revision']):
        _conflict()
    admission = Admission(binding['owner_id'], binding['request_id'], claim, binding['epoch'],
                          binding['fingerprint'], hashlib.sha256(row['envelope'].encode()).hexdigest(),
                          adapter, profile['id'], profile['revision'], target_binding_digest(target, profile),
                          target['id'], row['text'], row['envelope'])
    identity, _ = admission.freeze()
    return _ExpectedAdmission(admission, owner_canonical(identity),
                              digest('watch-owner-attachment-admission-v1', identity))


def _slot_identity(row, outbox):
    if outbox is None or (row['id'], row['owner_request_id']) != (outbox['request_id'], outbox['owner_request_id']):
        _conflict()
    frozen = {k: row[k] for k in ('destination', 'profile', 'text', 'envelope')}
    return dict(version=1, request_id=row['id'], owner_id=row['owner_request_id'], epoch=row['epoch'],
                fingerprint=row['fingerprint'], frozen_digest=digest('watch-native-mirror-frozen-v1', frozen))


def _ordering(pair, link, outcome, prior, old_link, old_outcome):
    if pair == prior:
        return 'duplicate'
    seq, old_seq = outcome['seq'], old_outcome['seq']
    rev, old_rev = link['revision'], old_link['revision']
    if (rev == old_rev or (seq == old_seq and outcome['digest'] != old_outcome['digest'])
            or (seq > old_seq and rev < old_rev) or (seq < old_seq and rev > old_rev)):
        _conflict()
    if rev < old_rev:
        return 'stale'
    if (any(old_outcome[key] and not outcome[key] for key in FACTS)
            or any(old_outcome[key] and old_outcome[key] != outcome[key] for key in FROZEN_FACTS)
            or any(old_link[key] and not link[key] for key in ('terminal_acked', 'payload_pruned'))
            or (old_link['claim'] == 'claimed' and link['claim'] != 'claimed')
            or link['created_ms'] != old_link['created_ms']
            or link['ack_seq'] < old_link['ack_seq']
            or (link['ack_seq'] == old_link['ack_seq'] and link['ack_digest'] != old_link['ack_digest'])
            or (old_link['reply_available'] and not link['reply_available']
                and not (link['payload_pruned'] or outcome['conflict']))
            or outcome['evidence_sequence'] < old_outcome['evidence_sequence']
            or (outcome['evidence_sequence'] == old_outcome['evidence_sequence']
                and outcome['evidence_fingerprint'] != old_outcome['evidence_fingerprint'])):
        _conflict()
    return 'applied'


class NativeOutcomeMirror:
    """Opt-in fixed attachment mirror on the owner's attachment store."""

    def __init__(self, requests):
        if type(requests) is not RequestLedger:
            _conflict()
        with requests.store._gate():
            with requests.store._transaction():
                self._install(requests.store)
            self._bind(requests)

    @staticmethod
    def _install(store):
        if not store.db.in_transaction:
            _conflict()
        if not _native_mirror_layout(store):
            store.db.execute(NATIVE_MIRROR_SCHEMA)
            store.db.execute('INSERT INTO meta VALUES (?,?)', (NATIVE_MIRROR_VERSION, '1'))

    @classmethod
    def _open_committed(cls, requests):
        instance = cls.__new__(cls)
        instance._bind(requests)
        return instance

    def _bind(self, requests):
        if type(requests) is not RequestLedger:
            _conflict()
        self.requests = requests
        self.store = requests.store
        self.db = self.store.db
        if self.db.in_transaction:
            _conflict()
        self._require_layout()

    def _require_layout(self):
        if (self.db is not self.store.db or not _native_mirror_layout(self.store)
                or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1):
            fail('mirror_unavailable', 'The native attachment mirror cannot be verified.', 503)
        if self.db.execute("""SELECT 1 FROM sqlite_temp_schema WHERE name COLLATE NOCASE IN
                            ('meta','native_outcome_mirror','attachment_requests','attachment_outbox') LIMIT 1""").fetchone():
            fail('mirror_unavailable', 'The native attachment mirror cannot be verified.', 503)

    def _rows(self, request_id):
        self._require_layout()
        row = self.requests._request(request_id)
        if row['epoch'] != self.store.owner_epoch:
            _conflict()
        outbox = self.db.execute('SELECT * FROM attachment_outbox WHERE request_id=?', (request_id,)).fetchone()
        expected_header = _slot_identity(row, outbox)
        raw = self.db.execute('SELECT slot FROM native_outcome_mirror WHERE request_id=?', (request_id,)).fetchone()
        return row, outbox, expected_header, raw[0] if raw is not None else None

    @staticmethod
    def _slot(raw, header):
        if type(raw) is not bytes or len(raw) != SLOT_BYTES:
            _conflict()
        value = decode(raw[:HEADER_BYTES], HEADER_BYTES)
        keys(value, HEADER_KEYS)
        encoded = padded(header, HEADER_BYTES)
        if raw[:HEADER_BYTES] != encoded:
            _conflict()
        if raw[HEADER_BYTES:] == EMPTY_PAIR:
            return encoded, None
        return encoded, (raw[HEADER_BYTES:HEADER_BYTES + LINK_BYTES], raw[-OUTCOME_BYTES:])

    def _state(self, row, outbox, header, raw):
        encoded, prior = self._slot(raw, header)
        if prior is None:
            if outbox['owner_sequence'] != 0 or outbox['receipt_digest'] != '0' * 64:
                _conflict()
            return encoded, None, None
        link, outcome = _expected_admission(row, outbox).read_pair(prior)
        if (outbox['owner_sequence'], outbox['receipt_digest']) != (outcome['seq'], outcome['digest']):
            _conflict()
        return encoded, prior, (link, outcome)

    def prepare_request(self, *args, **kwargs):
        """A failed allocation leaves the durable intent available for exact prepare replay."""
        prepared = self.requests.prepare_request(*args, **kwargs)
        request_id = prepared['id']
        with self.store._gate():
            row, outbox, header, raw = self._rows(request_id)
            self.requests._authorize(request_id, prepared['capability'], fingerprint=prepared['fingerprint'])
            if raw is not None:
                self._state(row, outbox, header, raw)
                return prepared
            if (row['stage'] != 'intent' or row['authorized_at'] is not None or outbox['claim_id'].strip('0')
                    or outbox['claimed_at'] is not None or outbox['owner_sequence'] != 0):
                _conflict()
            with self.store._transaction():
                self.db.execute('INSERT INTO native_outcome_mirror VALUES (?,?)',
                                (request_id, padded(header, HEADER_BYTES) + EMPTY_PAIR))
        return prepared

    def authorize_send(self, request_id, capability, expected_revision, fingerprint, **kwargs):
        with self.store._gate():
            self.requests._authorize(request_id, capability, fingerprint=fingerprint)
            row, outbox, header, raw = self._rows(request_id)
            self._state(row, outbox, header, raw)
        # A reserved slot cannot be removed by any supported writer.
        return self.requests.authorize_send(request_id, capability, expected_revision, fingerprint, **kwargs)

    def _expected(self, request_id):
        with self.store._gate():
            row, outbox, header, raw = self._rows(request_id)
            self._state(row, outbox, header, raw)
            return _expected_admission(row, outbox)

    def _as_of(self, request_id):
        """Private detached facts, with current availability only as of this pair."""
        with self.store._gate():
            row, outbox, header, raw = self._rows(request_id)
            _, _, facts = self._state(row, outbox, header, raw)
            return facts

    def _apply(self, request_id, pair):
        with self.store._gate(), self.store._transaction(reclaim=True):
            row, outbox, header, raw = self._rows(request_id)
            encoded, prior, facts = self._state(row, outbox, header, raw)
            expected = _expected_admission(row, outbox)
            link, outcome = expected.read_pair(pair)
            if prior is not None:
                ordering = _ordering(pair, link, outcome, prior, *facts)
                if ordering != 'applied':
                    return ordering
            terminal = outcome['execution_ended'] or outcome['not_submitted']
            owner_state = ('execution_ended' if link['payload_pruned'] and outcome['execution_ended']
                           else outcome['state'])
            stage = ('execution_ended' if outcome['execution_ended'] and not link['reply_available']
                     else owner_state)
            provenance = next((outcome[k] for k in reversed(PROVENANCE_FIELDS) if outcome[k]), '')
            self.db.execute('UPDATE native_outcome_mirror SET slot=? WHERE request_id=?',
                            (encoded + b''.join(pair), request_id))
            self.db.execute('''UPDATE attachment_outbox SET state=?,owner_sequence=?,receipt_digest=?,owner_state=?,
                               provenance=?,attempt_id=?,retention_pin=?,accepted=?,padding=substr(padding,1,256)
                               WHERE request_id=?''',
                            ('reconciled' if terminal else 'owner_recorded', outcome['seq'], outcome['digest'],
                             _fixed(owner_state, 24), _fixed(provenance, 48), _fixed(link['attempt_id'], 64),
                             int(not terminal), int(outcome['accepted']), request_id))
            self.db.execute('''UPDATE attachment_requests SET stage=?,revision=revision+1,updated=?,
                               padding=substr(padding,1,512) WHERE id=?''',
                            (stage, now(self.store.clock), request_id))
        return 'applied'

    def reconcile(self, request_id, owner, *, _acknowledge=None):
        """Mirror the exact owner snapshot, then acknowledge outside both store locks."""
        if (type(owner) is not OwnerAttachmentLedger or owner.db is self.db
                or owner.lock is self.store.lock):
            _conflict()
        owner._require_native_writer()
        expected = self._expected(request_id)
        owner_id = expected.admission.owner_request_id
        pair = owner._exact_snapshot(owner_id, expected.digest)
        _, outcome = expected.read_pair(pair)
        applied = self._apply(request_id, pair)
        if applied == 'stale':
            return applied, None
        acknowledge = owner.acknowledge if _acknowledge is None else _acknowledge
        ack = acknowledge(owner_id, expected.digest, outcome['seq'], outcome['digest'])
        return applied, ack['acknowledgment']
