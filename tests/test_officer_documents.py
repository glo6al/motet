"""Fictional bytes and responses only. No provider, parser, filesystem or store."""
from dataclasses import FrozenInstanceError, asdict, replace
import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_documents as documents
from attachments_conversations import SourceDependency


def profile(**changes):
    candidate = documents.OfficerInputProfile('officer-direct-pdf-png', 'v1', 'haiku-direct-pdf-png-2026-11',
        documents.MODEL, 'minimal-direct-v1', ('application/pdf', 'image/png'), None, **changes)
    evidence = documents.StrictBudgetEvidence('strict_usd', 'fictional-prices-v1', 'fictional-billing-v1',
        '0.20', 'fictional-conservative-bound', 'v1', 10000, documents.profile_binding(candidate))
    return replace(candidate, strict_budget_evidence=evidence)


def source(number=1, media='application/pdf', content=None, **dependency_changes):
    if content is None:
        # Deliberately not a parsed/structurally validated document or image.
        content = documents.SIGNATURES.get(media, b'FICTIONAL') + b' fictional source ' + str(number).encode()
    digest = hashlib.sha256(content).hexdigest()
    dep = SourceDependency('a' * 64, 'officer', 'fictional-room', 'c' * 32, f'{1000 + number:032x}',
        1, 1, f'{number:032x}', 1, 1, 0, digest, (1, 100 + number, len(content), 1000, 1000))
    return documents.DocumentSource(replace(dep, **dependency_changes), media, 'officer_direct', len(content), digest, content)


def manifest(p, sources, question='Read the fictional sources.'):
    return documents.InputManifest('b' * 32, 'c' * 32, 'fictional-room', p.revision,
        hashlib.sha256(question.encode()).hexdigest(), tuple(s.source_dependency.source_id for s in sources),
        (), tuple(s.source_dependency for s in sources), 1)


def prepare(sources=None, p=None, question='Read the fictional sources.'):
    p = p or profile()
    sources = sources or (source(), source(2, 'image/png'))
    return p, documents.build_input(p, manifest(p, sources, question), question, sources)


def response(text='A fictional fact.\nIMAGE_SOURCE: image-1', *, citations=True):
    block = {'type': 'text', 'text': text}
    if citations:
        block['citations'] = [{'type': 'page_location', 'document_index': 0, 'document_title': 'Document 1',
            'start_page_number': 2, 'end_page_number': 3, 'cited_text': 'A fictional fact.'}]
    return {'id': 'msg_fictional', 'type': 'message', 'role': 'assistant', 'model': documents.MODEL,
        'content': [block], 'stop_reason': 'end_turn', 'stop_sequence': None,
        'usage': {'input_tokens': 100, 'output_tokens': 80, 'cache_creation_input_tokens': 0,
                  'cache_read_input_tokens': 0, 'cache_creation': {'ephemeral_5m_input_tokens': 0,
                  'ephemeral_1h_input_tokens': 0}, 'output_tokens_details': {'thinking_tokens': 0},
                  'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'},
        'container': None, 'diagnostics': None, 'stop_details': None}


def wire(value):
    return json.dumps(value, separators=(',', ':')).encode()


