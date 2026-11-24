"""Descriptor-relative private attachment IO; never receives a project path.

The folders are held through the no-link walk (``private_storage``): by descriptor on POSIX, by handle on Windows.
On POSIX every file check is the one this module has always made (modes exactly 0600 and 0700, this user's uid, one
name). Windows' stat has no owner or mode, so there the same rule is read on the opened handle: owner and access
list, type, no reparse point, one name. Windows storage must be on an NTFS volume (see ``_id_ok``)."""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import threading

import private_storage as ps
from attachments_types import AttachmentError, fail, ident


_MAX_WORKSPACE_PINS = 64
# Strong process roots. Entries are only added or removed under the owning
# PrivateFiles I/O lock. A quarantined entry has no in-process release path.
_WORKSPACE_PINS = {}


class _WorkspacePin:
    __slots__ = ('files', 'installation', 'request', 'keys', 'lock_fd', 'uncertain', 'finishing')

    def __init__(self, files, installation, request, keys):
        self.files, self.installation, self.request, self.keys = files, installation, request, keys
        self.lock_fd, self.uncertain, self.finishing = None, False, False


def identity(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _unavailable():
    fail('storage_unavailable', 'Private attachment storage is unavailable.', 503)


def _posix_private(st, directory=False):
    """The POSIX rule on a stat result: the right type, this user's uid, mode exactly 0600 (0700 for a folder) and
    one name. Returns the identity."""
    if (not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600)
            or not directory and st.st_nlink != 1):
        _unavailable()
    return identity(st)


def private(st, directory=False):
    """The POSIX rule on a stat result, for callers outside the store. Windows' stat carries no owner or mode, so
    there it always refuses: use ``private_fd`` on an open descriptor instead (a call that reaches here on Windows
    fails closed)."""
    if ps.WIN:
        _unavailable()
    return _posix_private(st, directory)


# Identities are stored and checked as integers of at most 2**63-1 (the validators in attachments_catalogue,
# attachments_conversations, attachments_deletion_slots and others). NTFS file references are 64 bits (a 48-bit
# record number and a 16-bit sequence number) and unique on their volume, so storage is kept to NTFS; ReFS's 128-bit
# ids do not fit, and FAT has no access lists. An NTFS reference whose sequence number has its top bit set would not
# fit either: such a file is refused rather than stored under an identity that cannot be checked.
_ID_LIMIT = 2 ** 63 - 1


def _id_ok(values):
    """A Windows identity (``private_storage.file_identity``: volume serial, file index, size, write time, creation
    time) as a list, refused when the volume or index does not fit the stored range."""
    if not (0 <= values[0] <= _ID_LIMIT and 0 < values[1] <= _ID_LIMIT):
        _unavailable()
    return list(values)


def _private_fd(fd, windows, directory=False):
    """``private`` for an open descriptor. POSIX: exactly ``private(os.fstat(fd))``. Windows: the owner, access list,
    type and link count are read on its handle, and the identity comes from the handle too (its 32-bit volume serial
    and 64-bit file index, size and times), never from Python's fstat, whose st_dev and st_ino changed in 3.12."""
    if not windows:
        return _posix_private(os.fstat(fd), directory)
    try:
        ps.check_private_fd(fd, directory, single_link=not directory)
    except ps.PrivateStorageError:
        _unavailable()
    return _id_ok(ps.file_identity(fd))


def private_fd(fd, directory=False):
    return _private_fd(fd, ps.WIN, directory)


def _folder_identity(folder):
    """[volume, file index] of a held Windows folder, from its handle; its owner and access list were checked when
    it was opened."""
    return _id_ok(folder.identity())[:2]


class _BufferLease:
    """Opaque preparation ownership. Transfer disables this object's close."""
    __slots__ = ('_files', '_token')

    def __init__(self, files, token):
        self._files, self._token = files, token

    @property
    def source_ids(self):
        with self._files._io_lock:
            return self._files._buffer_entry(self)['ids']

    def close(self):
        self._files._release_preparation_buffer(self)

    def __enter__(self):
        with self._files._io_lock:
            if self._files._buffer_entry(self)['state'] != 'preparation':
                fail('buffer_lease_invalid', 'Attachment buffer ownership changed.', 409)
        return self

    def __exit__(self, *args):
        self.close()


