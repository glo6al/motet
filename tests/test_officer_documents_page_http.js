#!/usr/bin/env node
/* Actual page DTO validator + actual client + disposable owner HTTP/storage.
 * This checks every published snapshot, including transient local File reads;
 * it does not claim native picker, DOM, layout, or real provider coverage. */
'use strict';
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {webcrypto} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createOfficerDocumentsClient} = require('../src/web/officer-documents-client.js');

const pageSource = fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const liveMarker = '/* Workspace live Officer documents: separate asynchronous owner-resource consumer. */';
const liveEnd = '/* end Workspace live Officer documents */';
assert.equal(pageSource.split(liveMarker).length,2,'one actual live consumer region');
const liveSource = pageSource.split(liveMarker)[1].split(liveEnd)[0];
const validatorEnd = liveSource.indexOf('function workspaceLiveDocumentsOutcome(');
assert(validatorEnd>0,'actual closed validator extraction boundary');
// Both real validation functions, with their actual dependency closure. No DOM
// substitutes or relaxed public DTO schema are introduced by this harness.
const validate = vm.runInNewContext(liveSource.slice(0,validatorEnd)+';workspaceLiveDocumentsSnapshot');
const clientSource = fs.readFileSync(path.join(__dirname,'../src/web/officer-documents-client.js'),'utf8');
const key = 'fictional-officer-browser-fixture-key';
const context = {scopeId:'room',modelId:'claude-haiku-5-5'};

function selected() {
  return new File(['%PDF-1.4\nFictional offline document.\n%%EOF'],'fictional.pdf',{type:'application/pdf'});
}
function selectedImage() {
  // Fictional one-pixel PNG, also used by the repository's offline fixture tests.
  return new File([Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6zs8AAAAASUVORK5CYII=','base64')],
    'fictional-marker.png',{type:'image/png'});
}
class UnreadableFile extends File {
  async arrayBuffer() { throw Error('Fictional local File read failed'); }
}
function unreadable() {
  return new UnreadableFile(['Fictional unreadable bytes'],'fictional-unreadable.pdf',{type:'application/pdf'});
}
function summary(s) {
  return JSON.stringify({revision:s?.revision,routing:s?.routing,busy:s?.busy,complete:s?.complete,
    uncertain:s?.uncertain,conversation:!!s?.conversation,draft:!!s?.draft,
    uploads:s?.uploads?.map(u=>({state:u.state,mapped:!!u.fileId}))});
}
function auditFailure(stage,s) {
  const error = Error(stage+' rejected by actual page validator: '+summary(s));
  error.code='PAGE_SNAPSHOT_REJECTED';
  return error;
}

async function ownerFixture(run,args=[]) {
  const child=spawn('python3',[path.join(__dirname,'officer_document_live_fixture.py'),'--model',context.modelId,...args],
    {env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';
  child.stderr.on('data',b=>{stderr=(stderr+b).slice(-10000);});
  const exited=new Promise(resolve=>child.once('exit',resolve));
  try {
    const origin=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Disposable owner HTTP startup timed out')),10000);
      child.once('error',e=>{clearTimeout(timer);reject(e);});
      child.once('exit',code=>{clearTimeout(timer);reject(Error('Disposable owner HTTP exited '+code+' '+stderr));});
      child.stdout.on('data',b=>{
        stdout+=b;const match=stdout.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if(match){clearTimeout(timer);resolve(match[0].slice(0,-1));}
      });
    });
    assert.equal(new URL(origin).hostname,'127.0.0.1');
    await run(origin);
  } finally {
    if(child.exitCode===null&&child.signalCode===null)child.kill('SIGINT');
    let timer;
    try {
      await Promise.race([exited,new Promise((_,reject)=>{timer=setTimeout(()=>{
        child.kill('SIGKILL');reject(Error('Disposable owner HTTP cleanup timed out: '+stderr));
      },5000);})]);
    } finally {clearTimeout(timer);}
  }
}

async function coupled(factory,scenario,options={}) {
  await ownerFixture(async origin=>{
    const calls=[],changes=[];
    let phase='construct',dropCreate=null,committedConversation=null,dropConfirm=false;
    let failManagement=options.failFirstManagement===true,observe=null,requestDeadline=null;
    const transport=async(url,init)=>{
      assert.equal(typeof url,'string');
      assert(url.startsWith('/api/attachments/'),'client stays on owner attachment routes');
      const target=new URL(url,origin);
      assert.equal(target.origin,origin,'no external transport');
      const call={url,method:init.method||'GET',body:init.body,
        commandKey:init.headers?.['Idempotency-Key']};calls.push(call);
      const create=call.method==='POST'&&url==='/api/attachments/conversations/current';
      if(create&&dropCreate==='before'){
        dropCreate=null;throw Error('Fictional create transport lost before server receipt');
      }
      if(failManagement&&call.method==='GET'&&url.includes('/management?')){
        failManagement=false;throw Error('Fictional management GET transport lost');
      }
      if(dropConfirm&&call.method==='POST'&&/\/deletions\/[^/]+\/confirm$/.test(url)){
        dropConfirm=false;throw Error('Fictional confirmation transport lost before server receipt');
      }
      if(observe)await observe(call);
      const timeout=requestDeadline===null?5000:Math.max(1,Math.min(5000,Math.ceil(requestDeadline-performance.now())));
      const response=await fetch(target,{...init,signal:AbortSignal.any([init.signal,AbortSignal.timeout(timeout)].filter(Boolean))});
      if(create&&dropCreate==='after'){
        dropCreate=null;
        assert.equal(response.status,200);
        committedConversation=(await response.clone().json()).conversation;
        throw Error('Fictional committed create acknowledgement lost');
      }
      return response;
    };
    const client=factory({enabled:true,demo:false,auth:()=>key,crypto:webcrypto,AbortController,
      ...(options.documentsEnabled===undefined?{}:{documentsEnabled:options.documentsEnabled}),
      fetch:transport,changed:s=>{
        // The actual client deliberately catches callback exceptions. Record
        // validation here and assert outside it so bad intermediates cannot hide.
        changes.push({phase,s,valid:validate(s)});
      }});
    async function checked(label,operation) {
      phase=label;const begin=changes.length;
      const result=await operation();
      for(const change of changes.slice(begin)){
        if(!change.valid)throw auditFailure(label+' callback',change.s);
      }
      if(!validate(result))throw auditFailure(label+' result',result);
      for(let i=1;i<changes.length;i++)assert(changes[i].s.revision>changes[i-1].s.revision,'callback revisions advance');
      return result;
    }
    try {
      const initial=await checked('connect',()=>client.connect(options.context||context));
      if(options.checkInitial)options.checkInitial(initial);
      else {assert.equal(initial.routing,'ordinary');assert.equal(initial.complete,true);assert.equal(initial.available,true);}
      assert(calls.every(c=>c.method==='GET'),'connect is read-only');
      await scenario({client,calls,changes,checked,initial,setDrop:value=>{dropCreate=value;},
        dropNextConfirmation:()=>{dropConfirm=true;},observe:value=>{observe=value;},committed:()=>committedConversation,
        requestDeadline:value=>{requestDeadline=value;},
        releaseDeletion:async()=>{
          const response=await fetch(origin+'/fixture/release-deletion',{method:'POST',
            headers:{'X-Live-Room-Key':key,'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(5000)});
          assert.equal(response.status,200);assert.equal((await response.json()).released,true);
        }});
      assert(changes.length>0,'actual changed callback was exercised');
      assert(changes.every(c=>c.valid),'all callbacks pass actual page validator');
    } finally {
      phase='dispose';client.dispose();
      const disposed=changes.at(-1);
      if(disposed&&!disposed.valid)throw auditFailure('dispose callback',disposed.s);
      if(!validate(client.snapshot()))throw auditFailure('disposed snapshot',client.snapshot());
      const executions=calls.filter(c=>c.method==='POST').flatMap(c=>{
        const match=c.url.match(/\/(send|send-intent|question-intent|ask)$/);return match?[match[1]]:[];
      });
      const expectedExecutions=typeof options.executions==='function'?options.executions():options.executions||[];
      assert.deepEqual(executions,expectedExecutions,'only the exact explicit question/admission attempts are issued');
      const metadataResponse=await fetch(origin+'/fixture/metadata',{headers:{'X-Live-Room-Key':key},signal:AbortSignal.timeout(5000)});
      assert.equal(metadataResponse.status,200);
      const metadata=await metadataResponse.json();
      const expectedProviderCalls=typeof options.providerCalls==='function'?options.providerCalls():options.providerCalls||0;
      assert.equal(metadata.offline,true);assert.equal(metadata.fakeProviderCalls,expectedProviderCalls);
      assert.equal(metadata.ordinaryAskBlocked,0);assert.equal(metadata.outboundBlocked,0);
    }
  },options.fixtureArgs||[]);
}

async function firstUpload(factory) {
  await coupled(factory,async h=>{
    const s=await h.checked('first upload',()=>h.client.addFiles([selected()]));
    assert.equal(s.error,null);assert.equal(s.routing,'documents');assert(s.conversation);
    assert.equal(s.uploads.length,1);assert.equal(s.uploads[0].state,'ready');
    assert.equal(s.draft.files.length,1);assert.equal(s.draft.files[0].state,'ready');
    assert.equal(s.draft.files[0].id,s.uploads[0].fileId);
    const reading=h.changes.filter(c=>c.phase==='first upload'&&c.s.uploads.some(u=>u.state==='reading'));
    assert(reading.length>0,'actual transient File-reading callback must be covered');
    assert(reading.some(c=>!c.s.conversation),'includes read before durable conversation creation');
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url==='/api/attachments/conversations/current').length,1);
  });
}

async function readFailure(factory) {
  await coupled(factory,async h=>{
    let s=await h.checked('File read failure',()=>h.client.addFiles([unreadable()]));
    assert.equal(s.uploads.length,1);assert.equal(s.uploads[0].state,'failed');assert.equal(s.uploads[0].fileId,null);
    assert.equal(s.error.code,'file_read_failed');assert.equal(s.conversation,null);assert.equal(s.draft,null);
    assert.notEqual(s.routing,'ordinary');assert(h.calls.every(c=>c.method==='GET'),'failed local read creates no durable resources');
    const localId=s.uploads[0].id,start=h.calls.length;
    s=await h.checked('refresh failed local selection',()=>h.client.refresh());
    assert(h.calls.slice(start).length>0);assert(h.calls.slice(start).every(c=>c.method==='GET'));
    assert.equal(s.complete,true);assert.equal(s.uploads[0].id,localId);assert.equal(s.uploads[0].state,'failed');
    assert.notEqual(s.routing,'ordinary','empty server history cannot discard the local failed selection');
    const removeStart=h.calls.length;
    s=await h.checked('remove failed local selection',()=>h.client.removeUpload(localId));
    assert.equal(h.calls.length,removeStart,'unmapped local removal does not mutate the server');
    assert.deepEqual(s.uploads,[]);assert.equal(s.conversation,null);assert.equal(s.draft,null);
    assert.equal(s.uncertain,false);assert.equal(s.complete,true);assert.equal(s.routing,'ordinary');
    s=await h.checked('refresh after local removal',()=>h.client.refresh());
    assert.equal(s.routing,'ordinary');assert.deepEqual(s.uploads,[]);
  });
}

async function lostCreate(factory,commit) {
  await coupled(factory,async h=>{
    h.setDrop(commit?'after':'before');
    let s=await h.checked('lost create acknowledgement',()=>h.client.addFiles([selected()]));
    assert.equal(s.uploads.length,1);assert.equal(s.uploads[0].state,'failed');assert.equal(s.uploads[0].fileId,null);
    assert.equal(s.uncertain,true);assert.notEqual(s.routing,'ordinary');
    const localId=s.uploads[0].id,start=h.calls.length;
    // Removing locally before reconciliation cannot resolve a possibly committed
    // create. It must leave routing held even when the last local row disappears.
    s=await h.checked('remove unresolved local selection',()=>h.client.removeUpload(localId));
    assert.equal(h.calls.length,start);assert.deepEqual(s.uploads,[]);
    assert.equal(s.uncertain,true);assert.notEqual(s.routing,'ordinary');
    s=await h.checked('reconcile lost create with GET',()=>h.client.refresh());
    assert(h.calls.slice(start).length>0);assert(h.calls.slice(start).every(c=>c.method==='GET'),'no automatic create retry');
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url==='/api/attachments/conversations/current').length,1);
    assert.equal(s.uncertain,false);assert.equal(s.complete,true);
    if(commit){
      assert(h.committed());assert.equal(s.conversation.id,h.committed().id);assert.equal(s.routing,'documents');
    }else{
      assert.equal(h.committed(),null);assert.equal(s.conversation,null);assert.equal(s.routing,'ordinary');
    }
    assert.equal(s.draft,null);assert.deepEqual(s.uploads,[]);assert.deepEqual(s.requests,[]);assert.deepEqual(s.answers,[]);
  });
}

