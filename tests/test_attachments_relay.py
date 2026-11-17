from attachment_schema_fixtures import drop_officer_schema
import hashlib
import io
import json
import os
import sqlite3
import shutil
import subprocess
from dataclasses import replace
from contextlib import closing
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from attachments_relay import original_relay, SIGNATURES
from attachments_store import AttachmentStore, SCHEMA_VERSION
from attachments_types import AttachmentError
from attachments_service import AttachmentService, UnqualifiedResolver


class RelayPreparationTests(unittest.TestCase):
    def test_opaque_originals_are_never_parsed_or_returned(self):
        # These are intentionally malformed; signature agreement is not validity.
        for media, prefix in SIGNATURES.items():
            original = prefix + b'\x00fictional opaque bytes\xff'
            with self.subTest(media=media), patch('builtins.__import__', side_effect=AssertionError('parser import')):
                result = original_relay(original, media, max_bytes=1024)
            self.assertEqual(result['kind'], 'original_relay')
            self.assertEqual(result['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(result['bytes'], len(original))
            self.assertEqual(result['preview']['available'], False)
            self.assertEqual(set(result), {'kind', 'mediaType', 'bytes', 'sha256', 'preview'})
            self.assertNotIn('text', result)
            self.assertNotIn('pages', result)

    def test_mismatch_and_empty_bytes_refuse(self):
        for media in SIGNATURES:
            for raw in (b'', b'fictional', b'\x00' + SIGNATURES[media]):
                with self.subTest(media=media, raw=raw), self.assertRaises(AttachmentError) as caught:
                    original_relay(raw, media, max_bytes=1024)
                self.assertEqual(caught.exception.code, 'type_signature_mismatch')

    def test_limits_types_and_exact_boundary(self):
        raw = b'%PDF-fictional'
        self.assertEqual(original_relay(raw, 'application/pdf', max_bytes=len(raw))['bytes'], len(raw))
        cases = ((raw, 'application/pdf', len(raw)-1, 'file_too_large'),
                 (bytearray(raw), 'application/pdf', 1024, 'file_too_large'),
                 (raw, 'text/plain', 1024, 'preparation_unavailable'),
                 (raw, [], 1024, 'preparation_unavailable'),
                 (raw, 'application/pdf', True, 'preparation_unavailable'),
                 (raw, 'application/pdf', 10*1024*1024+1, 'preparation_unavailable'))
        for data, media, limit, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(AttachmentError) as caught:
                original_relay(data, media, max_bytes=limit)
            self.assertEqual(caught.exception.code, expected)

    def test_detached_metadata_has_no_retained_content_or_authority(self):
        first = original_relay(b'%PDF-fictional', 'application/pdf', max_bytes=1024)
        first['preview']['available'] = True
        second = original_relay(b'%PDF-fictional', 'application/pdf', max_bytes=1024)
        self.assertFalse(second['preview']['available'])
        self.assertNotIn('available', second)
        self.assertNotIn('recipient', second)


class RelayCapabilityTests(unittest.TestCase):
    def setUp(self):
        class Store:
            def limits_view(self):
                return {'fileBytes': 1024, 'draftBytes': 2048, 'fileCount': 5,
                        'preparationFormats': ['text/plain', 'text/markdown']}
        self.store = Store()
        self.resolved = {'available': True, 'kind': 'agent', 'id': 'fictional-ada',
                         'scopeId': 'fictional-orchard', 'modelId': None,
                         'capabilityRevision': 'fictional-revision',
                         'formats': ['text/plain', 'application/pdf', 'image/png', 'image/jpeg'],
                         'limits': {'fileBytes': 1024, 'draftBytes': 2048, 'fileCount': 5}}
        outer = self
        class Resolver:
            def resolve_destination(self, *args):
                return dict(outer.resolved)
        self.service = AttachmentService(self.store, resolver=Resolver())

    def caps(self):
        return self.service.capabilities(self.resolved['id'], self.resolved['scopeId'], self.resolved['modelId'])

    def test_originals_require_explicit_native_qualification(self):
        self.assertEqual(self.caps()['formats'], ['text/plain'])
        self.resolved['originalRelayFormats'] = ['application/pdf', 'image/png']
        result = self.caps()
        self.assertEqual(result['formats'], ['text/plain', 'application/pdf', 'image/png'])
        self.assertEqual(result['preparationModes'], {'text/plain': 'text', 'application/pdf': 'original_relay', 'image/png': 'original_relay'})
        result['preparationModes']['image/png'] = 'text'
        self.assertEqual(self.caps()['preparationModes']['image/png'], 'original_relay')

    def test_officer_never_inherits_native_relay(self):
        self.resolved.update(kind='officer', id='officer', modelId='fictional-model',
                             originalRelayFormats=['application/pdf'])
        with self.assertRaises(AttachmentError) as caught:
            self.caps()
        self.assertEqual(caught.exception.code, 'destination_unavailable')

    def test_malformed_and_overclaimed_profiles_refuse(self):
        for formats in (True, ['image/png', 'image/png'], ['text/plain'], ['application/zip'], [{}]):
            self.resolved['originalRelayFormats'] = formats
            with self.subTest(formats=formats), self.assertRaises(AttachmentError):
                self.caps()
        self.resolved['formats'] = ['text/plain']
        self.resolved['originalRelayFormats'] = ['image/png']
        with self.assertRaises(AttachmentError):
            self.caps()

    def test_default_runtime_stays_unavailable(self):
        service = AttachmentService(self.store, resolver=UnqualifiedResolver())
        self.assertFalse(service.capabilities('fictional-ada', 'fictional-orchard', None)['available'])


@unittest.skipUnless(os.name == 'posix', 'Qualified private storage requires POSIX.')
class RelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.temp.name))
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch='a'*64)
        self.binding = {'kind':'agent','id':'fictional-ada','scopeId':'fictional-orchard',
                        'modelId':None,'conversationId':None,'capabilityRevision':'fictional-v1'}
        self.draft = self.store.create_draft(self.binding, 'fictional-draft-key')

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def fence(self):
        current = self.store.get_draft(self.draft['id'], self.draft['capability'])
        return {'revision':current['revision'], 'writerGeneration':current['writerGeneration'],
                'writerNonce':self.draft['writerNonce']}

    def reserve(self, raw, media='application/pdf', mode='original_relay', key='fictional-upload-key'):
        return self.store.reserve_upload(self.draft['id'], self.draft['capability'], self.fence(),
            {'name':'fictional-source','mediaType':media,'bytes':len(raw),'digest':hashlib.sha256(raw).hexdigest()},
            key, preparation_kind=mode)

    def upload(self, raw, media='application/pdf'):
        upload = self.reserve(raw, media)
        result = self.store.receive_upload(self.draft['id'], self.draft['capability'], self.fence(),
                                          upload['id'], upload['fingerprint'], io.BytesIO(raw))
        return upload, result

    def test_upload_retains_original_and_disables_preview_without_parser(self):
        raw = b'%PDF-fictional opaque malformed structure'
        with patch('attachments_store.prepare', side_effect=AssertionError('must not parse')):
            _, result = self.upload(raw)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['preparationKind'], 'original_relay')
        self.assertFalse(result['previewAvailable'])
        row = self.store.db.execute('SELECT * FROM files WHERE id=?', (result['fileId'],)).fetchone()
        self.assertEqual(self.store.files.read(row['id'], json.loads(row['identity']), row['digest'], 1024), raw)
        file = self.store.get_draft(self.draft['id'], self.draft['capability'])['files'][0]
        self.assertEqual(file['preparationKind'], 'original_relay')
        self.assertFalse(file['previewAvailable'])
        with patch.object(self.store.files, 'read', side_effect=AssertionError('no preview read')):
            with self.assertRaises(AttachmentError) as caught:
                self.store.read_text(self.draft['id'], self.draft['capability'], row['id'])
        self.assertEqual(caught.exception.code, 'preview_unavailable')

    def test_invalid_prefix_stays_failed_and_retry_cannot_promote_it(self):
        _, result = self.upload(b'fictional not a PDF')
        self.assertEqual(result['error'], 'type_signature_mismatch')
        result = self.store.retry_preparation(self.draft['id'], self.draft['capability'], self.fence(), result['fileId'])
        self.assertEqual(result['files'][0]['state'], 'failed')
        self.assertFalse(result['files'][0]['previewAvailable'])

    def test_stored_mode_survives_restart(self):
        self.upload(b'\x89PNG\r\n\x1a\nfictional pixels', 'image/png')
        self.store.close()
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch='a'*64)
        file = self.store.get_draft(self.draft['id'], self.draft['capability'])['files'][0]
        self.assertEqual(file['preparationKind'], 'original_relay')
        self.assertFalse(file['previewAvailable'])

    def test_mode_cannot_change_replay_identity(self):
        raw = b'%PDF-fictional'
        first = self.reserve(raw)
        self.assertEqual(self.reserve(raw)['id'], first['id'])
        with self.assertRaises(AttachmentError) as caught:
            self.reserve(raw, mode='text')
        self.assertEqual(caught.exception.code, 'request_conflict')

    def test_officer_and_text_cannot_use_original_relay(self):
        conversation = self.store.create_conversation('fictional-room', 'fictional-conversation-key')
        self.binding.update(kind='officer', id='officer', scopeId='fictional-room',
                            modelId='fictional-model', conversationId=conversation['id'])
        self.draft = self.store.create_draft(self.binding, 'fictional-officer-draft')
        with self.assertRaises(AttachmentError) as caught:
            self.reserve(b'%PDF-fictional')
        self.assertEqual(caught.exception.code, 'preparation_unavailable')
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    def test_service_qualification_drift_prevents_reservation(self):
        class Resolver:
            available = True
            def resolve_destination(inner, *args):
                return {'available':inner.available, 'kind':'agent','id':'fictional-ada',
                    'scopeId':'fictional-orchard','modelId':None,'capabilityRevision':'fictional-v1',
                    'formats':['application/pdf'],'originalRelayFormats':['application/pdf'],
                    'limits':{'fileBytes':1024,'draftBytes':2048,'fileCount':5}}
        resolver = Resolver(); service = AttachmentService(self.store, resolver=resolver)
        self.assertEqual(service.capabilities('fictional-ada','fictional-orchard',None)['formats'], ['application/pdf'])
        resolver.available = False
        data = dict(self.fence(), name='fictional.pdf', mediaType='application/pdf', bytes=5,
                    digest=hashlib.sha256(b'%PDF-').hexdigest())
        with self.assertRaises(AttachmentError) as caught:
            service.mutate('reserve', self.draft['id'], self.draft['capability'], data, command_key='fictional-service-key')
        self.assertEqual(caught.exception.code, 'destination_unavailable')
        self.assertEqual(self.store.storage()['usedBytes'], 0)

    @unittest.skipUnless(shutil.which('git'), 'Offline integration requires Git.')
    def test_original_relay_through_actual_ledger_and_workspace_copy(self):
        from attachments_requests import RequestLedger, TransportProfile
        from attachments_resources import ResourceLocks
        from attachments_materialize import (Materializer, WorkspaceContext, DirectoryIdentity,
            FileIdentity, WorkspaceDirectories, copy_identity)
        original = b'%PDF-fictional deliberately unparsed original bytes'
        self.store.edit_text(self.draft['id'], self.draft['capability'], self.fence(), 'Review this fictional file.')
        with patch('attachments_store.prepare', side_effect=AssertionError('parser must not run')):
            self.upload(original)
            ledger = RequestLedger(self.store)
            profile = TransportProfile('fictional-profile','v1','codex','fictional-v1','b'*64,'c'*64,
                                       'fictional-qualification',('application/pdf',))
            with self.assertRaises(AttachmentError) as caught:
                ledger.prepare_request(self.draft['id'], self.draft['capability'], self.fence(),
                                       'fictional-intent-unqualified', self.binding, profile)
            self.assertEqual(caught.exception.code, 'source_unavailable')
            profile = replace(profile, original_relay_formats=('application/pdf',))
            request = ledger.prepare_request(self.draft['id'], self.draft['capability'], self.fence(),
                                             'fictional-intent-qualified', self.binding, profile)
            self.assertEqual(self.store.storage()['usedBytes'], len(original))
            self.assertEqual(self.store.db.execute('SELECT preparation_kind FROM request_files').fetchone()[0], 'original_relay')
            repo = self.root/'orchard'; repo.mkdir()
            git = str(Path(shutil.which('git')).resolve())
            env = {'PATH':os.defpath,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
            def command(*args):
                return subprocess.run([git,'-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-C',str(repo),*args],
                    env=env,check=True,capture_output=True,timeout=5).stdout
            command('init','-q')
            gitdir = (repo/'.git').resolve()
            context = WorkspaceContext(profile.context_fingerprint,profile.workspace_fingerprint,str(repo),DirectoryIdentity.of(repo.stat()),
                str(gitdir),DirectoryIdentity.of(gitdir.stat()),str(gitdir/'info'),DirectoryIdentity.of((gitdir/'info').stat()),
                git,FileIdentity.of(Path(git).stat()))
            lockroot = self.root/'locks'; lockroot.mkdir(mode=0o700)
            locks = ResourceLocks(str(lockroot),enabled=True)
            try:
                with locks.hold(ledger.resource_keys(request['id'])) as lease:
                    with self.assertRaises(AttachmentError) as caught:
                        ledger.authorize_send(request['id'],request['capability'],request['revision'],request['fingerprint'],
                            resolved_binding=self.binding,transport_profile=replace(profile,original_relay_formats=()),resource_lease=lease)
                    self.assertEqual(caught.exception.code,'capability_changed')
                    self.assertFalse((repo/'.watch-attachments').exists())
                    current = ledger.authorize_send(request['id'],request['capability'],request['revision'],request['fingerprint'],
                        resolved_binding=self.binding,transport_profile=profile,resource_lease=lease)
                    self.assertEqual(self.store.storage()['usedBytes'],len(original)*2)
                    materializer = Materializer(enabled=True,
                        validate_authority=lambda authority,phase,copies: authority==ledger.authority(request['id']),
                        record_directory=ledger.record_directory_created,record_created=ledger.record_copy_created,
                        record_partial=ledger.record_partial)
                    authority = ledger.authority(request['id'])
                    materializer.prepare_directories(context,authority,lease)
                    spec = ledger.claim_copy(request['id'],current['revision'],current['copies'][0]['id'],lease)
                    authority = ledger.authority(request['id'])
                    dirs = WorkspaceDirectories.from_record(ledger.workspace_directories(request['id']))
                    materializer.ensure_exclusion(context,authority,(spec,),lease)
                    with ledger.source_handle(spec.id,lease) as fd:
                        saved = materializer.materialize(context,authority,spec,fd,copy_identity(FileIdentity.of(os.fstat(fd))),lease,directories=dirs)
                    current = ledger.record_copy_result(saved,lease)
                    self.assertEqual(current['state'],'materialized')
                    self.assertEqual((repo/spec.relative_path).read_bytes(),original)
                    self.assertEqual(command('ls-files','--cached','--',spec.relative_path),b'')
                    authority = ledger.authority(request['id'])
                    proof = materializer.verify_handoff(context,authority,(spec,),(saved,),lease,directories=dirs)
                    claim = ledger.claim_handoff(request['id'],current['revision'],request['fingerprint'],proof,lease)
                    self.assertTrue(claim['ownerRequestId'])
                    repeat = ledger.claim_handoff(request['id'],current['revision'],request['fingerprint'],proof,lease)
                    self.assertFalse(repeat['newClaim'])
                    self.assertEqual(repeat['claimId'],claim['claimId'])
                    self.assertNotIn('envelope',repeat)
                    # Only a local claim: no owner/native transport is invoked.
            finally:
                locks.close()

    def test_schema_migration_preserves_text_default(self):
        raw = b'fictional text'
        self.reserve(raw, media='text/plain', mode='text')
        self.store.close()
        db = self.root / 'attachments' / 'ledger.sqlite3'
        with closing(sqlite3.connect(db)) as con, con:
            drop_officer_schema(con)
            for index in ('request_source', 'request_created', 'request_history'):
                con.execute('DROP INDEX ' + index)
            con.execute('ALTER TABLE request_files DROP COLUMN catalogued_at')
            for field in ('kind', 'destination', 'scope', 'conversation'):
                con.execute('ALTER TABLE attachment_requests DROP COLUMN catalogue_' + field)
            con.execute('ALTER TABLE files DROP COLUMN preparation_kind')
            con.execute('PRAGMA user_version=3')
        self.store = AttachmentStore(self.root, enabled=True, owner_epoch='a'*64)
        file = self.store.get_draft(self.draft['id'], self.draft['capability'])['files'][0]
        self.assertEqual(file['preparationKind'], 'text')
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
