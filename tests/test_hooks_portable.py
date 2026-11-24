"""Motet's portable Claude Code hook (scripts/motet_hook.py) and the installer's two forms (scripts/install_hooks.py).

Every server here is a small fake on 127.0.0.1 with an ephemeral port. Every settings file is a temporary one; the
real ~/.claude/settings.json is never read or written. Runs on macOS, Linux and Windows.
"""
import http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts' / 'motet_hook.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import install_hooks  # noqa: E402

KEY = 'fictional-key-0123456789abcdefghijklmnopqrstu'
# The two decisions, written out here rather than imported, so a change on either side is caught.
ALLOW = '{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}'
DENY = ('{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", '
        '"message": "Denied by the user from Motet."}}}')
PAYLOAD = json.dumps({'session_id': 'fictional-session', 'hook_event_name': 'PermissionRequest',
                      'tool_name': 'Bash', 'tool_input': {'command': 'echo fictional'}}).encode()


class Fake(http.server.ThreadingHTTPServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get('Content-Length', '0'))
        self.server.seen.append({'path': self.path, 'headers': dict(self.headers.items()), 'body': self.rfile.read(n),
                                 'chunked': 'Transfer-Encoding' in self.headers})
        kind, status, body = self.server.script
        if kind == 'slow':
            self.server.release.wait(30)
            return
        if kind == 'cut':                       # promises more than it sends, then closes
            self.send_response(200)
            self.send_header('Content-Length', str(len(body) + 20))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def dead_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / '.live-room').mkdir()
        (self.home / '.live-room' / 'header').write_text('X-Live-Room-Key: %s\n' % KEY, encoding='utf-8')   # as load_key() writes it
        self.server = Fake(('127.0.0.1', 0), Handler)
        self.server.seen, self.server.script, self.server.release = [], ('reply', 200, ALLOW.encode()), threading.Event()
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
        e.pop('MOTET_HOOK_TEST_TIMEOUT', None)
        e.update(HOME=str(self.home), USERPROFILE=str(self.home))
        e.update(extra)
        return e

    def hook(self, mode, port=None, stdin=PAYLOAD, argv=None, **env):
        argv = argv or [sys.executable, '-I', str(HOOK), mode, str(port or self.port), install_hooks.MARK]
        r = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(**env), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(KEY.encode(), r.stdout + r.stderr)
        return r.stdout

    def reply(self, body, status=200, kind='reply'):
        self.server.script = (kind, status, body if isinstance(body, bytes) else body.encode())

    def test_replies_match_the_installer_and_the_server_format(self):
        self.assertEqual(install_hooks.REPLIES, [ALLOW, DENY])
        allow = json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}})
        self.assertEqual(allow, ALLOW)          # what permission() in src/server.py sends

    def test_ask_forwards_key_and_body_and_prints_allow_exactly(self):
        self.assertEqual(self.hook('ask'), ALLOW.encode())
        (req,) = self.server.seen
        self.assertEqual(req['path'], '/permission')
        self.assertEqual(req['body'], PAYLOAD)
        self.assertFalse(req['chunked'])
        self.assertEqual(req['headers'].get('X-Live-Room-Key'), KEY)
        self.assertEqual(req['headers'].get('Content-Type'), 'application/json')
        self.assertEqual(req['headers'].get('Host'), '127.0.0.1:%d' % self.port)     # what the server's host_ok() wants

    def test_ask_prints_deny_exactly(self):
        self.reply(DENY)
        self.assertEqual(self.hook('ask'), DENY.encode())

    def test_ask_prints_nothing_on_any_doubt(self):
        cases = {
            'altered JSON': (json.dumps(json.loads(ALLOW), separators=(',', ':')), 200, 'reply'),
            'other behavior': (ALLOW.replace('allow', 'ask'), 200, 'reply'),
            'extra space': (ALLOW + ' ', 200, 'reply'),
            'trailing newline': (ALLOW + '\n', 200, 'reply'),
            'empty (no decision)': ('', 200, 'reply'),
            'server error': (ALLOW, 500, 'reply'),
            'forbidden': (ALLOW, 403, 'reply'),
            'other success code': (ALLOW, 201, 'reply'),
            'closed mid-body': (ALLOW, 200, 'cut'),
        }
        for name, (body, status, kind) in cases.items():
            with self.subTest(name):
                self.reply(body, status, kind)
                self.assertEqual(self.hook('ask'), b'')

    def test_ask_prints_nothing_with_no_server(self):
        self.assertEqual(self.hook('ask', port=dead_port()), b'')

    def test_ask_prints_nothing_when_the_server_is_too_slow(self):
        self.reply(ALLOW, kind='slow')
        start = time.time()
        self.assertEqual(self.hook('ask', MOTET_HOOK_TEST_TIMEOUT='0.5'), b'')
        self.assertLess(time.time() - start, 20)

    def test_missing_or_malformed_header_sends_nothing(self):
        header = self.home / '.live-room' / 'header'
        for content in (None, 'X-Live-Room-Key: %s\nX-Other: y\n' % KEY, 'Authorization: %s\n' % KEY, 'X-Live-Room-Key: \n'):
            with self.subTest(content=content and content[:20]):
                if content is None:
                    header.unlink()
                else:
                    header.write_text(content, encoding='utf-8')
                self.assertEqual(self.hook('ask'), b'')
                self.assertEqual(self.hook('tell'), b'')
        self.assertEqual(self.server.seen, [])

    def test_tell_forwards_and_prints_nothing(self):
        self.reply('ok')
        self.assertEqual(self.hook('tell'), b'')
        (req,) = self.server.seen
        self.assertEqual((req['path'], req['body'], req['headers'].get('X-Live-Room-Key')), ('/hook', PAYLOAD, KEY))
        for status, kind in ((500, 'reply'), (200, 'cut')):
            self.reply(ALLOW, status, kind)
            self.assertEqual(self.hook('tell'), b'')
        self.assertEqual(self.hook('tell', port=dead_port()), b'')

    def test_proxy_settings_are_ignored(self):
        proxy = 'http://127.0.0.1:%d' % dead_port()
        out = self.hook('ask', HTTP_PROXY=proxy, http_proxy=proxy, ALL_PROXY=proxy, all_proxy=proxy, NO_PROXY='', no_proxy='')
        self.assertEqual(out, ALLOW.encode())
        self.assertEqual(len(self.server.seen), 1)

    def test_oversized_input_is_not_sent(self):
        self.assertEqual(self.hook('ask', stdin=b'{"a": "' + b'x' * 200_001 + b'"}'), b'')
        self.assertEqual(self.server.seen, [])

    def test_bad_arguments_print_nothing(self):
        for args in ([], ['ask'], ['ask', 'eighty'], ['decide', str(self.port)], ['ask', '0'], ['ask', '70000']):
            with self.subTest(args=args):
                self.assertEqual(self.hook('', argv=[sys.executable, '-I', str(HOOK)] + args), b'')
        self.assertEqual(self.server.seen, [])

    def test_installed_windows_entry_runs_as_written(self):
        """Run the exact program and arguments the installer writes for Windows, as Claude Code's exec form would."""
        settings = self.home / 'settings.json'
        install_hooks.run(win=True, settings=str(settings), port=str(self.port), claude_version=lambda: None)
        hooks = json.loads(settings.read_text(encoding='utf-8'))['hooks']
        ask = hooks['PermissionRequest'][0]['hooks'][0]
        tell = hooks['Stop'][0]['hooks'][0]
        self.reply(DENY)
        self.assertEqual(self.hook('', argv=[ask['command']] + ask['args']), DENY.encode())
        self.assertEqual(self.hook('', argv=[tell['command']] + tell['args']), b'')
        self.assertEqual([r['path'] for r in self.server.seen], ['/permission', '/hook'])

    @unittest.skipIf(os.name == 'nt' or not shutil.which('curl') or not shutil.which('sh'), 'the shell hooks need sh and curl')
    def test_shell_and_python_hooks_agree(self):
        ask = install_hooks.shell_commands(str(self.port))[1]
        for body, status, kind in ((ALLOW, 200, 'reply'), (DENY, 200, 'reply'), (ALLOW + ' ', 200, 'reply'),
                                   (ALLOW, 500, 'reply'), (ALLOW, 200, 'cut'), ('', 200, 'reply')):
            with self.subTest(body=body[:40], status=status, kind=kind):
                self.reply(body, status, kind)
                shell = self.hook('', argv=['sh', '-c', ask['command']])
                self.assertEqual(self.hook('ask'), shell)
        seen = self.server.seen
        self.assertEqual(seen[0]['body'], seen[1]['body'])
        self.assertEqual(seen[0]['headers'].get('X-Live-Room-Key'), seen[1]['headers'].get('X-Live-Room-Key'))


