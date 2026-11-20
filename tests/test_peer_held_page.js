'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const test=require('node:test');
const vm=require('node:vm');

const html=fs.readFileSync(require.resolve('../src/web/index.html'),'utf8');
let source=html.slice(html.indexOf('/* ---------- owner-only chats on hold ---------- */'),
  html.indexOf('/* end owner-only chats on hold */'));
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
if(process.env.MOTET_HOLD_TEST_MUTATION==='demo-start'){
  source=source.replace("const demo=DEMO&&new URLSearchParams", "const demo=new URLSearchParams");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='remote-start'){
  source=source.replace("if(!demo&&(DEMO||!local))return;", "if(!demo&&DEMO)return;");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='settings-name'){
  source=source.replace("name.append(wncEl('b','',item.name),wncEl('small','',item.provider));",
    "const chat=wncEl('b');chat.innerHTML=item.name;name.append(chat,wncEl('small','',item.provider));");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='bell-name'){
  source=source.replace("title.append(wncEl('b','',item.name),", "const chat=wncEl('b');chat.innerHTML=item.name;title.append(chat,");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='require-hold'){
  source=source.replace("confirm:'retire_unknown_without_resend',require_hold:true", "confirm:'retire_unknown_without_resend'");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='recovered-hold'){
  source=source.replace("else if(['running','submitted','reserved'].includes(value.execution))items.delete(item.id);",
    "else if(['running','submitted','reserved'].includes(value.execution))results.set(item.id,'still');");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='inspect-hold'){
  source=source.replace("request(ticket.key,'inspect',{...item.selector,require_hold:true})",
    "request(ticket.key,'inspect',item.selector)");
}
if(process.env.MOTET_HOLD_TEST_MUTATION==='unmatched-hold'){
  source=source.replace("items.set(item.id,{...item,group:'unavailable',clearable:false,held_since:null,",
    "items.set(item.id,{...item,group:'held',clearable:true,held_since:null,");
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
  assert.equal(t.controller.snapshot().items.size,2);
  assert.equal(t.row(3),undefined,'older servers cannot put open work in Connections');
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
      confirm:'retire_unknown_without_resend',require_hold:true});
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
  assert.equal(t.controller.snapshot().items.size,2);
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
  assert.equal(t.controller.snapshot().items.size,2);
  await t.controller.act(t.row(),'retire');
  assert.equal(t.controller.snapshot().results.get(t.row().id),'cleared');
  await t.controller.refresh();
  assert.equal(t.controller.snapshot().items.size,1);
  const another=await workspaceHeldChatsDemo()('fixture','held',{});
  assert.equal(another.items.length,2);
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

function heldPageDOM(hostname,search,records=[],key='fictional-owner',answers={}){
  const calls=[],timers=[],markup=[];
  class Element {
    constructor(tag){
      this.tagName=tag.toUpperCase();
      this.children=[];
      this.dataset={};
      this.attributes=new Map();
      this.classList={toggle(){}};
      this.parentElement=null;
      this.text='';
    }
    insertBefore(node,before){
      node.remove();
      this.children.splice(before?this.children.indexOf(before):this.children.length,0,node);
      node.parentElement=this;
    }
    append(...nodes){for(const node of nodes)this.insertBefore(node,null)}
    prepend(...nodes){for(const node of nodes.reverse())this.insertBefore(node,this.children[0])}
    remove(){
      const parent=this.parentElement;
      if(parent)parent.children.splice(parent.children.indexOf(this),1);
      this.parentElement=null;
    }
    contains(node){return node===this||this.children.some(child=>child.contains(node))}
    setAttribute(key,value){this.attributes.set(key,value)}
    get textContent(){return this.text+this.children.map(child=>child.textContent).join('')}
    set textContent(value){this.text=String(value);for(const child of [...this.children])child.remove()}
    set innerHTML(value){markup.push(String(value));this.textContent='';this.append(new Element('img'))}
  }
  const body=new Element('body'),pane=new Element('section');
  const nodes={};
  function node(id,tag,parent){
    const element=new Element(tag);nodes['#'+id]=element;parent.append(element);return element;
  }
  const host=node('workspaceHeldChats','div',pane);
  node('workspaceHeldStatus','p',host);node('workspaceHeldRetry','button',host);
  const section=node('workspaceHeldSection','section',host);section.hidden=true;
  const rows=node('workspaceHeldRows','div',section),bell=node('workspaceBellHeld','div',body);
  body.append(pane);
  const document={body,activeElement:body,hidden:false,createElement:tag=>new Element(tag),addEventListener(){}};
  const context={document,URLSearchParams,location:{hostname,search},locked:false,claiming:false,KEY:key,
    performance:{now:()=>1000},AbortSignal,sessionStorage:storage(),store:storage(),$:selector=>nodes[selector],
    txt:(node,value)=>{node.textContent=value},addEventListener(){},setInterval:callback=>timers.push(callback),
    clock:()=> '10:00 AM',workspaceNotificationMark:kind=>{const mark=new Element('i');mark.dataset.kind=kind;return mark},
    fetch:async(url,options)=>{
      calls.push({url,options});
      if(answers[url])return answers[url](options);
      return {ok:true,json:async()=>page(records)};
    }
  };
  const declarations=html.split('\n').filter(line=>line.startsWith('const DEMO=')||line.startsWith('const wncEl=')).join('\n');
  const api=vm.runInNewContext(declarations+'\n'+source+'\n({workspaceHeldChatsInit,workspaceHeldChatsRefresh,workspaceHeldChatsBellPaint,workspaceHeldChatsOwnerChanged})',context);
  context.workspaceBellPaint=()=>api.workspaceHeldChatsBellPaint(api.workspaceHeldChatsOwnerChanged.controller);
  api.workspaceHeldChatsInit();
  return {api,calls,timers,markup,section,rows,bell,pending:context.sessionStorage};
}

