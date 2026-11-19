"""Fictional metadata/directory proof boundaries; no provider operations."""
from dataclasses import replace
import os
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from new_chat_inventory import KnownFolderInventory, validate_folder
from new_chat_types import NewChatError, VerifiedIdentity

NATIVE = '11111111-1111-4111-8111-111111111111'


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = self.root / 'fictional-project'
        self.folder.mkdir()
        self.source = self.root / 'native.jsonl'
        self.source.write_text('{}\n')
        st = self.source.stat()
        self.identity = VerifiedIdentity(NATIVE, str(self.folder), str(self.source), st.st_dev, st.st_ino, 'fixture-v1')
        self.rows = [self.identity]
        self.inventory = KnownFolderInventory(lambda: self.rows, b'a' * 32, home=self.root,
                                               state_root=self.root / 'private-state')

    def test_only_verified_identity_type_and_pinned_regular_source(self):
        self.rows = [{'cwd': str(self.folder)}, replace(self.identity, source_inode=1),
                     replace(self.identity, native_id='not-a-native-id'), self.identity]
        rows = self.inventory.refresh()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, str(self.folder))
        self.assertEqual(self.inventory.snapshot()[0]['provenance'][0]['native_id'], NATIVE)

    def test_distinct_same_named_folders_have_distinct_ids(self):
        other = self.root / 'other' / self.folder.name
        other.mkdir(parents=True)
        self.rows.append(replace(self.identity, cwd=str(other)))
        rows = self.inventory.refresh()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].label, rows[1].label)
        self.assertNotEqual(rows[0].id, rows[1].id)

    def test_stable_ids_and_revision_not_random_per_refresh(self):
        first = self.inventory.refresh()[0]
        self.assertEqual(self.inventory.refresh()[0], first)
        self.assertEqual(self.inventory.get(first.id, first.revision), first)

    def test_replacement_rejects_prepared_folder(self):
        first = self.inventory.refresh()[0]
        self.folder.rename(self.root / 'old-project')
        self.folder.mkdir()
        with self.assertRaises(NewChatError):
            validate_folder(first)
        with self.assertRaises(NewChatError):
            self.inventory.get(first.id, first.revision)

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires Windows privileges.')
    def test_symlink_resolves_to_shown_destination_and_retarget_retires_entry(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.folder, target_is_directory=True)
        self.rows = [replace(self.identity, cwd=str(alias))]
        first = self.inventory.refresh()[0]
        self.assertEqual(first.path, str(self.folder))
        other = self.root / 'other'
        other.mkdir()
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        with self.assertRaises(NewChatError):
            self.inventory.get(first.id, first.revision)

    @unittest.skipIf(os.name == 'nt', 'Creating file symlinks requires Windows privileges.')
    def test_source_replacement_or_symlink_cannot_preserve_inventory(self):
        self.inventory.refresh()
        self.source.rename(self.root / 'old-native.jsonl')
        self.source.symlink_to(self.root / 'old-native.jsonl')
        self.assertEqual(self.inventory.refresh(), [])

    def test_incomplete_oversize_or_failed_source_retires_cached_entries(self):
        self.inventory.maximum = 1
        self.assertEqual(len(self.inventory.refresh()), 1)
        self.rows = [self.identity, self.identity]
        self.assertEqual(self.inventory.refresh(), [])
        self.inventory.source = lambda: iter([self.identity, self.identity])
        self.assertEqual(self.inventory.refresh(), [])

    def test_registration_accepts_only_service_home_relative_expansion(self):
        literal = self.root / '$HOME'
        literal.mkdir()
        with patch('new_chat_inventory.os.path.expanduser', side_effect=AssertionError('Home was already injected.')) as home:
            saved = self.inventory.register('~/fictional-project')
            self.assertEqual(saved.path, str(self.folder))
            self.assertEqual(self.inventory.register('~//fictional-project'), saved)
            self.assertEqual(self.inventory.register('~/$HOME').path, str(literal))
            for path in ('~', '~fictional/fictional-project', '$HOME/fictional-project',
                         '${HOME}/fictional-project', '$(pwd)/fictional-project'):
                with self.subTest(path=path), self.assertRaises(NewChatError) as caught:
                    self.inventory.register(path)
                self.assertEqual(caught.exception.code, 'invalid_folder')
            home.assert_not_called()

    def test_registration_reports_regular_file_without_exposing_filesystem_errors(self):
        selected = self.root / 'fictional-file.txt'
        selected.write_text('Fictional content that must not be returned.')
        paths = [selected]
        if os.name != 'nt':
            alias = self.root / 'file-alias'
            alias.symlink_to(selected)
            paths.append(alias)
        for path in paths:
            with self.subTest(path=path), self.assertRaises(NewChatError) as caught:
                self.inventory.register(str(path))
            self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.message),
                             (400, 'invalid_folder', 'That is a file, not a folder.'))
        for path in (str(self.root / 'missing'), '/' + 'x' * 4096, '/fictional\x7f', '/fictional\ud800'):
            with self.subTest(path=path), self.assertRaises(NewChatError) as caught:
                self.inventory.register(path)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, 'invalid_folder'))
            self.assertNotIn(str(self.root), caught.exception.message)
        with patch('new_chat_inventory.Path.resolve', side_effect=PermissionError('/fictional/private-detail')):
            with self.assertRaises(NewChatError) as caught:
                self.inventory.register(str(self.folder))
        self.assertEqual(caught.exception.message,
                         'Motet could not verify that folder. Check the path and try again.')

    def test_registered_rows_survive_discovery_failure_but_not_directory_replacement(self):
        saved = self.inventory.register(str(self.folder))
        self.inventory.source = lambda: (_ for _ in ()).throw(OSError('Fictional discovery failure.'))
        self.assertEqual(self.inventory.refresh([saved]), [saved])
        self.assertEqual(self.inventory.get(saved.id, saved.revision, [saved]), saved)
        self.folder.rename(self.root / 'old-project')
        self.folder.mkdir()
        self.assertEqual(self.inventory.refresh([saved]), [])
        with self.assertRaises(NewChatError):
            self.inventory.get(saved.id, saved.revision, [saved])

    def test_private_roots_and_descendants_are_refused_without_a_temporary_directory_ban(self):
        roots = [self.root / name for name in ('.live-room', '.codex', '.claude', 'private-state')]
        for root in roots:
            child = root / 'nested' / 'project'
            child.mkdir(parents=True)
            for path in (root, child):
                with self.subTest(path=path), self.assertRaises(NewChatError) as caught:
                    self.inventory.register(str(path))
                self.assertEqual((caught.exception.code, caught.exception.message),
                                 ('invalid_folder', 'Motet cannot use that folder.'))
        for spelling in (str(self.root), '~/', '~/.codex/nested/project'):
            with self.subTest(spelling=spelling), self.assertRaises(NewChatError) as caught:
                self.inventory.register(spelling)
            self.assertEqual(caught.exception.message, 'Motet cannot use that folder.')
        for name in ('project', '.codex-project', '.claude-project', '.live-room-project', 'private-state-project'):
            project = self.root / name
            project.mkdir()
            self.assertEqual(self.inventory.register(str(project)).path, str(project))
        # This fixture itself is an explicit temporary project, outside private roots.
        self.assertEqual(self.inventory.register(str(self.folder)).path, str(self.folder))

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires Windows privileges.')
    def test_private_alias_retarget_and_replacement_cannot_keep_a_folder_eligible(self):
        saved = self.inventory.refresh()[0]
        private = self.root / '.codex'
        private.symlink_to(self.folder, target_is_directory=True)
        validate_folder(saved)  # The target's inode has not changed.
        with self.assertRaises(NewChatError):
            self.inventory.require_allowed(saved)
        self.assertEqual(self.inventory.refresh([saved]), [])
        with self.assertRaises(NewChatError):
            self.inventory.get(saved.id, saved.revision, [saved])
        alias = self.root / 'private-alias'
        alias.symlink_to(private, target_is_directory=True)
        with self.assertRaises(NewChatError):
            self.inventory.register(str(alias))
        private.unlink()
        state = self.root / 'private-state'
        state.mkdir()
        state.rename(self.root / 'former-state')
        state.mkdir()
        with self.assertRaises(NewChatError):
            self.inventory.register(str(state))

    @unittest.skipUnless(sys.platform == 'darwin', 'Physical directory spelling is a macOS capability.')
    def test_private_anchor_uses_physical_case(self):
        private = self.root / '.Claude'
        private.mkdir()
        if not (self.root / '.claude').exists():
            self.skipTest('This volume is case-sensitive.')
        with self.assertRaises(NewChatError) as caught:
            self.inventory.register(str(private))
        self.assertEqual(caught.exception.message, 'Motet cannot use that folder.')

    @unittest.skipUnless(sys.platform == 'darwin', 'Physical directory spelling is a macOS capability.')
    def test_private_case_alias_discovery_and_get_are_refused(self):
        private = self.root / '.claude'
        private.mkdir()
        alias = self.root / '.CLAUDE'
        if not alias.exists():
            self.skipTest('This volume is case-sensitive.')
        self.rows = [replace(self.identity, cwd=str(alias))]
        folder = self.inventory._folder(str(alias), alias.stat())
        self.assertEqual(self.inventory.refresh([folder]), [])
        with self.assertRaises(NewChatError):
            self.inventory.get(folder.id, folder.revision, [folder])

    def test_private_metadata_provenance_does_not_exclude_a_public_project(self):
        private = self.root / '.codex'
        private.mkdir()
        source = private / 'fictional-native.jsonl'
        self.source.rename(source)
        self.rows = [replace(self.identity, source_path=str(source))]
        self.assertEqual(self.inventory.refresh()[0].path, str(self.folder))

    def test_non_directory_provider_anchor_does_not_exclude_an_unrelated_project(self):
        for name in ('.codex', '.claude'):
            (self.root / name).write_text('Fictional non-directory state anchor.')
        self.assertEqual(self.inventory.register(str(self.folder)).path, str(self.folder))

    def test_genuine_codex_worktrees_root_and_projects_are_available(self):
        worktrees = self.root / '.codex' / 'worktrees'
        project = worktrees / 'orchard'
        project.mkdir(parents=True)
        saved = self.inventory.register(str(project))
        self.assertEqual(self.inventory.register(str(worktrees)).path, str(worktrees))
        self.rows = [replace(self.identity, cwd=str(project))]
        self.assertEqual(self.inventory.refresh([saved]), [saved])
        self.assertEqual(self.inventory.get(saved.id, saved.revision, [saved]), saved)
        for name in ('sessions', 'worktrees-backup', 'worktree'):
            private = worktrees.parent / name
            private.mkdir()
            with self.subTest(name=name), self.assertRaises(NewChatError):
                self.inventory.register(str(private))

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires Windows privileges.')
    def test_worktree_aliases_allow_projects_but_cannot_launder_private_roots(self):
        codex = self.root / '.codex'
        worktrees = codex / 'worktrees'
        project = worktrees / 'orchard'
        project.mkdir(parents=True)
        sessions = codex / 'sessions'
        sessions.mkdir()
        alias = self.root / 'project-alias'
        alias.symlink_to(project, target_is_directory=True)
        saved = self.inventory.register(str(project))
        self.assertEqual(self.inventory.register(str(alias)), saved)
        self.rows = [replace(self.identity, cwd=str(alias))]
        self.assertEqual(self.inventory.refresh(), [saved])
        escape = project / 'private-alias'
        escape.symlink_to(sessions, target_is_directory=True)
        with self.assertRaises(NewChatError):
            self.inventory.register(str(escape))
        former = codex / 'former-worktrees'
        worktrees.rename(former)
        for target in (codex, sessions):
            worktrees.symlink_to(target, target_is_directory=True)
            try:
                with self.subTest(target=target), self.assertRaises(NewChatError):
                    self.inventory.register(str(worktrees))
            finally:
                worktrees.unlink()
        former.rename(worktrees)
        for name in ('.claude', '.live-room', 'private-state'):
            private = self.root / name
            private.symlink_to(project, target_is_directory=True)
            try:
                with self.subTest(name=name), self.assertRaises(NewChatError):
                    self.inventory.register(str(project))
            finally:
                private.unlink()

    @unittest.skipUnless(sys.platform == 'darwin', 'Case-equivalent directory spellings require macOS.')
    def test_worktree_case_alias_uses_stat_identity_without_rewriting_the_frozen_folder(self):
        project = self.root / '.codex' / 'worktrees' / 'orchard'
        project.mkdir(parents=True)
        alias = self.root / '.CODEX' / 'WORKTREES' / 'ORCHARD'
        if not alias.exists():
            self.skipTest('This volume is case-sensitive.')
        self.rows = [replace(self.identity, cwd=str(alias))]
        folder = self.inventory.refresh()[0]
        before = (folder.id, folder.revision, folder.path, folder.device, folder.inode)
        self.assertIs(self.inventory.require_allowed(folder), folder)
        self.assertEqual((folder.id, folder.revision, folder.path, folder.device, folder.inode), before)
        self.assertEqual(self.inventory.get(folder.id, folder.revision), folder)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission semantics.')
    def test_unreadable_private_anchor_does_not_refuse_an_unrelated_project(self):
        private = self.root / '.codex'
        private.mkdir()
        private.chmod(0)
        try:
            self.assertEqual(self.inventory.register(str(self.folder)).path, str(self.folder))
            self.assertEqual(self.inventory.refresh()[0].path, str(self.folder))
        finally:
            private.chmod(0o700)
        child = private / 'private-project'
        child.mkdir()
        stat_call = os.stat

        def unavailable_anchor(path, *args, **kwargs):
            if os.fspath(path) == str(private):
                raise PermissionError('Fictional inaccessible anchor.')
            return stat_call(path, *args, **kwargs)

        # A stat failure is uncertainty, unlike a readable stat of a mode000 directory.
        with patch('new_chat_inventory.os.stat', side_effect=unavailable_anchor):
            self.assertEqual(self.inventory.refresh(), [])
            with self.assertRaises(NewChatError):
                self.inventory.register(str(child))

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission semantics.')
    def test_unresolved_private_anchor_cannot_hide_its_accessible_sibling_target(self):
        if os.geteuid() == 0:
            self.skipTest('Root bypasses the permission barrier used by this fixture.')
        home = self.root / 'private-home'
        home.mkdir()
        (home / '.claude').symlink_to(self.folder, target_is_directory=True)
        inventory = KnownFolderInventory(lambda: self.rows, b'f' * 32, home=home,
                                         state_root=self.root / 'private-state')
        folder = inventory._folder(str(self.folder), self.folder.stat())
        home.chmod(0)
        try:
            with self.assertRaises(NewChatError) as caught:
                inventory.require_allowed(folder)
            self.assertEqual(caught.exception.message, 'Motet cannot use that folder.')
            self.assertEqual(inventory.refresh([folder]), [])
        finally:
            home.chmod(0o700)

    def test_anchor_io_error_is_unresolved_but_proven_absence_is_not(self):
        private = self.root / '.claude'
        stat_call = os.stat

        def unavailable_anchor(path, *args, **kwargs):
            if os.fspath(path) == str(private):
                raise OSError(errno.EIO, 'Fictional metadata I/O failure.')
            return stat_call(path, *args, **kwargs)

        self.assertEqual(self.inventory.refresh()[0].path, str(self.folder))
        with patch('new_chat_inventory.os.stat', side_effect=unavailable_anchor):
            self.assertEqual(self.inventory.refresh(), [])

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks requires Windows privileges.')
    def test_unresolved_anchor_symlink_loop_is_not_proven_absence(self):
        private = self.root / '.claude'
        other = self.root / 'private-loop'
        private.symlink_to(other, target_is_directory=True)
        other.symlink_to(private, target_is_directory=True)
        self.assertEqual(self.inventory.refresh(), [])

    def test_refresh_and_get_never_open_project_or_private_directories(self):
        saved = self.inventory.register(str(self.folder))
        with patch('new_chat_inventory.os.open', side_effect=AssertionError('Refresh cannot open a directory.')) as opened:
            self.assertEqual(self.inventory.refresh([saved]), [saved])
            self.assertEqual(self.inventory.get(saved.id, saved.revision, [saved]), saved)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
