/* Native attachment drafts, original uploads, and exact readback. */
(function () {
'use strict';

const PREFIX = '/api/attachments';
const MIME = ['application/pdf', 'image/png', 'image/jpeg'];
const MAX_REPLY = 262144;
const LIMITS = ['fileBytes', 'draftBytes', 'fileCount', 'storageBytes', 'agentTextCharacters'];
const FEATURES = {drafts: false, send: false, files: false, deletion: false};
const INACTIVE_FILES = {state: 'unavailable', associationCount: null, unclassifiedAssociationCount: null,
  first: null, previous: null, next: null, items: []};
const PHASE = {draft_create: 'creating', upload_reserve: 'reserving', upload_put: 'uploading',
  draft_edit: 'editing', draft_remove: 'removing', draft_discard: 'discarding', send_prepare: 'preparing', send_execute: 'sending',
  deletion_prepare: 'preparing_deletion', deletion_confirm: 'confirming_deletion', deletion_execute: 'executing_deletion'};
const EARLIER_DELIVERY_UNRESOLVED = 'earlier_delivery_unresolved';
const COPY_STATES = ['not_saved', 'saving', 'available', 'unavailable', 'removed', 'unknown'];
// The same list, in order, as REASONS in src/attachments_native_deletion.py ('copy_in_use': another program has the
// workspace copy open on Windows, so it is left and retried).
const DELETION_REASONS = [null, 'source_in_use', 'identity_changed', 'removal_unconfirmed', 'workspace_unavailable', 'workspace_unverified', 'copy_in_use'];
const DELETION_PIECES = ['watch_original', 'workspace'];
const REQUEST_KEYS = ['id', 'draftId', 'revision', 'createdAt', 'text', 'preparation', 'files', 'delivery',
  'accepted', 'execution', 'reply', 'reason'];
const FILE_STATE = {uploading: 'reserved', receiving: 'receiving', ready: 'ready', failed: 'failed'};
const CLEANUP = ['cleanup_pending', 'cleanup_failed', 'deleted'];
const FILE_ERRORS = new Set(['upload_interrupted', 'upload_failed', 'upload_timeout', 'invalid_upload', 'invalid_request',
  'upload_size', 'upload_digest', 'source_changed', 'source_unavailable', 'draft_conflict', 'type_signature_mismatch',
  'file_too_large', 'preparation_unavailable', 'storage_unavailable', 'clock_uncertain', 'identity_changed']);
const PRE_EFFECT = {
  draft_create: {destination_unavailable: 503, capability_changed: 409, draft_capacity: 429, history_capacity: 429},
  upload_reserve: {destination_unavailable: 503, capability_changed: 409, preparation_unavailable: 503, unsupported_type: 415,
    file_too_large: 413, draft_limit: 413, object_capacity: 429, upload_capacity: 429, storage_capacity: 429,
    disk_capacity: 429, history_capacity: 429},
  draft_edit: {text_limit: 413},
};
const MESSAGES = {
  sqlite_unavailable: 'Sending files to agents is unavailable because this Python uses an older database library. Install a newer Python and use it to restart Motet.',
  legacy_managed_storage: "Sending files to agents is unavailable because Motet's Claude chats were first set up with an older Python. Ordinary messages still work.",
  // Why the attachment store is not running; worded as in ATTACHMENTS_OFF_WHY (src/server.py).
  attachments_windows_unavailable: 'Sending files is not available on Windows yet. Ordinary messages still work.',
  attachments_storage_unavailable: 'Sending files is unavailable because Motet could not open its private file storage. Restart Motet to try again. Ordinary messages still work.',
  // Windows: why one agent chat cannot be sent files here; worded as in src/attachments_windows_git.py and ATTACHMENTS_OFF_WHY.
  attachments_git_missing: 'Sending files to Claude chats needs Git for Windows installed for all users (in Program Files). Ordinary messages still work.',
  attachments_git_per_user: 'Sending files to Claude chats needs Git for Windows installed for all users (in Program Files). Git installed only for your account can be changed by any program you run, so Motet does not use it. Ordinary messages still work.',
  attachments_git_unsafe: 'Sending files is unavailable because the Git in Program Files can be changed by programs you run. Reinstall Git for Windows for all users. Ordinary messages still work.',
  attachments_codex_mac_only: 'Sending files to Codex chats needs the Codex desktop app on a Mac.',
  invalid_response: 'The attachment response could not be verified.', invalid_request: 'Choose an exact attachment item.',
  unavailable: 'This attachment operation is unavailable.', unauthorized: 'Owner access changed.',
  forbidden: 'The attachment could not be authorized.', destination_unavailable: 'Attachments are unavailable for this agent.',
  capability_changed: 'The attachment qualification changed. Refresh the exact destination.',
  [EARLIER_DELIVERY_UNRESOLVED]: 'An earlier unresolved delivery blocked this send. Nothing was sent.',
  journal_unavailable: 'Attachment recovery storage could not be verified.', journal_conflict: 'An attachment operation is unresolved.',
  draft_conflict: 'This draft changed in another window.', incomplete: 'The exact draft state could not be confirmed.',
  network_error: 'The attachment operation could not be confirmed.', draft_capacity: 'Discard a saved draft before creating another.',
  history_capacity: 'Attachment history has reached its limit.', storage_capacity: 'Attachment storage has no room for this file.',
  disk_capacity: 'This computer has insufficient free storage.', draft_limit: 'This draft exceeds its attachment limits.',
  file_too_large: 'This file exceeds the attachment size limit.', unsupported_type: 'This attachment type is unavailable.',
  file_unavailable: 'This file could not be read.', upload_interrupted: 'The upload was interrupted.',
  upload_timeout: 'The upload timed out.', type_signature_mismatch: 'The file does not match its selected type.',
  upload_digest: 'The saved upload did not match the selected file.', text_limit: 'The attachment message exceeds its text limit.',
  requests_incomplete: 'The full attachment request history could not be read.',
  files_incomplete: 'The exact retained Files page could not be read.',
  deletion_incomplete: 'The exact attachment deletion state could not be confirmed.',
};

function fault(code, status = null) { return Object.assign(new Error(code), {code, status}); }
function notice(code) {
  const known = Object.hasOwn(MESSAGES, code) || FILE_ERRORS.has(code) || Object.values(PRE_EFFECT).some(row => Object.hasOwn(row, code));
  return {code: known ? code : 'unavailable', message: Object.hasOwn(MESSAGES, code) ? MESSAGES[code] : 'The attachment operation could not be completed.'};
}
function check(value) { if (!value) throw fault('invalid_response'); }
function object(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
function closed(v, keys) { return object(v) && Reflect.ownKeys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k)); }
function re(v, pattern) { return typeof v === 'string' && pattern.test(v); }
function opaque(v) { return re(v, /^[A-Za-z0-9_.:-]{1,160}(?![\s\S])/); }
function id(v) { return re(v, /^[a-f0-9]{32}(?![\s\S])/); }
function secret(v) { return re(v, /^[a-f0-9]{64}(?![\s\S])/); }
function int(v, low = 0) { return Number.isSafeInteger(v) && v >= low; }
function time(v) { return typeof v === 'number' && Number.isFinite(v) && v >= 0; }
function text(v, max) { return typeof v === 'string' && !/[\uD800-\uDFFF]/u.test(v) && [...v].length <= max; }
function name(v) { return text(v, 255) && v.length > 0 && v === v.trim() && v !== '.' && v !== '..' && !/[\\/\p{C}]/u.test(v); }
function unique(v) { return new Set(v).size === v.length; }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function query(data) { return '?' + new URLSearchParams(data).toString(); }
function fence(draft, writer) { return {revision: draft.revision, writerGeneration: draft.writerGeneration, writerNonce: writer.nonce}; }
function cap(writer) { return {'X-Attachment-Draft-Capability': writer.capability}; }
function draftPath(draftId) { return '/drafts/' + draftId; }

function selection(value, destinationId, staged, deletionFixture) {
  const completion = !staged && value?.completionOnly === true && re(destinationId,
    /^[cx]:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(?![\s\S])/);
  check(closed(value, completion ? ['context', 'features', 'completionOnly'] : ['context', 'features']) &&
    closed(value.features, Object.keys(FEATURES)) &&
    Object.values(value.features).every(flag => typeof flag === 'boolean') &&
    (!value.features.deletion || deletionFixture && value.features.files && !value.features.send && !value.features.drafts) &&
    (staged || completion && value.features.send && !value.features.files && !value.features.deletion ||
      !value.features.send && !value.features.files) && (!value.features.send || value.features.drafts));
  if (value.context === null) check(!completion && Object.values(value.features).every(flag => flag === false));
  else {
    const c = value.context;
    check(closed(c, ['kind', 'destinationId', 'scopeId', 'modelId', 'conversationId']) && c.kind === 'agent' &&
      c.destinationId === destinationId && opaque(c.scopeId) && c.modelId === null && c.conversationId === null &&
      (value.features.drafts || staged && value.features.files));
  }
  return value;
}

function sameContext(value, context) {
  return value.destinationId === context.destinationId && value.scopeId === context.scopeId;
}

function inputRow(value) {
  return id(value.fileId) && name(value.name) && MIME.includes(value.mediaType) && int(value.bytes) &&
    value.bytes <= 10 * 1024 * 1024;
}

function wireObservation(value, context, completion) {
  check(closed(value, ['destinationId', 'scopeId', 'request', 'draftFence', 'draftAdmission']) && sameContext(value, context) &&
    closed(value.draftFence, ['revision', 'writerGeneration']) && int(value.draftFence.revision, 1) &&
    int(value.draftFence.writerGeneration, 1) && ['unadmitted', 'matched'].includes(value.draftAdmission));
  const r = value.request;
  check(closed(r, REQUEST_KEYS) && id(r.id) && id(r.draftId) && int(r.revision, 1) && time(r.createdAt) &&
    text(r.text, 2000) && r.text.trim().length && !r.text.includes('\0') &&
    ['intent', 'preparing_delivery', 'materialized', 'cleanup', 'cleaned'].includes(r.preparation) &&
    (completion ? ['not_started', 'pending', 'completed', 'unknown'] :
      ['not_started', 'pending', 'queued', 'not_submitted', 'unknown']).includes(r.delivery) && typeof r.accepted === 'boolean' &&
    ['unobserved', 'started', 'ended'].includes(r.execution) &&
    [null, 'destination_unavailable', 'source_changed', 'copy_failed', 'dispatch_unconfirmed', 'proven_not_submitted',
      EARLIER_DELIVERY_UNRESOLVED].includes(r.reason) &&
    Array.isArray(r.files) && r.files.length >= 1 && r.files.length <= 5 && r.files.every(f =>
      closed(f, ['fileId', 'name', 'mediaType', 'bytes', 'savedCopyState']) && inputRow(f) && COPY_STATES.includes(f.savedCopyState)) &&
    unique(r.files.map(f => f.fileId)) && closed(r.reply, ['state', 'text']) &&
    ['available', 'withheld', 'unknown'].includes(r.reply.state));
  const admitted = value.draftAdmission === 'matched', refused = r.reason === EARLIER_DELIVERY_UNRESOLVED;
  check(!refused || completion && !admitted && context.destinationId.startsWith('c:'));
  check(admitted ? r.revision > 1 && r.preparation !== 'intent' && r.delivery !== 'not_started' :
    r.preparation === 'intent' && r.revision === 1 && r.delivery === 'not_started' && r.files.every(f => f.savedCopyState === 'not_saved'));
  check((completion ? (r.delivery === 'completed' ? admitted && r.accepted && r.execution === 'ended' :
    !r.accepted && r.execution === 'unobserved') :
    (!r.accepted || admitted && ['queued', 'unknown'].includes(r.delivery)) && (r.delivery !== 'queued' || r.accepted)) &&
    (r.execution === 'unobserved' || r.accepted) &&
    (r.delivery === 'not_submitted' ? !r.accepted && r.reason === 'proven_not_submitted' : r.reason !== 'proven_not_submitted') &&
    (r.reply.state === 'available' ? r.accepted && r.execution === 'ended' && text(r.reply.text, 16000) &&
      r.reply.text.length > 0 && new TextEncoder().encode(r.reply.text).length <= 64000 : r.reply.text === null) &&
    (admitted || !r.accepted && r.execution === 'unobserved' && r.reply.state === 'unknown' && (r.reason === null || refused)));
  return value;
}

function sameInput(a, b) {
  return a.text === b.text && a.files.length === b.files.length && a.files.every((f, i) => {
    const g = b.files[i];
    return f.fileId === g.fileId && f.name === g.name && f.mediaType === g.mediaType && f.bytes === g.bytes;
  });
}

function wireFileRow(value) {
  check(closed(value, ['id', 'requestId', 'name', 'mediaType', 'bytes', 'cataloguedAt', 'savedCopyAvailable',
    'savedCopyState', 'retentionPinned', 'preparationKind', 'previewAvailable', 'expiresAt']) &&
    inputRow({...value, fileId: value.id}) && id(value.requestId) && time(value.cataloguedAt) &&
    typeof value.savedCopyAvailable === 'boolean' && COPY_STATES.includes(value.savedCopyState) &&
    value.savedCopyAvailable === (value.savedCopyState === 'available') && typeof value.retentionPinned === 'boolean' &&
    value.preparationKind === 'original_relay' && value.previewAvailable === false && value.expiresAt === null);
  return value;
}

function cursorValue(value) { return re(value, /^[A-Za-z0-9_-]{1,4096}(?![\s\S])/); }

function deletionRecord(value) { return value.kind.startsWith('deletion_'); }
function associationKey(fileId, requestId) { return fileId + ':' + requestId; }

function wireManagementRow(row) {
  check(closed(row, ['fileId', 'requestId', 'sourceAvailable', 'managementGeneration', 'canPrepare', 'operation']) &&
    id(row.fileId) && id(row.requestId) && typeof row.sourceAvailable === 'boolean' && typeof row.canPrepare === 'boolean' &&
    int(row.managementGeneration) && row.managementGeneration < Number.MAX_SAFE_INTEGER &&
    (row.operation === null || closed(row.operation, ['id', 'previewGeneration']) && id(row.operation.id) &&
      int(row.operation.previewGeneration, 1) && row.operation.previewGeneration === row.managementGeneration));
  return row;
}

function wireDeletion(value, context, fileId, operationId) {
  check(closed(value, ['id', 'fileId', 'destinationId', 'scopeId', 'previewGeneration', 'revision', 'state',
    'releasedBytes', 'sourceUnavailable', 'canExecute', 'canRecover', 'pieces']) && sameContext(value, context) &&
    value.fileId === fileId && id(value.id) && (operationId === undefined || operationId === value.id) &&
    int(value.previewGeneration, 1) && int(value.revision, value.previewGeneration) && int(value.releasedBytes) &&
    ['sourceUnavailable', 'canExecute', 'canRecover'].every(k => typeof value[k] === 'boolean') &&
    Array.isArray(value.pieces) && value.pieces.length === 2);
  check(value.pieces.every((piece, i) => closed(piece, ['kind', 'state', 'bytes', 'chargedBytes', 'releasedBytes', 'reason']) &&
    piece.kind === DELETION_PIECES[i] && ['pending', 'removed', 'changed', 'unknown'].includes(piece.state) &&
    int(piece.bytes, 1) && piece.bytes <= 10 * 1024 * 1024 && int(piece.chargedBytes) && int(piece.releasedBytes) &&
    piece.chargedBytes + piece.releasedBytes <= piece.bytes &&
    (piece.state === 'removed' ? piece.chargedBytes === 0 && piece.releasedBytes === piece.bytes : piece.releasedBytes === 0) &&
    DELETION_REASONS.includes(piece.reason)));
  const pieces = value.pieces.map(piece => piece.state);
  const pending = pieces.includes('pending'), removed = pieces.includes('removed'), unknown = pieces.includes('unknown');
  const state = !value.sourceUnavailable ? 'confirm' : pieces.every(s => s === 'removed') ? 'deleted' :
    pieces.includes('changed') || removed && unknown ? 'partial_failure' : unknown ? 'unknown' : 'deleting';
  check(state === value.state && value.releasedBytes === value.pieces[0].releasedBytes + value.pieces[1].releasedBytes &&
    (value.sourceUnavailable ? value.revision > value.previewGeneration :
      value.revision === value.previewGeneration && pieces.every(s => s === 'pending') && value.releasedBytes === 0) &&
    (!value.canExecute || value.sourceUnavailable && pending) && (!value.canRecover || value.sourceUnavailable && unknown));
  return value;
}

function deletionIdentity(a, b) {
  return a.id === b.id && a.fileId === b.fileId && a.previewGeneration === b.previewGeneration;
}

function checkDeletionProgress(value, prior) {
  check(deletionIdentity(value, prior) && value.revision >= prior.revision &&
    (!prior.sourceUnavailable || value.sourceUnavailable) && value.releasedBytes >= prior.releasedBytes &&
    (value.revision !== prior.revision || value.sourceUnavailable === prior.sourceUnavailable));
  check(value.pieces.every((piece, i) => {
    const old = prior.pieces[i];
    return piece.bytes === old.bytes && piece.releasedBytes >= old.releasedBytes && piece.chargedBytes <= old.chargedBytes &&
      (!['removed', 'changed'].includes(old.state) || piece.state === old.state) &&
      (old.state !== 'unknown' || piece.state !== 'pending') &&
      // The producer can settle an in-progress piece at its attempt revision.
      (value.revision !== prior.revision || recordEqual(piece, old) ||
        old.kind === 'watch_original' && old.state === 'pending' && piece.state === 'pending' &&
          old.chargedBytes === piece.chargedBytes && [old.reason, piece.reason].every(r => r === null || r === 'source_in_use') ||
        old.state === 'unknown' && old.reason === 'removal_unconfirmed' && piece.state !== 'pending');
  }));
}

function qualification(value, context) {
  check(object(value) && value.destinationId === context.destinationId && value.scopeId === context.scopeId && value.modelId === null);
  if (value.available === false) {
    check(closed(value, ['available', 'reason', 'destinationId', 'scopeId', 'modelId']) && text(value.reason, 512));
    return {state: 'unavailable', reason: 'destination_unavailable', capabilityRevision: null, limits: null, formats: []};
  }
  check(closed(value, ['available', 'kind', 'destinationId', 'scopeId', 'modelId', 'capabilityRevision', 'formats', 'limits', 'preparationModes']) &&
    value.available === true && value.kind === 'agent' && opaque(value.capabilityRevision) && Array.isArray(value.formats) &&
    value.formats.length > 0 && value.formats.every(f => MIME.includes(f)) && unique(value.formats) &&
    closed(value.preparationModes, value.formats) && value.formats.every(f => value.preparationModes[f] === 'original_relay'));
  const limits = value.limits;
  check(closed(limits, [...LIMITS, 'officerTextCharacters', 'preparationFormats']) && LIMITS.every(k => int(limits[k], 1)) &&
    limits.fileBytes <= 10 * 1024 * 1024 && limits.agentTextCharacters <= 2000 && int(limits.officerTextCharacters, 1) &&
    Array.isArray(limits.preparationFormats) && limits.preparationFormats.length === 2 &&
    unique(limits.preparationFormats) && limits.preparationFormats.every(f => ['text/plain', 'text/markdown'].includes(f)));
  return {state: 'available', reason: null, capabilityRevision: value.capabilityRevision,
    limits: Object.fromEntries(LIMITS.map(k => [k, limits[k]])), formats: [...value.formats]};
}

const CORE = ['id', 'destination', 'state', 'revision', 'writerGeneration', 'createdAt', 'updatedAt', 'expiresAt'];
function draftCore(value, context) {
  const d = value.destination;
  check(closed(d, ['kind', 'id', 'scopeId', 'modelId', 'conversationId', 'capabilityRevision']) &&
    d.kind === 'agent' && d.id === context.destinationId && d.scopeId === context.scopeId && d.modelId === null &&
    d.conversationId === null && opaque(d.capabilityRevision) && id(value.id) && int(value.revision, 1) &&
    int(value.writerGeneration, 1) && time(value.createdAt) && time(value.updatedAt) && value.expiresAt === null);
}
function summary(value, context) {
  check(closed(value, [...CORE, 'fileCount', 'totalBytes']) && value.state === 'active' && int(value.fileCount) && int(value.totalBytes) &&
    (value.fileCount !== 0 || value.totalBytes === 0));
  draftCore(value, context);
  const {id, revision, writerGeneration, createdAt, updatedAt, fileCount, totalBytes} = value;
  return {id, revision, writerGeneration, createdAt, updatedAt, fileCount, totalBytes, readOnly: true};
}
function fileFacts(f, allowCleanup = false) {
  check(f.preparationKind === 'original_relay' && f.previewAvailable === false && f.expiresAt === null &&
    int(f.sourceGeneration, 1) && (Object.hasOwn(FILE_STATE, f.state) || allowCleanup && CLEANUP.includes(f.state)) &&
    (f.error === null || FILE_ERRORS.has(f.error)) &&
    (f.state === 'cleanup_pending' || (f.state === 'failed' || f.state === 'cleanup_failed') === (f.error !== null)));
}
function wireDraft(value, context, mode) {
  const credentials = mode === 'resume' || mode === 'create' && Object.hasOwn(value, 'capability');
  const replay = mode === 'create' && !credentials;
  check(closed(value, [...CORE, 'text', 'files', ...(credentials ? ['capability', 'writerNonce'] : replay ? ['authorityChanged'] : [])]) &&
    (value.state === 'active' || replay && value.state === 'discarded') && text(value.text, 2000) && Array.isArray(value.files));
  draftCore(value, context);
  if (credentials) check(secret(value.capability) && secret(value.writerNonce));
  if (replay) check(value.authorityChanged === true);
  check(value.files.length <= 1000 && unique(value.files.map(f => f?.id)));
  for (const f of value.files) {
    check(closed(f, ['id', 'name', 'mediaType', 'bytes', 'state', 'error', 'sourceGeneration', 'expiresAt', 'preparationKind',
      'previewAvailable', ...(mode === 'get' ? ['sourceCapability'] : [])]) && id(f.id) && name(f.name) && MIME.includes(f.mediaType) &&
      int(f.bytes) && f.bytes <= 10 * 1024 * 1024 && (mode !== 'get' || f.sourceCapability === null));
    fileFacts(f);
  }
  return value;
}
function wireUpload(value) {
  check(closed(value, ['id', 'fileId', 'fingerprint', 'state', 'error', 'sourceGeneration', 'draftRevision', 'expiresAt',
    'preparationKind', 'previewAvailable']) && id(value.id) && id(value.fileId) && secret(value.fingerprint) && int(value.draftRevision, 1));
  fileFacts(value, true);
  return value;
}
function upMember(draft, upload) {
  return draft.files.find(f => f.id === upload.fileId && f.state === upload.state && f.error === upload.error &&
    f.sourceGeneration === upload.sourceGeneration && draft.revision === upload.draftRevision);
}
function recordEqual(a, b) {
  return a && b && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.hasOwn(b, k) && a[k] === b[k]);
}

