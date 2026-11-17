"""Offline adapter tests with real stores, pinned source and materialized originals."""

import ast
import builtins
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import uuid

import attachments_native_fixture as fixture
from attachments_native_handoff import FictionalAdapterBinding, OfflineNativeHandoff
from attachments_owner_transport import RETENTION_MS
from attachments_types import AttachmentError, canonical
from codex_attachment_adapter import FictionalCodexSource, FictionalQueueCommand, OfflineCodexAttachmentAdapter
from codex_attachment_evidence import EvidenceUnavailable
from test_attachments_foundation import fence, metadata
from test_codex_attachment_evidence import event, line, pin, sha, user


REPLY = 'The fictional orchard label is blue. 🧪'
PDF = b'%PDF-1.4\n% Fictional orchard label: blue.\n%%EOF\n'


class CodexFixture(fixture.NativeFixture):
    def __init__(self, test):
        super().__init__(test)
        self.mount()
        self.native = self.root / 'fictional-native'
        self.sessions = self.native / 'sessions'
        self.map_parent = self.root / 'fictional-map'
        self.sessions.mkdir(parents=True, mode=0o700)
        self.map_parent.mkdir(mode=0o700)
        self.thread = self.binding['id'][2:]
        self.turn = str(uuid.uuid4())
        self.rollout = self.sessions / ('rollout-fictional-' + self.thread + '.jsonl')
        self.header = dict(id=self.thread, session_id=self.thread, cwd=str(self.repo), source='cli')
        self.rollout.write_bytes(line('session_meta', self.header))
        self.index = self.native / 'state_5.sqlite'
        index = sqlite3.connect(self.index)
        try:
            with index:
                index.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
                index.execute('INSERT INTO threads VALUES (?,?,?,?,?)',
                              (self.thread, str(self.rollout), str(self.repo), 0, 'cli'))
        finally:
            index.close()
        self.config = FictionalCodexSource(pin(self.root), pin(self.native), pin(self.sessions), pin(self.index),
                                           pin(self.rollout), pin(self.repo), str(self.map_parent / 'map.sqlite'), True)
        self.adapter = self.runtime = None
        self.write = True
        self.return_value = None
        self.lose_return = False
        self.reply = REPLY
        self.test.addCleanup(self.close_adapter)

    def start(self, *, reopen=False, configuration=None):
        installation = self.adapter.installation_id if reopen else None
        self.close_adapter()
        config = configuration or (replace(self.config, create=False, installation_id=installation) if reopen else self.config)
        self.adapter = OfflineCodexAttachmentAdapter(canonical(self.binding), self.profile, self.context, config,
                                                     self.queue, enabled=True)
        self.runtime = OfflineNativeHandoff(self.requests, self.owner, self.mirror, self.resources, self.materializer,
                                            self.adapter_binding, enabled=True, codex_adapter=self.adapter)
        return self.runtime

    def close_adapter(self):
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None
        if self.adapter is not None:
            self.adapter.close()
            self.adapter = None

    def append(self, *records):
        with self.rollout.open('ab') as fd:
            for row in records:
                fd.write(row)

    def source_records(self, command):
        owner_marker = command.envelope.split('\n', 1)[0].split(' ', 1)[1]
        args = {} if self.reply is None else {'last_agent_message': 'WATCH_REPLY ' + owner_marker + '\n' + self.reply}
        return (event('task_started', self.turn), user(command.envelope, turn_id=self.turn),
                event('task_complete', self.turn, **args))

    def queue(self, command):
        test = self.test
        self.calls.append(command)
        test.assertIs(type(command), FictionalQueueCommand)
        test.assertEqual(command.thread_id, self.thread)
        test.assertFalse(self.owner_lock._is_owned())
        test.assertFalse(self.store.lock._is_owned())
        test.assertFalse(self.owner_db.in_transaction)
        test.assertFalse(self.store.db.in_transaction)
        test.assertFalse(self.adapter._map.db.in_transaction)
        owner_id = command.envelope.split('\n', 1)[0].split(' ', 1)[1]
        owner = self.owner.status(owner_id)
        test.assertEqual((owner['claim'], owner['outcome']['evidence_sequence']), ('claimed', 0))
        test.assertEqual(self.map_row(), (2, 1, 0))
        test.assertEqual(self.adapter._map.db.execute('SELECT floor_hash FROM frontiers').fetchone()[0],
                         sha(self.rollout.read_bytes()))
        if self.write:
            self.append(*self.source_records(command))
        if self.lose_return:
            raise RuntimeError('Fictional lost queue return.')
        return self.return_value

    def map_row(self):
        row = self.adapter._map.db.execute('SELECT generation,armed,emitted FROM attempts').fetchone()
        return row[:] if row is not None else None

    def arm(self, r, saved):
        return self.runtime.handoff(r['id'], saved)

    def observe(self, r):
        return self.runtime.observe_codex(r['id'])

    def payload(self, r):
        with self.owner._transaction():
            return self.owner._read(r['ownerRequestId'])[2]

    def charge(self, r):
        return self.store.db.execute('SELECT charged,released,bytes FROM workspace_copies WHERE request_id=?',
                                     (r['id'],)).fetchone()[:]

    def pdf(self):
        media = ('application/pdf',)
        self.profile = replace(self.profile, media_types=media, original_relay_formats=media)
        self.adapter_binding = FictionalAdapterBinding(canonical(self.binding), self.profile, self.context)
        d = self.store.create_draft(self.binding, self.key())
        d['revision'] = self.store.edit_text(d['id'], d['capability'], fence(d), fixture.QUESTION)['revision']
        upload = self.store.reserve_upload(d['id'], d['capability'], fence(d),
                                           metadata(PDF, media[0], 'fictional.pdf'), self.key(),
                                           preparation_kind='original_relay')
        d['revision'] = upload['draftRevision']
        d['revision'] = self.store.receive_upload(d['id'], d['capability'], fence(d), upload['id'],
                                                  upload['fingerprint'], io.BytesIO(PDF))['draftRevision']
        with patch.object(fixture, 'CONTENT', PDF):
            return self.materialize(self.prepare(draft=d))