class RealServerTests(unittest.TestCase):
    """The Python hook against Motet's own request handler (src/server.py), in this process, on an ephemeral port:
    the server must not depend on anything curl sends that http.client does not."""

    SESSION = '0f1e2d3c-4b5a-4968-8776-655443322110'
    PAYLOAD = json.dumps(dict(json.loads(PAYLOAD), session_id=SESSION)).encode()

    def setUp(self):
        sys.path.insert(0, str(ROOT / 'src'))
        import server
        from unittest.mock import patch
        self.server_mod = server
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / '.live-room').mkdir()
        (self.home / '.live-room' / 'header').write_text('X-Live-Room-Key: %s\n' % KEY, encoding='utf-8')
        self.httpd = server.Server(('127.0.0.1', 0), server.H)
        self.port = self.httpd.server_port
        self.emitted = []
        self.patches = [patch.object(server, name, value) for name, value in (
            ('PORT', self.port), ('KEY', KEY), ('APPROVER', time.time() + 3600), ('PENDING', {}), ('AGENTS', {}), ('HOOKED', {}),
            ('describe', lambda aid, old=False, **kw: server.AGENTS.setdefault(aid, {})),
            ('emit', lambda aid, kind, old, **kw: self.emitted.append((kind, kw.get('how')))),
            ('claude_permission_ownership', lambda *a, **k: {'classification': 'unmanaged', 'checkRequired': False}))]
        for p in self.patches:
            p.start()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def hook(self, mode, **env):
        e = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
        e.pop('MOTET_HOOK_TEST_TIMEOUT', None)
        e.update(HOME=str(self.home), USERPROFILE=str(self.home), **env)
        return subprocess.Popen([sys.executable, '-I', str(HOOK), mode, str(self.port), install_hooks.MARK], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)

    def wait_for(self, check, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.05)
        return False

    def decide(self, allow):
        proc = self.hook('ask')
        proc.stdin.write(self.PAYLOAD)
        proc.stdin.close()
        pending = self.server_mod.PENDING
        self.assertTrue(self.wait_for(lambda: len(pending) == 1), 'the server never held the request')
        (p,) = list(pending.values())
        p['allow'] = allow
        p['event'].set()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        return out

    def test_allow_and_deny_round_trip(self):
        self.assertEqual(self.decide(True), ALLOW.encode())
        self.assertEqual(self.decide(False), DENY.encode())
        self.assertIn(('perm_end', 'allowed from the stage'), self.emitted)
        self.assertIn(('perm_end', 'denied from the stage'), self.emitted)

    def test_tell_reaches_the_server(self):
        body = json.dumps({'session_id': self.SESSION, 'hook_event_name': 'Notification',
                           'notification_type': 'permission_prompt', 'message': 'Claude wants to run: echo fictional'}).encode()
        out, err = self.hook('tell').communicate(body, timeout=30)
        self.assertEqual(out, b'')
        self.assertTrue(self.wait_for(lambda: ('waiting', None) in self.emitted))
        self.assertIn('c:' + self.SESSION, self.server_mod.AGENTS)

    def test_hook_leaving_while_held_releases_the_request(self):
        proc = self.hook('ask', MOTET_HOOK_TEST_TIMEOUT='1')
        out, err = proc.communicate(self.PAYLOAD, timeout=30)
        self.assertEqual((proc.returncode, out), (0, b''))
        pending = self.server_mod.PENDING
        self.assertTrue(self.wait_for(lambda: not pending), 'the server kept holding a request whose hook had gone')
        self.assertTrue(self.wait_for(lambda: any(kind == 'perm_end' and how.startswith('the request went away')
                                                  for kind, how in self.emitted if how)))


