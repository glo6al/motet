#!/usr/bin/env python3
"""Disposable, offline browser fixture for the actual WATCH document page.

Run: python3 tests/officer_document_live_fixture.py
Open the printed fictional-key URL. Stop with Ctrl-C: the listener, source bytes,
and SQLite state are closed and the temporary directory is removed. No production
gates, policy files, credentials, session logs, or live Officer calls are used.
--metadata prints fixture information without importing the server or opening state.
--outcome selects a fixed fictional result; --model @automatic exercises the real saved Auto selection.
--ordinary-answer enables one fixed fictional response for authenticated ordinary questions.
--fictional-station adds one synthetic station. With --seed-file, both scopes have a kept PDF.
--seed-file --seed-answers 9 --model @automatic prepares nine governed fictional room answers.
Add --fictional-station --seed-answer-scope station to prepare them at the synthetic station instead.
"""
import argparse
from contextlib import ExitStack
import faulthandler
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

KEY = 'fictional-officer-browser-fixture-key'
CLAIM = 'fictional-officer-browser-fixture-claim'
OUTCOMES = ('answered', 'answered-no-citations', 'answered-marker-only', 'rejected', 'unknown', 'budget_exhausted', 'context_too_large', 'reading_unavailable', 'generic_failed')
MODELS = ('claude-haiku-5-5', '@automatic')
BROWSER_FAULTS = ('none', 'confirm-before-send-once', 'question-intent-after-response-once',
                  'upload-pause-once', 'upload-network-loss-once')
INSTRUMENT_START = b'/* BEGIN OFFICER OFFLINE FIXTURE INSTRUMENTATION */\n'
INSTRUMENT_END = b'/* END OFFICER OFFLINE FIXTURE INSTRUMENTATION */\n'
ORDINARY_FILE_MARKER = 'WATCH_OFFLINE_FILE_QUESTION_MARKER'
ORDINARY_TEXT = 'Fictional offline ordinary answer. No provider was contacted.'
STATION_AGENT = 'x:00000000-0000-4000-8000-000000000001'
STATION_SCOPE = 'station:' + STATION_AGENT
STATION_NAME = 'Fictional Station'


class AdvancingClock:
    """Fictional creation chronology only, never a production arrival guarantee."""
    def __init__(self, base, monotonic=time.monotonic):
        self.base, self.monotonic = base, monotonic
        self.started, self.previous, self.lock = monotonic(), base, threading.Lock()

    def __call__(self):
        with self.lock:
            self.previous = max(self.base + self.monotonic() - self.started, self.previous + .001)
            return self.previous


