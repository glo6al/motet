"""Owner-only, injected Mac folder chooser. Import/construction perform no I/O.

Selection is not registration. Only accept() may invoke the registration engine.
Native paths remain inside this coordinator until a validated saved receipt exists.
"""
import contextlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import selectors
import stat
import subprocess
import sys
import threading
import time
import uuid

from new_chat_inventory import canonical_uuid
from new_chat_types import NewChatError

ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
MAX_OUTPUT = 16384
# Root registry metadata can exceed 64 KiB on an ordinary unlocked Mac.
# Keep this exceptional bound separate from subprocess/chooser output limits.
IOREG_OUTPUT_MAX = 1024 * 1024
# OS-reported NSRunningApplication bundle IDs, never User-Agent/header claims.
# Matching socket ancestry remains mandatory even for these browser identities.
BROWSER_BUNDLE_IDS = frozenset({
    'com.google.Chrome', 'com.google.Chrome.beta', 'com.google.Chrome.dev', 'com.google.Chrome.canary',
    'com.microsoft.edgemac', 'com.brave.Browser', 'org.mozilla.firefox',
    'org.mozilla.firefoxdeveloperedition', 'org.mozilla.nightly', 'org.chromium.Chromium',
    'com.apple.Safari', 'com.apple.SafariTechnologyPreview',
})

# No Apple Events, tab reading, Accessibility automation, or UI in this probe.
PROOF_SCRIPT = r'''ObjC.import('AppKit'); ObjC.import('CoreGraphics');
function run() {
  const app = $.NSWorkspace.sharedWorkspace.frontmostApplication;
  if (!app) return JSON.stringify({});
  return JSON.stringify({pid:Number(app.processIdentifier), bundle:ObjC.unwrap(app.bundleIdentifier),
    idle:Number($.CGEventSourceSecondsSinceLastEventType($.kCGEventSourceStateHIDSystemState, $.kCGAnyInputEventType))});
}'''

# The expected *server* parent is supplied before spawn, never inferred at child
# startup. A server dying before this script starts must not produce a panel.
CHOOSER_SCRIPT = r'''ObjC.import('AppKit'); ObjC.import('stdlib');
ObjC.import('unistd'); ObjC.import('sys/file'); ObjC.import('sys/fcntl');
function run(argv) {
  const v = JSON.parse(argv[0]);
  if ($.getppid() !== v.parent || $.fcntl(v.fd, 1) < 0 || $.flock(v.fd, 6) !== 0)
    return JSON.stringify({state:'cancelled'});
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1);
  const panel = $.NSOpenPanel.openPanel;
  panel.canChooseDirectories = true; panel.canChooseFiles = false;
  panel.allowsMultipleSelection = false; panel.canCreateDirectories = false;
  panel.message = 'Choose a project folder for Motet. Add only folders you trust.';
  if (v.initial) panel.directoryURL = $.NSURL.fileURLWithPath(v.initial);
  let done = false, result = {state:'cancelled'};
  if ($.getppid() !== v.parent) return JSON.stringify(result);
  panel.beginWithCompletionHandler(function(code) {
    if (Number(code) === 1 && $.getppid() === v.parent)
      result = {state:'selected',path:ObjC.unwrap(panel.URL.path)};
    done = true;
  });
  app.activateIgnoringOtherApps(true);
  const end = Date.now() + 120000;
  while (!done) {
    if ($.getppid() !== v.parent) {
      panel.cancel(null); return JSON.stringify({state:'cancelled'});
    }
    if (Date.now() >= end) {
      panel.cancel(null); return JSON.stringify({state:'failed'});
    }
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
  }
  return JSON.stringify(result);
}'''


def _failure(code='invalid_request', status=400):
    raise NewChatError(code, 'The folder window request could not be completed.', status)


class NativeStartUnknown(Exception):
    def __init__(self, operation):
        self.operation = operation


class LocalityChanged(Exception):
    pass


