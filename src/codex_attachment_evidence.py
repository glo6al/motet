"""Offline candidate evidence from an exact, bounded Codex transcript snapshot."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import stat
import unicodedata
from urllib.parse import quote
import uuid

import codex_transcript


MAX_INDEX = MAX_SOURCE = 16 * 1024 * 1024
MAX_CHUNK = 2 * 1024 * 1024
MAX_LINE, MAX_LINES, MAX_TURNS = 1_000_000, 16_384, 256
MAX_ATTEMPTS, MAX_MAP_BYTES = 32, 2 * 1024 * 1024
POLICY, MAP_SCHEMA_VERSION = 1, 2


class EvidenceUnavailable(ValueError):
    """Fixed source/map fault. No native data is included in the error."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Conflict(EvidenceUnavailable):
    """A stable source contradiction that permanently withholds the attempt."""


def _require(condition, code):
    if not condition:
        raise EvidenceUnavailable(code)


def _number(value, low=0, high=2**63 - 1):
    return type(value) is int and low <= value <= high


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _text(value, scalars, limit):
    if type(value) is not str or len(value) > scalars:
        return False
    try:
        return len(value.encode('utf-8', 'strict')) <= limit
    except UnicodeError:
        return False


def _path(value):
    return (_text(value, 1024, 1024) and '\x00' not in value
            and os.path.isabs(value) and value == os.path.abspath(value))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _proof_hash(domain, value):
    return _sha(_canonical([domain, POLICY, value]).encode('ascii'))


@dataclass(frozen=True)
class PathPin:
    path: str
    device: int
    inode: int
    uid: int

    def __post_init__(self):
        _require(_path(self.path) and _number(self.device) and _number(self.inode, 1) and _number(self.uid), 'invalid_pin')


@dataclass(frozen=True)
class AttemptPin:
    attempt_id: str
    owner_request_id: str
    thread_id: str
    admission_digest: str
    target_binding_digest: str
    envelope_sha256: str
    native_root: PathPin
    sessions: PathPin
    index: PathPin
    rollout: PathPin
    project: PathPin

    def __post_init__(self):
        _require(all(_uuid(v) for v in (self.attempt_id, self.owner_request_id, self.thread_id))
                 and all(_digest(v) for v in (self.admission_digest, self.target_binding_digest, self.envelope_sha256))
                 and all(type(v) is PathPin for v in (self.native_root, self.sessions, self.index, self.rollout, self.project)),
                 'invalid_pin')
        _require(self.sessions.path == os.path.join(self.native_root.path, 'sessions')
                 and self.index.path == os.path.join(self.native_root.path, 'state_5.sqlite')
                 and self.rollout.path.startswith(self.sessions.path + os.sep), 'invalid_pin')


@dataclass(frozen=True)
class Candidate:
    ordinal: int
    kind: str
    turn_id: str
    start: int
    end: int
    line: int
    raw_sha256: str
    semantic_sha256: str
    proof_sha256: str
    reply_sha256: str | None


@dataclass(frozen=True)
class Observation:
    status: str
    reason: str | None = None
    candidate: Candidate | None = None


def _identity(st):
    return st.st_dev, st.st_ino, st.st_uid


