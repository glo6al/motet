#!/usr/bin/env python3
"""No live sends: mocked CLI, temporary transcripts and an ephemeral HTTP server."""
import concurrent.futures
import errno
import http.client
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import officer_messaging as m
import server


class PublicTimestampTests(unittest.TestCase):
    def test_only_valid_stored_creation_time_is_added_to_public_allowlist(self):
        row = dict(request_id='fictional-request', agent='x:fictional-agent', name='Aster',
                   question='Fictional question', state='answered', reply='Fictional reply', error='',
                   path='/fictional/private', cursor=123, envelope='private transport metadata')
        expected = {key: row[key] for key in ('request_id', 'agent', 'name', 'question', 'state', 'reply', 'error')}
        self.assertEqual(m.public(row), expected)
        for created in (1800000000, 1800000000.125):
            with self.subTest(created=created):
                self.assertEqual(m.public(dict(row, created=created)), dict(expected, created=created))
        for created in (True, False, '1800000000', None, 0, -1, float('nan'), float('inf'),
                        -float('inf'), 10 ** 400):
            with self.subTest(kind=type(created).__name__):
                value = m.public(dict(row, created=created))
                self.assertEqual(value, expected)
                json.dumps(value, allow_nan=False)
        self.assertEqual(m.public(dict(row, created=1800000000, delivery_warning='Fictional warning')),
                         dict(expected, created=1800000000, delivery_warning='Fictional warning'))

    def test_public_created_removal_mutant_is_rejected(self):
        source = inspect.getsource(m.public)
        field = "result['created'] = created"
        self.assertIn(field, source)
        namespace = {'math': m.math}
        exec(compile(source.replace(field, 'pass', 1), '<fictional public timestamp mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m, 'public', namespace['public']):
            PublicTimestampTests('test_only_valid_stored_creation_time_is_added_to_public_allowlist').run(result)
        self.assertFalse(result.wasSuccessful(), 'Removing the public field must break the serializer contract test')


@unittest.skipIf(os.name == "nt", "Codex flows read transcripts by handle-based folder walk; Codex messaging needs the Mac app")
class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = os.path.join(self.temp.name, 'sessions')
        os.mkdir(self.sessions)
        self.ident = str(uuid.uuid4())
        self.agent = 'x:' + self.ident
        self.path = os.path.join(self.sessions, 'rollout-' + self.ident + '.jsonl')
        self.append('session_meta', {'id': self.ident, 'source': 'vscode', 'originator': 'Codex Desktop'})
        self.state = os.path.join(self.temp.name, 'state')
        self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available, self.store.reason = True, ''
        self.agents = {self.agent: {'runtime': 'Codex', 'name': 'Aster'}}
        self.rid = str(uuid.uuid4())
        self.question = 'How far along are your subagents?'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def append(self, typ, payload, partial=False):
        with open(self.path, 'ab') as f:
            f.write(json.dumps({'type': typ, 'payload': payload}).encode() + (b'' if partial else b'\n'))

    def submit(self):
        return self.store.submit(self.agent, self.question, self.rid, self.agents)

    def row(self):
        return self.store.snapshot()['requests'][0]

    def tick(self, returncode=0):
        proc = Mock()
        proc.wait.return_value = returncode
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick()
        return launch

    def user(self, turn='t1', text=None):
        self.append('event_msg', {'type': 'task_started', 'turn_id': turn})
        self.append('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text if text is not None else m.envelope(self.rid, self.question)}]})

    def answer(self, turn='t1', text=None, partial=False):
        self.append('event_msg', {'type': 'task_complete', 'turn_id': turn,
                                 'last_agent_message': text or 'WATCH_REPLY ' + self.rid + '\nTwo workers are testing.'}, partial)

    def test_ask_concurrent_claims_only_one_planner(self):
        def claim(_):
            try: return self.store.claim_ask(self.rid, self.question)
            except m.MessagingError as error: return error.status
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(results.count(409), 15)
        with self.assertRaises(m.MessagingError) as error:
            self.store.claim_ask(self.rid, 'Different text')
        self.assertEqual(error.exception.status, 409)

    def test_work_authorization_and_status_scope_preserved(self):
        self.question = 'Implement the Claude connection now. Codex clearly works.'
        self.submit()
        launch = self.tick()
        sent = launch.call_args.args[0][-1]
        self.assertIn('The owner asks through Motet:\n' + self.question, sent)
        self.assertIn('an explicit work instruction authorizes that work under your existing permissions', sent)
        self.assertIn('a status question asks only for information', sent)
        self.assertNotIn('not permission to change files', sent)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with patch.object(m, 'envelope', return_value='future wrapper must not change this request'):
            self.user(text=sent)
            self.answer()
            self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_legacy_pending_envelope_survives_upgrade(self):
        self.submit()
        with self.store.lock:
            row = self.store._rows()[0]
            row.pop('envelope')
            row.update(state='queued', launched=True)
            self.store._save(row)
        old_text = ('WATCH_REQUEST ' + self.rid + '\nThe owner asks through the Officer of the Watch:\n' + self.question +
                    '\nReply in your final answer with WATCH_REPLY ' + self.rid + ' on its own line, followed by your answer. '
                    'This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.')
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.user(text=old_text)
        self.answer()
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
        launch.assert_not_called()
        self.assertEqual(self.row()['state'], 'answered')

    def test_ask_decision_persisted_before_submission_and_reopen(self):
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))
        decision = {'kind': 'relay', 'agent': self.agent, 'name': 'Aster', 'question': self.question}
        self.assertEqual(self.store.decide_ask(self.rid, decision), decision)
        self.assertFalse(self.store.snapshot()['requests'])
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertEqual(self.store.claim_ask(self.rid, self.question), decision)
        self.assertEqual(self.store.decide_ask(self.rid, decision), decision)
        with self.assertRaises(m.MessagingError):
            self.store.decide_ask(self.rid, {'kind': 'none'})
        self.store.available = True
        self.submit()
        self.assertEqual(self.store.claim_ask(self.rid, self.question), decision)
        self.assertEqual(self.store.submit(self.agent, self.question, self.rid, {})['request_id'], self.rid)

    def test_ask_planning_crash_does_not_replan(self):
        self.store.claim_ask(self.rid, self.question)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with self.assertRaises(m.MessagingError) as error:
            self.store.claim_ask(self.rid, self.question)
        self.assertEqual(error.exception.status, 409)
        self.assertIn('already started', str(error.exception))

    def test_ask_validation_rate_retention_and_capacity(self):
        for text in ('', ' ', 'a' * 4001, '\x00', None):
            with self.assertRaises(m.MessagingError): self.store.claim_ask(self.rid, text)
        with self.assertRaises(m.MessagingError): self.store.claim_ask('bad', self.question)
        with self.assertRaises(m.MessagingError): self.store.decide_ask(self.rid, {'kind': 'none'})
        for _ in range(20): self.store.claim_ask(str(uuid.uuid4()), self.question)
        with self.assertRaises(m.MessagingError) as error: self.store.claim_ask(self.rid, self.question)
        self.assertEqual(error.exception.status, 429)
        self.store.db.execute('UPDATE asks SET created=?', (time.time() - m.RETENTION - 1,)); self.store.db.commit()
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM asks').fetchone()[0], 1)
        with patch.object(m, 'MAX_RECORDS', 1):
            with self.assertRaises(m.MessagingError): self.store.claim_ask(str(uuid.uuid4()), self.question)

    def test_lifetime_lock_prevents_second_store_and_releases_on_close(self):
        with self.assertRaises(OSError): m.MessagingStore(self.state, self.sessions)
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertIsNone(self.store.claim_ask(self.rid, self.question))

    def test_import_quiet_and_private_state(self):
        self.assertIsNone(server.MESSAGING)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.state, 'officer-messages.sqlite3')).st_mode & 0o777, 0o600)

    def test_queue_acceptance_is_not_answer(self):
        self.assertEqual(self.submit()['state'], 'sending')
        call = self.tick().call_args
        self.assertEqual(call.args[0], [m.CLI, 'queue', '--thread', self.ident, '--message', m.envelope(self.rid, self.question)])
        self.assertEqual(self.row()['state'], 'queued')
        self.user()
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer()
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'Two workers are testing.')
        self.assertEqual(self.row()['state'], 'answered')

    def test_exact_envelope_and_same_turn_required(self):
        self.submit(); self.tick()
        self.user('unrelated', 'Someone quoted WATCH_REQUEST ' + self.rid)
        self.answer('unrelated')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')
        self.user('actual')
        self.answer('other')
        self.append('response_item', {'type': 'function_call_output', 'output': 'WATCH_REPLY ' + self.rid})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer('actual')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_nested_turn_metadata_overrides_current_turn(self):
        self.submit(); self.tick()
        self.append('event_msg', {'type': 'task_started', 'turn_id': 'new'})
        self.append('response_item', {'type': 'message', 'role': 'user',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'old'},
            'content': [{'text': m.envelope(self.rid, self.question)}]})
        self.answer('new')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.assertEqual(self.store._rows()[0]['bound_turn'], 'old')
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'other'},
            'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nWrong turn'}]})
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 'old'})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.answer('old')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_conflicting_or_malformed_turn_metadata_never_binds(self):
        self.submit(); self.tick()
        self.append('event_msg', {'type': 'task_started', 'turn_id': 'new'})
        for nested in ({'turn_id': 'old'}, {'turn_id': []}, 'old'):
            self.append('response_item', {'type': 'message', 'role': 'user', 'turn_id': 'new',
                'internal_chat_message_metadata_passthrough': nested,
                'content': [{'text': m.envelope(self.rid, self.question)}]})
        self.answer('new'); self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')

    def test_rebinding_cannot_reuse_previous_turn_final(self):
        self.submit(); self.tick(); self.user('old')
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'old'},
            'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nStale answer'}]})
        self.store.tick()
        self.assertTrue(self.store._rows()[0]['final'])
        self.user('new')
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 'new'})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.assertEqual(self.store._rows()[0]['final'], '')
        self.answer('new'); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_final_only_after_completed_matching_turn(self):
        self.submit(); self.tick(); self.user()
        self.append('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'text': 'WATCH_REPLY ' + self.rid + '\nDone'}]})
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.append('event_msg', {'type': 'task_complete', 'turn_id': 't1'})
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'Done')

    def test_partial_records_and_restart_keep_cursor(self):
        self.submit(); self.tick(); self.user(); self.answer(partial=True)
        self.store.tick()
        self.assertEqual(self.row()['state'], 'awaiting_reply')
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        with open(self.path, 'ab') as f:
            f.write(b'\n')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_preexisting_records_are_not_replies(self):
        self.user(); self.answer(); self.submit(); self.tick()
        self.assertEqual(self.row()['state'], 'queued')

    def test_idempotency_concurrent_and_target_departure(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(len(self.store.snapshot()['requests']), 1)
        self.assertTrue(all(r['request_id'] == self.rid for r in results))
        self.assertEqual(self.store.submit(self.agent, self.question, self.rid, {})['request_id'], self.rid)
        with self.assertRaises(m.MessagingError) as e:
            self.store.submit(self.agent, 'different', self.rid, {})
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.tick().call_count, 1)
        self.assertEqual(self.tick().call_count, 0)

    def test_distinct_followup_after_pause_has_independent_delivery_and_reply(self):
        self.question = 'Pause your work.'
        self.submit(); self.tick()
        followup = str(uuid.uuid4())
        text = 'Continue your work where you left off.'
        self.store.submit(self.agent, text, followup, self.agents)
        launch = self.tick()
        self.assertEqual(launch.call_count, 1)
        self.assertIn(m.envelope(followup, text), launch.call_args.args[0])
        self.assertEqual(self.tick().call_count, 0)
        # The follow-up completes first. Its marker cannot answer the old request.
        self.user('later', m.envelope(followup, text))
        self.answer('later', 'WATCH_REPLY ' + followup + '\nContinuing.')
        self.store.tick()
        rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
        self.assertEqual(rows[followup]['state'], 'answered')
        self.assertEqual(rows[followup]['reply'], 'Continuing.')
        self.assertEqual(rows[self.rid]['state'], 'queued')
        self.user('original'); self.answer('original')
        self.store.tick()
        rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
        self.assertEqual(rows[self.rid]['state'], 'answered')
        self.assertEqual(rows[followup]['reply'], 'Continuing.')

    def test_unknown_and_expired_allow_new_instruction_without_retry_after_restart(self):
        for state in ('unknown', 'expired'):
            with self.subTest(state=state):
                original_id = str(uuid.uuid4())
                original_text = 'Old request ' + state
                self.store.submit(self.agent, original_text, original_id, self.agents)
                row = next(r for r in self.store._rows() if r['request_id'] == original_id)
                row.update(state=state, launched=True)
                self.store._save(row)
                new_id = str(uuid.uuid4())
                new_text = 'New instruction ' + state
                self.store.submit(self.agent, new_text, new_id, self.agents)
                self.assertEqual(self.tick().call_count, 1)
                self.store.close()
                self.store = m.MessagingStore(self.state, self.sessions)
                self.store.available = True
                self.assertEqual(self.tick().call_count, 0)
                self.assertEqual(self.store.submit(self.agent, new_text, new_id, {})['state'], 'queued')
                rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
                self.assertEqual(rows[original_id]['state'], state)
                with self.assertRaises(m.MessagingError):
                    self.store.submit(self.agent, original_text, str(uuid.uuid4()), self.agents)

    def test_same_turn_coalescing_requires_each_request_reply_marker(self):
        self.submit()
        followup = str(uuid.uuid4()); text = 'Continue the work.'
        self.store.submit(self.agent, text, followup, self.agents)
        self.assertEqual(self.tick().call_count, 2)
        self.user('shared')
        self.append('response_item', {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': m.envelope(followup, text)}]})
        self.answer('shared', 'WATCH_REPLY ' + followup + '\nSecond request only.')
        self.store.tick()
        rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
        self.assertEqual(rows[followup]['state'], 'answered')
        self.assertEqual(rows[self.rid]['state'], 'awaiting_reply')
        self.assertEqual(rows[self.rid]['reply'], '')

    def test_unresolved_duplicate_text_rejected_but_distinct_requests_remain_bounded(self):
        self.submit()
        with self.assertRaises(m.MessagingError):
            self.store.submit(self.agent, self.question, str(uuid.uuid4()), self.agents)
        with patch.object(m, 'MAX_PENDING', 1):
            with self.assertRaises(m.MessagingError) as error:
                self.store.submit(self.agent, 'A different instruction', str(uuid.uuid4()), self.agents)
            self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.tick().call_count, 1)

    def test_timeout_nonzero_unknown_never_retry(self):
        self.submit()
        proc = Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired('codex', 10), -9]
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick(); self.store.tick()
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.row()['state'], 'unknown')
        self.user(); self.answer(); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')
        self.rid = str(uuid.uuid4())
        self.submit(); self.tick(1)
        self.assertEqual(self.store.snapshot()['requests'][-1]['state'], 'unknown')

    def test_postlaunch_wait_error_is_unknown(self):
        self.submit()
        proc = Mock()
        proc.wait.side_effect = OSError('process status unavailable')
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick(); self.store.tick()
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.row()['state'], 'unknown')

    def test_restart_sending_unknown(self):
        self.submit()
        self.store.close()
        self.store = m.MessagingStore(self.state, self.sessions)
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertEqual(self.tick().call_count, 0)

    def test_identity_exec_guardian_and_ambiguity(self):
        for payload in ({'id': str(uuid.uuid4())}, {'id': self.ident, 'source': 'exec'},
                        {'id': self.ident, 'originator': 'codex_exec'}, {'id': self.ident, 'thread_source': 'guardian_review'}):
            open(self.path, 'w').close()
            self.append('session_meta', payload)
            with self.assertRaises(m.MessagingError):
                self.submit()
        open(self.path, 'w').close()
        self.append('session_meta', {'id': self.ident})
        other = os.path.join(self.sessions, 'other-' + self.ident + '.jsonl')
        with open(other, 'w') as f: f.write('{}\n')
        with self.assertRaises(m.MessagingError): self.submit()
        self.assertFalse(self.store.snapshot()['requests'])

    def test_changed_identity_before_launch_fails(self):
        self.submit()
        open(self.path, 'w').close()
        self.append('session_meta', {'id': str(uuid.uuid4())})
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.row()['state'], 'failed')

    def test_expiry_late_reply_and_duplicate_limit(self):
        self.submit(); self.tick()
        with self.store.lock:
            row = self.store._rows()[0]
            row['created'] -= m.TTL + 1
            self.store._save(row)
        self.store.tick()
        self.assertEqual(self.row()['state'], 'expired')
        with self.assertRaises(m.MessagingError):
            self.store.submit(self.agent, self.question, str(uuid.uuid4()), self.agents)
        self.user(); self.answer(); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')

    def test_reject_invalid_target_question_uuid(self):
        for target in ('c:' + self.ident, 'x:bad', 'e:' + self.ident):
            with self.assertRaises(m.MessagingError): self.store.submit(target, self.question, self.rid, self.agents)
        for question in ('', ' ', 'a' * 2001, '\x00', None):
            with self.assertRaises(m.MessagingError): self.store.submit(self.agent, question, self.rid, self.agents)
        with self.assertRaises(m.MessagingError): self.store.submit(self.agent, self.question, 'bad', self.agents)

    def test_distinct_followup_warns_about_older_unresolved_messages_and_persists(self):
        first = self.submit()
        self.assertNotIn('delivery_warning', first)
        other = str(uuid.uuid4())
        accepted = self.store.submit(self.agent, 'Continue your work.', other, self.agents)
        self.assertEqual(accepted['delivery_warning'], 'When this message was submitted, older messages to this agent were unresolved and could still run. This request did not cancel them.')
        with self.store.lock:
            older = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            older.update(state='answered', reply='Finished.')
            self.store._save(older)
        refreshed = next(row for row in self.store.snapshot()['requests'] if row['request_id'] == other)
        self.assertEqual(refreshed['delivery_warning'], accepted['delivery_warning'])
        self.assertEqual(self.store.snapshot()['requests'][0]['state'], 'answered')
        self.store.close(); self.store = m.MessagingStore(self.state, self.sessions)
        restored = next(row for row in self.store.snapshot()['requests'] if row['request_id'] == other)
        self.assertEqual(restored['delivery_warning'], accepted['delivery_warning'])
        self.assertEqual(self.store.submit(self.agent, 'Continue your work.', other, {}), restored)

    def test_message_limits_explain_reason_without_suggesting_automatic_resolution(self):
        self.submit()
        original = self.store._rows()
        cases = [('MAX_PENDING', 1, 'unresolved message deliveries'),
                 ('MAX_RECORDS', 1, 'storage limit')]
        for limit, value, phrase in cases:
            with self.subTest(limit=limit), patch.object(m, limit, value):
                with self.assertRaises(m.MessagingError) as error:
                    self.store.submit(self.agent, 'Distinct followup', str(uuid.uuid4()), self.agents)
                self.assertEqual(error.exception.status, 429)
                self.assertIn(phrase, str(error.exception))
                self.assertIn('no new message was sent', str(error.exception).lower())
                self.assertNotIn('Try again later', str(error.exception))
                self.assertEqual(self.store._rows(), original)
        with self.store.lock:
            for _ in range(9):
                copy = dict(original[0], request_id=str(uuid.uuid4()), state='answered')
                self.store._save(copy)
        with self.assertRaises(m.MessagingError) as error:
            self.store.submit(self.agent, 'Distinct followup', str(uuid.uuid4()), self.agents)
        self.assertIn('last minute', str(error.exception))
        self.assertIn('No new message was sent', str(error.exception))
        self.assertEqual(len(self.store._rows()), 10)

    def test_unavailable_snapshot_includes_empty_controls_and_remains_authenticated(self):
        handler = server.H.__new__(server.H)
        handler.path = '/agent-messages'
        handler.headers = {'Host': '127.0.0.1:' + str(server.PORT)}
        handler.send = Mock()
        with patch.object(server, 'KEY', 'test-key'), patch.object(server, 'MESSAGING', None):
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], 403)
            handler.headers['X-Live-Room-Key'] = 'test-key'
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], 200)
            payload = json.loads(handler.send.call_args.args[1])
            self.assertEqual(payload['controls'], [])
            self.assertEqual(payload['requests'], [])

    def test_capacity_rate_and_retention_bounds(self):
        with patch.object(m, 'MAX_PENDING', 0):
            with self.assertRaises(m.MessagingError) as error: self.submit()
            self.assertEqual(error.exception.status, 429)
        with patch.object(m, 'MAX_RECORDS', 0):
            with self.assertRaises(m.MessagingError): self.submit()
        self.submit(); self.tick(); self.user(); self.answer(); self.store.tick()
        with self.store.lock:
            original = self.store._rows()[0]
            for _ in range(9):
                copy = dict(original, request_id=str(uuid.uuid4()))
                self.store._save(copy)
        self.rid = str(uuid.uuid4())
        with self.assertRaises(m.MessagingError) as error: self.submit()
        self.assertEqual(error.exception.status, 429)
        with self.store.lock:
            for row in self.store._rows():
                row['created'] -= m.RETENTION + 1
                self.store._save(row)
        self.assertEqual(self.submit()['state'], 'sending')
        self.assertEqual(len(self.store.snapshot()['requests']), 1)

    def test_capability_probe_is_bounded_and_platform_specific(self):
        with patch.object(m.sys, 'platform', 'win32'), patch.object(m.subprocess, 'run') as run:
            self.assertFalse(self.store.probe()[0])
            run.assert_not_called()
        with patch.object(m.sys, 'platform', 'darwin'), patch.object(m.subprocess, 'run') as run:
            run.side_effect = [Mock(returncode=0, stdout='codex-cli 0.163.0-alpha.11.2'), Mock(returncode=0, stdout='--thread --message')]
            self.assertTrue(self.store.probe()[0])
            self.assertTrue(all(c.kwargs['timeout'] == 5 for c in run.call_args_list))
            run.side_effect = [Mock(returncode=0, stdout='codex-cli 0.163.0'), Mock(returncode=0, stdout='unrelated')]
            self.assertFalse(self.store.probe()[0])

    def test_oversized_partial_record_stops_bounded_tracking(self):
        self.submit(); self.tick()
        with open(self.path, 'ab') as f: f.write(b'x' * (m.MAX_LINE + 1))
        self.store.tick()
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertTrue(self.store._rows()[0]['tracking_stopped'])

    def test_http_auth_host_origin_and_idempotency(self):
        old_key, old_store, old_port = server.KEY, server.MESSAGING, server.PORT
        old_agents = server.AGENTS.copy()
        server.KEY, server.MESSAGING = 'test-key', self.store
        server.AGENTS.clear(); server.AGENTS.update(self.agents)
        httpd = server.Server(('127.0.0.1', 0), server.H)
        server.PORT = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        def request(method, path, headers=None, body=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
            conn.request(method, path, body=json.dumps(body or {}) if method == 'POST' else None, headers=headers or {})
            res = conn.getresponse(); data = res.read(); conn.close()
            return res.status, data
        try:
            self.assertEqual(request('GET', '/agent-messages')[0], 403)
            self.assertEqual(request('GET', '/agent-messages?k=test-key')[0], 403)
            auth = {'X-Live-Room-Key': 'test-key'}
            self.assertEqual(request('GET', '/agent-messages', dict(auth, Origin='https://evil.test'))[0], 403)
            self.assertEqual(request('GET', '/agent-messages', dict(auth, Host='evil.test'))[0], 403)
            payload = {'agent': self.agent, 'question': self.question, 'request_id': self.rid}
            self.assertEqual(request('POST', '/agent-message', body=payload)[0], 403)
            sent_status, sent_body = request('POST', '/agent-message', auth, payload)
            self.assertEqual(sent_status, 200)
            created = json.loads(sent_body)['created']
            self.assertGreater(created, 0)
            self.assertEqual(created, self.store._rows()[0]['created'])
            self.assertEqual(request('POST', '/agent-message', auth, payload)[0], 200)
            self.assertEqual(request('POST', '/agent-message', auth, dict(payload, question='changed'))[0], 409)
            status, body = request('GET', '/agent-messages', auth)
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)['requests']), 1)
            self.assertEqual(json.loads(body)['requests'][0]['created'], created)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join()
            server.KEY, server.MESSAGING, server.PORT = old_key, old_store, old_port
            server.AGENTS.clear(); server.AGENTS.update(old_agents)


