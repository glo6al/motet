'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { DraftModel, ManualJournal, textPatch } = require('../src/web/dictation_draft.js');
const { PCMResampler } = require('../src/web/dictation_worklet.js');

const source = fs.readFileSync(path.join(__dirname, '../src/web/dictation.js'), 'utf8');
const draftSource = fs.readFileSync(path.join(__dirname, '../src/web/dictation_draft.js'), 'utf8');
const viewSource = fs.readFileSync(path.join(__dirname, '../src/web/dictation_view.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function flush() { for (let i = 0; i < 4; i++) await tick(); }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, handler, capture = false) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(handler);
    if (!this.listenerOptions) this.listenerOptions = new Map();
    this.listenerOptions.set(handler, capture);
  }
  removeEventListener(name, handler) { this.listeners.get(name)?.delete(handler); }
  dispatch(name, value = {}) {
    const handlers = [...(this.listeners.get(name) || [])];
    for (const capture of [true, false]) for (const handler of handlers)
      if (!!this.listenerOptions?.get(handler) === capture) handler(value);
  }
}
class Element extends Target {
  constructor(document) {
    super(); this.ownerDocument = document; this.style = new Proxy({}, { set(target, name, value) {
      if (name === 'height') document.heightWrites = (document.heightWrites || 0) + 1;
      target[name] = value; return true;
    } }); this.hidden = false; this.disabled = false;
    this.value = ''; this.maxLength = 50000; this.scrollTop = 17; this.scrollLeft = 3;
    this.selectionStart = 0; this.selectionEnd = 0; this.attributes = new Map(); this.children = [];
    const names = new Set();
    this.classList = { toggle(name, on) { if (on) names.add(name); else names.delete(name); },
      add(...values) { for (const name of values) names.add(name); },
      remove(...values) { for (const name of values) names.delete(name); }, contains(name) { return names.has(name); } };
  }
  get textContent() { return this._textContent || ''; }
  set textContent(value) { this._textContent = String(value); this.textWrites = (this.textWrites || 0) + 1; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  toggleAttribute(name, on) { if (on) this.attributes.set(name, ''); else this.attributes.delete(name); if (name === 'hidden') this.hidden = on; }
  appendChild(child) { child.parent = this; this.children.push(child); }
  insertBefore(child, before) {
    const at = before ? this.children.indexOf(before) : this.children.length; assert(at >= 0);
    child.parent = this; this.children.splice(at, 0, child); return child;
  }
  append(...children) { for (const child of children) this.appendChild(child); }
  after(node) { assert(this.parent); node.parent = this.parent; this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, node); }
  replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  setRangeText(text, start, end, mode) {
    assert.equal(mode, 'preserve');
    const oldStart = this.selectionStart, oldEnd = this.selectionEnd;
    const delta = text.length - (end - start);
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    this.selectionStart = oldStart > end ? oldStart + delta : oldStart > start ? start : oldStart;
    this.selectionEnd = oldEnd > end ? oldEnd + delta : oldEnd > start ? start + text.length : oldEnd;
  }
  setSelectionRange(start, end, direction = 'none') {
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  }
  focus() { this.ownerDocument.activeElement = this; }
}

