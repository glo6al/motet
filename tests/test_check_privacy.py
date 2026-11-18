#!/usr/bin/env python3
"""Tests for scripts/check_privacy.py."""
import os
import re
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import check_privacy as cp


class PrivacyCheckTests(unittest.TestCase):
    def setUp(self):
        self.patterns = cp.build_privacy_patterns()

    def test_prohibited_file_paths(self):
        self.assertIsNotNone(cp.check_file_path('.env'))
        self.assertIsNotNone(cp.check_file_path('.env.local'))
        self.assertIsNotNone(cp.check_file_path('.claude/settings.json'))
        self.assertIsNotNone(cp.check_file_path('.gemini/config.json'))
        self.assertIsNotNone(cp.check_file_path('archive/notes.md'))
        self.assertIsNotNone(cp.check_file_path('docs/investigations/notes.md'))
        self.assertIsNotNone(cp.check_file_path('brand/HANDOFF.md'))
        self.assertIsNotNone(cp.check_file_path('brand/reference/notes.md'))
        self.assertIsNotNone(cp.check_file_path('secret.pem'))
        self.assertIsNotNone(cp.check_file_path('id_rsa'))
        self.assertIsNotNone(cp.check_file_path('foo/.DS_Store'))

        # Normal repository files should be permitted
        self.assertIsNone(cp.check_file_path('src/server.py'))
        self.assertIsNone(cp.check_file_path('README.md'))
        self.assertIsNone(cp.check_file_path('tests/test_server.py'))

    def test_private_doc_folders_are_prohibited_except_for_the_public_guide(self):
        for path in ('docs/plans/draft.md', 'docs/attachments/proof.md',
                     'docs/bridge/plan.md', 'DOCS/PLANS/draft.md',
                     'docs/bridge/recovery.md', 'docs/bridge/old/RECOVERY.md'):
            with self.subTest(path=path):
                self.assertIsNotNone(cp.check_file_path(path))
        self.assertIsNone(cp.check_file_path('docs/bridge/RECOVERY.md'))
        self.assertIsNone(cp.check_file_path('docs/fixtures/attachments/README.md'))

    def test_internal_names_and_note_numbers_are_checked_in_lines_and_names(self):
        patterns = cp.build_privacy_patterns(('orbit', 'BEACON', 'caf\u00e9'))
        for name in ('ORBIT', 'bEaCoN', 'orb\u0131t', 'cafe\u0301'):
            with self.subTest(name=name):
                self.assertEqual(cp.scan_text('Ask ' + name, 'docs/guide.md', patterns)[0].kind,
                                 'internal agent name')
                self.assertEqual(cp.path_findings('docs/' + name + '.md', patterns)[0].kind,
                                 'internal agent name in a file name')
        self.assertEqual(cp.scan_text('An orbital beaconing example', 'docs/guide.md', patterns), [])
        note = 'change-{}'.format(830154)
        self.assertEqual(cp.scan_text(note, 'docs/guide.md', self.patterns)[0].kind,
                         'internal note reference')
        self.assertEqual(cp.path_findings('docs/' + note + '.md', self.patterns)[0].kind,
                         'internal note reference in a file name')

    def test_safe_demo_paths_are_permitted(self):
        safe_diff = (
            "diff --git a/test.txt b/test.txt\n"
            "--- a/test.txt\n"
            "+++ b/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            "+path = '/Users/demo/Projects/Lighthouse/Makefile'\n"
            "+path2 = 'C:\\\\Users\\\\me\\\\Projects\\\\site'\n"
            "+threading.local()\n"
        )
        hits = cp.scan_diff_lines(safe_diff, self.patterns)
        self.assertEqual(hits, [])

    def test_personal_paths_are_detected(self):
        leak_diff = (
            "diff --git a/src/test.txt b/src/test.txt\n"
            "--- a/src/test.txt\n"
            "+++ b/src/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            "+path = '/Users/johnsmith/Documents/secret.txt'\n"
        )
        hits = cp.scan_diff_lines(leak_diff, self.patterns)
        self.assertTrue(len(hits) > 0)
        self.assertTrue(any('personal macOS user path' in h[2] for h in hits))

    def test_private_ip_is_detected(self):
        ip_diff = (
            "diff --git a/src/test.txt b/src/test.txt\n"
            "--- a/src/test.txt\n"
            "+++ b/src/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            "+endpoint = 'http://192.168.1.100:8080/api'\n"
        )
        hits = cp.scan_diff_lines(ip_diff, self.patterns)
        self.assertTrue(len(hits) > 0)
        self.assertTrue(any('private network IP' in h[2] for h in hits))

    def test_local_domain_in_commit_is_detected(self):
        host_diff = (
            "diff --git a/src/test.txt b/src/test.txt\n"
            "--- a/src/test.txt\n"
            "+++ b/src/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            "+host = 'my-macbook-pro.local'\n"
        )
        hits = cp.scan_diff_lines(host_diff, self.patterns)
        self.assertTrue(len(hits) > 0)
        self.assertTrue(any('.local' in h[2] for h in hits))


    def test_own_fixtures_and_ci_account_are_not_leaks(self):
        fixture = "+path = '/Users/johnsmith/Documents/secret.txt'"
        def found(line, path):
            return [label for label, rx in self.patterns if rx.search(cp.mask_safe(line, path))]
        self.assertEqual(found(fixture, 'tests/test_check_privacy.py'), [])
        self.assertTrue(found(fixture, 'tests/test_other.py'))
        self.assertTrue(found(fixture, 'tests/other/test_check_privacy.py'), 'only the exact file is exempt')
        self.assertTrue(found(fixture, 'test_check_privacy.py'))
        mixed = "+demo = '/Users/demo/site'; real = '/Users/johnsmith/site'"
        self.assertIn('personal macOS user path', found(mixed, 'src/test.txt'), 'an allowed token does not exempt the rest of its line')
        from unittest.mock import patch
        for account in ('runner', 'runneradmin'):
            with patch.object(cp.getpass, 'getuser', return_value=account), patch.object(cp.os.path, 'expanduser', return_value='/home/' + account):
                labels = [label for label, _ in cp.build_privacy_patterns()]
            self.assertFalse(any(label.startswith('personal username') for label in labels), account)
            self.assertTrue(any(label.startswith('personal home path') for label in labels), 'the home path is still checked')
        with patch.object(cp.getpass, 'getuser', return_value='fictional-person'):
            self.assertTrue(any(label.startswith('personal username') for label, _ in cp.build_privacy_patterns()))

    def test_diff_findings_carry_the_new_line_number(self):
        diff = (
            "diff --git a/src/test.txt b/src/test.txt\n"
            "--- a/src/test.txt\n"
            "+++ b/src/test.txt\n"
            "@@ -3,0 +4,2 @@\n"
            "+fine\n"
            "+endpoint = 'http://192.168.1.100:8080/api'\n"
        )
        self.assertEqual([(h.path, h.line) for h in cp.scan_diff_lines(diff, self.patterns)], [('src/test.txt', 5)])


