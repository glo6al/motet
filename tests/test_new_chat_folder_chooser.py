"""Fictional chooser/helper fixtures only. Never activate or open a native UI."""
import base64
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

if os.name != 'nt':
    import fcntl
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import new_chat_folder_chooser as chooser
import new_chat_folder_chooser_native as native
from new_chat_service import NewChatService
from new_chat_types import NewChatError


def uid():
    return str(uuid.uuid4())


class Connection:
    def getsockname(self):
        return '127.0.0.1', 18801
    def getpeername(self):
        return '127.0.0.1', 54000


class FakeNative:
    def __init__(self):
        self.release = threading.Event()
        self.window_started, self.dead, self.stop_works = False, False, True
        self.value = {'state': 'selected', 'path': '/fictional/Caf\u00e9 Orchard'}
    def result(self):
        self.release.wait(2)
        self.dead = self.stop_works
        return self.value
    def cancel(self):
        self.dead = self.stop_works
        self.release.set()
        return self.dead
    def exited(self):
        return self.dead


class FakeChooser:
    def __init__(self):
        self.calls, self.natives = [], []
    def open(self, initial_path=None, before_spawn=None):
        if before_spawn:
            before_spawn()
        self.calls.append(initial_path)
        value = FakeNative()
        self.natives.append(value)
        return value


def saved():
    return {'schemaVersion': 1, 'folder': {'id': 'a' * 64, 'revision': 'b' * 64,
            'label': 'Orchard', 'displayPath': '/fictional/Orchard', 'registered': True}}


