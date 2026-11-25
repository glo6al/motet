"""Offline filesystem/Git and resource-lease proofs; no native/provider calls."""
import dataclasses
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_materialize import (Materializer, WorkspaceContext, DirectoryIdentity,
    FileIdentity, WorkspaceDirectories, relative_path, resource_keys, copy_identity)
from attachments_requests import RequestAuthority, CopyIntent, PartialCopy
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError

GIT = shutil.which('git')


# POSIX: these use POSIX modes, links and Git (Git on Windows is step 6c.5b). The copy itself on Windows is tested in
# test_attachments_windows_materialize.py.
@unittest.skipUnless(os.name == 'posix' and GIT, 'POSIX modes and links, and Git; Windows: test_attachments_windows_materialize.py')
class MaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='attachment-copy-test-')
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'orchard'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('-c', 'user.name=Fictional', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '--allow-empty', '-m', 'Fictional fixture')
        self.lockroot = self.base / 'locks'
        self.lockroot.mkdir(mode=0o700)
        self.locks = ResourceLocks(str(self.lockroot), enabled=True)
        self.context = self.context_for(self.repo)
        self.authority = RequestAuthority('a'*32, 'b'*64, 1, 'qualified-test', 'c'*64, '9'*64, 'd'*64)
        self.content = b'Fictional orchard notes.\n'
        self.source = self.base / 'source'
        self.source.write_bytes(self.content)
        self.source.chmod(0o600)
        self.source_fd = os.open(self.source, os.O_RDONLY)
        self.source_identity = copy_identity(FileIdentity.of(os.fstat(self.source_fd)))
        self.spec = CopyIntent('e'*32, self.authority.request_id, 'f'*32, 1, 1, 1, 'workspace',
                               relative_path(self.authority.request_id, 'e'*32, 'text/plain'), len(self.content),
                               hashlib.sha256(self.content).hexdigest(), 'text/plain', '1'*64)
        self.recorded, self.created, self.partials = {}, [], []
        self.allow, self.cleanup = True, False
        self.materializer = Materializer(enabled=True, validate_authority=self.validate,
                                        record_directory=self.record_directory, record_created=self.record_created,
                                        record_partial=self.record_partial)

    def tearDown(self):
        os.close(self.source_fd)
        self.locks.close()
        self.temp.cleanup()

    def git(self, *args, folder=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        result = subprocess.run([GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                 '-C', str(folder or self.repo), *args], env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, 'Fictional Git setup failed')
        return result.stdout

    def context_for(self, folder):
        gd = Path(self.git('rev-parse', '--absolute-git-dir', folder=folder).decode().strip()).resolve()
        common = Path(self.git('rev-parse', '--path-format=absolute', '--git-common-dir', folder=folder).decode().strip()).resolve()
        exe = Path(GIT).resolve()
        return WorkspaceContext('c'*64, '9'*64, str(folder), DirectoryIdentity.of(folder.stat()), str(gd),
                                DirectoryIdentity.of(gd.stat()), str(common/'info'), DirectoryIdentity.of((common/'info').stat()),
                                str(exe), FileIdentity.of(exe.stat()))

    def validate(self, authority, phase, copies):
        return authority == self.authority and (self.cleanup if phase == 'cleanup' else self.allow)

    def record_directory(self, authority, role, identity, lease):
        lease.require(resource_keys(self.context, authority))
        self.recorded[role] = identity
        return True

    def record_created(self, copy_id, lease_id, identity, lease):
        self.assertEqual(identity[2], 0)  # durable receipt precedes source bytes
        lease.require(resource_keys(self.context, self.authority, (self.spec,)))
        self.created.append((copy_id, identity))
        return True

    def record_partial(self, partial, lease):
        lease.require(resource_keys(self.context, self.authority, (self.spec,)))
        self.assertTrue(partial.handles_closed)
        self.partials.append(partial)
        return True

    def dirs(self):
        return WorkspaceDirectories.from_record({'root_identity': self.recorded.get('root'), 'request_identity': self.recorded.get('request')})

    def hold(self, spec=None):
        return self.locks.hold(resource_keys(self.context, self.authority, (spec or self.spec,)))

    def make(self, lease):
        self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
        return self.materializer.materialize(self.context, self.authority, self.spec, self.source_fd,
                                             self.source_identity, lease)

    def assert_code(self, code, call):
        with self.assertRaises(AttachmentError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_success_copy_verify_and_manual_unknown_cleanup(self):
        with self.hold() as lease:
            saved = self.make(lease)
            self.assertEqual((self.repo/self.spec.relative_path).read_bytes(), self.content)
            self.assertEqual(len(self.created), 1)
            self.assertEqual(saved.identity[2], len(self.content))
            self.assertEqual(saved.verification.phase, 'publication')
            self.allow, self.cleanup = False, True  # native outcome may remain unknown
            result = self.materializer.remove_copy(self.context, self.authority, self.spec, saved, lease, directories=self.dirs())
            self.assertEqual(result.result, 'removed')
            self.assertTrue(result.handles_closed)
            result = self.materializer.remove_copy(self.context, self.authority, self.spec, saved, lease, directories=self.dirs())
            self.assertEqual(result.result, 'absent')
        self.assertIn(self.authority.request_id, (Path(self.context.exclude_parent)/'exclude').read_text())

    def test_disabled_and_missing_send_authority_write_nothing(self):
        before = (Path(self.context.exclude_parent)/'exclude').read_bytes()
        with self.hold() as lease:
            self.assert_code('attachments_disabled', lambda: Materializer().ensure_exclusion(self.context, self.authority, (self.spec,), lease))
            self.allow = False
            self.assert_code('send_authority_changed', lambda: self.make(lease))
        self.assertFalse((self.repo/'.watch-attachments').exists())
        self.assertEqual((Path(self.context.exclude_parent)/'exclude').read_bytes(), before)

    def test_no_copy_before_effective_ignore(self):
        with self.hold() as lease:
            self.assert_code('exclusion_ineffective', lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                             self.source_fd, self.source_identity, lease))
        self.assertFalse((self.repo/'.watch-attachments').exists())

    def test_root_negation_blocks_before_bytes(self):
        (self.repo/'.gitignore').write_text('!/.watch-attachments/*/\n')
        with self.hold() as lease:
            self.assert_code('exclusion_ineffective', lambda: self.make(lease))
        self.assertFalse((self.repo/'.watch-attachments').exists())

    def test_later_exclude_negation_not_just_line_presence(self):
        with self.hold() as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            with (Path(self.context.exclude_parent)/'exclude').open('a') as f:
                f.write('!/.watch-attachments/' + self.authority.request_id + '/\n')
            self.assert_code('exclusion_ineffective', lambda: self.materializer.prove_ignored(self.context, self.authority, (self.spec,), lease))

    def test_tracked_reserved_subtree_rejected_independently(self):
        path = self.repo/'.watch-attachments'/'other.txt'
        path.parent.mkdir(mode=0o700)
        path.write_text('Fictional tracked file')
        self.git('add', '-f', '--', '.watch-attachments/other.txt')
        with self.hold() as lease:
            self.assert_code('destination_tracked', lambda: self.make(lease))
        self.assertFalse((self.repo/self.spec.relative_path).exists())

    def test_linked_worktree_uses_own_index_and_ignore(self):
        linked = self.base/'linked'
        self.git('worktree', 'add', '-q', '-b', 'fixture-linked', str(linked))
        self.context = self.context_for(linked)
        with self.hold() as lease:
            saved = self.make(lease)
            self.assertTrue((linked/self.spec.relative_path).exists())
            self.assertFalse((self.repo/self.spec.relative_path).exists())
            self.git('add', '-f', '--', self.spec.relative_path, folder=linked)
            self.assert_code('exclusion_ineffective', lambda: self.materializer.verify_copy(self.context, self.authority, self.spec,
                             saved, lease, directories=self.dirs()))
            self.assertEqual(self.git('ls-files', '-z', '--', '.watch-attachments'), b'')

    def test_exclusion_concurrent_append_preserves_bytes(self):
        exclude = Path(self.context.exclude_parent)/'exclude'
        before = exclude.read_bytes()
        def hook(stage):
            if stage == 'exclude_before_append':
                with exclude.open('ab') as f:
                    f.write(b'\nunrelated-fictional-rule\n')
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
        after = exclude.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertIn(b'unrelated-fictional-rule', after)

    def test_exclusion_prefix_rewrite_rejected(self):
        exclude = Path(self.context.exclude_parent)/'exclude'
        def hook(stage):
            if stage == 'exclude_after_append':
                data = exclude.read_bytes()
                exclude.write_bytes(b'changed' + data[7:])
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.assert_code('exclusion_changed', lambda: self.make(lease))
        self.assertFalse((self.repo/'.watch-attachments').exists())

    def test_exclusion_short_write_never_completes_suffix(self):
        write = os.write
        def short(fd, payload):
            return write(fd, payload[:8])
        with self.hold() as lease, mock.patch('attachments_materialize.os.write', side_effect=short) as calls:
            self.assert_code('exclusion_partial', lambda: self.make(lease))
            self.assertEqual(calls.call_count, 1)
        self.assertFalse((self.repo/'.watch-attachments').exists())

    def test_exclusion_symlink_and_hardlink_refused(self):
        exclude = Path(self.context.exclude_parent)/'exclude'
        other = self.base/'exclude-original'
        exclude.rename(other)
        exclude.symlink_to(other)
        with self.hold() as lease:
            self.assert_code('exclusion_unavailable', lambda: self.make(lease))
        exclude.unlink()
        os.link(other, exclude)
        with self.hold() as lease:
            self.assert_code('copy_changed', lambda: self.make(lease))

    def test_unrecognized_directory_never_adopted(self):
        (self.repo/'.watch-attachments').mkdir(mode=0o700)
        with self.hold() as lease:
            self.assert_code('copy_changed', lambda: self.make(lease))
        self.assertFalse(self.created)

    def test_source_digest_mismatch_never_published(self):
        self.spec = dataclasses.replace(self.spec, sha256='0'*64)
        with self.hold() as lease:
            self.assert_code('source_changed', lambda: self.make(lease))
        self.assertEqual(len(self.created), 0)

    def test_partial_write_exception_records_exact_prefix_for_cleanup(self):
        real_write = os.write
        with self.hold() as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            def partial_write(fd, data):
                real_write(fd, data[:5])
                raise OSError('fictional write failure')
            with mock.patch('attachments_materialize.os.write', side_effect=partial_write):
                self.assert_code('copy_failed', lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                                 self.source_fd, self.source_identity, lease))
            self.assertEqual(len(self.partials), 1)
            partial = self.partials[0]
            self.assertIs(type(partial), PartialCopy)
            self.assertEqual(partial.bytes, 5)
            self.assertEqual(partial.sha256, hashlib.sha256(self.content[:5]).hexdigest())
            self.allow, self.cleanup = False, True
            result = self.materializer.remove_copy(self.context, self.authority, self.spec, partial, lease, directories=self.dirs())
            self.assertEqual(result.result, 'removed')
            self.assertTrue(result.handles_closed)

    def _materialize_close_fault(self, manager, phase):
        original_context = self.materializer._context
        original_directories = self.materializer._directories
        original_verify = self.materializer.verify_copy
        original_partial = self.materializer._partial_evidence
        real_close, real_write = os.close, os.write
        state = {'phase': None, 'target': None, 'failed': None}
        attempted, selected = [], []

        @contextmanager
        def context(*args, **kwargs):
            with original_context(*args, **kwargs) as fds:
                if manager == 'context' and state['phase'] == phase and state['target'] is None:
                    selected.extend(fds)
                    state['target'] = fds[-1]
                yield fds

        @contextmanager
        def directories(*args, **kwargs):
            with original_directories(*args, **kwargs) as result:
                if manager == 'directories' and state['phase'] == phase and state['target'] is None:
                    selected.append(result[0])
                    state['target'] = result[0]
                yield result

        def verify(*args, **kwargs):
            state['phase'] = 'verification'
            try:
                return original_verify(*args, **kwargs)
            finally:
                state['phase'] = None

        def partial(*args, **kwargs):
            state['phase'] = 'partial'
            try:
                return original_partial(*args, **kwargs)
            finally:
                state['phase'] = None

        def close(fd):
            if fd == state['target'] and state['failed'] is None:
                state['failed'] = fd
                attempted.append(fd)
                raise OSError('fictional unconfirmed directory close')
            attempted.append(fd)
            real_close(fd)

        def write(fd, data):
            if phase == 'partial':
                real_write(fd, data[:5])
                raise OSError('fictional original write failure')
            return real_write(fd, data)

        try:
            with self.hold() as lease:
                self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
                with mock.patch.object(self.materializer, '_context', side_effect=context), \
                     mock.patch.object(self.materializer, '_directories', side_effect=directories), \
                     mock.patch.object(self.materializer, 'verify_copy', side_effect=verify), \
                     mock.patch.object(self.materializer, '_partial_evidence', side_effect=partial), \
                     mock.patch('attachments_materialize.os.close', side_effect=close), \
                     mock.patch('attachments_materialize.os.write', side_effect=write):
                    self.assert_code('copy_failed' if phase == 'partial' else 'handle_close_failed',
                                     lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                                         self.source_fd, self.source_identity, lease))
                self.assertIsNotNone(state['failed'])
                self.assertTrue(self.created)
                self.assertEqual(self.partials, [])
                self.assertTrue(set(selected).issubset(set(attempted)))
                for fd in selected:
                    if fd != state['failed']:
                        with self.assertRaises(OSError): os.fstat(fd)
                # The injected failed close really left its descriptor open;
                # a later successful probe must not turn this into closed=True.
                os.fstat(state['failed'])
            self.assertEqual(self.locks._leases, 0)
        finally:
            if state['failed'] is not None:
                real_close(state['failed'])

    def test_context_close_failure_in_verification_withholds_partial_receipt(self):
        self._materialize_close_fault('context', 'verification')

    def test_directories_close_failure_in_verification_withholds_partial_receipt(self):
        self._materialize_close_fault('directories', 'verification')

    def test_context_close_failure_in_partial_recovery_preserves_primary(self):
        self._materialize_close_fault('context', 'partial')

    def test_directories_close_failure_in_partial_recovery_preserves_primary(self):
        self._materialize_close_fault('directories', 'partial')

    def test_close_all_attempts_remaining_descriptors_and_preserves_primary(self):
        from attachments_materialize import _close_all, _closure_scope
        real_close = os.close
        fds = [os.open(self.source, os.O_RDONLY) for _ in range(3)]
        attempted = []
        def close(fd):
            attempted.append(fd)
            if fd == fds[0]:
                raise OSError('fictional unconfirmed close')
            real_close(fd)
        try:
            with _closure_scope() as closure:
                with mock.patch('attachments_materialize.os.close', side_effect=close):
                    with self.assertRaisesRegex(RuntimeError, 'fictional primary') as error:
                        try:
                            raise RuntimeError('fictional primary')
                        finally:
                            _close_all(fds)
                self.assertTrue(closure.unconfirmed)
                self.assertTrue(error.exception.__notes__)
            self.assertEqual(attempted, fds)
            for fd in fds[1:]:
                with self.assertRaises(OSError): os.fstat(fd)
        finally:
            real_close(fds[0])

    def test_partial_modified_on_same_inode_or_replaced_never_blessed(self):
        real_write = os.write
        for mode in ('modified', 'replaced'):
            with self.subTest(mode=mode):
                with self.hold() as lease:
                    self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
                    def partial_write(fd, data):
                        real_write(fd, data[:5])
                        target = self.repo/self.spec.relative_path
                        if mode == 'modified':
                            os.pwrite(fd, b'WRONG', 0)
                        else:
                            target.rename(target.with_suffix('.old'))
                            target.write_bytes(data[:5])
                        raise OSError('fictional write failure')
                    with mock.patch('attachments_materialize.os.write', side_effect=partial_write):
                        self.assert_code('copy_failed', lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                                         self.source_fd, self.source_identity, lease))
                    self.assertEqual(self.partials, [])
                shutil.rmtree(self.repo/'.watch-attachments')
                self.recorded.clear()

    def test_partial_modified_after_receipt_is_left_alone(self):
        real_write = os.write
        with self.hold() as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            def partial_write(fd, data):
                real_write(fd, data[:5]); raise OSError('fictional write failure')
            with mock.patch('attachments_materialize.os.write', side_effect=partial_write):
                self.assert_code('copy_failed', lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                                 self.source_fd, self.source_identity, lease))
            partial = self.partials[0]
            (self.repo/self.spec.relative_path).write_bytes(b'OTHER')
            self.allow, self.cleanup = False, True
            self.assert_code('copy_changed', lambda: self.materializer.remove_copy(self.context, self.authority, self.spec,
                             partial, lease, directories=self.dirs()))
            self.assertEqual((self.repo/self.spec.relative_path).read_bytes(), b'OTHER')

    def test_partial_receipt_is_after_owned_copy_descriptor_closes(self):
        real_open, real_write = os.open, os.write
        copied_fds = []
        def capture_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if isinstance(name, str) and name.endswith('.txt') and flags & os.O_CREAT:
                copied_fds.append(fd)
            return fd
        def record(partial, lease):
            self.assertTrue(copied_fds)
            for fd in copied_fds:
                with self.assertRaises(OSError): os.fstat(fd)
            return self.record_partial(partial, lease)
        self.materializer.record_partial = record
        with self.hold() as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            def partial_write(fd, data):
                real_write(fd, data[:5]); raise OSError('fictional write failure')
            with mock.patch('attachments_materialize.os.open', side_effect=capture_open), \
                 mock.patch('attachments_materialize.os.write', side_effect=partial_write):
                self.assert_code('copy_failed', lambda: self.materializer.materialize(self.context, self.authority, self.spec,
                                 self.source_fd, self.source_identity, lease))
        self.assertEqual(len(self.partials), 1)

    def test_record_creation_failure_happens_before_bytes(self):
        self.materializer.record_created = lambda *a: False
        with self.hold() as lease:
            self.assert_code('copy_record_failed', lambda: self.make(lease))
        self.assertEqual((self.repo/self.spec.relative_path).stat().st_size, 0)

    def test_revocation_during_copy_prevents_publication(self):
        def hook(stage):
            if stage == 'copy_created':
                self.allow = False
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.assert_code('send_authority_changed', lambda: self.make(lease))
        self.assertEqual((self.repo/self.spec.relative_path).stat().st_size, 0)

    def test_publication_rechecks_effective_ignore(self):
        def hook(stage):
            if stage == 'copy_before_publication':
                (self.repo/'.gitignore').write_text('!/.watch-attachments/*/\n')
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.assert_code('exclusion_ineffective', lambda: self.make(lease))

    def test_directory_rename_before_publication_rejected(self):
        def hook(stage):
            if stage == 'copy_before_publication':
                self.repo.rename(self.base/'moved')
                self.repo.mkdir()
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.assert_code('folder_changed', lambda: self.make(lease))

    def test_modified_copy_not_deleted_and_symlink_not_followed(self):
        with self.hold() as lease:
            saved = self.make(lease)
            self.cleanup = True
            target = self.repo/self.spec.relative_path
            target.write_bytes(b'changed')
            self.assert_code('copy_changed', lambda: self.materializer.remove_copy(self.context, self.authority, self.spec,
                             saved, lease, directories=self.dirs()))
            target.unlink()
            target.symlink_to(self.source)
            self.assert_code('cleanup_incomplete', lambda: self.materializer.remove_copy(self.context, self.authority, self.spec,
                             saved, lease, directories=self.dirs()))
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_replacement_immediately_before_unlink_left_alone(self):
        with self.hold() as lease:
            saved = self.make(lease)
            self.cleanup = True
            target = self.repo/self.spec.relative_path
            def hook(stage):
                if stage == 'cleanup_before_unlink':
                    target.rename(target.with_suffix('.old'))
                    target.write_bytes(b'Fictional replacement')
            self.materializer.checkpoint = hook
            self.assert_code('copy_changed', lambda: self.materializer.remove_copy(self.context, self.authority, self.spec,
                             saved, lease, directories=self.dirs()))
            self.assertEqual(target.read_bytes(), b'Fictional replacement')

    def test_stale_or_incomplete_resource_lease_rejected(self):
        with self.locks.hold(resource_keys(self.context, self.authority)) as lease:
            self.assert_code('resource_lease_required', lambda: self.make(lease))
        self.assert_code('resource_lease_required', lambda: self.make(lease))

    def test_directory_preparation_then_canonical_ledger_copy_shapes(self):
        with self.hold() as lease:
            dirs = self.materializer.prepare_directories(self.context, self.authority, lease)
            self.assertFalse((self.repo/self.spec.relative_path).exists())
            self.assertEqual(dirs, self.dirs())
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            saved = self.materializer.materialize(self.context, self.authority, self.spec, self.source_fd,
                                                 self.source_identity, lease, directories=dirs)
            proof = self.materializer.verify_handoff(self.context, self.authority, (self.spec,), (saved,), lease, directories=dirs)
            self.assertEqual(proof.phase, 'handoff')
            self.assertEqual(proof.copy_generations, ((self.spec.id, 1),))

    def test_second_native_context_reuses_same_verified_workspace_root(self):
        with self.hold() as lease:
            first = self.materializer.prepare_directories(self.context, self.authority, lease)
        old_keys = resource_keys(self.context, self.authority)
        self.context = dataclasses.replace(self.context, fingerprint='8'*64)
        self.authority = dataclasses.replace(self.authority, request_id='7'*32, context_fingerprint='8'*64)
        self.spec = dataclasses.replace(self.spec, request_id=self.authority.request_id,
                                       relative_path=relative_path(self.authority.request_id, self.spec.id, 'text/plain'))
        with self.hold() as lease:
            second = self.materializer.prepare_directories(self.context, self.authority, lease,
                                                           directories=WorkspaceDirectories(first.root, None))
            self.assertEqual(first.root, second.root)
            self.assertNotEqual(first.request, second.request)
        self.assertIn('workspace:'+'9'*32, old_keys)
        self.assertIn('workspace:'+'9'*32, resource_keys(self.context, self.authority))

    def test_cleanup_does_not_require_current_git_or_native_availability(self):
        with self.hold() as lease:
            saved = self.make(lease)
            self.allow, self.cleanup = False, True
            (self.repo/'.git').rename(self.base/'retired-git')
            result = self.materializer.remove_copy(self.context, self.authority, self.spec, saved, lease, directories=self.dirs())
            self.assertEqual(result.result, 'removed')

    def test_git_inputs_changed_during_proof_fail_closed(self):
        def hook(stage):
            if stage == 'git_file_proved':
                (self.repo/'.gitignore').write_text('!/.watch-attachments/*/\n')
        self.materializer.checkpoint = hook
        with self.hold() as lease:
            self.assert_code('exclusion_changed', lambda: self.make(lease))

    def test_config_include_not_silently_qualified(self):
        with (self.repo/'.git/config').open('a') as f:
            f.write('\n[include]\n path = missing-fictional-config\n')
        with self.hold() as lease:
            self.assert_code('exclusion_unavailable', lambda: self.make(lease))

    def test_config_include_after_a_header_or_byte_order_mark_is_refused(self):
        original = (self.repo/'.git/config').read_bytes()
        for config in (original + b'\n[core] [include]\n path = missing-fictional-config\n',
                       b'\xef\xbb\xbf[include]\n path = missing-fictional-config\n' + original,
                       original + b'\n[includeIf.gitdir:/fictional/]\n path = missing-fictional-config\n'):
            with self.subTest(config=config[:40]):
                (self.repo/'.git/config').write_bytes(config)
                with self.hold() as lease:
                    self.assert_code('exclusion_unavailable', lambda: self.make(lease))
        (self.repo/'.git/config').write_bytes(original + b'\n[includes]\n\tfictional = 1\n')
        with self.hold() as lease:
            self.make(lease)

    def test_fake_git_output_and_timeout_are_bounded(self):
        exe = self.base/'fictional-git'
        exe.write_text('#!' + sys.executable + '\nimport sys\nsys.stdout.write("x"*20000)\n')
        exe.chmod(0o700)
        context = dataclasses.replace(self.context, git_executable=str(exe), git_executable_identity=FileIdentity.of(exe.stat()))
        self.assert_code('exclusion_unavailable', lambda: self.materializer._git(context, ['unused'], time.monotonic()+1, stdout_limit=16384))
        exe.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(2)\n')
        context = dataclasses.replace(context, git_executable_identity=FileIdentity.of(exe.stat()))
        started = time.monotonic()
        self.assert_code('exclusion_unavailable', lambda: self.materializer._git(context, ['unused'], started+.05, stdout_limit=0))
        self.assertLess(time.monotonic()-started, 1)


