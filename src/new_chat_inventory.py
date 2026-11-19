"""Known launch folders from injected, independently verified native identities.

No transcript parsing, native calls, or import-time I/O. Display aliases never
establish a destination. The source is an internal trust seam, not HTTP input.
"""
from dataclasses import asdict
import hashlib
import hmac
import itertools
import json
import errno
import os
from pathlib import Path
import stat
import sys
import threading
import uuid

from new_chat_types import FrozenFolder, VerifiedIdentity, NewChatError


def canonical_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _valid_path(value):
    return (type(value) is str and os.path.isabs(value) and len(value) <= 4096
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _physical_path(path):
    # Resolving links does not read a directory. stat identities below handle
    # physical equivalence where realpath preserves case or volume aliases.
    return os.path.realpath(path)


def _directory_identity(path):
    try:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            # realpath is best-effort on Python 3.9. A remaining link means
            # resolution was incomplete or raced, rather than proven absence.
            raise OSError(errno.ELOOP, 'Folder anchor could not be resolved.')
        return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None
    except (FileNotFoundError, NotADirectoryError):
        return None


def _ancestry(path):
    rows = []
    while True:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError()
        rows.append((path, (info.st_dev, info.st_ino)))
        parent = os.path.dirname(path)
        if parent == path:
            return rows
        path = parent


def _contains(root, ancestry):
    current = _physical_path(root)
    for spelling in (root, current):
        try:
            if os.path.commonpath((ancestry[0][0], spelling)) == spelling:
                return True
        except ValueError:
            pass  # Distinct Windows drives cannot be ancestors of one another.
    identity = _directory_identity(current)
    return identity is not None and any(row[1] == identity for row in ancestry)


def validate_folder(folder):
    """Recheck the frozen destination, including replacement and link changes."""
    try:
        if (not isinstance(folder, FrozenFolder) or not os.path.isabs(folder.path)
                or os.path.realpath(folder.path) != folder.path):
            raise ValueError()
        info = os.stat(folder.path, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (folder.device, folder.inode):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise NewChatError('folder_changed', 'The selected folder changed. Review its current entry.', 409) from None
    return folder


class KnownFolderInventory:
    def __init__(self, source, key, *, maximum=256, state_root=None, home=None):
        self.source, self.key, self.maximum = source, key, maximum
        self.home = os.fspath(home) if home is not None else os.path.expanduser('~')
        if not _valid_path(self.home):
            raise ValueError('An absolute service home is required.')
        self.state_root = os.path.realpath(os.fspath(state_root)) if state_root is not None else None
        self.lock = threading.RLock()
        self.entries, self.provenance = {}, {}

    def _digest(self, value):
        return hmac.new(self.key, json.dumps(value, ensure_ascii=True, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def _folder(self, path, directory):
        ident = self._digest(['folder', path])
        revision = self._digest(['revision', path, directory.st_dev, directory.st_ino])
        return FrozenFolder(ident, revision, os.path.basename(path) or path,
                            path, directory.st_dev, directory.st_ino)

    def require_allowed(self, folder):
        """Apply target-directory policy without changing shared physical proof."""
        try:
            path = _physical_path(folder.path)
            ancestry = _ancestry(path)
            if ancestry[0][1] != (folder.device, folder.inode):
                raise ValueError()
            if path == _physical_path(self.home) or ancestry[0][1] == _directory_identity(_physical_path(self.home)):
                raise ValueError()
            roots = [os.path.join(self.home, name) for name in ('.live-room', '.claude')]
            if self.state_root is not None:
                roots.append(self.state_root)
            # These exclusions always win, even if an anchor points at a worktree.
            if any(_contains(root, ancestry) for root in roots):
                raise ValueError()
            codex = os.path.join(self.home, '.codex')
            if _contains(codex, ancestry):
                current_codex = _physical_path(codex)
                worktrees = os.path.join(current_codex, 'worktrees')
                # The exception is a genuine directory below the Codex root,
                # never a symlink laundering sessions or the entire private root.
                worktree_identity = _directory_identity(worktrees)
                if worktree_identity is None:
                    raise ValueError()
                worktree_ancestry = _ancestry(worktrees)
                codex_identity = _directory_identity(current_codex)
                if (codex_identity is None or codex_identity not in [row[1] for row in worktree_ancestry[1:]]
                        or not _contains(worktrees, ancestry)):
                    raise ValueError()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise NewChatError('invalid_folder', 'Motet cannot use that folder.', 400) from None
        return folder

    def restore_registered(self, ident, value):
        """Validate a stored identity without repairing it or doing filesystem I/O."""
        if (type(value) is not dict or set(value) != set(FrozenFolder.__dataclass_fields__)
                or any(type(value[name]) is not str for name in ('id', 'revision', 'label', 'path'))
                or any(type(value[name]) is not int or not 0 <= value[name] < 2**64 for name in ('device', 'inode'))
                or not _valid_path(value['path']) or not value['label'] or len(value['label']) > 4096
                or any(0xD800 <= ord(c) <= 0xDFFF for c in value['label'])
                or type(ident) is not str or ident != value['id']
                or not hmac.compare_digest(value['id'], self._digest(['folder', value['path']]))
                or not hmac.compare_digest(value['revision'], self._digest(
                    ['revision', value['path'], value['device'], value['inode']]))):
            raise ValueError('Invalid saved folder identity.')
        return FrozenFolder(**value)

    def register(self, path):
        if type(path) is str and path.startswith('~/'):
            # Only the service account's home-relative spelling is accepted.
            # This is path handling, never shell or environment interpolation.
            path = self.home + path[1:]
        if not _valid_path(path):
            raise NewChatError('invalid_folder', 'Enter a full folder path on the computer running Motet.', 400)
        try:
            resolved = str(Path(path).resolve(strict=True))
            directory = os.stat(resolved, follow_symlinks=False)
            if stat.S_ISREG(directory.st_mode):
                raise NewChatError('invalid_folder', 'That is a file, not a folder.', 400)
            if sys.platform == 'darwin':
                # Follow macOS's physical spelling, including case-insensitive volumes.
                import fcntl
                fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    resolved = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0])
                    directory = os.fstat(fd)
                finally:
                    os.close(fd)
            if not _valid_path(resolved) or resolved == os.path.sep:
                raise ValueError()
            folder = self._folder(resolved, directory)
            return self.require_allowed(validate_folder(folder))
        except NewChatError as error:
            if error.code == 'invalid_folder':
                raise
            raise NewChatError('invalid_folder', 'Motet could not verify that folder. Check the path and try again.', 400) from None
        except (OSError, RuntimeError, ValueError):
            raise NewChatError('invalid_folder', 'Motet could not verify that folder. Check the path and try again.', 400) from None

    def refresh(self, registered=()):
        entries, provenance = {}, {}
        try:
            rows = list(itertools.islice(iter(self.source()), self.maximum + 1))
            if len(rows) > self.maximum:
                raise ValueError()
            for identity in rows:
                # The source must have verified root/native-index semantics. Check
                # its pinned file and the actual directory again at this boundary.
                if not isinstance(identity, VerifiedIdentity) or not canonical_uuid(identity.native_id):
                    continue
                path, source = identity.cwd, identity.source_path
                if (type(path) is not str or not os.path.isabs(path) or len(path) > 4096
                        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in path)
                        or type(source) is not str or not os.path.isabs(source)
                        or os.path.realpath(source) != source):
                    continue
                try:
                    file = os.stat(source, follow_symlinks=False)
                    resolved = os.path.realpath(path)
                    directory = os.stat(resolved, follow_symlinks=False)
                    if (not stat.S_ISREG(file.st_mode) or file.st_nlink != 1
                            or (file.st_dev, file.st_ino) != (identity.source_device, identity.source_inode)
                            or not stat.S_ISDIR(directory.st_mode)):
                        continue
                    folder = self.require_allowed(validate_folder(self._folder(resolved, directory)))
                    ident = folder.id
                except (OSError, NewChatError):
                    continue
                entries[ident] = folder
                provenance.setdefault(ident, []).append({
                    'native_id': identity.native_id, 'cwd': path, 'source_path': source,
                    'source_device': file.st_dev, 'source_inode': file.st_ino,
                    'adapter_version': identity.adapter_version,
                })
        except Exception:
            # An incomplete inventory is never authority to retain stale entries.
            entries, provenance = {}, {}
        for folder in registered:
            try:
                self.require_allowed(validate_folder(folder))
            except NewChatError:
                continue
            entries[folder.id] = folder
            provenance.setdefault(folder.id, [])
        with self.lock:
            self.entries, self.provenance = entries, provenance
            return list(entries.values())

    def get(self, ident, revision, registered=()):
        self.refresh(registered)
        with self.lock:
            folder = self.entries.get(ident)
            if folder is None or folder.revision != revision:
                raise NewChatError('folder_changed', 'The selected folder is no longer verified. Review the folder list.', 409)
            return self.require_allowed(validate_folder(folder))

    def snapshot(self):
        with self.lock:
            return [{'folder': asdict(folder), 'provenance': list(self.provenance[ident])}
                    for ident, folder in self.entries.items()]


def public_folder(folder):
    return {'id': folder.id, 'revision': folder.revision,
            'label': folder.label, 'displayPath': folder.path}