def instrument_page(body, fault):
    """Opt-in test code in the existing nonce, with production bytes untouched."""
    if fault not in BROWSER_FAULTS:
        raise ValueError('Unknown fictional browser fault')
    if fault == 'none':
        return body
    script = re.findall(br'<script nonce="[^"<>\s]+">', body)
    if len(script) != 1 or body.count(INSTRUMENT_START):
        raise RuntimeError('The offline page script seam could not be verified')
    path = Path(__file__).parent / 'fixtures' / 'officer_documents' / 'browser_faults.js'
    with path.open('rb') as source:
        wrapper = source.read(20001)
    placeholder = b'"__OFFICER_BROWSER_FAULT__"'
    if len(wrapper) > 20000 or b'</script' in wrapper.lower() or wrapper.count(placeholder) != 1:
        raise RuntimeError('The fixed offline wrapper cannot be embedded')
    wrapper = wrapper.replace(placeholder, json.dumps(fault).encode())
    instrumentation = INSTRUMENT_START + wrapper + b'\n' + INSTRUMENT_END
    return body.replace(script[0], script[0] + b'\n' + instrumentation, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', action='store_true')
    parser.add_argument('--outcome', choices=OUTCOMES, default='answered')
    parser.add_argument('--ordinary-answer', action='store_true', help='Answer ordinary questions with fixed fictional text')
    parser.add_argument('--model', choices=MODELS, default=MODELS[0])
    parser.add_argument('--reading-off', action='store_true', help='Seed one fictional kept file, then serve local management without document reading')
    parser.add_argument('--seed-file', action='store_true', help='Keep one fictional PDF locally without a model call')
    parser.add_argument('--seed-count', type=int, choices=(1, 3, 51), default=1)
    parser.add_argument('--fictional-station', action='store_true', help='Show one fixed fictional agent station')
    parser.add_argument('--seed-answers', type=int, choices=(9,), default=0,
                        help='Explicitly seed nine governed answers using the local fake provider')
    parser.add_argument('--seed-answer-scope', choices=('room', 'station'))
    parser.add_argument('--deletion-outcome', choices=('normal', 'pending', 'changed', 'unknown'), default='normal')
    parser.add_argument('--browser-fault', choices=BROWSER_FAULTS, default='none')
    parser.add_argument('--management-fault', choices=('none', 'unreadable-once'), default='none')
    parser.add_argument('--upload-fault', choices=('none', 'interrupted-once'), default='none')
    parser.add_argument('--trace-startup', action='store_true', help='Print a traceback if startup stalls')
    args = parser.parse_args()
    if args.seed_count != 1 and not (args.seed_file or args.reading_off):
        parser.error('--seed-count requires --seed-file or --reading-off')
    if args.seed_answer_scope is not None and not args.seed_answers:
        parser.error('--seed-answer-scope requires --seed-answers 9')
    if args.seed_answers and (not args.seed_file or args.reading_off
                             or args.seed_count==51 or args.outcome not in ('answered', 'answered-no-citations')):
        parser.error('--seed-answers 9 requires --seed-count 1 or 3, reading enabled, and an answered outcome')
    if args.seed_answer_scope == 'station' and not args.fictional_station:
        parser.error('Station answer seeding requires --fictional-station')
    answer_scope = (STATION_SCOPE if args.seed_answer_scope == 'station' else 'room') if args.seed_answers else None
    extra_metadata = dict(fictionalStationScope=STATION_SCOPE if args.fictional_station else None,
                          seedAnswerCount=args.seed_answers, seedAnswerScope=answer_scope)
    ordinary_mode = 'fictional-answer' if args.ordinary_answer else 'blocked'
    if args.metadata:
        print(json.dumps({'offline': True, 'bind': '127.0.0.1:0',
                          'ordinaryOfficer': ordinary_mode, 'cleanup': 'Ctrl-C',
                          'productionGatesChanged': False, 'availableOutcomes': OUTCOMES,
                          'availableModels': MODELS, 'outcome': args.outcome, 'model': args.model,
                          'readingEnabled': not args.reading_off, 'seeded': args.seed_file or args.reading_off,
                          'deletionOutcome': args.deletion_outcome, 'seedCount': args.seed_count,
                          'browserFault': args.browser_fault, 'managementFault': args.management_fault,
                          'uploadFault': args.upload_fault, 'clock': 'advancing-fictional', **extra_metadata}))
        return
    if args.trace_startup:
        faulthandler.dump_traceback_later(10)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    import server
    import officer_documents as documents
    from officer_policy import AUTOMATIC
    assert MODELS[1] == AUTOMATIC, 'The offline picker must use the production Auto sentinel'
    import officer_document_policy as policy
    import officer_document_transport as transport
    from attachments_http import handle_http
    from attachments_store import AttachmentStore
    from attachments_types import AttachmentError
    from officer_document_coordinator import DocumentCoordinator
    from officer_document_service import OfficerAttachmentService
    from test_officer_document_transport import FakeConnection, FakeResponse
    from test_officer_documents import response, wire

    stats = {'fakeProviderCalls': 0, 'ordinaryAskBlocked': 0, 'outboundBlocked': 0}
    if args.ordinary_answer:
        stats.update(ordinaryFakeCalls=0, ordinaryFileMarkerHits=0)
    if args.seed_answers:
        stats['seededAnswerCalls'] = 0
    stop = threading.Event()
    selected = [args.model]
    deletion_pending = [args.deletion_outcome == 'pending']
    faults = {'managementReadFaults': 0, 'interruptedUploads': 0, 'uploadBytesBeforeInterruption': 0}
    fault_lock = threading.Lock()

    def no_network(*_args, **_kwargs):
        stats['outboundBlocked'] += 1
        raise RuntimeError('Outbound connections are forbidden by the offline fixture')

    class FictionalConnection(FakeConnection):
        def request(self, method, path, *, body, headers):
            super().request(method, path, body=body, headers=headers)
            stats['fakeProviderCalls'] += 1
            if args.outcome in ('rejected', 'unknown'):
                self.response = FakeResponse(b'{"fictional":"offline fixed failure"}',
                                             status=400 if args.outcome == 'rejected' else 200)
                return
            blocks = json.loads(body)['messages'][0]['content']
            pdf = any(block['type'] == 'document' for block in blocks)
            image = any(block['type'] == 'image' for block in blocks)
            if args.outcome == 'answered-marker-only':
                if not image:
                    self.response = FakeResponse(b'{"fictional":"offline fixed failure"}', status=200)
                    return
                text = 'IMAGE_SOURCE: image-1'
            else:
                text = 'Fictional offline answer. No provider was contacted.'
                if image and args.outcome != 'answered-no-citations':
                    text += '\nIMAGE_SOURCE: image-1'
            self.response = FakeResponse(wire(response(text, citations=pdf and args.outcome != 'answered-no-citations')))

    class Handler(server.H):
        def send(self, code, body, ctype='text/plain; charset=utf-8', extra=()):
            self.close_connection = True
            return super().send(code, body, ctype, (*extra, ('Connection', 'close')))

        def handle(self):
            try:
                return super().handle()
            except OSError:
                if not stop.is_set():
                    raise

        def json(self, value, status=200):
            return self.send(status, json.dumps(value).encode(), 'application/json')

        def authorized(self):
            return self.host_ok() and self.headers.get('X-Live-Room-Key') == KEY

        def attachments_http(self):
            return handle_http(self, service, KEY, self.server.server_port)

        def ordinary_answer(self):
            if (not self.authorized() or self.headers.get_all('X-Live-Room-Key') != [KEY]
                    or len(self.headers.get_all('Host', [])) != 1 or len(self.headers.get_all('Origin', [])) > 1):
                return self.send(403, b'forbidden')
            invalid = {'error': 'Use a bounded fictional ordinary question.'}
            if self.path != '/ask' or self.headers.get_content_type() != 'application/json':
                return self.json(invalid, 400)
            data = self.body()
            if type(data) is not dict or set(data) - {'turns', 'context', 'request_id'}:
                return self.json(invalid, 400)
            if 'request_id' in data and not server.valid_uuid(data['request_id']):
                return self.json(invalid, 400)
            turns = data.get('turns')
            if type(turns) is not list or not 1 <= len(turns) <= server.OFFICER_TURNS:
                return self.json(invalid, 400)
            try:
                if 'context' in data:
                    server.officer_context(data['context'])
                for turn in turns:
                    if type(turn) is not dict or set(turn) - {'role', 'content', 'context', 'receipt', 'verification', 'warnings'}:
                        return self.json(invalid, 400)
                    role, content = turn.get('role'), turn.get('content')
                    maximum = server.ANSWER_CHARS if role == 'assistant' else server.OFFICER_TURN_CHARS
                    if role not in ('user', 'assistant') or type(content) is not str or not 0 < len(content) <= maximum:
                        return self.json(invalid, 400)
                    if 'context' in turn:
                        server.officer_context(turn['context'], historical=True)
            except ValueError:
                return self.json(invalid, 400)
            if turns[-1]['role'] != 'user' or not turns[-1]['content'].strip():
                return self.json(invalid, 400)
            with fault_lock:
                stats['ordinaryFakeCalls'] += 1
                if any(ORDINARY_FILE_MARKER in turn['content'] for turn in turns):
                    stats['ordinaryFileMarkerHits'] += 1
            return self.json({'text': ORDINARY_TEXT, 'actions': [], 'cites': []})

        def do_GET(self):
            parsed = urlsplit(self.path)
            route, query = parsed.path, parse_qs(parsed.query)
            if route.startswith('/api/attachments/'):
                return self.attachments_http()
            if not self.host_ok():
                return self.send(403, b'forbidden')
            if route == '/':
                body, csp = server.page_with_nonce()
                body = instrument_page(body, args.browser_fault)
                return self.send(200, body, 'text/html; charset=utf-8',
                                 (('Content-Security-Policy', csp), ('X-Frame-Options', 'DENY')))
            if route.startswith('/static/') and route[8:] in server.STATIC:
                name = route[8:]
                return self.send(200, (Path(server.HERE) / 'web/static' / name).read_bytes(), server.STATIC[name])
            if route.startswith('/app-icon/'):      # the real read-only route, over HOME's own icon folder (fictional icons only)
                if not self.authorized():
                    return self.send(403, b'forbidden')
                icon = None if parsed.query or parsed.fragment else server.app_icon(route[len('/app-icon/'):])
                return self.send(200, icon[0], icon[1], (('Content-Security-Policy', server.APP_ICON_CSP),)) if icon else self.send(404, b'not found')
            if route == '/check':
                return self.send(200 if query.get('k') == [KEY] else 403, b'')
            if route == '/events':
                if query.get('k') != [KEY]:
                    return self.send(403, b'forbidden')
                self.close_connection = True
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                hello = dict(kind='hello', agent='', snap=True, boot='fictional-browser', seq=-1,
                             pkey='fictional-preview', window=1200, names={}, watch={}, win=False,
                             prefixes=['c:', 'x:'], claudePermissionOwnershipCheckRequired=False)
                try:
                    self.wfile.write(('data: ' + json.dumps(hello) + '\n\n').encode())
                    if args.fictional_station:
                        station = dict(kind='agent', agent=STATION_AGENT, name=STATION_NAME, runtime='Codex',
                                       old=True, ts=0, snap=True, boot=hello['boot'], seq=-1)
                        self.wfile.write(('data: ' + json.dumps(station) + '\n\n').encode())
                    self.wfile.flush()
                    while not stop.wait(5):
                        self.wfile.write(b': offline fixture heartbeat\n\n')
                        self.wfile.flush()
                except OSError:
                    pass
                return
            if not self.authorized():
                return self.send(403, b'forbidden')
            if route == '/officer/model-selection':
                return self.json(dict(schemaVersion=1, selected_model=selected[0],
                                      selection_available=True, attachments_enabled=True, documents_enabled=not args.reading_off))
            if route == '/officer/models':
                return self.json(dict(available=True, complete=True, selection_available=True,
                                      selected_model=selected[0], selected_name='Auto' if selected[0] == '@automatic' else 'Haiku 5.5',
                                      models=[dict(id=documents.MODEL, display_name='Haiku 5.5'), dict(id='@automatic', display_name='Auto')]))
            if route == '/fixture/metadata':
                with fault_lock:
                    facts = dict(fixtureFaults=dict(faults), **stats)
                return self.json(dict(offline=True, ordinaryOfficer=ordinary_mode, outcome=args.outcome, model=selected[0], readingEnabled=not args.reading_off, deletionOutcome=args.deletion_outcome, deletionPending=deletion_pending[0], seedCount=args.seed_count, browserFault=args.browser_fault, managementFault=args.management_fault, uploadFault=args.upload_fault, clock='advancing-fictional', **extra_metadata, **facts))
            if route == '/agent-messages':
                return self.json(dict(requests=[], controls=[], available=False, reason='Offline fixture'))
            return self.json({'available': False, 'error': 'Unavailable in the offline fixture'}, 503)

        def do_POST(self):
            route = urlsplit(self.path).path
            if route.startswith('/api/attachments/'):
                return self.attachments_http()
            if not self.host_ok():
                return self.send(403, b'forbidden')
            if route == '/ask' and args.ordinary_answer:
                return self.ordinary_answer()
            data = self.body()
            if route == '/pair':
                return self.json({'offline': True, 'claim': CLAIM})
            if route == '/claim':
                return self.send(200 if data == {'code': CLAIM} else 403,
                                 KEY.encode() if data == {'code': CLAIM} else b'forbidden')
            if not self.authorized():
                return self.send(403, b'forbidden')
            if route == '/officer/model':
                if type(data) is not dict or set(data) != {'model'} or data['model'] not in MODELS:
                    return self.json({'error': 'Choose a fixed fictional model'}, 400)
                selected[0] = data['model']
                return self.json(dict(available=True, complete=True, selection_available=True,
                    selected_model=selected[0], selected_name='Auto' if selected[0]=='@automatic' else 'Haiku 5.5',
                    models=[dict(id=mid,display_name='Auto' if mid=='@automatic' else 'Haiku 5.5') for mid in MODELS]))
            if route == '/fixture/release-deletion':
                if data != {}:
                    return self.json({'error': 'Use the exact fictional release action'}, 400)
                deletion_pending[0] = False
                store.files.wake_cleanup()
                return self.json({'offline': True, 'released': True})
            if route == '/ask':
                stats['ordinaryAskBlocked'] += 1
                return self.json({'error': 'Ordinary Motet calls are blocked in this fixture'}, 503)
            if route in ('/presence', '/keep'):
                return self.send(200, b'ok')
            return self.json({'error': 'Action blocked in the offline fixture'}, 503)

        def do_PUT(self):
            if self.path.startswith('/api/attachments/'):
                return self.attachments_http()
            return self.send(503, b'Action blocked in the offline fixture')

        do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_PUT

    class FixtureServer(server.ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

        def __init__(self, *a, **kw):
            self.active, self.active_lock, self.closing = set(), threading.Lock(), False
            super().__init__(*a, **kw)

        def server_bind(self):
            # The fixture binds a numeric loopback address. HTTPServer's reverse
            # lookup serves no purpose here and can stall startup on hosted runners.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

        def process_request(self, request, address):
            with self.active_lock:
                if self.closing:
                    self.shutdown_request(request)
                    return
                self.active.add(request)
            try:
                super().process_request(request, address)
            except BaseException:
                with self.active_lock:
                    self.active.discard(request)
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                with self.active_lock:
                    self.active.discard(request)

        def server_close(self):
            # Unblock partial headers/bodies as well as the event stream, then
            # join every accepted handler before services or temp state close.
            with self.active_lock:
                self.closing = True
                for request in self.active:
                    try:
                        request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            super().server_close()
            with self.active_lock:
                if self.active:
                    raise RuntimeError('Offline fixture handlers did not join')

    with ExitStack() as stack:
        for name in ('connect', 'connect_ex'):
            stack.enter_context(patch.object(socket.socket, name, no_network))
        stack.enter_context(patch.object(socket, 'create_connection', no_network))
        stack.enter_context(patch.object(socket, 'getfqdn', no_network))
        stack.enter_context(patch.object(transport, '_tls_context',
                                       lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='watch-offline-browser-'))).resolve()
        os.chmod(root, 0o700)
        clock = AdvancingClock(policy.VALID_FROM + 60)
        store = AttachmentStore(str(root), enabled=True, owner_epoch=hashlib.sha256(KEY.encode()).hexdigest(),
                                clock=clock)
        stack.callback(store.close)
        fake = transport.DirectDocumentTransport(enabled=True, connect=lambda *_a, **_k: FictionalConnection(),
                                                 wall=clock)
        stack.callback(fake.close)
        coordinator = DocumentCoordinator(store, policy.HostAllowance(policy.EVIDENCE_ID, 'fictional-browser-budget', 'v1', '100'),
                                          selected_model=lambda: selected[0], credential=lambda: 'fictional-only',
                                          transport=fake, enabled=True)
        stack.callback(coordinator.close)
        refusals = {'budget_exhausted': ('claim', 'document_budget_exhausted'),
                    'context_too_large': ('prepare', 'serialized_document_input_bound'),
                    'reading_unavailable': ('prepare', 'document_policy_unavailable')}
        if args.outcome in refusals:
            method, code = refusals[args.outcome]
            def refuse(*_args, **_kwargs):
                raise AttachmentError(code, 'Fictional offline execution refusal.', 503)
            # These execution seams run after exact local admission; no paid claim occurs.
            stack.enter_context(patch.object(coordinator.ledger, method, refuse))
        scopes = frozenset(('room', STATION_SCOPE)) if args.fictional_station else frozenset(('room',))
        service = OfficerAttachmentService(store, coordinator=coordinator, known_scopes=lambda: scopes)
        # Resolve the current service at teardown; reading-off swaps it only
        # after the original coordinator has closed. Setup failures also clean up.
        stack.callback(lambda: service.close())
        if args.seed_file or args.reading_off:
            from test_attachments_foundation import fence, metadata
            wire_model = 'auto' if selected[0] == AUTOMATIC else selected[0]
            selections = {}
            for scope_id in sorted(scopes):
                cid = service.current_conversation(scope_id, create=True)['conversation']['id']
                selection = dict(destinationId='officer', scopeId=scope_id, conversationId=cid,
                                 modelId=wire_model, capabilityRevision=policy.PROFILE_REVISION)
                selections[scope_id] = selection
                count = args.seed_count if scope_id == 'room' else 1
                for index in range(count):
                    suffix = ('station-' if scope_id == STATION_SCOPE else '') + '%03d' % (index + 1)
                    draft = service.create_draft(selection, 'fictional-seed-draft-' + suffix)
                    raw = b'%PDF-1.4\nFictional offline retained source ' + suffix.encode() + b'.\n%%EOF'
                    if scope_id == STATION_SCOPE:
                        name = 'fictional-station-kept.pdf'
                    else:
                        name = 'fictional-kept.pdf' if count == 1 else 'fictional-kept-' + suffix + '.pdf'
                    upload = service.mutate('reserve', draft['id'], draft['capability'],
                        dict(fence(draft), **dict(metadata(raw), name=name, mediaType='application/pdf')),
                        command_key='fictional-seed-upload-' + suffix)
                    draft['revision'] = upload['draftRevision']
                    uploaded = store.receive_upload(draft['id'], draft['capability'], fence(draft),
                        upload['id'], upload['fingerprint'], io.BytesIO(raw))
                    draft['revision'] = uploaded['draftRevision']
                    intent = service.document_prepare(draft['id'], draft['capability'], fence(draft), 'fictional-seed-intent-' + suffix)
                    kept = service.document_send(intent['requestId'], intent['requestCapability'], draft['capability'],
                        dict(fence(draft), fingerprint=intent['fingerprint']), 'fictional-seed-keep-' + suffix)
                    if kept['state'] != 'retained_local' or stats['fakeProviderCalls']:
                        raise RuntimeError('The fictional file seed must remain local')
            if answer_scope is not None:
                selection = selections[answer_scope]
                answers = []
                for index in range(1, args.seed_answers + 1):
                    suffix = '%02d' % index
                    intent = service.document_ask(selection['conversationId'],
                        dict(selection, question='Fictional earlier file question ' + suffix + '.',
                             priorAnswerIds=answers[-8:], priorAnswerPolicy='recent_available_8',
                             keptFileIds=[f['id'] for f in service.history('officer',selection['scopeId'],selection['conversationId'])['items'][:1]]),
                        'fictional-seed-question-' + suffix, prepare_only=True)
                    answer = service.document_send_question(intent['requestId'], intent['requestCapability'],
                        {'fingerprint': intent['fingerprint']}, 'fictional-seed-answer-' + suffix)
                    if answer['state'] != 'answered' or not answer['answerId'] or stats['fakeProviderCalls'] != index:
                        raise RuntimeError('The fictional answer seed could not be verified')
                    answers.append(answer['answerId'])
                stats['seededAnswerCalls'] = len(answers)
        if args.outcome == 'generic_failed':
            def generic_failure(*_args, **_kwargs):
                raise AttachmentError('fictional_execution_refused', 'Fictional unclassified execution failure.', 503)
            # A retained-only explicit question has already consumed its public
            # intent when it reaches this seam. Blank seeding never enters it.
            stack.enter_context(patch.object(coordinator.ledger, 'prepare', generic_failure))
        if args.reading_off:
            from attachments_service import AttachmentService, UnqualifiedResolver
            coordinator.close()
            service = AttachmentService(store, resolver=UnqualifiedResolver())
        actual_management, actual_receive = service.management, store.receive_upload
        def management(*a, **kw):
            with fault_lock:
                refuse = args.management_fault == 'unreadable-once' and not faults['managementReadFaults']
                if refuse: faults['managementReadFaults'] += 1
            if refuse:
                raise AttachmentError('storage_unavailable', 'The fictional Files page could not be read.', 503)
            return actual_management(*a, **kw)
        def receive(*a, **kw):
            with fault_lock:
                interrupt = args.upload_fault == 'interrupted-once' and not faults['interruptedUploads']
                if interrupt: faults['interruptedUploads'] += 1
            if not interrupt:
                return actual_receive(*a, **kw)
            if kw or len(a) != 6:
                raise RuntimeError('Unexpected fixture upload receiver shape')
            reader = a[-1]
            class InterruptedReader:
                def read(self, size):
                    if faults['uploadBytesBeforeInterruption']:
                        raise OSError('Fictional partial upload interruption')
                    chunk = reader.read(min(size, 8))
                    faults['uploadBytesBeforeInterruption'] += len(chunk)
                    if not chunk:
                        raise OSError('Fictional empty upload interruption')
                    return chunk
            return actual_receive(*a[:-1], InterruptedReader())
        stack.enter_context(patch.object(service, 'management', management))
        stack.enter_context(patch.object(store, 'receive_upload', receive))
        actual_remove = store.files.remove
        actual_registry = store.files.cleanup_registry
        def fictional_remove(*a, **kw):
            if kw.get('durable_absence') and args.deletion_outcome == 'changed':
                return 'changed'
            if kw.get('durable_absence') and args.deletion_outcome == 'unknown':
                raise OSError('Fictional offline removal uncertainty')
            return actual_remove(*a, **kw)
        def fictional_registry(file_id=None):
            generation, state = actual_registry(file_id)
            if file_id is not None and deletion_pending[0]:
                return generation, 'busy'
            return generation, state
        stack.enter_context(patch.object(store.files, 'remove', fictional_remove))
        stack.enter_context(patch.object(store.files, 'cleanup_registry', fictional_registry))
        # Close owned operations before fault patches are restored. The earlier
        # callback remains a setup-failure fallback; service close is idempotent.
        stack.callback(lambda: service.close())
        httpd = FixtureServer(('127.0.0.1', 0), Handler)
        stack.callback(httpd.server_close)
        server.PORT = httpd.server_port
        if args.trace_startup:
            faulthandler.cancel_dump_traceback_later()
        print('OFFLINE fixture URL: http://127.0.0.1:%d/#k=%s' % (httpd.server_port, KEY), flush=True)
        print('Fictional owner key: ' + KEY + '\nStop with Ctrl-C; temporary state is removed.', flush=True)
        if args.browser_fault != 'none':
            print('EXTRA TEST INSTRUMENTATION: ' + args.browser_fault, flush=True)
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    # A final counter record certifies that owned handlers and services closed.
    print(json.dumps(stats), flush=True)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
