'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.resolve(__dirname,'../src/web/index.html'),'utf8');
const source=html.split('/* Workspace attachments presentation */')[1].split('/* end Workspace attachments presentation */')[0];
const lifecycle=html.split('/* Workspace attachments lifecycle presentation */')[1].split('/* end Workspace attachments lifecycle presentation */')[0];
function api(code=source){
  const forbidden=()=>{throw Error('Presentation crossed its pure boundary');};
  return vm.runInNewContext(code+'\n'+lifecycle+';({workspaceAttachmentsCreateTray,workspaceAttachmentsCreateIssues,workspaceAttachmentsCreateRecipientNote,workspaceAttachmentsErrorDescription,workspaceAttachmentsFileState})',{fetch:forbidden,setTimeout:forbidden,setInterval:forbidden,document:new Proxy({},{get:forbidden}),window:new Proxy({},{get:forbidden}),localStorage:new Proxy({},{get:forbidden}),sessionStorage:new Proxy({},{get:forbidden})});
}
class Node{
  constructor(tag='',text=''){this.tagName=tag.toUpperCase();this.childNodes=[];this.attributes={};this.style={};this.className='';this._text=text;this.disabled=false;}
  append(...nodes){for(const n of nodes){assert(n instanceof Node);assert.equal(n.parentNode,undefined);n.parentNode=this;this.childNodes.push(n);}}
  setAttribute(k,v){this.attributes[k]=String(v);}getAttribute(k){return this.attributes[k]??null;}
  set textContent(v){this.childNodes=[];this._text=String(v);}get textContent(){return this._text+this.childNodes.map(n=>n.textContent).join('');}
  set innerHTML(_){throw Error('Unsafe HTML insertion');}addEventListener(){throw Error('Factory installed handler');}focus(){throw Error('Factory moved focus');}
}
const doc={createElement:t=>new Node(t),createElementNS:(_,t)=>new Node(t),createTextNode:t=>new Node('',t)};
const descend=n=>n?[n,...n.childNodes.flatMap(descend)]:[];
const cls=(node,name)=>descend(node).filter(n=>n.className.split(' ').includes(name));
(async()=>{
  const {createAttachmentsFixture}=await import(require('node:url').pathToFileURL(path.resolve(__dirname,'../docs/fixtures/attachments/fixture.mjs')).href);
  const snapshot=scenario=>createAttachmentsFixture({enabled:true,search:'?demo',scenario}).snapshot();
  let count=0;
  function canonical(A){
    for(const scenario of ['a04-mixed','a04-budget','a04-folder','a04-quota']){
      const s=snapshot(scenario),before=JSON.stringify(s),v=A.workspaceAttachmentsCreateTray(doc,s,{scopeLabel:'Ada'});
      const failed=s.files.filter(f=>s.draft.fileIds.includes(f.id)&&f.preparation==='failed');
      assert.equal(v.issues.parentNode,v.root);assert.equal(v.issues.getAttribute('role'),'status');
      assert.equal(cls(v.root,'workspace-attachments-issue').length,failed.length,scenario+' has exactly one issue per failed file');
      assert.equal(v.chips.length,s.draft.fileIds.length);assert.equal(v.sendEnabled,false);assert.equal(v.allReady,false);
      for(const f of failed){
        const row=cls(v.root,'workspace-attachments-issue').find(row=>row.textContent.includes(f.name));assert.ok(row);assert.equal(row.childNodes[0].getAttribute('width'),'14');
        const copy=cls(row,'workspace-attachments-issue-copy')[0];assert.equal(copy.childNodes[0].textContent,f.name);
        // The picture's punctuated brand wins only for this known error. All other canonical copy matches.
        assert.equal(copy.textContent,f.error.message.replace('Motet cannot','Motet cannot'));
        assert.deepEqual(Array.from(v.issueActions.filter(a=>a.fileId===f.id),a=>a.token),f.error.actions);
      }
      assert.equal(JSON.stringify(s),before);assert.equal(v.root.parentNode,undefined);
      for(const a of v.issueActions){assert.equal(a.button.type,'button');assert.equal(a.button.disabled,false);if(a.token==='manage_files')assert.deepEqual(JSON.parse(JSON.stringify(a.authority)),s.management.context);assert.equal(a.button.getAttribute('title'),null);assert.equal(a.button.onclick,undefined);}
      const block=scenario==='a04-folder'?'Send is off until these files can be prepared for Ada. Your message and files are kept.':failed.length===1?'Send is off until this file is fixed or removed. To send the text alone, remove it first.':'Send is off until these files are fixed or removed. To send the text alone, remove them first.';
      assert.equal(v.block.textContent,block);
      if(scenario==='a04-budget')assert.match(v.root.textContent,/For Motet · about Ada/);
      if(scenario==='a04-quota')assert.match(v.root.textContent,/orchard-delivery-plan-2030/);
    }
  }
  function permissions(A){
    const s=snapshot('a04-folder');s.files[0].actions=['retry_preparation','remove_from_draft'];s.files[0].error.actions=[];
    let v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.issueActions.filter(a=>a.fileId===s.files[0].id).length,0);assert.equal(v.chips[0].remove,null);
    s.files[0].error.actions=['retry_preparation','retry_preparation','manage_files','delete_local_copies','remove_from_draft'];
    v=A.workspaceAttachmentsCreateTray(doc,s);assert.deepEqual(Array.from(v.issueActions.filter(a=>a.fileId===s.files[0].id),a=>a.token),['retry_preparation','manage_files','remove_from_draft']);assert.equal(v.issueActions.find(a=>a.token==='manage_files').button.disabled,true,'duplicate malformed File actions cannot promote a management capability');const noAuthority=snapshot('a04-quota');noAuthority.management=null;assert.equal(A.workspaceAttachmentsCreateTray(doc,noAuthority).issueActions.find(a=>a.token==='manage_files').button.disabled,true);
    for(const state of ['editing','conflict']){s.draft.editable=false;s.draft.state=state;v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.issueActions.length,0);assert.ok(v.chips.every(c=>c.remove===null));}
    const malformed=snapshot('a04-folder');malformed.files[0].error.actions='retry_preparation';malformed.files[1].error.actions=null;assert.equal(A.workspaceAttachmentsCreateTray(doc,malformed).issueActions.length,0);
  }
  function unknownAndSafe(A){
    const s=snapshot('a04-mixed'),f=s.files[1],literal='<img src=x onerror="run()"> & <script>run()</script>';
    for(const code of ['future_code','toString','__proto__',undefined]){
      f.name='<b>literal-file.pdf</b>';f.error={code,message:literal,actions:['remove_from_draft']};
      const v=A.workspaceAttachmentsCreateTray(doc,s);assert.equal(v.sendEnabled,false);assert.equal(A.workspaceAttachmentsFileState(f).label,'Not ready: '+literal);
      assert.equal(cls(v.issues,'workspace-attachments-issue-copy')[0].textContent,literal);assert.equal(descend(v.root).some(n=>['IMG','SCRIPT','B'].includes(n.tagName)&&n.textContent==='run()'),false);assert.ok(v.chips[1].details.getAttribute('aria-label').includes(f.name));
    }
    delete f.error.message;assert.equal(A.workspaceAttachmentsErrorDescription(f,s.capabilities).text,'This file is not ready.');
    f.error={code:'folder_unreachable',message:'ignored',actions:[]};s.destination.label=literal;s.draft.destination.label=literal;
    const v=A.workspaceAttachmentsCreateTray(doc,s);assert.ok(v.issues.textContent.includes(literal+'’s project folder'));assert.equal(descend(v.root).some(n=>['IMG','SCRIPT'].includes(n.tagName)),false);
  }
  function limits(A){
    const s=snapshot('a04-mixed'),f=s.files[1];f.error={code:'over_size',message:'ignored',actions:['remove_from_draft']};
    s.capabilities.maxFileBytes=3*1048576;s.capabilities.sample=true;
    assert.equal(A.workspaceAttachmentsErrorDescription(f,s.capabilities).text,'is larger than the limit for one file (3 MB in this sample). Attach a smaller file, or remove it.');
    s.capabilities.maxFileBytes=512*1024;s.capabilities.sample=false;assert.match(A.workspaceAttachmentsErrorDescription(f,s.capabilities).text,/\(512 KB\)/);
    f.error.code='unsupported_type';f.mediaType='application/octet-stream';s.capabilities.formats=['text/plain'];assert.equal(A.workspaceAttachmentsErrorDescription(f,s.capabilities).text,'is a file type this recipient does not support. Attach a text file, or remove it.');
    const bad=snapshot('a04-folder');bad.draft.fileIds.push('missing');const v=A.workspaceAttachmentsCreateTray(doc,bad);assert.equal(v.chips.length,0);assert.equal(v.issues,null);assert.equal(v.sendEnabled,false);assert.match(v.block.textContent,/all selected files can be verified/);
    const falseReady=snapshot('a04-mixed');falseReady.files[1].preparation='ready';assert.equal(A.workspaceAttachmentsCreateTray(doc,falseReady).sendEnabled,false);
    const structured=snapshot('a04-folder');structured.draft.blockCode='folder_unreachable';structured.draft.sendBlockedReason='A fallback string is not the machine state.';assert.equal(A.workspaceAttachmentsCreateTray(doc,structured).block.textContent,'Send is off until these files can be prepared for Ada. Your message and files are kept.');
  }
  function conflictAndRecipient(A){
    const s=snapshot('a05-conflict'),before=JSON.stringify(s),v=A.workspaceAttachmentsCreateTray(doc,s);
    assert.match(v.root.textContent,/Read only in this window/);assert.equal(v.block.textContent,s.draft.sendBlockedReason);assert.doesNotMatch(v.block.textContent,/Send is off/);assert.ok(v.chips.every(c=>c.root.className.includes('workspace-attachments-readonly')&&c.remove===null));assert.equal(v.issueActions.length,0);assert.equal(JSON.stringify(s),before);
    const u=snapshot('a04-recipient'),n=A.workspaceAttachmentsCreateRecipientNote(doc,u);assert.equal(n.root.textContent,u.capabilities.reason);assert.equal(n.root.getAttribute('role'),'status');assert.equal(n.root.parentNode,undefined);assert.equal(A.workspaceAttachmentsCreateTray(doc,u).root,null);
    u.capabilities.reason='<svg onload=run()> & literal';assert.equal(A.workspaceAttachmentsCreateRecipientNote(doc,u).root.textContent,u.capabilities.reason);assert.equal(descend(A.workspaceAttachmentsCreateRecipientNote(doc,u).root).length,1);
    u.capabilities.reason=null;assert.equal(A.workspaceAttachmentsCreateRecipientNote(doc,u).root,null);u.enabled=false;assert.equal(A.workspaceAttachmentsCreateRecipientNote(doc,u).root,null);
    const officer=snapshot('a04-budget');assert.match(A.workspaceAttachmentsCreateTray(doc,officer).root.textContent,/For MotetPrivate/);assert.match(A.workspaceAttachmentsCreateTray(doc,officer,{scopeLabel:'Ada'}).root.textContent,/For Motet · about AdaPrivate/);
  }
  const groups={canonical,permissions,unknownAndSafe,limits,conflictAndRecipient};
  for(const [name,check] of Object.entries(groups)){check(api());count++;console.log('PASS '+name);}
  const mutants=[
    ['issues omitted','canonical','result.issues=issues.root;result.issueActions=issues.actions;if(issues.root)root.append(issues.root);','result.issues=null;result.issueActions=[];'],
    ['file actions authorize issues','permissions','Array.isArray(file.error?.actions)?file.error.actions:[]','Array.isArray(file.actions)?file.actions:[]'],
    ['manage authority required','permissions',"if(token==='manage_files')button.disabled=!authority?.actions.includes('open_storage');","if(token==='manage_files')button.disabled=false;"],
    ['unknown code invented','unknownAndSafe',"return {known:false,text:workspaceAttachmentsText(file?.error?.message,'This file is not ready.')};", "return {known:true,text:'is locked.'};"],
    ['known code prototype lookup','unknownAndSafe','Object.hasOwn(errors,file.error?.code)?errors[file.error.code]:null','errors[file.error?.code]'],
    ['unsafe issue text','unknownAndSafe','else copy.textContent=description.text;','else copy.innerHTML=description.text;'],
    ['hardcoded size limit','limits','workspaceAttachmentsBytes(capabilities?.maxFileBytes)',"'10 MB'"],
    ['folder generic block','canonical',"if(snapshot.draft?.blockCode==='folder_unreachable'||folderOnly)",'if(false)'],
    ['conflict prefix invented','conflictAndRecipient','if(plainBlock)result.block.textContent=blocked;','if(false)result.block.textContent=blocked;'],
    ['recipient reason guessed','conflictAndRecipient',"'workspace-attachments-recipient-note',reason", "'workspace-attachments-recipient-note','Files unavailable'"],
  ];
  for(const [name,test,from,to] of mutants){assert.ok(source.includes(from),'mutation target exists: '+name);assert.throws(()=>groups[test](api(source.replace(from,to))),'causal mutant survived: '+name);console.log('KILLED '+name);}
  assert(!/addEventListener|\.focus\(|fetch\(|setTimeout|setInterval|localStorage|sessionStorage|innerHTML|\.on\w+\s*=/.test(source));
  console.log('PASS '+count+' phase-2 behavior groups, '+mutants.length+' causal mutants; canonical shared fixture, detached DOM harness has no layout');
})().catch(error=>{console.error(error);process.exitCode=1;});
