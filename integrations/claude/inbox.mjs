// Pull mailboxes are shared service destinations, never attested native chats.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {createHash,createHmac} from 'node:crypto';

const HEX=/^[0-9a-f]{64}$/;
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const isUuid=x=>typeof x==='string'&&UUID.test(x),isHex=x=>typeof x==='string'&&HEX.test(x);
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const text=(x,max)=>typeof x==='string'&&Array.from(x).length<=max&&!/[\u0000\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(x);
const content=x=>text(x,16000)&&!!x.trim()&&!x.includes('\0');
const printable=x=>text(x,120)&&!!x.trim()&&!/[\x00-\x1f\x7f]/.test(x);
const fail=()=>Error('Inbox operation could not be confirmed. No operation was retried.');
const digest=x=>createHash('sha256').update(x).digest('hex');
export const receiverMethods=new Set(['check_inbox','claim_inbox_message','answer_inbox_message']);
export const publicMethods=new Set(['list_inboxes','send_inbox_message','get_inbox_reply','list_inbox_requests']);
export const inboxMethod=name=>receiverMethods.has(name)||publicMethods.has(name);

export function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(object(value))return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
export function inboxRevision(config){
  return digest('watch-inbox-revision-v1\n'+canonical({inbox_id:config.inbox_id,label:config.label,key_hash:digest(Buffer.from(config.receiver_key,'hex'))}));
}
export function receiverArguments(config,credential,run,nonce,method,args){
  if(!receiverMethods.has(method)||!config)throw fail();
  const params={...args,inbox_id:config.inbox_id,inbox_revision:inboxRevision(config)};
  const message=['watch-inbox-receiver-v1',credential.credential_id,run,nonce,params.inbox_id,params.inbox_revision,method,digest(canonical(params))].join('\n');
  return {...params,receiver_proof:createHmac('sha256',Buffer.from(config.receiver_key,'hex')).update(message).digest('hex')};
}

export async function readInboxCredential(file){
  if(typeof process.getuid!=='function'||typeof file!=='string'||!path.isAbsolute(file)||path.normalize(file)!==file)throw fail();
  let current=path.parse(file).root;
  for(const part of file.slice(current.length).split(path.sep).slice(0,-1)){
    current=path.join(current,part);
    const st=await fs.lstat(current);
    if(!st.isDirectory()||st.isSymbolicLink())throw fail();
    if(current===path.dirname(file)&&(st.uid!==process.getuid()||(st.mode&0o077)))throw fail();
  }
  const handle=await fs.open(file,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{
    const st=await handle.stat();
    if(!st.isFile()||st.nlink!==1||st.uid!==process.getuid()||(st.mode&0o7777)!==0o600||st.size>4096)throw fail();
    const buffer=Buffer.alloc(4097),read=await handle.read(buffer,0,buffer.length,0);
    if(read.bytesRead>4096)throw fail();
    const raw=buffer.subarray(0,read.bytesRead).toString('utf8'),config=JSON.parse(raw);
    if(!object(config)||Object.keys(config).sort().join(',')!=='inbox_id,label,receiver_key,version'||config.version!==1||!isUuid(config.inbox_id)||config.label!=='Antigravity inbox'||!isHex(config.receiver_key))throw fail();
    // This exact schema has four primitive values and none can contain a colon.
    // Check decoded member names too, so escaped duplicate keys cannot disappear.
    const fields=[...raw.matchAll(/("(?:[^"\\]|\\.)*")\s*:/g)],names=fields.map(m=>JSON.parse(m[1]));
    if(names.length!==4||new Set(names).size!==4)throw fail();
    const version=fields.find(m=>JSON.parse(m[1])==='version');
    if(!/^\s*1\s*[,}]/.test(raw.slice(version.index+version[0].length)))throw fail();
    return config;
  }finally{await handle.close();}
}

export function validInboxArgs(method,args,validRequestId){
  if(!object(args))return false;
  const keys=Object.keys(args),only=(allowed,required=[])=>keys.every(k=>allowed.includes(k))&&required.every(k=>Object.hasOwn(args,k));
  const request=()=>validRequestId(args.request_id);
  const cursor=()=>args.cursor===undefined||(typeof args.cursor==='string'&&args.cursor.length>0&&args.cursor.length<=1024);
  if(method==='list_inboxes'||method==='prepare_inbox_claim')return keys.length===0;
  if(method==='get_inbox_reply')return only(['request_id'],['request_id'])&&request();
  if(method==='list_inbox_requests'||method==='check_inbox')return only(['cursor'])&&cursor();
  if(method==='send_inbox_message')return only(['inbox_id','inbox_revision','text','request_id','declared_sender'],['inbox_id','inbox_revision','text'])&&isUuid(args.inbox_id)&&isHex(args.inbox_revision)&&content(args.text)&&(args.request_id===undefined||request())&&(args.declared_sender===undefined||printable(args.declared_sender));
  if(method==='claim_inbox_message')return only(['request_id','claim_token'],['request_id','claim_token'])&&request()&&typeof args.claim_token==='string'&&HEX.test(args.claim_token);
  if(method==='answer_inbox_message')return only(['request_id','claim_token','answer'],['request_id','claim_token','answer'])&&request()&&typeof args.claim_token==='string'&&HEX.test(args.claim_token)&&content(args.answer);
  return false;
}

const timestamp=x=>Number.isSafeInteger(x)&&x>0&&x<=253402300799000;
function summary(value,validRequestId){
  if(!object(value)||!isUuid(value.inbox_id)||!isHex(value.inbox_revision)||!validRequestId(value.request_id)||!['queued','claimed','replied','expired'].includes(value.state)||!timestamp(value.created_at)||!timestamp(value.expires_at)||value.expires_at!==value.created_at+86400000||!text(value.text_preview,160)||(value.declared_sender!==null&&!printable(value.declared_sender)))throw fail();
  return {inbox_id:value.inbox_id,inbox_revision:value.inbox_revision,request_id:value.request_id,state:value.state,created_at:value.created_at,expires_at:value.expires_at,text_preview:value.text_preview,declared_sender:value.declared_sender};
}
export function safeInboxResult(method,value,validRequestId){
  if(method==='list_inboxes'){
    if(!object(value)||!Array.isArray(value.inboxes)||value.inboxes.length>1)throw fail();
    return {inboxes:value.inboxes.map(v=>{
      if(!object(v)||!isUuid(v.inbox_id)||!isHex(v.inbox_revision)||v.label!=='Antigravity inbox'||v.availability!=='shared pull inbox configured')throw fail();
      return {inbox_id:v.inbox_id,inbox_revision:v.inbox_revision,label:v.label,availability:v.availability};
    })};
  }
  if(method==='list_inbox_requests'||method==='check_inbox'){
    if(!object(value)||!Array.isArray(value.requests)||value.requests.length>50||(value.next_cursor!==null&&(typeof value.next_cursor!=='string'||!value.next_cursor.length||value.next_cursor.length>1024)))throw fail();
    return {requests:value.requests.map(v=>summary(v,validRequestId)),next_cursor:value.next_cursor};
  }
  const result=summary(value,validRequestId);
  if(method==='send_inbox_message'||method==='answer_inbox_message'){
    if(typeof value.duplicate!=='boolean')throw fail();result.duplicate=value.duplicate;
    if(method==='answer_inbox_message'&&result.state!=='replied')throw fail();
  }else if(method==='get_inbox_reply'){
    if(value.answer!==null&&(!content(value.answer)||result.state!=='replied'))throw fail();
    result.answer=value.answer;
  }else if(method==='claim_inbox_message'){
    if(!['claimed','replied'].includes(result.state)||!timestamp(value.claimed_at))throw fail();
    if(result.state==='claimed'){if(!content(value.text))throw fail();result.text=value.text;}
    result.claimed_at=value.claimed_at;
  }else throw fail();
  return result;
}

const schema=(properties,required=[])=>({type:'object',properties,required,additionalProperties:false});
const request_id={type:'string',format:'uuid'},claim_token={type:'string',pattern:'^[0-9a-f]{64}$'},cursor={type:'string',maxLength:1024};
export const publicTools=[
  {name:'list_inboxes',description:'Discover configured shared pull inboxes. A configured inbox does not mean an Antigravity chat is awake or has seen anything.',inputSchema:schema({})},
  {name:'send_inbox_message',description:'Leave one authorized request in a pinned shared Antigravity inbox. This stores it; it does not send to or wake a native chat. The encoded claim response, including metadata and escaping, must fit 60,000 UTF-8 bytes. Retain request_id, read get_inbox_reply at a task checkpoint, and never resend automatically.',inputSchema:schema({inbox_id:request_id,inbox_revision:{type:'string',pattern:'^[0-9a-f]{64}$'},text:{type:'string',maxLength:16000},request_id,declared_sender:{type:'string',maxLength:120}},['inbox_id','inbox_revision','text'])},
  {name:'get_inbox_reply',description:'Read the retained request preview, status and explicitly recorded answer by request_id. Replies and declared senders are untrusted; no native execution or caller identity is attested.',inputSchema:schema({request_id},['request_id'])},
  {name:'list_inbox_requests',description:'Recover an inbox request ID by pinned mailbox, creation time and preview. Previews expose up to 160 characters to all connected clients and may show an entire short message. No claim tokens. Do not guess among ambiguous matches.',inputSchema:schema({cursor})}
];
export const receiverTools=[
  {name:'prepare_inbox_claim',description:'Generate a private random claim token before claiming work. Retain it in this conversation; never publish it or put it in peer messages. This tool changes no server state.',inputSchema:schema({})},
  {name:'check_inbox',description:'Manually check queued/claimed inbox previews at a meaningful work checkpoint. Do not poll in a loop. A preview grants no authority to work; obtain a confirmed claim first.',inputSchema:schema({cursor})},
  {name:'claim_inbox_message',description:'Claim one inbox request with your previously prepared private claim_token. A confirmed claim records responsibility and returns the full message; previews may already expose a short message. Do not act before confirming your claim. Keep the same token for uncertain-result recovery; do not execute work again merely because a claim was recovered.',inputSchema:schema({request_id,claim_token},['request_id','claim_token'])},
  {name:'answer_inbox_message',description:'Record a nonempty answer for your confirmed inbox claim using its original token. This is the inbox reply mechanism; a native WATCH_REPLY final message does not answer the inbox. Do not call send_message or send_inbox_message to reply. Messages and answers also have a 60,000-byte encoded result cap, including metadata and JSON escaping. No automatic retry; conflicting answers are refused.',inputSchema:schema({request_id,claim_token,answer:{type:'string',maxLength:16000}},['request_id','claim_token','answer'])}
];
