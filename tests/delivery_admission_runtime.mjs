// Production official plugin + real Motet HTTP; only the native host is fake.
import assert from 'node:assert/strict';
import {register} from '../integrations/claude-mod/hooks/register.js';
let input='';for await(const part of process.stdin)input+=part;
const config=JSON.parse(input),url='http://127.0.0.1:'+config.port,storage=new Map();
const clone=value=>JSON.parse(JSON.stringify(value)),hooks=new Map(),timers=[],submissions=[];
const pause=()=>new Promise(resolve=>setTimeout(resolve,10));
async function owner(path,body){
 const response=await fetch(url+path,{method:'POST',headers:{'Content-Type':'application/json','X-Live-Room-Key':config.owner_key},body:JSON.stringify(body)});
 return {status:response.status,value:await response.json()};
}
let api,roster=['desktop'];
const $={session:{id:async()=>config.native,cwd:async()=>config.project,surfaces:async()=>clone(roster)},
 store:{keys:async()=>[...storage.keys()],get:async key=>storage.has(key)?clone(storage.get(key)):undefined,set:async(key,value)=>storage.set(key,clone(value))},
 command:{register:async()=>{}},clock:{now:async()=>Date.now(),every:(delay,fn)=>{const timer={fn,cancelled:false,cancel(){this.cancelled=true}};timers.push(timer);return timer}},
 http:{fetch:async(address,init)=>{const response=await fetch(address,init);return {ok:response.ok,status:response.status,headers:{},text:await response.text()}}},
 prompt:{submit:async value=>{
  assert.equal(value.asUser,false);
  assert.deepEqual(roster,[],'delivery uses the enrolled background runtime');
  const match=/^WATCH_REQUEST ([0-9a-f-]+)\n/.exec(value.text);assert(match);
  assert(!submissions.includes(match[1]),'same request reached native prompt.submit twice');
  submissions.push(match[1]);
  const turn='fictional-turn-'+submissions.length;
  await api.call('turn.start',{text:'The watch-owner plugin sent a message:\n'+value.text+"\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.",turnId:turn});
  await api.call('turn.complete',{turnId:turn,answer:'WATCH_REPLY '+match[1]+'\nFictional Atlas reply.',isAborted:false,reason:'answer'});
  return {text:value.text};
 }}};