def _stamp(st):
    return (*_identity(st), st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _match_pin(pin, st):
    if _identity(st) != (pin.device, pin.inode, pin.uid):
        raise _Conflict('source_identity_changed')


@contextmanager
def _open_directory(path):
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (part for part in path.split(os.path.sep) if part):
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _directory(pin):
    with _open_directory(pin.path) as fd:
        _match_pin(pin, os.fstat(fd))


def _check_file(root, pin, expected):
    source, st = codex_transcript.open_regular(root.path, pin.path)
    with source:
        _match_pin(pin, st)
        _require(_stamp(st) == expected, 'source_changed_during_read')


def _snapshot(root, pin, limit, chunk):
    source, st = codex_transcript.open_regular(root.path, pin.path)
    with source:
        _match_pin(pin, st)
        _require(st.st_size <= limit, 'source_byte_limit')
        expected = _stamp(st)
        data = bytearray()
        while len(data) < st.st_size:
            part = source.read(min(chunk, st.st_size - len(data)))
            _require(bool(part), 'source_changed_during_read')
            data.extend(part)
        _require(_stamp(os.fstat(source.fileno())) == expected, 'source_changed_during_read')
        _check_file(root, pin, expected)
    return bytes(data), expected


def _no_index_sides(path):
    _require(not any(os.path.lexists(path + suffix) for suffix in ('-wal', '-shm', '-journal')), 'index_not_clean')


def _selected_index(raw, thread_id):
    _require(len(raw) >= 100 and raw[:16] == b'SQLite format 3\0' and raw[18:20] == b'\x01\x01', 'index_not_clean')
    db = sqlite3.connect(':memory:')
    try:
        _require(hasattr(db, 'deserialize'), 'index_reader_unavailable')
        db.deserialize(raw)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: 1, 100_000)
        info = db.execute("PRAGMA table_list('threads')").fetchall()
        _require(len(info) == 1 and info[0][:3] == ('main', 'threads', 'table'), 'index_schema')
        columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
        fields = ['id', 'rollout_path', 'cwd', 'archived', 'source']
        _require(set(fields) <= columns, 'index_schema')
        fields.extend(v for v in ('agent_path', 'thread_source', 'originator', 'attached_thread_parent_id') if v in columns)
        query = 'SELECT ' + ','.join(fields) + ' FROM main.threads WHERE id=? COLLATE BINARY LIMIT 2'
        plans = db.execute('EXPLAIN QUERY PLAN ' + query, (thread_id,)).fetchall()
        _require(any('SEARCH ' in r[3] and '(id=?)' in r[3] for r in plans), 'index_schema')
        rows = db.execute(query, (thread_id,)).fetchall()
        _require(len(rows) == 1 and rows[0][0] == thread_id, 'index_cardinality')
        entry = dict(zip(fields, rows[0]))
        path = codex_transcript.selected(entry)
        _require(_path(path) and _path(entry['cwd']), 'index_identity')
        return path, entry['cwd']
    finally:
        db.close()


def _pairs(items):
    value = {}
    for key, item in items:
        _require(key not in value, 'invalid_record')
        value[key] = item
    return value


def _float(value):
    result = float(value)
    _require(math.isfinite(result), 'invalid_record')
    return result


def _invalid_constant(_value):
    raise EvidenceUnavailable('invalid_record')


def _strict(raw):
    try:
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                           parse_constant=_invalid_constant, parse_float=_float)
        # JSON escapes can decode to lone surrogates even when the wire is UTF-8.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
        _require(type(value) is dict, 'invalid_record')
        return value
    except (UnicodeError, ValueError, RecursionError, TypeError, OverflowError):
        raise EvidenceUnavailable('invalid_record') from None


@dataclass(frozen=True)
class _Source:
    snapshot: bytes
    index_stamp: tuple
    rollout_stamp: tuple

    def recheck(self, pin):
        for directory in (pin.native_root, pin.sessions, pin.project):
            _directory(directory)
        _no_index_sides(pin.index.path)
        _check_file(pin.native_root, pin.index, self.index_stamp)
        _check_file(pin.sessions, pin.rollout, self.rollout_stamp)
        _no_index_sides(pin.index.path)


def _read_source(pin, chunk, checkpoint):
    for directory in (pin.native_root, pin.sessions, pin.project):
        _directory(directory)
    _no_index_sides(pin.index.path)
    index, index_stamp = _snapshot(pin.native_root, pin.index, MAX_INDEX, chunk)
    _no_index_sides(pin.index.path)
    pointer, project = _selected_index(index, pin.thread_id)
    if (pointer, project) != (pin.rollout.path, pin.project.path):
        _check_file(pin.native_root, pin.index, index_stamp)
        _no_index_sides(pin.index.path)
        raise _Conflict('index_selection_changed')
    checkpoint('after_index_read')
    data, roll_stamp = _snapshot(pin.sessions, pin.rollout, MAX_SOURCE, chunk)
    return _Source(data, index_stamp, roll_stamp)


