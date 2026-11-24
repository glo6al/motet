"""Real-store file projections with fictional results; no provider transport."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from attachments_types import AttachmentError
from officer_document_projection import OfficerDocumentProjection
from officer_document_intents import OfficerIntents
import test_officer_document_intents as fixtures
from test_officer_document_ledger import PNG
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY


@unittest.skipUnless(STORE, STORE_WHY)
class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.t=fixtures.IntentTests();self.t.setUp()
        self.i=self.t.i;self.store=self.t.store;self.f=self.t.f
        self.p=OfficerDocumentProjection(self.i);self.winners={}
    def tearDown(self):self.t.tearDown()
    @property
    def selection(self):return self.f.selection
    def admission(self):
        d,_,public=self.t.prepared();record,winner=self.t.consume(d,public);self.winners[record['id']]=winner;message=self.t.admit(d,record)
        return public,record,message
    def execution(self,record,message=None):
        return self.f.l.prepare(self.selection,self.f.p,self.f.scope.id,self.i.command(record,'execution'),
            record['question'],local_message_id=message['id'] if message else None,prior_answer_ids=tuple(record['prior_answers']))
    def question(self,prior=()):
        public=self.i.prepare_question(self.f.f.binding,'Fictional follow-up',list(prior),self.f.f.direct_profile(),self.f.f.key())
        record,winner=self.i.consume(public['requestId'],public['requestCapability'],fingerprint=public['fingerprint'])
        self.winners[record['id']]=winner
        return public,record
    def finish(self,record):self.i.finish(record['id'],self.winners[record['id']])
    def one(self,**kwargs):return self.p.history(self.selection,**kwargs)['items'][0]
    def test_local_only_metadata_never_reads_or_renders_original(self):
        public,record,message=self.admission();self.finish(record)
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('metadata read source bytes')):
            item=self.one();value=self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
        self.assertEqual(item['officerDelivery'],{'state':'retained_local','requestId':public['requestId']})
        self.assertEqual(item['admissionRequestId'],public['requestId'])
        self.assertFalse(item['previewAvailable']);self.assertTrue(item['previewable']);self.assertFalse(value['available'])
        self.assertEqual(set(item['officerDelivery']),{'state','requestId'})
        self.assertEqual(value['kind'],'fallback');self.assertEqual(value['officerPriorDelivery'],'none')
        self.assertEqual(value['payload']['noticeParts']['title'],'No preview for this kind of file yet')
        self.assertEqual(value['payload']['footer'],'Nothing was checked or drawn.')
        self.assertFalse(set(value)&{'text','lines','url','data','pages','pixels'})
        self.assertEqual(value['officerState'],'retained_local')
    def test_legacy_admission_has_no_invented_public_id(self):
        self.f.add()
        item=self.one()
        self.assertEqual(item['officerDelivery'],{'state':'retained_local','requestId':None})
        self.assertIsNone(item['admissionRequestId'])
        self.assertIsNotNone(item['sourceCapability'])
    def test_confirmed_uses_complete_dependencies_even_without_citations(self):
        public,record,message=self.admission()
        # Additional retained image is uncited, but its original is still input.
        image=self.f.add(PNG,'image/png')
        ex=self.execution(record,message);self.f.complete(ex,text='No source citations.',citation=False)
        result=self.p.history(self.selection,request_id=public['requestId'])
        self.assertEqual(len(result['items']),2)
        for item in result['items']:
            self.assertEqual(item['officerDelivery']['state'],'confirmed')
            self.assertEqual(item['officerDelivery']['requestId'],public['requestId'])
            self.assertNotEqual(item['officerDelivery']['requestId'],ex['id'])
    def test_exact_current_retained_attempt_preserves_prior_confirmed(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex)
        later,current=self.question((ex['id'],));self.execution(current);self.finish(current)
        item=self.one(request_id=later['requestId'])
        self.assertEqual(item['officerDelivery'],{'state':'confirmed','requestId':public['requestId']})
        preview=self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
        self.assertEqual(preview['officerState'],'retained_local');self.assertEqual(preview['officerPriorDelivery'],'confirmed')
        self.assertEqual(self.one()['officerDelivery']['requestId'],public['requestId'])
    def test_claimed_without_acceptance_is_unknown_not_none(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.l.claim(self.selection,ex['id'])
        self.assertEqual(self.one()['officerDelivery']['state'],'unknown')
        later,current=self.question();self.execution(current);self.finish(current)
        item=self.one(request_id=later['requestId'])
        self.assertEqual(self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)['officerPriorDelivery'],'unknown')
    def test_missing_execution_command_receipt_refuses_instead_of_no_call(self):
        _,record,message=self.admission();self.execution(record,message)
        self.store.db.execute('DELETE FROM seen WHERE key=?',(self.i.command(record,'execution'),))
        with self.assertRaises(AttachmentError):self.one()
    def test_unmapped_execution_refuses_instead_of_no_call(self):
        self.f.add();self.f.prepare()
        with self.assertRaises(AttachmentError):self.one()
    def test_corrupt_result_receipt_refuses(self):
        _,record,message=self.admission();ex=self.execution(record,message)
        self.store.db.execute('UPDATE officer_document_requests SET receipt=zeroblob(4096) WHERE id=?',(ex['id'],))
        with self.assertRaises(AttachmentError):self.one()
    def test_context_process_and_generation_capabilities_cannot_be_reused(self):
        _,record,_=self.admission();self.finish(record);item=self.one();cap=item['sourceCapability']
        other=OfficerDocumentProjection(self.i)
        with self.assertRaises(AttachmentError):other.preview(item['id'],cap,None,lambda x:x)
        with self.assertRaises(AttachmentError):self.p.history(replace(self.selection,scope_id='other'))
        self.f.f.revoke(item['id'])
        with self.assertRaises(AttachmentError):self.p.preview(item['id'],cap,None,lambda x:x)
        removed=self.one();self.assertFalse(removed['available']);self.assertIsNone(removed['sourceCapability'])
    def test_citation_authority_revalidates_uncited_dependencies_and_never_returns_prose(self):
        public,record,message=self.admission();uncited=self.f.add(PNG,'image/png')
        ex=self.execution(record,message);self.f.complete(ex,citation=True)
        pdf=message['files'][0]['id']
        cap=self.p.citation_authority(public['requestId'],public['requestCapability'],ex['id'],pdf,{'page':2})
        out=self.p.preview(pdf,cap,{'page':2},lambda x:x)
        self.assertFalse(out['available']);self.assertNotIn('text',out)
        self.assertEqual(out['officerState'],'cited_page');self.assertIsNone(out['officerPriorDelivery'])
        self.assertIsNone(out['location']);self.assertIsNone(out['pageCount']);self.assertIsNone(out['lineCount']);self.assertEqual(out['navigation'],{})
        self.assertIn('cited page 2',out['payload']['noticeParts']['body'])
        with self.assertRaises(AttachmentError):self.p.preview(pdf,cap,{'page':3},lambda x:x)
        self.f.f.revoke(uncited)
        published=[]
        with self.assertRaises(AttachmentError):self.p.preview(pdf,cap,{'page':2},published.append)
        self.assertEqual(published,[])
    def test_citation_external_source_mutation_refuses_before_publication(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex,citation=True)
        file_id=message['files'][0]['id'];cap=self.p.citation_authority(public['requestId'],public['requestCapability'],ex['id'],file_id,{'page':2})
        path=self.store.files.root/'objects'/file_id;raw=path.read_bytes();path.write_bytes(raw[:-1]+b'X')
        published=[]
        with self.assertRaises(AttachmentError):self.p.preview(file_id,cap,{'page':2},published.append)
        self.assertEqual(published,[])
    def test_no_citation_cannot_mint_citation_authority_or_cross_answer(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex,citation=False)
        with self.assertRaises(AttachmentError):self.p.citation_authority(public['requestId'],public['requestCapability'],ex['id'],message['files'][0]['id'],{'page':2})
        later,current=self.question();other=self.execution(current);self.f.complete(other,citation=True)
        with self.assertRaises(AttachmentError):self.p.citation_authority(public['requestId'],public['requestCapability'],other['id'],message['files'][0]['id'],{'page':2})
    def test_live_unmapped_other_intent_keeps_prior_uncertain(self):
        _,record,_=self.admission();self.finish(record);self.question()
        item=self.one()
        self.assertEqual(self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)['officerPriorDelivery'],'unknown')
    def test_confirmed_generation_survives_revocation_without_restoring_availability(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex)
        source=message['files'][0]['id'];self.f.f.revoke(source)
        item=self.one();self.assertEqual(item['officerDelivery']['state'],'confirmed')
        self.assertFalse(item['available']);self.assertIsNone(item['sourceCapability'])
        self.assertEqual(item['admissionRequestId'],public['requestId'])

    def test_complete_evidence_does_not_depend_on_displayed_request_page(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex)
        for _ in range(51):
            self.i.prepare_question(self.f.f.binding,'Other unsent question',[],self.f.f.direct_profile(),self.f.f.key())
        later,current=self.question((ex['id'],));self.execution(current);self.finish(current)
        item=self.one(request_id=later['requestId'])
        self.assertEqual(self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)['officerPriorDelivery'],'confirmed')

    def test_file_history_paginates_without_hiding_local_only_facts(self):
        for _ in range(51):self.f.add()
        first=self.p.history(self.selection);second=self.p.history(self.selection,cursor=first['nextCursor'])
        self.assertEqual(len(first['items']),50);self.assertEqual(len(second['items']),1)
        self.assertIsNone(second['nextCursor'])
        self.assertEqual(len({item['id'] for item in first['items']+second['items']}),51)
        self.assertTrue(all(item['officerDelivery']=={'state':'retained_local','requestId':None} for item in first['items']+second['items']))

    def test_missing_source_index_cannot_hide_orphan_execution(self):
        self.f.add();ex=self.f.prepare()
        self.store.db.execute('DELETE FROM officer_document_dependencies WHERE request_id=?',(ex['id'],))
        with self.assertRaises(AttachmentError):self.one()

    def test_exact_current_request_must_be_same_context_and_real_public_intent(self):
        public,record,message=self.admission();ex=self.execution(record,message)
        with self.assertRaises(AttachmentError):self.one(request_id=ex['id'])
        with self.assertRaises(AttachmentError):self.one(request_id='a'*32)
        self.assertEqual(self.one(request_id=public['requestId'])['officerDelivery']['requestId'],public['requestId'])

    def test_in_flight_pre_execution_is_unknown_not_no_call(self):
        public,record,message=self.admission()
        item=self.one();self.assertEqual(self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)['officerState'],'unknown')
        self.assertEqual(item['officerDelivery']['state'],'unknown')
        self.finish(record);self.assertEqual(self.one()['officerDelivery']['state'],'retained_local')

    def test_image_citation_is_whole_image_metadata_only(self):
        public,record,message=self.admission();image=self.f.add(PNG,'image/png')
        ex=self.execution(record,message);self.f.complete(ex,text='IMAGE_SOURCE: image-1',citation=False)
        cap=self.p.citation_authority(public['requestId'],public['requestCapability'],ex['id'],image,{'image':True})
        out=self.p.preview(image,cap,{'image':True},lambda x:x)
        self.assertEqual(out['mediaType'],'image/png');self.assertFalse(out['available'])
        self.assertEqual(out['officerState'],'cited_whole');self.assertIsNone(out['officerPriorDelivery'])
        self.assertNotIn('pixels',out)

    def test_preview_publication_holds_store_gate_against_revocation(self):
        import threading
        public,record,message=self.admission();self.finish(record);item=self.one()
        started=threading.Event();done=threading.Event();threads=[]
        def publish(value):
            def revoke():
                started.set();self.f.f.revoke(item['id']);done.set()
            thread=threading.Thread(target=revoke);threads.append(thread);thread.start()
            self.assertTrue(started.wait(1));self.assertFalse(done.wait(.02))
            return value
        self.p.preview(item['id'],item['sourceCapability'],None,publish)
        for thread in threads:thread.join(2)
        self.assertTrue(done.is_set())
        with self.assertRaises(AttachmentError):self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)

    def test_projection_is_read_only_and_refuses_overbound_context(self):
        _,record,_=self.admission();self.finish(record)
        before=self.store.db.total_changes
        item=self.one();self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
        self.assertEqual(self.store.db.total_changes,before)
        self.i.prepare_question(self.f.f.binding,'Unsent question',[],self.f.f.direct_profile(),self.f.f.key())
        with mock.patch.dict(self.store.limits,requests=1):
            with self.assertRaises(AttachmentError):self.one()

    def test_dependency_fanout_overbound_refuses_without_truncation(self):
        _,record,message=self.admission();self.execution(record,message)
        _,later=self.question();self.execution(later)
        with mock.patch('officer_document_projection.MAX_DEPENDENTS',1):
            with self.assertRaises(AttachmentError):self.one()

    def test_another_confirmed_attempt_without_causal_order_is_prior_unknown(self):
        public,record,message=self.admission();ex=self.execution(record,message);self.f.complete(ex)
        later,current=self.question();self.execution(current);self.finish(current)
        item=self.one(request_id=later['requestId'])
        self.assertEqual(item['officerDelivery'],{'state':'confirmed','requestId':public['requestId']})
        preview=self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
        self.assertEqual(preview['officerState'],'retained_local')
        self.assertEqual(preview['officerPriorDelivery'],'unknown')
        self.assertNotIn('earlier question',preview['payload']['noticeParts']['body'])

    def test_two_file_current_draft_pre_execution_and_terminal_retention(self):
        earlier,first,message_a=self.admission();self.finish(first)
        current,second,message_b=self.admission()
        active=self.p.history(self.selection,request_id=current['requestId'])
        self.assertEqual({item['id'] for item in active['items']},
            {message_a['files'][0]['id'],message_b['files'][0]['id']})
        for item in active['items']:
            self.assertEqual(item['officerDelivery'],{'state':'unknown','requestId':current['requestId']})
            view=self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
            self.assertEqual(view['officerState'],'unknown');self.assertIsNone(view['officerPriorDelivery'])
        self.finish(second)
        ended=self.p.history(self.selection,request_id=current['requestId'])
        self.assertEqual(len(ended['items']),2)
        for item in ended['items']:
            self.assertEqual(item['officerDelivery'],{'state':'retained_local','requestId':current['requestId']})
            view=self.p.preview(item['id'],item['sourceCapability'],None,lambda x:x)
            self.assertEqual(view['officerState'],'retained_local');self.assertEqual(view['officerPriorDelivery'],'none')

    def test_unadmitted_current_draft_cannot_borrow_retained_sources(self):
        _,first,_=self.admission();self.finish(first)
        draft,_,current=self.t.prepared()
        with self.assertRaises(AttachmentError):self.p.history(self.selection,request_id=current['requestId'])
        record,winner=self.t.consume(draft,current)
        self.winners[record['id']]=winner
        with self.assertRaises(AttachmentError):self.p.history(self.selection,request_id=current['requestId'])

    def test_existing_execution_cannot_borrow_new_source_outside_frozen_set(self):
        current,record,message=self.admission();self.execution(record,message)
        _,later,_=self.admission();self.finish(later)
        with self.assertRaises(AttachmentError):self.p.history(self.selection,request_id=current['requestId'])

    def test_new_draft_retention_preserves_earlier_file_confirmation(self):
        prior,first,message_a=self.admission();self.f.complete(self.execution(first,message_a));self.finish(first)
        current,second,message_b=self.admission();self.finish(second)
        items={item['id']:item for item in self.p.history(self.selection,request_id=current['requestId'])['items']}
        old=items[message_a['files'][0]['id']]
        self.assertEqual(old['officerDelivery'],{'state':'confirmed','requestId':prior['requestId']})
        old_view=self.p.preview(old['id'],old['sourceCapability'],None,lambda x:x)
        self.assertEqual(old_view['officerState'],'retained_local');self.assertEqual(old_view['officerPriorDelivery'],'unknown')
        new=items[message_b['files'][0]['id']]
        new_view=self.p.preview(new['id'],new['sourceCapability'],None,lambda x:x)
        self.assertEqual(new_view['officerState'],'retained_local');self.assertEqual(new_view['officerPriorDelivery'],'none')


    def test_origin_does_not_follow_a_later_confirming_supply_witness(self):
        public,record,message=self.admission();self.finish(record)
        later,current=self.question();execution=self.execution(current);self.f.complete(execution);self.finish(current)
        item=self.one()
        self.assertEqual(item['admissionRequestId'],public['requestId'])
        self.assertEqual(item['officerDelivery']['requestId'],later['requestId'])
        self.assertNotEqual(item['admissionRequestId'],item['officerDelivery']['requestId'])

    def test_origin_refuses_foreign_context_and_corrupt_local_receipt(self):
        public,record,_=self.admission();self.finish(record)
        with self.assertRaises(AttachmentError):self.p.history(replace(self.selection,scope_id='foreign'))
        key=self.i.command(record,'local')
        self.store.db.execute("UPDATE seen SET fingerprint=? WHERE key=?",('0'*64,key))
        with self.assertRaises(AttachmentError):self.one()

    def test_prepared_draft_cannot_claim_legacy_retained_source_as_its_origin(self):
        legacy=self.f.add();self.t.prepared()
        item=self.one()
        self.assertEqual(item['id'],legacy)
        self.assertIsNone(item['admissionRequestId'])


    def test_origin_requires_unique_original_frozen_membership_even_with_a_matching_admission(self):
        public,record,message=self.admission();self.finish(record)
        original=self.p._context(self.selection)
        for change in ('missing','identity','duplicate_origin'):
            altered=json.loads(json.dumps(record))
            if change=='missing':altered['sources']=[]
            elif change=='identity':altered['sources'][0]['identity']='[]'
            records=dict(original[0]);records[record['id']]=(altered,original[0][record['id']][1])
            if change=='duplicate_origin':records['b'*32]=(record,original[0][record['id']][1])
            context=(records,original[1],{})
            with mock.patch.object(self.p,'_context',return_value=context),mock.patch.object(self.p,'_correlated',return_value=(dict(self.i.conversations._message(message['id'])),None)):
                with self.assertRaises(AttachmentError):self.one()


if __name__=='__main__':unittest.main()