function input(env, text, { start, end = start, after = start + Math.max(0, text.length - env.elements.textarea.value.length + end - start), type = 'insertText' } = {}) {
  const textarea = env.elements.textarea;
  textarea.setSelectionRange(start, end);
  textarea.dispatch('beforeinput', { inputType: type, cancelable: true, preventDefault() {} });
  textarea.value = text; textarea.setSelectionRange(after, after);
  textarea.dispatch('input', { inputType: type });
}
function historyKey(env, { redo = false, ctrl = false } = {}) {
  const event = { key: ctrl && redo ? 'y' : 'z', metaKey: !ctrl, ctrlKey: ctrl, shiftKey: !ctrl && redo,
    defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
  env.elements.textarea.dispatch('keydown', event); return event;
}

function environment(configuration = {}) {
  const document = new Target(); document.hidden = false;
  document.createElement = () => new Element(document);
  document.createElementNS = (namespace, tag) => { const element = new Element(document); element.namespaceURI = namespace; return element; };
  document.createTextNode = text => { const element = new Element(document); element.textContent = text; return element; };
  const elements = Object.fromEntries(['host', 'textarea', 'mic', 'cancel', 'send', 'wave', 'status', 'settings'].map(name => [name, new Element(document)]));
  elements.host.append(elements.textarea, elements.mic, elements.cancel, elements.send);
  const env = { document, elements, demo: false, allowed: true, target: 'stage:all', readiness: 'ready', epoch: 1,
    calls: [], mediaCalls: 0, contexts: [], worklets: [], tracks: [], pendingEvents: [], nextId: 0, sequence: 0,
    patches: 0, stateChanges: [], sessionNumber: 0, resumeCalls: 0, intervals: [], drafts: new Map() };
  Object.assign(env, configuration);
  // The page binds its native draft-save/autosize/paint listeners before the
  // optional controller is constructed. Their synchronous refresh is real.
  if (env.legacyRefresh) elements.textarea.addEventListener('input', () => {
    env.legacyRefreshCalls = (env.legacyRefreshCalls || 0) + 1; env.controller?.refresh();
  });
  function newStream() {
    const track = new Target(); track.stopped = false;
    track.stop = () => { track.stopped = true; };
    env.tracks.push(track);
    return { getTracks: () => [track], getAudioTracks: () => [track] };
  }
  class AudioContext extends Target {
    constructor() {
      super(); this.state = env.contextState || 'running'; this.sampleRate = 48000; this.destination = {};
      this.audioWorklet = { addModule: url => { env.moduleUrl = url; return env.moduleDeferred ? env.moduleDeferred.promise : Promise.resolve(); } };
      env.contexts.push(this);
    }
    resume() { env.resumeCalls++; return env.resume ? env.resume(this, env.resumeCalls) : Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { fftSize: 256, connect() {}, disconnect() {}, getFloatTimeDomainData(values) { values.fill(.02); } }; }
  }
  class AudioWorkletNode extends Target {
    constructor(context, processor) {
      super(); assert.equal(processor, 'motet-dictation-pcm');
      this.port = { onmessage: null, postMessage: value => {
        if (value.type === 'flush') queueMicrotask(() => this.port.onmessage?.({ data: { type: 'flushed', requestId: value.requestId } }));
      } };
      env.worklets.push(this);
    }
    connect() {}
    disconnect() {}
    pcm(bytes = 6400) { this.port.onmessage?.({ data: { type: 'pcm', buffer: new ArrayBuffer(bytes) } }); }
  }
  const browser = new Target();
  const motion = new Target(); motion.matches = !!env.reducedMotion; env.motion = motion;
  const setMeterInterval = env.fakeIntervals ? (fn, delay) => { const timer = { fn, delay, active: true }; env.intervals.push(timer); return timer; } : setInterval;
  const clearMeterInterval = env.fakeIntervals ? timer => { timer.active = false; } : clearInterval;
  Object.assign(browser, { document, isSecureContext: true, AudioContext, AudioWorkletNode,
    navigator: { mediaDevices: { getUserMedia: async () => {
      env.mediaCalls++;
      env.noticeBeforeMicrophone = env.stateChanges.at(-1)?.detail.notice;
      if (env.mediaError) throw env.mediaError;
      return env.mediaDeferred ? env.mediaDeferred.promise : newStream();
    } } },
    console, setTimeout, clearTimeout, setInterval: setMeterInterval, clearInterval: clearMeterInterval, AbortController, ArrayBuffer,
    matchMedia: query => query.includes('prefers-reduced-motion') ? motion : { matches: false },
    getComputedStyle: () => ({ fontFamily: 'system-ui', fontSize: '16px', fontWeight: '400', fontStyle: 'italic',
      lineHeight: '24px', paddingTop: '0px', paddingRight: '0px', paddingBottom: '0px', paddingLeft: '0px' }),
    Float32Array, Float64Array, Date, Promise, Map, Set });
  vm.createContext(browser); vm.runInContext(env.mutateDraft ? env.mutateDraft(draftSource) : draftSource, browser);
  vm.runInContext(env.mutateController ? env.mutateController(source) : source, browser);
  if (env.withView) {
    vm.runInContext(env.mutateView ? env.mutateView(viewSource) : viewSource, browser);
    for (const name of ['textarea','mic','cancel','send','wave','status','settings']) elements[name].remove();
    const row = new Element(document), compose = new Element(document);
    elements.host.append(row); row.append(compose, elements.send); compose.append(elements.textarea);
    const view = browser.MotetDictationView.attach({ textarea: elements.textarea, row, compose, host: elements.host, send: elements.send });
    env.view = view;
    for (const name of ['mic','cancel','send','wave','status','settings']) elements[name] = view[name];
  }
  env.browser = browser; env.newStream = newStream;
  const endpoint = async (url, config) => {
    const route = url.replace('/api/dictation/', '');
    let data = config.body;
    if (typeof data === 'string') data = JSON.parse(data);
    env.calls.push({ route, config, data });
    if (route === 'status') return { state: env.readiness, code: env.readinessCode || null };
    if (route === 'start') {
      if (env.readiness === 'failed' && !env.prepareReturnedSession) {
        env.readiness = 'loading'; const error = new Error('worker preparing'); error.code = 'loading'; throw error;
      }
      env.sessionNumber++; env.epoch = 1; env.sequence = 0;
      const result = { sessionId: String(env.sessionNumber).padStart(48, '0'), epoch: 1 };
      return env.startDeferred ? env.startDeferred.promise : result;
    }
    if (route === 'audio') {
      if (env.audioDeferred) return env.audioDeferred.promise;
      assert.equal(Number(config.headers['X-Dictation-Sequence']), env.sequence++);
      return { acceptedSequence: env.sequence - 1, epoch: env.epoch };
    }
    if (route === 'reset') {
      if (env.resetDeferred) return env.resetDeferred.promise;
      return { epoch: ++env.epoch };
    }
    if (route === 'stop') {
      if (env.stopDeferred) return env.stopDeferred.promise;
      const result = { epoch: env.epoch, ended: true };
      queueMicrotask(() => env.emit([
        ...(env.stopText ? [env.line(env.stopText, true)] : []),
        { id: ++env.nextId, type: 'ended', epoch: env.epoch }
      ], true));
      return result;
    }
    if (route === 'cancel') return { epoch: env.epoch, ended: true };
    if (route === 'events') {
      const result = deferred();
      const item = { resolve: result.resolve, reject: result.reject, signal: config.signal };
      env.pendingEvents.push(item);
      config.signal.addEventListener('abort', () => {
        const index = env.pendingEvents.indexOf(item);
        if (index !== -1) env.pendingEvents.splice(index, 1);
        const error = new Error('aborted'); error.name = 'AbortError'; result.reject(error);
      }, { once: true });
      return result.promise;
    }
    throw new Error('Unexpected route: ' + route);
  };
  env.line = (text, complete = false, epoch = env.epoch, lineId = 'a') => ({ id: ++env.nextId, type: 'line', epoch, lineId, text, complete });
  env.emit = (events, ended = false) => {
    const entry = env.pendingEvents.shift();
    assert(entry, 'Expected a pending event request'); entry.resolve({ events, ended, epoch: env.epoch });
  };
  env.controller = browser.MotetDictation.create(Object.assign({}, elements, {
    api: endpoint, demo: () => env.demo, canUse: () => env.allowed, target: () => env.target,
    prepareTimeoutMs: env.prepareTimeoutMs, captureNotice: () => env.captureNotice || '',
    canCancelEscape: () => !env.foreground,
    onDraftChange: () => { env.patches++; env.drafts.set(env.target, elements.textarea.value); elements.textarea.dispatch('input'); },
    onState: (state, detail) => {
      env.stateChanges.push({ state, detail });
      if (env.view) env.view.render(state === 'checking' ? 'preparing' : state,
        Object.assign({}, detail, { message: state === 'listening' ? '' : detail.message }));
    }
  }));
  env.close = async () => { env.controller.destroy(); if (env.view) env.view.destroy(); await flush(); };
  return env;
}

async function test(name, action) {
  await action(); console.log('PASS ' + name);
}

async function caretProof(mutateController) {
  for (const place of ['end', 'interior']) {
    const env = environment({ mutateController });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('first phrase')]); await flush();
      const textarea = env.elements.textarea, position = place === 'end' ? textarea.value.length : 8;
      textarea.setSelectionRange(position, position);
      env.emit([env.line('first sentence now')]); await flush();
      assert.equal(textarea.selectionStart, textarea.selectionEnd, place + ' caret must stay collapsed');
      assert.equal(textarea.selectionStart, place === 'end' ? textarea.value.length : position);
    } finally { await env.close(); }
  }
}

async function legacyInputProof(mutateController) {
  const env = environment({ mutateController, legacyRefresh: true });
  try {
    const textarea = env.elements.textarea; textarea.value = 'Typed prefix. ';
    await env.controller.start(); await flush(); env.emit([env.line('Which colleague changed pricing')]); await flush();
    const original = textarea.value, at = original.indexOf('colleague') + 2;
    input(env, original.slice(0, at) + 'x' + original.slice(at), { start: at, after: at + 1 }); await flush();
    assert.equal(env.controller.state, 'listening', 'A native edit must reconcile before older autosize/paint input listeners refresh ownership');
    assert(env.legacyRefreshCalls >= 2, 'Both recognition input and native input reached the older page listener');
    assert(textarea.value.includes('coxlleague'));
    env.emit([env.line('Later sentence stays')]); await flush();
    assert(textarea.value.startsWith('Typed prefix. Which coxlleague changed pricing')); assert(textarea.value.includes('Later sentence stays'));
    textarea.dispatch('compositionstart');
    input(env, textarea.value + '途中', { start: textarea.value.length, after: textarea.value.length + 2 });
    assert.equal(env.controller.state, 'listening', 'Legacy refresh during IME cannot misclassify composition as replacement');
    textarea.dispatch('compositionend'); await flush(); assert.equal(env.controller.state, 'listening');
    const replacement = 'A genuinely replaced draft';textarea.value = replacement;env.controller.refresh();
    assert.equal(env.controller.active, false, 'A programmatic replacement without an input edit still retires old recognition');
    assert.equal(textarea.value, replacement);assert.equal(env.stateChanges.at(-1).detail.reason, 'draft_changed');
  } finally { await env.close(); }
}

