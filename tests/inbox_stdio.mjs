// Called only by the disposable Python HTTP fixture. Never targets a native app.
import assert from 'node:assert/strict';
import {Client} from '../integrations/claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import {StdioClientTransport} from '../integrations/claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';
import path from 'node:path';
const chunks=[];for await(const b of process.stdin)chunks.push(b);
const input=JSON.parse(Buffer.concat(chunks).toString()),clients=[];
async function connect(receiver){
 const client=new Client({name:'fictional-inbox-test',version:'1'});
 clients.push(client);
 await client.connect(new StdioClientTransport({command:process.execPath,args:[path.resolve('integrations/claude/peer.mjs'),'--credential',input.credential_file,...receiver?['--inbox-credential',input.inbox_file]:[]],stderr:'pipe'}));
 return client;
}
async function call(client,name,args={}){
 const result=await client.callTool({name,arguments:args});assert.ok(!result.isError,'Inbox operation rejected');
 const data=JSON.parse(result.content[0].text);return data.untrusted_peer_data??data;
}
try{
 const receiver=await connect(true);
 if(input.mode==='claim'){
  const result=await call(receiver,'claim_inbox_message',{request_id:input.request_id,claim_token:input.claim_token});
  assert.equal(result.state,'claimed');assert.ok(result.text.includes('Fictional'));
 }else{
  const sender=await connect(false),senderTools=(await sender.listTools()).tools.map(t=>t.name);
  assert.ok(!senderTools.includes('check_inbox'));assert.ok(senderTools.includes('send_inbox_message'));
  const {inboxes}=await call(sender,'list_inboxes');assert.equal(inboxes.length,1);
  const request=await call(sender,'send_inbox_message',{inbox_id:inboxes[0].inbox_id,inbox_revision:inboxes[0].inbox_revision,text:'Fictional café request. Explain apples.\nNo tools or files.',declared_sender:'Fictional sender'});
  assert.equal(request.state,'queued');
  const token=(await call(receiver,'prepare_inbox_claim')).claim_token;assert.match(token,/^[0-9a-f]{64}$/);
  const checked=await call(receiver,'check_inbox');assert.ok(checked.requests.some(r=>r.request_id===request.request_id));
  const claimed=await call(receiver,'claim_inbox_message',{request_id:request.request_id,claim_token:token});assert.equal(claimed.state,'claimed');
  const again=await call(receiver,'claim_inbox_message',{request_id:request.request_id,claim_token:token});assert.deepEqual(again,claimed);
  const foreign=await receiver.callTool({name:'claim_inbox_message',arguments:{request_id:request.request_id,claim_token:'f'.repeat(64)}});assert.equal(foreign.isError,true);
  const replied=await call(receiver,'answer_inbox_message',{request_id:request.request_id,claim_token:token,answer:'Fictional answer: café and apples 🍎.'});assert.equal(replied.state,'replied');
  const recoveredAnswer=await call(receiver,'claim_inbox_message',{request_id:request.request_id,claim_token:token});assert.equal(recoveredAnswer.state,'replied');assert.equal(recoveredAnswer.text,undefined);
  const answer=await call(sender,'get_inbox_reply',{request_id:request.request_id});assert.equal(answer.answer,'Fictional answer: café and apples 🍎.');
 }
 console.log(JSON.stringify({ok:true,mode:input.mode}));
}finally{for(const c of clients)await c.close();}