def _header_hash(data, pin):
    first = data.find(b'\n') + 1
    _require(0 < first <= codex_transcript.MAX_HEADER, 'invalid_header')
    raw_header = data[:first]
    _strict(raw_header)
    meta = codex_transcript.header(io.BytesIO(raw_header), pin.thread_id, pin.project.path)
    _require('history_base' not in meta, 'continuation_unavailable')
    return _sha(raw_header)


def _positions(raw, offset=0, line=0):
    while offset < len(raw):
        stop = raw.find(b'\n', offset)
        if stop < 0:
            _require(len(raw) - offset <= MAX_LINE, 'source_line_limit')
            return
        end = stop + 1
        line += 1
        _require(end - offset <= MAX_LINE and line <= MAX_LINES, 'source_line_limit')
        yield offset, end, line
        offset = end


def _turn_id(value):
    return (_text(value, 128, 512) and bool(value)
            and not any(unicodedata.category(c).startswith('C') for c in value))


def _input_turn(payload):
    nested = payload.get('internal_chat_message_metadata_passthrough', {})
    _require(type(nested) is dict and ('internal_chat_message_metadata_passthrough' not in payload or set(nested) == {'turn_id'}),
             'invalid_turn')
    values = [item['turn_id'] for item in (payload, nested) if 'turn_id' in item]
    _require(bool(values) and all(_turn_id(v) for v in values) and len(set(values)) == 1, 'invalid_turn')
    return values[0]


def _record(raw):
    value = _strict(raw)
    _require(set(value) in ({'type', 'payload'}, {'type', 'payload', 'timestamp'})
             and ('timestamp' not in value or _text(value['timestamp'], 128, 512)), 'invalid_record')
    p = value['payload']
    _require(type(p) is dict, 'invalid_record')
    typ, kind = value['type'], p.get('type')
    if typ == 'event_msg' and kind in ('task_started', 'task_complete'):
        allowed = {'type', 'turn_id'} | ({'last_agent_message'} if kind == 'task_complete' else set())
        _require(set(p) <= allowed and _turn_id(p.get('turn_id'))
                 and ('last_agent_message' not in p or type(p['last_agent_message']) is str), 'invalid_terminal')
        return kind, p['turn_id'], p.get('last_agent_message')
    if typ == 'response_item' and kind == 'message' and p.get('role') == 'user':
        _require(set(p) <= {'type', 'role', 'content', 'turn_id', 'internal_chat_message_metadata_passthrough'}, 'opaque_input')
        turn = _input_turn(p)
        body = codex_transcript.pure_text(p.get('content'), 'user')
        _require(type(body) is str and bool(body), 'opaque_input')
        return 'input', turn, body
    raise EvidenceUnavailable('unsupported_record')


def _reply(value, owner_id):
    if not value:
        return None
    lines = value.split('\n')
    expected = 'WATCH_REPLY ' + owner_id
    if [line for line in lines if 'WATCH_REPLY' in line] != [expected]:
        return None
    body = '\n'.join(line for line in lines if line != expected)
    return body if body.strip() and _text(body, 16_000, 64_000) else None


