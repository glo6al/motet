#!/usr/bin/env node
/* Actual browser client against the disposable owner HTTP/storage/fake-provider stack. */
'use strict';
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {webcrypto,createHash} = require('node:crypto');
const path = require('node:path');
const {createOfficerDocumentsClient} = require('../src/web/officer-documents-client.js');
const {createOfficerDocumentPending} = require('../src/web/officer-document-pending.js');
const {PYTHON,stopFixture,WINDOWS} = require('./officer_document_fixture_process.js');
const key = 'fictional-officer-browser-fixture-key';
// On Windows the fixture is stopped through its standard input (see officer_document_fixture_process.js).
const fixtureArgs = extra => [path.join(__dirname, 'officer_document_live_fixture.py'), ...extra, ...(WINDOWS ? ['--stop-on-stdin'] : [])];
const fixtureOptions = {env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:[WINDOWS?'pipe':'ignore','pipe','pipe']};
const child = spawn(PYTHON, fixtureArgs([]), fixtureOptions);
let logs = '', errors = '', secondary = null;
child.stderr.on('data', b => { errors = (errors + b).slice(-10000); });
const ready = new Promise((resolve,reject) => {
  const timer = setTimeout(() => reject(Error('Offline fixture startup timed out')),10000);
  child.stdout.on('data', b => { logs += b; const match = logs.match(/http:\/\/127\.0\.0\.1:\d+\//); if(match){clearTimeout(timer);resolve(match[0].slice(0,-1));} });
  child.once('exit', code => {clearTimeout(timer);reject(Error('Offline fixture exited: '+code+' '+errors));});
});
(async () => {
  let client;
  try {
    const origin = await ready;
    const requests = [];
    const changes=[];let digestCalls=0;
    const crypto={randomUUID:()=>webcrypto.randomUUID(),subtle:{digest:(...args)=>{digestCalls++;return webcrypto.subtle.digest(...args);}}};
    const metadata = async()=> (await fetch(origin+'/fixture/metadata',{headers:{'X-Live-Room-Key':key}})).json();
    let dropPreparation = false, lostPrepared = null, observeDeletionConfirm = false,dropConfirmation=false,lostConfirmKey=null;
    let recordedPrepareDigest=null,journalGeneration=0;
    const browserStorage=new Map(),storage={getItem:k=>browserStorage.get(k)??null,setItem:(k,v)=>browserStorage.set(k,v),removeItem:k=>browserStorage.delete(k)};
    const createJournal=()=>createOfficerDocumentPending({current:()=>({key,scopeId:'room',generation:journalGeneration}),storage,
      locks:{request:async(name,options,fn)=>fn({name})},crypto:webcrypto});
    const transport = async (url, init) => {
      const selectedBody=init.method==='POST'&&(url==='/api/attachments/drafts'||url.endsWith('/question-intent'))?JSON.parse(init.body):null;
      requests.push({url,method:init.method,modelId:selectedBody?.modelId,priorIds:selectedBody?.priorAnswerIds,priorPolicy:selectedBody?.priorAnswerPolicy});
      if(observeDeletionConfirm&&url.endsWith('/confirm')){assert.deepEqual(client.snapshot().answers,[]);assert.equal(client.snapshot().preview,null);}
      if(url.endsWith('/confirm')&&dropConfirmation){dropConfirmation=false;lostConfirmKey=init.headers['Idempotency-Key'];throw Error('Fictional pre-dispatch confirmation loss');}
      if(url.endsWith('/confirm')&&lostConfirmKey){assert.equal(init.headers['Idempotency-Key'],lostConfirmKey);lostConfirmKey=null;}
      const response = await fetch(origin+url,init);
      if(dropPreparation && url.endsWith('/question-intent')) {
        dropPreparation=false;lostPrepared=await response.clone().json();recordedPrepareDigest=createHash('sha256').update(init.headers['Idempotency-Key']).digest('hex');
        throw Error('Fictional lost preparation response');
      }
      return response;
    };
    client = createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto,AbortController,fetch:transport,changed:s=>changes.push(s)});
    let s = await client.connect({scopeId:'room',modelId:'claude-haiku-5-5'});
    assert.equal(s.routing,'ordinary',JSON.stringify(s.error));
    assert.equal(s.usableFileCount,0);
    assert.equal(s.available,true);
    assert(requests.every(r=>r.method==='GET'));
    const preflightStart=changes.length,requestCount=requests.length,digests=digestCalls,metadataBefore=await metadata();let reads=0;
    s=await client.addFiles([{name:'fictional-oversized.pdf',type:'application/pdf',size:s.limits.fileBytes+1,arrayBuffer:async()=>{reads++;return new ArrayBuffer(4);}}]);
    // A one-file size refusal is held as a local, unread failed row that names its reason; nothing is read or sent.
    assert.equal(s.error.code,'file_too_large');assert.equal(s.routing,'blocked');assert.equal(s.complete,true);assert.equal(s.available,true);assert.equal(s.reason,null);
    assert.equal(reads,0);assert.equal(digestCalls,digests);assert.equal(requests.length,requestCount);
    assert.equal(s.uploads.length,1);assert.equal(s.uploads[0].state,'failed');assert.equal(s.uploads[0].fileId,null);assert.equal(s.uploads[0].error.code,'file_too_large');
    assert(changes.slice(preflightStart).every(v=>!v.draft&&!v.conversation&&!v.uncertain&&!(v.routing==='ordinary'&&v.uploads.length)));
    s=await client.removeUpload(s.uploads[0].id);assert.equal(s.routing,'ordinary');assert.deepEqual(s.uploads,[]);assert.equal(requests.length,requestCount);
    const metadataAfter=await metadata();for(const field of ['fakeProviderCalls','ordinaryAskBlocked','outboundBlocked'])assert.equal(metadataAfter[field],metadataBefore[field],field);
    const file = new File(['%PDF-1.4\nFictional offline content.\n%%EOF'], 'fictional.pdf', {type:'application/pdf'});
    const image = new File([Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6zs8AAAAASUVORK5CYII=','base64')],'fictional.png',{type:'image/png'});
    s = await client.addFiles([file,image]);
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.draft.files[0].state,'ready');
    assert.equal(s.uploads.length,2);
    const originalFileId=s.draft.files[0].id,imageFileId=s.draft.files[1].id;
    s = await client.send('');
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.requests[0].state,'retained_local');
    assert.equal(s.requests[0].notAskedReason,'no_question');
    assert.equal(s.draft,null);assert.deepEqual(s.uploads,[]);assert.ok(s.files.some(f=>f.id===originalFileId));
    assert.equal(s.usableFileCount,2);
    const admissionRequestId=s.requests[0].requestId;assert(s.files.every(f=>f.admissionRequestId===admissionRequestId));
    assert.equal((await metadata()).fakeProviderCalls,0);
    s = await client.send('What does the fictional file say?',{keptFiles:s.files.filter(f=>f.available)});
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.answers[0].state,'available');
    assert.deepEqual(s.answers[0].priorAnswerWindow,{policy:'recent_available_8',includedCount:0,omittedOlder:false});
    assert(Number.isFinite(s.answers[0].answeredAt));
    assert.match(s.answers[0].text,/Fictional offline/);
    assert(!s.answers[0].text.includes('IMAGE_SOURCE'));assert.deepEqual(new Set(s.answers[0].suppliedFileIds),new Set([originalFileId,imageFileId]));
    assert(s.answers[0].citations.some(c=>c.fileId===imageFileId&&c.location.image===true));assert(s.answers[0].citations.some(c=>c.fileId===originalFileId&&c.location.page===2));
    assert(s.files.every(f=>f.admissionRequestId===admissionRequestId));assert(s.files.every(f=>f.officerDelivery.requestId!==admissionRequestId));
    assert.deepEqual(s.uploads,[]);
    assert.equal((await metadata()).fakeProviderCalls,1);
    const oldHandle = s.answers[0].citations[0].previewHandle;
    s = await client.preview(oldHandle);
    assert.equal(s.preview.kind,'fallback');
    assert.equal(s.preview.officerState,'cited_page');
    const before = requests.length;
    s = await client.refresh();
    assert(requests.slice(before).every(r=>r.method==='GET'));
    assert.equal((await metadata()).fakeProviderCalls,1);
    s = await client.preview(oldHandle);
    assert(s.error);
    s=await client.addFiles([new File(['%PDF-1.4\nFictional staged source.\n%%EOF'],'fictional-staged.pdf',{type:'application/pdf'})]);
    assert.equal(s.error,null);const removedStaged=s.draft.files[0].id;
    s=await client.editText('Separate fictional private draft text.');assert.equal(s.error,null);
    s=await client.remove(removedStaged);assert.equal(s.error,null);assert.deepEqual(s.draft.files,[]);
    const untouchedDraft=s.draft,retainedFollowupStart=requests.length;
    s = await client.send('Summarize your earlier answer.',{keptFiles:s.files.filter(f=>f.available)});
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.deepEqual(s.draft,untouchedDraft);assert.equal(s.draft.text,'Separate fictional private draft text.');
    assert.deepEqual(requests.slice(retainedFollowupStart).filter(r=>r.method!=='GET').map(r=>r.url),
      ['/api/attachments/conversations/'+s.conversation.id+'/question-intent','/api/attachments/requests/'+s.lastSend.requestId+'/ask']);
    assert.deepEqual(new Set(s.answers.find(a=>a.requestId===s.lastSend.requestId).suppliedFileIds),new Set([originalFileId,imageFileId]));
    assert.equal(s.answers.length,2);assert(s.answers.every(a=>!a.text.includes('IMAGE_SOURCE')&&a.suppliedFileIds.every(id=>s.files.some(f=>f.id===id&&f.available))));
    assert.equal((await metadata()).fakeProviderCalls,2);
    let journal=createJournal();client.dispose();
    client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport,journal});
    s=await client.connect({scopeId:'room',modelId:'claude-haiku-5-5'});assert.equal(s.error,null);
    dropPreparation=true;
    let captured;
    s = await client.send('Fictional interrupted preparation.',{keptFiles:s.files.filter(f=>f.available),canCapture:()=>journalGeneration===0,onCaptured:receipt=>{captured=receipt;journalGeneration++;return true;}});
    assert(s.uncertain);
    assert.equal(captured.prepareDigest,recordedPrepareDigest);assert.equal(journal.snapshot().receipt.requestId,null);
    client.dispose();journal.dispose();journal=createJournal();
    client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport,journal});
    const pendingReads=requests.length;s=await client.connect({scopeId:'room',modelId:'claude-haiku-5-5'});
    assert(s.uncertain);
    assert(requests.slice(pendingReads).every(r=>r.method==='GET'));
    assert(requests.slice(pendingReads).some(r=>r.url.includes('/requests/by-prepare?')&&r.url.includes(recordedPrepareDigest)));
    assert.deepEqual(s.pendingMatch,{id:captured.id,requestId:lostPrepared.requestId});
    assert.equal(s.requests.find(r=>r.requestId===lostPrepared.requestId).canAbandon,true);
    s = await client.abandon(lostPrepared.requestId);
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.uncertain,false);
    assert.equal(journal.snapshot().receipt,null);assert.equal(browserStorage.size,0);
    assert(s.requests.find(r=>r.requestId===lostPrepared.requestId).abandoned);
    const late = await fetch(origin+'/api/attachments/requests/'+lostPrepared.requestId+'/ask',{
      method:'POST',headers:{'X-Live-Room-Key':key,'X-Attachment-Request-Capability':lostPrepared.requestCapability,
        'Idempotency-Key':'fictional-delayed-execution','Content-Type':'application/json'},
      body:JSON.stringify({fingerprint:lostPrepared.fingerprint})});
    assert.equal(late.status,200);
    assert.equal((await late.json()).abandoned,true);
    assert.equal((await metadata()).fakeProviderCalls,2);
    const cid = s.conversation.id;
    client.dispose();journal.dispose();
    client = createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport});
    const rejectedAlias = await fetch(origin+'/officer/model',{method:'POST',headers:{'X-Live-Room-Key':key,'Content-Type':'application/json'},body:JSON.stringify({model:'auto'})});
    assert.equal(rejectedAlias.status,400,'fixture picker must reject the attachment-only alias');
    const savedAuto = await fetch(origin+'/officer/model',{method:'POST',headers:{'X-Live-Room-Key':key,'Content-Type':'application/json'},body:JSON.stringify({model:'@automatic'})});
    assert.equal(savedAuto.status,200);
    assert.equal((await savedAuto.json()).selected_model,'@automatic');
    const savedSelection = await fetch(origin+'/officer/model-selection',{headers:{'X-Live-Room-Key':key}});
    assert.equal((await savedSelection.json()).selected_model,'@automatic');
    const autoConnect = requests.length;
    s = await client.connect({scopeId:'room',modelId:'@automatic'});
    assert.equal(s.routing,'documents');
    assert.equal(s.available,true,JSON.stringify(s.error));
    assert.equal(s.context.modelId,'@automatic');
    assert.equal(s.conversation.id,cid);
    assert.equal(s.answers.length,2);
    assert(s.answers.every(a=>a.model.id==='claude-haiku-5-5'));
    assert(requests.slice(autoConnect).every(r=>r.method==='GET'));
    assert(requests.slice(autoConnect).some(r=>r.url.startsWith('/api/attachments/capabilities?')&&new URL('http://fictional.invalid'+r.url).searchParams.get('modelId')==='auto'));
    assert(requests.slice(autoConnect).every(r=>!r.url.includes('%40automatic')),'attachment queries must use the narrow wire alias');
    assert.equal((await metadata()).fakeProviderCalls,2);
    s = await client.addFiles([new File(['%PDF-1.4\nFictional Auto content.\n%%EOF'],'fictional-auto.pdf',{type:'application/pdf'})]);
    assert.equal(s.error,null,JSON.stringify(s.error));
    const laterFileId=s.uploads[0].fileId;
    assert.equal(requests.filter(r=>r.url==='/api/attachments/drafts'&&r.method==='POST').at(-1).modelId,'auto');
    s = await client.send('Read this fictional file using the saved Auto selection.');
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.context.modelId,'@automatic');
    assert.equal(s.answers.length,3);
    assert.equal(s.draft,null);assert.deepEqual(s.uploads,[]);
    assert(s.answers.every(a=>a.model.id==='claude-haiku-5-5'));
    const answerFacts=a=>({...a,citations:a.citations.map(({previewHandle,...c})=>c)});
    const historical=s.answers.map(a=>({id:a.id,requestId:a.requestId})),laterAnswerId=s.answers.find(a=>a.requestId===s.lastSend.requestId).id;
    const earlierAnswers=s.answers.filter(a=>a.id!==laterAnswerId).map(answerFacts);
    const exactHistory=view=>{
      assert.equal(view.answers.length,historical.length,'every committed answer remains present');
      assert.deepEqual(new Set(view.answers.map(a=>a.id)),new Set(historical.map(a=>a.id)));
      for(const a of historical){const r=view.requests.find(r=>r.requestId===a.requestId);assert(r);assert.equal(r.state,'answered');assert.equal(r.answerId,a.id);assert(view.answers.some(v=>v.id===a.id&&v.requestId===a.requestId));}
    };
    assert.equal((await metadata()).fakeProviderCalls,3);
    client.dispose();
    client = createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport});
    s = await client.connect({scopeId:'room',modelId:'unsupported-explicit-model'});
    assert.equal(s.routing,'documents');
    assert.equal(s.available,false);
    assert.equal(s.context.modelId,'unsupported-explicit-model');
    assert.equal(s.conversation.id,cid);
    assert.equal(s.answers.length,3);
    assert.equal((await metadata()).fakeProviderCalls,3);
    assert.equal((await metadata()).ordinaryAskBlocked,0);
    assert.equal((await metadata()).outboundBlocked,0);
    // Deleting only the later original must retain older available answers and
    // the later exact answer identity as an explicit governed withdrawal.
    const availabilityReason=s.reason;assert.equal(availabilityReason,'Motet file reading requires the qualified model and host policy.');
    s=await client.prepareDeletion(laterFileId);assert.equal(s.error,null);const selectiveOperation=s.deletion.id;
    observeDeletionConfirm=true;dropConfirmation=true;s=await client.confirmDeletion(s.deletion.confirmationHandle);
    assert.ok(s.error?.message);assert.equal(s.reason,availabilityReason);assert.equal(s.uncertain,true);assert.deepEqual(s.answers,[]);
    const recoveryReads=requests.length;s=await client.refresh();assert.equal(s.error,null);assert.equal(s.reason,availabilityReason);assert.equal(s.uncertain,true);assert.equal(s.deletions.find(d=>d.id===selectiveOperation).state,'confirm');assert.deepEqual(s.answers,[]);
    s=await client.closeDeletion();assert.equal(s.deletion,null);assert.equal(s.uncertain,true);
    s=await client.openDeletion(s.deletions.find(d=>d.id===selectiveOperation).operationHandle);assert.equal(s.deletion.id,selectiveOperation);assert.equal(s.deletion.uncertain,true);assert(requests.slice(recoveryReads).every(r=>r.method==='GET'));
    s=await client.confirmDeletion(s.deletion.confirmationHandle);assert.equal(s.error,null,JSON.stringify(s.error));assert.equal(s.reason,availabilityReason);assert.equal(s.uncertain,false);assert.equal(s.deletion.id,selectiveOperation);assert.equal(s.deletion.sourceUnavailable,true);assert.equal(lostConfirmKey,null);
    const selective=view=>{exactHistory(view);assert.deepEqual(view.answers.filter(a=>a.id!==laterAnswerId).map(answerFacts),earlierAnswers);const a=view.answers.find(a=>a.id===laterAnswerId);assert.equal(a.state,'source_unavailable');assert.equal(a.text,null);assert.deepEqual(a.suppliedFileIds,[]);assert.deepEqual(a.citations,[]);assert.equal(view.files.find(f=>f.id===laterFileId).available,false);assert.equal(view.files.find(f=>f.id===originalFileId).available,true);};
    selective(s);const selectiveReads=requests.length;s=await client.refresh();assert(requests.slice(selectiveReads).every(r=>r.method==='GET'));selective(s);
    client.dispose();client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport});
    const coldReads=requests.length;s=await client.connect({scopeId:'room',modelId:'unsupported-explicit-model'});assert.equal(s.error,null,JSON.stringify(s.error));assert(requests.slice(coldReads).every(r=>r.method==='GET'));selective(s);assert.equal((await metadata()).fakeProviderCalls,3);
    const source = s.management.files.find(f=>f.id===originalFileId&&f.available);
    assert(source?.canDelete,'unsupported reading model must retain exact cleanup eligibility');
    s = await client.prepareDeletion(source.id);
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.deletion.state,'confirm');
    assert.equal(s.deletion.hasUnresolvedQuestion,false);
    assert.equal(s.deletion.removedAt,null);
    const operationId=s.deletion.id;
    observeDeletionConfirm=true;
    s = await client.confirmDeletion(s.deletion.confirmationHandle);
    assert.equal(s.error,null,JSON.stringify(s.error));
    assert.equal(s.deletion.id,operationId);
    assert.equal(s.deletion.sourceUnavailable,true);
    assert(['deleting','deleted'].includes(s.deletion.state));
    exactHistory(s);for(const a of s.answers){assert.equal(a.state,'source_unavailable');assert.equal(a.text,null);assert.deepEqual(a.suppliedFileIds,[]);assert.deepEqual(a.citations,[]);}
    assert.equal((await metadata()).fakeProviderCalls,3);
    const afterConfirm=requests.length;
    await client.closeDeletion();
    s=await client.openDeletion(s.deletions.find(d=>d.id===operationId).operationHandle);
    assert(requests.slice(afterConfirm).every(r=>r.method==='GET'));
    assert.equal(s.deletion.sourceUnavailable,true);assert.equal(s.files.find(f=>f.id===originalFileId).admissionRequestId,admissionRequestId);
    if(s.deletion.state==='deleted'){assert.equal(typeof s.deletion.removedAt,'number');assert.equal(s.deletion.releasedBytes,source.bytes);}
    else assert.equal(s.deletion.releasedBytes,0);
    assert(!/"(?:capability|fingerprint|requestCapability|sourceCapability|writerNonce)"/.test(JSON.stringify(s)));

    // Add an original after the prior answers were withdrawn. The still-usable
    // image remains in the roster, and withdrawn answers stay out of the window.
    assert.equal(s.usableFileCount,1);
    client.dispose();client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport});
    s=await client.connect({scopeId:'room',modelId:'@automatic'});assert.equal(s.error,null);
    s=await client.addFiles([new File(['%PDF-1.4\nFictional new roster.\n%%EOF'],'fictional-window.pdf',{type:'application/pdf'})]);
    assert.equal(s.error,null);const windowSource=s.draft.files[0].id;
    s=await client.send('');assert.equal(s.error,null);assert.equal(s.usableFileCount,2);
    const recorded=[];
    for(let n=0;n<10;n++){
      s=await client.send('Fictional window question '+n,{keptFiles:s.files.filter(f=>f.available)});assert.equal(s.error,null,JSON.stringify(s.error));
      const a=s.answers.find(a=>a.requestId===s.lastSend.requestId),q=s.requests.find(r=>r.requestId===s.lastSend.requestId);
      const post=requests.findLast(r=>r.url.endsWith('/question-intent'));
      assert.equal(post.priorPolicy,'recent_available_8');assert.deepEqual(post.priorIds,recorded.slice(-8));
      assert.equal(a.state,'available');assert(a.answeredAt>=q.createdAt);assert.deepEqual(a.priorAnswerWindow,{policy:'recent_available_8',includedCount:Math.min(n,8),omittedOlder:n>8});
      recorded.push(a.id);
    }
    const last=s.answers.find(a=>a.id===recorded.at(-1));assert.equal(s.answers.length,13);
    client.dispose();client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:transport});
    const windowReload=requests.length;s=await client.connect({scopeId:'room',modelId:'@automatic'});assert.equal(s.error,null);assert(requests.slice(windowReload).every(r=>r.method==='GET'));
    assert.deepEqual(answerFacts(s.answers.find(a=>a.id===last.id)),answerFacts(last));
    s=await client.prepareDeletion(windowSource);assert.equal(s.error,null);s=await client.confirmDeletion(s.deletion.confirmationHandle);assert.equal(s.error,null);
    const gone=s.answers.find(a=>a.id===last.id);assert.equal(gone.state,'source_unavailable');assert.equal(gone.answeredAt,last.answeredAt);assert.equal(gone.priorAnswerWindow,null);assert.equal(s.usableFileCount,1);
    const windowMeta=await metadata();assert.equal(windowMeta.fakeProviderCalls,13);assert.equal(windowMeta.outboundBlocked,0);assert.equal(windowMeta.ordinaryAskBlocked,0);

    secondary=spawn(PYTHON,fixtureArgs(['--reading-off']),fixtureOptions);
    let secondaryLog='',secondaryErrors='';secondary.stderr.on('data',b=>secondaryErrors=(secondaryErrors+b).slice(-10000));
    const coldOrigin=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Management-only fixture startup timed out')),10000);secondary.stdout.on('data',b=>{secondaryLog+=b;const match=secondaryLog.match(/http:\/\/127\.0\.0\.1:\d+\//);if(match){clearTimeout(timer);resolve(match[0].slice(0,-1));}});secondary.once('exit',code=>{clearTimeout(timer);reject(Error('Management-only fixture exited '+code+' '+secondaryErrors));});});
    const managementRequests=[];
    const managementTransport=(url,init)=>{managementRequests.push({url,method:init.method});return fetch(coldOrigin+url,init);};
    client.dispose();
    client=createOfficerDocumentsClient({enabled:true,documentsEnabled:false,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:managementTransport});
    s=await client.connect({scopeId:'room',modelId:null});
    assert.equal(s.error,null,JSON.stringify(s.error));assert.equal(s.available,false);assert.equal(s.management.available,true);assert.equal(s.routing,'documents');assert.equal(s.management.files.length,1);assert.deepEqual(s.answers,[]);
    assert(managementRequests.every(r=>r.method==='GET'&&(r.url.includes('/conversations/current')||r.url.includes('/management'))));
    const kept=s.management.files[0];s=await client.prepareDeletion(kept.id);assert.equal(s.error,null,JSON.stringify(s.error));assert.equal(s.deletion.state,'confirm');
    s=await client.confirmDeletion(s.deletion.confirmationHandle);assert.equal(s.error,null,JSON.stringify(s.error));assert.equal(s.deletion.sourceUnavailable,true);const coldOperation=s.deletion.id;
    client.dispose();client=createOfficerDocumentsClient({enabled:true,documentsEnabled:false,demo:false,auth:()=>key,crypto:webcrypto,AbortController,fetch:managementTransport});
    const restoreStart=managementRequests.length;s=await client.connect({scopeId:'room',modelId:null});assert.equal(s.error,null,JSON.stringify(s.error));assert.equal(s.deletions[0].id,coldOperation);assert.equal(s.deletions[0].sourceUnavailable,true);assert(managementRequests.slice(restoreStart).every(r=>r.method==='GET'));
    const coldMetadata=await(await fetch(coldOrigin+'/fixture/metadata',{headers:{'X-Live-Room-Key':key}})).json();assert.equal(coldMetadata.readingEnabled,false);assert.equal(coldMetadata.fakeProviderCalls,0);assert.equal(coldMetadata.ordinaryAskBlocked,0);assert.equal(coldMetadata.outboundBlocked,0);
    console.log('Motet document client: actual HTTP reading/Auto/history, recent-answer window, deletion withdrawal/reopen and reading-off cleanup/reload passed. 13 fictional calls, 0 external calls.');
  } finally {
    client?.dispose();
    if(secondary){stopFixture(secondary);await new Promise(resolve=>secondary.exitCode!==null?resolve():secondary.once('exit',resolve));}
    stopFixture(child);
    await new Promise(resolve=>child.exitCode!==null?resolve():child.once('exit',resolve));
  }
})().catch(e=>{console.error(e);if(errors)console.error(errors);process.exitCode=1;});