function seededOptions(extra={}) {
  return {fixtureArgs:['--seed-file'],checkInitial:s=>{
    assert.equal(s.error,null);assert.equal(s.routing,'documents');assert(s.conversation);
    assert.equal(s.management.available,true);assert.equal(s.management.complete,true);
    assert.equal(s.management.files.length,1);assert.equal(s.management.files[0].canDelete,true);
  },...extra};
}
function checkDeletion(s,file,state) {
  const d=s.deletion;
  assert(d);assert.equal(d.fileId,file.id);assert.equal(d.name,file.name);assert.equal(d.bytes,file.bytes);
  assert.equal(d.state,state);assert.equal(d.conversationId,s.conversation.id);assert.equal(d.scopeId,'room');
  assert.equal(d.confirmation.copyCount,1);assert.equal(d.confirmation.scope,'watch_original');
  assert.deepEqual(d.confirmation.retained,['original_file','provider_copies','conversation_messages']);
  if(state==='confirm'){
    assert.equal(d.releasedBytes,0);assert.equal(d.sourceUnavailable,false);assert.equal(d.removedAt,null);
  }else{
    assert.equal(d.sourceUnavailable,true);assert.equal(d.canConfirm,false);assert.equal(d.confirmationHandle,null);
    if(state==='deleted'){assert.equal(d.releasedBytes,file.bytes);assert.equal(typeof d.removedAt,'number');}
    else {assert.equal(d.releasedBytes,0);assert.equal(d.removedAt,null);}
  }
}
async function observeResult(h,operationId,expected) {
  // Only test-side observations wait for the real joined cleanup worker. These
  // bounded explicit API reads never prepare, confirm, execute or retry work.
  let s=h.client.snapshot();const deadline=performance.now()+5000;h.requestDeadline(deadline);
  try {
    for(let reads=0;s.deletion?.state!==expected&&reads<8&&performance.now()<deadline;reads++){
      const row=s.deletions.find(d=>d.id===operationId);assert(row?.operationHandle);
      s=await h.checked('read recorded cleanup result',()=>h.client.openDeletion(row.operationHandle));
      assert.equal(s.error,null);
      const wait=Math.min(50,deadline-performance.now());
      if(s.deletion?.state!==expected&&wait>0)await new Promise(resolve=>setTimeout(resolve,wait));
    }
  }finally{h.requestDeadline(null);}
  assert.equal(s.deletion?.state,expected,'real cleanup worker reached its recorded outcome: '+summary(s));
  return s;
}
async function deletionLifecycle(factory,managementOnly=false,outcome='normal') {
  const expected={normal:'deleted',changed:'partial_failure',unknown:'unknown'}[outcome];
  const options=seededOptions({fixtureArgs:['--seed-file','--deletion-outcome',outcome,...(managementOnly?['--reading-off']:[])],
    ...(managementOnly?{documentsEnabled:false,context:{scopeId:'room',modelId:null}}:{})});
  await coupled(factory,async h=>{
    const file=h.initial.management.files[0];
    if(managementOnly){
      assert.equal(h.initial.context.modelId,null);assert.equal(h.initial.documentsEnabled,false);
      assert.equal(h.initial.available,false);assert.deepEqual(h.initial.files,[]);assert.deepEqual(h.initial.answers,[]);
      assert(h.calls.every(c=>c.url.includes('/conversations/current')||c.url.includes('/management?')));
    }
    let s=await h.checked('prepare exact kept file',()=>h.client.prepareDeletion(file.id));
    assert.equal(s.error,null,'cleanup remains independently authorized');checkDeletion(s,file,'confirm');
    const operationId=s.deletion.id,closedHandle=s.deletion.confirmationHandle;assert(closedHandle);
    const closeStart=h.calls.length;
    s=await h.checked('Close before confirmation',()=>h.client.closeDeletion());
    assert.equal(s.deletion,null);assert.equal(h.calls.length,closeStart,'Close makes no HTTP mutation');
    s=await h.checked('closed confirmation handle rejected',()=>h.client.confirmDeletion(closedHandle));
    assert(s.error);assert.equal(h.calls.length,closeStart,'closing invalidates exact confirmation authority');
    s=await h.checked('read existing deletion',()=>h.client.openDeletion(s.deletions.find(d=>d.id===operationId).operationHandle));
    assert.equal(s.error,null);checkDeletion(s,file,'confirm');assert.notEqual(s.deletion.confirmationHandle,closedHandle);
    s=await h.checked('explicit deletion confirmation',()=>h.client.confirmDeletion(s.deletion.confirmationHandle));
    assert.equal(s.error,null);assert.equal(s.deletion.id,operationId);
    s=await observeResult(h,operationId,expected);checkDeletion(s,file,expected);
    assert.equal(s.deletion.reason,outcome==='changed'?'identity_changed':outcome==='unknown'?'removal_unconfirmed':null);
    const beforeRead=h.calls.length;
    await h.checked('Close recorded deletion result',()=>h.client.closeDeletion());
    s=await h.checked('reopen recorded deletion result',()=>h.client.openDeletion(s.deletions.find(d=>d.id===operationId).operationHandle));
    checkDeletion(s,file,expected);assert(h.calls.slice(beforeRead).every(c=>c.method==='GET'));
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/deletion-intent')).length,1);
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/confirm')).length,1);
    assert(!/"(?:capability|fingerprint|requestCapability|sourceCapability|writerNonce)"/.test(JSON.stringify(s)));
    if(managementOnly)assert(h.calls.every(c=>!c.url.includes('/capabilities')&&!c.url.includes('/requests')&&!c.url.includes('/answers/')));
  },options);
}

