#!/usr/bin/env python3
"""The shared private-storage layer on POSIX and Windows. Synthetic files in temporary folders only.

Run: python tests/test_private_storage.py
"""
from contextlib import contextmanager
import ctypes
import errno
import os
import stat
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


class WriteRule(unittest.TestCase):
    """acl_write_verdict ("nobody else can change this"), fed collected descriptors; runs on every system."""
    USER, OTHER, USERS = 'S-1-5-21-1-2-3-1001', 'S-1-5-21-1-2-3-1002', 'S-1-5-32-545'
    FULL, READ = 0x1F01FF, 0x1200A9                     # full control; read and execute

    def ace(self, sid, mask, ace_type=0, flags=0):
        return (ace_type, flags, mask, sid, sid == self.USER)

    def profile(self):
        """A typical folder under the user's profile: SYSTEM, Administrators and the user, each inherited."""
        return [self.ace(ps.SYSTEM_SID, self.FULL, flags=0x13), self.ace(ps.ADMINISTRATORS_SID, self.FULL, flags=0x13),
                self.ace(self.USER, self.FULL, flags=0x13)]

    def test_the_owner_must_be_this_user_or_with_system_owned_the_system(self):
        ps.acl_write_verdict(self.USER, True, True, self.profile())
        reason(self, lambda: ps.acl_write_verdict(ps.ADMINISTRATORS_SID, False, True, self.profile()), 'owner')
        for owner in (ps.SYSTEM_SID, ps.ADMINISTRATORS_SID, ps.TRUSTED_INSTALLER_SID):
            ps.acl_write_verdict(owner, False, True, [], system_owned=True)
        reason(self, lambda: ps.acl_write_verdict(self.USER, True, True, [], system_owned=True), 'owner')
        reason(self, lambda: ps.acl_write_verdict(self.OTHER, False, True, [], system_owned=True), 'owner')

    def test_anyone_else_with_a_change_right_is_refused_and_readers_are_not(self):
        ps.acl_write_verdict(self.USER, True, True, self.profile() + [self.ace(self.USERS, self.READ), self.ace(ps.OWNER_RIGHTS_SID, self.FULL)])
        for mask in (0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000, 0x10000000, 0x40000000):
            for sid in (self.OTHER, self.USERS, 'S-1-5-11', 'S-1-1-0', 'S-1-3-0'):
                with self.subTest(mask=hex(mask), sid=sid):
                    aces = self.profile() + [self.ace(sid, mask)]
                    reason(self, lambda: ps.acl_write_verdict(self.USER, True, True, aces), 'access')
        reason(self, lambda: ps.acl_write_verdict(self.USER, True, False, []), 'access')   # no list grants everything

    def test_deny_and_inherit_only_entries_are_passed_over_and_other_types_refused(self):
        ps.acl_write_verdict(self.USER, True, True, self.profile() + [self.ace('S-1-1-0', self.FULL, ace_type=1)])
        creator_owner = self.ace('S-1-3-0', 0x10000000, flags=0x8 | 0x3)     # inherit only: applies to children
        ps.acl_write_verdict(self.USER, True, True, self.profile() + [creator_owner])
        for entry in ((5, 0, 0x2, None, False), (9, 0, 0x1, None, False)):   # object and callback allow entries
            with self.subTest(ace_type=entry[0]):
                reason(self, lambda: ps.acl_write_verdict(self.USER, True, True, [entry]), 'access')

    def test_a_system_program_may_be_changed_by_the_system_only(self):
        program = [self.ace(ps.TRUSTED_INSTALLER_SID, self.FULL), self.ace(ps.SYSTEM_SID, self.READ),
                   self.ace(ps.ADMINISTRATORS_SID, self.READ), self.ace(self.USERS, self.READ)]
        ps.acl_write_verdict(ps.TRUSTED_INSTALLER_SID, False, True, program, system_owned=True)
        ps.acl_write_verdict(ps.ADMINISTRATORS_SID, False, True, program + [self.ace(ps.SYSTEM_SID, self.FULL)], system_owned=True)
        reason(self, lambda: ps.acl_write_verdict(ps.TRUSTED_INSTALLER_SID, False, True,
                                                  program + [self.ace(self.USER, 0x2)], system_owned=True), 'access')


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
        self.opened, self.closed, self.verdicts, self.access = [], [], {}, {}
        self.fd_for = lambda handle: os.open(self.path, os.O_RDONLY)

        def fake_open(path, access, share, disposition, flags, sa=None):
            if not os.path.lexists(path):
                error = OSError('missing'); error.winerror = 2
                raise error
            handle = 100 + len(self.opened)
            self.opened.append((handle, path, share))
            self.access[handle] = access
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
        self.assertTrue(self.access[folder_handle] & (ps._FILE_LIST_DIRECTORY | ps._FILE_TRAVERSE),
                        'Windows applies sharing only to an open that asks for data access')
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


class WindowsDeletionCalls(unittest.TestCase):
    """_win_set_delete's order of calls, with SetFileInformationByHandle replaced; runs on every system."""

    def run_with(self, accepted, posix=True):
        calls = []

        class Kernel:
            @staticmethod
            def SetFileInformationByHandle(handle, info_class, pointer, size):
                value = pointer._obj.value
                calls.append((info_class, value, size))
                return (info_class, value) in accepted
        with patch.object(ps, '_win_api', lambda: (None, Kernel)), patch.object(ps, '_POSIX_DELETE', posix):
            try:
                return ps._win_set_delete(7), calls
            except ps.PrivateStorageError as e:
                return e.reason, calls

    def test_posix_deletion_first_with_read_only_ignored_then_without_then_the_older_call(self):
        ex, old = ps._FILE_DISPOSITION_INFO_EX, ps._FILE_DISPOSITION_INFO
        full, plain = 0x1 | 0x2 | 0x10, 0x1 | 0x2
        tries = [(ex, full, 4), (ex, plain, 4), (old, 1, 1)]
        self.assertEqual(self.run_with({(ex, full)}), ('posix', tries[:1]))
        self.assertEqual(self.run_with({(ex, plain)}), ('posix', tries[:2]), 'a system without the read-only flag')
        self.assertEqual(self.run_with({(old, 1)}), ('legacy', tries), 'a system without POSIX deletion')
        self.assertEqual(self.run_with(set()), ('open', tries))
        self.assertEqual(self.run_with({(ex, full), (old, 1)}, posix=False), ('legacy', tries[2:]))


