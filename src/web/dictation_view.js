/* Local dictation controls; the original textarea remains the editing surface. */
(function (scope) {
  'use strict';
  function attach(options) {
    const {textarea, row, compose, host, send} = options, doc = textarea.ownerDocument;
    host.classList.add('dictation-mounted');
    const make = (tag, name) => { const node = doc.createElement(tag); node.className = name; return node; };
    function icon(path, square = false) {
      const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
      const shape = doc.createElementNS(svg.namespaceURI, square ? 'rect' : 'path');
      // The 24-unit viewBox is displayed at 20px: 12 units make a 10px square.
      if (square) { for (const [key, value] of Object.entries({x:6,y:6,width:12,height:12,rx:2.4})) shape.setAttribute(key,value); }
      else shape.setAttribute('d', path);
      svg.appendChild(shape); return svg;
    }
    const microphone = 'M12 3.5a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0v-5a3 3 0 0 0-3-3ZM6 11.5a6 6 0 0 0 12 0M12 17.5v3';
    const mic = make('button','dictation-tool dictation-mic'); mic.type = 'button';
    const micIcon = icon(microphone), stopIcon = icon('',true), blockedIcon = icon('M4.5 4.5l15 15');
    blockedIcon.classList.add('dictation-mic-blocked'); mic.append(micIcon,stopIcon,blockedIcon);
    const cancel = make('button','dictation-tool dictation-cancel'); cancel.type = 'button';
    cancel.setAttribute('aria-label','Cancel dictation'); cancel.title = 'Cancel dictation';
    cancel.appendChild(icon('M7 7l10 10M17 7 7 17'));
    const wave = make('span','dictation-wave'); wave.setAttribute('role','img'); wave.setAttribute('aria-label','Listening');
    const statusRow = make('div','dictation-feedback'), status = make('span','dictation-status');
    const announcer = make('span','dictation-announcer');
    announcer.setAttribute('role','status'); announcer.setAttribute('aria-live','polite'); announcer.setAttribute('aria-atomic','true');
    host.appendChild(announcer);
    const settings = make('button','dictation-settings'); settings.type = 'button';
    settings.setAttribute('aria-label','Open dictation settings'); settings.title = 'Open dictation settings';
    settings.appendChild(icon('M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7ZM9.8 3.5h4.4l.5 2 1.6.9 2-.6 2.2 3.8-1.5 1.4v1.9l1.5 1.4-2.2 3.8-2-.6-1.6.9-.5 2H9.8l-.5-2-1.6-.9-2 .6-2.2-3.8L5 12.9V11L3.5 9.6l2.2-3.8 2 .6 1.6-.9.5-2Z'));
    if (options.onSettings) settings.addEventListener('click', options.onSettings);
    statusRow.append(status,settings); row.insertBefore(cancel,send); row.insertBefore(wave,send); row.insertBefore(mic,send);
    row.after(statusRow);
    const mirror = make('div','dictation-mirror'), ink = make('div','dictation-mirror-ink');
    mirror.setAttribute('aria-hidden','true'); mirror.appendChild(ink); compose.appendChild(mirror);
    let detail = {}, state = 'idle', active = false, composing = false, destroyed = false, announcementKey = '';
    const originalColour = textarea.style.color;
    function clearMirror() { mirror.hidden = true; textarea.style.color = originalColour; }
    function paintMirror() {
      if (destroyed) return;
      const ranges = (detail.ranges || []).filter(range => Number.isInteger(range.start) && Number.isInteger(range.end)
        && range.start >= 0 && range.end > range.start && range.end <= textarea.value.length && range.complete !== true);
      if (state !== 'listening' || !ranges.length || composing || textarea.selectionStart !== textarea.selectionEnd
          || (scope.matchMedia && scope.matchMedia('(forced-colors: active)').matches)) { clearMirror(); return; }
      const computed = scope.getComputedStyle(textarea);
      for (const name of ['fontFamily','fontSize','fontWeight','fontStyle','fontVariantLigatures','lineHeight','letterSpacing',
                          'wordSpacing','textIndent','textTransform','textAlign','tabSize','wordBreak','overflowWrap']) ink.style[name] = computed[name];
      ink.style.whiteSpace = textarea.wrap === 'off' ? 'pre' : 'pre-wrap';
      for (const name of ['paddingTop','paddingRight','paddingBottom','paddingLeft']) ink.style[name] = computed[name];
      mirror.style.left = (textarea.offsetLeft + textarea.clientLeft) + 'px';
      mirror.style.top = (textarea.offsetTop + textarea.clientTop) + 'px';
      mirror.style.width = textarea.clientWidth + 'px'; mirror.style.height = textarea.clientHeight + 'px';
      ink.style.width = textarea.clientWidth + 'px';
      ink.style.transform = 'translate(' + (-textarea.scrollLeft) + 'px,' + (-textarea.scrollTop) + 'px)';
      ink.replaceChildren();
      const text = textarea.value; let at = 0;
      for (const range of ranges.sort((a,b)=>a.start-b.start)) {
        if (range.start < at) continue;
        ink.appendChild(doc.createTextNode(text.slice(at,range.start)));
        const tail = make('span','dictation-provisional'); tail.textContent = text.slice(range.start,range.end); ink.appendChild(tail); at = range.end;
      }
      ink.appendChild(doc.createTextNode(text.slice(at) + (text.endsWith('\n') ? '\u200b' : '')));
      mirror.hidden = false; textarea.style.color = 'transparent';
    }
    function render(next, value = {}) {
      state = next; detail = value; active = ['preparing','listening'].includes(state);
      host.classList.toggle('dictation-active',active); host.classList.toggle('dictation-listening',state === 'listening');
      const blocked = value.code === 'microphone_blocked' || value.code === 'permission_denied';
      host.classList.toggle('dictation-blocked',blocked);
      cancel.hidden = wave.hidden = !active;
      micIcon.toggleAttribute('hidden',active); stopIcon.toggleAttribute('hidden',!active); blockedIcon.toggleAttribute('hidden',!blocked);
      mic.setAttribute('aria-label',active ? 'Stop dictation' : 'Dictate a message'); mic.title = active ? 'Stop dictation' : 'Dictate a message';
      mic.disabled = state === 'finishing' || !!(options.disabled && options.disabled());
      settings.hidden = !['not_installed','not_running','not_configured','dependency_missing','model_missing_or_invalid'].includes(value.code);
      const notice = typeof value.notice === 'string' ? value.notice : '';
      const message = ['preparing','listening'].includes(state) && notice ? notice : typeof value.message === 'string' ? value.message : '';
      statusRow.hidden = !message; status.textContent = message;
      let announcement = typeof value.announcement === 'string' ? value.announcement : '';
      if (state === 'listening') announcement = 'Listening' + (notice ? '. ' + notice : '');
      else if (!announcement) announcement = message;
      const key = state + '|' + announcement;
      if (announcement && key !== announcementKey) { announcer.textContent = announcement; announcementKey = key; }
      paintMirror();
    }
    const select = () => paintMirror(), startComposition = () => { composing = true; clearMirror(); },
      endComposition = () => { composing = false; paintMirror(); };
    textarea.addEventListener('scroll',select); textarea.addEventListener('select',select); textarea.addEventListener('input',select);
    textarea.addEventListener('compositionstart',startComposition); textarea.addEventListener('compositionend',endComposition);
    doc.addEventListener('selectionchange',select); scope.addEventListener('resize',select);
    const observer = typeof scope.ResizeObserver === 'function' ? new scope.ResizeObserver(select) : null;
    if (observer) observer.observe(textarea);
    render('idle');
    return {mic,cancel,send,wave,status,settings,announcer,render,refresh:paintMirror,
      destroy() { destroyed = true; clearMirror(); observer?.disconnect();
        textarea.removeEventListener('scroll',select); textarea.removeEventListener('select',select); textarea.removeEventListener('input',select);
        textarea.removeEventListener('compositionstart',startComposition); textarea.removeEventListener('compositionend',endComposition);
        doc.removeEventListener('selectionchange',select); scope.removeEventListener('resize',select);
        host.classList.remove('dictation-mounted','dictation-active','dictation-listening','dictation-blocked');
        for (const node of [mic,cancel,wave,statusRow,mirror,announcer]) node.remove(); }
    };
  }
  scope.MotetDictationView = Object.freeze({attach});
})(typeof window !== 'undefined' ? window : globalThis);