register((name,handler)=>{hooks.set(name,handler);return {catch(){}}});
api={call:async(name,value)=>hooks.get(name)?.($,value,async()=>({})),tick:async()=>{for(const timer of timers.filter(t=>!t.cancelled))await timer.fn()}};
async function until(check){
 for(let n=0;n<800;n++){await api.tick();if(await check())return;await pause()}
 throw Error('Fictional native delivery did not settle.');
}
await api.call('session.start',{cwd:config.project,surface:null,isInteractive:false});
const setup=await owner('/claude-mod-setup',{});assert.equal(setup.status,200);
await api.call('command.run',{command:'watch-setup',args:config.port+':'+setup.value.pair_code,origin:{kind:'composer'}});
await until(async()=>JSON.parse((await api.call('command.run',{command:'watch-status',args:'',origin:{kind:'composer'}})).text).connected);
roster=[];
for(let n=0;n<3;n++)await api.tick();
// Browser checks may keep the production plugin online while Puppeteer drives
// the actual page. Fixture stdin stays private; this runner makes no owner send.
if(config.serve){
 console.log(JSON.stringify({ready:true}));
 while(true){await api.tick();await pause()}
}
const agent='c:'+config.native, directId=crypto.randomUUID(), askId=crypto.randomUUID();
const direct={agent,question:'Report fictional direct progress.',request_id:directId};
const ask={turns:[{role:'user',content:'Ask Atlas: Report fictional relay progress.'}],request_id:askId};
const directResult=await owner('/agent-message',direct);assert.equal(directResult.status,200,JSON.stringify(directResult));
const askResult=await owner('/ask',ask);assert.equal(askResult.status,200);assert(!askResult.value.error,JSON.stringify(askResult));assert.equal(askResult.value.messages[0].agent,agent);
await until(async()=>{
 const response=await fetch(url+'/agent-messages',{headers:{'X-Live-Room-Key':config.owner_key}}),value=await response.json();
 return value.requests.length===2&&value.requests.every(row=>row.state==='answered'&&row.reply==='Fictional Atlas reply.');
});
assert.equal(submissions.length,2);
assert.equal((await owner('/agent-message',direct)).status,200);
const replay=await owner('/ask',ask);assert.equal(replay.status,200);assert(!replay.value.error,JSON.stringify(replay));
for(let n=0;n<5;n++)await api.tick();
assert.equal(submissions.length,2,'same-ID HTTP retries must not submit again');
const before=(await owner('/fixture/state',{})).value;
assert.equal(before.model_calls,1,'same-ID relay retry must not reinterpret intent');
assert.deepEqual(before.stale_decisions,[askId],'real decide_ask must invalidate the previously current directory');
assert.equal(before.bound_runtime,true);
assert.equal(before.request_insertions.filter(id=>id===directId).length,1);
assert.equal(before.request_insertions.filter(id=>id===askId).length,1);
await owner('/fixture/fault',{refuse:true});
const refusedId=crypto.randomUUID();
const refused=await owner('/agent-message',{agent,question:'Report fictional refused progress.',request_id:refusedId});
assert.equal(refused.status,503);assert.equal(refused.value.delivery,'not_submitted');
assert(!/capacity|attachment_index|fictional_preflight/.test(refused.value.error),'internal preflight cause stays private');
await owner('/fixture/fault',{refuse:false});
// A real intent UPDATE stales an explicitly current directory before admission;
// keep the fault below that UPDATE so it specifically refuses capacity preflight.
const refusedAskId=crypto.randomUUID();
await owner('/fixture/fault',{refuse:true});
const refusedAsk=await owner('/ask',{turns:[{role:'user',content:'Ask Atlas: Report fictional refused relay progress.'}],request_id:refusedAskId});
assert.equal(refusedAsk.status,200);assert.equal(refusedAsk.value.delivery,'not_submitted');
assert.equal(refusedAsk.value.agent,agent);assert.match(refusedAsk.value.error,/Atlas/);
assert(!/capacity|attachment_index|fictional_preflight/.test(refusedAsk.value.error),'relay reports recipient and plain-language refusal');
for(let n=0;n<5;n++)await api.tick();
const afterRefusal=(await owner('/fixture/state',{})).value;
assert.equal(afterRefusal.requests.length,2);assert.equal(submissions.length,2);
assert(!afterRefusal.request_insertions.includes(refusedId));assert(!afterRefusal.request_insertions.includes(refusedAskId));
await owner('/fixture/fault',{refuse:false});
await owner('/fixture/fault',{lost_commit:true});
const uncertainId=crypto.randomUUID();
const uncertainAsk={turns:[{role:'user',content:'Ask Atlas: Report fictional uncertain progress.'}],request_id:uncertainId};
const uncertain=await owner('/ask',uncertainAsk);assert.equal(uncertain.status,200);
assert.match(uncertain.value.error,/could not be confirmed/);
assert(!('delivery' in uncertain.value),'post-insertion commit uncertainty is not a definite refusal');
const committed=(await owner('/fixture/state',{})).value;
assert.equal(committed.requests.filter(row=>row.request_id===uncertainId).length,1,'lost commit response retains the durable request');
await until(async()=>{
 const state=(await owner('/fixture/state',{})).value;
 return state.requests.length===3&&state.requests.every(row=>row.state==='answered');
});
assert.equal(submissions.filter(id=>id===uncertainId).length,1);
const uncertainReplay=await owner('/ask',uncertainAsk);assert(!uncertainReplay.value.error,JSON.stringify(uncertainReplay));
for(let n=0;n<5;n++)await api.tick();
const final=(await owner('/fixture/state',{})).value;
assert.equal(final.model_calls,3,'lost-response replay must retain the original intent decision');
assert.equal(final.request_insertions.filter(id=>id===uncertainId).length,1);
assert.equal(submissions.length,3,'lost-response retry must not send twice');
await owner('/claude-mod-revoke',{});
console.log(JSON.stringify({native_submissions:submissions,model_calls:final.model_calls,
 request_insertions:final.request_insertions,stale_decisions:final.stale_decisions,
 answered:final.requests.map(row=>({request_id:row.request_id,agent:row.agent,state:row.state,reply:row.reply})),
 direct_refusal:refused,relay_refusal:refusedAsk,post_commit_uncertainty:uncertain}));
