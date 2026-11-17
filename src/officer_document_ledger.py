"""Offline durable Motet document execution participant; never calls a provider.

Host-only construction/configuration. No HTTP, resolver, page, feature switch,
ordinary history integration, or executable transport is provided here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, DecimalException
import hashlib
import json
import os
import re
import secrets
import struct
import threading
import time

from attachments_catalogue import HistorySelection
from attachments_conversations import OfficerConversations, SourceDependency
from attachments_types import AttachmentError, ID, OPAQUE, TOKEN, canonical, digest, fail, ident, integer, key
import officer_documents as documents

RECEIPT_BYTES = 4096
ANSWER_BYTES = 65536
MAX_REQUESTS = 10000
MAX_DEPENDENTS = 64
MONEY_SCALE = 1_000_000_000
MAX_MONEY = 2**53 - 1
MAX_ANSWER_TIME_MS = 2**53 - 1
ZERO_ANSWER = bytes(ANSWER_BYTES)
ERRORS = ('none', 'rejected', 'transport_unknown', 'validation_failed', 'usage_unverified', 'billing_mismatch', 'source_withdrawn')
STATES = ('prepared', 'claimed', 'accepted', 'failed', 'unknown')
RECEIPT_KEYS = {'v','request','fingerprint','state','nonce','body','profile','reserved','charged','settled',
                'result_digest','initial_result_digest','response','provider_message','candidate','usage','error','availability','answer_digest','checksum'}


def reject(code):
    fail(code, 'The Motet document operation could not be verified.', 409)


def money(value):
    if type(value) is not str or not 1 <= len(value) <= 64:
        reject('document_budget_invalid')
    try:
        # String construction and as_tuple are exact and context-independent.
        # Decimal multiplication/to_integral_value would use ambient precision
        # and can silently round a fractional nanodollar or a scope ceiling.
        number = Decimal(value)
        if not number.is_finite() or number.is_signed():
            reject('document_budget_invalid')
        parts = number.as_tuple()
        coefficient = int(''.join(str(digit) for digit in parts.digits))
        if not coefficient:
            reject('document_budget_invalid')
        shift = parts.exponent + 9  # Integer nanodollars; no Decimal arithmetic.
        digits = len(str(coefficient))
        if shift >= 0:
            # Bound before exponentiation, including huge exponent spellings.
            if digits + shift > len(str(MAX_MONEY)):
                reject('document_budget_invalid')
            units = coefficient * (10 ** shift)
        else:
            if -shift >= digits:
                reject('document_budget_invalid')
            units, remainder = divmod(coefficient, 10 ** -shift)
            if remainder:
                reject('document_budget_invalid')
        if not 0 < units <= MAX_MONEY:
            reject('document_budget_invalid')
        return units
    except (DecimalException, OverflowError):
        reject('document_budget_invalid')


def encoded(value):
    return canonical(value).encode('utf-8')


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def token(value, *, empty=False):
    if type(value) is not str or not (empty and value == '' or TOKEN.fullmatch(value)):
        reject('document_record_invalid')
    return value


def pack(value, size):
    value = dict(value)
    value['checksum'] = digest(value)
    body = encoded(value)
    if len(body) > size - 4:
        reject('document_slot_capacity')
    return struct.pack('>I',len(body)) + body + bytes(size-4-len(body))


def unpack(raw, size):
    if type(raw) is not bytes or len(raw) != size:
        reject('document_record_invalid')
    length = struct.unpack('>I',raw[:4])[0]
    if not 1 <= length <= size-4 or any(raw[4+length:]):
        reject('document_record_invalid')
    def unique(pairs):
        result = {}
        for k,v in pairs:
            if k in result: reject('document_record_invalid')
            result[k] = v
        return result
    try:
        value = json.loads(raw[4:4+length],object_pairs_hook=unique)
        if type(value) is not dict or 'checksum' not in value:
            reject('document_record_invalid')
        checksum = value.pop('checksum')
        if checksum != digest(value) or encoded(dict(value,checksum=checksum)) != raw[4:4+length]:
            reject('document_record_invalid')
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):
        reject('document_record_invalid')


@dataclass(frozen=True)
class DocumentBudgetScope:
    id: str
    revision: str
    usd_limit: str


@dataclass(frozen=True)
class DocumentPricing:
    """Trusted host policy only; syntax is not evidence that rates are current.

    Rates are integer nanodollars/token for the exact supported billing basis.
    No defaults and no page/service caller can override persisted scope terms.
    """
    pricing_revision: str
    billing_revision: str
    inference_geo: str
    input_rate: int
    output_rate: int
    cache_read_rate: int
    cache_5m_rate: int
    cache_1h_rate: int
    qualification_id: str | None = None


@dataclass(frozen=True)
class ResultFacts:
    request_id: str
    claim_nonce: str
    body_digest: str
    profile_digest: str
    state: str
    response_digest: str
    provider_message_id: str
    model_id: str
    billing_revision: str
    usage: documents.DocumentUsage | None
    error: str = 'none'


class _Permit:
    __slots__ = ()


def migrate(store):
    """Schema8 ->9 startup participant; caller owns one real writer transaction."""
    db = store.db
    if not db.in_transaction or db.execute('PRAGMA user_version').fetchone()[0] != 8:
        reject('document_schema_unavailable')
    statements = [
        '''CREATE TABLE officer_document_scopes(id TEXT PRIMARY KEY, configuration TEXT NOT NULL CHECK(length(configuration)<=16384),
           halt BLOB NOT NULL CHECK(length(halt)=4096)) WITHOUT ROWID''',
        '''CREATE TABLE officer_document_requests(id TEXT PRIMARY KEY, command_digest TEXT UNIQUE NOT NULL,
           destination_id TEXT NOT NULL,scope_id TEXT NOT NULL,conversation_id TEXT NOT NULL REFERENCES conversations(id),
           budget_scope TEXT NOT NULL REFERENCES officer_document_scopes(id),record TEXT NOT NULL CHECK(length(record)<=131072),
           receipt BLOB NOT NULL CHECK(length(receipt)=4096),answer BLOB NOT NULL CHECK(length(answer)=65536)) WITHOUT ROWID''',
        '''CREATE TABLE officer_document_dependencies(request_id TEXT NOT NULL REFERENCES officer_document_requests(id),
           source_id TEXT NOT NULL REFERENCES files(id),record TEXT NOT NULL CHECK(length(record)<=2048),
           PRIMARY KEY(request_id,source_id)) WITHOUT ROWID''',
        'CREATE INDEX officer_document_source ON officer_document_dependencies(source_id,request_id)',
        'CREATE INDEX officer_document_context ON officer_document_requests(destination_id,scope_id,conversation_id,id)',
        'CREATE INDEX officer_document_budget ON officer_document_requests(budget_scope,id)',
        '''CREATE TRIGGER officer_document_request_immutable BEFORE UPDATE OF id,command_digest,destination_id,
           scope_id,conversation_id,budget_scope,record ON officer_document_requests
           BEGIN SELECT RAISE(ABORT,'immutable document request'); END''',
        '''CREATE TRIGGER officer_document_dependency_immutable BEFORE UPDATE ON officer_document_dependencies
           BEGIN SELECT RAISE(ABORT,'immutable document dependency'); END''',
        '''CREATE TRIGGER officer_document_scope_immutable BEFORE UPDATE OF id,configuration ON officer_document_scopes
           BEGIN SELECT RAISE(ABORT,'immutable document scope'); END''',
    ]
    for sql in statements: db.execute(sql)
    db.execute('PRAGMA user_version=9')
    store.checkpoint('officer_document_migration_before_commit')


class OfficerDocumentLedger:
    def __init__(self, store):
        self.store = store
        self.conversations = OfficerConversations(store)
        self._permit_lock = threading.Lock()
        self._permits = {}
        self._process_epoch = secrets.token_hex(32)
        self._pid = os.getpid()
        with store._gate():
            if store.db.execute('PRAGMA user_version').fetchone()[0] not in (9,10):
                reject('document_schema_unavailable')

    def register_scope(self, scope, profile, pricing):
        if (type(scope) is not DocumentBudgetScope or type(pricing) is not DocumentPricing
                or any(type(x) is not str or not OPAQUE.fullmatch(x) for x in
                       (scope.id,scope.revision,pricing.pricing_revision,pricing.billing_revision))):
            reject('document_budget_invalid')
        documents._profile(profile)
        proof = profile.strict_budget_evidence
        if (pricing.pricing_revision != proof.pricing_revision or pricing.billing_revision != proof.billing_revision
                or pricing.inference_geo not in ('global','us','default_global_or_us')
                or any(not integer(getattr(pricing,k),0,MAX_MONEY) for k in
                       ('input_rate','output_rate','cache_read_rate','cache_5m_rate','cache_1h_rate'))):
            reject('document_budget_invalid')
        qualified = pricing.qualification_id is not None
        if qualified:
            from officer_document_policy import validate_pricing
            validate_pricing(profile, pricing)
            from officer_document_policy import current
            current(self.store.clock)
        elif pricing.inference_geo == 'default_global_or_us':
            reject('document_budget_invalid')
        ceiling = money(proof.usd_ceiling)
        # All input tokens can land in the most expensive configured class.
        worst = (proof.max_input_tokens * max(pricing.input_rate,pricing.cache_read_rate,pricing.cache_5m_rate,pricing.cache_1h_rate)
                 + profile.max_output_tokens * pricing.output_rate)
        if qualified:
            from officer_document_policy import reserve_units
            worst = reserve_units()
        if worst > ceiling or ceiling > money(scope.usd_limit): reject('document_budget_invalid')
        price_record = asdict(pricing)
        if not qualified:
            price_record.pop('qualification_id')  # Preserve original single-geo identity.
        configuration = dict(scope=asdict(scope),profile=asdict(profile),pricing=price_record,limit=money(scope.usd_limit),ceiling=ceiling)
        configuration['digest'] = digest(configuration)
        with self.store._gate(), self.store._transaction():
            prior = self.store.db.execute('SELECT * FROM officer_document_scopes WHERE id=?',(scope.id,)).fetchone()
            if prior:
                if prior['configuration'] != canonical(configuration): reject('document_budget_conflict')
                return
            # One immutable global scope per registered profile; no rotation to escape debt.
            for existing in self.store.db.execute('SELECT configuration FROM officer_document_scopes'):
                if json.loads(existing[0])['profile']['profile_id'] == profile.profile_id:
                    reject('document_budget_conflict')
            if self.store.db.execute('SELECT COUNT(*) FROM officer_document_scopes').fetchone()[0] >= 16:
                reject('document_budget_capacity')
            self.store.db.execute('INSERT INTO officer_document_scopes VALUES(?,?,?)',
                                 (scope.id,canonical(configuration),pack({'v':1,'halted':False},RECEIPT_BYTES)))

    def _scope(self, scope_id, profile=None, *, authority=False):
        row = self.store.db.execute('SELECT * FROM officer_document_scopes WHERE id=?',(scope_id,)).fetchone()
        if row is None: reject('document_budget_missing')
        config = json.loads(row['configuration'])
        if (type(config) is not dict or set(config)!={'scope','profile','pricing','limit','ceiling','digest'}
                or config['digest'] != digest({k:v for k,v in config.items() if k!='digest'})
                or config['scope']['id'] != scope_id or not integer(config['limit'],1,MAX_MONEY)
                or not integer(config['ceiling'],1,config['limit'])):
            reject('document_record_invalid')
        if profile is not None and config['profile'] != json.loads(canonical(asdict(profile))):
            reject('document_budget_conflict')
        if authority and config['pricing'].get('qualification_id') is not None:
            from officer_document_policy import current
            current(self.store.clock)
        halt = unpack(row['halt'],RECEIPT_BYTES)
        if set(halt) != {'v','halted'} or halt['v'] != 1 or type(halt['halted']) is not bool:
            reject('document_record_invalid')
        return config, halt['halted']

    def _selection(self, selection):
        self.conversations._selection(selection)

    def _roster(self, selection):
        rows, page = self.conversations._page(selection,None,context=True)
        if page['nextCursor'] is not None or not 1 <= page['availableCount'] <= 50 or len(rows) != page['availableCount']:
            reject('document_complete_roster_required')
        if any(r['preparation_kind'] != 'officer_direct' or r['media'] not in ('application/pdf','image/png') for r in rows):
            reject('document_source_unsupported')
        return tuple(self.conversations._dependency(r) for r in rows), tuple(dict(r) for r in rows)

    def _selected(self, selection, file_ids):
        """Resolve only the files named by the tray, in canonical input order."""
        self._selection(selection)
        if (type(file_ids) is not tuple or not 1 <= len(file_ids) <= min(5,self.store.limits['files'])
                or any(type(file_id) is not str or not ID.fullmatch(file_id) for file_id in file_ids)
                or len(set(file_ids)) != len(file_ids)):
            reject('document_tray_invalid')
        rows = []
        for file_id in sorted(file_ids):
            row = self.store.db.execute(
                'SELECT ' + self.conversations._COLUMNS + ' ' + self.conversations._JOIN
                + ' WHERE mf.source_id=?', (file_id,)).fetchone()
            if (row is None or not self.conversations._matches(row,selection)
                    or not self.conversations._available(row) or row['preparation_kind'] != 'officer_direct'
                    or row['media'] not in documents.SIGNATURES):
                reject('document_tray_source_unavailable')
            rows.append(dict(row))
        if sum(row['bytes'] for row in rows) > min(300000,self.store.limits['draft_bytes']):
            reject('document_tray_limit')
        return tuple(self.conversations._dependency(row) for row in rows), tuple(rows)

    @contextmanager
    def _read_dependencies(self, selection, dependencies, *, sources=False):
        # One global in-process lifetime protects store close; source leases are
        # sequential (never >16 keys). Final gate catches WATCH revocation between
        # reads. No immutable filesystem snapshot across external same-UID writes.
        marker = 'officer-document:' + secrets.token_hex(16)
        with self.store._gate():
            if len(self.store.request_handles) >= self.store.limits['streams']:
                reject('document_resource_busy')
            self.store.request_handles.add(marker)
        result = []
        try:
            self.conversations.validate_dependencies(selection,dependencies)
            for dep in dependencies:
                with self.conversations.catalogue._handle(dep.source_id):
                    with self.store._gate():
                        self.conversations.validate_dependencies(selection,(dep,))
                        row = self.store.db.execute('SELECT * FROM files WHERE id=?',(dep.source_id,)).fetchone()
                    raw = self.store.files.read(dep.source_id,list(dep.identity),dep.sha256,self.store.limits['file_bytes'])
                    if sources:
                        result.append(documents.DocumentSource(dep,row['media'],row['preparation_kind'],row['bytes'],dep.sha256,raw))
            yield tuple(result)
        finally:
            result.clear()
            with self.store.lock: self.store.request_handles.discard(marker)

    def _request(self, selection, request_id):
        self._selection(selection)
        row = self.store.db.execute('SELECT * FROM officer_document_requests WHERE id=?',(ident(request_id),)).fetchone()
        if row is None or (row['destination_id'],row['scope_id'],row['conversation_id']) != (selection.destination_id,selection.scope_id,selection.conversation_id):
            reject('document_context_mismatch')
        record = self._record(row)
        if record['owner_epoch'] != self.store.owner_epoch: reject('document_epoch_changed')
        receipt = unpack(row['receipt'],RECEIPT_BYTES)
        self._receipt(record,receipt)
        config,_ = self._scope(record['scope'],self._profile_from(record))
        if receipt['reserved'] != (0 if receipt['state']=='prepared' else config['ceiling']):
            reject('document_record_invalid')
        deps = tuple(self._decode_dependency(r[0]) for r in self.store.db.execute(
            'SELECT record FROM officer_document_dependencies WHERE request_id=? ORDER BY source_id',(request_id,)))
        if digest([asdict(x) for x in deps]) != record['dependency_digest'] or len(deps) != record['dependency_count']:
            reject('document_dependencies_changed')
        return row,record,receipt,deps

    @staticmethod
    def _record(row):
        raw = row['record']
        if type(raw) is not str or len(raw.encode('utf-8'))>131072: reject('document_record_invalid')
        record = json.loads(raw)
        fields = {'v','id','intent','owner_epoch','selection','question','local_message_id','profile','profile_digest',
                  'body_digest','manifest_digest','manifest','scope','created_ms','dependency_digest','dependency_count','roster_digest','fingerprint'}
        if (type(record) is not dict or not fields <= set(record) <= fields|{'prior_answer_refs','selected_tray'}
                or 'selected_tray' in record and record['selected_tray'] is not True or type(record['v']) is not int or record['v']!=1
                or canonical(record)!=raw or record['fingerprint']!=digest({k:v for k,v in record.items() if k!='fingerprint'})
                or record['id']!=row['id'] or record['scope']!=row['budget_scope']
                or not integer(record['created_ms'],0) or not integer(record['dependency_count'],1,50)
                or not documents._text(record['question'],4000,empty=False)
                or record['selection']!=dict(kind='officer',destination_id=row['destination_id'],scope_id=row['scope_id'],conversation_id=row['conversation_id'])
                or digest(record['manifest'])!=record['manifest_digest']):
            reject('document_record_invalid')
        for field in ('intent','owner_epoch','profile_digest','body_digest','manifest_digest','dependency_digest','roster_digest','fingerprint'):
            token(record[field])
        ident(record['id'])
        if record['local_message_id'] is not None: ident(record['local_message_id'])
        prior_ids = tuple(record['manifest']['selection']['prior_answer_ids'])
        OfficerDocumentLedger._prior_ids(prior_ids)
        refs = record.get('prior_answer_refs',[])
        ref_fields = {'answer_id','conversation_id','request_digest','manifest_digest','answer_digest','text_digest','dependency_digest'}
        if (type(refs) is not list or len(refs)!=len(prior_ids)
                or refs!=record['manifest'].get('prior_answer_refs',[])):
            reject('document_record_invalid')
        for expected,reference in zip(prior_ids,refs):
            if (type(reference) is not dict or set(reference)!=ref_fields or reference['answer_id']!=expected
                    or reference['conversation_id']!=row['conversation_id']): reject('document_record_invalid')
            for field in ref_fields-{'answer_id','conversation_id'}: token(reference[field])
        p = OfficerDocumentLedger._profile_from(record); documents._profile(p)
        if documents.profile_binding(p)!=record['profile_digest']: reject('document_record_invalid')
        return record

    @staticmethod
    def _decode_dependency(raw):
        value = json.loads(raw); value['identity'] = tuple(value['identity'])
        dep = SourceDependency(**value); documents._dependency(dep)
        return dep

    @staticmethod
    def _receipt(record,r):
        fields = RECEIPT_KEYS - {'checksum'}
        if (set(r) not in (fields, fields | {'answer_committed_ms'}) or type(r['v']) is not int or r['v'] != 1
                or r['request'] != record['id'] or r['fingerprint'] != record['fingerprint']
                or r['body'] != record['body_digest'] or r['profile'] != record['profile_digest']
                or r['state'] not in STATES or r['error'] not in ERRORS
                or r['availability'] not in ('unavailable','available','revoked')
                or any(not integer(r[k],0,MAX_MONEY) for k in ('reserved','charged'))
                or type(r['settled']) is not bool or r['charged'] > r['reserved']):
            reject('document_record_invalid')
        if (bool(r['result_digest']) != bool(r['initial_result_digest'])
                or r['state'] in ('prepared','claimed') and r['result_digest']
                or r['state'] in ('accepted','failed','unknown') and not r['result_digest']
                or r['settled'] and ((r['usage'] is None and r['error']!='source_withdrawn') or not r['result_digest'])
                or r['error']=='source_withdrawn' and (r['state']!='failed' or not r['settled'] or r['charged']!=0
                    or r['availability']!='revoked' or r['usage'] is not None or r['provider_message'] or r['response'])
                or not r['settled'] and r['charged'] != r['reserved']
                or r['availability']=='available' and (not r['settled'] or r['error']!='none')):
            reject('document_record_invalid')
        if r['usage'] is not None:
            if type(r['usage']) is not dict or set(r['usage']) != set(documents.DocumentUsage.__dataclass_fields__):
                reject('document_record_invalid')
            u = documents.DocumentUsage(**r['usage'])
            values = asdict(u)
            usage = {k:values[k] for k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens','service_tier','inference_geo','speed')}
            usage.update(cache_creation=dict(ephemeral_5m_input_tokens=u.cache_5m_tokens,ephemeral_1h_input_tokens=u.cache_1h_tokens),output_tokens_details=dict(thinking_tokens=u.thinking_tokens))
            documents._usage(usage)
        for k in ('nonce','result_digest','initial_result_digest','response','candidate','answer_digest'): token(r[k],empty=True)
        # Only commit_answer writes this pair, atomically. Revocation keeps its
        # historical witness while wiping the governed answer slot.
        if (bool(r['candidate']) != bool(r['answer_digest'])
                or r['answer_digest'] and (r['state']!='accepted' or not r['settled']
                    or r['error']!='none' or r['availability'] not in ('available','revoked'))):
            reject('document_record_invalid')
        if 'answer_committed_ms' in r and (not integer(r['answer_committed_ms']) or not r['answer_digest']):
            reject('document_record_invalid')
        if (type(r['provider_message']) is not str or len(r['provider_message']) > 128
                or (r['state'] == 'prepared') != (r['nonce'] == '')
                or (r['state'] == 'prepared' and (r['reserved'] or r['charged']))
                or (r['availability'] == 'available' and (r['state'] != 'accepted' or not r['answer_digest']))):
            reject('document_record_invalid')

    @staticmethod
    def _answered_at(receipt):
        return receipt['answer_committed_ms'] / 1000 if 'answer_committed_ms' in receipt else None

    @staticmethod
    def _view(record,r):
        return dict(id=record['id'],state=r['state'],answerAvailable=r['availability']=='available',
                    answerCommitted=bool(r['answer_digest']),
                    answeredAt=OfficerDocumentLedger._answered_at(r),
                    availability=r['availability'],error=r['error'],reservedUnits=r['reserved'],chargedUnits=r['charged'],
                    settled=r['settled'],bodyDigest=r['body'],profileDigest=r['profile'],claimNonce=r['nonce'])

    def status(self, selection, request_id):
        with self.store._gate():
            _,record,r,_ = self._request(selection,request_id)
            return self._view(record,r)

    @staticmethod
    def _prior_ids(prior_answer_ids):
        if (type(prior_answer_ids) is not tuple or len(prior_answer_ids)>documents.MAX_PRIOR_ANSWERS
                or any(type(x) is not str or not ID.fullmatch(x) for x in prior_answer_ids)
                or len(set(prior_answer_ids))!=len(prior_answer_ids)):
            reject('document_prior_answers_invalid')
        return prior_answer_ids

    @staticmethod
    def _as_prior(answer_id,selection,value):
        deps=tuple(OfficerDocumentLedger._decode_dependency(canonical(d)) for d in value['consumed_dependencies'])
        return documents.PriorAnswerInput(answer_id,selection.conversation_id,value['request_digest'],value['manifest_digest'],
                                          digest(value),value['text'],deps)

    def _prior_inputs(self,selection,prior_answer_ids):
        self._prior_ids(prior_answer_ids)
        # Governed restoration does actual source reads for every prior answer,
        # including uncited and already-flattened transitive dependencies.
        return tuple(self.publish(selection,answer_id,
                     lambda value,answer_id=answer_id:self._as_prior(answer_id,selection,value))
                     for answer_id in prior_answer_ids)

    def _prior_refs_current(self,selection,references):
        # Final database-gated recheck after actual source reads. Read no browser
        # prose and never copy provider text into the immutable request record.
        for expected in references:
            row,_,receipt,_=self._request(selection,expected['answer_id'])
            value=self._answer_value(row,receipt,expected['answer_id'])
            if documents.prior_metadata(self._as_prior(expected['answer_id'],selection,value))!=expected:
                reject('document_prior_answer_changed')

    def source_ids(self,selection,request_id):
        """Metadata only, for a later buffer pin; never a call/publication permit."""
        with self.store._gate():
            _,_,receipt,deps=self._request(selection,request_id)
            if receipt['availability']=='revoked': reject('document_answer_revoked')
            self.conversations.validate_dependencies(selection,deps)
            return tuple(dep.source_id for dep in deps)

    def prepare(self, selection, profile, scope_id, command_key, question, *, local_message_id=None, prior_answer_ids=(), file_ids=None):
        key(command_key); documents._profile(profile)
        if not documents._text(question,4000,empty=False): reject('document_question_invalid')
        if local_message_id is not None: ident(local_message_id)
        self._prior_ids(prior_answer_ids)
        intent_value = dict(selection=asdict(selection),profile=asdict(profile),scope=scope_id,question=question,message=local_message_id)
        if prior_answer_ids: intent_value['prior_answer_ids']=prior_answer_ids
        if file_ids is not None: intent_value['file_ids']=file_ids
        intent = digest(intent_value)
        command = hash_bytes(command_key.encode())
        with self.store._gate():
            self._selection(selection); self._scope(scope_id,profile,authority=True)
            self.store._seen(command_key,'officer_document_prepare',intent)
            prior = self.store.db.execute('SELECT id,record FROM officer_document_requests WHERE command_digest=?',(command,)).fetchone()
            if prior:
                if json.loads(prior['record'])['intent'] != intent: reject('document_command_conflict')
                return self.status(selection,prior['id'])
            deps, rows = self._roster(selection) if file_ids is None else self._selected(selection,file_ids)
            if local_message_id is not None and not self.conversations._matches(self.conversations._message(local_message_id),selection):
                reject('document_context_mismatch')
            if sum(r['bytes'] for r in rows) > profile.max_encoded_bytes:
                reject('serialized_document_input_bound')
        priors=self._prior_inputs(selection,prior_answer_ids)
        references=[documents.prior_metadata(prior) for prior in priors]
        # Earlier answer text is eligible only when every exact dependency is in this question.
        current_by_id={dep.source_id:dep for dep in deps}
        if any(current_by_id.get(dep.source_id)!=dep for prior in priors for dep in prior.dependencies):
            reject('document_prior_answer_changed')
        request_id = secrets.token_hex(16)
        manifest = documents.InputManifest(request_id,selection.conversation_id,selection.scope_id,profile.revision,
                                          hash_bytes(question.encode()),tuple(d.source_id for d in deps),prior_answer_ids,deps,1)
        with self._read_dependencies(selection,deps,sources=True) as sources:
            candidate = documents.build_input(profile,manifest,question,sources,prior_answers=priors)
            with self.store._gate(), self.store._transaction():
                self.store._seen(command_key,'officer_document_prepare',intent)
                prior = self.store.db.execute('SELECT id,record FROM officer_document_requests WHERE command_digest=?',(command,)).fetchone()
                if prior:
                    if json.loads(prior['record'])['intent'] != intent: reject('document_command_conflict')
                    return self.status(selection,prior['id'])
                current,_ = self._roster(selection) if file_ids is None else self._selected(selection,file_ids)
                if current != deps: reject('document_roster_changed')
                self.conversations.validate_dependencies(selection,deps)
                self._prior_refs_current(selection,references)
                self._scope(scope_id,profile,authority=True)
                if self.store.db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0] >= min(MAX_REQUESTS,self.store.limits['officer_messages']):
                    reject('document_request_capacity')
                for dep in deps:
                    if self.store.db.execute('SELECT COUNT(*) FROM officer_document_dependencies WHERE source_id=?',(dep.source_id,)).fetchone()[0] >= MAX_DEPENDENTS:
                        reject('document_dependency_capacity')
                self.store._capacity()
                record = dict(v=1,id=request_id,intent=intent,owner_epoch=self.store.owner_epoch,selection=asdict(selection),
                              question=question,local_message_id=local_message_id,profile=asdict(profile),profile_digest=documents.profile_binding(profile),
                              body_digest=candidate.body_digest,manifest_digest=candidate.complete_manifest.digest,
                              manifest=candidate.complete_manifest.metadata,scope=scope_id,created_ms=int(self.store.clock()*1000),
                              dependency_digest=digest([asdict(d) for d in deps]),dependency_count=len(deps),roster_digest=digest([asdict(d) for d in deps]))
                if references: record['prior_answer_refs']=references
                if file_ids is not None: record['selected_tray']=True
                record['fingerprint'] = digest(record)
                r = dict(v=1,request=request_id,fingerprint=record['fingerprint'],state='prepared',nonce='',body=candidate.body_digest,
                         profile=record['profile_digest'],reserved=0,charged=0,settled=False,result_digest='',initial_result_digest='',response='',provider_message='',
                         candidate='',usage=None,error='none',availability='unavailable',answer_digest='')
                self.store.db.execute('INSERT INTO officer_document_requests VALUES(?,?,?,?,?,?,?,?,?)',
                    (request_id,command,selection.destination_id,selection.scope_id,selection.conversation_id,scope_id,canonical(record),pack(r,RECEIPT_BYTES),ZERO_ANSWER))
                self.store.db.executemany('INSERT INTO officer_document_dependencies VALUES(?,?,?)',
                    [(request_id,d.source_id,canonical(asdict(d))) for d in deps])
                self.store.db.execute('INSERT INTO seen VALUES(?,?,?,?,?)',(command_key,'officer_document_prepare',intent,request_id,self.store.owner_epoch))
                self.store.checkpoint('officer_document_prepare_before_commit')
                return self._view(record,r)

    @staticmethod
    def _profile_from(record):
        p = dict(record['profile']); p['media_types'] = tuple(p['media_types'])
        p['strict_budget_evidence'] = documents.StrictBudgetEvidence(**p['strict_budget_evidence'])
        return documents.OfficerInputProfile(**p)

    def _prepared(self, record, deps, sources):
        p = self._profile_from(record)
        selection=HistorySelection(**record['selection'])
        prior_ids=tuple(record['manifest']['selection']['prior_answer_ids'])
        priors=self._prior_inputs(selection,prior_ids)
        if [documents.prior_metadata(prior) for prior in priors]!=record.get('prior_answer_refs',[]):
            reject('document_prior_answer_changed')
        manifest = documents.InputManifest(record['id'],record['selection']['conversation_id'],record['selection']['scope_id'],p.revision,
                                          hash_bytes(record['question'].encode()),tuple(d.source_id for d in deps),prior_ids,deps,1)
        prepared = documents.build_input(p,manifest,record['question'],sources,prior_answers=priors)
        if prepared.body_digest != record['body_digest'] or prepared.complete_manifest.digest != record['manifest_digest']:
            reject('document_request_changed')
        return prepared

    def claim(self, selection, request_id, *, ttl=120):
        if type(ttl) not in (int,float) or not 0 < ttl <= 120: reject('document_permit_invalid')
        with self.store._gate():
            _,record,r,deps = self._request(selection,request_id)
            if r['state'] != 'prepared': return self._view(record,r),None,None
            if r['availability'] == 'revoked': reject('document_answer_revoked')
        with self._read_dependencies(selection,deps,sources=True) as sources:
            prepared = self._prepared(record,deps,sources)
            with self.store._gate(), self._permit_lock:
                # Process identity is never silently rotated to remint inherited permits.
                if os.getpid() != self._pid: reject('document_permit_invalid')
                self._permits = {p:e for p,e in self._permits.items() if e[6] > time.monotonic()}
                if len(self._permits) >= 4: reject('document_permit_capacity')
                with self.store._transaction(reclaim=True):
                    _,current,r,actual = self._request(selection,request_id)
                    if r['state'] != 'prepared': return self._view(current,r),None,None
                    if current != record or actual != deps: reject('document_request_changed')
                    if record.get('selected_tray'):
                        roster,_ = self._selected(selection,tuple(dep.source_id for dep in deps))
                    else:
                        roster,_ = self._roster(selection)
                    if roster != deps: reject('document_roster_changed')
                    self.conversations.validate_dependencies(selection,deps)
                    self._prior_refs_current(selection,record.get('prior_answer_refs',[]))
                    config,halted = self._scope(record['scope'],self._profile_from(record),authority=True)
                    if halted: reject('document_budget_halted')
                    charged = 0
                    for item in self.store.db.execute('SELECT receipt,record FROM officer_document_requests WHERE budget_scope=?',(record['scope'],)):
                        prior = unpack(item['receipt'],RECEIPT_BYTES); self._receipt(json.loads(item['record']),prior)
                        charged += prior['charged']
                    if charged + config['ceiling'] > config['limit']: reject('document_budget_exhausted')
                    r.update(state='claimed',nonce=secrets.token_hex(32),reserved=config['ceiling'],charged=config['ceiling'])
                    self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(pack(r,RECEIPT_BYTES),request_id))
                    self.store.checkpoint('officer_document_claim_before_commit')
                self.store.checkpoint('officer_document_claim_committed')
                permit = _Permit()
                self._permits[permit] = (request_id,prepared.body_digest,record['profile_digest'],r['nonce'],self._pid,self._process_epoch,time.monotonic()+ttl)
                return self._view(record,r),permit,prepared

    def _permit_entry(self, permit, request_id, body_digest, profile_digest):
        entry = self._permits.get(permit) if type(permit) is _Permit else None
        if entry is None or entry[:3] != (request_id,body_digest,profile_digest) or entry[4:6] != (os.getpid(),self._process_epoch) or time.monotonic() >= entry[6]:
            reject('document_permit_invalid')
        return entry

    def retire_permit(self, permit):
        """Nonblocking removal only; never a claim, settlement or source read.

        True confirms this exact object cannot be consumed in this registry,
        including verified absence after consumption. False proves nothing.
        """
        if type(permit) is not _Permit or os.getpid() != self._pid:
            return False
        if not self._permit_lock.acquire(blocking=False):
            return False
        try:
            entry = self._permits.get(permit)
            if entry is not None:
                if entry[4:6] != (self._pid, self._process_epoch):
                    return False
                del self._permits[permit]
            return True
        finally:
            self._permit_lock.release()

    def consume(self, permit, selection, request_id, body_digest, profile_digest):
        with self.store._gate(), self._permit_lock:
            entry = self._permit_entry(permit,request_id,body_digest,profile_digest)
            _,record,r,deps = self._request(selection,request_id)
        # A permit can wait in memory. Recheck real bytes again, not just the
        # metadata that was current at claim. Registry lock is never held over IO.
        with self._read_dependencies(selection,deps):
            with self.store._gate(), self._permit_lock:
                if self._permit_entry(permit,request_id,body_digest,profile_digest) != entry:
                    reject('document_permit_invalid')
                _,current,r,actual = self._request(selection,request_id)
                self._scope(record['scope'],authority=True)
                self.conversations.validate_dependencies(selection,deps)
                if current != record or actual != deps or r['state'] != 'claimed' or r['nonce'] != entry[3] or r['availability'] == 'revoked':
                    reject('document_permit_invalid')
                del self._permits[permit]
                return True

    def settle_unconsumed_source_change(self, permit, selection, request_id, body_digest, profile_digest):
        # An authorized worker whose consume failed can prove no dispatch
        # through this permit only while the exact unconsumed entry is present.
        with self.store._gate(), self._permit_lock:
            entry=self._permit_entry(permit,request_id,body_digest,profile_digest)
            _,record,r,deps=self._request(selection,request_id)
            if r['state']!='claimed' or r['nonce']!=entry[3]:
                reject('document_permit_invalid')
            if self._dispatch_sources_current(selection,record,r,deps):
                return False
            del self._permits[permit]
            return True

    def dispatch_ready(self, selection, request_id, body_digest, profile_digest):
        # Called at the worker's single dispatch boundary after TLS. The caller
        # holds the store gate until it commits the in-memory dispatch decision.
        with self.store._gate():
            _,record,r,deps = self._request(selection,request_id)
            if r['state']!='claimed' or (r['body'],r['profile'])!=(body_digest,profile_digest):
                reject('document_permit_invalid')
            return self._dispatch_sources_current(selection,record,r,deps)

    def _dispatch_sources_current(self, selection, record, r, deps):
        changed=r['availability']=='revoked'
        if not changed:
            try:
                self.conversations.validate_dependencies(selection,deps)
            except AttachmentError as problem:
                if problem.code!='source_changed': raise
                changed=True
        if changed:
            # A shared admission generation can also move when an
            # unselected sibling is deleted. No POST was made in either case.
            with self.store._transaction(reclaim=True):
                ended=digest(['source_withdrawn_before_dispatch',record['fingerprint'],r['nonce']])
                r.update(state='failed',error='source_withdrawn',availability='revoked',charged=0,settled=True,
                         result_digest=ended,initial_result_digest=ended)
                self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',
                                     (pack(r,RECEIPT_BYTES),record['id']))
            return False
        return True

    def _cost(self, usage, config):
        if type(usage) is not documents.DocumentUsage: reject('document_usage_unverified')
        values = asdict(usage)
        raw = {k:values[k] for k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens','service_tier','inference_geo','speed')}
        raw['cache_creation'] = dict(ephemeral_5m_input_tokens=usage.cache_5m_tokens,ephemeral_1h_input_tokens=usage.cache_1h_tokens)
        raw['output_tokens_details'] = dict(thinking_tokens=usage.thinking_tokens)
        documents._usage(raw)
        p = config['pricing'];proof = config['profile']['strict_budget_evidence']
        if p.get('qualification_id') is not None:
            from officer_document_policy import usage_units, validate_pricing
            pricing = DocumentPricing(**p)
            validate_pricing(self._profile_from(config), pricing)
            cost = usage_units(usage)
            if not integer(cost,0,config['ceiling']): reject('document_usage_unverified')
            return cost
        if (usage.inference_geo != p['inference_geo'] or usage.input_tokens+usage.cache_creation_input_tokens+usage.cache_read_input_tokens > proof['max_input_tokens']):
            reject('document_usage_unverified')
        cost = (usage.input_tokens*p['input_rate']+usage.output_tokens*p['output_rate']+usage.cache_read_input_tokens*p['cache_read_rate']
                +usage.cache_5m_tokens*p['cache_5m_rate']+usage.cache_1h_tokens*p['cache_1h_rate'])
        if not integer(cost,0,config['ceiling']): reject('document_usage_unverified')
        return cost

    def record_result(self, selection, facts):
        if type(facts) is not ResultFacts: reject('document_result_invalid')
        ident(facts.request_id)
        for value in (facts.claim_nonce,facts.body_digest,facts.profile_digest): token(value)
        if facts.state not in ('accepted','failed','unknown') or facts.error not in ERRORS or facts.error=='source_withdrawn': reject('document_result_invalid')
        token(facts.response_digest,empty=facts.state!='accepted')
        if (type(facts.provider_message_id) is not str or len(facts.provider_message_id)>128
                or type(facts.model_id) is not str or not OPAQUE.fullmatch(facts.model_id)
                or type(facts.billing_revision) is not str or not OPAQUE.fullmatch(facts.billing_revision)
                or facts.provider_message_id and not re.fullmatch(r'msg_[A-Za-z0-9_]+',facts.provider_message_id)
                or facts.usage is not None and type(facts.usage) is not documents.DocumentUsage):
            reject('document_result_invalid')
        if facts.usage is not None:
            fields = asdict(facts.usage)
            if (any(not integer(fields[k],0) for k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens','cache_5m_tokens','cache_1h_tokens','thinking_tokens'))
                    or any(type(fields[k]) is not str or len(fields[k])>32 or not OPAQUE.fullmatch(fields[k]) for k in ('service_tier','inference_geo','speed'))):
                reject('document_result_invalid')
        fingerprint = digest(asdict(facts))
        with self.store._gate(), self.store._transaction(reclaim=True):
            _,record,r,_ = self._request(selection,facts.request_id)
            if (facts.claim_nonce,facts.body_digest,facts.profile_digest) != (r['nonce'],r['body'],r['profile']) or r['state']=='prepared':
                reject('document_result_correlation')
            if fingerprint in (r['result_digest'],r['initial_result_digest']):
                return self._view(record,r)
            if r['result_digest']:
                # At most one later settlement of a previously unverified result.
                # An exact old uncertainty replay recovers the current facts above.
                if (r['settled'] or r['result_digest'] != r['initial_result_digest']
                        or facts.state == 'unknown' or r['state'] not in ('unknown',facts.state)
                        or r['response'] and r['response'] != facts.response_digest
                        or r['provider_message'] and r['provider_message'] != facts.provider_message_id):
                    reject('document_result_conflict')
            config,_ = self._scope(record['scope'])
            valid = facts.model_id == config['profile']['model_id'] and facts.billing_revision == config['pricing']['billing_revision']
            try:
                cost = self._cost(facts.usage,config) if valid and facts.response_digest and facts.provider_message_id else None
            except (documents.DocumentRejected,AttachmentError):
                cost = None
            if cost is None:
                self.store.db.execute('UPDATE officer_document_scopes SET halt=? WHERE id=?',
                                     (pack({'v':1,'halted':True},RECEIPT_BYTES),record['scope']))
            accepted = facts.state=='accepted' and valid and bool(facts.provider_message_id) and facts.error=='none'
            if not r['initial_result_digest']: r['initial_result_digest'] = fingerprint
            r.update(state='accepted' if accepted else ('unknown' if facts.state=='unknown' else 'failed'),result_digest=fingerprint,
                     response=facts.response_digest,provider_message=facts.provider_message_id,error=facts.error,
                     usage=asdict(facts.usage) if cost is not None else None)
            if cost is not None: r.update(charged=cost,settled=True)
            else: r['error'] = facts.error if valid and facts.error!='none' else ('usage_unverified' if valid else 'billing_mismatch')
            self.store.db.execute('UPDATE officer_document_requests SET receipt=? WHERE id=?',(pack(r,RECEIPT_BYTES),record['id']))
            self.store.checkpoint('officer_document_result_before_commit')
            return self._view(record,r)

    @staticmethod
    def _candidate(candidate,record,deps):
        if (type(candidate) is not documents.AnswerCandidate or not documents._text(candidate.text,65536,empty=False)
                or len(candidate.text.encode('utf-8'))>65536 or type(candidate.citations) is not tuple
                or len(candidate.citations)>64 or type(candidate.consumed_dependencies) is not tuple
                or candidate.consumed_dependencies!=deps or type(candidate.usage_facts) is not documents.DocumentUsage
                or candidate.actions!=()):
            reject('document_answer_invalid')
        entries = record['manifest']['input_map']
        for ref in candidate.citations:
            if (type(ref) not in (documents.PDFPageReference,documents.WholeImageReference)
                    or not integer(ref.input_index,0,len(entries)-1)
                    or not integer(ref.text_start,0,len(candidate.text)) or not integer(ref.text_end,ref.text_start,len(candidate.text))):
                reject('document_answer_invalid')
            entry = entries[ref.input_index]
            if (json.loads(canonical(asdict(ref.dependency)))!=entry['dependency']
                    or ref.source_id!=ref.dependency.source_id): reject('document_answer_invalid')
            if type(ref) is documents.PDFPageReference:
                if (entry['media_type']!='application/pdf' or ref.document_index!=entry['pdf_document_index']
                        or not integer(ref.start_page,1,2**31-1) or not integer(ref.end_page_exclusive,2,2**31)
                        or ref.end_page_exclusive!=ref.start_page+1 or not documents._text(ref.cited_text,8192,empty=False)
                        or len(ref.cited_text.encode('utf-8'))>8192 or ref.kind!='upload_pdf_pages'
                        or ref.provenance!='provider_reported_single_page'):
                    reject('document_answer_invalid')
            elif (entry['media_type']!='image/png' or ref.marker!=entry['image_marker']
                    or ref.kind!='upload_whole_image' or ref.provenance!='declared_whole_image_reference'):
                reject('document_answer_invalid')

    def commit_answer(self, selection, request_id, candidate):
        raw = documents.candidate_bytes(candidate)
        candidate_digest = hash_bytes(raw)
        with self.store._gate():
            _,record,r,deps = self._request(selection,request_id)
            if (candidate.request_digest,candidate.response_digest,candidate.manifest_digest,candidate.message_id,candidate.model) != (
                    r['body'],r['response'],record['manifest_digest'],r['provider_message'],record['profile']['model_id']):
                reject('document_answer_correlation')
            self._candidate(candidate,record,deps)
            if candidate.consumed_dependencies != deps or asdict(candidate.usage_facts) != r['usage'] or candidate.actions:
                reject('document_answer_dependencies')
            if r['state']!='accepted' or not r['settled'] or r['error']!='none': reject('document_answer_unavailable')
            if r['availability']=='revoked': reject('document_answer_revoked')
            if r['availability']=='available':
                if r['candidate'] != candidate_digest: reject('document_answer_conflict')
                return self._view(record,r)
        # Exact files, including uncited sources, checked again before first publication.
        with self._read_dependencies(selection,deps):
            with self.store._gate(), self.store._transaction(reclaim=True):
                _,current,latest,actual = self._request(selection,request_id)
                self.conversations.validate_dependencies(selection,deps)
                if current!=record or actual!=deps: reject('document_request_changed')
                if latest['availability']=='revoked': reject('document_answer_revoked')
                if latest['availability']=='available':
                    if latest['candidate']!=candidate_digest: reject('document_answer_conflict')
                    return self._view(record,latest)
                if latest != r: reject('document_result_changed')
                slot = pack(dict(v=1,request=request_id,candidate=json.loads(raw)),ANSWER_BYTES)
                committed = self.store.clock()
                if type(committed) not in (int,float) or not 0 <= committed <= MAX_ANSWER_TIME_MS / 1000:
                    reject('document_record_invalid')
                committed_ms = int(committed * 1000)
                if not integer(committed_ms):
                    reject('document_record_invalid')
                latest.update(availability='available',answer_digest=hash_bytes(slot),candidate=candidate_digest,
                              answer_committed_ms=committed_ms)
                self.store.db.execute('UPDATE officer_document_requests SET answer=?,receipt=? WHERE id=?',
                                     (slot,pack(latest,RECEIPT_BYTES),request_id))
                self.store.checkpoint('officer_document_answer_before_commit')
                return self._view(record,latest)

    @staticmethod
    def _answer_value(row,receipt,request_id):
        if receipt['availability']!='available': reject('document_answer_unavailable')
        if hash_bytes(row['answer'])!=receipt['answer_digest']: reject('document_record_invalid')
        value=unpack(row['answer'],ANSWER_BYTES)
        if (set(value)!={'v','request','candidate'} or value['v']!=1 or value['request']!=request_id
                or hash_bytes(encoded(value['candidate']))!=receipt['candidate']):
            reject('document_record_invalid')
        return value['candidate']

    def publish(self, selection, request_id, publish):
        if not callable(publish): reject('document_publication_invalid')
        with self.store._gate():
            _,record,r,deps = self._request(selection,request_id)
            if r['availability'] != 'available': reject('document_answer_unavailable')
        with self._read_dependencies(selection,deps):
            with self.store._gate():
                row,current,latest,actual = self._request(selection,request_id)
                self.conversations.validate_dependencies(selection,deps)
                if current!=record or actual!=deps or latest['availability']!='available': reject('document_answer_unavailable')
                return publish(self._answer_value(row,latest,request_id))

    def replay(self, selection, request_id):
        """Exact governed prior-answer material only; no dispatch authority."""
        return self.publish(selection,request_id,lambda value: value)


def invalidate_source_in_transaction(store, source_id):
    """Called after the source/membership fence in that SAME writer transaction."""
    if not store.db.in_transaction: reject('document_transaction_required')
    ident(source_id)
    rows = store.db.execute('''SELECT r.* FROM officer_document_requests r
        JOIN officer_document_dependencies d ON d.request_id=r.id WHERE d.source_id=? LIMIT 65''',(source_id,)).fetchall()
    if len(rows)>MAX_DEPENDENTS: reject('document_dependency_capacity')
    if not rows: return
    source = store.db.execute('SELECT deletion_generation,access_generation FROM files WHERE id=?',(source_id,)).fetchone()
    if source is None or not source['deletion_generation'] and store.db.execute('SELECT 1 FROM conversation_files WHERE source_id=? AND available=0',(source_id,)).fetchone() is None:
        reject('document_revocation_required')
    for row in rows:
        record = OfficerDocumentLedger._record(row); r = unpack(row['receipt'],RECEIPT_BYTES)
        OfficerDocumentLedger._receipt(record,r)
        if r['availability']=='revoked': continue
        r.update(availability='revoked')
        store.db.execute('UPDATE officer_document_requests SET answer=zeroblob(65536),receipt=? WHERE id=?',
                         (pack(r,RECEIPT_BYTES),row['id']))
