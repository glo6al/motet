#!/usr/bin/env python3
"""Managed Claude's control channel (step 8b.3; the feature stays off on Windows until 8b.5).

On every system: the supervisor's serve loop over an endpoint (the seam the Windows named pipe and the POSIX Unix
socket share), with the line protocol answered through socket pairs. On macOS and Linux, under the Windows
simulation: the supervisor records its pipe's name and process ID in its private run folder and removes the record
when it ends, and the web server reads that record and refuses one that does not match. On real Windows only: the
named pipe itself (a round trip, a second server with the same name refused, a client refusing a pipe whose server
is another process, the pipe's owner and access list, and every wait bounded). No Claude is started.

Run: python tests/test_managed_windows_control.py
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
sys.path.insert(0, str(HERE))
import encodings.idna  # noqa: F401  (imported before the simulation, which restores the module table it found)
import private_storage as ps
import claude_managed
import claude_managed_runner as runner_module
import claude_managed_windows as windows
import test_claude_managed_runner as runner_tests

SIMULATED = os.name != 'nt'
WINDOWS = os.name == 'nt'


def fixture(case):
    """The runner suite's run folder, manifest, token and fake transport, for ``case``."""
    base = runner_tests.RunnerTests('test_identity_claim_and_status_precede_first_input')
    base.setUp()
    case.addCleanup(base.tearDown)
    return base


class Client(threading.Thread):
    """A client over one end of a socket pair: sends ``frame``, then reads the reply line."""

    def __init__(self, end, frame):
        super().__init__(daemon=True)
        self.end, self.frame, self.reply = end, frame, None

    def run(self):
        with self.end:
            self.end.settimeout(5)
            self.end.sendall(self.frame)
            data = bytearray()
            while b'\n' not in data:
                chunk = self.end.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
            self.reply = json.loads(bytes(data).split(b'\n', 1)[0]) if data else None


class Endpoint:
    """An endpoint as the serve loop sees one: each accept hands over the next queued connection, then stops."""

    def __init__(self, runner, frames):
        self.runner, self.frames, self.clients, self.accepts = runner, list(frames), [], 0

    def accept(self, seconds):
        self.accepts += 1
        if not self.frames:
            self.runner.stopping = True
            return None
        server, client = socket.socketpair()
        thread = Client(client, self.frames.pop(0))
        thread.start()
        self.clients.append(thread)
        return server


class ServeLoopTests(unittest.TestCase):
    """The protocol through the endpoint seam, one connection at a time, as the supervisor serves it."""

    def setUp(self):
        self.base = fixture(self)

    def frame(self, token=None, command='status', params=None):
        return (json.dumps({'token': token or self.base.token, 'command': command, 'params': params or {}})
                + '\n').encode()

    def test_one_connection_at_a_time_with_the_token_checked(self):
        runner = self.base.runner()
        endpoint = Endpoint(runner, [self.frame(), self.frame(token='b' * 64), b'not json\n'])
        self.assertEqual(runner_module._serve_loop(runner, endpoint), 0)
        for client in endpoint.clients:
            client.join(5)
        replies = [client.reply for client in endpoint.clients]
        self.assertTrue(replies[0]['ok'])
        self.assertEqual(replies[0]['value']['runId'], self.base.manifest['runId'])
        self.assertNotIn(self.base.token, json.dumps(replies))
        self.assertEqual(replies[1]['error']['code'], 'unauthorized')
        self.assertEqual(replies[2]['error']['code'], 'invalid_request')

    def test_the_loop_ends_after_a_recorded_native_exit(self):
        runner = self.base.runner()
        self.base.transport.code = 0
        endpoint = Endpoint(runner, [self.frame()] * 3)
        self.assertEqual(runner_module._serve_loop(runner, endpoint), 0)
        self.assertEqual((endpoint.accepts, len(endpoint.frames)), (0, 3))


