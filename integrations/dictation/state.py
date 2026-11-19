"""Private helper capability, separate from Motet's owner credential."""
import contextlib
import hmac
import os
from pathlib import Path
import secrets
import stat


class StateError(Exception):
    pass


class PrivateState:
    def __init__(self, home):
        # Canonicalize the owner-selected home (including platform /tmp aliases),
        # then refuse links in every helper-owned descendant.
        self.home = Path(home).resolve()
        self.root = self.home / '.live-room' / 'dictation'

    @contextlib.contextmanager
    def directory(self, create=False):
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

    def _read(self, fd):
        file_fd = os.open('header', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise StateError('Helper credential must be a private regular file.')
            raw = os.read(file_fd, 256)
            prefix = b'X-Dictation-Key: '
            if len(raw) != len(prefix) + 64 + 1 or not raw.startswith(prefix) or not raw.endswith(b'\n'):
                raise StateError('Helper credential is invalid.')
            key = raw[len(prefix):-1].decode('ascii')
            if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                raise StateError('Helper credential is invalid.')
            return key
        finally:
            os.close(file_fd)

    def key(self, create=False):
        with self.directory(create=create) as fd:
            try:
                return self._read(fd)
            except FileNotFoundError:
                if not create:
                    raise StateError('Helper credential is not installed.') from None
            key = secrets.token_hex(32)
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
