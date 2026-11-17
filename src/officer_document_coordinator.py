"""Owner-authorized document questions, structurally isolated from Motet actions."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import re
import threading
import time
import sys

from attachments_catalogue import HistorySelection
from attachments_conversations import OfficerConversations
from attachments_officer_storage import OfficerDirectRetentionProfile
from attachments_types import AttachmentError, canonical, fail, ident
from officer_document_intents import OfficerIntents
from officer_document_projection import OfficerDocumentProjection
from officer_document_ledger import OfficerDocumentLedger, DocumentBudgetScope, ResultFacts
from officer_policy import AUTOMATIC
import officer_document_policy as policy
import officer_documents as documents


def _presentation(candidate, frozen, dependencies):
    """Read-only display facts; never mutate canonical/replay evidence."""
    def bad():
        fail('document_answer_invalid', 'Motet answer presentation could not be verified.', 409)
    try:
        text = candidate['text']
        entries = frozen['manifest']['input_map']
        ordered = frozen['manifest']['selection']['ordered_sources']
        if (type(text) is not str or not text.strip() or type(entries) is not list
                or not 1 <= len(entries) <= 50 or type(ordered) is not list): bad()
        ids, originals = [], []
        for index, entry in enumerate(entries):
            dep = OfficerDocumentLedger._decode_dependency(canonical(entry['dependency']))
            if (type(entry['input_index']) is not int or entry['input_index'] != index
                    or entry['media_type'] not in ('application/pdf', 'image/png')
                    or entry['preparation_kind'] != 'officer_direct' or dep.source_id in ids): bad()
            ids.append(dep.source_id)
            originals.append(asdict(dep))
        # The current route re-supplies every prior dependency as an original.
        # Do not mistake a future prior-only closure for an original input block.
        if (ids != ordered or canonical(originals) != canonical([asdict(d) for d in dependencies])
                or canonical(originals) != canonical(candidate['consumed_dependencies'])
                or canonical(originals) != canonical(frozen['manifest']['selection']['flattened_dependencies'])): bad()
        refs = candidate['citations']
        if type(refs) is not list or len(refs) > 64: bad()
        spans, markers = [], set()
        for ref in refs:
            index, start, end = ref['input_index'], ref['text_start'], ref['text_end']
            if (type(index) is not int or not 0 <= index < len(entries)
                    or ref['source_id'] != ids[index]
                    or canonical(ref['dependency']) != canonical(originals[index])
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= len(text)): bad()
            if ref['kind'] == 'upload_pdf_pages':
                if entries[index]['media_type'] != 'application/pdf': bad()
            elif ref['kind'] == 'upload_whole_image':
                marker = ref['marker']
                if (entries[index]['media_type'] != 'image/png'
                        or type(marker) is not str or re.fullmatch(r'image-[1-9][0-9]?',marker) is None or marker != entries[index]['image_marker']
                        or marker in markers or ref['provenance'] != 'declared_whole_image_reference'
                        or start == end or text[start:end] != 'IMAGE_SOURCE: ' + marker
                        or start and text[start - 1] != '\n'
                        or end < len(text) and text[end] != '\n' and text[end:end + 2] != '\r\n'): bad()
                markers.add(marker)
                spans.append((start, end))
            else: bad()
        spans.sort()
        if any(start < previous_end for (_, previous_end), (start, _) in zip(spans, spans[1:])): bad()
        display = text
        for start, end in reversed(spans):
            display = display[:start] + display[end:]
        if 'IMAGE_SOURCE' in display: bad()  # Missing/invalid typed spans never leak or strip arbitrary prose.
        return display if display.strip() else '', ids
    except (KeyError, TypeError, ValueError, IndexError, AttachmentError):
        bad()


class DocumentCoordinator:
    def __init__(self, store, allowance, *, selected_model, credential, transport,
                 enabled=False, demo=False, monotonic=time.monotonic):
        if enabled is not True or demo is not False:
            fail('document_disabled','Motet file reading is unavailable.',503)
        if not callable(selected_model) or not callable(credential):policy.unavailable()
        allowance.validate(store.clock)
        self.store,self.allowance,self.profile=store,allowance,policy.input_profile()
        self.selected_model,self.credential,self.transport=selected_model,credential,transport
        self.monotonic=monotonic
        self.ledger=OfficerDocumentLedger(store)
        self.scope=DocumentBudgetScope(allowance.scope_id,allowance.scope_revision,allowance.usd_limit)
        self.ledger.register_scope(self.scope,self.profile,policy.document_pricing())
        self.conversations=OfficerConversations(store)
        self.intents=OfficerIntents(store,self.ledger,self.profile,self.scope.id)
        self.projection=OfficerDocumentProjection(self.intents)
        self.condition=threading.Condition();self.accepting=True;self.active=0
        self.retention=OfficerDirectRetentionProfile('officer-original-storage','v1','local_retention',
            self.profile.qualification_revision,documents.MODEL)

    def qualify(self, binding=None):
        self.allowance.validate(self.store.clock)
        configured=self.selected_model()
        if configured not in (AUTOMATIC,documents.MODEL):
            fail('document_model_unavailable','File reading requires Auto or the qualified Haiku model. Your files remain in this conversation.',503)
        # The saved Officer picker uses its own sentinel. Only that exact value
        # maps to this attachment-safe selected identity; literal "auto" is not
        # an authorized provider model choice.
        selected='auto' if configured==AUTOMATIC else configured
        if binding is not None and (binding['kind']!='officer' or binding['id']!='officer'
                or binding['modelId']!=selected or binding['capabilityRevision']!=self.profile.qualification_revision):
            fail('document_context_changed','The Motet file-reading context changed.',409)
        secret=self.credential()
        if type(secret) is not str or not secret:
            fail('document_credential_unavailable','Motet file reading is unavailable on this host.',503)
        secret=None
        return selected

    def retention_for(self,binding):
        self.qualify(binding)
        # Local retention follows the exact picker selection. The independent
        # qualified input profile, provider request and billing remain Haiku.
        return replace(self.retention,model_id=binding['modelId'])

    @contextmanager
    def operation(self):
        with self.condition:
            if not self.accepting or self.active>=2:
                fail('document_busy','Motet is busy with file questions.',429)
            self.active+=1
        try:yield
        finally:
            with self.condition:
                self.active-=1;self.condition.notify_all()

    def close(self,timeout=125):
        with self.condition:
            self.accepting=False
            if not self.condition.wait_for(lambda:self.active==0,timeout):
                fail('document_busy','Motet file operations are still active.',409)
        self.transport.close(timeout=0)

    def prepare(self,draft_id,capability,fence,command_key,binding,*,kept_file_ids=None):
        retention=self.retention_for(binding)
        return self.intents.prepare_draft(draft_id,capability,fence,command_key,binding,retention,kept_file_ids=kept_file_ids)

    def send(self,request_id,request_capability,draft_capability,fence,fingerprint):
        with self.operation():
            record,receipt=self.intents.authorize(request_id,request_capability)
            if record['kind']!='draft':
                fail('request_conflict','Choose the exact attachment draft request.',409)
            if receipt['phase']=='prepared':self.qualify(record['binding'])
            record,won=self.intents.consume(request_id,request_capability,draft_capability=draft_capability,
                fence=fence,fingerprint=fingerprint)
            if not won:return self.intents.view(record)
            return self._execute(record,won,draft_capability,fence)

    def prepare_question(self,binding,question,prior_answers,command_key,*,prior_answer_policy=None,kept_file_ids=None):
        retention=self.retention_for(binding)
        return self.intents.prepare_question(binding,question,prior_answers,retention,command_key,
                                             prior_answer_policy=prior_answer_policy,kept_file_ids=kept_file_ids)

    def send_question(self,request_id,request_capability,fingerprint):
        with self.operation():
            record,receipt=self.intents.authorize(request_id,request_capability)
            if record['kind']!='question':
                fail('request_conflict','Choose the exact Motet question request.',409)
            if receipt['phase']=='prepared':self.qualify(record['binding'])
            record,won=self.intents.consume(request_id,request_capability,fingerprint=fingerprint)
            return self._execute(record,won) if won else self.intents.view(record)

    def ask(self,binding,question,prior_answers,command_key,*,kept_file_ids=None):
        with self.operation():
            retention=self.retention_for(binding)
            prepared=self.intents.prepare_question(binding,question,prior_answers,retention,command_key,kept_file_ids=kept_file_ids)
            record,won=self.intents.consume(prepared['requestId'],prepared['requestCapability'],fingerprint=prepared['fingerprint'])
            out=self._execute(record,won) if won else self.intents.view(record)
            return dict(out,requestCapability=prepared['requestCapability'])

    def _facts(self,execution,state,error,*,raw=None,candidate=None):
        return ResultFacts(execution['id'],execution['claimNonce'],execution['bodyDigest'],execution['profileDigest'],
            state,hashlib.sha256(raw).hexdigest() if raw is not None else '',
            candidate.message_id if candidate is not None else '',documents.MODEL,policy.BILLING_REVISION,
            candidate.usage_facts if candidate is not None else None,error)

    def _execute(self,record,winner,draft_capability=None,fence=None):
        """Only the consume winner enters. Recovery and status never enter here."""
        from officer_document_transport import OwnedDocumentResult, TransportFailure, TransportRejected
        selection=HistorySelection.from_binding(record['binding'])
        execution=prepared=permit=lease=result=candidate=raw=None
        error='none';message=None
        try:
            retention=self.retention_for(record['binding'])
            if record['question'].strip() and 'kept_file_ids' not in record:
                fail('document_tray_required','Choose exactly which files go in this question.',409)
            if record['kind']=='draft':
                if record['question'].strip():
                    with self.store._gate():
                        kept=tuple(record['kept_file_ids'])
                        _,rows=self.ledger._selected(selection,kept) if kept else ((),())
                        if (len(self.intents.file_ids(record))>min(5,self.store.limits['files'])
                                or sum(row['bytes'] for row in rows)+sum(row['bytes'] for row in record['sources'])
                                    >min(300000,self.store.limits['draft_bytes'])):
                            fail('document_tray_limit','Choose at most five files and 300 KB for this question.',409)
                message=self.conversations.admit(record['draft_id'],draft_capability,fence,
                    self.intents.command(record,'local'),record['binding'],retention)
            if not record['question'].strip():
                self.intents.finish(record['id'],winner)
                return self.intents.view(record)
            kwargs={'local_message_id':message['id'] if message is not None else None}
            if record['prior_answers']:kwargs['prior_answer_ids']=tuple(record['prior_answers'])
            ids=self.intents.file_ids(record)
            with self.store._gate():
                self.ledger._selected(selection,ids)
            kwargs['file_ids']=ids
            lease=self.store.files.buffer_lease(ids)
            execution=self.ledger.prepare(selection,self.profile,self.scope.id,self.intents.command(record,'execution'),record['question'],**kwargs)
            # Metadata is not a pin; claim revalidates every exact source after pinning.
            if set(ids)!=set(self.ledger.source_ids(selection,execution['id'])):
                fail('document_roster_changed','The Motet file context changed.',409)
            self.qualify(record['binding'])
            execution,permit,prepared=self.ledger.claim(selection,execution['id'])
            if permit is None:
                fail('document_claim_consumed','This Motet request has already been attempted.',409)
            seconds=min(120.0,policy.VALID_UNTIL-self.store.clock())
            if seconds<=0:policy.unavailable()
            try:
                self.qualify(record['binding'])
            except Exception:
                # Transport has not received ownership of this one-use permit.
                self.ledger.retire_permit(permit)
                raise
            result=self.transport.call(prepared,self.ledger,permit,lease,self.credential,self.monotonic()+seconds)
            if isinstance(result,TransportFailure) and result.error=='source_withdrawn' and result.dispatch_started is False:
                # The worker settled the local reservation at its no-POST boundary.
                error='tray_unavailable'
            elif isinstance(result,TransportFailure):
                known_rejection=result.http_status is not None and result.http_status>=300
                self.ledger.record_result(selection,self._facts(execution,'failed' if known_rejection else 'unknown',
                    'rejected' if known_rejection else 'transport_unknown'))
            elif type(result) is OwnedDocumentResult:
                raw=result.raw_response
                candidate=documents.validate_response(self.profile,prepared,raw)
                self.ledger.record_result(selection,self._facts(execution,'accepted','none',raw=raw,candidate=candidate))
                self.ledger.commit_answer(selection,execution['id'],candidate)
            else:
                fail('document_result_unavailable','The Motet result could not be verified.',503)
        except Exception as problem:
            # Normalize ordinary failures without retaining their traceback, which
            # can itself own source/response buffers until this handler exits.
            error='execution_refused' if execution is None or execution['state']=='prepared' else 'result_unavailable'
            if error=='execution_refused':
                code = problem.code if isinstance(problem, AttachmentError) else problem.reason if isinstance(problem, documents.DocumentRejected) else None
                error = {'document_budget_exhausted':'budget_exhausted',
                         'serialized_document_input_bound':'context_too_large',
                         'document_policy_unavailable':'reading_unavailable',
                         'document_model_unavailable':'reading_unavailable',
                         'document_credential_unavailable':'reading_unavailable',
                         'document_budget_halted':'reading_unavailable',
                         'document_tray_source_unavailable':'tray_unavailable'}.get(code,error)
            if execution is not None and execution['state']!='prepared':
                try:self.ledger.record_result(selection,self._facts(execution,'unknown','validation_failed'))
                except AttachmentError:pass  # Never overwrite a stronger correlated result.
        finally:
            candidate=raw=prepared=None
            if sys.exc_info()[1] is None and result is not None and type(result) is OwnedDocumentResult:
                result.close()
            if sys.exc_info()[1] is None and lease is not None:
                # Only a still-owned preparation lease may be closed here. The
                # transport exclusively owns transferred worker/result lifetimes.
                try:lease.close()
                except AttachmentError:pass
            self.intents.finish(record['id'],winner,error)
        return self.intents.view(record)

    def status(self,request_id,capability):
        record,_=self.intents.authorize(request_id,capability)
        return self.intents.view(record)

    def recover(self,request_id,binding,fingerprint):
        return self.intents.recover(request_id,binding,fingerprint)

    def publish(self,answer_id,request_id,capability,publish):
        record,_=self.intents.authorize(request_id,capability)
        _,execution=self.intents.correlated(record)
        if execution is None or execution['id']!=ident(answer_id):
            fail('forbidden','That Motet answer belongs to another request.',403)
        selection=HistorySelection.from_binding(record['binding'])
        def value(candidate):
            _,frozen,receipt,dependencies=self.ledger._request(selection,answer_id)
            display,supplied=_presentation(candidate,frozen,dependencies)
            window=record.get('prior_answer_window')
            if window is not None and frozen['manifest']['selection']['prior_answer_ids']!=record['prior_answers']:
                fail('document_answer_invalid','Motet answer context could not be verified.',409)
            cites=[]
            for n,ref in enumerate(candidate['citations']):
                location={'page':ref['start_page']} if ref['kind']=='upload_pdf_pages' else {'image':True}
                cites.append({'id':answer_id+':'+str(n),'kind':'upload','fileId':ref['source_id'],
                    'activityId':None,'label':'PDF page '+str(location['page']) if 'page' in location else 'Whole image',
                    'location':location,'available':True,'sourceCapability':self.projection.citation_authority_checked(
                        request_id,capability,answer_id,ref['source_id'],location,candidate)})
            return publish({'id':answer_id,'requestId':request_id,'state':'available','text':display,'suppliedFileIds':supplied,
                'priorAnswerWindow':window,'answeredAt':self.ledger._answered_at(receipt),
                'model':{'id':documents.MODEL,'label':'Haiku 5.5'},'analysisOnly':True,
                'sourceValidation':'current','citations':cites,'actions':[]})
        try:return self.ledger.publish(selection,answer_id,value)
        except AttachmentError as exc:
            if exc.code not in ('document_answer_unavailable','document_answer_revoked','source_changed','source_unavailable'):
                raise
            return publish({'id':answer_id,'requestId':request_id,'state':'source_unavailable','text':None,'suppliedFileIds':[],
                'priorAnswerWindow':None,'answeredAt':execution['answeredAt'],
                'model':{'id':documents.MODEL,'label':'Haiku 5.5'},'analysisOnly':True,
                'sourceValidation':'invalid','citations':[],'actions':[]})
