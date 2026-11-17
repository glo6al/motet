"""Real private storage/SQLite, fictional document bytes/results, zero provider IO."""
from dataclasses import asdict, replace
from decimal import localcontext, ROUND_DOWN, ROUND_UP, ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

import officer_document_ledger as ledger
import officer_documents as documents
from attachments_catalogue import HistorySelection
from attachments_conversations import OfficerConversations
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_types import AttachmentError, canonical
import test_attachments_officer_storage as storage_fixtures
PDF, PNG = storage_fixtures.PDF, storage_fixtures.PNG
from test_officer_documents import profile, response, wire


class MoneyTests(unittest.TestCase):
    def rejects(self,value):
        with self.assertRaises(AttachmentError) as caught:ledger.money(value)
        self.assertEqual(caught.exception.code,'document_budget_invalid')

    def test_default_context_preserves_exact_integer_nanodollars(self):
        for value,units in (('0.1',100_000_000),('1.05',1_050_000_000),
                            ('0.1000000000000000000000000000000000000000',100_000_000),
                            ('1.0500000000000000000000000000000000000000',1_050_000_000),
                            ('1e-9',1),('9007199.254740991',ledger.MAX_MONEY)):
            self.assertEqual(ledger.money(value),units)

    def test_default_context_rejects_inexact_values_near_point_one_and_one_point_zero_five(self):
        for value in ('0.100000000000000000000000000000000000000000000001',
                      '0.099999999999999999999999999999999999999999999999',
                      '1.050000000000000000000000000000000000000000000001',
                      '1.049999999999999999999999999999999999999999999999',
                      '0.0000000001','9007199.254740992'):
            self.rejects(value)

    def test_precision_two_and_rounding_modes_cannot_change_conversion_or_rejection(self):
        for rounding in (ROUND_DOWN,ROUND_UP,ROUND_HALF_EVEN,ROUND_FLOOR,ROUND_CEILING):
            with self.subTest(rounding=rounding),localcontext() as context:
                context.prec=2;context.rounding=rounding
                self.test_default_context_preserves_exact_integer_nanodollars()
                self.test_default_context_rejects_inexact_values_near_point_one_and_one_point_zero_five()
                self.assertEqual(ledger.money('1.234567891'),1_234_567_891)
                self.rejects('1.2345678911')

    def test_extreme_exponents_nonfinite_and_nonpositive_refuse_boundedly(self):
        for value in ('1e999999999','1e-999999999','NaN','Infinity','-1','0','-0','1e-10'):
            self.rejects(value)


