"""Officer original storage and real SQLite migrations. Fictional bytes; no calls."""
from contextlib import closing
from dataclasses import asdict, replace
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_officer_storage import OfficerDirectRetentionProfile, prepare_original
from attachments_conversations import OfficerConversations, OfficerRetentionProfile
from attachments_catalogue import Catalogue, HistorySelection
from attachments_requests import RequestLedger, TransportProfile, PhaseVerification, VerifiedCopy
from attachments_types import AttachmentError, canonical, digest
import attachments_officer_migration as migration
import attachments_deletion_slots as slots
import test_attachments_conversations as cf
from test_attachments_foundation import EPOCH, BINDING, fence, metadata
from test_attachments_requests import FakeLease
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root
import private_storage

PDF = b'%PDF-1.7\nfictional original; no parsed content asserted'
PNG = b'\x89PNG\r\n\x1a\nfictional original; no pixels decoded'


class PreparationTests(unittest.TestCase):
    def test_distinct_bounded_metadata_is_not_a_parser_or_reading_receipt(self):
        for raw, media in ((PDF,'application/pdf'),(PNG,'image/png')):
            result = prepare_original(raw, media, max_bytes=1024)
            self.assertEqual(result, dict(kind='officer_direct', mediaType=media, bytes=len(raw),
                                         sha256=hashlib.sha256(raw).hexdigest(), previewAvailable=False))
            self.assertNotIn('text',result);self.assertNotIn('pageCount',result);self.assertNotIn('readable',result)
        for raw, media, limit, code in ((b'no','application/pdf',100,'type_signature_mismatch'),
                (PDF,'application/pdf',1,'file_too_large'),(PNG,'image/jpeg',1024,'preparation_unavailable'),
                (PDF,'text/plain',1024,'preparation_unavailable'),(PDF,'application/pdf',True,'preparation_unavailable')):
            with self.subTest(code=code),self.assertRaises(AttachmentError) as caught:
                prepare_original(raw,media,max_bytes=limit)
            self.assertEqual(caught.exception.code,code)

    def test_schema_normalization_preserves_all_quoted_literal_bytes(self):
        self.assertEqual(migration.normalized(' CREATE  TABLE "files" ( id TEXT ) '),
                         migration.normalized('create table files(id text)'))
        for literal in ("'TEXT'", "'t e x t'", "'te''XT'", "'te\nt'", '"TEXT"', '`TEXT`', '[TEXT]'):
            with self.subTest(literal=literal):
                self.assertEqual(migration.normalized(' CHECK ( value = '+literal+' ) '),
                                 'check(value='+literal+')')
        self.assertNotEqual(migration.normalized("CHECK(value='text')"),
                            migration.normalized("CHECK(value='TEXT')"))

    def test_profile_is_closed_local_only_and_keeps_old_text_shape(self):
        p = OfficerDirectRetentionProfile('fictional-direct','v1','local_retention','fictional-v1','fictional-fast')
        for changes in ({'purpose':'reading'},{'model_id':''},{'media_types':('image/jpeg',)},
                        {'media_types':['application/pdf']},{'file_count':6},{'file_bytes':True}):
            with self.subTest(changes=changes),self.assertRaises(AttachmentError):replace(p,**changes)
        self.assertEqual(set(asdict(OfficerRetentionProfile('fictional-text','v1','local_retention','fictional-v1'))),
                         {'id','revision','purpose','capability_revision','media_types','file_count','file_bytes','draft_bytes'})


