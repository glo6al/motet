'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const phase1=html.split('/* Workspace attachments presentation */')[1].split('/* end Workspace attachments presentation */')[0];
const source=html.split('/* Workspace attachments lifecycle presentation */')[1].split('/* end Workspace attachments lifecycle presentation */')[0];
function api(code=source){const forbidden=()=>{throw Error('Pure presentation crossed a boundary')};return vm.runInNewContext(phase1+'\n'+code+';({workspaceAttachmentsLifecycleManifest,workspaceAttachmentsValidateReply,workspaceAttachmentsValidateAnswer,workspaceAttachmentsCreateMessages,workspaceAttachmentsCreateOfficerAnswer,workspaceAttachmentsManagement})',{fetch:forbidden,setTimeout:forbidden,setInterval:forbidden,document:new Proxy({},{get:forbidden}),localStorage:new Proxy({},{get:forbidden})})}
class Node{
 constructor(tag='',text=''){this.tagName=tag.toUpperCase();this.childNodes=[];this.attributes={};this.className='';this._text=text;this.disabled=false;}
 append(...nodes){for(const n of nodes){assert.equal(n.parentNode,undefined);n.parentNode=this;this.childNodes.push(n)}}
 set textContent(value){this.childNodes=[];this._text=String(value)}get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('')}
 setAttribute(k,v){this.attributes[k]=String(v)}getAttribute(k){return this.attributes[k]??null}
 set innerHTML(_){throw Error('HTML injection')}addEventListener(){throw Error('Interactive factory')}focus(){throw Error('Focus steal')}
}
const doc={createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createTextNode:text=>new Node('',text)};
const all=root=>root?[root,...root.childNodes.flatMap(all)]:[],cls=(root,name)=>all(root).filter(n=>n.className.split(' ').includes(name));
const clone=x=>JSON.parse(JSON.stringify(x));
function inert(view){for(const n of all(view.root)){assert.equal(n.onclick,undefined);assert.equal(n.onkeydown,undefined);assert.equal(n.getAttribute('href'),null);assert.equal(n.getAttribute('data-n'),null);assert.equal(n.getAttribute('data-deck'),null);if(n.tagName==='BUTTON')assert([...(view.actions||[]),...(view.messages||[]).flatMap(m=>m.cards.map(c=>c.preview).filter(Boolean))].some(a=>a.element===n&&['open_citation','open_preview'].includes(a.type)&&a.authority?.actions.includes(a.type)),'pure enabled source control requires its detached authority descriptor')}}
// Keep this synthetic shared-file ownership test coherent with the reviewed typed catalogue.
function rebindCitationOwner(s,message){
 const answer=s.officerAnswer,previous=answer.replyTo;
 answer.replyTo={messageId:message.id,requestId:message.requestId,destination:clone(message.destination)};
 for(const rows of [s.management.history.entries,s.management.storage.entries]){
  const original=rows.find(e=>e.requestId===previous.requestId&&e.messageId===previous.messageId),entry=clone(original);
  entry.entryId+='-rebound';entry.requestId=message.requestId;entry.messageId=message.id;entry.destination=clone(message.destination);entry.fileIds=clone(message.fileIds);
  for(const ref of entry.fileRefs)if(ref.authority)Object.assign(ref.authority.target.owner,{messageId:message.id,requestId:message.requestId,destination:clone(message.destination),fileIds:clone(message.fileIds)});
  rows.push(entry);
 }
 for(const ref of s.management.sourceRefs)if(ref.authority?.target.owner.kind==='request')Object.assign(ref.authority.target.owner,{messageId:message.id,requestId:message.requestId,destination:clone(message.destination),fileIds:clone(message.fileIds)});
 for(const citation of answer.citations)if(citation.navigation.kind==='preview')for(const target of [citation.navigation.citationTarget,citation.navigation.authority.target])Object.assign(target.owner,{messageId:message.id,requestId:message.requestId,destination:clone(message.destination),fileIds:clone(message.fileIds)});
}
(async()=>{
 const {createAttachmentsFixture}=await import(require('node:url').pathToFileURL(path.join(__dirname,'../docs/fixtures/attachments/fixture.mjs')).href);
 const snapshot=scenario=>createAttachmentsFixture({enabled:true,search:'?demo',scenario}).snapshot();let groups=0;
 function datedFixture(scenario,date,all=false){
  const f=createAttachmentsFixture({enabled:true,search:'?demo',scenario});let s=f.snapshot();s=f.act({type:'open_storage',authority:s.management.context});
  if(all)s=f.act({type:'open_expiry_all',authority:s.management.views.storage.authority});
  else{const fileId=s.messages[0].fileIds[0],ref=s.management.storage.entries.flatMap(e=>e.fileRefs).find(r=>r.fileId===fileId);s=f.act({type:'open_expiry',authority:ref.authority});}
  f.act({type:'set_expiry',authority:s.management.expiryDialog.authority,date});return f;
 }

 function validAnswer(A=api()){
  for(const scenario of ['a08-doc','a08-compare']){const s=snapshot(scenario),before=JSON.stringify(s),v=A.workspaceAttachmentsValidateAnswer(s),view=A.workspaceAttachmentsCreateOfficerAnswer(doc,s),messages=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(v.valid,true);assert.equal(v.kind,'answer');assert.equal(v.usedFileIds.length,s.files.length);assert.equal(cls(messages.root,'workspace-attachments-availability').every(n=>n.textContent==='Used in this answer'),true);assert.equal(cls(view.root,'workspace-attachments-citation-upload').length,2);assert.equal(cls(view.root,'workspace-attachments-citation-activity').length,scenario==='a08-compare'?2:0);assert.equal(cls(view.root,'workspace-attachments-answer-paragraph').length,2);assert(view.root.textContent.includes('Motet · Haiku 5.5 · analysis only · sample answer'));inert(view);inert(messages);assert.equal(view.actions.length,2);for(const citation of cls(view.root,'workspace-attachments-citation')){assert.equal(citation.tagName,citation.className.includes('workspace-attachments-citation-upload')?'BUTTON':'SPAN');assert.equal(citation.getAttribute('role'),null);assert.equal(citation.getAttribute('tabindex'),null)}assert.equal(JSON.stringify(s),before)}
 }validAnswer();groups++;
 function rejected(change,A=api(),scenario='a08-compare'){
  const s=snapshot(scenario),body=s.officerAnswer.text;change(s);assert.equal(A.workspaceAttachmentsValidateAnswer(s).valid,false);const view=A.workspaceAttachmentsCreateOfficerAnswer(doc,s),messages=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(cls(view.root,'workspace-attachments-answer-paragraph').length,0);assert.equal(cls(view.root,'workspace-attachments-citation-upload').length,0);assert.equal(view.root.textContent.includes(body),false);assert.equal(messages.root?.textContent.includes('Used in this answer')||false,false);inert(view);
 }
 const mutations=[
  s=>s.officerAnswer.replyTo.requestId+='-wrong',s=>s.officerAnswer.replyTo.messageId+='-wrong',s=>s.officerAnswer.replyTo.destination.projectLabel+=' changed',s=>s.officerAnswer.replyTo.destination.scope.agentId='bram',
  s=>s.officerAnswer.model.id='current-composer-model',s=>s.officerAnswer.model.label='',s=>s.officerAnswer.sample=false,s=>s.officerAnswer.analysisOnly=false,s=>s.officerAnswer.sourceValidation='invalid',s=>s.officerAnswer.revision=0,s=>s.officerAnswer.revision++,
  s=>s.officerAnswer.citations[1].id=s.officerAnswer.citations[0].id,s=>s.officerAnswer.citations[0].fileId='missing',s=>s.officerAnswer.citations[0].kind='native',s=>s.officerAnswer.citations[0].activityId='12',s=>s.officerAnswer.citations[2].fileId=s.files[0].id,s=>s.officerAnswer.citations[2].location={page:1},
  s=>s.officerAnswer.citations[0].location={page:0},s=>s.officerAnswer.citations[0].location={page:2,lineStart:1},s=>s.officerAnswer.citations[1].location={lineStart:17,lineEnd:10},s=>s.officerAnswer.citations[0].navigation.reason='fixture_activity_only',s=>s.officerAnswer.citations[2].navigation.kind='native',s=>s.officerAnswer.citations[0].available=false,
  s=>s.files[0].usedInAnswer=false,s=>s.files[0].availability='missing',s=>{s.files[0].availability='expired';s.files[0].changedAt='2030-11-14T00:00:00.000Z'},s=>s.files[0].availableTo='Ada',s=>s.files[0].preparation='preparing',s=>s.files.push(clone(s.files[0])),s=>s.messages[0].fileIds.push('missing'),s=>s.messages.push(clone(s.messages[0])),s=>s.officerAnswer.text='bad\0text',s=>s.destination.kind='agent'
 ];for(const change of mutations)rejected(change);groups+=mutations.length;
 function crossMessage(A=api()){
  const s=snapshot('a08-compare'),second=clone(s.messages[0]);second.id+='-other';second.requestId+='-other';second.fileIds=[s.files[1].id];s.messages[0].fileIds=[s.files[0].id];s.messages.push(second);assert.equal(A.workspaceAttachmentsLifecycleManifest(s).valid,true,'both retained messages are independently valid');assert.equal(A.workspaceAttachmentsValidateAnswer(s).valid,false,'other valid message is not source authority');assert.equal(A.workspaceAttachmentsCreateOfficerAnswer(doc,s).root.textContent.includes(s.officerAnswer.text),false);
 }crossMessage();groups++;
 function sharedFileRows(A=api()){
  const s=snapshot('a08-doc'),second=clone(s.messages[0]);second.id+='-second';second.requestId+='-second';s.messages.push(second);assert.equal(A.workspaceAttachmentsLifecycleManifest(s).valid,true,'a file can occur in two valid retained messages');
  let cards=cls(A.workspaceAttachmentsCreateMessages(doc,s).root,'workspace-attachments-availability');assert.deepEqual(cards.map(n=>n.textContent),['Used in this answer','Prepared for this question'],'only the exact matched message earns Used');
  s.officerAnswer.replyTo={messageId:second.id,requestId:second.requestId,destination:clone(second.destination)};assert.equal(A.workspaceAttachmentsValidateAnswer(s).valid,false,'stale citation owner cannot follow an answer-only rebind');s.officerAnswer.replyTo=clone({messageId:s.messages[0].id,requestId:s.messages[0].requestId,destination:s.messages[0].destination});rebindCitationOwner(s,second);
  cards=cls(A.workspaceAttachmentsCreateMessages(doc,s).root,'workspace-attachments-availability');assert.deepEqual(cards.map(n=>n.textContent),['Prepared for this question','Used in this answer'],'rebinding the answer moves only its row attribution');
 }sharedFileRows();groups++;

 function replyGuards(A=api()){
  const s=snapshot('a07-answered'),text=s.messages[0].reply.text,view=A.workspaceAttachmentsCreateMessages(doc,s);assert.equal(cls(view.root,'workspace-attachments-agent-reply').length,1);assert(view.root.textContent.includes('Reply from Ada'));assert(view.root.textContent.includes(text));assert(s.files.every(f=>f.retention.expiresAt===null),'default retained files have no expiry');assert(!view.root.textContent.includes('until ')&&!view.root.textContent.includes(' · expires '),'default reply must not promise timed expiry');const dated=datedFixture('a07-answered','2030-11-20').snapshot();assert(A.workspaceAttachmentsManagement(dated));assert(A.workspaceAttachmentsCreateMessages(doc,dated).root.textContent.includes(' · expires 20\u00a0Nov'),'explicit policy displays the chosen date, not its exclusive deadline');assert(!view.root.textContent.includes('Used in this answer'));inert(view);
  for(const change of [r=>r.sample=false,r=>r.messageId+='wrong',r=>r.requestId+='wrong',r=>r.destination.projectLabel+='wrong',r=>r.author.kind='officer',r=>r.author.agentId='bram',r=>r.author.label='Cleo',r=>r.text='bad\0text']){const bad=clone(s);change(bad.messages[0].reply);assert.equal(A.workspaceAttachmentsValidateReply(bad,bad.messages[0]).valid,false);const rendered=A.workspaceAttachmentsCreateMessages(doc,bad);assert(!rendered.root.textContent.includes(text));assert(rendered.root.textContent.includes('could not be verified'))}
  delete s.messages[0].reply;assert.equal(cls(A.workspaceAttachmentsCreateMessages(doc,s).root,'workspace-attachments-agent-reply').length,0,'Answered alone never fabricates reply');
 }replyGuards();groups++;
 {
  const A=api(),s=snapshot('a07-answered');for(const expiry of [null,'not a date','2030-02-31T00:00:00.000Z']){s.files.forEach(f=>f.retention.expiresAt=expiry);assert.equal(!!A.workspaceAttachmentsManagement(s),expiry===null,'malformed or policy-inconsistent expiry metadata is rejected');assert(!A.workspaceAttachmentsCreateMessages(doc,s).root.textContent.includes(' · expires '))}const pinned=datedFixture('a06-queued','2030-11-20',true).snapshot();assert(pinned.messages[0].fileIds.every(id=>pinned.files.find(f=>f.id===id).retention.pinned));assert(A.workspaceAttachmentsManagement(pinned));assert(A.workspaceAttachmentsCreateMessages(doc,pinned).root.textContent.includes(' · expires 20\u00a0Nov'),'an explicit future date remains visible for protected unfinished files');
 }groups++;
 function unavailable(A=api()){
  const s=snapshot('a08-gone');s.officerAnswer.text='STALE PRIVATE ANSWER';const v=A.workspaceAttachmentsCreateOfficerAnswer(doc,s);assert.equal(v.valid,true);assert(!v.root.textContent.includes('STALE PRIVATE ANSWER'));assert(v.root.textContent.includes(s.officerAnswer.notice));assert.equal(cls(v.root,'workspace-attachments-citation-upload').length,0);assert.equal(all(v.root).filter(n=>n.tagName==='BUTTON').length,0);inert(v);assert(A.workspaceAttachmentsCreateMessages(doc,s).root.textContent.includes('Deleted by you on 13\u00a0Nov'));
 }unavailable();groups++;
 {
  const A=api(),s=snapshot('a08-forward');assert.equal(A.workspaceAttachmentsValidateAnswer(s).kind,'refusal');const v=A.workspaceAttachmentsCreateOfficerAnswer(doc,s);assert(v.root.textContent.includes(s.officerAnswer.notice));assert.equal(cls(v.root,'workspace-attachments-answer-paragraph').length,0);inert(v);s.officerAnswer.replyTo={messageId:'invented'};assert.equal(A.workspaceAttachmentsValidateAnswer(s).valid,false);
 }groups++;
 {
  const A=api(),s=snapshot('a08-doc'),literal='<img src=x onerror=alert(1)> & <script>bad</script>';s.officerAnswer.text=literal;s.officerAnswer.model.label=literal;s.officerAnswer.citations[0].label=literal;const v=A.workspaceAttachmentsCreateOfficerAnswer(doc,s);assert(v.root.textContent.includes(literal));assert(!all(v.root).some(n=>['IMG','SCRIPT','A'].includes(n.tagName)));inert(v);s.officerAnswer=null;assert.equal(A.workspaceAttachmentsCreateOfficerAnswer(doc,s).root,null);
 }groups++;
 function officerMetadata(A=api()){
  const s=snapshot('a08-doc'),view=A.workspaceAttachmentsCreateMessages(doc,s,()=> '4:02 PM',{scopeLabel:'Ada'}),row=view.messages[0];assert.equal(cls(view.root,'workspace-attachments-message-meta')[0].textContent,'You to Motet · about Ada · 4:02 PM');assert.equal(cls(view.root,'workspace-attachments-message-status').length,0,'Motet Answered has no hidden placeholder');assert.equal(row.scrollAnchor,cls(view.root,'workspace-attachments-message-meta')[0]);
  for(const d of [s.destination,s.draft.destination,s.messages[0].destination]){d.scope={kind:'room',agentId:null}}s.officerAnswer=null;const room=A.workspaceAttachmentsCreateMessages(doc,s,()=> '4:02 PM');assert.equal(cls(room.root,'workspace-attachments-message-meta')[0].textContent,'You to Motet · whole stage · 4:02 PM');assert.equal(cls(room.root,'workspace-attachments-message-status').length,0);
  for(const scenario of ['a07-answered','a06-queued','a06-unknown']){const v=A.workspaceAttachmentsCreateMessages(doc,snapshot(scenario));assert.equal(cls(v.root,'workspace-attachments-message-status').length,1);assert.equal(v.messages[0].scrollAnchor,cls(v.root,'workspace-attachments-message-status')[0])}
 }officerMetadata();groups++;
 function noticeParts(A=api()){
  for(const scenario of ['a08-forward','a08-gone']){const s=snapshot(scenario),parts=s.officerAnswer.noticeParts,view=A.workspaceAttachmentsCreateOfficerAnswer(doc,s);assert.equal(view.valid,true);assert(view.root.textContent.includes(parts.title+' '+parts.body));assert.equal(all(view.root).filter(n=>n.tagName==='BUTTON').length,0);if(scenario==='a08-gone'){assert.equal(all(view.root).filter(n=>n.tagName==='SVG').length,1);assert.equal(all(view.root).find(n=>n.tagName==='B').textContent,parts.title)}else{assert.equal(cls(view.root,'workspace-attachments-answer-notice-title')[0].textContent,parts.title);assert.equal(cls(view.root,'workspace-attachments-answer-notice-body')[0].textContent,parts.body)}
   for(const change of [a=>delete a.noticeParts,a=>a.noticeParts=null,a=>a.noticeParts.title='',a=>a.noticeParts.body='bad\0body',a=>a.notice+=' mismatch',a=>a.noticeParts.extra='unrecognized',a=>a.actions=['attach_again'],a=>delete a.revision,a=>a.revision=0,a=>a.revision='1']){const bad=clone(s);bad.officerAnswer.text='STALE ANSWER MUST NOT RETURN';change(bad.officerAnswer);const rejected=A.workspaceAttachmentsCreateOfficerAnswer(doc,bad);assert.equal(rejected.valid,false);assert(!rejected.root.textContent.includes('STALE ANSWER MUST NOT RETURN'));assert(!rejected.root.textContent.includes(parts.body));assert.equal(cls(rejected.root,'workspace-attachments-citation').length,0)}
  }
  const s=snapshot('a08-doc');s.officerAnswer.noticeParts={title:'Bad field',body:'Wrong state'};assert.equal(A.workspaceAttachmentsValidateAnswer(s).valid,false);assert(!A.workspaceAttachmentsCreateMessages(doc,s).root.textContent.includes('Used in this answer'));
  const literal=snapshot('a08-forward');literal.officerAnswer.noticeParts={title:'<b>literal title</b>',body:'<script>literal body</script>'};literal.officerAnswer.notice=literal.officerAnswer.noticeParts.title+' '+literal.officerAnswer.noticeParts.body;const view=A.workspaceAttachmentsCreateOfficerAnswer(doc,literal);assert(view.root.textContent.includes(literal.officerAnswer.notice));assert(!all(view.root).some(n=>n.tagName==='SCRIPT'));inert(view);
 }noticeParts();groups++;
 const mutants=[
  ['Motet answered omission',"if(!(message.destination.kind==='officer'&&message.state==='answered'))","if(true)",officerMetadata],
  ['owned metadata anchor','view.scrollAnchor=status||meta','view.scrollAnchor=status',officerMetadata],
  ['notice exact join',"||answer.notice!==parts.title+' '+parts.body",'',noticeParts],
  ['exact Used row','if(usedFileIds.includes(file.id)&&usedMessage?.messageId===message.id&&usedMessage.requestId===message.requestId&&workspaceAttachmentsSameDestination(usedMessage.destination,message.destination))', 'if(usedFileIds.includes(file.id))',sharedFileRows],
  ['matched-message membership',[['const file=files.find(file=>file.id===citation.fileId);','const file=snapshot.files.find(file=>file.id===citation.fileId);'],['||!workspaceAttachmentsP4CitationCurrent(snapshot,nav.citationTarget)','']],null,crossMessage],
  ['reply sample','reply.sample!==true||','',replyGuards],['reply author','reply.author.agentId!==message.destination.agentId||','',replyGuards],
  ['historical model','answer.model.id!==message.destination.modelId||','',A=>rejected(s=>s.officerAnswer.model.id='wrong',A)],
  ['answer sample','answer.sample!==true||','',A=>rejected(s=>s.officerAnswer.sample=false,A)],
  ['used source','||file.usedInAnswer!==true','',A=>rejected(s=>s.files[0].usedInAnswer=false,A)],
  ['complete manifest','{valid:problems.length===0,problems,messages:problems.length?[]:messages}','{valid:true,problems,messages}',A=>rejected(s=>s.files.push(clone(s.files[0])),A)],
  ['activity navigation','||citation.navigation.reason!==reason','',A=>rejected(s=>s.officerAnswer.citations[2].navigation.reason='native',A)],
  ['uploaded citation control',"citation.authority?workspaceAttachmentsButton(doc,css,citation.label):workspaceAttachmentsElement(doc,'span',css)","workspaceAttachmentsElement(doc,'span',css)",validAnswer]
 ];for(const [label,from,to,check] of mutants){const edits=Array.isArray(from)?from:[[from,to]];let changed=source;for(const [target,replacement] of edits){assert(changed.includes(target),label+' target exists');changed=changed.replace(target,replacement)}assert.throws(()=>check(api(changed)),assert.AssertionError,'survived '+label)}
 const css=html.split('/* Workspace attachments phase 3 presentation styles */')[1].split('/* end Workspace attachments phase 3 presentation styles */')[0];assert(css.trim().split('\n').every(line=>line.startsWith('html.workspace.workspace-attachments #officer ')),'styles remain explicitly scoped');
 function citationInk(code){const rule=code.split('.workspace-attachments-citation{')[1].split('}')[0];assert(rule.includes('color:var(--ws-ink)'),'passive citations retain full ink')}citationInk(css);assert.throws(()=>citationInk(css.replace('border-radius:8px;color:var(--ws-ink)','border-radius:8px;color:var(--ws-mute)')),assert.AssertionError);
 console.log('ok phase3 presentation: '+groups+' groups, '+mutants.length+' causal mutants; no browser geometry claimed');
})().catch(error=>{console.error(error);process.exitCode=1});
