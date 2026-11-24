// The bridge adapter's start-up on this system with a credential file Motet's own Python created: on Windows the
// adapter must read it only through Motet's Python check (Node cannot read an access list); elsewhere Node checks
// owner and mode itself. No bridge server runs: listing tools needs only a credential the adapter accepted.
// Runs on macOS, Linux and Windows. Fictional values and temporary folders only.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {execFileSync,spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {WINDOWS_PYTHON_REQUIRED} from './peer.mjs';

const here=path.dirname(fileURLToPath(import.meta.url)),repo=path.resolve(here,'..','..'),adapter=path.join(here,'peer.mjs');
const windows=process.platform==='win32';

function findPython(){
 for(const command of [process.env.PYTHON,'python3','python'].filter(Boolean)){
  try{
   const found=execFileSync(command,['-c','import sys; print(sys.executable)'],{encoding:'utf8',windowsHide:true,stdio:['ignore','pipe','ignore']}).trim();
   if(found&&path.isAbsolute(found))return found;
  }catch{/* try the next name */}
 }
 return null;
}

// Motet's Python makes the folders and the file private from the start (a protected access list on Windows).
function writeCredential(python,state,credential){
 const code=['import json, sys','sys.path[:0] = [sys.argv[1], sys.argv[2]]','import peer_bridge, watch_peer',
  'peer_bridge.private_directory(sys.argv[3], True)',
  'print(watch_peer.write_credential(sys.argv[3], json.loads(sys.argv[4]), 1))'].join('\n');
 return execFileSync(python,['-c',code,path.join(repo,'src'),path.join(repo,'scripts'),state,JSON.stringify(credential)],{encoding:'utf8',windowsHide:true}).trim();
}

function start(args){
 return new Promise(resolve=>{const p=spawn(process.execPath,[adapter,...args],{cwd:here,stdio:['ignore','pipe','pipe'],windowsHide:true});let stdout='',stderr='';p.stdout.on('data',b=>stdout+=b);p.stderr.on('data',b=>stderr+=b);p.on('exit',code=>resolve({code,stdout,stderr}))});
}

test('the adapter starts only with a credential Motet checked as private',async t=>{
 const python=findPython();if(!python){t.skip('Python is not available');return}
 const root=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'motet-adapter-'));
 try{
  const credential={version:2,credential_id:randomUUID(),secret:'e'.repeat(64)};
  const file=writeCredential(python,path.join(root,'state'),credential);
  const args=['--credential',file,...(windows?['--python',python]:[])];
  const client=new Client({name:'credential check host',version:'1'});
  await client.connect(new StdioClientTransport({command:process.execPath,args:[adapter,...args],cwd:here,stderr:'pipe'}));
  try{
   const names=(await client.listTools()).tools.map(tool=>tool.name).sort();
   assert.deepEqual(names,['get_reply','list_agents','list_requests','send_message']);
  }finally{await client.close()}
  if(windows){
   const missing=await start(['--credential',file]);
   assert.equal(missing.code,1);assert.equal(missing.stderr,WINDOWS_PYTHON_REQUIRED+'\n');
   // Readable by everyone: Motet's access-list rule refuses it, so the adapter does not start.
   execFileSync('icacls',[file,'/grant','*S-1-1-0:(R)'],{windowsHide:true,stdio:'ignore'});
  }else{
   await fs.chmod(file,0o644);
  }
  const refused=await start(args);
  assert.equal(refused.code,1);assert.equal(refused.stdout,'');assert.equal(refused.stderr,'Motet shared bridge adapter could not start.\n');
  for(const privateValue of [root,file,credential.secret,credential.credential_id])assert.equal(refused.stderr.includes(privateValue),false);
 }finally{await fs.rm(root,{recursive:true,force:true})}
});
