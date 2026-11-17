'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.resolve(__dirname,'../src/web/index.html'),'utf8');
const phase1=html.split('/* Workspace attachments presentation */')[1].split('/* end Workspace attachments presentation */')[0];
const source=html.split('/* Workspace attachments lifecycle presentation */')[1].split('/* end Workspace attachments lifecycle presentation */')[0];
const css=html.split('/* Workspace attachments lifecycle presentation styles */')[1].split('/* end Workspace attachments lifecycle presentation styles */')[0];
function api(code=source){
  const forbidden=()=>{throw Error('Presentation crossed pure boundary');};
  const blocked=new Proxy({},{get:forbidden});
  return vm.runInNewContext(phase1+'\n'+code+';({workspaceAttachmentsCreateDraftNotice,workspaceAttachmentsCreateMessages,workspaceAttachmentsLifecycleManifest,workspaceAttachmentsLifecyclePartialCopy})',{fetch:forbidden,setTimeout:forbidden,setInterval:forbidden,document:blocked,window:blocked,localStorage:blocked,sessionStorage:blocked});
}
class Node{
 constructor(tag='',text=''){this.tagName=tag.toUpperCase();this.childNodes=[];this.className='';this.attributes={};this._text=text;this.disabled=false;this.style={};}
 append(...nodes){for(const node of nodes){assert(node instanceof Node);assert.equal(node.parentNode,undefined);node.parentNode=this;this.childNodes.push(node);}}
 set textContent(value){this.childNodes=[];this._text=String(value);}
 get textContent(){return this._text+this.childNodes.map(node=>node.textContent).join('');}
 setAttribute(key,value){this.attributes[key]=String(value);}
 getAttribute(key){return this.attributes[key]??null;}
 set innerHTML(_){throw Error('Unsafe HTML');}
 addEventListener(){throw Error('Factory installed a handler');}
 focus(){throw Error('Factory moved focus');}
}
const doc={createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createTextNode:text=>new Node('',text)};
const clone=value=>JSON.parse(JSON.stringify(value));
const all=node=>node?[node,...node.childNodes.flatMap(all)]:[];
const cls=(node,name)=>all(node).filter(n=>n.className.split(' ').includes(name));
function assertPure(view){for(const node of all(view.root)){assert.equal(node.onclick,undefined);assert.equal(node.onkeydown,undefined);assert.equal(node.title,undefined);}if(view.root)assert.equal(view.root.parentNode,undefined);}
(async()=>{
 const {createAttachmentsFixture}=await import(require('node:url').pathToFileURL(path.resolve(__dirname,'../docs/fixtures/attachments/fixture.mjs')).href);
 const fixture=createAttachmentsFixture({enabled:true,search:'?demo'});
 const snapshot=key=>fixture.selectScenario(key);
 function invoke(action){const {element,token,...binding}=action;return fixture.act(clone({type:token,...binding}));}
 function expiredOfficer(){
  const f=createAttachmentsFixture({enabled:true,search:'?demo',scenario:'a08-doc'});let s=f.snapshot();s=f.act({type:'open_storage',authority:s.management.context});const ref=s.management.storage.entries.flatMap(e=>e.fileRefs).find(r=>r.fileId===s.messages[0].fileIds[0]);s=f.act({type:'open_expiry',authority:ref.authority});f.act({type:'set_expiry',authority:s.management.expiryDialog.authority,date:'2030-11-14'});s=f.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});return f.act({type:'advance_sample_deletion',authority:s.management.deletions.find(op=>op.cause.kind==='owner_expiry').authority});
 }

 const checks={
  canonicalNotices(A){
   for(const key of ['a05-saved','a05-scope','a05-resume','a05-conflict','a05-missing']){
    const s=snapshot(key),before=JSON.stringify(s),v=A.workspaceAttachmentsCreateDraftNotice(doc,s);assert(v.root,key);assertPure(v);assert.equal(JSON.stringify(s),before);assert.equal(v.readOnly,s.draft.editable!==true);assert.equal(v.hideUnresumedDraft,s.draft.state==='resume_available');
    assert.equal(v.root.getAttribute('role'),'status');
    if(['editing','saved'].includes(s.draft.state)){assert.equal(v.root.textContent,s.draft.notice);assert.equal(v.actions.length,0);assert.equal(cls(v.root,'workspace-attachments-saved').length,1);}
    else if(s.draft.state==='resume_available'){assert.equal(cls(v.root,'workspace-attachments-notice-copy')[0].childNodes[0].textContent,'A draft for Ada was open in another window');assert.deepEqual(Array.from(v.actions,a=>a.element.textContent),['Resume draft','Create new draft']);assert.equal(v.hideUnresumedDraft,true);}
    else if(s.draft.state==='conflict'){assert.equal(cls(v.root,'workspace-attachments-notice-copy')[0].childNodes[0].textContent,'This draft is being edited in another window');assert.deepEqual(Array.from(v.actions,a=>a.element.textContent),['Resume draft here','Create new draft']);assert.equal(v.hideUnresumedDraft,false);}
    else {assert.equal(v.root.textContent,'Draft unavailable—attach the files againThis window can no longer reach the earlier draft.Start a new draft');assert.deepEqual(Array.from(v.actions,a=>a.token),['create_draft']);assert.doesNotMatch(v.root.textContent,/Nothing from it was sent/);}
    for(const a of v.actions){assert.equal(a.element.type,'button');assert.equal(a.draftId,s.draft.id);assert.equal(a.revision,s.draft.revision);assert.equal(a.writerGeneration,s.draft.writerGeneration);assert.equal(JSON.stringify(a.destination),JSON.stringify(s.draft.destination));assert.notEqual(a.destination,s.draft.destination);assert.equal(a.element.onclick,undefined);}
   }
  },
  unknownNotice(A){
   const s=snapshot('a05-conflict');s.draft.noticeCode='future_notice';s.draft.notice='<b>Unknown & safe</b>. No inferred recovery.';const v=A.workspaceAttachmentsCreateDraftNotice(doc,s);assert.equal(v.root.textContent,s.draft.notice);assert.equal(v.actions.length,0);assert.equal(all(v.root).filter(n=>n.tagName==='B').length,0);
   s.draft.noticeCode='resume_available';assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).actions.length,0,'code/state mismatch stays literal');
   s.draft.noticeCode='draft_conflict';s.draft.actions=[];assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).actions.length,0);s.draft.actions=['resume_draft'];s.draft.writerGeneration=0;assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).actions.length,0);
   s.draft.writerGeneration=1;s.draft.fileIds.push('unknown-file');assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).actions.length,0);
  },
  canonicalMessages(A){
   for(const key of ['a06-preparing','a06-queued','a06-unknown','a06-partial','a06-cleaned']){
    const s=snapshot(key),before=JSON.stringify(s),v=A.workspaceAttachmentsCreateMessages(doc,s);assert(v.valid,key);assertPure(v);assert.equal(v.messages.length,1);const m=s.messages[0],r=v.messages[0];assert.deepEqual(Array.from(r.fileIds),m.fileIds);assert.equal(r.messageId,m.id);assert.equal(r.requestId,m.requestId);assert.equal(JSON.stringify(s),before);assert.equal(cls(r.root,'workspace-attachments-message-bubble')[0].textContent,m.text);assert.equal(r.cards.length,m.fileIds.length);assert.deepEqual(Array.from(r.cards,c=>c.fileId),m.fileIds);
    assert.equal(cls(r.root,'workspace-attachments-message-meta')[0].textContent,'Message to Ada · 3:58 PM');for(const card of r.cards){assert.equal(card.root.tagName,card.preview?'BUTTON':'DIV');assert.equal(card.root.getAttribute('role'),card.preview?null:'group');assert.equal(card.root.getAttribute('tabindex'),null);assert.equal(card.root.getAttribute('aria-label'),s.files.find(f=>f.id===card.fileId).name+', '+cls(card.root,'workspace-attachments-availability')[0].textContent);assert.equal(card.root.title,undefined);assert.equal(card.root.onclick,undefined);}
    const labels=cls(r.root,'workspace-attachments-availability').map(n=>n.textContent);
    if(m.state==='preparing_delivery'){assert.deepEqual(labels,['Saving for Ada…','Saving for Ada…']);assert.equal(cls(r.root,'workspace-attachments-message-status')[0].textContent,m.statusText);assert.equal(r.pending,null);assert.equal(r.actions.length,0);}
    if(m.state==='queued'){assert.deepEqual(labels,['Available to Ada','Available to Ada']);assert.equal(r.pending.statusText,'Queued · waiting for a reply');assert.equal(r.pending.fileCount,2);assert.equal(r.pending.qualifier,'This does not steer its current turn');assert.equal(r.actions.length,0);}
    if(m.state==='delivery_unknown'){assert.deepEqual(labels,['Available to Ada','Available to Ada']);assert.match(cls(r.root,'workspace-attachments-message-status')[0].textContent,/Delivery unknown · check status; do not resend/);assert.deepEqual(Array.from(r.actions,a=>a.token),['check_status']);assert.equal(r.pending.qualifier,'Do not resend');assert.doesNotMatch(r.root.textContent,/Failed|Read|Resend/);}
    if(m.state==='partial_not_sent'){assert.deepEqual(labels,['Saved for Ada','Not saved']);assert.equal(cls(r.root,'workspace-attachments-message-status')[0].textContent,'Not sent');assert.equal(cls(r.root,'workspace-attachments-send-failure')[0].childNodes[1].textContent,'delivery-plan.pdf was already copied into Ada\’s project folder before sending stopped. Ada has not received your message or requirements.md.');assert.deepEqual(Array.from(r.actions,a=>a.token),['finish_send','delete_saved_copies']);assert.deepEqual(Array.from(r.actions,a=>a.element.textContent),['Finish sending','Remove the copied file']);assert.equal(r.pending,null);}
    if(m.state==='not_sent'){assert.deepEqual(labels,['Not saved','Not saved']);assert.equal(cls(r.root,'workspace-attachments-message-status')[0].textContent,'Not sent · the copied file was removed');assert.equal(cls(r.root,'workspace-attachments-card-unavailable').length,2);assert.equal(cls(r.root,'workspace-attachments-send-failure').length,0);assert.equal(r.actions.length,0);assert.equal(r.pending,null);}
    for(const a of r.actions){assert.equal(a.messageId,m.id);assert.equal(a.requestId,m.requestId);assert.equal(a.revision,m.revision);assert.equal(a.operationGeneration,m.operationGeneration);assert.equal(JSON.stringify(a.destination),JSON.stringify(m.destination));assert.notEqual(a.destination,m.destination);}
   }
  },
  messageActionGates(A){
   for(const change of [m=>m.actions=[],m=>m.recovery.canCheck=false,m=>m.revision=0,m=>m.operationGeneration=0]){const s=snapshot('a06-unknown');change(s.messages[0]);const v=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(v.messages[0].actions.length,0);assert.match(v.root.textContent,/do not resend/);}
   for(const change of [m=>m.actions=[],m=>{m.recovery.canFinish=false;m.recovery.canDeleteSavedCopies=false;},m=>m.operationGeneration=0]){const s=snapshot('a06-partial');change(s.messages[0]);assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).messages[0].actions.length,0);}
   const s=snapshot('a06-unknown');s.messages[0].actions.push('finish_send','delete_saved_copies');s.messages[0].recovery.canFinish=true;s.messages[0].recovery.canDeleteSavedCopies=true;assert.deepEqual(Array.from(A.workspaceAttachmentsCreateMessages(doc,s).messages[0].actions,a=>a.token),['check_status']);
  },
  completeManifests(A){
   const edits=[s=>s.messages[0].fileIds.push('missing-file'),s=>s.messages[0].fileIds.push(s.messages[0].fileIds[0]),s=>s.files.push(clone(s.files[0])),s=>s.messages.push(clone(s.messages[0])),s=>s.messages[0].id='',s=>s.messages[0].requestId='',s=>s.messages[0].text=null,s=>s.messages[0].createdAt='not a time',s=>s.messages[0].createdAt='2030-02-31T15:58:00.000Z',s=>s.messages[0].deliveryPhase='unknown',s=>s.messages[0].destination.agentId='different-agent',s=>s.files[0].preparation='preparing',s=>s.files[0].error={code:'future'},s=>s.files[0].availability='private',s=>s.files[0].bytes=-1,s=>s.files[0].availableTo='Other recipient',s=>s.messages[0].state='future_state'];
   for(const change of edits){const s=snapshot('a06-queued');change(s);const v=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(v.valid,false);assert.equal(v.root,null);assert.equal(v.messages.length,0);assert(v.problems.length);}
   const s=snapshot('a06-partial');s.files[1].availability='available';s.files[1].availableTo='Ada';assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);
   const order=snapshot('a06-queued');order.messages[0].fileIds.reverse();assert.deepEqual(Array.from(A.workspaceAttachmentsCreateMessages(doc,order).messages[0].cards,c=>c.fileId),order.messages[0].fileIds);
  },
  independentAvailability(A){
   const s=snapshot('a06-unknown');s.files[0].availability='deleted';s.files[0].changedAt='2030-11-13T12:00:00.000Z';s.files[1].availability='missing';const v=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(v.valid,true);assert.deepEqual(cls(v.root,'workspace-attachments-availability').map(n=>n.textContent),['Deleted by you on 13\u00a0Nov','File missing: its saved copy was moved or changed on disk.']);assert.match(v.root.textContent,/Delivery unknown · check status; do not resend/);assert.deepEqual(Array.from(v.messages[0].actions,a=>a.token),['check_status']);s.files[0].changedAt=null;assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);const expired=expiredOfficer();assert.equal(cls(A.workspaceAttachmentsCreateMessages(doc,expired).root,'workspace-attachments-availability')[0].textContent,'Expired on 14\u00a0Nov');expired.files.find(f=>f.id===expired.messages[0].fileIds[0]).changedAt=null;assert.equal(A.workspaceAttachmentsCreateMessages(doc,expired).root,null);
  },
  safeTextAndTime(A){
   const s=snapshot('a06-queued');s.messages[0].text='<img src=x onerror="alert(1)"> & exact\ntext';s.files[0].name='<script>sample</script>.pdf';const before=JSON.stringify(s),v=A.workspaceAttachmentsCreateMessages(doc,s,t=>{assert.equal(t,Date.parse(s.messages[0].createdAt)/1000);return 'recorded clock';});assert.equal(cls(v.root,'workspace-attachments-message-bubble')[0].textContent,s.messages[0].text);assert.equal(cls(v.root,'workspace-attachments-message-meta')[0].textContent,'Message to Ada · recorded clock');assert.equal(cls(v.messages[0].cards[0].root,'workspace-attachments-name')[0].textContent,s.files[0].name);assert.equal(all(v.root).some(n=>n.tagName==='SCRIPT'||n.tagName==='IMG'),false);assert.equal(JSON.stringify(s),before);assertPure(v);
  },
  cleanupTruth(A){
   const s=snapshot('a06-cleaned'),m=s.messages[0];
   const v=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(v.valid,true);assert.equal(v.messages[0].actions.length,0);assert.doesNotMatch(v.root.textContent,/some files were saved|was saved in|has not been given/);assert.equal(cls(v.root,'workspace-attachments-message-status')[0].textContent,'Not sent · the copied file was removed');assert.equal(cls(v.root,'workspace-attachments-card-unavailable').length,2);assert.equal(cls(v.root,'workspace-attachments-card-not-saved').length,0);m.cleanup.fileIds.push(s.files[1].id);assert.equal(cls(A.workspaceAttachmentsCreateMessages(doc,s).root,'workspace-attachments-message-status')[0].textContent,'Not sent · the copied files were removed');m.cleanup.fileIds.push('wrong-file');assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);
  },
  receiptCards(A){
   for(const key of ['a06-preparing','a06-queued','a06-unknown','a06-partial','a06-cleaned']){
    const s=snapshot(key),view=A.workspaceAttachmentsCreateMessages(doc,s);
    for(const card of view.messages[0].cards){const file=s.files.find(f=>f.id===card.fileId);assert.equal(card.root.tagName,card.preview?'BUTTON':'DIV');assert.equal(card.root.getAttribute('role'),card.preview?null:'group');assert.equal(card.root.getAttribute('tabindex'),null);assert.equal(card.root.getAttribute('aria-label'),file.name+', '+cls(card.root,'workspace-attachments-availability')[0].textContent);assert.equal(card.root.getAttribute('type'),card.preview?'button':null);assert.equal(card.root.onclick,undefined);assert.equal(card.root.onkeydown,undefined);assert.equal(card.root.title,undefined);assert.equal(card.root.childNodes.flatMap(all).some(n=>['BUTTON','A','INPUT'].includes(n.tagName)),false);if(card.preview){assert.equal(card.preview.element,card.root);assert.equal(card.preview.type,'open_preview');assert.equal(card.preview.authority.kind,'source');assert(card.preview.authority.actions.includes('open_preview'));assert.equal(card.preview.authority.target.owner.messageId,view.messages[0].messageId);assert.equal(card.preview.authority.target.fileId,file.id)}}
   }
  },
  ownerPartialCopy(A){
   let s=snapshot('a06-partial'),m=s.messages[0],before=JSON.stringify(s),view=A.workspaceAttachmentsCreateMessages(doc,s),failure=cls(view.root,'workspace-attachments-send-failure')[0];
   assert.equal(failure.childNodes[0].textContent,'This message was not delivered');assert.equal(failure.childNodes[1].textContent,"delivery-plan.pdf was already copied into Ada’s project folder before sending stopped. Ada has not received your message or requirements.md.");assert.deepEqual(Array.from(view.messages[0].actions,a=>a.element.textContent),['Finish sending','Remove the copied file']);assert.deepEqual(Array.from(view.messages[0].actions,a=>a.token),['finish_send','delete_saved_copies']);assert.equal(JSON.stringify(s),before);
   m.statusText='STALE fallback must not become a partial title';view=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(cls(view.root,'workspace-attachments-send-failure')[0].childNodes[0].textContent,'This message was not delivered');assert.doesNotMatch(view.root.textContent,/STALE fallback/);
   const draft=snapshot('a03-five'),d=draft.draft;const actual=fixture.act({type:'send_sample',draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration});
   // Exercise display grammar with clones of actual selected/sent records, never a page scenario or action implementation.
   for(const copiedCount of [1,2,3,4]){
    s=clone(actual);m=s.messages[0];Object.assign(m,{state:'partial_not_sent',deliveryPhase:'pre_dispatch',statusText:'stale fallback',actions:['finish_send','delete_saved_copies'],recovery:{canCheck:false,canFinish:true,canDeleteSavedCopies:true}});
    const files=m.fileIds.map(id=>s.files.find(f=>f.id===id));files.forEach((f,i)=>{f.availability=i<copiedCount?'available':'not_saved';f.availableTo=i<copiedCount?m.destination.label:null;});
    const n=files.map(f=>f.name),expected=[
     `${n[0]} was already copied into Ada’s project folder before sending stopped. Ada has not received your message, ${n[1]}, ${n[2]}, ${n[3]} or ${n[4]}.`,
     `${n[0]} and ${n[1]} were already copied into Ada’s project folder before sending stopped. Ada has not received your message, ${n[2]}, ${n[3]} or ${n[4]}.`,
     `${n[0]}, ${n[1]} and ${n[2]} were already copied into Ada’s project folder before sending stopped. Ada has not received your message, ${n[3]} or ${n[4]}.`,
     `${n[0]}, ${n[1]}, ${n[2]} and ${n[3]} were already copied into Ada’s project folder before sending stopped. Ada has not received your message or ${n[4]}.`
    ][copiedCount-1];
    before=JSON.stringify(s);view=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(view.valid,true);failure=cls(view.root,'workspace-attachments-send-failure')[0];assert.equal(failure.childNodes[1].textContent,expected);assert.equal(view.messages[0].actions[1].element.textContent,'Remove the copied '+(copiedCount===1?'file':'files'));assert.deepEqual(Array.from(view.messages[0].fileIds),m.fileIds);assert.equal(JSON.stringify(s),before);
   }
   s=clone(actual);m=s.messages[0];m.state='partial_not_sent';m.deliveryPhase='pre_dispatch';before=JSON.stringify(s);assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).valid,false,'all-copied partial stays outside current canonical rendering contract');assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);const onlyHelper=A.workspaceAttachmentsLifecyclePartialCopy(m.destination.label,m.fileIds.map(id=>s.files.find(f=>f.id===id)));const n=m.fileIds.map(id=>s.files.find(f=>f.id===id).name);assert.equal(onlyHelper.body,`${n[0]}, ${n[1]}, ${n[2]}, ${n[3]} and ${n[4]} were already copied into Ada’s project folder before sending stopped. Ada has not received your message.`);assert.doesNotMatch(onlyHelper.body,/your message or|your message,/);assert.equal(onlyHelper.removeLabel,'Remove the copied files');assert.equal(JSON.stringify(s),before);
   s=snapshot('a06-partial');s.files[0].name='<img onerror="x">copied & literal.pdf';s.files[1].name='<b>missing & literal.md</b>';before=JSON.stringify(s);view=A.workspaceAttachmentsCreateMessages(doc,s);const body=cls(view.root,'workspace-attachments-send-failure')[0].childNodes[1].textContent;for(const file of s.files){assert.equal(body.split(file.name).length-1,1);assert.equal(view.messages[0].cards.find(c=>c.fileId===file.id).root.getAttribute('aria-label').startsWith(file.name+', '),true);}assert.equal(all(view.root).some(n=>['IMG','SCRIPT'].includes(n.tagName)),false);assert.equal(JSON.stringify(s),before);
  },
  officerSendReceiptF2(A){
   const command=(s,type,extra={})=>({type,draftId:s.draft.id,destination:s.draft.destination,revision:s.draft.revision,writerGeneration:s.draft.writerGeneration,...extra});
   for(const scenario of ['a04-budget','a01-officer']){
    let s=snapshot(scenario);
    if(scenario==='a04-budget')s=fixture.act(command(s,'remove_file',{fileId:s.files.find(f=>f.preparation==='failed').id}));
    else {s=fixture.act(command(s,'open_picker'));s=fixture.act(command(s,'add_sample_files',{fileIds:[s.pickerFiles[0].id]}));}
    const beforeSend=clone(s),sent=fixture.act(command(s,'send_sample')),untouched=JSON.stringify(sent),m=sent.messages[0],view=A.workspaceAttachmentsCreateMessages(doc,sent);
    assert.equal(m.destination.kind,'officer');assert.equal(m.state,'queued');assert.equal(sent.officerAnswer,null);assert.equal(m.text,beforeSend.draft.text);assert.deepEqual(m.fileIds,beforeSend.draft.fileIds);assert(m.fileIds.length>0);for(const id of m.fileIds){const file=sent.files.find(f=>f.id===id);assert.equal(file.availability,'available');assert.equal(file.availableTo,null);assert.equal(file.usedInAnswer,false);}
    assert.equal(view.valid,true,'F2: canonical Motet Send must retain its complete receipt');assert(view.root);assert.equal(view.messages.length,1);assert.deepEqual(Array.from(view.messages[0].fileIds),m.fileIds);assert.equal(view.messages[0].cards.length,m.fileIds.length);assert.deepEqual(cls(view.root,'workspace-attachments-availability').map(n=>n.textContent),m.fileIds.map(()=>"Prepared for this question"));assert.equal(cls(view.root,'workspace-attachments-message-bubble')[0].textContent,m.text);assert.equal(cls(view.root,'workspace-attachments-message-status')[0].textContent,'Queued · waiting for a reply');assert.doesNotMatch(view.root.textContent,/Available to null|Used in this answer|read it|project folder/);assert.equal(JSON.stringify(sent),untouched);const malformed=clone(sent);malformed.files.find(f=>f.id===m.fileIds[0]).availableTo=m.destination.label;assert.equal(A.workspaceAttachmentsCreateMessages(doc,malformed).valid,false,'F2: Motet availability must not invent a workspace recipient');
   }
   for(const scenario of ['a06-queued','a06-unknown'])for(const recipient of [null,'Other recipient']){const s=snapshot(scenario);s.files[0].availableTo=recipient;const view=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(view.valid,false,'F2: agent receipt still needs its exact availableTo recipient');assert.equal(view.root,null);assert.equal(view.messages.length,0);}
  },
  fixtureActionDescriptors(A){
   for(const key of ['a05-resume','a05-conflict']){
    const s=snapshot(key),notice=A.workspaceAttachmentsCreateDraftNotice(doc,s),resume=notice.actions.find(a=>a.token==='resume_draft');const next=invoke(resume);assert.equal(next.draft.id,s.draft.id);assert.equal(next.draft.text,s.draft.text);assert.deepEqual(next.draft.fileIds,s.draft.fileIds);assert.equal(next.draft.editable,true);assert(next.draft.writerGeneration>s.draft.writerGeneration);assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,next).hideUnresumedDraft,false);assert.equal(next.messages.length,0);assert.throws(()=>invoke(resume));
   }
   for(const key of ['a05-resume','a05-conflict','a05-missing']){
    const s=snapshot(key),create=A.workspaceAttachmentsCreateDraftNotice(doc,s).actions.find(a=>a.token==='create_draft'),next=invoke(create);assert.notEqual(next.draft.id,s.draft.id);assert.equal(next.draft.text,'');assert.deepEqual(next.draft.fileIds,[]);assert.equal(next.messages.length,0);assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,next).root,null);assert.doesNotMatch(JSON.stringify(next.draft),/earlier draft was unsent|Nothing from it was sent/);
   }
   let s=snapshot('a06-unknown'),old=clone(s.messages[0]),draft=clone(s.draft),action=A.workspaceAttachmentsCreateMessages(doc,s).messages[0].actions[0],next=invoke(action);assert.equal(next.messages[0].state,'delivery_unknown');assert.equal(next.messages[0].id,old.id);assert.equal(next.messages[0].requestId,old.requestId);assert.equal(next.messages.length,1);assert.deepEqual(next.draft,draft);assert.match(A.workspaceAttachmentsCreateMessages(doc,next).root.textContent,/do not resend/);assert.throws(()=>invoke(action));
   s=snapshot('a06-partial');old=clone(s.messages[0]);draft=clone(s.draft);action=A.workspaceAttachmentsCreateMessages(doc,s).messages[0].actions.find(a=>a.token==='finish_send');next=invoke(action);assert.equal(next.messages[0].state,'queued');assert.equal(next.messages[0].id,old.id);assert.equal(next.messages[0].requestId,old.requestId);assert.equal(next.messages[0].text,old.text);assert.deepEqual(next.messages[0].fileIds,old.fileIds);assert.equal(next.messages.length,1);assert.deepEqual(next.draft,draft);assert.equal(A.workspaceAttachmentsCreateMessages(doc,next).messages[0].actions.length,0);assert.throws(()=>invoke(action));
   s=snapshot('a06-partial');old=clone(s.messages[0]);draft=clone(s.draft);const actions=A.workspaceAttachmentsCreateMessages(doc,s).messages[0].actions;next=invoke(actions.find(a=>a.token==='delete_saved_copies'));assert.equal(next.messages[0].state,'not_sent');assert.equal(next.messages[0].deliveryPhase,'cleanup_fenced');assert.equal(next.messages[0].id,old.id);assert.equal(next.messages[0].requestId,old.requestId);assert.equal(next.messages[0].text,old.text);assert.deepEqual(next.messages[0].fileIds,old.fileIds);assert.deepEqual(next.draft,draft);let view=A.workspaceAttachmentsCreateMessages(doc,next);assert.equal(view.messages[0].actions.length,0);assert.equal(cls(view.root,'workspace-attachments-send-failure').length,0);assert.equal(cls(view.root,'workspace-attachments-card-unavailable').length,2);assert.match(view.root.textContent,/Not sent · the copied file was removed/);assert.doesNotMatch(view.root.textContent,/some files were saved/);assert.throws(()=>invoke(actions.find(a=>a.token==='finish_send')));
   s=snapshot('a03-one');const d=s.draft;next=fixture.act({type:'send_sample',draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration});view=A.workspaceAttachmentsCreateMessages(doc,next);assert.equal(view.messages.length,1);assert.equal(view.messages[0].pending.state,'queued');assert.deepEqual(Array.from(view.messages[0].fileIds),s.draft.fileIds);assert.equal(cls(view.root,'workspace-attachments-message-bubble')[0].textContent,s.draft.text);assert.equal(next.draft.fileIds.length,0);
  },
  gateAndEmpty(A){
   for(const change of [s=>s.enabled=false,s=>s.fixture=false,s=>s.schemaVersion=2]){const s=snapshot('a05-conflict');change(s);assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).root,null);assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);}
   const s=snapshot('a01-agent');assert.equal(A.workspaceAttachmentsCreateDraftNotice(doc,s).root,null);assert.equal(A.workspaceAttachmentsCreateMessages(doc,s).root,null);
  }
 };
 const A=api();for(const [name,check] of Object.entries(checks)){check(A);console.log('PASS '+name);}
 const mutants=[
  ['all-copied-remains-invalid','ownerPartialCopy',"||!files.some(f=>f.availability==='not_saved')",""],
  ['receipt-no-tabstop','receiptCards',"card.setAttribute('role','group');","card.setAttribute('role','group');card.setAttribute('tabindex','0');"],
  ['receipt-full-accessible-name','receiptCards',"card.setAttribute('aria-label',file.name+', '+workspaceAttachmentsLifecycleAvailability(file,message,answer.usedFileIds,answer.usedMessage,snapshot))","card.setAttribute('aria-label',workspaceAttachmentsLifecycleAvailability(file,message))"],
  ['owner-partial-title','ownerPartialCopy',"title:'This message was not delivered'","title:'Message not sent; some files were saved for the agent'"],
  ['owner-missing-file-list','ownerPartialCopy',"notCopied.map(file=>file.name),'or'","notCopied.map(file=>file.name),'and'"],
  ['owner-remove-plural','ownerPartialCopy',"copied.length===1?'file':'files'","true?'file':'files'"],
  ['owner-all-copied-display-only','ownerPartialCopy',"const missing=notCopied.length?","const missing=true?"],
  ['F2-officer-null-availability','officerSendReceiptF2',"message.destination.kind==='officer'?file.availableTo!==null:typeof file.availableTo!=='string'||file.availableTo!==message.destination.label","typeof file.availableTo!=='string'||file.availableTo!==message.destination.label"],
  ['F2-agent-recipient-remains-strict','officerSendReceiptF2',"message.destination.kind==='officer'?file.availableTo!==null:typeof file.availableTo!=='string'||file.availableTo!==message.destination.label","message.destination.kind==='officer'?file.availableTo!==null:false"],
  ['F2-no-agent-workspace-label','officerSendReceiptF2',"return 'Prepared for this question'","return 'Available to '+file.availableTo"],
  ['unknown-to-failed','canonicalMessages',"message.state==='partial_not_sent'?'Not sent':message.statusText","message.state==='delivery_unknown'?'Failed':message.statusText"],
  ['complete-manifest','completeManifests','{valid:problems.length===0,problems,messages:problems.length?[]:messages}','{valid:true,problems,messages:messages}'],
  ['ordered-manifest','completeManifests','for(const id of message.fileIds)','for(const id of message.fileIds.slice().reverse())'],
  ['readonly-notice-code','unknownNotice',"code==='draft_conflict'&&draft.state==='conflict'","draft.state==='conflict'"],
  ['recovery-flag','messageActionGates',"message.recovery?.canCheck===true","true"],
  ['readonly-phase-gate','completeManifests',"if(message.deliveryPhase!==expectedPhase)","if(false)"],
  ['unavailable receipt stays passive','receiptCards',"authority?'button':'div'","'button'"],
  ['prepared-sent-files','completeManifests',"file.preparation!=='ready'","false"],
  ['unknown-qualifier','canonicalMessages',"qualifier:message.state==='delivery_unknown'?'Do not resend'","qualifier:message.state==='delivery_unknown'?'Safe to resend'"],
  ['cleanup-outcome','cleanupTruth',"message.cleanup.fileIds.length===1?'file was':'files were'","true?'file was':'files were'"],
 ];
 for(const [name,test,from,to] of mutants){assert(source.includes(from),'mutation target '+name);assert.throws(()=>checks[test](api(source.replace(from,to))),'survived mutant '+name);console.log('KILLED '+name);}
 function cssChecks(code){
  const rule=selector=>{const start=code.indexOf(selector+'{');assert(start>=0,'CSS rule '+selector);return code.slice(start+selector.length+1,code.indexOf('}',start));};
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-draft-notice'),/margin-bottom:6px/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-sent-card'),/cursor:default/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-sent-card>.workspace-attachments-name'),/color:#1c1b19;.*font:600/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-card-not-saved>.workspace-attachments-name'),/color:#b3261e;font-weight:600/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-card-not-saved>.workspace-attachments-availability'),/color:#b3261e;font-weight:600/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-card-unavailable>.workspace-attachments-name'),/color:var\(--ws-mute\);font-weight:500/);
  assert.match(rule('html.workspace.workspace-attachments #officer .workspace-attachments-card-unavailable'),/border-style:dashed/);assert.doesNotMatch(code,/workspace-attachments-sent-card:(?:hover|disabled)/);
 }
 cssChecks(css);
 const cssMutants=[['banner-gap','margin-bottom:6px','margin-bottom:0'],['receipt-name-ink','color:#1c1b19;font:600','color:var(--ws-mute);font:600'],['missing-name-red','workspace-attachments-card-not-saved>.workspace-attachments-name{color:#b3261e','workspace-attachments-card-not-saved>.workspace-attachments-name{color:#66635d'],['missing-availability-weight','workspace-attachments-card-not-saved>.workspace-attachments-availability{color:#b3261e;font-weight:600','workspace-attachments-card-not-saved>.workspace-attachments-availability{color:#b3261e;font-weight:400'],['cleaned-quiet','color:var(--ws-mute);font-weight:500','color:var(--ws-mute);font-weight:600']];
 for(const [name,from,to] of cssMutants){assert(css.includes(from),'CSS mutation target '+name);assert.throws(()=>cssChecks(css.replace(from,to)),'survived CSS mutant '+name);console.log('KILLED CSS '+name);}
 for(const line of css.split('\n').filter(line=>line.includes('{')))for(const selector of line.split('{')[0].split(','))assert(selector.trim().startsWith('html.workspace.workspace-attachments #officer '),'ungated CSS '+selector);
 assert(!/addEventListener|\.focus\(|fetch\(|setTimeout|setInterval|localStorage|sessionStorage|innerHTML|selectScenario/.test(source),'pure presentation source boundary');
 console.log('PASS '+Object.keys(checks).length+' behavior groups, '+mutants.length+' JS causal mutants, '+cssMutants.length+' CSS causal mutants; canonical fixture records, detached DOM, CSS scope. DOM harness has no layout.');
})().catch(error=>{console.error(error);process.exitCode=1;});
