"""Fictional adapter/stdio peers only. Never starts native Codex or sends a real turn."""
from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import plistlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_new_chat as native
from new_chat_types import Capability, FrozenFolder, NewChatError, OpenResult, VerifiedIdentity

ID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


def freeze(path):
    info = path.stat()
    return FrozenFolder('fictional-folder', 'fictional-revision', 'Orchard', str(path), info.st_dev, info.st_ino)


class FakeTransport:
    def __init__(self, folder, events):
        self.folder, self.events, self.closed = folder, events, False
        self.response = {'thread': {'id': ID, 'cwd': folder.path, 'sessionId': ID, 'turns': [], 'ephemeral': False},
                         'cwd': folder.path}
        self.fail = None

    def request(self, method, params):
        self.events.append((method, params))
        if self.fail == method:
            raise RuntimeError('FICTIONAL_PRIVATE_NATIVE_DIAGNOSTIC')
        if method == 'thread/start':
            return self.response
        return {}

    def notify_initialized(self):
        self.events.append(('initialized', {}))

    def close(self):
        self.closed = True
        self.events.append(('close', {}))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS temp aliases resolve to a canonical path before freezing it.
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'orchard'
        self.project.mkdir()
        self.folder = freeze(self.project)
        self.events = []
        self.transport = FakeTransport(self.folder, self.events)
        self.proof = VerifiedIdentity(ID, self.folder.path, str(self.root / 'rollout.jsonl'), 2, 3,
                                      native.ADAPTER_VERSION, self.folder.device, self.folder.inode)
        self.dependencies = native.TestDependencies(
            lambda: Capability(True, version=native.ADAPTER_VERSION),
            lambda: {'contract': 'fictional-native-defaults-v1', 'verified': True},
            lambda folder: self.transport, self.verify,
            lambda url: (self.events.append(('open', url)) or OpenResult('accepted')))
        self.adapter = native.Adapter(self.root, _test_dependencies=self.dependencies)
        self.addCleanup(self.adapter.close)

    def verify(self, ident, folder):
        self.events.append(('verify', ident))
        return self.proof

    def receipt(self, ident, cwd):
        self.events.append(('receipt', (ident, cwd)))

    def test_environment_cannot_bypass_missing_private_desktop_endpoint(self):
        with patch.object(native, '_installed_build', return_value=Capability(True)), \
                patch.dict(os.environ, {'WATCH_NEW_CHAT_ENABLE': '1', 'CODEX_NEW_CHAT_DEFAULTS_VERIFIED': 'true'}), \
                patch.object(native, 'StdioTransport', side_effect=AssertionError('must not spawn')):
            adapter = native.Adapter(self.root)
            self.assertEqual(adapter.preflight().code, 'app_version_unsupported')
            with self.assertRaises(NewChatError) as caught:
                adapter.create_once(self.folder, self.receipt)
            self.assertEqual(caught.exception.code, 'app_version_unsupported')
        self.assertEqual(self.events, [])

    def desktop(self, *, lost=False, owner_missing=False, status='inProgress'):
        events = self.events
        class Peer:
            mutation_attempted = False
            def request(peer, method, params, client, version, owner=None):
                events.append((method, params))
                if method == 'initialize':
                    return {'handledByClientId': ID, 'result': {'clientId': ID}}
                if method == 'thread-owner-discovery':
                    if owner_missing:
                        raise OSError('FICTIONAL_PRIVATE_DIAGNOSTIC')
                    return {'handledByClientId': OTHER, 'result': {'supportsUntrustedAppInput': True}}
                if method == 'thread-follower-read-model-settings':
                    return {'result': {'settings': {'model': 'fictional-model', 'reasoningEffort': None,
                                                   'mode': 'default', 'modelProvider': 'fictional'}}}
                if method == 'thread-follower-start-turn':
                    peer.mutation_attempted = True
                    if lost:
                        raise OSError('FICTIONAL_PRIVATE_INSTRUCTION')
                    return {'result': {'result': {'turn': {'id': OTHER, 'status': status}}}}
                raise AssertionError(method)
            def close(peer):
                events.append(('desktop-close', {}))
        return Peer()

    def working_adapter(self, **kwargs):
        deps = replace(self.dependencies, desktop_factory=lambda: self.desktop(**kwargs))
        adapter = native.Adapter(self.root, _test_dependencies=deps)
        self.addCleanup(adapter.close)
        adapter.verify_existing(ID, self.folder)
        return adapter

    def test_supported_native_build_uses_private_endpoint_without_env_enable(self):
        with patch.object(native, '_installed_build', return_value=Capability(True)), \
                patch.object(native._desktop, '_verify_endpoint') as endpoint, \
                patch.object(native, 'StdioTransport', side_effect=AssertionError('no spawn in preflight')):
            result = native.Adapter(self.root).preflight()
        self.assertTrue(result.available)
        self.assertTrue(result.supports_work)
        endpoint.assert_called_once_with(self.root / '.codex/ipc/ipc.sock')

    def test_desktop_turn_reserves_and_records_exact_receipt_with_inherited_settings(self):
        adapter = self.working_adapter()
        result = adapter.start_work_once(self.proof, 'Fictional instruction',
            lambda: self.events.append(('claim', {})),
            lambda turn: self.events.append(('turn-receipt', turn)))
        self.assertEqual((result.outcome, result.turn_id), ('accepted', OTHER))
        names = [x[0] for x in self.events]
        self.assertLess(names.index('claim'), names.index('thread-follower-start-turn'))
        self.assertLess(names.index('thread-follower-start-turn'), names.index('turn-receipt'))
        self.assertEqual(names[-1], 'desktop-close')
        params = next(value for name, value in self.events if name == 'thread-follower-start-turn')
        self.assertEqual(params, {'conversationId': ID, 'turnStart': {'request': {'threadId': ID,
            'input': [{'type': 'text', 'text': 'Fictional instruction', 'text_elements': []}]},
            'context': {'inheritThreadSettings': True}}})
        self.assertFalse(any(name in ('turn/start', 'turn/interrupt') for name in names))

    def test_readiness_rechecks_transient_read_failure_before_one_mutation(self):
        peers = [self.desktop(owner_missing=True), self.desktop()]
        calls = []
        def factory():
            calls.append(True)
            return peers.pop(0)
        adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies, desktop_factory=factory))
        adapter.verify_existing(ID, self.folder)
        with patch.object(native.time, 'sleep'):
            result = adapter.start_work_once(self.proof, 'Fictional instruction', lambda: None, lambda _: None)
        self.assertEqual(result.outcome, 'accepted')
        self.assertEqual(len(calls), 2)
        self.assertEqual([name for name, _ in self.events].count('thread-follower-start-turn'), 1)

    def test_malformed_native_settings_fail_closed_without_retry_or_submission(self):
        peer = self.desktop()
        request = peer.request
        def malformed(method, *args, **kwargs):
            return {'result': {'settings': {'model': 'fictional'}}} if method == 'thread-follower-read-model-settings' else request(method, *args, **kwargs)
        peer.request = malformed
        calls = []
        def factory():
            calls.append(True)
            return peer
        adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies, desktop_factory=factory))
        adapter.verify_existing(ID, self.folder)
        result = adapter.start_work_once(self.proof, 'Fictional instruction', lambda: self.fail('no claim'), lambda _: None)
        self.assertEqual(result.outcome, 'not_attempted')
        self.assertEqual(len(calls), 1)

    def test_lost_first_turn_reply_is_unknown_and_never_falls_back_or_retries(self):
        adapter = self.working_adapter(lost=True)
        result = adapter.start_work_once(self.proof, 'Fictional instruction', lambda: None,
                                         lambda _: self.fail('no turn receipt'))
        self.assertEqual((result.outcome, result.turn_id, result.code), ('unknown', None, 'execution_unknown'))
        names = [x[0] for x in self.events]
        self.assertEqual(names.count('thread-follower-start-turn'), 1)
        self.assertEqual(names.count('initialize'), 1)
        self.assertNotIn('turn/start', names)
        self.assertNotIn('PRIVATE', repr(result))

    def test_no_desktop_owner_or_failed_durable_claim_never_sends_instruction(self):
        for missing in (True, False):
            self.events.clear()
            adapter = self.working_adapter(owner_missing=missing)
            def refuse():
                raise OSError('FICTIONAL_DURABLE_COMMIT_FAILED')
            result = adapter.start_work_once(self.proof, 'Fictional instruction', refuse, lambda _: None)
            self.assertEqual(result.outcome, 'not_attempted')
            self.assertFalse(any(name == 'thread-follower-start-turn' for name, _ in self.events))

    def test_desktop_transport_cannot_approve_deny_or_answer_owner_questions(self):
        connection = object.__new__(native.DesktopConnection)
        for method in ('turn/start', 'turn/interrupt', 'thread-follower-command-approval-decision',
                       'thread-follower-file-approval-decision', 'thread-follower-submit-user-input',
                       'thread-follower-submit-mcp-server-elicitation-response'):
            with self.assertRaises(native.ProtocolError):
                connection.request(method, {}, ID, 1, OTHER)
        # The native Desktop keeps these requests; WATCH has no decision path.

    def test_failed_or_interrupted_native_turn_is_not_reported_as_completed(self):
        for status, expected in [('failed', 'failed'), ('interrupted', 'needs_attention')]:
            adapter = self.working_adapter(status=status)
            result = adapter.start_work_once(self.proof, 'Fictional instruction', lambda: None, lambda _: None)
            self.assertEqual(result.outcome, expected)

    def test_unsupported_platform_is_unavailable_without_opening_native_files(self):
        with patch.object(native.sys, 'platform', 'win32'), \
                patch.object(native, '_regular', side_effect=AssertionError('must not inspect native files')):
            result = native.Adapter(self.root).preflight()
        self.assertFalse(result.available)
        self.assertEqual(result.code, 'unsupported_platform')

    def test_build_or_unverified_fixture_settings_refuses_before_transport(self):
        for settings in ({}, {'contract': 'fictional-native-defaults-v1', 'verified': 1},
                         {'contract': 'fictional-native-defaults-v1', 'verified': True, 'model': 'fictional'}):
            adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies, settings=lambda: settings))
            self.assertFalse(adapter.preflight().available)
        bad = replace(self.dependencies, build_check=lambda: Capability(False, 'app_version_unsupported'))
        self.assertEqual(native.Adapter(self.root, _test_dependencies=bad).preflight().code, 'app_version_unsupported')
        self.assertEqual(self.events, [])

    def test_create_records_receipt_before_naming_closes_before_independent_verify(self):
        self.assertEqual(self.adapter.create_once(self.folder, self.receipt), self.proof)
        self.assertEqual([x[0] for x in self.events],
                         ['initialize', 'initialized', 'thread/start', 'receipt', 'thread/name/set', 'close', 'verify'])
        self.assertEqual(self.events[2][1], {'cwd': self.folder.path, 'ephemeral': False})
        self.assertEqual(self.events[3][1], (ID, self.folder.path))
        self.assertEqual(self.events[4][1], {'threadId': ID, 'name': 'New chat'})
        self.assertTrue(self.transport.closed)
        self.assertFalse(any(name == 'turn/start' for name, _ in self.events))

    def test_callback_failure_stops_all_following_mutations_and_hides_exception(self):
        def fail(*_):
            raise OSError('FICTIONAL_PRIVATE_JOURNAL_LOCATION')
        with self.assertRaises(NewChatError) as caught:
            self.adapter.create_once(self.folder, fail)
        self.assertEqual(caught.exception.code, 'creation_unknown')
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual([x[0] for x in self.events], ['initialize', 'initialized', 'thread/start', 'close'])

    def test_invalid_creation_rejects_and_retains_every_valid_native_receipt(self):
        for change in ({'cwd': str(self.root / 'other-orchard')}, {'thread': {'id': 'not-a-uuid'}},
                       {'thread': {'id': ID, 'sessionId': OTHER}},
                       {'thread': {'id': ID, 'forkedFromId': OTHER}},
                       {'thread': {'id': ID, 'turns': [{'id': 'fictional-turn'}]}},
                       {'thread': {'id': ID, 'cwd': '/fictional/other'}},
                       {'thread': {'id': ID, 'ephemeral': True}}):
            self.events.clear()
            self.transport.response = {'cwd': self.folder.path, 'thread': {'id': ID}, **change}
            with self.assertRaises(NewChatError):
                self.adapter.create_once(self.folder, self.receipt)
            receipts = [x[1] for x in self.events if x[0] == 'receipt']
            self.assertEqual(receipts, [(ID, self.transport.response['cwd'])]
                             if self.transport.response['thread'].get('id') == ID else [])
            self.assertNotIn('thread/name/set', [x[0] for x in self.events])

    def test_failed_name_never_recreates_or_opens(self):
        self.transport.fail = 'thread/name/set'
        with self.assertRaises(NewChatError):
            self.adapter.create_once(self.folder, self.receipt)
        self.assertEqual([x[0] for x in self.events].count('thread/start'), 1)
        self.assertIn('receipt', [x[0] for x in self.events])
        self.assertNotIn('open', [x[0] for x in self.events])
        self.assertTrue(self.transport.closed)

    def test_lost_creation_response_closes_without_guessing_or_retry(self):
        self.transport.fail = 'thread/start'
        with self.assertRaises(NewChatError) as caught:
            self.adapter.create_once(self.folder, self.receipt)
        self.assertEqual(caught.exception.code, 'creation_unknown')
        self.assertEqual([x[0] for x in self.events], ['initialize', 'initialized', 'thread/start', 'close'])

    def test_missing_persistence_preserves_receipt_without_success(self):
        deps = replace(self.dependencies, verifier=lambda *_: None)
        adapter = native.Adapter(self.root, _test_dependencies=deps)
        with self.assertRaises(NewChatError) as caught:
            adapter.create_once(self.folder, self.receipt)
        self.assertEqual(caught.exception.code, 'creation_unverified')
        self.assertIn('receipt', [x[0] for x in self.events])

    def test_folder_replacement_before_dispatch_or_after_receipt_rejects(self):
        self.project.rename(self.root / 'old-orchard')
        self.project.mkdir()
        with self.assertRaises(NewChatError) as caught:
            self.adapter.create_once(self.folder, self.receipt)
        self.assertEqual(caught.exception.code, 'folder_changed')
        self.assertEqual(self.events, [])
        self.folder = freeze(self.project)
        self.transport.folder = self.folder
        def replace_after_receipt(*args):
            self.receipt(*args)
            self.project.rename(self.root / 'second-old-orchard')
            self.project.mkdir()
        with self.assertRaises(NewChatError):
            self.adapter.create_once(self.folder, replace_after_receipt)
        self.assertNotIn('thread/name/set', [x[0] for x in self.events])

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires separate Windows privileges.')
    def test_symlink_folder_rejects(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(NewChatError):
            self.adapter.create_once(replace(self.folder, path=str(alias)), self.receipt)

    def test_changed_source_or_folder_identity_rejects(self):
        for proof in (replace(self.proof, native_id=OTHER), replace(self.proof, cwd='/fictional/other'),
                      replace(self.proof, folder_inode=0), replace(self.proof, adapter_version='old'),
                      replace(self.proof, source_path='relative.jsonl')):
            adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies, verifier=lambda *_: proof))
            self.assertIsNone(adapter.verify_existing(ID, self.folder))
        changed = replace(self.proof, source_inode=99)
        adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies, verifier=lambda *_: changed))
        adapter.verify_existing(ID, self.folder)
        self.assertEqual(adapter.open_existing(self.proof).outcome, 'rejected')

    def test_open_exact_id_draft_is_single_encoded_value_never_submitted(self):
        draft = 'Fictional draft &prompt=other? # + /\n雪'
        self.assertEqual(self.adapter.open_existing(self.proof).outcome, 'rejected')
        self.assertEqual(self.adapter.verify_existing(ID, self.folder), self.proof)
        self.events.clear()
        self.assertEqual(self.adapter.open_existing(self.proof, draft).outcome, 'accepted')
        url = [value for name, value in self.events if name == 'open'][0]
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ('codex', 'threads', '/' + ID))
        self.assertEqual(parse_qs(parsed.query), {'prompt': [draft]})
        self.assertEqual(parsed.fragment, '')
        self.assertEqual([x[0] for x in self.events], ['verify', 'open'])

    def test_open_outcomes_and_invalid_drafts_do_not_recreate(self):
        for outcome in ('accepted', 'unknown', 'rejected'):
            adapter = native.Adapter(self.root, _test_dependencies=replace(self.dependencies,
                opener=lambda _: OpenResult(outcome)))
            adapter.verify_existing(ID, self.folder)
            self.assertEqual(adapter.open_existing(self.proof).outcome, outcome)
        for draft in ('x' * 8001, '\ud800', 3):
            self.assertEqual(self.adapter.open_existing(self.proof, draft).code, 'invalid_draft')
        self.assertNotIn('thread/start', [x[0] for x in self.events])

    def test_close_is_terminal_and_closes_only_owned_fixture(self):
        self.adapter._transports.add(self.transport)
        self.adapter.close()
        self.assertTrue(self.transport.closed)
        self.assertFalse(self.adapter.preflight().available)
        self.assertEqual(self.adapter.open_existing(self.proof).code, 'adapter_closed')

    def test_advisory_binding_cache_evicts_oldest_and_requires_fresh_verification(self):
        self.adapter.verify_existing(ID, self.folder)
        for index in range(1024):
            ident = '33333333-3333-4333-8333-%012x' % index
            proof = replace(self.proof, native_id=ident)
            self.adapter._test = replace(self.dependencies, verifier=lambda *_: proof)
            self.assertIsNotNone(self.adapter.verify_existing(ident, self.folder))
        self.assertEqual(len(self.adapter._expected_folders), 1024)
        self.assertNotIn(ID, self.adapter._expected_folders)
        self.assertEqual(self.adapter.open_existing(self.proof).outcome, 'rejected')
        self.adapter._test = self.dependencies
        self.assertEqual(self.adapter.verify_existing(ID, self.folder), self.proof)
        self.assertEqual(self.adapter.open_existing(self.proof).outcome, 'accepted')