class InputTests(unittest.TestCase):
    def reject(self, action):
        with self.assertRaises(documents.DocumentRejected) as caught:
            action()
        self.assertNotIn('fictional source', str(caught.exception))

    def test_exact_minimal_profile_body_headers_and_unchanged_bytes(self):
        p, request = prepare()
        body = json.loads(request.body_bytes)
        self.assertEqual(set(body), {'model', 'max_tokens', 'service_tier', 'system', 'messages'})
        self.assertEqual((body['model'], body['max_tokens'], body['service_tier']),
                         ('claude-haiku-5-5', 1024, 'standard_only'))
        self.assertEqual(dict(request.headers)['anthropic-version'], '2026-10-21')
        self.assertEqual(request.endpoint, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(set(body['messages'][0]), {'role', 'content'})
        blocks = body['messages'][0]['content']
        self.assertEqual(blocks[0]['citations'], {'enabled': True})
        for block, original in zip(blocks, (source(), source(2, 'image/png'))):
            self.assertEqual(base64.b64decode(block['source']['data']), original.content)
        self.assertEqual(request.body_digest, hashlib.sha256(request.body_bytes).hexdigest())
        from officer_request import request_digest
        self.assertEqual(request.body_digest, request_digest(body))
        self.assertEqual(request.budget_inputs.body_digest, request.body_digest)
        self.assertEqual(request.budget_inputs.encoded_bytes, len(request.body_bytes))
        self.assertEqual(request.budget_inputs.strict_evidence, p.strict_budget_evidence)
        for forbidden in ('speed', 'inference_geo', 'stream', 'tools', 'tool_choice', 'temperature', 'top_k', 'top_p'):
            self.assertNotIn(forbidden, body)

    def test_profile_and_strict_budget_evidence_are_required_not_defaulted(self):
        p, request = prepare()
        values = (replace(p, strict_budget_evidence=None),
                  replace(p, model_id='claude-other-model'), replace(p, media_types=('image/jpeg',)),
                  replace(p, max_calls=2), replace(p, max_output_tokens=True),
                  replace(p, max_encoded_bytes=documents.MAX_BODY_BYTES + 1), replace(p, qualification_revision='unproved'))
        sources = (source(),)
        for value in values:
            self.reject(lambda value=value: documents.build_input(value, manifest(p, sources), 'Read the fictional sources.', sources))
        for changes in ({'mode': 'bounded'}, {'usd_ceiling': '0'}, {'usd_ceiling': 'NaN'},
                        {'usd_ceiling': '-1'}, {'pricing_revision': ''}, {'billing_revision': ''},
                        {'token_bound_algorithm': ''}, {'max_input_tokens': None}, {'profile_binding': 'f' * 64}):
            bad = replace(p, strict_budget_evidence=replace(p.strict_budget_evidence, **changes))
            self.reject(lambda bad=bad: documents.build_input(bad, manifest(bad, sources), 'Read the fictional sources.', sources))
        self.assertFalse(hasattr(documents, 'CANDIDATE_PROFILE'))

    def test_complete_roster_exact_order_closure_and_context(self):
        p = profile()
        sources = (source(), source(2, 'image/png'))
        frozen = manifest(p, sources)
        for changes in ({'ordered_sources': frozen.ordered_sources[:1]},
                        {'flattened_dependencies': frozen.flattened_dependencies[:1]},
                        {'ordered_sources': frozen.ordered_sources[::-1]}, {'question_digest': '0' * 64},
                        {'roster_revision': True}, {'scope_id': 'foreign-room'}, {'conversation_id': 'd' * 32},
                        {'prior_answer_ids': ('e' * 32,)}, {'profile_revision': 'v2'}):
            self.reject(lambda changes=changes: documents.build_input(p, replace(frozen, **changes),
                'Read the fictional sources.', sources))
        foreign = (source(), source(2, 'image/png', scope_id='other-room'))
        self.reject(lambda: documents.build_input(p, manifest(p, foreign), 'Read the fictional sources.', foreign))
        self.reject(lambda: documents.build_input(p, manifest(p, (sources[0], sources[0])),
            'Read the fictional sources.', (sources[0], sources[0])))

    def test_50_sources_complete_and_51_refuses_without_truncation(self):
        sources = tuple(source(n + 1) for n in range(50))
        p, request = prepare(sources)
        self.assertEqual(len(request.complete_manifest.dependencies), 50)
        self.assertEqual(len(request.input_map), 50)
        too_many = sources + (source(51),)
        self.reject(lambda: documents.build_input(p, manifest(p, too_many), 'Read the fictional sources.', too_many))

    def test_original_relay_jpeg_text_and_type_prefix_mismatch_refuse(self):
        p = profile()
        for item in (replace(source(), preparation_kind='original_relay'),
                     replace(source(), preparation_kind='text'), source(1, 'image/jpeg'), source(1, 'text/plain'),
                     source(content=b'not a PDF'), source(1, 'image/png', content=b'not a PNG')):
            self.reject(lambda item=item: documents.build_input(p, manifest(p, (item,)), 'Read the fictional sources.', (item,)))

    def test_digest_size_identity_and_dependency_types_are_strict(self):
        p = profile()
        original = source()
        for item in (replace(original, sha256='0' * 64), replace(original, byte_count=True),
                     replace(original, byte_count=original.byte_count + 1), replace(original, content=bytearray(original.content)),
                     replace(original, source_dependency=replace(original.source_dependency, sha256='A' * 64)),
                     replace(original, source_dependency=replace(original.source_dependency, source_generation=True)),
                     replace(original, source_dependency=replace(original.source_dependency, deletion_generation=1)),
                     replace(original, source_dependency=replace(original.source_dependency, identity=(1, 1, 0, 1, 1)))):
            self.reject(lambda item=item: documents.build_input(p, manifest(p, (item,)), 'Read the fictional sources.', (item,)))

    def test_full_json_budget_exact_boundary_and_base64_expansion(self):
        p, request = prepare((source(),))
        exact = profile(max_encoded_bytes=len(request.body_bytes))
        _, rebuilt = prepare((source(),), exact)
        self.assertEqual(rebuilt.body_bytes, request.body_bytes)
        small = profile(max_encoded_bytes=len(request.body_bytes) - 1)
        self.reject(lambda: prepare((source(),), small))
        raw = source(content=b'%PDF-' + b'x' * 1400)
        small = profile(max_encoded_bytes=1800)
        self.assertLess(raw.byte_count, small.max_encoded_bytes)
        self.reject(lambda: prepare((raw,), small))
        encoded_overflow = source(content=b'%PDF-' + b'x' * 400000)
        self.assertLess(encoded_overflow.byte_count, documents.MAX_BODY_BYTES)
        self.reject(lambda: prepare((encoded_overflow,)))

    def test_deterministic_frozen_manifest_and_no_io(self):
        with mock.patch('builtins.open', side_effect=AssertionError('no filesystem')),\
             mock.patch('socket.socket', side_effect=AssertionError('no network')),\
             mock.patch('sqlite3.connect', side_effect=AssertionError('no database')):
            p, one = prepare()
            _, two = prepare()
        self.assertEqual(one, two)
        self.assertEqual(one.complete_manifest.digest, two.complete_manifest.digest)
        with self.assertRaises(FrozenInstanceError):
            one.body_digest = '0' * 64
        with self.assertRaises(FrozenInstanceError):
            one.input_map[0].dependency.source_generation = 2
        self.assertIsInstance(one.complete_manifest.dependencies, tuple)
        self.assertNotIn(source().content.decode(), repr(one))


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.p, self.request = prepare()

    def validate(self, value):
        return documents.validate_response(self.p, self.request, wire(value))

    def reject(self, value):
        with self.assertRaises(documents.DocumentRejected):
            self.validate(value)

    def test_pdf_single_page_and_explicit_whole_image_are_distinct(self):
        answer = self.validate(response())
        self.assertEqual(answer.actions, ())
        self.assertEqual(len(answer.citations), 2)
        pdf, image = answer.citations
        self.assertIsInstance(pdf, documents.PDFPageReference)
        self.assertEqual((pdf.start_page, pdf.end_page_exclusive), (2, 3))
        self.assertEqual(pdf.provenance, 'provider_reported_single_page')
        self.assertIsInstance(image, documents.WholeImageReference)
        self.assertEqual(image.source_id, source(2, 'image/png').source_dependency.source_id)
        self.assertEqual(image.provenance, 'declared_whole_image_reference')
        self.assertEqual(answer.consumed_dependencies, self.request.complete_manifest.dependencies)

    def test_pdf_indices_count_documents_independently_of_images(self):
        self.p, self.request = prepare((source(1, 'image/png'), source(2), source(3, 'image/png'), source(4)))
        value = response('Cited second PDF.\nIMAGE_SOURCE: image-2')
        citation = value['content'][0]['citations'][0]
        citation.update(document_index=1, document_title='Document 2')
        answer = self.validate(value)
        self.assertEqual(answer.citations[0].source_id, source(4).source_dependency.source_id)
        self.assertEqual(answer.citations[0].input_index, 3)
        self.assertEqual(answer.citations[1].source_id, source(3, 'image/png').source_dependency.source_id)
        self.assertEqual(len(answer.consumed_dependencies), 4)

    def test_uncited_inputs_remain_dependencies_without_used_reference(self):
        answer = self.validate(response('Only the PDF fact.', citations=True))
        self.assertEqual(len(answer.citations), 1)
        self.assertEqual(len(answer.consumed_dependencies), 2)
        self.assertNotIn(source(2, 'image/png').source_dependency.source_id, {r.source_id for r in answer.citations})
        no_citations = self.validate(response('An answer without source declarations.', citations=False))
        self.assertEqual(no_citations.citations, ())
        self.assertEqual(len(no_citations.consumed_dependencies), 2)

    def test_whole_image_marker_has_closed_grammar_not_casual_prose(self):
        for text in ('IMAGE_SOURCE: image-2', 'IMAGE_SOURCE: image-0', 'IMAGE_SOURCE: image-01',
                     'IMAGE_SOURCE: image-1\nIMAGE_SOURCE: image-1', 'IMAGE_SOURCE:image-1',
                     'IMAGE_SOURCE: https://example.invalid/file.png', 'IMAGE_SOURCE: image-1 and image-2',
                     'The marker IMAGE_SOURCE: image-1 is mentioned here.',
                     '\vIMAGE_SOURCE: image-1', 'IMAGE_SOURCE: image-1\r'):
            self.reject(response(text, citations=False))
        answer = self.validate(response('This ordinary prose mentions image-1.', citations=False))
        self.assertEqual(answer.citations, ())
        self.assertEqual(len(answer.consumed_dependencies), 2)
        crlf = self.validate(response('A fact.\r\nIMAGE_SOURCE: image-1\r\n', citations=False))
        self.assertEqual(len(crlf.citations), 1)
        self.assertEqual(crlf.citations[0].text_start, len('A fact.\r\n'))

    def test_valid_native_multi_page_range_is_unsupported_not_shortened(self):
        value = response()
        value['content'][0]['citations'][0]['end_page_number'] = 4
        self.reject(value)

    def test_foreign_unknown_malformed_or_url_citations_refuse(self):
        for changes in ({'document_index': 1}, {'document_index': True}, {'document_index': -1},
                        {'start_page_number': 0}, {'start_page_number': True}, {'end_page_number': 2},
                        {'type': 'char_location'}, {'type': 'web_search_result_location'},
                        {'url': 'https://example.invalid/file.pdf'}, {'cited_text': ''},
                        {'cited_text': 'x' * (documents.MAX_CITED_BYTES + 1)}, {'document_title': 'foreign'}):
            value = response()
            value['content'][0]['citations'][0].update(changes)
            self.reject(value)
        value = response()
        value['content'][0]['citations'] = None
        self.reject(value)

    def test_tool_unknown_content_model_stop_and_top_fields_refuse(self):
        for changes in ({'model': 'claude-other'}, {'stop_reason': 'max_tokens'}, {'stop_reason': 'tool_use'},
                        {'actions': []}, {'diagnostics': {'private': 'not allowed'}}, {'role': 'user'}):
            value = response()
            value.update(changes)
            self.reject(value)
        for block in ({'type': 'tool_use', 'name': 'message_agent', 'input': {}},
                      {'type': 'image', 'source': {}}, {'type': 'text', 'text': 'answer', 'extra': 1}):
            value = response()
            value['content'] = [block]
            self.reject(value)

    def test_thinking_is_discarded_and_raw_response_never_enters_candidate(self):
        value = response()
        value['content'].insert(0, {'type': 'thinking', 'thinking': 'PRIVATE_FICTIONAL_THINKING', 'signature': 'fictional-signature'})
        answer = self.validate(value)
        self.assertNotIn('PRIVATE_FICTIONAL_THINKING', repr(answer))
        self.assertNotIn('fictional-signature', json.dumps(asdict(answer)))
        self.assertFalse(hasattr(answer, 'raw_response'))
        self.assertEqual(answer.text, response()['content'][0]['text'])

    def test_usage_is_closed_and_not_a_billing_authorization(self):
        for changes in ({'service_tier': 'priority'}, {'speed': 'fast'}, {'inference_geo': 'other'},
                        {'output_tokens': 1025}, {'input_tokens': True}, {'input_tokens': 10001}, {'unexpected': 1},
                        {'cache_creation_input_tokens': 1}, {'output_tokens_details': {'thinking_tokens': 81}}):
            value = response()
            value['usage'].update(changes)
            self.reject(value)
        self.assertEqual(self.validate(response()).usage_facts.inference_geo, 'global')

    def test_canonical_candidate_accepts_exact_boundary_and_rejects_one_more(self):
        baseline = self.validate(response('x', citations=False))
        overhead = len(documents.candidate_bytes(baseline)) - 1
        size = documents.MAX_CANDIDATE_BYTES - overhead
        accepted = self.validate(response('x' * size, citations=False))
        self.assertEqual(len(documents.candidate_bytes(accepted)), 60 * 1024)
        self.assertEqual(65536 - 4 - len(documents.candidate_bytes(accepted)), 4092)
        self.reject(response('x' * (size + 1), citations=False))
        self.assertEqual(len(accepted.consumed_dependencies), 2)

    def test_candidate_fingerprints_bind_exact_question_request_and_response(self):
        raw = wire(response())
        first = documents.validate_response(self.p, self.request, raw)
        self.assertEqual(first.request_digest, hashlib.sha256(self.request.body_bytes).hexdigest())
        self.assertEqual(first.response_digest, hashlib.sha256(raw).hexdigest())
        sources = (source(), source(2, 'image/png'))
        question = 'Answer a different question about the same files.'
        changed_manifest = replace(manifest(self.p, sources, question), request_id='d' * 32)
        second_request = documents.build_input(self.p, changed_manifest, question, sources)
        second = documents.validate_response(self.p, second_request, raw)
        self.assertEqual(first.consumed_dependencies, second.consumed_dependencies)
        self.assertNotEqual(first.request_digest, second.request_digest)
        self.assertNotEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(first.response_digest, second.response_digest)
        # Owner request identity is independently bound by the complete manifest,
        # even when identical questions happen to serialize to identical bodies.
        same_body_selection = replace(self.request.complete_manifest.selection, request_id='e' * 32)
        same_body_request = documents.build_input(self.p, same_body_selection,
            'Read the fictional sources.', sources)
        same_body = documents.validate_response(self.p, same_body_request, raw)
        self.assertEqual(first.request_digest, same_body.request_digest)
        self.assertNotEqual(first.manifest_digest, same_body.manifest_digest)

    def test_swapped_body_or_exact_raw_response_cannot_match_candidate_fingerprint(self):
        raw = wire(response())
        first = documents.validate_response(self.p, self.request, raw)
        # Whitespace changes raw response identity without changing normalized
        # text/citations/usage. It must not recover another result's receipt.
        same_prose = documents.validate_response(self.p, self.request, raw + b'\n')
        self.assertEqual(first.text, same_prose.text)
        self.assertEqual(first.citations, same_prose.citations)
        self.assertEqual(first.usage_facts, same_prose.usage_facts)
        self.assertNotEqual(first.response_digest, same_prose.response_digest)
        changed_raw = wire(response('A different result.\nIMAGE_SOURCE: image-1'))
        changed = documents.validate_response(self.p, self.request, changed_raw)
        self.assertNotEqual(first.response_digest, changed.response_digest)
        question = 'A different question.'
        sources = (source(), source(2, 'image/png'))
        other_request = documents.build_input(self.p, manifest(self.p, sources, question), question, sources)
        other = documents.validate_response(self.p, other_request, raw)
        expected = (first.manifest_digest, self.request.body_digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual((first.manifest_digest, first.request_digest, first.response_digest), expected)
        for swapped in (same_prose, changed, other):
            self.assertNotEqual((swapped.manifest_digest, swapped.request_digest, swapped.response_digest), expected)
        self.assertLessEqual(len(documents.candidate_bytes(first)), documents.MAX_CANDIDATE_BYTES)

    def test_raw_byte_bounds_duplicate_keys_and_bad_internal_manifests(self):
        with self.assertRaises(documents.DocumentRejected):
            documents.validate_response(self.p, self.request, b'x' * (documents.MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(documents.DocumentRejected):
            documents.validate_response(self.p, self.request, b'{"id":"a","id":"b"}')
        with self.assertRaises(documents.DocumentRejected):
            documents.validate_response(self.p, self.request, {'not': 'raw bytes'})
        for changed in (replace(self.request, body_digest='0' * 64), replace(self.request, headers=()),
                        replace(self.request, input_map=self.request.input_map[::-1]),
                        replace(self.request, body_bytes=self.request.body_bytes + b' ')):
            with self.assertRaises(documents.DocumentRejected):
                documents.validate_response(self.p, changed, wire(response()))
        self.assertEqual(documents.validate_response(self.p, self.request, wire(response())), self.validate(response()))


class ReplayInputTests(unittest.TestCase):
    def inputs(self,count=1,p=None):
        p=p or profile();sources=(source(),source(2,'image/png'))
        priors=tuple(documents.PriorAnswerInput(f'{100+n:032x}','c'*32,'a'*64,'b'*64,'d'*64,
                    'Fictional retained answer '+str(n),tuple(x.source_dependency for x in sources)) for n in range(count))
        m=replace(manifest(p,sources),prior_answer_ids=tuple(x.answer_id for x in priors))
        return p,sources,priors,m

    def test_exact_order_and_untrusted_user_blocks_preserve_all_prior_text(self):
        p,sources,priors,m=self.inputs(8)
        request=documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        blocks=json.loads(request.body_bytes)['messages'][0]['content']
        self.assertEqual(len(blocks),len(sources)+8+1)
        for block,prior in zip(blocks[len(sources):-1],priors):
            self.assertEqual(block['type'],'text');self.assertIn('UNTRUSTED PRIOR OFFICER ANSWER',block['text'])
            self.assertIn(prior.text,block['text']);self.assertIn(prior.answer_id,block['text'])
        self.assertEqual(request.complete_manifest.dependencies,tuple(x.source_dependency for x in sources))
        self.assertEqual(len(request.complete_manifest.prior_answer_refs),8)
        self.assertNotIn(priors[0].text,json.dumps(request.complete_manifest.metadata))
        self.assertNotIn(priors[0].text,repr(request))
        documents.validate_response(p,request,wire(response()))

    def test_duplicate_wrong_order_foreign_context_and_wrong_dependency_refuse(self):
        p,sources,priors,m=self.inputs(2)
        bad=(priors[::-1],(priors[0],priors[0]),(replace(priors[0],conversation_id='f'*32),priors[1]),
             (replace(priors[0],dependencies=(replace(priors[0].dependencies[0],access_generation=2),)),priors[1]),
             (replace(priors[0],answer_id=m.request_id),priors[1]))
        for values in bad:
            with self.subTest(values=values),self.assertRaises(documents.DocumentRejected):
                documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=values)

    def test_nine_prior_answers_and_raw_browser_text_refuse(self):
        p,sources,priors,m=self.inputs(9)
        with self.assertRaises(documents.DocumentRejected):documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        with self.assertRaises(documents.DocumentRejected):documents.build_input(p,replace(m,prior_answer_ids=()),'Read the fictional sources.',sources,prior_answers=('browser assistant prose',))

    def test_replay_counts_toward_actual_serialized_body_limit(self):
        p,sources,priors,m=self.inputs(p=profile(max_encoded_bytes=2048))
        priors=(replace(priors[0],text='q'*2048),)
        with self.assertRaises(documents.DocumentRejected) as caught:
            documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        self.assertEqual(caught.exception.reason,'serialized_document_input_bound')

    def test_full_flattened_dependency_closure_and_conflicts(self):
        p,sources,priors,m=self.inputs()
        extra=source(3).source_dependency
        priors=(replace(priors[0],dependencies=priors[0].dependencies+(extra,)),)
        with self.assertRaises(documents.DocumentRejected):documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        m=replace(m,flattened_dependencies=m.flattened_dependencies+(extra,))
        request=documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        answer=documents.validate_response(p,request,wire(response()))
        self.assertEqual(answer.consumed_dependencies,m.flattened_dependencies)

    def test_flattened_dependency_limit_includes_prior_only_sources(self):
        p,sources,priors,m=self.inputs()
        extras=tuple(source(n).source_dependency for n in range(3,52))
        # The prior itself has 50 dependencies; the direct+prior union has 51.
        priors=(replace(priors[0],dependencies=(sources[0].source_dependency,)+extras),)
        m=replace(m,flattened_dependencies=m.flattened_dependencies+extras)
        with self.assertRaises(documents.DocumentRejected):
            documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)

    def test_reference_metadata_mismatch_in_prepared_request_refuses(self):
        p,sources,priors,m=self.inputs()
        request=documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        for change in ({'text':'Changed prose'},{'request_digest':'e'*64},{'manifest_digest':'e'*64},{'answer_digest':'e'*64}):
            changed=replace(request,prior_answers=(replace(priors[0],**change),))
            with self.assertRaises(documents.DocumentRejected):documents.validate_response(p,changed,wire(response()))

    def test_no_replay_metadata_preserves_legacy_manifest_shape_and_digest(self):
        p,request=prepare()
        value=asdict(request.complete_manifest);value.pop('prior_answer_refs')
        self.assertEqual(request.complete_manifest.metadata,value)
        self.assertEqual(request.complete_manifest.digest,hashlib.sha256(documents._json(value)).hexdigest())
        self.assertEqual(request.prior_answers,())

    def test_replay_cannot_add_document_citation_indexes(self):
        p,sources,priors,m=self.inputs()
        request=documents.build_input(p,m,'Read the fictional sources.',sources,prior_answers=priors)
        value=response();value['content'][0]['citations'][0]['document_index']=1
        with self.assertRaises(documents.DocumentRejected):documents.validate_response(p,request,wire(value))


if __name__ == '__main__':
    unittest.main()