# The exact texts the store has always given on POSIX. Written out here, not read from the module,
# so these tests fail if a refactor changes what the owner sees.
STATE_REFUSED = 'Messaging state directory must be private and owned by you.'
LOCK_REFUSED = 'Messaging lock must be a private regular file.'
DATABASE_REFUSED = 'Messaging database must be a private regular file.'


class StoreStorageCommon(unittest.TestCase):
    """Behaviour every system shares: creation, the lifetime lock, and reopening after close."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.temp.name)
        self.state = os.path.join(self.base, 'state')
        self.sessions = os.path.join(self.base, 'sessions')

    def tearDown(self):
        self.temp.cleanup()

    def open(self):
        return m.MessagingStore(self.state, self.sessions)

    def test_new_state_is_private_and_a_second_store_is_refused_until_the_first_closes(self):
        import private_storage
        store = self.open()
        try:
            private_storage.check_private(self.state, directory=True)
            for name in ('officer-messages.lock', 'officer-messages.sqlite3'):
                private_storage.check_private(os.path.join(self.state, name), single_link=True)
            with self.assertRaises(BlockingIOError) as held:
                self.open()
            self.assertIn(held.exception.errno, (errno.EWOULDBLOCK, errno.EAGAIN))
        finally:
            store.close()
        again = self.open()                                # the lock was released by close()
        again.close()

    def test_missing_parent_folders_are_made(self):
        self.state = os.path.join(self.base, 'home', '.live-room')
        self.open().close()
        import private_storage
        private_storage.check_private(self.state, directory=True)


@unittest.skipIf(os.name == 'nt', 'POSIX owner and mode bits')
class StoreStoragePosix(StoreStorageCommon):
    """The refusals and their texts, unchanged from the store's own POSIX checks before the shared layer."""

    def refused(self, text):
        with self.assertRaises(OSError) as raised:
            self.open()
        self.assertEqual(str(raised.exception), text)
        self.assertNotIsInstance(raised.exception, BlockingIOError)

    def make_state(self):
        os.mkdir(self.state, 0o700)

    def test_created_modes(self):
        old = os.umask(0o022)
        try:
            self.open().close()
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        for name in ('officer-messages.lock', 'officer-messages.sqlite3'):
            self.assertEqual(os.stat(os.path.join(self.state, name)).st_mode & 0o777, 0o600)

    def test_state_folder_open_to_others_or_linked_or_not_mine_is_refused(self):
        self.make_state()
        os.chmod(self.state, 0o755)
        self.refused(STATE_REFUSED)
        os.chmod(self.state, 0o700)
        real = self.state
        self.state = os.path.join(self.base, 'linked-state')
        os.symlink(real, self.state)
        self.refused(STATE_REFUSED)
        self.state = real
        with patch('os.getuid', return_value=os.getuid() + 1):
            self.refused(STATE_REFUSED)
        self.state = os.path.join(self.base, 'plain-file')
        open(self.state, 'w').close()
        with self.assertRaises(FileExistsError):       # makedirs refuses a file in the folder's place first
            self.open()

    def test_lock_file_that_is_shared_open_to_others_or_linked_is_refused(self):
        self.make_state()
        lock = os.path.join(self.state, 'officer-messages.lock')
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(lock, 0o644)
        self.refused(LOCK_REFUSED)
        os.chmod(lock, 0o600)
        os.link(lock, lock + '.second')
        self.refused(LOCK_REFUSED)
        os.unlink(lock + '.second')
        with patch('os.getuid', return_value=os.getuid() + 1):
            self.refused(STATE_REFUSED)               # the folder is checked first
        os.unlink(lock)
        os.symlink(os.path.join(self.state, 'elsewhere'), lock)
        with self.assertRaises(OSError) as raised:     # the open itself refuses a link: the system's own error
            self.open()
        self.assertEqual(raised.exception.errno, errno.ELOOP)
        self.assertFalse(os.path.lexists(os.path.join(self.state, 'elsewhere')))
        os.unlink(lock)
        os.mkdir(lock, 0o700)
        with self.assertRaises(OSError) as raised:
            self.open()
        self.assertEqual(raised.exception.errno, errno.EISDIR)

    def test_database_that_is_shared_open_to_others_or_linked_is_refused(self):
        self.open().close()
        database = os.path.join(self.state, 'officer-messages.sqlite3')
        os.chmod(database, 0o640)
        self.refused(DATABASE_REFUSED)
        os.chmod(database, 0o600)
        os.link(database, database + '.second')
        self.refused(DATABASE_REFUSED)
        os.unlink(database + '.second')
        os.rename(database, database + '.moved')
        os.symlink(database + '.moved', database)
        with self.assertRaises(OSError) as raised:
            self.open()
        self.assertEqual(raised.exception.errno, errno.ELOOP)
        os.unlink(database)
        os.rename(database + '.moved', database)
        self.open().close()                            # the original file is accepted again
        self.assertTrue(os.path.exists(os.path.join(self.state, 'officer-messages.lock')))

    def test_refused_lock_or_database_releases_the_lock(self):
        self.open().close()
        database = os.path.join(self.state, 'officer-messages.sqlite3')
        os.chmod(database, 0o644)
        self.refused(DATABASE_REFUSED)
        os.chmod(database, 0o600)
        self.open().close()                            # not left holding the lifetime lock


