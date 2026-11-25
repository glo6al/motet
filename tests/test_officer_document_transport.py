"""Real ledger, private files and workers; only fictional fake HTTPS connections."""
from dataclasses import replace
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_document_transport as transport
import officer_documents as documents
import officer_document_ledger as ledger_module
from attachments_types import AttachmentError
import test_officer_document_ledger as execution_fixtures
from test_officer_documents import response, wire
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY

# A call that must time out while the worker is held at a gated step (its connect, request or response) needs a
# deadline the worker can reach that step within. On POSIX 50 ms is plenty; on Windows the durable store commits on
# the way flush to disk and take longer, so the deadline is 2 s there. The step stays held either way.
GATED = 2 if os.name == 'nt' else .05

KEY = 'fictional-host-key'
REAL_TLS_CONTEXT = transport._tls_context


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.shutdowns = 0
    def shutdown(self, how):
        self.shutdowns += 1
        self.closed = True
    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, raw, status=200, *, gate=None, close_fails=False):
        self.raw, self.status, self.offset = raw, status, 0
        self.length = len(raw)
        self.gate, self.entered, self.closed = gate, threading.Event(), False
        self.close_fails = close_fails
    def read(self, size):
        self.entered.set()
        if self.gate is not None:
            if not self.gate.wait(3): raise RuntimeError('fictional read test gate timed out')
        out = self.raw[self.offset:self.offset + size]
        self.offset += len(out)
        self.length -= len(out)
        return out
    def close(self):
        if self.close_fails: raise OSError('fictional secret close error')
        self.closed = True


class FakeConnection:
    def __init__(self, response=None, *, connect_gate=None, request_gate=None, close_fails=False):
        self.response = response or FakeResponse(wire(globals()['response']('Fictional answer.', citations=False)))
        self.connect_gate, self.request_gate = connect_gate, request_gate
        self.connect_entered, self.request_entered = threading.Event(), threading.Event()
        self.sock = None
        self.auto_open, self.debuglevel, self.connects, self.closes = True, 99, 0, 0
        self.requests = []
        self.close_fails = close_fails
    def set_debuglevel(self, value): self.debuglevel = value
    def connect(self):
        self.connects += 1
        self.connect_entered.set()
        if self.connect_gate is not None and not self.connect_gate.wait(3):
            raise RuntimeError('fictional connect test gate timed out')
        self.sock = FakeSocket()
    def request(self, method, path, *, body, headers):
        self.request_entered.set()
        if self.request_gate is not None and not self.request_gate.wait(3):
            raise RuntimeError('fictional send test gate timed out')
        # Model HTTPConnection's actual no-reconnect contract, not just counters.
        if self.sock is None:
            if not self.auto_open: raise http.client.NotConnected()
            self.connect()
        if self.sock.closed: raise BrokenPipeError('fictional closed socket')
        self.requests.append((method, path, body, headers))
    def getresponse(self): return self.response
    def close(self):
        self.closes += 1
        if self.close_fails: raise OSError('fictional secret connection close error')
        if self.sock is not None: self.sock.close()
        self.sock = None


class Factory:
    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()
        self.calls = []
    def __call__(self, host, port, **options):
        self.calls.append((host, port, options))
        return self.connection


class LatentThread:
    def __init__(self, *, target, **kwargs): self.target = target
    def start(self): raise RuntimeError('fictional ambiguous start')
    def is_alive(self): return False
    def finish_late(self): self.target()