class ReadWrite(unittest.TestCase):
    """open_private_rw: agent messaging opens its lock and database with it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(os.path.realpath(self.tmp.name), 'state')
        ps.make_private_folder(self.folder)
        self.path = os.path.join(self.folder, 'fixture.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_then_reopen_for_writing(self):
        with self.assertRaises(OSError):
            ps.open_private_rw(self.path)                 # without create a missing file is an error
        self.assertFalse(os.path.lexists(self.path))
        fd = ps.open_private_rw(self.path, create=True)
        try:
            os.write(fd, b'fixture')
        finally:
            os.close(fd)
        ps.check_private(self.path, single_link=True)
        if os.name == 'nt':
            ps.windows_security(self.path)
        else:
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        fd = ps.open_private_rw(self.path)
        try:
            self.assertEqual(os.read(fd, 100), b'fixture')
        finally:
            os.close(fd)

    @unittest.skipIf(os.name == 'nt', 'POSIX owner and mode bits')
    def test_posix_refusals(self):
        os.close(ps.open_private_rw(self.path, create=True))
        os.chmod(self.path, 0o644)
        reason(self, lambda: ps.open_private_rw(self.path), 'access')
        reason(self, lambda: ps.open_private_rw(self.path, create=True), 'access')
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644, 'an existing file is never repaired')
        os.chmod(self.path, 0o600)
        os.link(self.path, self.path + '.second')
        reason(self, lambda: ps.open_private_rw(self.path), 'links')
        os.unlink(self.path + '.second')
        link = os.path.join(self.folder, 'link')
        os.symlink(self.path, link)
        with self.assertRaises(OSError) as raised:      # the open refuses the link: the system's own error
            ps.open_private_rw(link, create=True)
        self.assertNotIsInstance(raised.exception, ps.PrivateStorageError)
        with patch.object(ps.os, 'getuid', return_value=os.getuid() + 1):
            reason(self, lambda: ps.open_private_rw(self.path), 'owner')

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows access lists and reparse points')
    def test_windows_refusals_and_held_name(self):
        os.close(ps.open_private_rw(self.path, create=True))
        fd = ps.open_private_rw(self.path)
        try:
            with self.assertRaises(OSError):
                os.replace(self.path, self.path + '.moved')   # the handle does not share delete
        finally:
            os.close(fd)
        icacls(self.path, '/grant', EVERYONE_READ)
        reason(self, lambda: ps.open_private_rw(self.path), 'access')
        reason(self, lambda: ps.open_private_rw(self.path, create=True), 'access')
        target = os.path.join(os.path.dirname(self.folder), 'target')
        os.mkdir(target)
        reason(self, lambda: ps.open_private_rw(target), 'not_regular')
        junction(os.path.join(self.folder, 'junction'), target)
        reason(self, lambda: ps.open_private_rw(os.path.join(self.folder, 'junction')), 'link')


class Lock(unittest.TestCase):
    """open_lock_file, try_lock and unlock: agent messaging holds its lifetime lock with them."""

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


    def test_close_lock_releases_a_held_lock_and_closes_an_unheld_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(os.path.realpath(tmp), 'state')
            ps.make_private_folder(folder)
            path = os.path.join(folder, 'fixture.lock')
            first, second = ps.open_lock_file(path), ps.open_lock_file(path)
            self.assertTrue(ps.try_lock(first))
            self.assertFalse(ps.try_lock(second))
            ps.close_lock(second)                     # never held: simply closed
            ps.close_lock(first)
            third = ps.open_lock_file(path)
            try:
                self.assertTrue(ps.try_lock(third))
            finally:
                ps.close_lock(third)
            with self.assertRaises(OSError):
                os.fstat(first)

    def test_a_lock_survives_on_a_duplicate_until_the_last_copy_closes(self):
        """Attachment storage keeps its writer lock on a duplicated descriptor (``os.dup``). A POSIX flock and a
        Windows byte-range lock both belong to the open file, not to one descriptor of it: closing the original
        leaves the lock held through the duplicate, the duplicate can release it, and closing the last copy releases
        it (Windows may take a moment after the close)."""
        import time
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(os.path.realpath(tmp), 'state')
            ps.make_private_folder(folder)
            path = os.path.join(folder, 'fixture.lock')
            first = ps.open_lock_file(path)
            self.assertTrue(ps.try_lock(first))
            copy = os.dup(first)
            os.close(first)
            other = ps.open_lock_file(path)
            try:
                self.assertFalse(ps.try_lock(other), 'the lock survives the original closing while a duplicate is open')
                ps.unlock(copy)
                self.assertTrue(ps.try_lock(other), 'the duplicate released the lock')
                ps.unlock(other)
                self.assertTrue(ps.try_lock(copy))
                os.close(copy)
                copy = None
                deadline = time.monotonic() + 2
                while not ps.try_lock(other):
                    self.assertLess(time.monotonic(), deadline, 'closing the last copy did not release the lock')
                    time.sleep(0.05)
                ps.unlock(other)
            finally:
                if copy is not None:
                    os.close(copy)
                ps.close_lock(other)


class SqliteCompanions(unittest.TestCase):
    def test_plain_journal_passes_and_a_second_name_or_folder_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(os.path.realpath(tmp), 'fixture.sqlite3')
            self.assertTrue(ps.sqlite_companions_plain(database))
            with open(database + '-journal', 'wb') as out:
                out.write(b'fixture')
            self.assertTrue(ps.sqlite_companions_plain(database))
            os.link(database + '-journal', database + '-second')
            self.assertFalse(ps.sqlite_companions_plain(database))
            os.unlink(database + '-second')
            os.mkdir(database + '-shm')
            self.assertFalse(ps.sqlite_companions_plain(database))


# ---------------------------------------------------------------------------
# The no-link folder walk (open_folder, open_no_links).

@unittest.skipIf(os.name == 'nt', 'POSIX links and modes; Windows runs WindowsWalk below')
class PosixWalk(unittest.TestCase):
    """Links in every position, swapped folders, wrong types, second names and privacy, with nothing left open."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from handle_leaks import no_leaked_handles
        self.no_leaks = lambda: no_leaked_handles(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.top = os.path.join(self.base, 'top')
        self.middle = os.path.join(self.top, 'middle')
        os.makedirs(self.middle, 0o700)
        self.file = os.path.join(self.middle, 'notes.jsonl')
        with open(self.file, 'w') as out:
            out.write('fixture\n')
        os.chmod(self.file, 0o600)
        self.elsewhere = os.path.join(self.base, 'elsewhere')
        os.makedirs(os.path.join(self.elsewhere, 'middle'))
        with open(os.path.join(self.elsewhere, 'middle', 'notes.jsonl'), 'w') as out:
            out.write('not this one\n')

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, fd):
        try:
            return os.read(fd, 100)
        finally:
            os.close(fd)

    def test_available_here(self):
        self.assertTrue(ps.folder_walk_available())

    def test_file_folder_and_listing(self):
        with self.no_leaks():
            self.assertEqual(self.read(ps.open_no_links(self.file, single_link=True, private=True)), b'fixture\n')
            fd = ps.open_no_links(self.middle, directory=True, private=True)
            try:
                self.assertTrue(os.path.samestat(os.fstat(fd), os.stat(self.middle)))
            finally:
                os.close(fd)
            with ps.open_folder(self.top) as top, top.folder('middle') as middle:
                self.assertEqual(middle.names(), ['notes.jsonl'])
                self.assertEqual(self.read(middle.open('notes.jsonl')), b'fixture\n')
                self.assertTrue(os.path.samestat(middle.stat(), os.stat(self.middle)))
            fd = ps.open_no_links('/', directory=True)
            os.close(fd)

    def test_a_link_in_every_position_is_refused(self):
        positions = {
            'top folder': (self.top, os.path.join(self.base, 'top-link'), os.path.join(self.base, 'top-link', 'middle', 'notes.jsonl')),
            'middle folder': (self.middle, os.path.join(self.top, 'middle-link'), os.path.join(self.top, 'middle-link', 'notes.jsonl')),
            'file': (self.file, os.path.join(self.middle, 'file-link'), os.path.join(self.middle, 'file-link')),
        }
        for position, (target, link, path) in positions.items():
            os.symlink(target, link)
            with self.subTest(position=position), self.no_leaks():
                with self.assertRaises(OSError):
                    os.close(ps.open_no_links(path))
                with self.assertRaises(OSError):
                    ps.open_folder(link).close()
        # The base folder itself reached through a link: every component counts, not just the last ones.
        os.symlink(self.base, os.path.join(self.elsewhere, 'base-link'))
        with self.no_leaks(), self.assertRaises(OSError):
            os.close(ps.open_no_links(os.path.join(self.elsewhere, 'base-link', 'top', 'middle', 'notes.jsonl')))

    def test_a_held_folder_swapped_for_a_link_still_reads_the_folder_that_was_checked(self):
        with self.no_leaks():
            with ps.open_folder(self.top) as top:
                os.rename(self.top, self.top + '-moved')
                os.symlink(self.elsewhere, self.top)
                with top.folder('middle') as middle:
                    self.assertEqual(self.read(middle.open('notes.jsonl')), b'fixture\n')
            with self.assertRaises(OSError):
                os.close(ps.open_no_links(self.file))        # a fresh walk meets the link and refuses

    def test_wrong_types_and_second_names(self):
        with self.no_leaks():
            reason(self, lambda: os.close(ps.open_no_links(self.middle)), 'not_regular')
            with self.assertRaises(NotADirectoryError):
                ps.open_folder(self.file).close()
            with self.assertRaises(FileNotFoundError):
                os.close(ps.open_no_links(os.path.join(self.middle, 'absent')))
            with self.assertRaises(FileNotFoundError):
                ps.open_folder(os.path.join(self.base, 'absent', 'deeper')).close()
            pipe = os.path.join(self.middle, 'pipe')
            os.mkfifo(pipe)
            reason(self, lambda: os.close(ps.open_no_links(pipe)), 'not_regular')       # opened without hanging
            os.link(self.file, os.path.join(self.middle, 'second-name'))
            reason(self, lambda: os.close(ps.open_no_links(self.file, single_link=True)), 'links')
            os.close(ps.open_no_links(self.file))                                         # allowed unless asked
            with ps.open_folder(self.middle) as middle:
                for name in ('', '.', '..', 'a/b', 'x\0y'):
                    with self.subTest(name=name):
                        reason(self, lambda: middle.open(name), 'unavailable')
                        reason(self, lambda: middle.folder(name), 'unavailable', 'folder')

    def test_privacy_is_checked_on_what_was_opened(self):
        with self.no_leaks():
            os.chmod(self.file, 0o644)
            reason(self, lambda: os.close(ps.open_no_links(self.file, private=True)), 'access')
            os.close(ps.open_no_links(self.file))
            os.chmod(self.middle, 0o755)
            reason(self, lambda: ps.open_folder(self.middle, private=True).close(), 'access', 'folder')
            reason(self, lambda: os.close(ps.open_no_links(self.middle, directory=True, private=True)), 'access', 'folder')
            with ps.open_folder(self.top) as top:
                reason(self, lambda: top.folder('middle', private=True).close(), 'access', 'folder')

    def test_a_closed_folder_refuses_further_use(self):
        top = ps.open_folder(self.top)
        top.close()
        top.close()                                            # closing twice is harmless
        reason(self, lambda: top.open('middle', directory=True), 'changed', 'folder')
        reason(self, top.names, 'changed', 'folder')


