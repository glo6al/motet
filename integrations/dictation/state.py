"""Private helper capability, separate from Motet's owner credential.

POSIX: descriptors opened with O_NOFOLLOW below the resolved home, each folder owned by this user with no group or
other bits, and the credential a private regular file with one name, as before. Windows: the same folders and file
through private_storage's no-link walk (each name opened by handle inside the folder above, refusing any link or
junction), each checked private on its handle (owned by this user; an access list naming only this user and SYSTEM,
and Administrators on folders), created private from the start.
"""
import contextlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import sys

PREFIX = b'X-Dictation-Key: '


class StateError(Exception):
    pass


def storage():
    """private_storage from Motet's src folder, imported on first use (the supervisor stays importable alone)."""
    src = str(Path(__file__).resolve().parents[2] / 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    import private_storage
    return private_storage


def _binary(fd):
    """Windows: a descriptor in binary mode, so a written newline stays one byte (the C runtime's default text mode
    would write CRLF). Elsewhere, and under the tests' Windows simulation, descriptors are already binary."""
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(fd, os.O_BINARY)
    return fd


def credential(raw):
    """The 64-hex key from the credential file's bytes, or StateError."""
    if len(raw) != len(PREFIX) + 64 + 1 or not raw.startswith(PREFIX) or not raw.endswith(b'\n'):
        raise StateError('Helper credential is invalid.')
    key = raw[len(PREFIX):-1].decode('ascii')
    if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise StateError('Helper credential is invalid.')
    return key


class PrivateState:
    def __init__(self, home):
        # Canonicalize the owner-selected home (including platform /tmp aliases),
        # then refuse links in every helper-owned descendant.
        self.home = Path(home).resolve()
        self.root = self.home / '.live-room' / 'dictation'

    @contextlib.contextmanager
    def directory(self, create=False):
        """The held private state folder: a POSIX descriptor, or on Windows a private_storage NoLinkFolder."""
        ps = storage()
        if ps.WIN:
            with self._windows_directory(ps, create) as held:
                yield held
            return
        if os.name != 'posix':
            raise StateError('Private helper state currently requires a POSIX host.')
        fd = None
        try:
            fd = os.open(str(self.home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in ('.live-room', 'dictation'):
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    os.close(child)
                    raise StateError('Helper state must be owned by this user and private.')
                os.close(fd)
                fd = child
            yield fd
        except OSError:
            raise StateError('Private helper state is missing or unsafe.') from None
        finally:
            if fd is not None:
                os.close(fd)

    @contextlib.contextmanager
    def _windows_directory(self, ps, create):
        if not ps.folder_walk_available():
            raise StateError('Private helper state is unavailable on this Windows.')
        try:
            with contextlib.ExitStack() as stack:
                held = stack.enter_context(ps.open_folder(str(self.home), writable=create))
                for part in ('.live-room', 'dictation'):
                    if create:
                        try:
                            held.mkdir(part)          # private from the start
                        except FileExistsError:
                            pass
                    held = stack.enter_context(held.folder(part, private=True, writable=create))
                yield held
        except ps.PrivateStorageError as error:
            if error.reason in ('owner', 'access'):
                raise StateError('Helper state must be owned by this user and private.') from None
            raise StateError('Private helper state is missing or unsafe.') from None
        except OSError:
            raise StateError('Private helper state is missing or unsafe.') from None

    @staticmethod
    def _windows_read(held):
        fd = held.open('header', single_link=True, private=True)
        try:
            return credential(os.read(fd, 256))
        finally:
            os.close(fd)

    def _read(self, fd):
        if storage().WIN:
            return self._windows_read(fd)
        file_fd = os.open('header', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise StateError('Helper credential must be a private regular file.')
            return credential(os.read(file_fd, 256))
        finally:
            os.close(file_fd)

    def _windows_create(self, held, key):
        try:
            fd = _binary(held.create('header'))
        except FileExistsError:
            return self._windows_read(held)
        try:
            view = memoryview(PREFIX + key.encode('ascii') + b'\n')
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except Exception:
            os.close(fd)
            try:
                held.remove('header', lambda _: True)
            except OSError:
                pass
            raise StateError('Could not create the helper credential.') from None
        os.close(fd)
        return key

    def key(self, create=False):
        with self.directory(create=create) as fd:
            try:
                return self._read(fd)
            except FileNotFoundError:
                if not create:
                    raise StateError('Helper credential is not installed.') from None
            key = secrets.token_hex(32)
            if storage().WIN:
                return self._windows_create(fd, key)
            try:
                file_fd = os.open('header', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=fd)
            except FileExistsError:
                return self._read(fd)
            try:
                raw = ('X-Dictation-Key: ' + key + '\n').encode('ascii')
                with os.fdopen(file_fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                os.unlink('header', dir_fd=fd)
                raise StateError('Could not create the helper credential.') from None
            return key


def authenticates(received, expected):
    return (type(received) is str and type(expected) is str
            and len(expected) == 64 and hmac.compare_digest(received.encode(), expected.encode()))
