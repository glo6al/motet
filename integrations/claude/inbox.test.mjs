import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {randomUUID,createHmac,createHash} from 'node:crypto';
import http from 'node:http';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {canonical,inboxRevision,receiverArguments,readInboxCredential,validInboxArgs,safeInboxResult,publicTools,receiverTools} from './inbox.mjs';
import {validRequestId,validArgs,instructions,PeerClient} from './peer.mjs';

const config=()=>({version:1,inbox_id:randomUUID(),label:'Antigravity inbox',receiver_key:'a'.repeat(64)});
const base=c=>({inbox_id:c.inbox_id,inbox_revision:inboxRevision(c),request_id:randomUUID(),state:'queued',created_at:1000,expires_at:86401000,text_preview:'Fictional question',declared_sender:null});

test('receiver proof binds nonce, run, credential, mailbox revision, method and unambiguous Unicode arguments',()=>{
 const c=config(),credential={credential_id:randomUUID()},run=randomUUID(),nonce='c'.repeat(64),args={request_id:randomUUID(),claim_token:'d'.repeat(64)};
 const p=receiverArguments(c,credential,run,nonce,'claim_inbox_message',args);
 const signed={...p};delete signed.receiver_proof;
 const sha=s=>createHash('sha256').update(s).digest('hex');
 const expected=createHmac('sha256',Buffer.from(c.receiver_key,'hex')).update(['watch-inbox-receiver-v1',credential.credential_id,run,nonce,c.inbox_id,inboxRevision(c),'claim_inbox_message',sha(canonical(signed))].join('\n')).digest('hex');
 assert.equal(p.receiver_proof,expected);
 for(const q of [receiverArguments(c,credential,run,'e'.repeat(64),'claim_inbox_message',args),receiverArguments(c,credential,randomUUID(),nonce,'claim_inbox_message',args),receiverArguments(c,{credential_id:randomUUID()},run,nonce,'claim_inbox_message',args),receiverArguments({...c,receiver_key:'b'.repeat(64)},credential,run,nonce,'claim_inbox_message',args),receiverArguments(c,credential,run,nonce,'answer_inbox_message',{...args,answer:'Fictional café 😀\nresponse'})])assert.notEqual(q.receiver_proof,p.receiver_proof);
 assert.equal(canonical({z:'é😀\n',a:'"'}),'{"a":"\\\"","z":"é😀\\n"}');
 assert.throws(()=>receiverArguments(null,credential,run,nonce,'claim_inbox_message',args));
});

test('inbox request validation rejects proof overrides, implicit claims, bad IDs and invalid Unicode',()=>{
 const c=config(),send={inbox_id:c.inbox_id,inbox_revision:inboxRevision(c),text:'Fictional request'};
 assert.equal(validArgs('send_inbox_message',send),true);
 for(const extra of [{receiver_proof:'a'.repeat(64)},{inbox_id:[c.inbox_id]},{inbox_revision:[inboxRevision(c)]},{text:'\0'},{text:'\ud800'},{text:' '},{text:'x'.repeat(16001)}])assert.equal(validArgs('send_inbox_message',{...send,...extra}),false);
 assert.equal(validArgs('send_inbox_message',{...send,text:'😀'.repeat(16000)}),true);
 assert.equal(validArgs('claim_inbox_message',{request_id:randomUUID()}),false);
 assert.equal(validArgs('claim_inbox_message',{request_id:randomUUID(),claim_token:'a'.repeat(64)}),true);
 assert.equal(validArgs('check_inbox',{inbox_id:c.inbox_id}),false);
 assert.equal(validArgs('answer_inbox_message',{request_id:randomUUID(),claim_token:'a'.repeat(64),answer:' '}),false);
 assert.equal(validInboxArgs('prepare_inbox_claim',{},validRequestId),true);
 assert.equal(validInboxArgs('prepare_inbox_claim',{request_id:randomUUID()},validRequestId),false);
});