@unittest.skipUnless(os.name=='posix','Private attachment storage requires POSIX.')
class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = storage_fixtures.DirectStoreTests(); self.f.setUp()
        self.store = self.f.store
        self.f.binding['modelId'] = documents.MODEL
        self.p = profile()
        self.scope = ledger.DocumentBudgetScope('fictional-budget','v1','1.00')
        self.pricing = ledger.DocumentPricing('fictional-prices-v1','fictional-billing-v1','global',1000,10000,500,1250,2000)
        self.l = ledger.OfficerDocumentLedger(self.store)
        self.l.register_scope(self.scope,self.p,self.pricing)
        self.sources = []

    def tearDown(self): self.f.tearDown()
    @property
    def selection(self): return self.f.selection
    def error(self,code,fn):
        with self.assertRaises(AttachmentError) as caught: fn()
        self.assertEqual(caught.exception.code,code)
    def add(self,raw=PDF,media='application/pdf'):
        d=self.f.draft();u,_=self.f.upload(d,raw,media);self.f.admit(d);self.sources.append(u['fileId']);return u['fileId']
    def prepare(self,question='Read the fictional files.',key=None):
        if not self.sources:self.add()
        return self.l.prepare(self.selection,self.p,self.scope.id,key or self.f.key(),question)
    def claim(self,r):return self.l.claim(self.selection,r['id'])
    def accept(self,r,*,text='Fictional answer.',citation=False):
        state,permit,prepared=self.claim(r)
        self.assertTrue(self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))
        raw=wire(response(text,citations=citation));candidate=documents.validate_response(self.p,prepared,raw)
        facts=ledger.ResultFacts(r['id'],state['claimNonce'],prepared.body_digest,state['profileDigest'],'accepted',
             hashlib.sha256(raw).hexdigest(),candidate.message_id,documents.MODEL,self.pricing.billing_revision,candidate.usage_facts)
        self.l.record_result(self.selection,facts)
        return candidate,facts
    def complete(self,r=None,**kwargs):
        r=r or self.prepare();candidate,facts=self.accept(r,**kwargs)
        self.l.commit_answer(self.selection,r['id'],candidate)
        return r,candidate,facts
    def reopen(self):
        self.store.close();self.store=self.f.open();self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
    def receipt(self,r):return ledger.unpack(self.store.db.execute('SELECT receipt FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],4096)

    def test_complete_offline_roundtrip_and_no_raw_intermediate_result(self):
        self.add();self.add(PNG,'image/png')
        r=self.prepare();candidate,facts=self.accept(r,text='Fictional final.\nIMAGE_SOURCE: image-1')
        row=self.store.db.execute('SELECT * FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()
        self.assertEqual(row['answer'],bytes(65536));self.assertNotIn('Fictional final',row['record'])
        self.assertNotIn(b'Fictional final',row['receipt'])
        self.l.commit_answer(self.selection,r['id'],candidate)
        self.assertEqual(self.l.replay(self.selection,r['id'])['text'],candidate.text)
        self.assertEqual(len(candidate.consumed_dependencies),2)
        self.assertTrue(self.l.status(self.selection,r['id'])['answerAvailable'])
        for table in ('attachment_requests','workspace_copies','attachment_outbox'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.store.request_handles,set());self.assertFalse(self.store.files.io_active())

    def test_exact_prepare_claim_result_replays_do_not_mint_io_or_reservation(self):
        self.add();key=self.f.key();r=self.prepare(key=key)
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('duplicate read')):
            self.assertEqual(self.prepare(key=key),r)
        state,permit,prepared=self.claim(r)
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('duplicate claim read')):
            self.assertEqual(self.claim(r),(state,None,None))
        self.assertEqual(self.receipt(r)['charged'],200_000_000)
        self.error('document_permit_invalid',lambda:self.l.consume(object(),self.selection,r['id'],prepared.body_digest,state['profileDigest']))
        self.assertTrue(self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))
        self.error('document_permit_invalid',lambda:self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))

    def test_global_command_identity_and_context_conflict(self):
        self.add();key=self.f.key();r=self.prepare(key=key)
        self.error('request_conflict',lambda:self.prepare(question='Different question',key=key))
        other=replace(self.selection,scope_id='fictional-other')
        self.error('forbidden',lambda:self.l.status(other,r['id']))
        foreign=self.store.create_conversation('fictional-room',self.f.key())['id']
        self.error('forbidden',lambda:self.l.status(replace(self.selection,conversation_id=foreign),r['id']))
        occupied=self.f.key();self.store.create_conversation('fictional-room',occupied)
        self.error('request_conflict',lambda:self.prepare(key=occupied))

    def test_complete_roster_new_upload_before_claim_refuses(self):
        r=self.prepare();self.add(PNG,'image/png')
        self.error('document_roster_changed',lambda:self.claim(r))
        self.assertEqual(self.receipt(r)['state'],'prepared')

    def test_complete_roster_51_sources_and_mixed_text_refuse_without_reads(self):
        for _ in range(51):self.add()
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('partial selection read')):
            self.error('document_complete_roster_required',self.prepare)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],0)

    def test_cross_request_candidate_and_write_once_answers(self):
        first=self.prepare('Question A');second=self.prepare('Question B')
        a,af=self.accept(first);b,bf=self.accept(second)
        self.error('document_answer_correlation',lambda:self.l.commit_answer(self.selection,second['id'],a))
        original=self.l.commit_answer(self.selection,first['id'],a)
        self.assertEqual(self.l.commit_answer(self.selection,first['id'],a),original)
        self.error('document_answer_conflict',lambda:self.l.commit_answer(self.selection,first['id'],replace(a,text='Changed answer')))
        self.assertEqual(self.l.replay(self.selection,first['id'])['text'],a.text)

    def test_missing_strict_scope_and_mutation_of_global_terms_refuse(self):
        self.add()
        self.error('document_budget_missing',lambda:self.l.prepare(self.selection,self.p,'absent',self.f.key(),'Fictional'))
        self.error('document_budget_conflict',lambda:self.l.register_scope(replace(self.scope,usd_limit='2'),self.p,self.pricing))
        self.error('document_budget_conflict',lambda:self.l.register_scope(replace(self.scope,id='new-scope'),self.p,self.pricing))
        self.error('document_budget_invalid',lambda:self.l.register_scope(self.scope,self.p,replace(self.pricing,input_rate=True)))
        with self.assertRaises(documents.DocumentRejected):self.l.prepare(self.selection,replace(self.p,strict_budget_evidence=None),self.scope.id,self.f.key(),'Fictional')

    def test_combined_budget_unknown_restart_and_conversation_cannot_reset_debt(self):
        requests=[self.prepare('Fictional '+str(n)) for n in range(6)]
        for r in requests[:5]:
            _,permit,_=self.claim(r)
            self.l._permits.pop(permit)  # Simulate lost process-local permit, no refund.
        self.error('document_budget_exhausted',lambda:self.claim(requests[5]))
        self.reopen()
        self.error('document_budget_exhausted',lambda:self.claim(requests[5]))
        self.l.register_scope(self.scope,self.p,self.pricing)
        for r in requests[:5]:self.assertEqual(self.claim(r)[1:],(None,None))
        self.f.cid=self.store.create_conversation('fictional-room',self.f.key())['id']
        self.f.binding['conversationId']=self.f.cid;self.sources=[]
        new=self.prepare()
        self.error('document_budget_exhausted',lambda:self.claim(new))

    def test_exact_usage_settlement_is_integer_idempotent_and_no_caller_cost(self):
        r=self.prepare();candidate,facts=self.accept(r)
        result=self.l.record_result(self.selection,facts)
        self.assertEqual(result['chargedUnits'],100*1000+80*10000)
        self.assertTrue(result['settled'])
        self.assertEqual(self.l.record_result(self.selection,facts),result)
        self.error('document_result_conflict',lambda:self.l.record_result(self.selection,replace(facts,usage=replace(facts.usage,input_tokens=101))))
        self.assertEqual(self.receipt(r)['charged'],result['chargedUnits'])

    def test_unknown_retains_ceiling_halts_and_late_usage_does_not_unhalt(self):
        r=self.prepare();state,permit,prepared=self.claim(r)
        unknown=ledger.ResultFacts(r['id'],state['claimNonce'],prepared.body_digest,state['profileDigest'],'unknown','',
              '',documents.MODEL,self.pricing.billing_revision,None,'transport_unknown')
        unknown_status=self.l.record_result(self.selection,unknown)
        self.assertEqual(unknown_status['chargedUnits'],200_000_000);self.assertFalse(unknown_status['settled'])
        raw=wire(response('Fictional',citations=False));candidate=documents.validate_response(self.p,prepared,raw)
        accepted=replace(unknown,state='accepted',response_digest=hashlib.sha256(raw).hexdigest(),provider_message_id=candidate.message_id,usage=candidate.usage_facts,error='none')
        settled=self.l.record_result(self.selection,accepted)
        self.assertTrue(settled['settled']);self.assertEqual(self.l.record_result(self.selection,unknown),settled)
        later=self.prepare('Later');self.error('document_budget_halted',lambda:self.claim(later))

    def unexpected(self,change):
        r=self.prepare();state,permit,prepared=self.claim(r)
        raw=wire(response('Fictional',citations=False));candidate=documents.validate_response(self.p,prepared,raw)
        f=ledger.ResultFacts(r['id'],state['claimNonce'],prepared.body_digest,state['profileDigest'],'accepted',hashlib.sha256(raw).hexdigest(),candidate.message_id,documents.MODEL,self.pricing.billing_revision,candidate.usage_facts)
        result=self.l.record_result(self.selection,replace(f,**change))
        self.assertEqual(result['chargedUnits'],200_000_000);self.assertFalse(result['settled'])
        self.assertFalse(result['answerAvailable'])
        self.error('document_budget_halted',lambda:self.claim(self.prepare('Later')))

    def test_unexpected_model_never_refunds_or_publishes(self):self.unexpected({'model_id':'wrong-model'})
    def test_unexpected_billing_never_refunds_or_publishes(self):self.unexpected({'billing_revision':'wrong-billing'})
    def test_missing_usage_never_refunds_or_publishes(self):self.unexpected({'usage':None})

    def test_permit_copy_expiry_pid_epoch_and_deadline(self):
        r=self.prepare();state,permit,prepared=self.claim(r)
        consume=lambda p:self.l.consume(p,self.selection,r['id'],prepared.body_digest,state['profileDigest'])
        self.error('document_permit_invalid',lambda:consume(copy.copy(permit)))
        with mock.patch.object(ledger.os,'getpid',return_value=os.getpid()+1):self.error('document_permit_invalid',lambda:consume(permit))
        original=self.l._process_epoch;self.l._process_epoch='f'*64
        self.error('document_permit_invalid',lambda:consume(permit));self.l._process_epoch=original
        with mock.patch.object(ledger.time,'monotonic',return_value=float('inf')):self.error('document_permit_invalid',lambda:consume(permit))
        self.reopen();self.error('document_permit_invalid',lambda:self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))
        self.assertEqual(self.claim(r)[1:],(None,None))

    def test_concurrent_claim_and_consume_have_single_winner(self):
        r=self.prepare();outputs=[];errors=[]
        def claim():
            try:outputs.append(self.claim(r))
            except BaseException as e:errors.append(e)
        threads=[threading.Thread(target=claim) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[])
        winners=[v for v in outputs if v[1] is not None];self.assertEqual(len(winners),1)
        state,permit,prepared=winners[0];consumed=[]
        def consume():
            try:consumed.append(self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))
            except AttachmentError:consumed.append(False)
        threads=[threading.Thread(target=consume) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sorted(consumed),[False,True])

    def test_tracked_actual_reads_at_every_private_boundary(self):
        self.add();self.add(PNG,'image/png');opened=[]
        original=self.store.files._opened
        def check(token):
            original(token)
            self.assertTrue(self.store.files.io_active());self.assertTrue(self.store.request_handles)
            opened.append(1)
        with mock.patch.object(self.store.files,'_opened',side_effect=check):
            r=self.prepare();candidate,facts=self.accept(r,text='Fictional answer.')
            self.l.commit_answer(self.selection,r['id'],candidate)
            def publish(value):
                self.assertTrue(self.store.request_handles)
                self.error('upload_busy',self.store.close)
                return value
            self.l.publish(self.selection,r['id'],publish)
        self.assertEqual(len(opened),10) # prepare, claim, consume, commit, publish x two files
        self.assertFalse(self.store.files.io_active());self.assertFalse(self.store.request_handles)

    def test_external_mutations_refuse_claim_consume_commit_and_restore(self):
        r,candidate,facts=self.complete()
        fid=self.sources[0]
        path=self.store.files.root/'objects'/fid
        self.assertTrue(path.exists())
        original=path.read_bytes()
        path.write_bytes(original[:-1]+b'X')
        self.error('source_changed',lambda:self.l.replay(self.selection,r['id']))
        self.assertFalse(self.store.files.io_active());self.assertFalse(self.store.request_handles)

    def test_uncited_dependency_revoke_wipes_slot_and_late_answer_is_denied(self):
        a=self.add();b=self.add(PNG,'image/png');r,candidate,_=self.complete(text='No citations.')
        committed=self.receipt(r)
        self.assertTrue(self.l.status(self.selection,r['id'])['answerCommitted'])
        before=self.store.storage()['usedBytes'];self.f.revoke(b)
        self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))
        self.assertEqual(self.l.status(self.selection,r['id'])['availability'],'revoked')
        self.assertTrue(self.l.status(self.selection,r['id'])['answerCommitted'])
        self.assertEqual((self.receipt(r)['candidate'],self.receipt(r)['answer_digest']),
                         (committed['candidate'],committed['answer_digest']))
        self.error('document_answer_revoked',lambda:self.l.commit_answer(self.selection,r['id'],candidate))
        self.error('document_answer_unavailable',lambda:self.l.replay(self.selection,r['id']))
        self.assertEqual(self.store.storage()['usedBytes'],before)
        self.reopen();self.error('document_answer_unavailable',lambda:self.l.replay(self.selection,r['id']))
        self.assertTrue(self.l.status(self.selection,r['id'])['answerCommitted'])

    def test_uncommitted_acceptance_never_gains_commitment_on_revocation(self):
        r=self.prepare();self.accept(r)
        before=self.l.status(self.selection,r['id'])
        self.assertEqual(before['state'],'accepted');self.assertFalse(before['answerCommitted'])
        self.f.revoke(self.sources[0]);after=self.l.status(self.selection,r['id'])
        self.assertEqual(after['availability'],'revoked');self.assertFalse(after['answerCommitted'])
        self.assertEqual(after['chargedUnits'],before['chargedUnits']);self.assertEqual(after['settled'],before['settled'])
        self.reopen();self.assertFalse(self.l.status(self.selection,r['id'])['answerCommitted'])

    def test_commitment_digest_pair_and_compatible_receipt_are_required(self):
        r,_,_=self.complete();original=self.receipt(r)
        changes=[{'candidate':''},{'answer_digest':''},{'availability':'unavailable'},
                 {'state':'unknown'},{'state':'failed'},
                 {'settled':False,'charged':original['reserved']}, {'error':'usage_unverified'}]
        for availability in ('available','revoked'):
            for change in changes:
                receipt=dict(original,availability=availability);receipt.update(change)
                self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',
                                     (ledger.pack(receipt,4096),r['id']))
                self.error('document_record_invalid',lambda:self.l.status(self.selection,r['id']))
        self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',
                             (ledger.pack(original,4096),r['id']))
        self.assertTrue(self.l.status(self.selection,r['id'])['answerCommitted'])

    def test_dependency_fanout_64_refuses_without_eviction(self):
        for n in range(64):self.prepare('Fictional '+str(n))
        self.error('document_dependency_capacity',lambda:self.prepare('Overflow'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0],64)
        self.f.revoke(self.sources[0])
        self.assertEqual(sum(ledger.unpack(r[0],4096)['availability']=='revoked' for r in self.store.db.execute('SELECT receipt FROM officer_document_requests')),64)

    def test_claim_and_answer_crash_boundaries_never_remint_or_publish_raw(self):
        r=self.prepare()
        def lost_commit(name):
            if name=='officer_document_claim_committed':raise RuntimeError('fictional lost acknowledgement')
        self.store.checkpoint=lost_commit
        with self.assertRaises(RuntimeError):self.claim(r)
        self.store.checkpoint=lambda _:None
        self.assertEqual(self.claim(r)[1:],(None,None));self.assertEqual(self.l._permits,{})
        self.assertEqual(self.receipt(r)['state'],'claimed')
        self.reopen();self.assertEqual(self.claim(r)[1:],(None,None))

    def test_physical_sqlite_full_revokes_uncited_answers_without_allocating(self):
        self.store.close();self.store=self.f.open(overrides={'database_bytes':2*1024*1024});self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
        first_source=self.add();self.add(PNG,'image/png')
        first,first_answer,_=self.complete(text='First answer without citations.')
        second,_,_=self.complete(text='Second answer without citations.')
        pending=self.prepare('Pending answer');pending_answer,_=self.accept(pending)
        first_selection=self.selection
        self.f.cid=self.store.create_conversation('fictional-room',self.f.key())['id']
        self.f.binding['conversationId']=self.f.cid;self.sources=[]
        unrelated,unrelated_answer,_=self.complete(text='Unrelated answer.')
        unrelated_selection=self.selection
        cap=self.store.db.execute('PRAGMA max_page_count').fetchone()[0]
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('CREATE TABLE fictional_document_filler(id INTEGER PRIMARY KEY,payload BLOB)')
        refused=False
        for n in range(50):
            try:self.prepare('Headroom '+str(n))
            except AttachmentError as error:
                self.assertEqual(error.code,'history_capacity');refused=True;break
        self.assertTrue(refused,'API did not enforce cleanup headroom')
        physical_full=False
        for _ in range(10000):
            try:
                with self.store.lock,self.store._transaction(reclaim=True):
                    self.store.db.execute('INSERT INTO fictional_document_filler(payload) VALUES(zeroblob(3000))')
            except sqlite3.DatabaseError as error:
                # Python 3.9 lacks SQLite exception codes/constants. Keep the
                # real engine FULL check, with its exact native fallback.
                code=getattr(error,'sqlite_errorcode',None)
                if code is None:
                    self.assertIsInstance(error,sqlite3.OperationalError)
                    self.assertEqual(str(error),'database or disk is full')
                else:self.assertEqual(code,getattr(sqlite3,'SQLITE_FULL',13))
                self.assertEqual(self.store.db.execute('PRAGMA page_count').fetchone()[0],cap)
                self.assertGreater(self.store.db.execute('SELECT COUNT(*) FROM fictional_document_filler').fetchone()[0],0)
                physical_full=True;break
        self.assertTrue(physical_full)
        filled=self.store.db.execute('SELECT COUNT(*) FROM fictional_document_filler').fetchone()[0]
        used=self.store.storage()['usedBytes']
        self.f.revoke(first_source);self.f.revoke(first_source)
        for r in (first,second,pending):
            self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))
            self.error('document_answer_unavailable',lambda r=r:self.l.replay(first_selection,r['id']))
        self.error('document_answer_revoked',lambda:self.l.commit_answer(first_selection,pending['id'],pending_answer))
        self.assertEqual(self.l.replay(unrelated_selection,unrelated['id'])['text'],unrelated_answer.text)
        self.assertEqual(self.store.storage()['usedBytes'],used)
        self.assertEqual(self.store.db.execute('PRAGMA max_page_count').fetchone()[0],cap)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fictional_document_filler').fetchone()[0],filled)
        self.store.close();self.store=self.f.open(overrides={'database_bytes':2*1024*1024});self.f.store=self.store
        self.l=ledger.OfficerDocumentLedger(self.store)
        self.error('document_answer_unavailable',lambda:self.l.replay(first_selection,first['id']))
        self.assertEqual(self.l.replay(unrelated_selection,unrelated['id'])['text'],unrelated_answer.text)
        self.assertEqual(self.store.db.execute('PRAGMA max_page_count').fetchone()[0],cap)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM fictional_document_filler').fetchone()[0],filled)

    def mutate_source(self):
        path=self.store.files.root/'objects'/self.sources[0]
        raw=path.read_bytes();path.write_bytes(raw[:-1]+b'X')

    def test_actual_same_inode_mutation_before_claim_refuses(self):
        r=self.prepare();self.mutate_source()
        self.error('source_changed',lambda:self.claim(r))
        self.assertEqual(self.receipt(r)['state'],'prepared')

    def test_actual_same_inode_mutation_after_claim_before_consume_refuses(self):
        r=self.prepare();state,permit,prepared=self.claim(r);self.mutate_source()
        self.error('source_changed',lambda:self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest']))
        self.assertEqual(self.receipt(r)['state'],'claimed')

    def test_actual_inode_replacement_before_answer_refuses(self):
        r=self.prepare();candidate,_=self.accept(r)
        path=self.store.files.root/'objects'/self.sources[0];raw=path.read_bytes();path.unlink();path.write_bytes(raw);path.chmod(0o600)
        self.error('source_changed',lambda:self.l.commit_answer(self.selection,r['id'],candidate))
        self.assertEqual(self.receipt(r)['availability'],'unavailable')

    def test_revocation_during_final_read_prevents_answer_publication(self):
        r=self.prepare();candidate,_=self.accept(r)
        original=self.store.files.read
        def revoke_after_read(*args,**kwargs):
            result=original(*args,**kwargs);self.f.revoke(self.sources[0]);return result
        with mock.patch.object(self.store.files,'read',side_effect=revoke_after_read):
            self.error('source_changed',lambda:self.l.commit_answer(self.selection,r['id'],candidate))
        self.assertEqual(self.receipt(r)['availability'],'revoked')
        self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))

    def test_answer_failure_rolls_back_and_never_stores_provider_raw(self):
        r=self.prepare();candidate,_=self.accept(r)
        def crash(name):
            if name=='officer_document_answer_before_commit':raise RuntimeError('fictional fault')
        self.store.checkpoint=crash
        with self.assertRaises(RuntimeError):self.l.commit_answer(self.selection,r['id'],candidate)
        self.store.checkpoint=lambda _:None
        self.assertEqual(self.receipt(r)['availability'],'unavailable')
        self.assertNotIn('answer_committed_ms',self.receipt(r))
        self.assertIsNone(self.l.status(self.selection,r['id'])['answeredAt'])
        self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))
        self.reopen();self.assertEqual(self.receipt(r)['state'],'accepted')
        self.assertFalse(self.l.status(self.selection,r['id'])['answerAvailable'])

    def test_answer_commit_timestamp_records_completion_once_and_survives_withdrawal(self):
        first=self.prepare('First recorded question');second=self.prepare('Second recorded question')
        candidate_first,_=self.accept(first);candidate_second,_=self.accept(second)
        base=self.f.clock[0];self.f.clock[0]=base+3.125
        committed_second=self.l.commit_answer(self.selection,second['id'],candidate_second)
        self.f.clock[0]=base+4.250
        committed_first=self.l.commit_answer(self.selection,first['id'],candidate_first)
        self.assertEqual(committed_second['answeredAt'],base+3.125)
        self.assertEqual(committed_first['answeredAt'],base+4.250)
        self.f.clock[0]=base+20
        self.assertEqual(self.l.commit_answer(self.selection,second['id'],candidate_second),committed_second)
        self.assertEqual(self.l.replay(self.selection,first['id'])['text'],candidate_first.text)
        self.f.revoke(self.sources[0]);self.reopen()
        for r,expected in ((first,committed_first),(second,committed_second)):
            state=self.l.status(self.selection,r['id'])
            self.assertEqual(state['availability'],'revoked');self.assertEqual(state['answeredAt'],expected['answeredAt'])
            self.error('document_answer_unavailable',lambda:self.l.replay(self.selection,r['id']))

    def test_legacy_and_malformed_answer_timestamp_receipts(self):
        r,_,_=self.complete();saved=self.receipt(r)
        for value in (None,True,-1,1.25,2**53,'100'):
            receipt=dict(saved,answer_committed_ms=value)
            self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(ledger.pack(receipt,4096),r['id']))
            self.error('document_record_invalid',lambda:self.l.status(self.selection,r['id']))
        legacy=dict(saved);del legacy['answer_committed_ms']
        self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(ledger.pack(legacy,4096),r['id']))
        self.assertIsNone(self.l.status(self.selection,r['id'])['answeredAt'])
        self.assertEqual(self.l.replay(self.selection,r['id'])['text'],'Fictional answer.')
        other=self.prepare('Uncommitted question');uncommitted=dict(self.receipt(other),answer_committed_ms=100)
        self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(ledger.pack(uncommitted,4096),other['id']))
        self.error('document_record_invalid',lambda:self.l.status(self.selection,other['id']))

    def test_fixed_slot_codec_bounds_and_corruption_fail_closed(self):
        r,candidate,_=self.complete()
        row=self.store.db.execute('SELECT receipt,answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()
        self.assertEqual(tuple(map(len,row)),(4096,65536))
        self.assertEqual(len(ledger.pack({'v':1,'text':'x'*60000},65536)),65536)
        self.error('document_slot_capacity',lambda:ledger.pack({'v':1,'text':'x'*65536},65536))
        corrupt=row['answer'][:-1]+b'X'
        self.store.db.execute('UPDATE officer_document_requests SET answer=? WHERE id=?',(corrupt,r['id']))
        self.error('document_record_invalid',lambda:self.l.replay(self.selection,r['id']))

    def test_schema8_to9_failure_is_atomic_and_preserves_retained_sources(self):
        self.add();before=self.store.storage()['usedBytes']
        from attachment_schema_fixtures import drop_document_schema
        with self.store._gate(),self.store._transaction(reclaim=True):
            drop_document_schema(self.store.db);self.store.db.execute('PRAGMA user_version=8')
        schema=self.store.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        def crash(name):
            if name=='officer_document_migration_before_commit':raise RuntimeError('fictional migration fault')
        self.store.checkpoint=crash
        with self.assertRaises(RuntimeError):
            with self.store._gate(),self.store._transaction(reclaim=True):ledger.migrate(self.store)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],8)
        self.assertEqual(self.store.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(),schema)
        self.store.checkpoint=lambda _:None
        self.reopen()
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.store.storage()['usedBytes'],before)
        self.assertTrue(OfficerConversations(self.store).context_sources(self.selection)['dependencies'])

    def test_disabled_and_demo_store_create_nothing(self):
        for options in ({},{'enabled':True,'demo':True}):
            root=self.f.root/('fictional-disabled-'+str(len(options)))
            store=AttachmentStore(root,**options)
            self.assertFalse(root.exists());self.assertIsNone(store.db)
            self.error('attachments_disabled',lambda:ledger.OfficerDocumentLedger(store))
            store.close()

    def test_immutable_metadata_and_dependency_rows_cannot_be_updated(self):
        r=self.prepare()
        for sql in ("UPDATE officer_document_requests SET record='{}'", "UPDATE officer_document_dependencies SET record='{}'",
                    "UPDATE officer_document_scopes SET configuration='{}'"):
            with self.assertRaises(sqlite3.IntegrityError):self.store.db.execute(sql)
        self.assertEqual(self.l.status(self.selection,r['id'])['state'],'prepared')

    def test_malformed_candidate_text_and_reference_never_persist(self):
        r=self.prepare();candidate,_=self.accept(r)
        self.error('document_answer_invalid',lambda:self.l.commit_answer(self.selection,r['id'],replace(candidate,text={'raw':'unvalidated'})))
        self.error('document_answer_invalid',lambda:self.l.commit_answer(self.selection,r['id'],replace(candidate,citations=({'raw':'unvalidated'},))))
        self.assertEqual(self.store.db.execute('SELECT answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()[0],bytes(65536))

    def test_concurrent_claims_cannot_cross_shared_remaining_budget(self):
        requests=[self.prepare('Concurrent '+str(n)) for n in range(6)]
        for r in requests[:4]:
            state,permit,prepared=self.claim(r)
            self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest'])
        outputs=[];errors=[]
        def claim(r):
            try:outputs.append((r,self.claim(r)))
            except AttachmentError as error:errors.append((r,error.code))
        threads=[threading.Thread(target=claim,args=(r,)) for r in requests[4:]]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(outputs),1);self.assertEqual(len(errors),1)
        self.assertIn(errors[0][1],('document_budget_exhausted','resource_busy'))
        self.error('document_budget_exhausted',lambda:self.claim(errors[0][0]))
        total=sum(ledger.unpack(row[0],4096)['charged'] for row in self.store.db.execute('SELECT receipt FROM officer_document_requests'))
        self.assertEqual(total,1_000_000_000)

    def test_fifty_sources_fit_frozen_manifest_and_every_uncited_dependency(self):
        for _ in range(50):self.add()
        r=self.prepare(''*4000)
        state,permit,prepared=self.claim(r)
        self.assertEqual(len(prepared.complete_manifest.dependencies),50)
        self.l.consume(permit,self.selection,r['id'],prepared.body_digest,state['profileDigest'])
        raw=wire(response('Fictional fifty-source answer.',citations=False))
        candidate=documents.validate_response(self.p,prepared,raw)
        facts=ledger.ResultFacts(r['id'],state['claimNonce'],prepared.body_digest,state['profileDigest'],'accepted',hashlib.sha256(raw).hexdigest(),candidate.message_id,documents.MODEL,self.pricing.billing_revision,candidate.usage_facts)
        self.l.record_result(self.selection,facts);self.l.commit_answer(self.selection,r['id'],candidate)
        row=self.store.db.execute('SELECT record,receipt,answer FROM officer_document_requests WHERE id=?',(r['id'],)).fetchone()
        self.assertLess(len(row['record'].encode()),131072);self.assertEqual((len(row['receipt']),len(row['answer'])),(4096,65536))
        self.assertEqual(len(self.l.replay(self.selection,r['id'])['consumed_dependencies']),50)
        self.f.revoke(self.sources[-1]);self.error('document_answer_unavailable',lambda:self.l.replay(self.selection,r['id']))

    def test_revocation_participant_requires_source_fence(self):
        self.prepare()
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.error('document_revocation_required',lambda:ledger.invalidate_source_in_transaction(self.store,self.sources[0]))
        self.assertEqual(self.receipt({'id':self.store.db.execute('SELECT id FROM officer_document_requests').fetchone()[0]})['availability'],'unavailable')


if __name__=='__main__':unittest.main()