async function layoutKeyProof(mutateController) {
  const env = environment({ mutateController });
  try {
    input(env, 'Original', { start: 0, after: 8 });
    await env.controller.start(); await flush(); env.emit([env.line('spoken words')]); await flush();
    input(env, 'Edited spoken words', { start: 0, end: 8, after: 6 });
    function key(value, code, control = false) {
      const event = { key: value, code, metaKey: !control, ctrlKey: control, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
      env.elements.textarea.dispatch('keydown', event); return event;
    }
    assert(key('z', 'KeyY').defaultPrevented, 'German semantic Z means Undo');
    assert.equal(env.elements.textarea.value, 'Original spoken words');
    assert(key('y', 'KeyZ').defaultPrevented, 'German semantic Y means Redo');
    assert.equal(env.elements.textarea.value, 'Edited spoken words');
    assert.equal(key('f', 'KeyY', true).defaultPrevented, false, 'Dvorak Ctrl+F is not a history command');
    assert(key('я', 'KeyZ').defaultPrevented, 'Non-Latin key falls back to the physical history shortcut');
    assert.equal(env.elements.textarea.value, 'Original spoken words');
  } finally { await env.close(); }
}

async function motionProof(mutateController) {
  const env = environment({ reducedMotion: true, fakeIntervals: true, mutateController });
  try {
    await env.controller.start(); await flush();
    assert(env.elements.wave.classList.contains('dictation-wave-still'));
    const writes = env.document.heightWrites;
    for (const timer of env.intervals.filter(timer => timer.active)) { timer.fn(); timer.fn(); timer.fn(); }
    assert.equal(env.document.heightWrites, writes, 'Reduced-motion meter must not animate bar heights');
    env.motion.matches = false; env.motion.dispatch('change');
    const moving = env.document.heightWrites;
    for (const timer of env.intervals.filter(timer => timer.active)) timer.fn();
    assert(env.document.heightWrites > moving, 'The control case must exercise the live meter');
  } finally { await env.close(); }
}

async function resumeProof(mutateController) {
  const env = environment({ mutateController, contextState: 'suspended',
    resume: (context, calls) => { if (calls === 1) return new Promise(() => {}); context.state = 'running'; return Promise.resolve(); } });
  try {
    await env.controller.start(); await flush();
    assert.equal(env.resumeCalls, 2, 'Audio must resume again after the microphone stream arrives');
    assert.equal(env.controller.state, 'listening');
  } finally { await env.close(); }
}

async function resumeTimeoutProof(mutateController) {
  const env = environment({ mutateController, prepareTimeoutMs: 15, resume: () => new Promise(() => {}) });
  try {
    const starting = env.controller.start();
    const result = await Promise.race([starting.then(() => 'settled'), new Promise(resolve => setTimeout(() => resolve('hung'), 100))]);
    assert.equal(result, 'settled', 'A live microphone cannot wait indefinitely for resume');
    assert.equal(env.controller.state, 'audio_start_failed');
    assert.equal(env.controller.active, false); assert(env.tracks.length > 0 && env.tracks.every(track => track.stopped));
    assert.equal(env.calls.filter(call => call.route === 'start').length, 0);
  } finally { await env.close(); }
}

function viewFixture(mutateView = value => value) {
  const document = new Target(); document.createElement = () => new Element(document);
  document.createElementNS = namespace => { const node = new Element(document); node.namespaceURI = namespace; return node; };
  document.createTextNode = text => { const node = new Element(document); node.textContent = text; return node; };
  const host = new Element(document), row = new Element(document), compose = new Element(document),
    textarea = new Element(document), send = new Element(document);
  host.appendChild(row); row.append(compose, send); compose.appendChild(textarea);
  textarea.value = 'Some current words'; textarea.clientWidth = 500; textarea.clientHeight = 60;
  textarea.offsetLeft = textarea.offsetTop = textarea.clientLeft = textarea.clientTop = 0;
  const scope = new Target(); Object.assign(scope, { document, getComputedStyle: () => ({ fontFamily: 'system-ui',
    fontSize: '16px', lineHeight: '24px', fontStyle: 'italic', paddingTop: '0px', paddingRight: '0px', paddingBottom: '0px', paddingLeft: '0px' }),
  matchMedia: () => ({ matches: false }) });
  vm.createContext(scope); vm.runInContext(mutateView(viewSource), scope);
  const view = scope.MotetDictationView.attach({ textarea, row, compose, host, send });
  return { view, host, textarea, document };
}

function announcerProof(mutateView) {
  const { view, host } = viewFixture(mutateView);
  try {
    view.render('listening', { message: '', ranges: [] });
    assert.equal(view.announcer.parent, host, 'The live region must remain outside hidden feedback');
    assert.equal(view.announcer.hidden, false); assert.equal(view.status.parent.hidden, true);
    assert.equal(view.announcer.getAttribute('role'), 'status');
    assert.equal(view.announcer.getAttribute('aria-live'), 'polite');
    assert.equal(view.announcer.textContent, 'Listening');
    const writes = view.announcer.textWrites;
    for (let i = 0; i < 10; i++) view.render('listening', { message: '', ranges: [] });
    assert.equal(view.announcer.textWrites, writes, 'Partials must not repeat Listening');
    view.render('idle', { reason: 'stop', announcement: 'Dictation stopped' });
    assert.equal(view.announcer.textContent, 'Dictation stopped');
    view.render('listening'); view.render('idle', { reason: 'cancel', announcement: 'Dictation cancelled' });
    assert.equal(view.announcer.textContent, 'Dictation cancelled');
  } finally { view.destroy(); }
}

async function contextReasonProof(mutateController) {
  for (const reason of ['recipient', 'scope', 'desk', 'locked', 'keychanged', 'unavailable']) {
    const env = environment({ mutateController, withView: reason === 'desk' });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('Keep these words')]); await flush();
      env.controller.contextChanged(reason);
      assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
      assert.equal(env.elements.textarea.value, 'Keep these words');
      const detail = env.stateChanges.at(-1).detail;
      assert.equal(detail.reason, reason); assert.match(detail.message, /Dictation stopped/);
      assert(detail.message.length > 25); assert.equal(detail.announcement, detail.message);
      if (reason === 'desk') {
        assert.equal(detail.announcement, 'Dictation stopped when you changed desks.');
        assert.equal(env.view.announcer.textContent, detail.announcement, 'The attached live region announces the desk reason');
      }
    } finally { await env.close(); }
  }
}

async function escapeProof(mutateController) {
  const env = environment({ mutateController });
  try {
    env.elements.textarea.value = 'Typed first'; await env.controller.start(); await flush();
    env.emit([env.line('dictated next')]); await flush();
    const event = { key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
    env.elements.host.dispatch('keydown', event);
    assert(event.defaultPrevented); assert.equal(env.elements.textarea.value, 'Typed first', 'Escape has Cancel semantics, not Stop');
    assert.equal(env.controller.active, false); assert.equal(env.stateChanges.at(-1).detail.announcement, 'Dictation cancelled');
    assert(env.tracks.every(track => track.stopped));
  } finally { await env.close(); }
}

async function focusedEscapeProof(mutateController) {
  for (const control of ['mic', 'cancel', 'send']) {
    const env = environment({ mutateController });
    try {
      env.elements.textarea.value = 'Typed prefix'; env.elements.mic.focus(); env.elements.mic.dispatch('click'); await flush();
      assert.equal(env.controller.state, 'listening'); assert.equal(env.document.activeElement, env.elements.mic);
      env.emit([env.line('spoken words')]); await flush(); env.elements[control].focus();
      const event = { key: 'Escape', target: env.elements[control], defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.immediate = true; } };
      env.elements.host.dispatch('keydown', event);
      assert(event.defaultPrevented); assert(event.immediate);
      assert.equal(env.controller.active, false, control + '-focused Escape must cancel');
      assert.equal(env.elements.textarea.value, 'Typed prefix'); assert(env.tracks.every(track => track.stopped));
      assert([...env.elements.host.listeners.get('keydown')].some(handler => env.elements.host.listenerOptions.get(handler) === true));
      await flush(); assert.equal(env.calls.filter(call => call.route === 'cancel').length, 1);
    } finally { await env.close(); }
  }
}

