"""Private owner/managed namespace witnesses and reciprocal reservation fence.

POSIX: every record is opened by path without following a link and checked on its descriptor (owner, mode, one
name, and the path still naming that same file), flushed with ``fsync``, and the host lock is ``flock``. Windows:
the same records are opened through ``private_storage``'s no-link walk (each folder held by handle from the drive
down, any reparse point refused) and checked on their handles (owner and access list, one name); identities are the
volume serial and file index read from the handle (``file_identity``); new records are made private from the start
through the held folder (``create``) and appended through an append-only handle (``open_append``); flushes go
through a handle that can write (``FlushFileBuffers`` refuses a read-only one); and the host lock is the byte lock
``private_storage.try_lock`` takes. The folders above the state folder are resolved once (``anchored``), as the
attachment store resolves them, and the owner database's name is compared ignoring case and slashes, then by
identity.
"""
from contextlib import closing, contextmanager
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from attachments_owner_guards import LINKS, OUTCOMES, OwnerAttachmentGuard, OwnerGuardError, _table
from attachments_owner_marker import CLEAR, UNCERTAIN, sqlite_marker_available
import private_storage as ps

OWNER_DB = 'officer-messages.sqlite3'
ANCHOR = 'native-attachment-namespace.expected'
AUTHORITY = 'native-attachment-namespace.authority'
HOST = 'managed-claude.lock'
MANAGED = 'managed-claude'
REGISTRY = 'registry.sqlite3'
REGISTRY_LOCK = 'registry.lock'
REGISTRY_EXPECTED = 'registry.expected'
MANAGED_EXPECTED = 'managed-claude.expected'
OWNER_IDENTITY = 'owner_attachment_namespace_identity'
MANAGED_IDENTITY = 'managed_attachment_namespace_identity'
IDENTITY = 'slot INTEGER PRIMARY KEY CHECK(slot=1), nonce TEXT NOT NULL CHECK(length(nonce)=64)'
MANAGED_TABLES = {
    'runs': ('id TEXT PRIMARY KEY, attempt TEXT UNIQUE NOT NULL, native TEXT UNIQUE NOT NULL, '
             'occupied INTEGER NOT NULL, data TEXT NOT NULL', (('id',), ('attempt',), ('native',))),
    'messages': ('id TEXT PRIMARY KEY, run TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL, '
                 'data TEXT NOT NULL', (('id',),)),
    'operations': ('id TEXT PRIMARY KEY, run TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL', (('id',),)),
    MANAGED_IDENTITY: (IDENTITY, ()),
}
LIMIT = 4096


class NamespaceError(ValueError):
    def __init__(self, code='managed_unavailable'):
        self.code = code
        super().__init__(code)


def _require(condition):
    if not condition:
        raise NamespaceError()


@contextmanager
def _qualified():
    try:
        yield
    except NamespaceError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OwnerGuardError):
        raise NamespaceError() from None


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _id(st):
    return [st.st_dev, st.st_ino]


def descriptor_identity(fd):
    """The identity this module records for an open file: ``[st_dev, st_ino]`` of ``fstat`` on POSIX; on Windows the
    volume serial and file index read from its handle (``private_storage.file_identity``)."""
    if ps.WIN:
        return list(ps.file_identity(fd)[:2])
    return _id(os.fstat(fd))


def _validate_fd(path, fd, *, directory=False):
    if ps.WIN:
        return _win_validate(path, fd)
    st = os.fstat(fd)
    _require(st.st_uid == os.getuid() and not st.st_mode & 0o077)
    _require(stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1)
    _require(Path(os.path.realpath(path)) == path and _id(path.lstat()) == _id(st))
    return _id(st)


@contextmanager
def _open(path, *, flags=os.O_RDONLY, directory=False):
    if ps.WIN:
        with _win_open(path, flags=flags, directory=directory) as opened:
            yield opened
        return
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0), 0o600)
    try:
        yield fd, _validate_fd(path, fd, directory=directory)
    finally:
        os.close(fd)


def _try_lock(fd):
    """``private_storage.try_lock``; a POSIX without fcntl fails closed (NamespaceError), as the fence always has."""
    try:
        return ps.try_lock(fd)
    except ImportError:
        raise NamespaceError() from None


def _sync_directory(path):
    if ps.WIN:
        with ps.open_folder(str(path), private=True, writable=True) as folder:
            folder.sync()
        return
    with _open(path, directory=True) as (fd, _):
        os.fsync(fd)


