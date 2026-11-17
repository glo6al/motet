"""Durable owner Send intent, separate from local retention and paid execution.

Recovery is read-only. No method here opens a provider connection or repeats an
admission. The coordinator owns the single in-process continuation after consume.
"""
from dataclasses import asdict
import hashlib
import hmac
import json
import math
import os
import secrets
import threading

from attachments_catalogue import Catalogue, HistorySelection
from attachments_conversations import OfficerConversations
from attachments_types import AttachmentError, TOKEN, canonical, destination, digest, fail, ident, integer, key
from officer_document_ledger import pack, unpack
import officer_documents as documents

SLOT = 4096
DOMAIN = 'officer-document-request'
RECENT_ANSWER_POLICY = 'recent_available_8'
RECORD_FIELDS = {'kind','binding','draft_id','fence','question','prior_answers','profile','sources',
                 'local_intent','input_profile','budget_scope','id','epoch','created','fingerprint'}
# One identity for this process, shared by every coordinator instance.
_PROCESS_NONCE = secrets.token_hex(32)

def process_identity():
    return hashlib.sha256((_PROCESS_NONCE + ':' + str(os.getpid())).encode()).hexdigest()


def reject():
    fail('document_request_conflict', 'The Motet document request could not be verified.', 409)


def window_changed():
    fail('document_prior_window_changed', 'The available Motet answers changed. Look again before sending.', 409)


def migrate(store):
    if not store.db.in_transaction or store.db.execute('PRAGMA user_version').fetchone()[0] != 9:
        reject()
    for statement in (
        '''CREATE TABLE officer_document_intents(id TEXT PRIMARY KEY,
          command_hash TEXT UNIQUE NOT NULL, draft_id TEXT, scope_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL, record TEXT NOT NULL CHECK(length(record)<=65536),
          receipt BLOB NOT NULL CHECK(length(receipt)=4096)) WITHOUT ROWID''',
        'CREATE INDEX officer_intent_draft ON officer_document_intents(draft_id,id)',
        'CREATE INDEX officer_intent_context ON officer_document_intents(scope_id,conversation_id,id)',
        '''CREATE TRIGGER officer_document_intent_immutable BEFORE UPDATE OF id,command_hash,draft_id,scope_id,conversation_id,record
          ON officer_document_intents BEGIN SELECT RAISE(ABORT,'immutable Officer intent'); END'''):
        store.db.execute(statement)
    store.checkpoint('officer_document_intents_before_version')
    store.db.execute('PRAGMA user_version=10')


