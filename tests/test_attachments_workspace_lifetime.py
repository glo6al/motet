"""Physical workspace closure with real stores, descriptors and fresh processes."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from attachments_native_fixture import CONTENT, GIT, NativeFixture
from attachments_files import _MAX_WORKSPACE_PINS, _WORKSPACE_PINS
from attachments_materialize import (DirectoryIdentity, FileIdentity, Materializer, WorkspaceContext,
                                     WorkspaceDirectories, _WORKSPACE_SCOPE, copy_identity)
from attachments_requests import RequestLedger, VerifiedCopy
from attachments_resources import ResourceLocks
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
from test_attachments_foundation import EPOCH


def qualified(case, *, checkpoint=None):
    return Materializer(enabled=True, workspace_files=case.store.files,
                        validate_authority=case.validate_authority,
                        record_created=case.requests.record_copy_created,
                        record_directory=case.requests.record_directory_created,
                        record_partial=case.requests.record_partial, checkpoint=checkpoint)


def owned_copy(case, request):
    rid = request['id']
    with case.store._gate():
        row = case.requests._copies(rid)[0]
        return case.requests._intent(row)


def materialize_neighbor(case, retained_root):
    r = case.prepare()
    rid = r['id']
    with case.hold(r) as lease:
        case.authorize(r, lease)
        auth = case.requests.authority(rid)
        dirs = case.materializer.prepare_directories(case.context, auth, lease,
                                                     directories=WorkspaceDirectories(retained_root, None))
        copy_id = case.status(r)['copies'][0]['id']
        spec = case.requests.claim_copy(rid, auth.operation_revision, copy_id, lease)
        auth = case.requests.authority(rid)
        case.materializer.ensure_exclusion(case.context, auth, (spec,), lease)
        with case.requests.source_handle(spec.id, lease) as fd:
            saved = case.materializer.materialize(case.context, auth, spec, fd,
                copy_identity(FileIdentity.of(os.fstat(fd))), lease, directories=dirs)
        case.requests.record_copy_result(saved, lease)
    return r, saved, spec


def remove(case, r, saved, *, materializer=None):
    materializer = case.materializer if materializer is None else materializer
    with case.hold(r) as lease:
        return materializer.remove_workspace_copy(case.store.files, case.context, case.requests.authority(r['id']),
                                                   owned_copy(case, r), saved, lease, directories=case.dirs(r))


def quota(case):
    return case.store.db.execute('SELECT bytes FROM quota').fetchone()[0]


def make_case(test, *, checked=True):
    case = NativeFixture(test)
    case.mount()
    if checked:
        case.materializer = qualified(case)
    return case


def device_inode(info):
    return info.st_dev, info.st_ino


class _Codes(unittest.TestCase):
    def code(self, code, fn):
        with self.assertRaises(AttachmentError) as error:
            fn()
        self.assertEqual(error.exception.code, code)


@unittest.skipUnless(os.name == 'posix' and GIT, 'Requires POSIX descriptor primitives and Git.')
class WorkspaceLifetimeTests(_Codes):
    def test_real_qualified_paths_pin_before_open_and_cleanly_release(self):
        f = make_case(self)
        files = f.store.files
        original = f.materializer._workspace_directory
        seen = []

        def guarded(path):
            self.assertTrue(files.io_active())
            self.assertEqual(len(files._workspace_pins), 1)
            live, = files._workspace_pins
            self.assertIs(_WORKSPACE_PINS[live], files)
            self.assertIs(live.files, files)
            self.assertTrue(live.keys)
            self.assertNotEqual(live.lock_fd, files.lock_fd)
            self.assertEqual(device_inode(os.fstat(live.lock_fd)), tuple(files.lock_identity))
            self.assertFalse(os.get_inheritable(live.lock_fd))
            seen.append(live)
            return original(path)

        with patch.object(f.materializer, '_workspace_directory', side_effect=guarded):
            r, saved, path = f.materialize()
            with f.hold(r) as lease:
                result = f.materializer.verify_handoff(f.context, f.requests.authority(r['id']), (owned_copy(f, r),),
                                                       saved, lease, directories=f.dirs(r))
            self.assertEqual(result.phase, 'handoff')
            self.assertEqual(remove(f, r, saved[0]).result, 'removed')
            self.assertEqual(remove(f, r, saved[0]).result, 'absent')
        self.assertFalse(path.exists())
        self.assertGreaterEqual(len(set(seen)), 6)
        self.assertFalse(files.io_active())
        self.assertTrue(all(pin not in _WORKSPACE_PINS for pin in seen))

    def test_absent_verified_parent_must_sync_after_unlink_even_on_recovery(self):
        f = make_case(self)
        r, saved, path = f.materialize()
        before = quota(f)
        parent = device_inode(path.parent.stat())
        original_fsync = os.fsync
        synced = []

        def fail_sync(fd):
            if device_inode(os.fstat(fd)) == parent:
                synced.append(fd)
                raise OSError('Fictional failed parent sync.')
            original_fsync(fd)

        with patch('attachments_materialize.os.fsync', side_effect=fail_sync):
            self.code('cleanup_incomplete', lambda: remove(f, r, saved[0]))
            self.assertFalse(path.exists())
            self.code('cleanup_incomplete', lambda: remove(f, r, saved[0]))
        self.assertEqual(len(synced), 2)
        self.assertEqual(quota(f), before)
        self.assertEqual(remove(f, r, saved[0]).result, 'absent')

        def replace_path(fd):
            if device_inode(os.fstat(fd)) == parent:
                path.write_bytes(b'Fictional replacement.')
            original_fsync(fd)

        with patch('attachments_materialize.os.fsync', side_effect=replace_path):
            self.code('copy_changed', lambda: remove(f, r, saved[0]))
        self.assertEqual(path.read_bytes(), b'Fictional replacement.')
        self.assertEqual(quota(f), before)

    def test_legacy_result_cannot_use_qualified_store_bound_entry(self):
        f = make_case(self, checked=False)
        r, saved, path = f.materialize()
        other_root = f.root / 'other-store'
        other_root.mkdir(mode=0o700)
        other = AttachmentStore(other_root, enabled=True, owner_epoch=EPOCH)
        self.addCleanup(other.close)
        materializer = qualified(f)
        with f.hold(r) as lease:
            args = f.context, f.requests.authority(r['id']), owned_copy(f, r), saved[0], lease
            with patch.object(materializer, '_context', side_effect=AssertionError('Cross-store descriptor open.')):
                self.code('storage_unavailable', lambda: materializer.remove_workspace_copy(
                    other.files, *args, directories=f.dirs(r)))
            self.code('storage_unavailable', lambda: f.materializer.remove_workspace_copy(
                f.store.files, *args, directories=f.dirs(r)))
            self.assertTrue(path.exists())
            self.assertEqual(f.materializer.remove_copy(*args, directories=f.dirs(r)).result, 'removed')
        self.assertFalse(f.store.files.io_active())
        self.assertFalse(other.files.io_active())

    def test_active_capacity_refuses_before_workspace_descriptor_and_recovers_after_release(self):
        f = make_case(self)
        r, saved, _ = f.materialize()
        files = f.store.files
        pins = []
        try:
            for field in range(1, _MAX_WORKSPACE_PINS + 1):
                key = f'copy:{field:032x}'
                pin = files._reserve_workspace(f'{field:032x}', frozenset((key,)))
                pins.append(pin)
                files._duplicate_workspace_lock(pin)
            self.assertEqual(len(files._workspace_pins), 64)
            with patch.object(f.materializer, '_workspace_directory', side_effect=AssertionError('Capacity opened path.')):
                self.code('workspace_handle_capacity', lambda: remove(f, r, saved[0]))
            self.code('upload_busy', f.store.close)
            self.assertEqual(len(files._workspace_pins), 64)
        finally:
            for pin in pins:
                self.assertFalse(files._finish_workspace(pin, False))
        self.assertFalse(files.io_active())
        self.assertEqual(remove(f, r, saved[0]).result, 'removed')

    def test_active_operation_fences_store_close_without_retiring_storage(self):
        f = make_case(self)
        r, saved, _ = f.materialize()
        original = f.materializer._workspace_directory
        attempted = []

        with ThreadPoolExecutor(max_workers=1) as pool:
            def try_shutdown(path):
                if not attempted:
                    attempted.append(path)
                    self.code('upload_busy', lambda: pool.submit(f.store.close).result(timeout=3))
                    self.assertIsNotNone(f.store.db)
                return original(path)

            with patch.object(f.materializer, '_workspace_directory', side_effect=try_shutdown):
                self.assertEqual(remove(f, r, saved[0]).result, 'removed')
        self.assertEqual(len(attempted), 1)
        self.assertFalse(f.store.files.io_active())
        f.store.close()
        self.assertTrue(f.store.closed)

    def test_nested_cleanup_cannot_return_before_outer_handle_closure(self):
        f = make_case(self)
        r, saved, _ = f.materialize()
        seen = []

        def nested(point):
            if point == 'cleanup_before_unlink':
                seen.append(point)
                self.code('workspace_handles_busy', lambda: f.materializer.remove_workspace_copy(
                    f.store.files, f.context, f.requests.authority(r['id']), owned_copy(f, r), saved[0], lease,
                    directories=f.dirs(r)))

        f.materializer.checkpoint = nested
        with f.hold(r) as lease:
            result = f.materializer.remove_workspace_copy(f.store.files, f.context, f.requests.authority(r['id']),
                        owned_copy(f, r), saved[0], lease, directories=f.dirs(r))
        self.assertEqual((result.result, len(seen)), ('removed', 1))
        self.assertFalse(f.store.files.io_active())

    def _worker(self, *args):
        return [sys.executable, __file__, *args]

    def test_process_exit_is_required_to_release_the_actual_lifetime_flock(self):
        for fault in ('workspace', 'verify_workspace', 'ancestor', 'lock_dup', 'reused_lock_dup', 'partial_lock_dup'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix='workspace-closure-process-') as temp:
                parent = str(Path(temp).resolve())
                env = dict(os.environ, TMPDIR=parent)
                process = subprocess.Popen(self._worker('hold', fault), text=True, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, close_fds=True)
                try:
                    ready, _, _ = select.select([process.stdout], [], [], 15)
                    self.assertTrue(ready, 'The fictional owner did not report readiness.')
                    raw = process.stdout.readline()
                    if not raw:
                        process.wait(timeout=5)
                        self.fail(process.stderr.read())
                    state = json.loads(raw)
                    self.assertGreater(state['charge'], 0)
                    folder = str(Path(parent) / state['fixture'])
                    probe = subprocess.run(self._worker('probe', folder), capture_output=True, text=True,
                                           timeout=5, env=env)
                    self.assertEqual((probe.returncode, probe.stdout.strip()), (0, 'blocked'), probe.stderr)
                    out, error = process.communicate('exit\n', timeout=5)
                    self.assertEqual((process.returncode, out), (0, ''), error)
                    recovered = subprocess.run(self._worker('recover', folder, state['request'], str(state['charge']),
                                               *map(str, state['workspace_identity'])),
                                               capture_output=True, text=True, timeout=10, env=env)
                    self.assertEqual((recovered.returncode, recovered.stdout.strip()),
                                     (0, state['recovery']), recovered.stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()


def hold_uncertain(fault):
    check = _Codes()
    f = make_case(check)
    files = f.store.files
    if fault == 'partial_lock_dup':
        r = f.prepare()
        path, failed = damage_partial(check, f, r)
        recovery = 'materializing'
    else:
        r, saved, path = f.materialize()
        if fault == 'ancestor':
            check.assertEqual(remove(f, r, saved[0]).result, 'removed')
            destination = device_inode(f.root.stat())
        else:
            destination = device_inode(path.stat())
        original_close = os.close
        failed = []

        def close(fd):
            active = tuple(files._workspace_pins)
            candidate = (bool(active) and fd == active[0].lock_fd if 'lock_dup' in fault
                         else _WORKSPACE_SCOPE.get() is not None and device_inode(os.fstat(fd)) == destination)
            if candidate and not failed:
                failed.append(fd)
                if fault == 'reused_lock_dup':
                    original_close(fd)
                    os.dup2(files.root_fd, fd)
                raise OSError('Fictional unconfirmed close.')
            check.assertNotIn(fd, failed, 'An uncertain descriptor number was retried.')
            original_close(fd)

        def operate():
            if fault != 'verify_workspace':
                return remove(f, r, saved[0])
            with f.hold(r) as lease:
                return f.materializer.verify_copy(f.context, f.requests.authority(r['id']), owned_copy(f, r),
                                                   saved[0], lease, directories=f.dirs(r))

        with patch('attachments_materialize.os.close', side_effect=close):
            # A handled exception in the caller is not the current operation's
            # primary failure and must not allow an affirmative result.
            try:
                raise ValueError('Fictional outer handler.')
            except ValueError:
                check.code('handle_close_failed', operate)
        check.assertEqual(len(failed), 1)
        if fault == 'verify_workspace':
            check.assertEqual(path.read_bytes(), CONTENT)
            recovery = 'removed'
        else:
            check.assertFalse(path.exists())
            recovery = 'absent'

    os.fstat(failed[0])  # The fault actually left a kernel descriptor live.
    pin, = files._workspace_pins
    check.assertIs(_WORKSPACE_PINS[pin], files)
    check.assertTrue(pin.uncertain)
    check.assertTrue(files._finish_workspace(pin, False))
    os.fstat(failed[0])
    check.assertTrue(files.io_active())
    before = quota(f)
    if fault != 'partial_lock_dup':
        second = qualified(f)
        with patch.object(second, '_workspace_directory', side_effect=AssertionError('Quarantined path opened.')):
            check.code('workspace_handles_busy', lambda: remove(f, r, saved[0], materializer=second))
        check.code('workspace_handles_busy', lambda: remove(f, r, saved[0]))
    check.code('upload_busy', files.close)
    check.code('upload_busy', f.store.close)
    check.assertIsNotNone(f.store.db)
    check.code('storage_unavailable', lambda: AttachmentStore(f.state, enabled=True, owner_epoch=EPOCH))
    check.assertEqual(quota(f), before)
    check_fork(f, r)
    if fault != 'partial_lock_dup':
        neighbor, neighbor_saved, spec = materialize_neighbor(f, f.dirs(r).root)
        source = f.store.db.execute('SELECT identity,digest FROM files WHERE id=?', (spec.source_id,)).fetchone()
        check.assertEqual(files.read(spec.source_id, json.loads(source['identity']), source['digest'], 1024), CONTENT)
        with f.hold(neighbor) as lease:
            verified = f.materializer.verify_copy(f.context, f.requests.authority(neighbor['id']), spec,
                                                   neighbor_saved, lease, directories=f.dirs(neighbor))
        check.assertEqual(verified.sha256, neighbor_saved.sha256)
    # No Close/LOCK_UN can invalidate a quarantined pin. In the first fault
    # variant, drop the base descriptor directly after those checks to isolate
    # the duplicate's independent kernel lock in the fresh-process probe.
    if fault == 'workspace':
        probe_capacity_with_quarantine(check, files, pin)
        os.close(files.lock_fd)
        files.lock_fd = None
        check.assertEqual(device_inode(os.fstat(pin.lock_fd)), tuple(files.lock_identity))
    workspace_identity = (f.context.root_identity.device, f.context.root_identity.inode)
    print(json.dumps(dict(fixture=f.root.name, request=r['id'], charge=quota(f), recovery=recovery,
                          workspace_identity=workspace_identity)), flush=True)
    if sys.stdin.readline().strip() != 'exit':
        raise AssertionError('Fictional owner exit handshake failed.')
    os._exit(0)


def probe_capacity_with_quarantine(check, files, quarantined):
    live = []
    try:
        for number in range(1, 64):
            pin = files._reserve_workspace(f'{number:032x}', frozenset((f'copy:{number:032x}',)))
            live.append(pin)
            files._duplicate_workspace_lock(pin)
        check.assertEqual(len(files._workspace_pins), 64)
        check.code('workspace_handle_capacity', lambda: files._reserve_workspace('0' * 32, frozenset()))
        check.assertIn(quarantined, files._workspace_pins)
    finally:
        for pin in live:
            check.assertFalse(files._finish_workspace(pin, False))
    check.assertEqual(files._workspace_pins, {quarantined})


def damage_partial(check, f, r):
    files = f.store.files
    rd = r['id']
    with f.hold(r) as lease:
        f.authorize(r, lease)
        auth = f.requests.authority(rd)
        dirs = f.materializer.prepare_directories(f.context, auth, lease)
        copy_id = f.status(r)['copies'][0]['id']
        spec = f.requests.claim_copy(rd, auth.operation_revision, copy_id, lease)
        auth = f.requests.authority(rd)
        f.materializer.ensure_exclusion(f.context, auth, (spec,), lease)
        original_write, original_close = os.write, os.close
        failed = []

        def partial(fd, data):
            original_write(fd, data[:2])
            raise OSError('Fictional partial write.')

        def close(fd):
            if any(pin.lock_fd == fd for pin in files._workspace_pins):
                check.assertNotIn(fd, failed)
                failed.append(fd)
                raise OSError('Fictional unconfirmed duplicate close.')
            original_close(fd)

        with f.requests.source_handle(copy_id, lease) as fd:
            specialized = qualified(f)
            with patch.object(specialized, 'record_partial', side_effect=AssertionError('Unconfirmed partial reported.')), \
                 patch('attachments_materialize.os.write', side_effect=partial), \
                 patch('attachments_materialize.os.close', side_effect=close):
                check.code('copy_failed', lambda: specialized.materialize(f.context, auth, spec, fd,
                           copy_identity(FileIdentity.of(os.fstat(fd))), lease, directories=dirs))
    check.assertEqual(len(failed), 1)
    return f.repo / spec.relative_path, failed


def check_fork(f, r):
    if not hasattr(os, 'fork'):
        return
    # No inherited mutex can be taken by the child to qualify a workspace pin.
    files = f.store.files
    with files._io_lock:
        child = os.fork()
        if child == 0:
            try:
                files._reserve_workspace(r['id'], frozenset())
            except AttachmentError:
                os._exit(0)
            os._exit(1)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child, os.WNOHANG)
        if pid:
            if status:
                raise AssertionError('An inherited workspace owner was admitted.')
            return
        time.sleep(.01)
    os.kill(child, 9)
    os.waitpid(child, 0)
    raise AssertionError('An inherited workspace owner waited on the inherited mutex.')


def recover(root, rid, charge, workspace_identity):
    state = root / 'state'
    store = AttachmentStore(state, enabled=True, owner_epoch=EPOCH)
    requests = RequestLedger(store)
    resources = ResourceLocks(str(root / 'locks'), enabled=True)
    try:
        assert store.db.execute('SELECT bytes FROM quota').fetchone()[0] == charge
        with store._gate():
            row = requests._request(rid)
            if row['stage'] == 'materializing':
                print('materializing')
                return
            copy = requests._copies(rid)[0]
            spec = requests._intent(copy)
            authority = requests.authority(rid)
            dirs = WorkspaceDirectories.from_record(requests.workspace_directories(rid))
        workspace = root / 'orchard'
        git = workspace / '.git'
        exe = Path(GIT).resolve()
        context = WorkspaceContext(authority.context_fingerprint, authority.workspace_fingerprint,
            str(workspace), workspace_identity, str(git), DirectoryIdentity.of(git.stat()),
            str(git / 'info'), DirectoryIdentity.of((git / 'info').stat()), str(exe), FileIdentity.of(exe.stat()))
        saved = VerifiedCopy(spec.id, spec.lease_id, spec.copy_generation, tuple(json.loads(copy['identity'])),
                             spec.sha256, spec.bytes,
                             Materializer._verification(context, authority, (spec,), 'publication', spec.lease_id))
        materializer = Materializer(enabled=True, workspace_files=store.files,
                                     validate_authority=lambda value, phase, copies: authority == value)
        with resources.hold(requests.resource_keys(rid)) as lease:
            result = materializer.remove_workspace_copy(store.files, context, authority, spec, saved, lease, directories=dirs)
        assert result.handles_closed and store.db.execute('SELECT bytes FROM quota').fetchone()[0] == charge
        print(result.result)
    finally:
        resources.close()
        store.close()


def process_fixture_root(value):
    path = Path(value)
    parent = Path(tempfile.gettempdir()).resolve()
    if path.parent != parent or not path.name.startswith('native-attachment-fixture-') or path.resolve() != path:
        raise AssertionError('Expected a test-owned fixture under the disposable process directory.')
    return path


if __name__ == '__main__':
    action, *args = sys.argv[1:]
    if action == 'hold':
        hold_uncertain(args[0])
    elif action == 'probe':
        try:
            owner = AttachmentStore(process_fixture_root(args[0]) / 'state', enabled=True, owner_epoch=EPOCH)
        except AttachmentError as error:
            assert error.code == 'storage_unavailable'
            print('blocked')
        else:
            owner.close()
            print('opened')
    elif action == 'recover':
        recover(process_fixture_root(args[0]), args[1], int(args[2]), DirectoryIdentity(int(args[3]), int(args[4])))
    else:
        raise AssertionError('Unknown fictional process mode.')
