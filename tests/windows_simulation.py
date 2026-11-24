"""Run the bridge's Windows branches on macOS or Linux, for tests only.

``private_storage.WIN`` is set, so callers take their Windows branch, but each private_storage call they make is
answered by the POSIX implementation (links, owner and mode still refused). The handle-based folder walk, fcntl and
os.getuid are made to fail loudly, so a Windows branch that still reached for them would show up as an error. This
checks the wiring only; the access-list rules themselves are tested on real Windows (skipUnless nt).
"""
from contextlib import ExitStack, contextmanager
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import claude_channel
import private_storage as ps

CALLS = ('check_private', 'make_private_folder', 'create_private_file', 'open_private_file', 'open_private_rw',
         'open_lock_file', 'try_lock', 'unlock', 'close_lock', 'sqlite_companions_plain')


def _forbidden(name):
    def refuse(*args, **kwargs):
        raise AssertionError('Windows branch used ' + name)
    return refuse


@contextmanager
def simulated_windows(folder_walk=False):
    """Yield the list of private_storage calls made while simulating Windows."""
    import fcntl                       # only ever simulated on POSIX
    calls = []
    real = {name: getattr(ps, name) for name in CALLS}
    real_getuid = os.getuid

    def posix(name):
        def call(*args, **kwargs):
            calls.append(name)
            with patch.object(ps, 'WIN', False), patch.object(os, 'getuid', real_getuid), \
                    patch.dict(sys.modules, {'fcntl': fcntl}):
                return real[name](*args, **kwargs)
        return call

    with ExitStack() as stack:
        stack.enter_context(patch.object(ps, 'WIN', True))
        for name in CALLS:
            stack.enter_context(patch.object(ps, name, posix(name)))
        stack.enter_context(patch.object(claude_channel, 'folder_walk_available', return_value=folder_walk))
        stack.enter_context(patch.object(claude_channel, '_directory', _forbidden('the handle-based folder walk')))
        stack.enter_context(patch.dict(sys.modules, {'fcntl': None}))     # import fcntl now fails
        stack.enter_context(patch.object(os, 'getuid', _forbidden('os.getuid'), create=True))
        yield calls
