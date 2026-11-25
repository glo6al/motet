"""Managed Claude's processes and pipes on Windows (step 8b.2). Importing this module starts nothing and loads no
Win32 call; the feature stays refused on Windows until step 8b.5.

POSIX keeps its own way (claude_managed.spawn_runner and claude_managed_runner.NativeTransport): the supervisor
starts in a new session so it outlives the server, Claude starts in its own session, the pipes are non-blocking
under a selector, and Shutdown sends Claude SIGTERM and waits. If the supervisor dies, Claude is left running and
its outcome becomes uncertain; it is never adopted.

Windows differs in four ways:

- The supervisor starts detached from the server's console and process group, and outside any job the server is
  in (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB), suspended. Before its thread is
  resumed, Windows is asked whether it is in any job at all (``IsProcessInJob``): one that still holds it (an
  outer job in a nest that forbids breaking away) would end it with the console, so it is ended before it ran and
  the start is refused (DetachRefused, in plain words). ``breakaway_refusal`` gives the same answer earlier, before
  anything is claimed, where it can tell; the check on the started process is the one that decides.
- Claude starts suspended and without a window, is put in a new job object that kills everything in it when the
  job's last handle closes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), and only then is its one thread resumed. Every
  program it starts (Bash-tool shells, node) is in that job. If any step fails, the suspended process is ended and
  nothing is resumed. So on Windows Claude ends with its supervisor, and leaves no orphans: the supervisor holds
  the job's only handle.
- Shutdown closes Claude's input, waits as long as POSIX does, then ends the whole job (``stop_tree``). A job
  ended by Motet leaves the exit code JOB_ENDED_EXIT, never one Claude itself gives.
- Windows can select sockets only, so the pipes are read and written by threads (``ThreadedPipes``), as the
  dictation runtime does; the line limits, budgets and deadlines are the transport's, unchanged. The C runtime
  holds a descriptor's lock for a whole read, write or close, so a pipe is never closed while its thread may still
  be blocked in it: the job is ended first (its processes' ends of the pipes then close, and the blocked thread
  returns), the threads are waited for, and only then are the pipes closed.
"""
import os
import queue
import subprocess
import threading
import time

CHUNK = 65536
DEPTH = 16                       # chunks queued at most (1 MiB): a reader that gets ahead waits
FINISH_SECONDS = 2               # how long a pipe thread is waited for once the job is ended

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
ERROR_ACCESS_DENIED = 5
JOB_ENDED_EXIT = 0x4D4F5445      # "MOTE": the exit code of every process in a job Motet ended

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_EXTENDED_LIMITS = 9          # JobObjectExtendedLimitInformation
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002

DETACH_REFUSED = ('Motet cannot start a Claude supervisor that outlives this window: the program Motet runs in '
                  'does not let it leave its job. Try starting Motet from an ordinary terminal window or a shortcut.')
NOT_STARTED = 'Motet could not start the Claude supervisor. Nothing was started.'


class NotStarted(OSError):
    """The supervisor was not started, or was ended before it ran: nothing started."""


class DetachRefused(NotStarted):
    """The supervisor could not leave the server's job, so it was not started (it never ran)."""


class JobError(OSError):
    """Claude could not be started inside its own job; it was ended before it ran."""


class AlreadyRunning(JobError):
    """The process's thread was not suspended when it was to be resumed: it may already have run. Never NotStarted."""


# ---------------------------------------------------------------------------------------------------------------
# Threads in place of selectors on pipes.

