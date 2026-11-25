#!/usr/bin/env python3
"""Sending files to a Claude chat on Windows: finding Git, the workspace check, and a whole copy with Git's proof.

On macOS and Linux these run the Windows branches under tests/windows_simulation.py, with /usr/bin/git standing in
for Git for Windows (root's, which the simulation's POSIX checks treat as the system's). On real Windows they use
the Git for Windows the runner has under Program Files. The whole route through the owner HTTP server and a
fictional Claude chat is tests/test_claude_attachment_integration.py, which also runs on Windows. Synthetic files in
temporary folders only.

Run: python tests/test_attachments_windows_delivery.py
"""
from contextlib import nullcontext
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
import attachments_windows_git as wingit
import attachments_claude_service as service
from attachments_materialize import Materializer, WorkspaceDirectories, relative_path, resource_keys, copy_identity
import attachments_materialize as materialize
from attachments_requests import RequestAuthority, CopyIntent
from attachments_resources import ResourceLocks
from attachments_types import AttachmentError

CONTENT = b'%PDF-1.4\nFictional orchard attachment.\n%%EOF'
SYSTEM_GIT = '/usr/bin/git'
SIMULATED = (os.name != 'nt' and os.path.isfile(SYSTEM_GIT) and os.stat(SYSTEM_GIT).st_uid == 0
             and not os.stat(SYSTEM_GIT).st_mode & 0o022)


class DeliveryChecks:
    def windows(self):
        raise NotImplementedError

    def private_folder(self, path):
        raise NotImplementedError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='attachment-delivery-windows-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))

    def code(self, call):
        with self.assertRaises(wingit.GitUnavailable) as raised:
            call()
        return raised.exception.code

    def fake_git(self, folder):
        """A git program this user made: a file at Git\\mingw64\\bin\\git.exe under ``folder`` (never run)."""
        program = folder.joinpath(*wingit.PROGRAM)
        program.parent.mkdir(parents=True)
        program.write_bytes(b'fictional program')
        return program

    def test_git_missing_and_git_only_for_this_account_are_refused_plainly(self):
        local = self.base / 'local'
        with mock.patch.object(wingit, '_roots', return_value=[str(self.base / 'nothing-here')]), \
                mock.patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
            with self.windows():
                self.assertEqual(self.code(wingit.find), 'attachments_git_missing')
            self.fake_git(local / 'Programs')
            with self.windows():
                self.assertEqual(self.code(wingit.find), 'attachments_git_per_user')
                self.assertEqual(wingit.refusal(), ('attachments_git_per_user', wingit.WHY['attachments_git_per_user']))

    def test_a_git_this_user_can_change_is_refused_plainly(self):
        programs = self.base / 'programs'
        self.private_folder(programs)
        self.fake_git(programs)
        with mock.patch.object(wingit, '_roots', return_value=[str(programs)]):
            with self.windows():
                self.assertEqual(self.code(wingit.find), 'attachments_git_unsafe')

    def test_a_workspace_that_is_not_a_repository_is_refused(self):
        folder = self.base / 'notes'
        self.private_folder(folder)
        with self.windows():
            with self.assertRaises(AttachmentError) as raised:
                service._workspace(str(folder))
        self.assertEqual(raised.exception.code, 'destination_unavailable')