async function publicEscapeProof(mutateController) {
  const env = environment({ mutateController });
  const event = (extra = {}) => Object.assign({ key: 'Escape', target: env.elements.mic, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.immediate = true; } }, extra);
  try {
    assert.equal(env.controller.escape(event()), false, 'Idle Escape belongs to the surrounding application');
    env.elements.textarea.value = 'Typed prefix'; env.elements.mic.focus();
    env.elements.mic.dispatch('click'); await flush(); env.emit([env.line('spoken words')]); await flush();
    for (const blocked of [event({ isComposing: true }), event({ keyCode: 229 }), event({ defaultPrevented: true }), event({ key: 'Enter' })]) {
      assert.equal(env.controller.escape(blocked), false); assert(env.controller.active);
      assert.equal(blocked.immediate, undefined);
    }
    env.elements.textarea.dispatch('compositionstart');
    assert.equal(env.controller.escape(event()), false); assert(env.controller.active);
    env.elements.textarea.dispatch('compositionend'); await flush();
    env.foreground = true;
    assert.equal(env.controller.escape(event()), false, 'A foreground popup has first refusal'); assert(env.controller.active);
    env.foreground = false;
    const accepted = event();
    assert.equal(env.controller.escape(accepted), true, 'The earlier window capture handler can cancel a mic-focused take');
    assert(accepted.defaultPrevented); assert(accepted.immediate); assert.equal(env.controller.active, false);
    assert.equal(env.elements.textarea.value, 'Typed prefix'); assert(env.tracks.every(track => track.stopped));
    assert.equal(env.controller.escape(event()), false, 'Host fallback cannot cancel the same take twice');
    await flush(); assert.equal(env.calls.filter(call => call.route === 'cancel').length, 1);
  } finally { await env.close(); }
}

async function finishingEscapeProof(mutateController) {
  const stopped = deferred(), env = environment({ mutateController, withView: true, stopDeferred: stopped });
  try {
    env.elements.textarea.value = 'Typed.';await env.controller.start();await flush();
    env.emit([env.line('these words were kept by Stop')]);await flush();
    const visible = env.elements.textarea.value, pending = env.controller.stop();await flush();
    assert.equal(env.controller.state, 'finishing');assert(env.controller.active);assert.equal(env.elements.cancel.hidden, true);
    assert(env.tracks.every(track => track.stopped));
    const event = () => ({ key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; } });
    const direct = event();assert.equal(env.controller.escape(direct), false, 'Stop completion cannot be changed into Cancel');
    const local = event();env.elements.host.dispatch('keydown', local);
    for (const ignored of [direct, local]) { assert.equal(ignored.defaultPrevented, false);assert.equal(ignored.stopped, undefined); }
    assert.equal(env.elements.textarea.value, visible);assert.equal(env.controller.state, 'finishing');
    assert.equal(env.calls.filter(call => call.route === 'cancel').length, 0);
    stopped.resolve({ epoch: env.epoch, ended: true });
    env.emit([env.line('these words were kept by Stop and finalized', true), { id: ++env.nextId, type: 'ended', epoch: env.epoch }], true);
    await pending;await flush();
    assert.equal(env.elements.textarea.value, 'Typed. these words were kept by Stop and finalized');
    assert.equal(env.controller.active, false);assert.equal(env.stateChanges.at(-1).detail.reason, 'stop');
  } finally { stopped.resolve({ epoch: env.epoch, ended: true });await env.close(); }
}

async function sendNoticeProof(mutateController, mutateView) {
  const notice = 'This desk cannot receive a message. You can dictate a draft, but cannot send it here.';
  for (const withView of [false, true]) {
    const env = environment({ captureNotice: notice, mutateController, mutateView, withView });
    try {
      env.elements.send.disabled = true;await env.controller.start();await flush();
      assert.equal(env.noticeBeforeMicrophone, notice);assert.equal(env.controller.state, 'listening');
      const check = () => {
        assert.equal(env.elements.status.textContent, notice);assert.equal(env.elements.status.hidden, false, 'The notice span itself remains visible');
        if (withView) { assert.equal(env.elements.status.parent.hidden, false);assert(env.view.announcer.textContent.includes(notice)); }
      };
      check();
      env.emit([env.line('A draft only')]);await flush();check();
      env.controller.refresh();check();
      if (withView) {
        // Rendering a message must also recover a span hidden by an earlier writer.
        env.elements.status.hidden = true;env.view.render('listening', { notice });check();
      }
      assert.equal(env.elements.textarea.value, 'A draft only');assert(!env.calls.some(call => ['ask','send'].includes(call.route)));
    } finally { await env.close(); }
  }
}

function viewEndProof(mutateView) {
  const { view, host } = viewFixture(mutateView);
  try {
    for (const ending of [
      ['finishing', { message: 'Finishing the last words…' }], ['idle', { reason: 'stop', announcement: 'Dictation stopped' }],
      ['idle', { reason: 'cancel', announcement: 'Dictation cancelled' }], ['idle', { reason: 'send', announcement: 'Dictation stopped' }],
      ['idle', { reason: 'recipient', message: 'Dictation stopped when you changed the recipient.' }],
      ['failed', { code: 'failed', message: 'Dictation stopped because the local recognizer failed.' }]
    ]) {
      view.render('listening'); assert(host.classList.contains('dictation-active'));
      view.render(...ending);
      assert.equal(host.classList.contains('dictation-active'), false, ending[0] + ' returns stage and desk controls');
      assert.equal(view.cancel.hidden, true); assert.equal(view.wave.hidden, true);
      assert.equal(view.mic.getAttribute('aria-label'), 'Dictate a message');
    }
  } finally { view.destroy(); }
}