class OfficerIntents:
    def __init__(self, store, execution, input_profile, budget_scope):
        if execution.store is not store:
            reject()
        self.store, self.execution = store, execution
        self.input_profile, self.budget_scope = input_profile, budget_scope
        self.catalogue, self.conversations = Catalogue(store), OfficerConversations(store)
        self.boot = secrets.token_hex(32)
        self.process = process_identity()
        self._winners = {}
        self._winner_lock = threading.Lock()

    def command(self, record, operation):
        if operation not in ('local', 'execution'):
            reject()
        return hmac.new(self.store.secret, canonical([DOMAIN, operation, self.store.owner_epoch,
            record['id'], record['fingerprint']]).encode(), hashlib.sha256).hexdigest()

    def _record(self, request_id):
        if self.process != process_identity(): reject()
        row = self.store.db.execute('SELECT * FROM officer_document_intents WHERE id=?', (ident(request_id),)).fetchone()
        if row is None:
            reject()
        try:
            record = json.loads(row['record'])
            if (record['id'] != request_id or record['epoch'] != self.store.owner_epoch
                    or record['draft_id']!=row['draft_id'] or record['binding']['scopeId']!=row['scope_id']
                    or record['binding']['conversationId']!=row['conversation_id']
                    or not RECORD_FIELDS <= set(record) <= RECORD_FIELDS | {'prior_answer_window','kept_file_ids'}
                    or record['kind'] not in ('draft','question')
                    or (record['draft_id'] is None)!=(record['kind']=='question')
                    or type(record['created']) not in (int,float) or not math.isfinite(record['created']) or record['created'] < 0
                    or record['fingerprint'] != digest({k:v for k,v in record.items() if k != 'fingerprint'})
                    or canonical(record) != row['record']):
                reject()
            if 'kept_file_ids' in record:
                self._kept_ids(record['kept_file_ids'])
            priors = record['prior_answers']
            if type(priors) is not list or record['kind']=='draft' and priors:
                reject()
            self.execution._prior_ids(tuple(priors))
            if 'prior_answer_window' in record:
                window = record['prior_answer_window']
                if (record['kind']!='question' or type(window) is not dict
                        or set(window)!={'policy','includedCount','omittedOlder'} or window['policy']!=RECENT_ANSWER_POLICY
                        or not integer(window['includedCount'],0,documents.MAX_PRIOR_ANSWERS)
                        or window['includedCount']!=len(priors) or type(window['omittedOlder']) is not bool
                        or window['omittedOlder'] and len(priors)!=documents.MAX_PRIOR_ANSWERS):
                    reject()
            receipt = unpack(row['receipt'], SLOT)
            if (set(receipt) != {'v','id','phase','boot','worker','error','localVacant'} or receipt['v'] != 1
                    or receipt['id'] != request_id or receipt['phase'] not in ('prepared','running','ended')
                    or receipt['boot'] and (type(receipt['boot']) is not str or len(receipt['boot']) != 64)
                    or type(receipt['worker']) is not str or (receipt['worker'] and len(receipt['worker'])!=64)
                    or (receipt['phase']=='prepared') != (receipt['boot']==receipt['worker']=='')
                    or type(receipt['localVacant']) is not bool or receipt['localVacant'] and receipt['phase']!='ended'
                    or receipt['error'] not in ('none','local_refused','execution_refused','result_unavailable','budget_exhausted','context_too_large','reading_unavailable','tray_unavailable','abandoned')
                    or receipt['error']=='abandoned' and (receipt['phase']!='ended' or not receipt['localVacant'])):
                reject()
            self.conversations._selection(HistorySelection.from_binding(record['binding']))
            return record, receipt
        except (KeyError, ValueError, TypeError):
            reject()

    def _capability(self, record):
        return self.catalogue._seal(DOMAIN, {'id':record['id'], 'fingerprint':record['fingerprint'],
            'binding':record['binding'], 'boot':self.boot})

    def authorize(self, request_id, capability):
        with self.store._gate():
            record, receipt = self._record(request_id)
            value = self.catalogue._unseal(DOMAIN, capability)
            if (set(value) != {'id','fingerprint','binding','boot','epoch','v'}
                    or value['id'] != request_id or value['fingerprint'] != record['fingerprint']
                    or value['binding'] != record['binding'] or value['boot'] != self.boot):
                reject()
            return record, receipt

    def recover(self, request_id, binding, fingerprint):
        with self.store._gate():
            record, _ = self._record(request_id)
            if record['binding'] != destination(binding) or record['fingerprint'] != fingerprint:
                reject()
            # This only renews authority. No prepare/admit/claim is invoked.
            return dict(self.view(record), requestCapability=self._capability(record))

    def _configured(self, value):
        return dict(value, input_profile=asdict(self.input_profile), budget_scope=self.budget_scope)

    @staticmethod
    def _command_digest(value, policy):
        return digest(value if policy is None else dict(value, prior_answer_policy=policy))

    def _existing(self, command_key, intent):
        prior = self.store._seen(command_key, 'officer_send_intent', intent)
        if prior is None:
            return None
        record, _ = self._record(prior['target'])
        return dict(self.view(record), requestCapability=self._capability(record))

    def _insert(self, command_key, value, *, window=None, policy=None):
        value = self._configured(value)
        intent = self._command_digest(value, policy)
        existing = self._existing(command_key, intent)
        if existing is not None:
            return existing
        if self.store.db.execute('SELECT COUNT(*) FROM officer_document_intents').fetchone()[0] >= self.store.limits['requests']:
            fail('request_capacity', 'Motet document requests have reached capacity.', 429)
        if value['draft_id'] is not None and self.store.db.execute('SELECT COUNT(*) FROM officer_document_intents WHERE draft_id=?',(value['draft_id'],)).fetchone()[0]>=16:
            fail('request_capacity','This draft has reached its request history limit.',429)
        self.store._capacity()
        record = dict(value, id=secrets.token_hex(16), epoch=self.store.owner_epoch, created=self.store.clock())
        if window is not None:
            record['prior_answer_window'] = window
        record['fingerprint'] = digest(record)
        receipt = {'v':1,'id':record['id'],'phase':'prepared','boot':'','worker':'','error':'none','localVacant':False}
        serialized = canonical(record)
        if len(serialized.encode()) > 65536:
            fail('document_request_bound', 'The Motet document request is too large.', 413)
        self.store.db.execute('INSERT INTO officer_document_intents VALUES(?,?,?,?,?,?,?)',
            (record['id'], hashlib.sha256(command_key.encode()).hexdigest(), record['draft_id'],
             record['binding']['scopeId'],record['binding']['conversationId'],serialized, pack(receipt,SLOT)))
        self.store.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)',
            (command_key,'officer_send_intent',intent,record['id'],self.store.owner_epoch))
        self.store.checkpoint('officer_intent_before_commit')
        return dict(self.view(record), requestCapability=self._capability(record))

    def _existing_draft(self, draft_id, fence, command_key, binding, profile, kept_file_ids):
        existing = self.store.db.execute('SELECT * FROM seen WHERE key=?',(command_key,)).fetchone()
        if existing is not None:
            if existing['kind']!='officer_send_intent' or existing['epoch']!=self.store.owner_epoch:
                reject()
            old,_=self._record(existing['target'])
            if (type(fence) is not dict or set(fence)!={'revision','writerGeneration','writerNonce'}
                    or old['kind']!='draft' or old['draft_id']!=draft_id or old['binding']!=binding
                    or old.get('kept_file_ids')!=kept_file_ids
                    or old['profile']!=json.loads(canonical(asdict(profile)))
                    or old['fence']!={'revision':fence['revision'],'writerGeneration':fence['writerGeneration'],
                                     'nonceHash':self.store._hash(fence['writerNonce'])}): reject()
            return dict(self.view(old),requestCapability=self._capability(old))
        return None

    @staticmethod
    def _kept_ids(value):
        if (type(value) is not list or len(value)>5 or any(type(item) is not str for item in value)
                or len(set(value))!=len(value)):
            fail('document_tray_invalid','Choose up to five files in this question.',400)
        for file_id in value: ident(file_id)
        return value

    def prepare_draft(self, draft_id, capability, fence, command_key, binding, profile, *, kept_file_ids=None):
        key(command_key); binding = destination(binding)
        if kept_file_ids is not None: self._kept_ids(kept_file_ids)
        with self.store._gate():
            self.store._authorize(draft_id, capability)
            existing=self._existing_draft(draft_id,fence,command_key,binding,profile,kept_file_ids)
            if existing is not None:return existing
            draft = self.store._authorize(draft_id, capability, fence=fence)
            rows = self.conversations._sources(draft_id)
            self.conversations._validate(draft, binding, profile, rows)
            question = draft['text']
            if question.strip() and not documents._text(question,4000,empty=False):
                fail('document_question_invalid','Use a document question of at most 4,000 characters.',400)
            if kept_file_ids is not None and not question.strip() and kept_file_ids:
                fail('document_question_invalid','Enter a question when using kept files.',400)
            value = dict(kind='draft', binding=binding, draft_id=draft_id,
                fence={'revision':fence['revision'],'writerGeneration':fence['writerGeneration'],
                       'nonceHash':self.store._hash(fence['writerNonce'])},
                question=question, prior_answers=[], profile=asdict(profile), sources=rows,
                local_intent=digest({'draft':draft_id,'fence':fence,'binding':binding,'profile':asdict(profile)}))
            if kept_file_ids is not None:
                value['kept_file_ids']=kept_file_ids
                selected=self.file_ids(value)
                if len(selected)>min(5,self.store.limits['files']) or len(set(selected))!=len(selected):
                    fail('document_tray_invalid','Choose up to five distinct files in this question.',400)
        with self.conversations._reading(rows):
            for source in rows:
                self.store.files.read(source['id'],json.loads(source['identity']),source['digest'],self.store.limits['file_bytes'])
            with self.store._gate(), self.store._transaction():
                existing=self._existing_draft(draft_id,fence,command_key,binding,profile,kept_file_ids)
                if existing is not None:return existing
                draft = self.store._authorize(draft_id,capability,fence=fence)
                if rows != self.conversations._sources(draft_id) or draft['text'] != question:
                    reject()
                self.conversations._validate(draft,binding,profile,rows)
                return self._insert(command_key,value)

    def _recent_answer_window(self, selection, prior_answers, kept_file_ids=None):
        """Check the bounded same-conversation suffix using durable metadata."""
        maximum = self.store.limits['requests']
        rows = self.store.db.execute('''SELECT id FROM officer_document_intents
            WHERE scope_id=? AND conversation_id=? ORDER BY id LIMIT ?''',
            (selection.scope_id, selection.conversation_id, maximum+1)).fetchall()
        if len(rows) > maximum:
            reject()
        try:
            current, _ = (self.execution._roster(selection) if kept_file_ids is None
                          else self.execution._selected(selection,tuple(kept_file_ids)))
        except AttachmentError as exc:
            if kept_file_ids is not None and exc.code.startswith('document_tray_'):
                # Record a refusal on execution without reserving budget, even
                # when a previously selected file was deleted during Send.
                return None
            if exc.code in ('document_complete_roster_required','document_source_unsupported'):
                window_changed()
            raise
        current_by_id = {dep.source_id: dep for dep in current}
        candidates, execution_ids = [], set()
        for row in rows:
            record, _ = self._record(row['id'])
            if HistorySelection.from_binding(record['binding']) != selection:
                reject()
            _, execution = self.correlated(record)
            if execution is None:
                continue
            answer_id = execution['id']
            if answer_id in execution_ids:
                reject()
            execution_ids.add(answer_id)
            if not execution['answerAvailable']:
                continue
            stored, _, receipt, dependencies = self.execution._request(selection, answer_id)
            if all(current_by_id.get(dep.source_id) == dep for dep in dependencies):
                self.execution._answer_value(stored, receipt, answer_id)
                candidates.append((record['created'], record['id'], answer_id))

        linked = self.store.db.execute('''SELECT id FROM officer_document_requests
            WHERE destination_id=? AND scope_id=? AND conversation_id=? LIMIT ?''',
            (selection.destination_id, selection.scope_id, selection.conversation_id, maximum+1)).fetchall()
        if len(linked)>maximum or {row['id'] for row in linked} != execution_ids:
            reject()
        candidates.sort()
        limit = documents.MAX_PRIOR_ANSWERS
        if [aid for _, _, aid in candidates[-limit:]] != prior_answers:
            window_changed()
        return {'policy': RECENT_ANSWER_POLICY, 'includedCount': len(prior_answers), 'omittedOlder': len(candidates)>limit}

    def prepare_question(self, binding, question, prior_answers, profile, command_key, *, prior_answer_policy=None, kept_file_ids=None):
        key(command_key); binding=destination(binding)
        if kept_file_ids is not None: self._kept_ids(kept_file_ids)
        if (not documents._text(question,4000,empty=False) or not question.strip()
                or prior_answer_policy not in (None, RECENT_ANSWER_POLICY)
                or type(prior_answers) is not list or len(prior_answers)>documents.MAX_PRIOR_ANSWERS
                or any(type(v) is not str for v in prior_answers)
                or len(set(prior_answers))!=len(prior_answers)):
            reject()
        for aid in prior_answers: ident(aid)
        selection = HistorySelection.from_binding(binding)
        value = dict(kind='question',binding=binding,draft_id=None,fence=None,
            question=question,prior_answers=prior_answers,profile=asdict(profile),sources=[],local_intent=None)
        if kept_file_ids is not None: value['kept_file_ids']=kept_file_ids
        with self.store._gate():
            self.conversations._selection(selection)
            window = None
            if prior_answer_policy is not None:
                existing = self._existing(command_key, self._command_digest(self._configured(value), prior_answer_policy))
                if existing is not None:
                    return existing
                window = self._recent_answer_window(selection, prior_answers, kept_file_ids)
            with self.store._transaction():
                return self._insert(command_key, value, window=window, policy=prior_answer_policy)

    def consume(self, request_id, capability, *, draft_capability=None, fence=None, fingerprint=None):
        # Keep registry and CAS serialized, without running IO or continuation here.
        with self._winner_lock,self.store._gate():
            with self.store._transaction(reclaim=True):
                record, receipt = self.authorize(request_id,capability)
                if fingerprint != record['fingerprint']:reject()
                if record['kind']=='draft':
                    self.store._authorize(record['draft_id'],draft_capability)
                    if (type(fence) is not dict or set(fence)!={'revision','writerGeneration','writerNonce'}
                            or {'revision':fence['revision'],'writerGeneration':fence['writerGeneration'],
                                'nonceHash':self.store._hash(fence['writerNonce'])} != record['fence']):reject()
                if receipt['phase'] != 'prepared':return record, None
                if record['kind']=='draft':
                    for other in self.store.db.execute('SELECT id FROM officer_document_intents WHERE draft_id=? AND id<>?',(record['draft_id'],request_id)):
                        other_record,other_receipt=self._record(other['id'])
                        if other_receipt['phase']!='prepared':
                            local,execution=self.correlated(other_record)
                            exited=other_receipt['phase']=='ended' or other_receipt['boot']!=self.process
                            if not exited or local is not None or execution is not None:
                                fail('document_request_conflict','This draft already has a consumed Motet request.',409)
                            if other_receipt['error']=='abandoned':
                                continue  # Explicit cancellation is a durable terminal fact.
                            # This explicit new operation may retire only a proven
                            # exited predecessor with neither admission nor execution.
                            other_receipt.update(phase='ended',localVacant=True,error='local_refused')
                            self.store.db.execute('UPDATE officer_document_intents SET receipt=? WHERE id=?',
                                (pack(other_receipt,SLOT),other['id']))
                if len(self._winners)>=4:
                    fail('document_busy','Motet document operations are busy.',429)
                if record['kind']=='draft':
                    draft=self.store._authorize(record['draft_id'],draft_capability,fence=fence)
                    if (json.loads(draft['destination'])!=record['binding'] or draft['text']!=record['question']
                            or self.conversations._sources(record['draft_id'])!=record['sources']):reject()
                receipt.update(phase='running',boot=self.process,worker=self.boot)
                self.store.db.execute('UPDATE officer_document_intents SET receipt=? WHERE id=?',
                    (pack(receipt,SLOT),request_id))
                self.store.checkpoint('officer_intent_consume_before_commit')
            # Only a confirmed committed CAS creates an in-memory winner token.
            winner=object();self._winners[winner]=request_id
            return record,winner

    def abandon(self, request_id, capability, fingerprint):
        """Explicit owner cancellation competes with consume, never with execution."""
        with self._winner_lock,self.store._gate():
            with self.store._transaction(reclaim=True):
                record,receipt=self.authorize(request_id,capability)
                if fingerprint!=record['fingerprint']:reject()
                if receipt['phase']=='ended' and receipt['error']=='abandoned':
                    return self._view(record)
                if receipt['phase']!='prepared':
                    fail('request_already_started','This request has started or already has an outcome. Nothing was canceled.',409)
                admission,execution=self.correlated(record)
                if admission is not None or execution is not None:reject()
                receipt.update(phase='ended',boot=self.process,worker=self.boot,error='abandoned',localVacant=True)
                self.store.db.execute('UPDATE officer_document_intents SET receipt=? WHERE id=?',
                                      (pack(receipt,SLOT),request_id))
            return self._view(record)

    def finish(self, request_id, winner, error='none'):
        if error not in ('none','local_refused','execution_refused','result_unavailable','budget_exhausted','context_too_large','reading_unavailable','tray_unavailable'):reject()
        with self._winner_lock,self.store._gate():
            if self._winners.get(winner)!=request_id:return False
            with self.store._transaction(reclaim=True):
                record,receipt=self._record(request_id)
                if receipt['phase']!='running' or receipt['boot']!=self.process or receipt['worker']!=self.boot:reject()
                admission,execution=self.correlated(record)
                receipt.update(phase='ended',error=error,localVacant=admission is None and execution is None)
                self.store.db.execute('UPDATE officer_document_intents SET receipt=? WHERE id=?',
                    (pack(receipt,SLOT),request_id))
            del self._winners[winner]
            return True

    def correlated(self, record):
        """Exact read-only receipt correlation, never resume a workflow."""
        with self.store._gate():
            current,intent_receipt=self._record(record['id'])
            if canonical(current)!=canonical(record):reject()
            local_key=self.command(record,'local')
            local=self.store.db.execute('SELECT * FROM seen WHERE key=?',(local_key,)).fetchone()
            admission=None
            if local is not None:
                if intent_receipt['localVacant']:reject()
                if local['kind']!='officer_attachment_admission' or local['epoch']!=self.store.owner_epoch:
                    reject()
                admission=self.conversations._message(local['target'])
                if (record['kind']!='draft' or admission['draft_id']!=record['draft_id']
                        or not self.conversations._matches(admission,HistorySelection.from_binding(record['binding']))
                        or admission['text']!=record['question'] or admission['epoch']!=record['epoch']
                        or json.loads(admission['profile'])!=record['profile']
                        or admission['intent_fingerprint']!=record['local_intent']
                        or local['fingerprint']!=record['local_intent']): reject()
                # The original nonce is not stored; its complete admission
                # fingerprint was frozen while the owner's draft fence was live.
                draft=self.store._draft(record['draft_id'])
                if draft['admitted_message']!=admission['id'] or draft['admitted_kind']!='officer': reject()
            elif record['kind']=='draft':
                draft=self.store._draft(record['draft_id'])
                if (draft['admitted_message'] is not None or draft['admitted_request'] is not None) and not (intent_receipt['phase']=='prepared' or intent_receipt['localVacant']):
                    reject()
            exec_key=self.command(record,'execution')
            seen=self.store.db.execute('SELECT * FROM seen WHERE key=?',(exec_key,)).fetchone()
            target=self.store.db.execute('SELECT id,record FROM officer_document_requests WHERE command_digest=?',
                (hashlib.sha256(exec_key.encode()).hexdigest(),)).fetchone()
            if (seen is None)!=(target is None): reject()
            if seen is None:
                return admission, None
            if intent_receipt['phase']=='prepared' or intent_receipt['localVacant'] or record['kind']=='draft' and admission is None:reject()
            if (seen['kind']!='officer_document_prepare' or seen['epoch']!=record['epoch'] or seen['target']!=target['id']): reject()
            execution=json.loads(target['record'])
            expected=dict(selection=asdict(HistorySelection.from_binding(record['binding'])),profile=record['input_profile'],
                scope=record['budget_scope'],question=record['question'],message=admission['id'] if admission is not None else None)
            if record['prior_answers']:expected['prior_answer_ids']=record['prior_answers']
            if 'kept_file_ids' in record:
                file_ids = self.file_ids(record)
                expected['file_ids']=file_ids
                if (execution.get('selected_tray') is not True or
                        execution['manifest']['selection']['ordered_sources'] != sorted(file_ids)):
                    reject()
            if execution['intent']!=digest(expected):reject()
            if (execution['owner_epoch']!=record['epoch'] or execution['selection']!=asdict(HistorySelection.from_binding(record['binding']))
                    or execution['question']!=record['question'] or execution['intent']!=seen['fingerprint']
                    or execution['local_message_id']!=(admission['id'] if admission is not None else None)
                    or execution['profile']!=record['input_profile'] or execution['scope']!=record['budget_scope']
                    or execution['manifest']['selection']['prior_answer_ids']!=record['prior_answers']):
                reject()
            return admission, self.execution.status(HistorySelection.from_binding(record['binding']),target['id'])

    @staticmethod
    def file_ids(record):
        return tuple(record['kept_file_ids'])+tuple(row['id'] for row in record['sources'])

    def history(self, binding, cursor=None):
        selection=HistorySelection.from_binding(destination(binding))
        with self.store._gate():
            self.conversations._selection(selection)
            last=''
            if cursor is not None:
                value=self.catalogue._unseal('officer-request-cursor',cursor)
                if set(value)!={'epoch','v','selection','last'} or value['selection']!=asdict(selection):reject()
                last=ident(value['last'])
            rows=self.store.db.execute('SELECT id FROM officer_document_intents WHERE scope_id=? AND conversation_id=? AND id>? ORDER BY id LIMIT 51',
                (selection.scope_id,selection.conversation_id,last)).fetchall()
            items=[]
            for row in rows[:50]:
                record,_=self._record(row['id'])
                if HistorySelection.from_binding(record['binding'])!=selection:reject()
                items.append(dict(self.view(record),question=record['question'],createdAt=record['created'],
                    destination=record['binding'],requestCapability=self._capability(record)))
            next_cursor=self.catalogue._seal('officer-request-cursor',{'selection':asdict(selection),'last':rows[49]['id']}) if len(rows)>50 else None
            return {'items':items,'nextCursor':next_cursor}

    def by_prepare(self, selection, prepare_digest):
        """An exact read of prepare identity. Absence supplies no terminal proof."""
        if type(prepare_digest) is not str or not TOKEN.fullmatch(prepare_digest):
            fail('invalid_request', 'Choose an exact Motet prepare digest.', 400)
        with self.store._gate():
            self.conversations._selection(selection)
            row = self.store.db.execute('''SELECT id FROM officer_document_intents
                WHERE command_hash=? AND scope_id=? AND conversation_id=?''',
                (prepare_digest, selection.scope_id, selection.conversation_id)).fetchone()
            if row is None:
                return {'request': None}
            record, _ = self._record(row['id'])
            if HistorySelection.from_binding(record['binding']) != selection:
                reject()
            view = self._view(record)
            return {'request': {'requestId': record['id'], 'draftId': record['draft_id'],
                                'kind': record['kind'], 'state': view['state']}}

    def view(self, record):
        with self.store._gate():
            return self._view(record)

    def _view(self, record):
        _, receipt=self._record(record['id'])
        admission, execution=self.correlated(record)
        state='prepared' if receipt['phase']=='prepared' else 'preparing'
        delivery='retained_local' if admission is not None else 'not_sent'
        answer_id=None
        if execution is not None:
            delivery='retained_local' if execution['state']=='prepared' else 'unknown'
            if execution['state']=='accepted':
                delivery='confirmed'
                committed=execution['answerAvailable'] or execution['availability']=='revoked' and execution['answerCommitted']
                state='answered' if committed else 'failed'
                if committed: answer_id=execution['id']
            elif execution['state']=='failed':
                state='failed'
                if execution['error']=='source_withdrawn':
                    delivery='retained_local' if admission is not None else 'not_sent'
            elif execution['state'] in ('claimed','unknown'):state='unknown'
            elif receipt['phase']=='ended' or receipt['boot']!=self.process:state='retained_local'
        elif receipt['phase']!='prepared' and (receipt['phase']=='ended' or receipt['boot']!=self.process):
            state='retained_local' if admission is not None else 'failed'
            delivery='retained_local' if admission is not None else 'not_sent'
        failure='provider_rejected' if execution is not None and execution['state']=='failed' and execution['error']=='rejected' else None
        if (receipt['error']=='tray_unavailable' and state in ('failed','retained_local')
                or execution is not None and execution['error']=='source_withdrawn'):
            failure='tray_unavailable'
        not_asked = None
        if state=='retained_local' and delivery=='retained_local' and receipt['phase']=='ended':
            if not record['question'].strip():not_asked='no_question'
            elif receipt['error'] in ('budget_exhausted','context_too_large','reading_unavailable'):not_asked=receipt['error']
        # Legacy answers may predate explicit selection. Their immutable manifest
        # still says which originals were actually sent and can seed a follow-up.
        follow_up = {}
        if 'kept_file_ids' not in record and execution is not None and answer_id is not None:
            selection = HistorySelection.from_binding(record['binding'])
            _,_,_,deps = self.execution._request(selection,answer_id)
            follow_up['legacyFollowUpIds'] = [dep.source_id for dep in deps]
        files = {'questionFileIds':list(self.file_ids(record)),
                 'draftFileIds':[source['id'] for source in record['sources']]} if 'kept_file_ids' in record else {}
        return dict(requestId=record['id'],draftId=record['draft_id'],fingerprint=record['fingerprint'],state=state,failure=failure,notAskedReason=not_asked,
            **files,**follow_up,
            canAbandon=receipt['phase']=='prepared',abandoned=receipt['phase']=='ended' and receipt['error']=='abandoned',
            officerDelivery={'state':delivery,'requestId':None if delivery=='not_sent' else record['id']},
            answerId=answer_id,analysisOnly=True,actions=[])
