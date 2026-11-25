#!/usr/bin/env python3
"""Workspace copies and resource locks on their Windows branch.

The materializer copies an attachment into a chat's workspace folder (``.watch-attachments/<request>/``), keeps the
Git exclude rule for it, and removes the copy again. On macOS and Linux these tests run its Windows branch under
tests/windows_simulation.py (every private_storage call answered by the POSIX walk, os.getuid and fcntl failing
loudly). On real Windows they check what only Windows can: junctions, a workspace whose access list lets others
change it, a copy held open by another program, and the resource lock held by another process. Git's own check
(``git check-ignore``) is step 6c.5b, so it is left out here. Synthetic files in temporary folders only.

Run: python tests/test_attachments_windows_materialize.py
"""
from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import private_storage as ps
import attachments_materialize as materialize
from attachments_materialize import (Materializer, WorkspaceContext, FileIdentity, WorkspaceDirectories,
                                     relative_path, resource_keys, copy_identity)
from attachments_requests import RequestAuthority, CopyIntent
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError

SIMULATED_WHY = 'the simulation answers Windows calls with the POSIX walk'
REAL_WHY = 'requires real Windows handles, access lists and junctions'
CONTENT = b'Fictional orchard notes.\n'

# A second process holding one resource lock until it reads a line.
HOLDER = r'''
import sys
sys.path.insert(0, sys.argv[1])
from attachments_resources import ResourceLocks
locks = ResourceLocks(sys.argv[2], enabled=True)
with locks.hold([sys.argv[3]]):
    print('held', flush=True)
    sys.stdin.readline()
locks.close()
'''


