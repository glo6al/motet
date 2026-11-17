"""Native-format fixtures with a real WAL index and an append-only rollout."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from codex_attachment_live_evidence import (
    EvidenceUnavailable, SourceFloor, observe, prepare, revalidate, verify_observation,
)


THREAD = '01961111-aaaa-7111-8111-000000000001'
TURN = '01961111-aaaa-7111-8111-000000000002'
OTHER_TURN = '01961111-aaaa-7111-8111-000000000003'
REQUEST = '01961111-aaaa-7111-8111-000000000004'
ENVELOPE = 'WATCH_REQUEST ' + REQUEST + '\nReturn the label of the attached fictional chart.'
ANSWER = 'The chart label is Amber Grove.'


def encode(record):
    return json.dumps(record, separators=(',', ':')).encode() + b'\n'


def event(kind, **payload):
    return {'type': 'event_msg', 'payload': {'type': kind, **payload}}


def queued_start(turn=TURN, trigger='queue'):
    return event('task_started', turn_id=turn, root_turn_id=turn, started_at=10,
                 collaboration_mode_kind='default', model_context_window=100000,
                 turn_attribution={'initiating_agent_path': '/root', 'parent_turn_id': None,
                                   'root_turn_id': turn, 'turn_id': turn, 'turn_trigger': trigger})


def raw_user(text=ENVELOPE, turn=TURN, *, context=False):
    kind = 'agents_md.instructions' if context else 'user.text'
    provenance = 'agents_md' if context else 'user'
    result = {'type': 'response_item',
              'payload': {'type': 'message', 'role': 'user', 'id': 'fictional-message',
                          'content': [{'type': 'input_text', 'text': text}],
                          'internal_chat_message_metadata_passthrough': {
                              'turn_id': turn, 'create_time': 10.5, 'content_item_kinds': [kind],
                              'content_item_metadata': [{'provenance': {'type': provenance}}],
                          }}}
    if not context:
        result['metadata'] = {
            'retained_source': {'complete': True, 'revision': 'fictional-revision',
                                'id': {'message_id': 'fictional-message', 'turn_id': turn, 'role': 'user'}},
            'client_authored': False, 'user_input_order': 6,
            'mcp_attribution': {'status': 'not_applicable', 'error_reason': ''},
        }
    return result


def user_item(text=ENVELOPE, turn=TURN, thread=THREAD, *, started=False):
    return event('item_started' if started else 'item_completed', turn_id=turn, thread_id=thread,
                 started_at_ms=10, completed_at_ms=11,
                 item={'type': 'UserMessage', 'id': 'different-fictional-id', 'client_id': 'native',
                       'content': [{'type': 'text', 'text': text, 'text_elements': []}]})


def ended(turn=TURN, reply='WATCH_REPLY ' + REQUEST + '\n' + ANSWER):
    return event('task_complete', turn_id=turn, root_turn_id=turn, started_at=10,
                 completed_at=15, duration_ms=5, time_to_first_token_ms=1, last_agent_message=reply)


class LiveSourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.native = base / 'native'
        self.sessions = self.native / 'sessions'
        self.sessions.mkdir(parents=True)
        self.folder = base / 'orchard'
        self.folder.mkdir()
        self.rollout = self.sessions / ('rollout-2026-01-01-' + THREAD + '.jsonl')
        self.immaterial = 'fictional project'
        self.rollout.touch()
        self.ordinal = 0
        self.append({'type': 'session_meta', 'payload': {
            'id': THREAD, 'session_id': THREAD, 'cwd': str(self.folder), 'source': 'appServer',
            'base_instructions': self.immaterial,
        }}, event('thread_settings_applied', thread_id=THREAD, thread_settings={}))
        self.conn = sqlite3.connect(self.native / 'state_5.sqlite')
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INT, source TEXT)')
        self.conn.execute("INSERT INTO threads VALUES(?,?,?,0,'appServer')",
                          (THREAD, str(self.rollout), str(self.folder)))
        self.conn.commit()
        self.hash = hashlib.sha256(ENVELOPE.encode()).hexdigest()

    def append(self, *records):
        with open(self.rollout, 'ab') as stream:
            for record in records:
                raw = encode({'ordinal': self.ordinal, 'timestamp': '2026-01-01T00:00:00Z', **record})
                stream.write(raw)
                self.ordinal += 1

    def prepare(self):
        info = self.folder.stat()
        return prepare(self.native, THREAD, self.folder, info.st_dev, info.st_ino)

    def observe(self, floor):
        return observe(self.native, floor, REQUEST, self.hash)

    def test_complete_queue_with_context_and_wal(self):
        self.assertTrue((self.native / 'state_5.sqlite-wal').is_file())
        floor = self.prepare()
        saved = SourceFloor.from_record(floor.to_record())
        self.assertEqual(floor, saved)
        revalidate(self.native, saved)
        self.append(queued_start(), raw_user('Harness-provided project instructions.', context=True),
                    user_item(started=True), raw_user(), user_item(),
                    {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'fictional'}},
                    ended())
        result = self.observe(saved)
        self.assertEqual((result.state, result.turn_id, result.reply), ('reply', TURN, ANSWER))
        self.assertNotIn(ANSWER, repr(result))
        verify_observation(self.native, saved, result)
        self.append(event('token_count'))
        with self.assertRaises(EvidenceUnavailable):
            verify_observation(self.native, saved, result)
        self.assertEqual(self.observe(saved).state, 'reply')

    def test_waiting_until_complete_line_and_end(self):
        floor = self.prepare()
        self.assertEqual(self.observe(floor).state, 'waiting')
        self.append(queued_start(), raw_user(), user_item())
        self.assertEqual(self.observe(floor).state, 'waiting')
        terminal = encode({'ordinal': self.ordinal, 'timestamp': 'timestamp', **ended()})
        with self.rollout.open('ab') as f:
            f.write(terminal[:-1])
        self.assertEqual(self.observe(floor).state, 'waiting')
        with self.rollout.open('ab') as f:
            f.write(b'\n')
        self.assertEqual(self.observe(floor).state, 'reply')

    def test_terminal_without_verified_marker_records_only_end(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), ended(reply='Uncorrelated text.'))
        result = self.observe(floor)
        self.assertEqual((result.state, result.turn_id, result.reply), ('ended', TURN, None))

    def test_no_turn_guess_for_existing_busy_turn(self):
        self.append(queued_start())
        floor = self.prepare()
        self.append(raw_user(), user_item(), ended())
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_pending_floor_refuses_intact_advancing_transcript(self):
        floor = self.prepare()
        self.append(event('token_count'))
        with self.assertRaisesRegex(EvidenceUnavailable, 'source_floor_advanced'):
            revalidate(self.native, floor)

    def test_prefix_rewrite_refuses_terminal_even_if_source_identity_unchanged(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), ended())
        raw = self.rollout.read_bytes()
        replacement = b'fictiona1 project'
        self.assertEqual(len(replacement), len(self.immaterial))
        self.rollout.write_bytes(raw.replace(self.immaterial.encode(), replacement, 1))
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_duplicate_fields_in_native_header_are_rejected(self):
        raw = self.rollout.read_bytes()
        self.rollout.write_bytes(raw.replace(b'{"ordinal":0', b'{"ordinal":0,"ordinal":0', 1))
        with self.assertRaisesRegex(EvidenceUnavailable, 'invalid_record'):
            self.prepare()

    def test_index_selection_change_refuses_original_bytes(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), ended())
        other = self.sessions / 'different-rollout.jsonl'
        other.write_bytes(self.rollout.read_bytes())
        self.conn.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(other), THREAD))
        self.conn.commit()
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_worktree_replacement_refuses_original_bytes(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), ended())
        self.folder.rename(self.folder.with_name('old-orchard'))
        self.folder.mkdir()
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_turn_without_native_user_item_is_unavailable(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), ended())
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_independent_new_turn_does_not_poison_confirmed_turn(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), ended(), queued_start(OTHER_TURN),
                    raw_user('An unrelated later message.', OTHER_TURN))
        self.assertEqual(self.observe(floor).state, 'reply')

    def test_unknown_source_shape_withholds_candidate(self):
        floor = self.prepare()
        self.append(queued_start(), raw_user(), user_item(), event('fixture_unknown'), ended())
        self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_strictly_bound_and_unique_input(self):
        invalid_cases = (
            (queued_start(trigger='manual'), raw_user(), user_item(), ended()),
            (queued_start(), raw_user(), user_item(), raw_user('Other input during turn.'), ended()),
            (queued_start(), raw_user(), user_item(), user_item('Different native event.'), ended()),
            (queued_start(), raw_user(), user_item(ENVELOPE + ' extra text'), ended()),
            (queued_start(), raw_user(), user_item('Unrelated native input event.'), ended()),
            (queued_start(), raw_user(), user_item(thread=OTHER_TURN), ended()),
            (queued_start(), raw_user(), user_item(), user_item(turn=OTHER_TURN), ended()),
            (queued_start(), raw_user(), user_item(), event('task_aborted', turn_id=TURN), ended()),
            (queued_start(), raw_user(), user_item(), ended(), ended()),
            (queued_start(), raw_user(), raw_user('Late context.', context=True), user_item(), ended()),
        )
        for records in invalid_cases:
            with self.subTest(records=records):
                floor = self.prepare()
                self.append(*records)
                self.assertEqual(self.observe(floor).state, 'unavailable')

    def test_retained_source_body_and_id_must_be_confirmed(self):
        for field, value in (('complete', False), ('id', {'message_id': 'different'})):
            with self.subTest(field=field):
                floor = self.prepare()
                raw = raw_user()
                raw['metadata']['retained_source'][field] = value
                self.append(queued_start(), raw, user_item(), ended())
                self.assertEqual(self.observe(floor).state, 'unavailable')


if __name__ == '__main__':
    unittest.main()
