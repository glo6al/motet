#!/usr/bin/env python3
"""The owner's app icons: one read-only, owner-keyed address over a private folder, on macOS, Linux and Windows.
Fictional icons in a temporary home only, written in binary; the WATCH listener binds an ephemeral loopback port,
never 8793. On Windows a junction stands in for a folder link (it needs no special privilege); a symbolic link to a
file is tried and the check skipped where Windows does not allow one."""
import http.client
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#2f86c9"/></svg>'
PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 24


def folder_link(target, link):
    """A link to a folder: a junction on Windows, a symbolic link elsewhere."""
    if server.WIN:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)


def remove_folder_link(link):
    (os.rmdir if server.WIN else os.unlink)(link)      # the link itself, never what it points to


class AppIconFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.icons = self.home / '.live-room' / 'app-icons'
        self.icons.mkdir(parents=True)
        self.links = []

    def tearDown(self):
        while self.links:
            remove_folder_link(self.links.pop())
        self.tmp.cleanup()

    def link_folder(self, target, link):
        folder_link(target, link)
        self.links.append(link)

    def get(self, name):
        return server.app_icon(name, home=str(self.home))

    def test_svg_first_then_png_with_matching_types(self):
        (self.icons / 'codex.png').write_bytes(PNG)
        self.assertEqual(self.get('codex'), (PNG, 'image/png'))
        (self.icons / 'codex.svg').write_bytes(SVG)
        self.assertEqual(self.get('codex'), (SVG, 'image/svg+xml'))

    def test_only_the_four_names_and_nothing_from_the_request_becomes_a_path(self):
        (self.icons / 'claude.svg').write_bytes(SVG)
        (self.home / '.live-room' / 'secret.svg').write_bytes(SVG)
        for name in ('claude', 'codex', 'antigravity', 'cursor'):
            self.assertIn(name, server.APP_ICON_NAMES)
        for name in ('Claude', 'claude.svg', '../secret', '..%2Fsecret', 'gemini', '', 'claude/', 'app-icons/claude'):
            self.assertIsNone(self.get(name), name)
        self.assertIsNotNone(self.get('claude'))

    def test_missing_folder_or_file_is_quietly_absent(self):
        self.assertIsNone(self.get('cursor'))
        self.assertIsNone(server.app_icon('claude', home=str(self.home / 'nowhere')))

    def test_a_linked_icon_is_refused(self):
        outside = self.home / 'outside.svg'
        outside.write_bytes(SVG)
        try:
            os.symlink(outside, self.icons / 'claude.svg')
        except OSError as e:
            self.skipTest('creating a symbolic link needs Developer Mode or the symlink privilege (%s)' % type(e).__name__)
        self.assertIsNone(self.get('claude'), 'a link out of the folder is not followed')

    def test_hard_links_size_and_shape_are_refused(self):
        outside = self.home / 'outside.svg'
        outside.write_bytes(SVG)
        os.link(outside, self.icons / 'codex.svg')
        self.assertIsNone(self.get('codex'), 'a second name for a file elsewhere is not served')
        (self.icons / 'cursor.svg').write_bytes(b'<svg>' + b' ' * server.APP_ICON_MAX)
        self.assertIsNone(self.get('cursor'), 'over the size cap')
        (self.icons / 'antigravity.png').write_bytes(b'not a png')
        self.assertIsNone(self.get('antigravity'), 'a .png must be a PNG')
        (self.icons / 'antigravity.svg').mkdir()
        self.assertIsNone(self.get('antigravity'), 'a folder is not an icon')

    def test_a_linked_icon_folder_is_refused(self):
        real = self.home / 'elsewhere'
        real.mkdir()
        (real / 'claude.svg').write_bytes(SVG)
        self.icons.rmdir()
        self.link_folder(real, self.icons)
        self.assertIsNone(self.get('claude'))
        remove_folder_link(self.links.pop())
        self.icons.mkdir()
        (self.icons / 'claude.svg').write_bytes(SVG)
        self.assertEqual(self.get('claude'), (SVG, 'image/svg+xml'), 'served again from a real folder')

    def test_a_linked_state_folder_is_refused(self):
        (self.icons / 'claude.svg').write_bytes(SVG)
        state = self.home / '.live-room'
        state.rename(self.home / 'state-elsewhere')
        self.link_folder(self.home / 'state-elsewhere', state)
        self.assertIsNone(self.get('claude'))

    def test_links_above_the_state_folder_are_resolved_once(self):
        """As before the walk: your home may be reached through a link (a moved profile, /var on macOS)."""
        (self.icons / 'claude.svg').write_bytes(SVG)
        self.link_folder(self.home, self.home.parent / (self.home.name + '-via-link'))
        self.assertEqual(server.app_icon('claude', home=str(self.links[-1])), (SVG, 'image/svg+xml'))

    def test_icons_are_opened_through_the_shared_walk(self):
        (self.icons / 'claude.svg').write_bytes(SVG)
        calls = []
        real_folder = server.private_storage.open_folder

        def recorded(path, *args, **kwargs):
            calls.append(path)
            return real_folder(path, *args, **kwargs)
        with unittest.mock.patch.object(server.private_storage, 'open_folder', recorded):
            self.assertEqual(self.get('claude'), (SVG, 'image/svg+xml'))
        self.assertEqual(calls, [os.path.join(os.path.realpath(str(self.home)), '.live-room')])