class CopyChecks:
    """The same checks with Windows simulated (``windows`` is the simulation) or for real."""

    def windows(self):
        raise NotImplementedError

    def private_folder(self, path):
        raise NotImplementedError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='attachment-copy-windows-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.repo = self.base / 'orchard'
        for folder in (self.repo, self.repo / '.git', self.repo / '.git' / 'info', self.base / 'locks'):
            self.private_folder(folder)
        self.authority = RequestAuthority('a' * 32, 'b' * 64, 1, 'qualified-test', 'c' * 64, '9' * 64, 'd' * 64)
        self.spec = CopyIntent('e' * 32, self.authority.request_id, 'f' * 32, 1, 1, 1, 'workspace',
                               relative_path(self.authority.request_id, 'e' * 32, 'text/plain'), len(CONTENT),
                               hashlib.sha256(CONTENT).hexdigest(), 'text/plain', '1' * 64)
        self.recorded, self.created, self.partials = {}, [], []
        self.allow, self.cleanup = True, False
        with self.windows():
            self.locks = ResourceLocks(str(self.base / 'locks'), enabled=True)
            self.context = self.context_for(self.repo)
            source = self.base / 'source'
            fd = ps.create_private_file(str(source))
            try:
                os.write(fd, CONTENT)
            finally:
                os.close(fd)
            self.source_fd = ps.open_no_links(str(source), single_link=True, private=True)
            self.source_identity = copy_identity(materialize._regular_fd(self.source_fd))
        self.addCleanup(os.close, self.source_fd)
        self.addCleanup(self.close_locks)
        self.materializer = Materializer(enabled=True, validate_authority=self.validate,
                                         record_directory=self.record_directory, record_created=self.record_created,
                                         record_partial=self.record_partial)
        # Git's own check-ignore proof is step 6c.5b; everything around it runs.
        patcher = mock.patch.object(Materializer, '_prove_ignored', lambda *args: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_locks(self):
        with self.windows():
            self.locks.close()

    def identity(self, folder):
        with ps.open_folder(str(folder)) as held:
            return materialize._folder_identity(held)

    def context_for(self, folder):
        info = folder / '.git' / 'info'
        return WorkspaceContext('c' * 64, '9' * 64, str(folder), self.identity(folder), str(folder / '.git'),
                                self.identity(folder / '.git'), str(info), self.identity(info), sys.executable,
                                FileIdentity(1, 1, 0, 0, 1, 0, 0, 0))

    def validate(self, authority, phase, copies):
        return authority == self.authority and (self.cleanup if phase == 'cleanup' else self.allow)

    def record_directory(self, authority, role, identity, lease):
        self.recorded[role] = identity
        return True

    def record_created(self, copy_id, lease_id, identity, lease):
        self.created.append((copy_id, identity))
        return True

    def record_partial(self, partial, lease):
        self.partials.append(partial)
        return True

    def dirs(self):
        return WorkspaceDirectories.from_record({'root_identity': self.recorded.get('root'),
                                                 'request_identity': self.recorded.get('request')})

    def copied(self):
        """Exclusion, then the copy, as a Send makes them; returns the verified copy."""
        with self.windows(), self.locks.hold(resource_keys(self.context, self.authority, (self.spec,))) as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            return self.materializer.materialize(self.context, self.authority, self.spec, self.source_fd,
                                                 self.source_identity, lease)

    def remove(self, saved):
        self.cleanup = True
        with self.windows(), self.locks.hold(resource_keys(self.context, self.authority, (self.spec,))) as lease:
            return self.materializer.remove_copy(self.context, self.authority, self.spec, saved, lease,
                                                 directories=self.dirs())

    def code(self, call):
        with self.assertRaises(AttachmentError) as raised:
            call()
        return raised.exception.code

    def test_copy_into_the_workspace_is_verified_excluded_and_removed(self):
        verified = self.copied()
        copy = self.repo / self.spec.relative_path
        self.assertEqual(copy.read_bytes(), CONTENT)
        exclude = (self.repo / '.git' / 'info' / 'exclude').read_bytes()
        self.assertEqual(exclude.count(b'/.watch-attachments/' + self.authority.request_id.encode() + b'/'), 1)
        self.assertEqual(set(self.recorded), {'root', 'request'})
        self.assertEqual(self.created[0][1][2], 0)                    # recorded before any byte was written
        with self.windows(), self.locks.hold(resource_keys(self.context, self.authority, (self.spec,))) as lease:
            again = self.materializer.verify_copy(self.context, self.authority, self.spec, verified, lease,
                                                  directories=self.dirs())
            self.assertEqual(again.identity, verified.identity)
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
        self.assertEqual((self.repo / '.git' / 'info' / 'exclude').read_bytes(), exclude)   # the rule once only
        self.assertEqual(self.remove(verified).result, 'removed')
        self.assertFalse(copy.exists())
        self.assertEqual(self.remove(verified).result, 'absent')

    def test_a_source_that_can_be_written_is_refused(self):
        with self.windows():
            writable = ps.open_private_rw(str(self.base / 'source'))
        self.addCleanup(os.close, writable)
        with self.windows(), self.locks.hold(resource_keys(self.context, self.authority, (self.spec,))) as lease:
            self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
            self.assertFalse(ps.read_only(writable))
            self.assertTrue(ps.read_only(self.source_fd))
            self.assertEqual(self.code(lambda: self.materializer.materialize(
                self.context, self.authority, self.spec, writable, self.source_identity, lease)), 'source_changed')

    # The exclusion step reads the ignore files in those folders first, so the refusal can come from there.
    REFUSED = ('exclusion_unavailable', 'copy_failed', 'copy_changed')

    def test_a_link_in_place_of_the_attachment_or_request_folder_is_refused(self):
        elsewhere = self.base / 'elsewhere'
        self.private_folder(elsewhere)
        self.link_folder(self.repo / materialize.ROOT, elsewhere)
        self.assertIn(self.code(self.copied), self.REFUSED)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.unlink_folder(self.repo / materialize.ROOT)
        self.recorded.clear()
        self.private_folder(self.repo / materialize.ROOT)
        # A recorded root, then a link in place of the request folder.
        with self.windows():
            self.recorded['root'] = tuple(self.windows_identity(self.repo / materialize.ROOT))
        self.link_folder(self.repo / materialize.ROOT / self.authority.request_id, elsewhere)
        directories = WorkspaceDirectories.from_record({'root_identity': self.recorded['root'],
                                                        'request_identity': None})
        with self.windows(), self.locks.hold(resource_keys(self.context, self.authority, (self.spec,))) as lease:
            def attempt():
                self.materializer.ensure_exclusion(self.context, self.authority, (self.spec,), lease)
                self.materializer.materialize(self.context, self.authority, self.spec, self.source_fd,
                                              self.source_identity, lease, directories=directories)
            self.assertIn(self.code(attempt), self.REFUSED)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def windows_identity(self, folder):
        with ps.open_folder(str(folder), private=True) as held:
            return held.identity()[:2]

    def test_a_workspace_others_can_change_is_refused_with_a_plain_reason(self):
        self.open_to_others(self.repo)
        with self.windows():
            with self.assertRaises(AttachmentError) as raised:
                self.context_for(self.repo)
        self.assertEqual(raised.exception.code, 'workspace_unsafe')
        self.assertIn('Other people using this computer can change the selected workspace folder', str(raised.exception))
        self.assertIn('as a folder others can write to does on a Mac', str(raised.exception))

    def test_a_copy_held_open_elsewhere_is_left_and_retried_later(self):
        verified = self.copied()
        copy = self.repo / self.spec.relative_path
        with self.held_open(copy):
            self.assertEqual(self.code(lambda: self.remove(verified)), 'cleanup_busy')
            self.assertTrue(copy.exists())
        self.assertEqual(self.remove(verified).result, 'removed')       # retried once it is closed

    def test_the_exclude_file_stays_readable_and_writers_are_serialised_by_the_workspace_lock(self):
        self.copied()
        exclude = self.repo / '.git' / 'info' / 'exclude'
        workspace = 'workspace:' + self.context.workspace_fingerprint[:32]
        with self.windows(), self.locks.hold([workspace]):
            self.assertIn(b'/.watch-attachments/', exclude.read_bytes())     # Git can read it meanwhile
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(HERE.parent / 'src'), str(self.base / 'locks'),
                                   workspace], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is not None or holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        with self.windows():
            with self.assertRaises(AttachmentError) as raised:
                with self.locks.hold(resource_keys(self.context, self.authority, (self.spec,)), timeout=.2):
                    pass
        self.assertEqual(raised.exception.code, 'resource_busy')
        holder.communicate('release\n', timeout=60)
        with self.windows(), self.locks.hold([workspace], timeout=10):
            pass


