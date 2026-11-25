"""The Windows folder window for New chat, with fictional paths and fake dialogs. No test here shows a window: on
Windows the real dialog is created and configured, and released without Show()."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import new_chat_folder_chooser as chooser
import new_chat_folder_chooser_windows as windows
from new_chat_types import NewChatError


class Connection:
    def getsockname(self):
        return '127.0.0.1', 18801

    def getpeername(self):
        return '127.0.0.1', 54000


class FakeDialog:
    def __init__(self, value=None, fail=None, block=None):
        self.value = value or {'state': 'selected', 'path': 'C:\\Projects\\Orchard'}
        self.fail, self.block, self.calls = fail, block, []

    def open(self):
        self.calls.append('open')
        if self.fail == 'open':
            raise OSError('fictional COM failure')

    def configure(self, initial):
        self.calls.append(('configure', initial))

    def show(self):
        self.calls.append('show')
        if self.block is not None:
            self.block.wait(5)
        if self.fail == 'show':
            raise OSError('fictional COM failure')
        return self.value

    def close(self):
        self.calls.append('close')


def run_helper(dialog, data=None, **kw):
    frames = []
    windows.helper(data or {'parent': 4242, 'initial': ''}, dialog=dialog, emit=frames.append,
                   parent=lambda: 4242, alive=kw.pop('alive', lambda: True), **kw)
    return frames


class PathRules(unittest.TestCase):
    def test_drive_and_share_paths(self):
        for good in ('C:\\Projects\\Orchard', 'd:\\Caf\u00e9 Orchard', 'C:\\Projects\\Orchard\\', 'C:\\',
                     '\\\\fictional-server\\share\\Orchard', '\\\\fictional-server\\share'):
            with self.subTest(path=good):
                self.assertTrue(windows.windows_path(good))
        for bad in ('/fictional/Orchard', 'Projects\\Orchard', 'C:Projects', 'C:/Projects', 'C:\\Projects/Orchard',
                    '\\\\?\\C:\\Projects', '\\\\.\\C:\\Projects', '\\\\?\\UNC\\fictional-server\\share',
                    '\\\\.\\pipe\\fictional', '\\\\fictional-server', '\\\\fictional-server\\\\x', '\\\\..\\share\\x',
                    'C:\\Projects\\file.txt:stream', 'C:\\Projects\\..\\Windows', 'C:\\Projects\\.\\Orchard',
                    'C:\\Projects\\\\Orchard', 'C:\\Pro\njects', 'C:\\Pro\x7fjects', 'C:\\\ud800', 'C:\\' + 'x' * 4094,
                    None, b'C:\\Projects', '', '\\\\[fe80::1]\\share\\x'):
            with self.subTest(path=bad):
                self.assertFalse(windows.windows_path(bad))

    def test_a_hint_never_reaches_a_share(self):
        self.assertFalse(windows.windows_path('\\\\fictional-server\\share\\Orchard', share=False))
        looked = []
        self.assertEqual(windows.initial_folder('\\\\fictional-server\\share\\Orchard', isdir=looked.append), '')
        self.assertEqual(looked, [], 'a share hint is never looked at')

    def test_the_hint_opens_its_parent_as_on_the_mac(self):
        self.assertEqual(windows.initial_folder('C:\\Projects\\Orchard', isdir=lambda p: p == 'C:\\Projects'), 'C:\\Projects')
        self.assertEqual(windows.initial_folder('C:\\Projects\\Orchard\\', isdir=lambda p: True), 'C:\\Projects')
        self.assertEqual(windows.initial_folder('C:\\Orchard', isdir=lambda p: True), 'C:\\')
        self.assertEqual(windows.initial_folder('C:\\Projects\\Orchard', isdir=lambda p: False), '')
        self.assertEqual(windows.initial_folder('', isdir=lambda p: True), '')
        self.assertEqual(windows.initial_folder(None, isdir=lambda p: True), '')

        def slow(_):
            raise OSError('fictional unreachable drive')
        self.assertEqual(windows.initial_folder('Z:\\Projects\\Orchard', isdir=slow), '')

    def test_dialog_options(self):
        value = windows.dialog_options(windows.FOS_ALLNONSTORAGEITEMS | 0x10000)
        for flag in (windows.FOS_PICKFOLDERS, windows.FOS_FORCEFILESYSTEM, windows.FOS_PATHMUSTEXIST, windows.FOS_NOCHANGEDIR, 0x10000):
            self.assertTrue(value & flag)
        self.assertFalse(value & windows.FOS_ALLNONSTORAGEITEMS)

    def test_no_command_text_is_ever_built(self):
        source = Path(windows.__file__).read_text(encoding='utf-8')
        for word in ('import subprocess', 'powershell', 'cmd.exe', 'os.system', 'ShellExecute', 'eval(', 'exec('):
            self.assertNotIn(word, source)


class Helper(unittest.TestCase):
    def test_started_then_selected(self):
        dialog = FakeDialog()
        with patch.object(windows, 'initial_folder', return_value='C:\\Projects') as initial:
            frames = run_helper(dialog, {'parent': 4242, 'initial': 'C:\\Projects\\Orchard'})
        initial.assert_called_once_with('C:\\Projects\\Orchard')
        self.assertEqual(frames, [{'state': 'started'}, {'state': 'selected', 'path': 'C:\\Projects\\Orchard'}])
        self.assertEqual(dialog.calls, ['open', ('configure', 'C:\\Projects'), 'show', 'close'])

    def test_cancel_failure_and_a_strange_answer(self):
        self.assertEqual(run_helper(FakeDialog({'state': 'cancelled'})), [{'state': 'started'}, {'state': 'cancelled'}])
        self.assertEqual(run_helper(FakeDialog(fail='open')), [{'state': 'failed'}])
        self.assertEqual(run_helper(FakeDialog(fail='show')), [{'state': 'started'}, {'state': 'failed'}])
        for path in ('\\\\?\\C:\\Projects', '/fictional/Orchard', 'C:\\Pro\njects', 7):
            with self.subTest(path=path):
                self.assertEqual(run_helper(FakeDialog({'state': 'selected', 'path': path})),
                                 [{'state': 'started'}, {'state': 'failed'}])

    def test_another_parent_never_opens_a_window(self):
        dialog, frames = FakeDialog(), []
        windows.helper({'parent': 1, 'initial': ''}, dialog=dialog, emit=frames.append, parent=lambda: 2, alive=lambda: True)
        self.assertEqual((frames, dialog.calls), ([{'state': 'cancelled'}], []))
        windows.helper({'parent': '2'}, dialog=dialog, emit=frames.append, parent=lambda: 2, alive=lambda: True)
        self.assertEqual(dialog.calls, [])

    def test_deadline_reports_cancelled_once_and_ends_the_process(self):
        block, ended = threading.Event(), []

        def exit(code):
            ended.append(code)
            block.set()
        frames = run_helper(FakeDialog(block=block), lifetime=.3, exit=exit)
        self.assertEqual(frames, [{'state': 'started'}, {'state': 'cancelled'}])
        self.assertEqual(ended, [0])

    def test_server_gone_ends_the_process(self):
        block, ended, alive = threading.Event(), [], [True]

        def exit(code):
            ended.append(code)
            block.set()
        dialog = FakeDialog({'state': 'cancelled'}, block=block)
        timer = threading.Timer(.2, lambda: alive.__setitem__(0, False))
        timer.start()
        run_helper(dialog, alive=lambda: alive[0], exit=exit)
        timer.join()
        self.assertEqual(ended[:1], [0])


class ServerSide(unittest.TestCase):
    def test_spawn_is_an_argument_list_with_the_hint_as_data(self):
        calls, spawned = [], []

        def popen(argv, **kw):
            spawned.append((argv, kw))
            return subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.PIPE)
        hint = 'C:\\Users\\demo\\a"b; & %PATH% $(x)'
        operation = chooser.WindowsChooser(popen=popen, env={'PATH': 'C:\\Windows\\System32'}).open(
            hint, before_spawn=lambda: calls.append('checked'))
        operation.cancel()
        operation.process.stdout.close()
        argv, kw = spawned[0]
        self.assertEqual(calls, ['checked'])
        self.assertEqual(argv[:3], [sys.executable, '-I', str(Path(windows.__file__).resolve())])
        self.assertEqual(json.loads(argv[3]), {'parent': os.getpid(), 'initial': hint})
        self.assertEqual(len(argv), 4)
        self.assertIs(kw['shell'], False)
        self.assertEqual((kw['stdin'], kw['stdout'], kw['stderr']), (subprocess.DEVNULL, subprocess.PIPE, subprocess.DEVNULL))
        self.assertEqual(kw['creationflags'], 0x08000000)
        self.assertEqual(kw['env'], {'PATH': 'C:\\Windows\\System32'})

    def test_a_refused_check_spawns_nothing(self):
        spawned = []

        def refuse():
            raise ValueError()
        with self.assertRaises(ValueError):
            chooser.WindowsChooser(popen=lambda *a, **k: spawned.append(a), env={}).open(None, before_spawn=refuse)
        self.assertEqual(spawned, [])

    def test_environment_keeps_only_what_windows_needs(self):
        env = chooser.windows_environment({'SystemRoot': 'C:\\Windows', 'USERPROFILE': 'C:\\Users\\demo',
                                           'PYTHONPATH': 'C:\\fictional', 'Path': 'C:\\fictional\\bin', 'SECRET_TOKEN': 'x'})
        self.assertEqual(env, {'SYSTEMROOT': 'C:\\Windows', 'USERPROFILE': 'C:\\Users\\demo',
                               'PATH': 'C:\\Windows\\System32;C:\\Windows'})

    def frames(self, script, timeout=5):
        process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        starts = []
        try:
            return chooser.read_lines(process, timeout, lambda: starts.append(True)), starts
        finally:
            process.kill()
            process.wait()
            process.stdout.close()

    def test_read_lines_follows_the_frame_rules(self):
        started = 'print(\'{"state": "started"}\', flush=True);'
        self.assertEqual(self.frames(started + 'import json;print(json.dumps({"state": "selected", "path": "C:\\\\Projects\\\\Orchard"}))'),
                         ({'state': 'selected', 'path': 'C:\\Projects\\Orchard'}, [True]))
        self.assertEqual(self.frames('print(\'{"state": "cancelled"}\')'), ({'state': 'cancelled'}, []))
        for script in ('import json;print(json.dumps({"state": "selected", "path": "C:\\\\Projects"}))',      # no start
                       started + 'import json;print(json.dumps({"state": "selected", "path": "/fictional/Orchard"}))',
                       started + 'import json;print(json.dumps({"state": "selected", "path": "\\\\\\\\?\\\\C:\\\\x"}))',
                       started + started, 'print("x" * 20000)', 'print(\'{"state": "failed"}\');print(\'{"state": "failed"}\')',
                       'pass'):
            with self.subTest(script=script):
                with self.assertRaises(ValueError):
                    self.frames(script)
        with self.assertRaises(TimeoutError):
            self.frames('import time;time.sleep(30)', timeout=.3)

    def test_operation_cancel_ends_the_helper_and_result_needs_a_clean_exit(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], stdout=subprocess.PIPE)
        operation = chooser.WindowsOperation(process)
        self.assertFalse(operation.exited())
        self.assertTrue(operation.cancel())
        self.assertTrue(operation.exited())
        process.stdout.close()
        process = subprocess.Popen([sys.executable, '-c', 'import sys;print(\'{"state": "cancelled"}\');sys.exit(3)'],
                                   stdout=subprocess.PIPE)
        with self.assertRaises(ValueError):
            chooser.WindowsOperation(process).result()
        self.assertIsNotNone(process.returncode)

    def test_windows_paths_through_the_coordinator(self):
        class Native:
            window_started = True
            def result(self):
                return {'state': 'selected', 'path': 'C:\\Projects\\Orchard'}
            def cancel(self):
                return True
            def exited(self):
                return True

        class Adapter:
            valid_path = staticmethod(windows.windows_path)
            hints = []
            def open(self, initial_path=None, before_spawn=None):
                before_spawn()
                self.hints.append(initial_path)
                return Native()
        coordinator = chooser.FolderChooser(Adapter(), schedule=False)
        try:
            for hint in ('/fictional/Orchard', '\\\\?\\C:\\Projects'):
                with self.assertRaises(NewChatError):
                    coordinator.begin({'requestId': str(uuid.uuid4()), 'epoch': coordinator.epoch, 'gesture': {'trusted': True},
                                       'initialPath': hint}, Connection(), lambda: None)
            ident = str(uuid.uuid4())
            coordinator.begin({'requestId': ident, 'epoch': coordinator.epoch, 'gesture': {'trusted': True},
                               'initialPath': 'C:\\Projects\\Old'}, Connection(), lambda: None)
            for _ in range(100):
                view = coordinator.status(ident)
                if view['state'] != 'pending':
                    break
                time.sleep(.02)
            self.assertEqual(view['state'], 'selected')
            self.assertEqual(Adapter.hints, ['C:\\Projects\\Old'])
            receipt = {'schemaVersion': 1, 'folder': {'id': 'a' * 64, 'revision': 'b' * 64, 'label': 'Orchard',
                                                      'displayPath': 'C:\\Projects\\Orchard', 'registered': True}}
            got = []
            result = coordinator.accept(ident, {'selectionToken': view['selectionToken']},
                                        lambda value: got.append(value) or receipt, lambda: None)
            self.assertEqual((result['state'], got), ('saved', [{'path': 'C:\\Projects\\Orchard'}]))
        finally:
            coordinator.close()
        self.assertFalse(chooser.FolderChooser(chooser.MacChooser('/fictional')).valid_path('C:\\Projects'),
                         'the Mac window keeps its POSIX rule')
        self.assertIs(chooser.host_chooser(), chooser.WindowsChooser if os.name == 'nt' else chooser.MacChooser)


@unittest.skipUnless(os.name == 'nt', 'The real dialog exists only on Windows.')
class RealWindows(unittest.TestCase):
    def test_the_dialog_is_created_and_configured_but_never_shown(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = os.path.realpath(folder)
            result = {}

            def sta():          # its own thread, so the apartment is this test's alone
                dialog = windows.Dialog()
                try:
                    dialog.open()
                    before = dialog.options()
                    dialog.configure(folder)
                    result['options'] = (before, dialog.options())
                except Exception as error:
                    result['error'] = repr(error)
                finally:
                    dialog.close()
            thread = threading.Thread(target=sta)
            thread.start()
            thread.join(30)
            self.assertNotIn('error', result)
            before, after = result['options']
            self.assertEqual(after, windows.dialog_options(before))
            self.assertTrue(after & windows.FOS_PICKFOLDERS and after & windows.FOS_FORCEFILESYSTEM)

    def test_the_parent_watch_holds_a_handle(self):
        self.assertTrue(windows.parent_alive(os.getpid())())
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        alive = windows.parent_alive(child.pid)
        child.wait()
        self.assertFalse(alive())

    def test_a_real_hint_and_the_system_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            inner = os.path.join(os.path.realpath(folder), 'Orchard')
            os.mkdir(inner)
            self.assertEqual(os.path.normcase(windows.initial_folder(inner)), os.path.normcase(os.path.realpath(folder)))
        env = chooser.windows_environment()
        self.assertTrue(os.path.isdir(env['SYSTEMROOT']))
        self.assertNotIn('PYTHONPATH', env)


if __name__ == '__main__':
    unittest.main()
