"""Managed service ordering uses fictional providers and files, never a CLI."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_service import NewChatService, RETENTION
from new_chat_types import Capability, ExecutionResult, NewChatError, VerifiedIdentity
import test_new_chat_service as fixtures

wait_for = fixtures.wait_for


class ManagedAdapter:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder
        self.run_id, self.native_id, self.message_id = [str(uuid.uuid4()) for _ in range(3)]
        self.version = 'fictional-managed-v1'
        self.calls = []
        self.metadata = False
        self.echo = True
        self.lost_launch = False
        self.source_provider = 'claude_code'
        self.state, self.permissions = 'ready', False
        self.launch_entered, self.launch_release = threading.Event(), threading.Event()
        self.launch_release.set()
        self.before_launch = self.before_send = None
        self.executed = False

    def preflight(self):
        self.calls.append('preflight')
        return Capability(True, version=self.version, supports_work=True,
                          supports_blank=False, ownership='watch_managed')

    def reserve(self, folder, attempt_id):
        self.calls.append('reserve')
        self.attempt_id = attempt_id
        return self.run_id

    def launch_once(self, run_id, folder, receipt):
        self.calls.append('launch')
        assert run_id == self.run_id
        if self.before_launch:
            self.before_launch()
        self.launch_entered.set()
        self.launch_release.wait(3)
        if self.lost_launch:
            raise RuntimeError('FICTIONAL PRIVATE PROVIDER FAILURE')
        receipt(self.native_id, folder.path)

    def start_managed_work_once(self, run_id, text, claim, receipt):
        assert run_id == self.run_id
        if self.before_send:
            self.before_send()
        claim()
        self.calls.extend(['claim', 'send'])
        self.executed = True
        if self.echo:
            receipt(self.message_id)
        return self.managed_execution_status(run_id)

    def managed_status(self, run_id):
        self.calls.append('status')
        assert run_id == self.run_id
        return {'runId': run_id, 'state': self.state, 'recoveryRequired': self.state == 'uncertain',
                'permissionsAvailable': self.permissions, 'ignoredSecret': 'FICTIONAL PRIVATE TOKEN'}

    def managed_execution_status(self, run_id):
        self.calls.append('execution_status')
        assert run_id == self.run_id
        return ExecutionResult('accepted' if self.executed and self.echo else 'unknown',
            self.message_id if self.executed and self.echo else None, receipt_kind='native_user_message')

    def verify_existing(self, native_id, folder):
        self.calls.append('verify')
        assert native_id == self.native_id
        if not self.metadata:
            raise RuntimeError('No fictional transcript yet')
        source = self.root / 'fictional-managed.jsonl'
        if not source.exists():
            source.write_text(json.dumps({'id': native_id, 'cwd': folder.path}) + '\n')
        st, directory = source.stat(), self.folder.stat()
        return VerifiedIdentity(native_id, folder.path, str(source), st.st_dev, st.st_ino,
            self.version, directory.st_dev, directory.st_ino, provider=self.source_provider)

    def close(self):
        self.launch_release.set()


@unittest.skipIf(os.name == 'nt', 'Private POSIX storage contract.')
class ManagedServiceTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    cleanup_services = fixtures.ServiceTests.cleanup_services

    def service(self, *, enabled=True, demo=False):
        if not hasattr(self, 'managed'):
            self.managed = ManagedAdapter(self.root, self.folder)
        service = NewChatService(self.root, {'codex': self.adapter, 'claude_code': self.managed},
            lambda: [self.seed], enabled=enabled, demo=demo,
            clock=lambda: self.clocks.mono, wall=lambda: self.clocks.wall)
        self.services.append(service)
        return service

    def request(self, service):
        folder = service.context()['folders'][0]
        selection = dict(provider='claude_code', folderId=folder['id'], folderRevision=folder['revision'], mode='work')
        intent = service.prepare(selection)
        return dict(selection, requestId=intent['requestId'], instruction='FICTIONAL PRIVATE FIRST INSTRUCTION')

    def settle(self, service, attempt):
        wait_for(lambda: not service.jobs)
        return service.status(attempt['attemptId'])

    def test_capabilities_are_work_only_with_desktop_codex_compatibility(self):
        service = self.service()
        providers = {row['id']: row for row in service.context()['providers']}
        self.assertEqual(service.context()['limits']['managedPermissionTimeoutSeconds'], 120)
        self.assertEqual((providers['claude_code']['ownership'], providers['claude_code']['supportsBlank'],
                          providers['claude_code']['supportsWork'], providers['claude_code']['supportsDraft']),
                         ('watch_managed', False, True, False))
        self.assertEqual(providers['codex']['ownership'], 'desktop')
        request = self.request(service)
        selection = {key: request[key] for key in ('provider', 'folderId', 'folderRevision')}
        with self.assertRaises(NewChatError) as caught:
            service.prepare(selection)
        self.assertEqual(caught.exception.code, 'blank_unavailable')
        self.assertNotIn('reserve', self.managed.calls)

    def test_handle_and_permissions_available_before_native_or_transcript_receipt(self):
        service = self.service()
        request = self.request(service)
        persisted = []
        def before_launch():
            reader = sqlite3.connect(service.root / 'journal.sqlite')
            raw = json.loads(reader.execute('SELECT data FROM attempts').fetchone()[0])
            reader.close()
            persisted.append(raw['managed']['runId'])
            self.assertIsNone(raw['native_id'])
        self.managed.before_launch = before_launch
        self.managed.launch_release.clear()
        _, attempt = service.open(request)
        self.assertTrue(self.managed.launch_entered.wait(1))
        self.managed.permissions = True
        self.managed.state = 'needs_attention'
        early = service.status(attempt['attemptId'])
        self.assertEqual(persisted, [self.managed.run_id])
        self.assertEqual(early['managed'], {'runId': self.managed.run_id, 'state': 'needs_attention',
                                         'recoveryRequired': False, 'permissionsAvailable': True})
        self.assertEqual(early['uiOpen'], {'attempts': 0, 'outcome': 'not_required'})
        self.assertIsNone(early['existingSession'])
        self.assertIsNone(early['session'])
        self.assertEqual(early['creation']['outcome'], 'unknown')
        self.assertEqual(early['execution']['outcome'], 'not_attempted')
        self.assertNotIn('PRIVATE TOKEN', json.dumps(early))
        service.process_admissions(lambda *args: self.fail('Unverified metadata was admitted'))
        self.assertNotIn('verify', self.managed.calls)
        self.managed.launch_release.set()
        self.settle(service, attempt)

    def test_first_instruction_claim_precedes_metadata_and_never_opens_desktop(self):
        service = self.service()
        request = self.request(service)
        claims = []
        original_claim = service._execution_claim
        def claim(ident):
            original_claim(ident)
            reader = sqlite3.connect(service.root / 'journal.sqlite')
            row = json.loads(reader.execute('SELECT data FROM attempts').fetchone()[0])
            reader.close()
            claims.append((row['execution']['attempts'], row['creation'], row['launch'], row['ui']))
        service._execution_claim = claim
        _, attempt = service.open(request)
        final = self.settle(service, attempt)
        self.assertEqual(claims, [(1, 'unknown', 'accepted', 'not_required')])
        events = [item for item in self.managed.calls if item in ('reserve', 'launch', 'claim', 'send', 'verify')]
        self.assertEqual(events[:5], ['reserve', 'launch', 'claim', 'send', 'verify'])
        self.assertEqual(final['execution']['receiptKind'], 'native_user_message')
        self.assertEqual(final['execution']['outcome'], 'accepted')
        self.assertIsNone(final['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (0, 0))
        self.assertNotIn('PRIVATE FIRST INSTRUCTION', (service.root / 'journal.sqlite').read_bytes().decode('latin1'))

    def test_admission_requires_independent_provider_metadata_and_claude_room_prefix(self):
        service = self.service()
        _, attempt = service.open(self.request(service))
        self.settle(service, attempt)
        service.process_admissions(lambda *args: self.fail('Missing metadata admitted'))
        self.managed.metadata = True
        self.managed.source_provider = 'codex'
        service.process_admissions(lambda *args: self.fail('Wrong provider metadata admitted'))
        self.assertIsNone(service.status(attempt['attemptId'])['existingSession'])
        self.managed.source_provider = 'claude_code'
        service.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        self.assertIsNone(service.status(attempt['attemptId'])['session'])
        service.process_admissions(lambda identity, folder, permit: 'c:' + identity.native_id if permit() else None)
        final = service.status(attempt['attemptId'])
        self.assertEqual(final['session']['roomAgentId'], 'c:' + self.managed.native_id)
        self.assertEqual(final['uiOpen'], {'attempts': 0, 'outcome': 'not_required'})
        self.assertEqual(self.managed.calls.count('send'), 1)

    def test_lost_launch_receipt_restart_is_read_only_and_retains_run_handle(self):
        service = self.service()
        request = self.request(service)
        self.managed.lost_launch = True
        _, attempt = service.open(request)
        before = self.settle(service, attempt)
        service.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('Native identity was guessed'))
        self.assertEqual(restored.open(request)[0], 200)
        after = restored.status(attempt['attemptId'])
        self.assertEqual(after['managed']['runId'], before['managed']['runId'])
        self.assertIsNone(after['existingSession'])
        self.assertEqual((self.managed.calls.count('reserve'), self.managed.calls.count('launch'),
                          self.managed.calls.count('send')), (1, 1, 0))
        self.assertNotIn('PRIVATE PROVIDER FAILURE', json.dumps(after))

    def test_lost_message_receipt_reconciles_without_metadata_or_resubmission(self):
        service = self.service()
        self.managed.echo = False
        request = self.request(service)
        _, attempt = service.open(request)
        before = self.settle(service, attempt)
        self.assertEqual(before['execution']['outcome'], 'unknown')
        self.assertIsNone(before['execution']['turnId'])
        service.close()
        restored = self.service()
        self.managed.echo = True
        after = restored.status(attempt['attemptId'])
        self.assertEqual(after['execution']['outcome'], 'accepted')
        self.assertEqual(after['execution']['turnId'], self.managed.message_id)
        self.assertIsNone(after['existingSession'])
        self.assertEqual(restored.open(request)[0], 200)
        self.assertEqual(self.managed.calls.count('send'), 1)

    def test_changed_folder_stops_first_instruction_claim(self):
        service = self.service()
        request = self.request(service)
        def replace_folder():
            self.folder.rename(self.root / 'old-fictional-project')
            self.folder.mkdir()
        self.managed.before_send = replace_folder
        _, attempt = service.open(request)
        final = self.settle(service, attempt)
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertNotIn('send', self.managed.calls)

    def test_queue_acceptance_without_native_receipt_never_becomes_accepted(self):
        service = self.service()
        self.managed.echo = False
        def queue_only(run_id, instruction, claim, receipt):
            claim()
            self.managed.calls.append('send')
            self.managed.executed = True
            # Deliberately broken provider: queueing cannot establish receipt.
            return ExecutionResult('accepted', receipt_kind='native_user_message')
        self.managed.start_managed_work_once = queue_only
        _, attempt = service.open(self.request(service))
        final = self.settle(service, attempt)
        self.assertEqual(final['execution']['outcome'], 'unknown')
        self.assertIsNone(final['execution']['turnId'])
        self.assertIsNone(final['execution']['receiptKind'])
        self.assertEqual(self.managed.calls.count('send'), 1)

    def test_nul_instruction_rejects_before_reserve(self):
        service = self.service()
        request = self.request(service)
        with self.assertRaises(NewChatError) as caught:
            service.open(dict(request, instruction='fictional\0instruction'))
        self.assertEqual(caught.exception.code, 'invalid_instruction')
        self.assertNotIn('reserve', self.managed.calls)

    def test_handle_write_failure_prevents_native_launch(self):
        service = self.service()
        request = self.request(service)
        original_save = service._save
        failed = []
        def save(row):
            if row.get('managed') and not failed:
                failed.append(True)
                raise sqlite3.OperationalError('Fictional durability failure')
            original_save(row)
        service._save = save
        _, attempt = service.open(request)
        final = self.settle(service, attempt)
        self.assertEqual(self.managed.calls.count('reserve'), 1)
        self.assertNotIn('launch', self.managed.calls)
        self.assertNotIn('send', self.managed.calls)
        self.assertIsNone(final['managed'])

    def test_managed_binding_survives_attempt_expiry_without_provider_io(self):
        service = self.service()
        _, attempt = service.open(self.request(service))
        self.settle(service, attempt)
        calls = list(self.managed.calls)
        self.clocks.advance(RETENTION * 3)
        with self.assertRaises(NewChatError) as caught:
            service.status(attempt['attemptId'])
        self.assertEqual(caught.exception.status, 410)
        row = json.loads(service.db.execute('SELECT data FROM attempts').fetchone()[0])
        self.assertEqual(row['managed']['runId'], self.managed.run_id)
        self.assertEqual(self.managed.calls, calls)

    def test_disabled_demo_perform_zero_provider_calls_including_restored_handle(self):
        service = self.service()
        _, attempt = service.open(self.request(service))
        self.settle(service, attempt)
        service.close()
        self.managed.calls.clear()
        for options in ({'enabled': False}, {'demo': True}):
            blocked = self.service(**options)
            self.assertEqual(blocked.context()['folders'], [])
            blocked.status(attempt['attemptId'])
            blocked.process_admissions(lambda *args: self.fail('Disabled provider admitted'))
            with self.assertRaises(NewChatError):
                blocked.open({'provider': 'claude_code', 'folderId': 'fictional', 'folderRevision': 'fictional',
                              'requestId': str(uuid.uuid4()), 'mode': 'work', 'instruction': 'fictional'})
            blocked.close()
        self.assertEqual(self.managed.calls, [])


if __name__ == '__main__':
    unittest.main()
