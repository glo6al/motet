"""Managed Claude's processes and pipes on Windows (step 8b.2). Importing this module starts nothing and loads no
Win32 call; the feature stays refused on Windows until step 8b.5.

POSIX keeps its own way (claude_managed.spawn_runner and claude_managed_runner.NativeTransport): the supervisor
starts in a new session so it outlives the server, Claude starts in its own session, the pipes are non-blocking
under a selector, and Shutdown sends Claude SIGTERM and waits. If the supervisor dies, Claude is left running and
its outcome becomes uncertain; it is never adopted.

Windows differs in four ways:

- The supervisor starts detached from the server's console and process group, and outside any job the server is
  in (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB), so closing the server's window does
  not end it. A job that forbids breaking away (some terminals and editors hold their programs in one) refuses the
  start: ``breakaway_refusal`` says so in plain words before anything is claimed, and a start that is refused
  anyway raises DetachRefused. Motet never starts a supervisor that would die with the console.
- Claude starts suspended and without a window, is put in a new job object that kills everything in it when the
  job's last handle closes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), and only then is its one thread resumed. Every
  program it starts (Bash-tool shells, node) is in that job. If any step fails, the suspended process is ended and
  nothing is resumed. So on Windows Claude ends with its supervisor, and leaves no orphans: the supervisor holds
  the job's only handle.
- Shutdown closes Claude's input, waits as long as POSIX does, then ends the whole job (``stop_tree``).
- Windows can select sockets only, so the pipes are read and written by threads (``ThreadedPipes``), as the
  dictation runtime does; the line limits, budgets and deadlines are the transport's, unchanged.
"""
import os
import queue
import subprocess
import threading

CHUNK = 65536
DEPTH = 16                       # chunks queued at most (1 MiB): a reader that gets ahead waits

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
ERROR_ACCESS_DENIED = 5

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_EXTENDED_LIMITS = 9          # JobObjectExtendedLimitInformation
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002

DETACH_REFUSED = ('Motet cannot start a Claude supervisor that outlives this window: the program Motet runs in '
                  'does not let it leave its job. Start Motet from an ordinary terminal or a shortcut.')


class DetachRefused(OSError):
    """The supervisor could not leave the server's job, so it was not started."""


class JobError(OSError):
    """Claude could not be started inside its own job; it was ended before it ran."""


# ---------------------------------------------------------------------------------------------------------------
# Threads in place of selectors on pipes.

class ThreadedPipes:
    """Blocking pipe I/O by threads. One reader thread reads Claude's output in chunks of at most CHUNK bytes into a
    queue of at most DEPTH chunks and marks the end of the stream (or a read error) once. Each write runs in its
    own thread and must finish within the caller's deadline; a write still blocked refuses the next one, so frames
    never interleave."""

    def __init__(self, process):
        self.process = process
        self.chunks = queue.Queue(maxsize=DEPTH)
        self.closed = threading.Event()
        self.writer = None
        self.reader = threading.Thread(target=self._read, name='managed-claude-reader', daemon=True)
        self.reader.start()

    def _put(self, item):
        while not self.closed.is_set():
            try:
                self.chunks.put(item, timeout=.1)
                return True
            except queue.Full:
                continue
        return False

    def _read(self):
        try:
            fd = self.process.stdout.fileno()
            while not self.closed.is_set():
                chunk = os.read(fd, CHUNK)
                if not chunk or not self._put(chunk):
                    break
        except (OSError, ValueError):
            pass                      # a closed or broken pipe is the end of the stream
        self._put(None)

    def get(self, timeout):
        """The next chunk; None for the end of the stream; b'' if nothing arrived within ``timeout`` seconds."""
        try:
            return self.chunks.get(timeout=timeout) if timeout > 0 else self.chunks.get_nowait()
        except queue.Empty:
            return b''

    def write(self, raw, seconds):
        """Write all of ``raw`` within ``seconds``, or raise TimeoutError; a pipe error is raised as it happened."""
        if self.writer is not None and self.writer.is_alive():
            raise TimeoutError()          # an earlier write is still blocked: never interleave frames
        done, failed = threading.Event(), []

        def write():
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(self.process.stdin.fileno(), view)
                    if written <= 0:
                        raise BrokenPipeError()
                    view = view[written:]
            except (OSError, ValueError) as error:
                failed.append(error)
            finally:
                done.set()
        self.writer = threading.Thread(target=write, name='managed-claude-writer', daemon=True)
        self.writer.start()
        if not done.wait(max(0, seconds)):
            raise TimeoutError()
        if failed:
            error = failed[0]
            raise error if isinstance(error, OSError) else OSError(str(error))

    def close(self):
        self.closed.set()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


def stop_tree(close_input, wait, kill_tree, timeout):
    """Shutdown on Windows: close Claude's input (its end of stream-json input ends it), wait up to ``timeout``
    seconds as POSIX waits after SIGTERM, then end the whole job either way, so nothing Claude started stays
    running. Returns Claude's exit code: its own if it ended in time, else the one after the job was ended.
    ``wait`` raises subprocess.TimeoutExpired if even that cannot be confirmed (the caller records uncertainty)."""
    close_input()
    try:
        code = wait(timeout)
    except subprocess.TimeoutExpired:
        kill_tree()
        return wait(5)
    kill_tree()
    return code


