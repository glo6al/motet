"""Live page seams against real private storage and a fictional provider."""
import concurrent.futures
import hashlib
import os
import unittest
from unittest import mock

import test_officer_document_coordinator as fixtures
from attachments_types import AttachmentError


@unittest.skipUnless(os.name == 'posix', 'Private storage requires POSIX.')  # borrows the coordinator's storage setUp
class PageRouteTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    selection = fixtures.CoordinatorTests.selection
    draft = fixtures.CoordinatorTests.draft
    prepare = fixtures.CoordinatorTests.prepare
    send = fixtures.CoordinatorTests.send
    http = fixtures.CoordinatorTests.http

    def test_current_discovery_never_creates_and_exact_empty_mapping_restores(self):
        route = '/conversations/current?scopeId=fictional-room'
        with self.store.lock:
            before = self.store.db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
            self.assertIsNone(self.http('GET', route)[1]['conversation'])
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], before)
        # Treat this response as lost: only the following GET supplies page identity.
        self.http('POST', '/conversations/current', {'scopeId': 'fictional-room'})
        restored = self.http('GET', route)[1]['conversation']
        cid = restored['id']
        self.assertEqual(restored['scopeId'], 'fictional-room')
        for path in ['/conversations/'+cid+'/requests?destinationId=officer&scopeId=fictional-room',
                     '/files?destinationId=officer&scopeId=fictional-room&conversationId='+cid]:
            status, value, _ = self.http('GET', path)
            self.assertEqual(status, 200, value)
            self.assertEqual(value['items'], [])
        self.f.binding['conversationId'] = cid
        draft, source = self.draft(text='')
        self.assertEqual(self.send(draft, self.prepare(draft))['state'], 'retained_local')
        self.assertEqual(self.provider.calls, 0)

    def test_current_concurrent_creation_returns_one_exact_id_and_offline_restores(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.service.current_conversation('fictional-room', create=True), range(8)))
        self.assertEqual(len({r['conversation']['id'] for r in rows}), 1)
        self.scopes.clear()
        self.assertEqual(self.service.current_conversation('fictional-room', create=True), rows[0])
        self.assertEqual(self.http('POST', '/conversations/current', {'scopeId': 'unregistered'})[0], 503)
        self.assertIsNone(self.http('GET', '/conversations/current?scopeId=unregistered')[1]['conversation'])

    def test_current_closed_owner_boundary_and_no_guess_by_scope_or_newest(self):
        self.assertEqual(self.http('GET', '/conversations/current?scopeId=fictional-room', defaults=False)[0], 401)
        self.assertEqual(self.http('POST', '/conversations/current', {'scopeId': 'fictional-room', 'id': 'a'*32})[0], 400)
        self.assertEqual(self.http('GET', '/conversations/current?scopeId=fictional-room&extra=x')[0], 400)
        unrelated = self.store.create_conversation('fictional-room', self.f.key())
        self.assertIsNone(self.service.current_conversation('fictional-room')['conversation'])
        status, _, _ = self.http('GET', '/conversations/'+unrelated['id']+'/requests?destinationId=officer&scopeId=fictional-room')
        self.assertEqual(status, 403)

    def test_question_preparation_has_no_call_and_execution_is_once_only(self):
        draft, source = self.draft(text='')
        self.send(draft, self.prepare(draft))
        data = dict(self.selection, question='Read the fictional file.', priorAnswerIds=[], keptFileIds=[source])
        path = '/conversations/'+self.f.cid+'/question-intent'
        key = self.f.key()
        status, prepared, _ = self.http('POST', path, data, [('Idempotency-Key', key)])
        self.assertEqual(status, 200, prepared)
        self.assertEqual(prepared['state'], 'prepared')
        self.assertEqual(self.provider.calls, 0)
        again = self.http('POST', path, data, [('Idempotency-Key', key)])[1]
        self.assertEqual(again['requestId'], prepared['requestId'])
        header = [('X-Attachment-Request-Capability', prepared['requestCapability'])]
        request = '/requests/'+prepared['requestId']
        status, result, _ = self.http('POST', request+'/ask', {'fingerprint': prepared['fingerprint']}, header+[('Idempotency-Key', self.f.key())])
        self.assertEqual(status, 200, result)
        self.assertEqual(result['state'], 'answered')
        self.assertEqual(self.provider.calls, 1)
        # Lost execution response: a read recovers without repeating execution.
        restored = self.http('GET', request, headers=header)[1]
        self.assertEqual(restored['answerId'], result['answerId'])
        self.http('POST', request+'/ask', {'fingerprint': prepared['fingerprint']}, header+[('Idempotency-Key', self.f.key())])
        self.assertEqual(self.provider.calls, 1)

    def test_officer_question_context_check_uses_the_store_lock(self):
        draft, source = self.draft(text='')
        self.send(draft, self.prepare(draft))
        data = dict(self.selection, question='Read the fictional file.', priorAnswerIds=[], keptFileIds=[source])
        original = self.c.conversations._selection
        checks = []

        def check_under_lock(selection):
            checks.append(self.store.lock._is_owned())
            return original(selection)

        with mock.patch.object(self.c.conversations, '_selection', check_under_lock):
            for endpoint in ('question-intent', 'ask'):
                path = '/conversations/' + self.f.cid + '/' + endpoint
                status, body, _ = self.http('POST', path, data, [('Idempotency-Key', self.f.key())])
                self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue(all(checks))
        self.assertEqual(self.provider.calls, 1)

    def test_prepare_digest_lookup_is_read_only_exact_and_never_grants_authority(self):
        self.f.cid=self.service.current_conversation('fictional-room',create=True)['conversation']['id']
        self.f.binding['conversationId']=self.f.cid
        def endpoint(digest,scope='fictional-room',cid=None):
            return '/conversations/'+(cid or self.f.cid)+'/requests/by-prepare?destinationId=officer&scopeId='+scope+'&prepareDigest='+digest
        key=self.f.key();prepare_digest=hashlib.sha256(key.encode()).hexdigest();route=endpoint(prepare_digest)
        self.assertEqual(self.http('GET',route,defaults=False)[0],401)
        self.assertEqual(self.http('GET',route+'&cursor=unexpected')[0],400)
        self.assertEqual(self.http('GET',route+'&prepareDigest='+prepare_digest)[0],400)
        self.assertEqual(self.http('GET',endpoint('F'*64))[0],400)
        self.assertEqual(self.http('GET',endpoint(prepare_digest,scope='fictional-other'))[0],403)
        self.assertEqual(self.http('POST',route,{})[0],405)
        with self.store.lock:
            before=self.store.db.total_changes
            self.assertIsNone(self.http('GET',route)[1]['request'])
            self.assertEqual(self.store.db.total_changes,before)
        draft,_=self.draft()
        prepared=self.service.document_prepare(draft['id'],draft['capability'],fixtures.fence(draft),key)
        status,found,_=self.http('GET',route);self.assertEqual(status,200)
        self.assertEqual(found['request'],{'requestId':prepared['requestId'],'draftId':draft['id'],'kind':'draft','state':'prepared'})
        self.assertFalse({'requestCapability','fingerprint','question','prepareDigest'}.intersection(found['request']))
        self.assertIsNone(self.http('GET',endpoint('0'*64))[1]['request'])
        other=self.store.create_conversation('fictional-room',self.f.key())['id']
        self.assertEqual(self.http('GET',endpoint(prepare_digest,cid=other))[0],403)
        self.scopes.add('fictional-other')
        foreign=self.service.current_conversation('fictional-other',create=True)['conversation']['id']
        self.assertIsNone(self.http('GET',endpoint(prepare_digest,scope='fictional-other',cid=foreign))[1]['request'])
        with self.store.lock:
            prior=self.store.db.total_changes
            with mock.patch.object(self.c.intents,'consume',side_effect=AssertionError('lookup cannot consume')), \
                    mock.patch.object(self.store.files,'read',side_effect=AssertionError('lookup cannot read file bytes')):
                self.assertEqual(self.http('GET',route)[1]['request'],found['request'])
            self.assertEqual(self.store.db.total_changes,prior);self.assertEqual(self.provider.calls,0)

    def test_prepare_digest_lookup_tracks_exact_question_cancellation(self):
        draft,source=self.draft(text='');self.send(draft,self.prepare(draft))
        key=self.f.key();data=dict(self.selection,question='Exact fictional follow-up.',priorAnswerIds=[], keptFileIds=[source])
        prepared=self.service.document_ask(self.f.cid,data,key,prepare_only=True)
        route='/conversations/'+self.f.cid+'/requests/by-prepare?destinationId=officer&scopeId=fictional-room&prepareDigest='+hashlib.sha256(key.encode()).hexdigest()
        expected={'requestId':prepared['requestId'],'draftId':None,'kind':'question','state':'prepared'}
        self.assertEqual(self.http('GET',route)[1]['request'],expected)
        cmd=[('Idempotency-Key',self.f.key()),('X-Attachment-Request-Capability',prepared['requestCapability'])]
        self.assertEqual(self.http('POST','/requests/'+prepared['requestId']+'/abandon',{'fingerprint':prepared['fingerprint']},cmd)[0],200)
        self.assertEqual(self.http('GET',route)[1]['request'],dict(expected,state='failed'))
        self.assertEqual(self.provider.calls,0)

    def test_wrong_intent_kind_capability_fingerprint_and_model_refuse_before_call(self):
        draft, source = self.draft(text='')
        prepared = self.prepare(draft)
        headers = [('X-Attachment-Request-Capability', prepared['requestCapability']), ('Idempotency-Key', self.f.key())]
        self.assertEqual(self.http('POST', '/requests/'+prepared['requestId']+'/ask', {'fingerprint': prepared['fingerprint']}, headers)[0], 409)
        self.send(draft, prepared)
        data = dict(self.selection, question='Read the file.', priorAnswerIds=[], keptFileIds=[source])
        question = self.service.document_ask(self.f.cid, data, self.f.key(), prepare_only=True)
        request = '/requests/'+question['requestId']+'/ask'
        header = [('X-Attachment-Request-Capability', question['requestCapability']), ('Idempotency-Key', self.f.key())]
        self.assertEqual(self.http('POST', request, {'fingerprint': '0'*64}, header)[0], 409)
        self.assertEqual(self.http('POST', request, {'fingerprint': question['fingerprint'], 'question': 'changed'}, header)[0], 400)
        self.model[0] = 'unqualified'
        self.assertEqual(self.http('POST', request, {'fingerprint': question['fingerprint']}, header)[0], 503)
        self.assertEqual(self.provider.calls, 0)

    def test_known_no_call_reason_is_retained_and_unknown_error_is_not_classified(self):
        for code, expected in [('document_budget_exhausted', 'budget_exhausted'),
                               ('document_budget_halted', 'reading_unavailable'),
                               ('not_a_public_reason', None)]:
            draft, _ = self.draft()
            prepared = self.prepare(draft)
            with mock.patch.object(self.c.ledger, 'claim', side_effect=AttachmentError(code, 'private fixture detail')):
                result = self.send(draft, prepared)
            self.assertEqual(result['state'], 'retained_local')
            self.assertEqual(result['notAskedReason'], expected)
            self.assertEqual(self.c.status(prepared['requestId'], prepared['requestCapability'])['notAskedReason'], expected)
            self.assertNotIn('private fixture detail', str(result))
        self.assertEqual(self.provider.calls, 0)

    def test_unselected_large_kept_file_does_not_block_new_question(self):
        raw = b'%PDF-1.4\n' + b'x' * 249000
        first, _ = self.draft(text='', raw=raw)
        saved = self.send(first, self.prepare(first))
        self.assertEqual(saved['notAskedReason'], 'no_question')
        second, _ = self.draft(raw=raw)
        result = self.send(second, self.prepare(second))
        self.assertEqual(result['state'], 'answered')
        self.assertEqual(self.provider.calls, 1)
        files = self.service.history('officer', self.selection['scopeId'], self.selection['conversationId'])
        self.assertEqual(len(files['items']), 2)

    def test_abandon_lost_question_preparation_fences_delayed_execution(self):
        draft, source = self.draft(text='')
        self.send(draft, self.prepare(draft))
        data = dict(self.selection, question='Fictional first question.', priorAnswerIds=[], keptFileIds=[source])
        prepared = self.service.document_ask(self.f.cid, data, self.f.key(), prepare_only=True)
        header = [('X-Attachment-Request-Capability', prepared['requestCapability']), ('Idempotency-Key', self.f.key())]
        path = '/requests/'+prepared['requestId']
        code, canceled, _ = self.http('POST', path+'/abandon', {'fingerprint': prepared['fingerprint']}, header)
        self.assertEqual(code, 200, canceled)
        self.assertTrue(canceled['abandoned'])
        self.assertFalse(canceled['canAbandon'])
        self.assertEqual(canceled['officerDelivery']['state'], 'not_sent')
        self.assertEqual(canceled['state'], 'failed')
        again = self.http('POST', path+'/abandon', {'fingerprint': prepared['fingerprint']}, header)[1]
        self.assertEqual({k:v for k,v in again.items() if k!='serverTime'}, {k:v for k,v in canceled.items() if k!='serverTime'})
        late = self.http('POST', path+'/ask', {'fingerprint': prepared['fingerprint']}, header)[1]
        self.assertEqual({k:v for k,v in late.items() if k!='serverTime'}, {k:v for k,v in canceled.items() if k!='serverTime'})
        self.assertEqual(self.provider.calls, 0)
        newer = self.service.document_ask(self.f.cid, data, self.f.key())
        self.assertEqual(newer['state'], 'answered')
        self.assertEqual(self.provider.calls, 1)
        code, _, _ = self.http('POST', '/requests/'+newer['requestId']+'/abandon',
                              {'fingerprint': newer['fingerprint']},
                              [('X-Attachment-Request-Capability', newer['requestCapability']), ('Idempotency-Key', self.f.key())])
        self.assertEqual(code, 409)

    def test_abandoned_draft_preserves_files_for_explicit_resume_and_new_send(self):
        draft, fid = self.draft()
        prepared = self.prepare(draft)
        canceled = self.service.document_abandon(prepared['requestId'], prepared['requestCapability'],
                                                 {'fingerprint': prepared['fingerprint']}, self.f.key())
        self.assertEqual(canceled['draftId'], draft['id'])
        self.assertTrue(self.send(draft, prepared)['abandoned'])
        recovered = self.service.recover(draft['id'], {'writerGeneration': draft['writerGeneration']})
        resumed = self.service.resume(draft['id'], recovered['recoveryCapability'],
                                      {'revision': recovered['revision'], 'writerGeneration': recovered['writerGeneration']})
        self.assertEqual(resumed['files'][0]['id'], fid)
        self.assertEqual(resumed['text'], draft['text'])
        self.assertEqual(self.send(resumed, self.prepare(resumed))['state'], 'answered')
        self.assertEqual(self.provider.calls, 1)
        old = self.c.status(prepared['requestId'], prepared['requestCapability'])
        self.assertTrue(old['abandoned'])
        self.assertEqual(self.service.document_abandon(prepared['requestId'], prepared['requestCapability'],
                         {'fingerprint': prepared['fingerprint']}, self.f.key()), old)

    def test_abandon_competes_with_consume_without_canceling_a_started_request(self):
        import threading
        draft, source = self.draft(text='')
        self.send(draft, self.prepare(draft))
        for _ in range(6):
            prepared = self.service.document_ask(self.f.cid, dict(self.selection, question='Race fixture.', priorAnswerIds=[], keptFileIds=[source]), self.f.key(), prepare_only=True)
            barrier = threading.Barrier(2)
            def consume():
                barrier.wait()
                return self.c.intents.consume(prepared['requestId'], prepared['requestCapability'], fingerprint=prepared['fingerprint'])
            def abandon():
                barrier.wait()
                try:
                    return self.c.intents.abandon(prepared['requestId'], prepared['requestCapability'], prepared['fingerprint'])
                except AttachmentError as error:
                    return error.code
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                run, cancel = pool.submit(consume), pool.submit(abandon)
                record, winner = run.result()
                outcome = cancel.result()
            if winner is None:
                self.assertTrue(outcome['abandoned'])
            else:
                self.assertEqual(outcome, 'request_already_started')
                self.c.intents.finish(record['id'], winner)
        self.assertEqual(self.provider.calls, 0)


if __name__ == '__main__':
    unittest.main()
