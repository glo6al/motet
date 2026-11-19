"""Optional-worker checks use only stock Python and temporary fake processes."""
import ast
import base64
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'integrations' / 'dictation'
sys.path.insert(0, str(MODULES))
from runtime import Runtime, RuntimeUnavailable
import worker

READY = "print(json.dumps({'id':0,'ready':True,'loadMs':1.25}),flush=True)\n"
GOOD = "{'id':request['id'],'ok':True,'epoch':1,'events':[],'commandMs':.2}"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.worker = Path(self.directory.name) / 'fake_worker.py'

    def runtime(self, body, timeout=2):
        self.worker.write_text('import sys,json,time,os\n' + textwrap.dedent(body), encoding='utf-8')
        value = Runtime(sys.executable, Path(self.directory.name) / 'model', self.worker, timeout)
        self.addCleanup(value.close)
        return value

    def test_constructor_is_inert_and_source_is_python39(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('process on import')):
            Runtime('not-installed-python', 'not-installed-model')
        for name in ('runtime.py', 'worker.py'):
            ast.parse((MODULES / name).read_text(), feature_version=(3, 9))

    def test_normal_protocol_and_safe_environment(self):
        body = READY + """
sys.stderr.write('discarded native diagnostics\\n')
sys.stderr.flush()
for raw in sys.stdin:
    request=json.loads(raw)
    if any(k in os.environ for k in ('DICTATION_TEST_SECRET','HTTPS_PROXY','PYTHONPATH')):
        print(json.dumps({'id':request['id'],'ok':False,'code':'protocol_error'}),flush=True)
    else:
        print(json.dumps(RESULT),flush=True)
""".replace('RESULT', GOOD)
        value = self.runtime(body)
        with mock.patch.dict(os.environ, {'DICTATION_TEST_SECRET': 'fictional-secret',
                                         'HTTPS_PROXY': 'http://fictional.invalid', 'PYTHONPATH': '/fictional'}):
            value.start()
        self.assertTrue(value.ready)
        self.assertEqual(value.load_ms, 1.25)
        self.assertEqual(value.call('start', interval=.25), {'epoch': 1, 'events': [], 'commandMs': .2})
        self.assertEqual(value.call('cancel')['events'], [])

    def test_native_stdout_is_chunk_buffered_without_changing_stdin(self):
        value = self.runtime(READY + 'time.sleep(1)\n')
        value.start()
        self.assertIsInstance(value._process.stdout, io.BufferedReader)
        self.assertIsInstance(value._process.stdin, io.FileIO)
        class CountingRaw(io.RawIOBase):
            def __init__(self): self.remaining = 100000; self.reads = 0
            def readable(self): return True
            def readinto(self, buffer):
                self.reads += 1
                count = min(len(buffer), self.remaining)
                buffer[:count] = b'x' * count
                self.remaining -= count
                return count
        raw = CountingRaw()
        with io.BufferedReader(raw) as buffered:
            self.assertEqual(len(buffered.readline(100000)), 100000)
            self.assertLess(raw.reads, 20)

    def test_missing_interpreter_is_safe_dependency_error(self):
        value = Runtime(Path(self.directory.name) / 'absent', 'model', timeout=.1)
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.start()
        self.assertEqual(caught.exception.code, 'dependency_missing')
        self.assertNotIn(self.directory.name, str(caught.exception))

    def test_model_loading_has_its_own_bounded_deadline(self):
        value=self.runtime('time.sleep(.12)\n'+READY+'for raw in sys.stdin:\n request=json.loads(raw)\n print(json.dumps('+GOOD+'),flush=True)\n')
        value.timeout=2;value.load_timeout=5
        deadlines = []
        receive = value._receive
        def observed(request_id, deadline):
            deadlines.append(deadline - time.monotonic())
            return receive(request_id, deadline)
        with mock.patch.object(value, '_receive', side_effect=observed):
            value.start();self.assertTrue(value.ready)
            self.assertEqual(value.call('start',interval=.25)['epoch'],1)
        self.assertGreater(deadlines[0], 4, 'loading uses its own five-second budget')
        self.assertLess(deadlines[1], 3, 'commands use the separate two-second budget')
        self.assertIsInstance(value._process.stdout, io.BufferedReader)
        value.close()
        value=self.runtime('time.sleep(2)\n'+READY)
        value.load_timeout=.05
        began=time.monotonic()
        with self.assertRaises(RuntimeUnavailable) as caught:value.start()
        self.assertEqual(caught.exception.code,'worker_timeout')
        self.assertLess(time.monotonic()-began,1)
        self.assertFalse(value.ready)

    def test_startup_failure_keeps_safe_code_even_when_worker_exits(self):
        value = self.runtime("print(json.dumps({'id':0,'ready':False,'code':'dependency_missing'}),flush=True)\n")
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.start()
        self.assertEqual(caught.exception.code, 'dependency_missing')
        self.assertFalse(value.ready)

    def test_native_errors_never_republish_worker_details(self):
        value = self.runtime("print(json.dumps({'id':0,'ready':False,'code':'model_error','error':'fictional private detail'}),flush=True)\n")
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.start()
        self.assertEqual(caught.exception.code, 'model_error')
        self.assertNotIn('private detail', str(caught.exception))

    def test_response_ids_are_checked_and_child_retired(self):
        body = READY + """
for raw in sys.stdin:
    request=json.loads(raw)
    print(json.dumps({'id':request['id']+1,'ok':True,'epoch':1,'events':[],'commandMs':0}),flush=True)
"""
        value = self.runtime(body)
        value.start()
        process = value._process
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'protocol_error')
        self.assertFalse(value.ready)
        self.assertIsNotNone(process.poll())
        with self.assertRaises(RuntimeUnavailable):
            value.call('start', interval=.25)

    def test_timed_out_generation_cannot_supply_next_response(self):
        body = READY + """
import signal
signal.signal(signal.SIGTERM, lambda *args:None)
request=json.loads(sys.stdin.readline())
time.sleep(.2)
print(json.dumps({'id':request['id'],'ok':True,'epoch':99,'events':[],'commandMs':0}),flush=True)
"""
        value = self.runtime(body)
        value.start()
        value.timeout = .05
        began = time.monotonic()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'worker_timeout')
        self.assertLess(time.monotonic() - began, 1.5)
        self.worker.write_text('import sys,json,time,os\n' + READY + "for raw in sys.stdin:\n request=json.loads(raw)\n print(json.dumps(" + GOOD + "),flush=True)\n")
        value.timeout = 2
        value.start()
        self.assertEqual(value.call('start', interval=.25)['epoch'], 1)

    def test_deadline_also_covers_blocked_stdin_write(self):
        value = self.runtime(READY + 'time.sleep(5)\n')
        value.start()
        value.timeout = .08
        began = time.monotonic()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('audio', pcm=base64.b64encode(b'\0' * 64000).decode())
        self.assertEqual(caught.exception.code, 'worker_timeout')
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertFalse(value.ready)

    def test_oversized_response_is_rejected(self):
        value = self.runtime(READY + "sys.stdin.readline()\nprint('x'*(1024*1024+1),flush=True)\ntime.sleep(1)\n")
        value.timeout = 10
        value.start()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'protocol_error')

    def test_duplicate_and_nonfinite_protocol_values_are_rejected(self):
        for raw in ('{"id":0,"id":0,"ready":true,"loadMs":1}',
                    '{"id":0,"ready":true,"loadMs":NaN}', '[]'):
            with self.subTest(raw=raw):
                value = self.runtime('print(' + repr(raw) + ',flush=True)\ntime.sleep(1)\n')
                with self.assertRaises(RuntimeUnavailable) as caught:
                    value.start()
                self.assertEqual(caught.exception.code, 'protocol_error')

    def test_bad_event_shape_retires_child(self):
        body = READY + """
for raw in sys.stdin:
    request=json.loads(raw)
    print(json.dumps({'id':request['id'],'ok':True,'epoch':1,'events':[{'type':'line','text':'hello'}],'commandMs':0}),flush=True)
"""
        value = self.runtime(body)
        value.timeout = 10
        value.start()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'protocol_error')
        self.assertFalse(value.ready)

    def test_nonstring_error_code_is_a_protocol_error(self):
        body = READY + """
for raw in sys.stdin:
    request=json.loads(raw)
    print(json.dumps({'id':request['id'],'ok':False,'code':[]}),flush=True)
"""
        value = self.runtime(body)
        value.timeout = 10
        value.start()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'protocol_error')
        self.assertFalse(value.ready)

    def test_safe_worker_error_retires_stream(self):
        body = READY + """
for raw in sys.stdin:
    request=json.loads(raw)
    print(json.dumps({'id':request['id'],'ok':False,'code':'recognition_failed'}),flush=True)
"""
        value = self.runtime(body)
        value.timeout = 10
        value.start()
        with self.assertRaises(RuntimeUnavailable) as caught:
            value.call('stop')
        self.assertEqual(caught.exception.code, 'recognition_failed')
        self.assertFalse(value.ready)

    def test_close_is_bounded_and_idempotent(self):
        value = self.runtime(READY + 'sys.stdin.read()\n')
        value.start()
        process = value._process
        value.close()
        value.close()
        self.assertFalse(value.ready)
        self.assertIsNotNone(process.poll())