@unittest.skipUnless(STORE, STORE_WHY)
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.f = execution_fixtures.ExecutionTests()
        self.f.setUp()
        self.store = self.f.store
        self.transports, self.results, self.leases, self.release_events = [], [], [], []
        self.tls_patch = mock.patch.object(transport, '_tls_context', side_effect=REAL_TLS_CONTEXT)   # the real factory, so its no-key-logging setting is what is checked
        self.tls_patch.start()
    def tearDown(self):
        for event in self.release_events: event.set()
        for result in self.results:
            try: result.close()
            except (transport.TransportRejected, AttachmentError): pass
        for item in self.transports:
            item.close(timeout=3)
        for lease in self.leases:
            try: lease.close()
            except AttachmentError: pass
        self.f.tearDown()
        self.tls_patch.stop()
    def ticket(self):
        if not self.f.sources: self.f.add()
        lease = self.store.files.buffer_lease(tuple(self.f.sources))
        self.leases.append(lease)
        request = self.f.prepare()
        state, permit, prepared = self.f.claim(request)
        return request, state, permit, prepared, lease
    def make(self, connection=None, **options):
        factory = Factory(connection)
        options.setdefault('_test_qualification', lambda profile, wall: None)
        item = transport.DirectDocumentTransport(enabled=True, connect=factory, **options)
        self.transports.append(item)
        return item, factory
    def call(self, item, ticket, seconds=2, credential=lambda: KEY):
        _, _, permit, prepared, lease = ticket
        result = item.call(prepared, self.f.l, permit, lease, credential, time.monotonic() + seconds)
        if type(result) is transport.OwnedDocumentResult: self.results.append(result)
        return result
    def rejected(self, code, fn):
        with self.assertRaises(transport.TransportRejected) as caught: fn()
        self.assertEqual(caught.exception.code, code)
    def busy(self):
        with self.assertRaises(AttachmentError): self.store.close()

    def test_real_ledger_exact_request_and_result_scope_through_answer_commit(self):
        ticket = self.ticket()
        request, state, permit, prepared, lease = ticket
        item, factory = self.make()
        result = self.call(item, ticket)
        self.assertIs(type(result), transport.OwnedDocumentResult)
        self.assertEqual(len(factory.calls), 1)
        host, port, options = factory.calls[0]
        self.assertEqual((host, port), ('api.anthropic.com', 443))
        self.assertEqual(options['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(options['context'].check_hostname)
        self.assertIsNone(options['context'].keylog_filename)
        connection = factory.connection
        self.assertFalse(connection.auto_open)
        self.assertEqual(connection.debuglevel, 0)
        self.assertEqual(connection.connects, 1)
        self.assertEqual(connection.requests[0][3]['anthropic-version'],'2026-10-21')
        self.f.error('document_permit_invalid',lambda:self.f.l.consume(permit,self.f.selection,
            request['id'],prepared.body_digest,state['profileDigest']))
        with self.assertRaises(AttributeError):result.http_status=400
        self.assertEqual(connection.requests, [('POST', '/v1/messages', prepared.body_bytes,
            {**dict(documents.HEADERS), 'x-api-key': KEY})])
        self.busy()
        with self.assertRaises(AttachmentError): lease.close()
        with self.assertRaises(AttachmentError): self.store.files._release_preparation_buffer(copy.copy(lease))
        cloned = copy.copy(result)
        self.rejected('result_closed', cloned.close)
        candidate = documents.validate_response(self.f.p, prepared, result.raw_response)
        facts = ledger_module.ResultFacts(request['id'], state['claimNonce'], prepared.body_digest, state['profileDigest'],
            'accepted', candidate.response_digest, candidate.message_id, documents.MODEL,
            self.f.pricing.billing_revision, candidate.usage_facts)
        self.f.l.record_result(self.f.selection, facts)
        self.f.l.commit_answer(self.f.selection, request['id'], candidate)
        self.assertTrue(self.f.l.status(self.f.selection, request['id'])['answerAvailable'])
        candidate = prepared = ticket = None
        result.close()
        self.assertFalse(self.store.files.io_active())
        self.rejected('result_closed', lambda: result.raw_response)
        self.rejected('result_closed', result.close)
        with mock.patch.object(self.store.files, 'read', side_effect=AssertionError('no retirement IO')):
            self.assertTrue(self.f.l.retire_permit(permit))

    def test_gate_demo_bad_deadline_and_request_have_zero_consume_or_connections(self):
        ticket = self.ticket()
        _, _, permit, prepared, lease = ticket
        factory = Factory()
        for options in ({}, {'enabled': True, 'demo': True}, {'enabled': 1}):
            item = transport.DirectDocumentTransport(connect=factory, **options)
            self.rejected('transport_disabled', lambda: item.call(prepared, self.f.l, permit, lease, lambda: KEY, time.monotonic()+1))
        item, factory = self.make()
        with mock.patch.object(self.f.l, 'consume', wraps=self.f.l.consume) as consume:
            for deadline in (time.monotonic()-1, time.monotonic()+121, float('nan'), True):
                self.rejected('deadline_invalid', lambda deadline=deadline: item.call(prepared,self.f.l,permit,lease,lambda:KEY,deadline))
            self.rejected('request_invalid', lambda: item.call(replace(prepared, endpoint='https://foreign.invalid/'),
                self.f.l,permit,lease,lambda:KEY,time.monotonic()+1))
            self.rejected('request_invalid', lambda: item.call(replace(prepared, headers=()),
                self.f.l,permit,lease,lambda:KEY,time.monotonic()+1))
            self.assertEqual(consume.call_count, 0)
        self.assertEqual(factory.calls, [])

    def test_missing_credential_retires_without_consume_and_has_no_raw_error(self):
        ticket = self.ticket()
        item, factory = self.make()
        with mock.patch.object(self.f.l, 'consume', wraps=self.f.l.consume) as consume:
            result = self.call(item, ticket, credential=lambda: '')
        self.assertEqual(result.error, 'credential_unavailable')
        self.assertEqual(consume.call_count, 0)
        self.assertEqual(factory.calls, [])
        self.assertNotIn(KEY, repr(result))
        self.assertFalse(self.store.files.io_active())
        self.f.error('document_permit_invalid', lambda: self.f.l.consume(ticket[2],self.f.selection,
            ticket[0]['id'],ticket[3].body_digest,ticket[1]['profileDigest']))

    def test_subset_extra_cross_store_and_copied_lease_reject_before_consumption(self):
        self.f.add(); self.f.add(execution_fixtures.PNG,'image/png')
        ticket = self.ticket()
        item, factory = self.make()
        subset = self.store.files.buffer_lease((self.f.sources[0],))
        self.leases.append(subset)
        with mock.patch.object(self.f.l, 'consume', wraps=self.f.l.consume) as consume:
            self.rejected('buffer_lease_invalid', lambda: item.call(ticket[3],self.f.l,ticket[2],subset,lambda:KEY,time.monotonic()+1))
            self.rejected('buffer_lease_invalid', lambda: item.call(ticket[3],self.f.l,ticket[2],copy.copy(ticket[4]),lambda:KEY,time.monotonic()+1))
            self.assertEqual(consume.call_count,0)
        self.assertEqual(factory.calls,[])
        subset.close()

    def test_logical_revoke_is_immediate_while_physical_bytes_remain_pinned(self):
        ticket = self.ticket()
        gate = threading.Event(); self.release_events.append(gate)
        reply = FakeResponse(wire(response('Fictional answer.',citations=False)),gate=gate)
        item, factory = self.make(FakeConnection(reply))
        result = self.call(item,ticket,seconds=GATED)
        self.assertEqual(result.state,'unknown')
        self.assertTrue(reply.entered.is_set())
        source = self.f.sources[0]
        row = self.store.db.execute('SELECT * FROM files WHERE id=?',(source,)).fetchone()
        before = self.store.storage()['usedBytes']
        self.f.f.revoke(source)
        self.assertEqual(self.store.files.remove(source,json.loads(row['identity']),row['digest']),'busy')
        self.assertEqual(self.store.storage()['usedBytes'],before)
        self.busy()
        gate.set()
        item.close(timeout=2)
        self.assertFalse(self.store.files.io_active())
        self.assertEqual(self.f.l.status(self.f.selection,ticket[0]['id'])['state'],'claimed')

    @staticmethod
    def worker_buffer_facts(job):
        frame=sys._current_frames().get(job.thread.ident)
        while frame is not None and frame.f_code is not transport._Job.run.__code__:
            frame=frame.f_back
        if frame is None:
            raise AssertionError('the controlled worker frame was not live')
        facts={name:frame.f_locals.get(name) is None for name in ('raw','response','connection','key')}
        facts['prepared']=job.prepared is None
        facts['credential']=job.credential is None
        del frame
        return facts

    def test_abandoned_completed_read_drops_worker_buffers_before_unpin(self):
        ticket=self.ticket();closing=threading.Event();release=threading.Event()
        self.release_events.append(release)
        class SlowCloseResponse(FakeResponse):
            def close(self):
                closing.set()
                if not release.wait(3):raise RuntimeError('test close gate timed out')
                super().close()
        reply=SlowCloseResponse(wire(response('Completed before the close gate.',citations=False)))
        item,factory=self.make(FakeConnection(reply))
        stamp=[time.monotonic()];item.clock=lambda:stamp[0]
        observed=[];results=[]
        original=self.store.files._release_worker_buffer
        def inspect_release(claim):
            observed.append(self.worker_buffer_facts(claim._owner))
            return original(claim)
        caller=threading.Thread(target=lambda:results.append(item.call(ticket[3],self.f.l,ticket[2],
            ticket[4],lambda:KEY,stamp[0]+.1)))
        with mock.patch.object(self.store.files,'_release_worker_buffer',side_effect=inspect_release):
            caller.start()
            try:
                self.assertTrue(closing.wait(1))
                self.assertEqual(reply.offset,len(reply.raw))
                self.assertTrue(self.store.files.io_active())
                stamp[0]+=1
                caller.join(1)
                self.assertFalse(caller.is_alive());self.assertEqual(results[0].state,'unknown')
                self.assertTrue(self.store.files.io_active())
                release.set();item.clock=time.monotonic;item.close(timeout=2)
            finally:
                release.set();caller.join(2);item.clock=time.monotonic
        self.assertEqual(len(observed),1)
        self.assertTrue(all(observed[0].values()),observed)
        self.assertFalse(self.store.files.io_active())

    def test_result_handoff_clears_worker_buffers_before_abandonment_can_close(self):
        import attachments_files
        ticket=self.ticket();handoff=threading.Event();resume=threading.Event()
        self.release_events.append(resume)
        item,factory=self.make()
        stamp=[time.monotonic()];item.clock=lambda:stamp[0]
        jobs=[];observed=[];results=[]
        class PauseAfterHandoffUnlock:
            def __init__(self,job):self.job,self.lock,self.paused=job,threading.Lock(),False
            def __enter__(self):self.lock.acquire();return self
            def __exit__(self,*args):
                pause=(not self.paused and threading.current_thread() is self.job.thread
                       and type(self.job.outcome) is transport.OwnedDocumentResult)
                if pause:self.paused=True
                self.lock.release()
                if pause:
                    handoff.set()
                    if not resume.wait(3):raise RuntimeError('test handoff gate timed out')
        initialize=transport._Job.__init__
        def track_job(job,*args,**kwargs):
            initialize(job,*args,**kwargs);job.lock=PauseAfterHandoffUnlock(job);jobs.append(job)
        close_result=attachments_files._BufferResultLease.close
        def inspect_result_close(lease):
            observed.append(self.worker_buffer_facts(jobs[0]))
            return close_result(lease)
        caller=threading.Thread(target=lambda:results.append(item.call(ticket[3],self.f.l,ticket[2],
            ticket[4],lambda:KEY,stamp[0]+.1)))
        with mock.patch.object(transport._Job,'__init__',track_job),mock.patch.object(
                attachments_files._BufferResultLease,'close',inspect_result_close):
            caller.start()
            try:
                self.assertTrue(handoff.wait(1))
                self.assertTrue(self.store.files.io_active())
                stamp[0]+=1
                caller.join(1)
                self.assertFalse(caller.is_alive());self.assertEqual(results[0].state,'unknown')
                self.assertEqual(len(observed),1)
                self.assertTrue(all(observed[0].values()),observed)
                self.assertFalse(self.store.files.io_active())
                self.assertFalse(jobs[0].exited.is_set())
            finally:
                resume.set();caller.join(2);item.clock=time.monotonic
                item.close(timeout=2)

    def test_delayed_consume_timeout_retirement_prevents_connection(self):
        ticket = self.ticket(); entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = self.f.l._read_dependencies
        from contextlib import contextmanager
        @contextmanager
        def delayed(*args,**kwargs):
            entered.set(); release.wait(3)
            with original(*args,**kwargs) as value: yield value
        item, factory = self.make()
        with mock.patch.object(self.f.l,'_read_dependencies',delayed):
            result = self.call(item,ticket,seconds=.05)
            self.assertTrue(entered.is_set())
            self.assertEqual(result.state,'unknown')
            self.assertTrue(self.store.files.io_active());self.busy()
            release.set();item.close(timeout=2)
        self.assertEqual(factory.calls,[])
        self.assertFalse(self.store.files.io_active())
        self.assertEqual(self.f.l.status(self.f.selection,ticket[0]['id'])['state'],'claimed')

    def test_registry_lock_contention_does_not_block_timeout_or_allow_late_post(self):
        ticket = self.ticket();item,factory=self.make()
        self.f.l._permit_lock.acquire()
        try:
            before=time.monotonic();result=self.call(item,ticket,seconds=.05)
            self.assertLess(time.monotonic()-before,.5)
            self.assertEqual(result.state,'unknown')
            self.assertTrue(self.store.files.io_active())
            self.assertFalse(self.f.l.retire_permit(ticket[2]))
        finally:self.f.l._permit_lock.release()
        item.close(timeout=2)
        self.assertEqual(factory.calls,[])
        self.assertFalse(self.store.files.io_active())

    def test_delayed_connect_after_abandon_never_posts(self):
        ticket=self.ticket();gate=threading.Event();self.release_events.append(gate)
        connection=FakeConnection(connect_gate=gate);item,factory=self.make(connection)
        result=self.call(item,ticket,seconds=GATED)
        self.assertTrue(connection.connect_entered.is_set());self.assertFalse(result.dispatch_started)
        self.assertTrue(self.store.files.io_active());self.busy()
        gate.set();item.close(timeout=2)
        self.assertEqual(connection.requests,[]);self.assertEqual(connection.connects,1)
        self.assertFalse(self.store.files.io_active())

    def test_close_between_connect_and_send_does_not_reconnect(self):
        ticket=self.ticket();gate=threading.Event();self.release_events.append(gate)
        connection=FakeConnection(request_gate=gate);item,factory=self.make(connection)
        result=self.call(item,ticket,seconds=GATED)
        self.assertTrue(connection.request_entered.is_set());self.assertTrue(result.dispatch_started)
        # Simulate the real HTTPConnection.close race: sock=None would trigger
        # reconnect on request/send unless auto_open has been disabled.
        connection.close();gate.set();item.close(timeout=2)
        self.assertEqual(connection.connects,1);self.assertEqual(connection.requests,[])
        self.assertFalse(connection.auto_open)

    def test_ambiguous_start_exception_keeps_pin_until_actual_late_entry_exit(self):
        ticket=self.ticket();threads=[]
        def make_thread(**kwargs):
            thread=LatentThread(**kwargs);threads.append(thread);return thread
        item,factory=self.make(_thread_factory=make_thread)
        result=self.call(item,ticket)
        self.assertEqual(result.error,'startup_unconfirmed')
        self.assertTrue(self.store.files.io_active());self.busy()
        self.rejected('transport_busy',lambda:item.close(timeout=0))
        self.f.error('document_permit_invalid',lambda:self.f.l.consume(ticket[2],self.f.selection,ticket[0]['id'],ticket[3].body_digest,ticket[1]['profileDigest']))
        threads[0].finish_late();item.close(timeout=1)
        self.assertEqual(factory.calls,[]);self.assertFalse(self.store.files.io_active())

    def test_start_exception_after_real_entry_cannot_release_the_live_worker(self):
        ticket=self.ticket();gate=threading.Event();self.release_events.append(gate)
        connection=FakeConnection(connect_gate=gate)
        class StartedThenRaised:
            def __init__(self,**kwargs):self.thread=threading.Thread(**kwargs)
            def start(self):
                self.thread.start()
                if not connection.connect_entered.wait(1):raise AssertionError('worker did not enter')
                raise RuntimeError('fictional start error after actual entry')
            def is_alive(self):return self.thread.is_alive()
        item,factory=self.make(connection,_thread_factory=StartedThenRaised)
        result=self.call(item,ticket)
        self.assertEqual(result.error,'startup_unconfirmed')
        self.assertTrue(result.consumed);self.assertFalse(result.dispatch_started)
        self.assertTrue(self.store.files.io_active());self.busy()
        gate.set();item.close(timeout=2)
        self.assertEqual(connection.requests,[]);self.assertFalse(self.store.files.io_active())

    def test_proven_constructor_failure_retires_permit_and_drops_only_local_ownership(self):
        ticket=self.ticket();factory=Factory()
        with mock.patch('officer_document_transport.threading.Thread',side_effect=RuntimeError('fictional private startup error')):
            item=transport.DirectDocumentTransport(enabled=True,connect=factory,_test_qualification=lambda *a:None)
            self.transports.append(item)
            result=self.call(item,ticket)
        self.assertEqual(result.error,'startup_unconfirmed')
        self.assertEqual(factory.calls,[]);self.assertFalse(self.store.files.io_active())
        item.close(timeout=0)
        self.f.error('document_permit_invalid',lambda:self.f.l.consume(ticket[2],self.f.selection,ticket[0]['id'],ticket[3].body_digest,ticket[1]['profileDigest']))
        self.assertEqual(self.f.l.status(self.f.selection,ticket[0]['id'])['state'],'claimed')

    def test_non200_complete_response_has_no_retry_redirect_or_raw_body(self):
        ticket=self.ticket()
        connection=FakeConnection(FakeResponse(b'FICTIONAL_PRIVATE_PROVIDER_ERROR',status=302))
        item,factory=self.make(connection)
        result=self.call(item,ticket)
        self.assertEqual((result.state,result.http_status,result.error),('failed',302,'http_error'))
        self.assertEqual(len(factory.calls),1);self.assertEqual(len(connection.requests),1)
        self.assertFalse(hasattr(result,'raw_response'))
        self.assertNotIn('FICTIONAL_PRIVATE',repr(result));self.assertNotIn(KEY,repr(result))
        self.assertFalse(self.store.files.io_active())

    def test_oversized_or_incomplete_response_is_unknown_not_success(self):
        ticket=self.ticket()
        item,factory=self.make(FakeConnection(FakeResponse(b'x'*(documents.MAX_RESPONSE_BYTES+1))))
        result=self.call(item,ticket)
        self.assertEqual(result.state,'unknown');self.assertEqual(result.error,'response_bound')
        self.assertFalse(self.store.files.io_active())

    def test_chunked_framing_and_invalid_utf8_are_left_to_http_and_c1(self):
        ticket=self.ticket();payload=wire(response('Fictional answer.',citations=False))
        raw=(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+
             f'{len(payload):x}'.encode()+b'\r\n'+payload+b'\r\n0\r\n\r\n')
        class WireSocket:
            def makefile(self,*args):return io.BytesIO(raw)
        parsed=http.client.HTTPResponse(WireSocket());parsed.begin()
        item,factory=self.make(FakeConnection(parsed));result=self.call(item,ticket)
        self.assertEqual(result.raw_response,payload)
        candidate=documents.validate_response(self.f.p,ticket[3],result.raw_response)
        self.assertEqual(candidate.text,'Fictional answer.');result.close()

    def test_buffer_bounds_read_allowance_copy_protection_and_process_binding(self):
        self.f.add();fid=self.f.sources[0]
        lease=self.store.files.buffer_lease((fid,));self.leases.append(lease)
        row=self.store.db.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
        self.assertEqual(self.store.files.read(fid,json.loads(row['identity']),row['digest'],1024),execution_fixtures.PDF)
        self.assertEqual(self.store.files.remove(fid,json.loads(row['identity']),row['digest']),'busy')
        with self.assertRaises(AttachmentError):self.store.files.buffer_lease((fid,fid))
        with self.assertRaises(AttachmentError):self.store.files.buffer_lease((fid,)*51)
        with self.assertRaises(AttachmentError):copy.copy(lease).close()
        with mock.patch('attachments_files.os.getpid',return_value=os.getpid()+1):
            with self.assertRaises(AttachmentError):lease.close()
        lease.close()
        with self.assertRaises(AttachmentError):lease.close()
        self.assertFalse(self.store.files.io_active())

    def test_default_transport_rejects_generic_profile_before_consume(self):
        import officer_document_policy as policy
        ticket=self.ticket();factory=Factory()
        item=transport.DirectDocumentTransport(enabled=True,connect=factory,wall=lambda:policy.VALID_FROM+1)
        self.transports.append(item)
        with mock.patch.object(self.f.l,'consume',wraps=self.f.l.consume) as consume:
            self.rejected('qualification_unavailable',lambda:self.call(item,ticket))
            self.assertEqual(consume.call_count,0)
        self.assertEqual(factory.calls,[])

    def test_cross_store_and_extra_source_lease_are_not_authority(self):
        ticket=self.ticket();item,factory=self.make()
        self.f.add(execution_fixtures.PNG,'image/png')
        extra=self.store.files.buffer_lease(tuple(self.f.sources));self.leases.append(extra)
        other=execution_fixtures.ExecutionTests();other.setUp()
        try:
            other.add();foreign=other.store.files.buffer_lease(tuple(other.sources))
            with mock.patch.object(self.f.l,'consume',wraps=self.f.l.consume) as consume:
                for lease in (extra,foreign):
                    self.rejected('buffer_lease_invalid',lambda lease=lease:item.call(ticket[3],self.f.l,ticket[2],lease,lambda:KEY,time.monotonic()+1))
                self.assertEqual(consume.call_count,0)
            foreign.close()
        finally:other.tearDown()
        self.assertEqual(factory.calls,[])

    def test_four_timed_out_workers_keep_capacity_until_actual_exit(self):
        class HeldResponse(FakeResponse):
            def read(self, size):
                self.entered.set()
                self.gate.wait()  # Only test cleanup may release this worker.
                return super().read(size)

        tickets=[];connections=[];gates=[]
        for _ in range(4):
            tickets.append(self.ticket())
            gate=threading.Event();gates.append(gate);self.release_events.append(gate)
            connections.append(FakeConnection(HeldResponse(b'late',gate=gate)))
        pending=list(connections);calls=[]
        def connect(*args,**kwargs):
            calls.append((args,kwargs));return pending.pop(0)
        stamp=[time.monotonic()]
        item=transport.DirectDocumentTransport(enabled=True,connect=connect,clock=lambda:stamp[0],
            _test_qualification=lambda *a:None)
        self.transports.append(item)
        callers=[]
        try:
            for ticket,connection in zip(tickets,connections):
                results=[];errors=[];deadline=stamp[0]+.1
                def call(ticket=ticket,deadline=deadline,results=results,errors=errors):
                    try:
                        results.append(item.call(ticket[3],self.f.l,ticket[2],ticket[4],lambda:KEY,deadline))
                    except BaseException as error:
                        errors.append(error)
                caller=threading.Thread(target=call,daemon=True);callers.append(caller)
                caller.start()
                self.assertTrue(connection.response.entered.wait(10),'Worker did not reach the held response read.')
                stamp[0]=deadline+1
                caller.join(10)
                self.assertFalse(caller.is_alive(),'Caller did not return after its controlled deadline.')
                self.assertEqual(errors,[])
                self.assertEqual(len(results),1)
                result=results[0]
                self.assertEqual(result.state,'unknown')
                self.assertTrue(result.consumed);self.assertTrue(result.dispatch_started)
                self.assertFalse(connection.response.closed)
                self.assertFalse(connection.response.gate.is_set())
            self.assertEqual(len(calls),4)
            with item._condition:
                self.assertEqual(len(item._jobs),4)
                self.assertTrue(all(job.thread.is_alive() and not job.exited.is_set() for job in item._jobs))
            self.busy()
            # A different store has a free buffer bundle, isolating worker capacity
            # from the independent per-store four-bundle buffer limit.
            other=execution_fixtures.ExecutionTests();other.setUp()
            try:
                other.add();lease=other.store.files.buffer_lease(tuple(other.sources))
                request=other.prepare();state,permit,prepared=other.claim(request)
                self.rejected('worker_capacity',lambda:item.call(prepared,other.l,permit,lease,lambda:KEY,stamp[0]+1))
                lease.close()
            finally:other.tearDown()
        finally:
            for gate in gates:gate.set()
            for caller in callers:caller.join(10)
            item.clock=time.monotonic
            item.close(timeout=2)
        self.assertFalse(self.store.files.io_active())

    def test_buffer_bundle_cap_and_tls_key_logging_are_independently_bounded(self):
        self.f.add();fid=self.f.sources[0]
        for _ in range(4):self.leases.append(self.store.files.buffer_lease((fid,)))
        with self.assertRaises(AttachmentError):self.store.files.buffer_lease((fid,))
        logfile=self.f.f.root/'fictional-keylog.txt'
        with mock.patch.dict(os.environ,{'SSLKEYLOGFILE':str(logfile)}):
            context=REAL_TLS_CONTEXT()
        self.assertTrue(context.check_hostname);self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertIsNone(context.keylog_filename);self.assertFalse(logfile.exists())

    def test_invalid_utf8_and_incomplete_non200_never_become_validated_answer(self):
        ticket=self.ticket();item,factory=self.make(FakeConnection(FakeResponse(b'\xff')))
        result=self.call(item,ticket)
        with result:
            with self.assertRaises(documents.DocumentRejected):
                documents.validate_response(self.f.p,ticket[3],result.raw_response)
        self.assertFalse(self.store.files.io_active())
        ticket=self.ticket();reply=FakeResponse(b'error',status=400);reply.length+=1
        item,factory=self.make(FakeConnection(reply));result=self.call(item,ticket)
        self.assertEqual((result.state,result.http_status,result.error),('unknown',None,'response_incomplete'))
        self.assertEqual(len(factory.connection.requests),1)

    def test_actual_uncertain_close_holds_are_isolated_until_process_exit(self):
        with tempfile.TemporaryDirectory(prefix='officer-transport-fault-') as root:
            env=dict(os.environ,TMPDIR=root,PYTHONPATH=os.pathsep.join((str(Path(__file__).parent),str(Path(__file__).parent.parent/'src'))))
            for mode in ('connection','source'):
                code='import test_officer_document_transport as t;t.uncertain_close_child('+repr(mode)+')'
                run=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,timeout=60 if os.name=='nt' else 5)
                self.assertEqual(run.returncode,0,run.stderr.decode())
                self.assertEqual(run.stdout,b'')

    @unittest.skipUnless(POSIX, POSIX_WHY)       # reads object identities through POSIX's objects descriptor
    def test_fifty_existing_sources_pin_atomically_without_resource_lock_expansion(self):
        ids=tuple(f'{100000+i:032x}' for i in range(50))
        for source_id in ids:
            fd=self.store.files.create(source_id)
            try:os.write(fd,b'x')
            finally:os.close(fd)
        lease=self.store.files.buffer_lease(ids);self.leases.append(lease)
        self.assertEqual(lease.source_ids,tuple(sorted(ids)))
        self.assertEqual(self.store.files.remove(ids[-1],None),'busy')
        self.busy();lease.close()
        self.assertFalse(self.store.files.io_active())
        # These are isolated PrivateFiles fixture entries, not logical sources.
        for source_id in ids:
            stat=os.stat(source_id,dir_fd=self.store.files.objects_fd,follow_symlinks=False)
            expected=[stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]
            self.assertEqual(self.store.files.remove(source_id,expected,hashlib.sha256(b'x').hexdigest()),'removed')
        self.assertEqual(self.store.storage()['usedBytes'],0)

    def test_retire_permit_is_nonblocking_registry_only_and_irreversible(self):
        ticket=self.ticket();permit=ticket[2]
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('no retirement IO')),\
             mock.patch.object(self.store,'_gate',side_effect=AssertionError('no retirement store lock')):
            self.assertFalse(self.f.l.retire_permit(object()))
            self.assertTrue(self.f.l.retire_permit(permit))
            self.assertTrue(self.f.l.retire_permit(permit))
        self.f.error('document_permit_invalid',lambda:self.f.l.consume(permit,self.f.selection,ticket[0]['id'],ticket[3].body_digest,ticket[1]['profileDigest']))


