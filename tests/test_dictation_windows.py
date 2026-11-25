#!/usr/bin/env python3
"""Dictation on Windows: the helper's private state, the proxy's read of it, model setup, and the helper's start
and stop without a signal. No microphone, runtime or model download.

On macOS and Linux the Windows branches run under tests/windows_simulation.py (private_storage answers with the
POSIX walk, and fcntl and os.getuid fail if a Windows branch reaches for them). The start-and-stop check runs on
every system. On real Windows the state, proxy and lifecycle checks run against the real access lists; the model
setup tests in tests/test_dictation_assets.py run there with private fixtures. Synthetic files in temporary folders.

Run: python tests/test_dictation_windows.py
"""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(HERE))
import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import private_storage as ps
import dictation_proxy as proxy
import test_dictation_assets as asset_tests
from integrations.dictation.helper import probe
from integrations.dictation.state import PrivateState, StateError

SIMULATED = os.name != 'nt'
REAL_WINDOWS = os.name == 'nt' and ps.folder_walk_available()
HELPER = ROOT / 'integrations' / 'dictation' / 'helper.py'


def simulation():
    from windows_simulation import simulated_windows
    return simulated_windows()


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedWindowsModelSetupTests(asset_tests.AssetsTests):
    """Every model setup test, with the Windows branches taken: the walk, private files and folders, the private
    lock file and the retried renames."""

    def setUp(self):
        stack = ExitStack()
        self.calls = stack.enter_context(simulation())
        self.addCleanup(stack.close)
        super().setUp()

    def test_the_windows_branches_are_the_ones_used(self):
        for name, data in self.data.items():
            self.write(name, data)
        self.assertTrue(asset_tests.assets.validate_model(self.model))
        self.assertIn('open_folder', self.calls)
        self.assertIn('open_no_links', self.calls)
        del self.calls[:]
        asset_tests.assets.download_model(self.model, self.info['bytes'], opener=asset_tests.Opener(self.data))
        for name in ('open_lock_file', 'try_lock', 'close_lock'):
            self.assertIn(name, self.calls)

    def test_a_rename_a_scanner_briefly_blocks_is_tried_again(self):
        attempts = []

        def replace(source, target):
            attempts.append(source)
            if len(attempts) < 3:
                raise PermissionError(13, 'fictional sharing violation')
        with mock.patch.object(asset_tests.assets.time, 'sleep'):
            asset_tests.assets._windows_retry(replace, 'a', 'b')
        self.assertEqual(len(attempts), 3)
        with self.assertRaises(OSError):
            asset_tests.assets._windows_retry(mock.Mock(side_effect=OSError('not a sharing violation')))


class StateChecks:
    def windows(self):
        raise NotImplementedError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dictation-windows-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_the_credential_is_created_private_and_read_back_by_helper_and_proxy(self):
        with self.windows():
            key = PrivateState(self.home).key(create=True)
            self.assertEqual(PrivateState(self.home).key(), key)
            self.assertEqual(proxy._read_helper_key(self.home), key)
        header = self.home / '.live-room' / 'dictation' / 'header'
        self.assertEqual(header.read_bytes(), b'X-Dictation-Key: ' + key.encode() + b'\n')
        self.assertEqual([path.name for path in header.parent.iterdir()], ['header'])
        with self.windows():
            for folder in (self.home / '.live-room', header.parent):
                ps.check_private(str(folder), directory=True)
            ps.check_private(str(header), single_link=True)

    def test_status_without_state_creates_nothing(self):
        with self.windows():
            with self.assertRaises(StateError):
                PrivateState(self.home).key()
        self.assertEqual(list(self.home.iterdir()), [])


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedWindowsStateTests(StateChecks, unittest.TestCase):
    def windows(self):
        return simulation()

    def refused(self):
        with self.windows():
            with self.assertRaises(StateError):
                PrivateState(self.home).key()
            with self.assertRaises((OSError, ValueError)):
                proxy._read_helper_key(self.home)

    def test_a_credential_or_folder_others_can_read_is_refused_by_both(self):
        with self.windows():
            PrivateState(self.home).key(create=True)
        state = self.home / '.live-room' / 'dictation'
        for path, opened, closed in ((state / 'header', 0o644, 0o600), (state, 0o755, 0o700),
                                     (self.home / '.live-room', 0o755, 0o700)):
            with self.subTest(path=path.name):
                path.chmod(opened)
                self.refused()
                path.chmod(closed)
        with self.windows():
            self.assertEqual(len(proxy._read_helper_key(self.home)), 64)

    def test_a_linked_or_second_named_credential_is_refused_by_both(self):
        with self.windows():
            PrivateState(self.home).key(create=True)
        header = self.home / '.live-room' / 'dictation' / 'header'
        saved = header.with_name('saved')
        header.rename(saved)
        header.symlink_to(saved)
        self.refused()
        header.unlink()
        saved.rename(header)
        os.link(str(header), str(header.with_name('second-name')))
        self.refused()

    def test_a_linked_state_folder_creates_nothing_outside(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            (self.home / '.live-room').symlink_to(elsewhere, target_is_directory=True)
            with self.windows():
                with self.assertRaises(StateError):
                    PrivateState(self.home).key(create=True)
            self.assertEqual(list(Path(elsewhere).iterdir()), [])

    def test_the_windows_walk_is_used(self):
        with self.windows() as calls:
            PrivateState(self.home).key(create=True)
            proxy._read_helper_key(self.home)
        self.assertIn('open_folder', calls)


@unittest.skipUnless(REAL_WINDOWS, 'requires real Windows and its folder walk')
class WindowsStateTests(StateChecks, unittest.TestCase):
    def windows(self):
        from contextlib import nullcontext
        return nullcontext()

    def test_a_credential_others_can_read_is_refused_by_both(self):
        PrivateState(self.home).key(create=True)
        header = self.home / '.live-room' / 'dictation' / 'header'
        # Authenticated Users may read it: no longer private.
        subprocess.run(['icacls', str(header), '/grant', '*S-1-5-11:R'], check=True, capture_output=True, timeout=30)
        with self.assertRaises(StateError):
            PrivateState(self.home).key()
        with self.assertRaises((OSError, ValueError)):
            proxy._read_helper_key(self.home)


class HelperLifecycleTests(unittest.TestCase):
    """The helper serves and stops on request through standard input: the way a parent stops it on Windows, where
    a child gets no Ctrl-C. No signal and no select on a pipe; it runs on every system."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dictation-helper-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def start(self):
        child = subprocess.Popen([sys.executable, str(HELPER), 'serve', '--home', str(self.home), '--port', '0',
                                  '--stop-on-stdin'], cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=30)
        self.addCleanup(cleanup)
        line = []
        reader = threading.Thread(target=lambda: line.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(60)
        self.assertTrue(line and line[0].startswith(b'Local dictation helper on 127.0.0.1:'), line)
        return child, int(line[0].split(b':')[1].split(b';')[0])

    def test_it_serves_status_and_stops_when_asked(self):
        child, port = self.start()
        status = probe(self.home, port)
        self.assertEqual((status['state'], status['code']), ('not_installed', 'dependency_missing'))
        out, err = child.communicate(b'stop\n', timeout=30)
        self.assertEqual((child.returncode, out, err), (0, b'', b''))
        self.assertEqual(probe(self.home, port)['state'], 'not_running')

    def test_it_stops_when_its_input_closes(self):
        child, _ = self.start()
        out, err = child.communicate(b'', timeout=30)
        self.assertEqual((child.returncode, out, err), (0, b'', b''))


if __name__ == '__main__':
    unittest.main()
