#!/usr/bin/env node
'use strict';
/* Real desktop-stage layout functions, executed with identity-preserving DOM nodes. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8').replace(/\r\n?/g,'\n');
const block=html.slice(html.indexOf('const WSR='),html.indexOf('if(WORKSPACE){workspaceOfficerInit();workspaceOfficerLayout()}'));
const line=name=>html.split('\n').find(s=>s.startsWith('function '+name+'('));
function harness(mutate=x=>x,{microphone=false,motion=false,reduced=false}={}){
  const document={activeElement:null},animations=[],preference={matches:reduced};
  class Node{
    constructor(name,comment=false){this.name=name;this.comment=comment;this.children=[];this.parentNode=null;this.hidden=false;this.attrs={};this.dataset={};this.handlers={};this.isConnected=true;this.scrollHeight=38;this.rows=2;this.textContent='';this.value='unsent draft';this.classList={values:new Set(),contains:k=>this.classList.values.has(k),add:k=>this.classList.values.add(k),remove:k=>this.classList.values.delete(k)};this.style={values:{top:'',bottom:'100%','max-height':'300px'},getPropertyValue(k){return this.values[k]||''},getPropertyPriority(){return ''},setProperty(k,v){this.values[k]=v},removeProperty(k){delete this.values[k]}};}
    animate(frames,options){let resolve,reject;const finished=new Promise((yes,no)=>{resolve=yes;reject=no});const value={node:this,frames,options,finished,resolve,cancelled:false,cancel(){this.cancelled=true;reject(new Error('cancelled'))}};animations.push(value);return value}
    get firstElementChild(){return this.children.find(n=>!n.comment)||null}
    get innerHTML(){return this._html??this.textContent}set innerHTML(value){this._html=String(value);this._text=this._html.replace(/<[^>]*>/g,'')}
    get textContent(){return this._text||''}set textContent(value){this._text=String(value);this._html=null}
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
  const nodes={},node=id=>{if(!nodes[id]){nodes[id]=new Node(id);if(!motion)nodes[id].animate=undefined}return nodes[id]};
  const officer=node('#officer'),host=node('host'),body=node('#ofBody'),headButton=node('#ofB'),context=node('#ofContext'),scope=node('#ofScope'),station=node('#ofScope [data-scope=station]'),room=node('#ofScope [data-scope=room]'),progress=node('#ofProgress'),log=node('#ofLog'),tools=node('#ofTools'),suggestions=node('#ofSuggestions'),quick=node('#ofQuick'),check=node('#nativeCheckArea'),address=node('#ofAddress'),recipient=node('#recipient'),model=node('#ofModelPicker'),row=node('#ofRow'),compose=node('#ofCompose'),input=node('#ofIn'),send=node('#ofSend'),caret=node('#ofB s'),watch=node('#recipient [data-recipient=watch]'),summary=node('#ofSuggestions summary');
  node('#ofModelMenu').hidden=true;node('#cuAgentChoices').hidden=true;node('#ofSubject').textContent='Whole stage';watch.textContent='Motet';summary.textContent='Suggestions ▾';suggestions.open=false;send.textContent='Ask Motet ↗';headButton.setAttribute('aria-controls','ofBody');
  document.body=new Node('document.body');document.body.append(officer);officer.append(host);officer.classList.add('fold');officer.querySelector=()=>host;headButton.append(caret);host.append(headButton,body);scope.append(station,room);context.append(scope,progress);body.append(context,log,tools,address,row);tools.append(suggestions,check);suggestions.append(summary,quick);quick.append(new Node('prompt1'),new Node('prompt2'),new Node('prompt3'));address.append(recipient,model);row.append(compose,send);compose.append(input);
  const mic=microphone?node('microphone'):null;if(mic){mic.classList.add('dictation-mic');row.insertBefore(mic,send)}
  const original={host:[...host.children],body:[...body.children],tools:[...tools.children],address:[...address.children],row:[...row.children]},calls=[];
  const originalHandlers={name:()=>calls.push('name'),quick:()=>calls.push('quick'),send:()=>calls.push('send')};headButton.onclick=originalHandlers.name;quick.onclick=originalHandlers.quick;send.onclick=originalHandlers.send;
  Object.assign(document,{createComment:name=>new Node(name,true),createElement:name=>new Node(name)});
  const ctx={console,WORKSPACE:true,focus:null,innerWidth:1440,innerHeight:900,OFUI:{recipient:'watch'},WS:{model:false,section:null,controls:{open:false},settingsAnchors:new Map()},document,ofEl:officer,ofIn:input,$:node,txt:(n,v)=>n.textContent=v,workspaceClose(){ctx.WS.controls.open=false;ctx.WS.model=false},responseInvalidate(){},workspaceAttachmentsBeforeRestore:()=>calls.push('attachments restore')};
  const source=[line('workspaceSettingsMove'),line('workspaceSettingsRestore'),line('workspaceSettingsModelOwned'),mutate(block)].join('\n');
  if(motion){ctx.matchMedia=()=>preference;ctx.getComputedStyle=el=>({height:el===officer?(api.WSO.historyOpen?'400px':'120px'):'0px',opacity:'1'})}
  const api=vm.runInNewContext(source+';({WSO,workspaceOfficerLayout,workspaceOfficerPaint,workspaceOfficerRestore,workspaceOfficerHistoryIntent,workspaceOfficerDesktopPaint,workspaceOfficerPlaceModel,workspaceOfficerDesktopHistoryPaint,workspaceOfficerMotionBefore,workspaceOfficerMotionAfter,workspaceOfficerMotionCancel,workspaceOfficerShowHistory})',ctx);
  ctx.ofToggle=on=>{calls.push('history '+on);api.workspaceOfficerHistoryIntent(on);if(on)officer.classList.remove('fold');else officer.classList.add('fold')};
  return {api,ctx,node,animations,preference,document,officer,host,body,headButton,scope,station,room,progress,log,tools,suggestions,quick,address,recipient,model,row,compose,input,send,mic,caret,summary,original,originalHandlers,calls};
}
function ownershipProof(mutate){
  const h=harness(mutate),{api,ctx}=h;api.workspaceOfficerLayout();
  const desktop=api.WSO.desktop;assert(desktop,'desktop stage mounts the approved layout');
  assert.equal(h.model.parentNode,h.row,'real picker belongs in the bottom row');
  assert.equal(h.model.nextSibling,h.send,'without dictation controls the model is directly before Send');
  api.WSO.header.insertBefore(h.model,desktop.toggle);api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row,'painting repairs a stale header placement without changing model ownership');
  assert.equal(h.suggestions.parentNode,desktop.prompts,'real suggestions move into prompt rail');
  assert.equal(desktop.prompts.parentNode,h.officer,'closed history prompts sit outside the white card');
  assert.equal(h.quick.children.length,3);assert.equal(h.quick.onclick,h.originalHandlers.quick);assert.equal(h.headButton.onclick,h.originalHandlers.name);assert.equal(h.send.onclick,h.originalHandlers.send);
  assert.equal(h.input.parentNode,h.compose,'input node and composer wrapper do not move');
  assert.equal(h.progress.parentNode,h.body,'progress remains visible outside recipient menu');
  assert.equal(desktop.toggle.type,'button');assert.equal(desktop.toggle.getAttribute('aria-controls'),'ofLog');assert.equal(desktop.toggle.getAttribute('aria-expanded'),'false');
  assert.match(desktop.toggle.innerHTML,/M6\.5 14\.5 12 9/);
  desktop.toggle.onclick();assert.equal(api.WSO.historyOpen,true);assert.equal(desktop.toggle.getAttribute('aria-expanded'),'true');assert.equal(desktop.toggle.getAttribute('aria-label'),'Close the conversation');
  assert.equal(desktop.prompts.parentNode,h.officer,'prompts never move inside the opened card');assert.equal(desktop.prompts.hidden,true);assert.equal(desktop.prompts.inert,true);assert.equal(desktop.prompts.getAttribute('aria-hidden'),'true');assert.equal(h.document.activeElement,desktop.toggle);
  desktop.toggle.onclick();assert.equal(api.WSO.historyOpen,false);assert.equal(desktop.prompts.parentNode,h.officer);assert.equal(desktop.prompts.hidden,false);assert.equal(desktop.prompts.inert,false);assert.equal(h.suggestions.open,true);assert.equal(h.officer.firstElementChild,desktop.prompts,'prompt tab order precedes the header');assert.deepEqual(h.calls,['history true','history false']);
  for(let i=0;i<5;i++)api.workspaceOfficerPaint();assert.equal(api.WSO.desktop,desktop,'ordinary paint reuses layout nodes');
  h.suggestions.hidden=true;api.workspaceOfficerPaint();assert.equal(desktop.prompts.hidden,true,'direct recipient does not expose quick prompts');h.suggestions.hidden=false;
  api.WSO.header.insertBefore(h.model,desktop.toggle);
  ctx.innerWidth=1100;api.workspaceOfficerPaint();assert.equal(api.WSO.desktop,null);assert.equal(h.officer.dataset.workspaceComposer,'narrow');assert.equal(h.model.parentNode,h.row,'medium/phone picker returns to original bottom row');assert.equal(h.suggestions.parentNode,api.WSO.header,'medium/phone Suggestions returns to header');
  assert.equal(h.suggestions.open,false,'narrow details is a real closed menu');assert.equal(desktop.toggle.parentNode,null);assert.equal(desktop.prompts.parentNode,null);
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row);assert.notEqual(api.WSO.desktop,desktop);ctx.focus='fixture-agent';api.workspaceOfficerRestore();
  for(const [node,children]of[[h.host,h.original.host],[h.body,h.original.body],[h.tools,h.original.tools],[h.address,h.original.address],[h.row,h.original.row]])assert.deepEqual(node.children,children,'desktop and stage restoration retain original order');
  assert.equal(h.officer.children.length,1,'no detached prompt rail remains on officer');assert.equal(h.headButton.onclick,h.originalHandlers.name);assert.equal(h.quick.onclick,h.originalHandlers.quick);assert.equal(h.send.onclick,h.originalHandlers.send);assert.equal(h.caret.getAttribute('aria-hidden'),null);assert.equal(h.summary.textContent,'Suggestions ▾');assert.equal(h.calls.at(-1),'attachments restore');
}
ownershipProof();
function settingsProof(mutate){
  const h=harness(mutate),{api,ctx}=h;api.workspaceOfficerLayout();
  const settings=h.node('settings'),anchor=()=>ctx.WS.settingsAnchors.get(h.model);
  ctx.workspaceSettingsMove(h.model,settings);Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings,'layout does not steal a Settings-owned picker');
  api.WSO.header.insertBefore(anchor(),api.WSO.desktop.toggle);
  ctx.innerWidth=390;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings);assert.equal(anchor().parentNode,h.row,'return anchor follows narrow layout while Settings keeps picker');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row,'closing Settings returns actual picker to phone row');
  ctx.workspaceSettingsMove(h.model,settings);Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,settings);assert.equal(anchor().parentNode,h.row,'borrowed return anchor stays in the bottom row on desktop');
  api.WSO.header.insertBefore(anchor(),api.WSO.desktop.toggle);api.workspaceOfficerPaint();assert.equal(anchor().parentNode,h.row,'a stale borrowed return anchor is repaired without taking the picker from Settings');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row);
  const positions=[];ctx.workspaceOfficerPlacePopup=(popup,anchor)=>positions.push(anchor);h.node('#ofModelMenu').hidden=false;api.workspaceOfficerPlaceModel();assert.equal(positions.at(-1),h.model,'desktop model popup uses the picker geometry');
  ctx.innerWidth=390;api.workspaceOfficerPaint();assert.equal(positions.at(-1),h.model,'phone popup uses the same picker geometry');
  ctx.WS.model=true;ctx.WS.section='officer';ctx.WS.controls.open=true;const count=positions.length;api.workspaceOfficerPlaceModel();assert.equal(positions.length,count,'Settings retains popup placement ownership');
}
settingsProof();
function microphoneResizeProof(mutate){
  const h=harness(mutate,{microphone:true}),{api,ctx}=h;api.workspaceOfficerLayout();
  const order=()=>h.row.children.filter(n=>!n.comment).slice(-3);
  assert.equal(h.model.parentNode,h.row,'desktop keeps model choice in the bottom row when mic is present');
  assert.deepEqual(order(),[h.model,h.mic,h.send],'desktop DOM order is model, microphone, then Send');
  assert.equal(h.mic.nextSibling,h.send,'desktop mic remains immediately before Send');
  ctx.innerWidth=390;api.workspaceOfficerPaint();
  assert.deepEqual(order(),[h.model,h.mic,h.send],'phone row restores model, microphone, then Send');
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row);
  const settings=h.node('settings');ctx.workspaceSettingsMove(h.model,settings);
  Object.assign(ctx.WS,{model:true,section:'officer'});ctx.WS.controls.open=true;
  ctx.innerWidth=390;api.workspaceOfficerPaint();
  const anchor=ctx.WS.settingsAnchors.get(h.model);
  assert.equal(h.model.parentNode,settings,'Settings retains the borrowed picker during resize');
  assert.equal(anchor.parentNode,h.row,'the Settings return anchor moves to the phone row');
  assert.equal(anchor.nextSibling,h.mic,'the return anchor stays before the microphone, not after it');
  ctx.workspaceSettingsRestore(h.model);ctx.WS.model=false;ctx.WS.controls.open=false;api.workspaceOfficerPaint();
  assert.deepEqual(order(),[h.model,h.mic,h.send],'closing Settings restores the same model, mic and Send nodes in order');
  ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(h.model.parentNode,h.row);
  assert.equal(h.mic.nextSibling,h.send,'returning to desktop retains the microphone position');
}
microphoneResizeProof();
function narrowProof(mutate=x=>x){
  for(const width of [390,900,1100]){
    const h=harness(mutate,{microphone:true}),{api,ctx}=h;
    h.summary.setAttribute('aria-label','Original suggestions');h.summary.setAttribute('title','Original title');
    const originalBottom=[...h.row.children],actualSummary=h.summary,actualQuick=h.quick;
    api.workspaceOfficerLayout();ctx.innerWidth=width;api.workspaceOfficerPaint();
    const bottom=[...h.row.children],narrow=api.WSO.narrow;assert(narrow,width+' mounts the narrow stage owner');
    assert.equal(api.WSO.desktop,null);assert.equal(h.officer.dataset.workspaceComposer,'narrow');
    assert.equal(narrow.toggle.parentNode,api.WSO.header);assert.equal(api.WSO.header.children.at(-1),narrow.toggle,'The only conversation arrow is last in the top row');
    assert.equal(h.suggestions.nextSibling,narrow.toggle,'The real Suggestions menu is immediately before the arrow');
    assert.equal(narrow.summary,actualSummary);assert.equal(h.quick,actualQuick);assert.equal(h.quick.onclick,h.originalHandlers.quick);
    assert.equal(h.summary.getAttribute('aria-label'),'Suggested questions');assert.match(h.summary.innerHTML,/viewBox="0 0 24 24"/);assert.match(h.summary.innerHTML,/M9 18h6/);assert(!/Suggestions|▾|14\.5 12 9/.test(h.summary.innerHTML),'The summary contains a lightbulb without text or a chevron');
    assert.equal(narrow.toggle.type,'button');assert.equal(narrow.toggle.getAttribute('aria-controls'),'ofLog');assert.equal(narrow.toggle.getAttribute('aria-expanded'),'false');assert.equal(narrow.toggle.getAttribute('aria-label'),'Open the conversation');
    assert.match(narrow.toggle.innerHTML,/M6\.5 14\.5 12 9/);assert.equal(h.caret.getAttribute('aria-hidden'),'true');
    const icon=h.summary.innerHTML;h.suggestions.open=true;narrow.toggle.onclick();
    assert.equal(api.WSO.historyOpen,true);assert.equal(narrow.toggle.getAttribute('aria-expanded'),'true');assert.equal(narrow.toggle.getAttribute('aria-label'),'Close the conversation');assert.equal(h.summary.innerHTML,icon);assert.equal(h.summary.hidden,false);assert.equal(h.suggestions.hidden,false);assert.equal(h.suggestions.open,true,'Opening the conversation does not remove the icon or replace the native menu');
    assert.equal(h.document.activeElement,narrow.toggle);assert.deepEqual(h.row.children,bottom,'The narrow redesign does not alter the bottom row');assert.equal(h.input.parentNode,h.compose);assert.equal(h.mic.nextSibling,h.send);
    narrow.toggle.onclick();assert.equal(api.WSO.historyOpen,false);assert.equal(narrow.toggle.getAttribute('aria-expanded'),'false');assert.equal(h.summary.innerHTML,icon);
    api.workspaceOfficerHistoryIntent(true);assert.equal(narrow.toggle.getAttribute('aria-expanded'),'true','Automatic history intent updates the same arrow');
    api.workspaceOfficerPaint();assert.equal(api.WSO.narrow,narrow,'Ordinary paint does not rebuild controls');
    ctx.innerWidth=1440;api.workspaceOfficerPaint();assert.equal(api.WSO.narrow,null);assert(api.WSO.desktop);assert.equal(narrow.toggle.parentNode,null);assert.equal(h.summary.getAttribute('aria-label'),'Original suggestions');assert.equal(h.summary.getAttribute('title'),'Original title');assert.equal(h.summary.textContent,'Suggestions');
    ctx.innerWidth=width;api.workspaceOfficerPaint();assert(api.WSO.narrow);assert.equal(api.WSO.desktop,null);assert.equal(h.model.parentNode,h.row);assert.equal(h.mic.nextSibling,h.send);
    ctx.focus='fictional-agent';api.workspaceOfficerRestore();assert.equal(api.WSO.narrow,null);assert.equal(h.summary.textContent,'Suggestions ▾');assert.equal(h.summary.getAttribute('aria-label'),'Original suggestions');assert.deepEqual(h.row.children,originalBottom);
  }
}
narrowProof();
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
mutationCaught('narrow arrow mounts in the real header',s=>s.replace('WSO.header.appendChild(toggle);WSO.narrow=narrow;','WSO.narrow=narrow;'),narrowProof);
mutationCaught('narrow arrow opens the conversation',s=>s.replace('workspaceOfficerToggleHistory();if(WSO.narrow?.toggle===toggle)','workspaceOfficerActivate(toggle);if(WSO.narrow?.toggle===toggle)'),narrowProof);
mutationCaught('narrow icon remains available when expanded',s=>s.replace('const narrow=WSO.narrow;if(!narrow)return;const open=!!WSO.historyOpen;','const narrow=WSO.narrow;if(!narrow)return;const open=!!WSO.historyOpen;if(open)narrow.summary.hidden=true;'),narrowProof);
mutationCaught('narrow eligibility preserves wide layout',s=>s.replace('WSO.active&&!focus&&innerWidth<=1100','WSO.active&&!focus&&true'),narrowProof);
mutationCaught('narrow summary accessible label',s=>s.replace("summary.setAttribute('aria-label','Suggested questions');",''),narrowProof);
mutationCaught('narrow summary attribute restoration',s=>s.replace("narrow.summary.setAttribute(name,value)",'void 0'),narrowProof);
mutationCaught('wide model returns beside microphone',s=>s.replace('row.insertBefore(model,slot);','WSO.header.insertBefore(model,WSO.desktop.toggle);'),ownershipProof);
mutationCaught('borrowed model return anchor stays in bottom row',s=>s.replace('row.insertBefore(anchor,slot)','WSO.header.insertBefore(anchor,WSO.desktop.toggle)'),settingsProof);
mutationCaught('model popup follows its actual picker',s=>s.replace("workspaceOfficerPlacePopup($('#ofModelMenu'),$('#ofModelPicker'))","workspaceOfficerPlacePopup($('#ofModelMenu'),WSO.header)"),settingsProof);
const css=html.split('/* Approved desktop stage message box.')[1].split('/* End approved desktop stage message box. */')[0];
assert(css.includes('@media(min-width:1101px)'));for(const rule of css.split('\n').filter(s=>s.includes('{')&&!s.startsWith('@')))assert(rule.includes('#officer[data-workspace-composer=desktop]'),'every new visual rule belongs to desktop stage owner');
for(const expected of['right:18px;bottom:calc(100% + 12px)','gap:20px','color:#7a776f;font:400 13px/18px','width:32px;height:28px','stroke-width:1.75'])assert(css.includes(expected),expected);
assert(!block.includes('getUserMedia'),'box layout does not activate microphone');
const narrowCss=html.split('/* Narrow stage keeps its real Suggestions menu and one conversation arrow. */')[1].split('/* End narrow stage arrow and Suggestions icon. */')[0];
for(const expected of['#officer[data-workspace-composer=narrow]','width:32px;height:28px','width:20px;height:20px','stroke-width:1.75','color:rgb(102,99,93)','background:transparent','summary::-webkit-details-marker{display:none}','#ofB s{display:none}','outline:2px solid #1c1b19'])assert(narrowCss.includes(expected),expected);
assert(!narrowCss.includes('#ofRow'),'H does not restyle the existing bottom row');
function modelCssProof(source=html){const css=source.split('/* Stage model choice belongs beside the microphone at every width. */')[1].split('/* End stage model choice placement. */')[0];
  for(const expected of ['#ofRow>#ofModelPicker{position:relative','flex:0 1 auto!important','align-self:flex-end!important','order:3!important','#officer[data-workspace-composer].dictation-active #ofModelPicker{display:none!important}',':not(.dictation-active) #ofRow:has(>#ofModelPicker:not([hidden]))>#ofModelPicker{margin-left:auto!important}',':not(.dictation-active) #ofRow:has(>#ofModelPicker:not([hidden]))>.dictation-mic{margin-left:0!important}'])assert(css.includes(expected),expected);
  assert(!source.includes('#workspaceOfficerHeader>#ofModelPicker{position:relative'),'The model has no obsolete header placement');
}
modelCssProof();for(const target of ['order:3!important','align-self:flex-end!important',':not(.dictation-active) #ofRow:has(>#ofModelPicker:not([hidden]))>.dictation-mic{margin-left:0!important}'])assert.throws(()=>modelCssProof(html.replace(target,'')),assert.AssertionError);
console.log('ok stage message box: wide/narrow real-node ownership, accessible bulb and one arrow, bottom-row model/menu, responsive/Settings restoration; fifteen behavior and three CSS guard mutations');

