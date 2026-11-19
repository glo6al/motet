"""Owner-authenticated folder choice; selection alone never registers a folder."""
import contextlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from new_chat_inventory import canonical_uuid
from new_chat_types import NewChatError
import new_chat_folder_chooser_native as native

ENV, MAX_OUTPUT = native.ENV, native.LIMIT


def _failure(code='invalid_request', status=400):
    raise NewChatError(code, 'The folder window request could not be completed.', status)


def _path(value):
    return (type(value) is str and os.path.isabs(value) and len(value) <= 4096
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


class LocalityProof:
    """Direct loopback, including owner-authorized loopback tunnels.

    Host/Origin/key checks precede this. Forwarding-header presence comes from
    the HTTP boundary, never from the request body. No native probes are used.
    """
    def capture(self, connection, forwarded=False):
        try:
            if forwarded:
                return None
            local, peer = connection.getsockname(), connection.getpeername()
            if (len(local) != 2 or len(peer) != 2 or local[0] != '127.0.0.1'
                    or peer[0] != '127.0.0.1' or local == peer
                    or any(type(v) is not int or not 1 <= v <= 65535 for v in (local[1], peer[1]))):
                return None
            return local, peer
        except (OSError, ValueError, TypeError):
            return None

    def verify(self, connection, evidence, forwarded=False):
        return evidence is not None and self.capture(connection, forwarded) == evidence


def read_frames(process, timeout, started):
    end, pending, total, terminal = time.monotonic() + timeout, bytearray(), 0, None
    seen_start = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < end:
            if not selector.select(.1):
                continue
            chunk = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT + 1 - total))
            if not chunk:
                if pending or terminal is None:
                    raise ValueError()
                return terminal
            total += len(chunk)
            if total > MAX_OUTPUT:
                raise ValueError()
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending = bytearray(rest)
                value = json.loads(line.decode('ascii', 'strict'))
                if value == {'state': 'started'} and not seen_start and terminal is None:
                    seen_start = True
                    started()
                elif terminal is None and value in ({'state': 'cancelled'}, {'state': 'failed'}):
                    terminal = value
                elif (terminal is None and seen_start and type(value) is dict
                      and set(value) == {'state', 'path'} and value['state'] == 'selected' and _path(value['path'])):
                    terminal = value
                else:
                    raise ValueError()
    raise TimeoutError()


