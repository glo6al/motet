"""Disposable actual-HTTP management proof; child process forbids provider networking."""
import concurrent.futures
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
import urllib.parse
from unittest import mock

import officer_document_live_fixture as fixture

KEY = 'fictional-officer-browser-fixture-key'


@unittest.skipUnless(os.name == 'posix', 'Private attachment storage requires POSIX.')
class FixtureProcess(unittest.TestCase):
    expected_fake_calls = 0
    expected_ordinary_blocks = 0
    expected_ordinary_calls = None
    expected_ordinary_marker_hits = 0
    expected_seeded_answer_calls = None

    def start(self, outcome='normal', *, arguments=None):
        flags = ['--reading-off', '--deletion-outcome', outcome] if arguments is None else arguments
        self.child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('officer_document_live_fixture.py')),
            *flags], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.stopped = False
        self.addCleanup(self.stop)
        ready, _, _ = select.select([self.child.stdout], [], [], 10)
        self.assertTrue(ready, 'Fixture startup timed out')
        line = self.child.stdout.readline()
        match = re.search(r'http://127\.0\.0\.1:\d+', line)
        self.assertIsNotNone(match, 'Fixture did not start')
        self.origin = match[0]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if arguments is not None:
            return
        facts = self.request('/officer/model-selection')
        self.assertTrue(facts['attachments_enabled'])
        self.assertFalse(facts['documents_enabled'])
        cid = self.request('/api/attachments/conversations/current?scopeId=room')['conversation']['id']
        self.management = '/api/attachments/conversations/' + cid + '/management?destinationId=officer&scopeId=room'
        view = self.request(self.management)
        self.assertEqual(len(view['files']), 1)
        self.file = view['files'][0]
        self.cid = cid
        self.used = self.request('/api/attachments/storage')['usedBytes']
        self.assertGreater(self.used, 0)

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.child.poll() is None:
            self.child.send_signal(signal.SIGINT)
        try:
            out, err = self.child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            out, err = self.child.communicate(timeout=5)
            self.fail('Fixture failed to close its owned worker: ' + err[-500:])
        self.assertEqual(self.child.returncode, 0, err[-1000:])
        stats = json.loads(out.strip().splitlines()[-1])
        expected = dict(fakeProviderCalls=self.expected_fake_calls,
                        ordinaryAskBlocked=self.expected_ordinary_blocks, outboundBlocked=0)
        if self.expected_ordinary_calls is not None:
            expected.update(ordinaryFakeCalls=self.expected_ordinary_calls,
                            ordinaryFileMarkerHits=self.expected_ordinary_marker_hits)
        if self.expected_seeded_answer_calls is not None:
            expected['seededAnswerCalls'] = self.expected_seeded_answer_calls
        self.assertEqual(stats, expected)

    def request(self, path, body=None, headers=None, method=None):
        h = {'X-Live-Room-Key': KEY, **(headers or {})}
        if body is not None:
            h.setdefault('Content-Type', 'application/octet-stream' if isinstance(body, bytes) else 'application/json')
        raw = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.origin + path, data=raw, headers=h, method=method)
        with self.opener.open(req, timeout=5) as response:
            return json.load(response)

    def prepare(self):
        return self.request('/api/attachments/files/' + self.file['id'] + '/deletion-intent',
            dict(destinationId='officer', scopeId='room', conversationId=self.cid,
                 managementGeneration=self.file['managementGeneration']), {'Idempotency-Key': 'fictional-prepare'})

    def confirm(self, op):
        return self.request('/api/attachments/deletions/' + op['id'] + '/confirm', {'fingerprint': op['fingerprint']},
            {'X-Attachment-Deletion-Capability': op['capability'], 'Idempotency-Key': 'fictional-confirm'})

    def status(self, op):
        return self.request('/api/attachments/deletions/' + op['id'], headers={'X-Attachment-Deletion-Capability': op['capability']})

    def terminal(self, op, expected):
        # Bounded test synchronization only. Production status never polls or drives cleanup.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.status(op)
            if result['state'] == expected:
                return result
            time.sleep(.02)
        self.fail('Expected fixture deletion state ' + expected + ', received ' + result['state'])


