#!/usr/bin/env python3
"""The owner's stop of a chat Motet started, with real processes on a Mac.

The fictional Claude is the one the Windows end-to-end test runs (test_managed_windows_end_to_end.FICTIONAL_CLAUDE),
run here as a script with this Python, with three marker files in its folder: one makes it also start a background
process in a session of its own (as a program that detaches does), one makes it ignore SIGTERM, and one makes it
outlive the end of its input (so it outlives its supervisor). Its grandchild stays in its process group. A real
supervisor (claude_managed_runner.serve) runs it, with one change: the pin it checks Claude against is the fictional
build's; one test gives it an older Motet's commands. The manager talks to the supervisor over its real socket, and
the stop goes through the real route.

Checked: Stop through the route ends Claude and its group, and the supervisor then ends; a Claude that ignores
SIGTERM is ended by SIGKILL; a process in a session of its own is never signalled and keeps the stop unconfirmed until
it is gone; without its supervisor nothing is signalled, and the end is confirmed only once nothing is seen running;
a bystander whose command line names the run is never adopted as its supervisor; a recorded process ID that now names
another process is never signalled; an older supervisor's shutdown leaves the stop unconfirmed while its tree runs.
Only the group signal is ever sent. No real Claude is started; fictional names only.

Run: python3 tests/test_managed_stop_process.py
"""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'src'
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))
import claude_managed
import claude_managed_runner as runner
import managed_process_tree as tree
from new_chat_types import FrozenFolder
import server
from test_managed_windows_end_to_end import FICTIONAL_CLAUDE

MAC = sys.platform == 'darwin'

# The Windows end-to-end test's fictional Claude, with the two additions this test needs.
GRANDCHILD = ("    grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
              "stdin=subprocess.DEVNULL,\n                                  stdout=subprocess.DEVNULL, "
              "stderr=subprocess.DEVNULL)\n")
RECORD = "'grandchild': grandchild.pid,"
LOOP_END = "                  'result': 'Fictional reply after ' + behavior + '.'})\n"


def fictional_claude():
    source = FICTIONAL_CLAUDE
    for old in (GRANDCHILD, RECORD, LOOP_END):
        assert source.count(old) == 1, old
    source = source.replace('#!python', '#!' + os.path.realpath(sys.executable), 1)
    source = source.replace(GRANDCHILD, GRANDCHILD + (
        "    detached = (subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        "stdin=subprocess.DEVNULL,\n                                stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True) if os.path.exists('fictional-detach') else None)\n"
        "    if os.path.exists('fictional-ignore-term'):\n"
        "        import signal\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"))
    source = source.replace(RECORD, RECORD + " 'detached': detached.pid if detached else None,")
    return source.replace(LOOP_END, LOOP_END + (
        "    if os.path.exists('fictional-linger'):\n"
        "        import time\n"
        "        time.sleep(120)\n"))


SUPERVISOR = r'''import sys
sys.path.insert(0, %r)
import claude_managed_runner as runner
runner.PINNED_SHA256 = sys.argv[2]
if sys.argv[3:] == ['legacy']:
    # A supervisor as an older Motet started it: it knows no 'stop', only 'shutdown' (SIGTERM to Claude alone).
    command = runner.Runner.command
    def older(self, name, params):
        if name == 'stop':
            runner._fail('invalid_request')
        return command(self, name, params)
    runner.Runner.command = older
raise SystemExit(runner.serve(sys.argv[1]))
'''


def alive(pid):
    """Running (not ended, and not a zombie waiting to be reaped)."""
    row = tree.table().get(pid)
    return tree.alive(row)


