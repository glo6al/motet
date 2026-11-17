'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').resolve(__dirname,'../src/web/index.html'),'utf8');
const source=html.split('/* Workspace attachments presentation */')[1].split('/* end Workspace attachments presentation */')[0];
const lifecycle=html.split('/* Workspace attachments lifecycle presentation */')[1].split('/* end Workspace attachments lifecycle presentation */')[0];
const names=['Normalize','FileState','Bytes','Type','CreateControls','CreatePicker','CreateTray','CreateDetails','CreateDrop'];
function api(code=source) {
  // No live globals. An accidental network/storage/timer/DOM access fails immediately.
  const forbidden=()=>{throw Error('Presentation crossed its pure boundary');};
  const context={fetch:forbidden,setTimeout:forbidden,setInterval:forbidden,document:new Proxy({},{get:forbidden}),window:new Proxy({},{get:forbidden}),localStorage:new Proxy({},{get:forbidden}),sessionStorage:new Proxy({},{get:forbidden})};
  return vm.runInNewContext(code+'\n'+lifecycle+';({'+names.map(n=>'workspaceAttachments'+n).join(',')+'})',context);
}
class Node {
  constructor(tag='',text=''){this.tagName=tag.toUpperCase();this.childNodes=[];this.attributes={};this.style={};this.className='';this._text=text;this.disabled=false;}
  append(...nodes){for(const n of nodes){assert(n instanceof Node);assert.equal(n.parentNode,undefined,'factories return fresh detached nodes');n.parentNode=this;this.childNodes.push(n);}}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  set textContent(value){this.childNodes=[];this._text=String(value);}
  get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('');}
  set innerHTML(_){throw Error('Unsafe HTML insertion');}
  addEventListener(){throw Error('Factory installed an event handler');}
  focus(){throw Error('Factory moved focus');}
}
const doc={createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createTextNode:text=>new Node('',text)};
const destination=()=>({kind:'agent',agentId:'agent-a',label:'Ada',projectId:'fictional-project',projectLabel:'Orchard',scope:{kind:'agent',agentId:'agent-a'},modelId:null});
const file=(id,extra={})=>{const value={id,name:'file-'+id+'.pdf',mediaType:'application/pdf',bytes:1887437,preparation:'ready',preparationKind:'authored_sample',previewAvailable:false,preparationText:null,uploadPercent:null,error:null,officerDelivery:null,officerRefusal:null,availability:'private',availableTo:null,usedInAnswer:false,previewable:true,actions:['preview','remove_from_draft'],...extra};if(!Object.hasOwn(extra,'preparationText')&&value.preparation==='ready'&&value.availability==='private'&&value.error===null)value.preparationText='Ready to share. It stays private until you press Send.';return value;};
function snapshot(files=[],extra={}) {
  const d=destination();
  return {schemaVersion:1,fixture:true,enabled:true,destination:d,capabilities:{available:true,reason:null,maxFiles:7,maxFileBytes:9000000,maxDraftBytes:19000000,storageLimitBytes:5000000000,analysisOnly:false,maxTextChars:2000,sample:true,formats:['application/pdf','image/png','image/jpeg','text/plain','text/markdown'],preparationModes:{'application/pdf':'authored_sample','image/png':'authored_sample','image/jpeg':'authored_sample','text/plain':'authored_sample','text/markdown':'authored_sample'}},draft:{id:'draft-a',revision:1,writerGeneration:1,destination:structuredClone(d),state:'editing',text:'An editable question',suggestedText:false,fileIds:files.map(f=>f.id),editable:true,canSend:true,sendBlockedReason:null},files,filesCount:0,storage:{usedBytes:1200000000,limitBytes:5000000000,full:false},ui:{surface:'desk',pickerOpen:false,dropActive:false,detailsFileId:null},...extra};
}
function descend(node){return node?[node,...node.childNodes.flatMap(descend)]:[];}
function hasClass(node,name){return node.className.split(' ').includes(name);}
function cls(node,name){return descend(node).filter(n=>hasClass(n,name));}
function verifyDetached(result){for(const key of ['root','caption','target'])if(result[key])assert.equal(result[key].parentNode,undefined);}
const checks={
  inert(A){
    for(const s of [null,{},snapshot([],{enabled:false}),snapshot([],{fixture:false}),snapshot([],{schemaVersion:2})]) {
      const controls=A.workspaceAttachmentsCreateControls(doc,s),tray=A.workspaceAttachmentsCreateTray(doc,s),drop=A.workspaceAttachmentsCreateDrop(doc,s);
      assert.equal(controls.root,null);assert.equal(tray.root,null);assert.equal(drop.caption,null);
    }
  },
  orderedCompleteManifest(A){
    const s=snapshot([file('one'),file('two'),file('three')]);s.draft.fileIds=['three','one','two'];
    const frozen=JSON.stringify(s),v=A.workspaceAttachmentsCreateTray(doc,s);
    assert.deepEqual(Array.from(v.chips,c=>c.fileId),['three','one','two']);assert.equal(v.manifest.totalBytes,5662311);assert.equal(v.allReady,true);assert.equal(v.sendEnabled,true);
    assert.equal(JSON.stringify(s),frozen,'factory must not mutate canonical snapshot');verifyDetached(v);
  },
  badReferences(A){
    const bad=[s=>s.draft.fileIds.push('missing'),s=>s.draft.fileIds.push('one'),s=>s.files.push({...s.files[0]}),s=>s.draft.destination.agentId='other',s=>s.ui.detailsFileId='not-selected',s=>s.draft.fileIds.push(null),s=>s.files[0].bytes=-1,s=>s.files[0].name=null,s=>s.files=null,s=>s.draft.fileIds=null];
    for(const mutate of bad){const s=snapshot([file('one'),file('two')]);mutate(s);const v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.manifest.valid,false);assert.equal(v.allReady,false);assert.equal(v.sendEnabled,false);assert.equal(v.chips.length,0,'no subset is presented as the complete ready manifest');assert.match(v.block.textContent,/Send is off.*all selected files can be verified/);}
  },
  emptyControlsAndRoom(A){
    const s=snapshot([]),c=A.workspaceAttachmentsCreateControls(doc,s);verifyDetached(c);assert.equal(c.plus.type,'button');assert.equal(c.files.type,'button');assert.equal(c.plus.disabled,false);assert.equal(c.files.disabled,true);assert.equal(c.plus.getAttribute('aria-label'),'Attach files for Ada');assert.equal(A.workspaceAttachmentsCreateTray(doc,s).root,null);
    s.filesCount=12;assert.equal(A.workspaceAttachmentsCreateControls(doc,s).files.textContent,'Files12');s.filesCount=0;assert.equal(A.workspaceAttachmentsCreateControls(doc,s).files.textContent,'Files');
    s.destination.kind='officer';assert.equal(A.workspaceAttachmentsCreateControls(doc,s).plus.getAttribute('aria-label'),'Attach files for Motet');
    const room=snapshot([file('one')]);room.ui.surface='room';room.ui.dropActive=true;room.ui.pickerOpen=true;
    const r=A.workspaceAttachmentsCreateControls(doc,room);assert.equal(r.plus.disabled,true);assert.equal(r.files.disabled,true);assert.equal(r.plus.getAttribute('aria-expanded'),'false');assert.equal(A.workspaceAttachmentsCreateTray(doc,room).root,null);assert.equal(A.workspaceAttachmentsCreateDrop(doc,room).caption,null);assert.equal(A.workspaceAttachmentsNormalize(room).sendEnabled,false);
  },
  canonicalPicker(A){
    const s=snapshot([file('selected')]);s.ui.pickerOpen=true;s.pickerFiles=[file('picker-plan',{name:'delivery-plan.pdf'}),file('picker-shot',{name:'checkout-screenshot.png',mediaType:'image/png',bytes:655360}),file('picker-req',{name:'requirements.md',mediaType:'text/markdown',bytes:4096})];
    const before=JSON.stringify(s),v=A.workspaceAttachmentsCreatePicker(doc,s);verifyDetached(v);assert.equal(v.close,null);assert.deepEqual(Array.from(v.choices,c=>c.fileId),['picker-plan','picker-shot','picker-req']);assert.equal(v.root.childNodes[0].textContent,'The paperclip opens this computer’s file picker, or the photo and file picker on a phone. Shown here as a list of sample files.');
    assert.equal(v.choices[0].button.textContent,'delivery-plan.pdf1.8 MB');assert.equal(v.choices[1].button.textContent,'checkout-screenshot.png640 KB');assert.equal(v.choices[2].button.textContent,'requirements.md4 KB');
    for(const c of v.choices){assert.equal(c.button.type,'button');assert.equal(c.button.disabled,false);assert.equal(descend(c.button).slice(1).some(n=>n.tagName==='BUTTON'||n.tagName==='INPUT'),false);}
    assert.equal(JSON.stringify(s),before);assert.equal(descend(v.root).some(n=>n.tagName==='INPUT'||n.tagName==='TEXTAREA'),false);
    s.pickerFiles[0].name='<img src=x onerror=run()>';s.pickerFiles[0].id='id-with-"[]<> &';const safe=A.workspaceAttachmentsCreatePicker(doc,s);assert.equal(safe.choices[0].fileId,'id-with-"[]<> &');assert.equal(safe.choices[0].button.childNodes[1].textContent,'<img src=x onerror=run()>');assert.equal(descend(safe.root).some(n=>n.tagName==='IMG'),false);
    s.draft.editable=false;assert.equal(A.workspaceAttachmentsCreatePicker(doc,s).choices[0].button.disabled,true);s.draft.editable=true;
    for(const change of [s=>s.enabled=false,s=>s.ui.pickerOpen=false,s=>s.ui.surface='room',s=>s.pickerFiles=null,s=>s.pickerFiles.push({...s.pickerFiles[0]}),s=>s.pickerFiles[0].bytes=-1]){const changed=structuredClone(s);change(changed);assert.equal(A.workspaceAttachmentsCreatePicker(doc,changed).root,null);}
  },
  oneFiveAndLongName(A){
    const long='fictional-delivery-plan-2026-q4-revision-3-reviewed-by-finance.pdf';
    const s=snapshot([file('one',{name:long})]);s.draft.suggestedText=true;s.ui.detailsFileId='one';
    const v=A.workspaceAttachmentsCreateTray(doc,s),c=v.chips[0];assert.equal(v.chips.length,1);assert.match(v.root.textContent,/1 of 7 files · 1.8 MB/);
    assert.equal(cls(c.root,'workspace-attachments-name-tail')[0].textContent,long.slice(-10));assert.equal(cls(c.root,'workspace-attachments-name-head')[0].textContent,long.slice(0,-10));
    assert.equal(cls(c.root,'workspace-attachments-state')[0].textContent,'1.8 MB');assert.match(c.details.getAttribute('aria-label'),/Ready to share/);assert.match(c.details.getAttribute('aria-label'),new RegExp(long.replace(/\./g,'\\.')));assert.match(c.popover.textContent,/Ready to share\. It stays private until you press Send\./);assert.equal(c.preview.disabled,true);assert.equal(c.detailsRemove.textContent,'Remove from draft');
    assert.equal(v.suggestion.textContent,'Suggested message. Edit it or write your own. Nothing is sent until you press Send.');assert.equal(descend(v.root).some(n=>n.tagName==='TEXTAREA'||n.tagName==='INPUT'),false);
    for(const button of descend(v.root).filter(n=>n.tagName==='BUTTON')){assert.equal(button.type,'button');assert.equal(descend(button).slice(1).some(n=>n.tagName==='BUTTON'),false,'never nest buttons');}
    const five=snapshot(['a','b','c','d','e'].map(id=>file(id,{bytes:1024})));assert.equal(A.workspaceAttachmentsCreateTray(doc,five).chips.length,5);assert.match(A.workspaceAttachmentsCreateTray(doc,five).root.textContent,/5 of 7 files · 5 KB/);
  },
  uploadAndPreparation(A){
    const s=snapshot([file('upload',{preparation:'uploading',uploadPercent:62,actions:['cancel_upload']}),file('prep',{preparation:'preparing',actions:['remove_from_draft']}),file('done',{bytes:4000})]);
    const v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.allReady,false);assert.equal(v.sendEnabled,false);assert.match(v.block.textContent,/Send is off while 2 files are still being prepared\./);
    assert.equal(cls(v.chips[0].root,'workspace-attachments-state')[0].textContent,'Uploading 62%');assert.equal(cls(v.chips[1].root,'workspace-attachments-state')[0].textContent,'Preparing');assert.equal(v.chips[0].remove.getAttribute('aria-label'),'Cancel upload of file-upload.pdf');
    const progress=cls(v.chips[0].root,'workspace-attachments-progress')[0];assert.equal(progress.getAttribute('aria-valuenow'),'62');assert.equal(progress.childNodes[0].style.width,'62%');
    assert.equal(cls(v.chips[1].root,'workspace-attachments-progress')[0].getAttribute('aria-valuenow'),null);
    for(const percent of [-1,101,null,NaN])assert.equal(A.workspaceAttachmentsFileState(file('up',{preparation:'uploading',uploadPercent:percent})).label,'Uploading');
  },
  readinessAndCapabilities(A){
    const s=snapshot([file('one')]);
    for(const patch of [{preparation:'unknown'},{availability:'available'},{error:{code:'x',message:'Ready to share'}}]) {const changed=structuredClone(s);Object.assign(changed.files[0],patch);const v=A.workspaceAttachmentsCreateTray(doc,changed);assert.equal(v.allReady,false);assert.equal(v.sendEnabled,false);assert.match(v.chips[0].details.getAttribute('aria-label'),/Not ready/);}
    for(const mutate of [s=>s.capabilities.available=false,s=>s.capabilities.maxFiles=0,s=>s.capabilities.maxFileBytes=100,s=>s.capabilities.maxDraftBytes=100,s=>s.draft.canSend=false]) {const changed=structuredClone(s);mutate(changed);assert.equal(A.workspaceAttachmentsCreateTray(doc,changed).sendEnabled,false);}
    const unknown=file('bad',{preparation:'failed',error:{code:'new-error',message:'a safe reason'}});assert.equal(A.workspaceAttachmentsFileState(unknown).label,'Not ready: a safe reason');
    assert.equal(A.workspaceAttachmentsFileState(file('bad',{preparation:'failed',error:{code:'locked_pdf',message:'WRONG'}})).label,'Locked PDF');
    assert.equal(A.workspaceAttachmentsFileState(file('bad',{preparation:'failed',error:{code:'officer_budget',message:'WRONG'}})).label,'Too large for Motet');
  },
  currentDraftAndFormatGates(A){
    for(const files of [[],[file('pdf')]]) {
      for(const mutate of [s=>s.capabilities.available=false,s=>s.draft.editable=false,s=>s.draft.state='conflict',s=>s.draft.state='saved',s=>s.draft.state='unknown']) {
        const s=snapshot(files);mutate(s);const v=A.workspaceAttachmentsNormalize(s);assert.equal(v.allReady,false);assert.equal(v.sendEnabled,false,'current capability and editable editing draft are required even for empty sends');
        if(files.length)assert.ok(A.workspaceAttachmentsCreateTray(doc,s).block);
      }
    }
    const s=snapshot([file('pdf')]);s.capabilities.formats=['text/plain'];s.capabilities.preparationModes={'text/plain':'authored_sample'};s.ui.detailsFileId='pdf';const v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.allReady,false);assert.equal(v.sendEnabled,false);assert.equal(cls(v.chips[0].root,'workspace-attachments-state')[0].textContent,'Type not supported');assert.doesNotMatch(v.chips[0].details.getAttribute('aria-label'),/Ready to share/);assert.doesNotMatch(v.chips[0].popover.textContent,/Ready to share/);assert.equal(v.chips[0].preview,null);
    s.capabilities.formats=null;assert.equal(A.workspaceAttachmentsNormalize(s).sendEnabled,false);
    s.capabilities.formats=['application/pdf'];s.capabilities.preparationModes={'application/pdf':'authored_sample'};assert.equal(A.workspaceAttachmentsNormalize(s).sendEnabled,true);
  },
  safeLiteralText(A){
    const literal='<img src=x onerror="run()"> & <script>run()</script>',s=snapshot([file('one',{name:literal,mediaType:literal})]);s.destination.label=literal;s.draft.destination.label=literal;s.ui.detailsFileId='one';
    const v=A.workspaceAttachmentsCreateTray(doc,s);assert.match(v.root.textContent,/<img src=x/);assert.equal(v.chips[0].popover.childNodes[0].textContent,literal);assert.equal(descend(v.root).some(n=>['IMG','SCRIPT'].includes(n.tagName)),false);assert.ok(v.chips[0].details.getAttribute('aria-label').startsWith(literal));
    const unknown=file('err',{preparation:'failed',error:{code:'unknown',message:literal}});assert.equal(A.workspaceAttachmentsCreateDetails(doc,unknown).root.childNodes[1].textContent,'PDF · 1.8 MB · Not ready: '+literal);
  },
  actionCapabilities(A){
    for(const actions of [[],['delete_local_copies'],null]){const s=snapshot([file('one',{actions})]);s.ui.detailsFileId='one';const c=A.workspaceAttachmentsCreateTray(doc,s).chips[0];assert.equal(c.remove,null);assert.equal(c.preview,null);assert.equal(c.detailsRemove,null);}
    const s=snapshot([file('one')]);s.draft.editable=false;s.ui.detailsFileId='one';const c=A.workspaceAttachmentsCreateTray(doc,s).chips[0];assert.equal(c.remove,null);assert.equal(c.detailsRemove,null);assert.equal(c.preview.disabled,true);
    s.files[0].previewable=false;assert.equal(A.workspaceAttachmentsCreateTray(doc,s).chips[0].preview,null);
  },
  dropAndBytes(A){
    const s=snapshot([]);s.ui.dropActive=true;const v=A.workspaceAttachmentsCreateDrop(doc,s);verifyDetached(v);assert.equal(v.caption.textContent,'Drop to attach for AdaPDF, PNG, JPEG, text or Markdown. Up to 7 files. Nothing is shared until you press Send.');assert.equal(v.target.getAttribute('aria-hidden'),'true');
    s.capabilities.maxFiles=3;s.capabilities.formats=['text/plain'];s.capabilities.preparationModes={'text/plain':'authored_sample'};assert.match(A.workspaceAttachmentsCreateDrop(doc,s).caption.textContent,/text\. Up to 3 files\./);s.ui.dropActive=false;assert.equal(A.workspaceAttachmentsCreateDrop(doc,s).caption,null);
    const before=JSON.stringify(s);assert.ok(A.workspaceAttachmentsCreateDrop(doc,s,{active:true}).caption);assert.equal(JSON.stringify(s),before,'ephemeral drag cue never mutates canonical snapshot');s.capabilities.available=false;assert.equal(A.workspaceAttachmentsCreateDrop(doc,s,{active:true}).caption,null);
    assert.equal(A.workspaceAttachmentsBytes(0),'0 B');assert.equal(A.workspaceAttachmentsBytes(655360),'640 KB');assert.equal(A.workspaceAttachmentsBytes(1887437),'1.8 MB');assert.equal(A.workspaceAttachmentsBytes(-1),'Size unavailable');assert.equal(A.workspaceAttachmentsBytes(10485760),'10.0 MB');assert.equal(A.workspaceAttachmentsBytes(1536),'2 KB');
  }
};
for(const [name,check] of Object.entries(checks)){check(api());console.log('PASS '+name);}
const mutants=[
 ['selected order','orderedCompleteManifest',"for(const id of Array.isArray(draft?.fileIds)?draft.fileIds:[])","for(const id of Array.isArray(draft?.fileIds)?[...draft.fileIds].reverse():[])"],
 ['subset masquerade','badReferences','const valid=problems.length===0;','const valid=true;'],
 ['unknown state ready','readinessAndCapabilities',"file.preparation==='ready'&&file.availability==='private'&&file.error===null",'true'],
 ['room tray guard','emptyControlsAndRoom','if(!manifest.active||(manifest.isRoom&&!manifest.roomAllowed)||(!manifest.fileIds.length&&manifest.valid))','if(!manifest.active||(!manifest.fileIds.length&&manifest.valid))'],
 ['full accessible name','oneFiveAndLongName',"name+', '+workspaceAttachmentsBytes(file.bytes)","'shortened'+', '+workspaceAttachmentsBytes(file.bytes)"],
 ['Files authority required','emptyControlsAndRoom',"files.disabled=!(typeof workspaceAttachmentsManagement==='function'&&workspaceAttachmentsManagement(snapshot)&&snapshot.management.context.actions.includes('open_files'));",'files.disabled=false;'],
 ['action permission','actionCapabilities',"Array.isArray(file.actions)&&file.actions.includes(token)",'true'],
 ['upload progress','uploadAndPreparation',"String(state.percent));","String(99));"],
 ['fixed sample cap','dropAndBytes',"'Up to '+cap+' files. '","'Up to 5 files. '"],
 ['unsafe text','safeLiteralText','node.textContent=text;','node.innerHTML=text;']
 ,['canonical picker source','canonicalPicker','const choices=snapshot.pickerFiles;','const choices=snapshot.files;']
 ,['room picker guard','canonicalPicker','if(!manifest.active||(manifest.isRoom&&!manifest.roomInteractive)||snapshot.ui?.pickerOpen!==true)','if(!manifest.active||snapshot.ui?.pickerOpen!==true)']
 ,['1024 byte convention','dropAndBytes',"unit==='KB'?1024:1048576","unit==='KB'?1000:1000000"]
 ,['editable draft gate','currentDraftAndFormatGates',"const editable=draft?.editable===true&&draft?.state==='editing';","const editable=draft?.state==='editing';"]
 ,['editing state gate','currentDraftAndFormatGates',"const editable=draft?.editable===true&&draft?.state==='editing';","const editable=draft?.editable===true;"]
 ,['empty capability gate','currentDraftAndFormatGates','sendEnabled:valid&&(!isRoom||roomAllowed)&&cap?.available===true&&editable','sendEnabled:valid&&(!isRoom||roomAllowed)&&editable']
 ,['supported chip state','currentDraftAndFormatGates',"if(capabilities!==null&&(!Array.isArray(capabilities.formats)||!capabilities.formats.includes(file.mediaType)))",'if(false)']
 ,['ephemeral drag cue','dropAndBytes','snapshot.ui?.dropActive!==true&&options.active!==true','snapshot.ui?.dropActive!==true']
];
for(const [name,test,from,to] of mutants){assert(source.includes(from),'mutation target exists: '+name);const mutated=source.replace(from,to);assert.throws(()=>checks[test](api(mutated)),'causal mutant survived: '+name);console.log('KILLED '+name);}
const css=html.split('/* Workspace attachments presentation styles */')[1].split('/* end Workspace attachments presentation styles */')[0];
for(const line of css.split('\n').filter(line=>line.includes('{')))for(const selector of line.split('{')[0].split(','))assert(selector.trim().startsWith('html.workspace.workspace-attachments '),'unscoped feature CSS: '+selector);
assert(!/addEventListener|\.focus\(|fetch\(|setTimeout|setInterval|localStorage|sessionStorage|innerHTML/.test(source),'pure presentation source boundary');
// The populated room tray and input row are siblings; their declared width must share the same
// established expression. This checks source alignment only; browser measurements verify geometry.
function roomWidthAlignment(styles) {
  const row=styles.match(/html\.workspace body:not\(\.stepped\) #officer:has\(#workspaceOfficerHeader\) #ofRow\{([^}]+)\}/)?.[1];
  const tray=styles.match(/html\.workspace\.workspace-attachments body:not\(\.stepped\):not\(\.workspace-officer-desk\) #officer:not\(\.docked\) #ofBody>\.workspace-attachments-tray\{([^}]+)\}/)?.[1];
  const width=rule=>rule?.split(';').map(value=>value.trim()).find(value=>value.startsWith('width:'));
  assert(width(row),'existing room input row declares its width');
  assert.equal(width(tray),width(row),'populated room tray spans the existing input-row width');
}
roomWidthAlignment(html);
const roomRule=html.match(/html\.workspace\.workspace-attachments body:not\(\.stepped\):not\(\.workspace-officer-desk\) #officer:not\(\.docked\) #ofBody>\.workspace-attachments-tray\{[^}]+\}/)?.[0];
assert(roomRule);const shortTray=roomRule.replace(/width:calc\([^;]+\);/,'width:100%;');assert.notEqual(shortTray,roomRule);
assert.throws(()=>roomWidthAlignment(html.replace(roomRule,shortTray)),assert.AssertionError,'body-width tray regression must fail');
console.log('PASS room tray/input-row CSS width alignment and causal short-tray mutation; browser geometry unmeasured');

console.log('PASS '+Object.keys(checks).length+' behavior groups, '+mutants.length+' causal mutants, explicit feature CSS scope; DOM harness has no layout');

// Exercise the canonical repository fixture, without a second scenario catalog.
(async()=>{
  const path=require('node:path'),{pathToFileURL}=require('node:url');
  const fixturePath=path.resolve(__dirname,'../docs/fixtures/attachments/fixture.mjs');
  const {createAttachmentsFixture,ATTACHMENT_SCENARIOS}=await import(pathToFileURL(fixturePath).href);
  const fixture=createAttachmentsFixture({enabled:true,search:'?demo'}),A=api();
  for(const scenario of ['a01-agent','a01-picker','a01-officer','a01-room','a02-drop','a02-preparing','a03-one','a03-five','a03-details']){
    const s=fixture.selectScenario(scenario),before=JSON.stringify(s),m=A.workspaceAttachmentsNormalize(s),controls=A.workspaceAttachmentsCreateControls(doc,s),tray=A.workspaceAttachmentsCreateTray(doc,s),picker=A.workspaceAttachmentsCreatePicker(doc,s);
    assert.equal(m.valid,true,scenario);assert.ok(controls.root);assert.equal(controls.files.disabled,!s.management.context.actions.includes('open_files'));assert.equal(controls.plus.getAttribute('aria-haspopup'),'dialog');assert.equal(controls.plus.childNodes[0].getAttribute('width'),'18');assert.equal(JSON.stringify(s),before);
    if(s.ui.surface==='room'){assert.equal(controls.plus.disabled,false);assert.equal(tray.root,null);assert.equal(picker.root,null);}
    else assert.equal(controls.plus.disabled,false);
    if(s.ui.pickerOpen){assert.deepEqual(Array.from(picker.choices,c=>c.fileId),s.pickerFiles.map(f=>f.id));assert.deepEqual(Array.from(picker.choices,c=>c.button.childNodes[2].textContent),['1.8 MB','640 KB','4 KB']);}
    if(scenario==='a02-preparing'){assert.equal(tray.sendEnabled,false);assert.deepEqual(Array.from(tray.chips,c=>cls(c.root,'workspace-attachments-state')[0].textContent),['Uploading 62%','Preparing','4 KB']);}
    if(scenario==='a03-one'){assert.equal(tray.sendEnabled,true);assert.match(tray.root.textContent,/1 of 5 files · 1.8 MB/);}
    if(scenario==='a03-five'){assert.equal(tray.chips.length,5);assert.equal(tray.manifest.totalBytes,5076787);assert.match(tray.root.textContent,/5 of 5 files · 4.8 MB/);assert.equal(tray.sendEnabled,true);}
    if(scenario==='a03-details'){const c=tray.chips.find(c=>c.fileId===s.ui.detailsFileId);assert.ok(c.popover);assert.match(c.popover.textContent,/2.4 MB · Ready to share/);assert.equal(c.preview.disabled,false);assert(s.management.sourceRefs.find(r=>r.fileId===c.fileId).authority.actions.includes('open_preview'));}
  }
  console.log('PASS canonical fixture: all 9 phase-1 scenarios, accepted byte labels, exact picker/order, aggregate actual bytes');
})().catch(error=>{console.error(error);process.exitCode=1;});
