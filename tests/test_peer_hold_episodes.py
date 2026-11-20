"""Durable hold dates from real lifecycle transitions in fictional sessions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging as messaging
import peer_lifecycle as life


def uid():
    return str(uuid.uuid4())


@unittest.skipIf(os.name == 'nt', 'POSIX native messaging storage')
class HoldEpisodeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root / 'codex' / 'sessions'
        self.sessions.mkdir(parents=True)
        self.native, self.tid = uid(), uid()
        self.created = 1_700_000_000
        self.launched = self.created + 1
        self.path = self.sessions / ('rollout-' + self.native + '.jsonl')
        self.append('session_meta', id=self.native, cwd=str(self.root), source='vscode')
        pin = self.path.stat()
        self.row = dict(request_id=self.tid, agent='x:' + self.native, origin='peer', bridge_version=2,
                        runtime='codex', state='queued', created=self.created, launched=True,
                        path=str(self.path), device=pin.st_dev, inode=pin.st_ino, cursor=pin.st_size,
                        envelope='WATCH_PEER_REQUEST ' + self.tid + '\nFictional question.',
                        current_turn='', bound_turn='', final='', reply='', error='')
        self.store = self.open_store()
        self.addCleanup(lambda: self.store.close())
        self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,?,?)',
                              (uid(), self.row['agent'], '{}', 'fictional-hash', self.tid, uid(),
                               self.created, self.launched, 'Fictional preview'))
        self.store._save(self.row)

    def open_store(self):
        return messaging.MessagingStore(str(self.root / 'state'), str(self.sessions))

    def restart(self):
        self.store.close()
        self.store = self.open_store()

    def append(self, kind, **payload):
        with self.path.open('a') as transcript:
            transcript.write(json.dumps(dict(type=kind, payload=payload)) + '\n')

    def tick(self, now):
        with patch.object(messaging.time, 'time', return_value=now):
            self.store.tick()

    def bind(self):
        self.append('event_msg', type='task_started', turn_id='bound')
        self.append('response_item', type='message', role='user', turn_id='bound',
                    content=[dict(type='input_text', text=self.row['envelope'])])
        self.tick(self.created + 2)
        self.assertEqual(self.record()['execution'], 'running')

    def gap(self):
        with self.path.open('a') as transcript:
            transcript.write('{"type":"event_msg"\n')

    def record(self):
        return life.get(self.store.db, self.tid)

    def assert_hold(self, since):
        record = self.record()
        self.assertEqual(record['execution'], 'uncertain')
        episode = life.hold_episode(record)
        self.assertIsNotNone(episode)
        self.assertEqual(episode['since'], since)
        self.assertEqual(episode['due_at'], since + 300)
        self.assertEqual(str(uuid.UUID(episode['episode_id'])), episode['episode_id'])
        self.assertEqual(record['evidence']['hold'], dict(episode, version=1))
        return episode

    def assert_legacy_hold(self):
        record = self.record()
        self.assertEqual(record['execution'], 'uncertain')
        self.assertIsNone(life.hold_episode(record))
        self.assertNotIn('hold', record['evidence'])

    def test_live_gap_stamps_once_and_restart_preserves_original_deadline(self):
        self.bind()
        self.gap()
        since = self.created + 60
        self.tick(since)
        episode = self.assert_hold(since)
        old = self.record()
        changes = self.store.db.total_changes
        life.hold_episode(old).pop('since')
        self.assertEqual(life.hold_episode(old), episode)
        self.assertEqual(self.store.db.total_changes, changes)
        self.tick(since + 300)
        self.restart()
        self.tick(since + 600)
        self.assertEqual(self.assert_hold(since), episode)

    def test_running_and_submitted_do_not_start_hold_on_age(self):
        self.tick(self.created + 6 * 3600)
        self.assertEqual(self.record()['execution'], 'submitted')
        self.assertIsNone(life.hold_episode(self.record()))
        self.bind()
        self.tick(self.created + 6 * 3600)
        self.assertEqual(self.record()['execution'], 'running')
        self.assertIsNone(life.hold_episode(self.record()))

    def test_startup_and_repeat_cannot_date_an_old_hold(self):
        row = dict(self.row, state='sending')
        self.store._save(row)
        self.restart()
        self.assert_legacy_hold()
        self.tick(self.created + 900)
        self.assert_legacy_hold()
        with self.store.db:
            life.reconcile(self.store.db, dict(row, state='unknown'), hold_observed=True)
        self.assert_legacy_hold()

    def test_migration_never_dates_historical_or_missing_payload(self):
        self.bind()
        row = next(row for row in self.store._rows() if row['request_id'] == self.tid)
        with self.store.db:
            self.store.db.execute('UPDATE requests SET data=? WHERE id=?',
                                  (json.dumps(dict(row, state='unknown')), self.tid))
            life.migrate(self.store.db)
        self.assert_legacy_hold()
        with self.store.db:
            self.store.db.execute('DELETE FROM requests WHERE id=?', (self.tid,))
            life.migrate(self.store.db)
        self.assert_legacy_hold()

    def test_retention_dates_observed_prune_and_preserves_episode_after_restart(self):
        self.bind()
        cutoff = self.created + messaging.RETENTION
        self.tick(cutoff)
        self.assertEqual(self.record()['execution'], 'running')
        self.tick(cutoff + 1)
        episode = self.assert_hold(cutoff + 1)
        self.assertFalse(self.store._rows())
        self.restart()
        self.tick(cutoff + 1000)
        self.assertEqual(self.assert_hold(cutoff + 1), episode)

    def test_retention_repair_cannot_date_preexisting_missing_body(self):
        self.bind()
        with self.store.db:
            self.store.db.execute('DELETE FROM requests WHERE id=?', (self.tid,))
        self.tick(self.created + messaging.RETENTION + 1)
        self.assert_legacy_hold()

    def test_leave_and_reenter_rotates_the_episode_and_exact_end_clears_it(self):
        self.bind()
        self.gap()
        self.tick(self.created + 60)
        first = self.assert_hold(self.created + 60)
        # An exact fresh binding restores a running observation after a gap.
        self.append('response_item', type='message', role='user', turn_id='bound',
                    content=[dict(type='input_text', text=self.row['envelope'])])
        self.tick(self.created + 120)
        self.assertEqual(self.record()['execution'], 'running')
        self.assertNotIn('hold', self.record()['evidence'])
        self.gap()
        self.tick(self.created + 180)
        second = self.assert_hold(self.created + 180)
        self.assertNotEqual(first['episode_id'], second['episode_id'])
        self.append('event_msg', type='task_complete', turn_id='bound')
        self.tick(self.created + 240)
        self.assertEqual(self.record()['execution'], 'completed')
        self.assertNotIn('hold', self.record()['evidence'])
        self.assertIsNone(life.hold_episode(self.record()))

    def test_retirement_removes_hold_and_late_end_cannot_recreate_it(self):
        self.bind()
        self.gap()
        self.tick(self.created + 60)
        self.assert_hold(self.created + 60)
        with self.store.db:
            life.retire(self.store.db, self.tid, life.RETIRE_REASON)
        self.append('event_msg', type='task_complete', turn_id='bound')
        self.tick(self.created + 300)
        self.assertEqual(self.record()['execution'], 'retired')
        self.assertNotIn('hold', self.record()['evidence'])
        self.assertIsNone(life.hold_episode(self.record()))

    def test_invalid_admission_payload_and_clock_do_not_get_a_hold_date(self):
        self.bind()
        with self.store.db:
            life.reconcile(self.store.db, dict(self.row, state='unknown', launched=False), hold_observed=True)
        self.assert_legacy_hold()
        self.bind()
        self.gap()
        self.tick(self.created - 1)
        self.assert_legacy_hold()
        # An owner payload without the exact native-peer admission has no lifecycle.
        owner = dict(self.row, request_id=uid(), origin='owner', state='unknown')
        self.store._save(owner, hold_observed=True)
        self.assertIsNone(life.get(self.store.db, owner['request_id']))

    def test_invalid_episode_is_unavailable_without_changing_lifecycle(self):
        self.bind()
        self.gap()
        self.tick(self.created + 60)
        record = self.record()
        for changes in ({'version': True}, {'version': 2}, {'episode_id': uid().upper()}, {'since': float('nan')},
                        {'since': True}, {'due_at': float('inf')}, {'due_at': self.created + 61},
                        {'since': self.created - 1, 'due_at': self.created + 299}, {'extra': 'fictional'}):
            with self.subTest(changes=changes):
                evidence = dict(record['evidence'], hold=dict(record['evidence']['hold'], **changes))
                self.assertIsNone(life.hold_episode(dict(record, evidence=evidence)))
        self.assertIsNone(life.hold_episode(dict(record, capture='captured')))
        self.assertIsNone(life.hold_episode(dict(record, execution='retired')))
        self.assertIsNone(life.hold_episode(dict(record, evidence=dict(record['evidence'], launched=False))))
        self.assertEqual(self.record(), record)


if __name__ == '__main__':
    unittest.main()
