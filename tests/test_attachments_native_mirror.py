"""Cumulative native mirror on actual disposable attachment and owner SQLite."""
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch
import uuid

from attachments_native_fixture import CONTENT, GIT, QUESTION, TURN, NativeFixture
from attachments_native_mirror import EMPTY_PAIR, HEADER_BYTES, SLOT_BYTES, NativeOutcomeMirror
from attachments_owner_transport import (LINK_BYTES, OUTCOME_BYTES, RETENTION_MS, LedgerError, OwnerAttachmentLedger,
                                         _seal_link, _seal_outcome, digest, padded)
from attachments_requests import NATIVE_MIRROR_VERSION, OwnerReceipt
from attachments_types import AttachmentError, canonical

REPLY = 'Fictional: the orchard is green.'


def revised(owner_id, pair, *, link_fields=None, outcome_fields=None, identity_fields=None):
    """Seal independently plausible fixed records to test cross-record comparison."""
    link, outcome = OwnerAttachmentLedger.decode_pair(owner_id, *pair)
    link.update(link_fields or {})
    outcome.update(outcome_fields or {})
    if identity_fields:
        link['identity'].update(identity_fields)
        link['admission_digest'] = outcome['admission_digest'] = digest(
            'watch-owner-attachment-admission-v1', link['identity'])
    _seal_link(link)
    _seal_outcome(outcome)
    return padded(link, LINK_BYTES), padded(outcome, OUTCOME_BYTES)