test('response projection preserves mailbox honesty and never forwards claim secrets or foreign fields',()=>{
 const c=config(),r=base(c),privateToken='f'.repeat(64);
 const sent=safeInboxResult('send_inbox_message',{...r,duplicate:false,claim_token:privateToken,receiver_key:privateToken,native_status:'working'},validRequestId);
 assert.equal(sent.state,'queued');assert.doesNotMatch(JSON.stringify(sent),new RegExp(privateToken));assert.equal(sent.native_status,undefined);
 const claimed=safeInboxResult('claim_inbox_message',{...r,state:'claimed',text:'Fictional instructions',claimed_at:2000,claim_token:privateToken},validRequestId);
 assert.equal(claimed.text,'Fictional instructions');assert.equal(claimed.claim_token,undefined);
 const repliedClaim=safeInboxResult('claim_inbox_message',{...r,state:'replied',claimed_at:2000},validRequestId);assert.equal(repliedClaim.state,'replied');assert.equal(repliedClaim.text,undefined);
 assert.equal(safeInboxResult('get_inbox_reply',{...r,state:'replied',answer:'Fictional answer'},validRequestId).answer,'Fictional answer');
 for(const bad of [{state:'working'},{inbox_id:[c.inbox_id]},{created_at:1.5},{expires_at:86401001},{declared_sender:[]},{text_preview:'x'.repeat(161)}])assert.throws(()=>safeInboxResult('send_inbox_message',{...r,duplicate:false,...bad},validRequestId));
 assert.throws(()=>safeInboxResult('get_inbox_reply',{...r,answer:'Not yet replied'},validRequestId));
 assert.throws(()=>safeInboxResult('get_inbox_reply',{...r,state:'replied',answer:''},validRequestId));
 assert.throws(()=>safeInboxResult('claim_inbox_message',{...r,text:'Instructions',claimed_at:2000},validRequestId));
 assert.throws(()=>safeInboxResult('list_inbox_requests',{requests:Array(51).fill(r),next_cursor:null},validRequestId));
});

test('receiver configuration is private, exact, no-follow and never accepts a hard link',async()=>{
 const root=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'motet-inbox-test-'));
 try{
  await fs.chmod(root,0o700);const file=path.join(root,'inbox.json'),c=config();await fs.writeFile(file,JSON.stringify(c),{mode:0o600});
  assert.deepEqual(await readInboxCredential(file),c);
  await fs.chmod(file,0o644);await assert.rejects(readInboxCredential(file));await fs.chmod(file,0o600);
  const link=path.join(root,'link');await fs.symlink(file,link);await assert.rejects(readInboxCredential(link));await fs.unlink(link);
  await fs.link(file,link);await assert.rejects(readInboxCredential(file));await fs.unlink(link);
  for(const bad of [{...c,inbox_id:[c.inbox_id]},{...c,receiver_key:[c.receiver_key]},{...c,unexpected:true},{...c,version:2},{...c,label:'Other inbox'}]){await fs.writeFile(file,JSON.stringify(bad));await assert.rejects(readInboxCredential(file));}
  await fs.writeFile(file,JSON.stringify(c).replace('"version":1','"version":1,"vers\\u0069on":1'));await assert.rejects(readInboxCredential(file));
  await fs.writeFile(file,JSON.stringify(c)+' '.repeat(4096));await assert.rejects(readInboxCredential(file));
  await fs.writeFile(file,JSON.stringify(c).replace('"version":1','"version":1.0'));await assert.rejects(readInboxCredential(file));
  await fs.writeFile(file,JSON.stringify(c));await fs.chmod(file,0o4600);await assert.rejects(readInboxCredential(file));await fs.chmod(file,0o600);
 }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('inbox tool guidance distinguishes previews, confirmed claims and native final markers',()=>{
 assert.deepEqual(publicTools.map(t=>t.name),['list_inboxes','send_inbox_message','get_inbox_reply','list_inbox_requests']);
 assert.deepEqual(receiverTools.map(t=>t.name),['prepare_inbox_claim','check_inbox','claim_inbox_message','answer_inbox_message']);
 assert.match(instructions,/a final WATCH_REPLY marker does not answer an inbox/);
 assert.match(instructions,/retain that token before mutation/);
 assert.match(instructions,/not a verified named native chat/);
 for(const t of [...publicTools,...receiverTools])assert.equal(t.inputSchema.additionalProperties,false);
});