class NativeOperation:
    def __init__(self, process, fd, lock_path, identity, *, reader=read_frames, kill_group=None):
        self.process, self.fd, self.lock_path, self.identity = process, fd, Path(lock_path), identity
        self.reader = reader
        self.kill_group = kill_group if kill_group is not None else getattr(os, 'killpg', None)
        self.lock, self.started = threading.RLock(), threading.Event()
        self.confirmed = False

    @property
    def window_started(self):
        return self.started.is_set()

    def _lease_free(self):
        import fcntl
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        check = None
        try:
            check = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
            info = os.fstat(check)
            if ((info.st_dev, info.st_ino) != self.identity or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                return False
            fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if check is not None:
                os.close(check)

    def exited(self):
        with self.lock:
            if self.confirmed:
                return True
            try:
                if self.process.poll() is not None:
                    self.confirmed = self._lease_free()
            except OSError:
                return False
            return self.confirmed

    def cancel(self):
        with self.lock:
            # Never signal a PGID after reaping its leader: it may be reused.
            # All reaping/signalling shares this lock. Afterwards only the same
            # inode's exclusive lease proves that descendants released it.
            if self.process.returncode is None:
                with contextlib.suppress(OSError):
                    self.kill_group(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        self.kill_group(self.process.pid, signal.SIGKILL)
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        self.process.wait(timeout=2)
                except OSError:
                    return self.exited()
            return self.exited()

    def result(self):
        try:
            value = self.reader(self.process, 125, self.started.set)
            with self.lock:
                self.process.wait(timeout=2)
                if self.process.returncode != 0:
                    raise ValueError('Folder chooser helper did not exit successfully.')
            return value
        finally:
            self.cancel()
            if self.exited() and self.process.stdout:
                self.process.stdout.close()


class MacChooser:
    def __init__(self, root, popen=subprocess.Popen, reader=read_frames):
        self.root, self.popen, self.reader = Path(root), popen, reader

    def open(self, initial_path=None, before_spawn=None):
        import fcntl
        path = self.root / 'folder-chooser.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if before_spawn:
                before_spawn()
            data = {'parent': os.getpid(), 'fd': fd, 'lease': [info.st_dev, info.st_ino], 'initial': initial_path or ''}
            process = self.popen([sys.executable, '-I', str(Path(native.__file__).resolve()), json.dumps(data, ensure_ascii=True)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 shell=False, env=ENV, close_fds=True, pass_fds=(fd,), start_new_session=True)
            return NativeOperation(process, fd, path, (info.st_dev, info.st_ino), reader=self.reader)
        except Exception:
            os.close(fd)
            raise


class FolderChooser:
    def __init__(self, adapter, proof=None, *, clock=time.monotonic, maximum=128,
                 known_limit=65536, retention=150, schedule=True):
        self.adapter, self.proof = adapter, proof if proof is not None else LocalityProof()
        self.clock, self.maximum, self.known_limit, self.retention = clock, maximum, known_limit, retention
        self.schedule, self.epoch = schedule, str(uuid.uuid4())
        self.lock = threading.RLock()
        self.operations, self.retired, self.active, self.closed = {}, set(), None, False

    def _view(self, row):
        out = {'schemaVersion': 1, 'requestId': row['id'], 'state': row['state']}
        if row['state'] == 'pending':
            out['windowStarted'] = row['native'] is not None and row['native'].window_started
        if row['state'] == 'selected':
            out['selectionToken'] = row['token']
        if row['state'] == 'saved':
            out['folder'] = row['receipt']['folder']
        if row['state'] == 'refused' and row.get('reasonCode') == 'capacity':
            out['reasonCode'] = 'capacity'
        return out

    def _epoch(self, value):
        if not canonical_uuid(value) or value != self.epoch:
            _failure('choice_epoch_changed', 409)

    def _known_capacity(self):
        if len(self.operations) + len(self.retired) >= self.known_limit:
            _failure('chooser_capacity', 429)

    def _release(self, row):
        if self.active is row and (row['native'] is None or row['native'].exited()):
            self.active = None

    def _retire(self, row):
        self.operations.pop(row['id'], None)
        self.retired.add(row['id'])
        timer = row.pop('timer', None)
        if timer is not None:
            timer.cancel()
        for field in ('path', 'token', 'receipt'):
            row.pop(field, None)
        row['state'] = 'expired'
        if row['native'] is not None:
            row['native'].cancel()
        self._release(row)

    def expire(self):
        with self.lock:
            now = self.clock()
            for row in list(self.operations.values()):
                if now - row['at'] >= self.retention:
                    self._retire(row)
            if self.active is not None:
                self._release(self.active)

    def _reserve(self, ident):
        self._known_capacity()
        if len(self.operations) >= self.maximum:
            _failure('chooser_capacity', 429)
        row = {'id': ident, 'state': 'pending', 'at': self.clock(), 'native': None}
        self.operations[ident] = row
        if self.schedule:
            timer = threading.Timer(self.retention, self.expire)
            timer.daemon = True
            row['timer'] = timer
            timer.start()
        return row

    def _get(self, ident):
        if not canonical_uuid(ident):
            _failure()
        if ident in self.retired:
            _failure('choice_expired', 410)
        row = self.operations.get(ident)
        if row is None:
            _failure('unknown_choice', 404)
        return row

    def begin(self, data, connection, guard, forwarded=False):
        if (type(data) is not dict or not {'requestId', 'epoch', 'gesture'} <= data.keys()
                or data.keys() - {'requestId', 'epoch', 'gesture', 'initialPath'}
                or not canonical_uuid(data['requestId']) or type(data['gesture']) is not dict
                or set(data['gesture']) != {'trusted'} or type(data['gesture']['trusted']) is not bool
                or 'initialPath' in data and not _path(data['initialPath'])):
            _failure()
        with self.lock:
            guard()
            self._epoch(data['epoch'])
            if self.closed:
                _failure('chooser_closed', 503)
            self.expire()
            if data['requestId'] in self.retired:
                _failure('choice_expired', 410)
            if data['requestId'] in self.operations:
                return self._view(self.operations[data['requestId']])
            row = self._reserve(data['requestId'])
            if not data['gesture']['trusted']:
                row['state'] = 'not_local'
                return self._view(row)
            if self.active is None:
                self.active = row
        try:
            evidence = self.proof.capture(connection, forwarded)
            with self.lock:
                guard()
                if row['state'] == 'expired':
                    _failure('choice_expired', 410)
                if self.closed or row['state'] != 'pending':
                    self._release(row)
                    return self._view(row)
                if not self.proof.verify(connection, evidence, forwarded):
                    row['state'] = 'not_local'
                    self._release(row)
                    return self._view(row)
                if self.active is not row:
                    previous = self.active
                    if previous is not None and previous['state'] == 'pending':
                        previous['state'] = 'cancelled'
                        if previous['native'] is not None:
                            previous['native'].cancel()
                        self._release(previous)
                    if self.active is not None:
                        row['state'] = 'busy'
                        return self._view(row)
                    self.active = row
                def before_spawn():
                    guard()
                    if (self.closed or row['state'] != 'pending'
                            or not self.proof.verify(connection, evidence, forwarded)):
                        raise ValueError()
                row['native'] = self.adapter.open(data.get('initialPath'), before_spawn=before_spawn)
                worker = threading.Thread(target=self._wait, args=(row,), daemon=True)
                row['worker'] = worker
                worker.start()
                return self._view(row)
        except Exception:
            with self.lock:
                if row['state'] == 'expired':
                    _failure('choice_expired', 410)
                if row['state'] == 'pending':
                    if row['native'] is not None:
                        row['native'].cancel()
                    row['state'] = 'failed' if row['native'] is None or row['native'].exited() else 'unknown'
                self._release(row)
                return self._view(row)

    def _wait(self, row):
        try:
            result = row['native'].result()
            with self.lock:
                if row['state'] == 'pending':
                    if not row['native'].exited():
                        row['state'] = 'unknown'
                    elif result['state'] == 'selected':
                        row.update(state='selected', path=result['path'], token=str(uuid.uuid4()))
                    else:
                        row['state'] = result['state']
                self._release(row)
        except Exception:
            with self.lock:
                if row['state'] == 'pending':
                    row['state'] = 'failed' if row['native'].exited() else 'unknown'
                self._release(row)

    def status(self, ident):
        with self.lock:
            self.expire()
            return self._view(self._get(ident))

    def cancel(self, ident, data):
        with self.lock:
            if type(data) is not dict or set(data) != {'epoch'} or not canonical_uuid(ident):
                _failure()
            self._epoch(data['epoch'])
            self.expire()
            if ident in self.retired:
                _failure('choice_expired', 410)
            row = self.operations.get(ident)
            if row is None:
                self._known_capacity()
                self.retired.add(ident)
                return {'schemaVersion': 1, 'requestId': ident, 'state': 'cancelled'}
            if row['state'] not in ('saved', 'unknown'):
                row['state'] = 'cancelled'
                row.pop('path', None)
                row.pop('token', None)
            if row['native'] is not None:
                row['native'].cancel()
            self._release(row)
            return self._view(row)

    def accept(self, ident, data, register, guard):
        if type(data) is not dict or set(data) != {'selectionToken'} or not canonical_uuid(data['selectionToken']):
            _failure()
        with self.lock:
            guard()
            self.expire()
            row = self._get(ident)
            if row['state'] == 'saved' and row.get('token') == data['selectionToken']:
                return self._view(row)
            if row['state'] != 'selected' or row['token'] != data['selectionToken']:
                _failure('choice_not_selected', 409)
            row['state'] = 'unknown'
            try:
                receipt = register({'path': row['path']})
                folder = receipt.get('folder') if type(receipt) is dict else None
                if (type(receipt) is not dict or receipt.get('schemaVersion') != 1 or type(folder) is not dict
                        or folder.get('registered') is not True or not _path(folder.get('displayPath'))
                        or type(folder.get('label')) is not str or not folder['label'] or len(folder['label']) > 4096
                        or any(type(folder.get(key)) is not str or not re.fullmatch(r'[0-9a-f]{64}', folder[key]) for key in ('id', 'revision'))):
                    raise ValueError()
                row.update(state='saved', receipt=receipt)
            except NewChatError as exc:
                if exc.status < 500:
                    row['state'] = 'refused'
                    if exc.code == 'capacity' and exc.status == 429:
                        row['reasonCode'] = 'capacity'
            except Exception:
                pass
            row.pop('path', None)
            return self._view(row)

    def close(self):
        with self.lock:
            self.closed = True
            for row in list(self.operations.values()):
                self._retire(row)
            if self.active is not None:
                row = self.active
                if row['native'] is not None:
                    row['native'].cancel()
                self._release(row)


def trial(choose=None, validate=None, emit=print):
    """Explicit owner-run CLI. No journal, SQL, registration or native chat."""
    try:
        if choose is None:
            with tempfile.TemporaryDirectory(prefix='motet-folder-trial-') as root:
                value = MacChooser(root).open().result()
        else:
            value = choose()
        if value['state'] != 'selected':
            emit('Cancelled.' if value['state'] == 'cancelled' else 'Motet could not open the folder window.')
            return
        if validate is None:
            import secrets
            from new_chat_inventory import KnownFolderInventory
            home = os.path.expanduser('~')
            inventory = KnownFolderInventory(lambda: [], secrets.token_bytes(32), home=home,
                                             state_root=os.path.join(home, '.live-room'))
            inventory.register(value['path'])  # validates only, no persistence
        else:
            validate(value['path'])
        emit('ok')
    except NewChatError:
        emit('Motet cannot use that folder.')
    except Exception:
        emit('Motet could not open the folder window.')


if __name__ == '__main__' and sys.argv[1:] == ['--trial']:
    trial()