test('start-up requires demo opt-in and uses only localhost for authenticated reads',async()=>{
  const remote=heldPageDOM('room.example.test','?heldChats=1',[item()]);
  await remote.api.workspaceHeldChatsRefresh();
  assert.equal(remote.calls.length,0);
  assert.equal(remote.timers.length,0);
  assert.equal(remote.rows.children.length,0);
  for(const host of ['localhost','127.0.0.1']){
    const live=heldPageDOM(host,'?heldChats=1',[item(4,{name:'Fictional live chat'})]);
    await live.api.workspaceHeldChatsRefresh();
    assert.equal(live.calls.length,1,'only a real ?demo page uses fixture data');
    assert.equal(live.calls[0].url,'/bridge-admin/v1/held');
    assert.equal(live.calls[0].options.method,'POST');
    assert.equal(live.calls[0].options.headers['X-Live-Room-Key'],'fictional-owner');
    assert.match(live.rows.textContent,/Fictional live chat/);
  }
  const locked=heldPageDOM('localhost','?heldChats=1',[item()],'');
  await locked.api.workspaceHeldChatsRefresh();
  assert.equal(locked.calls.length,0);
  const ordinaryDemo=heldPageDOM('localhost','?demo');
  await ordinaryDemo.api.workspaceHeldChatsRefresh();
  assert.equal(ordinaryDemo.rows.children.length,0);
  assert.equal(ordinaryDemo.calls.length,0);
  const fixture=heldPageDOM('room.example.test','?demo&heldChats=1');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(fixture.rows.children.length,2);
  assert.equal(fixture.calls.length,0);
});

test('Connections and the bell render chat names literally and keep legacy open work off both',async()=>{
  const suspicious='<img src=x onerror=alert(1)>';
  const noMatch='Motet could not match this hold to a chat.';
  const retention='The message was kept for a day and no end was seen.';
  const rendered=heldPageDOM('localhost','',[
    item(1,{name:suspicious,reason:retention,reason_code:'codex_retention'}),
    item(2,{group:'unavailable',selector:null,agent:null,clearable:false,held_since:null,reason:noMatch,unavailable_reason:noMatch}),
    item(3,{group:'open',name:'Older open chat'})
  ]);
  await rendered.api.workspaceHeldChatsRefresh();
  assert.deepEqual(rendered.markup,[],'chat names must never reach an HTML parser');
  assert.equal(rendered.rows.children.length,2);
  assert.equal(rendered.bell.children.length,1);
  const [held,unavailable]=rendered.rows.children;
  assert.equal(held.children[0].children[0].textContent,suspicious);
  assert.equal(held.children[0].children[1].textContent,'Codex');
  assert.ok(held.textContent.includes(retention));
  assert.equal(unavailable.textContent.split(noMatch).length,2,'the reason appears exactly once');
  assert.match(unavailable.textContent,/Hold time unavailable/);
  assert.match(unavailable.textContent,/Can’t clear here/);
  const title=rendered.bell.children[0].children[1].children[0];
  assert.equal(title.children[0].textContent,suspicious);
  assert.equal(title.children[1].textContent,'Chat on hold');
  assert.ok(!rendered.rows.textContent.includes('Older open chat'));
  const empty=heldPageDOM('localhost','',[item(3,{group:'open'})]);
  await empty.api.workspaceHeldChatsRefresh();
  assert.equal(empty.section.hidden,true);
  assert.equal(empty.rows.children.length,0);
  assert.equal(empty.bell.children.length,0);
});

