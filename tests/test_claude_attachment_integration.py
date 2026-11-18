"""Gated production owner HTTP and Claude mod routes on disposable local stores."""
from contextlib import ExitStack, closing
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout   # a separate class before Python 3.11
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import uuid

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import server
import peer_lifecycle
from attachments_native_namespace import NativeNamespaceFence
from attachments_owner_transport import CLAUDE_CLAIMS
from attachments_types import AttachmentError
from officer_messaging import MessagingStore

OWNER_KEY = 'fictional-owner-key'
PREFIX = '/api/attachments'
PDF = b'%PDF-1.4\nFictional orchard attachment.\n%%EOF'
QUESTION = 'Review the fictional orchard attachment.'
ANSWER = 'Fictional orchard answer.'


class QuietHandler(server.H):
    def log_message(self, *_):
        pass


def uid():
    return str(uuid.uuid4())


@unittest.skipUnless(os.name == 'posix' and os.path.exists('/usr/bin/git'), 'Requires local Git and private POSIX storage.')
class ClaudeAttachmentIntegration(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='claude-native-integration-'))
        self.home = Path(temp).resolve()
        self.project = self.home / 'orchard'
        self.project.mkdir(mode=0o700)
        self.stack.enter_context(patch.dict(os.environ, WATCH_DEMO='0'))
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
        subprocess.run(['/usr/bin/git', '-c', 'init.templateDir=', 'init', '-q', str(self.project)],
                       env=env, check=True, capture_output=True, timeout=8)
        (self.project / '.git/info').mkdir(exist_ok=True)
        (self.project / '.git/info/exclude').touch(mode=0o600)
        self.destination = 'c:' + uid()
        self.room = {self.destination: {'runtime': 'Claude', 'cwd': str(self.project), 'name': 'Fictional orchard'}}
        state = self.home / '.live-room'
        self.messaging = self.stack.enter_context(closing(MessagingStore(str(state), str(self.home / 'sessions'))))
        self.messaging.codex_available = False
        self.stack.enter_context(patch.multiple(server, HOME=str(self.home), KEY=OWNER_KEY, AGENTS=self.room,
            ATTACHMENTS_ENABLED=True, NATIVE_AGENT_ATTACHMENTS_ENABLED=True, OFFICER_DOCUMENTS_ENABLED=False,
            MESSAGING=self.messaging, CLAUDE_MANAGED=None, CLAUDE_MANAGED_ENABLED=False, CLAUDE_MANAGED_FAILED=False))
        self.attachments = self.stack.enter_context(closing(server.start_attachments()))
        self.stack.enter_context(patch.object(server, 'ATTACHMENTS', self.attachments))
        self.stack.callback(self.messaging.stop_native_attachment_runtime)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.httpd.daemon_threads = False
        port = self.httpd.server_port
        self.stack.enter_context(patch.object(server, 'PORT', port))
        actual_connect = socket.socket.connect

        def loopback_only(sock, address):
            self.assertEqual(address, ('127.0.0.1', port))
            return actual_connect(sock, address)

        self.stack.enter_context(patch.object(socket.socket, 'connect', loopback_only))
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='fictional-claude-http')
        self.thread.start()

        def close_http():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive())

        self.stack.callback(close_http)

    def http(self, method, path, data=None, *, status=200, token=None, owner=True, headers=None):
        headers = dict(headers or {})
        if owner:
            headers['X-Live-Room-Key'] = OWNER_KEY
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        if isinstance(data, dict):
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        with closing(HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=12)) as client:
            client.request(method, path, data, headers)
            reply = client.getresponse()
            payload = reply.read()
            self.assertEqual(reply.status, status, payload)
            if path.startswith(PREFIX):
                self.assertEqual(set(reply.getheader('Cache-Control').split(', ')), {'no-store'})
        return json.loads(payload)

    def attach(self, method, path, data=None, **opts):
        return self.http(method, PREFIX + path, data, **opts)

    def start(self):
        runtime = server.start_native_attachment_maintenance(self.messaging, self.attachments)
        self.assertIsNotNone(runtime)
        self.comp = self.messaging._native_composition
        self.assertIsNotNone(self.comp, self.messaging._native_composition_error)
        self.assertIsNotNone(self.comp.claude, self.messaging._native_composition_error)
        runtime.checkpoint()
        until = time.monotonic() + 4
        while runtime.state != 'ready' and time.monotonic() < until:
            time.sleep(.01)
        self.assertEqual(runtime.state, 'ready')
        return runtime

    def register(self, *, destination=None, lifecycle=2, client=None, seq=0, grant=None):
        destination = destination or self.destination
        if grant is None:
            setup = self.http('POST', '/claude-mod-setup', {})
            grant = self.http('POST', '/claude-mod/setup', {'code': setup['pair_code']}, owner=False)['grant']
        args = dict(native_id=destination[2:], project=str(self.project), client_id=client or uid(),
                    connection_seq=seq, lifecycle_protocol=lifecycle)
        result = self.http('POST', '/claude-mod/register', args, token=grant, owner=False)
        return {**args, **result, 'grant': grant}

    def selection(self, destination=None, **opts):
        return self.attach('GET', '/native/selection?' + urlencode(dict(destinationId=destination or self.destination)), **opts)

    def poll(self, session, accept=True):
        data = {k: session[k] for k in ('native_id', 'generation', 'client_id', 'project')}
        return self.http('POST', '/claude-mod/poll', dict(data, accept=accept), token=session['token'], owner=False)['claim']

    def event(self, session, claim, kind, *, status=200, **fields):
        data = {k: session[k] for k in ('native_id', 'generation', 'client_id')}
        data.update({k: claim[k] for k in ('request_id', 'attempt_id')}, kind=kind, **fields)
        return self.http('POST', '/claude-mod/event', data, token=session['token'], owner=False, status=status)

    @staticmethod
    def context(selection):
        return {k: selection['context'][k] for k in ('destinationId', 'scopeId')}

    def build(self, selection):
        context = self.context(selection)
        capability = self.attach('GET', '/capabilities?' + urlencode(dict(context, modelId='')))
        self.assertTrue(capability['available'], capability)
        chosen = {**context, 'modelId': None, 'conversationId': None, 'capabilityRevision': capability['capabilityRevision']}
        draft = self.attach('POST', '/drafts', chosen, status=201, headers={'Idempotency-Key': uid()})
        path = '/drafts/' + draft['id']
        caps = {'X-Attachment-Draft-Capability': draft['capability']}
        fence = dict(revision=draft['revision'], writerGeneration=draft['writerGeneration'], writerNonce=draft['writerNonce'])
        edited = self.attach('PATCH', path, dict(fence, text=QUESTION), headers=caps)
        fence['revision'] = edited['revision']
        meta = dict(name='fictional.pdf', mediaType='application/pdf', bytes=len(PDF), digest=hashlib.sha256(PDF).hexdigest())
        upload = self.attach('POST', path+'/upload-intents', {**fence, **meta}, status=201,
                             headers={**caps, 'Idempotency-Key': uid()})
        received = self.attach('PUT', path+'/uploads/'+upload['id'], PDF, headers={
            **caps, 'Content-Type': 'application/octet-stream',
            'X-Attachment-Writer-Nonce': fence['writerNonce'], 'X-Attachment-Writer-Generation': str(fence['writerGeneration']),
            'X-Attachment-Draft-Revision': str(upload['draftRevision']), 'X-Attachment-Upload-Fingerprint': upload['fingerprint'],
        })
        self.assertEqual(received['state'], 'ready')
        fence['revision'] = received['draftRevision']
        return draft, fence, caps

    def prepare(self, selection):
        draft, fence, caps = self.build(selection)
        key = uid()
        path = '/native/drafts/' + draft['id'] + '/send-intent'
        intent = self.attach('POST', path, fence, status=201, headers={**caps, 'Idempotency-Key': key})
        self.assertEqual(intent['observation']['request']['delivery'], 'not_started')
        self.attach('POST', path, fence, status=409, headers={**caps, 'Idempotency-Key': key})
        digest = hashlib.sha256(key.encode()).hexdigest()
        query = urlencode({**self.context(selection), 'draftId': draft['id'], 'commandDigest': digest})
        receipt = self.attach('GET', '/native/receipts/send_prepare?' + query)
        self.assertEqual(receipt['match'], {'draftId': draft['id'], 'requestId': intent['observation']['request']['id']})
        self.assertNotIn(intent['requestCapability'], json.dumps(receipt))
        return intent

    def send(self, intent, *, status=200):
        req = intent['observation']['request']
        return self.attach('POST', '/native/requests/' + req['id'] + '/send',
                           dict(revision=req['revision'], fingerprint=intent['fingerprint']), status=status,
                           headers={'X-Attachment-Request-Capability': intent['requestCapability'], 'Idempotency-Key': uid()})

    def status(self, selection, intent, **opts):
        request = intent['observation']['request']
        return self.attach('GET', '/native/requests/' + request['id'] + '?' + urlencode(self.context(selection)), **opts)

    def test_real_http_one_claim_terminal_only_and_conflict_withdraws_current_reply(self):
        self.start()
        self.assertIsNone(self.selection()['context'])
        session = self.register()
        selection = self.selection()
        self.assertEqual(selection['features'], dict(drafts=True, send=True, files=False, deletion=False))
        self.assertTrue(selection['completionOnly'])
        intent = self.prepare(selection)
        self.assertIsNone(self.poll(session, False))
        sending = self.send(intent)['observation']['request']
        self.assertFalse(sending['accepted'])
        self.assertEqual(sending['execution'], 'unobserved')
        self.send(intent, status=409)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [item for item in pool.map(lambda _: self.poll(session), range(4)) if item]
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(set(claim), {'native_id','request_id','attempt_id','generation','expires_at','envelope','lifecycle_protocol'})
        self.assertIn('Attachment relay version: 3', claim['envelope'])
        self.assertIn(QUESTION, claim['envelope'])
        self.assertNotIn(str(self.project), claim['envelope'])
        saved = list((self.project / '.watch-attachments').glob('*/*.pdf'))
        self.assertEqual([path.read_bytes() for path in saved], [PDF])
        turn = uid()
        self.event(session, claim, 'submitted')
        self.event(session, claim, 'started', turn_id=turn)
        current = self.status(selection, intent)['observation']['request']
        self.assertEqual((current['delivery'], current['accepted'], current['execution']), ('unknown', False, 'unobserved'))
        self.assertEqual(self.messaging.db.execute('SELECT COUNT(*) FROM peer_execution_events WHERE request_id=?',
                                                  (claim['request_id'],)).fetchone(), (0,))
        completed = dict(turn_id=turn, outcome='answered', event_id=uid(), occurred_at=4000,
                         answer='WATCH_REPLY ' + claim['request_id'] + '\n' + ANSWER)
        self.event(session, claim, 'completed', **completed)
        terminal = self.status(selection, intent)['observation']['request']
        self.assertEqual((terminal['delivery'], terminal['accepted'], terminal['execution']), ('completed', True, 'ended'))
        self.assertEqual(terminal['reply'], {'state': 'available', 'text': ANSWER})
        self.event(session, claim, 'completed', **completed)
        replay = self.status(selection, intent)['observation']['request']
        self.assertEqual(replay, terminal)
        changed = dict(completed, event_id=uid())
        ack = self.event(session, claim, 'completed', **changed)
        self.assertEqual((ack['state'], ack['turn_id']), ('unknown', ''))
        conflict = self.status(selection, intent)['observation']['request']
        self.assertEqual((conflict['delivery'], conflict['execution']), ('completed', 'ended'))
        self.assertEqual(conflict['reply'], {'state': 'withheld', 'text': None})
        self.assertGreater(conflict['revision'], terminal['revision'])
        self.assertNotIn(ANSWER, json.dumps(self.attach('GET', '/native/requests?' + urlencode(self.context(selection)))))
        self.assertIsNone(self.poll(session))

    def test_off_gates_inert_and_retained_busy_orphan_checks_are_target_specific(self):
        state = self.home / '.live-room'
        fence = NativeNamespaceFence(state)
        self.assertFalse(fence.detected())
        with patch.object(server, 'NATIVE_AGENT_ATTACHMENTS_ENABLED', False):
            self.assertIsNone(server.start_native_attachment_maintenance(self.messaging, self.attachments))
            self.selection(status=503)
        self.assertFalse(fence.detected())
        self.assertIsNone(self.messaging.mods.attachment_mount)
        self.assertFalse(fence.host.exists())
        self.start()
        session = self.register()
        other = 'c:' + uid()
        self.room[other] = {**self.room[self.destination]}
        neighboring_session = self.register(destination=other, grant=session['grant'])
        selection = self.selection()
        intent = self.prepare(selection)
        self.send(intent)
        claim = self.poll(session)
        self.assertIsNotNone(claim)
        # Stock heartbeat writes invalidate the old directory epoch. Run its
        # normal bounded checkpoint before creating the plain control message.
        self.comp.owner_runtime.refresh_directory()
        pending_agent_message = uid()
        self.http('POST', '/agent-message', dict(agent=self.destination, question='Fictional later instruction.', request_id=pending_agent_message))
        unrelated = uid()
        self.http('POST', '/agent-message', dict(agent=other, question='Fictional neighboring instruction.', request_id=unrelated))
        self.messaging.stop_native_attachment_runtime()
        self.messaging.mods.attachment_mount = None
        with patch.object(server, 'NATIVE_AGENT_ATTACHMENTS_ENABLED', False):
            self.selection(status=503)
            self.assertIsNone(self.poll(session))
            self.assertEqual(self.poll(neighboring_session)['request_id'], unrelated)
            self.event(session, claim, 'unknown', status=409)
            with self.messaging.lock, self.messaging.db:
                self.messaging.db.execute(f'DELETE FROM main.{CLAUDE_CLAIMS} WHERE owner_request_id=?', (claim['request_id'],))
            self.assertIsNone(self.poll(session))
        self.assertEqual(self.messaging.db.execute('SELECT COUNT(*) FROM peer_execution_events WHERE request_id=?',
                                                  (claim['request_id'],)).fetchone(), (0,))

    def test_legacy_unknown_namespace_never_adopted(self):
        state = self.home / '.live-room'
        legacy = state / 'managed-claude'
        legacy.mkdir(mode=0o700)
        server.start_native_attachment_maintenance(self.messaging, self.attachments)
        self.assertIsNone(self.messaging._native_composition)
        self.assertEqual(self.messaging._native_composition_error, 'setup_unavailable')
        self.assertFalse((state / 'native-attachment-namespace.expected').exists())
        self.assertFalse((state / 'native-attachment-namespace.authority').exists())
        self.assertFalse((state / 'managed-claude.lock').exists())

    def test_initialized_namespace_excludes_the_exact_managed_uuid(self):
        from native_managed_namespace_fixture import ManagedFixture, fixture_files
        self.start()
        state = self.home / '.live-room'
        fixture_files(state)
        managed = ManagedFixture(state, None)
        self.addCleanup(managed.manager.close)
        run_id = managed.manager.reserve(managed.folder, uid())
        with managed.manager._registry() as db:
            native_id = db.execute('SELECT native FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        managed_target = 'c:' + native_id
        self.room[managed_target] = {**self.room[self.destination]}
        first = self.register()
        self.register(destination=managed_target, grant=first['grant'])
        selected = self.selection()
        self.assertTrue(selected['features']['send'])
        self.assertIsNone(self.selection(managed_target)['context'])
        self.assertEqual(managed.calls, [])

    def seed_uncertain_peer(self, session):
        request_id, created = uid(), time.time() - 1800
        row = dict(request_id=request_id, agent=self.destination, created=created, launched=True,
                   state='unknown', origin='peer', runtime='claude', transport='claude_mod', bridge_version=2,
                   mod_enrollment=session['enrollment_id'], mod_attempt_id=uid(), mod_generation=session['generation'],
                   mod_turn='fictional-turn', lifecycle_protocol=2, mod_project=str(self.project),
                   target_project=str(self.project))
        db = self.messaging.db
        with self.messaging.lock, db:
            db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                       (uid(), self.destination, '{}', 'fictional-digest', request_id, 'fictional-credential',
                        created, created, 'Fictional earlier request'))
            db.execute('INSERT INTO requests VALUES (?,?)', (request_id, json.dumps(row)))
            peer_lifecycle.reconcile(db, row)
            self.assertEqual(peer_lifecycle.get(db, request_id)['execution'], 'uncertain')
        return request_id

    def retire_fixture_peer(self, request_id):
        with self.messaging.lock, self.messaging.db:
            peer_lifecycle.retire(self.messaging.db, request_id, peer_lifecycle.RETIRE_REASON,
                                  expected_agent=self.destination)

    def test_uncertain_peer_refuses_before_copy_and_cannot_replay_after_recovery(self):
        self.start()
        session = self.register()
        selection = self.selection()
        draft, fence, caps = self.build(selection)
        prepare_path = '/native/drafts/' + draft['id'] + '/send-intent'

        def prepare():
            return self.attach('POST', prepare_path, fence, status=201,
                               headers={**caps, 'Idempotency-Key': uid()})

        first = prepare()
        earlier = self.seed_uncertain_peer(session)
        self.send({**first, 'requestCapability': '0' * 64}, status=403)
        self.assertIsNone(self.status(selection, first)['observation']['request']['reason'])
        refusal = self.send(first)['observation']
        self.assertEqual(refusal['draftAdmission'], 'unadmitted')
        self.assertEqual(refusal['request']['delivery'], 'not_started')
        self.assertEqual(refusal['request']['reason'], 'earlier_delivery_unresolved')
        self.assertEqual([f['savedCopyState'] for f in refusal['request']['files']], ['not_saved'])
        self.assertEqual(self.status(selection, first)['observation'], refusal)
        self.assertFalse((self.project / '.watch-attachments').exists())
        self.assertIsNone(self.poll(session))
        self.assertIsNone(self.attachments.store.db.execute(
            'SELECT authorized_at FROM attachment_requests').fetchone()[0])
        self.assertEqual(self.messaging.db.execute('SELECT COUNT(*) FROM owner_attachment_links').fetchone()[0], 0)
        saved = self.attach('GET', '/drafts/' + draft['id'], headers=caps)
        self.assertEqual((saved['text'], saved['files'][0]['state']), (QUESTION, 'ready'))

        self.retire_fixture_peer(earlier)
        self.send(first, status=403)
        self.assertEqual(self.status(selection, first)['observation'], refusal)
        self.assertFalse((self.project / '.watch-attachments').exists())

        second = prepare()
        self.assertEqual(self.send(second)['observation']['draftAdmission'], 'matched')
        self.assertIsNotNone(self.poll(session))
        self.assertIsNone(self.poll(session))
        self.assertEqual(len(list(self.project.glob('.watch-attachments/*/*.pdf'))), 1)

    def test_pruned_peer_becoming_uncertain_after_admission_still_blocks_the_claim(self):
        runtime = self.start()
        session = self.register()
        selection = self.selection()
        intent = self.prepare(selection)
        result = self.send(intent)['observation']
        self.assertEqual(result['draftAdmission'], 'matched')

        earlier = self.seed_uncertain_peer(session)
        # Retention can remove a message body while its execution still occupies the recipient.
        with self.messaging.lock, self.messaging.db:
            self.messaging.db.execute('DELETE FROM requests WHERE id=?', (earlier,))
            self.assertEqual(peer_lifecycle.get(self.messaging.db, earlier)['execution'], 'uncertain')
        runtime.refresh_directory()
        self.assertIsNone(self.poll(session))
        status = self.status(selection, intent)['observation']['request']
        self.assertFalse(status['accepted'])
        self.assertEqual(status['delivery'], 'unknown')
        self.assertEqual(len(list(self.project.glob('.watch-attachments/*/*.pdf'))), 1)

    def test_ordinary_pending_and_unknown_claims_take_precedence(self):
        runtime = self.start()
        session = self.register()
        selected = self.selection()
        intent = self.prepare(selected)
        self.send(intent)
        message_id = uid()
        runtime.refresh_directory()
        self.http('POST', '/agent-message', dict(agent=self.destination, question='Fictional ordinary work.', request_id=message_id))
        ordinary = self.poll(session)
        self.assertEqual(ordinary['request_id'], message_id)
        self.event(session, ordinary, 'unknown')
        self.assertIsNone(self.poll(session))
        self.event(session, ordinary, 'completed', turn_id=uid(), outcome='unusable', answer='',
                   event_id=uid(), occurred_at=4000)
        protected = self.poll(session)
        self.assertIsNotNone(protected)
        self.assertNotEqual(protected['request_id'], message_id)
        self.assertIn('Attachment relay version: 3', protected['envelope'])

    def test_busy_directory_worker_is_not_waited_on_and_preserves_intent(self):
        runtime = self.start()
        self.register()
        selection = self.selection()
        intent = self.prepare(selection)
        # Reproduce the worker already holding its step gate. The request
        # cannot wait on that worker while holding the shared owner lock.
        with runtime._running_step:
            with self.messaging.lock:
                runtime.ledger.directory.invalidate()
            began = time.monotonic()
            self.send(intent, status=503)
            self.assertLess(time.monotonic() - began, 2)
            self.assertIsNone(self.attachments.store.db.execute(
                'SELECT authorized_at FROM attachment_requests').fetchone()[0])
        self.assertEqual(list(self.project.glob('.watch-attachments/*')), [])
        self.assertEqual(self.messaging.db.execute('SELECT count(*) FROM owner_attachment_links').fetchone()[0], 0)

    def test_stale_connection_refuses_terminal_then_exact_reconnect_can_report(self):
        self.start()
        session = self.register()
        selection = self.selection()
        intent = self.prepare(selection)
        self.send(intent)
        claim = self.poll(session)
        self.assertIsNotNone(claim)
        with self.messaging.lock, self.messaging.db:
            self.messaging.db.execute('UPDATE claude_mod_enrollments SET last_seen=0 WHERE id=?', (session['enrollment_id'],))
        completed = dict(turn_id=uid(), outcome='answered', event_id=uid(), occurred_at=4000,
                         answer='WATCH_REPLY ' + claim['request_id'] + '\n' + ANSWER)
        self.event(session, claim, 'completed', status=409, **completed)
        self.assertFalse(self.status(selection, intent)['observation']['request']['accepted'])
        current = self.register(grant=session['grant'], client=session['client_id'], seq=1)
        self.assertEqual(current['enrollment_id'], session['enrollment_id'])
        self.assertNotEqual(current['generation'], session['generation'])
        self.assertIsNone(self.poll(current))
        self.event(current, claim, 'completed', **completed)
        self.assertEqual(self.status(selection, intent)['observation']['request']['reply'], {'state':'available', 'text':ANSWER})

    def test_real_status_and_history_are_read_only_while_terminal_mirror_lags(self):
        runtime = self.start()
        session = self.register()
        selection = self.selection()
        intent = self.prepare(selection)
        self.send(intent)
        claim = self.poll(session)
        self.assertIsNotNone(claim)
        turn = uid()
        event = dict(turn_id=turn, outcome='answered', event_id=uid(), occurred_at=4000,
                     answer='WATCH_REPLY ' + claim['request_id'] + '\n' + ANSWER)
        with patch.object(runtime, 'native_checkpoint', None), patch.object(self.comp.mirror, 'reconcile',
                 side_effect=AttachmentError('mirror_unavailable', 'Fictional mirror outage.', 503)) as failed:
            self.event(session, claim, 'completed', **event)
            self.assertEqual(failed.call_count, 1)
            before = (self.messaging.db.total_changes, self.attachments.store.db.total_changes)
            self.assertNotIn(ANSWER, json.dumps(self.status(selection, intent)))
            self.assertNotIn(ANSWER, json.dumps(self.attach('GET', '/native/requests?' + urlencode(self.context(selection)))))
            self.assertEqual((self.messaging.db.total_changes, self.attachments.store.db.total_changes), before)
            self.assertEqual(failed.call_count, 1)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            current = self.status(selection, intent)['observation']['request']
            if current['reply']['state'] == 'available':
                break
            time.sleep(.05)
        self.assertEqual(current['reply'], {'state': 'available', 'text': ANSWER})
        self.assertIsNone(self.poll(session))

    def assert_activity_drains(self, obj, name, work):
        entered, release, closing_activity = threading.Event(), threading.Event(), threading.Event()
        original = getattr(obj, name)
        close_reads = self.comp._reads.close

        def pause(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('The paused fictional activity did not get released.')
            return original(*args, **kwargs)

        def close(*args, **kwargs):
            closing_activity.set()
            return close_reads(*args, **kwargs)

        with (patch.object(obj, name, pause), patch.object(self.comp._reads, 'close', close),
              ThreadPoolExecutor(max_workers=2) as pool):
            active = pool.submit(work)
            try:
                self.assertTrue(entered.wait(3))
                shutdown = pool.submit(self.messaging.stop_native_attachment_runtime)
                self.assertTrue(closing_activity.wait(3))
                with self.assertRaises(FutureTimeout):
                    shutdown.result(.2)
                self.assertFalse(self.comp.claude.resources._closed)
            finally:
                release.set()
            result = active.result(4)
            shutdown.result(4)
        self.assertTrue(self.comp.claude.resources._closed)
        return result

    def test_shutdown_drains_verification_lease_before_resources_close(self):
        self.start()
        session = self.register()
        self.send(self.prepare(self.selection()))
        adapter = self.comp.claude.adapter
        self.assertIsNone(self.assert_activity_drains(adapter, '_verify_files', lambda: self.poll(session)))
        self.assertIsNone(self.poll(session))
        self.assertEqual(self.messaging.db.execute(f'SELECT COUNT(*) FROM main.{CLAUDE_CLAIMS}').fetchone()[0], 0)

    def test_shutdown_drains_duplicate_ack_before_attachment_reads_close(self):
        runtime = self.start()
        session = self.register()
        self.send(self.prepare(self.selection()))
        claim = self.poll(session)
        self.assertIsNotNone(claim)
        completion = dict(turn_id=uid(), outcome='answered', event_id=uid(), occurred_at=4000,
                          answer='WATCH_REPLY ' + claim['request_id'] + '\n' + ANSWER)
        first = self.event(session, claim, 'completed', **completion)
        with patch.object(runtime, 'native_checkpoint', None):
            before = (self.messaging.db.total_changes, self.attachments.store.db.total_changes)
            replay = self.assert_activity_drains(self.comp.claude.adapter, '_attachment_record',
                                                 lambda: self.event(session, claim, 'completed', **completion))
            self.assertEqual(before, (self.messaging.db.total_changes, self.attachments.store.db.total_changes))
        self.assertEqual(replay, first)
        self.event(session, claim, 'completed', status=503, **completion)

    def test_shutdown_drains_the_on_only_maintenance_checkpoint(self):
        runtime = self.start()
        checkpoint = runtime.native_checkpoint
        with patch.object(runtime, 'native_checkpoint', None):
            self.assert_activity_drains(self.comp.claude.adapter, 'reconcile_pending', checkpoint)
        before = (self.messaging.db.total_changes, self.attachments.store.db.total_changes)
        checkpoint()
        self.assertEqual(before, (self.messaging.db.total_changes, self.attachments.store.db.total_changes))

    def test_prepare_rejects_an_unrecognized_transaction_callback(self):
        self.start()

        class Unbound:
            store = self.attachments.store

            def record(self, *_):
                self.fail('An arbitrary prepare callback ran.')

        with self.assertRaises(AttachmentError) as refused:
            self.comp.mirror.requests.prepare_request('unused', None, None, None, None, None, _prepare_index=Unbound())
        self.assertEqual(refused.exception.code, 'profile_unavailable')
        self.assertEqual(self.attachments.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0], 0)