def _classify(pin, raw, floor_end, floor_line):
    turns, matching = {}, None
    for position in _positions(raw, floor_end, floor_line):
        start, end, _ = position
        record = raw[start:end]
        kind, turn, body = _record(record)
        if turn not in turns:
            _require(len(turns) < MAX_TURNS, 'turn_capacity')
            turns[turn] = {'task_started': [], 'input': [], 'task_complete': []}
        rows = turns[turn][kind]
        proof = (*position, _sha(record))
        # Counts suffice for ambiguity. Keep only the two first positions per kind.
        if len(rows) < 2:
            rows.append((proof, body if kind == 'task_complete' else None))
        if kind == 'input' and _sha(body.encode('utf-8')) == pin.envelope_sha256:
            _require(body.split('\n', 1)[0] == 'WATCH_REQUEST ' + pin.owner_request_id and matching is None,
                     'ambiguous_input')
            matching = turn
    if matching is None:
        return None, ''
    rows = turns[matching]
    starts, inputs, terminals = rows['task_started'], rows['input'], rows['task_complete']
    _require(len(starts) == len(inputs) == 1 and len(terminals) <= 1, 'ambiguous_turn')
    start, user = starts[0][0], inputs[0][0]
    _require(start[1] <= user[0], 'invalid_turn_order')
    if not terminals:
        return None, ''
    terminal, reply_body = terminals[0]
    _require(user[1] <= terminal[0], 'invalid_turn_order')
    answer = _reply(reply_body, pin.owner_request_id)
    reply_hash = _sha(answer.encode('utf-8')) if answer is not None else None
    kind = 'reply' if reply_hash is not None else 'ended'
    semantic = _semantic_hash(pin, kind, matching, reply_hash)
    proof_hash = _proof_hash('watch-codex-turn-spans', [start, user, terminal])
    return Candidate(1, kind, matching, *terminal, semantic, proof_hash, reply_hash), answer or ''


_SCHEMA = {
    'installation': '''CREATE TABLE installation (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL,
        schema_version INTEGER NOT NULL, policy INTEGER NOT NULL)''',
    'attempts': '''CREATE TABLE attempts (
        id TEXT PRIMARY KEY NOT NULL, pin TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
        withheld INTEGER NOT NULL CHECK(withheld IN (0,1)), emitted INTEGER NOT NULL CHECK(emitted IN (0,1)),
        armed INTEGER NOT NULL CHECK(armed IN (0,1)))''',
    'frontiers': '''CREATE TABLE frontiers (
        attempt TEXT PRIMARY KEY NOT NULL REFERENCES attempts(id), header_hash TEXT NOT NULL,
        floor_end INTEGER NOT NULL CHECK(floor_end>0), floor_line INTEGER NOT NULL CHECK(floor_line>0),
        floor_hash TEXT NOT NULL, verified_end INTEGER NOT NULL CHECK(verified_end>=floor_end),
        verified_line INTEGER NOT NULL CHECK(verified_line>=floor_line), verified_hash TEXT NOT NULL,
        source_stamp TEXT NOT NULL)''',
    'emissions': '''CREATE TABLE emissions (
        attempt TEXT NOT NULL REFERENCES attempts(id), ordinal INTEGER NOT NULL CHECK(ordinal=1),
        segment INTEGER NOT NULL CHECK(segment=0), kind TEXT NOT NULL CHECK(kind IN ('reply','ended')),
        turn_id TEXT NOT NULL, start INTEGER NOT NULL CHECK(start>0), end INTEGER NOT NULL CHECK(end>start),
        line INTEGER NOT NULL CHECK(line>0), raw_sha256 TEXT NOT NULL, semantic_sha256 TEXT NOT NULL,
        proof_sha256 TEXT NOT NULL, reply_sha256 TEXT, PRIMARY KEY(attempt,ordinal), UNIQUE(attempt,segment,start,end))''',
}


@dataclass(frozen=True)
class _Frontier:
    header_hash: str
    floor_end: int
    floor_line: int
    floor_hash: str
    verified_end: int
    verified_line: int
    verified_hash: str
    source_stamp: tuple


@dataclass(frozen=True)
class _State:
    generation: int
    withheld: int
    armed: int
    frontier: _Frontier
    candidate: Candidate | None


@dataclass(frozen=True, repr=False)
class _Seed:
    mapping: object
    pin: AttemptPin
    state: _State
    index_stamp: tuple