class ThreadedPipes:
    """Blocking pipe I/O by threads. One reader thread reads Claude's output in chunks of at most CHUNK bytes into a
    queue of at most DEPTH chunks and marks the end of the stream (or a read error) once. Each write runs in its
    own thread and must finish within the caller's deadline; a write still blocked refuses the next, so frames
    never interleave. ``eof`` is set once the end of the stream has been read, and stays set."""

    def __init__(self, process):
        self.process = process
        self.chunks = queue.Queue(maxsize=DEPTH)
        self.closed = threading.Event()
        self.eof = False
        self.writer = None
        self.written = None           # the last write's completion: set once it has written all or failed
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

    def final(self, seconds=FINISH_SECONDS):
        """After the process exited with nothing queued: the next chunk or the end marker, waited for up to
        ``seconds`` (the reader may still be passing on bytes it read before the exit). b'' if the reader is still
        blocked then (a program outside the process still holds the pipe), which the caller takes as the end."""
        deadline = time.monotonic() + seconds
        while True:
            chunk = self.get(.05)
            if chunk != b'':
                return chunk
            if not self.reader.is_alive() and self.chunks.empty():
                return None
            if time.monotonic() >= deadline:
                return b''

    def writing(self):
        """Whether an earlier write has not completed (it is still blocked in its thread). A write that completed
        is not "writing" even while its thread is still returning."""
        return self.written is not None and not self.written.is_set()

    def write(self, raw, seconds):
        """Write all of ``raw`` within ``seconds``, or raise TimeoutError; a pipe error is raised as it happened."""
        if self.writing():
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
        self.written = done
        self.writer = threading.Thread(target=write, name='managed-claude-writer', daemon=True)
        self.writer.start()
        if not done.wait(max(0, seconds)):
            raise TimeoutError()
        self.writer.join(1)               # it has completed and is only returning
        if failed:
            error = failed[0]
            raise error if isinstance(error, OSError) else OSError(str(error))

    def close(self, seconds=FINISH_SECONDS):
        """Close the pipes without ever blocking on one a thread is still in. Call it once the process (on Windows,
        its whole job) has ended, so the threads see the end of their pipes: each is waited for up to ``seconds``,
        and a pipe whose thread is still blocked is left open rather than waited on. True if both were closed."""
        self.closed.set()
        self.reader.join(seconds)
        if self.writer is not None:
            self.writer.join(seconds)
        closed = True
        for stream, thread in ((self.process.stdout, self.reader), (self.process.stdin, self.writer)):
            if stream is None:
                continue
            if thread is not None and thread.is_alive():
                closed = False            # still blocked in it: closing would wait for that call to end
                continue
            try:
                stream.close()
            except OSError:
                pass
        return closed


def stop_tree(close_input, wait, kill_tree, timeout, input_blocked=lambda: False):
    """Shutdown on Windows: close Claude's input (its end of stream-json input ends it), wait up to ``timeout``
    seconds as POSIX waits after SIGTERM, then end the whole job either way, so nothing Claude started stays
    running. If a write is still blocked in the input pipe, closing it would wait on that write, so the job is
    ended at once instead. Returns Claude's exit code: its own if it ended in time, else JOB_ENDED_EXIT from ending
    the job. ``wait`` raises subprocess.TimeoutExpired if even that cannot be confirmed (the caller records
    uncertainty)."""
    if input_blocked():
        kill_tree()
        return wait(5)
    close_input()
    try:
        code = wait(timeout)
    except subprocess.TimeoutExpired:
        kill_tree()
        return wait(5)
    kill_tree()
    return code


# ---------------------------------------------------------------------------------------------------------------
# Win32: jobs, suspended starts and Toolhelp32. Loaded on first use, Windows only.

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


def _failed(message, code=None):
    """A JobError carrying the Win32 error just set (or ``code`` where the failure is not a Win32 error)."""
    ctypes = _api()[0]
    error = JobError(message)
    error.winerror = ctypes.get_last_error() if code is None else code
    return error


def _in_any_job(process_handle):
    """Whether the process is in any job (IsProcessInJob with no job named)."""
    ctypes, w, k = _api()[:3]
    inside = w.BOOL()
    if not k.IsProcessInJob(process_handle, None, ctypes.byref(inside)):
        raise _failed('job membership unavailable')
    return bool(inside.value)


def breakaway_refusal():
    """None if a supervisor started now looks able to leave the server's job (or the server is in none), else the
    plain reason (DETACH_REFUSED). It reads this process's immediate job only; an outer job in a nest can still
    forbid it, which the check on the started supervisor (``spawn_detached``) catches. Changes nothing."""
    ctypes, w, k, ExtendedLimits, _ = _api()
    try:
        if not _in_any_job(k.GetCurrentProcess()):
            return None
    except JobError:
        return DETACH_REFUSED
    limits = ExtendedLimits()
    if not k.QueryInformationJobObject(None, JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits), None):
        return DETACH_REFUSED
    flags = limits.BasicLimitInformation.LimitFlags
    if flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK):
        return None
    return DETACH_REFUSED


def _end_suspended(process, reason):
    """End a process that never ran (it is still suspended) and close its pipes. Returns ``reason`` with a note if
    Windows did not confirm the end; a TimeoutExpired from the wait never replaces the original error."""
    note = ''
    try:
        if not _api()[2].TerminateProcess(int(process._handle), JOB_ENDED_EXIT):
            note = '; the suspended process could not be ended (it never ran)'
        else:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                note = '; the end of the suspended process was not confirmed (it never ran)'
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return reason + note


def _server_in_a_job():
    try:
        return _in_any_job(_api()[2].GetCurrentProcess())
    except JobError:
        return True                   # unknown: take the stricter answer


