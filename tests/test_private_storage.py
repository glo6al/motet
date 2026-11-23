#!/usr/bin/env python3
"""The shared private-storage layer on POSIX and Windows. Synthetic files in temporary folders only.

Run: python tests/test_private_storage.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import private_storage as ps

EVERYONE_READ = '*S-1-1-0:(R)'      # a SID, so the test does not depend on the system language


def reason(test, call, expected, subject='file'):
    with test.assertRaises(ps.PrivateStorageError) as raised:
        call()
    test.assertEqual((raised.exception.reason, raised.exception.subject), (expected, subject))
    return raised.exception


class AccessListRule(unittest.TestCase):
    """The Windows decision, fed collected descriptors; runs on every system."""
    USER = 'S-1-5-21-1-2-3-1001'

    def test_owner_must_be_this_user(self):
        with self.assertRaisesRegex(OSError, '^nonprivate storage owner$'):
            ps.acl_verdict(False, True, True, [(0, self.USER, True)], False)

    def test_private_file_names_only_this_user_and_system_and_is_protected(self):
        ps.acl_verdict(True, True, True, [(0, ps.SYSTEM_SID, False), (0, self.USER, True)], False)
        for aces in ([(0, ps.ADMINISTRATORS_SID, False)], [(0, ps.OWNER_RIGHTS_SID, False)],
                     [(0, 'S-1-1-0', False)], [(0, 'S-1-5-32-545', False)], [(1, self.USER, True)]):
            with self.subTest(aces=aces), self.assertRaisesRegex(OSError, '^private storage unavailable$'):
                ps.acl_verdict(True, True, True, [(0, self.USER, True)] + aces, False)
        with self.assertRaises(ps.PrivateStorageError) as raised:
            ps.acl_verdict(True, True, False, [(0, self.USER, True)], False)   # follows its folder's list
        self.assertEqual(raised.exception.reason, 'access')
        with self.assertRaises(ps.PrivateStorageError):
            ps.acl_verdict(True, False, True, [], False)                        # no list at all grants everyone

    def test_private_folder_may_also_name_administrators_and_owner_rights(self):
        ps.acl_verdict(True, True, False, [(0, ps.SYSTEM_SID, False), (0, ps.ADMINISTRATORS_SID, False),
                                           (0, ps.OWNER_RIGHTS_SID, False), (0, self.USER, True)], True)
        for sid in ('S-1-1-0', 'S-1-5-32-545', 'S-1-5-11'):
            with self.subTest(sid=sid), self.assertRaises(ps.PrivateStorageError):
                ps.acl_verdict(True, True, False, [(0, self.USER, True), (0, sid, False)], True)
        with self.assertRaises(ps.PrivateStorageError):
            ps.acl_verdict(True, True, True, [(0, self.USER, True), (1, 'S-1-1-0', False)], True)


class Errors(unittest.TestCase):
    def test_refusals_are_oserrors_with_fixed_words(self):
        error = ps.PrivateStorageError('private storage unavailable', 'nonsense', 'folder')
        self.assertIsInstance(error, OSError)
        self.assertEqual((str(error), error.reason, error.subject), ('private storage unavailable', 'unavailable', 'folder'))


class SimulatedWindowsOpen(unittest.TestCase):
    """The Windows open's order of checks, with the Win32 calls replaced; runs on every system."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(os.path.realpath(self.tmp.name), 'state')
        os.mkdir(self.folder)
        self.path = os.path.join(self.folder, 'secret')
        with open(self.path, 'w') as out:
            out.write('fixture')
        self.opened, self.closed, self.verdicts = [], [], {}
        self.fd_for = lambda handle: os.open(self.path, os.O_RDONLY)

        def fake_open(path, access, share, disposition, flags, sa=None):
            if not os.path.lexists(path):
                error = OSError('missing'); error.winerror = 2
                raise error
            handle = 100 + len(self.opened)
            self.opened.append((handle, path, share))
            return handle

        def fake_check(handle, directory, subject, single_link=False):
            problem = self.verdicts.get('folder' if directory else 'file')
            if problem:
                raise ps.PrivateStorageError('refused', problem, subject)

        def fake_fd(handle, flags=0):
            self.closed.append(handle)          # the descriptor now owns the handle
            return self.fd_for(handle)

        self.patches = [patch.object(ps, 'WIN', True), patch.object(ps, '_win_open', fake_open),
                        patch.object(ps, '_win_close', lambda h: h is not None and self.closed.append(h)),
                        patch.object(ps, '_win_check_handle', fake_check), patch.object(ps, '_win_fd', fake_fd)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def assert_all_closed(self):
        self.assertEqual(sorted(self.closed), sorted(h for h, _, _ in self.opened))

    def test_success_holds_the_folder_without_delete_sharing_until_the_file_is_open(self):
        fd, info = ps.open_private_file(self.path, folder=True)
        try:
            self.assertEqual(os.read(fd, 100), b'fixture')
        finally:
            os.close(fd)
        (folder_handle, folder_path, folder_share), (file_handle, file_path, file_share) = self.opened
        self.assertEqual((folder_path, file_path), (self.folder, self.path))
        self.assertFalse(folder_share & 4, 'folder must not share delete (rename) while held')
        self.assertEqual(file_share, 1, 'file shares read only while it is read')
        self.assert_all_closed()

    def test_refusals_and_their_order(self):
        reason(self, lambda: ps.open_private_file(os.path.join(self.folder, 'absent'), folder=True), 'missing')
        reason(self, lambda: ps.open_private_file(os.path.join(self.folder, 'gone', 'x'), folder=True), 'missing', 'folder')
        self.verdicts['folder'] = 'access'
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'access', 'folder')
        self.assertEqual(len(self.opened), 1, 'the file is not opened behind a refused folder')
        self.verdicts.clear()
        self.verdicts['file'] = 'owner'
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'owner')
        self.assert_all_closed()

    def test_a_link_by_name_is_refused_before_any_open(self):
        link = os.path.join(self.folder, 'link')
        try:
            os.symlink(self.path, link)
        except (OSError, NotImplementedError) as e:
            self.skipTest('symbolic links unavailable here (%s)' % type(e).__name__)
        reason(self, lambda: ps.open_private_file(link), 'link')
        self.assertEqual(self.opened, [])

    def test_a_different_file_behind_the_handle_is_refused_and_closed(self):
        other = os.path.join(self.folder, 'other')
        with open(other, 'w') as out:
            out.write('swapped')
        seen = []
        def swapped(handle):
            seen.append(os.open(other, os.O_RDONLY))
            return seen[-1]
        self.fd_for = swapped
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'changed')
        with self.assertRaises(OSError):
            os.fstat(seen[0])                   # closed by the refusal
        self.assert_all_closed()
        self.fd_for = lambda handle: os.open(self.path, os.O_RDONLY)
        reason(self, lambda: ps.open_private_file(self.path, expect=os.stat(other)), 'changed')