@unittest.skipUnless(os.name == 'posix' and fixture.GIT and hasattr(sqlite3.Connection, 'deserialize'),
                     'Disposable Git and SQLite descriptor snapshots are required.')
class CodexAdapterTests(unittest.TestCase):
    def build(self, *, pdf=False):
        f = CodexFixture(self)
        r, saved, copy = f.pdf() if pdf else f.materialize()
        f.start()
        return f, r, saved, copy

    def assert_pending(self, f, r):
        owner = f.owner_status(r)
        self.assertEqual(owner['outcome']['evidence_sequence'], 0)
        self.assertEqual(owner['state'], 'delivery_pending')
        self.assertFalse(owner['outcome']['accepted'])
        self.assertIsNone(f.facts(r))
        self.assertEqual(f.outbox(r)['retention_pin'], 1)

    def test_real_original_and_exact_reply_commit_mirror_before_ack(self):
        f, r, saved, copy = self.build(pdf=True)
        charge = f.charge(r)
        self.assertTrue(f.arm(r, saved))
        self.assertEqual(len(f.calls), 1)
        expected = f.mirror._expected(r['id'])
        self.assertTrue(f.calls[0].envelope == expected.admission.envelope)
        self.assertEqual(sha(copy.read_bytes()), saved[0].sha256)
        self.assertEqual(sha(PDF), saved[0].sha256)
        self.assertNotIn('WATCH_REQUEST', repr(f.calls[0]))
        self.assert_pending(f, r)
        original_ack = f.owner.acknowledge
        def acknowledge(owner_id, admission, sequence, fingerprint):
            self.assertEqual((owner_id, admission), (r['ownerRequestId'], expected.digest))
            self.assertFalse(f.store.db.in_transaction)
            link, outcome = f.facts(r)
            self.assertEqual((outcome['seq'], outcome['digest']), (sequence, fingerprint))
            self.assertTrue(link['reply_available'])
            return original_ack(owner_id, admission, sequence, fingerprint)
        with patch.object(f.owner, 'acknowledge', side_effect=acknowledge) as ack:
            self.assertEqual(f.observe(r), 'reconciled')
            self.assertEqual(ack.call_count, 1)
        owner = f.owner_status(r)
        self.assertEqual((owner['state'], owner['outcome']['evidence_sequence'], owner['outcome']['turn_id']),
                         ('answered', 1, f.turn))
        self.assertTrue(owner['outcome']['accepted'])
        self.assertTrue(owner['outcome']['execution_ended'])
        self.assertFalse(owner['outcome']['started'])
        self.assertEqual(owner['outcome']['terminal_provenance'], 'correlated_reply')
        self.assertTrue(f.payload(r)['mutable']['reply'] == REPLY)
        self.assertNotIn(REPLY.encode(), Path(f.config.map_path).read_bytes())
        self.assertNotIn(REPLY.encode(), f.mirror_row(r)[0])
        self.assertEqual(f.charge(r), charge)
        self.assertEqual(f.outbox(r)['retention_pin'], 0)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(f.owner_status(r)['outcome'], owner['outcome'])
        self.assertEqual(len(f.calls), 1)

    def test_queue_result_carries_no_evidence_and_delayed_end_has_no_reply(self):
        f, r, saved, _ = self.build()
        f.write = False
        f.return_value = {'kind': 'accepted', 'turn_id': f.turn, 'sequence': 99}
        f.reply = None
        with self.assertRaisesRegex(AttachmentError, 'fictional native boundary'):
            f.arm(r, saved)
        self.assert_pending(f, r)
        self.assertEqual(f.observe(r), 'waiting')
        records = f.source_records(f.calls[0])
        f.append(records[0], records[1], records[2][:-1])
        self.assertEqual(f.observe(r), 'waiting')
        self.assert_pending(f, r)
        f.append(records[2][-1:])
        self.assertEqual(f.observe(r), 'reconciled')
        link, outcome = f.facts(r)
        self.assertTrue(outcome['accepted'] and outcome['execution_ended'])
        self.assertFalse(link['reply_available'] or outcome['started'])
        self.assertEqual((outcome['evidence_sequence'], outcome['terminal_provenance']), (1, 'codex_turn_ended'))
        self.assertFalse(f.arm(r, saved))
        self.assertEqual(len(f.calls), 1)

    def test_lost_return_reopen_never_dispatches_or_seeds_again(self):
        f, r, saved, _ = self.build()
        f.lose_return = True
        with self.assertRaises(AttachmentError):
            f.arm(r, saved)
        self.assert_pending(f, r)
        f.start(reopen=True)
        with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_recovery_only$'):
            f.arm(r, saved)
        other = f.prepare()
        before = f.status(other)
        with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_recovery_only$'):
            f.arm(other, ())
        self.assertEqual(f.status(other), before)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(f.owner_status(r)['outcome']['evidence_sequence'], 1)
        self.assertEqual(len(f.calls), 1)

    def test_seed_and_arm_commit_faults_never_invoke_or_resume(self):
        for checkpoint, row in [('before_seed_commit', None), ('after_seed_commit', (1, 0, 0)),
                                ('before_arm_commit', (1, 0, 0)), ('after_arm_commit', (2, 1, 0))]:
            with self.subTest(checkpoint=checkpoint):
                f, r, saved, _ = self.build()
                def fault(point):
                    if point == checkpoint:
                        raise EvidenceUnavailable('fictional_lost_commit')
                f.adapter._map.checkpoint = fault
                with self.assertRaisesRegex(EvidenceUnavailable, '^fictional_lost_commit$'):
                    f.arm(r, saved)
                self.assertEqual(f.map_row(), row)
                self.assertEqual(f.calls, [])
                self.assert_pending(f, r)
                f.start(reopen=True)
                with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_recovery_only$'):
                    f.arm(r, saved)
                self.assertEqual(f.observe(r), 'waiting' if checkpoint == 'after_arm_commit' else 'unavailable')
                self.assert_pending(f, r)
                self.assertEqual(f.calls, [])

    def test_seed_floor_matches_before_arm_and_after_copy_verification(self):
        for checkpoint in ('after_seed_commit', 'after_arm_commit'):
            with self.subTest(checkpoint=checkpoint):
                f, r, saved, _ = self.build()
                def advance(point):
                    if point == checkpoint:
                        f.append(event('task_started', 'fictional-unrelated'))
                f.adapter._map.checkpoint = advance
                with self.assertRaisesRegex(EvidenceUnavailable, '^source_floor_changed$'):
                    f.arm(r, saved)
                self.assertEqual(f.calls, [])
                self.assert_pending(f, r)

    def test_copy_and_binding_are_reverified_after_arm(self):
        for change in ('copy', 'epoch'):
            with self.subTest(change=change):
                f, r, saved, copy = self.build()
                def replace_input(point):
                    if point == 'after_arm_commit':
                        if change == 'epoch':
                            f.store.owner_epoch = 'a' * 63 + 'b'
                        else:
                            copy.write_bytes(b'Fictional replacement.')
                f.adapter._map.checkpoint = replace_input
                with self.assertRaises(AttachmentError):
                    f.arm(r, saved)
                self.assertEqual(f.calls, [])
                self.assertTrue(f.map_row()[1])
                self.assertEqual(f.observe(r), 'unavailable' if change == 'epoch' else 'waiting')
                self.assertEqual(f.owner_status(r)['outcome']['evidence_sequence'], 0)
                if change == 'copy':
                    self.assert_pending(f, r)

    def test_pre_floor_match_and_unarmed_matching_input_never_publish(self):
        f, r, saved, _ = self.build()
        with f.store._gate():
            envelope = f.requests._request(r['id'])['envelope']
        fake = FictionalQueueCommand(f.thread, envelope)
        f.append(*f.source_records(fake))
        f.write = False
        self.assertTrue(f.arm(r, saved))
        self.assertEqual(f.observe(r), 'waiting')
        self.assert_pending(f, r)
        self.assertEqual(len(f.calls), 1)

        f, r, saved, _ = self.build()
        f.adapter._map.checkpoint = lambda point: f.runtime.stop() if point == 'after_seed_commit' else None
        self.assertFalse(f.arm(r, saved))
        self.assertEqual(f.map_row(), (1, 0, 0))
        with f.store._gate():
            envelope = f.requests._request(r['id'])['envelope']
        f.append(*f.source_records(FictionalQueueCommand(f.thread, envelope)))
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'unavailable')
        self.assertEqual(f.map_row(), (1, 0, 0))
        self.assert_pending(f, r)
        self.assertEqual(f.calls, [])

    def test_disabled_reentrant_and_cross_thread_paths(self):
        f, r, saved, _ = self.build()
        with patch('codex_attachment_adapter.EvidenceMap', side_effect=AssertionError('Disabled I/O.')):
            inert = OfflineCodexAttachmentAdapter(None, None, None, None, None)
            self.assertIsNone(inert.installation_id)
            inert.close()
        disabled = OfflineNativeHandoff(None, None, None, None, None, None, codex_adapter=object())
        self.assertFalse(disabled.handoff(r['id'], saved))
        self.assertEqual(disabled.observe_codex(r['id']), 'unavailable')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_actor_required$'):
                pool.submit(f.arm, r, saved).result(timeout=3)
            self.assertEqual(pool.submit(f.observe, r).result(timeout=3), 'unavailable')
        self.assertEqual(f.status(r)['state'], 'materialized')
        def recursive(command):
            self.assertEqual(f.observe(r), 'unavailable')
            with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_adapter_busy$'):
                f.arm(r, saved)
            f.queue(command)
        with patch.object(f.adapter, '_boundary', recursive):
            self.assertTrue(f.arm(r, saved))
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(len(f.calls), 1)

    def test_stop_after_consumption_allows_one_call_but_no_followup_observation(self):
        f, r, saved, _ = self.build()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def stop(point):
                if point == 'after_arm_commit':
                    pool.submit(f.runtime.stop).result(timeout=3)
            f.adapter._map.checkpoint = stop
            self.assertTrue(f.arm(r, saved))
        self.assertFalse(f.arm(r, saved))
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(len(f.calls), 1)

    def test_stop_during_observation_withholds_owner_publication(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        f.adapter._map.checkpoint = lambda point: f.runtime.stop() if point == 'after_commit' else None
        self.assertEqual(f.observe(r), 'unavailable')
        self.assertEqual(f.map_row()[2], 1)
        self.assert_pending(f, r)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(len(f.calls), 1)

    def test_conflicting_same_policy_owner_event_cannot_be_mirrored_from_map(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        def fault(point):
            if point == 'after_commit':
                raise EvidenceUnavailable('fictional_lost_map_return')
        f.adapter._map.checkpoint = fault
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        f.event(r, 'ended', turn_id=f.turn)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'unavailable')
        self.assertIsNone(f.facts(r))
        self.assertEqual(f.owner_status(r)['outcome']['evidence_sequence'], 1)

    def test_wrong_exact_scope_project_thread_and_map_installation_are_refused(self):
        f, r, saved, _ = self.build()
        changed = dict(f.binding, scopeId='fictional-other-scope')
        with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_target_invalid$'):
            OfflineNativeHandoff(f.requests, f.owner, f.mirror, f.resources, f.materializer,
                FictionalAdapterBinding(canonical(changed), f.profile, f.context), enabled=True, codex_adapter=f.adapter)
        for changes in ({'project': pin(f.map_parent)},
                        {'map_path': str(f.root / 'map.sqlite')}):
            with self.assertRaisesRegex(EvidenceUnavailable, '^fixture_source_invalid$'):
                replace(f.config, **changes)
        f.rollout.write_bytes(line('session_meta', dict(f.header, id=str(uuid.uuid4()))))
        with self.assertRaises(EvidenceUnavailable):
            f.arm(r, saved)
        self.assertEqual(f.calls, [])
        self.assert_pending(f, r)
        other = replace(f.config, create=False, installation_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(EvidenceUnavailable, '^map_identity$'):
            f.start(configuration=other)

    def test_changed_attempt_never_looks_up_by_envelope_or_recovers_another_receipt(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        expected = f.mirror._expected(r['id'])
        true = f.adapter._pin(expected, f.owner_status(r)['attempt_id'])
        with patch.object(f.adapter, '_pin', return_value=replace(true, attempt_id=str(uuid.uuid4()))):
            self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        self.assertEqual(f.observe(r), 'reconciled')

    def test_changed_source_after_map_commit_cannot_publish_same_pass_reply(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        clean = f.rollout.read_bytes()

        def replace_record(point):
            if point == 'after_commit':
                f.rollout.write_bytes(clean.replace(b'blue', b'pink'))

        f.adapter._map.checkpoint = replace_record
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        self.assertEqual(f.map_row()[2], 1)
        self.assertEqual(f.observe(r), 'unavailable')
        f.rollout.write_bytes(clean)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)

    def test_identity_replaced_after_map_commit_stays_withheld_through_restore(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        old = f.sessions / 'fictional-old.jsonl'

        def replace_source(point):
            if point == 'after_commit':
                f.rollout.rename(old)
                f.rollout.write_bytes(old.read_bytes())

        f.adapter._map.checkpoint = replace_source
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        os.replace(old, f.rollout)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)

    def test_same_pass_reply_hash_mismatch_is_never_written_or_mirrored(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        genuine_scan = f.adapter._map._scan_armed

        def altered_scan(attempt):
            return replace(genuine_scan(attempt), reply='Fictional altered bytes.')

        with patch.object(f.adapter._map, '_scan_armed', side_effect=altered_scan):
            self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertTrue(f.payload(r)['mutable']['reply'] == REPLY)

    def test_stable_ambiguous_turn_before_emission_remains_withheld_on_reopen(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        clean = f.rollout.read_bytes()
        f.append(event('task_complete', f.turn))
        self.assertEqual(f.observe(r), 'unavailable')
        self.assert_pending(f, r)
        self.assertEqual(f.map_row()[2], 0)
        f.rollout.write_bytes(clean)
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'unavailable')
        self.assertEqual(len(f.calls), 1)

    def test_index_and_map_capacity_refuse_before_boundary(self):
        for fault, code in [('unclean_index', 'index_not_clean'), ('capacity', 'map_capacity')]:
            with self.subTest(fault=fault):
                f, r, saved, _ = self.build()
                if fault == 'unclean_index':
                    f.index.write_bytes(b'Fictional unclean index.')
                with patch('codex_attachment_evidence.MAX_ATTEMPTS', 0 if fault == 'capacity' else 32):
                    with self.assertRaisesRegex(EvidenceUnavailable, '^' + code + '$'):
                        f.arm(r, saved)
                self.assertIsNone(f.map_row())
                self.assertEqual(f.calls, [])
                self.assert_pending(f, r)

    def test_map_commit_loss_requires_source_revalidation_before_first_owner_write(self):
        for rewrite in (True, False):
            with self.subTest(rewrite=rewrite):
                f, r, saved, _ = self.build()
                self.assertTrue(f.arm(r, saved))
                def lose(point):
                    if point == 'after_commit':
                        raise EvidenceUnavailable('fictional_lost_commit')
                f.adapter._map.checkpoint = lose
                self.assertEqual(f.observe(r), 'unavailable')
                self.assertEqual(f.map_row()[2], 1)
                self.assert_pending(f, r)
                initial = f.rollout.read_bytes()
                if rewrite:
                    f.rollout.write_bytes(initial.replace(b'blue', b'pink'))
                f.start(reopen=True)
                self.assertEqual(f.observe(r), 'unavailable' if rewrite else 'reconciled')
                if rewrite:
                    f.rollout.write_bytes(initial)
                    self.assertEqual(f.observe(r), 'unavailable')
                    self.assert_pending(f, r)
                self.assertEqual(len(f.calls), 1)

    def test_owner_commit_loss_exact_emission_can_mirror_after_source_rewrite(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        original_record = f.owner.record_outcome
        def lose_owner(*args, **kwargs):
            original_record(*args, **kwargs)
            raise sqlite3.OperationalError('Fictional lost owner commit.')
        with (patch.object(f.owner, 'record_outcome', side_effect=lose_owner),
              patch.object(f.owner, 'acknowledge', wraps=f.owner.acknowledge) as ack):
            self.assertEqual(f.observe(r), 'unavailable')
            ack.assert_not_called()
        self.assertIsNone(f.facts(r))
        owner = f.owner_status(r)['outcome']
        f.rollout.write_bytes(b'')
        f.start(reopen=True)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertEqual(f.owner_status(r)['outcome'], owner)
        f.owner.prune_payload(r['ownerRequestId'], f.mirror._expected(r['id']).digest,
                              now_ms=owner['updated_ms'] + RETENTION_MS + 1)
        self.assertEqual(f.observe(r), 'reconciled')
        self.assertFalse(f.facts(r)[0]['reply_available'])
        self.assertTrue(f.facts(r)[1]['reply_captured'])
        self.assertEqual(len(f.calls), 1)

    def test_mirror_failure_prevents_ack_and_missing_map_never_reconstitutes(self):
        f, r, saved, _ = self.build()
        self.assertTrue(f.arm(r, saved))
        f.store.db.update_fault = 'UPDATE native_outcome_mirror'
        with patch.object(f.owner, 'acknowledge', wraps=f.owner.acknowledge) as ack:
            self.assertEqual(f.observe(r), 'unavailable')
            ack.assert_not_called()
        self.assertIsNone(f.facts(r))
        self.assertEqual(f.owner_status(r)['outcome']['evidence_sequence'], 1)
        installation = f.adapter.installation_id
        f.close_adapter()
        Path(f.config.map_path).unlink()
        with self.assertRaises(EvidenceUnavailable):
            f.start(configuration=replace(f.config, create=False, installation_id=installation))
        self.assertFalse(Path(f.config.map_path).exists())
        self.assertEqual(len(f.calls), 1)


class OfflineBoundaryTests(unittest.TestCase):
    def test_handoff_import_and_disabled_dispatch_do_not_import_concrete_adapter(self):
        source = Path(__file__).resolve().parents[1] / 'src' / 'attachments_native_handoff.py'
        spec = importlib.util.spec_from_file_location('fictional_handoff_import_probe', source)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def import_guard(name, *args, **kwargs):
            self.assertNotIn(name, ('codex_attachment_adapter', 'codex_attachment_evidence'))
            return original_import(name, *args, **kwargs)

        with (patch.dict(sys.modules, {spec.name: module}),
              patch('builtins.__import__', side_effect=import_guard)):
            spec.loader.exec_module(module)
            disabled = module.OfflineNativeHandoff(None, None, None, None, None, None, codex_adapter=object())
            self.assertEqual(disabled.observe_codex('fictional-request'), 'unavailable')
            self.assertFalse(disabled.handoff('fictional-request', ()))

    def test_adapter_does_not_import_native_launchers_or_generic_messaging(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        restricted = {'officer_messaging', 'codex_new_chat', 'codex_desktop_control', 'subprocess', 'socket',
                      'peer_bridge', 'native_observation', 'claude_mod'}
        for name in ('attachments_native_handoff.py', 'codex_attachment_adapter.py', 'codex_attachment_evidence.py'):
            tree = ast.parse((root / name).read_text(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(restricted.isdisjoint(alias.name.split('.')[-1] for alias in node.names), name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[-1], restricted, name)
