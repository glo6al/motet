"""Isolated chooser watchdog. No UI or filesystem work on import.

The helper is invoked with the current Python in isolated mode; all arguments
are data. Only this module's constant AppleScript may reach osascript.
"""
import base64
import contextlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}
LIMIT = 16384
APPLE_SCRIPT = '''use framework "Foundation"
use scripting additions
on run argv
    try
        set leaseFD to (item 2 of argv) as integer
        set lease to current application's NSFileHandle's alloc()'s initWithFileDescriptor:leaseFD closeOnDealloc:false
        set leaseOffset to lease's offsetInFile()
        activate
        with timeout of 120 seconds
            if (item 1 of argv) is "" then
                set chosen to choose folder with prompt "Choose a project folder for Motet. Add only folders you trust."
            else
                set chosen to choose folder with prompt "Choose a project folder for Motet. Add only folders you trust." default location (POSIX file (item 1 of argv))
            end if
        end timeout
        set value to current application's NSString's stringWithString:(POSIX path of chosen)
        set dataBytes to value's dataUsingEncoding:(current application's NSUTF8StringEncoding)
        return "selected:" & ((dataBytes's base64EncodedStringWithOptions:0) as text)
    on error number errorNumber
        if errorNumber is -128 then return "cancelled"
        return "failed"
    end try
end run
'''

# The only potentially blocking hinted-parent filesystem call runs in a child.
RESOLVER = '''import json,os,stat,sys
p=os.path.dirname(json.loads(sys.argv[1]))
try:
 ok=stat.S_ISDIR(os.stat(p).st_mode)
except OSError:
 ok=False
print(json.dumps(p if ok else "",ensure_ascii=True))
'''


def decode_choice(raw):
    text = raw.decode('ascii', 'strict').strip()
    if text in ('cancelled', 'failed'):
        return {'state': text}
    if not text.startswith('selected:'):
        raise ValueError()
    path = base64.b64decode(text[9:], validate=True).decode('utf-8', 'strict')
    if (not os.path.isabs(path) or len(path) > 4096
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in path)):
        raise ValueError()
    return {'state': 'selected', 'path': path}


def lease_valid(fd, expected):
    import fcntl
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or [info.st_dev, info.st_ino] != expected):
        return False
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return True


def stop_child(child):
    if child.poll() is None:
        with contextlib.suppress(OSError):
            child.kill()
    try:
        child.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return child.poll() is not None


def collect(child, stop, timeout, *, maximum=LIMIT):
    end, raw = time.monotonic() + timeout, bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while not stop.is_set() and time.monotonic() < end:
            if not selector.select(.1):
                continue
            chunk = os.read(child.stdout.fileno(), min(4096, maximum + 1 - len(raw)))
            if not chunk:
                child.wait(timeout=2)
                if child.returncode != 0:
                    raise ValueError()
                return bytes(raw)
            raw.extend(chunk)
            if len(raw) > maximum:
                raise ValueError()
    raise TimeoutError()


def helper(data, *, popen=subprocess.Popen, parent=os.getppid, emit=None, lifetime=120):
    """Own children until reaped; an independent watchdog never calls stat."""
    emit = emit or (lambda value: print(json.dumps(value, ensure_ascii=True), flush=True))
    fd, expected_parent = data['fd'], data['parent']
    if parent() != expected_parent or not lease_valid(fd, data['lease']):
        emit({'state': 'cancelled'})
        return
    stopped, finished = threading.Event(), threading.Event()
    why = ['failed']
    deadline = time.monotonic() + lifetime
    def watch():
        while not finished.wait(.1):
            if parent() != expected_parent:
                why[0] = 'cancelled'
                stopped.set()
                return
            if time.monotonic() >= deadline:
                stopped.set()
                return
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    previous = None
    if threading.current_thread() is threading.main_thread():
        def cancelled(*_):
            why[0] = 'cancelled'
            stopped.set()
        previous = signal.signal(signal.SIGTERM, cancelled)
    children = []
    try:
        initial = ''
        if data.get('initial'):
            resolver = popen([sys.executable, '-I', '-c', RESOLVER, json.dumps(data['initial'])],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=ENV, shell=False, close_fds=True, pass_fds=(fd,))
            children.append(resolver)
            try:
                value = json.loads(collect(resolver, stopped, 2))
                if type(value) is str and (not value or os.path.isabs(value)):
                    initial = value
            except (ValueError, OSError, TimeoutError, subprocess.SubprocessError):
                if not stop_child(resolver):
                    raise RuntimeError()
            finally:
                resolver.stdout.close()
        if stopped.is_set() or parent() != expected_parent or not lease_valid(fd, data['lease']):
            emit({'state': why[0]})
            return
        native = popen(['/usr/bin/osascript', '-', initial, str(fd)], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV, shell=False,
                       close_fds=True, pass_fds=(fd,))
        children.append(native)
        native.stdin.write(APPLE_SCRIPT.encode('utf-8'))
        native.stdin.close()
        emit({'state': 'started'})
        try:
            value = decode_choice(collect(native, stopped, max(.01, deadline - time.monotonic())))
        except (ValueError, OSError, TimeoutError, subprocess.SubprocessError):
            value = {'state': why[0]}
        if not stop_child(native):
            raise RuntimeError()
        native.stdout.close()
        emit(value if not stopped.is_set() else {'state': why[0]})
    except Exception:
        emit({'state': 'failed'})
    finally:
        # Never exit voluntarily while an owned child may still hold the lease.
        for child in children:
            while not stop_child(child):
                time.sleep(.1)
        finished.set()
        watcher.join(timeout=.2)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        os.close(fd)  # close only: no LOCK_UN on the inherited description


if __name__ == '__main__':
    try:
        helper(json.loads(sys.argv[1]))
    except Exception:
        print('{"state":"failed"}', flush=True)
