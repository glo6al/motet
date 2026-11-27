#!/usr/bin/env python3
"""The progress-based archive wait used by test fixtures, against a real DeckLog.

Writers are slowed, stalled or broken with patches; short windows keep it fast.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(1, str(pathlib.Path(__file__).resolve().parent))
import deck_log as d
import deck_log_settle as settle


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1800000000.0
        self.store = d.DeckLog(os.path.join(os.path.realpath(self.temp.name), 'state'), clock=lambda: self.now)
        self.assertTrue(self.store.available, self.store.reason)
        self.epoch = self.store.configure(enabled=True)['capture_epoch']
        self.store.activate(self.epoch)
        self.number = 0
        self.release = threading.Event()
        self.addCleanup(lambda: settle.close_and_clean(self.store, self.temp.cleanup, timeout=0.5))
        # Runs first: never leave a patched writer blocked during cleanup.
        self.addCleanup(self.release.set)

    def queue(self, count):
        # Hold the writer's gate so every record is queued before any is taken.
        with self.store.wake:
            for _ in range(count):
                self.number += 1
                record = dict(key=self.store.opaque_identity('record', self.number),
                              stream=self.store.opaque_identity('stream', 'fictional-settle'), provider='codex',
                              chat_id='x:chat-one', project_id=self.store.opaque_identity('project', 'Orchard'),
                              source_ts=self.now, time_basis='source')
                row = dict(kind='message', role='assistant', text='Fictional update %d' % self.number,
                           chat_name='Harbor', project_name='Orchard', source={'slot': '0'})
                self.assertEqual(self.store.offer_record(record, [row], capture_epoch=self.epoch)['status'], 'queued')

    def blocked_writer(self, entered=None):
        def write(*args):
            if entered is not None:
                entered.set()
            self.release.wait(30)
        return write

    def slow_writer(self, seconds):
        real = self.store._write
        def write(*args):
            time.sleep(seconds)
            return real(*args)
        return write

    def saved(self):
        with self.store.lock:
            return self.store.db.execute('SELECT count(*) FROM events').fetchone()[0]

    def test_writer_stuck_inside_a_record_fails_after_the_stall_window(self):
        with patch.object(self.store, '_write', self.blocked_writer()):
            self.queue(3)
            started = time.monotonic()
            problem = settle.settle(self.store, 'Stuck archive', stall=0.5, ceiling=10)
            elapsed = time.monotonic() - started
        self.assertIn('Stuck archive made no progress for 0.5s', problem)
        self.assertIn('inflight=1', problem)
        self.assertIn("step='write'", problem)
        self.assertIn('writer_alive=True', problem)
        self.assertLess(elapsed, 5, 'a stall must fail within its window, not at the ceiling')

    def test_write_error_is_reported_at_once_with_its_last_error(self):
        def broken(*args):
            raise sqlite3.OperationalError('fictional disk error')
        with patch.object(self.store, '_write', broken):
            self.queue(3)
            started = time.monotonic()
            problem = settle.settle(self.store, 'Broken archive', stall=10, ceiling=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('Broken archive reported a failure', problem)
        self.assertIn("reason='storage_full_or_unavailable'", problem)
        self.assertIn('OperationalError', problem)
        self.assertIn('fictional disk error', problem)

    def test_slow_but_steady_writer_passes_beyond_the_stall_window(self):
        with patch.object(self.store, '_write', self.slow_writer(0.15)):
            self.queue(6)
            started = time.monotonic()
            problem = settle.settle(self.store, stall=0.5, ceiling=10)
            elapsed = time.monotonic() - started
        self.assertEqual(problem, '')
        self.assertGreater(elapsed, 0.5, 'the whole batch outlasted the stall window')
        self.assertEqual(self.saved(), 6)

    def test_steady_writer_past_the_ceiling_still_fails(self):
        with patch.object(self.store, '_write', self.slow_writer(0.2)):
            self.queue(10)
            started = time.monotonic()
            problem = settle.settle(self.store, 'Long archive', stall=5, ceiling=1)
            elapsed = time.monotonic() - started
        self.assertIn('Long archive was still saving at the 1s ceiling', problem)
        self.assertLess(elapsed, 3)

    def test_writer_thread_gone_fails_at_once(self):
        def fatal(*args):
            raise ValueError('fictional writer crash')
        with patch.object(self.store, '_write', fatal), patch.object(threading, 'excepthook', lambda args: None):
            self.queue(3)
            self.store.thread.join(5)
            self.assertFalse(self.store.thread.is_alive())
            problem = settle.settle(self.store, 'Orphan archive', stall=10, ceiling=10)
        self.assertIn('Orphan archive has no running writer', problem)
        self.assertIn('queued=2', problem)
        self.assertIn('writer_alive=False', problem)
        with self.store.wake:
            self.store.queue.clear()

    def test_closed_with_records_pending_fails_at_once(self):
        entered = threading.Event()
        with patch.object(self.store, '_write', self.blocked_writer(entered)):
            self.queue(3)
            self.assertTrue(entered.wait(5))
            with self.store.wake:
                self.store.closed = True
            problem = settle.settle(self.store, 'Closed archive', stall=10, ceiling=10)
            self.release.set()
            self.store.thread.join(5)
        self.assertIn('Closed archive was closed with records still pending', problem)

    def test_close_reports_a_writer_that_keeps_the_file_open(self):
        entered = threading.Event()
        with patch.object(self.store, '_write', self.blocked_writer(entered)):
            self.queue(1)
            self.assertTrue(entered.wait(5))
            with self.assertRaises(AssertionError) as caught:
                settle.close(self.store, 'Leaking archive', stall=0.3, timeout=0.2)
            self.release.set()
            self.store.thread.join(5)
        message = str(caught.exception)
        self.assertIn('Leaking archive could not be closed (database open=True)', message)
        self.assertIn('writer_alive=True', message)
        settle.close(self.store, timeout=0.5)
        self.assertIsNone(self.store.db)

    def test_close_releases_the_file_after_a_normal_run(self):
        self.queue(2)
        self.assertEqual(settle.settle(self.store, stall=5, ceiling=10), '')
        settle.close(self.store, timeout=0.5)
        self.assertIsNone(self.store.db)
        self.assertFalse(self.store.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
