"""Durable service-facing store primitives, without HTTP or provider dependencies."""
from attachment_schema_fixtures import drop_officer_schema
import json
import sqlite3
from pathlib import Path
import tempfile
import os
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from attachments_store import AttachmentStore
from attachments_types import AttachmentError
from test_attachments_foundation import BINDING,EPOCH,fence,metadata
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, state_root


@unittest.skipUnless(STORE, STORE_WHY)
class StoreBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=state_root(self.temp.name)
        self.store=AttachmentStore(self.root,enabled=True,owner_epoch=EPOCH,clock=lambda:1800000000)
        self.serial=0
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def key(self):
        self.serial+=1;return 'fictional-boundary-%06d'%self.serial
    def draft(self): return self.store.create_draft(BINDING,self.key())
    def conflict(self,call):
        with self.assertRaises(AttachmentError) as caught: call()
        self.assertEqual(caught.exception.code,'request_conflict')
    def test_global_command_namespace_and_exact_retry_restart(self):
        creation=self.key();d=self.store.create_draft(BINDING,creation)
        self.conflict(lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),creation))
        conversation=self.key();self.store.create_conversation(BINDING['scopeId'],conversation)
        self.conflict(lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),conversation))
        upload_key=self.key();u=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'hello'),upload_key)
        d['revision']=u['draftRevision']
        self.conflict(lambda:self.store.discard_draft(d['id'],d['capability'],fence(d),upload_key))
        discarded=self.key();out=self.store.discard_draft(d['id'],d['capability'],fence(d),discarded)
        other=self.draft()
        self.conflict(lambda:self.store.discard_draft(other['id'],other['capability'],fence(other),discarded))
        self.conflict(lambda:self.store.create_draft(BINDING,discarded))
        self.conflict(lambda:self.store.create_conversation(BINDING['scopeId'],discarded))
        self.conflict(lambda:self.store.reserve_upload(other['id'],other['capability'],fence(other),metadata(b'x'),discarded))
        self.store.close();self.store=AttachmentStore(self.root,enabled=True,owner_epoch=EPOCH)
        self.assertEqual(self.store.discard_draft(d['id'],d['capability'],fence(d),discarded),out)
        self.assertTrue(self.store.create_draft(BINDING,creation)['authorityChanged'])
        self.assertEqual(self.store.storage()['usedBytes'],0)

    def test_cursor_pagination_filter_epoch_and_secrecy(self):
        drafts=[self.draft() for _ in range(53)]
        expected=sorted(d['id'] for d in drafts)
        page=self.store.list_drafts(destination_id=BINDING['id'],scope_id=BINDING['scopeId'])
        self.assertEqual([d['id'] for d in page['items']],expected[:50])
        serialized=json.dumps(page)
        for secret in ('writerNonce','capability"','text"','name"',drafts[0]['capability']): self.assertNotIn(secret,serialized)
        nxt=self.store.list_drafts(destination_id=BINDING['id'],scope_id=BINDING['scopeId'],cursor=page['nextCursor'])
        self.assertEqual([d['id'] for d in nxt['items']],expected[50:]);self.assertIsNone(nxt['nextCursor'])
        for cursor,did in [(page['nextCursor']+'a',BINDING['id']),('a'*2049,BINDING['id']),(page['nextCursor'],'other')]:
            with self.assertRaises(AttachmentError):self.store.list_drafts(destination_id=did,scope_id=BINDING['scopeId'],cursor=cursor)
        self.store.close();self.store=AttachmentStore(self.root,enabled=True,owner_epoch='b'*64)
        with self.assertRaises(AttachmentError):self.store.list_drafts(destination_id=BINDING['id'],scope_id=BINDING['scopeId'],cursor=page['nextCursor'])
        self.assertEqual(self.store.draft_summary(drafts[0]['id'])['id'],drafts[0]['id'])

    def test_transport_and_detached_limits(self):
        d=self.draft();other=self.draft()
        u=self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'hello'),self.key())
        info=self.store.upload_transport_info(d['id'],d['capability'],u['id'])
        self.assertEqual(set(info),{'id','bytes','fingerprint','state'});self.assertEqual(info['bytes'],5)
        with self.assertRaises(AttachmentError):self.store.upload_transport_info(other['id'],other['capability'],u['id'])
        limits=self.store.limits_view();limits['fileBytes']=1
        self.assertGreater(self.store.limits_view()['fileBytes'],1)

    def test_schema_one_officer_model_is_not_guessed(self):
        c=self.store.create_conversation('fictional-room',self.key())
        b=dict(BINDING,kind='officer',id='officer',scopeId='fictional-room',conversationId=c['id'],modelId='fictional-model')
        d=self.store.create_draft(b,self.key());self.store.close()
        db=sqlite3.connect(self.root/'attachments'/'ledger.sqlite3')
        old=dict(b);old.pop('modelId')
        db.execute('UPDATE drafts SET destination=? WHERE id=?',(json.dumps(old),d['id']))

        drop_officer_schema(db)
        for table in ('request_files','workspace_copies','attachment_outbox','attachment_requests','workspace_roots'):db.execute('DROP TABLE '+table)
        db.execute('DROP INDEX draft_listing')
        db.execute('ALTER TABLE drafts DROP COLUMN admitted_request')
        db.execute('ALTER TABLE files DROP COLUMN access_generation');db.execute('ALTER TABLE files DROP COLUMN deletion_generation')
        db.execute('ALTER TABLE drafts DROP COLUMN destination_id');db.execute('ALTER TABLE drafts DROP COLUMN scope_id')
        db.execute('ALTER TABLE files DROP COLUMN preparation_kind')
        db.execute('PRAGMA user_version=1');db.commit();db.close()
        self.store=AttachmentStore(self.root,enabled=True,owner_epoch=EPOCH)
        self.assertIsNone(self.store.get_draft(d['id'],d['capability'])['destination']['modelId'])
        with self.assertRaises(AttachmentError) as caught:self.store.reserve_upload(d['id'],d['capability'],fence(d),metadata(b'x'),self.key())
        self.assertEqual(caught.exception.code,'model_binding_unavailable')
        self.assertEqual(self.store.discard_draft(d['id'],d['capability'],fence(d),self.key())['state'],'discarded')

if __name__=='__main__':unittest.main()
