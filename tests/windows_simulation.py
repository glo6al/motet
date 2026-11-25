"""Run the bridge's Windows branches on macOS or Linux, for tests only.

``private_storage.WIN`` is set, so callers take their Windows branch, but each private_storage call they make is
answered by the POSIX implementation (links, owner and mode still refused), including the no-link folder walk
(``open_folder``, ``open_no_links``) and every change made through a held folder (``create``, ``open_or_create``,
``mkdir``, ``remove``, ``child_stat``, ``sync``). fcntl and os.getuid are made to fail loudly, so a Windows branch
that still reached for them would show up as an error. A function handed to a held folder's method (``remove``'s
``expect``) is the caller's own code, so it runs as Windows again, and its POSIX-only calls fail too.

Three Windows answers are modelled rather than taken from POSIX. A held folder reports ``file_system`` as the volume
named to ``simulated_windows`` (NTFS by default). ``file_identity`` (and a held folder's ``identity``) answers as
Windows reads it from a handle: a fixed 32-bit volume serial (``SERIAL``) that is not ``fstat``'s ``st_dev``, with
the file index, size and times; so code that took identities from ``os.fstat`` instead would not match.
``path_identity`` (a user's own folder, opened by path) answers the same way: ``SERIAL`` and the file index.

The switches between Windows and POSIX are process-wide, so only the thread that entered the simulation may use
private_storage under it: a call from any other thread fails at once with AssertionError, before anything is
switched, and simulations are serialised by one lock. This checks the wiring only; the Windows walk's own decisions
are tested with the Win32 calls replaced (test_private_storage.py), and the access-list rules on real Windows.
"""
from contextlib import ExitStack, contextmanager
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import private_storage as ps

CALLS = ('check_private', 'make_private_folder', 'create_private_file', 'open_private_file', 'open_private_rw',
         'open_lock_file', 'try_lock', 'unlock', 'close_lock', 'sqlite_companions_plain', 'sqlite_companions_private',
         'folder_walk_available', 'open_folder', 'open_no_links', 'anchored', 'read_only', 'unwritable', 'sync_file',
         'check_private_fd')
SERIAL = 0x2A5E5E1D            # the simulated volume serial: 32 bits, and not what fstat reports as st_dev
_SIMULATION = threading.RLock()


def _forbidden(name):
    def refuse(*args, **kwargs):
        raise AssertionError('Windows branch used ' + name)
    return refuse


@contextmanager
def _module(name, value):
    """Set one entry of sys.modules and put back only that entry: never clear and refill the whole table, which
    would drop modules another thread imported meanwhile."""
    missing = object()
    before = sys.modules.get(name, missing)
    sys.modules[name] = value
    try:
        yield
    finally:
        if before is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = before


class _PosixFolder:
    """A held folder from the POSIX walk whose methods also run as POSIX (they check owner and mode). A function the
    caller hands one of them (``remove``'s ``expect``) is the caller's own code, so it runs as Windows again: a
    POSIX-only call inside it fails as it would there."""

    def __init__(self, folder, simulation):
        self._folder, self._simulation = folder, simulation

    def __getattr__(self, name):
        simulation = self._simulation
        if name == 'file_system':                       # POSIX has no answer; Windows storage asks for NTFS
            return lambda: simulation.file_system
        if name == 'identity':
            return lambda: simulation.identity(self._folder._fd)
        value = getattr(self._folder, name)
        if callable(value):
            def call(*args, **kwargs):
                args = [simulation.as_windows(a) if callable(a) else a for a in args]
                kwargs = {k: simulation.as_windows(v) if callable(v) else v for k, v in kwargs.items()}
                return simulation.as_posix(value, *args, **kwargs)
            return call
        return value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._folder.close()
        return False


class _Simulation:
    def __init__(self, file_system, calls):
        import fcntl                       # only ever simulated on POSIX
        self.fcntl, self.file_system, self.calls = fcntl, file_system, calls
        self.owner = threading.get_ident()
        self.real_getuid, self.real_fstat = os.getuid, os.fstat
        self.real = {name: getattr(ps, name) for name in CALLS}

    def same_thread(self):
        if threading.get_ident() != self.owner:
            raise AssertionError('private_storage used from another thread under the Windows simulation')

    def as_posix(self, function, *args, **kwargs):
        self.same_thread()
        with patch.object(ps, 'WIN', False), patch.object(os, 'getuid', self.real_getuid), \
                _module('fcntl', self.fcntl):
            result = function(*args, **kwargs)
        return _PosixFolder(result, self) if isinstance(result, ps.NoLinkFolder) else result

    def as_windows(self, function):
        def call(*args, **kwargs):
            self.same_thread()
            with patch.object(ps, 'WIN', True), patch.object(os, 'getuid', _forbidden('os.getuid')), \
                    _module('fcntl', None):
                return function(*args, **kwargs)
        return call

    def posix(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return self.as_posix(self.real[name], *args, **kwargs)
        return call

    def identity(self, fd):
        """What Windows reads from a handle: the volume serial (32 bits), file index, size, write and creation
        times. The real fstat is used, so a test can give ``os.fstat`` other values without changing these."""
        self.same_thread()
        self.calls.append('file_identity')
        info = self.real_fstat(fd)
        return SERIAL, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def path_identity(self, path):
        """What Windows reads from a handle opened by path: the volume serial and the file index."""
        self.same_thread()
        self.calls.append('path_identity')
        return SERIAL, os.stat(path).st_ino


@contextmanager
def simulated_windows(folder_walk=True, file_system='NTFS'):
    """Yield the list of private_storage calls made while simulating Windows; ``folder_walk=False`` simulates a
    Windows whose walk calls did not load, and ``file_system`` names the volume a held folder reports."""
    with _SIMULATION:
        calls = []
        simulation = _Simulation(file_system, calls)
        with ExitStack() as stack:
            stack.enter_context(patch.object(ps, 'WIN', True))
            for name in CALLS:
                stack.enter_context(patch.object(ps, name, simulation.posix(name)))
            stack.enter_context(patch.object(ps, 'file_identity', simulation.identity))
            stack.enter_context(patch.object(ps, 'path_identity', simulation.path_identity))
            if not folder_walk:
                stack.enter_context(patch.object(ps, 'folder_walk_available', return_value=False))
            stack.enter_context(_module('fcntl', None))                       # import fcntl now fails
            stack.enter_context(patch.object(os, 'getuid', _forbidden('os.getuid'), create=True))
            yield calls
