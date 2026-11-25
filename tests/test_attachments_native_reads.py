"""Native read-stage authority and recovery through real HTTP and private SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from attachments_native_reads import NativeAgentReadService
from attachments_native_receipts import CLEANUP_QUERY, FIND_SEEN, command_digest
from attachments_service import AttachmentService, UnqualifiedResolver
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
from native_agent_reads_fixture import serve_native_reads
from test_attachments_foundation import BINDING, fence, metadata


KEY = 'fictional-native-reads-owner'
DEST, SCOPE = BINDING['id'], BINDING['scopeId']
PREFIX = '/api/attachments'
PDF = b'%PDF-1.4\nFictional unchanged bytes.\n%%EOF'
MEDIA = 'application/pdf'
SELECTION = {'destinationId': DEST, 'scopeId': SCOPE, 'modelId': None, 'conversationId': None,
             'capabilityRevision': BINDING['capabilityRevision']}


def current(**overrides):
    return dict({'mode': 'current', 'destinationId': DEST, 'scopeId': SCOPE,
                 'capabilityRevision': BINDING['capabilityRevision'],
                 'formats': [MEDIA, 'image/png', 'image/jpeg'],
                 'limits': {'fileBytes': 1024, 'draftBytes': 2048, 'fileCount': 3}}, **overrides)


def open_store(root, key=KEY, **options):
    return AttachmentStore(root, enabled=True, owner_epoch=hashlib.sha256(key.encode()).hexdigest(), **options)


def crash_server(root, pipe):
    def checkpoint(name):
        if name == 'upload_claimed':
            os._exit(78)
    store = open_store(root, checkpoint=checkpoint)
    service = NativeAgentReadService(store, selections=[current()])
    try:
        with serve_native_reads(service, KEY) as server:
            pipe.send(server.server_address)
            pipe.close()
            threading.Event().wait(8)
    finally:
        service.close()


# The read stage of delivery to agent chats: off on Windows, where that delivery is not ported
# (NATIVE_ATTACHMENT_DELIVERY_HOST), although the attachment store itself runs there.
@unittest.skipUnless(os.name == 'posix', 'Delivery to agent chats, whose read stage this is, is off on Windows '
                     '(not ported).')
class NativeReadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key = KEY
        self.serial = 0
        self.store = self.service = None
        self.addCleanup(self.close)
        self.open()

    def open(self, selections=None, **options):
        self.store = open_store(self.root, self.key, **options)
        self.service = NativeAgentReadService(self.store, selections=[current()] if selections is None else selections)

    def close(self):
        if self.service is not None:
            self.service.close()
        elif self.store is not None:
            self.store.close()
        self.store = self.service = None

    @contextmanager
    def http(self, *, service=None):
        with serve_native_reads(self.service if service is None else service, self.key) as server:
            self.server = server
            self.address = server.server_address
            yield server
        self.assertEqual(server.outbound_blocked, 0)
        self.assertFalse(server.active)

    def request(self, method, route, data=None, headers=None, *, status=200):
        hdr = {'X-Live-Room-Key': self.key, **(headers or {})}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            hdr['Content-Type'] = 'application/json'
        with closing(http.client.HTTPConnection(*self.address, timeout=4)) as client:
            client.request(method, PREFIX + route, body=data, headers=hdr)
            result = client.getresponse()
            self.assertEqual(result.getheader('Cache-Control'), 'no-store')
            value = json.loads(result.read())
        self.assertEqual(result.status, status, value)
        self.assertIs(value['fixture'], False)
        self.assertEqual(value['schemaVersion'], 1)
        self.assertNotIn(str(self.root), repr(value))
        return value

    def command(self):
        self.serial += 1
        return 'fictional-native-command-%04d' % self.serial

    def create(self, command=None, *, status=201, selection=None):
        return self.request('POST', '/drafts', SELECTION if selection is None else selection,
                            {'Idempotency-Key': command or self.command()}, status=status)

    def receipt(self, kind, command, draft_id=None, *, scope=SCOPE, dest=DEST, status=200):
        query = {'destinationId': dest, 'scopeId': scope, 'commandDigest': command_digest(command)}
        if draft_id is not None:
            query['draftId'] = draft_id
        return self.request('GET', '/native/receipts/' + kind + '?' + urlencode(query), status=status)

    def draft_route(self, draft):
        return '/drafts/' + draft['id']

    def cap(self, draft):
        return {'X-Attachment-Draft-Capability': draft['capability']}

    def view(self, draft, *, status=200):
        return self.request('GET', self.draft_route(draft), headers=self.cap(draft), status=status)

    def reserve(self, draft, command=None, data=PDF, *, status=201):
        value = self.request('POST', self.draft_route(draft) + '/upload-intents',
                             {**fence(draft), **metadata(data, MEDIA, 'fictional.pdf')},
                             {**self.cap(draft), 'Idempotency-Key': command or self.command()}, status=status)
        if status == 201:
            draft['revision'] = value['draftRevision']
        return value

    def upload_route(self, draft, upload):
        return self.draft_route(draft) + '/uploads/' + upload['id']

    def put_headers(self, draft, upload):
        return {**self.cap(draft), 'Content-Type': 'application/octet-stream',
                'X-Attachment-Writer-Generation': str(draft['writerGeneration']),
                'X-Attachment-Draft-Revision': str(draft['revision']),
                'X-Attachment-Writer-Nonce': draft['writerNonce'],
                'X-Attachment-Upload-Fingerprint': upload['fingerprint']}

    def recover(self, draft, generation=None, *, status=200):
        return self.request('POST', self.draft_route(draft) + '/recover',
                            {'writerGeneration': generation or draft['writerGeneration']}, status=status)

    def resume(self, draft, grant, *, status=200):
        return self.request('POST', self.draft_route(draft) + '/resume',
                            {key: grant[key] for key in ('revision', 'writerGeneration')},
                            {'X-Attachment-Draft-Capability': grant['recoveryCapability']}, status=status)

    def listing(self, *, scope=SCOPE, status=200):
        return self.request('GET', '/drafts?' + urlencode({'destinationId': DEST, 'scopeId': scope}), status=status)

    def discard(self, draft, command, *, status=200):
        return self.request('DELETE', self.draft_route(draft), fence(draft),
                            {**self.cap(draft), 'Idempotency-Key': command}, status=status)

    def lost(self, when, method, route, invoke, committed_status):
        self.server.fault.arm(method, PREFIX + route, when)
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
            invoke()
        self.assertTrue(self.server.fault.fired.is_set())
        self.assertEqual(self.server.fault.status, committed_status if when == 'after' else None)

    def test_gates_schema_absence_auth_and_exact_selection(self):
        self.close()
        self.store = open_store(self.root)
        self.service = AttachmentService(self.store, resolver=UnqualifiedResolver(), officer_deletions=False)
        route = '/native/selection?destinationId=' + DEST
        with self.http():
            self.request('GET', route, status=503)
            self.receipt('draft_create', self.command(), status=503)
            self.assertIsNotNone(self.store.db.execute("SELECT name FROM sqlite_schema WHERE name='seen'").fetchone())
        self.close()
        pristine = self.root / 'fictional-pristine'
        pristine.mkdir(mode=0o700)
        with self.subTest('off means no native schema on a fresh store'):
            clean_store = open_store(pristine)
            clean_service = AttachmentService(clean_store, resolver=UnqualifiedResolver(), officer_deletions=False)
            try:
                with self.http(service=clean_service):
                    self.request('GET', route, status=503)
                self.assertIsNone(clean_store.db.execute("SELECT name FROM sqlite_schema WHERE name='native_read_receipts'").fetchone())
                with self.assertRaises(AttachmentError):
                    NativeAgentReadService(clean_store, selections=[current(), current()])
                self.assertIsNone(clean_store.db.execute("SELECT name FROM sqlite_schema WHERE name='native_read_receipts'").fetchone())
            finally:
                clean_service.close()
        self.open()
        before = self.store.db.total_changes
        with self.http():
            result = self.request('GET', route)
            self.assertEqual(result['context'], {'kind': 'agent', **{k: SELECTION[k] for k in (
                'destinationId', 'scopeId', 'modelId', 'conversationId')}})
            self.assertEqual(result['features'], {'drafts': True, 'send': False, 'files': False, 'deletion': False})
            self.assertIsNone(self.request('GET', '/native/selection?destinationId=officer')['context'])
            self.assertFalse(self.listing()['items'])
            self.request('GET', route, headers={'X-Live-Room-Key': 'fictional-wrong-key'}, status=401)
            self.request('GET', route, headers={'Origin': 'http://fictional.invalid'}, status=403)
            self.request('GET', route + '&scopeId=' + SCOPE, status=400)
            self.request('GET', route + '&destinationId=' + DEST, status=400)
            self.request('GET', route, headers={'Idempotency-Key': self.command()}, status=400)
            self.request('GET', route, headers={'X-Attachment-Scope': SCOPE}, status=400)
            self.request('GET', route, headers={'Range': 'bytes=0-1'}, status=400)
            self.request('GET', route, data=b'forbidden body', status=400)
            self.listing(scope='fictional-wrong-scope', status=403)
            self.create(selection=dict(SELECTION, modelId='fictional-model'), status=503)
            self.create(selection=dict(SELECTION, capabilityRevision='fictional-old-revision'), status=409)
            self.create(selection=dict(SELECTION, conversationId='a'*32), status=400)
            self.request('GET', '/files?' + urlencode({'destinationId': DEST, 'scopeId': SCOPE, 'conversationId': ''}), status=503)
        self.assertEqual(self.store.db.total_changes, before)

    def test_receipts_bind_exact_kind_scope_parent_epoch_and_historical_allocation(self):
        create_key, reserve_key = self.command(), self.command()
        with self.http():
            draft = self.create(create_key)
            self.assertEqual(self.create(create_key)['id'], draft['id'])
            starting = dict(draft)
            upload = self.reserve(draft, reserve_key)
            self.assertEqual(self.reserve(starting, reserve_key)['id'], upload['id'])
            self.assertEqual(self.store.storage()['usedBytes'], len(PDF))
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM native_read_receipts').fetchone()[0], 2)
            self.assertEqual(self.receipt('draft_create', create_key)['match'], {'draftId': draft['id']})
            receipt = {'draftId': draft['id'], 'uploadId': upload['id'], 'fileId': upload['fileId']}
            self.assertEqual(self.receipt('upload_reserve', reserve_key, draft['id'])['match'], receipt)
            self.assertIsNone(self.receipt('draft_create', reserve_key)['match'])
            self.assertIsNone(self.receipt('upload_reserve', reserve_key, '0'*32)['match'])
            self.receipt('draft_create', create_key, scope='fictional-wrong-scope', status=403)
            self.request('DELETE', self.draft_route(draft) + '/files/' + upload['fileId'], fence(draft), self.cap(draft))
            self.assertEqual(self.receipt('upload_reserve', reserve_key, draft['id'])['match'], receipt)
        self.close()
        self.key = 'fictional-rekeyed-owner'
        self.open()
        with self.http():
            self.assertIsNone(self.receipt('draft_create', create_key)['match'])
            self.assertIsNone(self.receipt('upload_reserve', reserve_key, draft['id'])['match'])
            self.view(draft, status=403)
            listing = self.listing()['items'][0]
            self.assertEqual(listing['id'], draft['id'])
            self.assertGreater(listing['writerGeneration'], draft['writerGeneration'])
            self.assertEqual(self.resume(listing, self.recover(listing))['files'], [])

    def test_retained_anchor_is_exact_and_remains_distinct_from_admission(self):
        key = self.command()
        with self.http():
            draft = self.create(key)
            upload = self.reserve(draft)
            self.assertEqual(self.view(draft)['files'][0]['state'], 'uploading')
        self.close()
        self.open([current(scopeId='fictional-new-scope')])
        with self.http():
            self.listing(status=403)
            self.view(draft, status=403)
            self.recover(draft, status=403)
            self.request('GET', self.upload_route(draft, upload), headers=self.cap(draft), status=403)
            self.request('PUT', self.upload_route(draft, upload), PDF, self.put_headers(draft, upload), status=403)
        self.close()
        retained = {'mode': 'retained_only', 'destinationId': DEST, 'scopeId': SCOPE, 'anchorDraftId': draft['id']}
        self.open([dict(retained, anchorDraftId='0'*32)])
        with self.http():
            self.assertIsNone(self.request('GET', '/native/selection?destinationId=' + DEST)['context'])
            self.listing(status=403)
        self.close()
        self.open([retained])
        with self.http():
            self.assertTrue(self.request('GET', '/native/selection?destinationId=' + DEST)['features']['drafts'])
            self.assertEqual(self.receipt('draft_create', key)['match'], {'draftId': draft['id']})
            self.create(status=503)
            self.reserve(draft, status=503)
            self.request('PUT', self.upload_route(draft, upload), PDF, self.put_headers(draft, upload), status=503)
            resumed = self.resume(draft, self.recover(draft))
            self.view(draft, status=403)
            discard_key = self.command()
            self.discard(resumed, discard_key)
            self.assertTrue(self.request('GET', '/native/selection?destinationId=' + DEST)['features']['drafts'])
            self.assertEqual(self.listing()['items'], [])
            self.assertFalse(self.receipt('draft_discard', discard_key, draft['id'])['match']['cleanupPending'])

    def test_index_write_rollback_stale_schema_and_legacy_replay(self):
        with self.http():
            legacy = self.command()
            self.store.create_draft(BINDING, legacy)
            self.create(legacy, status=503)
            self.assertIsNone(self.receipt('draft_create', legacy)['match'])
            record = self.service.receipts.record

            def refuse_after_write(*args):
                record(*args)
                raise AttachmentError('storage_unavailable', 'Fictional commit refused.', 503)

            create_key = self.command()
            with patch.object(self.service.receipts, 'record', side_effect=refuse_after_write):
                self.create(create_key, status=503)
            self.assertIsNone(self.receipt('draft_create', create_key)['match'])
            self.assertEqual(len(self.listing()['items']), 1)
            draft = self.create(create_key)
            collision = self.command()
            with patch('attachments_native_receipts.command_digest', return_value=command_digest(create_key)):
                self.create(collision, status=503)
            self.assertEqual(len(self.listing()['items']), 2)
            self.assertIsNone(self.receipt('draft_create', collision)['match'])
            reserve_key = self.command()
            with patch.object(self.service.receipts, 'record', side_effect=refuse_after_write):
                self.reserve(draft, reserve_key, status=503)
            self.assertEqual(self.store.storage()['usedBytes'], 0)
            self.assertEqual(self.view(draft)['revision'], draft['revision'])
            self.assertIsNone(self.receipt('upload_reserve', reserve_key, draft['id'])['match'])
            self.reserve(draft, reserve_key)
            self.store.db.execute("UPDATE seen SET fingerprint='broken' WHERE key=?", (reserve_key,))
            self.receipt('upload_reserve', reserve_key, draft['id'], status=503)
            self.store.db.execute('DROP INDEX native_read_pending_cleanup')
            self.receipt('draft_create', create_key, status=503)
            self.create(status=503)
        self.close()
        self.store = open_store(self.root)
        with self.assertRaises(AttachmentError) as rejected:
            NativeAgentReadService(self.store, selections=[current()])
        self.assertEqual(rejected.exception.code, 'storage_unavailable')
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_schema WHERE name='native_read_pending_cleanup'").fetchone())

    def test_same_scope_or_same_metadata_cannot_substitute_a_command_target(self):
        create_key, reserve_key = self.command(), self.command()
        with self.http():
            draft = self.create(create_key)
            another_draft = self.create()
            self.store.db.execute('UPDATE seen SET target=? WHERE key=?', (another_draft['id'], create_key))
            self.receipt('draft_create', create_key, status=503)
            self.create(create_key, status=503)
            first = self.reserve(draft, reserve_key)
            another_upload = self.reserve(draft)
            self.assertEqual(first['fingerprint'], another_upload['fingerprint'])
            self.store.db.execute('UPDATE seen SET target=? WHERE key=?', (another_upload['id'], reserve_key))
            self.receipt('upload_reserve', reserve_key, draft['id'], status=503)
            self.reserve(draft, reserve_key, status=503)

    def test_real_originals_keep_bytes_order_and_no_preview(self):
        with self.http():
            draft = self.create()
            sources = ((PDF, MEDIA, 'fictional.pdf'), (b'\x89PNG\r\n\x1a\nfictional', 'image/png', 'fictional.png'))
            accepted = []
            with patch('attachments_store.prepare', side_effect=AssertionError('Native relay must not parse.')):
                for data, media, name in sources:
                    upload = self.request('POST', self.draft_route(draft) + '/upload-intents',
                                          {**fence(draft), **metadata(data, media, name)},
                                          {**self.cap(draft), 'Idempotency-Key': self.command()}, status=201)
                    draft['revision'] = upload['draftRevision']
                    result = self.request('PUT', self.upload_route(draft, upload), data, self.put_headers(draft, upload))
                    self.assertEqual(result['state'], 'ready')
                    draft['revision'] = result['draftRevision']
                    accepted.append(result['fileId'])
                    raw = self.store.db.execute('SELECT identity,digest FROM files WHERE id=?', (result['fileId'],)).fetchone()
                    self.assertEqual(self.store.files.read(result['fileId'], json.loads(raw['identity']), raw['digest'], len(data)), data)
            mismatch = self.reserve(draft, data=b'wrong signature')
            refused = self.request('PUT', self.upload_route(draft, mismatch), b'wrong signature', self.put_headers(draft, mismatch))
            self.assertEqual((refused['state'], refused['error']), ('failed', 'type_signature_mismatch'))
            files = self.view(draft)['files']
            self.assertEqual([file['id'] for file in files], accepted + [mismatch['fileId']])
            self.assertTrue(all(file['sourceCapability'] is None and not file['previewAvailable'] for file in files))

    def test_discard_read_index_and_reclaim_preserve_charge_until_cleanup(self):
        with self.http():
            draft = self.create()
            upload = self.reserve(draft)
            self.request('PUT', self.upload_route(draft, upload), PDF, self.put_headers(draft, upload))
            draft['revision'] = self.view(draft)['revision']
            for sql, parameter, names in (
                    (FIND_SEEN, '0'*64, ('native_read_receipts', 'seen')),
                    (CLEANUP_QUERY, draft['id'], ('native_read_pending_cleanup',)),
                    ('SELECT * FROM discard_slots WHERE draft_id=?', draft['id'], ('discard_slots',))):
                plans = [row['detail'] for row in self.store.db.execute('EXPLAIN QUERY PLAN ' + sql, (parameter,))]
                self.assertTrue(all(any(name in detail and 'SEARCH' in detail for detail in plans) for name in names), plans)
            pages = self.store.db.execute('PRAGMA page_count').fetchone()[0]
            self.assertEqual(self.store.db.execute('PRAGMA max_page_count=%d' % pages).fetchone()[0], pages)
            self.create(status=429)
            key = self.command()
            with patch.object(self.store.files, 'remove', return_value='changed') as remove:
                self.assertTrue(self.discard(draft, key)['cleanupPending'])
                calls = remove.call_count
                changes = self.store.db.total_changes
                result = self.receipt('draft_discard', key, draft['id'])['match']
                self.assertEqual(result, {'draftId': draft['id'], 'writerGeneration': 2, 'cleanupPending': True})
                self.assertIsNone(self.receipt('draft_discard', self.command(), draft['id'])['match'])
                self.assertEqual(self.store.db.total_changes, changes)
                self.assertEqual(remove.call_count, calls)
                self.assertEqual(self.store.storage()['usedBytes'], len(PDF))
            self.store.reconcile()
            self.assertFalse(self.receipt('draft_discard', key, draft['id'])['match']['cleanupPending'])
            self.assertEqual(self.store.storage()['usedBytes'], 0)
            self.store.db.execute('UPDATE discard_slots SET generation=generation+1 WHERE draft_id=?', (draft['id'],))
            self.receipt('draft_discard', key, draft['id'], status=503)

    def test_unclassified_members_cannot_extend_or_mint_preview(self):
        with self.http():
            legacy = self.store.create_draft(BINDING, self.command())
            upload = self.store.reserve_upload(legacy['id'], legacy['capability'], fence(legacy),
                                               metadata(b'Fictional text'), self.command())
            legacy['revision'] = upload['draftRevision']
            self.view(legacy, status=503)
            self.reserve(legacy, status=503)
            self.request('GET', self.upload_route(legacy, upload), headers=self.cap(legacy), status=503)
            self.request('GET', '/files/' + upload['fileId'] + '/preview',
                         headers={'X-Attachment-Source-Capability': 'fictional-opaque-preview'}, status=503)
            self.discard(legacy, self.command())

    def test_lost_keyed_acks_and_delayed_commit_have_real_readback(self):
        with self.http() as server:
            early, later = self.command(), self.command()
            self.lost('before', 'POST', '/drafts', lambda: self.create(early), 201)
            self.assertIsNone(self.receipt('draft_create', early)['match'])
            server.fault.arm('POST', PREFIX + '/drafts', 'pause')
            with ThreadPoolExecutor(max_workers=1) as worker:
                create = worker.submit(self.create, later)
                try:
                    self.assertTrue(server.fault.fired.wait(2))
                    self.assertIsNone(self.receipt('draft_create', later)['match'])
                finally:
                    server.fault.release.set()
                created = create.result(timeout=10)['id']           # committed before its receipt is read
                self.assertEqual(self.receipt('draft_create', later)['match'], {'draftId': created})
            for when in ('before', 'after'):
                key = self.command()
                self.lost(when, 'POST', '/drafts', lambda: self.create(key), 201)
                match = self.receipt('draft_create', key)['match']
                self.assertEqual(match is not None, when == 'after')
            summary = next(item for item in self.listing()['items'] if item['id'] == match['draftId'])
            draft = self.resume(summary, self.recover(summary))
            for when in ('before', 'after'):
                key = self.command()
                self.lost(when, 'POST', self.draft_route(draft) + '/upload-intents', lambda: self.reserve(draft, key), 201)
                upload_match = self.receipt('upload_reserve', key, draft['id'])['match']
                self.assertEqual(upload_match is not None, when == 'after')
            stored = self.view(draft)
            self.assertEqual(stored['files'][0]['id'], upload_match['fileId'])
            self.assertEqual(stored['files'][0]['state'], 'uploading')
            draft['revision'] = stored['revision']
            for when in ('before', 'after'):
                key = self.command()
                self.lost(when, 'DELETE', self.draft_route(draft), lambda: self.discard(draft, key), 200)
                self.assertEqual(self.receipt('draft_discard', key, draft['id'])['match'] is not None, when == 'after')

    def test_lost_unkeyed_acks_require_exact_reads_and_new_explicit_writer(self):
        with self.http():
            draft = self.create()
            route = self.draft_route(draft)
            patch_text = lambda: self.request('PATCH', route, dict(fence(draft), text='Fictional edit.'), self.cap(draft))
            for when in ('before', 'after'):
                self.lost(when, 'PATCH', route, patch_text, 200)
                stored = self.view(draft)
                self.assertEqual(stored['text'], 'Fictional edit.' if when == 'after' else '')
            draft['revision'] = stored['revision']
            upload = self.reserve(draft)
            put = lambda: self.request('PUT', self.upload_route(draft, upload), PDF, self.put_headers(draft, upload))
            for when in ('before', 'after'):
                self.lost(when, 'PUT', self.upload_route(draft, upload), put, 200)
                stored = self.view(draft)
                self.assertEqual(stored['files'][0]['state'], 'ready' if when == 'after' else 'uploading')
            draft['revision'] = stored['revision']
            file_route = route + '/files/' + upload['fileId']
            remove = lambda: self.request('DELETE', file_route, fence(draft), self.cap(draft))
            for when in ('before', 'after'):
                self.lost(when, 'DELETE', file_route, remove, 200)
                self.assertEqual(bool(self.view(draft)['files']), when == 'before')
            for when in ('before', 'after'):
                before = self.store.db.total_changes
                self.lost(when, 'POST', route + '/recover', lambda: self.recover(draft), 200)
                self.assertEqual(self.listing()['items'][0]['writerGeneration'], draft['writerGeneration'])
                self.assertEqual(self.store.db.total_changes > before, when == 'after')
            grant = self.recover(draft)
            for when in ('before', 'after'):
                self.lost(when, 'POST', route + '/resume', lambda: self.resume(draft, grant), 200)
                summary = self.listing()['items'][0]
                self.assertEqual(summary['writerGeneration'], draft['writerGeneration'] + (when == 'after'))
            self.view(draft, status=403)
            later = self.resume(summary, self.recover(summary))
            self.assertEqual(later['writerGeneration'], draft['writerGeneration'] + 2)
            self.resume(draft, grant, status=403)

    def test_receiving_claim_is_visible_and_retakeover_fences_old_bytes(self):
        claimed = threading.Event()
        self.store.checkpoint = lambda name: claimed.set() if name == 'upload_claimed' else None
        with self.http():
            draft = self.create()
            upload = self.reserve(draft)
            self.assertEqual(self.view(draft)['files'][0]['state'], 'uploading')
            with closing(http.client.HTTPConnection(*self.address, timeout=4)) as stream:
                stream.putrequest('PUT', PREFIX + self.upload_route(draft, upload))
                for name, value in dict(self.put_headers(draft, upload), **{
                        'X-Live-Room-Key': self.key, 'Content-Length': str(len(PDF))}).items():
                    stream.putheader(name, value)
                stream.endheaders()
                self.assertTrue(claimed.wait(2))
                received = self.view(draft)['files'][0]
                self.assertEqual(received['state'], 'receiving')
                self.assertIsNone(received['sourceCapability'])
                self.assertFalse(received['previewAvailable'])
                next_writer = self.resume(draft, self.recover(draft))
                stream.send(PDF)
                self.assertEqual(stream.getresponse().status, 409)
            readback = self.view(next_writer)['files'][0]
            self.assertEqual((readback['state'], readback['error']), ('failed', 'draft_conflict'))
            self.assertEqual(self.store.storage()['usedBytes'], len(PDF))

    def test_process_loss_after_receive_claim_recovers_interrupted_state(self):
        with self.http():
            draft = self.create()
            upload = self.reserve(draft)
        self.close()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=False)
        proc = context.Process(target=crash_server, args=(self.root, child))
        proc.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5))
            self.address = parent.recv()
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                self.request('PUT', self.upload_route(draft, upload), PDF, self.put_headers(draft, upload))
            proc.join(5)
            self.assertEqual(proc.exitcode, 78)
        finally:
            parent.close()
            if proc.is_alive():
                proc.terminate()
                proc.join(3)
            proc.close()
        self.open()
        with self.http():
            summary = self.listing()['items'][0]
            draft = self.resume(summary, self.recover(summary))
            readback = self.view(draft)['files'][0]
            self.assertEqual((readback['state'], readback['error']), ('failed', 'upload_interrupted'))
            self.assertEqual(self.store.storage()['usedBytes'], len(PDF))

    def test_fixture_denies_nonfixture_connections_without_calling_network(self):
        with serve_native_reads(self.service, self.key) as server:
            with self.assertRaisesRegex(OSError, 'Non-fixture connections'):
                socket.create_connection(('fictional.invalid', 1), timeout=.1)
            self.assertEqual(server.outbound_blocked, 1)


if __name__ == '__main__':
    unittest.main()