@unittest.skipIf(os.name == 'nt', 'Pinned native file verification uses POSIX O_NOFOLLOW.')
class DiskVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'orchard'
        self.project.mkdir()
        self.folder = freeze(self.project)
        self.sessions = self.root / '.codex/sessions'
        self.sessions.mkdir(parents=True)
        self.rollout = self.sessions / ('rollout-fictional-' + ID + '.jsonl')
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': ID, 'session_id': ID, 'cwd': self.folder.path, 'source': 'appServer', 'agent_path': '/root'}}) + '\n')
        self.database = self.root / '.codex/state_5.sqlite'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,archived INTEGER,source TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (ID, str(self.rollout), self.folder.path, 0, 'appServer'))
        db.close()
        self.adapter = native.Adapter(self.root)
        self.build = patch.object(native, '_installed_build', return_value=Capability(True))
        self.build.start()
        self.addCleanup(self.build.stop)

    def test_requires_index_and_fresh_pinned_native_header(self):
        proof = self.adapter.verify_existing(ID, self.folder)
        self.assertEqual(proof.native_id, ID)
        self.assertEqual(proof.source_inode, self.rollout.stat().st_ino)
        self.assertEqual(proof.folder_inode, self.folder.inode)
        self.assertIsNone(self.adapter.verify_existing(OTHER, self.folder))
        self.database.unlink()
        self.assertIsNone(self.adapter.verify_existing(ID, self.folder))

    def test_wrong_folder_archived_subagent_and_source_replacement_reject(self):
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE threads SET cwd=?', ('/fictional/other',))
        db.close()
        self.assertIsNone(self.adapter.verify_existing(ID, self.folder))
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE threads SET cwd=?,archived=1', (self.folder.path,))
        db.close()
        self.assertIsNone(self.adapter.verify_existing(ID, self.folder))
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE threads SET archived=0')
        db.close()
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': ID, 'cwd': self.folder.path, 'is_subagent': True}}) + '\n')
        self.assertIsNone(self.adapter.verify_existing(ID, self.folder))

    def test_actual_pinned_folder_mismatch_is_evidence_but_never_opens(self):
        other = self.root / 'other-orchard'
        other.mkdir()
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE threads SET cwd=?', (str(other),))
        db.close()
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': ID, 'cwd': str(other), 'source': 'appServer'}}) + '\n')
        proof = self.adapter.verify_existing(ID, self.folder)
        self.assertEqual(proof.cwd, str(other))
        self.assertEqual(proof.folder_inode, other.stat().st_ino)
        with patch.object(native.subprocess, 'Popen', side_effect=AssertionError('must not open')):
            self.assertEqual(self.adapter.open_existing(proof).code, 'folder_mismatch')
        self.assertIsNone(self.adapter.verify_existing(ID, freeze(other)))  # Cannot replace selection binding.

    def test_native_open_uses_fixed_bundle_and_reports_handoff_outcomes(self):
        proof = self.adapter.verify_existing(ID, self.folder)
        class Process:
            pid = 999999
            def __init__(self, outcome):
                self.outcome, self.waits, self.killed = outcome, 0, False
            def wait(self, timeout):
                self.waits += 1
                if self.outcome == 'timeout' and self.waits == 1:
                    raise subprocess.TimeoutExpired('fictional-open', timeout)
                return self.outcome if isinstance(self.outcome, int) else 0
            def kill(self):
                self.killed = True
        for exit_code, expected in ((0, 'accepted'), (1, 'rejected'), ('timeout', 'unknown')):
            process = Process(exit_code)
            with patch.object(native.subprocess, 'Popen', return_value=process) as popen, \
                    patch.object(native.os, 'killpg') as killpg:
                self.assertEqual(self.adapter.open_existing(proof, 'fictional & unsent').outcome, expected)
                argv = popen.call_args.args[0]
                self.assertEqual(argv[:3], ['/usr/bin/open', '-a', str(native.APP)])
                self.assertEqual(argv[3], 'codex://threads/' + ID + '?prompt=fictional%20%26%20unsent')
                self.assertTrue(popen.call_args.kwargs['start_new_session'])
                self.assertNotIn('shell', popen.call_args.kwargs)
                self.assertEqual(killpg.call_count, 2 if exit_code == 'timeout' else 0)

    def test_folder_retarget_immediately_before_open_dispatch_rejects(self):
        proof = self.adapter.verify_existing(ID, self.folder)
        self.project.rename(self.root / 'old-orchard')
        self.project.mkdir()
        with patch.object(native.subprocess, 'Popen', side_effect=AssertionError('must not dispatch')):
            self.assertEqual(self.adapter.open_existing(proof).outcome, 'rejected')


class BuildTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'macOS build verification uses POSIX O_NOFOLLOW and executable modes.')
    def test_fixed_bundle_version_and_actual_binary_content_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory).resolve() / 'Fictional.app'
            resources = app / 'Contents/Resources'
            resources.mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': native.VERSION,
                'CFBundleVersion': native.BUILD}))
            asar = resources / 'app.asar'
            asar.write_bytes(b'fictional desktop build')
            binary = resources / 'fictional-native-binary'
            binary.write_bytes(b'fictional executable content')
            binary.chmod(0o700)
            with patch.object(native, 'APP', app), patch.object(native, 'BINARY', binary), \
                    patch.object(native.sys, 'platform', 'darwin'), \
                    patch.object(native, 'ASAR_SHA256', hashlib.sha256(asar.read_bytes()).hexdigest()), \
                    patch.object(native, 'BINARY_SHA256', hashlib.sha256(binary.read_bytes()).hexdigest()):
                self.assertTrue(native._installed_build().available)
                binary.chmod(0o600)
                self.assertFalse(native._installed_build().available)
                binary.chmod(0o700)
                binary.write_bytes(b'different executable')
                self.assertFalse(native._installed_build().available)
                with patch.object(native.sys, 'platform', 'fictional-os'):
                    self.assertEqual(native._installed_build().code, 'unsupported_platform')


