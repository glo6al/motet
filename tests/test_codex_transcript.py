"""Fictional native metadata and continuation fixtures; never contacts agents."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_transcript as c
import officer_messaging as m
import peer_admin
import peer_bridge
import peer_lifecycle
import peer_progress
from peer_recovery import directory_hash


class FilenameTests(unittest.TestCase):
    def test_rollout_instance_suffix_is_not_the_thread_identity(self):
        thread, instance = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertEqual(c.filename_id('rollout-' + thread + '.jsonl'), thread)
        self.assertEqual(c.filename_id('rollout-' + thread + '_' + instance + '.jsonl'), thread)
        self.assertIsNone(c.filename_id('not-a-native-rollout.jsonl'))

    def test_native_output_metadata_is_supported_only_on_output(self):
        part = dict(type='output_text', text='Fictional answer.', annotations=[], logprobs=None)
        self.assertEqual(c.pure_text([part], 'assistant'), 'Fictional answer.')
        self.assertIsNone(c.pure_text([dict(part, type='input_text')], 'user'))
        self.assertEqual(c.pure_text([dict(type='input_text', text='Fictional instruction.')], 'developer'), 'Fictional instruction.')
        self.assertIsNone(c.pure_text([dict(part, image_url='fictional')], 'assistant'))
        self.assertIsNone(c.pure_text([dict(part, annotations='malformed')], 'assistant'))


class IndexErrorTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(sqlite3, 'SQLITE_LOCKED'), 'SQLite exception codes require Python 3.11')
    def test_index_lock_codes_take_precedence_over_messages(self):
        error = sqlite3.OperationalError('Fictional SQLite diagnostic')
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED | 0x100):
            error.sqlite_errorcode = code
            self.assertTrue(c._sqlite_index_locked(error))
        error = sqlite3.OperationalError('database is locked')
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        self.assertFalse(c._sqlite_index_locked(error))

    def test_index_lock_diagnostics_on_older_python_are_exact(self):
        self.assertTrue(c._sqlite_index_locked(sqlite3.OperationalError('database is locked')))
        self.assertTrue(c._sqlite_index_locked(sqlite3.OperationalError('database table is locked: threads')))
        self.assertFalse(c._sqlite_index_locked(sqlite3.OperationalError('Fictional database is locked label')))
        self.assertFalse(c._sqlite_index_locked(sqlite3.DatabaseError('database is locked')))


@unittest.skipIf(os.name == 'nt', 'POSIX native metadata and private messaging storage')
class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.sessions = self.root / 'sessions'; self.sessions.mkdir()
        self.ident = str(uuid.uuid4())
        self.old = self.sessions / ('rollout-' + self.ident + '.jsonl')
        self.new = self.sessions / ('rollout-' + self.ident + '_' + str(uuid.uuid4()) + '.jsonl')
        self.meta = dict(id=self.ident, session_id=self.ident, cwd=str(self.root), source='vscode')
        self.append(self.old, 'session_meta', self.meta)
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
        self.rid = str(uuid.uuid4())
        st = self.old.stat()
        self.row = dict(request_id=self.rid, agent='x:' + self.ident, origin='peer', runtime='codex',
                        state='queued', created=time.time(), launched=True, path=str(self.old), device=st.st_dev,
                        inode=st.st_ino, cursor=st.st_size, current_turn='', bound_turn='', final='', reply='', error='',
                        envelope='WATCH_PEER_REQUEST ' + self.rid + '\nFictional test only.')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def append(self, path, typ, payload):
        with open(path, 'ab') as f:
            f.write(json.dumps(dict(type=typ, payload=payload)).encode() + b'\n')

    def pointer(self, path, **updates):
        db = sqlite3.connect(self.root / 'state_5.sqlite')
        db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER, source TEXT)')
        value = dict(id=self.ident, rollout_path=str(path), cwd=str(self.root), archived=0, source='vscode'); value.update(updates)
        db.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)', tuple(value[k] for k in ('id','rollout_path','cwd','archived','source')))
        db.commit(); db.close()

    def successor(self, **base_updates):
        raw = self.old.read_bytes()
        base = dict(thread_id=self.ident, end_byte_offset=len(raw), end_ordinal_exclusive=raw.count(b'\n'))
        base.update(base_updates)
        self.append(self.new, 'session_meta', dict(self.meta, history_base=base, history_mode='paginated'))
        self.pointer(self.new)

    def message(self, path, turn, text):
        self.append(path, 'response_item', dict(type='message', role='user', content=[dict(text=text)], internal_chat_message_metadata_passthrough=dict(turn_id=turn)))

    def reply(self, turn='t1', start=True):
        if start: self.append(self.new, 'event_msg', dict(type='task_started', turn_id=turn))
        self.message(self.new, turn, self.row['envelope'])
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id=turn, last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))

    def poll(self, count=8):
        for _ in range(count):
            if self.row['state'] in ('answered', 'quarantined') or self.row.get('tracking_stopped'): break
            self.store._poll(self.row)

    def test_native_pointer_selects_suffixed_rollout_not_suffix_agent(self):
        self.successor()
        path, source, _, _ = c.resolve(str(self.sessions), self.ident)
        source.close()
        self.assertEqual(path, str(self.new))
        self.assertEqual(c.filename_id(self.new.name), self.ident)
        self.assertEqual(self.store._target_log(self.ident)[0], str(self.new))
        with self.assertRaises(m.MessagingError): self.store._metadata(str(self.old), self.ident)

    def test_existing_invalid_native_index_never_falls_back(self):
        (self.root / 'state_5.sqlite').write_bytes(b'not a database')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_index_missing_row_archived_project_or_header_fail_closed(self):
        for changes in [dict(id=str(uuid.uuid4())), dict(archived=1), dict(cwd='/fictional/wrong'), dict(source='exec')]:
            with self.subTest(changes=changes):
                (self.root / 'state_5.sqlite').unlink(missing_ok=True)
                self.pointer(self.old, **changes)
                with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)
        self.pointer(self.old)
        self.old.write_text(json.dumps(dict(type='session_meta', payload=dict(self.meta, id=str(uuid.uuid4()))))+'\n')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_symlink_log_and_index_rejected(self):
        backup = self.root / 'backup'; self.old.rename(backup); self.old.symlink_to(backup)
        with self.assertRaises((OSError, c.Unavailable)): c.resolve(str(self.sessions), self.ident)
        self.old.unlink(); backup.rename(self.old)
        self.pointer(self.old)
        db = self.root / 'state_5.sqlite'; db.rename(self.root/'saved.sqlite'); db.symlink_to(self.root/'saved.sqlite')
        with self.assertRaises(c.Unavailable): c.resolve(str(self.sessions), self.ident)

    def test_replaced_tracking_file_does_not_parse_new_contents(self):
        replacement = self.root / 'replacement'; replacement.write_bytes(self.old.read_bytes())
        os.replace(replacement, self.old)
        self.store._poll(self.row)
        self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['bound_turn'], '')

    def test_clean_continuation_preserves_original_pin_and_answers(self):
        self.successor(); self.reply(); original = self.row['path']
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'answered')
        self.assertEqual(self.row['reply'], 'Fictional answer.')
        self.assertEqual(self.row['path'], original)
        self.assertEqual(self.row['codex_tracking']['path'], str(self.new))
        self.assertTrue(self.row['settled'])

    def test_unread_predecessor_owner_input_is_drained_and_withheld(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='t1'))
        self.message(self.old, 't1', 'Fictional owner input')
        self.successor(); self.reply(start=False)
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown')
        self.assertTrue(self.row['settled']); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['cursor'], self.old.stat().st_size)

    def test_cursor_past_history_base_with_aborted_old_tail_is_conservative(self):
        prefix = self.old.read_bytes()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='old'))
        self.message(self.old, 'old', 'Fictional old owner input')
        self.append(self.old, 'event_msg', dict(type='turn_aborted', turn_id='old'))
        self.row['cursor'] = self.old.stat().st_size
        self.successor(end_byte_offset=len(prefix), end_ordinal_exclusive=prefix.count(b'\n'))
        self.reply(); self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown'); self.assertTrue(self.row['settled'])
        self.assertEqual(self.row['reply'], '')

    def test_mixed_successor_turn_never_publishes(self):
        self.successor(); self.append(self.new, 'event_msg', dict(type='task_started', turn_id='t1'))
        self.message(self.new, 't1', 'Fictional prior owner message')
        self.reply(start=False); self.poll(); self.store.peers.reconcile([self.row])
        self.assertEqual(self.row['state'], 'unknown'); self.assertTrue(self.row['settled'])

    def test_wrong_boundary_is_not_accepted_on_later_ticks(self):
        self.successor(end_ordinal_exclusive=999); self.reply()
        self.poll(5)
        self.assertEqual(self.row['state'], 'unknown'); self.assertNotIn('codex_tracking', self.row)
        self.assertFalse(self.row.get('settled'))

    def test_multiple_hop_or_extra_segment_rejected(self):
        self.successor(); self.reply()
        third = self.sessions / ('rollout-' + self.ident + '_' + str(uuid.uuid4()) + '.jsonl')
        self.append(third, 'session_meta', self.meta)
        self.poll()
        self.assertEqual(self.row['state'], 'unknown'); self.assertNotIn('codex_tracking', self.row)
        third.unlink()
        # A predecessor with its own history base is not a proven root segment.
        self.old.write_text(json.dumps(dict(type='session_meta', payload=dict(self.meta, history_base={})))+'\n')
        self.poll(); self.assertNotIn('codex_tracking', self.row)

    def test_bounded_prefix_progress_survives_restart_without_send(self):
        for _ in range(30): self.append(self.old, 'event_msg', dict(type='fixture', value='x'*80))
        self.row['cursor'] = self.old.stat().st_size
        self.successor(); self.reply()
        with patch.object(m, 'MAX_READ', 512):
            self.store._poll(self.row)
            self.assertEqual(self.row['codex_continuation']['offset'], 512)
            self.store._save(self.row)
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no resend')):
                self.poll(50)
        self.store.peers.reconcile([self.row]); self.assertEqual(self.row['state'], 'answered')

    def test_owner_offset_in_other_segment_does_not_release_quarantine(self):
        self.successor(); self.reply(); self.poll()
        owner = dict(self.row, origin='owner', request_id=str(uuid.uuid4()), state='unknown', bound_turn='', cursor=10**9)
        owner.pop('codex_tracking'); owner.pop('complete_segment', None)
        self.store.peers.reconcile([self.row, owner])
        self.assertEqual(self.row['state'], 'quarantined')
        owner['tracking_stopped'] = True
        self.store.peers.reconcile([self.row, owner])
        self.assertEqual(self.row['state'], 'unknown'); self.assertEqual(self.row['reply'], '')

    def test_native_pointer_change_during_proof_never_switches(self):
        self.successor(); self.reply(); self.store._poll(self.row)
        self.pointer(self.old)
        self.store._poll(self.row)
        self.assertNotIn('codex_tracking', self.row)
        self.assertEqual(self.row['bound_turn'], '')

    def oversized(self, path, size=4500):
        self.append(path, 'compacted', {'opaque': 'x' * size})

    def admit(self, version=2):
        self.client_id, self.sender_id = str(uuid.uuid4()), str(uuid.uuid4())
        if version == 2:
            self.row['bridge_version'] = 2
            self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                (self.client_id, self.row['agent'], '{}', 'a'*64, self.rid, str(uuid.uuid4()),
                 self.row['created'], self.row['created'], 'Fictional'))
        else:
            principal = str(uuid.uuid4())
            self.store.db.execute('INSERT INTO peer_principals VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (principal, 'Fictional recipient', 'codex', self.ident, '/fictional/project',
                 'exact', '{}', 'fictional-secret', 1, 1, self.row['created']))
            self.store.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                (self.sender_id, self.client_id, principal, 'a'*64, self.rid,
                 self.row['created'], self.row['created']))
        self.store._save(self.row)

    def public_result(self, version):
        return (self.store.shared.result(self.client_id) if version == 2
                else self.store.peers.result(self.sender_id, self.client_id))

    def queue_for_progress(self, returncode):
        self.admit()
        self.row.update(state='sending', launched=False)
        self.store.db.execute('UPDATE shared_bridge_requests SET launched=NULL WHERE transport_id=?', (self.rid,))
        self.store._save(self.row)
        self.store._peer_progress_clock = lambda: self.row['created'] + 10

        def authorize(row):
            row['launched'] = True
            self.store.db.execute('UPDATE shared_bridge_requests SET launched=? WHERE transport_id=?',
                                  (row['created'], self.rid))
            self.store.db.commit()

        with patch.object(self.store.shared, 'authorize', side_effect=authorize), patch.object(m.subprocess, 'Popen') as launch:
            launch.return_value.wait.return_value = returncode
            self.store.tick()
            launch.assert_called_once()

    def test_progress_observes_queue_binding_exact_end_and_successful_check(self):
        self.queue_for_progress(0)
        base = self.row['created']
        accepted = peer_progress.load(self.store.db, self.rid)
        self.assertEqual(accepted['accepted_at'], base + 10)
        self.assertIsNone(accepted['claim_issued_at'])
        self.assertIsNone(accepted['started_at'])
        self.assertIsNone(accepted['last_checked_at'])

        self.store._peer_progress_clock = lambda: base + 20
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='foreign'))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='foreign'))
        self.store.tick()
        self.assertEqual(peer_progress.load(self.store.db, self.rid), accepted)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store.tick()
        started = peer_progress.load(self.store.db, self.rid)
        self.assertEqual(started['started_at'], base + 20)
        self.assertEqual(started['last_checked_at'], base + 20)
        self.assertIsNone(started['ended_at'])

        self.store._peer_progress_clock = lambda: base + 30
        held = self.root / 'temporarily-moved'
        self.old.rename(held)
        self.store.tick()
        self.assertEqual(peer_progress.load(self.store.db, self.rid), started)
        held.rename(self.old)
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))
        self.store.tick()
        ended = peer_progress.load(self.store.db, self.rid)
        self.assertEqual(ended['ended_at'], base + 30)
        self.assertEqual(ended['last_checked_at'], base + 30)
        self.assertEqual(ended['started_at'], base + 20)
        self.assertEqual(self.public_result(2)['reply'], 'Fictional answer.')

    def test_progress_rejects_queue_error_as_native_acceptance(self):
        self.queue_for_progress(1)
        self.assertEqual(self.public_result(2)['state'], 'unknown')
        self.assertIsNone(peer_progress.load(self.store.db, self.rid)['accepted_at'])

    def bind_progress_turn(self):
        self.admit()
        self.store._peer_progress_clock = lambda: self.row['created'] + 10
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store.tick()
        prior = peer_progress.load(self.store.db, self.rid)
        self.assertEqual(prior['last_checked_at'], self.row['created'] + 10)
        self.store._peer_progress_clock = lambda: self.row['created'] + 20
        return prior

    def test_progress_check_waits_for_unread_transcript_bytes(self):
        prior = self.bind_progress_turn()
        start = self.old.stat().st_size
        self.append(self.old, 'event_msg', dict(type='token_count'))
        boundary = self.old.stat().st_size
        self.append(self.old, 'event_msg', dict(type='token_count'))
        with patch.object(m, 'MAX_READ', boundary - start):
            self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['bound_turn'], 'bound')
        self.assertEqual(saved['cursor'], boundary)
        self.assertLess(saved['cursor'], self.old.stat().st_size)
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

        self.store.tick()
        verified = peer_progress.load(self.store.db, self.rid)
        self.assertEqual(verified['last_checked_at'], self.row['created'] + 20)

    def test_progress_check_does_not_advance_across_unresolved_gap(self):
        prior = self.bind_progress_turn()
        with self.old.open('ab') as source:
            source.write(b'{unreadable complete record}\n')
        self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['bound_turn'], 'bound')
        self.assertEqual(saved['cursor'], self.old.stat().st_size)
        self.assertTrue(saved.get('codex_gap_needs_envelope'))
        self.assertFalse(saved.get('codex_discard_record'))
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

    def test_progress_check_does_not_advance_during_oversized_record_discard(self):
        prior = self.bind_progress_turn()
        with self.old.open('ab') as source:
            source.write(b'{"type":"compacted","payload":{"opaque":"' + b'x' * 600)
        with patch.object(m, 'MAX_LINE', 200), patch.object(m, 'MAX_READ', 400):
            self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['bound_turn'], 'bound')
        self.assertLess(saved['cursor'], self.old.stat().st_size)
        self.assertTrue(saved.get('codex_gap_needs_envelope'))
        self.assertEqual(saved.get('codex_discard_record'), list(c.segment_key(saved)))
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

    def test_progress_discards_observations_when_conflict_holds_row_in_same_poll(self):
        self.admit()
        prior = peer_progress.load(self.store.db, self.rid)
        self.store._peer_progress_clock = lambda: self.row['created'] + 10
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='original'))
        self.message(self.old, 'original', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='conflict'))
        self.message(self.old, 'conflict', self.row['envelope'])
        self.store.tick()

        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['bound_turn'], 'original')
        self.assertEqual(saved['state'], 'unknown')
        self.assertTrue(saved.get('tracking_stopped'))
        self.assertFalse(saved.get('settled'))
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'uncertain')
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

    def test_progress_bad_clock_cannot_undo_exact_completion(self):
        self.admit()
        self.store._peer_progress_clock = lambda: self.row['created'] + 10
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store.tick()
        prior = peer_progress.load(self.store.db, self.rid)
        self.store._peer_progress_clock = lambda: float('nan')
        self.append(self.old, 'event_msg', dict(type='turn_aborted', turn_id='bound'))
        self.store.tick()
        self.assert_durable_peer_end(gap=False)
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

    def test_progress_does_not_date_a_recovered_historical_abort(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        prior = peer_progress.load(self.store.db, self.rid)
        self.assertTrue(all(value is None for key, value in prior.items() if key != 'version'))
        self.store._peer_progress_clock = lambda: self.row['created'] + 10
        self.store.tick()
        self.assert_durable_peer_end(gap=False)
        self.assertEqual(peer_progress.load(self.store.db, self.rid), prior)

    def assert_withheld_completion(self):
        self.assertTrue(self.row.get('settled'))
        self.assertTrue(self.row.get('tracking_stopped'))
        self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['capture_disposition'], 'withheld')
        for key in ('reply', 'candidate_reply', 'final', 'final_turn'):
            self.assertEqual(self.row.get(key, ''), '')

    def test_oversized_before_explicit_envelope_drains_across_restart_and_withholds(self):
        self.oversized(self.old)
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])],
                    internal_chat_message_metadata_passthrough=dict(turn_id='explicit')))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='explicit',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay private.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600):
            self.store._poll(self.row)
            self.assertTrue(self.row.get('codex_discard_record'))
            self.assertLessEqual(self.row['cursor'], self.old.stat().st_size)
            self.store._save(self.row)
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no resend')):
                self.poll(30)
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled'))
        self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')
        self.assertNotIn('codex_discard_record', self.row)

    def test_oversized_inside_bound_turn_exact_end_releases_only_its_capacity(self):
        self.admit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store._poll(self.row); self.store._save(self.row)
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        other = dict(self.row, request_id=str(uuid.uuid4()), agent='x:'+str(uuid.uuid4()),
                     bound_turn='', state='unknown')
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), other['agent'], '{}', 'b'*64, other['request_id'], str(uuid.uuid4()),
             other['created'], other['created'], 'Fictional other'))
        self.store._save(other)
        self.oversized(self.old)
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nNot enough provenance.'))
        terminal_offset = self.old.stat().st_size
        self.message(self.old, 'later', self.row['envelope'])
        self.row.update(reply='Old reply', candidate_reply='Old candidate', final='Old final', final_turn='bound')
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600):
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no dispatch')):
                self.poll(30); self.store._save(self.row); self.store.tick()
        self.assert_withheld_completion()
        self.assertTrue(self.row.get('codex_gap_needs_envelope'))
        self.assertEqual(c.segment(self.row)['cursor'], terminal_offset)
        self.assertEqual(self.row['bound_turn'], 'bound')
        record = peer_lifecycle.get(self.store.db, self.rid)
        self.assertEqual((record['execution'], record['capture']), ('completed', 'withheld'))
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, other['agent'], time.time(), 900))
        self.assertEqual(self.public_result(2)['reply'], '')

    def test_exact_bound_gap_end_survives_restart_without_answer_or_marker(self):
        self.admit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store._poll(self.row); self.store._save(self.row)
        self.oversized(self.old)
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600):
            self.store._poll(self.row)
            self.assertTrue(self.row.get('codex_discard_record'))
            self.store._save(self.row); self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound'))
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no dispatch')):
                self.poll(30); self.store._save(self.row)
        self.assert_withheld_completion()
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'completed')

    def test_exact_bound_gap_aborts_do_not_read_answer(self):
        class Terminal(dict):
            def get(self, key, *args):
                if key == 'last_agent_message':
                    raise AssertionError('terminal answer must not be inspected')
                return super().get(key, *args)
        for kind in ('task_complete', 'turn_aborted', 'task_aborted'):
            with self.subTest(kind=kind):
                self.row.update(bound_turn='bound', settled=False, tracking_stopped=False,
                                reply='Old reply', candidate_reply='Old candidate', final='Old final', final_turn='bound')
                self.store._codex_gap(self.row)
                self.store._consume(self.row, dict(type='event_msg', payload=Terminal(type=kind, turn_id='bound')))
                self.assert_withheld_completion()

    def native_bound_gap_abort(self, kind):
        self.admit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.oversized(self.old, 900)
        self.append(self.old, 'event_msg', dict(type=kind, turn_id='bound'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600):
            with patch.object(m.subprocess, 'Popen', side_effect=AssertionError('no dispatch')):
                self.poll(); self.store._save(self.row); self.store.tick()
        self.assert_withheld_completion()
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'completed')
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_native_bound_gap_turn_abort_releases_capacity(self):
        self.native_bound_gap_abort('turn_aborted')

    def test_native_bound_gap_task_abort_releases_capacity(self):
        self.native_bound_gap_abort('task_aborted')

    def assert_durable_peer_end(self, *, gap, version=2):
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assert_withheld_completion()
        record = peer_lifecycle.get(self.store.db, self.rid)
        self.assertEqual((record['execution'], record['capture']), ('completed', 'withheld'))
        self.assertEqual(record['evidence']['bound_turn'], 'bound')
        self.assertIs(record['evidence'].get('codex_gap_seen', False), gap)
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        result = self.public_result(version)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.assertEqual('transcript gaps' in result['error'], gap)

    def test_native_gap_then_exact_binding_and_turn_abort_in_one_tick(self):
        self.admit()
        self.oversized(self.old, 900)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])]))
        self.append(self.old, 'event_msg', dict(type='turn_aborted', turn_id='bound'))
        terminal_offset = self.old.stat().st_size
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 2000):
            with patch.object(m.subprocess, 'Popen') as launch:
                self.store.tick(); self.store.tick()
                launch.assert_not_called()
        row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(c.segment(row)['cursor'], terminal_offset)
        self.assert_durable_peer_end(gap=True)

    def test_native_gap_then_exact_binding_and_task_abort_after_restart(self):
        self.admit()
        self.oversized(self.old, 900)
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600):
            with patch.object(m.subprocess, 'Popen') as launch:
                self.store.tick(); self.store.close()
                self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
                self.message(self.old, 'bound', self.row['envelope'])
                self.store.tick()
                self.assertEqual(self.public_result(2)['state'], 'awaiting_reply')
                self.append(self.old, 'event_msg', dict(type='task_aborted', turn_id='bound'))
                self.store.tick()
                launch.assert_not_called()
        self.assert_durable_peer_end(gap=True)

    def test_native_clean_abort_requires_exact_original_binding(self):
        self.admit(version=1)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.append(self.old, 'event_msg', dict(type='task_aborted', turn_id='bound'))
        self.store.tick()
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'submitted')
        self.message(self.old, 'bound', self.row['envelope'])
        for fields in ({}, {'turn_id': ''}, {'turn_id': 'other'}, {'turn_id': ['bound']}):
            self.append(self.old, 'event_msg', dict(type='turn_aborted', **fields))
        self.store.tick()
        self.assertEqual(self.public_result(1)['state'], 'awaiting_reply')
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        self.append(self.old, 'event_msg', dict(type='turn_aborted', turn_id='bound'))
        self.store.tick()
        self.assert_durable_peer_end(gap=False, version=1)
        self.assertIn('was interrupted', self.public_result(1)['error'])

    def test_native_gap_then_exact_binding_and_completion_persists_gap_in_one_tick(self):
        self.admit()
        self.oversized(self.old, 900)
        self.message(self.old, 'bound', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional withheld answer.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600):
            self.store.tick()
        self.assert_durable_peer_end(gap=True)

    def retain_previous_worker_binding(self, terminal=None, *, version=2, prefix=()):
        self.client_id, self.sender_id = str(uuid.uuid4()), str(uuid.uuid4())
        identity = {key: self.row[key] for key in ('path', 'device', 'inode')}
        project, name, question = str(self.root), 'Fictional recipient', 'Fictional request'
        if version == 2:
            credential = peer_bridge.initialize_service(self.store.db, {'history_days': 'all'})
            revision = directory_hash(['codex', self.ident, project, name, identity])
            snapshot = dict(agent_id=self.row['agent'], name=name, provider='codex',
                            project_id=directory_hash(['watch-project-v1', project]), identity_revision=revision)
            digest = self.store.shared._hash(self.row['agent'], revision, question, None)
            self.row.update(bridge_version=2, envelope=peer_bridge.shared_envelope(self.rid, None, question, 'codex'))
            self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                (self.client_id, self.row['agent'], json.dumps(snapshot), digest, self.rid, credential['credential_id'],
                 self.row['created'], self.row['created'], question))
        else:
            sender = peer_bridge.enroll(self.store.db, 'Fictional sender', 'codex', str(uuid.uuid4()), project, identity)
            target = peer_bridge.enroll(self.store.db, name, 'codex', self.ident, project, identity)
            self.sender_id = sender['endpoint_id']
            digest = peer_bridge.mac(sender['secret'], 'watch-peer-dedupe-v1', json.dumps([target['endpoint_id'], question]))
            self.row.update(envelope=peer_bridge.envelope(self.rid, 'Fictional sender', question, 'codex'))
            self.store.db.execute('INSERT INTO peer_requests VALUES (?,?,?,?,?,?,?)',
                (self.sender_id, self.client_id, target['endpoint_id'], digest, self.rid,
                 self.row['created'], self.row['created']))
        self.row['question'] = question
        self.store._save(self.row)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        for kind, payload in prefix:
            self.append(self.old, kind, payload)
        if terminal:
            self.append(self.old, 'event_msg', terminal)

        consume, poll = self.store._consume, self.store._poll
        def previous_consume(row, event):
            if not terminal or event.get('payload') != terminal:
                consume(row, event)

        def previous_worker(row):
            with patch.object(self.store, '_consume', previous_consume):
                poll(row)
            row.pop('codex_peer_terminal_checked', None)

        with patch.object(self.store, '_poll', previous_worker), patch.object(self.store, '_check_previous_codex_terminal', return_value=True):
            for _ in range(8):
                self.store.tick()
                self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
                if c.segment(self.row)['cursor'] == self.old.stat().st_size:
                    break
        self.assertEqual(c.segment(self.row)['cursor'], self.old.stat().st_size)
        self.assertEqual(self.row['bound_turn'], 'bound')
        self.assertIn(peer_lifecycle.get(self.store.db, self.rid)['execution'], ('running', 'uncertain'))
        self.store.close()
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))

    def test_previous_worker_abort_settles_before_late_completion(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        self.store.tick()
        self.assert_durable_peer_end(gap=False)
        self.assertIn('was interrupted', self.public_result(2)['error'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
        self.store.tick()
        self.assert_durable_peer_end(gap=False)
        self.assertIn('was interrupted', self.public_result(2)['error'])

    def test_previous_worker_abort_accepts_symlinked_project(self):
        alias = self.root / 'project-link'
        alias.symlink_to(self.root, target_is_directory=True)
        self.old.write_bytes(b'')
        self.append(self.old, 'session_meta', dict(self.meta, cwd=str(alias)))
        self.row['cursor'] = self.old.stat().st_size
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        self.pointer(self.old, cwd=str(alias))
        self.store.tick()
        self.assert_durable_peer_end(gap=False)

    def assert_held_reply_tracks_to_exact_end(self, path):
        self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertFalse(saved.get('tracking_stopped'))
        self.assertFalse(saved.get('settled'))
        self.assertTrue(saved.get('codex_gap_seen'))
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        self.assertEqual(self.public_result(2)['reply'], '')
        self.append(path, 'event_msg', dict(type='task_complete', turn_id='other'))
        self.store.tick()
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
        self.append(path, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional withheld answer.'))
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            launch.assert_not_called()
        self.assert_durable_peer_end(gap=True)
        self.assertNotIn('was interrupted', self.public_result(2)['error'])

    def test_previous_worker_compacted_prefix_tracks_to_exact_end(self):
        self.retain_previous_worker_binding(prefix=[('compacted', {'message': 'Fictional compaction'})])
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def test_previous_worker_envelope_outside_window_tracks_to_exact_end(self):
        self.retain_previous_worker_binding(prefix=[('event_msg', {'type': 'agent_reasoning', 'text': 'x' * 900_000})] * 3)
        self.assertGreater(self.row['cursor'], m.MAX_READ)
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def test_previous_worker_consumed_image_tracks_to_exact_end(self):
        image = dict(type='message', role='user', content=[dict(type='input_image', image_url='fixture')])
        self.retain_previous_worker_binding(prefix=[('response_item', image)])
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def live_tracking_survives_temporary_source(self, origin, *, lock_index=False):
        if origin == 'peer':
            self.retain_previous_worker_binding()
        else:
            self.row.update(origin='owner', envelope=m.envelope(self.rid, 'Fictional owner question.', version=1))
            self.store._save(self.row)
            self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
            self.message(self.old, 'bound', self.row['envelope'])
        self.store.tick()
        before = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual((before['state'], before['bound_turn']), ('awaiting_reply', 'bound'))
        self.pointer(self.old)
        if lock_index:
            locked = sqlite3.connect(self.root / 'state_5.sqlite')
            try:
                locked.execute('BEGIN EXCLUSIVE')
                self.store.tick()
            finally:
                locked.close()
        else:
            hidden = self.root / 'temporarily-hidden-live-source'
            self.old.rename(hidden)
            try:
                self.store.tick()
            finally:
                hidden.rename(self.old)
        after = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual((after['state'], after['bound_turn'], after['cursor']),
                         (before['state'], before['bound_turn'], before['cursor']))
        self.assertFalse(after.get('tracking_stopped'))
        self.assertFalse(after.get('settled'))
        self.assertIn('will keep checking', after['error'])
        if origin == 'peer':
            self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'running')
        # An empty successful read repairs this diagnostic before any state change.
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            launch.assert_not_called()
        recovered = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual((recovered['state'], recovered['bound_turn']), ('awaiting_reply', 'bound'))
        self.assertEqual(recovered.get('error'), '')
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional recovered answer.'))
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            launch.assert_not_called()
        result = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual((result['state'], result['reply']), ('answered', 'Fictional recovered answer.'))

    def test_successful_read_preserves_an_unrelated_diagnostic(self):
        self.retain_previous_worker_binding()
        self.store.tick()
        row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        row['error'] = 'Fictional diagnostic that is not a temporary-read warning.'
        self.store._save(row)
        self.store.tick()
        after = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(after['state'], 'awaiting_reply')
        self.assertEqual(after['error'], row['error'])

    def test_live_owner_missing_source_keeps_awaiting_then_answers(self):
        self.live_tracking_survives_temporary_source('owner')

    def test_live_peer_missing_source_keeps_running_then_answers(self):
        self.live_tracking_survives_temporary_source('peer')

    def test_live_owner_locked_index_keeps_awaiting_then_answers(self):
        self.live_tracking_survives_temporary_source('owner', lock_index=True)

    def test_live_peer_locked_index_keeps_running_then_answers(self):
        self.live_tracking_survives_temporary_source('peer', lock_index=True)

    def test_previous_worker_unfollowed_successor_waits_until_successor_can_open(self):
        self.retain_previous_worker_binding()
        self.successor()
        hidden = self.root / 'temporarily-hidden-successor'
        self.new.rename(hidden)
        try:
            self.store.tick()
            self.assert_previous_proof_waiting()
        finally:
            hidden.rename(self.new)
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late successor answer.'))
        with patch.object(m.subprocess, 'Popen') as launch:
            for _ in range(5):
                self.store.tick()
            launch.assert_not_called()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('answered', 'Fictional late successor answer.'))
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_bound_peer_age_does_not_expire_and_late_exact_end_is_captured(self):
        self.retain_previous_worker_binding()
        for elapsed in (m.TTL, m.TTL + 1, 6 * 3600, m.RETENTION - 1):
            with self.subTest(elapsed=elapsed), patch.object(m.time, 'time', return_value=self.row['created'] + elapsed):
                self.store.tick()
                saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
                self.assertEqual(saved['state'], 'awaiting_reply')
                self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'running')
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional long-work answer.'))
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.RETENTION - 1), patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            launch.assert_not_called()
        self.assertEqual(self.public_result(2)['reply'], 'Fictional long-work answer.')

    def test_previous_peer_proof_read_failure_does_not_expire_by_age(self):
        self.retain_previous_worker_binding()
        hidden = self.root / 'temporarily-hidden-long-work'
        self.old.rename(hidden)
        try:
            with patch.object(m.time, 'time', return_value=self.row['created'] + 6 * 3600):
                self.store.tick()
            saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            self.assertEqual(saved['state'], 'awaiting_reply')
            self.assertFalse(saved.get('tracking_stopped'))
        finally:
            hidden.rename(self.old)

    def test_owner_age_expiry_is_unchanged(self):
        self.row.update(origin='owner', envelope=m.envelope(self.rid, 'Fictional owner question.', version=1))
        self.store._save(self.row)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store.tick()
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.TTL + 1):
            self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['state'], 'expired')
        self.assertEqual(saved['error'], m.CODEX_REPLY_EXPIRED)

    def test_peer_retention_prune_holds_bound_attempt_through_late_end(self):
        self.retain_previous_worker_binding()
        cutoff = self.row['created'] + m.RETENTION
        with patch.object(m.time, 'time', return_value=cutoff):
            self.store.tick()
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'running')
        with patch.object(m.time, 'time', return_value=cutoff + 1), patch.object(self.store, '_poll') as poll:
            self.store.tick()
            poll.assert_not_called()
        self.assert_retention_hold()
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
        with patch.object(m.time, 'time', return_value=cutoff + 3600), patch.object(self.store, '_poll') as poll, patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            poll.assert_not_called()
            launch.assert_not_called()
            self.assert_retention_hold()
            result = self.public_result(2)
            self.assertEqual((result['state'], result['reply']), ('expired', ''))
            self.assertTrue(peer_admin.inspect(self.store, {'transport_id': self.rid})['eligible'])
        stale = dict(self.row, state='answered', settled=True, reply='Fictional stale reply.')
        with self.store.db:
            peer_lifecycle.reconcile(self.store.db, stale)
        self.assert_retention_hold()
        self.store._save(dict(stale))
        self.assert_retention_hold()
        retired = peer_admin.retire(self.store, {'transport_id': self.rid, 'agent': self.row['agent'],
                                                 'confirm': peer_admin.CONFIRM})
        self.assertEqual((retired['status'], retired['provider_outcome']), ('retired', 'unknown'))

    def assert_retention_hold(self):
        self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (self.rid,)).fetchone())
        execution = peer_lifecycle.get(self.store.db, self.rid)
        self.assertEqual((execution['execution'], execution['capture']), ('uncertain', 'withheld'))
        self.assertEqual(execution['evidence']['bound_turn'], 'bound')
        self.assertEqual(execution['evidence']['codex_retention_hold'],
                         'Content retention passed before an end was observed.')
        self.assertIn('requires recovery', peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), m.TTL))

    def test_peer_retention_holds_bound_attempt_when_bulk_prunes_invalid_payload(self):
        self.retain_previous_worker_binding()
        self.store.db.execute('UPDATE requests SET data=? WHERE id=?', ('{}', self.rid))
        self.store.db.commit()
        self.assertEqual(self.store._rows(), [])
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.RETENTION + 1):
            self.store.tick()
        self.assert_retention_hold()

    def test_peer_retention_repairs_already_absent_bound_payload(self):
        self.retain_previous_worker_binding()
        self.store.db.execute('DELETE FROM requests WHERE id=?', (self.rid,))
        self.store.db.commit()
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.RETENTION + 1):
            self.store.tick()
        self.assert_retention_hold()

    def test_peer_retention_does_not_mutate_protected_alias(self):
        self.retain_previous_worker_binding()
        marker = json.dumps({'attachment_owner_version': 1})
        self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (marker, self.rid))
        self.store.db.commit()
        before = peer_lifecycle.get(self.store.db, self.rid)
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.RETENTION + 1):
            self.store.tick()
        self.assertEqual(self.store.db.execute('SELECT data FROM requests WHERE id=?', (self.rid,)).fetchone(), (marker,))
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid), before)

    def test_peer_retention_pruned_launched_unbound_attempt_remains_reserved(self):
        self.admit()
        with patch.object(m.time, 'time', return_value=self.row['created'] + m.RETENTION + 1):
            self.store.tick()
        self.assertIsNone(self.store.db.execute('SELECT data FROM requests WHERE id=?', (self.rid,)).fetchone())
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'submitted')

    def test_previous_worker_unfollowed_successor_tracks_to_exact_end(self):
        self.retain_previous_worker_binding()
        self.successor()
        with patch.object(m.subprocess, 'Popen') as launch:
            for _ in range(4):
                self.store.tick()
            saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            self.assertFalse(saved.get('tracking_stopped'))
            self.assertEqual(saved['codex_tracking']['path'], str(self.new))
            self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
            self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='other'))
            self.store.tick()
            self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
            self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound',
                        last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional verified successor answer.'))
            self.store.tick()
            launch.assert_not_called()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('answered', 'Fictional verified successor answer.'))
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_previous_worker_unfollowed_successor_proof_survives_restart(self):
        self.retain_previous_worker_binding(prefix=[('event_msg', {'type': 'agent_reasoning', 'text': 'x' * 2_500})])
        self.successor()
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional withheld answer.'))
        with patch.object(m, 'MAX_READ', 512), patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            self.assertFalse(saved.get('tracking_stopped'))
            self.assertEqual(saved['codex_continuation']['offset'], 512)
            self.assertNotIn('codex_tracking', saved)
            self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            for _ in range(24):
                self.store.tick()
            launch.assert_not_called()
        self.assert_durable_peer_end(gap=True)

    def test_previous_worker_successor_does_not_skip_unfinished_original_proof(self):
        self.retain_previous_worker_binding()
        self.successor()
        # First tick records a bounded prefix; the next can prove the chain.
        self.store.tick()
        with patch.object(self.store, '_previous_codex_envelope', side_effect=BlockingIOError()):
            self.store.tick()
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertNotIn('codex_tracking', saved)
        self.assert_previous_proof_waiting()
        with patch.object(self.store, '_previous_codex_envelope', side_effect=ValueError()) as original:
            self.store.tick()
            original.assert_called_once()
        self.assert_previous_proof_held()

    def test_previous_worker_unfollowed_successor_waits_for_original_to_return(self):
        self.retain_previous_worker_binding()
        self.successor()
        hidden = self.root / 'temporarily-hidden-predecessor'
        self.old.rename(hidden)
        self.store.tick()
        self.assert_previous_proof_waiting()
        hidden.rename(self.old)
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional restored successor answer.'))
        with patch.object(m.subprocess, 'Popen') as launch:
            for _ in range(6):
                self.store.tick()
            launch.assert_not_called()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('answered', 'Fictional restored successor answer.'))
        self.assertIsNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_previous_worker_unfollowed_successor_rejects_wrong_boundary(self):
        self.retain_previous_worker_binding()
        self.successor(end_ordinal_exclusive=999)
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional unproven answer.'))
        for _ in range(4):
            self.store.tick()
        self.assert_previous_proof_held()
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_previous_worker_successor_tracks_to_exact_end(self):
        self.retain_previous_worker_binding()
        self.successor()
        with patch.object(self.store, '_check_previous_codex_terminal', return_value=True):
            self.store.tick()
            self.store.tick()
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(self.row['codex_tracking']['path'], str(self.new))
        self.row.pop('codex_peer_terminal_checked')
        self.store._save(self.row)
        self.assert_held_reply_tracks_to_exact_end(self.new)

    def test_previous_worker_partial_discard_tracks_to_exact_end(self):
        self.retain_previous_worker_binding()
        with self.old.open('ab') as source:
            source.write(b'{"type":"compacted","payload":{"message":"' + b'x' * 2_000)
        with patch.object(m, 'MAX_LINE', 400), patch.object(m, 'MAX_READ', 800):
            with patch.object(self.store, '_check_previous_codex_terminal', return_value=True):
                self.store.tick()
            self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
            self.assertTrue(self.row.get('codex_discard_record'))
            self.row.pop('codex_peer_terminal_checked')
            self.store._save(self.row)
        with self.old.open('ab') as source:
            source.write(b'"}}\n')
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def assert_previous_proof_waiting(self):
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(saved['cursor'], self.row['cursor'])
        self.assertFalse(saved.get('tracking_stopped'))
        self.assertNotIn('codex_peer_terminal_checked', saved)
        self.assertEqual(self.public_result(2)['reply'], '')

    def test_previous_worker_abort_recovers_when_original_transcript_returns(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        original = self.root / 'temporarily-moved'
        self.old.rename(original)
        with patch.object(m.subprocess, 'Popen') as launch:
            self.store.tick()
            self.assert_previous_proof_waiting()
            self.store.close()
            self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
            self.append(original, 'event_msg', dict(type='task_complete', turn_id='bound',
                        last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
            original.rename(self.old)
            self.store.tick()
            launch.assert_not_called()
        self.assert_durable_peer_end(gap=False)

    def test_previous_worker_retries_when_pinned_transcript_grows_during_proof(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        stat = os.stat

        def append_before_stat(path, *args, **kwargs):
            if path == str(self.old) and kwargs.get('follow_symlinks') is False:
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
            return stat(path, *args, **kwargs)

        with patch.object(m.os, 'stat', append_before_stat):
            self.store.tick()
        self.assert_previous_proof_waiting()
        self.store.tick()
        self.assert_durable_peer_end(gap=False)

    def test_previous_worker_retries_when_native_index_is_locked(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        self.pointer(self.old)
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional late answer.'))
        with patch.object(m.subprocess, 'Popen') as launch:
            lock = sqlite3.connect(self.root / 'state_5.sqlite')
            try:
                lock.execute('BEGIN EXCLUSIVE')
                self.store.tick()
                self.assert_previous_proof_waiting()
            finally:
                lock.close()
            self.store.tick()
            launch.assert_not_called()
        self.assert_durable_peer_end(gap=False)

    def test_previous_worker_retries_when_native_index_changes_during_validation(self):
        self.retain_previous_worker_binding(dict(type='task_aborted', turn_id='bound'))
        self.pointer(self.old)
        before = c.index(str(self.root), self.ident)
        with patch.object(c, 'index', side_effect=[before, None]):
            self.store.tick()
        self.assert_previous_proof_waiting()
        self.store.tick()
        self.assert_durable_peer_end(gap=False)

    def assert_previous_proof_held(self):
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertTrue(saved.get('tracking_stopped'))
        self.assertEqual(saved['cursor'], self.row['cursor'])
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'uncertain')
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))

    def test_previous_worker_holds_when_native_index_is_corrupt(self):
        self.retain_previous_worker_binding(dict(type='task_aborted', turn_id='bound'))
        (self.root / 'state_5.sqlite').write_bytes(b'Fictional invalid SQLite header')
        self.store.tick()
        self.assert_previous_proof_held()

    def test_previous_worker_holds_when_native_index_selects_another_transcript(self):
        self.retain_previous_worker_binding(dict(type='task_aborted', turn_id='bound'))
        # A readable file without continuation metadata is contradictory; a
        # temporarily unopenable native pointer is retryable, not that proof.
        self.append(self.new, 'session_meta', self.meta)
        self.pointer(self.new)
        self.store.tick()
        self.assert_previous_proof_held()

    def test_previous_worker_holds_when_prefix_has_exact_completion(self):
        self.retain_previous_worker_binding(dict(type='task_complete', turn_id='bound'))
        self.store.tick()
        self.assert_previous_proof_held()

    def test_previous_worker_holds_when_prefix_has_duplicate_envelope(self):
        self.retain_previous_worker_binding()
        self.message(self.old, 'bound', self.row['envelope'])
        with patch.object(self.store, '_check_previous_codex_terminal', return_value=True):
            self.store._poll(self.row)
        self.row.pop('codex_peer_terminal_checked')
        self.store._save(self.row)
        self.store.tick()
        self.assert_previous_proof_held()

    def test_previous_worker_legacy_abort_settles_with_legacy_admission(self):
        self.retain_previous_worker_binding(dict(type='task_aborted', turn_id='bound'), version=1)
        self.store.tick()
        self.assert_durable_peer_end(gap=False, version=1)

    def test_previous_worker_ambiguous_abort_cannot_publish_late_reply(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted'))
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def test_previous_worker_bounded_recovery_requires_original_envelope(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        with patch.object(m, 'MAX_READ', 100):
            self.store.tick()
        self.assert_held_reply_tracks_to_exact_end(self.old)

    def test_previous_worker_abort_requires_authentic_admission_envelope(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        self.store.db.execute('UPDATE shared_bridge_requests SET payload_hash=? WHERE transport_id=?', ('f' * 64, self.rid))
        self.store.db.commit()
        self.store.tick()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'uncertain')
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertTrue(saved['tracking_stopped'])

    def test_previous_worker_abort_rejects_transcript_replacement(self):
        self.retain_previous_worker_binding(dict(type='turn_aborted', turn_id='bound'))
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.old.read_bytes())
        os.replace(replacement, self.old)
        self.store.tick()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'uncertain')
        saved = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertTrue(saved['tracking_stopped'])

    def test_previous_worker_unrelated_abort_allows_original_reply(self):
        self.retain_previous_worker_binding(dict(type='task_aborted', turn_id='other'))
        self.store.tick()
        self.store.close()
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional actual answer.'))
        with patch.object(self.store, '_check_previous_codex_terminal') as recheck:
            self.store.tick()
            recheck.assert_not_called()
        result = self.public_result(2)
        self.assertEqual((result['state'], result['reply']), ('answered', 'Fictional actual answer.'))

    def test_gap_end_requires_exact_nonempty_binding(self):
        baseline = dict(self.row)
        for binding, terminal in [('bound', {}), ('bound', {'turn_id':''}), ('bound', {'turn_id':'wrong'}),
                                  ('bound', {'turn_id':[]}), ('', {'turn_id':'unbound'})]:
            with self.subTest(binding=binding, terminal=terminal):
                self.row = dict(baseline, bound_turn=binding)
                self.store._codex_gap(self.row)
                for kind in ('task_complete', 'turn_aborted', 'task_aborted'):
                    self.store._consume(self.row, dict(type='event_msg', payload=dict(type=kind, **terminal)))
                self.assertFalse(self.row.get('settled'))
                self.assertFalse(self.row.get('tracking_stopped'))
                self.assertEqual(self.row['reply'], '')

    def test_incomplete_oversized_end_record_cannot_settle_bound_gap(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        with open(self.old, 'ab') as source:
            source.write(json.dumps(dict(type='event_msg', payload=dict(type='task_complete', turn_id='bound',
                              last_agent_message='x'*4500))).encode())
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 600): self.poll(30)
        self.assertFalse(self.row.get('settled'))
        self.assertTrue(self.row.get('codex_discard_record'))
        self.assertEqual(self.row['reply'], '')
        with open(self.old, 'ab') as source: source.write(b'\n')
        self.poll()
        self.assertFalse(self.row.get('settled'))  # Draining an unreadable end is not completion proof.

    def bind_and_gap(self):
        self.admit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.oversized(self.old, 900)
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll()
        self.store._save(self.row)
        self.assertEqual(self.row['bound_turn'], 'bound')
        self.assertTrue(self.row.get('codex_gap_needs_envelope'))

    def test_replaced_bound_gap_transcript_cannot_prove_completion(self):
        self.bind_and_gap()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.old.read_bytes())
        self.append(replacement, 'event_msg', dict(type='task_complete', turn_id='bound'))
        os.replace(replacement, self.old)
        self.poll(); self.store._save(self.row)
        self.assertFalse(self.row.get('settled'))
        self.assertEqual(self.row['reply'], '')
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def test_invalid_bound_gap_continuation_cannot_prove_completion(self):
        self.bind_and_gap()
        self.successor(end_ordinal_exclusive=999)
        self.append(self.new, 'event_msg', dict(type='task_complete', turn_id='bound'))
        self.poll(); self.store._save(self.row)
        self.assertFalse(self.row.get('settled'))
        self.assertEqual(self.row['reply'], '')
        self.assertIsNotNone(peer_lifecycle.capacity_error(self.store.db, self.row['agent'], time.time(), 900))

    def stale_answer_save_stays_withheld(self, version):
        self.admit(version)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
        self.message(self.old, 'bound', self.row['envelope'])
        self.store._poll(self.row); self.store._save(self.row)
        stale = dict(self.row, state='answered', settled=True, capture_disposition='captured',
                     tracking_stopped=False, reply='Fictional late answer', candidate_reply='Late candidate',
                     final='Late final', final_turn='bound')
        self.oversized(self.old, 900)
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll()
        self.store._save(self.row)
        self.store._save(dict(stale))  # Exercise the real save ordering, not a hand-written UPDATE.
        with self.assertRaisesRegex(ValueError, 'original attempt'):
            self.store._save(dict(stale, bound_turn='conflicting-turn'))
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assert_withheld_completion()
        result = self.public_result(version)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.store.close()
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assert_withheld_completion()
        self.assertEqual(self.public_result(version)['state'], 'unknown')
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['capture'], 'withheld')
        self.store.db.execute('DELETE FROM requests WHERE id=?', (self.rid,)); self.store.db.commit()
        self.store._save(dict(stale))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (self.rid,)).fetchone())
        result = self.public_result(version)
        self.assertEqual((result['state'], result['reply']), ('unknown', ''))
        self.store.close()
        self.store = m.MessagingStore(str(self.root / 'watch'), str(self.sessions))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (self.rid,)).fetchone())
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], 'completed')
        self.assertEqual(self.public_result(version)['reply'], '')

    def test_shared_stale_answer_save_and_pruning_preserve_withheld_result(self):
        self.stale_answer_save_stays_withheld(2)

    def test_legacy_stale_answer_save_and_pruning_preserve_withheld_result(self):
        self.stale_answer_save_stays_withheld(1)

    def pruned_terminal_payload_stays_absent(self, execution):
        self.admit()
        stale = dict(self.row, state='answered', bound_turn='late', settled=True,
                     reply='Fictional late answer', candidate_reply='Late candidate', final='Late final', final_turn='late')
        if execution == 'retired':
            peer_lifecycle.retire(self.store.db, self.rid, peer_lifecycle.RETIRE_REASON)
        else:
            self.store._save(dict(self.row, state='failed', execution_outcome='dropped', settled=True))
        self.store.db.execute('DELETE FROM requests WHERE id=?', (self.rid,)); self.store.db.commit()
        self.store._save(dict(stale))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM requests WHERE id=?', (self.rid,)).fetchone())
        self.assertEqual(self.public_result(2)['reply'], '')
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['execution'], execution)

    def test_save_does_not_recreate_pruned_retired_payload(self):
        self.pruned_terminal_payload_stays_absent('retired')

    def test_save_does_not_recreate_pruned_dropped_payload(self):
        self.pruned_terminal_payload_stays_absent('dropped')

    def test_oversized_gap_fresh_started_envelope_proves_completion_not_capture(self):
        self.oversized(self.old, 900)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='fresh'))
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])]))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='fresh',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWithheld.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll(8)
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['state'], 'unknown')
        self.assertEqual(self.row['reply'], '')

    def test_gap_does_not_trust_turn_context_without_fresh_start_or_explicit_envelope(self):
        self.oversized(self.old, 900)
        self.append(self.old, 'turn_context', dict(turn_id='unproven'))
        self.append(self.old, 'response_item', dict(type='message', role='user', content=[dict(text=self.row['envelope'])]))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='unproven',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nUnproven.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll(8)
        self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')

    def test_malformed_or_nondict_after_binding_withholds_but_exact_end_settles(self):
        original = json.loads(json.dumps(self.row))
        header = self.old.read_bytes()
        for malformed in (b'{broken native record}\n', b'[]\n', b'null\n', b'{"type":"event_msg","payload":[]}\n'):
            with self.subTest(record=malformed):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
                self.message(self.old, 'bound', self.row['envelope'])
                with open(self.old, 'ab') as source: source.write(malformed)
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nNo provenance.'))
                self.poll()
                self.assert_withheld_completion()
                self.assertTrue(self.row.get('codex_gap_needs_envelope'))
                self.assertEqual(self.row['reply'], '')

    def test_malformed_before_fresh_explicit_envelope_settles_withheld(self):
        with open(self.old, 'ab') as source: source.write(b'{broken native record}\n')
        self.message(self.old, 'explicit', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='explicit',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay withheld.'))
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_native_single_lf_envelope_normalization_is_narrow(self):
        original = json.loads(json.dumps(self.row))
        header = self.old.read_bytes()
        for suffix in ('', '\n', '\r\n', '\n\n', ' ', '\nExtra user instruction'):
            with self.subTest(suffix=repr(suffix)):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='native'))
                self.message(self.old, 'native', self.row['envelope'] + suffix)
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='native',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assertEqual(bool(self.row.get('settled')), suffix in ('', '\n'))
                self.assertEqual(self.row['envelope'], original['envelope'])
                if suffix not in ('', '\n'): self.assertEqual(self.row['reply'], '')

    def test_postgap_conflicting_turn_preserves_durable_binding_without_worker_failure(self):
        self.row['bridge_version'] = 2
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), self.row['agent'], '{}', 'a'*64, self.row['request_id'], str(uuid.uuid4()),
             self.row['created'], time.time(), 'Fictional'))
        self.store.db.commit()
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='original'))
        self.message(self.old, 'original', self.row['envelope'])
        self.store._poll(self.row); self.store._save(self.row)
        self.assertEqual(peer_lifecycle.get(self.store.db,self.rid)['evidence']['bound_turn'], 'original')
        self.oversized(self.old, 900)
        self.message(self.old, 'different', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='different',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWrong turn.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.store.tick()
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(self.row['bound_turn'], 'original')
        self.assertEqual(self.row['state'], 'unknown'); self.assertFalse(self.row.get('settled'))
        self.assertEqual(self.row['reply'], ''); self.assertFalse(self.store.worker_failed)
        self.assertEqual(peer_lifecycle.get(self.store.db,self.rid)['evidence']['bound_turn'], 'original')

    def test_same_poll_conflicting_peer_turn_preserves_first_binding(self):
        self.row['bridge_version'] = 2
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), self.row['agent'], '{}', 'a'*64, self.rid, str(uuid.uuid4()),
             self.row['created'], time.time(), 'Fictional'))
        self.store.db.commit(); self.store._save(self.row)
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='original'))
        self.message(self.old, 'original', self.row['envelope'])
        self.message(self.old, 'different', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='different',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWrong turn.'))
        self.store.tick()
        self.row = next(row for row in self.store._rows() if row['request_id'] == self.rid)
        self.assertEqual(self.row['bound_turn'], 'original')
        self.assertFalse(self.row.get('settled')); self.assertFalse(self.store.worker_failed)
        self.assertEqual(self.row['reply'], '')
        self.assertEqual(peer_lifecycle.get(self.store.db, self.rid)['evidence']['bound_turn'], 'original')

    def test_same_turn_fresh_postgap_envelope_can_settle_only_withheld(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='same'))
        self.message(self.old, 'same', self.row['envelope'])
        self.oversized(self.old, 900)
        self.message(self.old, 'same', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='same',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nWithheld.'))
        with patch.object(m, 'MAX_LINE', 300), patch.object(m, 'MAX_READ', 1600): self.poll()
        self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['bound_turn'], 'same')
        self.assertEqual(self.row['state'], 'unknown'); self.assertEqual(self.row['reply'], '')

    def test_unverifiable_foreign_message_metadata_withholds_but_exact_end_settles(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        variants = [dict(internal_chat_message_metadata_passthrough=[]), dict(turn_id=[]),
                    dict(turn_id='bound', internal_chat_message_metadata_passthrough=dict(turn_id='different'))]
        for metadata in variants:
            with self.subTest(metadata=metadata):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='bound'))
                self.message(self.old, 'bound', self.row['envelope'])
                self.append(self.old, 'response_item', dict(type='message', role='user',
                            content=[dict(text='Fictional owner input')], **metadata))
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='bound',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nMust not escape.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assert_withheld_completion(); self.assertTrue(self.row.get('codex_gap_needs_envelope'))
                self.assertEqual(self.row['reply'], ''); self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_pure_native_user_text_content_shapes_remain_correlatable(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        for kind in ('input_text', 'text', None):
            with self.subTest(kind=kind):
                self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                part = dict(text=self.row['envelope'])
                if kind is not None: part['type'] = kind
                self.append(self.old, 'event_msg', dict(type='task_started', turn_id='pure'))
                self.append(self.old, 'response_item', dict(type='message', role='user', content=[part],
                            internal_chat_message_metadata_passthrough=dict(turn_id='pure')))
                self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='pure',
                            last_agent_message='WATCH_REPLY ' + self.rid + '\nFictional answer.'))
                self.poll(); self.store.peers.reconcile([self.row])
                self.assertEqual(self.row['state'], 'answered')
                self.assertEqual(self.row['reply'], 'Fictional answer.')

    def test_extra_native_image_or_malformed_part_cannot_be_dropped_from_envelope(self):
        original = json.loads(json.dumps(self.row)); header = self.old.read_bytes()
        extras = [dict(type='input_image', image_url='fictional-image'), dict(type='image', source='fictional'),
                  dict(type='unknown', text=''), dict(type='input_text', text=123), 'invalid part', None]
        for kind in ('input_text', 'text'):
            for extra in extras:
                with self.subTest(kind=kind,extra=extra):
                    self.old.write_bytes(header); self.row = json.loads(json.dumps(original))
                    self.append(self.old, 'event_msg', dict(type='task_started', turn_id='mixed'))
                    self.append(self.old, 'response_item', dict(type='message', role='user',
                                content=[dict(type=kind,text=self.row['envelope']),extra],
                                internal_chat_message_metadata_passthrough=dict(turn_id='mixed')))
                    self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                                last_agent_message='WATCH_REPLY ' + self.rid + '\nPrivate combined answer.'))
                    self.poll(); self.store.peers.reconcile([self.row])
                    self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
                    self.assertEqual(self.row['capture_disposition'], 'withheld')
                    self.assertTrue(self.row.get('codex_gap_needs_envelope'))

    def test_unknown_content_field_is_not_assumed_to_be_text_metadata(self):
        self.append(self.old, 'event_msg', dict(type='task_started', turn_id='mixed'))
        self.append(self.old, 'response_item', dict(type='message', role='user',
                    content=[dict(type='input_text',text=self.row['envelope'],image_url='fictional-image')],
                    internal_chat_message_metadata_passthrough=dict(turn_id='mixed')))
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nMust stay withheld.'))
        self.poll(); self.assertFalse(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        # Only later pure input can restore execution evidence, never clean capture.
        self.message(self.old, 'mixed', self.row['envelope'])
        self.append(self.old, 'event_msg', dict(type='task_complete', turn_id='mixed',
                    last_agent_message='WATCH_REPLY ' + self.rid + '\nStill withheld.'))
        self.poll(); self.store.peers.reconcile([self.row])
        self.assertTrue(self.row.get('settled')); self.assertEqual(self.row['reply'], '')
        self.assertEqual(self.row['capture_disposition'], 'withheld')

    def test_postauthorization_failure_only_releases_proven_no_native_submission(self):
        for failure in ('metadata', 'spawn', 'nonzero', 'timeout'):
            with self.subTest(failure=failure):
                row = dict(self.row, request_id=str(uuid.uuid4()), bridge_version=2, state='sending')
                self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), row['agent'], '{}', 'a'*64, row['request_id'], str(uuid.uuid4()), row['created'], time.time(), 'Fictional'))
                self.store.db.commit()
                process = Mock(); process.wait.return_value = 1
                if failure == 'timeout': process.wait.side_effect = m.subprocess.TimeoutExpired('fixture', 10)
                with patch.object(self.store, '_metadata', side_effect=m.MessagingError(400, 'fixture')) if failure == 'metadata' else patch.object(self.store, '_metadata', wraps=self.store._metadata):
                    with patch.object(m.subprocess, 'Popen', side_effect=OSError('fixture')) if failure == 'spawn' else patch.object(m.subprocess, 'Popen', return_value=process):
                        row['state'], row['error'] = self.store._queue_codex(row, row['envelope'])
                self.store._save(row)
                state = peer_lifecycle.get(self.store.db, row['request_id'])
                self.assertEqual(state['execution'], 'dropped' if failure in ('metadata','spawn') else 'uncertain')


if __name__ == '__main__': unittest.main()
