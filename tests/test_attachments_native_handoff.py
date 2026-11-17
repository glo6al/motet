"""Native one-winner handoff with real private files, leases and durable claims."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from attachments_native_fixture import CONTENT, GIT, TURN, NativeFixture
from attachments_owner_transport import LedgerError
from attachments_types import AttachmentError, canonical


@unittest.skipUnless(os.name == 'posix' and GIT, 'Requires POSIX private files and Git')
class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = NativeFixture(self)
        self.f.mount()

    def error(self, code, action):
        with self.assertRaises((AttachmentError, LedgerError)) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def durable(self, f, r, *, attachment, admission, owner_claim):
        outbox = f.outbox(r)
        self.assertEqual(bool(outbox['claim_id'].strip('0')), attachment)
        self.assertEqual(outbox['claimed_at'] is not None, attachment)
        self.assertEqual(outbox['retention_pin'], 1)
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM owner_attachment_links WHERE owner_request_id=?',
                                            (r['ownerRequestId'],)).fetchone()[0], int(admission))
        if not admission:
            self.assertFalse(owner_claim)
            return
        owner = f.owner_status(r)
        self.assertEqual(owner['admission_digest'], f.mirror._expected(r['id']).digest)
        self.assertEqual(owner['claim'], 'claimed' if owner_claim else 'unclaimed')
        self.assertEqual(bool(owner['attempt_id']), owner_claim)
        self.assertEqual(owner['outcome']['seq'], 2 if owner_claim else 1)
        self.assertEqual(owner['outcome']['evidence_sequence'], 0)
        self.assertTrue(owner['pending_ack'])
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM requests WHERE id=?',
                                            (r['ownerRequestId'],)).fetchone()[0], 1)

    def test_disabled_actions_and_maintenance_only_are_inert(self):
        f = self.f
        r, saved, _ = f.materialize()
        off = f.coordinator(enabled=False)
        # Disabled construction/actions do not even read the attachment database.
        with patch.object(f.requests, 'resource_keys', side_effect=AssertionError):
            self.assertFalse(off.handoff(r['id'], saved))
            off.stop()
            self.assertFalse(off.handoff(r['id'], saved))
        f.open_owner(maintenance=True)
        self.error('owner_ledger_maintenance_only', lambda: f.coordinator().handoff(r['id'], saved))
        self.error('owner_ledger_maintenance_only', lambda: f.mirror.reconcile(r['id'], f.owner))
        self.durable(f, r, attachment=False, admission=False, owner_claim=False)
        self.assertEqual(f.calls, [])

    def test_two_coordinators_race_one_fresh_commit_and_callback(self):
        f = self.f
        r, saved, copy = f.materialize()
        start = threading.Barrier(2, timeout=2)
        first, second = f.coordinator(), f.coordinator()
        returned = {'newClaim': [], 'new_admission': [], 'new_claim': []}

        def document(method, key):
            def record(*args, **kwargs):
                result = method(*args, **kwargs)
                returned[key].append(result[key])
                return result
            return record

        def invoke(coordinator):
            start.wait()
            return coordinator.handoff(r['id'], saved)

        with ExitStack() as stack:
            for obj, name, key in ((f.requests, 'claim_handoff', 'newClaim'), (f.owner, 'admit', 'new_admission'),
                                   (f.owner, 'record_claim', 'new_claim')):
                stack.enter_context(patch.object(obj, name, side_effect=document(getattr(obj, name), key)))
            with ThreadPoolExecutor(2) as pool:
                workers = [pool.submit(invoke, obj) for obj in (first, second)]
                self.assertCountEqual([w.result(timeout=10) for w in workers], [True, False])
        self.assertEqual(returned, {'newClaim': [True], 'new_admission': [True], 'new_claim': [True]})
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)
        self.assertEqual(len(f.calls), 1)
        admission, attempt = f.calls[0]
        self.assertEqual(admission, f.mirror._expected(r['id']).admission)
        self.assertEqual(attempt, f.owner_status(r)['attempt_id'])
        self.assertEqual(copy.read_bytes(), CONTENT)
        self.assertEqual(f.store.storage()['usedBytes'], 2 * len(CONTENT))
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        self.assertFalse(second.handoff(r['id'], saved))
        self.assertEqual(len(f.calls), 1)

    def test_three_positive_fresh_returns_are_each_required(self):
        for gate in ('attachment', 'admission', 'attempt'):
            with self.subTest(gate=gate):
                f = NativeFixture(self)
                f.mount()
                r, saved, _ = f.materialize()
                original = f.requests.claim_handoff

                def handoff_replay(*args):
                    first = original(*args)
                    self.assertIs(first['newClaim'], True)
                    second = original(*args)
                    self.assertIs(second['newClaim'], False)
                    return first | second  # Extra known fields cannot make a replay fresh.

                def owner_replay(method, result_key):
                    def replay(*args, **kwargs):
                        self.assertIs(method(*args, **kwargs)[result_key], True)
                        second = method(*args, **kwargs)
                        self.assertIs(second[result_key], False)
                        return second
                    return replay

                with ExitStack() as stack:
                    if gate == 'attachment':
                        stack.enter_context(patch.object(f.requests, 'claim_handoff', side_effect=handoff_replay))
                    elif gate == 'admission':
                        stack.enter_context(patch.object(f.owner, 'admit', side_effect=owner_replay(f.owner.admit, 'new_admission')))
                    else:
                        stack.enter_context(patch.object(f.owner, 'record_claim', side_effect=owner_replay(f.owner.record_claim, 'new_claim')))
                    self.assertFalse(f.coordinator().handoff(r['id'], saved))
                self.durable(f, r, attachment=True, admission=gate != 'attachment', owner_claim=gate == 'attempt')
                self.assertFalse(f.coordinator().handoff(r['id'], saved))
                self.assertEqual(f.calls, [])

    def test_faults_before_and_after_each_durable_commit_never_retry(self):
        for method in ('claim_handoff', 'admit', 'record_claim'):
            for fault in ('before', 'after'):
                with self.subTest(method=method, fault=fault):
                    f = NativeFixture(self)
                    f.mount()
                    r, saved, _ = f.materialize()
                    attachment = method == 'claim_handoff'
                    target = f.requests if attachment else f.owner
                    db = f.store.db if attachment else f.owner_db
                    original = getattr(target, method)

                    def lose(*args, **kwargs):
                        db.commit_fault = fault
                        return original(*args, **kwargs)

                    with patch.object(target, method, side_effect=lose) as operation:
                        if attachment:
                            self.error('storage_unavailable', lambda: f.coordinator().handoff(r['id'], saved))
                        else:
                            with self.assertRaises(sqlite3.OperationalError):
                                f.coordinator().handoff(r['id'], saved)
                        self.assertEqual(operation.call_count, 1)
                    attached = not attachment or fault == 'after'
                    admitted = method == 'record_claim' or (method == 'admit' and fault == 'after')
                    claimed = method == 'record_claim' and fault == 'after'
                    self.durable(f, r, attachment=attached, admission=admitted, owner_claim=claimed)
                    self.assertEqual(f.calls, [])
                    f.open_store()
                    f.open_owner()
                    self.assertEqual(f.status(r)['state'], 'unknown' if attached else 'materialized')
                    self.durable(f, r, attachment=attached, admission=admitted, owner_claim=claimed)
                    self.assertEqual(f.calls, [])  # Opening/reading never dispatches.
                    # A known rollback creates no claim. A distinct explicit call
                    # can obtain the first fresh claim; any committed claim blocks it.
                    self.assertEqual(f.coordinator().handoff(r['id'], saved), not attached)
                    self.assertEqual(len(f.calls), 0 if attached else 1)

    def test_recursive_and_concurrent_same_permit_consumption(self):
        f = self.f
        r, saved, _ = f.materialize()
        captured, recursive = [], []
        entered, released = threading.Event(), threading.Event()
        coordinator = None

        def callback(admission, attempt):
            f.boundary(admission, attempt)
            if len(f.calls) > 1:
                return  # A broken single-use guard must fail the count, not recurse forever.
            recursive.append(coordinator._invoke(captured[0]))
            entered.set()
            self.assertTrue(released.wait(3))

        coordinator = f.coordinator(callback)
        entry = coordinator._invoke

        def record(permit):
            if not captured:
                captured.append(permit)
            return entry(permit)

        with patch.object(coordinator, '_invoke', side_effect=record), ThreadPoolExecutor(2) as pool:
            first = pool.submit(coordinator.handoff, r['id'], saved)
            try:
                self.assertTrue(entered.wait(3))
                second = pool.submit(coordinator._invoke, captured[0])
                self.assertFalse(second.result(timeout=2))
                other = f.coordinator()
                self.assertFalse(other._invoke(captured[0]))
            finally:
                released.set()
            self.assertTrue(first.result(timeout=3))
        self.assertEqual(recursive, [False])
        self.assertFalse(coordinator._invoke(captured[0]))
        self.assertEqual(len(f.calls), 1)
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)

    def test_expired_phase_evidence_is_not_a_resource_lease_timeout(self):
        for phase in ('initial', 'final'):
            with self.subTest(phase=phase):
                f = NativeFixture(self)
                f.mount()
                r, saved, _ = f.materialize()
                verify = f.materializer.verify_handoff
                observed = []

                def expire(*args, **kwargs):
                    fresh = verify(*args, **kwargs)
                    lease = args[4]
                    lease.require(f.requests.resource_keys(r['id']))
                    observed.append(lease)
                    if len(observed) == (1 if phase == 'initial' else 2):
                        return replace(fresh, checked_at=time.time() - 3, expires_at=time.time() - 1)
                    return fresh

                with patch.object(f.materializer, 'verify_handoff', side_effect=expire):
                    self.error('proof_unavailable', lambda: f.coordinator().handoff(r['id'], saved))
                self.assertEqual(len(observed), 1 if phase == 'initial' else 2)
                if phase == 'final':
                    self.assertIs(observed[0], observed[1])
                self.assertEqual(f.calls, [])
                self.durable(f, r, attachment=phase == 'final', admission=phase == 'final', owner_claim=phase == 'final')

    def test_actual_resource_release_immediately_before_consumption_refuses(self):
        f = self.f
        r, saved, _ = f.materialize()
        original_hold = f.resources.hold
        original_verify = f.requests._verification
        released = []

        @contextmanager
        def held(keys):
            with ExitStack() as stack:
                lease = stack.enter_context(original_hold(keys))
                released.append(stack.close)
                yield lease

        def lose_lease(row, *args):
            original_verify(row, *args)
            if row['stage'] == 'handoff_claimed':
                released[0]()

        with patch.object(f.resources, 'hold', side_effect=held), patch.object(f.requests, '_verification', side_effect=lose_lease):
            self.error('resource_lease_required', lambda: f.coordinator().handoff(r['id'], saved))
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)
        with f.hold(r) as active_again:
            active_again.require(f.requests.resource_keys(r['id']))
        self.assertEqual(f.calls, [])

    def test_copy_directory_epoch_and_scope_replacements_fail_final_check(self):
        for change in ('copy', 'directory', 'epoch', 'scope'):
            with self.subTest(change=change):
                f = NativeFixture(self)
                f.mount()
                r, saved, copy = f.materialize()
                coordinator = f.coordinator()
                owner_result = coordinator._owner_result

                def replace_after_claim(*args, claimed):
                    attempt = owner_result(*args, claimed=claimed)
                    if claimed:
                        if change == 'copy':
                            alternate = copy.with_suffix('.fictional')
                            alternate.write_bytes(CONTENT)
                            alternate.chmod(0o600)
                            alternate.replace(copy)
                        elif change == 'directory':
                            copy.parent.rename(copy.parent.with_suffix('.fictional'))
                            copy.parent.mkdir(mode=0o700)
                            copy.write_bytes(CONTENT)
                            copy.chmod(0o600)
                        elif change == 'epoch':
                            f.store.owner_epoch = 'f' * 64
                        else:
                            f.store.db.execute('UPDATE attachment_requests SET destination=? WHERE id=?',
                                               (canonical(dict(f.binding, scopeId='fictional-replaced')), r['id']))
                    return attempt

                expected = {'copy': 'copy_changed', 'directory': 'copy_changed', 'epoch': 'mirror_conflict', 'scope': 'mirror_conflict'}
                with patch.object(coordinator, '_owner_result', side_effect=replace_after_claim):
                    self.error(expected[change], lambda: coordinator.handoff(r['id'], saved))
                self.assertTrue(f.outbox(r)['claim_id'].strip('0'))
                self.assertEqual(f.owner_status(r)['claim'], 'claimed')
                self.assertEqual(f.calls, [])

    def test_stop_before_consumption_wins_and_stop_after_does_not_cancel(self):
        for stop_first in (True, False):
            with self.subTest(stop_first=stop_first):
                f = NativeFixture(self)
                f.mount()
                r, saved, _ = f.materialize()
                checkpoint, resume = threading.Event(), threading.Event()

                def pause():
                    checkpoint.set()
                    self.assertTrue(resume.wait(3))

                def boundary(*args):
                    f.boundary(*args)
                    if not stop_first:
                        pause()

                coordinator = f.coordinator(boundary)
                original = f.requests._verification

                def phase(row, *args):
                    original(row, *args)
                    if stop_first and row['stage'] == 'handoff_claimed':
                        pause()

                with patch.object(f.requests, '_verification', side_effect=phase), ThreadPoolExecutor(2) as pool:
                    task = pool.submit(coordinator.handoff, r['id'], saved)
                    try:
                        self.assertTrue(checkpoint.wait(3))
                        self.assertIsNone(pool.submit(coordinator.stop).result(timeout=2))
                        self.assertEqual(len(f.calls), 0 if stop_first else 1)
                    finally:
                        resume.set()
                    self.assertEqual(task.result(timeout=3), not stop_first)
                self.assertFalse(coordinator.handoff(r['id'], saved))
                self.durable(f, r, attachment=True, admission=True, owner_claim=True)

    def test_replaced_qualification_at_last_check_is_rejected(self):
        f = self.f
        r, saved, _ = f.materialize()
        coordinator = f.coordinator()
        original = f.requests._verification

        def change_binding(row, *args):
            original(row, *args)
            if row['stage'] == 'handoff_claimed':
                coordinator._binding = replace(coordinator._binding,
                                               profile=replace(f.profile, qualification_revision='fictional-new-revision'))

        with patch.object(f.requests, '_verification', side_effect=change_binding):
            self.assertFalse(coordinator.handoff(r['id'], saved))
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)
        self.assertEqual(f.calls, [])

    def test_boundary_failure_creates_no_unknown_producer_evidence_or_fallback(self):
        f = self.f
        r, saved, _ = f.materialize()

        def timeout(*args):
            f.boundary(*args)
            raise TimeoutError('Fictional lost return from final boundary.')

        self.error('native_boundary_uncertain', lambda: f.coordinator(timeout).handoff(r['id'], saved))
        self.assertEqual(len(f.calls), 1)
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        self.assertIsNone(f.facts(r))
        f.open_store()
        f.open_owner()
        self.assertEqual(f.status(r)['state'], 'unknown')
        self.assertFalse(f.coordinator().handoff(r['id'], saved))
        self.assertEqual(len(f.calls), 1)
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)

    def test_claimed_replay_in_new_process_and_contender_keeps_lifetime_lock(self):
        f = self.f
        r, saved, _ = f.materialize()
        call_log = f.root / 'fictional-boundary-calls.txt'
        request_id, owner_id = r['id'], r['ownerRequestId']
        args = (str(f.state), str(f.root / 'fictional-owner.sqlite3'), str(f.lockroot), str(call_log),
                request_id, owner_id, r['capability'], f.store.owner_epoch, json.dumps(asdict(saved[0])))
        child = """
