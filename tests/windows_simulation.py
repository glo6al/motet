"""Run the bridge's Windows branches on macOS or Linux, for tests only.

``private_storage.WIN`` is set, so callers take their Windows branch, but each private_storage call they make is
answered by the POSIX implementation (links, owner and mode still refused), including the no-link folder walk
(``open_folder``, ``open_no_links``) and every change made through a held folder (``create``, ``open_or_create``,
``mkdir``, ``remove``, ``child_stat``, ``sync``). fcntl and os.getuid are made to fail loudly, so a Windows branch
that still reached for them would show up as an error. A function handed to a held folder's method (``remove``'s
``expect``) is the caller's own code, so it runs as Windows again, and its POSIX-only calls fail too. A held folder
reports ``file_system`` as the volume named to ``simulated_windows`` (NTFS by default). The switches are
process-wide, so a test whose threads use private_storage at the same time does not belong under it. This checks the wiring only; the Windows walk's own decisions are
tested with the Win32 calls replaced (test_private_storage.py), and the access-list rules on real Windows.
"""
from contextlib import ExitStack, contextmanager
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import private_storage as ps

CALLS = ('check_private', 'make_private_folder', 'create_private_file', 'open_private_file', 'open_private_rw',
         'open_lock_file', 'try_lock', 'unlock', 'close_lock', 'sqlite_companions_plain', 'folder_walk_available',
         'open_folder', 'open_no_links', 'anchored', 'read_only', 'unwritable', 'sync_file', 'check_private_fd')


def _forbidden(name):
    def refuse(*args, **kwargs):
        raise AssertionError('Windows branch used ' + name)
    return refuse


class _PosixFolder:
    """A held folder from the POSIX walk whose methods also run as POSIX (they check owner and mode). A function the
    caller hands one of them (``remove``'s ``expect``) is the caller's own code, so it runs as Windows again: a
    POSIX-only call inside it fails as it would there."""

    def __init__(self, folder, as_posix, as_windows, file_system):
        self._folder, self._as_posix, self._as_windows, self._file_system = folder, as_posix, as_windows, file_system

    def __getattr__(self, name):
        if name == 'file_system':                       # POSIX has no answer; Windows storage asks for NTFS
            return lambda: self._file_system
        value = getattr(self._folder, name)
        if callable(value):
            def call(*args, **kwargs):
                args = [self._as_windows(a) if callable(a) else a for a in args]
                kwargs = {k: self._as_windows(v) if callable(v) else v for k, v in kwargs.items()}
                return self._as_posix(value, *args, **kwargs)
            return call
        return value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._folder.close()
        return False


@contextmanager
def simulated_windows(folder_walk=True, file_system='NTFS'):
    """Yield the list of private_storage calls made while simulating Windows; ``folder_walk=False`` simulates a
    Windows whose walk calls did not load, and ``file_system`` names the volume a held folder reports."""
    import fcntl                       # only ever simulated on POSIX
    calls = []
    real = {name: getattr(ps, name) for name in CALLS}
    real_getuid = os.getuid

    def as_posix(function, *args, **kwargs):
        with patch.object(ps, 'WIN', False), patch.object(os, 'getuid', real_getuid), \
                patch.dict(sys.modules, {'fcntl': fcntl}):
            result = function(*args, **kwargs)
        return (_PosixFolder(result, as_posix, as_windows, file_system) if isinstance(result, ps.NoLinkFolder)
                else result)

    def as_windows(function):
        def call(*args, **kwargs):
            with patch.object(ps, 'WIN', True), patch.object(os, 'getuid', _forbidden('os.getuid')), \
                    patch.dict(sys.modules, {'fcntl': None}):
                return function(*args, **kwargs)
        return call

    def posix(name):
        def call(*args, **kwargs):
            calls.append(name)
            return as_posix(real[name], *args, **kwargs)
        return call

    with ExitStack() as stack:
        stack.enter_context(patch.object(ps, 'WIN', True))
        for name in CALLS:
            stack.enter_context(patch.object(ps, name, posix(name)))
        if not folder_walk:
            stack.enter_context(patch.object(ps, 'folder_walk_available', return_value=False))
        stack.enter_context(patch.dict(sys.modules, {'fcntl': None}))     # import fcntl now fails
        stack.enter_context(patch.object(os, 'getuid', _forbidden('os.getuid'), create=True))
        yield calls