def _flush(path, fd):
    """Flush the file ``fd`` has open, read-only or not. POSIX: ``fsync`` on it. Windows: ``FlushFileBuffers`` needs a
    handle that can write, so the same name is opened again to append through its held folder, checked to be the
    same file, and flushed through that handle (Windows flushes a file's data whichever handle asks)."""
    if not ps.WIN:
        os.fsync(fd)
        return
    with ps.open_folder(str(path.parent), private=True, writable=True) as folder:
        writer, _ = folder.open_append(path.name, create=False)
        try:
            _require(descriptor_identity(writer) == descriptor_identity(fd))
            ps.sync_file(writer)
        finally:
            os.close(writer)


@contextmanager
def _win_open(path, *, flags, directory):
    """Windows ``_open``: ``(fd, identity)`` through the no-link walk; a folder yields ``(None, identity)``. Without
    ``O_CREAT`` or ``O_APPEND`` the file is opened read-only and must be private with one name; ``O_CREAT`` (always
    with ``O_EXCL`` here) makes a new private file, and an existing name raises FileExistsError; ``O_APPEND`` opens
    an existing file to append only, and it must be private with one name too."""
    if directory:
        with ps.open_folder(str(path), private=True) as folder:
            yield None, list(folder.identity()[:2])
        return
    create, append = bool(flags & os.O_CREAT), bool(flags & os.O_APPEND)
    _require(not create or flags & os.O_EXCL)
    with ps.open_folder(str(path.parent), private=True, writable=create or append) as folder:
        if create:
            fd = folder.create(path.name)
        elif append:
            fd, _ = folder.open_append(path.name, create=False)
        else:
            fd = folder.open(path.name, single_link=True, private=True)
        try:
            if append:
                ps.check_private_fd(fd, single_link=True)    # open_append itself asks only that nobody else can write
            yield fd, descriptor_identity(fd)
        finally:
            os.close(fd)


def _win_validate(path, fd):
    """Windows ``_validate_fd`` for a file: private with one name on its handle, and its name, opened again through
    the walk, still that same file."""
    ps.check_private_fd(fd, single_link=True)
    identity = descriptor_identity(fd)
    with ps.open_folder(str(path.parent), private=True) as folder:
        again = folder.open(path.name, single_link=True, private=True)
        try:
            _require(descriptor_identity(again) == identity)
        finally:
            os.close(again)
    return identity


def same_database_path(name, path):
    """Whether SQLite's ``name`` for an open database is ``path``. POSIX: the same string. Windows: the same after
    resolving the folders above the last name (``anchored``) and ignoring case and slashes; callers also compare the
    file's identity, which is what decides."""
    if not ps.WIN:
        return name == str(path)
    if type(name) is not str or not name:
        return False

    def canonical(value):
        return ntpath.normcase(ntpath.normpath(ps.anchored(value)))
    try:
        return canonical(name) == canonical(str(path))
    except (OSError, ValueError):
        return False                 # a name the walk refuses by its shape (a device or UNC path) is not this file


def _line(record):
    return json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\n'


def _records(fd):
    raw = os.read(fd, LIMIT + 1)
    _require(0 < len(raw) <= LIMIT and raw.endswith(b'\n'))
    values = [json.loads(line) for line in raw.splitlines()]
    _require(all(type(value) is dict for value in values) and b''.join(map(_line, values)) == raw)
    return values


def _write(fd, data):
    while data:
        written = os.write(fd, data)
        _require(written > 0)
        data = data[written:]


def _nonce(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _identity(value):
    return type(value) is list and len(value) == 2 and all(type(part) is int and part >= 0 for part in value)


def _db_path(db, path, expected):
    names = [tuple(row) for row in db.execute('PRAGMA database_list')]
    if ps.WIN:
        _require(len(names) in (1, 2) and names[0][:2] == (0, 'main') and same_database_path(names[0][2], path)
                 and names[1:] in ([], [(1, 'temp', '')]))
    else:
        _require(names in ([(0, 'main', str(path))], [(0, 'main', str(path)), (1, 'temp', '')]))
    with _open(path) as (_, opened):
        _require(opened == expected)


def _sidecars_absent(path):
    _require(not any(_present(Path(str(path) + suffix)) for suffix in ('-journal', '-wal', '-shm')))


@contextmanager
def _reader(path, expected=None, *, strict=True):
    with _open(path) as (_, observed):
        _require(expected is None or observed == expected)
        if strict:
            _sidecars_absent(path)
        elif ps.WIN:
            # SQLite makes its journal by name, with the folder's inherited access list and, when Motet runs
            # elevated, Administrators as owner: the companion rule applies, as for the attachment store.
            _require(ps.sqlite_companions_private(str(path)))
        else:
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if _present(sidecar):
                    with _open(sidecar):
                        pass
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.12)) as db:
            db.execute('PRAGMA query_only=ON')
            if strict:
                _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',))
            db.execute('BEGIN')
            _db_path(db, path, observed)
            yield db
            _db_path(db, path, observed)
            if strict:
                _sidecars_absent(path)


