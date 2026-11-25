#!/usr/bin/env node
/* Fixed browser transport faults; native requests are restricted to the owned loopback fixture. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');
const {spawn} = require('node:child_process');
const {webcrypto} = require('node:crypto');
const {install, MODES, UPLOAD_PAUSE_MS} = require('./fixtures/officer_documents/browser_faults.js');
const {createOfficerDocumentsClient} = require('../src/web/officer-documents-client.js');
// The shared start and stop: python3, or on Windows python stopped through its standard input, with Windows' longer
// startup and cleanup limits.
const {PYTHON, stopFixture, WINDOWS, STARTUP_MS, CLEANUP_MS} = require('./officer_document_fixture_process.js');
const KEY = 'fictional-officer-browser-fixture-key';
const ID = '1'.repeat(32), OTHER = '2'.repeat(32);
const confirmPath = '/api/attachments/deletions/' + ID + '/confirm';
const intentPath = '/api/attachments/conversations/' + ID + '/question-intent';
const uploadPath = '/api/attachments/drafts/' + ID + '/uploads/' + OTHER;
const START = '/* BEGIN OFFICER OFFLINE FIXTURE INSTRUMENTATION */\n';
const END = '/* END OFFICER OFFLINE FIXTURE INSTRUMENTATION */\n';
const tests = [];
const liveChildren = new Set();
const test = (name, run) => tests.push({name, run});
const served = (name, run) => tests.push({name, run});      // against the actual offline fixture
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; });return {promise, resolve}; }
function target(fetch) {
  const jobs = [], events = new Map(), entered = deferred();
  return {location:{origin:'http://127.0.0.1:8799'},fetch,jobs,events,entered,
    setTimeout(fn, ms) { const job = {fn,ms,cleared:false};jobs.push(job);entered.resolve();return job; },
    clearTimeout(job) { if(job)job.cleared=true; },
    addEventListener(name, fn) { events.set(name, fn); }};
}
const result = () => new Response('{"fictional":true}', {status:200});

test('default and invalid configuration never install an unintended wrapper', async () => {
  const original = async () => result(), t = target(original);
  install(t, 'none');assert.equal(t.fetch, original);
  assert.throws(() => install(t, 'invented'), /Unknown/);assert.equal(t.fetch, original);
  t.location.origin='https://fictional.invalid';
  assert.throws(() => install(t, MODES[1]), /loopback/);assert.equal(t.fetch, original);
});
test('one-shot before-confirm failure matches only its exact origin, path and method', async () => {
  const calls=[], t=target(async (...args) => {calls.push(args);return result();});
  install(t, 'confirm-before-send-once');
  for(const [url, init] of [[confirmPath,{method:'GET'}],['http://fictional.invalid'+confirmPath,{method:'POST'}],
    [confirmPath+'?extra=1',{method:'POST'}],[confirmPath+'/extra',{method:'POST'}]])await t.fetch(url,init);
  assert.equal(calls.length,4);
  const init={method:'POST',body:'{"fingerprint":"fictional"}',headers:{'X-Live-Room-Key':KEY},signal:new AbortController().signal};
  await assert.rejects(t.fetch(confirmPath,init),TypeError);assert.equal(calls.length,4);
  await t.fetch(confirmPath,init);assert.equal(calls.length,5);assert.equal(calls[4][1],init);
});
test('after-intent failure waits for a real successful response and does not rewrite it', async () => {
  let requests=0,drained=0;
  const response={ok:true,clone:()=>({arrayBuffer:async()=>{drained++;return new ArrayBuffer(0);}})};
  const t=target(async()=>{requests++;return response;});install(t,'question-intent-after-response-once');
  await assert.rejects(t.fetch(intentPath,{method:'POST'}),TypeError);
  assert.equal(requests,1);assert.equal(drained,1);
  assert.equal(await t.fetch(intentPath,{method:'POST'}),response);assert.equal(requests,2);
  const refused={ok:false}, second=target(async()=>refused);install(second,'question-intent-after-response-once');
  assert.equal(await second.fetch(intentPath,{method:'POST'}),refused);
});
test('upload pause forwards the original request once only after its bounded timer', async () => {
  const calls=[],t=target(async(...args)=>{calls.push(args);return result();});install(t,'upload-pause-once');
  const init={method:'PUT',body:new Uint8Array([1,2]).buffer,signal:new AbortController().signal};
  const pending=t.fetch(uploadPath,init);await t.entered.promise;
  assert.equal(calls.length,0);assert.equal(t.jobs[0].ms,8000);assert.equal(UPLOAD_PAUSE_MS,8000);
  t.jobs[0].fn();await pending;assert.equal(calls.length,1);assert.equal(calls[0][1],init);assert(t.jobs[0].cleared);
  await t.fetch(uploadPath,init);assert.equal(t.jobs.length,1);assert.equal(calls.length,2);
});
test('upload pause cleans timers on signal abort, already-aborted input and page hide', async () => {
  for(const mode of ['signal','already','pagehide']){
    let calls=0;const t=target(async()=>{calls++;return result();}),controller=new AbortController();
    install(t,'upload-pause-once');if(mode==='already')controller.abort();
    const pending=t.fetch(uploadPath,{method:'PUT',signal:controller.signal});
    if(mode!=='already'){await t.entered.promise;if(mode==='signal')controller.abort();else t.events.get('pagehide')();}
    await assert.rejects(pending,e=>e.name==='AbortError');assert.equal(calls,0);assert(t.jobs.every(j=>j.cleared));
  }
});
test('abort or page hide after timer resolution still cannot forward the paused request', async () => {
  for(const mode of ['signal','pagehide']){
    let calls=0;const t=target(async()=>{calls++;return result();}),controller=new AbortController();
    install(t,'upload-pause-once');const pending=t.fetch(uploadPath,{method:'PUT',signal:controller.signal});
    await t.entered.promise;t.jobs[0].fn();
    if(mode==='signal')controller.abort();else t.events.get('pagehide')();
    await assert.rejects(pending,e=>e.name==='AbortError');assert.equal(calls,0);assert(t.jobs[0].cleared);
  }
});
test('upload network loss rejects before any receiver call and is consumed once', async () => {
  let calls=0;const t=target(async()=>{calls++;return result();});install(t,'upload-network-loss-once');
  await assert.rejects(t.fetch(uploadPath,{method:'PUT'}),TypeError);assert.equal(calls,0);
  await t.fetch(uploadPath,{method:'PUT'});assert.equal(calls,1);
});

