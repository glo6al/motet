'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {createNativeAgentAttachmentsPage, stageOneSnapshot} = require('../src/web/native-agent-attachments-page.js');

const id = '1'.repeat(32);
const destination = 'fictional-ada';
const saved = {id, revision:2, writerGeneration:1, createdAt:1, updatedAt:2, readOnly:false};
function stage(revision, extra = {}) {
  return {schemaVersion:1, fixture:false, revision, context:{kind:'agent', destinationId:destination,
    scopeId:'fictional-scope', modelId:null, conversationId:null},
  features:{drafts:true, send:false, files:false, deletion:false},
  qualification:{state:'available', reason:null, capabilityRevision:'fictional-v1',
    limits:{fileBytes:2048,draftBytes:4096,fileCount:3,storageBytes:8192,agentTextCharacters:2000},
    formats:['application/pdf']},
  draftRead:'complete', busy:false, uncertain:false, error:null,
  draft:{...saved, text:'Saved words', files:[]}, unsavedEdit:null,
  savedDrafts:[], uploads:[], attempts:[], requests:[],
  files:{state:'unavailable',associationCount:null,unclassifiedAssociationCount:null,first:null,previous:null,next:null,items:[]},
  detail:null, deletion:null, ...extra};
}
class Element {
  constructor(value = '', attrs = {}) { this.value = ''; this.words = value; this.attrs = attrs; this.children = []; this.events = {}; }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  insertBefore(node) { this.append(node); }
  before(node) { this.parentNode.append(node); }
  after(node) { this.before(node); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); this.parentNode = null; }
  contains(node) { return node === this || this.children.some(n => n.contains(node)); }
  setAttribute(name, value) { this.attrs[name] = value; }
  getAttribute(name) { return this.attrs[name]; }
  addEventListener(name, fn) { this.events[name] = fn; }
  get isConnected() { return !!this.parentNode; }
  get textContent() { return this.words + this.children.map(n => n.textContent).join(''); }
  set textContent(value) { this.words = value; this.children = []; }
  all() { return [this, ...this.children.flatMap(n => n.all())]; }
  querySelector(selector) { return this.all().find(node => node.className === selector.slice(1)); }
}
async function tick() { for (let i=0;i<8;i++) await Promise.resolve(); }
async function harness(initial = stage(1), target = destination, stage2Fixture = false) {
  const top = new Element(), row = new Element(), input = new Element(), compose = new Element(), send = new Element();
  const log = new Element();
  top.append(log, compose, row); compose.append(input); row.append(send); input.value = 'Independent ordinary text';
  const classes = new Set(), tasks = new Map(), calls = [], paints = [];
  let changed, snapshot = initial, serial = 0, nearEnd = true, selected = {target, identity:{}, key:'fictional-owner'};
  function publish(value) { snapshot = value; changed(value); return value; }
  const p = createNativeAgentAttachmentsPage({
    input, send, row, compose, log, stage2Fixture,
    captureConversationScroll:()=>{paints.push('measure');return {near:nearEnd};},
    repaintConversation:scroll=>{
      paints.push(['paint',scroll?.near,classes.has('workspace-attachments')]);
      for (const child of [...log.children]) child.remove();
      for (const item of p.conversationRows()) log.append(item.el);
    },
    doc:{documentElement:{classList:{add:(...names)=>names.forEach(name=>classes.add(name)),remove:name=>classes.delete(name)}},
      addEventListener(){},createTextNode: text=>new Element(text),activeElement:null},
    selection:()=>selected,sameRecipient:()=>!!selected,showControls:()=>true,allowDrop:()=>true,
    recipientName:()=> 'Fictional Ada', crypto:require('node:crypto').webcrypto,
    element:(_d,_tag,cls,text)=>{const el=new Element(text);el.className=cls;el.classList={add(){}};el.click=()=>calls.push(['picker']);return el;},
    button:(_,cls,label,text)=>{const el=new Element(text,{'aria-label':label});el.className=cls;return el;},icon:()=>new Element(),
    fileName:(_d,value)=>new Element(value),
    bytes:n=>n+' B',formatDate:()=> '1 Nov, 1:00 PM',attachmentStylesInUse:()=>false,
    restoreSend:()=>{send.disabled=false;},setHintHidden(){paints.push('layout');},
    setTimer:(fn,ms)=>{tasks.set(++serial,{fn,ms});return serial;},clearTimer:i=>tasks.delete(i),
    createClient:op=>{changed=op.changed;return {
      connect:async()=>snapshot, dispose(){},
      refresh:async()=>{calls.push(['refresh']);return snapshot;},
      createDraft:async()=>{calls.push(['createDraft']);return snapshot;},
      discardDraft:async()=>{calls.push(['discardDraft']);return publish({...snapshot,draft:null,revision:snapshot.revision+1});},
      editText:async text=>{calls.push(['editText',text]);return publish({...snapshot,
        draft:{...snapshot.draft,text},revision:snapshot.revision+1,unsavedEdit:null});},
      resume:async draftId=>{calls.push(['resume',draftId]);return publish({...snapshot,
        draft:{...snapshot.draft,readOnly:false},revision:snapshot.revision+1});},
      send:async text=>{calls.push(['send',text]);return publish({...snapshot,draft:null,revision:snapshot.revision+1});},
      checkRequest:async handle=>{calls.push(['checkRequest',handle]);return snapshot;},
      discardUnsavedEdit:async draftId=>{calls.push(['discardUnsavedEdit',draftId]);return publish({...snapshot,
        revision:snapshot.revision+1,unsavedEdit:null});}
    };}
  });
  p.sync(); await tick();
  return {p,input,send,calls,publish,log,paints,setNearEnd(value){nearEnd=value;},root:top,enter(text){input.value=text;input.events.input();},button(label){
    const el=top.all().find(n=>n.getAttribute('aria-label')===label);
    assert.ok(el && !el.disabled,'Available action: '+label);
    el.onclick();},advance(){for (const [number,item] of [...tasks]) if(item.ms===300&&tasks.delete(number))item.fn();},
  leave(){selected=null;p.sync();}};
}