def gone(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(.05)
    return True


@unittest.skipUnless(MAC, 'a real supervisor and the fictional Claude: macOS, where managed Claude runs on POSIX')
class MacStopTests(unittest.TestCase):
    def setUp(self):
        # Short: the supervisor's socket path must stay under the Mac's 104 bytes.
        base = Path(tempfile.mkdtemp(prefix='ms', dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree, str(base), True)      # registered first, so it runs after every process ends
        base.chmod(0o700)
        self.started = {}
        self.supervisors = []
        self.legacy = []
        self.addCleanup(self.end_everything)
        self.state = base / '.live-room'
        self.state.mkdir(mode=0o700)
        self.project = base / 'orchard'
        self.project.mkdir(mode=0o700)
        self.exe = base / 'fictional-claude'
        content = fictional_claude().encode('utf-8')
        self.exe.write_bytes(content)
        self.exe.chmod(0o700)
        self.sha = hashlib.sha256(content).hexdigest()
        self.supervisor = base / 'supervisor.py'
        self.supervisor.write_text(SUPERVISOR % str(SRC), encoding='utf-8')
        pinned = mock.patch.object(runner, 'PINNED_SHA256', self.sha)    # the readers in this process
        pinned.start()
        self.addCleanup(pinned.stop)
        st = self.project.stat()
        self.folder = FrozenFolder('orchard', 'revision', 'Orchard', str(self.project), st.st_dev, st.st_ino)

    def end_everything(self):
        for pid in self.started.values():
            if alive(pid):
                os.kill(pid, signal.SIGKILL)          # the test's own fictional processes, which it started
        for process in self.supervisors:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)

    def end_ourselves(self, *names):
        """The test ends its own fictional processes, as the owner would with the Terminal command."""
        for name in names:
            if alive(self.started[name]):
                os.kill(self.started[name], signal.SIGKILL)
            self.assertTrue(gone(self.started[name]), name)

    def spawn(self, run_dir):
        """The supervisor's start as spawn_runner makes it (its own session, no inherited streams)."""
        process = subprocess.Popen([sys.executable, str(self.supervisor), str(run_dir), self.sha] + self.legacy,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        self.supervisors.append(process)

    def start(self, *markers):
        for name in markers:
            (self.project / name).write_text('')
        manager = claude_managed.ManagedClaude(
            self.state, home=self.state.parent, enabled=True, configuration_ready=True,
            _test_dependencies=claude_managed.TestDependencies(
                executable=dict(path=str(self.exe), version=claude_managed.VERSION, sha256=self.sha),
                spawn=self.spawn, request=claude_managed.client_request, read_snapshot=runner.read_snapshot,
                directory=None, launch_timeout=claude_managed.STARTUP_TIMEOUT,
                read_message=runner.read_message, read_decision=runner.read_decision,
                real_processes=True))           # the stop's own process work is the real one here
        run_id = manager.reserve(self.folder, str(uuid.uuid4()))
        native = manager.launch_once(run_id, self.folder, lambda *args: None)
        with open(str(self.project / 'fictional-claude.json')) as source:
            record = json.load(source)
        self.started = {name: record[name] for name in ('claude', 'grandchild', 'detached') if record[name]}
        for name, pid in self.started.items():
            self.assertTrue(alive(pid), name)
        claude = tree.table()[record['claude']]
        self.assertEqual(claude.pgid, claude.pid, 'Claude leads its own process group')
        if record['detached']:
            self.assertNotEqual(tree.table()[record['detached']].pgid, claude.pgid, 'the detached one left it')
        return manager, run_id, native

    def assert_all_gone(self):
        for name, pid in self.started.items():
            self.assertTrue(gone(pid), '%s (process %d) is still running' % (name, pid))

    @contextmanager
    def route(self, manager):
        httpd = server.Server(('127.0.0.1', 0), server.H)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with mock.patch.object(server, 'CLAUDE_MANAGED', manager), mock.patch.object(server, 'MESSAGING', None), \
                mock.patch.object(server, 'PORT', httpd.server_port), mock.patch.object(server, 'KEY', 'fictional-owner'):
            thread.start()

            def post(path, body):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=30)
                raw = json.dumps(body)
                connection.request('POST', path, raw, {'X-Live-Room-Key': 'fictional-owner',
                                                       'Content-Type': 'application/json'})
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            try:
                yield post
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_stop_through_the_route_ends_claude_and_everything_it_started(self):
        manager, run_id, native = self.start()
        with self.route(manager) as post:
            status, reply = post('/api/managed-claude/runs/%s/stop' % run_id,
                                 {'requestId': str(uuid.uuid4()), 'agent': 'c:' + native})
        self.assertEqual(status, 200, reply)
        self.assertEqual((reply['state'], reply['ownerStop']['outcome'], reply['already']), ('exited', 'stopped', False))
        self.assert_all_gone()
        self.assertEqual(self.supervisors[-1].wait(timeout=30), 0)        # the supervisor ends after the exit
        with manager._registry() as db:
            self.assertFalse(manager._row(db, run_id)['occupied'])

    def test_a_claude_that_ignores_sigterm_is_ended_with_sigkill(self):
        manager, run_id, native = self.start('fictional-ignore-term')
        began = time.monotonic()
        result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)
        self.assertEqual(result['ownerStop']['outcome'], 'stopped')
        self.assertGreaterEqual(time.monotonic() - began, tree.GENTLE_SECONDS - .5, 'SIGTERM was waited for first')
        self.assert_all_gone()
        self.assertEqual(manager.status(run_id)['nativeExit']['code'], -signal.SIGKILL)

    def test_a_process_that_left_claudes_group_is_never_signalled_and_keeps_the_stop_unconfirmed(self):
        manager, run_id, native = self.start('fictional-detach')
        result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)
        self.assertEqual((result['state'], result['ownerStop']['outcome']), ('exited', 'unconfirmed'))
        self.assertTrue(gone(self.started['claude']) and gone(self.started['grandchild']), 'its group ended')
        self.assertTrue(alive(self.started['detached']), 'a process in a session of its own is not signalled')
        self.assertEqual(manager.status(run_id)['ownerStop']['outcome'], 'unconfirmed')
        self.end_ourselves('detached')
        self.assertEqual(manager.status(run_id)['ownerStop']['outcome'], 'stopped', 'confirmed once it is gone')

    def test_without_its_supervisor_nothing_is_signalled_and_the_end_is_confirmed_only_when_seen(self):
        manager, run_id, native = self.start('fictional-linger')
        supervisor = self.supervisors[-1]
        supervisor.kill()
        supervisor.wait(timeout=30)
        time.sleep(.3)
        result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)
        self.assertEqual(result['ownerStop']['outcome'], 'unconfirmed')
        for name, pid in self.started.items():
            self.assertTrue(alive(pid), name + ' is not signalled without its supervisor')
        self.end_ourselves('claude', 'grandchild')
        result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)           # Try again
        self.assertEqual((result['state'], result['ownerStop']['outcome']), ('exited', 'stopped'))
        with manager._registry() as db:
            self.assertFalse(manager._row(db, run_id)['occupied'])

    def test_a_process_whose_command_names_the_run_is_never_adopted_as_its_supervisor(self):
        manager, run_id, native = self.start('fictional-linger')
        supervisor = self.supervisors[-1]
        supervisor.kill()
        supervisor.wait(timeout=30)
        run_dir = manager._run_dir(run_id)
        # A bystander whose command line names the runner and ends with this run's folder, as the supervisor's does.
        runner_path = str(SRC / 'claude_managed_runner.py')
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', runner_path, str(run_dir)],
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.end_ourselves('claude', 'grandchild')                 # the recorded Claude is gone
        import fcntl
        held = os.open(str(run_dir / 'run.lock'), os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)             # a supervisor lock still held, as if hung
        try:
            self.assertEqual(manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)['ownerStop']['outcome'],
                             'unconfirmed')
            self.assertIsNone(bystander.poll(), 'never adopted from its command line, lock held')
        finally:
            os.close(held)                                           # and later released
        self.assertEqual(manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)['ownerStop']['outcome'], 'stopped')
        self.assertIsNone(bystander.poll(), 'never adopted from its command line, lock released')

    def test_a_process_id_that_names_another_process_is_never_signalled(self):
        manager, run_id, native = self.start('fictional-linger')
        supervisor = self.supervisors[-1]
        supervisor.kill()
        supervisor.wait(timeout=30)
        bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        recorded = runner.read_native_process(manager._run_dir(run_id))
        self.assertEqual(recorded['pid'], self.started['claude'])
        for forged in (dict(recorded, pid=bystander.pid),                      # the ID now names another process
                       dict(recorded, startedAt=recorded['claimedAt'] - 3600, claimedAt=recorded['claimedAt'] - 3601)):
            with self.subTest(forged=forged), mock.patch.object(runner, 'read_native_process', return_value=forged):
                result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)
                self.assertEqual(result['ownerStop']['outcome'], 'unconfirmed')
                self.assertIsNone(bystander.poll(), 'an unverified process is never signalled')
                for name, pid in self.started.items():
                    self.assertTrue(alive(pid), name + ' was not signalled either')

    def test_an_older_supervisors_shutdown_leaves_the_stop_unconfirmed_while_its_tree_runs(self):
        self.legacy = ['legacy']
        manager, run_id, native = self.start('fictional-detach')
        result = manager.stop(run_id, str(uuid.uuid4()), 'c:' + native)
        self.assertEqual((result['state'], result['ownerStop']['outcome']), ('exited', 'unconfirmed'))
        self.assertTrue(gone(self.started['claude']), 'the older supervisor ended Claude itself')
        for name in ('grandchild', 'detached'):
            self.assertTrue(alive(self.started[name]), name + ' is seen, not signalled')
        self.assertEqual(self.supervisors[-1].wait(timeout=30), 0)
        self.end_ourselves('grandchild', 'detached')
        self.assertEqual(manager.status(run_id)['ownerStop']['outcome'], 'stopped', 'confirmed once they are gone')