# A real local Python child simulates JSONL transport and owned startup children.
# Popen is patched only at the fixed native executable boundary.
PEER = r'''
import json, os, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    if mode == 'timeout':
        time.sleep(10)
    elif mode == 'request':
        print(json.dumps({'id': 'approve-fiction', 'method': 'item/commandExecution/requestApproval', 'params': {}}), flush=True)
    elif mode == 'bad':
        print('not-json FICTIONAL_PRIVATE_OUTPUT', flush=True)
    elif mode == 'oversized':
        print('x' * (1024 * 1024 + 1), flush=True)
    elif mode == 'stderr':
        sys.stderr.write('x' * (8 * 1024 * 1024 + 1)); sys.stderr.flush()
    elif mode == 'wrong-id':
        print(json.dumps({'id': 999, 'result': {}}), flush=True)
    elif mode == 'duplicate':
        print('{"id":1,"id":1,"result":{}}', flush=True)
    elif mode == 'generation':
        print(json.dumps({'method': 'turn/started', 'params': {}}), flush=True)
    elif mode == 'frames':
        for _ in range(513):
            print(json.dumps({'method': 'thread/status/changed', 'params': {}}), flush=True)
    else:
        print(json.dumps({'method': 'mcpServer/startupStatus/updated', 'params': {}}), flush=True)
        print(json.dumps({'id': request['id'], 'result': {'fictional': True}}), flush=True)
'''