class ManagementFixtureTests(FixtureProcess):
    def test_management_only_delete_and_exact_replay(self):
        self.start()
        op = self.prepare()
        self.assertEqual(op['state'], 'confirm')
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], self.used)
        self.confirm(op)
        done = self.terminal(op, 'deleted')
        self.assertEqual(done['releasedBytes'], self.file['bytes'])
        self.assertTrue(done['sourceUnavailable'])
        self.confirm(op)
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], 0)
        view = self.request(self.management)
        self.assertFalse(view['files'][0]['available'])
        self.assertEqual(view['deletions'][0]['state'], 'deleted')

    def test_pending_retains_charge_until_explicit_fixture_release(self):
        self.start('pending')
        op = self.prepare()
        self.confirm(op)
        self.assertEqual(self.status(op)['state'], 'deleting')
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], self.used)
        self.request('/fixture/release-deletion', {})
        self.terminal(op, 'deleted')
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], 0)

    def test_changed_source_is_not_called_deleted(self):
        self.start('changed')
        op = self.prepare()
        self.confirm(op)
        result = self.terminal(op, 'partial_failure')
        self.assertEqual(result['reason'], 'identity_changed')
        self.assertEqual(result['releasedBytes'], 0)
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], self.used)

    def test_unconfirmed_removal_keeps_full_charge(self):
        self.start('unknown')
        op = self.prepare()
        self.confirm(op)
        result = self.terminal(op, 'unknown')
        self.assertEqual(result['reason'], 'removal_unconfirmed')
        self.assertEqual(result['releasedBytes'], 0)
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], self.used)


