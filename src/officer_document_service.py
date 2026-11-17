"""Strict attachment service extension for qualified, analysis-only Motet files."""
from dataclasses import asdict

from attachments_catalogue import HistorySelection
from attachments_service import AttachmentService, UnqualifiedResolver, FENCE, SELECTION, closed, opaque
from attachments_types import AttachmentError, fail
from officer_document_coordinator import DocumentCoordinator
import officer_document_policy as policy
import officer_documents as documents


class OfficerScopeResolver(UnqualifiedResolver):
    def __init__(self, known_scopes):self.known_scopes=known_scopes
    def resolve_scope(self, scope_id):
        opaque(scope_id)
        if scope_id not in self.known_scopes():
            fail('destination_unavailable','Choose a registered Motet room or station.',503)
        return {'id':scope_id}


class OfficerAttachmentService(AttachmentService):
    def __init__(self,store,*,coordinator,known_scopes):
        super().__init__(store,resolver=OfficerScopeResolver(known_scopes))
        if coordinator.store is not store:
            fail('document_context_changed','Motet attachment storage changed.',503)
        self.documents=coordinator

    def close(self,timeout=125):
        with self.condition:self.accepting=False
        self.documents.close(timeout)
        super().close(timeout)

    def capabilities(self,destination_id,scope_id,model_id):
        opaque(destination_id);opaque(scope_id)
        if model_id is not None:opaque(model_id)
        if destination_id!='officer':return super().capabilities(destination_id,scope_id,model_id)
        try:
            selected=self.documents.qualify()
            self.resolver.resolve_scope(scope_id)
            if model_id!=selected:policy.unavailable()
        except AttachmentError:
            return {'available':False,'reason':'Motet file reading requires the qualified model and host policy.',
                    'destinationId':destination_id,'scopeId':scope_id,'modelId':model_id}
        formats=['application/pdf','image/png']
        return {'available':True,'kind':'officer','destinationId':destination_id,'scopeId':scope_id,
            'modelId':model_id,'effectiveModelId':documents.MODEL,
            'capabilityRevision':self.documents.profile.qualification_revision,
            'formats':formats,'limits':dict(self.store.limits_view(),fileBytes=min(self.store.limits['file_bytes'],300000),draftBytes=min(self.store.limits['draft_bytes'],300000),documentEncodedBytes=self.documents.profile.max_encoded_bytes),
            'preparationModes':{f:'officer_direct' for f in formats},'analysisOnly':True}

    def mutate(self,operation,draft_id,capability,data,*,command_key=None,file_id=None):
        if operation!='reserve':
            return super().mutate(operation,draft_id,capability,data,command_key=command_key,file_id=file_id)
        closed(data,FENCE|{'name','mediaType','bytes','digest'})
        draft=self.store.get_draft(draft_id,capability);binding=draft['destination']
        if binding['kind']!='officer':
            return super().mutate(operation,draft_id,capability,data,command_key=command_key,file_id=file_id)
        retention=self.documents.retention_for(binding)
        if data['mediaType'] not in ('application/pdf','image/png'):
            fail('preparation_unavailable','This file type is not qualified for Motet.',503)
        return self.store.reserve_upload(draft_id,capability,{k:data[k] for k in FENCE},
            {k:data[k] for k in ('name','mediaType','bytes','digest')},command_key,
            preparation_kind='officer_direct',officer_profile=retention,
            admission_limits={'fileBytes':min(self.store.limits['file_bytes'],300000),
                'draftBytes':min(self.store.limits['draft_bytes'],300000),'fileCount':self.store.limits['files']})

    def document_prepare(self,draft_id,capability,data,command_key):
        fields=FENCE|{'keptFileIds'} if type(data) is dict and 'keptFileIds' in data else FENCE
        closed(data,fields)
        kept=data['keptFileIds'] if 'keptFileIds' in data else []
        self.documents.intents._kept_ids(kept)
        binding=self.store.get_draft(draft_id,capability)['destination']
        return self.documents.prepare(draft_id,capability,{k:data[k] for k in FENCE},command_key,binding,kept_file_ids=kept)

    def document_send(self,request_id,request_capability,draft_capability,data,command_key):
        from attachments_types import key
        key(command_key);closed(data,FENCE|{'fingerprint'})
        # The durable intent, not this transport retry key, is the one-attempt identity.
        return self.documents.send(request_id,request_capability,draft_capability,{k:data[k] for k in FENCE},data['fingerprint'])

    def document_status(self,request_id,capability):return self.documents.status(request_id,capability)

    def document_abandon(self,request_id,capability,data,command_key):
        from attachments_types import key
        key(command_key);closed(data,{'fingerprint'})
        return self.documents.intents.abandon(request_id,capability,data['fingerprint'])

    def document_recover(self,request_id,data):
        closed(data,SELECTION|{'fingerprint'})
        # Recovery is not qualification, admission or scope discovery.
        from attachments_types import destination
        binding=destination({'kind':'officer','id':data['destinationId'],'scopeId':data['scopeId'],
            'modelId':data['modelId'],'conversationId':data['conversationId'],'capabilityRevision':data['capabilityRevision']})
        return self.documents.recover(request_id,binding,data['fingerprint'])

    def document_ask(self,conversation_id,data,command_key,*,prepare_only=False):
        fields = SELECTION | {'question','priorAnswerIds','keptFileIds'}
        policy = None
        if prepare_only and type(data) is dict and 'priorAnswerPolicy' in data:
            from officer_document_intents import RECENT_ANSWER_POLICY
            fields = fields | {'priorAnswerPolicy'}
            policy = data['priorAnswerPolicy']
            if policy != RECENT_ANSWER_POLICY:
                fail('invalid_request', 'Choose a supported Motet prior-answer policy.', 400)
        closed(data,fields)
        self.documents.intents._kept_ids(data['keptFileIds'])
        if not data['keptFileIds']:
            fail('files_required','Choose at least one file in this question.',400)
        if conversation_id!=data['conversationId']:
            fail('forbidden','Choose the exact Motet conversation.',403)
        from attachments_types import destination
        binding=destination({'kind':'officer','id':data['destinationId'],'scopeId':data['scopeId'],
            'modelId':data['modelId'],'conversationId':data['conversationId'],'capabilityRevision':data['capabilityRevision']})
        # An existing owner conversation remains usable when its station is offline.
        with self.store._gate():
            self.documents.conversations._selection(HistorySelection.from_binding(binding))
        if prepare_only:
            return self.documents.prepare_question(binding,data['question'],data['priorAnswerIds'],command_key,
                                                   prior_answer_policy=policy,kept_file_ids=data['keptFileIds'])
        return self.documents.ask(binding,data['question'],data['priorAnswerIds'],command_key,kept_file_ids=data['keptFileIds'])

    def document_send_question(self,request_id,capability,data,command_key):
        from attachments_types import key
        key(command_key);closed(data,{'fingerprint'})
        return self.documents.send_question(request_id,capability,data['fingerprint'])

    def document_history(self,conversation_id,destination_id,scope_id,cursor=None):
        binding={'kind':'officer','id':destination_id,'scopeId':scope_id,'conversationId':conversation_id,
            'modelId':documents.MODEL,'capabilityRevision':self.documents.profile.qualification_revision}
        return self.documents.intents.history(binding,cursor)

    def document_by_prepare(self,conversation_id,destination_id,scope_id,prepare_digest):
        return self.documents.intents.by_prepare(HistorySelection('officer',destination_id,scope_id,conversation_id),
                                                 prepare_digest)

    def document_answer(self,answer_id,request_id,capability,publish):
        return self.documents.publish(answer_id,request_id,capability,publish)

    def history(self,destination_id,scope_id,conversation_id,*,cursor=None,request_id=None):
        if conversation_id is None:return super().history(destination_id,scope_id,conversation_id,cursor=cursor)
        selection=HistorySelection('officer',destination_id,scope_id,conversation_id)
        return self.documents.projection.history(selection,cursor=cursor,request_id=request_id)

    def preview(self,file_id,capability,location,publish):
        from officer_document_projection import DOMAIN
        try:self.catalogue._unseal(DOMAIN,capability)
        except AttachmentError:
            return super().preview(file_id,capability,location,publish)
        return self.documents.projection.preview(file_id,capability,location,publish)
