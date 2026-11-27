"""One private, non-replaying supervisor for one Motet-owned Claude process.

Importing this module has no effects. The web process never adopts a PID or
restarts a native child: only this original supervisor can record its exit.

Platforms: macOS and Windows (MANAGED_PLATFORMS; step 8b.5 added Windows). Linux stays refused.

Private state. POSIX: the run folder is opened from "/" without following a link and every file in it is opened
relative to it and checked on its descriptor (this user's, no group or other bits, one name), and ``run.lock`` is
``flock``, as before. Windows (step 8b.1): the run folder is held through private_storage's no-link walk and checked
private on its handle, its files are opened or created private through that held folder, SQLite's companion files
follow private_storage's companion rule, and ``run.lock`` is private_storage's byte lock. The folder identity is read
from a handle there, as New chat's inventory records it.
"""
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from new_chat_types import MANAGED_PERMISSION_TIMEOUT_SECONDS as PERMISSION_SECONDS
from new_chat_types import MANAGED_CLAUDE_PLATFORMS as MANAGED_PLATFORMS   # Linux stays refused (8b.5 added Windows)
import private_storage as ps

PINNED_VERSION = '2.1.323'
PINNED_SHA256 = '5432918d904a9b6afef7bb79d20df24d76052eabc1f41bceb84091d21f8889a7'
MAX_FRAME = 256 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_NATIVE_LINE = 1024 * 1024
MAX_INPUT = 32 * 1024
MAX_REPLY = 16000
MAX_RETAINED = 4096
MAX_EVIDENCE_BYTES = 24 * 1024 * 1024
CONTROL_SECONDS = 30
SUPPORTED_TOOLS = frozenset(('Bash', 'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit',
                             'Glob', 'Grep', 'LS', 'WebFetch', 'WebSearch', 'Task', 'Agent',
                             'Skill', 'TodoWrite', 'ToolSearch'))
# Claude's "approve this plan?" prompt. Approving a plan also chooses how the edits are then made, which a bare allow
# does not, and this headless session has no app box for the owner to answer it in. So it is never supported here
# (never pending, never allowable): it is denied at once with this reason, which hands the plan back to the owner.
PLAN_TOOL = 'ExitPlanMode'
PLAN_DENIED = ('Motet cannot approve a plan: approving one also chooses how edits are made, and only the owner can '
               'do that. Do not carry out the plan. End this turn with the plan as your reply so the owner can review it.')
ERRORS = {
    'unsafe_state': (503, 'Managed session state is unavailable.'),
    'invalid_request': (400, 'The managed request is invalid.'),
    'unauthorized': (403, 'Managed session authentication failed.'),
    'conflict': (409, 'This request identity has already been used differently.'),
    'stale_permission': (409, 'This permission request is no longer pending.'),
    'unavailable': (503, 'The managed session cannot accept new work.'),
    'capacity': (429, 'The managed session queue is full.'),
    'already_running': (409, 'The managed session already has a supervisor.'),
    'launch_claimed': (409, 'The native launch was already claimed; recovery is required.'),
    'native_uncertain': (503, 'The native outcome is uncertain; it will not be repeated.'),
    'unsupported_build': (503, 'This Claude build is not verified for managed sessions.'),
    'folder_changed': (409, 'The selected folder changed. Choose it again.'),
}


class RunnerError(Exception):
    def __init__(self, code, message=None, status=None):
        default_status, default_message = ERRORS.get(code, ERRORS['unavailable'])
        self.code = code if code in ERRORS else 'unavailable'
        self.status = default_status if status is None else status
        self.message = default_message  # Never accept provider/error text.
        super().__init__(self.message)


def _fail(code):
    raise RunnerError(code)


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except RecursionError:
        raise ValueError() from None


def _load(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except RecursionError:
        raise ValueError() from None


def _private(info, directory=False):
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not kind or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _fail('unsafe_state')


def _directory(path, writable=False):
    """Reject symlinks in every path component, including non-private parents. Windows: the private run folder held
    through private_storage's walk (``writable`` lets files be created through it)."""
    path = os.path.abspath(os.fspath(path))
    if ps.WIN:
        # A hold kept for the supervisor's lifetime: only the run folder's own handle (``alone``), as the attachment
        # store keeps its folders; NTFS still refuses to rename the folders above it while it is open.
        return ps.open_folder(path, private=True, writable=writable, alone=True)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.split('/'):
            if component:
                nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        _private(os.fstat(fd), True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _close(handle):
    """Close the run folder or a file from ``_directory`` / ``_file``: a held folder on Windows, else a descriptor."""
    if handle is not None and not isinstance(handle, int):
        handle.close()
    elif handle is not None:
        os.close(handle)


def _windows_file(root, name, maximum, flags):
    """Windows ``_file``: through the held run folder. ``O_CREAT`` (always with ``O_EXCL`` here) makes a new private
    file and an existing name raises FileExistsError; otherwise the file is opened read-only, private with one name
    (a missing one raises FileNotFoundError). A read-only descriptor serves the byte lock as well."""
    fd = root.create(name) if flags & os.O_CREAT else root.open(name, single_link=True, private=True)
    try:
        if os.fstat(fd).st_size > maximum:
            _fail('unsafe_state')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(root_fd, name, maximum, *, flags=os.O_RDONLY):
    if ps.WIN:
        return _windows_file(root_fd, name, maximum, flags)
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root_fd)
    try:
        info = os.fstat(fd)
        _private(info)
        if info.st_size > maximum:
            _fail('unsafe_state')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _folder(folder):
    try:
        path = folder['path']
        if (not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path
                or type(folder['device']) is not int or type(folder['inode']) is not int):
            raise ValueError()
        if ps.WIN:
            # The same checks; the identity is read from a handle opened by path, as New chat's inventory records it
            # (a project folder may be on a network share or in OneDrive: not the private walk).
            if (not stat.S_ISDIR(os.lstat(path).st_mode)
                    or ps.path_identity(path) != (folder['device'], folder['inode'])):
                raise ValueError()
            return
        info = os.lstat(path)
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (folder['device'], folder['inode'])):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        _fail('folder_changed')


def _windows_pin():
    """Windows: the (SHA-256, size) of the build pinned for this machine's architecture, or None."""
    import claude_managed_windows as windows
    return windows.windows_pin()


def _pinned_sha256():
    """The pinned build's SHA-256: the Mac's (PINNED_SHA256) on POSIX, as before; on Windows the build for this
    machine's architecture."""
    if not ps.WIN:
        return PINNED_SHA256
    pin = _windows_pin()
    return pin[0] if pin else None


def _manifest(raw):
    data = _load(raw)
    keys = {'schemaVersion', 'runId', 'nativeId', 'incarnation', 'folder', 'executable', 'profile', 'createdAt'}
    if (not isinstance(data, dict) or set(data) != keys or type(data['schemaVersion']) is not int
            or data['schemaVersion'] != 1 or not all(_uuid(data[k]) for k in ('runId', 'nativeId', 'incarnation'))
            or data['profile'] not in ('normal', 'restricted_test')
            or not isinstance(data['folder'], dict)
            or set(data['folder']) != {'id', 'revision', 'label', 'path', 'device', 'inode'}
            or not isinstance(data['executable'], dict)
            or set(data['executable']) != {'path', 'sha256', 'version'}
            or not isinstance(data['createdAt'], (int, float)) or isinstance(data['createdAt'], bool)):
        _fail('unsafe_state')
    executable = data['executable']
    if (not isinstance(executable['path'], str) or not os.path.isabs(executable['path'])
            or executable['sha256'] != _pinned_sha256() or executable['version'] != PINNED_VERSION):
        _fail('unsupported_build')
    _folder(data['folder'])
    return data


