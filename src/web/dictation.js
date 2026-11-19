/* Local dictation controller. Only an explicit microphone gesture starts capture. */
(function (scope) {
  'use strict';
  const COPY = {
    not_installed: 'Dictation is not set up on this computer yet. Turn it on in Settings.',
    not_running: 'Dictation is not running. Start it, then press the microphone again.',
    loading: 'Dictation is loading on this computer. Try again in a moment.',
    busy: 'Dictation is already running. Stop it, then try again.',
    microphone_blocked: 'Motet cannot use the microphone. Allow it for this page in your browser’s settings, then press the microphone again.',
    microphone_missing: 'No microphone was found. Connect one and try again.',
    insecure_context: 'The microphone needs a secure connection. Open Motet using localhost or a trusted HTTPS connection.',
    unsupported: 'This browser does not support the microphone capture used by dictation.',
    unavailable: 'Dictation is unavailable here. Your draft has been kept.',
    take_limit: 'Dictation stopped after three minutes. Your words are kept; press the microphone to continue.',
    draft_limit: 'The message is full. Your words are kept; edit or send it before dictating more.',
    events_expired: 'Dictation lost its place. Your visible words are kept; start a new take.',
    failed: 'Dictation stopped. Your visible words are kept; press the microphone to try again.'
  };
  function problem(code) { const error = new Error(COPY[code] || COPY.failed); error.code = code; return error; }
  function create(options) {
    const textarea = options.textarea;
    if (!textarea || typeof options.api !== 'function') throw new TypeError('Dictation needs a textarea and a local API adapter.');
    const document = textarea.ownerDocument || scope.document;
    const DraftModel = scope.MotetDictationDraft && scope.MotetDictationDraft.DraftModel;
    const ManualJournal = scope.MotetDictationDraft && scope.MotetDictationDraft.ManualJournal;
    const textPatch = scope.MotetDictationDraft && scope.MotetDictationDraft.textPatch;
    if (!DraftModel || !ManualJournal || !textPatch) throw new TypeError('The dictation draft model must be loaded first.');
    const model = new DraftModel(textarea.value);
    const journal = new ManualJournal(textarea.value);
    const listeners = [], originalFont = textarea.style.fontStyle;
    const bars = [];
    const maximum = () => Number.isInteger(options.maxLength) && options.maxLength > 0 ? options.maxLength
      : textarea.maxLength > 0 ? textarea.maxLength : 64000;
    let state = 'idle', detail = {}, take = null, operation = 0, destroyed = false, composing = false;
    let cleanup = Promise.resolve(), applying = false, pendingInput = null, compositionSelection = null;
    const demo = () => !!(options.demo && options.demo());
    const canUse = () => !destroyed && !demo() && (!options.canUse || !!options.canUse());
    const target = () => options.target ? options.target() : '';
    const bind = (element, event, handler) => {
      if (!element) return;
      element.addEventListener(event, handler); listeners.push(() => element.removeEventListener(event, handler));
    };
    function notifyDraft() { if (options.onDraftChange) options.onDraftChange(textarea.value); }
    function selection() {
      return { start: textarea.selectionStart, end: textarea.selectionEnd, direction: textarea.selectionDirection || 'none' };
    }
    function changed() {
      applying = true;
      try { notifyDraft(); } finally { applying = false; }
      render();
    }
    function render() {
      const on = !!take;
      textarea.style.fontStyle = state === 'listening' ? 'italic' : originalFont || 'normal';
      textarea.classList.toggle('dictation-listening', state === 'listening');
      textarea.setAttribute('aria-busy', state === 'finishing' ? 'true' : 'false');
      if (options.mic) {
        options.mic.disabled = !canUse() || state === 'finishing';
        options.mic.setAttribute('aria-label', on ? 'Stop dictation' : 'Dictate a message');
        options.mic.setAttribute('aria-pressed', state === 'listening' ? 'true' : 'false');
        options.mic.classList.toggle('dictation-stop', on);
        options.mic.classList.toggle('dictation-blocked', state === 'microphone_blocked');
      }
      if (options.cancel) options.cancel.hidden = !on;
      if (options.wave) options.wave.hidden = !on;
      if (options.status) {
        options.status.textContent = detail.message || '';
        options.status.hidden = !detail.message;
      }
      if (options.settings) options.settings.hidden = state !== 'not_installed';
      if (options.onState) options.onState(state, Object.assign({}, detail, {
        active: on, composing, ranges: model.provisionalRanges(), epoch: take ? take.epoch : 0
      }));
    }
    function setState(next, value = {}) { state = next; detail = Object.assign({ code: next }, value); render(); }
    function patchText(patch, announce = true, source = 'engine') {
      if (!patch) return;
      const top = textarea.scrollTop, left = textarea.scrollLeft;
      if (source === 'engine') {
        if (journal.text !== textarea.value) journal.reset(textarea.value);
        journal.enginePatch(patch);
      }
      textarea.setRangeText(patch.text, patch.start, patch.end, 'preserve');
      textarea.scrollTop = top; textarea.scrollLeft = left;
      if (announce) changed();
    }
    function valid(current) { return !destroyed && take === current && !current.closed; }
    function still(current) {
      if (!valid(current)) return false;
      if (!canUse() || target() !== current.target) {
        abandon(current, 'idle'); return false;
      }
      if (!composing && !applying && textarea.value !== model.text) {
        journal.reset(textarea.value);
        abandon(current, 'idle'); return false;
      }
      return true;
    }
    async function request(path, value, extra = {}, current = null, polling = false) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 12000);
      if (polling && current) current.pollAbort = controller;
      try {
        let config = { method: value === undefined ? 'GET' : 'POST', signal: controller.signal,
          cache: 'no-store', credentials: 'same-origin' };
        if (value !== undefined) config = Object.assign(config, {
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value)
        });
        config = Object.assign(config, extra);
        const response = await options.api('/api/dictation/' + path, config);
        if (!response || typeof response !== 'object' || Array.isArray(response)) throw problem('failed');
        if (response.error) throw problem(response.error.code || 'failed');
        return response;
      } catch (error) {
        if (error && error.code) throw error;
        throw problem(error && error.name === 'TypeError' ? 'not_running' : 'failed');
      } finally {
        clearTimeout(timer);
        if (polling && current && current.pollAbort === controller) current.pollAbort = null;
      }
    }
    function releaseCapture(current) {
      if (!current) return;
      if (current.stream) for (const track of current.stream.getTracks()) track.stop();
      try { if (current.source) current.source.disconnect(); } catch (_) {}
      if (current.meterTimer) clearInterval(current.meterTimer);
      current.meterTimer = null;
      for (const bar of bars) bar.style.height = '2px';
    }
    function closeGraph(current) {
      if (!current) return;
      releaseCapture(current);
      try { if (current.worklet) current.worklet.disconnect(); } catch (_) {}
      try { if (current.analyser) current.analyser.disconnect(); } catch (_) {}
      if (current.context && current.context.state !== 'closed') current.context.close().catch(() => {});
    }
    function scheduleCancel(current) {
      if (!current || current.cancelScheduled || (!current.sessionId && !current.startPromise)) return;
      current.cancelScheduled = true;
      const prior = cleanup;
      cleanup = Promise.allSettled([prior, current.queue, current.startPromise]).then(() => {
        if (current.sessionId && !current.backendEnded) return request('cancel', { sessionId: current.sessionId }, { keepalive: true });
      }).catch(() => {});
    }
    function retire(current) {
      if (!current) return;
      current.closed = true; current.acceptFrames = false; current.acceptEvents = false;
      clearTimeout(current.finishTimer); clearTimeout(current.pollTimer);
      if (current.pollResolve) current.pollResolve();
      if (current.pollAbort) current.pollAbort.abort();
      if (current.flushResolver) current.flushResolver();
      closeGraph(current); scheduleCancel(current);
      if (current.resolveDone) current.resolveDone();
    }
    function abandon(current, next = 'idle', message) {
      if (current && take !== current) return;
      operation++; retire(current); take = null;
      journal.barrier();
      model.reset(textarea.value);
      setState(next, { message: message || COPY[next] || '' });
    }
    function fail(current, error) {
      if (!valid(current)) return;
      const code = error && error.code;
      const next = Object.prototype.hasOwnProperty.call(COPY, code) ? code : 'failed';
      abandon(current, next, COPY[next]);
    }
    function queue(current, task) {
      const next = current.queue.then(() => !still(current) || current.transportClosed ? undefined : task());
      current.queue = next.catch(error => { if (valid(current)) fail(current, error); });
      return next;
    }
    function pcm(current, buffer) {
      if (!still(current) || !current.acceptFrames) return;
      if (!(buffer instanceof ArrayBuffer) || !buffer.byteLength || buffer.byteLength > 64000 || buffer.byteLength % 4) {
        fail(current, problem('failed')); return;
      }
      const samples = buffer.byteLength / 4;
      current.lastFrameAt = Date.now();
      current.queuedSamples += samples;
      if (current.queuedSamples > 32000) { fail(current, problem('failed')); return; }
      const sequence = current.sequence++;
      queue(current, async () => {
        try {
          const response = await request('audio', null, { method: 'POST', body: buffer, headers: {
            'Content-Type': 'application/octet-stream', 'X-Dictation-Session': current.sessionId,
            'X-Dictation-Sequence': String(sequence)
          } });
          if (response.acceptedSequence !== sequence || response.epoch !== current.epoch) throw problem('failed');
        } finally { current.queuedSamples -= samples; }
      }).catch(() => {});
    }
    function beginMeter(current) {
      const values = new Float32Array(current.analyser.fftSize);
      current.meterTimer = setInterval(() => {
        if (!still(current)) return;
        if (state === 'listening' && Date.now() - current.lastFrameAt > 2500) { fail(current, problem('failed')); return; }
        current.analyser.getFloatTimeDomainData(values);
        let peak = 0;
        for (const sample of values) peak = Math.max(peak, Math.abs(sample));
        current.heights.shift(); current.heights.push(Math.min(24, 2 + Math.sqrt(peak) * 35));
        bars.forEach((bar, index) => { bar.style.height = `${current.heights[index]}px`; });
      }, 65);
    }
    function resetAfterEdit(current) {
      current.acceptEvents = false;
      current.editVersion++;
      if (state !== 'listening' || current.resetting) return;
      function enqueueReset() {
        if (!valid(current) || state !== 'listening') return;
        current.resetting = true;
        const version = current.editVersion;
        queue(current, async () => {
          const result = await request('reset', { sessionId: current.sessionId });
          if (!still(current)) return;
          if (!Number.isInteger(result.epoch) || result.epoch <= current.epoch) throw problem('failed');
          current.epoch = result.epoch;
          if (version !== current.editVersion) { current.resetting = false; enqueueReset(); return; }
          current.resetting = false; current.acceptEvents = true; model.advanceEpoch(result.epoch); render();
        }).catch(() => {});
      }
      enqueueReset();
    }
    function acceptLine(current, event) {
      if (!still(current) || !current.acceptEvents || event.epoch !== current.epoch) return;
      if (typeof event.text !== 'string' || event.text.length > 8192 || typeof event.lineId !== 'string'
          || event.lineId.length > 128 || typeof event.complete !== 'boolean') { fail(current, problem('failed')); return; }
      if (composing) {
        current.compositionEvents.set(`${event.epoch}:${event.lineId}`, event);
        if (current.compositionEvents.size > 32) fail(current, problem('draft_limit'));
        return;
      }
      if (textarea.value !== model.text) {
        journal.reset(textarea.value);
        abandon(current, 'idle'); return;
      }
      const patch = model.line(event);
      if (model.text.length > maximum()) { model.reset(textarea.value); fail(current, problem('draft_limit')); return; }
      if (patch) patchText(patch); else render();
    }
    function finish(current) {
      if (!valid(current)) return;
      current.backendEnded = true; current.transportClosed = true; current.acceptFrames = false;
      clearTimeout(current.finishTimer); closeGraph(current);
      if (composing) { current.deferredEnd = true; setState('finishing', { message: 'Finishing the last words…' }); return; }
      current.closed = true; current.acceptEvents = false;
      take = null; model.commitTake();
      journal.barrier();
      if (current.resolveDone) current.resolveDone();
      setState('idle');
    }
    function pausePoll(current) {
      return new Promise(resolve => {
        current.pollResolve = resolve;
        current.pollTimer = setTimeout(() => { current.pollResolve = null; resolve(); }, 60);
      });
    }
    async function poll(current) {
      while (still(current)) {
        let result;
        try { result = await request('events', { sessionId: current.sessionId, after: current.after }, {}, current, true); }
        catch (error) { fail(current, error); return; }
        if (!still(current)) return;
        if (!Array.isArray(result.events) || result.events.length > 512) { fail(current, problem('failed')); return; }
        for (const event of result.events) {
          if (!event || !Number.isInteger(event.id) || event.id < 1) { fail(current, problem('failed')); return; }
          if (event.id <= current.after) continue;
          current.after = event.id;
          if (event.type === 'line') acceptLine(current, event);
          else if (event.type === 'error') { fail(current, problem(event.code || 'failed')); return; }
          else if (event.type === 'ended') { finish(current); return; }
          else { fail(current, problem('failed')); return; }
          if (!valid(current)) return;
        }
        if (result.ended) { finish(current); return; }
        if (!result.events.length) await pausePoll(current);
      }
    }
    async function start() {
      if (!canUse() || take) return;
      if (journal.text !== textarea.value) journal.reset(textarea.value);
      journal.barrier();
      const token = ++operation;
      const current = { token, target: target(), sessionId: null, epoch: 1, sequence: 0, after: 0,
        queue: Promise.resolve(), queuedSamples: 0, closed: false, acceptFrames: false, acceptEvents: true,
        heights: Array(130).fill(2), compositionEvents: new Map(), resetting: false, editVersion: 0 };
      current.done = new Promise(resolve => { current.resolveDone = resolve; });
      take = current; model.reset(textarea.value); model.beginTake(); setState('checking', { message: 'Checking dictation…' });
      try {
        await cleanup;
        if (!still(current) || token !== operation) return;
        const availability = await request('status');
        if (!still(current) || token !== operation) return;
        if (availability.state === 'failed') {
          // This explicit gesture can ask the helper to reload a failed worker.
          // Preparation must finish before a later gesture can request a mic.
          current.startPromise = request('start', { intervalSeconds: .25 }).then(result => {
            current.sessionId = result.sessionId; return result;
          });
          await current.startPromise;
          // A competing client may have made the helper ready between status
          // and start. Retire any returned session without capturing audio.
          if (still(current)) throw problem('loading');
          scheduleCancel(current); return;
        }
        if (availability.state !== 'ready') {
          const code = ['not_installed', 'not_running', 'loading'].includes(availability.state)
            ? availability.state : availability.state === 'listening' || availability.state === 'finishing' ? 'busy' : 'failed';
          throw problem(code);
        }
        if (!scope.isSecureContext || !scope.navigator || !scope.navigator.mediaDevices || !scope.navigator.mediaDevices.getUserMedia) throw problem('insecure_context');
        if (!scope.AudioContext || !scope.AudioWorkletNode) throw problem('unsupported');
        setState('preparing', { message: 'Allow microphone access for this page, then speak when it is listening.' });
        current.context = new scope.AudioContext();
        const resume = current.context.resume(); resume.catch(() => {});
        const stream = await scope.navigator.mediaDevices.getUserMedia({ audio: {
          channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
        }, video: false });
        current.stream = stream;
        if (!still(current) || token !== operation) { closeGraph(current); return; }
        await resume;
        if (!still(current) || token !== operation) return;
        await current.context.audioWorklet.addModule(options.workletUrl || '/dictation-worklet.js');
        if (!still(current) || token !== operation) return;
        current.startPromise = request('start', { intervalSeconds: .25 }).then(result => {
          current.sessionId = result.sessionId; return result;
        });
        const result = await current.startPromise;
        if (!still(current) || token !== operation) { scheduleCancel(current); return; }
        if (typeof result.sessionId !== 'string' || !/^[a-zA-Z0-9_-]{16,128}$/.test(result.sessionId)
            || !Number.isInteger(result.epoch) || result.epoch < 1) throw problem('failed');
        current.epoch = result.epoch; model.advanceEpoch(result.epoch);
        current.worklet = new scope.AudioWorkletNode(current.context, 'motet-dictation-pcm', {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]
        });
        if (current.worklet.addEventListener) current.worklet.addEventListener('processorerror', () => {
          if (valid(current)) fail(current, problem('failed'));
        });
        current.worklet.port.onmessage = ({ data }) => {
          if (!data || !valid(current)) return;
          if (data.type === 'pcm') pcm(current, data.buffer);
          else if (data.type === 'flushed' && current.flushResolver) current.flushResolver();
        };
        current.source = current.context.createMediaStreamSource(stream);
        current.analyser = current.context.createAnalyser(); current.analyser.fftSize = 256;
        current.source.connect(current.analyser); current.source.connect(current.worklet);
        current.worklet.connect(current.context.destination);
        current.acceptFrames = true; current.lastFrameAt = Date.now();
        for (const track of stream.getAudioTracks()) track.addEventListener('ended', () => {
          if (valid(current) && state === 'listening') fail(current, problem('failed'));
        });
        if (current.context.addEventListener) current.context.addEventListener('statechange', () => {
          if (valid(current) && state === 'listening' && current.context.state !== 'running') fail(current, problem('failed'));
        });
        beginMeter(current); setState('listening'); poll(current);
      } catch (error) {
        if (!valid(current)) { closeGraph(current); return; }
        if (error && error.name === 'NotAllowedError') error = problem('microphone_blocked');
        else if (error && error.name === 'NotFoundError') error = problem('microphone_missing');
        fail(current, error);
      }
    }
    async function stop() {
      const current = take;
      if (!current) return;
      journal.barrier();
      if (state === 'finishing') return current.done;
      if (state !== 'listening') { abandon(current); return; }
      setState('finishing', { message: 'Finishing the last words…' });
      releaseCapture(current);
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => { current.flushResolver = null; reject(problem('failed')); }, 750);
          current.flushResolver = () => { current.flushResolver = null; clearTimeout(timer); resolve(); };
          current.worklet.port.postMessage({ type: 'flush', requestId: 1 });
        });
        current.acceptFrames = false; closeGraph(current);
        if (!still(current)) return;
        await queue(current, () => request('stop', { sessionId: current.sessionId }));
        if (valid(current)) current.finishTimer = setTimeout(() => fail(current, problem('failed')), 8000);
      } catch (error) { fail(current, error); }
      return current.done;
    }
    function cancel() {
      const current = take;
      if (!current) return;
      if (!composing && textarea.value !== model.text) { journal.reset(textarea.value); abandon(current); return; }
      model.userEdit(textarea.value);
      const patches = model.cancelTake();
      operation++; retire(current); take = null;
      for (const patch of patches) patchText(patch, false);
      model.reset(textarea.value); setState('idle');
      journal.barrier();
      if (patches.some(Boolean)) changed();
    }
    function beforeSend() {
      const visible = textarea.value;
      journal.barrier();
      if (take) abandon(take);
      else { model.reset(visible); render(); }
      return visible;
    }
    function applyHistory(direction) {
      if (destroyed || composing || textarea.readOnly || textarea.disabled || !journal.enabled) return false;
      if (journal.text !== textarea.value) {
        journal.reset(textarea.value);
        if (take) abandon(take); else model.reset(textarea.value);
        return false;
      }
      pendingInput = null;
      const value = journal[direction]();
      if (!value) return false;
      const patch = textPatch(textarea.value, value.text), result = model.userEdit(value.text);
      if (patch) patchText(patch, true, 'history'); else render();
      if (typeof textarea.setSelectionRange === 'function') {
        const top = textarea.scrollTop, left = textarea.scrollLeft;
        textarea.setSelectionRange(value.selection.start, value.selection.end, value.selection.direction);
        textarea.scrollTop = top; textarea.scrollLeft = left;
      }
      if (result.reset && take) resetAfterEdit(take);
      return true;
    }
    function onBeforeInput(event) {
      if (applying || composing || event.isComposing) return;
      if (journal.enabled && ['historyUndo', 'historyRedo'].includes(event.inputType)) {
        if (event.cancelable === false) {
          pendingInput = { type: event.inputType, selection: selection(), nativeHistory: true }; return;
        }
        event.preventDefault();
        applyHistory(event.inputType === 'historyUndo' ? 'undo' : 'redo');
        return;
      }
      if (journal.text !== textarea.value) journal.reset(textarea.value);
      pendingInput = { type: event.inputType || 'manual', selection: selection() };
    }
    function onHistoryKey(event) {
      if (destroyed || composing || textarea.readOnly || textarea.disabled || event.isComposing || event.altKey
          || !(event.metaKey || event.ctrlKey) || !journal.enabled) return;
      const key = String(event.key || '').toLowerCase();
      const undoKey = key === 'z' || event.code === 'KeyZ';
      const redoKey = key === 'y' || event.code === 'KeyY';
      if (!undoKey && !redoKey) return;
      event.preventDefault(); if (event.stopPropagation) event.stopPropagation();
      applyHistory(redoKey || event.shiftKey ? 'redo' : 'undo');
    }
    function onEdit(event = {}) {
      if (applying) return;
      if (!composing) {
        const type = event.inputType || pendingInput && pendingInput.type || 'manual';
        if ((!journal.enabled || pendingInput && pendingInput.nativeHistory) && ['historyUndo', 'historyRedo'].includes(type)) {
          journal.nativeHistory(textarea.value, type === 'historyUndo' ? 'undo' : 'redo');
        } else {
          journal.record(textarea.value, pendingInput && pendingInput.selection, selection(), type);
        }
        pendingInput = null;
        const result = model.userEdit(textarea.value);
        if (result.reset && take) resetAfterEdit(take);
      }
      render();
    }
    function contextChanged() {
      if (take) abandon(take);
      model.reset(textarea.value); journal.reset(textarea.value); pendingInput = null; render();
    }
    function refresh() { if (take && !canUse()) abandon(take); render(); }
    function destroy() {
      if (destroyed) return;
      if (take) abandon(take);
      destroyed = true; operation++;
      for (const unbind of listeners.splice(0)) unbind();
      textarea.style.fontStyle = originalFont;
      textarea.classList.remove('dictation-listening');
      textarea.removeAttribute('aria-busy');
      if (options.mic) options.mic.disabled = true;
      for (const bar of bars) bar.remove();
      journal.reset('');
    }
    if (options.wave && document) {
      for (let index = 0; index < 130; index++) {
        const bar = document.createElement('span'); bar.className = 'dictation-wave-bar';
        bar.style.height = '2px'; options.wave.appendChild(bar); bars.push(bar);
      }
      options.wave.setAttribute('role', 'img'); options.wave.setAttribute('aria-label', 'Listening');
    }
    bind(options.mic, 'click', () => { if (take) stop(); else start(); });
    bind(options.cancel, 'click', cancel);
    bind(textarea, 'beforeinput', onBeforeInput);
    bind(textarea, 'keydown', onHistoryKey);
    bind(textarea, 'input', onEdit);
    bind(textarea, 'compositionstart', () => {
      if (journal.text !== textarea.value) journal.reset(textarea.value);
      compositionSelection = selection(); pendingInput = null; journal.barrier();
      composing = true; render();
    });
    bind(textarea, 'compositionend', () => {
      composing = false; pendingInput = { type: 'insertCompositionText', selection: compositionSelection };
      compositionSelection = null; onEdit({ inputType: 'insertCompositionText' }); journal.barrier();
      if (!take) return;
      const current = take;
      for (const event of current.compositionEvents.values()) acceptLine(current, event);
      current.compositionEvents.clear();
      if (current.deferredEnd) finish(current);
    });
    bind(document, 'visibilitychange', () => { if (document.hidden && take) abandon(take); });
    bind(scope, 'pagehide', () => {
      if (take) abandon(take);
    });
    render();
    return { start, stop, cancel, beforeSend, contextChanged, refresh, destroy,
      undo: () => applyHistory('undo'), redo: () => applyHistory('redo'),
      get active() { return !!take; }, get state() { return state; },
      get ranges() { return model.provisionalRanges(); }, get composing() { return composing; } };
  }
  scope.MotetDictation = { create };
  if (typeof module !== 'undefined' && module.exports) module.exports = { create };
})(typeof globalThis !== 'undefined' ? globalThis : this);