test('actual stdio receiver advertises scoped tools and prepares a token before any HTTP mutation',async()=>{
 const root=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'motet-inbox-mcp-'));
 const c=config(),credential={version:2,credential_id:randomUUID(),secret:'b'.repeat(64)},run=randomUUID(),r=base(c),calls=[];
 const hash=x=>createHash('sha256').update(x).digest('hex');
 const mac=(key,parts)=>createHmac('sha256',key).update(parts.join('\n')).digest('hex');
 let server,client,inboxMode='configured';
 try{
  await fs.chmod(root,0o700);const state=path.join(root,'state'),dir=path.join(state,'bridge-credentials');await fs.mkdir(dir,{recursive:true,mode:0o700});await fs.chmod(state,0o700);
  server=http.createServer(async(req,res)=>{
   const chunks=[];for await(const b of req)chunks.push(b);const body=Buffer.concat(chunks),value=JSON.parse(body.toString());
   if(req.url==='/bridge/v2/challenge'){
    const proof={run_id:run,proof:mac(Buffer.from(credential.secret,'hex'),['watch-bridge-server-v2',String(credential.port),credential.credential_id,value.nonce,run])};
    if(value.inbox_id)proof.inbox_proof=mac(Buffer.from(c.receiver_key,'hex'),['watch-inbox-server-v1',String(credential.port),credential.credential_id,value.nonce,run,c.inbox_id,inboxRevision(c)]);
    res.end(JSON.stringify(proof));return;
   }
   calls.push(value);const p=value.params,nonce=req.headers['x-watch-nonce'];
   const receiving=['check_inbox','claim_inbox_message'].includes(value.method);
   if(receiving){
    const args={...p};delete args.receiver_proof;
    const expected=mac(Buffer.from(c.receiver_key,'hex'),['watch-inbox-receiver-v1',credential.credential_id,run,nonce,c.inbox_id,inboxRevision(c),value.method,hash(canonical(args))]);
    assert.equal(p.receiver_proof,expected);
    assert.equal(p.inbox_id,c.inbox_id);assert.equal(p.inbox_revision,inboxRevision(c));
   }
   const result=value.method==='list_inboxes'?(inboxMode==='configured'?{inboxes:[{inbox_id:c.inbox_id,inbox_revision:inboxRevision(c),label:c.label,availability:'shared pull inbox configured'}]}:inboxMode==='malformed'?{wrong:true}:{inboxes:[]}):value.method==='list_agents'?{agents:[],next_cursor:null,directory:{complete:true,reasons:[],refreshed_at:Date.now()/1000}}:value.method==='check_inbox'?{requests:[r],next_cursor:null}:{...r,state:'claimed',request_id:p.request_id,text:'Fictional work request.',claimed_at:2000};
   const response=JSON.stringify({result});res.setHeader('X-Watch-Signature',mac(Buffer.from(credential.secret,'hex'),['watch-bridge-response-v2',String(credential.port),run,nonce,'200',hash(response)]));
   if(receiving)res.setHeader('X-Watch-Inbox-Signature',mac(Buffer.from(c.receiver_key,'hex'),['watch-inbox-response-v1',String(credential.port),credential.credential_id,run,nonce,c.inbox_id,inboxRevision(c),'200',hash(response)]));
   res.end(response);
  });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));credential.port=server.address().port;
  const credentialFile=path.join(dir,'shared.json'),inboxFile=path.join(state,'inbox.json');await fs.writeFile(credentialFile,JSON.stringify(credential),{mode:0o600});await fs.writeFile(inboxFile,JSON.stringify(c),{mode:0o600});
  const script=new URL('./peer.mjs',import.meta.url).pathname;
  client=new Client({name:'fictional-inbox-receiver',version:'1'});
  await client.connect(new StdioClientTransport({command:process.execPath,args:[script,'--credential',credentialFile,'--inbox-credential',inboxFile],stderr:'pipe'}));
  const tools=(await client.listTools()).tools.map(t=>t.name);for(const t of receiverTools)assert.ok(tools.includes(t.name));
  const startupReads=calls.length;const prepared=JSON.parse((await client.callTool({name:'prepare_inbox_claim',arguments:{}})).content[0].text);
  assert.match(prepared.claim_token,/^[0-9a-f]{64}$/);assert.equal(calls.length,startupReads,'token preparation must not mutate server state');
  const checked=await client.callTool({name:'check_inbox',arguments:{}});assert.equal(checked.isError,undefined);assert.equal(calls.length,startupReads+1);
  const claimed=await client.callTool({name:'claim_inbox_message',arguments:{request_id:r.request_id,claim_token:prepared.claim_token}});
  assert.equal(claimed.isError,undefined);assert.equal(calls.length,startupReads+2);assert.ok(!JSON.stringify(claimed).includes(prepared.claim_token));
  assert.equal(JSON.parse(claimed.content[0].text).untrusted_peer_data.state,'claimed');
  await client.close();client=null;
  for(const invalid of ['missing','malformed','nonprivate','deep','utf16','fraction_version','special_mode']){
   const optional=path.join(state,'optional-'+invalid+'.json');
   if(invalid!=='missing'){
    const raw=invalid==='deep'?'['.repeat(1500)+']'.repeat(1500):invalid==='utf16'?Buffer.from(JSON.stringify(c),'utf16le'):invalid==='malformed'?'not json':invalid==='fraction_version'?JSON.stringify(c).replace('"version":1','"version":1.0'):JSON.stringify(c);
    await fs.writeFile(optional,raw,{mode:invalid==='nonprivate'?0o644:0o600});
    if(invalid==='special_mode')await fs.chmod(optional,0o4600);
   }
   client=new Client({name:'fictional-optional-receiver-test',version:'1'});
   await client.connect(new StdioClientTransport({command:process.execPath,args:[script,'--credential',credentialFile,'--inbox-credential',optional],stderr:'pipe'}));
   const available=(await client.listTools()).tools.map(t=>t.name);assert.ok(!available.includes('check_inbox'),invalid);assert.ok(available.includes('send_message'),invalid);
   const ordinary=await client.callTool({name:'list_agents',arguments:{}});assert.equal(ordinary.isError,undefined,invalid);
   await client.close();client=null;
  }
  for(const mode of ['unconfigured','malformed']){
   inboxMode=mode;client=new Client({name:'fictional-native-only',version:'1'});
   await client.connect(new StdioClientTransport({command:process.execPath,args:[script,'--credential',credentialFile],stderr:'pipe'}));
   assert.deepEqual((await client.listTools()).tools.map(t=>t.name),['list_agents','send_message','get_reply','list_requests']);
   assert.doesNotMatch(client.getInstructions(),/list_inboxes|send_inbox_message|claim_inbox_message/);
   const before=calls.length;assert.equal((await client.callTool({name:'list_inboxes',arguments:{}})).isError,true);assert.equal(calls.length,before);
   assert.equal((await client.callTool({name:'list_agents',arguments:{}})).isError,undefined);
   await client.close();client=null;
  }
 }finally{if(client)await client.close();if(server)await new Promise(resolve=>server.close(resolve));await fs.rm(root,{recursive:true,force:true});}
});