def uncertain_close_child(mode):
    # Deliberately unrecoverable ownership is tested in a disposable process.
    # There is no production reset, forced close or registry-clearing shortcut.
    fixture=execution_fixtures.ExecutionTests();fixture.setUp();fixture.add()
    lease=fixture.store.files.buffer_lease(tuple(fixture.sources))
    request=fixture.prepare();state,permit,prepared=fixture.claim(request)
    if mode=='connection':
        connection=FakeConnection(close_fails=True)
        item=transport.DirectDocumentTransport(enabled=True,connect=Factory(connection),_test_qualification=lambda *a:None)
        result=item.call(prepared,fixture.l,permit,lease,lambda:KEY,time.monotonic()+2)
        assert result.error=='cleanup_unconfirmed'
        assert fixture.store.files.io_active()
        try:item.close(timeout=0)
        except transport.TransportRejected:pass
        else:raise AssertionError('uncertain connection was treated as complete')
    else:
        source=fixture.sources[0]
        row=fixture.store.db.execute('SELECT * FROM files WHERE id=?',(source,)).fetchone()
        reading,closing=os.read,os.close;held=[]
        def read(fd,count):
            if not held:held.append(fd)
            return reading(fd,count)
        def close(fd):
            if held and fd==held[0]:raise OSError('fictional source close failure')
            return closing(fd)
        with mock.patch('attachments_files.os.read',side_effect=read),mock.patch('attachments_files.os.close',side_effect=close):
            try:fixture.store.files.read(source,json.loads(row['identity']),row['digest'],1024)
            except OSError:pass
            else:raise AssertionError('genuine close failure was hidden')
        assert os.fstat(held[0]).st_nlink==1
        lease.close()
        assert fixture.store.files.io_active()
        assert fixture.store.files.remove(source,json.loads(row['identity']),row['digest'])=='busy'
        try:fixture.store.files.buffer_lease((source,))
        except AttachmentError:pass
        else:raise AssertionError('uncertain descriptor admitted a new buffer')
    try:fixture.store.close()
    except AttachmentError:pass
    else:raise AssertionError('uncertain ownership did not pin shutdown')
    # Process exit is the actual lifetime boundary; parent removes its fixture.
    os._exit(0)