class FixtureExtensionTests(FixtureProcess):
    def test_shutdown_joins_event_stream_and_partial_header_handler(self):
        self.start(arguments=[])
        parsed = urllib.parse.urlsplit(self.origin)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as events, \
             socket.create_connection((parsed.hostname, parsed.port), timeout=3) as partial:
            events.sendall(('GET /events?k=' + KEY + ' HTTP/1.1\r\nHost: ' + parsed.netloc + '\r\n\r\n').encode())
            received = b''
            while b'fictional-browser' not in received:
                received += events.recv(8192)
            partial.sendall(('GET /fixture/metadata HTTP/1.1\r\nHost: ' + parsed.netloc + '\r\n').encode())
            # A further completed request establishes that the listener has
            # accepted the earlier partial request before shutdown.
            self.request('/fixture/metadata')
            self.stop()
            self.assertEqual(events.recv(8192), b'')
            self.assertEqual(partial.recv(8192), b'')

    def current(self):
        return self.request('/api/attachments/conversations/current?scopeId=room')['conversation']

    def selection(self, cid):
        cap = self.request('/api/attachments/capabilities?destinationId=officer&scopeId=room&modelId=claude-haiku-5-5')
        return dict(destinationId='officer', scopeId='room', modelId='claude-haiku-5-5', conversationId=cid,
                    capabilityRevision=cap['capabilityRevision'])

    def test_seed_51_uses_real_pagination_and_distinct_creation_times_without_calls(self):
        self.start(arguments=['--seed-file', '--seed-count', '51'])
        cid = self.current()['id']
        route = '/api/attachments/conversations/' + cid + '/management?destinationId=officer&scopeId=room'
        first = self.request(route)
        second = self.request(route + '&cursor=' + urllib.parse.quote(first['nextCursor']))
        self.assertEqual((len(first['files']), len(second['files'])), (50, 1))
        self.assertEqual(len({f['id'] for f in first['files'] + second['files']}), 51)
        self.assertTrue(all(f['available'] for f in first['files'] + second['files']))
        history = '/api/attachments/conversations/' + cid + '/requests?destinationId=officer&scopeId=room'
        page = self.request(history)
        rest = self.request(history + '&cursor=' + urllib.parse.quote(page['nextCursor']))
        rows = page['items'] + rest['items']
        times = sorted(r['createdAt'] for r in rows)
        self.assertEqual(len(set(times)), 51)
        self.assertLess(times[-1] - times[0], 60, 'fictional clock advancement must remain bounded')
        self.assertTrue(all(r['state'] == 'retained_local' and r['notAskedReason'] == 'no_question' for r in rows))
        self.assertEqual(self.request('/fixture/metadata')['fakeProviderCalls'], 0)

    def test_generic_failure_is_real_consumed_intent_not_provider_rejection(self):
        self.start(arguments=['--seed-file', '--outcome', 'generic_failed'])
        cid = self.current()['id']
        data = dict(self.selection(cid), question='Fictional generic-failure question.', keptFileIds=[f['id'] for f in self.request(
                '/api/attachments/files?destinationId=officer&scopeId=room&conversationId='+cid)['items']], priorAnswerIds=[])
        prepared = self.request('/api/attachments/conversations/' + cid + '/question-intent', data,
                                {'Idempotency-Key': 'fictional-generic-question'})
        result = self.request('/api/attachments/requests/' + prepared['requestId'] + '/ask',
                              {'fingerprint': prepared['fingerprint']},
                              {'X-Attachment-Request-Capability': prepared['requestCapability'],
                               'Idempotency-Key': 'fictional-generic-execute'})
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['failure'])
        self.assertIsNone(result['notAskedReason'])
        self.assertEqual(result['officerDelivery'], {'state': 'not_sent', 'requestId': None})
        self.assertFalse(result['canAbandon'])
        self.assertEqual(self.request('/fixture/metadata')['fakeProviderCalls'], 0)

    def test_unreadable_management_uses_actual_error_envelope_and_one_read_recovery(self):
        self.start(arguments=['--reading-off', '--management-fault', 'unreadable-once'])
        cid = self.current()['id']
        route = '/api/attachments/conversations/' + cid + '/management?destinationId=officer&scopeId=room'
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(route)
        self.assertEqual(caught.exception.code, 503)
        with caught.exception as response:
            error = json.load(response)
        self.assertEqual(error['schemaVersion'], 1)
        self.assertFalse(error['fixture'])
        self.assertEqual(error['error']['code'], 'storage_unavailable')
        self.assertEqual(len(self.request(route)['files']), 1)
        self.assertEqual(self.request('/fixture/metadata')['fixtureFaults']['managementReadFaults'], 1)

    def test_receiver_interruption_has_partial_bytes_real_failed_state_and_no_admission(self):
        self.start(arguments=['--upload-fault', 'interrupted-once'])
        cid = self.request('/api/attachments/conversations/current', {'scopeId': 'room'})['conversation']['id']
        draft = self.request('/api/attachments/drafts', self.selection(cid), {'Idempotency-Key': 'fictional-upload-draft'})
        raw = b'%PDF-1.4\nFictional interrupted body.\n%%EOF'
        cap = {'X-Attachment-Draft-Capability': draft['capability']}
        metadata = dict(revision=draft['revision'], writerGeneration=draft['writerGeneration'], writerNonce=draft['writerNonce'],
                        name='fictional-interrupted.pdf', mediaType='application/pdf', bytes=len(raw), digest=hashlib.sha256(raw).hexdigest())
        upload = self.request('/api/attachments/drafts/' + draft['id'] + '/upload-intents', metadata,
                              dict(cap, **{'Idempotency-Key': 'fictional-upload-reserve'}))
        headers = dict(cap, **{'X-Attachment-Writer-Nonce': draft['writerNonce'],
            'X-Attachment-Writer-Generation': str(draft['writerGeneration']),
            'X-Attachment-Draft-Revision': str(upload['draftRevision']),
            'X-Attachment-Upload-Fingerprint': upload['fingerprint']})
        path = '/api/attachments/drafts/' + draft['id'] + '/uploads/' + upload['id']
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(path, raw, headers, 'PUT')
        self.assertEqual(caught.exception.code, 422)
        with caught.exception as response:
            self.assertEqual(json.load(response)['error']['code'], 'upload_failed')
        failed = self.request(path, headers=cap)
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['error'], 'upload_failed')
        saved = self.request('/api/attachments/drafts/' + draft['id'], headers=cap)
        self.assertEqual(saved['files'][0]['state'], 'failed')
        self.assertEqual(self.request('/api/attachments/storage')['usedBytes'], len(raw))
        history = self.request('/api/attachments/conversations/' + cid + '/requests?destinationId=officer&scopeId=room')
        self.assertEqual(history['items'], [])
        facts = self.request('/fixture/metadata')
        self.assertEqual(facts['fixtureFaults']['interruptedUploads'], 1)
        self.assertEqual(facts['fixtureFaults']['uploadBytesBeforeInterruption'], 8)
        self.assertEqual(facts['fakeProviderCalls'], 0)
        metadata.update(revision=saved['revision'], name='fictional-next-upload.pdf')
        next_upload = self.request('/api/attachments/drafts/' + draft['id'] + '/upload-intents', metadata,
                                   dict(cap, **{'Idempotency-Key': 'fictional-next-reserve'}))
        headers.update({'X-Attachment-Draft-Revision': str(next_upload['draftRevision']),
                        'X-Attachment-Upload-Fingerprint': next_upload['fingerprint']})
        next_path = '/api/attachments/drafts/' + draft['id'] + '/uploads/' + next_upload['id']
        self.assertEqual(self.request(next_path, raw, headers, 'PUT')['state'], 'ready')
        self.assertEqual(self.request('/fixture/metadata')['fixtureFaults']['interruptedUploads'], 1)


