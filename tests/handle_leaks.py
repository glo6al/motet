"""Fail a test when a call leaves a file or database open behind it. Tests only.

Windows will not delete or replace a file that is still open, so a connection or descriptor that only garbage
collection would close breaks cleanup there (and can keep a lock or database busy). Two checks, both on any system:
garbage collection is paused during the call, and on POSIX the process must hold no more descriptors after it than
before; then a collection must not report an unclosed SQLite connection or file (ResourceWarning, which SQLite
gives from Python 3.13).
"""
from contextlib import contextmanager
import gc
import os
import warnings


def _open_descriptors():
    for folder in ('/proc/self/fd', '/dev/fd'):
        if os.path.isdir(folder):
            return len(os.listdir(folder))
    return None                                  # Windows: only the warning check applies


@contextmanager
def no_leaked_handles(test):
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            before = _open_descriptors()
            yield
            after = _open_descriptors()
            gc.collect()
    finally:
        if enabled:
            gc.enable()
    test.assertEqual(after, before, 'descriptors left open')
    test.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [],
                     'handles closed only by garbage collection')
