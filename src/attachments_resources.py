"""Short, process-visible attachment handle leases. No database or transport IO.

Callers acquire outside every database/room lock. Leases protect WATCH's local
handles, not a queued agent turn. Durable outcome pins are a separate policy.

POSIX holds the lock folder by descriptor and locks with flock, exactly as before. Windows holds it through the
no-link walk (one handle, which NTFS keeps from being renamed or removed, with every folder above it), checks each
lock file's owner and access list on its handle, and locks with the byte-range lock in private_storage.
"""
from contextlib import contextmanager
import hashlib
import math
import os
import re
import stat
import sys
import threading
import time

import private_storage as ps
from attachments_types import AttachmentError, add_note, fail

KEY = re.compile(r'(request|source|copy|workspace):[a-f0-9]{32}\Z')


def directory(path):
    """Open a canonical directory without following any symlink component."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        fail('resource_unavailable', 'The attachment resource directory is unavailable.', 503)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.split('/')[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private(st, is_dir=False):
    kind = stat.S_ISDIR if is_dir else stat.S_ISREG
    if (not kind(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != (0o700 if is_dir else 0o600)
            or (not is_dir and st.st_nlink != 1)):
        fail('resource_unavailable', 'Attachment resource ownership is unavailable.', 503)
    return (st.st_dev, st.st_ino)


def _refused():
    fail('resource_unavailable', 'Attachment resource ownership is unavailable.', 503)


def _windows_identity(values):
    """(volume serial, file index) on Windows, read from the handle (``private_storage.file_identity``), where the
    owner and access list were also checked; an id outside the stored range is refused, as in attachments_files."""
    if not (0 <= values[0] <= 2 ** 63 - 1 and 0 < values[1] <= 2 ** 63 - 1):
        _refused()
    return (values[0], values[1])


class ResourceLease:
    def __init__(self, manager, keys, fds):
        self._manager, self._keys, self._fds = manager, frozenset(keys), fds
        self._active = True

    def require(self, keys):
        if not self._active or not set(keys).issubset(self._keys):
            fail('resource_lease_required', 'An active attachment resource lease is required.', 409)
        self._manager._check()
        for name, fd, expected in self._fds:
            if self._manager._fd_identity(fd) != expected or self._manager._name_identity(name) != expected:
                fail('resource_changed', 'An attachment resource lock changed.', 409)


class ResourceLocks:
    """Private existing lock directory; disabled construction creates nothing.

    Lock files are retained, never deleted/replaced while other processes may
    hold them. The caller supplies a bounded set of recorded opaque identities.
    """
    def __init__(self, private_root, *, enabled=False):
        self._fd, self._identity, self._path = None, None, private_root
        self._folder, self._windows = None, ps.WIN
        self._guard, self._leases, self._closed = threading.Lock(), 0, False
        self.enabled = enabled is True
        if not self.enabled:
            return
        if self._windows:
            try:
                self._folder = ps.open_folder(private_root, private=True, writable=True, alone=True)
                self._identity = _windows_identity(self._folder.identity())
            except BaseException as error:
                if self._folder is not None:
                    self._folder.close()
                    self._folder = None
                if isinstance(error, AttachmentError) or not isinstance(error, OSError):
                    raise
                fail('resource_unavailable', 'Attachment resource locking is unavailable.', 503)
            return
        try:
            import fcntl
            self._flock = fcntl.flock
            self._exclusive, self._nonblock, self._unlock = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN
            self._fd = directory(private_root)
            self._identity = _private(os.fstat(self._fd), True)
        except BaseException as error:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except BaseException:
                    add_note(error, 'Attachment resource constructor cleanup could not be fully confirmed.')
                finally:
                    self._fd = None
            if isinstance(error, AttachmentError):
                raise
            if not isinstance(error, (OSError, ImportError)):
                raise
            fail('resource_unavailable', 'Attachment resource locking is unavailable.', 503)

    def _fd_identity(self, fd):
        """The identity of an open lock file, checked as private (on Windows on its handle)."""
        if not self._windows:
            return _private(os.fstat(fd))
        try:
            ps.check_private_fd(fd, single_link=True)
        except ps.PrivateStorageError:
            _refused()
        return _windows_identity(ps.file_identity(fd))

    def _name_identity(self, name):
        """The identity of the lock file that ``name`` names now, not followed."""
        if not self._windows:
            return _private(os.stat(name, dir_fd=self._fd, follow_symlinks=False))
        try:
            fd = self._folder.open(name, single_link=True, private=True)
        except ps.PrivateStorageError:
            _refused()
        try:
            return _windows_identity(ps.file_identity(fd))
        finally:
            os.close(fd)

    def _open_lock(self, name):
        if not self._windows:
            return os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._fd)
        try:
            return self._folder.open_or_create(name)
        except ps.PrivateStorageError:
            _refused()

    def _try_lock(self, fd):
        if self._windows:
            return ps.try_lock(fd)
        try:
            self._flock(fd, self._exclusive | self._nonblock)
            return True
        except BlockingIOError:
            return False

    def _release(self, fd):
        if self._windows:
            ps.unlock(fd)
        else:
            self._flock(fd, self._unlock)

    def _check(self):
        held = self._folder if self._windows else self._fd
        if not self.enabled or self._closed or held is None:
            fail('attachments_disabled', 'Attachments are unavailable.', 503)
        if self._windows:
            try:
                with ps.open_folder(self._path, private=True) as current:
                    if (_windows_identity(current.identity()) != self._identity
                            or _windows_identity(self._folder.identity()) != self._identity):
                        fail('resource_changed', 'Attachment resource locking changed.', 409)
            except OSError:
                fail('resource_changed', 'Attachment resource locking changed.', 409)
            return
        try:
            current = directory(self._path)
            try:
                if _private(os.fstat(current), True) != self._identity or _private(os.fstat(self._fd), True) != self._identity:
                    fail('resource_changed', 'Attachment resource locking changed.', 409)
            finally:
                os.close(current)
        except OSError:
            fail('resource_changed', 'Attachment resource locking changed.', 409)

    @contextmanager
    def hold(self, keys, *, timeout=2):
        if (type(keys) not in (tuple, list) or not 1 <= len(keys) <= 16
                or any(type(k) is not str or not KEY.fullmatch(k) for k in keys)
                or len(set(keys)) != len(keys) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 <= timeout <= 30):
            fail('invalid_request', 'Invalid attachment resource lease.', 400)
        fds, lease = [], None
        with self._guard:
            self._check()
            self._leases += 1
        try:
            deadline = time.monotonic() + timeout
            for key in sorted(keys):
                name = hashlib.sha256(key.encode('ascii')).hexdigest() + '.lock'
                self._check()
                fd = self._open_lock(name)
                try:
                    expected = self._fd_identity(fd)
                    if self._name_identity(name) != expected:
                        fail('resource_changed', 'An attachment resource lock changed.', 409)
                    while not self._try_lock(fd):
                        if time.monotonic() >= deadline:
                            fail('resource_busy', 'An attachment operation still holds its files.', 409)
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
                    fds.append((name, fd, expected))
                except BaseException:
                    os.close(fd)
                    raise
            lease = ResourceLease(self, keys, fds)
            lease.require(keys)
            yield lease
        except OSError:
            fail('resource_unavailable', 'Attachment resource locking is unavailable.', 503)
        finally:
            primary_error = sys.exc_info()[1]
            release_errors = []
            if lease is not None:
                lease._active = False
            for _, fd, _ in reversed(fds):
                try:
                    self._release(fd)
                except BaseException as error:
                    release_errors.append(error)
                finally:
                    try:
                        os.close(fd)
                    except BaseException as error:
                        release_errors.append(error)
            with self._guard:
                self._leases -= 1
            if release_errors:
                if primary_error is not None:
                    add_note(primary_error, 'Attachment resource release encountered an error after attempting every descriptor close.')
                else:
                    fail('resource_release_failed', 'Attachment resource release could not be fully confirmed.', 503)

    def close(self):
        with self._guard:
            if self._leases:
                fail('resource_busy', 'Attachment resource leases are still active.', 409)
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self._folder is not None:
                self._folder.close()
                self._folder = None
            self._closed = True
