"""Chooser proof/lifecycle tests. No real native probe or chooser is executed."""
import copy
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

if os.name != 'nt':
    import fcntl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import new_chat_folder_chooser as chooser
from new_chat_service import NewChatService
from new_chat_types import NewChatError


def ident():
    return str(uuid.uuid4())


def begin_data(value=None):
    return {'requestId': value or ident(), 'gesture': {'visible': True, 'focused': True, 'trusted': True}}


def receipt():
    return {'schemaVersion': 1, 'folder': {'id': 'a' * 64, 'revision': 'b' * 64,
            'label': 'Orchard', 'displayPath': '/fictional/Orchard', 'registered': True}}


def settle(coordinator, value, state):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        view = coordinator.status(value)
        if view['state'] == state:
            return view
        time.sleep(.002)
    raise AssertionError('Fictional chooser did not reach ' + state + ': ' + str(view))


def mock_jxa_completion(script):
    """Execute the actual script in plain JS with bridge-shaped AppKit fakes."""
    node = shutil.which('node')
    if node is None:
        raise AssertionError('Node is required for the pure-JS native bridge fixture')
    harness = r'''
const vm=require('node:vm'), fs=require('node:fs');
const script=JSON.parse(fs.readFileSync(0,'utf8')).script, results=[];
for(const code of [1,'1',0,'0']){
  let presentations=0;
  const panel={URL:{path:'/fictional/Orchard'},
    beginWithCompletionHandler(callback){presentations++;callback(code)},cancel(){}};
  const context={ObjC:{import(){},unwrap:value=>value},$:{getppid:()=>42,
    fcntl:()=>0,flock:()=>0,NSApplication:{sharedApplication:{
      setActivationPolicy(){},activateIgnoringOtherApps(){}}},
    NSOpenPanel:{openPanel:panel},NSURL:{fileURLWithPath:path=>path}}};
  const value=vm.runInNewContext(script+'\nrun([JSON.stringify({parent:42,fd:7,initial:""})]);',context,{timeout:1000});
  results.push({value:JSON.parse(value),presentations});
}
process.stdout.write(JSON.stringify(results));
'''
    result = subprocess.run([node, '-e', harness], input=json.dumps({'script': script}),
                            text=True, capture_output=True, timeout=5, check=True)
    return json.loads(result.stdout)


class FakeProof:
    def __init__(self):
        self.allowed = True
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.verifications = 0

    def capture(self, connection):
        self.entered.set()
        self.release.wait(2)
        return ('fixture-evidence',) if self.allowed else None

    def verify(self, connection, evidence):
        self.verifications += 1
        return self.allowed and evidence == ('fixture-evidence',)


class FakeNative:
    def __init__(self):
        self.release, self.started = threading.Event(), threading.Event()
        self.value = {'state': 'selected', 'path': '/fictional/Orchard'}
        self.stopped, self.stop_works = False, True

    def result(self):
        self.started.set()
        self.release.wait(2)
        self.stopped = self.stop_works
        return self.value

    def exited(self):
        return self.stopped

    def cancel(self):
        self.stopped = self.stop_works
        self.release.set()
        return self.stopped


