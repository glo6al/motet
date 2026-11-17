"""Derived bounded-access directory for the offline owner attachment ledger.

No schema, provider authority, production construction, or worker is added here.
An explicit maintenance caller drives keyset rebuild steps; it must never rebuild
inside a hot status/admission transaction. Exact records remain authoritative.
"""
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass
import threading

from attachments_owner_transport import LedgerError, NAMESPACES, MAX_LINKS, fail, integer

BATCH_SIZE = 64
KEY_BYTES = 36


@dataclass(frozen=True)
class DirectoryEntry:
    owner_id: str
    agent_id: str
    attempt_id: str
    turn_id: str
    claimed: bool
    payload_present: bool
    execution_unresolved: bool
    terminal_acked: bool
    capacity_pending: bool
    pending_ack: bool
    created_ms: int
    quarantined: bool = False


class _State:
    def __init__(self):
        self.entries = {}
        self.pending = []
        self.times = []
        self.payloads = self.capacity = self.unknown_recent = self.quarantined = 0

    def put(self, entry):
        old = self.entries.get(entry.owner_id)
        if old is not None:
            self.payloads -= old.payload_present
            self.capacity -= old.capacity_pending
            self.quarantined -= old.quarantined
            if old.quarantined:
                self.unknown_recent -= 1
            else:
                self.times.pop(bisect_left(self.times, (old.created_ms, old.owner_id)))
                if old.pending_ack:
                    self.pending.pop(bisect_left(self.pending, old.owner_id))
        self.entries[entry.owner_id] = entry
        self.payloads += entry.payload_present
        self.capacity += entry.capacity_pending
        self.quarantined += entry.quarantined
        if entry.quarantined:
            self.unknown_recent += 1
        else:
            insort(self.times, (entry.created_ms, entry.owner_id))
            if entry.pending_ack:
                insort(self.pending, entry.owner_id)