def spawn_detached(argv):
    """Start the supervisor detached from this console, process group and job, and resume it only if Windows says
    it is in no job at all. Every way it does not run is NotStarted, so a caller records that nothing started:
    DetachRefused when it is still held by a job (it is ended before it ran) or when Windows refuses the start while
    this process is in a job; NotStarted when Windows refuses the start otherwise (the program itself could not be
    started) or the suspended supervisor cannot be resumed (it is ended first)."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                                  | CREATE_BREAKAWAY_FROM_JOB | CREATE_SUSPENDED))
    except OSError as error:
        # Access denied while this process is in a job (or that cannot be checked) is the job refusing the
        # breakaway; any other failure (a missing or unreadable program, say) is a plain start failure.
        if getattr(error, 'winerror', None) == ERROR_ACCESS_DENIED and _server_in_a_job():
            raise DetachRefused(DETACH_REFUSED) from error
        raise NotStarted(NOT_STARTED) from error
    try:
        if _in_any_job(int(process._handle)):
            raise DetachRefused(_end_suspended(process, DETACH_REFUSED))
        _resume_only_thread(process.pid)
    except NotStarted:
        raise
    except AlreadyRunning as error:
        # It may have run: ended anyway, and never reported as not started (the launch stays unknown).
        _api()[2].TerminateProcess(int(process._handle), JOB_ENDED_EXIT)
        raise JobError('the supervisor was already running when it was to be resumed; it was ended') from error
    except BaseException as error:
        reason = _end_suspended(process, NOT_STARTED)
        if not isinstance(error, Exception):
            raise
        raise NotStarted(reason) from error
    return process


class Job:
    """A job object that ends everything in it when its last handle closes. The supervisor holds the only handle."""

    def __init__(self):
        ctypes, w, k, ExtendedLimits, _ = _api()
        self.handle = k.CreateJobObjectW(None, None)
        self.ended = False
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

    def terminate(self):
        """End every process in the job (Claude and everything it started) with JOB_ENDED_EXIT."""
        if self.handle:
            if not _api()[2].TerminateJobObject(self.handle, JOB_ENDED_EXIT):
                raise _failed('job could not be ended')
            self.ended = True

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
        if previous == 0xFFFFFFFF:
            raise _failed('thread could not be resumed')
        if previous == 0:
            raise AlreadyRunning('the process was already running')
        if previous != 1:
            raise _failed('the thread was not suspended once (count %d)' % previous, code=0)
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
    except BaseException as error:
        try:
            reason = _end_suspended(process, str(error) or 'Claude could not be started in its job')
        finally:
            if job is not None:
                job.close()
        if isinstance(error, JobError):
            error.args = (reason,)
            raise
        if not isinstance(error, Exception):
            raise                         # an interrupt stays an interrupt; the process was still ended
        raise JobError(reason) from error


# ---------------------------------------------------------------------------------------------------------------
# The control channel (step 8b.3): a named pipe in place of POSIX's private Unix socket.
#
# The supervisor creates one pipe instance with a random name (``\\.\pipe\motet-`` and 32 random hex digits),
# FILE_FLAG_FIRST_PIPE_INSTANCE (if anything already holds that name, creating it fails: no squatting) and
# PIPE_REJECT_REMOTE_CLIENTS, under private_storage's protected this-user/SYSTEM security descriptor (explicit owner;
# Everyone and anonymous get nothing). Only after the pipe exists does it write the name and its own process ID to
# ``control.json`` in its private run folder. The web server reads that file through private_storage and opens the
# pipe with identification-only impersonation. It talks only if the pipe's server process is the recorded
# supervisor (GetNamedPipeServerProcessId) and the pipe's owner and access list are this user's private ones (read
# back from the opened handle); only then is the token sent.
#
# The name is not a secret: any local user can list \\.\pipe\*. What refuses a foreign pipe is the owner and list
# check (a pipe another account made, under any name or process ID, is not owned by this user) together with the
# process check (a pipe this same user's other process made is not served by the recorded supervisor; this same
# user could read the token from the run folder anyway, as with the Unix socket). A stale ``control.json`` names a
# pipe that ended with its supervisor, so opening it fails; if another process of this user later serves that name
# under the recorded process ID, that user could have read the token anyway.
#
# Every operation is overlapped I/O with a deadline on a raw handle no other thread uses (no C runtime descriptor,
# so none of its locks). An operation's buffer, OVERLAPPED and event are freed only once Windows has confirmed the
# operation is over: a wait that ends any way but completion cancels it and waits for that cancellation, and an
# exception (an interrupt, say) does the same on the way out. If even that cannot be confirmed, they are kept for
# the life of the process rather than freed under a pending operation. One instance serves one client at a time, as
# the Unix socket's single accept loop does.

PIPE_PREFIX = '\\\\.\\pipe\\motet-'
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_BUFFER = 65536
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
SECURITY_SQOS_PRESENT, SECURITY_IDENTIFICATION = 0x00100000, 0x00010000
ERROR_FILE_NOT_FOUND, ERROR_BROKEN_PIPE, ERROR_PIPE_BUSY = 2, 109, 231
ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED, ERROR_PIPE_CONNECTED, ERROR_IO_PENDING = 232, 233, 535, 997
ERROR_NOT_FOUND = 1168
ENDED = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)   # the other end is gone: the end of stream
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
CONTROL_SECONDS = 2.0            # every connect, read and write, as the Unix socket's settimeout(2)
_KEPT = []                       # operations whose end Windows never confirmed: never freed

_PIPE_API = None


def _pipe_api():
    global _PIPE_API
    if _PIPE_API is not None:
        return _PIPE_API
    import private_storage as ps
    ctypes, w = _api()[:2]
    SA = ps._win_api()[3]

    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t), ('Offset', w.DWORD),
                    ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    OV, D = ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD)
    for name, args, result in (
            ('CreateNamedPipeW', [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
                                  ctypes.POINTER(SA)], w.HANDLE),
            ('ConnectNamedPipe', [w.HANDLE, OV], w.BOOL),
            ('DisconnectNamedPipe', [w.HANDLE], w.BOOL),
            ('CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            ('WaitNamedPipeW', [w.LPCWSTR, w.DWORD], w.BOOL),
            ('GetNamedPipeServerProcessId', [w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
            ('CreateEventW', [w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
            ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
            ('ReadFile', [w.HANDLE, w.LPVOID, w.DWORD, D, OV], w.BOOL),
            ('WriteFile', [w.HANDLE, w.LPVOID, w.DWORD, D, OV], w.BOOL),
            ('GetOverlappedResult', [w.HANDLE, OV, D, w.BOOL], w.BOOL),
            ('CancelIoEx', [w.HANDLE, OV], w.BOOL),
            ('CloseHandle', [w.HANDLE], w.BOOL)):
        function = getattr(k, name)
        function.argtypes, function.restype = args, result
    _PIPE_API = (ctypes, w, k, Overlapped, ps)
    return _PIPE_API


def _pipe_error(message, code=None):
    ctypes = _pipe_api()[0]
    error = OSError(message)
    error.winerror = ctypes.get_last_error() if code is None else code
    return error


def _milliseconds(seconds):
    return max(0, min(0xFFFFFFFE, int(seconds * 1000)))


class _Operation:
    """One overlapped operation on ``handle``: its event, OVERLAPPED and data buffer (``buffer``), kept together
    until Windows has confirmed the operation is over. ``pending`` is set before the call that starts it, so any
    exit, an exception included, cancels and confirms it before anything is freed (``close``)."""

    def __init__(self, handle, buffer=None):
        ctypes, w, k, Overlapped, _ = _pipe_api()
        self.handle, self.buffer, self.pending, self.unconfirmed = handle, buffer, False, False
        self.event = k.CreateEventW(None, True, False, None)
        if not self.event:
            raise _pipe_error('event unavailable')
        try:
            self.overlapped = Overlapped()
            self.overlapped.hEvent = self.event
            self.count = w.DWORD()
        except BaseException:
            k.CloseHandle(self.event)          # nothing was started with it
            self.event = None
            raise

    def started(self, result, code):
        """Record what the call that should have started the operation returned (its BOOL and the error it set). A
        call that failed with anything but ERROR_IO_PENDING started nothing; returns that error, else None."""
        if not result and code != ERROR_IO_PENDING:
            self.pending = False
            return code
        return None

    def cancel(self):
        """Cancel the operation if it is still running and wait until Windows says it is over. True when its end is
        confirmed (CancelIoEx finds nothing to cancel only when nothing is running)."""
        ctypes, w, k = _pipe_api()[:3]
        if not self.pending:
            return True
        if self.unconfirmed:
            return False                        # a cancel already went unconfirmed: never wait for it again
        if k.CancelIoEx(self.handle, ctypes.byref(self.overlapped)) or ctypes.get_last_error() != ERROR_NOT_FOUND:
            # Running, or unknown: wait for its end, bounded, through its own event.
            if k.WaitForSingleObject(self.event, 5000) != WAIT_OBJECT_0:
                self.unconfirmed = True
                return False
        self.pending = False
        return True

    def finish(self, result, code, seconds):
        """Wait for the operation the call just started for up to ``seconds``: the byte count, or socket.timeout
        once it is cancelled and over; Win32 errors as OSError with ``winerror``."""
        import socket
        ctypes, w, k = _pipe_api()[:3]
        failed = self.started(result, code)
        if failed is not None:
            raise _pipe_error('pipe operation failed', failed)
        waited = k.WaitForSingleObject(self.event, _milliseconds(seconds))
        if waited != WAIT_OBJECT_0:                     # timed out, or the wait itself failed
            failure = None if waited == WAIT_TIMEOUT else _pipe_error('pipe wait failed')
            if not self.cancel():
                raise _pipe_error('pipe operation could not be ended', 0)
            if failure is not None:
                raise failure
            raise socket.timeout('pipe operation timed out')
        ok = k.GetOverlappedResult(self.handle, ctypes.byref(self.overlapped), ctypes.byref(self.count), False)
        error = None if ok else _pipe_error('pipe operation failed')
        self.pending = False
        if error is not None:
            raise error
        return self.count.value

    def close(self):
        """Free the event (and with it the OVERLAPPED and buffer) only once the operation is confirmed over; one
        Windows never confirms is kept for the life of the process instead. It is kept first and let go only after
        the confirmation, so an interrupt during the cancel cannot free a pending operation."""
        _KEPT.append(self)
        if not self.cancel():
            return
        _KEPT.remove(self)
        event, self.event = self.event, None
        if event:
            _pipe_api()[2].CloseHandle(event)


class PipeConnection:
    """The part of a socket the line protocol uses (``settimeout``, ``recv``, ``sendall``, ``with``) over a pipe
    handle with overlapped I/O; every call is bounded by the timeout set. The end of the stream (the other end
    closed or disconnected) is ``b''``, as from a socket."""

    def __init__(self, handle, on_close=None):
        self.handle, self.timeout, self.on_close = handle, CONTROL_SECONDS, on_close

    def settimeout(self, seconds):
        self.timeout = float(seconds)

    def recv(self, size):
        ctypes, w, k = _pipe_api()[:3]
        size = max(1, size)
        operation = _Operation(self.handle, ctypes.create_string_buffer(size))
        try:
            operation.pending = True
            result = k.ReadFile(self.handle, operation.buffer, size, None, ctypes.byref(operation.overlapped))
            code = ctypes.get_last_error()
            try:
                count = operation.finish(result, code, self.timeout)
            except OSError as error:
                if getattr(error, 'winerror', None) in ENDED:
                    return b''
                raise
            return operation.buffer.raw[:count]
        finally:
            operation.close()

    def sendall(self, data):
        import socket
        import time
        ctypes, w, k = _pipe_api()[:3]
        view, deadline = memoryview(bytes(data)), time.monotonic() + self.timeout
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout('pipe write timed out')
            part = bytes(view[:PIPE_BUFFER])
            operation = _Operation(self.handle, ctypes.create_string_buffer(part, len(part)))
            try:
                operation.pending = True
                result = k.WriteFile(self.handle, operation.buffer, len(part), None,
                                     ctypes.byref(operation.overlapped))
                written = operation.finish(result, ctypes.get_last_error(), left)
            finally:
                operation.close()
            if written <= 0:
                raise _pipe_error('pipe write failed', 0)
            view = view[written:]

    def close(self):
        on_close, self.on_close = self.on_close, None
        if on_close is not None:
            on_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class PipeServer:
    """The supervisor's end: one instance of a new pipe, serving one client at a time (``accept``). One instance
    is enough: a reading client closes as soon as it has its reply, so the next is served at once, and a client
    that arrives meanwhile waits for the instance within its own deadline, as a Unix socket's queued client waits
    within its receive timeout."""

    def __init__(self, name=None):
        import secrets
        ctypes, w, k, _, ps = _pipe_api()
        self.name = name or PIPE_PREFIX + secrets.token_hex(16)
        self.handle, self.connect = None, None
        with ps._CurrentUser() as user, ps._PrivateAttributes(user) as sa:
            handle = k.CreateNamedPipeW(
                self.name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                PIPE_REJECT_REMOTE_CLIENTS, 1, PIPE_BUFFER, PIPE_BUFFER, 0, ctypes.byref(sa))
        if not handle or handle == w.HANDLE(-1).value:
            raise _pipe_error('pipe unavailable')       # ERROR_ACCESS_DENIED: the name is held already
        self.handle = handle

    def accept(self, seconds):
        """A connected client as a PipeConnection, or None if none connected within ``seconds`` (the wait for a
        client stays pending for the next call)."""
        ctypes, w, k = _pipe_api()[:3]
        if self.connect is None:
            operation = _Operation(self.handle)
            try:
                operation.pending = True
                result = k.ConnectNamedPipe(self.handle, ctypes.byref(operation.overlapped))
                code = ctypes.get_last_error()
                if result or code in (ERROR_PIPE_CONNECTED, ERROR_NO_DATA):
                    operation.pending = False                   # nothing outstanding: connected already
                    operation.close()
                    if code == ERROR_NO_DATA and not result:    # a client connected and left already
                        k.DisconnectNamedPipe(self.handle)
                        return None
                    return PipeConnection(self.handle, self._done)
                failed = operation.started(result, code)
                if failed is not None:
                    operation.close()
                    raise _pipe_error('pipe connect failed', failed)
                self.connect = operation
            except BaseException:
                if self.connect is not operation:
                    operation.close()                           # cancels and confirms anything it started
                raise
        waited = k.WaitForSingleObject(self.connect.event, _milliseconds(seconds))
        if waited == WAIT_TIMEOUT:
            return None
        operation, self.connect = self.connect, None
        try:
            if waited != WAIT_OBJECT_0:
                raise _pipe_error('pipe wait failed')
            ok = k.GetOverlappedResult(self.handle, ctypes.byref(operation.overlapped), ctypes.byref(operation.count),
                                       False)
            operation.pending = False
            if not ok:
                k.DisconnectNamedPipe(self.handle)
                return None
        finally:
            operation.close()
        return PipeConnection(self.handle, self._done)

    def _done(self):
        """After a reply: give the client up to two seconds to read it and close its end, then disconnect so the
        instance can serve the next client. A disconnect discards anything unread (a socket close does not); the
        wait is how the reply still reaches a client that is reading it. A reading client closes at once."""
        import socket
        import time
        connection = PipeConnection(self.handle)
        deadline = time.monotonic() + CONTROL_SECONDS
        try:
            while time.monotonic() < deadline:
                connection.settimeout(max(.001, deadline - time.monotonic()))
                if not connection.recv(1):
                    break
        except (OSError, socket.timeout):
            pass
        _pipe_api()[2].DisconnectNamedPipe(self.handle)

    def close(self):
        ctypes, w, k = _pipe_api()[:3]
        if self.connect is not None:
            self.connect.close()                    # cancels the pending wait for a client and confirms it
            self.connect = None
        handle, self.handle = self.handle, None
        if handle:
            k.CloseHandle(handle)


def open_pipe(name, server_pid, seconds=CONTROL_SECONDS):
    """The web server's end: open the pipe ``name`` within ``seconds`` (waiting for a busy instance within that
    time) and return a PipeConnection (each later read or write bounded by CONTROL_SECONDS), only if its server
    process is ``server_pid`` and the pipe's owner and access list are this user's private ones. Only a local pipe
    name of the supervisor's form is accepted, and the server may only identify, never impersonate, the caller."""
    import re
    import socket
    import time
    if not isinstance(name, str) or not re.fullmatch(re.escape(PIPE_PREFIX) + '[0-9a-f]{32}', name):
        raise _pipe_error('not a supervisor pipe name', 0)
    if type(server_pid) is not int or not 0 < server_pid <= 0xFFFFFFFF:
        raise _pipe_error('no supervisor process recorded', 0)
    ctypes, w, k, _, ps = _pipe_api()
    deadline = time.monotonic() + seconds
    while True:
        handle = k.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING,
                               FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
        if handle and handle != w.HANDLE(-1).value:
            break
        code = ctypes.get_last_error()
        left = deadline - time.monotonic()
        if code == ERROR_PIPE_BUSY and left > 0:
            k.WaitNamedPipeW(name, max(1, _milliseconds(left)))
            continue
        if code == ERROR_PIPE_BUSY:
            raise socket.timeout('the supervisor is busy')
        if code == ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(2, 'no supervisor pipe')
        raise _pipe_error('supervisor pipe unavailable', code)
    try:
        pid = w.ULONG()
        if not k.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise _pipe_error('supervisor process unavailable')
        if pid.value != server_pid:
            raise _pipe_error('the pipe is not the recorded supervisor\'s', 0)
        ps.windows_handle_security(handle)         # owned by this user; only this user and SYSTEM on its list
    except BaseException:
        k.CloseHandle(handle)
        raise
    return PipeConnection(handle, lambda: k.CloseHandle(handle))


