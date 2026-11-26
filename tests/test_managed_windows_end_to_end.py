#!/usr/bin/env python3
"""Managed Claude switched on for Windows (step 8b.5), for the Claude adapter only.

On every system, with the platform patched explicitly (never taken from the machine running the tests): the gates
let macOS and Windows through and keep Linux refused, as it was before 8b.5. That covers the readiness record,
preflight, the supervisor's start, the server's managed factory, New chat and its known-folder inventory, and the
owner fence. On Windows, Codex New chat is refused in plain words, and building its adapter neither touches anything
nor stops Claude from starting.

On real Windows only, with no real Claude, through the manager's test seams (claude_managed.TestDependencies: a
fictional executable, its pin and the supervisor's spawn). A whole managed start runs in order:
- the readiness record for this PC;
- the executable check;
- the supervisor, and Claude in its kill-on-close job;
- the named-pipe control channel and the native claim;
- one message with one permission (Allow) and its reply;
- Stop, after which nothing the fictional Claude started is still running.
A supervisor that is killed takes Claude and everything it started with it.

The fictional "claude.exe" is pip's own console-script launcher (distlib) with a Python script appended, as pip
builds pip.exe. So the started program is the very file that was hashed and held, as the real build is, and the
check that Claude was started from that file is made for real. The supervisor, and the supervisor's read-only
readers in this process (status after it ended), accept that file's pin in place of Claude Code 2.1.323's; nothing
else is changed. A missing real-Windows prerequisite fails these tests; it
never skips them.

Run: python tests/test_managed_windows_end_to_end.py
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'src'
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))
import private_storage as ps
import claude_managed
import claude_managed_runner as runner
import codex_new_chat
from new_chat_types import FrozenFolder
import new_chat_inventory
import server

WINDOWS = os.name == 'nt'


def private_folder(path):
    """A folder only this user can open. On Windows through private_storage: the test runner is elevated, so a plain
    folder would inherit an access list that names Administrators too."""
    if WINDOWS:
        ps.make_private_folder(str(path))
    else:
        path.mkdir(mode=0o700)
    return path


class PlatformGateTests(unittest.TestCase):
    """The platform gates, on every system. Each case names its platform; private_storage's Windows switch is
    patched with it only where everything behind the gate is replaced, since a Mac or Linux machine has no Win32."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='managed-gates-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.home = private_folder(self.root / 'home')

    @staticmethod
    def platform(name, windows=False):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(sys, 'platform', name))
        stack.enter_context(mock.patch.object(ps, 'WIN', windows))
        return stack

    def test_the_readiness_record_is_read_on_macos_and_windows_only(self):
        state = str(self.home / '.live-room')
        with self.platform('win32', windows=True), \
                mock.patch.object(claude_managed, '_windows_readiness', return_value=True) as windows:
            self.assertIs(claude_managed.load_configuration_readiness(state, enabled=True), True)
        windows.assert_called_once_with(state)
        for name, read in (('darwin', True), ('linux', False)):
            with self.subTest(platform=name), self.platform(name), \
                    mock.patch.object(claude_managed, '_windows_readiness', side_effect=AssertionError('Windows read')), \
                    mock.patch.object(claude_managed, 'Path', side_effect=OSError('the POSIX read began')) as posix:
                self.assertIs(claude_managed.load_configuration_readiness(state, enabled=True), False)
                self.assertEqual(posix.called, read)
        with self.platform('win32', windows=True), \
                mock.patch.object(claude_managed, '_windows_readiness', side_effect=AssertionError('read')):
            self.assertIs(claude_managed.load_configuration_readiness(state), False)          # still off by default
            self.assertIs(claude_managed.load_configuration_readiness(state, enabled=True, demo=True), False)

    def test_preflight_lets_macos_and_windows_past_the_platform_gate(self):
        manager = claude_managed.ManagedClaude(self.home / '.live-room', home=self.home, enabled=True,
                                               configuration_ready=True)
        reached = claude_managed.ManagedError(503, 'fictional_reached', 'The executable check was reached.')
        for name, windows, code in (('win32', True, 'fictional_reached'), ('darwin', False, 'fictional_reached'),
                                    ('linux', False, 'app_version_unsupported')):
            with self.subTest(platform=name), self.platform(name, windows), \
                    mock.patch.object(claude_managed.ManagedClaude, '_breakaway_refusal', return_value=None), \
                    mock.patch.object(claude_managed.ManagedClaude, '_executable', side_effect=reached) as executable:
                capability = manager.preflight()
                self.assertFalse(capability.available)
                self.assertEqual(capability.code, code)
                self.assertEqual(executable.called, code == 'fictional_reached')
        unready = claude_managed.ManagedClaude(self.home / '.live-room', home=self.home, enabled=True)
        with self.platform('win32', windows=True), \
                mock.patch.object(claude_managed.ManagedClaude, '_executable', side_effect=reached):
            self.assertEqual(unready.preflight().code, 'configuration_unverified')     # the record still decides

    def test_the_supervisor_starts_claude_on_macos_and_windows_only(self):
        exe = str(self.root / 'fictional-claude')
        transport = runner.NativeTransport({'executable': {'path': exe}})
        with self.platform('win32', windows=True), \
                mock.patch.object(runner.NativeTransport, '_windows_start') as windows_start:
            transport.start()
        windows_start.assert_called_once_with(exe)
        for name, reached in (('darwin', True), ('linux', False)):
            with self.subTest(platform=name), self.platform(name), \
                    mock.patch.object(runner.NativeTransport, '_windows_start', side_effect=AssertionError('Windows')), \
                    mock.patch.object(runner.os.path, 'realpath', side_effect=lambda path: path + '-moved') as posix:
                with self.assertRaises(runner.RunnerError) as raised:
                    transport.start()
                self.assertEqual(raised.exception.code, 'unsupported_build')
                self.assertEqual(posix.called, reached)
        self.assertIsNone(transport.process)

    def test_the_server_starts_the_managed_manager_on_macos_and_windows_only(self):
        with mock.patch.object(server, 'HOME', str(self.home)), \
                mock.patch.object(claude_managed, 'load_configuration_readiness', return_value=True) as readiness:
            for name in ('win32', 'darwin'):
                with self.subTest(platform=name), mock.patch.object(sys, 'platform', name):
                    manager, failed = server.start_managed_claude()
                    self.assertIsInstance(manager, claude_managed.ManagedClaude)
                    self.assertFalse(failed)
                    self.assertTrue(manager.configuration_ready)
            with mock.patch.object(sys, 'platform', 'linux'):
                self.assertEqual(server.start_managed_claude(), (None, False))
        self.assertEqual(readiness.call_count, 2)
        self.assertEqual(readiness.call_args, mock.call(str(self.home / '.live-room'), enabled=True, demo=False))
        self.assertFalse(os.path.exists(str(self.home / '.live-room')))                   # nothing created at start

    def test_new_chat_starts_on_windows_with_claude_and_a_plainly_refused_codex(self):
        manager = claude_managed.ManagedClaude(self.home / '.live-room', home=self.home, enabled=True)
        chooser = mock.Mock(return_value=mock.Mock(return_value=None))
        with mock.patch.object(server, 'HOME', str(self.home)), mock.patch.object(server, 'CLAUDE_MANAGED', manager), \
                mock.patch.dict(os.environ, {'WATCH_DEMO': ''}), \
                mock.patch('new_chat_folder_chooser.host_chooser', chooser), \
                mock.patch.object(codex_new_chat, '_regular', side_effect=AssertionError('Codex files read')):
            with mock.patch.object(sys, 'platform', 'linux'):
                self.assertIsNone(server.start_new_chat())
            with mock.patch.object(sys, 'platform', 'win32'):
                service = server.start_new_chat()
                self.assertIsNotNone(service)
                try:
                    self.assertEqual(set(service.adapters), {'codex', 'claude_code'})
                    self.assertIs(service.adapters['claude_code'].manager, manager)
                    codex = service.adapters['codex'].preflight()
                finally:
                    service.close()
        self.assertFalse(codex.available)
        self.assertEqual((codex.code, codex.message), ('unsupported_platform', 'Codex New chat currently requires macOS.'))

    def test_codex_new_chat_is_refused_in_plain_words_on_windows_without_touching_anything(self):
        adapter = codex_new_chat.Adapter(home=self.home)        # building it reads nothing
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(codex_new_chat, '_regular', side_effect=AssertionError('Codex files read')), \
                mock.patch.object(codex_new_chat.subprocess, 'Popen', side_effect=AssertionError('Codex started')):
            capability = adapter.preflight()
            self.assertEqual((capability.available, capability.code), (False, 'unsupported_platform'))
            self.assertEqual(capability.message, 'Codex New chat currently requires macOS.')
            folder = FrozenFolder('orchard', 'revision', 'Orchard', str(self.home), 1, 1)
            with self.assertRaises(codex_new_chat.NewChatError) as raised:
                adapter.create_once(folder, lambda *_: None)
            self.assertEqual(raised.exception.code, 'unsupported_platform')

    def test_the_owner_fence_counts_windows_as_a_managed_platform(self):
        with mock.patch.multiple(server, CLAUDE_MANAGED=None, CLAUDE_MANAGED_FAILED=False, CLAUDE_MANAGED_ENABLED=True), \
                mock.patch.object(server, '_managed_permission_registry', return_value=None), \
                mock.patch.object(server, '_managed_permission_artifacts_present', return_value=False):
            for name, required in (('win32', True), ('darwin', True), ('linux', False)):
                with self.subTest(platform=name), mock.patch.object(sys, 'platform', name):
                    self.assertIs(server.claude_permission_ownership_check_required(), required)

    def test_new_chat_inventory_offers_claude_folders_only_on_windows(self):
        claude, codex = 'c:' + str(uuid.uuid4()), 'x:' + str(uuid.uuid4())
        proof = object()
        import codex_transcript
        for name, codex_read, expected in (('win32', False, [proof]), ('darwin', True, [proof]), ('linux', False, [])):
            with self.subTest(platform=name), mock.patch.object(sys, 'platform', name), \
                    mock.patch.object(server, 'HOME', str(self.home)), \
                    mock.patch.object(server, 'AGENTS', {claude: {}, codex: {}}), \
                    mock.patch.object(server, 'new_chat_claude_inventory_identity',
                                      side_effect=lambda aid: proof if aid == claude else None) as claude_reader, \
                    mock.patch.object(codex_transcript, 'resolve', side_effect=OSError('fictional')) as codex_reader:
                self.assertEqual(server.new_chat_inventory(), expected)
                self.assertEqual(codex_reader.called, codex_read)
                self.assertEqual(claude_reader.called, bool(expected))


