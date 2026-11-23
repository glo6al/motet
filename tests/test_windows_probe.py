import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'windows_probe.py')
spec = importlib.util.spec_from_file_location('windows_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

CTX = {
    'profile': r'C:\Users\demo',
    'localappdata': r'C:\Users\demo\AppData\Local',
    'appdata': r'C:\Users\demo\AppData\Roaming',
    'user': 'demo',
    'pc': 'DEMO-PC',
}


class RedactionTests(unittest.TestCase):
    def test_profile_and_app_data_paths(self):
        r = probe.redact(r'C:\Users\demo\.codex\sessions', CTX)
        self.assertEqual(r, r'%USERPROFILE%\.codex\sessions')
        r = probe.redact(r'c:\users\DEMO\appdata\local\Programs\Codex\codex.exe', CTX)
        self.assertEqual(r, r'%LOCALAPPDATA%\Programs\Codex\codex.exe')
        self.assertEqual(probe.redact(r'C:\Users\demo\AppData\Roaming\Claude', CTX), r'%APPDATA%\Claude')

    def test_forward_slashes_and_doubled_backslashes(self):
        self.assertEqual(probe.redact('C:/Users/demo/.claude', CTX), '%USERPROFILE%/.claude')
        self.assertEqual(probe.redact('C:\\\\Users\\\\demo\\\\x', CTX), '%USERPROFILE%\\\\x')

    def test_user_and_pc_names_in_a_pipe_name(self):
        r = probe.redact(r'\\.\pipe\codex-demo-ipc and DEMO-PC', CTX)
        self.assertNotIn('demo', r.lower())
        self.assertIn('<user>', r)
        self.assertIn('<pc>', r)

    def test_short_user_name_does_not_mangle_words(self):
        ctx = dict(CTX, user='me', profile=r'C:\Users\me', localappdata='', appdata='')
        r = probe.redact('mesh pipe me-1 and %s' % r'C:\Users\me\x', ctx)
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
        self.assertNotIn('hunter22', probe.redact('password=hunter22', CTX))

    def test_ordinary_text_is_kept(self):
        self.assertEqual(probe.redact('codex-cli 0.1.2 found', CTX), 'codex-cli 0.1.2 found')


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