class PrivacyCheckRepositoryTests(unittest.TestCase):
    """The real script against a throwaway Git repository holding fictional leaks."""
    LEAK = '/Users/johnsmith/Documents/plan.txt'
    SENTINEL = 'unrelated-sentinel-words'

    def setUp(self):
        import shutil, tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(self.repo, 'scripts'))
        shutil.copy(cp.__file__, os.path.join(self.repo, 'scripts', 'check_privacy.py'))
        self.env = dict(os.environ, HOME=self.tmp, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Fictional Author',
                        GIT_AUTHOR_EMAIL='author@example.com', GIT_COMMITTER_NAME='Fictional Author',
                        GIT_COMMITTER_EMAIL='author@example.com')
        for key in ('CI', 'GITHUB_ACTIONS'):
            self.env.pop(key, None)
        self.git('init', '-q')
        self.git('add', 'scripts/check_privacy.py')
        self.git('commit', '-q', '-m', 'Add the check')

    def git(self, *args):
        import subprocess
        subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', *args],
                       cwd=self.repo, env=self.env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, name, text):
        with open(os.path.join(self.repo, name), 'w', encoding='utf-8') as f:
            f.write(text)

    def check(self, *args, env=None, host=None):
        import subprocess
        command = [sys.executable, os.path.join('scripts', 'check_privacy.py'), *args]
        if host is not None:   # the machine name the operating system reports, injected
            command = [sys.executable, '-c', 'import runpy, socket, sys; host = sys.argv.pop(1); socket.gethostname = lambda: host; '
                       "sys.argv[0] = 'scripts/check_privacy.py'; runpy.run_path(sys.argv[0], run_name='__main__')", host, *args]
        run = subprocess.run(command, cwd=self.repo, env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding='utf-8', errors='replace', timeout=10)
        return run.returncode, run.stdout + run.stderr

    def test_report_names_the_kind_and_place_but_not_the_finding(self):
        self.write('notes.txt', 'first line\nsee %s and %s\n' % (self.LEAK, self.SENTINEL))
        self.git('add', 'notes.txt')
        code, out = self.check('--staged')
        self.assertEqual(code, 1)
        self.assertIn('[personal macOS user path] notes.txt:2', out)
        self.assertNotIn('johnsmith', out)
        self.assertNotIn(self.SENTINEL, out)
        self.git('commit', '-q', '-m', 'Add notes')
        code, out = self.check('--all', '--show', env=dict(self.env, CI='true'))
        self.assertEqual(code, 1)
        self.assertNotIn('johnsmith', out, '--show is ignored under CI')
        self.assertNotIn(self.SENTINEL, out)
        code, out = self.check('--all', '--show')
        self.assertIn(self.SENTINEL, out, '--show prints the line on a local terminal')

    def test_a_file_name_that_is_itself_a_finding_is_withheld(self):
        with unittest.mock.patch.object(cp.getpass, 'getuser', return_value='johnsmith'):
            hits = cp.path_findings('docs/johnsmith/notes.txt', cp.build_privacy_patterns())
            self.assertTrue(hits)
            self.assertEqual(cp.safe_location(cp.Hit('docs/johnsmith/notes.txt', 1, 'k', '', ''), cp.build_privacy_patterns()), '(file name withheld):1')
        os.makedirs(os.path.join(self.repo, 'archive'))
        self.write(os.path.join('archive', 'private-project.md'), 'plain words\n')
        self.git('add', '-f', 'archive/private-project.md')
        code, out = self.check('--staged')
        self.assertEqual(code, 1)
        self.assertIn('[prohibited file path: internal working history (archive/)] (file name withheld)', out)
        self.assertNotIn('private-project', out)

    def test_unicode_file_names_are_scanned(self):
        name = 'caf\u00e9 notes \u2014 draft.txt'
        self.write(name, 'see %s\n' % self.LEAK)
        self.git('add', name)
        code, out = self.check('--staged')
        self.assertEqual(code, 1, out)
        self.assertIn('[personal macOS user path]', out)
        self.git('commit', '-q', '-m', 'Add a draft')
        code, out = self.check('--all')
        self.assertEqual(code, 1, out)
        self.assertIn('[personal macOS user path]', out)
        self.assertIn('draft.txt:1', out)

    def test_range_scans_every_commit_not_only_the_net_change(self):
        import subprocess
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, env=self.env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.write('notes.txt', 'see %s\n' % self.LEAK)
        self.git('add', 'notes.txt')
        self.git('commit', '-q', '-m', 'Add notes')
        self.write('notes.txt', 'see the plan\n')
        self.git('commit', '-q', '-am', 'Tidy notes')
        code, out = self.check('--all')
        self.assertEqual(code, 0, out)
        code, out = self.check('--range', base + '..HEAD')
        self.assertEqual(code, 1, out)
        self.assertIn('[personal macOS user path]', out)
        self.git('commit', '-q', '--allow-empty', '-m', 'Mention %s' % self.LEAK)
        code, out = self.check('--range', 'HEAD~1..HEAD')
        self.assertEqual(code, 1, out)
        self.assertIn('commit message:1', out)
        self.assertNotIn('johnsmith', out)


    def test_private_release_material_is_blocked_even_if_removed_later(self):
        locations = ('docs/plans/notes.md', 'docs/attachments/proof.md', 'docs/bridge/plan.md')
        for path in locations:
            os.makedirs(os.path.dirname(os.path.join(self.repo, path)))
            self.write(path, 'Private working record\n')
        self.write('docs/bridge/RECOVERY.md', 'Public recovery instructions\n')
        name = 'Beacon'
        self.write('.git/motet-private-names', name + '\n')
        note = 'CHANGE-{}'.format(830154)
        self.write('public.md', 'Ask ' + name + '\n')
        self.git('add', 'docs', 'public.md')
        code, out = self.check('--staged')
        self.assertEqual(code, 1, out)
        self.assertIn('private planning and proof documents', out)
        self.assertIn('private bridge documents', out)
        self.assertIn('[internal agent name] public.md:1', out)
        self.assertNotIn('RECOVERY.md', out)
        self.git('commit', '-q', '-m', 'Add materials from ' + note)
        self.git('rm', '-q', *locations, 'public.md')
        self.git('commit', '-q', '-m', 'Keep only public material')
        code, out = self.check('--all')
        self.assertEqual(code, 0, out)
        code, out = self.check('--range', 'HEAD~2..HEAD')
        self.assertEqual(code, 1, out)
        self.assertIn('private planning and proof documents', out)
        self.assertIn('private bridge documents', out)
        self.assertRegex(out, r'\[internal agent name\] [a-f0-9]+ public\.md:1')
        self.assertRegex(out, r'\[internal note reference\] [a-f0-9]+ commit message:1')
        self.assertNotIn(note, out)
        self.assertNotIn(name, out)

    def test_private_names_file_is_optional_and_its_contents_are_withheld(self):
        name = 'Orbit'
        self.write(name + '.md', 'Ask ' + name + '\n')
        self.git('add', name + '.md')
        code, out = self.check('--all')
        self.assertEqual(code, 0, out)

        self.write('.git/motet-private-names', '\n' + name.lower() + '\n')
        code, out = self.check('--all', '--show', env=dict(self.env, CI='true'))
        self.assertEqual(code, 1, out)
        self.assertIn('[internal agent name in a file name] (file name withheld)', out)
        self.assertIn('[internal agent name] (file name withheld):1', out)
        self.assertNotIn(name, out)

        self.write('.git/motet-private-names', name + ' ' + self.SENTINEL + '\n')
        code, out = self.check('--all', '--show', env=dict(self.env, CI='true'))
        self.assertEqual(code, 1, out)
        self.assertIn('must be readable UTF-8 with one word per line', out)
        self.assertNotIn(name, out)
        self.assertNotIn(self.SENTINEL, out)

    def test_private_names_file_rejects_special_entries(self):
        path = os.path.join(self.repo, '.git', 'motet-private-names')
        os.mkdir(path)
        self.assertEqual(self.check('--all')[0], 1)
        os.rmdir(path)
        if os.name != 'nt' and hasattr(os, 'symlink'):
            os.symlink('missing-fictional-file', path)
            self.assertEqual(self.check('--all')[0], 1)
            os.unlink(path)
        if hasattr(os, 'mkfifo'):
            os.mkfifo(path)
            self.assertEqual(self.check('--all')[0], 1)

    def test_files_git_shows_as_binary_are_still_scanned(self):
        import subprocess
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, env=self.env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.write('.gitattributes', '*.dat binary\n')
        self.write('marked.dat', 'see %s\n' % self.LEAK)
        with open(os.path.join(self.repo, 'embedded-nul.txt'), 'wb') as f:
            f.write(b'header\0\nsee ' + self.LEAK.encode() + b'\n')
        self.git('add', '.gitattributes', 'marked.dat', 'embedded-nul.txt')
        code, out = self.check('--staged')
        self.assertEqual(code, 1, out)
        self.assertIn('[personal macOS user path] marked.dat:1', out)
        self.assertIn('[personal macOS user path] embedded-nul.txt:2', out)
        self.git('commit', '-q', '-m', 'Add data')
        self.git('rm', '-q', 'marked.dat', 'embedded-nul.txt')
        self.git('commit', '-q', '-m', 'Remove data')
        code, out = self.check('--all')
        self.assertEqual(code, 0, out)
        code, out = self.check('--range', base + '..HEAD')
        self.assertEqual(code, 1, out)
        self.assertIn('marked.dat:1', out)
        self.assertIn('embedded-nul.txt:2', out)

    @unittest.skipIf(os.name == 'nt', 'Windows file names cannot hold a newline')
    def test_each_finding_is_one_log_line_whatever_the_file_name(self):
        name = 'notes\n' + self.SENTINEL + '.txt'
        self.write(name, 'see %s\n' % self.LEAK)
        self.git('add', name)
        for args in (('--staged',), ('--all',)):
            if args == ('--all',):
                self.git('commit', '-q', '-m', 'Add notes')
            code, out = self.check(*args, env=dict(self.env, CI='true'))
            self.assertEqual(code, 1, out)
            lines = [line for line in out.splitlines() if self.SENTINEL in line]
            self.assertEqual(len(lines), 1, out)
            self.assertTrue(lines[0].startswith('  [personal macOS user path] notes\\n'), lines[0])
        self.assertEqual(cp.printable('a\u2028b\tc\x1b'), 'a\\u2028b\\tc\\x1b')

    def test_a_broken_comparison_branch_is_a_finding_and_a_missing_one_is_not(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        import subprocess
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, env=self.env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.git('update-ref', 'refs/remotes/origin/main', head)
        self.write('notes.txt', 'see %s\n' % self.LEAK)
        self.git('add', 'notes.txt')
        self.git('commit', '-q', '-m', 'Add notes')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('notes.txt:1', out)
        loose = os.path.join(self.repo, '.git', 'refs', 'remotes', 'origin', 'main')
        broken = {'a missing object': '1' * len(head) + '\n', 'malformed text': 'not an object id\n', 'an empty file': '',
                  'a symbolic ref to nothing': 'ref: refs/remotes/origin/fictional-missing\n'}
        for case, text in broken.items():
            with open(loose, 'w') as f:
                f.write(text)
            code, out = self.check()
            self.assertEqual(code, 1, case + ': ' + out)
            self.assertIn('origin/main could not be read', out, case)
        os.remove(loose)
        self.git('update-ref', 'refs/remotes/origin/main', head)
        self.git('pack-refs', '--all')
        self.assertFalse(os.path.exists(loose), 'Git must pack the comparison ref')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('notes.txt:1', out, 'a packed origin/main is compared too')

    def test_without_show_ref_exists_a_stored_but_unreadable_branch_is_still_a_finding(self):
        import subprocess
        real = subprocess.run
        def older_git(args, *rest, **kwargs):
            if 'show-ref' in args:
                return subprocess.CompletedProcess(args, 129, b'', b'')   # an option this Git does not know
            return real(args, *rest, **kwargs)
        loose = os.path.join(self.repo, '.git', 'refs', 'remotes', 'origin', 'main')
        with unittest.mock.patch.object(cp, 'ROOT', self.repo), unittest.mock.patch.object(cp.subprocess, 'run', older_git):
            self.assertIsNone(cp.comparison_base())
            os.makedirs(os.path.dirname(loose))
            with open(loose, 'w') as f:
                f.write('not an object id\n')
            with self.assertRaises(LookupError):
                cp.comparison_base()
            os.remove(loose)
            head = real(['git', 'rev-parse', 'HEAD'], cwd=self.repo, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
            with open(os.path.join(self.repo, '.git', 'packed-refs'), 'a') as f:
                f.write('%s refs/remotes/origin/main\n' % ('2' * len(head)))
            with self.assertRaises(LookupError):
                cp.comparison_base()

    def test_an_account_that_only_begins_with_a_fictional_name_is_found(self):
        patterns = cp.build_privacy_patterns()
        def found(line, path='src/test.txt'):
            return [label for label, rx in patterns if rx.search(cp.mask_safe(line, path))]
        for fictional in ('/Users/' + 'demo', '/home/' + 'me', 'C:\\Users\\' + 'demo'):
            self.assertEqual(found(fictional + '/site'), [], fictional)
            for real in (fictional + '-real', fictional + '_real', fictional + '\u00e9', fictional + '\u00e9real', fictional + '2',
                         fictional + '\u0301', fictional + '\u0332', fictional + 'e\u0301'):
                self.assertTrue(found(real + '/site'), real)
        self.assertTrue(found('/Users/' + 'johnsmith-real/site', 'tests/test_check_privacy.py'))
        self.assertTrue(found('/Users/' + '\u00e9lise/site'), 'an account may start with any letter')

    def test_an_accented_continuation_of_a_fictional_account_is_found_in_staged_and_history_scans(self):
        import subprocess
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, env=self.env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        composed, decomposed = 'dem\u00f3', 'demo\u0301'   # one fictional name, spelled both ways
        lines = []
        for root, kind in (('/Users/', 'personal macOS user path'), ('/home/', 'personal Linux home path'), ('C:\\Users\\', 'personal Windows user path')):
            for name in (composed, decomposed):
                lines.append((root + name + '/notes', kind))
        self.write('notes.txt', ''.join('see %s\n' % text for text, _ in lines))
        self.git('add', 'notes.txt')
        code, out = self.check('--staged')
        self.assertEqual(code, 1, out)
        for number, (_, kind) in enumerate(lines, 1):
            self.assertIn('[%s] notes.txt:%d' % (kind, number), out)
        self.git('commit', '-q', '-m', 'Add notes')
        code, out = self.check('--all')
        for number, (_, kind) in enumerate(lines, 1):
            self.assertIn('[%s] notes.txt:%d' % (kind, number), out)
        self.write('notes.txt', 'see the demo notes\n')
        self.git('commit', '-q', '-am', 'Tidy notes')
        code, out = self.check('--range', base + '..HEAD')
        self.assertEqual(code, 1, out)
        for number, (_, kind) in enumerate(lines, 1):
            self.assertRegex(out, r'\[%s\] [0-9a-f]{12} notes\.txt:%d\n' % (re.escape(kind), number))
        self.write('notes.txt', 'see %s\n' % ('/Users/' + 'demo/notes'))
        self.git('commit', '-q', '-am', 'Point at the demo')
        code, out = self.check('--range', 'HEAD~1..HEAD')
        self.assertEqual(code, 0, 'the exact fictional user is still accepted: ' + out)


    def test_a_decomposed_machine_name_is_found_in_either_spelling(self):
        import subprocess
        decomposed, composed = 'fictional-cafe\u0301-studio', 'fictional-caf\u00e9-studio'
        self.write('notes.txt', 'built on %s\nand on %s\n' % (decomposed, composed))
        self.git('add', 'notes.txt')
        blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.repo, env=self.env, input=b'plain words\n',
                              stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '100644,%s,%s.txt' % (blob, decomposed))   # the index keeps this exact spelling
        ci = dict(self.env, CI='true')
        for args in (('--staged', '--show'), ('--all', '--show')):
            code, out = self.check(*args, env=ci, host=decomposed + '.example')
            self.assertEqual(code, 1, out)
            self.assertIn('[machine hostname] notes.txt:1', out)
            self.assertIn('[machine hostname] notes.txt:2', out)
            self.assertIn('[machine hostname in a file name] (file name withheld)', out)
            for spelling in (decomposed, composed, 'studio'):
                self.assertNotIn(spelling, out, 'the machine name is withheld under CI')
            if args[0] == '--staged':
                self.git('commit', '-q', '-m', 'Add notes')
        code, out = self.check('--all', host='fictional-other-studio')
        self.assertEqual(code, 0, out)


if __name__ == '__main__':
    unittest.main()
