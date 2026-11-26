"""Disposable lock and identity proof. Production never imports this module."""
from contextlib import closing, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

from attachments_owner_guards import LINKS, OUTCOMES, OwnerAttachmentGuard, OwnerGuardError, _table
from attachments_owner_marker import CLEAR, UNCERTAIN
from attachments_owner_transport import OwnerAttachmentLedger
from claude_managed import Adapter, ManagedClaude, ManagedError, TestDependencies, VERSION
# These suites race managed Claude through POSIX fixtures (os.getuid, fcntl, dir_fd), so they skip on Windows; the
# reason comes from the platform, never from whether its module imports. Managed Claude on Windows (step 8b.5) is
# checked by test_managed_windows_storage and test_managed_windows_end_to_end.
MANAGED_WHY = (None if os.name != 'nt' else
               'These fixtures are POSIX (owner ids, fcntl locks, dir_fd); managed Claude on Windows is checked by '
               'test_managed_windows_storage and test_managed_windows_end_to_end.')
from new_chat_types import FrozenFolder
from officer_messaging import MessagingStore
from test_attachments_owner_guards import admission, uid


OWNER_DB = 'officer-messages.sqlite3'
OWNER_EXPECTED = 'native-namespace-fixture.expected'
AUTHORITY = 'managed-namespace-fixture.authority'
HOST_LOCK = 'managed-claude.lock'
MANAGED_ROOT = 'managed-claude'
MANAGED_EXPECTED = 'managed-claude.expected'
REGISTRY = 'registry.sqlite3'
REGISTRY_LOCK = 'registry.lock'
REGISTRY_EXPECTED = 'registry.expected'
OWNER_IDENTITY = 'native_namespace_fixture_identity'
MANAGED_IDENTITY = 'managed_namespace_fixture_identity'
IDENTITY = 'slot INTEGER PRIMARY KEY CHECK(slot=1), nonce TEXT NOT NULL CHECK(length(nonce)=64)'
LIMIT = 4096
MANAGED_TABLES = {
    'runs': ('id TEXT PRIMARY KEY, attempt TEXT UNIQUE NOT NULL, native TEXT UNIQUE NOT NULL, '
             'occupied INTEGER NOT NULL, data TEXT NOT NULL', (('id',), ('attempt',), ('native',))),
    'messages': ('id TEXT PRIMARY KEY, run TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL, '
                 'data TEXT NOT NULL', (('id',),)),
    'operations': ('id TEXT PRIMARY KEY, run TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL', (('id',),)),
    MANAGED_IDENTITY: (IDENTITY, ()),
}
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)   # this fixture's proof runs on POSIX only


def require(condition):
    if not condition:
        raise ManagedError(503, 'managed_unavailable', 'Fixture namespace proof is unavailable.')


@contextmanager
def guarded_proof():
    try:
        yield
    except (OSError, sqlite3.Error, ValueError, OwnerGuardError) as error:
        raise ManagedError(503, 'managed_unavailable', 'Fixture namespace proof is unavailable.') from error


def file_id(info):
    return [info.st_dev, info.st_ino]


def exists(path):
    return os.path.lexists(path)


def validate_fd(path, fd, *, directory=False):
    info = os.fstat(fd)
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    require(Path(os.path.realpath(path)) == path and file_id(path.lstat()) == file_id(info))
    return file_id(info)