@dataclass(frozen=True)
class _ScanPass:
    observation: Observation
    reply: str = field(default='', repr=False)
    source: _Source | None = field(default=None, repr=False)
    state: _State | None = field(default=None, repr=False)


def _semantic_hash(pin, kind, turn, reply_hash):
    return _proof_hash('watch-codex-candidate', [pin.attempt_id, pin.owner_request_id, pin.admission_digest,
        pin.target_binding_digest, pin.thread_id, pin.envelope_sha256, kind, turn, reply_hash])


def _load_frontier(row, pin):
    _require(row is not None, 'map_corrupt')
    fields = dict(row)
    fields.pop('attempt')
    stamp = json.loads(fields['source_stamp'])
    _require(type(stamp) is list and len(stamp) == 6 and all(_number(v) for v in stamp), 'map_corrupt')
    fields['source_stamp'] = tuple(stamp)
    f = _Frontier(**fields)
    _require(all(_digest(v) for v in (f.header_hash, f.floor_hash, f.verified_hash))
             and _number(f.floor_end, 1, MAX_SOURCE) and _number(f.floor_line, 1, MAX_LINES)
             and _number(f.verified_end, f.floor_end, MAX_SOURCE) and _number(f.verified_line, f.floor_line, MAX_LINES)
             and f.source_stamp[:3] == (pin.rollout.device, pin.rollout.inode, pin.rollout.uid)
             and f.source_stamp[3] == f.verified_end, 'map_corrupt')
    return f


def _load_candidate(row, pin, frontier):
    fields = dict(row)
    fields.pop('attempt')
    segment = fields.pop('segment')
    c = Candidate(**fields)
    _require(segment == 0 and c.ordinal == 1 and c.kind in ('reply', 'ended') and _turn_id(c.turn_id)
             and _number(c.start, frontier.floor_end, frontier.verified_end - 1)
             and _number(c.end, c.start + 1, frontier.verified_end)
             and _number(c.line, frontier.floor_line + 1, frontier.verified_line)
             and all(_digest(v) for v in (c.raw_sha256, c.semantic_sha256, c.proof_sha256))
             and (_digest(c.reply_sha256) if c.kind == 'reply' else c.reply_sha256 is None)
             and c.semantic_sha256 == _semantic_hash(pin, c.kind, c.turn_id, c.reply_sha256), 'map_corrupt')
    return c


def _prefix(raw, frontier):
    if len(raw) < frontier.verified_end:
        raise _Conflict('verified_prefix_truncated')
    floor, verified = raw[:frontier.floor_end], raw[:frontier.verified_end]
    if any(not prefix.endswith(b'\n') or prefix.count(b'\n') != count or _sha(prefix) != digest
           for prefix, count, digest in ((floor, frontier.floor_line, frontier.floor_hash),
                                         (verified, frontier.verified_line, frontier.verified_hash))):
        raise _Conflict('verified_prefix_changed')