@unittest.skipUnless(os.name == 'posix' and GIT, 'Requires POSIX private files and Git')
class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.f = NativeFixture(self)

    def error(self, code, action):
        with self.assertRaises((AttachmentError, LedgerError)) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_optional_layout_and_partial_unknown_schema_are_refused(self):
        f = self.f
        table = "SELECT name FROM sqlite_schema WHERE name='native_outcome_mirror'"
        self.assertIsNone(f.store.db.execute(table).fetchone())
        f.store.db.execute('INSERT INTO meta VALUES (?,?)', (NATIVE_MIRROR_VERSION, '2'))
        self.error('mirror_unavailable', f.mount)
        self.assertIsNone(f.store.db.execute(table).fetchone())
        f.store.db.execute('DELETE FROM meta WHERE key=?', (NATIVE_MIRROR_VERSION,))
        f.store.db.execute('CREATE TABLE native_outcome_mirror (unrelated INTEGER)')
        self.error('mirror_unavailable', f.mount)
        self.assertIsNotNone(f.store.db.execute(table).fetchone())
        f.store.db.execute('DROP TABLE native_outcome_mirror')
        f.mount()
        f.store.db.execute('CREATE TRIGGER fictional_extra AFTER INSERT ON native_outcome_mirror BEGIN SELECT 1; END')
        self.error('mirror_unavailable', lambda: NativeOutcomeMirror(f.requests))
        self.assertIsNotNone(f.store.db.execute("SELECT 1 FROM sqlite_schema WHERE name='fictional_extra'").fetchone())

    def test_reservation_failure_stays_unadmitted_and_exact_prepare_can_finish(self):
        f = self.f
        f.mount()
        draft, cmd = f.draft(), f.key()
        f.store.db.update_fault = 'INSERT INTO native_outcome_mirror'
        self.error('storage_unavailable', lambda: f.prepare(draft=draft, key=cmd))
        r = f.prepare(mirror=False, draft=draft, key=cmd)
        self.assertIsNone(f.mirror_row(r))
        self.assertFalse(f.status(r)['authorized'])
        with f.hold(r) as lease:
            self.error('mirror_conflict', lambda: f.authorize(r, lease))
        self.assertEqual(f.store.storage()['usedBytes'], len(CONTENT))
        self.assertEqual(f.outbox(r)['retention_pin'], 0)
        self.assertEqual(f.owner_db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

        f.store.db.commit_fault = 'after'
        self.error('storage_unavailable', lambda: f.prepare(draft=draft, key=cmd))
        self.assertFalse(f.status(r)['authorized'])
        self.assertEqual(len(f.mirror_row(r)[0]), SLOT_BYTES)
        f.open_store()
        self.assertEqual(f.prepare(draft=draft, key=cmd), r)
        self.assertEqual(f.mirror_row(r)[0][HEADER_BYTES:], EMPTY_PAIR)
        with f.hold(r) as lease:
            self.assertTrue(f.authorize(r, lease)['authorized'])
        self.assertEqual(f.store.storage()['usedBytes'], 2 * len(CONTENT))

    def test_never_backfills_an_already_authorized_legacy_intent(self):
        f = self.f
        f.mount()
        draft, cmd = f.draft(), f.key()
        r = f.prepare(mirror=False, draft=draft, key=cmd)
        with f.hold(r) as lease:
            self.assertTrue(f.authorize(r, lease, mirror=False)['authorized'])
        self.error('mirror_conflict', lambda: f.prepare(draft=draft, key=cmd))
        self.assertIsNone(f.mirror_row(r))
        self.assertEqual(f.outbox(r)['retention_pin'], 1)

    def test_cumulative_unknown_end_reply_conflict_and_stale_snapshot(self):
        f = self.f
        f.mount()
        r, _, copy = f.handed_off()
        quota = f.store.storage()['usedBytes']
        f.event(r, 'accepted')
        accepted_pair = f.owner_pair(r)
        unknown = f.event(r, 'unknown')['outcome']
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        link, facts = f.facts(r)
        self.assertEqual(facts, unknown)
        self.assertTrue(facts['accepted'] and facts['capture_withheld'])
        self.assertGreater(facts['accepted_at'], 0)
        self.assertEqual(facts['accepted_provenance'], 'codex_queue_accepted')
        self.assertEqual((f.status(r)['state'], f.outbox(r)['accepted']), ('unknown', 1))
        self.assertTrue(f.status(r)['retentionPinned'])
        with patch.object(f.owner, '_exact_snapshot', return_value=accepted_pair), patch.object(f.owner, 'acknowledge') as ack:
            self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('stale', None))
            ack.assert_not_called()
        self.assertEqual(f.facts(r), (link, facts))

        ended = f.event(r, 'ended', turn_id=TURN)['outcome']
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        self.assertEqual(f.facts(r)[1], ended)
        self.assertTrue(ended['execution_ended'])
        self.assertFalse(ended['reply_captured'])
        self.assertEqual(f.status(r)['state'], 'execution_ended')
        self.assertFalse(f.status(r)['retentionPinned'])

        f.event(r, 'reply', turn_id=TURN, reply=REPLY)
        f.mirror.reconcile(r['id'], f.owner)
        link, facts = f.facts(r)
        self.assertTrue(link['reply_available'] and facts['reply_captured'] and facts['capture_withheld'])
        self.assertEqual((f.status(r)['state'], f.status(r)['replyState']), ('answered', 'unknown'))
        for value in (REPLY, QUESTION, str(copy)):
            self.assertNotIn(value.encode(), f.mirror_row(r)[0])
        reply_fingerprint = facts['reply_fingerprint']
        f.event(r, 'reply', turn_id=TURN, reply='Fictional conflicting reply.')
        f.mirror.reconcile(r['id'], f.owner)
        link, facts = f.facts(r)
        self.assertTrue(facts['conflict'] and facts['execution_ended'])
        self.assertFalse(link['reply_available'])
        self.assertEqual(facts['reply_fingerprint'], reply_fingerprint)
        self.assertEqual(f.status(r)['state'], 'execution_ended')
        self.assertEqual(f.store.storage()['usedBytes'], quota)
        self.assertEqual(copy.read_bytes(), CONTENT)
        self.assertTrue(f.owner_status(r)['terminal_acked'])

    def test_prune_with_same_sequence_withdraws_availability_and_preserves_witness(self):
        f = self.f
        f.mount()
        r, _, _ = f.handed_off()
        f.event(r, 'reply', turn_id=TURN, reply=REPLY)
        f.mirror.reconcile(r['id'], f.owner)
        old_link, old_out = f.facts(r)
        self.assertTrue(old_link['reply_available'])
        self.assertFalse(old_out['capture_withheld'])
        f.owner.prune_payload(r['ownerRequestId'], old_link['admission_digest'],
                              now_ms=int(time.time() * 1000) + RETENTION_MS + 1000)
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        pruned_link, pruned_out = f.facts(r)
        self.assertGreater(pruned_link['revision'], old_link['revision'])
        self.assertEqual(pruned_out, old_out)
        self.assertTrue(pruned_link['payload_pruned'] and pruned_link['terminal_acked'])
        self.assertFalse(pruned_link['reply_available'] or pruned_out['capture_withheld'])
        self.assertEqual((f.status(r)['state'], f.status(r)['replyState']), ('execution_ended', 'unknown'))
        fixed_record = f.mirror_row(r)
        revision = f.status(r)['revision']
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('duplicate', 'acknowledged'))
        self.assertEqual((f.mirror_row(r), f.status(r)['revision']), (fixed_record, revision))
        damaged_prune = revised(r['ownerRequestId'], f.owner_pair(r), link_fields={
            'revision': pruned_link['revision'] + 1, 'payload_pruned': False})
        self.error('mirror_conflict', lambda: f.mirror._apply(r['id'], damaged_prune))
        damaged_ack = revised(r['ownerRequestId'], f.owner_pair(r), link_fields={
            'revision': pruned_link['revision'] + 1, 'payload_pruned': False, 'terminal_acked': False,
            'ack_digest': '', 'ack_seq': 0})
        self.error('mirror_conflict', lambda: f.mirror._apply(r['id'], damaged_ack))
        f.event(r, 'reply', turn_id=TURN, reply=REPLY)
        f.mirror.reconcile(r['id'], f.owner)
        late_link, late_out = f.facts(r)
        self.assertTrue(late_out['late_reply_observed'] and late_out['capture_withheld'])
        self.assertTrue(late_out['execution_ended'] and late_out['reply_captured'])
        self.assertFalse(late_link['reply_available'])
        self.assertEqual(late_out['reply_fingerprint'], late_out['late_reply_fingerprint'])

    def test_complete_frozen_identity_and_legacy_receipt_exclusion(self):
        f = self.f
        f.mount()
        r, _, _ = f.handed_off()
        rid, owner_id = r['id'], r['ownerRequestId']
        expected = f.mirror._expected(rid)
        plain = f.owner_pair(r)
        self.assertEqual(tuple(map(len, plain)), (LINK_BYTES, OUTCOME_BYTES))
        self.assertEqual(tuple(map(type, plain)), (bytes, bytes))
        self.assertNotIn(QUESTION.encode(), b''.join(plain))
        self.error('invalid_digest', lambda: f.owner._exact_snapshot(owner_id, None))
        self.error('identity_conflict', lambda: f.owner._exact_snapshot(owner_id, 'f' * 64))
        changes = {'attachment_request_id': 'f' * 32, 'handoff_claim_id': 'f' * 64,
                   'owner_epoch': 'f' * 64, 'fingerprint': 'f' * 64, 'profile': 'other-profile',
                   'profile_revision': 'v2', 'target_binding_digest': 'f' * 64,
                   'envelope_sha256': 'f' * 64, 'payload_digest': 'f' * 64, 'agent_id': 'x:' + str(uuid.uuid4())}
        for field, value in changes.items():
            with self.subTest(field=field):
                variant = revised(owner_id, plain, identity_fields={field: value})
                self.error('mirror_conflict', lambda: f.mirror._apply(rid, variant))
        for field, value in (('destination', canonical(dict(f.binding, scopeId='fictional-changed-scope'))),
                             ('profile', canonical(dict(json.loads(
                                 f.store.db.execute('SELECT profile FROM attachment_requests WHERE id=?', (rid,)).fetchone()[0]), revision='v2'))),
                             ('fingerprint', 'f' * 64), ('epoch', 'f' * 64), ('text', 'Fictional different question.'),
                             ('envelope', 'Fictional substituted envelope.')):
            with self.subTest(local_field=field):
                original = f.store.db.execute('SELECT ' + field + ' FROM attachment_requests WHERE id=?', (rid,)).fetchone()[0]
                f.store.db.execute('UPDATE attachment_requests SET ' + field + '=? WHERE id=?', (value, rid))
                self.error('mirror_conflict', lambda: f.mirror._apply(rid, plain))
                f.store.db.execute('UPDATE attachment_requests SET ' + field + '=? WHERE id=?', (original, rid))
        f.store.db.execute('UPDATE attachment_outbox SET claim_id=? WHERE request_id=?', ('f' * 64, rid))
        self.error('mirror_conflict', lambda: f.mirror._apply(rid, plain))
        f.store.db.execute('UPDATE attachment_outbox SET claim_id=? WHERE request_id=?', (expected.admission.handoff_claim_id, rid))
        f.event(r, 'reply', turn_id=TURN, reply=REPLY)
        f.mirror.reconcile(rid, f.owner)
        receipt = OwnerReceipt(rid, owner_id, expected.admission.handoff_claim_id, r['fingerprint'],
                               expected.admission.owner_epoch, 99, 'answered', 'correlated_reply', f.calls[0][1])
        frozen, projected = f.mirror_row(r), f.status(r)
        for write in (f.requests.apply_owner_outcome, f.requests.record_owner_receipt):
            self.error('mirror_owned', lambda: write(receipt))
        f.open_store(mirror=False)
        self.error('mirror_owned', lambda: f.requests.apply_owner_outcome(receipt))
        self.assertEqual((f.mirror_row(r), f.status(r)), (frozen, projected))
        f.owner_db.execute('UPDATE requests SET data=? WHERE id=?', ('{}', owner_id))
        f.owner_db.commit()
        self.error('invalid_record_size', lambda: f.owner._exact_snapshot(owner_id, expected.digest))

    def test_contradictory_order_cumulative_erosion_and_malformed_slot(self):
        f = self.f
        f.mount()
        r, _, _ = f.handed_off()
        rid, owner_id = r['id'], r['ownerRequestId']
        initial = f.owner_pair(r)
        f.event(r, 'started', turn_id=TURN)
        f.event(r, 'unknown')
        before = f.owner_pair(r)
        self.assertEqual(f.mirror._apply(rid, before), 'applied')
        self.assertEqual(f.mirror._apply(rid, before), 'duplicate')
        self.assertEqual(f.mirror._apply(rid, initial), 'stale')
        prior_link, prior = f.facts(r)
        f.event(r, 'ended', turn_id=TURN)
        newer = f.owner_pair(r)
        link, outcome = OwnerAttachmentLedger.decode_pair(owner_id, *newer)
        variants = [
            revised(owner_id, initial, link_fields=dict(revision=link['revision'])),
            revised(owner_id, before, outcome_fields=dict(reason='native_dropped')),
            revised(owner_id, newer, link_fields=dict(revision=prior_link['revision'])),
            revised(owner_id, newer, link_fields=dict(revision=prior_link['revision'] - 1)),
            revised(owner_id, newer, link_fields=dict(turn_id='fictional-changed-turn'), outcome_fields=dict(turn_id='fictional-changed-turn')),
            revised(owner_id, newer, link_fields=dict(attempt_id=str(uuid.uuid4()))),
            revised(owner_id, newer, outcome_fields=dict(started=False)),
            revised(owner_id, newer, outcome_fields=dict(accepted_at=prior['accepted_at'] + 10)),
            revised(owner_id, newer, outcome_fields=dict(accepted_provenance='claude_started')),
            revised(owner_id, newer, outcome_fields=dict(capture_withheld=False)),
        ]
        for index, variant in enumerate(variants):
            with self.subTest(case=index):
                self.error('corrupt_outcome' if index == 5 else 'mirror_conflict', lambda: f.mirror._apply(rid, variant))
                self.assertEqual(f.facts(r), (prior_link, prior))
        attempt = str(uuid.uuid4())
        variant = revised(owner_id, newer, link_fields=dict(attempt_id=attempt), outcome_fields=dict(attempt_id=attempt))
        self.error('mirror_conflict', lambda: f.mirror._apply(rid, variant))
        raw = f.mirror_row(r)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            f.store.db.execute('UPDATE native_outcome_mirror SET slot=? WHERE request_id=?', (raw[1:], rid))
        self.error('invalid_record_size', lambda: f.mirror._apply(rid, (newer[0][1:], newer[1])))
        for damaged, code in ((b'!' + raw[1:], 'invalid_encoding'),
                              (raw[:HEADER_BYTES] + EMPTY_PAIR, 'mirror_conflict')):
            f.store.db.execute('UPDATE native_outcome_mirror SET slot=? WHERE request_id=?', (damaged, rid))
            self.error(code, lambda: f.mirror._apply(rid, newer))
        f.store.db.execute('UPDATE native_outcome_mirror SET slot=? WHERE request_id=?', (raw, rid))
        self.assertEqual(f.mirror._apply(rid, newer), 'applied')
        self.assertFalse(f.status(r)['retentionPinned'])

    def test_mirror_rolls_back_update_failure_and_lost_return_replays(self):
        f = self.f
        f.mount()
        r, _, _ = f.handed_off()
        f.event(r, 'not_submitted')
        empty, request, outbox = f.mirror_row(r), f.status(r), f.outbox(r)
        for fault in ('update', 'before', 'after'):
            with self.subTest(fault=fault), patch.object(f.owner, 'acknowledge', wraps=f.owner.acknowledge) as ack:
                if fault == 'update':
                    f.store.db.update_fault = 'UPDATE attachment_outbox'
                else:
                    f.store.db.commit_fault = fault
                self.error('storage_unavailable', lambda: f.mirror.reconcile(r['id'], f.owner))
                ack.assert_not_called()
                self.assertTrue(f.owner_status(r)['pending_ack'])
                if fault != 'after':
                    self.assertEqual((f.mirror_row(r), f.status(r), f.outbox(r)), (empty, request, outbox))
        self.assertFalse(f.status(r)['retentionPinned'])
        f.open_store()
        f.open_owner()
        self.assertEqual(f.status(r)['state'], 'failed')
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('duplicate', 'acknowledged'))
        self.assertTrue(f.owner_status(r)['terminal_acked'])
        self.assertEqual(f.store.storage()['usedBytes'], 2 * len(CONTENT))

    def test_ack_fault_and_intervening_owner_outcome(self):
        f = self.f
        f.mount()
        r, _, _ = f.handed_off()
        f.event(r, 'accepted')
        original = f.owner.acknowledge

        def supersede(*args):
            self.assertFalse(f.store.lock._is_owned())
            self.assertFalse(f.store.db.in_transaction)
            self.assertTrue(f.facts(r)[1]['accepted'])
            f.event(r, 'ended', turn_id=TURN)
            return original(*args)

        with patch.object(f.owner, 'acknowledge', side_effect=supersede):
            self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'stale'))
        self.assertTrue(f.status(r)['retentionPinned'])
        self.assertTrue(f.owner_status(r)['pending_ack'])

        for mode in ('before', 'after'):
            with self.subTest(mode=mode):
                def lose(*args):
                    self.assertFalse(f.store.lock._is_owned())
                    self.assertTrue(f.facts(r)[1]['execution_ended'])
                    f.owner_db.commit_fault = mode
                    return original(*args)

                with patch.object(f.owner, 'acknowledge', side_effect=lose), self.assertRaises(sqlite3.OperationalError):
                    f.mirror.reconcile(r['id'], f.owner)
                self.assertFalse(f.status(r)['retentionPinned'])
                self.assertEqual(f.owner_status(r)['terminal_acked'], mode == 'after')
        f.open_owner()
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        self.assertTrue(f.facts(r)[0]['terminal_acked'])

    def test_database_headroom_can_block_reservation_but_never_existing_slot_update(self):
        f = self.f
        f.mount()
        draft, cmd = f.draft(), f.key()
        r = f.prepare(mirror=False, draft=draft, key=cmd)
        db = f.store.db
        initial_max = db.execute('PRAGMA max_page_count').fetchone()[0]
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute('PRAGMA max_page_count=%d' % max(1, pages))
        with self.assertRaises(AttachmentError):
            f.prepare(draft=draft, key=cmd)
        self.assertIsNone(f.mirror_row(r))
        self.assertFalse(f.status(r)['authorized'])
        db.execute('PRAGMA max_page_count=%d' % initial_max)
        self.assertEqual(f.prepare(draft=draft, key=cmd), r)
        r, copies, _ = f.materialize(r)
        self.assertTrue(f.coordinator().handoff(r['id'], copies))
        current_pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute('PRAGMA max_page_count=%d' % current_pages)
        f.event(r, 'ended', turn_id=TURN)
        self.assertEqual(f.mirror.reconcile(r['id'], f.owner), ('applied', 'acknowledged'))
        self.assertEqual(len(f.mirror_row(r)[0]), SLOT_BYTES)
        self.assertEqual(db.execute('PRAGMA page_count').fetchone()[0], current_pages)
        self.assertEqual(f.store.storage()['usedBytes'], 2 * len(CONTENT))


if __name__ == '__main__':
    unittest.main()
