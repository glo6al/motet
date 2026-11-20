'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const test=require('node:test');
const vm=require('node:vm');

const html=fs.readFileSync(require.resolve('../src/web/index.html'),'utf8');
let source=html.slice(html.indexOf('/* ---------- owner-only chats on hold ---------- */'),
  html.indexOf('function workspaceHeldChatsOwnerChanged()'));
if(process.env.MOTET_HOLD_TEST_MUTATION==='exact'){
  source=source.replace('value?.[item.field]===item.selector[item.field]&&value.agent===item.agent','true');
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='generation'){
  source=source.replace('const current=ticket=>ownerChanged()===ticket','const current=ticket=>true');
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='episode'){
  source=source.replace("item.group!=='held'||!item.episode||!item.notice_due_at||now<item.notice_due_at",
    "!item.episode||!item.notice_due_at");
}
const {createWorkspaceHeldChats,workspaceHeldChatsDemo}=vm.runInNewContext(
  source+'({createWorkspaceHeldChats,workspaceHeldChatsDemo})',{fetch:()=>{throw Error('Unexpected network call')}});

const uuid=(number)=>'00000000-0000-4000-8000-'+String(number).padStart(12,'0');
function item(number=1,extra={}){
  return {item_id:String(number).padStart(64,'0'),selector:{request_id:uuid(number)},
    agent:'x:'+uuid(number+20),provider:'codex',name:'Elara',group:'held',clearable:true,
    created_at:520,held_since:600,notice_due_at:900,episode_id:uuid(number+10),
    reason:'Delivery or completion could not be verified.',...extra};
}
const page=(items,next_cursor=null,checked_at=1000)=>({items,next_cursor,checked_at});
const storage=()=>{const data=new Map();return {getItem:key=>data.get(key),setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)}};
function setup(respond,options={}){
  let key='fictional-owner',now=1000;
  const calls=[];
  const controller=createWorkspaceHeldChats({owner:()=>key,time:()=>now,storage:options.storage||storage(),
    pendingStorage:options.pendingStorage||storage(),changed(){},
    request:(key,action,body)=>{calls.push({key,action,body});return respond(action,body,key)}
  });
  const row=(number=1)=>controller.snapshot().items.get(item(number).item_id);
  return {controller,calls,row,key:value=>{key=value;controller.ownerChanged()},time:value=>{now=value}};
}
const actions=t=>t.calls.map(call=>call.action).join(',');

test('loads every page atomically, deduplicates overlap and keeps unverifiable rows',async()=>{
  const unavailable=item(2,{selector:null,agent:null,group:'unavailable',clearable:false,held_since:null});
  const stillOpen=item(3,{group:'open'});
  const t=setup((_action,body)=>body.cursor?page([item(),unavailable,stillOpen]):page([item()],'fixture-cursor'));
  await t.controller.refresh();
  assert.equal(actions(t),'held,held');
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.map(call=>call.body))),[{}, {cursor:'fixture-cursor'}]);
  assert.equal(t.controller.snapshot().items.size,3);
  assert.equal(t.row(2).held_since,null);
  assert.equal(t.controller.mode(t.row(2)),'unavailable');
  await t.controller.act(t.row(2),'retire');
  assert.equal(actions(t),'held,held');
});

test('failed traversal keeps a stale read visible and blocks actions',async()=>{
  let fail=false;
  const t=setup((_action,body)=>fail?(body.cursor?Promise.reject(Error('offline')):page([item(5)],'later')):page([item()]));
  await t.controller.refresh();fail=true;await t.controller.refresh();
  assert(t.controller.snapshot().stale);
  assert(t.row());
  assert.equal(t.row(5),undefined);
  await t.controller.act(t.row(),'retire');
  assert.equal(actions(t),'held,held,held');
  assert.equal(t.controller.notices().length,0);
});

test('retirement requires an exact response and preserves a confirmed result until refresh',async()=>{
  let removed=false;
  const record=item(2,{selector:{transport_id:uuid(30)}});
  const t=setup((action,body)=>{
    if(action==='held')return page(removed?[]:[record]);
    assert.equal(action,'retire');
    assert.deepEqual(JSON.parse(JSON.stringify(body)),{transport_id:uuid(30),agent:record.agent,
      confirm:'retire_unknown_without_resend'});
    removed=true;
    return {...record.selector,agent:record.agent,status:'retired',provider_outcome:'unknown'};
  });
  await t.controller.refresh();
  const row=t.row(2);
  await t.controller.act(row,'retire');
  assert.equal(t.controller.snapshot().results.get(row.id),'cleared');
  assert.equal(t.controller.snapshot().items.size,1);
  await t.controller.act(row,'retire');
  assert.equal(actions(t),'held,retire');
  await t.controller.refresh();
  assert.equal(t.controller.snapshot().items.size,0);
});