# The fictional Claude Code: stream-json in and out over its pipes, as the pinned build speaks it to Motet. It
# records its own process, its launcher's and a grandchild's, and the arguments it was given, then answers the
# supervisor's initialize, claim_session and get_status with this session and its working folder. A message is
# echoed (--replay-user-messages), asks permission to use Write, and is answered once the decision arrives (which it
# echoes too). The end of its input ends it, as it ends Claude; the grandchild would sleep on, but for the job.
FICTIONAL_CLAUDE = r'''#!python
import json
import os
import subprocess
import sys


def send(frame):
    sys.stdout.buffer.write((json.dumps(frame) + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()


def main():
    args = sys.argv[1:]
    session = args[args.index('--session-id') + 1]
    grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open('fictional-claude.tmp', 'w') as record:
        json.dump({'launcher': os.getppid(), 'claude': os.getpid(), 'grandchild': grandchild.pid, 'args': args,
                   'cwd': os.getcwd()}, record)
    os.replace('fictional-claude.tmp', 'fictional-claude.json')
    folder = os.getcwd()
    for raw in sys.stdin.buffer:
        frame = json.loads(raw.decode('utf-8'))
        kind = frame.get('type')
        if kind == 'control_request':
            request = frame['request']
            subtype = request['subtype']
            if subtype == 'initialize':
                response = {}
            elif subtype == 'claim_session':
                response = {'session_id': session, 'cwd': folder}
            elif subtype == 'get_status':
                response = {'sections': [{'rows': [{'label': 'Session ID', 'value': session},
                                                   {'label': 'cwd', 'value': folder}]}]}
            else:
                send({'type': 'control_response', 'response': {'subtype': 'error', 'request_id': frame['request_id'],
                                                               'error': 'fictional: unsupported'}})
                continue
            send({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': frame['request_id'],
                                                           'response': response}})
        elif kind == 'user':
            send(frame)
            send({'type': 'control_request', 'request_id': 'fictional-permission-1',
                  'request': {'subtype': 'can_use_tool', 'tool_name': 'Write', 'tool_use_id': 'fictional-tool-1',
                              'input': {'file_path': 'notes.txt', 'content': 'Fictional notes.'}}})
        elif kind == 'control_response':
            send({'type': 'control_response', 'response': frame['response']})
            behavior = frame['response']['response']['behavior']
            send({'type': 'assistant', 'session_id': session,
                  'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Fictional.'}]}})
            send({'type': 'result', 'session_id': session, 'subtype': 'success', 'is_error': False,
                  'result': 'Fictional reply after ' + behavior + '.'})


if __name__ == '__main__':
    main()
'''