class MarkerOnlyFixtureTests(FixtureProcess):
    expected_fake_calls = 1

    def start_marker(self, *, seed_pdf=False):
        self.start(arguments=['--outcome', 'answered-marker-only'] + (['--seed-file'] if seed_pdf else []))
        self.assertEqual(self.request('/fixture/metadata')['fakeProviderCalls'], 0)
        cid = self.request('/api/attachments/conversations/current', {'scopeId': 'room'})['conversation']['id']
        cap = self.request('/api/attachments/capabilities?destinationId=officer&scopeId=room&modelId=claude-haiku-5-5')
        self.marker_selection = dict(destinationId='officer', scopeId='room', modelId='claude-haiku-5-5',
                                    conversationId=cid, capabilityRevision=cap['capabilityRevision'])
        return cid

    def retain_png(self):
        raw = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6zs8AAAAASUVORK5CYII=')
        draft = self.request('/api/attachments/drafts', self.marker_selection,
                             {'Idempotency-Key': 'fictional-marker-draft'})
        cap = {'X-Attachment-Draft-Capability': draft['capability']}
        fence = {key: draft[key] for key in ('revision', 'writerGeneration', 'writerNonce')}
        upload = self.request('/api/attachments/drafts/' + draft['id'] + '/upload-intents',
            dict(fence, name='fictional-marker.png', mediaType='image/png', bytes=len(raw), digest=hashlib.sha256(raw).hexdigest()),
            dict(cap, **{'Idempotency-Key': 'fictional-marker-upload'}))
        ready = self.request('/api/attachments/drafts/' + draft['id'] + '/uploads/' + upload['id'], raw,
            dict(cap, **{'X-Attachment-Writer-Nonce': draft['writerNonce'],
                         'X-Attachment-Writer-Generation': str(draft['writerGeneration']),
                         'X-Attachment-Draft-Revision': str(upload['draftRevision']),
                         'X-Attachment-Upload-Fingerprint': upload['fingerprint']}), 'PUT')
        self.assertEqual(ready['state'], 'ready')
        fence['revision'] = ready['draftRevision']
        prepared = self.request('/api/attachments/drafts/' + draft['id'] + '/send-intent', fence,
                                dict(cap, **{'Idempotency-Key': 'fictional-marker-keep-intent'}))
        kept = self.request('/api/attachments/requests/' + prepared['requestId'] + '/send',
            dict(fence, fingerprint=prepared['fingerprint']),
            dict(cap, **{'X-Attachment-Request-Capability': prepared['requestCapability'],
                         'Idempotency-Key': 'fictional-marker-keep'}))
        self.assertEqual(kept['state'], 'retained_local')
        self.assertEqual(self.request('/fixture/metadata')['fakeProviderCalls'], 0)
        return upload['fileId']

    def ask_marker(self, cid):
        prepared = self.request('/api/attachments/conversations/' + cid + '/question-intent',
            dict(self.marker_selection, question='Describe the fictional originals.', keptFileIds=[f['id'] for f in self.request(
                '/api/attachments/files?destinationId=officer&scopeId=room&conversationId='+cid)['items']], priorAnswerIds=[]),
            {'Idempotency-Key': 'fictional-marker-question'})
        headers = {'X-Attachment-Request-Capability': prepared['requestCapability']}
        result = self.request('/api/attachments/requests/' + prepared['requestId'] + '/ask',
            {'fingerprint': prepared['fingerprint']}, dict(headers, **{'Idempotency-Key': 'fictional-marker-ask'}))
        self.assertEqual(self.request('/fixture/metadata')['fakeProviderCalls'], 1)
        return prepared, result, headers

    def test_png_only_marker_answer_has_empty_public_text_and_exact_source(self):
        cid = self.start_marker()
        image_id = self.retain_png()
        prepared, result, headers = self.ask_marker(cid)
        self.assertEqual(result['state'], 'answered')
        answer = self.request('/api/attachments/answers/' + result['answerId'] + '?requestId=' + prepared['requestId'], headers=headers)
        self.assertEqual(answer['text'], '')
        self.assertEqual(answer['sourceValidation'], 'current')
        self.assertEqual(answer['suppliedFileIds'], [image_id])
        self.assertEqual([(c['fileId'], c['location']) for c in answer['citations']], [(image_id, {'image': True})])

    def test_mixed_pdf_png_marker_answer_keeps_both_real_typed_citations(self):
        cid = self.start_marker(seed_pdf=True)
        management = self.request('/api/attachments/conversations/' + cid + '/management?destinationId=officer&scopeId=room')
        pdf_id = management['files'][0]['id']
        image_id = self.retain_png()
        prepared, result, headers = self.ask_marker(cid)
        self.assertEqual(result['state'], 'answered')
        answer = self.request('/api/attachments/answers/' + result['answerId'] + '?requestId=' + prepared['requestId'], headers=headers)
        self.assertEqual(answer['text'], '')
        self.assertEqual(answer['sourceValidation'], 'current')
        self.assertEqual(len(answer['suppliedFileIds']), 2)
        self.assertEqual(set(answer['suppliedFileIds']), {pdf_id, image_id})
        self.assertEqual({(c['fileId'], json.dumps(c['location'], sort_keys=True)) for c in answer['citations']},
                         {(pdf_id, '{"page": 2}'), (image_id, '{"image": true}')})

    def test_marker_outcome_without_an_image_remains_unknown_without_an_answer(self):
        cid = self.start_marker(seed_pdf=True)
        prepared, result, headers = self.ask_marker(cid)
        self.assertEqual(result['state'], 'unknown')
        self.assertIsNone(result['answerId'])
        status = self.request('/api/attachments/requests/' + prepared['requestId'], headers=headers)
        self.assertEqual(status['state'], 'unknown')
        self.assertIsNone(status['answerId'])


