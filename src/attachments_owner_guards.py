"""Protect installed native attachment IDs at the shared owner SQL boundary.

Guarded startup may install canonical protection in its own atomic transaction.
Existing protection is validated without repair; isolated fixtures also use it.
"""
from contextlib import contextmanager
import re

from attachments_owner_marker import CLEAR, MARKER, UNCERTAIN, lexical_payload_state, sqlite_marker_available, sqlite_payload_state


LINKS = 'owner_attachment_links'
OUTCOMES = 'owner_attachment_outcomes'
PERMIT = 'owner_attachment_payload_permit'
TRIGGER_PREFIX = 'owner_attachment_guard_v1_'
NEUTRAL = '0' * 36
DURABLE = 3

# table, protected columns, all declared unique keys (ROWID is checked separately)
SHARED_KEYS = (
    ('requests', ('id',), (('id',),)),
    ('controls', ('id',), (('id',),)),
    ('expired_controls', ('id',), (('id',),)),
    ('asks', ('id',), (('id',),)),
    ('request_fulfillments', ('request_id', 'control_request_id'), (('request_id',),)),
    ('peer_requests', ('transport_id',), (('sender', 'client_id'), ('transport_id',))),
    ('shared_bridge_requests', ('transport_id',), (('client_id',), ('transport_id',))),
    ('peer_execution', ('request_id',), (('request_id',),)),
    ('peer_execution_events', ('request_id',), (('event_id',),)),
    ('peer_reply_admissions', ('request_id',), (('request_id',),)),
)

# Match the actual shared owner declarations. Exact column affinity/collation
# and uniqueness matter to the BEFORE INSERT/UPDATE displaced-row probes.
SHARED_DECLARATIONS = {
    'requests': 'id TEXT PRIMARY KEY, data TEXT NOT NULL',
    'controls': 'id TEXT PRIMARY KEY, data TEXT NOT NULL',
    'expired_controls': 'id TEXT PRIMARY KEY',
    'asks': 'id TEXT PRIMARY KEY, user_text TEXT NOT NULL, created REAL NOT NULL, decision TEXT',
    'request_fulfillments': 'request_id TEXT PRIMARY KEY, control_request_id TEXT NOT NULL, confirmed_at REAL NOT NULL',
    'peer_requests': '''sender TEXT NOT NULL, client_id TEXT NOT NULL, target TEXT NOT NULL,
        payload_hash TEXT NOT NULL, transport_id TEXT NOT NULL UNIQUE,
        created REAL NOT NULL, launched REAL, PRIMARY KEY(sender,client_id)''',
    'shared_bridge_requests': '''client_id TEXT PRIMARY KEY, target TEXT NOT NULL, target_snapshot TEXT NOT NULL,
        payload_hash TEXT NOT NULL, transport_id TEXT NOT NULL UNIQUE,
        credential_id TEXT NOT NULL, created REAL NOT NULL, launched REAL, preview TEXT NOT NULL''',
    'peer_execution': '''request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, created REAL NOT NULL,
        execution TEXT NOT NULL, capture TEXT NOT NULL, evidence TEXT NOT NULL''',
    'peer_execution_events': '''event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL,
        reason TEXT NOT NULL, received REAL NOT NULL, occurred REAL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL, fingerprint TEXT NOT NULL''',
    'peer_reply_admissions': 'request_id TEXT PRIMARY KEY, args_sha256 TEXT NOT NULL',
}
RECORD = 'owner_request_id TEXT PRIMARY KEY, record BLOB NOT NULL CHECK(length(record)=4096)'
PERMIT_DECLARATION = '''slot INTEGER PRIMARY KEY CHECK(slot=1),
    owner_request_id TEXT NOT NULL CHECK(length(owner_request_id)=36),
    verb TEXT NOT NULL CHECK(verb IN ('-', 'I', 'U', 'D'))'''


class OwnerGuardError(ValueError):
    """Storage protection or the combined capacity proof is unavailable."""


def retained_id(column):
    """SQL expression over trusted, static qualified column names."""
    return (f'(EXISTS (SELECT 1 FROM {LINKS} WHERE owner_request_id={column}) '
            f'OR EXISTS (SELECT 1 FROM {OUTCOMES} WHERE owner_request_id={column}))')


def _protected(table, columns, alias):
    keys = ' OR '.join(retained_id(f'{alias}.{column}') for column in columns)
    if table == 'requests':
        return f'(CASE WHEN {keys} THEN 1 ELSE {sqlite_payload_state(f"{alias}.data")} <> {CLEAR} END)'
    return f'({keys})'


def _displaces(table, columns, keys, event):
    checks = ['existing.rowid=NEW.rowid']
    checks.extend('(' + ' AND '.join(f'existing.{col}=NEW.{col}' for col in key) + ')' for key in keys)
    conflict = '(' + ' OR '.join(checks) + ')'
    other = 'existing.rowid<>OLD.rowid AND ' if event == 'UPDATE' else ''
    return (f'EXISTS (SELECT 1 FROM {table} AS existing WHERE {other}{conflict} '
            f'AND {_protected(table, columns, "existing")})')


