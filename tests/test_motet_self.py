#!/usr/bin/env python3
"""Motet's self folder and its version history, against a temporary home only. Fictional file text."""
import ast
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import motet_self
import motet_self_history as history
import private_storage

FILES = {
    'SOUL.md': '## You are becoming someone.\n\nDefer to the owner.\n',
    'RULES.md': '## These are your rules.\n\n- Only the owner changes these rules.\n',
    'IDENTITY.md': '## You are Motet.\n',
    'VOCABULARY.md': '## How you speak\n\nShort sentences.\n',
    'ASKING_MOTET.md': '**Asking Motet**\n\nAsk once.\n',
    'skills/kick-off.md': '**Name:** Kick off a goal\n\n1. Hand it to Ada.\n',
}
POSIX = os.name != 'nt'


class SelfFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home = str(base / 'home')
        os.mkdir(self.home)
        self.source = base / 'prepared'
        (self.source / 'skills').mkdir(parents=True)
        for name, text in FILES.items():
            (self.source / name).write_bytes(text.encode('utf-8'))   # bytes: no newline translation
        self.root = Path(history.self_folder(self.home))
        self.pending = self.root / '.history' / history.PENDING

    def tearDown(self):
        self.tmp.cleanup()

    def install(self):
        return motet_self.init(str(self.source), home=self.home)

    def versions(self, name):
        return history.list_history(self.home, name)[name]

    def edit(self, name, text):
        # Rewrite in place, byte for byte, as an editor that keeps the file's permissions does.
        with open(self.root / name, 'r+b') as f:
            f.truncate(0)
            f.write(text.encode('utf-8'))

    def history_bytes(self):
        return {p: p.read_bytes() for p in (self.root / '.history').rglob('v*.json')}

    def text(self, name):
        return (self.root / name).read_bytes().decode('utf-8')

    # -- init ---------------------------------------------------------------------------------------------------

    def test_init_installs_each_copy_as_version_one_and_refuses_an_existing_folder(self):
        self.assertEqual(sorted(self.install()), sorted(FILES))
        for name, text in FILES.items():
            self.assertEqual(self.text(name), text)
            rows = self.versions(name)
            self.assertEqual([(r['version'], r['author'], r['reason']) for r in rows], [(1, 'owner', 'installed by init')])
        before = self.history_bytes()
        with self.assertRaisesRegex(history.SelfError, 'already exists'):
            self.install()
        self.assertEqual(self.history_bytes(), before)
        self.assertEqual(self.text('SOUL.md'), FILES['SOUL.md'])
        self.assertEqual([p.name for p in self.root.parent.iterdir()], ['motet-self'], 'no half-built folder is left')

    def test_init_needs_every_core_file_and_refuses_the_repository_templates(self):
        (self.source / 'RULES.md').unlink()
        with self.assertRaisesRegex(history.SelfError, 'RULES.md'):
            self.install()
        self.assertFalse(self.root.exists())
        (self.source / 'RULES.md').write_bytes(FILES['RULES.md'].encode('utf-8'))
        for marker in motet_self.TEMPLATE_ONLY:
            with self.subTest(marker=marker):
                (self.source / marker).write_text('template\n', encoding='utf-8')
                with self.assertRaisesRegex(history.SelfError, 'prepared copies'):
                    self.install()
                self.assertFalse(self.root.exists())
                (self.source / marker).unlink()
        with self.assertRaisesRegex(history.SelfError, 'INSTRUCTIONS.md'):
            motet_self.init(str(ROOT / 'motet'), home=self.home)
        self.assertFalse(self.root.exists())

    def test_folder_and_files_are_private_from_the_start(self):
        self.install()
        for path in [self.root.parent, self.root, *self.root.rglob('*')]:
            with self.subTest(path=path.name):
                private_storage.check_private(str(path), directory=path.is_dir(), single_link=not path.is_dir())
                if POSIX:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    @unittest.skipUnless(POSIX, 'POSIX modes')
    def test_a_folder_or_file_that_is_not_private_is_refused_not_repaired(self):
        live = Path(self.home) / '.live-room'
        live.mkdir()
        live.chmod(0o755)
        with self.assertRaisesRegex(history.SelfError, 'not a private folder'):
            self.install()
        self.assertFalse(self.root.exists())
        live.chmod(0o700)
        self.install()
        (self.root / 'SOUL.md').chmod(0o644)
        with self.assertRaises(history.SelfError):
            motet_self.scan(self.home)
        self.assertEqual(stat.S_IMODE((self.root / 'SOUL.md').stat().st_mode), 0o644)
        (self.root / 'SOUL.md').chmod(0o600)
        (self.root / 'SOUL.md').unlink()
        os.symlink(self.source / 'SOUL.md', self.root / 'SOUL.md')
        with self.assertRaises(history.SelfError):
            motet_self.scan(self.home)

    # -- scan, save, restore --------------------------------------------------------------------------------------

    def test_scan_records_a_direct_edit_once_and_finds_a_new_skill(self):
        self.install()
        self.edit('SOUL.md', FILES['SOUL.md'] + '\n- Be brief.\n')
        statuses = dict(motet_self.scan(self.home))
        self.assertEqual(statuses['SOUL.md'], 'recorded v2')
        self.assertEqual(statuses['RULES.md'], 'unchanged')
        rows = self.versions('SOUL.md')
        self.assertEqual((rows[-1]['author'], rows[-1]['reason']), ('owner', 'edited directly by the owner'))
        self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'unchanged')
        fd = private_storage.create_private_file(str(self.root / 'skills' / 'review-loop.md'))
        os.write(fd, b'**Name:** Review loop\n')
        os.close(fd)
        self.assertEqual(dict(motet_self.scan(self.home))['skills/review-loop.md'], 'new v1')

    @unittest.skipUnless(POSIX, 'POSIX modes')
    def test_scan_checks_every_file_before_recording_any(self):
        self.install()
        self.edit('SOUL.md', 'edited\n')
        self.edit('VOCABULARY.md', 'edited too\n')
        (self.root / 'VOCABULARY.md').chmod(0o644)
        before = self.history_bytes()
        with self.assertRaises(history.SelfError):
            motet_self.scan(self.home)
        self.assertEqual(self.history_bytes(), before, 'a refusal records nothing')
        (self.root / 'VOCABULARY.md').chmod(0o600)
        self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'recorded v2')

    def test_restore_round_trips_and_keeps_an_unrecorded_edit_first(self):
        self.install()
        motet_self.save('RULES.md', 'second\n', 'owner', 'tightened a rule', home=self.home)
        self.edit('RULES.md', 'third, typed by hand\n')
        self.assertEqual(motet_self.restore('RULES.md', 1, home=self.home), 4)
        self.assertEqual(self.text('RULES.md'), FILES['RULES.md'])
        rows = self.versions('RULES.md')
        self.assertEqual([r['reason'] for r in rows],
                         ['installed by init', 'tightened a rule', 'edited directly by the owner', 'restored from v1'])
        root = str(self.root)
        self.assertEqual(history.read_version(root, 'RULES.md', 3)['content'], 'third, typed by hand\n')
        self.assertEqual(history.read_version(root, 'RULES.md', 4)['content'], FILES['RULES.md'])
        motet_self.restore('RULES.md', 3, home=self.home)
        self.assertEqual(self.text('RULES.md'), 'third, typed by hand\n')
        with self.assertRaisesRegex(history.SelfError, 'no version 9'):
            motet_self.restore('RULES.md', 9, home=self.home)

    def test_crlf_and_lf_round_trip_byte_for_byte(self):
        crlf = '## Rules\r\n\r\n- Ask the owner first.\r\n'
        (self.source / 'RULES.md').write_bytes(crlf.encode('utf-8'))
        self.install()
        root = str(self.root)
        self.assertEqual((self.root / 'RULES.md').read_bytes(), crlf.encode('utf-8'))
        self.assertEqual((self.root / 'SOUL.md').read_bytes(), FILES['SOUL.md'].encode('utf-8'))
        self.assertEqual(history.read_version(root, 'RULES.md', 1)['content'], crlf)
        # A save keeps whichever line endings it is given.
        mixed = 'lf line\ncrlf line\r\nlone cr\rend'
        motet_self.save('RULES.md', mixed, 'owner', 'mixed endings', home=self.home)
        self.assertEqual((self.root / 'RULES.md').read_bytes(), mixed.encode('utf-8'))
        self.assertEqual(history.read_version(root, 'RULES.md', 2)['content'], mixed)
        # An editor that saves with CRLF is a genuine edit, recorded as it is.
        self.edit('SOUL.md', FILES['SOUL.md'].replace('\n', '\r\n'))
        self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'recorded v2')
        self.assertEqual(history.read_version(root, 'SOUL.md', 2)['content'], FILES['SOUL.md'].replace('\n', '\r\n'))
        # Restores reproduce the recorded bytes exactly, in both directions.
        motet_self.restore('SOUL.md', 1, home=self.home)
        self.assertEqual((self.root / 'SOUL.md').read_bytes(), FILES['SOUL.md'].encode('utf-8'))
        motet_self.restore('SOUL.md', 2, home=self.home)
        self.assertEqual((self.root / 'SOUL.md').read_bytes(), FILES['SOUL.md'].replace('\n', '\r\n').encode('utf-8'))
        motet_self.restore('RULES.md', 1, home=self.home)
        self.assertEqual((self.root / 'RULES.md').read_bytes(), crlf.encode('utf-8'))
        self.assertEqual(history.read_version(root, 'RULES.md', 3)['content'], crlf)
        self.assertEqual(dict(motet_self.scan(self.home))['RULES.md'], 'unchanged')
        self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'unchanged')
        # Version records and the latest marker are written byte for byte too.
        for path in (self.root / '.history').rglob('*.json'):
            with self.subTest(record=path.name):
                self.assertNotIn(b'\r\n', path.read_bytes().replace(b'\\r\\n', b''))

    def test_names_and_versions_are_strict(self):
        self.install()
        for name in ('../outside.md', 'skills/../SOUL.md', 'notes.md', '/etc/passwd', 'skills/Bad.md',
                     'skills/con.md', 'skills/nul.md', 'skills/com1.md', 'skills/lpt9.md', 'skills/aux.md'):
            with self.subTest(name=name), self.assertRaises(history.SelfError):
                motet_self.save(name, 'x', 'owner', 'no', home=self.home)
        self.assertTrue(history.skill_name('console.md'))
        self.assertEqual([history.parse_version(v) for v in ('3', 'v3', 'V12')], [3, 3, 12])
        for value in ('٣', '²', 'v', '0', '-1', '3.0', 'v1234567', ' 3'):
            with self.subTest(value=value), self.assertRaises(history.SelfError):
                history.parse_version(value)

    # -- append-only history and its chain ----------------------------------------------------------------------

    def test_history_is_append_only(self):
        self.install()
        before = self.history_bytes()
        motet_self.save('SOUL.md', 'v2\n', 'owner', 'a change', home=self.home)
        motet_self.restore('SOUL.md', 1, home=self.home)
        self.edit('IDENTITY.md', 'edited\n')
        motet_self.scan(self.home)
        after = self.history_bytes()
        for path, data in before.items():
            self.assertEqual(after[path], data)
        self.assertEqual([r['version'] for r in self.versions('SOUL.md')], [1, 2, 3])
        with self.assertRaisesRegex(history.SelfError, 'never rewritten'):
            motet_self._write(str(self.root / '.history' / 'SOUL.md'), 'v000001.json', b'{}', new=True)
        self.assertEqual(self.history_bytes(), after)

    def test_a_missing_or_changed_record_is_refused(self):
        self.install()
        motet_self.save('SOUL.md', 'v2\n', 'owner', 'a change', home=self.home)
        motet_self.save('SOUL.md', 'v3\n', 'owner', 'another', home=self.home)
        folder = self.root / '.history' / 'SOUL.md'
        newest = (folder / 'v000003.json').read_bytes()
        (folder / 'v000003.json').unlink()
        for attempt in (lambda: motet_self.save('SOUL.md', 'v4\n', 'owner', 'x', home=self.home),
                        lambda: motet_self.scan(self.home),
                        lambda: history.list_history(self.home, 'SOUL.md'),
                        lambda: motet_self.restore('SOUL.md', 1, home=self.home)):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(history.SelfError, 'does not match'):
                attempt()
        self.assertEqual(self.text('SOUL.md'), 'v3\n')
        self.assertFalse((folder / 'v000003.json').exists(), 'nothing was rebuilt behind the owner')
        fd = private_storage.create_private_file(str(folder / 'v000003.json'))
        os.write(fd, newest)
        os.close(fd)
        self.assertEqual(len(self.versions('SOUL.md')), 3)
        # A middle record changed by hand breaks the chain even when it still parses.
        middle = folder / 'v000002.json'
        changed = middle.read_bytes().replace(b'a change', b'b change')
        middle.unlink()
        fd = private_storage.create_private_file(str(middle))
        os.write(fd, changed)
        os.close(fd)
        with self.assertRaisesRegex(history.SelfError, 'does not match'):
            history.list_history(self.home, 'SOUL.md')
        (self.root / '.history' / 'IDENTITY.md' / 'latest.json').unlink()
        with self.assertRaisesRegex(history.SelfError, 'does not match'):
            history.list_history(self.home, 'IDENTITY.md')

    # -- interrupted saves ----------------------------------------------------------------------------------------

    def interrupted(self, fail_on):
        """Run a save whose fail_on-th file write (1 pending, 2 working file, 3 record, 4 latest marker) stops it."""
        real, calls = motet_self._write, []

        def write(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == fail_on:
                raise KeyboardInterrupt('stopped here')
            return real(*args, **kwargs)

        with patch.object(motet_self, '_write', write), self.assertRaises(KeyboardInterrupt):
            motet_self.save('SOUL.md', 'new text\n', 'owner', 'a careful change', home=self.home)
        shape = [c if c in (history.PENDING, 'SOUL.md', history.LATEST) else 'record' for c in calls]
        self.assertEqual(shape, [history.PENDING, 'SOUL.md', 'record', history.LATEST][:fail_on])

    def test_a_save_stopped_before_it_happens_leaves_the_previous_version_intact(self):
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on):
                self.tearDown()
                self.setUp()
                self.install()
                before = self.history_bytes()
                self.interrupted(fail_on)
                self.assertEqual(self.pending.exists(), fail_on == 2)
                self.assertEqual(self.text('SOUL.md'), FILES['SOUL.md'])
                self.assertEqual(self.history_bytes(), before)
                self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'unchanged')
                self.assertFalse(self.pending.exists())
                self.assertEqual(len(self.versions('SOUL.md')), 1)
                self.assertEqual([p.name for p in self.root.rglob('.tmp-*')], [])

    def test_a_save_stopped_after_it_happens_is_finished_by_the_next_change(self):
        for fail_on in (3, 4):
            with self.subTest(fail_on=fail_on):
                self.tearDown()
                self.setUp()
                self.install()
                self.interrupted(fail_on)
                self.assertTrue(self.pending.exists())
                self.assertEqual(self.text('SOUL.md'), 'new text\n')
                with self.assertRaisesRegex(history.SelfError, 'pending'):
                    history.list_history(self.home, 'SOUL.md')
                self.assertEqual(dict(motet_self.scan(self.home))['SOUL.md'], 'unchanged')
                rows = self.versions('SOUL.md')
                self.assertEqual([(r['version'], r['reason']) for r in rows],
                                 [(1, 'installed by init'), (2, 'a careful change')])

    def test_a_save_whose_last_cleanup_fails_still_reports_success(self):
        self.install()
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if os.path.basename(path) == history.PENDING:
                raise PermissionError('held by another program')
            return real(path, *args, **kwargs)

        with patch.object(motet_self.os, 'unlink', unlink):
            self.assertEqual(motet_self.save('SOUL.md', 'saved\n', 'owner', 'a change', home=self.home), 2)
        self.assertTrue(self.pending.exists())
        motet_self.settle(self.home)
        self.assertFalse(self.pending.exists())
        self.assertEqual([r['version'] for r in self.versions('SOUL.md')], [1, 2])
        self.assertEqual(self.text('SOUL.md'), 'saved\n')

    def test_a_failed_rename_leaves_the_working_file_and_no_temporary_file(self):
        self.install()
        real = os.replace

        def replace(source, target):
            if os.path.basename(target) == 'SOUL.md':
                raise OSError('disk full')
            return real(source, target)

        with patch.object(motet_self.os, 'replace', replace), self.assertRaises(OSError):
            motet_self.save('SOUL.md', 'new text\n', 'owner', 'a change', home=self.home)
        self.assertEqual(self.text('SOUL.md'), FILES['SOUL.md'])
        self.assertEqual([p.name for p in self.root.rglob('.tmp-*')], [])
        motet_self.scan(self.home)
        self.assertEqual(len(self.versions('SOUL.md')), 1)

    def test_an_odd_pending_file_is_refused_not_removed(self):
        self.install()
        self.pending.mkdir()
        with self.assertRaisesRegex(history.SelfError, 'folder sits where'):
            motet_self.scan(self.home)
        self.assertTrue(self.pending.is_dir())
        self.pending.rmdir()
        if POSIX:
            outside = Path(self.tmp.name) / 'elsewhere.json'
            outside.write_text('{}')
            os.symlink(outside, self.pending)
            with self.assertRaisesRegex(history.SelfError, 'link or not private'):
                motet_self.scan(self.home)
            self.assertTrue(self.pending.is_symlink())
            self.pending.unlink()
        fd = private_storage.create_private_file(str(self.pending))
        os.write(fd, b'not a record')
        os.close(fd)
        with self.assertRaisesRegex(history.SelfError, 'damaged.*aside by hand'):
            motet_self.scan(self.home)
        with self.assertRaisesRegex(history.SelfError, 'pending.*aside by hand'):
            history.list_history(self.home)
        self.assertTrue(self.pending.exists())

    def test_a_pending_record_left_in_the_same_run_is_named_plainly(self):
        self.install()
        self.edit('SOUL.md', 'one\n')
        self.edit('VOCABULARY.md', 'two\n')
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if os.path.basename(path) == history.PENDING:
                raise PermissionError('held by another program')
            return real(path, *args, **kwargs)

        with patch.object(motet_self.os, 'unlink', unlink):
            with self.assertRaisesRegex(history.SelfError, 'could not be cleared.*Recorded before it stopped: SOUL.md'):
                motet_self.scan(self.home)
        statuses = dict(motet_self.scan(self.home))
        self.assertEqual((statuses['SOUL.md'], statuses['VOCABULARY.md']), ('unchanged', 'recorded v2'))
        self.assertEqual([r['version'] for r in self.versions('SOUL.md')], [1, 2])

    def test_one_change_at_a_time_across_processes(self):
        self.install()
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); import private_storage as p\n'
             'fd = p.open_lock_file(sys.argv[2]); assert p.try_lock(fd); print("held", flush=True); sys.stdin.read()',
             str(ROOT / 'src'), str(self.root / '.history' / motet_self.LOCK)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            with self.assertRaisesRegex(history.SelfError, 'in progress'):
                motet_self.save('SOUL.md', 'x\n', 'owner', 'a change', home=self.home)
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assertEqual(self.text('SOUL.md'), FILES['SOUL.md'])
        self.assertEqual(motet_self.save('SOUL.md', 'x\n', 'owner', 'a change', home=self.home), 2)

    # -- no write path for the server -----------------------------------------------------------------------------

    def test_server_side_reading_has_no_write_path(self):
        tree = ast.parse((ROOT / 'src' / 'motet_self_history.py').read_text(encoding='utf-8'))
        modules = {n.func.value.id + '.' + n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                   and n.func.value.id in ('os', 'shutil', 'private_storage')}
        self.assertLessEqual(modules, {'os.read', 'os.close', 'os.listdir', 'private_storage.check_private',
                                       'private_storage.open_private_file'})
        called = {getattr(n.func, 'attr', getattr(n.func, 'id', None)) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for name in ('open', 'write', 'write_text', 'write_bytes', 'rename', 'unlink', 'mkdir'):
            self.assertNotIn(name, called)
        for path in (ROOT / 'src').rglob('*.py'):
            if path.name == 'motet_self.py':
                continue
            with self.subTest(module=str(path.relative_to(ROOT))):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertNotIn('motet_self', [a.name.split('.')[0] for a in node.names])
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotEqual((node.module or '').split('.')[0], 'motet_self')
                    elif isinstance(node, ast.Call):
                        func = getattr(node.func, 'attr', getattr(node.func, 'id', None))
                        if func in ('import_module', '__import__', 'spec_from_file_location', 'run_path'):
                            literals = [a.value for a in ast.walk(node) if isinstance(a, ast.Constant)
                                        and isinstance(a.value, str)]
                            self.assertFalse([v for v in literals if 'motet_self' in v and 'motet_self_history' not in v])
        self.install()
        listed = history.list_history(self.home)
        self.assertEqual(list(listed)[:5], list(history.CORE_FILES))
        self.assertNotIn('content', listed['SOUL.md'][0])

    def test_owner_command_against_a_temporary_home(self):
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        script = str(ROOT / 'scripts' / 'motet_self.py')

        def run(*args):
            return subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True, timeout=60)

        self.assertEqual(run('init', '--from', str(ROOT / 'motet')).returncode, 1)
        done = run('init', '--from', str(self.source))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('each as version 1', done.stdout)
        self.assertEqual(run('init', '--from', str(self.source)).returncode, 1)
        self.edit('SOUL.md', 'by hand\n')
        self.assertIn('recorded v2', run('scan').stdout)
        self.assertIn('Restored SOUL.md from v1 as v3.', run('restore', 'SOUL.md', 'v1').stdout)
        self.assertIn('restored from v1', run('history', 'SOUL.md').stdout)
        shown = subprocess.run([sys.executable, script, 'show', 'SOUL.md', '2'], env=env, capture_output=True, timeout=60)
        self.assertEqual(shown.stdout, b'by hand\n')
        self.assertEqual(run('show', 'notes.md', '1').returncode, 1)
        self.interrupted(4)
        listing = run('history', 'SOUL.md')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn('a careful change', listing.stdout, 'history settles a pending change first')


if __name__ == '__main__':
    unittest.main()
