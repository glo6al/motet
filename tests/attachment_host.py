"""Where the attachment store's tests can run, and the private folder they keep it in. Tests only.

The store runs wherever the no-link folder walk does: macOS and Linux by descriptor, Windows by handle. A test that
checks POSIX modes, links or descriptor accounting says so with ``POSIX`` instead.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import private_storage

STORE = private_storage.folder_walk_available()
STORE_WHY = 'The private attachment store needs the no-link folder walk.'
POSIX = os.name == 'posix'
POSIX_WHY = 'Checks POSIX modes, links or descriptors.'


def state_root(temporary):
    """The folder a test keeps the store's state in, inside its own temporary folder. On POSIX the temporary folder
    itself (private, mode 0700), as before. On Windows a new private folder in it: an elevated test runner can own
    new folders as Administrators, which the store's owner rule refuses, so this one is made owned by this user."""
    root = Path(os.path.realpath(temporary))
    if os.name != 'nt':
        return root
    state = root / 'state'
    private_storage.make_private_folder(str(state))
    return state
