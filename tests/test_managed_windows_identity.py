#!/usr/bin/env python3
"""Managed Claude's machine identity and pinned executable on Windows (step 8b.4; switched on there in 8b.5).

On every system: the SMBIOS parser (fixture tables), the Windows host fingerprint, the build pin by architecture,
the refused locations, and the bounded hash. Under the Windows simulation on macOS and Linux: finding the build
where the installer keeps it and refusing a link, a second name or the wrong bytes, and the supervisor keeping the
hashed handle open until Claude has started, and an availability check that hashes again only when the file
changed. With fakes everywhere: a started program that is not the checked file is ended before it runs. On real
Windows only: the SMBIOS call, this user's SID and the whole identity, a read-only-sharing handle that stops
writers, hashing through it, a junction refused, and a process started while that handle is held, and refused when
it was not started from the checked file. No Claude is started.

Run: python tests/test_managed_windows_identity.py
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import private_storage as ps
import claude_managed
import claude_managed_runner as runner_module
import claude_managed_windows as windows

SIMULATED = os.name != 'nt'
WINDOWS = os.name == 'nt' and ps.folder_walk_available()
MACHINE = uuid.UUID('12345678-9abc-def0-1122-334455667788')


def structure(kind, formatted, strings=()):
    """One SMBIOS structure: type, length, handle, the formatted area, then its strings and a double NUL."""
    head = bytes([kind, 4 + len(formatted)]) + b'\x01\x00' + formatted
    return head + (b''.join(text + b'\x00' for text in strings) + b'\x00' if strings else b'\x00\x00')


def system_information(raw_uuid):
    """Type 1: four string numbers, the 16-byte UUID at offset 8, wake-up type, SKU and family (length 0x1B)."""
    return structure(1, bytes([1, 2, 3, 4]) + raw_uuid + bytes([6, 5, 6]),
                     (b'Fictional maker', b'Fictional model', b'1.0', b'FICTIONAL-SERIAL', b'Family'))


def table(structures, major=3, minor=4, extra=b''):
    data = b''.join(structures)
    return bytes([0, major, minor, 0]) + struct.pack('<I', len(data) + len(extra)) + data


BIOS = structure(0, bytes(14), (b'Fictional BIOS', b'1.0'))
END = structure(127, b'')


class SmbiosTests(unittest.TestCase):
    def test_the_system_uuid_is_read_with_its_first_fields_little_endian(self):
        raw = table([BIOS, system_information(MACHINE.bytes_le), END])
        self.assertEqual(windows.parse_smbios_uuid(raw), MACHINE)

    def test_before_smbios_2_6_the_bytes_are_taken_as_they_are(self):
        raw = table([system_information(MACHINE.bytes), END], major=2, minor=5)
        self.assertEqual(windows.parse_smbios_uuid(raw), MACHINE)

    def test_short_truncated_missing_unset_and_absent_are_refused(self):
        cases = {
            'no header': b'\x00\x03',
            'length past the data': table([BIOS, END], extra=b'x' * 10),
            'strings without their end': table([structure(0, bytes(14))[:-2] + b'Fictional']),
            'no system information': table([BIOS, END]),
            'too short for a UUID': table([structure(1, bytes([1, 2, 3, 4])), END]),
            'all zero': table([system_information(b'\x00' * 16), END]),
            'all FF': table([system_information(b'\xff' * 16), END]),
        }
        cases['a structure cut short'] = (bytes([0, 3, 4, 0]) + struct.pack('<I', len(BIOS) + 10)
                                          + (BIOS + system_information(MACHINE.bytes_le))[:len(BIOS) + 10])
        for name, raw in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    windows.parse_smbios_uuid(raw)


class IdentityTests(unittest.TestCase):
    def test_the_windows_fingerprint_names_machine_and_user(self):
        sid = 'S-1-5-21-1111111111-2222222222-3333333333-1001'
        expected = hashlib.sha256(('watch-managed-claude/v1/windows/%s/%s' % (MACHINE, sid)).encode()).hexdigest()
        self.assertEqual(windows.host_fingerprint(MACHINE, sid), expected)
        self.assertNotEqual(windows.host_fingerprint(MACHINE, sid), windows.host_fingerprint(MACHINE, sid + '2'))

    def test_the_windows_record_uses_the_pin_for_this_machine(self):
        with mock.patch.object(claude_managed.ps, 'WIN', True), \
                mock.patch.object(windows, 'system_uuid', return_value=MACHINE), \
                mock.patch.object(windows, 'user_sid', return_value='S-1-5-21-1-2-3-1001'), \
                mock.patch.object(windows, 'native_architecture', return_value='arm64'):
            record = claude_managed.local_configuration_identity()
        self.assertEqual(record, dict(schemaVersion=1, profile='normal', executableVersion='2.1.323',
                                      hostFingerprint=windows.host_fingerprint(MACHINE, 'S-1-5-21-1-2-3-1001'),
                                      executableSha256=windows.WINDOWS_PINS['arm64'][0]))
        with mock.patch.object(claude_managed.ps, 'WIN', True), \
                mock.patch.object(windows, 'native_architecture', return_value=None):
            with self.assertRaises(ValueError):
                claude_managed.local_configuration_identity()      # no pinned build: no record matches

    @unittest.skipIf(os.name == 'nt', 'the Mac identity needs a POSIX uid')
    def test_the_mac_fingerprint_is_unchanged(self):
        hardware = subprocess.CompletedProcess([], 0, plistlib.dumps([{'IOPlatformUUID': str(MACHINE).upper()}]))
        with mock.patch.object(claude_managed.sys, 'platform', 'darwin'), \
                mock.patch.object(claude_managed.subprocess, 'run', return_value=hardware):
            record = claude_managed.local_configuration_identity()
        expected = hashlib.sha256(('watch-managed-claude/v1/%s/%d' % (MACHINE, os.getuid())).encode()).hexdigest()
        self.assertEqual(record, dict(schemaVersion=1, hostFingerprint=expected, profile='normal',
                                      executableVersion='2.1.323', executableSha256=claude_managed.PINNED_SHA256))


class PinAndLocationTests(unittest.TestCase):
    def test_the_build_follows_the_machines_own_architecture(self):
        self.assertEqual(windows.native_architecture(lambda: 0x8664), 'x64')
        self.assertEqual(windows.native_architecture(lambda: 0xAA64), 'arm64')      # also under x64 Python
        self.assertIsNone(windows.native_architecture(lambda: 0x014C))             # 32-bit Windows: none

    def test_windows_without_iswow64process2_asks_getnativesysteminfo(self):
        older = lambda code: (lambda: None, lambda: code)                       # noqa: E731
        self.assertEqual(windows.native_architecture(*older(9)), 'x64')          # PROCESSOR_ARCHITECTURE_AMD64
        self.assertEqual(windows.native_architecture(*older(12)), 'arm64')       # PROCESSOR_ARCHITECTURE_ARM64
        self.assertIsNone(windows.native_architecture(*older(0)))                # 32-bit Intel: none
        self.assertIsNone(windows.native_architecture(*older(5)))                # 32-bit ARM: none
        asked = []
        self.assertEqual(windows.native_architecture(lambda: 0xAA64, lambda: asked.append(1) or 9), 'arm64')
        self.assertEqual(asked, [])                                              # only where the call is missing
        self.assertEqual(windows.windows_pin('x64'), (
            '5064e6e0572dca86257363ca04d48653eef20ef27f1bd04b504ed7199ed3fd13', 367634080))
        self.assertEqual(windows.windows_pin('arm64')[0],
                         '3071c54f466e40e668c273b53ed8942efce35bf069d66a69668f74299b0cb888')
        self.assertIsNone(windows.windows_pin('x86'))

    def test_shims_aliases_and_the_desktop_apps_copy_are_refused(self):
        refused = ['C:\\Users\\fictional\\AppData\\Roaming\\npm\\claude.cmd',
                   'C:\\Users\\fictional\\AppData\\Roaming\\npm\\claude.bat',
                   'C:\\Users\\fictional\\AppData\\Local\\Microsoft\\WindowsApps\\claude.exe',
                   'C:\\Program Files\\WindowsApps\\Claude_1.0.0.0_x64__fictional\\app\\claude.exe',
                   'C:\\Users\\fictional\\AppData\\Local\\AnthropicClaude\\app-1.0\\claude.exe']
        for path in refused:
            with self.subTest(path=path):
                self.assertIsNotNone(windows.refused_location(path))
        self.assertIsNone(windows.refused_location(
            'C:\\Users\\fictional\\.local\\share\\claude\\versions\\2.1.323'))

    def test_only_the_installers_place_is_looked_in(self):
        with tempfile.TemporaryDirectory() as home:
            base = os.path.join(os.path.realpath(home), '.local', 'share', 'claude', 'versions')
            self.assertEqual(windows.claude_candidates(home),
                             [os.path.join(base, '2.1.323'), os.path.join(base, '2.1.323.exe')])
            with self.assertRaises(FileNotFoundError):
                windows.locate_claude(home)

    def test_hashing_has_its_own_bound(self):
        self.assertEqual(windows.WINDOWS_HASH_SECONDS, 60)
        chunks = iter([b'abc', b'def', b''])
        self.assertEqual(windows.hash_open_file(0, 60, read=lambda fd, size: next(chunks)),
                         hashlib.sha256(b'abcdef').hexdigest())
        ticks = iter(range(100))
        with self.assertRaises(ValueError):           # a slow reader passes the bound
            windows.hash_open_file(0, 3, read=lambda fd, size: b'x' * size, clock=lambda: next(ticks))


class FakeProcess:
    pid = 4242


class StartedImageTests(unittest.TestCase):
    """start_in_job's check of the program the suspended process was started from, with every Win32 step faked."""

    def start(self, started_from):
        events = []

        class Popen:
            pid, _handle = 4242, 77
            stdin = stdout = stderr = None

            def __init__(self, args, **options):
                events.append(('created', options['creationflags'] & windows.CREATE_SUSPENDED))

        class Job:
            def assign(self, handle):
                events.append(('assigned', handle))

            def close(self):
                events.append('job closed')

        def image_of(handle):
            events.append(('identified', handle))
            return started_from

        def ended(process, reason):
            events.append(('ended', reason))
            return reason
        with mock.patch.object(windows.subprocess, 'Popen', Popen), mock.patch.object(windows, 'Job', Job), \
                mock.patch.object(windows, '_resume_only_thread', lambda pid: events.append(('resumed', pid))), \
                mock.patch.object(windows, '_end_suspended', ended):
            try:
                windows.start_in_job(['claude'], 'C:\\fictional', image=(0x2A5E5E1D, 99), image_of=image_of)
            except windows.JobError as error:
                events.append(('refused', str(error)))
        return events

    def test_the_checked_file_is_resumed(self):
        self.assertEqual(self.start((0x2A5E5E1D, 99)), [
            ('created', windows.CREATE_SUSPENDED), ('assigned', 77), ('identified', 77), ('resumed', 4242)])

    def test_another_file_is_ended_and_never_resumed(self):
        for other in ((0x2A5E5E1D, 100), (0x11111111, 99)):          # another index, or another volume
            with self.subTest(other=other):
                events = self.start(other)
                reason = 'the started program is not the checked Claude Code'
                self.assertEqual(events, [('created', windows.CREATE_SUSPENDED), ('assigned', 77), ('identified', 77),
                                          ('ended', reason), 'job closed', ('refused', reason)])


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedExecutableTests(unittest.TestCase):
    def setUp(self):
        from windows_simulation import simulated_windows
        temporary = tempfile.TemporaryDirectory(prefix='managed-identity-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.versions = self.home / '.local' / 'share' / 'claude' / 'versions'
        self.versions.mkdir(parents=True)
        (self.home / '.live-room').mkdir(mode=0o700)
        self.content = b'fictional pinned build'
        self.pin = (hashlib.sha256(self.content).hexdigest(), len(self.content))
        stack = ExitStack()
        stack.enter_context(simulated_windows())
        stack.enter_context(mock.patch.object(windows, 'windows_pin', return_value=self.pin))
        # NTFS's change time, simulated by POSIX's ctime: both move on every write and cannot be set by utime.
        stack.enter_context(mock.patch.object(windows, 'change_time', lambda fd: os.fstat(fd).st_ctime_ns))
        self.addCleanup(stack.close)

    @staticmethod
    def as_windows_platform():
        """The platform gates (the supervisor's start, and preflight for a real manager) read sys.platform, which
        lets macOS and Windows through and refuses Linux: answer 'win32' while the Windows path behind them is
        exercised, so these tests run the same on Linux as on a Mac."""
        return mock.patch.object(sys, 'platform', 'win32')

    def manager(self):
        return claude_managed.ManagedClaude(self.home / '.live-room', home=self.home, enabled=True,
                                            configuration_ready=True)

    def install(self, name='2.1.323', content=None):
        path = self.versions / name
        path.write_bytes(content if content is not None else self.content)
        path.chmod(0o700)
        return path

    def assert_refused(self):
        with self.assertRaises(claude_managed.ManagedError) as raised:
            self.manager()._executable()
        self.assertEqual(raised.exception.code, 'app_version_unsupported')

    def test_found_with_or_without_exe(self):
        for name in ('2.1.323', '2.1.323.exe'):
            with self.subTest(name=name):
                path = self.install(name)
                self.assertEqual(self.manager()._executable(),
                                 {'path': str(path), 'version': '2.1.323', 'sha256': self.pin[0]})
                path.unlink()
        self.assert_refused()                                       # not installed

    def test_wrong_bytes_a_second_name_or_a_link_are_refused(self):
        self.install(content=b'fictional other build!')             # same size, other bytes
        self.assert_refused()
        (self.versions / '2.1.323').unlink()
        path = self.install()
        os.link(str(path), str(self.home / 'second-name'))
        self.assert_refused()
        (self.home / 'second-name').unlink()
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir()
        os.replace(str(path), str(elsewhere / '2.1.323'))
        self.versions.rmdir()
        self.versions.symlink_to(elsewhere, target_is_directory=True)
        self.assert_refused()

    def counting_hashes(self):
        hashed, real = [], windows.hash_open_file

        def counting(fd, seconds, *args, **options):
            hashed.append(seconds)
            return real(fd, seconds, *args, **options)
        patcher = mock.patch.object(windows, 'hash_open_file', counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return hashed

    def available(self, manager):
        with mock.patch.object(windows, 'breakaway_refusal', return_value=None), self.as_windows_platform():
            capability = manager.preflight()
        return capability.available, capability.code

    def test_a_second_availability_check_does_not_read_the_file_again(self):
        path = self.install()
        hashed, manager = self.counting_hashes(), self.manager()
        self.assertEqual(self.available(manager), (True, ''))
        self.assertEqual(self.available(manager), (True, ''))
        manager._executable()
        self.assertEqual(hashed, [60])                               # once, with Windows' own bound
        info = os.stat(str(path))
        os.utime(str(path), ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))   # a new last-write time
        self.assertEqual(self.available(manager), (True, ''))
        self.assertEqual(len(hashed), 2)
        self.assertEqual(self.available(manager), (True, ''))
        self.assertEqual(len(hashed), 2)

    def test_a_changed_or_replaced_file_never_reuses_an_earlier_check(self):
        path = self.install()
        hashed, manager = self.counting_hashes(), self.manager()
        self.assertEqual(self.available(manager), (True, ''))
        info = os.stat(str(path))
        with open(str(path), 'ab') as grown:                         # another size: refused, and forgotten
            grown.write(b'!')
        self.assertEqual(self.available(manager), (False, 'app_version_unsupported'))
        self.assertIsNone(manager._windows_verified)
        path.write_bytes(self.content)                                # the right bytes again,
        os.utime(str(path), ns=(info.st_atime_ns, info.st_mtime_ns))  # with the old last-write time:
        self.assertEqual(self.available(manager), (True, ''))         # hashed, as the refusal forgot the pass
        self.assertEqual(len(hashed), 2)
        self.assertEqual(len(hashed), 2)
        copy = self.versions / 'copy'
        copy.write_bytes(self.content)
        copy.chmod(0o700)
        os.utime(str(copy), ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(str(copy), str(path))                              # another file, same bytes and times
        self.assertEqual(self.available(manager), (True, ''))
        self.assertEqual(len(hashed), 3)
        path.write_bytes(b'fictional other build!')                   # same size, other bytes
        self.assertEqual(self.available(manager), (False, 'app_version_unsupported'))
        self.assertEqual(len(hashed), 4)

    def test_the_change_time_not_the_creation_time_is_remembered(self):
        path = str(self.install())
        hashed, changed = self.counting_hashes(), [1]
        identity = ps.file_identity
        with mock.patch.object(windows, 'change_time', lambda fd: changed[0]):
            verified = windows.verify_pinned(path, self.pin)
            with mock.patch.object(ps, 'file_identity', lambda fd: tuple(identity(fd))[:4] + (12345,)):
                self.assertEqual(windows.verify_pinned(path, self.pin, verified), verified)   # creation time: unused
            self.assertEqual(len(hashed), 1)
            changed[0] = 2                        # the file system saw a write, whatever the last-write time says
            windows.verify_pinned(path, self.pin, verified)
            self.assertEqual(len(hashed), 2)

    def test_an_earlier_check_counts_only_for_the_same_path_and_pin(self):
        path = str(self.install())
        hashed = self.counting_hashes()
        verified = windows.verify_pinned(path, self.pin)
        self.assertEqual(verified[:2], (path, self.pin))
        self.assertEqual(windows.verify_pinned(path, self.pin, verified), verified)
        self.assertEqual(len(hashed), 1)
        other = str(self.install('2.1.323.exe'))
        windows.verify_pinned(other, self.pin, verified)
        self.assertEqual(len(hashed), 2)
        with self.assertRaises(ValueError):                           # another pin: hashed, and refused
            windows.verify_pinned(path, ('0' * 64, self.pin[1]), verified)
        self.assertEqual(len(hashed), 3)

    def test_the_supervisor_keeps_the_hashed_handle_until_claude_has_started(self):
        exe = str(self.install())
        project = self.home / 'project'
        project.mkdir()
        device, inode = ps.path_identity(str(project))
        transport = runner_module.NativeTransport({
            'executable': {'path': exe}, 'nativeId': str(uuid.uuid4()), 'profile': 'restricted_test',
            'folder': {'path': str(project), 'device': device, 'inode': inode}})
        opened, events = [], []
        real_open = windows.open_pinned

        def open_pinned(path, pin):
            fd = real_open(path, pin)
            opened.append(fd)
            events.append('hashed')
            return fd

        def start_in_job(args, cwd, image=None):
            os.fstat(opened[0])                           # still open: the file cannot be replaced meanwhile
            self.assertEqual(image, tuple(ps.file_identity(opened[0])[:2]))     # the hashed file, to compare
            events.append(('started', args[0], cwd))
            return FakeProcess(), mock.Mock()
        with mock.patch.object(windows, 'open_pinned', open_pinned), \
                mock.patch.object(windows, 'start_in_job', start_in_job), \
                mock.patch.object(windows, 'ThreadedPipes', mock.Mock()), self.as_windows_platform():
            transport.start()
        self.assertEqual(events, ['hashed', ('started', exe, str(project))])
        with self.assertRaises(OSError):
            os.fstat(opened[0])                           # closed once Claude had started

    def test_a_start_that_fails_still_closes_the_handle(self):
        exe = str(self.install())
        project = self.home / 'project'
        project.mkdir()
        device, inode = ps.path_identity(str(project))
        transport = runner_module.NativeTransport({
            'executable': {'path': exe}, 'nativeId': str(uuid.uuid4()), 'profile': 'restricted_test',
            'folder': {'path': str(project), 'device': device, 'inode': inode}})
        opened = []
        real_open = windows.open_pinned

        def open_pinned(path, pin):
            opened.append(real_open(path, pin))
            return opened[-1]
        with mock.patch.object(windows, 'open_pinned', open_pinned), \
                mock.patch.object(windows, 'start_in_job', side_effect=windows.JobError('fictional refusal')), \
                self.as_windows_platform():
            with self.assertRaises(runner_module.RunnerError) as raised:
                transport.start()
        self.assertEqual(raised.exception.code, 'unsupported_build')
        with self.assertRaises(OSError):
            os.fstat(opened[0])


@unittest.skipUnless(WINDOWS, 'Windows firmware tables, sharing modes and junctions')
class WindowsExecutableTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='managed-identity-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / 'private'
        ps.make_private_folder(str(self.base))
        self.content = b'fictional pinned build'
        self.pin = (hashlib.sha256(self.content).hexdigest(), len(self.content))
        self.path = self.base / '2.1.323'
        fd = ps.create_private_file(str(self.path))
        try:
            os.write(fd, self.content)
        finally:
            os.close(fd)

    def test_the_smbios_system_uuid_is_set(self):
        machine = windows.system_uuid()
        self.assertNotIn(machine.int, (0, (1 << 128) - 1))
        self.assertIn(windows.native_architecture(), ('x64', 'arm64'))
        # The older answer agrees where it can see the truth (not under x64 emulation on ARM64).
        if windows._native_machine() == 0x8664:
            self.assertEqual(windows.native_architecture(lambda: None), 'x64')

    def test_this_users_identity_is_read_for_real_and_is_stable(self):
        sid = windows.user_sid()
        self.assertRegex(sid, r'^S-1-\d+(-\d+)+$')
        self.assertEqual(windows.user_sid(), sid)
        first, second = claude_managed.local_configuration_identity(), claude_managed.local_configuration_identity()
        self.assertEqual(first, second)
        self.assertEqual(set(first), {'schemaVersion', 'hostFingerprint', 'profile', 'executableVersion',
                                      'executableSha256'})
        self.assertTrue(re.fullmatch('[0-9a-f]{64}', first['hostFingerprint']))
        self.assertEqual(first['hostFingerprint'], windows.host_fingerprint(windows.system_uuid(), sid))
        self.assertEqual((first['schemaVersion'], first['profile'], first['executableVersion']),
                         (1, 'normal', '2.1.323'))
        self.assertEqual(first['executableSha256'], windows.windows_pin()[0])

    def test_a_handle_that_shares_reading_only_stops_writers(self):
        fd = ps.open_no_links(str(self.path), single_link=True, deny_writers=True)
        try:
            with self.assertRaises(PermissionError):           # a sharing violation
                open(str(self.path), 'r+b')
            with open(str(self.path), 'rb') as reader:         # readers are still welcome
                self.assertEqual(reader.read(), self.content)
        finally:
            os.close(fd)
        with open(str(self.path), 'r+b'):
            pass

    def test_the_hash_is_taken_through_that_handle(self):
        fd = windows.open_pinned(str(self.path), self.pin)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(os.read(fd, 100), self.content)
        finally:
            os.close(fd)
        with self.assertRaises(ValueError):
            windows.open_pinned(str(self.path), ('0' * 64, len(self.content)))

    def test_the_hold_keeps_the_whole_path_in_place(self):
        fd = ps.open_no_links(str(self.path), single_link=True, deny_writers=True)
        try:
            with self.assertRaises(PermissionError):             # the folder above it
                os.rename(str(self.base), str(self.base) + '-moved')
            with self.assertRaises(PermissionError):             # the file itself
                os.rename(str(self.path), str(self.base / 'moved'))
            with self.assertRaises(PermissionError):
                os.remove(str(self.path))
        finally:
            os.close(fd)
        os.rename(str(self.path), str(self.base / 'moved'))      # once it is let go

    def test_the_change_time_moves_on_a_write_that_setfiletime_cannot_hide(self):
        def changed():
            fd = ps.open_no_links(str(self.path), single_link=True)
            try:
                return windows.change_time(fd)
            finally:
                os.close(fd)
        before, info = changed(), os.stat(str(self.path))
        time.sleep(0.1)
        with open(str(self.path), 'r+b') as rewrite:              # same size, other bytes
            rewrite.write(b'F')
        os.utime(str(self.path), ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(os.stat(str(self.path)).st_mtime_ns, info.st_mtime_ns)
        self.assertNotEqual(changed(), before)

    def test_a_junction_on_the_way_is_refused(self):
        link = self.base / 'versions-link'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.base)], check=True, capture_output=True,
                       timeout=30)
        self.addCleanup(os.rmdir, str(link))
        with self.assertRaises((OSError, ValueError)):
            windows.open_pinned(str(link / '2.1.323'), self.pin)
        with self.assertRaises(OSError):
            ps.open_no_links(str(link / '2.1.323'), single_link=True, deny_writers=True)

    def test_a_program_starts_while_its_file_is_held_for_reading_only(self):
        fd = ps.open_no_links(os.path.realpath(sys.executable), deny_writers=True)
        try:
            image = tuple(ps.file_identity(fd)[:2])
            self.assertEqual(windows.process_image_identity(windows._api()[2].GetCurrentProcess()), image)
            process, job = windows.start_in_job([sys.executable, '-c', 'pass'], str(self.base), image=image)
            try:
                self.assertEqual(process.wait(timeout=60), 0)
            finally:
                job.close()
                for stream in (process.stdin, process.stdout):
                    stream.close()
        finally:
            os.close(fd)

    def test_a_program_started_from_another_file_never_runs(self):
        fd = ps.open_no_links(str(self.path), single_link=True, deny_writers=True)
        try:
            checked = tuple(ps.file_identity(fd)[:2])            # the fictional build, not Python
            with self.assertRaises(windows.JobError) as raised:
                windows.start_in_job([sys.executable, '-c', 'open("ran.txt", "w").close()'], str(self.base),
                                     image=checked)
            self.assertIn('not the checked Claude Code', str(raised.exception))
        finally:
            os.close(fd)
        time.sleep(1)
        self.assertFalse((self.base / 'ran.txt').exists())       # ended while still suspended


if __name__ == '__main__':
    unittest.main()