def base_commands(port):
    """The macOS and Linux hook strings exactly as the installer wrote them before Windows support (dev e3518b2)."""
    MARK = '#live-room-hook'
    HEAD = "-H 'Content-Type: application/json' -H @\"$HOME/.live-room/header\""
    URL = 'http://127.0.0.1:%s' % port
    CURL = "curl -q -sf --noproxy '*'"
    TELL = "%s -m 1 -X POST %s --data-binary @- %s/hook >/dev/null 2>&1 || true %s" % (CURL, HEAD, URL, MARK)
    REPLIES = [json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': d}}) for d in
               ({'behavior': 'allow'}, {'behavior': 'deny', 'message': 'Denied by the user from Motet.'})]
    ASK = ("r=$(%s -m 320 -X POST %s --data-binary @- %s/permission 2>/dev/null) && case \"$r\" in "
           "'%s'|'%s') printf '%%s' \"$r\";; esac; true %s" % (CURL, HEAD, URL, REPLIES[0], REPLIES[1], MARK))
    return TELL, ASK


EVENTS = ('Notification', 'Stop', 'SubagentStop', 'PermissionRequest')


class InstallerTests(unittest.TestCase):
    PYTHON = r'C:\Program Files\Python312\python.exe' if os.name == 'nt' else '/opt/fictional/bin/python3'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / 'claude' / 'settings.json'
        self.settings.parent.mkdir()
        self.others = {'Stop': [{'hooks': [{'type': 'command', 'command': 'fictional other stop'}]}],
                       'PermissionRequest': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'fictional other permission'}]}]}
        self.settings.write_text(json.dumps({'model': 'fictional', 'hooks': json.loads(json.dumps(self.others))}), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, win, remove=False, python=None):
        return install_hooks.run(remove=remove, win=win, settings=str(self.settings), port='8123',
                                 python=python or self.PYTHON, claude_version=lambda: None)

    def hooks(self):
        return json.loads(self.settings.read_text(encoding='utf-8')).get('hooks', {})

    def mine(self):
        return {ev: [h for g in groups for h in g['hooks'] if install_hooks.ours(h)] for ev, groups in self.hooks().items()}

    def cycle(self, win):
        self.install(win)
        self.install(win)
        mine = self.mine()
        for ev in EVENTS:
            self.assertEqual(len(mine[ev]), 1, ev)
            self.assertEqual(mine[ev][0].get('timeout'), 330 if ev == 'PermissionRequest' else None)
        self.assertIn({'type': 'command', 'command': 'fictional other stop'}, [h for g in self.hooks()['Stop'] for h in g['hooks']])
        self.install(win, remove=True)
        self.assertEqual(self.hooks(), self.others)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8'))['model'], 'fictional')
        self.assertTrue(list(self.settings.parent.glob('settings.json.live-room-backup-*')))
        self.assertFalse(list(self.settings.parent.glob('.settings-*.tmp')))

    def test_install_reinstall_remove_posix_form(self):
        self.cycle(False)

    def test_install_reinstall_remove_windows_form(self):
        self.cycle(True)

    def test_fresh_install_without_a_settings_file(self):
        self.settings.unlink()
        for win in (False, True):
            self.install(win)
            self.assertEqual(sorted(self.hooks()), sorted(EVENTS))
            self.assertTrue(all(len(v) == 1 for v in self.mine().values()))
            self.install(win, remove=True)
            self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), {})
            self.settings.unlink()

    def test_switching_form_replaces_the_other(self):
        self.install(False)
        self.install(True)
        mine = self.mine()
        self.assertTrue(all(len(v) == 1 and 'args' in v[0] for k, v in mine.items() if k in EVENTS))
        self.install(False)
        mine = self.mine()
        self.assertTrue(all(len(v) == 1 and 'args' not in v[0] for k, v in mine.items() if k in EVENTS))
        self.install(True, remove=True)
        self.assertEqual(self.hooks(), self.others)

    def test_remove_keeps_other_hooks_in_the_same_group(self):
        self.install(True)
        d = json.loads(self.settings.read_text(encoding='utf-8'))
        ours = d['hooks']['Stop'][-1]['hooks'][0]
        d['hooks']['Stop'][0]['hooks'].append(ours)                # someone grouped ours with theirs
        self.settings.write_text(json.dumps(d), encoding='utf-8')
        self.install(False, remove=True)
        self.assertEqual(self.hooks(), self.others)

    def test_windows_entries_are_exec_form_with_absolute_paths(self):
        self.install(True)
        mine = self.mine()
        for ev in EVENTS:
            (h,) = mine[ev]
            mode = 'ask' if ev == 'PermissionRequest' else 'tell'
            self.assertEqual(h['type'], 'command')
            self.assertEqual(h['command'], self.PYTHON)
            self.assertEqual(h['args'][:1] + h['args'][2:], ['-I', mode, '8123', '#live-room-hook'])
            same = lambda p: os.path.normcase(os.path.realpath(p))
            self.assertEqual(same(h['args'][1]), same(ROOT / 'scripts' / 'motet_hook.py'))
            self.assertTrue(os.path.isabs(h['args'][1]) and os.path.isfile(h['args'][1]))
            self.assertNotIn('shell', h)

    def test_posix_entries_are_unchanged(self):
        self.install(False)
        tell, ask = base_commands('8123')
        mine = self.mine()
        for ev in EVENTS:
            (h,) = mine[ev]
            want = {'type': 'command', 'command': ask, 'timeout': 330} if ev == 'PermissionRequest' else {'type': 'command', 'command': tell}
            self.assertEqual(json.dumps(h), json.dumps(want))

    def test_refuses_unusable_windows_pythons(self):
        for bad in (r'C:\Users\demo\AppData\Local\Microsoft\WindowsApps\python.exe',
                    r'C:\Users\demo\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_x\python.exe',
                    r'C:\tools\python.cmd', r'C:\tools\python.bat', r'C:\Python312\pythonw.exe', 'python.exe'):
            with self.subTest(bad=bad):
                before = self.settings.read_bytes()
                with self.assertRaises(SystemExit):
                    self.install(True, python=bad)
                self.assertEqual(self.settings.read_bytes(), before)
        self.assertIn('Microsoft Store', install_hooks.windows_python(r'C:\Users\demo\AppData\Local\Microsoft\WindowsApps\python.exe'))
        self.assertIn('.cmd', install_hooks.windows_python(r'C:\tools\python.cmd'))
        self.assertIn('pythonw', install_hooks.windows_python(r'C:\Python312\pythonw.exe'))
        self.assertEqual(install_hooks.windows_python(sys.executable) if os.name == 'nt' else '', '')
        with self.assertRaises(SystemExit):              # removing still works from any Python
            self.install(True, python='python.exe')
        self.install(True)
        self.install(True, remove=True, python=r'C:\tools\python.cmd')
        self.assertEqual(self.hooks(), self.others)

    def test_refuses_an_old_claude_and_notes_the_minimum(self):
        before = self.settings.read_bytes()
        with self.assertRaises(SystemExit) as e:
            install_hooks.run(win=True, settings=str(self.settings), port='8123', python=self.PYTHON, claude_version=lambda: (2, 1, 138))
        self.assertIn('2.1.139', str(e.exception))
        self.assertEqual(self.settings.read_bytes(), before)
        out = install_hooks.run(win=True, settings=str(self.settings), port='8123', python=self.PYTHON, claude_version=lambda: (2, 1, 139))
        self.assertIn('2.1.139', out)
        self.assertIn('installed', out)

    def test_command_line_uses_the_settings_override(self):
        env = {k: v for k, v in os.environ.items()}
        env.update(MOTET_CLAUDE_SETTINGS=str(self.settings), PORT='8123', PATH='')      # PATH='' keeps any real claude out
        r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'install_hooks.py')], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.decode().startswith('installed -> %s' % self.settings))
        self.assertEqual(len(self.mine()['PermissionRequest']), 1)
        r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'install_hooks.py'), '--remove'], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hooks(), self.others)
        self.assertEqual(r.stdout.decode().strip(), 'removed -> %s' % self.settings)

    def test_bad_port_is_refused(self):
        with self.assertRaises(SystemExit):
            install_hooks.run(win=False, settings=str(self.settings), port='80a')


if __name__ == '__main__':
    unittest.main(verbosity=2)