function createNativeAgentAttachmentsClient(options = {}) {
  const {enabled, demo, fetch, auth, crypto, AbortController, changed, journal} = options;
  const allowed = enabled === true && demo === false;
  const staged = options.stage23Fixture === true;
  const deletionFixture = staged && options.deletionFixture === true;
  const encoder = new TextEncoder();
  let disposed = false, active = null, operation = null, revision = 0;
  let state, lease, holds, privateHolds, undispatched, journalUnreadable, journalDirty, locals, draft, writer, details, submitted, unsavedEdits;
  let observations, requestHandles, attemptRequests, filePages, fileIndex, fileNavigation, firstPage, retainedDetails;
  let managementRows, deletionRowHandles, deletionView;
  let detailGeneration = 0;
  let deletionGeneration = 0;

  function initial() {
    return {schemaVersion: 1, fixture: false, revision, context: null, features: {...FEATURES},
      qualification: {state: 'unavailable', reason: null, capabilityRevision: null, limits: null, formats: []},
      draftRead: 'idle', busy: false, uncertain: false, error: null, draft: null, unsavedEdit: null, savedDrafts: [], uploads: [],
      attempts: [], requests: [], files: clone(INACTIVE_FILES), detail: null, deletion: null};
  }

  function clearScope() {
    lease?.dispose();
    lease = null;
    state = initial();
    holds = [];
    privateHolds = new Map();
    undispatched = new Map();
    journalUnreadable = false;
    journalDirty = false;
    locals = [];
    draft = writer = null;
    details = new Map();
    submitted = new Map();
    unsavedEdits = new Map();
    observations = new Map();
    requestHandles = new Map();
    attemptRequests = new Map();
    filePages = [];
    fileIndex = -1;
    fileNavigation = new Map();
    firstPage = null;
    retainedDetails = new Map();
    detailGeneration += 1;
    managementRows = new Map();
    deletionRowHandles = new Map();
    deletionView = null;
    deletionGeneration += 1;
  }
  clearScope();

  function currentKey() {
    if (!allowed || disposed || typeof auth !== 'function') return null;
    try { const key = auth(); return typeof key === 'string' && key.length > 0 ? key : null; }
    catch (_) { return null; }
  }
  function invalidate() {
    active?.controller.abort();
    if (active) active.key = null;
    active = operation = null;
    clearScope();
  }
  function current(t) { return t === active && t !== null && !t.controller.signal.aborted && currentKey() === t.key; }
  function guard(t) {
    if (current(t)) return;
    if (active === t) invalidate();
    throw fault('context_changed');
  }
  function allHolds() {
    return [...holds, ...[...privateHolds.values()].filter(r => !holds.some(h => h.attemptId === r.attemptId))];
  }
  function snapshot() {
    if (active && !current(active)) invalidate();
    return clone(state);
  }
  function handleFor(key) {
    if (!details.has(key)) details.set(key, uuid());
    return details.get(key);
  }
  function displayDraft() {
    if (!draft) return null;
    const {id, revision, writerGeneration, createdAt, updatedAt, text} = draft;
    return {id, revision, writerGeneration, createdAt, updatedAt, text,
      readOnly: !writer || writer.id !== id || state.draftRead !== 'complete' || journalUnreadable ||
        allHolds().some(r => r.draftId === id && r.kind === 'draft_resume'),
      files: draft.files.map(f => ({id: f.id, name: f.name, mediaType: f.mediaType, bytes: f.bytes, state: FILE_STATE[f.state],
        error: f.error === null ? null : notice(f.error), preparationKind: 'original_relay', previewAvailable: false,
        expiresAt: null, detailsHandle: handleFor('draft:' + id + ':' + f.id)}))};
  }
  function emit() {
    const records = allHolds();
    state.uncertain = journalUnreadable || records.length > 0;
    if (state.features.deletion && journalUnreadable) {
      hideDeletionAuthority();
      state.files.items = state.files.items.map(row => {
        if (!row.management?.deleteHandle) return row;
        deletionRowHandles.delete(row.management.deleteHandle);
        return {...row, management: {...row.management, deleteHandle: null}};
      });
    }
    state.draft = displayDraft();
    const unsaved = unsavedEdits.get(draft?.id);
    state.unsavedEdit = unsaved ? {draftId: draft.id, text: unsaved.text} : null;
    state.uploads = locals.map(({id, name, mediaType, bytes, fileId, state, error, detailsHandle}) =>
      ({id, name, mediaType, bytes, fileId, state, error, progress: null, detailsHandle}));
    state.attempts = records.filter(r => !deletionRecord(r) || state.features.deletion).map(r => ({id: r.attemptId, draftId: r.draftId ?? null,
      requestId: attemptRequests.get(r.attemptId) ?? null,
      phase: r.kind === 'draft_resume' ? r.step : r.kind === 'deletion_execute' && r.mode === 'recover' ? 'recovering_deletion' : PHASE[r.kind],
      submitted: submitted.get(r.attemptId) ?? null,
      ...(deletionRecord(r) ? {fileId: r.fileId, operationId: r.operationId ?? null} : {})}));
    if (state.detail && state.detail.owner !== 'retained') {
      const detail = state.detail;
      const item = detail.owner === 'selection' ? state.uploads.find(row => row.id === detail.selectionId) :
        state.draft?.id === detail.draftId ? state.draft.files.find(f => f.id === detail.fileId) : null;
      if (item) showDetails(item.detailsHandle);
      else state.detail = null;
    }
    state.revision = ++revision;
    try { if (typeof changed === 'function') changed(snapshot()); } catch (_) { /* Presentation has no write authority. */ }
  }

  function uuid() {
    const value = crypto.randomUUID();
    if (!re(value, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}(?![\s\S])/)) throw fault('unavailable');
    return value;
  }
  async function sha(t, bytes) {
    const data = await crypto.subtle.digest('SHA-256', bytes);
    guard(t);
    const result = Array.from(new Uint8Array(data), byte => byte.toString(16).padStart(2, '0')).join('');
    check(secret(result));
    return result;
  }

  async function replyText(t, response) {
    const length = response.headers?.get('content-length');
    check(length == null || int(Number(length)) && Number(length) <= MAX_REPLY);
    if (typeof response.body?.getReader !== 'function') {
      const raw = await response.text();
      guard(t);
      check(typeof raw === 'string' && raw.length <= MAX_REPLY && encoder.encode(raw).length <= MAX_REPLY);
      return raw;
    }
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', {fatal: true});
    let total = 0, raw = '';
    try {
      for (;;) {
        const {value, done} = await reader.read();
        guard(t);
        if (done) return raw + decoder.decode();
        total += value.byteLength;
        check(total <= MAX_REPLY);
        raw += decoder.decode(value, {stream: true});
      }
    } catch (error) {
      void reader.cancel().catch(() => {});
      throw error;
    } finally { reader.releaseLock(); }
  }

  async function wire(t, path, options = {}) {
    if (!options.deadline) return exchange(t, path, options, t.controller.signal);
    guard(t);
    const controller = new AbortController();
    const cancel = () => controller.abort();
    t.controller.signal.addEventListener('abort', cancel, {once: true});
    const timer = setTimeout(cancel, options.deadline);
    try {
      const result = await exchange(t, path, options, controller.signal);
      guard(t);
      if (controller.signal.aborted) throw fault('network_error');
      return result;
    } finally {
      clearTimeout(timer);
      t.controller.signal.removeEventListener('abort', cancel);
    }
  }

  async function exchange(t, path, {method = 'GET', status = 200, headers = {}, body}, signal) {
    guard(t);
    const binary = method === 'PUT';
    let response, raw;
    try {
      response = await fetch(PREFIX + path, {method, headers: {'X-Live-Room-Key': t.key,
        ...(body === undefined ? {} : {'Content-Type': binary ? 'application/octet-stream' : 'application/json'}), ...headers},
        ...(body === undefined ? {} : {body: binary ? body : JSON.stringify(body)}),
        signal, cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'});
      guard(t);
      raw = await replyText(t, response);
      guard(t);
    } catch (error) {
      if (error?.code === 'context_changed' || error?.code === 'invalid_response') throw error;
      guard(t);
      throw fault('network_error');
    }
    let value;
    try { value = JSON.parse(raw); } catch (_) { throw fault('invalid_response'); }
    check(object(value) && value.schemaVersion === 1 && value.fixture === false &&
      re(value.serverTime, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z(?![\s\S])/) && Number.isFinite(Date.parse(value.serverTime)));
    const {schemaVersion, fixture, serverTime, ...payload} = value;
    if (response.status !== status) {
      check(int(response.status, 400) && response.status <= 599 && closed(payload, ['error']) &&
        closed(payload.error, ['code', 'message', 'retryable']) && re(payload.error.code, /^[a-z0-9_]{1,64}(?![\s\S])/) &&
        text(payload.error.message, 512) && payload.error.retryable === false);
      throw fault(payload.error.code, response.status);
    }
    return payload;
  }

  async function run(t, action, affectsDraft = true) {
    if (!current(t) || operation) return snapshot();
    const job = {};
    let result;
    operation = job;
    state.busy = true;
    state.error = null;
    try { emit(); guard(t); await action(t); guard(t); }
    catch (error) {
      if (current(t)) {
        state.error = notice(error.code);
        if (affectsDraft && (state.draftRead === 'checking' || ['invalid_response', 'incomplete', 'network_error'].includes(error.code))) {
          state.draftRead = 'incomplete';
        }
        if (error.code === 'unauthorized' && error.status === 401) { invalidate(); state.error = notice('unauthorized'); emit(); }
      }
    } finally {
      if (current(t) && job === operation) {
        operation = null;
        state.busy = false;
        emit();
        if (journalDirty && current(t) && !operation) {
          // Starting a refresh clears state.error before its first await.
          result = snapshot();
          journalDirty = false;
          void run(t, refreshScope);
        }
      }
    }
    return result ?? snapshot();
  }

  function contextData() { return {destinationId: state.context.destinationId, scopeId: state.context.scopeId}; }
  function bind(t) {
    if (!journal || typeof journal.bind !== 'function') { journalUnreadable = true; return; }
    const context = state.context;
    lease = journal.bind({ownerKey: t.key, ...contextData(), isCurrent: () => current(t) && context === state.context,
      changed() {
        if (!current(t) || context !== state.context) return;
        if (operation) journalDirty = true;
        else void run(t, refreshScope);
      }});
  }
  async function readJournal(t) {
    if (!lease) { journalUnreadable = true; return; }
    try {
      const rows = await lease.read();
      guard(t);
      holds = rows;
      journalUnreadable = false;
      for (const [id, record] of undispatched) {
        if (!holds.some(row => recordEqual(row, record))) undispatched.delete(id);
      }
    } catch (error) {
      guard(t);
      journalUnreadable = true;
      state.error = notice('journal_unavailable');
    }
  }
  async function journalWrite(t, method, record, next) {
    if (!lease || journalUnreadable) throw fault('journal_unavailable');
    guard(t);
    try {
      const rows = await lease[method](record, next);
      guard(t);
      holds = rows;
      privateHolds.delete(record.attemptId);
      if (method === 'settle') {
        submitted.delete(record.attemptId);
        undispatched.delete(record.attemptId);
        attemptRequests.delete(record.attemptId);
      }
      else emit();
      guard(t);
    } catch (error) {
      guard(t);
      if (error?.code === 'journal_conflict') {
        const wasKnown = holds.some(r => recordEqual(r, record));
        await readJournal(t);
        guard(t);
        if (wasKnown && !holds.some(r => recordEqual(r, record))) privateHolds.set(record.attemptId, record);
        throw fault('journal_conflict');
      }
      if (method === 'capture') undispatched.set(record.attemptId, record);
      else privateHolds.set(record.attemptId, record);
      journalUnreadable = true;
      throw fault('journal_unavailable');
    }
  }
  async function settle(t, record) {
    if (!holds.some(r => recordEqual(r, record)) && recordEqual(privateHolds.get(record.attemptId), record)) {
      privateHolds.delete(record.attemptId);
      submitted.delete(record.attemptId);
      attemptRequests.delete(record.attemptId);
      guard(t);
      return;
    }
    await journalWrite(t, 'settle', record);
  }
  function attempt(kind, data = {}, attemptId = uuid()) { return {version: 1, attemptId, kind, ...data}; }
  function starting(d) { return {draftId: d.id, revision: d.revision, writerGeneration: d.writerGeneration}; }
  async function mutation(t, record, path, req) {
    try { return await wire(t, path, req); }
    catch (error) {
      if (PRE_EFFECT[record.kind]?.[error.code] === error.status && error.status !== null) await settle(t, record);
      throw error;
    }
  }

  async function list(t) {
    const rows = [], ids = new Set(), cursors = new Set();
    let cursor = null;
    for (let i = 0; i < 20; i++) {
      const raw = await wire(t, '/drafts' + query({...contextData(), ...(cursor === null ? {} : {cursor})}));
      guard(t);
      check(closed(raw, ['items', 'nextCursor']) && Array.isArray(raw.items) && raw.items.length <= 50 &&
        (raw.nextCursor === null || re(raw.nextCursor, /^[A-Za-z0-9_-]{1,2048}(?![\s\S])/) && raw.items.length === 50));
      for (const item of raw.items) {
        const row = summary(item, state.context);
        check(!ids.has(row.id));
        ids.add(row.id); rows.push(row);
      }
      cursor = raw.nextCursor;
      if (cursor === null) return rows;
      check(!cursors.has(cursor));
      cursors.add(cursor);
    }
    throw fault('incomplete');
  }

  async function getDraft(t, credential) {
    let raw;
    try { raw = await wire(t, draftPath(credential.id), {headers: cap(credential)}); }
    catch (error) {
      guard(t);
      if (credential === writer && error.status === 403) { writer = null; state.draftRead = 'incomplete'; }
      throw error;
    }
    guard(t);
    const value = wireDraft(raw, state.context, 'get');
    check(value.id === credential.id && value.writerGeneration === credential.generation);
    if (draft?.id === value.id && draft.writerGeneration === value.writerGeneration) check(value.revision >= draft.revision);
    return value;
  }
  function removeLocals(draftId, fileId) {
    locals = locals.filter(row => row.draftId !== draftId || fileId !== undefined && row.fileId !== fileId);
  }
  function setDraft(value, credential = writer) {
    const previous = draft;
    if (value) {
      const {capability, writerNonce, authorityChanged, ...content} = value;
      draft = content;
      if (unsavedEdits.get(value.id)?.text === value.text) unsavedEdits.delete(value.id);
    } else draft = null;
    writer = credential;
    if (previous && previous.id !== value?.id) removeLocals(previous.id);
    if (!previous) return;
    for (const file of previous.files) {
      if (!value || previous.id !== value.id || !value.files.some(row => row.id === file.id)) {
        details.delete('draft:' + previous.id + ':' + file.id);
        removeLocals(previous.id, file.id);
      }
    }
  }
  function credential(value) { return {id: value.id, generation: value.writerGeneration, capability: value.capability, nonce: value.writerNonce}; }
  function updateLocal(hold, member) {
    const row = locals.find(f => f.attemptId === hold.attemptId || f.fileId !== null && f.fileId === hold.fileId);
    if (!row || !member) return;
    Object.assign(row, {draftId: hold.draftId, fileId: member.id, name: member.name, mediaType: member.mediaType, bytes: member.bytes,
      state: FILE_STATE[member.state], error: member.error === null ? null : notice(member.error)});
  }
  function failUnlinkedSelection(hold) {
    const row = locals.find(f => f.attemptId === hold.attemptId && f.fileId === null);
    if (row) { row.state = 'failed'; row.error = notice('incomplete'); }
  }
  function advanced(hold) {
    return !!draft && draft.id === hold.draftId && (draft.writerGeneration > hold.writerGeneration ||
      draft.writerGeneration === hold.writerGeneration && draft.revision > hold.revision);
  }

  function publishRequests() {
    state.requests = [...observations.values()].map(({request}) => {
      if (!requestHandles.has(request.id)) requestHandles.set(request.id, uuid());
      return {...request, actions: {check: requestHandles.get(request.id), finish: null, cleanupSavedCopies: null}};
    }).sort((a, b) => a.createdAt - b.createdAt || (a.id < b.id ? -1 : a.id === b.id ? 0 : 1));
  }

  function compareObservation(value) {
    const r = value.request, prior = observations.get(r.id);
    if (!prior) return;
    const old = prior.request;
    check(old.draftId === r.draftId && old.createdAt === r.createdAt && sameInput(old, r) &&
      recordEqual(prior.draftFence, value.draftFence) && r.revision >= old.revision && (!old.accepted || r.accepted) &&
      (old.delivery !== 'not_submitted' || r.delivery === 'not_submitted') &&
      (old.reason !== EARLIER_DELIVERY_UNRESOLVED || r.reason === EARLIER_DELIVERY_UNRESOLVED) &&
      (old.execution === 'unobserved' || r.execution === old.execution || old.execution === 'started' && r.execution === 'ended') &&
      (prior.draftAdmission !== 'matched' || value.draftAdmission === 'matched'));
  }

  function rememberRequest(value) {
    compareObservation(value);
    observations.set(value.request.id, value);
    publishRequests();
  }

  function withdrawReplies(requestId = null) {
    for (const [key, value] of observations) {
      if (requestId !== null && key !== requestId) continue;
      value.request.reply = {state: 'unknown', text: null};
    }
    publishRequests();
  }

  function stageRead(t, path, fields = {}) {
    return wire(t, path + query({...contextData(), ...fields}), {deadline: 15000});
  }

  async function listRequests(t) {
    const found = new Set(), cursors = new Set(), rows = [];
    let cursor = null, previous = null;
    for (let i = 0; i < 20; i++) {
      const value = await stageRead(t, '/native/requests', cursor === null ? {} : {cursor});
      guard(t);
      check(closed(value, ['items', 'nextCursor']) && Array.isArray(value.items) && value.items.length <= 50 &&
        (value.nextCursor === null || cursorValue(value.nextCursor) && value.items.length > 0));
      for (const item of value.items) {
        const observation = wireObservation(item, state.context, !staged);
        const r = observation.request;
        check(!found.has(r.id) && (!previous || r.createdAt > previous.createdAt ||
          r.createdAt === previous.createdAt && r.id > previous.id));
        compareObservation(observation);
        found.add(r.id); previous = r; rows.push(observation);
      }
      cursor = value.nextCursor;
      if (cursor === null) {
        // A later list is not proof that any already verified request ceased to exist.
        for (const entry of rows) observations.set(entry.request.id, entry);
        for (const [id, value] of observations) if (!found.has(id)) value.request.reply = {state: 'unknown', text: null};
        publishRequests();
        return;
      }
      check(!cursors.has(cursor));
      cursors.add(cursor);
    }
    // Validated request identities remain independently checkable; their cached prose is withdrawn.
    for (const entry of rows) observations.set(entry.request.id, entry);
    withdrawReplies();
    throw fault('requests_incomplete');
  }

  async function getRequest(t, requestId) {
    try {
      const row = await stageRead(t, '/native/requests/' + requestId);
      guard(t);
      check(closed(row, ['observation']));
      const value = wireObservation(row.observation, state.context, !staged);
      check(value.request.id === requestId);
      compareObservation(value);
      return value;
    } catch (error) {
      guard(t); withdrawReplies(requestId);
      throw error;
    }
  }

  function matchesAttempt(value, hold) {
    const frozen = submitted.get(hold.attemptId);
    check(value.request.draftId === hold.draftId && value.draftFence.revision === hold.revision &&
      value.draftFence.writerGeneration === hold.writerGeneration &&
      (hold.requestId === undefined || value.request.id === hold.requestId && value.request.revision >= hold.requestRevision) &&
      (!frozen || sameInput(value.request, frozen)));
  }

  async function observeAttempt(t, hold, value) {
    matchesAttempt(value, hold);
    rememberRequest(value);
    attemptRequests.set(hold.attemptId, value.request.id);
    if (value.draftAdmission !== 'matched') {
      if (hold.kind === 'send_execute' && value.request.reason === EARLIER_DELIVERY_UNRESOLVED) {
        await settle(t, hold);
        guard(t);
        state.error = notice(EARLIER_DELIVERY_UNRESOLVED);
      }
      return;
    }
    const proven = new Set(value.request.files.map(f => f.fileId));
    if (draft?.id === hold.draftId) {
      check(draft.revision === hold.revision && draft.writerGeneration === hold.writerGeneration &&
        sameInput({text: draft.text, files: draft.files.map(f => ({...f, fileId: f.id}))}, value.request));
      for (const f of draft.files) details.delete('draft:' + draft.id + ':' + f.id);
      draft = writer = null;
    }
    locals = locals.filter(row => row.draftId !== hold.draftId || !proven.has(row.fileId));
    state.savedDrafts = state.savedDrafts.filter(row => row.id !== hold.draftId);
    if (state.draftRead === 'incomplete' && !journalUnreadable) state.draftRead = 'complete';
    await settle(t, hold);
    guard(t);
  }

  async function reconcileSend(t, hold) {
    const row = await stageRead(t, '/native/receipts/send_prepare', {draftId: hold.draftId, commandDigest: hold.commandDigest});
    guard(t);
    check(closed(row, ['match']));
    if (row.match === null) return;
    check(closed(row.match, ['draftId', 'requestId']) && row.match.draftId === hold.draftId && id(row.match.requestId) &&
      (hold.requestId === undefined || row.match.requestId === hold.requestId));
    attemptRequests.set(hold.attemptId, row.match.requestId);
    const value = await getRequest(t, row.match.requestId);
    guard(t);
    await observeAttempt(t, hold, value);
  }

  async function recoverStageReads(t) {
    if (!state.features.send) return;
    for (const hold of allHolds()) {
      if (hold.kind !== 'send_prepare' && hold.kind !== 'send_execute') continue;
      if (journalUnreadable) return;
      try { await reconcileSend(t, hold); guard(t); }
      catch (error) { stageFailure(t, error, 'requests_incomplete'); }
    }
  }

  function stageFailure(t, error, code) {
    guard(t);
    if (error.code === 'unauthorized' && error.status === 401) throw error;
    state.error = notice(code);
  }

  async function sendRequest(t, value) {
    if (!state.features.send) throw fault('unavailable');
    ready({qualified: true, activeDraft: true});
    freeDraft();
    if (!text(value, state.qualification.limits.agentTextCharacters) || !value.trim() || value.includes('\0')) throw fault('invalid_request');
    const original = draft.id;
    if (!draft.files.length || draft.files.length > 5 || draft.files.some(f => f.state !== 'ready' || !state.qualification.formats.includes(f.mediaType)) ||
        locals.some(f => (f.draftId === null || f.draftId === original) &&
          (f.state !== 'ready' || !draft.files.some(member => member.id === f.fileId))) ||
        unsavedEdits.has(original) && unsavedEdits.get(original).text !== value) throw fault('draft_conflict');
    if (draft.text !== value) {
      await edit(t, value);
      guard(t);
    }
    ready({qualified: true, activeDraft: true});
    freeDraft();
    check(draft.id === original && draft.text === value);
    const key = uuid(), sendKey = uuid();
    const commandDigest = await sha(t, encoder.encode(key));
    guard(t);
    const before = draft, draftWriter = writer;
    const hold = attempt('send_prepare', {...starting(before), commandDigest});
    submitted.set(hold.attemptId, {text: value, files: before.files.map(f => ({fileId: f.id, name: f.name,
      mediaType: f.mediaType, bytes: f.bytes}))});
    await journalWrite(t, 'capture', hold);
    guard(t);
    const raw = await wire(t, '/native' + draftPath(before.id) + '/send-intent', {method: 'POST', status: 201, deadline: 15000,
      headers: {...cap(draftWriter), 'Idempotency-Key': key}, body: fence(before, draftWriter)});
    guard(t);
    check(closed(raw, ['observation', 'requestCapability', 'fingerprint']) && secret(raw.requestCapability) && secret(raw.fingerprint));
    const prepared = wireObservation(raw.observation, state.context, !staged);
    matchesAttempt(prepared, hold);
    check(prepared.draftAdmission === 'unadmitted' && prepared.request.preparation === 'intent' && prepared.request.reason === null);
    rememberRequest(prepared);
    attemptRequests.set(hold.attemptId, prepared.request.id);
    const executing = {...hold, kind: 'send_execute', requestId: prepared.request.id, requestRevision: prepared.request.revision};
    await journalWrite(t, 'replace', hold, executing);
    guard(t);
    const sent = await wire(t, '/native/requests/' + executing.requestId + '/send', {method: 'POST', deadline: 30000,
      headers: {'X-Attachment-Request-Capability': raw.requestCapability, 'Idempotency-Key': sendKey},
      body: {revision: executing.requestRevision, fingerprint: raw.fingerprint}});
    guard(t);
    check(closed(sent, ['observation']));
    await observeAttempt(t, executing, wireObservation(sent.observation, state.context, !staged));
    guard(t);
    if (state.features.files) await readFilesFirst(t);
  }

  async function checkRequest(t, handle) {
    if (!state.features.send) throw fault('unavailable');
    const requestId = state.requests.find(row => row.actions.check === handle)?.id;
    if (!requestId) throw fault('invalid_request');
    const observation = await getRequest(t, requestId);
    guard(t);
    const hold = allHolds().find(record => record.kind === 'send_execute' && record.requestId === requestId ||
      record.kind === 'send_prepare' && attemptRequests.get(record.attemptId) === requestId);
    if (hold && !journalUnreadable) await observeAttempt(t, hold, observation);
    else rememberRequest(observation);
  }

  function clearFilePage(enabled = state.features.files) {
    detailGeneration += 1;
    if (state.detail?.owner === 'retained') state.detail = null;
    filePages = [];
    fileIndex = -1;
    fileNavigation = new Map();
    retainedDetails = new Map();
    deletionRowHandles = new Map();
    firstPage = enabled ? uuid() : null;
    state.files = {...clone(INACTIVE_FILES), state: enabled ? 'incomplete' : 'unavailable', first: firstPage};
  }

  async function fetchFiles(t, cursor, index) {
    const value = await stageRead(t, '/files', {conversationId: '', ...(cursor === null ? {} : {cursor})});
    guard(t);
    check(closed(value, ['destinationId', 'scopeId', 'items', 'associationCount', 'unclassifiedAssociationCount', 'nextCursor']) &&
      sameContext(value, state.context) && Array.isArray(value.items) && value.items.length <= 50 &&
      int(value.associationCount, value.items.length) && int(value.unclassifiedAssociationCount) &&
      (value.nextCursor === null || cursorValue(value.nextCursor) && value.items.length === 50 &&
        !filePages.slice(0, index + 1).some(page => page.cursor === value.nextCursor)));
    const rows = value.items.map(wireFileRow);
    if (state.features.deletion) check(rows.every(deletionFileProof));
    const keys = rows.map(r => r.id + ':' + r.requestId);
    check(unique(keys) && !filePages.slice(0, index).some(page => page.keys.some(key => keys.includes(key))));
    filePages.splice(index, filePages.length - index, {cursor, keys});
    const count = filePages.reduce((sum, page) => sum + page.keys.length, 0);
    check(value.nextCursor === null ? value.associationCount === count : value.associationCount > count);
    fileIndex = index;
    fileNavigation = new Map();
    retainedDetails = new Map();
    deletionRowHandles = new Map();
    let previous = null, next = null;
    if (index > 0) {
      previous = uuid(); fileNavigation.set(previous, {index: index - 1, cursor: filePages[index - 1].cursor});
    }
    if (value.nextCursor !== null && index + 1 < 20) {
      next = uuid(); fileNavigation.set(next, {index: index + 1, cursor: value.nextCursor});
    }
    state.files = {state: value.unclassifiedAssociationCount || value.nextCursor !== null && !next ? 'incomplete' : 'complete',
      associationCount: value.associationCount, unclassifiedAssociationCount: value.unclassifiedAssociationCount,
      first: firstPage, previous, next, items: rows.map(r => {
        const detailsHandle = uuid();
        retainedDetails.set(detailsHandle, r);
        return {...r, detailsHandle, management: deletionManagement(r)};
      })};
  }

  async function readFilesFirst(t) {
    clearFilePage();
    state.files.state = 'checking';
    emit(); guard(t);
    try { await fetchFiles(t, null, 0); guard(t); }
    catch (error) {
      guard(t);
      if (state.features.deletion) hideDeletionAuthority();
      clearFilePage(); stageFailure(t, error, 'files_incomplete');
    }
  }

  async function filesPage(t, handle) {
    if (!state.features.files) throw fault('unavailable');
    if (handle !== null && handle === firstPage) return readFilesFirst(t);
    const page = fileNavigation.get(handle);
    if (!page || Math.abs(page.index - fileIndex) !== 1) throw fault('invalid_request');
    if (state.detail?.owner === 'retained') state.detail = null;
    state.files = {...clone(INACTIVE_FILES), state: 'checking', first: firstPage};
    retainedDetails = new Map();
    deletionRowHandles = new Map();
    emit(); guard(t);
    try { await fetchFiles(t, page.cursor, page.index); guard(t); }
    catch (error) {
      guard(t);
      if (state.features.deletion) hideDeletionAuthority();
      clearFilePage(); stageFailure(t, error, 'files_incomplete');
    }
  }

  async function retainedFileDetails(t, handle) {
    const prior = retainedDetails.get(handle);
    if (!prior || !state.features.files) throw fault('invalid_request');
    const generation = ++detailGeneration;
    if (state.detail?.owner === 'retained') state.detail = null;
    emit(); guard(t);
    try {
      const value = await stageRead(t, '/native/files/' + prior.id + '/details', {requestId: prior.requestId});
      guard(t);
      if (generation !== detailGeneration) return;
      check(closed(value, ['destinationId', 'scopeId', 'item']) && sameContext(value, state.context));
      const row = wireFileRow(value.item);
      if (state.features.deletion) check(deletionFileProof(row));
      check(retainedDetails.get(handle) === prior && ['id', 'requestId', 'name', 'mediaType', 'bytes', 'cataloguedAt'].every(k => row[k] === prior[k]));
      retainedDetails.set(handle, row);
      state.files.items = state.files.items.map(item => item.detailsHandle === handle ? {...row, detailsHandle: handle, management: item.management} : item);
      const {id: fileId, requestId, name, mediaType, bytes, savedCopyAvailable} = row;
      state.detail = {owner: 'retained', selectionId: null, draftId: null, fileId, requestId, name, mediaType, bytes,
        savedCopyAvailable, kind: 'metadata_fallback', previewAvailable: false};
    } catch (error) {
      guard(t);
      if (error.code === 'unauthorized' && error.status === 401) throw error;
      if (generation !== detailGeneration) return;
      state.files.items = state.files.items.map(item => {
        if (item.detailsHandle !== handle) return item;
        if (item.management) {
          deletionRowHandles.delete(item.management.deleteHandle);
          deletionRowHandles.delete(item.management.operationHandle);
        }
        return {...item, savedCopyAvailable: false, savedCopyState: 'unknown', management: null};
      });
      state.detail = null;
      stageFailure(t, error, 'files_incomplete');
    }
  }

  function deletionFileProof(row) {
    return row.bytes > 0 && !row.savedCopyAvailable && ['unknown', 'removed'].includes(row.savedCopyState);
  }

  function deletionHeld(fileId) {
    return allHolds().some(r => deletionRecord(r) && r.fileId === fileId);
  }

  function deletionManagement(row) {
    if (!state.features.deletion) return null;
    const entry = managementRows.get(associationKey(row.id, row.requestId));
    if (!entry) return null;
    const deleteHandle = entry.canPrepare && !journalUnreadable && !deletionHeld(row.id) ? uuid() : null;
    const operationHandle = entry.operation ? uuid() : null;
    if (deleteHandle) deletionRowHandles.set(deleteHandle, {kind: 'prepare', entry, bytes: row.bytes});
    if (operationHandle) deletionRowHandles.set(operationHandle, {kind: 'open', entry, bytes: row.bytes});
    return {generation: entry.managementGeneration, deleteHandle, operationHandle};
  }

  async function readDeletionFiles(t) {
    managementRows = new Map();
    const rows = new Map(), sources = new Set(), slots = new Set(), cursors = new Set();
    const finish = performance.now() + 15000;
    let cursor = null;
    try {
      for (let page = 0; page < 20; page += 1) {
        const deadline = Math.floor(finish - performance.now());
        if (deadline <= 0) throw fault('deletion_incomplete');
        const fields = {...contextData(), ...(cursor === null ? {} : {cursor})};
        const value = await wire(t, '/native/files/management' + query(fields), {deadline});
        guard(t);
        check(closed(value, ['destinationId', 'scopeId', 'items', 'nextCursor']) && sameContext(value, state.context) &&
          Array.isArray(value.items) && value.items.length <= 50 && (value.nextCursor === null ||
            cursorValue(value.nextCursor) && value.items.length === 50 && !cursors.has(value.nextCursor)));
        for (const row of value.items.map(wireManagementRow)) {
          check(!sources.has(row.fileId) && (!row.operation || !slots.has(row.operation.id)));
          sources.add(row.fileId);
          if (row.operation) slots.add(row.operation.id);
          rows.set(associationKey(row.fileId, row.requestId), row);
        }
        cursor = value.nextCursor;
        if (cursor === null) break;
        cursors.add(cursor);
      }
      if (cursor !== null) throw fault('deletion_incomplete');
      managementRows = rows;
    } catch (error) {
      stageFailure(t, error, 'deletion_incomplete');
      hideDeletionAuthority();
    }
    if (deletionView) {
      const current = [...managementRows.values()].find(row => row.fileId === deletionView.value.fileId)?.operation;
      const before = deletionView.value;
      if (current?.id === before.id && current.previewGeneration > before.previewGeneration) beginDeletionView();
      else if (!current || current.id !== before.id || current.previewGeneration !== before.previewGeneration) hideDeletionAuthority();
    }
    await readFilesFirst(t);
  }

  function beginDeletionView() {
    deletionView = null;
    state.deletion = null;
    return ++deletionGeneration;
  }

  function hideDeletionAuthority() {
    if (!deletionView) return;
    deletionView.grant = null;
    deletionView.actions = {execute: null, recover: null};
    state.deletion = {...state.deletion, confirmHandle: null, actions: {...deletionView.actions}};
  }

  function showDeletion(value, generation, confirmation = null) {
    if (generation !== deletionGeneration) return;
    if (deletionView) checkDeletionProgress(value, deletionView.value);
    const allowedHere = !journalUnreadable && !deletionHeld(value.fileId);
    const actions = {execute: allowedHere && value.canExecute ? uuid() : null, recover: allowedHere && value.canRecover ? uuid() : null};
    const grant = allowedHere && confirmation ? {...confirmation, handle: uuid()} : null;
    const handle = deletionView?.handle ?? uuid();
    deletionView = {value, handle, grant, actions};
    state.deletion = {id: value.id, fileId: value.fileId, revision: value.revision, copies: value.pieces,
      state: value.state, releasedBytes: value.releasedBytes, sourceUnavailable: value.sourceUnavailable,
      reason: null, operationHandle: handle, confirmHandle: grant?.handle ?? null, actions: {...actions}};
  }

  function retireDeletionFiles() {
    managementRows = new Map();
    clearFilePage();
  }

  async function deletionStatus(t, operationId, fileId) {
    const data = await stageRead(t, '/native/deletions/' + operationId);
    check(closed(data, ['operation']));
    const value = wireDeletion(data.operation, state.context, fileId, operationId);
    compareViewedDeletion(value, deletionView?.value);
    return value;
  }

  function compareViewedDeletion(value, prior) {
    if (!prior || value.id !== prior.id || value.fileId !== prior.fileId) return;
    if (deletionIdentity(value, prior)) checkDeletionProgress(value, prior);
    else check(value.previewGeneration > prior.previewGeneration && !prior.sourceUnavailable);
  }

  async function openDeletion(t, handle) {
    const row = deletionRowHandles.get(handle);
    const previous = deletionView;
    const owned = row?.kind === 'open' ? {id: row.entry.operation.id, fileId: row.entry.fileId,
      previewGeneration: row.entry.operation.previewGeneration} : previous?.handle === handle ? previous.value : null;
    if (!owned) throw fault('invalid_request');
    const generation = beginDeletionView();
    emit(); guard(t);
    const value = await deletionStatus(t, owned.id, owned.fileId);
    check(deletionIdentity(value, owned) && (!row || value.pieces.every(piece => piece.bytes === row.bytes)));
    compareViewedDeletion(value, previous?.value);
    showDeletion(value, generation);
  }

  async function refreshDeletionView(t) {
    if (!deletionView) return;
    const value = deletionView.value;
    const generation = deletionGeneration;
    try {
      const fresh = await deletionStatus(t, value.id, value.fileId);
      if (generation !== deletionGeneration) return;
      if (fresh.previewGeneration > value.previewGeneration && !value.sourceUnavailable) beginDeletionView();
      else showDeletion(fresh, generation);
    } catch (error) { stageFailure(t, error, 'deletion_incomplete'); }
  }

  function canStartDeletion(fileId) {
    if (!state.features.deletion) throw fault('unavailable');
    if (journalUnreadable || !lease) throw fault('journal_unavailable');
    if (deletionHeld(fileId)) throw fault('journal_conflict');
  }

  async function postDeletion(t, hold, currentAction, path, options) {
    await readJournal(t);
    guard(t);
    if (journalUnreadable) throw fault('journal_unavailable');
    if (!holds.some(r => recordEqual(r, hold))) throw fault('journal_conflict');
    if (!currentAction()) {
      undispatched.set(hold.attemptId, hold);
      await settle(t, hold);
      return null;
    }
    retireDeletionFiles();
    return wire(t, path, {method: 'POST', ...options});
  }

  async function prepareDeletion(t, handle) {
    const row = deletionRowHandles.get(handle);
    if (row?.kind !== 'prepare' || !row.entry.canPrepare) throw fault('invalid_request');
    const {fileId, managementGeneration} = row.entry;
    canStartDeletion(fileId);
    const generation = beginDeletionView();
    const key = uuid();
    const commandDigest = await sha(t, encoder.encode(key));
    const hold = attempt('deletion_prepare', {fileId, managementGeneration, commandDigest});
    await journalWrite(t, 'capture', hold);
    const data = await postDeletion(t, hold, () => generation === deletionGeneration && deletionRowHandles.get(handle) === row,
      '/native/files/' + fileId + '/deletion-intent', {status: 201, deadline: 15000,
        headers: {'Idempotency-Key': key}, body: {...contextData(), managementGeneration}});
    if (data === null) return;
    check(closed(data, ['operation', 'confirmation']) && closed(data.confirmation, ['capability', 'fingerprint']) &&
      secret(data.confirmation.capability) && secret(data.confirmation.fingerprint));
    const value = wireDeletion(data.operation, state.context, fileId);
    check(value.state === 'confirm' && value.previewGeneration === managementGeneration + 1 && value.pieces.every(p => p.bytes === row.bytes));
    await settle(t, hold);
    guard(t);
    showDeletion(value, generation, data.confirmation);
    await readDeletionFiles(t);
  }

  function deletionFence(value) {
    return {fileId: value.fileId, operationId: value.id, previewGeneration: value.previewGeneration, revision: value.revision};
  }

  async function executeHeldDeletion(t, hold, prior, generation, currentAction) {
    const {fileId, operationId, previewGeneration, revision, mode} = hold;
    const data = await postDeletion(t, hold, currentAction, '/native/deletions/' + operationId + '/execute', {deadline: 30000,
      body: {...contextData(), fileId, previewGeneration, revision, mode}});
    if (data === null) return;
    check(closed(data, ['operation']));
    const value = wireDeletion(data.operation, state.context, fileId, operationId);
    checkDeletionProgress(value, prior);
    await settle(t, hold);
    guard(t);
    showDeletion(value, generation);
    await readDeletionFiles(t);
  }

  async function confirmDeletion(t, handle) {
    const view = deletionView;
    const grant = view?.grant;
    if (!grant || grant.handle !== handle) throw fault('invalid_request');
    const generation = deletionGeneration, prior = view.value;
    canStartDeletion(prior.fileId);
    const key = uuid();
    const commandDigest = await sha(t, encoder.encode(key));
    if (deletionView !== view || deletionGeneration !== generation || view.grant !== grant) return;
    hideDeletionAuthority();
    const hold = attempt('deletion_confirm', {...deletionFence(prior), commandDigest});
    await journalWrite(t, 'capture', hold);
    const {fileId, operationId, previewGeneration, revision} = hold;
    const data = await postDeletion(t, hold, () => deletionView === view && deletionGeneration === generation,
      '/native/deletions/' + operationId + '/confirm', {deadline: 15000,
        headers: {'Idempotency-Key': key, 'X-Attachment-Deletion-Capability': grant.capability},
        body: {...contextData(), fileId, previewGeneration, revision, fingerprint: grant.fingerprint}});
    if (data === null) return;
    check(closed(data, ['operation', 'confirmedNow']) && typeof data.confirmedNow === 'boolean');
    const value = wireDeletion(data.operation, state.context, fileId, operationId);
    checkDeletionProgress(value, prior);
    check(value.sourceUnavailable && value.revision > revision);
    if (data.confirmedNow) {
      check(value.revision === revision + 1 && value.state === 'deleting' && value.pieces.every(p => p.state === 'pending'));
    }
    if (data.confirmedNow && value.canExecute) {
      const executing = attempt('deletion_execute', {...deletionFence(value), mode: 'execute'}, hold.attemptId);
      showDeletion(value, generation);
      await journalWrite(t, 'replace', hold, executing);
      await executeHeldDeletion(t, executing, value, generation, () => state.features.deletion);
      return;
    }
    await settle(t, hold);
    guard(t);
    showDeletion(value, generation);
    await readDeletionFiles(t);
  }

  async function executeDeletion(t, handle) {
    const view = deletionView;
    const mode = handle && view?.actions.execute === handle ? 'execute' : handle && view?.actions.recover === handle ? 'recover' : null;
    if (!mode) throw fault('invalid_request');
    const generation = deletionGeneration, prior = view.value;
    canStartDeletion(prior.fileId);
    hideDeletionAuthority();
    const hold = attempt('deletion_execute', {...deletionFence(prior), mode});
    await journalWrite(t, 'capture', hold);
    await executeHeldDeletion(t, hold, prior, generation, () => deletionView === view && deletionGeneration === generation);
  }

  async function reconcileDeletion(t, hold) {
    if (hold.kind === 'deletion_prepare') {
      const reply = await stageRead(t, '/native/receipts/deletion_prepare', {fileId: hold.fileId, commandDigest: hold.commandDigest});
      check(closed(reply, ['destinationId', 'scopeId', 'fileId', 'match']) && sameContext(reply, state.context) && reply.fileId === hold.fileId);
      if (reply.match === null) return;
      const {match} = reply;
      check(closed(match, ['operationId', 'previewGeneration']) && id(match.operationId) &&
        int(match.previewGeneration, 1) && match.previewGeneration === hold.managementGeneration + 1);
      const value = await deletionStatus(t, match.operationId, hold.fileId);
      if (value.previewGeneration !== match.previewGeneration) return;
    } else {
      const value = await deletionStatus(t, hold.operationId, hold.fileId);
      const advanced = value.previewGeneration === hold.previewGeneration && value.sourceUnavailable && value.revision > hold.revision;
      const superseded = hold.kind === 'deletion_confirm' && value.previewGeneration > hold.previewGeneration;
      if (!advanced && !superseded) return;
    }
    await settle(t, hold);
  }

  async function receipt(t, hold) {
    const v = await wire(t, '/native/receipts/' + hold.kind + query({...contextData(), commandDigest: hold.commandDigest,
      ...(hold.draftId === undefined ? {} : {draftId: hold.draftId})}));
    guard(t);
    check(closed(v, ['match']));
    const r = v.match;
    if (r === null) return null;
    const keys = hold.kind === 'draft_create' ? ['draftId'] : hold.kind === 'upload_reserve' ? ['draftId', 'uploadId', 'fileId'] :
      ['draftId', 'writerGeneration', 'cleanupPending'];
    check(closed(r, keys) && id(r.draftId) && (hold.draftId === undefined || hold.draftId === r.draftId));
    if (hold.kind === 'upload_reserve') check(id(r.uploadId) && id(r.fileId));
    if (hold.kind === 'draft_discard') check(r.writerGeneration === hold.writerGeneration + 1 && typeof r.cleanupPending === 'boolean');
    return r;
  }

  async function reconcile(t) {
    if (journalUnreadable) return;
    for (const hold of allHolds()) {
      guard(t);
      if (!allHolds().some(r => recordEqual(r, hold))) continue;
      if (recordEqual(undispatched.get(hold.attemptId), hold)) {
        await settle(t, hold);
        continue;
      }
      if (hold.kind === 'send_prepare' || hold.kind === 'send_execute' || deletionRecord(hold)) continue;
      const exact = draft?.id === hold.draftId && writer?.id === hold.draftId;
      if (hold.commandDigest) {
        const match = await receipt(t, hold);
        if (hold.kind === 'draft_create') {
          if (match) await settle(t, hold);
        } else if (hold.kind === 'draft_discard') {
          if (match) {
            await settle(t, hold);
            guard(t);
            unsavedEdits.delete(hold.draftId);
            removeLocals(hold.draftId);
            if (draft?.id === hold.draftId) {
              setDraft(null, null);
              state.draftRead = 'complete';
            }
          } else if (exact && advanced(hold)) await settle(t, hold);
        } else if (match && exact) {
          const member = draft.files.find(f => f.id === match.fileId);
          if (member) {
            const up = wireUpload(await wire(t, draftPath(hold.draftId) + '/uploads/' + match.uploadId, {headers: cap(writer)}));
            guard(t);
            check(up.id === match.uploadId && up.fileId === match.fileId && upMember(draft, up));
            updateLocal(hold, member);
          }
          if (member || advanced(hold)) {
            if (!member) failUnlinkedSelection(hold);
            await settle(t, hold);
          }
        } else if (!match && exact && advanced(hold)) {
          failUnlinkedSelection(hold);
          await settle(t, hold);
        }
      } else if (hold.kind === 'draft_resume') {
        const visible = state.savedDrafts.find(row => row.id === hold.draftId);
        if (visible?.writerGeneration > hold.writerGeneration) await settle(t, hold);
      } else if (exact && hold.kind === 'upload_put') {
        const member = draft.files.find(f => f.id === hold.fileId);
        if (!member) {
          if (advanced(hold)) await settle(t, hold);
          continue;
        }
        const up = wireUpload(await wire(t, draftPath(hold.draftId) + '/uploads/' + hold.uploadId, {headers: cap(writer)}));
        guard(t);
        check(up.id === hold.uploadId && up.fileId === hold.fileId && upMember(draft, up));
        updateLocal(hold, member);
        if (['ready', 'failed'].includes(member.state) || member.state === 'uploading' && draft.writerGeneration > hold.writerGeneration) {
          await settle(t, hold);
        }
      } else if (exact && advanced(hold)) await settle(t, hold);
    }
  }

  async function refreshScope(t) {
    state.draftRead = 'checking';
    if (staged || state.features.send) {
      withdrawReplies();
      if (state.features.files) clearFilePage();
    }
    if (state.features.deletion) hideDeletionAuthority();
    emit();
    guard(t);
    const picked = selection(await wire(t, '/native/selection' + query({destinationId: t.destinationId})), t.destinationId, staged, deletionFixture);
    guard(t);
    if (state.context?.scopeId !== picked.context?.scopeId || state.features.deletion !== picked.features.deletion) {
      clearScope();
      state.busy = true;
      state.context = picked.context;
      state.draftRead = 'checking';
      if (picked.context) bind(t);
    }
    state.features = picked.features;
    if (!state.context) { state.draftRead = 'incomplete'; return; }
    if (!state.features.files) clearFilePage(false);
    if (!state.features.send) { observations.clear(); requestHandles.clear(); publishRequests(); }
    await readJournal(t);
    guard(t);
    if (state.features.deletion) {
      state.draftRead = 'idle';
      for (const hold of allHolds()) {
        if (journalUnreadable) break;
        if (!deletionRecord(hold)) continue;
        try {
          if (recordEqual(undispatched.get(hold.attemptId), hold)) await settle(t, hold);
          else await reconcileDeletion(t, hold);
          guard(t);
        } catch (error) { stageFailure(t, error, 'deletion_incomplete'); }
      }
      await refreshDeletionView(t);
      guard(t);
      await readDeletionFiles(t);
      return;
    }
    state.qualification = {state: 'checking', reason: null, capabilityRevision: null, limits: null, formats: []};
    const caps = await wire(t, '/capabilities' + query({...contextData(), modelId: ''}));
    guard(t);
    state.qualification = qualification(caps, state.context);
    const summaries = state.features.drafts ? await list(t) : [];
    guard(t);
    state.savedDrafts = summaries;
    if (writer) {
      try { const exact = await getDraft(t, writer); guard(t); setDraft(exact); }
      catch (error) {
        guard(t);
        const sending = state.features.send && allHolds().some(hold =>
          (hold.kind === 'send_prepare' || hold.kind === 'send_execute') && hold.draftId === writer.id);
        if (error.status !== 403 && !(sending && error.status === 409 && error.code === 'draft_admitted')) throw error;
        writer = null;
        state.draftRead = 'incomplete';
      }
    }
    if (state.draftRead === 'checking') state.draftRead = journalUnreadable ? 'incomplete' : 'complete';
    await reconcile(t);
    guard(t);
    if (state.features.send) {
      try { await listRequests(t); guard(t); }
      catch (error) { withdrawReplies(); stageFailure(t, error, 'requests_incomplete'); }
      await recoverStageReads(t);
      guard(t);
    }
    if (state.features.files) await readFilesFirst(t);
  }

  function ready({qualified = false, activeDraft = false} = {}) {
    if (journalUnreadable || !lease) throw fault('journal_unavailable');
    if (!state.context || !state.features.drafts || state.draftRead !== 'complete') throw fault('incomplete');
    if (qualified && state.qualification.state !== 'available') throw fault('destination_unavailable');
    if (activeDraft && (!draft || !writer || writer.id !== draft.id || writer.generation !== draft.writerGeneration)) throw fault('draft_conflict');
  }

  function freeDraft(except) {
    if (allHolds().some(r => r.draftId === draft?.id && !recordEqual(r, except))) throw fault('journal_conflict');
  }

  async function create(t) {
    ready({qualified: true});
    if (draft || allHolds().some(r => r.kind === 'draft_create')) throw fault('journal_conflict');
    const key = uuid();
    const hold = attempt('draft_create', {commandDigest: await sha(t, encoder.encode(key))});
    guard(t);
    await journalWrite(t, 'capture', hold);
    const result = wireDraft(await mutation(t, hold, '/drafts', {method: 'POST', status: 201,
      headers: {'Idempotency-Key': key}, body: {...contextData(), modelId: null, conversationId: null,
        capabilityRevision: state.qualification.capabilityRevision}}), state.context, 'create');
    guard(t);
    check(result.destination.capabilityRevision === state.qualification.capabilityRevision);
    if (result.authorityChanged) {
      const match = await receipt(t, hold);
      check(match?.draftId === result.id);
      await settle(t, hold);
      const summaries = await list(t);
      guard(t);
      state.savedDrafts = summaries;
      return;
    }
    check(result.revision === 1 && result.writerGeneration === 1 && result.text === '' && result.files.length === 0);
    await settle(t, hold);
    guard(t);
    setDraft(result, credential(result));
  }

  function validateFiles(files) {
    const {formats, limits} = state.qualification;
    if (!Array.isArray(files) || files.length === 0) throw fault('invalid_request');
    const selected = Array.from(files, file => {
      if (!file || !name(file.name) || !int(file.size) || typeof file.arrayBuffer !== 'function') throw fault('invalid_request');
      if (!formats.includes(file.type)) throw fault('unsupported_type');
      if (file.size > limits.fileBytes) throw fault('file_too_large');
      return {file, name: file.name, mediaType: file.type, bytes: file.size};
    });
    const counted = [...(draft?.files ?? []), ...locals.filter(row => row.fileId === null && row.state !== 'failed')];
    if (counted.length + selected.length > limits.fileCount ||
        [...counted, ...selected].reduce((sum, row) => sum + row.bytes, 0) > limits.draftBytes) throw fault('draft_limit');
    return selected;
  }

  async function uploadFile(t, file, row) {
    row.draftId = draft.id;
    let bytes;
    try {
      bytes = await file.arrayBuffer();
      guard(t);
      if (!(bytes instanceof ArrayBuffer) || bytes.byteLength !== row.bytes) throw fault('file_unavailable');
    } catch (error) {
      guard(t);
      row.state = 'failed'; row.error = notice('file_unavailable');
      return;
    }
    const digest = await sha(t, bytes), key = uuid();
    const commandDigest = await sha(t, encoder.encode(key));
    guard(t);
    const before = draft;
    const hold = attempt('upload_reserve', {...starting(before), commandDigest});
    row.draftId = before.id; row.attemptId = hold.attemptId; row.state = 'reserving';
    try { await journalWrite(t, 'capture', hold); }
    catch (error) {
      guard(t);
      row.state = 'failed'; row.error = notice(error.code);
      throw error;
    }
    let reservation;
    try {
      reservation = wireUpload(await mutation(t, hold, draftPath(before.id) + '/upload-intents', {method: 'POST', status: 201,
        headers: {...cap(writer), 'Idempotency-Key': key}, body: {...fence(before, writer), name: row.name,
          mediaType: row.mediaType, bytes: row.bytes, digest}}));
      guard(t);
    } catch (error) {
      guard(t);
      if (!allHolds().some(r => r.attemptId === hold.attemptId)) {
        row.state = 'failed'; row.error = notice(error.code);
      }
      throw error;
    }
    check(reservation.state === 'uploading' && reservation.draftRevision === before.revision + 1 &&
      !before.files.some(f => f.id === reservation.fileId));
    const saved = await getDraft(t, writer);
    guard(t);
    const member = upMember(saved, reservation);
    check(member && member.name === row.name && member.mediaType === row.mediaType && member.bytes === row.bytes);
    const put = attempt('upload_put', {...starting(saved), uploadId: reservation.id, fileId: reservation.fileId}, hold.attemptId);
    await journalWrite(t, 'replace', hold, put);
    guard(t);
    updateLocal(put, member);
    setDraft(saved);
    emit(); guard(t);
    const result = wireUpload(await mutation(t, put, draftPath(saved.id) + '/uploads/' + reservation.id, {method: 'PUT',
      body: bytes, headers: {...cap(writer), 'X-Attachment-Writer-Generation': String(saved.writerGeneration),
        'X-Attachment-Draft-Revision': String(saved.revision), 'X-Attachment-Writer-Nonce': writer.nonce,
        'X-Attachment-Upload-Fingerprint': reservation.fingerprint}}));
    guard(t);
    check(result.id === put.uploadId && result.fileId === put.fileId && result.fingerprint === reservation.fingerprint &&
      result.sourceGeneration === reservation.sourceGeneration && result.draftRevision === saved.revision + 1 && ['ready', 'failed'].includes(result.state));
    const exact = await getDraft(t, writer);
    guard(t);
    const stored = upMember(exact, result);
    check(stored);
    await settle(t, put);
    guard(t);
    updateLocal(put, stored);
    setDraft(exact);
  }

  async function add(t, files) {
    ready({qualified: true, activeDraft: draft !== null});
    if (draft) freeDraft();
    else if (allHolds().some(r => r.kind === 'draft_create')) throw fault('journal_conflict');
    const selected = validateFiles(files);
    const batch = selected.map(({file, name, mediaType, bytes}) => ({file, row: {id: uuid(), name, mediaType, bytes,
      draftId: draft?.id ?? null, fileId: null, attemptId: null, state: 'reading', error: null, detailsHandle: uuid()}}));
    locals.push(...batch.map(item => item.row));
    emit(); guard(t);
    try {
      if (!draft) await create(t);
      guard(t);
      if (!draft || !writer) throw fault('incomplete');
      for (const {file, row} of batch) {
        guard(t); freeDraft();
        await uploadFile(t, file, row);
        guard(t); emit();
      }
    } catch (error) {
      guard(t);
      for (const {row} of batch) {
        if (row.state === 'reading') { row.state = 'failed'; row.error = notice(error.code); }
      }
      throw error;
    }
  }

  async function edit(t, newText) {
    ready({activeDraft: true}); freeDraft();
    if (!text(newText, state.qualification.limits?.agentTextCharacters ?? 2000)) throw fault('text_limit');
    const before = draft, hold = attempt('draft_edit', starting(before));
    unsavedEdits.set(before.id, {text: newText, attemptId: hold.attemptId});
    submitted.set(hold.attemptId, {text: newText, files: []});
    await journalWrite(t, 'capture', hold);
    const updated = wireDraft(await mutation(t, hold, draftPath(before.id), {method: 'PATCH', headers: cap(writer),
      body: {...fence(before, writer), text: newText}}), state.context, 'mutate');
    guard(t);
    check(updated.id === before.id && updated.writerGeneration === before.writerGeneration && updated.revision === before.revision + 1 &&
      updated.text === newText && updated.files.length === before.files.length && updated.files.every((f, i) => f.id === before.files[i].id));
    await settle(t, hold);
    guard(t); setDraft(updated);
  }

  async function remove(t, fileId) {
    ready({activeDraft: true});
    const before = draft, member = before.files.find(f => f.id === fileId);
    if (!member) throw fault('invalid_request');
    const prior = allHolds().find(r => r.kind === 'upload_put' && r.draftId === before.id && r.fileId === fileId &&
      before.writerGeneration > r.writerGeneration && before.revision > r.revision);
    freeDraft(prior);
    const hold = attempt('draft_remove', {...starting(before), fileId});
    if (prior) await journalWrite(t, 'replace', prior, hold);
    else await journalWrite(t, 'capture', hold);
    const updated = wireDraft(await mutation(t, hold, draftPath(before.id) + '/files/' + fileId,
      {method: 'DELETE', headers: cap(writer), body: fence(before, writer)}), state.context, 'mutate');
    guard(t);
    const retained = before.files.filter(f => f.id !== fileId);
    check(updated.id === before.id && updated.writerGeneration === before.writerGeneration && updated.revision === before.revision + 1 &&
      updated.files.length === retained.length && updated.files.every((f, i) => f.id === retained[i].id));
    await settle(t, hold);
    guard(t); setDraft(updated);
  }

  async function discard(t) {
    ready({activeDraft: true}); freeDraft();
    const before = draft, key = uuid();
    const hold = attempt('draft_discard', {...starting(before), commandDigest: await sha(t, encoder.encode(key))});
    guard(t);
    await journalWrite(t, 'capture', hold);
    const value = await mutation(t, hold, draftPath(before.id), {method: 'DELETE', headers: {...cap(writer), 'Idempotency-Key': key},
      body: fence(before, writer)});
    guard(t);
    check(closed(value, ['id', 'state', 'writerGeneration', 'cleanupPending']) && value.id === before.id && value.state === 'discarded' &&
      value.writerGeneration === before.writerGeneration + 1 && typeof value.cleanupPending === 'boolean');
    await settle(t, hold);
    guard(t);
    unsavedEdits.delete(before.id);
    setDraft(null, null);
    state.savedDrafts = state.savedDrafts.filter(row => row.id !== before.id);
  }

  async function resume(t, draftId) {
    // A prior writer denial can leave the listing complete even while the active view is held.
    if (journalUnreadable || !lease) throw fault('journal_unavailable');
    if (!state.context || !state.features.drafts || !id(draftId) || !state.savedDrafts.some(d => d.id === draftId)) throw fault('invalid_request');
    const fresh = await list(t);
    guard(t);
    state.savedDrafts = fresh;
    const row = fresh.find(d => d.id === draftId);
    if (!row) throw fault('incomplete');
    const prev = allHolds().find(r => r.kind === 'draft_resume' && r.draftId === draftId);
    const starting = attempt('draft_resume', {draftId, step: 'recovering', writerGeneration: row.writerGeneration, revision: null});
    if (!prev) await journalWrite(t, 'capture', starting);
    else if (prev.step === 'recovering') await journalWrite(t, 'replace', prev, starting);
    const old = prev?.step === 'resuming' ? prev : starting;
    if (draft?.id === draftId) writer = null;
    emit(); guard(t);
    const grant = await wire(t, draftPath(draftId) + '/recover', {method: 'POST', body: {writerGeneration: row.writerGeneration}});
    guard(t);
    check(closed(grant, ['id', 'revision', 'writerGeneration', 'recoveryCapability']) && grant.id === draftId &&
      grant.writerGeneration === row.writerGeneration && int(grant.revision, row.revision) && secret(grant.recoveryCapability));
    const resuming = {...starting, step: 'resuming', writerGeneration: grant.writerGeneration, revision: grant.revision};
    await journalWrite(t, 'replace', old, resuming);
    guard(t);
    const value = wireDraft(await wire(t, draftPath(draftId) + '/resume', {method: 'POST',
      headers: {'X-Attachment-Draft-Capability': grant.recoveryCapability}, body: {revision: grant.revision, writerGeneration: grant.writerGeneration}}),
    state.context, 'resume');
    guard(t);
    check(value.id === draftId && value.revision === grant.revision + 1 && value.writerGeneration === grant.writerGeneration + 1);
    const nextWriter = credential(value), exact = await getDraft(t, nextWriter);
    guard(t);
    check(exact.revision >= value.revision);
    await settle(t, resuming);
    guard(t);
    setDraft(exact, nextWriter);
    state.draftRead = 'complete';
    await reconcile(t);
  }

  function localAction(action) {
    if (!active || !current(active)) return Promise.resolve(snapshot());
    try { action(); state.error = null; } catch (error) { state.error = notice(error.code); }
    emit();
    return Promise.resolve(snapshot());
  }
  function showDetails(handle) {
    const local = locals.find(row => row.detailsHandle === handle);
    const member = state.draft?.files.find(row => row.detailsHandle === handle);
    if (!local && !member) throw fault('invalid_request');
    const {name, mediaType, bytes} = local || member;
    state.detail = {owner: local ? 'selection' : 'draft', selectionId: local?.id ?? null,
      draftId: local ? (local.fileId === null ? null : local.draftId) : draft.id, fileId: local ? local.fileId : member.id,
      requestId: null, name, mediaType, bytes, savedCopyAvailable: false, kind: 'metadata_fallback', previewAvailable: false};
  }
  const unsupported = () => localAction(() => { throw fault('unavailable'); });
  return Object.freeze({
    snapshot,
    invalidate() { invalidate(); if (allowed && !disposed) emit(); },
    dispose() { disposed = true; invalidate(); },
    connect(value) {
      const key = currentKey();
      if (!key) { invalidate(); return Promise.resolve(snapshot()); }
      if (!closed(value, ['destinationId']) || !opaque(value.destinationId) || value.destinationId === 'officer') {
        invalidate(); state.error = notice('invalid_request'); emit(); return Promise.resolve(snapshot());
      }
      if (!active || active.key !== key || active.destinationId !== value.destinationId) {
        invalidate();
        active = {key, destinationId: value.destinationId, controller: new AbortController()};
      }
      return run(active, refreshScope);
    },
    refresh: () => run(active, refreshScope),
    createDraft: () => run(active, create),
    addFiles: files => run(active, t => add(t, files)),
    editText: value => run(active, t => edit(t, value)),
    discardUnsavedEdit(draftId) {
      return localAction(() => {
        const edit = unsavedEdits.get(draftId);
        if (draft?.id !== draftId || !edit) throw fault('invalid_request');
        unsavedEdits.delete(draftId);
        submitted.delete(edit.attemptId);
      });
    },
    removeFile: fileId => run(active, t => remove(t, fileId)),
    discardDraft: () => run(active, discard),
    resume: draftId => run(active, t => resume(t, draftId)),
    removeSelection(localId) {
      if (!active || !current(active) || operation) return Promise.resolve(snapshot());
      const row = locals.find(f => f.id === localId);
      if (row?.fileId !== null && row?.fileId !== undefined) return run(active, t => remove(t, row.fileId));
      return localAction(() => {
        if (!row || row.state !== 'failed' || allHolds().some(r => r.attemptId === row.attemptId || r.kind === 'draft_create')) {
          throw fault('journal_conflict');
        }
        locals = locals.filter(f => f !== row);
      });
    },
    openFileDetails: handle => retainedDetails.has(handle) ? run(active, t => retainedFileDetails(t, handle), false) :
      localAction(() => { detailGeneration += 1; showDetails(handle); }),
    closeFileDetails: () => localAction(() => { detailGeneration += 1; state.detail = null; }),
    send: value => staged || state.features.send ? run(active, t => sendRequest(t, value), false) : unsupported(),
    checkRequest: handle => staged || state.features.send ? run(active, t => checkRequest(t, handle), false) : unsupported(),
    filesPage: handle => staged ? run(active, t => filesPage(t, handle), false) : unsupported(),
    finishRequest: unsupported, cleanupSavedCopies: unsupported,
    prepareDeletion: handle => state.features.deletion ? run(active, t => prepareDeletion(t, handle), false) : unsupported(),
    openDeletion: handle => state.features.deletion ? run(active, t => openDeletion(t, handle), false) : unsupported(),
    confirmDeletion: handle => state.features.deletion ? run(active, t => confirmDeletion(t, handle), false) : unsupported(),
    executeDeletion: handle => state.features.deletion ? run(active, t => executeDeletion(t, handle), false) : unsupported(),
    closeDeletion: () => state.features.deletion ? localAction(beginDeletionView) : unsupported(),
  });
}

if (typeof module !== 'undefined' && module.exports) module.exports = {createNativeAgentAttachmentsClient};
else globalThis.createNativeAgentAttachmentsClient = createNativeAgentAttachmentsClient;
})();