@unittest.skipIf(os.name == 'nt', 'POSIX links and modes; Windows runs WindowsChanges below')
class PosixChanges(unittest.TestCase):
    """Section (h) on POSIX: the dir_fd calls the attachment code makes today, through a held folder."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from handle_leaks import no_leaked_handles
        self.no_leaks = lambda: no_leaked_handles(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.state = os.path.join(self.base, 'state')
        os.mkdir(self.state, 0o700)
        self.secret = os.path.join(self.state, 'secret')
        fd = ps.create_private_file(self.secret)
        os.write(fd, b'fixture\n')
        os.close(fd)
        self.elsewhere = os.path.join(self.base, 'elsewhere')

    def tearDown(self):
        self.tmp.cleanup()

    def writable(self):
        return ps.open_folder(self.state, writable=True)

    def test_changes_need_a_writable_hold_and_a_plain_name(self):
        with self.no_leaks():
            with ps.open_folder(self.state) as state:
                for change in (lambda: state.create('new'), lambda: state.open_or_create('new'),
                               lambda: state.mkdir('new'), lambda: state.remove('secret', lambda fd: True), state.sync):
                    reason(self, change, 'unavailable', 'folder')
            self.assertEqual(sorted(os.listdir(self.state)), ['secret'])
            with self.writable() as state:
                for name in ('', '.', '..', 'a/b', 'x\0y'):
                    with self.subTest(name=name):
                        reason(self, lambda: state.create(name), 'unavailable')
                        reason(self, lambda: state.open_or_create(name), 'unavailable')
                        reason(self, lambda: state.mkdir(name), 'unavailable', 'folder')
                        reason(self, lambda: state.remove(name, lambda fd: True), 'unavailable')
                        reason(self, lambda: state.child_stat(name), 'unavailable')
            top = ps.open_folder(self.base)
            child = top.folder('state', writable=True)
            os.close(child.create('through-a-child'))
            child.close()
            top.close()
            reason(self, lambda: child.create('late'), 'changed', 'folder')

    def test_create_is_exclusive_private_and_never_follows_a_link(self):
        os.symlink(self.elsewhere, os.path.join(self.state, 'link'))
        with self.no_leaks(), self.writable() as state:
            fd = state.create('new')
            try:
                import fcntl
                self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDWR)
                self.assertFalse(os.get_inheritable(fd))
                info = os.fstat(fd)
                self.assertEqual((info.st_mode & 0o077, info.st_nlink, info.st_uid), (0, 1, os.getuid()))
                os.write(fd, b'abc')
            finally:
                os.close(fd)
            for name in ('new', 'secret', 'link'):
                with self.subTest(name=name), self.assertRaises(FileExistsError):
                    state.create(name)
        self.assertFalse(os.path.lexists(self.elsewhere), 'nothing was made through the link')
        with open(os.path.join(self.state, 'new'), 'rb') as made:
            self.assertEqual(made.read(), b'abc')
        previous = os.umask(0o377)                     # a umask that would leave the owner without write
        try:
            with self.writable() as state:
                os.close(state.create('under-umask'))
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, 'under-umask')).st_mode), 0o600)

    def test_a_refused_new_file_is_removed(self):
        with self.no_leaks(), self.writable() as state:
            with patch.object(ps, 'posix_verdict', side_effect=ps.PrivateStorageError('refused', 'access')):
                reason(self, lambda: state.create('new'), 'access')
        self.assertFalse(os.path.lexists(os.path.join(self.state, 'new')))

    def test_open_or_create_opens_a_private_file_or_makes_one(self):
        os.symlink(self.secret, os.path.join(self.state, 'link'))
        public = os.path.join(self.state, 'public')
        with open(public, 'wb') as out:
            out.write(b'x')
        os.chmod(public, 0o644)
        os.link(self.secret, os.path.join(self.state, 'second-name'))
        with self.no_leaks(), self.writable() as state:
            first, second = state.open_or_create('fixture.lock'), state.open_or_create('fixture.lock')
            try:
                self.assertTrue(os.path.samestat(os.fstat(first), os.fstat(second)))
                self.assertEqual(os.fstat(first).st_mode & 0o077, 0)
            finally:
                os.close(first)
                os.close(second)
            with self.assertRaises(OSError) as raised:
                state.open_or_create('link')
            self.assertEqual(raised.exception.errno, errno.ELOOP)
            reason(self, lambda: state.open_or_create('public'), 'access')
            reason(self, lambda: state.open_or_create('second-name'), 'links')

    def test_mkdir_makes_a_private_folder_once(self):
        with self.no_leaks(), self.writable() as state:
            self.assertIsNone(state.mkdir('sub'))
            with self.assertRaises(FileExistsError):
                state.mkdir('sub')
            with self.assertRaises(FileExistsError):
                state.mkdir('secret')
            with state.folder('sub', private=True, writable=True) as sub:
                os.close(sub.create('inner'))
        self.assertEqual(os.stat(os.path.join(self.state, 'sub')).st_mode & 0o077, 0)

    def test_remove_deletes_only_the_file_it_recognised(self):
        import socket
        os.symlink(self.secret, os.path.join(self.state, 'link'))
        os.mkdir(os.path.join(self.state, 'sub'))
        os.mkfifo(os.path.join(self.state, 'pipe'))
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(os.path.join(self.state, 'sock'))
        seen = []

        def expect(fd):
            self.assertTrue(ps.read_only(fd))
            seen.append(os.read(fd, 100))
            return True
        with self.no_leaks(), self.writable() as state:
            for name in ('link', 'sub', 'pipe', 'sock'):
                with self.subTest(name=name):
                    self.assertEqual(state.remove(name, expect), 'changed')     # the pipe is opened without hanging
            self.assertEqual(seen, [], 'expect is asked only about a regular file')
            self.assertEqual(state.remove('secret', lambda fd: False), 'changed')
            self.assertEqual(state.remove('absent', expect), 'absent')
            self.assertEqual(state.remove('secret', expect), 'removed')
            self.assertEqual(seen, [b'fixture\n'])
            self.assertEqual(state.remove('secret', expect), 'absent')
            state.sync()
        self.assertEqual(sorted(os.listdir(self.state)), ['link', 'pipe', 'sock', 'sub'])

    def test_open_append_is_the_exclusion_files_open(self):
        exclude = os.path.join(self.state, 'exclude')
        with open(exclude, 'wb') as out:
            out.write(b'# fixture\n')
        os.chmod(exclude, 0o644)                       # readable by others is fine; changeable is not
        os.symlink(exclude, os.path.join(self.state, 'link'))
        os.link(self.secret, os.path.join(self.state, 'second-name'))
        os.mkdir(os.path.join(self.state, 'sub'))
        with self.no_leaks(), self.writable() as state:
            fd, created = state.open_append('exclude')
            try:
                import fcntl
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                self.assertEqual((flags & os.O_ACCMODE, bool(flags & os.O_APPEND), created), (os.O_RDWR, True, False))
                self.assertEqual(os.read(fd, 100), b'# fixture\n')
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b'/rule/\n')               # appended whatever the offset
            finally:
                os.close(fd)
            fd, created = state.open_append('new-exclude')
            os.close(fd)
            self.assertTrue(created)
            with self.assertRaises(FileNotFoundError):
                state.open_append('missing', create=False)
            with self.assertRaises(OSError) as raised:
                state.open_append('link')
            self.assertEqual(raised.exception.errno, errno.ELOOP)
            reason(self, lambda: state.open_append('second-name'), 'links')
            with self.assertRaises(IsADirectoryError):
                state.open_append('sub')                # the system's own refusal of O_RDWR on a folder
            os.chmod(exclude, 0o664)
            reason(self, lambda: state.open_append('exclude'), 'access')
            with patch.object(ps, '_posix_unwritable', side_effect=ps.PrivateStorageError('refused', 'owner')):
                reason(self, lambda: state.open_append('refused'), 'owner')
        with open(exclude, 'rb') as kept:
            self.assertEqual(kept.read(), b'# fixture\n/rule/\n')
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, 'new-exclude')).st_mode) & 0o077, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.state, 'missing')))
        self.assertFalse(os.path.lexists(os.path.join(self.state, 'refused')), 'a refused new file is removed again')

    def test_a_name_swapped_after_the_check_is_left_alone(self):
        def swap(fd):
            os.rename(self.secret, self.secret + '-moved')
            with open(self.secret, 'wb') as out:
                out.write(b'someone else')
            return True
        with self.no_leaks(), self.writable() as state:
            self.assertEqual(state.remove('secret', swap), 'changed')
        with open(self.secret, 'rb') as kept:
            self.assertEqual(kept.read(), b'someone else')
        self.assertTrue(os.path.exists(self.secret + '-moved'))

    def test_child_stat_reads_the_name_itself(self):
        os.symlink(self.secret, os.path.join(self.state, 'link'))
        with self.no_leaks(), ps.open_folder(self.state) as state:
            self.assertTrue(os.path.samestat(state.child_stat('secret'), os.stat(self.secret)))
            self.assertTrue(stat.S_ISLNK(state.child_stat('link').st_mode))
            with self.assertRaises(FileNotFoundError):
                state.child_stat('absent')

    def test_read_only_and_unwritable(self):
        with self.no_leaks():
            fd = ps.open_no_links(self.secret)
            try:
                self.assertTrue(ps.read_only(fd))
                ps.unwritable(fd)
            finally:
                os.close(fd)
            for flags in (os.O_RDWR, os.O_WRONLY):
                fd = os.open(self.secret, flags)
                try:
                    self.assertFalse(ps.read_only(fd))
                finally:
                    os.close(fd)
            fd = ps.open_no_links(self.state, directory=True)
            try:
                ps.unwritable(fd)
                os.chmod(self.state, 0o720)
                reason(self, lambda: ps.unwritable(fd), 'access', 'folder')
                os.chmod(self.state, 0o755)
                ps.unwritable(fd)                               # readable by others is fine; only change is refused
                if os.getuid() != 0:
                    reason(self, lambda: ps.unwritable(fd, system_owned=True), 'owner', 'folder')
            finally:
                os.close(fd)
            os.chmod(self.secret, 0o602)
            fd = ps.open_no_links(self.secret)
            try:
                reason(self, lambda: ps.unwritable(fd), 'access')
            finally:
                os.close(fd)
            shell = os.path.realpath('/bin/sh')
            if os.stat(shell).st_uid == 0 and not os.stat(shell).st_mode & 0o022:
                fd = ps.open_no_links(shell)
                try:
                    ps.unwritable(fd, system_owned=True)
                finally:
                    os.close(fd)


@unittest.skipIf(os.name == 'nt', 'the simulation answers Windows calls with the POSIX implementation')
class SimulatedWindowsWiring(unittest.TestCase):
    """Code that changes a held folder through section (h) also runs under tests/windows_simulation.py, so the
    attachment modules can be checked on a Mac once they move onto it: no fcntl or os.getuid in their Windows
    branch."""

    def test_changes_through_a_held_folder_run_under_the_simulation(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from windows_simulation import simulated_windows
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(os.path.realpath(tmp), 'state')
            os.mkdir(state, 0o700)
            with simulated_windows() as calls:
                with self.assertRaises(AssertionError):
                    os.getuid()
                with ps.open_folder(state, writable=True) as folder:
                    fd = folder.create('new')
                    try:
                        self.assertFalse(ps.read_only(fd))
                        ps.unwritable(fd)
                        os.write(fd, b'x')
                        ps.sync_file(fd)
                    finally:
                        os.close(fd)
                    folder.mkdir('sub')
                    os.close(folder.open_or_create('fixture.lock'))
                    self.assertTrue(os.path.samestat(folder.child_stat('new'), os.stat(os.path.join(state, 'new'))))
                    self.assertEqual(folder.remove('new', lambda fd: os.read(fd, 10) == b'x'), 'removed')
                    folder.sync()
            self.assertEqual(calls, ['open_folder', 'read_only', 'unwritable', 'sync_file'])
            self.assertEqual(sorted(os.listdir(state)), ['fixture.lock', 'sub'])


def full_dir_record(name, last=False, pad=0):
    """One FILE_FULL_DIR_INFO record, 8-byte aligned, as GetFileInformationByHandleEx writes it."""
    import struct
    raw = name.encode('utf-16-le')
    size = 68 + len(raw)
    size += -size % 8 + pad
    head = struct.pack('<II6qIII', 0 if last else size, 7, 1, 2, 3, 4, 100, 104, 0x20, len(raw), 0)
    record = head + raw
    return record + b'\0' * (size - len(record))


class WindowsRecordParsing(unittest.TestCase):
    """Pure parts of the Windows walk that run on every system: directory records and NtCreateFile's structures."""

    def test_full_dir_info_records(self):
        raw = b''.join([full_dir_record('.'), full_dir_record('..'), full_dir_record('chat.jsonl', pad=8),
                        full_dir_record('Café 😀.jsonl', last=True)]) + b'\xff' * 40   # unused buffer after the last
        self.assertEqual(ps._full_dir_names(raw), ['chat.jsonl', 'Café 😀.jsonl'])
        self.assertEqual(ps._full_dir_names(full_dir_record('only', last=True)), ['only'])

    def test_records_running_past_the_buffer_are_refused(self):
        record = full_dir_record('chat.jsonl', last=True)
        for broken in (record[:60], record[:70], full_dir_record('chat.jsonl')):   # cut short, or a next that is not there
            with self.subTest(size=len(broken)), self.assertRaises(ps.PrivateStorageError):
                ps._full_dir_names(broken)

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, 'NtCreateFile layouts checked for 64-bit Windows')
    def test_nt_structures_match_64_bit_windows(self):
        UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK = ps._nt_structures()
        self.assertEqual((ctypes.sizeof(UNICODE_STRING), UNICODE_STRING.Buffer.offset), (16, 8))
        self.assertEqual(ctypes.sizeof(OBJECT_ATTRIBUTES), 48)
        self.assertEqual([getattr(OBJECT_ATTRIBUTES, f).offset for f in
                          ('Length', 'RootDirectory', 'ObjectName', 'Attributes', 'SecurityDescriptor', 'SecurityQualityOfService')],
                         [0, 8, 16, 24, 32, 40])
        self.assertEqual(ctypes.sizeof(IO_STATUS_BLOCK), 16)