async function firstRecovery(factory) {
  await coupled(factory,async h=>{
    assert.equal(h.initial.management.available,false);assert.equal(h.initial.management.complete,false);
    assert.equal(h.initial.complete,false);assert(h.initial.management.firstHandle);
    const start=h.calls.length;
    let s=await h.checked('explicit First after lost management GET',()=>h.client.managementPage(h.initial.management.firstHandle));
    assert.equal(s.error,null);assert.equal(s.management.page,1);assert.equal(s.management.complete,true);
    assert.equal(s.complete,false,'management recovery does not invent complete reading restoration');
    assert(h.calls.slice(start).every(c=>c.method==='GET'));
    s=await h.checked('cleanup after First-only management recovery',()=>h.client.prepareDeletion(s.management.files[0].id));
    assert.equal(s.error,null,'cleanup remains independently authorized');assert.equal(s.deletion.state,'confirm');
    await h.checked('Close first-page recovered confirmation',()=>h.client.closeDeletion());
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/deletion-intent')).length,1);
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/confirm')).length,0);
  },{fixtureArgs:['--reading-off'],documentsEnabled:false,context:{scopeId:'room',modelId:null},failFirstManagement:true,
    checkInitial:s=>{assert(s.error);assert.equal(s.routing,'documents');assert(s.conversation);}});
}

async function singleFilePastFifty(factory) {
  await coupled(factory,async h=>{
    assert.equal(h.initial.files.length,51);
    const next=h.initial.management.nextHandle;
    assert.ok(next);
    let s=await h.checked('read the second page of kept files',()=>h.client.managementPage(next));
    assert.equal(s.management.page,2);
    assert.equal(s.management.files.length,1);
    const chosen=s.management.files[0];
    s=await h.checked('ask about exactly the selected file on page two',
      ()=>h.client.send('Read the one selected fictional file.',{keptFiles:[chosen]}));
    assert.equal(s.error,null);
    assert.deepEqual(Array.from(s.answers[0].suppliedFileIds),[chosen.id]);
    assert.deepEqual(Array.from(s.requests.find(r=>r.state==='answered').questionFileIds),[chosen.id]);
  },{fixtureArgs:['--seed-file','--seed-count','51'],providerCalls:1,
    executions:['question-intent','ask'],checkInitial:s=>{
    assert.equal(s.routing,'documents');assert.equal(s.complete,true);assert.equal(s.available,true);
  }});
}