async function motionProof(mutate=x=>x){
  const h=harness(mutate,{motion:true}),a=h.api;a.workspaceOfficerLayout();
  const desktop=a.WSO.desktop;
  h.officer.classList.remove('fold');a.workspaceOfficerShowHistory();assert(h.animations.length,'submission reveal animates');assert.equal(h.animations[0].options.duration,200,'typed submission uses the same smooth history reveal');h.animations[0].resolve();await Promise.resolve();a.WSO.historyOpen=false;a.workspaceOfficerDesktopHistoryPaint();h.animations.length=0;
  const open=a.workspaceOfficerMotionBefore(true);assert(open,'opening captures the old natural size');
  a.WSO.historyOpen=true;a.workspaceOfficerDesktopHistoryPaint();
  assert(desktop.prompts.hidden);assert(desktop.prompts.inert);assert.equal(desktop.prompts.getAttribute('aria-hidden'),'true');
  a.workspaceOfficerMotionAfter(open,true);
  assert.equal(h.animations[0].options.duration,200);assert.equal(h.animations[0].options.fill,'both','The final geometry holds until cleanup');assert.equal(h.animations[0].frames[0].height,'120px');assert.equal(h.animations[0].frames[1].height,'400px');
  assert(desktop.prompts.inert,'fading prompts stay out of focus');assert.equal(desktop.prompts.parentNode,h.officer);
  h.animations[0].resolve();await Promise.resolve();assert(desktop.prompts.hidden);assert.equal(h.host.style.overflow,open.overflow);assert(open.animations.every(animation=>animation.cancelled),'Completed filled animations release their effects after natural layout restores');
  const close=a.workspaceOfficerMotionBefore(false);a.WSO.historyOpen=false;a.workspaceOfficerDesktopHistoryPaint();h.suggestions.open=false;
  a.workspaceOfficerMotionAfter(close,false);assert.equal(h.officer.dataset.workspaceHistoryMotion,'closing');
  const oldHeight=close.animations[0];
  // A rapid reversal must retire every callback from the closing generation.
  const reverse=a.workspaceOfficerMotionBefore(true);assert(oldHeight.cancelled);
  a.WSO.historyOpen=true;a.workspaceOfficerDesktopHistoryPaint();a.workspaceOfficerMotionAfter(reverse,true);
  oldHeight.resolve();await Promise.resolve();assert.equal(a.workspaceOfficerMotionBefore.current,reverse);assert(desktop.prompts.inert);
  reverse.animations[0].resolve();await Promise.resolve();assert(desktop.prompts.hidden);
  const final=a.workspaceOfficerMotionBefore(false);a.WSO.historyOpen=false;a.workspaceOfficerDesktopHistoryPaint();a.workspaceOfficerMotionAfter(final,false);
  final.animations[0].resolve();await Promise.resolve();assert.equal(desktop.prompts.hidden,false);assert.equal(desktop.prompts.inert,false);assert.equal(h.suggestions.open,true,'closing completion restores the real prompt details');assert.equal(h.officer.dataset.workspaceHistoryMotion,undefined);
  const pending=a.workspaceOfficerMotionBefore(true);a.WSO.historyOpen=true;a.workspaceOfficerDesktopHistoryPaint();a.workspaceOfficerMotionAfter(pending,true);
  h.ctx.innerWidth=390;a.workspaceOfficerPaint();pending.animations[0].resolve();await Promise.resolve();assert.equal(a.WSO.desktop,null);assert.equal(desktop.prompts.parentNode,null,'a stale completion cannot resurrect detached controls');
  const r=harness(mutate,{motion:true,reduced:true});r.api.workspaceOfficerLayout();assert.equal(r.api.workspaceOfficerMotionBefore(true),null);r.api.WSO.historyOpen=true;r.api.workspaceOfficerDesktopHistoryPaint();assert(r.api.WSO.desktop.prompts.hidden);assert.equal(r.animations.length,0,'reduced motion is immediate');
}
async function narrowMotionProof(mutate=x=>x){
  for(const width of [390,900]){
    const h=harness(mutate,{motion:true}),a=h.api;h.ctx.innerWidth=width;a.workspaceOfficerLayout();const narrow=a.WSO.narrow;
    assert(narrow);const icon=h.summary.innerHTML;h.suggestions.open=true;
    for(const on of [true,false]){
      const run=a.workspaceOfficerMotionBefore(on);assert(run,'Narrow open and close both capture motion intent');
      a.workspaceOfficerHistoryIntent(on);a.workspaceOfficerMotionAfter(run,on);
      assert.equal(run.animations[0].options.duration,200);assert.equal(run.animations[0].options.fill,'both');
      assert.equal(run.animations[0].frames[0].height,on?'120px':'400px');assert.equal(run.animations[0].frames[1].height,on?'400px':'120px');
      assert.equal(h.suggestions.parentNode,a.WSO.header);assert.equal(h.suggestions.hidden,false);assert.equal(h.summary.innerHTML,icon);assert.equal(h.suggestions.open,true);
      run.animations[0].resolve();await Promise.resolve();assert(run.animations.every(animation=>animation.cancelled));assert.equal(a.workspaceOfficerMotionBefore.current,null);assert.equal(h.officer.dataset.workspaceHistoryMotion,undefined);assert.equal(h.host.style.overflow,run.overflow);
    }
    const old=a.workspaceOfficerMotionBefore(true);a.workspaceOfficerHistoryIntent(true);a.workspaceOfficerMotionAfter(old,true);
    h.ctx.innerWidth=1440;a.workspaceOfficerPaint();
    assert(old.animations.every(animation=>animation.cancelled),'Leaving the narrow owner immediately retires every live animation');assert.equal(a.workspaceOfficerMotionBefore.current,null);assert.equal(h.host.style.overflow,old.overflow);assert.equal(narrow.toggle.parentNode,null);
    const desktop=a.WSO.desktop;old.animations[0].resolve();await Promise.resolve();assert.equal(a.WSO.desktop,desktop);assert.equal(a.WSO.narrow,null,'A stale narrow completion cannot revive old controls');
    const r=harness(mutate,{motion:true,reduced:true});r.ctx.innerWidth=width;r.api.workspaceOfficerLayout();assert.equal(r.api.workspaceOfficerMotionBefore(true),null);r.api.workspaceOfficerHistoryIntent(true);assert.equal(r.animations.length,0);assert.equal(r.api.WSO.narrow.toggle.getAttribute('aria-expanded'),'true');
  }
}
function sendFunction(name){const start=html.indexOf('async function '+name+'('),end=html.indexOf('\n}',start);assert(start>=0&&end>start);return html.slice(start,end+2)}
function sendRevealProof(mutate=x=>x){
  for(const name of ['cuSend','ofSendOrdinary'])for(const failure of [false,true]){
    const question=failure?(name==='cuSend'?'Rejected\0message':'x'.repeat(4001)):'Which colleague changed the pricing?';
    const rows=[],reveals=[],posts=[],calls=[],input={value:question},agent={name:'Fictional colleague'};
    let painted=0,status='';
    // Height intentionally depends on both the appended message and shrinking
    // draft/status layout. Revealing before those mutations picks an old target.
    const height=()=>143+rows.length*119+(input.value?47:0)+painted*7+(status?18:0);
    const snapshot=()=>({height:height(),rows:rows.length,draft:input.value,painted,status});
    const request={history:{scope:'room'},context:{scope:'room'},epoch:3};
    const ctx={console,DEMO:false,WORKSPACE:true,KEY:'fictional-owner',claiming:false,locked:false,connected:true,
      crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},OFUI:{recipient:name==='cuSend'?'agent':'watch',scope:'room',epoch:3},
      A:{'fictional-agent':agent},CU:{busy:new Set()},OFM:{records:new Map()},OF:{busy:false,turns:[]},OFE:{eraseEpoch:0},ofIn:input,ofSt:{textContent:''},
      cuTarget:()=> 'fictional-agent',cuSendReason:()=>'',esc:String,ofDay(){},ofCurrent:()=>request,cuTurnMeta:()=>({ts:1}),ofAttributed:(context,body)=>body,
      cuDraftSave:()=>calls.push('draft saved'),ofContextPaint:()=>{painted++;calls.push('paint')},ofStatus:text=>{status=text;calls.push('status')},
      ofAdd:(kind,text)=>{const row={kind,text};rows.push(row);calls.push('row');return row},ofMessagePaint:record=>{if(!rows.includes(record))rows.push(record);calls.push('row')},ofMessageSave(){},
      ofRequestHistory:turns=>turns,cuVisible:()=>true,workspaceOfficerShowHistory:()=>{reveals.push(snapshot());calls.push('reveal')},
      post:(route,body)=>{posts.push({route,body,...snapshot()});return new Promise(()=>{})}
    };
    const owner=JSON.stringify([ctx.KEY,ctx.OFUI,ctx.cuTarget()]);
    const fn=vm.runInNewContext(mutate(sendFunction(name))+';'+name,ctx);fn();
    assert.equal(reveals.length,1,name+' reveals once after its synchronous visible content is ready');
    assert.equal(reveals[0].height,height(),name+' animation targets the current post-append layout height');
    assert.equal(reveals[0].rows,1,'The question or refusal is already visible at reveal');assert(calls.indexOf('row')<calls.indexOf('reveal'));
    if(failure){assert.equal(posts.length,0,'Rejected input never reaches transport');assert.equal(input.value,question);assert.match(rows[0].text,/Nothing was sent|nothing was sent/);}
    else {assert.equal(posts.length,1,'An explicit accepted Send reaches only the stub transport');assert.equal(posts[0].route,name==='cuSend'?'/agent-message':'/ask');assert.equal(reveals[0].draft,'');assert(reveals[0].painted>0);assert.equal(posts[0].height,reveals[0].height);if(name==='ofSendOrdinary')assert.equal(reveals[0].status,'thinking…');}
    assert.equal(JSON.stringify([ctx.KEY,ctx.OFUI,ctx.cuTarget()]),owner,'Opening history never changes the owner, recipient or scope');assert.equal(ctx.A['fictional-agent'],agent);
  }
}
(async()=>{
  await motionProof();await narrowMotionProof();sendRevealProof();
  await assert.rejects(()=>motionProof(s=>s.replace('duration:200','duration:0')),assert.AssertionError,'missing smooth transition is causal');
  await assert.rejects(()=>motionProof(s=>s.replace('desktop.prompts.inert=hidden','desktop.prompts.inert=false')),assert.AssertionError,'fading prompts cannot remain focusable');
  await assert.rejects(()=>motionProof(s=>s.replace("if(!hidden)$('#ofSuggestions').open=true;",'')),assert.AssertionError,'prompt restoration cannot wait for another paint');
  await assert.rejects(()=>motionProof(s=>s.replace("if(typeof workspaceOfficerMotionAfter==='function')workspaceOfficerMotionAfter(motion,true)",'')),assert.AssertionError,'automatic typed submission reveal is causal');
  await assert.rejects(()=>motionProof(s=>s.replace(",fill:'both'",'')),assert.AssertionError,'filled final geometry is causal');
  await assert.rejects(()=>motionProof(s=>s.replace('for(const animation of run.animations)animation.cancel()};','};')),assert.AssertionError,'completed filled animations must release their effects');
  await assert.rejects(()=>narrowMotionProof(s=>s.replace("const narrow=WSO.narrow;if(!narrow)return;\n  workspaceOfficerMotionCancel();","const narrow=WSO.narrow;if(!narrow)return;")),assert.AssertionError,'narrow teardown retires old animation generation');
  for(const name of ['cuSend','ofSendOrdinary'])assert.throws(()=>sendRevealProof(source=>source.startsWith('async function '+name+'(')?source.replace('const q=(suggestion??ofIn.value).trim();','const q=(suggestion??ofIn.value).trim();workspaceOfficerShowHistory();').replace('const id=cuTarget(),a=A[id],q=ofIn.value.trim();','const id=cuTarget(),a=A[id],q=ofIn.value.trim();workspaceOfficerShowHistory();'):source),assert.AssertionError,name+' early automatic reveal selects stale geometry');
  console.log('ok message-box motion: wide/phone/narrow 200ms open-close intent, retained Suggestions icon, filled-effect cleanup, rapid reversal, stale teardown and reduced-motion paths; seven causal mutants, browser geometry measured separately');
  console.log('ok actual Send reveals: direct and Motet message/refusal content is present before sizing, pending stub transport only, no destination change; two early-reveal mutants');
})().catch(error=>{console.error(error);process.exitCode=1});
