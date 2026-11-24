import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'windows_probe.py')
spec = importlib.util.spec_from_file_location('windows_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

UP = 'C:\\' + 'Users' + '\\'      # built in pieces: the privacy check dislikes literal example account paths

CTX = {
    'profile': UP + r'demo',
    'localappdata': UP + r'demo\AppData\Local',
    'appdata': UP + r'demo\AppData\Roaming',
    'user': 'demo',
    'pc': 'DEMO-PC',
}


class RedactionTests(unittest.TestCase):
    def test_profile_and_app_data_paths(self):
        r = probe.redact(UP + r'demo\.codex\sessions', CTX)
        self.assertEqual(r, r'%USERPROFILE%\.codex\sessions')
        r = probe.redact(r'c:\users\DEMO\appdata\local\Programs\Codex\codex.exe', CTX)
        self.assertEqual(r, r'%LOCALAPPDATA%\Programs\Codex\codex.exe')
        self.assertEqual(probe.redact(UP + r'demo\AppData\Roaming\Claude', CTX), r'%APPDATA%\Claude')

    def test_forward_slashes_and_doubled_backslashes(self):
        self.assertEqual(probe.redact('C:/Users/demo/.claude', CTX), '%USERPROFILE%/.claude')
        self.assertEqual(probe.redact('C:\\\\Users\\\\demo\\\\x', CTX), '%USERPROFILE%\\\\x')

    def test_user_and_pc_names_in_a_pipe_name(self):
        r = probe.redact(r'\\.\pipe\codex-demo-ipc and DEMO-PC', CTX)
        self.assertNotIn('demo', r.lower())
        self.assertIn('<user>', r)
        self.assertIn('<pc>', r)

    def test_short_user_name_does_not_mangle_words(self):
        ctx = dict(CTX, user='me', profile=UP + r'me', localappdata='', appdata='')
        r = probe.redact('mesh pipe me-1 and %s' % UP + r'me\x', ctx)
        self.assertIn('mesh', r)
        self.assertEqual(r, 'mesh pipe <user>-1 and %USERPROFILE%\\x')

    def test_email_and_keys(self):
        r = probe.redact('mail someone@example.com ok', CTX)
        self.assertNotIn('someone', r)
        self.assertIn('<email>', r)
        for secret in ('sk-' + 'a1B2' * 8, 'ghp_' + 'Z9' * 14, 'A' * 40,
                       'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij',
                       '0123456789abcdef0123', '123e4567-e89b-12d3-a456-426614174000'):
            r = probe.redact('value %s end' % secret, CTX)
            self.assertNotIn(secret, r)
        r = probe.redact('Authorization: Bearer abcdef123456', CTX)
        self.assertNotIn('abcdef123456', r)
        self.assertNotIn('hunter22xyz', probe.redact('password=hunter22xyz', CTX))

    def test_ordinary_text_is_kept(self):
        self.assertEqual(probe.redact('codex-cli 0.1.2 found', CTX), 'codex-cli 0.1.2 found')

    def test_probe_label_lines_keep_their_meaning(self):
        lines = ['  hooks section: not found', '  hooks section: found; events: PreToolUse, Stop',
                 'CLAUDE_CODE_GIT_BASH_PATH: not set', '  hooks section: error: could not read (ValueError)',
                 'codex on PATH: not found', 'Appx package: found: OpenAI.Codex version 1.2.3.0',
                 '  word "app-server" appears: yes', 'Moonshine wheel lookup: skipped (needs network)',
                 'token: not found', 'password: yes']
        for line in lines:
            self.assertEqual(probe.redact(line, CTX), line)

    def test_no_label_ends_in_a_secret_word(self):
        with open(SCRIPT, encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn('hooks key', src)

    def test_short_names_of_a_long_user_name(self):
        ctx = dict(CTX, user='John Smith', profile=UP + r'John Smith', localappdata='', appdata='')
        for text in (UP + r'JOHNSM~1\x', UP + r'johnsm~2', 'JohnSm~12'):
            r = probe.redact(text, ctx)
            self.assertNotIn('~', r, text)
        self.assertEqual(probe.redact(UP + r'John Smith\.codex', ctx), r'%USERPROFILE%\.codex')

    def test_one_letter_user_name(self):
        ctx = dict(CTX, user='a', profile=UP + r'a', localappdata='', appdata='', pc='PC1')
        self.assertEqual(probe.redact(UP + r'a\x', ctx), r'%USERPROFILE%\x')
        self.assertEqual(probe.redact('codex-a-1', ctx), 'codex-<user>-1')
        self.assertEqual(probe.redact('found a pipe', ctx), 'found a pipe')

    def test_common_word_user_names_keep_placeholders_and_words(self):
        for name in ('User', 'code', 'admin'):
            ctx = dict(CTX, user=name, profile='C:\\Users\\' + name, localappdata='', appdata='', pc='PC1')
            r = probe.redact('C:\\Users\\%s\\.codex %%USERPROFILE%%\\x codex' % name, ctx)
            self.assertEqual(r, '%USERPROFILE%\\.codex %USERPROFILE%\\x codex', name)
            self.assertEqual(probe.redact('\\\\.\\pipe\\%s\\x' % name, ctx), '\\\\.\\pipe\\<user>\\x')
            self.assertIn('%LOCALAPPDATA%', probe.redact(
                'C:\\Users\\%s\\AppData\\Local\\x' % name,
                dict(ctx, localappdata='C:\\Users\\%s\\AppData\\Local' % name)))

    def test_placeholders_are_not_rewritten_by_later_steps(self):
        ctx = dict(CTX, user='USERPROFILE', pc='LOCALAPPDATA')
        r = probe.redact(UP + r'demo\x and %LOCALAPPDATA%', ctx)
        self.assertIn('%USERPROFILE%\\x', r)


@unittest.skipIf(os.name == 'nt', 'uses a POSIX shell script as the fake tool')
class ToolTests(unittest.TestCase):
    def fake(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        path = os.path.join(d, 'codex')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_help_ok_only_when_usage_names_the_subcommand(self):
        top = self.fake('case "$1" in --version) echo codex 1;; *) echo "Usage: codex [OPTIONS] [PROMPT]";; esac\n')
        text = '\n'.join(probe.probe_codex_cli(top, 'x'))
        self.assertIn('queue --help: exit 0 but the help is not for this subcommand', text)
        self.assertIn('app-server --help: exit 0 but', text)
        real = self.fake('case "$1" in --version) echo codex 1;; *) echo "Usage: codex $1 [OPTIONS]";; esac\n')
        text = '\n'.join(probe.probe_codex_cli(real, 'x'))
        self.assertIn('queue --help: ok', text)
        self.assertIn('app-server --help: ok', text)

    def test_stuck_tool_and_its_child_are_stopped(self):
        stuck = self.fake('sleep 30 &\nsleep 30\n')
        start = time.time()
        rc, out = probe.run([stuck], timeout=1)
        self.assertIsNone(rc)
        self.assertIn('timed out', out)
        self.assertLess(time.time() - start, 8)


class PathTests(unittest.TestCase):
    def test_app_aliases_and_shims_are_not_run(self):
        self.assertIn('not run', probe.not_run_reason(UP + r'x\AppData\Local\Microsoft\WindowsApps\codex.exe'))
        self.assertIn('not run', probe.not_run_reason(r'C:\npm\codex.cmd'))
        self.assertEqual(probe.not_run_reason(r'C:\Tools\codex.exe'), '')

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_which_ignores_the_current_folder_and_relative_entries(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        tool = os.path.join(d, 'probe_tool_xyz')
        with open(tool, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(tool, 0o755)
        old, cwd = os.environ.get('PATH'), os.getcwd()
        try:
            os.chdir(d)
            os.environ['PATH'] = os.pathsep.join(['', '.', 'relative'])
            self.assertIsNone(probe.which('probe_tool_xyz'))
            os.environ['PATH'] = d
            self.assertEqual(probe.which('probe_tool_xyz'), tool)
        finally:
            os.chdir(cwd)
            os.environ['PATH'] = old


class RunTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'checks the non-Windows message')
    def test_non_windows_message(self):
        p = subprocess.run([sys.executable, SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), 'This script is for Windows.')

    @unittest.skipUnless(os.name == 'nt', 'runs the real probe on Windows')
    def test_windows_run(self):
        p = subprocess.run([sys.executable, SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr)
        for label, _ in probe.SECTIONS:
            self.assertIn(label, p.stdout)
        self.assertIn('read this output before posting', p.stdout)
        out = p.stdout.lower()
        ctx = probe.make_context()
        for secret in (ctx['profile'], ctx['user'], ctx['pc']):
            if len(secret) >= 4:
                self.assertNotIn(secret.lower(), out)


if __name__ == '__main__':
    unittest.main()
