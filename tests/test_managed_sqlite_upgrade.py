"""Legacy managed chats survive optional attachment startup without migration."""
from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


@unittest.skipUnless(os.name == 'posix', 'Managed Claude and private attachment storage require POSIX.')
class ManagedUpgradeTests(unittest.TestCase):
    def setUp(self):
        from attachments_native_composition import bind_native_composition
        from attachments_native_namespace import NativeNamespaceFence
        from attachments_service import AttachmentService, UnqualifiedResolver
        from attachments_store import AttachmentStore
        from native_managed_namespace_fixture import ManagedFixture, fixture_files
        from officer_messaging import MessagingStore
        from test_claude_managed import Directory
        self.bind, self.namespace = bind_native_composition, NativeNamespaceFence
        self.service_type, self.resolver_type = AttachmentService, UnqualifiedResolver
        self.store_type, self.messaging_type = AttachmentStore, MessagingStore
        self.fixture_type, self.fixture_files, self.directory_type = ManagedFixture, fixture_files, Directory
        temp = tempfile.TemporaryDirectory(prefix='motet-managed-upgrade-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state, self.sessions = self.root / 'state', self.root / 'sessions'
        self.sessions.mkdir()
        self.agent = 'x:' + str(uuid.uuid4())
        (self.sessions / ('rollout-' + self.agent[2:] + '.jsonl')).write_text(
            json.dumps({'type': 'session_meta', 'payload': {'id': self.agent[2:], 'originator': 'Codex Desktop',
                                                           'source': 'vscode', 'cwd': str(self.root)}}) + '\n')
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Fictional orchard'}}

    def open(self, stack):
        messaging = self.messaging_type(str(self.state), str(self.sessions))
        stack.callback(messaging.close)
        storage = self.store_type(str(self.state), enabled=True, owner_epoch='a' * 64)
        service = self.service_type(storage, resolver=self.resolver_type(), officer_deletions=False)
        stack.callback(service.close)
        messaging.available = messaging.codex_available = True
        return messaging, service

    def unsupported(self):
        stack = ExitStack()
        for module in ('attachments_owner_guards', 'attachments_owner_runtime',
                       'attachments_native_namespace', 'attachments_native_composition'):
            stack.enter_context(patch(module + '.sqlite_marker_available', return_value=False))
        return stack

    @staticmethod
    def schema(db):
        return db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()

    def file_state(self):
        return {str(path.relative_to(self.state)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.state.rglob('*') if path.is_file()}

    def managed(self, stack, *, create=False):
        if create:
            self.fixture_files(self.state)
        fixture = self.fixture_type(self.state, None)
        stack.callback(fixture.manager.close)
        source = self.state / 'project' / 'fictional-transcript.jsonl'
        source.write_text('Fictional local transcript.\n')
        fixture.manager._test = replace(fixture.manager._test, directory=self.directory_type(source))
        return fixture

    def assert_no_authority(self, messaging, service):
        from attachments_native_composition import VERSION_KEY
        from attachments_native_namespace import ANCHOR, AUTHORITY, OWNER_IDENTITY, MANAGED_IDENTITY
        self.assertIsNone(messaging._attachment_guard)
        self.assertIsNone(messaging._native_composition)
        self.assertIsNone(messaging._native_owner_runtime)
        for filename in (ANCHOR, AUTHORITY):
            self.assertFalse((self.state / filename).exists())
        self.assertIsNone(messaging.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (OWNER_IDENTITY,)).fetchone())
        self.assertIsNone(service.store.db.execute('SELECT value FROM meta WHERE key=?', (VERSION_KEY,)).fetchone())
        registry = self.state / 'managed-claude/registry.sqlite3'
        if registry.exists():
            with closing(sqlite3.connect(str(registry))) as db:
                self.assertIsNone(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (MANAGED_IDENTITY,)).fetchone())

    def assert_notice(self, service, code):
        from attachments_owner_marker import SQLITE_UNAVAILABLE_MESSAGE, LEGACY_MANAGED_UNAVAILABLE_MESSAGE
        from attachments_types import AttachmentError
        with self.assertRaises(AttachmentError) as rejected:
            service.native_selection(self.agent)
        self.assertEqual(rejected.exception.code, code)
        self.assertEqual(rejected.exception.status, 503)
        self.assertEqual(str(rejected.exception), LEGACY_MANAGED_UNAVAILABLE_MESSAGE
                         if code == 'legacy_managed_storage' else SQLITE_UNAVAILABLE_MESSAGE)

    def assert_ordinary_and_managed(self, messaging, fixture, run_id):
        ordinary = messaging.submit(self.agent, 'Fictional ordinary note ' + str(uuid.uuid4()), str(uuid.uuid4()), self.agents)
        self.assertEqual(ordinary['state'], 'sending')
        native = fixture.manager.status(run_id)['nativeId']
        proof = fixture.manager.verify_existing(native, fixture.folder)
        self.assertIsNotNone(proof)
        self.assertTrue(fixture.manager.mark_admitted(proof))
        messaging.managed = fixture.manager
        managed = messaging.submit('c:' + native, 'Fictional managed note.', str(uuid.uuid4()), {})
        self.assertEqual(managed['delivery_state'], 'delivered')
        self.assertTrue(fixture.manager.owner_capabilities('c:' + native)['actions']['queue']['available'])

    def test_first_unsupported_start_reclassifies_after_real_managed_launch(self):
        with self.unsupported(), ExitStack() as stack:
            messaging, service = self.open(stack)
            self.assertIsNone(self.bind(messaging, service, claude_room=lambda: {}))
            self.assert_notice(service, 'sqlite_unavailable')
            fixture = self.managed(stack, create=True)
            run_id = fixture.start()
            self.assertIn('spawn', fixture.calls)
            self.assert_notice(service, 'legacy_managed_storage')
            self.assert_no_authority(messaging, service)
            self.assert_ordinary_and_managed(messaging, fixture, run_id)

    def test_upgrade_then_older_restart_keeps_schemas_files_and_both_text_routes(self):
        with self.unsupported(), ExitStack() as stack:
            messaging, service = self.open(stack)
            self.assertIsNone(self.bind(messaging, service, claude_room=lambda: {}))
            fixture = self.managed(stack, create=True)
            run_id = fixture.start()
        for older in (False, True):
            with self.subTest(older_restart=older), ExitStack() as stack:
                if older:
                    stack.enter_context(self.unsupported())
                messaging, service = self.open(stack)
                before_schema = self.schema(messaging.db), self.schema(service.store.db)
                before_files = self.file_state()
                # The refusal must precede both the semantic gate and initializer.
                with patch('attachments_native_composition.sqlite_marker_available', side_effect=AssertionError('late probe')), \
                     patch.object(self.namespace, 'initialize_owner', side_effect=AssertionError('namespace write')), \
                     patch('attachments_native_composition.bind_owner_runtime', side_effect=AssertionError('guard write')):
                    self.assertIsNone(self.bind(messaging, service, claude_room=lambda: {}))
                self.assertEqual((self.schema(messaging.db), self.schema(service.store.db)), before_schema)
                self.assertEqual(self.file_state(), before_files)
                self.assertEqual(messaging._native_composition_error, 'legacy_managed_storage')
                self.assertEqual(messaging._native_owner_error, 'legacy_managed_storage')
                self.assert_notice(service, 'legacy_managed_storage')
                self.assert_no_authority(messaging, service)
                fixture = self.managed(stack)
                self.assert_ordinary_and_managed(messaging, fixture, run_id)
                self.assert_no_authority(messaging, service)

    def test_raced_legacy_creation_does_not_fall_through_to_guard_install(self):
        from attachments_native_namespace import NamespaceError
        with ExitStack() as stack:
            messaging, service = self.open(stack)
            self.fixture_files(self.state)

            def raced_initialization(*_args):
                fixture = self.managed(stack)
                fixture.start()
                raise NamespaceError()

            # No guard installation is reached; the test substitutes only the
            # semantic gate to exercise this race on older SQLite as well.
            with patch('attachments_native_composition.sqlite_marker_available', return_value=True), \
                 patch.object(self.namespace, 'initialize_owner', side_effect=raced_initialization), \
                 patch('attachments_native_composition.bind_owner_runtime', side_effect=AssertionError('guard write')):
                self.assertIsNone(self.bind(messaging, service, claude_room=lambda: {}))
            self.assert_notice(service, 'legacy_managed_storage')
            self.assert_no_authority(messaging, service)

    def test_retained_native_witnesses_are_not_legacy_or_repaired(self):
        from attachments_native_namespace import ANCHOR, AUTHORITY, OWNER_IDENTITY, MANAGED_IDENTITY
        from attachments_owner_guards import LINKS
        from attachments_native_namespace import NamespaceError
        with ExitStack() as stack:
            messaging, service = self.open(stack)
            fixture = self.managed(stack, create=True)
            fixture.start()
            namespace = self.namespace(self.state)
            self.assertTrue(namespace.legacy_managed_storage(messaging.db))
            for filename in (ANCHOR, AUTHORITY):
                path = self.state / filename
                path.write_bytes(b'Fictional incomplete native witness.\n')
                path.chmod(0o600)
                before = path.read_bytes()
                self.assertFalse(namespace.legacy_managed_storage(messaging.db))
                self.assertEqual(path.read_bytes(), before)
                path.unlink()
            for db_path, table in ((None, OWNER_IDENTITY),
                                   (self.state / 'managed-claude/registry.sqlite3', MANAGED_IDENTITY)):
                with ExitStack() as connections:
                    db = messaging.db if db_path is None else connections.enter_context(closing(sqlite3.connect(str(db_path))))
                    db.execute('CREATE TABLE ' + table + ' (incomplete TEXT)')
                    db.commit()
                    self.assertFalse(namespace.legacy_managed_storage(messaging.db))
                    db.execute('DROP TABLE ' + table)
                    db.commit()
            messaging.db.execute('CREATE TABLE ' + LINKS + ' (incomplete TEXT)')
            messaging.db.commit()
            before = self.schema(messaging.db)
            with self.assertRaises(NamespaceError):
                namespace.legacy_managed_storage(messaging.db)
            self.assertEqual(self.schema(messaging.db), before)


if __name__ == '__main__':
    unittest.main()