def _open_state(run_dir, writable=False):
    root_fd = None
    try:
        root_fd = _directory(run_dir, writable)
        fd = _file(root_fd, 'manifest.json', 16384)
        try:
            manifest = _manifest(os.read(fd, 16385))
        finally:
            os.close(fd)
        fd = _file(root_fd, 'token', 256)
        try:
            token = os.read(fd, 257).decode('ascii').strip()
        finally:
            os.close(fd)
        if not re.fullmatch(r'[0-9a-f]{64}', token):
            _fail('unsafe_state')
        return root_fd, manifest, token
    except RunnerError:
        _close(root_fd)
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        _close(root_fd)
        _fail('unsafe_state')


def _validate_db(root_fd):
    if ps.WIN:
        # The ledger itself is ours (created private); SQLite makes its companions by name, so they follow
        # private_storage's companion rule, checked through the held run folder.
        os.close(_file(root_fd, 'ledger.sqlite3', 64 * 1024 * 1024))
        if not ps.sqlite_companions_private(os.path.join(root_fd.path, 'ledger.sqlite3'), folder=root_fd):
            _fail('unsafe_state')
        for suffix in ('-journal', '-wal', '-shm'):      # the same 64 MiB bound as POSIX
            try:
                fd = root_fd.open('ledger.sqlite3' + suffix, single_link=True)
            except FileNotFoundError:
                continue
            try:
                if os.fstat(fd).st_size > 64 * 1024 * 1024:
                    _fail('unsafe_state')
            finally:
                os.close(fd)
        return
    for name in ('ledger.sqlite3', 'ledger.sqlite3-journal', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm'):
        try:
            fd = _file(root_fd, name, 64 * 1024 * 1024)
            os.close(fd)
        except FileNotFoundError:
            if name == 'ledger.sqlite3':
                raise


def _meta(db, name, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
    return _load(row[0]) if row else default


def _set(db, name, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (name, _json(value)))


def _message(row):
    return {'requestId': row['id'], 'source': row['source'], 'text': row['text'] or '',
            'digest': row['digest'], 'state': row['state'], 'nativeEcho': bool(row['native_echo']),
            'reply': row['reply'] or '', 'replyTruncated': bool(row['reply_truncated']),
            'error': row['error'], 'createdAt': row['created_at'], 'terminalAt': row['terminal_at']}


def _decision_receipt(row):
    # A durable claim alone cannot prove that a native write completed.
    state = 'written' if row['decision_state'] == 'written' else 'unknown'
    columns = row.keys()
    return {'permissionId': row['id'], 'requestId': row['decision_id'], 'decision': row['decision'],
            'state': state, 'decisionState': state,
            'resolutionReason': row['resolution_reason'] if 'resolution_reason' in columns else None,
            'resolvedAt': row['resolved_at'] if 'resolved_at' in columns else None}


def _snapshot(db, manifest, disconnected=False, server_time=None):
    if _meta(db, 'identity') != {k: manifest[k] for k in ('runId', 'nativeId', 'incarnation')}:
        _fail('unsafe_state')
    state = _meta(db, 'state', 'uncertain')
    native_exit = _meta(db, 'nativeExit')
    if state not in ('starting', 'ready', 'running', 'needs_attention', 'exited', 'uncertain'):
        _fail('unsafe_state')
    if disconnected and not native_exit:
        state = 'uncertain'
    # Terminal historical details are bounded independently of active records.
    messages = [_message(r) for r in db.execute(
        'SELECT * FROM messages ORDER BY seq DESC LIMIT 32')][::-1]
    if disconnected and not native_exit:
        for message in messages:
            if message['state'] in ('dispatching', 'delivered'):
                message.update(state='unknown', error='native_uncertain')
    permissions = []
    # Unknown, unanswered rows are last-seen requests, not resolved history.
    unresolved = "(state='pending' OR (state='unknown' AND decision IS NULL))"
    permission_columns = {row[1] for row in db.execute('PRAGMA table_info(permissions)')}
    recent = "COALESCE(resolved_at,created_at) DESC,seq DESC" if 'resolved_at' in permission_columns else "seq DESC"
    for row in db.execute("SELECT * FROM permissions WHERE " + unresolved +
                          " OR seq IN (SELECT seq FROM permissions WHERE NOT " + unresolved +
                          " ORDER BY " + recent + " LIMIT 8) ORDER BY seq"):
        pending = row['state'] == 'pending' and not disconnected and state not in ('uncertain', 'exited')
        columns = row.keys()
        # Old retained ledgers remain readable without modifying them. Never
        # invent a historical resolution timestamp or a more specific reason.
        input_available = (bool(row['input_available']) if 'input_available' in columns
                           and row['input_available'] is not None else row['state'] not in ('unsupported', 'rejected'))
        decision_state = row['decision_state']
        if disconnected and decision_state == 'dispatching':
            decision_state = 'unknown'
        permissions.append({'id': row['id'], 'tool': row['tool'], 'input': _load(row['input']),
                            'inputAvailable': input_available,
                            'state': row['state'], 'canAllow': pending and input_available,
                            'canDeny': pending,
                            'createdAt': row['created_at'], 'expiresAt': row['deadline'],
                            'decision': row['decision'], 'decisionState': decision_state,
                            'resolutionReason': row['resolution_reason'] if 'resolution_reason' in columns else None,
                            'resolvedAt': row['resolved_at'] if 'resolved_at' in columns else None})
    snapshot = {'runId': manifest['runId'], 'nativeId': manifest['nativeId'],
            'incarnation': manifest['incarnation'], 'cwd': manifest['folder']['path'],
            'state': state, 'nativeExit': native_exit,
            'identityVerified': bool(_meta(db, 'identityVerified', False)),
            'messages': messages, 'permissions': permissions, 'timeoutSeconds': PERMISSION_SECONDS,
            'serverTime': time.time() if server_time is None else server_time, 'evidenceAvailable': True}
    if _meta(db, 'treeSurvivors', 'unseen') != 'unseen':
        snapshot['treeSurvivors'] = _meta(db, 'treeSurvivors')    # [] all ended; a list; None: not seen completely
    # Preserve all current approval evidence. Drop only older terminal previews
    # when JSON escaping would exceed the response budget; full rows stay durable.
    while len(_json(snapshot).encode('utf-8')) > MAX_RESPONSE - 1024:
        terminal = next((m for m in messages if m['state'] in ('completed', 'failed')), None)
        if terminal is None:
            _fail('capacity')
        messages.remove(terminal)
    return snapshot


def read_snapshot(run_dir):
    """Read durable evidence without creating files, reconnecting or launching.

    Nonterminal disconnected state is uncertainty, even if its last writer called
    it ready. This helper cannot certify that the original supervisor is alive.
    """
    root_fd = db = None
    try:
        root_fd, manifest, _ = _open_state(run_dir)
        _validate_db(root_fd)
        db = sqlite3.connect(Path(run_dir).absolute().as_uri() + '/ledger.sqlite3?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return _snapshot(db, manifest, disconnected=True)
    except RunnerError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        _fail('unsafe_state')
    finally:
        if db is not None:
            db.close()
        _close(root_fd)


def read_message(run_dir, request_id):
    """Read one retained message/tombstone; never enqueue or contact a process."""
    if not _uuid(request_id):
        _fail('invalid_request')
    root_fd = db = None
    try:
        root_fd, manifest, _ = _open_state(run_dir)
        _validate_db(root_fd)
        db = sqlite3.connect(Path(run_dir).absolute().as_uri() + '/ledger.sqlite3?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if _meta(db, 'identity') != {k: manifest[k] for k in ('runId', 'nativeId', 'incarnation')}:
            _fail('unsafe_state')
        row = db.execute('SELECT * FROM messages WHERE id=?', (request_id,)).fetchone()
        value = _message(row) if row else None
        if value and value['state'] in ('dispatching', 'delivered') and not _meta(db, 'nativeExit'):
            value['state'] = 'unknown'
            value['error'] = 'native_uncertain'
        return value
    except RunnerError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        _fail('unsafe_state')
    finally:
        if db is not None:
            db.close()
        _close(root_fd)


def read_native_process(run_dir):
    """What the web process needs to find this run's Claude when its supervisor no longer answers: the manifest's
    identity and executable, the native process the supervisor recorded (its ID and when it was started) and when
    the launch was claimed. Read-only; never contacts or signals anything. None when no process was recorded."""
    root_fd = db = None
    try:
        root_fd, manifest, _ = _open_state(run_dir)
        _validate_db(root_fd)
        db = sqlite3.connect(Path(run_dir).absolute().as_uri() + '/ledger.sqlite3?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if _meta(db, 'identity') != {k: manifest[k] for k in ('runId', 'nativeId', 'incarnation')}:
            _fail('unsafe_state')
        native, claim = _meta(db, 'nativeProcess'), _meta(db, 'launchClaim')
        if (not isinstance(native, dict) or not isinstance(claim, dict) or type(native.get('pid')) is not int
                or native['pid'] <= 1 or type(native.get('startedAt')) not in (int, float)
                or type(claim.get('at')) not in (int, float) or claim['at'] > native['startedAt']):
            return None
        return {'runId': manifest['runId'], 'nativeId': manifest['nativeId'], 'executable': manifest['executable']['path'],
                'pid': native['pid'], 'startedAt': native['startedAt'], 'claimedAt': claim['at'],
                'nativeExit': _meta(db, 'nativeExit')}
    except RunnerError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        _fail('unsafe_state')
    finally:
        if db is not None:
            db.close()
        _close(root_fd)


def read_decision(run_dir, permission_id, request_id, decision):
    """Read a retained exact decision claim after disconnection, never answer it.

    A committed claim without a completed pipe write is unknown. This function
    cannot authorize a new claim, resolve cancellation, or replay a native write.
    """
    if not _uuid(permission_id) or not _uuid(request_id) or decision not in ('allow', 'deny'):
        _fail('invalid_request')
    root_fd = db = None
    try:
        root_fd, manifest, _ = _open_state(run_dir)
        _validate_db(root_fd)
        db = sqlite3.connect(Path(run_dir).absolute().as_uri() + '/ledger.sqlite3?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if _meta(db, 'identity') != {k: manifest[k] for k in ('runId', 'nativeId', 'incarnation')}:
            _fail('unsafe_state')
        collision = db.execute('SELECT id FROM permissions WHERE decision_id=?', (request_id,)).fetchone()
        if (db.execute('SELECT 1 FROM messages WHERE id=?', (request_id,)).fetchone()
                or db.execute('SELECT 1 FROM shutdowns WHERE id=?', (request_id,)).fetchone()
                or (collision and collision['id'] != permission_id)):
            _fail('conflict')
        row = db.execute('SELECT * FROM permissions WHERE id=?', (permission_id,)).fetchone()
        if row and row['decision_id'] == request_id:
            if row['decision'] != decision:
                _fail('conflict')
            return _decision_receipt(row)
        if row and row['state'] != 'pending':
            _fail('stale_permission')
        return None
    except RunnerError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        _fail('unsafe_state')
    finally:
        if db is not None:
            db.close()
        _close(root_fd)


class NativeTransport:
    """Pinned native JSONL, bounded reads/writes, one owned Popen instance.

    POSIX: non-blocking pipes under a selector; Shutdown sends SIGTERM and waits, and the owner's Stop ends Claude's
    process group (``end_tree``). Windows (step 8b.2; on since
    8b.5): Claude runs in a kill-on-close job and its pipes are read and written by threads
    (claude_managed_windows: ``pipes`` and ``job`` are set), with the same line limit, budgets and deadlines;
    Shutdown closes its input, waits as long, then ends the whole job. See claude_managed_windows for why Claude
    ends with its supervisor there."""
    def __init__(self, manifest):
        self.manifest = manifest
        self.process = None
        self.selector = None
        self.buffer = bytearray()
        self.pipes = None        # Windows: claude_managed_windows.ThreadedPipes
        self.job = None          # Windows: claude_managed_windows.Job
        self.stop_requested = False

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def _args(self, exe):
        args = [exe, '--print', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--verbose', '--await-claim', '--session-id', self.manifest['nativeId'],
                '--permission-mode', 'default', '--permission-prompt-tool', 'stdio',
                '--replay-user-messages', '--include-partial-messages']
        if self.manifest['profile'] == 'restricted_test':
            args += ['--safe-mode', '--strict-mcp-config', '--tools', 'Write,Read']
        return args

    def _windows_start(self, exe):
        """Windows: open the pinned build through the no-link walk sharing reading only, hash it through that
        handle, and keep the handle open until Claude has been started from the same file in its kill-on-close
        job, so it cannot be replaced in between. Before Claude's thread is resumed, the file it was started from
        must be that hashed file (start_in_job's ``image``). The handle is closed in every path."""
        import claude_managed_windows as windows
        fd = None
        try:
            fd = windows.open_pinned(exe, _windows_pin())
            image = tuple(ps.file_identity(fd)[:2])
            _folder(self.manifest['folder'])
            self.process, self.job = windows.start_in_job(self._args(exe), self.manifest['folder']['path'],
                                                          image=image)
            try:
                self.pipes = windows.ThreadedPipes(self.process)
            except BaseException as error:
                self.close()              # ends the job: nothing runs without its pipes
                if isinstance(error, Exception) and not isinstance(error, (OSError, ValueError)):
                    raise OSError('pipe threads unavailable') from error    # a thread that would not start
                raise
        except RunnerError:
            raise
        except (OSError, ValueError):
            _fail('unsupported_build')
        finally:
            if fd is not None:
                os.close(fd)

    def start(self):
        if sys.platform not in MANAGED_PLATFORMS:
            _fail('unsupported_build')
        exe = self.manifest['executable']['path']
        if ps.WIN:
            return self._windows_start(exe)
        fd = None
        try:
            if os.path.realpath(exe) != exe:
                raise ValueError()
            fd = os.open(exe, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not before.st_mode & 0o111
                    or before.st_size > 1024 * 1024 * 1024):
                raise ValueError()
            digest = hashlib.sha256()
            deadline = time.monotonic() + 10
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                if time.monotonic() > deadline:
                    raise ValueError()
                digest.update(chunk)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or digest.hexdigest() != PINNED_SHA256):
                raise ValueError()
            _folder(self.manifest['folder'])
            args = self._args(exe)
            # Check the path again after hashing. No shell, inherited stdin or stderr.
            path_info = os.lstat(exe)
            if (path_info.st_dev, path_info.st_ino, path_info.st_mtime_ns, path_info.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError()
            self.process = subprocess.Popen(args, cwd=self.manifest['folder']['path'],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
        except RunnerError:
            raise
        except (OSError, ValueError):
            _fail('unsupported_build')
        finally:
            if fd is not None:
                os.close(fd)

    def write(self, frame):
        raw = (_json(frame) + '\n').encode('utf-8')
        if len(raw) > MAX_NATIVE_LINE or self.poll() is not None:
            raise EOFError()
        if self.pipes is not None:
            self.pipes.write(raw, 2)            # the same 2 s deadline, by a writer thread
            return
        view = memoryview(raw)
        deadline = time.monotonic() + 2
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while view:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise TimeoutError()
                try:
                    view = view[os.write(self.process.stdin.fileno(), view):]
                except BlockingIOError:
                    continue

    def _threaded_fill(self, timeout):
        """``read``'s fill from the reader thread: a chunk extends the buffer and the end of the stream is EOFError,
        every time once seen, as a POSIX read of an ended pipe is. When the child has exited with nothing queued,
        the reader is given up to two seconds to pass on what it already read and the end marker (a selector would
        still report those bytes as readable); if it is still blocked then (a program outside the child holds the
        pipe), that is the end too."""
        if self.pipes.eof:
            raise EOFError()
        chunk = self.pipes.get(timeout)
        if chunk == b'' and self.poll() is not None:
            chunk = self.pipes.final()
            if chunk == b'':
                chunk = None
        if chunk is None:
            self.pipes.eof = True
            raise EOFError()
        self.buffer.extend(chunk)

    def read(self, timeout=0):
        if b'\n' not in self.buffer and self.pipes is not None:
            self._threaded_fill(timeout)
        elif b'\n' not in self.buffer:
            if self.selector.select(timeout):
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise EOFError()
                self.buffer.extend(chunk)
            elif self.poll() is not None:
                raise EOFError()
        if len(self.buffer) > MAX_NATIVE_LINE:
            raise ValueError()
        if b'\n' not in self.buffer:
            return None
        raw, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        frame = _load(raw)
        if not isinstance(frame, dict):
            raise ValueError()
        return frame

    def poll(self):
        return self.process.poll() if self.process else None

    def end_tree(self, gentle=3, firm=2):
        """The owner's stop: end Claude and everything it started. Returns ``(code, survivors)``: Claude's exit
        code, and the ``[pid, start]`` identities of its tree still running afterwards (empty: all of it ended; None:
        the tree could not be seen, so its end is not confirmed). Raises subprocess.TimeoutExpired if Claude itself
        is still running.

        Windows: close its input, wait, then end the whole job (``terminate`` and ``wait``); nothing it started can
        outlive the job. POSIX: SIGTERM then SIGKILL to Claude's process group while Claude is this supervisor's own
        unreaped child (managed_process_tree.end), then reap it. A process that left the group is seen but never
        signalled (no individual process is signalled by its number), and is reported as a survivor. If Claude had
        already ended and been reaped, its group is not signalled either; whatever is left of it is reported."""
        if self.job is not None:
            self.terminate()
            return self.wait(timeout=gentle), []
        import managed_process_tree as tree
        pid = self.process.pid
        if self.process.returncode is not None:
            # Claude was already reaped: what it started can no longer all be seen (its group is not pinned, and a
            # program that left the group has lost its link to it). Unknown, never an empty tree.
            return self.process.returncode, None
        try:
            root = tree.table().get(pid)
        except tree.TreeError:
            root = None
        if root is None or root.ppid != os.getpid():
            # The tree cannot be seen: only the unreaped child's own group is signalled, and its end is unconfirmed.
            self._group_signal(signal.SIGTERM)
            try:
                return self.process.wait(timeout=gentle), None
            except subprocess.TimeoutExpired:
                self._group_signal(signal.SIGKILL)
                return self.process.wait(timeout=firm), None
        try:
            left = tree.end(root, group_signal=self._group_signal, gentle=gentle, firm=firm)
            survivors = sorted([row.pid, row.start] for row in left.values())
        except tree.TreeError:
            self._group_signal(signal.SIGKILL)
            survivors = None
        return self.process.wait(timeout=firm), survivors

    def _group_signal(self, number):
        """Signal Claude's process group, only while Claude is still this supervisor's unreaped child."""
        if self.process is not None and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, number)
            except (ProcessLookupError, PermissionError):
                pass

    def terminate(self):
        # Only the original unreaped Popen child; never load or signal a saved PID.
        if self.job is not None:
            self.stop_requested = True          # Windows: ``wait`` closes input, waits, then ends the whole job
            return
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def wait(self, timeout=3):
        if self.job is not None and self.stop_requested:
            import claude_managed_windows as windows
            return windows.stop_tree(self._close_input, lambda seconds: self.process.wait(timeout=seconds),
                                     self.job.terminate, timeout,
                                     input_blocked=lambda: self.pipes is not None and self.pipes.writing())
        return self.process.wait(timeout=timeout)

    def _close_input(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def close(self):
        if self.pipes is not None or self.job is not None:
            # Windows: end the job first (Claude ends with its supervisor), so its ends of the pipes close and no
            # pipe thread stays blocked; only then close the pipes, never one a thread is still in.
            if self.job is not None:
                try:
                    self.job.terminate()
                except OSError:
                    pass                  # closing its only handle below ends it all the same
                self.job.close()
            if self.pipes is not None:
                self.pipes.close()
            return
        if self.selector:
            self.selector.close()
        if self.process:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()


class Runner:
    def __init__(self, run_dir, *, transport_factory=None, clock=time.monotonic, wall=time.time):
        self.run_dir = Path(run_dir).absolute()
        self.factory = transport_factory or NativeTransport
        self.clock, self.wall = clock, wall
        self.lock = threading.RLock()
        self.root_fd = self.lock_fd = self.db = self.transport = None
        self.manifest = self.token = None
        self.stopping = False
        self.awaiting_retry = False     # an owner's stop could not confirm the end: keep listening for another try
        self.started = False
        self.active = None
        self.control_id = None
        self.control_result = None
        self.native_bytes = 0
        self.deadlines = {}

    def _db_open(self):
        try:
            fd = _file(self.root_fd, 'ledger.sqlite3', 64 * 1024 * 1024,
                       flags=os.O_RDWR | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            _validate_db(self.root_fd)
        else:
            os.close(fd)
        _validate_db(self.root_fd)
        self.db = sqlite3.connect(str(self.run_dir / 'ledger.sqlite3'), timeout=1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA journal_mode=DELETE')
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL,text TEXT,digest TEXT NOT NULL,state TEXT NOT NULL,
                    native_echo INTEGER NOT NULL DEFAULT 0,reply TEXT,reply_truncated INTEGER DEFAULT 0,
                    error TEXT,created_at REAL NOT NULL,terminal_at REAL);
                CREATE TABLE IF NOT EXISTS permissions(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                    native_id TEXT UNIQUE NOT NULL,tool_use_id TEXT NOT NULL,tool TEXT NOT NULL,
                    input TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,
                    deadline REAL NOT NULL,decision_id TEXT UNIQUE,decision TEXT,decision_state TEXT,
                    resolution_reason TEXT,resolved_at REAL,input_available INTEGER);
                CREATE TABLE IF NOT EXISTS shutdowns(id TEXT PRIMARY KEY,state TEXT NOT NULL);
            ''')
            # Additive migration for retained pre-contract ledgers. Nulls mean
            # the old runner did not record the fact, not that it happened now.
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(permissions)')}
            for name, kind in (('resolution_reason', 'TEXT'), ('resolved_at', 'REAL'), ('input_available', 'INTEGER')):
                if name not in columns:
                    self.db.execute('ALTER TABLE permissions ADD COLUMN ' + name + ' ' + kind)

    def start(self):
        """Claim durably before factory/start; a subsequent supervisor cannot replay."""
        with self.lock:
            if self.started:
                _fail('already_running')
            try:
                self.root_fd, self.manifest, self.token = _open_state(self.run_dir, writable=True)
                # macOS sun_path includes its trailing NUL in 104 bytes. Refuse
                # before claiming or starting a native process, not at bind time.
                # Windows has no socket path (a named pipe; see serve).
                if not ps.WIN and len(os.fsencode(str(self.run_dir / 'control.sock'))) >= 104:
                    _fail('unsafe_state')
                self.lock_fd = _file(self.root_fd, 'run.lock', 0, flags=os.O_RDWR)
                if ps.WIN:
                    if not ps.try_lock(self.lock_fd):
                        _fail('already_running')
                else:
                    import fcntl
                    try:
                        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        _fail('already_running')
                self._db_open()
                identity = {k: self.manifest[k] for k in ('runId', 'nativeId', 'incarnation')}
                old_identity = _meta(self.db, 'identity')
                if old_identity is not None and old_identity != identity:
                    _fail('unsafe_state')
                if _meta(self.db, 'launchClaim'):
                    if not _meta(self.db, 'nativeExit'):
                        self._uncertain()
                    _fail('launch_claimed')
                with self.db:
                    _set(self.db, 'identity', identity)
                    _set(self.db, 'state', 'starting')
                    _set(self.db, 'identityVerified', False)
                    _set(self.db, 'launchClaim', {'at': self.wall(), 'incarnation': self.manifest['incarnation']})
                self.transport = self.factory(self.manifest)
                self.transport.start()
                with self.db:
                    _set(self.db, 'nativeProcess', {'pid': getattr(self.transport, 'pid', None),
                         'incarnation': str(uuid.uuid4()), 'startedAt': self.wall()})
                self._control({'subtype': 'initialize'})
                _folder(self.manifest['folder'])
                claim = self._control({'subtype': 'claim_session', 'cwd': self.manifest['folder']['path'],
                                       'include_initialize': False})
                if claim.get('session_id') != self.manifest['nativeId'] or claim.get('cwd') != self.manifest['folder']['path']:
                    _fail('native_uncertain')
                status = self._control({'subtype': 'get_status'})
                selected = {}
                for section in status.get('sections', []):
                    for row in section.get('rows', []):
                        if row.get('label') in ('Session ID', 'cwd'):
                            if row['label'] in selected:
                                _fail('native_uncertain')
                            selected[row['label']] = row.get('value')
                if selected != {'Session ID': self.manifest['nativeId'], 'cwd': self.manifest['folder']['path']}:
                    _fail('native_uncertain')
                with self.db:
                    _set(self.db, 'identityVerified', True)
                    _set(self.db, 'state', 'ready')
                self.started = True
                return self.snapshot()
            except RunnerError:
                if self.transport is not None and self.db is not None:
                    self._uncertain()
                raise
            except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, EOFError, TimeoutError):
                if self.db is not None:
                    self._uncertain()
                _fail('native_uncertain')

    def _write(self, frame):
        try:
            self.transport.write(frame)
        except (OSError, ValueError, EOFError, TimeoutError):
            self._uncertain()
            _fail('native_uncertain')

    def _control(self, body):
        self.control_id = str(uuid.uuid4())
        self.control_result = None
        self._write({'type': 'control_request', 'request_id': self.control_id, 'request': body})
        deadline = self.clock() + CONTROL_SECONDS
        while self.control_result is None:
            if self.clock() >= deadline:
                _fail('native_uncertain')
            frame = self.transport.read(min(.1, max(0, deadline - self.clock())))
            if frame is not None:
                self._event(frame)
            if self.transport.poll() is not None:
                _fail('native_uncertain')
        response, self.control_result, self.control_id = self.control_result, None, None
        if response.get('subtype') != 'success' or not isinstance(response.get('response', {}), dict):
            _fail('native_uncertain')
        return response.get('response', {})

    def _uncertain(self):
        with self.db:
            _set(self.db, 'state', 'uncertain')
            self.db.execute("UPDATE messages SET state='unknown',error='native_uncertain' WHERE state IN ('dispatching','delivered')")
            self.db.execute("UPDATE permissions SET state='unknown' WHERE state='pending'")
        self.deadlines.clear()

    def snapshot(self):
        with self.lock:
            return _snapshot(self.db, self.manifest, server_time=self.wall())

    def command(self, command, params):
        with self.lock:
            if not isinstance(params, dict):
                _fail('invalid_request')
            if command == 'status' and not params:
                return self.snapshot()
            if command == 'enqueue' and set(params) == {'requestId', 'source', 'text'}:
                return self.enqueue(**params)
            if command == 'decision' and set(params) == {'permissionId', 'requestId', 'decision'}:
                return self.decide(**params)
            if command == 'shutdown' and set(params) == {'requestId'}:
                return self.shutdown(**params)
            if command == 'stop' and set(params) == {'requestId', 'by'} and params['by'] == 'owner':
                return self.stop(params['requestId'])
            _fail('invalid_request')

    def _prune(self):
        cutoff = self.wall() - 7 * 86400
        with self.db:
            self.db.execute("UPDATE messages SET text=NULL,reply=NULL WHERE state IN ('completed','failed') AND terminal_at<?", (cutoff,))
        # Tombstones deliberately remain past 30 days; bounded capacity fails closed.

    def _storage_available(self, extra=0):
        # Keep data below the read-only helper's 64 MiB file limit, allowing
        # SQLite pages/indexes/tombstones overhead. Reserve every unfinished
        # message's largest possible reply before its instruction is accepted.
        messages = self.db.execute("""SELECT COALESCE(SUM(
            COALESCE(length(CAST(text AS BLOB)),0) +
            COALESCE(length(CAST(reply AS BLOB)),0) +
            CASE WHEN state IN ('queued','dispatching','delivered','unknown') THEN ? ELSE 0 END
            ),0) FROM messages""", (MAX_REPLY * 4,)).fetchone()[0]
        permissions = self.db.execute('SELECT COALESCE(SUM(length(CAST(input AS BLOB))),0) FROM permissions').fetchone()[0]
        return messages + permissions + extra <= MAX_EVIDENCE_BYTES

    def enqueue(self, requestId, source, text):
        with self.lock:
            if (not _uuid(requestId) or source not in ('new_chat', 'owner', 'motet') or not isinstance(text, str)
                    or not text.strip() or len(text) > 8000 or '\x00' in text):
                _fail('invalid_request')
            try:
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            except UnicodeError:
                _fail('invalid_request')
            previous = self.db.execute('SELECT * FROM messages WHERE id=?', (requestId,)).fetchone()
            if previous:
                if previous['source'] != source or previous['digest'] != digest:
                    _fail('conflict')
                return _message(previous)
            if (self.db.execute('SELECT 1 FROM permissions WHERE decision_id=?', (requestId,)).fetchone()
                    or self.db.execute('SELECT 1 FROM shutdowns WHERE id=?', (requestId,)).fetchone()):
                _fail('conflict')
            self._prune()
            if (not self.started or self.stopping or _meta(self.db, 'state') not in ('ready', 'running', 'needs_attention')
                    or self.transport.poll() is not None or not _meta(self.db, 'identityVerified')):
                _fail('unavailable')
            if (self.db.execute("SELECT COUNT(*) FROM messages WHERE state IN ('queued','dispatching','delivered','unknown')").fetchone()[0] >= 32
                    or self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] >= MAX_RETAINED
                    or not self._storage_available(len(text.encode('utf-8')) + MAX_REPLY * 4)):
                _fail('capacity')
            _folder(self.manifest['folder'])
            with self.db:
                self.db.execute('INSERT INTO messages(id,source,text,digest,state,created_at) VALUES(?,?,?,?,?,?)',
                                (requestId, source, text, digest, 'queued', self.wall()))
            self._dispatch()
            return _message(self.db.execute('SELECT * FROM messages WHERE id=?', (requestId,)).fetchone())

    def _dispatch(self):
        if self.active or self.stopping or _meta(self.db, 'state') not in ('ready', 'running'):
            return
        row = self.db.execute("SELECT * FROM messages WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
        if row is None:
            return
        try:
            _folder(self.manifest['folder'])
        except RunnerError:
            self._uncertain()
            raise
        self.active = row['id']
        self.native_bytes = 0
        with self.db:
            self.db.execute("UPDATE messages SET state='dispatching' WHERE id=?", (self.active,))
            _set(self.db, 'state', 'running')
        self._write({'type': 'user', 'session_id': self.manifest['nativeId'], 'uuid': row['id'],
                     'message': {'role': 'user', 'content': row['text']}, 'parent_tool_use_id': None})

    def _event(self, frame):
        if not isinstance(frame, dict):
            _fail('native_uncertain')
        if frame.get('session_id') not in (None, self.manifest['nativeId']):
            _fail('native_uncertain')
        kind = frame.get('type')
        if kind == 'control_response':
            response = frame.get('response', {})
            if not isinstance(response, dict):
                _fail('native_uncertain')
            if self.control_id is not None and response.get('request_id') == self.control_id:
                if self.control_result is not None:
                    _fail('native_uncertain')
                self.control_result = response
            else:
                self._permission_response_echo(response)
        elif kind == 'control_request':
            self._permission(frame)
        elif kind == 'control_cancel_request':
            native = frame.get('request_id')
            with self.db:
                self.db.execute("UPDATE permissions SET state='cancelled',resolution_reason='withdrawn',resolved_at=? WHERE native_id=? AND state='pending'", (self.wall(), native))
            self.deadlines.pop(native, None)
            self._phase()
        elif kind == 'user':
            # Tool results also use user frames; they are not instruction receipts.
            content = frame.get('message', {}).get('content')
            if isinstance(content, str):
                if (not self.active or frame.get('session_id') != self.manifest['nativeId'] or frame.get('uuid') != self.active):
                    _fail('native_uncertain')
                row = self.db.execute('SELECT * FROM messages WHERE id=?', (self.active,)).fetchone()
                if hashlib.sha256(content.encode('utf-8')).hexdigest() != row['digest']:
                    _fail('native_uncertain')
                with self.db:
                    self.db.execute("UPDATE messages SET state='delivered',native_echo=1 WHERE id=? AND state='dispatching'", (self.active,))
        elif kind == 'result':
            if not self.active or frame.get('session_id') != self.manifest['nativeId']:
                _fail('native_uncertain')
            # A result belongs only to this serialized active native turn.
            row = self.db.execute('SELECT state FROM messages WHERE id=?', (self.active,)).fetchone()
            if row['state'] not in ('dispatching', 'delivered') or not self.started:
                _fail('native_uncertain')
            reply = frame.get('result', '')
            if not isinstance(reply, str):
                _fail('native_uncertain')
            failed = frame.get('is_error') is True or frame.get('subtype', '').startswith('error')
            with self.db:
                self.db.execute('UPDATE messages SET state=?,native_echo=1,reply=?,reply_truncated=?,error=?,terminal_at=? WHERE id=?',
                                ('failed' if failed else 'completed', reply[:MAX_REPLY], len(reply) > MAX_REPLY,
                                 'native_failed' if failed else None, self.wall(), self.active))
                # A result ends this turn's unresolved permission callbacks.
                self.db.execute("UPDATE permissions SET state='cancelled',resolution_reason='turn_ended',resolved_at=? WHERE state='pending'", (self.wall(),))
            self.active = None
            self.deadlines.clear()
            self._phase()
            self._dispatch()
        elif kind in ('assistant', 'stream_event'):
            if not self.started or not self.active or frame.get('session_id') != self.manifest['nativeId']:
                _fail('native_uncertain')
        elif kind == 'command_lifecycle':
            # The pinned CLI emits command lifecycle metadata around a turn.
            # It is neither a user acknowledgement nor execution completion;
            # discard its payload and require this exact native session.
            if frame.get('session_id') != self.manifest['nativeId']:
                _fail('native_uncertain')
        elif kind in ('system', 'rate_limit_event', 'tool_progress', 'tool_use_summary', 'auth_status'):
            # Native account/status/configuration payloads are intentionally discarded.
            if frame.get('session_id') not in (None, self.manifest['nativeId']):
                _fail('native_uncertain')
        else:
            _fail('native_uncertain')

    def _phase(self):
        if _meta(self.db, 'state') in ('uncertain', 'exited'):
            return
        pending = self.db.execute("SELECT 1 FROM permissions WHERE state='pending' LIMIT 1").fetchone()
        with self.db:
            _set(self.db, 'state', 'needs_attention' if pending else ('running' if self.active else 'ready'))

    def _permission(self, frame):
        native = frame.get('request_id')
        request = frame.get('request', {})
        if (not isinstance(native, str) or not 1 <= len(native) <= 256 or not isinstance(request, dict)):
            _fail('native_uncertain')
        subtype = request.get('subtype')
        tool = request.get('tool_name')
        use = request.get('tool_use_id')
        if subtype != 'can_use_tool':
            # There is no implicit answer for native questions/unknown controls.
            self._write({'type': 'control_response', 'response': {'subtype': 'error',
                         'request_id': native, 'error': 'This native request is unsupported by Motet.'}})
            return
        if (not isinstance(tool, str) or not 1 <= len(tool) <= 128 or not isinstance(use, str)
                or not 1 <= len(use) <= 256 or not isinstance(request.get('input'), dict)):
            _fail('native_uncertain')
        original = _json(request['input'])
        digest = hashlib.sha256(_json({'tool': tool, 'use': use, 'input': request['input']}).encode('utf-8')).hexdigest()
        previous = self.db.execute('SELECT * FROM permissions WHERE native_id=?', (native,)).fetchone()
        if previous:
            if previous['digest'] != digest:
                _fail('native_uncertain')
            return  # Native replay cannot produce a second permission response.
        count = self.db.execute("SELECT COUNT(*) FROM permissions WHERE state='pending'").fetchone()[0]
        retained = self.db.execute('SELECT COUNT(*) FROM permissions').fetchone()[0]
        if retained >= MAX_RETAINED:
            _fail('native_uncertain')
        reason = ('inactive_turn' if not self.started or self.active is None else
                  'unsupported_tool' if tool not in SUPPORTED_TOOLS or tool == PLAN_TOOL else
                  'oversized' if len(original.encode('utf-8')) > MAX_INPUT else
                  'capacity' if count >= 8 or not self._storage_available(len(original.encode('utf-8'))) else None)
        accepted = reason is None
        public_id = str(uuid.uuid4())
        created_at = self.wall()
        with self.db:
            self.db.execute('''INSERT INTO permissions(id,native_id,tool_use_id,tool,input,digest,state,created_at,deadline,input_available)
                               VALUES(?,?,?,?,?,?,?,?,?,?)''',
                            (public_id, native, use, tool, original if accepted else '{}', digest,
                             'pending' if accepted else 'rejected', created_at, created_at + PERMISSION_SECONDS, int(accepted)))
        if accepted:
            self.deadlines[native] = self.clock() + PERMISSION_SECONDS
            self._phase()
        else:
            self._decide_native(public_id, 'deny', None, 'unsupported', resolution_reason=reason)

    @staticmethod
    def _permission_payload(row):
        if row['decision'] == 'allow':
            return {'behavior': 'allow', 'updatedInput': _load(row['input'])}
        if 'resolution_reason' in row.keys() and row['resolution_reason'] == 'owner_stop':
            # The owner stopped this chat in Motet: said as the owner's act, not as Motet's own refusal.
            return {'behavior': 'deny', 'message': 'The owner stopped this chat in Motet, so this request was denied.'}
        if row['tool'] == PLAN_TOOL:
            return {'behavior': 'deny', 'message': PLAN_DENIED}
        return {'behavior': 'deny', 'message': 'Motet denied this request. Do not retry without a new owner instruction.'}

    def _permission_response_echo(self, response):
        # With replay-user-messages, the pinned CLI also echoes our permission
        # response. Only a retained, committed decision can explain that frame.
        # An exact echo is metadata: never grant a permit, change a claim/receipt,
        # recover uncertainty, or treat it as evidence that a tool completed.
        if (set(response) != {'subtype', 'request_id', 'response'}
                or response.get('subtype') != 'success'
                or not isinstance(response.get('request_id'), str)):
            _fail('native_uncertain')
        row = self.db.execute('SELECT * FROM permissions WHERE native_id=?', (response['request_id'],)).fetchone()
        if (row is None or row['decision'] not in ('allow', 'deny')
                or row['decision_state'] not in ('dispatching', 'written', 'unknown')
                or row['state'] == 'pending'):
            _fail('native_uncertain')
        expected = self._permission_payload(row)
        # Compare immutable JSON values with stable object-key order. Encoding
        # preserves distinctions such as true versus 1 that Python == collapses.
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':'), allow_nan=False)
        if canonical(response['response']) != canonical(expected):
            _fail('native_uncertain')

    def _decide_native(self, permission_id, decision, request_id, reason=None, resolution_reason=None):
        row = self.db.execute('SELECT * FROM permissions WHERE id=?', (permission_id,)).fetchone()
        with self.db:
            self.db.execute('UPDATE permissions SET state=?,decision_id=?,decision=?,decision_state=?,resolution_reason=?,resolved_at=? WHERE id=?',
                            ('decided' if reason is None else reason, request_id, decision, 'dispatching',
                             resolution_reason or {'expired': 'timeout', 'shutdown': 'shutdown'}.get(reason, 'owner'),
                             self.wall(), permission_id))
        self.deadlines.pop(row['native_id'], None)
        row = dict(self.db.execute('SELECT * FROM permissions WHERE id=?', (permission_id,)).fetchone())
        response = self._permission_payload(row)
        try:
            self._write({'type': 'control_response', 'response': {'subtype': 'success',
                         'request_id': row['native_id'], 'response': response}})
        except RunnerError:
            with self.db:
                self.db.execute("UPDATE permissions SET decision_state='unknown' WHERE id=?", (permission_id,))
            raise
        with self.db:
            self.db.execute("UPDATE permissions SET decision_state='written' WHERE id=?", (permission_id,))
        self._phase()
        return _decision_receipt(self.db.execute('SELECT * FROM permissions WHERE id=?', (permission_id,)).fetchone())

    def decide(self, permissionId, requestId, decision):
        with self.lock:
            if not _uuid(permissionId) or not _uuid(requestId) or decision not in ('allow', 'deny'):
                _fail('invalid_request')
            row = self.db.execute('SELECT * FROM permissions WHERE id=?', (permissionId,)).fetchone()
            collision = self.db.execute('SELECT id FROM permissions WHERE decision_id=?', (requestId,)).fetchone()
            if (self.db.execute('SELECT 1 FROM messages WHERE id=?', (requestId,)).fetchone()
                    or self.db.execute('SELECT 1 FROM shutdowns WHERE id=?', (requestId,)).fetchone()
                    or (collision and collision['id'] != permissionId)):
                _fail('conflict')
            if row and row['decision_id'] == requestId:
                if row['decision'] != decision:
                    _fail('conflict')
                return _decision_receipt(row)
            if not row or row['state'] != 'pending':
                _fail('stale_permission')
            if _meta(self.db, 'state') in ('uncertain', 'exited') or self.stopping:
                _fail('unavailable')
            if self.clock() >= self.deadlines.get(row['native_id'], 0):
                self._decide_native(permissionId, 'deny', None, 'expired')
                _fail('stale_permission')
            return self._decide_native(permissionId, decision, requestId)

    def _exit(self, code):
        with self.db:
            _set(self.db, 'nativeExit', {'confirmed': True, 'code': code, 'incarnation': self.manifest['incarnation']})
            _set(self.db, 'state', 'exited')
            self.db.execute("UPDATE messages SET state='unknown',error='native_exited' WHERE state IN ('dispatching','delivered')")
            self.db.execute("UPDATE messages SET state='failed',error='native_exited',terminal_at=? WHERE state='queued'", (self.wall(),))
            self.db.execute("UPDATE permissions SET state='cancelled',resolution_reason='native_exit',resolved_at=? WHERE state IN ('pending','unknown') AND decision IS NULL", (self.wall(),))
        self.deadlines.clear()

    def step(self, timeout=0):
        with self.lock:
            if not self.transport or _meta(self.db, 'nativeExit') is not None:
                return
            try:
                # Read bounded batches to keep IPC/deadline work schedulable.
                for n in range(32):
                    frame = self.transport.read(timeout if n == 0 else 0)
                    if frame is None:
                        break
                    self.native_bytes += len(_json(frame).encode('utf-8'))
                    if self.native_bytes > 16 * 1024 * 1024:
                        _fail('native_uncertain')
                    if _meta(self.db, 'state') != 'uncertain':
                        self._event(frame)
                for native, deadline in list(self.deadlines.items()):
                    if self.clock() >= deadline:
                        row = self.db.execute("SELECT id FROM permissions WHERE native_id=? AND state='pending'", (native,)).fetchone()
                        if row:
                            self._decide_native(row['id'], 'deny', None, 'expired')
            except (RunnerError, OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, TimeoutError):
                self._uncertain()
            code = self.transport.poll()
            if code is not None:
                self._exit(code)

    def shutdown(self, requestId):
        with self.lock:
            if not _uuid(requestId):
                _fail('invalid_request')
            previous = self.db.execute('SELECT state FROM shutdowns WHERE id=?', (requestId,)).fetchone()
            if previous:
                return {'requestId': requestId, 'state': previous['state'], 'nativeExit': _meta(self.db, 'nativeExit')}
            if (self.db.execute('SELECT 1 FROM messages WHERE id=?', (requestId,)).fetchone()
                    or self.db.execute('SELECT 1 FROM permissions WHERE decision_id=?', (requestId,)).fetchone()):
                _fail('conflict')
            self.stopping = True
            with self.db:
                self.db.execute("INSERT INTO shutdowns VALUES(?,'dispatching')", (requestId,))
                self.db.execute("UPDATE messages SET state='failed',error='shutdown',terminal_at=? WHERE state='queued'", (self.wall(),))
            for row in self.db.execute("SELECT id FROM permissions WHERE state='pending'").fetchall():
                if _meta(self.db, 'state') == 'uncertain':
                    break
                with contextlib.suppress(RunnerError):
                    self._decide_native(row['id'], 'deny', None, 'shutdown')
            try:
                self.transport.terminate()
                code = self.transport.wait(timeout=3)
                self._exit(code)
            except (OSError, TimeoutError, subprocess.TimeoutExpired):
                self._uncertain()
            state = 'exited' if _meta(self.db, 'nativeExit') else 'unknown'
            with self.db:
                self.db.execute('UPDATE shutdowns SET state=? WHERE id=?', (state, requestId))
            return {'requestId': requestId, 'state': state, 'nativeExit': _meta(self.db, 'nativeExit')}

    def stop(self, requestId):
        """The owner's stop of this chat. As ``shutdown``, but said as the owner's: each waiting permission request
        is denied as stopped by the owner (resolution ``owner_stop``) and each queued message fails as ``owner_stop``.
        Claude's process group is ended, firmly (NativeTransport.end_tree), and what of its tree outlived Claude is
        recorded (``treeSurvivors``), so the stop is not confirmed while any of it runs. If Claude's own end cannot
        be confirmed the state is uncertain and this supervisor keeps listening until Claude's exit is recorded, so a
        stop with a new request ID tries again. A request ID is acted on once; its replay returns its receipt."""
        with self.lock:
            if not _uuid(requestId):
                _fail('invalid_request')
            previous = self.db.execute('SELECT state FROM shutdowns WHERE id=?', (requestId,)).fetchone()
            if previous:
                return {'requestId': requestId, 'state': previous['state'], 'nativeExit': _meta(self.db, 'nativeExit')}
            if (self.db.execute('SELECT 1 FROM messages WHERE id=?', (requestId,)).fetchone()
                    or self.db.execute('SELECT 1 FROM permissions WHERE decision_id=?', (requestId,)).fetchone()):
                _fail('conflict')
            self.stopping = True
            with self.db:
                self.db.execute("INSERT INTO shutdowns VALUES(?,'dispatching')", (requestId,))
                self.db.execute("UPDATE messages SET state='failed',error='owner_stop',terminal_at=? WHERE state='queued'", (self.wall(),))
            if _meta(self.db, 'nativeExit') is None:
                for row in self.db.execute("SELECT id FROM permissions WHERE state='pending'").fetchall():
                    if _meta(self.db, 'state') == 'uncertain':
                        break
                    with contextlib.suppress(RunnerError):
                        self._decide_native(row['id'], 'deny', None, 'shutdown', resolution_reason='owner_stop')
                # Unknown until the tree is seen to end: a wait that fails below leaves it unknown, never empty.
                self._survivors(None)
                try:
                    code, survivors = self.transport.end_tree()
                    self._survivors(survivors)
                    self._exit(code)
                except (OSError, TimeoutError, subprocess.TimeoutExpired):
                    self._uncertain()
            elif _meta(self.db, 'treeSurvivors', 'unseen') == 'unseen':
                # Claude had already ended by itself: what it started cannot all be seen any more. Unknown.
                self._survivors(None)
            state = 'exited' if _meta(self.db, 'nativeExit') else 'unknown'
            self.awaiting_retry = state != 'exited'
            with self.db:
                self.db.execute('UPDATE shutdowns SET state=? WHERE id=?', (state, requestId))
            return {'requestId': requestId, 'state': state, 'nativeExit': _meta(self.db, 'nativeExit')}

    def _survivors(self, survivors):
        """Record what of Claude's tree outlived its end (None: it could not be seen, or more than 64 did). Never
        empty unless every process of the tree was seen to end; the owner's stop is confirmed only then."""
        with self.db:
            _set(self.db, 'treeSurvivors', survivors if survivors is not None and len(survivors) <= 64 else None)

    def exchange(self, connection):
        """One bounded, authenticated command; secrets never appear in replies."""
        try:
            connection.settimeout(2)
            raw = bytearray()
            deadline = time.monotonic() + 2
            while b'\n' not in raw:
                connection.settimeout(max(.001, deadline - time.monotonic()))
                chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(raw)))
                if not chunk or len(raw) + len(chunk) > MAX_FRAME or time.monotonic() >= deadline:
                    _fail('invalid_request')
                raw.extend(chunk)
            line, _, remainder = raw.partition(b'\n')
            if remainder:
                _fail('invalid_request')
            data = _load(line)
            if (not isinstance(data, dict) or set(data) != {'token', 'command', 'params'}
                    or not isinstance(data['token'], str) or not isinstance(data['command'], str)):
                _fail('invalid_request')
            if not hmac.compare_digest(data['token'], self.token):
                _fail('unauthorized')
            value = self.command(data['command'], data['params'])
            response = {'ok': True, 'value': value}
        except RunnerError as exc:
            response = {'ok': False, 'error': {'code': exc.code, 'message': exc.message, 'status': exc.status}}
        except (OSError, ValueError, TypeError, UnicodeError, sqlite3.Error):
            response = {'ok': False, 'error': {'code': 'invalid_request', 'message': ERRORS['invalid_request'][1], 'status': 400}}
        raw = (_json(response) + '\n').encode('utf-8')
        if len(raw) > MAX_RESPONSE:
            raw = (_json({'ok': False, 'error': {'code': 'capacity', 'message': ERRORS['capacity'][1], 'status': 429}}) + '\n').encode()
        with contextlib.suppress(OSError):
            connection.settimeout(2)
            connection.sendall(raw)

    def close(self):
        """Close supervisor handles only. Never infer exit or kill a possible orphan."""
        with self.lock:
            if self.transport is not None:
                with contextlib.suppress(OSError):
                    self.transport.close()
            if self.db is not None:
                self.db.close()
                self.db = None
            for name in ('lock_fd', 'root_fd'):
                fd = getattr(self, name)
                if fd is not None:
                    if ps.WIN and name == 'lock_fd':
                        ps.close_lock(fd)     # Windows may keep a byte lock a while after a close
                    else:
                        _close(fd)
                    setattr(self, name, None)


CONTROL_FILE = 'control.json'        # Windows: the supervisor's pipe name and process ID, in the private run folder


def _serve_loop(runner, endpoint):
    """Windows (and the portable seam tests): the supervisor's loop over an endpoint (``accept(seconds)`` gives a
    connection or None): step the native transport, stop after a recorded native exit, and answer one connection at
    a time through ``exchange``. It is a copy of the POSIX loop in ``serve``, which keeps its own Unix socket code;
    a change to either loop belongs in both."""
    # An owner's stop that could not confirm the end leaves the supervisor listening (and stepping, which records the
    # exit when it comes), so the owner's next stop can try again; it ends once the exit is recorded.
    while not runner.stopping or runner.awaiting_retry:
        runner.step()
        if _meta(runner.db, 'nativeExit') is not None:
            break
        connection = endpoint.accept(.05)
        if connection is None:
            continue
        with connection:
            runner.exchange(connection)
    return 0


def _record_endpoint(runner, name):
    """Windows: write the pipe's name and this supervisor's process ID to the private run folder, after the pipe
    exists, through the held folder (private from the start). Returns the file's identity, for its removal."""
    fd = runner.root_fd.create(CONTROL_FILE)
    try:
        raw = _json({'pipe': name, 'pid': os.getpid()}).encode('ascii')
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        return ps.file_identity(fd)
    finally:
        os.close(fd)


def _serve_windows(run_dir):
    """Windows: ``serve`` over a named pipe (claude_managed_windows.PipeServer) in place of the Unix socket. The
    record of its name is removed when the supervisor ends, if it is still the one written; a stale one names a
    pipe that ended with its supervisor."""
    import claude_managed_windows as windows
    runner = Runner(run_dir)
    endpoint = recorded = None
    try:
        runner.start()
        endpoint = windows.PipeServer()
        recorded = _record_endpoint(runner, endpoint.name)
        return _serve_loop(runner, endpoint)
    except (RunnerError, OSError, sqlite3.Error):
        return 1
    finally:
        if endpoint is not None:
            endpoint.close()
        if recorded is not None:
            with contextlib.suppress(OSError):
                runner.root_fd.remove(CONTROL_FILE, lambda fd: ps.file_identity(fd)[:2] == recorded[:2])
        runner.close()


def serve(run_dir):
    if ps.WIN:
        return _serve_windows(run_dir)
    runner = Runner(run_dir)
    listener = None
    bound = None
    try:
        os.umask(0o077)
        runner.start()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = str(runner.run_dir / 'control.sock')
        listener.bind(path)  # Never replace an existing endpoint.
        os.chmod(path, 0o600)
        bound = os.lstat(path)
        listener.listen(8)
        listener.settimeout(.05)
        # This loop's Windows twin is _serve_loop (over a named pipe); a change here belongs there too.
        while not runner.stopping or runner.awaiting_retry:
            runner.step()
            if _meta(runner.db, 'nativeExit') is not None:
                break
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                runner.exchange(connection)
        return 0
    except (RunnerError, OSError, sqlite3.Error):
        return 1
    finally:
        if listener:
            listener.close()
        if bound is not None:
            with contextlib.suppress(OSError):
                current = os.lstat(runner.run_dir / 'control.sock')
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    os.unlink(runner.run_dir / 'control.sock')
        runner.close()


if __name__ == '__main__':
    raise SystemExit(serve(sys.argv[1]) if len(sys.argv) == 2 else 2)