def settle(coordinator, ident, state):
    end = time.monotonic() + 2
    while time.monotonic() < end:
        value = coordinator.status(ident)
        if value['state'] == state:
            return value
        time.sleep(.002)
    raise AssertionError('Fictional chooser did not settle: ' + str(value))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.adapter, self.now = FakeChooser(), [100.]
        self.coordinator = chooser.FolderChooser(self.adapter, clock=lambda: self.now[0], schedule=False)
        self.addCleanup(self.coordinator.close)
        self.guard = lambda: None
    def data(self, ident=None):
        return {'requestId': ident or uid(), 'epoch': self.coordinator.epoch, 'gesture': {'trusted': True}}
    def begin(self, data=None, **kw):
        data = data or self.data()
        return data, self.coordinator.begin(data, Connection(), self.guard, **kw)
    def cancel(self, ident):
        return self.coordinator.cancel(ident, {'epoch': self.coordinator.epoch})
    def selected(self):
        data, _ = self.begin()
        self.adapter.natives[-1].release.set()
        token = settle(self.coordinator, data['requestId'], 'selected')['selectionToken']
        return data['requestId'], token

    def test_pending_ack_and_same_id_are_read_only(self):
        data, value = self.begin()
        self.assertFalse(value['windowStarted'])
        self.assertFalse(self.coordinator.status(data['requestId'])['windowStarted'])
        self.adapter.natives[-1].window_started = True
        self.assertTrue(self.coordinator.status(data['requestId'])['windowStarted'])
        self.assertTrue(self.coordinator.begin(data, Connection(), self.guard)['windowStarted'])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertNotIn('path', value)

    def test_remote_forwarded_and_untrusted_never_spawn(self):
        data = self.data()
        self.assertEqual(self.coordinator.begin(data, Connection(), self.guard, forwarded=True)['state'], 'not_local')
        data = self.data()
        data['gesture']['trusted'] = False
        self.assertEqual(self.coordinator.begin(data, Connection(), self.guard)['state'], 'not_local')
        remote = Connection()
        remote.getpeername = lambda: ('192.0.2.1', 54000)
        self.assertEqual(self.coordinator.begin(self.data(), remote, self.guard)['state'], 'not_local')
        self.assertEqual(self.adapter.calls, [])

    def test_loopback_tunnel_is_explicitly_eligible_without_os_probes(self):
        _, value = self.begin()
        self.assertEqual(value['state'], 'pending')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_unknown_cancel_is_lightweight_and_prevents_late_begin(self):
        data = self.data()
        self.assertEqual(self.cancel(data['requestId'])['state'], 'cancelled')
        self.assertEqual(len(self.coordinator.operations), 0)
        with self.assertRaises(NewChatError) as error:
            self.begin(data)
        self.assertEqual(error.exception.status, 410)
        self.assertEqual(self.adapter.calls, [])

    def test_cancel_during_proof_and_old_epoch_never_spawn(self):
        entered, release = threading.Event(), threading.Event()
        original = self.coordinator.proof.capture
        def blocked(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        self.coordinator.proof.capture = blocked
        data = self.data()
        worker = threading.Thread(target=self.begin, args=(data,))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.cancel(data['requestId'])
        release.set()
        worker.join(2)
        self.assertEqual(self.adapter.calls, [])
        for action in (lambda: self.begin({**self.data(), 'epoch': uid()}),
                       lambda: self.coordinator.cancel(uid(), {'epoch': uid()})):
            with self.assertRaises(NewChatError) as error:
                action()
            self.assertEqual(error.exception.code, 'choice_epoch_changed')

    def test_expiry_during_proof_returns_gone_without_spawning(self):
        entered, release, errors = threading.Event(), threading.Event(), []
        original = self.coordinator.proof.capture
        def blocked(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        self.coordinator.proof.capture = blocked
        def begin():
            try:
                self.begin()
            except NewChatError as error:
                errors.append(error.status)
        worker = threading.Thread(target=begin)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.now[0] += 151
        self.coordinator.expire()
        release.set()
        worker.join(2)
        self.assertEqual(errors, [410])
        self.assertEqual(self.adapter.calls, [])

    def test_new_eligible_request_replaces_pending_after_confirmed_cleanup(self):
        first, _ = self.begin()
        second, value = self.begin()
        self.assertEqual(value['state'], 'pending')
        self.assertEqual(self.coordinator.status(first['requestId'])['state'], 'cancelled')
        self.assertTrue(self.adapter.natives[0].dead)
        self.assertFalse(self.adapter.natives[1].dead)
        self.assertEqual(len(self.adapter.calls), 2)

    def test_cancelled_late_selection_cannot_accept(self):
        data, _ = self.begin()
        self.cancel(data['requestId'])
        with self.assertRaises(NewChatError):
            self.coordinator.accept(data['requestId'], {'selectionToken': uid()}, lambda _: self.fail(), self.guard)
        self.assertEqual(self.coordinator.status(data['requestId'])['state'], 'cancelled')

    def test_accept_once_and_cancel_after_commit_preserves_receipt(self):
        ident, token = self.selected()
        calls = []
        def register(data):
            calls.append(data)
            return saved()
        view = self.coordinator.accept(ident, {'selectionToken': token}, register, self.guard)
        self.assertEqual(view['state'], 'saved')
        self.assertEqual(self.coordinator.accept(ident, {'selectionToken': token}, register, self.guard), view)
        self.assertEqual(self.cancel(ident), view)
        self.assertEqual(len(calls), 1)

    def test_commit_unknown_never_replays_and_expiry_erases_payload(self):
        ident, token = self.selected()
        def lost(_):
            raise OSError('fictional lost response after commit')
        self.assertEqual(self.coordinator.accept(ident, {'selectionToken': token}, lost, self.guard)['state'], 'unknown')
        with self.assertRaises(NewChatError):
            self.coordinator.accept(ident, {'selectionToken': token}, lambda _: self.fail(), self.guard)
        row = self.coordinator.operations[ident]
        self.now[0] += 151
        self.coordinator.expire()
        self.assertTrue(all(key not in row for key in ('path', 'token', 'receipt')))
        for action in (lambda: self.begin(self.data(ident)), lambda: self.coordinator.status(ident),
                       lambda: self.coordinator.accept(ident, {'selectionToken': token}, lambda _: self.fail(), self.guard)):
            with self.assertRaises(NewChatError) as error:
                action()
            self.assertEqual(error.exception.status, 410)

    def test_one_known_id_budget_counts_payloads_retired_and_unknown_cancels(self):
        self.coordinator.maximum, self.coordinator.known_limit = 1, 3
        self.cancel(uid())
        data, _ = self.begin()
        with self.assertRaises(NewChatError):
            self.begin()
        self.now[0] += 151
        self.coordinator.expire()
        self.assertEqual(len(self.coordinator.retired), 2)
        self.cancel(uid())
        with self.assertRaises(NewChatError) as error:
            self.begin()
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(len(self.coordinator.operations) + len(self.coordinator.retired), 3)

    def test_payload_capacity_recovers_after_expiry_without_evicting_id_protection(self):
        self.coordinator.maximum = 2
        first, _ = self.begin()
        self.cancel(first['requestId'])
        second, _ = self.begin()
        self.cancel(second['requestId'])
        with self.assertRaises(NewChatError):
            self.begin()
        self.now[0] += 151
        self.coordinator.expire()
        self.assertEqual(self.begin()[1]['state'], 'pending')
        self.assertEqual(len(self.coordinator.retired), 2)

    def test_unreaped_native_keeps_lease_after_payload_expiry(self):
        data, _ = self.begin()
        self.adapter.natives[-1].stop_works = False
        self.now[0] += 151
        self.coordinator.expire()
        self.assertIn(data['requestId'], self.coordinator.retired)
        self.assertEqual(self.begin()[1]['state'], 'busy')
        self.assertEqual(len(self.adapter.calls), 1)

    def test_timeout_allows_new_id_and_capacity_refusal_is_allowlisted(self):
        data, _ = self.begin()
        self.adapter.natives[-1].value = {'state': 'failed'}
        self.adapter.natives[-1].release.set()
        settle(self.coordinator, data['requestId'], 'failed')
        ident, token = self.selected()
        def refuse(_):
            raise NewChatError('capacity', 'private details', 429)
        value = self.coordinator.accept(ident, {'selectionToken': token}, refuse, self.guard)
        self.assertEqual(value['reasonCode'], 'capacity')
        self.assertNotIn('private', json.dumps(value))

    def test_daemon_expiry_cancels_without_polling_status(self):
        adapter = FakeChooser()
        coordinator = chooser.FolderChooser(adapter, retention=.05)
        try:
            ident = uid()
            coordinator.begin({'requestId': ident, 'epoch': coordinator.epoch, 'gesture': {'trusted': True}},
                              Connection(), self.guard)
            end = time.monotonic() + 2
            while ident not in coordinator.retired and time.monotonic() < end:
                time.sleep(.005)
            self.assertIn(ident, coordinator.retired)
            self.assertTrue(adapter.natives[0].dead)
        finally:
            coordinator.close()


class ProtocolTests(unittest.TestCase):
    def test_accented_space_protocol_strict_decoding_and_no_typed_fallback(self):
        path = '/fictional/Caf\u00e9 Orchard/'
        value = b'selected:' + base64.b64encode(path.encode('utf-8')) + b'\n'
        self.assertEqual(native.decode_choice(value), {'state': 'selected', 'path': path})
        for raw in (b'selected:***', b'selected:/raw/path', b'selected:Li4=', b'private error', b'\xff'):
            with self.assertRaises((ValueError, UnicodeError)):
                native.decode_choice(raw)
        self.assertEqual(native.decode_choice(b'cancelled\n'), {'state': 'cancelled'})
        self.assertIn('activate', native.APPLE_SCRIPT)
        self.assertIn('choose folder with prompt', native.APPLE_SCRIPT)
        self.assertIn('NSUTF8StringEncoding', native.APPLE_SCRIPT)
        self.assertNotIn('NSOpenPanel', native.APPLE_SCRIPT)
        self.assertNotIn('do shell script', native.APPLE_SCRIPT)

    def test_trial_is_fake_only_and_prints_no_path(self):
        lines, checked = [], []
        chooser.trial(choose=lambda: {'state': 'selected', 'path': '/fictional/Caf\u00e9 Orchard'},
                      validate=checked.append, emit=lines.append)
        self.assertEqual(lines, ['ok'])
        self.assertEqual(len(checked), 1)
        def private(_):
            raise NewChatError('invalid_folder', 'private path must not appear', 400)
        chooser.trial(choose=lambda: {'state': 'selected', 'path': '/fictional/private'}, validate=private, emit=lines.append)
        self.assertEqual(lines[-1], 'Motet cannot use that folder.')
        self.assertNotIn('/fictional', ' '.join(lines))

    def test_forwarding_and_socket_changes_are_not_local(self):
        proof, connection = chooser.LocalityProof(), Connection()
        evidence = proof.capture(connection)
        self.assertIsNotNone(evidence)
        self.assertIsNone(proof.capture(connection, True))
        connection.getpeername = lambda: ('127.0.0.1', 54001)
        self.assertFalse(proof.verify(connection, evidence))


class FakeProcess:
    def __init__(self):
        self.pid, self.returncode = 424242, None
        self.stdout, self.stdin = io.BytesIO(), io.BytesIO()
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fictional helper', timeout)
        return self.returncode
    def kill(self):
        self.returncode = -9


@unittest.skipIf(os.name == 'nt', 'Private lease/process-group integration requires POSIX.')
class NativeTests(unittest.TestCase):
    def lease(self, root):
        path = Path(root) / 'folder-chooser.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        return path, fd, (info.st_dev, info.st_ino)

    def test_helper_death_is_not_descendant_exit_proof_or_permission_to_signal_reused_group(self):
        with tempfile.TemporaryDirectory() as root:
            path, fd, identity = self.lease(root)
            inherited = os.dup(fd)
            process, signals = FakeProcess(), []
            process.returncode = -9  # unexpected helper death, descendant owns inherited lease
            operation = chooser.NativeOperation(process, fd, path, identity, kill_group=lambda *a: signals.append(a))
            self.assertFalse(operation.exited())
            self.assertFalse(operation.cancel())
            self.assertEqual(signals, [], 'Never signal a PGID after the helper was reaped')
            os.close(inherited)
            self.assertTrue(operation.exited())

    def test_selected_frame_with_nonzero_helper_exit_is_rejected_and_mutation_fails(self):
        def scenario(operation_class):
            with tempfile.TemporaryDirectory() as root:
                path, fd, identity = self.lease(root)
                process, signals = FakeProcess(), []
                process.returncode = 1
                selected = {'state': 'selected', 'path': '/fictional/Orchard'}
                def reader(child, timeout, started):
                    started()
                    return selected
                operation = operation_class(process, fd, path, identity, reader=reader,
                                            kill_group=lambda *args: signals.append(args))
                with self.assertRaises(ValueError):
                    operation.result()
                self.assertTrue(operation.window_started)
                self.assertTrue(operation.exited())
                self.assertIsNone(operation.fd)
                self.assertEqual(signals, [], 'Do not signal the already-reaped helper group')
        scenario(chooser.NativeOperation)
        source = Path(chooser.__file__).read_text()
        guard = 'if self.process.returncode != 0:'
        self.assertIn(guard, source)
        namespace = {'__name__': 'fictional_helper_exit_mutant'}
        exec(compile(source.replace(guard, 'if False:', 1), '<fictional helper exit mutant>', 'exec'), namespace)
        with self.assertRaises(AssertionError):
            scenario(namespace['NativeOperation'])

    def test_cancel_signals_owned_group_before_reap_and_releases_only_same_inode(self):
        with tempfile.TemporaryDirectory() as root:
            path, fd, identity = self.lease(root)
            process, signals = FakeProcess(), []
            def kill_group(pid, kind):
                self.assertIsNone(process.returncode)
                signals.append((pid, kind))
                process.returncode = -15
            operation = chooser.NativeOperation(process, fd, path, identity, kill_group=kill_group)
            self.assertTrue(operation.cancel())
            self.assertEqual(signals, [(process.pid, signal.SIGTERM)])
            self.assertIsNone(operation.fd)

    def test_group_escalation_happens_before_helper_reap(self):
        with tempfile.TemporaryDirectory() as root:
            path, fd, identity = self.lease(root)
            process, signals = FakeProcess(), []
            def kill_group(pid, kind):
                self.assertIsNone(process.returncode)
                signals.append(kind)
                if kind == signal.SIGKILL:
                    process.returncode = -9
            operation = chooser.NativeOperation(process, fd, path, identity, kill_group=kill_group)
            self.assertTrue(operation.cancel())
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_helper_selected_argv_constant_script_started_and_utf8_result(self):
        with tempfile.TemporaryDirectory() as root:
            _, fd, identity = self.lease(root)
            calls, scripts, frames = [], [], []
            class Stdin(io.BytesIO):
                def close(self):
                    scripts.append(self.getvalue())
                    super().close()
            def popen(argv, **kw):
                calls.append((argv, kw))
                process = FakeProcess()
                process.stdin = Stdin()
                return process
            expected = '/fictional/Caf\u00e9 Orchard/'
            def collect(process, *args, **kw):
                process.returncode = 0
                return b'selected:' + base64.b64encode(expected.encode('utf-8')) + b'\n'
            with patch.object(native, 'collect', side_effect=collect):
                native.helper({'parent': 42, 'fd': fd, 'lease': list(identity), 'initial': ''},
                              parent=lambda: 42, popen=popen, emit=frames.append)
            self.assertEqual(calls[0][0], ['/usr/bin/osascript', '-', '', str(fd)])
            self.assertEqual(calls[0][1]['pass_fds'], (fd,))
            self.assertIs(calls[0][1]['shell'], False)
            self.assertNotIn('start_new_session', calls[0][1], 'Native child must remain in owned helper group')
            self.assertEqual(scripts, [native.APPLE_SCRIPT.encode('utf-8')])
            self.assertEqual(frames, [{'state': 'started'}, {'state': 'selected', 'path': expected}])

    def test_parent_death_during_native_choice_cancels_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as root:
            _, fd, identity = self.lease(root)
            parent, frames, process = [42], [], FakeProcess()
            def popen(*args, **kw):
                parent[0] = 43
                return process
            def collect(child, stopped, *args, **kw):
                self.assertTrue(stopped.wait(.5))
                raise TimeoutError()
            with patch.object(native, 'collect', side_effect=collect):
                native.helper({'parent': 42, 'fd': fd, 'lease': list(identity), 'initial': ''},
                              parent=lambda: parent[0], popen=popen, emit=frames.append)
            self.assertIsNotNone(process.returncode)
            self.assertEqual(frames, [{'state': 'started'}, {'state': 'cancelled'}])

    def test_spawn_isolated_python_argv_and_parent_hint_is_never_statted_in_server(self):
        with tempfile.TemporaryDirectory() as root:
            calls, process = [], FakeProcess()
            def popen(argv, **kw):
                calls.append((argv, kw))
                return process
            with patch.object(chooser.os, 'stat', side_effect=AssertionError('Server stat of hinted path')):
                operation = chooser.MacChooser(root, popen=popen).open('/fictional/mount/Orchard')
            argv, kw = calls[0]
            self.assertEqual(argv[:2], [sys.executable, '-I'])
            self.assertEqual(Path(argv[2]).name, 'new_chat_folder_chooser_native.py')
            payload = json.loads(argv[3])
            self.assertEqual(payload['initial'], '/fictional/mount/Orchard')
            self.assertEqual(kw['pass_fds'], (operation.fd,))
            self.assertIs(kw['start_new_session'], True)
            self.assertIs(kw['shell'], False)
            process.returncode = 0
            self.assertTrue(operation.exited())

    def test_started_protocol_is_ordered_bounded_and_requires_ack_for_selection(self):
        def parse(raw):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, raw)
            os.close(write_fd)
            process, starts = FakeProcess(), []
            process.stdout = os.fdopen(read_fd, 'rb')
            try:
                return chooser.read_frames(process, .2, lambda: starts.append(True)), starts
            finally:
                process.stdout.close()
        selected = {'state': 'selected', 'path': '/fictional/Orchard'}
        raw = b'{"state":"started"}\n' + json.dumps(selected).encode() + b'\n'
        self.assertEqual(parse(raw), (selected, [True]))
        for raw in (json.dumps(selected).encode() + b'\n', b'{"state":"started"}\n' * 2,
                    b'{"state":"failed"}\nextra\n'):
            with self.assertRaises(ValueError):
                parse(raw)

    def test_helper_rejects_changed_parent_before_spawn(self):
        with tempfile.TemporaryDirectory() as root:
            _, fd, identity = self.lease(root)
            frames = []
            native.helper({'parent': 42, 'fd': fd, 'lease': list(identity), 'initial': ''},
                          parent=lambda: 43, popen=lambda *a, **kw: self.fail('No native spawn'), emit=frames.append)
            self.assertEqual(frames, [{'state': 'cancelled'}])
            os.close(fd)

    def test_helper_parent_watch_and_timeout_stop_blocked_resolver_without_ui(self):
        for parent_loss in (True, False):
            with self.subTest(parent_loss=parent_loss), tempfile.TemporaryDirectory() as root:
                _, fd, identity = self.lease(root)
                process, frames, current_parent = FakeProcess(), [], [42]
                def popen(*args, **kw):
                    self.assertEqual(args[0][0], sys.executable)
                    self.assertEqual(kw['pass_fds'], (fd,))
                    if parent_loss:
                        current_parent[0] = 43
                    return process
                def collect(child, stopped, timeout, **kw):
                    self.assertTrue(stopped.wait(.5), 'Independent watchdog must stop blocked preparation')
                    raise TimeoutError()
                with patch.object(native, 'collect', side_effect=collect):
                    native.helper({'parent': 42, 'fd': fd, 'lease': list(identity), 'initial': '/fictional/mount/Orchard'},
                                  parent=lambda: current_parent[0], popen=popen, emit=frames.append, lifetime=.02)
                self.assertIsNotNone(process.returncode)
                self.assertNotIn({'state': 'started'}, frames)
                self.assertEqual(frames[-1]['state'], 'cancelled' if parent_loss else 'failed')


class CausalTests(unittest.TestCase):
    def mutant(self, old, new):
        source = Path(chooser.__file__).read_text()
        self.assertIn(old, source)
        namespace = {'__name__': 'fictional_chooser_mutant'}
        exec(compile(source.replace(old, new, 1), '<fictional chooser mutant>', 'exec'), namespace)
        return namespace['FolderChooser']

    def test_expired_id_replay_guard_mutation_is_caught(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, schedule=False)
            data = {'requestId': uid(), 'epoch': coordinator.epoch, 'gesture': {'trusted': True}}
            try:
                coordinator.cancel(data['requestId'], {'epoch': coordinator.epoch})
                with self.assertRaises(NewChatError) as error:
                    coordinator.begin(data, Connection(), lambda: None)
                self.assertEqual(error.exception.status, 410)
                self.assertEqual(adapter.calls, [])
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        with self.assertRaises(AssertionError):
            scenario(self.mutant("if data['requestId'] in self.retired:", 'if False:'))

    def test_epoch_guard_mutation_is_caught(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, schedule=False)
            try:
                data = {'requestId': uid(), 'epoch': uid(), 'gesture': {'trusted': True}}
                with self.assertRaises(NewChatError):
                    coordinator.begin(data, Connection(), lambda: None)
                self.assertEqual(adapter.calls, [])
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        with self.assertRaises(AssertionError):
            scenario(self.mutant("self._epoch(data['epoch'])", 'pass'))

    def test_single_known_id_budget_guard_mutation_is_caught(self):
        def scenario(factory):
            coordinator = factory(FakeChooser(), known_limit=2, schedule=False)
            try:
                for _ in range(2):
                    coordinator.cancel(uid(), {'epoch': coordinator.epoch})
                with self.assertRaises(NewChatError) as error:
                    coordinator.cancel(uid(), {'epoch': coordinator.epoch})
                self.assertEqual(error.exception.status, 429)
                self.assertEqual(len(coordinator.retired), 2)
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        with self.assertRaises(AssertionError):
            scenario(self.mutant('if len(self.operations) + len(self.retired) >= self.known_limit:', 'if False:'))

    def test_unknown_commit_guard_mutation_is_caught(self):
        def scenario(factory):
            adapter = FakeChooser()
            coordinator = factory(adapter, schedule=False)
            try:
                ident = uid()
                coordinator.begin({'requestId': ident, 'epoch': coordinator.epoch, 'gesture': {'trusted': True}},
                                  Connection(), lambda: None)
                adapter.natives[-1].release.set()
                token = settle(coordinator, ident, 'selected')['selectionToken']
                def lost(_):
                    raise OSError('fictional post-commit receipt loss')
                value = coordinator.accept(ident, {'selectionToken': token}, lost, lambda: None)
                self.assertEqual(value['state'], 'unknown')
                with self.assertRaises(NewChatError):
                    coordinator.accept(ident, {'selectionToken': token}, lost, lambda: None)
            finally:
                coordinator.close()
        scenario(chooser.FolderChooser)
        with self.assertRaises(AssertionError):
            scenario(self.mutant("row['state'] = 'unknown'\n            try:", "row['state'] = 'selected'\n            try:"))


@unittest.skipIf(os.name == 'nt', 'Private service storage requires POSIX flock.')
class ServiceTests(unittest.TestCase):
    def test_disabled_and_demo_chooser_actions_are_inert(self):
        for enabled, demo in ((False, False), (True, True)):
            with tempfile.TemporaryDirectory() as root:
                adapter = FakeChooser()
                coordinator = chooser.FolderChooser(adapter, schedule=False)
                service = NewChatService(root, {}, lambda: [], enabled=enabled, demo=demo, folder_chooser=coordinator)
                try:
                    context = service.context()
                    self.assertFalse(context['canChooseFolders'])
                    self.assertIsNone(context['folderChoiceEpoch'])
                    data = {'requestId': uid(), 'epoch': coordinator.epoch, 'gesture': {'trusted': True}}
                    for action in (lambda: service.begin_folder_choice(data, Connection()),
                                   lambda: service.cancel_folder_choice(data['requestId'], {'epoch': coordinator.epoch}),
                                   lambda: service.accept_folder_choice(data['requestId'], {'selectionToken': uid()}),
                                   lambda: service.folder_choice_status(data['requestId'])):
                        with self.assertRaises(NewChatError):
                            action()
                    self.assertEqual(adapter.calls, [])
                    self.assertEqual(coordinator.operations, {})
                finally:
                    service.close()

    def test_guard_context_and_internal_registration_only_on_accept(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project = root / 'Orchard'
            project.mkdir()
            adapter = FakeChooser()
            coordinator = chooser.FolderChooser(adapter, schedule=False)
            service = NewChatService(root / 'state', {}, lambda: [], enabled=True, folder_chooser=coordinator,
                                     home=str(root / 'fictional-home'))
            try:
                context = service.context()
                self.assertNotIn('canRegisterFolders', context)
                self.assertEqual(context['folderChoiceEpoch'], coordinator.epoch)
                data = {'requestId': uid(), 'epoch': coordinator.epoch, 'gesture': {'trusted': True}}
                service.begin_folder_choice(data, Connection())
                self.assertEqual(service.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 0)
                adapter.natives[-1].value['path'] = str(project)
                adapter.natives[-1].release.set()
                token = settle(coordinator, data['requestId'], 'selected')['selectionToken']
                self.assertEqual(service.accept_folder_choice(data['requestId'], {'selectionToken': token})['state'], 'saved')
                self.assertEqual(service.db.execute('SELECT count(*) FROM registered_folders').fetchone()[0], 1)
            finally:
                service.close()
            self.assertTrue(coordinator.closed)


if __name__ == '__main__':
    unittest.main()
