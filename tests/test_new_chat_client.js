#!/usr/bin/env node
'use strict';
/* The actual page client, with an in-memory server boundary. No native app or server is launched. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const source=html.slice(html.indexOf('function createNewChatClient('),html.indexOf('/* end New chat HTTP client */'));
const create=vm.runInNewContext(source+';createNewChatClient');
const requestId='11111111-1111-4111-8111-111111111111',attemptId='22222222-2222-4222-8222-222222222222';
const choiceEpoch='44444444-4444-4444-8444-444444444444';
const folder={id:'opaque-folder',revision:'r7',label:'Orchard',displayPath:'/fictional/projects/orchard'};
const context=()=>({schemaVersion:1,fixture:false,folders:[folder],host:{id:'opaque-host',label:'This computer'},providers:[{id:'codex',available:true,supportsWork:true,supportsBlank:true},{id:'claude_code',available:false,unavailableReason:{message:'Claude active handoff is unavailable.'}}],limits:{observationWindowMs:120000}});
const receipt=(extra={})=>({schemaVersion:1,fixture:false,requestId,attemptId,provider:'codex',mode:'work',folder,state:'waiting',creation:{outcome:'verified'},uiOpen:{attempts:1,outcome:'rejected'},execution:{attempts:1,outcome:'running'},observation:'active',capabilities:{keepWaiting:false,stopWaiting:true},reason:{message:'The native session exists, but its window did not open.'},...extra});
const intent=(extra={})=>({schemaVersion:1,requestId,provider:'codex',mode:'work',folder,...extra});
const tick=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
function harness(options={}){
  let key='fictional-owner',requests=[],reports=[],jobs=[],clears=0,aborts=0;const store=options.store||new Map();
  const storage={getItem:k=>store.get(k)||null,setItem(k,v){if(options.storageFails)throw Error('disabled');store.set(k,v)},removeItem:k=>store.delete(k)};
  const run=options.route||((route,body)=>route==='context'?context():route==='intents'?intent({mode:body.mode}):route==='attempts'?receipt({mode:body.mode}):route.startsWith('requests/')?{schemaVersion:1,requestId,status:'attempted',attempt:receipt()}:receipt());
  const client=(options.create||create)({controller:()=>{const c=new AbortController();c.signal.addEventListener('abort',()=>aborts++);return c},auth:()=>key,storage,scope:async k=>'digest-'+k,setTimeout:(f,ms)=>{jobs.push({f,ms});return jobs.length},clearTimeout:()=>clears++,authChanged:()=>reports.push({authChanged:true}),report:(id,r)=>reports.push({id,...r}),changed(){},fetch:async(url,opts)=>{
    const route=url.replace('/api/new-chat/',''),body=opts.body&&JSON.parse(opts.body);requests.push({route,body,opts});
    if(route==='attempts')assert.equal(store.get('watch-new-chat:digest-'+key),requestId,'durable opaque request ID precedes dispatch');
    const value=await run(route,body,requests);
    if(value?.jsonHang)return {ok:true,status:200,json:()=>new Promise(()=>{})};
    if(value?.httpError)return {ok:false,status:value.httpError,json:async()=>({schemaVersion:1,error:{message:value.message,code:value.code}})};
    return {ok:true,status:200,json:async()=>value};
  }});
  return {client,requests,reports,store,jobs,setKey:k=>key=k,boot:async()=>{client.sync();await tick()},get clears(){return clears},get aborts(){return aborts}};
}
const work={request_id:'page-action',project:'Orchard',folder:folder.displayPath,app:'codex',mode:'work',instruction:'Fictional instruction: list three apples.'};
async function checkProductionTimerWiring(){
  const prefix='else WNC.adapter=createNewChatClient(';
  const wiring=html.split('\n').find(line=>line.trimStart().startsWith(prefix));
  assert.ok(wiring,'the live page installs the New chat client');
  const scheduled=[],cleared=[],requests=[];
  const window={
    setTimeout(_,ms){assert.equal(this,window,'Window timer receiver');scheduled.push(ms);return 17;},
    clearTimeout(id){assert.equal(this,window,'Window timer receiver');cleared.push(id);},
  };
  const page={
    window, setTimeout:window.setTimeout, clearTimeout:window.clearTimeout,
    WNC:{}, KEY:'fictional-owner', locked:false, claiming:false, TextEncoder, AbortController,
    sessionStorage:{getItem:()=>null,removeItem(){}},
    crypto:{subtle:{digest:async()=>new ArrayBuffer(32)}},
    async fetch(url,options){requests.push([url,options.method]);return {ok:true,json:async()=>context()};},
    workspaceNewChatReport(){},workspaceNewChatPaint(){},workspaceNewChatSettingsPaint(){},workspaceNewChatTimer(){},workspaceNewChatAddFolderClose(){},workspaceNewChatDialogClose(){},
  };
  vm.runInNewContext(source+'\n'+wiring.trimStart().slice('else '.length),page);
  const {adapter}=page.WNC;
  adapter.sync();await tick();
  assert.equal(adapter.apps.codex.status,'ready');
  assert.deepEqual(scheduled,[15000]);
  assert.deepEqual(cleared,[17]);
  page.locked=true;
  adapter.sync();
  assert.equal(adapter.apps.codex.status,'unavailable');
  assert.deepEqual(cleared,[17,0]);
  assert.deepEqual(requests,[['/api/new-chat/context','GET']]);
}
async function folderChecks(factory=create){
  const registered={...folder,registered:true};
  const inventory=()=>({...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch,folders:[registered]});
  let fail=false,malformed=false,revision='r7';
  let h=harness({create:factory,route:route=>{assert.equal(route,'context');if(fail)throw Error('Fictional unavailable inventory');const c=inventory();c.folders=[{...registered,revision}];if(malformed)c.providers=[null];return c}});
  await h.boot();assert.equal(h.client.canChooseFolders(),true);assert.equal(h.client.registeredFolders().length,1);assert.equal(h.client.folderRevision(folder.displayPath),'r7');
  fail=true;assert.equal(await h.client.retryContext(),false);assert.equal(h.client.inventoryFresh,false);
  assert.equal(h.client.hasFolder(folder.displayPath),true,'failed refresh retains the owner-visible cached path');assert.equal(h.client.registeredFolders().length,1,'failed refresh retains registered sidebar data');
  await assert.rejects(h.client.launch(work),/unavailable/);assert.equal(h.requests.filter(r=>r.body).length,0,'stale display is not launch authority');
  fail=false;malformed=true;assert.equal(await h.client.retryContext(),false);assert.equal(h.client.folderRevision(folder.displayPath),'r7','malformed read cannot overwrite cached display');
  malformed=false;revision='r8';assert.equal(await h.client.retryContext(),true);assert.equal(h.client.inventoryFresh,true);assert.equal(h.client.folderRevision(folder.displayPath),'r8');
  await assert.rejects(h.client.launch({...work,folderRevision:'r7'}),/unavailable/);assert.equal(h.requests.filter(r=>r.body).length,0,'stale explicit revision never creates an intent');
  h.setKey('');h.client.sync();assert.equal(h.client.registeredFolders().length,0,'owner reset clears cached folders');assert.equal(h.client.canChooseFolders(),false);
}
async function knownHostReasonCheck(factory=create){
 let failure={httpError:503,code:'unavailable',message:'New chat is unavailable on this host.'};const h=harness({create:factory,route:()=>failure});await h.boot();assert.equal(h.client.inventoryReason,'New chat is unavailable on this host.');assert.equal(h.client.apps.codex.reason,h.client.inventoryReason);assert.equal(h.client.inventoryFresh,false);
 for(const next of [{httpError:503,code:'other',message:'New chat is unavailable on this host.'},{httpError:503,code:'unavailable',message:'Fictional internal cause.'},{httpError:400,code:'unavailable',message:'New chat is unavailable on this host.'}]){failure=next;await h.client.retryContext();assert.equal(h.client.inventoryReason,'Motet could not load the list of apps and folders. Try again.','only the known structured host failure is retained')}
 assert.equal(h.requests.filter(r=>r.body).length,0);
}
async function descriptiveProviderCheck(factory=create){
  let failed=false;const h=harness({create:factory,route:()=>{if(failed)throw Error('Fictional unavailable inventory');const c=context();c.providers[1]={id:'claude_code',available:true,supportsWork:true,supportsBlank:false,ownership:'watch_managed'};c.limits.managedPermissionTimeoutSeconds=120;return c}});
  await h.boot();failed=true;await h.client.retryContext();const provider=h.client.apps.claude;
  assert.equal(provider.ownership,'watch_managed','failed refresh retains descriptive ownership for the visible first-message draft');assert.equal(provider.supportsWork,true);assert.equal(provider.provider,'claude_code');assert.equal(provider.status,'unavailable');assert.equal(h.client.inventoryFresh,false);
  await assert.rejects(h.client.launch({...work,app:'claude'}),/unavailable/);assert.equal(h.requests.filter(r=>r.body).length,0,'cached provider descriptions never restore launch authority');
}
async function serverFailureReceiptCheck(factory=create){
  const h=harness({create:factory,route:route=>route==='context'?{...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch}:{httpError:503,message:'Fictional saved receipt unavailable.'}});
  await h.boot();await assert.rejects(h.client.acceptFolderChoice(requestId,attemptId,choiceEpoch),error=>error.status===503,'a failed accept receipt stays an error for read-only recovery by the page');
  assert.equal(h.requests.filter(r=>r.route.endsWith('/accept')).length,1);assert.equal(h.requests.filter(r=>r.route==='context').length,1,'no automatic save or read is invented by the failed client response');
}
async function registrationChecks(){
  const inventory=()=>({...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch});let h;
  const recovery=new Map([['watch-new-chat:digest-fictional-owner',requestId]]);h=harness({store:recovery,route:route=>route.startsWith('requests/')?{schemaVersion:1,requestId,status:'prepared',mode:'work',provider:'codex',folder}:Promise.reject(Error('inventory unavailable'))});await h.boot();assert.equal(h.requests.filter(r=>r.route.startsWith('requests/')).length,1,'pending launch recovery does not require fresh inventory');await h.client.retryContext();await h.client.retryContext();assert.equal(h.requests.filter(r=>r.route.startsWith('requests/')).length,1,'refresh never repeats pending launch recovery');assert.equal(h.requests.filter(r=>r.body).length,0);
  let release;let reads=0;h=harness({route:route=>{assert.equal(route,'context');return ++reads===1?inventory():new Promise(resolve=>release=resolve)}});await h.boot();const a=h.client.retryContext(),b=h.client.retryContext();assert.equal(a,b,'concurrent refresh shares a promise');await tick();assert.equal(reads,2);release(inventory());assert.equal(await a,true);
  const baseline=h.requests.length;h.client.projectsChanged('initial');h.client.projectsChanged('new-root');h.client.projectsChanged('newer-root');assert.equal(h.jobs.filter(j=>j.ms===500).length,1,'observed project changes use one bounded refresh');const refreshJob=h.jobs.find(j=>j.ms===500);const running=refreshJob.f();await tick();release(inventory());await running;assert.equal(h.requests.length,baseline+1);assert.equal(h.requests.filter(r=>r.body).length,0);
  console.log('ok New chat refresh/recovery: one-time recovery, cached metadata freshness and bounded project refresh');
}
async function chooserChecks(factory=create){
  const token='33333333-3333-4333-8333-333333333333',saved={...folder,registered:true},data={requestId,epoch:choiceEpoch,gesture:{trusted:true}};
  let accepted=false,reads=0;
  const h=harness({create:factory,route:(route,body)=>{
    if(route==='context'){reads++;return {...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch,folders:accepted?[saved]:[]}}
    if(route==='folders/choose'){assert.deepEqual(body,data);return {schemaVersion:1,requestId,state:'pending'}}
    if(route==='folders/choose/'+requestId)return {schemaVersion:1,requestId,state:'selected',selectionToken:token};
    if(route.endsWith('/accept')){assert.deepEqual(body,{selectionToken:token});accepted=true;return {schemaVersion:1,requestId,state:'saved',folder:saved}}
    if(route.endsWith('/cancel'))return {schemaVersion:1,requestId,state:'saved',folder:saved};
    throw Error('Unexpected route');
  }});await h.boot();assert.equal(h.client.canChooseFolders(),true);
  const initialPosts=h.requests.filter(r=>r.body).length;await assert.rejects(h.client.beginFolderChoice({...data,epoch:attemptId}),/context changed/);assert.equal(h.requests.filter(r=>r.body).length,initialPosts,'old coordinator epoch cannot open a chooser');
  assert.equal((await h.client.beginFolderChoice(data)).state,'pending');assert.equal((await h.client.folderChoiceStatus(requestId)).selectionToken,token);assert.equal(accepted,false,'status cannot save a choice');
  const receipt=await h.client.acceptFolderChoice(requestId,token);assert.equal(receipt.folder.availabilityChecked,true);assert.equal(h.client.isRegistered(folder.displayPath),true);assert.equal(reads,2);
  assert.equal((await h.client.cancelFolderChoice(requestId,choiceEpoch)).state,'saved','cancel cannot claim to undo a completed save');assert.equal(h.requests.at(-1).opts.keepalive,true,'page-exit cancellation may finish after the page goes away');assert.deepEqual(h.requests.at(-1).body,{epoch:choiceEpoch});assert(!h.requests.at(-1).route.includes('fictional-owner'));assert.equal(h.requests.filter(r=>r.route.endsWith('/accept')).length,1);assert.equal(h.store.size,0,'chooser IDs and selected paths are not stored in the browser');assert.equal(h.requests.filter(r=>['intents','attempts'].includes(r.route)).length,0);
  await assert.rejects(h.client.folderChoiceStatus('not-an-id'));await assert.rejects(h.client.acceptFolderChoice(requestId,'not-a-token'));
  for(const bad of [{requestId:attemptId,state:'pending'},{requestId,state:'selected',selectionToken:'not-a-token'},{requestId,state:'saved',folder:{...saved,registered:false}},{requestId,state:'invented'}]){
    const b=harness({create:factory,route:route=>route==='context'?{...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch}:{schemaVersion:1,...bad}});await b.boot();await assert.rejects(b.client.beginFolderChoice(data),'a mismatched or malformed chooser receipt is not authority');await assert.rejects(b.client.folderChoiceStatus(requestId));
  }
  let response;const l=harness({create:factory,route:route=>route==='context'?{...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch}:new Promise(resolve=>response=resolve)});await l.boot();const pending=l.client.beginFolderChoice(data);await tick();l.setKey('changed-owner');l.client.sync();response({schemaVersion:1,requestId,state:'selected',selectionToken:token});await assert.rejects(pending,/authentication/);
  let recoveredReads=0;const recovered=harness({create:factory,route:route=>route==='context'?(recoveredReads++,{...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch,folders:recoveredReads===1?[]:[saved]}):{schemaVersion:1,requestId,state:'saved',folder:saved}});await recovered.boot();const recoveredReceipt=await recovered.client.folderChoiceStatus(requestId);assert.equal(recoveredReceipt.folder.availabilityChecked,true,'a saved GET receipt refreshes availability just as the accept reply does');assert.equal(recovered.client.isRegistered(folder.displayPath),true);assert.equal(recovered.requests.filter(r=>r.body).length,0);
  let posts=0;const u=harness({create:factory,route:(route,body)=>{if(route==='context')return {...context(),canChooseFolders:true,folderChoiceEpoch:choiceEpoch};if(body){posts++;throw Error('lost response')}return {schemaVersion:1,requestId,state:'pending'}}});await u.boot();await assert.rejects(u.client.beginFolderChoice(data));await u.client.folderChoiceStatus(requestId);assert.equal(posts,1,'unknown begin is never automatically repeated');
}
(async()=>{
  await checkProductionTimerWiring();
  await chooserChecks();
  assert.equal(typeof harness().client.registerFolder,'undefined','typed registration has no client surface');
  {const marker="return savedChoice(await choiceRequest(id,'',undefined,choiceEpoch),e,k)";assert(source.includes(marker));await assert.rejects(chooserChecks(vm.runInNewContext(source.replace(marker,"return choiceRequest(id,'',undefined)")+';createNewChatClient')),'unrefreshed saved GET receipt mutation must fail')}
  {const marker="value.state==='selected'&&!uuid.test(value.selectionToken||'')";assert(source.includes(marker));await assert.rejects(chooserChecks(vm.runInNewContext(source.replaceAll(marker,'false')+';createNewChatClient')),'unchecked selection token mutation must fail')}
  await folderChecks();await registrationChecks();await serverFailureReceiptCheck();await descriptiveProviderCheck();await knownHostReasonCheck();
  {const from="error.status===503&&error.code==='unavailable'&&error.message==='New chat is unavailable on this host.'";assert(source.includes(from));await assert.rejects(knownHostReasonCheck(vm.runInNewContext(source.replace(from,'false')+';createNewChatClient')),'known host explanation mutation must fail')}
  {const from='...api.apps.claude,';assert(source.includes(from));await assert.rejects(descriptiveProviderCheck(vm.runInNewContext(source.replace(from,'')+';createNewChatClient')),'dropped cached managed-provider description mutation must fail')}

  for(const [from,to] of [[";api.inventoryFresh=false;changed()};",";context=null;api.inventoryFresh=false;changed()};"],["req.folderRevision!==undefined&&req.folderRevision!==f.revision||",""]]){
    assert(source.includes(from),'causal client mutation is present');const mutant=vm.runInNewContext(source.replace(from,to)+';createNewChatClient');await assert.rejects(folderChecks(mutant),'freshness/revision mutation must fail');
  }
  let h=harness();assert.equal(h.client.apps.codex,undefined);await h.boot();assert.equal(h.client.apps.codex.status,'ready');assert.equal(h.client.apps.claude.status,'unavailable');assert.match(h.client.apps.claude.reason,/handoff/);assert.equal(h.client.hasFolder(folder.displayPath),true);assert.equal(h.client.hasFolder(folder.displayPath+'/other'),false);
  await h.client.launch(work);const posts=h.requests.filter(r=>r.body);assert.equal(posts.length,2);assert.deepEqual(posts[0].body,{provider:'codex',folderId:'opaque-folder',folderRevision:'r7',mode:'work'});assert.deepEqual(posts[1].body,{...posts[0].body,requestId,instruction:work.instruction});assert.ok(!JSON.stringify(posts).includes('"folder":"'));assert.equal(posts[1].opts.headers['X-Live-Room-Key'],'fictional-owner');assert.equal(posts[1].opts.credentials,'same-origin');assert.deepEqual([...h.store.values()],[requestId]);assert.ok(!JSON.stringify([...h.store]).includes(work.instruction));let r=h.reports.at(-1);assert.equal(r.execution.outcome,'running');assert.equal(r.uiOpen.outcome,'rejected');assert.equal(r.creation.outcome,'verified');assert.match(r.reason,/session exists/);
  await assert.rejects(h.client.launch(work),/previous/);assert.equal(h.requests.filter(r=>r.route==='attempts').length,1);
  const reloaded=harness({store:h.store});await reloaded.boot();assert.ok(reloaded.requests.some(r=>r.route==='requests/'+requestId));assert.equal(reloaded.requests.filter(r=>r.body).length,0,'reload never repeats dispatch');assert.equal(reloaded.reports.at(-1).execution.outcome,'running');
  h=harness();await h.boot();await h.client.launch({...work,mode:'blank',instruction:'MUST NOT SEND',draft:'An unsent fictional draft'});r=h.requests.find(r=>r.route==='attempts').body;assert.equal(r.mode,'blank');assert.equal(r.draft,'An unsent fictional draft');assert.equal(r.instruction,undefined);
  h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent({mode:body.mode}):route==='attempts'?Promise.reject(Error('lost response')):{schemaVersion:1,requestId,status:'attempted',attempt:receipt()}});await h.boot();await h.client.launch(work);assert.equal(h.requests.filter(r=>r.route==='attempts').length,1);assert.equal(h.requests.at(-1).route,'requests/'+requestId);assert.equal(h.reports.at(-1).creation.outcome,'verified','recovered fact survives lost response');
  h=harness({store:new Map([['watch-new-chat:digest-fictional-owner',requestId]]),route:route=>route==='context'?context():{schemaVersion:1,requestId,status:'prepared',mode:'work',provider:'codex',folder}});await h.boot();assert.match(h.reports.at(-1).reason,/Nothing is resubmitted/);assert.equal(h.requests.filter(r=>r.body).length,0);await assert.rejects(h.client.launch(work),/previous/);
  h=harness({storageFails:true});await h.boot();const storageFailure=await h.client.launch(work);assert.equal(h.requests.filter(r=>r.route==='attempts').length,0);assert.match(storageFailure.reason,/No launch was submitted/);assert.equal(h.client.pending(),false);
  h=harness();await h.boot();await assert.rejects(h.client.launch({...work,folder:'/fictional/arbitrary'}),/unavailable/);await assert.rejects(h.client.launch({...work,instruction:'  '}),/instruction/);assert.equal(h.requests.filter(r=>r.body).length,0);
  h=harness({route:route=>{const c=context();c.providers[0]={id:'codex',available:true};return c}});await h.boot();assert.equal(h.client.apps.codex.status,'unavailable');await assert.rejects(h.client.launch(work),/unavailable/);
  let finish;h=harness({route:(route,body)=>route==='context'?context():route==='intents'?new Promise(r=>finish=r):receipt()});await h.boot();const first=h.client.launch(work);await tick();await assert.rejects(h.client.launch(work),/previous/);h.setKey('different-owner');h.client.sync();finish(intent());await first;await tick();assert.equal(h.requests.filter(r=>r.route==='attempts').length,0,'late prepared intent cannot dispatch under another owner');assert.ok(h.reports.some(r=>r.authChanged));
  h=harness();h.setKey('');await h.boot();assert.equal(h.requests.length,0,'locked owner makes no API requests');
  let continueCount=0;h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent():route.endsWith('/observe')?(continueCount++,{httpError:409,message:'Observation cannot continue.'}):receipt()});await h.boot();await h.client.launch(work);await assert.rejects(h.client.observe('continue'),/cannot continue/);assert.equal(continueCount,1);assert.equal(h.requests.at(-1).body.action,'continue');assert.equal(h.client.pending(),true,'observation rejection cannot silently dismiss the request');
  h=harness();await h.boot();await h.client.launch(work);for(let i=0;i<60;i++){const job=h.jobs.at(-1);assert.equal(job.ms,2000);await job.f();await tick()}const count=h.jobs.filter(j=>j.ms===2000).length;assert.equal(count,60,'polling ends after a bounded active observation window');assert.equal(h.requests.filter(r=>r.route==='attempts').length,1);h.client.dismiss();assert.equal(h.store.size,0);
  // A never-settling HTTP POST is bounded, and recovery remains GET-only.
  h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent():route==='attempts'?new Promise(()=>{}):{schemaVersion:1,requestId,status:'attempted',attempt:receipt()}});await h.boot();const hung=h.client.launch(work);await tick();await h.jobs.findLast(j=>j.ms===15000).f();await hung;assert.equal(h.aborts,1,'timed out fetch is aborted');assert.equal(h.requests.filter(r=>r.route==='attempts').length,1);assert.equal(h.requests.at(-1).route,'requests/'+requestId);assert.equal(h.reports.at(-1).creation.outcome,'verified');h.client.dismiss();assert.equal(h.client.pending(),false,'dismiss clears the dispatch gate after an unknown request');
  h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent():route==='attempts'?{jsonHang:true}:{schemaVersion:1,requestId,status:'attempted',attempt:receipt()}});await h.boot();const hungBody=h.client.launch(work);await tick();await h.jobs.findLast(j=>j.ms===15000).f();await hungBody;assert.equal(h.requests.filter(r=>r.route==='attempts').length,1);assert.equal(h.reports.at(-1).execution.outcome,'running','response body read is bounded and recovered too');
  // Dismissal isolates a late POST from a subsequent explicit request, including its busy gate.
  let latePost,secondPrepare;let prepares=0;h=harness({route:(route,body)=>route==='context'?context():route==='intents'?(++prepares===1?intent():new Promise(r=>secondPrepare=r)):route==='attempts'?new Promise(r=>latePost=r):{schemaVersion:1,requestId,status:'prepared',folder}});await h.boot();const old=h.client.launch(work);await tick();h.client.dismiss();const newer=h.client.launch({...work,request_id:'new-action'});await tick();latePost(receipt());await old;assert.equal(h.client.pending(),true,'old completion cannot clear the newer busy gate');assert.equal(h.reports.length,0,'dismissed late POST cannot paint or recover a newer action');secondPrepare(intent());await tick();h.client.dismiss();await h.jobs.findLast(j=>j.ms===15000).f();await newer;
  // Room association is independent of execution: an added monitor must not freeze an active native turn.
  let statusReads=0;h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent():route==='attempts'?receipt({state:'added',observation:'complete',session:{roomAgentId:'x:fictional'},execution:{outcome:'not_attempted',pending:true}}):receipt({state:'added',observation:'complete',session:{roomAgentId:'x:fictional'},execution:{outcome:++statusReads===1?'running':'completed',pending:false}})});await h.boot();await h.client.launch(work);await h.jobs.at(-1).f();await tick();assert.equal(h.reports.at(-1).execution.outcome,'running');await h.jobs.at(-1).f();await tick();assert.equal(h.reports.at(-1).execution.outcome,'completed');assert.equal(h.jobs.filter(j=>j.ms===2000).length,2,'settled execution ends polling after admission');
  // An older status GET resolving after an explicit observation action cannot regress its newer receipt.
  let resolveOld;h=harness({route:(route,body)=>route==='context'?context():route==='intents'?intent():route==='attempts'?receipt():route.endsWith('/observe')?receipt({state:'timeout',observation:'stopped',execution:{outcome:'completed'}}):new Promise(r=>resolveOld=r)});await h.boot();await h.client.launch(work);const read=h.jobs.at(-1).f();await tick();await h.client.observe('stop');resolveOld(receipt({state:'waiting',execution:{outcome:'running'}}));await read;await tick();assert.equal(h.reports.at(-1).state,'timeout');assert.equal(h.reports.at(-1).execution.outcome,'completed');
  // Presentation wording belongs to page tests; this client protects the unsent blank contract.
  h=harness();await h.boot();await h.client.launch({...work,mode:'blank',instruction:'MUST NOT SEND',draft:''});
  r=h.requests.find(r=>r.route==='attempts').body;assert.equal(r.mode,'blank');assert.equal(r.instruction,undefined);assert.equal(r.draft,undefined,'empty blank chat does not manufacture a first message');
  assert.equal(h.requests.filter(r=>r.route==='attempts').length,1,'blank launch makes one attempt');
  assert.deepEqual([...h.store.values()],[requestId],'only the opaque recovery ID is persisted for a blank chat');
  // Scripted demo blank drafts never become fictional sent messages; work gets one scripted user event.
  const ds=html.slice(html.indexOf('function workspaceNewChatDemoAdapter(){'),html.indexOf('/* Workspace-only per-instance conversation snapshots;'));
  for(const mode of ['blank','work']){const events=[],queued=[];const demo=vm.runInNewContext(ds+';workspaceNewChatDemoAdapter()',{Map,Date,Promise,DSEQ:0,setTimeout:f=>(queued.push(f),1),clearTimeout(){},handle:e=>events.push(e),render(){},workspaceNewChatReport(){}});demo.launch({request_id:'demo-action',app:'codex',folder:folder.displayPath,mode,draft:'UNSENT',instruction:'Scripted work'});queued[0]();assert.equal(events.filter(e=>e.kind==='user').length,mode==='work'?1:0);assert.equal(events[0].goal,mode==='work'?'Scripted work':'');assert.equal(demo.apps.claude.status,'unavailable')}
  assert.ok(html.includes("else if(!Number.isFinite(a.last)||a.last<=0)label='no log entry seen yet'"),'new blank monitor has an honest missing-timestamp state');
  // Managed Claude is enabled only by authoritative capabilities; work targets the provider ID, not the UI alias.
  const runId='33333333-3333-4333-8333-333333333333',permissionId='44444444-4444-4444-8444-444444444444';
  h=harness({route:(route,body)=>{
    if(route==='context'){const c=context();c.limits.managedPermissionTimeoutSeconds=120;c.providers[1]={id:'claude_code',available:true,supportsWork:true,supportsBlank:false,ownership:'watch_managed'};return c}
    if(route==='intents')return intent({provider:'claude_code'});
    if(route.startsWith('/api/managed-claude/'))return body?{schemaVersion:1,permissionId,requestId,decision:body.decision,state:'written',decisionState:'written',resolutionReason:'owner',resolvedAt:1900000030}:{schemaVersion:1,runId,state:'needs_attention',timeoutSeconds:120,serverTime:1900000030,evidenceAvailable:true,permissions:[{id:permissionId,createdAt:1900000000,expiresAt:1900000120,decision:null,resolutionReason:null,decisionState:null,resolvedAt:null,inputAvailable:true,state:'pending',canAllow:true,canDeny:true}]};
    return receipt({provider:'claude_code',ownership:'watch_managed',managed:{runId,state:'needs_attention'},creation:{outcome:'unknown'},uiOpen:{attempts:0,outcome:'not_required'},execution:{outcome:'needs_attention',receiptKind:'native_user_message'}});
  }});await h.boot();assert.equal(h.client.apps.claude.status,'ready');
  await assert.rejects(h.client.launch({...work,app:'claude',mode:'blank',draft:''}),/unavailable/);
  await h.client.launch({...work,app:'claude'});assert.equal(h.requests.find(r=>r.route==='attempts').body.provider,'claude_code');
  assert.equal(h.reports.at(-1).managed.runId,runId);assert.equal(h.reports.at(-1).ownership,'watch_managed');assert.equal(h.reports.at(-1).app,'claude');assert.equal(h.reports.at(-1).session,undefined);
  assert.equal(h.client.managedPermissionTimeoutSeconds,120,'Dialog gets authoritative timeout before permission request');
  const ms=await h.client.managedStatus(runId);assert.equal(h.requests.at(-1).opts.method,'GET');
  assert.equal(ms.serverTime,1900000030);assert.equal(ms.timeoutSeconds,120);assert.equal(ms.evidenceAvailable,true);assert.equal(ms.permissions[0].expiresAt,1900000120);assert.equal(ms.permissions[0].resolvedAt,null);
  const decisionReceipt=await h.client.decidePermission(runId,permissionId,requestId,'deny');assert.deepEqual(h.requests.at(-1).body,{requestId,decision:'deny'});
  assert.equal(decisionReceipt.decisionState,'written');assert.equal(decisionReceipt.resolutionReason,'owner');assert.equal(decisionReceipt.resolvedAt,1900000030);
  assert.equal(h.requests.filter(r=>r.route==='attempts').length,1,'status and permission actions do not relaunch');
  h.setKey('');h.client.sync();assert.equal(h.client.managedPermissionTimeoutSeconds,null);await assert.rejects(h.client.managedStatus(runId),/authentication/);
  console.log('ok New chat HTTP client: authoritative inventory, explicit work/blank contracts, authenticated requests, receipt separation, durable request-ID recovery, no mutation replay, key/lock stale guards, observation errors and bounded polling');
})().catch(error=>{console.error(error);process.exitCode=1});