# ---------------------------------------------------------------------------------------------------------------
# Win32: jobs, the suspended start and Toolhelp32. Loaded on first use, Windows only.

_API = None


def _api():
    global _API
    if _API is not None:
        return _API
    import ctypes
    from ctypes import wintypes as w

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', w.LARGE_INTEGER), ('PerJobUserTimeLimit', w.LARGE_INTEGER),
                    ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ThreadID', w.DWORD),
                    ('th32OwnerProcessID', w.DWORD), ('tpBasePri', w.LONG), ('tpDeltaPri', w.LONG),
                    ('dwFlags', w.DWORD)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in (
            ('CreateJobObjectW', [w.LPVOID, w.LPCWSTR], w.HANDLE),
            ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            ('QueryInformationJobObject', [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPDWORD], w.BOOL),
            ('IsProcessInJob', [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
            ('TerminateJobObject', [w.HANDLE, w.UINT], w.BOOL),
            ('TerminateProcess', [w.HANDLE, w.UINT], w.BOOL),
            ('GetCurrentProcess', [], w.HANDLE),
            ('CloseHandle', [w.HANDLE], w.BOOL),
            ('CreateToolhelp32Snapshot', [w.DWORD, w.DWORD], w.HANDLE),
            ('Thread32First', [w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            ('Thread32Next', [w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            ('OpenThread', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ('ResumeThread', [w.HANDLE], w.DWORD)):
        function = getattr(k, name)
        function.argtypes, function.restype = args, result
    _API = (ctypes, w, k, ExtendedLimits, ThreadEntry)
    return _API


def _failed(message):
    ctypes = _api()[0]
    error = JobError(message)
    error.winerror = ctypes.get_last_error()
    return error


def breakaway_refusal():
    """None if a supervisor started now could leave the server's job (or the server is in none), else the plain
    reason (DETACH_REFUSED). Asks Windows about this process's own job; changes nothing."""
    ctypes, w, k, ExtendedLimits, _ = _api()
    inside = w.BOOL()
    if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(inside)):
        return DETACH_REFUSED
    if not inside.value:
        return None
    limits = ExtendedLimits()
    if not k.QueryInformationJobObject(None, JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits), None):
        return DETACH_REFUSED
    flags = limits.BasicLimitInformation.LimitFlags
    if flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK):
        return None
    return DETACH_REFUSED


def spawn_detached(argv):
    """Start the supervisor detached from this console, process group and job. A job that refuses the breakaway
    raises DetachRefused; nothing is started then."""
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)
    except OSError as error:
        if getattr(error, 'winerror', None) == ERROR_ACCESS_DENIED:
            raise DetachRefused(DETACH_REFUSED) from None
        raise


class Job:
    """A job object that ends everything in it when its last handle closes. The supervisor holds the only handle."""

    def __init__(self):
        ctypes, w, k, ExtendedLimits, _ = _api()
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise _failed('job object unavailable')
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                         ctypes.sizeof(limits)):
            error = _failed('job limits unavailable')
            self.close()
            raise error

    def assign(self, process_handle):
        if not _api()[2].AssignProcessToJobObject(self.handle, process_handle):
            raise _failed('job assignment refused')

    def terminate(self, code=1):
        """End every process in the job (Claude and everything it started)."""
        if self.handle and not _api()[2].TerminateJobObject(self.handle, code):
            raise _failed('job could not be ended')

    def close(self):
        """Close the job's handle: with KILL_ON_JOB_CLOSE, anything still in it ends."""
        handle, self.handle = self.handle, None
        if handle:
            _api()[2].CloseHandle(handle)


def _resume_only_thread(pid):
    """Resume the one thread of the suspended process ``pid`` (Popen does not keep its thread handle). Exactly one
    thread must belong to it, and its suspend count must have been one."""
    ctypes, w, k, _, ThreadEntry = _api()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == w.HANDLE(-1).value:
        raise _failed('thread list unavailable')
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(ThreadEntry)
        threads = []
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                threads.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(ThreadEntry)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if len(threads) != 1:
        raise JobError('the suspended process does not have exactly one thread')
    thread = k.OpenThread(THREAD_SUSPEND_RESUME, False, threads[0])
    if not thread:
        raise _failed('thread unavailable')
    try:
        previous = k.ResumeThread(thread)
        if previous == 0xFFFFFFFF or previous != 1:
            raise _failed('thread could not be resumed')
    finally:
        k.CloseHandle(thread)


def start_in_job(args, cwd):
    """Start Claude suspended and without a window, put it in a new kill-on-close job, then resume its thread.
    Returns (Popen, Job). If any step fails the suspended process is ended (it never ran) and JobError is raised."""
    process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0,
                               creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
    job = None
    try:
        job = Job()
        job.assign(int(process._handle))
        _resume_only_thread(process.pid)
        return process, job
    except BaseException:
        try:
            _api()[2].TerminateProcess(int(process._handle), 1)
            process.wait(5)
        finally:
            for stream in (process.stdin, process.stdout):
                stream.close()
            if job is not None:
                job.close()
        raise
