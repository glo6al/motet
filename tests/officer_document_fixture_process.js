'use strict';
// Disposable test-process lifecycle only. Readiness remains the real fixture's flushed URL.
const assert=require('node:assert/strict'),{spawn}=require('node:child_process'),{EventEmitter}=require('node:events'),path=require('node:path');
const OWNER_STDERR_LIMIT = 10000;
const ownerNow = () => performance.now();
function scrub(value){let text=String(value);for(const prefix of [path.resolve(__dirname,'..'),require('node:os').homedir(),require('node:os').tmpdir()].sort((a,b)=>b.length-a.length))if(prefix.length>1)text=text.split(prefix).join('<fixture-path>');return text;}

function ownerFixtureError(reason,args,started,now,readStderr) {
  const elapsed = Math.max(0,Math.round(now()-started));
  const stderr = scrub(readStderr().slice(-OWNER_STDERR_LIMIT)).slice(-OWNER_STDERR_LIMIT).trim() || '<empty>';
  return Error(`Disposable owner HTTP ${scrub(reason)} after ${elapsed} ms. args=${scrub(JSON.stringify(args))} stderr=${stderr}`);
}

function waitForOwnerReady(child,args,{now=ownerNow,started=now(),readStderr=()=>'',startupMs=10000,
  setTimer=setTimeout,clearTimer=clearTimeout}={}) {
  return new Promise((resolve,reject)=>{
    let stdout='';
    const settle=(reason,origin)=>{
      clearTimer(timer);
      child.off('error',onError);
      child.off('close',onClose);
      child.stdout.off('data',onStdout);
      if(reason)reject(ownerFixtureError(reason,args,started,now,readStderr));
      else resolve(origin);
    };
    const onError=error=>settle(`startup spawn failed (${error.message})`);
    // Unlike exit, close follows the last bytes from the child pipes.
    const onClose=(code,signal)=>settle(`startup closed (code=${code}, signal=${signal})`);
    const onStdout=bytes=>{
      stdout+=bytes;
      const match=stdout.match(/OFFLINE fixture URL: (http:\/\/127\.0\.0\.1:\d+)\//);
      if(match)settle(null,match[1]);
      else stdout=stdout.slice(-1024);
    };
    const timer=setTimer(()=>settle('startup timed out'),Math.max(0,startupMs-(now()-started)));
    child.on('error',onError);
    child.on('close',onClose);
    child.stdout.on('data',onStdout);
  });
}

// Windows has no Ctrl-C for a child: Node's SIGINT there kills it outright, so the fixture is asked to stop through
// its standard input instead and still removes its temporary state. Elsewhere it gets SIGINT, as before.
const WINDOWS=process.platform==='win32';
const PYTHON=WINDOWS?'python':'python3';
const stopFixture=WINDOWS?child=>{child.stdin.end('stop\n');}:child=>child.kill('SIGINT');

async function ownerFixture(run,args=[],{model='claude-haiku-5-5',spawnChild=spawn,now=ownerNow,setTimer=setTimeout,clearTimer=clearTimeout,cleanupMs=5000,killWaitMs=1000,stop=stopFixture,stdinStop=WINDOWS}={}) {
  const variant=['--model',model,'--trace-startup',...args,...(stdinStop?['--stop-on-stdin']:[])],started=now();let child;
  try{child=spawnChild(PYTHON,[path.join(__dirname,'officer_document_live_fixture.py'),...variant],{env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},stdio:[stdinStop?'pipe':'ignore','pipe','pipe']});}
  catch(error){throw ownerFixtureError(`startup spawn failed (${error.message})`,variant,started,now,()=>'');}
  let stderr='',primaryFailed=false,childClosed=false;const readStderr=()=>stderr;
  const appendStderr=bytes=>{stderr=(stderr+bytes).slice(-OWNER_STDERR_LIMIT);};child.stderr.on('data',appendStderr);
  const onChildError=()=>{};child.on('error',onChildError);
  let finish;const closed=new Promise(resolve=>{finish=resolve});const onClose=()=>{childClosed=true;finish();};child.once('close',onClose);
  const within=async ms=>{let timer;try{return await Promise.race([closed.then(()=>true),new Promise(resolve=>{timer=setTimer(()=>resolve(false),ms)})]);}finally{clearTimer(timer);}};
  try{const origin=await waitForOwnerReady(child,variant,{started,now,readStderr,setTimer,clearTimer});assert.equal(new URL(origin).hostname,'127.0.0.1');return await run(origin);}
  catch(error){primaryFailed=true;throw error;}
  finally{try{
    let signalError;
    if(!childClosed&&child.pid&&child.exitCode===null&&child.signalCode===null)try{stop(child)}catch(error){signalError=error;}
    if(!await within(cleanupMs)){if(child.pid)try{child.kill('SIGKILL')}catch(error){signalError=error;}const reaped=await within(killWaitMs);throw ownerFixtureError(`cleanup timed out (SIGKILL close observed=${reaped}${signalError?', signal error='+signalError.message:''})`,variant,started,now,readStderr);}
    if(signalError)throw ownerFixtureError('cleanup signal failed ('+signalError.message+')',variant,started,now,readStderr);
  }catch(error){if(!primaryFailed)throw error;}
  finally{child.stderr.off('data',appendStderr);child.off('error',onChildError);child.off('close',onClose);}}
}