@unittest.skipUnless(os.name == 'posix', 'POSIX flock and modes; Windows: test_attachments_windows_materialize.py')
class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='attachment-resource-test-')
        self.root = str(Path(self.temp.name).resolve())
        os.chmod(self.root, 0o700)
        self.manager = ResourceLocks(self.root, enabled=True)
        self.key = 'request:' + 'a'*32

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_cross_thread_exclusion_and_close_guard(self):
        outcome = []
        with self.manager.hold([self.key]):
            def contender():
                try:
                    with self.manager.hold([self.key], timeout=.05):
                        outcome.append('unsafe')
                except AttachmentError as error:
                    outcome.append(error.code)
            t = threading.Thread(target=contender)
            t.start(); t.join(2)
            self.assertFalse(t.is_alive())
            self.assertEqual(outcome, ['resource_busy'])
            with self.assertRaises(AttachmentError): self.manager.close()
        with self.manager.hold([self.key]) as lease: lease.require([self.key])

    def test_constructor_private_validation_failure_closes_open_directory(self):
        from attachments_resources import directory
        opened = []
        def capture(path):
            fd = directory(path); opened.append(fd); return fd
        os.chmod(self.root, 0o755)
        try:
            with mock.patch('attachments_resources.directory', side_effect=capture):
                with self.assertRaises(AttachmentError): ResourceLocks(self.root, enabled=True)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError): os.fstat(opened[0])
        finally:
            os.chmod(self.root, 0o700)

    def test_first_unlock_error_still_closes_every_fd_and_reconciles_count(self):
        keys = [self.key, 'copy:'+'b'*32]
        original = self.manager._flock
        releases, descriptors = [], []
        def faulty(fd, operation):
            if operation == self.manager._unlock:
                releases.append(fd)
                if len(releases) == 1: raise OSError('fictional unlock failure')
            return original(fd, operation)
        with mock.patch.object(self.manager, '_flock', side_effect=faulty):
            with self.assertRaises(AttachmentError) as error:
                with self.manager.hold(keys) as lease:
                    descriptors = [item[1] for item in lease._fds]
            self.assertEqual(error.exception.code, 'resource_release_failed')
        self.assertEqual(len(releases), 2)
        for fd in descriptors:
            with self.assertRaises(OSError): os.fstat(fd)
        self.assertEqual(self.manager._leases, 0)
        with self.manager.hold(keys, timeout=.1): pass

    def test_release_error_preserves_primary_failure_after_cleanup(self):
        original = self.manager._flock
        def faulty(fd, operation):
            if operation == self.manager._unlock: raise OSError('fictional unlock failure')
            return original(fd, operation)
        with mock.patch.object(self.manager, '_flock', side_effect=faulty):
            with self.assertRaisesRegex(RuntimeError, 'fictional primary') as error:
                with self.manager.hold([self.key]): raise RuntimeError('fictional primary')
        self.assertEqual(self.manager._leases, 0)
        self.assertTrue(error.exception.__notes__)
        with self.manager.hold([self.key], timeout=.1): pass

    def test_first_close_error_does_not_skip_other_descriptors(self):
        keys, descriptors, closed = [self.key, 'copy:'+'b'*32], [], []
        real_close = os.close
        def faulty(fd):
            if fd in descriptors:
                real_close(fd)
                closed.append(fd)
                if len(closed) == 1: raise OSError('fictional close result error')
            else:
                real_close(fd)
        patch = None
        try:
            with self.assertRaises(AttachmentError) as error:
                with self.manager.hold(keys) as lease:
                    descriptors = [item[1] for item in lease._fds]
                    patch = mock.patch('attachments_resources.os.close', side_effect=faulty)
                    patch.start()
            self.assertEqual(error.exception.code, 'resource_release_failed')
        finally:
            if patch is not None: patch.stop()
        self.assertEqual(set(closed), set(descriptors))
        self.assertEqual(self.manager._leases, 0)
        with self.manager.hold(keys, timeout=.1): pass

    def test_process_death_releases_lock_not_timer(self):
        code = ('from attachments_resources import ResourceLocks\nimport sys\n'
                'm=ResourceLocks(sys.argv[1],enabled=True)\n'
                'with m.hold([sys.argv[2]]):\n print("HELD",flush=True)\n sys.stdin.read(1)\n')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        p = subprocess.Popen([sys.executable, '-c', code, self.root, self.key], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), 'HELD')
            with self.assertRaises(AttachmentError):
                with self.manager.hold([self.key], timeout=.05): pass
            p.kill(); p.wait(timeout=2)
            with self.manager.hold([self.key], timeout=.1) as lease: lease.require([self.key])
        finally:
            if p.poll() is None: p.kill(); p.wait(timeout=2)
            p.stdin.close(); p.stdout.close(); p.stderr.close()

    def test_lock_file_replacement_symlink_and_disabled(self):
        with self.manager.hold([self.key]) as lease:
            path = next(Path(self.root).glob('*.lock'))
            path.rename(path.with_suffix('.old'))
            path.symlink_to(path.with_suffix('.old'))
            with self.assertRaises(AttachmentError): lease.require([self.key])
        with self.assertRaises(AttachmentError):
            with self.manager.hold([self.key]): pass
        missing = Path(self.root)/'never-created'
        disabled = ResourceLocks(str(missing))
        with self.assertRaises(AttachmentError):
            with disabled.hold([self.key]): pass
        self.assertFalse(missing.exists())
        disabled.close()


if __name__ == '__main__':
    unittest.main()