class FakeChooser:
    def __init__(self):
        self.calls, self.natives = [], []

    def open(self, initial_path=None, before_spawn=None):
        if before_spawn:
            before_spawn()
        self.calls.append(initial_path)
        native = FakeNative()
        self.natives.append(native)
        return native


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.adapter, self.proof = FakeChooser(), FakeProof()
        self.coordinator = chooser.FolderChooser(self.adapter, self.proof)
        self.guard = lambda: None
        self.addCleanup(self.coordinator.close)

    def selected(self):
        value = begin_data()
        self.coordinator.begin(value, object(), self.guard)
        self.adapter.natives[-1].release.set()
        return value['requestId'], settle(self.coordinator, value['requestId'], 'selected')['selectionToken']

    def test_same_id_begin_and_status_never_repeat_native_or_register(self):
        data = begin_data()
        for _ in range(3):
            self.assertEqual(self.coordinator.begin(data, object(), self.guard)['state'], 'pending')
            self.assertEqual(self.coordinator.status(data['requestId'])['state'], 'pending')
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertNotIn('path', self.coordinator.status(data['requestId']))

    def test_global_busy_slot_and_cancel_before_begin_tombstone(self):
        data = begin_data()
        self.coordinator.begin(data, object(), self.guard)
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'busy')
        late = begin_data()
        self.coordinator.cancel(late['requestId'])
        self.assertEqual(self.coordinator.begin(late, object(), self.guard)['state'], 'cancelled')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_cancel_during_slow_proof_prevents_spawn(self):
        data = begin_data()
        self.proof.release.clear()
        worker = threading.Thread(target=self.coordinator.begin, args=(data, object(), self.guard))
        worker.start()
        self.assertTrue(self.proof.entered.wait(1))
        self.coordinator.cancel(data['requestId'])
        self.proof.release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.coordinator.status(data['requestId'])['state'], 'cancelled')

    def test_late_selected_result_after_cancel_cannot_save(self):
        data = begin_data()
        self.coordinator.begin(data, object(), self.guard)
        self.coordinator.cancel(data['requestId'])
        self.assertEqual(settle(self.coordinator, data['requestId'], 'cancelled')['state'], 'cancelled')
        with self.assertRaises(NewChatError):
            self.coordinator.accept(data['requestId'], {'selectionToken': ident()}, lambda _: self.fail(), self.guard)

    def test_accept_once_and_lost_receipt_status_is_read_only(self):
        value, token = self.selected()
        calls = []
        def register(data):
            calls.append(data)
            return receipt()
        view = self.coordinator.accept(value, {'selectionToken': token}, register, self.guard)
        self.assertEqual(view['state'], 'saved')
        self.assertEqual(self.coordinator.status(value), view)
        self.assertEqual(self.coordinator.accept(value, {'selectionToken': token}, register, self.guard), view)
        self.assertEqual(self.coordinator.cancel(value), view, 'cancel cannot undo a prior commit')
        self.assertEqual(calls, [{'path': '/fictional/Orchard'}])

    def test_post_commit_exception_is_unknown_and_cannot_replay(self):
        value, token = self.selected()
        calls = []
        def register(data):
            calls.append(data)
            raise OSError('fictional response lost after commit')
        self.assertEqual(self.coordinator.accept(value, {'selectionToken': token}, register, self.guard)['state'], 'unknown')
        self.assertEqual(self.coordinator.cancel(value)['state'], 'unknown')
        for _ in range(2):
            self.assertEqual(self.coordinator.status(value)['state'], 'unknown')
            with self.assertRaises(NewChatError):
                self.coordinator.accept(value, {'selectionToken': token}, register, self.guard)
        self.assertEqual(len(calls), 1)

    def test_known_private_refusal_and_malformed_receipt(self):
        value, token = self.selected()
        def refuse(_):
            raise NewChatError('invalid_folder', 'Motet cannot use that folder.', 400)
        self.assertEqual(self.coordinator.accept(value, {'selectionToken': token}, refuse, self.guard)['state'], 'refused')
        value, token = self.selected()
        self.assertEqual(self.coordinator.accept(value, {'selectionToken': token}, lambda _: {}, self.guard)['state'], 'unknown')

    def test_capacity_is_the_only_allowlisted_refusal_reason(self):
        value, token = self.selected()
        def capacity(_):
            raise NewChatError('capacity', 'private native detail must not escape', 429)
        view = self.coordinator.accept(value, {'selectionToken': token}, capacity, self.guard)
        self.assertEqual(view['reasonCode'], 'capacity')
        self.assertNotIn('private', json.dumps(view))

    def test_false_gesture_is_not_local_without_probes(self):
        for key in ('visible', 'focused', 'trusted'):
            data = begin_data()
            data['gesture'][key] = False
            self.assertEqual(self.coordinator.begin(data, object(), self.guard)['state'], 'not_local')
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.proof.entered.is_set())

    def test_timeout_is_failed_and_native_cancel_is_cancelled(self):
        for state in ('failed', 'cancelled'):
            data = begin_data()
            self.coordinator.begin(data, object(), self.guard)
            self.adapter.natives[-1].value = {'state': state}
            self.adapter.natives[-1].release.set()
            self.assertEqual(settle(self.coordinator, data['requestId'], state)['state'], state)

    def test_changed_evidence_inside_adapter_before_spawn_is_refused(self):
        original = self.adapter.open
        def open_changed(*args, **kw):
            self.proof.allowed = False
            return original(*args, **kw)
        self.adapter.open = open_changed
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'not_local')
        self.assertEqual(self.adapter.calls, [])

    def test_expired_initiating_gesture_cannot_spawn_after_slow_proof(self):
        now = [100]
        self.coordinator.clock = lambda: now[0]
        original = self.adapter.open
        def delayed(*args, **kw):
            now[0] += 16
            return original(*args, **kw)
        self.adapter.open = delayed
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'not_local')
        self.assertEqual(self.adapter.calls, [])

    def test_close_during_proof_prevents_native_and_cannot_reopen(self):
        data = begin_data()
        self.proof.release.clear()
        worker = threading.Thread(target=self.coordinator.begin, args=(data, object(), self.guard))
        worker.start()
        self.assertTrue(self.proof.entered.wait(1))
        self.coordinator.close()
        self.proof.release.set()
        worker.join(2)
        self.assertEqual(self.adapter.calls, [])
        with self.assertRaises(NewChatError):
            self.coordinator.begin(begin_data(), object(), self.guard)

    def test_unknown_start_process_is_retained_and_quarantined(self):
        native = FakeNative()
        native.stop_works = False
        def unknown(*args, **kw):
            raise chooser.NativeStartUnknown(native)
        self.adapter.open = unknown
        data = begin_data()
        self.assertEqual(self.coordinator.begin(data, object(), self.guard)['state'], 'unknown')
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'busy')
        self.assertIs(self.coordinator.operations[data['requestId']]['native'], native)

    def test_accept_cancel_are_serialized_at_commit_boundary(self):
        value, token = self.selected()
        entered, release = threading.Event(), threading.Event()
        def register(_):
            entered.set()
            release.wait(2)
            return receipt()
        worker = threading.Thread(target=self.coordinator.accept,
                                  args=(value, {'selectionToken': token}, register, self.guard))
        worker.start()
        self.assertTrue(entered.wait(1))
        cancelled = []
        cancel = threading.Thread(target=lambda: cancelled.append(self.coordinator.cancel(value)))
        cancel.start()
        release.set()
        worker.join(2)
        cancel.join(2)
        self.assertEqual(cancelled[0]['state'], 'saved')

    def test_changed_proof_before_spawn_and_guard_refusal(self):
        self.proof.allowed = False
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'not_local')
        self.assertEqual(self.adapter.calls, [])
        self.proof.allowed = True
        def deny():
            raise NewChatError('disabled', 'disabled', 503)
        with self.assertRaises(NewChatError):
            self.coordinator.begin(begin_data(), object(), deny)
        self.assertEqual(self.adapter.calls, [])

    def test_unknown_process_exit_keeps_slot_quarantined(self):
        data = begin_data()
        self.coordinator.begin(data, object(), self.guard)
        self.adapter.natives[-1].stop_works = False
        self.adapter.natives[-1].release.set()
        settle(self.coordinator, data['requestId'], 'unknown')
        self.assertEqual(self.coordinator.begin(begin_data(), object(), self.guard)['state'], 'busy')

    def test_capacity_does_not_evict_unknown_or_selected(self):
        self.coordinator.maximum = 1
        value, token = self.selected()
        self.coordinator.clock = lambda: 10000000
        with self.assertRaises(NewChatError):
            self.coordinator.cancel(ident())
        self.assertEqual(self.coordinator.status(value)['state'], 'selected')

    def test_malformed_begin_and_tokens_never_spawn(self):
        for data in ({}, {'requestId': 'bad'}, {**begin_data(), 'path': '/fictional'},
                     {**begin_data(), 'initialPath': 'relative'},
                     {'requestId': ident(), 'gesture': {'visible': True, 'focused': True, 'trusted': 1}}):
            with self.assertRaises(NewChatError):
                self.coordinator.begin(data, object(), self.guard)
        self.assertEqual(self.adapter.calls, [])


