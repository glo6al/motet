'use strict';
/* Actual-browser proof, deliberately runtime-injected: no browser dependency or launch here.
   A caller supplies a fresh, isolated Puppeteer-compatible page with evaluate/setContent/waitForFunction, keyboard.press, mouse.click, viewport and setViewport.
   Run from the repository root with an installed puppeteer-core and CHROME_BIN:
   node -e 'const p=require("puppeteer-core"),fs=require("node:fs");(async()=>{const b=await p.launch({executablePath:process.env.CHROME_BIN,headless:true});try{await require("./tests/workspace_focus_browser.js").runWorkspaceFocusBrowserTests(await b.newPage(),fs.readFileSync("src/web/index.html","utf8"))}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1})'
   Real Chrome is required; VM/fake DOM tests cannot measure UA styles or painted contrast.
   This module changes focus and its own fixture only; use a separate page from a running application. */
const assert = require('node:assert/strict');
const FALLBACK = ':where(html.workspace *):focus-visible{outline:1px solid rgb(28,27,25);outline-offset:2px}';
function focusRGBA(value) {
  if(value==='transparent')return [0,0,0,0];
  const match=String(value).match(/^rgba?\((.*)\)$/i);assert(match,'unmeasurable color: '+value);
  const parts=match[1].replace(/,/g,' ').replace(/\//g,' ').trim().split(/\s+/);assert(parts.length===3||parts.length===4,'unmeasurable color: '+value);
  const rgba=parts.map((v,i)=>v.endsWith('%')?Number(v.slice(0,-1))/100*(i===3?1:255):Number(v));if(rgba.length===3)rgba.push(1);
  assert(rgba.every((v,i)=>Number.isFinite(v)&&v>=0&&v<=(i===3?1:255)),'unmeasurable color: '+value);return rgba;
}
function focusComposite(fg,bg) {const a=fg[3]+bg[3]*(1-fg[3]);return [...[0,1,2].map(i=>a?(fg[i]*fg[3]+bg[i]*bg[3]*(1-fg[3]))/a:0),a];}
function focusBlue(rgba) {const [r,g,b]=rgba,max=Math.max(r,g,b),min=Math.min(r,g,b),d=max-min;if(d<12||d/max<0.05)return false;let h=max===r?((g-b)/d)%6:max===g?(b-r)/d+2:(r-g)/d+4;h=(h*60+360)%360;return h>=170&&h<=270;}
function focusLuminance(rgb) {return rgb.slice(0,3).reduce((sum,v,i)=>{v/=255;return sum+[0.2126,0.7152,0.0722][i]*(v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4)},0);}
function focusContrast(a,b) {const x=focusLuminance(a),y=focusLuminance(b);return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05);}
function focusMeasurement(row) {
  assert(!row.settleTimedOut,'unmeasurable focus transition did not settle');
  assert(row.borderImageSource==='none','unmeasurable border image');
  assert(!row.effects?.length,'unmeasurable focus effects: '+JSON.stringify(row.effects));
  const offset=Number.parseFloat(row.offset),width=Number.parseFloat(row.width);assert(Number.isFinite(offset)&&Number.isFinite(width),'unmeasurable outline geometry');
  assert(!(offset<0&&offset+width>0),'unmeasurable outline crosses element border');
  assert(!row.modalOutside,'unmeasurable outside modal outline: real backdrop/underlying surface required');
  if(offset<0)assert(row.backgroundClip==='border-box'&&row.borderWidths.every(v=>Number.isFinite(Number.parseFloat(v))),'unmeasurable inset background clip');
  if(offset<0&&row.backdrop)assert(-offset-width>Math.max(...row.borderWidths.map(Number.parseFloat)),'unmeasurable modal inset must be beyond its border');
  const resolve=(entries,point)=>{const layers=[];let opaque=false;
    for(const layer of entries||[]){if(point){const r=layer.rect;assert(r&&point.x>=r.left&&point.x<r.right&&point.y>=r.top&&point.y<r.bottom,'unmeasurable hit does not paint sample point');}assert(!layer.effects?.length,'unmeasurable sampled paint effects');assert(layer.image==='none','unmeasurable background image: '+layer.image);const color=focusRGBA(layer.color);layers.push({...layer,rgba:color});if(color[3]===1){opaque=true;break;}}
    assert(opaque,'unmeasurable focus background: no opaque paint');let background=layers.at(-1).rgba;for(let i=layers.length-2;i>=0;i--)background=focusComposite(layers[i].rgba,background);return {background,layers};};
  let surfaces,layers,background;
  if(offset>=0){assert(!row.boxShadow||row.boxShadow==='none','unmeasurable outside outline box shadow');assert(row.outsideSamples?.length===12,'unmeasurable outside outline: twelve painted witnesses required');
    for(const side of ['top','right','bottom','left'])assert(row.outsideSamples.filter(s=>s.side===side).length===3,'unmeasurable missing outline side');
    surfaces=row.outsideSamples.map(point=>{assert(!point.error,'unmeasurable outside paint: '+point.error+' ['+(row.id||row.tag||'unnamed')+']');const value=resolve(point.layers,point);return {role:point.side,x:point.x,y:point.y,...value};});background=surfaces[0].background;layers=surfaces.flatMap(s=>s.layers);
  }else{const value=resolve(row.backgrounds);background=value.background;layers=value.layers;surfaces=[{role:'fill',background}];}
  if(offset<0)for(let i=0;i<4;i++)if(Number.parseFloat(row.borderWidths[i])>-offset-width){assert(row.borderStyles?.[i]==='solid','unmeasurable non-solid overlapped border');surfaces.push({role:'border-'+i,background:focusComposite(focusRGBA(row.borderColors[i]),background)});}
  const samples=surfaces.map(s=>{const outline=focusComposite(focusRGBA(row.color),s.background);return {...s,outline,contrast:focusContrast(outline,s.background)}}),sides=samples.map(s=>Math.sign(focusLuminance(s.outline)-focusLuminance(s.background)));
  assert(!sides.some(s=>s>0)||!sides.some(s=>s<0),'unmeasurable border and fill straddle outline luminance');
  return {background,outline:samples[0].outline,contrast:Math.min(...samples.map(s=>s.contrast)),samples,layers,basis:offset<0?'self':'painted hit stack'};
}
async function auditWorkspaceFocus(page) {
  await page.keyboard.press('Tab'); // Real keyboard modality; programmatic focus must not fake :focus-visible.
  const result = await page.evaluate(async () => {
    if (!document.documentElement.classList.contains('workspace')) throw Error('Workspace document required');
    async function settle(target){
      const deadline=performance.now()+1200;
      const bounded=promise=>new Promise(resolve=>{const remaining=deadline-performance.now();if(remaining<=0){resolve(false);return;}let done=false;const timer=setTimeout(()=>{done=true;resolve(false)},remaining);promise.then(()=>{if(!done){done=true;clearTimeout(timer);resolve(true)}});});
      const paints=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      if(!await bounded(paints()))return false;
      while(true){const animations=[];if(target){for(let n=target;n;n=n.parentElement)animations.push(...n.getAnimations());}else animations.push(...document.getAnimations());const finite=animations.filter(a=>a.playState==='running'&&a.effect?.getTiming().iterations!==Infinity);if(!finite.length)return true;if(!await bounded(Promise.all(finite.map(a=>a.finished.catch(()=>{})))))return false;if(!await bounded(paints()))return false;}
    }
    const rows = [], skipped = [], before = document.activeElement;
    if(!await settle(null))return {rows,skipped,initialSettleTimedOut:true};
    // Probe every rendered HTMLElement. This discovers browser-focusable scroll containers even
    // when tabIndex is -1 and there is no tabindex attribute. Do not manufacture focusability.
    for (const el of Array.from(document.querySelectorAll('*'))) {
      if (!(el instanceof HTMLElement)) continue;
      const identity = {tag:el.tagName.toLowerCase(),id:el.id,tabIndex:el.tabIndex,tabindex:el.getAttribute('tabindex')};
      const style = getComputedStyle(el);
      const reason = !el.isConnected?'disconnected':el.closest('[hidden],[inert]')?'hidden/inert':!el.getClientRects().length||style.display==='none'||style.visibility!=='visible'?'not rendered':el.matches(':disabled')?'disabled':null;
      if (reason) { skipped.push({...identity,reason}); continue; }
      el.focus({preventScroll:true});
      if (document.activeElement!==el) { skipped.push({...identity,reason:document.querySelector('dialog:modal')?'browser rejected focus while a modal is open':'not browser-focusable'}); continue; }
      if (!el.matches(':focus-visible')) { skipped.push({...identity,reason:'not focus-visible'}); continue; }
      const settleTimedOut=!await settle(el);
      if(document.activeElement!==el||!el.isConnected||!el.matches(':focus-visible')){skipped.push({...identity,reason:'focus changed during settlement'});continue;}
      const focused = getComputedStyle(el);
      const offset=parseFloat(focused.outlineOffset),backgrounds=[],effects=[];
      for(let n=el;n;n=n.parentElement){const t=getComputedStyle(n);if(Number(t.opacity)!==1||t.filter!=='none'||t.backdropFilter!=='none'||t.mixBlendMode!=='normal')effects.push({tag:n.tagName,id:n.id,opacity:t.opacity,filter:t.filter,backdropFilter:t.backdropFilter,blend:t.mixBlendMode});}
      if(offset<0)for(let n=el;n;n=n.parentElement){const t=getComputedStyle(n);backgrounds.push({tag:n.tagName.toLowerCase(),id:n.id,color:t.backgroundColor,image:t.backgroundImage});}
      const outsideSamples=[];
      if(offset>=0){const rect=el.getBoundingClientRect(),distance=offset+parseFloat(focused.outlineWidth)/2;
        let geometryError='';for(const value of [focused.borderTopLeftRadius,focused.borderTopRightRadius,focused.borderBottomRightRadius,focused.borderBottomLeftRadius]){const parts=value.split(/\s+/);const x=parseFloat(parts[0]),y=parseFloat(parts[1]||parts[0]);if(value.includes('%')||!Number.isFinite(x)||!Number.isFinite(y)||x>rect.width/4||y>rect.height/4)geometryError='curved outline quarter-point geometry';}for(let n=el;n;n=n.parentElement){const t=getComputedStyle(n);if(t.transform!=='none'){const m=new DOMMatrixReadOnly(t.transform);if(!m.is2D||m.a!==1||m.b!==0||m.c!==0||m.d!==1)geometryError='transformed outline geometry';}}
        for(const side of ['top','right','bottom','left'])for(const fraction of [.25,.5,.75]){
          const x=side==='left'?rect.left-distance:side==='right'?rect.right+distance:rect.left+rect.width*fraction,y=side==='top'?rect.top-distance:side==='bottom'?rect.bottom+distance:rect.top+rect.height*fraction;
          const point={side,fraction,x,y,layers:[],error:geometryError};
          if(x<0||y<0||x>=innerWidth||y>=innerHeight)point.error='sample clipped by viewport';
          if(!point.error)for(let n=el.parentElement;n;n=n.parentElement){const t=getComputedStyle(n),r=n.getBoundingClientRect();if(t.clipPath!=='none'||t.maskImage!=='none')point.error='clipped or masked outline';if(t.overflowX!=='visible'&&(x<r.left+n.clientLeft||x>=r.left+n.clientLeft+n.clientWidth)||t.overflowY!=='visible'&&(y<r.top+n.clientTop||y>=r.top+n.clientTop+n.clientHeight))point.error='sample clipped by ancestor';}
          if(!point.error){const hits=document.elementsFromPoint(x,y),seen=new Set(),inside=document.elementsFromPoint(Math.max(rect.left+.5,Math.min(rect.right-.5,x)),Math.max(rect.top+.5,Math.min(rect.bottom-.5,y))),targetIndex=inside.findIndex(n=>n===el||el.contains(n));
            if(targetIndex<0)point.error='target is occluded or not hit-testable';
            for(const hit of hits){if(hit===el||el.contains(hit)||seen.has(hit))continue;seen.add(hit);const boxes=Array.from(hit.getClientRects()),box=boxes.find(r=>x>=r.left&&x<r.right&&y>=r.top&&y<r.bottom);if(!box)continue;
              if(!hit.contains(el)&&inside.indexOf(hit)<=targetIndex){point.error='foreign paint is not witnessed below target';break;}
              const t=getComputedStyle(hit),paintEffects=[];const widths=[t.borderTopWidth,t.borderRightWidth,t.borderBottomWidth,t.borderLeftWidth].map(parseFloat);if(t.backgroundClip!=='border-box')point.error='unsupported sampled background clip';if(widths.some(v=>!Number.isFinite(v))||x<box.left+widths[3]||x>=box.right-widths[1]||y<box.top+widths[0]||y>=box.bottom-widths[2])point.error='sample intersects foreign border paint';for(let n=hit;n;n=n.parentElement){const q=getComputedStyle(n);if(Number(q.opacity)!==1||q.filter!=='none'||q.backdropFilter!=='none'||q.mixBlendMode!=='normal')paintEffects.push({id:n.id,opacity:q.opacity,filter:q.filter,backdropFilter:q.backdropFilter,blend:q.mixBlendMode});}
              point.layers.push({tag:hit.tagName.toLowerCase(),id:hit.id,color:t.backgroundColor,image:t.backgroundImage,backgroundClip:t.backgroundClip,effects:paintEffects,rect:{left:box.left,right:box.right,top:box.top,bottom:box.bottom}});
              if(t.backgroundImage!=='none'||!/^rgba\([^)]*,\s*0\)$/.test(t.backgroundColor)&&!t.backgroundColor.startsWith('rgba'))break;
            }
            // Hit testing omits pointer-events:none paint. Such covering backgrounds cannot be guessed away.
            for(const n of document.querySelectorAll('*')){if(n===el||el.contains(n)||seen.has(n))continue;const t=getComputedStyle(n);if(t.pointerEvents!=='none'||t.display==='none'||t.visibility!=='visible')continue;const r=n.getBoundingClientRect();if(x>=r.left&&x<r.right&&y>=r.top&&y<r.bottom&&(t.backgroundImage!=='none'||!/^rgba\([^)]*,\s*0\)$/.test(t.backgroundColor)))point.error='unresolved non-hit-tested paint';}
          }outsideSamples.push(point);
        }
      }
      rows.push({...identity,color:focused.outlineColor,style:focused.outlineStyle,width:focused.outlineWidth,offset:focused.outlineOffset,boxShadow:focused.boxShadow,backgrounds,outsideSamples,effects,settleTimedOut,borderImageSource:focused.borderImageSource,borderColors:[focused.borderTopColor,focused.borderRightColor,focused.borderBottomColor,focused.borderLeftColor],borderStyles:[focused.borderTopStyle,focused.borderRightStyle,focused.borderBottomStyle,focused.borderLeftStyle],backgroundClip:focused.backgroundClip,borderWidths:[focused.borderTopWidth,focused.borderRightWidth,focused.borderBottomWidth,focused.borderLeftWidth],modalOutside:offset>=0&&el.matches('dialog:modal'),backdrop:el.matches('dialog:modal')?{color:getComputedStyle(el,'::backdrop').backgroundColor,image:getComputedStyle(el,'::backdrop').backgroundImage}:null});
    }
    before?.focus?.({preventScroll:true});
    return {rows,skipped};
  });
  assert(!result.initialSettleTimedOut,'unmeasurable initial document transition did not settle');
  assert(result.rows.length>0,'actual browser focus walk must not be vacuous');
  for (const row of result.rows) {
    const drawn = !['none','hidden'].includes(row.style)&&parseFloat(row.width)>0;
    if(!drawn)continue;
    const measurement=focusMeasurement(row);Object.assign(row,{measurement});
    assert(!focusBlue(measurement.outline),'blue keyboard outline: '+JSON.stringify(row));
    assert(measurement.contrast>=3,'low-contrast keyboard outline: '+JSON.stringify(row));
  }
  return result;
}
async function runWorkspaceFlagFocusTests(page,css) {
  const rule='html.workspace[data-theme=original] .st.idle>.flag.r:has(:focus-visible){opacity:1}';
  const mount=async(theme,workspace=true,sheet=css)=>page.setContent('<!doctype html><html '+(workspace?'class="workspace"':'')+' data-theme="'+theme+'"><head><style>'+sheet+'</style></head><body><button id="flag-before" style="position:absolute;top:8px;left:8px;z-index:9999;outline-color:var(--ink)">Outside</button><div class="st idle" style="position:relative;left:40px;top:70px;transform:none;width:300px"><div class="flag r" id="focus-flag"><button id="flag-target">Flag action</button></div></div></body></html>');
  const settled=async()=>{await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));await page.waitForFunction(()=>!document.getAnimations().some(a=>a.playState==='running'&&a.effect?.getTiming().iterations!==Infinity),{timeout:1200});};
  const state=()=>page.evaluate(()=>({opacity:getComputedStyle(document.getElementById('focus-flag')).opacity,active:document.activeElement.id,visible:document.getElementById('flag-target').matches(':focus-visible')}));
  const keyboard=async expected=>{await page.keyboard.press('Tab');await page.keyboard.press('Tab');await settled();const value=await state();assert.equal(value.active,'flag-target');assert.equal(value.visible,true);assert.equal(value.opacity,expected,'actual keyboard flag opacity');return value;};
  await mount('original');const original=await keyboard('1');await page.evaluate(()=>document.activeElement.blur());await settled();assert.equal((await state()).opacity,'0.5','blur restores idle dimming');
  await mount('original');const point=await page.evaluate(()=>{const r=document.getElementById('flag-target').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}});await page.mouse.click(point.x,point.y);await settled();const pointer=await state();assert.equal(pointer.active,'flag-target');assert.equal(pointer.visible,false,'real pointer-only button focus is not focus-visible');assert.equal(pointer.opacity,'0.5','pointer-only focus keeps idle dimming');
  await mount('neutral');const neutral=await keyboard('0.5');await mount('original',false);const classic=await keyboard('0.5');
  assert(css.includes(rule));await mount('original',true,css.replace(rule,''));await assert.rejects(()=>keyboard('1'),{name:'AssertionError'},'same behavior assertion detects removal of flag focus rule');
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable focus effects:.*focus-flag/},'missing flag rule restores strict opacity uncertainty at the flag descendant');
  return {original,pointer,neutral,classic,ruleDeletion:'rejected by same keyboard assertion and strict opacity audit'};
}
async function runWorkspacePromptKeylineTests(page,css) {
  const declaration='outline-color:rgb(232,233,236);box-shadow:0 0 0 5px rgb(7,9,13)';
  const previous=page.viewport();await page.setViewport({width:1440,height:900});
  const mount=async(background,theme='original',workspace=true,sheet=css)=>page.setContent('<!doctype html><html '+(workspace?'class="workspace"':'')+' data-theme="'+theme+'"><head><style>'+sheet+'</style></head><body><div id="room" style="position:fixed;inset:0;background:'+background+';background-image:none"></div><div id="officer" data-workspace-composer="desktop" style="position:fixed;left:80px;top:240px;right:auto;bottom:auto;width:700px;height:100px;background:white;transform:none;opacity:1"><div id="workspaceOfficerPrompts"><div id="ofQuick"><button id="keyline-prompt">Who is waiting on me?</button></div></div></div></body></html>');
  const read=()=>page.evaluate(()=>{const b=document.getElementById('keyline-prompt'),s=getComputedStyle(b),r=b.getBoundingClientRect();return {shadow:s.boxShadow,color:s.outlineColor,width:s.outlineWidth,offset:s.outlineOffset,radius:s.borderRadius,visible:b.matches(':focus-visible'),rect:[r.left,r.top,r.width,r.height]}});
  const check=async()=>{const value=await read();assert.equal(value.visible,true);assert.equal(value.shadow,'rgb(7, 9, 13) 0px 0px 0px 5px','actual keyboard keyline covers the complete outline band');assert.equal(value.width,'2px');assert.equal(value.offset,'2px');assert.equal(value.radius,'0px');assert(5>=parseFloat(value.width)+parseFloat(value.offset)+1,'at least one pixel of outer backing');assert(parseFloat(value.offset)>=1,'inner backing separates the ring from the button');assert(focusContrast(focusRGBA(value.color),[7,9,13,1])>=3);return value;};
  const results=[];
  for(const background of ['rgb(255,255,255)','rgb(185,155,237)']){await mount(background);const before=await read();assert.equal(before.shadow,'none');await page.keyboard.press('Tab');const keyboard=await check();assert.deepEqual(keyboard.rect,before.rect,'keyline does not move or resize the prompt');await page.evaluate(()=>document.activeElement.blur());assert.equal((await read()).shadow,'none','blur removes keyline');await mount(background);const r=await page.evaluate(()=>{const b=document.getElementById('keyline-prompt').getBoundingClientRect();return {x:b.left+b.width/2,y:b.top+b.height/2}});await page.mouse.click(r.x,r.y);const pointer=await read();assert.equal(pointer.visible,false);assert.equal(pointer.shadow,'none','fresh pointer-only focus does not add keyline');results.push({background,keyboard,pointer});}
  for(const [theme,workspace] of [['neutral',true],['original',false]]){await mount('white',theme,workspace);await page.keyboard.press('Tab');assert.equal((await read()).shadow,'none','Neutral and Classic keep their existing appearance');}
  assert(css.includes(declaration));await mount('rgb(185,155,237)','original',true,css.replace(declaration,'outline-color:rgb(232,233,236)'));await page.keyboard.press('Tab');await assert.rejects(check,{name:'AssertionError'},'same keyboard coverage assertion rejects keyline deletion');
  await mount('rgb(185,155,237)');await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable outside outline box shadow/},'shadow paint is explicit UNKNOWN, never discarded as nonexistent');
  if(previous)await page.setViewport(previous);return results;
}
async function runWorkspaceFocusBrowserTests(page, html) {
  assert(html.includes(FALLBACK),'use the actual page stylesheet with its exact fallback');
  const css = html.slice(html.indexOf('<style>')+7,html.indexOf('</style>'));
  const fixture = '<main style="display:grid;gap:12px;justify-items:start"><button id="ordinary">Action</button><details open><summary id="summary">Details</summary><span class="sw" id="switch" tabindex="0" style="display:block;margin-top:12px">Toggle</span></details><div id="return-target" tabindex="-1">Return target</div><div id="implicit-scroll" style="height:40px;width:160px;overflow:auto"><div style="height:160px">Scrollable text</div></div><section id="workspacePanelEvidence"><article class="workspace-change-card workspace-change-selected" id="selected-card">Selected change</article></section></main>';
  const mount = async sheet => {await page.setContent('<!doctype html><html class="workspace" data-theme="neutral"><head><style>'+sheet+'</style></head><body style="padding:32px">'+fixture+'</body></html>');await page.evaluate(()=>{const n=document.createElement('button');n.id='late-unlisted';n.textContent='Another action';document.querySelector('main').append(n)});};
  await mount(css);
  const positive = await auditWorkspaceFocus(page);
  for (const id of ['ordinary','summary','switch','return-target','late-unlisted']) assert(positive.rows.some(r=>r.id===id),'generic walker discovers '+id);
  await page.evaluate(()=>{document.documentElement.dataset.theme='original';for(const [id,action] of [['workspaceControlsDialog','first-modal-action'],['workspaceNavDialog','top-modal-action']]){const d=document.createElement('dialog');d.id=id;d.className='workspace-dialog';const b=document.createElement('button');b.id=action;b.textContent='Modal action';d.append(b);document.body.append(d);d.showModal();}});
  const implicit=positive.rows.find(r=>r.id==='implicit-scroll');assert(implicit,'real browser discovers the implicit scroll-container focus target');assert.equal(implicit.tabindex,null,'walker never adds tabindex');assert.equal(implicit.tabIndex,-1,'implicit focus does not depend on positive tabIndex');
  const modal = await auditWorkspaceFocus(page);
  assert(modal.rows.some(r=>r.id==='top-modal-action'),'generic walker checks the actual top modal');
  assert(!modal.rows.some(r=>r.id==='first-modal-action'),'browser blocks focus behind the top modal');
  await page.evaluate(()=>{document.getElementById('workspaceNavDialog').remove();document.getElementById('workspaceControlsDialog').remove();document.documentElement.dataset.theme='neutral';});
  const checkSelected = async()=>{const value=await page.evaluate(()=>{const s=getComputedStyle(document.getElementById('selected-card'));return {color:s.outlineColor,width:s.outlineWidth,style:s.outlineStyle};});assert.deepEqual(value,{color:'rgb(28, 27, 25)',width:'1px',style:'solid'},'selected change card has its own ink outline');return value;};
  const selected = await checkSelected();
  await mount(css.replace(FALLBACK,''));
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/blue keyboard outline/},'removing fallback exposes the real user-agent blue ring');
  await mount(css.replace('workspace-change-selected{outline:1px solid rgb(28,27,25)','workspace-change-selected{outline:1px solid #7195df'));
  await assert.rejects(checkSelected,{name:'AssertionError'},'the same computed ink assertion rejects the selected-card mutant');
  // Square test-only chip isolates color/background math; actual curved pills remain UNKNOWN.
  const original=async sheet=>page.setContent('<!doctype html><html class="workspace" data-theme="original"><head><style>'+sheet+'</style></head><body><div id="workspaceFilters"><button id="contrast-chip" style="border-radius:0" data-filter="all" aria-pressed="false">All</button></div></body></html>');
  await original(css);const dark=await auditWorkspaceFocus(page),chip=dark.rows.find(r=>r.id==='contrast-chip');assert(chip,'actual Original chip is measured');assert.equal(chip.measurement.basis,'painted hit stack');assert.deepEqual(chip.measurement.background.slice(0,3),[7,9,13],'outside ring uses real dark parent, not white button');assert(chip.measurement.contrast>=3);
  const lightRule='html.workspace[data-theme=original] #workspaceFilters button:focus-visible{outline:1px solid rgb(232,233,236);outline-offset:2px}';assert(css.includes(lightRule));await original(css.replace(lightRule,lightRule.replace('232,233,236','28,27,25')));await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/low-contrast keyboard outline/},'dark ink on the real dark stage fails contrast');
  const promptRule='html.workspace[data-theme=original] body:not(.stepped):not(.workspace-officer-desk) #officer[data-workspace-composer=desktop] #workspaceOfficerPrompts #ofQuick button:focus-visible{outline-color:rgb(232,233,236)}';
  // Isolate the color/background regression from the separately verified keyline paint.
  const colorOnly=css.replace(';box-shadow:0 0 0 5px rgb(7,9,13)','');
  const overflow=async sheet=>page.setContent('<!doctype html><html class="workspace" data-theme="original"><head><style>'+sheet+'</style></head><body><div id="room" style="position:fixed;inset:0;background:rgb(7,9,13);background-image:none"></div><div id="officer" data-workspace-composer="desktop" style="position:fixed;left:80px;top:240px;right:auto;bottom:auto;width:700px;height:100px;background:white;transform:none;opacity:1"><div id="workspaceOfficerPrompts"><div id="ofQuick"><button id="overflow-prompt">What did I miss?</button></div></div></div></body></html>');
  // Caller runs the desktop fixture in a real desktop viewport; no CSS engine is simulated.
  const previousViewport=page.viewport();await page.setViewport({width:1440,height:900});
  {await overflow(colorOnly);const paint=await auditWorkspaceFocus(page),prompt=paint.rows.find(r=>r.id==='overflow-prompt');assert(prompt,'actual overflowing prompt is measured');const geometry=await page.evaluate(()=>({prompt:document.getElementById('overflow-prompt').getBoundingClientRect().bottom,parent:document.getElementById('officer').getBoundingClientRect().top}));assert(geometry.prompt<geometry.parent,'fixture really overflows above its white parent');assert(prompt.outsideSamples.every(p=>p.layers[0]?.id==='room'),'actual room is first paint on all four sides');assert.equal(prompt.measurement.basis,'painted hit stack');assert(prompt.measurement.samples.every(s=>s.background.slice(0,3).join(',')==='7,9,13'),'all twelve ring points see the dark room, not its white ancestor');
    await overflow(colorOnly.replace(promptRule,''));await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/low-contrast keyboard outline:.*overflow-prompt/},'the old desktop color fails the SAME actual painted-background audit');
  }
  if(previousViewport)await page.setViewport(previousViewport);
  await page.setContent('<!doctype html><html class="workspace" data-theme="neutral"><head><style>'+css+'</style></head><body style="padding:32px"><button>Initial focus</button><div tabindex="0" id="border-image-probe" style="margin:20px;width:180px;height:60px;background:black;border:2px solid black;border-image:linear-gradient(white,white) 1;outline:1px solid white;outline-offset:-1px">Border image</div></body></html>');
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable border image/},'painted border image cannot be replaced by its black border-color');
  await page.setContent('<!doctype html><html class="workspace" data-theme="neutral"><head><style>'+css+'</style></head><body style="padding:32px"><button>Initial focus</button><div tabindex="0" id="border-outset-probe" style="margin:40px;width:180px;height:60px;background:black;border:2px solid black;border-image:linear-gradient(white,white) 1;border-image-outset:8px;outline:1px solid white;outline-offset:2px">Border image outset</div></body></html>');
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable border image/},'outset border image can cover the positive-offset ring background too');
  await page.setContent('<html class="workspace" data-theme="neutral"><head><style>'+css+'</style></head><body style="padding:40px"><button id="covered" style="position:absolute;left:100px;top:100px;width:140px;height:40px;border-radius:0;outline:1px solid white;outline-offset:2px">Covered</button><div style="position:absolute;left:80px;top:80px;width:180px;height:80px;background:black;z-index:999"></div></body></html>');
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable outside paint: (?:target is occluded or not hit-testable|foreign paint is not witnessed below target) \[covered\]/},'opaque foreign overlay is not a hypothetical passing background');
  await page.setContent('<html class="workspace" data-theme="neutral"><head><style>'+css+'</style></head><body style="padding:40px"><button id="rounded-probe" style="width:300px;height:12px;min-height:0;padding:0;border:0;line-height:12px;border-radius:4px;outline:1px solid black;outline-offset:2px">Rounded</button></body></html>');
  const rounded=await page.evaluate(()=>{const e=document.getElementById('rounded-probe');return {height:e.getBoundingClientRect().height,radius:getComputedStyle(e).borderRadius}});assert.deepEqual(rounded,{height:12,radius:'4px'},'actual rounded fixture isolates quarter-side geometry from interior occlusion');
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable outside paint:.*curved/},'quarter-side witnesses do not pretend to follow rounded curves');
  for(const kind of ['content-box','border']){await page.setContent('<html class="workspace" data-theme="neutral"><head><style>'+css+'</style></head><body style="padding:40px"><div style="position:relative;width:240px;height:120px;background:black;'+(kind==='content-box'?'padding:20px;background-clip:content-box':'border:20px solid black')+'"><button id="paint-probe" style="position:absolute;left:0;top:0;margin:0;width:80px;height:40px;border-radius:0;outline:1px solid white;outline-offset:2px">Paint probe</button></div></body></html>');await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/unmeasurable outside paint: (?:unsupported sampled background clip|sample intersects foreign border paint).*paint-probe/},'foreign clip/border paint is not replaced by backgroundColor');}
  const keyline=await runWorkspacePromptKeylineTests(page,css);
  const flag=await runWorkspaceFlagFocusTests(page,css);
  return {positive,modal,selected,dark,flag,keyline,fallbackMutant:'rejected actual blue',selectedMutant:'rejected by same computed ink assertion',darkInkMutant:'rejected low contrast',borderImage:'rejected unknown painted border'};
}
module.exports = {FALLBACK,focusRGBA,focusComposite,focusBlue,focusContrast,focusMeasurement,auditWorkspaceFocus,runWorkspaceFlagFocusTests,runWorkspacePromptKeylineTests,runWorkspaceFocusBrowserTests};
