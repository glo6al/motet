"""Exact intent/receipt recovery using actual local ledgers, no transport."""
from contextlib import contextmanager
from dataclasses import asdict,replace
import os
import json
import subprocess
import sqlite3
import threading
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from attachments_types import AttachmentError,canonical,digest
from attachments_store import SCHEMA_VERSION
from officer_document_intents import OfficerIntents,migrate
import test_officer_document_ledger as fixture
from test_attachments_foundation import fence
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY


@unittest.skipUnless(STORE, STORE_WHY)
class IntentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ExecutionTests();self.f.setUp();self.store=self.f.store
        if self.store.db.execute('PRAGMA user_version').fetchone()[0]==9:
            with self.store._gate(),self.store._transaction():migrate(self.store)
        self.i=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        self.winners={}
    def tearDown(self):self.f.tearDown()
    def prepared(self):
        d=self.f.f.draft();self.f.f.upload(d)
        edited=self.store.edit_text(d['id'],d['capability'],fence(d),'Read the fictional file.');d.update(edited)
        k=self.f.f.key();r=self.i.prepare_draft(d['id'],d['capability'],fence(d),k,self.f.f.binding,self.f.f.direct_profile())
        return d,k,r
    def consume(self,d,r):
        record,winner=self.i.consume(r['requestId'],r['requestCapability'],draft_capability=d['capability'],fence=fence(d),fingerprint=r['fingerprint'])
        if winner:self.winners[record['id']]=winner
        return record,winner
    def admit(self,d,record):
        return self.f.f.officer.admit(d['id'],d['capability'],fence(d),self.i.command(record,'local'),self.f.f.binding,self.f.f.direct_profile())
    def accepted(self,commit=True,verified_usage=True):
        d,_,r=self.prepared();record,_=self.consume(d,r);message=self.admit(d,record)
        ex=self.f.l.prepare(self.f.selection,self.f.p,self.f.scope.id,self.i.command(record,'execution'),
                            record['question'],local_message_id=message['id'])
        write=self.f.l.record_result
        with mock.patch.object(self.f.l,'record_result',side_effect=lambda selection,facts:
                               write(selection,facts if verified_usage else replace(facts,usage=None))):
            candidate,_=self.f.accept(ex)
        if commit:self.f.l.commit_answer(self.f.selection,ex['id'],candidate)
        self.i.finish(record['id'],self.winners[record['id']])
        return record,ex

    def test_committed_revocation_keeps_exact_historical_answer_without_replay_authority(self):
        record,ex=self.accepted();before=self.i.view(record)
        self.assertEqual((before['state'],before['answerId']),('answered',ex['id']))
        self.f.f.revoke(record['sources'][0]['id'])
        after=self.i.view(record);self.assertEqual(after,before)
        with self.assertRaises(AttachmentError) as caught:self.f.l.replay(self.f.selection,ex['id'])
        self.assertEqual(caught.exception.code,'document_answer_unavailable')
        self.f.reopen();self.store=self.f.store;self.i=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        self.assertEqual(self.i.view(record),before)

    def test_unpublished_and_usage_unverified_acceptances_stay_failed_after_revocation(self):
        for verified in (True,False):
            case=IntentTests();case.setUp()
            try:
                record,ex=case.accepted(commit=False,verified_usage=verified)
                before=case.i.view(record);ledger_before=case.f.l.status(case.f.selection,ex['id'])
                self.assertEqual((before['state'],before['answerId']),('failed',None))
                self.assertFalse(ledger_before['answerCommitted'])
                case.f.f.revoke(record['sources'][0]['id']);self.assertEqual(case.i.view(record),before)
                ledger_after=case.f.l.status(case.f.selection,ex['id'])
                self.assertFalse(ledger_after['answerCommitted'])
                for field in ('settled','chargedUnits','reservedUnits','error'):self.assertEqual(ledger_after[field],ledger_before[field])
                if not verified:
                    self.assertFalse(ledger_after['settled']);self.assertEqual(ledger_after['error'],'usage_unverified')
                    self.assertEqual(ledger_after['chargedUnits'],ledger_after['reservedUnits'])
                    self.assertTrue(case.f.l._scope(case.f.scope.id)[1])
                case.f.reopen();case.store=case.f.store;case.i=OfficerIntents(case.store,case.f.l,case.f.p,case.f.scope.id)
                self.assertEqual(case.i.view(record),before)
            finally:case.tearDown()

    def test_missing_corrupt_execution_or_commitment_never_becomes_historical_answer(self):
        record,ex=self.accepted();row=self.store.db.execute('SELECT receipt FROM officer_document_requests WHERE id=?',(ex['id'],)).fetchone()
        receipt=fixture.ledger.unpack(row['receipt'],4096)
        for field in ('candidate','answer_digest'):
            corrupt=dict(receipt,**{field:''})
            self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(fixture.ledger.pack(corrupt,4096),ex['id']))
            with self.assertRaises(AttachmentError) as caught:self.i.view(record)
            self.assertEqual(caught.exception.code,'document_record_invalid')
        self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(row['receipt'],ex['id']))
        self.store.db.execute('DELETE FROM seen WHERE key=?',(self.i.command(record,'execution'),))
        with self.assertRaises(AttachmentError):self.i.view(record)
    def test_prepare_is_private_and_duplicate_send_has_one_winner(self):
        d,k,r=self.prepared()
        self.assertEqual(r['state'],'prepared');self.assertEqual(r['officerDelivery']['state'],'not_sent')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)
        record,won=self.consume(d,r);self.assertTrue(won)
        _,won=self.consume(d,r);self.assertFalse(won)
        self.assertEqual(self.i.view(record)['state'],'preparing')
        self.admit(d,record);self.i.finish(record['id'],self.winners[record['id']])
        recovered=self.i.prepare_draft(d['id'],d['capability'],fence(d),k,self.f.f.binding,self.f.f.direct_profile())
        self.assertEqual(recovered['requestId'],r['requestId']);self.assertEqual(recovered['state'],'retained_local')
    def test_read_only_recovery_after_admission_prepare_claim_never_reinvokes(self):
        d,_,r=self.prepared();record,_=self.consume(d,r);m=self.admit(d,record)
        ex=self.f.l.prepare(self.f.selection,self.f.p,self.f.scope.id,self.i.command(record,'execution'),record['question'],local_message_id=m['id'])
        self.f.l.claim(self.f.selection,ex['id'])
        # A fresh coordinator object cannot run any recovery continuation.
        other=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        with mock.patch.object(self.f.l,'prepare',side_effect=AssertionError('must not prepare')),\
             mock.patch.object(self.f.l,'claim',side_effect=AssertionError('must not claim')),\
             mock.patch.object(other.conversations,'admit',side_effect=AssertionError('must not admit')):
            out=other.recover(record['id'],record['binding'],record['fingerprint'])
        self.assertEqual(out['state'],'unknown');self.assertEqual(out['requestId'],record['id'])
        with self.assertRaises(AttachmentError):other.authorize(record['id'],r['requestCapability'])
        other.authorize(record['id'],out['requestCapability'])
    def test_retained_without_claim_is_not_turned_into_unknown_or_answer(self):
        d,_,r=self.prepared();record,_=self.consume(d,r);self.admit(d,record)
        other=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        out=other.recover(record['id'],record['binding'],record['fingerprint'])
        self.assertEqual(out['state'],'preparing');self.assertIsNone(out['answerId'])
        # A different object in the same process cannot declare a live winner dead.
        self.i.finish(record['id'],self.winners[record['id']])
        self.assertEqual(other.recover(record['id'],record['binding'],record['fingerprint'])['state'],'retained_local')
    def test_stale_changed_fence_and_foreign_capability_refuse(self):
        d,_,r=self.prepared()
        self.store.edit_text(d['id'],d['capability'],fence(d),'Different owner question')
        with self.assertRaises(AttachmentError):self.consume(d,r)
        with self.assertRaises(AttachmentError):self.i.authorize(r['requestId'],'invalid')
        with self.assertRaises(AttachmentError):self.i.recover(r['requestId'],dict(d['destination'],scopeId='other'),r['fingerprint'])
    def test_missing_execution_receipt_is_not_evidence_of_no_call(self):
        d,_,r=self.prepared();record,_=self.consume(d,r);m=self.admit(d,record)
        self.f.l.prepare(self.f.selection,self.f.p,self.f.scope.id,self.i.command(record,'execution'),record['question'],local_message_id=m['id'])
        with self.store._gate(),self.store._transaction():
            self.store.db.execute('DELETE FROM seen WHERE key=?',(self.i.command(record,'execution'),))
        with self.assertRaises(AttachmentError):self.i.view(record)
    def test_cross_intent_local_receipt_substitution_refuses(self):
        d,_,r=self.prepared();a,_=self.consume(d,r);m=self.admit(d,a)
        d2,_,r2=self.prepared();b,_=self.consume(d2,r2)
        with self.store._gate(),self.store._transaction():
            self.store.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)',
                (self.i.command(b,'local'),'officer_attachment_admission',b['local_intent'],m['id'],self.store.owner_epoch))
        with self.assertRaises(AttachmentError):self.i.view(b)

    def test_concurrent_consumes_have_exactly_one_winner(self):
        d,_,r=self.prepared();outputs=[]
        threads=[threading.Thread(target=lambda:outputs.append(bool(self.consume(d,r)[1]))) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sorted(outputs),[False,False,False,True])

    def test_actual_worker_process_exit_recovers_retained_without_dispatch(self):
        d,_,r=self.prepared();self.store.close()
        script="""
import json,sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'src'),str(Path.cwd()/'tests')]
from attachments_store import AttachmentStore
from officer_document_ledger import OfficerDocumentLedger
from officer_document_intents import OfficerIntents
from test_officer_documents import profile
from test_attachments_foundation import EPOCH
from attachments_officer_storage import OfficerDirectRetentionProfile
v=json.load(sys.stdin)
s=AttachmentStore(v['root'],enabled=True,owner_epoch=EPOCH,clock=lambda:v['time'])
i=OfficerIntents(s,OfficerDocumentLedger(s),profile(),v['scope'])
r=i.recover(v['id'],v['binding'],v['fingerprint'])
record,won=i.consume(v['id'],r['requestCapability'],draft_capability=v['cap'],fence=v['fence'],fingerprint=v['fingerprint'])
assert won
p=v['profile'];p['media_types']=tuple(p['media_types'])
i.conversations.admit(record['draft_id'],v['cap'],v['fence'],i.command(record,'local'),record['binding'],OfficerDirectRetentionProfile(**p))
s.close()
"""
        data=dict(root=str(self.f.f.root),time=self.f.f.clock[0],scope=self.f.scope.id,id=r['requestId'],
            binding=self.f.f.binding,fingerprint=r['fingerprint'],cap=d['capability'],fence=fence(d),profile=asdict(self.f.f.direct_profile()))
        subprocess.run([sys.executable,'-c',script],input=json.dumps(data),text=True,check=True,capture_output=True,
            cwd=Path(__file__).resolve().parents[1],env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.f.reopen();self.store=self.f.store;self.i=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        out=self.i.recover(r['requestId'],self.f.f.binding,r['fingerprint'])
        self.assertEqual(out['state'],'retained_local');self.assertIsNone(out['answerId'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],0)

    def test_bounded_read_only_history_and_foreign_cursor(self):
        d,_,r=self.prepared();record,_=self.consume(d,r);self.admit(d,record);self.i.finish(record['id'],self.winners[record['id']])
        for n in range(50):
            self.i.prepare_question(record['binding'],'Fictional question '+str(n),[],self.f.f.direct_profile(),self.f.f.key())
        with mock.patch.object(self.f.l,'prepare',side_effect=AssertionError('history cannot prepare')):
            first=self.i.history(record['binding']);second=self.i.history(record['binding'],first['nextCursor'])
        self.assertEqual(len(first['items']),50);self.assertEqual(len(second['items']),1);self.assertIsNone(second['nextCursor'])
        self.assertTrue(all('text' not in item and 'question' in item for item in first['items']))
        foreign=dict(record['binding'],conversationId=self.store.create_conversation('fictional-room',self.f.f.key())['id'])
        with self.assertRaises(AttachmentError):self.i.history(foreign,first['nextCursor'])

    def test_recent_intent_record_rejects_malformed_window_even_with_recomputed_fingerprint(self):
        draft,_,prepared=self.prepared();record,_=self.consume(draft,prepared);self.admit(draft,record)
        self.i.finish(record['id'],self.winners[record['id']]);binding=record['binding']
        issued=self.i.prepare_question(binding,'Use available answers.',[],self.f.f.direct_profile(),self.f.f.key(),
                                       prior_answer_policy='recent_available_8')
        current,_=self.i.authorize(issued['requestId'],issued['requestCapability'])
        proof=current['prior_answer_window'];self.assertEqual(proof['includedCount'],0)
        malformed=[None,dict(proof,payload='additional field'),dict(proof,policy='unknown'),
                   dict(proof,includedCount=True),dict(proof,includedCount=1),dict(proof,omittedOlder=True)]
        self.store.db.execute('DROP TRIGGER officer_document_intent_immutable')
        for window in malformed:
            changed=dict(current,prior_answer_window=window)
            changed['fingerprint']=digest({k:v for k,v in changed.items() if k!='fingerprint'})
            self.store.db.execute('UPDATE officer_document_intents SET record=? WHERE id=?',(canonical(changed),current['id']))
            with self.assertRaises(AttachmentError) as caught:self.i._record(current['id'])
            self.assertEqual(caught.exception.code,'document_request_conflict')

    def test_per_draft_intent_capacity_has_no_eviction(self):
        d,_,r=self.prepared()
        for _ in range(15):self.i.prepare_draft(d['id'],d['capability'],fence(d),self.f.f.key(),self.f.f.binding,self.f.f.direct_profile())
        with self.assertRaises(AttachmentError) as caught:
            self.i.prepare_draft(d['id'],d['capability'],fence(d),self.f.f.key(),self.f.f.binding,self.f.f.direct_profile())
        self.assertEqual(caught.exception.code,'request_capacity')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_intents').fetchone()[0],16)
        self.i.authorize(r['requestId'],r['requestCapability'])

    def test_migration_rollback_and_reopen_preserve_schema9(self):
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('DROP TABLE officer_document_intents');self.store.db.execute('PRAGMA user_version=9')
        def crash(name):
            if name=='officer_document_intents_before_version':raise RuntimeError('fictional migration interruption')
        self.store.checkpoint=crash
        with self.assertRaises(RuntimeError),self.store._gate(),self.store._transaction(reclaim=True):migrate(self.store)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],9)
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='officer_document_intents'").fetchone())
        self.store.checkpoint=lambda _:None;self.f.reopen();self.store=self.f.store
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],10)

    def test_consumed_receipt_survives_actual_sqlite_full(self):
        self.store.close();self.f.store=self.f.f.open(overrides={'database_bytes':2*1024*1024});self.f.f.store=self.f.store
        self.store=self.f.store;self.f.l=fixture.ledger.OfficerDocumentLedger(self.store)
        self.i=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        d,_,r=self.prepared();record,_=self.consume(d,r)
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('CREATE TABLE fictional_intent_filler(payload BLOB)')
        full=False
        for _ in range(10000):
            try:
                with self.store.lock,self.store._transaction(reclaim=True):
                    self.store.db.execute('INSERT INTO fictional_intent_filler VALUES(zeroblob(3000))')
            except sqlite3.OperationalError as exc:
                self.assertEqual(str(exc),'database or disk is full');full=True;break
        self.assertTrue(full)
        self.assertEqual(self.store.db.execute('PRAGMA page_count').fetchone()[0],self.store.db.execute('PRAGMA max_page_count').fetchone()[0])
        self.i.finish(record['id'],self.winners[record['id']],'local_refused')
        self.assertEqual(self.i.view(record)['state'],'failed')
        self.assertFalse(self.consume(d,r)[1])

    def test_consume_commit_failure_does_not_release_authority_twice(self):
        d,_,r=self.prepared()
        def crash(name):
            if name=='officer_intent_consume_before_commit':raise RuntimeError('fictional rollback')
        self.store.checkpoint=crash
        with self.assertRaises(RuntimeError):self.consume(d,r)
        self.store.checkpoint=lambda _:None
        self.assertTrue(self.consume(d,r)[1]);self.assertFalse(self.consume(d,r)[1])

    def test_retry_losing_final_gate_returns_consumed_winner_receipt(self):
        d=self.f.f.draft();self.f.f.upload(d);k=self.f.f.key();original=self.i.conversations._reading;result={};once=[False]
        @contextmanager
        def interleave(rows):
            if not once[0]:
                once[0]=True
                r=self.i.prepare_draft(d['id'],d['capability'],fence(d),k,self.f.f.binding,self.f.f.direct_profile())
                record,winner=self.consume(d,r);self.admit(d,record);self.i.finish(record['id'],winner);result.update(r)
            with original(rows):yield
        with mock.patch.object(self.i.conversations,'_reading',side_effect=interleave):
            loser=self.i.prepare_draft(d['id'],d['capability'],fence(d),k,self.f.f.binding,self.f.f.direct_profile())
        self.assertEqual(loser['requestId'],result['requestId']);self.assertEqual(loser['state'],'retained_local')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_intents').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)

    def test_only_exact_continuation_winner_can_finish(self):
        d,_,r=self.prepared();record,winner=self.consume(d,r)
        other=OfficerIntents(self.store,self.f.l,self.f.p,self.f.scope.id)
        self.assertFalse(other.finish(record['id'],winner))
        self.assertFalse(self.i.finish(record['id'],object()))
        self.assertEqual(self.i.view(record)['state'],'preparing')
        self.assertTrue(self.i.finish(record['id'],winner));self.assertFalse(self.i.finish(record['id'],winner))

    def test_superseded_unconsumed_intent_does_not_adopt_new_admission(self):
        d,_,a=self.prepared();d.update(self.store.edit_text(d['id'],d['capability'],fence(d),'Revised fictional question'))
        b=self.i.prepare_draft(d['id'],d['capability'],fence(d),self.f.f.key(),self.f.f.binding,self.f.f.direct_profile())
        record,winner=self.consume(d,b);self.admit(d,record);self.i.finish(record['id'],winner)
        old,_=self.i.authorize(a['requestId'],a['requestCapability'])
        self.assertEqual(self.i.correlated(old),(None,None));self.assertEqual(self.i.view(old)['state'],'prepared')
        with self.assertRaises(AttachmentError):self.consume(d,a)

    def test_distinct_intents_cannot_consume_the_same_draft(self):
        d,_,a=self.prepared()
        b=self.i.prepare_draft(d['id'],d['capability'],fence(d),self.f.f.key(),self.f.f.binding,self.f.f.direct_profile())
        self.consume(d,a)
        with self.assertRaises(AttachmentError):self.consume(d,b)

    def test_explicit_new_intent_after_proven_pre_admission_exit(self):
        d,_,a=self.prepared();old,winner=self.consume(d,a)
        self.i.finish(old['id'],winner,'local_refused')
        b=self.i.prepare_draft(d['id'],d['capability'],fence(d),self.f.f.key(),self.f.f.binding,self.f.f.direct_profile())
        new,winner=self.consume(d,b);self.admit(d,new);self.i.finish(new['id'],winner)
        self.assertEqual(self.i.correlated(old),(None,None));self.assertEqual(self.i.view(old)['state'],'failed')
        self.assertEqual(self.i.view(new)['state'],'retained_local')
        self.assertFalse(self.consume(d,a)[1])

    def test_view_is_one_locked_snapshot_before_concurrent_consume(self):
        d,_,r=self.prepared();record,_=self.i.authorize(r['requestId'],r['requestCapability'])
        entered=threading.Event();release=threading.Event();done=threading.Event();started=threading.Event();result=[];once=[False]
        original=self.i._record
        def pause(*args):
            value=original(*args)
            if not once[0]:
                once[0]=True;entered.set();self.assertTrue(release.wait(2))
            return value
        def mutate():
            started.set();self.consume(d,r);done.set()
        with mock.patch.object(self.i,'_record',side_effect=pause):
            reader=threading.Thread(target=lambda:result.append(self.i.view(record)));reader.start()
            self.assertTrue(entered.wait(2));writer=threading.Thread(target=mutate);writer.start();self.assertTrue(started.wait(2))
            try:self.assertFalse(done.wait(.05))
            finally:release.set();reader.join(2);writer.join(2)
        self.assertEqual(result[0]['state'],'prepared');self.assertEqual(result[0]['officerDelivery']['state'],'not_sent')
        self.assertTrue(done.is_set());self.assertEqual(self.i.view(record)['state'],'preparing')


if __name__=='__main__':unittest.main()