import json, sqlite3, sys, threading
from attachments_native_fixture import GIT
from attachments_materialize import DirectoryIdentity, FileIdentity, Materializer, WorkspaceContext
from attachments_native_handoff import FictionalAdapterBinding, OfflineNativeHandoff
from attachments_native_mirror import NativeOutcomeMirror
from attachments_owner_transport import OwnerAttachmentLedger
from attachments_requests import PhaseVerification, RequestLedger, TransportProfile, VerifiedCopy
from attachments_resources import ResourceLocks
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
from pathlib import Path
state, path, locks, call_log, rid, oid, capability, epoch, saved_record = sys.argv[1:]
try:
    store = AttachmentStore(state, enabled=True, owner_epoch=epoch)
except AttachmentError as error:
    print(error.code)
    sys.exit(0)
db = sqlite3.connect(path)
resources = ResourceLocks(locks, enabled=True)
try:
    owner = OwnerAttachmentLedger(db, threading.RLock())
    requests = RequestLedger(store)
    mirror = NativeOutcomeMirror(requests)
    row = store.db.execute('SELECT destination,profile FROM attachment_requests WHERE id=?', (rid,)).fetchone()
    profile = json.loads(row['profile'])
    profile['media_types'] = tuple(profile['media_types'])
    profile['original_relay_formats'] = tuple(profile['original_relay_formats'])
    profile = TransportProfile(**profile)
    workspace = Path(state).parent / 'orchard'
    git_dir, exe = workspace / '.git', Path(GIT).resolve()
    context = WorkspaceContext(profile.context_fingerprint, profile.workspace_fingerprint,
        str(workspace), DirectoryIdentity.of(workspace.stat()), str(git_dir), DirectoryIdentity.of(git_dir.stat()),
        str(git_dir / 'info'), DirectoryIdentity.of((git_dir / 'info').stat()), str(exe), FileIdentity.of(exe.stat()))
    binding = FictionalAdapterBinding(row['destination'], profile, context)
    copy = json.loads(saved_record)
    phase = copy['verification']
    phase['copy_generations'] = tuple(map(tuple, phase['copy_generations']))
    copy['verification'] = PhaseVerification(**phase)
    copy['identity'] = tuple(copy['identity'])
    saved = VerifiedCopy(**copy)
    materializer = Materializer(enabled=True, validate_authority=lambda auth, phase, copies: requests.authority(rid) == auth)
    def boundary(admission, attempt):
        with Path(call_log).open('a') as log:
            print(attempt, file=log)
    coordinator = OfflineNativeHandoff(requests, owner, mirror, resources, materializer, binding, boundary, enabled=True)
    s = owner.status(oid)
    box = store.db.execute('SELECT claim_id FROM attachment_outbox WHERE request_id=?', (rid,)).fetchone()
    print(json.dumps([requests.request_status(rid, capability)['state'], coordinator.handoff(rid, (saved,)),
                      box[0], s['attempt_id'], s['outcome']['seq'], s['outcome']['evidence_sequence'],
                      len(Path(call_log).read_text().splitlines())]))