# ---------------------------------------------------------------------------------------------------------------
# Identity and the executable (step 8b.4).
#
# Machine and user: the readiness record names this machine by its SMBIOS system UUID (structure type 1, read with
# GetSystemFirmwareTable('RSMB'), the hardware UUID macOS's ioreg reports there) and this user by the SID of the
# process token, in place of the Mac's IOPlatformUUID and uid. An all-zero or all-FF UUID (not set, or not present)
# is refused, as the Mac refuses zero; MachineGuid (copied with disk images) is never used.
#
# The executable: Claude Code 2.1.323's own Windows build for this machine's real architecture (an ARM64 PC
# running x64 Python still gets the ARM64 build), at %USERPROFILE%\.local\share\claude\versions\2.1.323, with or
# without ".exe". That location is what the native installer is expected to write; it has not been checked on a
# real PC yet (docs/WINDOWS.md's checklist). An npm claude.cmd or .bat shim, an app execution alias in a
# WindowsApps folder and the Claude desktop app's own copy are refused, as is anything reached through a link or
# junction (the no-link walk). The binary is opened with a handle that shares reading only, hashed through it, and
# the handle is kept open until Claude has been started from that same file, so it cannot be replaced in between.

CLAUDE_VERSION = '2.1.323'
# Claude Code 2.1.323's release manifest: the Windows builds' SHA-256 and size, by native machine architecture.
WINDOWS_PINS = {
    'x64': ('5064e6e0572dca86257363ca04d48653eef20ef27f1bd04b504ed7199ed3fd13', 367634080),
    'arm64': ('3071c54f466e40e668c273b53ed8942efce35bf069d66a69668f74299b0cb888', 349943968),
}
IMAGE_FILE_MACHINES = {0x8664: 'x64', 0xAA64: 'arm64'}      # IMAGE_FILE_MACHINE_AMD64, IMAGE_FILE_MACHINE_ARM64
WINDOWS_HASH_SECONDS = 60      # hashing ~367 MB while Microsoft Defender scans it (POSIX keeps 10 s)
RSMB = 0x52534D42              # 'RSMB': the raw SMBIOS firmware table provider


