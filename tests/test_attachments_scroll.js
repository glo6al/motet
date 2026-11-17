'use strict';
const assert=require('node:assert/strict'),path=require('node:path'),{pathToFileURL}=require('node:url');
const {harness,Node,controller,sourceFunction,extendedBindings}=require('./test_workspace_attachments_controller.js');
const cls=(q,name)=>q.ids['#officer'].all().filter(n=>n.classList.contains(name));
const actions=q=>q.calls.filter(c=>c&&typeof c==='object'&&c.type);
const click=(q,label)=>{const b=q.ids['#officer'].all().find(n=>n.tagName==='BUTTON'&&n.getAttribute('aria-label')===label);assert(b,'Missing '+label);assert.equal(typeof b.onclick,'function');b.onclick();return b};
(async()=>{
 const {createAttachmentsFixture}=await import(pathToFileURL(path.join(__dirname,'../docs/fixtures/attachments/fixture.mjs')).href);
 // Geometry reproduces the observed layout: before pending, the 667px log fits a 672px
 // viewport; pending consumes 70px, clipping status y681.52..709.52 below pane bottom659.25.
 function setup(scenario='a06-unknown',{mutate=x=>x,height=672,pending=70,content=667,native=200,top=0,statusTop=624.27}={}){
   const fixture=createAttachmentsFixture({enabled:true,search:'?demo',scenario}),q=harness(fixture,{withLifecycle:true,withFrames:true,query:'?ui=workspace&demo&attachments='+scenario,mutate}),log=q.ids['#ofLog'];
   const scene={height,pending,content,native,statusTop,paneTop:57.25,writes:[],lastTop:top};
   const hasPending=()=>cls(q,'workspace-attachments-pending').length>0;
   Object.defineProperties(log,{clientHeight:{get:()=>scene.height-(hasPending()?scene.pending:0)},scrollHeight:{get:()=>cls(q,'workspace-attachments-messages').length?scene.content:scene.native},scrollTop:{get(){return Math.min(scene.lastTop,Math.max(0,this.scrollHeight-this.clientHeight))},set(value){scene.lastTop=Math.max(0,Math.min(value,Math.max(0,this.scrollHeight-this.clientHeight)));scene.writes.push(scene.lastTop)}}});
   function rect(node){let offset=0,height=20;if(node===log)return {top:scene.paneTop,bottom:scene.paneTop+log.clientHeight,height:log.clientHeight};if(node.classList.contains('workspace-attachments-message')){offset=scene.statusTop-44;height=80}else if(node.classList.contains('workspace-attachments-message-status')||node.classList.contains('workspace-attachments-check-status')){offset=scene.statusTop;height=28}else if(node.classList.contains('workspace-attachments-lifecycle-button')){offset=scene.statusTop+190;height=32}else {offset=scene.statusTop;height=20}const top=scene.paneTop+offset-log.scrollTop;return {top,bottom:top+height,height,left:0,right:200,width:200}}
   const create=q.doc.createElement.bind(q.doc),createNS=q.doc.createElementNS.bind(q.doc);const decorate=node=>{node.getBoundingClientRect=()=>rect(node);return node};q.doc.createElement=tag=>decorate(create(tag));q.doc.createElementNS=(ns,tag)=>decorate(createNS(ns,tag));for(const node of q.doc.body.all())decorate(node);
   q.input.focus();assert.equal(q.install(),true);return {q,fixture,log,scene};
 }
 function visible(q,log){const status=cls(q,'workspace-attachments-message-status').at(-1),pane=log.getBoundingClientRect(),r=status.getBoundingClientRect();assert(r.top>=pane.top+3.9,'status above clip top');assert(r.bottom<=pane.bottom-3.9,'status/actions below clip bottom');for(const button of cls(q,'workspace-attachments-check-status')){const b=button.getBoundingClientRect();assert(b.top>=pane.top&&b.bottom<=pane.bottom,'Check status must be reachable')}}
 let groups=0;
 for(const scenario of ['a06-queued','a06-unknown'])for(const height of [672,420]){
   const {q,log,scene}=setup(scenario,{height});assert.equal(q.frames.size,1,'message and pending changes coalesce to one bounded frame');const pre=cls(q,'workspace-attachments-message-status')[0].getBoundingClientRect();assert(pre.bottom>log.getBoundingClientRect().bottom,'fixture reproduces obscured status before layout correction');assert.equal(log.scrollTop,0);const focus=q.doc.activeElement;q.doc.body.scrollTop=187;q.ids['#ofBody'].scrollTop=91;q.flushFrames();visible(q,log);assert.equal(q.doc.activeElement,focus);assert.equal(q.doc.body.scrollTop,187);assert.equal(q.ids['#ofBody'].scrollTop,91);assert.equal(q.frames.size,0,'no polling frame');const expected=scene.statusTop+28-(height-scene.pending-4);assert(Math.abs(log.scrollTop-expected)<.01,'smallest overflow correction, not unconditional bottom');assert(log.scrollTop<log.scrollHeight-log.clientHeight);q.api.workspaceAttachmentsSync();q.api.workspaceAttachmentsSync();assert.equal(q.frames.size,0,'unchanged paints schedule nothing');
 }groups++;
 {
   const {q,log}=setup('a03-one');assert.equal(q.frames.size,0,'an empty message list has no scroll work');q.input.value='An explicit sample send';q.input.event('input');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.frames.size,1);q.flushFrames();visible(q,log);assert.equal(actions(q).filter(a=>a.type==='send_sample').length,1);assert.equal(q.input.value,'');
 }groups++;
 {
   const {q,log,scene}=setup();q.flushFrames();q.input.value='bad\0unsent';q.input.event('input');const before=JSON.stringify(q.c().snapshot.draft);scene.pending=140;click(q,'Check status');assert.equal(q.frames.size,1,'state check resettles after pending layout');const focus=q.doc.activeElement;q.flushFrames();visible(q,log);assert.equal(q.doc.activeElement,focus);assert.equal(q.input.value,'bad\0unsent');assert.equal(JSON.stringify(q.c().snapshot.draft),before);assert.equal(actions(q).filter(a=>a.type==='check_status').length,1);assert.equal(actions(q).some(a=>a.type==='send_sample'),false);
 }groups++;
 {
   const {q,log}=setup();q.flushFrames();click(q,'Dismiss message notice');assert.equal(cls(q,'workspace-attachments-pending').length,0);assert.equal(q.frames.size,1,'pending disappearance schedules a final layout measurement');const top=log.scrollTop,focus=q.doc.activeElement;q.flushFrames();visible(q,log);assert.equal(log.scrollTop,top,'already visible after growth does not move');assert.equal(q.doc.activeElement,focus);assert.equal(q.frames.size,0);
 }groups++;
 {
   const {q,log,scene}=setup('a06-unknown',{content:2000,native:2000,top:100,statusTop:1500});assert.equal(q.frames.size,0,'far-up native reading position is preserved');assert.equal(log.scrollTop,100);q.api.workspaceAttachmentsSync();assert.equal(q.frames.size,0);assert.equal(scene.writes.at(-1),100);
 }groups++;
 // A real log-directed scroll gesture plus movement overrides automatic receipt following.
 {
   const {q,log}=setup();log.event('wheel',{deltaY:-5});log.scrollTop=5;log.event('scroll');q.flushFrames();assert.equal(log.scrollTop,5);assert.equal(q.frames.size,0);
 }groups++;
 for(const departure of ['leave','dispose','lane']){
   const {q,log}=setup();const callback=[...q.frames.values()][0];if(departure==='leave')q.api.workspaceAttachmentsBeforeRestore();else if(departure==='dispose')q.api.workspaceAttachmentsDispose();else {q.context.focus='x:demo-bram';q.context.OFUI.target='x:demo-bram';q.api.workspaceAttachmentsSync()}const top=log.scrollTop;assert.equal(q.frames.size,0,departure+' cancels frame');callback();assert.equal(log.scrollTop,top,departure+' old callback does nothing');
 }groups++;
 {
   const {q,log}=setup();const obsolete=[...q.frames.values()][0];q.api.workspaceAttachmentsBeforeRestore();q.api.workspaceAttachmentsSync();assert.equal(q.frames.size,1);obsolete();assert.equal(q.frames.size,1,'canceled callback cannot consume replacement frame');assert.equal(log.scrollTop,0);q.flushFrames();visible(q,log);
 }groups++;
 // Causal mutants demonstrate that the correction must follow pending layout, honor user scroll and
 // preserve canceled/new frame identity. Mutants affect only this VM's source string.
 function pendingCorrection(mutate=x=>x){const {q,log}=setup('a06-unknown',{mutate});q.flushFrames();visible(q,log)}
 pendingCorrection();const delta='const delta=bottom>pane.bottom-4?bottom-(pane.bottom-4):top<pane.top+4?top-(pane.top+4):0;';assert(controller.includes(delta));assert.throws(()=>pendingCorrection(s=>s.replace(delta,'const delta=0;')),assert.AssertionError);
 function readingPosition(mutate=x=>x){const {q,log}=setup('a06-unknown',{mutate,content:2000,native:2000,top:100,statusTop:1500});q.flushFrames();assert.equal(log.scrollTop,100)}
 readingPosition();const eligible='function lifecycleScrollEligible(view){';assert(controller.includes(eligible));assert.throws(()=>readingPosition(s=>s.replace(eligible,eligible+'return true;')),assert.AssertionError);
 function supersededFrame(mutate=x=>x){const {q,log}=setup('a06-unknown',{mutate});const obsolete=[...q.frames.values()][0];q.api.workspaceAttachmentsBeforeRestore();q.api.workspaceAttachmentsSync();obsolete();assert.equal(log.scrollTop,0,'old frame cannot consume new request')}
 supersededFrame();const generation='if(scheduledGeneration!==scrollGeneration)return;';assert(controller.includes(generation));assert.throws(()=>supersededFrame(s=>s.replace(generation,'')),assert.AssertionError);groups++;
 // Native paints are executed from the page source. Only unrelated leaf painters are replaced;
 // attachment roots, signatures, receipts and frame scheduling are the real controller's own state.
 function nativeSetup(scenario,{mutate=x=>x,mutateNative=x=>x,mutateLifecycle=x=>x,height=672,noActions=false,invalidReply=false,mountShrink=0,initialTop=0}={}){
   const observers=new Set(),windowEvents=new Node({});
   class ResizeObserver{
     constructor(callback){this.callback=callback;this.targets=new Map();observers.add(this)}
     observe(target){const r=target.getBoundingClientRect();this.targets.set(target,[r.width,r.height])}unobserve(target){this.targets.delete(target)}
     disconnect(){this.targets.clear();observers.delete(this)}
   }
   const fixture=createAttachmentsFixture({enabled:true,search:'?demo',scenario});
   const q=harness(fixture,{withLifecycle:true,withFrames:true,query:'?ui=workspace&demo&attachments='+scenario,mutate,mutateLifecycle,bindings:extendedBindings(),globals:{ResizeObserver,addEventListener:windowEvents.addEventListener.bind(windowEvents),removeEventListener:windowEvents.removeEventListener.bind(windowEvents)}}),log=q.ids['#ofLog'];
   q.context.window=q.context;
   if(scenario==='a07-together'){q.context.focus='c:demo-cleo';q.context.OFUI.target='c:demo-cleo'}
   if(scenario.startsWith('a08-')){q.officer(true);q.context.OFUI.recipient='watch';q.context.OFUI.target='c:demo-ada';q.context.OFUI.scope='station'}
   if(noActions){const snapshot=q.client.snapshot;q.client.snapshot=()=>{const s=snapshot();for(const m of s.messages){m.actions=[];m.recovery.canFinish=false;m.recovery.canDeleteSavedCopies=false}return s}}
   if(invalidReply){const snapshot=q.client.snapshot;q.client.snapshot=()=>{const s=snapshot();s.messages[0].reply.author.agentId='bram';return s}}
   const scene={height,reduction:0,contentExtra:0,replyExtra:0,native:[230,230],top:0,writes:[],nativeWrites:0};
   const isPending=()=>cls(q,'workspace-attachments-pending').length>0;
   const group=()=>cls(q,'workspace-attachments-messages')[0];
   const partial=scenario==='a06-partial',unknown=scenario==='a06-unknown',answered=scenario==='a07-answered',cardHeight=partial?(noActions?320:384):answered||scenario.startsWith('a08-')?340:unknown?282:240;
   const nativeHeight=()=>log.children.filter(n=>n.nativeHeight&&!n.hidden).reduce((sum,n)=>sum+n.nativeHeight,0);
   const connectedContent=()=>nativeHeight()+(group()&&log.contains(group())?cardHeight+scene.contentExtra+scene.replyExtra:0);
   Object.defineProperties(log,{clientHeight:{get:()=>Math.max(0,scene.height-scene.reduction-(isPending()?70:0)-(cls(q,'workspace-attachments-tray').length?mountShrink:0))},scrollHeight:{get:connectedContent},scrollTop:{get(){scene.top=Math.min(scene.top,Math.max(0,this.scrollHeight-this.clientHeight));return scene.top},set(value){scene.top=Math.max(0,Math.min(Number(value)||0,Math.max(0,this.scrollHeight-this.clientHeight)));scene.writes.push(scene.top)}}});
   function rect(node){
     if(!node.isConnected)return {top:0,bottom:0,height:0,left:0,right:0,width:0};
     if(node===log)return {top:57,bottom:57+log.clientHeight,height:log.clientHeight,left:0,right:320,width:320};
     let y=nativeHeight(),h=20;
     if(node.nativeHeight){y=0;for(const sibling of log.children){if(sibling===node)break;if(!sibling.hidden)y+=sibling.nativeHeight||0}h=node.nativeHeight}
     else if(node.classList.contains('workspace-attachments-messages'))h=cardHeight+scene.contentExtra+scene.replyExtra;
     else if(node.classList.contains('workspace-attachments-message'))h=(answered?240:cardHeight)+scene.contentExtra;
     else if(node.classList.contains('workspace-attachments-agent-reply')){y+=248+scene.contentExtra;h=84+scene.replyExtra}
     else if(node.classList.contains('workspace-attachments-message-meta')){y+=200+scene.contentExtra;h=20.3}
     else if(node.classList.contains('workspace-attachments-message-status')){y+=200+scene.contentExtra;h=28}
     else if(node.classList.contains('workspace-attachments-check-status')){y+=232+scene.contentExtra;h=32}
     else if(node.classList.contains('workspace-attachments-send-failure')){y+=236+scene.contentExtra;h=noActions?58:140}
     else if(node.tagName==='SMALL'&&node.parentNode?.classList.contains('workspace-attachments-send-failure')){y+=262+scene.contentExtra;h=32}
     else if(node.classList.contains('workspace-attachments-lifecycle-button')){const buttons=cls(q,'workspace-attachments-lifecycle-button');y+=304+40*buttons.indexOf(node)+scene.contentExtra;h=32}
     else y+=200;
     const top=57+y-log.scrollTop;return {top,bottom:top+h,height:h,left:0,right:200,width:200};
   }
   function decorate(node){
     node.dataset={};Object.defineProperties(node,{firstElementChild:{get:()=>node.children[0]||null},nextElementSibling:{get:()=>node.nextSibling},isConnected:{get:()=>q.doc.documentElement.contains(node)}});
     node.matches=selector=>selector.split(',').some(value=>{const part=value.trim();return part.startsWith('.')?node.classList.contains(part.slice(1)):part.startsWith('[contenteditable')?node.getAttribute('contenteditable')!==null:part.toUpperCase()===node.tagName});
     node.closest=selector=>{for(let current=node;current;current=current.parentNode)if(current.matches?.(selector))return current;return null};
     node.getBoundingClientRect=()=>rect(node);node.getClientRects=()=>node.isConnected?[rect(node)]:[];return node;
   }
   const create=q.doc.createElement.bind(q.doc),createNS=q.doc.createElementNS.bind(q.doc);
   q.doc.createElement=tag=>decorate(create(tag));q.doc.createElementNS=(ns,tag)=>decorate(createNS(ns,tag));for(const node of [q.doc.documentElement,q.doc.body,...q.doc.body.all()])decorate(node);
   log.clientWidth=320;log.offsetWidth=320;log.clientLeft=0;
   q.context.CU.nodes=new Map();
   function nativeRow(height,ts){const el=q.doc.createElement('div');el.nativeHeight=height;el.textContent='Native conversation row '+ts;const row={el,kind:'source',ts,context:{agent:'c:demo-ada',scope:'station'}};q.context.CU.nodes.set('native:'+ts,row);return row}
   for(const [i,h] of scene.native.entries())log.append(nativeRow(h,i+1).el);
   q.context.ofCurrent=()=>({context:{scope:'station'}});q.context.cuPendingPaint=()=>{};q.context.cuLaneVisible=()=>true;
   q.context.nearEnd=el=>el.scrollHeight-el.scrollTop-el.clientHeight<40;
   q.context.responseDraw=()=>{};
   for(const [name,value] of Object.entries(q.api))q.context[name]=value;
   q.run(mutateNative(sourceFunction('cuScrollSnapshot')+'\n'+sourceFunction('cuPaint')+'\n'+sourceFunction('responsePaint')));
   log.scrollTop=initialTop;const beforeMount={top:log.scrollTop,height:log.clientHeight,total:log.scrollHeight};
   q.input.focus();assert.equal(q.install(),true);
   const focus=q.doc.activeElement;q.doc.body.scrollTop=187;q.ids['#ofBody'].scrollTop=91;
   function settle(){for(let i=0;q.frames.size&&i<8;i++)q.flushFrames();assert.equal(q.frames.size,0,'layout correction must settle without frame polling')}
   function reflow(){for(const observer of [...observers]){const entries=[];for(const [target,last] of observer.targets){const r=target.getBoundingClientRect(),next=[r.width,r.height];if(next.some((value,i)=>value!==last[i])){observer.targets.set(target,next);entries.push({target,contentRect:r})}}if(entries.length)observer.callback(entries)}}
   function unchanged(){const before=JSON.stringify(q.c().snapshot);q.api.workspaceAttachmentsSync();assert.equal(JSON.stringify(q.c().snapshot),before);assert.equal(q.doc.activeElement,focus);assert.equal(q.doc.body.scrollTop,187);assert.equal(q.ids['#ofBody'].scrollTop,91);assert.equal(actions(q).length,0,'painting and reflow never dispatch fixture actions');for(const row of q.context.CU.nodes.values()){assert.equal(row.el.parentNode,log);assert.match(row.el.textContent,/Native conversation row/)}}
   return {q,log,scene,beforeMount,nativeRow,observers,settle,reflow,unchanged,nativePaint:()=>q.run('cuPaint()'),responsePaint:()=>q.run('responsePaint(null,null)')};
 }
 function receiptVisible(q,log,scenario,{noActions=false}={}){
   const officer=scenario.startsWith('a08-'),pane=log.getBoundingClientRect(),status=cls(q,officer?'workspace-attachments-message-meta':'workspace-attachments-message-status').at(-1);
   if(officer)assert.equal(cls(q,'workspace-attachments-message-status').length,0,'answered Motet uses owned visible metadata, not a hidden status');
   assert(status,'real receipt anchor exists');const buttons=[...cls(q,'workspace-attachments-check-status'),...cls(q,'workspace-attachments-lifecycle-button')];
   if(scenario==='a06-partial'&&!noActions)assert.deepEqual(buttons.map(n=>n.textContent),['Finish sending','Remove the copied file'],'both real recovery actions are measured');
   if(scenario==='a06-unknown')assert.equal(buttons.length,1);if(scenario==='a06-queued')assert.equal(buttons.length,0);
   const lastCopy=cls(q,'workspace-attachments-send-failure').flatMap(n=>n.children.filter(c=>c.tagName==='SMALL'));
   if(scenario==='a06-partial')assert.equal(lastCopy.length,1,'measure the failure explanation last line, including without actions');
   const replies=scenario==='a07-answered'?cls(q,'workspace-attachments-agent-reply'):[];if(scenario==='a07-answered')assert.equal(replies.length,1,'validated reply is part of the visible receipt');
   for(const node of [status,...lastCopy,...buttons,...replies]){const r=node.getBoundingClientRect();assert(r.height>0&&r.top>=pane.top&&r.bottom<=pane.bottom,scenario+' last line/action clipped: '+node.textContent+' '+JSON.stringify({r,pane,top:log.scrollTop}))}
 }
 const nativeFailures=[];let nativeCases=0;
 function nativeCase(name,test){nativeCases++;try{test()}catch(error){nativeFailures.push(name+': '+error.message)}}
 for(const scenario of ['a06-queued','a06-unknown','a06-partial','a07-answered','a08-doc'])for(let repeat=0;repeat<3;repeat++){
   nativeCase(scenario+' arrival/native anchor '+repeat,()=>{
     const h=nativeSetup(scenario);const {q,log}=h;assert(q.frames.size>0);h.nativeRow(100,0);h.nativePaint();assert(log.scrollTop>0,'actual cuPaint changed top to retain its native anchor');h.settle();receiptVisible(q,log,scenario);h.unchanged();
   });
   nativeCase(scenario+' unchanged native repaint '+repeat,()=>{
     const h=nativeSetup(scenario);h.settle();receiptVisible(h.q,h.log,scenario);const oldGroup=cls(h.q,'workspace-attachments-messages')[0];h.nativeRow(120,3);h.nativePaint();assert.equal(cls(h.q,'workspace-attachments-messages')[0],oldGroup,'native repaint leaves the canonical receipt unchanged');h.settle();receiptVisible(h.q,h.log,scenario);h.unchanged();
   });
   nativeCase(scenario+' paused frames/resume '+repeat,()=>{
     const h=nativeSetup(scenario);h.q.doc.hidden=true;h.q.flushFrames();h.q.doc.hidden=false;h.q.doc.event('visibilitychange');h.settle();receiptVisible(h.q,h.log,scenario);h.unchanged();
   });
 }
 for(const scenario of ['a06-queued','a06-unknown','a06-partial','a07-answered','a08-doc']){
   nativeCase(scenario+' ancestor resize and content reflow',()=>{
     const h=nativeSetup(scenario);h.settle();h.scene.reduction=90;h.responsePaint();h.reflow();h.settle();receiptVisible(h.q,h.log,scenario);h.scene.contentExtra=80;h.reflow();h.settle();receiptVisible(h.q,h.log,scenario);h.unchanged();
   });
   nativeCase(scenario+' real user intent survives native repaint',()=>{
     const h=nativeSetup(scenario);h.settle();h.log.event('wheel',{deltaY:-100});h.log.scrollTop=0;h.log.event('scroll');h.nativeRow(100,3);h.nativePaint();h.reflow();h.settle();assert.equal(h.log.scrollTop,0,'deliberate reader scroll stays put');h.unchanged();
   });
 }
 function initialMountFollow(mutate=x=>x){const h=nativeSetup('a07-together',{mutate,height:321,mountShrink:77,initialTop:139});assert.deepEqual(h.beforeMount,{top:139,height:321,total:460});assert.equal(h.log.clientHeight,174,'tray and pending shrink the pre-mount log');assert.equal(h.log.scrollTop,139);assert(cls(h.q,'workspace-attachments-message-status')[0].getBoundingClientRect().bottom>h.log.getBoundingClientRect().bottom);h.settle();receiptVisible(h.q,h.log,'a07-together');h.nativePaint();h.reflow();h.settle();receiptVisible(h.q,h.log,'a07-together');h.unchanged()}
 nativeCase('first mount retains pre-tray follow eligibility',()=>initialMountFollow());
 nativeCase('first mount preserves genuinely far-up reader',()=>{const h=nativeSetup('a07-together',{height:321,mountShrink:77,initialTop:0});assert.equal(h.q.frames.size,0);h.nativePaint();h.reflow();h.settle();assert.equal(h.log.scrollTop,0);h.unchanged()});
 nativeCase('reader movement before first follow frame wins',()=>{const h=nativeSetup('a07-together',{height:321,mountShrink:77,initialTop:139});h.log.event('wheel',{deltaY:-100});h.log.scrollTop=0;h.log.event('scroll');h.settle();h.nativePaint();h.reflow();h.settle();assert.equal(h.log.scrollTop,0);h.unchanged()});
 nativeCase('empty first mount discards capture before later Send',()=>{const h=nativeSetup('a03-one',{height:321,mountShrink:77,initialTop:139});assert.equal(cls(h.q,'workspace-attachments-message').length,0);h.log.event('wheel',{deltaY:-100});h.log.scrollTop=0;h.log.event('scroll');h.q.api.workspaceAttachmentsInterceptSend();h.settle();assert.equal(actions(h.q).filter(a=>a.type==='send_sample').length,1);assert.equal(cls(h.q,'workspace-attachments-message').length,1);assert.equal(h.log.scrollTop,0,'initial empty-mount eligibility never leaks into later receipt');assert.equal(h.q.frames.size,0)});
 nativeCase('A07 reply-only resize remains visible after settling',()=>{const h=nativeSetup('a07-answered');h.settle();h.scene.replyExtra=90;h.reflow();h.settle();receiptVisible(h.q,h.log,'a07-answered');h.unchanged()});
 nativeCase('invalid A07 reply is not an observer or follow target',()=>{const h=nativeSetup('a07-answered',{invalidReply:true}),invalid=cls(h.q,'workspace-attachments-agent-reply')[0];assert(invalid.textContent.includes('could not be verified'));for(const observer of h.observers)assert(!observer.targets.has(invalid),'unverified reply cannot earn geometry ownership');h.settle();const top=h.log.scrollTop;h.scene.replyExtra=90;h.reflow();assert.equal(h.q.frames.size,0,'invalid reply-only growth schedules no follow work');assert.equal(h.log.scrollTop,top);h.unchanged()});
 nativeCase('A07 departure disconnects receipt and reply observation',()=>{const h=nativeSetup('a07-answered');h.settle();const targets=[...h.observers].flatMap(o=>[...o.targets.keys()]);assert(targets.includes(cls(h.q,'workspace-attachments-agent-reply')[0]));h.q.api.workspaceAttachmentsBeforeRestore();assert.equal(h.observers.size,0);h.scene.replyExtra=90;h.reflow();h.q.doc.event('visibilitychange');assert.equal(h.q.frames.size,0)});
 nativeCase('paused held scrollbar remains reader intent until release',()=>{
   for(const release of ['pointerup','pointercancel']){
     const h=nativeSetup('a06-partial');h.settle();h.log.event('pointerdown',{clientX:319,clientY:200});h.q.advanceTimers(500);
     assert.equal(h.q.timers.size,0,'a held scrollbar has no idle expiry');h.log.scrollTop=0;h.log.event('scroll');h.nativePaint();h.scene.contentExtra=80;h.reflow();h.settle();assert.equal(h.log.scrollTop,0,'drag after a pause still suppresses automatic follow');
     h.q.doc.event(release);assert.equal(h.q.timers.size,1,release+' starts finite settlement after movement');h.q.advanceTimers(1000);assert.equal(h.q.timers.size,0,release+' settlement expires');
     h.scene.height=1200;assert.equal(h.log.scrollTop,0);h.log.event('scroll');h.reflow();h.settle();h.scene.height=672;h.reflow();h.nativePaint();h.settle();assert.equal(h.log.scrollTop,0,'released drag remains a reader position after unrelated layout clamping');h.unchanged();
   }
 });
 nativeCase('released scrollbar timers cannot survive departure or consume a new gesture',()=>{
   const h=nativeSetup('a06-partial');h.settle();h.log.event('pointerdown',{clientX:319,clientY:200});h.log.scrollTop=0;h.log.event('scroll');h.q.doc.event('pointerup');
   assert.equal(h.q.timers.size,1);const stale=[...h.q.timers.values()][0].callback;h.q.api.workspaceAttachmentsBeforeRestore();assert.equal(h.q.timers.size,0,'leave clears held-drag settlement');assert.equal(h.q.frames.size,0);h.q.api.workspaceAttachmentsSync();
   h.log.event('pointerdown',{clientX:319,clientY:200});stale();h.log.scrollTop=20;h.log.event('scroll');h.nativePaint();h.settle();assert.equal(h.log.scrollTop,20,'old expiry cannot cancel a new held gesture');
   h.q.api.workspaceAttachmentsDispose();assert.equal(h.q.timers.size,0);assert.equal(h.q.frames.size,0);stale();h.q.advanceTimers(1000);assert.equal(h.q.frames.size,0,'stale settlement cannot revive disposed work');
 });
 nativeCase('late browser scroll after a frame still records reader intent',()=>{const h=nativeSetup('a06-partial');h.settle();h.log.event('wheel',{deltaY:-100});h.q.flushFrames();h.q.advanceTimers(40);h.log.scrollTop=0;h.log.event('scroll');h.nativePaint();h.settle();assert.equal(h.log.scrollTop,0,'late scroll still belongs to the recent real gesture');h.unchanged()});
 nativeCase('idle reader suppression survives later clamping and a fresh gesture restores follow',()=>{
   const h=nativeSetup('a06-partial');h.settle();h.log.event('wheel',{deltaY:-100});h.log.scrollTop=40;h.log.event('scroll');h.settle();h.q.advanceTimers(1000);
   h.scene.height=1000;assert.equal(h.log.scrollTop,0,'ancestor growth clamps the reader to the temporary bottom');h.log.event('scroll');h.reflow();h.settle();
   h.scene.height=672;h.reflow();h.nativePaint();h.settle();assert.equal(h.log.scrollTop,0,'a later layout clamp cannot restore automatic follow');
   h.log.event('wheel',{deltaY:500});h.log.scrollTop=h.log.scrollHeight;h.log.event('scroll');h.settle();h.q.advanceTimers(1000);
   h.scene.contentExtra=80;h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged();
 });
 nativeCase('input keys and gestures without movement do not suppress initial correction',()=>{for(const gesture of ['input-key','wheel','button-pointer','scrollbar']){const h=nativeSetup('a06-partial');if(gesture==='input-key')h.log.event('keydown',{key:'ArrowUp',target:h.q.input});else if(gesture==='wheel')h.log.event('wheel',{deltaY:-50});else if(gesture==='button-pointer')h.log.event('pointerdown',{clientX:20,clientY:20,target:cls(h.q,'workspace-attachments-lifecycle-button')[0]});else{h.log.event('pointerdown',{clientX:319,clientY:200});h.q.doc.event('pointerup')}h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()}});
 nativeCase('native anchor movement during a no-op gesture is not reader movement',()=>{const h=nativeSetup('a06-partial');h.log.event('wheel',{deltaY:-50});h.nativeRow(100,0);h.nativePaint();assert.equal(h.log.scrollTop,100);h.log.event('scroll');h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()});
 nativeCase('programmatic clamp does not become reader intent',()=>{const h=nativeSetup('a06-partial');h.settle();h.scene.reduction=90;h.responsePaint();h.scene.height=1000;assert.equal(h.log.scrollTop,0,'ancestor growth clamps top');h.scene.height=672;h.log.event('scroll');h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()});
 nativeCase('partial explanation without recovery controls',()=>{const h=nativeSetup('a06-partial',{noActions:true});h.settle();assert.equal(cls(h.q,'workspace-attachments-lifecycle-button').length,0);receiptVisible(h.q,h.log,'a06-partial',{noActions:true});h.unchanged()});
 nativeCase('initial zero-sized ancestor resumes after reveal',()=>{const h=nativeSetup('a06-partial',{height:0});h.q.flushFrames();h.scene.height=672;h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()});
 nativeCase('zero-sized ancestor resumes after reveal',()=>{const h=nativeSetup('a06-partial');h.scene.height=0;h.reflow();h.q.flushFrames();h.scene.height=672;h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()});
 nativeCase('dismissed pending stays dismissed across native repaint',()=>{const h=nativeSetup('a06-unknown');h.settle();click(h.q,'Dismiss message notice');assert.equal(cls(h.q,'workspace-attachments-pending').length,0);const before=JSON.stringify(h.q.c().snapshot);h.nativePaint();h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-unknown');assert.equal(JSON.stringify(h.q.c().snapshot),before);assert.equal(cls(h.q,'workspace-attachments-pending').length,0);assert.equal(actions(h.q).length,0)});
 for(const exit of ['normal','early','throw'])nativeCase('actual responsePaint settles after '+exit+' exit',()=>{const h=nativeSetup('a06-partial');h.settle();h.scene.reduction=90;if(exit==='early')h.q.context.workspaceManagedInitialScroll=()=>true;if(exit==='throw')h.q.context.responseDraw=()=>{throw Error('response-leaf-sentinel')};if(exit==='throw')assert.throws(()=>h.responsePaint(),/response-leaf-sentinel/);else h.responsePaint();h.settle();receiptVisible(h.q,h.log,'a06-partial');h.unchanged()});
 nativeCase('removed saved copies stays removed across native repaint',()=>{const h=nativeSetup('a06-partial');h.settle();click(h.q,'Remove the copied file');const before=JSON.stringify(h.q.c().snapshot);assert.match(h.q.ids['#officer'].textContent,/Not sent · the copied file was removed/);h.nativePaint();h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial-cleaned');assert.equal(JSON.stringify(h.q.c().snapshot),before);assert.equal(actions(h.q).filter(a=>a.type==='delete_saved_copies').length,1);assert.equal(actions(h.q).some(a=>a.type==='finish_send'||a.type==='send_sample'),false)});
 nativeCase('departure disconnects geometry work',()=>{for(const departure of ['leave','dispose','lane']){const h=nativeSetup('a06-unknown');const callbacks=[...h.q.frames.values()];h.log.event('wheel',{deltaY:-50});if(departure==='leave')h.q.api.workspaceAttachmentsBeforeRestore();else if(departure==='dispose')h.q.api.workspaceAttachmentsDispose();else{h.q.context.focus='x:demo-bram';h.q.context.OFUI.target='x:demo-bram';h.q.api.workspaceAttachmentsSync()}assert.equal(h.q.frames.size,0);assert.equal(h.observers.size,0,departure+' disconnects layout observers');assert.equal(h.q.timers.size,0,departure+' clears gesture timers');const before=h.log.scrollTop;h.reflow();h.q.doc.event('visibilitychange');for(const callback of callbacks)callback();assert.equal(h.q.frames.size,0);assert.equal(h.log.scrollTop,before)}});
 if(nativeFailures.length)throw new assert.AssertionError({message:nativeFailures.length+'/'+nativeCases+' native/visibility regressions failed:\n'+nativeFailures.join('\n')});
 // Causal mutations remove behavior from the real controller/native functions in this VM only.
 let newMutants=0;
 initialMountFollow();assert.throws(()=>initialMountFollow(s=>s.replace("initialFollow=typeof ofLog!=='undefined'?lifecycleScrollEligible():null;",'')),assert.AssertionError);newMutants++;
 function nativeGesture(mutateNative=x=>x){const h=nativeSetup('a06-partial',{mutateNative});h.log.event('wheel',{deltaY:-50});h.nativeRow(100,0);h.nativePaint();h.log.event('scroll');h.settle();receiptVisible(h.q,h.log,'a06-partial')}
 nativeGesture();assert.throws(()=>nativeGesture(s=>s.replaceAll('finishAttachments?.()','')),assert.AssertionError);newMutants++;
 function failureCopy(mutate=x=>x){const h=nativeSetup('a06-partial',{mutate,noActions:true});h.settle();receiptVisible(h.q,h.log,'a06-partial',{noActions:true})}
 const failureMeasure='[status,failure,...controls]';assert(controller.includes(failureMeasure));failureCopy();assert.throws(()=>failureCopy(s=>s.replace(failureMeasure,'[status,...controls]')),assert.AssertionError);newMutants++;
 function hiddenResume(mutate=x=>x){const h=nativeSetup('a06-unknown',{mutate});h.q.doc.hidden=true;h.q.flushFrames();h.q.doc.hidden=false;h.q.doc.event('visibilitychange');h.settle();receiptVisible(h.q,h.log,'a06-unknown')}
 const hiddenRetain='if(document.hidden||pane.height<=0||!status)return;';assert(controller.includes(hiddenRetain));hiddenResume();assert.throws(()=>hiddenResume(s=>s.replace(hiddenRetain,'if(document.hidden||pane.height<=0||!status){cancelLifecycleScroll();return}')),assert.AssertionError);newMutants++;
 function contentResize(mutate=x=>x){const h=nativeSetup('a06-partial',{mutate});h.settle();h.scene.contentExtra=80;h.reflow();h.settle();receiptVisible(h.q,h.log,'a06-partial')}
 const observeReceipt='scrollObserver.observe(receipt.root);';assert(controller.includes(observeReceipt));contentResize();assert.throws(()=>contentResize(s=>s.replace(observeReceipt,'')),assert.AssertionError);newMutants++;
 function actualReader(mutate=x=>x){const h=nativeSetup('a06-partial',{mutate});h.settle();h.log.event('wheel',{deltaY:-100});h.log.scrollTop=0;h.log.event('scroll');h.nativePaint();h.settle();assert.equal(h.log.scrollTop,0)}
 const suppressReader='scrollSuppressed=ofLog.scrollHeight-ofLog.scrollTop-ofLog.clientHeight>1;';assert(controller.includes(suppressReader));actualReader();assert.throws(()=>actualReader(s=>s.replace(suppressReader,'scrollSuppressed=false;')),assert.AssertionError);newMutants++;
 function replyMeasure(mutate=x=>x){const h=nativeSetup('a07-answered',{mutate});h.nativePaint();h.settle();receiptVisible(h.q,h.log,'a07-answered')}
 const measureReply='if(replyRect.height>0)rects.push(replyRect)';assert(controller.includes(measureReply));replyMeasure();assert.throws(()=>replyMeasure(s=>s.replace(measureReply,'')),assert.AssertionError);newMutants++;
 function replyResize(mutate=x=>x){const h=nativeSetup('a07-answered',{mutate});h.settle();h.scene.replyExtra=90;h.reflow();h.settle();receiptVisible(h.q,h.log,'a07-answered')}
 const observeReply='if(receipt.replyRoot)scrollObserver.observe(receipt.replyRoot);';assert(controller.includes(observeReply));replyResize();assert.throws(()=>replyResize(s=>s.replace(observeReply,'')),assert.AssertionError);newMutants++;
 function replyTarget(mutateLifecycle=x=>x){const h=nativeSetup('a07-answered',{invalidReply:true,mutateLifecycle}),invalid=cls(h.q,'workspace-attachments-agent-reply')[0];for(const observer of h.observers)assert(!observer.targets.has(invalid),'invalid reply must not become a follow target')}
 replyTarget();assert.throws(()=>replyTarget(s=>s.replace('if(reply.valid)view.replyRoot=block','view.replyRoot=block')),assert.AssertionError);newMutants++;
 function officerAnchor(mutate=x=>x){const h=nativeSetup('a08-doc',{mutate});h.nativeRow(100,3);h.nativePaint();h.settle();receiptVisible(h.q,h.log,'a08-doc');h.scene.contentExtra=80;h.reflow();h.settle();receiptVisible(h.q,h.log,'a08-doc')}
 officerAnchor();assert.throws(()=>officerAnchor(s=>s.replace('status=request.receipt.scrollAnchor',"status=request.receipt.root.querySelector('.workspace-attachments-message-status')")),assert.AssertionError);newMutants++;
 console.log('ok attachment regression mutants: '+newMutants+' native-hook, explanation, hidden-retention, content-observer and reader-intent mutations');
 console.log('ok attachment native paints: '+nativeCases+' repeated arrival, real native ordering, delayed-frame and reflow cases');

 console.log('ok attachment scroll: '+groups+' geometry/handler groups and three legacy causal mutants');
})().catch(error=>{console.error(error);process.exitCode=1});