class Connection:
    def getsockname(self):
        return '127.0.0.1', 18801

    def getpeername(self):
        return '127.0.0.1', 54000


class ProbeRunner:
    def __init__(self):
        self.gui = {'pid': 100, 'idle': .1, 'bundle': 'com.google.Chrome'}
        self.root = {'IOConsoleLocked': False, 'IOConsoleUsers': [{
            'kCGSSessionUserIDKey': 501, 'kCGSSessionOnConsoleKey': True,
            'kCGSessionLoginDoneKey': True}]}
        self.lsof = 'p200\nu501\nf10\ntIPv4\nn127.0.0.1:54000->127.0.0.1:18801\np999\nu501\nf8\nn127.0.0.1:18801->127.0.0.1:54000\n'
        self.ps = {200: '200 100 501 Thu Nov 19 12:00:00 2026',
                   100: '100 1 501 Thu Nov 19 11:00:00 2026'}
        self.calls = []

    def __call__(self, argv, script=None, maximum=16384):
        self.calls.append((argv, script, maximum))
        if argv[0] == '/usr/sbin/lsof':
            return self.lsof.encode()
        if argv[0] == '/usr/bin/osascript':
            self.assert_no_ui(script)
            return json.dumps(self.gui).encode()
        if argv[0] == '/usr/sbin/ioreg':
            return plistlib.dumps(self.root)
        if argv[0] == '/bin/ps':
            return self.ps.get(int(argv[3]), '').encode()
        raise AssertionError('Unapproved fictional probe')

    @staticmethod
    def assert_no_ui(script):
        assert 'NSOpenPanel' not in script and "Application('System Events')" not in script