@unittest.skipIf(os.name == 'nt', 'POSIX owner and mode bits')
class Posix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(os.path.realpath(self.tmp.name), 'state')
        ps.make_private_folder(self.folder)
        self.path = os.path.join(self.folder, 'secret')
        fd = ps.create_private_file(self.path)
        os.write(fd, b'fixture value\n')
        os.close(fd)

    def tearDown(self):
        os.chmod(self.folder, 0o700)
        self.tmp.cleanup()

    def test_private_folder_and_file(self):
        self.assertEqual(os.stat(self.folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        ps.check_private(self.folder, directory=True)
        ps.check_private(self.path, single_link=True)
        fd, info = ps.open_private_file(self.path, folder=True, single_link=True, expect=os.stat(self.path))
        try:
            self.assertEqual(os.read(fd, 100), b'fixture value\n')
            self.assertEqual(info.st_size, 14)
        finally:
            os.close(fd)

    def test_refusals(self):
        reason(self, lambda: ps.check_private(os.path.join(self.folder, 'absent')), 'missing')
        reason(self, lambda: ps.open_private_file(os.path.join(self.folder, 'absent')), 'missing')
        reason(self, lambda: ps.check_private(self.path, directory=True), 'not_regular', 'folder')
        os.chmod(self.path, 0o640)
        reason(self, lambda: ps.check_private(self.path), 'access')
        reason(self, lambda: ps.open_private_file(self.path), 'access')
        os.chmod(self.path, 0o400)
        os.close(ps.open_private_file(self.path)[0])        # owner-only modes other than 600 stay accepted
        os.chmod(self.path, 0o600)
        link = os.path.join(self.folder, 'link')
        os.symlink(self.path, link)
        reason(self, lambda: ps.check_private(link), 'link')
        reason(self, lambda: ps.open_private_file(link), 'link')
        second = os.path.join(self.folder, 'second-name')
        os.link(self.path, second)
        reason(self, lambda: ps.check_private(self.path, single_link=True), 'links')
        reason(self, lambda: ps.open_private_file(self.path, single_link=True), 'links')
        os.close(ps.open_private_file(self.path)[0])        # extra names are allowed unless asked
        os.unlink(second)
        pipe = os.path.join(self.folder, 'pipe')
        os.mkfifo(pipe, 0o600)
        reason(self, lambda: ps.open_private_file(pipe), 'not_regular')   # and the open does not hang
        os.chmod(self.folder, 0o755)
        reason(self, lambda: ps.check_private(self.folder, directory=True), 'access', 'folder')
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'access', 'folder')
        os.close(ps.open_private_file(self.path)[0])

    def test_creation_is_exclusive_and_private(self):
        with self.assertRaises(FileExistsError):
            ps.create_private_file(self.path)
        link = os.path.join(self.folder, 'dangling')
        os.symlink(os.path.join(self.folder, 'target'), link)
        with self.assertRaises(FileExistsError):
            ps.create_private_file(link)
        self.assertFalse(os.path.lexists(os.path.join(self.folder, 'target')))
        old = os.umask(0o277)
        try:
            fd, temporary = ps.create_private_temp(self.folder, '.fixture-')
        finally:
            os.umask(old)
        os.close(fd)
        self.assertTrue(os.path.basename(temporary).startswith('.fixture-'))
        self.assertEqual(os.stat(temporary).st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            ps.make_private_folder(self.folder)


class CreationCleanup(unittest.TestCase):
    """A new file whose post-create check fails is removed, not left behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(os.path.realpath(self.tmp.name), 'state')
        ps.make_private_folder(self.folder)
        self.path = os.path.join(self.folder, 'secret')
        self.check = '_win_check_handle' if os.name == 'nt' else 'posix_verdict'

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_refused_new_file_is_removed(self):
        refused = ps.PrivateStorageError('refused', 'access')
        with patch.object(ps, self.check, side_effect=refused):
            reason(self, lambda: ps.create_private_file(self.path), 'access')
        self.assertFalse(os.path.lexists(self.path))
        if os.name == 'nt':
            with patch.object(ps, self.check, side_effect=refused):
                reason(self, lambda: ps.create_private_temp(self.folder, '.fixture-'), 'access')
        else:
            with patch.object(ps.os, 'fchmod', side_effect=PermissionError('refused')):
                with self.assertRaises(PermissionError):
                    ps.create_private_temp(self.folder, '.fixture-')
        self.assertEqual(os.listdir(self.folder), [])
        os.close(ps.create_private_file(self.path))       # the name is free again
        ps.check_private(self.path, single_link=True)

    @unittest.skipIf(os.name == 'nt', 'POSIX removes by name; Windows removes through the handle')
    def test_a_name_replaced_before_the_cleanup_is_left_alone(self):
        def replace_then_refuse(*args, **kwargs):
            os.rename(self.path, self.path + '.created')
            with open(self.path, 'w') as out:
                out.write('someone else')
            raise ps.PrivateStorageError('refused', 'access')
        with patch.object(ps, 'posix_verdict', side_effect=replace_then_refuse):
            reason(self, lambda: ps.create_private_file(self.path), 'access')
        with open(self.path) as kept:
            self.assertEqual(kept.read(), 'someone else')

    def test_windows_cleanup_deletes_through_the_created_handle(self):
        calls = []
        class Nothing:
            def __init__(self, *a): pass
            def __enter__(self): return None
            def __exit__(self, *exc): return False
        def fake_open(path, access, share, disposition, flags, sa=None):
            calls.append(('open', access, share, disposition))
            return 7
        with patch.object(ps, 'WIN', True), patch.object(ps, '_CurrentUser', Nothing), patch.object(ps, '_PrivateAttributes', Nothing), \
                patch.object(ps, '_win_open', fake_open), patch.object(ps, '_win_close', lambda h: calls.append(('close', h))), \
                patch.object(ps, '_win_check_handle', side_effect=ps.PrivateStorageError('refused', 'owner')), \
                patch.object(ps, '_win_discard', lambda h: calls.append(('discard', h))):
            reason(self, lambda: ps.create_private_file(self.path), 'owner')
        (_, access, share, disposition), last = calls[0], calls[-1]
        self.assertTrue(access & ps._DELETE, 'the handle can delete its own file')
        self.assertEqual((share, disposition), (0, ps._CREATE_NEW))
        self.assertEqual(last, ('discard', 7))
        self.assertNotIn(('close', 7), calls, 'closed only by the discard')


class Lock(unittest.TestCase):
    """open_lock_file, try_lock and unlock have no callers yet; this keeps them working until messaging uses them."""

    def test_exclusive_non_blocking_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(os.path.realpath(tmp), 'state')
            ps.make_private_folder(folder)
            path = os.path.join(folder, 'fixture.lock')
            first = ps.open_lock_file(path)
            second = ps.open_lock_file(path)
            try:
                ps.check_private(path, single_link=True)
                self.assertTrue(ps.try_lock(first))
                self.assertFalse(ps.try_lock(second))
                ps.unlock(first)
                self.assertTrue(ps.try_lock(second))
                self.assertFalse(ps.try_lock(first))
                ps.unlock(second)
            finally:
                os.close(first)
                os.close(second)
            if os.name != 'nt':
                link = os.path.join(folder, 'link.lock')
                os.symlink(path, link)
                with self.assertRaises(OSError):
                    ps.open_lock_file(link)


def icacls(*args):
    subprocess.run(['icacls'] + list(args), check=True, capture_output=True, timeout=30)


def junction(link, target):
    """A directory junction needs no special privilege, unlike a symbolic link."""
    subprocess.run(['cmd', '/c', 'mklink', '/J', link, target], check=True, capture_output=True, timeout=30)


@unittest.skipUnless(os.name == 'nt', 'requires real Windows access lists and reparse points')
class Windows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.folder = os.path.join(base, 'state')
        # Explicit ownership: an elevated test runner's default owner can be Administrators.
        ps.make_private_folder(self.folder)
        self.path = os.path.join(self.folder, 'secret')
        fd = ps.create_private_file(self.path)
        os.write(fd, b'fixture value\n')
        os.close(fd)

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_folder_and_file_open_and_identity(self):
        ps.windows_security(self.folder, True)
        ps.windows_security(self.path)
        ps.check_private(self.folder, directory=True)
        ps.check_private(self.path, single_link=True)
        fd, info = ps.open_private_file(self.path, folder=True, single_link=True, expect=os.stat(self.path))
        try:
            self.assertEqual(os.read(fd, 100), b'fixture value\n')
            self.assertEqual(info.st_size, 14)
            self.assertTrue(os.path.samestat(info, os.stat(self.path)))
        finally:
            os.close(fd)

    def test_file_readable_by_everyone_is_refused(self):
        icacls(self.path, '/grant', EVERYONE_READ)
        reason(self, lambda: ps.check_private(self.path), 'access')
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'access')

    def test_folder_open_to_everyone_is_refused(self):
        icacls(self.folder, '/grant', EVERYONE_READ)
        reason(self, lambda: ps.check_private(self.folder, directory=True), 'access', 'folder')
        reason(self, lambda: ps.open_private_file(self.path, folder=True), 'access', 'folder')
        fd, _ = ps.open_private_file(self.path)       # without the folder rule the file alone still passes
        os.close(fd)

    def test_inherited_file_is_refused_until_made_private_with_the_documented_command(self):
        plain = os.path.join(self.folder, 'plain')
        with open(plain, 'w') as out:
            out.write('fixture')                     # follows the folder's list: not protected
        # An elevated runner's default owner for new files can be Administrators: that is refused first.
        owned = self.owned_by_me(plain)
        reason(self, lambda: ps.open_private_file(plain), 'access' if owned else 'owner')
        reason(self, lambda: windows_handle_check(plain), 'access' if owned else 'owner')
        if not owned:
            subprocess.run('icacls "%s" /setowner "%%USERNAME%%"' % plain, shell=True, check=True, capture_output=True, timeout=30)
            reason(self, lambda: ps.open_private_file(plain), 'access')
        subprocess.run('icacls "%s" /inheritance:r /grant:r "%%USERNAME%%:F"' % plain, shell=True, check=True, capture_output=True, timeout=30)
        fd, _ = ps.open_private_file(plain, folder=True)
        os.close(fd)

    def owned_by_me(self, path):
        try:
            ps.windows_security(path)
            return True
        except ps.PrivateStorageError as e:
            return e.reason != 'owner'

    def test_junction_in_place_of_the_folder_is_refused(self):
        link = os.path.join(os.path.dirname(self.folder), 'junction')
        junction(link, self.folder)
        reason(self, lambda: ps.check_private(link, directory=True), 'link', 'folder')
        reason(self, lambda: ps.open_private_file(os.path.join(link, 'secret'), folder=True), 'link', 'folder')

    def test_symbolic_link_to_the_file_is_refused(self):
        link = os.path.join(self.folder, 'link')
        try:
            os.symlink(self.path, link)
        except OSError as e:
            self.skipTest('creating a file symbolic link needs Developer Mode or the symlink privilege (%s)' % type(e).__name__)
        reason(self, lambda: ps.check_private(link), 'link')
        reason(self, lambda: ps.open_private_file(link, folder=True), 'link')

    def test_second_name_and_changed_identity_are_refused(self):
        second = os.path.join(self.folder, 'second-name')
        os.link(self.path, second)
        reason(self, lambda: ps.check_private(self.path, single_link=True), 'links')
        reason(self, lambda: ps.open_private_file(self.path, single_link=True), 'links')
        os.close(ps.open_private_file(self.path)[0])
        os.unlink(second)
        other = os.path.join(self.folder, 'other')
        os.close(ps.create_private_file(other))
        reason(self, lambda: ps.open_private_file(self.path, expect=os.stat(other)), 'changed')
        real_lstat = os.lstat
        with patch.object(ps.os, 'lstat', side_effect=lambda p, *a, **k: real_lstat(other if p == self.path else p)):
            reason(self, lambda: ps.open_private_file(self.path, folder=True), 'changed')   # the name pointed elsewhere when looked at

    def test_held_file_and_folder_cannot_be_swapped_while_opening(self):
        fd, _ = ps.open_private_file(self.path, folder=True)
        try:
            with self.assertRaises(OSError):
                os.replace(self.path, self.path + '.moved')     # the reading handle does not share delete
        finally:
            os.close(fd)

    def test_created_files_are_private_and_exclusive(self):
        with self.assertRaises(OSError):
            ps.create_private_file(self.path)
        fd, temporary = ps.create_private_temp(self.folder, '.fixture-')
        os.write(fd, b'x')
        os.close(fd)
        ps.windows_security(temporary)
        ps.check_private(temporary, single_link=True)
        os.replace(temporary, self.path)
        ps.check_private(self.path, single_link=True)
        with self.assertRaises(OSError):
            ps.make_private_folder(self.folder)


def windows_handle_check(path):
    """Owner and list of an existing file, read on a handle (test helper)."""
    handle = ps._win_open(path, ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, 7, ps._OPEN_EXISTING, 0)
    try:
        ps.windows_handle_security(handle)
    finally:
        ps._win_close(handle)



if __name__ == '__main__':
    unittest.main(verbosity=2)