def _icacls(*args):
    subprocess.run(['icacls'] + list(args), check=True, capture_output=True, timeout=30)


def _junction(link, target):
    """A directory junction needs no special privilege, unlike a symbolic link."""
    subprocess.run(['cmd', '/c', 'mklink', '/J', link, target], check=True, capture_output=True, timeout=30)


@unittest.skipUnless(os.name == 'nt', 'requires real Windows access lists and junctions')
class StoreStorageWindows(StoreStorageCommon):
    EVERYONE_READ = '*S-1-1-0:(R)'      # a SID, so the test does not depend on the system language

    def refused(self, text):
        with self.assertRaises(OSError) as raised:
            self.open()
        self.assertEqual(str(raised.exception), text)

    def test_new_files_have_protected_private_access_lists(self):
        import private_storage
        self.open().close()
        private_storage.windows_security(self.state, True)
        for name in ('officer-messages.lock', 'officer-messages.sqlite3'):
            private_storage.windows_security(os.path.join(self.state, name))

    def test_state_folder_open_to_everyone_is_refused(self):
        import private_storage
        private_storage.make_private_folder(self.state)
        _icacls(self.state, '/grant', self.EVERYONE_READ)
        self.refused(STATE_REFUSED)
        self.assertEqual(os.listdir(self.state), [], 'nothing is created in a refused folder')

    def test_junction_in_place_of_the_state_folder_is_refused(self):
        import private_storage
        target = os.path.join(self.base, 'real-state')
        private_storage.make_private_folder(target)
        _junction(self.state, target)
        self.refused(STATE_REFUSED)
        self.assertEqual(os.listdir(target), [], 'nothing is created behind the junction')

    def test_existing_private_folder_is_accepted(self):
        import private_storage
        private_storage.make_private_folder(self.state)
        self.open().close()

    def test_database_open_to_everyone_is_refused(self):
        self.open().close()
        database = os.path.join(self.state, 'officer-messages.sqlite3')
        _icacls(database, '/grant', self.EVERYONE_READ)
        self.refused(DATABASE_REFUSED)

    def test_lock_open_to_everyone_is_refused(self):
        self.open().close()
        _icacls(os.path.join(self.state, 'officer-messages.lock'), '/grant', self.EVERYONE_READ)
        self.refused(LOCK_REFUSED)

    def test_journal_junction_is_refused(self):
        self.open().close()
        elsewhere = os.path.join(self.base, 'elsewhere')
        os.mkdir(elsewhere)
        _junction(os.path.join(self.state, 'officer-messages.sqlite3-journal'), elsewhere)
        self.refused(DATABASE_REFUSED)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_open_database_cannot_be_swapped_while_sqlite_opens_it(self):
        import private_storage
        self.open().close()
        database = os.path.join(self.state, 'officer-messages.sqlite3')
        fd = private_storage.open_private_rw(database)
        try:
            with self.assertRaises(OSError):
                os.replace(database, database + '.moved')   # the checked handle does not share delete
        finally:
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
