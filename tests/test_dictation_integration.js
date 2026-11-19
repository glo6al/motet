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
    get nextSibling(){return this.parentNode?.children[this.parentNode.children.indexOf(this)+1]||null}get nextElementSibling(){return this.nextSibling}
    remove(){if(this.parentNode){const siblings=this.parentNode.children;siblings.splice(siblings.indexOf(this),1);this.parentNode=null}}
    contains(child){return child===this||this.children.some(node=>node.contains(child))}
    append(...children){for(const child of children){child.remove();this.children.push(child);child.parentNode=this}}appendChild(child){this.append(child);return child}
    insertBefore(child,next){if(child===next)return;calls.push(['move',child]);if(child.contains(doc.activeElement))doc.activeElement=null;child.remove();child.parentNode=this;const at=next?this.children.indexOf(next):-1;if(at<0)this.children.push(child);else this.children.splice(at,0,child)}
    querySelector(selector){return this.children.flatMap(child=>[child,...child.descendants()]).find(child=>child.className===selector.slice(1))||null}descendants(){return this.children.flatMap(child=>[child,...child.descendants()])}
    classList={values:new Set(),contains(name){return this.values.has(name)},add(name){this.values.add(name)},remove(name){this.values.delete(name)},toggle(name,on){if(on)this.add(name);else this.remove(name)}};
    addEventListener(type,fn,capture){(this.handlers[type]||=[]).push({fn,capture})}
    dispatchEvent(event){calls.push(['input event',event.type,event.bubbles]);for(const listener of this.handlers[event.type]||[])listener.fn(event);return true}
    focus(){doc.activeElement=this}closest(){return null}
    getClientRects(){return this.rects===false?[]:[{}]}
  }
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
  const input=node('#ofIn'),host=node('#officer');
  doc.createElement=tag=>new Node(tag);doc.activeElement=input;
  const controls={mic:node('mic'),cancel:node('cancel'),send:node('#ofSend'),wave:node('wave'),status:node('status'),settings:node('settings')};
  const view={...controls,render:(state,detail)=>{host.classList.toggle('dictation-active',['preparing','listening'].includes(state));calls.push(['render',state,detail])},refresh:()=>calls.push(['view refresh'])};
  const ctx={console,WORKSPACE:workspace,DEMO:demo,KEY:key,locked:false,claiming:false,document:doc,ofIn:input,ofEl:host,OFUI:{epoch:3,recipient:'watch',scope:'room',docked:false},cuTarget:()=>null,Headers,AbortController,Event,setTimeout,clearTimeout,
    cuDraftKey:vm.runInNewContext(sourceLine('cuDraftKey')+';cuDraftKey'),WS:{controls:{open:false}},$:node,fetch:async(url,options)=>{calls.push(['fetch',url,options]);return {ok:true,json:async()=>({state:'ready'})}},
    workspaceOpen:(dialog,origin)=>{dialog.open=true;calls.push(['settings open',origin])},workspaceSettingsSelect:section=>calls.push(['settings section',section]),
    MutationObserver:class{constructor(callback){this.callback=callback}observe(target,options){calls.push(['observe',target,options])}}
  };
  let controllerOptions,viewOptions;
  const controller={state:'idle',active:false,beforeSend:()=>{calls.push(['before send',input.value]);return input.value},contextChanged:()=>calls.push(['retire',input.value]),refresh:()=>calls.push(['controller refresh']),escape:event=>{calls.push(['escape attempt']);if(!controller.active||event.isComposing||event.keyCode===229||event.defaultPrevented)return false;calls.push(['escape']);controller.active=false;event.preventDefault();event.stopImmediatePropagation();return true}};
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
  const retired=h.calls.filter(c=>c[0]==='retire').length;h.ctx.OFUI.epoch++;h.ctx.workspaceDictationRefresh();assert.equal(h.calls.filter(c=>c[0]==='retire').length,retired,'presentation epochs do not end a take');h.ctx.OFUI.recipient='agent';h.ctx.workspaceDictationRefresh();assert.equal(h.calls.filter(c=>c[0]==='retire').length,retired+1,'fallback identity check retires a changed owner');
  // Ancestor capture runs before native/attachment handlers bound on the shared Send/input nodes.
  const click=h.host.handlers.click.find(v=>v.capture&&String(v.fn).includes("'#ofSend'"));assert(click);
  const key=h.host.handlers.keydown.find(v=>v.capture);assert(key);
  const count=h.calls.filter(c=>c[0]==='before send').length;click.fn({target:{closest:selector=>selector==='#ofSend'?h.controls.send:null}});key.fn({target:h.input,key:'Enter',shiftKey:false,ctrlKey:false,altKey:false,metaKey:false,isComposing:false});assert.equal(h.calls.filter(c=>c[0]==='before send').length,count+2);
  key.fn({target:h.input,key:'Enter',shiftKey:true});key.fn({target:h.input,key:'Enter',isComposing:true});assert.equal(h.calls.filter(c=>c[0]==='before send').length,count+2,'newline/IME are not treated as Send');
}
initProof();
function orderProof(mutate=x=>x){
  const h=harness({mutate}),row=h.node('#ofRow'),compose=h.node('#ofCompose'),plus=h.node('plus'),files=h.node('files');
  plus.className='workspace-attachments-plus';files.className='workspace-attachments-files';
  compose.append(h.input);h.host.append(row);row.append(compose,plus,files,h.controls.cancel,h.controls.wave,h.controls.mic,h.controls.send);
  const sequence=()=>row.children.map(node=>node.name);
  h.input.focus();h.ctx.workspaceDictationOrderControls();
  assert.deepEqual(sequence(),['plus','files','#ofCompose','cancel','wave','mic','#ofSend']);
  assert.equal(h.doc.activeElement,h.input);assert.equal(h.input.parentNode,compose);
  const originalText=h.input.value;h.input.selectionStart=3;h.input.selectionEnd=7;h.input.scrollTop=18;
  h.host.classList.add('dictation-active');files.focus();h.ctx.workspaceDictationOrderControls();
  assert.deepEqual(sequence(),['#ofCompose','plus','files','cancel','wave','mic','#ofSend']);
  assert.equal(h.doc.activeElement,files,'Moving Files retains keyboard focus');
  h.host.classList.remove('dictation-active');plus.focus();h.ctx.workspaceDictationOrderControls();
  assert.equal(h.doc.activeElement,plus,'Returning plus to idle retains keyboard focus');
  assert(!h.calls.some(call=>call[0]==='move'&&(call[1]===compose||call[1]===h.input)),'The live editor and its wrapper are never reparented');
  assert.equal(h.input.value,originalText);assert.equal(h.input.selectionStart,3);assert.equal(h.input.selectionEnd,7);assert.equal(h.input.scrollTop,18);
  const moves=h.calls.filter(call=>call[0]==='move').length;h.ctx.workspaceDictationOrderControls();assert.equal(h.calls.filter(call=>call[0]==='move').length,moves,'Settled order makes no extra DOM moves');
}
function identityProof(mutate=x=>x){
  const h=harness({mutate});let target='fictional-agent-one';h.ctx.cuTarget=()=>target;
  const room=h.options.target();target='fictional-agent-two';h.ctx.OFUI.epoch++;h.ctx.OFUI.docked=true;
  assert.equal(h.options.target(),room,'Room Motet draft ignores stale agent target and layout epochs');
  h.controller.active=true;h.ctx.workspaceDictationRefresh();assert.equal(h.calls.filter(call=>call[0]==='retire').length,0);
  h.ctx.OFUI.scope='station';const station=h.options.target();assert.notEqual(station,room);h.ctx.workspaceDictationRefresh();
  const retires=h.calls.filter(call=>call[0]==='retire').length;target='fictional-agent-three';assert.notEqual(h.options.target(),station);h.ctx.workspaceDictationRefresh();assert.equal(h.calls.filter(call=>call[0]==='retire').length,retires+1);
  const before=h.options.target();h.ctx.KEY='another-fictional-owner';assert.notEqual(h.options.target(),before);
}
function windowEscapeProof(mutate=x=>x){
  for(const route of ['desk','global'])for(const location of ['mic','outside','ime','popup','model','recipient',...(route==='global'?['attachments','suggestions']:[])]){
    const h=harness({mutate}),ctx=h.ctx;h.controller.active=true;const background=[];
    Object.assign(ctx,{focus:'fictional-agent',DESK:{drawer:null,open:true},workspaceForeground:()=>location==='popup',agDialogForeground:()=>false,deckForeground:()=>false,
      officerModelEscape:()=>location==='model',workspaceOfficerEscape:()=>location==='recipient',workspaceAttachmentsEscape:()=>location==='attachments',
      workspaceOfficerDeskActive:()=>false,workspaceOfficerUnifiedMenu:()=>false,workspaceOfficerInlineSuggestions:()=>false,
      deskEscapeLayer:(drawer,work)=>drawer?'drawer':work?'work':'room',deskCloseWork:()=>background.push('work'),deskCloseDrawer:()=>background.push('drawer'),render:()=>{},
      workspaceOfficerDeskClosePhonePanel:()=>{background.push('phone');return false},ofToggle:()=>background.push('fold'),cuChooserClose:()=>background.push('chooser')});
    h.node('#cuAgentChoices').hidden=true;h.node('#ofSuggestions').open=location==='suggestions';h.node('#ofSuggestions summary').focus=()=>{};
    let handler;if(route==='desk')handler=vm.runInNewContext(mutate(functionBlock('deskKeydown'))+';deskKeydown',ctx);
    else {ctx.addEventListener=(name,fn,capture)=>{assert.equal(name,'keydown');assert.equal(capture,true);handler=fn};const line=html.split('\n').find(line=>line.startsWith("addEventListener('keydown',e=>{")&&line.includes('workspaceDictationEscape'));vm.runInNewContext(mutate(line),ctx)}
    const event={key:'Escape',target:location==='outside'?h.node('unrelated-stage'):h.controls.mic,isComposing:location==='ime',
      preventDefault(){this.defaultPrevented=true},stopImmediatePropagation(){this.stopped=true}};
    handler(event);
    if(location==='mic'||location==='outside'){assert.equal(h.controller.active,false,route+' '+location+' Escape cancels the active take');assert(event.defaultPrevented);assert(event.stopped);assert.deepEqual(background,[],'Cancel precedes nonmodal panel/navigation');}
    else {assert.equal(h.controller.active,true,route+' '+location+' keeps the take');assert(!h.calls.some(call=>call[0]==='escape attempt'));assert.deepEqual(background,[]);}
  }
}
function hiddenPopupProof(mutate=x=>x){
  for(const location of ['motet-model','phone-model','stage-model','settings-model']){
    const h=harness({mutate}),ctx=h.ctx,menu=h.node('#ofModelMenu'),button=h.node('#ofModelButton');
    ctx.WSO={active:false};ctx.OMODEL={token:0,attempted:true};
    vm.runInNewContext(sourceLine('officerModelClose')+'\n'+sourceLine('officerModelEscape'),ctx);
    menu.hidden=false;h.node('#cuAgentChoices').hidden=true;button.rects=!['motet-model','phone-model'].includes(location);h.controls.mic.focus();
    ctx.workspaceDictationCloseHiddenPopups();assert.equal(menu.hidden,false,'Inactive layout never closes a popup');
    h.controller.active=true;h.options.onState('listening',{});
    const shouldClose=!button.getClientRects().length;
    assert.equal(menu.hidden,shouldClose,location+' closes only a hidden owner');
    assert.equal(h.doc.activeElement,h.controls.mic,'Closing a hidden model never returns focus to its trigger');
    const event={key:'Escape',target:h.controls.mic,preventDefault(){this.defaultPrevented=true},stopImmediatePropagation(){this.stopped=true}};
    if(!ctx.officerModelEscape(event))ctx.workspaceDictationEscape(event);
    assert.equal(h.controller.active,!shouldClose,'The first Escape cancels capture unless a visible model popup takes precedence');
    if(!shouldClose){menu.hidden=false;button.rects=false;h.controls.mic.focus();ctx.workspaceDictationRefresh();assert.equal(menu.hidden,true,'Active resize refresh closes the model when the phone breakpoint hides its trigger');assert.equal(h.doc.activeElement,h.controls.mic);}
  }
  for(const kind of ['WOD','WSO','WSR','chooser'])for(const visible of [false,true]){
    const h=harness({mutate}),ctx=h.ctx,trigger=h.node('popup-trigger'),popup=h.node('popup'),closed=[];
    trigger.rects=visible;popup.hidden=false;h.node('#ofModelMenu').hidden=true;h.node('#cuAgentChoices').hidden=true;
    if(kind==='chooser'){h.node('#cuAgentChoices').hidden=false;h.node('#recipient [data-recipient=agent]').rects=visible;ctx.cuChooserClose=restore=>{closed.push(restore);h.node('#cuAgentChoices').hidden=true};}
    else {ctx[kind]=kind==='WOD'?{recipient:popup,recipientButton:trigger}:{popover:popup,trigger};const close={WOD:'workspaceOfficerDeskRecipientClose',WSO:'workspaceOfficerClose',WSR:'workspaceStationClose'}[kind];ctx[close]=restore=>{closed.push(restore);popup.hidden=true};}
    h.controls.mic.focus();h.options.onState('listening',{});
    assert.deepEqual(closed,visible?[]:[false],kind+' closes without focus return only when the owner disappears');
    assert.equal(h.doc.activeElement,h.controls.mic);
  }
}
function openContextProof(mutate=x=>x){
  for(const scenario of [
    {recipient:'watch',scope:'room',target:'fictional-a',next:{scope:'room',agent:'fictional-b'},changed:false},
    {recipient:'watch',scope:'station',target:'fictional-a',next:{scope:'station',agent:'fictional-a'},changed:false},
    {recipient:'agent',scope:'station',target:'fictional-a',next:{scope:'station',agent:'fictional-a'},changed:true},
    {recipient:'watch',scope:'room',target:'fictional-a',next:{scope:'station',agent:'fictional-a'},changed:true},
    {recipient:'watch',scope:'station',target:'fictional-a',next:{scope:'station',agent:'fictional-b'},changed:true}
  ]){
    const calls=[],ctx={OFUI:{recipient:scenario.recipient,scope:scenario.scope,epoch:4},focus:scenario.target,A:{'fictional-a':{},'fictional-b':{}},cuTarget:()=>scenario.target,
      workspaceDictationContextChanging:()=>calls.push('retire'),cuDraftSave:()=>calls.push('save'),cuDraftRestore:()=>calls.push('restore'),ofContextPaint:()=>{},ofToggle:()=>{},setFocus:()=>{}};
    const fn=vm.runInNewContext(mutate(functionBlock('ofOpenContext'))+';ofOpenContext',ctx);fn(scenario.next);
    assert.equal(calls.includes('retire'),scenario.changed,'Opening the already displayed Motet context is only a presentation change');
    if(scenario.changed)assert.equal(calls[0],'retire');
  }
}
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
  calls=[];const nativeFactory=()=>nativeFactory.current;nativeFactory.current={retireIfChanged(){calls.push('native retire')},ownsInput:()=>true};const restoreContext={workspaceDictationRefresh:()=>calls.push('voice'),workspaceNativeAttachments:nativeFactory};fn=vm.runInNewContext(mutate(functionBlock('cuDraftRestore'))+';cuDraftRestore',restoreContext);fn();assert.deepEqual(calls,['voice','native retire'],'voice retires before draft controller ownership can change');
  calls=[];const stop=new Error('stop after first unrelated hook'),focusContext={focus:null,A:{'fictional-agent':{}},workspaceDictationContextChanging:()=>calls.push('voice'),workspaceCancelDeskReturn:()=>{calls.push('desk');throw stop}};
  fn=vm.runInNewContext(mutate(functionBlock('setFocus'))+';setFocus',focusContext);assert.throws(()=>fn('fictional-agent'),error=>error===stop);assert.deepEqual(calls,['voice','desk'],'voice retires before focus handoff begins');
  calls=[];const lockContext={workspaceDictationContextChanging:()=>{calls.push('voice');throw stop},workspaceAppIconsReset:()=>calls.push('lock')};
  fn=vm.runInNewContext(mutate(sourceLine('lock'))+';lock',lockContext);assert.throws(()=>fn(true),error=>error===stop);assert.deepEqual(calls,['lock','voice'],'app icon reset precedes dictation retirement on locking');
  for(const name of['workspaceOfficerRestore','workspaceStationRestore','workspaceOfficerDeskClose','workspaceOfficerDeskOpen'])assert(!sourceLine(name).includes('workspaceDictationContextChanging'),name+' is a visual move, not a destination fence');assert(!sourceLine('ofToggle').includes('workspaceDictationContextChanging'));
  const prompt=html.split('\n').find(s=>s.startsWith("$('#ofQuick').onclick="));assert(prompt.indexOf('workspaceDictationBeforeSend')<prompt.indexOf('workspaceLiveDocumentsSuggest'),'quick-prompt voice retirement precedes draft overwrite/interception');
}
lifecycleProof();
async function main(){
  await apiProof();await sendProof();orderProof();identityProof();windowEscapeProof();hiddenPopupProof();openContextProof();
  for(const [name,mutate,proof]of[
    ['control ordering',s=>s.replace('for(const node of controls)row.insertBefore(node,anchor);',''),orderProof],
    ['stationary editor',s=>s.replace('for(const node of controls)row.insertBefore(node,anchor);','row.insertBefore(compose,anchor);for(const node of controls)row.insertBefore(node,anchor);'),orderProof],
    ['held control focus',s=>s.replace('held.focus({preventScroll:true});','void 0;'),orderProof],
    ['presentation epoch isolation',s=>s.replace('JSON.stringify([KEY,cuDraftKey(cuTarget(),OFUI.recipient,OFUI.scope)])','JSON.stringify([KEY,OFUI.epoch,OFUI.docked,cuDraftKey(cuTarget(),OFUI.recipient,OFUI.scope)])'),identityProof],
    ['window Escape ordering',s=>s.replace("if(typeof workspaceDictationEscape==='function'&&workspaceDictationEscape(e))return;",''),windowEscapeProof],
    ['window IME guard',s=>s.replace('if(e.isComposing||e.keyCode===229)return;',''),windowEscapeProof],
    ['hidden-owner popup cleanup',s=>s.replace("if(!$('#ofModelMenu').hidden&&hidden($('#ofModelButton')))officerModelClose(false);",''),hiddenPopupProof],
    ['popup cleanup after capture render',s=>s.replace('workspaceDictationCloseHiddenPopups();workspaceDictationOrderControls()},','workspaceDictationOrderControls()},'),hiddenPopupProof],
    ['popup owner visibility',s=>s.replace('const hidden=trigger=>!!trigger&&!trigger.getClientRects().length;','const hidden=trigger=>!!trigger;'),hiddenPopupProof],
    ['same-context opening retains capture',s=>s.replace("(OFUI.recipient!=='watch'||OFUI.scope!==context.scope||context.scope==='station'&&cuTarget()!==context.agent)",'true'),openContextProof]
  ])assert.throws(()=>proof(mutate),assert.AssertionError,name+' causal mutation caught');
  assert.throws(()=>initProof(s=>s.replace("if(DEMO){view.render('idle');return}","if(false){view.render('idle');return}")),assert.AssertionError,'demo construction bypass mutation caught');
  assert.throws(()=>initProof(s=>s.replace("if(state==='listening')shown.message='';",'')),assert.AssertionError,'listening status words mutation caught');
  await assert.rejects(()=>sendProof(s=>s.replace("if(typeof workspaceDictationBeforeSend==='function')workspaceDictationBeforeSend();",'')),assert.AssertionError,'missing pre-send retirement mutation caught');
  assert.throws(()=>lifecycleProof(s=>s.replace("if(typeof workspaceDictationContextChanging==='function')workspaceDictationContextChanging();",'')),assert.AssertionError,'missing pre-context retirement mutation caught');
  assert(wiring.includes("help.href='/dictation-help'"));assert(wiring.includes("help.rel='noopener noreferrer'"));assert(!/X-Dictation-Key|127\.0\.0\.1:[0-9]|\.live-room|localStorage/.test(wiring),'page wiring contains no helper capability, private path or extra persistence');
  console.log('ok dictation page wiring: inert demo/classic/load, Send/draft fences, stationary editor ordering/focus, real window Escape and hidden-popup precedence, private adapter, Settings help and fourteen causal mutations');
}
main().catch(error=>{console.error(error);process.exitCode=1});