def parse_smbios_uuid(raw):
    """The system UUID from a raw SMBIOS table as GetSystemFirmwareTable('RSMB') returns it (the RawSMBIOSData
    header: calling method, major and minor version, DMI revision, a 32-bit length, then the table). Structure type 1
    holds the UUID at offset 8; from SMBIOS 2.6 its first three fields are little-endian. ValueError for a short or
    malformed table, a missing type 1, or an all-zero or all-FF UUID."""
    import struct
    import uuid
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8:
        raise ValueError('SMBIOS table unavailable')
    major, minor, length = raw[1], raw[2], struct.unpack_from('<I', raw, 4)[0]
    table = bytes(raw[8:8 + length])
    if len(table) != length:
        raise ValueError('SMBIOS table truncated')
    offset = 0
    while offset + 4 <= len(table):
        kind, size = table[offset], table[offset + 1]
        if size < 4 or offset + size > len(table):
            raise ValueError('SMBIOS structure truncated')
        if kind == 1:
            if size < 0x19:
                raise ValueError('SMBIOS system information has no UUID')
            value = table[offset + 8:offset + 24]
            if value in (b'\x00' * 16, b'\xff' * 16):
                raise ValueError('SMBIOS system UUID is not set')
            return uuid.UUID(bytes_le=value) if (major, minor) >= (2, 6) else uuid.UUID(bytes=value)
        end = table.find(b'\x00\x00', offset + size)     # the structure's strings end with two NULs
        if end < 0:
            raise ValueError('SMBIOS strings truncated')
        if kind == 127:                                  # end of table
            break
        offset = end + 2
    raise ValueError('SMBIOS system information missing')


