#!/usr/bin/env python3
"""The shared private-storage layer on POSIX and Windows. Synthetic files in temporary folders only.

Run: python tests/test_private_storage.py
"""
import ctypes
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

        def new_handle(node, name, access, share):
            handle = 500 + len(self.handles)
            self.log.append((name, access, share, sorted(self.open_now)))
            self.handles[handle] = (node, name, access, share)
            self.open_now.add(handle)
            return handle

        def fake_open(path, access, share, disposition, flags, sa=None):
            self.assertEqual((disposition, flags, sa), (ps._OPEN_EXISTING, 0, None))
            node = self.drives.get(path)
            if node is None:
                error = OSError('missing'); error.winerror = 3
                raise error
            return new_handle(node, path, access, share)

        def fake_child(parent, name, access, share, directory):
            self.assertIn(parent, self.open_now, 'opened relative to a closed handle')
            if self.during_open:
                self.during_open(name)
            node = self.handles[parent][0]['children'].get(name)
            def fail(status):
                error = OSError('nt'); error.ntstatus = status
                raise error
            if node is None:
                fail(0xC0000034)
            if node['kind'] == 'busy':
                fail(0xC0000043)                         # STATUS_SHARING_VIOLATION
            is_folder = node['kind'] in ('folder', 'link-folder')
            if directory and not is_folder:
                fail(0xC0000103)
            if not directory and node['kind'] == 'folder':
                fail(0xC00000BA)
            return new_handle(node, name, access, share)

        def fake_close(handle):
            if handle is not None:
                self.assertIn(handle, self.open_now, 'closed twice or never opened')
                self.open_now.discard(handle)
                self.closed.append(handle)

        def fake_facts(handle):
            kind = self.handles[handle][0]['kind']
            return {'folder': (ps.DIRECTORY, 1, 1), 'file': (0x20, 1, 1), 'two-names': (0x20, 2, 1),
                    'link-folder': (ps.DIRECTORY | ps.REPARSE_POINT, 1, 1), 'link-file': (ps.REPARSE_POINT | 0x20, 1, 1),
                    'device': (0x20, 1, 2)}[kind]

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
                        patch.object(ps, 'windows_handle_security', fake_security), patch.object(ps, '_win_fd', fake_fd)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def rename_allowed(self, node):
        """Windows' sharing rule for a rename or delete of ``node`` (DELETE access) while handles are open on it."""
        for handle in self.open_now:
            held, _, access, share = self.handles[handle]
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
                     'C:\\Users\\example.\\chat.jsonl', '1:\\chat.jsonl'):
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
        with open(self.file, 'w') as out:
            out.write('fixture\n')

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
            with open('\\\\?\\' + os.path.join(deep, 'notes.jsonl'), 'w') as out:
                out.write('deep\n')
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


def windows_handle_check(path):
    """Owner and list of an existing file, read on a handle (test helper)."""
    handle = ps._win_open(path, ps._READ_CONTROL | ps._FILE_READ_ATTRIBUTES, 7, ps._OPEN_EXISTING, 0)
    try:
        ps.windows_handle_security(handle)
    finally:
        ps._win_close(handle)



if __name__ == '__main__':
    unittest.main(verbosity=2)
