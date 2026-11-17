"""Known launch folders from injected, independently verified native identities.

No transcript parsing, native calls, or import-time I/O. Display aliases never
establish a destination. The source is an internal trust seam, not HTTP input.
"""
from dataclasses import asdict
import hashlib
import hmac
import itertools
import json
import os
import stat
import threading
import uuid

from new_chat_types import FrozenFolder, VerifiedIdentity, NewChatError


def canonical_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


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
    def __init__(self, source, key, *, maximum=256):
        self.source, self.key, self.maximum = source, key, maximum
        self.lock = threading.RLock()
        self.entries, self.provenance = {}, {}

    def _digest(self, value):
        return hmac.new(self.key, json.dumps(value, ensure_ascii=True, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def refresh(self):
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
                    ident = self._digest(['folder', resolved])
                    revision = self._digest(['revision', resolved, directory.st_dev, directory.st_ino])
                    folder = FrozenFolder(ident, revision, os.path.basename(resolved) or resolved,
                                          resolved, directory.st_dev, directory.st_ino)
                    validate_folder(folder)
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
        with self.lock:
            self.entries, self.provenance = entries, provenance
            return list(entries.values())

    def get(self, ident, revision):
        self.refresh()
        with self.lock:
            folder = self.entries.get(ident)
            if folder is None or folder.revision != revision:
                raise NewChatError('folder_changed', 'The selected folder is no longer verified. Review the folder list.', 409)
            return validate_folder(folder)

    def snapshot(self):
        with self.lock:
            return [{'folder': asdict(folder), 'provenance': list(self.provenance[ident])}
                    for ident, folder in self.entries.items()]


def public_folder(folder):
    return {'id': folder.id, 'revision': folder.revision,
            'label': folder.label, 'displayPath': folder.path}