@contextmanager
def checked_fd(path, *, flags=os.O_RDONLY, directory=False):
    fd = os.open(path, flags | NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    try:
        yield fd, validate_fd(path, fd, directory=directory)
    finally:
        os.close(fd)


def sync_directory(path):
    with checked_fd(path, directory=True) as (fd, _):
        os.fsync(fd)


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'


def parse_lines(raw):
    require(0 < len(raw) <= LIMIT and raw.endswith(b'\n'))
    records = [json.loads(line) for line in raw.splitlines()]
    require(b''.join(json_bytes(row) for row in records) == raw)
    require(all(type(row) is dict for row in records))
    return records


def write_all(fd, raw):
    while raw:
        count = os.write(fd, raw)
        require(count > 0)
        raw = raw[count:]


def valid_nonce(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def valid_id(value):
    return type(value) is list and len(value) == 2 and all(type(part) is int and part >= 0 for part in value)


def sqlite_path(db, path, expected):
    names = db.execute('PRAGMA database_list').fetchall()
    require(names == [(0, 'main', str(path))] or
            (names and names[0] == (0, 'main', str(path)) and names[1:] == [(1, 'temp', '')]))
    with checked_fd(path) as (_, current):
        require(current == expected)


def read_identity(db, table, nonce):
    found = dict(db.execute('SELECT name,sql FROM main.sqlite_master WHERE name=?', (table,)))
    _table(db, found, table, IDENTITY, ())
    require(db.execute(f'SELECT slot,nonce FROM main.{table} LIMIT 2').fetchall() == [(1, nonce)])


@contextmanager
def read_db(path, identity):
    with checked_fd(path) as (_, before):
        require(before == identity)
        require(all(not exists(Path(f'{path}{suffix}')) for suffix in ('-wal', '-shm', '-journal')))
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.12)) as db:
            db.execute('PRAGMA query_only=ON')
            require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',))
            db.execute('BEGIN')
            sqlite_path(db, path, identity)
            yield db
            sqlite_path(db, path, identity)