class _BufferClaim:
    __slots__ = ('_files', '_token', '_owner')

    def __init__(self, files, token, owner):
        self._files, self._token, self._owner = files, token, owner


class _BufferResultLease:
    __slots__ = ('_files', '_token', '_owner')

    def __init__(self, files, token, owner):
        self._files, self._token, self._owner = files, token, owner

    def close(self):
        with self._files._io_lock:
            entry = self._files._buffer_owned(self)
            if entry['state'] != 'result' or entry['result'] is not self:
                fail('buffer_lease_invalid', 'Attachment result ownership changed.', 409)
            del self._files._buffers[self._token]
            self._files._registry_changed()


class PrivateFiles:
    def __init__(self, state_dir):
        self.root_fd = self.objects_fd = self.lock_fd = None
        self._io_lock, self._io, self._closing = threading.Lock(), {}, False
        self._io_condition = threading.Condition(self._io_lock)
        self._io_generation = 0
        self._buffers, self._buffer_pid, self._buffer_epoch = {}, os.getpid(), object()
        self._workspace_pins, self._workspace_epoch = set(), object()
        self._root = self._objects = self._db_hold = None
        self._windows = ps.WIN
        if not ps.folder_walk_available():
            fail('host_unavailable', 'This host cannot safely store attachments.', 503)
        if self._windows:
            # The folders above are resolved once (a moved profile often uses a junction); the walk then opens each
            # by handle and refuses any reparse point, as messaging does for the same folder.
            self.parent = Path(ps.anchored(os.path.abspath(state_dir)))
        else:
            self.parent = Path(os.path.abspath(state_dir))
        self.root = self.parent / 'attachments'
        try:
            self._open(str(self.parent))
        except Exception:
            self.close()
            raise

    def _posix_root(self):
        """The storage folder on POSIX, found exactly as before: no ancestor may be a link or anything but a folder
        (checked with lstat, which needs only search permission on the folders above), the state folder must be
        private (0700), then ``attachments`` is made 0700 if missing and opened without following a link. Its
        descriptor is then held as a NoLinkFolder, so everything below it goes through the same calls as Windows."""
        for ancestor in (self.parent, *self.parent.parents):
            st = ancestor.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _unavailable()
        _posix_private(self.parent.lstat(), True)
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._root = ps.NoLinkFolder(str(self.root), False, fd=self.root_fd, writable=True)

    def _windows_root(self, parent_path):
        """The storage folder on Windows: the walk opens every name from the drive down by handle, refusing any
        reparse point, checks the state folder's owner and access list, and requires an NTFS volume. ``attachments``
        is made private if missing, then held alone: its one handle keeps the folders above it in place."""
        try:
            parent = ps.open_folder(parent_path, private=True, writable=True)
        except ps.PrivateStorageError:
            _unavailable()
        with parent:
            if parent.file_system() != 'NTFS':
                _unavailable()
            try:
                parent.mkdir('attachments')
            except FileExistsError:
                pass
            try:
                self._root = parent.folder('attachments', private=True, writable=True, alone=True)
            except ps.PrivateStorageError:      # a junction, or a folder that is not private
                _unavailable()

    def _open(self, parent_path):
        """Hold ``attachments`` and ``objects`` for this storage's lifetime and take the writer lock. Only those two
        folders are kept: on POSIX each is one descriptor, on Windows one handle that also keeps every folder above
        it in place (``open_folder(alone=True)``)."""
        windows = self._windows
        if windows:
            self._windows_root(parent_path)
        else:
            self._posix_root()
        self.root_identity = self._held_identity(self._root)
        try:
            self.lock_fd = self._root.open_or_create('writer.lock')
        except ps.PrivateStorageError:
            _unavailable()
        self.lock_identity = self.private_fd(self.lock_fd)[:2]
        if not ps.try_lock(self.lock_fd):
            raise BlockingIOError(errno.EWOULDBLOCK, 'Attachment storage is in use by another Motet.')
        try:
            self._root.mkdir('objects')
        except FileExistsError:
            pass
        try:
            self._objects = self._root.folder('objects', private=windows, writable=True, alone=True)
        except ps.PrivateStorageError:
            _unavailable()
        self.objects_identity = self._held_identity(self._objects)
        self._root.sync()
        if not windows:
            self.root_fd, self.objects_fd = self._root._fd, self._objects._fd

    def _held_identity(self, folder):
        if self._windows:
            return _folder_identity(folder)        # its owner and access list were checked when it was opened
        return _posix_private(os.fstat(folder._fd), True)[:2]

    def verify(self):
        if not self._windows:
            if _posix_private(self.root.lstat(), True)[:2] != self.root_identity:
                fail('storage_unavailable', 'Attachment storage changed.', 503)
            if _posix_private(os.stat('writer.lock', dir_fd=self.root_fd, follow_symlinks=False))[:2] != self.lock_identity:
                fail('storage_unavailable', 'Attachment storage lock changed.', 503)
            if _posix_private(os.stat('objects', dir_fd=self.root_fd, follow_symlinks=False), True)[:2] != self.objects_identity:
                fail('storage_unavailable', 'Attachment storage changed.', 503)
            return
        # Windows: walk to the storage folder again by name, as POSIX looks at it by path, and compare identities.
        try:
            with ps.open_folder(str(self.root), private=True) as again:
                if _folder_identity(again) != self.root_identity:
                    fail('storage_unavailable', 'Attachment storage changed.', 503)
                fd = again.open('writer.lock', single_link=True, private=True)
                try:
                    if self.private_fd(fd)[:2] != self.lock_identity:
                        fail('storage_unavailable', 'Attachment storage lock changed.', 503)
                finally:
                    os.close(fd)
                with again.folder('objects', private=True) as objects:
                    if _folder_identity(objects) != self.objects_identity:
                        fail('storage_unavailable', 'Attachment storage changed.', 503)
        except OSError:
            fail('storage_unavailable', 'Attachment storage changed.', 503)

    def database(self):
        self.verify()
        if self._windows:
            # Kept open, without delete sharing, for this storage's lifetime: SQLite then opens it by path, and nothing
            # can rename, remove or replace that name meanwhile (SQLite's own handle shares the same way).
            if self._db_hold is not None:
                os.close(self._db_hold)
                self._db_hold = None
            try:
                self._db_hold = self._root.open_or_create('ledger.sqlite3')
            except ps.PrivateStorageError:
                _unavailable()
            self.db_identity = self.private_fd(self._db_hold)[:2]
        else:
            fd = os.open('ledger.sqlite3', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.root_fd)
            try:
                self.db_identity = _posix_private(os.fstat(fd))[:2]
            finally:
                os.close(fd)
        self.verify_database()  # Reject unsafe sidecars before SQLite can open them.
        return self.root / 'ledger.sqlite3'

    def verify_database(self):
        self.verify()
        if self._windows:
            try:
                fd = self._root.open('ledger.sqlite3', single_link=True, private=True)
            except OSError:
                fail('storage_unavailable', 'Attachment storage changed.', 503)
            try:
                if self.private_fd(fd)[:2] != self.db_identity:
                    fail('storage_unavailable', 'Attachment storage changed.', 503)
            finally:
                os.close(fd)
            # SQLite makes its journal by name, with the folder's inherited access list (private, but not protected
            # as a file made here is), so the file rule would refuse it: it must be a plain file with one name, owned
            # by this user, whose list names only this user, SYSTEM, Administrators or OWNER RIGHTS.
            if not ps.sqlite_companions_private(str(self.root / 'ledger.sqlite3')):
                _unavailable()
            return
        if _posix_private(os.stat('ledger.sqlite3', dir_fd=self.root_fd, follow_symlinks=False))[:2] != self.db_identity:
            fail('storage_unavailable', 'Attachment storage changed.', 503)
        for name in ('ledger.sqlite3-journal', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm'):
            try:
                _posix_private(os.stat(name, dir_fd=self.root_fd, follow_symlinks=False))
            except FileNotFoundError:
                pass

    def free_bytes(self):
        """Free space for new objects: ``fstatvfs`` on the held objects folder on POSIX, as before; on Windows
        ``shutil.disk_usage`` of that folder, which the held handle keeps in place."""
        if self._windows:
            return shutil.disk_usage(str(self.root / 'objects')).free
        free = os.fstatvfs(self.objects_fd)
        return free.f_bavail * free.f_frsize

    def object_stat(self, file_id):
        """The stat of the object name itself, not followed: FileNotFoundError when it is missing."""
        ident(file_id)
        if self._windows:
            return self._objects.child_stat(file_id)
        return os.stat(file_id, dir_fd=self.objects_fd, follow_symlinks=False)

    def object_identity(self, file_id):
        """``private`` identity of the object by name, not followed: FileNotFoundError when it is missing."""
        ident(file_id)
        if not self._windows:
            return _posix_private(os.stat(file_id, dir_fd=self.objects_fd, follow_symlinks=False))
        try:
            fd = self._objects.open(file_id, single_link=True, private=True)
        except ps.PrivateStorageError:
            _unavailable()
        try:
            return self.private_fd(fd)
        finally:
            os.close(fd)

    def private_fd(self, fd, directory=False):
        """``private_fd`` on this storage's own system (decided once, when it opened)."""
        return _private_fd(fd, self._windows, directory)

    def create(self, file_id):
        self.verify()
        ident(file_id)
        try:
            fd = self._objects.create(file_id)
        except ps.PrivateStorageError:
            _unavailable()
        try:
            self.private_fd(fd)
            self._objects.sync()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _begin_io(self, file_id, kind):
        # Only bookkeeping under this mutex: no file IO, database lock or callback.
        with self._io_lock:
            if self._closing:
                fail('storage_unavailable', 'Private attachment storage is closing.', 503)
            if any(value[2] == 'uncertain' for value in self._io.values()):
                if kind == 'remove':
                    return None
                fail('source_unavailable', 'An attachment handle could not be confirmed closed.', 503)
            active = [value for value in self._io.values() if value[0] == file_id]
            if kind == 'remove' and (active or any(file_id in b['ids'] for b in self._buffers.values())):
                return None
            if kind == 'read' and any(value[1] == 'remove' for value in active):
                fail('source_unavailable', 'Attachment cleanup is still active.', 409)
            token = object()
            self._io[token] = [file_id, kind, 'unopened']
            return token

    def _opened(self, token):
        with self._io_lock:
            self._io[token][2] = 'open'

    def _finish_io(self, token, fd):
        # A failed close can leave the descriptor live. Never retry its number or
        # release its pin by elapsed time, pathname absence or an exception alone.
        if fd is not None:
            try:
                os.close(fd)
            except BaseException:
                with self._io_lock:
                    self._io[token][2] = 'uncertain'
                    self._registry_changed()
                raise
        with self._io_lock:
            del self._io[token]
            self._registry_changed()

    def _registry_changed(self):
        """Registry bookkeeping only; caller holds _io_lock."""
        self._io_generation += 1
        self._io_condition.notify_all()

    def wake_cleanup(self):
        with self._io_lock:
            self._registry_changed()

    def cleanup_registry(self, file_id=None):
        """No IO/DB/callbacks; uncertain ownership is never timed out."""
        with self._io_lock:
            uncertain = any(v[2] == 'uncertain' for v in self._io.values()) or any(
                b['state'] == 'uncertain' and (file_id is None or file_id in b['ids']) for b in self._buffers.values())
            busy = any(file_id is None or v[0] == file_id for v in self._io.values()) or any(
                file_id is None or file_id in b['ids'] for b in self._buffers.values())
            return self._io_generation, 'uncertain' if uncertain else 'busy' if busy else 'idle'

    def wait_cleanup(self, generation, stop):
        with self._io_condition:
            self._io_condition.wait_for(lambda: self._io_generation != generation or stop.is_set())

    def io_active(self):
        if self._workspace_pins:
            self._workspace_process()
        with self._io_lock:
            return bool(self._io or self._buffers or self._workspace_pins)

    def begin_close(self):
        if self._workspace_pins:
            self._workspace_process()
        with self._io_lock:
            if self._io or self._buffers or self._workspace_pins:
                fail('upload_busy', 'Attachment file handles are still active or uncertain.', 409)
            self._closing = True

    def _workspace_process(self):
        # Reject inherited authority before taking a mutex inherited from a
        # different process. No inherited descriptor number is retried.
        if self._buffer_pid != os.getpid():
            fail('storage_unavailable', 'Attachment workspace ownership changed.', 503)

    def _workspace_identity(self):
        return (self._workspace_epoch, self.root_identity, self.lock_identity,
                self.objects_identity, self.db_identity)

    def _reserve_workspace(self, request, keys):
        self._workspace_process()
        with self._io_lock:
            if self._closing or self.lock_fd is None or not hasattr(self, 'db_identity'):
                fail('storage_unavailable', 'Attachment storage is closing or unavailable.', 503)
            if any(pin.keys & keys or pin.request == request and (not pin.keys or not keys)
                   for pin in self._workspace_pins):
                fail('workspace_handles_busy', 'Attachment workspace handles are active or uncertain.', 409)
            if len(self._workspace_pins) >= _MAX_WORKSPACE_PINS:
                fail('workspace_handle_capacity', 'Attachment workspace handle capacity is unavailable.', 503)
            pin = _WorkspacePin(self, self._workspace_identity(), request, keys)
            self._workspace_pins.add(pin)
            _WORKSPACE_PINS[pin] = self
            return pin

    def _duplicate_workspace_lock(self, pin):
        self._workspace_process()
        with self._io_lock:
            if pin not in self._workspace_pins or pin.lock_fd is not None or pin.uncertain or pin.finishing:
                fail('storage_unavailable', 'Attachment workspace ownership changed.', 503)
        self.verify_database()
        if self.private_fd(self.lock_fd)[:2] != self.lock_identity:
            fail('storage_unavailable', 'Attachment storage lock changed.', 503)
        # dup keeps the held flock's open file description. It is non-inheritable
        # across exec. Neither this pin nor PrivateFiles explicitly unlocks it.
        pin.lock_fd = os.dup(self.lock_fd)
        if (self.private_fd(pin.lock_fd)[:2] != self.lock_identity
                or pin.installation != self._workspace_identity()):
            fail('storage_unavailable', 'Attachment storage lock changed.', 503)

    def _finish_workspace(self, pin, uncertain):
        self._workspace_process()
        with self._io_lock:
            if pin not in self._workspace_pins or pin.finishing:
                fail('storage_unavailable', 'Attachment workspace ownership changed.', 503)
            if pin.uncertain:
                return True
            pin.finishing = True
        if not uncertain and pin.lock_fd is not None:
            try:
                os.close(pin.lock_fd)
            except BaseException:
                # The descriptor number could already have been reused. Keep
                # the base lifetime fenced too, with no later close attempt.
                uncertain = True
        with self._io_lock:
            if uncertain:
                pin.uncertain = True
                pin.finishing = False
            else:
                self._workspace_pins.remove(pin)
                del _WORKSPACE_PINS[pin]
            self._registry_changed()
        return uncertain

    def _buffer_entry(self, lease):
        # Caller holds _io_lock. No IO, DB callbacks or foreign locks here.
        entry = self._buffers.get(lease._token) if type(lease) is _BufferLease and lease._files is self else None
        if (entry is None or entry['lease'] is not lease or self._closing
                or entry['pid'] != os.getpid() or entry['epoch'] is not self._buffer_epoch):
            fail('buffer_lease_invalid', 'Attachment buffer ownership changed.', 409)
        return entry

    def _buffer_owned(self, claim):
        entry = self._buffers.get(claim._token) if type(claim) in (_BufferClaim, _BufferResultLease) and claim._files is self else None
        if (entry is None or entry['owner'] is not claim._owner or self._closing
                or entry['pid'] != os.getpid() or entry['epoch'] is not self._buffer_epoch):
            fail('buffer_lease_invalid', 'Attachment buffer ownership changed.', 409)
        return entry

    def buffer_lease(self, source_ids):
        if type(source_ids) not in (tuple, list) or not 1 <= len(source_ids) <= 50:
            fail('buffer_lease_invalid', 'Use a bounded exact attachment source set.', 400)
        ids = tuple(sorted(ident(value) for value in source_ids))
        if len(set(ids)) != len(ids):
            fail('buffer_lease_invalid', 'Use unique attachment sources.', 400)
        with self._io_lock:
            if self._closing or self._buffer_pid != os.getpid():
                fail('buffer_lease_invalid', 'Attachment buffer ownership changed.', 409)
            if (any(value[2] == 'uncertain' for value in self._io.values())
                    or any(value[0] in ids and value[1] == 'remove' for value in self._io.values())):
                fail('resource_busy', 'Attachment file handles are still active or uncertain.', 409)
            if len(self._buffers) >= 4:
                fail('resource_busy', 'Attachment buffers are busy.', 409)
            token = object()
            lease = _BufferLease(self, token)
            self._buffers[token] = dict(lease=lease, ids=ids, state='preparation', pid=self._buffer_pid,
                epoch=self._buffer_epoch, owner=None, claim=None, result=None, worker=None, start_attempted=False)
        try:
            # Pin first, then inspect existing entries without the registry mutex.
            self.verify()
            for source_id in ids:
                self.object_identity(source_id)
        except BaseException:
            lease.close()
            raise
        return lease

    def _release_preparation_buffer(self, lease):
        with self._io_lock:
            entry = self._buffer_entry(lease)
            if entry['state'] != 'preparation':
                fail('buffer_lease_invalid', 'Attachment buffers belong to an active operation.', 409)
            del self._buffers[lease._token]
            self._registry_changed()

    def _reserve_buffer_handoff(self, lease, source_ids, owner):
        with self._io_lock:
            entry = self._buffer_entry(lease)
            if entry['state'] != 'preparation' or entry['ids'] != tuple(sorted(source_ids)):
                fail('buffer_lease_invalid', 'Attachment buffer sources or ownership changed.', 409)
            claim = _BufferClaim(self, lease._token, owner)
            entry.update(state='reserved', owner=owner, claim=claim)
            return claim

    def _buffer_start_attempt(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if entry['claim'] is not claim or entry['state'] != 'reserved' or entry['start_attempted']:
                fail('buffer_lease_invalid', 'Attachment worker ownership changed.', 409)
            entry['start_attempted'] = True

    def _buffer_worker_enter(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if entry['claim'] is not claim or entry['state'] != 'reserved' or entry['worker'] is not None:
                fail('buffer_lease_invalid', 'Attachment worker ownership changed.', 409)
            entry['worker'] = threading.current_thread()

    def _buffer_worker_ready(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if (entry['claim'] is not claim or entry['state'] != 'reserved'
                    or entry['worker'] is not threading.current_thread()):
                fail('buffer_lease_invalid', 'Attachment worker ownership changed.', 409)
            entry['state'] = 'worker'

    def _release_local_buffer(self, claim):
        # Only a constructor failure before any Thread.start attempt is local.
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if (entry['claim'] is not claim or entry['state'] != 'reserved'
                    or entry['start_attempted'] or entry['worker'] is not None):
                fail('buffer_lease_invalid', 'Attachment worker completion is unconfirmed.', 409)
            del self._buffers[claim._token]
            self._registry_changed()

    def _release_worker_buffer(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if (entry['claim'] is not claim or entry['state'] not in ('reserved', 'worker')
                    or entry['worker'] is not threading.current_thread()):
                fail('buffer_lease_invalid', 'Attachment worker completion is unconfirmed.', 409)
            del self._buffers[claim._token]
            self._registry_changed()

    def _uncertain_worker_buffer(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if entry['claim'] is not claim or entry['worker'] is not threading.current_thread():
                fail('buffer_lease_invalid', 'Attachment worker ownership changed.', 409)
            entry['state'] = 'uncertain'
            self._registry_changed()

    def _buffer_result(self, claim):
        with self._io_lock:
            entry = self._buffer_owned(claim)
            if (entry['claim'] is not claim or entry['state'] != 'worker'
                    or entry['worker'] is not threading.current_thread()):
                fail('buffer_lease_invalid', 'Attachment result ownership changed.', 409)
            result = _BufferResultLease(self, claim._token, claim._owner)
            entry.update(state='result', result=result)
            return result

    def read(self, file_id, expected, expected_digest, maximum):
        ident(file_id)
        token, fd = self._begin_io(file_id, 'read'), None
        try:
            self.verify()
            fd = self.open_object(file_id)
            self._opened(token)
            before = self.private_fd(fd)
            if before != expected or before[2] > maximum:
                fail('source_changed', 'The saved attachment could not be verified.')
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if (len(data) > maximum or self.private_fd(fd) != before
                    or hashlib.sha256(data).hexdigest() != expected_digest):
                fail('source_changed', 'The saved attachment could not be verified.')
            return bytes(data)
        finally:
            self._finish_io(token, fd)

    def open_object(self, file_id):
        """A read-only descriptor for the object: ``O_RDONLY | O_NOFOLLOW`` by the held folder on POSIX, as before;
        on Windows through the walk, refusing a reparse point as POSIX's ELOOP does and checking the handle."""
        if not self._windows:
            return os.open(file_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.objects_fd)
        try:
            return self._objects.open(file_id, single_link=True, private=True)
        except ps.PrivateStorageError as error:
            if error.reason == 'link':
                raise OSError(errno.ELOOP, 'private storage: a link in place of an attachment') from None
            _unavailable()

    def remove(self, file_id, expected, expected_digest=None, *, durable_absence=False):
        ident(file_id)
        token, fd = self._begin_io(file_id, 'remove'), None
        if token is None:
            return 'busy'
        if self._windows:
            try:
                self.verify()
                return self._windows_remove(token, file_id, expected, expected_digest, durable_absence)
            finally:
                self._finish_io(token, None)
        try:
            self.verify()
            try:
                fd = os.open(file_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.objects_fd)
            except FileNotFoundError:
                if durable_absence:
                    os.fsync(self.objects_fd)
                return 'absent'
            except OSError as error:
                if durable_absence and error.errno == errno.ELOOP:
                    return 'changed'
                raise
            self._opened(token)
            try:
                current = _posix_private(os.fstat(fd))
            except AttachmentError:
                if durable_absence:
                    return 'changed'
                raise
            if expected is None or current != expected:
                return 'changed'
            if expected_digest:
                h = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    h.update(chunk)
                if h.hexdigest() != expected_digest or _posix_private(os.fstat(fd)) != current:
                    return 'changed'
            # Private directory serializes WATCH operations; same-UID hostile namespace
            # mutation is outside this storage profile, not an isolation guarantee.
            if _posix_private(os.stat(file_id, dir_fd=self.objects_fd, follow_symlinks=False)) != current:
                return 'changed'
            os.unlink(file_id, dir_fd=self.objects_fd)
            os.fsync(self.objects_fd)
            return 'removed'
        finally:
            self._finish_io(token, fd)

    def _windows_remove(self, token, file_id, expected, expected_digest, durable_absence):
        """POSIX's decisions, but the file is deleted through the handle the checks were made on, so no name can be
        swapped between them. The walk closes that handle itself. 'busy' (another program has the file open without
        sharing delete, or an earlier removal waits for one to close it) is retried later, as for an active read."""
        refused = []

        def recognise(fd):
            self._opened(token)
            try:
                current = self.private_fd(fd)
            except AttachmentError as error:
                if not durable_absence:
                    refused.append(error)
                return False
            if expected is None or current != expected:
                return False
            if expected_digest:
                h = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    h.update(chunk)
                if h.hexdigest() != expected_digest or self.private_fd(fd) != current:
                    return False
            return True
        outcome = self._objects.remove(file_id, recognise)
        if refused:
            raise refused[0]
        if outcome == 'changed' and not durable_absence:
            # As on POSIX, where O_NOFOLLOW meets a link with ELOOP: a link in place of the object is that error, not
            # a quiet 'changed' (with durable_absence POSIX also answers 'changed').
            try:
                if stat.S_ISLNK(self._objects.child_stat(file_id).st_mode):
                    raise OSError(errno.ELOOP, 'private storage: a link in place of an attachment')
            except FileNotFoundError:
                pass
        if outcome == 'removed' or outcome == 'absent' and durable_absence:
            self._objects.sync()
        return outcome

    def close(self):
        self.begin_close()
        # Objects first, then the lock, then the storage folder, as before.
        if self._objects is not None:
            self._objects.close()
            self._objects = self.objects_fd = None
        if self.lock_fd is not None:
            if self._windows:
                ps.close_lock(self.lock_fd)        # Windows may keep a byte lock a moment after its file closes
            else:
                os.close(self.lock_fd)
            self.lock_fd = None
        if self._db_hold is not None:
            os.close(self._db_hold)
            self._db_hold = None
        if self._root is not None:
            self._root.close()
            self._root = self.root_fd = None