def _path(value):
    return (type(value) is str and os.path.isabs(value) and len(value) <= 4096
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _stop(process):
    """Only a process object created by this adapter may be killed/reaped."""
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _read(process, timeout, maximum=MAX_OUTPUT, clock=time.monotonic):
    """Bound allocations while reading, rather than checking communicate() later."""
    end, raw = clock() + timeout, bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = end - clock()
            if remaining <= 0:
                raise TimeoutError()
            events = selector.select(min(.1, remaining))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), min(4096, maximum + 1 - len(raw)))
            if not chunk:
                process.wait(timeout=max(.01, end - clock()))
                if process.returncode != 0:
                    raise ValueError()
                return bytes(raw)
            raw.extend(chunk)
            if len(raw) > maximum:
                raise ValueError()


def _run(argv, script=None, maximum=MAX_OUTPUT):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if script else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               shell=False, env=ENV, close_fds=True)
    try:
        if script:
            process.stdin.write(script.encode('utf-8'))
            process.stdin.close()
        return _read(process, 2, maximum)
    finally:
        _stop(process)
        if process.stdout:
            process.stdout.close()


class LocalityProof:
    """Fail-closed foreground-browser socket evidence, not human attestation."""
    def __init__(self, runner=_run, uid=None, platform=None):
        self.run, self.uid = runner, uid if uid is not None else getattr(os, 'getuid', lambda: -1)
        self.platform = platform

    def _process(self, pid):
        if type(pid) is not int or not 1 <= pid <= 2147483647:
            raise ValueError()
        value = self.run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'pid=,ppid=,uid=,lstart=']).decode('utf-8').split()
        if len(value) != 8 or not all(x.isdigit() for x in value[:3]):
            raise ValueError()
        ident, parent, uid = map(int, value[:3])
        if ident != pid or uid != self.uid():
            raise ValueError()
        return pid, parent, uid, ' '.join(value[3:])

    def capture(self, connection):
        try:
            if (self.platform or sys.platform) != 'darwin':
                return None
            local, peer = connection.getsockname(), connection.getpeername()
            if (len(local) != 2 or len(peer) != 2 or local[0] != '127.0.0.1'
                    or peer[0] != '127.0.0.1' or local == peer):
                return None
            endpoint = '%s:%d->%s:%d' % (peer[0], peer[1], local[0], local[1])
            raw = self.run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:%d' % local[1],
                            '-sTCP:ESTABLISHED', '-Fpuftn']).decode('utf-8')
            # Parse PID/UID and per-FD endpoints together; never associate another
            # descriptor's name with an earlier process record.
            candidates, pid, uid, descriptor = set(), None, None, False
            for line in raw.splitlines():
                if line.startswith('p'):
                    pid, uid, descriptor = int(line[1:]), None, False
                elif line.startswith('u'):
                    uid = int(line[1:])
                elif line.startswith('f'):
                    descriptor = True
                elif line.startswith('n') and line[1:] == endpoint:
                    if not descriptor or pid is None or uid != self.uid():
                        raise ValueError()
                    candidates.add(pid)
            if len(candidates) != 1:
                return None
            gui = json.loads(self.run(['/usr/bin/osascript', '-l', 'JavaScript', '-'], PROOF_SCRIPT).decode('utf-8'))
            if (type(gui) is not dict or type(gui.get('pid')) is not int
                    or type(gui.get('bundle')) is not str or gui['bundle'] not in BROWSER_BUNDLE_IDS
                    or type(gui.get('idle')) not in (int, float) or not math.isfinite(gui['idle'])
                    or not 0 <= gui['idle'] <= 15):
                return None
            registry = plistlib.loads(self.run(['/usr/sbin/ioreg', '-a', '-d', '1', '-n', 'Root'], maximum=IOREG_OUTPUT_MAX))
            if type(registry) is list and len(registry) == 1:
                registry = registry[0]
            if type(registry) is not dict or registry.get('IOConsoleLocked') is not False:
                return None
            users = registry.get('IOConsoleUsers')
            if (type(users) is not list or sum(type(user) is dict
                    and type(user.get('kCGSSessionUserIDKey')) is int
                    and user['kCGSSessionUserIDKey'] == self.uid()
                    and user.get('kCGSSessionOnConsoleKey') is True
                    and user.get('kCGSessionLoginDoneKey') is True for user in users) != 1):
                return None
            chain, current, visited = [], next(iter(candidates)), set()
            for _ in range(12):
                if current in visited:
                    return None
                visited.add(current)
                row = self._process(current)
                chain.append(row)
                if current == gui['pid']:
                    return local, peer, gui['bundle'], tuple(chain)
                current = row[1]
            return None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, subprocess.SubprocessError):
            return None

    def verify(self, connection, evidence):
        return evidence is not None and self.capture(connection) == evidence