@unittest.skipUnless(STORE, STORE_WHY)
class DirectStoreTests(unittest.TestCase):
    for _name in ('setUp','tearDown','open','key','error','revoke','officer','selection'):
        locals()[_name] = getattr(cf.ConversationTests,_name)
    del _name

    def direct_profile(self):
        return OfficerDirectRetentionProfile('fictional-direct','v1','local_retention',self.binding['capabilityRevision'],self.binding['modelId'])

    def draft(self,binding=None):return self.store.create_draft(binding or self.binding,self.key())

    def reserve(self,d,raw=PDF,media='application/pdf',key=None,profile=None,mode='officer_direct'):
        return self.store.reserve_upload(d['id'],d['capability'],fence(d),dict(metadata(raw),mediaType=media,name='fictional-source'),key or self.key(),
                                        preparation_kind=mode,officer_profile=profile or self.direct_profile())

    def upload(self,d,raw=PDF,media='application/pdf'):
        u=self.reserve(d,raw,media);d['revision']=u['draftRevision']
        out=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(raw))
        d['revision']=out['draftRevision'];return u,out

    def admit(self,d,key=None,profile=None):
        return self.officer.admit(d['id'],d['capability'],fence(d),key or self.key(),d['destination'],profile or self.direct_profile())

    def test_pdf_png_upload_retention_unchanged_no_model_or_workspace_claim(self):
        d=self.draft();items=[self.upload(d),self.upload(d,PNG,'image/png')]
        used=self.store.storage()['usedBytes'];out=self.admit(d)
        self.assertEqual(out['state'],'retained_local');self.assertFalse(out['modelCallStarted']);self.assertTrue(out['retainedLocally'])
        self.assertEqual([f['preparationKind'] for f in out['files']],['officer_direct']*2)
        self.assertTrue(all(f['available'] and not f['previewAvailable'] for f in out['files']))
        for (u,_),raw in zip(items,(PDF,PNG)):
            row=self.store.db.execute('SELECT * FROM files WHERE id=?',(u['fileId'],)).fetchone()
            self.assertEqual(self.store.files.read(row['id'],json.loads(row['identity']),row['digest'],1024),raw)
            self.assertIsNone(row['expires_at']);slots.verify_source(self.store,row['id'])
        self.assertEqual(self.store.storage()['usedBytes'],used)
        for table in ('attachment_requests','workspace_copies','attachment_outbox'):
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_profile_target_type_limit_and_retry_fingerprints(self):
        d=self.draft();key=self.key();u=self.reserve(d,key=key)
        self.assertEqual(self.reserve(d,key=key),u)
        d['revision']=u['draftRevision']
        self.error('request_conflict',lambda:self.reserve(d,key=key,profile=replace(self.direct_profile(),revision='v2')))
        self.error('capability_changed',lambda:self.reserve(d,profile=replace(self.direct_profile(),model_id='fictional-other')))
        self.error('capability_changed',lambda:self.reserve(d,profile=replace(self.direct_profile(),capability_revision='changed')))
        self.error('capability_changed',lambda:self.reserve(self.draft(BINDING)))
        self.error('preparation_unavailable',lambda:self.reserve(d,b'\xff\xd8\xfffake','image/jpeg'))
        self.error('profile_unavailable',lambda:self.reserve(d,profile=object()))
        self.error('profile_unavailable',lambda:self.reserve(d,mode='text'))
        self.error('draft_limit',lambda:self.reserve(d,profile=replace(self.direct_profile(),file_bytes=1)))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],1)

    def test_opaque_native_relay_and_plain_text_cannot_adopt_direct_admission(self):
        d=self.draft();u,out=self.upload(d)
        self.error('source_unavailable',lambda:self.admit(d,profile=self.profile))
        self.error('capability_changed',lambda:self.admit(d,profile=replace(self.direct_profile(),model_id='fictional-other')))
        self.store.db.execute("UPDATE files SET preparation_kind='original_relay' WHERE id=?",(u['fileId'],))
        self.error('source_unavailable',lambda:self.admit(d))
        self.assertIsNone(self.store.admission_kind(self.store._draft(d['id'])))

    def test_native_ledger_rejects_direct_even_if_database_target_is_changed(self):
        d=self.draft();u,_=self.upload(d)
        self.store.db.execute('UPDATE drafts SET destination=? WHERE id=?',(canonical(BINDING),d['id']))
        self.store.edit_text(d['id'],d['capability'],fence(d),'Fictional review')
        d['revision']+=1
        profile=TransportProfile('fictional-native','v1','codex','fictional-v1','b'*64,'e'*64,'fictional-proof',('application/pdf',),original_relay_formats=('application/pdf',))
        self.error('source_unavailable',lambda:RequestLedger(self.store).prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile))

    def test_invalid_signature_stays_failed_and_retry_has_no_parser(self):
        d=self.draft();u,out=self.upload(d,b'not a PDF')
        self.assertEqual(out['state'],'failed');self.assertEqual(out['error'],'type_signature_mismatch')
        with mock.patch('attachments_store.prepare',side_effect=AssertionError('no parser')):
            result=self.store.retry_preparation(d['id'],d['capability'],fence(d),u['fileId'])
        self.assertEqual(result['files'][0]['state'],'failed');d['revision']=result['revision']
        self.error('source_unavailable',lambda:self.admit(d))

    def test_both_preview_paths_refuse_before_reading_original(self):
        d=self.draft();u,_=self.upload(d);cap=Catalogue(self.store).draft(d['id'],d['capability'])['files'][0]['sourceCapability']
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('no binary preview read')):
            self.error('preview_unavailable',lambda:self.store.read_text(d['id'],d['capability'],u['fileId']))
            snap=Catalogue(self.store).preview_snapshot(cap,u['fileId'])
            self.assertFalse(snap.read_bytes)
            self.assertFalse(json.loads(snap.payload)['available'])

    def test_exact_retry_restart_day31_and_model_change_keep_membership(self):
        d=self.draft();u,_=self.upload(d);key=self.key();first=self.admit(d,key)
        deps=self.officer.context_sources(self.selection)['dependencies']
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('no duplicate read')):
            self.assertEqual(self.admit(d,key),first)
        self.clock[0]+=31*86400;self.store.close();self.store=self.open()
        other=dict(self.binding,modelId='fictional-other',capabilityRevision='different')
        other_draft=self.draft(other)
        self.assertEqual(self.officer.message(self.selection,first['id'])['files'][0]['id'],u['fileId'])
        self.assertTrue(self.officer.validate_dependencies(self.selection,deps))
        self.assertEqual(self.admit(d,key)['id'],first['id']);self.assertFalse(self.admit(d,key)['modelCallStarted'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM conversation_files').fetchone()[0],1)

    def test_complete_pagination_exceeds_five_lifetime_files_and_revoke_uncited_member(self):
        sources=[]
        for _ in range(11):
            d=self.draft()
            for _ in range(5):sources.append(self.upload(d)[0]['fileId'])
            self.admit(d)
        page=self.officer.context_sources(self.selection)
        self.assertEqual(page['availableCount'],55);self.assertEqual(len(page['dependencies']),50)
        tail=self.officer.context_sources(self.selection,cursor=page['nextCursor'])
        self.assertEqual(len(tail['dependencies']),5);self.assertIsNone(tail['nextCursor'])
        deps=tuple(page['dependencies'])+tuple(tail['dependencies'])
        self.error('invalid_dependencies',lambda:self.officer.validate_dependencies(self.selection,deps))
        victim=page['dependencies'][0];self.revoke(victim.source_id)
        self.error('source_changed',lambda:self.officer.validate_dependencies(self.selection,(victim,)))
        sibling=next(d for d in deps if d.message_id!=victim.message_id)
        self.assertTrue(self.officer.validate_dependencies(self.selection,(sibling,)))
        self.assertEqual(self.officer.context_sources(self.selection)['availableCount'],54)

    def test_changed_source_metadata_and_actual_bytes_fail_admission(self):
        d=self.draft();u,_=self.upload(d);path=self.root/'attachments'/'objects'/u['fileId'];path.write_bytes(b'changed')
        self.error('source_changed',lambda:self.admit(d))
        self.assertIsNone(self.store.admission_kind(self.store._draft(d['id'])))
        d=self.draft();u,_=self.upload(d)
        def change(point):
            if point=='officer_sources_verified':
                self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
        self.store.checkpoint=change
        self.error('source_changed',lambda:self.admit(d));self.assertFalse(self.store.request_handles)

    def test_concurrent_exact_retry_is_at_most_one_retained_message(self):
        d=self.draft();self.upload(d);key=self.key();barrier=threading.Barrier(2);results=[]
        def run():
            try:barrier.wait(2);results.append(self.admit(d,key)['id'])
            except AttachmentError as exc:results.append(exc.code)
        ts=[threading.Thread(target=run) for _ in range(2)]
        for t in ts:t.start()
        for t in ts:t.join(5);self.assertFalse(t.is_alive())
        winner=self.admit(d,key)['id']
        self.assertIn(winner,results);self.assertTrue(all(r in (winner,'resource_unavailable') for r in results))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0],1)

    def test_api_metadata_saturation_preserves_direct_membership_revocation_and_retry(self):
        self.store.close();self.store=self.open(overrides={'database_bytes':524288});admitted=[]
        for _ in range(1000):
            try:
                d=self.draft();u,_=self.upload(d);key=self.key();m=self.admit(d,key);admitted.append((d,u,key,m['id']))
            except AttachmentError as exc:
                self.assertIn(exc.code,('history_capacity','storage_unavailable'));break
        else:self.fail('capacity was not reached')
        self.assertGreater(len(admitted),1);used=self.store.storage()['usedBytes']
        for d,u,key,mid in admitted:self.revoke(u['fileId'])
        self.assertEqual(self.store.storage()['usedBytes'],used)
        self.store.close();self.store=self.open(overrides={'database_bytes':524288})
        for d,u,key,mid in admitted:
            out=self.admit(d,key);self.assertEqual(out['id'],mid);self.assertFalse(out['files'][0]['available'])
        slots.verify_all(self.store)

    def test_actual_open_removed_nonmember_does_not_strand_after_sibling_admission(self):
        d=self.draft();a,_=self.upload(d);b,_=self.upload(d,PNG,'image/png')
        row=self.store.db.execute('SELECT * FROM files WHERE id=?',(a['fileId'],)).fetchone()
        identity=json.loads(row['identity']);real=os.read;entered=[];used=self.store.storage()['usedBytes']
        def reading(fd,count):
            if not entered and tuple(private_storage.file_identity(fd)[:2])==tuple(identity[:2]):   # as the store records it
                entered.append(True)
                self.error('upload_busy',self.store.close)
                d['revision']=self.store.remove_file(d['id'],d['capability'],fence(d),a['fileId'])['revision']
                self.assertEqual(self.store.storage()['usedBytes'],used)
                result=self.admit(d);self.assertEqual([x['id'] for x in result['files']],[b['fileId']])
                self.assertTrue((self.root/'attachments'/'objects'/a['fileId']).exists())
            return real(fd,count)
        with mock.patch('attachments_files.os.read',side_effect=reading):
            self.assertEqual(self.store.files.read(a['fileId'],identity,row['digest'],1024),PDF)
        self.assertTrue(entered);self.store.reconcile(maximum=10)
        self.assertFalse((self.root/'attachments'/'objects'/a['fileId']).exists())
        self.assertTrue((self.root/'attachments'/'objects'/b['fileId']).exists())
        self.assertEqual(self.store.storage()['usedBytes'],len(PNG));self.store.reconcile(maximum=10)
        self.assertEqual(self.store.storage()['usedBytes'],len(PNG));self.assertFalse(self.store.request_handles)

    def test_retained_direct_draft_fences_every_old_writer(self):
        d=self.draft();u,_=self.upload(d);self.admit(d)
        for action in (lambda:self.reserve(d),lambda:self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId']),
                       lambda:self.store.edit_text(d['id'],d['capability'],fence(d),'later'),
                       lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),self.key()),
                       lambda:self.store.retry_preparation(d['id'],d['capability'],fence(d),u['fileId']),
                       lambda:self.store.recover_draft(d['id'],d['writerGeneration'])):
            self.error('draft_admitted',action)

    def test_disabled_demo_and_unqualified_service_remain_closed(self):
        for enabled,demo in ((False,False),(True,True)):
            root=self.root/str(demo);store=AttachmentStore(root,enabled=enabled,demo=demo)
            self.assertFalse(root.exists());store.close()
        from attachments_service import AttachmentService,UnqualifiedResolver
        result=AttachmentService(self.store,resolver=UnqualifiedResolver()).capabilities('officer','fictional-room','fictional-fast')
        self.assertFalse(result['available'])