class Namespace:
    def __init__(self, root, *, checkpoint=None, timeout=.35):
        self.root = Path(root)
        self.managed = self.root / MANAGED_ROOT
        self.registry = self.managed / REGISTRY
        self.authority = self.root / AUTHORITY
        self.owner = self.root / OWNER_DB
        self.owner_expected = self.root / OWNER_EXPECTED
        self.host = self.root / HOST_LOCK
        self.checkpoint = checkpoint or (lambda _name: None)
        self.timeout = timeout

    def exclusive(self, path, data, stage):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        try:
            info = validate_fd(path, fd)
            self.checkpoint(f'{stage}_opened')
            write_all(fd, data)
            self.checkpoint(f'{stage}_before_sync')
            os.fsync(fd)
        finally:
            os.close(fd)
        self.checkpoint(f'{stage}_before_parent_sync')
        sync_directory(path.parent)
        return info

    @contextmanager
    def host_fence(self):
        import fcntl                     # POSIX only, like the rest of this fixture's proof
        with checked_fd(self.host) as (fd, _):
            end = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    require(time.monotonic() < end)
                    time.sleep(.005)
            validate_fd(self.host, fd)
            yield fd

    def initialize(self, store):
        """Explicit fresh-fixture step. Reopen never invokes initialization."""
        self.exclusive(self.host, b'', 'host_lock')
        with store.lock, self.host_fence() as hostfd:
            require(not any(map(exists, (self.owner_expected, self.authority, self.managed,
                                         self.root / MANAGED_EXPECTED))))
            require(not store.db.in_transaction)
            nonce = secrets.token_hex(32)
            store.db.execute('PRAGMA synchronous=FULL')
            store.db.execute('BEGIN IMMEDIATE')
            try:
                guard = OwnerAttachmentGuard.probe(store.db, lock=store.lock)
                require(guard is not None)
                require(not any(store.db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                                for table in (LINKS, OUTCOMES)))
                store.db.execute(f'CREATE TABLE {OWNER_IDENTITY} ({IDENTITY})')
                store.db.execute(f'INSERT INTO {OWNER_IDENTITY} VALUES (1,?)', (nonce,))
                self.checkpoint('owner_before_commit')
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
            self.checkpoint('owner_after_commit')
            sync_directory(self.root)
            authority = self.exclusive(self.authority, json_bytes({
                'v': 1, 'state': 'never_created', 'owner': nonce,
            }), 'authority')
            with checked_fd(self.owner) as (_, owner):
                sqlite_path(store.db, self.owner, owner)
            self.exclusive(self.owner_expected, json_bytes({
                'v': 1, 'owner': nonce, 'db': owner, 'authority': authority,
                'host': validate_fd(self.host, hostfd),
            }), 'owner_marker')
            self.checkpoint('owner_initialized')

    def owner_record(self, hostfd):
        require(hostfd is not None)
        with checked_fd(self.owner_expected) as (fd, _):
            records = parse_lines(os.read(fd, LIMIT + 1))
            require(len(records) == 1)
            record = records[0]
            require(set(record) == {'v', 'owner', 'db', 'authority', 'host'}
                    and type(record['v']) is int and record['v'] == 1 and valid_nonce(record['owner'])
                    and all(valid_id(record[key]) for key in ('db', 'authority', 'host')))
            require(validate_fd(self.host, hostfd) == record['host'])
            os.fsync(fd)
        sync_directory(self.root)
        return record

    def owner_guard(self, db, record, lock=None):
        sqlite_path(db, self.owner, record['db'])
        read_identity(db, OWNER_IDENTITY, record['owner'])
        guard = OwnerAttachmentGuard.probe(db, lock=lock)
        require(guard is not None)
        guard.require_unshadowed()
        return guard

    @contextmanager
    def owner_reader(self, record):
        with read_db(self.owner, record['db']) as db:
            yield self.owner_guard(db, record)

    def authority_records(self, record):
        with checked_fd(self.authority) as (fd, identity):
            require(identity == record['authority'])
            records = parse_lines(os.read(fd, LIMIT + 1))
            require(1 <= len(records) <= 3)
            require(records[0] == {'v': 1, 'state': 'never_created', 'owner': record['owner']}
                    and type(records[0]['v']) is int)
            if len(records) > 1:
                intent = records[1]
                require(set(intent) == {'state', 'nonce'} and intent['state'] == 'creating'
                        and valid_nonce(intent['nonce']))
            if len(records) == 3:
                ready = records[2]
                require(set(ready) == {'state', 'nonce', 'files'} and ready['state'] == 'initialized'
                        and ready['nonce'] == intent['nonce'] and type(ready['files']) is dict
                        and set(ready['files']) == {MANAGED_ROOT, REGISTRY, REGISTRY_LOCK,
                                                    REGISTRY_EXPECTED, MANAGED_EXPECTED}
                        and all(valid_id(identity) for identity in ready['files'].values()))
            os.fsync(fd)
            return records[-1]

    def append(self, record, transition, stage):
        with checked_fd(self.authority, flags=os.O_WRONLY | os.O_APPEND) as (fd, identity):
            require(identity == record['authority'])
            self.checkpoint(f'{stage}_before_append')
            write_all(fd, json_bytes(transition))
            self.checkpoint(f'{stage}_before_sync')
            os.fsync(fd)
        self.checkpoint(f'{stage}_after_sync')

    def managed_files(self, *, sync=False):
        with checked_fd(self.managed, directory=True) as (_, root):
            files = {MANAGED_ROOT: root}
        for name, path in ((REGISTRY, self.registry), (REGISTRY_LOCK, self.managed / REGISTRY_LOCK),
                           (REGISTRY_EXPECTED, self.managed / REGISTRY_EXPECTED),
                           (MANAGED_EXPECTED, self.root / MANAGED_EXPECTED)):
            with checked_fd(path) as (fd, identity):
                files[name] = identity
                if name in (REGISTRY_EXPECTED, MANAGED_EXPECTED):
                    require(os.read(fd, 3) == b'1\n' and not os.read(fd, 1))
                    if sync:
                        os.fsync(fd)
        if sync:
            sync_directory(self.managed)
            sync_directory(self.root)
        return files

    def registry_identity(self, db, state):
        require(state['state'] == 'initialized' and self.managed_files(sync=True) == state['files'])
        sqlite_path(db, self.registry, state['files'][REGISTRY])
        require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',))
        require(not db.execute("SELECT 1 FROM sqlite_temp_master WHERE type IN ('table','view') LIMIT 1").fetchone())
        rows = db.execute("SELECT type,name,sql FROM main.sqlite_master WHERE substr(name,1,7)<>'sqlite_'").fetchall()
        require({(kind, name) for kind, name, _ in rows} == {('table', name) for name in MANAGED_TABLES})
        catalogue = {name: sql for _, name, sql in rows}
        for name, (declaration, keys) in MANAGED_TABLES.items():
            _table(db, catalogue, name, declaration, keys)
        read_identity(db, MANAGED_IDENTITY, state['nonce'])

    def checked_empty_managed(self):
        require(not exists(self.managed) and not exists(self.root / MANAGED_EXPECTED))

    def before_create(self, hostfd):
        record = self.owner_record(hostfd)
        with self.owner_reader(record):
            state = self.authority_records(record)
        if state['state'] == 'initialized':
            with read_db(self.registry, state['files'][REGISTRY]) as db:
                self.registry_identity(db, state)
            return None
        require(state['state'] == 'never_created')
        self.checked_empty_managed()
        nonce = secrets.token_hex(32)
        self.append(record, {'state': 'creating', 'nonce': nonce}, 'creating')
        return nonce

    def finish_create(self, db, hostfd, nonce):
        record = self.owner_record(hostfd)
        require(not db.in_transaction and valid_nonce(nonce)
                and self.authority_records(record) == {'state': 'creating', 'nonce': nonce})
        self.checkpoint('registry_schema_committed')
        db.execute('BEGIN IMMEDIATE')
        db.execute(f'CREATE TABLE {MANAGED_IDENTITY} ({IDENTITY})')
        db.execute(f'INSERT INTO {MANAGED_IDENTITY} VALUES (1,?)', (nonce,))
        db.commit()
        self.checkpoint('managed_identity_committed')
        state = {'state': 'initialized', 'nonce': nonce, 'files': self.managed_files(sync=True)}
        self.registry_identity(db, state)
        self.append(record, state, 'initialized')

    def check_reservation(self, db, hostfd, request_id):
        with guarded_proof():
            record = self.owner_record(hostfd)
            state = self.authority_records(record)
            require(db is not None and db.in_transaction)
            self.registry_identity(db, state)
            with self.owner_reader(record) as guard:
                reservation = guard.reservation(request_id)
        require(reservation != UNCERTAIN)
        if reservation != CLEAR:
            raise ManagedError(409, 'request_conflict', 'That identifier belongs to another operation.')

    def native_presence(self, hostfd, owner_db, request_id):
        record = self.owner_record(hostfd)
        self.owner_guard(owner_db, record)
        state = self.authority_records(record)
        if state['state'] == 'never_created':
            self.checked_empty_managed()
            return False
        require(state['state'] == 'initialized')
        with read_db(self.registry, state['files'][REGISTRY]) as db:
            self.registry_identity(db, state)
            present = db.execute('SELECT 1 FROM main.messages WHERE id=? UNION ALL '
                                 'SELECT 1 FROM main.operations WHERE id=? LIMIT 1', (request_id, request_id)).fetchone()
            return present is not None

    def admit(self, store, ledger, attempt, now_ms):
        with guarded_proof(), store.lock, self.host_fence() as hostfd:
            require(not store.db.in_transaction)
            if self.native_presence(hostfd, store.db, attempt.owner_request_id):
                raise ManagedError(409, 'request_conflict', 'That identifier belongs to a managed operation.')
            return ledger.admit(attempt, now_ms=now_ms)