@unittest.skipIf(os.name == 'nt', SIMULATED_WHY)
class SimulatedWindowsCopyTests(CopyChecks, unittest.TestCase):
    """With Windows simulated. A POSIX symbolic link stands in for a junction, mode 0777 for an access list that lets
    others change the workspace, and a 'busy' answer from the held folder's remove for a copy another program has
    open; real Windows checks those for real (WindowsCopyTests)."""

    def windows(self):
        from windows_simulation import simulated_windows
        return simulated_windows()

    def private_folder(self, path):
        os.mkdir(path, 0o700)

    def link_folder(self, path, target):
        os.symlink(target, path, target_is_directory=True)

    def unlink_folder(self, path):
        os.remove(path)

    def open_to_others(self, path):
        os.chmod(path, 0o777)

    @contextmanager
    def held_open(self, path):
        with mock.patch.object(ps.NoLinkFolder, 'remove', lambda self, name, expect: 'busy'):
            yield

    def test_windows_identities_are_read_from_handles(self):
        from windows_simulation import SERIAL
        self.copied()
        self.assertEqual(self.created[0][1][0], SERIAL)
        self.assertEqual(self.recorded['root'][0], SERIAL)


def junction(link, target):
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True, timeout=30)


@unittest.skipUnless(os.name == 'nt' and ps.folder_walk_available(), REAL_WHY)
class WindowsCopyTests(CopyChecks, unittest.TestCase):
    """On real NTFS, in folders made private and owned by this user (the runner is elevated, so a plain new folder
    would belong to Administrators)."""

    def windows(self):
        return nullcontext()

    def private_folder(self, path):
        ps.make_private_folder(str(path))

    def link_folder(self, path, target):
        junction(path, target)

    def unlink_folder(self, path):
        os.rmdir(path)

    def open_to_others(self, path):
        """What a folder made directly under a drive inherits: Authenticated Users may modify it."""
        subprocess.run(['icacls', str(path), '/grant', '*S-1-5-11:(OI)(CI)M'], check=True, capture_output=True,
                       timeout=30)

    @contextmanager
    def held_open(self, path):
        """Another program with the copy open without sharing delete (Python's open() shares reading and writing
        only), as an editor or scanner may hold it."""
        with open(path, 'rb'):
            yield


if __name__ == '__main__':
    unittest.main()
