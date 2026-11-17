"""Durable New chat behavior using injected fictional native identities only."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_service import NewChatService, RETENTION
from new_chat_types import Capability, NewChatError, OpenResult, VerifiedIdentity


def wait_for(fn):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(.005)
    raise AssertionError('Fictional worker did not settle.')


class Clocks:
    def __init__(self):
        self.wall, self.mono = 1900000000., 1000.

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class FakeAdapter:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder
        self.preflights, self.creates, self.opens, self.verifies = 0, 0, 0, 0
        self.version, self.available = 'fixture-v1', True
        self.identities = {}
        self.create_entered, self.create_release = threading.Event(), threading.Event()
        self.create_release.set()
        self.open_result = OpenResult('accepted')
        self.after_receipt = None
        self.before_receipt = None
        self.wrong_folder = None
        self.closed = False

    def make_identity(self, folder=None):
        folder = folder or self.folder
        ident = str(uuid.uuid4())
        source = self.root / (ident + '.jsonl')
        source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': ident, 'cwd': str(folder)}}) + '\n')
        st, directory = source.stat(), folder.stat()
        identity = VerifiedIdentity(ident, str(folder), str(source), st.st_dev, st.st_ino,
                                    self.version, directory.st_dev, directory.st_ino)
        self.identities[ident] = identity
        return identity

    def preflight(self):
        self.preflights += 1
        return Capability(self.available, 'native_defaults_unverified' if not self.available else '', version=self.version)

    def create_once(self, folder, callback):
        self.creates += 1
        self.create_entered.set()
        self.create_release.wait(3)
        if self.before_receipt:
            self.before_receipt()
        identity = self.make_identity(self.wrong_folder or Path(folder.path))
        callback(identity.native_id, identity.cwd)
        if self.after_receipt:
            self.after_receipt(identity)
        return identity

    def verify_existing(self, native_id, folder):
        self.verifies += 1
        return self.identities.get(native_id)

    def open_existing(self, identity, draft):
        self.opens += 1
        return self.open_result

    def close(self):
        self.closed = True
        self.create_release.set()


@unittest.skipIf(os.name == 'nt', 'New chat uses private POSIX storage; Windows native launch is unavailable.')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = self.root / 'fictional-project'
        self.folder.mkdir()
        self.adapter = FakeAdapter(self.root, self.folder)
        self.seed = self.adapter.make_identity()
        self.clocks = Clocks()
        self.services = []
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        for service in self.services:
            service.close()

    def service(self, **kwargs):
        s = NewChatService(self.root, self.adapter, lambda: [self.seed],
                           enabled=kwargs.pop('enabled', True), clock=lambda: self.clocks.mono,
                           wall=lambda: self.clocks.wall, **kwargs)
        self.services.append(s)
        return s

    def request(self, service):
        folder = service.context()['folders'][0]
        selection = {'provider': 'codex', 'folderId': folder['id'], 'folderRevision': folder['revision']}
        intent = service.prepare(selection)
        return dict(selection, requestId=intent['requestId'])

    def settle(self, s):
        wait_for(lambda: not s.jobs)

    def test_disabled_and_demo_short_circuit_all_native_reads_and_creation(self):
        for kwargs in ({'enabled': False}, {'demo': True}):
            s = self.service(**kwargs)
            self.assertEqual(s.context()['folders'], [])
            self.assertFalse(s.context()['providers'][0]['available'])
            with self.assertRaises(NewChatError):
                s.prepare({'provider': 'codex', 'folderId': 'fake', 'folderRevision': 'fake'})
            s.close()
        self.assertEqual((self.adapter.preflights, self.adapter.verifies, self.adapter.creates), (0, 0, 0))

    def test_async_one_creation_and_duplicate_click_replay(self):
        s = self.service()
        request = self.request(s)
        self.adapter.create_release.clear()
        status, result = s.open(request)
        self.assertEqual(status, 202)
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(self.adapter.create_entered.wait(1))
        results = []
        threads = [threading.Thread(target=lambda: results.append(s.open(request))) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 and out['attemptId'] == result['attemptId'] for code, out in results))
        self.assertEqual(self.adapter.creates, 1)
        self.adapter.create_release.set()
        self.settle(s)

    def test_omitted_empty_draft_replay_same_but_changed_draft_conflicts(self):
        s = self.service()
        request = self.request(s)
        s.open(request)
        self.assertEqual(s.open(dict(request, draft=''))[0], 200)
        with self.assertRaises(NewChatError) as caught:
            s.open(dict(request, draft='different'))
        self.assertEqual(caught.exception.status, 409)
        self.settle(s)

    def test_receipt_durable_before_adapter_next_mutation_and_no_draft_in_store(self):
        s = self.service()
        request = self.request(s)
        observations = []
        def after_receipt(identity):
            db = sqlite3.connect(s.root / 'journal.sqlite')
            raw = db.execute('SELECT data FROM attempts').fetchone()[0]
            db.close()
            observations.append(json.loads(raw)['native_id'])
        self.adapter.after_receipt = after_receipt
        _, result = s.open(dict(request, draft='PRIVATE FICTIONAL DRAFT'))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(observations, [out['existingSession']['id']])
        self.assertNotIn('PRIVATE FICTIONAL DRAFT', (s.root / 'journal.sqlite').read_bytes().decode('latin1'))
        self.assertEqual(out['draft']['disposition'], 'prefill_requested')

    def test_success_only_after_exact_blank_room_acknowledgement(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.assertIsNone(s.status(result['attemptId'])['session'])
        s.process_admissions(lambda identity, folder, permit: 'wrong-room-id' if permit() else None)
        self.assertEqual(s.status(result['attemptId'])['state'], 'waiting')
        s.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'added')
        self.assertEqual(out['session']['roomAgentId'], 'x:' + out['existingSession']['id'])
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_creation_success_ui_failure_has_existing_session_and_no_admission(self):
        s = self.service()
        self.adapter.open_result = OpenResult('rejected')
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual((out['state'], out['creation']['outcome'], out['uiOpen']['outcome']), ('failed', 'verified', 'rejected'))
        self.assertEqual(out['reason']['code'], 'session_created_ui_open_failed')
        self.assertIsNotNone(out['existingSession'])
        self.assertIsNone(out['session'])
        self.assertFalse(out['capabilities']['openExisting'])
        self.assertEqual(s.observe(result['attemptId'], {'action': 'stop'}), out)
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_lost_creation_response_never_repeated_after_restart(self):
        s = self.service()
        self.adapter.before_receipt = lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE RAW ERROR'))
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        self.adapter.closed = False
        restored = self.service()
        self.assertEqual(restored.open(request)[0], 200)
        self.assertEqual(self.adapter.creates, 1)
        self.assertNotIn('PRIVATE RAW ERROR', json.dumps(restored.status(result['attemptId'])))
        self.assertEqual(restored.status(result['attemptId'])['creation']['outcome'], 'unknown')

    def test_process_loss_after_native_creation_before_receipt_has_no_recovery_guess(self):
        s = self.service()
        # SystemExit terminates only the fictional worker, preserving precisely
        # the journal at this boundary instead of running exception recovery.
        s._receipt = lambda *args: (_ for _ in ()).throw(SystemExit())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        self.assertEqual(len(self.adapter.identities), 2)  # seed + unrecorded native creation
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('An unrecorded native ID was guessed'))
        self.assertEqual(restored.open(request)[0], 200)
        self.assertIsNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_process_loss_after_receipt_recovery_does_not_repeat_naming_or_open(self):
        s = self.service()
        self.adapter.after_receipt = lambda _: (_ for _ in ()).throw(SystemExit())
        _, result = s.open(self.request(s))
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('A never-opened chat was admitted'))
        self.assertIsNotNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_process_loss_after_ui_claim_never_reopens_on_recovery(self):
        s = self.service()
        self.adapter.open_existing = lambda *args: (_ for _ in ()).throw(SystemExit())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('Unknown UI handoff was treated as accepted'))
        restored.open(request)
        out = restored.status(result['attemptId'])
        self.assertEqual(out['uiOpen'], {'attempts': 1, 'outcome': 'unknown'})
        self.assertEqual(out['reason']['code'], 'ui_open_outcome_unknown')
        self.assertIsNotNone(out['existingSession'])
        self.assertEqual(self.adapter.creates, 1)

    def test_recorded_receipt_after_creation_exception_recovers_only_by_read(self):
        s = self.service()
        self.adapter.after_receipt = lambda _: (_ for _ in ()).throw(RuntimeError())
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        s.close()
        restored = self.service()
        restored.process_admissions(lambda *args: self.fail('UI was not opened; no admission'))
        self.assertIsNotNone(restored.status(result['attemptId'])['existingSession'])
        self.assertEqual((self.adapter.creates, self.adapter.opens), (1, 0))

    def test_late_receipt_after_stop_is_retained_but_never_associated(self):
        s = self.service()
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        stopped = s.observe(result['attemptId'], {'action': 'stop'})
        self.assertEqual(stopped['observation'], 'stopped')
        self.adapter.create_release.set()
        self.settle(s)
        calls = []
        s.process_admissions(lambda *args: calls.append(args))
        out = s.status(result['attemptId'])
        self.assertEqual(calls, [])
        self.assertEqual(out['observation'], 'stopped')
        self.assertIsNotNone(out['existingSession'])

    def test_admission_and_stop_linearize(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        entered, release = threading.Event(), threading.Event()
        def admit(identity, folder, permit):
            entered.set()
            release.wait(2)
            return 'x:' + identity.native_id if permit() else None
        worker = threading.Thread(target=lambda: s.process_admissions(admit))
        worker.start()
        self.assertTrue(entered.wait(1))
        outputs = []
        stopper = threading.Thread(target=lambda: outputs.append(s.observe(result['attemptId'], {'action': 'stop'})))
        stopper.start()
        release.set()
        worker.join(2)
        stopper.join(2)
        self.assertEqual(outputs[0]['state'], 'added')

    def test_timeout_continue_stop_no_native_retry_and_no_window_extension_on_restart(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.advance(120)
        self.assertEqual(s.status(result['attemptId'])['state'], 'timeout')
        continued = s.observe(result['attemptId'], {'action': 'continue'})
        deadline = continued['deadlineAt']
        self.clocks.advance(25)
        s.close()
        restored = self.service()
        self.assertEqual(restored.status(result['attemptId'])['deadlineAt'], deadline)
        restored.observe(result['attemptId'], {'action': 'stop'})
        self.clocks.advance(120)
        restored.process_admissions(lambda *args: self.fail('Stopped request admitted'))
        self.assertEqual(self.adapter.creates, 1)

    def test_keep_waiting_is_offered_only_where_continue_can_run(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.advance(120)
        view = s.status(result['attemptId'])
        self.assertEqual(view['state'], 'timeout')
        self.assertTrue(view['capabilities']['keepWaiting'])
        s.close()
        off = self.service(enabled=False)
        view = off.status(result['attemptId'])
        self.assertEqual(view['state'], 'timeout')
        self.assertFalse(view['capabilities']['keepWaiting'], 'a disabled host must not advertise Keep waiting')
        with self.assertRaises(NewChatError):
            off.observe(result['attemptId'], {'action': 'continue'})
        self.assertEqual(off.observe(result['attemptId'], {'action': 'stop'})['observation'], 'stopped', 'Stop stays local')
        off.close()
        on = self.service()
        self.assertFalse(on.status(result['attemptId'])['capabilities']['keepWaiting'], 'stopped stays stopped')
        self.assertEqual(self.adapter.creates, 1)

    def test_expired_intent_cannot_authorize_and_expired_details_redacted(self):
        s = self.service()
        request = self.request(s)
        self.clocks.advance(301)
        with self.assertRaises(NewChatError):
            s.open(request)
        self.assertEqual(self.adapter.creates, 0)
        request = self.request(s)
        _, result = s.open(request)
        self.settle(s)
        self.clocks.advance(RETENTION)
        with self.assertRaises(NewChatError) as caught:
            s.status(result['attemptId'])
        self.assertEqual(caught.exception.status, 410)
        self.assertEqual(s.db.execute('SELECT data FROM attempts').fetchone()[0], '{"expired":true}')
        with self.assertRaises(NewChatError):
            s.open(request)
        self.clocks.advance(RETENTION)
        s.context()
        with self.assertRaises(NewChatError) as caught:
            s.status(result['attemptId'])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.adapter.creates, 1)

    def test_capacity_cannot_be_bypassed_by_stopping_blocked_worker(self):
        s = self.service(max_active=1)
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        s.observe(result['attemptId'], {'action': 'stop'})
        another = self.request(s)
        with self.assertRaises(NewChatError) as caught:
            s.open(another)
        self.assertEqual(caught.exception.status, 429)
        self.adapter.create_release.set()
        self.settle(s)

    def test_unknown_observe_ids_do_not_allocate_unbounded_gates(self):
        s = self.service()
        for _ in range(20):
            with self.assertRaises(NewChatError):
                s.observe(str(uuid.uuid4()), {'action': 'stop'})
        self.assertEqual(s.gates, {})

    def test_wrong_folder_receipt_requires_independent_proof_for_mismatch(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'mismatch')
        self.assertEqual(out['mismatch']['displayPath'], str(other))
        self.assertIsNone(out['existingSession'])
        self.assertIsNone(out['session'])
        self.assertEqual(self.adapter.opens, 0)

    def test_wrong_folder_receipt_without_persisted_proof_remains_uncertain(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        self.adapter.verify_existing = lambda *args: None
        _, result = s.open(self.request(s))
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'waiting')
        self.assertIsNone(out['mismatch'])
        self.assertEqual(self.adapter.opens, 0)

    def test_late_wrong_folder_identity_cannot_override_timeout(self):
        s = self.service()
        other = self.root / 'another-project'
        other.mkdir()
        self.adapter.wrong_folder = other
        self.adapter.create_release.clear()
        _, result = s.open(self.request(s))
        self.assertTrue(self.adapter.create_entered.wait(1))
        self.clocks.advance(120)
        self.adapter.create_release.set()
        self.settle(s)
        out = s.status(result['attemptId'])
        self.assertEqual(out['state'], 'timeout')
        self.assertIsNone(out['mismatch'])
        self.assertIsNone(out['existingSession'])

    def test_native_id_cannot_bind_a_second_attempt(self):
        s = self.service()
        _, first = s.open(self.request(s))
        self.settle(s)
        original = s.status(first['attemptId'])['existingSession']['id']
        s.observe(first['attemptId'], {'action': 'stop'})
        identity = self.adapter.identities[original]
        self.adapter.make_identity = lambda folder=None: identity
        _, second = s.open(self.request(s))
        self.settle(s)
        out = s.status(second['attemptId'])
        self.assertIsNone(out['existingSession'])
        self.assertEqual(self.adapter.opens, 1)
        with s.lock:
            self.assertIsNone(s._load(second['attemptId'])['native_id'])
        self.assertEqual(s.status(first['attemptId'])['existingSession']['id'], original)

    def test_wall_clock_discontinuity_stops_observation(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        self.clocks.wall -= 30
        out = s.status(result['attemptId'])
        self.assertEqual((out['state'], out['reason']['code']), ('timeout', 'clock_uncertain'))

    def test_receipt_write_failure_prevents_later_adapter_mutations(self):
        s = self.service()
        s._receipt = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('fictional disk full'))
        named = []
        self.adapter.after_receipt = lambda identity: named.append(identity)
        _, result = s.open(self.request(s))
        self.settle(s)
        self.assertEqual(named, [])
        self.assertEqual(self.adapter.opens, 0)
        self.assertEqual(s.status(result['attemptId'])['creation']['outcome'], 'unknown')

    def test_final_publication_permit_checks_deadline_before_side_effect(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        publications = []
        def admit(identity, folder, permit):
            self.clocks.advance(120)
            if not permit():
                return None
            publications.append(identity.native_id)
            return 'x:' + identity.native_id
        s.process_admissions(admit)
        self.assertEqual(s.status(result['attemptId'])['state'], 'timeout')
        self.assertEqual(publications, [])

    def test_permitted_publication_is_not_retroactively_expired_after_callback(self):
        s = self.service()
        _, result = s.open(self.request(s))
        self.settle(s)
        def admit(identity, folder, permit):
            self.assertTrue(permit())
            self.clocks.advance(120)
            return 'x:' + identity.native_id
        s.process_admissions(admit)
        self.assertEqual(s.status(result['attemptId'])['state'], 'added')

    def test_folder_replacement_between_prepare_and_open_rejects_before_native(self):
        s = self.service()
        request = self.request(s)
        self.folder.rename(self.root / 'previous-project')
        self.folder.mkdir()
        with self.assertRaises(NewChatError):
            s.open(request)
        self.assertEqual(self.adapter.creates, 0)

    def test_two_process_writers_are_rejected_and_hmac_survives_restart(self):
        s = self.service()
        key = s.key
        with self.assertRaises((OSError, ValueError)):
            self.service()
        s.close()
        restored = self.service()
        self.assertEqual(restored.key, key)

    def test_adapter_unavailable_settings_reject_before_any_claim(self):
        s = self.service()
        self.adapter.available = False
        folder = s.context()['folders'][0]
        with self.assertRaises(NewChatError) as caught:
            s.prepare({'provider': 'codex', 'folderId': folder['id'], 'folderRevision': folder['revision']})
        self.assertEqual(caught.exception.code, 'native_defaults_unverified')
        self.assertEqual(self.adapter.creates, 0)


class WorkAdapter(FakeAdapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.submissions = 0
        self.turn_id = str(uuid.uuid4())
        self.submit_entered, self.submit_release = threading.Event(), threading.Event()
        self.submit_release.set()
        self.lost_reply = False
        self.completed = False
        self.owner_missing = False

    def preflight(self):
        return Capability(True, version=self.version, supports_work=True)

    def start_work_once(self, identity, instruction, claim, receipt):
        from new_chat_types import ExecutionResult
        self.submit_entered.set()
        self.submit_release.wait(3)
        if self.owner_missing:
            return ExecutionResult('not_attempted', code='owner_unavailable')
        try:
            claim()
        except NewChatError:
            return ExecutionResult('not_attempted')
        self.submissions += 1
        if self.lost_reply:
            return ExecutionResult('unknown', code='execution_unknown')
        receipt(self.turn_id)
        return ExecutionResult('accepted', self.turn_id)

    def execution_status(self, identity, turn_id):
        from new_chat_types import ExecutionResult
        return ExecutionResult('completed', turn_id) if self.completed else None


class WorkingServiceTests(unittest.TestCase):
    setUp = ServiceTests.setUp
    cleanup_services = ServiceTests.cleanup_services
    service = ServiceTests.service

    def working(self):
        self.adapter = WorkAdapter(self.root, self.folder)
        s = self.service()
        folder = s.context()['folders'][0]
        choice = dict(provider='codex', folderId=folder['id'], folderRevision=folder['revision'], mode='work')
        intent = s.prepare(choice)
        return s, dict(choice, requestId=intent['requestId'], instruction='FICTIONAL_PRIVATE_INSTRUCTION')

    def settled(self, service, attempt_id):
        wait_for(lambda: attempt_id not in service.jobs)
        return service.status(attempt_id)

    def test_explicit_mode_instruction_validation_and_backwards_compatible_blank(self):
        service, request = self.working()
        for change in ({'mode': 'other'}, {'instruction': ''}, {'instruction': '  '},
                       {'instruction': 'x' * 8001}, {'draft': 'not an instruction'}, {'instruction': None}):
            with self.assertRaises(NewChatError):
                service.open(dict(request, **change))
        with self.assertRaises(NewChatError) as caught:
            service.open(dict(request, mode='blank', instruction='fictional'))
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(self.adapter.creates, 0)

    def test_work_mode_is_frozen_before_any_mutation_and_not_a_draft(self):
        service, request = self.working()
        blank = dict(request, mode='blank')
        blank.pop('instruction')
        with self.assertRaises(NewChatError) as caught:
            service.open(blank)
        self.assertEqual(caught.exception.code, 'intent_conflict')
        self.assertEqual(self.adapter.creates, 0)
        _, initial = service.open(request)
        status = self.settled(service, initial['attemptId'])
        self.assertEqual(status['mode'], 'work')
        self.assertEqual(status['draft']['disposition'], 'not_provided')
        self.assertEqual(status['execution'], {'attempts': 1, 'outcome': 'accepted',
                         'turnId': self.adapter.turn_id, 'receiptKind': 'native_turn', 'reason': None, 'pending': False})
        self.assertEqual((self.adapter.creates, self.adapter.submissions, self.adapter.opens), (1, 1, 1))
        raw = service.db.execute('SELECT data FROM attempts').fetchone()[0]
        self.assertNotIn('PRIVATE_INSTRUCTION', raw)
        self.assertNotIn('PRIVATE_INSTRUCTION', json.dumps(status))

    def test_recovery_by_request_is_read_only_before_and_after_lost_post(self):
        service, request = self.working()
        recovered = service.request_status(request['requestId'])
        self.assertEqual((recovered['status'], recovered['attempt']), ('prepared', None))
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (0, 0))
        _, initial = service.open(request)
        final = self.settled(service, initial['attemptId'])
        recovered = service.request_status(request['requestId'])
        self.assertEqual(recovered['status'], 'attempted')
        self.assertEqual(recovered['attempt'], final)
        for _ in range(3):
            code, same = service.open(request)
            self.assertEqual((code, same['attemptId']), (200, initial['attemptId']))
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))
        with self.assertRaises(NewChatError) as caught:
            service.open(dict(request, instruction='different task'))
        self.assertEqual(caught.exception.code, 'replay_conflict')

    def test_lost_submission_reply_and_restart_never_resend_or_recreate(self):
        service, request = self.working()
        self.adapter.lost_reply = True
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['execution']['outcome'], 'unknown')
        self.assertIsNone(final['execution']['turnId'])
        service.close()
        restarted = self.service()
        recovered = restarted.request_status(request['requestId'])['attempt']
        self.assertEqual(recovered['execution'], final['execution'])
        restarted.open(request)
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))

    def test_execution_survives_observation_timeout_stop_and_restart(self):
        service, request = self.working()
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.clocks.advance(121)
        final = service.status(first['attemptId'])
        self.assertEqual(final['state'], 'timeout')
        self.assertEqual(final['execution']['outcome'], 'accepted')
        service.observe(first['attemptId'], {'action': 'stop'})
        service.close()
        restarted = self.service()
        self.assertEqual(restarted.status(first['attemptId'])['execution']['outcome'], 'unknown')
        self.adapter.completed = True
        final = restarted.status(first['attemptId'])
        self.assertEqual(final['execution']['outcome'], 'completed')
        self.assertEqual(final['observation'], 'stopped')
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))

    def test_no_owner_means_blank_exists_instruction_not_submitted(self):
        service, request = self.working()
        self.adapter.owner_missing = True
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['creation']['outcome'], 'verified')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual(final['execution']['outcome'], 'not_attempted')
        self.assertEqual(final['execution']['reason']['code'], 'owner_unavailable')
        self.assertEqual(self.adapter.submissions, 0)

    def test_folder_mismatch_never_submits_first_turn(self):
        service, request = self.working()
        wrong = self.root / 'other-fictional-project'
        wrong.mkdir()
        self.adapter.wrong_folder = wrong
        _, first = service.open(request)
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'mismatch')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual((self.adapter.opens, self.adapter.submissions), (0, 0))

    def test_deadline_before_first_turn_claim_prevents_late_submission(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        self.clocks.advance(121)
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'timeout')
        self.assertEqual(final['execution']['attempts'], 0)
        self.assertEqual(self.adapter.submissions, 0)

    def test_stop_waiting_does_not_cancel_requested_first_turn(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        service.observe(first['attemptId'], {'action': 'stop'})
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['execution']['attempts'], 1)
        self.assertEqual(final['observation'], 'stopped')
        self.assertEqual(self.adapter.submissions, 1)

    def test_admission_before_desktop_dispatch_does_not_skip_requested_turn(self):
        service, request = self.working()
        self.adapter.submit_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.submit_entered.wait(1))
        service.process_admissions(lambda identity, folder, permit: 'x:' + identity.native_id if permit() else None)
        added = service.status(first['attemptId'])
        self.assertEqual(added['state'], 'added')
        self.assertTrue(added['execution']['pending'])
        self.adapter.submit_release.set()
        final = self.settled(service, first['attemptId'])
        self.assertEqual(final['state'], 'added')
        self.assertEqual(final['execution']['attempts'], 1)
        self.assertEqual(self.adapter.submissions, 1)

    def test_concurrent_work_duplicates_share_creation_and_first_turn_claim(self):
        service, request = self.working()
        self.adapter.create_release.clear()
        _, first = service.open(request)
        self.assertTrue(self.adapter.create_entered.wait(1))
        results = []
        workers = [threading.Thread(target=lambda: results.append(service.open(request))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 and row['attemptId'] == first['attemptId'] for code, row in results))
        self.adapter.create_release.set()
        self.settled(service, first['attemptId'])
        self.assertEqual((self.adapter.creates, self.adapter.submissions), (1, 1))


if __name__ == '__main__':
    unittest.main()