async function publicationBarrier(factory,lostConfirmation=false) {
  await coupled(factory,async h=>{
    let s=await h.checked('ask fictional retained source',()=>h.client.send('What does the fictional kept source say?',{keptFiles:h.client.snapshot().files.filter(f=>f.available)}));
    assert.equal(s.error,null);assert.equal(s.answers.length,1);assert.equal(s.answers[0].state,'available');
    assert(s.answers[0].text);assert(s.answers[0].citations.length>0);
    const file=s.management.files[0],oldHandles=[s.files[0].previewHandle,s.answers[0].citations[0].previewHandle];
    assert(oldHandles.every(Boolean));
    s=await h.checked('open current governed source',()=>h.client.preview(oldHandles[1]));assert(s.preview);
    s=await h.checked('prepare governed-source deletion',()=>h.client.prepareDeletion(file.id));
    assert.equal(s.error,null);const operationId=s.deletion.id,firstHandle=s.deletion.confirmationHandle;
    if(lostConfirmation)h.dropNextConfirmation();
    const barrier=h.changes.length;
    s=await h.checked('confirm governed-source deletion',()=>{
      const pending=h.client.confirmDeletion(firstHandle),immediate=h.client.snapshot();
      assert(immediate.answers.length===0&&immediate.preview===null&&immediate.files.every(f=>f.previewHandle===null),
        'confirmation synchronously withdraws governed publication');
      return pending;
    });
    assert.equal(s.deletion.id,operationId);assert.equal(s.uncertain,lostConfirmation);
    if(lostConfirmation){assert(s.error);assert.equal(s.deletion.confirmationHandle,null);}
    else {assert.equal(s.error,null);checkDeletion(s,file,'deleting');}
    const afterConfirm=h.calls.length;
    s=await h.checked('Close issued deletion',()=>h.client.closeDeletion());assert.equal(s.deletion,null);
    s=await h.checked('GET reconcile issued deletion',()=>h.client.refresh());
    assert(h.calls.slice(afterConfirm).every(c=>c.method==='GET'));
    assert.equal(s.preview,null);assert(s.answers.every(a=>a.text===null),'same prepared generation cannot republish governed answers');assert(s.files.every(f=>f.previewHandle===null));
    if(lostConfirmation){
      assert.equal(s.uncertain,true);assert.equal(s.deletions.find(d=>d.id===operationId).state,'confirm');
      assert.equal(s.deletions.find(d=>d.id===operationId).uncertain,true);
      const staleStart=h.calls.length;
      for(const handle of oldHandles){s=await h.checked('stale governed preview rejected',()=>h.client.preview(handle));assert(s.error);}
      assert.equal(h.calls.length,staleStart,'old source handles never reach HTTP');
      await h.checked('invalidate unresolved cleanup view',()=>{h.client.invalidate();return h.client.snapshot();});
      s=await h.checked('restore same-owner cleanup hold',()=>h.client.connect(context));
      assert.equal(s.uncertain,true);assert.deepEqual(s.answers,[]);assert(s.files.every(f=>f.previewHandle===null));
      s=await h.checked('read fresh uncertain confirmation handle',()=>h.client.openDeletion(s.deletions.find(d=>d.id===operationId).operationHandle));
      assert.equal(s.deletion.uncertain,true);assert(s.deletion.confirmationHandle);
      assert.notEqual(s.deletion.confirmationHandle,firstHandle);assert.equal(s.deletion.state,'confirm');
      const fresh=s.deletion.confirmationHandle,oldStart=h.calls.length;
      s=await h.checked('consumed old confirmation rejected',()=>h.client.confirmDeletion(firstHandle));
      assert(s.error);assert.equal(h.calls.length,oldStart);
      // This is a second explicit owner action after a read, never an automatic
      // retry. The canonical client must preserve the immutable command key.
      s=await h.checked('explicit same-generation confirmation',()=>h.client.confirmDeletion(fresh));
      assert.equal(s.error,null);s=await observeResult(h,operationId,'deleted');checkDeletion(s,file,'deleted');
      const confirmations=h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/confirm'));
      assert.equal(confirmations.length,2);assert.equal(confirmations[0].commandKey,confirmations[1].commandKey);
      assert.equal(confirmations[0].body,confirmations[1].body);
    }else{
      s=await h.checked('read pending result',()=>h.client.openDeletion(s.deletions.find(d=>d.id===operationId).operationHandle));
      checkDeletion(s,file,'deleting');assert.equal(s.deletion.reason,'source_in_use');
      await h.releaseDeletion();s=await observeResult(h,operationId,'deleted');checkDeletion(s,file,'deleted');
      assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/confirm')).length,1);
    }
    assert(h.changes.slice(barrier+1).every(c=>c.s.answers.every(a=>a.text===null)&&c.s.preview===null&&c.s.files.every(f=>f.previewHandle===null)),
      'issued cleanup cannot republish governed prose or source handles');
    assert.equal(h.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/deletion-intent')).length,1);
  },seededOptions({fixtureArgs:['--seed-file',...(!lostConfirmation?['--deletion-outcome','pending']:[])],
    providerCalls:1,executions:['question-intent','ask']}));
}

function admissionObserver(h) {
  const boundaries=[];
  h.observe(call=>{
    if(call.method==='PATCH'||call.method==='POST'&&/\/(send-intent|send)$/.test(call.url)){
      const s=h.client.snapshot();boundaries.push({url:call.url,s,valid:validate(s)});
    }
  });
  return boundaries;
}
function checkPrivateUntilAdmission(h,boundaries,localId,fileId) {
  assert(boundaries.some(b=>b.url.endsWith('/send-intent')),'real preparation boundary was observed');
  assert(boundaries.some(b=>b.url.endsWith('/send')),'real admission boundary was observed');
  assert(boundaries.every(b=>b.valid),'actual boundary snapshots pass page validation');
  const privateRows=s=>s.uploads.length===1&&s.uploads[0].id===localId&&s.uploads[0].fileId===fileId;
  assert(boundaries.every(b=>privateRows(b.s)),'selected rows stay private through PATCH and prepared intent');
  const saved=h.changes.filter(c=>c.phase==='admit selected file'&&c.s.lastSend?.retained&&c.s.lastSend.requestId===null);
  assert(saved.length>0,'actual acknowledged text-save emissions were checked');
  assert(saved.every(c=>privateRows(c.s)),'text-save acknowledgement is not file admission');
  const prepared=h.changes.filter(c=>c.phase==='admit selected file'&&c.s.lastSend?.requestId&&privateRows(c.s));
  assert(prepared.length>0,'prepared intent retains the exact private selected row');
}
async function admittedSelection(factory,outcome='keep') {
  const question=outcome!=='keep';
  await coupled(factory,async h=>{
    let s=await h.checked('upload selected source',()=>h.client.addFiles([selected()]));
    assert.equal(s.error,null);assert.equal(s.uploads.length,1);
    const localId=s.uploads[0].id,fileId=s.uploads[0].fileId,draftId=s.draft.id;
    s=await h.checked('save private draft text',()=>h.client.editText(question?'A fictional question.':''));
    assert.equal(s.error,null);assert.equal(s.uploads[0].id,localId);assert.equal(s.draft.id,draftId);
    const boundaries=admissionObserver(h);
    s=await h.checked('admit selected file',()=>h.client.send(question?'What does the fictional selected source say?':''));
    assert.equal(s.error,null);checkPrivateUntilAdmission(h,boundaries,localId,fileId);
    assert.equal(s.uploads.length,0,'exact admitted selection leaves no stale private upload rows');
    assert.equal(s.draft,null);assert.equal(s.files.length,1);assert.equal(s.files[0].id,fileId);
    assert.equal(s.requests.length,1);assert.equal(s.requests[0].draftId,draftId);
    assert.equal(s.requests[0].state,outcome==='keep'?'retained_local':outcome==='unknown'?'unknown':'answered');
    assert.equal(s.uncertain,outcome==='unknown');const requestId=s.requests[0].requestId;
    h.observe(null);
    const refreshStart=h.calls.length;
    s=await h.checked('read admitted selection status',()=>h.client.refresh());
    assert(h.calls.slice(refreshStart).every(c=>c.method==='GET'));assert.equal(s.uploads.length,0);
    assert.equal(s.requests[0].requestId,requestId);assert.equal(s.uncertain,outcome==='unknown');
    if(outcome==='unknown'){
      const before=h.calls.length;
      s=await h.checked('unknown admission cannot be resent',()=>h.client.send('A later fictional question.'));
      assert(s.error);assert.equal(s.uncertain,true);assert.equal(s.uploads.length,0);assert.equal(h.calls.length,before);
      const source=s.management.files.find(f=>f.id===fileId);assert.equal(source.canDelete,true);
      s=await h.checked('independent cleanup with unknown question',()=>h.client.prepareDeletion(source.id));
      assert.equal(s.error,null);assert.equal(s.deletion.hasUnresolvedQuestion,true);
      assert.equal(s.deletion.uncertain,false,'provider uncertainty is not deletion acknowledgement uncertainty');
      const operationId=s.deletion.id;
      s=await h.checked('confirm cleanup without resolving unknown question',()=>h.client.confirmDeletion(s.deletion.confirmationHandle));
      assert.equal(s.error,null);s=await observeResult(h,operationId,'deleted');checkDeletion(s,source,'deleted');
      assert.equal(s.uncertain,true,'exact cleanup result preserves the separate unknown question hold');
      assert.equal(s.deletions.find(d=>d.id===operationId).uncertain,false);
      assert.equal(s.requests.find(r=>r.requestId===requestId).state,'unknown');assert.equal(s.uploads.length,0);
    }else{
      // The earlier receipt must not retire a different current draft's rows.
      s=await h.checked('select a distinct later private source',()=>h.client.addFiles([
        new File(['%PDF-1.4\nFictional later source.\n%%EOF'],'fictional-later.pdf',{type:'application/pdf'})]));
      assert.equal(s.error,null);assert.equal(s.uploads.length,1);assert.notEqual(s.uploads[0].id,localId);
      assert.notEqual(s.draft.id,draftId);assert.notEqual(s.uploads[0].fileId,fileId);
      const laterId=s.uploads[0].id,laterFileId=s.uploads[0].fileId;
      s=await h.checked('read older receipt with a newer private draft',()=>h.client.refresh());
      assert.equal(s.error,null);assert.equal(s.uploads.length,1);assert.equal(s.uploads[0].id,laterId);
      assert.equal(s.uploads[0].fileId,laterFileId);assert.equal(s.draft.files[0].id,laterFileId);
    }
  },{fixtureArgs:outcome==='unknown'?['--outcome','unknown']:[],providerCalls:question?1:0,executions:['send-intent','send']});
}
async function failedSiblingBlocksAdmission(factory) {
  await coupled(factory,async h=>{
    let s=await h.checked('ready plus failed and unprocessed selections',()=>h.client.addFiles([selected(),unreadable(),selected()]));
    assert.equal(s.error.code,'file_read_failed');assert.equal(s.draft.files.length,1);
    assert.deepEqual(s.uploads.map(u=>u.state),['ready','failed','failed']);
    assert.equal(s.uploads[1].fileId,null);assert.equal(s.uploads[2].fileId,null);
    assert.equal(s.uploads[2].error.code,'selection_not_uploaded');
    const [ready,failed,unprocessed]=s.uploads,draftId=s.draft.id;
    for(const removeId of [failed.id,unprocessed.id]){
      const start=h.calls.length;
      s=await h.checked('failed local sibling blocks Send',()=>h.client.send(''));
      assert.equal(s.error?.code,'selection_not_ready','unmapped selected sibling blocks admission before HTTP');
      assert.equal(h.calls.length,start,'blocked sibling never PATCHes, prepares or Sends');
      assert.equal(s.draft.id,draftId);assert.equal(s.requests.length,0);
      s=await h.checked('explicitly remove failed local sibling',()=>h.client.removeUpload(removeId));
      assert.equal(h.calls.length,start,'unmapped removal stays local');assert.equal(s.uploads[0].id,ready.id);
    }
    assert.equal(s.uploads.length,1);assert.equal(s.draft.files[0].id,ready.fileId);
    const boundaries=admissionObserver(h);
    s=await h.checked('admit selected file',()=>h.client.send(''));
    assert.equal(s.error,null);checkPrivateUntilAdmission(h,boundaries,ready.id,ready.fileId);
    assert.equal(s.uploads.length,0,'exact admitted selection leaves no stale private upload rows');
    assert.equal(s.requests.length,1);assert.equal(s.requests[0].state,'retained_local');assert.equal(s.requests[0].draftId,draftId);
  },{executions:['send-intent','send']});
}

async function wholeBatchPreflight(factory) {
  await coupled(factory,async h=>{
    let reads=0;
    class ObservedFile extends File {
      async arrayBuffer(){reads++;return super.arrayBuffer();}
    }
    const file=(name,bytes=32,type='application/pdf')=>{
      const content=new Uint8Array(bytes),words=new TextEncoder().encode('%PDF-1.4\nFictional preflight bytes.\n%%EOF');
      content.set(words.subarray(0,bytes));return new ObservedFile([content],name,{type});
    };
    const preserved=({revision,busy,error,reason,...value})=>value;
    const firstCalls=h.calls.length,firstChanges=h.changes.length,empty=preserved(h.initial);
    let s=await h.checked('first empty-scope whole-batch refusal',()=>h.client.addFiles([
      file('fictional-first.pdf'),file('fictional-first-refused.jpg',32,'image/jpeg')]));
    assert.equal(reads,0,'whole-batch preflight refuses before any File read');
    assert.equal(h.calls.length,firstCalls,'whole-batch preflight refuses before any HTTP fetch');
    const firstEmitted=h.changes.slice(firstChanges);assert(firstEmitted.length>=2);
    assert.equal(firstEmitted[0].s.busy,true);assert.equal(firstEmitted[0].s.error,null);
    for(const change of firstEmitted)assert.deepEqual(preserved(change.s),empty,'preflight preserves draft, selections and every non-error field');
    assert.equal(s.error?.code,'file_unavailable');assert.equal(s.complete,true);assert.equal(s.routing,'ordinary');
    assert.equal(s.busy,false);assert.equal(s.uncertain,false);assert.equal(s.conversation,null);assert.equal(s.draft,null);
    assert.equal(s.uploads.length,0);assert.equal(s.files.length,0);assert.equal(s.requests.length,0);assert.equal(s.lastSend,null);
    assert.deepEqual(s,h.client.snapshot(),'first refusal returns exact current complete ordinary-empty proof');
    assert.deepEqual(firstEmitted.at(-1).s,s);
    s=await h.checked('create existing private draft',()=>h.client.addFiles([selected()]));
    assert.equal(s.error,null);
    s=await h.checked('save exact existing private text',()=>h.client.editText('  Fictional private question\nΩ  '));
    assert.equal(s.error,null);assert.equal(s.draft.readOnly,false);assert.equal(s.draft.files.length,1);
    const limits=s.limits,existing=s.draft.files.reduce((sum,f)=>sum+f.bytes,0);
    assert(limits.fileBytes<=300000&&limits.draftBytes<=300000&&limits.fileCount<=50,'bounded actual qualification');
    assert(limits.fileCount>=3&&limits.draftBytes>existing+64,'fixture admits the bounded test batches');
    const aggregate=limits.draftBytes-existing-32+1;
    assert(aggregate>0&&aggregate<=limits.fileBytes,'aggregate refusal stays within each advertised file limit');
    const cases=[
      ['unsupported MIME',[file('fictional-first.pdf'),file('fictional-refused.jpg',32,'image/jpeg')],'file_unavailable'],
      ['over file bytes',[file('fictional-first.pdf'),file('fictional-too-large.pdf',limits.fileBytes+1)],'file_unavailable'],
      ['over file count',Array.from({length:limits.fileCount-s.draft.files.length+1},(_,i)=>file('fictional-count-'+i+'.pdf')),'file_limit'],
      ['over total bytes',[file('fictional-first.pdf'),file('fictional-total.pdf',aggregate)],'file_limit']
    ];
    // Revision/busy/error/reason describe this refused attempt. Every other
    // field, including the full draft/row metadata and authority-free identity,
    // must remain exactly as before through every published callback.
    for(const [label,batch,code] of cases){
      const before=h.client.snapshot(),unchanged=preserved(before),calls=h.calls.length,changes=h.changes.length,readCount=reads;
      s=await h.checked('whole-batch preflight '+label,()=>h.client.addFiles(batch));
      assert.equal(reads,readCount,'whole-batch preflight refuses before any File read');
      assert.equal(h.calls.length,calls,'whole-batch preflight refuses before any HTTP fetch');
      const emitted=h.changes.slice(changes);assert(emitted.length>=2,'real operation start and completion were observed');
      assert.equal(emitted[0].s.busy,true);assert.equal(emitted[0].s.error,null,'each new attempt clears the earlier error');
      for(const change of emitted)assert.deepEqual(preserved(change.s),unchanged,'preflight preserves draft, selections and every non-error field');
      assert.deepEqual(preserved(s),unchanged,'preflight preserves draft, selections and every non-error field');
      assert.equal(s.error?.code,code);assert.equal(s.busy,false);assert.equal(s.uncertain,false);
      assert.deepEqual(s,h.client.snapshot(),'completed refusal is the exact current client snapshot');
      assert.equal(emitted.at(-1).s.revision,s.revision);assert.deepEqual(emitted.at(-1).s,s);
    }
    assert.equal(reads,0);assert.equal(s.draft.text,'  Fictional private question\nΩ  ');
  });
}

function answerFacts(answer) {
  assert(answer);return {...answer,citations:answer.citations.map(({previewHandle,...citation})=>citation)};
}
function chronological(requests) {
  // Equal timestamps have only the documented deterministic ID tie-break;
  // neither arrival order nor a durable append ordinal is inferred here.
  for(let i=1;i<requests.length;i++){
    const before=requests[i-1],after=requests[i];
    assert(before.createdAt<after.createdAt||before.createdAt===after.createdAt&&before.requestId.localeCompare(after.requestId)<0);
  }
}
async function immutablePresentationFacts(factory) {
  for(const outcome of ['answered','answered-no-citations']){
    // Keep the exact issued owner-action prefix even when a causal mutant fails
    // before a later step. Cleanup must not mask that intended semantic failure
    // with an expectation for a question the scenario never reached.
    const expectedExecutions=[];let expectedProviderCalls=0;
    const admission=()=>expectedExecutions.push('send-intent','send');
    const question=()=>{expectedExecutions.push('question-intent','ask');expectedProviderCalls++;};
    await coupled(factory,async h=>{
    let s=await h.checked('upload first same-named original',()=>h.client.addFiles([
      new File(['%PDF-1.4\nFictional first original.\n%%EOF'],'fictional-same-name.pdf',{type:'application/pdf'})]));
    assert.equal(s.error,null);const f1=s.uploads[0].fileId;
    admission();s=await h.checked('blank admission of first original',()=>h.client.send(''));
    assert.equal(s.error,null);const r1=s.lastSend.requestId;
    assert.equal(s.requests.find(r=>r.requestId===r1).state,'retained_local');
    assert.equal(s.files.find(f=>f.id===f1).admissionRequestId,r1);
    question();s=await h.checked('first explicit governed question',()=>h.client.send('Read the first fictional original.',{keptFiles:h.client.snapshot().files.filter(f=>f.available)}));
    assert.equal(s.error,null);const q1=s.lastSend.requestId,a1=s.answers.find(a=>a.requestId===q1);
    assert.equal(a1.state,'available');assert.deepEqual(a1.suppliedFileIds,[f1]);
    if(outcome==='answered')assert.deepEqual(new Set(a1.citations.map(c=>c.fileId)),new Set([f1]));
    else assert.deepEqual(a1.citations,[],'an uncited answer still has its complete supplied roster');
    const originalAnswer=answerFacts(a1),first=s.files.find(f=>f.id===f1);
    assert.equal(first.admissionRequestId,r1);assert.equal(first.officerDelivery.requestId,q1);
    s=await h.checked('upload distinct later same-named original',()=>h.client.addFiles([
      new File(['%PDF-1.4\nFictional second original with different bytes and length.\n%%EOF'],'fictional-same-name.pdf',{type:'application/pdf'})]));
    assert.equal(s.error,null);const f2=s.uploads[0].fileId;assert.notEqual(f2,f1);
    admission();s=await h.checked('blank admission of later original',()=>h.client.send(''));
    assert.equal(s.error,null);const r3=s.lastSend.requestId;
    const sources=new Map(s.files.map(f=>[f.id,f]));assert.equal(sources.size,2);
    assert.equal(sources.get(f1).name,sources.get(f2).name);assert.notEqual(sources.get(f1).bytes,sources.get(f2).bytes);
    assert.equal(sources.get(f1).admissionRequestId,r1);assert.equal(sources.get(f2).admissionRequestId,r3);
    assert.deepEqual(answerFacts(s.answers.find(a=>a.requestId===q1)),originalAnswer,'earlier answer keeps its frozen supplied roster');
    question();s=await h.checked('second explicit governed question',()=>h.client.send('Read both fictional originals.',{keptFiles:h.client.snapshot().files.filter(f=>f.available)}));
    assert.equal(s.error,null);const q2=s.lastSend.requestId,a2=s.answers.find(a=>a.requestId===q2);
    assert.equal(a2.state,'available');assert.equal(a2.suppliedFileIds.length,2);
    assert.deepEqual(new Set(a2.suppliedFileIds),new Set([f1,f2]));
    assert(a2.citations.every(c=>a2.suppliedFileIds.includes(c.fileId)));
    if(outcome==='answered'){
      assert(a2.citations.length>0);assert(a2.suppliedFileIds.some(id=>!a2.citations.some(c=>c.fileId===id)),
        'the actual PDF stand-in leaves a supplied original uncited');
    }else assert.deepEqual(a2.citations,[]);
    assert.deepEqual(answerFacts(s.answers.find(a=>a.requestId===q1)),originalAnswer,'earlier answer keeps its frozen supplied roster');
    assert.equal(s.files.find(f=>f.id===f1).admissionRequestId,r1);assert.equal(s.files.find(f=>f.id===f2).admissionRequestId,r3);
    const witness=s.files.find(f=>f.id===f1).officerDelivery.requestId;
    assert([q1,q2].includes(witness),'a confirmed supply witness is separate from original admission');
    assert.notEqual(witness,r1);assert.equal(s.files.find(f=>f.id===f2).officerDelivery.requestId,q2);
    const expectedRequests=[r1,q1,r3,q2];chronological(s.requests);
    assert.deepEqual(s.requests.map(r=>r.requestId),expectedRequests,'advancing fixture times give actual chronological order');
    const reads=h.calls.length;s=await h.checked('restore immutable presentation facts',()=>h.client.refresh());
    assert(h.calls.slice(reads).every(c=>c.method==='GET'));chronological(s.requests);
    assert.deepEqual(s.requests.map(r=>r.requestId),expectedRequests);
    assert.deepEqual(answerFacts(s.answers.find(a=>a.requestId===q1)),originalAnswer,'earlier answer keeps its frozen supplied roster');
    const later=s.management.files.find(f=>f.id===f2);
    s=await h.checked('prepare deletion of later original only',()=>h.client.prepareDeletion(later.id));assert.equal(s.error,null);
    const operationId=s.deletion.id;
    s=await h.checked('confirm deletion of later original only',()=>{
      const pending=h.client.confirmDeletion(s.deletion.confirmationHandle),immediate=h.client.snapshot();
      assert.deepEqual(immediate.answers,[],'issued confirmation temporarily withdraws all governed publication');return pending;
    });
    assert.equal(s.error,null);s=await observeResult(h,operationId,'deleted');checkDeletion(s,later,'deleted');
    assert.deepEqual(answerFacts(s.answers.find(a=>a.requestId===q1)),originalAnswer,
      'terminal deletion restores an earlier answer whose exact supplied original remains available');
    const revoked=s.answers.find(a=>a.requestId===q2);assert.equal(revoked.state,'source_unavailable');
    assert.equal(revoked.text,null);assert.deepEqual(revoked.suppliedFileIds,[]);assert.deepEqual(revoked.citations,[]);
    assert.equal(s.files.find(f=>f.id===f1).available,true);assert.equal(s.files.find(f=>f.id===f2).available,false);
    assert.equal(s.files.find(f=>f.id===f1).admissionRequestId,r1);assert.equal(s.files.find(f=>f.id===f2).admissionRequestId,r3);
    },{fixtureArgs:['--outcome',outcome],providerCalls:()=>expectedProviderCalls,executions:()=>expectedExecutions});
  }
}

async function markerOnlyPresentation(factory,mode='image') {
  const seeded=mode!=='image';
  const options={fixtureArgs:['--outcome','answered-marker-only',...(seeded?['--seed-file']:[])],providerCalls:1,
    executions:[...(mode!=='no-image'?['send-intent','send']:[]),'question-intent','ask'],
    ...(seeded?{checkInitial:seededOptions().checkInitial}:{})};
  await coupled(factory,async h=>{
    let s=h.initial,imageId=null;
    if(mode!=='no-image'){
      s=await h.checked('select actual fictional PNG',()=>h.client.addFiles([selectedImage()]));
      assert.equal(s.error,null);imageId=s.uploads[0].fileId;
      s=await h.checked('retain actual fictional PNG without asking',()=>h.client.send(''));assert.equal(s.error,null);
    }
    const originals=s.files.map(f=>f.id);
    s=await h.checked('ask raw marker-only fixture explicitly',()=>h.client.send('Describe the fictional originals.',{keptFiles:h.client.snapshot().files.filter(f=>f.available)}));
    assert.equal(s.error,null,'actual marker-only display projection is accepted');
    const request=s.requests.find(r=>r.requestId===s.lastSend.requestId);assert(request);
    if(mode==='no-image'){
      assert.equal(request.state,'unknown');assert.equal(request.answerId,null);assert.equal(s.uncertain,true);
      assert.equal(s.answers.length,0,'an invalid marker response without an image creates no answer');
    }else{
      assert.equal(request.state,'answered');const answer=s.answers.find(a=>a.id===request.answerId);
      assert.equal(answer.state,'available');assert.equal(answer.text,'','typed image evidence supports citation-only available display');
      assert.equal(answer.sourceValidation,'current');assert.deepEqual(new Set(answer.suppliedFileIds),new Set(originals));
      assert.equal(answer.citations.filter(c=>c.fileId===imageId&&c.location.image===true).length,1);
      if(mode==='mixed')assert.equal(answer.citations.filter(c=>c.location.page===2).length,1);
      else assert.equal(answer.citations.length,1);
      assert(answer.citations.every(c=>answer.suppliedFileIds.includes(c.fileId)));
    }
    const reads=h.calls.length;s=await h.checked('GET restore marker-only outcome',()=>h.client.refresh());
    assert(h.calls.slice(reads).every(c=>c.method==='GET'));
    if(mode==='no-image'){assert.equal(s.answers.length,0);assert.equal(s.uncertain,true);}
    else {assert.equal(s.answers[0].text,'');assert(s.answers[0].citations.some(c=>c.location.image===true));}
  },options);
}

function vmFactory(source) {
  const module={exports:{}};
  // Real JSON cloning returns host-realm public DTOs, as the normally required
  // module does; a prototype-only cross-realm mismatch must not kill a mutant.
  vm.runInNewContext(source,{module,exports:module.exports,
    JSON,ArrayBuffer,Uint8Array,URLSearchParams,TextDecoder,TextEncoder,setTimeout,clearTimeout,AbortController},
    {filename:'officer-documents-client.routing-mutant.js'});
  return module.exports.createOfficerDocumentsClient;
}
function mutatedFactory(before,after) {
  assert.notEqual(before,after);assert.equal(clientSource.split(before).length,2,'causal mutation has one actual target');
  return vmFactory(clientSource.replace(before,after));
}
async function killed(label,factory,scenario,expected) {
  await assert.rejects(()=>scenario(factory),expected,'causal mutation survived: '+label);
  console.log('ok causal '+label);
}

async function run() {
  await firstUpload(createOfficerDocumentsClient);console.log('ok actual first-upload callbacks and final DTO');
  await readFailure(createOfficerDocumentsClient);console.log('ok actual File-read failure / GET refresh / local removal');
  await lostCreate(createOfficerDocumentsClient,false);console.log('ok unacknowledged create / authoritative empty GET / no retry');
  await lostCreate(createOfficerDocumentsClient,true);console.log('ok committed lost create / exact discovered identity / no retry');
  await deletionLifecycle(createOfficerDocumentsClient);console.log('ok actual deletion confirmation / Close / stale handle / verified result');
  await deletionLifecycle(createOfficerDocumentsClient,true);console.log('ok actual management-only null-model cleanup');
  await deletionLifecycle(createOfficerDocumentsClient,false,'changed');console.log('ok actual changed-identity deletion result');
  await deletionLifecycle(createOfficerDocumentsClient,false,'unknown');console.log('ok actual unknown physical deletion result');
  await firstRecovery(createOfficerDocumentsClient);console.log('ok actual incomplete management / explicit First / independent cleanup');
  await publicationBarrier(createOfficerDocumentsClient);console.log('ok actual pending cleanup / synchronous publication withdrawal / recorded completion');
  await publicationBarrier(createOfficerDocumentsClient,true);console.log('ok actual lost confirmation / fresh uncertain handle / lifecycle barrier / explicit same command');
  await admittedSelection(createOfficerDocumentsClient);console.log('ok actual blank keep retires only admitted selections, later private draft survives');
  await admittedSelection(createOfficerDocumentsClient,'answered');console.log('ok actual question admission retires selections after exact consumption');
  await admittedSelection(createOfficerDocumentsClient,'unknown');console.log('ok actual unknown admission retires selections and preserves no-resend hold');
  await failedSiblingBlocksAdmission(createOfficerDocumentsClient);console.log('ok actual failed/unprocessed siblings block admission until explicit Remove');
  await wholeBatchPreflight(createOfficerDocumentsClient);console.log('ok actual whole-batch MIME/file/count/total preflight preserves existing private draft without reads or fetch');
  await singleFilePastFifty(createOfficerDocumentsClient);console.log('ok actual one-file question beyond the old 50-file roster');
  await immutablePresentationFacts(createOfficerDocumentsClient);console.log('ok actual immutable origins and answer rosters, cited/uncited variants, later-source deletion restores only supported history');
  await markerOnlyPresentation(createOfficerDocumentsClient);console.log('ok actual raw marker-only image yields available empty text and typed citation');
  await markerOnlyPresentation(createOfficerDocumentsClient,'mixed');console.log('ok actual raw marker-only mixed PDF/image retains both typed citations');
  await markerOnlyPresentation(createOfficerDocumentsClient,'no-image');console.log('ok actual raw marker-only outcome without an image remains unknown without an answer');
  // Each mutated scenario must first pass with identical VM loading and real
  // HTTP. This prevents realm/dependency failures from masquerading as a kill.
  const control=vmFactory(clientSource);
  await firstUpload(control);await readFailure(control);await lostCreate(control,true);
  await deletionLifecycle(control,true);await publicationBarrier(control);await publicationBarrier(control,true);
  await admittedSelection(control);await failedSiblingBlocksAdmission(control);
  await wholeBatchPreflight(control);
  await immutablePresentationFacts(control);await markerOnlyPresentation(control);
  console.log('ok 12 unmodified VM factory scenario controls on the exact causal scenarios');
  await killed('reading falls through ordinary routing',mutatedFactory(
    "state.uploads.push(...selections);if(!state.conversation)state.routing='checking';emit();guard(t);",
    'state.uploads.push(...selections);emit();guard(t);'),firstUpload,{code:'PAGE_SNAPSHOT_REJECTED'});
  await killed('empty GET loses local upload ownership',mutatedFactory(
    "state.routing=state.uploads.length||state.draft||state.uncertain?'blocked':'ordinary';",
    "state.routing='ordinary';"),readFailure,{code:'PAGE_SNAPSHOT_REJECTED'});
  await killed('removal drops unresolved create ownership',mutatedFactory(
    "if(!state.conversation&&!state.draft&&!state.uploads.length&&!state.uncertain&&!unresolved.size&&state.complete)state.routing='ordinary';",
    "if(!state.uploads.length)state.routing='ordinary';"),factory=>lostCreate(factory,true),{code:'PAGE_SNAPSHOT_REJECTED'});
  await killed('cleanup wrongly requires reading qualification',mutatedFactory(
    'function requireManagement() { if(!state.conversation||!state.management.available||!state.management.complete)',
    'function requireManagement() { if(!state.available||!state.complete||state.uncertain||!state.conversation||!state.management.available||!state.management.complete)'),
    factory=>deletionLifecycle(factory,true),/cleanup remains independently authorized/);
  await killed('confirmation fails to withdraw publication before HTTP',mutatedFactory(
    'syncUncertainty();withdrawPublication();guard(t);t.publication=publication;',
    'syncUncertainty();guard(t);t.publication=publication;'),publicationBarrier,/confirmation synchronously withdraws governed publication/);
  await killed('same prepared generation republishes old answer',mutatedFactory(
    'for(const r of history) if(!confirmationUnresolved()&&r.answerId)',
    'for(const r of history) if(r.answerId)'),factory=>publicationBarrier(factory,true),/same prepared generation cannot republish governed answers/);
  await killed('admitted selection remains private',mutatedFactory(
    'function retireAdmittedSelection(requestId,view,retainedIds=null) {',
    'function retireAdmittedSelection(requestId,view,retainedIds=null) { return;'),admittedSelection,/exact admitted selection leaves no stale private upload rows/);
  await killed('text save prematurely retires private selection',mutatedFactory(
    'state.lastSend.retained=true;emit();guard(t);const original=',
    'state.lastSend.retained=true;state.uploads=[];emit();guard(t);const original='),admittedSelection,/selected rows stay private through PATCH and prepared intent/);
  await killed('failed sibling is silently omitted by Send',mutatedFactory(
    'emit();guard(t);requireCompleteSelection();',
    'emit();guard(t);'),failedSiblingBlocksAdmission,/unmapped selected sibling blocks admission before HTTP/);
  await killed('MIME preflight gate permits processing a refused whole batch',mutatedFactory(
    '!state.formats.includes(f.type)||',
    ''),wholeBatchPreflight,/whole-batch preflight refuses before any File read/);
  // Move the actual selection-creation code ahead of the actual metadata loop;
  // no invented row schema or substituted limits are needed for this mutant.
  const validationStart=clientSource.indexOf('      let total=existing.reduce('),rowsStart=clientSource.indexOf('      const selections=files.map(',validationStart),rowsEnd=clientSource.indexOf('\n',rowsStart)+1;
  assert(validationStart>=0&&rowsStart>validationStart&&rowsEnd>rowsStart,'actual preflight/row creation boundary');
  const metadata=clientSource.slice(validationStart,rowsStart),rows=clientSource.slice(rowsStart,rowsEnd);
  await killed('refused batch publishes private rows before validation',mutatedFactory(metadata+rows,rows+metadata),
    wholeBatchPreflight,/preflight preserves draft, selections and every non-error field/);
  await killed('later delivery witness replaces original admission',mutatedFactory(
    'admissionRequestId:f.admissionRequestId',
    'admissionRequestId:f.officerDelivery?.requestId??null'),immutablePresentationFacts,{code:'PAGE_SNAPSHOT_REJECTED'});
  await killed('current sources replace a frozen earlier answer roster',mutatedFactory(
    'suppliedFileIds:[...value.suppliedFileIds]',
    'suppliedFileIds:state.files.filter(f=>f.available).map(f=>f.id)'),immutablePresentationFacts,/earlier answer keeps its frozen supplied roster/);
  await killed('empty display with genuine image citation is rejected',mutatedFactory(
    "!str(value.text,20000)||value.sourceValidation!=='current'",
    "!str(value.text,20000)||!value.text.trim()||value.sourceValidation!=='current'"),markerOnlyPresentation,/actual marker-only display projection is accepted/);
  console.log('Motet page/client actual HTTP: 21 flows + 12 unmodified VM scenario controls / 14 causal mutants passed; every callback and final DTO validated, only explicit fictional calls, 0 external calls.');
}
// The aggregate requires CPU-heavy suites whose synchronous/microtask work can
// delay stdout delivery. Start the disposable listener after that import turn,
// so its real startup deadline measures startup rather than blocked parent IO.
setImmediate(()=>{run().catch(e=>{console.error(e);process.exitCode=1;});});
