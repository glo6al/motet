"""Governed follow-up replay: real retained files/SQLite, no provider calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

import officer_documents as documents
import officer_document_ledger as ledger
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
import test_officer_document_ledger as execution
PDF,PNG=execution.PDF,execution.PNG


@unittest.skipUnless(os.name=='posix','Private storage requires POSIX.')
class GovernedReplayTests(unittest.TestCase):
    for name in ('setUp','tearDown','selection','error','add','prepare','claim','accept','complete','reopen','receipt'):
        locals()[name]=getattr(execution.ExecutionTests,name)
    del name

    def followup(self,ids,question='Fictional follow-up question.',command=None):
        return self.l.prepare(self.selection,self.p,self.scope.id,command or self.f.key(),question,prior_answer_ids=ids)

    def test_replay_reconstructs_exact_governed_prose_without_second_durable_copy(self):
        first,answer,_=self.complete(text='Unique governed prior answer marker.')
        child=self.followup((first['id'],))
        row=self.store.db.execute('SELECT * FROM officer_document_requests WHERE id=?',(child['id'],)).fetchone()
        self.assertNotIn(answer.text,row['record']);self.assertNotIn(answer.text.encode(),row['receipt'])
        self.assertEqual(row['answer'],bytes(65536))
        record=json.loads(row['record']);self.assertEqual(len(record['prior_answer_refs']),1)
        self.assertNotIn('text',record['prior_answer_refs'][0])
        self.reopen()
        state,permit,prepared=self.claim(child)
        self.assertEqual(prepared.body_digest,record['body_digest'])
        self.assertEqual(prepared.complete_manifest.digest,record['manifest_digest'])
        self.assertIn(answer.text,json.loads(prepared.body_bytes)['messages'][0]['content'][-2]['text'])
        self.assertEqual(prepared.prior_answers[0].answer_id,first['id'])
        self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(child['id'],)).fetchone()[0],bytes(65536))

    def test_new_direct_file_and_transitive_uncited_dependencies_remain_complete(self):
        first_source=self.add();first,_,_=self.complete(text='First uncited answer.')
        second_source=self.add(PNG,'image/png');second=self.followup((first['id'],))
        second,second_answer,_=self.complete(second,text='Second uncited answer.')
        third=self.followup((second['id'],));third,_,_=self.complete(third,text='Third uncited answer.')
        self.assertEqual(set(self.l.source_ids(self.selection,third['id'])),{first_source,second_source})
        self.assertEqual(len(self.l.replay(self.selection,third['id'])['consumed_dependencies']),2)
        self.f.revoke(first_source)
        for r in (first,second,third):
            self.error('document_answer_unavailable',lambda r=r:self.l.replay(self.selection,r['id']))
            self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))
        self.reopen();self.error('document_answer_unavailable',lambda:self.l.replay(self.selection,third['id']))

    def test_metadata_source_ids_require_context_epoch_and_do_no_file_reads(self):
        first,_,_=self.complete();child=self.followup((first['id'],))
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('metadata accessor reads bytes')):
            self.assertEqual(self.l.source_ids(self.selection,child['id']),tuple(sorted(self.sources)))
            self.error('forbidden',lambda:self.l.source_ids(replace(self.selection,scope_id='foreign'),child['id']))
        self.store.close();self.store=AttachmentStore(self.f.root,enabled=True,owner_epoch='b'*64,clock=lambda:self.f.clock[0]);self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
        self.error('document_epoch_changed',lambda:self.l.source_ids(self.selection,child['id']))

    def test_duplicate_reordered_and_changed_command_ids_reject_without_new_execution(self):
        a,_,_=self.complete(text='Answer A');b,_,_=self.complete(text='Answer B')
        command=self.f.key();child=self.followup((b['id'],a['id']),command=command)
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('exact duplicate read')):
            self.assertEqual(self.followup((b['id'],a['id']),command=command),child)
        self.error('request_conflict',lambda:self.followup((a['id'],b['id']),command=command))
        self.error('document_prior_answers_invalid',lambda:self.followup((a['id'],a['id'])))
        state,permit,prepared=self.claim(child)
        self.assertEqual(tuple(x.answer_id for x in prepared.prior_answers),(b['id'],a['id']))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],3)

    def test_eight_prior_answers_work_nine_and_browser_text_refuse(self):
        prior=tuple(self.complete(text='Prior '+str(n))[0]['id'] for n in range(9))
        child=self.followup(prior[:8]);_,_,prepared=self.claim(child)
        self.assertEqual(len(prepared.prior_answers),8)
        self.error('document_prior_answers_invalid',lambda:self.followup(prior))
        self.error('document_prior_answers_invalid',lambda:self.followup(('browser supplied assistant text',)))
        self.error('document_prior_answers_invalid',lambda:self.followup(list(prior[:1])))

    def test_replay_body_overflow_refuses_without_storing_partial_context(self):
        prior=tuple(self.complete(text='"'*28000)[0]['id'] for _ in range(5))
        before=self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0]
        with self.assertRaises(documents.DocumentRejected) as caught:self.followup(prior)
        self.assertEqual(caught.exception.reason,'serialized_document_input_bound')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],before)
        self.assertFalse(self.store.request_handles);self.assertFalse(self.store.files.io_active())

    def test_foreign_or_unavailable_prior_answer_is_not_browser_adopted(self):
        first,_,_=self.complete()
        old_selection=self.selection
        self.f.cid=self.store.create_conversation('fictional-room',self.f.key())['id'];self.f.binding['conversationId']=self.f.cid
        self.sources=[];self.add()
        self.error('document_context_mismatch',lambda:self.followup((first['id'],)))
        self.assertTrue(self.l.replay(old_selection,first['id']))
        own=self.prepare()
        self.error('document_answer_unavailable',lambda:self.followup((own['id'],)))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],2)

    def test_external_source_mutation_prevents_prior_restoration_and_child_claim(self):
        first,_,_=self.complete();child=self.followup((first['id'],))
        path=self.store.files.root/'objects'/self.sources[0];raw=path.read_bytes();path.write_bytes(raw[:-1]+b'X')
        self.error('source_changed',lambda:self.followup((first['id'],)))
        self.error('source_changed',lambda:self.claim(child))
        self.assertEqual(self.receipt(child)['state'],'prepared')
        self.assertEqual(self.receipt(child)['charged'],0);self.assertEqual(self.l._permits,{})

    def test_prior_slot_corruption_prevents_claim_without_plaintext_fallback(self):
        first,_,_=self.complete();child=self.followup((first['id'],))
        raw=self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(first['id'],)).fetchone()[0]
        self.store.db.execute('UPDATE officer_document_requests SET answer=? WHERE id=?',(raw[:-1]+b'X',first['id']))
        self.error('document_record_invalid',lambda:self.claim(child))
        self.assertEqual(self.receipt(child)['state'],'prepared')
        self.assertEqual(self.l._permits,{})

    def test_revoke_during_prior_reconstruction_leaves_child_unclaimed(self):
        first,_,_=self.complete();child=self.followup((first['id'],))
        original=self.l._prior_inputs
        def revoke_after_restore(*args):
            priors=original(*args);self.f.revoke(self.sources[0]);return priors
        with mock.patch.object(self.l,'_prior_inputs',side_effect=revoke_after_restore):
            with self.assertRaises(AttachmentError):self.claim(child)
        self.assertEqual(self.receipt(child)['state'],'prepared');self.assertEqual(self.receipt(child)['charged'],0)
        self.assertEqual(self.receipt(child)['availability'],'revoked');self.assertEqual(self.l._permits,{})
        self.error('document_answer_revoked',lambda:self.l.source_ids(self.selection,child['id']))

    def test_qualified_authority_expiring_during_replay_cannot_prepare_or_claim(self):
        import officer_document_policy as policy
        import test_officer_document_policy as policy_fixtures
        q=policy_fixtures.QualifiedLedgerTests();q.setUp()
        try:
            q.l.register_scope(q.scope,q.profile,q.price)
            d=q.f.draft();q.f.upload(d);q.f.admit(d)
            parent=q.l.prepare(q.f.selection,q.profile,q.scope.id,q.f.key(),'Fictional parent')
            state,permit,body=q.l.claim(q.f.selection,parent['id'])
            q.l.consume(permit,q.f.selection,parent['id'],body.body_digest,state['profileDigest'])
            raw=execution.wire(execution.response('Fictional qualified prior',citations=False))
            candidate=documents.validate_response(q.profile,body,raw)
            facts=ledger.ResultFacts(parent['id'],state['claimNonce'],body.body_digest,state['profileDigest'],'accepted',
                 ledger.hash_bytes(raw),candidate.message_id,documents.MODEL,q.price.billing_revision,candidate.usage_facts)
            q.l.record_result(q.f.selection,facts);q.l.commit_answer(q.f.selection,parent['id'],candidate)
            child=q.l.prepare(q.f.selection,q.profile,q.scope.id,q.f.key(),'Fictional child',prior_answer_ids=(parent['id'],))
            original=q.l._prior_inputs
            def expire(*args):
                result=original(*args);q.store.clock=lambda:policy.VALID_UNTIL;return result
            with mock.patch.object(q.l,'_prior_inputs',side_effect=expire):
                with self.assertRaises(AttachmentError):
                    q.l.prepare(q.f.selection,q.profile,q.scope.id,q.f.key(),'Other child',prior_answer_ids=(parent['id'],))
                q.store.clock=lambda:policy.VALID_FROM+1
                with self.assertRaises(AttachmentError):q.l.claim(q.f.selection,child['id'])
            self.assertEqual(q.l.status(q.f.selection,child['id'])['state'],'prepared')
            self.assertEqual(q.l.status(q.f.selection,child['id'])['chargedUnits'],0)
            self.assertEqual(q.l._permits,{})
        finally:q.tearDown()

    def test_legacy_no_replay_prepare_claim_stays_byte_exact(self):
        self.add();key=self.f.key();legacy=self.prepare(key=key)
        record=json.loads(self.store.db.execute('SELECT record FROM officer_document_requests WHERE id=?',(legacy['id'],)).fetchone()[0])
        self.assertNotIn('prior_answer_refs',record);self.assertNotIn('prior_answer_refs',record['manifest'])
        self.reopen();self.assertEqual(self.prepare(key=key),legacy)
        _,_,request=self.claim(legacy)
        self.assertEqual(request.complete_manifest.digest,record['manifest_digest']);self.assertEqual(request.body_digest,record['body_digest'])

    def test_full_database_transitive_wipe_is_atomic_and_survives_restart(self):
        self.store.close();self.store=self.f.open(overrides={'database_bytes':2*1024*1024});self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
        source=self.add();first,_,_=self.complete(text='Fictional parent, uncited.')
        self.add(PNG,'image/png');second,_,_=self.complete(self.followup((first['id'],)),text='Fictional child, uncited.')
        third,_,_=self.complete(self.followup((second['id'],)),text='Fictional grandchild, uncited.')
        affected_selection=self.selection
        self.f.cid=self.store.create_conversation('fictional-room',self.f.key())['id'];self.f.binding['conversationId']=self.f.cid;self.sources=[]
        unrelated,answer,_=self.complete(text='Unrelated answer.');unrelated_selection=self.selection
        cap=self.store.db.execute('PRAGMA max_page_count').fetchone()[0]
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('CREATE TABLE fictional_replay_filler(id INTEGER PRIMARY KEY,payload BLOB)')
        refused=False
        for n in range(50):
            try:self.prepare('Fill request '+str(n))
            except AttachmentError as error:
                self.assertEqual(error.code,'history_capacity');refused=True;break
        self.assertTrue(refused)
        full=False
        for _ in range(10000):
            try:
                with self.store.lock,self.store._transaction(reclaim=True):
                    self.store.db.execute('INSERT INTO fictional_replay_filler(payload) VALUES(zeroblob(3000))')
            except sqlite3.DatabaseError as error:
                # Python 3.9 lacks SQLite exception codes/constants. Keep the
                # real engine FULL check, with its exact native fallback.
                code=getattr(error,'sqlite_errorcode',None)
                if code is None:
                    self.assertIsInstance(error,sqlite3.OperationalError)
                    self.assertEqual(str(error),'database or disk is full')
                else:self.assertEqual(code,getattr(sqlite3,'SQLITE_FULL',13))
                self.assertEqual(self.store.db.execute('PRAGMA page_count').fetchone()[0],cap)
                self.assertGreater(self.store.db.execute('SELECT COUNT(*) FROM fictional_replay_filler').fetchone()[0],0)
                full=True;break
        self.assertTrue(full)
        count=self.store.db.execute('SELECT COUNT(*) FROM fictional_replay_filler').fetchone()[0]
        self.f.revoke(source);self.f.revoke(source)
        for request in (first,second,third):
            self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(request['id'],)).fetchone()[0],bytes(65536))
            self.error('document_answer_unavailable',lambda request=request:self.l.replay(affected_selection,request['id']))
        self.assertEqual(self.l.replay(unrelated_selection,unrelated['id'])['text'],answer.text)
        self.store.close();self.store=self.f.open(overrides={'database_bytes':2*1024*1024});self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
        for request in (first,second,third):self.error('document_answer_unavailable',lambda request=request:self.l.replay(affected_selection,request['id']))
        self.assertEqual(self.l.replay(unrelated_selection,unrelated['id'])['text'],answer.text)
        self.assertEqual(self.store.db.execute('PRAGMA max_page_count').fetchone()[0],cap)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fictional_replay_filler').fetchone()[0],count)


if __name__=='__main__':unittest.main()