function ownedFetch(origin) {
  return (input, init={}) => new Promise((resolve,reject) => {
    const url=new URL(input,origin);
    if(url.origin!==origin||url.protocol!=='http:'||url.hostname!=='127.0.0.1')return reject(Error('Only the owned loopback fixture may be contacted'));
    const body=init.body===undefined?null:Buffer.from(typeof init.body==='string'?init.body:new Uint8Array(init.body));
    const headers={...(init.headers||{}),...(body?{'Content-Length':String(body.length)}:{})};
    const req=http.request(url,{method:init.method||'GET',headers,signal:init.signal},response=>{
      let size=0;const chunks=[];
      response.on('data',chunk=>{size+=chunk.length;if(size>2000000)req.destroy(Error('Fixture response bound'));else chunks.push(chunk);});
      response.on('error',reject);
      response.on('end',()=>resolve(new Response(Buffer.concat(chunks),{status:response.statusCode,headers:response.headers})));
    });
    req.on('error',reject);req.end(body);
  });
}
async function fixture(mode, flags, run) {
  const child=spawn(PYTHON,[path.join(__dirname,'officer_document_live_fixture.py'),'--browser-fault',mode,...flags,
    ...(WINDOWS?['--stop-on-stdin']:[])],
    {env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:[WINDOWS?'pipe':'ignore','pipe','pipe']});
  child.stdin?.on('error',()=>{});      // a fixture that already exited gives EPIPE: its close is what counts
  liveChildren.add(child);
  // 'close' (not 'exit') follows the child's last output, so the closing counters line has arrived by then.
  const closed=new Promise(resolve=>child.once('close',(code,signal)=>{liveChildren.delete(child);resolve({code,signal});}));
  let output='',errors='',client;
  child.stderr.on('data',b=>{errors=(errors+b).slice(-10000);});
  try {
    const origin=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Offline fixture startup timeout')),STARTUP_MS);
      child.stdout.on('data',b=>{output+=b;const m=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});
      child.once('error',error=>{clearTimeout(timer);reject(error);});
      child.once('exit',code=>{clearTimeout(timer);reject(Error('Offline fixture exited '+code+' '+errors));});
    });
    const native=ownedFetch(origin),html=await(await native('/')).text();
    assert.equal(html.split(START).length,2);assert.equal(html.split(END).length,2);
    const script=html.slice(html.indexOf(START)+START.length,html.indexOf(END));
    const calls=[], t=target(async(...args)=>{calls.push({url:String(args[0]),method:args[1]?.method||'GET'});return native(...args);});
    t.location.origin=origin;vm.runInNewContext(script,{globalThis:t,URL,TypeError,DOMException});
    const info=async()=> (await native('/fixture/metadata',{headers:{'X-Live-Room-Key':KEY}})).json();
    assert.equal((await info()).browserFault,mode);
    client=createOfficerDocumentsClient({enabled:true,demo:false,auth:()=>KEY,crypto:webcrypto,AbortController,fetch:t.fetch});
    const s=await client.connect({scopeId:'room',modelId:'claude-haiku-5-5'});assert.equal(s.error,null,JSON.stringify(s.error));
    await run({client,s,t,calls,info,native});
    const stats=await info();assert.equal(stats.fakeProviderCalls,0);assert.equal(stats.ordinaryAskBlocked,0);assert.equal(stats.outboundBlocked,0);
  } finally {
    client?.dispose();
    if(child.exitCode===null&&child.signalCode===null)stopFixture(child);
    let timer,ended;
    try { ended=await Promise.race([closed,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Fixture shutdown timeout')),Math.max(10000,CLEANUP_MS));})]); }
    catch(error){child.kill('SIGKILL');await closed;throw error;}
    finally{clearTimeout(timer);}
    assert.equal(ended.code,0,errors);
    const stats=JSON.parse(output.trim().split('\n').at(-1));
    assert.deepEqual(stats,{fakeProviderCalls:0,ordinaryAskBlocked:0,outboundBlocked:0});
  }
}
served('served after-intent instrumentation creates an actual recoverable unstarted request', async()=>fixture('question-intent-after-response-once',['--seed-file'],async({client,calls})=>{
  let s=await client.send('Fictional question with a lost preparation response.',
    {keptFiles:client.snapshot().files.filter(f=>f.available)});assert(s.uncertain);
  assert.equal(calls.some(c=>/\/requests\/[a-f0-9]{32}\/ask$/.test(c.url)),false);
  s=await client.refresh();const prepared=s.requests.find(r=>r.canAbandon);assert(prepared);assert.equal(prepared.state,'prepared');
  s=await client.abandon(prepared.requestId);assert.equal(s.error,null);assert(s.requests.find(r=>r.requestId===prepared.requestId).abandoned);
}));
served('served before-confirm instrumentation preserves the exact prepared selection until explicit repeat', async()=>fixture('confirm-before-send-once',['--seed-file'],async({client,calls})=>{
  let s=await client.prepareDeletion(client.snapshot().management.files[0].id);const old=s.deletion;
  s=await client.confirmDeletion(old.confirmationHandle);assert(s.uncertain);assert.equal(calls.some(c=>c.url.endsWith('/confirm')),false);
  s=await client.refresh();assert.equal(s.deletions[0].state,'confirm');assert.equal(s.deletions[0].revision,old.revision);assert(s.uncertain);
  s=await client.openDeletion(s.deletions[0].operationHandle);assert(s.deletion.uncertain);assert.notEqual(s.deletion.confirmationHandle,old.confirmationHandle);
  s=await client.confirmDeletion(s.deletion.confirmationHandle);assert.equal(s.error,null);assert(s.deletion.sourceUnavailable);
  assert.equal(calls.filter(c=>c.url.endsWith('/confirm')).length,1);
}));
served('served upload pause exposes genuine local uploading before forwarding one PUT', async()=>fixture('upload-pause-once',[],async({client,t,calls})=>{
  const pending=client.addFiles([new File(['%PDF-1.4\nFictional paused upload.'],'fictional-pause.pdf',{type:'application/pdf'})]);
  await t.entered.promise;const s=client.snapshot();assert(s.busy);assert.equal(s.uploads[0].state,'uploading');assert.equal(s.uploads[0].progress,null);
  assert.equal(calls.some(c=>c.method==='PUT'),false);assert.equal(t.jobs[0].ms,8000);t.jobs[0].fn();
  const done=await pending;assert.equal(done.error,null,JSON.stringify(done.error));assert.equal(done.uploads[0].state,'ready');
  assert.equal(calls.filter(c=>c.method==='PUT').length,1);
}));
served('served upload network loss leaves reservation evidence without inventing partial server bytes', async()=>fixture('upload-network-loss-once',[],async({client,calls,info})=>{
  let s=await client.addFiles([new File(['%PDF-1.4\nFictional unsent upload.'],'fictional-network.pdf',{type:'application/pdf'})]);
  assert(s.uncertain);assert.equal(s.uploads[0].state,'failed');assert(s.uploads[0].fileId);
  assert.equal(calls.some(c=>c.method==='PUT'),false);s=await client.refresh();
  assert.equal(s.draft.files[0].state,'uploading');
  const facts=await info();assert.equal(facts.fixtureFaults.interruptedUploads,0);assert.equal(facts.fixtureFaults.uploadBytesBeforeInterruption,0);
}));
(async()=>{
  let completed=0,skipped=0;
  for(const {name,run,skip} of tests){
    if(skip){skipped++;console.log('skip '+name);continue;}
    // A served check starts the fixture: allow Windows' longer startup and cleanup on top of the check itself.
    const limit=WINDOWS?STARTUP_MS+CLEANUP_MS+30000:15000;
    let timer;
    try { await Promise.race([run(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Check timed out: '+name)),limit);})]); }
    finally { clearTimeout(timer); }
    completed++;console.log('ok '+name);
  }
  assert.equal(completed+skipped,tests.length);console.log(completed+' offline browser fault checks passed'+(skipped?', '+skipped+' skipped':''));
})().catch(error=>{
  for(const child of liveChildren)child.kill('SIGINT');
  console.error(error);process.exitCode=1;
});
