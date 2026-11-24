"""Internal local Officer retention using actual temporary storage; no model calls."""
from contextlib import closing
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from attachments_conversations import OfficerConversations, OfficerRetentionProfile, SourceDependency
from attachments_catalogue import Catalogue, HistorySelection
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_requests import RequestLedger, TransportProfile
from attachments_types import AttachmentError
from attachment_schema_fixtures import drop_officer_schema
from test_attachments_foundation import EPOCH, BINDING, fence, metadata
from test_attachments_requests import FakeLease
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


@unittest.skipUnless(STORE, STORE_WHY)
class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=state_root(self.temp.name);self.clock=[1800000000.0];self.serial=0
        self.store=self.open();self.cid=self.store.create_conversation('fictional-room',self.key())['id']
        self.binding=dict(kind='officer',id='officer',scopeId='fictional-room',conversationId=self.cid,modelId='fictional-fast',capabilityRevision='fictional-v1')
        self.profile=OfficerRetentionProfile('fictional-retention','v1','local_retention','fictional-v1')
    def open(self,**kwargs):return AttachmentStore(self.root,enabled=True,owner_epoch=EPOCH,clock=lambda:self.clock[0],**kwargs)
    def tearDown(self):self.store.close();self.temp.cleanup()
    @property
    def officer(self):return OfficerConversations(self.store)
    @property
    def selection(self):return HistorySelection.from_binding(self.binding)
    def key(self):self.serial+=1;return 'fictional-officer-%08d'%self.serial
    def error(self,code,call):
        with self.assertRaises(AttachmentError) as caught:call()
        self.assertEqual(caught.exception.code,code)
    def upload(self,d,body=b'Fictional retained note',**changes):
        u=self.store.reserve_upload(d['id'],d['capability'],fence(d),dict(metadata(body),**changes),self.key());d['revision']=u['draftRevision']
        result=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(body));d['revision']=result['draftRevision'];return u
    def draft(self,body=b'Fictional retained note',binding=None,message=''):
        d=self.store.create_draft(binding or self.binding,self.key())
        if message:d['revision']=self.store.edit_text(d['id'],d['capability'],fence(d),message)['revision']
        return d,self.upload(d,body)
    def admit(self,d,*,key=None,binding=None,profile=None):
        return self.officer.admit(d['id'],d['capability'],fence(d),key or self.key(),binding or d['destination'],profile or self.profile)
    def native(self,d):
        ledger=RequestLedger(self.store);profile=TransportProfile('fictional-profile','v1','codex',BINDING['capabilityRevision'],'b'*64,'e'*64,'fictional-proof',('text/plain',))
        request=ledger.prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile)
        lease=FakeLease(ledger.resource_keys(request['id']))
        ledger.authorize_send(request['id'],request['capability'],request['revision'],request['fingerprint'],resolved_binding=BINDING,transport_profile=profile,resource_lease=lease)
        return ledger,request
    def revoke(self,fid):
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('UPDATE files SET access_generation=access_generation+1,deletion_generation=deletion_generation+1 WHERE id=?',(fid,))
            return self.officer.invalidate_source_in_transaction(fid)

    def test_admission_is_local_exact_and_no_extra_quota(self):
        d,u=self.draft(message='');used=self.store.storage()['usedBytes'];result=self.admit(d)
        self.assertEqual(result['state'],'retained_local');self.assertFalse(result['modelCallStarted']);self.assertTrue(result['retainedLocally'])
        self.assertEqual(result['text'],'');self.assertEqual(result['conversationId'],self.cid)
        self.assertEqual([f['id'] for f in result['files']],[u['fileId']]);self.assertEqual(self.store.storage()['usedBytes'],used)
        self.assertEqual(self.store.storage()['activeDrafts'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_outbox').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM workspace_copies').fetchone()[0],0)

    def test_exact_retry_has_no_io_or_new_receipt_and_new_key_conflicts(self):
        d,u=self.draft();key=self.key();first=self.admit(d,key=key);before=self.store.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0]
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('no retry IO')):
            self.assertEqual(self.admit(d,key=key),first)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0],before)
        self.error('draft_admitted',lambda:self.admit(d))
        self.error('request_conflict',lambda:self.admit(d,key=key,binding=dict(self.binding,modelId='fictional-careful')))
        self.error('request_conflict',lambda:self.store.create_conversation('fictional-other',key))
        self.revoke(u['fileId']);self.assertFalse(self.admit(d,key=key)['files'][0]['available'])

    def test_existing_command_keys_and_foreign_draft_key_reject(self):
        key=self.key();d=self.store.create_draft(self.binding,key);self.upload(d)
        self.error('request_conflict',lambda:self.admit(d,key=key))
        d2,u=self.draft();key2=self.key();self.admit(d2,key=key2)
        self.error('request_conflict',lambda:self.admit(d,key=key2))
        self.assertEqual(self.store.admission_kind(self.store._draft(d['id'])),None)

    def test_wrong_binding_profile_and_non_officer_reject_atomically(self):
        d,u=self.draft()
        for binding in (dict(self.binding,scopeId='fictional-other'),dict(self.binding,modelId='fictional-other'),dict(self.binding,conversationId='0'*32),BINDING):
            self.error('capability_changed',lambda b=binding:self.admit(d,binding=b))
        self.error('capability_changed',lambda:self.admit(d,profile=replace(self.profile,capability_revision='changed')))
        self.error('profile_unavailable',lambda:self.admit(d,profile=object()))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)
        self.assertIsNone(self.store.admission_kind(self.store._draft(d['id'])))

    def test_incomplete_and_failed_sources_never_select_only_ready_subset(self):
        d,u=self.draft();pending=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'second'),self.key());d['revision']=pending['draftRevision']
        self.error('source_unavailable',lambda:self.admit(d))
        self.error('upload_digest',lambda:self.store.receive_upload(d['id'],d['capability'],fence(d),pending['id'],pending['fingerprint'],io.BytesIO(b'wrong')))
        self.error('source_unavailable',lambda:self.admit(d))

    def test_failed_pdf_and_opaque_relay_are_not_officer_retention(self):
        d=self.store.create_draft(self.binding,self.key());self.upload(d,b'%PDF-1.7\nfictional',name='fictional.pdf',mediaType='application/pdf')
        self.error('source_unavailable',lambda:self.admit(d))
        d2,u=self.draft()
        with self.store._gate(),self.store._transaction():self.store.db.execute("UPDATE files SET preparation_kind='original_relay' WHERE id=?",(u['fileId'],))
        self.error('source_unavailable',lambda:self.admit(d2))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)

    def test_all_old_writer_and_preview_paths_are_fenced(self):
        d,u=self.draft();oldcap=Catalogue(self.store).draft(d['id'],d['capability'])['files'][0]['sourceCapability'];message=self.admit(d)
        calls=[lambda:self.store.edit_text(d['id'],d['capability'],fence(d),'later'),
               lambda:self.store.recover_draft(d['id'],d['writerGeneration']),
               lambda:self.store.resume_draft(d['id'],d['capability'],d['revision'],d['writerGeneration']),
               lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),self.key()),
               lambda:self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'new'),self.key()),
               lambda:self.store.retry_preparation(d['id'],d['capability'],fence(d),u['fileId']),
               lambda:self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId']),
               lambda:self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(b'')),
               lambda:self.store.read_text(d['id'],d['capability'],u['fileId'])]
        for call in calls:self.error('draft_admitted',call)
        self.error('forbidden',lambda:Catalogue(self.store).preview_snapshot(oldcap,u['fileId']))
        view=Catalogue(self.store).draft(d['id'],d['capability']);self.assertIsNone(view['files'][0]['sourceCapability']);self.assertFalse(view['files'][0]['previewAvailable'])
        self.assertEqual(self.store.list_drafts(destination_id='officer',scope_id='fictional-room')['items'],[])
        creation_key=self.store.db.execute("SELECT key FROM seen WHERE kind='draft' AND target=?",(d['id'],)).fetchone()[0]
        replay=self.store.create_draft(self.binding,creation_key)
        self.assertTrue(replay['authorityChanged']);self.assertNotIn('writerNonce',replay);self.assertNotIn('capability',replay)
        self.error('forbidden',lambda:RequestLedger(self.store).begin_saved_cleanup(message['id'],'a'*64,1))

    def test_consumed_draft_cannot_read_bytes(self):
        d,u=self.draft();self.admit(d)
        self.error('draft_admitted',lambda:self.store.read_text(d['id'],d['capability'],u['fileId']))

    def test_database_rejects_second_message_for_same_draft(self):
        d,u=self.draft();self.admit(d)
        columns=[row[1] for row in self.store.db.execute('PRAGMA table_info(officer_attachment_messages)')]
        projected=["'"+'f'*32+"'" if name=='id' else name for name in columns]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('INSERT INTO officer_attachment_messages('+','.join(columns)+') SELECT '+','.join(projected)+' FROM officer_attachment_messages')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)

    def test_native_preparation_fence_and_database_admission_constraints(self):
        d,u=self.draft();self.admit(d)
        profile=TransportProfile('fictional-profile','v1','codex','fictional-v1','b'*64,'e'*64,'fictional-proof',('text/plain',))
        self.error('draft_admitted',lambda:RequestLedger(self.store).prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile))
        fresh,u=self.draft()
        for update,args in (("admitted_kind='native'",()),("admitted_request=?",('0'*32,)),("admitted_kind='officer',admitted_message=?",('0'*32,))):
            with self.assertRaises(sqlite3.IntegrityError):self.store.db.execute('UPDATE drafts SET '+update+' WHERE id=?',args+(fresh['id'],))
        native,u=self.draft(binding=BINDING,message='Review fictional note');self.native(native)
        self.assertEqual(self.store.admission_kind(self.store._draft(native['id'])),'native')
        self.error('draft_admitted',lambda:self.admit(native))

    def test_source_change_after_read_aborts_without_rows_or_fences(self):
        d,u=self.draft();before=self.store.storage()['usedBytes']
        def changing(point):
            if point=='officer_sources_verified':
                with self.store._gate(),self.store._transaction():self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
        self.store.checkpoint=changing
        self.error('source_changed',lambda:self.admit(d))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)
        self.assertIsNone(self.store.admission_kind(self.store._draft(d['id'])));self.assertEqual(self.store.storage()['usedBytes'],before)
        self.assertFalse(self.store.request_handles)

    def test_takeover_after_read_aborts(self):
        d,u=self.draft()
        def takeover(point):
            if point=='officer_sources_verified':self.store.resume_draft(d['id'],d['capability'],d['revision'],d['writerGeneration'])
        self.store.checkpoint=takeover
        self.error('forbidden',lambda:self.admit(d));self.assertFalse(self.store.request_handles)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)

    def test_crash_before_commit_rolls_back_and_after_commit_replays(self):
        d,u=self.draft();key=self.key()
        def failing(point):
            if point=='officer_admission_before_commit':raise RuntimeError('fictional crash')
        self.store.checkpoint=failing
        with self.assertRaises(RuntimeError):self.admit(d,key=key)
        self.assertIsNone(self.store.admission_kind(self.store._draft(d['id'])))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)
        def after(point):
            if point=='officer_admission_committed':raise RuntimeError('fictional lost reply')
        self.store.checkpoint=after
        with self.assertRaises(RuntimeError):self.admit(d,key=key)
        self.store.close();self.store=self.open()
        self.assertEqual(self.admit(d,key=key)['state'],'retained_local')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)

    def test_same_key_concurrency_is_one_local_message(self):
        d,u=self.draft();key=self.key();barrier=threading.Barrier(2);results=[]
        def run():
            try:barrier.wait(2);results.append(self.admit(d,key=key)['id'])
            except BaseException as exc:results.append(exc)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(4);self.assertFalse(t.is_alive())
        self.assertEqual(len(results),2)
        successes=[r for r in results if type(r)is str]
        self.assertTrue(successes,results);self.assertEqual(len(set(successes)),1)
        for result in results:
            if isinstance(result,AttachmentError):self.assertEqual(result.code,'resource_unavailable')
            else:self.assertIs(type(result),str)
        # Concurrent first lock-file creation may fail closed on this host. An
        # explicit exact retry must still recover the one committed admission.
        self.assertEqual(self.admit(d,key=key)['id'],successes[0])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_message_files').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM conversation_files').fetchone()[0],1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM seen WHERE kind='officer_attachment_admission'").fetchone()[0],1)
        self.assertEqual(self.store.storage()['usedBytes'],len(b'Fictional retained note'))
        self.assertEqual(self.store.storage()['activeDrafts'],0)

    def test_private_read_tamper_and_read_error_release_lifetime_guard(self):
        d,u=self.draft();real=os.read;seen=[]
        def reading(fd,count):self.error('upload_busy',self.store.close);seen.append(fd);return real(fd,count)
        with mock.patch('attachments_files.os.read',side_effect=reading):self.admit(d)
        self.assertTrue(seen);self.assertFalse(self.store.request_handles)
        d,u=self.draft();(self.root/'attachments'/'objects'/u['fileId']).write_bytes(b'tampered')
        self.error('source_changed',lambda:self.admit(d));self.assertFalse(self.store.request_handles)
        d,u=self.draft()
        with mock.patch.object(self.store.files,'read',side_effect=OSError('fictional read failure')):self.error('source_unavailable',lambda:self.admit(d))
        self.assertFalse(self.store.request_handles)

    def interleaving(self,kind):
        binding=self.binding if kind=='officer' else BINDING
        d,a=self.draft(body=b'AAA',binding=binding,message='Retain fictional B');b=self.upload(d,b'BBBBB')
        cap=next(f['sourceCapability'] for f in Catalogue(self.store).draft(d['id'],d['capability'])['files'] if f['id']==a['fileId'])
        inode=(self.root/'attachments'/'objects'/a['fileId']).stat().st_ino
        opened=threading.Event();release=threading.Event();real=os.read;errors=[]
        def hold(fd,count):
            if os.fstat(fd).st_ino==inode and not opened.is_set():opened.set();self.assertTrue(release.wait(4))
            return real(fd,count)
        def read():
            try:Catalogue(self.store).preview_snapshot(cap,a['fileId'])
            except BaseException as exc:errors.append(exc)
        with mock.patch('attachments_files.os.read',side_effect=hold):
            t=threading.Thread(target=read);t.start();self.assertTrue(opened.wait(2))
            try:
                removed=self.store.remove_file(d['id'],d['capability'],fence(d),a['fileId']);d['revision']=removed['revision']
                self.assertEqual(self.store.storage()['usedBytes'],8)
                self.assertTrue((self.root/'attachments'/'objects'/a['fileId']).exists())
                if kind=='officer':self.admit(d)
                else:self.native(d)
                self.assertFalse(self.store._source_admitted(a['fileId']));self.assertTrue(self.store._source_admitted(b['fileId']))
                self.store.reconcile(100)
                self.assertFalse(self.store.db.execute('SELECT released FROM reservations WHERE file_id=?',(a['fileId'],)).fetchone()[0])
            finally:release.set();t.join(4)
        self.assertFalse(t.is_alive());self.assertFalse(errors,errors)
        self.store.reconcile(100)
        self.assertFalse((self.root/'attachments'/'objects'/a['fileId']).exists());self.assertTrue((self.root/'attachments'/'objects'/b['fileId']).exists())
        used=5 if kind=='officer' else 10
        self.assertEqual(self.store.storage()['usedBytes'],used);self.store.reconcile(100);self.assertEqual(self.store.storage()['usedBytes'],used)
        table='officer_message_files' if kind=='officer' else 'request_files'
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table+' WHERE source_id=?',(a['fileId'],)).fetchone()[0],0)
    def test_officer_excluded_nonmember_cleanup_after_actual_descriptor_closes(self):self.interleaving('officer')
    def test_native_excluded_nonmember_cleanup_after_actual_descriptor_closes(self):self.interleaving('native')

    def test_abandoned_native_intent_does_not_protect_removed_source(self):
        d,u=self.draft(binding=BINDING,message='Review fictional note');ledger=RequestLedger(self.store)
        profile=TransportProfile('fictional','v1','codex',BINDING['capabilityRevision'],'b'*64,'e'*64,'fictional-proof',('text/plain',))
        ledger.prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile)
        self.assertFalse(self.store._source_admitted(u['fileId']))
        self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId'])
        self.assertEqual(self.store.storage()['usedBytes'],0)

    def test_generic_cleanup_cannot_delete_exact_admitted_source(self):
        d,u=self.draft();self.admit(d);used=self.store.storage()['usedBytes']
        # A later authoritative deletion worker owns admitted sources. Stale
        # generic cleanup flags must not make old unsent cleanup consume them.
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute("UPDATE files SET member=0,needs_cleanup=1,state='cleanup_pending' WHERE id=?",(u['fileId'],))
            self.store._cleanup(u['fileId'])
        self.store.reconcile(1)
        self.assertTrue((self.root/'attachments'/'objects'/u['fileId']).exists())
        self.assertEqual(self.store.storage()['usedBytes'],used)
        self.assertFalse(self.store.db.execute('SELECT released FROM reservations WHERE file_id=?',(u['fileId'],)).fetchone()[0])

    def test_conversation_bytes_can_exceed_single_message_budget_without_eviction(self):
        for _ in range(3):
            d,u=self.draft(body=b'x'*(8*1024*1024));self.admit(d)
        context=self.officer.context_sources(self.selection)
        self.assertEqual(context['availableCount'],3)
        self.assertEqual(context['availableBytes'],24*1024*1024)
        self.assertEqual(len(context['dependencies']),3)
        self.assertEqual(self.store.storage()['usedBytes'],24*1024*1024)

    def test_retention_survives_model_change_restart_day31_and_view_reset(self):
        d,u=self.draft();first=self.admit(d)
        second_binding=dict(self.binding,modelId='fictional-careful');d2,u2=self.draft(binding=second_binding);self.admit(d2)
        self.clock[0]+=31*86400;self.store.close();self.store=self.open()
        self.assertEqual(self.officer.conversation_files(self.selection)['associationCount'],2)
        self.assertEqual(self.officer.message(self.selection,first['id'])['modelId'],'fictional-fast')
        self.assertEqual(self.officer.context_sources(self.selection)['availableCount'],2)
        self.assertEqual(self.store.storage()['activeDrafts'],0)
        self.assertTrue(all(r[0] is None for r in self.store.db.execute('SELECT expires_at FROM files')))

    def test_two_conversations_and_wrong_scope_are_isolated(self):
        d,u=self.draft();m=self.admit(d);othercid=self.store.create_conversation('fictional-room',self.key())['id']
        other=dict(self.binding,conversationId=othercid);d2,u2=self.draft(binding=other);self.admit(d2)
        selected=HistorySelection.from_binding(other)
        self.assertEqual(self.officer.context_sources(self.selection)['items'][0]['id'],u['fileId'])
        self.assertEqual(self.officer.context_sources(selected)['items'][0]['id'],u2['fileId'])
        self.error('forbidden',lambda:self.officer.message(selected,m['id']))
        self.error('forbidden',lambda:self.officer.context_sources(replace(self.selection,scope_id='fictional-other')))

    def test_51_members_paginate_beyond_per_message_limits(self):
        for _ in range(51):d,u=self.draft();self.admit(d)
        first=self.officer.conversation_files(self.selection);second=self.officer.conversation_files(self.selection,cursor=first['nextCursor'])
        self.assertEqual((len(first['items']),len(second['items'])),(50,1));self.assertEqual(first['associationCount'],51);self.assertEqual(second['availableCount'],51)
        context=self.officer.context_sources(self.selection);tail=self.officer.context_sources(self.selection,cursor=context['nextCursor'])
        self.assertEqual(len(context['dependencies']),50);self.assertEqual(len(tail['dependencies']),1)
        self.assertEqual(len({x.source_id for x in context['dependencies']+tail['dependencies']}),51)
        self.assertEqual(context['availableBytes'],51*len(b'Fictional retained note'))
        self.error('invalid_cursor',lambda:self.officer.context_sources(self.selection,cursor=first['nextCursor']))
        self.error('invalid_cursor',lambda:self.officer.conversation_files(self.selection,cursor=first['nextCursor']+'x'))
        self.error('invalid_dependencies',lambda:self.officer.validate_dependencies(self.selection,context['dependencies']+tail['dependencies']))

    def test_dependency_valid_subset_and_duplicates_stale_or_cross_context_reject(self):
        d,u=self.draft();u2=self.upload(d,b'second');self.admit(d);deps=self.officer.context_sources(self.selection)['dependencies']
        self.assertTrue(self.officer.validate_dependencies(self.selection,deps[:1]));self.assertTrue(self.officer.validate_dependencies(self.selection,()))
        self.error('invalid_dependencies',lambda:self.officer.validate_dependencies(self.selection,(deps[0],deps[0])))
        self.error('invalid_dependencies',lambda:self.officer.validate_dependencies(self.selection,({},)))
        self.error('invalid_dependencies',lambda:self.officer.validate_dependencies(self.selection,(replace(deps[0],source_generation=True),)))
        self.error('source_changed',lambda:self.officer.validate_dependencies(self.selection,(replace(deps[0],scope_id='fictional-other'),)))
        with self.store._gate(),self.store._transaction():self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
        self.error('source_changed',lambda:self.officer.validate_dependencies(self.selection,deps))

    def test_revocation_is_atomic_idempotent_preserves_history_and_no_quota_release(self):
        d,u=self.draft();self.upload(d,b'second');message=self.admit(d);deps=self.officer.context_sources(self.selection)['dependencies'];used=self.store.storage()['usedBytes']
        self.error('transaction_required',lambda:self.officer.invalidate_source_in_transaction(u['fileId']))
        with self.store._gate(),self.store._transaction(reclaim=True):self.error('revocation_required',lambda:self.officer.invalidate_source_in_transaction(u['fileId']))
        affected=self.revoke(u['fileId']);self.assertEqual(affected,[{'messageId':message['id'],'conversationId':self.cid}])
        with self.store._gate(),self.store._transaction(reclaim=True):self.assertEqual(self.officer.invalidate_source_in_transaction(u['fileId']),affected)
        self.assertEqual(self.store.db.execute('SELECT publication_generation FROM officer_attachment_messages').fetchone()[0],'0000000000000002')
        sibling=next(dep for dep in deps if dep.source_id != u['fileId'])
        self.error('source_changed',lambda:self.officer.validate_dependencies(self.selection,(sibling,))) # same-message publication generation
        view=self.officer.conversation_files(self.selection);self.assertEqual(view['associationCount'],2);self.assertEqual(view['availableCount'],1)
        self.assertEqual(self.store.storage()['usedBytes'],used)
        fresh=self.officer.context_sources(self.selection)['dependencies'];self.assertTrue(self.officer.validate_dependencies(self.selection,fresh))
        self.assertFalse(self.officer.message(self.selection,message['id'])['modelCallStarted'])

    def test_revocation_transaction_rollback_restores_every_dependency(self):
        d,u=self.draft();self.admit(d);deps=self.officer.context_sources(self.selection)['dependencies']
        with self.assertRaises(RuntimeError):
            with self.store._gate(),self.store._transaction(reclaim=True):
                self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
                self.officer.invalidate_source_in_transaction(u['fileId']);raise RuntimeError('fictional rollback')
        self.assertTrue(self.officer.validate_dependencies(self.selection,deps))

    def test_revoked_source_without_officer_membership_is_empty_not_a_native_action(self):
        d,u=self.draft(binding=BINDING,message='Review fictional note');ledger,r=self.native(d)
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
            self.assertEqual(self.officer.invalidate_source_in_transaction(u['fileId']),[])
        self.assertEqual(ledger.request_status(r['id'],r['capability'])['state'],'send_authorized')
        self.assertGreater(self.store.storage()['usedBytes'],0)

    def test_owner_epoch_rotation_revokes_old_deps_but_fresh_management_survives(self):
        d,u=self.draft();key=self.key();m=self.admit(d,key=key);deps=self.officer.context_sources(self.selection)['dependencies']
        self.store.close();self.store=AttachmentStore(self.root,enabled=True,owner_epoch='f'*64,clock=lambda:self.clock[0])
        self.error('source_changed',lambda:self.officer.validate_dependencies(self.selection,deps))
        self.error('forbidden',lambda:self.admit(d,key=key))
        self.assertEqual(self.officer.message(self.selection,m['id'])['id'],m['id'])
        fresh=self.officer.context_sources(self.selection)['dependencies'];self.assertTrue(self.officer.validate_dependencies(self.selection,fresh))

    def test_row_capacity_refuses_without_fencing_or_pruning(self):
        self.store.limits['officer_messages']=1
        d,u=self.draft();self.admit(d);d2,u2=self.draft();used=self.store.storage()['usedBytes']
        self.error('message_capacity',lambda:self.admit(d2))
        self.assertIsNone(self.store.admission_kind(self.store._draft(d2['id'])))
        self.assertEqual(self.store.storage()['usedBytes'],used);self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)

    def test_actual_sqlite_saturation_preserves_all_revocations_and_retries(self):
        self.store.close();self.store=self.open(overrides={'database_bytes':524288})
        admitted=[];failed=False
        for _ in range(1000):
            try:
                d,u=self.draft();key=self.key();m=self.admit(d,key=key);admitted.append((d,u,key,m['id']))
            except AttachmentError as exc:
                self.assertIn(exc.code,('storage_unavailable','history_capacity'));failed=True;break
        self.assertTrue(failed);self.assertGreater(len(admitted),1);self.assertLess(len(admitted),1000)
        self.assertGreater(self.store.db.execute('PRAGMA page_count').fetchone()[0],30)
        used=self.store.storage()['usedBytes']
        for d,u,key,mid in admitted:self.revoke(u['fileId'])
        self.assertEqual(self.store.storage()['usedBytes'],used)
        self.store.close();self.store=self.open(overrides={'database_bytes':524288})
        for d,u,key,mid in admitted:
            result=self.admit(d,key=key);self.assertEqual(result['id'],mid);self.assertFalse(result['files'][0]['available']);self.assertFalse(result['modelCallStarted'])
            self.assertEqual(self.store.admission_kind(self.store._draft(d['id'])),'officer')
        self.assertEqual(self.officer.context_sources(self.selection)['availableCount'],0)
        self.assertEqual(self.officer.conversation_files(self.selection)['associationCount'],len(admitted))

    def test_schema5_migration_preserves_native_admission_and_creates_no_officer_rows(self):
        d,u=self.draft(binding=BINDING,message='Review fictional note');ledger,r=self.native(d)
        with self.store._gate(),self.store._transaction(reclaim=True):drop_officer_schema(self.store.db);self.store.db.execute('PRAGMA user_version=5')
        self.store.close();self.store=self.open()
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.store.admission_kind(self.store._draft(d['id'])),'native')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],0)
        self.assertEqual(RequestLedger(self.store).request_status(r['id'],r['capability'])['state'],'send_authorized')

    def test_dangling_schema5_native_reference_fails_closed(self):
        d,u=self.draft()
        with self.store._gate(),self.store._transaction(reclaim=True):
            drop_officer_schema(self.store.db);self.store.db.execute('UPDATE drafts SET admitted_request=? WHERE id=?',('0'*32,d['id']));self.store.db.execute('PRAGMA user_version=5')
        self.store.close();self.error('schema_unavailable',lambda:self.open())

    def test_startup_rejects_receiving_admitted_source_without_inventing_progress(self):
        d,u=self.draft();self.admit(d)
        self.store.db.execute("UPDATE files SET state='receiving' WHERE id=?",(u['fileId'],))
        self.store.close();self.error('storage_unavailable',lambda:self.open())

    def test_disabled_demo_create_no_retention_storage_or_routes(self):
        for enabled,demo in ((False,False),(True,True)):
            root=self.root/('off' if not enabled else 'demo');store=AttachmentStore(root,enabled=enabled,demo=demo)
            self.error('attachments_disabled',lambda:OfficerConversations(store).context_sources(self.selection))
            self.assertFalse(root.exists());store.close()
        from attachments_service import AttachmentService,UnqualifiedResolver
        service=AttachmentService(self.store,resolver=UnqualifiedResolver())
        self.assertFalse(service.capabilities('officer','fictional-room','fictional-fast')['available'])
        self.assertFalse(hasattr(service,'admit_officer_message'))


if __name__=='__main__':unittest.main()