@unittest.skipIf(os.name == 'nt', 'Native stdio transport uses POSIX pipe selectors and owned process groups.')
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = freeze(Path(self.temp.name).resolve())
        self.real_popen = subprocess.Popen
        self.calls = []

    def transport(self, mode='ok', timeout=1):
        def spawn(argv, **kwargs):
            self.calls.append(argv)
            return self.real_popen([sys.executable, '-u', '-c', PEER, mode], **kwargs)
        with patch.object(native.subprocess, 'Popen', side_effect=spawn):
            transport = native.StdioTransport(self.folder, timeout=timeout)
        self.addCleanup(transport.close)
        return transport

    def test_only_fixed_binary_argv_and_allowed_methods(self):
        transport = self.transport()
        self.assertEqual(transport.request('initialize', {}), {'fictional': True})
        transport.notify_initialized()
        self.assertEqual(transport.request('thread/name/set', {'threadId': ID, 'name': 'New chat'}), {'fictional': True})
        with self.assertRaises(native.ProtocolError):
            transport.request('turn/start', {})
        self.assertEqual(self.calls, [[str(native.BINARY), 'app-server', '--listen', 'stdio://']])
        self.assertFalse(any('sandbox' in item or 'approval' in item for item in self.calls[0]))

    def test_malformed_oversized_unexpected_request_and_generation_refuse(self):
        for mode in ('bad', 'oversized', 'stderr', 'wrong-id', 'duplicate', 'request', 'generation', 'frames'):
            with self.subTest(mode=mode):
                transport = self.transport(mode)
                with self.assertRaises(native.ProtocolError) as caught:
                    transport.request('initialize', {})
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(transport.sequence, 1)  # No approval response was sent.
                transport.close()
                self.assertIsNotNone(transport.process.returncode)

    def test_timeout_has_absolute_budget_and_child_is_reaped(self):
        transport = self.transport('timeout', timeout=.1)
        started = time.monotonic()
        with self.assertRaises(native.ProtocolError):
            transport.request('thread/start', {'cwd': self.folder.path})
        transport.close()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(transport.process.returncode)
        transport.close()

    def test_close_stops_owned_startup_descendant_even_when_leader_exits(self):
        marker = Path(self.temp.name) / 'fictional-child.pid'
        script = ('import os,time,sys,signal\npid=os.fork()\nif pid==0:\n'
                  ' signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                  ' with open(sys.argv[1]+".part","w") as marker:\n'
                  '  marker.write(str(os.getpid()))\n'
                  ' os.replace(sys.argv[1]+".part",sys.argv[1])\n time.sleep(30)\n')
        def spawn(argv, **kwargs):
            return self.real_popen([sys.executable, '-c', script, str(marker)], **kwargs)
        with patch.object(native.subprocess, 'Popen', side_effect=spawn):
            transport = native.StdioTransport(self.folder)
        self.addCleanup(transport.close)
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        child = int(marker.read_text())
        self.addCleanup(lambda: self.kill_if_alive(child))
        transport.close()
        self.assertIsNotNone(transport.process.returncode)
        # A zombie may briefly remain with init; no live descendant may survive.
        result = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True)
        self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'))

    @staticmethod
    def kill_if_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == '__main__':
    unittest.main()
