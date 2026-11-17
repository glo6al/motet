// Real MCP, signed HTTP, SQLite and plugin; explicit retrieval, no reply notices.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {register} from '../hooks/register.js';
const {Client}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js');
const {StdioClientTransport}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js');
let input='';for await(const part of process.stdin)input+=part;
const config=JSON.parse(input),url='http://127.0.0.1:'+config.port,storage=new Map();
const copy=value=>JSON.parse(JSON.stringify(value));
const pause=()=>new Promise(resolve=>setTimeout(resolve,10));
const client=new Client({name:'fictional reply integration client',version:'1'});
let current=null;

async function rpc(name,args={}) {
 const raw=await client.callTool({name,arguments:args});
 assert(!raw.isError,JSON.stringify(raw));
 return {raw,value:JSON.parse(raw.content[0].text).untrusted_peer_data};
}
async function runtime(native,index) {
 const hooks=new Map(),timers=[],submissions=[],polls=[];
 let api,surfaces=['desktop'];
 const $={
  session:{id:async()=>native,cwd:async()=>config.project,surfaces:async()=>copy(surfaces)},
  store:{keys:async()=>[...storage.keys()],get:async key=>storage.has(key)?copy(storage.get(key)):undefined,
   set:async(key,value)=>storage.set(key,copy(value))},
  command:{register:async()=>{}},
  clock:{now:async()=>Date.now(),every:(_ms,fn)=>{
   const timer={fn,cancelled:false,cancel(){this.cancelled=true}};timers.push(timer);return timer;
  }},
  http:{fetch:async(address,init)=>{
   const response=await fetch(address,init),text=await response.text();
   if(response.ok&&address.endsWith('/poll'))polls.push(JSON.parse(text));
   return {ok:response.ok,status:response.status,headers:{},text};
  }},
  prompt:{submit:async value=>{
   assert.equal(value.asUser,false);
   assert.deepEqual(surfaces,[],'background chats must remain usable without a room tile');
   submissions.push(value.text);
   const turn='fictional-'+crypto.randomUUID();
   assert(!value.text.startsWith('WATCH_PEER_REPLY '),'retired notice must never reach native submit');
   {
    assert.equal(index,0,'only the pinned target receives the question');
    const match=/^WATCH_PEER_REQUEST ([0-9a-f-]+)\n/.exec(value.text);
    assert(match);
    assert(current.source_finished,'target answer is deliberately released after asking turn completion');
    assert.notEqual(match[1],current.public_request_id,'transport and public IDs must stay distinct');
    current.question_submissions++;
    assert.equal(current.question_submissions,1);
    current.events.push('target_answer');
    const wrapped='The watch-owner plugin sent a message:\n'+value.text+
     "\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";
    await api.call('turn.start',{text:wrapped,turnId:turn});
    await api.call('turn.complete',{turnId:turn,
     answer:'WATCH_REPLY '+match[1]+'\nFictional '+current.source_provider+' answer.',isAborted:false,reason:'answer'});
   }
   return {text:value.text};
  }}
 };
 register((name,fn)=>{hooks.set(name,fn);return {catch(){}}});
 api={submissions,polls,background:()=>{surfaces=[]},call:async(name,value)=>hooks.get(name)?.($,value,async()=>({})),
  tick:async()=>{for(const timer of timers.filter(t=>!t.cancelled))await timer.fn()},
  status:async()=>JSON.parse((await api.call('command.run',{command:'watch-status',args:'',origin:{kind:'composer'}})).text)};
 await api.call('session.start',{cwd:config.project,surface:null,isInteractive:false});
 return api;
}
const runtimes=await Promise.all(config.native.map(runtime));
async function until(check) {
 for(let n=0;n<500;n++) {
  await Promise.all(runtimes.map(r=>r.tick()));
  if(await check())return;
  await pause();
 }
 throw Error('Reply integration did not settle: '+JSON.stringify(current));
}
try {
 await client.connect(new StdioClientTransport({command:process.execPath,
  args:[fileURLToPath(new URL('../../claude/peer.mjs',import.meta.url)),'--credential',config.credential]}));
 await runtimes[0].call('command.run',{command:'watch-setup',args:config.port+':'+config.code,origin:{kind:'composer'}});
 await until(async()=>(await Promise.all(runtimes.map(r=>r.status()))).every(s=>s.connected));
 await until(()=>runtimes.every(r=>r.polls.some(p=>p.reply_notice_protocol===1)));
 for(const runtime of runtimes)runtime.background();
 let directory;
 await until(async()=>{
  directory=(await rpc('list_agents',{provider:'claude'})).value;
  return directory.directory.complete&&directory.agents.length===3&&directory.agents.every(a=>a.capability==='reachable');
 });
 const target=directory.agents.find(a=>a.agent_id==='c:'+config.native[0]);assert(target);
 const codexDirectory=(await rpc('list_agents',{provider:'codex'})).value;
 const codexTarget=codexDirectory.agents.find(a=>a.agent_id==='x:'+config.codex_source);assert(codexTarget);
 const cases=[];
 for(const [caseIndex,provider] of ['codex','claude','codex','claude'].entries()) {
  await fs.rm(config.source_ready_file,{force:true});
  await fs.rm(config.codex_notice_file,{force:true});
  const recipient=provider==='codex'?target:codexTarget;
  current={source_provider:provider,reverse_index:Math.floor(caseIndex/2),public_request_id:null,source_finished:false,events:[],
   question_submissions:0,notice_submissions:0,get_reply_calls:0,reply:null};
  const args={agent_id:recipient.agent_id,identity_revision:recipient.identity_revision,
   text:'Fictional '+provider+' return test. Reply only; do not start work.'};
  const sent=await rpc('send_message',args);
  assert.equal(sent.value.duplicate,false);
  current.public_request_id=sent.value.request_id;
  current.events.push('send_complete');
  current.source_finished=true;
  current.events.push('source_turn_complete');
  await fs.writeFile(config.source_ready_file,'ready');
  // The harness waits for durable target completion, then models one explicit lookup.
  // No hook, injected prompt, or automatic source turn is involved.
  await until(async()=>{
   const listing=(await rpc('list_requests')).value;
   return listing.requests.some(r=>r.request_id===current.public_request_id&&r.state==='answered');
  });
  if(provider==='claude') {
   const questions=(await fs.readFile(config.codex_question_file,'utf8')).trim().split('\n').map(JSON.parse);
   assert.equal(questions.length,current.reverse_index+1);
   assert(questions.every(q=>q.thread===config.codex_source));
   current.question_submissions=questions.filter(q=>q.request_id===questions.at(-1).request_id).length;
   current.events.push('target_answer');
  }
  current.get_reply_calls++;
  const result=(await rpc('get_reply',{request_id:current.public_request_id})).value;
  assert.equal(result.state,'answered');assert.equal(result.reply,'Fictional '+provider+' answer.');
  current.reply=result.reply;current.events.push('get_reply');
  assert.deepEqual(current.events,['send_complete','source_turn_complete','target_answer','get_reply']);
  assert.equal(current.notice_submissions,0);
  assert.equal(current.get_reply_calls,1);
  assert.equal(runtimes[2].submissions.length,0);
  const duplicate=(await rpc('send_message',{...args,request_id:current.public_request_id})).value;
  assert.equal(duplicate.duplicate,true);assert.equal(duplicate.request_id,current.public_request_id);
  cases.push({source_provider:provider,public_request_id:current.public_request_id,reply:current.reply,
   question_submissions:current.question_submissions,notice_submissions:current.notice_submissions,
   get_reply_calls:current.get_reply_calls,source_turn_finished_before_answer:true,sibling_notice_submissions:runtimes[2].submissions.length});
 }
 assert.equal(runtimes[0].submissions.length,2);
 assert.equal(runtimes[1].submissions.length,0);
 assert.equal(runtimes[2].submissions.length,0);
 console.log(JSON.stringify({cases}));
}finally{await client.close()}
