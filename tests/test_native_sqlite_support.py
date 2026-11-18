"""Real local startup refusal, retained protection and numeric listener checks."""
from contextlib import ExitStack, closing
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_owner_marker import (CLEAR, MARKER, UNCERTAIN, SQLITE_UNAVAILABLE_MESSAGE,
                                      lexical_payload_state, sqlite_marker_available)
import attachments_owner_guards as guards
import server


class SemanticChecks(unittest.TestCase):
    def test_real_engine_capability_and_atomic_guard_refusal(self):
        from test_attachments_owner_transport import owner_schema
        with closing(sqlite3.connect(':memory:')) as db:
            supported = sqlite_marker_available(db)
            try:
                key = db.execute('SELECT CAST(key AS BLOB) FROM json_each(?)',
                                 (r'{"attachment_owner_version\u0000":null}',)).fetchone()[0]
            except sqlite3.OperationalError:
                key = None
            if supported:
                self.assertEqual(key, b'attachment_owner_version\0')
            else:
                self.assertNotEqual(key, b'attachment_owner_version\0')
                owner_schema(db)
                before = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
                with self.assertRaisesRegex(guards.OwnerGuardError, 'JSON checks are unavailable'):
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        guards.OwnerAttachmentGuard.install(db, threading.RLock())
                self.assertEqual(db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(), before)

    def test_lexical_fallback_covers_all_bytes_without_sql_support(self):
        from test_attachments_owner_marker import reference
        for template in (b'{"question":"a%sZ"}', b'{"a":%s1}', b'{"question":"a"}%s',
                         b'{%s"a":0}', b'%s{"a":0}'):
            for byte in range(256):
                sample = template % bytes([byte])
                self.assertEqual(lexical_payload_state(sample), reference(sample), sample)
        self.assertEqual(lexical_payload_state(br'{"\u0061ttachment_owner_version":null}'), MARKER)
        self.assertEqual(lexical_payload_state(br'{"attachment_owner_version\u0000":null}'), CLEAR)
        self.assertEqual(lexical_payload_state(b'{"x":"\xff"}'), UNCERTAIN)

    def test_production_listener_never_looks_up_a_name(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        with patch.object(socket, 'getfqdn', side_effect=AssertionError('reverse DNS')):
            with server.Server(('127.0.0.1', 0), Handler) as listener:
                self.assertEqual(listener.server_name, '127.0.0.1')
                self.assertEqual(listener.server_port, listener.socket.getsockname()[1])
                worker = threading.Thread(target=listener.serve_forever)
                worker.start()
                try:
                    with closing(http.client.HTTPConnection('127.0.0.1', listener.server_port, timeout=3)) as connection:
                        connection.request('GET', '/')
                        self.assertEqual(connection.getresponse().status, 204)
                    with self.assertRaises(OSError):
                        server.Server(listener.server_address, Handler)
                finally:
                    listener.shutdown()
                    worker.join(3)
                self.assertFalse(worker.is_alive())


@unittest.skipUnless(os.name == 'posix', 'Agent messaging and private attachment storage require POSIX.')
class StartupChecks(unittest.TestCase):
    def setUp(self):
        from attachments_native_composition import bind_native_composition
        from attachments_native_namespace import NativeNamespaceFence
        from attachments_owner_runtime import bind_owner_runtime
        from attachments_service import AttachmentService, UnqualifiedResolver
        from attachments_store import AttachmentStore
        from officer_messaging import MessagingStore
        self.bind = bind_native_composition
        self.runtime = bind_owner_runtime
        self.namespace = NativeNamespaceFence
        self.service_type, self.resolver_type = AttachmentService, UnqualifiedResolver
        self.store_type, self.messaging_type = AttachmentStore, MessagingStore
        self.temp = tempfile.TemporaryDirectory(prefix='motet-sqlite-support-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.agent = 'x:' + str(uuid.uuid4())
        (self.sessions / ('rollout-' + self.agent[2:] + '.jsonl')).write_text(
            json.dumps({'type': 'session_meta', 'payload': {'id': self.agent[2:], 'originator': 'Codex Desktop',
                                                           'source': 'vscode', 'cwd': str(self.root)}}) + '\n')
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Fictional orchard'}}

    def unsupported(self):
        stack = ExitStack()
        for module in ('attachments_owner_guards', 'attachments_owner_runtime',
                       'attachments_native_namespace', 'attachments_native_composition'):
            stack.enter_context(patch(module + '.sqlite_marker_available', return_value=False))
        return stack

    def open(self, stack):
        messaging = self.messaging_type(str(self.state), str(self.sessions))
        stack.callback(messaging.close)
        store = self.store_type(str(self.state), enabled=True, owner_epoch='a' * 64)
        service = self.service_type(store, resolver=self.resolver_type(), officer_deletions=False)
        stack.callback(service.close)
        messaging.available = messaging.codex_available = True
        return messaging, service

    def assert_ordinary_admitted(self, messaging, number):
        result = messaging.submit(self.agent, 'Fictional plain text ' + str(number), str(uuid.uuid4()), self.agents)
        self.assertEqual(result['state'], 'sending')

    def two_starts(self):
        from attachments_native_namespace import ANCHOR, AUTHORITY, HOST, OWNER_IDENTITY
        from attachments_native_composition import VERSION_KEY
        from attachments_types import AttachmentError
        for number in range(2):
            with ExitStack() as stack:
                messaging, service = self.open(stack)
                before = messaging.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
                with patch.dict(os.environ, {'WATCH_DEMO': '0'}):
                    self.assertIsNone(server.start_native_attachment_maintenance(messaging, service))
                self.assertIsNone(messaging._native_composition)
                self.assertEqual(messaging._native_composition_error, 'sqlite_unavailable')
                self.assertIsNone(messaging._attachment_guard)
                self.assertEqual(messaging.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(), before)
                for filename in (ANCHOR, AUTHORITY, HOST, 'managed-claude', 'managed-claude.expected'):
                    self.assertFalse((self.state / filename).exists(), filename)
                self.assertIsNone(messaging.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (OWNER_IDENTITY,)).fetchone())
                self.assertIsNone(service.store.db.execute('SELECT value FROM meta WHERE key=?', (VERSION_KEY,)).fetchone())
                with self.assertRaises(AttachmentError) as rejected:
                    service.native_selection(self.agent)
                self.assertEqual(rejected.exception.code, 'sqlite_unavailable')
                self.assertEqual(str(rejected.exception), SQLITE_UNAVAILABLE_MESSAGE)
                self.assert_ordinary_admitted(messaging, number)

    def test_two_failing_starts_create_no_attachment_authority_and_admit_text(self):
        with self.unsupported():
            self.two_starts()

    def test_authenticated_route_reports_the_exact_notice_without_granting_files(self):
        with self.unsupported(), ExitStack() as stack:
            messaging, service = self.open(stack)
            self.assertIsNone(self.bind(messaging, service, claude_room=lambda: {}))
            listener = server.Server(('127.0.0.1', 0), server.H)
            stack.callback(listener.server_close)
            stack.enter_context(patch.multiple(server, ATTACHMENTS=service, MESSAGING=messaging,
                                               KEY='fictional-sqlite-owner', PORT=listener.server_port))
            worker = threading.Thread(target=listener.serve_forever)
            worker.start()
            try:
                path = '/api/attachments/native/selection?destinationId=' + self.agent
                for key, status in (('fictional-wrong-owner', 401), ('fictional-sqlite-owner', 503)):
                    with closing(http.client.HTTPConnection('127.0.0.1', listener.server_port, timeout=3)) as connection:
                        connection.request('GET', path, headers={'X-Live-Room-Key': key})
                        response = connection.getresponse()
                        self.assertEqual(response.status, status)
                        payload = json.loads(response.read())
                        if status == 503:
                            self.assertEqual(payload['error']['code'], 'sqlite_unavailable')
                            self.assertEqual(payload['error']['message'], SQLITE_UNAVAILABLE_MESSAGE)
                            self.assertFalse(payload['error']['retryable'])
                            self.assertNotIn('context', payload)
                self.assert_ordinary_admitted(messaging, 'after HTTP')
            finally:
                listener.shutdown()
                worker.join(3)

    def test_actual_linked_sqlite_matches_the_startup_capability(self):
        with closing(sqlite3.connect(':memory:')) as db:
            supported = sqlite_marker_available(db)
        if not supported:
            self.two_starts()
        else:
            with ExitStack() as stack:
                messaging, _ = self.open(stack)
                self.assert_ordinary_admitted(messaging, 'supported')

    def test_direct_initialization_and_standalone_runtime_refuse_before_writes(self):
        from attachments_native_namespace import NamespaceError, ANCHOR, AUTHORITY, HOST
        with self.unsupported(), ExitStack() as stack:
            messaging, _ = self.open(stack)
            with self.assertRaises(NamespaceError):
                self.namespace(self.state).initialize_owner(messaging.db, messaging.lock)
            self.assertIsNone(self.runtime(messaging, allow_install=True))
            self.assertEqual(messaging._native_owner_error, 'sqlite_unavailable')
            for filename in (ANCHOR, AUTHORITY, HOST):
                self.assertFalse((self.state / filename).exists())
            self.assertFalse(messaging.db.in_transaction)
            self.assert_ordinary_admitted(messaging, 'standalone')

    def test_retained_protection_refuses_incompatible_sqlite(self):
        # Reconstruct a persisted guard layout as data from a prior supported
        # installation. The current engine is never claimed to qualify for it.
        with ExitStack() as stack:
            messaging, _ = self.open(stack)
            with messaging.db:
                for table in (guards.LINKS, guards.OUTCOMES):
                    messaging.db.execute(f'CREATE TABLE {table} ({guards.RECORD})')
                guards._install_protection(messaging.db)
        with self.unsupported():
            with self.assertRaises(guards.OwnerGuardError):
                self.messaging_type(str(self.state), str(self.sessions))

    def test_incomplete_retained_state_is_never_recovered(self):
        from attachments_native_namespace import AUTHORITY, NamespaceError
        with ExitStack() as stack:
            self.open(stack)
        marker = self.state / AUTHORITY
        marker.write_bytes(b'fictional incomplete installation\n')
        marker.chmod(0o600)
        before = marker.read_bytes()
        with self.unsupported(), self.assertRaises((NamespaceError, OSError, ValueError)):
            self.messaging_type(str(self.state), str(self.sessions))
        self.assertEqual(marker.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
