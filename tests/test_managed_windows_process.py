#!/usr/bin/env python3
"""Managed Claude's processes and pipes on Windows (step 8b.2; the feature stays off there until 8b.5).

On every system: the runner's native transport in its threaded mode (the reader thread and deadline-bound writes
Windows uses, because it can select sockets only) against a fictional child, and the Shutdown decision
(``stop_tree``) with fakes. On real Windows only: Claude's job, with a fictional "claude" Python script that starts
a grandchild; the whole tree is gone after Shutdown and after the job's handle closes; and the supervisor's
detached start. No Claude is started and no model or account is used.

Run: python tests/test_managed_windows_process.py
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_managed_runner as runner
import claude_managed_windows as windows

WINDOWS = os.name == 'nt'
LINES = ('import sys\n'         # about 3.3 KB: under the 4 KiB a Windows pipe holds before a writer waits
         'for n in range(300):\n'
         '    sys.stdout.write(\'{"n": %d}\\n\' % n)\n'
         'sys.stdout.flush()\n')
ECHO = ('import sys,json\n'
        'for raw in sys.stdin:\n'
        '    row=json.loads(raw)\n'
        '    print(json.dumps({"type":"fictional","value":row["value"]}),flush=True)\n')


def threaded(script):
    """A transport in its Windows (threaded) mode around a fictional child running ``script``."""
    transport = runner.NativeTransport({})
    transport.process = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    transport.pipes = windows.ThreadedPipes(transport.process)
    return transport


def frame(transport, attempts=50):
    for _ in range(attempts):
        value = transport.read(.1)
        if value is not None:
            return value
    return None


class ThreadedTransportTests(unittest.TestCase):
    def transport(self, script):
        transport = threaded(script)

        def cleanup():
            if transport.process.poll() is None:
                transport.process.kill()
            transport.process.wait(timeout=10)
            transport.close()
        self.addCleanup(cleanup)
        return transport

    def test_fake_child_uses_bounded_jsonl_and_actual_exit(self):
        """The same exchange as NativePipeTests, through the reader thread and a writer thread."""
        transport = self.transport('import sys,json; row=json.loads(sys.stdin.readline()); '
                                   'print(json.dumps({"type":"fictional","value":row["value"]}),flush=True)')
        transport.write({'value': 'fictional'})
        self.assertEqual(frame(transport), {'type': 'fictional', 'value': 'fictional'})
        self.assertEqual(transport.wait(timeout=10), 0)
        self.assertEqual(transport.poll(), 0)

    def test_many_frames_in_order_and_split_lines(self):
        transport = self.transport(ECHO)
        for value in range(200):
            transport.write({'value': value})
        self.assertEqual([frame(transport)['value'] for _ in range(200)], list(range(200)))
        transport.process.stdin.close()
        self.assertEqual(transport.wait(timeout=10), 0)

    def test_end_of_stream_and_exit_are_eof(self):
        transport = self.transport('import sys; sys.stdout.write("{\\"type\\":\\"last\\"}\\n"); sys.stdout.flush()')
        self.assertEqual(frame(transport), {'type': 'last'})
        transport.wait(timeout=10)
        with self.assertRaises(EOFError):
            for _ in range(50):
                transport.read(.1)
        with self.assertRaises(EOFError):
            transport.write({'value': 'after exit'})     # a write after exit is refused before writing

    def test_every_frame_written_before_exit_is_read_then_eof_stays(self):
        transport = self.transport(LINES)
        transport.wait(timeout=10)                   # the child has exited before anything is read
        seen = []
        with self.assertRaises(EOFError):
            while True:
                value = transport.read(.1)
                if value is not None:
                    seen.append(value['n'])
        self.assertEqual(seen, list(range(300)))
        for _ in range(3):                           # once seen, the end is the answer every time
            with self.assertRaises(EOFError):
                transport.read(0)

    def test_partial_last_line_at_exit_is_eof(self):
        transport = self.transport('import sys; sys.stdout.write("{\\"type\\":"); sys.stdout.flush()')
        transport.wait(timeout=10)
        with self.assertRaises(EOFError):
            for _ in range(50):
                transport.read(.1)

    def test_line_over_one_mebibyte_is_refused(self):
        transport = self.transport('import sys; sys.stdout.write("x" * (1024 * 1024 + 2)); sys.stdout.flush(); '
                                   'import time; time.sleep(30)')
        deadline = time.monotonic() + 10      # by time, not reads: Windows pipes may pass 4 KiB at a time
        with self.assertRaises(ValueError):
            while time.monotonic() < deadline:
                transport.read(.1)

    def test_a_write_the_child_never_reads_times_out_and_refuses_the_next(self):
        transport = self.transport('import time; time.sleep(30)')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'x' * (runner.MAX_NATIVE_LINE - 64)})    # more than any pipe buffer
        self.assertLess(time.monotonic() - started, 10)
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'next'})      # the first is still blocked: frames never interleave

    def test_close_leaves_a_pipe_whose_write_is_still_blocked(self):
        transport = self.transport('import time; time.sleep(30)')
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'x' * (runner.MAX_NATIVE_LINE - 64)})
        started = time.monotonic()
        self.assertFalse(transport.pipes.close(seconds=.2))       # the input pipe is left, not waited on
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(transport.process.stdin.closed)
        transport.process.kill()
        transport.process.wait(timeout=10)
        self.assertTrue(transport.pipes.close(seconds=5))         # once the writer has returned, both close
        self.assertTrue(transport.process.stdin.closed)

    def test_an_oversized_frame_is_refused_before_writing(self):
        transport = self.transport('import time; time.sleep(30)')
        with self.assertRaises(EOFError):
            transport.write({'value': 'x' * runner.MAX_NATIVE_LINE})


class StopDecisionTests(unittest.TestCase):
    """Shutdown on Windows: input closed, a bounded wait, then the whole job ended either way."""

    def test_an_exit_in_time_still_ends_the_tree(self):
        calls = []
        code = windows.stop_tree(lambda: calls.append('close'), lambda seconds: calls.append(('wait', seconds)) or 0,
                                 lambda: calls.append('kill'), 3)
        self.assertEqual((code, calls), (0, ['close', ('wait', 3), 'kill']))

    def test_no_exit_in_time_ends_the_tree_then_reads_the_code(self):
        calls = []

        def wait(seconds):
            calls.append(('wait', seconds))
            if len([c for c in calls if c[0] == 'wait']) == 1:
                raise subprocess.TimeoutExpired('fictional', seconds)
            return 1
        code = windows.stop_tree(lambda: calls.append('close'), wait, lambda: calls.append('kill'), 3)
        self.assertEqual((code, calls), (1, ['close', ('wait', 3), 'kill', ('wait', 5)]))

    def test_a_blocked_write_skips_closing_input_and_ends_the_job_at_once(self):
        calls = []
        code = windows.stop_tree(lambda: calls.append('close'), lambda seconds: calls.append(('wait', seconds)) or 9,
                                 lambda: calls.append('kill'), 3, input_blocked=lambda: True)
        self.assertEqual((code, calls), (9, ['kill', ('wait', 5)]))

    def test_an_unconfirmed_end_is_left_to_the_caller(self):
        def wait(seconds):
            raise subprocess.TimeoutExpired('fictional', seconds)
        with self.assertRaises(subprocess.TimeoutExpired):
            windows.stop_tree(lambda: None, wait, lambda: None, 3)

    def test_the_transport_uses_the_decision_only_with_a_job(self):
        class Job:
            def __init__(self):
                self.calls = []

            def terminate(self):
                self.calls.append('terminate')

            def close(self):
                self.calls.append('close')
        transport = threaded('import sys; sys.stdin.read()')      # ends when its input closes
        transport.job = Job()
        try:
            transport.terminate()
            self.assertIsNone(transport.process.poll())           # nothing is signalled before ``wait``
            self.assertEqual(transport.wait(timeout=10), 0)
            self.assertEqual(transport.job.calls, ['terminate'])
        finally:
            transport.close()
        # Closing ends the job first (before any pipe is touched), then closes its only handle.
        self.assertEqual(transport.job.calls, ['terminate', 'terminate', 'close'])


class FakeKernel:
    """Win32 calls answered in Python, for the decisions around them (portable)."""

    def __init__(self, resume=1, owner=9):
        self.resume, self.owner, self.ended = resume, owner, []

    def CreateToolhelp32Snapshot(self, flags, pid):
        return 1

    def Thread32First(self, snapshot, entry):
        entry._obj.th32OwnerProcessID, entry._obj.th32ThreadID = self.owner, 7
        return 1

    def Thread32Next(self, snapshot, entry):
        return 0

    def OpenThread(self, access, inherit, thread):
        return 2

    def ResumeThread(self, thread):
        return self.resume

    def CloseHandle(self, handle):
        return 1

    def TerminateProcess(self, handle, code):
        self.ended.append((handle, code))
        return 1


class FakeWin32Tests(unittest.TestCase):
    def setUp(self):
        import ctypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32), ('th32ThreadID', ctypes.c_uint32),
                        ('th32OwnerProcessID', ctypes.c_uint32), ('tpBasePri', ctypes.c_int32),
                        ('tpDeltaPri', ctypes.c_int32), ('dwFlags', ctypes.c_uint32)]
        self.kernel = FakeKernel()
        fake = (ctypes, type('W', (), {'HANDLE': ctypes.c_void_p}), self.kernel, None, ThreadEntry)
        patcher = mock.patch.object(windows, '_api', return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def popen(self, **kwargs):
        process = mock.Mock(pid=9, _handle=5, stdin=None, stdout=None, stderr=None)
        return mock.patch.object(windows.subprocess, 'Popen', return_value=process, **kwargs)

    def test_a_thread_that_was_already_running_may_have_run(self):
        self.kernel.resume = 0
        with self.assertRaises(windows.AlreadyRunning) as raised:
            windows._resume_only_thread(9)
        self.assertNotIsInstance(raised.exception, windows.NotStarted)
        self.kernel.resume = 1
        windows._resume_only_thread(9)                        # suspended once: resumed

    def test_a_supervisor_that_may_have_run_is_never_reported_not_started(self):
        self.kernel.resume = 0
        with self.popen(), mock.patch.object(windows, '_in_any_job', return_value=False):
            with self.assertRaises(windows.JobError) as raised:
                windows.spawn_detached(['fictional-python', 'fictional-runner'])
        self.assertNotIsInstance(raised.exception, windows.NotStarted)
        self.assertEqual(self.kernel.ended, [(5, windows.JOB_ENDED_EXIT)])   # ended anyway

    def test_only_access_denied_in_a_job_is_a_refused_detach(self):
        def refused(code):
            error = OSError('fictional start failure')
            error.winerror = code
            return error
        for code, in_job, expected in ((5, True, windows.DetachRefused), (2, True, windows.NotStarted),
                                       (5, False, windows.NotStarted), (2, False, windows.NotStarted)):
            with self.subTest(winerror=code, in_job=in_job):
                with self.popen(side_effect=refused(code)), \
                        mock.patch.object(windows, '_server_in_a_job', return_value=in_job):
                    with self.assertRaises(windows.NotStarted) as raised:
                        windows.spawn_detached(['fictional-python', 'fictional-runner'])
                self.assertIs(type(raised.exception), expected)


FAKE_CLAUDE = '''import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
sys.stdout.write('{"grandchild": %d}\\n' % child.pid)
sys.stdout.flush()
if sys.argv[1:] == ['read']:
    sys.stdin.read()
time.sleep(120)
'''


def alive(pid):
    """Real Windows: whether process ``pid`` is still running."""
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k.GetExitCodeProcess.argtypes, k.GetExitCodeProcess.restype = [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL
    k.CloseHandle.argtypes, k.CloseHandle.restype = [w.HANDLE], w.BOOL
    handle = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = w.DWORD()
        return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


def gone(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(.05)
    return True


@unittest.skipUnless(WINDOWS, 'Windows job objects')
class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='managed-job-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.script = self.folder / 'fictional_claude.py'
        self.script.write_bytes(FAKE_CLAUDE.encode('utf-8'))

    def start(self, *mode):
        process, job = windows.start_in_job([sys.executable, '-u', str(self.script), *mode], str(self.folder))
        transport = runner.NativeTransport({})
        transport.process, transport.job = process, job
        transport.pipes = windows.ThreadedPipes(process)

        def cleanup():
            transport.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.addCleanup(cleanup)
        first = frame(transport, 200)
        self.assertIsNotNone(first)
        return transport, first['grandchild']

    def test_shutdown_ends_claude_and_everything_it_started(self):
        transport, grandchild = self.start('read')
        self.assertTrue(alive(grandchild))
        started = time.monotonic()
        transport.terminate()
        code = transport.wait(timeout=3)              # input closed; the fake then sleeps, so the job is ended
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(code, windows.JOB_ENDED_EXIT)
        self.assertTrue(transport.job.ended)
        self.assertTrue(gone(transport.process.pid))
        self.assertTrue(gone(grandchild))

    def test_shutdown_with_a_blocked_write_ends_the_job_at_once(self):
        transport, grandchild = self.start()          # this fake never reads its input
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'x' * (runner.MAX_NATIVE_LINE - 64)})
        started = time.monotonic()
        transport.terminate()
        self.assertEqual(transport.wait(timeout=3), windows.JOB_ENDED_EXIT)
        transport.close()
        self.assertLess(time.monotonic() - started, 15)  # never waits on the blocked write, nor on the fake's 120 s
        self.assertTrue(gone(grandchild))

    def test_closing_ends_the_tree_without_waiting_for_claude(self):
        transport, grandchild = self.start('read')
        started = time.monotonic()
        transport.close()                              # job ended first, then the pipes; the fake sleeps 120 s
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(gone(transport.process.pid))
        self.assertTrue(gone(grandchild))

    def test_closing_the_job_handle_alone_ends_the_tree(self):
        transport, grandchild = self.start('read')
        transport.job.close()                          # the only handle: kill on close
        self.assertTrue(gone(transport.process.pid))
        self.assertTrue(gone(grandchild))

    def test_a_failed_assignment_never_resumes_the_process(self):
        original = windows.Job.assign

        def refuse(job, handle):
            raise windows.JobError('fictional refusal')
        windows.Job.assign = refuse
        try:
            with self.assertRaises(windows.JobError):
                windows.start_in_job([sys.executable, '-c', 'open("ran.txt", "w").close()'], str(self.folder))
        finally:
            windows.Job.assign = original
        time.sleep(.5)
        self.assertFalse((self.folder / 'ran.txt').exists())

    def test_the_supervisor_starts_detached_or_is_refused_plainly(self):
        refusal = windows.breakaway_refusal()
        argv = [sys.executable, '-c', 'import sys; sys.exit(7)']
        if refusal is None:
            try:
                process = windows.spawn_detached(argv)
            except windows.DetachRefused:
                return                # an outer job forbids it, which only the started supervisor shows
            self.assertEqual(process.wait(timeout=30), 7)
        else:
            self.assertEqual(refusal, windows.DETACH_REFUSED)
            with self.assertRaises(windows.DetachRefused):
                windows.spawn_detached(argv)

    def test_a_supervisor_still_in_a_job_never_runs(self):
        original = windows._in_any_job
        windows._in_any_job = lambda handle: True
        try:
            with self.assertRaises(windows.DetachRefused):
                windows.spawn_detached([sys.executable, '-c', 'open(%r, "w").close()' % str(self.folder / 'ran.txt')])
        finally:
            windows._in_any_job = original
        time.sleep(.5)
        self.assertFalse((self.folder / 'ran.txt').exists())


if __name__ == '__main__':
    unittest.main()
