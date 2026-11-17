"""Actual HTTP/service/store boundary with fictional contexts and bounded readers."""
import io
import json
import os
from email.message import Message
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_http import handle_http, LimitedReader
from attachments_service import AttachmentService, UnqualifiedResolver
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
from test_attachments_foundation import BINDING, EPOCH, fence, metadata

KEY = 'fictional-owner-key'
SELECTION = {'destinationId': BINDING['id'], 'scopeId': BINDING['scopeId'], 'modelId': None,
             'conversationId': None, 'capabilityRevision': 'fictional-v1'}


class Resolver:
    def __init__(self):
        self.available, self.revision = True, 'fictional-v1'
        self.calls = 0
    def resolve_scope(self, scope):
        self.calls += 1
        return {'id': scope} if scope == BINDING['scopeId'] else None
    def resolve_destination(self, did, scope, model):
        self.calls += 1
        if not self.available or scope != BINDING['scopeId'] or did not in (BINDING['id'], 'officer'):
            return {'available': False}
        return {'available': True, 'kind': 'officer' if did == 'officer' else 'agent', 'id': did,
                'scopeId': scope, 'modelId': model, 'capabilityRevision': self.revision,
                'formats': ['text/plain', 'text/markdown', 'application/pdf'],
                'limits': {'fileBytes': 10*1024*1024, 'draftBytes': 20*1024*1024, 'fileCount':5}}


