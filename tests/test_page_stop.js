#!/usr/bin/env node
'use strict';
/* Stop for a chat Motet started: the real page functions against an inert fixture adapter. Names, times and
   identifiers are fictional. Run: node tests/test_page_stop.js (tests/test_page.js also requires it). */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8').replace(/\r\n?/g,'\n');
const fn=name=>{const a=html.indexOf('function '+name+'('),b=html.indexOf('\nfunction ',a+10);assert.ok(a>=0&&b>a,name);return html.slice(a,b)};
const controllerStart=html.indexOf('function workspaceRequestCard('),controllerEnd=html.indexOf('/* end managed Claude workspace controller */',controllerStart);
assert.ok(controllerStart>0&&controllerEnd>controllerStart,'managed controller source markers');
const controllerSource=html.slice(controllerStart,controllerEnd);
assert.ok(html.includes('id="deskStopChat"'));assert.doesNotMatch(html,/id="deskStopChat"[^>]*title=/,'the menu item has no explainer tooltip');
const tick=()=>new Promise(r=>setImmediate(r));
/* The notice's buttons draw as drawn: their rules carry #compactDesk, so a theme's '#compactDesk button' rule cannot
   outrank them; the notice's two actions sit 6px apart (8px in the menu). The unconfirmed label is the quiet pair. */
{const cssStart=html.indexOf('/* Stop for a chat Motet started: a divider'),css=html.slice(cssStart,html.indexOf('html.workspace #officer.docked #responseCard',cssStart));assert.ok(cssStart>0&&css.length>200);
 for(const cls of ['workspace-managed-stop-retry','workspace-managed-stop-copy']){const rules=css.split('\n').filter(l=>l.includes(cls));assert.ok(rules.length);for(const rule of rules)for(const sel of rule.split('{')[0].split(','))if(sel.includes(cls))assert.ok(sel.includes('#compactDesk'),sel)}
 assert.ok(css.includes('html.workspace #compactDesk .workspace-managed-notice .workspace-managed-stop-actions{gap:6px}'));
 assert.match(css,/#deskStopConfirm \.workspace-stop-actions,[^{]*\{display:flex;align-items:center;gap:8px/);
 assert.ok(html.includes('html.workspace body.stepped #workspaceTop #deskStatus[data-workspace-state=unsure]{color:#9b4b16;background:#fff0df}')||/#workspaceTop #deskStatus\[data-workspace-state=unsure\]\{color:#9b4b16;background:#fff0df\}/.test(html),'the top bar draws unsure as the quiet pair');}

const CLEO='c:11111111-1111-4111-8111-111111111111',RUN='22222222-2222-4222-8222-222222222222',PERM='33333333-3333-4333-8333-333333333333';
const PLAIN='c:99999999-9999-4999-8999-999999999999',CODEX='x:fictional-codex';
const DOV='c:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',NERI='c:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',ORIN='c:cccccccc-cccc-4ccc-8ccc-cccccccccccc';

function harness(code=controllerSource){
  const map=new Map(),ids=['55555555-5555-4555-8555-555555555555','66666666-6666-4666-8666-666666666666','77777777-7777-4777-8777-777777777777'];let serial=0,now=1000;
  function el(){const cl=new Set();const n={className:'',children:[],hidden:false,disabled:false,isConnected:true,_text:'',attrs:{},dataset:{},title:'',classList:{toggle(k,v){v?cl.add(k):cl.delete(k)},contains(k){return cl.has(k)},add(k){cl.add(k)},remove(k){cl.delete(k)}},append(...a){this.children.push(...a)},prepend(...a){this.children.unshift(...a)},after(){},before(){},remove(){this.isConnected=false},setAttribute(k,v){this.attrs[k]=v},closest(){return null},getClientRects(){return [1]},getBoundingClientRect(){return {left:10,right:300,top:10,bottom:90,width:290,height:80}},contains(x){return x===this||this.children.some(y=>y===x||y.contains?.(x))},focus(){doc.activeElement=this}};Object.defineProperty(n,'textContent',{get(){return this._text+this.children.map(x=>x.textContent||'').join('')},set(v){this._text=String(v);this.children=[]}});return n}
  const doc={hidden:false,activeElement:null,hasFocus(){return true},createElement:el,createTextNode:t=>({textContent:String(t)}),elementFromPoint(){return null},getElementById(id){return $('#'+id)}};
  function $(id){if(!map.has(id))map.set(id,el());return map.get(id)}
  const cleo={id:CLEO,name:'Cleo'},dov={id:DOV,name:'Dov',sub:true,parent:CLEO,turn:'in'},neri={id:NERI,name:'Neri',sub:true,parent:DOV,turn:'in'},orin={id:ORIN,name:'Orin',sub:true,parent:CLEO,turn:'ended'},plain={id:PLAIN,name:'Plain'},codex={id:CODEX,name:'Bram'};
  const pending={id:PERM,tool:'Shell',input:{command:'echo FICTIONAL'},inputAvailable:true,state:'pending',canAllow:true,canDeny:true,createdAt:10,expiresAt:130,resolutionReason:null,decision:null,decisionState:null,resolvedAt:null};
  const status={schemaVersion:1,runId:RUN,state:'needs_attention',recoveryRequired:false,evidenceAvailable:true,serverTime:50,timeoutSeconds:120,capabilities:{permissions:true},permissions:[pending],ownerStop:null,queued:2};
  const stops=[],copies=[],timers=[];let stopReply=null;
  const c={WORKSPACE:true,DEMO:true,KEY:'fictional',connected:true,locked:false,claiming:false,focus:CLEO,A:{[CLEO]:cleo,[DOV]:dov,[NERI]:neri,[ORIN]:orin,[PLAIN]:plain,[CODEX]:codex},OFUI:{recipient:'agent'},WNC:{pending:null,adapter:null},STEADY:false,document:doc,ofLog:el(),$,innerWidth:1440,innerHeight:900,performance:{now:()=>now},Date,Promise,AbortSignal,
    crypto:{randomUUID:()=>ids[serial++]},navigator:{clipboard:{writeText:async t=>{copies.push(t)}}},setTimeout(f,ms){timers.push({f,ms});return timers.length},clearTimeout(){},clock:x=>'t'+x,workspaceNewChatWait:()=> '2 minutes',covered:()=>false,cuTarget:()=>c.focus,workspaceNewChatPaint(){},render(){}};
  const api=vm.runInNewContext(code+';({MCP,workspaceManagedHello,workspaceManagedSync,workspaceManagedBinding,workspaceManagedDeskPaint,workspaceManagedSendReason,workspaceManagedComposerPaint,workspaceManagedStatusPill,workspaceManagedValidStatus,workspaceManagedOutcome,workspaceManagedStopOffered,workspaceManagedStopMenuPaint,workspaceManagedStopWords,workspaceManagedStopPhase,workspaceManagedStopPillState,workspaceManagedStopBegin,managedStopped,workspaceStopNote,workspaceManagedStopValid,workspaceManagedStopNewer})',c);
  api.MCP.adapter={fixture:true,
    capability:async agent=>agent===CLEO?{agent,permissionOwnership:{classification:'managed',checkRequired:true},managed:{runId:RUN}}:{agent,permissionOwnership:{classification:'unmanaged',checkRequired:true},managed:null},
    managedStatus:async()=>structuredClone(status),decidePermission:async()=>{throw Error('not used')},
    stop:(runId,requestId,agent)=>{stops.push({runId,requestId,agent});return new Promise((resolve,reject)=>{stopReply={resolve,reject,requestId}})}};
  return {api,c,$,status,cleo,dov,neri,orin,plain,codex,stops,copies,timers,reply:()=>stopReply,advance:ms=>{now+=ms}};
}
async function ready(h){h.api.workspaceManagedHello(true);h.api.workspaceManagedSync();await tick();h.api.workspaceManagedSync();await tick();h.api.workspaceManagedDeskPaint(h.cleo);h.api.workspaceManagedComposerPaint()}
const text=n=>n.textContent;

async function menuAndConfirm(code){
  const h=await (async()=>{const h=harness(code);await ready(h);return h})(),{api,$}=h;
  assert.equal(api.workspaceManagedStopOffered(h.cleo),true,'a chat Motet started offers Stop');
  api.workspaceManagedBinding(h.plain);await tick();assert.equal(api.MCP.bindings.get(h.plain).classification,'unmanaged');assert.equal(api.workspaceManagedStopOffered(h.plain),false,'an ordinary Claude chat never offers Stop');
  assert.equal(api.workspaceManagedStopOffered(h.codex),false,'a Codex chat never offers Stop');
  api.workspaceManagedStopMenuPaint(h.cleo);assert.equal($('#deskStopArea').hidden,false);assert.equal($('#deskStopChat').hidden,false);assert.equal($('#deskStopConfirm').hidden,true);
  api.workspaceManagedStopMenuPaint(h.plain);assert.equal($('#deskStopArea').hidden,true,'the item is hidden for an ordinary chat');
  api.MCP.stopConfirm=CLEO;api.workspaceManagedStopMenuPaint(h.cleo);const confirm=$('#deskStopConfirm');assert.equal($('#deskStopChat').hidden,true,'the confirm step replaces the item');assert.equal(confirm.hidden,false);
  assert.equal(confirm.children.length,2,'one paragraph, then Cancel and Stop: no second grey line');
  assert.equal(text(confirm.children[0]),'Stop Cleo? Dov and Neri stop with it. Its waiting request is denied. 2 queued messages are not delivered. It can\u2019t be started again.');
  const [cancel,go]=confirm.children[1].children;assert.equal(text(cancel),'Cancel');assert.equal(text(go),'Stop');assert.equal(go.className,'workspace-stop-go');
  cancel.onclick();assert.equal(api.MCP.stopConfirm,null);assert.equal($('#deskStopChat').hidden,false,'Cancel returns to the item');assert.equal(h.stops.length,0);
  const words=(kids,status)=>{const agents={[CLEO]:h.cleo};kids.forEach((name,i)=>{agents['k'+i]={id:'k'+i,name,sub:true,parent:CLEO,turn:'in'}});return api.workspaceManagedStopWords(h.cleo,status,agents)};
  const CANT=' It can\u2019t be started again.';
  assert.equal(words([],null).lead,CANT);assert.equal(words([],null).note,undefined,'no second paragraph');
  assert.equal(words(['Dov'],null).lead,' Dov stops with it.'+CANT);
  assert.equal(words(['Ada','Bo','Cy'],null).lead,' Ada, Bo and Cy stop with it.'+CANT);
  assert.equal(words([],{permissions:[],queued:1}).lead,' 1 queued message is not delivered.'+CANT);
  assert.equal(words([],{permissions:[{state:'pending',resolutionReason:null}],queued:0}).lead,' Its waiting request is denied.'+CANT);
  assert.equal(words([],{permissions:[{state:'cancelled',resolutionReason:'turn_ended'}],queued:0}).lead,CANT,'a resolved request is not waiting');
  for(const gone of ['It ends now','which it started','Its history stays here'])assert.ok(!controllerSource.includes(gone),gone);
  return h;
}

async function stoppedFlow(code){
  const h=harness(code),{api,$}=h;await ready(h);
  api.MCP.stopConfirm=CLEO;api.workspaceManagedStopMenuPaint(h.cleo);const go=$('#deskStopConfirm').children[1].children[1];go.onclick();go.onclick();
  assert.equal(api.workspaceManagedStopBegin(h.cleo),false,'a stop in flight is not sent again');assert.equal(h.stops.length,1,'a double click sends one stop');
  assert.deepEqual(h.stops[0],{runId:RUN,requestId:'55555555-5555-4555-8555-555555555555',agent:CLEO});
  api.workspaceManagedDeskPaint(h.cleo);api.workspaceManagedComposerPaint();
  assert.equal(api.workspaceManagedStatusPill(h.cleo),'stopping…');assert.equal(api.workspaceManagedStopPillState(h.cleo),'working');
  const notice=api.MCP.composerNotice;assert.equal(notice.textContent,'Stopping Cleo…');assert.equal(notice.hidden,false);assert.equal($('#ofIn').hidden,true);assert.equal($('#ofSend').hidden,true);assert.equal($('#sendHint').textContent,'','no hint under the box: the status line says stopping…');
  assert.notEqual(api.workspaceManagedSendReason(CLEO,h.cleo),'','no message is sent while stopping');assert.equal(api.workspaceManagedStopOffered(h.cleo),false);
  const record={by:'owner',outcome:'stopped',requestedAt:500,lastAttemptAt:500,stoppedAt:503,terminalCommand:'fictional command'};
  h.status.state='exited';h.status.ownerStop=record;h.status.permissions=[{...h.status.permissions[0],state:'shutdown',canAllow:false,canDeny:false,decision:'deny',decisionState:'written',resolutionReason:'owner_stop',resolvedAt:502}];h.status.capabilities={permissions:false};h.status.queued=0;
  h.reply().resolve({schemaVersion:1,requestId:h.reply().requestId,runId:RUN,agent:CLEO,state:'exited',ownerStop:record,already:false});await tick();await tick();await tick();
  api.workspaceManagedDeskPaint(h.cleo);api.workspaceManagedComposerPaint();
  assert.equal(api.workspaceManagedStatusPill(h.cleo),'stopped · t503');assert.equal(api.workspaceManagedStopPillState(h.cleo),'ended');
  assert.equal(notice.textContent,'Cleo is stopped');assert.equal($('#sendHint').textContent,'','no hint under the box: the status line carries the time');
  assert.equal(api.MCP.noticeHost.hidden,true,'no red notice for a stopped chat');assert.doesNotMatch(api.MCP.noticeHost.textContent,/contact|cannot reach/);
  assert.doesNotMatch(notice.textContent+$('#sendHint').textContent,/paused|cannot reach|contact lost/,'none of the contact-lost words');
  assert.equal(api.workspaceManagedSendReason(CLEO,h.cleo),'This chat is stopped.');
  $('#deskPin').hidden=false;api.workspaceManagedStopMenuPaint(h.cleo);assert.equal($('#deskStopArea').hidden,true,'after a stop the menu loses Stop this chat');assert.equal($('#deskPin').hidden,false,'Pin stays');
  assert.equal(api.workspaceManagedOutcome(h.status.permissions[0]).title,'Denied when you stopped this chat');
  return h;
}

async function unconfirmedFlow(code){
  const h=harness(code),{api,$}=h;await ready(h);
  assert.equal(api.workspaceManagedStopBegin(h.cleo),true);
  const record={by:'owner',outcome:'unconfirmed',requestedAt:600,lastAttemptAt:604,stoppedAt:null,terminalCommand:'pkill -KILL -f -- fictional',claudeEnded:false};
  h.cleo.managedStop={outcome:'unconfirmed',requestedAt:600,lastAttemptAt:604,stoppedAt:null};      /* the room's copy of the same attempt: no command, no claudeEnded */
  h.status.state='uncertain';h.status.recoveryRequired=true;h.status.ownerStop=record;h.status.capabilities={permissions:false};h.status.permissions=[];
  h.reply().resolve({schemaVersion:1,requestId:h.reply().requestId,runId:RUN,agent:CLEO,state:'uncertain',ownerStop:record,already:false});await tick();await tick();await tick();
  api.workspaceManagedDeskPaint(h.cleo);api.workspaceManagedComposerPaint();const host=api.MCP.noticeHost;
  assert.equal(api.workspaceManagedStatusPill(h.cleo),'may still be running');assert.equal(api.workspaceManagedStopPillState(h.cleo),'unsure','the quiet label, not the filled needs-you one');
  assert.equal(host.hidden,false);assert.equal(host.children.length,2,'the title and its two actions, nothing else');const [title,row]=host.children;
  assert.equal(text(title),'Motet could not confirm that Cleo stopped.');assert.doesNotMatch(text(host),/asked it to stop|did not answer|history stays|paused/);
  const [retry,copy]=row.children;assert.equal(text(retry),'Try again');assert.equal(text(copy),'Copy the Terminal command');
  assert.equal(api.MCP.composerNotice.textContent,'Messages to Cleo are paused');assert.equal($('#sendHint').textContent,'','could not confirm: no contact-lost hint under the box');assert.doesNotMatch(api.workspaceManagedSendReason(CLEO,h.cleo),/cannot reach/);
  copy.onclick();await tick();assert.deepEqual(h.copies,['pkill -KILL -f -- fictional']);assert.equal(text(copy),'Copied');h.timers.at(-1).f();assert.equal(text(copy),'Copy the Terminal command');
  assert.equal(api.workspaceManagedStopOffered(h.cleo),false,'Try again lives in the notice, not the menu');
  retry.onclick();retry.onclick();assert.equal(h.stops.length,2,'Try again sends one more stop');assert.notEqual(h.stops[1].requestId,h.stops[0].requestId,'Try again uses a new request id');
  api.workspaceManagedDeskPaint(h.cleo);assert.equal(api.workspaceManagedStatusPill(h.cleo),'stopping…');assert.equal(host.hidden,true);
  h.reply().reject(Object.assign(Error('timed out'),{status:0}));await tick();await tick();await tick();h.status.ownerStop=null;
  return h;
}

/* Claude itself has exited and only what it left (or could not be seen) keeps the stop unconfirmed: neither button can
   help, so the notice is its bold line alone. The same attempt seen with claudeEnded false gets both buttons back. */
async function claudeEndedFlow(code){
  const h=harness(code),{api}=h;await ready(h);
  assert.equal(api.workspaceManagedStopBegin(h.cleo),true);
  const record={by:'owner',outcome:'unconfirmed',requestedAt:700,lastAttemptAt:704,stoppedAt:null,terminalCommand:'pkill -KILL -f -- fictional',claudeEnded:true};
  h.cleo.managedStop={outcome:'unconfirmed',requestedAt:700,lastAttemptAt:704,stoppedAt:null};
  h.status.state='exited';h.status.recoveryRequired=false;h.status.ownerStop=record;h.status.capabilities={permissions:false};h.status.permissions=[];h.status.queued=0;
  h.reply().resolve({schemaVersion:1,requestId:h.reply().requestId,runId:RUN,agent:CLEO,state:'exited',ownerStop:record,already:false});await tick();await tick();await tick();
  api.workspaceManagedDeskPaint(h.cleo);const host=api.MCP.noticeHost;
  assert.equal(api.workspaceManagedStatusPill(h.cleo),'may still be running','the label stays as it is');
  assert.equal(host.hidden,false);assert.equal(host.children.length,1,'no buttons when Claude has ended: the bold line alone');
  assert.equal(text(host.children[0]),'Motet could not confirm that Cleo stopped.');assert.equal(text(host),'Motet could not confirm that Cleo stopped.','no other words');
  const again={...record,claudeEnded:false};api.MCP.stops.get(CLEO).result=again;api.MCP.sessions.get('desk:'+CLEO).snapshot.ownerStop=again;
  api.workspaceManagedDeskPaint(h.cleo);assert.equal(host.children.length,2,'the field change repaints');
  const [retry,copy]=host.children[1].children;assert.equal(text(retry),'Try again');assert.equal(text(copy),'Copy the Terminal command');
  return h;
}

/* An older status read of the same attempt (Claude not yet seen ended) lands after the newer Stop reply that says
   Claude has ended: Claude's end is not undone, so no button comes back. */
async function endedRace(code){
  const h=harness(code),{api}=h;await ready(h);
  api.workspaceManagedStopBegin(h.cleo);
  const prior={by:'owner',outcome:'unconfirmed',requestedAt:700,lastAttemptAt:704,stoppedAt:null,terminalCommand:'fictional command',claudeEnded:false},ended={...prior,claudeEnded:true};
  const session=api.MCP.sessions.get('desk:'+CLEO),old={...h.status,state:'uncertain',recoveryRequired:true,ownerStop:prior,permissions:[],capabilities:{permissions:false},queued:0};
  session.snapshot=structuredClone(old);h.cleo.managedStop={outcome:'unconfirmed',requestedAt:700,lastAttemptAt:704,stoppedAt:null};
  let release=null;api.MCP.adapter.managedStatus=()=>new Promise(resolve=>{release=resolve});
  session.needsRead=true;api.workspaceManagedSync();await tick();assert.equal(typeof release,'function','the older status read is in flight');
  h.reply().resolve({schemaVersion:1,requestId:h.reply().requestId,runId:RUN,agent:CLEO,state:'exited',ownerStop:ended,already:false});await tick();await tick();
  api.workspaceManagedDeskPaint(h.cleo);const host=api.MCP.noticeHost;
  assert.equal(host.hidden,false);assert.equal(host.children.length,1,'the newer reply: no buttons');
  release(structuredClone(old));await tick();await tick();await tick();
  assert.equal(session.snapshot.ownerStop.claudeEnded,false,'the older read has landed');
  api.workspaceManagedDeskPaint(h.cleo);assert.equal(host.children.length,1,'and still no buttons');assert.equal(text(host),'Motet could not confirm that Cleo stopped.');
  const record=api.workspaceManagedStopPhase(h.cleo).record;assert.equal(record.claudeEnded,true);assert.equal(record.terminalCommand,'fictional command');
  return h;
}

async function lostReply(code){
  const h=harness(code),{api}=h;await ready(h);api.workspaceManagedStopBegin(h.cleo);h.reply().reject(Error('network'));await tick();await tick();await tick();
  api.workspaceManagedDeskPaint(h.cleo);const host=api.MCP.noticeHost;assert.equal(api.workspaceManagedStatusPill(h.cleo),'may still be running','a lost reply is never shown as stopped');assert.equal(host.hidden,false);assert.equal(host.children[1].children.length,1,'no Terminal command without a server record');
  const h2=harness(code);await ready(h2);h2.api.workspaceManagedStopBegin(h2.cleo);h2.reply().reject(Object.assign(Error('ended'),{code:'already_ended'}));await tick();await tick();await tick();assert.equal(h2.api.workspaceManagedStopPhase(h2.cleo),null,'a chat that already ended is not shown as a stop');
}

function statusShapes(h){
  const {api}=h,base={schemaVersion:1,runId:RUN,state:'exited',recoveryRequired:false,evidenceAvailable:true,serverTime:50,timeoutSeconds:120,capabilities:{permissions:false},permissions:[],queued:0},stop={by:'owner',outcome:'stopped',requestedAt:5,lastAttemptAt:5,stoppedAt:6,terminalCommand:'x'};
  assert.equal(api.workspaceManagedValidStatus({...base,ownerStop:stop},RUN),true);assert.equal(api.workspaceManagedValidStatus({...base,ownerStop:{...stop,claudeEnded:false}},RUN),true);
  for(const v of [true,false])assert.equal(api.workspaceManagedStopValid({by:'owner',outcome:'unconfirmed',requestedAt:5,lastAttemptAt:9,stoppedAt:null,claudeEnded:v}),true);
  for(const v of ['true','false',1,0,null,{}])assert.equal(api.workspaceManagedStopValid({by:'owner',outcome:'unconfirmed',requestedAt:5,lastAttemptAt:9,stoppedAt:null,claudeEnded:v}),false,'claudeEnded only as a boolean: '+JSON.stringify(v));assert.equal(api.workspaceManagedValidStatus({...base,ownerStop:null},RUN),true);
  for(const bad of [{...base,state:'running',ownerStop:stop},{...base,ownerStop:{...stop,outcome:'unconfirmed'}},{...base,ownerStop:{...stop,by:'motet'}},{...base,ownerStop:{...stop,terminalCommand:'a\nb'}},{...base,ownerStop:{...stop,terminalCommand:'x'.repeat(513)}},{...base,ownerStop:{...stop,outcome:'restarted'}},{...base,ownerStop:{...stop,stoppedAt:null}},{...base,ownerStop:{...stop,claudeEnded:'true'}},{...base,ownerStop:{...stop,claudeEnded:1}},{...base,ownerStop:{...stop,claudeEnded:null}},{...base,queued:-1},{...base,queued:1.5}])
    assert.equal(api.workspaceManagedValidStatus({...bad,ownerStop:bad.ownerStop},RUN),false,JSON.stringify(bad.ownerStop||bad.queued));
  const older={outcome:'stopping',requestedAt:5,lastAttemptAt:5,stoppedAt:null},done={outcome:'stopped',requestedAt:5,lastAttemptAt:5,stoppedAt:6},retry={outcome:'unconfirmed',requestedAt:5,lastAttemptAt:9,stoppedAt:null};
  assert.equal(api.workspaceManagedStopNewer(done,older),done,'a confirmed stop is never replaced');assert.equal(api.workspaceManagedStopNewer(older,retry),retry);assert.equal(api.workspaceManagedStopNewer(retry,older),retry,'an older record is ignored');
  const full={...retry,terminalCommand:'x',claudeEnded:true};assert.equal(api.workspaceManagedStopNewer(retry,full),full,'the same attempt: the record with the command and claudeEnded');
  assert.equal(api.workspaceManagedStopNewer(full,retry),full,'whichever order the room copy comes in');
  const again={...full,claudeEnded:false};for(const [a,b] of [[full,again],[again,full]])assert.equal(api.workspaceManagedStopNewer(a,b).claudeEnded,true,'one attempt: Claude\u2019s end, once seen, is kept whatever order the records come in');
  const later={...again,lastAttemptAt:12};for(const [a,b] of [[full,later],[later,full]]){const w=api.workspaceManagedStopNewer(a,b);assert.equal(w,later,'another attempt: its own record, nothing carried over');assert.equal(w.claudeEnded,false)}
  const sending={...full,outcome:'stopping',claudeEnded:false},room={outcome:'unconfirmed',requestedAt:5,lastAttemptAt:9,stoppedAt:null};for(const [a,b] of [[sending,room],[room,sending]]){const w=api.workspaceManagedStopNewer(a,b);assert.equal(w.outcome,'unconfirmed');assert.equal(w.terminalCommand,'x','the sparse room copy never drops the command');assert.equal('claudeEnded' in w,false,'no end carried from a stopping record')}
  for(const outcome of ['stopping','stopped'])assert.equal(api.workspaceManagedStopValid({by:'owner',outcome,requestedAt:5,lastAttemptAt:9,stoppedAt:outcome==='stopped'?10:null,claudeEnded:true}),false,'claudeEnded true only on an unconfirmed stop: '+outcome);
  assert.equal(api.workspaceManagedStopValid({by:'owner',outcome:'stopped',requestedAt:5,lastAttemptAt:9,stoppedAt:10,claudeEnded:false}),true);
  assert.equal(api.workspaceManagedOutcome({decision:'deny',resolutionReason:'owner_stop',decisionState:'unknown'}).title,'Motet tried to deny this');
}

function stage(){
  const clock=x=>'t'+x,controller=new Map(['managedStopped','workspaceStopNote','workspaceManagedStopValid','workspaceManagedStopNewer'].map(n=>[n,fn(n)]));
  const code=[...controller.values(),fn('need'),fn('stateOf'),fn('workspaceFilterMatch'),fn('captionOf')].join('\n');
  const A={},c={A,clock,dur:x=>x+'s',DOING:{},median:()=>0,PINS:{},order:[],dismissed:[],Date,STEADY:false,did:()=>'handed off: log search',one:x=>x,plain:x=>x};
  const api=vm.runInNewContext(code+';({managedStopped,workspaceStopNote,stateOf,need,workspaceFilterMatch,workspaceManagedStopNewer,captionOf})',c);
  const base=(id,extra={})=>({id,name:id,calls:{},perms:{},asks:{},last:900,turn:'in',...extra});
  A[CLEO]=base(CLEO,{calls:{s:{kind:'spawn',ts:950}},perms:{p:{tool:'Shell',text:'x'}}});A[DOV]=base(DOV,{sub:true,parent:CLEO});A[NERI]=base(NERI,{sub:true,parent:DOV});A[ORIN]=base(ORIN,{soft:true,parent:CLEO});A[PLAIN]=base(PLAIN);c.order.push(CLEO,DOV,NERI,ORIN,PLAIN);
  assert.equal(api.stateOf(A[CLEO],1000).label,'waiting on you','before a stop the chat waits on its request');assert.ok(api.need(A[CLEO]));
  /* the agent event line, as the page runs it, including a late replay of an older record */
  const line=html.split('\n').find(l=>l.includes('if(e.managed_stop&&String(a.id).startsWith'));assert.ok(line,'agent event handles managed_stop');
  const apply=(a,e)=>vm.runInNewContext(line,{a,e,workspaceManagedStopValid:x=>true,workspaceManagedStopNewer:api.workspaceManagedStopNewer,String});
  apply(A[CLEO],{managed_stop:{outcome:'stopped',requestedAt:700,lastAttemptAt:700,stoppedAt:702}});apply(A[CLEO],{managed_stop:{outcome:'stopping',requestedAt:700,lastAttemptAt:700,stoppedAt:null}});apply(A[CLEO],{});
  assert.equal(A[CLEO].managedStop.outcome,'stopped','the stop is sticky against older replays and rows without it');
  const s=api.stateOf(A[CLEO],1000);assert.equal(s.label,'stopped · t702');assert.equal(s.busy,false);assert.equal(s.need,null,'a stopped chat waits on no one');assert.equal(api.need(A[CLEO]),null);
  assert.equal(api.stateOf(A[DOV],1000).label,'ended');assert.equal(api.stateOf(A[NERI],1000).label,'ended','a recorded sub-agent of a sub-agent ends too');assert.equal(api.stateOf(A[ORIN],1000).busy,true,'an inferred relation proves nothing');
  assert.equal(api.workspaceStopNote(A[CLEO]),'stopped');assert.equal(api.workspaceStopNote(A[DOV]),'ended');assert.equal(api.workspaceStopNote(A[PLAIN]),'');
  for(const id of [CLEO,DOV,NERI])assert.equal(api.workspaceFilterMatch(A[id],api.stateOf(A[id],1000),'ended'),true,'counts under Ended');assert.equal(api.workspaceFilterMatch(A[CLEO],api.stateOf(A[CLEO],1000),'working'),false);
  /* Dismiss N idle: the page's own handler */
  const dismiss=html.split('\n').find(l=>l.startsWith("$('#clearQ').onclick="));const ctx={...c,stateOf:api.stateOf,dismiss:k=>ctx.dismissed.push(k),$:()=>({}),Date:{now:()=>1000000}};vm.runInNewContext(dismiss+";$('#clearQ').onclick=null",ctx);
  const handler=vm.runInNewContext('('+dismiss.slice("$('#clearQ').onclick=".length).replace(/;\s*$/,'')+')',ctx);handler();assert.ok(ctx.dismissed.includes(CLEO),'goes with the idle ones for Dismiss');assert.ok(!ctx.dismissed.includes(ORIN));
  apply(A[PLAIN],{managed_stop:{outcome:'stopped',requestedAt:1,lastAttemptAt:1,stoppedAt:2}});
  /* a step in flight reads as stopped, not spinning */
  const start=html.indexOf('    const e=it.e,halted='),end=html.indexOf('    const delta=',start);assert.ok(start>0&&end>start);
  const label=vm.runInNewContext(html.slice(start,end)+';[label,spinning,halted]',{it:{e:{kind:'spawn',done:false},ts:950},a:A[CLEO],STEADY:false,singleLine:true,managedStopped:api.managedStopped,workspaceStepRunning:e=>!e.done,workspaceStepLabel:()=> 'handed off: log search',clock});
  assert.deepEqual([...label],['handed off: log search · started t950 · stopped before a result',false,true]);
  const live=vm.runInNewContext(html.slice(start,end)+';label',{it:{e:{kind:'spawn',done:false},ts:950},a:base('c:live'),STEADY:false,singleLine:true,managedStopped:api.managedStopped,workspaceStepRunning:e=>!e.done,workspaceStepLabel:()=> 'handed off: log search',clock});
  assert.equal(live,'handed off: log search · started t950, no result yet','a running chat keeps its open step');
  /* the extra status after the words: never 'no result yet' after 'stopped before a result' */
  const statusLine=html.split('\n').find(l=>l.startsWith('    const status=STEADY||running'));assert.ok(statusLine);
  const extra=halted=>vm.runInNewContext(statusLine.replace('const status=','status=')+';status',{STEADY:false,running:false,halted,e:{},workspaceStepExtraStatus:()=> 'no result yet'});
  assert.equal(extra(true),'','a stopped chat\u2019s step (and a sub-agent\u2019s) ends at stopped before a result');assert.equal(extra(false),'no result yet');
  /* the stopped chat's monitor caption on the stage says it once, without no result yet */
  A[CLEO].cur={kind:'spawn',done:false};const cap=api.captionOf(A[CLEO],1000);
  assert.equal(cap.meaning,'a step stopped before a result');assert.equal(cap.line,'last: handed off: log search');
  assert.doesNotMatch(cap.meaning+' '+cap.line,/no result/);
  A[PLAIN].managedStop=null;A[PLAIN].turn='ended';A[PLAIN].cur={kind:'spawn',done:false};assert.equal(api.captionOf(A[PLAIN],1000).meaning,'a step with no result yet','a chat not stopped is unchanged');
}

(async()=>{
  const h=await menuAndConfirm();statusShapes(h);await stoppedFlow();await unconfirmedFlow();await claudeEndedFlow();await endedRace();await lostReply();stage();
  /* causal mutations: each must be caught */
  const mutants=[
    ['double click guard',"if(old?.inFlight)return false;",''],
    ['menu on unmanaged chats',"if(!cap||cap.classification!=='managed'||!managedUUID(cap.runId)||cap.key!==KEY||cap.generation!==MCP.generation)return false;",''],
    ['stopped words',"+' is stopped'","+' has ended'"],
    ['no hint under the box',"setHint('');return}","setHint('You stopped this chat.');return}"],
    ['contact-lost notice after stop',"MCP.noticeHost.hidden=stop.phase!=='unconfirmed';","MCP.noticeHost.hidden=false;"],
    ['no buttons check removed',"if(!ended)node.append(row)}","node.append(row)}"],
    ['no buttons check inverted',"if(!ended)node.append(row)}","if(ended)node.append(row)}"],
    ['claudeEnded read loosely',"ended=stop.record?.claudeEnded===true,","ended=!!stop.record,"],
    ['claudeEnded not in the repaint signature',"sig=JSON.stringify(['stop',name,when,command,ended]);","sig=JSON.stringify(['stop',name,when,command]);"],
    ['claudeEnded accepted as any value',"if('claudeEnded' in s&&(typeof s.claudeEnded!=='boolean'||s.claudeEnded&&s.outcome!=='unconfirmed'))return false;",""],
    ['claudeEnded true accepted on any outcome',"||s.claudeEnded&&s.outcome!=='unconfirmed'))return false;","))return false;"],
    ['the room copy wins a tie',"fields(next)>=fields(old)?next:old,other=","old,other="],
    ['status order decides',"return add||end?{...win,...(add?{terminalCommand:other.terminalCommand}:{}),...(end?{claudeEnded:true}:{})}:win}","return next}"],
    ['claudeEnded not sticky',"&&other.claudeEnded===true;","&&false;"],
    ['the sparse room copy drops the command',"add=!('terminalCommand' in win)&&'terminalCommand' in other,","add=false,"],
    ['claudeEnded carried across attempts',"if(next.lastAttemptAt!==old.lastAttemptAt)return next.lastAttemptAt>old.lastAttemptAt?next:old;const win=","const win=next.lastAttemptAt!==old.lastAttemptAt?(next.lastAttemptAt>old.lastAttemptAt?next:old):"],
    ['new id for Try again',"const requestId=globalThis.crypto?.randomUUID?.();if(!managedUUID(requestId))return false;const entry=","const requestId=old?.lastId||globalThis.crypto?.randomUUID?.();if(!managedUUID(requestId))return false;const entry="],
  ];
  for(const [name,from,to] of mutants){const code=controllerSource.replace(from,to);assert.notEqual(code,controllerSource,name);let caught=false;try{const m=await menuAndConfirm(code);statusShapes(m);await stoppedFlow(code);await claudeEndedFlow(code);await endedRace(code);await unconfirmedFlow(code.replace("entry={inFlight:requestId,","entry={lastId:requestId,inFlight:requestId,").replace("const old=MCP.stops.get(a.id);if(old?.inFlight)return false;","const old=MCP.stops.get(a.id);if(old?.inFlight)return false;"))}catch{caught=true}assert.ok(caught,'mutation detected: '+name)}
  console.log('ok managed Stop page: menu only on chats Motet started, confirm words, stopping/stopped/could-not-confirm states, Try again and Copy, no buttons once Claude has ended (not brought back by an older read), one POST per click, stage stopped and ended, Ended filter and Dismiss idle, step stopped before a result; '+mutants.length+' causal mutations detected');
})().catch(error=>{console.error(error);process.exitCode=1});
