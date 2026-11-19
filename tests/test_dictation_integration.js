#!/usr/bin/env node
'use strict';
/* Page wiring only: fictional keys/drafts, no browser microphone or external requests. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8').replace(/\r\n?/g,'\n');
const wiring=html.slice(html.indexOf('/* Optional local dictation page wiring.'),html.indexOf('/* End optional local dictation page wiring. */'));
const sourceLine=name=>html.split('\n').find(s=>s.startsWith('function '+name+'('));
function functionBlock(name){const start=html.indexOf('function '+name+'(');assert(start>=0);const firstEnd=html.indexOf('\n',start);if(html.slice(start,firstEnd).trimEnd().endsWith('}'))return html.slice(start,firstEnd);const end=html.indexOf('\n}',start);assert(end>=0);return html.slice(start,end+2)}
function harness({demo=false,key='fictional-owner',workspace=true,modules=true,mutate=x=>x}={}){
  const calls=[],nodes=new Map(),doc={hidden:false};
  class Node{
    constructor(name){this.name=name;this.attrs={};this.handlers={};this.children=[];this.value='exact draft Ω';this.disabled=false;this.readOnly=false;this.dataset={};this.isConnected=true;this.style={};this.parentNode=null;}
    set id(value){this._id=value;nodes.set('#'+value,this)}get id(){return this._id}
    setAttribute(k,v){this.attrs[k]=v}getAttribute(k){return this.attrs[k]??null}
    append(...children){for(const child of children){this.children.push(child);child.parentNode=this}}appendChild(child){this.append(child);return child}
    addEventListener(type,fn,capture){(this.handlers[type]||=[]).push({fn,capture})}
    dispatchEvent(event){calls.push(['input event',event.type,event.bubbles]);for(const listener of this.handlers[event.type]||[])listener.fn(event);return true}
    focus(){doc.activeElement=this}closest(){return null}
  }
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
  const input=node('#ofIn'),host=node('#officer');
  doc.createElement=tag=>new Node(tag);doc.activeElement=input;
  const controls={mic:node('mic'),cancel:node('cancel'),send:node('#ofSend'),wave:node('wave'),status:node('status'),settings:node('settings')};
  const view={...controls,render:(state,detail)=>calls.push(['render',state,detail]),refresh:()=>calls.push(['view refresh'])};
  const ctx={console,WORKSPACE:workspace,DEMO:demo,KEY:key,locked:false,claiming:false,document:doc,ofIn:input,ofEl:host,OFUI:{epoch:3,recipient:'watch',scope:'room',docked:false},cuTarget:()=>null,Headers,AbortController,Event,setTimeout,clearTimeout,
    WS:{controls:{open:false}},$:node,fetch:async(url,options)=>{calls.push(['fetch',url,options]);return {ok:true,json:async()=>({state:'ready'})}},
    workspaceOpen:(dialog,origin)=>{dialog.open=true;calls.push(['settings open',origin])},workspaceSettingsSelect:section=>calls.push(['settings section',section]),
    MutationObserver:class{constructor(callback){this.callback=callback}observe(target,options){calls.push(['observe',target,options])}}
  };
  let controllerOptions,viewOptions;
  const controller={state:'idle',active:false,beforeSend:()=>{calls.push(['before send',input.value]);return input.value},contextChanged:()=>calls.push(['retire',input.value]),refresh:()=>calls.push(['controller refresh'])};
  if(modules){ctx.MotetDictationView={attach:options=>{viewOptions=options;calls.push(['attach']);return view}};ctx.MotetDictationDraft={};ctx.MotetDictation={create:options=>{controllerOptions=options;calls.push(['create']);return controller}}}
  ctx.deskSubmitKey=vm.runInNewContext(sourceLine('deskSubmitKey')+';deskSubmitKey');
  vm.runInNewContext(mutate(wiring),ctx);
  return {ctx,calls,node,input,host,controller,view,controls,doc,get options(){return controllerOptions},get viewOptions(){return viewOptions}};
}
function initProof(mutate=x=>x){
  for(const args of[{workspace:false},{modules:false},{workspace:false,modules:false}]){const h=harness({...args,mutate});assert.equal(h.calls.length,0,'missing/off feature is inert')}
  const demo=harness({demo:true,mutate});assert(demo.calls.some(c=>c[0]==='attach'));assert(!demo.calls.some(c=>c[0]==='create'||c[0]==='fetch'),'demo never constructs a capture controller or calls API');assert.equal(demo.viewOptions.disabled(),true);
  const h=harness({mutate});assert.equal(h.calls.filter(c=>c[0]==='create').length,1);assert.equal(h.calls.filter(c=>c[0]==='fetch').length,0,'page load never probes or starts recognition');
  h.ctx.workspaceDictationInit();assert.equal(h.calls.filter(c=>c[0]==='attach').length,1,'shared composer binds once');
  assert.equal(h.options.workletUrl,'/dictation-worklet.js');assert.equal(h.options.canUse(),true);
  for(const prop of['locked','claiming']){h.ctx[prop]=true;assert.equal(h.options.canUse(),false);h.ctx[prop]=false}
  for(const prop of['disabled','readOnly']){h.input[prop]=true;assert.equal(h.options.canUse(),false);h.input[prop]=false}
  h.ctx.KEY='';assert.equal(h.options.canUse(),false);h.ctx.KEY='fictional-owner';h.doc.hidden=true;assert.equal(h.options.canUse(),false);h.doc.hidden=false;
  const before=h.options.target();h.ctx.OFUI.scope='station';assert.notEqual(h.options.target(),before);h.ctx.OFUI.scope='room';
  h.options.onState('listening',{message:'Listening',ranges:[{start:0,end:4}]});const last=h.calls.at(-1);assert.equal(last[1],'listening');assert.equal(last[2].message,'','plain listening row has no status words');assert.equal(last[2].ranges.length,1);
  h.options.onState('checking',{message:'Checking local dictation…'});assert.equal(h.calls.at(-1)[1],'preparing');h.options.onState('not_installed',{message:'Not installed'});assert.equal(h.calls.at(-1)[2].code,'not_installed');
  h.options.onDraftChange();assert(h.calls.some(c=>c[0]==='input event'&&c[1]==='input'&&c[2]===true),'programmatic transcript notifies actual input listeners');
  const n=h.calls.length;h.ctx.workspaceDictationBeforeSend();assert.equal(h.calls[n][0],'before send');
  const old=h.calls.length;h.ctx.workspaceDictationContextChanging();assert.deepEqual(h.calls[old],['retire','exact draft Ω']);
  const retired=h.calls.filter(c=>c[0]==='retire').length;h.ctx.OFUI.epoch++;h.ctx.workspaceDictationRefresh();assert.equal(h.calls.filter(c=>c[0]==='retire').length,retired+1,'fallback identity check retires a changed owner');
  // Ancestor capture runs before native/attachment handlers bound on the shared Send/input nodes.
  const click=h.host.handlers.click.find(v=>v.capture&&String(v.fn).includes("'#ofSend'"));assert(click);
  const key=h.host.handlers.keydown.find(v=>v.capture);assert(key);
  const count=h.calls.filter(c=>c[0]==='before send').length;click.fn({target:{closest:selector=>selector==='#ofSend'?h.controls.send:null}});key.fn({target:h.input,key:'Enter',shiftKey:false,ctrlKey:false,altKey:false,metaKey:false,isComposing:false});assert.equal(h.calls.filter(c=>c[0]==='before send').length,count+2);
  key.fn({target:h.input,key:'Enter',shiftKey:true});key.fn({target:h.input,key:'Enter',isComposing:true});assert.equal(h.calls.filter(c=>c[0]==='before send').length,count+2,'newline/IME are not treated as Send');
}
initProof();
async function apiProof(mutate=x=>x){
  const h=harness({mutate}),api=h.ctx.workspaceDictationApi;const result=await api('/api/dictation/status');assert.equal(result.state,'ready');
  const fetch=h.calls.find(c=>c[0]==='fetch');assert.equal(fetch[1],'/api/dictation/status');assert.equal(fetch[2].headers.get('X-Live-Room-Key'),'fictional-owner');assert.equal(fetch[2].credentials,'same-origin');assert.equal(fetch[2].cache,'no-store');assert.equal(fetch[2].redirect,'error');
  const audio=new Uint8Array([1,2,3,4]);await api('/api/dictation/audio',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Dictation-Session':'fictional-session','X-Dictation-Sequence':'0'},body:audio});const sent=h.calls.at(-1)[2];assert.equal(sent.body,audio);assert.equal(sent.headers.get('Content-Type'),'application/octet-stream');assert.equal(sent.headers.get('X-Dictation-Session'),'fictional-session');
  for(const args of[{demo:true},{key:''},{workspace:false}]){const off=harness({...args,mutate});await assert.rejects(()=>off.ctx.workspaceDictationApi('/api/dictation/status'),error=>error.code==='unavailable');assert(!off.calls.some(c=>c[0]==='fetch'),'unauthorized/demo API does not fetch')}
  const before=h.calls.length;await assert.rejects(()=>api('https://example.invalid/api/dictation/start'));await assert.rejects(()=>api('/api/dictation/start?key=fictional'));await assert.rejects(()=>api('/agent-message'));assert.equal(h.calls.length,before,'adapter refuses noncanonical/foreign routes before transport');
  h.ctx.fetch=async()=>({ok:false,json:async()=>({error:{code:'not_installed'}})});await assert.rejects(()=>api('/api/dictation/start'),error=>error.code==='not_installed');
  h.ctx.fetch=async()=>{h.ctx.KEY='different-fictional-owner';return {ok:true,json:async()=>({state:'ready'})}};await assert.rejects(()=>api('/api/dictation/status'),error=>error.code==='unavailable','old response cannot publish across owner-key change');
}
async function sendProof(mutate=x=>x){
  for(const name of['cuSend','ofSend','ofSendOrdinary']){
    const calls=[],ctx={workspaceDictationBeforeSend:()=>calls.push('voice'),workspaceLiveDocumentsSend:()=>{calls.push('documents');return true},workspaceOfficerHistoryActive:()=>{calls.push('history');return true}};
    const fn=vm.runInNewContext(mutate('async '+functionBlock(name))+';'+name,ctx);await fn();assert.equal(calls[0],'voice',name+' stops capture before any send interceptor');assert.equal(calls[1],name==='ofSendOrdinary'?'history':'documents');
  }
}
function lifecycleProof(mutate=x=>x){
  let calls=[];const scope={scope:'room',recipient:'watch'},input={value:'old visible draft'},node={open:true};
  const ctx={OFUI:scope,ofIn:input,$:()=>node,workspaceDictationContextChanging:()=>calls.push(['retire',input.value,scope.recipient,scope.scope]),cuDraftSave:()=>calls.push(['save',input.value,scope.recipient,scope.scope]),cuDraftRestore:()=>{input.value='new recipient draft';calls.push(['restore'])},ofContextPaint:()=>calls.push(['paint'])};
  let fn=vm.runInNewContext(mutate(sourceLine('cuSelectRecipient'))+';cuSelectRecipient',ctx);fn('agent');assert.deepEqual(calls[0],['retire','old visible draft','watch','room']);assert.equal(calls[1][0],'save');assert.equal(input.value,'new recipient draft');
  calls=[];scope.recipient='watch';scope.scope='room';input.value='scope draft';ctx.ofCurrent=()=>({context:{scope:'room'}});const button={disabled:false,dataset:{scope:'station'}};
  vm.runInNewContext(mutate(html.split('\n').find(s=>s.startsWith("$('#ofScope').onclick="))),ctx);node.onclick({target:{closest:()=>button}});assert.deepEqual(calls[0],['retire','scope draft','watch','room']);assert.equal(calls[1][0],'save');assert.equal(scope.scope,'station');
  calls=[];const nativeFactory=()=>nativeFactory.current;nativeFactory.current={retireIfChanged(){calls.push('native retire')},ownsInput:()=>true};const restoreContext={workspaceDictationContextChanging:()=>calls.push('voice'),workspaceNativeAttachments:nativeFactory};fn=vm.runInNewContext(mutate(functionBlock('cuDraftRestore'))+';cuDraftRestore',restoreContext);fn();assert.deepEqual(calls,['voice','native retire'],'voice retires before draft controller ownership can change');
  calls=[];const stop=new Error('stop after first unrelated hook'),focusContext={focus:null,A:{'fictional-agent':{}},workspaceDictationContextChanging:()=>calls.push('voice'),workspaceCancelDeskReturn:()=>{calls.push('desk');throw stop}};
  fn=vm.runInNewContext(mutate(functionBlock('setFocus'))+';setFocus',focusContext);assert.throws(()=>fn('fictional-agent'),error=>error===stop);assert.deepEqual(calls,['voice','desk'],'voice retires before focus handoff begins');
  calls=[];const lockContext={workspaceDictationContextChanging:()=>calls.push('voice'),workspaceAppIconsReset:()=>{calls.push('lock');throw stop}};
  fn=vm.runInNewContext(mutate(sourceLine('lock'))+';lock',lockContext);assert.throws(()=>fn(true),error=>error===stop);assert.deepEqual(calls,['voice','lock'],'voice retires before locking changes private state');
  for(const name of['workspaceOfficerRestore','workspaceStationRestore','workspaceOfficerDeskClose']){const code=sourceLine(name);const before=code.indexOf('workspaceDictationContextChanging');const restore=code.indexOf('workspaceAttachmentsBeforeRestore');assert(before>0&&before<restore,name+' retires voice before shared composer moves')}
  const prompt=html.split('\n').find(s=>s.startsWith("$('#ofQuick').onclick="));assert(prompt.indexOf('workspaceDictationBeforeSend')<prompt.indexOf('workspaceLiveDocumentsSuggest'),'quick-prompt voice retirement precedes draft overwrite/interception');
}
lifecycleProof();
async function main(){
  await apiProof();await sendProof();
  assert.throws(()=>initProof(s=>s.replace("if(DEMO){view.render('idle');return}","if(false){view.render('idle');return}")),assert.AssertionError,'demo construction bypass mutation caught');
  assert.throws(()=>initProof(s=>s.replace("if(state==='listening')shown.message='';",'')),assert.AssertionError,'listening status words mutation caught');
  await assert.rejects(()=>sendProof(s=>s.replace("if(typeof workspaceDictationBeforeSend==='function')workspaceDictationBeforeSend();",'')),assert.AssertionError,'missing pre-send retirement mutation caught');
  assert.throws(()=>lifecycleProof(s=>s.replace("if(typeof workspaceDictationContextChanging==='function')workspaceDictationContextChanging();",'')),assert.AssertionError,'missing pre-context retirement mutation caught');
  assert(wiring.includes("help.href='/dictation-help'"));assert(wiring.includes("help.rel='noopener noreferrer'"));assert(!/X-Dictation-Key|127\.0\.0\.1:[0-9]|\.live-room|localStorage/.test(wiring),'page wiring contains no helper capability, private path or extra persistence');
  console.log('ok dictation page wiring: inert demo/classic/load, exact visible Send fence, pre-context ownership, private same-origin adapter, Settings help and four causal guard mutations');
}
main().catch(error=>{console.error(error);process.exitCode=1});