class Reader(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.reads = 0
    def read1(self, n):
        self.reads += 1
        return super().read1(n)


class Handler:
    def __init__(self, method, path, body=b'', extra=(), defaults=True):
        self.command, self.path, self.headers = method, path, Message()
        if defaults:
            self.headers['Host'] = '127.0.0.1:8786'
            self.headers['X-Live-Room-Key'] = KEY
        for k, v in extra:
            self.headers[k] = v
        self.rfile, self.connection, self.response = Reader(body), mock.Mock(), None
    def send(self, status, body, content_type, extra=()):
        self.response = status, json.loads(body), dict(extra)


@unittest.skipUnless(os.name == 'posix', 'Private attachment storage requires qualified POSIX primitives.')
class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.temp.name))
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch=EPOCH)
        self.resolver = Resolver()
        self.service = AttachmentService(self.store, resolver=self.resolver)
        self.serial = 0
    def tearDown(self):
        self.service.close()
        self.temp.cleanup()
    def key(self):
        self.serial += 1
        return 'fictional-http-%08d' % self.serial
    def call(self, method, route, data=None, headers=(), raw=None, service=True, defaults=True):
        body = json.dumps(data).encode() if data is not None else raw or b''
        hdr = list(headers)
        if data is not None or raw is not None:
            hdr += [('Content-Length', str(len(body))), ('Content-Type', 'application/json')]
        h = Handler(method, '/api/attachments' + route, body, hdr, defaults)
        handle_http(h, self.service if service else None, KEY, 8786)
        self.assertTrue(h.close_connection)
        self.assertFalse(h.response[1]['fixture'])
        self.assertNotIn(str(self.root), json.dumps(h.response))
        return h
    def draft(self):
        h = self.call('POST', '/drafts', SELECTION, [('Idempotency-Key', self.key())])
        self.assertEqual(h.response[0], 201, h.response)
        return h.response[1]
    def cap(self, d):
        return [('X-Attachment-Draft-Capability', d['capability'])]
    def upload(self, d, body, *, declared=None, fingerprint=None, raw=None, additional=()):
        intent = self.call('POST', '/drafts/' + d['id'] + '/upload-intents', dict(fence(d), **metadata(body)), self.cap(d) + [('Idempotency-Key', self.key())])
        self.assertEqual(intent.response[0], 201, intent.response)
        u = intent.response[1]; d['revision'] = u['draftRevision']
        hdr = self.cap(d) + [('Content-Length', str(len(body) if declared is None else declared)),
                ('Content-Type', 'application/octet-stream'), ('X-Attachment-Writer-Nonce', d['writerNonce']),
                ('X-Attachment-Writer-Generation', str(d['writerGeneration'])), ('X-Attachment-Draft-Revision', str(d['revision'])),
                ('X-Attachment-Upload-Fingerprint', fingerprint or u['fingerprint'])] + list(additional)
        h = Handler('PUT', '/api/attachments/drafts/' + d['id'] + '/uploads/' + u['id'], body if raw is None else raw, hdr)
        handle_http(h, self.service, KEY, 8786)
        return h, u, hdr

    def test_disabled_and_auth_before_any_read(self):
        for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
            h = self.call(method, '/drafts', raw=b'bad', service=False)
            self.assertEqual(h.response[0], 503)
            self.assertEqual(h.rfile.reads, 0)
            h = self.call(method, '/drafts', raw=b'bad', service=False, defaults=False)
            self.assertEqual(h.response[0], 401)
        self.assertEqual(self.resolver.calls, 0)

    def test_auth_origin_duplicates_before_lookup(self):
        for hdr, status in [([('X-Live-Room-Key', KEY)], 401), ([('Host', 'localhost:8786')], 403),
                            ([('Origin', 'https://untrusted.invalid')], 403), ([('Origin', 'http://127.0.0.1:8786')] * 2, 403),
                            ([('X-Attachment-Draft-Capability', 'x')] * 2, 400)]:
            for did in ('0'*32, self.draft()['id']):
                h = self.call('GET', '/drafts/' + did, headers=hdr)
                self.assertEqual(h.response[0], status)
                self.assertEqual(h.rfile.reads, 0)

    def test_strict_route_queries_methods(self):
        for route in ('/storage?extra=x', '/storage#bad', '/%2fstorage', '/drafts?destinationId=x&destinationId=y&scopeId=z',
                      '/capabilities?destinationId=x&scopeId=z&modelId=%zz', '/drafts?scopeId=x', '/drafts?destinationId=x&scopeId=%ff'):
            self.assertEqual(self.call('GET', route).response[0], 400, route)
        self.assertEqual(self.call('PATCH', '/storage', {}).response[0], 405)
        self.assertEqual(self.call('GET', '/missing').response[0], 404)
        self.assertEqual(self.call('GET', '/files').response[0], 400)  # Exact retained context is now required.
        self.assertEqual(self.call('GET', '/storage', raw=b'{}').response[0], 400)

    def test_json_duplicate_fields_unicode_and_limits(self):
        for raw in (b'{"scopeId":"x","scopeId":"y"}', b'{"scopeId":NaN}', b'{"scopeId":1e999}', b'{"scopeId":"\\ud800"}', b'[]', b'\xff'):
            h = self.call('POST', '/conversations', raw=raw, headers=[('Idempotency-Key', self.key())])
            self.assertEqual(h.response[0], 400)
        h = self.call('POST', '/drafts', dict(SELECTION, path='fictional'), [('Idempotency-Key', self.key())])
        self.assertEqual(h.response[0], 400)
        for hdr, status in [([('Content-Length', '99999999')], 413), ([('Content-Length', '2')] * 2, 400),
                            ([('Content-Length', '2'), ('Transfer-Encoding', 'chunked')], 400)]:
            h = Handler('POST', '/api/attachments/drafts', b'{}', hdr + [('Content-Type','application/json'), ('Idempotency-Key', self.key())])
            handle_http(h, self.service, KEY, 8786)
            self.assertEqual(h.response[0], status)
            self.assertEqual(h.rfile.reads, 0)

    def test_binding_and_disappeared_recipient_recovery(self):
        d = self.draft()
        self.resolver.available = False
        h = self.call('POST', '/drafts/' + d['id'] + '/upload-intents', dict(fence(d), **metadata(b'a')), self.cap(d) + [('Idempotency-Key', self.key())])
        self.assertEqual(h.response[0], 503)
        recover = self.call('POST', '/drafts/' + d['id'] + '/recover', {'writerGeneration':d['writerGeneration']})
        self.assertEqual(recover.response[0], 200)
        resumed = self.call('POST', '/drafts/' + d['id'] + '/resume', {'writerGeneration': d['writerGeneration'], 'revision':d['revision']}, [('X-Attachment-Draft-Capability', recover.response[1]['recoveryCapability'])])
        self.assertEqual(resumed.response[0], 200)
        new = resumed.response[1]
        self.assertEqual(self.call('GET', '/drafts/' + d['id'], headers=self.cap(d)).response[0], 403)
        self.assertEqual(self.call('DELETE', '/drafts/' + d['id'], fence(new), self.cap(new) + [('Idempotency-Key', self.key())]).response[0], 200)

    def test_officer_model_and_conversation_bound(self):
        c = self.call('POST','/conversations', {'scopeId': BINDING['scopeId']}, [('Idempotency-Key', self.key())]).response[1]
        selection = dict(SELECTION, destinationId='officer', modelId='fictional-model', conversationId=c['id'])
        d = self.call('POST','/drafts', selection, [('Idempotency-Key', self.key())])
        self.assertEqual(d.response[0], 201)
        self.assertEqual(d.response[1]['destination']['modelId'], 'fictional-model')
        for extra in ({'conversationId':'0'*32}, {'modelId':None}):
            self.assertIn(self.call('POST','/drafts', dict(selection, **extra), [('Idempotency-Key', self.key())]).response[0], (400,403))
        self.resolver.revision = 'fictional-v2'
        self.assertEqual(self.call('POST','/drafts', SELECTION, [('Idempotency-Key', self.key())]).response[0], 409)

    def test_capability_does_not_qualify_pdf_or_runtime(self):
        h = self.call('GET','/capabilities?destinationId=fictional-ada&scopeId=fictional-orchard&modelId=')
        self.assertEqual(h.response[1]['formats'], ['text/plain','text/markdown'])
        self.service.resolver = UnqualifiedResolver()
        h = self.call('GET','/capabilities?destinationId=fictional-ada&scopeId=fictional-orchard&modelId=')
        self.assertFalse(h.response[1]['available'])

    def test_upload_frames_zero_short_bad_digest_replay(self):
        for body in (b'', b'Fictional orchard'):
            d = self.draft(); h, u, hdr = self.upload(d, body)
            self.assertEqual(h.response[0], 200, h.response)
            self.assertEqual(h.response[1]['state'], 'ready')
            replay = Handler('PUT', h.path, b'not-consumed', hdr)
            handle_http(replay,self.service,KEY,8786)
            self.assertEqual(replay.response[0],200)
            self.assertEqual(replay.rfile.reads,0)
            self.assertEqual(self.store.storage()['usedBytes'], len(body))
        for raw in (b'x', b'xxxx'):
            d = self.draft(); h,u,hdr = self.upload(d,b'abcd',raw=raw)
            self.assertEqual(h.response[0],422)
            self.assertEqual(self.store.get_upload(d['id'],d['capability'],u['id'])['state'],'failed')

    def test_length_fingerprint_range_rejected_before_read(self):
        for extra in ({'declared':2}, {'fingerprint':'0'*64}, {'additional':[('Content-Range','bytes 0-0/1')]},
                      {'additional':[('Transfer-Encoding','chunked')]}, {'additional':[('X-Attachment-Writer-Nonce','duplicate')]}):
            d=self.draft(); h,u,hdr=self.upload(d,b'a',**extra)
            self.assertIn(h.response[0],(400,409))
            self.assertEqual(h.rfile.reads,0)
            self.assertEqual(self.store.get_upload(d['id'],d['capability'],u['id'])['state'],'uploading')

    def test_narrow_limits_and_replay_after_full_slot(self):
        original = self.resolver.resolve_destination
        def narrow(*args):
            resolved = original(*args)
            resolved['limits'] = {'fileBytes': 4, 'draftBytes': 4, 'fileCount': 1}
            return resolved
        self.resolver.resolve_destination = narrow
        d = self.draft()
        route = '/drafts/' + d['id'] + '/upload-intents'
        key = self.key(); body = dict(fence(d), **metadata(b'four'))
        headers = self.cap(d) + [('Idempotency-Key', key)]
        first = self.call('POST', route, body, headers)
        self.assertEqual(first.response[0], 201)
        replay = self.call('POST', route, body, headers)
        self.assertEqual(replay.response[1]['id'], first.response[1]['id'])
        d['revision'] = first.response[1]['draftRevision']
        blocked = self.call('POST', route, dict(fence(d), **metadata(b'x')), self.cap(d) + [('Idempotency-Key', self.key())])
        self.assertEqual(blocked.response[0], 413)
        self.assertEqual(self.store.storage()['usedBytes'], 4)

    def test_stalled_binary_remains_incomplete_and_error_is_sanitized(self):
        d = self.draft()
        h, u, headers = self.upload(d, b'abcd', declared=3)
        stalled = Handler('PUT', h.path, b'', [(name, '4' if name == 'Content-Length' else value) for name, value in headers])
        stalled.rfile.read1 = mock.Mock(side_effect=TimeoutError('fictional private data'))
        handle_http(stalled, self.service, KEY, 8786)
        self.assertEqual(stalled.response[0], 422)
        self.assertNotIn('fictional private data', json.dumps(stalled.response))
        self.assertEqual(self.store.get_upload(d['id'], d['capability'], u['id'])['state'], 'failed')
        self.assertEqual(self.store.storage()['usedBytes'], 4)

    def test_reader_deadline_eof_and_short_read(self):
        h=Handler('PUT','/',b'abcdef')
        clock=iter([10,11,12,40])
        reader=LimitedReader(h,3,2,monotonic=lambda:next(clock))
        self.assertEqual(reader.read(100),b'abc')
        self.assertEqual(reader.read(100),b'')
        elapsed = [10]
        reader=LimitedReader(h,2,1,monotonic=lambda: elapsed[0])
        elapsed[0] = 12
        with self.assertRaises(AttachmentError): reader.read(2)
        self.assertEqual(h.rfile.reads,1)

    def test_stopping_service_blocks_without_store_close_under_operation(self):
        with self.service.operation():
            with self.assertRaises(AttachmentError): self.service.close(timeout=0)
            self.assertIsNotNone(self.store.db)
        self.assertEqual(self.call('GET','/storage').response[0],503)


if __name__ == '__main__':
    unittest.main()