class EvidenceMap:
    """Private fixture store. A reopened handle can never seed another attempt."""
    def __init__(self, path, installation_id=None, *, create=False, checkpoint=None):
        self.db = None
        self.checkpoint = checkpoint or (lambda _point: None)
        _require(_path(path) and type(create) is bool and (installation_id is None if create else _uuid(installation_id)),
                 'invalid_map')
        if create:
            installation_id = str(uuid.uuid4())
        self.path, self.installation_id, self.creating = path, installation_id, create
        parent = os.path.dirname(path)
        try:
            with _open_directory(parent) as fd:
                st = os.fstat(fd)
                _require(st.st_uid == os.getuid() and st.st_mode & 0o077 == 0, 'map_not_private')
                self.root = PathPin(parent, st.st_dev, st.st_ino, st.st_uid)
                if create:
                    owned = os.open(os.path.basename(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=fd)
                    os.close(owned)
            source, st = codex_transcript.open_regular(parent, path)
            with source:
                self.file = PathPin(path, st.st_dev, st.st_ino, st.st_uid)
                self._require_private(st)
            self.db = sqlite3.connect('file:' + quote(path, safe='/') + '?mode=rw',
                                      uri=True, timeout=0.2, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('PRAGMA foreign_keys=ON')
            _require(self.db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete', 'map_schema')
            self.db.execute('PRAGMA synchronous=FULL')
            pages = MAX_MAP_BYTES // self.db.execute('PRAGMA page_size').fetchone()[0]
            _require(self.db.execute('PRAGMA max_page_count=' + str(pages)).fetchone()[0] == pages, 'map_capacity')
            if create:
                with self._transaction(write=True):
                    for sql in _SCHEMA.values():
                        self.db.execute(sql)
                    self.db.execute('INSERT INTO installation VALUES (1,?,?,?)',
                                    (installation_id, MAP_SCHEMA_VERSION, POLICY))
            self._schema()
            self.schema_version = self.db.execute('PRAGMA schema_version').fetchone()[0]
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, sqlite3.Error, codex_transcript.Unavailable)):
                raise EvidenceUnavailable('map_unavailable') from None
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _require_private(st):
        _require(st.st_uid == os.getuid() and st.st_mode & 0o077 == 0 and st.st_size <= MAX_MAP_BYTES,
                 'map_not_private')

    def _check_db(self):
        _require(self.db is not None, 'map_unavailable')
        _directory(self.root)
        source, st = codex_transcript.open_regular(self.root.path, self.path)
        with source:
            _match_pin(self.file, st)
            self._require_private(st)

    def _schema(self):
        actual = {r[1]: (r[0], ' '.join((r[2] or '').split())) for r in
                  self.db.execute("SELECT type,name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'")}
        _require(actual == {name: ('table', ' '.join(sql.split())) for name, sql in _SCHEMA.items()}, 'map_schema')
        self._installation()

    def _installation(self):
        meta = self.db.execute('SELECT * FROM installation LIMIT 2').fetchall()
        _require([tuple(row) for row in meta] == [(1, self.installation_id, MAP_SCHEMA_VERSION, POLICY)], 'map_identity')

    @contextmanager
    def _transaction(self, *, write=False):
        try:
            self._check_db()
            self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                if hasattr(self, 'schema_version'):
                    _require(self.db.execute('PRAGMA schema_version').fetchone()[0] == self.schema_version, 'map_schema')
                    self._installation()
                yield
                self._check_db()
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        except (OSError, sqlite3.Error, codex_transcript.Unavailable):
            raise EvidenceUnavailable('map_unavailable') from None

    def _state(self, pin):
        row = self.db.execute('SELECT * FROM attempts WHERE id=?', (pin.attempt_id,)).fetchone()
        _require(row is not None, 'map_attempt_missing')
        _require(row['pin'] == _canonical(asdict(pin)), 'map_attempt_conflict')
        _require(_number(row['armed'], 0, 1) and _number(row['generation'], 1 + row['armed'])
                 and _number(row['withheld'], 0, 1)
                 and _number(row['emitted'], 0, 1), 'map_corrupt')
        frontier_row = self.db.execute('SELECT * FROM frontiers WHERE attempt=?', (pin.attempt_id,)).fetchone()
        try:
            frontier = _load_frontier(frontier_row, pin)
            entries = self.db.execute('SELECT * FROM emissions WHERE attempt=? LIMIT 2', (pin.attempt_id,)).fetchall()
            _require(len(entries) == row['emitted'], 'map_corrupt')
            candidate = _load_candidate(entries[0], pin, frontier) if entries else None
        except (ValueError, TypeError, KeyError):
            raise EvidenceUnavailable('map_corrupt') from None
        return _State(row['generation'], row['withheld'], row['armed'], frontier, candidate)

    @staticmethod
    def _input(pin, chunk):
        _require(type(pin) is AttemptPin and _number(chunk, 1, MAX_CHUNK), 'invalid_input')

    def _seed_slot(self, pin):
        _require(self.db.execute('SELECT 1 FROM attempts WHERE id=?', (pin.attempt_id,)).fetchone() is None, 'map_attempt_exists')
        _require(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] < MAX_ATTEMPTS, 'map_capacity')

    def seed(self, pin, *, chunk=MAX_CHUNK):
        self._seed(pin, chunk=chunk)

    def _seed(self, pin, *, chunk=MAX_CHUNK):
        self._input(pin, chunk)
        _require(self.creating, 'map_reopen_read_only_seed')
        with self._transaction():
            self._seed_slot(pin)
        try:
            source = _read_source(pin, chunk, self.checkpoint)
            raw = source.snapshot
            header = _header_hash(raw, pin)
            _require(raw.endswith(b'\n'), 'incomplete_floor')
            line = sum(1 for _ in _positions(raw))
            self.checkpoint('before_source_recheck')
            source.recheck(pin)
        except (OSError, sqlite3.Error, codex_transcript.Unavailable):
            raise EvidenceUnavailable('source_unavailable') from None
        floor = _sha(raw)
        with self._transaction(write=True):
            self._seed_slot(pin)
            self.db.execute('INSERT INTO attempts VALUES (?,?,1,0,0,0)', (pin.attempt_id, _canonical(asdict(pin))))
            self.db.execute('INSERT INTO frontiers VALUES (?,?,?,?,?,?,?,?,?)', (pin.attempt_id, header, len(raw), line,
                floor, len(raw), line, floor, _canonical(source.rollout_stamp)))
            seed = _Seed(self, pin, self._state(pin), source.index_stamp)
            self.checkpoint('before_seed_commit')
        self.checkpoint('after_seed_commit')
        return seed

    def _check_floor(self, seed, *, armed):
        _require(type(seed) is _Seed and seed.mapping is self and self.creating, 'map_seed_required')
        expected = replace(seed.state, generation=2, armed=1) if armed else seed.state
        with self._transaction():
            _require(self._state(seed.pin) == expected, 'map_generation_changed')
        try:
            source = _read_source(seed.pin, MAX_CHUNK, self.checkpoint)
            _prefix(source.snapshot, expected.frontier)
            _require(source.rollout_stamp == expected.frontier.source_stamp
                     and source.index_stamp == seed.index_stamp, 'source_floor_changed')
            self.checkpoint('before_source_recheck')
            source.recheck(seed.pin)
        except (OSError, sqlite3.Error, codex_transcript.Unavailable):
            raise EvidenceUnavailable('source_unavailable') from None
        return expected

    def _arm(self, seed):
        expected = self._check_floor(seed, armed=False)
        with self._transaction(write=True):
            _require(self._state(seed.pin) == expected and expected.generation == 1 and not expected.armed,
                     'map_generation_changed')
            self.db.execute('UPDATE attempts SET armed=1,generation=2 WHERE id=?', (seed.pin.attempt_id,))
            self.checkpoint('before_arm_commit')
        self.checkpoint('after_arm_commit')

    def _retained_armed_emission(self, pin):
        self._input(pin, MAX_CHUNK)
        with self._transaction():
            state = self._state(pin)
            _require(state.armed and state.candidate is not None, 'map_emission_required')
            return state.candidate

    def _withhold(self, pin, expected):
        with self._transaction(write=True):
            _require(self._state(pin) == expected, 'map_generation_changed')
            self.db.execute('UPDATE attempts SET withheld=1,generation=generation+1 WHERE id=?', (pin.attempt_id,))

    def _read_candidate(self, pin, state, chunk):
        source = _read_source(pin, chunk, self.checkpoint)
        raw, old = source.snapshot, state.frontier
        try:
            _prefix(raw, old)
            if _header_hash(raw, pin) != old.header_hash:
                raise _Conflict('header_changed')
            if not raw.endswith(b'\n'):
                _require(len(raw) - raw.rfind(b'\n') - 1 < MAX_LINE, 'source_line_limit')
                return source, None, None, ''
            line = sum(1 for _ in _positions(raw))
            candidate, reply = _classify(pin, raw, old.floor_end, old.floor_line)
            if state.candidate is not None and state.candidate != candidate:
                raise _Conflict('emission_conflict')
        except EvidenceUnavailable as error:
            self.checkpoint('before_source_recheck')
            source.recheck(pin)
            if not isinstance(error, _Conflict) and error.code not in ('source_line_limit', 'turn_capacity'):
                raise _Conflict(error.code) from None
            raise
        return source, line, candidate, reply

    def scan(self, pin, *, chunk=MAX_CHUNK):
        return self._scan(pin, chunk, armed=False).observation

    def _scan_armed(self, pin):
        return self._scan(pin, MAX_CHUNK, armed=True)

    def _recheck_armed(self, pin, verified):
        _require(verified.observation.candidate is not None and verified.source is not None, 'map_emission_required')
        with self._transaction():
            _require(self._state(pin) == verified.state and verified.state.armed, 'map_generation_changed')
        try:
            verified.source.recheck(pin)
        except _Conflict:
            self._withhold(pin, verified.state)
            raise
        except (OSError, sqlite3.Error, codex_transcript.Unavailable):
            raise EvidenceUnavailable('source_unavailable') from None

    def _scan(self, pin, chunk, *, armed):
        self._input(pin, chunk)
        with self._transaction():
            state = self._state(pin)
            _require(bool(state.armed) == armed, 'map_arm_required')
        if state.withheld:
            return _ScanPass(Observation('unavailable', 'retained_source_conflict'))
        try:
            source, line, candidate, reply = self._read_candidate(pin, state, chunk)
            self.checkpoint('before_source_recheck')
            source.recheck(pin)
        except _Conflict as error:
            self._withhold(pin, state)
            return _ScanPass(Observation('unavailable', error.code))
        except EvidenceUnavailable as error:
            return _ScanPass(Observation('unavailable', error.code))
        except (OSError, sqlite3.Error, codex_transcript.Unavailable):
            return _ScanPass(Observation('unavailable', 'source_unavailable'))
        if line is None:
            return _ScanPass(Observation('waiting', 'incomplete_record'))

        raw = source.snapshot
        result = Observation('candidate', candidate=candidate) if candidate is not None else Observation('waiting')
        snapshot = replace(source, snapshot=b'') if armed else None
        if len(raw) == state.frontier.verified_end and candidate == state.candidate:
            with self._transaction():
                _require(self._state(pin) == state, 'map_generation_changed')
            return _ScanPass(result, reply if armed else '', snapshot, state)
        frontier = replace(state.frontier, verified_end=len(raw), verified_line=line,
                           verified_hash=_sha(raw), source_stamp=source.rollout_stamp)
        with self._transaction(write=True):
            _require(self._state(pin) == state, 'map_generation_changed')
            if candidate is not None and state.candidate is None:
                c = candidate
                self.db.execute('INSERT INTO emissions VALUES (?,?,0,?,?,?,?,?,?,?,?,?)', (pin.attempt_id, c.ordinal,
                    c.kind, c.turn_id, c.start, c.end, c.line, c.raw_sha256, c.semantic_sha256, c.proof_sha256, c.reply_sha256))
            self.db.execute('UPDATE frontiers SET verified_end=?,verified_line=?,verified_hash=?,source_stamp=? WHERE attempt=?',
                            (frontier.verified_end, frontier.verified_line, frontier.verified_hash,
                             _canonical(frontier.source_stamp), pin.attempt_id))
            self.db.execute('UPDATE attempts SET generation=generation+1,emitted=? WHERE id=?',
                            (int(candidate is not None), pin.attempt_id))
            self.checkpoint('before_commit')
        self.checkpoint('after_commit')
        committed = replace(state, generation=state.generation + 1, frontier=frontier, candidate=candidate)
        return _ScanPass(result, reply if armed else '', snapshot, committed)