class FakePipeServer:
    """A pipe server as the supervisor uses one, answering nothing: what the record says is checked from inside."""
    created = []

    def __init__(self):
        self.name = windows.PIPE_PREFIX + '0123456789abcdef' * 2
        self.closed = False
        FakePipeServer.created.append(self)

    def accept(self, seconds):
        return None

    def close(self):
        self.closed = True


@unittest.skipUnless(SIMULATED, 'simulates Windows on macOS and Linux')
class SimulatedControlRecordTests(unittest.TestCase):
    def setUp(self):
        from windows_simulation import simulated_windows
        stack = ExitStack()
        self.calls = stack.enter_context(simulated_windows())
        self.addCleanup(stack.close)
        self.base = fixture(self)
        with ps.open_folder(str(self.base.project)) as held:
            device, inode = held.identity()[:2]
        self.base.manifest['folder'].update(device=device, inode=inode)
        self.base.write('manifest.json', json.dumps(self.base.manifest))

    def test_the_supervisor_records_its_pipe_privately_then_removes_the_record(self):
        transport = runner_tests.FakeTransport(self.base.manifest, self.base.run)
        runner = runner_module.Runner(self.base.run, transport_factory=lambda m: transport)
        self.base.runners.append(runner)
        seen = []

        class Recording(FakePipeServer):
            def accept(inner, seconds):
                record = self.base.run / runner_module.CONTROL_FILE
                ps.check_private(str(record), single_link=True)
                seen.append(json.loads(record.read_bytes()))
                runner.stopping = True
                return None
        with mock.patch.object(windows, 'PipeServer', Recording), \
                mock.patch.object(runner_module, 'Runner', return_value=runner):
            self.assertEqual(runner_module.serve(self.base.run), 0)
        self.assertEqual(seen, [{'pipe': windows.PIPE_PREFIX + '0123456789abcdef' * 2, 'pid': os.getpid()}])
        self.assertFalse((self.base.run / runner_module.CONTROL_FILE).exists())
        self.assertFalse((self.base.run / 'control.sock').exists())       # no Unix socket on Windows

    def write_record(self, value):
        fd = ps.create_private_file(str(self.base.run / runner_module.CONTROL_FILE))
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def test_the_web_server_reads_the_record_and_sends_the_token_only_over_that_pipe(self):
        name = windows.PIPE_PREFIX + 'f' * 32
        self.write_record({'pipe': name, 'pid': 4321})
        opened, sent = [], []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendall(self, data):
                sent.append(data)

            def recv(self, size):
                return b'{"ok":true,"value":{"state":"ready"}}\n'

        def open_pipe(pipe, pid):
            opened.append((pipe, pid))
            return Connection()
        with mock.patch.object(windows, 'open_pipe', open_pipe):
            value = claude_managed.client_request(self.base.run, 'status', {})
        self.assertEqual(value, {'state': 'ready'})
        self.assertEqual(opened, [(name, 4321)])
        self.assertEqual(json.loads(sent[0]), {'token': self.base.token, 'command': 'status', 'params': {}})

    def test_a_refused_pipe_never_gets_the_token(self):
        self.write_record({'pipe': windows.PIPE_PREFIX + 'f' * 32, 'pid': 4321})
        with mock.patch.object(windows, 'open_pipe', side_effect=OSError('not the recorded supervisor')):
            with self.assertRaises(OSError):
                claude_managed.client_request(self.base.run, 'status', {})

    def test_a_malformed_or_readable_record_is_refused(self):
        for value in ({'pipe': windows.PIPE_PREFIX + 'f' * 32}, {'pipe': 'x', 'pid': 1, 'extra': 2}, b'[]'):
            with self.subTest(value=value):
                record = self.base.run / runner_module.CONTROL_FILE
                if record.exists():
                    record.unlink()
                self.write_record(value)
                with mock.patch.object(windows, 'open_pipe', side_effect=AssertionError('opened')):
                    with self.assertRaises((claude_managed.ManagedError, ValueError)):
                        claude_managed.client_request(self.base.run, 'status', {})
        (self.base.run / runner_module.CONTROL_FILE).chmod(0o644)
        with mock.patch.object(windows, 'open_pipe', side_effect=AssertionError('opened')):
            with self.assertRaises((claude_managed.ManagedError, OSError)):
                claude_managed.client_request(self.base.run, 'status', {})


