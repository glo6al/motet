"""Disposable native handoff fixture: real stores, workspace and OS resource locks."""
from contextlib import contextmanager
import io
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_materialize import (DirectoryIdentity, FileIdentity, Materializer, WorkspaceContext,
                                     WorkspaceDirectories, copy_identity)
from attachments_native_handoff import FictionalAdapterBinding, OfflineNativeHandoff
from attachments_native_mirror import NativeOutcomeMirror
from attachments_owner_transport import OwnerAttachmentLedger
from attachments_requests import RequestLedger, TransportProfile
from attachments_resources import ResourceLocks
from attachments_store import AttachmentStore
from attachments_types import canonical
from test_attachments_foundation import BINDING, EPOCH, fence, metadata
from test_attachments_owner_transport import owner_schema

GIT = shutil.which('git')
CONTENT = b'Fictional orchard notes.\n'
QUESTION = 'Review the fictional orchard note.'
TURN = 'fictional-turn'
_CONNECT = sqlite3.connect


class FaultConnection(sqlite3.Connection):
    commit_fault = None
    update_fault = None

    def execute(self, sql, parameters=()):
        if self.update_fault is not None and sql.startswith(self.update_fault):
            self.update_fault = None
            raise sqlite3.OperationalError('Fictional statement failure.')
        return super().execute(sql, parameters)

    def commit(self):
        fault, self.commit_fault = self.commit_fault, None
        if fault == 'before':
            raise sqlite3.OperationalError('Fictional commit failure.')
        super().commit()
        if fault == 'after':
            raise sqlite3.OperationalError('Fictional lost commit return.')


class AuditedLock:
    def __init__(self, original, other):
        self.original, self.other = original, other

    def __enter__(self):
        assert not self.other()._is_owned(), 'Both store locks must never be nested.'
        return self.original.__enter__()

    def __exit__(self, *args):
        return self.original.__exit__(*args)

    def _is_owned(self):
        return self.original._is_owned()


