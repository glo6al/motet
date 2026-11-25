"""Private, fail-closed ownership and transport for detached Claude runners.

Construction and observation never launch a native process. Production readiness
is deliberately separate from the feature gate and has no environment override.

Storage. POSIX: every folder and file is checked on its descriptor (this user's, no group or other bits, one name,
no link), created 0700 or 0600, and the host and registry locks are blocking ``flock``. Windows (step 8b.1; the
feature stays refused there until 8b.5): the same folders and files through private_storage, checked on their
handles and created private from the start; SQLite's own companion files follow private_storage's companion rule;
folders are flushed through a held handle; and the locks are private_storage's byte lock on the same files, the lock
the attachment namespace fence takes on ``managed-claude.lock``, waited for up to LOCK_SECONDS.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from xml.parsers.expat import ExpatError

from new_chat_inventory import canonical_uuid, validate_folder
from attribution import AUTHORS, motet_worded, motet_words
import private_storage as ps
from new_chat_types import (Capability, ExecutionResult, FrozenFolder, NewChatError, VerifiedIdentity,
                            MANAGED_PERMISSION_TIMEOUT_SECONDS)

VERSION = '2.1.323'
PINNED_SHA256 = '5432918d904a9b6afef7bb79d20df24d76052eabc1f41bceb84091d21f8889a7'
ADAPTER_VERSION = 'claude-managed-v1/' + VERSION
TRANSPORT = 'claude_managed_v1'
READINESS_FILENAME = 'managed-claude-readiness.json'
MAX_FRAME = 256 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_RUNS = 4096
MAX_MESSAGES = 8192
LOCK_SECONDS = 10         # Windows: how long a host or registry lock is waited for (POSIX flock waits without end)
TEXT_RETENTION = 7 * 86400
STARTUP_TIMEOUT = 15
PERMISSION_TIMEOUT_SECONDS = MANAGED_PERMISSION_TIMEOUT_SECONDS
PERMISSION_REASONS = frozenset(('owner', 'timeout', 'withdrawn', 'turn_ended', 'native_exit',
                               'oversized', 'unsupported_tool', 'capacity', 'shutdown', 'inactive_turn'))
STATES = frozenset(('starting', 'ready', 'running', 'needs_attention', 'exited', 'uncertain'))
PUBLIC_STATES = {'queued': 'sending', 'dispatching': 'sending', 'delivered': 'awaiting_reply',
                 'completed': 'answered', 'failed': 'failed', 'unknown': 'unknown'}



def local_configuration_identity():
    """Identify the local Mac, user and pinned executable without starting Claude. Windows: the SMBIOS system UUID
    and the process token's SID, and the pinned build for this machine's architecture (claude_managed_windows)."""
    if ps.WIN:
        return _windows_configuration_identity()
    if sys.platform != 'darwin':
        raise ValueError('Managed Claude requires macOS.')
    output = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=2, check=True).stdout
    if len(output) > 64 * 1024:
        raise ValueError('Unexpected macOS platform identity.')
    devices = plistlib.loads(output)
    if type(devices) is not list or len(devices) != 1 or type(devices[0]) is not dict:
        raise ValueError('Unexpected macOS platform identity.')
    hardware_id = devices[0].get('IOPlatformUUID')
    if type(hardware_id) is not str:
        raise ValueError('Missing macOS platform identity.')
    machine = uuid.UUID(hardware_id)
    if machine.int == 0:
        raise ValueError('Missing macOS platform identity.')
    host = hashlib.sha256(f'watch-managed-claude/v1/{machine}/{os.getuid()}'.encode('ascii')).hexdigest()
    return dict(schemaVersion=1, hostFingerprint=host, profile='normal',
                executableVersion=VERSION, executableSha256=PINNED_SHA256)


def _windows_configuration_identity():
    import claude_managed_windows as windows
    pin = windows.windows_pin()
    if pin is None:
        raise ValueError('No Claude Code build is pinned for this machine.')
    return dict(schemaVersion=1, hostFingerprint=windows.host_fingerprint(windows.system_uuid(), windows.user_sid()),
                profile='normal', executableVersion=VERSION, executableSha256=pin[0])


def _readiness_json_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('Duplicate readiness field.')
    return value


def _windows_readiness(state_dir):
    """Windows: the readiness record through private_storage. The state folder and the record are held through the
    no-link walk and checked private on their handles; a held folder and an open file cannot be renamed, so the
    record is the same file throughout, and its handle identity (with size and times) is compared before and
    after the read."""
    try:
        state_dir = Path(state_dir)
        directory = state_dir.parent.resolve(strict=True) / state_dir.name
        with ps.open_folder(str(directory), private=True) as folder:
            fd = folder.open(READINESS_FILENAME, single_link=True, private=True)
            try:
                before = ps.file_identity(fd)
                if not 1 <= before[2] <= 1024:
                    return False
                raw = os.read(fd, 1025)
                if len(raw) != before[2]:
                    return False
                record = json.loads(raw.decode('utf-8'), object_pairs_hook=_readiness_json_object)
                if type(record) is not dict or type(record.get('schemaVersion')) is not int:
                    return False
                if record != local_configuration_identity():
                    return False
                return ps.file_identity(fd) == before
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError, RuntimeError, ManagedError,
            ExpatError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return False


def _readiness_file_identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def load_configuration_readiness(state_dir, *, enabled=False, demo=False):
    """Read the local qualification once at startup. Missing or unsafe records fail closed."""
    if enabled is not True or demo is not False or sys.platform != 'darwin':
        return False
    if ps.WIN:
        return _windows_readiness(state_dir)
    parent_fd = fd = None
    try:
        state_dir = Path(state_dir)
        # Match the manager's trusted home-alias resolution without following a state-dir link.
        directory = state_dir.parent.resolve(strict=True) / state_dir.name
        parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(parent_fd)
        _require_private(parent, True)
        fd = os.open(READINESS_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(fd)
        _require_private(before)
        if not 1 <= before.st_size <= 1024:
            return False
        raw = os.read(fd, 1025)
        if len(raw) != before.st_size:
            return False
        record = json.loads(raw.decode('utf-8'), object_pairs_hook=_readiness_json_object)
        if type(record) is not dict or type(record.get('schemaVersion')) is not int:
            return False
        if record != local_configuration_identity():
            return False
        current = os.stat(READINESS_FILENAME, dir_fd=parent_fd, follow_symlinks=False)
        parent_now = os.lstat(directory)
        _require_private(parent_now, True)
        return (_readiness_file_identity(before) == _readiness_file_identity(os.fstat(fd))
                and _readiness_file_identity(before) == _readiness_file_identity(current)
                and (parent.st_dev, parent.st_ino) == (parent_now.st_dev, parent_now.st_ino))
    except (OSError, ValueError, TypeError, RuntimeError, ManagedError,
            ExpatError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return False
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


# Who put a message in a managed chat: the owner's own typed words, Motet's wording at the owner's request, or the
# first instruction of a new managed chat.
MESSAGE_SOURCES = ('owner', 'motet', 'new_chat')
MOTET_SOURCE_REFUSED = ('This managed chat refused the message; it may have been started by an older Motet. '
                        'Restart it to receive messages Motet words.')


def _keep_author(old, message):
    """A runner snapshot does not carry the author; keep the one the registry recorded (absent stays absent)."""
    try:
        previous = json.loads(old) if isinstance(old, str) else old
    except ValueError:
        previous = None
    if isinstance(previous, dict) and previous.get('author') in AUTHORS and 'author' not in message:
        return dict(message, author=previous['author'])
    return message


def _permission_resolution(value):
    """Project retained facts, leaving older rows' unavailable facts unknown."""
    fields = {key: value.get(key) for key in ('decision', 'resolutionReason', 'decisionState', 'resolvedAt')}
    if (fields['decision'] not in (None, 'allow', 'deny')
            or fields['resolutionReason'] not in PERMISSION_REASONS | {None}
            or fields['decisionState'] not in (None, 'dispatching', 'written', 'unknown')):
        _fail()
    _permission_time(fields['resolvedAt'])
    return fields