def _trigger_definitions():
    triggers = {}
    for table, columns, keys in SHARED_KEYS:
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = {'INSERT': ('NEW',), 'UPDATE': ('OLD', 'NEW'), 'DELETE': ('OLD',)}[event]
            protected = '(' + ' OR '.join(_protected(table, columns, side) for side in sides) + ')'
            if table == 'requests':
                side = sides[0]
                verb = event[0]
                same_key = 'OLD.rowid=NEW.rowid AND OLD.id=NEW.id AND ' if event == 'UPDATE' else ''
                permit = (f'({same_key}EXISTS (SELECT 1 FROM {PERMIT} WHERE slot=1 '
                          f"AND owner_request_id={side}.id AND verb='{verb}'))")
                protected = f'({protected} AND NOT {permit})'
            if event != 'DELETE':
                protected += ' OR ' + _displaces(table, columns, keys, event)
            name = f'{TRIGGER_PREFIX}{table}_{event.lower()}'
            triggers[name] = (f'CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {protected} '
                              "BEGIN SELECT RAISE(ROLLBACK, 'owner request storage protection conflict'); END")
    return triggers


def _normalized(sql):
    if not isinstance(sql, str):
        return ''
    # SQLite catalogue SQL drops IF NOT EXISTS. The accepted spelling otherwise
    # permits whitespace and case differences, never additional SQL clauses.
    parts = re.split(r"('(?:[^']|'')*')", sql)
    return ''.join(part if index % 2 else re.sub(r'\s+', '', part).lower()
                   for index, part in enumerate(parts))


def _catalogue(db):
    return dict(db.execute('SELECT name,sql FROM sqlite_master WHERE type=\'table\' OR '
                           "(type='trigger' AND substr(name,1,?)=?)",
                           (len(TRIGGER_PREFIX), TRIGGER_PREFIX)))


def _table(db, catalogue, name, declaration, unique_keys):
    if _normalized(catalogue.get(name)) != _normalized(f'CREATE TABLE {name} ({declaration})'):
        raise OwnerGuardError('Owner attachment protection schema is unavailable.')
    actual = []
    for _, index, unique, origin, partial in db.execute(f'PRAGMA index_list({name})'):
        if not unique:
            continue
        if partial or origin not in ('pk', 'u'):
            raise OwnerGuardError('Owner attachment protection uniqueness is unavailable.')
        parts = db.execute('SELECT cid,name,desc,coll FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno', (index,))
        key = []
        for cid, column, descending, collation in parts:
            if cid < 0 or descending or collation != 'BINARY':
                raise OwnerGuardError('Owner attachment protection uniqueness is unavailable.')
            key.append(column)
        actual.append(tuple(key))
    if sorted(actual) != sorted(unique_keys):
        raise OwnerGuardError('Owner attachment protection uniqueness is unavailable.')


def _validate_base_layout(db, catalogue):
    for table, _, keys in SHARED_KEYS:
        _table(db, catalogue, table, SHARED_DECLARATIONS[table], keys)
    if not sqlite_marker_available(db):
        raise OwnerGuardError('Owner attachment protection JSON checks are unavailable.')


def _validate_layout(db, catalogue):
    _validate_base_layout(db, catalogue)
    for table in (LINKS, OUTCOMES):
        _table(db, catalogue, table, RECORD, (('owner_request_id',),))


def _fallback_payload_state(column):
    return (f'(CASE WHEN typeof({column}) = \'text\' THEN '
            f'watch_owner_attachment_state(CAST({column} AS BLOB)) ELSE {UNCERTAIN} END)')


def bind_uninstalled_payload_state(db):
    if sqlite_marker_available(db):
        return sqlite_payload_state
    # Only MessagingStore's own connection and queries need this compatibility
    # scalar. SQLite marshals the existing bytes and no tree is built in Python.
    db.create_function('watch_owner_attachment_state', 1, lexical_payload_state)
    return _fallback_payload_state


def alias_state(column, payload_state):
    return (f'COALESCE((SELECT {payload_state("body.data")} FROM requests AS body '
            f'WHERE body.id={column}), {CLEAR})')


def check_uninstalled_peer_orphans(db, payload_state):
    # Legacy peer migration consults these rows before ordinary projection.
    tables = _catalogue(db)
    if 'requests' not in tables:
        return
    for table in ('peer_requests', 'shared_bridge_requests'):
        if table in tables and db.execute(
                f'SELECT 1 FROM {table} AS p WHERE {alias_state("p.transport_id", payload_state)} = {MARKER} LIMIT 1').fetchone():
            raise OwnerGuardError('An owner request needs recovery before peer migration.')


def install_for_test(db):
    """Install the L2b schema in an isolated owner fixture, under its writer."""
    if not db.in_transaction:
        raise OwnerGuardError('Owner attachment protection requires a writer.')
    _validate_layout(db, _catalogue(db))
    _install_protection(db)


def _install_protection(db):
    db.execute(f'CREATE TABLE {PERMIT} ({PERMIT_DECLARATION})')
    db.execute(f'INSERT INTO {PERMIT} VALUES (1,?,\'-\')', (NEUTRAL,))
    for statement in _trigger_definitions().values():
        db.execute(statement)


