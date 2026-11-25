"""Managed owner ledger and all room-entry paths; fictional local files only."""
from dataclasses import replace
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sys
import unittest
import uuid
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import test_claude_managed as fixture
from test_claude_managed import uid
from officer_messaging import MessagingStore, MessagingError
import server


class ManagedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.native = fixture.Fixture(methodName='runTest')
        self.native.setUp()
        self.manager = self.native.manager
        self.run, self.ident = self.native.start()
        self.aid = 'c:' + self.ident
        self.source = self.native.root / (self.ident + '.jsonl')
        self.source.write_text(json.dumps({'type': 'user', 'sessionId': self.ident,
            'cwd': self.native.folder.path, 'uuid': uid(), 'timestamp': '2026-11-13T12:00:00Z',
            'message': {'role': 'user', 'content': 'Fictional orchard task'}}) + '\n')
        self.native.directory.source = self.source
        self.state = self.native.root / 'legacy-state'
        self.store = MessagingStore(str(self.state), str(self.native.root / 'sessions'), managed=self.manager)
        self.patches = [patch.object(server, 'CLAUDE_MANAGED', self.manager),
                        patch.object(server, 'HOME', str(self.native.root)),
                        patch.object(server, 'CLAUDE_MANAGED_FAILED', False),
                        patch.object(server, 'AGENTS', {}), patch.object(server, 'LINKS', {}),
                        patch.object(server, 'LOG', []), patch.object(server, 'TITLES', {}),
                        patch.object(server, 'HISTORY', None), patch.object(server, 'MESSAGING', self.store)]
        for p in self.patches: p.start()
        self.tails = {}

    def tearDown(self):
        self.store.close()
        for p in reversed(self.patches): p.stop()
        self.native.tearDown()

    def admit(self, permit=lambda: True):
        proof = self.manager.verify_existing(self.ident, self.native.folder)
        return server.admit_new_chat(proof, self.native.folder, self.tails, permit)

    @contextmanager
    def http(self):
        httpd = server.Server(('127.0.0.1', 0), server.H)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        with patch.object(server, 'PORT', httpd.server_port), patch.object(server, 'KEY', 'fictional-owner'):
            thread.start()
            def request(path, body=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                connection.request('POST' if body is not None else 'GET', path,
                                   json.dumps(body) if body is not None else None,
                                   {'X-Live-Room-Key': 'fictional-owner', **(headers or {})})
                response = connection.getresponse()
                status, raw = response.status, response.read()
                connection.close()
                return status, raw
            try:
                yield request
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    @unittest.skipUnless(sys.platform == 'darwin', 'Managed ownership factory is macOS-only')
    def test_disabled_managed_ownership_under_home_alias_preserves_ordinary_paths(self):
        physical_home = self.native.root / 'home'
        physical_home.mkdir(mode=0o700)
        alias = self.native.root / 'home-alias'
        alias.symlink_to(physical_home, target_is_directory=True)
        sessions = physical_home / 'sessions'
        sessions.mkdir()
        target = uid()
        agent = 'x:' + target
        (sessions / ('rollout-' + target + '.jsonl')).write_text(json.dumps({
            'type': 'session_meta',
            'payload': {'id': target, 'source': 'vscode', 'originator': 'Codex Desktop'},
        }) + '\n')
        legacy_ident = uid()
        ordinary_claude = alias / (legacy_ident + '.jsonl')
        legacy_record = json.loads(self.source.read_text())
        legacy_record['sessionId'] = legacy_ident
        ordinary_claude.write_text(json.dumps(legacy_record) + '\n')
        with patch.object(server, 'HOME', str(alias)), patch.object(server, 'CLAUDE_MANAGED_ENABLED', False):
            manager, failed = server.start_managed_claude()
            self.assertFalse(failed)
            self.assertFalse(manager.enabled)
            state = alias / '.live-room'
            store = MessagingStore(str(state), str(alias / 'sessions'), managed=manager)
            self.addCleanup(store.close)
            store.available = True  # The test queues only. No native worker runs.
            agents = {agent: {'runtime': 'Codex', 'name': 'Fictional Codex'}}
            with patch.object(server, 'CLAUDE_MANAGED', manager), \
                 patch.object(server, 'MESSAGING', store), \
                 patch.object(server, 'AGENTS', agents), self.http() as request:
                status, raw = request('/agent-capabilities?agent=' + agent)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(raw)['actions']['queue']['available'])
                payload = dict(agent=agent, question='Fictional next step?', request_id=uid())
                status, raw = request('/agent-message', payload)
                self.assertEqual(status, 200, raw)
                self.assertEqual(json.loads(raw)['request_id'], payload['request_id'])
                self.assertEqual(store._rows()[0]['question'], payload['question'])
                self.assertTrue(server.managed_source_allowed(str(ordinary_claude), server.ClaudeTail))
                self.assertEqual(server.claude_permission_ownership('c:' + legacy_ident),
                                 dict(classification='unmanaged', checkRequired=False))
                self.assertFalse((state / 'managed-claude').exists())
                self.assertFalse((state / 'managed-claude.lock').exists())
                # An incomplete retained marker under the same alias still
                # refuses ordinary routing and cannot expose Claude observation.
                marker = state / 'managed-claude.expected'
                marker.symlink_to(state / 'missing')
                self.assertTrue(server.claude_permission_ownership_check_required())
                self.assertFalse(server.managed_source_allowed(str(ordinary_claude), server.ClaudeTail))
                self.assertEqual(request('/agent-message', dict(payload, request_id=uid()))[0], 503)
                marker.unlink()
                # Retargeting the alias after startup must not classify the
                # still-open store's managed namespace as empty.
                replacement = self.native.root / 'other-home'
                replacement.mkdir(mode=0o700)
                (replacement / '.live-room').mkdir(mode=0o700)
                alias.unlink()
                alias.symlink_to(replacement, target_is_directory=True)
                self.assertFalse(server.managed_source_allowed(str(ordinary_claude), server.ClaudeTail))
                self.assertEqual(request('/agent-message', dict(payload, request_id=uid()))[0], 503)
                self.assertEqual(len(store._rows()), 1)

    def test_home_alias_cannot_bypass_retained_registry_or_follow_a_state_symlink(self):
        home_alias = self.native.root / 'parent-alias'
        home_alias.symlink_to(self.native.root.parent, target_is_directory=True)
        state = home_alias / self.native.root.name
        manager = fixture.managed.ManagedClaude(state)
        self.assertTrue(manager.has_retained_runs())
        self.assertEqual(manager.ownership(self.ident), self.manager.ownership(self.ident))
        self.assertFalse(manager.can_observe(self.ident, self.source))
        self.assertTrue(manager.can_observe(uid(), self.source))

        linked_state = self.native.root / 'linked-state'
        linked_state.symlink_to(self.native.root, target_is_directory=True)
        with self.assertRaises(fixture.managed.ManagedError):
            fixture.managed.ManagedClaude(linked_state).has_retained_runs()
        (self.manager.root / 'registry.sqlite3').unlink()
        with self.assertRaises(fixture.managed.ManagedError):
            manager.can_observe(uid(), self.source)

    def test_reserved_managed_identity_and_children_not_observed_before_admission(self):
        self.assertFalse(server.managed_source_allowed(str(self.source), server.ClaudeTail))
        child = self.native.root / self.ident / 'subagents' / 'agent-fictional.jsonl'
        self.assertFalse(server.managed_source_allowed(str(child), server.ClaudeTail))
        self.assertIsNone(self.admit(lambda: False))
        self.assertFalse(self.manager.ownership(self.ident)['admitted'])
        self.assertFalse(server.AGENTS)
        self.assertEqual(self.admit(), self.aid)
        self.assertEqual(server.AGENTS[self.aid]['cwd'], self.native.folder.path)
        self.assertTrue(server.managed_source_allowed(str(self.source), server.ClaudeTail))
        before = list(server.LOG)
        self.assertEqual(self.admit(), self.aid)
        self.assertEqual(server.LOG, before)

    def test_invalid_metadata_and_conflicting_monitor_fail_before_reader_creation(self):
        self.native.directory.wrong = True
        st = self.source.stat()
        from new_chat_types import VerifiedIdentity
        proof = VerifiedIdentity(self.ident, self.native.folder.path, str(self.source),
                                 st.st_dev, st.st_ino, 'fixture', provider='claude_code')
        self.assertIsNone(server.admit_new_chat(proof, self.native.folder, self.tails, lambda: True))
        self.assertFalse(server.AGENTS)
        self.native.directory.wrong = False
        server.AGENTS[self.aid] = {'runtime': 'Claude', 'cwd': '/fictional/different'}
        self.assertIsNone(self.admit())
        self.assertFalse(self.tails)
        self.assertFalse(self.manager.ownership(self.ident)['admitted'])

    def test_fence_survives_web_restart_and_lost_registry(self):
        self.admit()
        replacement = self.native.make()
        with patch.object(server, 'CLAUDE_MANAGED', replacement):
            self.assertTrue(server.managed_source_allowed(str(self.source), server.ClaudeTail))
            self.native.directory.wrong = True
            self.assertFalse(server.managed_source_allowed(str(self.source), server.ClaudeTail))
        self.native.directory.wrong = False
        (self.manager.root / 'registry.sqlite3').unlink()
        self.assertFalse(server.managed_source_allowed(str(self.source), server.ClaudeTail))
        self.assertFalse(server.managed_source_allowed(str(self.native.root / (uid() + '.jsonl')), server.ClaudeTail))
        self.assertTrue(server.managed_source_allowed(str(self.source), server.CodexTail))

    def test_managed_owner_large_message_never_enters_legacy_dispatch_or_pruning(self):
        self.admit(); self.native.echo = True
        request = uid(); text = 'Fictional text ' * 220
        with patch.object(self.store.shared, 'ensure_directory', side_effect=AssertionError('Bridge consulted')):
            row = server.owner_target_call('owner_action', self.aid, text, request)
        self.assertEqual(row['state'], 'awaiting_reply')
        self.assertEqual(row['delivery_state'], 'delivered')
        self.assertFalse(self.store._rows())
        self.assertTrue(self.store.owner_capabilities(self.aid, {})['actions']['queue']['available'])
        self.store.worker_failed = True
        snapshot = self.store.snapshot()
        self.assertTrue(snapshot['available'])
        self.assertTrue(snapshot['capabilities']['claude_managed'])
        self.assertEqual(snapshot['requests'][0]['question'], text)
        self.store.worker_failed = False
        with patch.object(self.store, '_send', side_effect=AssertionError('legacy send')), patch.object(self.store, '_poll', side_effect=AssertionError('legacy poll')):
            self.store.tick()
        self.store.close()
        self.store = MessagingStore(str(self.state), str(self.native.root / 'sessions'), managed=self.manager)
        self.assertFalse(self.store._rows())
        before = len([x for x in self.native.commands if x[1] == 'enqueue'])
        self.native.disconnected = True
        replay = self.store.owner_action('queue', self.aid, text, request, {}, author='owner')
        self.assertEqual(replay['request_id'], request)
        self.assertEqual(len([x for x in self.native.commands if x[1] == 'enqueue']), before)

    def test_cross_origin_legacy_and_managed_identifiers_cannot_be_reused(self):
        self.admit()
        with self.assertRaises(MessagingError) as invalid:
            server.owner_target_call('owner_action', 'x:' + uid(), 'text', 'not-a-uuid')
        self.assertEqual(invalid.exception.status, 400)
        request = uid()
        self.manager.enqueue(self.run, request, 'new_chat', 'first instruction')
        with self.assertRaises(MessagingError):
            self.store.owner_action('queue', self.aid, 'first instruction', request, {})
        legacy = uid()
        self.store.db.execute('INSERT INTO requests VALUES(?,?)', (legacy, json.dumps({'request_id': legacy})))
        self.store.db.commit()
        with self.assertRaises(MessagingError) as rejected:
            server.owner_target_call('owner_action', self.aid, 'follow up', legacy)
        self.assertEqual(rejected.exception.status, 409)
        self.assertIsNone(self.manager.lookup_message(legacy))
        with self.assertRaises(MessagingError):
            self.store.owner_action('interrupt', self.aid, '', uid(), {})

    def test_disabled_gates_and_failed_registry_do_not_erase_ownership_fence(self):
        self.manager.enabled = False
        with patch.multiple(server, NEW_CHAT_ENABLED=False, CLAUDE_MANAGED_ENABLED=False):
            self.assertFalse(server.managed_source_allowed(str(self.source), server.ClaudeTail))
            self.assertTrue(server.managed_source_allowed(str(self.native.root / (uid() + '.jsonl')), server.ClaudeTail))
            with patch.multiple(server, CLAUDE_MANAGED=None, CLAUDE_MANAGED_FAILED=True):
                self.assertFalse(server.managed_source_allowed(str(self.source), server.ClaudeTail))

    def test_owner_capability_binds_admitted_agent_to_run_after_restart_and_disconnect(self):
        self.assertIsNone(self.manager.owner_capabilities(self.aid)['managed'])
        self.admit()
        expected = {'runId': self.run}
        capability = self.store.owner_capabilities(self.aid, {})
        self.assertEqual(capability['managed'], expected)
        replacement = self.native.make()
        self.native.disconnected = True
        after = replacement.owner_capabilities(self.aid)
        self.assertEqual(after['managed'], expected)
        self.assertFalse(after['actions']['queue']['available'])
        self.assertIsNone(replacement.owner_capabilities('c:' + uid())['managed'])
        replacement.enabled = False
        self.assertIsNone(replacement.owner_capabilities(self.aid)['managed'])

    def test_permission_classification_is_independent_of_rollout_and_availability(self):
        self.manager.enabled = False
        self.manager.closed = True
        self.assertTrue(self.manager.has_retained_runs())
        self.assertTrue(server.claude_permission_ownership_check_required())
        self.assertEqual(server.claude_permission_ownership(self.aid),
                         dict(classification='managed', checkRequired=True))
        self.assertEqual(server.claude_permission_ownership('c:' + self.ident.upper())['classification'], 'managed')
        self.assertEqual(server.claude_permission_ownership('c:ambiguous-id')['classification'], 'unknown')
        self.assertEqual(server.claude_permission_ownership('c:' + uid())['classification'], 'unmanaged')
        self.assertEqual(server.claude_permission_ownership('x:' + uid()),
                         dict(classification='unmanaged', checkRequired=False))

    def test_release_enables_managed_claude(self):
        self.assertIs(server.CLAUDE_MANAGED_ENABLED, True)

    def test_clean_off_and_failed_initialization_have_distinct_activation_signals(self):
        with patch.multiple(server, CLAUDE_MANAGED=None, CLAUDE_MANAGED_ENABLED=False), \
                patch.object(self.store, 'managed', None):
            self.assertFalse(server.claude_permission_ownership_check_required())
            self.assertEqual(server.claude_permission_ownership('c:legacy-id')['classification'], 'unmanaged')
            with patch.object(server, 'CLAUDE_MANAGED_ENABLED', True), \
                    patch.object(server.sys, 'platform', 'darwin'):
                self.assertTrue(server.claude_permission_ownership_check_required())
                self.assertEqual(server.claude_permission_ownership(self.aid)['classification'], 'unknown')
            with patch.object(server, 'CLAUDE_MANAGED_FAILED', True):
                self.assertTrue(server.claude_permission_ownership_check_required())
                with patch.object(self.store, 'owner_capabilities', side_effect=AssertionError('legacy capability fallback')):
                    result = server.owner_target_call('owner_capabilities', self.aid)
                    self.assertEqual(result['permissionOwnership']['classification'], 'unknown')
                    self.assertFalse(result['actions']['queue']['available'])
                with self.assertRaises(MessagingError):
                    server.owner_target_call('owner_action', self.aid, 'Do not send', uid())

    def test_no_manager_requires_verified_absence_of_retained_artifacts(self):
        state = self.native.root / '.live-room'
        with patch.multiple(server, CLAUDE_MANAGED=None, CLAUDE_MANAGED_ENABLED=False), \
                patch.object(self.store, 'managed', None):
            self.assertFalse(server.claude_permission_ownership_check_required())
            state.mkdir()
            self.assertFalse(server.claude_permission_ownership_check_required())
            marker = state / 'managed-claude.expected'
            marker.write_text('1\n')
            self.assertTrue(server.claude_permission_ownership_check_required())
            self.assertEqual(server.claude_permission_ownership(self.aid)['classification'], 'unknown')
            marker.unlink()
            root = state / 'managed-claude'
            root.symlink_to(state / 'missing')
            self.assertTrue(server.claude_permission_ownership_check_required())
            root.unlink()
            with patch.object(server.os, 'lstat', side_effect=PermissionError('fictional inaccessible state')):
                self.assertTrue(server.claude_permission_ownership_check_required())

    def test_registry_failure_and_manager_mismatch_never_mean_unmanaged(self):
        with patch.object(self.store, 'managed', None):
            result = server.owner_target_call('owner_capabilities', self.aid)
            self.assertEqual(result['permissionOwnership']['classification'], 'managed')
            self.assertIsNone(result['managed'])
            self.assertFalse(result['actions']['queue']['available'])
        with patch.object(self.store, 'managed', self.native.make()):
            result = server.owner_target_call('owner_capabilities', self.aid)
            self.assertEqual(result['permissionOwnership']['classification'], 'managed')
            self.assertFalse(result['actions']['queue']['available'])
        (self.manager.root / 'registry.sqlite3').unlink()
        self.assertTrue(server.claude_permission_ownership_check_required())
        result = server.owner_target_call('owner_capabilities', self.aid)
        self.assertEqual(result['permissionOwnership']['classification'], 'unknown')
        self.assertFalse(result['actions']['queue']['available'])

    def test_legacy_decide_http_cannot_bypass_managed_or_unknown_ownership(self):
        pending = {'aid': self.aid, 'allow': None, 'event': threading.Event()}
        with patch.object(server, 'PENDING', {'fictional': pending}), self.http() as request:
            status, _ = request('/decide', {'pid': 'fictional', 'allow': True})
            self.assertEqual(status, 409)
            self.assertIsNone(pending['allow']); self.assertFalse(pending['event'].is_set())
            with patch.object(server, 'CLAUDE_MANAGED_FAILED', True):
                status, _ = request('/decide', {'pid': 'fictional', 'allow': False})
                self.assertEqual(status, 503)
                self.assertIsNone(pending['allow']); self.assertFalse(pending['event'].is_set())
            pending['aid'] = 'c:' + uid()
            status, _ = request('/decide', {'pid': 'fictional', 'allow': False})
            self.assertEqual(status, 200)
            self.assertIs(pending['allow'], False); self.assertTrue(pending['event'].is_set())

    def test_managed_parent_cannot_escape_via_subagent_or_uppercase_hook_identity(self):
        child = 'c:' + uid()
        with patch.object(server, 'PENDING', {}), patch.object(server, 'HOOKED', {}), self.http() as request:
            for route, event in (('/permission', 'PermissionRequest'), ('/hook', 'Notification')):
                status, raw = request(route, {'session_id': self.ident.upper(), 'agent_id': child[2:],
                    'hook_event_name': event, 'notification_type': 'permission_prompt',
                    'tool_name': 'Write', 'tool_input': {'file_path': '/fictional/note'}})
                self.assertEqual((status, raw), (200, b''))
                self.assertFalse(server.PENDING)
                self.assertNotIn(child, server.AGENTS)
            pending = dict(aid=child, session_id=self.ident, allow=None, event=threading.Event())
            server.PENDING['fictional'] = pending
            self.assertEqual(request('/decide', {'pid': 'fictional', 'allow': True})[0], 409)
            self.assertIsNone(pending['allow']); self.assertFalse(pending['event'].is_set())
            # A retained old hook without origin can only use recorded ancestry.
            del pending['session_id']
            server.AGENTS[child] = dict(sub=True, parent=self.aid)
            self.assertEqual(request('/decide', {'pid': 'fictional', 'allow': True})[0], 409)
            self.assertEqual(server.owner_target_call('owner_capabilities', child)['permissionOwnership']['classification'], 'managed')

    def test_mixed_fleet_short_legacy_child_requires_verified_unmanaged_parent(self):
        child, legacy = 'c:1234abcd', uid()
        self.assertEqual(server.claude_permission_ownership(child)['classification'], 'unknown')
        self.assertEqual(server.claude_permission_ownership(child, self.ident)['classification'], 'managed')
        self.assertEqual(server.claude_permission_ownership(child, legacy)['classification'], 'unmanaged')
        with patch.object(server, 'PENDING', {}), patch.object(server, 'HOOKED', {}), self.http() as request:
            with patch.object(server, 'APPROVER', 0):
                status, raw = request('/permission', {'session_id': legacy, 'agent_id': child[2:],
                    'tool_name': 'Write', 'tool_input': {'file_path': '/fictional/note'}})
            self.assertEqual((status, raw), (200, b''))
            self.assertIn(child, server.AGENTS, 'Verified-unmanaged legacy child still reaches normal hook description')
            self.assertFalse(server.PENDING)

    def test_legacy_decide_revalidates_exact_pending_record_after_ownership_io(self):
        original = dict(aid='c:' + uid(), allow=None, event=threading.Event())
        replacement = dict(aid=self.aid, allow=None, event=threading.Event())
        def ownership(*args):
            acquired = server.LOCK.acquire(blocking=False)
            self.assertTrue(acquired, 'Registry lookup must be outside room lock')
            if acquired:
                server.LOCK.release()
            server.PENDING['fictional'] = replacement
            return dict(classification='unmanaged', checkRequired=True)
        with patch.object(server, 'PENDING', {'fictional': original}), self.http() as request:
            with patch.object(server, 'claude_permission_ownership', side_effect=ownership):
                self.assertEqual(request('/decide', {'pid': 'fictional', 'allow': True})[0], 404)
        for pending in (original, replacement):
            self.assertIsNone(pending['allow']); self.assertFalse(pending['event'].is_set())

    def test_sse_hello_carries_explicit_ownership_check_signal(self):
        with patch.object(server, 'BASE', 2), patch.object(server, 'apply_watch'), self.http() as request:
            status, raw = request('/events?k=fictional-owner', headers={'Last-Event-ID': '0'})
        self.assertEqual(status, 200)
        hello = json.loads(raw.decode().splitlines()[0].removeprefix('data: '))
        self.assertTrue(hello['claudePermissionOwnershipCheckRequired'])

    def test_capability_http_never_treats_disabled_managed_null_as_unmanaged(self):
        self.manager.enabled = False
        with self.http() as request:
            status, raw = request('/agent-capabilities?agent=' + self.aid)
            result = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertEqual(result['permissionOwnership'], dict(classification='managed', checkRequired=True))
            self.assertIsNone(result['managed'])
            self.assertFalse(result['actions']['queue']['available'])
            with patch.object(server, 'CLAUDE_MANAGED_FAILED', True):
                status, raw = request('/agent-capabilities?agent=' + self.aid)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)['permissionOwnership']['classification'], 'unknown')


if __name__ == '__main__':
    unittest.main()
