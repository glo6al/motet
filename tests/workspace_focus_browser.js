'use strict';
/* Actual-browser proof, deliberately runtime-injected: no browser dependency or launch here.
   A caller supplies a fresh, isolated Puppeteer-compatible page with evaluate/setContent/keyboard.press.
   This module changes focus and its own fixture only; use a separate page from a running application. */
const assert = require('node:assert/strict');
const FALLBACK = ':where(html.workspace *):focus-visible{outline:1px solid rgb(28,27,25);outline-offset:2px}';
async function auditWorkspaceFocus(page) {
  await page.keyboard.press('Tab'); // Real keyboard modality; programmatic focus must not fake :focus-visible.
  const result = await page.evaluate(() => {
    if (!document.documentElement.classList.contains('workspace')) throw Error('Workspace document required');
    const rows = [], skipped = [], before = document.activeElement;
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
      const focused = getComputedStyle(el);
      rows.push({...identity,color:focused.outlineColor,style:focused.outlineStyle,width:focused.outlineWidth,offset:focused.outlineOffset});
    }
    before?.focus?.({preventScroll:true});
    return {rows,skipped};
  });
  assert(result.rows.length>0,'actual browser focus walk must not be vacuous');
  for (const row of result.rows) {
    const rgb = row.color.match(/^rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)/);
    assert(rgb,'actual computed outline color must be RGB: '+row.color);
    const drawn = !['none','hidden'].includes(row.style)&&parseFloat(row.width)>0;
    const blue = +rgb[3]>+rgb[1]&&+rgb[3]>+rgb[2];
    assert(!(drawn&&blue),'blue keyboard outline: '+JSON.stringify(row));
  }
  return result;
}
async function runWorkspaceFocusBrowserTests(page, html) {
  assert(html.includes(FALLBACK),'use the actual page stylesheet with its exact fallback');
  const css = html.slice(html.indexOf('<style>')+7,html.indexOf('</style>'));
  const fixture = '<main><button id="ordinary">Action</button><details open><summary id="summary">Details</summary><span class="sw" id="switch" tabindex="0">Toggle</span></details><div id="return-target" tabindex="-1">Return target</div><div id="implicit-scroll" style="height:40px;width:160px;overflow:auto"><div style="height:160px">Scrollable text</div></div><section id="workspacePanelEvidence"><article class="workspace-change-card workspace-change-selected" id="selected-card">Selected change</article></section></main>';
  const mount = async sheet => {await page.setContent('<!doctype html><html class="workspace"><head><style>'+sheet+'</style></head><body>'+fixture+'</body></html>');await page.evaluate(()=>{const n=document.createElement('button');n.id='late-unlisted';n.textContent='Another action';document.querySelector('main').append(n)});};
  await mount(css);
  const positive = await auditWorkspaceFocus(page);
  for (const id of ['ordinary','summary','switch','return-target','late-unlisted']) assert(positive.rows.some(r=>r.id===id),'generic walker discovers '+id);
  await page.evaluate(()=>{for(const id of ['first-modal','top-modal']){const d=document.createElement('dialog');d.id=id;const b=document.createElement('button');b.id=id+'-action';b.textContent='Modal action';d.append(b);document.body.append(d);d.showModal();}});
  const implicit=positive.rows.find(r=>r.id==='implicit-scroll');assert(implicit,'real browser discovers the implicit scroll-container focus target');assert.equal(implicit.tabindex,null,'walker never adds tabindex');assert.equal(implicit.tabIndex,-1,'implicit focus does not depend on positive tabIndex');
  const modal = await auditWorkspaceFocus(page);
  assert(modal.rows.some(r=>r.id==='top-modal-action'),'generic walker checks the actual top modal');
  assert(!modal.rows.some(r=>r.id==='first-modal-action'),'browser blocks focus behind the top modal');
  await page.evaluate(()=>{document.getElementById('top-modal').remove();document.getElementById('first-modal').remove();});
  const checkSelected = async()=>{const value=await page.evaluate(()=>{const s=getComputedStyle(document.getElementById('selected-card'));return {color:s.outlineColor,width:s.outlineWidth,style:s.outlineStyle};});assert.deepEqual(value,{color:'rgb(28, 27, 25)',width:'1px',style:'solid'},'selected change card has its own ink outline');return value;};
  const selected = await checkSelected();
  await mount(css.replace(FALLBACK,''));
  await assert.rejects(()=>auditWorkspaceFocus(page),{name:'AssertionError',message:/blue keyboard outline/},'removing fallback exposes the real user-agent blue ring');
  await mount(css.replace('workspace-change-selected{outline:1px solid rgb(28,27,25)','workspace-change-selected{outline:1px solid #7195df'));
  await assert.rejects(checkSelected,{name:'AssertionError'},'the same computed ink assertion rejects the selected-card mutant');
  return {positive,modal,selected,fallbackMutant:'rejected actual blue',selectedMutant:'rejected by same computed ink assertion'};
}
module.exports = {FALLBACK,auditWorkspaceFocus,runWorkspaceFocusBrowserTests};