finally:
    resources.close()
    db.close()
    store.close()
"""
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src'))))
        def run(script, *script_args):
            return subprocess.run([sys.executable, '-c', script, *script_args], env=env,
                                  text=True, capture_output=True, check=True, timeout=8).stdout.strip()

        resource_contender = """
import sys
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError
locks = ResourceLocks(sys.argv[1], enabled=True)
try:
    with locks.hold(sys.argv[2:], timeout=0):
        print('held')
except AttachmentError as error:
    print(error.code)
finally:
    locks.close()
"""
        def observed(admission, attempt):
            f.boundary(admission, attempt)
            self.assertEqual(run(resource_contender, str(f.lockroot), *f.requests.resource_keys(r['id'])), 'resource_busy')
            call_log.write_text(attempt + '\n')

        self.assertTrue(f.coordinator(observed).handoff(r['id'], saved))
        self.assertEqual(run(child, *args), 'storage_unavailable')
        claim_id, attempt_id = f.outbox(r)['claim_id'], f.owner_status(r)['attempt_id']
        f.close_store()
        f.close_owner()
        self.assertEqual(json.loads(run(child, *args)), ['unknown', False, claim_id, attempt_id, 2, 0, 1])
        f.open_store()
        f.open_owner()
        self.durable(f, r, attachment=True, admission=True, owner_claim=True)
        self.assertEqual(call_log.read_text().splitlines(), [attempt_id])
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        f.event(r, 'ended', turn_id=TURN)
        f.mirror.reconcile(r['id'], f.owner)
        f.open_store()
        self.assertEqual(f.status(r)['state'], 'execution_ended')
        self.assertFalse(f.status(r)['retentionPinned'])
        self.assertEqual(len(f.calls), 1)

    def test_claude_profile_maps_to_exact_fictional_adapter(self):
        f = NativeFixture(self, adapter='claude_official')
        f.mount()
        r, _, _ = f.handed_off()
        admission, _ = f.calls[0]
        self.assertEqual((admission.transport, admission.agent_id), ('claude', f.binding['id']))
        f.event(r, 'accepted')
        f.mirror.reconcile(r['id'], f.owner)
        self.assertEqual(f.facts(r)[1]['accepted_provenance'], 'claude_submitted')


if __name__ == '__main__':
    unittest.main()