# The supervisor, started as Motet starts it, with one change: the pin it checks Claude against is the fictional
# build's, given on its command line, in place of Claude Code 2.1.323's for this machine.
SUPERVISOR = r'''import sys
sys.path.insert(0, %r)
import claude_managed_runner as runner
pin = (sys.argv[2], int(sys.argv[3]))
runner._windows_pin = lambda: pin
raise SystemExit(runner.serve(sys.argv[1]))
'''


def fictional_claude_exe(work):
    """The fictional build's bytes: pip's console-script launcher, the shebang naming this Python, and a zip whose
    __main__.py is FICTIONAL_CLAUDE (distlib's ScriptMaker, as pip makes pip.exe)."""
    try:
        from pip._vendor.distlib.scripts import ScriptMaker
    except ImportError as error:
        raise AssertionError("pip's console-script launcher (distlib) is needed to build the fictional claude.exe; "
                             'install pip for this Python') from error
    source, target = work / 'source', work / 'built'
    source.mkdir()
    target.mkdir()
    (source / 'claude.py').write_text(FICTIONAL_CLAUDE, encoding='utf-8')
    maker = ScriptMaker(str(source), str(target), add_launchers=True)
    python = os.path.realpath(sys.executable)
    maker.executable = '"%s"' % python if ' ' in python else python
    maker.variants = {''}
    built = [name for name in maker.make('claude.py') if name.lower().endswith('.exe')]
    if len(built) != 1:
        raise AssertionError('distlib did not build one fictional claude.exe: %r' % (built,))
    return Path(built[0]).read_bytes()