class NativeFixture:
    def __init__(self, test, *, adapter='codex'):
        self.test = test
        temp = tempfile.TemporaryDirectory(prefix='native-attachment-fixture-')
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo, self.state, self.lockroot = (self.root / name for name in ('orchard', 'state', 'locks'))
        for path in (self.repo, self.state, self.lockroot):
            path.mkdir(mode=0o700)
        self.git('init', '-q')
        self.git('-c', 'user.name=Fictional', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '--allow-empty', '-m', 'Fictional orchard')
        git_dir = self.repo / '.git'
        exe = Path(GIT).resolve()
        self.context = WorkspaceContext('b' * 64, 'c' * 64, str(self.repo), DirectoryIdentity.of(self.repo.stat()),
                                        str(git_dir), DirectoryIdentity.of(git_dir.stat()), str(git_dir / 'info'),
                                        DirectoryIdentity.of((git_dir / 'info').stat()), str(exe), FileIdentity.of(exe.stat()))
        self.binding = dict(BINDING, id=('x:' if adapter == 'codex' else 'c:') + str(uuid.uuid4()))
        self.profile = TransportProfile('fictional-profile', 'v1', adapter, self.binding['capabilityRevision'],
                                        self.context.fingerprint, self.context.workspace_fingerprint,
                                        'fictional-qualification', ('text/plain',))
        self.adapter_binding = FictionalAdapterBinding(canonical(self.binding), self.profile, self.context)
        self.owner_lock, self.store, self.owner_db = threading.RLock(), None, None
        self.resources = ResourceLocks(str(self.lockroot), enabled=True)
        self.serial = 0
        self.calls = []
        test.addCleanup(self.resources.close)
        test.addCleanup(self.close_owner)
        test.addCleanup(self.close_store)
        self.open_store(mirror=False)
        self.open_owner(initial=True)

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        subprocess.run([GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                        '-C', str(self.repo), *args], env=env, capture_output=True, timeout=5, check=True)

    def close_store(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def close_owner(self):
        if self.owner_db is not None:
            self.owner_db.close()
            self.owner_db = None

    def open_store(self, *, mirror=True, epoch=EPOCH):
        self.close_store()
        with patch.object(sqlite3, 'connect', side_effect=lambda *a, **k: _CONNECT(*a, factory=FaultConnection, **k)):
            self.store = AttachmentStore(self.state, enabled=True, owner_epoch=epoch)
        self.store.lock = AuditedLock(self.store.lock, lambda: self.owner_lock)
        self.requests = RequestLedger(self.store)
        self.materializer = Materializer(enabled=True, validate_authority=self.validate_authority,
                                         record_created=self.requests.record_copy_created,
                                         record_directory=self.requests.record_directory_created,
                                         record_partial=self.requests.record_partial)
        self.mirror = NativeOutcomeMirror(self.requests) if mirror else None

    def open_owner(self, *, initial=False, maintenance=False):
        self.close_owner()
        self.owner_db = _CONNECT(self.root / 'fictional-owner.sqlite3', factory=FaultConnection, check_same_thread=False)
        if initial:
            owner_schema(self.owner_db)
        self.owner_lock = AuditedLock(threading.RLock(), lambda: self.store.lock if self.store else threading.RLock())
        kwargs = {}
        if maintenance:
            from attachments_owner_guards import OwnerAttachmentGuard
            from test_attachments_owner_guards import install
            install(self.owner_db)
            kwargs = {'guard': OwnerAttachmentGuard(self.owner_db, self.owner_lock), 'maintenance_only': True}
        self.owner = OwnerAttachmentLedger(self.owner_db, self.owner_lock, **kwargs)

    def validate_authority(self, authority, phase, copies):
        self.test.assertFalse(self.store.lock._is_owned())
        self.test.assertFalse(self.owner_lock._is_owned())
        return self.requests.authority(authority.request_id) == authority

    def mount(self):
        self.mirror = NativeOutcomeMirror(self.requests)
        return self.mirror

    def key(self):
        self.serial += 1
        return f'fictional-native-{self.serial:06}'

    def draft(self):
        store = self.store
        d = store.create_draft(self.binding, self.key())
        d['revision'] = store.edit_text(d['id'], d['capability'], fence(d), QUESTION)['revision']
        upload = store.reserve_upload(d['id'], d['capability'], fence(d), metadata(CONTENT), self.key())
        d['revision'] = upload['draftRevision']
        d['revision'] = store.receive_upload(d['id'], d['capability'], fence(d), upload['id'],
                                           upload['fingerprint'], io.BytesIO(CONTENT))['draftRevision']
        return d

    def prepare(self, *, mirror=True, draft=None, key=None):
        d = self.draft() if draft is None else draft
        ledger = self.mirror if mirror else self.requests
        return ledger.prepare_request(d['id'], d['capability'], fence(d), key or self.key(), self.binding, self.profile)

    def authorize(self, r, lease, *, mirror=True):
        ledger = self.mirror if mirror else self.requests
        return ledger.authorize_send(r['id'], r['capability'], r['revision'], r['fingerprint'],
                                     resolved_binding=self.binding, transport_profile=self.profile, resource_lease=lease)

    def status(self, r):
        return self.requests.request_status(r['id'], r['capability'])

    @contextmanager
    def hold(self, r):
        with self.resources.hold(self.requests.resource_keys(r['id'])) as lease:
            yield lease

    def materialize(self, r=None, *, mirror=True):
        r = self.prepare(mirror=mirror) if r is None else r
        rid = r['id']
        with self.hold(r) as lease:
            self.authorize(r, lease, mirror=mirror)
            authority = self.requests.authority(rid)
            directories = self.materializer.prepare_directories(self.context, authority, lease)
            copy_id = self.status(r)['copies'][0]['id']
            copy = self.requests.claim_copy(rid, authority.operation_revision, copy_id, lease)
            authority = self.requests.authority(rid)
            self.materializer.ensure_exclusion(self.context, authority, (copy,), lease)
            with self.requests.source_handle(copy.id, lease) as fd:
                source = copy_identity(FileIdentity.of(os.fstat(fd)))
                result = self.materializer.materialize(self.context, authority, copy, fd, source, lease,
                                                       directories=directories)
            self.requests.record_copy_result(result, lease)
        self.test.assertEqual(self.status(r)['state'], 'materialized')
        self.test.assertEqual((self.repo / copy.relative_path).read_bytes(), CONTENT)
        return r, (result,), self.repo / copy.relative_path

    def boundary(self, admission, attempt):
        self.test.assertFalse(self.owner_lock._is_owned())
        self.test.assertFalse(self.store.lock._is_owned())
        self.test.assertFalse(self.owner_db.in_transaction)
        self.test.assertFalse(self.store.db.in_transaction)
        self.calls.append((admission, attempt))

    def coordinator(self, boundary=None, *, enabled=True):
        return OfflineNativeHandoff(self.requests, self.owner, self.mirror, self.resources, self.materializer,
                                    self.adapter_binding, boundary or self.boundary, enabled=enabled)

    def handed_off(self):
        r, saved, copy = self.materialize()
        self.test.assertTrue(self.coordinator().handoff(r['id'], saved))
        self.test.assertEqual(len(self.calls), 1)
        admission, attempt = self.calls[0]
        expected = self.mirror._expected(r['id'])
        self.test.assertEqual(expected.admission, admission)
        self.test.assertEqual(self.owner.status(admission.owner_request_id, expected.digest)['attempt_id'], attempt)
        return r, saved, copy

    def facts(self, r):
        return self.mirror._as_of(r['id'])

    def owner_status(self, r):
        return self.owner.status(r['ownerRequestId'])

    def owner_pair(self, r):
        return self.owner._exact_snapshot(r['ownerRequestId'], self.mirror._expected(r['id']).digest)

    def event(self, r, kind, **kwargs):
        status = self.owner_status(r)
        source_ordinal = status['outcome']['evidence_sequence'] + 1
        return self.owner.record_outcome(r['ownerRequestId'], status['admission_digest'], status['attempt_id'], kind,
                                         evidence_sequence=source_ordinal,
                                         now_ms=int(time.time() * 1000) + source_ordinal, **kwargs)

    def mirror_row(self, r):
        return self.store.db.execute('SELECT slot FROM native_outcome_mirror WHERE request_id=?', (r['id'],)).fetchone()

    def outbox(self, r):
        return dict(self.store.db.execute('SELECT * FROM attachment_outbox WHERE request_id=?', (r['id'],)).fetchone())

    def dirs(self, r):
        return WorkspaceDirectories.from_record(self.requests.workspace_directories(r['id']))
