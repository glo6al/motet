"""Causal runner tests. No Claude executable or real provider is invoked."""
import collections
import contextlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_managed_runner as managed

# POSIX fixtures (owner ids, modes, links, flock, a Unix socket): CI runs this suite on macOS and Linux only, and
# by hand on Windows it skips. Windows is covered by test_managed_windows_storage (this suite again, under the
# simulation) and test_managed_windows_end_to_end (for real).
POSIX_ONLY = 'POSIX fixtures; Windows: test_managed_windows_storage, _process and _end_to_end.'


def uid():
    return str(uuid.uuid4())


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeTransport:
    """Writes inspect durable SQLite state, so claim ordering is actually tested."""
    pid = 12345

    def __init__(self, manifest, directory, *, mismatch=None, fail_user=False, fail_decision=False):
        self.manifest = manifest
        self.directory = directory
        self.mismatch = mismatch
        self.fail_user = fail_user
        self.fail_decision = fail_decision
        self.frames = collections.deque()
        self.writes = []
        self.code = None
        self.starts = self.terminates = 0
        self.claims_seen = []

    def start(self):
        with contextlib.closing(sqlite3.connect(self.directory / 'ledger.sqlite3')) as db:
            self.claims_seen.append(json.loads(db.execute("SELECT value FROM meta WHERE key='launchClaim'").fetchone()[0]))
        self.starts += 1

    def write(self, frame):
        with contextlib.closing(sqlite3.connect(self.directory / 'ledger.sqlite3')) as db:
            if frame['type'] == 'user':
                state = db.execute('SELECT state FROM messages WHERE id=?', (frame['uuid'],)).fetchone()[0]
                assert state == 'dispatching', state
                self.claims_seen.append(('message', frame['uuid']))
                if self.fail_user:
                    raise BrokenPipeError('private fake native error')
            if frame['type'] == 'control_response' and 'behavior' in frame['response'].get('response', {}):
                row = db.execute('SELECT decision_state FROM permissions WHERE native_id=?',
                                 (frame['response']['request_id'],)).fetchone()
                assert row and row[0] == 'dispatching', row
                self.claims_seen.append(('decision', frame['response']['request_id']))
                if self.fail_decision:
                    raise BrokenPipeError('private fake native error')
        self.writes.append(frame)
        if frame['type'] != 'control_request':
            return
        request = frame['request']
        subtype = request['subtype']
        value = {}
        if subtype == 'claim_session':
            value = {'session_id': self.manifest['nativeId'], 'cwd': self.manifest['folder']['path']}
            if self.mismatch == 'claim':
                value['session_id'] = uid()
        elif subtype == 'get_status':
            value = {'sections': [{'rows': [
                {'label': 'Session ID', 'value': self.manifest['nativeId']},
                {'label': 'cwd', 'value': self.manifest['folder']['path']},
                {'label': 'Account', 'value': 'SECRET_ACCOUNT_INFORMATION'}]}]}
            if self.mismatch == 'status':
                value['sections'][0]['rows'][1]['value'] = '/fictional/wrong'
        self.frames.append({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': frame['request_id'], 'response': value}})

    def read(self, timeout=0):
        return self.frames.popleft() if self.frames else None

    def poll(self):
        return self.code

    def terminate(self):
        self.terminates += 1
        self.code = -15

    def wait(self, timeout=3):
        if self.code is None:
            raise TimeoutError()
        return self.code

    def close(self):
        pass

    @property
    def user_writes(self):
        return [frame for frame in self.writes if frame['type'] == 'user']

    @property
    def decisions(self):
        return [frame for frame in self.writes if frame['type'] == 'control_response']

    def echo(self, request_id, text, session=None):
        self.frames.append({'type': 'user', 'uuid': request_id,
                            'session_id': session or self.manifest['nativeId'],
                            'message': {'role': 'user', 'content': text}})

    def result(self, text='answer', session=None, error=False):
        self.frames.append({'type': 'result', 'session_id': session or self.manifest['nativeId'],
                            'subtype': 'success', 'result': text, 'is_error': error})

    def permission(self, native=None, tool='Write', input=None, use=None):
        native = native or uid()
        self.frames.append({'type': 'control_request', 'request_id': native, 'request': {
            'subtype': 'can_use_tool', 'tool_name': tool, 'tool_use_id': use or 'tool-' + native,
            'input': input if input is not None else {'file_path': '/fictional/orchard.txt', 'content': 'FICTIONAL'}}})
        return native


@unittest.skipIf(os.name == 'nt', POSIX_ONLY)
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watch-managed-runner-')
        self.base = Path(self.temp.name).resolve()
        self.run = self.base / 'run'
        self.run.mkdir(mode=0o700)
        self.project = self.base / 'fictional-project'
        self.project.mkdir()
        info = self.project.stat()
        self.manifest = {'schemaVersion': 1, 'runId': uid(), 'nativeId': uid(), 'incarnation': uid(),
                         'folder': {'id': 'fictional', 'revision': 'one', 'label': 'Fictional orchard',
                                    'path': str(self.project), 'device': info.st_dev, 'inode': info.st_ino},
                         'executable': {'path': '/fictional/claude', 'version': managed.PINNED_VERSION,
                                        'sha256': managed.PINNED_SHA256},
                         'profile': 'restricted_test', 'createdAt': 1000}
        self.token = 'a' * 64
        self.write('manifest.json', json.dumps(self.manifest))
        self.write('token', self.token)
        self.write('run.lock', '')
        self.clock = FakeClock()
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
        self.temp.cleanup()

    def write(self, name, value):
        path = self.run / name
        path.write_text(value)
        path.chmod(0o600)

    def runner(self, **kwargs):
        self.transport = FakeTransport(self.manifest, self.run, **kwargs)
        runner = managed.Runner(self.run, transport_factory=lambda manifest: self.transport,
                                clock=self.clock, wall=self.clock)
        self.runners.append(runner)
        runner.start()
        return runner

    def assert_error(self, code, callback):
        with self.assertRaises(managed.RunnerError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        self.assertNotIn('private fake', error.exception.message)

    def send(self, runner, text='first', source='owner', request=None):
        request = request or uid()
        return runner.enqueue(request, source, text)

    def test_identity_claim_and_status_precede_first_input(self):
        runner = self.runner()
        self.assertEqual([v['request']['subtype'] for v in self.transport.writes],
                         ['initialize', 'claim_session', 'get_status'])
        self.assertTrue(self.transport.claims_seen[0]['incarnation'])
        self.assertTrue(runner.snapshot()['identityVerified'])
        self.assertNotIn('SECRET_ACCOUNT_INFORMATION', json.dumps(runner.snapshot()))
        self.send(runner)
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assertEqual(self.transport.claims_seen[-1][0], 'message')

    def test_matching_echo_result_and_fifo_are_distinct(self):
        runner = self.runner()
        one = self.send(runner, 'one')
        two = self.send(runner, 'two')
        self.assertEqual(one['state'], 'dispatching')
        self.assertEqual(two['state'], 'queued')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.transport.echo(one['requestId'], 'one')
        runner.step()
        self.assertEqual(runner.snapshot()['messages'][0]['state'], 'delivered')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.transport.result('answer one')
        runner.step()
        self.assertEqual([v['uuid'] for v in self.transport.user_writes], [one['requestId'], two['requestId']])
        self.assertEqual(runner.snapshot()['messages'][0]['state'], 'completed')
        self.assertEqual(runner.snapshot()['messages'][1]['state'], 'dispatching')

    def test_duplicate_content_and_source_binding_before_eligibility(self):
        runner = self.runner()
        request = uid()
        self.send(runner, request=request)
        self.transport.code = 17
        runner.step()
        duplicate = self.send(runner, request=request)
        self.assertEqual(duplicate['state'], 'unknown')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assert_error('conflict', lambda: self.send(runner, 'different', request=request))
        self.assert_error('conflict', lambda: self.send(runner, source='new_chat', request=request))
        self.assert_error('unavailable', lambda: self.send(runner))

    def test_motet_source_is_its_own_binding(self):
        runner = self.runner()
        request = uid()
        sent = self.send(runner, 'Motet wording.', source='motet', request=request)
        self.assertEqual(sent['source'], 'motet')
        self.assert_error('conflict', lambda: self.send(runner, 'Motet wording.', source='owner', request=request))
        self.assert_error('invalid_request', lambda: self.send(runner, 'Bram wording.', source='peer'))

    def test_partial_write_claim_is_unknown_and_never_replayed(self):
        runner = self.runner(fail_user=True)
        request = uid()
        self.assert_error('native_uncertain', lambda: self.send(runner, request=request))
        self.assertEqual(self.send(runner, request=request)['state'], 'unknown')
        self.assertEqual(sum(c == ('message', request) for c in self.transport.claims_seen), 1)
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertIsNone(runner.snapshot()['nativeExit'])

    def test_restart_refuses_launch_even_after_crash_between_claim_and_write(self):
        runner = self.runner()
        self.send(runner)
        transport = self.transport
        runner.close()
        called = []
        replacement = managed.Runner(self.run, transport_factory=lambda m: called.append(m))
        self.runners.append(replacement)
        self.assert_error('launch_claimed', replacement.start)
        self.assertEqual(called, [])
        self.assertEqual(transport.starts, 1)
        self.assertEqual(transport.terminates, 0)
        snapshot = managed.read_snapshot(self.run)
        self.assertEqual(snapshot['state'], 'uncertain')
        self.assertEqual(snapshot['messages'][0]['state'], 'unknown')
        self.assertIsNone(snapshot['nativeExit'])

    def test_exclusive_run_lock_prevents_second_supervisor(self):
        self.runner()
        called = []
        replacement = managed.Runner(self.run, transport_factory=lambda m: called.append(m))
        self.runners.append(replacement)
        self.assert_error('already_running', replacement.start)
        self.assertEqual(called, [])

    def test_claim_or_native_status_mismatch_never_sends(self):
        for mismatch in ('claim', 'status'):
            with self.subTest(mismatch=mismatch):
                transport = FakeTransport(self.manifest, self.run, mismatch=mismatch)
                runner = managed.Runner(self.run, transport_factory=lambda m: transport)
                self.runners.append(runner)
                self.assert_error('native_uncertain', runner.start)
                self.assertEqual(transport.user_writes, [])
                self.assertFalse(runner.snapshot()['identityVerified'])
                runner.close()
                for name in ('ledger.sqlite3', 'ledger.sqlite3-journal'):
                    (self.run / name).unlink(missing_ok=True)

    def test_unsolicited_or_mismatched_native_results_hold_queue(self):
        runner = self.runner()
        one = self.send(runner)
        self.send(runner, 'queued')
        self.transport.result(session=uid())
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.transport.result()
        runner.step()
        self.assertEqual(runner.snapshot()['messages'][0]['state'], 'unknown')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assertTrue(one['requestId'])

    def test_unsolicited_result_with_no_active_is_uncertain(self):
        runner = self.runner()
        self.transport.result()
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')

    def test_user_echo_requires_exact_uuid_session_and_content(self):
        runner = self.runner()
        request = self.send(runner)
        self.transport.echo(request['requestId'], 'not the original')
        runner.step()
        self.assertFalse(runner.snapshot()['messages'][0]['nativeEcho'])
        self.assertEqual(runner.snapshot()['state'], 'uncertain')

    def test_overlapping_permissions_preserve_evidence_and_once_only_decision(self):
        runner = self.runner()
        self.send(runner)
        native1 = self.transport.permission(input={'file_path': '/fictional/one', 'content': 'ONE'})
        native2 = self.transport.permission(input={'file_path': '/fictional/two', 'content': 'TWO'})
        runner.step()
        permissions = runner.snapshot()['permissions']
        self.assertEqual(len(permissions), 2)
        self.assertTrue(all(p['canAllow'] and p['canDeny'] for p in permissions))
        self.assertEqual(runner.snapshot()['state'], 'needs_attention')
        request = uid()
        result = runner.decide(permissions[1]['id'], request, 'allow')
        self.assertEqual(result['state'], 'written')
        self.assertEqual(runner.decide(permissions[1]['id'], request, 'allow'), result)
        self.assertEqual(len(self.transport.decisions), 1)
        response = self.transport.decisions[0]['response']
        self.assertEqual(response['request_id'], native2)
        self.assertEqual(response['response'], {'behavior': 'allow', 'updatedInput': permissions[1]['input']})
        self.assertNotIn('updatedPermissions', json.dumps(response))
        self.assert_error('conflict', lambda: runner.decide(permissions[1]['id'], request, 'deny'))
        self.assert_error('stale_permission', lambda: runner.decide(permissions[1]['id'], uid(), 'allow'))
        self.assertTrue(runner.snapshot()['permissions'][0]['canAllow'])
        self.assertNotEqual(permissions[0]['id'], native1)

    def test_cancellation_wins_before_decision_and_allow_wins_before_cancel(self):
        runner = self.runner()
        self.send(runner)
        native = self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][0]
        self.transport.frames.append({'type': 'control_cancel_request', 'request_id': native})
        runner.step()
        self.assert_error('stale_permission', lambda: runner.decide(permission['id'], uid(), 'allow'))
        self.assertEqual(self.transport.decisions, [])
        native2 = self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][1]
        request = uid()
        runner.decide(permission['id'], request, 'allow')
        self.transport.frames.append({'type': 'control_cancel_request', 'request_id': native2})
        runner.step()
        self.assertEqual(runner.decide(permission['id'], request, 'allow')['state'], 'written')
        self.assertEqual(len(self.transport.decisions), 1)

    def test_deadline_wins_and_decision_before_deadline_wins(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][0]
        self.clock.now += 120
        self.assert_error('stale_permission', lambda: runner.decide(permission['id'], uid(), 'allow'))
        runner.step()
        self.assertEqual(len(self.transport.decisions), 1)
        self.assertEqual(self.transport.decisions[0]['response']['response']['behavior'], 'deny')
        self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][1]
        runner.decide(permission['id'], uid(), 'allow')
        self.clock.now += 121
        runner.step()
        self.assertEqual(len(self.transport.decisions), 2)
        self.assertEqual(self.transport.decisions[1]['response']['response']['behavior'], 'allow')

    def test_deadline_auto_denies_each_overlap_without_owner_poll(self):
        runner = self.runner()
        self.send(runner)
        for _ in range(3):
            self.transport.permission()
        runner.step()
        self.clock.now += 121
        runner.step()
        runner.step()
        self.assertEqual(len(self.transport.decisions), 3)
        self.assertTrue(all(p['state'] == 'expired' for p in runner.snapshot()['permissions']))

    def test_oversize_overflow_and_questions_receive_correlated_deny(self):
        runner = self.runner()
        self.send(runner)
        natives = [self.transport.permission() for _ in range(8)]
        overflow = self.transport.permission()
        oversized = self.transport.permission(input={'content': 'x' * (32 * 1024)})
        question = self.transport.permission(tool='AskUserQuestion', input={'questions': ['fictional?']})
        runner.step()
        responses = self.transport.decisions
        self.assertEqual([r['response']['request_id'] for r in responses], [overflow, oversized, question])
        self.assertTrue(all(r['response']['response']['behavior'] == 'deny' for r in responses))
        snapshot = runner.snapshot()
        self.assertEqual(sum(p['canAllow'] for p in snapshot['permissions']), 8)
        self.assertNotIn('x' * (32 * 1024), json.dumps(snapshot))
        # Later rejected requests cannot hide older actionable evidence.
        for _ in range(20):
            self.transport.permission(tool='UnknownTool')
        runner.step()
        self.assertEqual(sum(p['canAllow'] for p in runner.snapshot()['permissions']), 8)
        self.assertEqual(len(natives), 8)

    def test_plan_approval_is_never_allowed_and_hands_the_plan_back_to_the_owner(self):
        # Approving a plan also chooses the edit mode, so a bare allow is no approval: never pending, never allowable,
        # denied at once with a reason that sends the plan to the owner; an ordinary tool is still held.
        runner = self.runner()
        self.send(runner)
        plan = self.transport.permission(tool='ExitPlanMode', input={'plan': 'Fictional plan: tidy the orchard.'})
        ordinary = self.transport.permission()
        runner.step()
        (response,) = self.transport.decisions
        self.assertEqual(response['response']['request_id'], plan)
        self.assertEqual(response['response']['response'], {'behavior': 'deny', 'message': managed.PLAN_DENIED})
        self.assertIn('owner', managed.PLAN_DENIED)
        rows = runner.snapshot()['permissions']
        self.assertEqual([(p['tool'], p['state'], p['decision'], p['resolutionReason'], p['canAllow']) for p in rows],
                         [('ExitPlanMode', 'unsupported', 'deny', 'unsupported_tool', False),
                          ('Write', 'pending', None, None, True)])
        self.assert_error('stale_permission', lambda: runner.decide(rows[0]['id'], uid(), 'allow'))
        self.assertEqual(len(self.transport.decisions), 1)
        # Claude's echo of the plan denial is recognised as the retained decision, not as unexplained output.
        before = runner.snapshot()
        self.transport.frames.append(json.loads(json.dumps(response)))
        runner.step()
        self.assertEqual(runner.snapshot(), before)
        runner.decide(rows[1]['id'], uid(), 'allow')
        self.assertEqual([d['response']['request_id'] for d in self.transport.decisions], [plan, ordinary])

    def test_changed_duplicate_native_permission_fails_closed(self):
        runner = self.runner()
        self.send(runner)
        native = self.transport.permission()
        runner.step()
        self.transport.permission(native=native, input={'content': 'different'})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertFalse(runner.snapshot()['permissions'][0]['canAllow'])
        self.assertEqual(self.transport.decisions, [])

    def test_lost_decision_write_never_retries(self):
        runner = self.runner(fail_decision=True)
        self.send(runner)
        native = self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][0]
        request = uid()
        self.assert_error('native_uncertain', lambda: runner.decide(permission['id'], request, 'allow'))
        self.assertEqual(runner.decide(permission['id'], request, 'allow')['state'], 'unknown')
        self.assertEqual(sum(c == ('decision', native) for c in self.transport.claims_seen), 1)

    def test_queue_capacity_rejects_before_native_write(self):
        runner = self.runner()
        for index in range(32):
            self.send(runner, str(index))
        self.assert_error('capacity', lambda: self.send(runner, 'overflow'))
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assertEqual(len(runner.snapshot()['messages']), 32)

    def test_native_exit_receipt_only_from_owned_transport(self):
        runner = self.runner()
        self.send(runner)
        self.send(runner, 'queued')
        self.transport.code = 143
        runner.step()
        snapshot = runner.snapshot()
        self.assertEqual(snapshot['nativeExit'], {'confirmed': True, 'code': 143,
                                                  'incarnation': self.manifest['incarnation']})
        self.assertEqual([m['state'] for m in snapshot['messages']], ['unknown', 'failed'])
        self.assertEqual(managed.read_snapshot(self.run)['nativeExit'], snapshot['nativeExit'])

    def test_shutdown_denies_pending_fails_unsent_and_replays_receipt(self):
        runner = self.runner()
        self.send(runner)
        self.send(runner, 'unsent')
        self.transport.permission()
        runner.step()
        request = uid()
        receipt = runner.shutdown(request)
        self.assertEqual(receipt['state'], 'exited')
        permission = runner.snapshot()['permissions'][0]
        self.assertEqual((permission['resolutionReason'], permission['decisionState'], permission['resolvedAt']),
                         ('shutdown', 'written', 1000))
        self.assertEqual(self.transport.terminates, 1)
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assertEqual(self.transport.decisions[0]['response']['response']['behavior'], 'deny')
        self.assertEqual(runner.shutdown(request), receipt)
        self.assertEqual(self.transport.terminates, 1)
        self.assertEqual([m['state'] for m in runner.snapshot()['messages']], ['unknown', 'failed'])

    def test_read_only_snapshot_is_uncertain_without_exit_no_file_changes(self):
        runner = self.runner()
        self.send(runner)
        names = set(os.listdir(self.run))
        before = (self.run / 'ledger.sqlite3').stat().st_mtime_ns
        snapshot = managed.read_snapshot(self.run)
        self.assertEqual(snapshot['state'], 'uncertain')
        self.assertIsNone(snapshot['nativeExit'])
        self.assertEqual(set(os.listdir(self.run)), names)
        self.assertEqual((self.run / 'ledger.sqlite3').stat().st_mtime_ns, before)
        self.assertEqual(len(self.transport.user_writes), 1)

    def test_read_message_retains_old_tombstone_outside_snapshot(self):
        runner = self.runner()
        first = self.send(runner)
        self.transport.result()
        runner.step()
        for index in range(34):
            self.send(runner, str(index))
            self.transport.result()
            runner.step()
        self.assertNotIn(first['requestId'], [m['requestId'] for m in runner.snapshot()['messages']])
        self.assertEqual(managed.read_message(self.run, first['requestId'])['state'], 'completed')
        self.clock.now += 8 * 86400
        self.send(runner, 'trigger retention')
        old = managed.read_message(self.run, first['requestId'])
        self.assertEqual(old['text'], '')
        self.assertEqual(old['digest'], first['digest'])
        writes = len(self.transport.user_writes)
        self.assertEqual(self.send(runner, request=first['requestId'])['state'], 'completed')
        self.assertEqual(len(self.transport.user_writes), writes)

    def test_changed_folder_and_unsafe_state_refused_before_factory(self):
        self.project.rename(self.base / 'old')
        self.project.mkdir()
        calls = []
        runner = managed.Runner(self.run, transport_factory=lambda m: calls.append(m))
        self.runners.append(runner)
        self.assert_error('folder_changed', runner.start)
        self.assertEqual(calls, [])
        self.assertFalse((self.run / 'ledger.sqlite3').exists())

    def test_symlink_or_readable_manifest_rejected(self):
        (self.run / 'manifest.json').chmod(0o644)
        self.assert_error('unsafe_state', lambda: managed.read_snapshot(self.run))
        (self.run / 'manifest.json').chmod(0o600)
        (self.run / 'token').unlink()
        (self.run / 'token').symlink_to(self.run / 'manifest.json')
        self.assert_error('unsafe_state', lambda: managed.read_snapshot(self.run))

    def test_linked_or_world_readable_ledger_rejected(self):
        runner = self.runner()
        (self.run / 'ledger.sqlite3').chmod(0o644)
        self.assert_error('unsafe_state', lambda: managed.read_snapshot(self.run))
        (self.run / 'ledger.sqlite3').chmod(0o600)
        os.link(self.run / 'ledger.sqlite3', self.run / 'copy')
        self.assert_error('unsafe_state', lambda: managed.read_snapshot(self.run))
        self.assertTrue(runner.snapshot()['identityVerified'])

    def test_protocol_text_validation_and_unknown_commands(self):
        runner = self.runner()
        for text in ('', ' ', 'a' * 8001, 'a\x00b', '\ud800'):
            self.assert_error('invalid_request', lambda text=text: self.send(runner, text))
        self.assert_error('invalid_request', lambda: runner.command('status', {'extra': True}))
        self.assert_error('invalid_request', lambda: runner.command('restart', {}))
        self.assertEqual(self.transport.user_writes, [])

    def exchange(self, runner, value):
        client, server = socket.socketpair()
        thread = threading.Thread(target=lambda: (runner.exchange(server), server.close()))
        thread.start()
        try:
            client.sendall(value)
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return json.loads(b''.join(chunks))
        finally:
            client.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_authenticated_bounded_ipc_rejects_unknown_keys_duplicate_keys_and_frames(self):
        runner = self.runner()
        request = {'token': self.token, 'command': 'status', 'params': {}}
        response = self.exchange(runner, json.dumps(request).encode() + b'\n')
        self.assertTrue(response['ok'])
        request['token'] = 'b' * 64
        response = self.exchange(runner, json.dumps(request).encode() + b'\n')
        self.assertEqual(response['error']['code'], 'unauthorized')
        request['token'] = self.token
        request['extra'] = 'secret'
        response = self.exchange(runner, json.dumps(request).encode() + b'\n')
        self.assertEqual(response['error']['code'], 'invalid_request')
        response = self.exchange(runner, b'{"token":"a","token":"b"}\n')
        self.assertEqual(response['error']['code'], 'invalid_request')
        response = self.exchange(runner, b'x' * managed.MAX_FRAME + b'\n')
        self.assertEqual(response['error']['code'], 'invalid_request')
        self.assertNotIn(self.token, json.dumps(response))
        self.assertEqual(self.transport.user_writes, [])

    def test_serve_exits_after_confirmed_native_exit_and_cleans_endpoint(self):
        transport = FakeTransport(self.manifest, self.run)
        original_read = transport.read

        def eof_after_handshake(timeout=0):
            if transport.frames:
                return original_read(timeout)
            transport.code = 0
            raise EOFError()

        transport.read = eof_after_handshake
        runner = managed.Runner(self.run, transport_factory=lambda m: transport)
        self.runners.append(runner)
        old_umask = os.umask(0o077)
        try:
            with mock.patch.object(managed, 'Runner', return_value=runner):
                self.assertEqual(managed.serve(self.run), 0)
        finally:
            os.umask(old_umask)
        self.assertFalse((self.run / 'control.sock').exists())
        snapshot = managed.read_snapshot(self.run)
        self.assertEqual(snapshot['state'], 'exited')
        self.assertEqual(snapshot['nativeExit']['code'], 0)
        self.assertEqual(transport.terminates, 0)

    def test_terminal_step_does_not_rewrite_exit_or_uncertainty(self):
        runner = self.runner()
        self.transport.code = 0
        runner.step()
        before = (self.run / 'ledger.sqlite3').stat().st_mtime_ns
        self.transport.frames.append({'type': 'result', 'session_id': uid()})
        runner.step()
        self.assertEqual((self.run / 'ledger.sqlite3').stat().st_mtime_ns, before)
        self.assertEqual(runner.snapshot()['state'], 'exited')

    def test_disconnected_decision_replay_reads_exact_claim_without_native_write(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][0]['id']
        request = uid()
        self.assertIsNone(managed.read_decision(self.run, permission, request, 'allow'))
        runner.decide(permission, request, 'allow')
        writes = len(self.transport.decisions)
        runner.close()
        receipt = managed.read_decision(self.run, permission, request, 'allow')
        self.assertEqual(receipt, {'permissionId': permission, 'requestId': request,
                                   'decision': 'allow', 'state': 'written', 'decisionState': 'written',
                                   'resolutionReason': 'owner', 'resolvedAt': 1000.0})
        self.assertEqual(len(self.transport.decisions), writes)
        self.assert_error('conflict', lambda: managed.read_decision(self.run, permission, request, 'deny'))
        self.assert_error('stale_permission', lambda: managed.read_decision(self.run, permission, uid(), 'allow'))
        self.assert_error('conflict', lambda: managed.read_decision(self.run, uid(), request, 'allow'))

    def test_disconnected_decision_claim_without_write_receipt_is_unknown(self):
        runner = self.runner(fail_decision=True)
        self.send(runner)
        self.transport.permission()
        runner.step()
        permission = runner.snapshot()['permissions'][0]['id']
        request = uid()
        self.assert_error('native_uncertain', lambda: runner.decide(permission, request, 'allow'))
        runner.close()
        self.assertEqual(managed.read_decision(self.run, permission, request, 'allow')['state'], 'unknown')
        self.assertEqual(len(self.transport.decisions), 0)
        # The crash window between committed claim and updating the write receipt
        # has the same retained uncertainty, never a new write opportunity.
        with contextlib.closing(sqlite3.connect(self.run / 'ledger.sqlite3')) as db:
            with db:
                db.execute("UPDATE permissions SET decision_state='dispatching' WHERE id=?", (permission,))
        self.assertEqual(managed.read_decision(self.run, permission, request, 'allow')['state'], 'unknown')

    def test_storage_budget_reserves_reply_before_send_and_denies_new_permission(self):
        runner = self.runner()
        with mock.patch.object(managed, 'MAX_EVIDENCE_BYTES', managed.MAX_REPLY * 4 + 16):
            self.send(runner, 'one')
            self.assert_error('capacity', lambda: self.send(runner, 'two'))
            native = self.transport.permission(input={'content': 'x' * 100})
            runner.step()
        self.assertEqual(len(self.transport.user_writes), 1)
        self.assertEqual(self.transport.decisions[0]['response']['request_id'], native)
        self.assertEqual(self.transport.decisions[0]['response']['response']['behavior'], 'deny')
        self.assertFalse(runner.snapshot()['permissions'][0]['canAllow'])
        self.assertEqual(runner.snapshot()['permissions'][0]['resolutionReason'], 'capacity')

    def test_malformed_native_shapes_and_deep_ipc_fail_closed(self):
        runner = self.runner()
        self.send(runner)
        self.transport.frames.append({'type': 'user', 'message': []})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        response = self.exchange(runner, b'[' * 2000 + b']' * 2000 + b'\n')
        self.assertEqual(response['error']['code'], 'invalid_request')
        self.assertEqual(len(self.transport.user_writes), 1)

    def test_overlong_endpoint_path_rejected_before_native_launch_claim(self):
        long_run = self.base / ('long-' + 'x' * 70)
        self.run.rename(long_run)
        calls = []
        runner = managed.Runner(long_run, transport_factory=lambda m: calls.append(m))
        self.runners.append(runner)
        self.assert_error('unsafe_state', runner.start)
        self.assertEqual(calls, [])
        self.assertFalse((long_run / 'ledger.sqlite3').exists())

    def test_native_command_lifecycle_is_metadata_before_echo_and_after_result(self):
        runner = self.runner()
        message = self.send(runner)
        self.transport.frames.append({'type': 'command_lifecycle', 'session_id': self.manifest['nativeId'],
                                      'uuid': message['requestId'], 'result': 'not execution evidence'})
        runner.step()
        retained = runner.snapshot()['messages'][0]
        self.assertEqual(retained['state'], 'dispatching')
        self.assertFalse(retained['nativeEcho'])
        self.assertEqual(retained['reply'], '')
        self.assertEqual(runner.snapshot()['state'], 'running')
        self.assertEqual(len(self.transport.user_writes), 1)
        self.transport.echo(message['requestId'], 'first')
        self.transport.result('actual native result')
        runner.step()
        completed = runner.snapshot()['messages'][0]
        self.transport.frames.append({'type': 'command_lifecycle', 'session_id': self.manifest['nativeId'],
                                      'result': 'not a second result'})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'ready')
        self.assertEqual(runner.snapshot()['messages'][0], completed)
        self.assertEqual(completed['reply'], 'actual native result')
        self.assertEqual(len(self.transport.user_writes), 1)

    def test_native_command_lifecycle_requires_exact_session(self):
        runner = self.runner()
        self.send(runner)
        self.transport.frames.append({'type': 'command_lifecycle', 'session_id': uid()})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertFalse(runner.snapshot()['messages'][0]['nativeEcho'])

    def test_native_command_lifecycle_without_session_is_not_accepted(self):
        runner = self.runner()
        self.send(runner)
        self.transport.frames.append({'type': 'command_lifecycle'})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')

    def test_unrecognized_native_event_type_still_fails_closed(self):
        runner = self.runner()
        self.send(runner)
        self.transport.frames.append({'type': 'fictional_unknown_event', 'session_id': self.manifest['nativeId']})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(runner.snapshot()['messages'][0]['state'], 'unknown')

    def test_exact_claimed_permission_response_echo_is_only_metadata(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission(input={'file_path': '/fictional/one', 'content': 'ONE'})
        self.transport.permission(input={'file_path': '/fictional/two', 'content': 'TWO'})
        runner.step()
        permissions = runner.snapshot()['permissions']
        runner.decide(permissions[0]['id'], uid(), 'allow')
        runner.decide(permissions[1]['id'], uid(), 'deny')
        before = runner.snapshot()
        responses = json.loads(json.dumps(self.transport.decisions))
        self.transport.frames.extend(responses)
        self.transport.frames.extend(responses)  # A repeated echo is still metadata.
        runner.step()
        self.assertEqual(runner.snapshot(), before)
        self.assertEqual(len(self.transport.decisions), 2)
        self.assertFalse(runner.snapshot()['messages'][0]['nativeEcho'])
        self.transport.result('the actual result')
        runner.step()
        completed = runner.snapshot()
        self.transport.frames.extend(responses)
        runner.step()
        self.assertEqual(runner.snapshot(), completed)
        self.assertEqual(len(self.transport.decisions), 2)

    def test_permission_response_echo_requires_a_retained_decision_claim(self):
        runner = self.runner()
        self.send(runner)
        native = self.transport.permission()
        runner.step()
        self.transport.frames.append({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': native,
            'response': {'behavior': 'deny', 'message': 'unclaimed response'}}})
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(self.transport.decisions, [])

    def test_permission_response_echo_changed_input_fails_closed(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission(input={'content': 'original'})
        runner.step()
        runner.decide(runner.snapshot()['permissions'][0]['id'], uid(), 'allow')
        echo = json.loads(json.dumps(self.transport.decisions[0]))
        echo['response']['response']['updatedInput']['content'] = 'changed'
        self.transport.frames.append(echo)
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.decisions), 1)

    def test_permission_response_echo_changed_behavior_fails_closed(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission()
        runner.step()
        runner.decide(runner.snapshot()['permissions'][0]['id'], uid(), 'deny')
        echo = json.loads(json.dumps(self.transport.decisions[0]))
        echo['response']['response']['behavior'] = 'allow'
        self.transport.frames.append(echo)
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.decisions), 1)

    def test_permission_response_echo_unmatched_native_id_fails_closed(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission()
        runner.step()
        runner.decide(runner.snapshot()['permissions'][0]['id'], uid(), 'deny')
        echo = json.loads(json.dumps(self.transport.decisions[0]))
        echo['response']['request_id'] = uid()
        self.transport.frames.append(echo)
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.decisions), 1)

    def test_permission_response_echo_distinguishes_boolean_and_number(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission(input={'value': 1})
        runner.step()
        runner.decide(runner.snapshot()['permissions'][0]['id'], uid(), 'allow')
        echo = json.loads(json.dumps(self.transport.decisions[0]))
        echo['response']['response']['updatedInput']['value'] = True
        self.transport.frames.append(echo)
        runner.step()
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.decisions), 1)

    def test_maximum_snapshot_wire_evidence_fits_bounded_response(self):
        runner = self.runner()
        first = self.send(runner, '\x01' * 8000)
        # JSON control escaping exercises the largest expansion per character.
        original = {'content': '\x03' * 5458}
        self.assertLessEqual(len(managed._json(original).encode()), managed.MAX_INPUT)
        for _ in range(8):
            self.transport.permission(input=original)
        runner.step()
        for permission in runner.snapshot()['permissions']:
            runner.decide(permission['id'], uid(), 'deny')
        self.transport.result('\x02' * managed.MAX_REPLY)
        runner.step()
        for _ in range(30):
            self.send(runner, '\x01' * 8000)
            self.transport.result('\x02' * managed.MAX_REPLY)
            runner.step()
        self.send(runner, '\x01' * 8000)
        for _ in range(8):
            self.transport.permission(input=original)
        runner.step()
        request = {'token': self.token, 'command': 'status', 'params': {}}
        response = self.exchange(runner, json.dumps(request).encode() + b'\n')
        self.assertTrue(response['ok'])
        raw = (managed._json(response) + '\n').encode('utf-8')
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        self.assertLess(len(raw), managed.MAX_RESPONSE)
        self.assertEqual(len(response['value']['messages']), 32)
        self.assertEqual(len(response['value']['permissions']), 16)
        self.assertEqual(sum(p['canAllow'] for p in response['value']['permissions']), 8)
        self.assertEqual(response['value']['messages'][0]['requestId'], first['requestId'])
        self.assertEqual(response['value']['messages'][0]['reply'], '\x02' * managed.MAX_REPLY)
        self.assertEqual(response['value']['permissions'][-1]['input'], original)

    def test_large_reply_preview_truncated_explicitly(self):
        runner = self.runner()
        self.send(runner)
        self.transport.result('z' * (managed.MAX_REPLY + 1))
        runner.step()
        message = runner.snapshot()['messages'][0]
        self.assertTrue(message['replyTruncated'])
        self.assertEqual(len(message['reply']), managed.MAX_REPLY)


    def test_permission_contract_has_server_deadline_and_preserved_creation_time(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission()
        runner.step()
        self.clock.now += 25
        snapshot = runner.snapshot()
        self.assertEqual(snapshot['timeoutSeconds'], 120)
        self.assertEqual(snapshot['serverTime'], 1025)
        self.assertTrue(snapshot['evidenceAvailable'])
        permission = snapshot['permissions'][0]
        self.assertEqual((permission['createdAt'], permission['expiresAt']), (1000, 1120))
        for name in ('decision', 'decisionState', 'resolutionReason', 'resolvedAt'):
            self.assertIsNone(permission[name])
        self.assertTrue(permission['inputAvailable'])
        # Sending a message is not an answer and does not extend the deadline.
        queued = self.send(runner, 'yes, do it')
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(runner.snapshot()['permissions'][0], permission)
        self.assertEqual(self.transport.decisions, [])

    def test_owner_outcomes_keep_decision_reason_time_and_receipt_on_reread(self):
        runner = self.runner()
        self.send(runner)
        for choice in ('allow', 'deny'):
            self.transport.permission()
            runner.step()
            permission = runner.snapshot()['permissions'][-1]
            self.clock.now += 4
            request = uid()
            receipt = runner.decide(permission['id'], request, choice)
            expected = runner.snapshot()['permissions'][-1]
            self.assertEqual((expected['state'], expected['decision'], expected['decisionState']),
                             ('decided', choice, 'written'))
            self.assertEqual(expected['resolutionReason'], 'owner')
            self.assertEqual(expected['resolvedAt'], self.clock.now)
            self.assertEqual(receipt['resolvedAt'], self.clock.now)
            self.assertEqual(managed.read_decision(self.run, permission['id'], request, choice), receipt)
        self.assertEqual(len(self.transport.decisions), 2)

    def test_cancellation_turn_end_and_exit_have_distinct_resolution_reasons(self):
        runner = self.runner()
        self.send(runner)
        native = self.transport.permission()
        runner.step()
        self.clock.now += 1
        self.transport.frames.append({'type': 'control_cancel_request', 'request_id': native})
        runner.step()
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolutionReason'], 'withdrawn')
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolvedAt'], 1001)
        self.transport.permission()
        runner.step()
        self.clock.now += 1
        self.transport.result()
        runner.step()
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolutionReason'], 'turn_ended')
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolvedAt'], 1002)
        self.send(runner)
        self.transport.permission()
        runner.step()
        self.clock.now += 1
        self.transport.code = 0
        runner.step()
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolutionReason'], 'native_exit')
        self.assertEqual(runner.snapshot()['permissions'][-1]['resolvedAt'], 1003)
        for permission in runner.snapshot()['permissions']:
            self.assertIsNone(permission['decision'])
            self.assertIsNone(permission['decisionState'])
            self.assertFalse(permission['canAllow'])
        self.assertEqual(self.transport.decisions, [])

    def test_automatic_rejection_reasons_do_not_claim_withheld_evidence_is_complete(self):
        runner = self.runner()
        self.send(runner)
        for _ in range(8):
            self.transport.permission()
        self.transport.permission()
        self.transport.permission(input={'content': 'x' * (32 * 1024)})
        self.transport.permission(tool='UnknownTool')
        runner.step()
        rows = runner.snapshot()['permissions'][-3:]
        self.assertEqual([p['resolutionReason'] for p in rows], ['capacity', 'oversized', 'unsupported_tool'])
        for row in rows:
            self.assertEqual((row['state'], row['decision'], row['decisionState']), ('unsupported', 'deny', 'written'))
            self.assertEqual(row['resolvedAt'], 1000)
            self.assertFalse(row['inputAvailable'])
            self.assertFalse(row['canAllow'])
            self.assertFalse(row['canDeny'])
        self.clock.now += 120
        runner.step()
        rows = runner.snapshot()['permissions']
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row['resolutionReason'] == 'timeout' for row in rows))
        self.assertTrue(all(row['decisionState'] == 'written' for row in rows))

    def test_failed_automatic_denial_keeps_reason_but_never_claims_written(self):
        runner = self.runner(fail_decision=True)
        self.send(runner)
        self.transport.permission()
        runner.step()
        self.clock.now += 120
        runner.step()
        row = runner.snapshot()['permissions'][0]
        self.assertEqual((row['resolutionReason'], row['decision'], row['decisionState']), ('timeout', 'deny', 'unknown'))
        self.assertEqual(row['resolvedAt'], 1120)
        self.assertEqual(runner.snapshot()['state'], 'uncertain')
        self.assertEqual(len(self.transport.decisions), 0)
        runner.step()
        self.assertEqual(len(self.transport.claims_seen), 3)  # launch, message, attempted denial

    def test_failed_owner_decision_preserves_owner_choice_time_without_replay(self):
        runner = self.runner(fail_decision=True)
        self.send(runner)
        self.transport.permission()
        runner.step()
        ident = runner.snapshot()['permissions'][0]['id']
        request = uid()
        self.clock.now += 8
        self.assert_error('native_uncertain', lambda: runner.decide(ident, request, 'allow'))
        row = runner.snapshot()['permissions'][0]
        self.assertEqual((row['resolutionReason'], row['decision'], row['decisionState'], row['resolvedAt']),
                         ('owner', 'allow', 'unknown', 1008))
        receipt = managed.read_decision(self.run, ident, request, 'allow')
        self.assertEqual(receipt['decisionState'], 'unknown')
        self.assertEqual(receipt['resolvedAt'], 1008)
        self.assertEqual(self.transport.decisions, [])

    def test_disconnected_snapshot_retains_unanswered_evidence_and_true_read_time(self):
        runner = self.runner()
        self.send(runner)
        for _ in range(8):
            self.transport.permission()
        runner.step()
        for _ in range(10):
            self.transport.permission(tool='UnknownTool')
        runner.step()
        runner._uncertain()
        with mock.patch.object(managed.time, 'time', return_value=5000):
            snapshot = managed.read_snapshot(self.run)
        self.assertEqual(snapshot['serverTime'], 5000)
        self.assertEqual(snapshot['state'], 'uncertain')
        self.assertTrue(snapshot['evidenceAvailable'])
        self.assertEqual(len(snapshot['permissions']), 16)
        unanswered = [p for p in snapshot['permissions'] if p['decision'] is None]
        self.assertEqual(len(unanswered), 8)
        for permission in unanswered:
            self.assertEqual(permission['createdAt'], 1000)
            self.assertIsNone(permission['resolvedAt'])
            self.assertIsNone(permission['resolutionReason'])
            self.assertTrue(permission['inputAvailable'])
            self.assertFalse(permission['canAllow'])
            self.assertFalse(permission['canDeny'])

    def test_legacy_ledger_read_and_migration_do_not_invent_terminal_facts(self):
        runner = self.runner()
        self.send(runner)
        self.transport.permission(tool='UnknownTool')
        self.transport.permission()
        runner.step()
        native_writes = len(self.transport.writes)
        runner.close()
        with contextlib.closing(sqlite3.connect(self.run / 'ledger.sqlite3')) as db:
            with db:
                for name in ('resolution_reason', 'resolved_at', 'input_available'):
                    db.execute('ALTER TABLE permissions DROP COLUMN ' + name)
        snapshot = managed.read_snapshot(self.run)
        self.assertFalse(snapshot['permissions'][0]['inputAvailable'])
        self.assertTrue(snapshot['permissions'][1]['inputAvailable'])
        for row in snapshot['permissions']:
            self.assertIsNone(row['resolvedAt'])
            self.assertIsNone(row['resolutionReason'])
        # A restart migrates storage but cannot launch or replay the old turn.
        replacement = managed.Runner(self.run, transport_factory=lambda manifest: self.transport,
                                     clock=self.clock, wall=self.clock)
        self.runners.append(replacement)
        self.assert_error('launch_claimed', replacement.start)
        columns = {r[1] for r in replacement.db.execute('PRAGMA table_info(permissions)')}
        self.assertTrue({'resolution_reason', 'resolved_at', 'input_available'} <= columns)
        self.assertEqual(len(self.transport.writes), native_writes)
        for row in replacement.snapshot()['permissions']:
            self.assertIsNone(row['resolvedAt'])
            self.assertIsNone(row['resolutionReason'])


