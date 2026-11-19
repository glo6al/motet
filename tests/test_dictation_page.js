'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { DraftModel, ManualJournal, textPatch } = require('../src/web/dictation_draft.js');
const { PCMResampler } = require('../src/web/dictation_worklet.js');

const source = fs.readFileSync(path.join(__dirname, '../src/web/dictation.js'), 'utf8');
const draftSource = fs.readFileSync(path.join(__dirname, '../src/web/dictation_draft.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function flush() { for (let i = 0; i < 4; i++) await tick(); }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, handler) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(handler);
  }
  removeEventListener(name, handler) { this.listeners.get(name)?.delete(handler); }
  dispatch(name, value = {}) { for (const handler of [...(this.listeners.get(name) || [])]) handler(value); }
}
class Element extends Target {
  constructor(document) {
    super(); this.ownerDocument = document; this.style = {}; this.hidden = false; this.disabled = false;
    this.value = ''; this.maxLength = 50000; this.scrollTop = 17; this.scrollLeft = 3;
    this.selectionStart = 0; this.selectionEnd = 0; this.attributes = new Map(); this.children = [];
    const names = new Set();
    this.classList = { toggle(name, on) { if (on) names.add(name); else names.delete(name); },
      remove(name) { names.delete(name); }, contains(name) { return names.has(name); } };
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  appendChild(child) { child.parent = this; this.children.push(child); }
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
  const elements = Object.fromEntries(['textarea', 'mic', 'cancel', 'send', 'wave', 'status', 'settings'].map(name => [name, new Element(document)]));
  const env = { document, elements, demo: false, allowed: true, target: 'stage:all', readiness: 'ready', epoch: 1,
    calls: [], mediaCalls: 0, contexts: [], worklets: [], tracks: [], pendingEvents: [], nextId: 0, sequence: 0,
    patches: 0, stateChanges: [], sessionNumber: 0 };
  Object.assign(env, configuration);
  function newStream() {
    const track = new Target(); track.stopped = false;
    track.stop = () => { track.stopped = true; };
    env.tracks.push(track);
    return { getTracks: () => [track], getAudioTracks: () => [track] };
  }
  class AudioContext extends Target {
    constructor() {
      super(); this.state = 'running'; this.sampleRate = 48000; this.destination = {};
      this.audioWorklet = { addModule: url => { env.moduleUrl = url; return env.moduleDeferred ? env.moduleDeferred.promise : Promise.resolve(); } };
      env.contexts.push(this);
    }
    resume() { return Promise.resolve(); }
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
  Object.assign(browser, { document, isSecureContext: true, AudioContext, AudioWorkletNode,
    navigator: { mediaDevices: { getUserMedia: async () => {
      env.mediaCalls++;
      if (env.mediaError) throw env.mediaError;
      return env.mediaDeferred ? env.mediaDeferred.promise : newStream();
    } } },
    console, setTimeout, clearTimeout, setInterval, clearInterval, AbortController, ArrayBuffer,
    Float32Array, Float64Array, Date, Promise, Map, Set });
  vm.createContext(browser); vm.runInContext(draftSource, browser); vm.runInContext(source, browser);
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
    onDraftChange: () => { env.patches++; elements.textarea.dispatch('input'); },
    onState: (state, detail) => env.stateChanges.push({ state, detail })
  }));
  env.close = async () => { env.controller.destroy(); await flush(); };
  return env;
}

async function test(name, action) {
  await action(); console.log('PASS ' + name);
}

async function main() {
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
  console.log('Dictation page checks passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
