"""Actual owner service, ledger and HTTP boundaries; fake provider connections only."""
from dataclasses import replace
import copy
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import officer_documents as documents
import officer_document_policy as policy
from officer_document_coordinator import DocumentCoordinator, _presentation
from officer_document_service import OfficerAttachmentService
from officer_document_transport import DirectDocumentTransport
from attachments_http import handle_http
from attachments_types import AttachmentError
from test_attachments_foundation import fence,metadata
from test_attachments_http import Handler,KEY
import test_attachments_officer_storage as storage_fixtures
PDF,PNG=storage_fixtures.PDF,storage_fixtures.PNG
from test_officer_document_transport import FakeConnection,FakeResponse
from test_officer_documents import wire,response


class Provider:
    def __init__(self):self.connections=[];self.next=[]
    def __call__(self,*args,**kwargs):
        result=self.next.pop(0) if self.next else FakeResponse(wire(response('Fictional answer.',citations=True)))
        conn=FakeConnection(result);self.connections.append(conn);return conn
    @property
    def calls(self):return sum(len(c.requests) for c in self.connections)


@unittest.skipUnless(os.name=='posix','Private storage requires POSIX.')
class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.f=storage_fixtures.DirectStoreTests();self.f.setUp();self.store=self.f.store
        self.f.clock[0]=policy.VALID_FROM+60
        self.f.binding.update(modelId=documents.MODEL,capabilityRevision=policy.PROFILE_REVISION)
        self.provider=Provider();self.model=[documents.MODEL]
        self.transport=DirectDocumentTransport(enabled=True,connect=self.provider,wall=lambda:self.f.clock[0])
        self.c=DocumentCoordinator(self.store,policy.HostAllowance(policy.EVIDENCE_ID,'fictional-shared','v1','10'),
            selected_model=lambda:self.model[0],credential=lambda:'fictional-only-key',transport=self.transport,enabled=True)
        self.scopes={'fictional-room'}
        self.service=OfficerAttachmentService(self.store,coordinator=self.c,known_scopes=lambda:self.scopes)
    def tearDown(self):self.service.close();self.f.temp.cleanup()
    def read_row(self, sql, args=()):
        # The deletion worker uses this same connection. Fetch the row while
        # the gate still holds, since fetchone can also step the statement.
        with self.store._gate():
            return self.store.db.execute(sql, args).fetchone()

    def write_sql(self, sql, args=()):
        with self.store._gate():
            self.store.db.execute(sql, args)

    @property
    def selection(self):
        b=self.f.binding
        return dict(destinationId=b['id'],scopeId=b['scopeId'],conversationId=b['conversationId'],modelId=b['modelId'],capabilityRevision=b['capabilityRevision'])
    def draft(self,text='Read the fictional document.',raw=PDF,media='application/pdf'):
        d=self.service.create_draft(self.selection,self.f.key())
        u=self.service.mutate('reserve',d['id'],d['capability'],dict(fence(d),**dict(metadata(raw),name='fictional-source',mediaType=media)),command_key=self.f.key())
        d['revision']=u['draftRevision']
        done=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(raw));d['revision']=done['draftRevision']
        if text:d.update(self.service.mutate('edit',d['id'],d['capability'],dict(fence(d),text=text)))
        return d,u['fileId']
    def prepare(self,d,kept=()):
        return self.service.document_prepare(d['id'],d['capability'],dict(fence(d),keptFileIds=list(kept)),self.f.key())
    def send(self,d,r):return self.service.document_send(r['requestId'],r['requestCapability'],d['capability'],dict(fence(d),fingerprint=r['fingerprint']),self.f.key())
    def publish(self,r,out):return self.service.document_answer(out['answerId'],r['requestId'],r['requestCapability'],lambda value:value)
    def http(self,method,route,data=None,headers=(),defaults=True):
        body=json.dumps(data).encode() if data is not None else b''
        hdr=list(headers)
        if data is not None:hdr += [('Content-Length',str(len(body))),('Content-Type','application/json')]
        h=Handler(method,'/api/attachments'+route,body,hdr,defaults)
        handle_http(h,self.service,KEY,8786)
        return h.response

    def test_pdf_roundtrip_duplicate_send_and_private_public_projection(self):
        d,fid=self.draft();r=self.prepare(d)
        self.assertEqual(self.provider.calls,0);self.assertEqual(r['state'],'prepared');self.assertIsNone(r['answerId'])
        out=self.send(d,r);self.assertEqual(out['state'],'answered',out)
        answer=self.publish(r,out);self.assertIn('Fictional answer',answer['text']);self.assertEqual(answer['actions'],[])
        self.assertEqual(answer['citations'][0]['fileId'],fid)
        self.assertNotIn('claimNonce',out);self.assertNotIn('reservedUnits',out)
        self.assertEqual(self.send(d,r),out);self.assertEqual(self.provider.calls,1)
        body=json.loads(self.provider.connections[0].requests[0][2])
        self.assertEqual(body['model'],documents.MODEL);self.assertNotIn('tools',body)
        self.assertFalse(self.store.files.io_active())
        record=self.read_row('SELECT record,receipt FROM officer_document_intents')
        self.assertNotIn(answer['text'],record['record']);self.assertNotIn(answer['text'].encode(),record['receipt'])

    def test_png_whole_image_and_no_local_render(self):
        self.provider.next.append(FakeResponse(wire(response('Fictional picture.\nIMAGE_SOURCE: image-1',citations=False))))
        d,fid=self.draft(raw=PNG,media='image/png');r=self.prepare(d);out=self.send(d,r)
        self.assertEqual(out['state'],'answered',out)
        answer=self.publish(r,out);self.assertEqual(answer['citations'][0]['location'],{'image':True})
        self.assertEqual(answer['citations'][0]['fileId'],fid)

    def test_empty_attachment_message_is_retained_without_call_then_followup_reads_it(self):
        d,fid=self.draft(text='');r=self.prepare(d);out=self.send(d,r)
        self.assertEqual(out['state'],'retained_local');self.assertIsNone(out['answerId']);self.assertEqual(self.provider.calls,0)
        kept=self.service.history('officer',self.f.binding['scopeId'],self.f.cid)['items'][0]
        self.assertEqual(kept['admissionRequestId'],r['requestId'])
        key=self.f.key();data=dict(self.selection,question='Read that retained file.',keptFileIds=[fid],priorAnswerIds=[])
        ask=self.service.document_ask(self.f.cid,data,key)
        self.assertEqual(ask['state'],'answered',ask);self.assertEqual(self.provider.calls,1)
        supplied=self.service.history('officer',self.f.binding['scopeId'],self.f.cid)['items'][0]
        self.assertEqual(supplied['admissionRequestId'],r['requestId']);self.assertEqual(supplied['officerDelivery']['requestId'],ask['requestId'])
        self.assertEqual(self.service.document_ask(self.f.cid,data,key)['requestId'],ask['requestId'])
        self.assertEqual(self.provider.calls,1)
        self.assertIsNone(self.read_row('SELECT expires_at FROM files WHERE id=?',(fid,))[0])

    def test_followup_replays_governed_answer_and_all_uncited_sources(self):
        d,a=self.draft();r=self.prepare(d);one=self.send(d,r)
        d2,b=self.draft(text='',raw=PNG,media='image/png');r2=self.prepare(d2);self.send(d2,r2)
        two=self.service.document_ask(self.f.cid,dict(self.selection,question='Compare with earlier answer.',keptFileIds=[a,b],priorAnswerIds=[one['answerId']]),self.f.key())
        self.assertEqual(two['state'],'answered',two)
        published=self.service.document_answer(two['answerId'],two['requestId'],two['requestCapability'],lambda v:v)
        self.assertEqual(set(published['suppliedFileIds']),{a,b});self.assertEqual({c['fileId'] for c in published['citations']},{a})
        body=json.loads(self.provider.connections[-1].requests[0][2]);blocks=body['messages'][0]['content']
        self.assertEqual([x['type'] for x in blocks].count('document'),1);self.assertEqual([x['type'] for x in blocks].count('image'),1)
        self.assertIn('Fictional answer.',json.dumps(body))
        self.f.revoke(b)
        unavailable=self.service.document_answer(two['answerId'],two['requestId'],two['requestCapability'],lambda v:v)
        self.assertIsNone(unavailable['text']);self.assertEqual(unavailable['citations'],[]);self.assertEqual(unavailable['suppliedFileIds'],[])
        self.assertEqual(self.provider.calls,2)

    def test_recent_prior_window_matches_recorded_order_and_freezes_disclosure_before_replay(self):
        draft,source=self.draft(text='');self.send(draft,self.prepare(draft))
        base=self.f.clock[0];recorded=[]
        for i in range(8):
            self.f.clock[0]=base+[2,3,1,1,4,4,5,5][i]
            out=self.service.document_ask(self.f.cid,dict(self.selection,question='Earlier answer '+str(i),keptFileIds=[source],priorAnswerIds=[]),self.f.key())
            self.assertEqual(out['state'],'answered');recorded.append((self.f.clock[0],out['requestId'],out['answerId']))
        self.f.clock[0]=base+6
        ids=[aid for _,_,aid in sorted(recorded)]
        ask=dict(self.selection,question='Follow up from an exact window.',keptFileIds=[source],priorAnswerIds=ids,priorAnswerPolicy='recent_available_8')
        key=self.f.key()
        check_window=self.c.intents._recent_answer_window
        def outside_transaction(*args):
            self.assertFalse(self.store.db.in_transaction)
            return check_window(*args)
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('preparation reads metadata only')), \
                mock.patch.object(self.c.intents,'_recent_answer_window',side_effect=outside_transaction):
            eight=self.service.document_ask(self.f.cid,ask,key,prepare_only=True)
        self.assertEqual(self.provider.calls,8)
        record,_=self.c.intents.authorize(eight['requestId'],eight['requestCapability'])
        self.assertEqual(record['prior_answer_window'],{'policy':'recent_available_8','includedCount':8,'omittedOlder':False})
        self.f.clock[0]=base+7
        ninth=self.service.document_ask(self.f.cid,dict(self.selection,question='A newer answer.',keptFileIds=[source],priorAnswerIds=[]),self.f.key())
        self.assertEqual(ninth['state'],'answered')
        self.assertEqual(self.service.document_ask(self.f.cid,ask,key,prepare_only=True),eight)
        current_ids=ids[1:]+[ninth['answerId']]
        for altered in (ids,ids[:-1],current_ids[::-1],current_ids[:-1]+['f'*32]):
            before=self.read_row('SELECT COUNT(*) FROM officer_document_intents')[0]
            with self.assertRaises(AttachmentError) as caught:
                self.service.document_ask(self.f.cid,dict(ask,priorAnswerIds=altered),self.f.key(),prepare_only=True)
            self.assertEqual(caught.exception.code,'document_prior_window_changed');self.assertEqual(self.read_row('SELECT COUNT(*) FROM officer_document_intents')[0],before)
        with self.assertRaises(AttachmentError):
            self.service.document_ask(self.f.cid,dict(ask,priorAnswerIds=current_ids),key,prepare_only=True)
        omitted_slot=self.read_row('SELECT answer FROM officer_document_requests WHERE id=?',(ids[0],))[0]
        self.write_sql('UPDATE officer_document_requests SET answer=? WHERE id=?',(omitted_slot[:-1]+b'X',ids[0]))
        with self.assertRaises(AttachmentError) as caught:
            self.service.document_ask(self.f.cid,dict(ask,priorAnswerIds=current_ids),self.f.key(),prepare_only=True)
        self.assertEqual(caught.exception.code,'document_record_invalid')
        self.write_sql('UPDATE officer_document_requests SET answer=? WHERE id=?',(omitted_slot,ids[0]))
        fresh=self.service.document_ask(self.f.cid,dict(ask,priorAnswerIds=current_ids),self.f.key(),prepare_only=True)
        self.assertEqual(self.provider.calls,9)
        self.f.clock[0]=base+8.125
        answer=self.service.document_send_question(fresh['requestId'],fresh['requestCapability'],{'fingerprint':fresh['fingerprint']},self.f.key())
        self.assertEqual(answer['state'],'answered');self.assertEqual(self.provider.calls,10)
        shown=self.publish(fresh,answer)
        self.assertEqual(shown['priorAnswerWindow'],{'policy':'recent_available_8','includedCount':8,'omittedOlder':True})
        self.assertEqual(shown['answeredAt'],base+8.125)
        with self.store._gate():
            _,frozen,_,_=self.c.ledger._request(self.f.selection,answer['answerId'])
        self.assertEqual(frozen['manifest']['selection']['prior_answer_ids'],current_ids)
        self.assertNotIn('priorAnswerPolicy',json.loads(self.provider.connections[-1].requests[0][2]))
        self.f.clock[0]=base+9;self.assertEqual(self.publish(fresh,answer),shown)
        self.f.revoke(source);withdrawn=self.publish(fresh,answer)
        self.assertEqual(withdrawn['state'],'source_unavailable');self.assertIsNone(withdrawn['priorAnswerWindow']);self.assertEqual(withdrawn['answeredAt'],base+8.125)

    def test_recent_policy_split_boundary_and_physical_read_refusal(self):
        draft,source=self.draft(text='');self.send(draft,self.prepare(draft))
        route='/conversations/'+self.f.cid
        data=dict(self.selection,question='Read the exact retained file.',keptFileIds=[source],priorAnswerIds=[],priorAnswerPolicy='recent_available_8')
        for suffix,body in (('/ask',data),('/question-intent',dict(data,priorAnswerPolicy=None)),
                            ('/question-intent',dict(data,priorAnswerPolicy='other')),('/question-intent',dict(data,omittedOlder=True))):
            result=self.http('POST',route+suffix,body,[('Idempotency-Key',self.f.key())]);self.assertEqual(result[0],400)
        result=self.http('POST',route+'/question-intent',dict(data,priorAnswerIds=['f'*32]*2),[('Idempotency-Key',self.f.key())])
        self.assertEqual(result[0],409)
        created=self.service.document_ask(self.f.cid,data,self.f.key(),prepare_only=True)
        path=self.store.files.root/'objects'/source;raw=path.read_bytes();path.write_bytes(raw[:-1]+b'X')
        out=self.service.document_send_question(created['requestId'],created['requestCapability'],{'fingerprint':created['fingerprint']},self.f.key())
        self.assertEqual(out['state'],'failed');self.assertIsNone(out['answerId']);self.assertEqual(self.provider.calls,0)
        self.f.revoke(source)
        refused=self.service.document_ask(self.f.cid,data,self.f.key(),prepare_only=True)
        failed=self.service.document_send_question(refused['requestId'],refused['requestCapability'],
            {'fingerprint':refused['fingerprint']},self.f.key())
        self.assertEqual(failed['state'],'failed')
        self.assertEqual(failed['officerDelivery']['state'],'not_sent')
        self.assertEqual(self.provider.calls,0)

    def test_mixed_tray_sends_only_selected_originals_and_records_them(self):
        unrelated,excluded=self.draft(text='',raw=b'%PDF-1.4\nFictional unrelated bytes\n%%EOF')
        self.send(unrelated,self.prepare(unrelated))
        stored,kept=self.draft(text='')
        self.send(stored,self.prepare(stored))
        draft,new=self.draft(raw=PNG,media='image/png')
        intent=self.prepare(draft,kept=[kept])
        result=self.send(draft,intent)
        self.assertEqual(result['state'],'answered',result)
        self.assertEqual(result['questionFileIds'],[kept,new])
        self.assertEqual(set(self.publish(intent,result)['suppliedFileIds']),{kept,new})
        self.assertEqual(self.provider.calls,1)
        from base64 import b64decode
        body=json.loads(self.provider.connections[-1].requests[0][2])
        originals={b64decode(block['source']['data']) for block in body['messages'][0]['content']
                   if block['type'] in ('document','image')}
        self.assertEqual(originals,{PDF,PNG})
        files=self.service.history('officer',self.f.binding['scopeId'],self.f.cid)['items']
        self.assertEqual(next(f for f in files if f['id']==excluded)['officerDelivery']['state'],'retained_local')

    def test_tray_limit_and_deleted_source_refuse_before_paid_call(self):
        for raw in (b'%PDF-1.4\n'+b'a'*160000, b'%PDF-1.4\n'+b'b'*160000):
            d,fid=self.draft(text='',raw=raw)
            self.send(d,self.prepare(d))
        kept=[file['id'] for file in self.service.history('officer',self.f.binding['scopeId'],self.f.cid)['items']]
        body=dict(self.selection,question='Fictional tray exceeds its byte allowance.',keptFileIds=kept,priorAnswerIds=[])
        too_large=self.service.document_ask(self.f.cid,body,self.f.key())
        self.assertEqual((too_large['state'],too_large['officerDelivery']['state']),('failed','not_sent'))
        draft,_=self.draft()
        request=self.prepare(draft,kept=[kept[0]])
        self.f.revoke(kept[0])
        missing=self.send(draft,request)
        self.assertEqual((missing['state'],missing['officerDelivery']['state']),('failed','not_sent'))
        self.assertEqual(self.provider.calls,0)
        self.assertEqual(self.read_row('SELECT COUNT(*) FROM officer_document_requests')[0],0)

    def test_delete_after_dial_settles_without_post_or_budget_charge(self):
        from threading import Event, Thread
        kept,source=self.draft(text='')
        self.send(kept,self.prepare(kept))
        gate=Event()
        connect=FakeConnection(connect_gate=gate)
        self.transport.connect=lambda *args,**kwargs:connect
        selection=dict(self.selection,question='Read this exact fictional file.',keptFileIds=[source],priorAnswerIds=[])
        outcomes=[]
        worker=Thread(target=lambda:outcomes.append(self.service.document_ask(self.f.cid,selection,self.f.key())))
        worker.start()
        try:
            self.assertTrue(connect.connect_entered.wait(2))
            self.f.revoke(source)
        finally:
            gate.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes),1)
        reply=outcomes[0]
        self.assertEqual((reply['state'],reply['failure'],reply['officerDelivery']['state']),
                         ('failed','tray_unavailable','not_sent'))
        self.assertEqual(connect.requests,[])
        selection=self.f.selection
        record,_=self.c.intents._record(reply['requestId'])
        _,execution=self.c.intents.correlated(record)
        self.assertEqual(execution['chargedUnits'],0)
        self.assertTrue(execution['settled'])

    def test_removing_unselected_sibling_after_dial_releases_reservation(self):
        from threading import Event, Thread
        d,selected=self.draft(text='')
        u=self.service.mutate('reserve',d['id'],d['capability'],dict(fence(d),**dict(metadata(PDF),name='unselected.pdf',mediaType='application/pdf')),command_key=self.f.key())
        d['revision']=u['draftRevision']
        d['revision']=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(PDF))['draftRevision']
        self.send(d,self.prepare(d))
        gate=Event()
        connect=FakeConnection(connect_gate=gate)
        self.transport.connect=lambda *args,**kwargs:connect
        body=dict(self.selection,question='Read only the selected fictional file.',keptFileIds=[selected],priorAnswerIds=[])
        outcomes=[]
        worker=Thread(target=lambda:outcomes.append(self.service.document_ask(self.f.cid,body,self.f.key())))
        worker.start()
        try:
            self.assertTrue(connect.connect_entered.wait(2))
            self.f.revoke(u['fileId'])
        finally:
            gate.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        reply,=outcomes
        self.assertEqual((reply['state'],reply['failure'],reply['officerDelivery']['state']),
                         ('failed','tray_unavailable','not_sent'))
        self.assertEqual(connect.requests,[])
        _,receipt=self.c.intents.correlated(self.c.intents._record(reply['requestId'])[0])
        self.assertEqual(receipt['chargedUnits'],0)
        self.assertTrue(receipt['settled'])

    def test_delete_between_claim_and_consume_releases_reservation(self):
        d,source=self.draft(text='')
        self.send(d,self.prepare(d))
        consume=self.c.ledger.consume
        def delete_then_consume(*args):
            self.f.revoke(source)
            return consume(*args)
        self.c.ledger.consume=delete_then_consume
        body=dict(self.selection,question='Read this exact file.',keptFileIds=[source],priorAnswerIds=[])
        reply=self.service.document_ask(self.f.cid,body,self.f.key())
        self.assertEqual((reply['state'],reply['failure'],reply['officerDelivery']['state']),
                         ('failed','tray_unavailable','not_sent'))
        self.assertEqual(self.provider.connections,[])
        _,receipt=self.c.intents.correlated(self.c.intents._record(reply['requestId'])[0])
        self.assertEqual(receipt['chargedUnits'],0)
        self.assertTrue(receipt['settled'])

    def test_deadline_while_validating_final_dispatch_prevents_post(self):
        d,source=self.draft(text='')
        self.send(d,self.prepare(d))
        clock=[1000.]
        self.c.monotonic=self.transport.clock=lambda:clock[0]
        ready=self.c.ledger.dispatch_ready
        def validate(*args):
            answer=ready(*args)
            clock[0]+=121
            return answer
        self.c.ledger.dispatch_ready=validate
        result=self.service.document_ask(self.f.cid,dict(self.selection,question='Read this exact file.',
            keptFileIds=[source],priorAnswerIds=[]),self.f.key())
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(len(self.provider.connections),1)
        self.assertEqual(self.provider.calls,0)

    def test_same_reader_reopens_and_manages_tray_records_with_provider_refused(self):
        d,source=self.draft(text='')
        self.send(d,self.prepare(d))
        body=dict(self.selection,question='Read this exact fictional file.',keptFileIds=[source],priorAnswerIds=[])
        answer=self.service.document_ask(self.f.cid,body,self.f.key())
        self.assertEqual(answer['state'],'answered')
        self.assertEqual(self.provider.calls,1)
        self.service.close()
        self.f.store=self.store=self.f.open()
        disabled=DirectDocumentTransport(enabled=True,connect=self.provider,wall=lambda:self.f.clock[0])
        self.c=DocumentCoordinator(self.store,policy.HostAllowance(policy.EVIDENCE_ID,'fictional-shared','v1','10'),
            selected_model=lambda:None,credential=lambda:'fictional-only-key',transport=disabled,enabled=True)
        self.service=OfficerAttachmentService(self.store,coordinator=self.c,known_scopes=lambda:self.scopes)
        scope=self.f.binding['scopeId']
        self.assertFalse(self.service.capabilities('officer',scope,documents.MODEL)['available'])
        history=self.service.document_history(self.f.cid,'officer',scope)['items']
        row=next(x for x in history if x['requestId']==answer['requestId'])
        self.assertEqual(row['questionFileIds'],[source])
        self.f.error('document_model_unavailable',lambda:self.service.document_ask(self.f.cid,body,self.f.key()))
        result=self.service.deletion_prepare(source,dict(destinationId='officer',scopeId=scope,
            conversationId=self.f.cid,managementGeneration=0),self.f.key())
        deletion=self.service.deletion_confirm(result['id'],result['capability'],
            {'fingerprint':result['fingerprint']},self.f.key())
        self.assertTrue(deletion['sourceUnavailable'])
        self.assertEqual(self.provider.calls,1)

    def test_oversized_and_duplicate_tray_cannot_create_an_intent(self):
        draft,source=self.draft()
        count=self.read_row('SELECT COUNT(*) FROM officer_document_intents')[0]
        for kept in ([source],[str(n)*32 for n in range(1,6)]):
            with self.assertRaises(AttachmentError) as caught:
                self.prepare(draft,kept=kept)
            self.assertEqual(caught.exception.code,'document_tray_invalid')
        self.assertEqual(self.read_row('SELECT COUNT(*) FROM officer_document_intents')[0],count)

    def test_historical_untimed_answer_stays_null_before_and_after_revocation(self):
        from officer_document_ledger import pack, unpack
        draft,source=self.draft();request=self.prepare(draft);out=self.send(draft,request)
        blob=self.read_row('SELECT receipt FROM officer_document_requests WHERE id=?',(out['answerId'],))[0]
        legacy=unpack(blob,4096);del legacy['answer_committed_ms']
        self.write_sql('UPDATE officer_document_requests SET receipt=? WHERE id=?',(pack(legacy,4096),out['answerId']))
        shown=self.publish(request,out);self.assertEqual(shown['state'],'available');self.assertIsNone(shown['answeredAt']);self.assertIsNone(shown['priorAnswerWindow'])
        self.f.revoke(source)
        withdrawn=self.publish(request,out);self.assertEqual(withdrawn['state'],'source_unavailable');self.assertIsNone(withdrawn['answeredAt']);self.assertIsNone(withdrawn['priorAnswerWindow'])

    def test_recent_window_does_not_relax_encoded_request_limit(self):
        raw=b'%PDF-1.4\n'+b'X'*299970+b'\n%%EOF'
        draft,source=self.draft(text='',raw=raw);self.send(draft,self.prepare(draft))
        prior_ids=[]
        for index in range(8):
            self.f.clock[0]+=1
            self.provider.next.append(FakeResponse(wire(response('Fictional '+('p'*19000),citations=False))))
            out=self.service.document_ask(self.f.cid,dict(self.selection,question='Large earlier answer '+str(index),keptFileIds=[source],priorAnswerIds=[]),self.f.key())
            self.assertEqual(out['state'],'answered');prior_ids.append(out['answerId'])
        data=dict(self.selection,question='Read all of that context.',keptFileIds=[source],priorAnswerIds=prior_ids,priorAnswerPolicy='recent_available_8')
        prepared=self.service.document_ask(self.f.cid,data,self.f.key(),prepare_only=True)
        self.assertEqual(prepared['state'],'prepared')
        out=self.service.document_send_question(prepared['requestId'],prepared['requestCapability'],{'fingerprint':prepared['fingerprint']},self.f.key())
        self.assertEqual(out['state'],'failed');self.assertIsNone(out['answerId']);self.assertEqual(self.provider.calls,8)
        _,receipt=self.c.intents.authorize(prepared['requestId'],prepared['requestCapability'])
        self.assertEqual(receipt['error'],'context_too_large')

    def test_provider_rejection_is_not_no_call_and_duplicate_never_retries(self):
        self.provider.next.append(FakeResponse(b'{"secret":"not retained"}',status=400))
        d,_=self.draft();r=self.prepare(d);out=self.send(d,r)
        self.assertEqual(out['state'],'failed',out);self.assertEqual(out['failure'],'provider_rejected')
        self.assertEqual(out['officerDelivery']['state'],'unknown');self.assertIsNone(out['answerId'])
        self.f.revoke(self.c.intents.authorize(r['requestId'],r['requestCapability'])[0]['sources'][0]['id'])
        self.assertEqual(self.service.document_status(r['requestId'],r['requestCapability']),out)
        self.send(d,r);self.assertEqual(self.provider.calls,1)
        self.assertNotIn('not retained',str(self.read_row('SELECT record,receipt,answer FROM officer_document_requests')))

    def test_invalid_response_is_unknown_and_raw_never_published(self):
        self.provider.next.append(FakeResponse(b'{"untrusted":"raw provider payload"}'))
        d,_=self.draft();r=self.prepare(d);out=self.send(d,r)
        self.assertEqual(out['state'],'unknown');self.assertIsNone(out['answerId']);self.assertEqual(self.provider.calls,1)
        self.f.revoke(self.c.intents.authorize(r['requestId'],r['requestCapability'])[0]['sources'][0]['id'])
        self.assertEqual(self.service.document_status(r['requestId'],r['requestCapability']),out)
        self.assertFalse(self.store.files.io_active())

    def test_model_change_expiry_stale_fence_refuse_without_call(self):
        d,_=self.draft();r=self.prepare(d);self.model[0]='unqualified-model'
        with self.assertRaises(AttachmentError):self.send(d,r)
        self.model[0]=documents.MODEL;self.f.clock[0]=policy.VALID_UNTIL
        with self.assertRaises(AttachmentError):self.send(d,r)
        self.f.clock[0]=policy.VALID_FROM+60
        self.store.edit_text(d['id'],d['capability'],fence(d),'Changed question')
        with self.assertRaises(AttachmentError):self.send(d,r)
        self.assertEqual(self.provider.calls,0)
        self.assertEqual(self.read_row('SELECT COUNT(*) FROM officer_document_requests')[0],0)

    def test_unknown_new_scope_refuses_existing_offline_conversation_can_ask(self):
        d,source=self.draft(text='');r=self.prepare(d);self.send(d,r);self.scopes.clear()
        with self.assertRaises(AttachmentError):self.service.create_conversation({'scopeId':'fictional-room'},self.f.key())
        out=self.service.document_ask(self.f.cid,dict(self.selection,question='Read retained file offline.',keptFileIds=[source],priorAnswerIds=[]),self.f.key())
        self.assertEqual(out['state'],'answered',out)

    def test_unsupported_format_and_closed_payload_are_zero_call(self):
        with self.assertRaises(AttachmentError):self.draft(raw=b'fictional text',media='text/plain')
        with self.assertRaises(AttachmentError):self.draft(raw=b'\xff\xd8\xfffictional',media='image/jpeg')
        d,_=self.draft();r=self.prepare(d)
        bad=self.http('POST','/requests/'+r['requestId']+'/send',dict(fence(d),fingerprint=r['fingerprint'],endpoint='https://invalid.example'),
            [('X-Attachment-Request-Capability',r['requestCapability']),('X-Attachment-Draft-Capability',d['capability']),('Idempotency-Key',self.f.key())])
        self.assertEqual(bad[0],400);self.assertEqual(self.provider.calls,0)

    def test_auth_scope_capability_and_recovery_never_send(self):
        d,_=self.draft();r=self.prepare(d)
        self.assertEqual(self.http('GET','/requests/'+r['requestId'],defaults=False)[0],401)
        self.assertEqual(self.http('GET','/requests/'+r['requestId'],headers=[('X-Attachment-Request-Capability','bad')])[0],403)
        recovered=self.http('POST','/requests/'+r['requestId']+'/recover',dict(self.selection,fingerprint=r['fingerprint']))
        self.assertEqual(recovered[0],200,recovered)
        wrong=dict(self.selection,scopeId='fictional-other',fingerprint=r['fingerprint'])
        self.assertEqual(self.http('POST','/requests/'+r['requestId']+'/recover',wrong)[0],409)
        self.assertEqual(self.provider.calls,0)

    def test_actual_loopback_owner_send_and_guarded_answer(self):
        service=self.service
        class HTTP(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,status,body,content_type,extra=()):
                self.send_response(status);self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(body)))
                for k,v in extra:self.send_header(k,v)
                self.end_headers();self.wfile.write(body)
            def do_GET(self):handle_http(self,service,KEY,self.server.server_port)
            def do_POST(self):handle_http(self,service,KEY,self.server.server_port)
        httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),HTTP)
        thread=threading.Thread(target=httpd.serve_forever);thread.start()
        def call(method,path,data=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            try:
                body=json.dumps(data) if data is not None else None
                hdr={'X-Live-Room-Key':KEY,**(headers or {})}
                if body is not None:hdr['Content-Type']='application/json'
                conn.request(method,'/api/attachments'+path,body=body,headers=hdr)
                reply=conn.getresponse();return reply.status,json.loads(reply.read())
            finally:conn.close()
        try:
            d,_=self.draft();r=self.prepare(d)
            status,out=call('POST','/requests/'+r['requestId']+'/send',dict(fence(d),fingerprint=r['fingerprint']),
                {'X-Attachment-Request-Capability':r['requestCapability'],'X-Attachment-Draft-Capability':d['capability'],'Idempotency-Key':self.f.key()})
            self.assertEqual(status,200,out);self.assertEqual(out['state'],'answered',out)
            status,answer=call('GET','/answers/'+out['answerId']+'?requestId='+r['requestId'],headers={'X-Attachment-Request-Capability':r['requestCapability']})
            self.assertEqual(status,200,answer);self.assertEqual(answer['sourceValidation'],'current');self.assertEqual(self.provider.calls,1)
        finally:httpd.shutdown();httpd.server_close();thread.join()

    def test_invalid_result_traceback_is_gone_before_result_pin_release(self):
        import attachments_files
        for bad_commit in (False,True):
            with self.subTest(commit_failure=bad_commit):
                # A fresh shared allowance is unnecessary; a prior rejection halts
                # this instance, so exercise each boundary in an isolated fixture.
                case=CoordinatorTests();case.setUp()
                try:
                    if not bad_commit:case.provider.next.append(FakeResponse(b'{"bad":"fictional malformed response"}'))
                    d,_=case.draft();r=case.prepare(d);observed=[]
                    original=attachments_files._BufferResultLease.close
                    def checked(lease):
                        active_exception=sys.exc_info()[1] is not None
                        frame=sys._getframe().f_back
                        while frame and frame.f_code.co_name!='_execute':frame=frame.f_back
                        clean_locals=frame is not None and all(frame.f_locals.get(name) is None for name in ('raw','candidate','prepared'))
                        frame=None
                        # Observe, never interrupt the release with an assertion
                        # that the production failure handler could swallow.
                        observed.append((active_exception,clean_locals))
                        return original(lease)
                    context=mock.patch.object(case.c.ledger,'commit_answer',side_effect=RuntimeError('fictional commit failure')) if bad_commit else mock.patch.object(case.c.ledger,'commit_answer',wraps=case.c.ledger.commit_answer)
                    with context,mock.patch.object(attachments_files._BufferResultLease,'close',checked):
                        out=case.send(d,r)
                    self.assertTrue(observed);self.assertTrue(all(not active and clean for active,clean in observed),observed)
                    self.assertIsNone(out['answerId']);self.assertFalse(case.store.files.io_active())
                finally:case.tearDown()


    def test_display_projection_multiple_markers_preserves_canonical_slot_digest_and_prior_replay(self):
        first,a=self.draft(text='',raw=PNG,media='image/png');self.send(first,self.prepare(first))
        draft,b=self.draft(raw=PNG,media='image/png')
        raw_text='Look 🦉\r\nIMAGE_SOURCE: image-1\r\nOther line\nIMAGE_SOURCE: image-2'
        self.provider.next.append(FakeResponse(wire(response(raw_text,citations=False))))
        request=self.prepare(draft,kept=[a]);out=self.send(draft,request);answer_id=out['answerId']
        from attachments_catalogue import HistorySelection
        context=HistorySelection.from_binding(self.f.binding)
        canonical=self.c.ledger.replay(context,answer_id)
        frozen=json.loads(self.read_row('SELECT record FROM officer_document_requests WHERE id=?',(answer_id,))[0])
        before=tuple(self.read_row('SELECT record,receipt,answer FROM officer_document_requests WHERE id=?',(answer_id,)))
        for _ in range(3):
            shown=self.publish(request,out)
            self.assertEqual(shown['text'],'Look 🦉\r\n\r\nOther line\n')
            self.assertEqual(shown['suppliedFileIds'],frozen['manifest']['selection']['ordered_sources'])
            self.assertEqual(set(shown['suppliedFileIds']),{a,b})
            self.assertEqual(len(shown['citations']),2)
            self.assertTrue(all(c['location']=={'image':True} for c in shown['citations']))
            self.assertEqual(self.c.ledger.replay(context,answer_id),canonical)
            self.assertEqual(tuple(self.read_row('SELECT record,receipt,answer FROM officer_document_requests WHERE id=?',(answer_id,))),before)
        self.assertEqual(canonical['text'],raw_text)
        self.provider.next.append(FakeResponse(wire(response('A later plain answer mentions image-1.',citations=False))))
        child=self.service.document_ask(self.f.cid,dict(self.selection,question='Use the prior answer.',keptFileIds=[a,b],priorAnswerIds=[answer_id]),self.f.key())
        self.assertEqual(child['state'],'answered')
        body=json.loads(self.provider.connections[-1].requests[0][2])
        self.assertIn('IMAGE_SOURCE: image-1',body['messages'][0]['content'][-2]['text'])
        self.assertEqual(self.c.ledger.replay(context,answer_id),canonical)
        self.assertEqual(tuple(self.read_row('SELECT record,receipt,answer FROM officer_document_requests WHERE id=?',(answer_id,))),before)

    def test_marker_only_answer_has_empty_display_real_citation_then_deletion_withdraws_roster(self):
        self.provider.next.append(FakeResponse(wire(response('IMAGE_SOURCE: image-1',citations=False))))
        draft,source=self.draft(raw=PNG,media='image/png');request=self.prepare(draft);out=self.send(draft,request)
        shown=self.publish(request,out)
        self.assertEqual(shown['text'],'');self.assertEqual(shown['suppliedFileIds'],[source])
        self.assertEqual(shown['citations'][0]['fileId'],source);self.assertEqual(shown['citations'][0]['location'],{'image':True})
        prepared=self.service.deletion_prepare(source,dict(destinationId='officer',scopeId=self.f.binding['scopeId'],conversationId=self.f.cid,managementGeneration=0),self.f.key())
        self.service.deletion_confirm(prepared['id'],prepared['capability'],{'fingerprint':prepared['fingerprint']},self.f.key())
        self.service.deletions.execute(prepared['id'])
        self.assertEqual(self.service.deletion_status(prepared['id'],prepared['capability'])['state'],'deleted')
        retained=self.service.history('officer',self.f.binding['scopeId'],self.f.cid)['items'][0]
        self.assertFalse(retained['available']);self.assertEqual(retained['admissionRequestId'],request['requestId'])
        gone=self.publish(request,out)
        self.assertIsNone(gone['text']);self.assertEqual(gone['citations'],[]);self.assertEqual(gone['suppliedFileIds'],[])
        self.assertEqual(self.provider.calls,1)

    def test_publication_rejects_invalid_marker_spans_and_frozen_original_roster_without_mutation(self):
        self.provider.next.append(FakeResponse(wire(response('Plain prose.\nIMAGE_SOURCE: image-1',citations=False))))
        draft,_=self.draft(raw=PNG,media='image/png');request=self.prepare(draft);out=self.send(draft,request)
        from attachments_catalogue import HistorySelection
        context=HistorySelection.from_binding(self.f.binding)
        candidate=self.c.ledger.replay(context,out['answerId'])
        with self.store._gate():
            _,frozen,_,deps=self.c.ledger._request(context,out['answerId'])
        mutations=[lambda c,f:c['citations'][0].update(text_start=0),lambda c,f:c['citations'][0].update(text_end=len(c['text'])+1),lambda c,f:c['citations'].append(copy.deepcopy(c['citations'][0])),lambda c,f:c.update(citations=[]),lambda c,f:f['manifest']['input_map'][0].update(input_index=1),lambda c,f:f['manifest']['selection'].update(ordered_sources=[]),lambda c,f:c['consumed_dependencies'].append(copy.deepcopy(c['consumed_dependencies'][0]))]
        for mutate in mutations:
            bad,record=copy.deepcopy(candidate),copy.deepcopy(frozen);mutate(bad,record)
            with self.assertRaises(AttachmentError) as caught:_presentation(bad,record,deps)
            self.assertEqual(caught.exception.code,'document_answer_invalid')
        bad=copy.deepcopy(candidate);bad['citations'][0]['text_start']=0
        with mock.patch.object(self.c.ledger,'_answer_value',return_value=bad):
            with self.assertRaises(AttachmentError) as caught:self.publish(request,out)
        self.assertEqual(caught.exception.code,'document_answer_invalid')
        unchanged=copy.deepcopy(candidate)
        _presentation(candidate,frozen,deps)
        self.assertEqual(candidate,unchanged)
        ordinary=copy.deepcopy(candidate);ordinary.update(text='An ordinary image-1 mention.',citations=[])
        self.assertEqual(_presentation(ordinary,frozen,deps)[0],ordinary['text'])
        self.assertEqual(self.c.ledger.replay(context,out['answerId']),candidate)

    def test_admitted_error_and_unknown_outcomes_keep_original_admission_association(self):
        for rejection in (True,False):
            case=CoordinatorTests();case.setUp()
            try:
                case.provider.next.append(FakeResponse(b'{"fictional":"invalid"}',status=400 if rejection else 200))
                draft,source=case.draft();request=case.prepare(draft);out=case.send(draft,request)
                self.assertEqual(out['state'],'failed' if rejection else 'unknown')
                item=case.service.history('officer',case.f.binding['scopeId'],case.f.cid)['items'][0]
                self.assertEqual(item['id'],source);self.assertEqual(item['admissionRequestId'],request['requestId'])
            finally:case.tearDown()


    def test_marker_only_mixed_pdf_image_answer_keeps_both_typed_citations(self):
        pdf,a=self.draft(text='');self.send(pdf,self.prepare(pdf))
        image,b=self.draft(raw=PNG,media='image/png')
        self.provider.next.append(FakeResponse(wire(response('IMAGE_SOURCE: image-1',citations=True))))
        request=self.prepare(image,kept=[a]);out=self.send(image,request);shown=self.publish(request,out)
        self.assertEqual(shown['text'],'');self.assertEqual(set(shown['suppliedFileIds']),{a,b})
        self.assertEqual({c['fileId'] for c in shown['citations']},{a,b})
        self.assertEqual({json.dumps(c['location'],sort_keys=True) for c in shown['citations']},{'{"page": 2}','{"image": true}'})


    def test_older_answer_roster_excludes_later_admitted_sources(self):
        draft,a=self.draft();request=self.prepare(draft);out=self.send(draft,request)
        later,b=self.draft(text='',raw=PNG,media='image/png');self.send(later,self.prepare(later))
        shown=self.publish(request,out)
        self.assertEqual(shown['suppliedFileIds'],[a]);self.assertNotIn(b,shown['suppliedFileIds'])
        second=self.service.document_ask(self.f.cid,dict(self.selection,question='Compare both originals.',keptFileIds=[a,b],priorAnswerIds=[out['answerId']]),self.f.key())
        two=self.service.document_answer(second['answerId'],second['requestId'],second['requestCapability'],lambda v:v)
        self.assertEqual(set(two['suppliedFileIds']),{a,b})
        self.f.revoke(b)
        self.assertEqual(self.publish(request,out),shown)
        history=self.service.document_history(self.f.cid,'officer',self.f.binding['scopeId'])['items']
        answered={r['requestId']:r for r in history if r['answerId'] is not None}
        self.assertEqual(set(answered),{request['requestId'],second['requestId']})
        for original in (dict(out,requestId=request['requestId']),second):
            self.assertEqual(answered[original['requestId']]['state'],'answered')
            self.assertEqual(answered[original['requestId']]['answerId'],original['answerId'])
        withdrawn=self.service.document_answer(second['answerId'],second['requestId'],second['requestCapability'],lambda v:v)
        self.assertEqual((withdrawn['id'],withdrawn['requestId'],withdrawn['state'],withdrawn['text']),
                         (second['answerId'],second['requestId'],'source_unavailable',None))
        self.assertEqual(withdrawn['suppliedFileIds'],[]);self.assertEqual(withdrawn['citations'],[])
        for aid,rid,cap,status in ((out['answerId'],second['requestId'],second['requestCapability'],403),
                                   (second['answerId'],second['requestId'],request['requestCapability'],409)):
            result=self.http('GET','/answers/'+aid+'?requestId='+rid,headers=[('X-Attachment-Request-Capability',cap)])
            self.assertEqual(result[0],status);self.assertIn('error',result[1]);self.assertNotIn('text',result[1])
        self.assertNotIn('answerCommitted',str(history));self.assertEqual(self.provider.calls,2)

    def test_corrupt_available_answer_is_refusal_not_a_withdrawal_result(self):
        draft,_=self.draft();request=self.prepare(draft);out=self.send(draft,request)
        raw=self.read_row('SELECT answer FROM officer_document_requests WHERE id=?',(out['answerId'],))[0]
        self.write_sql('UPDATE officer_document_requests SET answer=? WHERE id=?',(raw[:-1]+b'X',out['answerId']))
        with self.assertRaises(AttachmentError) as caught:self.publish(request,out)
        self.assertEqual(caught.exception.code,'document_record_invalid');self.assertEqual(self.provider.calls,1)


class HostGateTests(unittest.TestCase):
    def test_release_enables_attachment_routes(self):
        import server
        self.assertIs(server.ATTACHMENTS_ENABLED, True)
        self.assertIs(server.OFFICER_DOCUMENTS_ENABLED, True)
        self.assertIs(server.NATIVE_AGENT_ATTACHMENTS_ENABLED, True)

    def test_off_switches_demo_and_missing_policy_construct_no_transport(self):
        import server
        import tempfile
        for attachments,documents_enabled,demo in ((False,True,'0'),(True,True,'1'),(False,False,'0')):
            with mock.patch.object(server,'ATTACHMENTS_ENABLED',attachments),mock.patch.object(server,'OFFICER_DOCUMENTS_ENABLED',documents_enabled),mock.patch.dict(os.environ,{'WATCH_DEMO':demo}),mock.patch('attachments_store.AttachmentStore',side_effect=AssertionError('constructed')):
                self.assertIsNone(server.start_attachments())
        for documents_enabled in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(os.path.realpath(tmp));(root/'.live-room').mkdir(mode=0o700)
                with mock.patch.object(server,'ATTACHMENTS_ENABLED',True),mock.patch.object(server,'OFFICER_DOCUMENTS_ENABLED',documents_enabled),mock.patch.object(server,'HOME',str(root)),mock.patch.object(server,'KEY',KEY),mock.patch.dict(os.environ,{'WATCH_DEMO':'0'}),mock.patch('officer_document_transport.DirectDocumentTransport',side_effect=AssertionError('network constructed')):
                    service = server.start_attachments()
                    try:
                        if os.name == 'posix':
                            self.assertIsNotNone(service)
                            self.assertFalse(service.capabilities('officer', 'room', documents.MODEL)['available'])
                        else:
                            self.assertIsNone(service)
                    finally:
                        if service is not None:
                            service.close()
                self.assertFalse((root/'.live-room'/policy.POLICY_FILENAME).exists())

    def test_ordinary_ask_rejects_document_fields_before_action_routing(self):
        import server
        host=server.Server(('127.0.0.1',0),server.H);port=host.server_address[1]
        thread=threading.Thread(target=host.serve_forever)
        with mock.patch.object(server,'KEY',KEY),mock.patch.object(server,'PORT',port),mock.patch.object(server,'OFFICER_KEY','fictional-officer-key'),mock.patch.object(server,'officer_ask',side_effect=AssertionError('ordinary route called')),mock.patch.object(server.H,'log_message'):
            thread.start()
            try:
                for extra in ({'conversationId':'0'*32},{'attachmentIds':['0'*32]},{'priorAnswerIds':[]}):
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                    try:
                        conn.request('POST','/ask',json.dumps(dict(turns=[{'role':'user','content':'Fictional question'}],**extra)),{'X-Live-Room-Key':KEY,'Content-Type':'application/json'})
                        response_=conn.getresponse();self.assertEqual(response_.status,400);response_.read()
                    finally:conn.close()
            finally:host.shutdown();host.server_close();thread.join()


if __name__=='__main__':unittest.main()
