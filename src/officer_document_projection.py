"""Metadata-only Motet retained-file projection; no renderer or bytes endpoint.

Internal API: history(selection, cursor=None, request_id=None) projects the exact
optional current public intent, otherwise the file's original admission intent.
preview(file_id, capability, location, publish) emits only guarded metadata.
citation_authority(...) validates an answer with ledger.publish; its trusted
citation_authority_checked variant is for use INSIDE an existing publish callback.
Both opening paths revalidate source generation/context; citation opening also
re-enters governed answer publication. Capabilities expire with this instance.
"""
from dataclasses import asdict
import hashlib
import json
import secrets

from attachments_catalogue import HistorySelection
from attachments_types import digest, fail, ident, integer
from officer_document_ledger import MAX_DEPENDENTS, MAX_REQUESTS

DOMAIN = 'officer-document-source'


def refuse():
    fail('document_file_evidence_unavailable', 'The retained file evidence could not be verified.', 409)


class OfficerDocumentProjection:
    def __init__(self, intents):
        self.intents, self.store, self.ledger = intents, intents.store, intents.execution
        self.conversations, self.catalogue = intents.conversations, intents.catalogue
        self.boot = secrets.token_hex(32)

    def _row(self, selection, source_id):
        self.conversations._selection(selection)
        row = self.store.db.execute('SELECT '+self.conversations._COLUMNS+' '+self.conversations._JOIN+
            ' WHERE mf.source_id=?', (ident(source_id),)).fetchone()
        if row is None or not self.conversations._matches(row, selection): refuse()
        return row

    @staticmethod
    def _source_match(dep, row, selection, epoch):
        # Historical delivery belongs to this exact original, including after
        # availability revocation; it never makes that original available again.
        return (dep.owner_epoch == epoch and dep.destination_id == selection.destination_id
            and dep.scope_id == selection.scope_id and dep.conversation_id == selection.conversation_id
            and dep.message_id == row['message_id'] and dep.source_id == row['source_id']
            and dep.source_generation == row['source_generation'] and dep.access_generation == row['access_generation']
            and dep.sha256 == row['digest'] and digest(list(dep.identity)) == digest(json.loads(row['identity'])))

    def _context(self, selection):
        self.conversations._selection(selection)
        maximum = self.store.limits['requests']
        rows = self.store.db.execute('SELECT id FROM officer_document_intents WHERE scope_id=? AND conversation_id=? ORDER BY id LIMIT ?',
            (selection.scope_id, selection.conversation_id, maximum+1)).fetchall()
        if len(rows)>maximum: refuse()
        records, executions = {}, {}
        for row in rows:
            record, receipt = self.intents._record(row['id'])
            if HistorySelection.from_binding(record['binding']) != selection: refuse()
            command = hashlib.sha256(self.intents.command(record,'execution').encode()).hexdigest()
            if command in executions: refuse()
            executions[command] = record['id']
            records[record['id']] = (record, receipt)
        # A missing reverse map cannot be hidden by a missing source index row.
        # These indexed metadata rows are globally capped by the execution ledger.
        limit = min(MAX_REQUESTS,self.store.limits['officer_messages'])
        retained = self.store.db.execute('SELECT id,command_digest FROM officer_document_requests WHERE destination_id=? AND scope_id=? AND conversation_id=? ORDER BY id LIMIT ?',
            (selection.destination_id,selection.scope_id,selection.conversation_id,limit+1)).fetchall()
        if len(retained)>limit or any(row['command_digest'] not in executions for row in retained): refuse()
        return records, executions, {}

    def _correlated(self, context, record):
        # Per-operation metadata cache only, used while the store gate is held;
        # never reuse across history pages, preview opens or mutation boundaries.
        cache=context[2]
        if record['id'] not in cache:cache[record['id']]=self.intents.correlated(record)
        return cache[record['id']]

    def _evidence(self, selection, row, context, request_id):
        records, commands, _ = context
        dependencies = self.store.db.execute('SELECT request_id FROM officer_document_dependencies WHERE source_id=? ORDER BY request_id LIMIT ?',
            (row['source_id'], MAX_DEPENDENTS+1)).fetchall()
        if len(dependencies)>MAX_DEPENDENTS: refuse()
        attempts, execution_ids = {}, {}
        for dep_row in dependencies:
            execution, frozen, receipt, deps = self.ledger._request(selection, dep_row['request_id'])
            matching = [d for d in deps if d.source_id==row['source_id']]
            if len(matching)!=1 or not self._source_match(matching[0],row,selection,self.store.owner_epoch): refuse()
            intent_id = commands.get(execution['command_digest'])
            if intent_id is None: refuse()  # Missing mapping is never no-call.
            record, _ = records[intent_id]
            _, correlated = self._correlated(context,record)
            if correlated is None or correlated['id']!=execution['id']: refuse()
            execution_ids[intent_id] = execution['id']
            attempts[intent_id] = 'confirmed' if receipt['state']=='accepted' else 'none' if receipt['state']=='prepared' else 'unknown'
        origin = None
        # Exact admission receipt, never a timestamp or newest-request heuristic.
        draft_id = self.conversations._message(row['message_id'])['draft_id']
        for intent_id, (record, receipt) in records.items():
            if record['kind']=='draft' and record['draft_id']==draft_id:
                admission, _ = self._correlated(context,record)
                if admission is not None:
                    if admission['id']!=row['message_id'] or receipt['phase']=='prepared': refuse()
                    if type(record['sources']) is not list or any(type(source) is not dict for source in record['sources']): refuse()
                    sources=[source for source in record['sources'] if source.get('id')==row['source_id']]
                    if len(sources)!=1: refuse()
                    source=sources[0]
                    original={'draft_id':draft_id,'generation':row['source_generation'],
                        'access_generation':row['access_generation'],'identity':row['identity'],
                        'digest':row['digest'],'media':row['media'],'bytes':row['bytes'],
                        'name':row['name'],'preparation_kind':row['preparation_kind']}
                    if json.dumps({key:source.get(key) for key in original},sort_keys=True)!=json.dumps(original,sort_keys=True): refuse()
                    if origin is not None: refuse()
                    origin = intent_id
        current = request_id if request_id is not None else origin
        current_execution = None
        if current is not None:
            ident(current)
            if current not in records: refuse()
            record, current_receipt = records[current]
            if 'kept_file_ids' in record and row['source_id'] not in self.intents.file_ids(record):
                refuse()
            admission, execution = self._correlated(context,record)
            current_execution = execution
            if current not in attempts and (admission is None or admission['id']!=row['message_id']):
                # Before execution, only the admitted draft or a selected kept
                # source can be tied to the question. Afterwards require the
                # frozen dependency record.
                retained_draft = (record['kind']=='draft' and admission is not None
                    and current_receipt['phase'] in ('running','ended'))
                if execution is not None or not (record['kind']=='question' or retained_draft): refuse()
            view = self.intents.view(record)
            delivery = dict(view['officerDelivery'])
            delivery['requestId'] = current
            officer_state = view['state']
            if execution is None and current_receipt['phase']=='running': delivery['state']='unknown'
        else:
            delivery = {'state':'retained_local','requestId':None}
            officer_state = 'retained_local'
        earlier = [state for intent,state in attempts.items() if intent!=current]
        # A live or uncertain consumed intent without an execution mapping cannot
        # establish no transmission. Examine complete context, not a history page.
        for intent_id,(record,receipt) in records.items():
            if intent_id==current or intent_id in attempts: continue
            _, execution = self._correlated(context,record)
            if (execution is None and receipt['phase']=='running' and
                    ('kept_file_ids' not in record or row['source_id'] in self.intents.file_ids(record))):
                earlier.append('unknown')
        causal_ids = ()
        if current_execution is not None:
            _,frozen,_,_=self.ledger._request(selection,current_execution['id'])
            causal_ids=tuple(frozen['manifest']['selection']['prior_answer_ids'])
        # No acceptance timestamps exist. Only frozen governed prior-answer IDs
        # prove that a confirming response preceded this exact prepared attempt.
        causal_confirmed=any(intent!=current and state=='confirmed' and execution_ids[intent] in causal_ids
                             for intent,state in attempts.items())
        prior = 'confirmed' if causal_confirmed else 'unknown' if any(state in ('confirmed','unknown') for state in earlier) else 'none'
        confirmed = sorted(intent for intent,state in attempts.items() if state=='confirmed')
        card = dict(delivery)
        if confirmed:
            # Stable exact confirming witness, not a guessed chronological order.
            card = {'state':'confirmed','requestId':current if current in confirmed else confirmed[0]}
        elif card['state']=='not_sent':
            card = {'state':'retained_local','requestId':current}
        return officer_state, delivery, current, prior, card, origin

    def _authority(self, selection, row, request_id, *, answer_id=None, citation=None):
        return self.catalogue._seal(DOMAIN, {'selection':asdict(selection),'source':row['source_id'],
            'dependency':digest(asdict(self.conversations._dependency(row))), 'request':request_id,
            'answer':answer_id,'citation':citation,'boot':self.boot})

    def history(self, selection, *, cursor=None, request_id=None):
        with self.store._gate():
            context = self._context(selection)
            result = self.conversations.conversation_files(selection,cursor=cursor)
            items = []
            for item in result['items']:
                row = self._row(selection,item['id'])
                state, delivery, current, prior, card, origin = self._evidence(selection,row,context,request_id)
                item = dict(item, admissionRequestId=origin, officerDelivery=card, previewAvailable=False, previewable=False, sourceCapability=None)
                if item['available'] and row['preparation_kind']=='officer_direct' and row['media'] in ('application/pdf','image/png'):
                    item['sourceCapability'] = self._authority(selection,row,current)
                    item['previewable'] = True
                items.append(item)
            return dict(result,items=items)

    @staticmethod
    def _location(location):
        if location is None: return None
        if type(location) is dict and set(location)=={'page'} and integer(location['page'],1,100000): return dict(location)
        if type(location) is dict and set(location)=={'image'} and location['image'] is True: return {'image':True}
        fail('invalid_location','Choose an exact PDF citation page or image.',400)

    @staticmethod
    def _citation(candidate, source_id, location):
        if type(candidate) is not dict or type(candidate.get('citations')) is not list: refuse()
        for citation in candidate['citations']:
            if citation['source_id']!=source_id: continue
            if (citation['kind']=='upload_pdf_pages' and location=={'page':citation['start_page']}
                    or citation['kind']=='upload_whole_image' and location=={'image':True}): return
        refuse()

    def citation_authority_checked(self, request_id, request_capability, answer_id, file_id, location, candidate):
        """Trusted internal seam, ONLY inside ledger.publish's guarded callback."""
        location=self._location(location)
        with self.store._gate():
            record,_=self.intents.authorize(request_id,request_capability)
            _,execution=self.intents.correlated(record)
            if execution is None or execution['id']!=ident(answer_id):refuse()
            selection=HistorySelection.from_binding(record['binding'])
            row=self._row(selection,file_id)
            if not self.conversations._available(row):refuse()
            self._citation(candidate,file_id,location)
            return self._authority(selection,row,request_id,answer_id=answer_id,citation=location)

    def citation_authority(self, request_id, request_capability, answer_id, file_id, location):
        record,_=self.intents.authorize(request_id,request_capability)
        selection=HistorySelection.from_binding(record['binding'])
        return self.ledger.publish(selection,answer_id,lambda candidate:self.citation_authority_checked(
            request_id,request_capability,answer_id,file_id,location,candidate))

    def preview(self, file_id, capability, location, publish):
        if not callable(publish):refuse()
        location=self._location(location)
        with self.store._gate():
            authority=self.catalogue._unseal(DOMAIN,capability)
            if (set(authority)!={'selection','source','dependency','request','answer','citation','boot','epoch','v'}
                    or authority['source']!=ident(file_id) or authority['boot']!=self.boot):refuse()
            try: selection=HistorySelection(**authority['selection'])
            except (TypeError,ValueError):refuse()
        def emit(candidate=None):
            with self.store._gate():
                row=self._row(selection,file_id)
                if (not self.conversations._available(row) or row['preparation_kind']!='officer_direct'
                        or row['media'] not in ('application/pdf','image/png')
                        or digest(asdict(self.conversations._dependency(row)))!=authority['dependency']):refuse()
                if authority['answer'] is not None:
                    if location!=authority['citation']:refuse()
                    self._citation(candidate,file_id,location)
                    record,_=self.intents._record(authority['request'])
                    _,execution=self.intents.correlated(record)
                    if execution is None or execution['id']!=authority['answer']:refuse()
                elif location is not None:refuse()
                state,delivery,_,prior,card,_=self._evidence(selection,row,self._context(selection),authority['request'])
                if authority['answer'] is not None:
                    state='cited_page' if 'page' in location else 'cited_whole'
                elif delivery['state']=='confirmed':state='supplied'
                elif delivery['state']=='unknown' or state=='preparing':state='unknown'
                else:state='retained_local'
                prior=prior if state=='retained_local' else None
                body={
                    'retained_local': {
                        'none':'This file is kept in this Motet conversation. Motet was not asked, so nothing was sent to its model. Motet has not opened this file.',
                        'confirmed':'This file is kept in this Motet conversation. Motet was not asked this time; an earlier question already gave it this file. Motet has not opened this file.',
                        'unknown':'This file is kept in this Motet conversation. Motet was not asked this time. Motet has not opened this file.'}.get(prior),
                    'supplied':'This file was supplied to the model as it is. Motet does not open this kind of file.',
                    'unknown':'Motet could not confirm that the model received this file. Motet does not open this kind of file.',
                    'cited_page':'Motet cited page '+str(location['page'] if location and 'page' in location else '')+' of this file. Motet does not open this kind of file, so it cannot show the page.',
                    'cited_whole':'Motet used this image as a whole. Motet does not open this kind of file.'}[state]
                return publish({'available':False,'kind':'fallback',
                    'fileId':file_id,'name':row['name'],'mediaType':row['media'],'bytes':row['bytes'],
                    'officerState':state,'officerPriorDelivery':prior,'officerDelivery':card,
                    'location':None,'pageCount':None,'lineCount':None,'navigation':{},
                    'payload':{'noticeParts':{'title':'No preview for this kind of file yet','body':body},
                        'footer':'Nothing was checked or drawn.'}})
        if authority['answer'] is not None:
            return self.ledger.publish(selection,authority['answer'],emit)
        return emit()