test('a shared-key-only endpoint cannot receive a private claim token or forge a receiver receipt',async()=>{
 const c=config(),credential={credential_id:randomUUID(),secret:'b'.repeat(64)},run=randomUUID(),calls=[];
 const sha=x=>createHash('sha256').update(x).digest('hex'),mac=(key,parts)=>createHmac('sha256',key).update(parts.join('\n')).digest('hex');
 let badResponse=false;
 const server=http.createServer(async(req,res)=>{
  const chunks=[];for await(const b of req)chunks.push(b);const raw=Buffer.concat(chunks),v=JSON.parse(raw.toString());
  if(req.url==='/bridge/v2/challenge'){
   const proof={run_id:run,proof:mac(Buffer.from(credential.secret,'hex'),['watch-bridge-server-v2',String(credential.port),credential.credential_id,v.nonce,run])};
   if(badResponse)proof.inbox_proof=mac(Buffer.from(c.receiver_key,'hex'),['watch-inbox-server-v1',String(credential.port),credential.credential_id,v.nonce,run,c.inbox_id,inboxRevision(c)]);
   res.end(JSON.stringify(proof));return;
  }
  calls.push(raw.toString());const nonce=req.headers['x-watch-nonce'],result={...base(c),request_id:v.params.request_id,state:'claimed',text:'Fictional request',claimed_at:2000},body=JSON.stringify({result});
  res.setHeader('X-Watch-Signature',mac(Buffer.from(credential.secret,'hex'),['watch-bridge-response-v2',String(credential.port),run,nonce,'200',sha(body)]));
  res.setHeader('X-Watch-Inbox-Signature','0'.repeat(64));res.end(body);
 });
 try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));credential.port=server.address().port;
  const client=new PeerClient(credential,c),args={request_id:randomUUID(),claim_token:'d'.repeat(64)};
  await assert.rejects(client.call('claim_inbox_message',args));assert.equal(calls.length,0,'No claim token reaches shared-key-only endpoint');
  badResponse=true;await assert.rejects(client.call('claim_inbox_message',args));assert.equal(calls.length,1,'Even a shared-signed result needs the receiver signature');
 }finally{await new Promise(resolve=>server.close(resolve));}
});