class OrdinaryFixtureTests(FixtureProcess):
    def ordinary(self, text='Fictional activity question.'):
        return dict(turns=[{'role': 'user', 'content': text, 'context': {'scope': 'room'}}],
                    context={'scope': 'room'}, request_id='00000000-0000-4000-8000-000000000001')

    def assert_http_error(self, want, route, data, headers=None):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(route, data, headers)
        with raised.exception as response:
            self.assertEqual(response.code, want)
            return response.read()

    def assert_fixed_answer(self, answer):
        self.assertEqual(answer, {'text': 'Fictional offline ordinary answer. No provider was contacted.',
                                  'actions': [], 'cites': []})

    def test_default_ordinary_question_stays_blocked(self):
        self.expected_ordinary_blocks = 1
        self.start(arguments=[])
        self.assertIn(b'blocked', self.assert_http_error(503, '/ask', self.ordinary()))
        metadata = self.request('/fixture/metadata')
        self.assertEqual(metadata['ordinaryOfficer'], 'blocked')
        self.assertEqual(metadata['ordinaryAskBlocked'], 1)
        self.assertNotIn('ordinaryFakeCalls', metadata)

    def test_ordinary_opt_in_checks_owner_origin_body_and_reports_only_marker_count(self):
        self.expected_ordinary_calls = 2
        self.expected_ordinary_marker_hits = 1
        self.start(arguments=['--ordinary-answer'])
        self.assert_http_error(403, '/ask', self.ordinary(), {'X-Live-Room-Key': 'fictional-wrong-key'})
        self.assert_http_error(403, '/ask', self.ordinary(), {'Origin': 'http://fictional.invalid'})
        self.assert_http_error(403, '/ask', self.ordinary(), {'Host': '127.0.0.1:1'})
        self.assert_http_error(400, '/ask?unexpected=1', self.ordinary())
        self.assert_http_error(400, '/ask', dict(self.ordinary(), attachment={'content': 'fictional injected bytes'}))
        self.assert_http_error(400, '/ask', {'turns': [{'role': 'assistant', 'content': 'Last role is not owner.'}]})
        self.assert_http_error(400, '/ask', b'x' * 200001, {'Content-Type': 'application/json'})
        self.assert_http_error(503, '/fictional-other-route', self.ordinary())
        self.assert_fixed_answer(self.request('/ask', self.ordinary()))
        with_marker = self.ordinary('Distinct fictional follow-up.')
        with_marker['turns'].insert(0, {'role': 'user', 'content': fixture.ORDINARY_FILE_MARKER})
        self.assert_fixed_answer(self.request('/ask', with_marker))
        metadata = self.request('/fixture/metadata')
        self.assertEqual(metadata['ordinaryOfficer'], 'fictional-answer')
        self.assertEqual(metadata['ordinaryFakeCalls'], 2)
        self.assertEqual(metadata['ordinaryFileMarkerHits'], 1)
        self.assertNotIn(fixture.ORDINARY_FILE_MARKER, json.dumps(metadata))
        self.assertNotIn('Distinct fictional follow-up.', json.dumps(metadata))

    def test_ordinary_file_ordinary_uses_separate_fictional_counters(self):
        self.expected_fake_calls = 1
        self.expected_ordinary_calls = 2
        self.start(arguments=['--seed-file', '--ordinary-answer'])
        initial = self.request('/fixture/metadata')
        self.assertEqual((initial['ordinaryFakeCalls'], initial['fakeProviderCalls'], initial['outboundBlocked']), (0, 0, 0))
        self.assert_fixed_answer(self.request('/ask', self.ordinary()))
        cid = self.request('/api/attachments/conversations/current?scopeId=room')['conversation']['id']
        capabilities = self.request('/api/attachments/capabilities?destinationId=officer&scopeId=room&modelId=claude-haiku-5-5')
        prepared = self.request('/api/attachments/conversations/' + cid + '/question-intent',
            dict(destinationId='officer', scopeId='room', conversationId=cid, modelId='claude-haiku-5-5',
                 capabilityRevision=capabilities['capabilityRevision'], question=fixture.ORDINARY_FILE_MARKER, keptFileIds=[f['id'] for f in self.request(
                '/api/attachments/files?destinationId=officer&scopeId=room&conversationId='+cid)['items']], priorAnswerIds=[]),
            {'Idempotency-Key': 'fictional-file-between-ordinary-prepare'})
        answered = self.request('/api/attachments/requests/' + prepared['requestId'] + '/ask',
            {'fingerprint': prepared['fingerprint']}, {'X-Attachment-Request-Capability': prepared['requestCapability'],
             'Idempotency-Key': 'fictional-file-between-ordinary-execute'})
        self.assertEqual(answered['state'], 'answered')
        later = self.ordinary('A later fictional ordinary question.')
        later['request_id'] = '00000000-0000-4000-8000-000000000002'
        self.assert_fixed_answer(self.request('/ask', later))
        metadata = self.request('/fixture/metadata')
        self.assertEqual((metadata['ordinaryFakeCalls'], metadata['fakeProviderCalls']), (2, 1))
        self.assertEqual(metadata['ordinaryFileMarkerHits'], 0)