@unittest.skipIf(os.name == 'nt', 'POSIX pipes under a selector; Windows: test_managed_windows_process.')
class NativePipeTests(unittest.TestCase):
    def test_fake_child_subprocess_uses_bounded_jsonl_and_actual_exit(self):
        # Explicitly inject a fictional child Popen; NativeTransport.start(), which
        # launches Claude, is never called by this test suite.
        transport = managed.NativeTransport({})
        transport.process = subprocess.Popen(
            [sys.executable, '-u', '-c',
             'import sys,json; row=json.loads(sys.stdin.readline()); '
             'print(json.dumps({"type":"fictional","value":row["value"]}),flush=True)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        import selectors
        transport.selector = selectors.DefaultSelector()
        transport.selector.register(transport.process.stdout, selectors.EVENT_READ)
        os.set_blocking(transport.process.stdin.fileno(), False)
        os.set_blocking(transport.process.stdout.fileno(), False)
        try:
            transport.write({'value': 'fictional'})
            frame = None
            for _ in range(20):
                frame = transport.read(.1)
                if frame is not None:
                    break
            self.assertEqual(frame, {'type': 'fictional', 'value': 'fictional'})
            self.assertEqual(transport.wait(timeout=3), 0)
            self.assertEqual(transport.poll(), 0)
        finally:
            transport.terminate()
            transport.wait(timeout=3)
            transport.close()


if __name__ == '__main__':
    unittest.main()
