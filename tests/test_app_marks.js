#!/usr/bin/env node
'use strict';
// Side panel app icons: one request batch per authenticated session, never in the demo, owner key header only, text
// kept on any failure, and nothing outlives a lock. Fictional icon bytes; no server, no network.
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),vm=require('vm');
const html=fs.readFileSync(path.join(__dirname,'..','src','web','index.html'),'utf8').replace(/\r\n?/g,'\n');
const start=html.indexOf('function workspaceAppName('),end=html.indexOf('function workspaceFolder(',start);
assert.ok(start>0&&end>start,'app mark helpers are present');
const source=html.slice(start,end);
assert.match(html,/function lock\(on\)\{if\(on&&typeof workspaceAppIconsReset==='function'\)workspaceAppIconsReset\(\);/,'locking resets the icons first');
const realTxt=html.split('\n').find(l=>l.startsWith('const txt='));assert.ok(realTxt,'the page\'s own txt() helper');
const tick=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function element(tag){const e={tag,dataset:{},children:[],className:'',hidden:false,_text:'',append(...n){this.children.push(...n)},get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('')},set textContent(v){this._text=String(v);this.children=[]}};return e;}
function rig({demo=false,key='fictional-owner-key',workspace=true,responses={},holdDecode=false}={}){
  const calls=[],revoked=[],decodes=[],rows=new Map(),signals=[];let serial=0;
  const mark=element('span'),label=element('span');label.children=[element('span'),mark];const row=element('button');row.children=[element('span'),label];rows.set('fictional-agent',row);
  class Signal{constructor(){this.aborted=false}}
  class Controller{constructor(){this.signal=new Signal()}abort(){this.signal.aborted=true}}
  const ctx={DEMO:demo,WORKSPACE:workspace,KEY:key,locked:false,APP_ICONS:{batch:null,icons:{}},WS:{agents:rows},document:{createElement:element},Object,String,Set,
    AbortController:Controller,AbortSignal:{timeout:()=>new Signal(),any:list=>{const s=new Signal();s.parts=list;return s;}},
    URL:{createObjectURL:b=>'blob:fictional-'+b.type+'-'+(++serial),revokeObjectURL:u=>revoked.push(u)},Blob:class{constructor(parts,o){this.type=o.type;this.size=parts[0].size}},
    Image:class{decode(){if(!holdDecode)return Promise.resolve();return new Promise((resolve,reject)=>decodes.push({resolve,reject}));}},
    fetch:(url,opts)=>{calls.push([url,opts]);signals.push(opts.signal);const r=responses[url];if(!r)return Promise.resolve({ok:false,headers:{get:()=>'text/plain'}});return Promise.resolve({ok:true,headers:{get:()=>r.type},blob:async()=>({size:r.size??40,type:r.type})});}};
  vm.createContext(ctx);vm.runInContext(realTxt.replace('const txt=','var txt=')+'\n'+source,ctx);
  ctx.workspaceAppMarkPaint(mark,'Codex');assert.equal(mark.textContent,'Codex');
  const lock=()=>{ctx.workspaceAppIconsReset();ctx.locked=true;};
  return {ctx,calls,revoked,decodes,signals,mark,lock};
}
(async()=>{
  for(const variant of [{demo:true},{key:''},{workspace:false}]){const r=rig(variant);r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.calls.length,0,JSON.stringify(variant));assert.equal(r.mark.textContent,'Codex');}
  let r=rig();r.ctx.locked=true;r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.calls.length,0,'a locked page asks for nothing');
  r=rig();r.ctx.workspaceAppIconsLoad();r.ctx.workspaceAppIconsLoad();await tick();
  assert.deepEqual([...r.calls.map(c=>c[0])],['/app-icon/claude','/app-icon/codex','/app-icon/antigravity','/app-icon/cursor'],'four names, once per session, despite a second call');
  for(const [,o] of r.calls){assert.equal(JSON.stringify(o.headers),JSON.stringify({'X-Live-Room-Key':'fictional-owner-key'}));assert.equal(o.cache,'no-store');}
  assert.equal(r.mark.textContent,'Codex','no icon file: text stays');
  for(const bad of [{type:'text/html'},{type:'image/svg+xml',size:0},{type:'image/png',size:300000}]){r=rig({responses:{'/app-icon/codex':bad}});r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.mark.textContent,'Codex',JSON.stringify(bad));assert.equal(r.ctx.APP_ICONS.icons.Codex,undefined);}
  // An already painted icon: lock aborts, revokes and restores the text; unlocking asks again and the icon comes back.
  const codex={'/app-icon/codex':{type:'image/svg+xml'}};
  r=rig({responses:codex});r.ctx.workspaceAppIconsLoad();await tick();
  const painted=r.ctx.APP_ICONS.icons.Codex;assert.match(painted,/^blob:fictional-image\/svg\+xml/);assert.equal(r.mark.children[0].src,painted);assert.equal(r.mark.className,'workspace-app workspace-app-icon');
  r.lock();assert.equal(r.mark.textContent,'Codex','lock restores the text mark');assert.equal(r.mark.className,'workspace-app');assert.ok(r.revoked.includes(painted),'lock revokes the painted icon');
  assert.ok(r.signals.every(s=>s.parts?s.parts[0].aborted:s.aborted),'lock aborts the batch');assert.equal(r.ctx.APP_ICONS.batch,null);
  r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.calls.length,4,'still locked: no new batch');
  r.ctx.locked=false;r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.calls.length,8,'the next authenticated session asks once more');
  assert.equal(r.mark.children[0].src,r.ctx.APP_ICONS.icons.Codex);assert.notEqual(r.ctx.APP_ICONS.icons.Codex,painted,'a fresh URL, never the revoked one');
  // A decode that completes after lock never paints, and its URL is revoked.
  r=rig({responses:codex,holdDecode:true});r.ctx.workspaceAppIconsLoad();await tick();assert.equal(r.decodes.length,1);
  r.lock();const stale=[...r.revoked];r.decodes[0].resolve();await tick();
  assert.equal(r.mark.textContent,'Codex');assert.equal(r.ctx.APP_ICONS.icons.Codex,undefined,'a stale decode publishes nothing');assert.ok(stale.some(u=>/^blob:fictional-image\/svg\+xml/.test(u)),'the in-flight URL is revoked at lock');
  // A key change without a lock retires the old batch: its late decode is dropped and the new key gets its own batch.
  r=rig({responses:codex,holdDecode:true});r.ctx.workspaceAppIconsLoad();await tick();r.ctx.KEY='fictional-second-key';r.ctx.workspaceAppIconsLoad();await tick();
  assert.equal(r.calls.length,8);assert.equal(JSON.stringify(r.calls[7][1].headers),JSON.stringify({'X-Live-Room-Key':'fictional-second-key'}));
  r.decodes[0].resolve();await tick();assert.equal(r.ctx.APP_ICONS.icons.Codex,undefined,'the first key\'s decode is stale');r.decodes[1].resolve();await tick();assert.match(r.ctx.APP_ICONS.icons.Codex,/^blob:/);
  // Through the real entry points: the page's own takeKey() and start(), with an already painted icon and a held check.
  {const take=html.slice(html.indexOf('function takeKey('),html.indexOf('\ntakeKey();')),boot=html.slice(html.indexOf('function start(){'),html.indexOf('\n}',html.indexOf('function start(){'))+2);
   assert.ok(take.includes('workspaceAppIconsReset()')&&boot.includes('workspaceAppIconsReset()'),'both entry points retire a different key\'s icons');
   const r=rig({responses:codex,key:'fictional-key-one'}),checks=[],realFetch=r.ctx.fetch;
   Object.assign(r.ctx,{claiming:false,authN:0,connects:0,location:{hash:'',pathname:'/',search:'?ui=workspace'},history:{replaceState(){}},localStorage:{setItem(){},removeItem(){}},URLSearchParams,encodeURIComponent,
     $:()=>({}),setTimeout:()=>0,lock(on){if(on)r.ctx.workspaceAppIconsReset();r.ctx.locked=on;},connect(){r.ctx.connects++;},
     fetch:(url,opts)=>{if(url.startsWith('/check?')){return new Promise((resolve,reject)=>checks.push({url,resolve,reject}));}return realFetch(url,opts);}});
   vm.runInContext(take+'\n'+boot,r.ctx);
   r.ctx.start();assert.equal(checks.length,1);checks[0].resolve({ok:true});await tick();
   const first=r.ctx.APP_ICONS.icons.Codex;assert.match(first,/^blob:/);assert.equal(r.mark.children[0].src,first);const before=r.calls.length;assert.equal(before,4);
   // The page takes a different key; its check is held (slow or offline).
   r.ctx.location.hash='#k=fictional-key-two';assert.equal(r.ctx.takeKey(),true);
   assert.equal(r.ctx.KEY,'fictional-key-two');assert.equal(r.mark.textContent,'Codex','text is back as soon as the key changes');assert.ok(r.revoked.includes(first),'the old URL is revoked at once');assert.equal(r.ctx.APP_ICONS.batch,null);
   r.ctx.start();assert.equal(checks.length,2);assert.match(checks[1].url,/fictional-key-two/);await tick();
   assert.equal(r.calls.length,before,'no icon requests while the new key is unchecked');assert.equal(r.mark.textContent,'Codex');
   checks[1].reject(new Error('offline'));await tick();assert.equal(r.calls.length,before,'a failed check asks for nothing');assert.equal(r.mark.textContent,'Codex');
   r.ctx.start();checks[2].resolve({ok:true});await tick();
   const fresh=r.calls.slice(before);assert.equal(fresh.length,4,'one fresh batch after success');assert.ok(fresh.every(([,o])=>o.headers['X-Live-Room-Key']==='fictional-key-two'));
   assert.match(r.ctx.APP_ICONS.icons.Codex,/^blob:/);assert.notEqual(r.ctx.APP_ICONS.icons.Codex,first);assert.equal(r.mark.children[0].src,r.ctx.APP_ICONS.icons.Codex);
   r.ctx.start();checks[3].resolve({ok:true});await tick();assert.equal(r.calls.length,before+4,'re-checking the same key asks again for nothing');
   r.ctx.location.hash='#k=fictional-key-two';r.ctx.takeKey();assert.match(r.mark.children[0]?.src||'',/^blob:/,'the same key does not retire its icons');
   const fresh2=rig();fresh2.ctx.APP_ICONS={batch:null,icons:{}};delete fresh2.ctx.WS;Object.assign(fresh2.ctx,{claiming:false,location:{hash:'#k=fictional-first',pathname:'/',search:''},history:{replaceState(){}},localStorage:{setItem(){}},URLSearchParams});vm.runInContext(take,fresh2.ctx);
   assert.equal(fresh2.ctx.takeKey(),true,'the first takeKey() works before the workspace state exists');assert.equal(fresh2.ctx.KEY,'fictional-first');}
  process.stdout.write('app marks: icon loader checks passed\n');
})().catch(e=>{console.error(e);process.exitCode=1;});