class StationAndAnswerSeedTests(FixtureProcess):
    def conversation(self, scope):
        return self.request('/api/attachments/conversations/current?' + urllib.parse.urlencode({'scopeId': scope}))['conversation']['id']

    def context_route(self, conversation, action, scope):
        return '/api/attachments/conversations/' + conversation + '/' + action + '?' + urllib.parse.urlencode(
            {'destinationId': 'officer', 'scopeId': scope})

    def capabilities(self, scope):
        return self.request('/api/attachments/capabilities?' + urllib.parse.urlencode(
            {'destinationId': 'officer', 'scopeId': scope, 'modelId': 'auto'}))

    def test_fixed_station_snapshot_registers_only_one_scope_and_keeps_distinct_files(self):
        self.start(arguments=['--fictional-station', '--seed-file', '--model', '@automatic'])
        metadata = self.request('/fixture/metadata')
        self.assertEqual(metadata['fictionalStationScope'], fixture.STATION_SCOPE)
        self.assertEqual(metadata['fakeProviderCalls'], 0)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.opener.open(self.origin + '/events', timeout=2)
        with denied.exception as response:
            self.assertEqual(response.code, 403)
        with self.opener.open(self.origin + '/events?k=' + KEY, timeout=3) as events:
            self.assertEqual(events.headers.get_content_type(), 'text/event-stream')
            first = events.readline(8192)
            self.assertEqual(events.readline(8192), b'\n')
            second = events.readline(8192)
            self.assertTrue(first.startswith(b'data: '))
            self.assertTrue(second.startswith(b'data: '))
            hello, agent = json.loads(first[6:]), json.loads(second[6:])
            self.assertEqual(hello['kind'], 'hello')
            self.assertEqual(agent, dict(kind='agent', agent=fixture.STATION_AGENT, name='Fictional Station',
                                         runtime='Codex', old=True, ts=0, snap=True, boot=hello['boot'], seq=-1))
        scopes = ('room', fixture.STATION_SCOPE)
        conversations = [self.conversation(scope) for scope in scopes]
        self.assertNotEqual(*conversations)
        files = []
        for conversation, scope in zip(conversations, scopes):
            self.assertTrue(self.capabilities(scope)['available'])
            management = self.request(self.context_route(conversation, 'management', scope))
            self.assertEqual(len(management['files']), 1)
            self.assertTrue(management['files'][0]['available'])
            files.append(management['files'][0])
        self.assertNotEqual(files[0]['id'], files[1]['id'])
        self.assertEqual([row['name'] for row in files], ['fictional-kept.pdf', 'fictional-station-kept.pdf'])
        self.assertFalse(self.capabilities('station:x:00000000-0000-4000-8000-000000000002')['available'])
        self.assertFalse(self.request('/agent-messages')['available'])
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.request('/agent-message', {'agent': fixture.STATION_AGENT, 'question': 'Fictional question.'})
        with blocked.exception as response:
            self.assertEqual(response.code, 503)

    def available_requests(self, cid, scope):
        records = self.request(self.context_route(cid, 'requests', scope))['items']
        return sorted((row for row in records if row['state'] == 'answered'),
                      key=lambda row: (row['createdAt'], row['requestId']))

    def read_answer(self, record):
        return self.request('/api/attachments/answers/' + record['answerId'] + '?requestId=' + record['requestId'],
                            headers={'X-Attachment-Request-Capability': record['requestCapability']})

    def test_nine_seeded_room_answers_make_next_real_followup_omit_the_oldest(self):
        self.expected_fake_calls = 10
        self.expected_seeded_answer_calls = 9
        self.start(arguments=['--seed-file', '--seed-answers', '9', '--model', '@automatic'])
        metadata = self.request('/fixture/metadata')
        self.assertEqual((metadata['seedAnswerCount'], metadata['seedAnswerScope'], metadata['seededAnswerCalls'],
                          metadata['fakeProviderCalls']), (9, 'room', 9, 9))
        self.assertEqual(self.request('/officer/model-selection')['selected_model'], '@automatic')
        self.assertFalse(self.capabilities(fixture.STATION_SCOPE)['available'])
        cid = self.conversation('room')
        history = self.available_requests(cid, 'room')
        self.assertEqual(len(history), 9)
        self.assertEqual([record['question'] for record in history],
                         ['Fictional earlier file question %02d.' % i for i in range(1, 10)])
        first, ninth = self.read_answer(history[0]), self.read_answer(history[-1])
        self.assertEqual(first['priorAnswerWindow'], dict(policy='recent_available_8', includedCount=0, omittedOlder=False))
        self.assertEqual(ninth['priorAnswerWindow'], dict(policy='recent_available_8', includedCount=8, omittedOlder=False))
        selected = self.capabilities('room')
        prepared = self.request('/api/attachments/conversations/' + cid + '/question-intent',
            dict(destinationId='officer', scopeId='room', conversationId=cid, modelId='auto',
                 capabilityRevision=selected['capabilityRevision'], question='Fictional tenth question.',
                 priorAnswerPolicy='recent_available_8', keptFileIds=[f['id'] for f in self.request(
                '/api/attachments/files?destinationId=officer&scopeId=room&conversationId='+cid)['items']], priorAnswerIds=[row['answerId'] for row in history[-8:]]),
            {'Idempotency-Key': 'fictional-seed-tenth-prepare'})
        last = self.request('/api/attachments/requests/' + prepared['requestId'] + '/ask',
            {'fingerprint': prepared['fingerprint']}, {'X-Attachment-Request-Capability': prepared['requestCapability'],
             'Idempotency-Key': 'fictional-seed-tenth-execute'})
        self.assertEqual(last['state'], 'answered')
        shown = self.read_answer(dict(last, requestCapability=prepared['requestCapability']))
        self.assertEqual(shown['model']['id'], 'claude-haiku-5-5')
        self.assertEqual(shown['priorAnswerWindow'], dict(policy='recent_available_8', includedCount=8, omittedOlder=True))

    def test_station_answer_seed_does_not_place_any_answers_in_room(self):
        self.expected_fake_calls = 9
        self.expected_seeded_answer_calls = 9
        self.start(arguments=['--fictional-station', '--seed-file', '--seed-answers', '9',
                              '--seed-answer-scope', 'station', '--model', '@automatic'])
        station = self.conversation(fixture.STATION_SCOPE)
        room = self.conversation('room')
        history = self.available_requests(station, fixture.STATION_SCOPE)
        self.assertEqual(len(history), 9)
        self.assertEqual(self.available_requests(room, 'room'), [])
        self.assertEqual(self.request('/fixture/metadata')['seedAnswerScope'], fixture.STATION_SCOPE)
        management = self.request(self.context_route(station, 'management', fixture.STATION_SCOPE))
        self.assertEqual(self.read_answer(history[-1])['suppliedFileIds'], [management['files'][0]['id']])


