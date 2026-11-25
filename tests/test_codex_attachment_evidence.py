"""Offline source-map checks using fictional rollouts and real private SQLite."""
import ast
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import codex_attachment_evidence as evidence


def uid():
    return str(uuid.uuid4())


def sha(value):
    return hashlib.sha256(value).hexdigest()


def pin(path):
    st = path.stat()
    return evidence.PathPin(str(path), st.st_dev, st.st_ino, st.st_uid)


def line(kind, payload):
    return json.dumps({'type': kind, 'payload': payload}, ensure_ascii=False).encode() + b'\n'


def event(kind, turn, **fields):
    return line('event_msg', {'type': kind, 'turn_id': turn, **fields})


def user(text, **metadata):
    return line('response_item', {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': text}], **metadata})


def unsupported():
    """Why Motet's Codex evidence reader is unavailable here, or None. It refuses without these by design."""
    if os.name != 'posix':
        return ('Delivery to agent chats, Codex included, is off on Windows (not ported); this reader also opens '
                'Codex files with POSIX descriptors.')
    if not hasattr(sqlite3.Connection, 'deserialize'):
        return ('Codex attachment evidence reads a snapshot of the Codex index with sqlite3 deserialize '
                '(Python 3.11 or later); Motet refuses it on older Pythons.')
    return None


@unittest.skipIf(unsupported(), unsupported())
class CodexEvidenceTests(unittest.TestCase):
    def setUp(self):
        shared = tempfile.TemporaryDirectory()
        self.addCleanup(shared.cleanup)
        self.root = Path(shared.name).resolve()
        self.native = self.root / 'fictional-native'
        self.sessions = self.native / 'sessions'
        self.project = self.root / 'fictional-project'
        self.sessions.mkdir(parents=True)
        self.project.mkdir()
        self.thread_id = uid()
        self.owner_id = uid()
        self.turn = uid()
        self.rollout = self.sessions / ('rollout-fictional-' + self.thread_id + '.jsonl')
        self.header = {'id': self.thread_id, 'session_id': self.thread_id, 'cwd': str(self.project), 'source': 'cli'}
        self.rollout.write_bytes(line('session_meta', self.header))
        self.index = self.native / 'state_5.sqlite'
        self.update_index('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
        self.update_index('INSERT INTO threads VALUES (?,?,?,?,?)', self.index_values())
        self.envelope = 'WATCH_REQUEST ' + self.owner_id + '\nFictional source proof.'
        self.attempt = evidence.AttemptPin(uid(), self.owner_id, self.thread_id, 'a'*64, 'b'*64, sha(self.envelope.encode()),
                                          pin(self.native), pin(self.sessions), pin(self.index), pin(self.rollout), pin(self.project))
        self.database = self.root / 'fictional-map.sqlite'
        self.map = evidence.EvidenceMap(str(self.database), create=True)
        self.install = self.map.installation_id
        self.addCleanup(lambda: self.map.close())

    def index_values(self, path=None, project=None):
        return (self.thread_id, str(path or self.rollout), str(project or self.project), 0, 'cli')

    def update_index(self, sql, params=()):
        db = sqlite3.connect(self.index)
        try:
            with db:
                db.execute(sql, params)
        finally:
            db.close()

    def append(self, *records):
        with self.rollout.open('ab') as destination:
            for record in records:
                destination.write(record)

    def message(self, turn=None, **metadata):
        return user(self.envelope, **({'turn_id': turn or self.turn} if not metadata else metadata))

    def terminal(self, reply=None):
        fields = {'last_agent_message': reply} if reply is not None else {}
        return event('task_complete', self.turn, **fields)

    def complete(self, reply=None):
        self.append(event('task_started', self.turn), self.message(), self.terminal(reply))

    def seed(self):
        self.map.seed(self.attempt)

    def scan(self, **options):
        return self.map.scan(self.attempt, **options)

    def frontier(self):
        return tuple(self.map.db.execute('SELECT verified_end,verified_line,verified_hash FROM frontiers').fetchone())

    def count(self):
        return self.map.db.execute('SELECT COUNT(*) FROM emissions').fetchone()[0]

    def reopen(self, **options):
        self.map.close()
        self.map = evidence.EvidenceMap(str(self.database), self.install, **options)

    def assert_unavailable(self, code=None):
        report = self.scan()
        self.assertEqual(report.status, 'unavailable')
        self.assertIsNone(report.candidate)
        if code is not None:
            self.assertEqual(report.reason, code)

    def test_exact_terminal_and_reply_fingerprints_without_raw_persistence(self):
        self.seed()
        earlier = uid()
        self.append(event('task_started', earlier), user('Another fictional turn.', turn_id=earlier), event('task_complete', earlier))
        self.append(event('task_started', self.turn), self.message(internal_chat_message_metadata_passthrough={'turn_id': self.turn}))
        self.assertEqual(self.scan().status, 'waiting')
        self.assertEqual(self.count(), 0)
        text = 'Fictional reply with exact UTF-8. 🧪'
        raw = self.terminal('WATCH_REPLY ' + self.owner_id + '\n' + text)
        offset = self.rollout.stat().st_size
        self.append(raw)
        report = self.scan(chunk=47)
        self.assertEqual(report.status, 'candidate')
        c = report.candidate
        self.assertEqual((c.kind, c.ordinal, c.turn_id, c.start, c.end, c.line),
                         ('reply', 1, self.turn, offset, offset + len(raw), 7))
        self.assertEqual((c.raw_sha256, c.reply_sha256), (sha(raw), sha(text.encode())))
        self.assertEqual(self.count(), 1)
        self.assertNotIn(text.encode(), self.database.read_bytes())
        self.assertNotIn(self.envelope.encode(), self.database.read_bytes())
        self.assertEqual({row[1] for row in self.map.db.execute('PRAGMA table_info(emissions)')},
                         {'attempt', 'ordinal', 'segment', 'kind', 'turn_id', 'start', 'end', 'line',
                          'raw_sha256', 'semantic_sha256', 'proof_sha256', 'reply_sha256'})
        self.reopen()
        before = self.map.db.total_changes
        self.assertEqual(self.scan(chunk=1), report)
        self.assertEqual(self.map.db.total_changes, before)

    def test_independent_chunk_and_checkpoint_order_keep_same_candidate(self):
        self.seed()
        second_path = self.root / 'fictional-second-map.sqlite'
        with evidence.EvidenceMap(str(second_path), create=True) as second:
            second.seed(self.attempt, chunk=31)
            self.append(event('task_started', self.turn))
            self.assertEqual(self.scan(chunk=7).status, 'waiting')
            self.reopen()
            self.append(self.message(), self.terminal())
            result = self.scan(chunk=211)
            self.assertEqual(result, second.scan(self.attempt, chunk=64))
            self.assertEqual(result.candidate.kind, 'ended')
            self.assertEqual(result.candidate.ordinal, 1)
            extra = uid()
            previous = self.frontier()
            self.append(event('task_started', extra), user('Unrelated fictional input.', turn_id=extra))
            self.assertEqual(self.scan(), result)
            self.assertGreater(self.frontier()[0], previous[0])
            self.assertEqual(self.count(), 1)

    def test_incomplete_record_does_not_advance_until_stable_complete_snapshot(self):
        self.seed()
        before = self.frontier()
        terminal = self.terminal()
        self.append(event('task_started', self.turn), self.message(), terminal[:-1])
        self.assertEqual(self.scan(), evidence.Observation('waiting', 'incomplete_record'))
        self.assertEqual(self.frontier(), before)
        self.reopen()
        self.append(terminal[-1:])
        self.assertEqual(self.scan().candidate.kind, 'ended')

    def test_armed_scan_returns_only_transient_exact_reply_with_same_pass_proof(self):
        seed = self.map._seed(self.attempt)
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_arm_required$'):
            self.map._scan_armed(self.attempt)
        self.map._arm(seed)
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_generation_changed$'):
            self.map._arm(seed)
        reply = 'Fictional unretained answer. 🧪'
        self.complete('WATCH_REPLY ' + self.owner_id + '\n' + reply)
        verified = self.map._scan_armed(self.attempt)
        self.assertTrue(verified.reply == reply)
        self.assertEqual(verified.observation.candidate.reply_sha256, sha(reply.encode()))
        self.assertNotIn(reply, repr(verified))
        self.assertNotIn(reply.encode(), self.database.read_bytes())
        self.assertEqual(verified.source.snapshot, b'')
        self.map._recheck_armed(self.attempt, verified)
        self.reopen()
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_seed_required$'):
            self.map._arm(seed)
        self.assertEqual(self.map._retained_armed_emission(self.attempt), verified.observation.candidate)
        self.assertTrue(self.map._scan_armed(self.attempt).reply == reply)

    def test_same_size_verified_rewrite_is_withheld_through_restoration_and_reopen(self):
        self.seed()
        self.complete()
        prior = self.scan().candidate
        raw = self.rollout.read_bytes()
        old = self.frontier()
        fresh = raw.replace(b'Fictional source proof.', b'Fictional source spoof.')
        self.assertNotEqual(fresh, raw)
        self.assertEqual(len(fresh), len(raw))
        self.rollout.write_bytes(fresh)
        self.assert_unavailable('verified_prefix_changed')
        self.assertEqual(self.frontier(), old)
        self.assertEqual(self.count(), 1)
        self.rollout.write_bytes(raw)
        self.reopen()
        self.assert_unavailable('retained_source_conflict')
        self.assertEqual(self.map.db.execute('SELECT raw_sha256 FROM emissions').fetchone()[0], prior.raw_sha256)

    def test_floor_rewrite_and_zero_byte_truncation_before_emission_are_not_rebased(self):
        self.seed()
        before = self.frontier()
        self.rollout.write_bytes(b'')
        self.assert_unavailable('verified_prefix_truncated')
        self.assertEqual(self.frontier(), before)
        self.assertEqual(self.count(), 0)

    def test_floor_rewrite_with_equal_size_and_line_count_after_reopen(self):
        original = self.rollout.read_bytes()
        earlier = event('task_started', 'fictional-previous')
        self.append(earlier)
        self.seed()
        self.reopen()
        self.rollout.write_bytes(original + earlier.replace(b'previous', b'replaced'))
        self.complete()
        self.assert_unavailable('verified_prefix_changed')
        self.assertEqual(self.count(), 0)

    def test_same_byte_replacement_cannot_publish_old_identity(self):
        self.seed()
        self.complete()
        previous = self.frontier()
        replacement = self.sessions / 'fictional-replacement.jsonl'
        replacement.write_bytes(self.rollout.read_bytes())
        os.replace(replacement, self.rollout)
        self.assert_unavailable('source_identity_changed')
        self.assertEqual(self.frontier(), previous)

    def test_hardlink_symlink_and_absent_index_never_acquire_other_source(self):
        self.seed()
        self.complete()
        before = self.frontier()
        duplicate = self.sessions / 'fictional-duplicate.jsonl'
        os.link(self.rollout, duplicate)
        self.assert_unavailable()
        duplicate.unlink()
        self.rollout.rename(duplicate)
        self.rollout.symlink_to(duplicate)
        self.assert_unavailable()
        self.rollout.unlink()
        duplicate.rename(self.rollout)
        self.index.unlink()
        self.assert_unavailable()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.frontier(), before)

    def test_native_index_duplicates_with_a_lookup_index_are_rejected_before_rollout(self):
        self.seed()
        self.update_index('DROP TABLE threads')
        self.update_index('CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
        self.update_index('CREATE INDEX threads_id ON threads(id)')
        for _ in range(2):
            self.update_index('INSERT INTO threads VALUES (?,?,?,?,?)', self.index_values())
        with patch.object(evidence, '_snapshot', wraps=evidence._snapshot) as reads:
            self.assert_unavailable('index_cardinality')
            self.assertEqual([c.args[1] for c in reads.call_args_list], [self.attempt.index])
        self.assertEqual(self.count(), 0)

    def test_corrupt_index_wal_and_same_inode_pointer_changes_fail_closed(self):
        self.seed()
        before = self.frontier()
        normal_index = self.index.read_bytes()
        self.index.write_bytes(b'fictional invalid SQLite')
        self.assert_unavailable('index_not_clean')
        self.index.write_bytes(normal_index)
        wal = self.index.with_name(self.index.name + '-wal')
        wal.write_bytes(b'fictional side file')
        self.assert_unavailable('index_not_clean')
        wal.unlink()
        alternate = self.sessions / 'fictional-other.jsonl'
        self.update_index('UPDATE threads SET rollout_path=? WHERE id=?', (str(alternate), self.thread_id))
        self.assert_unavailable('index_selection_changed')
        self.assertEqual(self.frontier(), before)

    def test_index_and_rollout_change_during_a_pass_do_not_commit(self):
        self.seed()
        self.complete()
        before = self.frontier()
        def change_index(point):
            if point == 'after_index_read':
                self.update_index('UPDATE threads SET archived=1 WHERE id=?', (self.thread_id,))
        self.map.checkpoint = change_index
        self.assert_unavailable('source_changed_during_read')
        self.assertEqual(self.frontier(), before)
        self.update_index('UPDATE threads SET archived=0 WHERE id=?', (self.thread_id,))
        def change_source(point):
            if point == 'before_source_recheck':
                self.append(event('task_started', uid()))
        self.map.checkpoint = change_source
        self.assert_unavailable('source_changed_during_read')
        self.assertEqual(self.frontier(), before)
        self.assertEqual(self.count(), 0)
        self.map.checkpoint = lambda _: None
        self.assertEqual(self.scan().candidate.kind, 'ended')

    def test_source_root_project_child_and_continuation_checks(self):
        for update in ({'id': uid()}, {'cwd': str(self.sessions)}, {'parent_thread_id': uid()},
                       {'thread_source': 'guardian_review'}, {'history_base': {'thread_id': self.thread_id}}):
            with self.subTest(update=update):
                self.rollout.write_bytes(line('session_meta', dict(self.header, **update)))
                with self.assertRaises(evidence.EvidenceUnavailable):
                    self.seed()
                self.assertEqual(self.map.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.rollout.write_bytes(line('session_meta', self.header))
        self.seed()
        saved = self.root / 'fictional-project-old'
        self.project.rename(saved)
        self.project.mkdir()
        self.assert_unavailable('source_identity_changed')

    def test_refuse_invalid_record_shapes_and_order_without_advancing(self):
        initial = self.rollout.read_bytes()
        start = event('task_started', self.turn)
        good = self.message()
        end = self.terminal()
        invalid = {
            'invalid UTF-8': [start, b'\xff\n', good, end],
            'duplicate fields': [start, b'{"type":"event_msg","type":"event_msg","payload":{}}\n', good, end],
            'nonfinite': [start, b'{"type":"event_msg","timestamp":1e600,"payload":{}}\n', good, end],
            'escaped lone surrogate': [start, b'{"type":"event_msg","timestamp":"\\ud800","payload":{}}\n', good, end],
            'missing exact turn': [start, user(self.envelope), end],
            'contradictory turn': [start, self.message(turn_id=self.turn, internal_chat_message_metadata_passthrough={'turn_id': uid()}), end],
            'coalesced users': [start, good, user('Fictional other instruction.', turn_id=self.turn), end],
            'duplicate input': [start, good, good, end],
            'another matching turn': [start, good, end, event('task_started', 'second'), self.message('second'), event('task_complete', 'second')],
            'duplicate terminal': [start, good, end, end],
            'terminal before input': [start, end, good],
            'start after input': [good, start, end],
            'abort unsupported': [start, good, event('turn_aborted', self.turn)],
            'native opaque input': [start, line('response_item', {'type': 'message', 'role': 'user', 'turn_id': self.turn,
                'content': [{'type': 'input_text', 'text': self.envelope}, {'type': 'input_image'}]}), end],
        }
        for case, (name, records) in enumerate(invalid.items()):
            with self.subTest(name=name):
                self.rollout.write_bytes(initial)
                with evidence.EvidenceMap(str(self.root / f'fictional-invalid-{case}.sqlite'), create=True) as attempt_map:
                    attempt_map.seed(self.attempt)
                    before = attempt_map.db.execute('SELECT * FROM frontiers').fetchone()[:]
                    self.rollout.write_bytes(initial + b''.join(records))
                    report = attempt_map.scan(self.attempt)
                    self.assertEqual(report.status, 'unavailable')
                    self.assertIsNone(report.candidate)
                    self.assertEqual(attempt_map.db.execute('SELECT * FROM frontiers').fetchone()[:], before)
                    self.assertEqual(attempt_map.db.execute('SELECT COUNT(*) FROM emissions').fetchone()[0], 0)

    def test_marker_mismatch_is_end_candidate_without_text_fallback(self):
        self.seed()
        self.complete('WATCH_REPLY ' + self.owner_id + '\nFictional reply.\nWATCH_REPLY ' + uid())
        c = self.scan().candidate
        self.assertEqual((c.kind, c.ordinal, c.reply_sha256), ('ended', 1, None))

    def test_duplicate_closed_turn_seen_after_candidate_never_changes_ordinal(self):
        self.seed()
        self.complete()
        c = self.scan().candidate
        previous = self.frontier()
        self.append(self.terminal())
        self.assert_unavailable('ambiguous_turn')
        self.assertEqual(self.frontier(), previous)
        self.assertEqual(self.map.db.execute('SELECT raw_sha256,ordinal FROM emissions').fetchone()[:], (c.raw_sha256, 1))

    def assert_first_emission_stays_withheld(self, duplicate_record, reason):
        self.seed()
        previous = self.frontier()
        self.complete()
        clean = self.rollout.read_bytes()
        self.append(duplicate_record)
        self.assert_unavailable(reason)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.frontier(), previous)
        self.rollout.write_bytes(clean)
        self.reopen()
        self.assert_unavailable('retained_source_conflict')
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.frontier(), previous)

    def test_duplicate_terminal_before_first_emission_cannot_heal_after_reopen(self):
        self.assert_first_emission_stays_withheld(self.terminal(), 'ambiguous_turn')

    def test_multiple_matching_inputs_before_first_emission_cannot_heal_after_reopen(self):
        self.assert_first_emission_stays_withheld(self.message(), 'ambiguous_input')

    def test_ambiguity_from_snapshot_changed_before_recheck_does_not_withhold(self):
        self.seed()
        previous = self.frontier()
        self.complete()
        clean = self.rollout.read_bytes()
        self.append(self.terminal())

        def finish_write(point):
            if point == 'before_source_recheck':
                self.rollout.write_bytes(clean)

        self.map.checkpoint = finish_write
        self.assert_unavailable('source_changed_during_read')
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.frontier(), previous)
        self.reopen()
        self.assertEqual(self.scan().candidate.kind, 'ended')
        self.assertEqual(self.count(), 1)

    def test_bounds_on_source_bytes_line_count_and_turn_capacity(self):
        self.seed()
        previous = self.frontier()
        initial = self.rollout.read_bytes()
        with patch.object(evidence, 'MAX_SOURCE', len(initial) + 32):
            self.append(b'z'*33)
            self.assert_unavailable('source_byte_limit')
        self.rollout.write_bytes(initial)
        with patch.object(evidence, 'MAX_LINE', len(initial) + 10):
            self.append(b'z'*(len(initial) + 10))
            self.assert_unavailable('source_line_limit')
        self.rollout.write_bytes(initial)
        self.complete()
        with patch.object(evidence, 'MAX_LINES', 2):
            self.assert_unavailable('source_line_limit')
        self.append(event('task_started', uid()))
        with patch.object(evidence, 'MAX_TURNS', 1):
            self.assert_unavailable('turn_capacity')
        self.assertEqual(self.frontier(), previous)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.scan().candidate.kind, 'ended')

    def test_bound_refusal_after_emission_leaves_existing_verified_map_untouched(self):
        self.seed()
        self.complete()
        report = self.scan()
        prior = self.frontier()
        self.append(event('task_started', uid()))
        with patch.object(evidence, 'MAX_TURNS', 1):
            self.assert_unavailable('turn_capacity')
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.frontier(), prior)
        self.assertEqual(self.map.db.execute('SELECT withheld FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.scan(), report)

    def test_attempt_capacity_missing_map_and_corrupt_reopen(self):
        self.seed()
        for _ in range(evidence.MAX_ATTEMPTS - 1):
            self.map.seed(replace(self.attempt, attempt_id=uid()))
        with patch.object(evidence, '_read_source', side_effect=AssertionError('No source read after capacity refusal.')):
            with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_capacity$'):
                self.map.seed(replace(self.attempt, attempt_id=uid()))
            with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_attempt_exists$'):
                self.seed()
        self.reopen()
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_reopen_read_only_seed$'):
            self.map.seed(replace(self.attempt, attempt_id=uid()))
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_attempt_missing$'):
            self.map.scan(replace(self.attempt, attempt_id=uid()))
        self.map.db.execute('UPDATE attempts SET emitted=1 WHERE id=?', (self.attempt.attempt_id,))
        self.reopen()
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_corrupt$'):
            self.scan()
        self.map.close()
        self.database.unlink()
        with self.assertRaises(evidence.EvidenceUnavailable):
            evidence.EvidenceMap(str(self.database), self.install)
        with self.assertRaises(evidence.EvidenceUnavailable):
            evidence.EvidenceMap(str(self.database), self.install, create=True)
        self.assertFalse(self.database.exists())

    def test_closed_schema_and_exact_pin_before_write(self):
        self.seed()
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_attempt_conflict$'):
            self.map.scan(replace(self.attempt, envelope_sha256='c'*64))
        self.map.db.execute('CREATE TABLE unexpected(value TEXT)')
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_schema$'):
            self.scan()
        with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_schema$'):
            self.reopen()

    def test_before_and_after_commit_fault_recover_exact_mapping_without_renumber(self):
        self.seed()
        before = self.frontier()
        self.complete()
        def lose_at(name):
            def checkpoint(point):
                if point == name:
                    raise RuntimeError('Fictional lost acknowledgement.')
            return checkpoint
        self.map.checkpoint = lose_at('before_commit')
        with self.assertRaises(RuntimeError):
            self.scan()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.frontier(), before)
        self.reopen(checkpoint=lose_at('after_commit'))
        with self.assertRaises(RuntimeError):
            self.scan()
        self.assertEqual(self.count(), 1)
        self.reopen()
        prior = self.map.db.total_changes
        self.assertEqual(self.scan().candidate.ordinal, 1)
        self.assertEqual(self.map.db.total_changes, prior)

    def test_competing_mapper_generation_does_not_publish_second_insert(self):
        self.seed()
        self.complete()
        with evidence.EvidenceMap(str(self.database), self.install) as second:
            result = []
            def win(point):
                if point == 'before_source_recheck':
                    result.append(second.scan(self.attempt))
            self.map.checkpoint = win
            with self.assertRaisesRegex(evidence.EvidenceUnavailable, '^map_generation_changed$'):
                self.scan()
        self.map.checkpoint = lambda _: None
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.scan(), result[0])


class UnmountedEvidenceTests(unittest.TestCase):
    def test_no_production_module_mounts_or_imports_candidate_mapper(self):
        source = Path(__file__).resolve().parents[1] / 'src'
        for file in source.rglob('*.py'):
            if file == source / 'codex_attachment_evidence.py':
                continue
            tree = ast.parse(file.read_text(encoding='utf-8'), filename=file.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(part.name.split('.')[-1] == 'codex_attachment_evidence' for part in node.names), file.name)
                if isinstance(node, ast.ImportFrom):
                    if file == source / 'codex_attachment_adapter.py' and node.module == 'codex_attachment_evidence':
                        continue
                    self.assertNotEqual((node.module or '').split('.')[-1], 'codex_attachment_evidence', file.name)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotEqual(node.value, 'codex_attachment_evidence', file.name)


if __name__ == '__main__':
    unittest.main()
