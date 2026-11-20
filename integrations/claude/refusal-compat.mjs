// Offline compatibility check against the frozen 200-character-error adapter.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {randomUUID,createHash,createHmac} from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const reasons=JSON.parse(await new Promise(resolve=>{let data='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>data+=x);process.stdin.on('end',()=>resolve(data));}));
assert(reasons.length>=6);
for(const reason of reasons){assert.equal(typeof reason,'string');assert(Array.from(reason).length<=200);}
const root=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'motet-compat-'));await fs.chmod(root,0o700);
const folder=path.join(root,'credentials');await fs.mkdir(folder,{mode:0o700});
const credential={version:2,credential_id:randomUUID(),secret:'a'.repeat(64),port:1},run=randomUUID();
const hash=v=>createHash('sha256').update(v).digest('hex');
const mac=lines=>createHmac('sha256',Buffer.from(credential.secret,'hex')).update(lines.join('\n')).digest('hex');
let selected='',sends=0;const sockets=new Set();
const server=http.createServer(async(req,res)=>{
 let raw='';for await(const chunk of req)raw+=chunk;const args=JSON.parse(raw);
 if(req.url==='/bridge/v2/challenge'){
  res.end(JSON.stringify({run_id:run,proof:mac(['watch-bridge-server-v2',String(credential.port),credential.credential_id,args.nonce,run])}));return;
 }
 assert.equal(req.url,'/bridge/v2/rpc');
 const nonce=req.headers['x-watch-nonce'];assert.equal(req.headers['x-watch-signature'],mac(['watch-bridge-request-v2',String(credential.port),'POST','/bridge/v2/rpc',req.headers['x-watch-time'],nonce,run,hash(raw)]));
 const discovery=args.method==='list_inboxes';if(!discovery){assert.equal(args.method,'send_message');sends+=1;}
 const status=discovery?200:429,body=JSON.stringify(discovery?{result:{inboxes:[]}}:{error:selected});
 res.writeHead(status,{'Content-Length':Buffer.byteLength(body),'X-Watch-Signature':mac(['watch-bridge-response-v2',String(credential.port),run,nonce,String(status),hash(body)])});res.end(body);
});
server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));credential.port=server.address().port;
const file=path.join(folder,'service.json');await fs.writeFile(file,JSON.stringify(credential),{mode:0o600});
const client=new Client({name:'fictional compatibility check',version:'1'});let connected=false;
try{
 const script=new URL('./fixtures/peer-200.fixture.mjs',import.meta.url);
 await client.connect(new StdioClientTransport({command:process.execPath,args:[script.pathname,'--credential',file],stderr:'pipe'}));connected=true;
 for(const reason of reasons){
  selected=reason;const before=sends;
  const result=await client.callTool({name:'send_message',arguments:{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),text:'Fictional compatibility request only.'}});
  assert.equal(result.isError,true);assert.equal(result.content[0].text,'Peer request rejected (429): '+reason);
  assert.match(result.content[1].text,/This attempt was not accepted/);assert.doesNotMatch(result.content[1].text,/Acceptance is not confirmed/);assert.equal(sends,before+1);
 }
 // Pin the frozen adapter's old limit: an oversized signed error remains uncertain.
 const refusal=' The new request was not accepted.';
 selected='x'.repeat(201-refusal.length)+refusal;
 assert.equal(Array.from(selected).length,201);const before=sends;
 const tooLong=await client.callTool({name:'send_message',arguments:{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),text:'Fictional boundary check.'}});
 assert.match(tooLong.content[1].text,/Acceptance is not confirmed/);assert.equal(sends,before+1);
 console.log('Legacy adapter confirmed '+reasons.length+' compact refusals without retries.');
}finally{if(connected)await client.close();for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));await fs.rm(root,{recursive:true,force:true});}
