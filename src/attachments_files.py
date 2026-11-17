"""Descriptor-relative private attachment IO; never receives a project path."""
import errno
import hashlib
import os
from pathlib import Path
import stat
import threading

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


def private(st, directory=False):
    if (not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600)
            or not directory and st.st_nlink != 1):
        fail('storage_unavailable', 'Private attachment storage is unavailable.', 503)
    return identity(st)


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
        if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
            fail('host_unavailable', 'This host cannot safely store attachments.', 503)
        import fcntl
        self.fcntl = fcntl
        self.parent = Path(os.path.abspath(state_dir))
        # Do not resolve away a symlink and then call the destination trusted.
        for ancestor in (self.parent, *self.parent.parents):
            st = ancestor.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                fail('storage_unavailable', 'Private attachment storage is unavailable.', 503)
        private(self.parent.lstat(), True)
        self.root = self.parent / 'attachments'
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.root_identity = private(os.fstat(self.root_fd), True)[:2]
            self.lock_fd = os.open('writer.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.root_fd)
            self.lock_identity = private(os.fstat(self.lock_fd))[:2]
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.mkdir('objects', 0o700, dir_fd=self.root_fd)
            except FileExistsError:
                pass
            self.objects_fd = os.open('objects', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)
            self.objects_identity = private(os.fstat(self.objects_fd), True)[:2]
            os.fsync(self.root_fd)
        except Exception:
            self.close()
            raise

    def verify(self):
        if private(self.root.lstat(), True)[:2] != self.root_identity:
            fail('storage_unavailable', 'Attachment storage changed.', 503)
        if private(os.stat('writer.lock', dir_fd=self.root_fd, follow_symlinks=False))[:2] != self.lock_identity:
            fail('storage_unavailable', 'Attachment storage lock changed.', 503)
        if private(os.stat('objects', dir_fd=self.root_fd, follow_symlinks=False), True)[:2] != self.objects_identity:
            fail('storage_unavailable', 'Attachment storage changed.', 503)

    def database(self):
        self.verify()
        fd = os.open('ledger.sqlite3', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.root_fd)
        try:
            self.db_identity = private(os.fstat(fd))[:2]
        finally:
            os.close(fd)
        self.verify_database()  # Reject unsafe sidecars before SQLite can open them.
        return self.root / 'ledger.sqlite3'

    def verify_database(self):
        self.verify()
        if private(os.stat('ledger.sqlite3', dir_fd=self.root_fd, follow_symlinks=False))[:2] != self.db_identity:
            fail('storage_unavailable', 'Attachment storage changed.', 503)
        for name in ('ledger.sqlite3-journal', 'ledger.sqlite3-wal', 'ledger.sqlite3-shm'):
            try:
                private(os.stat(name, dir_fd=self.root_fd, follow_symlinks=False))
            except FileNotFoundError:
                pass

    def create(self, file_id):
        self.verify()
        ident(file_id)
        fd = os.open(file_id, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.objects_fd)
        try:
            private(os.fstat(fd))
            os.fsync(self.objects_fd)
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
        if private(os.fstat(self.lock_fd))[:2] != self.lock_identity:
            fail('storage_unavailable', 'Attachment storage lock changed.', 503)
        # dup keeps the held flock's open file description. It is non-inheritable
        # across exec. Neither this pin nor PrivateFiles explicitly unlocks it.
        pin.lock_fd = os.dup(self.lock_fd)
        if (private(os.fstat(pin.lock_fd))[:2] != self.lock_identity
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
                private(os.stat(source_id, dir_fd=self.objects_fd, follow_symlinks=False))
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
            fd = os.open(file_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.objects_fd)
            self._opened(token)
            before = private(os.fstat(fd))
            if before != expected or before[2] > maximum:
                fail('source_changed', 'The saved attachment could not be verified.')
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if (len(data) > maximum or private(os.fstat(fd)) != before
                    or hashlib.sha256(data).hexdigest() != expected_digest):
                fail('source_changed', 'The saved attachment could not be verified.')
            return bytes(data)
        finally:
            self._finish_io(token, fd)

    def remove(self, file_id, expected, expected_digest=None, *, durable_absence=False):
        ident(file_id)
        token, fd = self._begin_io(file_id, 'remove'), None
        if token is None:
            return 'busy'
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
                current = private(os.fstat(fd))
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
                if h.hexdigest() != expected_digest or private(os.fstat(fd)) != current:
                    return 'changed'
            # Private directory serializes WATCH operations; same-UID hostile namespace
            # mutation is outside this storage profile, not an isolation guarantee.
            if private(os.stat(file_id, dir_fd=self.objects_fd, follow_symlinks=False)) != current:
                return 'changed'
            os.unlink(file_id, dir_fd=self.objects_fd)
            os.fsync(self.objects_fd)
            return 'removed'
        finally:
            self._finish_io(token, fd)

    def close(self):
        self.begin_close()
        for name in ('objects_fd', 'lock_fd', 'root_fd'):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