class ManagedFixture:
    """Real ManagedClaude and adapter with an in-process synthetic runner."""
    def __init__(self, root, namespace, *, request_failure=False):
        self.root = Path(root)
        self.calls = []
        self.claims = []
        self.turns = []
        self.messages = {}
        self.decisions = {}
        self.stopped = set()
        self.request_failure = request_failure
        project = self.root / 'project'
        info = project.stat()
        self.folder = FrozenFolder('orchard', 'v1', 'Orchard', str(project), info.st_dev, info.st_ino)
        program = self.root / 'fixture-executable'
        dependencies = TestDependencies(
            dict(path=str(program), version=VERSION, sha256=hashlib.sha256(program.read_bytes()).hexdigest()),
            self.spawn, self.request, self.snapshot, None, namespace=namespace,
            read_decision=lambda _path, permission, request, decision: self.decisions.get(request))
        self.manager = ManagedClaude(root, enabled=True, configuration_ready=True, _test_dependencies=dependencies)

    def spawn(self, _path):
        self.calls.append('spawn')

    def snapshot(self, path):
        manifest = json.loads((path / 'manifest.json').read_bytes())
        ended = path.name in self.stopped
        return dict(runId=path.name, nativeId=manifest['nativeId'], incarnation=manifest['incarnation'],
                    cwd=self.folder.path, state='exited' if ended else 'ready',
                    nativeExit=dict(confirmed=True, code=-15, incarnation=manifest['incarnation']) if ended else None,
                    identityVerified=True, messages=list(self.messages.values()), permissions=[])

    def request(self, path, command, params):
        self.calls.append(command)
        if command == 'enqueue':
            if self.request_failure:
                raise OSError('Fictional lost enqueue response.')
            message = dict(params, digest=hashlib.sha256(params['text'].encode()).hexdigest(),
                           state='delivered', nativeEcho=True, reply='', error='', createdAt=time.time())
            self.messages[params['requestId']] = message
            return message
        if command == 'decision':
            if self.request_failure:
                raise OSError('Fictional lost decision response.')
            value = dict(params, state='written')
            self.decisions[params['requestId']] = value
            return value
        if command == 'stop':                     # the owner's stop: the fictional Claude's exit is recorded
            self.stopped.add(path.name)
            return dict(requestId=params['requestId'], state='exited')
        require(command == 'status')
        return self.snapshot(path)

    def start(self):
        run_id = self.manager.reserve(self.folder, uid())
        self.manager.launch_once(run_id, self.folder, lambda *_: None)
        return run_id

    def action(self, kind, run_id, request_id, permission=None, decision='allow'):
        if kind == 'enqueue':
            return self.manager.enqueue(run_id, request_id, 'owner', 'Fictional note.')
        if kind == 'decide':
            return self.manager.decide(run_id, permission, request_id, decision)
        require(kind == 'new_chat' and Adapter.first_request_id(run_id) == request_id)
        return Adapter(self.manager).start_managed_work_once(
            run_id, 'Fictional note.', lambda: self.claims.append(request_id), self.turns.append)