def _table_exists(db, name):
    return db.execute('SELECT 1 FROM main.sqlite_schema WHERE name=? LIMIT 1', (name,)).fetchone() is not None


def _read_identity(db, table, expected):
    found = dict(db.execute('SELECT name,sql FROM main.sqlite_schema WHERE name=?', (table,)))
    _table(db, found, table, IDENTITY, ())
    _require([tuple(row) for row in db.execute(
        f'SELECT slot,nonce FROM main.{table} LIMIT 2')] == [(1, expected)])


class NativeNamespaceFence:
    """The same permanent host flock fences both directions of reservation."""

    def __init__(self, root):
        # Windows: the folders above the state folder (the user's profile, often moved with a junction) are resolved
        # once; the walk then refuses a junction or link at the state folder or anything below it.
        self.root = Path(ps.anchored(os.path.abspath(root)) if ps.WIN else os.path.abspath(root))
        self.host = self.root / HOST
        self.owner = self.root / OWNER_DB
        self.registry = self.root / MANAGED / REGISTRY
        self.authority = self.root / AUTHORITY
        self.anchor = self.root / ANCHOR

    def detected(self, *, owner_db=None):
        """Find surviving installation signals without initializing anything."""
        with _qualified():
            if _present(self.anchor) or _present(self.authority):
                return True
            if owner_db is not None and _table_exists(owner_db, OWNER_IDENTITY):
                return True
            for path, table in ((self.owner, OWNER_IDENTITY), (self.registry, MANAGED_IDENTITY)):
                if (_present(path) and not (path == self.owner and owner_db is not None)):
                    with _reader(path, strict=False) as reader:
                        if _table_exists(reader, table):
                            return True
            return False

    def legacy_managed_storage(self, owner_db):
        """Decline optional files for an unfenced managed registry; write nothing.

        Any surviving native witness or owner protection keeps its existing
        maintenance path. A legacy managed directory is not adopted or repaired.
        The caller holds the owner database lock.
        """
        with _qualified():
            if self.detected(owner_db=owner_db) or OwnerAttachmentGuard.probe(owner_db) is not None:
                return False
            return _present(self.root / MANAGED) or _present(self.root / MANAGED_EXPECTED)

    @contextmanager
    def host_fence(self):
        """Hold the host lock for the body. POSIX: ``flock(LOCK_EX | LOCK_NB)``, retried for up to .35 s while another
        holder has it (``private_storage.try_lock`` makes that call and reports EAGAIN, EWOULDBLOCK or EACCES as
        held elsewhere). Windows: the same through ``try_lock``'s byte lock, released before its file closes
        (Windows may keep a byte lock for a while after a close)."""
        with _qualified(), _open(self.host) as (fd, _):
            deadline = time.monotonic() + .35
            while not _try_lock(fd):
                _require(time.monotonic() < deadline)
                time.sleep(.005)
            try:
                _validate_fd(self.host, fd)
                yield fd
            finally:
                if ps.WIN:
                    try:
                        ps.unlock(fd)
                    except OSError:
                        pass            # not held: closing is all that is left

    def _anchor(self, hostfd):
        _require(hostfd is not None)
        with _open(self.anchor) as (fd, _), _open(self.root, directory=True) as (_, root):
            values = _records(fd)
            _require(len(values) == 1)
            value = values[0]
            _require(set(value) == {'v', 'owner', 'db', 'authority', 'host', 'root'}
                     and type(value['v']) is int and value['v'] == 1 and _nonce(value['owner'])
                     and all(_identity(value[key]) for key in ('db', 'authority', 'host', 'root'))
                     and value['root'] == root and value['host'] == _validate_fd(self.host, hostfd))
            _flush(self.anchor, fd)
        _sync_directory(self.root)
        return value

    def _owner_guard(self, db, record):
        _db_path(db, self.owner, record['db'])
        _read_identity(db, OWNER_IDENTITY, record['owner'])
        guard = OwnerAttachmentGuard.probe(db)
        _require(guard is not None)
        guard.require_unshadowed()
        return guard

    @contextmanager
    def _owner_reader(self, record):
        with _reader(self.owner, record['db']) as db:
            yield self._owner_guard(db, record)

    def _state(self, record):
        with _open(self.authority) as (fd, identity):
            _require(identity == record['authority'])
            values = _records(fd)
            _require(1 <= len(values) <= 3 and type(values[0].get('v')) is int
                     and values[0] == {'v': 1, 'state': 'never_created', 'owner': record['owner']})
            if len(values) > 1:
                intent = values[1]
                _require(set(intent) == {'state', 'nonce'} and intent['state'] == 'creating' and _nonce(intent['nonce']))
            if len(values) == 3:
                ready = values[2]
                _require(set(ready) == {'state', 'nonce', 'files'} and ready['state'] == 'initialized'
                         and ready['nonce'] == intent['nonce'] and type(ready['files']) is dict
                         and set(ready['files']) == {MANAGED, REGISTRY, REGISTRY_LOCK, REGISTRY_EXPECTED, MANAGED_EXPECTED}
                         and all(_identity(value) for value in ready['files'].values()))
            _flush(self.authority, fd)
        return values[-1]

    def _append(self, record, transition):
        with _open(self.authority, flags=os.O_WRONLY | os.O_APPEND) as (fd, identity):
            _require(identity == record['authority'])
            _write(fd, _line(transition))
            os.fsync(fd)

    def _managed_files(self):
        managed = self.root / MANAGED
        with _open(managed, directory=True) as (_, root):
            files = {MANAGED: root}
        for name, path in ((REGISTRY, self.registry), (REGISTRY_LOCK, managed / REGISTRY_LOCK),
                           (REGISTRY_EXPECTED, managed / REGISTRY_EXPECTED),
                           (MANAGED_EXPECTED, self.root / MANAGED_EXPECTED)):
            with _open(path) as (fd, identity):
                files[name] = identity
                if name in (REGISTRY_EXPECTED, MANAGED_EXPECTED):
                    _require(os.read(fd, 3) == b'1\n' and not os.read(fd, 1))
                    _flush(path, fd)
        _sync_directory(managed)
        _sync_directory(self.root)
        return files

    def _registry_identity(self, db, state):
        _require(state['state'] == 'initialized' and self._managed_files() == state['files'])
        _db_path(db, self.registry, state['files'][REGISTRY])
        _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
                 and not db.execute("SELECT 1 FROM sqlite_temp_schema WHERE type IN ('table','view') LIMIT 1").fetchone())
        _sidecars_absent(self.registry)
        rows = db.execute("SELECT type,name,sql FROM main.sqlite_schema WHERE substr(name,1,7)<>'sqlite_'").fetchall()
        _require({(kind, name) for kind, name, _ in rows} == {('table', name) for name in MANAGED_TABLES})
        catalogue = {name: sql for _, name, sql in rows}
        for name, (declaration, indexes) in MANAGED_TABLES.items():
            _table(db, catalogue, name, declaration, indexes)
        _read_identity(db, MANAGED_IDENTITY, state['nonce'])

    def _never_created(self):
        _require(not _present(self.root / MANAGED) and not _present(self.root / MANAGED_EXPECTED))

    def before_create(self, hostfd):
        with _qualified():
            record = self._anchor(hostfd)
            with self._owner_reader(record):
                state = self._state(record)
            if state['state'] == 'initialized':
                with _reader(self.registry, state['files'][REGISTRY]) as db:
                    self._registry_identity(db, state)
                return None
            _require(state['state'] == 'never_created')
            self._never_created()
            nonce = secrets.token_hex(32)
            self._append(record, {'state': 'creating', 'nonce': nonce})
            return nonce

    def finish_create(self, db, hostfd, nonce):
        with _qualified():
            record = self._anchor(hostfd)
            _require(not db.in_transaction and _nonce(nonce)
                     and self._state(record) == {'state': 'creating', 'nonce': nonce})
            db.execute('BEGIN IMMEDIATE')
            db.execute(f'CREATE TABLE {MANAGED_IDENTITY} ({IDENTITY})')
            db.execute(f'INSERT INTO {MANAGED_IDENTITY} VALUES (1,?)', (nonce,))
            db.commit()
            state = {'state': 'initialized', 'nonce': nonce, 'files': self._managed_files()}
            self._registry_identity(db, state)
            self._append(record, state)

    def check_reservation(self, db, hostfd, request_id):
        with _qualified():
            _require(db is not None and db.in_transaction)
            record = self._anchor(hostfd)
            self._registry_identity(db, self._state(record))
            with self._owner_reader(record) as guard:
                self._clear(guard.reservation(request_id))

    @staticmethod
    def _clear(value):
        _require(value != UNCERTAIN)
        if value != CLEAR:
            raise NamespaceError('request_conflict')

    def check_existing_owner(self, request_id):
        """L2c holds apply to managed reservations without certifying absence for native."""
        with _qualified():
            if _present(self.owner):
                with _reader(self.owner, strict=False) as db:
                    guard = OwnerAttachmentGuard.probe(db)
                    if guard is not None:
                        guard.require_unshadowed()
                        self._clear(guard.reservation(request_id))

    @contextmanager
    def owner_open(self):
        """Verify a retained owner database before opening it without O_CREAT."""
        with self.host_fence() as hostfd:
            record = self._anchor(hostfd)
            with self._owner_reader(record):
                self._state(record)
            yield record['db']

    def require_owner_connection(self, db, expected):
        with _qualified():
            _db_path(db, self.owner, expected)

    @contextmanager
    def admission(self, owner_db, request_id, *, unmanaged_native=None):
        """Caller holds the owner RLock, then takes this flock before its writer."""
        with self.host_fence() as hostfd:
            _require(not owner_db.in_transaction)
            record = self._anchor(hostfd)
            self._owner_guard(owner_db, record)
            state = self._state(record)
            if state['state'] == 'never_created':
                self._never_created()
            else:
                _require(state['state'] == 'initialized')
                with _reader(self.registry, state['files'][REGISTRY]) as db:
                    self._registry_identity(db, state)
                    present = db.execute('SELECT 1 FROM main.messages WHERE id=? UNION ALL '
                                         'SELECT 1 FROM main.operations WHERE id=? LIMIT 1',
                                         (request_id, request_id)).fetchone()
                    managed = None if unmanaged_native is None else db.execute(
                        'SELECT 1 FROM main.runs WHERE native=? LIMIT 1', (unmanaged_native,)).fetchone()
                if present is not None or managed is not None:
                    raise NamespaceError('request_conflict')
            yield

    def initialize_owner(self, owner_db, owner_lock):
        """Install the witness only with proved owner identity and no managed history."""
        with owner_lock, _qualified():
            _require(sqlite_marker_available(owner_db))
            self._never_created()
            if not _present(self.host):
                try:
                    with _open(self.host, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL) as (fd, _):
                        os.fsync(fd)
                    _sync_directory(self.root)
                except FileExistsError:
                    pass
            with self.host_fence() as hostfd:
                _require(not self.detected(owner_db=owner_db) and not owner_db.in_transaction)
                self._never_created()
                guard = OwnerAttachmentGuard.probe(owner_db)
                if guard is not None:
                    guard.require_unshadowed()
                    for table in (LINKS, OUTCOMES):
                        _require(owner_db.execute(f'SELECT 1 FROM main.{table} LIMIT 1').fetchone() is None)
                with _open(self.root, directory=True) as (_, root), _open(self.owner) as (_, identity):
                    _db_path(owner_db, self.owner, identity)
                    _require(owner_db.execute('PRAGMA synchronous').fetchone()[0] >= 2)
                    nonce = secrets.token_hex(32)

                    # Publish a durable uncertainty signal before the DB write.
                    # A fault or partial publication requires recovery, never adoption.
                    with _open(self.authority, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL) as (fd, authority):
                        _write(fd, _line({'v': 1, 'state': 'never_created', 'owner': nonce}))
                        os.fsync(fd)
                    _sync_directory(self.root)
                    owner_db.execute('BEGIN IMMEDIATE')
                    try:
                        if guard is None:
                            guard = OwnerAttachmentGuard.install(owner_db, owner_lock)
                        owner_db.execute(f'CREATE TABLE {OWNER_IDENTITY} ({IDENTITY})')
                        owner_db.execute(f'INSERT INTO {OWNER_IDENTITY} VALUES (1,?)', (nonce,))
                        owner_db.commit()
                    except BaseException:
                        owner_db.rollback()
                        raise
                    _sync_directory(self.root)
                    with _open(self.anchor, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL) as (fd, _):
                        _write(fd, _line(dict(v=1, owner=nonce, db=identity, authority=authority,
                                              host=_validate_fd(self.host, hostfd), root=root)))
                        os.fsync(fd)
                    _sync_directory(self.root)
                    self._owner_guard(owner_db, self._anchor(hostfd))
                    return OwnerAttachmentGuard.probe(owner_db, lock=owner_lock)

    def validate_mount(self, owner_db):
        # Empty spelling cannot match a managed reservation and proves no real ID absent.
        with self.admission(owner_db, ''):
            pass
