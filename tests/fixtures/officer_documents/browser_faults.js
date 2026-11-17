/* Fixed opt-in offline fixture instrumentation. Never a production adapter. */
(function (root) {
  'use strict';
  const MODES = ['none', 'confirm-before-send-once', 'question-intent-after-response-once',
    'upload-pause-once', 'upload-network-loss-once'];
  const UPLOAD_PAUSE_MS = 8000;

  function install(target, mode) {
    if (!MODES.includes(mode)) throw new TypeError('Unknown offline fixture fault');
    if (mode === 'none') return;
    const origin = new URL(target.location.origin);
    if (origin.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(origin.hostname) ||
        typeof target.fetch !== 'function') throw new TypeError('A loopback fixture is required');
    const original = target.fetch.bind(target), pauses = new Set();
    let consumed = false, closing = false;
    const patterns = {
      'confirm-before-send-once': ['POST', /^\/api\/attachments\/deletions\/[a-f0-9]{32}\/confirm$/],
      'question-intent-after-response-once': ['POST', /^\/api\/attachments\/conversations\/[a-f0-9]{32}\/question-intent$/],
      'upload-pause-once': ['PUT', /^\/api\/attachments\/drafts\/[a-f0-9]{32}\/uploads\/[a-f0-9]{32}$/],
      'upload-network-loss-once': ['PUT', /^\/api\/attachments\/drafts\/[a-f0-9]{32}\/uploads\/[a-f0-9]{32}$/]
    };
    function matches(input, init) {
      try {
        const supplied = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
        const url = new URL(supplied, origin), method = String(init?.method || input?.method || 'GET').toUpperCase();
        return url.origin === origin.origin && !url.search && !url.hash &&
          method === patterns[mode][0] && patterns[mode][1].test(url.pathname);
      } catch (_) { return false; }
    }
    function pause(signal) {
      return new Promise((resolve, reject) => {
        let timer;
        const aborted = () => finish(new DOMException('Offline fixture request aborted', 'AbortError'));
        const finish = error => {
          target.clearTimeout(timer);signal?.removeEventListener('abort', aborted);pauses.delete(aborted);
          if (error) reject(error); else resolve();
        };
        if (signal?.aborted) { aborted();return; }
        pauses.add(aborted);signal?.addEventListener('abort', aborted, {once:true});
        timer = target.setTimeout(() => finish(), UPLOAD_PAUSE_MS);
      });
    }
    target.addEventListener?.('pagehide', () => { closing = true;for (const abort of [...pauses]) abort(); });
    target.fetch = async function (input, init) {
      if (consumed || !matches(input, init)) return original(input, init);
      consumed = true;
      if (mode === 'confirm-before-send-once' || mode === 'upload-network-loss-once') {
        throw new TypeError('Fictional offline request acknowledgement lost');
      }
      if (mode === 'upload-pause-once') {
        const signal = init?.signal || input?.signal;
        await pause(signal);
        if (closing || signal?.aborted) throw new DOMException('Offline fixture request aborted', 'AbortError');
        return original(input, init);
      }
      const response = await original(input, init);
      if (response.ok) {
        await response.clone().arrayBuffer();
        throw new TypeError('Fictional offline response acknowledgement lost');
      }
      return response;
    };
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {install, MODES, UPLOAD_PAUSE_MS};
  } else {
    install(root, "__OFFICER_BROWSER_FAULT__");
  }
})(globalThis);
