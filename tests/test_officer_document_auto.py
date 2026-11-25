"""Auto is a selected context; qualified document execution remains Haiku only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_officer_document_coordinator as fixtures
from attachments_types import AttachmentError
import officer_documents as documents
from officer_policy import AUTOMATIC
from attachment_host import POSIX, POSIX_WHY, STORE, STORE_WHY, private_folder


@unittest.skipUnless(STORE, STORE_WHY)  # borrows the coordinator's storage setUp
class AutoDocumentTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    selection = fixtures.CoordinatorTests.selection
    draft = fixtures.CoordinatorTests.draft
    prepare = fixtures.CoordinatorTests.prepare
    send = fixtures.CoordinatorTests.send
    publish = fixtures.CoordinatorTests.publish

    def select_auto(self):
        self.model[0] = AUTOMATIC
        self.f.binding['modelId'] = 'auto'

    def test_auto_capability_separates_selected_and_effective_model(self):
        explicit = self.service.capabilities('officer', 'fictional-room', documents.MODEL)
        self.assertTrue(explicit['available'], explicit)
        self.assertEqual(explicit['modelId'], documents.MODEL)
        self.assertEqual(explicit['effectiveModelId'], documents.MODEL)
        self.select_auto()
        cap = self.service.capabilities('officer', 'fictional-room', 'auto')
        self.assertTrue(cap['available'], cap)
        self.assertEqual(cap['modelId'], 'auto')
        self.assertEqual(cap['effectiveModelId'], documents.MODEL)
        self.assertEqual(self.model[0], AUTOMATIC)
        self.assertEqual(self.provider.calls, 0)
        # A caller cannot borrow the effective model as another picker selection.
        self.assertFalse(self.service.capabilities('officer', 'fictional-room', documents.MODEL)['available'])

    def test_auto_draft_retention_followup_and_byline_use_actual_haiku(self):
        self.select_auto()
        draft, source = self.draft()
        self.assertEqual(draft['destination']['modelId'], 'auto')
        prepared = self.prepare(draft)
        out = self.send(draft, prepared)
        self.assertEqual(out['state'], 'answered', out)
        answer = self.publish(prepared, out)
        self.assertEqual(answer['model'], {'id': documents.MODEL, 'label': 'Haiku 5.5'})
        followup = self.service.document_ask(self.f.cid,
            dict(self.selection, question='Read the retained file again.', keptFileIds=[source], priorAnswerIds=[out['answerId']]),
            self.f.key())
        self.assertEqual(followup['state'], 'answered', followup)
        self.assertEqual(self.provider.calls, 2)
        for connection in self.provider.connections:
            self.assertEqual(json.loads(connection.requests[0][2])['model'], documents.MODEL)
        self.assertEqual(self.model[0], AUTOMATIC)

    def test_real_saved_auto_selection_routes_document_question_without_mutation(self):
        import server
        with tempfile.TemporaryDirectory(prefix='watch-auto-selection-') as directory, \
                mock.patch.object(server, 'officer_model_path',
                                  return_value=str(private_folder(Path(directory) / 'state') / 'officer-model.json')), \
                mock.patch.object(server, 'OFFICER_MODEL', documents.MODEL), \
                mock.patch.object(server, 'OFFICER_MODEL_ERROR', ''), \
                mock.patch.object(server, 'OFFICER_KEY', 'fictional-officer-key'):
            server.save_officer_model(AUTOMATIC)
            server.load_officer_model()
            self.assertEqual(server.OFFICER_MODEL, AUTOMATIC)
            self.assertEqual(server.OFFICER_MODEL_ERROR, '')
            # Production's startup callback returns the unmodified saved value.
            self.c.selected_model = lambda: server.OFFICER_MODEL
            self.f.binding['modelId'] = 'auto'
            draft, source = self.draft()
            prepared = self.prepare(draft)
            answered = self.send(draft, prepared)
            self.assertEqual(answered['state'], 'answered', answered)
            self.assertEqual(self.publish(prepared, answered)['model']['id'], documents.MODEL)
            self.assertEqual(server.OFFICER_MODEL, AUTOMATIC)
            self.assertEqual(self.provider.calls, 1)

    def test_explicit_other_models_and_mismatched_selection_are_zero_call(self):
        for selected, supplied in [('unqualified-model', 'unqualified-model'),
                                   ('unqualified-model', 'auto'),
                                   (documents.MODEL, 'auto'), (AUTOMATIC, documents.MODEL),
                                   ('auto', 'auto')]:
            with self.subTest(selected=selected, supplied=supplied):
                self.model[0] = selected
                self.f.binding['modelId'] = supplied
                self.assertFalse(self.service.capabilities('officer', 'fictional-room', supplied)['available'])
                with self.assertRaises(AttachmentError):
                    self.service.create_draft(self.selection, self.f.key())
                with self.assertRaises(AttachmentError):
                    self.service.document_ask(self.f.cid,
                        dict(self.selection, question='Read a file.', priorAnswerIds=[]), self.f.key(), prepare_only=True)
        self.assertEqual(self.provider.calls, 0)

    def test_auto_png_retention_needs_explicit_followup_before_call(self):
        self.select_auto()
        draft, source = self.draft(text='', raw=fixtures.PNG, media='image/png')
        prepared = self.prepare(draft)
        self.assertEqual(self.send(draft, prepared)['state'], 'retained_local')
        self.assertEqual(self.provider.calls, 0)
        self.provider.next.append(fixtures.FakeResponse(fixtures.wire(fixtures.response(
            'Fictional picture.\nIMAGE_SOURCE: image-1', citations=False))))
        followup = self.service.document_ask(self.f.cid,
            dict(self.selection, question='Read the retained image.', keptFileIds=[source], priorAnswerIds=[]), self.f.key())
        self.assertEqual(followup['state'], 'answered', followup)
        self.assertEqual(self.provider.calls, 1)

    def test_prior_auto_history_recovery_and_answer_stay_readable_after_model_change(self):
        self.select_auto()
        original_selection = self.selection
        draft, source = self.draft()
        prepared = self.prepare(draft)
        answered = self.send(draft, prepared)
        self.assertEqual(answered['state'], 'answered', answered)
        self.model[0] = documents.MODEL
        self.f.binding['modelId'] = documents.MODEL
        history = self.service.document_history(self.f.cid, 'officer', 'fictional-room')
        old = next(item for item in history['items'] if item['requestId'] == prepared['requestId'])
        self.assertEqual(old['destination']['modelId'], 'auto')
        restored = self.service.document_recover(prepared['requestId'],
            dict(original_selection, fingerprint=prepared['fingerprint']))
        self.assertEqual(restored['state'], 'answered', restored)
        self.assertEqual(self.publish(restored, answered)['model']['id'], documents.MODEL)
        with self.assertRaises(AttachmentError):
            self.service.document_recover(prepared['requestId'],
                dict(self.selection, fingerprint=prepared['fingerprint']))
        followup = self.service.document_ask(self.f.cid,
            dict(self.selection, question='Use the earlier Auto answer.', keptFileIds=[source], priorAnswerIds=[answered['answerId']]),
            self.f.key())
        self.assertEqual(followup['state'], 'answered', followup)
        self.assertEqual(self.provider.calls, 2)

    def test_auto_and_explicit_haiku_changes_fence_prepared_draft_and_question(self):
        for start, changed in [(AUTOMATIC, documents.MODEL), (documents.MODEL, AUTOMATIC)]:
            with self.subTest(start=start):
                self.model[0] = start
                self.f.binding['modelId'] = 'auto' if start == AUTOMATIC else start
                draft, source = self.draft()
                prepared = self.prepare(draft)
                question = self.service.document_ask(self.f.cid,
                    dict(self.selection, question='Read the file.', keptFileIds=[source], priorAnswerIds=[]), self.f.key(), prepare_only=True)
                self.model[0] = changed
                with self.assertRaises(AttachmentError):
                    self.send(draft, prepared)
                with self.assertRaises(AttachmentError):
                    self.service.document_send_question(question['requestId'], question['requestCapability'],
                        {'fingerprint': question['fingerprint']}, self.f.key())
        self.assertEqual(self.provider.calls, 0)

    def test_model_change_after_claim_is_requalified_before_transport(self):
        self.select_auto()
        draft, source = self.draft()
        prepared = self.prepare(draft)
        claim = self.c.ledger.claim
        def changed(*args, **kwargs):
            result = claim(*args, **kwargs)
            self.model[0] = documents.MODEL
            return result
        with mock.patch.object(self.c.ledger, 'claim', side_effect=changed), \
                mock.patch.object(self.transport, 'call', side_effect=AssertionError('dispatch reached')) as call:
            out = self.send(draft, prepared)
        call.assert_not_called()
        self.assertEqual(len(self.c.ledger._permits), 0)
        self.assertNotEqual(out['state'], 'answered')
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.model[0], documents.MODEL)


if __name__ == '__main__':
    unittest.main()