class OwnerAttachmentGuard:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock
        self.capacity = None
        self._schema_version = None

    @classmethod
    def probe(cls, db, *, lock=None):
        catalogue = _catalogue(db)
        if not ({LINKS, OUTCOMES, PERMIT} & catalogue.keys() or
                any(name.startswith(TRIGGER_PREFIX) for name in catalogue)):
            return None
        guard = cls(db, lock)
        guard.require_schema(catalogue)
        return guard

    @classmethod
    def install(cls, db, lock):
        if not db.in_transaction:
            raise OwnerGuardError('Owner attachment protection requires a writer.')
        if cls.probe(db, lock=lock) is not None:
            raise OwnerGuardError('Owner attachment protection is already installed.')
        cls(db, lock).require_unshadowed()
        _validate_base_layout(db, _catalogue(db))
        for table in (LINKS, OUTCOMES):
            db.execute(f'CREATE TABLE {table} ({RECORD})')
        _install_protection(db)
        return cls.probe(db, lock=lock)

    def require_schema(self, catalogue=None):
        version = self.db.execute('PRAGMA schema_version').fetchone()[0]
        if catalogue is None and version == self._schema_version:
            return
        catalogue = _catalogue(self.db) if catalogue is None else catalogue
        _validate_layout(self.db, catalogue)
        _table(self.db, catalogue, PERMIT, PERMIT_DECLARATION, ())
        triggers = {name: _normalized(sql) for name, sql in catalogue.items() if name.startswith(TRIGGER_PREFIX)}
        if triggers != {name: _normalized(sql) for name, sql in _trigger_definitions().items()}:
            raise OwnerGuardError('Owner attachment protection triggers are unavailable.')
        self.require_neutral()
        self._schema_version = version

    def require_unshadowed(self):
        names = (LINKS, OUTCOMES, PERMIT, 'peer_principals', *(table for table, _, _ in SHARED_KEYS))
        bind = ','.join('?' for _ in names)
        if self.db.execute(f"SELECT 1 FROM sqlite_temp_master WHERE type IN ('table','view') "
                           f'AND name COLLATE NOCASE IN ({bind}) LIMIT 1', names).fetchone():
            raise OwnerGuardError('Owner attachment protection names are shadowed.')

    def require_neutral(self):
        if self.db.execute(f'SELECT slot,owner_request_id,verb FROM {PERMIT}').fetchmany(2) != [(1, NEUTRAL, '-')]:
            raise OwnerGuardError('Owner attachment protection permit is unavailable.')

    def check_startup(self):
        # Legacy peer migration reads payloads before ordinary projection.
        for table in ('peer_requests', 'shared_bridge_requests'):
            proof = self.reservation_state('p.transport_id')
            if self.db.execute(f'SELECT 1 FROM {table} AS p WHERE {proof} IN ({MARKER}, {DURABLE}) LIMIT 1').fetchone():
                raise OwnerGuardError('An owner request needs recovery before peer migration.')

    def legacy_rows(self):
        self.require_schema()
        protected = _protected('requests', ('id',), 'requests')
        return self.db.execute(f'SELECT id,data FROM requests WHERE NOT {protected}').fetchall()

    @staticmethod
    def reservation_state(column):
        return (f'(CASE WHEN {retained_id(column)} THEN {DURABLE} '
                f'ELSE {alias_state(column, sqlite_payload_state)} END)')

    def reserved_request(self, column):
        return f'({self.reservation_state(column)} <> {CLEAR})'

    def reservation(self, request_id):
        return self.db.execute(f'SELECT {self.reservation_state("candidate.id")} '
                               'FROM (SELECT ? AS id) AS candidate', (request_id,)).fetchone()[0]

    def reserved(self, request_id):
        return self.reservation(request_id) != CLEAR

    @contextmanager
    def admission(self, now_ms):
        if self.capacity is None:
            raise OwnerGuardError('Owner attachment capacity is unavailable.')
        with self.capacity(now_ms) as counts:
            yield counts

    def write_payload(self, verb, owner_id, data=None):
        if not self.db.in_transaction or verb not in ('I', 'U', 'D'):
            raise OwnerGuardError('Owner attachment protection requires an exact payload write.')
        self.require_schema()
        permit = f'UPDATE {PERMIT} SET owner_request_id=?, verb=? WHERE slot=1 AND owner_request_id=? AND verb=?'
        if self.db.execute(permit, (owner_id, verb, NEUTRAL, '-')).rowcount != 1:
            raise OwnerGuardError('Owner attachment protection permit is unavailable.')
        if verb == 'I':
            changed = self.db.execute('INSERT INTO requests VALUES (?,?)', (owner_id, data))
        elif verb == 'U':
            changed = self.db.execute('UPDATE requests SET data=? WHERE id=?', (data, owner_id))
        else:
            changed = self.db.execute('DELETE FROM requests WHERE id=?', (owner_id,))
        if changed.rowcount != 1 or self.db.execute(permit, (NEUTRAL, '-', owner_id, verb)).rowcount != 1:
            raise OwnerGuardError('Owner attachment protection permit is unavailable.')