@unittest.skipUnless(STORE, STORE_WHY)
class MigrationTests(unittest.TestCase):
    for _name in ('tearDown','open','key','error','officer','selection','upload','draft','admit','native'):
        locals()[_name] = getattr(cf.ConversationTests,_name)
    del _name

    def setUp(self):
        # Real original schema1..7 creation and public APIs; only the new migration is postponed.
        with mock.patch('attachments_officer_migration.migrate'), mock.patch('officer_document_ledger.migrate'), mock.patch('officer_document_intents.migrate'):
            cf.ConversationTests.setUp(self)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],7)

    def populate(self):
        d,u=self.draft();key=self.key();message=self.admit(d,key=key)
        self.officer_retry=(d,key,message['id'])
        d,u=self.draft(binding=BINDING,message='Review fictional text');ledger,request=self.native(d)
        lease=FakeLease(ledger.resource_keys(request['id']));auth=ledger.authority(request['id'])
        ledger.record_directory_created(auth,'root',(1,100),lease);ledger.record_directory_created(auth,'request',(1,200),lease)
        status=ledger.request_status(request['id'],request['capability']);copy=status['copies'][0]
        intent=ledger.claim_copy(request['id'],status['revision'],copy['id'],lease)
        ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
        status=ledger.request_status(request['id'],request['capability'])
        proof=PhaseVerification(request['id'],request['fingerprint'],status['revision'],auth.context_fingerprint,auth.profile_revision,'publication',
                                ((copy['id'],intent.copy_generation),),intent.lease_id,self.clock[0],self.clock[0]+2)
        ledger.record_copy_result(VerifiedCopy(copy['id'],intent.lease_id,intent.copy_generation,(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,proof),lease)
        self.assertIsNotNone(self.store.db.execute('SELECT catalogued_at FROM request_files WHERE request_id=?',(request['id'],)).fetchone()[0])
        d=self.store.create_draft(BINDING,self.key());d['revision']=self.store.edit_text(d['id'],d['capability'],fence(d),'Review fictional original')['revision']
        u=self.store.reserve_upload(d['id'],d['capability'],fence(d),dict(metadata(PDF),mediaType='application/pdf'),self.key(),preparation_kind='original_relay');d['revision']=u['draftRevision']
        out=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(PDF));d['revision']=out['draftRevision']
        profile=TransportProfile('fictional-native','v1','codex','fictional-v1','b'*64,'e'*64,'fictional-proof',('application/pdf',),original_relay_formats=('application/pdf',))
        RequestLedger(self.store).prepare_request(d['id'],d['capability'],fence(d),self.key(),BINDING,profile)
        d,u=self.draft();self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId'])
        pending=self.store.create_draft(self.binding,self.key())
        self.store.reserve_upload(pending['id'],pending['capability'],fence(pending),metadata(b'pending original text'),self.key())
        self.store.db.execute('UPDATE files SET rowid=rowid+100')
        d=self.store.create_draft(self.binding,self.key());discard_key=self.key()
        result=self.store.discard_draft(d['id'],d['capability'],fence(d),discard_key)
        self.discard_retry=(d,discard_key,result)

    def snapshot(self):
        names=[r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result={n:[tuple(r) for r in self.store.db.execute('SELECT '+('rowid,' if n=='files' else '')+'* FROM '+n+' ORDER BY 1')] for n in names}
        return result

    def test_real_schema7_preserves_all_rows_rowids_refs_slots_and_retry_receipts(self):
        self.populate();before=self.snapshot();triggers=self.store.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        migration.migrate(self.store)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual([tuple(r) for r in self.store.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")],[tuple(r) for r in triggers])
        self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0],1);self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        migration.verify_schema(self.store.db,direct=True);slots.verify_all(self.store)
        d,key,mid=self.officer_retry;self.assertEqual(self.admit(d,key=key)['id'],mid)
        d,key,result=self.discard_retry;self.assertEqual(self.store.discard_draft(d['id'],d['capability'],fence(d),key),result)
        self.store.close();self.store=self.open();self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.admit(self.officer_retry[0],key=self.officer_retry[1])['id'],mid)

    def test_each_structural_failure_rolls_back_and_restores_fk(self):
        self.populate();before=self.snapshot()
        points=['officer_migration_copied_files','officer_migration_copied_officer_message_files',
                'officer_migration_dropped_files','officer_migration_renamed_files',
                'officer_migration_dropped_officer_message_files','officer_migration_renamed_officer_message_files','officer_migration_before_commit']
        for point in points:
            def crash(p):
                if p==point:raise sqlite3.OperationalError('fictional migration failure')
            self.store.checkpoint=crash
            with self.subTest(point=point),self.assertRaises(sqlite3.OperationalError):migration.migrate(self.store)
            self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0],1);self.assertFalse(self.store.db.in_transaction)
            self.assertEqual(self.snapshot(),before);migration.verify_schema(self.store.db,direct=False);slots.verify_all(self.store)
        self.store.checkpoint=lambda _:None;migration.migrate(self.store)

    def test_changed_check_literals_are_rejected_without_migration_mutation(self):
        # Valid empty schema7 tables; no writable_schema or bypassed CHECK writes.
        for table, altered in (('files', 'TEXT'), ('files', 't e x t'),
                               ('officer_message_files', 'TEXT'), ('officer_message_files', 't e x t')):
            sql = migration.files_sql('files', direct=False) if table == 'files' else migration.members_sql('officer_message_files', direct=False)
            changed = sql.replace("'text'", "'"+altered+"'")
            self.store.db.execute('PRAGMA foreign_keys=OFF')
            with self.store._transaction(reclaim=True):
                self.store.db.execute('DROP TABLE '+table)
                self.store.db.execute(changed)
                if table == 'files':
                    for statement in migration.INDEXES.values():self.store.db.execute(statement)
            self.store.db.execute('PRAGMA foreign_keys=ON')
            before=self.snapshot()
            schema=[tuple(r) for r in self.store.db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name')]
            with self.subTest(table=table,literal=altered):
                self.error('schema_unavailable',lambda:migration.migrate(self.store))
                self.assertEqual(self.snapshot(),before)
                self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name')],schema)
                self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],7)
                self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            # Restore only the disposable empty table for the next independent case.
            self.store.db.execute('PRAGMA foreign_keys=OFF')
            with self.store._transaction(reclaim=True):
                self.store.db.execute('DROP TABLE '+table);self.store.db.execute(sql)
                if table == 'files':
                    for statement in migration.INDEXES.values():self.store.db.execute(statement)
            self.store.db.execute('PRAGMA foreign_keys=ON')

    def test_unexpected_schema_and_dangling_reference_fail_without_repair(self):
        self.populate();self.store.db.execute('CREATE INDEX fictional_extra ON files(name)')
        self.error('schema_unavailable',lambda:migration.migrate(self.store));self.store.db.execute('DROP INDEX fictional_extra')
        self.store.db.execute('PRAGMA foreign_keys=OFF');self.store.db.execute("UPDATE uploads SET file_id=? WHERE id=(SELECT id FROM uploads LIMIT 1)",('f'*32,));self.store.db.execute('PRAGMA foreign_keys=ON')
        before=self.snapshot();self.error('schema_unavailable',lambda:migration.migrate(self.store))
        self.assertEqual(self.snapshot(),before);self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],7)

    def test_startup_failure_releases_lock_and_does_not_run_recovery(self):
        self.populate();before=self.snapshot();self.store.close()
        def crash(point):
            if point=='officer_migration_before_commit':raise sqlite3.OperationalError('fictional crash')
        with mock.patch.object(AttachmentStore,'_recover',side_effect=AssertionError('no recovery before migration')):
            self.error('storage_unavailable',lambda:self.open(checkpoint=crash))
        # A fresh old-schema startup proves writer ownership was released on failure.
        with mock.patch('attachments_officer_migration.migrate'), mock.patch('officer_document_ledger.migrate'), mock.patch('officer_document_intents.migrate'):
            self.store=self.open()
        self.assertEqual(self.snapshot(),before)
        self.store.close();self.store=self.open();self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0],1)

    def test_physical_full_migration_refuses_without_cap_raise_and_old_cleanup_works(self):
        # Saturate genuine APIs first, then fill only their intentionally reserved headroom.
        self.store.close()
        with mock.patch('attachments_officer_migration.migrate'), mock.patch('officer_document_ledger.migrate'), mock.patch('officer_document_intents.migrate'):
            self.store=self.open(overrides={'database_bytes':524288})
        admitted=[]
        for _ in range(1000):
            try:
                d=self.store.create_draft(self.binding,self.key());admitted.append(d)
            except AttachmentError as exc:
                self.assertIn(exc.code,('history_capacity','storage_unavailable'));break
        else:self.fail('API capacity was not reached')
        self.assertGreater(len(admitted),1)
        with self.store._gate():
            self.store.db.execute('CREATE TABLE fictional_filler(id INTEGER PRIMARY KEY,payload BLOB)')
            try:
                for _ in range(10000):self.store.db.execute('INSERT INTO fictional_filler(payload) VALUES(zeroblob(3000))')
            except sqlite3.OperationalError as exc:self.assertIn('full',str(exc).lower())
            else:self.fail('physical FULL was not reached')
        before=self.snapshot();maximum=self.store.db.execute('PRAGMA max_page_count').fetchone()[0]
        with self.assertRaises(sqlite3.OperationalError) as caught:migration.migrate(self.store)
        self.assertIn('full',str(caught.exception).lower());self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],7)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('PRAGMA max_page_count').fetchone()[0],maximum)
        keys=[]
        for d in admitted:
            key=self.key();out=self.store.discard_draft(d['id'],d['capability'],fence(d),key);keys.append((d,key,out))
        self.store.close()
        # The old server equivalent remains operable; migration is still not silently attempted.
        with mock.patch('attachments_officer_migration.migrate'), mock.patch('officer_document_ledger.migrate'), mock.patch('officer_document_intents.migrate'):
            self.store=self.open(overrides={'database_bytes':524288})
        for d,key,out in keys:self.assertEqual(self.store.discard_draft(d['id'],d['capability'],fence(d),key),out)
        slots.verify_all(self.store)


if __name__ == '__main__':unittest.main()
