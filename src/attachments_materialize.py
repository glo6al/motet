"""Disabled, trusted-only workspace copy primitives. No owner/native transport.

The caller owns durable Send authority, reservations and request/source fences.
It supplies ledger callbacks and live resource leases; no browser path or general
RPC belongs here. This cooperative local-filesystem profile does not isolate
hostile code running under the same OS user.

POSIX opens every folder and file by descriptor without following a link, as
before. Windows holds the workspace, Git and attachment folders through
private_storage's no-link walk and makes, opens, removes and flushes through
those held folders. Identities come from handles (volume serial and file
index, kept within the store's range), the workspace and Git folders must be
changeable by nobody but their owner (the Windows form of POSIX's owner and
0o022 rule), and the attachment folders and copies must be private. The Git
exclude file is appended through an append-only handle with the same rule, and
instead of a byte lock on it (Windows byte locks are mandatory and would stop
Git reading it) the workspace's own private resource lock serialises writers.
Git itself (the check-ignore proof) is not yet ported to Windows.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
import selectors
import secrets
import stat
import subprocess
import sys
import time

from attachments_files import PrivateFiles, _id_ok
from attachments_resources import ResourceLease, directory
import private_storage as ps
from attachments_types import AttachmentError, add_note, ident, integer, fail, TOKEN
from attachments_requests import RequestAuthority, CopyIntent, VerifiedCopy, PartialCopy, PhaseVerification, CopyCleanup, file_identity

MAX_FILE = 10 * 1024 * 1024
MAX_EXCLUDE = 1024 * 1024
ROOT = '.watch-attachments'
EXTENSIONS = {'text/plain': 'txt', 'text/markdown': 'md', 'application/pdf': 'pdf',
              'image/png': 'png', 'image/jpeg': 'jpg'}


@dataclass
class _ClosureState:
    unconfirmed: bool = False
    partial: PartialCopy | None = None


_CLOSURE_STATES = ContextVar('attachment_materializer_closure_states', default=())
_WORKSPACE_SCOPE = ContextVar('attachment_workspace_scope', default=None)


@contextmanager
def _closure_scope():
    # Per-call state survives exception translation and a later successful
    # recovery probe. Context-local tracking avoids cross-thread contamination.
    state = _ClosureState()
    token = _CLOSURE_STATES.set((*_CLOSURE_STATES.get(), state))
    try:
        yield state
    finally:
        _CLOSURE_STATES.reset(token)


def _report_close_failure(primary):
    if primary is not None:
        add_note(primary, 'Attachment descriptor closure could not be confirmed.')
    else:
        fail('handle_close_failed', 'Attachment handle closure could not be confirmed.', 503)


def _close_owned(closers):
    """Try every owned descriptor once; a close error never proves closure.

    Do not retry an uncertain close: the OS may have closed and reused its fd.
    Preserve a primary operation failure, while retaining uncertainty even if
    a caller translates or catches that failure during partial-copy recovery.
    """
    primary = sys.exc_info()[1]
    uncertain = False
    for close in closers:
        try:
            close()
        except BaseException:
            uncertain = True
            for state in _CLOSURE_STATES.get()[-1:]:
                state.unconfirmed = True
    if uncertain:
        _report_close_failure(primary)


def _close_all(fds):
    # A descriptor (POSIX) or a held Windows folder (closed through its own close).
    _close_owned((lambda fd=fd: os.close(fd)) if isinstance(fd, int) else fd.close for fd in fds)


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, st):
        return cls(st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink,
                   st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int

    @classmethod
    def of(cls, st):
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            fail('folder_changed', 'The selected workspace identity changed.', 409)
        return cls(st.st_dev, st.st_ino)


@dataclass(frozen=True)
class WorkspaceContext:
    """Only an independently registered/qualified resolver constructs this."""
    fingerprint: str
    workspace_fingerprint: str
    root: str
    root_identity: DirectoryIdentity
    git_dir: str
    git_identity: DirectoryIdentity
    exclude_parent: str
    exclude_parent_identity: DirectoryIdentity
    git_executable: str
    git_executable_identity: FileIdentity


@dataclass(frozen=True)
class WorkspaceDirectories:
    root: DirectoryIdentity | None
    request: DirectoryIdentity | None

    @classmethod
    def from_record(cls, value):
        if type(value) is not dict or set(value) != {'root_identity', 'request_identity'}:
            fail('copy_changed', 'The attachment directory record is unavailable.', 409)
        def entry(v):
            if v is None:
                return None
            if type(v) not in (tuple, list) or len(v) != 2 or any(not integer(n, 0, 2**63-1) for n in v):
                fail('copy_changed', 'The attachment directory record changed.', 409)
            return DirectoryIdentity(*v)
        return cls(entry(value['root_identity']), entry(value['request_identity']))


@dataclass(frozen=True)
class ExclusionReceipt:
    identity: FileIdentity
    rule: str


@dataclass(frozen=True)
class RetainedCopyCleanup:
    """Identity retained in a committed deletion receipt, without Send authority."""
    copy_id: str
    lease_id: str
    copy_generation: int
    identity: tuple
    sha256: str
    bytes: int
    kind: str

    def __post_init__(self):
        ident(self.copy_id)
        file_identity(self.identity)
        if (type(self.lease_id) is not str or not TOKEN.fullmatch(self.lease_id) or not self.lease_id.strip('0')
                or type(self.sha256) is not str or not TOKEN.fullmatch(self.sha256)
                or not integer(self.copy_generation, 1) or not integer(self.bytes, 0, MAX_FILE)
                or self.identity[2] != self.bytes or self.kind not in ('full', 'closed_partial')):
            fail('copy_changed', 'The retained attachment identity is unavailable.', 409)


@dataclass(frozen=True)
class _WorkspaceScope:
    materializer: object
    context: WorkspaceContext
    authority: RequestAuthority
    lease: ResourceLease
    keys: frozenset
    closure: _ClosureState


def copy_identity(value):
    return (value.device, value.inode, value.size, value.mtime_ns, value.ctime_ns)


def resource_keys(context, authority, copies=()):
    keys = {'workspace:' + context.workspace_fingerprint[:32], 'request:' + authority.request_id}
    for spec in copies:
        keys.update(('source:' + spec.source_id, 'copy:' + spec.id))
    return sorted(keys)


def relative_path(request_id, copy_id, media_type):
    ident(request_id)
    ident(copy_id)
    if media_type not in EXTENSIONS:
        fail('unsupported_type', 'This attachment type is not supported.', 415)
    return '%s/%s/%s.%s' % (ROOT, request_id, copy_id, EXTENSIONS[media_type])


def _regular(st, *, private=True):
    value = FileIdentity.of(st)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
            or private and stat.S_IMODE(st.st_mode) != 0o600):
        fail('copy_changed', 'An attachment file identity changed.', 409)
    return value


def _regular_fd(fd, *, private=True):
    """``_regular`` for an open file. POSIX: exactly ``_regular(os.fstat(fd), private=)``. Windows: on its handle a
    regular file with one name that is private (``private``) or that nobody but its owner can change; its identity
    is the handle's volume serial, file index, size and times (``file_identity``, within the store's range), with
    fstat's type and link count."""
    if not ps.WIN:
        return _regular(os.fstat(fd), private=private)
    try:
        if private:
            ps.check_private_fd(fd, single_link=True)
        else:
            ps.unwritable(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError('not one regular file')
        device, inode, size, mtime_ns, ctime_ns = _id_ok(ps.file_identity(fd))
    except (OSError, AttachmentError):
        fail('copy_changed', 'An attachment file identity changed.', 409)
    return FileIdentity(device, inode, st.st_mode, 0, st.st_nlink, size, mtime_ns, ctime_ns)


def _pread(fd, size, offset):
    """``os.pread`` (POSIX); Windows has none, so the descriptor's own position is moved and read."""
    if not ps.WIN:
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


# How a Windows refusal names the folder it is about (the roles of WorkspaceContext's three folders).
FOLDER_NAMES = {'workspace': 'The selected workspace folder', 'git': "The selected workspace's .git folder",
                'git_info': "The selected workspace's .git\\info folder"}


def _windows_refusal(error, role):
    """Plain words for a Windows refusal of a workspace or Git folder, naming which one; None for any other."""
    name = FOLDER_NAMES.get(role, FOLDER_NAMES['workspace'])
    reason = getattr(error, 'reason', None)
    if reason == 'access':
        fail('workspace_unsafe', name + ' can be changed by other people using this computer, so files are not '
             'copied into the workspace. A folder made directly under a drive (such as C:\\src) lets every '
             'signed-in user change it, as a folder others can write to does on a Mac. Choose a folder inside your '
             'user folder.', 409)
    if reason == 'owner':
        fail('workspace_not_yours', name + ' belongs to another account (often Administrators, when it was made '
             'from an administrator window), so files are not copied into the workspace. Take ownership of it, or '
             'choose a folder you made.', 409)
    if reason == 'link':
        fail('workspace_linked', name + ' is reached through a link or junction, which Motet does not follow. '
             "Choose the folder's real location.", 409)
    return None


def _windows_folder(folder, *, private=False, role='workspace'):
    """DirectoryIdentity for a held Windows folder: private (attachment folders) or changeable by nobody but its
    owner (the workspace and Git folders), with an identity within the store's range."""
    try:
        if not private:
            folder.unwritable()
    except ps.PrivateStorageError as error:
        _windows_refusal(error, role)
        fail('folder_changed', 'The selected workspace identity changed.', 409)
    try:
        return DirectoryIdentity(*_id_ok(folder.identity())[:2])
    except AttachmentError:
        fail('folder_changed', 'The selected workspace identity changed.', 409)


def _folder_identity(handle, *, private=False, role='workspace'):
    """DirectoryIdentity of an open folder: a descriptor on POSIX (exactly ``DirectoryIdentity.of(os.fstat(fd))``),
    a held folder on Windows (``role`` names it in a refusal)."""
    if isinstance(handle, int):
        return DirectoryIdentity.of(os.fstat(handle))
    return _windows_folder(handle, private=private, role=role)


def _sync_folder(handle):
    if isinstance(handle, int):
        os.fsync(handle)
    else:
        handle.sync()


def _child_folder(parent, name):
    """Open the attachment folder ``name`` in ``parent``. POSIX: ``O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC``
    by ``dir_fd``, as before. Windows: held through ``parent``, private, and writable (copies are made in it)."""
    if isinstance(parent, int):
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    return parent.folder(name, private=True, writable=True)


def _make_folder(parent, name):
    if isinstance(parent, int):
        os.mkdir(name, 0o700, dir_fd=parent)
    else:
        parent.mkdir(name)


def _open_file(parent, name, *, create=False):
    """A file in ``parent``: read-only, or with ``create`` a new private one open for reading and writing. POSIX:
    ``O_RDONLY | O_NOFOLLOW | O_CLOEXEC`` or ``O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600`` by
    ``dir_fd``, as before. Windows: through the held folder, never following a reparse point."""
    if isinstance(parent, int):
        if create:
            return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    return parent.create(name) if create else parent.open(name)


def _child_stat(parent, name):
    if isinstance(parent, int):
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    return parent.child_stat(name)


def _bounded_read(fd, maximum):
    data = bytearray()
    while True:
        chunk = _pread(fd, min(65536, maximum - len(data) + 1), len(data))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > maximum:
            fail('exclusion_unavailable', 'Local Git exclusion cannot be verified.', 503)


def _same_entry(parent, name, fd):
    if not isinstance(parent, int):
        _windows_same_entry(parent, name, fd)
        return
    entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(fd)
    if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
        fail('copy_changed', 'An attachment path identity changed.', 409)


def _windows_same_entry(parent, name, opened):
    """Windows ``_same_entry``: ``name`` in the held ``parent``, opened again without following a reparse point, is
    the same file or folder (volume serial and file index from the handles) as ``opened``."""
    try:
        if isinstance(opened, int):
            again = parent.open(name)
            try:
                same = ps.file_identity(again)[:2] == ps.file_identity(opened)[:2]
            finally:
                os.close(again)
        else:
            with parent.folder(name) as again:
                same = again.identity()[:2] == opened.identity()[:2]
    except OSError:
        same = False
    if not same:
        fail('copy_changed', 'An attachment path identity changed.', 409)


def _verify_cleanup_file(fd, saved):
    if copy_identity(_regular_fd(fd)) != saved.identity:
        fail('copy_changed', 'The attachment copy was changed and was left alone.', 409)
    digest = hashlib.sha256(_bounded_read(fd, MAX_FILE)).hexdigest()
    if digest != saved.sha256 or copy_identity(_regular_fd(fd)) != saved.identity:
        fail('copy_changed', 'The attachment copy was changed and was left alone.', 409)


def _windows_remove(parent, name, saved):
    """Windows: remove the copy ``name`` through the held folder, only if it is still the recorded copy, and return
    'removed' or 'absent' (no such name). The remove opens it sharing only reading and checks it on that handle, so
    nobody writes it between the check and the deletion (POSIX checks, then unlinks by name). Another program
    holding it open, with or without sharing (a scanner, an editor), is 'busy': it is left in place and the cleanup
    is retried later."""
    def expect(fd):
        try:
            _verify_cleanup_file(fd, saved)
            return True
        except AttachmentError:
            return False
    outcome = parent.remove(name, expect)
    if outcome == 'busy':
        fail('cleanup_busy', 'Another program has the attachment copy open, so Motet left it in place and will try '
             'again later.', 409)
    if outcome == 'changed':
        fail('copy_changed', 'The attachment copy was changed and was left alone.', 409)
    if outcome not in ('removed', 'absent'):
        fail('cleanup_incomplete', 'The attachment copy could not be safely removed.', 409)
    return outcome


class Materializer:
    def __init__(self, *, enabled=False, validate_authority=None, record_created=None,
                 record_directory=None, record_partial=None, checkpoint=None, workspace_files=None):
        self.enabled = enabled is True
        if self.enabled and workspace_files is not None and type(workspace_files) is not PrivateFiles:
            fail('storage_unavailable', 'Attachment workspace storage is unavailable.', 503)
        self._workspace_files = workspace_files if self.enabled else None
        self.validate_authority = validate_authority
        self.record_created = record_created
        self.record_directory = record_directory
        self.record_partial = record_partial
        self.checkpoint = checkpoint or (lambda _: None)

    @staticmethod
    def _verification(context, authority, copies, phase, lease_id):
        now = time.time()
        return PhaseVerification(authority.request_id, authority.fingerprint, authority.operation_revision,
                                 context.fingerprint, authority.profile_revision, phase,
                                 tuple(sorted((s.id, s.copy_generation) for s in copies)), lease_id, now, now + 2)

    def _authorize(self, context, authority, lease, phase, copies=()):
        if not self.enabled:
            fail('attachments_disabled', 'Attachments are unavailable.', 503)
        if self._workspace_files is not None:
            self._workspace_files._workspace_process()
            active = _WORKSPACE_SCOPE.get()
            if active is not None and active.materializer is self and active.closure.unconfirmed:
                _report_close_failure(None)
        if (type(context) is not WorkspaceContext or type(authority) is not RequestAuthority
                or type(lease) is not ResourceLease or not callable(self.validate_authority)
                or not TOKEN.fullmatch(context.fingerprint or '')
                or context.fingerprint != authority.context_fingerprint
                or not TOKEN.fullmatch(context.workspace_fingerprint or '')
                or context.workspace_fingerprint != authority.workspace_fingerprint
                or not TOKEN.fullmatch(authority.fingerprint or '')
                or not integer(authority.operation_revision, 1)
                or type(authority.profile_revision) is not str or not authority.profile_revision
                or not isinstance(authority.owner_epoch, str) or not TOKEN.fullmatch(authority.owner_epoch)):
            fail('send_authority_required', 'Verified attachment Send authority is required.', 409)
        ident(authority.request_id)
        for spec in copies:
            if (type(spec) is not CopyIntent or not integer(spec.bytes, 0, MAX_FILE)
                    or not integer(spec.copy_generation, 1) or not integer(spec.source_generation, 1) or not integer(spec.access_generation, 1)
                    or spec.request_id != authority.request_id or spec.role != 'workspace'
                    or not isinstance(spec.lease_id, str) or not TOKEN.fullmatch(spec.lease_id)
                    or not isinstance(spec.sha256, str) or not TOKEN.fullmatch(spec.sha256)
                    or spec.relative_path != relative_path(authority.request_id, spec.id, spec.media_type)):
                fail('invalid_copy_intent', 'The attachment copy intent is invalid.', 400)
            ident(spec.source_id)
        lease.require(resource_keys(context, authority, copies))
        if self.validate_authority(authority, phase, tuple(copies)) is not True:
            fail('send_authority_changed', 'Attachment Send authority changed.', 409)

    @contextmanager
    def _workspace_operation(self, context, authority, lease, copies=(), *, nested=False, track_local=False):
        files = self._workspace_files
        if files is None:
            if track_local:
                with _closure_scope() as closure:
                    yield closure
            else:
                yield None
            return
        expected = resource_keys(context, authority, copies) if copies else lease._keys
        keys = frozenset(k for k in expected if k.startswith(('source:', 'copy:')))
        active = _WORKSPACE_SCOPE.get()
        if active is not None and active.materializer is self:
            if (not nested or active.context != context or active.authority != authority or active.lease is not lease
                    or not keys.issubset(active.keys)):
                fail('workspace_handles_busy', 'Attachment workspace ownership changed.', 409)
            if active.closure.unconfirmed:
                _report_close_failure(None)
            yield active.closure
            return

        pin = files._reserve_workspace(authority.request_id, keys)
        with _closure_scope() as closure:
            token = _WORKSPACE_SCOPE.set(_WorkspaceScope(self, context, authority, lease, keys, closure))
            primary = None
            try:
                files._duplicate_workspace_lock(pin)
                yield closure
            except BaseException as error:
                primary = error
                raise
            finally:
                _WORKSPACE_SCOPE.reset(token)
                if files._finish_workspace(pin, closure.unconfirmed):
                    closure.unconfirmed = True
                    _report_close_failure(primary)

    def _workspace_directory(self, path):
        if ps.WIN:
            return self._windows_directory(path, False)
        if self._workspace_files is None:
            return directory(path)
        active = _WORKSPACE_SCOPE.get()
        if active is None or active.materializer is not self:
            fail('workspace_handles_busy', 'Attachment workspace ownership could not be verified.', 409)
        if active.closure.unconfirmed:
            _report_close_failure(None)
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
            fail('folder_changed', 'The selected workspace changed.', 409)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open('/', flags)
        try:
            for part in path.split('/')[1:]:
                child = os.open(part, flags, dir_fd=fd)
                previous, fd = fd, child
                _close_all((previous,))
                if active.closure.unconfirmed:
                    _report_close_failure(None)
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                _close_all((fd,))

    def _windows_directory(self, path, writable):
        """Windows: hold the folder at ``path`` through the no-link walk (any reparse point on the way refused),
        asking to change it only where copies or the exclude file are made (``writable``)."""
        if self._workspace_files is not None:
            active = _WORKSPACE_SCOPE.get()
            if active is None or active.materializer is not self:
                fail('workspace_handles_busy', 'Attachment workspace ownership could not be verified.', 409)
            if active.closure.unconfirmed:
                _report_close_failure(None)
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
            fail('folder_changed', 'The selected workspace changed.', 409)
        return ps.open_folder(path, writable=writable)

    @contextmanager
    def _context(self, context, *, workspace_only=False):
        fds = []
        # The workspace is where the attachment folder is made, and the exclude file's folder where it is appended
        # (Windows asks for that access on those holds only).
        bindings = [(context.root, context.root_identity, True, 'workspace')]
        if not workspace_only:
            bindings.extend(((context.git_dir, context.git_identity, False, 'git'),
                             (context.exclude_parent, context.exclude_parent_identity, True, 'git_info')))
        role = 'workspace'
        try:
            try:
                for path, expected, writable, role in bindings:
                    fd = self._windows_directory(path, writable) if ps.WIN else self._workspace_directory(path)
                    fds.append(fd)
                    if _folder_identity(fd, role=role) != expected:
                        fail('folder_changed', 'The selected workspace changed.', 409)
            except OSError as error:
                if ps.WIN:
                    _windows_refusal(error, role)        # a link or junction on the way, in plain words
                fail('folder_changed', 'The selected workspace changed.', 409)
            yield fds
            # Native readers follow paths, not our still-open renamed directory.
            try:
                for path, expected, _, role in bindings:
                    fd = self._workspace_directory(path)
                    try:
                        if _folder_identity(fd, role=role) != expected:
                            fail('folder_changed', 'The selected workspace changed.', 409)
                    finally:
                        _close_all((fd,))
            except OSError as error:
                if ps.WIN:
                    _windows_refusal(error, role)
                fail('folder_changed', 'The selected workspace changed.', 409)
        finally:
            _close_all(reversed(fds))

    def _git(self, context, args, deadline, *, stdout_limit):
        """Bounded fixed Git operations only; no shell, hooks, inherited Git env."""
        exe = context.git_executable
        if (not isinstance(exe, str) or not os.path.isabs(exe) or os.path.realpath(exe) != exe
                or FileIdentity.of(os.stat(exe, follow_symlinks=False)) != context.git_executable_identity
                or not stat.S_ISREG(context.git_executable_identity.mode)):
            fail('exclusion_unavailable', 'The qualified Git executable changed.', 503)
        argv = [exe, '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                '-c', 'core.excludesFile=/dev/null', '--git-dir=' + context.git_dir,
                '--work-tree=' + context.root] + args
        env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
        proc, selector = None, selectors.DefaultSelector()
        output = {0: bytearray(), 1: bytearray()}
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            proc = subprocess.Popen(argv, cwd=context.root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
            for i, stream in enumerate((proc.stdout, proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, i)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                for event, _ in selector.select(remaining):
                    chunk = os.read(event.fd, 4096)
                    if not chunk:
                        selector.unregister(event.fileobj)
                        continue
                    output[event.data].extend(chunk)
                    if len(output[event.data]) > (stdout_limit if event.data == 0 else 1024):
                        raise ValueError()
            code = proc.wait(timeout=max(.001, deadline - time.monotonic()))
            if output[1]:
                raise ValueError()
            return code, bytes(output[0])
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            fail('exclusion_unavailable', 'Effective Git exclusion could not be verified.', 503)
        finally:
            if self._workspace_files is None:
                selector.close()
                if proc is not None:
                    self._reap_git(proc)
                    proc.stdout.close()
                    proc.stderr.close()
            else:
                closers = [selector.close]
                if proc is not None:
                    closers.extend((lambda: self._reap_git(proc), proc.stdout.close, proc.stderr.close))
                _close_owned(closers)

    @staticmethod
    def _reap_git(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    def _git_inputs(self, context, authority):
        """Metadata fences for the qualified, include-free repository profile.

        The resolver must qualify configuration includes separately; this slice
        refuses them rather than scanning arbitrary additional local paths.
        """
        import re
        paths = [os.path.join(context.git_dir, 'index'), os.path.join(context.git_dir, 'config'),
                 os.path.join(context.git_dir, 'config.worktree'),
                 os.path.join(os.path.dirname(context.exclude_parent), 'config'),
                 os.path.join(context.exclude_parent, 'exclude'),
                 os.path.join(context.root, '.gitignore'),
                 os.path.join(context.root, ROOT, '.gitignore'),
                 os.path.join(context.root, ROOT, authority.request_id, '.gitignore')]
        result = []
        for path in sorted(set(paths)):
            parent, fd = None, None
            try:
                parent = self._workspace_directory(os.path.dirname(path))
                fd = _open_file(parent, os.path.basename(path))
                value = _regular_fd(fd, private=False)
                _same_entry(parent, os.path.basename(path), fd)
                if os.path.basename(path).startswith('config'):
                    content = _bounded_read(fd, MAX_EXCLUDE)
                    if re.search(rb'(?im)^\s*\[\s*include(?:if)?(?:\s|\])', content):
                        fail('exclusion_unavailable', 'This Git configuration is not qualified for attachments.', 503)
                result.append((path, value))
            except FileNotFoundError:
                result.append((path, None))
            except OSError:
                fail('exclusion_unavailable', 'Git input identities could not be verified.', 503)
            finally:
                _close_all(item for item in (fd, parent) if item is not None)
        return tuple(result)

    def prove_ignored(self, context, authority, copies, lease):
        if type(copies) not in (list, tuple) or not 1 <= len(copies) <= 5 or len({s.id for s in copies}) != len(copies):
            fail('invalid_copy_intent', 'Use a bounded attachment copy manifest.', 400)
        self._authorize(context, authority, lease, 'git_proof', copies)
        with self._workspace_operation(context, authority, lease, copies, nested=True):
            self._prove_ignored(context, authority, copies)

    def _prove_ignored(self, context, authority, copies):
        with self._context(context):
            before = self._git_inputs(context, authority)
            deadline = time.monotonic() + 2
            for spec in copies:
                code, out = self._git(context, ['check-ignore', '-q', '--', spec.relative_path], deadline, stdout_limit=0)
                if code != 0 or out:
                    fail('exclusion_ineffective', 'Git does not ignore the attachment destination.', 409)
                self.checkpoint('git_file_proved')
            code, out = self._git(context, ['--literal-pathspecs', 'ls-files', '--cached', '--stage', '-z', '--', ROOT],
                                  deadline, stdout_limit=16384)
            if code != 0 or out:
                fail('destination_tracked', 'Git already tracks part of the attachment destination.', 409)
            if self._git_inputs(context, authority) != before:
                fail('exclusion_changed', 'Git inputs changed during attachment verification.', 409)

    def ensure_exclusion(self, context, authority, copies, lease):
        if type(copies) not in (list, tuple) or not 1 <= len(copies) <= 5:
            fail('invalid_copy_intent', 'Use a bounded attachment copy manifest.', 400)
        self._authorize(context, authority, lease, 'exclusion', copies)
        with self._workspace_operation(context, authority, lease, copies):
            return self._ensure_exclusion(context, authority, copies, lease)

    def _ensure_exclusion(self, context, authority, copies, lease):
        rule = ('/%s/%s/' % (ROOT, authority.request_id)).encode('ascii')
        fd = None
        with self._context(context) as fds:
            self._git_inputs(context, authority)
            parent = fds[2]
            try:
                created = False
                if not isinstance(parent, int):
                    # Windows: appended through an append-only handle that nobody but its owner can change
                    # (open_append's rule), made private if missing. A byte lock on it would be mandatory and stop
                    # Git reading it, so writers are serialised by this workspace's private resource lock instead,
                    # held by the lease through this whole update.
                    lease.require(['workspace:' + context.workspace_fingerprint[:32]])
                    fd, created = parent.open_append('exclude', create=True)
                    _regular_fd(fd, private=False)
                else:
                    try:
                        fd = os.open('exclude', os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    except FileNotFoundError:
                        fd = os.open('exclude', os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     0o600, dir_fd=parent)
                        created = True
                    import fcntl
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        fail('exclusion_busy', 'Local Git exclusion is being updated.', 409)
                    current = _regular(os.fstat(fd), private=False)
                    if stat.S_IMODE(current.mode) & 0o022:
                        fail('exclusion_unavailable', 'Local Git exclusion permissions are unsafe.', 503)
                _same_entry(parent, 'exclude', fd)
                before = _bounded_read(fd, MAX_EXCLUDE)
                if rule not in before.splitlines():
                    payload = b'\n' + rule + b'\n'
                    self.checkpoint('exclude_before_append')
                    if os.write(fd, payload) != len(payload):
                        fail('exclusion_partial', 'Local Git exclusion was only partially written; review it before continuing.', 409)
                    os.fsync(fd)
                    self.checkpoint('exclude_after_append')
                    after = _bounded_read(fd, MAX_EXCLUDE)
                    if after[:len(before)] != before or rule not in after[len(before):].splitlines():
                        fail('exclusion_changed', 'Local Git exclusion changed while being updated.', 409)
                _same_entry(parent, 'exclude', fd)
                os.fsync(fd)
                result = ExclusionReceipt(_regular_fd(fd, private=False), rule.decode('ascii'))
                if created:
                    _sync_folder(parent)
            except AttachmentError:
                raise
            except OSError:
                fail('exclusion_unavailable', 'Local Git exclusion could not be established.', 503)
            finally:
                if fd is not None:
                    _close_all((fd,))
        self.prove_ignored(context, authority, copies, lease)
        return result

    @contextmanager
    def _directories(self, root_fd, authority, retained, lease, *, create):
        fds, values = [], []
        try:
            parent = root_fd
            for component, expected, role in ((ROOT, retained.root if retained else None, 'root'),
                                               (authority.request_id, retained.request if retained else None, 'request')):
                created = False
                if expected is None:
                    if not create:
                        fail('copy_changed', 'The attachment directory identity is unavailable.', 409)
                    try:
                        _make_folder(parent, component)
                        created = True
                    except FileExistsError:
                        fail('copy_changed', 'An unrecognized attachment directory already exists.', 409)
                fd = _child_folder(parent, component)
                fds.append(fd)
                if isinstance(fd, int):
                    st = os.fstat(fd)
                    value = DirectoryIdentity.of(st)
                    private = stat.S_IMODE(st.st_mode) == 0o700
                else:
                    value, private = _folder_identity(fd, private=True), True   # opened as private, or refused
                if not private or expected is not None and value != expected:
                    fail('copy_changed', 'The attachment directory identity changed.', 409)
                if created:
                    _sync_folder(parent)
                    if not callable(self.record_directory) or self.record_directory(authority, role, (value.device, value.inode), lease) is False:
                        fail('copy_record_failed', 'The attachment directory could not be recorded.', 503)
                _same_entry(parent, component, fd)
                values.append(value)
                parent = fd
            yield fds[-1], WorkspaceDirectories(*values)
            _same_entry(root_fd, ROOT, fds[0])
            _same_entry(fds[0], authority.request_id, fds[1])
        finally:
            _close_all(reversed(fds))

    def prepare_directories(self, context, authority, lease, *, directories=None):
        """After Send, before ledger copy claim. No source/workspace file bytes."""
        self._authorize(context, authority, lease, 'directories')
        if not callable(self.record_directory):
            fail('copy_record_failed', 'Durable attachment directory recording is unavailable.', 503)
        with self._workspace_operation(context, authority, lease):
            return self._prepare_directories(context, authority, lease, directories)

    def _prepare_directories(self, context, authority, lease, directories):
        try:
            with self._context(context) as roots:
                with self._directories(roots[0], authority, directories, lease, create=True) as (_, dirs):
                    self._authorize(context, authority, lease, 'directories')
                    return dirs
        except OSError:
            fail('copy_failed', 'Attachment directories could not be established.', 422)

    def _partial_evidence(self, context, authority, spec, fd, created, dirs, source_data,
                          written_total, attempted_end, lease):
        """Observe only our still-live created inode and its known source prefix.

        A failed write can have changed bytes without returning a count. Such
        bytes qualify only if their actual size lies within the attempted write
        and every byte is the frozen, digest-verified source prefix. This is not
        complete preparation or publication evidence.
        """
        lease.require(resource_keys(context, authority, (spec,)))
        before = _regular_fd(fd)
        if ((before.device, before.inode) != (created.device, created.inode)
                or not written_total <= before.size <= attempted_end <= spec.bytes):
            fail('partial_unverified', 'The partial attachment could not be verified.', 409)
        data = _bounded_read(fd, spec.bytes)
        if len(data) != before.size or data != source_data[:before.size]:
            fail('partial_unverified', 'The partial attachment was changed and was left alone.', 409)
        os.fsync(fd)
        if _regular_fd(fd) != before:
            fail('partial_unverified', 'The partial attachment changed while being checked.', 409)
        with self._context(context, workspace_only=True) as roots:
            with self._directories(roots[0], authority, dirs, lease, create=False) as (parent, _):
                _same_entry(parent, spec.relative_path.rsplit('/', 1)[1], fd)
                _sync_folder(parent)
        return (copy_identity(before), hashlib.sha256(data).hexdigest(), len(data))

    def materialize(self, context, authority, spec, source_fd, source_identity, lease, *, directories=None):
        self._authorize(context, authority, lease, 'materialize', (spec,))
        closure = None
        try:
            with self._workspace_operation(context, authority, lease, (spec,), track_local=True) as closure:
                return self._materialize(context, authority, spec, source_fd, source_identity, lease,
                                         directories=directories, closure=closure)
        finally:
            if closure is not None and closure.partial is not None and not closure.unconfirmed:
                if self.record_partial(closure.partial, lease) is False:
                    fail('partial_record_failed', 'The partial attachment receipt could not be recorded.', 503)

    def _materialize(self, context, authority, spec, source_fd, source_identity, lease, *, directories, closure):
        if not callable(self.record_created) or not callable(self.record_directory) or not callable(self.record_partial):
            fail('copy_record_failed', 'Durable attachment copy recording is unavailable.', 503)
        self.prove_ignored(context, authority, (spec,), lease)
        fd, created, dirs = None, None, None
        recorded, completed = False, False
        written_total, attempted_end, source_data = 0, 0, b''
        try:
            if not ps.read_only(source_fd):          # POSIX: fcntl(F_GETFL) & O_ACCMODE == O_RDONLY, as before
                fail('source_changed', 'Use a verified read-only attachment source.', 409)
            source_before = _regular_fd(source_fd)
            if copy_identity(source_before) != source_identity or source_before.size != spec.bytes:
                fail('source_changed', 'The attachment source changed.', 409)
            # Freeze a bounded original source, with full identity/digest checks,
            # so unknown partial writes can later be checked against its prefix.
            source_data = _bounded_read(source_fd, MAX_FILE)
            if (len(source_data) != spec.bytes or hashlib.sha256(source_data).hexdigest() != spec.sha256
                    or _regular_fd(source_fd) != source_before):
                fail('source_changed', 'The attachment source changed.', 409)
            with self._context(context) as roots:
                with self._directories(roots[0], authority, directories, lease, create=True) as (parent, dirs):
                    name = spec.relative_path.rsplit('/', 1)[1]
                    fd = _open_file(parent, name, create=True)
                    created = _regular_fd(fd)
                    if self.record_created(spec.id, spec.lease_id, copy_identity(created), lease) is False:
                        fail('copy_record_failed', 'The attachment copy could not be recorded.', 503)
                    recorded = True
                    self.checkpoint('copy_created')
                    hasher, offset = hashlib.sha256(), 0
                    deadline = time.monotonic() + 30
                    while offset < spec.bytes:
                        if time.monotonic() > deadline:
                            fail('copy_timeout', 'The attachment copy timed out.', 422)
                        self._authorize(context, authority, lease, 'copy_chunk', (spec,))
                        data = source_data[offset:offset + min(65536, spec.bytes - offset)]
                        if not data:
                            fail('source_changed', 'The attachment source changed.', 409)
                        hasher.update(data)
                        written = 0
                        while written < len(data):
                            attempted_end = offset + len(data)
                            count = os.write(fd, data[written:])
                            if count <= 0 or count > len(data) - written:
                                raise OSError()
                            written += count
                            written_total += count
                        offset += len(data)
                    if _pread(source_fd, 1, offset) or _regular_fd(source_fd) != source_before or hasher.hexdigest() != spec.sha256:
                        fail('source_changed', 'The attachment source changed.', 409)
                    os.fsync(fd)
                    final = _regular_fd(fd)
                    if (final.device, final.inode) != (created.device, created.inode):
                        fail('copy_changed', 'The attachment copy changed.', 409)
                    _same_entry(parent, name, fd)
                    _sync_folder(parent)
                    result = VerifiedCopy(spec.id, spec.lease_id, spec.copy_generation, copy_identity(final), spec.sha256, spec.bytes,
                                          self._verification(context, authority, (spec,), 'publication', spec.lease_id))
                    self.checkpoint('copy_before_publication')
                    verified = self.verify_copy(context, authority, spec, result, lease, directories=dirs)
            completed = True
            return verified
        except AttachmentError:
            raise
        except OSError:
            fail('copy_failed', 'The attachment copy could not be completed.', 422)
        finally:
            partial = None
            if fd is not None:
                try:
                    if not completed and recorded:
                        try:
                            partial = self._partial_evidence(context, authority, spec, fd, created, dirs, source_data,
                                                             written_total, attempted_end, lease)
                        except (AttachmentError, OSError):
                            pass  # Unverified partial remains charged; no invented cleanup authority.
                finally:
                    _close_all((fd,))
            if partial is not None and not closure.unconfirmed:
                # The public wrapper records only after the lifetime pin's
                # duplicate closes. The resource lease still covers that write.
                closure.partial = PartialCopy(spec.id, spec.lease_id, spec.copy_generation, *partial, True)

    def verify_copy(self, context, authority, spec, saved, lease, *, directories):
        self._authorize(context, authority, lease, 'publication', (spec,))
        if type(saved) is not VerifiedCopy or saved.copy_id != spec.id or saved.copy_generation != spec.copy_generation or saved.sha256 != spec.sha256 or saved.bytes != spec.bytes:
            fail('copy_changed', 'The attachment copy record changed.', 409)
        with self._workspace_operation(context, authority, lease, (spec,), nested=True):
            return self._verify_copy(context, authority, spec, saved, lease, directories)

    def _verify_copy(self, context, authority, spec, saved, lease, directories):
        self.prove_ignored(context, authority, (spec,), lease)
        try:
            with self._context(context) as roots:
                with self._directories(roots[0], authority, directories, lease, create=False) as (parent, _):
                    name = spec.relative_path.rsplit('/', 1)[1]
                    fd = _open_file(parent, name)
                    try:
                        if copy_identity(_regular_fd(fd)) != saved.identity:
                            fail('copy_changed', 'The attachment copy identity changed.', 409)
                        h, offset = hashlib.sha256(), 0
                        while offset <= spec.bytes:
                            chunk = _pread(fd, min(65536, spec.bytes - offset + 1), offset)
                            if not chunk:
                                break
                            offset += len(chunk)
                            h.update(chunk)
                        if offset != spec.bytes or h.hexdigest() != spec.sha256 or copy_identity(_regular_fd(fd)) != saved.identity:
                            fail('copy_changed', 'The attachment copy content changed.', 409)
                        _same_entry(parent, name, fd)
                    finally:
                        _close_all((fd,))
            self._authorize(context, authority, lease, 'publication', (spec,))
            return VerifiedCopy(spec.id, spec.lease_id, spec.copy_generation, saved.identity, spec.sha256, spec.bytes,
                                self._verification(context, authority, (spec,), 'publication', spec.lease_id))
        except OSError:
            fail('copy_changed', 'The attachment copy is unavailable.', 409)

    def verify_handoff(self, context, authority, copies, saved_copies, lease, *, directories):
        """Fresh all-file evidence; caller keeps this lease through native claim."""
        if (type(copies) not in (list, tuple) or not 1 <= len(copies) <= 5
                or type(saved_copies) not in (list, tuple) or len(copies) != len(saved_copies)):
            fail('invalid_copy_intent', 'Use the exact bounded attachment manifest.', 400)
        self._authorize(context, authority, lease, 'handoff', copies)
        with self._workspace_operation(context, authority, lease, copies):
            for spec, saved in zip(copies, saved_copies):
                self.verify_copy(context, authority, spec, saved, lease, directories=directories)
            self._authorize(context, authority, lease, 'handoff', copies)
            return self._verification(context, authority, copies, 'handoff', secrets.token_hex(32))

    def remove_workspace_copy(self, files, context, authority, spec, saved, lease, *, directories):
        """Return physical proof only from this exact store's tracked lifetime."""
        if type(self) is not Materializer or type(files) is not PrivateFiles or self._workspace_files is not files:
            fail('storage_unavailable', 'Attachment workspace lifetime is unavailable.', 503)
        if type(saved) is RetainedCopyCleanup:
            self._authorize(context, authority, lease, 'cleanup', (spec,))
            if (saved.copy_id != spec.id or saved.copy_generation != spec.copy_generation
                    or saved.lease_id != spec.lease_id or saved.bytes > spec.bytes
                    or saved.kind == 'full' and (saved.bytes, saved.sha256) != (spec.bytes, spec.sha256)):
                fail('copy_changed', 'The retained attachment identity changed.', 409)
            with self._workspace_operation(context, authority, lease, (spec,)):
                return self._remove_copy(context, authority, spec, saved, lease, directories)
        return self.remove_copy(context, authority, spec, saved, lease, directories=directories)

    def remove_copy(self, context, authority, spec, saved, lease, *, directories):
        """Manual cleanup does not require native terminal status or Git ignore.

        The trusted cleanup validator must check committed revocation/selection,
        not a live recipient. This method never changes message outcome/quota.
        """
        self._authorize(context, authority, lease, 'cleanup', (spec,))
        if (type(saved) not in (VerifiedCopy, PartialCopy) or saved.copy_id != spec.id or saved.copy_generation != spec.copy_generation
                or saved.lease_id != spec.lease_id or not integer(saved.bytes, 0, spec.bytes)
                or saved.identity[2] != saved.bytes
                or type(saved) is PartialCopy and saved.handles_closed is not True):
            fail('copy_changed', 'The attachment cleanup selection changed.', 409)
        with self._workspace_operation(context, authority, lease, (spec,)):
            return self._remove_copy(context, authority, spec, saved, lease, directories)

    def _remove_copy(self, context, authority, spec, saved, lease, directories):
        try:
            with self._context(context, workspace_only=True) as roots:
                with self._directories(roots[0], authority, directories, lease, create=False) as (parent, _):
                    name = spec.relative_path.rsplit('/', 1)[1]
                    if not isinstance(parent, int):
                        # Windows: no separate open first. An editor or viewer holding the copy without sharing
                        # would refuse it, and the remove checks the copy on the very handle it deletes through.
                        self.checkpoint('cleanup_before_unlink')
                        self._authorize(context, authority, lease, 'cleanup', (spec,))
                        outcome = _windows_remove(parent, name, saved)
                        _sync_folder(parent)
                        return CopyCleanup(spec.id, spec.copy_generation, spec.lease_id, outcome, True)
                    try:
                        fd = _open_file(parent, name)
                    except FileNotFoundError:
                        self._authorize(context, authority, lease, 'cleanup', (spec,))
                        _sync_folder(parent)
                        try:
                            _child_stat(parent, name)
                        except FileNotFoundError:
                            pass
                        else:
                            fail('copy_changed', 'The attachment copy appeared during verification.', 409)
                        return CopyCleanup(spec.id, spec.copy_generation, spec.lease_id, 'absent', True)
                    try:
                        _verify_cleanup_file(fd, saved)
                        self.checkpoint('cleanup_before_unlink')
                        self._authorize(context, authority, lease, 'cleanup', (spec,))
                        _verify_cleanup_file(fd, saved)
                        _same_entry(parent, name, fd)
                        os.unlink(name, dir_fd=parent)
                        _sync_folder(parent)
                    finally:
                        _close_all((fd,))
                    return CopyCleanup(spec.id, spec.copy_generation, spec.lease_id, 'removed', True)
        except AttachmentError:
            raise
        except OSError:
            fail('cleanup_incomplete', 'The attachment copy could not be safely removed.', 409)