def browser_client_fixture():
    """Real route authorities. Stdin provides the fictional official-mod boundary."""
    import sys
    fixture = ClaudeAttachmentIntegration()
    try:
        fixture.setUp()
        fixture.start()
        session = fixture.register()
        print(json.dumps({'origin': f'http://127.0.0.1:{fixture.httpd.server_port}',
                          'destinationId': fixture.destination}), flush=True)
        claim, first, blocked = None, None, None
        for line in sys.stdin:
            mode = json.loads(line)
            if mode == 'block' and blocked is None:
                blocked = fixture.seed_uncertain_peer(session)
                value = {'blocked': True}
            elif mode == 'retire' and blocked is not None:
                fixture.retire_fixture_peer(blocked)
                blocked = None
                value = {'retired': True}
            elif mode == 'claim':
                if claim is not None:
                    raise ValueError('This fictional fixture already granted its attempt.')
                claim = fixture.poll(session)
                if claim is None:
                    raise ValueError('The real gated route produced no claim.')
                value = {'claimed': True}
            elif mode in ('complete', 'conflict') and claim is not None:
                if mode == 'complete':
                    if first is not None:
                        raise ValueError('The fictional completion already ran.')
                    first = dict(turn_id=uid(), outcome='answered', event_id=uid(), occurred_at=4000,
                                 answer='WATCH_REPLY ' + claim['request_id'] + '\n' + ANSWER)
                    args = first
                elif first is not None:
                    args = {**first, 'event_id': uid()}
                else:
                    raise ValueError('The fictional completion has not run.')
                receipt = fixture.event(session, claim, 'completed', **args)
                value = {'state': receipt['state']}
            elif mode == 'stop':
                return
            else:
                raise ValueError('Invalid fictional fixture command.')
            print(json.dumps(value), flush=True)
    finally:
        if not fixture.doCleanups():
            raise RuntimeError('The fictional fixture could not be closed.')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--browser-client-fixture']:
        browser_client_fixture()
    elif not sys.argv[1:]:
        unittest.main()
    else:
        raise SystemExit('Unknown fictional fixture option.')