class LocalityTests(unittest.TestCase):
    def setUp(self):
        self.runner = ProbeRunner()
        self.proof = chooser.LocalityProof(self.runner, uid=lambda: 501, platform='darwin')
        self.connection = Connection()

    def test_exact_client_socket_ancestry_and_fresh_identity(self):
        evidence = self.proof.capture(self.connection)
        self.assertIsNotNone(evidence)
        self.assertTrue(self.proof.verify(self.connection, evidence))
        self.runner.ps[200] = '200 100 501 Thu Nov 19 12:00:01 2026'
        self.assertFalse(self.proof.verify(self.connection, evidence))
        self.assertTrue(all(c[0][0].startswith('/') for c in self.runner.calls))

    def test_os_browser_bundle_is_required_and_bound_to_reverification(self):
        for bundle in chooser.BROWSER_BUNDLE_IDS:
            self.runner.gui['bundle'] = bundle
            self.assertIsNotNone(self.proof.capture(self.connection), bundle)
        self.runner.gui['bundle'] = 'com.google.Chrome'
        evidence = self.proof.capture(self.connection)
        self.runner.gui['bundle'] = 'org.mozilla.firefox'
        self.assertFalse(self.proof.verify(self.connection, evidence), 'Even another allowed browser changes the evidence')
        for bundle in ('com.apple.Terminal', 'com.example.UnknownBrowser', '', None):
            self.runner.gui['bundle'] = bundle
            self.assertIsNone(self.proof.capture(self.connection))
        del self.runner.gui['bundle']
        self.assertIsNone(self.proof.capture(self.connection))

    def test_ssh_proxy_unknown_daemon_or_cycles_fail_closed(self):
        for parent in (1, 300, 200):
            with self.subTest(parent=parent):
                self.runner.ps[200] = '200 %d 501 Thu Nov 19 12:00:00 2026' % parent
                self.assertIsNone(self.proof.capture(self.connection))

    def test_ambiguous_or_foreign_socket_owner_fails_closed(self):
        for suffix in ('p201\nu501\nf3\nn127.0.0.1:54000->127.0.0.1:18801\n',):
            self.runner.lsof += suffix
            self.assertIsNone(self.proof.capture(self.connection))
        self.runner.lsof = ProbeRunner().lsof.replace('u501', 'u502')
        self.assertIsNone(self.proof.capture(self.connection))

    def test_locked_unknown_switched_console_or_stale_input_fail_closed(self):
        baseline = copy.deepcopy(self.runner.root)
        for locked in (True, None, 0):
            self.runner.root = {**baseline, 'IOConsoleLocked': locked}
            self.assertIsNone(self.proof.capture(self.connection))
        for key, value in (('kCGSSessionUserIDKey', 502), ('kCGSSessionOnConsoleKey', False),
                           ('kCGSessionLoginDoneKey', False)):
            self.runner.root = copy.deepcopy(baseline)
            self.runner.root['IOConsoleUsers'][0][key] = value
            self.assertIsNone(self.proof.capture(self.connection))
        self.runner.root = baseline
        for idle in (16, -1, float('nan'), True):
            self.runner.gui['idle'] = idle
            self.assertIsNone(self.proof.capture(self.connection))

    def test_missing_probe_remote_socket_platform_and_changed_frontmost_fail_closed(self):
        evidence = self.proof.capture(self.connection)
        self.runner.gui['pid'] = 300
        self.assertFalse(self.proof.verify(self.connection, evidence))
        self.proof.platform = 'linux'
        self.assertIsNone(self.proof.capture(self.connection))
        self.proof.platform = 'darwin'
        self.connection.getpeername = lambda: ('192.0.2.1', 54000)
        self.assertIsNone(self.proof.capture(self.connection))
        self.proof.run = lambda *a, **kw: (_ for _ in ()).throw(PermissionError())
        self.assertIsNone(self.proof.capture(Connection()))