@unittest.skipUnless(WINDOWS, 'real Windows: jobs, named pipes, the firmware identity and access lists')
class WindowsEndToEndTests(unittest.TestCase):
    def setUp(self):
        import claude_managed_windows as windows
        self.windows = windows
        self.assertEqual(sys.platform, 'win32')
        self.assertTrue(ps.folder_walk_available(), 'the private no-link folder walk is unavailable here')
        self.assertIsNotNone(windows.windows_pin(), 'no Claude Code build is pinned for this machine')
        base = Path(tempfile.mkdtemp(prefix='managed-e2e-')).resolve()
        self.addCleanup(shutil.rmtree, str(base), True)        # registered first, so it runs after every process ends
        self.supervisors = []
        self.addCleanup(self.end_supervisors)
        self.home = private_folder(base / 'home')
        self.state = private_folder(self.home / '.live-room')
        versions = self.home
        for name in ('.local', 'share', 'claude', 'versions'):
            versions = private_folder(versions / name)
        self.project = private_folder(self.home / 'orchard')
        work = private_folder(base / 'work')
        content = fictional_claude_exe(work)
        self.exe = versions / '2.1.323.exe'
        self.write_private(self.exe, content)
        self.assertEqual(windows.locate_claude(self.home), str(self.exe))   # where Motet looks, unverified on a PC
        self.assertIsNone(windows.refused_location(str(self.exe)))
        import hashlib
        self.pin = (hashlib.sha256(content).hexdigest(), len(content))
        # The run's manifest names the fictional build, so the ledger readers here (status once the supervisor has
        # ended) check it against the same pin the supervisor was given.
        pinned = mock.patch.object(runner, '_windows_pin', return_value=self.pin)
        pinned.start()
        self.addCleanup(pinned.stop)
        self.supervisor = work / 'supervisor.py'
        self.supervisor.write_text(SUPERVISOR % str(SRC), encoding='utf-8')
        device, inode = new_chat_inventory.folder_identity(str(self.project))
        self.folder = FrozenFolder('orchard', 'revision', 'Orchard', str(self.project), device, inode)

    @staticmethod
    def write_private(path, data):
        fd = ps.create_private_file(str(path))
        try:
            import msvcrt
            msvcrt.setmode(fd, os.O_BINARY)                     # bytes as given, as claude_managed writes them
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    def end_supervisors(self):
        for process in self.supervisors:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)

    def spawn(self, run_dir):
        """The supervisor's start, as spawn_runner makes it: detached from this process and its job. Where the test
        runner's own job forbids leaving it (spawn_detached refuses before the supervisor ran), the supervisor
        starts as an ordinary child instead; the detached start itself is tested in test_managed_windows_process."""
        argv = [sys.executable, str(self.supervisor), str(run_dir), self.pin[0], str(self.pin[1])]
        try:
            process = self.windows.spawn_detached(argv)
        except self.windows.DetachRefused:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=self.windows.CREATE_NO_WINDOW)
        self.supervisors.append(process)

    def ready(self):
        """This PC's readiness record, written as the owner writes it, read back through the lifted gate."""
        record = claude_managed.local_configuration_identity()
        self.write_private(self.state / claude_managed.READINESS_FILENAME, json.dumps(record).encode('utf-8'))
        ready = claude_managed.load_configuration_readiness(str(self.state), enabled=True, demo=False)
        self.assertIs(ready, True)
        return ready

    def manager(self, configuration_ready):
        return claude_managed.ManagedClaude(
            self.state, home=self.home, enabled=True, configuration_ready=configuration_ready,
            _test_dependencies=claude_managed.TestDependencies(
                executable=dict(path=str(self.exe), version=claude_managed.VERSION, sha256=self.pin[0]),
                spawn=self.spawn, request=claude_managed.client_request, read_snapshot=runner.read_snapshot,
                directory=None, launch_timeout=claude_managed.STARTUP_TIMEOUT,
                read_message=runner.read_message, read_decision=runner.read_decision))

    @staticmethod
    def wait_for(probe, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = probe()
            if value is not None:
                return value
            time.sleep(.1)
        raise AssertionError('timed out waiting for the managed session')

    def start(self):
        manager = self.manager(self.ready())
        capability = manager.preflight()
        self.assertTrue(capability.available, capability)
        run_id = manager.reserve(self.folder, str(uuid.uuid4()))
        receipts = []
        native = manager.launch_once(run_id, self.folder, lambda *args: receipts.append(args))
        self.assertEqual(receipts, [(native, self.folder.path)])
        with open(str(self.project / 'fictional-claude.json')) as source:
            started = json.load(source)
        args = started['args']
        self.assertEqual(args[args.index('--session-id') + 1], native)
        for flags in (['--input-format', 'stream-json'], ['--output-format', 'stream-json'], ['--await-claim'],
                      ['--permission-prompt-tool', 'stdio'], ['--replay-user-messages'], ['--print']):
            self.assertTrue(any(args[i:i + len(flags)] == flags for i in range(len(args))), flags)
        self.assertEqual(os.path.normcase(started['cwd']), os.path.normcase(self.folder.path))
        processes = {name: started[name] for name in ('launcher', 'claude', 'grandchild')}
        from test_managed_windows_process import alive
        for name, pid in processes.items():
            self.assertTrue(alive(pid), name)
        return manager, run_id, processes

    def assert_tree_gone(self, processes):
        from test_managed_windows_process import gone
        for name, pid in processes.items():
            self.assertTrue(gone(pid, 15), '%s (process %d) is still running' % (name, pid))

    def test_a_record_for_another_machine_keeps_it_refused(self):
        record = dict(claude_managed.local_configuration_identity(), hostFingerprint='0' * 64)
        self.write_private(self.state / claude_managed.READINESS_FILENAME, json.dumps(record).encode('utf-8'))
        self.assertIs(claude_managed.load_configuration_readiness(str(self.state), enabled=True), False)
        self.assertEqual(self.manager(False).preflight().code, 'configuration_unverified')
        with mock.patch.object(server, 'HOME', str(self.home)):
            manager, failed = server.start_managed_claude()
        self.assertFalse(failed)
        self.assertFalse(manager.configuration_ready)

    def test_the_server_factory_reads_this_pcs_record(self):
        self.ready()
        with mock.patch.object(server, 'HOME', str(self.home)):
            manager, failed = server.start_managed_claude()
        self.assertFalse(failed)
        self.assertTrue(manager.configuration_ready)

    def test_a_whole_managed_start_one_message_and_stop(self):
        manager, run_id, processes = self.start()
        request_id = str(uuid.uuid4())
        manager.enqueue(run_id, request_id, 'owner', 'Fictional instruction: write the orchard notes.')

        def pending():
            return next((p for p in manager.status(run_id)['permissions'] if p['state'] == 'pending'), None)
        permission = self.wait_for(pending)
        self.assertEqual(permission['tool'], 'Write')
        self.assertTrue(permission['canAllow'])
        receipt = manager.decide(run_id, permission['id'], str(uuid.uuid4()), 'allow')
        self.assertEqual(receipt['decision'], 'allow')

        def answered():
            return next((m for m in manager.status(run_id)['messages']
                         if m['requestId'] == request_id and m['state'] == 'completed'), None)
        message = self.wait_for(answered)
        self.assertEqual(message['reply'], 'Fictional reply after allow.')
        self.assertTrue(message['nativeEcho'])
        self.assertIn(manager.status(run_id)['state'], ('ready', 'running'))

        manager.shutdown(run_id, str(uuid.uuid4()))

        def exited():
            snapshot = manager.status(run_id)
            return snapshot if snapshot['state'] == 'exited' else None
        final = self.wait_for(exited)
        self.assertTrue(final['nativeExit']['confirmed'])
        self.assertIn(final['nativeExit']['code'], (0, self.windows.JOB_ENDED_EXIT))
        self.assert_tree_gone(processes)
        self.assertEqual(self.supervisors[-1].wait(timeout=30), 0)       # the supervisor ends after the exit
        with manager._registry() as db:
            self.assertFalse(manager._row(db, run_id)['occupied'])      # its slot is released

    def test_a_supervisor_that_is_killed_takes_claude_and_all_it_started(self):
        manager, run_id, processes = self.start()
        supervisor = self.supervisors[-1]
        supervisor.kill()                                               # its job's only handle closes with it
        supervisor.wait(timeout=30)
        self.assert_tree_gone(processes)
        snapshot = manager.status(run_id)
        self.assertEqual(snapshot['state'], 'uncertain')                # never adopted, never called an exit
        self.assertTrue(snapshot['recoveryRequired'])


if __name__ == '__main__':
    unittest.main()
