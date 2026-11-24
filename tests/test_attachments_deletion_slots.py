"""Actual fixed BLOBs/migrations; no deletion, provider, native or path actions."""
from contextlib import closing
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import attachments_deletion_slots as slots
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_requests import RequestLedger
from attachments_conversations import OfficerConversations, OfficerRetentionProfile
from attachments_types import AttachmentError
from attachment_schema_fixtures import drop_deletion_schema
from test_attachments_foundation import BINDING, EPOCH, fence
import test_attachments_requests as request_fixtures
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


class CodecTests(unittest.TestCase):
    def error(self, action):
        with self.assertRaises(AttachmentError) as caught:
            action()
        self.assertEqual(caught.exception.code, 'cleanup_storage_unavailable')

    def test_exact_layout_and_initialized_checksum(self):
        for codec, size, end in ((slots.SOURCE_PREVIEW, 2048, 704), (slots.SOURCE_RECEIPT, 2048, 704),
                                 (slots.COPY_PREVIEW, 512, 440), (slots.COPY_RECEIPT, 512, 440)):
            raw = codec.encode({})
            self.assertEqual(len(raw), size)
            self.assertEqual(codec.payload_end, end)
            self.assertEqual(raw[-32:], hashlib.sha256(raw[:-32]).digest())
            self.assertEqual(raw[end:-32], bytes(size - 32 - end))
            self.assertEqual(codec.encode(codec.decode(raw)), raw)
        self.assertEqual(slots.SOURCE_PREVIEW.offsets['slot_id'], (24, 40))
        self.assertEqual(slots.SOURCE_PREVIEW.offsets['roster_ids'], (552, 632))
        self.assertEqual(slots.COPY_PREVIEW.offsets['identity'], (352, 392))

    def test_all_fields_fit_their_maximum_representation(self):
        for codec in (slots.SOURCE_PREVIEW, slots.SOURCE_RECEIPT, slots.COPY_PREVIEW, slots.COPY_RECEIPT):
            values = {}
            for field in codec.fields:
                if field.kind == 'pad':
                    continue
                value = {'u8': 0, 'u64': slots.UINT64, 'id': 'f' * 32,
                         'hash': 'f' * 64, 'identity': (2**63 - 1,) * 5}[field.kind]
                values[field.name] = (value,) * field.count if field.count > 1 else value
            values.update(identity_present=1, bytes=slots.UINT64, released_bytes=slots.UINT64)
            if 'roster_ids' in values:
                values.update(roster_count=5, copy_count=2,
                              roster_ids=tuple(f'{n:032x}' for n in range(1, 6)),
                              roster_generations=(slots.UINT64,) * 5,
                              copy_ids=('a' * 32, 'b' * 32))
            encoded = codec.encode(values)
            self.assertEqual(codec.decode(encoded), values)
            self.assertEqual(len(encoded), codec.size)

    def test_strict_scalar_bounds_types_and_closed_fields(self):
        bad = ({'source_generation': -1}, {'source_generation': 2**64}, {'source_generation': True},
               {'bytes': 1.0}, {'state': 7}, {'cause': 3}, {'flags': 1}, {'origin_kind': 4},
               {'identity_present': 2}, {'released': True}, {'slot_id': 'A' * 32},
               {'source_id': '1' * 31}, {'digest': 'z' * 64}, {'unknown': 1},
               {'identity': [0] * 5}, {'identity_present': 1, 'identity': (2**63,) * 5},
               {'identity': (0, 0, 1, 0, 0)}, {'bytes': 1, 'released_bytes': 2})
        for values in bad:
            with self.subTest(values=values):
                self.error(lambda: slots.SOURCE_PREVIEW.encode(values))
        for values in ({'role': 3}, {'charged': 2}, {'bytes': 1, 'partial_bytes': 2}):
            self.error(lambda values=values: slots.COPY_PREVIEW.encode(values))

    def test_roster_is_complete_bounded_unique_and_zero_tailed(self):
        valid = {'roster_count': 2, 'roster_ids': ('a' * 32, 'b' * 32) + (slots.ZERO_ID,) * 3,
                 'roster_generations': (1, 2, 0, 0, 0)}
        self.assertEqual(slots.SOURCE_PREVIEW.decode(slots.SOURCE_PREVIEW.encode(valid))['roster_ids'], valid['roster_ids'])
        for change in ({'roster_count': 6}, {'roster_ids': ('a' * 32,) * 5},
                       {'roster_ids': list(valid['roster_ids'])}, {'roster_generations': (1, 0, 0, 0, 0)},
                       {'roster_generations': (1, 2, 0, 0, 9)}, {'copy_count': 3},
                       {'copy_count': 1, 'copy_ids': ('a' * 32, 'b' * 32)}):
            self.error(lambda change=change: slots.SOURCE_PREVIEW.encode(dict(valid, **change)))

    def test_wrong_kind_version_checksum_and_reserved_bytes_reject(self):
        raw = slots.SOURCE_PREVIEW.encode({})
        for value in (raw[:-1], raw + b'x', bytearray(raw), 'not bytes'):
            self.error(lambda value=value: slots.SOURCE_PREVIEW.decode(value))
        self.error(lambda: slots.SOURCE_RECEIPT.decode(raw))
        for position in (0, 4, 5, 6, 18, 704, 2016):
            changed = bytearray(raw)
            changed[position] ^= 1
            # Header/padding validation must still reject a recomputed checksum.
            if position != 2016:
                changed[-32:] = hashlib.sha256(changed[:-32]).digest()
            self.error(lambda changed=changed: slots.SOURCE_PREVIEW.decode(bytes(changed)))
        changed = bytearray(slots.COPY_PREVIEW.encode({}))
        changed[352:360] = (2**63).to_bytes(8, 'big')
        changed[-32:] = hashlib.sha256(changed[:-32]).digest()
        self.error(lambda: slots.COPY_PREVIEW.decode(bytes(changed)))


