#!/usr/bin/env python3
"""Page previews: the folder grant and the walk that serves styles, scripts, images and fonts beside a page.

Three groups. ``SimulatedWindows`` runs the server's Windows branch on macOS or Linux (windows_simulation answers
each private_storage call with the POSIX walk), so the wiring is checked everywhere. ``RealWindows`` runs only on
Windows with Python 3.12 or later, with real junctions and links. ``Posix`` keeps the macOS and Linux rules pinned.
Synthetic files in temporary folders only, written in binary.

Run: python tests/test_preview_walk.py
"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
import server

ASSET = b'body{}\r\n\x1a after a Ctrl-Z'


def write(path, data):
    with open(path, 'wb') as out:
        out.write(data)


def ident(path):
    st = os.stat(path)
    return (st.st_ino, st.st_dev)


@contextlib.contextmanager
def names_not_private():
    """Take the by-name private checks out of the way, so only the walk's identity checks can refuse."""
    with patch.object(server, 'in_private', lambda path: False), patch.object(server, 'named_private', lambda path: False):
        yield


class Fake:
    def send(self, code, body, *a):
        self.got = (code, body)


class PreviewCase:
    """Shared fixture: a home with a project folder holding a page, a css folder, and private folders."""

    def make_link(self, target, link):
        raise NotImplementedError

    def remove_link(self, link):
        raise NotImplementedError

    def setUp(self):
        self.saved = (server.HOME, server.PRIVATE_ROOTS, dict(server.ALLOWED_FILES), dict(server.ALLOWED_DIRS),
                      list(server.LOG), server.BASE)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.links = []
        server.HOME = self.home
        self.live_room = os.path.join(self.home, '.live-room')
        os.makedirs(self.live_room)
        server.PRIVATE_ROOTS = tuple(os.path.join(self.home, p) + os.sep for p in ('.live-room', 'AppData'))
        self.proj = os.path.join(self.home, 'work', 'site')
        self.css = os.path.join(self.proj, 'css')
        os.makedirs(self.css)
        self.page = os.path.join(self.proj, 'index.html')
        write(self.page, b'<link rel="stylesheet" href="css/a.css"><p>page')
        self.asset = os.path.join(self.css, 'a.css')
        write(self.asset, ASSET)
        self.outside = os.path.join(self.home, 'outside')
        os.makedirs(self.outside)
        write(os.path.join(self.outside, 'a.css'), b'outside')
        write(os.path.join(self.live_room, 'key'), b'fictional-key')
        server.ALLOWED_FILES.clear()
        server.ALLOWED_DIRS.clear()

    def tearDown(self):
        while self.links:
            self.remove_link(self.links.pop())
        server.HOME, server.PRIVATE_ROOTS = self.saved[0], self.saved[1]
        server.ALLOWED_FILES.clear(); server.ALLOWED_FILES.update(self.saved[2])
        server.ALLOWED_DIRS.clear(); server.ALLOWED_DIRS.update(self.saved[3])
        server.LOG[:] = self.saved[4]; server.BASE = self.saved[5]
        self.tmp.cleanup()

    def link(self, target, link):
        self.make_link(target, link)
        self.links.append(link)

    def unlink(self, link):
        self.links.remove(link)
        self.remove_link(link)

    def get(self, path):
        f = Fake()
        server.H.preview(f, path)
        return f.got

    def grant(self, page=None):
        server.allow_preview('c:00000000', False, 0, 'id1', page or self.page)

    # The checks every system shares.

    def check_grant_and_serve(self):
        self.grant()
        self.assertIn(self.page, server.ALLOWED_FILES)
        self.assertEqual(server.ALLOWED_DIRS.get(self.proj), ident(self.proj), 'the folder is granted by identity')
        self.assertEqual(self.get(self.page), (200, b'<link rel="stylesheet" href="css/a.css"><p>page'))
        self.assertEqual(self.get(self.asset), (200, ASSET), 'byte for byte')

    def check_links_at_each_position(self):
        self.grant()
        # a link to a folder inside the grant, pointing inside it or outside it
        for name, target in (('in', self.css), ('out', self.outside)):
            with self.subTest(position='folder below the grant', target=name):
                self.link(target, os.path.join(self.css, name))
                self.assertEqual(self.get(os.path.join(self.css, name, 'a.css'))[0], 404)
        # the granted folder itself moved aside and a link put in its place
        with self.subTest(position='the granted folder'):
            os.rename(self.proj, self.proj + '-aside')
            self.link(self.proj + '-aside', self.proj)
            self.assertEqual(self.get(self.asset)[0], 404)
            self.assertEqual(self.get(self.page)[0], 404 if server.WIN else 200)
            self.unlink(self.proj)
            os.rename(self.proj + '-aside', self.proj)
            self.assertEqual(self.get(self.asset), (200, ASSET), 'served again once back in place')
        # a folder above the granted folder moved aside and a link put in its place
        with self.subTest(position='above the granted folder'):
            work = os.path.dirname(self.proj)
            os.rename(work, work + '-aside')
            self.link(work + '-aside', work)
            self.assertEqual(self.get(self.asset)[0], 404 if server.WIN else 200)   # macOS follows links above the grant, then checks its identity
            self.unlink(work)
            os.rename(work + '-aside', work)

    def check_private_below_the_grant(self):
        self.grant()
        vault = os.path.join(self.proj, 'vault')
        os.makedirs(vault)
        write(os.path.join(vault, 'x.css'), b'secret')
        self.assertEqual(self.get(os.path.join(vault, 'x.css')), (200, b'secret'), 'control: not private yet')
        server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (vault + os.sep,)
        self.assertEqual(self.get(os.path.join(vault, 'x.css'))[0], 404)
        # The walk's own identity check, with the name check out of the way: still refused.
        with names_not_private():
            self.assertEqual(self.get(os.path.join(vault, 'x.css'))[0], 404)
            server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (self.proj + os.sep,)
            self.assertEqual(self.get(self.asset)[0], 404, 'the granted folder itself is now private')

    def check_second_name_and_swaps(self):
        self.grant()
        twin = os.path.join(self.css, 'twin.css')
        os.link(os.path.join(self.live_room, 'key'), twin)
        self.assertEqual(self.get(twin)[0], 404, 'a second name for the key is not served')
        os.unlink(twin)
        # the granted folder replaced by a different folder under the same name
        os.rename(self.proj, self.proj + '-old')
        os.makedirs(os.path.join(self.proj, 'css'))
        write(self.asset, b'replacement')
        self.assertEqual(self.get(self.asset)[0], 404)

    def check_home_reached_through_a_link(self):
        """The home folder given by a link (a profile moved with a junction): a page in the real home or above it
        brings no folder, so files under the real home are not served."""
        real_home = os.path.join(self.home, 'disk', 'person')
        os.makedirs(os.path.join(real_home, 'notes'))
        write(os.path.join(real_home, 'notes', 'a.css'), b'home file')
        given = os.path.join(self.home, 'person-link')
        self.link(real_home, given)
        server.HOME = given
        server.PRIVATE_ROOTS = ()      # no private folder, so only the home rule can refuse (their real places would too)
        self.assertEqual(server.resolved_home(), real_home)
        for page in (os.path.join(self.home, 'disk', 'page.html'), os.path.join(real_home, 'page.html')):
            write(page, b'<p>page')
            self.grant(page)
            self.assertIn(page, server.ALLOWED_FILES, 'the page itself is still shown')
            self.assertNotIn(os.path.dirname(page), server.ALLOWED_DIRS, 'the real home or above it brings no folder')
            self.assertEqual(self.get(os.path.join(real_home, 'notes', 'a.css'))[0], 404)
        # a project inside the real home is still granted as usual
        project = os.path.join(real_home, 'site')
        os.makedirs(project)
        write(os.path.join(project, 'index.html'), b'<p>site')
        write(os.path.join(project, 'a.css'), b'site style')
        self.grant(os.path.join(project, 'index.html'))
        self.assertIn(project, server.ALLOWED_DIRS)
        self.assertEqual(self.get(os.path.join(project, 'a.css')), (200, b'site style'))

    def check_linked_private_folder(self):
        """A private folder that is a link or junction (~/.claude moved to another drive): a page above its real place
        grants no folder, a page beside it reaches nothing in it, and a folder granted before the link pointed into it
        is refused at that place once any later grant has recorded where the link leads."""
        drive = os.path.join(self.home, 'drive')
        target = os.path.join(drive, 'claude-config')
        os.makedirs(target)
        write(os.path.join(target, 'x.js'), b'private settings')
        private_link = os.path.join(self.home, '.claude')
        self.link(target, private_link)
        server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (private_link + os.sep,)
        secret = os.path.join(target, 'x.js')
        # above it: the page is shown, its folder is not granted
        above_page = os.path.join(drive, 'page.html')
        write(above_page, b'<p>above')
        self.grant(above_page)
        self.assertIn(above_page, server.ALLOWED_FILES)
        self.assertNotIn(drive, server.ALLOWED_DIRS, 'a folder holding a private folder\'s real place is not granted')
        self.assertEqual(self.get(secret)[0], 404)
        # beside it: granted as usual, and nothing in the private folder is reached from it
        site = os.path.join(drive, 'site')
        os.makedirs(site)
        write(os.path.join(site, 'index.html'), b'<p>beside')
        write(os.path.join(site, 'a.css'), b'site style')
        self.grant(os.path.join(site, 'index.html'))
        self.assertIn(site, server.ALLOWED_DIRS)
        self.assertEqual(self.get(os.path.join(site, 'a.css')), (200, b'site style'))
        self.assertEqual(self.get(os.path.join(site, '..', 'claude-config', 'x.js'))[0], 404)
        self.assertEqual(self.get(secret)[0], 404)
        # pointed into a folder granted earlier: refused there once a later grant has recorded the link's target
        self.unlink(private_link)
        os.makedirs(private_link)                    # a plain folder for now
        server.ALLOWED_FILES.clear(); server.ALLOWED_DIRS.clear()
        server._PRIVATE_TARGETS = (None, ())       # nothing recorded yet for this home
        self.grant(above_page)
        self.assertIn(drive, server.ALLOWED_DIRS, 'granted while the private folder was elsewhere')
        self.assertEqual(self.get(secret), (200, b'private settings'), 'control: not private yet')
        os.rmdir(private_link)
        self.link(target, private_link)
        self.grant(os.path.join(site, 'index.html'))  # any later grant records where the private folder really is
        with patch.object(server, 'PRIVATE_IDS_SECONDS', 0):
            self.assertEqual(self.get(secret)[0], 404)

    def check_what_is_not_granted(self):
        write(os.path.join(self.home, 'top.html'), b'<p>home')
        self.grant(os.path.join(self.home, 'top.html'))
        self.assertIn(os.path.join(self.home, 'top.html'), server.ALLOWED_FILES)
        self.assertNotIn(self.home, server.ALLOWED_DIRS, 'a page in the home folder brings no folder')
        write(os.path.join(self.live_room, 'p.html'), b'<p>private')
        self.grant(os.path.join(self.live_room, 'p.html'))
        self.assertNotIn(os.path.join(self.live_room, 'p.html'), server.ALLOWED_FILES)
        self.grant()
        for path in (os.path.join(self.outside, 'a.css'), os.path.join(self.live_room, 'key'),
                     os.path.join(self.proj, 'index.html.css'), os.path.join(self.proj, 'other.html')):
            self.assertEqual(self.get(path)[0], 404, path)
        write(os.path.join(self.proj, 'other.html'), b'<p>other')
        self.assertEqual(self.get(os.path.join(self.proj, 'other.html'))[0], 404, 'never another page')