def _permission_time(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
        _fail()


class ManagedError(NewChatError):
    def __init__(self, status, code, message):
        super().__init__(code, message, status)


def _fail(code='managed_unavailable', status=409):
    messages = {
        'managed_disabled': 'Managed Claude is unavailable.',
        'configuration_unverified': 'The Claude configuration has not been verified.',
        'app_version_unsupported': 'This Claude build is not verified for managed sessions.',
        'managed_unavailable': 'Managed Claude state is unavailable; recovery is required.',
        'capacity': 'Managed Claude capacity is full. No session was started.',
        'request_conflict': 'That identifier belongs to another managed operation.',
        'invalid_request': 'The managed request is invalid.',
        'identity_unavailable': 'The exact Claude session and folder could not be verified.',
        'launch_outcome_unknown': 'The launch outcome is unknown. It will not be repeated.',
        'managed_not_ready': 'This managed Claude session is not ready for new messages.',
        'runner_refused': 'This managed Claude session refused the request. Nothing was queued.',
        'supervisor_detach_refused': ('Motet cannot start a Claude supervisor that outlives this window: the program '
                                      'Motet runs in does not let it leave its job. Try starting Motet from an '
                                      'ordinary terminal window or a shortcut.'),
        'supervisor_not_started': 'Motet could not start the Claude supervisor. Nothing was started.',
    }
    raise ManagedError(status, code, messages.get(code, messages['managed_unavailable']))


def _require_private(st, directory=False):
    if (st.st_uid != os.getuid() or st.st_mode & 0o077
            or not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            or (not directory and st.st_nlink != 1)):
        _fail()


def _private(path, directory=False):
    path = os.fspath(path)
    if os.path.realpath(path) != path:
        _fail()
    if ps.WIN:
        # The name itself (not a link) checked on its handle: this user's, an access list naming only this user and
        # SYSTEM (and Administrators on folders), a folder or a file with one name. Missing raises as POSIX does.
        try:
            ps.check_private(path, directory=directory, single_link=not directory)
        except ps.PrivateStorageError as error:
            if error.reason == 'missing':
                raise FileNotFoundError(path) from None
            _fail()
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        _require_private(st, directory)
        return st
    finally:
        os.close(fd)


def _present(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _write_private(path, data):
    if ps.WIN:
        fd = ps.create_private_file(os.fspath(path))     # private from the start; an existing name raises
        if os.name == 'nt':
            import msvcrt
            msvcrt.setmode(fd, os.O_BINARY)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _mkdir_private(path):
    """POSIX: ``mkdir(0o700)``, as before. Windows: a folder private from the start (an existing name raises)."""
    if ps.WIN:
        ps.make_private_folder(os.fspath(path))
    else:
        path.mkdir(mode=0o700, parents=False)


def _sync_folder(path):
    """Flush a folder's entries. POSIX: ``fsync`` on its descriptor, as before. Windows: through a held handle that
    can write (FlushFileBuffers needs one), opened with no link on the way."""
    if ps.WIN:
        with ps.open_folder(os.fspath(path), private=True, writable=True) as folder:
            folder.sync()
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock(path):
    """Hold the exclusive lock on the private lock file at ``path`` and return its descriptor. POSIX: blocking
    ``flock``, as before. Windows: private_storage's byte lock (the one the attachment namespace fence takes on the
    host lock) on a descriptor opened through the no-link walk, retried until LOCK_SECONDS pass."""
    if not ps.WIN:
        import fcntl
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd
    with ps.open_folder(os.fspath(path.parent), private=True) as folder:
        fd = folder.open(path.name, single_link=True, private=True)
    try:
        deadline = time.monotonic() + LOCK_SECONDS
        while not ps.try_lock(fd):
            if time.monotonic() >= deadline:
                _fail()
            time.sleep(.005)
    except BaseException:
        ps.close_lock(fd)
        raise
    return fd


def _unlock(fd):
    """Release a lock from ``_lock``: POSIX closes it, as before; Windows releases the byte lock, then closes."""
    if ps.WIN:
        ps.close_lock(fd)
    else:
        os.close(fd)


def _companions_private(path):
    """SQLite's journal, -wal and -shm beside ``path``. POSIX: each that exists is a private file, as before.
    Windows: private_storage's companion rule (SQLite creates them by name: owner this user, Administrators or
    SYSTEM, the folder's private list), checked on their handles."""
    if ps.WIN:
        if not ps.sqlite_companions_private(os.fspath(path)):
            _fail()
        return
    for suffix in ('-journal', '-wal', '-shm'):
        side = Path(str(path) + suffix)
        if _present(side):
            _private(side)


def _text(text):
    if (type(text) is not str or not text.strip() or len(text) > 8000 or '\x00' in text):
        _fail('invalid_request', 400)
    try:
        text.encode('utf-8', 'strict')
    except UnicodeError:
        _fail('invalid_request', 400)
    return hashlib.sha256(text.encode()).hexdigest()


def client_request(run_dir, command, params):
    """Authenticated bounded IPC only; never starts or adopts a process."""
    path = Path(run_dir)
    _private(path, True)
    for name in ('manifest.json', 'token', 'run.lock'):
        _private(path / name)
    token_path = path / 'token'
    fd = (ps.open_no_links(os.fspath(token_path), single_link=True, private=True) if ps.WIN
          else os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW))
    with os.fdopen(fd, 'rb') as source:
        token = source.read(65).decode('ascii')
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        _fail()
    if ps.WIN:
        return _windows_request(path, token, command, params)
    target = path / 'control.sock'
    st = os.lstat(target)
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        _fail()
    frame = json.dumps(dict(token=token, command=command, params=params), ensure_ascii=True).encode() + b'\n'
    if len(frame) > MAX_FRAME:
        _fail('invalid_request', 400)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(target))
        connection.sendall(frame)
        response = bytearray()
        while b'\n' not in response:
            chunk = connection.recv(min(8192, MAX_RESPONSE + 1 - len(response)))
            if not chunk or len(response) + len(chunk) > MAX_RESPONSE:
                _fail()
            response.extend(chunk)
        # These reply checks' Windows twin is _reply; a change here belongs there too.
        head, tail = bytes(response).split(b'\n', 1)
        if tail:
            _fail()
        value = json.loads(head)
        if not isinstance(value, dict) or type(value.get('ok')) is not bool:
            _fail()
        if not value['ok']:
            # Native/runtime exception text is never forwarded.
            code = value.get('error', {}).get('code')
            if code in ('request_conflict', 'conflict', 'stale_permission'):
                _fail('request_conflict')
            if code == 'invalid_request' and command == 'enqueue':
                _fail('runner_refused', 400)       # the runner refused this message; whether it kept it is unknown
            _fail('managed_not_ready')
        if set(value) != {'ok', 'value'}:
            _fail()
        return value['value']



def _not_started_code(error):
    """Windows: the plain refusal for a supervisor start that started nothing (claude_managed_windows.NotStarted),
    else None (an outcome that may have started something stays uncertain)."""
    import claude_managed_windows as windows
    if isinstance(error, windows.DetachRefused):
        return 'supervisor_detach_refused'
    if isinstance(error, windows.NotStarted):
        return 'supervisor_not_started'
    return None


def _windows_request(path, token, command, params):
    """Windows ``client_request`` over the supervisor's named pipe: its name and process ID come from the private
    ``control.json`` in the run folder; claude_managed_windows.open_pipe refuses a pipe whose server is not that
    process or whose owner and access list are not this user's private ones, before the token is sent. The frame,
    its limit, the reply's limits and its checks are client_request's."""
    import claude_managed_windows as windows
    control = path / 'control.json'
    _private(control)
    fd = ps.open_no_links(os.fspath(control), single_link=True, private=True)
    try:
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        _fail()
    record = json.loads(raw.decode('ascii'))
    if not isinstance(record, dict) or set(record) != {'pipe', 'pid'}:
        _fail()
    frame = json.dumps(dict(token=token, command=command, params=params), ensure_ascii=True).encode() + b'\n'
    if len(frame) > MAX_FRAME:
        _fail('invalid_request', 400)
    with windows.open_pipe(record['pipe'], record['pid']) as connection:
        connection.sendall(frame)
        response = bytearray()
        while b'\n' not in response:
            chunk = connection.recv(min(8192, MAX_RESPONSE + 1 - len(response)))
            if not chunk or len(response) + len(chunk) > MAX_RESPONSE:
                _fail()
            response.extend(chunk)
    return _reply(response, command)