test('reject a foreign recipient or later-stage authority and keep the ordinary draft separate', async () => {
  assert.equal(stageOneSnapshot(stage(1),destination),true);
  assert.equal(stageOneSnapshot(stage(1),'fictional-bram'),false);
  assert.equal(stageOneSnapshot(stage(1,{features:{drafts:true,send:true,files:false,deletion:false}}),destination),false);
  const h=await harness();
  h.enter('Private attachment draft');
  assert.equal(h.p.blocksSend(),true);
  h.leave();
  h.advance(); await tick();
  assert.deepEqual(h.calls,[]);
  assert.equal(h.input.value,'');
});

test('a temporarily busy read preserves a buffered edit, but Look again does not save it', async () => {
  const h=await harness();
  h.enter('Buffered');
  h.publish(stage(2,{draftRead:'checking',busy:true}));
  h.advance(); assert.deepEqual(h.calls,[]);
  h.publish(stage(3,{draftRead:'incomplete',error:{code:'incomplete',message:'Internal failure text'}}));
  h.button('Look again'); await tick();
  assert.deepEqual(h.calls,[['refresh']]);
  assert.equal(h.input.value,'Buffered');
  assert.equal(h.input.readOnly,true);
  h.publish(stage(4));
  h.button('Save it'); await tick();
  assert.deepEqual(h.calls,[['refresh'],['editText','Buffered']]);
});

test('a writer change requires explicit takeover and saved text clears both unsaved layers', async () => {
  const h=await harness();
  const unsavedEdit={draftId:id,text:'Previous unsaved words'};
  h.publish(stage(2,{unsavedEdit}));
  h.enter('Latest buffered words');
  h.publish(stage(3,{draft:{...saved,writerGeneration:2,readOnly:true,text:'Words saved by the current writer',files:[]},
    unsavedEdit,savedDrafts:[{...saved,writerGeneration:2,readOnly:true,fileCount:0,totalBytes:0}]}));
  h.advance(); assert.deepEqual(h.calls,[]);
  assert.equal(h.input.value,'Latest buffered words');
  assert.equal(h.input.readOnly,true);
  h.button('Resume draft here'); await tick();
  assert.deepEqual(h.calls,[['resume',id]]);
  assert.equal(h.input.value,'Latest buffered words');
  h.button('Use the saved text'); await tick();
  assert.deepEqual(h.calls,[['resume',id],['discardUnsavedEdit',id]]);
  assert.equal(h.input.value,'Words saved by the current writer');
});