def system_uuid():
    """This machine's SMBIOS system UUID (Windows: GetSystemFirmwareTable('RSMB'), no administrator rights needed)."""
    ctypes, w = _api()[:2]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetSystemFirmwareTable.argtypes = [w.DWORD, w.DWORD, w.LPVOID, w.DWORD]
    k.GetSystemFirmwareTable.restype = w.UINT
    size = k.GetSystemFirmwareTable(RSMB, 0, None, 0)
    if not size or size > 16 * 1024 * 1024:
        raise _failed('SMBIOS table unavailable')
    buffer = ctypes.create_string_buffer(size)
    if k.GetSystemFirmwareTable(RSMB, 0, buffer, size) != size:
        raise _failed('SMBIOS table unavailable')
    return parse_smbios_uuid(buffer.raw)


def user_sid():
    """The process token's user SID, as text (private_storage reads it the same way for its access lists)."""
    import private_storage as ps
    with ps._CurrentUser() as user:
        return user.text


def host_fingerprint(machine, sid):
    """The readiness record's host fingerprint on Windows. The Mac's is built from IOPlatformUUID and the uid, under
    the same version label; the "windows" part keeps the two apart."""
    import hashlib
    return hashlib.sha256(('watch-managed-claude/v1/windows/%s/%s' % (machine, sid)).encode('ascii')).hexdigest()