class NativeOperation:
    def __init__(self, process, fd, reader=_read):
        self.process, self.fd, self.reader = process, fd, reader
        self.lock = threading.Lock()

    def _release(self):
        with self.lock:
            if self.process.poll() is not None and self.fd is not None:
                # flock is inherited through the same open file description.
                # LOCK_UN here would also unlock a still-live child's lease.
                os.close(self.fd)
                self.fd = None

    def exited(self):
        return self.process.poll() is not None

    def cancel(self):
        stopped = _stop(self.process)
        self._release()
        return stopped

    def result(self):
        try:
            value = json.loads(self.reader(self.process, 120).decode('utf-8'))
            if type(value) is not dict:
                raise ValueError()
            if value in ({'state': 'cancelled'}, {'state': 'failed'}):
                return value
            if set(value) != {'state', 'path'} or value['state'] != 'selected' or not _path(value['path']):
                raise ValueError()
            return value
        finally:
            self.cancel()
            if self.exited() and self.process.stdout:
                self.process.stdout.close()


class MacChooser:
    def __init__(self, root, popen=subprocess.Popen, reader=_read):
        self.root, self.popen, self.reader = Path(root), popen, reader

    def open(self, initial_path=None, before_spawn=None):
        import fcntl
        initial = ''
        if initial_path:
            parent = os.path.dirname(initial_path)
            try:
                if stat.S_ISDIR(os.stat(parent).st_mode):
                    initial = parent
            except OSError:
                pass
        fd = os.open(self.root / 'folder-chooser.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        process = None
        try:
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                    or meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) != 0o600):
                raise ValueError()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if before_spawn:
                before_spawn()
            payload = json.dumps({'parent': os.getpid(), 'fd': fd, 'initial': initial}, ensure_ascii=True)
            process = self.popen(['/usr/bin/osascript', '-l', 'JavaScript', '-', payload],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 shell=False, env=ENV, close_fds=True, pass_fds=(fd,), start_new_session=True)
            process.stdin.write(CHOOSER_SCRIPT.encode('utf-8'))
            process.stdin.close()
            return NativeOperation(process, fd, self.reader)
        except Exception:
            if process is None or _stop(process):
                os.close(fd)
            else:
                # Retain the owned process/descriptor for shutdown retry; never
                # forget an uncertain child and start a second native window.
                raise NativeStartUnknown(NativeOperation(process, fd, self.reader)) from None
            raise


