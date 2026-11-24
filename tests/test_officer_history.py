#!/usr/bin/env python3
"""Fictional isolated history tests. No native logs, network service, or model use."""
import concurrent.futures
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from deck_log import DeckLog
from officer_history import day_bounds
import server
from officer_messaging import MessagingStore


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(os.path.realpath(self.tmp.name), 'private')
        self.now = day_bounds('2026-11-01')[0] + 3600
        self.store = DeckLog(self.root, clock=lambda: self.now)
        self.arm()

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def arm(self, **patch):
        state = self.store.configure(enabled=True, **patch)
        self.store.activate(state['capture_epoch'])

    def begin(self, question='What did Cedar report?', ident=None, scope=None):
        return self.store.officer_begin(ident or str(uuid.uuid4()), question, scope or {'scope': 'room'})

    def record(self, answer='Cedar reported a partial result.', **kwargs):
        token = self.begin(**kwargs)
        self.assertEqual(token['status'], 'recorded')
        self.assertTrue(self.store.officer_finish(token, dict(text=answer, model='selected-model'), 'actual-model'))
        return token

    def entries(self):
        return self.store.officer_read(day='2026-11-01')['entries']

    def test_restart_persistence_and_schema_migration(self):
        token = self.record()
        self.store.close(); self.store = DeckLog(self.root, clock=lambda: self.now)
        entries = self.entries()
        self.assertEqual(entries[0]['id'], token['id'])
        self.assertEqual(entries[0]['actual_model'], 'actual-model')
        self.assertEqual(entries[0]['selected_model'], 'selected-model')
        self.assertTrue(entries[0]['read_only'])
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]), 4)

    def test_pending_restart_unknown(self):
        self.begin(); self.store.close(); self.store = DeckLog(self.root, clock=lambda: self.now)
        self.assertEqual(self.entries()[0]['state'], 'delivery_unknown')
        self.assertIn('restarted', self.entries()[0]['qualification'])

    def test_timezone_utc_boundaries_and_dst(self):
        for day in ('2026-03-08', '2026-11-01'):
            start, end = day_bounds(day)
            self.assertEqual(end-start, 86400)
        self.now = day_bounds('2026-11-01')[0] - .001
        self.record()
        self.now += .002; self.record()
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.store.officer_read(day='2026-10-31')['entries'][0]['started_at'][:10], '2026-10-31')
        self.assertEqual(self.store.officer_read()['timezone'], 'UTC')

    def test_pagination_no_duplicates_equal_timestamps(self):
        for _ in range(5): self.record()
        ids, cursor = [], None
        while True:
            page = self.store.officer_read(day='2026-11-01', limit=2, cursor=cursor)
            ids += [e['id'] for e in page['entries']]
            cursor = page['cursor']
            if not cursor: break
        self.assertEqual(len(ids), 5); self.assertEqual(len(set(ids)), 5)
        self.assertTrue(page['page_complete'])
        self.now += 86400; self.record()
        first = self.store.officer_read(limit=1)
        second = self.store.officer_read(limit=1, cursor=first['cursor'])
        self.assertEqual(first['days'][0]['day'], '2026-11-02')
        self.assertEqual(second['days'][0]['day'], '2026-11-01')

    def test_retention_duplicate_does_not_return_expired_text(self):
        token = self.record(answer='Old fictional narrative')
        self.now += 31 * 86400
        duplicate = self.begin(ident=token['request_id'])
        self.assertEqual(duplicate['status'], 'duplicate')
        self.assertIsNone(duplicate['entry'])
        self.assertEqual(self.store.officer_read(day='2026-11-01')['status'], 'expired')

    def test_disabled_paused_and_excluded(self):
        token = self.record()
        self.store.configure(paused=True)
        self.assertEqual(self.begin()['status'], 'not_recorded')
        self.assertFalse(self.store.officer_finish(token, dict(text='Late private result')))
        self.assertEqual(len(self.entries()), 1)
        self.arm(paused=False, excluded_chats=['x:fictional'])
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.begin()['reason'], 'excluded_scope')
        self.arm(excluded_chats=[], excluded_projects=['a'*64])
        self.assertEqual(self.begin()['status'], 'not_recorded')
        self.arm(excluded_projects=[])
        self.store.configure(enabled=False)
        self.assertEqual(self.begin()['status'], 'not_recorded')

    def test_erasure_blocks_late_results_and_old_request_reexecution(self):
        token = self.record()
        pending = self.begin()
        self.store.delete(chat_id='x:fictional')
        self.arm()
        self.assertFalse(self.store.officer_finish(pending, dict(text='Late deleted answer')))
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.begin(ident=token['request_id'])['entry'])
        self.assertTrue(any(x['reason']=='source_erased_or_expired' for x in self.store.officer_read()['coverage']))
        with open(self.store.path, 'rb') as saved:
            self.assertNotIn(b'Late deleted answer', saved.read())

    def test_concurrent_claims_and_changed_scope(self):
        ident = str(uuid.uuid4())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.begin(ident=ident), range(20)))
        self.assertEqual(sum(x['status']=='recorded' for x in results), 1)
        self.assertEqual(sum(x['status']=='duplicate' for x in results), 19)
        self.assertEqual(self.begin(ident=ident, scope={'scope':'station','agent':'x:fictional'})['status'], 'conflict')
        self.assertEqual(self.begin(ident=ident, question='Different request')['status'], 'conflict')

    def test_redaction_and_receipt_positive_projection(self):
        token = self.record(question='Review /home/demo/private.txt and `code`', answer='password=fictional-secret')
        entry = self.entries()[0]
        self.assertNotIn('/home/', entry['question'])
        self.assertEqual(entry['answer'], '[content omitted]')
        self.assertTrue(entry['redacted'])
        self.assertTrue(self.store.officer_finish(token, dict(text='Safe answer', receipt={'signature':'never-save', 'payload':{'evidence':{'refs':['deck:'+'b'*64], 'raw':'never-save'}, 'generation':{'store':self.store._evidence_store}}})))
        raw = self.store.db.execute('SELECT payload FROM officer_entries').fetchone()[0]
        self.assertNotIn('never-save', raw)
        self.assertIsNone(self.entries()[0]['answer'])
        self.assertEqual(self.entries()[0]['sources'][0]['status'], 'removed')

    def test_storage_failure_and_bounds(self):
        with patch.object(self.store, '_verify', side_effect=OSError('fictional disk')):
            self.assertEqual(self.store.officer_read()['status'], 'unavailable')
        self.store.reason = ''; self.store.active = True
        for query in ({'day':'2026-02-30'}, {'day':'2026-1-1'}, {'limit':True}, {'limit':101}, {'cursor':'../fictional'}, {'day':'2026-11-01','cursor':'a'*64}):
            with self.assertRaises(ValueError): self.store.officer_read(**query)
        with self.assertRaises(ValueError): self.begin(ident='bad')

    def test_outcome_states_and_no_resurrection_or_regression(self):
        token = self.begin()
        for state, expected in [('queued','queued'),('awaiting_reply','awaiting_reply'),('unknown','delivery_unknown'),('failed','failed'),('answered','answered'),('expired','expired')]:
            self.store.officer_outcome(dict(request_id=token['request_id'],state=state))
            self.assertEqual(self.entries()[0]['actions'][0]['outcome'], expected)
        self.store.officer_finish(token, {'text':'Accepted initially','messages':[{'request_id':token['request_id'],'state':'queued'}]})
        self.assertEqual(self.entries()[0]['actions'][0]['outcome'], 'expired')
        self.store.delete(all=True); self.arm()
        self.store.officer_outcome(dict(request_id=token['request_id'],state='answered'))
        self.assertEqual(self.entries(), [])

    def test_overview_never_fabricates_zero_or_cost(self):
        out = self.store.officer_read(day='2026-10-30')
        self.assertEqual(out['status'], 'not_recorded')
        self.assertIsNone(out['overview']['recorded_questions'])
        self.record()
        out = self.store.officer_read(day='2026-11-01')
        self.assertEqual(out['overview']['recorded_questions'], 1)
        self.assertIsNone(out['overview']['daily_officer_estimated_usd'])
        self.assertIsNone(out['overview']['skills'])

    def test_backend_ask_restart_http_read_zero_actions(self):
        ident = str(uuid.uuid4())
        def scripted(body, **kwargs):
            return {'model':'actual-model', 'content':[{'type':'tool_use','name':'clarify_relay','input':{'text':'A fictional recorded answer'}}]}, None
        from unittest.mock import Mock
        live = Mock(side_effect=scripted)
        with patch.object(server, 'HISTORY', self.store), patch.object(server, 'MESSAGING', None), patch.object(server, 'OFFICER_USAGE', None):
            result = server.officer_ask([{'role':'user','content':'What did Cedar report?'}], call=live, request_id=ident)
            self.assertEqual(result['history_status'], 'recorded')
            again = server.officer_ask([{'role':'user','content':'What did Cedar report?'}], call=live, request_id=ident)
            self.assertTrue(again['duplicate']); self.assertEqual(live.call_count, 1)
        self.store.close(); self.store = DeckLog(self.root, clock=lambda:self.now)
        httpd = server.Server(('127.0.0.1',0), server.H)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever,daemon=True); thread.start()
        try:
            with patch.object(server, 'HISTORY', self.store), patch.object(server, 'PORT',port), patch.object(server,'KEY','fictional-owner-key'), patch.object(server,'anthropic_call',side_effect=AssertionError('model')), patch.object(server,'owner_target_call',side_effect=AssertionError('control')), patch.object(server,'officer_ask',side_effect=AssertionError('ask')):
                def get(path, key='fictional-owner-key'):
                    conn = http.client.HTTPConnection('127.0.0.1',port)
                    conn.request('GET', path, headers={'X-Live-Room-Key':key})
                    response=conn.getresponse(); body=response.read(); conn.close(); return response.status, body
                code, body=get('/history/officer/day?day=2026-11-01')
                self.assertEqual(code,200); self.assertEqual(json.loads(body)['entries'][0]['answer'],'A fictional recorded answer')
                self.assertEqual(get('/history/officer/days')[0],200)
                self.assertEqual(get('/history/officer/days',key='wrong')[0],403)
                for query in ('day=2026-11-01&day=2026-11-02','day=bad','day=2026-11-01&limit=0','day=2026-11-01&extra=1','day=%ZZ'):
                    self.assertEqual(get('/history/officer/day?'+query)[0],400)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join()



    def test_failed_exclusion_cleanup_cannot_resurrect_prose(self):
        token=self.record(answer='Sensitive fictional narrative')
        with patch.object(self.store,'_officer_erase',side_effect=sqlite3.OperationalError('fictional failure')):
            self.store.configure(excluded_chats=['x:fictional'])
        self.assertEqual(self.entries(),[])
        self.assertIsNone(self.begin(ident=token['request_id'])['entry'])
        self.store.close(); self.store=DeckLog(self.root,clock=lambda:self.now)
        self.assertEqual(self.entries(),[])
        self.arm(excluded_chats=[])
        self.assertEqual(self.entries(),[])
        self.assertIsNone(self.begin(ident=token['request_id'])['entry'])

    def test_late_outcome_after_restart(self):
        token=self.begin()
        self.store.officer_outcome({'request_id':token['request_id'],'state':'queued'})
        self.store.close(); self.store=DeckLog(self.root,clock=lambda:self.now); self.arm()
        earlier = self.entries()[0]
        self.assertEqual(earlier['actions'][0]['observation_status'], 'before_restart')
        self.now += 120
        self.store.officer_outcome({'request_id':token['request_id'],'state':'answered'})
        later = self.entries()[0]
        self.assertEqual(later['actions'][0]['outcome'], 'answered')
        self.assertNotIn('observation_status', later['actions'][0])
        self.assertGreater(later['actions'][0]['observed_at'], earlier['actions'][0]['observed_at'])
        self.assertEqual(later['updated_at'], later['actions'][0]['observed_at'])
        self.assertFalse(self.store.officer_finish(token,{'text':'Late answer cannot cross restart'}))

    @unittest.skipIf(os.name == 'nt', 'Codex reply tracking checks transcripts with POSIX owner and mode rules; Codex messaging needs the Mac app')
    def test_idle_messaging_ticks_preserve_the_recorded_outcome(self):
        sessions = os.path.join(self.tmp.name, 'sessions')
        os.mkdir(sessions)
        messaging = MessagingStore(os.path.join(self.tmp.name, 'messages'), sessions)
        try:
            messaging.history_observer = lambda row, kind: self.store.officer_outcome(row, kind)
            token = self.begin()
            row = dict(request_id=token['request_id'], state='answered', agent='x:' + str(uuid.uuid4()),
                       created=time.time(), reply='Fictional answer', launched=True)
            with messaging.lock:
                messaging._save(row)
            self.assertTrue(self.store.officer_finish(token, {'text': 'Fictional recorded response'}))
            original = self.entries()[0]
            self.assertEqual(original['actions'][0]['outcome'], 'answered')

            self.now += 120
            messaging.tick()
            self.assertEqual(self.entries()[0], original)

            self.store.close()
            self.store = DeckLog(self.root, clock=lambda: self.now)
            self.arm()
            restarted = self.entries()[0]
            self.assertEqual(restarted['actions'][0]['observation_status'], 'before_restart')
            self.assertEqual(restarted['actions'][0]['observed_at'], original['actions'][0]['observed_at'])
            self.assertEqual(restarted['updated_at'], original['updated_at'])

            self.now += 120
            changes = self.store.db.total_changes
            messaging.tick()
            self.assertEqual(self.entries()[0], restarted)
            self.assertEqual(self.store.db.total_changes, changes)
        finally:
            messaging.close()

    def test_schema_three_existing_events_survive_migration(self):
        self.store.offer_record({'key':'a'*64,'stream':'b'*64,'provider':'codex','chat_id':'x:fictional','project_id':'','source_ts':self.now,'time_basis':'source'}, [{'kind':'message','role':'assistant','text':'Fictional pre-migration evidence','source':{'slot':'0'}}], capture_epoch=self.store.epoch)
        self.assertTrue(self.store.flush())
        self.record()
        self.store._save_setting('schema_version', 3)
        for table in ('officer_entries', 'officer_seen', 'officer_gaps'):
            self.store.db.execute('DROP TABLE '+table)
        self.store.db.commit(); self.store.close()
        self.store = DeckLog(self.root, clock=lambda:self.now)
        self.assertTrue(self.store.available)
        self.assertEqual(self.store.officer_read()['days'], [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM source_records').fetchone()[0],1)
        self.assertEqual(self.store.search()['events'][0]['text'],'Fictional pre-migration evidence')

    def test_missing_versus_unavailable_evidence(self):
        token=self.record()
        with patch.object(self.store, 'evidence_current', return_value={'available':False,'records':{}}):
            self.store.officer_finish(token, {'text':'Earlier safe text', 'receipt':{'payload':{'generation':{'store':self.store._evidence_store},'evidence':{'refs':['deck:'+'b'*64]}}}})
            entry=self.entries()[0]
            self.assertEqual(entry['sources'][0]['status'],'unavailable')
            self.assertEqual(entry['answer'],'Earlier safe text')
        self.assertIsNone(self.entries()[0]['answer'])

    def test_erasure_while_model_blocked_and_storage_failure(self):
        entered, release=threading.Event(), threading.Event()
        def live(*args,**kwargs):
            entered.set(); self.assertTrue(release.wait(3)); return {'text':'Late fictional result'}
        with patch.object(server,'HISTORY',self.store), patch.object(server,'_officer_ask_live',side_effect=live):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                future=pool.submit(server.officer_ask,[{'role':'user','content':'Fictional question'}])
                self.assertTrue(entered.wait(2)); self.store.delete(all=True); self.arm(); release.set()
                self.assertEqual(future.result()['history_status'], 'not_recorded')
        self.assertEqual(self.entries(),[])
        with patch.object(server,'HISTORY',self.store), patch.object(server,'_officer_ask_live',return_value={'text':'Result'}), patch.object(self.store,'officer_finish',side_effect=sqlite3.OperationalError('fictional disk full')):
            out=server.officer_ask([{'role':'user','content':'Other fictional question'}])
            self.assertEqual(out['history_status'],'unavailable')

    @unittest.skipIf(os.name=='nt','Codex messages and controls need the Mac app')
    @patch.object(sys, 'platform', 'darwin')   # Codex live controls exist only with the Mac app; its bridge is mocked
    def test_real_messaging_save_and_control_observers(self):
        sessions=os.path.join(self.tmp.name,'sessions'); os.mkdir(sessions)
        sid, turn=str(uuid.uuid4()),str(uuid.uuid4()); aid='x:'+sid
        path=os.path.join(sessions,'rollout-'+sid+'.jsonl')
        with open(path,'w') as handle:
            for kind,payload in [('session_meta',{'id':sid,'cwd':self.tmp.name}),('event_msg',{'type':'task_started','turn_id':turn})]:
                handle.write(json.dumps({'type':kind,'payload':payload})+'\n')
        messaging=MessagingStore(os.path.join(self.tmp.name,'messages'),sessions)
        messaging.history_observer=self.store.officer_outcome
        try:
            token=self.begin()
            messaging._save({'request_id':token['request_id'],'state':'queued','agent':aid})
            self.assertEqual(self.entries()[0]['actions'][0]['outcome'],'queued')
            messaging._save({'request_id':token['request_id'],'state':'answered','agent':aid})
            self.assertEqual(self.entries()[0]['actions'][0]['outcome'],'answered')
            control=self.begin()
            result=messaging.submit_control('interrupt',aid,'',control['request_id'],{aid:{'runtime':'Codex','name':'Cedar'}},execute=lambda *a,**k:{'state':'interrupted'})
            self.assertEqual(result['state'],'interrupted')
            entry=next(e for e in self.entries() if e['request_id']==control['request_id'])
            action=entry['actions'][0]
            self.assertEqual((action['agent'],action['action'],action['outcome']),(aid,'interrupt','interrupted'))
            self.assertTrue(action['observed_at'].endswith('Z'))
        finally: messaging.close()


class FreshMemoryStatusTests(unittest.TestCase):
    def test_healthy_storage_without_an_archive_reads_not_recorded_and_unverifiable_storage_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = DeckLog(os.path.join(os.path.realpath(tmp), 'private'), clock=lambda: day_bounds('2026-11-01')[0])
            try:
                self.assertTrue(fresh.available)
                self.assertIsNone(fresh.db, 'Memory has not created its archive')
                self.assertEqual(fresh.officer_read()['status'], 'not_recorded')
                self.assertEqual(fresh.officer_read(day='2026-11-01')['status'], 'not_recorded')
            finally:
                fresh.close()
            blocked = os.path.join(os.path.realpath(tmp), 'not-a-folder')
            with open(blocked, 'w') as handle:
                handle.write('fictional')
            broken = DeckLog(blocked, clock=lambda: day_bounds('2026-11-01')[0])
            try:
                self.assertFalse(broken.available)
                self.assertEqual(broken.officer_read()['status'], 'unavailable')
            finally:
                broken.close()

    def test_a_closed_store_with_a_recorded_archive_still_reads_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeckLog(os.path.join(os.path.realpath(tmp), 'private'), clock=lambda: day_bounds('2026-11-01')[0] + 3600)
            state = store.configure(enabled=True)
            store.activate(state['capture_epoch'])
            token = store.officer_begin(str(uuid.uuid4()), 'What did Cedar report?', {'scope': 'room'})
            self.assertTrue(store.officer_finish(token, dict(text='Cedar reported a partial result.', model='selected-model'), 'actual-model'))
            store.close()
            self.assertTrue(store.available)
            self.assertIsNone(store.db)
            self.assertEqual(store.officer_read()['status'], 'unavailable')
            self.assertEqual(store.officer_read(day='2026-11-01')['status'], 'unavailable')

    def test_a_close_that_timed_out_with_the_archive_open_still_reads_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeckLog(os.path.join(os.path.realpath(tmp), 'private'), clock=lambda: day_bounds('2026-11-01')[0] + 3600)
            state = store.configure(enabled=True)
            store.activate(state['capture_epoch'])
            token = store.officer_begin(str(uuid.uuid4()), 'What did Cedar report?', {'scope': 'room'})
            self.assertTrue(store.officer_finish(token, dict(text='Cedar reported a partial result.', model='selected-model'), 'actual-model'))
            self.assertEqual(store.officer_read(day='2026-11-01')['status'], 'partial')
            release = threading.Event()
            writer = threading.Thread(target=release.wait, daemon=True)   # stands in for a writer still busy past the deadline
            writer.start()
            real, store.thread = store.thread, writer
            try:
                store.close(timeout=0.05)
                self.assertTrue(store.closed)
                self.assertIsNotNone(store.db, 'the timed-out close left the archive open')
                for read in (store.officer_read(), store.officer_read(day='2026-11-01')):
                    self.assertEqual(read['status'], 'unavailable')
                    self.assertFalse(read['entries'])
            finally:
                release.set()
                writer.join()
                real.join(2)
                store.close()
            self.assertIsNone(store.db)

    def test_fresh_storage_under_a_linked_folder_reads_unavailable_as_enabling_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_home = os.path.join(os.path.realpath(tmp), 'real-home')
            os.mkdir(real_home)
            linked_home = os.path.join(os.path.realpath(tmp), 'linked-home')
            try:
                os.symlink(real_home, linked_home, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('This system cannot create a folder link.')
            os.mkdir(os.path.join(real_home, '.live-room'), 0o700)
            store = DeckLog(os.path.join(linked_home, '.live-room'), clock=lambda: day_bounds('2026-11-01')[0])
            try:
                self.assertIsNone(store.db, 'Memory has not created its archive')
                self.assertEqual(store.officer_read()['status'], 'unavailable')
                self.assertEqual(store.officer_read(day='2026-11-01')['status'], 'unavailable')
                store.configure(enabled=True)
                self.assertFalse(store.available)
                self.assertEqual(store.reason, 'private_storage_unavailable')
                self.assertFalse(os.path.lexists(os.path.join(real_home, '.live-room', 'deck-log.sqlite3')))
            finally:
                store.close()


if __name__ == '__main__': unittest.main()
