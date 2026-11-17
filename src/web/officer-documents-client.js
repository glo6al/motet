/* Owner HTTP client for governed Officer documents. No persistence or provider transport. */
(function () {
'use strict';
function createOfficerDocumentsClient(options) {
  const o = options || {}, active = o.enabled === true && o.demo === false;
  let documentsEnabled=o.documentsEnabled!==false;
  const transport = o.fetch, auth = typeof o.auth === 'function' ? o.auth : () => '';
  const crypt = o.crypto, Controller = o.AbortController;
  const journal = o.journal ?? null;
  const ID = /^[a-f0-9]{32}$/, OPAQUE = /^[A-Za-z0-9_.:-]{1,160}$/;
  const attemptId = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
  const sha256 = /^[a-f0-9]{64}$/;
  const pendingPhases = new Set(['captured','prepare_may_have_started','execute_may_have_started']);
  const pendingMessages = {
    pending_capacity:'This browser is already holding the maximum number of file questions.',
    pending_conflict:'This conversation has a pending file question. Look again before changing its files.',
    pending_context_changed:'The file question or its owner context changed before it could be recorded.',
    pending_invalid:'This file question does not fit the browser’s pending record.',
    pending_presentation_failed:'The recorded file question could not be moved out of the composer. Look again.',
    pending_storage_invalid:'This browser’s pending file-question records could not be verified.',
    pending_storage_unavailable:'The browser cannot store pending file questions right now.',
    pending_storage_write_failed:'The browser could not verify that it saved the pending file question.'
  };
  const states = new Set(['prepared','preparing','retained_local','answered','failed','unknown']);
  const deliveries = new Set(['not_sent','retained_local','confirmed','unknown']);
  const priorAnswerPolicy = 'recent_available_8', priorAnswerLimit = 8;
  let publication=0,dialogVersion=0,journalHold=false;
  let deletionOps=new Map(),deletionMetas=new Map(),operationHandles=new Map(),confirmationHandles=new Map(),cursorHandles=new Map(),managementFiles=new Map(),pendingDeletions=new Map(),confirmKeys=new Map(),revokedSources=new Set(),managementPath=[null],managementIndex=0,activeDialog=null;
  let epoch = 0, owner = '', dead = false, controller = null, capability = null;
  let uploadDrafts=new Map(),admissionAttempts=new Map();
  let writer = null, privateRequests = new Map(), handles = new Map(), knownDocument = false, everKnownDocument = false;
  let unresolved = new Map(), attemptedDrafts = new Set(), ownerIdentity = null, suspended = null;
  const empty = () => ({schemaVersion:1,fixture:false,revision:0,complete:false,context:null,routing:'blocked',available:false,reason:'Motet documents are unavailable.',conversation:null,limits:null,formats:[],busy:false,uncertain:false,error:null,draft:null,savedDrafts:[],uploads:[],lastSend:null,files:[],requests:[],answers:[],preview:null,usableFileCount:null,pendingMatch:null,documentsEnabled,management:{available:false,complete:false,page:0,files:[],firstHandle:null,previousHandle:null,nextHandle:null},deletions:[],deletion:null});
  let state = empty();
  const clone = value => JSON.parse(JSON.stringify(value));
  const fail = (code, message) => { const e = new Error(message); e.code = code; e.officerDocumentClient = true; return e; };
  const metadataRejections = new WeakSet();
  const priorWindowRefusals = new WeakSet(), trayRefusals = new WeakSet();
  const refusedFiles = new Map();   // local refused row id -> the selected File, unread and never published, for an explicit Add it
  const rejectMetadata = (code,message) => { const e=fail(code,message);metadataRejections.add(e);return e; };
  const invalid = () => fail('invalid_response','The document response could not be verified.');
  const object = v => v && typeof v === 'object' && !Array.isArray(v);
  const str = (v,n=4000) => typeof v === 'string' && v.length <= n;
  const number = v => Number.isSafeInteger(v) && v >= 0;
  const identity = v => typeof v === 'string' && ID.test(v);
  function emit() { state.revision++; if (typeof o.changed === 'function') { try { o.changed(clone(state)); } catch (_) {} } }
  function clear(nextContext=null) {
    epoch++;publication++;dialogVersion++;if (controller) controller.abort(); controller = null;
    journalHold=false;
    capability = null; writer = null;uploadDrafts.clear();refusedFiles.clear();admissionAttempts.clear(); ownerIdentity = null; privateRequests.clear(); handles.clear(); unresolved.clear(); attemptedDrafts.clear();deletionOps.clear();deletionMetas.clear();operationHandles.clear();confirmationHandles.clear();cursorHandles.clear();managementFiles.clear();pendingDeletions.clear();confirmKeys.clear();revokedSources.clear();managementPath=[null];managementIndex=0;activeDialog=null;
    const revision = state.revision; state = empty(); state.revision = revision; state.context = nextContext;
  }
  function invalidate() {
    if(owner&&auth()!==owner)suspended=null;else if(owner&&ownerIdentity&&state.context){suspended=unresolved.size||attemptedDrafts.size||pendingDeletions.size||deletionOps.size||managementIndex?{ownerIdentity,context:clone(state.context),unresolved:clone([...unresolved]),attemptedDrafts:[...attemptedDrafts],confirmKeys:clone([...confirmKeys]),pendingDeletions:clone([...pendingDeletions]),deletionMetas:clone([...deletionMetas]),revokedSources:[...revokedSources],managementPath:[...managementPath],managementIndex}:null;}
    clear(); owner = ''; knownDocument = false; emit();
  }
  function syncOwner() { const key = auth(); if (key !== owner) { if(owner)suspended=null;clear(); owner = ''; knownDocument = false; emit(); return false; } return true; }
  function capture() { if (dead || !syncOwner() || !owner) throw fail('context_changed','Reconnect this Motet conversation.'); return {epoch,key:owner,publication}; }
  function guard(t) { if (dead || t.epoch !== epoch) throw fail('context_changed','Motet context changed.'); if(t.publicationBound&&t.publication!==publication)throw fail('publication_changed','The governed file view changed. Refresh before opening it.');if (auth() !== t.key) { invalidate(); throw fail('context_changed','The owner session changed.'); } }
  const query = data => '?' + new URLSearchParams(data).toString();
  function token() { if (!crypt || typeof crypt.randomUUID !== 'function') throw fail('crypto_unavailable','Secure document identity is unavailable.'); return crypt.randomUUID(); }
  function attachmentModel() { return state.context.modelId==='@automatic'?'auto':state.context.modelId; }
  function bindingMatches(b, model=true) { return object(b) && b.kind === 'officer' && b.id === 'officer' && b.scopeId === state.context.scopeId && b.conversationId === state.conversation.id && ['auto','claude-haiku-5-5'].includes(b.modelId) && (!model || b.modelId === attachmentModel() && capability && b.capabilityRevision===capability.capabilityRevision) && str(b.capabilityRevision,160); }
  function selection() { return {destinationId:'officer',scopeId:state.context.scopeId,modelId:attachmentModel(),conversationId:state.conversation.id,capabilityRevision:capability.capabilityRevision}; }
  function fence() { if (!writer || !state.draft) throw fail('resume_required','Explicitly resume the saved draft first.'); return {revision:state.draft.revision,writerGeneration:state.draft.writerGeneration,writerNonce:writer.nonce}; }
  const draftHeader = () => ({'X-Attachment-Draft-Capability':writer.cap});
  async function request(t, path, method='GET', body, headers={},authority='documents') {
    guard(t);
    const parent=controller.signal, operation=new Controller();let timedOut=false,reader=null;
    const abort=()=>operation.abort();parent.addEventListener('abort',abort,{once:true});
    const check=()=>{guard(t);if(operation.signal.aborted)throw fail('request_timeout','The document operation could not be confirmed. Use status refresh.');};
    const schedule=o.setTimeout || setTimeout, unschedule=o.clearTimeout || clearTimeout;
    const deadline=method==='PUT'?30000:method==='POST'&&/^\/requests\/[a-f0-9]{32}\/(send|ask)$/.test(path)?130000:15000;
    let timer;const timeout=new Promise((_,reject)=>{timer=schedule(()=>{timedOut=true;operation.abort();reject(fail('request_timeout','The document operation could not be confirmed. Use status refresh.'));},deadline);});
    const perform=async()=>{
    check();
    if (method !== 'GET'&&authority==='documents') { state.uncertain = true; emit(); check(); }
    const response = await transport('/api/attachments'+path, {method,credentials:'same-origin',cache:'no-store',redirect:'error',signal:operation.signal,
      headers:{'X-Live-Room-Key':t.key,...(body !== undefined ? {'Content-Type':'application/json'} : {}),...headers},
      ...(body !== undefined ? {body:headers['Content-Type'] === 'application/octet-stream' ? body : JSON.stringify(body)} : {})});
    check();
    let value;
    if (response.body && typeof response.body.getReader === 'function') {
      reader=response.body.getReader(); let size=0, chunks=[];
      try { while (true) { const chunk=await reader.read(); check(); if (chunk.done) break; size+=chunk.value.byteLength; if(size>262144) { await reader.cancel(); check(); throw invalid(); } chunks.push(chunk.value); }
        const bytes=new Uint8Array(size); let offset=0; for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;} value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
      } finally { try{reader.releaseLock();}catch(_){} }
    } else { value=await response.json(); check(); if(JSON.stringify(value).length>262144) throw invalid(); }
    if (!object(value) || value.schemaVersion !== 1 || value.fixture !== false || !str(value.serverTime,80)) throw invalid();
    if (!response.ok || value.error) {
      const code=object(value.error)&&/^[a-z_]{1,64}$/.test(value.error.code) ? value.error.code : 'request_failed';
      const failure=fail(code,'The document operation could not be completed. Use status refresh before continuing.');failure.status=response.status;
      if(!response.ok&&response.status===409&&code==='document_prior_window_changed')priorWindowRefusals.add(failure);
      if(!response.ok&&response.status===400&&code==='document_tray_invalid')trayRefusals.add(failure);
      throw failure;
    }
    return value;
    };
    try { return await Promise.race([perform(),timeout]); }
    finally {unschedule(timer);parent.removeEventListener('abort',abort);if(reader&&timedOut){try{const cancelled=reader.cancel();if(cancelled&&cancelled.catch)cancelled.catch(()=>{});}catch(_){}}}
  }
  async function pages(t,path,params,maximum,validate) {
    const records=[], seen=new Set(), cursors=new Set(); let cursor=null;
    for(let page=0;page<Math.max(200,Math.ceil(maximum/50));page++) {
      const r=await request(t,path+query({...params,...(cursor?{cursor}:{})})); guard(t);
      if(!Array.isArray(r.items)||r.items.length>50 || !(r.nextCursor===null || str(r.nextCursor,4096)&&r.nextCursor.length)) throw invalid();
      for(const item of r.items) { const key=validate(item); if(seen.has(key)||records.length>=maximum) throw fail('incomplete_history','Document history exceeds its safe bounds or contains duplicate identities.'); seen.add(key); records.push(item); }
      if(r.nextCursor===null) return records;
      if(cursors.has(r.nextCursor)) throw fail('incomplete_history','Document history contains a repeated cursor.');
      cursors.add(r.nextCursor);cursor=r.nextCursor;
    }
    throw fail('incomplete_history','Document history exceeds its page limit.');
  }
  const filePublic = f => ({id:f.id,name:f.name,mediaType:f.mediaType,bytes:f.bytes,state:f.state || null,error:typeof f.error==='string'&&/^[a-z_]{1,64}$/.test(f.error)?f.error:f.error?'file_unavailable':null,sourceGeneration:f.sourceGeneration || null,preparationKind:f.preparationKind || null,available:f.available === true,retainedAt:f.retainedAt || null,messageId:f.messageId || null,reason:f.reason || null,officerDelivery:f.officerDelivery ? {state:f.officerDelivery.state,requestId:f.officerDelivery.requestId} : null,previewHandle:null,progress:null});
  const retainedFilePublic=f=>({...filePublic(f),admissionRequestId:f.admissionRequestId});
  function validateFile(f,retained=false) {
    if(!object(f)||!identity(f.id)||!str(f.name,255)||!['application/pdf','image/png'].includes(f.mediaType)||!number(f.bytes)||(f.reason!==undefined&&f.reason!==null&&!str(f.reason,1000)))throw invalid();
    if(retained&&(!Object.hasOwn(f,'available')||typeof f.available!=='boolean'||
      !Object.hasOwn(f,'preparationKind')||!['officer_direct','text'].includes(f.preparationKind)||
      !Object.hasOwn(f,'admissionRequestId')||!(f.admissionRequestId===null||identity(f.admissionRequestId))))throw invalid();
    if(f.officerDelivery&&(!deliveries.has(f.officerDelivery.state)||!(f.officerDelivery.requestId===null||identity(f.officerDelivery.requestId))))throw invalid();
    return f.id;
  }
  function requestPublic(r) { return {requestId:r.requestId,draftId:r.draftId,canAbandon:r.canAbandon,abandoned:r.abandoned,state:r.state,failure:r.failure || null,notAskedReason:r.notAskedReason || null,officerDelivery:{state:r.officerDelivery.state,requestId:r.officerDelivery.requestId},answerId:r.answerId,question:r.question,createdAt:r.createdAt,analysisOnly:true,
    ...(r.legacyFollowUpIds===undefined?{}:{legacyFollowUpIds:[...r.legacyFollowUpIds]}),
    ...(r.questionFileIds===undefined?{}:{questionFileIds:[...r.questionFileIds],draftFileIds:[...r.draftFileIds]})}; }
  function validateRequest(r, history=false) {
    if(!object(r)||!identity(r.requestId)||!states.has(r.state)||r.analysisOnly!==true||!Array.isArray(r.actions)||r.actions.length||!object(r.officerDelivery)||!deliveries.has(r.officerDelivery.state)||!(r.officerDelivery.requestId===null||r.officerDelivery.requestId===r.requestId)||!(r.answerId===null||identity(r.answerId))||!(r.failure===null||r.failure==='provider_rejected'||r.failure==='tray_unavailable'&&['failed','retained_local'].includes(r.state)&&['not_sent','retained_local'].includes(r.officerDelivery.state))) throw invalid();
    if(!(r.draftId===null||identity(r.draftId))||typeof r.canAbandon!=='boolean'||typeof r.abandoned!=='boolean'||r.canAbandon!==(r.state==='prepared')||(r.canAbandon&&(r.abandoned||r.failure!==null||r.notAskedReason!=null||r.officerDelivery.state!=='not_sent'||r.officerDelivery.requestId!==null||r.answerId!==null))||(r.abandoned&&(r.state!=='failed'||r.failure!==null||r.notAskedReason!=null||r.canAbandon||r.officerDelivery.state!=='not_sent'||r.officerDelivery.requestId!==null||r.answerId!==null)))throw invalid();
    if(r.questionFileIds!==undefined&&(!Array.isArray(r.questionFileIds)||!r.questionFileIds.length||
      r.questionFileIds.length>5||r.questionFileIds.some(id=>!identity(id))||new Set(r.questionFileIds).size!==r.questionFileIds.length||
      !Array.isArray(r.draftFileIds)||(r.draftId===null?r.draftFileIds.length!==0:r.draftFileIds.length===0)||
      new Set(r.draftFileIds).size!==r.draftFileIds.length||r.draftFileIds.length>r.questionFileIds.length||
      r.draftFileIds.some((id,index)=>r.questionFileIds[r.questionFileIds.length-r.draftFileIds.length+index]!==id)))throw invalid();
    if(r.legacyFollowUpIds!==undefined&&(!Array.isArray(r.legacyFollowUpIds)||r.legacyFollowUpIds.length>50||
      r.legacyFollowUpIds.some(id=>!identity(id))||new Set(r.legacyFollowUpIds).size!==r.legacyFollowUpIds.length||
      r.state!=='answered'||r.questionFileIds!==undefined))throw invalid();
    const previous=privateRequests.get(r.requestId);if(previous&&(previous.draftId!==r.draftId||
      JSON.stringify(previous.questionFileIds)!==JSON.stringify(r.questionFileIds)||
      JSON.stringify(previous.draftFileIds)!==JSON.stringify(r.draftFileIds)))throw invalid();
    if(!(r.notAskedReason===undefined||r.notAskedReason===null||['no_question','budget_exhausted','context_too_large','reading_unavailable'].includes(r.notAskedReason))||(r.notAskedReason&&r.state!=='retained_local'))throw invalid();
    if(history&&(!bindingMatches(r.destination,false)||!str(r.question)||!Number.isFinite(r.createdAt)||r.createdAt<0||!str(r.requestCapability,8192)||!str(r.fingerprint,128))) throw invalid();
    return r.requestId;
  }
  function addHandle(source) { const handle=token(); if(handles.has(handle)) throw invalid(); handles.set(handle,source); return handle; }
  function answerWindow(value, request) {
    const window=value.priorAnswerWindow;
    if(window===null)return null;
    if(!object(window)||Object.keys(window).length!==3||window.policy!==priorAnswerPolicy||
      !number(window.includedCount)||window.includedCount>priorAnswerLimit||typeof window.omittedOlder!=='boolean'||
      (window.omittedOlder&&window.includedCount!==priorAnswerLimit)||value.state!=='available'||request.draftId!==null)throw invalid();
    return {policy:window.policy,includedCount:window.includedCount,omittedOlder:window.omittedOlder};
  }
  async function answer(t,r) {
    const value=await request(t,'/answers/'+r.answerId+query({requestId:r.requestId}),'GET',undefined,{'X-Attachment-Request-Capability':r.requestCapability}); guard(t);
    if(value.id!==r.answerId || value.requestId!==r.requestId || value.analysisOnly!==true || !object(value.model) || value.model.id!=='claude-haiku-5-5' || !str(value.model.label,100)||!Array.isArray(value.actions)||value.actions.length||!Array.isArray(value.citations)||value.citations.length>50||!Array.isArray(value.suppliedFileIds)||value.suppliedFileIds.length>50||value.suppliedFileIds.some(id=>!identity(id))||new Set(value.suppliedFileIds).size!==value.suppliedFileIds.length) throw invalid();
    if(!['available','source_unavailable'].includes(value.state)|| (value.state==='available' ? !str(value.text,20000)||value.sourceValidation!=='current'||!value.suppliedFileIds.length : value.text!==null||value.citations.length||value.suppliedFileIds.length||value.sourceValidation!=='invalid')) throw invalid();
    if(!(value.answeredAt===null||Number.isFinite(value.answeredAt)&&value.answeredAt>=0&&value.answeredAt<=Number.MAX_SAFE_INTEGER/1000))throw invalid();
    const priorAnswerWindow=answerWindow(value,r);
    if(value.state==='available'&&(value.text.includes('IMAGE_SOURCE')||value.suppliedFileIds.some(id=>!state.files.some(f=>f.id===id&&f.available))))throw invalid();
    const seen=new Set(),supplied=new Set(value.suppliedFileIds),citations=[];
    for(const c of value.citations) {
      const loc=c.location, good=object(loc)&&(Object.keys(loc).length===1)&&(Number.isSafeInteger(loc.page)&&loc.page>=1&&loc.page<=100000||loc.image===true);
      if(!str(c.id,80)||seen.has(c.id)||!identity(c.fileId)||!supplied.has(c.fileId)||c.kind!=='upload'||c.available!==true||!str(c.label,100)||!good||!str(c.sourceCapability,8192)||!c.sourceCapability.length) throw invalid();
      seen.add(c.id); citations.push({id:c.id,kind:'upload',fileId:c.fileId,label:c.label,location:clone(loc),available:true,previewHandle:addHandle({fileId:c.fileId,cap:c.sourceCapability,location:clone(loc)})});
    }
    if(value.state==='available'&&value.text===''&&!citations.some(c=>c.location.image===true))throw invalid();
    return {id:value.id,requestId:value.requestId,state:value.state,text:value.text,suppliedFileIds:[...value.suppliedFileIds],answeredAt:value.answeredAt,priorAnswerWindow,model:{id:value.model.id,label:value.model.label},analysisOnly:true,sourceValidation:value.sourceValidation,citations};
  }
  function setDraft(d,authority=false) {
    if(!object(d)||!identity(d.id)||!bindingMatches(d.destination)||!str(d.text)||!number(d.revision)||d.revision<1||!number(d.writerGeneration)||d.writerGeneration<1||!Array.isArray(d.files)||d.files.length>50||!['active','discarded','admitted'].includes(d.state)) throw invalid();
    const ids=new Set(); for(const f of d.files){validateFile(f);if(ids.has(f.id))throw invalid();ids.add(f.id);}
    if(authority) { if(!str(d.capability,8192)||!d.capability||!str(d.writerNonce,8192)||!d.writerNonce)throw invalid();writer={cap:d.capability,nonce:d.writerNonce}; }
    state.draft={id:d.id,text:d.text,state:d.state,revision:d.revision,writerGeneration:d.writerGeneration,files:d.files.map(filePublic),readOnly:!writer,createdAt:d.createdAt,updatedAt:d.updatedAt};
  }
  function requireCompleteSelection() {
    const draft=state.draft;
    for(const local of state.uploads)if(local.state!=='ready'||!local.fileId||!draft||uploadDrafts.get(local.id)!==draft.id||!draft.files.some(f=>f.id===local.fileId&&f.state==='ready'))throw fail('selection_not_ready','Remove failed or unprocessed selections before Send. Every selected file must belong to the ready draft.');
  }
  function retireAdmittedSelection(requestId,view,retainedIds=null) {
    const attempt=admissionAttempts.get(requestId);
    if(!attempt||!view||view.requestId!==requestId||view.draftId!==attempt.draftId||view.state==='prepared'||!['retained_local','confirmed','unknown'].includes(view.officerDelivery.state)||view.officerDelivery.requestId!==requestId)return;
    state.uploads=state.uploads.filter(local=>{const exact=attempt.membership.get(local.id);if(!exact||local.fileId!==exact||uploadDrafts.get(local.id)!==attempt.draftId||retainedIds&&!retainedIds.has(exact))return true;uploadDrafts.delete(local.id);return false;});
    if(!state.uploads.some(local=>attempt.membership.has(local.id)))admissionAttempts.delete(requestId);
  }
  function confirmationUnresolved() { return [...pendingDeletions.values()].some(h=>h.kind==='confirm'); }
  function cleanupUncertain() { return pendingDeletions.size>0; }
  function syncUncertainty() { state.uncertain=journalHold||unresolved.size>0||cleanupUncertain()||state.requests.some(r=>['prepared','preparing','unknown'].includes(r.state));if(state.uncertain&&state.routing==='ordinary')state.routing='blocked'; }
  function withdrawPublication() { publication++;state.usableFileCount=null;state.pendingMatch=null;state.answers=[];state.preview=null;state.files=state.files.map(f=>({...f,previewHandle:null}));handles.clear();state.complete=false;emit(); }
  const operationIdentity = v => v.id+':'+v.revision;
  function validateDeletion(v,expected=null) {
    if(!object(v)||!identity(v.id)||!identity(v.fileId)||!state.conversation||v.conversationId!==state.conversation.id||v.scopeId!==state.context.scopeId||!str(v.name,255)||!number(v.bytes)||!number(v.revision)||v.revision<1||!['confirm','deleting','deleted','partial_failure','unknown'].includes(v.state)||!number(v.releasedBytes)||v.releasedBytes>v.bytes||typeof v.sourceUnavailable!=='boolean'||typeof v.canConfirm!=='boolean'||typeof v.hasUnresolvedQuestion!=='boolean'||!(v.removedAt===null||typeof v.removedAt==='number'&&Number.isFinite(v.removedAt)&&v.removedAt>=0)||![null,'source_in_use','identity_changed','removal_unconfirmed'].includes(v.reason)||!str(v.fingerprint,64)||!/^[a-f0-9]{64}$/.test(v.fingerprint)||!str(v.capability,64)||!/^[a-f0-9]{64}$/.test(v.capability))throw invalid();
    if(v.state==='confirm'?(v.sourceUnavailable||v.releasedBytes!==0):(!v.sourceUnavailable||v.canConfirm))throw invalid();
    if((v.state==='deleted')!==(v.removedAt!==null))throw invalid();
    if(v.canConfirm&&v.state!=='confirm'||v.state==='deleted'&&(v.releasedBytes!==v.bytes||v.reason!==null)||v.state!=='deleted'&&v.releasedBytes!==0)throw invalid();
    if(expected&&(v.id!==expected.id||v.fileId!==expected.fileId||v.fingerprint!==expected.fingerprint||v.revision<expected.revision))throw invalid();
    const prior=deletionMetas.get(v.id);if(prior&&(prior.fileId!==v.fileId||v.revision<prior.revision||prior.sourceUnavailable&&!v.sourceUnavailable))throw invalid();
    return v.id;
  }
  function operationUncertain(v) { const hold=pendingDeletions.get('confirm:'+v.id);return Boolean(hold&&hold.fileId===v.fileId&&(v.state!=='confirm'||hold.revision===v.revision)); }
  function deletionPublic(v) { return {id:v.id,fileId:v.fileId,conversationId:v.conversationId,scopeId:v.scopeId,name:v.name,bytes:v.bytes,revision:v.revision,state:v.state,releasedBytes:v.releasedBytes,sourceUnavailable:v.sourceUnavailable,hasUnresolvedQuestion:v.hasUnresolvedQuestion,removedAt:v.removedAt,reason:v.reason,canConfirm:v.canConfirm,operationHandle:v.operationHandle,uncertain:operationUncertain(v)}; }
  function updateManagementSnapshot() {
    state.management.files=[...managementFiles.values()].map(f=>{const op=f.deletionId&&deletionOps.get(f.deletionId);return {id:f.id,name:f.name,mediaType:f.mediaType,bytes:f.bytes,available:f.available,hasUnresolvedQuestion:f.hasUnresolvedQuestion,managementGeneration:f.managementGeneration,deletionId:f.deletionId,canDelete:state.management.available&&state.management.complete&&f.available&&!revokedSources.has(f.id)&&![...pendingDeletions.values()].some(h=>h.kind==='confirm'||h.fileId===f.id),operationHandle:op?op.operationHandle:null};});
    state.deletions=[...deletionOps.values()].map(deletionPublic).sort((a,b)=>a.id.localeCompare(b.id));
  }
  function refreshDialog(v) {
    if(!activeDialog||activeDialog.id!==v.id)return;
    if(activeDialog.fingerprint!==v.fingerprint){dialogVersion++;activeDialog=null;confirmationHandles.clear();state.deletion=null;return;}
    const entry=activeDialog.confirmationHandle&&confirmationHandles.get(activeDialog.confirmationHandle);
    state.deletion={...deletionPublic(v),confirmationHandle:v.canConfirm&&entry&&!entry.used?activeDialog.confirmationHandle:null,uncertain:operationUncertain(v),confirmation:{copyCount:1,scope:'watch_original',retained:['original_file','provider_copies','conversation_messages']}};
  }
  function recordDeletion(v,read=false) {
    validateDeletion(v);const old=deletionOps.get(v.id);
    if(old&&old.fingerprint!==v.fingerprint&&(old.sourceUnavailable||v.state!=='confirm'||v.revision<=old.revision))throw invalid();
    const same=old&&old.fingerprint===v.fingerprint&&old.capability===v.capability;
    const operationHandle=same?old.operationHandle:token();
    if(!same){for(const [h,prior]of operationHandles)if(prior.id===v.id)operationHandles.delete(h);for(const key of confirmKeys.keys())if(key.startsWith(v.id+':')&&key!==v.id+':'+v.revision)confirmKeys.delete(key);if(operationHandles.has(operationHandle))throw invalid();operationHandles.set(operationHandle,{id:v.id,fileId:v.fileId,fingerprint:v.fingerprint,capability:v.capability,revision:v.revision});}
    const current={...deletionPublic({...v,operationHandle}),fingerprint:v.fingerprint,capability:v.capability};deletionOps.set(v.id,current);deletionMetas.set(v.id,{fileId:v.fileId,revision:v.revision,sourceUnavailable:v.sourceUnavailable});
    if(v.sourceUnavailable){if(!revokedSources.has(v.fileId)){state.usableFileCount=null;if(state.answers.length||state.preview||handles.size)withdrawPublication();}revokedSources.add(v.fileId);const file=managementFiles.get(v.fileId);if(file)file.available=false;}
    if(v.state==='confirm'){const file=managementFiles.get(v.fileId);if(file){file.managementGeneration=Math.max(file.managementGeneration,v.revision);file.deletionId=v.id;}}
    const hold=pendingDeletions.get('confirm:'+v.id);
    if(read&&hold&&hold.fileId===v.fileId&&v.revision>=hold.revision){if(v.state==='confirm'&&v.revision===hold.revision)hold.needsRead=false;else pendingDeletions.delete('confirm:'+v.id);}
    refreshDialog(current);updateManagementSnapshot();syncUncertainty();return current;
  }
  function closeDeletionView() { dialogVersion++;activeDialog=null;confirmationHandles.clear();state.deletion=null; }
  function showDeletion(v) {
    closeDeletionView();const handle=v.canConfirm?token():null;
    activeDialog={id:v.id,fingerprint:v.fingerprint,confirmationHandle:handle,version:dialogVersion};
    if(handle)confirmationHandles.set(handle,{id:v.id,fileId:v.fileId,fingerprint:v.fingerprint,capability:v.capability,revision:v.revision,version:dialogVersion,used:false});
    refreshDialog(v);
  }
  async function readManagement(t,cursor=managementPath[managementIndex]||null,index=managementIndex,path=managementPath) {
    guard(t);if(!state.conversation)throw fail('management_unavailable','Choose an existing Motet file conversation.');
    state.management.complete=false;state.management.files=state.management.files.map(f=>({...f,canDelete:false}));cursorHandles.clear();const firstHandle=token();cursorHandles.set(firstHandle,{cursor:null,index:0,path:[null]});state.management.firstHandle=firstHandle;state.management.previousHandle=null;state.management.nextHandle=null;emit();guard(t);
    const q={destinationId:'officer',scopeId:state.context.scopeId,...(cursor?{cursor}:{})};
    const value=await request(t,'/conversations/'+state.conversation.id+'/management'+query(q));guard(t);
    if(!Array.isArray(value.files)||value.files.length>50||!Array.isArray(value.deletions)||value.deletions.length>50||!(value.nextCursor===null||str(value.nextCursor,4096)&&value.nextCursor.length))throw invalid();
    const sourceMap=new Map(),operationIds=new Set();
    for(const f of value.files){if(!object(f)||!identity(f.id)||sourceMap.has(f.id)||!str(f.name,255)||!['application/pdf','image/png'].includes(f.mediaType)||!number(f.bytes)||typeof f.available!=='boolean'||typeof f.hasUnresolvedQuestion!=='boolean'||!number(f.managementGeneration)||!(f.deletionId===null||identity(f.deletionId))||revokedSources.has(f.id)&&f.available)throw invalid();sourceMap.set(f.id,{id:f.id,name:f.name,mediaType:f.mediaType,bytes:f.bytes,available:f.available,hasUnresolvedQuestion:f.hasUnresolvedQuestion,managementGeneration:f.managementGeneration,deletionId:f.deletionId});}
    for(const v of value.deletions){validateDeletion(v);const f=sourceMap.get(v.fileId);if(!f||f.deletionId!==v.id||f.name!==v.name||f.bytes!==v.bytes||v.sourceUnavailable&&f.available||operationIds.has(v.id))throw invalid();operationIds.add(v.id);}
    for(const f of sourceMap.values())if(f.deletionId&&!operationIds.has(f.deletionId))throw invalid();
    if(value.nextCursor&&path.slice(0,index+1).includes(value.nextCursor))throw invalid();if(index>=10000)throw fail('management_limit','The management page navigation limit was reached.');
    managementFiles=sourceMap;managementPath=path.slice(0,index+1);managementIndex=index;cursorHandles.clear();
    const navigation=(nextCursor,nextIndex,nextPath)=>{const h=token();if(cursorHandles.has(h))throw invalid();cursorHandles.set(h,{cursor:nextCursor,index:nextIndex,path:nextPath});return h;};
    state.management={available:true,complete:true,page:index+1,files:[],firstHandle:navigation(null,0,[null]),previousHandle:index?navigation(managementPath[index-1],index-1,managementPath.slice(0,index)):null,nextHandle:value.nextCursor?navigation(value.nextCursor,index+1,[...managementPath,value.nextCursor]):null};
    for(const v of value.deletions)recordDeletion(v,true);
    for(const [key,hold] of pendingDeletions)if(hold.kind==='prepare'&&managementFiles.has(hold.fileId))pendingDeletions.delete(key);
    updateManagementSnapshot();syncUncertainty();emit();guard(t);
  }
  async function readDeletionStatuses(t) {
    for(const hold of [...pendingDeletions.values()])if(hold.kind==='confirm'){const exact=deletionOps.get(hold.id);if(!exact)continue;const v=await request(t,'/deletions/'+exact.id,'GET',undefined,{'X-Attachment-Deletion-Capability':exact.capability});guard(t);validateDeletion(v,exact);recordDeletion(v,true);}
  }
  function requireManagement() { if(!state.conversation||!state.management.available||!state.management.complete)throw fail('management_unavailable','Refresh the current Files page before changing a kept file.'); }

  function pendingFailure(code) {
    const known=Object.hasOwn(pendingMessages,code)?code:'pending_storage_unavailable';
    return fail(known,pendingMessages[known]);
  }
  async function journalCall(t,call) {
    try {const result=await call();guard(t);return result;}
    catch(e){guard(t);throw pendingFailure(e?.code);}
  }
  function validateReceipt(receipt) {
    if(!object(receipt)||receipt.schemaVersion!==1||receipt.route!=='files'||
      receipt.ownerDigest!==ownerIdentity||receipt.scopeId!==state.context.scopeId||
      typeof receipt.id!=='string'||!attemptId.test(receipt.id)||typeof receipt.prepareDigest!=='string'||!sha256.test(receipt.prepareDigest)||
      !(receipt.conversationId===null||identity(receipt.conversationId))||!(receipt.draftId===null||identity(receipt.draftId))||
      !(receipt.requestId===null||identity(receipt.requestId))||!pendingPhases.has(receipt.phase)||!str(receipt.question)||
      !Array.isArray(receipt.files)||receipt.files.length>5||receipt.files.some(f=>!object(f)||!identity(f.id)||!str(f.name,255))||
      (receipt.draftId===null?!receipt.question.trim():receipt.files.length===0))throw pendingFailure('pending_storage_invalid');
    return receipt;
  }
  async function readJournal(t,requireReady=false) {
    if(!journal)return null;
    const view=await journalCall(t,()=>journal.sync());
    if(!object(view)||!['ready','inactive','checking','unavailable'].includes(view.status))throw pendingFailure('pending_storage_invalid');
    const receipt=view.receipt===null?null:validateReceipt(view.receipt);
    if(view.status==='ready'||receipt)journalHold=receipt!==null;
    syncUncertainty();
    if(requireReady&&view.status!=='ready')throw pendingFailure(view.error);
    if(requireReady&&receipt)throw pendingFailure('pending_conflict');
    return receipt;
  }
  function pendingSources(draft,kept) {return [...kept,...(draft?.files||[])].map(({id,name})=>({id,name}));}
  function samePendingSources(a,b) {
    return a.length===b.length&&a.every((source,i)=>source.id===b[i].id&&source.name===b[i].name);
  }
  function sameAttempt(a,b) {
    return a.id===b.id&&a.draftId===b.draftId&&a.question===b.question&&a.prepareDigest===b.prepareDigest&&
      a.submittedAt===b.submittedAt&&samePendingSources(a.files,b.files);
  }
  async function captureAttempt(t,text,draft,hooks,kept) {
    const prepareKey=token();
    if(!journal){if(hooks?.canCapture||hooks?.onCaptured||hooks?.onNotAsked)throw pendingFailure('pending_storage_unavailable');return {prepareKey,receipt:null};}
    const bytes=new TextEncoder().encode(prepareKey);
    let hash;
    try {hash=await crypt.subtle.digest('SHA-256',bytes);guard(t);}
    catch(_){guard(t);throw pendingFailure('pending_storage_unavailable');}
    finally{bytes.fill(0);}
    const prepareDigest=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');
    if(!sha256.test(prepareDigest))throw pendingFailure('pending_storage_unavailable');
    const input={id:token(),conversationId:state.conversation?.id??null,draftId:draft?.id??null,
      question:text,files:pendingSources(draft,kept),prepareDigest,submittedAt:Date.now()/1000};
    if(hooks?.canCapture){
      let current=false;try{current=hooks.canCapture()===true;}catch(_){}
      if(!current)throw pendingFailure('pending_context_changed');
    }
    guard(t);
    const receipt=validateReceipt(await journalCall(t,()=>journal.capture(input,{fileLimit:Math.min(5,state.limits.fileCount)})));
    journalHold=true;syncUncertainty();
    if(!sameAttempt(receipt,input)||receipt.conversationId!==input.conversationId||receipt.phase!=='captured'||receipt.requestId!==null)throw pendingFailure('pending_storage_invalid');
    if(hooks?.onCaptured){
      let accepted;
      try{accepted=hooks.onCaptured(clone(receipt));}catch(_){throw pendingFailure('pending_presentation_failed');}
      if(accepted===false||accepted&&typeof accepted.then==='function')throw pendingFailure('pending_presentation_failed');
    }
    guard(t);
    return {prepareKey,receipt};
  }
  async function markPreparation(t,attempt,draft,kept) {
    const prior=attempt.receipt;
    if(!prior)return;
    if(prior.draftId!==(draft?.id??null)||!samePendingSources(prior.files,pendingSources(draft,kept)))throw invalid();
    const updated=validateReceipt(await journalCall(t,()=>journal.markPrepare(prior.id,state.conversation.id)));
    if(!sameAttempt(updated,prior)||updated.phase!=='prepare_may_have_started'||updated.requestId!==null||updated.conversationId!==state.conversation.id)throw pendingFailure('pending_storage_invalid');
    attempt.receipt=updated;
  }
  async function markExecution(t,attempt,requestId) {
    const prior=attempt.receipt;
    if(!prior)return;
    const prepared=validateReceipt(await journalCall(t,()=>journal.recordPrepared(prior.id,requestId)));
    if(!sameAttempt(prepared,prior)||prepared.conversationId!==prior.conversationId||prepared.phase!=='prepare_may_have_started'||prepared.requestId!==requestId)throw pendingFailure('pending_storage_invalid');
    const executing=validateReceipt(await journalCall(t,()=>journal.markExecute(prior.id,requestId)));
    if(!sameAttempt(executing,prior)||executing.conversationId!==prior.conversationId||executing.phase!=='execute_may_have_started'||executing.requestId!==requestId)throw pendingFailure('pending_storage_invalid');
    attempt.receipt=executing;
  }
  async function settleReceipt(t,receipt) {
    const result=await journalCall(t,()=>journal.settle(receipt.id,{phase:receipt.phase,requestId:receipt.requestId}));
    if(result!==null)throw pendingFailure('pending_storage_invalid');
    journalHold=false;
    syncUncertainty();
  }
  async function settleUnprepared(t,receipt,hooks,reason) {
    if(!receipt||receipt.phase!=='prepare_may_have_started'||receipt.requestId!==null||typeof hooks?.onNotAsked!=='function')return;
    let covered=false;
    try{covered=hooks.onNotAsked({id:receipt.id,reason})===true;}catch(_){}
    guard(t);
    if(!covered)return;
    await settleReceipt(t,receipt);
  }
  async function lookupPending(t,receipt) {
    if(!receipt)return null;
    const conversationId=state.conversation.id;
    if(receipt.conversationId!==null&&receipt.conversationId!==conversationId)throw invalid();
    const value=await request(t,'/conversations/'+conversationId+'/requests/by-prepare'+query({destinationId:'officer',
      scopeId:state.context.scopeId,prepareDigest:receipt.prepareDigest}));guard(t);
    const found=value.request;
    if(found===null)return null;
    if(!object(found)||Object.keys(found).length!==4||!identity(found.requestId)||!states.has(found.state)||
      (receipt.requestId!==null&&receipt.requestId!==found.requestId)||found.draftId!==receipt.draftId||
      found.kind!==(receipt.draftId===null?'question':'draft'))throw invalid();
    return found.requestId;
  }
  async function matchPending(t,receipt,requestId,history,files) {
    if(!requestId)return null;
    const row=history.find(r=>r.requestId===requestId);
    if(!row)return null;
    if(row.draftId!==receipt.draftId||row.question!==receipt.question||!bindingMatches(row.destination,false)||
      row.questionFileIds!==undefined&&(row.questionFileIds.length!==receipt.files.length||
        row.questionFileIds.some((id,index)=>id!==receipt.files[index].id)))throw invalid();
    if(receipt.requestId===null&&receipt.phase==='prepare_may_have_started') {
      const recorded=validateReceipt(await journalCall(t,()=>journal.recordPrepared(receipt.id,requestId)));
      if(!sameAttempt(recorded,receipt)||recorded.conversationId!==receipt.conversationId||recorded.phase!==receipt.phase||recorded.requestId!==requestId)throw pendingFailure('pending_storage_invalid');
      receipt=recorded;
    }
    const preparation=unresolved.get('prepare');
    if(preparation?.attemptId===receipt.id){
      unresolved.delete('prepare');
      if(['prepared','preparing','unknown'].includes(row.state))unresolved.set('request:'+requestId,{kind:'request',requestId,draftId:row.draftId});
    }
    const matched=receipt.requestId===requestId;
    const abandoned=row.abandoned&&row.state==='failed'&&!row.canAbandon&&row.officerDelivery.state==='not_sent';
    const admitted=receipt.draftId!==null&&row.officerDelivery.requestId===requestId&&
      ['retained_local','confirmed'].includes(row.officerDelivery.state)&&
      receipt.files.every(source=>files.some(f=>f.id===source.id&&f.name===source.name&&
        ((row.draftFileIds===undefined||row.draftFileIds.includes(source.id))?f.admissionRequestId===requestId:
          f.admissionRequestId!==requestId)));
    const refused=row.questionFileIds!==undefined&&row.state==='failed'&&row.officerDelivery.state==='not_sent';
    const terminal=!['prepared','preparing','unknown'].includes(row.state);
    if(matched)state.pendingMatch={id:receipt.id,requestId};
    return terminal&&(receipt.draftId===null||abandoned||admitted||refused)?receipt:null;
  }

  async function load(input,pendingReceipt=undefined) {
    const t={...input,publicationBound:true};
    state.complete=false;state.usableFileCount=null;state.pendingMatch=null;state.answers=[];state.preview=null;handles.clear();emit();guard(t);
    const pending=pendingReceipt===undefined?await readJournal(t):pendingReceipt;guard(t);
    const priorRequests=privateRequests,evidence=new Map();
    const current=await request(t,'/conversations/current'+query({scopeId:state.context.scopeId}));guard(t);
    if(current.conversation!==null&&(!object(current.conversation)||!identity(current.conversation.id)||current.conversation.scopeId!==state.context.scopeId||Object.keys(current.conversation).length!==2)) throw invalid();
    if(state.conversation && (!current.conversation || current.conversation.id!==state.conversation.id)) throw invalid();
    state.conversation=current.conversation&&clone(current.conversation);
    if(!state.conversation) {
      if(knownDocument||pendingDeletions.size||deletionMetas.size)throw invalid();state.files=[];state.requests=[];state.savedDrafts=[];unresolved.delete('create');syncUncertainty();state.routing=state.uploads.length||state.draft||state.uncertain?'blocked':'ordinary';state.complete=true;if(state.documentsEnabled&&state.context.modelId!==null)state.usableFileCount=0;return;
    }
    knownDocument=true;everKnownDocument=true;state.routing='documents';
    await readManagement(t);guard(t);await readDeletionStatuses(t);guard(t);
    if(!state.documentsEnabled||state.context.modelId===null){state.available=false;state.files=[];state.requests=[];state.answers=[];state.savedDrafts=[];state.complete=true;syncUncertainty();return;}
    const params={destinationId:'officer',scopeId:state.context.scopeId};
    let history,pendingRequestId;
    try {
      pendingRequestId=await lookupPending(t,pending);guard(t);
      history=await pages(t,'/conversations/'+state.conversation.id+'/requests',params,10000,r=>validateRequest(r,true));guard(t);
    }catch(e){if(e.code!=='unavailable'||e.status!==503)throw e;state.available=false;state.files=[];state.requests=[];state.answers=[];state.savedDrafts=[];state.complete=true;syncUncertainty();return;}
    for(const hold of unresolved.values())if(hold.requestId){const known=history.find(r=>r.requestId===hold.requestId)||priorRequests.get(hold.requestId);if(known&&known.requestCapability){const status=await request(t,'/requests/'+known.requestId,'GET',undefined,{'X-Attachment-Request-Capability':known.requestCapability});guard(t);validateRequest(status);if(status.requestId!==known.requestId)throw invalid();const refreshed={...known,...status};priorRequests.set(known.requestId,refreshed);evidence.set(known.requestId,refreshed);const index=history.findIndex(r=>r.requestId===known.requestId);if(index>=0)history[index]=refreshed;}}

    const files=await pages(t,'/files',{...params,conversationId:state.conversation.id},50000,f=>validateFile(f,true));guard(t);
    const originRequests=new Map(history.map(r=>[r.requestId,r]));for(const file of files)if(file.admissionRequestId!==null){const origin=originRequests.get(file.admissionRequestId);if(!origin||origin.draftId===null||origin.canAbandon||origin.abandoned||!['retained_local','confirmed','unknown'].includes(origin.officerDelivery.state)||origin.officerDelivery.requestId!==origin.requestId)throw invalid();}
    const admittedSourceIds=new Set(files.map(f=>f.id));for(const requestId of admissionAttempts.keys())retireAdmittedSelection(requestId,evidence.get(requestId)||history.find(r=>r.requestId===requestId),admittedSourceIds);
    const drafts=await pages(t,'/drafts',params,10000,d=>{if(!identity(d.id)||!object(d.destination)||d.destination.scopeId!==state.context.scopeId||d.destination.id!=='officer'||d.state!=='active'||!number(d.revision)||d.revision<1||!number(d.writerGeneration)||d.writerGeneration<1||!Number.isFinite(d.createdAt)||!Number.isFinite(d.updatedAt)||!number(d.fileCount)||!number(d.totalBytes)) throw invalid();return d.id;});guard(t);
    state.savedDrafts=drafts.filter(d=>bindingMatches(d.destination)).map(d=>({id:d.id,state:d.state,revision:d.revision,writerGeneration:d.writerGeneration,createdAt:d.createdAt,updatedAt:d.updatedAt,fileCount:d.fileCount,totalBytes:d.totalBytes,readOnly:true}));
    privateRequests=new Map(history.map(r=>[r.requestId,r]));for(const r of history)evidence.set(r.requestId,r);
    for(const hold of unresolved.values())if(hold.requestId&&!privateRequests.has(hold.requestId)&&priorRequests.has(hold.requestId))privateRequests.set(hold.requestId,priorRequests.get(hold.requestId));
    const answerIds=new Set();
    for(const r of history)if(r.answerId!==null){if(answerIds.has(r.answerId))throw invalid();answerIds.add(r.answerId);}
    history.sort((a,b)=>a.createdAt-b.createdAt||(a.requestId<b.requestId?-1:a.requestId>b.requestId?1:0));
    state.requests=history.map(requestPublic);
    state.files=files.map(f=>{const p=retainedFilePublic(f);if(!confirmationUnresolved()&&f.available===true&&f.previewable===true){if(!str(f.sourceCapability,8192)||!f.sourceCapability)throw invalid();p.previewHandle=addHandle({fileId:f.id,cap:f.sourceCapability});}return p;});
    for(const r of history) if(!confirmationUnresolved()&&r.answerId){const validated=await answer(t,r);guard(t);state.answers.push(validated);}
    unresolved.delete('create');unresolved.delete('mutation');
    for(const [key,hold] of unresolved){
      const recovered=hold.requestId&&evidence.get(hold.requestId);
      if(!recovered||['prepared','preparing','unknown'].includes(recovered.state))continue;
      unresolved.delete(key);
      if(recovered.abandoned&&hold.releasePreparation){const preparation=unresolved.get('prepare');if(preparation&&(preparation.draftId||null)===recovered.draftId)unresolved.delete('prepare');}
      if(state.draft&&recovered.draftId===state.draft.id){state.draft=null;writer=null;}
    }
    for(const r of history)if((r.abandoned||r.questionFileIds!==undefined&&r.state==='failed'&&r.officerDelivery.state==='not_sent')&&r.draftId&&!history.some(other=>other.requestId!==r.requestId&&other.draftId===r.draftId&&['prepared','preparing','unknown'].includes(other.state)))attemptedDrafts.delete(r.draftId);
    if(writer&&state.draft) {const d=await request(t,'/drafts/'+state.draft.id,'GET',undefined,draftHeader());guard(t);setDraft(d);}
    if(state.lastSend&&state.lastSend.requestId){const exact=history.find(r=>r.requestId===state.lastSend.requestId&&r.question===state.lastSend.text);if(exact&&exact.state!=='prepared'&&['retained_local','confirmed','unknown'].includes(exact.officerDelivery.state)&&exact.officerDelivery.requestId===exact.requestId)state.lastSend.retained=true;}
    const settle=await matchPending(t,pending,pendingRequestId,history,files);guard(t);
    state.available=Boolean(capability);syncUncertainty();if(state.draft)state.draft.readOnly=state.uncertain||!writer;state.complete=true;
    if(!confirmationUnresolved())state.usableFileCount=state.files.filter(f=>f.available&&f.preparationKind==='officer_direct'&&!revokedSources.has(f.id)).length;
    if(settle){
      emit();guard(t);
      await settleReceipt(t,settle);guard(t);
      if(state.draft)state.draft.readOnly=state.uncertain||!writer;
    }
  }
  // A one-file selection refused only for its size stays in the tray as a local, never-read failed row, so the page can
  // name the file and the published limit. Any other refusal, or a name the page cannot show, is still the whole-selection
  // metadata refusal below. The File itself is kept unread so an explicit Add it can admit it once it fits.
  function sizeRefusal(files,existing,l) {
    const f=files.length===1?files[0]:null;
    if(!f||existing.length+1>l.fileCount||!str(f.name,255)||!f.name||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(f.name)||state.formats.indexOf(f.type)<0||!number(f.size)||typeof f.arrayBuffer!=='function')return null;
    const held=existing.reduce((s,x)=>s+x.bytes,0);
    const error=f.size>l.fileBytes?{code:'file_too_large',message:'The selected file is larger than Motet’s file byte limit.'}
      :existing.length&&held+f.size>l.draftBytes?{code:'draft_too_large',message:'The selected file takes the draft past Motet’s draft byte limit.'}:null;
    return error&&{id:token(),name:f.name,mediaType:f.type,bytes:f.size,state:'failed',fileId:null,error,progress:null};
  }
  function preserveOrdinary(e) {
    return metadataRejections.has(e)&&state.routing==='ordinary'&&state.complete&&state.available&&Boolean(capability)&&!knownDocument&&!everKnownDocument&&!state.uncertain&&!state.conversation&&!state.draft&&!writer&&!state.lastSend&&!state.preview&&!suspended&&
      !state.uploads.length&&!state.files.length&&!state.requests.length&&!state.answers.length&&!state.savedDrafts.length&&
      !unresolved.size&&!attemptedDrafts.size&&!uploadDrafts.size&&!admissionAttempts.size&&!privateRequests.size&&!handles.size&&!pendingDeletions.size&&!deletionMetas.size&&!deletionOps.size&&!confirmKeys.size&&!revokedSources.size&&!managementFiles.size;
  }
  function report(e) { if(e.code==='context_changed'||e.code==='publication_changed')return;if(!state.complete)state.available=false; const trusted=e.officerDocumentClient===true&&typeof e.code==='string'&&/^[a-z_]{1,64}$/.test(e.code);state.error={code:trusted?e.code:'transport_unavailable',message:trusted?e.message:'The document operation could not be confirmed. Use status refresh.'}; if((!knownDocument||state.routing==='checking')&&!preserveOrdinary(e))state.routing='blocked'; }
  async function run(action,mutation=false) {
    if(dead)return clone(state);
    if(state.busy){return clone(state);}
    let t;try {t=capture();if(mutation&&(!state.available||!state.complete||state.uncertain))throw fail('mutation_blocked','Refresh status or reconnect before changing documents.');}catch(e){report(e);emit();return clone(state);}
    state.busy=true;state.error=null;emit();
    try {guard(t);if(mutation&&journal){await readJournal(t,true);guard(t);}await action(t);guard(t);}
    catch(e){if(t.epoch===epoch){if(journalHold)syncUncertainty();if(mutation&&state.uncertain&&!unresolved.size&&!journalHold)unresolved.set('mutation',{kind:'mutation'});if(state.draft&&state.uncertain)state.draft.readOnly=true;report(e);}}
    finally{if(t.epoch===epoch){state.busy=false;emit();}}
    return clone(state);
  }
  async function ensureDraft(t) {
    if(state.draft) {fence();return;}
    if(!state.conversation) {unresolved.set('create',{kind:'create'});const c=await request(t,'/conversations/current','POST',{scopeId:state.context.scopeId});guard(t);if(!c.conversation||!identity(c.conversation.id)||c.conversation.scopeId!==state.context.scopeId)throw invalid();state.conversation=clone(c.conversation);knownDocument=true;everKnownDocument=true;state.routing='documents';unresolved.delete('create');}
    const d=await request(t,'/drafts','POST',selection(),{'Idempotency-Key':token()});guard(t);setDraft(d,true);state.uncertain=false;
  }
  async function saveText(t,text) {
    if(!str(text,Math.min(4000,state.limits.officerTextCharacters || 4000)))throw fail('text_limit','This question exceeds Motet’s text limit.');
    await ensureDraft(t);guard(t);const d=await request(t,'/drafts/'+state.draft.id,'PATCH',{...fence(),text},draftHeader());guard(t);setDraft(d);if(state.draft.text!==text)throw invalid();state.uncertain=journalHold;state.draft.readOnly=journalHold;emit();guard(t);
  }
  async function admit(t,files,replacing=null,kept=[]) {
      if(!Array.isArray(files)||!files.length)return;
      const existing=[...kept,...(state.draft?.files||[])],l=state.limits;
      const refused=sizeRefusal(files,existing,l);if(refused){const held=replacing&&state.uploads.find(u=>u.id===replacing);if(held){held.error=refused.error;emit();throw fail(refused.error.code,refused.error.message);}refusedFiles.set(refused.id,files[0]);state.uploads.push(refused);if(!state.conversation)state.routing='checking';emit();throw fail(refused.error.code,refused.error.message);}
      if(files.length+existing.length>l.fileCount)throw rejectMetadata('file_limit','The selected files exceed the draft file count.');
      let total=existing.reduce((s,f)=>s+f.bytes,0);
      for(const f of files){if(!f||!str(f.name,255)||!f.name||!state.formats.includes(f.type)||!number(f.size)||f.size>l.fileBytes||typeof f.arrayBuffer!=='function')throw rejectMetadata('file_unavailable','The selected file is not a bounded qualified PDF or PNG.');total+=f.size;if(total>l.draftBytes)throw rejectMetadata('file_limit','The selected files exceed the draft byte limit.');}
      if(replacing){state.uploads=state.uploads.filter(u=>u.id!==replacing);refusedFiles.delete(replacing);}
      const selections=files.map(file=>({id:token(),name:file.name,mediaType:file.type,bytes:file.size,state:'reading',fileId:null,error:null,progress:null}));state.uploads.push(...selections);if(!state.conversation)state.routing='checking';emit();guard(t);
      for(let index=0;index<files.length;index++){const file=files[index],local=selections[index];
        let bytes;try {
          bytes=await file.arrayBuffer();guard(t);if(!(bytes instanceof ArrayBuffer)||bytes.byteLength!==file.size)throw fail('file_read_failed','The selected file could not be read within its bound.');
          const hash=await crypt.subtle.digest('SHA-256',bytes);guard(t);const digest=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');if(!/^[a-f0-9]{64}$/.test(digest))throw invalid();
          await ensureDraft(t);guard(t);uploadDrafts.set(local.id,state.draft.id);
          const u=await request(t,'/drafts/'+state.draft.id+'/upload-intents','POST',{...fence(),name:file.name,mediaType:file.type,bytes:file.size,digest},{...draftHeader(),'Idempotency-Key':token()});guard(t);
          if(!identity(u.id)||!identity(u.fileId)||!str(u.fingerprint,128)||!number(u.draftRevision)||u.draftRevision<=state.draft.revision)throw invalid();state.draft.revision=u.draftRevision;local.fileId=u.fileId;local.state='uploading';emit();guard(t);
          const f=fence();const done=await request(t,'/drafts/'+state.draft.id+'/uploads/'+u.id,'PUT',bytes,{...draftHeader(),'Content-Type':'application/octet-stream','X-Attachment-Writer-Nonce':f.writerNonce,'X-Attachment-Writer-Generation':String(f.writerGeneration),'X-Attachment-Draft-Revision':String(f.revision),'X-Attachment-Upload-Fingerprint':u.fingerprint});guard(t);
          if(done.id!==u.id||done.fileId!==u.fileId||done.state!=='ready'||!number(done.draftRevision))throw fail('upload_unavailable','The selected file was not confirmed ready.');
          const d=await request(t,'/drafts/'+state.draft.id,'GET',undefined,draftHeader());guard(t);setDraft(d);state.uncertain=false;local.state='ready';emit();guard(t);
        }catch(e){if(t.epoch===epoch&&e.code!=='context_changed'){e=fail(e.officerDocumentClient===true?e.code:'file_read_failed','The selected file “'+file.name+'” was not confirmed uploaded. Refresh status before continuing.');local.state='failed';local.error={code:e.code,message:e.message};for(const later of selections.slice(index+1)){later.state='failed';later.error={code:'selection_not_uploaded',message:'This selected file was not uploaded.'};}}throw e;}finally{bytes=null;}
      }
  }
  return {
    async connect(context) {
      if(dead)return clone(state);if(object(context)){if(context.documentsEnabled!==undefined&&typeof context.documentsEnabled!=='boolean'){state.error={code:'invalid_request',message:'Use a boolean reading mode.'};emit();return clone(state);}if(typeof context.documentsEnabled==='boolean')documentsEnabled=context.documentsEnabled;context={scopeId:context.scopeId,modelId:context.modelId??null};}if(owner&&(!context||!state.context||owner!==auth()||state.context.scopeId!==context.scopeId||state.context.modelId!==context.modelId))suspended=null;const sameScope=owner===auth()&&state.context&&context&&state.context.scopeId===context.scopeId;const sameIdentity=sameScope&&state.context.modelId===context.modelId;const preserveKnown=knownDocument&&owner===auth()&&state.context&&context&&state.context.scopeId===context.scopeId;const pending=sameIdentity?new Map(unresolved):new Map(),attempted=sameIdentity?new Set(attemptedDrafts):new Set(),verifiedIdentity=sameScope?ownerIdentity:null,cleanup=sameScope?new Map(pendingDeletions):new Map(),metas=sameScope?new Map(deletionMetas):new Map(),commands=sameScope?new Map(confirmKeys):new Map(),revoked=sameScope?new Set(revokedSources):new Set(),path=sameScope?[...managementPath]:[null],page=sameScope?managementIndex:0;clear();pendingDeletions=cleanup;deletionMetas=metas;confirmKeys=commands;revokedSources=revoked;managementPath=path;managementIndex=page;unresolved=pending;attemptedDrafts=attempted;ownerIdentity=verifiedIdentity;state.uncertain=unresolved.size>0;knownDocument=preserveKnown;owner=auth();
      if(!active||typeof transport!=='function'||!Controller||!crypt||!owner||!object(context)||!OPAQUE.test(context.scopeId)||context.modelId!==null&&(typeof context.modelId!=='string'||context.modelId==='auto'||!(context.modelId==='@automatic'||OPAQUE.test(context.modelId)))){emit();return clone(state);}
      state.context={scopeId:context.scopeId,modelId:context.modelId};state.routing='checking';controller=new Controller();emit();
      return run(async t=>{
        const authentication=new TextEncoder().encode(t.key);let digest;try{digest=await crypt.subtle.digest('SHA-256',authentication);guard(t);}finally{authentication.fill(0);}
        ownerIdentity=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');if(!/^[a-f0-9]{64}$/.test(ownerIdentity))throw invalid();
        const carry=suspended;suspended=null;if(carry&&carry.ownerIdentity===ownerIdentity&&carry.context.scopeId===state.context.scopeId){if(carry.context.modelId===state.context.modelId){for(const [key,hold] of carry.unresolved){const existing=unresolved.get(key);unresolved.set(key,existing?{...hold,...existing,releasePreparation:hold.releasePreparation||existing.releasePreparation}:hold);}for(const id of carry.attemptedDrafts)attemptedDrafts.add(id);}pendingDeletions=new Map(carry.pendingDeletions||[]);confirmKeys=new Map(carry.confirmKeys||[]);deletionMetas=new Map(carry.deletionMetas||[]);revokedSources=new Set(carry.revokedSources||[]);managementPath=carry.managementPath||[null];managementIndex=carry.managementIndex||0;state.uncertain=unresolved.size>0||cleanupUncertain();emit();guard(t);}
        const pending=await readJournal(t);guard(t);
        if(!state.documentsEnabled||state.context.modelId===null){state.reason='Motet reading is unavailable. Kept files can still be managed.';await load(t,pending);guard(t);return;}
        let c;try{c=await request(t,'/capabilities'+query({destinationId:'officer',scopeId:state.context.scopeId,modelId:attachmentModel()}));guard(t);}catch(e){if(e.code==='attachments_disabled'&&!everKnownDocument){state.routing=state.uncertain?'blocked':'ordinary';state.reason='Attachments are disabled.';state.complete=true;return;}if(!['unavailable','destination_unavailable','attachments_unavailable','reading_unavailable','policy_unavailable'].includes(e.code))throw e;state.reason='Motet reading is unavailable. Kept files can still be managed.';await load(t,pending);guard(t);return;}
        if(c.destinationId!=='officer'||c.scopeId!==state.context.scopeId||c.modelId!==attachmentModel()||typeof c.available!=='boolean')throw invalid();
        if(c.available){if(c.kind!=='officer'||c.analysisOnly!==true||!['auto','claude-haiku-5-5'].includes(c.modelId)||c.effectiveModelId!=='claude-haiku-5-5'||!str(c.capabilityRevision,160)||!Array.isArray(c.formats)||c.formats.length!==2||!c.formats.includes('application/pdf')||!c.formats.includes('image/png')||!object(c.limits)||!['fileBytes','draftBytes','fileCount'].every(k=>number(c.limits[k])&&c.limits[k]>0)||c.limits.fileBytes>300000||c.limits.draftBytes>300000||c.limits.fileCount>50)throw invalid();capability=c;state.available=true;state.formats=[...c.formats];state.limits={fileBytes:c.limits.fileBytes,draftBytes:c.limits.draftBytes,fileCount:c.limits.fileCount,officerTextCharacters:Math.min(c.limits.officerTextCharacters||4000,4000),documentEncodedBytes:c.limits.documentEncodedBytes||null};state.reason=null;}
        else {state.reason='Motet file reading requires the qualified model and host policy.';}
        await load(t,pending);guard(t);
      });
    },
    addFiles(files,kept=[]) {return run(t=>admit(t,files,null,kept),true);},
    // Explicit Add it for a refused local row: the same admission as choosing that File again. Never automatic.
    addRefused(uploadId,kept=[]) {const local=state.uploads.find(u=>u.id===uploadId);if(dead||!local||local.fileId||local.state!=='failed'||!refusedFiles.has(uploadId))return Promise.resolve(clone(state));return run(t=>admit(t,[refusedFiles.get(uploadId)],uploadId,kept),true);},
    remove(fileId){return run(async t=>{if(!identity(fileId)||!state.draft||!state.draft.files.some(f=>f.id===fileId))throw fail('file_unavailable','Choose a current draft file.');const d=await request(t,'/drafts/'+state.draft.id+'/files/'+fileId,'DELETE',fence(),draftHeader());guard(t);setDraft(d);state.uploads=state.uploads.filter(local=>{if(local.fileId!==fileId)return true;uploadDrafts.delete(local.id);return false;});state.uncertain=false;},true);},
    removeUpload(uploadId){if(dead||!syncOwner())return Promise.resolve(clone(state));if(state.busy)return Promise.resolve(clone(state));const local=state.uploads.find(u=>u.id===uploadId);if(!local)return Promise.resolve(clone(state));if(!local.fileId){if(local.state!=='failed')return Promise.resolve(clone(state));state.uploads=state.uploads.filter(u=>u.id!==uploadId);uploadDrafts.delete(uploadId);refusedFiles.delete(uploadId);if(!state.conversation&&!state.draft&&!state.uploads.length&&!state.uncertain&&!unresolved.size&&state.complete)state.routing='ordinary';emit();return Promise.resolve(clone(state));}return run(async t=>{if(!state.draft||!state.draft.files.some(f=>f.id===local.fileId))throw fail('file_unavailable','Refresh the exact draft before removing this selected file.');const d=await request(t,'/drafts/'+state.draft.id+'/files/'+local.fileId,'DELETE',fence(),draftHeader());guard(t);setDraft(d);state.uploads=state.uploads.filter(u=>u.id!==uploadId);uploadDrafts.delete(uploadId);state.uncertain=false;},true);},
    editText(text){return run(t=>saveText(t,text),true);},
    resume(draftId){return run(async t=>{const saved=state.savedDrafts.find(d=>d.id===draftId);if(!saved||!identity(draftId)||attemptedDrafts.has(draftId))throw fail('resume_unavailable','Choose an exact saved draft in this context.');const r=await request(t,'/drafts/'+draftId+'/recover','POST',{writerGeneration:saved.writerGeneration});guard(t);if(r.id!==draftId||!str(r.recoveryCapability,8192)||!number(r.revision)||r.writerGeneration!==saved.writerGeneration)throw invalid();const d=await request(t,'/drafts/'+draftId+'/resume','POST',{revision:r.revision,writerGeneration:r.writerGeneration},{'X-Attachment-Draft-Capability':r.recoveryCapability});guard(t);if(d.id!==draftId)throw invalid();setDraft(d,true);state.uncertain=false;},true);},
    send(text,hooks){return run(async t=>{
      if(!str(text,Math.min(4000,state.limits.officerTextCharacters || 4000)))throw fail('text_limit','This question exceeds Motet’s text limit.');
      state.lastSend={text,requestId:null,retained:false};emit();guard(t);requireCompleteSelection();
      const kept=clone(hooks?.keptFiles||[]);
      const attached=state.draft?.files||[], all=[...kept,...attached];
      const fileIds=all.map(f=>f.id);
      if(!all.length)throw fail('files_required','No files in this question yet. Attach one, or choose a kept file in Files.');
      if(fileIds.some(id=>!identity(id))||new Set(fileIds).size!==fileIds.length||
        all.length>Math.min(5,state.limits.fileCount)||all.some(f=>!number(f.bytes))||
        all.reduce((total,f)=>total+f.bytes,0)>state.limits.draftBytes)throw fail('file_limit','Choose files within the question limit.');
      if(kept.length&&!text.trim())throw fail('question_required','Enter a question when using kept files.');
      const keptFileIds=kept.map(f=>f.id);
      const checkTray=r=>{if(r.questionFileIds===undefined||r.questionFileIds.length!==fileIds.length||
        r.questionFileIds.some((id,index)=>id!==fileIds[index])||r.draftFileIds.length!==attached.length||
        r.draftFileIds.some((id,index)=>id!==attached[index].id))throw invalid();};
      if(state.draft&&state.draft.files.length){
        if(attemptedDrafts.has(state.draft.id))throw fail('already_attempted','This draft already has a Send attempt. Refresh its status.');
        if(state.draft.files.some(f=>f.state!=='ready'))throw fail('file_unavailable','Every draft file must be ready before Send.');
        fence();
        const attempt=await captureAttempt(t,text,state.draft,hooks,kept);guard(t);
        await saveText(t,text);guard(t);state.lastSend.retained=true;emit();guard(t);const original=state.draft.id, f=fence(),membership=new Map(state.uploads.filter(local=>uploadDrafts.get(local.id)===state.draft.id&&state.draft.files.some(source=>source.id===local.fileId)).map(local=>[local.id,local.fileId]));
        await markPreparation(t,attempt,state.draft,kept);guard(t);
        const preparation={kind:'prepare',draftId:original,attemptId:attempt.receipt?.id};
        attemptedDrafts.add(original);unresolved.set('prepare',preparation);
        let p;
        try{
          p=await request(t,'/drafts/'+original+'/send-intent','POST',{...f,keptFileIds},
            {...draftHeader(),'Idempotency-Key':attempt.prepareKey});
        }catch(e){
          guard(t);
          if(trayRefusals.has(e)&&unresolved.get('prepare')===preparation){
            unresolved.delete('prepare');attemptedDrafts.delete(original);syncUncertainty();
            await settleUnprepared(t,attempt.receipt,hooks,'tray_invalid');guard(t);
            if(attempt.receipt&&!journalHold){state.draft=null;writer=null;await load(t);guard(t);}
          }
          throw e;
        }
        guard(t);validateRequest(p);checkTray(p);if(p.draftId!==original||attempt.receipt&&p.state!=='prepared')throw invalid();if(!str(p.requestCapability,8192)||!str(p.fingerprint,128))throw invalid();
        state.lastSend.requestId=p.requestId;privateRequests.set(p.requestId,p);admissionAttempts.set(p.requestId,{draftId:original,membership});attemptedDrafts.add(original);unresolved.delete('prepare');unresolved.set('request:'+p.requestId,{kind:'request',requestId:p.requestId,draftId:original});state.uncertain=true;emit();guard(t);
        await markExecution(t,attempt,p.requestId);guard(t);
        const result=await request(t,'/requests/'+p.requestId+'/send','POST',{...f,fingerprint:p.fingerprint},{...draftHeader(),'X-Attachment-Request-Capability':p.requestCapability,'Idempotency-Key':token()});guard(t);validateRequest(result);checkTray(result);if(result.requestId!==p.requestId)throw invalid();retireAdmittedSelection(p.requestId,result);state.draft=null;writer=null;
        if(!['unknown','preparing','prepared'].includes(result.state))unresolved.delete('request:'+p.requestId);
        await load(t);guard(t);return;
      }
      if(!text.trim())return;
      if(!state.conversation)throw fail('files_required','This Motet conversation is unavailable.');
      const draftPending=()=>fail('draft_pending','Finish or resume the pending draft before asking another question.');
      if(state.draft&&(!writer||state.draft.readOnly||state.draft.state!=='active'||attemptedDrafts.has(state.draft.id)))throw draftPending();
      // The draft branch above owns any staged files. A retained-only question
      // can leave a verified empty writer untouched, including its saved text.
      const untouchedDraft=JSON.stringify(state.draft),untouchedWriter=writer;
      await load(t);guard(t);if(!state.available||!state.complete||state.uncertain)throw fail('mutation_blocked','Refresh status or reconnect before changing documents.');
      if(writer!==untouchedWriter||JSON.stringify(state.draft)!==untouchedDraft){
        writer=null;if(state.draft)state.draft.readOnly=true;
        throw draftPending();
      }
      requireCompleteSelection();
      const prior=state.answers.filter(a=>a.state==='available'&&
        a.suppliedFileIds.every(id=>keptFileIds.includes(id))).slice(-priorAnswerLimit).map(a=>a.id);
      const attempt=await captureAttempt(t,text,null,hooks,kept);guard(t);
      await markPreparation(t,attempt,null,kept);guard(t);
      const preparation={kind:'prepare',draftId:null,attemptId:attempt.receipt?.id};let p;
      unresolved.set('prepare',preparation);
      try {
        p=await request(t,'/conversations/'+state.conversation.id+'/question-intent','POST',
          {...selection(),question:text,priorAnswerIds:prior,priorAnswerPolicy,keptFileIds},{'Idempotency-Key':attempt.prepareKey});
      } catch(e) {
        guard(t);
        if(priorWindowRefusals.has(e)&&unresolved.get('prepare')===preparation){
          unresolved.delete('prepare');syncUncertainty();withdrawPublication();guard(t);
          await settleUnprepared(t,attempt.receipt,hooks,'prior_window_changed');guard(t);
        }
        throw e;
      }
      guard(t);validateRequest(p);checkTray(p);if(p.draftId!==null||attempt.receipt&&p.state!=='prepared')throw invalid();if(!str(p.requestCapability,8192)||!str(p.fingerprint,128))throw invalid();
      state.lastSend.requestId=p.requestId;privateRequests.set(p.requestId,p);unresolved.delete('prepare');unresolved.set('request:'+p.requestId,{kind:'request',requestId:p.requestId,draftId:null});emit();guard(t);
      await markExecution(t,attempt,p.requestId);guard(t);
      const r=await request(t,'/requests/'+p.requestId+'/ask','POST',{fingerprint:p.fingerprint},{'X-Attachment-Request-Capability':p.requestCapability,'Idempotency-Key':token()});guard(t);validateRequest(r);checkTray(r);if(r.requestId!==p.requestId)throw invalid();if(!['unknown','preparing','prepared'].includes(r.state))unresolved.delete('request:'+p.requestId);
      await load(t);guard(t);
    },true);},
    abandon(requestId){return run(async t=>{const exact=privateRequests.get(requestId);if(!identity(requestId)||!exact||exact.canAbandon!==true||exact.abandoned||!str(exact.requestCapability,8192)||!str(exact.fingerprint,128))throw fail('abandon_unavailable','Choose an exact prepared request from the current conversation.');const held=unresolved.get('request:'+requestId),preparation=unresolved.get('prepare');unresolved.set('request:'+requestId,{...held,kind:'request',requestId,draftId:exact.draftId,releasePreparation:Boolean(held&&held.releasePreparation||preparation&&(preparation.draftId||null)===exact.draftId)});const result=await request(t,'/requests/'+requestId+'/abandon','POST',{fingerprint:exact.fingerprint},{'X-Attachment-Request-Capability':exact.requestCapability,'Idempotency-Key':token()});guard(t);validateRequest(result);if(result.requestId!==requestId||result.draftId!==exact.draftId||result.abandoned!==true)throw invalid();privateRequests.set(requestId,{...exact,...result});await load(t);guard(t);});},
    managementPage(cursorHandle){return run(async t=>{const target=cursorHandles.get(cursorHandle);if(!target)throw fail('management_unavailable','Choose a current Files page control.');closeDeletionView();managementFiles.clear();state.management.complete=false;state.management.files=[];emit();guard(t);await readManagement(t,target.cursor,target.index,target.path);guard(t);await readDeletionStatuses(t);guard(t);});},
    prepareDeletion(fileId){return run(async t=>{requireManagement();const file=managementFiles.get(fileId);if(!identity(fileId)||!file||!file.available||revokedSources.has(fileId))throw fail('deletion_unavailable','Choose a current available kept file.');if([...pendingDeletions.values()].some(h=>h.kind==='confirm'||h.fileId===fileId))throw fail('deletion_unconfirmed','Read the existing deletion status before preparing another selection.');closeDeletionView();const dialog=dialogVersion;pendingDeletions.set('prepare:'+fileId,{kind:'prepare',fileId,generation:file.managementGeneration});updateManagementSnapshot();syncUncertainty();emit();guard(t);const v=await request(t,'/files/'+fileId+'/deletion-intent','POST',{destinationId:'officer',scopeId:state.context.scopeId,conversationId:state.conversation.id,managementGeneration:file.managementGeneration},{'Idempotency-Key':token()},'cleanup');guard(t);validateDeletion(v);if(v.fileId!==fileId||v.name!==file.name||v.bytes!==file.bytes||v.state!=='confirm'||v.revision!==file.managementGeneration+1)throw invalid();pendingDeletions.delete('prepare:'+fileId);const op=recordDeletion(v);if(dialogVersion===dialog)showDeletion(op);syncUncertainty();emit();guard(t);});},
    confirmDeletion(confirmationHandle){return run(async t=>{requireManagement();const exact=confirmationHandles.get(confirmationHandle),dialog=activeDialog;if(!exact||exact.used||!dialog||dialog.confirmationHandle!==confirmationHandle||dialog.version!==exact.version)throw fail('deletion_unavailable','Open the exact current deletion confirmation.');const current=deletionOps.get(exact.id),hold=pendingDeletions.get('confirm:'+exact.id);if(!current||!current.canConfirm||current.fingerprint!==exact.fingerprint||current.fileId!==exact.fileId||hold&&(hold.needsRead||hold.revision!==exact.revision)||[...pendingDeletions.values()].some(h=>h.kind==='confirm'&&h.id!==exact.id))throw fail('deletion_unconfirmed','Read and reopen the exact existing deletion before confirming.');exact.used=true;const command=operationIdentity(exact);let key=confirmKeys.get(command);if(!key){key=token();confirmKeys.set(command,key);}pendingDeletions.set('confirm:'+exact.id,{kind:'confirm',id:exact.id,fileId:exact.fileId,revision:exact.revision,needsRead:true});refreshDialog(current);updateManagementSnapshot();syncUncertainty();withdrawPublication();guard(t);t.publication=publication;const v=await request(t,'/deletions/'+exact.id+'/confirm','POST',{fingerprint:exact.fingerprint},{'X-Attachment-Deletion-Capability':exact.capability,'Idempotency-Key':key},'cleanup');guard(t);validateDeletion(v,exact);recordDeletion(v);await load(t);guard(t);});},
    openDeletion(operationHandle){return run(async t=>{const exact=operationHandles.get(operationHandle);if(!exact)throw fail('deletion_unavailable','Choose a current deletion status.');closeDeletionView();const dialog=dialogVersion;emit();guard(t);const v=await request(t,'/deletions/'+exact.id,'GET',undefined,{'X-Attachment-Deletion-Capability':exact.capability});guard(t);validateDeletion(v,exact);const current=recordDeletion(v,true);if(dialogVersion===dialog)showDeletion(current);syncUncertainty();emit();guard(t);});},
    async closeDeletion(){if(dead||!syncOwner())return clone(state);closeDeletionView();emit();return clone(state);},
    refresh(){return run(async t=>{await load(t);guard(t);});},
    preview(handle){return run(async t=>{t.publicationBound=true;if(confirmationUnresolved())throw fail('deletion_unconfirmed','Wait for exact deletion reconciliation before opening governed sources.');const source=handles.get(handle);state.preview=null;if(!source)throw fail('preview_unavailable','Refresh and select a current source.');const location=source.location;const q=location?query({location:location.page?'pdf_page:'+location.page:'whole_image'}):'';const p=await request(t,'/files/'+source.fileId+'/preview'+q,'GET',undefined,{'X-Attachment-Source-Capability':source.cap});guard(t);if(p.fileId!==source.fileId||p.kind!=='fallback'||p.available!==false||!object(p.payload)||!object(p.payload.noticeParts)||!str(p.payload.noticeParts.title,200)||!str(p.payload.noticeParts.body,2000)||!str(p.payload.footer,200)||!['retained_local','supplied','unknown','cited_page','cited_whole'].includes(p.officerState))throw invalid();state.preview={kind:'fallback',available:false,fileId:p.fileId,name:p.name,mediaType:p.mediaType,bytes:p.bytes,officerState:p.officerState,officerPriorDelivery:p.officerPriorDelivery,payload:{noticeParts:{title:p.payload.noticeParts.title,body:p.payload.noticeParts.body},footer:p.payload.footer}};});},
    async closePreview(){if(dead||!syncOwner())return clone(state);state.preview=null;emit();return clone(state);},
    snapshot(){if(!dead&&owner)syncOwner();return clone(state);},
    invalidate,
    dispose(){if(dead)return;clear();suspended=null;owner='';knownDocument=false;dead=true;refusedFiles.clear();emit();}
  };
}
if (typeof module !== 'undefined' && module.exports) module.exports = {createOfficerDocumentsClient};

if (typeof globalThis !== 'undefined') globalThis.createOfficerDocumentsClient = createOfficerDocumentsClient;
})();
