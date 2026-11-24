"""Actual private bytes/ledger; fictional copy attestations, no native/provider calls."""
from attachment_schema_fixtures import drop_officer_schema
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_catalogue import Catalogue, HistorySelection
from attachments_service import AttachmentService, UnqualifiedResolver
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_requests import RequestLedger, VerifiedCopy, CopyCleanup, OwnerReceipt
from attachments_http import handle_http
from attachments_types import AttachmentError
import test_attachments_requests as fixtures
from test_attachments_foundation import BINDING, EPOCH, fence, metadata
from test_attachments_http import Handler, KEY
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


@unittest.skipUnless(STORE, STORE_WHY)
class CatalogueTests(unittest.TestCase):
    # Reuse fixture setup/actions, not another TestCase's test methods/count.
    for _name in ('setUp', 'tearDown', 'open', 'key', 'error', 'draft', 'prepare', 'send',
                  'status', 'directories', 'proof', 'materialized', 'handoff'):
        locals()[_name] = getattr(fixtures.RequestTests, _name)
    del _name

    @property
    def cat(self): return Catalogue(self.store)
    @property
    def selection(self): return HistorySelection.from_binding(BINDING)
    def history(self, **kw): return self.cat.history(self.selection, **kw)
    def cap(self, d, fid):
        return next(f['sourceCapability'] for f in self.cat.draft(d['id'], d['capability'])['files'] if f['id'] == fid)
    def publish(self, snapshot):
        return self.cat.publish_preview(snapshot, lambda value: value)
    def call(self, route, headers=(), method='GET', service=True, defaults=True):
        h = Handler(method, '/api/attachments' + route, extra=headers, defaults=defaults)
        svc = AttachmentService(self.store, resolver=UnqualifiedResolver()) if service else None
        handle_http(h, svc, KEY, 8786)
        return h
    def history_route(self, **kw):
        values = dict(destinationId=BINDING['id'], scopeId=BINDING['scopeId'], conversationId='', **kw)
        return '/files?' + '&'.join(k+'='+v for k,v in values.items())

    def test_history_only_first_full_workspace_publication_and_survives_cleanup(self):
        d, r, lease = self.prepare()
        self.assertEqual(self.history()['associationCount'], 0)
        d2, r2, lease2, intent = self.materialized()
        first = self.history()['items'][0]
        self.assertEqual(first['requestId'], r2['id'])
        self.clock[0] += 31*86400
        self.ledger.begin_saved_cleanup(r2['id'], r2['capability'], self.status(r2)['revision'])
        plan = self.ledger.cleanup_copy(intent.id)
        self.ledger.record_copy_cleanup(CopyCleanup(intent.id, plan['intent'].copy_generation, plan['intent'].lease_id, 'removed', True), lease2)
        out = self.history()
        self.assertEqual(out['associationCount'], 1)
        self.assertEqual(out['items'][0]['cataloguedAt'], first['cataloguedAt'])
        self.assertFalse(out['items'][0]['savedCopyAvailable'])
        self.assertTrue(out['items'][0]['previewAvailable']) # private source survives workspace cleanup
        self.store.close(); self.store = self.open(); self.ledger = RequestLedger(self.store)
        self.assertEqual(self.history()['associationCount'], 1)
        self.assertEqual(self.history()['unclassifiedAssociationCount'], 1)

    def test_two_files_are_two_associations_and_publication_never_relatches(self):
        d,u=self.draft()
        second=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'Second fictional note'),self.key());d['revision']=second['draftRevision']
        done=self.store.receive_upload(d['id'],d['capability'],fence(d),second['id'],second['fingerprint'],io.BytesIO(b'Second fictional note'));d['revision']=done['draftRevision']
        d,r,lease=self.send(d);self.directories(r,lease)
        first_result=None
        for copy in self.status(r)['copies']:
            intent=self.ledger.claim_copy(r['id'],self.status(r)['revision'],copy['id'],lease)
            self.ledger.record_copy_created(copy['id'],intent.lease_id,(1,300,0,1,1),lease)
            result=VerifiedCopy(copy['id'],intent.lease_id,copy['generation'],(1,300,intent.bytes,2,2),intent.sha256,intent.bytes,self.proof(r,intent.lease_id,'publication',[copy]))
            self.ledger.record_copy_result(result,lease)
            first_result=first_result or result
        before=self.history()
        self.assertEqual(before['associationCount'],2)
        self.assertEqual(len(before['items']),2)
        self.clock[0]+=10
        self.error('proof_unavailable',lambda:self.ledger.record_copy_result(first_result,lease))
        self.assertEqual([x['cataloguedAt'] for x in self.history()['items']],[x['cataloguedAt'] for x in before['items']])

    def test_publication_callback_is_transaction_free_and_failure_releases_handles(self):
        d,u=self.draft();snapshot=self.cat.preview_snapshot(self.cap(d,u['fileId']),u['fileId'])
        def publishing(value):
            self.assertFalse(self.store.db.in_transaction)
            self.assertTrue(self.store.request_handles)
            self.error('upload_busy',self.store.close)
            raise RuntimeError('fictional publication failure')
        with self.assertRaises(RuntimeError):self.cat.publish_preview(snapshot,publishing)
        self.assertFalse(self.store.request_handles)

    def test_temporary_and_partial_copies_never_latch_history(self):
        profile = replace(self.profile, temporary_copy=True)
        d,r,lease = self.send(profile=profile); self.directories(r,lease)
        temp = next(c for c in self.status(r)['copies'] if c['role']=='temporary')
        intent = self.ledger.claim_copy(r['id'], self.status(r)['revision'], temp['id'], lease)
        self.ledger.record_copy_created(temp['id'], intent.lease_id, (1,600,0,1,1),lease)
        self.assertEqual(self.history()['associationCount'],0)
        self.ledger.record_copy_result(VerifiedCopy(temp['id'],intent.lease_id,temp['generation'],(1,600,intent.bytes,2,2),intent.sha256,intent.bytes,self.proof(r,intent.lease_id,'publication',[temp])),lease)
        self.assertEqual(self.history()['associationCount'],0)

    def test_history_51_keyset_rows_complete_totals_and_cursor_guards(self):
        for _ in range(51): self.materialized()
        first = self.history(); second = self.history(cursor=first['nextCursor'])
        self.assertEqual((len(first['items']),len(second['items'])),(50,1))
        self.assertEqual((first['associationCount'],second['associationCount']),(51,51))
        self.assertEqual(len({f['requestId'] for f in first['items']+second['items']}),51)
        self.assertIsNone(second['nextCursor'])
        cursor = first['nextCursor']; self.error('invalid_cursor', lambda:self.history(cursor=cursor+'x'))
        other = dict(BINDING, scopeId='fictional-other')
        self.store.create_draft(other,self.key())
        self.error('invalid_cursor', lambda:self.cat.history(HistorySelection.from_binding(other),cursor=cursor))
        self.error('invalid_cursor',lambda:self.cat.storage_files(cursor=cursor))
        self.store.close();self.store=AttachmentStore(self.root,enabled=True,owner_epoch='f'*64,clock=lambda:self.clock[0])
        self.error('invalid_cursor',lambda:self.history(cursor=cursor))

    def test_inventory_full_physical_totals_pages_no_authorities_or_paths(self):
        d,r,lease = self.send(profile=replace(self.profile,temporary_copy=True))
        for _ in range(50):self.draft()
        before=self.store.storage()['usedBytes']
        first=self.cat.storage_files();second=self.cat.storage_files(cursor=first['nextCursor'])
        self.assertEqual((len(first['items']),len(second['items'])),(50,1))
        self.assertEqual(first['sourceCount'],51)
        self.assertEqual(first['usedBytes'],before)
        self.assertEqual(second['usedBytes'],before)
        self.assertEqual(first['sourceBytes']+first['copyBytes'],before)
        self.assertEqual(sum(i['physicalBytes'] for i in first['items']+second['items']),before)
        value=json.dumps(first)
        for word in ('capability','digest','writerNonce',str(self.root),'relative_path'):self.assertNotIn(word,value)
        self.assertEqual(self.store.storage()['usedBytes'],before)

    def test_unknown_and_changed_copy_allocations_stay_charged(self):
        d,r,lease,out=self.handoff()
        self.store.close();self.store=self.open();self.ledger=RequestLedger(self.store)
        before=self.store.storage()['usedBytes'];view=self.cat.storage_files()
        self.assertEqual(view['usedBytes'],before)
        self.assertEqual(self.history()['items'][0]['replyState'],'unknown')
        # Simulate a retained changed-copy report, without claiming an actual unlink.
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute("UPDATE workspace_copies SET state='cleanup_failed'")
        self.assertEqual(self.cat.storage_files()['usedBytes'],before)

    def test_execution_ended_withheld_is_never_an_answer_in_history(self):
        d,r,lease,out=self.handoff()
        receipt=OwnerReceipt(r['id'],r['ownerRequestId'],out['claimId'],r['fingerprint'],EPOCH,1,'owner_recorded','owner_recorded')
        self.ledger.record_owner_receipt(receipt)
        self.ledger.apply_owner_outcome(replace(receipt,sequence=2,state='running',provenance='codex_turn_started',attempt_id='fictional-attempt'))
        self.ledger.apply_owner_outcome(replace(receipt,sequence=3,state='execution_ended',provenance='codex_turn_ended',attempt_id='fictional-attempt'))
        item=self.history()['items'][0]
        self.assertEqual(item['replyState'],'unknown');self.assertFalse(item['retentionPinned'])

    def test_offline_management_does_not_call_resolver(self):
        self.materialized()
        resolver=mock.Mock();resolver.resolve_destination.side_effect=AssertionError('must not resolve')
        service=AttachmentService(self.store,resolver=resolver)
        self.assertEqual(service.history(BINDING['id'],BINDING['scopeId'],None)['associationCount'],1)
        self.assertEqual(service.storage_files()['sourceCount'],1)
        resolver.resolve_destination.assert_not_called()

    def officer_draft(self,cid,model):
        binding=dict(BINDING,kind='officer',id='officer',conversationId=cid,modelId=model)
        d=self.store.create_draft(binding,self.key());u=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'Fictional officer note'),self.key());d['revision']=u['draftRevision']
        out=self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(b'Fictional officer note'));d['revision']=out['draftRevision']
        return binding,d,u

    def test_officer_conversation_isolation_and_no_fabricated_saved_history(self):
        cid=self.store.create_conversation(BINDING['scopeId'],self.key())['id']
        cid2=self.store.create_conversation(BINDING['scopeId'],self.key())['id']
        binding,d,u=self.officer_draft(cid,'fictional-fast');binding2,d2,u2=self.officer_draft(cid2,'fictional-fast')
        cap=self.cap(d,u['fileId'])
        self.error('forbidden',lambda:self.cat.source_authority(HistorySelection.from_binding(binding2),'draft',d['id'],u['fileId']))
        self.error('forbidden',lambda:self.cat.preview_snapshot(cap,u2['fileId']))
        self.officer_draft(cid,'fictional-careful')
        self.assertTrue(self.publish(self.cat.preview_snapshot(cap,u['fileId']))['available'])
        history=self.cat.history(HistorySelection.from_binding(binding))
        self.assertFalse(history['retentionAvailable']);self.assertEqual(history['associationCount'],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attachment_requests').fetchone()[0],0)

    def test_source_capability_exact_association_and_expiry(self):
        d,u=self.draft();d2,u2=self.draft();cap=self.cap(d,u['fileId'])
        self.error('forbidden',lambda:self.cat.preview_snapshot(cap,u2['fileId']))
        self.error('forbidden',lambda:self.cat.source_authority(self.selection,'draft',d2['id'],u['fileId']))
        self.error('forbidden',lambda:self.cat.source_authority(self.selection,'request',d['id'],u['fileId']))
        self.clock[0]+=301;self.error('forbidden',lambda:self.cat.preview_snapshot(cap,u['fileId']))
        self.assertEqual(self.store.storage()['usedBytes'],2*len(b'Fictional orchard'))

    def test_text_edit_preserves_cap_takeover_revokes(self):
        d,u=self.draft();cap=self.cap(d,u['fileId'])
        edited=self.store.edit_text(d['id'],d['capability'],fence(d),'Another instruction');d['revision']=edited['revision']
        self.assertTrue(self.publish(self.cat.preview_snapshot(cap,u['fileId']))['available'])
        recovery=self.store.recover_draft(d['id'],d['writerGeneration'])
        self.store.resume_draft(d['id'],recovery['recoveryCapability'],d['revision'],d['writerGeneration'])
        self.error('forbidden',lambda:self.cat.preview_snapshot(cap,u['fileId']))

    def test_send_admission_revokes_draft_preview_capability(self):
        d,u=self.draft();cap=self.cap(d,u['fileId']);self.send(d)
        self.error('forbidden',lambda:self.cat.preview_snapshot(cap,u['fileId']))

    def test_revoke_between_read_and_publication_releases_no_payload(self):
        d,u=self.draft();cap=self.cap(d,u['fileId']);snapshot=self.cat.preview_snapshot(cap,u['fileId'])
        with self.store._gate(),self.store._transaction(reclaim=True):
            self.store.db.execute('UPDATE files SET access_generation=access_generation+1 WHERE id=?',(u['fileId'],))
        publish=mock.Mock();self.error('forbidden',lambda:self.cat.publish_preview(snapshot,publish));publish.assert_not_called()

    def test_remove_between_read_and_publication_releases_no_payload(self):
        d,u=self.draft();snapshot=self.cat.preview_snapshot(self.cap(d,u['fileId']),u['fileId'])
        self.store.remove_file(d['id'],d['capability'],fence(d),u['fileId'])
        publish=mock.Mock();self.error('forbidden',lambda:self.cat.publish_preview(snapshot,publish));publish.assert_not_called()

    def test_final_digest_and_identity_check_detects_tampering(self):
        d,u=self.draft();snapshot=self.cat.preview_snapshot(self.cap(d,u['fileId']),u['fileId'])
        (self.root/'attachments'/'objects'/u['fileId']).write_bytes(b'Altered private bytes')
        self.error('source_changed',lambda:self.publish(snapshot));self.assertFalse(self.store.request_handles)

    def test_live_read_handle_prevents_shutdown_and_exception_closes(self):
        d,u=self.draft();cap=self.cap(d,u['fileId']);real=os.read;observed=[]
        def reading(fd,count):
            self.error('upload_busy',self.store.close);observed.append(fd);return real(fd,count)
        with mock.patch('attachments_files.os.read',side_effect=reading):self.cat.preview_snapshot(cap,u['fileId'])
        self.assertTrue(observed);self.assertFalse(self.store.request_handles)
        with mock.patch.object(self.store.files,'read',side_effect=OSError('fictional read failure')):
            self.error('source_unavailable',lambda:self.cat.preview_snapshot(cap,u['fileId']))
        self.assertFalse(self.store.request_handles)
        for fd in observed:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_bounded_text_location_unicode_plain_text_and_large_line(self):
        d,u=self.draft(body='one\n<script>fictional</script>\n☃\n'.encode());cap=self.cap(d,u['fileId'])
        out=self.publish(self.cat.preview_snapshot(cap,u['fileId'],{'kind':'text_lines','start':2,'count':1}))
        self.assertEqual(out['lines'],[{'number':2,'text':'<script>fictional</script>'}]);self.assertTrue(out['hasMore'])
        self.error('invalid_location',lambda:self.cat.preview_snapshot(cap,u['fileId'],{'kind':'text_lines','start':1,'count':201}))
        self.error('invalid_location',lambda:self.cat.preview_snapshot(cap,u['fileId'],{'kind':'text_lines','start':99,'count':1}))
        d,u=self.draft(body=b'x'*140000)
        self.assertFalse(self.publish(self.cat.preview_snapshot(self.cap(d,u['fileId']),u['fileId']))['available'])

    def test_original_relay_unavailable_without_bytes_or_parser(self):
        body=b'%PDF-1.7\nFictional opaque original\n';d=self.store.create_draft(BINDING,self.key())
        meta=dict(metadata(body),name='fictional.pdf',mediaType='application/pdf')
        u=self.store.reserve_upload(d['id'],d['capability'],fence(d),meta,self.key(),preparation_kind='original_relay');d['revision']=u['draftRevision']
        self.store.receive_upload(d['id'],d['capability'],fence(d),u['id'],u['fingerprint'],io.BytesIO(body))
        cap=self.cap(d,u['fileId'])
        with mock.patch.object(self.store.files,'read',side_effect=AssertionError('no source read')), mock.patch('attachments_catalogue.prepare',side_effect=AssertionError('no parser')):
            self.assertFalse(self.publish(self.cat.preview_snapshot(cap,u['fileId']))['available'])
        self.assertFalse(self.store.request_handles)

    def test_http_exact_read_routes_and_duplicate_sensitive_authority(self):
        d,u=self.draft();h=self.call('/drafts/'+d['id'],[('X-Attachment-Draft-Capability',d['capability'])])
        cap=h.response[1]['files'][0]['sourceCapability'];route='/files/'+u['fileId']+'/preview'
        h=self.call(route+'?location=text_lines:1:1',[('X-Attachment-Source-Capability',cap)])
        self.assertEqual(h.response[0],200);self.assertTrue(h.response[1]['available'])
        self.assertEqual(self.call(route,[('X-Attachment-Source-Capability',cap)]*2).response[0],400)
        self.assertEqual(self.call(route+'?location=pdf_page:1',[('X-Attachment-Source-Capability',cap)]).response[0],400)
        self.assertEqual(self.call(self.history_route()).response[0],200)
        self.assertEqual(self.call('/files?destinationId=x&scopeId=y').response[0],400)
        self.assertEqual(self.call('/storage/files?path=x').response[0],400)
        self.assertEqual(self.call('/requests/'+'0'*32+'/recover',method='POST').response[0],503)

    def test_http_publication_rechecks_after_load_and_write_failure_closes(self):
        d,u=self.draft();cap=self.cap(d,u['fileId']);route='/files/'+u['fileId']+'/preview'
        actual=Catalogue.preview_snapshot
        def revoking(cat,*args):
            result=actual(cat,*args)
            with self.store._gate(),self.store._transaction(reclaim=True):self.store.db.execute('UPDATE files SET access_generation=access_generation+1')
            return result
        with mock.patch.object(Catalogue,'preview_snapshot',revoking):
            self.assertEqual(self.call(route,[('X-Attachment-Source-Capability',cap)]).response[0],403)
        cap=self.cap(d,u['fileId'])
        with mock.patch.object(Handler,'send',side_effect=OSError('fictional socket failure')):
            self.call(route,[('X-Attachment-Source-Capability',cap)])
        self.assertFalse(self.store.request_handles)

    def test_disabled_demo_and_authenticate_before_catalogue_lookup(self):
        for enabled,demo in ((False,False),(True,True)):
            root=self.root/('disabled' if not enabled else 'demo')
            store=AttachmentStore(root,enabled=enabled,demo=demo)
            self.error('attachments_disabled',lambda:Catalogue(store).storage_files())
            self.assertFalse(root.exists());store.close()
        with mock.patch.object(Catalogue,'storage_files',side_effect=AssertionError('no lookup')):
            self.assertEqual(self.call('/storage/files',defaults=False).response[0],401)
            self.assertEqual(self.call('/storage/files',service=False).response[0],503)

    def test_schema4_migration_backfills_only_proven_available(self):
        self.materialized();d,r,lease=self.send()
        self.ledger.begin_saved_cleanup(r['id'],r['capability'],self.status(r)['revision']);copy=self.status(r)['copies'][0];plan=self.ledger.cleanup_copy(copy['id'])
        self.ledger.record_copy_cleanup(CopyCleanup(copy['id'],plan['intent'].copy_generation,plan['intent'].lease_id,'absent',True),lease)
        with self.store._gate(),self.store._transaction(reclaim=True):
            drop_officer_schema(self.store.db)
            for index in ('request_source','request_created','request_history'):self.store.db.execute('DROP INDEX '+index)
            self.store.db.execute('ALTER TABLE request_files DROP COLUMN catalogued_at')
            for field in ('kind','destination','scope','conversation'):self.store.db.execute('ALTER TABLE attachment_requests DROP COLUMN catalogue_'+field)
            self.store.db.execute('PRAGMA user_version=4')
        self.store.close();self.store=self.open();self.ledger=RequestLedger(self.store)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.history()['associationCount'],1)
        self.assertEqual(self.history()['unclassifiedAssociationCount'],1)
        self.assertEqual(self.cat.storage_files()['usedBytes'],self.store.storage()['usedBytes'])


if __name__ == '__main__':unittest.main()
