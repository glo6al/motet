#!/usr/bin/env node
'use strict';
/* Real desktop-stage layout functions, executed with identity-preserving DOM nodes. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8').replace(/\r\n?/g,'\n');
const block=html.slice(html.indexOf('const WSR='),html.indexOf('if(WORKSPACE){workspaceOfficerInit();workspaceOfficerLayout()}'));
const line=name=>html.split('\n').find(s=>s.startsWith('function '+name+'('));
function harness(mutate=x=>x,{microphone=false}={}){
  const document={activeElement:null};
  class Node{
    constructor(name,comment=false){this.name=name;this.comment=comment;this.children=[];this.parentNode=null;this.hidden=false;this.attrs={};this.dataset={};this.handlers={};this.isConnected=true;this.scrollHeight=38;this.rows=2;this.textContent='';this.value='unsent draft';this.classList={values:new Set(),contains:k=>this.classList.values.has(k),add:k=>this.classList.values.add(k),remove:k=>this.classList.values.delete(k)};this.style={values:{top:'',bottom:'100%','max-height':'300px'},getPropertyValue(k){return this.values[k]||''},getPropertyPriority(){return ''},setProperty(k,v){this.values[k]=v},removeProperty(k){delete this.values[k]}};}
    get firstElementChild(){return this.children.find(n=>!n.comment)||null}
    get nextSibling(){return this.parentNode?.children[this.parentNode.children.indexOf(this)+1]||null}
    setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]??null}removeAttribute(k){delete this.attrs[k]}
    remove(){if(this.parentNode){const p=this.parentNode;p.children.splice(p.children.indexOf(this),1);this.parentNode=null}}
    insertBefore(n,before){n.remove();const at=before?this.children.indexOf(before):this.children.length;assert(at>=0,'insert target belongs to parent');this.children.splice(at,0,n);n.parentNode=this;return n}
    append(...nodes){for(const n of nodes)this.insertBefore(n,null)}appendChild(n){return this.insertBefore(n,null)}prepend(n){this.insertBefore(n,this.children[0]||null)}
    before(n){this.parentNode.insertBefore(n,this)}after(n){this.parentNode.insertBefore(n,this.nextSibling)}
    contains(n){return n===this||this.children.some(c=>c.contains(n))}
    getClientRects(){return this.hidden?[]:[{}]}focus(){document.activeElement=this}
    addEventListener(k,fn){this.handlers[k]=fn}querySelector(selector){return selector==='.dictation-mic'?this.children.find(n=>n.classList.contains('dictation-mic'))||null:null}
  }
  const nodes={},node=id=>nodes[id]||(nodes[id]=new Node(id));
  const officer=node('#officer'),host=node('host'),body=node('#ofBody'),headButton=node('#ofB'),context=node('#ofContext'),scope=node('#ofScope'),station=node('#ofScope [data-scope=station]'),room=node('#ofScope [data-scope=room]'),progress=node('#ofProgress'),log=node('#ofLog'),tools=node('#ofTools'),suggestions=node('#ofSuggestions'),quick=node('#ofQuick'),check=node('#nativeCheckArea'),address=node('#ofAddress'),recipient=node('#recipient'),model=node('#ofModelPicker'),row=node('#ofRow'),compose=node('#ofCompose'),input=node('#ofIn'),send=node('#ofSend'),caret=node('#ofB s'),watch=node('#recipient [data-recipient=watch]'),summary=node('#ofSuggestions summary');
  node('#ofModelMenu').hidden=true;node('#cuAgentChoices').hidden=true;node('#ofSubject').textContent='Whole stage';watch.textContent='Motet';summary.textContent='Suggestions ▾';suggestions.open=false;send.textContent='Ask Motet ↗';headButton.setAttribute('aria-controls','ofBody');
  document.body=new Node('document.body');document.body.append(officer);officer.append(host);officer.classList.add('fold');officer.querySelector=()=>host;headButton.append(caret);host.append(headButton,body);scope.append(station,room);context.append(scope,progress);body.append(context,log,tools,address,row);tools.append(suggestions,check);suggestions.append(summary,quick);quick.append(new Node('prompt1'),new Node('prompt2'),new Node('prompt3'));address.append(recipient,model);row.append(compose,send);compose.append(input);
  const mic=microphone?node('microphone'):null;if(mic){mic.classList.add('dictation-mic');row.insertBefore(mic,send)}
  const original={host:[...host.children],body:[...body.children],tools:[...tools.children],address:[...address.children],row:[...row.children]},calls=[];
  const originalHandlers={name:()=>calls.push('name'),quick:()=>calls.push('quick'),send:()=>calls.push('send')};headButton.onclick=originalHandlers.name;quick.onclick=originalHandlers.quick;send.onclick=originalHandlers.send;
  Object.assign(document,{createComment:name=>new Node(name,true),createElement:name=>new Node(name)});
  const ctx={console,WORKSPACE:true,focus:null,innerWidth:1440,innerHeight:900,OFUI:{recipient:'watch'},WS:{model:false,section:null,controls:{open:false},settingsAnchors:new Map()},document,ofEl:officer,ofIn:input,$:node,txt:(n,v)=>n.textContent=v,workspaceClose(){ctx.WS.controls.open=false;ctx.WS.model=false},responseInvalidate(){},workspaceAttachmentsBeforeRestore:()=>calls.push('attachments restore')};
  const source=[line('workspaceSettingsMove'),line('workspaceSettingsRestore'),line('workspaceSettingsModelOwned'),mutate(block)].join('\n');
  const api=vm.runInNewContext(source+';({WSO,workspaceOfficerLayout,workspaceOfficerPaint,workspaceOfficerRestore,workspaceOfficerHistoryIntent,workspaceOfficerDesktopPaint,workspaceOfficerPlaceModel})',ctx);
  ctx.ofToggle=on=>{calls.push('history '+on);api.workspaceOfficerHistoryIntent(on);if(on)officer.classList.remove('fold');else officer.classList.add('fold')};
  return {api,ctx,node,document,officer,host,body,headButton,scope,station,room,progress,log,tools,suggestions,quick,address,recipient,model,row,compose,input,send,mic,caret,summary,original,originalHandlers,calls};
}
function ownershipProof(mutate){
  const h=harness(mutate),{api,ctx}=h;api.workspaceOfficerLayout();
  const desktop=api.WSO.desktop;assert(desktop,'desktop stage mounts the approved layout');
  assert.equal(h.model.parentNode,api.WSO.header,'real picker moves into header');
  assert.equal(h.suggestions.parentNode,desktop.prompts,'real suggestions move into prompt rail');
  assert.equal(desktop.prompts.parentNode,h.officer,'closed history prompts sit outside the white card');
  assert.equal(h.quick.children.length,3);assert.equal(h.quick.onclick,h.originalHandlers.quick);assert.equal(h.headButton.onclick,h.originalHandlers.name);assert.equal(h.send.onclick,h.originalHandlers.send);
  assert.equal(h.input.parentNode,h.compose,'input node and composer wrapper do not move');
  assert.equal(h.progress.parentNode,h.body,'progress remains visible outside recipient menu');
  assert.equal(desktop.toggle.type,'button');assert.equal(desktop.toggle.getAttribute('aria-controls'),'ofLog');assert.equal(desktop.toggle.getAttribute('aria-expanded'),'false');
  assert.match(desktop.toggle.innerHTML,/M6\.5 14\.5 12 9/);
  desktop.toggle.onclick();assert.equal(api.WSO.historyOpen,true);assert.equal(desktop.toggle.getAttribute('aria-expanded'),'true');assert.equal(desktop.toggle.getAttribute('aria-label'),'Close the conversation');
  assert.equal(desktop.prompts.parentNode,h.body);assert.equal(desktop.prompts.nextSibling,h.row,'open prompts sit directly above typing row');assert.equal(h.document.activeElement,desktop.toggle);
  desktop.toggle.onclick();assert.equal(api.WSO.historyOpen,false);assert.equal(desktop.prompts.parentNode,h.officer);assert.deepEqual(h.calls,['history true','history false']);
  for(let i=0;i<5;i++)api.workspaceOfficerPaint();assert.equal(api.WSO.desktop,desktop,'ordinary paint reuses layout nodes');
  h.suggestions.hidden=true;api.workspaceOfficerPaint();assert.equal(desktop.prompts.hidden,true,'direct recipient does not expose quick prompts');h.suggestions.hidden=false;
  ctx.innerWidth=1100;api.workspaceOfficerPaint();assert.equal(api.WSO.desktop,null);assert.equal(h.officer.dataset.workspaceComposer,undefined);assert.equal(h.model.parentNode,h.row,'medium/phone picker returns to original bottom row');assert.equal(h.suggestions.parentNode,api.WSO.header,'medium/phone Suggestions returns to header');
  assert.equal(h.suggestions.open,false,'narrow details is a real closed menu');assert.equal(desktop.toggle.parentNode,null);assert.equal(desktop.prompts.parentNode,null);
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,api.WSO.header);assert.notEqual(api.WSO.desktop,desktop);ctx.focus='fixture-agent';api.workspaceOfficerRestore();
  for(const [node,children]of[[h.host,h.original.host],[h.body,h.original.body],[h.tools,h.original.tools],[h.address,h.original.address],[h.row,h.original.row]])assert.deepEqual(node.children,children,'desktop and stage restoration retain original order');
  assert.equal(h.officer.children.length,1,'no detached prompt rail remains on officer');assert.equal(h.headButton.onclick,h.originalHandlers.name);assert.equal(h.quick.onclick,h.originalHandlers.quick);assert.equal(h.send.onclick,h.originalHandlers.send);assert.equal(h.caret.getAttribute('aria-hidden'),null);assert.equal(h.summary.textContent,'Suggestions ▾');assert.equal(h.calls.at(-1),'attachments restore');
}
ownershipProof();
function settingsProof(mutate){
  const h=harness(mutate),{api,ctx}=h;api.workspaceOfficerLayout();
  const settings=h.node('settings'),anchor=()=>ctx.WS.settingsAnchors.get(h.model);
  ctx.workspaceSettingsMove(h.model,settings);Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings,'layout does not steal a Settings-owned picker');
  ctx.innerWidth=390;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings);assert.equal(anchor().parentNode,h.row,'return anchor follows narrow layout while Settings keeps picker');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row,'closing Settings returns actual picker to phone row');
  ctx.workspaceSettingsMove(h.model,settings);Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings);assert.equal(anchor().parentNode,api.WSO.header,'borrowed return anchor follows desktop header');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,api.WSO.header);
  const positions=[];ctx.workspaceOfficerPlacePopup=(popup,anchor)=>positions.push(anchor);h.node('#ofModelMenu').hidden=false;api.workspaceOfficerPlaceModel();assert.equal(positions.at(-1),api.WSO.header,'desktop model popup uses header geometry');
  ctx.innerWidth=390;api.workspaceOfficerPaint();assert.equal(positions.at(-1),h.row,'phone popup uses original row geometry');
  ctx.WS.model=true;ctx.WS.section='officer';ctx.WS.controls.open=true;const count=positions.length;api.workspaceOfficerPlaceModel();assert.equal(positions.length,count,'Settings retains popup placement ownership');
}
settingsProof();
function microphoneResizeProof(mutate){
  const h=harness(mutate,{microphone:true}),{api,ctx}=h;api.workspaceOfficerLayout();
  const order=()=>h.row.children.filter(n=>!n.comment).slice(-3);
  assert.equal(h.model.parentNode,api.WSO.header,'desktop keeps model choice in the header when mic is present');
  assert.equal(h.mic.nextSibling,h.send,'desktop mic remains immediately before Send');
  ctx.innerWidth=390;api.workspaceOfficerPaint();
  assert.deepEqual(order(),[h.model,h.mic,h.send],'phone row restores model, microphone, then Send');
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,api.WSO.header);
  const settings=h.node('settings');ctx.workspaceSettingsMove(h.model,settings);
  Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  ctx.innerWidth=390;api.workspaceOfficerPaint();
  const anchor=ctx.WS.settingsAnchors.get(h.model);
  assert.equal(h.model.parentNode,settings,'Settings retains the borrowed picker during resize');
  assert.equal(anchor.parentNode,h.row,'the Settings return anchor moves to the phone row');
  assert.equal(anchor.nextSibling,h.mic,'the return anchor stays before the microphone, not after it');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();
  assert.deepEqual(order(),[h.model,h.mic,h.send],'closing Settings restores the same model, mic and Send nodes in order');
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,api.WSO.header);
  assert.equal(h.mic.nextSibling,h.send,'returning to desktop retains the microphone position');
}
microphoneResizeProof();
for(const mode of ['phone','desk','classic','officer-desk','stepped']){
  const h=harness();if(mode==='phone')h.ctx.innerWidth=390;if(mode==='desk')h.ctx.focus='fixture-agent';if(mode==='classic')h.ctx.WORKSPACE=false;if(mode==='officer-desk')h.document.body.classList.add('workspace-officer-desk');if(mode==='stepped')h.document.body.classList.add('stepped');
  if(mode==='desk'||mode==='classic')h.api.workspaceOfficerLayout();else{h.api.workspaceOfficerLayout();h.api.workspaceOfficerDesktopPaint()}
  assert.equal(h.api.WSO.desktop,null,mode+' does not get desktop stage redesign');
}
function mutationCaught(name,mutate,proof){assert.notEqual(mutate(block),block,name+' mutation has a target');assert.throws(()=>proof(mutate),assert.AssertionError,name+' must break the behavioral proof')}
mutationCaught('lost narrow model restoration',s=>s.replace("else if(!borrowed)$('#ofRow').insertBefore(model,slot);",''),ownershipProof);
mutationCaught('cloned or missing real suggestions',s=>s.replace("prompts.appendChild($('#ofSuggestions'));",''),ownershipProof);
mutationCaught('arrow activates instead of opening history',s=>s.replace('workspaceOfficerToggleHistory();if(WSO.desktop?.toggle===toggle)','workspaceOfficerActivate(toggle);if(WSO.desktop?.toggle===toggle)'),ownershipProof);
mutationCaught('Settings return anchor lost on resize',s=>s.replace("if(anchor)$('#ofRow').insertBefore(anchor,slot);","if(anchor){}"),settingsProof);
mutationCaught('phone model placed after the microphone',s=>s.replace("const slot=$('#ofRow').querySelector('.dictation-mic')||$('#ofSend');","const slot=$('#ofSend');"),microphoneResizeProof);
mutationCaught('Settings return anchor placed after the microphone',s=>s.replace("if(anchor)$('#ofRow').insertBefore(anchor,slot);","if(anchor)$('#ofRow').insertBefore(anchor,$('#ofSend'));"),microphoneResizeProof);
const css=html.split('/* Approved desktop stage message box.')[1].split('/* End approved desktop stage message box. */')[0];
assert(css.includes('@media(min-width:1101px)'));for(const rule of css.split('\n').filter(s=>s.includes('{')&&!s.startsWith('@')))assert(rule.includes('#officer[data-workspace-composer=desktop]'),'every new visual rule belongs to desktop stage owner');
for(const expected of['right:18px;bottom:calc(100% + 12px)','gap:20px','color:#7a776f;font:400 13px/18px','width:32px;height:28px','stroke-width:1.75'])assert(css.includes(expected),expected);
assert(!block.includes('getUserMedia'),'box layout does not activate microphone');
console.log('ok desktop stage message box: real-node ownership, history arrow, prompt placement, phone/desk/classic guards, mic-present model order, Settings resize restoration and six guard mutations');