async function deskIdentityProof(mutateController) {
  for (const desk of ['watch:room', 'agent:claude', 'agent:codex', 'agent:cursor', 'agent:antigravity',
    'agent:sub-agent', 'agent:ended', 'agent:unavailable-send', 'agent:attachments']) {
    const env = environment({ target: desk, mutateController });
    try {
      env.drafts.set('unrelated:watch', 'Unchanged Motet draft');
      env.elements.textarea.value = 'Desk prefix'; await env.controller.start(); await flush();
      env.emit([env.line('words for this desk')]); await flush();
      const previous = env.pendingEvents[0], saved = env.elements.textarea.value;
      env.target = 'different:' + desk; env.drafts.set(env.target, saved);
      env.controller.refresh();
      assert.equal(env.controller.active, false, desk + ' target identity must retire even with an identical visible draft');
      previous.resolve({ events: [env.line('late old desk words')], ended: false }); await flush();
      assert.equal(env.elements.textarea.value, saved);
      assert.equal(env.drafts.get(desk), saved); assert.equal(env.drafts.get(env.target), saved);
      assert.equal(env.drafts.get('unrelated:watch'), 'Unchanged Motet draft');
      assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  }
}

async function main() {
  await test('recognition revisions preserve collapsed end and interior carets', () => caretProof());
  await test('native edits reconcile before older page autosize/paint refresh listeners', () => legacyInputProof());
  await test('history shortcuts follow Latin key meaning on German and Dvorak layouts', () => layoutKeyProof());
  await test('reduced motion keeps the sound indicator still and responds to preference changes', () => motionProof());
  await test('audio resumes again after the microphone stream arrives', () => resumeProof());
  await test('resume has a bounded timeout that releases the live microphone', () => resumeTimeoutProof());
  await test('listening, stopped and cancelled use one permanent quiet live announcer', () => announcerProof());
  await test('every non-user context stop has one explicit reason and keeps its words', () => contextReasonProof());
  await test('Escape cancels dictation and preserves the pre-existing draft', () => escapeProof());
  await test('composer capture cancels Escape with the microphone, Cancel or Send focused', () => focusedEscapeProof());
  await test('window Escape interception preserves IME and foreground-popup precedence', () => publicEscapeProof());
  await test('Escape during Stop completion keeps visible and finalized words without consuming dismissal', () => finishingEscapeProof());
  await test('stage and desk controls return on Stop, Cancel, Send and every interruption', () => viewEndProof());
  await test('Motet and all synthetic agent desk identities cannot share late dictated words', () => deskIdentityProof());
  await test('a cancelled pending resume settles and releases capture', async () => {
    const env = environment({ resume: () => new Promise(() => {}), prepareTimeoutMs: 200 });
    try {
      const beginning = env.controller.start(); await flush();
      assert.equal(env.resumeCalls, 2); assert(env.tracks.some(track => !track.stopped));
      env.controller.cancel(); await beginning;
      assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
      assert.equal(env.calls.filter(call => call.route === 'start').length, 0);
    } finally { await env.close(); }
  });
  await test('missing worklet preparation fails before asking for a microphone', async () => {
    const env = environment({ moduleDeferred: deferred(), prepareTimeoutMs: 15 });
    try {
      await env.controller.start();
      assert.equal(env.controller.state, 'capture_failed'); assert.equal(env.mediaCalls, 0);
      assert.equal(env.contexts[0].state, 'closed');
    } finally { await env.close(); }
  });
  await test('an unavailable-Send notice remains visibly rendered by the real controller and attached view', () => sendNoticeProof());
  await test('ordinary refresh and view changes leave a take listening', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush();
      env.controller.refresh(); env.controller.refresh(); env.browser.dispatch('resize');
      assert.equal(env.controller.state, 'listening'); assert(env.tracks.some(track => !track.stopped));
      env.document.hidden = true; env.document.dispatch('visibilitychange');
      assert.equal(env.stateChanges.at(-1).detail.reason, 'hidden');
      assert.match(env.elements.status.textContent, /left this tab/);
    } finally { await env.close(); }
  });
  await test('no capture or API activity on construction, demo, or missing key', async () => {
    for (const configuration of [{}, { demo: true }, { allowed: false }]) {
      const env = environment(configuration);
      try {
        assert.equal(env.mediaCalls, 0); assert.equal(env.calls.length, 0);
        if (configuration.demo || configuration.allowed === false) {
          await env.controller.start(); assert.equal(env.calls.length, 0); assert.equal(env.mediaCalls, 0);
        }
      } finally { await env.close(); }
    }
  });
  await test('unavailable helper is checked before any microphone request', async () => {
    for (const readiness of ['not_installed', 'not_running', 'loading', 'failed', 'listening']) {
      const env = environment({ readiness });
      try {
        env.elements.textarea.value = 'Keep my draft'; await env.controller.start();
        assert.equal(env.mediaCalls, 0); assert.equal(env.contexts.length, 0);
        assert.equal(env.calls.length, readiness === 'failed' ? 2 : 1); assert.equal(env.calls[0].route, 'status');
        assert.equal(env.elements.textarea.value, 'Keep my draft'); assert.equal(env.controller.active, false);
      } finally { await env.close(); }
    }
  });
  await test('failed worker preparation is explicit and never asks for the microphone', async () => {
    const env = environment({ readiness: 'failed' });
    try {
      env.elements.textarea.value = 'Keep this draft'; await env.controller.start(); await flush();
      assert.equal(env.controller.state, 'loading'); assert.equal(env.controller.active, false);
      assert.deepEqual(env.calls.map(call => call.route), ['status', 'start']);
      assert.equal(env.mediaCalls, 0); assert.equal(env.contexts.length, 0);
      assert.equal(env.elements.textarea.value, 'Keep this draft');
      env.readiness = 'ready'; await env.controller.start(); await flush();
      assert.equal(env.controller.state, 'listening'); assert.equal(env.mediaCalls, 1);
    } finally { await env.close(); }
  });
  await test('a failed-status preparation race closes any returned session without capture', async () => {
    const env = environment({ readiness: 'failed', prepareReturnedSession: true });
    try {
      await env.controller.start(); await flush();
      assert.equal(env.controller.state, 'loading'); assert.equal(env.controller.active, false);
      assert.deepEqual(env.calls.map(call => call.route), ['status', 'start', 'cancel']);
      assert.equal(env.mediaCalls, 0); assert.equal(env.contexts.length, 0);
    } finally { await env.close(); }
  });
  await test('permission denial keeps the draft and exposes a separate browser remedy', async () => {
    const error = new Error('private native detail'); error.name = 'NotAllowedError';
    const env = environment({ mediaError: error });
    try {
      env.elements.textarea.value = 'Typed first'; await env.controller.start();
      assert.equal(env.controller.state, 'microphone_blocked'); assert.equal(env.controller.active, false);
      assert.equal(env.elements.textarea.value, 'Typed first');
      assert(!env.elements.status.textContent.includes('private native detail'));
      assert.equal(env.contexts[0].state, 'closed');
    } finally { await env.close(); }
  });
  await test('live words preserve caret, selection, scroll and draft ownership', async () => {
    const env = environment();
    try {
      const textarea = env.elements.textarea; textarea.value = 'Please check:';
      textarea.selectionStart = 0; textarea.selectionEnd = 6;
      await env.controller.start(); await flush();
      assert.equal(env.elements.textarea.style.fontStyle, 'italic');
      assert.equal(env.moduleUrl, '/dictation-worklet.js');
      env.emit([env.line('the plan')]); await flush();
      assert.equal(textarea.value, 'Please check: the plan');
      assert.equal(textarea.selectionStart, 0); assert.equal(textarea.selectionEnd, 6);
      assert.equal(textarea.scrollTop, 17); assert.equal(textarea.scrollLeft, 3);
      assert.equal(env.controller.ranges[0].start, 14); assert.equal(env.controller.ranges[0].end, textarea.value.length);
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 0, 'Own input notification must not reset recognition');
      textarea.value = 'Please review: the plan'; textarea.dispatch('input'); await flush();
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 0);
      env.emit([env.line('the latest plan')]); await flush();
      assert.equal(textarea.value, 'Please review: the latest plan');
      textarea.value = 'Please review: the revised plan'; textarea.dispatch('input'); await flush();
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 1);
      assert.equal(env.controller.ranges.length, 0);
      env.emit([env.line('old overwrite', false, 1), env.line('and tomorrow', false, 2, 'b')]); await flush();
      assert.equal(textarea.value, 'Please review: the revised plan and tomorrow');
      env.controller.cancel(); await flush();
      assert.equal(textarea.value, 'Please review: the revised plan');
      assert.equal(textarea.style.fontStyle, 'normal');
      assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  });
  await test('Send freezes exact visible text synchronously and rejects late events', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush(); env.emit([env.line('Visible draft')]); await flush();
      const late = env.pendingEvents[0];
      const visible = env.controller.beforeSend();
      assert.equal(visible, 'Visible draft'); assert.equal(env.controller.active, false);
      assert(env.tracks.every(track => track.stopped)); assert.equal(env.elements.textarea.style.fontStyle, 'normal');
      env.elements.textarea.value = '';
      late.resolve({ events: [env.line('late replacement')], ended: false }); await flush();
      assert.equal(env.elements.textarea.value, '');
      assert.equal(env.calls.filter(call => call.route === 'send' || call.route === 'ask').length, 0);
      assert.equal(env.calls.filter(call => call.route === 'cancel').length, 1);
    } finally { await env.close(); }
  });
  await test('Stop releases the microphone immediately, then accepts the final phrase', async () => {
    const env = environment({ stopText: 'complete sentence' });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('complete')]); await flush();
      const stopped = env.controller.stop();
      assert(env.tracks.every(track => track.stopped)); assert.equal(env.elements.textarea.style.fontStyle, 'normal');
      await stopped; assert.equal(env.elements.textarea.value, 'complete sentence');
      assert.equal(env.controller.state, 'idle'); assert.equal(env.controller.ranges.length, 0);
    } finally { await env.close(); }
  });
  await test('unchanged provisional words keep their native selection during tail updates', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush(); env.emit([env.line('first second')]); await flush();
      const textarea = env.elements.textarea;
      textarea.selectionStart = 0; textarea.selectionEnd = 5;
      env.emit([env.line('first second third')]); await flush();
      assert.equal(textarea.selectionStart, 0); assert.equal(textarea.selectionEnd, 5);
      env.emit([env.line('first second third', true)]); await flush();
      assert.equal(env.controller.ranges.length, 0);
      assert.equal(textarea.selectionStart, 0); assert.equal(textarea.selectionEnd, 5);
    } finally { await env.close(); }
  });
  await test('typing during a pending reset stays owned before Stop finalizes', async () => {
    const reset = deferred(); const env = environment({ resetDeferred: reset, stopText: 'new speech' });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('active words')]); await flush();
      env.elements.textarea.value = 'changed words'; env.elements.textarea.dispatch('input'); await flush();
      env.elements.textarea.value = 'corrected words'; env.elements.textarea.dispatch('input');
      const stopping = env.controller.stop(); await flush();
      env.epoch = 2; reset.resolve({ epoch: 2 }); await stopping;
      assert.equal(env.elements.textarea.value, 'corrected words new speech');
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 1);
      assert.equal(env.controller.active, false);
    } finally { reset.resolve({ epoch: 2 }); await env.close(); }
  });
  await test('backend completion closes capture even while IME remains open', async () => {
    const env = environment({ stopText: 'complete words' });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('partial words')]); await flush();
      env.elements.textarea.dispatch('compositionstart');
      const stopping = env.controller.stop(); await flush();
      assert(env.tracks.every(track => track.stopped)); assert.equal(env.controller.state, 'finishing');
      env.elements.textarea.value = 'manually finished words'; env.elements.textarea.dispatch('input');
      env.elements.textarea.dispatch('compositionend'); await stopping;
      assert.equal(env.elements.textarea.value, 'manually finished words');
      assert.equal(env.controller.state, 'idle');
    } finally { await env.close(); }
  });
  await test('context switches reject stragglers without touching the new draft', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush(); env.emit([env.line('Old desk')]); await flush();
      const pending = env.pendingEvents[0]; env.target = 'desk:other'; env.controller.contextChanged();
      env.elements.textarea.value = 'New desk draft';
      pending.resolve({ events: [env.line('old target result')], ended: false }); await flush();
      assert.equal(env.elements.textarea.value, 'New desk draft'); assert.equal(env.controller.active, false);
      assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  });
  await test('Cancel keeps edits but removes untouched recognition across a reset', async () => {
    const env = environment();
    try {
      env.elements.textarea.value = 'Typed'; await env.controller.start(); await flush();
      env.emit([env.line('first phrase', true, 1, 'a'), env.line('second phrase', false, 1, 'b')]); await flush();
      env.elements.textarea.value = 'Typed first phrase revised second phrase'; env.elements.textarea.dispatch('input'); await flush();
      env.emit([env.line('retired first phrase', true, 1, 'a'), env.line('third phrase', false, 2, 'c')]); await flush();
      assert.equal(env.elements.textarea.value, 'Typed first phrase revised second phrase third phrase');
      env.controller.cancel(); await flush();
      assert.equal(env.elements.textarea.value, 'Typed revised second phrase');
      assert.equal(env.controller.active, false);
    } finally { await env.close(); }
  });
  await test('programmatic draft replacement retires old recognition without appending', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush(); env.emit([env.line('old recognition')]); await flush();
      env.elements.textarea.value = 'Restored attachment draft';
      env.emit([env.line('old recognition continued')]); await flush();
      assert.equal(env.elements.textarea.value, 'Restored attachment draft');
      assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 0);
    } finally { await env.close(); }
  });
  await test('programmatic replacement is checked before another audio packet', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush();
      env.elements.textarea.value = 'Another restored draft'; env.worklets[0].pcm(); await flush();
      assert.equal(env.elements.textarea.value, 'Another restored draft');
      assert.equal(env.calls.filter(call => call.route === 'audio').length, 0);
      assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  });
  await test('IME text is not changed mid-composition and old partials cannot overwrite it', async () => {
    const env = environment();
    try {
      await env.controller.start(); await flush(); env.emit([env.line('First words')]); await flush();
      env.elements.textarea.dispatch('compositionstart'); env.elements.textarea.value = 'First corrected words';
      env.elements.textarea.dispatch('input'); env.emit([env.line('engine replacement')]); await flush();
      assert.equal(env.elements.textarea.value, 'First corrected words');
      env.elements.textarea.dispatch('compositionend'); await flush();
      assert.equal(env.elements.textarea.value, 'First corrected words');
      assert.equal(env.calls.filter(call => call.route === 'reset').length, 1);
      env.emit([env.line('new speech', false, 2, 'b')]); await flush();
      assert.equal(env.elements.textarea.value, 'First corrected words new speech');
    } finally { await env.close(); }
  });
  await test('abandoned permission prompts cannot leave a microphone open', async () => {
    const permission = deferred(); const env = environment({ mediaDeferred: permission });
    try {
      const beginning = env.controller.start(); await flush();
      env.controller.cancel(); const stream = env.newStream(); permission.resolve(stream);
      await beginning; await flush();
      assert(env.tracks.every(track => track.stopped)); assert.equal(env.controller.active, false);
      assert.equal(env.calls.filter(call => call.route === 'start').length, 0);
    } finally { await env.close(); }
  });
  await test('cancel while loading the worklet prevents a stale backend start', async () => {
    const module = deferred(); const env = environment({ moduleDeferred: module });
    try {
      const beginning = env.controller.start(); await flush();
      env.controller.cancel(); module.resolve(); await beginning; await flush();
      assert.equal(env.calls.filter(call => call.route === 'start').length, 0);
      assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  });
  await test('a new take waits for an abandoned backend start and cancellation', async () => {
    const first = deferred(); const env = environment({ startDeferred: first });
    try {
      const beginning = env.controller.start(); await flush();
      assert.equal(env.calls.filter(call => call.route === 'start').length, 1);
      env.controller.cancel(); env.startDeferred = null;
      const again = env.controller.start(); await flush();
      assert.equal(env.calls.filter(call => call.route === 'start').length, 1);
      first.resolve({ sessionId: '1'.padStart(48, '0'), epoch: 1 });
      await beginning; await again; await flush();
      const controls = env.calls.filter(call => ['start', 'cancel'].includes(call.route)).map(call => call.route);
      assert.deepEqual(controls, ['start', 'cancel', 'start']);
      assert.equal(env.controller.state, 'listening'); assert(env.tracks[0].stopped);
    } finally { await env.close(); }
  });
  await test('capture is ordered and a growing queue stops without losing the draft', async () => {
    const blocked = deferred(); const env = environment({ audioDeferred: blocked });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('Keep these words')]); await flush();
      const node = env.worklets[0]; for (let i = 0; i < 21; i++) node.pcm();
      assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
      assert.equal(env.elements.textarea.value, 'Keep these words');
      blocked.resolve({ acceptedSequence: 0, epoch: 1 }); await flush();
    } finally { blocked.resolve({ acceptedSequence: 0, epoch: 1 }); await env.close(); }
  });
  await test('visibility, key loss and processor failure stop capture', async () => {
    for (const reason of ['hidden', 'key', 'processor']) {
      const env = environment();
      try {
        await env.controller.start(); await flush();
        if (reason === 'hidden') { env.document.hidden = true; env.document.dispatch('visibilitychange'); }
        if (reason === 'key') { env.allowed = false; env.controller.refresh(); }
        if (reason === 'processor') env.worklets[0].dispatch('processorerror');
        assert.equal(env.controller.active, false); assert(env.tracks.every(track => track.stopped));
      } finally { await env.close(); }
    }
  });
  await test('draft reducer drops stale epochs and Cancel keeps frozen manual edits', async () => {
    const model = new DraftModel('Typed'); model.beginTake();
    model.line({ epoch: 1, lineId: 'a', text: 'first phrase', complete: true });
    model.line({ epoch: 1, lineId: 'b', text: 'active phrase', complete: false });
    assert.deepEqual(model.provisionalRanges(), [{ start: 19, end: 32 }]);
    assert.equal(model.userEdit('Typed changed phrase active phrase').reset, false);
    assert.equal(model.userEdit('Typed changed phrase revised active phrase').reset, true);
    assert.equal(model.line({ epoch: 1, lineId: 'c', text: 'late', complete: false }), null);
    model.advanceEpoch(2); model.line({ epoch: 2, lineId: 'd', text: 'new speech', complete: false });
    model.cancelTake(); assert.equal(model.text, 'Typed changed phrase revised active phrase');
  });
  await test('recognition honors the current recipient message limit', async () => {
    const env = environment();
    try {
      env.elements.textarea.value = 'Typed';
      env.elements.textarea.maxLength = 12;
      await env.controller.start(); await flush();
      env.emit([env.line('this is too long')]); await flush();
      assert.equal(env.elements.textarea.value, 'Typed');
      assert.equal(env.controller.state, 'draft_limit'); assert(env.tracks.every(track => track.stopped));
    } finally { await env.close(); }
  });
  await test('Undo after Stop reverses the manual correction while keeping later speech', async () => {
    const env = environment({ stopText: 'the task today please' });
    try {
      const prefix = 'Please check old wording';
      input(env, prefix, { start: 0, type: 'insertFromPaste', after: prefix.length });
      assert.equal(historyKey(env).defaultPrevented, false, 'Ordinary editing keeps native history before dictation');
      await env.controller.start(); await flush(); env.emit([env.line('the task')]); await flush();
      const at = prefix.indexOf('old');
      input(env, env.elements.textarea.value.replace('old', 'new'), { start: at, end: at + 3, after: at + 3 });
      env.emit([env.line('the task today')]); await flush(); await env.controller.stop();
      assert.equal(env.elements.textarea.value, 'Please check new wording the task today please');
      const undo = historyKey(env); assert(undo.defaultPrevented);
      assert.equal(env.elements.textarea.value, 'Please check old wording the task today please');
      assert.equal(env.elements.textarea.selectionStart, at); assert.equal(env.elements.textarea.selectionEnd, at + 3);
      assert(historyKey(env, { redo: true }).defaultPrevented);
      assert.equal(env.elements.textarea.value, 'Please check new wording the task today please');
      assert.equal(env.controller.active, false);
    } finally { await env.close(); }
  });
  await test('typing a selected correction groups across intervening recognition updates', async () => {
    const env = environment();
    try {
      input(env, 'Please check old', { start: 0, type: 'insertFromPaste', after: 16 });
      await env.controller.start(); await flush(); env.emit([env.line('first speech')]); await flush();
      const textarea = env.elements.textarea, at = textarea.value.indexOf('old');
      input(env, textarea.value.replace('old', 'n'), { start: at, end: at + 3, after: at + 1 });
      env.emit([env.line('first speech grows')]); await flush();
      input(env, textarea.value.slice(0, at + 1) + 'e' + textarea.value.slice(at + 1), { start: at + 1, after: at + 2 });
      input(env, textarea.value.slice(0, at + 2) + 'w' + textarea.value.slice(at + 2), { start: at + 2, after: at + 3 });
      assert(historyKey(env, { ctrl: true }).defaultPrevented);
      assert.equal(textarea.value, 'Please check old first speech grows');
      env.emit([env.line('first speech grows again')]); await flush();
      assert(historyKey(env, { redo: true, ctrl: true }).defaultPrevented);
      assert.equal(textarea.value, 'Please check new first speech grows again');
    } finally { await env.close(); }
  });
  await test('IME commit is one manual undo entry and active composition keeps its own keys', async () => {
    const env = environment({ stopText: 'next words settled' });
    try {
      await env.controller.start(); await flush(); env.emit([env.line('first words')]); await flush();
      const textarea = env.elements.textarea; textarea.setSelectionRange(6, 11);
      textarea.dispatch('compositionstart'); textarea.value = 'first corrected'; textarea.setSelectionRange(15, 15);
      textarea.dispatch('input', { inputType: 'insertCompositionText', isComposing: true });
      assert.equal(historyKey(env).defaultPrevented, false);
      textarea.dispatch('compositionend'); await flush();
      env.emit([env.line('next words', false, 2, 'b')]); await flush();
      env.stopText = null;
      const stopping = env.controller.stop(); await stopping;
      assert(historyKey(env).defaultPrevented);
      assert.equal(textarea.value, 'first words next words');
      assert(historyKey(env, { redo: true }).defaultPrevented);
      assert.equal(textarea.value, 'first corrected next words');
    } finally { await env.close(); }
  });
  await test('Undo never crosses a recipient or programmatic draft replacement', async () => {
    const env = environment();
    try {
      input(env, 'Typed prefix', { start: 0, after: 12 });
      await env.controller.start(); await flush(); env.emit([env.line('speech')]); await flush();
      input(env, 'Changed prefix speech', { start: 0, end: 5, after: 7 });
      env.controller.beforeSend(); env.elements.textarea.value = 'Restored foreign draft';
      historyKey(env);
      assert.equal(env.elements.textarea.value, 'Restored foreign draft');
      env.controller.contextChanged();
      assert.equal(historyKey(env).defaultPrevented, false);
      assert.equal(env.elements.textarea.value, 'Restored foreign draft');
    } finally { await env.close(); }
  });
  await test('history menu actions use the same manual journal and respect readonly state', async () => {
    const env = environment();
    try {
      input(env, 'Original', { start: 0, after: 8 });
      await env.controller.start(); await flush(); env.emit([env.line('spoken words')]); await flush();
      input(env, 'Edited spoken words', { start: 0, end: 8, after: 6 });
      const event = { inputType: 'historyUndo', cancelable: true, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; } };
      env.elements.textarea.dispatch('beforeinput', event); assert(event.defaultPrevented);
      assert.equal(env.elements.textarea.value, 'Original spoken words');
      env.elements.textarea.readOnly = true;
      assert.equal(env.controller.redo(), false);
      assert.equal(env.elements.textarea.value, 'Original spoken words');
    } finally { await env.close(); }
  });
  await test('manual journal keeps recognition separate, uses UTF16 offsets, and bounds retained history', async () => {
    const journal = new ManualJournal('A😀B');
    journal.record('A😀C', { start: 3, end: 4 }, { start: 4, end: 4 }, 'insertText', 100);
    journal.enginePatch({ start: 4, end: 4, text: ' new speech' });
    assert.equal(journal.undo().text, 'A😀B new speech');
    assert.equal(journal.redo().text, 'A😀C new speech');
    const replace = new ManualJournal('original');
    replace.record('replacement', { start: 0, end: 8 }, { start: 11, end: 11 });
    replace.enginePatch({ start: 11, end: 11, text: ' latest words' });
    assert.equal(replace.undo().text, 'original latest words');
    assert.equal(replace.redo().text, 'replacement latest words');
    const bounded = new ManualJournal('', { maxEntries: 3, maxUnits: 100 });
    for (let index = 0; index < 20; index++) bounded.record(bounded.text + index, null, null, 'insertFromPaste');
    assert.equal(bounded.undoEntries.length, 3);
    const active = new ManualJournal('stable');
    for (let index = 0; index < 500; index++) active.enginePatch(textPatch(active.text, 'stable speech ' + index));
    assert(active.nodes.length < 30, 'Superseded recognition characters must not accumulate');
    assert.equal(active.undo(), null, 'Recognition is never a manual undo entry');
  });
  await test('PCM resampling is continuous, normalized, bounded and rate-correct', async () => {
    for (const rate of [16000, 44100, 48000, 96000]) {
      const packets = []; const resampler = new PCMResampler(rate, 16000, 1600, value => packets.push(value));
      for (let offset = 0; offset < rate; offset += 128) {
        const input = Float32Array.from({ length: Math.min(128, rate - offset) }, (_, index) => Math.sin(2 * Math.PI * 440 * (offset + index) / rate) * .4);
        resampler.feed(input);
      }
      resampler.flush(); const output = packets.flatMap(packet => [...packet]);
      assert(Math.abs(output.length - 16000) <= 1); assert(packets.every(packet => packet.length <= 1600));
      assert(output.every(value => Number.isFinite(value) && Math.abs(value) <= 1));
    }
    const input = Float32Array.from({ length: 48001 }, (_, index) => Math.sin(index / 17));
    function convert(size) {
      const output = []; const converter = new PCMResampler(48000, 16000, 1600, values => output.push(...values));
      for (let offset = 0; offset < input.length; offset += size) converter.feed(input.slice(offset, offset + size));
      converter.flush(); return output;
    }
    assert.deepEqual(convert(128), convert(777));
    for (const rate of [44100, 48000]) {
      const output = []; const converter = new PCMResampler(rate, 16000, 1600, packet => output.push(...packet));
      converter.feed(Float32Array.from({ length: rate }, (_, index) => Math.floor(index / 311) % 2 ? 1 : -1));
      converter.flush(); assert(output.every(value => Math.abs(value) <= 1));
    }
  });
  await test('causal mutations break the caret, layout-key, accessibility, motion, resume and lifecycle proofs', async () => {
    async function caught(name, mutate, proof, original = source) {
      assert.notEqual(mutate(original), original, name + ' must target the current implementation');
      await assert.rejects(async () => proof(mutate), assert.AssertionError, name + ' must fail its behavioral proof');
    }
    await caught('collapsed caret mapping', value => value.replace('textarea.setSelectionRange(mapped.start, mapped.end, mapped.direction);', ''), caretProof);
    await caught('native edit capture ordering', value => value.replace("bind(textarea, 'input', onEdit, true);", "bind(textarea, 'input', onEdit);"), legacyInputProof);
    await caught('semantic keyboard layout', value => value.replace("const undoKey = key === 'z', redoKey = key === 'y';",
      "const undoKey = letter === 'z' || event.code === 'KeyZ', redoKey = letter === 'y' || event.code === 'KeyY';"), layoutKeyProof);
    await caught('permanent live-region placement', value => value.replace('host.appendChild(announcer);', 'statusRow.appendChild(announcer);'), announcerProof, viewSource);
    await caught('live announcement deduplication', value => value.replace('announcement && key !== announcementKey', 'announcement'), announcerProof, viewSource);
    await caught('reduced-motion still meter', value => value.replace('const reduced = !!(motionQuery && motionQuery.matches);', 'const reduced = false;'), motionProof);
    await caught('resume after stream permission', value => value.replace("if (!await prepare(current, () => current.context.resume(), 'audio_start_failed')) return;", ''), resumeProof);
    await caught('bounded preparation deadline', value => value.replace('timer = setTimeout(() => reject(problem(code)), prepareTimeout);', 'timer = setTimeout(() => {}, prepareTimeout);'), resumeTimeoutProof);
    await caught('explicit interruption reason', value => value.replace('if (take) endForContext(take, reason);', 'if (take) abandon(take);'), contextReasonProof);
    await caught('Escape is Cancel', value => value.replace('cancel(); return true;', 'stop(); return true;'), escapeProof);
    await caught('Escape composer capture binding', value => value.replace("bind(escapeBoundary, 'keydown', onComposerEscape, true);", ''), focusedEscapeProof);
    await caught('Escape foreground precedence', value => value.replace('(options.canCancelEscape && !options.canCancelEscape())', 'false'), publicEscapeProof);
    await caught('Stop finishing cannot become Cancel', value => value.replace("state === 'finishing' || take.stopRequested || ", ''), finishingEscapeProof);
    await caught('legacy IME Escape guard', value => value.replace(' || event.keyCode === 229', ''), publicEscapeProof);
    await caught('controller keeps unavailable-Send notice visible', value => value.replace("['checking','preparing','listening'].includes(state) && notice ? notice : detail.message || ''", "detail.message || ''"), sendNoticeProof);
    await caught('view owns notice span visibility', value => value.replace('status.hidden = !message;', ''), mutate => sendNoticeProof(undefined, mutate), viewSource);
    await caught('finishing returns desk controls', value => value.replace("active = ['preparing','listening'].includes(state);", "active = ['preparing','listening','finishing'].includes(state);"), viewEndProof, viewSource);
    await caught('actual desk identity guard', value => value.replace('!canUse() || target() !== current.target', '!canUse() || false'), deskIdentityProof);
  });
  console.log('Dictation page checks passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