test('mismatched and lost responses use only exact inspection and show completion races',async()=>{
  const record=item();
  let phase=0;
  const t=setup((action)=>{
    if(action==='held')return page([record]);
    if(action==='retire')return {...record.selector,agent:'x:'+uuid(99),status:'retired',provider_outcome:'unknown'};
    return {...record.selector,agent:phase===0?'x:'+uuid(99):record.agent,execution:'completed',capture:'withheld'};
  });
  await t.controller.refresh();
  await t.controller.act(t.row(),'retire');
  assert.equal(t.controller.snapshot().results.size,0);
  assert.equal(t.controller.mode(t.row()),'inspect');
  await t.controller.act(t.row(),'retire');
  assert.equal(actions(t),'held,retire,inspect');
  phase=1;
  await t.controller.act(t.row(),'inspect');
  assert.equal(actions(t),'held,retire,inspect,inspect');
  assert.equal(t.controller.snapshot().results.get(t.row().id),'withheld');
  assert.equal(t.controller.notices().length,0);
});

test('a lost acknowledgement keeps the inspect requirement across a reload',async()=>{
  const data=storage(),record=item();
  const before=setup((action)=>action==='held'?page([record]):Promise.reject(Error('offline')),{pendingStorage:data});
  await before.controller.refresh();
  await before.controller.act(before.row(),'retire');
  const after=setup((action)=>action==='held'?page([record]):{...record.selector,agent:record.agent,execution:'retired'},{pendingStorage:data});
  await after.controller.refresh();
  assert.equal(after.controller.mode(after.row()),'inspect');
  await after.controller.act(after.row(),'inspect');
  assert.equal(actions(after),'held,inspect');
  assert.equal(after.controller.snapshot().results.get(after.row().id),'cleared');
});

test('lock invalidates an in-flight lookup and fences a pending write',async()=>{
  const record=item();
  let resolveRead,resolveWrite,readDeferred=true;
  const t=setup((action)=>{
    if(action==='retire')return new Promise(resolve=>{resolveWrite=resolve});
    if(readDeferred)return new Promise(resolve=>{resolveRead=resolve});
    return page([record]);
  });
  const first=t.controller.refresh();
  t.key(null);readDeferred=false;t.key('another-owner');
  await t.controller.refresh();
  resolveRead(page([item(12)]));await first;
  assert(t.row());
  assert.equal(t.row(12),undefined);
  const old=t.controller.act(t.row(),'retire');
  t.key(null);t.key('another-owner');await t.controller.refresh();
  await t.controller.act(t.row(),'inspect');
  assert.equal(actions(t),'held,held,retire,held');
  resolveWrite({...record.selector,agent:record.agent,status:'retired',provider_outcome:'unknown'});await old;
  assert.equal(t.controller.snapshot().results.size,0);
  assert.equal(t.controller.mode(t.row()),'inspect');
});

test('bell waits five minutes, excludes open work, and persists only episode dismissal',async()=>{
  const data=storage(),records=[item(),item(2,{group:'open'}),item(3,{held_since:null})];
  const t=setup(()=>page(records,null,899),{storage:data});
  await t.controller.refresh();
  assert.equal(t.controller.notices().length,0);
  t.time(1001);
  assert.equal(t.controller.notices().map(x=>x.id).join(','),t.row().id);
  t.controller.dismiss(t.row().episode);
  assert.equal(t.controller.notices().length,0);
  assert.equal(t.controller.snapshot().items.size,3);
  assert.equal(actions(t),'held');
  const reload=setup(()=>page(records),{storage:data});
  await reload.controller.refresh();
  assert.equal(reload.controller.notices().length,0);
  records[0]=item(1,{episode_id:uuid(99)});
  await reload.controller.refresh();
  assert.equal(reload.controller.notices().length,1);
});

test('demo clear changes only the disposable fixture',async()=>{
  const request=workspaceHeldChatsDemo();
  const t=setup((action,body,key)=>request(key,action,body));
  await t.controller.refresh();
  assert.equal(t.controller.snapshot().items.size,3);
  await t.controller.act(t.row(),'retire');
  assert.equal(t.controller.snapshot().results.get(t.row().id),'cleared');
  await t.controller.refresh();
  assert.equal(t.controller.snapshot().items.size,2);
  const another=await workspaceHeldChatsDemo()('fixture','held',{});
  assert.equal(another.items.length,3);
});

test('initial unlock links can change the key before the workspace controller exists',()=>{
  const keySource=html.slice(html.indexOf('function takeKey(){'),html.indexOf('const post='));
  const ownerSource=html.slice(html.indexOf('function workspaceHeldChatsOwnerChanged(){'),
    html.indexOf('function workspaceHeldChatsRefresh(){'));
  const context={URLSearchParams,location:{hash:'#k=fictional-unlock',pathname:'/',search:''},
    history:{replaceState(){}},addEventListener(){},localStorage:storage(),KEY:'',DEMO:false,
    workspaceAppIconsReset(){}};
  vm.runInNewContext(keySource+ownerSource,context);
  assert.equal(context.KEY,'fictional-unlock');
  assert.equal(context.localStorage.getItem('liveRoomKey'),'fictional-unlock');
});