def new_store(root):
    return MessagingStore(str(root), str(root / 'project'))


def fixture_files(root):
    (root / 'project').mkdir(mode=0o700)
    program = root / 'fixture-executable'
    program.write_bytes(b'No executable process is started by this fixture.')
    program.chmod(0o700)


def bootstrap(root, namespace, *, legacy_rows=()):
    require(not any(root.iterdir()))
    fixture_files(root)
    with closing(new_store(root)) as store:
        with store.lock:
            store.db.execute('BEGIN IMMEDIATE')
            try:
                store.db.executemany('INSERT INTO requests VALUES (?,?)', legacy_rows)
                store._attachment_guard = OwnerAttachmentGuard.install(store.db, store.lock)
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
        namespace.initialize(store)


@contextmanager
def native_store(root, namespace):
    # Refuse history loss before calling the owner's O_CREAT constructor.
    with guarded_proof(), namespace.host_fence() as hostfd:
        with namespace.owner_reader(namespace.owner_record(hostfd)):
            pass
    # A production pre-open refusal is also an unavailable proof for this fixture.
    with guarded_proof():
        store = new_store(root)
    with closing(store):
        yield store, OwnerAttachmentLedger(store.db, store.lock, guard=store._attachment_guard)


def native_action(root, namespace, request_id, *, now_ms=None):
    chosen = admission('x:' + str(uuid.UUID(int=1)), owner_request_id=request_id)
    with native_store(root, namespace) as (store, ledger):
        return namespace.admit(store, ledger, chosen, int(time.time() * 1000) if now_ms is None else now_ms)