def _reply(response, command=None):
    """Windows only: the supervisor's reply line, checked exactly as client_request's POSIX path checks it (one
    line, an object with ``ok``, provider error text never forwarded). A copy of that code, which POSIX keeps
    inline; a change to either belongs in both."""
    head, tail = bytes(response).split(b'\n', 1)
    if tail:
        _fail()
    value = json.loads(head)
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        _fail()
    if not value['ok']:
        code = value.get('error', {}).get('code')
        if code in ('request_conflict', 'conflict', 'stale_permission'):
            _fail('request_conflict')
        if code == 'invalid_request' and command == 'enqueue':
            _fail('runner_refused', 400)           # the runner refused this message; whether it kept it is unknown
        _fail('managed_not_ready')
    if set(value) != {'ok', 'value'}:
        _fail()
    return value['value']


def spawn_runner(run_dir):
    """Start one detached supervisor after a manager's durable launch claim. Windows: detached from the console,
    its process group and the server's job (claude_managed_windows.spawn_detached), or refused."""
    if ps.WIN:
        import claude_managed_windows as windows
        return windows.spawn_detached([sys.executable, str(Path(__file__).with_name('claude_managed_runner.py')),
                                       str(run_dir)])
    return subprocess.Popen([sys.executable, str(Path(__file__).with_name('claude_managed_runner.py')), str(run_dir)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)


@dataclass(frozen=True)
class TestDependencies:
    """Explicit internal fixture seams; never populated from HTTP or environment."""
    executable: dict
    spawn: object
    request: object
    read_snapshot: object
    directory: object
    profile: str = 'restricted_test'
    launch_timeout: float = .1
    read_message: object = None
    read_decision: object = None
    namespace: object = None


class ManagedClaude:
    def __init__(self, state_dir, *, enabled=False, demo=False, configuration_ready=False,
                 home=None, _test_dependencies=None):
        if _test_dependencies is not None and not isinstance(_test_dependencies, TestDependencies):
            raise TypeError('Explicit managed test dependencies are required.')
        self._state_dir = Path(os.path.abspath(os.fspath(state_dir)))
        # A home alias is legitimate. The private state directory itself and
        # every managed artifact still have to be real, owned entries.
        state_parent = self._state_dir.parent.resolve(strict=True)
        self.root = state_parent / self._state_dir.name / 'managed-claude'
        self.expected = self.root.parent / 'managed-claude.expected'
        self.home = Path(home or Path.home())
        self.enabled, self.demo = bool(enabled), bool(demo)
        self.configuration_ready = bool(configuration_ready)
        self._test = _test_dependencies
        self._lock = threading.RLock()
        self._proofs = {}
        self._directory = None
        self._windows_verified = None     # Windows: the last executable check that passed (verify_pinned)
        self.closed = False
        # No state creation, provider access, socket calls, or process work here.

    def _breakaway_refusal(self):
        """Windows: the plain reason a supervisor started now could not leave the server's job, else None (fixture
        managers never start a real supervisor, so they have none)."""
        if self._test:
            return None
        import claude_managed_windows as windows
        return windows.breakaway_refusal()

    def _active(self):
        if not self.enabled or self.demo or self.closed:
            _fail('managed_disabled', 503)

    def _windows_executable(self):
        """Windows: Claude Code 2.1.323 for this machine's architecture, found only where the native installer is
        expected to keep it (claude_managed_windows.locate_claude), refused if it is a shim, an alias, the desktop
        app's copy or reached through a link or junction, and hashed through a handle that shares reading only.
        The hash is taken again only when the file's identity (volume, index, size, last-write and NTFS change
        times) has changed since the
        last check that passed, so the New chat menu does not read ~367 MB each time it asks; the supervisor still
        hashes in full before it starts Claude."""
        import claude_managed_windows as windows
        known, self._windows_verified = self._windows_verified, None
        try:
            if self._test:
                configured = self._test.executable
                if not isinstance(configured, dict) or set(configured) != {'path', 'version', 'sha256'}:
                    raise ValueError()
                checked = (configured['sha256'], os.stat(configured['path']).st_size)
            else:
                pin = windows.windows_pin()
                if pin is None:
                    raise ValueError()
                configured = {'path': windows.locate_claude(self.home), 'version': VERSION, 'sha256': pin[0]}
                checked = pin
            if type(configured['path']) is not str or configured['version'] != VERSION:
                raise ValueError()
            self._windows_verified = windows.verify_pinned(configured['path'], checked, known)
            return dict(configured)
        except (OSError, ValueError, TypeError, KeyError):
            _fail('app_version_unsupported', 503)

    def _executable(self):
        if ps.WIN:
            return self._windows_executable()
        configured = (self._test.executable if self._test else {
            'path': str(self.home / '.local/share/claude/versions' / VERSION),
            'version': VERSION, 'sha256': PINNED_SHA256})
        if not isinstance(configured, dict) or set(configured) != {'path', 'version', 'sha256'}:
            _fail('app_version_unsupported', 503)
        try:
            path = configured['path']
            if type(path) is not str or os.path.realpath(path) != path or configured['version'] != VERSION:
                raise ValueError()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not before.st_mode & 0o111 or before.st_size > 1024**3:
                    raise ValueError()
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(fd)
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or digest.hexdigest() != configured['sha256']
                        or (not self._test and configured['sha256'] != PINNED_SHA256)):
                    raise ValueError()
            finally:
                os.close(fd)
            return dict(configured)
        except (OSError, ValueError, TypeError, KeyError):
            _fail('app_version_unsupported', 503)

    def preflight(self):
        try:
            self._active()
            if not self.configuration_ready:
                _fail('configuration_unverified', 503)
            if not self._test and sys.platform != 'darwin':
                _fail('app_version_unsupported', 503)
            if ps.WIN and self._breakaway_refusal() is not None:
                # Before any reservation: a supervisor that could not leave the server's job would end with it.
                _fail('supervisor_detach_refused', 503)
            self._executable()
            with self._registry() as db:
                if db is not None and db.execute('SELECT count(*) FROM runs WHERE occupied=1').fetchone()[0] >= 4:
                    _fail('capacity', 429)
            return Capability(True, version=ADAPTER_VERSION, supports_work=True,
                              supports_blank=False, ownership='watch_managed')
        except ManagedError as error:
            return Capability(False, error.code, error.message, ADAPTER_VERSION,
                              supports_work=False, supports_blank=False, ownership='watch_managed')

    @contextlib.contextmanager
    def _registry(self, create=False, *, reservation_id=None):
        """Host lock precedes registry transaction; never held across provider I/O."""
        db = None
        lockfd = hostfd = None
        namespace = self._test.namespace if self._test else None
        discovery = None
        with self._lock:
            try:
                try:
                    state_parent = self._state_dir.parent.resolve(strict=True)
                except RuntimeError:  # Python 3.9 raises this for symlink loops.
                    _fail()
                if state_parent != self.root.parent.parent:
                    _fail()
                try:
                    _private(self.root.parent, True)
                except FileNotFoundError:
                    if create:
                        _fail()
                host_lock = self.root.parent / 'managed-claude.lock'
                if namespace is None and (create or reservation_id is not None):
                    from attachments_native_namespace import NativeNamespaceFence
                    discovery = NativeNamespaceFence(self.root.parent)
                    if not os.path.lexists(host_lock) and discovery.detected():
                        namespace = discovery
                # Serialize initialization before publishing the root directory.
                # Readers acquire this permanent parent lock too, so they never
                # mistake an in-progress database initialization for corruption.
                if create and namespace is None:
                    try:
                        _write_private(host_lock, b'')
                    except FileExistsError:
                        pass
                if _present(host_lock):
                    _private(host_lock)
                    hostfd = _lock(host_lock)
                elif _present(self.root) or _present(self.expected):
                    _fail()
                if discovery is not None and discovery.detected():
                    namespace = discovery
                creation = namespace.before_create(hostfd) if create and namespace is not None else None
                if not _present(self.root):
                    if _present(self.expected):
                        _fail()
                    if not create:
                        if reservation_id is not None and namespace is not None:
                            namespace.check_reservation(None, hostfd, reservation_id)
                        yield None
                        return
                    if namespace is not None and creation is None:
                        _fail('managed_unavailable', 503)
                    try:
                        _mkdir_private(self.root)
                    except FileExistsError:
                        pass
                _private(self.root, True)
                if _present(self.expected):
                    _private(self.expected)
                lock_path = self.root / 'registry.lock'
                if create:
                    try:
                        _write_private(lock_path, b'')
                    except FileExistsError:
                        pass
                _private(lock_path)
                lockfd = _lock(lock_path)
                path, marker = self.root / 'registry.sqlite3', self.root / 'registry.expected'
                witnessed = discovery is not None and namespace is discovery
                endpoint = path.as_uri() + '?mode=rw' if witnessed else path
                options = {'uri': True} if witnessed else {}
                if not _present(path):
                    if not create or _present(marker) or namespace is not None and creation is None:
                        _fail()
                    _write_private(path, b'')
                    db = sqlite3.connect(endpoint, timeout=5, **options)
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('CREATE TABLE runs (id TEXT PRIMARY KEY, attempt TEXT UNIQUE NOT NULL, native TEXT UNIQUE NOT NULL, occupied INTEGER NOT NULL, data TEXT NOT NULL)')
                    db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, run TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL)')
                    db.execute('CREATE TABLE operations (id TEXT PRIMARY KEY, run TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL)')
                    db.commit()
                    _write_private(marker, b'1\n')
                    try:
                        _write_private(self.expected, b'1\n')
                    except FileExistsError:
                        _private(self.expected)
                    _sync_folder(self.root)
                    if namespace is not None:
                        namespace.finish_create(db, hostfd, creation)
                _private(path)
                _private(marker)
                _companions_private(path)
                if db is None:
                    db = sqlite3.connect(endpoint, timeout=5, **options)
                db.execute('PRAGMA synchronous=FULL')
                if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    _fail()
                records = db.execute('SELECT id,attempt,native,occupied,data FROM runs').fetchall()
                if len(records) > MAX_RUNS:
                    _fail()
                for ident, attempt, native, occupied, raw in records:
                    record = json.loads(raw)
                    keys = {'runId', 'nativeId', 'incarnation', 'attemptId', 'folder', 'state',
                            'occupied', 'launchClaimed', 'admitted', 'createdAt', 'nativeReceipt'}
                    if (not isinstance(record, dict) or not keys <= set(record) <= keys | {'startupExpiresAt'}
                            or record.get('runId') != ident or record.get('attemptId') != attempt
                            or record.get('nativeId') != native or not canonical_uuid(ident)
                            or not canonical_uuid(native) or not canonical_uuid(attempt)
                            or not canonical_uuid(record.get('incarnation'))
                            or record.get('state') not in STATES
                            or any(type(record.get(key)) is not bool for key in
                                   ('occupied', 'admitted', 'launchClaimed', 'nativeReceipt'))
                            or occupied not in (0, 1) or bool(occupied) != record.get('occupied')
                            or record['admitted'] and not record['nativeReceipt']
                            or record['nativeReceipt'] and not record['launchClaimed']
                            or not record['occupied'] and record['state'] != 'exited'
                            or type(record['createdAt']) not in (int, float)
                            or not math.isfinite(record['createdAt']) or record['createdAt'] <= 0
                            or record.get('startupExpiresAt') is not None and (
                                type(record['startupExpiresAt']) not in (int, float)
                                or not math.isfinite(record['startupExpiresAt'])
                                or record['startupExpiresAt'] <= record['createdAt'])):
                        _fail()
                    folder = record['folder']
                    if (not isinstance(folder, dict) or set(folder) != set(FrozenFolder.__dataclass_fields__)
                            or any(type(folder.get(key)) is not str or not folder[key]
                                   or len(folder[key]) > 4096 or '\x00' in folder[key]
                                   or any(0xD800 <= ord(char) <= 0xDFFF for char in folder[key])
                                   for key in ('id', 'revision', 'label', 'path'))
                            or not os.path.isabs(folder['path']) or os.path.normpath(folder['path']) != folder['path']
                            or type(folder.get('device')) is not int or folder['device'] < 0
                            or type(folder.get('inode')) is not int or folder['inode'] <= 0):
                        _fail()
                db.execute('SELECT id,run,source,digest,data FROM messages LIMIT 1').fetchall()
                db.execute('SELECT id,run,digest,data FROM operations LIMIT 1').fetchall()
                db.execute('BEGIN IMMEDIATE')
                if reservation_id is not None and namespace is not None:
                    namespace.check_reservation(db, hostfd, reservation_id)
                elif reservation_id is not None and discovery is not None:
                    discovery.check_existing_owner(reservation_id)
                yield db
                db.commit()
            except ManagedError:
                if db is not None:
                    db.rollback()
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError) as error:
                if db is not None:
                    db.rollback()
                if discovery is not None:
                    from attachments_native_namespace import NamespaceError
                    if isinstance(error, NamespaceError):
                        _fail('request_conflict' if error.code == 'request_conflict' else 'managed_unavailable',
                              409 if error.code == 'request_conflict' else 503)
                _fail()
            finally:
                if db is not None:
                    db.close()
                if lockfd is not None:
                    _unlock(lockfd)
                if hostfd is not None:
                    _unlock(hostfd)

    @staticmethod
    def _row(db, run_id):
        if not canonical_uuid(run_id):
            _fail('invalid_request', 400)
        raw = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone() if db is not None else None
        if raw is None:
            _fail('managed_unavailable', 404)
        try:
            row = json.loads(raw[0])
            if (row['runId'] != run_id or not canonical_uuid(row['nativeId'])
                    or not canonical_uuid(row['incarnation']) or row['state'] not in STATES):
                _fail()
            FrozenFolder(**row['folder'])
            return row
        except (ValueError, KeyError, TypeError):
            _fail()

    @staticmethod
    def _save(db, row):
        db.execute('UPDATE runs SET occupied=?,data=? WHERE id=?',
                   (int(row['occupied']), json.dumps(row, ensure_ascii=True), row['runId']))

    def reserve(self, folder, attempt_id):
        self._active()
        if not canonical_uuid(attempt_id):
            _fail('invalid_request', 400)
        validate_folder(folder)
        # Replay precedes capacity/preflight, but immutable folder binding remains.
        with self._registry() as db:
            old = db.execute('SELECT id FROM runs WHERE attempt=?', (attempt_id,)).fetchone() if db else None
            if old:
                row = self._row(db, old[0])
                if row['folder'] != asdict(folder):
                    _fail('request_conflict')
                return row['runId']
        cap = self.preflight()
        if not cap.available:
            raise ManagedError(503, cap.code, cap.message)
        with self._registry(create=True) as db:
            old = db.execute('SELECT id FROM runs WHERE attempt=?', (attempt_id,)).fetchone()
            if old:
                row = self._row(db, old[0])
                if row['folder'] != asdict(folder):
                    _fail('request_conflict')
                return row['runId']
            if (db.execute('SELECT count(*) FROM runs WHERE occupied=1').fetchone()[0] >= 4
                    or db.execute('SELECT count(*) FROM runs').fetchone()[0] >= MAX_RUNS):
                _fail('capacity', 429)
            row = dict(runId=str(uuid.uuid4()), nativeId=str(uuid.uuid4()), incarnation=str(uuid.uuid4()),
                       attemptId=attempt_id, folder=asdict(folder), state='starting', occupied=True,
                       launchClaimed=False, admitted=False, createdAt=time.time(), nativeReceipt=False, startupExpiresAt=None)
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',
                       (row['runId'], attempt_id, row['nativeId'], 1, json.dumps(row)))
            return row['runId']

    def _run_dir(self, run_id):
        if not canonical_uuid(run_id):
            _fail('invalid_request', 400)
        path = self.root / run_id
        _private(self.root, True)
        _private(path, True)
        return path

    def _request(self, run_id, command, params):
        self._active()
        path = self._run_dir(run_id)
        for name in ('manifest.json', 'token', 'run.lock'):
            _private(path / name)
        if self._test:
            return self._test.request(path, command, params)
        return client_request(path, command, params)

    def launch_once(self, run_id, folder, record_native_receipt):
        self._active()
        validate_folder(folder)
        executable = self._executable()
        if not self.configuration_ready:
            _fail('configuration_unverified', 503)
        with self._registry() as db:
            row = self._row(db, run_id)
            if row['folder'] != asdict(folder):
                _fail('request_conflict')
            claimed = row['launchClaimed']
            if not claimed:
                row['launchClaimed'] = True
                duration = min(STARTUP_TIMEOUT, max(.001, self._test.launch_timeout if self._test else STARTUP_TIMEOUT))
                row['startupExpiresAt'] = time.time() + duration
                self._save(db, row)
        if not claimed:
            try:
                run_dir = self.root / run_id
                _mkdir_private(run_dir)
                manifest = {key: row[key] for key in ('runId', 'nativeId', 'incarnation', 'folder', 'createdAt')}
                manifest.update(schemaVersion=1, executable=executable,
                                profile=self._test.profile if self._test else 'normal')
                _write_private(run_dir / 'manifest.json', json.dumps(manifest).encode())
                _write_private(run_dir / 'token', secrets.token_hex(32).encode())
                _write_private(run_dir / 'run.lock', b'')
                _sync_folder(run_dir)
                validate_folder(folder)
                self._executable()
                if self._test:
                    self._test.spawn(run_dir)
                else:
                    spawn_runner(run_dir)
            except Exception as error:
                code = _not_started_code(error) if ps.WIN else None
                if code is not None:
                    # Windows: the supervisor never ran, so nothing started. The run ends without a native receipt,
                    # its reason is kept in its folder, and the plain refusal is the answer, also on replay.
                    _fail(self._not_started(run_id, code), 503)
                self._uncertain(run_id)
                _fail('launch_outcome_unknown')
        elif ps.WIN:
            code = self._recorded_not_started(run_id, row)
            if code is not None:
                _fail(code, 503)
        # Replays and a replacement web process inherit the original startup
        # deadline. Neither status nor a second launch_once extends the window.
        deadline = time.monotonic() + max(0, (row.get('startupExpiresAt') or 0) - time.time())
        while True:
            # A detached child returns before it binds its socket. Initial IPC
            # absence is a bounded wait for this one launch, never a new launch
            # or a durable orphan diagnosis from a disconnected ledger view.
            snapshot = None
            try:
                value = self._request(run_id, 'status', {})
            except ManagedError:
                raise
            except Exception:
                value = None
            if value is not None:
                snapshot = self._checked_snapshot(value, row)
                if snapshot['state'] in ('uncertain', 'exited'):
                    self._uncertain(run_id)
                    _fail('launch_outcome_unknown')
                if snapshot['identityVerified']:
                    validate_folder(folder)
                    with self._registry() as db:
                        current = self._row(db, run_id)
                        current.update(nativeReceipt=True, state=snapshot['state'])
                        self._save(db, current)
                    record_native_receipt(row['nativeId'], folder.path)
                    return row['nativeId']
            if ps.WIN:
                # A launch that another caller recorded as not started is that refusal here too, at once.
                with self._registry() as db:
                    code = self._recorded_not_started(run_id, self._row(db, run_id))
                if code is not None:
                    _fail(code, 503)
            if time.monotonic() >= deadline:
                self._uncertain(run_id)
                _fail('launch_outcome_unknown')
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    NOT_STARTED_FILE = 'not-started'

    def _not_started(self, run_id, code):
        """Windows: the claimed launch started nothing (the supervisor never ran). Its reason is written to the run
        folder (a private file read only for this) and flushed, then the run ends without a native receipt,
        releasing its slot; it ends either way, since nothing started. Returns the code to answer with: ``code``
        if it was recorded, else the plainer supervisor_not_started."""
        recorded = True
        try:
            _write_private(self.root / run_id / self.NOT_STARTED_FILE, code.encode('ascii'))
            _sync_folder(self.root / run_id)
        except FileExistsError:
            pass
        except (OSError, ManagedError):
            recorded = False
        with self._registry() as db:
            row = self._row(db, run_id)
            row.update(state='exited', occupied=False)
            self._save(db, row)
        return code if recorded else 'supervisor_not_started'

    def _recorded_not_started(self, run_id, row):
        """Windows: the reason recorded by ``_not_started`` for a run that ended without a native receipt, else None
        (a Claude that started and exited before its identity was proved has no such record)."""
        if row['occupied'] or row['state'] != 'exited' or row['nativeReceipt'] or not row['launchClaimed']:
            return None
        path = self.root / run_id / self.NOT_STARTED_FILE
        try:
            _private(path)
            fd = ps.open_no_links(os.fspath(path), single_link=True, private=True)
            try:
                code = os.read(fd, 64).decode('ascii')
            finally:
                os.close(fd)
        except (OSError, ManagedError, UnicodeError):
            return None
        return code if code in ('supervisor_detach_refused', 'supervisor_not_started') else None

    def _uncertain(self, run_id):
        with self._registry() as db:
            row = self._row(db, run_id)
            if not row['occupied'] and row['state'] == 'exited':
                # A released slot records a confirmed end (an exit, or on Windows a launch that started nothing).
                # Uncertainty never reopens it: an unoccupied row must stay 'exited' for the registry to be valid.
                return
            row['state'] = 'uncertain'
            self._save(db, row)

    def _checked_snapshot(self, value, row):
        if (not isinstance(value, dict) or value.get('runId') != row['runId']
                or value.get('nativeId') != row['nativeId'] or value.get('incarnation') != row['incarnation']
                or value.get('cwd') != row['folder']['path'] or value.get('state') not in STATES
                or type(value.get('messages')) is not list or len(value['messages']) > MAX_MESSAGES
                or type(value.get('permissions')) is not list or len(value['permissions']) > 16):
            _fail()
        messages = []
        for message in value['messages']:
            if (not isinstance(message, dict) or not canonical_uuid(message.get('requestId'))
                    or message.get('state') not in PUBLIC_STATES or message.get('source') not in MESSAGE_SOURCES):
                _fail()
            clean = {k: message.get(k) for k in ('requestId', 'source', 'text', 'digest', 'state', 'nativeEcho', 'reply', 'error', 'createdAt', 'replyTruncated') if k in message}
            if type(clean.get('text', '')) is not str or len(clean.get('text', '')) > 8000:
                _fail()
            # Error strings are runner allowlisted; enforce our own public mapping.
            clean['error'] = ('Managed delivery failed or is uncertain.' if clean.get('error') else '')
            reply = clean.get('reply', '')
            if type(reply) is not str:
                _fail()
            if len(reply) > 16000:
                clean['reply'], clean['replyTruncated'] = reply[:16000], True
            messages.append(clean)
        permissions = []
        evidence_available = value.get('evidenceAvailable', True)
        if type(evidence_available) is not bool or (not evidence_available and value['permissions']):
            _fail()
        timeout = value.get('timeoutSeconds', PERMISSION_TIMEOUT_SECONDS)
        if type(timeout) is not int or timeout != PERMISSION_TIMEOUT_SECONDS:
            _fail()
        for permission in value['permissions']:
            if not isinstance(permission, dict) or not canonical_uuid(permission.get('id')):
                _fail()
            if len(json.dumps(permission.get('input'), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > 32768:
                _fail()
            clean = {key: permission.get(key) for key in ('id', 'tool', 'input', 'state', 'canAllow', 'canDeny', 'createdAt', 'expiresAt')}
            for key in ('createdAt', 'expiresAt'):
                _permission_time(clean[key])
            if (clean['createdAt'] is not None and clean['expiresAt'] is not None
                    and clean['expiresAt'] < clean['createdAt']):
                _fail()
            clean.update(_permission_resolution(permission))
            clean['inputAvailable'] = permission.get('inputAvailable', permission.get('state') not in ('unsupported', 'rejected'))
            if type(clean['inputAvailable']) is not bool:
                _fail()
            for key in ('canAllow', 'canDeny'):
                if type(clean[key]) is not bool:
                    _fail()
                clean[key] = (clean[key] and clean['state'] == 'pending' and clean['inputAvailable']
                              and value['state'] not in ('uncertain', 'exited'))
            permissions.append(clean)
        exit_receipt = value.get('nativeExit')
        if exit_receipt is not None and (not isinstance(exit_receipt, dict)
                or exit_receipt.get('confirmed') is not True or type(exit_receipt.get('code')) is not int
                or exit_receipt.get('incarnation') != row['incarnation'] or value['state'] != 'exited'):
            _fail()
        return dict(runId=row['runId'], nativeId=row['nativeId'], incarnation=row['incarnation'],
                    cwd=row['folder']['path'], state=value['state'], nativeExit=exit_receipt,
                    identityVerified=value.get('identityVerified') is True, messages=messages,
                    permissions=permissions, recoveryRequired=value['state'] == 'uncertain',
                    evidenceAvailable=evidence_available, timeoutSeconds=timeout, serverTime=time.time())

    def status(self, run_id):
        self._active()
        with self._registry() as db:
            row = self._row(db, run_id)
        value = None
        live = False
        try:
            value = self._request(run_id, 'status', {})
            live = True
        except Exception:
            try:
                run_dir = self._run_dir(run_id)
                if self._test:
                    value = self._test.read_snapshot(run_dir)
                else:
                    from claude_managed_runner import read_snapshot
                    value = read_snapshot(run_dir)
            except Exception:
                pass
        if value is None:
            value = dict(runId=run_id, nativeId=row['nativeId'], incarnation=row['incarnation'],
                         cwd=row['folder']['path'], state='uncertain' if row['launchClaimed'] else 'starting',
                         nativeExit=None, identityVerified=False, messages=[], permissions=[], evidenceAvailable=False)
        snapshot = self._checked_snapshot(value, row)
        with self._registry() as db:
            current = self._row(db, run_id)
            if not live and not snapshot['nativeExit'] and current['launchClaimed']:
                expires = current.get('startupExpiresAt')
                remaining = expires - time.time() if expires is not None else -1
                startup_pending = (current['state'] == 'starting' and not current['nativeReceipt']
                                   and 0 <= remaining <= STARTUP_TIMEOUT)
                # A socket absence observed before another thread committed its
                # first exact receipt cannot veto that newer phase. This single
                # stale observation still supplies no fresh identity proof.
                receipt_advanced = not row['nativeReceipt'] and current['nativeReceipt']
                # Slot release is itself retained evidence of an original
                # confirmed exit; missing endpoint/detail cannot undo it.
                confirmed_exit = not current['occupied'] and current['state'] == 'exited'
                state = (current['state'] if receipt_advanced or confirmed_exit else
                         'starting' if startup_pending else 'uncertain')
                snapshot.update(state=state, identityVerified=False,
                                recoveryRequired=state == 'uncertain')
                for permission in snapshot['permissions']:
                    permission.update(canAllow=False, canDeny=False)
                for message in snapshot['messages']:
                    if message['state'] in ('dispatching', 'delivered'):
                        message.update(state='unknown', error='Managed delivery is uncertain.')
            current['state'] = snapshot['state']
            if snapshot['nativeExit']:
                current['occupied'] = False
            self._save(db, current)
            for message in snapshot['messages']:
                old = db.execute('SELECT run,source,digest,data FROM messages WHERE id=?', (message['requestId'],)).fetchone()
                if old and old[:3] == (run_id, message['source'], message.get('digest')):
                    db.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(_keep_author(old[3], message)), message['requestId']))
        return snapshot

    def enqueue(self, run_id, request_id, source, text):
        self._active()
        digest = _text(text)
        if not canonical_uuid(request_id) or source not in MESSAGE_SOURCES:
            _fail('invalid_request', 400)
        with self._registry(reservation_id=request_id) as db:
            run = self._row(db, run_id)
            old = db.execute('SELECT run,source,digest,data FROM messages WHERE id=?', (request_id,)).fetchone()
            if db.execute('SELECT 1 FROM operations WHERE id=?', (request_id,)).fetchone():
                _fail('request_conflict')
            if old:
                if old[:3] != (run_id, source, digest):
                    _fail('request_conflict')
                return self._retained(json.loads(old[3]))
            if not run['nativeReceipt'] or run['state'] not in ('ready', 'running', 'needs_attention'):
                _fail('managed_not_ready')
            validate_folder(FrozenFolder(**run['folder']))
            if db.execute('SELECT count(*) FROM messages').fetchone()[0] >= MAX_MESSAGES:
                _fail('capacity', 429)
            pending = sum(json.loads(raw)['state'] in ('queued', 'dispatching', 'delivered', 'unknown')
                          for (raw,) in db.execute('SELECT data FROM messages WHERE run=?', (run_id,)))
            if pending >= 32:
                _fail('capacity', 429)
            message = dict(requestId=request_id, source=source, text=text, digest=digest,
                           state='unknown', nativeEcho=False, reply='', error='', createdAt=time.time())
            if source in AUTHORS:
                # Who wrote the words, kept across status updates (_keep_author). A row stored before this was
                # recorded has no author and reads as unknown, never as the owner, whatever its source says.
                message['author'] = source
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', (request_id, run_id, source, digest, json.dumps(message)))
        try:
            self._request(run_id, 'enqueue', dict(requestId=request_id, source=source, text=text))
        except ManagedError as error:
            # The claim is retained even when the response was lost. Never reissue.
            if error.code == 'runner_refused' and source == 'motet':
                # A supervisor started before Motet recorded who wrote a message refuses source 'motet'. The same
                # refusal can follow a storage fault after the runner kept the message, so the state stays unknown
                # and the words say so; it is never resent, and never as the owner's.
                self._refused(request_id, run_id, digest, MOTET_SOURCE_REFUSED)
        except Exception:
            pass
        self.status(run_id)
        return self.lookup_message(request_id)

    def _refused(self, request_id, run_id, digest, reason):
        with self._registry() as db:
            row = db.execute('SELECT run,digest,data FROM messages WHERE id=?', (request_id,)).fetchone()
            if row and row[:2] == (run_id, digest):
                message = json.loads(row[2])
                if message.get('state') == 'unknown' and not message.get('error'):
                    message['error'] = reason          # the state stays unknown: delivery is not certain either way
                    db.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(message), request_id))

    @staticmethod
    def _retained(message):
        result = dict(message)
        if result.get('state') in ('completed', 'failed') and time.time() - result.get('createdAt', 0) > TEXT_RETENTION:
            result.update(text='', reply='', textExpired=True)
        return result

    def lookup_message(self, request_id):
        if not canonical_uuid(request_id):
            _fail('invalid_request', 400)
        with self._registry() as db:
            old = db.execute('SELECT run,data FROM messages WHERE id=?', (request_id,)).fetchone() if db else None
        if old is None:
            with self._registry() as db:
                operation = db.execute('SELECT data FROM operations WHERE id=?', (request_id,)).fetchone() if db else None
            return json.loads(operation[0]) if operation else None
        message = json.loads(old[1])
        if self.enabled and not self.demo and not self.closed and message['state'] not in ('completed', 'failed'):
            try:
                run_dir = self._run_dir(old[0])
                if self._test:
                    fresh = self._test.read_message(run_dir, request_id) if self._test.read_message else None
                else:
                    from claude_managed_runner import read_message
                    fresh = read_message(run_dir, request_id)
                if fresh is not None:
                    with self._registry() as db:
                        row = self._row(db, old[0])
                    checked = self._checked_snapshot(dict(runId=row['runId'], nativeId=row['nativeId'],
                        incarnation=row['incarnation'], cwd=row['folder']['path'], state=row['state'],
                        nativeExit=None, messages=[fresh], permissions=[]), row)['messages'][0]
                    if (checked['requestId'], checked['source'], checked['digest']) != (request_id, message['source'], message['digest']):
                        _fail('request_conflict')
                    # Read-only evidence can add a receipt or terminal outcome. It
                    # cannot erase a stronger live status with its disconnect view.
                    if checked['state'] in ('completed', 'failed') or checked.get('nativeEcho') and not message.get('nativeEcho'):
                        message = _keep_author(message, checked)
                        with self._registry() as db:
                            db.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(message), request_id))
            except Exception:
                pass
        result = self._retained(message)
        result['runId'] = old[0]
        return result

    def ownership(self, native_id):
        if not canonical_uuid(native_id):
            return None
        with self._registry() as db:
            old = db.execute('SELECT id FROM runs WHERE native=?', (native_id,)).fetchone() if db else None
            if not old:
                return None
            row = self._row(db, old[0])
            return dict(runId=row['runId'], nativeId=native_id, admitted=row['admitted'], state=row['state'])

    def owns_agent(self, agent):
        return bool(isinstance(agent, str) and agent.startswith('c:') and self.ownership(agent[2:]))

    def has_retained_runs(self):
        """Ownership fence survives disabled rollout and disconnected processes."""
        with self._registry() as db:
            return bool(db and db.execute('SELECT 1 FROM runs LIMIT 1').fetchone())

    def _get_directory(self):
        if self._test:
            return self._test.directory
        if self._directory is None:
            from chat_directory import ChatDirectory
            self._directory = ChatDirectory(str(self.home / '.codex'), str(self.home / '.claude/projects'), policy={'history_days': 'all'})
        return self._directory

    def verify_existing(self, native_id, folder):
        self._active()
        validate_folder(folder)
        owned = self.ownership(native_id)
        if not owned:
            return None
        with self._registry() as db:
            row = self._row(db, owned['runId'])
        if row['folder'] != asdict(folder) or not row['nativeReceipt']:
            return None
        try:
            directory = self._get_directory()
            directory.refresh(force=True)
            proof = directory.claude_metadata('c:' + native_id, folder.path)
            source = proof['identity']
            st = os.stat(source['path'], follow_symlinks=False)
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or os.path.realpath(source['path']) != source['path']
                    or (st.st_dev, st.st_ino) != (source['device'], source['inode'])
                    or proof['agent'] != 'c:' + native_id or proof['project'] != folder.path
                    or not directory.claude_proof_current(proof)):
                return None
            validate_folder(folder)
            identity = VerifiedIdentity(native_id, folder.path, source['path'], st.st_dev, st.st_ino,
                                        ADAPTER_VERSION, folder.device, folder.inode, provider='claude_code')
            with self._lock:
                self._proofs[native_id] = (identity, proof, time.monotonic())
            return identity
        except Exception:
            return None

    def mark_admitted(self, identity, permit=lambda: True):
        self._active()
        if not isinstance(identity, VerifiedIdentity) or identity.provider != 'claude_code':
            _fail('identity_unavailable')
        with self._lock:
            retained = self._proofs.get(identity.native_id)
        if not retained or retained[0] is not identity or time.monotonic() - retained[2] > 5:
            _fail('identity_unavailable')
        if not self._get_directory().claude_proof_current(retained[1]):
            _fail('identity_unavailable')
        owned = self.ownership(identity.native_id)
        with self._registry() as db:
            row = self._row(db, owned['runId'])
            validate_folder(FrozenFolder(**row['folder']))
            info = os.stat(identity.source_path, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (identity.source_device, identity.source_inode)):
                _fail('identity_unavailable')
            if not permit():
                return False
            row['admitted'] = True
            self._save(db, row)
        return True

    def can_observe(self, native_id, source_path):
        native_id = native_id[2:] if isinstance(native_id, str) and native_id.startswith('c:') else native_id
        owned = self.ownership(native_id)  # integrity is checked even for unrelated IDs
        if not owned:
            # Invalid identifiers still check registry integrity above.
            with self._registry():
                pass
            return True
        if not owned['admitted'] or not self.enabled or self.demo or self.closed:
            return False
        with self._registry() as db:
            row = self._row(db, owned['runId'])
        identity = self.verify_existing(native_id, FrozenFolder(**row['folder']))
        return bool(identity and identity.source_path == os.fspath(source_path))

    def decide(self, run_id, permission_id, request_id, decision):
        self._active()
        if not canonical_uuid(permission_id) or not canonical_uuid(request_id) or decision not in ('allow', 'deny'):
            _fail('invalid_request', 400)
        digest = hashlib.sha256(json.dumps([permission_id, decision]).encode()).hexdigest()
        with self._registry(reservation_id=request_id) as db:
            self._row(db, run_id)
            if db.execute('SELECT 1 FROM messages WHERE id=?', (request_id,)).fetchone():
                _fail('request_conflict')
            old = db.execute('SELECT run,digest,data FROM operations WHERE id=?', (request_id,)).fetchone()
            if old and old[:2] != (run_id, digest):
                _fail('request_conflict')
            if not old:
                if db.execute('SELECT count(*) FROM operations').fetchone()[0] >= MAX_MESSAGES:
                    _fail('capacity', 429)
                binding = dict(requestId=request_id, runId=run_id, source='permission', digest=digest,
                               permissionId=permission_id, decision=decision, state='unknown')
                db.execute('INSERT INTO operations VALUES (?,?,?,?)', (request_id, run_id, digest, json.dumps(binding)))
        value = None
        native_error = None
        if old and json.loads(old[2]).get('error'):
            native_error = json.loads(old[2])['error']
        if old is None:
            try:
                value = self._request(run_id, 'decision', dict(permissionId=permission_id, requestId=request_id, decision=decision))
            except ManagedError as error:
                # An error response can follow a committed native decision
                # claim. Its exact retained receipt wins over transport error
                # mapping; neither this call nor a replay repeats the command.
                native_error = error.code
            except Exception:
                pass
        if value is None:
            try:
                run_dir = self._run_dir(run_id)
                if self._test:
                    value = self._test.read_decision(run_dir, permission_id, request_id, decision) if self._test.read_decision else None
                else:
                    from claude_managed_runner import read_decision
                    value = read_decision(run_dir, permission_id, request_id, decision)
            except Exception as error:
                if getattr(error, 'code', '') in ('conflict', 'request_conflict', 'stale_permission'):
                    _fail('request_conflict')
                _fail('managed_not_ready')
        if value is None:
            if native_error:
                with self._registry() as db:
                    binding = json.loads(old[2]) if old else binding
                    binding['error'] = native_error
                    db.execute('UPDATE operations SET data=? WHERE id=?', (json.dumps(binding), request_id))
                _fail(native_error)
            if old:
                value = json.loads(old[2])
            else:
                _fail('managed_not_ready')
        if (not isinstance(value, dict) or value.get('permissionId') != permission_id
                or value.get('requestId') != request_id or value.get('decision') != decision
                or value.get('state') not in ('written', 'unknown', 'dispatching', 'decided')):
            _fail()
        receipt = {k: value[k] for k in ('permissionId', 'requestId', 'decision', 'state')}
        receipt.update(_permission_resolution(value))
        if receipt['state'] == 'dispatching':
            receipt['state'] = 'unknown'
        if receipt['decisionState'] == 'dispatching':
            receipt['decisionState'] = 'unknown'
        with self._registry() as db:
            binding = dict(receipt, runId=run_id, source='permission', digest=digest)
            db.execute('UPDATE operations SET data=? WHERE id=?', (json.dumps(binding), request_id))
        return receipt

    def owner_send(self, agent, text, request_id, author='motet'):
        """Queue text in a managed chat. ``author`` is 'owner' only for the exact words the owner typed in Motet's
        send box; those go in as typed with source 'owner'. Text Motet composed goes in with source 'motet', worded
        as Motet's (attribution.motet_worded), so it never reaches the chat as the owner typing it."""
        if author not in AUTHORS:
            _fail('invalid_request', 400)
        source = author
        delivered = text if author == 'owner' else motet_worded(text)
        digest = _text(delivered)
        old = self.lookup_message(request_id)
        if old:
            owned = self.ownership(agent[2:]) if isinstance(agent, str) and agent.startswith('c:') else None
            # A replay of a request stored before authors were recorded matches its original text and source.
            legacy = old.get('source') == 'owner' and old.get('author') is None and old.get('digest') == _text(text)
            if (not owned or old['runId'] != owned['runId']
                    or not (legacy or (old['source'] == source and old['digest'] == digest))):
                _fail('request_conflict')
            return self._public_message(old, agent)
        self._active()
        owned = self.ownership(agent[2:]) if isinstance(agent, str) and agent.startswith('c:') else None
        if not owned or not owned['admitted']:
            _fail('managed_not_ready')
        with self._registry() as db:
            row = self._row(db, owned['runId'])
        if self.verify_existing(agent[2:], FrozenFolder(**row['folder'])) is None:
            _fail('identity_unavailable')
        snapshot = self.status(owned['runId'])
        if snapshot['state'] not in ('ready', 'running', 'needs_attention') or not snapshot['identityVerified']:
            _fail('managed_not_ready')
        return self._public_message(self.enqueue(owned['runId'], request_id, source, delivered), agent)

    @staticmethod
    def _public_message(message, agent):
        state = message.get('state', 'unknown')
        text = message.get('text', '')
        if message.get('source') == 'motet':
            text = motet_words(text) if motet_words(text) is not None else text
        result = dict(request_id=message['requestId'], agent=agent, name='Claude', question=text,
                      state=PUBLIC_STATES.get(state, 'unknown'), reply=message.get('reply', ''),
                      error=message.get('error', ''), created=message.get('createdAt', 0),
                      transport=TRANSPORT, runtime='claude', delivery_state=state)
        if message.get('replyTruncated'):
            result['reply_truncated'] = True
        if message.get('textExpired'):
            result['text_expired'] = True
        author = 'motet' if message.get('source') == 'motet' else message.get('author')
        if author in AUTHORS:
            result['author'] = author
        return result

    def owner_snapshot(self):
        with self._registry() as db:
            runs = [self._row(db, ident) for (ident,) in db.execute('SELECT id FROM runs')] if db else []
        ready = False
        if self.enabled and not self.demo and not self.closed:
            # Only occupied runs (at most four) contact a live endpoint. Terminal
            # retained history is read from our registry without provider I/O.
            for row in runs:
                if not row['occupied']:
                    continue
                snapshot = self.status(row['runId'])
                if (row['admitted'] and snapshot['identityVerified']
                        and snapshot['state'] in ('ready', 'running', 'needs_attention')
                        and self.verify_existing(row['nativeId'], FrozenFolder(**row['folder']))):
                    ready = True
        rows = []
        with self._registry() as db:
            if db:
                for row in runs:
                    for (raw,) in db.execute("SELECT data FROM messages WHERE run=? AND source IN ('owner','motet')", (row['runId'],)):
                        rows.append(self._public_message(self._retained(json.loads(raw)), 'c:' + row['nativeId']))
        return dict(requests=sorted(rows, key=lambda row: row['created']), available=ready,
                    capabilities={'claude_managed': ready})

    def owner_capabilities(self, agent):
        ready = False
        managed_binding = None
        if self.enabled and not self.demo and not self.closed:
            owned = self.ownership(agent[2:]) if isinstance(agent, str) and agent.startswith('c:') else None
            if owned and owned['admitted']:
                # Durable exact native identity, never a title/folder guess. Keep
                # the binding on disconnection so the desk can read retained facts.
                managed_binding = dict(runId=owned['runId'])
                snapshot = self.status(owned['runId'])
                with self._registry() as db:
                    row = self._row(db, owned['runId'])
                ready = bool(snapshot['identityVerified'] and snapshot['state'] in ('ready', 'running', 'needs_attention')
                             and self.verify_existing(agent[2:], FrozenFolder(**row['folder'])))
        reason = '' if ready else 'This managed Claude session is unavailable for new messages.'
        unsupported = 'Live controls are unavailable for managed Claude sessions.'
        return dict(agent=agent, transport=TRANSPORT, managed=managed_binding, actions={
            'queue': dict(available=ready, reason=reason, mode='queue', maxCharacters=8000, maxPending=32),
            'steer': dict(supported=False, reason=unsupported, mode='new_instruction'),
            'interrupt': dict(supported=False, reason=unsupported, mode='interrupt'),
            'promote_queued': dict(available=False, reason=unsupported, mode='queued_promotion')})

    def any_ready(self):
        if not self.enabled or self.demo or self.closed:
            return False
        with self._registry() as db:
            natives = [self._row(db, ident)['nativeId'] for (ident,) in db.execute('SELECT id FROM runs WHERE occupied=1')
                       if self._row(db, ident)['admitted']] if db else []
        return any(self.owner_capabilities('c:' + native)['actions']['queue']['available'] for native in natives)

    def shutdown(self, run_id, request_id):
        self._active()
        if not canonical_uuid(request_id):
            _fail('invalid_request', 400)
        with self._registry() as db:
            self._row(db, run_id)
        try:
            self._request(run_id, 'shutdown', dict(requestId=request_id))
        except Exception:
            pass
        return self.status(run_id)

    def close(self):
        self.closed = True


