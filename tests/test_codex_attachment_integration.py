"""Real WATCH native attachment HTTP and ledgers with a private fictional Codex source."""
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import test_claude_attachment_integration as harness
import test_codex_attachment_live_evidence as records


@unittest.skipUnless(sys.platform == 'darwin', 'The production adapter targets the pinned macOS Codex application.')
class CodexAttachmentIntegration(unittest.TestCase):
    def setUp(self):
        self.h = harness.ClaudeAttachmentIntegration()
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        h = self.h
        h.room.clear()
        h.destination = 'x:' + records.THREAD
        h.room[h.destination] = {'runtime': 'Codex', 'cwd': str(h.project), 'name': 'Fictional orchard'}
        native = h.home / 'native'
        sessions = native / 'sessions'
        sessions.mkdir(parents=True)
        h.messaging.sessions_dir = str(sessions)
        self.rollout = sessions / 'fictional.jsonl'
        self.rollout.touch(mode=0o600)
        self.ordinal = 0
        self.append({'type': 'session_meta', 'payload': {
            'id': records.THREAD, 'cwd': str(h.project), 'source': 'appServer'}})
        self.db = sqlite3.connect(native / 'state_5.sqlite')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INT, source TEXT)')
        self.db.execute("INSERT INTO threads VALUES(?,?,?,0,'appServer')",
                        (records.THREAD, str(self.rollout), str(h.project)))
        self.db.commit()
        h.start()
        queue = h.comp.claude.codex.queue
        self.calls = h.stack.enter_context(patch.object(queue, 'send_once'))
        self.ready = h.stack.enter_context(patch.object(queue, 'ready', return_value=True))
        self.settings = h.stack.enter_context(patch('codex_model_settings._read_settings', side_effect=lambda thread_id, **_: {
            'hostId': 'local', 'nativeThreadId': thread_id, 'state': 'observed',
            'settings': {'model': 'gpt-6-astra', 'mode': 'default', 'modelProvider': 'openai'}}))

    def append(self, *events):
        with self.rollout.open('ab') as stream:
            for event in events:
                stream.write(records.encode({'timestamp': '2026-01-01T00:00:00Z', 'ordinal': self.ordinal, **event}))
                self.ordinal += 1

    def test_exact_native_terminal_is_required_for_confirmation(self):
        h = self.h
        selected = h.selection()
        self.assertTrue(selected['completionOnly'])
        intent = h.prepare(selected)
        submission = h.send(intent)['observation']['request']
        self.assertFalse(submission['accepted'])
        self.assertEqual(submission['delivery'], 'unknown')
        self.calls.assert_called_once()
        target, envelope = self.calls.call_args.args
        self.assertEqual((target.thread_id, target.project), (records.THREAD, str(h.project)))
        self.assertIn(harness.QUESTION, envelope)
        source = next((h.project / '.watch-attachments').rglob('*.pdf'))
        self.assertEqual(source.read_bytes(), harness.PDF)
        self.assertIn(str(source.relative_to(h.project)), envelope)
        h.send(intent, status=409)
        self.calls.assert_called_once()

        owner_request_id = envelope.splitlines()[0].split(' ')[1]
        self.append(records.queued_start(), records.raw_user(envelope), records.user_item(envelope))
        intermediate = h.status(selected, intent)['observation']['request']
        self.assertFalse(intermediate['accepted'])
        self.assertEqual(intermediate['delivery'], 'unknown')
        self.append(records.ended(reply='WATCH_REPLY ' + owner_request_id + '\n' + records.ANSWER))
        for _ in range(2):
            confirmed = h.status(selected, intent)['observation']['request']
            self.assertTrue(confirmed['accepted'])
            self.assertEqual((confirmed['delivery'], confirmed['execution']), ('completed', 'ended'))
            self.assertEqual(confirmed['reply'], {'state': 'available', 'text': records.ANSWER})
        self.settings.reset_mock()
        self.settings.side_effect = AssertionError('Retained reads must not wait for Desktop settings.')
        retained = h.status(selected, intent)['observation']['request']
        self.settings.assert_not_called()
        self.assertEqual(retained['delivery'], 'completed')
        self.assertEqual(retained['reply'], {'state': 'available', 'text': records.ANSWER})
        self.ready.return_value = False
        self.assertEqual(h.status(selected, intent)['observation']['request'], retained)
        self.calls.assert_called_once()

    def test_unsupported_source_refuses_send_before_creating_workspace_copies(self):
        h = self.h
        selected = h.selection()
        intent = h.prepare(selected)
        with self.rollout.open('ab') as stream:
            stream.write(b'partial fictional line')
        h.send(intent, status=503)
        self.assertFalse((h.project / '.watch-attachments').exists())
        self.calls.assert_not_called()

    def test_stopping_after_final_copy_check_prevents_invocation(self):
        h = self.h
        selected = h.selection()
        intent = h.prepare(selected)
        adapter = h.comp.claude.codex
        check_work = adapter._check_work

        def stop_after_check(*args):
            result = check_work(*args)
            h.comp._permission.stop_entry(h.comp.claude.adapter)
            return result

        with patch.object(adapter, '_check_work', side_effect=stop_after_check):
            h.send(intent, status=503)

        self.calls.assert_not_called()


if __name__ == '__main__':
    unittest.main()