test('opening the picker from saved drafts does not create an empty draft', async () => {
  const savedDraft = {...saved, readOnly:true, fileCount:0, totalBytes:0};
  const h=await harness(stage(1,{draft:null,savedDrafts:[savedDraft]}));
  h.button('Create new draft'); await tick();
  assert.deepEqual(h.calls,[['picker']]);
});

test('SQLite notice leaves ordinary words and Send usable only without retained work', async () => {
  const {createNativeAgentAttachmentsClient} = require('../src/web/native-agent-attachments-client.js');
  const expected = 'Sending files to agents is unavailable because this Python uses an older database library. Install a newer Python and use it to restart Motet.';
  const calls=[];
  const client=createNativeAgentAttachmentsClient({enabled:true,demo:false,auth:()=> 'fictional-owner',
    crypto:require('node:crypto').webcrypto,AbortController,fetch:async (path,init)=>{
      calls.push([path,init.method]);
      return new Response(JSON.stringify({schemaVersion:1,fixture:false,serverTime:'2030-01-01T00:00:00Z',
        error:{code:'sqlite_unavailable',message:'Untrusted wire text.',retryable:false}}),{status:503});
    }});
  const passive=await client.connect({destinationId:destination});
  const h=await harness(passive);
  assert(h.root.textContent.includes(expected));
  assert.equal(h.input.value,'Independent ordinary text');
  assert.equal(h.input.readOnly,undefined);
  assert.equal(h.send.disabled,false);
  assert.equal(h.p.ownsInput(),false);
  assert.equal(h.p.blocksSend(),false);
  assert.deepEqual(calls,[[`/api/attachments/native/selection?destinationId=${destination}`,'GET']]);
  for (const change of [s=>s.uncertain=true,s=>s.uploads=stage(1,{uploads:[{}]}).uploads,
    s=>s.attempts=[{id:'fictional',draftId:null,requestId:null,phase:'creating',submitted:null}],
    s=>s.busy=true]) {
    const held=JSON.parse(JSON.stringify(passive));change(held);
    const locked=await harness(held);
    assert.equal(locked.p.blocksSend(),true,'retained or unreadable work cannot become passive');
  }
  client.dispose();
});

test('discard needs an explicit confirmation for the displayed draft version', async () => {
  const h=await harness();
  h.button('Discard draft');
  h.button('Keep'); await tick();
  assert.deepEqual(h.calls,[]);
  h.button('Discard draft');
  h.publish(stage(2,{draft:{...saved,revision:3,text:'A newer edit',files:[]}}));
  assert.throws(()=>h.button('Discard'),/Available action/);
  h.button('Discard draft');
  h.button('Discard'); await tick();
  assert.deepEqual(h.calls,[['discardDraft']]);
});

for (const [prefix, provider] of [['c', 'Claude'], ['x', 'Codex']]) {
  test(`existing Send is captured once for a qualified completion-only ${provider} draft`, async () => {
    const target = prefix + ':8bf3f280-68ff-421f-84be-8b7cb3d62aaa';
    const initial = stage(1, {features:{drafts:true,send:true,files:false,deletion:false},
      draft:{...saved,text:'Saved words',files:[{id:'2'.repeat(32),name:'fictional.pdf',mediaType:'application/pdf',
        bytes:10,state:'ready',error:null,detailsHandle:'fictional-file',preparationKind:'original_relay',
        previewAvailable:false,expiresAt:null}]}});
    initial.context.destinationId = target;
    const h = await harness(initial,target);
    assert.equal(h.send.disabled,false);
    h.enter('Exact buffered owner instruction');
    assert.equal(h.p.blocksSend(),true);
    assert.equal(h.p.blocksSend(),true);
    h.advance();
    await tick();
    assert.deepEqual(h.calls,[['send','Exact buffered owner instruction']]);
    assert.equal(h.input.value,'Independent ordinary text');
    assert.equal(h.p.blocksSend(),false);
  });
}

const claude = 'c:8bf3f280-68ff-421f-84be-8b7cb3d62aaa';
const submittedFile = {fileId:'2'.repeat(32),name:'fictional-plan.pdf',mediaType:'application/pdf',bytes:700};
const fileDraft = {id,revision:3,writerGeneration:1,createdAt:1,updatedAt:2,readOnly:false,text:'Private draft',
  files:[{id:submittedFile.fileId,name:submittedFile.name,mediaType:submittedFile.mediaType,bytes:700,state:'ready',
    error:null,detailsHandle:'fictional-detail',preparationKind:'original_relay',previewAvailable:false,expiresAt:null}]};