test('a recovered chat disappears after exact inspection with no second Clear or Cleared status',async()=>{
  const held=item();
  for(const execution of ['running','submitted','reserved']){
    const view=heldPageDOM('localhost','',[held],undefined,{
      '/bridge-admin/v1/retire':()=>{
        if(execution==='submitted')throw Error('Lost acknowledgement');
        return {ok:false,status:409};
      },
      '/bridge-admin/v1/inspect':()=>({ok:true,json:async()=>({...held.selector,agent:held.agent,
        execution,capture:'pending',eligible:execution!=='reserved'})})
    });
    await view.api.workspaceHeldChatsRefresh();
    const row=view.rows.children[0];
    const clear=row.children.find(child=>child.tagName==='BUTTON');
    assert.equal(clear.textContent,'Clear');
    assert.equal(view.bell.children.length,1);
    await clear.onclick();
    assert.equal(view.section.hidden,true);
    assert.equal(view.rows.children.length,0);
    assert.equal(view.bell.children.length,0);
    assert.equal(row._heldStatus.textContent,'');
    assert.equal(view.pending.getItem('motetHeldChatUnconfirmed:'+held.item_id),undefined);
    await clear.onclick();
    assert.deepEqual(view.calls.map(call=>call.url),[
      '/bridge-admin/v1/held','/bridge-admin/v1/retire','/bridge-admin/v1/inspect'
    ]);
    assert.deepEqual(JSON.parse(view.calls[1].options.body),{
      ...held.selector,agent:held.agent,confirm:'retire_unknown_without_resend',require_hold:true
    });
    assert.deepEqual(JSON.parse(view.calls[2].options.body),{...held.selector,require_hold:true});
  }
});

test('inspection failure allows read-only lookup, and an unverifiable hold cannot be tried again',async()=>{
  const held=item();
  let inspectCount=0;
  const view=heldPageDOM('localhost','',[held],undefined,{
    '/bridge-admin/v1/retire':()=>({ok:false,status:409}),
    '/bridge-admin/v1/inspect':()=>{
      if(++inspectCount===1)throw Error('Inspection unavailable');
      return {ok:true,json:async()=>({...held.selector,agent:held.agent,execution:'uncertain',capture:'pending',eligible:false})};
    }
  });
  await view.api.workspaceHeldChatsRefresh();
  const row=view.rows.children[0],action=()=>row.children.find(child=>child.tagName==='BUTTON');
  const clear=action();
  await clear.onclick();
  const check=action();
  assert.equal(check.textContent,'Check status');
  assert.equal(view.pending.getItem('motetHeldChatUnconfirmed:'+held.item_id),'1');

  await check.onclick();
  assert.equal(view.rows.children[0],row);
  assert.equal(view.section.hidden,false);
  assert.equal(action(),undefined);
  assert.equal(row._heldStatus.textContent,'');
  assert.match(row.textContent,/Can’t clear here/);
  assert.match(row.textContent,/Hold time unavailable/);
  assert.equal(row.textContent.split('Motet could not match this hold to a chat.').length,2);
  assert.equal(view.bell.children.length,0);
  assert.equal(view.pending.getItem('motetHeldChatUnconfirmed:'+held.item_id),undefined);
  await clear.onclick();
  await check.onclick();
  assert.deepEqual(view.calls.map(call=>call.url),[
    '/bridge-admin/v1/held','/bridge-admin/v1/retire','/bridge-admin/v1/inspect','/bridge-admin/v1/inspect'
  ]);
  for(const call of view.calls.slice(2)){
    assert.deepEqual(JSON.parse(call.options.body),{...held.selector,require_hold:true});
  }
});
