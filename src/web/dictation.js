/* Local dictation controller. Only an explicit microphone gesture starts capture. */
(function (scope) {
  'use strict';
  const COPY = {
    not_installed: 'Dictation is not set up on this computer yet. Turn it on in Settings.',
    not_running: 'Dictation is not running. Start it, then press the microphone again.',
    loading: 'Dictation is loading on this computer. Try again in a moment.',
    busy: 'Dictation is busy. If another window is dictating, stop it there. Otherwise wait a moment and try again.',
    control_unconfirmed: 'The microphone is off and your words are kept. Dictation did not confirm that it stopped, so wait a moment before dictating again.',
    finalize_unconfirmed: 'Dictation stopped, but the last words may be missing. The words you can see are kept.',
    microphone_blocked: 'Motet cannot use the microphone. Allow it for this page in your browser’s settings, then press the microphone again.',
    microphone_missing: 'No microphone was found. Connect one and try again.',
    insecure_context: 'The microphone needs a secure connection. Open Motet using localhost or a trusted HTTPS connection.',
    unsupported: 'This browser does not support the microphone capture used by dictation.',
    unavailable: 'Dictation is unavailable here. Your draft has been kept.',
    take_limit: 'Dictation stopped after three minutes. Your words are kept; press the microphone to continue.',
    draft_limit: 'The message is full. Your words are kept; edit or send it before dictating more.',
    events_expired: 'Dictation lost its place. Your visible words are kept; start a new take.',
    backlog: 'Dictation stopped because recognition fell behind. Your words are kept.',
    capture_failed: 'Dictation stopped because audio capture failed. Your words are kept.',
    audio_start_failed: 'Dictation could not start because the microphone did not become ready. Your words are kept.',
    audio_interrupted: 'Dictation stopped because the microphone was interrupted. Your words are kept.',
    timeout: 'Dictation stopped because the local recognizer did not answer. Your words are kept.',
    failed: 'Dictation stopped because the local recognizer failed. Your words are kept; press the microphone to try again.'
  };
  const END_REASON = {
    recipient: 'Dictation stopped when you changed the recipient.',
    desk: 'Dictation stopped when you changed desks.',
    scope: 'Dictation stopped when you changed the scope.',
    hidden: 'Dictation stopped when you left this tab.',
    locked: 'Dictation stopped when you locked the stage.',
    keychanged: 'Dictation stopped because the owner key changed.',
    draft_changed: 'Dictation stopped because the draft changed elsewhere.',
    unavailable: 'Dictation stopped because this message box became unavailable.',
    destroyed: 'Dictation stopped because this message box closed.'
  };
  function problem(code) { const error = new Error(COPY[code] || COPY.failed); error.code = code; return error; }
  function create(options) {
    const textarea = options.textarea;
    if (!textarea || typeof options.api !== 'function') throw new TypeError('Dictation needs a textarea and a local API adapter.');
    const document = textarea.ownerDocument || scope.document;
    const DraftModel = scope.MotetDictationDraft && scope.MotetDictationDraft.DraftModel;
    const ManualJournal = scope.MotetDictationDraft && scope.MotetDictationDraft.ManualJournal;
    const textPatch = scope.MotetDictationDraft && scope.MotetDictationDraft.textPatch;
    const mapSelection = scope.MotetDictationDraft && scope.MotetDictationDraft.mapSelection;
    if (!DraftModel || !ManualJournal || !textPatch || !mapSelection) throw new TypeError('The dictation draft model must be loaded first.');
    const model = new DraftModel(textarea.value);
    const journal = new ManualJournal(textarea.value);
    const listeners = [], originalFont = textarea.style.fontStyle;
    const bars = [];
    const motionQuery = scope.matchMedia ? scope.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const prepareTimeout = Number.isInteger(options.prepareTimeoutMs) && options.prepareTimeoutMs > 0 && options.prepareTimeoutMs <= 10000
      ? options.prepareTimeoutMs : 3000;
    const maximum = () => Number.isInteger(options.maxLength) && options.maxLength > 0 ? options.maxLength
      : textarea.maxLength > 0 ? textarea.maxLength : 64000;
    let state = 'idle', detail = {}, take = null, operation = 0, destroyed = false, composing = false;
    let feedbackVersion = 0, statusVersion = 0;
    let cleanup = Promise.resolve(), applying = false, pendingInput = null, compositionSelection = null;
    const TERMINAL_BUDGET_MS = 20000, BUSY_RETRY_MS = 100;
    const now = scope.performance && typeof scope.performance.now === 'function' ? scope.performance.now.bind(scope.performance) : null;
    function terminalDeadline(current) {
      if (current.terminalDeadline === undefined) current.terminalDeadline = now() + TERMINAL_BUDGET_MS;
      return current.terminalDeadline;
    }
    function withinDeadline(promise, deadline) {
      const remaining = deadline - now();
      if (remaining <= 0) return Promise.reject(problem('control_unconfirmed'));
      let timer;
      const expired = new Promise((resolve, reject) => { timer = setTimeout(() => reject(problem('control_unconfirmed')), remaining); });
      return Promise.race([promise, expired]).finally(() => clearTimeout(timer));
    }
    const demo = () => !!(options.demo && options.demo());
    const canUse = () => !destroyed && !demo() && (!options.canUse || !!options.canUse());
    const target = () => options.target ? options.target() : '';
    const bind = (element, event, handler, capture = false) => {
      if (!element) return;
      element.addEventListener(event, handler, capture); listeners.push(() => element.removeEventListener(event, handler, capture));
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
        const notice = typeof detail.notice === 'string' ? detail.notice : '';
        const message = ['checking','preparing','listening'].includes(state) && notice ? notice : detail.message || '';
        options.status.textContent = message;
        options.status.hidden = !message;
      }
      if (options.settings) options.settings.hidden = state !== 'not_installed';
      if (options.onState) options.onState(state, Object.assign({}, detail, {
        active: on, composing, ranges: model.provisionalRanges(), epoch: take ? take.epoch : 0
      }));
    }
    function setState(next, value = {}) {
      statusVersion++;
      state = next; detail = Object.assign({ code: next, notice: take ? take.notice : '' }, value); render();
    }
    function patchText(patch, announce = true, source = 'engine') {
      if (!patch) return;
      const top = textarea.scrollTop, left = textarea.scrollLeft;
      const mapped = mapSelection(patch, selection(), textarea.value.length);
      if (source === 'engine') {
        if (journal.text !== textarea.value) journal.reset(textarea.value);
        journal.enginePatch(patch);
      }
      textarea.setRangeText(patch.text, patch.start, patch.end, 'preserve');
      textarea.setSelectionRange(mapped.start, mapped.end, mapped.direction);
      textarea.scrollTop = top; textarea.scrollLeft = left;
      if (announce) changed();
    }
    function valid(current) { return !destroyed && take === current && !current.closed; }
    function still(current) {
      if (!valid(current)) return false;
      if (!canUse() || target() !== current.target) {
        endForContext(current, canUse() ? 'recipient' : 'unavailable'); return false;
      }
      if (!composing && !applying && textarea.value !== model.text) {
        journal.reset(textarea.value);
        endForContext(current, 'draft_changed'); return false;
      }
      return true;
    }
    async function request(path, value, extra = {}, current = null, polling = false, deadline = null) {
      const timeout = deadline === null ? 12000 : Math.min(12000, deadline - now());
      if (timeout <= 0) throw problem('control_unconfirmed');
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      if (polling && current) current.pollAbort = controller;
      if (path === 'stop' && current) current.stopAbort = controller;
      let onAbort;
      const aborted = new Promise((resolve, reject) => {
        onAbort = () => reject(problem('timeout')); controller.signal.addEventListener('abort', onAbort, { once: true });
      });
      try {
        let config = { method: value === undefined ? 'GET' : 'POST', signal: controller.signal,
          cache: 'no-store', credentials: 'same-origin' };
        if (value !== undefined) config = Object.assign(config, {
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value)
        });
        config = Object.assign(config, extra);
        const response = await Promise.race([Promise.resolve().then(() => options.api('/api/dictation/' + path, config)), aborted]);
        if (!response || typeof response !== 'object' || Array.isArray(response)) throw problem('failed');
        if (response.error) throw problem(response.error.code || 'failed');
        return response;
      } catch (error) {
        if (error && error.code) throw error;
        throw problem(error && error.name === 'AbortError' ? 'timeout' : error && error.name === 'TypeError' ? 'not_running' : 'failed');
      } finally {
        clearTimeout(timer);
        controller.signal.removeEventListener('abort', onAbort);
        if (polling && current && current.pollAbort === controller) current.pollAbort = null;
        if (current && current.stopAbort === controller) current.stopAbort = null;
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
    async function terminalRequest(current, action, deadline) {
      while (now() < deadline) {
        if (action === 'stop' && (!valid(current) || current.backendEnded)) return;
        try {
          const result = await request(action, { sessionId: current.sessionId }, action === 'cancel' ? { keepalive: true } : {},
            action === 'stop' ? current : null, false, deadline);
          if (result.sessionId !== current.sessionId || result.ended !== true) throw problem('control_unconfirmed');
          if (action === 'stop') current.stopAcknowledged = true;
          return result;
        } catch (error) {
          if (action === 'stop' && (!valid(current) || current.backendEnded)) return;
          if (action === 'cancel' && error && ['ended','unknown_session'].includes(error.code)) return;
          if (action === 'stop' && error && error.code === 'ended') { current.stopAcknowledged = true;return; } // Still drain final events.
          if (action === 'stop' && error && error.code === 'unknown_session') throw problem('finalize_unconfirmed');
          if (action === 'stop' && now() >= deadline) throw problem('finalize_unconfirmed');
          if (!error || error.code !== 'busy') throw error;
          if (action === 'stop' && (!valid(current) || current.backendEnded)) return;
          const remaining = deadline - now();
          if (remaining <= 0) break;
          await withinDeadline(new Promise(resolve => setTimeout(resolve, Math.min(BUSY_RETRY_MS, remaining))), deadline);
        }
      }
      throw problem(action === 'stop' ? 'finalize_unconfirmed' : 'control_unconfirmed');
    }
    function rememberCleanup(current) {
      if (current && current.cancelScheduled) current.cleanupFeedback = {
        target: target(), text: textarea.value, feedback: feedbackVersion, status: statusVersion
      };
    }
    function cleanupWarning(current, error) {
      const stamp = current.cleanupFeedback;
      if (destroyed || composing || !stamp || target() !== stamp.target
          || current.target !== stamp.target || textarea.value !== stamp.text || feedbackVersion !== stamp.feedback
          || statusVersion !== stamp.status || [COPY.control_unconfirmed,COPY.finalize_unconfirmed].includes(detail.message)) return;
      // Only the adapter can attest that Lock or an owner-key claim refused locally.
      if (['locked','keychanged'].includes(detail.reason) && error && error.code === 'unavailable' && !canUse()
          && options.isLocalRefusal && options.isLocalRefusal(error) === true) return;
      setState('idle', { code: 'control_unconfirmed', reason: 'cleanup_unconfirmed',
        message: COPY.control_unconfirmed, announcement: COPY.control_unconfirmed });
    }
    function scheduleCancel(current) {
      if (!current || current.cancelScheduled || current.backendEnded || current.stopAcknowledged || (!current.sessionId && !current.startPromise)) return;
      current.cancelScheduled = true;
      const prior = cleanup, deadline = terminalDeadline(current);
      cleanup = withinDeadline(Promise.allSettled([prior, current.queue, current.startPromise]), deadline).then(() => {
        if (current.sessionId && !current.backendEnded && !current.stopAcknowledged) return terminalRequest(current, 'cancel', deadline);
        if (!current.sessionId && current.startUnconfirmed) throw problem('control_unconfirmed');
      }).catch(error => cleanupWarning(current, error));
    }
    function retire(current) {
      if (!current) return;
      current.closed = true; current.acceptFrames = false; current.acceptEvents = false;
      clearTimeout(current.finishTimer); clearTimeout(current.pollTimer);
      if (current.pollResolve) current.pollResolve();
      if (current.pollAbort) current.pollAbort.abort();
      if (current.stopAbort) current.stopAbort.abort();
      if (current.flushResolver) current.flushResolver();
      for (const cancel of current.prepareWaiters || []) cancel();
      closeGraph(current); scheduleCancel(current);
      if (current.resolveDone) current.resolveDone();
    }
    function abandon(current, next = 'idle', message, ending = {}) {
      if (current && take !== current) return;
      operation++; retire(current); take = null;
      journal.barrier();
      model.reset(textarea.value);
      const shown = message === undefined ? COPY[next] || '' : message;
      setState(next, Object.assign({ message: shown, reason: next, announcement: shown }, ending));
      rememberCleanup(current);
    }
    function endForContext(current, reason) {
      const code = Object.prototype.hasOwnProperty.call(END_REASON, reason) ? reason : 'recipient';
      abandon(current, 'idle', END_REASON[code], { reason: code, announcement: END_REASON[code] });
    }
    function fail(current, error) {
      if (!valid(current)) return;
      const code = error && error.code;
      const next = Object.prototype.hasOwnProperty.call(COPY, code) ? code : 'failed';
      const message = current.hasListened && code === 'not_running'
        ? 'Dictation stopped because the local recognizer is not running. Your words are kept.' : COPY[next];
      abandon(current, next, message, { reason: typeof code === 'string' ? code : 'failed', announcement: message });
    }
    function queue(current, task) {
      const next = current.queue.then(() => !still(current) || current.transportClosed ? undefined : task());
      current.queue = next.catch(error => { if (valid(current)) fail(current, error); });
      return next;
    }
    function pcm(current, buffer) {
      if (!still(current) || !current.acceptFrames) return;
      if (!(buffer instanceof ArrayBuffer) || !buffer.byteLength || buffer.byteLength > 64000 || buffer.byteLength % 4) {
        fail(current, problem('capture_failed')); return;
      }
      const samples = buffer.byteLength / 4;
      current.lastFrameAt = Date.now();
      current.queuedSamples += samples;
      if (current.queuedSamples > 32000) { fail(current, problem('backlog')); return; }
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
      if (current.meterTimer) clearInterval(current.meterTimer);
      const reduced = !!(motionQuery && motionQuery.matches);
      if (options.wave) options.wave.classList.toggle('dictation-wave-still', reduced);
      if (reduced) bars.forEach((bar, index) => { bar.style.height = (index % 11 === 5 ? 6 : index % 11 === 4 || index % 11 === 6 ? 4 : 2) + 'px'; });
      const values = new Float32Array(current.analyser.fftSize);
      current.meterTimer = setInterval(() => {
        if (!still(current)) return;
        if (state === 'listening' && Date.now() - current.lastFrameAt > 2500) { fail(current, problem('capture_failed')); return; }
        if (reduced) return;
        current.analyser.getFloatTimeDomainData(values);
        let peak = 0;
        for (const sample of values) peak = Math.max(peak, Math.abs(sample));
        current.heights.shift(); current.heights.push(Math.min(24, 2 + Math.sqrt(peak) * 35));
        bars.forEach((bar, index) => { bar.style.height = `${current.heights[index]}px`; });
      }, reduced ? 500 : 65);
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
        endForContext(current, 'draft_changed'); return;
      }
      const patch = model.line(event);
      if (model.text.length > maximum()) { model.reset(textarea.value); fail(current, problem('draft_limit')); return; }
      if (patch) patchText(patch); else render();
    }
    function finish(current) {
      if (!valid(current)) return;
      current.backendEnded = true; current.transportClosed = true; current.acceptFrames = false;
      if (current.stopAbort) current.stopAbort.abort();
      clearTimeout(current.finishTimer); closeGraph(current);
      if (composing) { current.deferredEnd = true; setState('finishing', { message: 'Finishing the last words…' }); return; }
      current.closed = true; current.acceptEvents = false;
      take = null; model.commitTake();
      journal.barrier();
      if (current.resolveDone) current.resolveDone();
      const message = current.stopRequested ? '' : 'Dictation stopped because the local recognizer ended this take. Your words are kept.';
      setState('idle', { message, reason: current.stopRequested ? 'stop' : 'ended', announcement: message || 'Dictation stopped' });
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
    function prepare(current, action, code) {
      let timer, cancel;
      const pending = new Promise((resolve, reject) => {
        cancel = () => resolve(false); current.prepareWaiters.add(cancel);
        timer = setTimeout(() => reject(problem(code)), prepareTimeout);
        Promise.resolve().then(action).then(() => resolve(true), () => reject(problem(code)));
      });
      return pending.finally(() => { clearTimeout(timer); current.prepareWaiters.delete(cancel); });
    }
    function startRequest(current) {
      return request('start', { intervalSeconds: .25 }).then(result => {
        if (typeof result.sessionId !== 'string' || !/^[a-zA-Z0-9_-]{16,128}$/.test(result.sessionId)) {
          current.startUnconfirmed = true; throw problem('control_unconfirmed');
        }
        current.sessionId = result.sessionId; return result;
      }).catch(error => {
        // An ambiguous Start cannot be cancelled safely without its session ID.
        const refused = error && ['busy','loading','not_configured','not_installed','dependency_missing',
          'runtime_version','model_missing_or_invalid','demo'].includes(error.code);
        if (!refused) current.startUnconfirmed = true;
        throw error;
      });
    }
    async function start() {
      if (!canUse() || take) return;
      if (!now) { setState('unsupported', { message: COPY.unsupported }); return; }
      if (journal.text !== textarea.value) journal.reset(textarea.value);
      journal.barrier();
      const token = ++operation;
      const current = { token, target: target(), sessionId: null, epoch: 1, sequence: 0, after: 0,
        queue: Promise.resolve(), queuedSamples: 0, closed: false, acceptFrames: false, acceptEvents: true,
        heights: Array(130).fill(2), compositionEvents: new Map(), resetting: false, editVersion: 0,
        prepareWaiters: new Set(), notice: options.captureNotice ? String(options.captureNotice() || '').slice(0, 1024) : '' };
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
          current.startPromise = startRequest(current);
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
        try { Promise.resolve(current.context.resume()).catch(() => {}); } catch (_) {}
        if (!await prepare(current, () => current.context.audioWorklet.addModule(options.workletUrl || '/dictation-worklet.js'), 'capture_failed')) return;
        if (!still(current) || token !== operation) return;
        const stream = await scope.navigator.mediaDevices.getUserMedia({ audio: {
          channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
        }, video: false });
        current.stream = stream;
        if (!still(current) || token !== operation) { closeGraph(current); return; }
        if (!await prepare(current, () => current.context.resume(), 'audio_start_failed')) return;
        if (current.context.state !== 'running') throw problem('audio_start_failed');
        if (!still(current) || token !== operation) return;
        current.startPromise = startRequest(current);
        const result = await current.startPromise;
        if (!still(current) || token !== operation) { scheduleCancel(current); return; }
        if (typeof result.sessionId !== 'string' || !/^[a-zA-Z0-9_-]{16,128}$/.test(result.sessionId)
            || !Number.isInteger(result.epoch) || result.epoch < 1) throw problem('failed');
        current.epoch = result.epoch; model.advanceEpoch(result.epoch);
        current.worklet = new scope.AudioWorkletNode(current.context, 'motet-dictation-pcm', {
          numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]
        });
        if (current.worklet.addEventListener) current.worklet.addEventListener('processorerror', () => {
          if (valid(current)) fail(current, problem('capture_failed'));
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
          if (valid(current) && state === 'listening') fail(current, problem('audio_interrupted'));
        });
        if (current.context.addEventListener) current.context.addEventListener('statechange', () => {
          if (valid(current) && state === 'listening' && current.context.state !== 'running') fail(current, problem('audio_interrupted'));
        });
        current.hasListened = true; beginMeter(current); setState('listening'); poll(current).catch(error => fail(current, error));
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
      const deadline = terminalDeadline(current);
      current.stopRequested = true;
      if (state !== 'listening') { abandon(current, 'idle', '', { reason: 'stop', announcement: 'Dictation stopped' }); return; }
      setState('finishing', { message: 'Finishing the last words…' });
      releaseCapture(current);
      current.finishTimer = setTimeout(() => fail(current, problem('finalize_unconfirmed')), Math.max(0, deadline - now()));
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => { current.flushResolver = null; reject(problem('failed')); }, 750);
          current.flushResolver = () => { current.flushResolver = null; clearTimeout(timer); resolve(); };
          current.worklet.port.postMessage({ type: 'flush', requestId: 1 });
        });
        current.acceptFrames = false; closeGraph(current);
        if (!still(current)) return;
        await withinDeadline(queue(current, () => terminalRequest(current, 'stop', deadline)), deadline);
      } catch (error) { fail(current, now() >= deadline ? problem('finalize_unconfirmed') : error); }
      return current.done;
    }
    function cancel() {
      const current = take;
      if (!current) return;
      terminalDeadline(current);
      if (!composing && textarea.value !== model.text) { journal.reset(textarea.value); endForContext(current, 'draft_changed'); return; }
      model.userEdit(textarea.value);
      const patches = model.cancelTake();
      operation++; retire(current); take = null;
      for (const patch of patches) patchText(patch, false);
      model.reset(textarea.value); setState('idle', { reason: 'cancel', announcement: 'Dictation cancelled' });
      journal.barrier();
      if (patches.some(Boolean)) changed();
      rememberCleanup(current);
    }
    function beforeSend() {
      feedbackVersion++;
      const visible = textarea.value;
      journal.barrier();
      if (take) abandon(take, 'idle', '', { reason: 'send', announcement: 'Dictation stopped' });
      else { model.reset(visible); render(); }
      return visible;
    }
    function applyHistory(direction) {
      if (destroyed || composing || textarea.readOnly || textarea.disabled || !journal.enabled) return false;
      feedbackVersion++;
      if (journal.text !== textarea.value) {
        journal.reset(textarea.value);
        if (take) endForContext(take, 'draft_changed'); else model.reset(textarea.value);
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
    function onComposerEscape(event) {
      if (!event || destroyed || !take || state === 'finishing' || take.stopRequested || event.key !== 'Escape' || event.defaultPrevented || composing || event.isComposing || event.keyCode === 229
          || (options.canCancelEscape && !options.canCancelEscape())) return false;
      event.preventDefault();
      if (event.stopImmediatePropagation) event.stopImmediatePropagation();
      else if (event.stopPropagation) event.stopPropagation();
      cancel(); return true;
    }
    function onHistoryKey(event) {
      if (destroyed || composing || event.isComposing) return;
      if (textarea.readOnly || textarea.disabled || event.altKey
          || !(event.metaKey || event.ctrlKey) || !journal.enabled) return;
      const letter = String(event.key || '').toLowerCase();
      const key = /^[a-z]$/.test(letter) ? letter : event.code === 'KeyZ' ? 'z' : event.code === 'KeyY' ? 'y' : '';
      const undoKey = key === 'z', redoKey = key === 'y';
      if (!undoKey && !redoKey) return;
      event.preventDefault(); if (event.stopPropagation) event.stopPropagation();
      applyHistory(redoKey || event.shiftKey ? 'redo' : 'undo');
    }
    function onEdit(event = {}) {
      if (applying) return;
      feedbackVersion++;
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
    function contextChanged(reason = 'recipient') {
      feedbackVersion++;
      if (take) endForContext(take, reason);
      model.reset(textarea.value); journal.reset(textarea.value); pendingInput = null; render();
    }
    function refresh() { if (take) still(take); render(); }
    function destroy() {
      if (destroyed) return;
      if (take) endForContext(take, 'destroyed');
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
    const escapeBoundary = options.host || options.mic && options.mic.parentElement || textarea;
    bind(escapeBoundary, 'keydown', onComposerEscape, true);
    bind(textarea, 'beforeinput', onBeforeInput);
    bind(textarea, 'keydown', onHistoryKey);
    // Reconcile the native edit before older bubble listeners autosize/save and
    // synchronously refresh this controller's draft-ownership fence.
    bind(textarea, 'input', onEdit, true);
    bind(textarea, 'compositionstart', () => {
      feedbackVersion++;
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
    function motionChanged() { if (take && take.analyser && state === 'listening') beginMeter(take); }
    if (motionQuery && motionQuery.addEventListener) bind(motionQuery, 'change', motionChanged);
    bind(document, 'visibilitychange', () => { if (document.hidden && take) endForContext(take, 'hidden'); });
    bind(scope, 'pagehide', () => {
      if (take) endForContext(take, 'hidden');
    });
    render();
    return { start, stop, cancel, beforeSend, contextChanged, refresh, destroy,
      escape: onComposerEscape,
      undo: () => applyHistory('undo'), redo: () => applyHistory('redo'),
      get active() { return !!take; }, get state() { return state; },
      get ranges() { return model.provisionalRanges(); }, get composing() { return composing; } };
  }
  scope.MotetDictation = { create };
  if (typeof module !== 'undefined' && module.exports) module.exports = { create };
})(typeof globalThis !== 'undefined' ? globalThis : this);
