// Existing Playwright and Chrome are prerequisites; this command installs nothing.
// Usage: node brand/tools/check-demo.cjs --baseline /path/to/baseline/src/web --output /path/to/private/report.json
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require('playwright');
const args = process.argv.slice(2);
const option = key => {const i=args.indexOf(key); return i<0?null:args[i+1];};
const baseline = option('--baseline'), output = option('--output');
if (!baseline || !output) throw Error('Provide --baseline and --output; no files are installed.');
const servers = new Set();
const candidate = path.resolve(option('--candidate') || path.resolve(__dirname, '../../src/web'));
async function serve(root) {
  root=path.resolve(root);
  const server=http.createServer((req,res)=>{
    let target;
    try {target=path.resolve(root,'.'+decodeURIComponent(new URL(req.url,'http://localhost').pathname));}
    catch {res.writeHead(400).end();return;}
    if(target===root) target=path.join(root,'index.html');
    if(!target.startsWith(root+path.sep)||!['GET','HEAD'].includes(req.method)||!fs.existsSync(target)||!fs.statSync(target).isFile()) {res.writeHead(404).end();return;}
    const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon'};
    res.setHeader('Content-Type',mime[path.extname(target)]||'application/octet-stream');
    res.end(req.method==='HEAD'?'':fs.readFileSync(target));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  servers.add(server);
  const port=server.address().port; assert.ok(![8793,18879,18959].includes(port));
  return {server,origin:`http://127.0.0.1:${port}`};
}
async function instrument(context) {
  await context.addInitScript(()=>{
    const effects=[];window.__demoEffects=effects;window.__demoIntervals=[];
    const blocked=name=>(...args)=>{effects.push({name,args:args.map(String)});throw Error('Demo effect refused: '+name);};
    window.fetch=blocked('fetch');window.EventSource=blocked('EventSource');window.WebSocket=blocked('WebSocket');
    XMLHttpRequest.prototype.open=blocked('XHR');navigator.sendBeacon=blocked('beacon');window.open=blocked('window.open');
    for(const name of ['setItem','removeItem','clear']) Storage.prototype[name]=blocked('storage.'+name);
    if(navigator.mediaDevices) navigator.mediaDevices.getUserMedia=blocked('microphone/camera');
    if(navigator.geolocation) for(const method of ['getCurrentPosition','watchPosition']) navigator.geolocation[method]=blocked('geolocation');
    if(navigator.requestMIDIAccess) navigator.requestMIDIAccess=blocked('MIDI permission');
    const NotificationMock=blocked('Notification');NotificationMock.permission='default';NotificationMock.requestPermission=blocked('notification permission');window.Notification=NotificationMock;
    for(const method of ['submit','requestSubmit']) HTMLFormElement.prototype[method]=blocked('form.'+method);
    const NativeDate=Date, fixed=Date.UTC(2026,9,6,12,0,0);
    window.Date=class extends NativeDate {constructor(...v){super(...(v.length?v:[fixed]));}static now(){return fixed;}};
    const interval=window.setInterval;
    window.setInterval=(fn,ms,...v)=>{window.__demoIntervals.push({fn,ms});return ms===3500?0:interval(fn,ms,...v);};
  });
}
async function inspect(browser,root,expected) {
  const host=await serve(root);let context;
  try {
  context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1});
  const requests=[],errors=[];
  await instrument(context);
  await context.route('**/*',async route=>{
    const r=route.request(),url=new URL(r.url());
    const relative=url.pathname==='/'?'index.html':decodeURIComponent(url.pathname).slice(1),asset=path.resolve(root,relative);
    const staticAsset=asset.startsWith(path.resolve(root)+path.sep)&&fs.existsSync(asset)&&fs.statSync(asset).isFile();
    if(!staticAsset||url.origin!==host.origin||!['GET','HEAD'].includes(r.method())||url.pathname.startsWith('/api/')||['/events','/ask','/pair'].includes(url.pathname)) {requests.push({url:r.url(),method:r.method()});await route.abort();}
    else await route.continue();
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(host.origin+'/?ui=workspace&demo');
    await page.waitForFunction(n=>document.querySelectorAll('#world .st').length===n,expected);
    await page.evaluate(()=>document.fonts.ready);
    const cast=await page.evaluate(()=>order.map(id=>({id,name:A[id].name,parent:A[id].parent||null,sub:!!A[id].sub,observed:!!A[id].observed,state:stateOf(A[id],Date.now()/1000).label||A[id].turn})));
    assert.equal(cast.length,expected);
    // These call the same page handlers as the controls, including simulated
    // answers. No stubs replace the product's demo guards.
    await page.evaluate(async()=>{
      document.querySelector('#notify').onclick();document.querySelector('#appr').onclick();here();keep();attention(Date.now()/1000);
      setFocus('c:demo-ada');setFocus(null);workspaceNewChatOpen(proj('c:demo-ada'));workspaceNewChatDialogClose();
      for(const question of ['What did I miss?','Anything stuck?','Who is waiting on me?','Draft a rule that flags the sessions migration as a deploy.']) await ofSendOrdinary(question);
      setFocus('c:demo-cleo');setFocus(null);
    });
    const effects=await page.evaluate(()=>window.__demoEffects);assert.deepEqual(effects,[],'demo browser side effects');assert.deepEqual(requests,[],'demo outbound requests');assert.deepEqual(errors,[],'page exceptions');
    // CPU time for the real scripted tick + render; frame delivery for the
    // same existing turn navigation. Callback intervals are captured, not rebuilt.
    const performance=await page.evaluate(async()=>{
      const tick=window.__demoIntervals.find(x=>x.ms===3500);if(!tick)throw Error('Missing real demo tick');
      const samples=[];
      for(let i=0;i<20;i++){
        const began=performance.now();tick.fn();render();const cpu=performance.now()-began;
        const nav=performance.now();turn(i%2?1:-1);
        await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);
        samples.push({cpuMs:cpu,navigationTwoFramesMs:performance.now()-nav});
      }
      return samples.slice(4);
    });
    assert.deepEqual(await page.evaluate(()=>window.__demoEffects),[],'no effects during benchmark');
    assert.deepEqual(requests,[],'no outbound requests during benchmark');
    return {expected,cast,effects,requests,errors,samples:performance};
  } finally {if(context)await context.close().catch(()=>{});host.server.closeAllConnections();await new Promise(resolve=>host.server.close(resolve));servers.delete(host.server);}
}
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  let timer;
  const job=(async()=>{
    const version=browser.version(),runs=[];
    // Paired order is reversed to expose warm-up/order effects rather than
    // presenting one favourable sample as a production speed guarantee.
    for(const order of [['baseline','candidate'],['candidate','baseline']])for(const label of order)runs.push({label,...await inspect(browser,label==='baseline'?baseline:candidate,label==='baseline'?7:22)});
    const summary={};for(const label of ['baseline','candidate']){const all=runs.filter(r=>r.label===label).flatMap(r=>r.samples);summary[label]={samples:all.length};for(const key of ['cpuMs','navigationTwoFramesMs']){const xs=all.map(x=>x[key]).sort((a,b)=>a-b);summary[label][key]={median:xs[Math.floor(xs.length/2)],p95:xs[Math.ceil(xs.length*.95)-1]};}}
    const report={browser:version,viewport:[1440,900],deviceScaleFactor:1,clock:'2026-10-06T12:00:00.000Z',method:'two reversed-order paired runs; automatic3500ms tick suppressed, real callback invoked per sample; four warmups omitted per run; CPU tick+render and two-rAF navigation latency, not FPS',summary,runs};
    fs.mkdirSync(path.dirname(path.resolve(output)),{recursive:true});fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify({browser:version,summary,effects:0,requests:0,report:output},null,2));
  })();
  try {await Promise.race([job,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Demo check operational deadline exceeded')),90_000);})]);}
  finally {clearTimeout(timer);await browser.close().catch(()=>{});for(const server of servers){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}await job.catch(()=>{});}
})().catch(error=>{console.error(error);process.exitCode=1;});
