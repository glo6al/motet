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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_managed_runner as runner
import claude_managed_windows as windows

WINDOWS = os.name == 'nt'
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

    def test_partial_last_line_at_exit_is_eof(self):
        transport = self.transport('import sys; sys.stdout.write("{\\"type\\":"); sys.stdout.flush()')
        transport.wait(timeout=10)
        with self.assertRaises(EOFError):
            for _ in range(50):
                transport.read(.1)

    def test_line_over_one_mebibyte_is_refused(self):
        transport = self.transport('import sys; sys.stdout.write("x" * (1024 * 1024 + 2)); sys.stdout.flush(); '
                                   'import time; time.sleep(30)')
        with self.assertRaises(ValueError):
            for _ in range(200):
                transport.read(.1)

    def test_a_write_the_child_never_reads_times_out_and_refuses_the_next(self):
        transport = self.transport('import time; time.sleep(30)')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'x' * (runner.MAX_NATIVE_LINE - 64)})    # more than any pipe buffer
        self.assertLess(time.monotonic() - started, 10)
        with self.assertRaises(TimeoutError):
            transport.write({'value': 'next'})      # the first is still blocked: frames never interleave

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
        self.assertEqual(transport.job.calls, ['terminate', 'close'])


FAKE_CLAUDE = '''import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
sys.stdout.write('{"grandchild": %d}\\n' % child.pid)
sys.stdout.flush()
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

    def start(self):
        process, job = windows.start_in_job([sys.executable, '-u', str(self.script)], str(self.folder))
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
        transport, grandchild = self.start()
        self.assertTrue(alive(grandchild))
        transport.terminate()
        code = transport.wait(timeout=3)              # input closed; the fake then sleeps, so the job is ended
        self.assertIsNotNone(code)
        self.assertTrue(gone(transport.process.pid))
        self.assertTrue(gone(grandchild))

    def test_closing_the_job_handle_ends_the_tree(self):
        transport, grandchild = self.start()
        transport.close()                              # the supervisor's only job handle: kill on close
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
            process = windows.spawn_detached(argv)
            self.assertEqual(process.wait(timeout=30), 7)
        else:
            self.assertEqual(refusal, windows.DETACH_REFUSED)
            with self.assertRaises(windows.DetachRefused):
                windows.spawn_detached(argv)


if __name__ == '__main__':
    unittest.main()
