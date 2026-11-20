"""Owner administration against fictional disposable state and local HTTP only."""
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging
import peer_admin as admin
import peer_bridge
import peer_lifecycle as life
import server


def uid():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = officer_messaging.MessagingStore(os.path.join(self.tmp.name, 'state'), self.tmp.name)
        self.addCleanup(self.store.close)
        self.tid, self.agent, self.cid = uid(), 'x:' + uid(), uid()
        self.created = 100.
        clock_patch = patch.object(admin, 'time', wraps=time)
        clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.clock = clock.time
        self.clock.return_value = self.created + 2
        self.snapshot = dict(agent_id=self.agent, provider='codex', project_id='b'*64, identity_revision='c'*64)
        self.row = dict(request_id=self.tid, agent=self.agent, created=self.created, launched=True,
                        state='unknown', origin='peer', runtime='codex', bridge_version=2,
                        identity_revision=self.snapshot['identity_revision'],
                        reply='', candidate_reply='', final='', bound_turn='fictional-turn')
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                             (self.cid, self.agent, json.dumps(self.snapshot), 'a'*64, self.tid, uid(),
                              self.created, 101., 'PRIVATE PROMPT'))
        self.store._save(self.row)

    def request(self):
        return dict(transport_id=self.tid, agent=self.agent, confirm=admin.CONFIRM)

    def test_inspect_is_read_only_metadata_and_retirement_is_idempotent(self):
        before = self.store.db.total_changes
        view = admin.inspect(self.store, {'transport_id': self.tid})
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(view, dict(transport_id=self.tid, agent=self.agent, execution='uncertain',
                                    capture='pending', retained_payload=True, eligible=True))
        ledger = self.store.db.execute('SELECT * FROM shared_bridge_requests').fetchall()
        with patch.object(self.store, '_send', side_effect=AssertionError('native dispatch')), patch.object(self.store, '_poll', side_effect=AssertionError('native read')):
            receipt = admin.retire(self.store, self.request())
        self.assertEqual((receipt['status'], receipt['provider_outcome']), ('retired', 'unknown'))
        original = life.get(self.store.db, self.tid)
        self.assertEqual(original['evidence']['owner_retirement']['source'], 'online_owner_decision')
        self.assertEqual(admin.retire(self.store, self.request())['status'], 'already_retired')
        self.assertEqual(life.get(self.store.db, self.tid), original)
        self.assertEqual(ledger, self.store.db.execute('SELECT * FROM shared_bridge_requests').fetchall())
        self.assertEqual(self.store.shared.result(self.cid)['state'], 'unknown')
        self.assertEqual(life.blockers(self.store.db, self.agent), [])
        self.assertNotIn('settled', json.loads(self.store.db.execute('SELECT data FROM requests').fetchone()[0]))

    def test_public_id_inspect_and_retire_resolve_one_exact_shared_mapping(self):
        db = self.store.db
        public = {'request_id': self.cid}
        transport = admin.inspect(self.store, {'transport_id': self.tid})
        before = db.total_changes
        queries = []
        db.set_trace_callback(queries.append)
        try:
            self.assertEqual(admin.inspect(self.store, public), dict(transport, **public))
            self.assertEqual(db.total_changes, before)
            self.assertEqual(sum('FROM shared_bridge_requests WHERE client_id=' in query for query in queries), 1)
            queries.clear()
            result = admin.retire(self.store, dict(public, agent=self.agent, confirm=admin.CONFIRM))
            self.assertEqual(sum('FROM shared_bridge_requests WHERE client_id=' in query for query in queries), 1)
        finally:
            db.set_trace_callback(None)
        self.assertEqual((result['request_id'], result['transport_id'], result['provider_outcome']),
                         (self.cid, self.tid, 'unknown'))
        self.assertEqual(admin.retire(self.store, dict(public, agent=self.agent, confirm=admin.CONFIRM))['status'],
                         'already_retired')

    def test_public_id_rejects_wrong_scope_noncanonical_and_ambiguous_selectors(self):
        db = self.store.db
        legacy_id, legacy_transport = uid(), uid()
        db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                   (uid(), legacy_id, uid(), 'a'*64, legacy_transport, self.created, 101.))
        db.commit()
        for public, status in ((self.tid, 404), (legacy_id, 404), (legacy_transport, 404), (uid(), 404),
                               (self.cid.upper(), 400), (self.cid[:8], 400), ([], 400)):
            with self.subTest(public=public), self.assertRaises(peer_bridge.PeerError) as error:
                admin.inspect(self.store, {'request_id': public})
            self.assertEqual(error.exception.status, status)
        selectors = {'request_id': self.cid, 'transport_id': self.tid}
        with self.assertRaises(peer_bridge.PeerError) as error:
            admin.inspect(self.store, selectors)
        self.assertEqual(error.exception.status, 400)
        with self.assertRaises(peer_bridge.PeerError) as error:
            admin.retire(self.store, dict(selectors, agent=self.agent, confirm=admin.CONFIRM))
        self.assertEqual(error.exception.status, 400)

        public_request = {'request_id': self.cid}
        apply = dict(public_request, agent=self.agent, confirm=admin.CONFIRM)
        self.assertTrue(admin.inspect(self.store, public_request)['eligible'])
        db.execute('UPDATE peer_requests SET transport_id=? WHERE client_id=?', (self.tid, legacy_id))
        db.commit()
        for operation, body in ((admin.inspect, public_request), (admin.retire, apply)):
            with self.assertRaises(peer_bridge.PeerError) as error:
                operation(self.store, body)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(life.get(db, self.tid)['execution'], 'uncertain')

    def test_public_id_is_available_after_pruning_but_refuses_an_inbox_collision(self):
        db = self.store.db
        public = {'request_id': self.cid}
        apply = dict(public, agent=self.agent, confirm=admin.CONFIRM)
        db.execute('DELETE FROM requests WHERE id=?', (self.tid,))
        db.execute('INSERT INTO peer_inbox_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (self.cid, uid(), 'c'*64, 'a'*64, uid(), 100000, 200000, 'queued', None, None, None))
        db.commit()
        for operation, body in ((admin.inspect, public), (admin.retire, apply)):
            with self.assertRaises(peer_bridge.PeerError) as error:
                operation(self.store, body)
            self.assertEqual(error.exception.status, 409)
        db.execute('DELETE FROM peer_inbox_requests WHERE request_id=?', (self.cid,))
        db.commit()
        view = admin.inspect(self.store, public)
        self.assertTrue(view['eligible'])
        self.assertFalse(view['retained_payload'])
        self.assertEqual(admin.retire(self.store, apply)['transport_id'], self.tid)
        self.assertIsNone(db.execute('SELECT data FROM requests WHERE id=?', (self.tid,)).fetchone())

    def test_public_id_rechecks_target_snapshot_namespace_and_expected_agent(self):
        db = self.store.db
        public = {'request_id': self.cid}
        apply = dict(public, agent=self.agent, confirm=admin.CONFIRM)
        self.assertTrue(admin.inspect(self.store, public)['eligible'])
        with self.assertRaises(peer_bridge.PeerError) as error:
            admin.retire(self.store, dict(apply, agent='x:'+uid()))
        self.assertEqual(error.exception.status, 409)
        for update in (dict(agent_id='x:'+uid()), dict(provider='claude'), dict(identity_revision='bad'),
                       dict(identity_revision='d'*64), dict(project_id='bad')):
            db.execute('UPDATE shared_bridge_requests SET target_snapshot=? WHERE client_id=?',
                       (json.dumps(dict(self.snapshot, **update)), self.cid))
            db.commit()
            for operation, request in ((admin.inspect, public), (admin.retire, apply)):
                with self.subTest(update=update), self.assertRaises(peer_bridge.PeerError) as error:
                    operation(self.store, request)
                self.assertEqual(error.exception.status, 409)
        db.execute('UPDATE shared_bridge_requests SET target_snapshot=? WHERE client_id=?',
                   (json.dumps(self.snapshot), self.cid))
        db.execute('UPDATE requests SET data=? WHERE id=?',
                   (json.dumps(dict(self.row, attachment_owner_version=None)), self.tid))
        db.commit()
        for operation, request in ((admin.inspect, public), (admin.retire, apply)):
            with self.assertRaises(peer_bridge.PeerError) as error:
                operation(self.store, request)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(life.get(db, self.tid)['execution'], 'uncertain')

    def test_public_selector_cannot_inspect_or_retire_an_owner_desk_id(self):
        owner_id = uid()
        owner = dict(request_id=owner_id, agent=self.agent, origin='owner', runtime='codex',
                     state='awaiting_reply', created=self.created, launched=True, bound_turn='owner-turn')
        self.store._save(owner)
        db = self.store.db
        saved = db.execute('SELECT data FROM requests WHERE id=?', (owner_id,)).fetchone()
        self.assertIsNotNone(saved)
        public = {'request_id': owner_id}
        apply = dict(public, agent=self.agent, confirm=admin.CONFIRM)
        for operation, body in ((admin.inspect, public), (admin.retire, apply)):
            with self.assertRaises(peer_bridge.PeerError) as error:
                operation(self.store, body)
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (owner_id,)).fetchone(), saved)
        self.assertIsNone(life.get(db, owner_id))

    def test_public_selector_cannot_retire_a_healthy_attempt_at_sixty_seconds(self):
        self.store._save(dict(self.row, state='awaiting_reply'))
        self.clock.return_value = self.created + 60
        db = self.store.db
        before = life.get(db, self.tid)
        saved = db.execute('SELECT data FROM requests WHERE id=?', (self.tid,)).fetchone()
        public = {'request_id': self.cid}
        view = admin.inspect(self.store, public)
        self.assertEqual((view['transport_id'], view['execution'], view['eligible']), (self.tid, 'running', False))
        with self.assertRaises(peer_bridge.PeerError) as error:
            admin.retire(self.store, dict(public, agent=self.agent, confirm=admin.CONFIRM))
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(life.get(db, self.tid), before)
        self.assertEqual(db.execute('SELECT data FROM requests WHERE id=?', (self.tid,)).fetchone(), saved)

    def test_public_id_retirement_rechecks_completion_after_inspection(self):
        public = {'request_id': self.cid}
        self.assertTrue(admin.inspect(self.store, public)['eligible'])
        self.store._save(dict(self.row, state='answered', settled=True, reply='Fictional answer'))
        self.assertFalse(admin.inspect(self.store, public)['eligible'])
        with self.assertRaises(peer_bridge.PeerError) as error:
            admin.retire(self.store, dict(public, agent=self.agent, confirm=admin.CONFIRM))
        self.assertEqual(error.exception.status, 409)
        record = life.get(self.store.db, self.tid)
        self.assertEqual((record['execution'], record['capture']), ('completed', 'captured'))

    def test_pruned_payload_remains_absent_and_wrong_identity_rejects_even_after_retirement(self):
        self.store.db.execute('DELETE FROM requests'); self.store.db.commit()
        self.assertFalse(admin.inspect(self.store, {'transport_id': self.tid})['retained_payload'])
        admin.retire(self.store, self.request())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, dict(self.request(), agent='x:' + uid()))
        self.assertEqual(self.store.shared.result(self.cid)['reply'], '')

    def test_reconcile_current_evidence_before_eligibility_and_reject_active_states(self):
        for state in ('sending', 'queued', 'awaiting_reply', 'answered'):
            with self.subTest(state=state):
                row = dict(self.row, state=state, reply='PRIVATE ANSWER' if state == 'answered' else '')
                if state in ('sending', 'queued'):
                    row.pop('bound_turn')
                self.store.db.execute('UPDATE requests SET data=?', (json.dumps(row),)); self.store.db.commit()
                before = self.store.db.total_changes
                self.assertFalse(admin.inspect(self.store, {'transport_id': self.tid})['eligible'])
                self.assertEqual(self.store.db.total_changes, before)
                with self.assertRaises(peer_bridge.PeerError) as raised:
                    admin.retire(self.store, self.request())
                self.assertEqual(raised.exception.status, 409)
                self.assertEqual(life.get(self.store.db, self.tid)['execution'], 'uncertain')
        self.store.db.execute('UPDATE shared_bridge_requests SET launched=NULL'); self.store.db.commit()
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, self.request())

    def test_malformed_missing_and_terminal_selections_reject(self):
        for data in ({}, {'transport_id': uid(), 'extra': True}, {'transport_id': []}, {'transport_id': uid()}):
            with self.subTest(data=data), self.assertRaises(peer_bridge.PeerError):
                admin.inspect(self.store, data)
        for changes in ({'confirm': 'yes'}, {'agent': []}, {'extra': True}):
            with self.subTest(changes=changes), self.assertRaises(peer_bridge.PeerError):
                admin.retire(self.store, dict(self.request(), **changes))
        self.store.db.execute("UPDATE requests SET data='not JSON'"); self.store.db.commit()
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, self.request())
        self.store.db.execute('DELETE FROM requests')
        for execution, capture in (('reserved', 'pending'), ('submitted', 'pending'), ('running', 'pending'),
                                    ('completed', 'withheld'), ('dropped', 'withheld'), ('uncertain', 'captured')):
            self.store.db.execute('UPDATE peer_execution SET execution=?,capture=?', (execution, capture)); self.store.db.commit()
            with self.subTest(execution=execution, capture=capture), self.assertRaises(peer_bridge.PeerError):
                admin.retire(self.store, self.request())

    def open_attempt(self, runtime, state, *, abort_without_turn=False, delete_transcript=False, **evidence):
        tid, agent, cid = uid(), ('c:' if runtime == 'claude' else 'x:') + uid(), uid()
        row = dict(self.row, request_id=tid, agent=agent, runtime=runtime, state=state)
        row.pop('bound_turn')
        if runtime == 'claude':
            row.update(transport='claude_mod', target_project=self.tmp.name, mod_project=self.tmp.name,
                       mod_enrollment=uid(), mod_attempt_id=uid(), mod_generation=uid())
        row.update(evidence)
        if abort_without_turn:
            event = dict(type='event_msg', payload=dict(type='turn_aborted'))
            self.assertIsNone(self.store._consume(row, event))
            self.assertFalse(row.get('settled'))
        if delete_transcript:
            transcript = Path(self.tmp.name) / (tid + '.jsonl')
            event = dict(type='event_msg', payload=dict(type='task_started', turn_id=row['bound_turn']))
            transcript.write_text(json.dumps(event) + '\n')
            st = transcript.stat()
            row.update(path=str(transcript), device=st.st_dev, inode=st.st_ino, cursor=st.st_size)
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                              (cid, agent, '{}', 'a'*64, tid, uid(), self.created, 101., 'Fictional prompt'))
        self.store._save(row)
        if delete_transcript:
            transcript.unlink()
        return dict(transport_id=tid, agent=agent, confirm=admin.CONFIRM), row

    def test_aged_unresolved_provider_attempts_can_be_retired_online(self):
        scenarios = (
            ('Codex start without end', 'codex', 'awaiting_reply', dict(bound_turn='fictional-turn')),
            ('Codex transcript removed after start', 'codex', 'awaiting_reply',
             dict(bound_turn='fictional-turn', delete_transcript=True)),
            ('Codex accepted and never picked up', 'codex', 'queued', {}),
            ('Codex abort without turn ID', 'codex', 'awaiting_reply',
             dict(bound_turn='fictional-turn', abort_without_turn=True)),
            ('Claude started with plugin polling', 'claude', 'awaiting_reply', dict(mod_turn='fictional-turn')),
            ('Claude claimed with plugin polling', 'claude', 'queued', {}),
        )
        with patch.object(self.store, '_send', side_effect=AssertionError('native dispatch')), \
                patch.object(self.store, '_poll', side_effect=AssertionError('native read')):
            for name, runtime, state, evidence in scenarios:
                for age in (20 * 60, 5 * 3600, 25 * 3600):
                    for retained in (True, False):
                        with self.subTest(scenario=name, age=age, retained=retained):
                            request, _ = self.open_attempt(runtime, state, **evidence)
                            tid, agent = request['transport_id'], request['agent']
                            if not retained:
                                self.store.db.execute('DELETE FROM requests WHERE id=?', (tid,))
                                self.store.db.commit()
                            self.clock.return_value = self.created + age
                            before = self.store.db.total_changes
                            view = admin.inspect(self.store, {'transport_id': tid})
                            self.assertEqual(self.store.db.total_changes, before)
                            self.assertEqual(view['execution'], 'running' if state == 'awaiting_reply' else 'submitted')
                            self.assertEqual(view['retained_payload'], retained)
                            self.assertTrue(view['eligible'])
                            self.assertTrue(life.blockers(self.store.db, agent))
                            receipt = admin.retire(self.store, request)
                            self.assertEqual((receipt['status'], receipt['provider_outcome']), ('retired', 'unknown'))
                            self.assertEqual(life.blockers(self.store.db, agent), [])
                            remains = self.store.db.execute('SELECT data FROM requests WHERE id=?', (tid,)).fetchone()
                            self.assertEqual(remains is not None, retained)
                            if remains:
                                final = json.loads(remains[0])
                                self.assertEqual((final['state'], final['reply']), ('unknown', ''))
                                self.assertFalse(final.get('settled'))

    def test_active_retirement_checks_deadline_and_exact_identity(self):
        request, row = self.open_attempt('codex', 'queued')
        selection = {'transport_id': row['request_id']}
        self.clock.return_value = self.created + peer_bridge.TTL - 0.001
        self.assertFalse(admin.inspect(self.store, selection)['eligible'])
        with self.assertRaises(peer_bridge.PeerError) as raised:
            admin.retire(self.store, request)
        self.assertEqual(raised.exception.status, 409)
        self.clock.return_value = self.created + peer_bridge.TTL
        self.assertTrue(admin.inspect(self.store, selection)['eligible'])
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, dict(request, agent='x:' + uid()))
        # Retirement rechecks the server boundary rather than trusting inspection.
        self.clock.return_value = self.created + peer_bridge.TTL - 0.001
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, request)
        self.assertTrue(life.blockers(self.store.db, row['agent']))
        self.clock.return_value = self.created + peer_bridge.TTL
        self.assertEqual(admin.retire(self.store, request)['provider_outcome'], 'unknown')

    def test_aged_attempt_excludes_new_completion_capture_and_protected_payload(self):
        self.clock.return_value = self.created + 1200
        for update in (dict(state='answered', reply='Fictional answer', settled=True),
                       dict(settled=True), dict(execution_outcome='completed'),
                       dict(state='quarantined'), dict(capture_disposition='captured'),
                       dict(agent='x:' + uid()), dict(attachment_owner_version=None)):
            with self.subTest(change=set(update)):
                request, row = self.open_attempt('codex', 'awaiting_reply', bound_turn='fictional-turn')
                selection = {'transport_id': row['request_id']}
                self.assertTrue(admin.inspect(self.store, selection)['eligible'])
                raw = json.dumps(dict(row, **update))
                self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (raw, row['request_id']))
                self.store.db.commit()
                before = self.store.db.total_changes
                if 'agent' in update or 'attachment_owner_version' in update:
                    with self.assertRaises(peer_bridge.PeerError):
                        admin.inspect(self.store, selection)
                else:
                    self.assertFalse(admin.inspect(self.store, selection)['eligible'])
                self.assertEqual(self.store.db.total_changes, before)
                with self.assertRaises(peer_bridge.PeerError) as raised:
                    admin.retire(self.store, request)
                self.assertEqual(raised.exception.status, 409)
                self.assertEqual(life.get(self.store.db, row['request_id'])['execution'], 'running')
                self.assertEqual(self.store.db.execute('SELECT data FROM requests WHERE id=?',
                                                       (row['request_id'],)).fetchone(), (raw,))

    def test_completion_after_inspection_cannot_be_retired(self):
        self.clock.return_value = self.created + 1200
        request, row = self.open_attempt('codex', 'awaiting_reply', bound_turn='fictional-turn')
        selection = {'transport_id': row['request_id']}
        self.assertTrue(admin.inspect(self.store, selection)['eligible'])
        self.store._save(dict(row, state='answered', settled=True, reply='Fictional answer'))
        self.assertFalse(admin.inspect(self.store, selection)['eligible'])
        with self.assertRaises(peer_bridge.PeerError) as raised:
            admin.retire(self.store, request)
        self.assertEqual(raised.exception.status, 409)
        record = life.get(self.store.db, row['request_id'])
        self.assertEqual((record['execution'], record['capture']), ('completed', 'captured'))
        self.store.db.execute('DELETE FROM requests WHERE id=?', (row['request_id'],))
        self.store.db.commit()
        self.assertFalse(admin.inspect(self.store, selection)['eligible'])
        with self.assertRaises(peer_bridge.PeerError):
            admin.retire(self.store, request)
        self.assertEqual(life.get(self.store.db, row['request_id']), record)

    def test_failure_rolls_back_retirement_and_reconciliation(self):
        before = self.store.db.execute('SELECT * FROM peer_execution').fetchall()
        public_before = self.store.shared.result(self.cid)
        with patch.object(life, 'record_event', side_effect=sqlite3.OperationalError('fictional failure')):
            with self.assertRaises(sqlite3.OperationalError):
                admin.retire(self.store, self.request())
        self.assertEqual(before, self.store.db.execute('SELECT * FROM peer_execution').fetchall())
        self.assertEqual(self.store.shared.result(self.cid), public_before)
        self.assertFalse(self.store.db.in_transaction)

    def test_lock_orders_retirement_stale_writeback_and_admission(self):
        entered, finished = threading.Event(), threading.Event()
        stale = dict(self.row, state='answered', reply='PRIVATE LATE ANSWER', settled=True)
        def worker():
            entered.set()
            admin.retire(self.store, self.request())
            finished.set()
        with self.store.lock:
            thread = threading.Thread(target=worker); thread.start()
            self.assertTrue(entered.wait(2)); self.assertFalse(finished.is_set())
            self.assertIsNotNone(life.capacity_error(self.store.db, self.agent, 102., 900))
        thread.join(3); self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        with self.store.lock:
            self.store._save(stale)
            self.assertIsNone(life.capacity_error(self.store.db, self.agent, 102., 900))
        result = self.store.shared.result(self.cid)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.assertEqual(life.get(self.store.db, self.tid)['execution'], 'retired')


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class HTTPTests(unittest.TestCase):
    request = AdminTests.request

    def setUp(self):
        AdminTests.setUp(self)
        self.old = server.MESSAGING, server.KEY, server.PORT
        server.MESSAGING, server.KEY = self.store, 'fictional-owner-key'
        self.httpd = server.Server(('127.0.0.1', 0), server.H)
        server.PORT = self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.cleanup_http)

    def cleanup_http(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()
        server.MESSAGING, server.KEY, server.PORT = self.old

    def http(self, path='/bridge-admin/v1/inspect', body=None, headers=None, method='POST'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        hs = {'X-Live-Room-Key': 'fictional-owner-key', 'Content-Type': 'application/json'}
        hs.update(headers or {})
        raw = json.dumps({'transport_id': self.tid}) if body is None else body
        conn.request(method, path, raw, hs)
        response = conn.getresponse(); data = response.read(); status = response.status; conn.close()
        return status, json.loads(data)

    def raw_http(self, extra='', body='{}', auth=True):
        key = 'X-Live-Room-Key: fictional-owner-key\r\n' if auth else ''
        raw = ('POST /bridge-admin/v1/inspect HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%sContent-Type: application/json\r\nContent-Length: %d\r\n%s\r\n%s' % (self.port, key, len(body.encode()), extra, body)).encode()
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as sock:
            sock.sendall(raw); response = http.client.HTTPResponse(sock); response.begin()
            status = response.status; response.read(); return status

    def test_actual_owner_dispatch_inspect_retire_and_no_peer_rpc_capability(self):
        self.assertEqual(self.http()[0], 200)
        status, data = self.http('/bridge-admin/v1/retire', json.dumps(self.request()))
        self.assertEqual((status, data['status'], data['provider_outcome']), (200, 'retired', 'unknown'))
        self.assertEqual(self.http('/bridge-admin/v1/retire', json.dumps(self.request()))[1]['status'], 'already_retired')
        with self.assertRaises(peer_bridge.PeerError):
            self.store.shared.rpc(uid(), {'method': 'retire', 'params': self.request()})

    def test_authentication_precedes_body_and_rejects_shared_credentials(self):
        for hs in ({'X-Live-Room-Key': ''}, {'X-Live-Room-Key': 'fictional-bridge-secret'},
                   {'Origin': 'https://evil.test'}, {'Host': 'evil.test'}):
            self.assertEqual(self.http(body='{', headers=hs)[0], 403)
        self.assertEqual(self.raw_http('X-Live-Room-Key: fictional-owner-key\r\n'), 403)
        self.assertEqual(self.raw_http('Host: 127.0.0.1:%d\r\n' % self.port), 403)
        self.assertEqual(self.raw_http('Origin: http://localhost:%d\r\nOrigin: http://localhost:%d\r\n' % (self.port, self.port)), 403)
        # Authentication must return without waiting for the advertised body.
        with socket.create_connection(('127.0.0.1', self.port), timeout=1) as sock:
            sock.sendall(('POST /bridge-admin/v1/retire HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 4000\r\n\r\n' % self.port).encode())
            response = http.client.HTTPResponse(sock); response.begin(); self.assertEqual(response.status, 403); response.read()

    def test_route_framing_and_json_rejections(self):
        for path in ('/bridge-admin/v1/inspect?', '/bridge-admin/v1/inspect?x=1', '/bridge-admin/v1/inspect#fragment',
                     '/bridge-admin/v1/%69nspect', '/bridge-admin/v1/other'):
            with self.subTest(path=path): self.assertEqual(self.http(path)[0], 400)
        for body in ('[]', 'null', '{', '{"transport_id":"x","transport_id":"y"}', '{"x":NaN}', '{"x":1e999}'):
            with self.subTest(body=body): self.assertEqual(self.http(body=body)[0], 400)
        for extra in ('Content-Length: 2\r\n', 'Transfer-Encoding: chunked\r\n', 'Content-Encoding: gzip\r\n', 'Content-Type: application/json\r\n'):
            with self.subTest(extra=extra): self.assertEqual(self.raw_http(extra), 400)
        self.assertEqual(self.http(headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.http(body=' ' * (admin.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.http(method='GET')[0], 400)

    def test_unavailable_and_failed_transaction_return_bounded_error(self):
        server.MESSAGING = None
        self.assertEqual(self.http()[0], 503)
        server.MESSAGING = self.store
        with patch.object(life, 'record_event', side_effect=sqlite3.OperationalError('PRIVATE ERROR')):
            status, result = self.http('/bridge-admin/v1/retire', json.dumps(self.request()))
        self.assertEqual(status, 503); self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(life.get(self.store.db, self.tid)['execution'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
