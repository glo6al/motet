// Fictional managed-Claude contract companion. No HTTP, filesystem or native calls.
export const MANAGED_SCENARIOS = Object.freeze([
  'starting', 'working', 'permission', 'failed', 'uncertain',
  'permission_desk', 'permission_multiple', 'permission_long',
  'owner_allowed', 'owner_denied', 'deadline_denied', 'withdrawn', 'turn_ended', 'native_exit',
  'oversized', 'unsupported_tool', 'capacity', 'inactive_turn', 'owner_allow_unknown', 'owner_deny_unknown',
  'automatic_deny_unknown', 'uncertain_readable', 'uncertain_unreadable', 'history',
]);
const ids = Object.freeze({request:'11111111-1111-4111-8111-111111111111',attempt:'22222222-2222-4222-8222-222222222222',run:'33333333-3333-4333-8333-333333333333',session:'55555555-5555-4555-8555-555555555555'});
const folder = Object.freeze({id:'fictional-orchard',revision:'1',label:'Orchard',displayPath:'/fictional/projects/orchard'});
const SERVER_TIME = 1893499500; // 2030-01-01, fixed fixture time, Unix seconds.
const TIMEOUT_SECONDS = 120;
const copy = v => structuredClone(v);
const fail = message => {throw new Error(message)};
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const permissionId = index => `44444444-4444-4444-8444-${String(index + 1).padStart(12, '0')}`;
function pending(index = 0) {
  const createdAt = SERVER_TIME - 60 + index * 5;
  return {id:permissionId(index),tool:'Write',input:{file_path:`/fictional/projects/orchard/notes-${index + 1}.txt`,content:'Fictional orchard notes'},
    inputAvailable:true,state:'pending',canAllow:true,canDeny:true,createdAt,expiresAt:createdAt + TIMEOUT_SECONDS,
    decision:null,resolutionReason:null,decisionState:null,resolvedAt:null};
}
const outcomes = Object.freeze({
  owner_allowed: ['decided','allow','owner','written'],
  owner_denied: ['decided','deny','owner','written'],
  deadline_denied: ['expired','deny','timeout','written'],
  withdrawn: ['cancelled',null,'withdrawn',null],
  turn_ended: ['cancelled',null,'turn_ended',null],
  native_exit: ['cancelled',null,'native_exit',null],
  oversized: ['unsupported','deny','oversized','written'],
  unsupported_tool: ['unsupported','deny','unsupported_tool','written'],
  capacity: ['unsupported','deny','capacity','written'],
  inactive_turn: ['unsupported','deny','inactive_turn','written'],
  owner_allow_unknown: ['decided','allow','owner','unknown'],
  owner_deny_unknown: ['decided','deny','owner','unknown'],
  automatic_deny_unknown: ['expired','deny','timeout','unknown'],
});
function terminal(scenario, index = 0) {
  const [state,decision,resolutionReason,decisionState] = outcomes[scenario];
  const value = {...pending(index),state,decision,resolutionReason,decisionState,canAllow:false,canDeny:false,
    createdAt:SERVER_TIME - 240 + index * 5,resolvedAt:SERVER_TIME - 120 + index * 5};
  value.expiresAt = value.createdAt + TIMEOUT_SECONDS;
  if(['oversized','unsupported_tool','capacity','inactive_turn'].includes(scenario)) {
    // The complete rejected input was not retained; {} must not appear as approval evidence.
    value.input = {};
    value.inputAvailable = false;
  }
  if(scenario === 'unsupported_tool')value.tool = 'FictionalUnsupportedTool';
  return value;
}
export function createManagedClaudeFixture(scenario = 'permission') {
  if(!MANAGED_SCENARIOS.includes(scenario))fail('Unknown fictional managed scenario.');
  let opened=false,selection=null,body=null;
  const receipts = new Map();
  const isUncertain = ['uncertain','uncertain_readable','uncertain_unreadable','owner_allow_unknown','owner_deny_unknown','automatic_deny_unknown'].includes(scenario);
  const evidenceAvailable = scenario !== 'uncertain_unreadable';
  let permissions = [];
  if(['permission','permission_desk','permission_long','uncertain','uncertain_readable'].includes(scenario))permissions = [pending()];
  if(scenario === 'permission_multiple')permissions = [pending(),pending(1),pending(2)];
  if(scenario === 'permission_long')permissions[0].input.content = Array.from({length:214}, (_,i) => `Fictional orchard row ${i + 1}`).join('\n');
  if(outcomes[scenario])permissions = [terminal(scenario)];
  if(scenario === 'history') {
    // Ten historical rows exist in the fictional ledger; only the most recent eight are returned.
    permissions = Array.from({length:10}, (_,i) => terminal(i % 2 ? 'owner_denied' : 'owner_allowed', i)).slice(-8);
    permissions.push(pending(10));
  }
  if(isUncertain)for(const permission of permissions)Object.assign(permission,{canAllow:false,canDeny:false});
  const visiblePermissions = () => {
    const recent = new Set(permissions.filter(p=>p.resolvedAt!==null).sort((a,b)=>a.resolvedAt-b.resolvedAt).slice(-8).map(p=>p.id));
    return permissions.filter(p=>p.resolvedAt===null||recent.has(p.id));
  };
  const choose = value => {
    if(value?.provider!=='claude_code'||value?.mode!=='work'||value?.folderId!==folder.id||value?.folderRevision!==folder.revision)fail('Choose managed Claude work in the fictional Orchard folder.');
  };
  const runState = () => {
    if(isUncertain)return 'uncertain';
    if(scenario === 'starting')return 'starting';
    if(['failed','native_exit'].includes(scenario))return 'exited';
    return permissions.some(p => p.canAllow || p.canDeny) ? 'needs_attention' : 'running';
  };
  const attempt = () => {
    if(!opened)fail('No fictional launch recorded.');
    const verified=scenario!=='starting'&&scenario!=='permission';
    const state=runState();
    return copy({schemaVersion:1,fixture:true,requestId:ids.request,attemptId:ids.attempt,provider:'claude_code',mode:'work',ownership:'watch_managed',folder,
      state:verified?'added':'waiting',observation:verified?'complete':'active',
      launch:{attempts:1,outcome:'accepted'},creation:{outcome:verified?'verified':'unknown'},
      uiOpen:{attempts:0,outcome:'not_required'},draft:{provided:false,disposition:'not_provided'},
      managed:{runId:ids.run,state,recoveryRequired:isUncertain,permissionsAvailable:permissions.some(p=>p.canAllow||p.canDeny)},
      execution:{attempts:scenario==='starting'?0:1,outcome:scenario==='starting'?'not_attempted':state==='needs_attention'?'needs_attention':state==='exited'?'failed':state==='uncertain'?'unknown':'running',pending:scenario==='starting',receiptKind:'native_user_message',turnId:scenario==='starting'?null:ids.request},
      session:verified?{id:ids.session,folderId:folder.id,roomAgentId:'c:'+ids.session}:null,
      existingSession:verified?{id:ids.session,folderId:folder.id}:null,
      capabilities:{openExisting:false,keepWaiting:false,stopWaiting:!verified},reason:null,mismatch:null});
  };
  return Object.freeze({
    context:async()=>copy({schemaVersion:1,fixture:true,folders:[folder],host:{id:'fixture-host',label:'Fictional workstation'},providers:[{id:'claude_code',label:'Claude Code',available:true,ownership:'watch_managed',supportsWork:true,supportsBlank:false,supportsDraft:false}],limits:{observationWindowMs:120000,managedPermissionTimeoutSeconds:TIMEOUT_SECONDS}}),
    prepare:async value=>{choose(value);selection=copy(value);return copy({schemaVersion:1,fixture:true,requestId:ids.request,provider:'claude_code',mode:'work',folder});},
    open:async value=>{choose(value);if(!selection||value.requestId!==ids.request||typeof value.instruction!=='string'||!value.instruction.trim())fail('Prepare first and provide the fictional instruction.');if(opened&&body!==JSON.stringify(value))fail('Request conflict.');body=JSON.stringify(value);opened=true;return attempt();},
    status:async id=>{if(id!==ids.attempt)fail('Unknown attempt.');return attempt();},
    managedStatus:async id=>{if(!opened||id!==ids.run)fail('Unknown managed run.');return copy({schemaVersion:1,fixture:true,runId:ids.run,state:runState(),recoveryRequired:isUncertain,
      timeoutSeconds:TIMEOUT_SECONDS,serverTime:SERVER_TIME,evidenceAvailable,permissions:visiblePermissions(),capabilities:{permissions:!['uncertain','exited'].includes(runState())}});},
    decidePermission:async (run,permissionId,requestId,value)=>{
      if(!opened||run!==ids.run||!uuid(requestId)||!['allow','deny'].includes(value))fail('No matching pending permission.');
      const previous=receipts.get(requestId);
      if(previous){if(previous.permissionId!==permissionId||previous.decision!==value)fail('Decision conflict.');return copy(previous);}
      if([...receipts.values()].some(receipt=>receipt.permissionId===permissionId))fail('Decision conflict.');
      const permission=permissions.find(p=>p.id===permissionId);
      if(!permission||!permission.canAllow||!permission.canDeny||runState()==='uncertain')fail('No matching pending permission.');
      Object.assign(permission,{state:'decided',decision:value,resolutionReason:'owner',decisionState:'written',resolvedAt:SERVER_TIME,canAllow:false,canDeny:false});
      const receipt={schemaVersion:1,fixture:true,permissionId,requestId,decision:value,state:'written',decisionState:'written',resolutionReason:'owner',resolvedAt:SERVER_TIME};
      receipts.set(requestId,receipt);
      return copy(receipt);
    }
  });
}
