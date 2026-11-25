import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

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
        ctx = dict(CTX, user='John Smith', profile=r'D:\Profiles\John Smith', localappdata='', appdata='')
        for text in (r'D:\Profiles\JOHNSM~1\x', r'D:\Profiles\johnsm~2', 'JohnSm~12'):
            r = probe.redact(text, ctx)
            self.assertNotIn('~', r, text)
        self.assertEqual(probe.redact(r'D:\Profiles\John Smith\.codex', ctx), r'%USERPROFILE%\.codex')

    def test_one_letter_user_name(self):
        ctx = dict(CTX, user='a', profile=r'D:\Profiles\a', localappdata='', appdata='', pc='PC1')
        self.assertEqual(probe.redact(r'D:\Profiles\a\x', ctx), r'%USERPROFILE%\x')
        self.assertEqual(probe.redact('codex-a-1', ctx), 'codex-<user>-1')
        self.assertEqual(probe.redact('found a pipe', ctx), 'found a pipe')

    def test_common_word_user_names_keep_placeholders_and_words(self):
        for name in ('User', 'code', 'admin'):
            ctx = dict(CTX, user=name, profile='D:\\Profiles\\' + name, localappdata='', appdata='', pc='PC1')
            r = probe.redact('D:\\Profiles\\%s\\.codex %%USERPROFILE%%\\x codex' % name, ctx)
            self.assertEqual(r, '%USERPROFILE%\\.codex %USERPROFILE%\\x codex', name)
            self.assertEqual(probe.redact('\\\\.\\pipe\\%s\\x' % name, ctx), '\\\\.\\pipe\\<user>\\x')
            self.assertIn('%LOCALAPPDATA%', probe.redact(
                'D:\\Profiles\\%s\\AppData\\Local\\x' % name,
                dict(ctx, localappdata='D:\\Profiles\\%s\\AppData\\Local' % name)))

    def test_placeholders_are_not_rewritten_by_later_steps(self):
        ctx = dict(CTX, user='USERPROFILE', pc='LOCALAPPDATA')
        r = probe.redact(r'C:\Users\demo\x and %LOCALAPPDATA%', ctx)
        self.assertIn('%USERPROFILE%\\x', r)

    def test_names_of_any_length_inside_other_text(self):
        def ctx_for(name):
            return dict(CTX, user=name, profile='D:\\Profiles\\' + name, localappdata='', appdata='', pc='PC1')
        cases = {'andy': ['cursor-andy2-sock', 'andy123'], 'Mike': ['MikeRoss'],
                 'anna': ['anna7'], 'bob': ['Claude (bob)', 'x "bob" y', 'mcp:bob:sock', 'bob1']}
        for name, texts in cases.items():
            for text in texts:
                r = probe.redact(text, ctx_for(name))
                self.assertNotIn(name.lower(), r.lower(), text)
                self.assertIn('<user>', r)
        self.assertEqual(probe.redact('bobby and Bobcat', ctx_for('bob')), 'bobby and Bobcat')

    def test_assigned_secrets(self):
        for text in ('secret: not-a-secret-value', 'password: none-of-your-business', 'token=abc',
                     'password=x', 'api_key: ab', '"token": "abc"'):
            r = probe.redact(text, CTX)
            self.assertIn('<key>', r, text)
            self.assertNotIn('abc', r.replace('api_key', ''), text)
        for text in ('token: not found', 'secret: found', 'password: yes', 'token: none', 'token: not set (x)',
                     'password: skipped; later'):
            self.assertEqual(probe.redact(text, CTX), text)

    def test_user_name_variants(self):
        ctx = dict(CTX, user='john_smith', profile=r'D:\Profiles\john_smith', localappdata='', appdata='')
        self.assertEqual(probe.redact('JOHN_S~1', ctx), '<user>')
        self.assertEqual(probe.redact('john_s~2', ctx), '<user>')
        self.assertEqual(probe.redact('x-JOHN_S~10-y', ctx), 'x-<user>-y')
        ctx = dict(CTX, user='John Smith', profile=r'D:\Profiles\John Smith', localappdata='', appdata='')
        for text in ('JohnSmith', 'John.Smith', 'John%20Smith', 'john_smith', 'John-Smith', 'pipe-JohnSmith-1'):
            r = probe.redact(text, ctx)
            self.assertIn('<user>', r, text)
            self.assertNotIn('john', r.lower(), text)


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
        self.assertIn('not run', probe.not_run_reason(r'C:\Users\demo\AppData\Local\Microsoft\WindowsApps\codex.exe'))
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

    def test_which_on_windows_ignores_extensionless_script(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        for n in ('claude', 'claude.cmd'):
            with open(os.path.join(d, n), 'w') as f:
                f.write('x')
        old = {k: os.environ.get(k) for k in ('PATH', 'PATHEXT')}
        try:
            os.environ['PATH'] = d
            os.environ['PATHEXT'] = '.com;.exe;.bat;.cmd'
            self.assertEqual(probe.which('claude', windows=True), os.path.join(d, 'claude.cmd'))
            os.remove(os.path.join(d, 'claude.cmd'))
            self.assertIsNone(probe.which('claude', windows=True))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def sid_bytes(text):
    parts = [int(x) for x in text.split('-')[2:]]
    return bytes([1, len(parts) - 1]) + parts[0].to_bytes(6, 'big') + b''.join(x.to_bytes(4, 'little') for x in parts[1:])


def ace(kind, flags, mask, sid, extra=b''):
    body = mask.to_bytes(4, 'little') + sid_bytes(sid) + extra
    return bytes([kind, flags]) + (4 + len(body)).to_bytes(2, 'little') + body


def acl(*aces):
    body = b''.join(aces)
    return bytes([2, 0]) + (8 + len(body)).to_bytes(2, 'little') + len(aces).to_bytes(2, 'little') + b'\0\0' + body


ME = 'S-1-5-21-1111111111-2222222222-3333333333-1001'
INSTALLER = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
MANIFEST = b"""<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3">
  <Applications>
    <Application Id="App" Executable="app\\ChatGPT.exe" EntryPoint="Windows.FullTrustApplication">
      <Extensions>
        <uap:Extension Category="windows.protocol"><uap:Protocol Name="codex"/></uap:Extension>
        <uap3:Extension Category="windows.appExecutionAlias">
          <uap3:AppExecutionAlias><uap3:ExecutionAlias Alias="fictional-alias.exe"/></uap3:AppExecutionAlias>
        </uap3:Extension>
      </Extensions>
    </Application>
  </Applications>
</Package>"""


class ProbeV2Tests(unittest.TestCase):
    def test_sids_in_words(self):
        self.assertEqual(probe.sid_text(sid_bytes('S-1-5-32-544')), 'S-1-5-32-544')
        self.assertEqual(probe.sid_text(sid_bytes(ME)), ME)
        self.assertEqual(probe.sid_label(ME, ME), 'you')
        self.assertEqual(probe.sid_label('S-1-1-0', ME), 'Everyone (S-1-1-0)')
        self.assertEqual(probe.sid_label('S-1-5-21-1111111111-2222222222-3333333333-1002', ME),
                         'another account (S-1-5-21-...-1002)')
        self.assertEqual(probe.sid_label('S-1-15-2-123-456-789-1-2-3-4', ME), 'an app package (S-1-15-2-...)')
        self.assertEqual(probe.sid_label('S-1-5-5-0-12345', ME), 'this logon session (S-1-5-5-...)')
        for bad in (b'', b'\x02\x01' + b'\0' * 10, b'\x01\x05' + b'\0' * 8):
            with self.assertRaises(ValueError):
                probe.sid_text(bad)

    def test_rights_in_words(self):
        self.assertEqual(probe.rights(0x1F01FF), 'full control (0x1f01ff)')
        self.assertEqual(probe.rights(0x10000000), 'full control (0x10000000)')
        self.assertEqual(probe.rights(0x120089), 'read (0x120089)')
        self.assertEqual(probe.rights(0x1200A9), 'read, execute (0x1200a9)')
        self.assertEqual(probe.rights(0x12019F), 'read, write (0x12019f)')
        self.assertEqual(probe.rights(0x100000), 'some rights (0x100000)')

    def test_access_list_from_bytes(self):
        raw = acl(ace(0, 0, 0x1F01FF, ME), ace(0, 0, 0x120089, 'S-1-1-0'), ace(1, 0x8, 0x120116, 'S-1-5-7'),
                  ace(9, 0, 0x1200A9, 'S-1-15-2-1', extra=b'artx\x00\x00\x00\x00'), ace(17, 0, 0x1, 'S-1-16-4096'))
        aces = probe.parse_acl(raw)
        self.assertEqual([a[0] for a in aces], [0, 0, 1, 9, 17])
        self.assertEqual(probe.acl_lines('S-1-5-18', aces, ME), [
            '  owner: SYSTEM (S-1-5-18)',
            '  allow you: full control (0x1f01ff)',
            '  allow Everyone (S-1-1-0): read (0x120089)',
            '  deny Anonymous (S-1-5-7): write (0x120116) (inherited by contents only)',
            '  allow, with a condition ALL APPLICATION PACKAGES (S-1-15-2-1): read, execute (0x1200a9)',
            '  entry of type 17 (not shown)'])
        self.assertEqual(probe.acl_lines(ME, None, ME), ['  owner: you', '  access list: none (anyone may open it)'])
        self.assertEqual(probe.acl_lines('', [], ME), ['  owner: unknown', '  access list: empty (no one may open it)'])
        for bad in (b'\x02\x00', acl(ace(0, 0, 1, ME))[:-3]):
            with self.assertRaises(ValueError):
                probe.parse_acl(bad)

    def test_appx_rows_and_manifest(self):
        rows = probe.parse_appx('OpenAI.Codex|26.1.2.0|C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.2.0_x64__fict0pub1d|'
                                'OpenAI.Codex_fict0pub1d|fict0pub1d\r\nOld.Row|1.0|C:\\x\n|bad\n')
        self.assertEqual(rows[0], {'name': 'OpenAI.Codex', 'version': '26.1.2.0', 'family': 'OpenAI.Codex_fict0pub1d',
                                   'publisher': 'fict0pub1d',
                                   'location': 'C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.2.0_x64__fict0pub1d'})
        self.assertEqual((rows[1]['family'], len(rows)), ('', 2))
        apps = probe.parse_manifest(MANIFEST)
        self.assertEqual(apps, [{'id': 'App', 'exe': 'app\\ChatGPT.exe', 'entry': 'Windows.FullTrustApplication',
                                 'protocols': ['codex'], 'aliases': ['fictional-alias.exe']}])
        self.assertEqual(probe.manifest_lines(apps), [
            '    application App: executable app\\ChatGPT.exe; entry point Windows.FullTrustApplication',
            '      protocols: codex://', '      execution aliases: fictional-alias.exe'])
        self.assertEqual(probe.manifest_lines([]), ['    applications: none declared'])

    def test_openai_packages_section(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        with open(os.path.join(d, 'AppxManifest.xml'), 'wb') as f:
            f.write(MANIFEST)
        rows = [{'name': 'OpenAI.Codex', 'version': '26.1.2.0', 'location': d, 'family': 'OpenAI.Codex_fict0pub1d',
                 'publisher': 'fict0pub1d'},
                {'name': 'Anthropic.Claude', 'version': '1', 'location': d, 'family': 'x', 'publisher': 'y'},
                {'name': 'OpenAI.Runtime', 'version': '2', 'location': os.path.join(d, 'gone'), 'family': 'f', 'publisher': 'p'}]
        with patch.object(probe, 'appx_rows', return_value=rows):
            lines = probe.check_openai_packages()
        self.assertEqual(lines[:5], ['Package OpenAI.Codex 26.1.2.0',
                                     '  family: OpenAI.Codex_fict0pub1d; publisher id: fict0pub1d',
                                     '    application App: executable app\\ChatGPT.exe; entry point Windows.FullTrustApplication',
                                     '      protocols: codex://', '      execution aliases: fictional-alias.exe'])
        self.assertEqual(lines[5:], ['Package OpenAI.Runtime 2', '  family: f; publisher id: p', '  manifest: not found'])
        with patch.object(probe, 'appx_rows', side_effect=RuntimeError('error: powershell exit 1')):
            self.assertEqual(probe.check_openai_packages(), ['OpenAI packages: error: powershell exit 1'])

    def test_codex_pipe_section(self):
        self.assertEqual(probe.check_codex_pipe(['codex-app-tools-1', 'other']),
                         ["codex-ipc (the Codex app's control pipe): not found"])
        image = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1.2.0_x64__fict0pub1d\app\ChatGPT.exe'
        facts = {'pid': 4242, 'owner': ME, 'aces': [(0, 0, 0x1F01FF, ME), (0, 0, 0x120089, 'S-1-1-0')]}
        with patch.object(probe, 'pipe_facts', return_value=facts) as opened, patch.object(probe, 'my_sid', return_value=ME), \
                patch.object(probe, 'process_facts', return_value={'image': image, 'family': 'OpenAI.Codex_fict0pub1d', 'owner': ME}):
            lines = probe.check_codex_pipe(['Codex-IPC'])
        opened.assert_called_once_with('\\\\.\\pipe\\codex-ipc')
        self.assertEqual(lines, ["codex-ipc (the Codex app's control pipe): found",
                                 '  opened for its attributes only; nothing was read or sent',
                                 '  served by process 4242', '  that process: ' + image,
                                 '  its package: OpenAI.Codex_fict0pub1d; it runs as: you',
                                 '  owner: you', '  allow you: full control (0x1f01ff)',
                                 '  allow Everyone (S-1-1-0): read (0x120089)'])
        for line in lines:
            self.assertEqual(probe.redact(line, CTX), line, 'redaction leaves the facts readable')
        self.assertEqual(probe.pipe_lines({'error': 'all instances busy'}, ME), ['  could not open it to look: all instances busy'])
        self.assertEqual(probe.pipe_lines({'security_error': 'error 5'}, ME)[1:],
                         ['  server process: not reported', '  permissions: error 5'])
        self.assertIn('that process: cannot be opened (error 5)',
                      '\n'.join(probe.pipe_lines({'pid': 7}, ME, {'error': 'cannot be opened (error 5)'})))

    def test_processes_section(self):
        app = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1.2.0_x64__fict0pub1d\app\ChatGPT.exe'
        cli = r'C:\Users\demo\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe'
        found = [(1, 'ChatGPT.exe'), (2, 'ChatGPT.exe'), (3, 'codex.exe'), (4, 'codex.exe')]
        facts = {1: {'image': app, 'family': 'OpenAI.Codex_fict0pub1d', 'owner': ME},
                 2: {'image': app, 'family': 'OpenAI.Codex_fict0pub1d', 'owner': ME},
                 3: {'image': cli, 'family': 'none', 'owner': ME},
                 4: {'image': '', 'family': '', 'owner': '', 'error': 'cannot be opened (error 5)'}}
        with patch.object(probe, 'processes_named', return_value=found), patch.object(probe, 'my_sid', return_value=ME), \
                patch.object(probe, 'process_facts', side_effect=lambda pid: facts[pid]):
            lines = [probe.redact(line, CTX) for line in probe.check_app_processes()]
        self.assertEqual(lines, ['ChatGPT.exe x2: ' + app, '  package: OpenAI.Codex_fict0pub1d; runs as: you',
                                 'codex.exe x1: cannot be opened (error 5)',
                                 'codex.exe x1: %LOCALAPPDATA%\\Programs\\OpenAI\\Codex\\bin\\codex.exe',
                                 '  package: none; runs as: you'])
        with patch.object(probe, 'processes_named', return_value=[]):
            self.assertEqual(probe.check_app_processes(), ['ChatGPT.exe, Codex.exe, codex.exe: not running'])

    def test_bundled_cli_section_never_runs_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        os.makedirs(os.path.join(d, 'pkg', 'app', 'resources'))
        os.makedirs(os.path.join(d, 'home', '.codex'))
        cli = os.path.join(d, 'pkg', 'app', 'resources', 'codex.exe')
        with open(cli, 'wb') as f:
            f.write(b'MZ fictional')
        rows = [{'name': 'OpenAI.Codex', 'version': '1', 'location': os.path.join(d, 'pkg'), 'family': 'f', 'publisher': 'p'}]
        aces = [(9, 0, 0x1200A9, 'S-1-5-32-545'), (0, 0, 0x1F01FF, INSTALLER)]
        with patch.object(probe, 'appx_rows', return_value=rows), patch.object(probe, 'my_sid', return_value=ME), \
                patch.object(probe, 'path_security', return_value=(INSTALLER, aces)), \
                patch.object(probe, 'signature', return_value=('valid', 'Fictional Signer, LLC')), \
                patch.object(probe, 'run') as ran, patch('os.path.expanduser', return_value=os.path.join(d, 'home')):
            lines = probe.check_codex_cli_file()
            self.assertEqual(lines, [
                cli + ': found (not run)',
                '  owner: TrustedInstaller (S-1-5-80-...)',
                '  allow, with a condition Users (S-1-5-32-545): read, execute (0x1200a9)',
                '  allow TrustedInstaller (S-1-5-80-...): full control (0x1f01ff)',
                '  signature: valid; signer: Fictional Signer, LLC',
                '%USERPROFILE%\\.codex\\app-server-control: not found'])
            for line in lines[1:]:
                self.assertEqual(probe.redact(line, CTX), line, 'redaction leaves the facts readable')
            os.mkdir(os.path.join(d, 'home', '.codex', 'app-server-control'))
            with patch.object(probe, 'signature', side_effect=OSError()):
                lines = probe.check_codex_cli_file()
            self.assertIn('  signature: unknown (OSError)', lines)
            self.assertEqual(lines[-1], '%USERPROFILE%\\.codex\\app-server-control: found')
            ran.assert_not_called()

    def test_alias_and_package_file_are_told_apart(self):
        class Info:
            st_reparse_tag = probe.IO_REPARSE_TAG_APPEXECLINK
        self.assertEqual(probe.not_run_reason(r'C:\Program Files\WindowsApps\OpenAI.Codex_1_x64__p\app\resources\codex.exe',
                                              lstat=lambda p: object()), 'file inside a Store app package, not run')
        self.assertEqual(probe.not_run_reason(r'C:\Users\demo\AppData\Local\Microsoft\WindowsApps\wt.exe',
                                              lstat=lambda p: object()), 'app execution alias, not run')
        self.assertEqual(probe.not_run_reason(r'D:\Tools\codex.exe', lstat=lambda p: Info()), 'app execution alias, not run')
        self.assertEqual(probe.not_run_reason(r'D:\Tools\codex.exe', lstat=lambda p: object()), '')

    def test_the_notes_survive_redaction(self):
        for line in (probe.NEW_SECTIONS_NOTE, probe.FINAL_LINE):
            self.assertEqual(probe.redact(line, CTX), line)
        self.assertIn('read this output before posting', probe.FINAL_LINE)


@unittest.skipUnless(os.name == 'nt', 'reads real Windows tokens, processes, pipes and files')
class RealWindowsProbeV2(unittest.TestCase):
    def test_own_process_and_token(self):
        mine = probe.my_sid()
        self.assertTrue(mine.startswith('S-1-5-'), mine)
        facts = probe.process_facts(os.getpid())
        self.assertEqual(facts['owner'], mine)
        self.assertTrue(facts['image'].lower().endswith('.exe'), facts)
        self.assertTrue(facts['family'])
        name = os.path.basename(facts['image'])
        self.assertIn(os.getpid(), [pid for pid, _ in probe.processes_named([name])])

    def test_a_pipe_of_our_own(self):
        from multiprocessing.connection import Listener
        address = '\\\\.\\pipe\\motet-probe-test-%d' % os.getpid()
        listener = Listener(address, family='AF_PIPE')
        try:
            facts = probe.pipe_facts(address)
            self.assertEqual(facts.get('pid'), os.getpid(), facts)
            self.assertIsInstance(facts.get('aces'), list)
            self.assertTrue(probe.pipe_lines(facts, probe.my_sid(), probe.process_facts(facts['pid'])))
        finally:
            listener.close()
        self.assertIn('error', probe.pipe_facts(address))

    def test_file_permissions_and_signature_are_read(self):
        owner, aces = probe.path_security(sys.executable)
        self.assertTrue(owner.startswith('S-1-'))
        self.assertTrue(probe.acl_lines(owner, aces, probe.my_sid()))
        status, signer = probe.signature(sys.executable)
        self.assertIsInstance(status, str)
        self.assertIsInstance(signer, str)


if __name__ == '__main__':
    unittest.main()