class Changed:
    def __init__(self, text='testing words'):
        self.line = SimpleNamespace(text=text, line_id=17)


class Completed(Changed):
    pass


class NativeError:
    pass


class FakeStream:
    def __init__(self):
        self.listeners = []
        self.stops = 0
        self.closed = False
        self.fail_audio = False
        self.fail_stop = False

    def add_listener(self, callback):
        self.listeners.append(callback)

    def start(self):
        pass

    def emit(self, event):
        for callback in self.listeners:
            try:
                callback(event)
            except Exception:
                pass  # Matches the binding's swallowed-callback behavior.

    def add_audio(self, samples, sample_rate):
        if self.fail_audio:
            self.emit(NativeError())
        else:
            self.emit(Changed())

    def stop(self):
        self.stops += 1
        self.emit(NativeError() if self.fail_stop else Completed('final words'))

    def remove_all_listeners(self):
        self.listeners.clear()

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self):
        self.streams = []

    def create_stream(self, update_interval):
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def close(self):
        pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.transcriber = FakeTranscriber()
        self.engine = worker.RecognitionEngine(self.transcriber, Changed, Completed, NativeError)
        self.addCleanup(self.engine.close)
        self.engine.handle({'id': 1, 'action': 'start', 'interval': .25})

    def audio(self):
        return {'id': 2, 'action': 'audio', 'pcm': base64.b64encode(b'\0' * 6400).decode()}

    def test_partials_and_final_lines_are_distinct(self):
        partial = self.engine.handle(self.audio())
        self.assertEqual(partial['events'][0]['text'], 'testing words')
        self.assertFalse(partial['events'][0]['complete'])
        self.assertAlmostEqual(partial['events'][0]['audioSeconds'], .1)
        final = self.engine.handle({'id': 3, 'action': 'stop'})
        self.assertTrue(final['events'][0]['complete'])
        self.assertEqual(final['events'][0]['text'], 'final words')
        self.assertIsNone(self.engine.stream)

    def test_cancel_does_not_finalize_and_discards_stream(self):
        stream = self.engine.stream
        result = self.engine.handle({'id': 3, 'action': 'cancel'})
        self.assertEqual(result['events'], [])
        self.assertEqual(stream.stops, 0)
        self.assertTrue(stream.closed)

    def test_reset_retires_prior_callbacks_even_across_new_take(self):
        stream = self.engine.stream
        callback = stream.listeners[0]
        self.engine.handle(self.audio())
        result = self.engine.handle({'id': 3, 'action': 'reset'})
        self.assertEqual(result['epoch'], 2)
        self.assertEqual(result['events'], [])
        self.assertEqual(stream.stops, 0)
        callback(Changed('retired text'))
        self.assertEqual(self.engine.events, [])
        self.engine.handle({'id': 4, 'action': 'start', 'interval': .5})
        callback(Changed('also retired when epoch returns to one'))
        self.assertEqual(self.engine.events, [])
        self.assertEqual(self.engine.handle(self.audio())['events'][0]['epoch'], 1)

    def test_swallowed_error_callbacks_fail_audio_and_stop(self):
        for action in ('audio', 'stop'):
            self.engine.handle({'id': 1, 'action': 'start', 'interval': .25})
            setattr(self.engine.stream, 'fail_' + action, True)
            with self.assertRaises(worker.WorkerError) as caught:
                self.engine.handle(self.audio() if action == 'audio' else {'id': 3, 'action': 'stop'})
            self.assertEqual(caught.exception.code, 'recognition_failed')
            self.assertIsNone(self.engine.stream)

    def test_callback_shape_error_is_not_swallowed(self):
        self.engine.stream.add_audio = lambda *args, **kwargs: self.engine.stream.emit(Changed(None))
        with self.assertRaises(worker.WorkerError) as caught:
            self.engine.handle(self.audio())
        self.assertEqual(caught.exception.code, 'recognition_failed')

    def test_invalid_pcm_is_rejected_without_native_processing(self):
        for raw in (b'', b'x', b'\0' * 64004, struct.pack('<f', float('nan')),
                    struct.pack('<f', float('inf')), struct.pack('<f', 1.001)):
            with self.subTest(size=len(raw)):
                self.engine.handle({'id': 1, 'action': 'start', 'interval': .25})
                request = {'id': 2, 'action': 'audio', 'pcm': base64.b64encode(raw).decode()}
                with self.assertRaises(worker.WorkerError) as caught:
                    self.engine.handle(request)
                self.assertEqual(caught.exception.code, 'invalid_request')

    def test_strict_requests_reject_duplicates_and_extra_fields(self):
        for raw in (b'{"id":1,"id":2,"action":"stop"}', b'{"id":true,"action":"stop"}',
                    b'{"id":NaN,"action":"stop"}', b'null'):
            with self.assertRaises(worker.WorkerError):
                worker.parse_request(raw)
        for request in ({'id': 1, 'action': 'start', 'interval': True},
                        {'id': 1, 'action': 'start', 'interval': .25, 'other': 2},
                        {'id': 1, 'action': 'cancel', 'other': 2}, {'id': 1, 'action': 'send'}):
            with self.assertRaises(worker.WorkerError):
                worker.validate_request(request)

    def test_runtime_version_checked_before_optional_import(self):
        with mock.patch.object(worker.sys, 'version_info', (3, 9, 99)):
            with mock.patch.object(importlib.metadata, 'version', side_effect=AssertionError('Version probe on unsupported Python')):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.load_engine(Path('unused'))
        self.assertEqual(caught.exception.code, 'runtime_version')
        with mock.patch.object(worker.sys, 'version_info', (3, 10, 0)):
            with mock.patch.object(importlib.metadata, 'version', return_value='unsupported'):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.load_engine(Path('unused'))
            self.assertEqual(caught.exception.code, 'runtime_version')
            with mock.patch.object(importlib.metadata, 'version', side_effect=importlib.metadata.PackageNotFoundError):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.load_engine(Path('unused'))
            self.assertEqual(caught.exception.code, 'dependency_missing')


if __name__ == '__main__':
    unittest.main()
