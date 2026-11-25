"""Synthetic manager tests: no native CLI, provider mutation, or app activity."""
import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_managed as managed
from new_chat_types import FrozenFolder, VerifiedIdentity


def uid():
    return str(uuid.uuid4())


class Directory:
    def __init__(self, source):
        self.source = source
        self.current = True
        self.wrong = False
        self.calls = 0

    def refresh(self, force=False):
        self.calls += 1

    def claude_metadata(self, agent, folder):
        st = self.source.stat()
        return dict(agent=agent, project='/fictional/wrong' if self.wrong else folder,
                    identity=dict(path=str(self.source), device=st.st_dev, inode=st.st_ino))

    def claude_proof_current(self, proof):
        return self.current


class ConfigurationReadinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.state = self.home / '.live-room'
        self.state.mkdir(mode=0o700)
        self.path = self.state / managed.READINESS_FILENAME
        self.enter_patch(patch.object(sys, 'platform', 'darwin'))
        self.probe = self.enter_patch(patch.object(managed.subprocess, 'run', return_value=self.hardware()))
        self.record = managed.local_configuration_identity()
        self.probe.reset_mock()

    def enter_patch(self, active_patch):
        value = active_patch.start()
        self.addCleanup(active_patch.stop)
        return value

    @staticmethod
    def hardware(machine='4fc7133b-e36a-47a4-94bd-70f50bc5d426'):
        return subprocess.CompletedProcess([], 0, plistlib.dumps([{'IOPlatformUUID': machine}]))

    def write(self, record):
        self.path.write_text(record if isinstance(record, str) else json.dumps(record), encoding='utf-8')
        self.path.chmod(0o600)

    def load(self):
        return managed.load_configuration_readiness(self.state, enabled=True)

    def test_startup_requires_a_private_local_record_with_the_current_build_pin(self):
        self.assertFalse(self.load())
        self.probe.assert_not_called()
        self.write(self.record)
        self.assertTrue(self.load())
        self.probe.assert_called_once()
        self.probe.return_value = self.hardware('94a0e037-bae3-448f-93ca-bab884c6e5e8')
        self.assertFalse(self.load())
        self.probe.return_value = self.hardware()
        for attribute, value in (('VERSION', 'fictional-next'), ('PINNED_SHA256', '0' * 64)):
            with self.subTest(attribute=attribute), patch.object(managed, attribute, value):
                self.assertFalse(self.load())

    def test_untrusted_file_cannot_enable_readiness(self):
        self.write(self.record)
        self.path.chmod(0o640)
        self.assertFalse(self.load())
        self.path.chmod(0o600)
        with patch.object(os, 'getuid', return_value=os.getuid() + 1):
            self.assertFalse(self.load())
        alias = self.state / 'linked-record'
        os.link(self.path, alias)
        self.assertFalse(self.load())
        alias.unlink()
        self.path.rename(alias)
        self.path.symlink_to(alias.name)
        self.assertFalse(self.load())
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assertFalse(self.load())
        self.path.unlink()
        alias.rename(self.path)
        self.state.chmod(0o750)
        self.assertFalse(self.load())
        self.state.chmod(0o700)
        physical = self.state.with_name('fictional-state')
        self.state.rename(physical)
        self.state.symlink_to(physical.name, target_is_directory=True)
        self.assertFalse(self.load())
        self.probe.assert_not_called()

    def test_malformed_oversized_and_unavailable_host_records_are_refused(self):
        for bad in ('{', ' ' * 1025, [self.record], dict(self.record, schemaVersion=True),
                    dict(self.record, approved=True), dict(self.record, hostFingerprint='foreign'),
                    json.dumps(self.record)[:-1] + ', "schemaVersion": 1}'):
            with self.subTest(bad=bad):
                self.write(bad)
                self.assertFalse(self.load())
        self.write(self.record)
        for output in (b'invalid plist', b'<?xml version="1.0"?><plist><array>',
                       plistlib.dumps([{'IOPlatformUUID': 'not-a-uuid'}])):
            with self.subTest(output=output):
                self.probe.return_value = subprocess.CompletedProcess([], 0, output)
                self.assertFalse(self.load())
        self.probe.side_effect = subprocess.TimeoutExpired('fictional-platform-probe', 2)
        self.assertFalse(self.load())

    def test_replaced_record_is_refused_during_hardware_lookup(self):
        self.write(self.record)

        def replace_record(*_args, **_kwargs):
            self.path.unlink()
            self.write(self.record)
            return self.hardware()

        self.probe.side_effect = replace_record
        self.assertFalse(self.load())

    def test_disabled_demo_and_other_platforms_skip_the_record(self):
        with patch.object(os, 'open', side_effect=AssertionError('readiness read')):
            self.assertFalse(managed.load_configuration_readiness(self.state))
            self.assertFalse(managed.load_configuration_readiness(self.state, enabled=True, demo=True))
            with patch.object(sys, 'platform', 'linux'):
                self.assertFalse(self.load())
        self.probe.assert_not_called()

    def test_server_factory_honors_record_and_keeps_owner_fence_without_it(self):
        import server
        binary = self.home / '.local/share/claude/versions' / managed.VERSION
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'fictional executable')
        binary.chmod(0o700)
        proof_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        with patch.multiple(server, HOME=str(self.home), CLAUDE_MANAGED_ENABLED=True), \
                patch.dict(os.environ, WATCH_DEMO='0'), patch.object(managed, 'PINNED_SHA256', proof_digest), \
                patch.object(managed, 'spawn_runner', side_effect=AssertionError('native launch')):
            manager, failed = server.start_managed_claude()
            self.assertFalse(failed)
            self.assertEqual(manager.preflight().code, 'configuration_unverified')
            with patch.multiple(server, CLAUDE_MANAGED=manager, CLAUDE_MANAGED_FAILED=failed):
                self.assertEqual(server.claude_permission_ownership('c:legacy-id'),
                                 dict(classification='unmanaged', checkRequired=False))
            marker = self.state / 'managed-claude.expected'
            marker.write_text('1\n')
            marker.chmod(0o600)
            with patch.multiple(server, CLAUDE_MANAGED=manager, CLAUDE_MANAGED_FAILED=failed):
                self.assertEqual(server.claude_permission_ownership('c:' + uid()),
                                 dict(classification='unknown', checkRequired=True))
            marker.unlink()
            self.write(managed.local_configuration_identity())
            manager, failed = server.start_managed_claude()
            self.assertFalse(failed)
            self.assertTrue(manager.preflight().available)
            with patch.multiple(server, CLAUDE_MANAGED=manager, CLAUDE_MANAGED_FAILED=failed):
                self.assertEqual(server.claude_permission_ownership('c:legacy-id'),
                                 dict(classification='unknown', checkRequired=True))
            binary.write_bytes(b'changed executable')
            self.assertEqual(manager.preflight().code, 'app_version_unsupported')

    def test_enabled_switch_preserves_legacy_permissions_on_clean_unsupported_host(self):
        import server
        with patch.object(sys, 'platform', 'linux'), \
                patch.multiple(server, HOME=str(self.home), CLAUDE_MANAGED_ENABLED=True,
                               CLAUDE_MANAGED=None, CLAUDE_MANAGED_FAILED=False, MESSAGING=None):
            self.assertEqual(server.start_managed_claude(), (None, False))
            self.assertFalse(server.claude_permission_ownership_check_required())
            self.assertEqual(server.claude_permission_ownership('c:legacy-id'),
                             dict(classification='unmanaged', checkRequired=False))
            self.assertTrue(server.managed_source_allowed('fictional.jsonl', server.ClaudeTail))
            self.state.joinpath('managed-claude.expected').write_text('1\n')
            self.assertTrue(server.claude_permission_ownership_check_required())
            self.assertEqual(server.claude_permission_ownership('c:legacy-id'),
                             dict(classification='unknown', checkRequired=True))
            self.assertFalse(server.managed_source_allowed('fictional.jsonl', server.ClaudeTail))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'orchard'
        self.project.mkdir()
        st = self.project.stat()
        self.folder = FrozenFolder('orchard', 'revision', 'Orchard', str(self.project), st.st_dev, st.st_ino)
        self.binary = self.root / 'fictional-cli'
        self.binary.write_bytes(b'fictional executable fixture only')
        self.binary.chmod(0o700)
        self.source = self.root / 'native-transcript.jsonl'
        self.source.write_text('{}\n')
        self.directory = Directory(self.source)
        self.snapshots = {}
        self.spawns = []
        self.commands = []
        self.drop = False
        self.disconnected = False
        self.echo = False
        self.dependencies = managed.TestDependencies(
            dict(path=str(self.binary), version=managed.VERSION,
                 sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest()),
            self.spawn, self.request, self.read_snapshot, self.directory, launch_timeout=.001)
        self.manager = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kwargs):
        return managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                     _test_dependencies=self.dependencies, **kwargs)

    def spawn(self, path):
        self.spawns.append(str(path))
        manifest = json.loads((path / 'manifest.json').read_text())
        self.snapshots[manifest['runId']] = dict(
            runId=manifest['runId'], nativeId=manifest['nativeId'], incarnation=manifest['incarnation'],
            cwd=manifest['folder']['path'], state='ready', nativeExit=None,
            identityVerified=True, messages=[], permissions=[])

    def request(self, path, command, params):
        self.commands.append((path.name, command, copy.deepcopy(params)))
        if self.disconnected:
            raise OSError('private runtime error must not escape')
        snapshot = self.snapshots[path.name]
        if command == 'enqueue' and params.get('source') in getattr(self, 'refused_sources', ()):
            managed._fail('runner_refused', 400)      # an older supervisor refusing a source it does not know
        if command == 'enqueue':
            message = dict(params, digest=hashlib.sha256(params['text'].encode()).hexdigest(),
                           state='delivered' if self.echo else 'queued', nativeEcho=self.echo,
                           reply='', error='', createdAt=1_900_000_000)
            snapshot['messages'].append(message)
            if self.drop:
                raise OSError('lost response')
            return copy.deepcopy(message)
        if command == 'decision':
            return dict(params, state='decided', token='must not escape')
        return copy.deepcopy(snapshot)

    def read_snapshot(self, path):
        return copy.deepcopy(self.snapshots[path.name])

    def start(self):
        run_id = self.manager.reserve(self.folder, uid())
        receipts = []
        native = self.manager.launch_once(run_id, self.folder, lambda *args: receipts.append(args))
        self.assertEqual(receipts, [(native, self.folder.path)])
        return run_id, native

    def admit(self):
        run_id, native = self.start()
        proof = self.manager.verify_existing(native, self.folder)
        self.assertIsNotNone(proof)
        self.manager.mark_admitted(proof)
        return run_id, native

    def test_default_disabled_demo_and_configuration_gate_do_no_activity(self):
        for opts in ({}, {'enabled': True, 'demo': True}, {'enabled': True}):
            manager = managed.ManagedClaude(self.root / 'absent', _test_dependencies=self.dependencies, **opts)
            self.assertFalse(manager.preflight().available)
            self.assertFalse(manager.any_ready())
            with self.assertRaises(managed.ManagedError):
                manager.status(uid())
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.directory.calls, 0)
        self.assertFalse((self.root / 'absent').exists())

    def test_retained_ownership_check_does_not_launch_or_depend_on_readiness(self):
        self.assertFalse(self.manager.has_retained_runs())
        self.assertFalse(self.manager.root.exists())
        run = self.manager.reserve(self.folder, uid())
        self.manager.enabled = False
        self.manager.closed = True
        self.assertTrue(self.manager.has_retained_runs())
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.directory.calls, 0)
        self.assertTrue(run)

    def test_capability_work_only_and_constructor_has_no_side_effects(self):
        self.assertFalse(self.manager.root.exists())
        cap = self.manager.preflight()
        self.assertTrue(cap.available)
        self.assertFalse(cap.supports_blank)
        self.assertEqual(cap.ownership, 'watch_managed')
        self.assertFalse(self.manager.root.exists())

    def test_reserve_replay_and_conflicting_binding(self):
        attempt = uid()
        run_id = self.manager.reserve(self.folder, attempt)
        self.assertEqual(self.manager.reserve(self.folder, attempt), run_id)
        with self.assertRaises(managed.ManagedError) as raised:
            self.manager.reserve(replace(self.folder, revision='changed'), attempt)
        self.assertEqual(raised.exception.code, 'request_conflict')
        self.assertEqual(self.spawns, [])

    def test_atomic_capacity_across_manager_instances(self):
        results = []
        barrier = threading.Barrier(8)
        def reserve():
            manager = self.make()
            barrier.wait()
            try:
                results.append(manager.reserve(self.folder, uid()))
            except managed.ManagedError:
                results.append(None)
        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sum(bool(value) for value in results), 4)
        self.assertEqual(len(results), 8)
        self.assertEqual(self.spawns, [])

    def test_a_launch_that_sees_an_exit_never_unsettles_the_released_slot(self):
        """Another caller's status() records Claude's confirmed exit first and releases the slot; the launch loop then
        sees the exit too. Its uncertainty must not reopen the released row (an unoccupied row that is not 'exited'
        fails registry validation, and every later registry open with it)."""
        run_id = self.manager.reserve(self.folder, str(uuid.uuid4()))
        spawn, request, first = self.spawn, self.request, []

        def exiting(path):
            spawn(path)
            snapshot = self.snapshots[path.name]
            snapshot.update(state='exited', identityVerified=False,
                            nativeExit=dict(confirmed=True, code=0, incarnation=snapshot['incarnation']))

        def status_first(path, command, params):
            if command == 'status' and not first:
                first.append(path.name)
                self.manager.status(path.name)          # the other caller, first
            return request(path, command, params)
        self.manager._test = replace(self.dependencies, spawn=exiting, request=status_first)
        with self.assertRaises(managed.ManagedError) as raised:
            self.manager.launch_once(run_id, self.folder, lambda *_: None)
        self.assertEqual(raised.exception.code, 'launch_outcome_unknown')
        with self.manager._registry() as db:              # still valid
            row = self.manager._row(db, run_id)
        self.assertEqual((row['state'], row['occupied']), ('exited', False))
        self.assertTrue(self.manager.reserve(self.folder, str(uuid.uuid4())))

    def test_private_manifest_separate_native_identity_and_launch_once(self):
        run_id, native = self.start()
        self.assertNotEqual(run_id, native)
        self.manager.launch_once(run_id, self.folder, lambda *_: None)
        self.assertEqual(len(self.spawns), 1)
        directory = self.manager.root / run_id
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ('manifest.json', 'token', 'run.lock'):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertEqual(manifest['profile'], 'restricted_test')
        self.assertEqual(len((directory / 'token').read_text()), 64)

    def test_detached_delayed_endpoint_gets_startup_grace_without_second_spawn(self):
        ready = threading.Event()
        launches = []
        def delayed_spawn(path):
            launches.append(str(path))
            def later():
                ready.wait(.04)
                # Fixture spawn publishes the endpoint only after the detached
                # launch call has returned, just as a real runner does.
                self.spawn(path)
            threading.Thread(target=later).start()
        def request(path, command, params):
            if path.name not in self.snapshots:
                raise FileNotFoundError('endpoint not bound yet')
            return self.request(path, command, params)
        self.manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
            _test_dependencies=replace(self.dependencies, spawn=delayed_spawn, request=request, launch_timeout=.5))
        run_id, native = self.start()
        self.assertEqual(self.manager.ownership(native)['state'], 'ready')
        self.assertEqual(len(launches), 1)
        self.assertEqual(len(self.spawns), 1)

    def test_concurrent_status_before_endpoint_preserves_persisted_startup_grace(self):
        spawned, release = threading.Event(), threading.Event()
        launches, failures, receipts = [], [], []
        def delayed_spawn(path):
            launches.append(str(path))
            spawned.set()
            def publish():
                release.wait(2)
                self.spawn(path)
            threading.Thread(target=publish).start()
        def request(path, command, params):
            if path.name not in self.snapshots:
                raise FileNotFoundError('endpoint not bound yet')
            return self.request(path, command, params)
        dependencies = replace(self.dependencies, spawn=delayed_spawn, request=request, launch_timeout=1)
        self.manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                             _test_dependencies=dependencies)
        run_id = self.manager.reserve(self.folder, uid())
        def launch():
            try:
                self.manager.launch_once(run_id, self.folder, lambda *args: receipts.append(args))
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=launch)
        worker.start()
        self.assertTrue(spawned.wait(2))
        try:
            other = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                           _test_dependencies=dependencies)
            for manager in (self.manager, other):
                status = manager.status(run_id)
                self.assertEqual(status['state'], 'starting')
                self.assertFalse(status['recoveryRequired'])
                self.assertFalse(status['identityVerified'])
            with self.manager._registry() as db:
                expires = self.manager._row(db, run_id)['startupExpiresAt']
            with other._registry() as db:
                self.assertEqual(other._row(db, run_id)['startupExpiresAt'], expires)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(launches), 1)
        self.assertEqual(self.manager.status(run_id)['state'], 'ready')

    def test_stale_socket_absence_cannot_veto_newer_native_receipt(self):
        run_id, _ = self.start()
        with self.manager._registry() as db:
            row = self.manager._row(db, run_id)
            row.update(nativeReceipt=False, state='starting', startupExpiresAt=row['createdAt'] + 1)
            self.manager._save(db, row)
        disconnected = copy.deepcopy(self.snapshots[run_id])
        disconnected.update(state='uncertain', identityVerified=False)
        def commit_receipt_after_socket_absence(path):
            with self.manager._registry() as db:
                current = self.manager._row(db, run_id)
                current.update(nativeReceipt=True, state='ready')
                self.manager._save(db, current)
            return copy.deepcopy(disconnected)
        manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
            _test_dependencies=replace(self.dependencies, read_snapshot=commit_receipt_after_socket_absence))
        self.disconnected = True
        snapshot = manager.status(run_id)
        self.assertEqual(snapshot['state'], 'ready')
        self.assertFalse(snapshot['identityVerified'])
        self.assertFalse(snapshot['recoveryRequired'])
        with manager._registry() as db:
            self.assertEqual(manager._row(db, run_id)['state'], 'ready')
        # A subsequent independently observed disconnect has no receipt advance,
        # so it correctly reports uncertainty rather than extending startup.
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')

    def test_stale_socket_absence_cannot_corrupt_newer_confirmed_exit(self):
        run_id, _ = self.start()
        disconnected = copy.deepcopy(self.snapshots[run_id])
        disconnected.update(state='uncertain', identityVerified=False)
        def commit_exit_after_socket_absence(path):
            with self.manager._registry() as db:
                current = self.manager._row(db, run_id)
                current.update(state='exited', occupied=False)
                self.manager._save(db, current)
            return copy.deepcopy(disconnected)
        manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
            _test_dependencies=replace(self.dependencies, read_snapshot=commit_exit_after_socket_absence))
        self.disconnected = True
        snapshot = manager.status(run_id)
        self.assertEqual(snapshot['state'], 'exited')
        self.assertFalse(snapshot['identityVerified'])
        self.assertFalse(snapshot['recoveryRequired'])
        with manager._registry() as db:
            row = manager._row(db, run_id)
            self.assertEqual(row['state'], 'exited')
            self.assertFalse(row['occupied'])
        self.assertEqual(self.manager.status(run_id)['state'], 'exited')

    def test_startup_grace_expires_without_renewal_and_never_masks_live_failure(self):
        run_id, _ = self.start()
        with self.manager._registry() as db:
            row = self.manager._row(db, run_id)
            row.update(nativeReceipt=False, state='starting', startupExpiresAt=row['createdAt'] + 1)
            self.manager._save(db, row)
        self.disconnected = True
        with patch.object(managed.time, 'time', return_value=row['startupExpiresAt'] + .01):
            status = self.make().status(run_id)
            self.assertEqual(status['state'], 'uncertain')
            self.assertTrue(status['recoveryRequired'])
        # Even if a clock correction places the old deadline in the future,
        # retained uncertainty cannot become a fresh startup window.
        with patch.object(managed.time, 'time', return_value=row['startupExpiresAt'] - .5):
            self.assertEqual(self.make().status(run_id)['state'], 'uncertain')
        self.disconnected = False
        with self.manager._registry() as db:
            current = self.manager._row(db, run_id)
            current['state'] = 'starting'
            self.manager._save(db, current)
        self.snapshots[run_id]['state'] = 'uncertain'
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')
        self.snapshots[run_id].update(state='exited', nativeExit=dict(confirmed=True, code=0,
                                    incarnation=self.snapshots[run_id]['incarnation']))
        self.assertEqual(self.manager.status(run_id)['state'], 'exited')
        self.assertEqual(len(self.spawns), 1)

    def test_eight_pending_permissions_plus_terminal_evidence_remain_available(self):
        run_id, _ = self.start()
        permissions = [dict(id=uid(), tool='Write', input={'path': '/fictional/note.txt'},
                            state='pending', canAllow=True, canDeny=True, createdAt=1)
                       for _ in range(8)]
        permissions.append(dict(id=uid(), tool='Read', input={}, state='denied',
                                canAllow=False, canDeny=False, createdAt=1))
        self.snapshots[run_id]['permissions'] = permissions
        self.assertEqual(len(self.manager.status(run_id)['permissions']), 9)
        self.assertTrue(managed.Adapter(self.manager).managed_status(run_id)['permissionsAvailable'])

    def test_lost_launch_response_never_spawns_again_after_restart(self):
        run_id, native = self.start()
        self.disconnected = True
        self.snapshots[run_id]['identityVerified'] = False
        other = self.make()
        with self.assertRaises(managed.ManagedError):
            other.launch_once(run_id, self.folder, lambda *_: self.fail('uncertain receipt'))
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(other.status(run_id)['state'], 'uncertain')

    def test_spawn_failure_claim_is_permanent(self):
        calls = []
        def broken(path):
            calls.append(path)
            raise RuntimeError('private failure')
        self.manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                             _test_dependencies=replace(self.dependencies, spawn=broken))
        run_id = self.manager.reserve(self.folder, uid())
        for _ in range(2):
            with self.assertRaises(managed.ManagedError):
                self.manager.launch_once(run_id, self.folder, lambda *_: self.fail('receipt'))
        self.assertEqual(len(calls), 1)

    def test_native_identity_mismatch_rejects_receipt(self):
        def wrong(path):
            self.spawn(path)
            self.snapshots[path.name]['nativeId'] = uid()
        self.manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                             _test_dependencies=replace(self.dependencies, spawn=wrong))
        run_id = self.manager.reserve(self.folder, uid())
        with self.assertRaises(managed.ManagedError):
            self.manager.launch_once(run_id, self.folder, lambda *_: self.fail('wrong identity receipt'))
        self.assertEqual(len(self.spawns), 1)

    def test_changed_folder_rejects_before_launch(self):
        run_id = self.manager.reserve(self.folder, uid())
        self.project.rename(self.root / 'old')
        self.project.mkdir()
        with self.assertRaises(NewChatError):
            self.manager.launch_once(run_id, self.folder, lambda *_: None)
        self.assertEqual(self.spawns, [])

    def test_missing_registry_and_whole_directory_are_explicit_errors(self):
        run_id, native = self.start()
        db = self.manager.root / 'registry.sqlite3'
        moved = db.with_suffix('.saved')
        db.rename(moved)
        for action in (lambda: self.manager.ownership(native),
                       lambda: self.manager.can_observe(uid(), self.source)):
            with self.assertRaises(managed.ManagedError):
                action()
        moved.rename(db)
        self.manager.root.rename(self.root / 'removed-managed')
        with self.assertRaises(managed.ManagedError):
            self.make().can_observe(uid(), self.source)

    def test_all_registry_rows_validate_before_unrelated_discovery(self):
        run_id, _ = self.start()
        db_path = self.manager.root / 'registry.sqlite3'
        with closing(sqlite3.connect(db_path)) as db, db:
            raw = db.execute('SELECT data FROM runs WHERE id=?', (run_id,)).fetchone()[0]
            original = json.loads(raw)
        for change in ({'folder': {'path': '/fictional'}}, {'nativeReceipt': 'yes'}, {'launchClaimed': 1}):
            corrupt = dict(original, **change)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(corrupt), run_id))
            with self.assertRaises(managed.ManagedError):
                self.manager.can_observe(uid(), self.source)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(original), run_id))
        # Integrity checks inspect retained fingerprints without requiring every
        # unrelated historical destination to still exist on disk.
        self.project.rmdir()
        self.assertTrue(self.manager.can_observe(uid(), self.source))

    def test_public_permissions_symlink_hardlink_and_world_readable_state_rejected(self):
        run_id, _ = self.start()
        token = self.manager.root / run_id / 'token'
        token.chmod(0o644)
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')
        token.chmod(0o600)
        original = token.read_bytes()
        token.unlink()
        os.link(self.binary, token)
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')
        token.unlink()
        token.symlink_to(self.binary)
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')
        token.unlink()
        token.write_bytes(original)
        token.chmod(0o600)

    def test_runner_uncertainty_keeps_capacity_until_confirmed_exit(self):
        run_id, _ = self.start()
        for _ in range(3):
            self.manager.reserve(self.folder, uid())
        self.disconnected = True
        self.assertEqual(self.manager.status(run_id)['state'], 'uncertain')
        with self.assertRaises(managed.ManagedError):
            self.manager.reserve(self.folder, uid())
        self.snapshots[run_id]['nativeExit'] = dict(confirmed=True, code=0, incarnation=self.snapshots[run_id]['incarnation'])
        self.snapshots[run_id]['state'] = 'exited'
        self.manager.status(run_id)
        self.assertTrue(self.manager.reserve(self.folder, uid()))

    def test_forged_exit_receipt_does_not_release_slot(self):
        run_id, _ = self.start()
        self.snapshots[run_id]['nativeExit'] = dict(confirmed=True, code=0, incarnation=uid())
        with self.assertRaises(managed.ManagedError):
            self.manager.status(run_id)
        with self.manager._registry() as db:
            self.assertTrue(self.manager._row(db, run_id)['occupied'])

    def test_enqueue_lost_response_replay_is_read_only_and_content_source_bound(self):
        run_id, _ = self.start()
        request = uid()
        self.drop = True
        first = self.manager.enqueue(run_id, request, 'owner', 'hello')
        self.assertEqual(first['state'], 'queued')
        other = self.make()
        self.assertEqual(other.enqueue(run_id, request, 'owner', 'hello')['requestId'], request)
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)
        for source, text in (('new_chat', 'hello'), ('owner', 'changed')):
            with self.assertRaises(managed.ManagedError):
                other.enqueue(run_id, request, source, text)

    def test_lost_write_outcome_tombstone_never_reissues(self):
        run_id, _ = self.start()
        self.disconnected = True
        request = uid()
        first = self.manager.enqueue(run_id, request, 'owner', 'hello')
        self.assertEqual(first['state'], 'unknown')
        self.disconnected = False
        self.manager.enqueue(run_id, request, 'owner', 'hello')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)

    def test_managed_text_limits_utf8_and_no_native_activity(self):
        run_id, _ = self.start()
        count = len(self.commands)
        for text in ('', 'a' * 8001, 'hello\x00', '\ud800'):
            with self.assertRaises(managed.ManagedError):
                self.manager.enqueue(run_id, uid(), 'owner', text)
        self.assertEqual(len(self.commands), count)
        self.assertEqual(self.manager.enqueue(run_id, uid(), 'owner', 'a' * 8000)['state'], 'queued')

    def test_exact_metadata_admission_and_scanner_fence(self):
        run_id, native = self.start()
        self.assertFalse(self.manager.can_observe(native, self.source))
        self.assertTrue(self.manager.can_observe(uid(), self.source))
        self.directory.wrong = True
        self.assertIsNone(self.manager.verify_existing(native, self.folder))
        self.directory.wrong = False
        proof = self.manager.verify_existing(native, self.folder)
        self.assertEqual(proof.provider, 'claude_code')
        self.assertFalse(self.manager.mark_admitted(proof, permit=lambda: False))
        self.assertFalse(self.manager.ownership(native)['admitted'])
        self.assertTrue(self.manager.mark_admitted(proof))
        self.assertTrue(self.manager.can_observe(native, self.source))
        self.assertFalse(self.manager.can_observe(native, self.root / 'wrong'))
        self.directory.current = False
        self.assertFalse(self.manager.can_observe(native, self.source))

    def test_fabricated_identity_cannot_mark_admitted(self):
        _, native = self.start()
        st = self.source.stat()
        forged = VerifiedIdentity(native, self.folder.path, str(self.source), st.st_dev, st.st_ino,
                                  managed.ADAPTER_VERSION, self.folder.device, self.folder.inode, provider='claude_code')
        with self.assertRaises(managed.ManagedError):
            self.manager.mark_admitted(forged)

    def test_owner_requires_admission_and_replay_precedes_disconnect(self):
        run_id, native = self.start()
        with self.assertRaises(managed.ManagedError):
            self.manager.owner_send('c:' + native, 'hello', uid())
        self.manager.mark_admitted(self.manager.verify_existing(native, self.folder))
        self.echo = True
        request = uid()
        row = self.manager.owner_send('c:' + native, 'hello', request)
        self.assertEqual(row['state'], 'awaiting_reply')
        self.assertEqual(row['delivery_state'], 'delivered')
        self.assertEqual(row['transport'], 'claude_managed_v1')
        self.disconnected = True
        self.manager.close()
        self.assertEqual(self.manager.owner_send('c:' + native, 'hello', request)['state'], 'awaiting_reply')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)

    def test_only_owner_typed_text_uses_the_owner_source(self):
        import attribution
        run_id, native = self.admit()
        self.echo = True
        owner, motet = uid(), uid()
        typed = self.manager.owner_send('c:' + native, 'Ada typed this.', owner, author='owner')
        worded = self.manager.owner_send('c:' + native, 'Is the orchard index rebuilt?', motet, author='motet')
        sent = {c[2]['requestId']: c[2] for c in self.commands if c[1] == 'enqueue'}
        self.assertEqual((sent[owner]['source'], sent[owner]['text']), ('owner', 'Ada typed this.'))
        self.assertEqual(sent[motet]['source'], 'motet')
        self.assertEqual(sent[motet]['text'], attribution.motet_worded('Is the orchard index rebuilt?'))
        self.assertEqual((typed['author'], typed['question']), ('owner', 'Ada typed this.'))
        self.assertEqual((worded['author'], worded['question']), ('motet', 'Is the orchard index rebuilt?'))
        self.manager.status(run_id)   # a runner snapshot does not carry the author; the registry keeps it
        listed = {row['request_id']: row for row in self.manager.owner_snapshot()['requests']}
        self.assertEqual((listed[owner]['author'], listed[motet]['author']), ('owner', 'motet'))
        self.assertEqual(listed[motet]['question'], 'Is the orchard index rebuilt?')
        with self.assertRaises(managed.ManagedError):
            self.manager.owner_send('c:' + native, 'Ada typed this.', owner, author='motet')
        with self.assertRaises(managed.ManagedError):
            self.manager.owner_send('c:' + native, 'Hello', uid(), author='Bram')

    def test_an_older_runner_refusing_motets_source_is_a_definite_failure(self):
        run_id, native = self.admit()
        self.refused_sources = ('motet',)
        request = uid()
        row = self.manager.owner_send('c:' + native, 'Is the orchard index rebuilt?', request, author='motet')
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error'], managed.MOTET_SOURCE_REFUSED)
        self.assertIn('Restart this managed chat', row['error'])
        sent = [c[2] for c in self.commands if c[1] == 'enqueue']
        self.assertEqual([c['source'] for c in sent], ['motet'], 'never resent as the owner')
        self.manager.status(run_id)
        self.assertEqual(self.manager.lookup_message(request)['state'], 'failed')
        self.assertEqual(self.manager.owner_send('c:' + native, 'Is the orchard index rebuilt?', request, author='motet')['state'], 'failed')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)
        with self.assertRaises(managed.ManagedError) as refused:
            managed._reply(b'{"ok":false,"error":{"code":"invalid_request"}}\n')
        self.assertEqual(refused.exception.code, 'runner_refused')
        with self.assertRaises(managed.ManagedError) as other:
            managed._reply(b'{"ok":false,"error":{"code":"unavailable"}}\n')
        self.assertEqual(other.exception.code, 'managed_not_ready')

    def test_owner_rows_stored_before_authors_stay_unknown(self):
        run_id, native = self.admit()
        self.echo = True
        request = uid()
        self.manager.owner_send('c:' + native, 'Old words.', request, author='owner')
        with self.manager._registry() as db:     # a row as an earlier Motet stored it: source owner, no author
            data = json.loads(db.execute('SELECT data FROM messages WHERE id=?', (request,)).fetchone()[0])
            data.pop('author')
            db.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(data), request))
        self.snapshots[run_id]['messages'][-1].pop('author', None)
        self.manager.status(run_id)
        row = next(r for r in self.manager.owner_snapshot()['requests'] if r['request_id'] == request)
        self.assertNotIn('author', row, 'an old owner-source row is unknown, never the owner')
        before = len([c for c in self.commands if c[1] == 'enqueue'])
        replay = self.manager.owner_send('c:' + native, 'Old words.', request, author='motet')
        self.assertEqual(replay['request_id'], request)
        self.assertNotIn('author', replay)
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), before)

    def test_disconnection_preserves_echo_but_reports_unknown_delivery(self):
        run_id, native = self.admit()
        self.echo = True
        request_id = uid()
        self.manager.owner_send('c:' + native, 'hello', request_id)
        self.disconnected = True
        snapshot = self.manager.status(run_id)
        self.assertEqual(snapshot['messages'][0]['state'], 'unknown')
        self.assertTrue(snapshot['messages'][0]['nativeEcho'])
        self.assertEqual(self.manager.lookup_message(request_id)['state'], 'unknown')
        self.assertEqual(self.manager.owner_snapshot()['requests'][0]['state'], 'unknown')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)

    def test_owner_collision_with_first_instruction_cannot_fall_through(self):
        run_id, native = self.admit()
        request = managed.Adapter.first_request_id(run_id)
        self.manager.enqueue(run_id, request, 'new_chat', 'hello')
        with self.assertRaises(managed.ManagedError):
            self.manager.owner_send('c:' + native, 'hello', request)

    def test_owner_snapshot_sanitizes_internal_runtime_and_includes_readiness(self):
        run_id, native = self.admit()
        self.echo = True
        self.manager.owner_send('c:' + native, 'hello', uid())
        snapshot = self.manager.owner_snapshot()
        self.assertTrue(snapshot['available'])
        encoded = json.dumps(snapshot)
        self.assertNotIn('incarnation', encoded)
        self.assertNotIn(str(self.manager.root), encoded)
        self.assertEqual(snapshot['requests'][0]['question'], 'hello')
        self.assertFalse(self.manager.owner_capabilities('c:' + native)['actions']['steer']['supported'])

    def test_real_ipc_client_accepts_bounded_large_response_and_hides_credentials(self):
        # Unix-domain sockets need a short path even when TMPDIR is long.
        with tempfile.TemporaryDirectory(prefix='cm-', dir=Path('/tmp').resolve()) as short:
            manager = managed.ManagedClaude(Path(short), enabled=True, configuration_ready=True,
                                             _test_dependencies=self.dependencies)
            run_id = manager.reserve(self.folder, uid())
            manager.launch_once(run_id, self.folder, lambda *_: None)
            path = manager.root / run_id
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path / 'control.sock'))
            os.chmod(path / 'control.sock', 0o600)
            listener.listen(1)
            requests = []
            def answer():
                connection, _ = listener.accept()
                with connection:
                    received = bytearray()
                    while b'\n' not in received:
                        received.extend(connection.recv(8192))
                    requests.append(json.loads(received))
                    connection.sendall(json.dumps({'ok': True, 'value': 'x' * (300 * 1024)}).encode() + b'\n')
            thread = threading.Thread(target=answer)
            thread.start()
            try:
                result = managed.client_request(path, 'status', {})
                self.assertEqual(len(result), 300 * 1024)
                self.assertEqual(requests[0]['token'], (path / 'token').read_text())
                self.assertEqual(requests[0]['command'], 'status')
            finally:
                thread.join(5)
                listener.close()

    def test_read_only_late_receipt_reconciles_outside_snapshot_window(self):
        run_id, _ = self.start()
        request_id = uid()
        self.manager.enqueue(run_id, request_id, 'owner', 'hello')
        retained = copy.deepcopy(self.snapshots[run_id]['messages'][0])
        retained.update(state='completed', nativeEcho=True, reply='done')
        self.snapshots[run_id]['messages'] = []
        other = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
            _test_dependencies=replace(self.dependencies, read_message=lambda path, ident: retained))
        self.assertEqual(other.lookup_message(request_id)['state'], 'completed')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)

    def test_thirty_two_unresolved_messages_reject_before_native_write(self):
        run_id, _ = self.start()
        for number in range(32):
            self.manager.enqueue(run_id, uid(), 'owner', 'message ' + str(number))
        count = len(self.commands)
        with self.assertRaises(managed.ManagedError) as error:
            self.manager.enqueue(run_id, uid(), 'owner', 'overflow')
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(len(self.commands), count)

    def test_permission_schema_is_allowlisted_and_validated(self):
        run_id, _ = self.start()
        receipt = self.manager.decide(run_id, uid(), uid(), 'deny')
        self.assertNotIn('token', receipt)
        for permission, request, decision in (('bad', uid(), 'allow'), (uid(), uid(), 'always')):
            with self.assertRaises(managed.ManagedError):
                self.manager.decide(run_id, permission, request, decision)

    def test_permission_timing_outcomes_and_contact_loss_are_projected(self):
        run_id, _ = self.start()
        permission = dict(id=uid(), tool='Write', input={'content': 'fictional'}, state='pending',
                          canAllow=True, canDeny=True, createdAt=1900000000, expiresAt=1900000120,
                          decision=None, resolutionReason=None, decisionState=None, resolvedAt=None,
                          inputAvailable=True, nativeRequestId='private-only')
        raw = self.snapshots[run_id]
        raw.update(permissions=[permission], timeoutSeconds=120, evidenceAvailable=True, serverTime=1)
        with patch.object(managed.time, 'time', return_value=1900000030):
            snapshot = self.manager.status(run_id)
        self.assertEqual(snapshot['serverTime'], 1900000030, 'fallback must not reuse a stale runner clock')
        self.assertEqual(snapshot['timeoutSeconds'], 120)
        self.assertTrue(snapshot['evidenceAvailable'])
        self.assertEqual(snapshot['permissions'][0], {k: v for k, v in permission.items() if k != 'nativeRequestId'})
        self.disconnected = True
        lost = self.manager.status(run_id)
        self.assertEqual(lost['state'], 'uncertain')
        self.assertTrue(lost['evidenceAvailable'])
        self.assertEqual(lost['permissions'][0]['createdAt'], 1900000000)
        self.assertFalse(lost['permissions'][0]['canAllow'])
        self.assertFalse(lost['permissions'][0]['canDeny'])
        self.assertIsNone(lost['permissions'][0]['resolvedAt'], 'contact loss cannot invent a resolution')
        raw['permissions'][0].update(state='expired', decision='deny', resolutionReason='timeout',
                                     decisionState='unknown', resolvedAt=1900000120)
        last = self.manager.status(run_id)['permissions'][0]
        self.assertEqual(last['decisionState'], 'unknown', 'attempted denial is not a confirmed denial')
        self.assertEqual(last['resolutionReason'], 'timeout')

    def test_unreadable_permission_ledger_is_not_empty_readable_history(self):
        run_id, _ = self.start()
        self.disconnected = True
        self.snapshots.pop(run_id)
        result = self.manager.status(run_id)
        self.assertEqual(result['state'], 'uncertain')
        self.assertFalse(result['evidenceAvailable'])
        self.assertEqual(result['permissions'], [])

    def test_permission_new_fields_reject_invalid_values(self):
        run_id, _ = self.start()
        base = dict(id=uid(), tool='Read', input={}, state='pending', canAllow=True, canDeny=True,
                    createdAt=10, expiresAt=130, inputAvailable=True)
        for key, value in [('expiresAt', float('nan')), ('resolvedAt', True), ('createdAt', -1),
                           ('expiresAt', 9), ('decision', 'always'), ('resolutionReason', 'made_up'),
                           ('decisionState', 'accepted'), ('inputAvailable', 'yes')]:
            with self.subTest(key=key, value=value):
                self.snapshots[run_id]['permissions'] = [{**base, key: value}]
                with self.assertRaises(managed.ManagedError):
                    self.manager.status(run_id)

    def test_legacy_permission_fields_remain_unknown(self):
        run_id, _ = self.start()
        self.snapshots[run_id]['permissions'] = [dict(id=uid(), tool='Read', input={},
            state='unsupported', canAllow=False, canDeny=False, createdAt=10)]
        permission = self.manager.status(run_id)['permissions'][0]
        for key in ('expiresAt', 'decision', 'resolutionReason', 'decisionState', 'resolvedAt'):
            self.assertIsNone(permission[key])
        self.assertFalse(permission['inputAvailable'])

    def test_permission_lost_ack_and_runner_loss_replays_read_only(self):
        run_id, native = self.admit()
        permission_id, request_id = uid(), uid()
        receipts = {}
        def lost_ack(path, command, params):
            if command == 'decision':
                self.commands.append((path.name, command, copy.deepcopy(params)))
                receipts[params['requestId']] = dict(params, state='unknown')
                raise OSError('lost native acknowledgement')
            return self.request(path, command, params)
        dependencies = replace(self.dependencies, request=lost_ack,
            read_decision=lambda path, permission, request, decision: receipts.get(request))
        manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
                                         _test_dependencies=dependencies)
        receipt = manager.decide(run_id, permission_id, request_id, 'deny')
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(manager.decide(run_id, permission_id, request_id, 'deny'), receipt)
        self.assertEqual(len([c for c in self.commands if c[1] == 'decision']), 1)
        with self.assertRaises(managed.ManagedError):
            manager.decide(run_id, permission_id, request_id, 'allow')
        with self.assertRaises(managed.ManagedError):
            manager.owner_send('c:' + native, 'new message', request_id)
        with self.assertRaises(managed.ManagedError):
            manager.enqueue(run_id, request_id, 'new_chat', 'new task')
        self.assertEqual(manager.lookup_message(request_id)['source'], 'permission')

    def test_native_uncertain_error_reconciles_claim_before_mapping_error(self):
        run_id, _ = self.start()
        permission_id, request_id = uid(), uid()
        receipt = dict(permissionId=permission_id, requestId=request_id, decision='deny', state='unknown',
                       decisionState='unknown', resolutionReason='owner', resolvedAt=1900000040)
        def uncertain(path, command, params):
            self.commands.append((path.name, command, copy.deepcopy(params)))
            raise managed.ManagedError(409, 'managed_not_ready', 'The native outcome is uncertain.')
        manager = managed.ManagedClaude(self.root, enabled=True, configuration_ready=True,
            _test_dependencies=replace(self.dependencies, request=uncertain,
                read_decision=lambda *args: copy.deepcopy(receipt)))
        self.assertEqual(manager.decide(run_id, permission_id, request_id, 'deny'), receipt)
        self.assertEqual(manager.decide(run_id, permission_id, request_id, 'deny'), receipt)
        self.assertEqual(len([c for c in self.commands if c[1] == 'decision']), 1)

    def test_missing_decision_claim_is_unavailable_without_automatic_retry(self):
        run_id, _ = self.start()
        permission_id, request_id = uid(), uid()
        self.disconnected = True
        with self.assertRaises(managed.ManagedError):
            self.manager.decide(run_id, permission_id, request_id, 'allow')
        self.disconnected = False
        self.assertEqual(self.manager.decide(run_id, permission_id, request_id, 'allow')['state'], 'unknown')
        self.assertEqual(len([c for c in self.commands if c[1] == 'decision']), 1)

    def test_adapter_claim_precedes_enqueue_and_queue_is_not_receipt(self):
        run_id, _ = self.start()
        adapter = managed.Adapter(self.manager)
        claims, receipts = [], []
        result = adapter.start_managed_work_once(run_id, 'first task', lambda: claims.append(len(self.commands)), receipts.append)
        self.assertEqual(result.outcome, 'unknown')
        self.assertEqual(result.receipt_kind, 'native_user_message')
        self.assertIsNone(result.turn_id)
        self.assertEqual(len(claims), 1)
        self.assertEqual(receipts, [])
        self.assertEqual(self.commands[claims[0]][1], 'enqueue')
        adapter.start_managed_work_once(run_id, 'first task', lambda: self.fail('duplicate claim'), receipts.append)
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)
        message = self.snapshots[run_id]['messages'][0]
        message.update(state='delivered', nativeEcho=True)
        result = adapter.managed_execution_status(run_id)
        self.assertEqual(result.turn_id, adapter.first_request_id(run_id))
        self.assertEqual(result.outcome, 'running')
        message.update(state='completed', reply='done')
        self.assertEqual(adapter.managed_execution_status(run_id).outcome, 'completed')

    def test_claim_rejection_does_not_enqueue(self):
        run_id, _ = self.start()
        def reject():
            raise RuntimeError('service claim rejected')
        with self.assertRaises(RuntimeError):
            managed.Adapter(self.manager).start_managed_work_once(run_id, 'first', reject, lambda *_: None)
        self.assertFalse(any(command == 'enqueue' for _, command, _ in self.commands))

    def test_terminal_text_expiry_retains_uuid_digest(self):
        run_id, _ = self.start()
        request = uid()
        self.manager.enqueue(run_id, request, 'owner', 'hello')
        self.snapshots[run_id]['messages'][0].update(state='completed', createdAt=1)
        self.manager.status(run_id)
        retained = self.manager.lookup_message(request)
        self.assertTrue(retained['textExpired'])
        self.assertEqual(retained['text'], '')
        self.manager.enqueue(run_id, request, 'owner', 'hello')
        self.assertEqual(len([c for c in self.commands if c[1] == 'enqueue']), 1)


from new_chat_types import NewChatError

if __name__ == '__main__':
    unittest.main()
