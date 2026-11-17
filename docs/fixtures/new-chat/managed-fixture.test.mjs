import assert from 'node:assert/strict';
import {MANAGED_SCENARIOS,createManagedClaudeFixture} from './managed-fixture.mjs';
const requestId = n => `66666666-6666-4666-8666-${String(n).padStart(12,'0')}`;
async function opened(scenario) {
 const fixture=createManagedClaudeFixture(scenario),context=await fixture.context(),folder=context.folders[0];
 assert.equal(context.providers[0].supportsBlank,false);
 assert.equal(context.limits.managedPermissionTimeoutSeconds,120);
 const selection={provider:'claude_code',mode:'work',folderId:folder.id,folderRevision:folder.revision};
 const intent=await fixture.prepare(selection),payload={...selection,requestId:intent.requestId,instruction:'Fictional apples.'};
 const attempt=await fixture.open(payload);
 return {fixture,attempt,payload,status:await fixture.managedStatus(attempt.managed.runId)};
}
let cases=0;
for(const scenario of MANAGED_SCENARIOS){
 const {fixture:f,attempt:a,payload,status}=await opened(scenario);
 assert.equal(a.launch.attempts,1);assert.equal(a.uiOpen.outcome,'not_required');
 assert.deepEqual(await f.open(payload),a);await assert.rejects(f.open({...payload,instruction:'Changed'}),/conflict/);
 assert.equal(status.fixture,true);
 assert.equal(status.timeoutSeconds,120);
 assert.equal(status.serverTime,1893499500);
 assert.equal(typeof status.evidenceAvailable,'boolean');
 assert.ok(status.permissions.filter(p=>p.state!=='pending').length<=8);
 assert.ok(status.permissions.filter(p=>p.state==='pending').length<=8);
 for(const p of status.permissions){
  assert.equal(p.expiresAt-p.createdAt,120);
  assert.ok(p.createdAt<=status.serverTime);
  for(const key of ['createdAt','expiresAt','decision','resolutionReason','decisionState','resolvedAt','inputAvailable'])assert.ok(Object.hasOwn(p,key),`${scenario}: ${key}`);
  if(p.canAllow){
   assert.equal(p.state,'pending');assert.equal(p.resolvedAt,null);assert.equal(p.decision,null);assert.equal(p.decisionState,null);assert.equal(p.resolutionReason,null);
   assert.equal(p.canDeny,true);assert.equal(p.inputAvailable,true);assert.ok(p.expiresAt>status.serverTime);
  }
  if(p.state!=='pending'){
   assert.equal(p.canAllow,false);assert.equal(p.canDeny,false);assert.ok(p.resolvedAt>=p.createdAt&&p.resolvedAt<=status.serverTime);
  }
  if(p.resolutionReason&&p.resolutionReason!=='owner')assert.notEqual(p.decision,'allow','No automatic allow');
 }
 // All returned objects are defensive copies.
 if(status.permissions.length){
  const before=await f.managedStatus(status.runId);
  status.permissions[0].input.changed='not retained';status.permissions[0].canAllow=true;
  assert.deepEqual(await f.managedStatus(status.runId),before);
 }
 cases++;
}
for(const [scenario,state,decision,reason,decisionState] of [
 ['owner_allowed','decided','allow','owner','written'],['owner_denied','decided','deny','owner','written'],
 ['deadline_denied','expired','deny','timeout','written'],['withdrawn','cancelled',null,'withdrawn',null],
 ['turn_ended','cancelled',null,'turn_ended',null],['native_exit','cancelled',null,'native_exit',null],
 ['oversized','unsupported','deny','oversized','written'],['unsupported_tool','unsupported','deny','unsupported_tool','written'],
 ['capacity','unsupported','deny','capacity','written'],['inactive_turn','unsupported','deny','inactive_turn','written'],
 ['owner_allow_unknown','decided','allow','owner','unknown'],['owner_deny_unknown','decided','deny','owner','unknown'],
 ['automatic_deny_unknown','expired','deny','timeout','unknown'],
]){
 const {fixture,status}=await opened(scenario),p=status.permissions[0];
 assert.deepEqual([p.state,p.decision,p.resolutionReason,p.decisionState],[state,decision,reason,decisionState]);
 if(['oversized','unsupported_tool','capacity','inactive_turn'].includes(scenario)){assert.equal(p.inputAvailable,false);assert.deepEqual(p.input,{});}
 if(decisionState==='unknown'){assert.equal(status.state,'uncertain');assert.equal(status.capabilities.permissions,false);}
 await assert.rejects(fixture.decidePermission(status.runId,p.id,requestId(1),'allow'));
 cases++;
}
{
 const {fixture,status,attempt}=await opened('permission');
 assert.equal(attempt.session,null,'Early request reachable before room admission');
 const p=status.permissions[0],id=requestId(2);
 const result=await fixture.decidePermission(status.runId,p.id,id,'deny');
 assert.deepEqual(result,{schemaVersion:1,fixture:true,permissionId:p.id,requestId:id,decision:'deny',state:'written',decisionState:'written',resolutionReason:'owner',resolvedAt:status.serverTime});
 assert.deepEqual(await fixture.decidePermission(status.runId,p.id,id,'deny'),result);
 await assert.rejects(fixture.decidePermission(status.runId,p.id,id,'allow'),/conflict/);
 await assert.rejects(fixture.decidePermission(status.runId,p.id,requestId(3),'deny'),/conflict/);
 const now=(await fixture.managedStatus(status.runId)).permissions[0];
 assert.equal(now.decision,'deny');assert.equal(now.canAllow,false);assert.equal(now.decisionState,'written');
 assert.equal(now.createdAt,p.createdAt);assert.equal(now.expiresAt,p.expiresAt,'No restarted deadline');
 const independent=await opened('permission');assert.equal(independent.status.permissions[0].state,'pending');
 cases++;
}
{
 const {fixture,status,attempt}=await opened('permission_multiple');
 assert.ok(attempt.session,'Desk scenario is admitted');assert.equal(status.permissions.length,3);
 assert.deepEqual(status.permissions.map(p=>p.createdAt),[...status.permissions].map(p=>p.createdAt).sort((a,b)=>a-b));
 await fixture.decidePermission(status.runId,status.permissions[0].id,requestId(4),'allow');
 await assert.rejects(fixture.decidePermission(status.runId,status.permissions[1].id,requestId(4),'allow'),/conflict/);
 const next=await fixture.managedStatus(status.runId);
 assert.equal(next.state,'needs_attention');assert.equal(next.permissions.filter(p=>p.canAllow).length,2,'One answer cannot answer another request');
 await assert.rejects(fixture.decidePermission(status.runId,next.permissions[1].id,'not-an-id','deny'));
 cases++;
}
{
 const {status}=await opened('permission_long'),p=status.permissions[0];
 assert.equal(p.inputAvailable,true);assert.equal(p.input.content.split('\n').length,214);
 assert.ok(p.input.content.endsWith('Fictional orchard row 214'));assert.equal(p.canAllow,true);
 cases++;
}
for(const scenario of ['uncertain','uncertain_readable','uncertain_unreadable']){
 const {fixture,status}=await opened(scenario);
 assert.equal(status.state,'uncertain');assert.equal(status.recoveryRequired,true);assert.equal(status.capabilities.permissions,false);
 assert.equal(status.evidenceAvailable,scenario!=='uncertain_unreadable');
 assert.equal(status.permissions.length,scenario==='uncertain_unreadable'?0:1);
 if(status.permissions.length){
  const p=status.permissions[0];assert.equal(p.state,'pending');assert.equal(p.resolvedAt,null);assert.equal(p.decision,null);
  assert.equal(p.canAllow,false);assert.equal(p.canDeny,false);assert.equal(p.inputAvailable,true);
  await assert.rejects(fixture.decidePermission(status.runId,p.id,requestId(5),'deny'));
 }
 cases++;
}
{
 const {fixture,status}=await opened('history');
 assert.equal(status.permissions.filter(p=>p.state!=='pending').length,8);assert.equal(status.permissions.filter(p=>p.state==='pending').length,1);
 assert.ok(status.permissions[0].id.endsWith('000000000003'),'First two historical records omitted');
 const pending=status.permissions.find(p=>p.state==='pending');
 await fixture.decidePermission(status.runId,pending.id,requestId(6),'allow');
 const after=await fixture.managedStatus(status.runId);
 assert.equal(after.permissions.length,8,'Resolving a ninth row must preserve the bounded history');
 assert.equal(after.permissions.filter(p=>p.state==='pending').length,0);
 assert.ok(after.permissions.some(p=>p.id===pending.id&&p.decision==='allow'));
 assert.ok(!after.permissions.some(p=>p.id===status.permissions[0].id),'Oldest terminal row falls out of the next snapshot');
 cases++;
}
console.log(`ok ${cases} fictional managed Claude contract checks`);