@unittest.skipIf(os.name == 'nt', 'simulates Windows on macOS or Linux')
class SimulatedWindowsIcons(unittest.TestCase):
    """app_icon under the Windows simulation: every open goes through private_storage, links still refused."""

    def test_served_and_links_refused(self):
        from windows_simulation import simulated_windows
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            icons = home / '.live-room' / 'app-icons'
            icons.mkdir(parents=True)
            (icons / 'codex.png').write_bytes(PNG)
            with simulated_windows() as calls, unittest.mock.patch.object(server, 'WIN', True):
                self.assertEqual(server.app_icon('codex', home=str(home)), (PNG, 'image/png'))
                self.assertIn('anchored', calls)
                self.assertIn('open_folder', calls)
                icons.rename(home / 'icons-elsewhere')
                os.symlink(home / 'icons-elsewhere', icons)
                self.assertIsNone(server.app_icon('codex', home=str(home)))


class AppIconAddress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        icons = Path(self.tmp.name) / '.live-room' / 'app-icons'
        icons.mkdir(parents=True)
        (icons / 'claude.svg').write_bytes(SVG)
        self.previous = server.KEY, server.PORT, server.HOME
        self.watch = ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        self.assertNotEqual(self.watch.server_port, 8793)
        server.KEY, server.PORT, server.HOME = 'fictional-owner-key', self.watch.server_port, self.tmp.name
        threading.Thread(target=self.watch.serve_forever, daemon=True).start()

    def tearDown(self):
        self.watch.shutdown()
        self.watch.server_close()
        server.KEY, server.PORT, server.HOME = self.previous
        self.tmp.cleanup()

    def get(self, path, key='fictional-owner-key', method='GET'):
        c = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=5)
        try:
            c.request(method, path, headers={'X-Live-Room-Key': key} if key is not None else {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_owner_key_required(self):
        for key in (None, '', 'wrong-key'):
            self.assertEqual(self.get('/app-icon/claude', key)[0], 403)

    def test_icon_served_with_type_nosniff_and_draw_only_policy(self):
        status, headers, body = self.get('/app-icon/claude')
        self.assertEqual((status, body), (200, SVG))
        self.assertEqual(headers['Content-Type'], 'image/svg+xml')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Content-Security-Policy'], server.APP_ICON_CSP)
        self.assertIn('sandbox', server.APP_ICON_CSP)
        self.assertIn("default-src 'none'", server.APP_ICON_CSP)

    def test_anything_else_is_not_found_and_nothing_is_listed_or_written(self):
        for path in ('/app-icon/codex', '/app-icon/', '/app-icon/claude?x=1', '/app-icon/claude.svg',
                     '/app-icon/../app-icon/claude', '/app-icon/%2e%2e/secret'):
            self.assertEqual(self.get(path)[0], 404, path)
        for method in ('PUT', 'DELETE', 'POST'):
            self.assertNotEqual(self.get('/app-icon/claude', method=method)[0], 200, method)


class LogoNameBackstop(unittest.TestCase):
    """The pre-commit check refuses a tracked or staged image named for an AI company or app. Fictional files only."""
    def test_names(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import check_secrets
        rows = [('100644', 'b' * 40, p) for p in ('brand/Claude-mark.PNG', 'docs/media/openai.svg', 'a/ChatGPT.ICNS', 'b/gemini-logo.webp',
                                                    'c/my-cursor.gif', 'd/anthropic.jpeg', 'e/codex.ico', 'f/antigravity.jpg',
                                                    'docs/claude.md', 'src/codex_transcript.py', 'docs/media/logo-green.svg', 'claude/mark.svg')]
        self.assertEqual(check_secrets.logo_names(rows), [r[2] for r in rows[:8]])

    def test_a_failed_logo_index_read_is_unread_not_clean(self):
        import subprocess
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import check_secrets
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
            (Path(tmp) / 'Gemini-Mark.svg').write_bytes(SVG)
            subprocess.run(['git', '-C', tmp, 'add', 'Gemini-Mark.svg'], check=True)
            hits, unread, provider_icons = check_secrets.run(tmp)
            self.assertEqual((hits, unread, provider_icons), ([], [], ['Gemini-Mark.svg']))
            real, calls = check_secrets.git_index, []
            def second_read_fails(root):
                calls.append(root)
                return real(root) if len(calls) == 1 else 'git could not list the index'
            with patch.object(check_secrets, 'git_index', second_read_fails):
                hits, unread, provider_icons = check_secrets.run(tmp)
            self.assertEqual(len(calls), 2)
            self.assertEqual(provider_icons, [])
            self.assertIn(('the staged files (logo-name check)', 'git could not list the index'), unread)
        with tempfile.TemporaryDirectory() as plain:      # outside a git checkout: unchanged, nothing staged, clean
            (Path(plain) / 'notes.md').write_text('fictional')
            self.assertEqual(check_secrets.run(plain), ([], [], []))

    def test_script_refuses_a_staged_logo_and_passes_a_fictional_mark(self):
        import subprocess
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'check_secrets.py'
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *a: subprocess.run(['git', '-C', tmp, *a], check=True, capture_output=True)
            git('init', '-q')
            (Path(tmp) / 'fictional-mark.svg').write_bytes(SVG)
            git('add', 'fictional-mark.svg')
            ok = subprocess.run([sys.executable, str(script), tmp], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout)
            (Path(tmp) / 'Codex-Icon.png').write_bytes(PNG)
            git('add', 'Codex-Icon.png')
            bad = subprocess.run([sys.executable, str(script), tmp], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn('LOGO    Codex-Icon.png', bad.stdout)
            self.assertNotIn('clean:', bad.stdout)


if __name__ == '__main__':
    unittest.main()