@unittest.skipIf(os.name == 'nt', 'POSIX process groups and ps')
class ProcessTreeTests(unittest.TestCase):
    def test_the_group_signal_ends_the_group_and_a_process_of_its_own_session_is_only_reported(self):
        script = ("import os, signal, subprocess, sys, time\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "b = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                  "print(a.pid, b.pid, flush=True)\n"
                  "time.sleep(60)\n")
        root = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, start_new_session=True)
        self.addCleanup(root.wait)
        in_group, own_session = (int(pid) for pid in root.stdout.readline().split())
        root.stdout.close()
        self.addCleanup(lambda: alive(own_session) and os.kill(own_session, signal.SIGKILL))

        def group_signal(number):                   # as the supervisor does: only while root is unreaped
            if root.returncode is None:
                os.killpg(root.pid, number)
        with mock.patch.object(tree.os, 'kill', side_effect=AssertionError('an individual process was signalled')):
            left = tree.end(tree.table()[root.pid], group_signal=group_signal, gentle=.5, firm=2)
        self.assertEqual(root.wait(timeout=5), -signal.SIGKILL)
        self.assertTrue(gone(in_group), 'the group ended')
        self.assertEqual(list(left), [own_session], 'the one in a session of its own is reported, not signalled')
        self.assertTrue(alive(own_session))

    def test_a_target_replaced_after_discovery_receives_nothing(self):
        uid = os.getuid()
        root = tree.Process(500, os.getpid(), 500, uid, 'S', 1000.0, 'fictional-claude')
        child = tree.Process(501, 500, 777, uid, 'S', 1001.0, 'fictional-detached')
        replaced = tree.Process(501, 1, 501, uid, 'S', 2000.0, 'fictional-unrelated-reused-pid')
        reads = iter([{500: root, 501: child}] + [{500: root._replace(stat='Z'), 501: replaced}] * 200)
        groups = []
        clock = iter(range(1000))
        with mock.patch.object(tree.os, 'kill', side_effect=AssertionError('an individual process was signalled')):
            left = tree.end(root, group_signal=groups.append, gentle=1, firm=1, read=lambda: next(reads),
                            clock=lambda: next(clock), sleep=lambda _: None)
        self.assertEqual(groups, [signal.SIGTERM], 'only the group is ever signalled')
        self.assertEqual(left, {}, 'the replacement is not the recorded process: neither signalled nor counted')

    def test_claude_already_reaped_its_group_is_reported_not_signalled(self):
        script = ("import subprocess, sys\n"
                  "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "print(a.pid, flush=True)\n")
        claude = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, start_new_session=True)
        child = int(claude.stdout.readline())
        claude.stdout.close()
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        claude.wait(timeout=10)                                         # ended and reaped before the stop
        transport = runner.NativeTransport({})
        transport.process = claude
        with mock.patch.object(runner.os, 'killpg', side_effect=AssertionError('a reaped group was signalled')):
            code, survivors = transport.end_tree(gentle=.2, firm=.2)
        self.assertEqual(code, 0)
        self.assertEqual([pid for pid, _ in survivors], [child])
        self.assertTrue(alive(child))

    def test_an_unreadable_process_table_signals_nothing(self):
        def broken(*args, **kwargs):
            raise OSError('fictional')
        with self.assertRaises(tree.TreeError):
            tree.table(run=broken)


if __name__ == '__main__':
    unittest.main()