@unittest.skipUnless(WINDOWS, 'Windows named pipes')
class WindowsPipeTests(unittest.TestCase):
    def server(self, answer=True):
        """A pipe server in a thread: one connection, echoing the request line as the reply (or never answering)."""
        server = windows.PipeServer()
        done = threading.Event()

        def serve():
            try:
                deadline = time.monotonic() + 20
                while not done.is_set() and time.monotonic() < deadline:
                    connection = server.accept(.05)
                    if connection is None:
                        continue
                    with connection:
                        data = bytearray()
                        while b'\n' not in data:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            data.extend(chunk)
                        if answer:
                            connection.sendall(bytes(data))
                        else:
                            done.wait(10)
                    return
            finally:
                server.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def cleanup():
            done.set()
            thread.join(30)
        self.addCleanup(cleanup)
        return server, thread

    def test_round_trip(self):
        server, thread = self.server()
        with windows.open_pipe(server.name, os.getpid()) as connection:
            connection.sendall(b'{"fictional": true}\n')
            reply = bytearray()
            while b'\n' not in reply:
                chunk = connection.recv(8192)
                self.assertTrue(chunk)
                reply.extend(chunk)
        self.assertEqual(bytes(reply), b'{"fictional": true}\n')

    def test_a_second_server_with_the_same_name_is_refused(self):
        server, _ = self.server()
        with self.assertRaises(OSError):
            windows.PipeServer(server.name)

    def test_a_client_refuses_a_pipe_whose_server_is_another_process(self):
        server, _ = self.server()
        with self.assertRaises(OSError):
            windows.open_pipe(server.name, os.getpid() + 4)

    def test_the_pipe_names_only_this_user_and_system(self):
        server = windows.PipeServer()
        try:
            ps.windows_handle_security(server.handle)       # owner this user; protected list: this user, SYSTEM
        finally:
            server.close()

    def test_a_missing_or_foreign_name_is_refused_plainly(self):
        with self.assertRaises(FileNotFoundError):
            windows.open_pipe(windows.PIPE_PREFIX + '0' * 32, os.getpid())
        for name in ('\\\\server\\pipe\\motet-' + '0' * 32, 'C:\\motet-' + '0' * 32, windows.PIPE_PREFIX + 'x'):
            with self.assertRaises(OSError):
                windows.open_pipe(name, os.getpid())

    def test_a_server_that_never_answers_times_out(self):
        server, _ = self.server(answer=False)
        with windows.open_pipe(server.name, os.getpid()) as connection:
            connection.sendall(b'{"fictional": true}\n')
            started = time.monotonic()
            with self.assertRaises(socket.timeout):
                connection.recv(8192)
            self.assertLess(time.monotonic() - started, 10)

    def test_a_client_that_never_reads_bounds_the_servers_write(self):
        server = windows.PipeServer()
        self.addCleanup(server.close)
        client = windows.open_pipe(server.name, os.getpid())
        self.addCleanup(client.close)
        connection = None
        deadline = time.monotonic() + 10
        while connection is None and time.monotonic() < deadline:
            connection = server.accept(.05)
        self.assertIsNotNone(connection)
        connection.settimeout(2)
        started = time.monotonic()
        with self.assertRaises(socket.timeout):
            connection.sendall(b'x' * (4 * windows.PIPE_BUFFER))
        self.assertLess(time.monotonic() - started, 10)

    def test_an_accept_with_no_client_returns_in_time(self):
        server = windows.PipeServer()
        self.addCleanup(server.close)
        started = time.monotonic()
        self.assertIsNone(server.accept(.2))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()
