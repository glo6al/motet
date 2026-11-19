'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8').replace(/\r\n?/g,'\n');
const start=html.indexOf('/* Workspace attachments controller:'),end=html.indexOf('/* end Workspace attachments controller */',start);
const controller=html.slice(start,end);
const presentation=html.split('/* Workspace attachments presentation */')[1].split('/* end Workspace attachments presentation */')[0];
const lifecycle=html.split('/* Workspace attachments lifecycle presentation */')[1]?.split('/* end Workspace attachments lifecycle presentation */')[0]||'';
const liveDocuments=html.split('/* Workspace live Officer documents: separate asynchronous owner-resource consumer. */')[1].split('/* end Workspace live Officer documents */')[0];
const names=['Install','Dispose','Sync','DraftRestore','BeforeRestore','OwnsInput','InterceptSend','InterceptSuggestion','Escape'];
function sourceFunction(name){const a=html.indexOf('function '+name+'('),b=html.indexOf('\nfunction ',a+10);return html.slice(a,b).replace(/^function /,'function ')}
class Node{
 constructor(doc,tag='span',text=''){this.doc=doc;this.tagName=tag.toUpperCase();this.childNodes=[];this.attributes={};this.dataset={};this.style={position:'',height:'',overflowY:''};this.className='';this._text=text;this.value='';this.hidden=false;this.disabled=false;this.readOnly=false;this.maxLength=2000;this.selectionStart=this.selectionEnd=0;this.selectionDirection='none';this.scrollTop=0;this.scrollHeight=600;this.clientHeight=600;this.handlers=new Map();this.classList={add:n=>{this.className+=' '+n},remove:n=>{this.className=this.className.split(' ').filter(k=>k!==n).join(' ')},contains:n=>this.className.split(' ').includes(n)};}
 get parentNode(){return this.parentElement||null}get nextSibling(){if(!this.parentNode)return null;const a=this.parentNode.childNodes;return a[a.indexOf(this)+1]||null}get children(){return this.childNodes}
 get nextElementSibling(){let next=this.nextSibling;while(next?.nodeType===8)next=next.nextSibling;return next}
 append(...nodes){for(const n of nodes){n.remove();n.parentElement=this;this.childNodes.push(n)}}
 insertBefore(node,next){if(node===next)return;node.remove();node.parentElement=this;const i=this.childNodes.indexOf(next);if(i<0)this.childNodes.push(node);else this.childNodes.splice(i,0,node)}
 before(node){this.parentNode?.insertBefore(node,this)}
 remove(){if(this.parentElement){const a=this.parentElement.childNodes;a.splice(a.indexOf(this),1);this.parentElement=null}}
 replaceChild(node,old){const i=this.childNodes.indexOf(old);node.remove();old.parentElement=null;node.parentElement=this;this.childNodes[i]=node}
 replaceChildren(...nodes){for(const n of [...this.childNodes])n.remove();this.append(...nodes)}
 setAttribute(k,v){this.attributes[k]=String(v)}getAttribute(k){return this.attributes[k]??null}removeAttribute(k){delete this.attributes[k]}
 set textContent(v){this.replaceChildren();this._text=String(v)}get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('')}
 contains(n){return n===this||this.childNodes.some(c=>c.contains(n))}
 querySelector(selector){if(selector.startsWith('.'))return this.all().find(n=>n.classList.contains(selector.slice(1)))||null;return null}
 all(){return this.childNodes.flatMap(c=>[c,...c.all()])}
 closest(selector){for(let n=this;n;n=n.parentNode)if(selector.split(',').some(s=>s.startsWith('.')&&n.classList.contains(s.slice(1))))return n;return null}
 focus(){this.doc.activeElement=this}setSelectionRange(a,b,d){this.selectionStart=a;this.selectionEnd=b;this.selectionDirection=d}
 addEventListener(type,fn){const a=this.handlers.get(type)||[];a.push(fn);this.handlers.set(type,a)}removeEventListener(type,fn){this.handlers.set(type,(this.handlers.get(type)||[]).filter(f=>f!==fn))}
 event(type,extra={}){const e={key:'',target:this,preventDefault(){this.prevented=true},stopPropagation(){this.stopped=true},stopImmediatePropagation(){this.stopped=true},...extra};for(const f of this.handlers.get(type)||[])f(e);return e}
}
function extendedBindings(){return {projectId:'fixture-orchard',agents:{ada:'c:demo-ada',bram:'x:demo-bram',cloud:'e:demo-dot',cleo:'c:demo-cleo'},projects:{ada:'fixture-orchard',bram:'fixture-orchard',cloud:'fixture-orchard',cleo:'fixture-lighthouse'}}}
function harness(fixture,{query='?ui=workspace&demo&attachments=a03-five',workspace=true,demo=true,mutate=x=>x,withLifecycle=false,mutateLifecycle=x=>x,withFrames=false,globals={},bindings:suppliedBindings=null}={}){
 const doc={activeElement:null,createElement(tag){return new Node(this,tag)},createElementNS(_,tag){return new Node(this,tag)},createTextNode(text){return new Node(this,'',text)}};doc.documentElement=new Node(doc,'html');doc.body=new Node(doc,'body');doc.documentElement.append(doc.body);const events=new Node(doc);doc.addEventListener=events.addEventListener.bind(events);doc.removeEventListener=events.removeEventListener.bind(events);doc.event=events.event.bind(events);
 const ids={};for(const id of ['officer','ofBody','ofLog','ofRow','ofCompose','ofIn','ofSend','ofAddress','ofModelPicker','workspaceStationToolbar','workspaceOfficerHeader'])ids['#'+id]=new Node(doc,id==='ofIn'?'textarea':'div');
 doc.body.append(ids['#officer']);ids['#officer'].append(ids['#ofBody']);ids['#ofBody'].append(ids['#ofLog'],ids['#ofRow']);ids['#ofRow'].append(ids['#ofCompose'],ids['#workspaceStationToolbar'],ids['#ofModelPicker'],ids['#ofSend']);ids['#ofCompose'].append(ids['#ofIn']);
 const timers=new Map();let timerId=0,timerTime=0;const setTimer=(callback,delay=0)=>{const id=++timerId;timers.set(id,{callback,at:timerTime+delay});return id};
 const frames=new Map();let frameId=0;const requestFrame=callback=>{const id=++frameId;frames.set(id,callback);return id};
 let focused='c:demo-ada',officer=false,calls=[];const input=ids['#ofIn'];input.value='ordinary unsent';input.title='ordinary';
 const client={schemaVersion:1,fixture:true,enabled:true,snapshot(){calls.push('snapshot');return fixture.snapshot()},act(command){calls.push(structuredClone(command));return fixture.act(JSON.parse(JSON.stringify(command)))}};
 // Phase1-only mounting still executes the page's mandatory pure legacy-data validator.
 // Keep its real lifecycle closure without mounting lifecycle DOM or substituting a stub.
 const validation=withLifecycle?{}:vm.runInNewContext(presentation+'\n'+mutateLifecycle(lifecycle)+';({workspaceAttachmentsLegacyExpiryInvariant,workspaceAttachmentsAddTransition})',{URLSearchParams});
 const context={...validation,console,setTimeout:setTimer,clearTimeout:id=>timers.delete(id),...(withFrames?{requestAnimationFrame:requestFrame,cancelAnimationFrame:id=>frames.delete(id)}:{}),URLSearchParams,WORKSPACE:workspace,DEMO:demo,location:{search:query},document:doc,ofIn:input,ofEl:ids['#officer'],...(withLifecycle?{ofLog:ids['#ofLog'],A:{'c:demo-ada':{name:'Ada'},'c:demo-cleo':{name:'Cleo'},'x:demo-bram':{name:'Bram'},'e:demo-dot':{name:'Codex cloud'}},clock:seconds=>new Date(seconds*1000).toISOString().slice(11,16)+' UTC'}:{}),focus:focused,OFUI:{recipient:'agent',scope:'station',target:focused},cuTarget(){return context.focus||context.OFUI.target},workspaceOfficerDeskActive:()=>officer,$:key=>ids[key],CU:{drafts:new Map(),draftKey:null},Map,Set,JSON,Error,Number,Array,Object,String,...globals};
 const code=mutate(controller);const api=vm.runInNewContext(presentation+'\n'+(withLifecycle?mutateLifecycle(lifecycle):'')+'\n'+code+'\n'+liveDocuments+'\n;({'+names.map(n=>'workspaceAttachments'+n).join(',')+'})',context);
 const bindings=suppliedBindings||{projectId:'fixture-orchard',agents:{ada:'c:demo-ada',bram:'x:demo-bram',cloud:'e:demo-dot'}};
 return {api,context,client,doc,ids,calls,input,frames,timers,advanceTimers(ms){timerTime+=ms;for(const [id,timer] of [...timers])if(timer.at<=timerTime&&timers.delete(id))timer.callback()},flushFrames(){const pending=[...frames];frames.clear();for(const [,callback] of pending)callback()},install:()=>api.workspaceAttachmentsInstall(client,{bindings}),c:()=>api.workspaceAttachmentsInstall.current,officer(value){officer=value;context.focus=value?null:'c:demo-ada'},run(code){return vm.runInNewContext(code,context)}};
}
(async()=>{
 const {createAttachmentsFixture}=await import(require('node:url').pathToFileURL(path.join(__dirname,'../docs/fixtures/attachments/fixture.mjs')).href);
 const fixture=scenario=>createAttachmentsFixture({enabled:true,search:'?demo',scenario});let count=0;
 for(const options of [{query:'?demo&attachments=a03-five',workspace:false},{demo:false},{query:'?ui=workspace&demo'},{query:'?ui=workspace&attachments=a03-five'},{query:'?ui=workspace&demo&attachments=a99-unreviewed'}]){const h=harness(fixture('a03-five'),options);const before=h.doc.body.all().length;assert.equal(h.install(),false);assert.equal(h.calls.length,0);assert.equal(h.doc.body.all().length,before);assert.equal(h.api.workspaceAttachmentsOwnsInput(),false)}count++;
 let h=harness(fixture('a03-five'));assert.equal(h.api.workspaceAttachmentsInterceptSend(),false);assert.equal(h.api.workspaceAttachmentsDraftRestore(),false);assert.equal(h.api.workspaceAttachmentsEscape({key:'Escape'}),false);assert.equal(h.install(),true);assert.equal(h.input.value,'Compare the plan with the requirements. Tell me what is missing.');assert.equal(h.c().snapshot.draft.fileIds.length,5);count++;
 // Actual canonical action result, exact identity and new empty draft remain behind the send guard.
 const initial=h.c().snapshot,question='The exact edited sample';h.input.value=question;h.input.setSelectionRange(4,8,'forward');h.input.event('input');assert.equal(h.input.selectionStart,4);assert.equal(h.c().snapshot.draft.text,question);
 let command=h.calls.filter(x=>x.type).at(-1);assert.equal(command.type,'edit_text');assert.equal(command.draftId,initial.draft.id);assert.equal(command.revision,initial.draft.revision);assert.deepEqual(command.destination,initial.destination);
 assert.equal(h.api.workspaceAttachmentsInterceptSend(),true);assert.equal(h.c().snapshot.draft.fileIds.length,0);assert.equal(h.c().snapshot.messages.at(-1).text,question);assert.equal(h.input.value,'');assert.equal(h.api.workspaceAttachmentsInterceptSend(),true);assert.equal(h.calls.filter(x=>x.type==='send_sample').length,1);count++;
 // F116: a rejected visible edit must not dispatch the older canonical text; IME blocks every direct send.
 h=harness(fixture('a03-one'));h.install();h.input.value='bad\0text';h.input.event('input');h.api.workspaceAttachmentsInterceptSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,0);assert.equal(h.input.value,'bad\0text');h.input.value='composing';h.input.event('compositionstart');h.api.workspaceAttachmentsInterceptSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,0);h.input.event('compositionend');assert.equal(h.c().snapshot.draft.text,'composing');h.api.workspaceAttachmentsInterceptSend();assert.equal(h.c().snapshot.messages.at(-1).text,'composing');count++;
 h=harness(fixture('a02-preparing'));h.install();h.api.workspaceAttachmentsInterceptSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,0);const root=h.ids['#ofBody'],buttons=root.all().filter(n=>n.getAttribute('aria-label')?.startsWith('Cancel upload'));assert.equal(buttons.length,1);const old=h.c().snapshot;buttons[0].onclick();assert.equal(h.c().snapshot.draft.fileIds.length,2);assert.equal(h.c().snapshot.draft.text,old.draft.text);assert.equal(h.calls.filter(x=>x.type).at(-1).type,'cancel_upload');count++;
 h=harness(fixture('a03-five'));h.install();let open=h.ids['#ofBody'].all().filter(n=>n.classList.contains('workspace-attachments-open'));const held=open[2];held.focus();held.onkeydown({key:'Delete',preventDefault(){}});assert.equal(h.c().snapshot.draft.fileIds.length,4);assert.equal(h.doc.activeElement.getAttribute('aria-label').startsWith('orchard-delivery-plan'),true);open=h.ids['#ofBody'].all().filter(n=>n.classList.contains('workspace-attachments-open'));const origin=open[0];origin.onclick();assert.equal(origin.getAttribute('aria-expanded'),'true');const ev={key:'Escape',preventDefault(){},stopImmediatePropagation(){}};assert.equal(h.api.workspaceAttachmentsEscape(ev),true);assert.equal(h.doc.activeElement,origin);count++;
 // A native Enter on the focused X invokes click; focus must not fall back to the page body.
 h=harness(fixture('a03-five'));h.install();let focusedRemove=h.ids['#ofBody'].all().find(n=>n.getAttribute('aria-label')==='Remove requirements.md from this draft');focusedRemove.focus();focusedRemove.onclick();assert.equal(h.c().snapshot.draft.fileIds.length,4);assert.match(h.doc.activeElement.getAttribute('aria-label'),/^orchard-delivery-plan/);count++;
 // Fixture text never enters legacy draft storage. Departure restores ordinary value, sends stay consumed.
 h=harness(fixture('a03-one'));h.install();const save=sourceFunction('cuDraftSave'),restore=sourceFunction('cuDraftRestore');h.context.workspaceAttachmentsOwnsInput=h.api.workspaceAttachmentsOwnsInput;h.context.workspaceAttachmentsDraftRestore=h.api.workspaceAttachmentsDraftRestore;h.context.cuDraftKey=(...x)=>JSON.stringify(x);h.run(save);h.run('cuDraftSave()');assert.equal(h.context.CU.drafts.size,0);h.context.focus='x:demo-bram';h.context.OFUI.target='x:demo-bram';h.api.workspaceAttachmentsSync();assert.equal(h.input.value,'ordinary unsent');assert.equal(h.api.workspaceAttachmentsInterceptSend(),true);assert.equal(h.calls.filter(x=>x.type==='send_sample').length,0);h.context.focus='c:demo-ada';h.api.workspaceAttachmentsSync();assert.equal(h.input.value,'Review the attached file');h.api.workspaceAttachmentsDispose();assert.equal(h.input.value,'ordinary unsent');assert.equal(h.api.workspaceAttachmentsInterceptSend(),false);assert.equal(h.doc.documentElement.classList.contains('workspace-attachments'),false);count++;
 // All real page entry seams execute the installed interceptor before any legacy expression.
 h=harness(fixture('a03-one'));h.install();h.context.workspaceAttachmentsInterceptSend=h.api.workspaceAttachmentsInterceptSend;h.context.workspaceAttachmentsInterceptSuggestion=h.api.workspaceAttachmentsInterceptSuggestion;
 const sends=html.slice(html.indexOf('async function cuSend(){'),html.indexOf('/* officer evidence acceptance:'));
 h.run(sends+';globalThis.actualCuSend=cuSend;globalThis.actualOfSend=ofSend');await h.context.actualCuSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,1);await h.context.actualOfSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,1);
 const quick=html.split('\n').find(s=>s.startsWith("$('#ofQuick').onclick="));h.ids['#ofQuick']=new Node(h.doc);h.run(quick);const before=h.input.value;h.ids['#ofQuick'].onclick({target:{closest:()=>({dataset:{q:'A suggestion'}})}});assert.equal(h.context.OFUI.recipient,'agent');assert.equal(h.input.value,before);count++;
 h=harness(fixture('a01-officer'));h.context.OFUI.recipient='watch';h.context.OFUI.scope='station';h.officer(true);assert.equal(h.install(),true);h.input.event('compositionstart');h.api.workspaceAttachmentsInterceptSuggestion('suggestion');assert.equal(h.input.value,'');h.input.event('compositionend');h.api.workspaceAttachmentsInterceptSuggestion('suggestion');assert.equal(h.c().snapshot.messages.at(-1).text,'suggestion');count++;
 // Picker and local drag use only canonical IDs. Poison host file access proves no bytes/items/files reads.
 h=harness(fixture('a01-picker'));h.install();let choice=h.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-picker-choice'));choice.ondragstart();h.ids['#ofBody'].event('drop',{dataTransfer:{types:[]}});assert.equal(h.c().snapshot.draft.fileIds.length,1);assert.match(h.calls.filter(x=>x.type==='add_sample_files').at(-1).fileIds[0],/^fixture-file-/);
 const poison={types:['Files']};for(const k of ['files','items','getAsFile'])Object.defineProperty(poison,k,{get(){throw Error('Read host file data')}});const calls=h.calls.length;const drop=h.ids['#ofBody'].event('drop',{dataTransfer:poison});assert.equal(drop.prevented,true);assert.equal(h.calls.length,calls);assert.equal(h.doc.event('drop',{dataTransfer:poison}).prevented,true);assert.equal(h.doc.event('drop',{dataTransfer:{types:['text/plain']}}).prevented,undefined);h.api.workspaceAttachmentsDispose();assert.equal(h.doc.event('drop',{dataTransfer:poison}).prevented,undefined);count++;
 h=harness(fixture('a01-room'));h.context.focus=null;h.context.OFUI.recipient='watch';h.context.OFUI.scope='room';h.install();assert.equal(h.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-plus')).disabled,false);assert.equal(h.ids['#ofBody'].all().some(n=>n.classList.contains('workspace-attachments-tray')),false);h.api.workspaceAttachmentsInterceptSend();assert.equal(h.calls.filter(x=>x.type==='send_sample').length,0);count++;
 // Retained callbacks from a previous installation/draft cannot mutate a new canonical instance.
 h=harness(fixture('a01-picker'));h.install();choice=h.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-picker-choice'));const callback=choice.onclick;h.api.workspaceAttachmentsDispose();h.install();const n=h.calls.length;callback();assert.equal(h.calls.length,n);count++;
 // Causal proof: remove each real send guard and the actual extracted path hits an undefined legacy global.
 for(const entry of ['cuSend','ofSend']){const prefix="async function "+entry+"(){\n  if(typeof workspaceDictationBeforeSend==='function')workspaceDictationBeforeSend();\n  if(typeof workspaceLiveDocumentsSend==='function'&&workspaceLiveDocumentsSend())return;",guard=prefix+"\n  if(typeof workspaceAttachmentsInterceptSend==='function'&&workspaceAttachmentsInterceptSend())return;";assert(sends.includes(guard));const code=sends.replace(guard,prefix);const m=harness(fixture('a03-one'));m.install();m.context.workspaceAttachmentsInterceptSend=m.api.workspaceAttachmentsInterceptSend;m.run(code+';globalThis.actual='+entry);await assert.rejects(m.context.actual(),error=>error.name==='ReferenceError')}count++;
 // The fixed demo table is validated in full, including unused entries. No name or provider fallback.
 const expectedBindings=()=>({projectId:'fixture-orchard',agents:{ada:'c:demo-ada',bram:'x:demo-bram',cloud:'e:demo-dot'}});
 for(const key of ['ada','bram','cloud']){
   const q=harness(fixture('a03-one')),bindings=expectedBindings();bindings.agents[key]='fictional-wrong-id';
   assert.equal(q.api.workspaceAttachmentsInstall(q.client,{bindings}),false,key+' must be validated even when not selected');assert.equal(q.calls.length,0);
 }
 for(const mutateBindings of [b=>{b.projectId='wrong-project'},b=>{delete b.agents.bram},b=>{b.agents.other='c:demo-ada'}]){
   const q=harness(fixture('a03-one')),bindings=expectedBindings();mutateBindings(bindings);assert.equal(q.api.workspaceAttachmentsInstall(q.client,{bindings}),false);assert.equal(q.calls.length,0);
 }count++;
 // Deliberately malformed JSON values derived from a canonical snapshot, not additional fixture scenarios.
 for(const change of [s=>{s.destination.projectId=s.draft.destination.projectId='wrong-project'},s=>{s.destination.scope.agentId=s.draft.destination.scope.agentId='bram'},s=>{s.destination.agentId=s.draft.destination.agentId='unknown'},s=>{s.destination.scope.kind=s.draft.destination.scope.kind='room';s.destination.scope.agentId=s.draft.destination.scope.agentId=null}]){
   const q=harness(fixture('a03-one')),original=q.client.snapshot;q.client.snapshot=()=>{const s=original();change(s);return s};assert.equal(q.install(),false);assert.equal(q.api.workspaceAttachmentsOwnsInput(),false);assert.equal(q.ids['#ofBody'].all().some(n=>n.classList.contains('workspace-attachments-tray')),false);
 }
 // Derived recipient values test mapping only; no claim that Bram/cloud has an official phase-1 scenario.
 for(const [canonical,target] of [['ada','c:demo-ada'],['bram','x:demo-bram'],['cloud','e:demo-dot']]){
   const q=harness(fixture('a03-one')),original=q.client.snapshot;q.client.snapshot=()=>{const s=original();for(const d of [s.destination,s.draft.destination]){d.agentId=canonical;d.scope.agentId=canonical}return s};q.context.focus=target;q.context.OFUI.target=target;assert.equal(q.install(),true);assert.equal(q.api.workspaceAttachmentsOwnsInput(),true,canonical+' requires its exact supplied ID');assert.equal(q.input.value,'Review the attached file');q.context.focus='wrong:'+canonical;q.api.workspaceAttachmentsSync();assert.equal(q.api.workspaceAttachmentsOwnsInput(),false);assert.equal(q.api.workspaceAttachmentsInterceptSend(),true);assert.equal(q.calls.some(c=>c.type==='send_sample'),false);
 }count++;
 // Officer recipient null stays independent of station target; scope and surface must both match.
 for(const {scenario,scope,target,desk,owns} of [
   {scenario:'a01-officer',scope:'station',target:'c:demo-ada',desk:true,owns:true},
   {scenario:'a01-officer',scope:'station',target:'x:demo-bram',desk:true,owns:false},
   {scenario:'a01-officer',scope:'room',target:'c:demo-ada',desk:true,owns:false},
   {scenario:'a01-officer',scope:'station',target:'c:demo-ada',desk:false,owns:false},
   {scenario:'a01-room',scope:'room',target:'x:demo-bram',desk:false,owns:true},
   {scenario:'a01-room',scope:'station',target:'c:demo-ada',desk:false,owns:false}]){
   const q=harness(fixture(scenario));q.officer(desk);q.context.focus=desk?null:null;q.context.OFUI.target=target;q.context.OFUI.recipient='watch';q.context.OFUI.scope=scope;assert.equal(q.install(),true);assert.equal(q.api.workspaceAttachmentsOwnsInput(),owns);q.api.workspaceAttachmentsInterceptSend();assert.equal(q.context.OFUI.recipient,'watch');assert.equal(q.c().snapshot.destination.agentId,null);assert.equal(q.calls.some(c=>c.type==='send_sample'),false);
 }count++;
 // Execute the actual save AND restore helpers. The returning target's ordinary draft is captured only
 // after normal restore, so disposal restores that target's own text rather than the previously viewed one.
 {
   const q=harness(fixture('a03-one'));q.context.workspaceAttachmentsOwnsInput=q.api.workspaceAttachmentsOwnsInput;q.context.workspaceAttachmentsDraftRestore=q.api.workspaceAttachmentsDraftRestore;q.context.cuDraftKey=(...parts)=>JSON.stringify(parts);q.run(sourceFunction('cuDraftSave')+'\n'+sourceFunction('cuDraftRestore'));
   q.run('cuDraftSave()');const adaKey=q.context.CU.draftKey;q.context.CU.drafts.set(JSON.stringify(['x:demo-bram','agent','station']),'Bram ordinary text');q.install();q.context.focus='x:demo-bram';q.context.OFUI.target='x:demo-bram';q.run('cuDraftRestore()');assert.equal(q.input.value,'Bram ordinary text');assert.equal(q.api.workspaceAttachmentsOwnsInput(),false);q.api.workspaceAttachmentsSync();q.run('cuDraftSave()');q.context.focus='c:demo-ada';q.context.OFUI.target='c:demo-ada';q.run('cuDraftRestore()');assert.equal(q.input.value,'ordinary unsent');q.api.workspaceAttachmentsSync();assert.equal(q.input.value,'Review the attached file');q.api.workspaceAttachmentsDispose();assert.equal(q.input.value,'ordinary unsent');assert.equal(q.context.CU.drafts.get(adaKey),'ordinary unsent');assert.equal(q.context.CU.drafts.get(JSON.stringify(['x:demo-bram','agent','station'])),'Bram ordinary text');
 }count++;
 // Execute all actual owner restore entry gates up to the first legacy close operation. The sentinel
 // avoids mocking geometry, but proves feature leave runs before any owner controls/anchors move.
 for(const [name,guard] of [['workspaceOfficerRestore','WSO'],['workspaceStationRestore','WSR'],['workspaceOfficerDeskClose','WOD']]){
   const q=harness(fixture('a03-one'));q.install();q.context.workspaceAttachmentsBeforeRestore=q.api.workspaceAttachmentsBeforeRestore;q.context.WSO={active:true};q.context.WSR={toolbar:{}};q.context.workspaceOfficerDeskActive=()=>true;q.context.workspaceSettingsModelOwned=()=>{throw Error('owner-restore-sentinel')};q.context.workspaceOfficerHistoryActive=()=>{throw Error('owner-restore-sentinel')};q.run(sourceFunction(name));assert.throws(()=>q.run(name+'()'),/owner-restore-sentinel/);assert.equal(q.api.workspaceAttachmentsOwnsInput(),false,name+' must unmount before legacy restore');assert.equal(q.input.value,'ordinary unsent');assert.equal(q.doc.documentElement.classList.contains('workspace-attachments'),false);assert.equal(q.api.workspaceAttachmentsInterceptSend(),true,'installed fixture still owns the send boundary');
 }count++;
 // Complete the actual station restore's anchor loop with minimal parent/anchor nodes.
 {
   const q=harness(fixture('a03-one'));q.install();const parent=new Node(q.doc),anchor=new Node(q.doc),control=new Node(q.doc);parent.append(anchor);q.doc.body.append(parent,control);anchor.after=node=>parent.insertBefore(node,anchor.nextSibling);q.context.workspaceAttachmentsBeforeRestore=q.api.workspaceAttachmentsBeforeRestore;q.context.WSR={toolbar:new Node(q.doc),popover:new Node(q.doc),anchors:new Map([[control,anchor]]),oldWatchLabel:null};q.context.workspaceSettingsModelOwned=()=>false;q.context.workspaceStationClose=()=>{};q.run(sourceFunction('workspaceStationRestore'));q.run('workspaceStationRestore()');assert.equal(control.parentNode,parent);assert.equal(anchor.parentNode,null);assert.equal(q.context.WSR.anchors.size,0);assert.equal(q.context.WSR.toolbar,null);assert.equal(q.input.value,'ordinary unsent');assert.equal(q.api.workspaceAttachmentsOwnsInput(),false);
 }count++;
 // A retained callback after canonical selectScenario must hit the fixture's writer-generation fence.
 {
   const f=fixture('a01-picker'),q=harness(f);q.install();const callback=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-picker-choice')).onclick;f.selectScenario('a01-picker');const before=JSON.stringify(f.snapshot());callback();assert.equal(JSON.stringify(f.snapshot()),before);assert.equal(f.snapshot().draft.fileIds.length,0);
 }count++;
 // Causal pre-mutation quick guard: without it the old handler changes recipient, writes text and dispatches.
 function quickBoundary(code=quick){
   const q=harness(fixture('a03-one'));q.install();q.ids['#ofQuick']=new Node(q.doc);q.ids['#ofSuggestions']=new Node(q.doc);let legacy=0;q.context.OF={busy:false};q.context.workspaceOfficerUnavailable=()=>false;q.context.workspaceAttachmentsInterceptSuggestion=q.api.workspaceAttachmentsInterceptSuggestion;q.context.cuDraftSave=()=>{};q.context.cuDraftRestore=()=>{q.input.value='other ordinary draft'};q.context.ofContextPaint=()=>{};q.context.ofSend=()=>legacy++;q.run(code);const before=q.input.value;q.ids['#ofQuick'].onclick({target:{closest:()=>({dataset:{q:'Suggested question'}})}});assert.equal(q.context.OFUI.recipient,'agent');assert.equal(q.input.value,before);assert.equal(legacy,0);assert.equal(q.calls.some(c=>c.type==='send_sample'),false);
 }
 quickBoundary();const quickGuard="if(b&&typeof workspaceAttachmentsInterceptSuggestion==='function'&&workspaceAttachmentsInterceptSuggestion(b.dataset.q))return;";assert(quick.includes(quickGuard));assert.throws(()=>quickBoundary(quick.replace(quickGuard,'')),assert.AssertionError);count++;
 // Causal IME registration and visible-text reconciliation. These fail when the meaningful guard is removed.
 function composingBoundary(mutate=x=>x){const q=harness(fixture('a03-one'),{mutate});q.install();q.input.value='unfinished composition';q.input.event('compositionstart');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false);assert.equal(q.input.value,'unfinished composition')}
 composingBoundary();assert.throws(()=>composingBoundary(s=>s.replace('if(owned)composing=true','')),assert.AssertionError);
 function reconcileBoundary(mutate=x=>x){const q=harness(fixture('a03-one'),{mutate});q.install();q.input.value='bad\0visible';q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false)}
 reconcileBoundary();assert.throws(()=>reconcileBoundary(s=>s.replace('if(!edit())return;','')),assert.AssertionError);count++;
 // Actual Enter binding plus deskSubmitKey handles IME/modified Return, leaving ordinary text editing alone.
 {
   const q=harness(fixture('a03-one'));q.install();let attempts=0;q.context.cuSend=()=>{attempts++;q.api.workspaceAttachmentsInterceptSend()};q.run(sourceFunction('deskSubmitKey'));const line=html.split('\n').find(s=>s.startsWith("ofIn.addEventListener('keydown',ev=>{if(deskSubmitKey"));q.run(line);q.input.event('compositionstart');q.input.event('keydown',{key:'Enter',isComposing:true});assert.equal(attempts,0);q.input.event('keydown',{key:'Enter',keyCode:229});assert.equal(attempts,0);q.input.event('keydown',{key:'Enter',shiftKey:true});assert.equal(attempts,0);q.input.event('compositionend');const e=q.input.event('keydown',{key:'Enter'});assert.equal(e.prevented,true);assert.equal(attempts,1);assert.equal(q.calls.filter(c=>c.type==='send_sample').length,1);
 }count++;

 // F117: details/picker/remove close paths cannot erase a rejected visible edit before Send reconciles it.
 function rejectedEditSurvives(mutate=x=>x){
   for(const text of ['bad\0visible','x'.repeat(2001)]){
     const q=harness(fixture('a03-one'),{mutate});q.install();q.input.value=text;q.input.event('input');assert.equal(q.c().snapshot.draft.text,'Review the attached file');
     const chip=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-open'));chip.onclick();assert.equal(q.input.value,text,'Show details must preserve rejected text');
     q.doc.event('pointerdown',{target:q.ids['#ofBody']});assert.equal(q.c().snapshot.ui.detailsFileId,null);assert.equal(q.input.value,text,'Outside close must preserve rejected text');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false);
     const plus=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-plus'));plus.onclick();assert.equal(q.input.value,text,'Open picker must preserve rejected text');q.doc.event('pointerdown',{target:q.ids['#ofBody']});assert.equal(q.input.value,text,'Close picker must preserve rejected text');
     const remove=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-remove'));remove.onclick();assert.equal(q.c().snapshot.draft.fileIds.length,0);assert.equal(q.input.value,text,'Remove file must preserve rejected text');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false);
   }
 }
 rejectedEditSurvives();const preservation="&&!composing&&ofIn.value===d.text";assert(controller.includes(preservation));assert.throws(()=>rejectedEditSurvives(s=>s.replace(preservation,'')),assert.AssertionError);count++;
 // Composition remains visible and uncommitted through unrelated canonical actions.
 {
   const q=harness(fixture('a03-one'));q.install();q.input.event('compositionstart');q.input.value='途中の文章';const chip=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-open'));chip.onclick();q.doc.event('pointerdown',{target:q.ids['#ofBody']});assert.equal(q.input.value,'途中の文章');assert.equal(q.c().snapshot.draft.text,'Review the attached file');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false);q.input.event('compositionend');assert.equal(q.c().snapshot.draft.text,'途中の文章');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.c().snapshot.messages.at(-1).text,'途中の文章');
 }count++;
 // A legitimate empty picker selection still displays the canonical editable attachment suggestion.
 {
   const q=harness(fixture('a01-picker'));q.install();assert.equal(q.input.value,'');q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-picker-choice')).onclick();assert.equal(q.input.value,'Review the attached file');assert.equal(q.c().snapshot.draft.suggestedText,true);
 }count++;

 // An empty attachment preview preserves the existing native Send affordance and quiet no-op.
 function emptySendAppearance(mutate=x=>x){
   for(const scenario of ['a01-agent','a01-officer','a01-room']){
     const q=harness(fixture(scenario),{mutate});if(scenario==='a01-officer'){q.officer(true);q.context.OFUI.recipient='watch';q.context.OFUI.scope='station'}if(scenario==='a01-room'){q.context.focus=null;q.context.OFUI.recipient='watch';q.context.OFUI.scope='room'}q.ids['#ofSend'].title='Original Send label';q.ids['#ofSend'].disabled=false;assert.equal(q.install(),true);assert.equal(q.ids['#ofSend'].disabled,false,scenario+' empty Send keeps the page appearance');assert.equal(q.ids['#ofSend'].title,'Original Send label');q.api.workspaceAttachmentsInterceptSend();assert.equal(q.calls.some(c=>c.type==='send_sample'),false);assert.equal(q.ids['#ofBody'].textContent.includes('Write a message or attach a file.'),false,'empty Send is a quiet no-op');
   }
   const q=harness(fixture('a01-picker'),{mutate});q.install();const plus=q.ids['#ofBody'].all().find(n=>n.classList.contains('workspace-attachments-plus'));plus.onclick();assert.equal(q.ids['#ofBody'].textContent.includes('Write a message or attach a file.'),false,'closing an empty picker does not announce a block');
   const busy=harness(fixture('a02-preparing'),{mutate});busy.install();assert.equal(busy.ids['#ofSend'].disabled,true,'actual preparing files still disable Send');
 }
 emptySendAppearance();for(const [from,to] of [["ordinaryEmpty?saved.disabled:!normal.sendEnabled","!normal.sendEnabled"],["!snapshot.draft.fileIds.length&&!ofIn.value.trim()?'':snapshot.draft.sendBlockedReason","snapshot.draft.sendBlockedReason"],["!next.draft.fileIds.length&&!next.draft.text.trim()?'':next.draft.sendBlockedReason","next.draft.sendBlockedReason"]]){assert(controller.includes(from));assert.throws(()=>emptySendAppearance(s=>s.replace(from,to)),assert.AssertionError)}count++;
 // Motet's model lives inside the address cluster, which is hidden while listening.
 // Mounted dictation must keep the actual Files control out of that hidden ancestor.
 function nestedFilesProof(mutate=x=>x){
   for(const mounted of [false,true]){
     const q=harness(fixture('a01-officer'),{mutate,globals:{workspaceDictationPlacesFiles:()=>mounted}});
     q.officer(true);q.context.OFUI.recipient='watch';q.context.OFUI.scope='station';
     const row=q.ids['#ofRow'],address=q.ids['#ofAddress'],model=q.ids['#ofModelPicker'];
     row.insertBefore(address,q.ids['#ofSend']);address.append(model);assert.equal(q.install(),true);
     const files=q.ids['#officer'].all().find(node=>node.classList.contains('workspace-attachments-files'));
     assert(files);assert.equal(files.parentNode,mounted?row:address,'Canonical attachment paint respects mounted dictation placement');
     q.api.workspaceAttachmentsSync();assert.equal(files.parentNode,mounted?row:address,'Subsequent attachment paint cannot nest Files back into the hidden address');
   }
 }
 nestedFilesProof();const dictationPlacement="&&!(typeof workspaceDictationPlacesFiles==='function'&&workspaceDictationPlacesFiles())";
 assert(controller.includes(dictationPlacement));assert.throws(()=>nestedFilesProof(s=>s.replace(dictationPlacement,'')),assert.AssertionError);count++;
 // Run the exact live-document placement statement too: that independent consumer must
 // make the same choice even when it repaints after the dictation row observer runs.
 const livePlacement=liveDocuments.split('\n').find(line=>line.includes('const placement=o.placement?.()'));
 assert(livePlacement);
 function liveNestedFilesProof(mutate=x=>x){
   for(const mounted of [false,true])for(const surface of ['officer','station','room']){
     const doc={activeElement:null},row=new Node(doc),compose=new Node(doc),send=new Node(doc),address=new Node(doc),model=new Node(doc),plus=new Node(doc),files=new Node(doc);
     row.append(compose,address,send);address.append(model);
     const ctx={row,compose,controls:{plus,files},o:{placement:()=>({recipient:address,model}),send},env:()=>({surface}),workspaceDictationPlacesFiles:()=>mounted};
     vm.runInNewContext('{'+mutate(livePlacement)+'}',ctx);
     assert.equal(files.parentNode,surface==='officer'&&!mounted?address:row,'Live documents '+surface+' Files placement');
     if(mounted){address.hidden=true;assert.equal(address.contains(files),false,'Visible Files cannot depend on the hidden dictation address cluster');}
     vm.runInNewContext('{'+mutate(livePlacement)+'}',ctx);assert.equal(files.parentNode,surface==='officer'&&!mounted?address:row);
   }
 }
 liveNestedFilesProof();assert(livePlacement.includes(dictationPlacement));assert.throws(()=>liveNestedFilesProof(s=>s.replace(dictationPlacement,'')),assert.AssertionError);count++;
 // The canonical attachment painter and real stage layout share the same model,
 // Files, editor and microphone nodes through repeated idle/listening paints.
 function stageModelWithAttachments(mutate=x=>x,mutateController=x=>x){
  for(const mounted of [true,false]){
   const q=harness(fixture('a01-room'),{mutate:mutateController,globals:{workspaceDictationPlacesFiles:()=>mounted}}),row=q.ids['#ofRow'],model=q.ids['#ofModelPicker'],compose=q.ids['#ofCompose'],send=q.ids['#ofSend'];q.ids['#officer'].dataset.workspaceComposer='desktop';
   q.context.focus=null;q.context.OFUI.recipient='watch';q.context.OFUI.scope='room';
   const mic=new Node(q.doc),cancel=new Node(q.doc),wave=new Node(q.doc),header=new Node(q.doc),toggle=new Node(q.doc);mic.className='dictation-mic';cancel.hidden=wave.hidden=true;if(mounted){row.insertBefore(cancel,send);row.insertBefore(wave,send);row.insertBefore(mic,send)}header.append(model,toggle);q.ids['#officer'].append(header);
   q.context.WSO={active:true,header,desktop:{toggle}};q.context.workspaceOfficerDesktopEligible=()=>true;q.context.workspaceOfficerNarrowRestore=()=>{};q.context.workspaceOfficerDesktopHistoryPaint=()=>{};
   const init=()=>{};if(mounted)init.current={view:{mic,cancel,wave}};q.context.workspaceDictationInit=init;q.context.workspaceDictationTarget=()=> 'fictional-room-draft';
   q.run(mutate(sourceFunction('workspaceOfficerDesktopPaint'))+'\n'+sourceFunction('workspaceDictationOrderControls'));
   assert.equal(q.install(),true);const draft=q.input.value;assert(row.childNodes.indexOf(compose)<row.childNodes.indexOf(row.querySelector('.workspace-attachments-plus')),'Initial attachment paint orders words before Attach without waiting for a capture observer');
   for(const active of mounted?[false,true,false,true,false]:[false,false]){
     if(active)q.ids['#officer'].classList.add('dictation-active');else q.ids['#officer'].classList.remove('dictation-active');
     q.run('workspaceOfficerDesktopPaint()');q.api.workspaceAttachmentsSync();q.run('workspaceDictationOrderControls()');
     const plus=row.querySelector('.workspace-attachments-plus'),files=row.querySelector('.workspace-attachments-files');assert(plus&&files);
     assert.equal(model.parentNode,row,'Attachments never return the model to the stage header');assert.equal(model.nextSibling,mounted?mic:send);if(mounted)assert.equal(mic.nextSibling,send);
     assert.equal(q.input.parentNode,compose);assert.equal(q.input.value,draft,'Layout paints do not change the current canonical draft');
     const controls=row.childNodes;
     assert(controls.indexOf(compose)<controls.indexOf(plus),'Words precede the attachment control row');
     if(active)assert(controls.indexOf(files)<controls.indexOf(cancel));else assert(controls.indexOf(files)<controls.indexOf(model));
   }
  }
 }
 stageModelWithAttachments();assert.throws(()=>stageModelWithAttachments(s=>s.replace('row.insertBefore(model,slot);','WSO.header.insertBefore(model,WSO.desktop.toggle);')),assert.AssertionError);assert.throws(()=>stageModelWithAttachments(x=>x,s=>s.replace("if(typeof workspaceDictationOrderControls==='function')workspaceDictationOrderControls();",'')),assert.AssertionError);count++;
 console.log('ok workspace attachments controller: '+count+' behavior groups, canonical fixture and causal send guards');
})().catch(error=>{console.error(error);process.exitCode=1});
module.exports={harness,Node,sourceFunction,html,controller,extendedBindings};