def native_architecture(query=None):
    """'x64' or 'arm64' for this machine's own architecture, else None. IsWow64Process2 gives the native machine
    even when this Python is emulated (x64 Python on an ARM64 PC); ``query`` stands in for it in tests and returns
    the native IMAGE_FILE_MACHINE value."""
    if query is None:
        ctypes, w, k = _api()[:3]
        query_function = getattr(k, 'IsWow64Process2', None)
        if query_function is None:
            return None                                  # before Windows 10 1709: not qualified
        query_function.argtypes = [w.HANDLE, ctypes.POINTER(w.USHORT), ctypes.POINTER(w.USHORT)]
        query_function.restype = w.BOOL

        def query():
            process, native = w.USHORT(), w.USHORT()
            if not query_function(k.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
                raise _failed('machine architecture unavailable')
            return native.value
    return IMAGE_FILE_MACHINES.get(query())


def windows_pin(architecture=None):
    """The (SHA-256, size) of Claude Code 2.1.323's build for ``architecture`` (default: this machine's), or None
    where there is no build Motet pins (32-bit Windows, or an unknown machine)."""
    return WINDOWS_PINS.get(architecture if architecture is not None else native_architecture())


def claude_candidates(home):
    """Where the native installer keeps Claude Code 2.1.323 on Windows (unverified on a real PC): under the
    resolved home, ``.local\\share\\claude\\versions\\2.1.323``, with or without ``.exe``."""
    base = os.path.join(os.path.realpath(os.fspath(home)), '.local', 'share', 'claude', 'versions')
    return [os.path.join(base, CLAUDE_VERSION), os.path.join(base, CLAUDE_VERSION + '.exe')]


def refused_location(path):
    """A plain reason Motet does not run the program at ``path``, or None: npm's claude.cmd or .bat shims need a
    shell, an app execution alias in a WindowsApps folder may start an app instead, and the Claude desktop app's
    own copy is not the pinned build."""
    import ntpath
    lowered = ntpath.normcase(os.fspath(path))
    parts = lowered.replace('/', '\\').split('\\')
    if lowered.endswith(('.cmd', '.bat')):
        return 'a .cmd or .bat file needs a shell'
    if 'windowsapps' in parts:
        return 'an app execution alias is not the program itself'   # and the desktop app's packaged copy
    if 'anthropicclaude' in parts:
        return "the Claude desktop app's own copy is not the pinned build"
    return None


def locate_claude(home):
    """The first existing candidate (``claude_candidates``), or FileNotFoundError."""
    for candidate in claude_candidates(home):
        if os.path.lexists(candidate):
            return candidate
    raise FileNotFoundError(2, 'Claude Code %s is not installed' % CLAUDE_VERSION)


def hash_open_file(fd, seconds, read=os.read, clock=None):
    """SHA-256 of what ``fd`` has open, read through it in 1 MiB pieces within ``seconds`` (ValueError when the
    bound passes). ``read`` and ``clock`` stand in for os.read and time.monotonic in tests."""
    import hashlib
    import time
    clock = clock or time.monotonic
    digest, deadline = hashlib.sha256(), clock() + seconds
    while True:
        chunk = read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        if clock() > deadline:
            raise ValueError('hashing took too long')
        digest.update(chunk)


def open_pinned(path, pin):
    """Open the program at ``path`` through the no-link walk, sharing reading only (nobody can write, rename or
    delete it while the descriptor is open), and check it against ``pin`` (SHA-256, size) by hashing through that
    same descriptor within WINDOWS_HASH_SECONDS. Returns the open descriptor, which the caller keeps until the
    program has been started from it, and closes in every path; ValueError or OSError if it is refused."""
    import private_storage as ps
    if pin is None or refused_location(path) is not None or os.path.realpath(path) != path:
        raise ValueError('not the pinned Claude Code')
    fd = ps.open_no_links(path, single_link=True, deny_writers=True)
    try:
        sha256, size = pin
        if os.fstat(fd).st_size != size or hash_open_file(fd, WINDOWS_HASH_SECONDS) != sha256:
            raise ValueError('not the pinned Claude Code')
        return fd
    except BaseException:
        os.close(fd)
        raise