function stage2(revision, extra = {}) {
  const value = stage(revision, {features:{drafts:true,send:true,files:false,deletion:false},draft:null,...extra});
  value.context.destinationId = claude;
  return value;
}
const observed = (extra = {}) => ({id:'3'.repeat(32),draftId:id,revision:2,createdAt:3,text:'Private draft',
  preparation:'materialized',files:[{...submittedFile,savedCopyState:'available'}],delivery:'completed',accepted:true,
  execution:'ended',reply:{state:'unknown',text:null},reason:null,actions:{check:'check-exact',finish:null,cleanupSavedCopies:null},...extra});

test('an unresolved Send stays read only with its files and permits only Look again', async () => {
  const intent = observed({delivery:'not_started',accepted:false,execution:'unobserved',preparation:'intent',
    files:[{...submittedFile,savedCopyState:'not_saved'}]});
  const attempt = {id:'held-attempt',draftId:id,requestId:intent.id,phase:'sending',submitted:null};
  const h = await harness(stage2(1,{draft:fileDraft,uncertain:true,attempts:[attempt],requests:[intent]}),claude);
  assert.equal(h.input.value,'Private draft');
  assert.equal(h.input.readOnly,true);
  assert.equal(h.send.disabled,true);
  assert.match(h.root.textContent,/Send is off while Motet does not know/);
  assert.match(h.root.textContent,/fictional-plan\.pdf/);
  assert.equal(h.log.children.length,0);
  assert.equal(h.p.blocksSend(),true);
  h.button('Look again'); await tick();
  assert.deepEqual(h.calls,[['refresh']]);
  h.publish(stage2(2,{requests:[observed()]}));
  assert.equal(h.input.value,'Independent ordinary text');
  assert.equal(h.log.children.length,1);
  assert.doesNotMatch(h.root.textContent,/did not hear back after Send/);
});

test('cold recovery holds the writer and never offers Resume for the same draft', async () => {
  const attempt = {id:'held-attempt',draftId:id,requestId:null,phase:'preparing',submitted:null};
  const summary = {...saved,readOnly:true,fileCount:1,totalBytes:700};
  const h = await harness(stage2(1,{uncertain:true,attempts:[attempt],savedDrafts:[summary]}),claude);
  assert.equal(h.input.readOnly,true);
  assert.equal(h.send.disabled,true);
  assert.match(h.root.textContent,/saved draft could not be read here/);
  assert.doesNotMatch(h.root.textContent,/Nothing in it has been sent|0 B/);
  assert.throws(()=>h.button('Resume draft'),/Available action/);
  h.button('Look again'); await tick();
  assert.deepEqual(h.calls,[['refresh']]);
});

test('another request for the same draft cannot claim or populate an unresolved hold', async () => {
  const attempt = {id:'held-attempt',draftId:id,requestId:'5'.repeat(32),phase:'sending',submitted:null};
  const otherIntent = observed({id:'6'.repeat(32),text:'Unrelated intent',delivery:'not_started',accepted:false,
    execution:'unobserved',preparation:'intent',files:[{...submittedFile,savedCopyState:'not_saved'}]});
  const h = await harness(stage2(1,{uncertain:true,attempts:[attempt],requests:[observed(),otherIntent]}),claude);
  assert.equal(h.input.value,'');
  assert.equal(h.input.readOnly,true);
  assert.equal(h.log.children.length,1);
  assert.match(h.root.textContent,/saved draft could not be read here/);
  assert.doesNotMatch(h.root.textContent,/Unrelated intent/);
  h.button('Look again'); await tick();
  assert.deepEqual(h.calls,[['refresh']]);
});

test('verified completion and reply remain with their exact message and retire on selection change', async () => {
  const h = await harness(stage2(1,{requests:[observed()]}),claude);
  assert.match(h.log.textContent,/Message to Fictional Ada/);
  assert.match(h.log.textContent,/Finished · no reply was returned to Motet/);
  assert.doesNotMatch(h.log.textContent,/Answered/);
  const row = h.log.children[0];
  h.button('Check status');
  assert.match(row.textContent,/Checking…/);
  await tick();
  assert.deepEqual(h.calls,[['checkRequest','check-exact']]);
  assert.equal(h.log.children[0],row,'inconclusive readback keeps the original row and action');
  assert.match(row.textContent,/Status checked\./);
  h.publish(stage2(2,{requests:[observed({reply:{state:'available',text:'A verified fictional reply.'}})]}));
  assert.match(h.log.textContent,/AnsweredStatus checked\.Reply from Fictional AdaA verified fictional reply\./);
  h.publish(stage2(3,{requests:[observed({reply:{state:'withheld',text:null}})]}));
  assert.match(h.log.textContent,/Finished · the reply was withheld/);
  assert.doesNotMatch(h.log.textContent,/A verified fictional reply/);
  h.leave();
  assert.equal(h.log.children.length,0);
});