@unittest.skipUnless(STORE, STORE_WHY)
class SlotStoreTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'open', 'key', 'error', 'draft', 'prepare', 'send', 'status'):
        locals()[_name] = getattr(request_fixtures.RequestTests, _name)
    del _name

    @property
    def database_path(self):
        return self.root / 'attachments' / 'ledger.sqlite3'

    def verify(self):
        with self.store._gate():
            slots.verify_all(self.store)

    def test_fresh_physical_source_slots_survive_restart_without_quota_effect(self):
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_source_slots').fetchone()[0], 0)
        d, upload = self.draft()
        row = self.store.db.execute('SELECT * FROM deletion_source_slots').fetchone()
        self.assertEqual(row['source_id'], upload['fileId'])
        self.assertNotEqual(row['slot_id'], row['source_id'])
        self.assertEqual((len(row['preview']), len(row['receipt'])), (2048, 2048))
        self.assertNotEqual(row['preview'], bytes(2048))
        self.assertEqual(slots.SOURCE_PREVIEW.decode(row['preview'])['draft_id'], d['id'])
        self.assertEqual(self.store.storage()['usedBytes'], len(b'Fictional orchard'))
        self.verify()
        self.store.close()
        with closing(sqlite3.connect(self.database_path)) as db:
            self.assertEqual(db.execute('SELECT preview,receipt FROM deletion_source_slots').fetchone(),
                             (row['preview'], row['receipt']))
        self.store = self.open()
        self.verify()
        self.assertEqual(self.store.storage()['usedBytes'], len(b'Fictional orchard'))

    def test_abandoned_intents_allocate_each_role_without_physical_charge(self):
        d, _ = self.draft()
        before = self.store.storage()['usedBytes']
        profile = replace(self.profile, temporary_copy=True)
        for _ in range(2):
            self.prepare(d, profile)
        copies = self.store.db.execute('SELECT * FROM workspace_copies').fetchall()
        self.assertEqual(len(copies), 4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_copy_slots').fetchone()[0], 4)
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertTrue(all(not c['charged'] and not c['released'] for c in copies))
        self.assertEqual(self.store.db.execute('SELECT SUM(retention_pin) FROM attachment_outbox').fetchone()[0], 0)
        self.assertIsNone(self.store.db.execute('SELECT admitted_kind FROM drafts').fetchone()[0])
        self.verify()
        for copy in copies:
            row = self.store.db.execute('SELECT * FROM deletion_copy_slots WHERE copy_id=?', (copy['id'],)).fetchone()
            value = slots.COPY_PREVIEW.decode(row['preview'])
            self.assertEqual(value['role'], 1 if copy['role'] == 'workspace' else 2)
            self.assertEqual(value['state'], 0)
        self.store.close()
        self.store = self.open()
        self.verify()
        self.assertEqual(self.store.storage()['usedBytes'], before)

    def test_native_admission_preserves_reserves_and_charges_only_existing_allocations(self):
        profile = replace(self.profile, temporary_copy=True)
        d, request, lease = self.prepare(profile=profile)
        before = self.store.storage()['usedBytes']
        original = self.store.db.execute('SELECT preview,receipt FROM deletion_copy_slots ORDER BY copy_id').fetchall()
        result = self.ledger.authorize_send(request['id'], request['capability'], request['revision'], request['fingerprint'],
                                            resolved_binding=BINDING, transport_profile=profile, resource_lease=lease)
        self.assertTrue(result['authorized'])
        self.assertEqual(self.store.storage()['usedBytes'], before * 3)
        self.assertEqual([tuple(r) for r in original], [tuple(r) for r in self.store.db.execute('SELECT preview,receipt FROM deletion_copy_slots ORDER BY copy_id')])
        self.verify()

    def test_missing_source_slot_prevents_preparation_and_officer_admission(self):
        d, upload = self.draft()
        self.store.db.execute('DELETE FROM deletion_source_slots WHERE source_id=?', (upload['fileId'],))
        before = self.store.storage()['usedBytes']
        self.error('cleanup_storage_unavailable', lambda: self.prepare(d))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0], 0)
        self.assertEqual(self.store.storage()['usedBytes'], before)
        conversation = self.store.create_conversation('fictional-room', self.key())['id']
        binding = dict(kind='officer', id='officer', scopeId='fictional-room', conversationId=conversation,
                       modelId='fictional-model', capabilityRevision='fictional-v1')
        officer_draft = self.store.create_draft(binding, self.key())
        from test_attachments_conversations import ConversationTests
        officer_upload = ConversationTests.upload(self, officer_draft)
        self.store.db.execute('DELETE FROM deletion_source_slots WHERE source_id=?', (officer_upload['fileId'],))
        profile = OfficerRetentionProfile('fictional-retention', 'v1', 'local_retention', 'fictional-v1')
        self.error('cleanup_storage_unavailable', lambda: OfficerConversations(self.store).admit(
            officer_draft['id'], officer_draft['capability'], fence(officer_draft), self.key(), binding, profile))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0], 0)
        self.assertIsNone(self.store.db.execute('SELECT admitted_kind FROM drafts WHERE id=?', (officer_draft['id'],)).fetchone()[0])

    def test_missing_or_mismatched_copy_slot_prevents_native_charge_and_admission(self):
        d, request, lease = self.prepare()
        copy = self.store.db.execute('SELECT * FROM workspace_copies').fetchone()
        row = self.store.db.execute('SELECT * FROM deletion_copy_slots').fetchone()
        self.store.db.execute('DELETE FROM deletion_copy_slots')
        before = self.store.storage()['usedBytes']
        action = lambda: self.ledger.authorize_send(request['id'], request['capability'], request['revision'], request['fingerprint'],
                                                    resolved_binding=BINDING, transport_profile=self.profile, resource_lease=lease)
        self.error('cleanup_storage_unavailable', action)
        bad = slots.COPY_PREVIEW.decode(row['preview'])
        bad['request_id'] = '0' * 32
        self.store.db.execute('INSERT INTO deletion_copy_slots VALUES(?,?,?)', (copy['id'], slots.COPY_PREVIEW.encode(bad), row['receipt']))
        self.error('cleanup_storage_unavailable', action)
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertIsNone(self.store.db.execute('SELECT admitted_kind FROM drafts').fetchone()[0])
        self.assertEqual(self.status(request)['state'], 'intent')

    def test_officer_local_admission_adds_no_copy_slots_or_quota(self):
        conversation = self.store.create_conversation('fictional-room', self.key())['id']
        binding = dict(kind='officer', id='officer', scopeId='fictional-room', conversationId=conversation,
                       modelId='fictional-model', capabilityRevision='fictional-v1')
        d = self.store.create_draft(binding, self.key())
        from test_attachments_conversations import ConversationTests
        upload = ConversationTests.upload(self, d)
        before = self.store.storage()['usedBytes']
        profile = OfficerRetentionProfile('fictional-retention', 'v1', 'local_retention', 'fictional-v1')
        result = OfficerConversations(self.store).admit(d['id'], d['capability'], fence(d), self.key(), binding, profile)
        self.assertTrue(result['retainedLocally'])
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_copy_slots').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT source_id FROM deletion_source_slots').fetchone()[0], upload['fileId'])
        self.verify()

    def legacy(self):
        with self.store._gate(), self.store._transaction(reclaim=True):
            drop_deletion_schema(self.store.db)
            self.store.db.execute('PRAGMA user_version=6')

    def test_schema6_backfill_preserves_native_and_officer_admissions_and_quota(self):
        d, request, lease = self.send()
        # Also retain an Officer source before reconstructing the exact old schema.
        conversation = self.store.create_conversation('fictional-room', self.key())['id']
        binding = dict(kind='officer', id='officer', scopeId='fictional-room', conversationId=conversation,
                       modelId='fictional-model', capabilityRevision='fictional-v1')
        officer_draft = self.store.create_draft(binding, self.key())
        from test_attachments_conversations import ConversationTests
        ConversationTests.upload(self, officer_draft)
        profile = OfficerRetentionProfile('fictional-retention', 'v1', 'local_retention', 'fictional-v1')
        retained = OfficerConversations(self.store).admit(officer_draft['id'], officer_draft['capability'], fence(officer_draft), self.key(), binding, profile)
        before = self.store.storage()['usedBytes']
        native = tuple(self.store.db.execute('SELECT * FROM attachment_requests').fetchone())
        self.legacy()
        self.store.close()
        self.store = self.open()
        self.ledger = RequestLedger(self.store)
        self.verify()
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM attachment_requests').fetchone()), native)
        self.assertEqual(self.store.db.execute('SELECT id FROM officer_attachment_messages').fetchone()[0], retained['id'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_source_slots').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_copy_slots').fetchone()[0], 1)

    def _assert_bad_legacy_rejected(self, change, parameters):
        before = self.store.storage()['usedBytes']
        self.legacy()
        self.store.close()
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute(change, parameters)
            expected = {name: db.execute('SELECT * FROM ' + name + ' ORDER BY 1').fetchall()
                        for name in ('files', 'drafts', 'attachment_requests', 'request_files', 'workspace_copies', 'quota')}
        self.error('cleanup_storage_unavailable', self.open)
        with closing(sqlite3.connect(self.database_path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT bytes FROM quota').fetchone()[0], before)
            for name, rows in expected.items():
                self.assertEqual(db.execute('SELECT * FROM ' + name + ' ORDER BY 1').fetchall(), rows)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'deletion_%'").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM meta WHERE key='deletion_cursor'").fetchone())
            self.assertEqual([r[2] for r in db.execute('PRAGMA index_info(file_draft)')], ['draft_id', 'member', 'id'])

    def test_schema6_dangling_copy_request_rejects_without_slot_backfill(self):
        self.prepare()
        self._assert_bad_legacy_rejected('UPDATE workspace_copies SET request_id=?', ('0' * 32,))

    def test_schema6_foreign_existing_source_rejects_even_with_clean_foreign_keys(self):
        self.prepare()
        _, other = self.draft()  # Identical bytes and digest; ownership must decide.
        self.store.db.execute('UPDATE workspace_copies SET source_id=?', (other['fileId'],))
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self._assert_bad_legacy_rejected('UPDATE workspace_copies SET source_id=?', (other['fileId'],))

    def test_schema6_missing_source_draft_rejects_without_slot_backfill(self):
        self.prepare()
        self._assert_bad_legacy_rejected('UPDATE files SET draft_id=?', ('0' * 32,))

    def test_schema6_wrong_role_allocation_rejects_without_slot_backfill(self):
        self.prepare(profile=replace(self.profile, temporary_copy=True))
        self._assert_bad_legacy_rejected("UPDATE workspace_copies SET role='temporary' WHERE role='workspace'", ())

    def _mirror_copy_slots(self, copy_id):
        row = self.store.db.execute('SELECT * FROM workspace_copies WHERE id=?', (copy_id,)).fetchone()
        source_slot = self.store.db.execute('SELECT slot_id FROM deletion_source_slots WHERE source_id=?', (row['source_id'],)).fetchone()[0]
        value = dict(copy_id=row['id'], source_id=row['source_id'], request_id=row['request_id'],
                     source_slot_id=source_slot, role=1 if row['role'] == 'workspace' else 2,
                     bytes=row['bytes'], digest=row['digest'])
        preview, receipt = slots.COPY_PREVIEW.encode(value), slots.COPY_RECEIPT.encode(value)
        self.store.db.execute('UPDATE deletion_copy_slots SET preview=?,receipt=? WHERE copy_id=?', (preview, receipt, copy_id))
        return preview, receipt

    def test_matching_recomputed_slots_cannot_bless_wrong_copy_ownership(self):
        d, request, lease = self.prepare()
        copy = self.store.db.execute('SELECT id FROM workspace_copies').fetchone()[0]
        _, other = self.draft()
        self.store.db.execute('UPDATE workspace_copies SET source_id=?', (other['fileId'],))
        mirrored = self._mirror_copy_slots(copy)
        before = self.store.storage()['usedBytes']
        self.error('cleanup_storage_unavailable', lambda: slots.verify_copy(self.store, copy))
        self.error('cleanup_storage_unavailable', self.verify)
        self.error('cleanup_storage_unavailable', lambda: self.ledger.authorize_send(
            request['id'], request['capability'], request['revision'], request['fingerprint'],
            resolved_binding=BINDING, transport_profile=self.profile, resource_lease=lease))
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertIsNone(self.store.db.execute('SELECT admitted_kind FROM drafts WHERE id=?', (d['id'],)).fetchone()[0])
        self.assertEqual(tuple(self.store.db.execute('SELECT preview,receipt FROM deletion_copy_slots WHERE copy_id=?', (copy,)).fetchone()), mirrored)
        self.store.close()
        self.error('cleanup_storage_unavailable', self.open)

    def test_moved_expected_copy_cannot_make_native_admission_empty(self):
        d, request, lease = self.prepare()
        copy = self.store.db.execute('SELECT id FROM workspace_copies').fetchone()[0]
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        self.store.db.execute('UPDATE workspace_copies SET request_id=?', ('0' * 32,))
        self.store.db.execute('PRAGMA foreign_keys=ON')
        mirrored = self._mirror_copy_slots(copy)
        before = self.store.storage()['usedBytes']
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM workspace_copies WHERE request_id=?', (request['id'],)).fetchone()[0], 0)
        self.error('cleanup_storage_unavailable', lambda: self.ledger.authorize_send(
            request['id'], request['capability'], request['revision'], request['fingerprint'],
            resolved_binding=BINDING, transport_profile=self.profile, resource_lease=lease))
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertEqual(self.store.db.execute('SELECT SUM(charged) FROM workspace_copies').fetchone()[0], 0)
        self.assertIsNone(self.store.db.execute('SELECT admitted_kind FROM drafts').fetchone()[0])
        self.assertEqual(tuple(self.store.db.execute('SELECT preview,receipt FROM deletion_copy_slots').fetchone()), mirrored)

    def test_role_and_immutable_manifest_disagreement_reject_matching_slots(self):
        self.prepare(profile=replace(self.profile, temporary_copy=True))
        copies = self.store.db.execute('SELECT * FROM workspace_copies').fetchall()
        for copy in copies:
            for column, bad in (('bytes', copy['bytes'] + 1), ('digest', '0' * 64),
                                ('role', 'temporary' if copy['role'] == 'workspace' else 'workspace')):
                with self.subTest(role=copy['role'], column=column):
                    self.store.db.execute('UPDATE workspace_copies SET ' + column + '=? WHERE id=?', (bad, copy['id']))
                    self._mirror_copy_slots(copy['id'])
                    self.error('cleanup_storage_unavailable', lambda: slots.verify_copy(self.store, copy['id']))
                    self.store.db.execute('UPDATE workspace_copies SET ' + column + '=? WHERE id=?', (copy[column], copy['id']))
                    self._mirror_copy_slots(copy['id'])
        self.store.db.execute('UPDATE request_files SET bytes=bytes+1')
        self.error('cleanup_storage_unavailable', self.verify)

    def test_zero_byte_workspace_and_temporary_allocations_are_real_members(self):
        d, _ = self.draft(body=b'')
        profile = replace(self.profile, temporary_copy=True)
        _, request, lease = self.prepare(d, profile)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deletion_copy_slots').fetchone()[0], 2)
        slots.verify_request(self.store, request['id'])
        result = self.ledger.authorize_send(request['id'], request['capability'], request['revision'], request['fingerprint'],
                                            resolved_binding=BINDING, transport_profile=profile, resource_lease=lease)
        self.assertTrue(result['authorized'])
        self.assertEqual(self.store.storage()['usedBytes'], 0)
        self.assertEqual(self.store.db.execute('SELECT SUM(charged) FROM workspace_copies').fetchone()[0], 2)
        self.verify()

    def test_actual_full_legacy_migration_rolls_back_without_eviction(self):
        self.send()
        before = self.store.storage()['usedBytes']
        self.legacy()
        pages = self.store.db.execute('PRAGMA page_count').fetchone()[0]
        maximum = pages + 4
        self.store.db.execute('PRAGMA max_page_count=%d' % maximum)
        self.store.db.execute('CREATE TABLE fictional_filler(value BLOB)')
        while True:
            try:
                self.store.db.execute('INSERT INTO fictional_filler VALUES(?)', (bytes(3000),))
            except sqlite3.OperationalError as error:
                self.assertIn('full', str(error).lower())
                break
        self.assertEqual(self.store.db.execute('PRAGMA page_count').fetchone()[0], maximum)
        native = tuple(self.store.db.execute('SELECT * FROM attachment_requests').fetchone())
        self.store.close()
        self.error('storage_unavailable', lambda: self.open(overrides={'database_bytes': maximum * 4096}))
        with closing(sqlite3.connect(self.database_path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT bytes FROM quota').fetchone()[0], before)
            self.assertEqual(tuple(db.execute('SELECT * FROM attachment_requests').fetchone()), native)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_source_slots'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM meta WHERE key='deletion_cursor'").fetchone())
        # Disposable fixture restores capacity so tearDown can close a live store.
        self.store = self.open()
        self.verify()

    def test_partial_backfill_failure_rolls_back_every_slot_and_index(self):
        self.draft()
        self.legacy()
        self.store.close()
        allocate = slots.allocate_source
        def failing(store, source):
            allocate(store, source)
            raise sqlite3.OperationalError('fictional migration fault after physical row')
        with mock.patch.object(slots, 'allocate_source', side_effect=failing):
            self.error('storage_unavailable', self.open)
        with closing(sqlite3.connect(self.database_path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_source_slots'").fetchone())
            self.assertEqual([r[2] for r in db.execute('PRAGMA index_info(file_draft)')], ['draft_id', 'member', 'id'])
        self.store = self.open()
        self.verify()

    def test_allocation_failure_rolls_back_new_source_or_copy_and_charge(self):
        before = self.store.storage()['usedBytes']
        with mock.patch.object(slots, 'allocate_source', side_effect=AttachmentError('cleanup_storage_unavailable', 'Fictional fault', 503)):
            self.error('cleanup_storage_unavailable', self.draft)
        self.assertEqual(self.store.storage()['usedBytes'], before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        d, _ = self.draft()
        before = self.store.storage()['usedBytes']
        with mock.patch.object(slots, 'allocate_copy', side_effect=AttachmentError('cleanup_storage_unavailable', 'Fictional fault', 503)):
            self.error('cleanup_storage_unavailable', lambda: self.prepare(d))
        self.assertEqual(self.store.storage()['usedBytes'], before)
        for table in ('attachment_requests', 'workspace_copies', 'deletion_copy_slots'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_startup_rejects_corrupt_slots_instead_of_repairing_them(self):
        d, upload = self.draft()
        row = self.store.db.execute('SELECT * FROM deletion_source_slots').fetchone()
        self.store.db.execute('UPDATE deletion_source_slots SET preview=?', (bytes(2048),))
        before = self.store.storage()['usedBytes']
        self.store.close()
        self.error('cleanup_storage_unavailable', self.open)
        with closing(sqlite3.connect(self.database_path)) as db:
            self.assertEqual(db.execute('SELECT preview FROM deletion_source_slots').fetchone()[0], bytes(2048))
            self.assertEqual(db.execute('SELECT bytes FROM quota').fetchone()[0], before)
            db.execute('UPDATE deletion_source_slots SET preview=?', (row['preview'],))
            db.commit()
        self.store = self.open()
        self.verify()

    def test_slot_sql_shape_and_indexes_have_no_mutable_keys(self):
        d, upload = self.draft()
        self.assertEqual([r[2] for r in self.store.db.execute('PRAGMA index_info(file_draft)')], ['draft_id', 'id'])
        for table in ('deletion_source_slots', 'deletion_copy_slots'):
            for index in self.store.db.execute('PRAGMA index_list(' + table + ')'):
                names = [r[2] for r in self.store.db.execute('PRAGMA index_info(' + index[1] + ')')]
                self.assertTrue(set(names) <= {'slot_id', 'source_id', 'copy_id'})
        for value in (b'', bytes(2047), 'x' * 2048):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute('UPDATE deletion_source_slots SET preview=?', (value,))
        self.verify()

    def test_disabled_and_demo_construct_no_slot_storage(self):
        for options in ({}, {'enabled': True, 'demo': True}):
            with mock.patch('attachments_store.PrivateFiles', side_effect=AssertionError('disabled IO')):
                store = AttachmentStore(self.root / 'fictional-unused', **options)
                self.assertIsNone(store.db)
                store.close()


if __name__ == '__main__':
    unittest.main()