class SimulatedWindowsWalk(unittest.TestCase):
    """The Windows walk's decisions with the Win32 and NT calls replaced by a model of a drive; runs on every system.

    The model is a tree of folder and file objects. A handle names an object, and NtCreateFile's relative open
    looks the child up in the folder object the parent handle names, as Windows does, so renaming or swapping a
    name in a folder changes nothing already open. Renames follow Windows' sharing rule: a rename needs DELETE
    access, which is refused while another handle that asked for data access does not share delete; a handle that
    asked only for READ_CONTROL or attributes holds nothing."""
    DATA_ACCESS = 0x1 | 0x2 | 0x4 | 0x20 | 0x10000      # read/list, write, append, execute/traverse, delete
    # Section (h) follows the same model: a create or open-or-create makes a node carrying the descriptor it was given
    # and a create fails on an existing name; any open fails while another open handle's sharing does not allow it;
    # deleting through a handle removes the name at once (POSIX semantics) or, the older way, marks it pending, which
    # refuses every open and create until the last handle on it closes.
    READS, WRITES = 0x1 | 0x20 | ps._GENERIC_READ, 0x2 | 0x4 | ps._GENERIC_WRITE   # checked against share read / write

    def node(self, kind, **children):
        return {'kind': kind, 'children': children}

    def setUp(self):
        n = self.node
        self.chat = n('file')
        self.root = n('folder', Users=n('folder', example=n('folder', **{'.claude': n('folder', **{'chat.jsonl': self.chat})})))
        self.drives = {'C:\\': self.root}
        self.public, self.verify_root = set(), {}
        self.handles, self.open_now, self.closed, self.fds, self.checked = {}, set(), [], [], []
        self.log = []          # (name, access, share, [held handles at that moment])
        self.during_open = None
        self.created, self.flushed, self.closed_fds, self.duplicated = [], [], [], []
        self.posix_delete, self.refuse_new = True, False

        def new_handle(node, name, access, share, parent=None):
            handle = 500 + len(self.handles)
            self.log.append((name, access, share, sorted(self.open_now)))
            self.handles[handle] = (node, name, access, share, parent)
            self.open_now.add(handle)
            return handle

        def conflicts(node, access, share):
            """Windows' sharing rule between this open and every handle already open on ``node``."""
            for handle in self.open_now:
                held, _, held_access, held_share = self.handles[handle][:4]
                if held is not node:
                    continue
                for asked, allowed in ((access, held_share), (held_access, share)):
                    if ((asked & self.READS and not allowed & ps._SHARE_READ)
                            or (asked & self.WRITES and not allowed & ps._SHARE_WRITE)
                            or (asked & ps._DELETE and not allowed & ps._SHARE_DELETE)):
                        return True
            return False

        def fake_open(path, access, share, disposition, flags, sa=None):
            self.assertEqual((disposition, flags, sa), (ps._OPEN_EXISTING, 0, None))
            node = self.drives.get(path)
            if node is None:
                error = OSError('missing'); error.winerror = 3
                raise error
            return new_handle(node, path, access, share)

        def fake_child(parent, name, access, share, directory, disposition=ps._NT_FILE_OPEN, options=0, descriptor=None,
                       information=None):
            self.assertIn(parent, self.open_now, 'opened relative to a closed handle')
            if self.during_open:
                self.during_open(name)
            folder = self.handles[parent][0]
            node = folder['children'].get(name)
            def fail(status):
                error = OSError('nt'); error.ntstatus = status
                raise error
            if disposition != ps._NT_FILE_OPEN:
                self.assertEqual(self.handles[parent][2] & ps._WRITE_HOLD, ps._WRITE_HOLD,
                                 'a create goes through a hold that asked for the change rights')
                self.assertEqual(descriptor, 'private-descriptor', 'a new name gets the private access list')
            if node is not None and node.get('pending'):
                fail(ps._NT_DELETE_PENDING)
            made = node is None
            if node is None:
                if disposition == ps._NT_FILE_OPEN:
                    fail(0xC0000034)
                node = self.node('folder' if directory else 'file')
                node['descriptor'] = descriptor
                folder['children'][name] = node
                self.created.append(name)
                if self.refuse_new:
                    self.public.add(id(node))
            # As real NtCreateFile does (seen on windows-latest): the wrong type is reported before a collision, so a
            # file created over a folder or junction gives STATUS_FILE_IS_A_DIRECTORY, a folder over a file
            # STATUS_NOT_A_DIRECTORY, and only a name of the asked-for type STATUS_OBJECT_NAME_COLLISION.
            is_folder = node['kind'] in ('folder', 'link-folder')
            if directory and not is_folder:
                fail(0xC0000103)
            if directory is False and is_folder:
                fail(0xC00000BA)
            if not made and disposition == ps._NT_FILE_CREATE:
                fail(ps._NT_COLLISION)
            if node['kind'] == 'busy':
                fail(ps._NT_SHARING_VIOLATION)
            if conflicts(node, access, share):
                fail(ps._NT_SHARING_VIOLATION)
            if information is not None:
                information.append(ps._FILE_CREATED if made else ps._FILE_OPENED)
            return new_handle(node, name, access, share, folder)

        def fake_close(handle):
            if handle is not None:
                self.assertIn(handle, self.open_now, 'closed twice or never opened')
                self.open_now.discard(handle)
                self.closed.append(handle)
                node = self.handles[handle][0]
                if node.get('pending') and not any(self.handles[h][0] is node for h in self.open_now):
                    folder, name = node['pending']
                    if folder['children'].get(name) is node:
                        del folder['children'][name]      # the last handle closed: the pending name goes

        def fake_set_delete(handle):
            node, name, access, _, folder = self.handles[handle]
            self.assertIn(handle, self.open_now)
            self.assertTrue(access & ps._DELETE, 'deleting through a handle needs DELETE access')
            if node['kind'] == 'readonly-file' and not self.posix_delete:
                raise ps.PrivateStorageError('refused', 'open')     # the older deletion cannot delete a read-only file
            if self.posix_delete:
                if folder['children'].get(name) is node:
                    del folder['children'][name]
                return 'posix'
            node['pending'] = (folder, name)
            return 'legacy'

        def fake_flush(handle):
            self.assertTrue(self.handles[handle][2] & ps._FILE_ADD_FILE, 'FlushFileBuffers needs write access')
            self.flushed.append(self.handles[handle][1])

        def fake_duplicate(handle, access=None):
            node, name, held_access, share, folder = self.handles[handle]
            self.duplicated.append(access)
            return new_handle(node, name, held_access if access is None else access, share, folder)

        def fake_fstat(handle):
            """As Windows' fstat: a reparse point gets the type of what it stands for, and only the attributes (here
            the model's facts) say it is one."""
            node = self.handles[handle][0]
            folder = node['kind'] in ('folder', 'link-folder')
            mode = (stat.S_IFDIR | 0o700) if folder else (stat.S_IFREG | 0o600)
            return os.stat_result((mode, id(node), 1, fake_facts(handle)[1], 0, 0, 0, 0, 0, 0))

        @contextmanager
        def fake_descriptor():
            yield 'private-descriptor'

        self.fake_child, self.new_handle = fake_child, new_handle

        def fake_facts(handle):
            kind = self.handles[handle][0]['kind']
            return {'folder': (ps.DIRECTORY, 1, 1), 'file': (0x20, 1, 1), 'two-names': (0x20, 2, 1),
                    'link-folder': (ps.DIRECTORY | ps.REPARSE_POINT, 1, 1), 'link-file': (ps.REPARSE_POINT | 0x20, 1, 1),
                    'device': (0x20, 1, 2), 'readonly-file': (ps._READONLY | 0x20, 1, 1)}[kind]

        def fake_security(handle, directory=False):
            node = self.handles[handle][0]
            self.checked.append(self.handles[handle][1])
            if id(node) in self.public:
                raise ps.PrivateStorageError('private storage unavailable', 'access')

        def fake_root(handle, root):
            problem = self.verify_root.get(root)
            if problem:
                raise ps.PrivateStorageError('private storage unavailable', 'unavailable', 'folder')

        def fake_list(handle):
            return sorted(self.handles[handle][0]['children'])

        def fake_fd(handle, flags=0):
            self.assertIn(handle, self.open_now)
            self.open_now.discard(handle)               # the descriptor owns the handle now
            self.fds.append(self.handles[handle][1])
            return 9000 + handle

        self.patches = [patch.object(ps, 'WIN', True), patch.object(ps, '_win_open', fake_open),
                        patch.object(ps, '_win_child', fake_child), patch.object(ps, '_win_root_verified', fake_root),
                        patch.object(ps, '_win_list', fake_list),
                        patch.object(ps, '_win_close', fake_close), patch.object(ps, '_win_facts', fake_facts),
                        patch.object(ps, 'windows_handle_security', fake_security), patch.object(ps, '_win_fd', fake_fd),
                        patch.object(ps, '_win_set_delete', fake_set_delete), patch.object(ps, '_win_flush', fake_flush),
                        patch.object(ps, '_win_duplicate', fake_duplicate), patch.object(ps, '_win_fstat', fake_fstat),
                        patch.object(ps, '_close_fd', self.closed_fds.append),
                        patch.object(ps, '_win_handle_of', lambda fd: fd - 9000),
                        patch.object(ps, '_win_access_mask', lambda handle: self.handles[handle][2]),
                        patch.object(ps, '_private_descriptor', fake_descriptor),
                        patch.object(ps, '_win_file_id', lambda handle: id(self.handles[handle][0]))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def rename_allowed(self, node):
        """Windows' sharing rule for a rename or delete of ``node`` (DELETE access) while handles are open on it."""
        for handle in self.open_now:
            held, _, access, share = self.handles[handle][:4]
            if held is node and access & self.DATA_ACCESS and not share & ps._SHARE_DELETE:
                return False
        return True

    def assert_nothing_open(self):
        self.assertEqual(self.open_now, set(), 'a handle was left open')

    def opened(self):
        return [entry[0] for entry in self.log]

    FILE_PATH = 'C:\\Users\\example\\.claude\\chat.jsonl'
    NAMES = ['C:\\', 'Users', 'example', '.claude']

    def folder_nodes(self):
        users = self.root['children']['Users']
        example = users['children']['example']
        return [self.root, users, example, example['children']['.claude']]

    def test_each_name_opens_relative_to_the_held_folder_above(self):
        fd = ps.open_no_links(self.FILE_PATH, single_link=True)
        self.assertEqual(fd - 9000, max(self.handles))
        self.assertEqual(self.opened(), self.NAMES + ['chat.jsonl'])
        no_delete = ps._SHARE_READ | ps._SHARE_WRITE
        for n, (name, access, share, held) in enumerate(self.log):
            self.assertEqual(len(held), n, 'every folder above is still held when a name is opened')
            if n < len(self.NAMES):
                self.assertEqual((access, share), (ps._HOLD_ACCESS, no_delete))
                self.assertTrue(access & (ps._FILE_LIST_DIRECTORY | ps._FILE_TRAVERSE),
                                'a hold must ask for data access, or Windows does not apply its sharing')
            else:
                self.assertEqual((access, share), (ps._GENERIC_READ, no_delete | ps._SHARE_DELETE))
        self.assertEqual(self.fds, ['chat.jsonl'])
        self.assertEqual(self.checked, [], 'no access-list check unless asked')
        self.assert_nothing_open()

    def test_held_folders_cannot_be_renamed_and_a_hold_without_data_access_would_not_hold(self):
        seen = []
        def try_renames(name):
            if name == 'chat.jsonl':
                seen.append([self.rename_allowed(node) for node in self.folder_nodes()])
        self.during_open = try_renames
        ps.open_no_links(self.FILE_PATH)
        self.assertEqual(seen, [[False, False, False, False]])
        # The rule the hold depends on: attribute-only access records no sharing.
        handle = 900
        self.handles[handle] = (self.root, 'C:\\', ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, ps._SHARE_READ | ps._SHARE_WRITE)
        self.open_now.add(handle)
        self.assertTrue(self.rename_allowed(self.root))
        self.open_now.discard(handle)

    def test_a_name_swapped_after_its_folder_was_opened_changes_nothing(self):
        users = self.root['children']['Users']
        original = users['children']['example']
        def swap(name):
            if name == 'example':           # Users is open; replace example in it by a junction to elsewhere
                users['children']['example'] = self.node('link-folder')
                users['children']['example-moved'] = original
            if name == '.claude':           # example is open; a new link at its old name is never looked at again
                self.assertEqual(users['children']['example']['kind'], 'link-folder')
        # The swap happens just before 'example' is looked up, so the walk meets the junction and refuses it ...
        self.during_open = swap
        reason(self, lambda: ps.open_no_links(self.FILE_PATH), 'link', 'folder')
        self.assert_nothing_open()
        # ... and once 'example' is open, renaming names in Users no longer reaches the walk at all.
        users['children'] = {'example': original}
        def late_swap(name):
            if name == '.claude':
                users['children'] = {'example': self.node('link-folder'), 'example-moved': original}
        self.during_open = late_swap
        ps.open_no_links(self.FILE_PATH)
        self.assertEqual(self.fds[-1], 'chat.jsonl')
        self.assert_nothing_open()

    def test_a_reparse_point_in_any_position_stops_the_walk_there(self):
        positions = [('Users', self.root), ('example', self.root['children']['Users']),
                     ('.claude', self.root['children']['Users']['children']['example']),
                     ('chat.jsonl', self.root['children']['Users']['children']['example']['children']['.claude'])]
        for n, (name, parent) in enumerate(positions, 1):
            with self.subTest(position=name):
                self.log.clear()
                original = parent['children'][name]
                parent['children'][name] = self.node('link-file' if name == 'chat.jsonl' else 'link-folder')
                try:
                    reason(self, lambda: ps.open_no_links(self.FILE_PATH), 'link', 'file' if name == 'chat.jsonl' else 'folder')
                    self.assertEqual(self.opened(), (self.NAMES + ['chat.jsonl'])[:n + 1], 'nothing opened beyond it')
                    self.assert_nothing_open()
                finally:
                    parent['children'][name] = original

    def test_wrong_types_second_names_missing_and_busy(self):
        claude = self.folder_nodes()[3]
        reason(self, lambda: ps.open_no_links('C:\\Users\\example\\.claude'), 'not_regular')
        reason(self, lambda: ps.open_folder(self.FILE_PATH), 'not_regular', 'folder')
        claude['children']['console.log'] = self.node('device')
        reason(self, lambda: ps.open_no_links('C:\\Users\\example\\.claude\\console.log'), 'not_regular')
        claude['children']['chat.jsonl'] = self.node('two-names')
        reason(self, lambda: ps.open_no_links(self.FILE_PATH, single_link=True), 'links')
        ps.open_no_links(self.FILE_PATH)                       # allowed unless asked
        with self.assertRaises(FileNotFoundError):
            ps.open_no_links('C:\\Users\\example\\absent\\chat.jsonl')
        with self.assertRaises(FileNotFoundError):
            ps.open_no_links('E:\\chat.jsonl')
        claude['children']['busy.jsonl'] = self.node('busy')
        reason(self, lambda: ps.open_no_links('C:\\Users\\example\\.claude\\busy.jsonl'), 'open')
        self.assert_nothing_open()

    def test_a_drive_that_is_not_a_local_volume_root_is_refused(self):
        self.verify_root['C:\\'] = 'subst or network'
        reason(self, lambda: ps.open_no_links(self.FILE_PATH), 'unavailable', 'folder')
        self.assertEqual(self.opened(), ['C:\\'])
        self.assert_nothing_open()

    def test_access_lists_are_checked_on_the_opened_handles_when_asked(self):
        ps.open_no_links(self.FILE_PATH, private=True)
        self.assertEqual(self.checked, ['chat.jsonl'])
        self.checked.clear()
        ps.open_no_links('C:\\Users\\example\\.claude', directory=True, private=True)
        self.assertEqual(self.checked, ['.claude'], 'only the folder asked for, not its parents')
        self.public.add(id(self.chat))
        reason(self, lambda: ps.open_no_links(self.FILE_PATH, private=True), 'access')
        self.public.add(id(self.folder_nodes()[2]))
        reason(self, lambda: ps.open_folder('C:\\Users\\example', private=True), 'access', 'folder')
        self.assert_nothing_open()

    def test_a_folder_descriptor_keeps_only_its_own_handle(self):
        ps.open_no_links('C:\\Users\\example', directory=True)
        self.assertEqual(self.fds, ['example'])
        self.assert_nothing_open()

    def test_held_folders_list_through_the_handle_and_open_children_through_the_hold(self):
        claude_node = self.folder_nodes()[3]
        claude_node['children']['con.corp'] = self.node('file')
        with ps.open_folder('C:\\Users\\example') as home:
            reason(self, home.names, 'unavailable', 'folder')          # not opened for listing
            with home.folder('.claude', listing=True) as claude:
                self.assertEqual(claude.names(), ['chat.jsonl', 'con.corp'])
                claude.open('chat.jsonl', single_link=True)
                self.assertEqual(len(self.log[-1][3]), 4, 'root, Users, example and .claude held')
                claude.open('con.corp')                                # device-like names are ordinary names here
                with self.assertRaises(FileNotFoundError):
                    claude.open('NUL')                                 # looked up as a name, never a device
            accesses = {entry[0]: entry[1] for entry in self.log}
            self.assertEqual(accesses['.claude'], ps._LIST_ACCESS, 'only the listed folder asks to list')
            for name in ('C:\\', 'Users', 'example'):
                self.assertEqual(accesses[name], ps._HOLD_ACCESS)
                self.assertFalse(accesses[name] & ps._FILE_LIST_DIRECTORY)
            for name in ('..', 'a\\b', 'chat.jsonl:secret', 'trailing.', 'trailing '):
                with self.subTest(name=name):
                    reason(self, lambda: home.open(name), 'unavailable')
        reason(self, lambda: claude.open('chat.jsonl'), 'changed', 'folder')   # its parent's hold is gone
        self.assert_nothing_open()

    def test_paths_outside_plain_drive_paths_are_refused_before_any_open(self):
        for path in ('\\\\server\\share\\chat.jsonl', '\\\\?\\C:\\Users\\example\\chat.jsonl', '\\\\.\\C:\\chat.jsonl',
                     '//server/share/chat.jsonl', 'C:\\Users\\example\\chat.jsonl:stream',
                     'C:\\Users\\example.\\chat.jsonl', 'C:\\Users\\example \\chat.jsonl', '1:\\chat.jsonl',
                     'C:\\Users\\example\\NUL', 'C:\\Users\\example\\con.txt', 'C:\\Users\\example\\chat.jsonl.'):
            with self.subTest(path=path):
                with self.assertRaises(ps.PrivateStorageError) as raised:
                    ps.open_folder(path)
                self.assertEqual(raised.exception.reason, 'unavailable')
        for path in ('C:relative\\chat.jsonl', 'C:\\Users\\..\\example', 'C:\\Users\\.\\example', 'C:\\Users\\\\example'):
            with self.subTest(path=path), self.assertRaises(ps.PrivateStorageError):
                ps._win_parts(path)      # open_folder first makes paths absolute and resolves "..", as Windows does
        self.assertEqual(self.log, [])
        self.assertEqual(ps._win_parts('d:/Profiles/example/'), ('d:\\', ['Profiles', 'example']))
        self.assertEqual(ps._win_parts('D:\\'), ('D:\\', []))

    def test_a_failed_check_closes_the_handle_it_was_made_on(self):
        with patch.object(ps, '_win_facts', side_effect=ps.PrivateStorageError('private storage unavailable')):
            with self.assertRaises(ps.PrivateStorageError):
                ps.open_no_links(self.FILE_PATH)
        self.assert_nothing_open()

    # Section (h): changing a held folder.

    CLAUDE = 'C:\\Users\\example\\.claude'

    def claude(self):
        return self.folder_nodes()[3]

    def granted(self, handle):
        """The access Windows grants for what was asked: generic read and write map to the file rights."""
        access = self.handles[handle][2]
        mask = access & ~(ps._GENERIC_READ | ps._GENERIC_WRITE)
        if access & ps._GENERIC_READ:
            mask |= 0x120089                    # FILE_GENERIC_READ
        if access & ps._GENERIC_WRITE:
            mask |= 0x120116                    # FILE_GENERIC_WRITE
        return mask

    def expecting(self, node):
        """An ``expect`` for remove that recognises ``node`` through the descriptor it is given."""
        seen = []

        def expect(fd):
            seen.append(fd)
            self.assertIn(fd - 9000, self.handles)
            return self.handles[fd - 9000][0] is node
        return expect, seen

    def test_a_writable_hold_asks_for_the_change_rights_on_that_folder_only(self):
        with ps.open_folder(self.CLAUDE, writable=True):
            access = {entry[0]: entry[1:3] for entry in self.log}
            self.assertEqual(access['.claude'], (ps._HOLD_ACCESS | ps._WRITE_HOLD, ps._SHARE_READ | ps._SHARE_WRITE))
            for name in ('C:\\', 'Users', 'example'):
                self.assertEqual(access[name][0], ps._HOLD_ACCESS)
        self.log.clear()
        with ps.open_folder('C:\\Users\\example') as home, home.folder('.claude', writable=True):
            self.assertEqual(self.log[-1][:2], ('.claude', ps._HOLD_ACCESS | ps._WRITE_HOLD))
        with ps.open_folder(self.CLAUDE) as held:
            before = len(self.log)
            for change in (lambda: held.create('new.json'), lambda: held.open_or_create('new.lock'),
                           lambda: held.mkdir('sub'), lambda: held.remove('chat.jsonl', lambda fd: True), held.sync):
                reason(self, change, 'unavailable', 'folder')
            self.assertEqual(len(self.log), before, 'a hold that did not ask refuses before opening anything')
        self.assert_nothing_open()

    def test_create_makes_a_private_file_and_never_takes_an_existing_name(self):
        claude = self.claude()
        claude['children']['link.json'] = self.node('link-file')
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            fd = folder.create('new.json')
            self.assertEqual(self.log[-1][:3], ('new.json', ps._GENERIC_READ | ps._GENERIC_WRITE | ps._DELETE,
                                                ps._SHARE_READ | ps._SHARE_WRITE))
            self.assertEqual(claude['children']['new.json']['descriptor'], 'private-descriptor')
            self.assertEqual(self.checked, ['new.json'], 'the access list is checked on the new handle')
            self.assertEqual((fd - 9000, self.fds[-1]), (max(self.handles), 'new.json'))
            for name in ('chat.jsonl', 'new.json', 'link.json'):       # a link counts as an existing name
                with self.subTest(name=name), self.assertRaises(FileExistsError):
                    folder.create(name)
            claude['children'].update({'sub': self.node('folder'), 'junction': self.node('link-folder')})
            for name in ('sub', 'junction'):          # reported by Windows as STATUS_FILE_IS_A_DIRECTORY
                with self.subTest(name=name), self.assertRaises(FileExistsError) as raised:
                    folder.create(name)
                self.assertEqual(raised.exception.ntstatus, 0xC00000BA)
                self.assertEqual(claude['children'][name]['kind'], 'folder' if name == 'sub' else 'link-folder')
            for name in ('', '..', 'a\\b', 'chat.jsonl:stream', 'trailing.'):
                with self.subTest(name=name):
                    reason(self, lambda: folder.create(name), 'unavailable')
        self.assertEqual(self.created, ['new.json'])
        self.assert_nothing_open()

    def test_a_refused_new_file_or_folder_is_deleted_through_its_own_handle(self):
        self.refuse_new = True
        for posix in (True, False):
            self.posix_delete = posix
            with self.subTest(posix=posix), ps.open_folder(self.CLAUDE, writable=True) as folder:
                reason(self, lambda: folder.create('new.json'), 'access')
                reason(self, lambda: folder.mkdir('sub'), 'access', 'folder')
                self.assertEqual(sorted(self.claude()['children']), ['chat.jsonl'])
        self.assert_nothing_open()

    def test_open_or_create_checks_what_is_there_and_makes_what_is_not(self):
        claude = self.claude()
        public = self.node('file')
        claude['children'].update({'link.lock': self.node('link-file'), 'sub': self.node('folder'),
                                   'two.lock': self.node('two-names'), 'public.lock': public})
        self.public.add(id(public))
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            folder.open_or_create('chat.jsonl')
            self.assertEqual(self.log[-1][:3], ('chat.jsonl', ps._GENERIC_READ | ps._GENERIC_WRITE,
                                                ps._SHARE_READ | ps._SHARE_WRITE))
            self.assertEqual((self.created, self.checked), ([], ['chat.jsonl']))
            folder.open_or_create('new.lock')
            self.assertEqual(self.created, ['new.lock'])
            self.assertEqual(claude['children']['new.lock']['descriptor'], 'private-descriptor')
            reason(self, lambda: folder.open_or_create('link.lock'), 'link')
            reason(self, lambda: folder.open_or_create('sub'), 'not_regular')
            reason(self, lambda: folder.open_or_create('two.lock'), 'links')
            reason(self, lambda: folder.open_or_create('public.lock'), 'access')
        self.assert_nothing_open()

    def test_mkdir_makes_a_private_folder_and_lets_it_go(self):
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            self.assertIsNone(folder.mkdir('sub'))
            self.assertEqual(self.log[-1][:3], ('sub', ps._HOLD_ACCESS | ps._DELETE, ps._SHARE_READ | ps._SHARE_WRITE))
            node = self.claude()['children']['sub']
            self.assertEqual((node['kind'], node['descriptor'], self.checked), ('folder', 'private-descriptor', ['sub']))
            self.assertEqual(len(self.open_now), 4, 'the new folder is closed; only the walk is held')
            self.claude()['children'].update({'link.json': self.node('link-file'), 'junction': self.node('link-folder')})
            for name, status in (('sub', ps._NT_COLLISION), ('junction', ps._NT_COLLISION),
                                 ('chat.jsonl', 0xC0000103), ('link.json', 0xC0000103)):   # STATUS_NOT_A_DIRECTORY
                with self.subTest(name=name), self.assertRaises(FileExistsError) as raised:
                    folder.mkdir(name)
                self.assertEqual(raised.exception.ntstatus, status)
            with folder.folder('sub', private=True, writable=True) as sub:
                sub.create('inner.json')
        self.assertIn('inner.json', node['children'])
        self.assert_nothing_open()

    def test_remove_deletes_through_the_handle_it_checked(self):
        expect, seen = self.expecting(self.chat)
        shown_read_only = []

        def recognise(fd):
            shown_read_only.append(ps.read_only(fd))
            return expect(fd)
        with ps.open_folder(self.CLAUDE, writable=True) as folder, patch.object(ps, '_win_access_mask', self.granted):
            self.assertEqual(folder.remove('chat.jsonl', recognise), 'removed')
            self.assertEqual((self.duplicated, shown_read_only), ([ps._FILE_GENERIC_READ], [True]),
                             'expect is shown a duplicate that can only read')
            self.assertNotIn('chat.jsonl', self.claude()['children'])
            opened = [entry for entry in self.log if entry[0] == 'chat.jsonl'][0]
            self.assertEqual(opened[1:3], (ps._GENERIC_READ | ps._DELETE, ps._SHARE_READ), 'no writer while it is checked')
            self.assertEqual(self.closed_fds, seen, 'the descriptor expect was given is closed')
            self.assertEqual(folder.remove('chat.jsonl', expect), 'absent')
        self.assert_nothing_open()

    def test_remove_leaves_anything_it_does_not_recognise(self):
        claude = self.claude()
        claude['children'].update({'other.json': self.node('file'), 'link.json': self.node('link-file'),
                                   'sub': self.node('folder')})
        expect, seen = self.expecting(self.chat)
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            self.assertEqual(folder.remove('other.json', expect), 'changed')
            self.assertEqual(len(seen), 1)
            for name in ('link.json', 'sub'):
                self.assertEqual(folder.remove(name, expect), 'changed')
            self.assertEqual(len(seen), 1, 'expect is asked only about a regular file')
        self.assertEqual(sorted(claude['children']), ['chat.jsonl', 'link.json', 'other.json', 'sub'])
        self.assert_nothing_open()

    def test_remove_is_busy_while_another_handle_does_not_share_delete_or_writes(self):
        claude = self.claude()
        expect, seen = self.expecting(self.chat)
        for access, share in ((ps._GENERIC_READ, ps._SHARE_READ | ps._SHARE_WRITE),            # the C runtime's open
                              (ps._GENERIC_WRITE, ps._SHARE_READ | ps._SHARE_WRITE | ps._SHARE_DELETE)):  # a writer
            other = self.new_handle(self.chat, 'other program', access, share)
            with self.subTest(access=hex(access)), ps.open_folder(self.CLAUDE, writable=True) as folder:
                self.assertEqual(folder.remove('chat.jsonl', expect), 'busy')
            ps._win_close(other)
        self.assertEqual(seen, [])
        claude['children']['busy.json'] = self.node('busy')
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            self.assertEqual(folder.remove('busy.json', expect), 'busy')
            self.assertEqual(folder.remove('chat.jsonl', expect), 'removed')
        self.assert_nothing_open()

    def test_the_older_deletion_keeps_the_name_until_the_last_handle_closes(self):
        self.posix_delete = False
        claude = self.claude()
        reader = self.new_handle(self.chat, 'reader', ps._GENERIC_READ, ps._SHARE_READ | ps._SHARE_WRITE | ps._SHARE_DELETE)
        expect, _ = self.expecting(self.chat)
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            self.assertEqual(folder.remove('chat.jsonl', expect), 'busy', 'deleted, but the name is still there')
            self.assertIs(claude['children']['chat.jsonl'], self.chat)
            self.assertEqual(folder.remove('chat.jsonl', expect), 'busy')
            with self.assertRaises(FileExistsError):
                folder.create('chat.jsonl')
            with self.assertRaises(FileNotFoundError):
                folder.open('chat.jsonl')
            with self.assertRaises(FileNotFoundError):
                folder.child_stat('chat.jsonl')
            ps._win_close(reader)
            self.assertNotIn('chat.jsonl', claude['children'])
            self.assertEqual(folder.remove('chat.jsonl', expect), 'absent')
            other = self.node('file')
            claude['children']['other.json'] = other
            self.assertEqual(folder.remove('other.json', self.expecting(other)[0]), 'removed',
                             'with no other handle the older deletion finishes when its own handle closes')
            self.assertNotIn('other.json', claude['children'])
        self.assert_nothing_open()

    def test_a_read_only_file_goes_with_posix_deletion_and_is_left_by_the_older_one(self):
        claude = self.claude()
        for posix, result in ((True, 'removed'), (False, 'changed')):
            self.posix_delete = posix
            marked = self.node('readonly-file')
            claude['children']['marked.json'] = marked
            with self.subTest(posix=posix), ps.open_folder(self.CLAUDE, writable=True) as folder:
                self.assertEqual(folder.remove('marked.json', self.expecting(marked)[0]), result)
                self.assertEqual('marked.json' in claude['children'], not posix)
        self.assert_nothing_open()

    def test_open_or_create_removes_a_new_file_it_refused_and_keeps_an_old_one(self):
        self.refuse_new = True
        claude = self.claude()
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            reason(self, lambda: folder.open_or_create('new.lock'), 'access')
            self.assertNotIn('new.lock', claude['children'])
            reopened = [entry for entry in self.log if entry[0] == 'new.lock']
            self.assertEqual([entry[1] for entry in reopened],
                             [ps._GENERIC_READ | ps._GENERIC_WRITE, ps._DELETE | ps._FILE_READ_ATTRIBUTES],
                             'the open has no DELETE access; the new file is removed by name after it closes')
            self.public.add(id(self.chat))
            reason(self, lambda: folder.open_or_create('chat.jsonl'), 'access')
            self.assertIs(claude['children']['chat.jsonl'], self.chat, 'an existing file is never removed')
            swapped = self.node('file')

            def swap_after_check(handle, directory=False):
                node = self.handles[handle][0]
                if node is not self.chat:                         # the new file: someone replaces it before the undo
                    claude['children']['other.lock'] = swapped
                raise ps.PrivateStorageError('private storage unavailable', 'access')
            with patch.object(ps, 'windows_handle_security', swap_after_check):
                reason(self, lambda: folder.open_or_create('other.lock'), 'access')
            self.assertIs(claude['children']['other.lock'], swapped, 'a name that changed file is left alone')
            self.chat['pending'] = (claude, 'chat.jsonl')
            reason(self, lambda: folder.open_or_create('chat.jsonl'), 'changed')
        self.assert_nothing_open()

    def test_open_append_opens_for_appending_and_applies_the_unwritable_rule(self):
        claude = self.claude()
        claude['children'].update({'link': self.node('link-file'), 'two': self.node('two-names'), 'sub': self.node('folder')})
        checked = []

        def collector(handle, system_owned=False):
            checked.append((self.handles[handle][1], system_owned))
            if id(self.handles[handle][0]) in self.public:
                raise ps.PrivateStorageError('storage others can change', 'access')
        everyone = ps._SHARE_READ | ps._SHARE_WRITE | ps._SHARE_DELETE
        with patch.object(ps, 'windows_handle_unwritable', collector), \
                ps.open_folder(self.CLAUDE, writable=True) as folder:
            fd, created = folder.open_append('chat.jsonl')
            self.assertEqual((fd - 9000, created), (max(self.handles), False))
            self.assertEqual(self.log[-1][:3], ('chat.jsonl', ps._APPEND_ACCESS, everyone))
            self.assertFalse(ps._APPEND_ACCESS & 0x2, 'append-only data access: no FILE_WRITE_DATA')
            fd, created = folder.open_append('exclude')
            self.assertTrue(created)
            self.assertEqual(claude['children']['exclude']['descriptor'], 'private-descriptor')
            with self.assertRaises(FileNotFoundError):
                folder.open_append('missing', create=False)
            reason(self, lambda: folder.open_append('link'), 'link')
            reason(self, lambda: folder.open_append('two'), 'links')
            reason(self, lambda: folder.open_append('sub'), 'not_regular')
            self.assertEqual(checked, [('chat.jsonl', False), ('exclude', False)], 'checked as unwritable, not private')
            self.assertEqual(self.checked, [], 'the private rule is not applied')
            lookups = []

            def junction_appears(name):               # missing when opened, a junction by the time it is created
                if name == 'raced':
                    lookups.append(name)
                    if len(lookups) == 2:
                        claude['children']['raced'] = self.node('link-folder')
            self.during_open = junction_appears
            with self.assertRaises(FileExistsError):
                folder.open_append('raced')
            self.during_open = None
            self.assertEqual(claude['children']['raced']['kind'], 'link-folder')
            self.public.add(id(self.chat))
            reason(self, lambda: folder.open_append('chat.jsonl'), 'access')
            self.assertIn('chat.jsonl', claude['children'])
            self.refuse_new = True
            reason(self, lambda: folder.open_append('refused'), 'access')
            self.assertNotIn('refused', claude['children'], 'a refused new file is removed again')
        with ps.open_folder(self.CLAUDE) as held:
            reason(self, lambda: held.open_append('chat.jsonl'), 'unavailable', 'folder')
        self.assertNotIn('missing', claude['children'])
        self.assert_nothing_open()

    def test_child_stat_reads_the_name_itself_with_attribute_access_only(self):
        claude = self.claude()
        claude['children'].update({'link.json': self.node('link-file'), 'sub': self.node('folder')})
        claude['children']['junction'] = self.node('link-folder')
        with ps.open_folder(self.CLAUDE) as folder:
            info = folder.child_stat('chat.jsonl')
            self.assertEqual((stat.S_ISREG(info.st_mode), info.st_ino), (True, id(self.chat)))
            self.assertEqual(self.log[-1][1:3], (ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, 7))
            for name in ('link.json', 'junction'):     # fstat says file or folder; the reparse attribute makes it a link
                with self.subTest(name=name):
                    info = folder.child_stat(name)
                    self.assertTrue(stat.S_ISLNK(info.st_mode), 'a link is reported as itself')
                    self.assertEqual(info.st_ino, id(claude['children'][name]))
            self.assertTrue(stat.S_ISDIR(folder.child_stat('sub').st_mode))
            with self.assertRaises(FileNotFoundError):
                folder.child_stat('absent')
            reason(self, lambda: folder.child_stat('a\\b'), 'unavailable')
        self.assert_nothing_open()

    def test_sync_flushes_a_writable_hold_only(self):
        with ps.open_folder(self.CLAUDE, writable=True) as folder:
            folder.sync()
        with ps.open_folder(self.CLAUDE) as folder:
            reason(self, folder.sync, 'unavailable', 'folder')
        self.assertEqual(self.flushed, ['.claude'])
        self.assert_nothing_open()

    def test_read_only_reads_the_access_the_handle_was_granted(self):
        with patch.object(ps, '_win_access_mask', self.granted):
            self.assertTrue(ps.read_only(ps.open_no_links(self.FILE_PATH)))
            with ps.open_folder(self.CLAUDE, writable=True) as folder:
                self.assertFalse(ps.read_only(folder.create('new.json')))
                self.assertFalse(ps.read_only(folder.open_or_create('new.lock')))
            handles = [self.new_handle(self.chat, 'attributes', ps._FILE_READ_ATTRIBUTES, 7)]
            self.assertFalse(ps.read_only(handles[0] + 9000), 'nothing to read')
            for extra in (0x2, 0x4, 0x10, 0x100, ps._DELETE, 0x40000, 0x80000):
                handles.append(self.new_handle(self.chat, 'reader', ps._GENERIC_READ | extra, 7))
                with self.subTest(extra=hex(extra)):
                    self.assertFalse(ps.read_only(handles[-1] + 9000))
            for handle in handles:
                ps._win_close(handle)
        self.assert_nothing_open()

    def test_unwritable_checks_the_opened_handle_and_names_what_was_refused(self):
        calls = []

        def collector(handle, system_owned=False):
            calls.append((self.handles[handle][1], system_owned))
            if id(self.handles[handle][0]) in self.public:
                raise ps.PrivateStorageError('storage others can change', 'access')
        with patch.object(ps, 'windows_handle_unwritable', collector):
            ps.unwritable(ps.open_no_links(self.CLAUDE, directory=True), system_owned=True)
            ps.unwritable(ps.open_no_links(self.FILE_PATH))
            self.public.update((id(self.chat), id(self.claude())))
            reason(self, lambda: ps.unwritable(ps.open_no_links(self.CLAUDE, directory=True)), 'access', 'folder')
            reason(self, lambda: ps.unwritable(ps.open_no_links(self.FILE_PATH)), 'access', 'file')
        self.assertEqual(calls, [('.claude', True), ('chat.jsonl', False), ('.claude', False), ('chat.jsonl', False)])
        self.assert_nothing_open()


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
            subprocess.run('icacls "%s" /setowner "%%USERDOMAIN%%\\%%USERNAME%%"' % plain, shell=True, check=True, capture_output=True, timeout=30)
            reason(self, lambda: ps.open_private_file(plain), 'access')
        subprocess.run('icacls "%s" /inheritance:r /grant:r "%%USERDOMAIN%%\\%%USERNAME%%:F"' % plain, shell=True, check=True, capture_output=True, timeout=30)
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


@unittest.skipUnless(os.name == 'nt', 'requires real Windows reparse points and sharing rules')
class WindowsWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.top = os.path.join(self.base, 'top')
        self.middle = os.path.join(self.top, 'middle')
        os.makedirs(self.middle)
        self.file = os.path.join(self.middle, 'notes.jsonl')
        with open(self.file, 'wb') as out:
            out.write(b'fixture\n')

    def tearDown(self):
        self.tmp.cleanup()     # fails if the walk left anything open

    def read(self, fd):
        try:
            return os.read(fd, 100)
        finally:
            os.close(fd)

    def test_available_and_reads_through_the_walk(self):
        self.assertTrue(ps.folder_walk_available())
        self.assertEqual(self.read(ps.open_no_links(self.file, single_link=True)), b'fixture\n')
        fd = ps.open_no_links(self.middle, directory=True)
        try:
            self.assertTrue(os.path.samestat(os.fstat(fd), os.stat(self.middle)))
        finally:
            os.close(fd)
        with ps.open_folder(self.top) as top, top.folder('middle', listing=True) as middle:
            self.assertEqual(middle.names(), ['notes.jsonl'])
            self.assertEqual(self.read(middle.open('notes.jsonl')), b'fixture\n')
            self.assertTrue(os.path.samestat(middle.stat(), os.stat(self.middle)))

    def test_a_junction_in_any_folder_position_is_refused(self):
        for link, target, path in ((os.path.join(self.base, 'top-junction'), self.top,
                                    os.path.join(self.base, 'top-junction', 'middle', 'notes.jsonl')),
                                   (os.path.join(self.top, 'middle-junction'), self.middle,
                                    os.path.join(self.top, 'middle-junction', 'notes.jsonl'))):
            junction(link, target)
            with self.subTest(link=os.path.basename(link)):
                reason(self, lambda: os.close(ps.open_no_links(path)), 'link', 'folder')
                reason(self, lambda: ps.open_folder(link).close(), 'link', 'folder')

    def test_a_symbolic_link_to_the_file_is_refused(self):
        link = os.path.join(self.middle, 'link.jsonl')
        try:
            os.symlink(self.file, link)
        except OSError as e:
            self.skipTest('creating a file symbolic link needs Developer Mode or the symlink privilege (%s)' % type(e).__name__)
        reason(self, lambda: os.close(ps.open_no_links(link)), 'link')

    def test_held_folders_cannot_be_renamed_or_removed(self):
        with ps.open_folder(self.middle):
            with self.assertRaises(OSError):
                os.rename(self.top, self.top + '-moved')
            with self.assertRaises(OSError):
                os.rename(self.middle, self.middle + '-moved')
        os.rename(self.top, self.top + '-moved')                 # released once closed

    def test_paths_longer_than_260_characters_open_through_the_walk(self):
        import shutil
        deep = os.path.join(self.base, *(['long-folder-name-for-the-walk'] * 10))
        self.assertGreater(len(deep), 300)
        os.makedirs('\\\\?\\' + deep)
        try:
            with open('\\\\?\\' + os.path.join(deep, 'notes.jsonl'), 'wb') as out:
                out.write(b'deep\n')
            self.assertEqual(self.read(ps.open_no_links(os.path.join(deep, 'notes.jsonl'))), b'deep\n')
        finally:
            shutil.rmtree('\\\\?\\' + os.path.join(self.base, 'long-folder-name-for-the-walk'))

    def test_a_subst_drive_standing_for_a_folder_is_refused(self):
        import string
        free = [letter for letter in reversed(string.ascii_uppercase[3:]) if not os.path.exists(letter + ':\\')]
        if not free:
            self.skipTest('no free drive letter')
        drive = free[0] + ':'
        subprocess.run(['subst', drive, self.top], check=True, capture_output=True, timeout=30)
        try:
            self.assertTrue(os.path.exists(drive + '\\middle\\notes.jsonl'))
            reason(self, lambda: os.close(ps.open_no_links(drive + '\\middle\\notes.jsonl')), 'unavailable', 'folder')
        finally:
            subprocess.run(['subst', drive, '/d'], capture_output=True, timeout=30)

    def test_device_names_below_the_root_are_only_names(self):
        with ps.open_folder(self.middle, listing=True) as middle:
            self.assertEqual(middle.names(), ['notes.jsonl'])
            for name in ('NUL', 'con.txt', 'COM1.jsonl'):
                with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                    middle.open(name)          # Win32 would open a device for some of these; a relative open does not

    def test_second_name_and_privacy(self):
        os.link(self.file, os.path.join(self.middle, 'second-name'))
        reason(self, lambda: os.close(ps.open_no_links(self.file, single_link=True)), 'links')
        private = os.path.join(self.base, 'private')
        ps.make_private_folder(private)
        secret = os.path.join(private, 'secret')
        os.close(ps.create_private_file(secret))
        os.close(ps.open_no_links(secret, single_link=True, private=True))
        os.close(ps.open_no_links(private, directory=True, private=True))
        icacls(secret, '/grant', EVERYONE_READ)
        reason(self, lambda: os.close(ps.open_no_links(secret, private=True)), 'access')

    def test_a_held_folder_or_an_open_file_inside_blocks_renaming_the_folders_above(self):
        """NTFS refuses to rename a folder while anything below it is open, whatever that open shares. Attachment
        storage relies on this to hold only its own folders for its lifetime, not every folder up to the drive."""
        fd = ps.open_no_links(self.middle, directory=True)       # only the middle folder's own handle is kept
        try:
            with self.assertRaises(OSError, msg='a held folder must pin the folder above it'):
                os.rename(self.top, self.top + '-moved')
        finally:
            os.close(fd)
        reader = ps.open_no_links(self.file)                      # shares read, write and delete
        try:
            with self.assertRaises(OSError, msg='an open file must pin its folder'):
                os.rename(self.middle, self.middle + '-moved')
            with self.assertRaises(OSError, msg='an open file must pin every folder above it'):
                os.rename(self.top, self.top + '-moved')
        finally:
            os.close(reader)
        os.rename(self.top, self.top + '-moved')                  # released once nothing below is open


@unittest.skipUnless(os.name == 'nt', 'requires real Windows handles, sharing and deletion')
class WindowsChanges(unittest.TestCase):
    """Section (h) on a real NTFS volume. Each test asserts the behaviour the attachment code will rely on, so a CI run
    answers the open questions: relative creates through a writable hold, flushing a folder, POSIX deletion and its
    fallback, and the sharing that makes a removal 'busy'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.tmp.name)
        self.state = os.path.join(self.base, 'state')
        ps.make_private_folder(self.state)
        self.path = os.path.join(self.state, 'secret')
        fd = ps.create_private_file(self.path)
        os.write(fd, b'fixture value\n')
        os.close(fd)

    def tearDown(self):
        self.tmp.cleanup()     # fails if anything was left open

    def writable(self):
        return ps.open_folder(self.state, writable=True)

    def until_settled(self, call, seconds=3):
        """Repeat ``call`` while it is 'busy': antivirus may briefly hold a new file open on a CI machine."""
        import time
        deadline = time.monotonic() + seconds
        result = call()
        while result == 'busy' and time.monotonic() < deadline:
            time.sleep(0.05)
            result = call()
        return result

    def test_create_mkdir_and_open_or_create_through_a_writable_hold(self):
        """A writable hold asks only for FILE_ADD_FILE, so this also proves that making a folder through it needs no
        FILE_ADD_SUBDIRECTORY on the handle."""
        self.assertEqual(ps._WRITE_HOLD, ps._FILE_ADD_FILE)
        new, sub, lock = (os.path.join(self.state, name) for name in ('new', 'sub', 'fixture.lock'))
        with self.writable() as state:
            fd = state.create('new')
            try:
                self.assertEqual(os.write(fd, b'abc'), 3)
                self.assertFalse(ps.read_only(fd))
            finally:
                os.close(fd)
            for name in ('new', 'secret'):
                with self.subTest(name=name), self.assertRaises(FileExistsError):
                    state.create(name)
            state.mkdir('sub')
            with self.assertRaises(FileExistsError):
                state.mkdir('sub')
            with state.folder('sub', private=True, writable=True) as inner:
                os.close(inner.create('inner'))
            first, second = state.open_or_create('fixture.lock'), state.open_or_create('fixture.lock')
            try:
                self.assertTrue(os.path.samestat(os.fstat(first), os.fstat(second)))
            finally:
                os.close(first)
                os.close(second)
        ps.windows_security(new)                       # protected, this user and SYSTEM only
        ps.check_private(new, single_link=True)
        ps.windows_security(sub, True)
        ps.check_private(os.path.join(sub, 'inner'), single_link=True)
        ps.check_private(lock, single_link=True)
        with open(new, 'rb') as made:
            self.assertEqual(made.read(), b'abc')

    def test_open_or_create_and_create_refuse_what_is_already_there(self):
        plain = os.path.join(self.state, 'plain')
        with open(plain, 'wb') as out:
            out.write(b'x')                            # follows the folder's list: not protected
        junction(os.path.join(self.state, 'junction'), self.base)
        with self.writable() as state:
            with self.assertRaises(ps.PrivateStorageError) as raised:
                state.open_or_create('plain')
            self.assertIn(raised.exception.reason, ('access', 'owner'))
            with self.assertRaises(ps.PrivateStorageError) as raised:
                state.open_or_create('junction')
            self.assertIn(raised.exception.reason, ('link', 'not_regular'))
            for change in (state.create, state.mkdir):
                with self.assertRaises(FileExistsError):
                    change('junction')
        self.assertEqual(sorted(os.listdir(self.base)), ['state'], 'nothing was made through the junction')

    def test_whether_a_traversal_only_hold_could_create_and_flush(self):
        """The open question, answered in the log. Motet asks for FILE_ADD_FILE on every hold it changes through (for
        the flush), so it does not depend on the answer; anything other than success or access denied fails here."""
        with ps.open_folder(self.state) as held:       # traversal only, as every hold was before section (h)
            with ps._private_descriptor() as descriptor:
                try:
                    handle = ps._win_child(held._handles[-1], 'probe', ps._GENERIC_READ | ps._GENERIC_WRITE | ps._DELETE,
                                           ps._SHARE_READ, False, ps._NT_FILE_CREATE, descriptor=descriptor)
                except OSError as e:
                    self.assertEqual(getattr(e, 'ntstatus', None), ps._NT_ACCESS_DENIED)
                    create = 'is refused'
                else:
                    ps._win_discard(handle)
                    create = 'works'
            flushed = ps._win_api()[1].FlushFileBuffers(held._handles[-1])
            error = ctypes.get_last_error()
            self.assertTrue(flushed or error == 5, 'FlushFileBuffers on a traversal-only hold: error %d' % error)
        print('\nWindows answer: a relative create through a traversal-only hold %s; flushing it %s.'
              % (create, 'works' if flushed else 'is refused (access denied)'))
        self.assertFalse(os.path.lexists(os.path.join(self.state, 'probe')))

    def test_a_folder_is_flushed_through_its_writable_hold(self):
        with self.writable() as state:
            os.close(state.create('new'))
            try:
                state.sync()
            except ps.PrivateStorageError:
                self.fail('FlushFileBuffers on a writable folder hold failed with Windows error %d' % ctypes.get_last_error())
        with ps.open_folder(self.state) as held:
            reason(self, held.sync, 'unavailable', 'folder')

    def test_remove_takes_the_name_away_at_once_while_a_reader_still_reads(self):
        """POSIX deletion (FileDispositionInfoEx): the name goes at once, an open reader keeps reading, and the name is
        free for a new file straight away."""
        reader = ps.open_no_links(self.path)           # shares delete, like every file the walk opens
        try:
            with self.writable() as state:
                recognise = lambda fd: os.read(fd, 100) == b'fixture value\n'
                self.assertEqual(self.until_settled(lambda: state.remove('secret', recognise)), 'removed')
                self.assertNotIn('secret', os.listdir(self.state), 'the name went at once (POSIX semantics)')
                with self.assertRaises(FileNotFoundError):
                    state.child_stat('secret')
                os.close(state.create('secret'))
            self.assertEqual(os.read(reader, 100), b'fixture value\n')
        finally:
            os.close(reader)

    def test_the_older_deletion_keeps_the_name_until_the_last_handle_closes(self):
        """FileDispositionInfo, the fallback: removal reports 'busy' while another handle keeps the name, and the name
        refuses opens and creates until that handle closes."""
        reader = ps.open_no_links(self.path)
        try:
            with patch.object(ps, '_POSIX_DELETE', False), self.writable() as state:
                self.assertEqual(state.remove('secret', lambda fd: True), 'busy')
                self.assertIn('secret', os.listdir(self.state), 'the older deletion keeps the name while it is open')
                self.assertEqual(state.remove('secret', lambda fd: True), 'busy')
                with self.assertRaises(FileNotFoundError):
                    state.child_stat('secret')
                with self.assertRaises(FileExistsError):
                    state.create('secret')
                os.close(reader)
                reader = None
                self.assertEqual(self.until_settled(lambda: state.remove('secret', lambda fd: True)), 'absent')
                os.close(state.create('secret'))
                # Removed at once, or (if another program glanced at the new file) pending, then gone.
                self.assertIn(self.until_settled(lambda: state.remove('secret', lambda fd: True)), ('removed', 'absent'))
                self.assertNotIn('secret', os.listdir(self.state))
        finally:
            if reader is not None:
                os.close(reader)

    def test_remove_is_busy_or_changed_and_leaves_the_file(self):
        other = os.open(self.path, os.O_RDONLY)        # the C runtime's own open does not share delete
        try:
            with self.writable() as state:
                self.assertEqual(state.remove('secret', lambda fd: True), 'busy')
        finally:
            os.close(other)
        self.assertTrue(os.path.isfile(self.path))
        junction(os.path.join(self.state, 'junction'), self.base)
        with self.writable() as state:
            self.assertEqual(state.remove('secret', lambda fd: False), 'changed')
            state.mkdir('sub')
            for name in ('sub', 'junction'):
                with self.subTest(name=name):
                    self.assertEqual(state.remove(name, lambda fd: True), 'changed')
            self.assertEqual(state.remove('absent', lambda fd: True), 'absent')
            self.assertTrue(os.path.isfile(self.path))
            shown = []

            def recognise(fd):
                shown.append(ps.read_only(fd))
                return True
            self.assertIn(self.until_settled(lambda: state.remove('secret', recognise)), ('removed', 'absent'))
            self.assertEqual(set(shown), {True}, 'expect is shown a descriptor that can only read')
        self.assertEqual(sorted(os.listdir(self.state)), ['junction', 'sub'])
        self.assertTrue(os.path.isdir(self.base))

    def test_child_stat_reads_the_name_itself_with_its_real_file_id(self):
        junction(os.path.join(self.state, 'junction'), self.base)
        with ps.open_folder(self.state) as state:
            info = state.child_stat('secret')
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertTrue(os.path.samestat(info, os.stat(self.path)))
            self.assertEqual(info.st_ino, os.stat(self.path).st_ino)
            self.assertNotEqual(info.st_ino, 0)
            link = state.child_stat('junction')
            self.assertTrue(stat.S_ISLNK(link.st_mode), 'a junction is reported as a link')
            self.assertTrue(link.st_file_attributes & ps.REPARSE_POINT)
            self.assertFalse(os.path.samestat(link, os.stat(self.base)), 'the junction itself, not where it points')
            with self.assertRaises(FileNotFoundError):
                state.child_stat('absent')

    def test_child_stat_reports_a_file_symbolic_link_as_a_link(self):
        try:
            os.symlink(self.path, os.path.join(self.state, 'link'))
        except OSError as e:
            self.skipTest('creating a file symbolic link needs Developer Mode or the symlink privilege (%s)' % type(e).__name__)
        with ps.open_folder(self.state) as state:
            link = state.child_stat('link')
            self.assertTrue(stat.S_ISLNK(link.st_mode))
            self.assertFalse(os.path.samestat(link, os.stat(self.path)), 'the link itself, not its target')

    def test_open_append_reads_appends_and_applies_the_unwritable_rule(self):
        exclude = os.path.join(self.state, 'exclude')
        with open(exclude, 'wb') as out:
            out.write(b'# fixture\n')                 # follows the folder's list, like a file Git made: not private
        if not self.owned_by_me(exclude):             # an elevated runner's default owner can be Administrators
            subprocess.run('icacls "%s" /setowner "%%USERDOMAIN%%\\%%USERNAME%%"' % exclude, shell=True, check=True,
                           capture_output=True, timeout=30)
        junction(os.path.join(self.state, 'junction'), self.base)
        with self.writable() as state:
            fd, created = state.open_append('exclude')
            try:
                self.assertFalse(created)
                self.assertEqual(os.read(fd, 100), b'# fixture\n')
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertEqual(os.write(fd, b'/rule/\n'), 7)      # append-only access: written at the end
            finally:
                os.close(fd)
            fd, created = state.open_append('new-exclude')
            os.close(fd)
            self.assertTrue(created)
            with self.assertRaises(FileNotFoundError):
                state.open_append('missing', create=False)
            with self.assertRaises(ps.PrivateStorageError) as raised:
                state.open_append('junction')
            self.assertIn(raised.exception.reason, ('link', 'not_regular'))
        with open(exclude, 'rb') as kept:
            self.assertEqual(kept.read(), b'# fixture\n/rule/\n')
        ps.check_private(os.path.join(self.state, 'new-exclude'), single_link=True)
        icacls(exclude, '/grant', '*S-1-1-0:(W)')     # Everyone may write
        with self.writable() as state:
            reason(self, lambda: state.open_append('exclude'), 'access')

    def owned_by_me(self, path):
        try:
            ps.windows_security(path)
            return True
        except ps.PrivateStorageError as e:
            return e.reason != 'owner'

    def test_a_read_only_file_is_removed_by_posix_deletion_and_left_by_the_older_one(self):
        """FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE: the newer call removes a file someone marked read-only; the
        older call cannot, and remove leaves it and says 'changed'."""
        marked = os.path.join(self.state, 'marked')
        os.close(ps.create_private_file(marked))
        os.chmod(self.path, stat.S_IREAD)              # sets the read-only attribute
        os.chmod(marked, stat.S_IREAD)
        try:
            with self.writable() as state:
                self.assertEqual(self.until_settled(lambda: state.remove('secret', lambda fd: True)), 'removed')
                with patch.object(ps, '_POSIX_DELETE', False):
                    self.assertEqual(state.remove('marked', lambda fd: True), 'changed')
            self.assertFalse(os.path.lexists(self.path))
            self.assertTrue(os.path.isfile(marked))
        finally:
            os.chmod(marked, stat.S_IREAD | stat.S_IWRITE)

    def test_read_only_and_unwritable_on_real_handles(self):
        fd = ps.open_no_links(self.path)
        try:
            self.assertTrue(ps.read_only(fd))
        finally:
            os.close(fd)
        fd = os.open(self.path, os.O_RDWR)
        try:
            self.assertFalse(ps.read_only(fd))
        finally:
            os.close(fd)
        fd = ps.open_no_links(self.state, directory=True)
        try:
            ps.unwritable(fd)                          # this user and SYSTEM only
        finally:
            os.close(fd)
        icacls(self.state, '/grant', '*S-1-1-0:(W)')   # Everyone may write
        fd = ps.open_no_links(self.state, directory=True)
        try:
            reason(self, lambda: ps.unwritable(fd), 'access', 'folder')
        finally:
            os.close(fd)
        program = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe')
        fd = ps.open_no_links(program)
        try:
            ps.unwritable(fd, system_owned=True)       # owned by TrustedInstaller; others may only read and run it
            reason(self, lambda: ps.unwritable(fd), 'owner')
        finally:
            os.close(fd)


def windows_handle_check(path):
    """Owner and list of an existing file, read on a handle (test helper)."""
    handle = ps._win_open(path, ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, 7, ps._OPEN_EXISTING, 0)
    try:
        ps.windows_handle_security(handle)
    finally:
        ps._win_close(handle)



if __name__ == '__main__':
    unittest.main(verbosity=2)