@unittest.skipUnless(SIMULATED, 'the simulation answers Windows calls with the POSIX walk, and needs a root-owned /usr/bin/git')
class SimulatedWindowsDeliveryTests(DeliveryChecks, unittest.TestCase):
    """With Windows simulated: /usr/bin stands in for Git for Windows' folder under Program Files."""

    def windows(self):
        from windows_simulation import simulated_windows
        return simulated_windows()

    def private_folder(self, path):
        os.mkdir(path, 0o700)

    def as_program_files(self):
        return mock.patch.multiple(wingit, PROGRAM=('bin', 'git'), _roots=mock.Mock(return_value=['/usr']))

    def test_the_system_git_is_found_and_checked(self):
        with self.as_program_files(), self.windows():
            self.assertEqual(wingit.find(), os.path.realpath(SYSTEM_GIT))
            self.assertIsNone(wingit.refusal())

    def test_a_copy_is_excluded_proved_ignored_by_git_copied_verified_and_removed(self):
        """The whole copy as a Send makes it, with Git's check-ignore and ls-files run by the Windows runner (reader
        threads, Git's own environment)."""
        repo = self.base / 'orchard'
        self.private_folder(repo)
        subprocess.run([SYSTEM_GIT, '-c', 'init.templateDir=', 'init', '-q', str(repo)], check=True, capture_output=True,
                       timeout=30, env={'PATH': '/usr/bin:/bin', 'HOME': str(self.base), 'GIT_CONFIG_NOSYSTEM': '1',
                                        'GIT_CONFIG_GLOBAL': '/dev/null'})
        (repo / '.git' / 'info').mkdir(exist_ok=True)
        locks = self.base / 'locks'
        self.private_folder(locks)
        source = self.base / 'source'
        source.write_bytes(CONTENT)
        source.chmod(0o600)
        with self.as_program_files(), self.windows():
            context = service._workspace(str(repo))
            self.assertEqual(context.git_executable, os.path.realpath(SYSTEM_GIT))
            resources = ResourceLocks(str(locks), enabled=True)
            source_fd = ps.open_no_links(str(source), single_link=True, private=True)
        try:
            authority = RequestAuthority('a' * 32, 'b' * 64, 1, 'qualified-test', context.fingerprint,
                                         context.workspace_fingerprint, 'd' * 64)
            spec = CopyIntent('e' * 32, authority.request_id, 'f' * 32, 1, 1, 1, 'workspace',
                              relative_path(authority.request_id, 'e' * 32, 'application/pdf'), len(CONTENT),
                              hashlib.sha256(CONTENT).hexdigest(), 'application/pdf', '1' * 64)
            recorded = {}
            materializer = Materializer(enabled=True, validate_authority=lambda *args: True,
                                        record_directory=lambda a, role, identity, lease: recorded.__setitem__(role, identity) or True,
                                        record_created=lambda *args: True, record_partial=lambda *args: True)
            with self.as_program_files(), self.windows():
                identity = copy_identity(materialize._regular_fd(source_fd))
                with resources.hold(resource_keys(context, authority, (spec,))) as lease:
                    materializer.ensure_exclusion(context, authority, (spec,), lease)
                    verified = materializer.materialize(context, authority, spec, source_fd, identity, lease)
                    directories = WorkspaceDirectories.from_record({'root_identity': recorded['root'],
                                                                    'request_identity': recorded['request']})
                    materializer.verify_copy(context, authority, spec, verified, lease, directories=directories)
            self.assertEqual((repo / spec.relative_path).read_bytes(), CONTENT)
            self.assertIn(b'/.watch-attachments/' + authority.request_id.encode() + b'/',
                          (repo / '.git' / 'info' / 'exclude').read_bytes())
            with self.as_program_files(), self.windows():
                with resources.hold(resource_keys(context, authority, (spec,))) as lease:
                    self.assertEqual(materializer.remove_copy(context, authority, spec, verified, lease,
                                                              directories=directories).result, 'removed')
            self.assertFalse((repo / spec.relative_path).exists())
        finally:
            os.close(source_fd)
            with self.windows():
                resources.close()


@unittest.skipUnless(os.name == 'nt' and ps.folder_walk_available(), 'requires real Windows and its Git for Windows')
class WindowsDeliveryTests(DeliveryChecks, unittest.TestCase):
    """On real Windows. The runner has Git for Windows installed for all users under Program Files."""

    def windows(self):
        return nullcontext()

    def private_folder(self, path):
        ps.make_private_folder(str(path))

    def test_git_for_windows_for_all_users_is_found_and_checked(self):
        program = wingit.find()
        self.assertTrue(program.lower().endswith(os.path.join('git', 'mingw64', 'bin', 'git.exe')))
        self.assertIsNone(wingit.refusal())
        result = subprocess.run([program, '--version'], env=wingit.environment(program), capture_output=True,
                                timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, b''))
        self.assertTrue(result.stdout.startswith(b'git version '))


if __name__ == '__main__':
    unittest.main()