class Adapter:
    def __init__(self, manager):
        self.manager = manager

    def preflight(self):
        return self.manager.preflight()

    def reserve(self, folder, attempt_id):
        return self.manager.reserve(folder, attempt_id)

    def launch_once(self, run_id, folder, record_native_receipt):
        return self.manager.launch_once(run_id, folder, record_native_receipt)

    def verify_existing(self, native_id, folder):
        return self.manager.verify_existing(native_id, folder)

    @staticmethod
    def first_request_id(run_id):
        return str(uuid.uuid5(uuid.UUID(run_id), 'watch-new-chat-first-instruction-v1'))

    def start_managed_work_once(self, run_id, instruction, claim_submission, record_turn_receipt):
        request_id = self.first_request_id(run_id)
        _text(instruction)
        old = self.manager.lookup_message(request_id)
        if old is None:
            claim_submission()
            self.manager.enqueue(run_id, request_id, 'new_chat', instruction)
        elif old.get('source') != 'new_chat' or old.get('digest') != _text(instruction):
            _fail('request_conflict')
        result = self.managed_execution_status(run_id)
        if result.turn_id:
            record_turn_receipt(result.turn_id)
        return result

    def managed_status(self, run_id):
        snapshot = self.manager.status(run_id)
        return dict(runId=run_id, state=snapshot['state'], recoveryRequired=snapshot['recoveryRequired'],
                    permissionsAvailable=any(p.get('canAllow') or p.get('canDeny') for p in snapshot['permissions']))

    def managed_execution_status(self, run_id):
        snapshot = self.manager.status(run_id)
        request_id = self.first_request_id(run_id)
        message = next((row for row in snapshot['messages'] if row['requestId'] == request_id), None)
        if message is None:
            message = self.manager.lookup_message(request_id)
        if message is None:
            return ExecutionResult('not_attempted', receipt_kind='native_user_message')
        state = message['state']
        receipt = request_id if message.get('nativeEcho') is True or state == 'completed' else None
        outcome = {'completed': 'completed', 'failed': 'failed', 'unknown': 'unknown',
                   'delivered': 'running', 'queued': 'unknown', 'dispatching': 'unknown'}.get(state, 'unknown')
        if snapshot['state'] == 'needs_attention' and receipt:
            outcome = 'needs_attention'
        return ExecutionResult(outcome, receipt, receipt_kind='native_user_message')

    def close(self):
        self.manager.close()