test('a failed Check status reports the read failure on the same row', async () => {
  const error = {code:'network_error',message:'Fictional HTTP failure'};
  const h = await harness(stage2(1,{requests:[observed()],error}),claude);
  const row = h.log.children[0];
  h.button('Check status'); await tick();
  assert.deepEqual(h.calls,[['checkRequest','check-exact']]);
  assert.equal(h.log.children[0],row);
  assert.match(row.textContent,/Could not check status\./);
  assert.doesNotMatch(row.textContent,/Status checked\./);
});

test('a sealed refusal keeps the draft and only reports that nothing was sent', async () => {
  const request = observed({revision:1, delivery:'not_started', accepted:false, execution:'unobserved',
    preparation:'intent', reason:'earlier_delivery_unresolved',
    files:[{...submittedFile,savedCopyState:'not_saved'}]});
  const draft = {...fileDraft,text:'Fictional draft'};
  const error = {code:'earlier_delivery_unresolved',message:'Fictional refusal'};
  const h = await harness(stage2(1,{draft,requests:[request],error}),claude);
  assert.equal(h.input.value,'Fictional draft');
  assert.equal(h.log.children.length,0);
  assert.match(h.root.textContent,/An earlier unresolved delivery to Fictional Ada blocked this send\. Nothing was sent\./);
});

test('an exact partial-copy fixture shows no Finish or Remove and cannot enable it on the live binding', async () => {
  const second = {...submittedFile,fileId:'4'.repeat(32),name:'fictional-requirements.pdf',savedCopyState:'not_saved'};
  const value = stage2(1,{requests:[observed({delivery:'not_submitted',accepted:false,execution:'unobserved',
    reason:'proven_not_submitted',files:[{...submittedFile,savedCopyState:'available'},second]})]});
  assert.equal(stageOneSnapshot(value,claude,true),false);
  const h = await harness(value,claude,true);
  assert.match(h.log.textContent,/Not sentThis message was not delivered/);
  assert.match(h.log.textContent,/Saved for Fictional Ada/);
  assert.match(h.log.textContent,/cannot finish sending or remove the copied file from here yet/);
  assert.equal(h.log.all().some(n=>n.getAttribute('aria-label')==='Finish sending' || n.getAttribute('aria-label')?.startsWith('Remove')),false);
  assert.deepEqual(h.calls,[]);
});

test('a copy failure with pending delivery does not claim that files are still saving', async () => {
  const other = {...submittedFile,fileId:'4'.repeat(32),name:'fictional-requirements.pdf',savedCopyState:'unavailable'};
  const value = stage2(1,{requests:[observed({delivery:'pending',accepted:false,execution:'unobserved',
    preparation:'preparing_delivery',reason:'copy_failed',files:[{...submittedFile,savedCopyState:'available'},other]})]});
  const h = await harness(value,claude);
  assert.match(h.log.textContent,/Sending · delivery not yet confirmed/);
  assert.doesNotMatch(h.log.textContent,/saving 2 files|Not sent/);
});

test('file receipts keep the pre-layout follow state and paint after styling the conversation', async () => {
  const h = await harness(stage2(1,{requests:[observed()]}),claude);
  assert.deepEqual(h.paints.slice(-3),['measure','layout',['paint',true,true]]);
  h.setNearEnd(false);
  h.publish(stage2(2,{requests:[observed({reply:{state:'withheld',text:null}})]}));
  assert.deepEqual(h.paints.slice(-3),['measure','layout',['paint',false,true]]);
  h.setNearEnd(true);
  h.publish(stage2(3,{requests:[observed({reply:{state:'available',text:'A longer fictional reply.'}})]}));
  assert.deepEqual(h.paints.slice(-3),['measure','layout',['paint',true,true]]);
});