@unittest.skipUnless(STORE, STORE_WHY)
class QualifiedTransportTests(unittest.TestCase):
    def setUp(self):
        import test_officer_document_policy as policy_fixtures
        self.f=policy_fixtures.QualifiedLedgerTests();self.f.setUp()
        self.clock=[self.f.store.clock()]
        self.f.store.clock=lambda:self.clock[0]
        self.f.l.register_scope(self.f.scope,self.f.profile,self.f.price)
        self.result=None;self.release=threading.Event();self.leases=[]
    def tearDown(self):
        self.release.set()
        if self.result is not None:
            try:self.result.close()
            except transport.TransportRejected:pass
        if hasattr(self,'item'):self.item.close(timeout=3)
        for lease in self.leases:
            try:lease.close()
            except AttachmentError:pass
        self.f.tearDown()
    def ticket(self):
        d=self.f.f.draft();u,_=self.f.f.upload(d);self.f.f.admit(d)
        lease=self.f.store.files.buffer_lease((u['fileId'],));self.leases.append(lease)
        request=self.f.l.prepare(self.f.f.selection,self.f.profile,self.f.scope.id,self.f.f.key(),'Read the fictional file.')
        state,permit,prepared=self.f.l.claim(self.f.f.selection,request['id'])
        return request,state,permit,prepared,lease
    def test_default_closed_profile_can_only_reach_fake_connection_when_current(self):
        ticket=self.ticket();factory=Factory()
        self.item=transport.DirectDocumentTransport(enabled=True,connect=factory,wall=lambda:self.clock[0])
        self.result=self.item.call(ticket[3],self.f.l,ticket[2],ticket[4],lambda:KEY,time.monotonic()+2)
        self.assertIs(type(self.result),transport.OwnedDocumentResult)
        self.assertEqual(len(factory.connection.requests),1)
        self.result.close();self.result=None
    def test_expired_default_profile_has_zero_consume_or_connection_calls(self):
        import officer_document_policy as policy
        ticket=self.ticket();factory=Factory()
        self.clock[0]=policy.VALID_UNTIL
        self.item=transport.DirectDocumentTransport(enabled=True,connect=factory,wall=lambda:self.clock[0])
        with mock.patch.object(self.f.l,'consume',wraps=self.f.l.consume) as consume:
            with self.assertRaises(transport.TransportRejected) as caught:
                self.item.call(ticket[3],self.f.l,ticket[2],ticket[4],lambda:KEY,time.monotonic()+2)
            self.assertEqual(caught.exception.code,'qualification_expired')
            self.assertEqual(consume.call_count,0)
        self.assertEqual(factory.calls,[])

    def test_qualified_expiry_crossed_during_connect_prevents_post(self):
        import officer_document_policy as policy
        ticket=self.ticket();connection=FakeConnection(connect_gate=self.release);factory=Factory(connection)
        self.item=transport.DirectDocumentTransport(enabled=True,connect=factory,wall=lambda:self.clock[0])
        outcomes=[]
        caller=threading.Thread(target=lambda:outcomes.append(self.item.call(ticket[3],self.f.l,ticket[2],ticket[4],lambda:KEY,time.monotonic()+2)))
        caller.start()
        self.assertTrue(connection.connect_entered.wait(1))
        self.clock[0]=policy.VALID_UNTIL
        self.release.set();caller.join(2)
        self.assertFalse(caller.is_alive());self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0].error,'qualification_expired')
        self.assertEqual(connection.requests,[])
        self.assertFalse(self.f.store.files.io_active())
        self.assertEqual(self.f.l.status(self.f.f.selection,ticket[0]['id'])['state'],'claimed')


if __name__=='__main__':unittest.main()