class FolderChooser:
    def __init__(self, adapter, proof=None, *, clock=time.monotonic, maximum=32, retention=600):
        self.adapter, self.proof = adapter, proof if proof is not None else LocalityProof()
        self.clock, self.maximum, self.retention = clock, maximum, retention
        self.lock = threading.RLock()
        self.operations, self.active, self.closed = {}, None, False

    def _view(self, row):
        out = {'schemaVersion': 1, 'requestId': row['id'], 'state': row['state']}
        if row['state'] == 'selected':
            out['selectionToken'] = row['token']
        if row['state'] == 'saved':
            out['folder'] = row['receipt']['folder']
        if row['state'] == 'refused' and row.get('reasonCode') == 'capacity':
            out['reasonCode'] = 'capacity'
        return out

    def _reserve(self, ident, state):
        now = self.clock()
        for key, row in list(self.operations.items()):
            if (key != self.active and row['state'] not in ('pending', 'selected', 'unknown')
                    and now - row['at'] >= self.retention):
                del self.operations[key]
        if len(self.operations) >= self.maximum:
            _failure('chooser_capacity', 429)
        row = {'id': ident, 'state': state, 'at': now, 'native': None}
        self.operations[ident] = row
        return row

    def _get(self, ident):
        if not canonical_uuid(ident):
            _failure()
        row = self.operations.get(ident)
        if row is None:
            _failure('unknown_choice', 404)
        return row

    def _release(self, row):
        native = row['native']
        if self.active == row['id'] and (native is None or native.exited()):
            self.active = None

    def begin(self, data, connection, guard):
        if (type(data) is not dict or not {'requestId', 'gesture'} <= data.keys()
                or data.keys() - {'requestId', 'gesture', 'initialPath'}
                or not canonical_uuid(data['requestId'])
                or type(data['gesture']) is not dict or set(data['gesture']) != {'visible', 'focused', 'trusted'}
                or any(type(v) is not bool for v in data['gesture'].values())
                or 'initialPath' in data and not _path(data['initialPath'])):
            _failure()
        with self.lock:
            guard()
            if self.closed:
                _failure('chooser_closed', 503)
            if data['requestId'] in self.operations:
                return self._view(self.operations[data['requestId']])
            row = self._reserve(data['requestId'], 'pending')
            if not all(data['gesture'].values()):
                row['state'] = 'not_local'
                return self._view(row)
            if self.active is not None:
                row['state'] = 'busy'
                return self._view(row)
            self.active = row['id']
        # Reserve before slow proof. cancel can tombstone this row meanwhile.
        try:
            evidence = self.proof.capture(connection)
            with self.lock:
                guard()
                if self.closed or row['state'] != 'pending':
                    self._release(row)
                    return self._view(row)
                if not self.proof.verify(connection, evidence):
                    row['state'] = 'not_local'
                    self._release(row)
                    return self._view(row)
                def before_spawn():
                    guard()
                    if self.closed or row['state'] != 'pending':
                        raise ValueError()
                    age = self.clock() - row['at']
                    if not 0 <= age <= 15 or not self.proof.verify(connection, evidence):
                        raise LocalityChanged()
                    # Verification itself may be slow. Its matching result
                    # cannot extend the initiating click's presentation window.
                    if not 0 <= self.clock() - row['at'] <= 15:
                        raise LocalityChanged()
                try:
                    row['native'] = self.adapter.open(data.get('initialPath'), before_spawn=before_spawn)
                except NativeStartUnknown as exc:
                    row.update(state='unknown', native=exc.operation)
                    return self._view(row)
                worker = threading.Thread(target=self._wait, args=(row,), daemon=True)
                row['worker'] = worker
                worker.start()
                return self._view(row)
        except LocalityChanged:
            with self.lock:
                if row['state'] == 'pending':
                    row['state'] = 'not_local'
                self._release(row)
                return self._view(row)
        except Exception:
            with self.lock:
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
            return self._view(self._get(ident))

    def cancel(self, ident):
        with self.lock:
            if not canonical_uuid(ident):
                _failure()
            row = self.operations.get(ident)
            if row is None:
                row = self._reserve(ident, 'cancelled')
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
            row = self._get(ident)
            if row['state'] == 'saved' and row.get('token') == data['selectionToken']:
                return self._view(row)
            if row['state'] != 'selected' or row['token'] != data['selectionToken']:
                _failure('choice_not_selected', 409)
            # Mark unknown before crossing the durable-insert boundary. Only a
            # known pre-insert NewChatError permits a definite refusal.
            row['state'] = 'unknown'
            try:
                receipt = register({'path': row['path']})
                folder = receipt.get('folder') if type(receipt) is dict else None
                if (type(receipt) is not dict or receipt.get('schemaVersion') != 1
                        or type(folder) is not dict or folder.get('registered') is not True
                        or any(type(folder.get(key)) is not str or not re.fullmatch(r'[0-9a-f]{64}', folder[key])
                               for key in ('id', 'revision'))
                        or type(folder.get('label')) is not str or not folder['label'] or len(folder['label']) > 4096
                        or not _path(folder.get('displayPath'))):
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
            for row in self.operations.values():
                if row['state'] not in ('saved', 'unknown'):
                    row['state'] = 'cancelled'
                row.pop('path', None)
                if row['native'] is not None:
                    row['native'].cancel()
                self._release(row)