@unittest.skipIf(os.name == 'nt', 'POSIX links; Windows runs RealWindows')
class Posix(PreviewCase, unittest.TestCase):
    """The macOS and Linux branch, unchanged by the Windows walk."""

    def make_link(self, target, link):
        os.symlink(target, link)

    def remove_link(self, link):
        os.unlink(link)

    def test_grant_and_serve(self):
        self.check_grant_and_serve()

    def test_links_at_each_position(self):
        self.check_links_at_each_position()

    def test_private_below_the_grant(self):
        self.check_private_below_the_grant()

    def test_second_name_and_swaps(self):
        self.check_second_name_and_swaps()

    def test_what_is_not_granted(self):
        self.check_what_is_not_granted()

    def test_home_reached_through_a_link(self):
        self.check_home_reached_through_a_link()

    def test_a_private_folder_that_is_a_link(self):
        self.check_linked_private_folder()

    def test_a_link_to_a_file_is_refused(self):
        self.grant()
        self.link(os.path.join(self.outside, 'a.css'), os.path.join(self.css, 'link.css'))
        self.assertEqual(self.get(os.path.join(self.css, 'link.css'))[0], 404)


@unittest.skipIf(os.name == 'nt', 'simulates Windows on macOS or Linux')
class SimulatedWindows(Posix):
    """The server's Windows branch, each private_storage call answered by the POSIX walk (see windows_simulation)."""

    def setUp(self):
        from windows_simulation import simulated_windows
        super().setUp()
        self.stack = contextlib.ExitStack()
        self.calls = self.stack.enter_context(simulated_windows())
        self.stack.enter_context(patch.object(server, 'WIN', True))
        self.stack.enter_context(patch.object(server, 'WIN_IDS', True))

    def tearDown(self):
        self.stack.close()
        super().tearDown()

    def test_grant_and_serve(self):
        self.check_grant_and_serve()
        self.assertIn('open_folder', self.calls, 'the folder grant and the asset walk go through the held walk')
        self.assertIn('open_no_links', self.calls, 'the page is opened through the held walk')

    def test_nothing_is_opened_by_its_whole_path(self):
        """The walk opens "/" and then one name at a time; the macOS branch would open the whole path."""
        self.grant()
        real_open = os.open

        def only_the_walk(path, *args, **kwargs):
            if os.path.isabs(path) and path != os.sep:
                raise AssertionError('opened by its whole path')
            return real_open(path, *args, **kwargs)
        with patch.object(os, 'open', only_the_walk):
            self.assertEqual(self.get(self.page)[0], 200)
            self.assertEqual(self.get(self.asset), (200, ASSET))
            self.assertEqual(self.get(os.path.join(self.css, 'missing.css'))[0], 404)

    def test_nothing_granted_before_python_312(self):
        with patch.object(server, 'WIN_IDS', False):
            self.grant()
        self.assertEqual((server.ALLOWED_FILES, server.ALLOWED_DIRS), ({}, {}))
        self.assertEqual(self.get(self.page)[0], 404)

    def test_no_walk_means_nothing_served(self):
        self.grant()
        def refuse(*args, **kwargs):
            raise server.private_storage.PrivateStorageError('private storage unavailable')
        with patch.object(server.private_storage, 'open_folder', refuse), \
                patch.object(server.private_storage, 'open_no_links', refuse):
            self.assertEqual(self.get(self.asset)[0], 404)
            self.assertEqual(self.get(self.page)[0], 404)

    def test_nothing_on_the_request_path_follows_a_link_before_the_walk(self):
        """A link under the grant (say to a network share) must not be followed by anything before the no-link walk:
        while a style or page is served, every call that resolves or stats a path by name fails the test."""
        self.grant()
        server.resolved_home()                       # read once per home, before any request
        self.link(self.outside, os.path.join(self.css, 'share'))
        self.link(self.outside, os.path.join(self.home, '.ssh'))        # a link in place of a private folder
        server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (os.path.join(self.home, '.ssh') + os.sep,)

        def never(*args, **kwargs):
            raise AssertionError('a path was resolved or stat-ed by name while serving')
        with patch.object(os, 'stat', never), patch.object(os, 'lstat', never), \
                patch.object(os.path, 'realpath', never), patch.object(os.path, 'exists', never):
            self.assertEqual(self.get(self.asset), (200, ASSET))
            self.assertEqual(self.get(os.path.join(self.css, 'share', 'a.css'))[0], 404)
            self.assertEqual(self.get(self.page)[0], 200)
        # The private identities are reused for a moment, not re-read for each file of a page.
        opened, real_open_folder = [], server.private_storage.open_folder

        def recorded(path, *args, **kwargs):
            opened.append(path)
            return real_open_folder(path, *args, **kwargs)
        with patch.object(server.private_storage, 'open_folder', recorded):
            for _ in range(3):
                self.assertEqual(self.get(self.asset), (200, ASSET))
        self.assertEqual(opened, [self.proj] * 3, 'only the granted folder is opened for each file')

    def test_private_identities_come_from_the_walk_handles(self):
        """Identity is the reading of the held handle, not os.stat: here the handle reading is replaced by the folder's
        own name, so a folder named like a private one elsewhere is refused only if handles are what is compared."""
        self.grant()
        vault, other_vault = os.path.join(self.proj, 'vault'), os.path.join(self.outside, 'vault')
        for folder in (vault, other_vault):
            os.makedirs(folder)
        write(os.path.join(vault, 'x.css'), b'secret')
        server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (other_vault + os.sep,)
        real_held_id = server.held_id

        def by_name(folder):
            name = os.path.basename(folder.path.rstrip(os.sep))
            return ('name', name) if name == 'vault' else real_held_id(folder)
        with names_not_private():
            self.assertEqual(self.get(os.path.join(vault, 'x.css')), (200, b'secret'), 'control: two different folders')
            with patch.object(server, 'held_id', by_name), patch.object(server, 'PRIVATE_IDS_SECONDS', 0):
                self.assertEqual(self.get(os.path.join(vault, 'x.css'))[0], 404)
                self.assertIn(('name', 'vault'), server.dir_ids([other_vault]), 'dir_ids adds the handle reading on Windows')
                self.assertIn(ident(other_vault), server.dir_ids([other_vault]), 'and keeps the os.stat one')

    def test_dot_names_are_refused(self):
        self.grant()
        write(os.path.join(self.proj, 'up.css'), b'up')
        self.assertEqual(self.get(self.css + os.sep + '..' + os.sep + 'up.css'), (200, b'up'), 'resolved before the walk, still inside')
        self.assertEqual(self.get(self.proj + os.sep + '..' + os.sep + 'site-other' + os.sep + 'a.css')[0], 404)