class OwnerAttachmentDirectory:
    def __init__(self, ledger):
        self.ledger = ledger
        self._state = _State()
        self._epoch = None
        self._building = None
        self._build_epoch = None
        self._cursor = None
        self.ready = False

    def epoch(self):
        # data_version is meaningful only on this same connection. total_changes
        # detects out-of-band DML on this connection, including rolled-back DML;
        # schema_version also catches same-connection main-schema replacement.
        db = self.ledger.db
        epoch = (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes,
                 db.execute('PRAGMA schema_version').fetchone()[0])
        if self.ledger.maintenance_only:
            return epoch + (db.execute('PRAGMA temp.schema_version').fetchone()[0],)
        return epoch

    def matches(self, epoch):
        return self.ready and self._epoch == epoch

    def invalidate(self):
        self.ready = False

    def discard_pass(self):
        self._building, self._build_epoch, self._cursor = None, None, None

    def abandon(self):
        self.invalidate()
        self.discard_pass()

    @property
    def building(self):
        return self._building is not None

    def rebase_ordinary(self, before, after):
        self._require_commit_boundary()
        if before[0] != after[0] or before[2:] != after[2:]:
            self.abandon()
            return
        if self.matches(before):
            self._epoch = after
        else:
            self.invalidate()
        if self.building:
            if self._build_epoch == before:
                self._build_epoch = after
            else:
                self.discard_pass()

    def require_current(self):
        # The token is set only AFTER the ledger acquires BEGIN IMMEDIATE.
        if (not self.ledger._writer_active or self.ledger._writer_thread != threading.get_ident()
                or not self.ledger.db.in_transaction):
            fail('writer_transaction_required')
        if not self.matches(self.epoch()):
            self.invalidate()
            fail('attachment_index_unavailable')

    @staticmethod
    def project(owner_id, link, outcome, payload_present):
        if link['payload_pruned']:
            if payload_present or not link['terminal_acked'] or link['reply_available']:
                fail('corrupt_prune')
        elif not payload_present:
            fail('payload_missing')
        return DirectoryEntry(owner_id, link['identity']['agent_id'], link['attempt_id'], link['turn_id'],
                              link['claim'] == 'claimed', payload_present,
                              not (outcome['execution_ended'] or outcome['not_submitted']),
                              link['terminal_acked'], not link['terminal_acked'],
                              link['ack_seq'] != outcome['seq'], link['created_ms'])

    @staticmethod
    def _quarantined(owner_id):
        # Missing/corrupt identity grants no recipient authority. Charge both
        # retained history and capacity until the exact record can be verified.
        return DirectoryEntry(owner_id, '', '', '', True, True, True, False, True, True, 0, True)

    def quarantine(self, owner_id):
        self.require_current()
        self._state.put(self._quarantined(owner_id))

    def _require_commit_boundary(self):
        if (not self.ledger._writer_active or self.ledger._writer_thread != threading.get_ident()
                or self.ledger.db.in_transaction):
            fail('commit_boundary_required')

    def _committed(self, updates, epoch):
        # A concurrent external commit invalidates this epoch on its next use.
        self._require_commit_boundary()
        for entry in updates.values():
            self._state.put(entry)
        self._epoch = epoch
        self.ready = True

    def counts(self, now_ms):
        integer(now_ms)
        self.require_current()
        state = self._state
        recent = len(state.times) - bisect_right(state.times, (now_ms - 60_000, '\U0010ffff'))
        return len(state.entries), state.payloads, state.capacity, recent + state.unknown_recent

    def pending_ids(self, limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            fail('invalid_limit')
        self.require_current()
        return tuple(self._state.pending[:limit])

    def entry(self, owner_id):
        self.require_current()
        return self._state.entries.get(owner_id)

    def _validate_key_schema(self):
        # UNION/dict can represent one entry per ID only when each authoritative
        # table enforces that uniqueness. Do not repair or adopt a malformed
        # table: preserve its records and leave aggregate access unavailable.
        for table in ('owner_attachment_links', 'owner_attachment_outcomes'):
            # table_info omits a column's default collation. Checking only the
            # PK index is also insufficient: a NOCASE column can have a BINARY
            # PK, while UNION/WHERE still use NOCASE. Accept only our canonical
            # declaration, plus the actual BINARY PK metadata. Fetch a bounded
            # schema prefix; incompatible schemas are held, never rewritten.
            declaration = self.ledger.db.execute(
                "SELECT substr(sql,1,513) FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone()
            expected = ('CREATE TABLE ' + table + ' (owner_request_id TEXT PRIMARY KEY, '
                        'record BLOB NOT NULL CHECK(length(record)=4096))')
            canonical = (declaration is not None and isinstance(declaration[0], str)
                         and len(declaration[0]) <= 512
                         and ''.join(declaration[0].split()).upper() == ''.join(expected.split()).upper())
            key = self.ledger.db.execute(
                "SELECT upper(type)='TEXT',pk FROM pragma_table_info(?) WHERE name='owner_request_id'", (table,)).fetchall()
            primary_columns = self.ledger.db.execute(
                'SELECT count(*) FROM pragma_table_info(?) WHERE pk>0', (table,)).fetchone()[0]
            key_index = self.ledger.db.execute("""
                SELECT c.name,c.coll FROM pragma_index_list(?) i
                JOIN pragma_index_xinfo(i.name) c
                WHERE i.origin='pk' AND i."unique"=1 AND i.partial=0 AND c.key=1""", (table,)).fetchall()
            if not canonical or key != [(1, 1)] or primary_columns != 1 or key_index != [('owner_request_id', 'BINARY')]:
                self._building = None
                self.invalidate()
                fail('directory_key_schema_unavailable')

    def begin_rebuild(self):
        with self.ledger._transaction():
            self._validate_key_schema()
            self._building = _State()
            self._build_epoch = self.epoch()
            self._cursor = None

    def rebuild_step(self):
        """One bounded batch; return True only when a coherent snapshot publishes.

        A concurrent change aborts this pass. A scheduler may start another pass
        at a later checkpoint, but this API never loops/retries behind the caller.
        """
        with self.ledger._transaction():
            if self._building is None:
                fail('directory_rebuild_not_started')
            current = self.epoch()
            if current != self._build_epoch:
                self._building = None
                if not self.matches(current):
                    self.invalidate()
                fail('directory_rebuild_changed')
            db = self.ledger.db
            if self._cursor is None:
                key_query = """
                    SELECT owner_request_id FROM owner_attachment_links
                    UNION SELECT owner_request_id FROM owner_attachment_outcomes
                    ORDER BY owner_request_id LIMIT ?"""
                parameters = (BATCH_SIZE,)
            else:
                key_query = """
                    SELECT owner_request_id FROM owner_attachment_links WHERE owner_request_id>?
                    UNION SELECT owner_request_id FROM owner_attachment_outcomes WHERE owner_request_id>?
                    ORDER BY owner_request_id LIMIT ?"""
                parameters = (self._cursor, self._cursor, BATCH_SIZE)
            # Inspect type and encoded length inside SQLite BEFORE returning any
            # key to Python. A sentinel cannot become a cursor/cache entry; even
            # a huge orphan TEXT/BLOB key is never transferred or retained here.
            ids = [r[0] for r in db.execute("""
                SELECT CASE WHEN typeof(owner_request_id)!='text' THEN NULL
                    WHEN length(CAST(owner_request_id AS BLOB))>? THEN NULL
                    ELSE owner_request_id END FROM (""" + key_query + ')',
                (KEY_BYTES,) + parameters)]
            # NULL is also possible in a legacy TEXT PRIMARY KEY and may repeat.
            # Unrepresentable identities cannot be charged once per ID safely.
            if any(owner_id is None for owner_id in ids):
                self._building = None
                self.invalidate()
                fail('directory_identity_unavailable')
            if len(self._building.entries) + len(ids) > MAX_LINKS:
                self._building = None
                self.invalidate()
                fail('directory_capacity')
            if ids:
                marks = ','.join('?' for _ in ids)
                links = dict(db.execute('SELECT owner_request_id,record FROM owner_attachment_links WHERE owner_request_id IN (' + marks + ')', ids))
                outcomes = dict(db.execute('SELECT owner_request_id,record FROM owner_attachment_outcomes WHERE owner_request_id IN (' + marks + ')', ids))
                payloads = {r[0] for r in db.execute('SELECT id FROM requests WHERE id IN (' + marks + ')', ids)}
                collisions = set()
                for table, column in NAMESPACES[1:]:
                    collisions.update(r[0] for r in db.execute('SELECT DISTINCT ' + column + ' FROM ' + table + ' WHERE ' + column + ' IN (' + marks + ')', ids))
                for owner_id in ids:
                    try:
                        if owner_id in collisions:
                            fail('namespace_conflict')
                        link, outcome = self.ledger.decode_pair(owner_id, links.get(owner_id), outcomes.get(owner_id))
                        entry = self.project(owner_id, link, outcome, owner_id in payloads)
                    except LedgerError:
                        entry = self._quarantined(owner_id)
                    self._building.put(entry)
                self._cursor = ids[-1]
            if len(ids) == BATCH_SIZE:
                return False
            self._state = self._building
            self._building = None
            self._epoch = current
            self.ready = True
            return True
