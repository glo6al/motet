"""Optional helper boundary and in-memory sessions; no native runtime or microphone."""
import base64
from email.message import Message
import http.client
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.dictation.helper import HelperServer, main, probe
from integrations.dictation.service import DictationService, ServiceError
from integrations.dictation.state import PrivateState, StateError


class Engine:
    load_ms = 1
    ready = True

    def __init__(self):
        self.calls = []
        self.epoch = 1
        self.failure = False
        self.closed = False

    def start(self):
        pass

    def call(self, action, **payload):
        self.calls.append((action, payload))
        if self.failure:
            raise RuntimeError('fictional private diagnostic must not escape')
        events = []
        if action == 'start':
            self.epoch = 1
        if action == 'reset':
            self.epoch += 1
            events = [{'type': 'line', 'epoch': self.epoch - 1, 'lineId': 'old',
                       'text': 'retired text', 'complete': True}]
        if action in ('audio', 'stop'):
            events = [{'type': 'line', 'epoch': self.epoch, 'lineId': 'line-1',
                       'text': 'A local draft.', 'complete': action == 'stop', 'decodeMs': 1}]
        return {'epoch': self.epoch, 'events': events, 'commandMs': 1}

    def close(self):
        self.closed = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.now = 100
        self.service = DictationService(lambda: self.engine, lambda: None, clock=lambda: self.now)
        self.service.load()
        self.service.loading_thread.join(2)
        self.assertEqual(self.service.status()['state'], 'ready')

    def tearDown(self):
        self.service.close()

    def test_partial_final_events_and_exact_audio_level(self):
        session = self.service.start(.25)['sessionId']
        packet = struct.pack('<ff', .5, -.5)
        result = self.service.audio(session, 0, packet)
        self.assertEqual(result['level'], {'peak': .5, 'rms': .5})
        self.assertEqual(base64.b64decode(self.engine.calls[-1][1]['pcm']), packet)
        events = self.service.events(session, 0)['events']
        self.assertFalse(events[-1]['complete'])
        self.service.control(session, 'stop')
        ended = self.service.events(session, events[-1]['id'])
        self.assertTrue(ended['ended'])
        self.assertEqual([event['type'] for event in ended['events']], ['line', 'ended'])
        self.assertTrue(ended['events'][0]['complete'])

    def test_duplicate_start_late_audio_and_wrong_sequence_rejected(self):
        ident = self.service.start(.5)['sessionId']
        for action in (lambda: self.service.start(.25), lambda: self.service.audio(ident, 1, b'\0' * 4)):
            with self.assertRaises(ServiceError) as result:
                action()
            self.assertEqual(result.exception.status, 409)
        self.service.control(ident, 'cancel')
        with self.assertRaises(ServiceError):
            self.service.audio(ident, 0, b'\0' * 4)
        self.assertNotIn('stop', [action for action, _ in self.engine.calls])

    def test_reset_rejects_retired_epoch_and_keeps_audio_sequence(self):
        ident = self.service.start(.25)['sessionId']
        self.service.audio(ident, 0, b'\0' * 4)
        result = self.service.control(ident, 'reset')
        self.assertEqual(result['epoch'], 2)
        self.service.audio(ident, 1, b'\0' * 4)
        events = self.service.events(ident, 0)['events']
        self.assertFalse(any(event.get('text') == 'retired text' for event in events))
        self.assertEqual(events[-1]['epoch'], 2)

    def test_worker_failure_is_generic_and_retires_take(self):
        ident = self.service.start(.25)['sessionId']
        self.engine.failure = True
        with self.assertRaises(ServiceError) as result:
            self.service.control(ident, 'stop')
        self.assertNotIn('private diagnostic', str(result.exception))
        self.assertEqual(self.service.status()['state'], 'failed')
        self.assertTrue(self.engine.closed)
        events = self.service.events(ident, 0)
        self.assertTrue(events['ended'])
        self.assertEqual(events['events'][0]['code'], 'worker_failed')

    def test_pcm_validation_before_runtime(self):
        ident = self.service.start(.25)['sessionId']
        before = len(self.engine.calls)
        for data in (b'', b'x', b'\0' * 64004, struct.pack('<f', float('nan')),
                     struct.pack('<f', float('inf')), struct.pack('<f', 1.1)):
            with self.assertRaises(ServiceError):
                self.service.audio(ident, 0, data)
        self.assertEqual(len(self.engine.calls), before)

    def test_disconnected_take_and_retained_events_expire(self):
        ident = self.service.start(.25)['sessionId']
        self.now += 21
        self.service.expire()
        events = self.service.events(ident, 0)
        self.assertTrue(events['ended'])
        self.assertEqual(events['events'][0]['code'], 'disconnected')
        self.now += 61
        self.service.expire()
        with self.assertRaises(ServiceError):
            self.service.events(ident, 0)

    def test_take_limit_closes_stream(self):
        ident = self.service.start(.25)['sessionId']
        self.now += 181
        with self.assertRaises(ServiceError) as error:
            self.service.audio(ident, 0, b'\0' * 4)
        self.assertEqual(error.exception.code, 'take_limit')
        self.assertTrue(self.service.events(ident, 0)['ended'])

    def test_polling_cannot_extend_take_deadline(self):
        ident = self.service.start(.25)['sessionId']
        self.service.audio(ident, 0, b'\0' * 4)
        self.now += 181
        self.service.events(ident, 0)  # A polling heartbeat refreshes inactivity only.
        self.service.expire()
        self.assertTrue(self.service.events(ident, 0)['ended'])
        self.assertEqual(self.engine.calls[-1][0], 'cancel')

    def test_shutdown_rejects_a_successful_late_native_result(self):
        ident = self.service.start(.25)['sessionId']
        entered, release = threading.Event(), threading.Event()
        original = self.engine.call
        def delayed(action, **payload):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(action, **payload)
        self.engine.call = delayed
        errors = []
        def audio():
            try:
                self.service.audio(ident, 0, b'\0' * 4)
            except ServiceError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=audio)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.service.close()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['ended'])
        self.assertEqual([event['type'] for event in self.service.events(ident, 0)['events']], ['ended'])
        self.assertEqual(self.service.status()['state'], 'not_running')

    def test_event_buffer_has_explicit_loss_signal(self):
        ident = self.service.start(.25)['sessionId']
        for sequence in range(513):
            self.service.audio(ident, sequence, b'\0' * 4)
        self.assertLessEqual(len(self.service.sessions[ident]['events']), 512)
        with self.assertRaises(ServiceError) as error:
            self.service.events(ident, 0)
        self.assertEqual(error.exception.code, 'events_expired')

    def test_concurrent_command_is_rejected_instead_of_queued(self):
        ident = self.service.start(.25)['sessionId']
        with self.service.command:
            with self.assertRaises(ServiceError) as error:
                self.service.control(ident, 'cancel')
        self.assertEqual(error.exception.code, 'busy')

    def test_missing_runtime_status_and_load_do_not_invoke_factory(self):
        factory = mock.Mock(side_effect=AssertionError('optional runtime was started'))
        service = DictationService(factory, lambda: 'dependency_missing')
        service.load()
        service.loading_thread.join(2)
        self.assertEqual(service.status()['state'], 'not_installed')
        factory.assert_not_called()
        service.close()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.service = DictationService(lambda: self.engine, lambda: None)
        self.service.load()
        self.service.loading_thread.join(2)
        self.key = 'a' * 64  # Fictional capability, never loaded from a user's home.
        self.server = HelperServer(0, self.key, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.assertNotEqual(self.port, 8793)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path='/v1/status', data=None, method=None, headers=(), key=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        verb = method or ('POST' if data is not None else 'GET')
        connection.putrequest(verb, path, skip_host=True)
        connection.putheader('Host', '127.0.0.1:%d' % self.port)
        if key:
            connection.putheader('X-Dictation-Key', self.key)
        if data is not None:
            connection.putheader('Content-Length', str(len(data)))
            connection.putheader('Content-Type', 'application/json')
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(data)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        self.assertIsNone(response.getheader('Access-Control-Allow-Origin'))
        connection.close()
        return result

    def test_all_routes_need_capability_and_no_read_before_auth(self):
        for path in ('/v1/status', '/v1/start', '/v1/audio', '/v1/events', '/v1/stop', '/v1/cancel', '/v1/reset'):
            self.assertEqual(self.request(path, key=False)[0], 403)
        self.assertEqual(self.engine.calls, [])
        # Announce a body but never send it: auth must reject without waiting for it.
        self.assertEqual(self.request('/v1/start', method='POST', key=False,
                                      headers=[('Content-Length', '64000')])[0], 403)

    def test_duplicate_sensitive_headers_and_hostile_origin(self):
        for header in [('Host', 'localhost:%d' % self.port), ('X-Dictation-Key', self.key),
                       ('Origin', 'https://fictional.invalid'), ('Sec-Fetch-Site', 'cross-site')]:
            self.assertEqual(self.request(headers=[header])[0], 403)
        own = 'http://127.0.0.1:%d' % self.port
        self.assertEqual(self.request(headers=[('Origin', own), ('Origin', own)])[0], 403)
        self.assertEqual(self.request(headers=[('Origin', own)])[0], 200)

    def test_strict_json_routes_and_framing(self):
        for raw in (b'null', b'[]', b'{"intervalSeconds":NaN}', b'{"intervalSeconds":1e999}',
                    b'{"intervalSeconds":.25}', b'{"x":1,"x":2}', b'{"unexpected":1}'):
            self.assertEqual(self.request('/v1/start', raw)[0], 400)
        for path in ('/v1/status?x=1', '/v1/status?', '/v1/%73tatus', 'http://localhost/v1/status', '/v1/status#x'):
            self.assertEqual(self.request(path)[0], 400)
        for headers in ([('Content-Length', '2')], [('Transfer-Encoding', 'chunked')], [('Content-Type', 'text/plain')]):
            self.assertEqual(self.request('/v1/start', b'{}', headers=headers)[0], 400)
        self.assertEqual(self.request('/v1/start', b'x' * 4097)[0], 413)

    def test_stop_and_events_flow_without_any_messaging_route(self):
        code, data = self.request('/v1/start', {'intervalSeconds': .25})
        self.assertEqual(code, 200)
        ident = data['sessionId']
        self.assertEqual(self.request('/v1/reset', {'sessionId': ident})[1]['epoch'], 2)
        self.assertEqual(self.request('/v1/stop', {'sessionId': ident})[0], 200)
        code, events = self.request('/v1/events', {'sessionId': ident, 'after': 0})
        self.assertTrue(events['ended'])
        self.assertTrue(any(e.get('complete') for e in events['events']))
        for route in ('/ask', '/send', '/api/start', '/', '/v1/model-download'):
            self.assertEqual(self.request(route)[0], 404)

    def test_get_bodies_and_future_cursors_are_rejected(self):
        self.assertEqual(self.request(headers=[('Content-Length', '1')])[0], 400)
        ident = self.request('/v1/start', {})[1]['sessionId']
        self.assertEqual(self.request('/v1/events', {'sessionId': ident, 'after': 123})[0], 409)

    def test_core_port_is_refused_before_binding(self):
        with self.assertRaises(ValueError):
            HelperServer(8793, self.key, self.service)


@unittest.skipUnless(os.name == 'posix', 'Private native helper state requires POSIX')
class StateTests(unittest.TestCase):
    def test_empty_home_creates_private_dedicated_key_and_probe_maps_absent_service(self):
        with tempfile.TemporaryDirectory() as home:
            state = PrivateState(home)
            key = state.key(create=True)
            self.assertEqual(len(key), 64)
            self.assertEqual(state.key(), key)
            self.assertEqual(state.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state.root / 'header').stat().st_mode & 0o777, 0o600)
            self.assertEqual(probe(home, 1)['state'], 'not_running')
            self.assertEqual([p.name for p in state.root.iterdir()], ['header'])

    def test_status_without_state_creates_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(probe(home, 1)['state'], 'not_installed')
            self.assertEqual(list(Path(home).iterdir()), [])

    def test_symlink_public_and_hardlinked_credentials_are_refused(self):
        with tempfile.TemporaryDirectory() as home:
            state = PrivateState(home)
            state.key(create=True)
            header = state.root / 'header'
            saved = state.root / 'saved'
            header.rename(saved)
            header.symlink_to(saved)
            with self.assertRaises(StateError):
                state.key()
            header.unlink()
            saved.rename(header)
            os.chmod(header, 0o644)
            with self.assertRaises(StateError):
                state.key()
            os.chmod(header, 0o600)
            os.link(header, state.root / 'linked')
            with self.assertRaises(StateError):
                state.key()

    def test_linked_state_directory_does_not_create_outside_state(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as elsewhere:
            (Path(home) / '.live-room').symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(StateError):
                PrivateState(home).key(create=True)
            self.assertEqual(list(Path(elsewhere).iterdir()), [])


class IsolationTests(unittest.TestCase):
    def test_demo_and_import_do_not_load_optional_runtime_or_touch_home(self):
        source = ("import sys, tempfile; from pathlib import Path; "
                  "from integrations.dictation.helper import main; "
                  "assert 'moonshine_voice' not in sys.modules; "
                  "d=tempfile.TemporaryDirectory(); "
                  "assert main(['serve','--home',d.name,'--demo']) == 0; "
                  "assert not list(Path(d.name).iterdir()); d.cleanup()")
        completed = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['state'], 'disabled')


if __name__ == '__main__':
    unittest.main()
