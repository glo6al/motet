"""One bounded, unwired Officer document request; no route or retry authority.

Production requires the reviewed document ledger and its opaque one-use permit.
Tests inject fake connections/ledgers explicitly. A timeout never proves worker,
socket or buffer completion. Raw responses exist only in an owned result scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import http.client
import math
import socket
import ssl
import threading
import time

import officer_documents as documents
from attachments_catalogue import HistorySelection
from attachments_files import PrivateFiles
from attachments_types import AttachmentError

HOST, PORT, PATH = 'api.anthropic.com', 443, '/v1/messages'
SAFE_ERRORS = frozenset(('transport_disabled', 'deadline_invalid', 'credential_unavailable', 'ledger_unavailable',
    'request_invalid', 'transport_closed', 'transport_busy', 'worker_capacity', 'permit_already_attempted', 'buffer_lease_invalid',
    'result_closed', 'response_invalid', 'response_bound', 'response_incomplete', 'connection_unavailable',
    'permit_unavailable', 'transport_unconfirmed', 'startup_unconfirmed', 'deadline_or_abandoned',
    'cleanup_unconfirmed', 'source_withdrawn', 'http_error', 'qualification_unavailable', 'qualification_expired'))


class TransportRejected(RuntimeError):
    def __init__(self, code):
        self.code = code if code in SAFE_ERRORS else 'transport_unconfirmed'
        super().__init__(self.code)


def _reject(code):
    raise TransportRejected(code) from None


def _tls_context():
    # create_default_context consults SSLKEYLOGFILE. Construct directly so even
    # context creation cannot open that file or enable TLS key logging.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.keylog_filename = None
    return context


def _connect(host, port, *, timeout, context):
    return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=context)


@dataclass(frozen=True)
class TransportFailure:
    state: str
    error: str
    http_status: int | None = None
    consumed: bool = False
    dispatch_started: bool = False


class OwnedDocumentResult:
    """Memory-only HTTP response, not a validated answer or publication permit.

    Callers must drop their borrowed raw/candidate/body references before close.
    Python cannot recall detached byte copies made by a cooperating caller.
    """
    __slots__ = ('_job', '_lease', '_raw', '_closed', '_lock')

    def __init__(self, job, lease, raw):
        self._job, self._lease, self._raw = job, lease, raw
        self._closed, self._lock = False, threading.Lock()

    @property
    def http_status(self):
        return 200

    @property
    def consumed(self):
        return True

    @property
    def dispatch_started(self):
        return True

    @property
    def raw_response(self):
        with self._lock:
            if self._closed or self._job.result_owner is not self:
                _reject('result_closed')
            return self._raw

    def close(self):
        with self._lock:
            if self._closed or self._job.result_owner is not self:
                _reject('result_closed')
            self._raw = None
            self._lease.close()
            self._lease = None
            self._closed = True
        with self._job.lock:
            self._job.released = True
        self._job.transport._notify()

    def __enter__(self):
        self.raw_response
        return self

    def __exit__(self, *args):
        self.close()


class _Abandoned(Exception):
    pass


class _Job:
    def __init__(self, transport, prepared, ledger, permit, credential, deadline, selection):
        self.transport, self.prepared, self.ledger, self.permit = transport, prepared, ledger, permit
        self.credential, self.deadline, self.selection = credential, deadline, selection
        self.files = ledger.store.files
        self.request_id = prepared.complete_manifest.selection.request_id
        self.body_digest = prepared.body_digest
        self.profile_digest = documents.profile_binding(prepared.complete_manifest.profile)
        self.lock, self.done, self.entered, self.exited = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
        self.claim = self.thread = self.connection = self.sock = None
        self.abandoned = self.dispatch_started = self.consumed = self.retired = self.released = False
        self.uncertain = self.never_started = False
        self.outcome = self.result_owner = self.uncertain_response = None

    def _live(self):
        with self.lock:
            if self.abandoned or self.transport.clock() >= self.deadline:
                raise _Abandoned()

    def retire(self):
        # The reviewed ledger method is registry-only/nonblocking, never IO.
        try:
            confirmed = self.ledger.retire_permit(self.permit) is True
        except BaseException:
            confirmed = False
        with self.lock:
            self.retired = self.retired or confirmed
        return confirmed

    def abandon(self):
        with self.lock:
            self.abandoned = True
            sock = self.sock
            result = self.outcome if type(self.outcome) is OwnedDocumentResult else None
            self.outcome = None
        self.retire()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except BaseException:
                # Shutdown failure is not completion; verified worker close may
                # still establish cleanup. No caller-side connection close/retry.
                pass
        if result is not None:
            result.close()

    def _read(self, response):
        raw = bytearray()
        while True:
            self._live()
            chunk = response.read(min(65536, documents.MAX_RESPONSE_BYTES + 1 - len(raw)))
            if type(chunk) is not bytes:
                _reject('response_invalid')
            raw.extend(chunk)
            if len(raw) > documents.MAX_RESPONSE_BYTES:
                _reject('response_bound')
            if not chunk:
                if getattr(response, 'length', None) not in (None, 0):
                    _reject('response_incomplete')
                break
        return bytes(raw)

    def run(self):
        response = connection = raw = key = outcome = None
        clean, owns_worker = True, False
        with self.lock:
            if self.entered.is_set():
                return  # An injected/copy target cannot run this job twice.
            self.entered.set()
        try:
            self.files._buffer_worker_enter(self.claim)
            owns_worker = True
            self._live()
            try:
                key = self.credential()
            except BaseException:
                _reject('credential_unavailable')
            if type(key) is not str or not 1 <= len(key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in key):
                _reject('credential_unavailable')
            self._live()
            self.transport._qualify(self.prepared.complete_manifest.profile)
            check_revocation = self.transport._test_ledger_type is None
            try:
                consumed=self.ledger.consume(self.permit, self.selection, self.request_id, self.body_digest, self.profile_digest)
            except AttachmentError:
                if check_revocation and self.ledger.settle_unconsumed_source_change(
                        self.permit,self.selection,self.request_id,self.body_digest,self.profile_digest):
                    _reject('source_withdrawn')
                raise
            if consumed is not True:
                _reject('permit_unavailable')
            self.consumed = True
            self.files._buffer_worker_ready(self.claim)
            self._live()
            context = _tls_context()
            self._live()
            connection = self.transport.connect(HOST, PORT,
                timeout=max(.001, self.deadline - self.transport.clock()), context=context)
            with self.lock:
                self.connection = connection
            connection.set_debuglevel(0)
            connection.auto_open = False
            self._live()
            connection.connect()
            with (self.ledger.store._gate() if check_revocation else nullcontext()), self.lock:
                self.sock = connection.sock
                if self.sock is None:
                    _reject('connection_unavailable')
                # The only dispatch linearization. Never hold this lock across
                # request(): abandonment before this transition forbids POST.
                if self.abandoned or self.transport.clock() >= self.deadline:
                    raise _Abandoned()
                if check_revocation and not self.ledger.dispatch_ready(
                        self.selection,self.request_id,self.body_digest,self.profile_digest):
                    _reject('source_withdrawn')
                self.transport._qualify(self.prepared.complete_manifest.profile)
                if self.abandoned or self.transport.clock() >= self.deadline:
                    raise _Abandoned()
                self.dispatch_started = True
            connection.request('POST', PATH, body=self.prepared.body_bytes,
                headers={**dict(documents.HEADERS), 'x-api-key': key})
            response = connection.getresponse()
            status = response.status
            if type(status) is not int or not 100 <= status <= 599:
                _reject('response_invalid')
            raw = self._read(response)
            if status != 200:
                raw = None
                outcome = TransportFailure('failed', 'http_error', status, True, True)
        except _Abandoned:
            outcome = TransportFailure('unknown', 'deadline_or_abandoned', consumed=self.consumed,
                                       dispatch_started=self.dispatch_started)
        except TransportRejected as error:
            outcome = TransportFailure('unknown', error.code, consumed=self.consumed, dispatch_started=self.dispatch_started)
        except BaseException:
            outcome = TransportFailure('unknown', 'transport_unconfirmed', consumed=self.consumed,
                                       dispatch_started=self.dispatch_started)
        finally:
            # A failed close can leave a live TLS/file wrapper. Never infer
            # completion from sock=None, EOF, a timeout or a raised exception.
            if response is not None:
                try:
                    response.close()
                except BaseException:
                    clean = False
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    clean = False
            key = None
            self.prepared = self.credential = None
            if not self.consumed:
                self.retire()
            try:
                with self.lock:
                    self.uncertain = not owns_worker or not clean or not (self.consumed or self.retired)
                    if clean:
                        self.connection = self.sock = None
                    if self.uncertain:
                        if owns_worker:
                            self.files._uncertain_worker_buffer(self.claim)
                        self.outcome = TransportFailure('unknown', 'cleanup_unconfirmed', consumed=self.consumed,
                                                        dispatch_started=self.dispatch_started)
                        # Hold exact uncertain resources, never a reset/fd retry.
                        self.connection, self.uncertain_response = connection, response
                    elif not self.abandoned and self.transport.clock() < self.deadline and raw is not None and outcome is None:
                        lease = self.files._buffer_result(self.claim)
                        self.outcome = OwnedDocumentResult(self, lease, raw)
                        self.result_owner = self.outcome
                        # Transfer the sole worker-owned payload reference while
                        # still locked. Abandonment can close the new owner as
                        # soon as this lock is released.
                        raw = response = connection = None
                    else:
                        # Verified close is not buffer disposal. Drop all local
                        # payload/resource references before physical unpinning.
                        raw = response = connection = None
                        self.files._release_worker_buffer(self.claim)
                        self.released = True
                        if not self.abandoned:
                            self.outcome = outcome or TransportFailure('unknown', 'deadline_or_abandoned',
                                consumed=self.consumed, dispatch_started=self.dispatch_started)
            except BaseException:
                with self.lock:
                    self.uncertain = True
                    self.outcome = TransportFailure('unknown', 'cleanup_unconfirmed', consumed=self.consumed,
                                                    dispatch_started=self.dispatch_started)
            raw = response = connection = outcome = None
            self.exited.set()
            self.done.set()
            self.transport._notify()


class DirectDocumentTransport:
    def __init__(self, *, enabled=False, demo=False, connect=_connect, clock=time.monotonic,
                 wall=time.time, _thread_factory=None, _test_ledger_type=None, _test_qualification=None):
        self.enabled = enabled is True and demo is False
        self.connect, self.clock, self.wall = connect, clock, wall
        self._test_qualification = _test_qualification
        self._thread_factory = _thread_factory or threading.Thread
        self._native_thread_factory = _thread_factory is None
        self._test_ledger_type = _test_ledger_type
        self._condition, self._jobs, self._accepting = threading.Condition(), set(), True

    def _qualify(self, profile):
        if self._test_qualification is not None:
            self._test_qualification(profile, self.wall)
            return
        try:
            import officer_document_policy as policy
        except ImportError:
            _reject('qualification_unavailable')
        if profile != policy.input_profile():
            _reject('qualification_unavailable')
        try:
            policy.current(self.wall)
        except AttachmentError:
            _reject('qualification_expired')

    def _notify(self):
        with self._condition:
            self._condition.notify_all()

    def _reap(self):
        # Worker entry/exit events plus actual thread completion, never merely
        # is_alive=False or a Thread.start exception, establish the boundary.
        for job in tuple(self._jobs):
            ended = job.never_started or (job.entered.is_set() and job.exited.is_set()
                and job.thread is not None and not job.thread.is_alive())
            if ended and job.released:
                self._jobs.remove(job)

    def call(self, prepared_request, ledger, permit, buffer_lease, credential, deadline):
        if not self.enabled:
            _reject('transport_disabled')
        now = self.clock()
        # Compared as ``deadline <= now + 120``, never ``deadline - now <= 120``: a caller's ``clock() + 120`` read in
        # the same clock tick then compares equal. The subtraction can round to just above 120 whenever ``now + 120``
        # crosses a power of two, and a coarse clock makes the same tick likely (time.monotonic on Windows before
        # Python 3.13 ticks every 15.6 ms), so a valid deadline was refused there at random after the claim.
        if (type(now) not in (int, float) or not math.isfinite(now)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not now < deadline <= now + 120):
            _reject('deadline_invalid')
        if not callable(credential):
            _reject('credential_unavailable')
        if self._test_ledger_type is None:
            try:
                from officer_document_ledger import OfficerDocumentLedger
                ledger_type = OfficerDocumentLedger
            except ImportError:
                _reject('ledger_unavailable')
        else:
            ledger_type = self._test_ledger_type
        if (type(ledger) is not ledger_type or type(getattr(getattr(ledger, 'store', None), 'files', None)) is not PrivateFiles
                or not callable(getattr(ledger, 'consume', None)) or not callable(getattr(ledger, 'retire_permit', None))):
            _reject('ledger_unavailable')
        try:
            profile = prepared_request.complete_manifest.profile
            manifest = documents._prepared(profile, prepared_request)
            self._qualify(profile)
            ids = tuple(dep.source_id for dep in manifest.dependencies)
            first = manifest.dependencies[0]
            selection = HistorySelection('officer', first.destination_id, first.scope_id, first.conversation_id)
        except (AttributeError, IndexError, documents.DocumentRejected, AttachmentError):
            _reject('request_invalid')
        job = _Job(self, prepared_request, ledger, permit, credential, deadline, selection)
        with self._condition:
            self._reap()
            if not self._accepting:
                _reject('transport_closed')
            if len(self._jobs) >= 4:
                _reject('worker_capacity')
            if any(active.permit is permit for active in self._jobs):
                _reject('permit_already_attempted')
            self._jobs.add(job)
        try:
            job.claim = job.files._reserve_buffer_handoff(buffer_lease, ids, job)
        except AttachmentError:
            with self._condition:
                self._jobs.remove(job)
            _reject('buffer_lease_invalid')
        prepared_request = buffer_lease = credential = ledger = permit = None
        try:
            job.thread = self._thread_factory(target=job.run, daemon=True, name='officer-document')
        except BaseException:
            job.abandon()
            if self._native_thread_factory and job.retired and not job.entered.is_set():
                job.prepared = job.credential = None
                job.files._release_local_buffer(job.claim)
                job.released, job.never_started = True, True
            return TransportFailure('unknown', 'startup_unconfirmed')
        try:
            job.files._buffer_start_attempt(job.claim)
            job.thread.start()
        except BaseException:
            job.abandon()
            return TransportFailure('unknown', 'startup_unconfirmed', consumed=job.consumed,
                                    dispatch_started=job.dispatch_started)
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                job.abandon()
                return TransportFailure('unknown', 'deadline_or_abandoned', consumed=job.consumed,
                                        dispatch_started=job.dispatch_started)
            if job.done.wait(remaining):
                with job.lock:
                    if job.abandoned or self.clock() >= deadline:
                        result = None
                    else:
                        result = job.outcome
                        job.outcome = None
                if result is not None:
                    return result
                job.abandon()
                return TransportFailure('unknown', 'deadline_or_abandoned', consumed=job.consumed,
                                        dispatch_started=job.dispatch_started)

    def close(self, timeout=0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 120:
            _reject('deadline_invalid')
        deadline = self.clock() + timeout
        with self._condition:
            self._accepting = False
            while True:
                self._reap()
                if not self._jobs:
                    return
                remaining = deadline - self.clock()
                if remaining <= 0:
                    _reject('transport_busy')
                self._condition.wait(min(.05, remaining))