class FixtureBoundaryTests(unittest.TestCase):
    def test_advancing_clock_is_monotonic_thread_safe_and_follows_elapsed_time(self):
        elapsed = [0.0]
        clock = fixture.AdvancingClock(1000, monotonic=lambda: elapsed[0])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: clock(), range(100)))
        self.assertEqual(len(set(values)), 100)
        self.assertLess(max(values) - min(values), 1)
        elapsed[0] = 10
        self.assertGreaterEqual(clock(), 1010)

    def test_instrumentation_is_opt_in_nonce_preserving_and_reversible(self):
        body = b'<html><script nonce="fictional-nonce">const production=true;</script></html>'
        self.assertIs(fixture.instrument_page(body, 'none'), body)
        for mode in fixture.BROWSER_FAULTS[1:]:
            result = fixture.instrument_page(body, mode)
            start = result.index(fixture.INSTRUMENT_START)
            end = result.index(fixture.INSTRUMENT_END) + len(fixture.INSTRUMENT_END)
            self.assertEqual(result[:start - 1] + result[end:], body)
            self.assertEqual(result.count(b'<script nonce='), 1)
            self.assertEqual(result.count(b'</script>'), 1)
            self.assertIn(json.dumps(mode).encode(), result)
            self.assertLess(start, result.index(b'const production'))

    def test_invalid_seam_source_and_closed_enums_fail_before_startup(self):
        body = b'<script nonce="fictional">production();</script>'
        with self.assertRaises(ValueError): fixture.instrument_page(body, 'invented')
        with self.assertRaises(RuntimeError): fixture.instrument_page(body + body, fixture.BROWSER_FAULTS[1])
        for source in (b'// </ScRiPt>', b'x' * 20001, b'no placeholder'):
            with mock.patch.object(Path, 'open', return_value=io.BytesIO(source)), self.assertRaises(RuntimeError):
                fixture.instrument_page(body, fixture.BROWSER_FAULTS[1])
        for flags in (['--browser-fault', 'invented'], ['--seed-file', '--seed-count', '50'],
                      ['--seed-count', '51'], ['--upload-fault', 'invented'], ['--management-fault', 'invented'],
                      ['--seed-file', '--seed-answers', '10'], ['--seed-answers', '9'],
                      ['--seed-file', '--seed-answers', '9', '--seed-answer-scope', 'station'],
                      ['--seed-file', '--seed-answers', '9', '--reading-off'],
                      ['--seed-file', '--seed-answers', '9', '--outcome', 'unknown'],
                      ['--seed-file', '--seed-answers', '9', '--seed-count', '51'], ['--seed-answer-scope', 'room']):
            result = subprocess.run([sys.executable, fixture.__file__, '--metadata', *flags], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('OFFLINE fixture URL', result.stdout)


if __name__ == '__main__':
    unittest.main()