async function ownerStartupChecks({causals=true}={}) {
  const args=['--seed-file','--seed-count','51','--trace-startup'];
  function fake() {
    const child=new EventEmitter();
    child.stdout=new EventEmitter();
    const started=120;
    let wall=started,stderr='',deadline;
    const timers=new Map();
    const pending=waitForOwnerReady(child,args,{started,now:()=>wall,readStderr:()=>stderr,
      setTimer:(callback,delay)=>{deadline=delay;timers.set('startup',callback);return 'startup';},
      clearTimer:id=>timers.delete(id)});
    assert.equal(deadline,10000);
    return {child,pending,setTime:elapsed=>{wall=started+elapsed;},setStderr:tail=>{stderr=tail;},
      timeout:()=>timers.get('startup')(),assertClean:()=>{
        assert.equal(timers.size,0);
        assert.equal(child.listenerCount('error'),0);
        assert.equal(child.listenerCount('close'),0);
        assert.equal(child.stdout.listenerCount('data'),0);
      }};
  }

  const timeout=fake();
  timeout.setTime(10007);
  timeout.setStderr('omitted prefix '+ 'x'.repeat(10000)+'stderr tail');
  timeout.timeout();
  await assert.rejects(timeout.pending,error=>error.message.includes('startup timed out after 10007 ms')&&
    error.message.includes(JSON.stringify(args))&&error.message.endsWith('stderr tail')&&!error.message.includes('omitted prefix'));
  timeout.assertClean();

  const early=fake();
  early.setTime(42);
  early.child.emit('exit',3);
  early.setStderr('late stderr drained');
  early.child.emit('close',3,null);
  await assert.rejects(early.pending,/startup closed \(code=3, signal=null\) after 42 ms.*late stderr drained/);
  early.assertClean();

  const missing=fake();
  missing.setTime(19);
  missing.child.emit('error',Error('spawn python3 ENOENT'));
  await assert.rejects(missing.pending,/startup spawn failed \(spawn python3 ENOENT\) after 19 ms.*stderr=<empty>/);
  missing.assertClean();

  const ready=fake();
  ready.child.stdout.emit('data',Buffer.from('OFFLINE fixture URL: http://127.0.'));
  ready.child.stdout.emit('data',Buffer.from('0.1:42017/#fictional\n'));
  assert.equal(await ready.pending,'http://127.0.0.1:42017');
  ready.assertClean();
  const privateTail=path.resolve(__dirname,'..')+'/fictional.py '+require('node:os').homedir()+'/fictional';
  const safe=ownerFixtureError('startup timed out',args,0,()=>10,()=>privateTail).message;
  assert(!safe.includes(path.resolve(__dirname,'..'))&&!safe.includes(require('node:os').homedir()),'known private prefixes are redacted');
  function lifecycle(mode){const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.pid=42;child.exitCode=null;child.signalCode=null;const signals=[];let reaped=false,observed=false;child.kill=signal=>{signals.push(signal);if(mode==='kill-error'&&signal==='SIGINT')throw Error('fictional cleanup signal error');if(mode==='forced'&&signal==='SIGINT')return true;queueMicrotask(()=>{reaped=true;observed=child.listenerCount('close')>0;child.emit('close',0,signal)});return true;};queueMicrotask(()=>child.stdout.emit('data','OFFLINE fixture URL: http://127.0.0.1:42017/#fictional\n'));return {child,signals,reaped:()=>reaped,observed:()=>observed};}
  // The cleanup order under test is the signal one on every system (the fakes take signals, not standard input).
  const signalled={stop:child=>child.kill('SIGINT'),stdinStop:false};
  const success=lifecycle('success');assert.equal(await ownerFixture(async()=>17,[],{...signalled,spawnChild:()=>success.child}),17);assert(success.reaped(),'callback success waits for close');assert.equal(success.child.stderr.listenerCount('data'),0);assert.equal(success.child.listenerCount('close'),0);
  const failure=lifecycle('kill-error'),primary=Error('fictional callback failure');await assert.rejects(()=>ownerFixture(async()=>{throw primary},[],{...signalled,spawnChild:()=>failure.child,cleanupMs:0}),error=>error===primary,'cleanup cannot replace callback failure');assert(failure.observed(),'signal failure still waits for observed close');assert.deepEqual(failure.signals,['SIGINT','SIGKILL'],'failed first signal still reaches owned fallback');
  const forced=lifecycle('forced');await assert.rejects(()=>ownerFixture(async()=>{},[],{...signalled,spawnChild:()=>forced.child,cleanupMs:0}),/cleanup timed out \(SIGKILL close observed=true\)/);assert.deepEqual(forced.signals,['SIGINT','SIGKILL']);assert(forced.reaped(),'SIGKILL is followed by observed close');
  if(causals){const Module=require('node:module'),source=require('node:fs').readFileSync(__filename,'utf8');for(const [before,after]of [["scrub(readStderr().slice(-OWNER_STDERR_LIMIT)).slice(-OWNER_STDERR_LIMIT)","''"],["child.on('close',onClose)","child.on('exit',onClose)"],["clearTimer(timer);","void timer;"]]){assert(source.includes(before));const mutant=new Module(__filename,module);mutant.filename=__filename;mutant.paths=module.paths;mutant._compile(source.replace(before,after),__filename);await assert.rejects(()=>mutant.exports.ownerStartupChecks({causals:false}),{name:'AssertionError'},'startup diagnostic causal guard');}}
  console.log('ok fixture startup diagnostics and listener cleanup');
}

module.exports={ownerFixture,waitForOwnerReady,ownerStartupChecks,PYTHON,stopFixture,WINDOWS};
