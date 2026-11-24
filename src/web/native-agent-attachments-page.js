/* Authenticated agent attachment composer and completion-only Send binding. */
(function (root) {
'use strict';

const ids = /^[a-f0-9]{32}$/;
const opaque = /^[A-Za-z0-9_.:-]{1,160}$/;
const supported = ['application/pdf', 'image/png', 'image/jpeg'];
const phase = ['creating', 'reserving', 'uploading', 'editing', 'removing', 'discarding', 'recovering', 'resuming'];
const unresolved = ['reserved', 'receiving', 'failed'];
const local = ['reading', 'reserving'];
const closed = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) &&
  Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const integer = value => Number.isSafeInteger(value) && value >= 0;
const time = value => Number.isFinite(value) && value >= 0 && value <= 8640000000000;
const str = (value, max) => typeof value === 'string' && value.length <= max;
const flag = value => typeof value === 'boolean';
const id = value => str(value, 32) && ids.test(value);
const handle = value => str(value, 160) && opaque.test(value);
const array = (value, limit, check) => Array.isArray(value) && value.length <= limit && value.every(check);
const list = (value, limit, check) => array(value, limit, check) && new Set(value.map(row => row.id)).size === value.length;
const error = value => value === null || closed(value, ['code', 'message']) && handle(value.code) && str(value.message, 512);
const fields = ['id', 'revision', 'writerGeneration', 'createdAt', 'updatedAt', 'readOnly'];
const fileFields = ['id', 'name', 'mediaType', 'bytes', 'state', 'error', 'detailsHandle'];
const common = row => id(row.id) && integer(row.revision) && integer(row.writerGeneration) &&
  row.writerGeneration > 0 && time(row.createdAt) && time(row.updatedAt) && flag(row.readOnly);
const file = row => handle(row.id) && str(row.name, 510) && row.name.length > 0 && !/[\\/\p{C}]/u.test(row.name) &&
  supported.includes(row.mediaType) && integer(row.bytes) && error(row.error) && handle(row.detailsHandle);
const member = row => closed(row, [...fileFields, 'preparationKind', 'previewAvailable', 'expiresAt']) &&
  file(row) && id(row.id) && [...unresolved, 'ready'].includes(row.state) &&
  row.preparationKind === 'original_relay' && row.previewAvailable === false && row.expiresAt === null;

const copyStates = ['not_saved', 'saving', 'available', 'unavailable', 'removed', 'unknown'];

function completionRequest(r, fixture) {
  return closed(r, ['id','draftId','revision','createdAt','text','preparation','files','delivery','accepted',
    'execution','reply','reason','actions']) && id(r.id) && id(r.draftId) && integer(r.revision) &&
    time(r.createdAt) && str(r.text, 4000) && closed(r.actions, ['check','finish','cleanupSavedCopies']) &&
    handle(r.actions.check) && r.actions.finish === null && r.actions.cleanupSavedCopies === null &&
    (fixture ? ['not_started','pending','unknown','queued','not_submitted'] :
      ['not_started','pending','unknown','completed']).includes(r.delivery) && flag(r.accepted) &&
    (fixture ? (!r.accepted || ['queued','unknown'].includes(r.delivery)) &&
      (r.delivery !== 'queued' || r.accepted) && ['unobserved','started','ended'].includes(r.execution) &&
      (r.accepted || r.execution === 'unobserved') :
      r.accepted === (r.delivery === 'completed') && r.execution === (r.accepted ? 'ended' : 'unobserved')) &&
    ['intent','preparing_delivery','materialized','cleanup','cleaned'].includes(r.preparation) &&
    (r.delivery === 'not_started') === (r.preparation === 'intent') &&
    [null,'destination_unavailable','source_changed','copy_failed','dispatch_unconfirmed','proven_not_submitted',
      'earlier_delivery_unresolved'].includes(r.reason) &&
    closed(r.reply, ['state','text']) && ['unknown','withheld','available'].includes(r.reply.state) &&
    (r.reply.state === 'available' ? r.accepted && r.execution === 'ended' && str(r.reply.text, 32000) && !!r.reply.text : r.reply.text === null) &&
    array(r.files, 5, f => closed(f, ['fileId','name','mediaType','bytes','savedCopyState']) && id(f.fileId) &&
      str(f.name, 510) && !!f.name && !/[\\/\p{C}]/u.test(f.name) && supported.includes(f.mediaType) &&
      integer(f.bytes) && copyStates.includes(f.savedCopyState)) && r.files.length > 0 &&
    (r.reason !== 'earlier_delivery_unresolved' || !fixture && r.delivery === 'not_started' && !r.accepted &&
      r.execution === 'unobserved' && r.files.every(f => f.savedCopyState === 'not_saved')) &&
    new Set(r.files.map(f => f.fileId)).size === r.files.length;
}

function stageOneSnapshot(value, destination, completion = false, fixture = false) {
  if (!closed(value, ['schemaVersion','fixture','revision','context','features','qualification','draftRead',
    'busy','uncertain','error','draft','unsavedEdit','savedDrafts','uploads','attempts','requests','files','detail','deletion']) ||
    value.schemaVersion !== 1 || value.fixture !== false || !integer(value.revision) ||
    !flag(value.busy) || !flag(value.uncertain) || !error(value.error) ||
    !['idle','checking','complete','incomplete'].includes(value.draftRead)) return false;
  const {features, context, qualification, files, draft, unsavedEdit, detail} = value;
  if (!closed(features, ['drafts','send','files','deletion']) || !flag(features.drafts) ||
    features.send !== completion || features.files !== false || features.deletion !== false ||
    value.deletion !== null || !list(value.requests, completion ? 1000 : 0, r => completionRequest(r, fixture))) return false;
  if (!closed(files, ['state','associationCount','unclassifiedAssociationCount','first','previous','next','items']) ||
    files.state !== 'unavailable' || !array(files.items, 0, () => false) ||
    ['associationCount','unclassifiedAssociationCount','first','previous','next'].some(key => files[key] !== null)) return false;
  if (context !== null && (!closed(context, ['kind','destinationId','scopeId','modelId','conversationId']) ||
    context.kind !== 'agent' || context.destinationId !== destination || !handle(context.scopeId) ||
    context.modelId !== null || context.conversationId !== null)) return false;
  if (features.drafts && !context || features.send && !features.drafts) return false;
  const keys = ['fileBytes','draftBytes','fileCount','storageBytes','agentTextCharacters'];
  if (!closed(qualification, ['state','reason','capabilityRevision','limits','formats']) ||
    !['checking','available','unavailable'].includes(qualification.state) ||
    !(qualification.reason === null || handle(qualification.reason)) ||
    !(qualification.capabilityRevision === null || handle(qualification.capabilityRevision)) ||
    !array(qualification.formats, 3, type => supported.includes(type)) ||
    new Set(qualification.formats).size !== qualification.formats.length ||
    !(qualification.limits === null || closed(qualification.limits, keys) && keys.every(key => integer(qualification.limits[key]))) ||
    (qualification.state === 'available' && (!qualification.limits || !qualification.capabilityRevision || !qualification.formats.length))) return false;
  if (draft !== null && (!closed(draft, [...fields,'text','files']) || !common(draft) ||
    !str(draft.text, 4000) || !list(draft.files, 100, member))) return false;
  if (unsavedEdit !== null && (!closed(unsavedEdit, ['draftId','text']) || !draft ||
    unsavedEdit.draftId !== draft.id || !str(unsavedEdit.text, 4000))) return false;
  if (!list(value.savedDrafts, 1000, row => closed(row, [...fields,'fileCount','totalBytes']) &&
    common(row) && row.readOnly && integer(row.fileCount) && integer(row.totalBytes))) return false;
  if (!list(value.uploads, 100, row => closed(row, [...fileFields,'fileId','progress']) && file(row) &&
    (row.fileId === null || id(row.fileId)) && row.progress === null && [...local, ...unresolved, 'ready'].includes(row.state))) return false;
  if (!list(value.attempts, 16, row => closed(row, ['id','draftId','requestId','phase','submitted']) &&
    handle(row.id) && (row.draftId === null || id(row.draftId)) &&
    (row.requestId === null || completion && id(row.requestId)) &&
    (phase.includes(row.phase) || completion && ['preparing','sending'].includes(row.phase)) &&
    (row.submitted === null || closed(row.submitted, ['text','files']) && str(row.submitted.text, 4000) &&
      array(row.submitted.files, 100, f => closed(f, ['fileId','name','mediaType','bytes']) &&
        (f.fileId === null || id(f.fileId)) && str(f.name, 510) && supported.includes(f.mediaType) && integer(f.bytes))))) return false;
  if (detail !== null) {
    if (!closed(detail, ['owner','selectionId','draftId','fileId','requestId','name','mediaType','bytes',
      'savedCopyAvailable','kind','previewAvailable']) || detail.kind !== 'metadata_fallback' ||
      detail.previewAvailable !== false || detail.savedCopyAvailable !== false || detail.requestId !== null) return false;
    const row = detail.owner === 'selection' ? value.uploads.find(f => f.id === detail.selectionId && f.fileId === detail.fileId) :
      detail.owner === 'draft' && detail.selectionId === null && draft?.id === detail.draftId ? draft.files.find(f => f.id === detail.fileId) : null;
    if (!row || detail.name !== row.name || detail.mediaType !== row.mediaType || detail.bytes !== row.bytes) return false;
    if (detail.owner === 'selection' && (row.fileId === null ? detail.draftId !== null : !id(detail.draftId))) return false;
  }
  return context !== null || draft === null && unsavedEdit === null && !value.savedDrafts.length &&
    !value.uploads.length && !value.attempts.length && detail === null;
}

function createNativeAgentAttachmentsPage(o) {
  const {input, send, row, compose, doc} = o;
  let binding = null, composer = null, controls = null, presentation = null;
  let picked = null, drag = null, escapeDeadline = 0, composing = false;
  const addNode = (tag, className, value) => o.element(doc, tag, className, value);
  const icon = (kind, size) => o.icon(doc, kind, size);
  const link = 'workspace-attachments-link';
  const btn = (label, className, fn, parent, accessible = label) => {
    const b = o.button(doc, className, accessible, label);
    b.onclick = () => { if (b.isConnected && current(parent)) fn(); };
    return b;
  };
  const current = b => !!b && binding === b && sameSelection(b, o.selection());
  const sameSelection = (b, selected) => !!selected && b.key === selected.key && b.target === selected.target && b.identity === selected.identity;
  const safeUUID = () => typeof o.crypto?.randomUUID === 'function' && !!o.crypto?.subtle;
  const passiveFileRefusal = s => !!s && ['sqlite_unavailable','legacy_managed_storage','attachments_windows_unavailable',
    'attachments_storage_unavailable'].includes(s.error?.code) &&
    Object.values(s.features).every(value => value === false) && !s.context && !s.draft && !s.unsavedEdit &&
    !s.uploads.length && !s.attempts.length && !s.requests.length && !s.uncertain && !s.busy;
  const stateOwns = b => {
    if (!b) return false;
    const s = b.state;
    if (!s) return true;
    if (passiveFileRefusal(s)) return false;
    if (!s.features.drafts) return s.busy || s.draftRead === 'idle' || !!s.error;
    return s.draftRead !== 'complete' || s.uncertain || !!s.attempts.length || !!s.uploads.length ||
      !!s.draft || !!s.unsavedEdit || ['addFiles','createDraft','resume','editText','removeFile','removeSelection','discardDraft','send'].includes(b.op);
  };
  const stageWritable = b => current(b) && !b.invalid && !b.problem && b.state?.features.drafts && b.state.draftRead === 'complete' &&
    !!b.state.draft && !b.state.draft.readOnly && !b.state.uncertain && !b.state.busy && !b.op;
  const canAdd = b => current(b) && safeUUID() && !b.invalid && !b.problem && !b.op && !b.editor && !b.state?.busy &&
    b.state?.draftRead === 'complete' && b.state.qualification.state === 'available' &&
    !b.state.uncertain && !b.state.draft?.readOnly;
  const canSend = b => stageWritable(b) && b.state.features.send && b.state.qualification.state === 'available' &&
    !b.state.unsavedEdit && b.editor?.automatic !== false && !composing &&
    b.state.draft.files.length > 0 && b.state.draft.files.every(f => f.state === 'ready') &&
    b.state.uploads.every(f => f.state === 'ready' && b.state.draft.files.some(member => member.id === f.fileId)) &&
    !!input.value.trim() && !input.value.includes('\0');
  function cancelEdit(b) {
    if (b.timer) o.clearTimer(b.timer);
    b.timer = 0;
    b.editor = null;
  }
  function removeDrag() {
    drag?.caption.remove();
    drag?.target.remove();
    drag = null;
  }
  function restoreOrdinary(keep) {
    if (!composer) return;
    input.readOnly = composer.readOnly;
    if (composer.maxLength < 0) input.removeAttribute('maxlength');
    else input.maxLength = composer.maxLength;
    input.value = keep ? composer.text : '';
    if (keep && Number.isInteger(composer.start) && Number.isInteger(composer.end)) {
      input.setSelectionRange?.(composer.start, composer.end, composer.direction);
    }
    composer = null;
    o.restoreSend();
  }
  function clearView() {
    removeDrag();
    controls?.plus.remove();
    controls?.picker.remove();
    presentation?.tray?.remove();
    presentation?.note?.remove();
    presentation?.notice?.remove();
    controls = presentation = picked = null;
    doc.documentElement.classList.remove('workspace-native-attachments');
    if (!o.attachmentStylesInUse()) doc.documentElement.classList.remove('workspace-attachments');
  }
  function retire() {
    const previous = binding;
    if (!previous) return;
    binding = null;
    cancelEdit(previous);
    if (previous.readTimer) o.clearTimer(previous.readTimer);
    previous.client?.dispose();
    clearView();
    for (const item of previous.messages.values()) item.el.remove();
    restoreOrdinary(currentSelection(previous));
    if (previous.messages.size) o.repaintConversation?.();
    escapeDeadline = 0;
    composing = false;
  }
  function currentSelection(b) {
    return !!o.sameRecipient?.(b);
  }
  function adopt(b, snapshot) {
    if (!current(b)) return;
    const completion = snapshot?.features?.send === true &&
      /^[cx]:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(b.target);
    if (!stageOneSnapshot(snapshot, b.target, completion, o.stage2Fixture === true) || b.scope && snapshot.context?.scopeId !== b.scope) {
      b.invalid = true;
      paint(b);
      return;
    }
    if (b.state && snapshot.revision <= b.state.revision) return;
    if (snapshot.context) b.scope = snapshot.context.scopeId;
    b.invalid = false;
    b.state = snapshot;
    if (b.editor && (snapshot.draft && snapshot.draft.id !== b.editor.id ||
      !snapshot.draft && snapshot.draftRead === 'complete' && !snapshot.busy && !snapshot.error)) cancelEdit(b);
    if (b.editor && b.op !== 'editText' && snapshot.draft &&
      (snapshot.draft.revision !== b.editor.revision || snapshot.draft.writerGeneration !== b.editor.writerGeneration)) {
      b.editor.automatic = false;
    }
    if (b.op === 'addFiles') for (const f of snapshot.uploads) {
      if (f.state === 'receiving') b.received.add(f.id);
      if (f.state === 'failed' && b.received.has(f.id)) b.stopped.add(f.id);
    }
    if (snapshot.draftRead !== 'checking' && !snapshot.busy && b.readTimer) {
      o.clearTimer(b.readTimer); b.readTimer = 0; b.readLong = false;
    }
    paint(b);
    flushEditor(b);
  }
  async function invoke(b, method, args = [], checkedRequest = null) {
    if (!current(b) || b.op || b.state?.busy) return;
    if (b.editor && !['editText', 'openFileDetails', 'closeFileDetails'].includes(method)) {
      if (method !== 'refresh' && !(method === 'resume' && b.state.draft?.readOnly && args[0] === b.editor.id)) return;
      // Reading or taking over a draft never submits the buffered words.
      if (b.timer) o.clearTimer(b.timer);
      b.timer = 0;
      b.editor.automatic = false;
    }
    if (method === 'refresh') b.readNotice = b.state ? message(b, b.state, o.recipientName())?.[0] : null;
    b.op = method;
    b.problem = null;
    if (checkedRequest) b.checkFeedback = {id: checkedRequest, text: 'Checking…'};
    if (method === 'addFiles') {
      b.received.clear();
      b.selectionCount = args[0].length;
    }
    paint(b);
    let relinquishEmptyDraft = false, checked = false;
    try {
      const result = method === 'refresh' && !b.state?.context ?
        await b.client.connect({destinationId: b.target}) : await b.client[method](...args);
      if (!current(b)) return;
      adopt(b, result);
      checked = !b.invalid && !result.error;
      const s = b.state;
      relinquishEmptyDraft = !b.invalid && s && !s.busy && !s.error && s.draftRead === 'complete' &&
        ['removeFile','removeSelection'].includes(method) && !s.uncertain && !s.attempts.length &&
        !s.uploads.length && !!s.draft && !s.draft.files.length && !s.unsavedEdit;
      if (method === 'editText' && b.editor?.text === args[0]) {
        if (s?.unsavedEdit || s?.draft?.text === args[0]) b.editor = null;
        else b.editor.automatic = false;
      }
    } catch (_) {
      if (current(b)) b.problem = 'read';
    } finally {
      if (current(b)) {
        b.op = null; b.readNotice = null;
        if (checkedRequest) b.checkFeedback = {id: checkedRequest,
          text: checked ? 'Status checked.' : 'Could not check status.'};
        if (relinquishEmptyDraft) {
          // The file removal is confirmed. Release the private writer and read
          // the retained draft as a saved draft before ordinary Send can return.
          retire();
          sync();
        } else {
          paint(b);
          flushEditor(b);
        }
      }
    }
  }
  function sync() {
    const selected = o.selection();
    if (!selected) { retire(); return; }
    if (current(binding)) { paint(binding); return; }
    retire();
    const b = {target: selected.target, identity: selected.identity, key: selected.key, state: null, scope: null,
      invalid: false, op: null, timer: 0, editor: null, readTimer: 0,
      readLong: false, received: new Set(), stopped: new Set(), problem: null, messages: new Map()};
    binding = b;
    b.client = o.createClient({auth: () => current(b) ? b.key : '', changed: value => adopt(b, value)});
    paint(b);
    b.readTimer = o.setTimer(() => { if (current(b)) { b.readLong = true; paint(b); } }, 1000);
    void b.client.connect({destinationId: b.target}).then(value => adopt(b, value)).catch(() => {
      if (current(b)) { b.problem = 'read'; paint(b); }
    });
  }
  function rememberEditor() {
    const b = binding, s = b?.state;
    if (composing || !composer || !stageWritable(b) || !s?.draft) return;
    cancelEdit(b);
    if (input.value === s.draft.text && !s.unsavedEdit) { paint(b); return; }
    const {id, revision, writerGeneration} = s.draft;
    b.editor = {id, revision, writerGeneration, text: input.value, ready: false, automatic: true};
    b.timer = o.setTimer(() => {
      b.timer = 0;
      if (b.editor) b.editor.ready = true;
      flushEditor(b);
    }, 300);
    paint(b);
  }
  function flushEditor(b) {
    const edit = b.editor;
    if (!edit?.ready || !edit.automatic || !stageWritable(b)) return;
    if (edit.id === b.state.draft.id) void invoke(b, 'editText', [edit.text]);
  }
  function useSavedText(b) {
    const saved = b.state?.unsavedEdit;
    cancelEdit(b);
    if (saved) void invoke(b, 'discardUnsavedEdit', [saved.draftId]);
    else paint(b);
  }
  function chooseFiles(b) {
    if (!canAdd(b) || !controls) return;
    picked = {b, scope: b.scope};
    controls.picker.click();
  }

  function closeDetails(b) {
    if (b.op || b.state?.busy) return;
    b.returnDetailsFocus = b.state?.detail;
    void invoke(b, 'closeFileDetails');
  }

  function discardAction(b, draft) {
    const area = addNode('span', 'workspace-native-discard');
    const {id, revision, writerGeneration} = draft;
    const same = b.confirmDiscard?.id === id && b.confirmDiscard.revision === revision &&
      b.confirmDiscard.writerGeneration === writerGeneration;
    if (!same) {
      area.append(btn('Discard draft', link, () => {
        b.confirmDiscard = {id, revision, writerGeneration};
        paint(b);
      }, b));
      return area;
    }
    area.append(addNode('span', '', 'Discard this draft and its files?'),
      btn('Discard', link, () => {
        b.confirmDiscard = null;
        void invoke(b, 'discardDraft');
      }, b), doc.createTextNode(' · '),
      btn('Keep', link, () => { b.confirmDiscard = null; paint(b); }, b));
    return area;
  }

  function sendHold(s) {
    if (!s?.features.send) return null;
    return s.attempts.find(a => ['preparing','sending'].includes(a.phase) &&
      (!s.draft || a.draftId === s.draft.id) &&
      !s.requests.some(r => r.id === a.requestId && r.draftId === a.draftId && r.delivery !== 'not_started')) || null;
  }
  function holdCopy(s, hold) {
    if (!hold || s.draft) return null;
    return hold.submitted || s.requests.find(r => r.id === hold.requestId && r.draftId === hold.draftId && r.delivery === 'not_started') || null;
  }
  function requestStatus(r, label) {
    if (r.delivery === 'not_submitted') {
      const removed = r.files.filter(f => f.savedCopyState === 'removed');
      return r.preparation === 'cleaned' && removed.length ?
        'Not sent · the copied ' + (removed.length === 1 ? 'file was' : 'files were') + ' removed' : 'Not sent';
    }
    if (r.delivery === 'unknown') return 'Delivery unknown · check status; do not resend';
    if (r.execution === 'ended') return {available:'Answered', unknown:'Finished · no reply was returned to Motet',
      withheld:'Finished · the reply was withheld'}[r.reply.state];
    if (r.execution === 'started') return 'Awaiting reply';
    if (r.delivery === 'queued') return 'Queued · waiting for a reply';
    if (r.preparation === 'preparing_delivery' && r.reason !== 'copy_failed') return 'Preparing delivery · saving ' + r.files.length +
      (r.files.length === 1 ? ' file in ' : ' files in ') + label + '’s project folder';
    return 'Sending · delivery not yet confirmed';
  }
  function partialNotice(r, label) {
    if (r.delivery !== 'not_submitted') return null;
    const copied = r.files.filter(f => f.savedCopyState === 'available');
    const missing = r.files.filter(f => ['not_saved','removed'].includes(f.savedCopyState));
    const format = (files, type = 'conjunction') => new Intl.ListFormat('en-GB', {type}).format(files.map(f => f.name));
    if (copied.length) return format(copied) + (copied.length === 1 ? ' was' : ' were') + ' already copied into ' +
      label + '’s project folder before sending stopped. ' + label + ' has not received your message' +
      (missing.length ? ' or ' + format(missing, 'disjunction') : '') +
      '. Motet cannot finish sending or remove the copied ' + (copied.length === 1 ? 'file' : 'files') + ' from here yet.';
    if (r.preparation === 'cleaned') return null;
    if (r.reason === 'destination_unavailable') return label + ' could not be reached, so nothing was sent.';
    if (r.reason === 'source_changed') return 'A file changed after you attached it, so nothing was sent.';
    return 'Sending stopped before anything reached ' + label + '.';
  }
  function requestRow(b, r, label, disabled) {
    const el = addNode('div', 'workspace-attachments-messages');
    const message = addNode('div', 'workspace-attachments-message');
    message.append(addNode('span', 'workspace-attachments-message-bubble', r.text));
    const cards = addNode('div', 'workspace-attachments-sent-cards');
    for (const f of r.files) {
      const status = {saving:'Saving for ' + label + '…',
        available:(r.delivery === 'not_submitted' ? 'Saved for ' : 'Available to ') + label,
        not_saved:'Not saved', removed:'Not saved', unavailable:'No longer available', unknown:'Copy not confirmed'}[f.savedCopyState];
      const card = addNode('div', 'workspace-attachments-sent-card' +
        (r.delivery === 'not_submitted' && f.savedCopyState === 'not_saved' ? ' workspace-attachments-card-not-saved' :
          ['saving','available'].includes(f.savedCopyState) ? '' : ' workspace-attachments-card-unavailable'));
      card.setAttribute('role', 'group');
      card.setAttribute('aria-label', f.name + ', ' + status);
      const mark = addNode('span', 'workspace-attachments-sent-icon');
      mark.append(icon(f.mediaType.startsWith('image/') ? 'image' : 'file', 20));
      card.append(mark, o.fileName(doc, f.name), addNode('span', 'workspace-attachments-availability', status));
      cards.append(card);
    }
    message.append(cards, addNode('small', 'workspace-attachments-message-meta',
      'Message to ' + label + ' · ' + (r.createdAt ? (o.messageTime || o.formatDate)(r.createdAt) : 'time not recorded')));
    const status = addNode('small', 'workspace-attachments-message-status' +
      (['unknown','not_submitted'].includes(r.delivery) ? ' workspace-attachments-lifecycle-warning' : ''), requestStatus(r, label));
    status.setAttribute('role', 'status');
    if (r.delivery === 'unknown' || r.execution === 'ended' && r.reply.state !== 'available' ||
      r.delivery === 'pending' || r.delivery === 'queued' || r.execution === 'started') {
      const check = btn('Check status', 'workspace-attachments-check-status', () => {
        if (check.getAttribute('aria-disabled') !== 'true') void invoke(b, 'checkRequest', [r.actions.check], r.id);
      }, b);
      check.setAttribute('aria-disabled', String(disabled));
      status.append(check);
    }
    const feedback = addNode('span', '');
    feedback.hidden = true;
    status.append(feedback);
    message.append(status);
    const notice = partialNotice(r, label);
    if (notice) {
      const box = addNode('div', 'workspace-attachments-send-failure');
      box.setAttribute('role', 'status');
      box.append(addNode('b', '', 'This message was not delivered'), addNode('small', '', notice));
      message.append(box);
    }
    el.append(message);
    if (r.execution === 'ended' && r.reply.state === 'available') {
      const reply = addNode('div', 'workspace-attachments-agent-reply');
      reply.append(addNode('small', '', 'Reply from ' + label));
      for (const paragraph of r.reply.text.split(/\n\s*\n/)) reply.append(addNode('p', '', paragraph));
      el.append(reply);
    }
    return {el, feedback, kind:'message', context:{scope:'station', agent:b.target, name:label}, ts:r.createdAt};
  }
  function paintRequests(b, s, label, scrollSnapshot) {
    const disabled = !!b.op || !!s?.busy || !!b.editor;
    const previous = b.requestView;
    if (previous && previous.snapshot === s && previous.label === label && previous.disabled === disabled &&
      previous.invalid === b.invalid && previous.feedback === b.checkFeedback) return;
    b.requestView = {snapshot:s, label, disabled, invalid:b.invalid, feedback:b.checkFeedback};
    const requests = !b.invalid && s?.features.send ? s.requests.filter(r => r.delivery !== 'not_started') : [];
    const ids = new Set(requests.map(r => r.id));
    let changed = false, focusedRequest = null;
    function remove(id, item) {
      if (item.el.contains(doc.activeElement)) focusedRequest = id;
      item.el.remove();
      changed = true;
    }
    for (const [id, item] of b.messages) if (!ids.has(id)) {
      remove(id, item);
      b.messages.delete(id);
    }
    for (const r of requests) {
      const signature = JSON.stringify([r, label]);
      let item = b.messages.get(r.id);
      if (item?.signature === signature) {
        item.el.querySelector('.workspace-attachments-check-status')?.setAttribute('aria-disabled', String(disabled));
      } else {
        if (item) remove(r.id, item);
        item = {...requestRow(b, r, label, disabled), signature};
        b.messages.set(r.id, item);
        changed = true;
      }
      const feedback = b.checkFeedback?.id === r.id ? b.checkFeedback.text : '';
      if (item.feedback.textContent !== feedback) item.feedback.textContent = feedback;
      item.feedback.hidden = !feedback;
    }
    if (changed) o.repaintConversation?.(scrollSnapshot);
    if (focusedRequest) {
      const check = b.messages.get(focusedRequest)?.el.querySelector('.workspace-attachments-check-status');
      (check || input).focus?.({preventScroll:true});
    }
  }

  function message(b, s, label) {
    const look = ['Look again', () => invoke(b, 'refresh')];
    if (!b.invalid && passiveFileRefusal(s)) return [s.error.message, []];
    const hold = !b.invalid && sendHold(s);
    if (hold && !s.busy && !b.op) {
      const detail = s.draft || holdCopy(s, hold) ? 'so nothing more is being changed.' : 'and the saved draft could not be read here.';
      return ['Motet did not hear back after Send, ' + detail, [look]];
    }
    if (b.invalid || b.problem === 'read') return ['Motet could not read your saved drafts for ' + label + ', so files cannot be added yet.', [look]];
    if (!s.features.drafts) return s.error ? ['Motet could not read your saved drafts for ' + label + ', so files cannot be added yet.', [look]] : null;
    if (s.qualification.state === 'unavailable') return ['Files cannot be attached for ' + label + ' right now.', []];
    if (!safeUUID()) return ['This browser cannot attach files securely, so files are off here.', []];
    if (s.draftRead === 'checking' || b.op === 'refresh') {
      return b.readLong ? ['Motet is still reading your saved drafts for ' + label + '.', []] : null;
    }
    const editing = s.attempts.some(a => a.phase === 'editing' && a.draftId === s.draft?.id);
    if (editing && b.op !== 'editText') return ['Motet did not hear whether your last change was saved, so nothing more is being changed yet.', [look]];
    if (s.draftRead === 'incomplete') return ['Motet could not read your saved drafts for ' + label + ', so files cannot be added yet.', [look]];
    if (s.uncertain && !b.op) return ['An earlier change to this draft has no known outcome yet, so nothing more is being changed.', [look]];
    if (b.editor?.automatic === false && !s.busy && !b.op) {
      const actions = [];
      if (stageWritable(b)) actions.push(['Save it', () => invoke(b, 'editText', [b.editor.text])]);
      if (!s.draft?.readOnly) actions.push(['Use the saved text', () => useSavedText(b)]);
      if (s.draft?.readOnly) return null;
      return ['Your last change to this message was not saved.', actions];
    }
    if (s.unsavedEdit && !s.draft?.readOnly && !s.busy) {
      return ['Your last change to this message was not saved.', [
        ['Save it', () => invoke(b, 'editText', [s.unsavedEdit.text])],
        ['Use the saved text', () => invoke(b, 'discardUnsavedEdit', [s.unsavedEdit.draftId])],
      ]];
    }
    if (!s.error || s.busy) return null;
    const {code} = s.error;
    if (code === 'earlier_delivery_unresolved') {
      return ['An earlier unresolved delivery to ' + label + ' blocked this send. Nothing was sent.', []];
    }
    const limits = s.qualification.limits;
    const refused = b.selectionCount === 1 ? 'That file was not added. ' : 'None of those files were added. ';
    if (limits && code === 'file_too_large') return [refused + 'The limit for one file is ' + o.bytes(limits.fileBytes) + '.', []];
    if (limits && code === 'draft_limit') return [refused + 'Attach up to ' + limits.fileCount + ' files, ' + o.bytes(limits.draftBytes) + ' together.', []];
    if (code === 'unsupported_type') {
      const names = s.qualification.formats.map(type => ({'application/pdf':'PDF','image/png':'PNG','image/jpeg':'JPEG'})[type]);
      const formats = new Intl.ListFormat('en-GB').format(names);
      return [refused + 'This agent takes ' + formats + ' files.', []];
    }
    if (['storage_capacity','disk_capacity','object_capacity'].includes(code)) {
      const reason = 'Attachment storage on this computer is full.';
      // A storage refusal can come from a later file after an earlier upload succeeded.
      return [(!s.uploads.length ? refused : '') + reason, []];
    }
    return ['Motet could not do that here just now.', [look]];
  }
  function savedNotice(b, s, label) {
    if (sendHold(s)) return null;
    const saved = s.savedDrafts.filter(d => d.id !== s.draft?.id).sort((a, c) => c.updatedAt - a.updatedAt);
    const readOnly = s.draft?.readOnly && (s.unsavedEdit || b.editor) && !s.uncertain && s.draftRead === 'complete';
    if ((!saved.length || s.draft) && !readOnly) return null;
    const n = addNode('div', 'workspace-attachments-draft-notice workspace-native-saved');
    n.setAttribute('role', 'status');
    const copy = addNode('div', 'workspace-attachments-notice-copy');
    const buttons = addNode('span', 'workspace-attachments-lifecycle-actions');
    const format = item => item.fileCount ? item.fileCount + (item.fileCount === 1 ? ' file · ' : ' files · ') + o.bytes(item.totalBytes) : 'No files';
    const add = (text, fn, {parent = buttons, accessible = text, disabled = false} = {}) => {
      const button = btn(text, link, fn, b, accessible);
      button.disabled = disabled || s.busy || !!b.op || b.invalid || !!b.problem;
      parent.append(button);
    };
    if (readOnly) {
      n.classList.add('workspace-attachments-lifecycle-warning');
      copy.append(addNode('b', '', 'This draft cannot be changed here'), addNode('small', '',
        'Nothing has been merged. The message box shows your last change here, which was not saved.'));
      if (s.savedDrafts.some(d => d.id === s.draft.id)) add('Resume draft here', () => invoke(b, 'resume', [s.draft.id]));
      add('Show the saved text', () => useSavedText(b));
    } else if (saved.length === 1) {
      copy.append(addNode('b', '', 'You have a saved draft for ' + label), addNode('small', '',
        format(saved[0]) + '. Nothing in it has been sent.' +
        (saved[0].fileCount ? ' Resuming brings its files back into the message box.' : '')));
      add('Resume draft', () => invoke(b, 'resume', [saved[0].id]));
    } else {
      copy.append(addNode('b', '', 'You have ' + saved.length + ' saved drafts for ' + label), addNode('small', '', 'Nothing in them has been sent.'));
      const list = addNode('div', 'workspace-native-saved-list');
      const lines = saved.map(item => ({item, label: o.formatDate(item.updatedAt) + ' · ' + format(item)}));
      const labelCounts = new Map();
      for (const {label} of lines) labelCounts.set(label, (labelCounts.get(label) || 0) + 1);
      for (const [index, {item, label}] of lines.entries()) {
        const line = addNode('div', 'workspace-native-saved-line');
        line.append(addNode('span', '', 'Saved ' + label));
        const duplicate = labelCounts.get(label) > 1;
        const accessible = 'Resume the draft saved ' + label + (duplicate ? ' · Draft ' + (index + 1) + ' of ' + lines.length : '');
        add('Resume', () => invoke(b, 'resume', [item.id]), {parent:line, accessible});
        list.append(line);
      }
      copy.append(list);
    }
    if (!s.draft) add('Create new draft', () => chooseFiles(b), {disabled:!canAdd(b)});
    n.append(copy, buttons);
    return n;
  }
  function trayFor(b, s, label, detailButtons) {
    const hold = sendHold(s);
    const held = holdCopy(s, hold);
    const indexed = new Set(s.uploads.map(f => f.fileId));
    const items = held ? held.files.map(f => ({...f, state:'ready'})) : [...s.uploads.map(f => ({...f, selected: true})),
      ...(s.draft?.files || []).filter(f => !indexed.has(f.id)).map(f => ({...f, selected: false}))];
    if (!items.length && !s.draft && !hold) return null;
    const tray = addNode('div', 'workspace-attachments-tray');
    const head = addNode('div', 'workspace-attachments-header');
    const limits = s.qualification.limits;
    const total = items.reduce((sum, item) => sum + item.bytes, 0);
    head.append(addNode('b', '', 'For ' + label));
    if (!hold) head.append(addNode('span', '', 'Private until you press Send'));
    if (!hold || s.draft || held) {
      head.append(addNode('span', 'workspace-attachments-total', items.length + (limits ? ' of ' + limits.fileCount : '') +
        (!limits && items.length === 1 ? ' file · ' : ' files · ') + o.bytes(total)));
    }
    tray.append(head);
    const chips = addNode('div', 'workspace-attachments-chips');
    const failed = items.filter(f => f.state === 'failed' || f.state === 'reserved' && b.op !== 'addFiles');
    const busy = items.filter(f => local.includes(f.state) || f.state === 'receiving' || f.state === 'reserved' && b.op === 'addFiles');
    for (const f of items) {
      const bad = failed.includes(f), preparing = busy.includes(f);
      const short = bad ? b.stopped.has(f.id) ? 'Upload stopped' : 'Not added' :
        preparing ? f.state === 'receiving' ? 'Uploading' : 'Preparing' : o.bytes(f.bytes);
      const chip = addNode('div', 'workspace-attachments-chip' + (bad ? ' workspace-attachments-not-ready' : '') +
        (preparing ? ' workspace-attachments-busy' : ''));
      const open = !hold ? btn('', 'workspace-attachments-open', () => {
        if (s.detail && matchDetails(s.detail, f)) closeDetails(b);
        else void invoke(b, 'openFileDetails', [f.detailsHandle]);
      }, b, 'Details for ' + f.name + ', ' + short) : null;
      const detailsOpen = open && s.detail && matchDetails(s.detail, f);
      if (open) {
        open.setAttribute('aria-expanded', String(!!detailsOpen));
        open.disabled = s.busy || !!b.op && !['openFileDetails', 'closeFileDetails'].includes(b.op);
        chip.append(open);
        detailButtons.push({file:f, button:open});
      }
      const type = addNode('span', 'workspace-attachments-type');
      type.append(icon(f.mediaType.startsWith('image/') ? 'image' : 'file', 16));
      const status = addNode('span', 'workspace-attachments-state', short);
      status.setAttribute('aria-hidden', 'true');
      chip.append(type, o.fileName(doc, f.name), status);
      const remove = () => invoke(b, f.selected ? 'removeSelection' : 'removeFile', [f.id]);
      const canRemove = !hold && !preparing && !s.busy && !b.op && !b.editor && !s.uncertain &&
        (bad && f.selected && f.fileId === null || stageWritable(b));
      if (canRemove) {
        const x = btn('', 'workspace-attachments-remove', remove, b, 'Remove ' + f.name + ' from draft');
        x.append(icon('remove', 14)); chip.append(x);
      }
      if (detailsOpen) {
        const panel = addNode('div', 'workspace-attachments-details workspace-native-details');
        panel.append(addNode('b', '', f.name), addNode('small', '', o.bytes(f.bytes)));
        if (bad) panel.append(addNode('p', '', f.name + (b.stopped.has(f.id) ? ' did not finish uploading.' : ' was not added.') + ' Remove it and attach it again.'));
        if (f.state === 'ready') panel.append(addNode('p', '', 'Ready to share as it is. Motet has not opened this file. It stays private until you press Send.'));
        const actions = addNode('div', 'workspace-attachments-detail-actions');
        if (canRemove) actions.append(btn(bad ? 'Remove' : 'Remove from draft', 'workspace-attachments-link', remove, b));
        else if (bad && !s.busy) actions.append(btn('Look again', 'workspace-attachments-link', () => invoke(b, 'refresh'), b));
        actions.append(btn('Close', link, () => closeDetails(b), b));
        panel.append(actions); chip.append(panel);
      }
      chips.append(chip);
    }
    if (items.length) tray.append(chips);
    const explanation = hold ? ' while Motet does not know whether this message was sent. Nothing is sent again.' :
      failed.length ? ' until ' + (failed.length === 1 ? 'this file is removed.' : 'these files are removed.') :
      busy.length ? ' while ' + busy.length + (busy.length === 1 ? ' file is' : ' files are') + ' still being prepared.' :
      items.length ? ' until sending files to agents is available. Text typed here stays with this draft.' :
      ' for this saved draft. No files are attached.';
    const footer = addNode('div', 'workspace-native-tray-footer');
    const line = addNode('p', 'workspace-attachments-block');
    if (hold || !s.features.send || !items.length || failed.length || busy.length || s.qualification.state !== 'available') {
      line.append(addNode('b', '', 'Send is off'), doc.createTextNode(explanation));
      footer.append(line);
    }
    const draft = s.draft;
    if (draft && stageWritable(b) && !b.editor) footer.append(discardAction(b, draft));
    tray.append(footer);
    return tray;
  }
  function matchDetails(detail, item) {
    return item.selected ? detail.owner === 'selection' && detail.selectionId === item.id :
      detail.owner === 'draft' && detail.fileId === item.id;
  }
  function paint(b) {
    if (!current(b)) return;
    const s = b.state;
    const scrollSnapshot = (s?.features.send || b.messages.size) ? o.captureConversationScroll?.() : undefined;
    const owned = stateOwns(b) || b.invalid;
    if (owned && !composer) {
      composer = {text: input.value, start: input.selectionStart, end: input.selectionEnd,
        direction: input.selectionDirection, readOnly: input.readOnly, maxLength: input.maxLength};
    }
    if (owned) {
      const held = holdCopy(s, sendHold(s));
      const value = b.invalid ? '' : (b.editor?.text ?? s?.unsavedEdit?.text ?? s?.draft?.text ?? held?.text ?? '');
      if (input.value !== value) input.value = value;
      input.readOnly = !stageWritable(b);
      input.maxLength = s?.qualification.limits?.agentTextCharacters ?? 2000;
      send.disabled = !canSend(b);
    } else restoreOrdinary(true);
    const label = o.recipientName();
    if (!o.showControls() || !s && !b.problem && !b.invalid ||
      s && !s.features.drafts && !s.error && !b.invalid) {
      clearView();
      paintRequests(b, s, label, scrollSnapshot);
      return;
    }
    doc.documentElement.classList.add('workspace-native-attachments', 'workspace-attachments');
    if (s?.features.drafts && !controls) {
      const picker = addNode('input', '');
      picker.type = 'file'; picker.multiple = true; picker.hidden = true;
      const plus = btn('', 'workspace-attachments-plus', () => chooseFiles(b), b, 'Attach files for ' + label);
      plus.append(icon('clip', 18));
      plus.setAttribute('aria-haspopup', 'dialog');
      picker.onchange = () => {
        const selection = picked;
        picked = null;
        const files = Array.from(picker.files || []);
        picker.value = '';
        if (selection?.b === b && selection.scope === b.scope && canAdd(b) && files.length) void invoke(b, 'addFiles', [files]);
      };
      picker.addEventListener('cancel',()=>{ if (picked?.b === b) picked = null; });
      const address = o.address?.();
      row.insertBefore(plus, address?.parentNode === row ? address : send);
      row.append(picker);
      controls = {plus, picker};
    }
    if (controls) {
      controls.plus.disabled = !canAdd(b);
      controls.picker.accept = s.qualification.formats.join(',');
    }
    if (!canAdd(b)) removeDrag();
    const focused = doc.activeElement, activeLabel = focused?.getAttribute?.('aria-label');
    const hadFocus = presentation && Object.values(presentation).some(node => node?.contains(focused));
    const listScroll = presentation?.notice?.querySelector('.workspace-native-saved-list')?.scrollTop || 0;
    const chipScroll = presentation?.tray?.querySelector('.workspace-attachments-chips')?.scrollLeft || 0;
    presentation?.tray?.remove(); presentation?.note?.remove(); presentation?.notice?.remove();
    const detailButtons = [];
    const tray = s?.features.drafts && !b.invalid ? trayFor(b, s, label, detailButtons) : null;
    const notice = s?.features.drafts && !b.invalid ? savedNotice(b, s, label) : null;
    if (tray) {
      compose.insertBefore(tray, input);
      const chips = tray.querySelector('.workspace-attachments-chips');
      if (chips) chips.scrollLeft = chipScroll;
    }
    if (notice) {
      row.before(notice);
      const list = notice.querySelector('.workspace-native-saved-list');
      if (list) list.scrollTop = listScroll;
    }
    const reading = b.op === 'refresh' && b.readNotice;
    const info = reading ? [b.readNotice, [['Looking…', () => {}]]] :
      s ? message(b, s, label) : ['Motet could not read your saved drafts for ' + label + ', so files cannot be added yet.',
        [['Look again', () => invoke(b, 'refresh')]]];
    let note = null;
    if (info) {
      note = addNode('div', 'workspace-attachments-recipient-note workspace-native-recipient-note');
      note.setAttribute('role', 'status');
      note.append(addNode('span', '', info[0]));
      for (const [index, [name, fn]] of info[1].entries()) {
        const action = btn(name, 'workspace-attachments-link', fn, b);
        action.disabled = !!reading;
        note.append(doc.createTextNode(index ? ' · ' : ' '), action);
      }
      row.after(note);
    }
    o.setHintHidden(!!info);
    presentation = {tray, note, notice};
    // The class and composer must size the log before positioning a new receipt.
    paintRequests(b, s, label, scrollSnapshot);
    if (b.returnDetailsFocus && !s?.detail) {
      const target = detailButtons.find(({file}) => matchDetails(b.returnDetailsFocus, file));
      b.returnDetailsFocus = null;
      (target?.button || input).focus({preventScroll:true});
    } else if (hadFocus && !focused.isConnected) {
      const alternate = activeLabel === 'Discard draft' ? 'Keep' : activeLabel === 'Keep' ? 'Discard draft' : null;
      const buttons = Object.values(presentation).flatMap(node => node ? [...node.querySelectorAll('button')] : []);
      const target = buttons.find(button => !button.disabled && button.getAttribute('aria-label') === activeLabel) ||
        buttons.find(button => alternate && !button.disabled && button.getAttribute('aria-label') === alternate);
      (target || input).focus({preventScroll:true});
    }
  }
  function dragEvent(e) {
    if (!e.dataTransfer?.types.includes('Files')) return;
    const b = binding, log = o.log;
    const inside = current(b) && o.allowDrop() && (log.contains(e.target) || compose.contains(e.target));
    if (!inside || !controls) { removeDrag(); return; }
    e.preventDefault();
    if (!canAdd(b)) { removeDrag(); return; }
    if (e.type === 'drop') {
      removeDrag();
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length) void invoke(b, 'addFiles', [files]);
      return;
    }
    if (drag) return;
    const caption = addNode('div', 'workspace-attachments-drop-caption');
    caption.append(icon('clip', 16), doc.createTextNode('Drop to attach for ' + o.recipientName()));
    caption.setAttribute('role', 'status');
    const target = addNode('div', 'workspace-native-drop-target');
    target.setAttribute('aria-hidden', 'true');
    row.before(caption);
    log.append(target);
    drag = {caption, target};
  }
  function escape(e) {
    if (e.key !== 'Escape') return false;
    if (drag || Date.now() < escapeDeadline) {
      removeDrag(); escapeDeadline = 0;
    } else if (binding?.state?.detail && current(binding)) {
      closeDetails(binding);
    } else return false;
    e.preventDefault(); e.stopImmediatePropagation();
    return true;
  }
  input.addEventListener('input', rememberEditor);
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; rememberEditor(); });
  doc.addEventListener('dragover', dragEvent);
  doc.addEventListener('drop', dragEvent);
  for (const event of ['dragend','dragleave','pointerup']) doc.addEventListener(event, e => {
    if (event === 'dragleave' && e.relatedTarget) return;
    if (drag) escapeDeadline = Date.now() + 200;
    removeDrag();
  });
  return {
    sync, retire, escape,
    retireIfChanged() { if (binding && !current(binding)) retire(); },
    ownsInput: () => !!composer && current(binding),
    conversationRows: () => current(binding) && !binding.invalid ? [...binding.messages.values()].sort((a, b) => a.ts - b.ts) : [],
    blocksSend() {
      sync();
      const b = binding;
      if (!current(b) || !(stateOwns(b) || b.invalid || picked?.b === b)) return false;
      // The existing composer calls this only from its explicit Send action.
      if (canSend(b) && picked?.b !== b) {
        const words = input.value;
        cancelEdit(b);
        void invoke(b, 'send', [words]);
      }
      return true;
    },
    foreground: () => current(binding) && !!binding.state?.detail,
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {createNativeAgentAttachmentsPage, stageOneSnapshot};
} else {
  root.createNativeAgentAttachmentsPage = createNativeAgentAttachmentsPage;
}
})(globalThis);