class FakeProcess:
    def __init__(self):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.returncode, self.kills = None, 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@unittest.skipIf(os.name == 'nt', 'Native chooser adapter requires POSIX descriptors and flock.')
class NativeAdapterTests(unittest.TestCase):
    def test_argv_data_fixed_script_lock_inheritance_and_parent_watch(self):
        with tempfile.TemporaryDirectory() as root:
            calls, scripts = [], []
            class Stdin(io.BytesIO):
                def close(self):
                    scripts.append(self.getvalue())
                    super().close()
            def popen(argv, **kw):
                calls.append((argv, kw))
                process = FakeProcess()
                process.stdin = Stdin()
                return process
            adapter = chooser.MacChooser(root, popen=popen)
            operation = adapter.open('/fictional/missing/Orchard')
            argv, kw = calls[0]
            self.assertEqual(argv[:4], ['/usr/bin/osascript', '-l', 'JavaScript', '-'])
            payload = json.loads(argv[4])
            self.assertEqual(payload['parent'], os.getpid())
            self.assertEqual(payload['initial'], '')
            self.assertEqual(kw['pass_fds'], (operation.fd,))
            self.assertIs(kw['shell'], False)
            self.assertEqual(scripts, [chooser.CHOOSER_SCRIPT.encode()])
            self.assertLess(chooser.CHOOSER_SCRIPT.index('$.getppid() !== v.parent'), chooser.CHOOSER_SCRIPT.index('NSOpenPanel'))
            self.assertIn('$.fcntl(v.fd, 1) < 0 || $.flock(v.fd, 6) !== 0', chooser.CHOOSER_SCRIPT)
            self.assertIn('NSRunLoop.currentRunLoop', chooser.CHOOSER_SCRIPT)
            self.assertIn('Date.now() >= end', chooser.CHOOSER_SCRIPT)
            second = os.open(Path(root) / 'folder-chooser.lock', os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(operation.cancel())
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(second)

    def test_parent_hint_only_and_prespawn_proof_gate(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = chooser.MacChooser(root, popen=lambda *a, **kw: self.fail('No spawn after proof refusal'))
            with self.assertRaises(ValueError):
                adapter.open(str(Path(root) / 'old-folder'), before_spawn=lambda: (_ for _ in ()).throw(ValueError()))
            fd = os.open(Path(root) / 'folder-chooser.lock', os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_malformed_native_output_and_timeout_reap_owned_process(self):
        for raw in (b'not json', b'{"state":"selected","path":"relative"}', b'{"state":"cancelled","path":"secret"}'):
            process = FakeProcess()
            operation = chooser.NativeOperation(process, None, reader=lambda *a, raw=raw: raw)
            with self.assertRaises(ValueError):
                operation.result()
            self.assertEqual(process.kills, 1)
        process = FakeProcess()
        operation = chooser.NativeOperation(process, None, reader=lambda *a: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(TimeoutError):
            operation.result()
        self.assertEqual(process.kills, 1)

    def test_stream_limit_uses_real_pipe_but_no_native_process(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'x' * 101)
        os.close(write_fd)
        process = FakeProcess()
        process.stdout = os.fdopen(read_fd, 'rb')
        try:
            with self.assertRaises(ValueError):
                chooser._read(process, .5, maximum=100)
        finally:
            process.stdout.close()

    def test_private_lock_refuses_symlink_and_unsafe_modes_without_spawn(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'folder-chooser.lock'
            path.write_text('')
            path.chmod(0o644)
            adapter = chooser.MacChooser(root, popen=lambda *a, **kw: self.fail('Unsafe lock spawned'))
            with self.assertRaises(ValueError):
                adapter.open()
            path.unlink()
            target = Path(root) / 'target'
            target.touch(mode=0o600)
            path.symlink_to(target)
            with self.assertRaises(OSError):
                adapter.open()

    def test_bounded_read_timeout_terminates_without_a_native_command(self):
        read_fd, write_fd = os.pipe()
        process = FakeProcess()
        process.stdout = os.fdopen(read_fd, 'rb')
        try:
            with self.assertRaises(TimeoutError):
                chooser._read(process, .01)
        finally:
            os.close(write_fd)
            process.stdout.close()


class CausalTests(unittest.TestCase):
    def factory(self, old, new, name='FolderChooser'):
        source = Path(chooser.__file__).read_text()
        self.assertIn(old, source)
        namespace = {'__name__': 'fictional_chooser_mutant'}
        exec(compile(source.replace(old, new, 1), '<fictional chooser mutant>', 'exec'), namespace)
        return namespace[name]

    def test_final_verification_cannot_extend_gesture_deadline(self):
        def scenario(factory):
            now, adapter, proof = [100], FakeChooser(), FakeProof()
            original = proof.verify
            def slow_final_verify(connection, evidence):
                answer = original(connection, evidence)
                if proof.verifications == 2:
                    now[0] += 16
                return answer
            proof.verify = slow_final_verify
            coordinator = factory(adapter, proof, clock=lambda: now[0])
            try:
                view = coordinator.begin(begin_data(), Connection(), lambda: None)
                self.assertEqual(view['state'], 'not_local')
                self.assertEqual(proof.verifications, 2)
                self.assertEqual(adapter.calls, [], 'Expired final proof must never spawn')
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        mutant = self.factory("if not 0 <= self.clock() - row['at'] <= 15:", 'if False:')
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_terminal_proxy_ancestry_is_not_browser_locality(self):
        def scenario(factory):
            runner, adapter = ProbeRunner(), FakeChooser()
            runner.gui['bundle'] = 'com.apple.Terminal'
            proof = factory(runner, uid=lambda: 501, platform='darwin')
            coordinator = chooser.FolderChooser(adapter, proof)
            try:
                view = coordinator.begin(begin_data(), Connection(), lambda: None)
                self.assertEqual(view['state'], 'not_local')
                self.assertEqual(adapter.calls, [], 'Terminal-owned matching proxy ancestry cannot open a chooser')
                runner.gui['bundle'] = 'com.google.Chrome'
                self.assertEqual(coordinator.begin(begin_data(), Connection(), lambda: None)['state'], 'pending')
                self.assertEqual(len(adapter.calls), 1, 'Same socket ancestry with browser identity remains eligible')
            finally:
                coordinator.close()
        scenario(chooser.LocalityProof)
        mutant = self.factory("gui['bundle'] not in BROWSER_BUNDLE_IDS", 'False', 'LocalityProof')
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_actual_jxa_script_accepts_numeric_and_string_modal_codes(self):
        def scenario(script):
            results = mock_jxa_completion(script)
            selected = {'state': 'selected', 'path': '/fictional/Orchard'}
            cancelled = {'state': 'cancelled'}
            self.assertEqual([row['value'] for row in results], [selected, selected, cancelled, cancelled])
            self.assertTrue(all(row['presentations'] == 1 for row in results))
        scenario(chooser.CHOOSER_SCRIPT)
        mutant = self.factory('Number(code) === 1', 'code === 1', 'CHOOSER_SCRIPT')
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_realistic_registry_size_and_hard_limit_with_bounded_runner(self):
        def scenario(factory):
            runner = ProbeRunner()
            runner.root['fixturePadding'] = 'x' * 200000
            def bounded(argv, script=None, maximum=16384):
                raw = runner(argv, script, maximum)
                if len(raw) > maximum:
                    raise ValueError('Fictional probe exceeded its output bound')
                return raw
            proof = factory(bounded, uid=lambda: 501, platform='darwin')
            self.assertIsNotNone(proof.capture(Connection()), 'Ordinary 200 KB registry must be accepted')
            registry_calls = [call for call in runner.calls if call[0][0] == '/usr/sbin/ioreg']
            self.assertEqual(registry_calls[0][2], 1024 * 1024)
            self.assertTrue(all(call[2] == 16384 for call in runner.calls if call[0][0] != '/usr/sbin/ioreg'))
            runner.root['fixturePadding'] = 'x' * (1024 * 1024)
            self.assertIsNone(proof.capture(Connection()), 'Registry above 1 MiB must fail closed')
            runner.root['fixturePadding'] = 'x' * 200000
            runner.root['IOConsoleLocked'] = True
            self.assertIsNone(proof.capture(Connection()), 'Larger limit cannot weaken explicit unlocked proof')
        scenario(chooser.LocalityProof)
        mutant = self.factory('maximum=IOREG_OUTPUT_MAX', 'maximum=65536', 'LocalityProof')
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_duplicate_native_mutation_is_caught_by_real_begin_behavior(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, FakeProof())
            try:
                data = begin_data()
                coordinator.begin(data, Connection(), lambda: None)
                self.assertEqual(coordinator.begin(data, Connection(), lambda: None)['state'], 'pending')
                self.assertEqual(len(adapter.calls), 1)
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        mutant = self.factory("if data['requestId'] in self.operations:", "if False:")
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_cancel_tombstone_mutation_cannot_start_a_late_begin(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, FakeProof())
            try:
                data = begin_data()
                coordinator.cancel(data['requestId'])
                self.assertEqual(coordinator.begin(data, Connection(), lambda: None)['state'], 'cancelled')
                self.assertEqual(adapter.calls, [])
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        mutant = self.factory("row = self._reserve(ident, 'cancelled')", "return {'schemaVersion':1,'requestId':ident,'state':'cancelled'}")
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_unknown_commit_mutation_cannot_allow_a_second_registration(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, FakeProof())
            try:
                data = begin_data()
                coordinator.begin(data, Connection(), lambda: None)
                adapter.natives[-1].release.set()
                token = settle(coordinator, data['requestId'], 'selected')['selectionToken']
                def lost(_):
                    raise OSError()
                self.assertEqual(coordinator.accept(data['requestId'], {'selectionToken': token}, lost, lambda: None)['state'], 'unknown')
                with self.assertRaises(NewChatError):
                    coordinator.accept(data['requestId'], {'selectionToken': token}, lost, lambda: None)
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        mutant = self.factory("row['state'] = 'unknown'\n            try:", "row['state'] = 'selected'\n            try:")
        with self.assertRaises(AssertionError):
            scenario(mutant)

    def test_lock_state_mutation_is_caught_by_actual_proof(self):
        def scenario(factory):
            runner = ProbeRunner()
            runner.root['IOConsoleLocked'] = True
            self.assertIsNone(factory(runner, uid=lambda: 501, platform='darwin').capture(Connection()))
        scenario(chooser.LocalityProof)
        mutant = self.factory("registry.get('IOConsoleLocked') is not False", 'False', 'LocalityProof')
        with self.assertRaises(AssertionError):
            scenario(mutant)


@unittest.skipIf(os.name == 'nt', 'New Chat private service storage requires POSIX flock.')
class ServiceGuardTests(unittest.TestCase):
    def test_disabled_demo_and_missing_injection_are_inert(self):
        for enabled, demo in ((False, False), (True, True)):
            with tempfile.TemporaryDirectory() as root:
                adapter, proof = FakeChooser(), FakeProof()
                coordinator = chooser.FolderChooser(adapter, proof)
                service = NewChatService(root, {}, lambda: [], enabled=enabled, demo=demo, folder_chooser=coordinator)
                try:
                    self.assertFalse(service.context()['canChooseFolders'])
                    for action in (lambda: service.begin_folder_choice(begin_data(), Connection()),
                                   lambda: service.folder_choice_status(ident()),
                                   lambda: service.cancel_folder_choice(ident(), {}),
                                   lambda: service.accept_folder_choice(ident(), {'selectionToken': ident()})):
                        with self.assertRaises(NewChatError):
                            action()
                    self.assertEqual(adapter.calls, [])
                    self.assertFalse(proof.entered.is_set())
                finally:
                    service.close()
        with tempfile.TemporaryDirectory() as root:
            service = NewChatService(root, {}, lambda: [], enabled=True)
            try:
                self.assertFalse(service.context()['canChooseFolders'])
            finally:
                service.close()

    def test_real_registration_engine_only_runs_on_accept_and_close_reaps(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project = root / 'Orchard'
            project.mkdir()
            adapter = FakeChooser()
            coordinator = chooser.FolderChooser(adapter, FakeProof())
            service = NewChatService(root / 'state', {}, lambda: [], enabled=True, folder_chooser=coordinator,
                                     home=str(root / 'fictional-home'))
            try:
                data = begin_data()
                service.begin_folder_choice(data, Connection())
                self.assertEqual(service.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 0)
                adapter.natives[-1].value['path'] = str(project)
                adapter.natives[-1].release.set()
                token = settle(coordinator, data['requestId'], 'selected')['selectionToken']
                view = service.accept_folder_choice(data['requestId'], {'selectionToken': token})
                self.assertEqual(view['state'], 'saved')
                self.assertEqual(service.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
                service.accept_folder_choice(data['requestId'], {'selectionToken': token})
                self.assertEqual(service.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
            finally:
                service.close()
            self.assertTrue(coordinator.closed)


if __name__ == '__main__':
    unittest.main()