def junction(target, link):
    import _winapi
    _winapi.CreateJunction(target, link)


@unittest.skipUnless(os.name == 'nt' and server.WIN_IDS, 'real Windows junctions, with Python 3.12 or later')
class RealWindows(PreviewCase, unittest.TestCase):
    """Real NTFS: junctions at each position, a symbolic link to a file, a private folder, a second name, case."""

    def make_link(self, target, link):
        junction(target, link)

    def remove_link(self, link):
        os.rmdir(link)          # removes the junction itself, never what it points to

    def test_grant_and_serve(self):
        self.check_grant_and_serve()

    def test_junctions_at_each_position(self):
        self.check_links_at_each_position()

    def test_private_below_the_grant(self):
        self.check_private_below_the_grant()

    def test_second_name_and_swaps(self):
        self.check_second_name_and_swaps()

    def test_what_is_not_granted(self):
        self.check_what_is_not_granted()

    def test_home_reached_through_a_junction(self):
        self.check_home_reached_through_a_link()

    def test_a_private_folder_that_is_a_link(self):
        self.check_linked_private_folder()

    def test_a_symbolic_link_at_the_file_or_a_folder_is_refused(self):
        self.grant()
        file_link, folder_link = os.path.join(self.css, 'link.css'), os.path.join(self.css, 'linked')
        try:
            os.symlink(os.path.join(self.outside, 'a.css'), file_link)
            os.symlink(self.outside, folder_link, target_is_directory=True)
        except OSError as e:
            self.skipTest('creating a symbolic link needs Developer Mode or the symlink privilege (%s)' % type(e).__name__)
        try:
            self.assertEqual(self.get(file_link)[0], 404)
            self.assertEqual(self.get(os.path.join(folder_link, 'a.css'))[0], 404)
        finally:
            os.unlink(file_link)
            os.rmdir(folder_link)

    def test_case_cannot_reach_a_private_folder_or_escape_the_grant(self):
        self.grant()
        vault = os.path.join(self.proj, 'vault')
        os.makedirs(vault)
        write(os.path.join(vault, 'x.css'), b'secret')
        server.PRIVATE_ROOTS = server.PRIVATE_ROOTS + (vault + os.sep,)
        self.assertEqual(self.get(os.path.join(self.proj, 'CSS', 'A.CSS')), (200, ASSET), 'names below the grant ignore case, as Windows does')
        with names_not_private():     # the identity walk alone
            self.assertEqual(self.get(os.path.join(self.proj, 'VAULT', 'x.css'))[0], 404)
        self.assertEqual(self.get(os.path.join(self.proj, 'VAULT', 'x.css'))[0], 404)
        self.assertEqual(self.get(self.proj.upper() + os.sep + 'CSS' + os.sep + 'A.CSS')[0], 404,
                         'a differently cased grant matches no granted folder')

    def test_names_windows_would_rewrite_or_streams_are_refused_by_the_walk(self):
        """Each path keeps a .css ending and a granted prefix, so it reaches the walk's own name rule (_plain_name):
        a trailing dot or space, which Win32 would drop to open another name, and a colon, which names a stream."""
        self.grant()
        write(self.asset + ':s.css', b'stream')
        refused = ((os.path.join(self.proj, 'css.', 'a.css'), 'folder'), (os.path.join(self.proj, 'css ', 'a.css'), 'folder'),
                   (os.path.join(self.proj, 'css:x', 'a.css'), 'folder'), (self.asset + ':s.css', 'file'))
        for path, subject in refused:
            with self.subTest(path=path):
                with self.assertRaises(server.private_storage.PrivateStorageError) as raised:
                    server.preview_asset_windows(path)
                self.assertEqual((raised.exception.reason, raised.exception.subject), ('unavailable', subject))
                self.assertEqual(self.get(path)[0], 404)

    def test_device_names_below_the_grant_are_only_names(self):
        """CON.css and the like are opened relative to the held folder, where they are plain names, never devices."""
        self.grant()
        for name in ('CON.css', 'nul.css', 'COM1.css'):
            with self.subTest(name=name):
                with self.assertRaises(FileNotFoundError):
                    server.preview_asset_windows(os.path.join(self.css, name))
                self.assertEqual(self.get(os.path.join(self.css, name))[0], 404)

    def test_the_granted_folder_is_held_while_an_asset_is_read(self):
        self.grant()
        seen = []
        real_open = server.private_storage.NoLinkFolder.open

        def open_and_try_rename(folder, name, *args, **kwargs):
            try:
                os.rename(self.proj, self.proj + '-moved')
                seen.append('renamed')
                os.rename(self.proj + '-moved', self.proj)
            except OSError:
                seen.append('held')
            return real_open(folder, name, *args, **kwargs)
        with patch.object(server.private_storage.NoLinkFolder, 'open', open_and_try_rename):
            self.assertEqual(self.get(self.asset), (200, ASSET))
        self.assertEqual(seen, ['held'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
