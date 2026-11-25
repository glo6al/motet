"""Real request ledger and private bytes; fictional materializer/owner receipts only."""
from attachment_schema_fixtures import drop_officer_schema
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from attachments_store import AttachmentStore
from attachments_requests import (RequestLedger, TransportProfile, PhaseVerification, VerifiedCopy,
                                  CopyCleanup, PartialCopy, OwnerReceipt, OwnerAbsenceEvidence, serialize_envelope)
from attachments_types import AttachmentError
from test_attachments_foundation import EPOCH, BINDING, fence, metadata
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


class FakeLease:
    """Trusted fictional integration proof, not filesystem or provider qualification."""
    def __init__(self, keys): self.keys=set(keys);self.held=True
    def require(self, keys):
        if not self.held or not set(keys) <= self.keys:
            raise AttachmentError('resource_unavailable','Fictional resource lease is not held.',503)


@unittest.skipUnless(STORE, STORE_WHY)
class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=state_root(self.temp.name);self.serial=0;self.clock=[1800000000.0]
        self.store=self.open();self.ledger=RequestLedger(self.store)
        self.profile=TransportProfile('fictional-profile','v1','codex',BINDING['capabilityRevision'],'b'*64,'e'*64,'fictional-proof',('text/plain',))
    def open(self, **kwargs):return AttachmentStore(self.root,enabled=True,owner_epoch=EPOCH,clock=lambda:self.clock[0],**kwargs)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def key(self):self.serial+=1;return 'request-fixture-%06d'%self.serial
    def error(self, code, call):
        with self.assertRaises(AttachmentError) as caught:call()
        self.assertEqual(caught.exception.code,code)
    def draft(self,body=b'Fictional orchard',message='Review the attached orchard note.'):
        d=self.store.create_draft(BINDING,self.key());edit=self.store.edit_text(d['id'],d['capability'],fence(d),message);d['revision']=edit['revision']
        u=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(body),self.key());d['revision']=u['draftRevision']
        out=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(body));d['revision']=out['draftRevision']
        return d,u
    def prepare(self,d=None,profile=None):
        if d is None:d,_=self.draft()
        request=self.ledger.prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile or self.profile)
        return d,request,FakeLease(self.ledger.resource_keys(request['id']))
    def send(self,d=None,profile=None):
        d,r,lease=self.prepare(d,profile);self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=profile or self.profile,resource_lease=lease)
        return d,r,lease
    def status(self,r):return self.ledger.request_status(r['id'],r['capability'])
    def directories(self,r,lease):
        auth=self.ledger.authority(r['id']);self.ledger.record_directory_created(auth,'root',(1,100),lease);self.ledger.record_directory_created(auth,'request',(1,200),lease)
    def proof(self,r,lease_id,phase,copies=None,**changes):
        status=self.status(r);copies=copies or status['copies']
        return replace(PhaseVerification(r['id'],r['fingerprint'],status['revision'],self.profile.context_fingerprint,self.profile.revision,phase,
                      tuple(sorted((c['id'],c['generation']) for c in copies)),lease_id,self.clock[0],self.clock[0]+2),**changes)
    def materialized(self):
        d,r,lease=self.send();self.directories(r,lease);c=self.status(r)['copies'][0]
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],c['id'],lease)
        self.ledger.record_copy_created(c['id'],intent.lease_id,(1,300,0,1,1),lease)
        proof=self.proof(r,intent.lease_id,'publication',[c])
        result=VerifiedCopy(c['id'],intent.lease_id,c['generation'],(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,proof)
        self.ledger.record_copy_result(result,lease)
        return d,r,lease,intent
    def handoff(self):
        d,r,lease,intent=self.materialized();status=self.status(r)
        out=self.ledger.claim_handoff(r['id'],status['revision'],r['fingerprint'],self.proof(r,'c'*64,'handoff'),lease)
        return d,r,lease,out

    def test_intent_freezes_envelope_without_send_or_copy_quota(self):
        d,r,lease=self.prepare();used=self.store.storage()['usedBytes']
        self.assertEqual(r['state'],'intent');self.assertFalse(r['authorized']);self.assertEqual(used,len(b'Fictional orchard'))
        self.error('request_conflict',lambda:self.ledger.authority(r['id']))
        row=self.store.db.execute('SELECT * FROM attachment_requests').fetchone()
        self.assertIn('WATCH_REQUEST '+r['ownerRequestId'],row['envelope']);self.assertEqual(row['envelope'].count('WATCH_REPLY '),1)
        self.assertNotIn('sample.txt',row['envelope']);self.assertNotIn('Fictional orchard',row['envelope'])
        self.assertEqual(self.store.db.execute('SELECT SUM(charged) FROM workspace_copies').fetchone()[0],0)
        self.store.edit_text(d['id'],d['capability'],fence(d),'Changed instruction')
        self.error('draft_conflict',lambda:self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=self.profile,resource_lease=lease))
        self.assertEqual(self.store.storage()['usedBytes'],used)

    def test_send_reserves_each_declared_allocation_once(self):
        profile=replace(self.profile,temporary_copy=True);d,r,lease=self.prepare(profile=profile)
        used=self.store.storage()['usedBytes'];result=self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=profile,resource_lease=lease)
        self.assertEqual(len(result['copies']),2);self.assertEqual(self.store.storage()['usedBytes'],used*3)
        self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=profile,resource_lease=lease)
        self.assertEqual(self.store.storage()['usedBytes'],used*3)
        self.assertEqual(self.store.storage()['activeDrafts'],0)
        self.assertEqual(self.store.list_drafts(destination_id=BINDING['id'],scope_id=BINDING['scopeId'])['items'],[])

    def test_admitted_draft_blocks_every_old_writer(self):
        d,u=self.draft();d,r,lease=self.send(d)
        calls=[lambda:self.store.edit_text(d['id'],d['capability'],fence(d),'x'),
               lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),self.key()),
               lambda:self.store.recover_draft(d['id'],d['writerGeneration']),
               lambda:self.store.resume_draft(d['id'],d['capability'],d['revision'],d['writerGeneration']),
               lambda:self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'x'),self.key()),
               lambda:self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId']),
               lambda:self.store.retry_preparation(d['id'],d['capability'],fence(d),u['fileId']),
               lambda:self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(b''))]
        for call in calls:self.error('draft_admitted',call)
        self.assertEqual(self.store.get_draft(d['id'],d['capability'])['text'],'Review the attached orchard note.')

    def test_global_intent_key_namespace_and_exact_replay(self):
        d,u=self.draft();key=self.key();r=self.ledger.prepare_request(d['id'],d['capability'],fence(d),key,BINDING,self.profile)
        replay=self.ledger.prepare_request(d['id'],d['capability'],fence(d),key,BINDING,self.profile);self.assertEqual(r,replay)
        self.error('request_conflict',lambda:self.store.create_conversation('room',key))
        self.error('request_conflict',lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),key))
        self.error('request_conflict',lambda:self.ledger.prepare_request(d['id'],d['capability'],dict(fence(d),revision=999),key,BINDING,self.profile))

    def test_empty_text_and_envelope_overflow_reject_before_intent(self):
        d,u=self.draft(message='')
        self.error('message_required',lambda:self.prepare(d))
        d,u=self.draft()
        self.error('envelope_limit',lambda:self.prepare(d,replace(self.profile,max_characters=50)))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0],0)

    def test_source_tamper_and_profile_change_do_not_authorize_send(self):
        d,r,lease=self.prepare()
        self.error('capability_changed',lambda:self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=replace(self.profile,revision='v2'),resource_lease=lease))
        source=self.store.db.execute('SELECT id FROM files').fetchone()[0]
        (self.root/'attachments'/'objects'/source).write_bytes(b'Changed bytes')
        self.error('source_changed',lambda:self.ledger.authorize_send(r['id'],r['capability'],r['revision'],r['fingerprint'],resolved_binding=BINDING,transport_profile=self.profile,resource_lease=lease))
        self.assertFalse(self.status(r)['authorized'])

    def test_directory_identity_and_copy_creation_are_required(self):
        d,r,lease=self.send();copy=self.status(r)['copies'][0]
        self.error('directory_unverified',lambda:self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease))
        self.directories(r,lease)
        self.error('source_changed',lambda:self.ledger.record_directory_created(self.ledger.authority(r['id']),'root',(9,9),lease))
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        proof=self.proof(r,intent.lease_id,'publication',[copy])
        result=VerifiedCopy(copy['id'],intent.lease_id,copy['generation'],(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,proof)
        self.error('copy_conflict',lambda:self.ledger.record_copy_result(result,lease))
        self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        self.error('copy_conflict',lambda:self.ledger.record_copy_result(replace(result,identity=(1,999,intent.bytes,2,2)),lease))
        self.ledger.record_copy_result(result,lease)
        self.assertEqual(self.status(r)['state'],'materialized')

    def test_publication_fenced_by_cleanup_and_released_lock(self):
        d,r,lease=self.send();self.directories(r,lease);copy=self.status(r)['copies'][0]
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        result=VerifiedCopy(copy['id'],intent.lease_id,copy['generation'],(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,self.proof(r,intent.lease_id,'publication',[copy]))
        lease.held=False;self.error('resource_unavailable',lambda:self.ledger.record_copy_result(result,lease));lease.held=True
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
        self.error('proof_unavailable',lambda:self.ledger.record_copy_result(result,lease))
        self.assertGreater(self.store.storage()['usedBytes'],intent.bytes)

    @unittest.skipUnless(POSIX, POSIX_WHY)
    def test_source_handles_prevent_store_close(self):
        d,r,lease=self.send();self.directories(r,lease);copy=self.status(r)['copies'][0]
        self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        with self.ledger.source_handle(copy['id'],lease) as fd:
            self.assertEqual(os.read(fd,100),b'Fictional orchard')
            self.error('upload_busy',self.store.close)
        self.assertEqual(self.store.request_handles,set())

    def test_handoff_is_one_claim_and_restart_stays_unknown(self):
        d,r,lease,out=self.handoff()
        self.assertTrue(out['newClaim']);self.assertEqual(self.status(r)['state'],'handoff_claimed')
        replay=self.ledger.claim_handoff(r['id'],999,r['fingerprint'],None,lease)
        self.assertFalse(replay['newClaim']);self.assertNotIn('envelope',replay)
        self.store.close();self.store=self.open();self.ledger=RequestLedger(self.store)
        self.assertEqual(self.status(r)['state'],'unknown')
        replay=self.ledger.claim_handoff(r['id'],999,r['fingerprint'],None,lease)
        self.assertFalse(replay['newClaim']);self.assertEqual(replay['claimId'],out['claimId'])
        self.error('handoff_uncertain',lambda:self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision']))

    def test_only_correlated_terminal_receipt_releases_retention_pin(self):
        d,r,lease,out=self.handoff()
        receipt=OwnerReceipt(r['id'],r['ownerRequestId'],out['claimId'],r['fingerprint'],EPOCH,1,'owner_recorded','owner_recorded')
        self.assertTrue(self.ledger.record_owner_receipt(receipt)['retentionPinned'])
        self.assertEqual(self.status(r)['state'],'owner_recorded')
        queued=replace(receipt,sequence=2,state='queued',provenance='codex_queue_accepted',attempt_id='fictional-attempt')
        self.assertTrue(self.ledger.apply_owner_outcome(queued)['retentionPinned'])
        self.error('request_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,state='unknown',provenance='dispatch_uncertain')))
        self.error('outcome_unverified',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=3,provenance='claude_submitted')))
        final=replace(queued,sequence=3,state='answered',provenance='correlated_reply')
        self.assertFalse(self.ledger.apply_owner_outcome(final)['retentionPinned'])
        self.assertEqual(self.ledger.apply_owner_outcome(final)['acknowledgedSequence'],3)
        self.assertIsNone(self.ledger.apply_owner_outcome(queued)['acknowledgedSequence'])
        self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=4)))
        self.assertGreater(self.store.storage()['usedBytes'],0)

    def test_saved_cleanup_releases_only_verified_copy_bytes_once(self):
        d,r,lease=self.send();charged=self.store.storage()['usedBytes']
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
        copy=self.status(r)['copies'][0];plan=self.ledger.cleanup_copy(copy['id'])
        result=CopyCleanup(copy['id'],copy['generation'],plan['intent'].lease_id,'never_created',True)
        self.error('cleanup_unverified',lambda:self.ledger.record_copy_cleanup(replace(result,handles_closed=False),lease))
        failed=replace(result,result='changed');self.ledger.record_copy_cleanup(failed,lease)
        self.assertEqual(self.store.storage()['usedBytes'],charged)
        self.ledger.record_copy_cleanup(result,lease);self.assertEqual(self.status(r)['state'],'cleaned')
        self.ledger.record_copy_cleanup(result,lease);self.assertEqual(self.store.storage()['usedBytes'],charged//2)

    def test_claimed_copy_cannot_be_assumed_never_created(self):
        d,r,lease=self.send();self.directories(r,lease);copy=self.status(r)['copies'][0]
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision']);copy=self.status(r)['copies'][0]
        self.error('cleanup_unverified',lambda:self.ledger.record_copy_cleanup(CopyCleanup(copy['id'],copy['generation'],intent.lease_id,'never_created',True),lease))

    def test_stale_phase_or_copy_generation_prevents_handoff(self):
        d,r,lease,intent=self.materialized();revision=self.status(r)['revision'];proof=self.proof(r,'c'*64,'handoff')
        for bad in (replace(proof,expires_at=self.clock[0]-1),replace(proof,context_fingerprint='d'*64),replace(proof,copy_generations=(('0'*32,1),)),replace(proof,operation_revision=1)):
            self.error('proof_unavailable',lambda:self.ledger.claim_handoff(r['id'],revision,r['fingerprint'],bad,lease))
        self.assertEqual(self.status(r)['state'],'materialized')

    def test_a_five_second_window_is_accepted_whatever_the_rounding(self):
        """checked_at + 5 is a valid window even where (checked_at + 5) - checked_at rounds above 5 (here just below
        2**31 seconds); both the handoff proof and the finish evidence."""
        start = 2147483643.003
        self.assertGreater((start + 5.0) - start, 5)                   # what the old comparison refused
        d,r,lease,intent=self.materialized();revision=self.status(r)['revision']
        self.clock[0]=start
        for bad in (dict(expires_at=start), dict(expires_at=start+5.001)):
            self.error('proof_unavailable',lambda bad=bad:self.ledger.claim_handoff(r['id'],revision,r['fingerprint'],
                       self.proof(r,'c'*64,'handoff',checked_at=start,**bad),lease))
        out=self.ledger.claim_handoff(r['id'],revision,r['fingerprint'],
                                      self.proof(r,'c'*64,'handoff',checked_at=start,expires_at=start+5.0),lease)
        self.assertTrue(out['newClaim']);self.assertEqual(self.status(r)['state'],'handoff_claimed')
        d,r,lease=self.send();status=self.status(r)
        proof=self.proof(r,'c'*64,'finish',checked_at=start,expires_at=start+5.0)
        absence=OwnerAbsenceEvidence(r['id'],r['ownerRequestId'],r['fingerprint'],start,start+5.0,True)
        self.error('request_conflict',lambda:self.ledger.begin_finish(r['id'],r['capability'],status['revision'],
                   r['fingerprint'],replace(absence,expires_at=start+5.001),proof,lease))
        out=self.ledger.begin_finish(r['id'],r['capability'],status['revision'],r['fingerprint'],absence,proof,lease)
        self.assertEqual(out['state'],'send_authorized')

    def test_metadata_full_cleanup_and_tombstones_survive_restart(self):
        self.store.close();self.store=self.open(overrides={'database_bytes':327680});self.ledger=RequestLedger(self.store)
        d,r,lease=self.send()
        while True:
            try:self.store.create_draft(BINDING,self.key())
            except AttachmentError as error:
                self.assertIn(error.code,('history_capacity','storage_unavailable'));break
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
        copy=self.status(r)['copies'][0];intent=self.ledger.cleanup_copy(copy['id'])['intent']
        result=CopyCleanup(copy['id'],copy['generation'],intent.lease_id,'never_created',True)
        self.ledger.record_copy_cleanup(result,lease)
        self.store.close();self.store=self.open(overrides={'database_bytes':327680});self.ledger=RequestLedger(self.store)
        self.assertEqual(self.status(r)['state'],'cleaned')
        self.ledger.record_copy_cleanup(result,lease)
        self.assertEqual(self.store.storage()['usedBytes'],len(b'Fictional orchard'))

    def test_finish_needs_exact_fresh_owner_absence(self):
        d,r,lease=self.send();status=self.status(r)
        proof=self.proof(r,'c'*64,'finish')
        absence=OwnerAbsenceEvidence(r['id'],r['ownerRequestId'],r['fingerprint'],self.clock[0],self.clock[0]+2,True)
        self.error('owner_status_unavailable',lambda:self.ledger.begin_finish(r['id'],r['capability'],status['revision'],r['fingerprint'],replace(absence,absent=False),proof,lease))
        self.error('request_conflict',lambda:self.ledger.begin_finish(r['id'],r['capability'],status['revision'],r['fingerprint'],replace(absence,owner_request_id='foreign'),proof,lease))
        out=self.ledger.begin_finish(r['id'],r['capability'],status['revision'],r['fingerprint'],absence,proof,lease)
        self.assertEqual(out['id'],r['id']);self.assertEqual(out['state'],'send_authorized')
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],out['revision'])
        self.error('request_conflict',lambda:self.ledger.begin_finish(r['id'],r['capability'],self.status(r)['revision'],r['fingerprint'],absence,proof,lease))

    def test_temporary_peak_allocation_releases_without_releasing_retention(self):
        profile=replace(self.profile,temporary_copy=True);d,r,lease=self.send(profile=profile)
        temp=next(c for c in self.status(r)['copies'] if c['role']=='temporary')
        intent=self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id'])
        self.ledger.record_copy_cleanup(CopyCleanup(temp['id'],intent.copy_generation,intent.lease_id,'never_created',True),lease)
        self.assertTrue(self.status(r)['retentionPinned'])
        self.assertEqual(self.store.storage()['usedBytes'],2*len(b'Fictional orchard'))
        self.directories(r,lease)
        copy=next(c for c in self.status(r)['copies'] if c['role']=='workspace')
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        self.ledger.record_copy_result(VerifiedCopy(copy['id'],intent.lease_id,intent.copy_generation,(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,self.proof(r,intent.lease_id,'publication',[copy])),lease)
        self.assertEqual(self.status(r)['state'],'materialized')

    def test_wrong_attempt_and_nonexecution_after_acceptance_reject(self):
        d,r,lease,out=self.handoff()
        queued=OwnerReceipt(r['id'],r['ownerRequestId'],out['claimId'],r['fingerprint'],EPOCH,1,'queued','codex_queue_accepted','attempt-a')
        self.ledger.apply_owner_outcome(queued)
        self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=2,state='answered',provenance='correlated_reply',attempt_id='attempt-b')))
        self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=2,state='failed',provenance='proven_not_submitted')))
        self.assertTrue(self.status(r)['retentionPinned'])

    def test_full_metadata_does_not_block_reserved_handoff_or_terminal_receipt(self):
        self.store.close();self.store=self.open(overrides={'database_bytes':327680});self.ledger=RequestLedger(self.store)
        d,r,lease,intent=self.materialized()
        while True:
            try:self.store.create_draft(BINDING,self.key())
            except AttachmentError as error:
                self.assertIn(error.code,('history_capacity','storage_unavailable'));break
        out=self.ledger.claim_handoff(r['id'],self.status(r)['revision'],r['fingerprint'],self.proof(r,'c'*64,'handoff'),lease)
        receipt=OwnerReceipt(r['id'],r['ownerRequestId'],out['claimId'],r['fingerprint'],EPOCH,1,'answered','correlated_reply','attempt-a')
        self.assertFalse(self.ledger.apply_owner_outcome(receipt)['retentionPinned'])
        self.store.close();self.store=self.open(overrides={'database_bytes':327680});self.ledger=RequestLedger(self.store)
        self.assertEqual(self.status(r)['state'],'answered')
        self.assertEqual(self.ledger.apply_owner_outcome(receipt)['acknowledgedSequence'],1)

    def test_request_capacity_never_evicts_intent_tombstones(self):
        self.store.close();self.store=self.open(overrides={'requests':1});self.ledger=RequestLedger(self.store)
        d,r,lease=self.prepare()
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],r['revision'])
        copy=self.status(r)['copies'][0];intent=self.ledger.cleanup_copy(copy['id'])['intent']
        self.ledger.record_copy_cleanup(CopyCleanup(copy['id'],copy['generation'],intent.lease_id,'never_created',True),lease)
        self.error('request_capacity',lambda:self.prepare(d))
        self.assertEqual(self.status(r)['state'],'cleaned')

    def test_owner_reset_revokes_request_capability_but_retains_quota(self):
        d,r,lease=self.send();used=self.store.storage()['usedBytes'];self.store.close()
        self.store=AttachmentStore(self.root,enabled=True,owner_epoch='c'*64);self.ledger=RequestLedger(self.store)
        self.error('forbidden',lambda:self.status(r))
        self.assertEqual(self.store.storage()['usedBytes'],used)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0],1)

    def test_day31_retains_request_and_known_copy_debt(self):
        d,r,lease=self.send();used=self.store.storage()['usedBytes'];self.clock[0]+=31*86400
        self.store.close();self.store=self.open();self.ledger=RequestLedger(self.store)
        self.assertEqual(self.status(r)['state'],'send_authorized');self.assertEqual(self.store.storage()['usedBytes'],used)

    def test_partial_evidence_stays_failed_and_cleans_only_exact_prefix(self):
        d,r,lease=self.send();self.directories(r,lease);copy=self.status(r)['copies'][0]
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        partial=PartialCopy(copy['id'],intent.lease_id,intent.copy_generation,(1,300,4,2,2),hashlib.sha256(b'Fict').hexdigest(),4,True)
        self.error('cleanup_unverified',lambda:self.ledger.record_partial(replace(partial,handles_closed=False),lease))
        self.error('source_changed',lambda:self.ledger.record_partial(replace(partial,sha256='0'*64),lease))
        charged=self.store.storage()['usedBytes'];self.ledger.record_partial(partial,lease);self.ledger.record_partial(partial,lease)
        self.assertEqual(self.status(r)['copies'][0]['state'],'partial');self.assertEqual(self.store.storage()['usedBytes'],charged)
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
        plan=self.ledger.cleanup_copy(copy['id']);self.assertEqual(plan['sha256'],partial.sha256);self.assertEqual(plan['identity'],list(partial.identity));self.assertTrue(plan['partial'])
        self.ledger.record_copy_cleanup(CopyCleanup(copy['id'],plan['intent'].copy_generation,intent.lease_id,'removed',True),lease)
        self.assertEqual(self.store.storage()['usedBytes'],charged//2)

    def test_late_partial_receipt_can_only_help_cleanup(self):
        d,r,lease=self.send();self.directories(r,lease);copy=self.status(r)['copies'][0]
        intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
        self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
        partial=PartialCopy(copy['id'],intent.lease_id,intent.copy_generation,(1,300,0,1,1),hashlib.sha256(b'').hexdigest(),0,True)
        self.ledger.record_partial(partial,lease)
        self.assertEqual(self.status(r)['state'],'cleanup');self.assertEqual(self.status(r)['copies'][0]['state'],'cleanup_pending')
        self.assertEqual(self.ledger.cleanup_copy(copy['id'])['sha256'],partial.sha256)

    def test_root_identity_shared_only_by_verified_workspace_fingerprint(self):
        d,r,lease=self.send();self.directories(r,lease)
        second_profile=replace(self.profile,context_fingerprint='f'*64,revision='v2')
        d2,r2,lease2=self.send(profile=second_profile)
        self.assertEqual(self.ledger.workspace_directories(r2['id'])['root_identity'],[1,100])
        self.assertIsNone(self.ledger.workspace_directories(r2['id'])['request_identity'])
        self.assertEqual(next(k for k in lease.keys if k.startswith('workspace:')),next(k for k in lease2.keys if k.startswith('workspace:')))
        isolated=replace(self.profile,workspace_fingerprint='c'*64)
        d3,r3,lease3=self.send(profile=isolated)
        self.assertIsNone(self.ledger.workspace_directories(r3['id'])['root_identity'])

    def test_verified_execution_end_keeps_reply_unknown_for_both_adapters(self):
        for adapter,accepted,ended,context in (('codex','codex_queue_accepted','codex_turn_ended','b'),('claude_official','claude_submitted','claude_turn_ended','f')):
            with self.subTest(adapter=adapter):
                self.profile=replace(self.profile,adapter=adapter,context_fingerprint=context*64)
                d,r,lease,out=self.handoff()
                other_d,other,other_lease=self.send()
                charged=self.store.storage()['usedBytes']
                queued=OwnerReceipt(r['id'],r['ownerRequestId'],out['claimId'],r['fingerprint'],EPOCH,1,'queued',accepted,'attempt-'+adapter)
                self.ledger.apply_owner_outcome(queued)
                terminal=replace(queued,sequence=2,state='execution_ended',provenance=ended)
                wrong_provenance='claude_turn_ended' if adapter=='codex' else 'codex_turn_ended'
                self.error('outcome_unverified',lambda:self.ledger.apply_owner_outcome(replace(terminal,provenance=wrong_provenance)))
                self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(terminal,attempt_id='foreign-attempt')))
                self.error('outcome_unverified',lambda:self.ledger.apply_owner_outcome(replace(terminal,attempt_id=None)))
                result=self.ledger.apply_owner_outcome(terminal)
                self.assertFalse(result['retentionPinned']);self.assertEqual(result['acknowledgedSequence'],2)
                self.assertEqual(self.status(r)['state'],'execution_ended');self.assertEqual(self.status(r)['replyState'],'unknown')
                self.assertTrue(self.status(other)['retentionPinned']);self.assertEqual(self.store.storage()['usedBytes'],charged)
                self.assertEqual(self.ledger.apply_owner_outcome(terminal),result)
                self.assertIsNone(self.ledger.apply_owner_outcome(queued)['acknowledgedSequence'])
                self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=3)))
                self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(queued,sequence=3,state='unknown',provenance='dispatch_uncertain')))
                self.store.close();self.store=self.open();self.ledger=RequestLedger(self.store)
                self.assertEqual(self.status(r)['state'],'execution_ended');self.assertEqual(self.status(r)['replyState'],'unknown')
                self.assertEqual(self.ledger.apply_owner_outcome(terminal),result)
                self.assertEqual(self.store.storage()['usedBytes'],charged)
                replay=self.ledger.claim_handoff(r['id'],999,r['fingerprint'],None,lease)
                self.assertFalse(replay['newClaim']);self.assertNotIn('envelope',replay)
                answer=replace(terminal,sequence=3,state='answered',provenance='correlated_reply')
                self.assertFalse(self.ledger.apply_owner_outcome(answer)['retentionPinned'])
                self.assertEqual(self.status(r)['replyState'],'available')
                self.error('outcome_conflict',lambda:self.ledger.apply_owner_outcome(replace(terminal,sequence=4)))

    def test_temporary_cleanup_plan_for_never_written_and_fully_written_copy(self):
        for full in (False,True):
            with self.subTest(full=full):
                profile=replace(self.profile,temporary_copy=True);d,r,lease=self.send(profile=profile)
                temp=next(c for c in self.status(r)['copies'] if c['role']=='temporary')
                workspace=next(c for c in self.status(r)['copies'] if c['role']=='workspace')
                self.error('cleanup_not_authorized',lambda:self.ledger.cleanup_copy(temp['id']))
                self.error('cleanup_not_authorized',lambda:self.ledger.cleanup_copy(workspace['id']))
                if full:
                    self.directories(r,lease)
                    claimed=self.ledger.claim_copy(r['id'],self.status(r)['revision'],temp['id'],lease)
                    self.ledger.record_copy_created(temp['id'],claimed.lease_id,(1,301,0,1,1),lease)
                    self.ledger.record_copy_result(VerifiedCopy(temp['id'],claimed.lease_id,claimed.copy_generation,(1,301,claimed.bytes,2,2),claimed.sha256,claimed.bytes,self.proof(r,claimed.lease_id,'publication',[temp])),lease)
                before=self.store.storage()['usedBytes'];stage=self.status(r)['state']
                intent=self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id'])
                again=self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id']);self.assertEqual(intent,again)
                plan=self.ledger.cleanup_copy(temp['id']);self.assertEqual(plan['intent'],intent)
                self.assertEqual(plan['neverClaimed'],not full);self.assertEqual(plan['partial'],False)
                self.assertEqual(plan['identity'] is not None,full)
                self.assertEqual(self.status(r)['state'],stage)
                self.error('cleanup_not_authorized',lambda:self.ledger.cleanup_copy(workspace['id']))
                result=CopyCleanup(temp['id'],intent.copy_generation,intent.lease_id,'removed' if full else 'never_created',True)
                self.ledger.record_copy_cleanup(result,lease);self.ledger.record_copy_cleanup(result,lease)
                self.assertEqual(self.store.storage()['usedBytes'],before-intent.bytes)
                self.assertTrue(self.status(r)['retentionPinned'])
                self.assertEqual(next(c for c in self.status(r)['copies'] if c['id']==workspace['id'])['state'],'reserved')
                self.assertEqual(self.ledger.cleanup_copy(temp['id'])['intent'],intent)

    def test_temporary_cleanup_accepts_only_exact_closed_late_partial_evidence(self):
        profile=replace(self.profile,temporary_copy=True);d,r,lease=self.send(profile=profile);self.directories(r,lease)
        temp=next(c for c in self.status(r)['copies'] if c['role']=='temporary')
        claimed=self.ledger.claim_copy(r['id'],self.status(r)['revision'],temp['id'],lease)
        self.ledger.record_copy_created(temp['id'],claimed.lease_id,(1,301,0,1,1),lease)
        before=self.store.storage()['usedBytes']
        intent=self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id'])
        partial=PartialCopy(temp['id'],claimed.lease_id,claimed.copy_generation,(1,301,4,2,2),hashlib.sha256(b'Fict').hexdigest(),4,True)
        self.error('cleanup_unverified',lambda:self.ledger.record_partial(replace(partial,handles_closed=False),lease))
        self.error('copy_conflict',lambda:self.ledger.record_partial(replace(partial,lease_id='c'*64),lease))
        self.error('copy_conflict',lambda:self.ledger.record_partial(replace(partial,copy_generation=99),lease))
        self.ledger.record_partial(partial,lease);self.ledger.record_partial(partial,lease)
        self.assertEqual(self.status(r)['state'],'materializing')
        self.assertEqual(next(c for c in self.status(r)['copies'] if c['id']==temp['id'])['state'],'cleanup_pending')
        self.assertEqual(self.store.storage()['usedBytes'],before);self.assertTrue(self.status(r)['retentionPinned'])
        plan=self.ledger.cleanup_copy(temp['id']);self.assertTrue(plan['partial']);self.assertEqual(plan['identity'],list(partial.identity));self.assertEqual(plan['sha256'],partial.sha256)
        result=CopyCleanup(temp['id'],intent.copy_generation,intent.lease_id,'removed',True)
        self.ledger.record_copy_cleanup(result,lease);self.ledger.record_copy_cleanup(result,lease);self.ledger.record_partial(partial,lease)
        self.assertEqual(self.store.storage()['usedBytes'],before-intent.bytes);self.assertTrue(self.status(r)['retentionPinned'])
        self.assertEqual(next(c for c in self.status(r)['copies'] if c['id']==temp['id'])['state'],'removed')

    def test_temporary_and_whole_cleanup_fences_compose_with_writer_evidence(self):
        for order in ('temporary_then_whole','whole_then_temporary'):
            for evidence_time in ('before','between','after'):
                with self.subTest(order=order,evidence_time=evidence_time):
                    profile=replace(self.profile,temporary_copy=True);d,r,lease=self.send(profile=profile);self.directories(r,lease)
                    temp=next(c for c in self.status(r)['copies'] if c['role']=='temporary')
                    workspace=next(c for c in self.status(r)['copies'] if c['role']=='workspace')
                    claimed=self.ledger.claim_copy(r['id'],self.status(r)['revision'],temp['id'],lease)
                    self.ledger.record_copy_created(temp['id'],claimed.lease_id,(1,301,0,1,1),lease)
                    partial=PartialCopy(temp['id'],claimed.lease_id,claimed.copy_generation,(1,301,4,2,2),hashlib.sha256(b'Fict').hexdigest(),4,True)
                    charged=self.store.storage()['usedBytes']
                    if evidence_time=='before':self.ledger.record_partial(partial,lease)
                    if order=='temporary_then_whole':
                        first=self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id'])
                        self.ledger.record_copy_cleanup(CopyCleanup(temp['id'],first.copy_generation,first.lease_id,'changed',True),lease)
                    else:
                        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
                        first=self.ledger.cleanup_copy(temp['id'])['intent']
                    self.assertEqual(first.copy_generation,claimed.copy_generation+1)
                    if evidence_time=='between':self.ledger.record_partial(partial,lease)
                    if order=='temporary_then_whole':self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
                    else:self.assertEqual(self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id']),first)
                    plan=self.ledger.cleanup_copy(temp['id']);self.assertEqual(plan['intent'].copy_generation,first.copy_generation)
                    self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision'])
                    self.assertEqual(self.ledger.begin_temporary_cleanup(r['id'],self.status(r)['revision'],temp['id']),first)
                    for invalid in (replace(partial,lease_id='c'*64),replace(partial,copy_generation=first.copy_generation),replace(partial,identity=(1,999,4,2,2))):
                        self.error('copy_conflict',lambda invalid=invalid:self.ledger.record_partial(invalid,lease))
                    if evidence_time=='after':self.ledger.record_partial(partial,lease)
                    self.ledger.record_partial(partial,lease)
                    plan=self.ledger.cleanup_copy(temp['id']);self.assertEqual(plan['sha256'],partial.sha256)
                    self.assertEqual(self.status(r)['state'],'cleanup');self.assertEqual(self.store.storage()['usedBytes'],charged)
                    current=next(c for c in self.status(r)['copies'] if c['id']==temp['id'])
                    publication=VerifiedCopy(temp['id'],claimed.lease_id,current['generation'],(1,301,claimed.bytes,2,2),claimed.sha256,claimed.bytes,self.proof(r,claimed.lease_id,'publication',[current]))
                    self.error('copy_conflict',lambda:self.ledger.record_copy_result(publication,lease))
                    self.error('request_conflict',lambda:self.ledger.claim_handoff(r['id'],self.status(r)['revision'],r['fingerprint'],self.proof(r,'c'*64,'handoff'),lease))
                    result=CopyCleanup(temp['id'],first.copy_generation,claimed.lease_id,'removed',True)
                    self.ledger.record_copy_cleanup(result,lease);self.ledger.record_copy_cleanup(result,lease)
                    self.assertEqual(self.store.storage()['usedBytes'],charged-claimed.bytes);self.assertTrue(self.status(r)['retentionPinned'])
                    workspace_plan=self.ledger.cleanup_copy(workspace['id'])['intent']
                    self.ledger.record_copy_cleanup(CopyCleanup(workspace['id'],workspace_plan.copy_generation,workspace_plan.lease_id,'never_created',True),lease)
                    self.assertEqual(self.status(r)['state'],'cleaned');self.assertFalse(self.status(r)['retentionPinned'])
                    remaining=self.store.storage()['usedBytes']
                    self.ledger.record_partial(partial,lease);self.ledger.record_copy_cleanup(result,lease)
                    self.assertEqual(self.status(r)['state'],'cleaned');self.assertEqual(self.store.storage()['usedBytes'],remaining)

    def test_schema_two_migration_preserves_private_files_and_discard(self):
        d,u=self.draft();self.store.close()
        db=sqlite3.connect(self.root/'attachments'/'ledger.sqlite3')
        drop_officer_schema(db)
        for table in ('request_files','workspace_copies','attachment_outbox','attachment_requests','workspace_roots'):db.execute('DROP TABLE '+table)
        db.execute('DROP INDEX draft_listing')
        db.execute('CREATE INDEX draft_listing ON drafts(destination_id,scope_id,state,created,id)')
        db.execute('ALTER TABLE drafts DROP COLUMN admitted_request');db.execute('ALTER TABLE files DROP COLUMN access_generation');db.execute('ALTER TABLE files DROP COLUMN deletion_generation')
        db.execute('ALTER TABLE files DROP COLUMN preparation_kind')
        db.execute('PRAGMA user_version=2');db.commit();db.close()
        self.store=self.open();self.ledger=RequestLedger(self.store)
        self.assertEqual(self.store.read_text(d['id'],d['capability'],u['fileId'])['text'],'Fictional orchard')
        self.assertEqual(self.store.discard_draft(d['id'],d['capability'],fence(d),self.key())['state'],'discarded')


class SerializerTests(unittest.TestCase):
    def test_exact_unicode_text_and_caps_no_document_or_name_injection(self):
        p=TransportProfile('p','v','codex','c','a'*64,'e'*64,'q',('text/plain',))
        refs=[{'id':'a'*32,'path':'.watch-attachments/'+ 'b'*32+'/'+ 'c'*32+'.txt','mediaType':'text/plain','bytes':1,'sha256':'d'*64}]
        owner='00000000-0000-4000-8000-000000000001';text='Read exactly these words: 🍎'
        value=serialize_envelope(owner,text,refs,p);self.assertIn(text,value)
        self.assertEqual(serialize_envelope(owner,text,refs,p),value)
        self.assertEqual(serialize_envelope(owner,text,refs,replace(p,max_characters=len(value))),value)
        for profile in (replace(p,max_characters=len(value)-1),replace(p,max_utf8_bytes=len(value.encode())-1)):
            with self.assertRaises(AttachmentError):serialize_envelope(owner,text,refs,profile)
        with self.assertRaises(AttachmentError):serialize_envelope(owner,'x'*2001,refs,p)
        with self.assertRaises(AttachmentError):serialize_envelope(owner,text,[dict(refs[0],path='../file')],p)

if __name__=='__main__':unittest.main()
