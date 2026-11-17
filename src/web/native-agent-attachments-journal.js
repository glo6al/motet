/* Scoped correlation for native attachment mutations. */
(function () {
'use strict';

const PREFIX = 'watchNativeAgentAttachments:v1:';
const MAX_RECORDS = 8;
const MAX_BYTES = 8192;
const BASE = ['version', 'attemptId', 'kind'];
const FENCE = ['draftId', 'revision', 'writerGeneration'];
const DELETION = ['fileId', 'operationId', 'previewGeneration', 'revision'];
const FIELDS = new Map([
  ['draft_create', ['commandDigest']],
  ['upload_reserve', [...FENCE, 'commandDigest']],
  ['upload_put', [...FENCE, 'uploadId', 'fileId']],
  ['draft_edit', FENCE],
  ['draft_remove', [...FENCE, 'fileId']],
  ['draft_discard', [...FENCE, 'commandDigest']],
  ['draft_resume', [...FENCE, 'step']],
  ['send_prepare', [...FENCE, 'commandDigest']],
  ['send_execute', [...FENCE, 'commandDigest', 'requestId', 'requestRevision']],
  ['deletion_prepare', ['fileId', 'managementGeneration', 'commandDigest']],
  ['deletion_confirm', [...DELETION, 'commandDigest']],
  ['deletion_execute', [...DELETION, 'mode']],
]);

function pattern(value, re) { return typeof value === 'string' && re.test(value); }
function id(value) { return pattern(value, /^[a-f0-9]{32}(?![\s\S])/); }
function digest(value) { return pattern(value, /^[a-f0-9]{64}(?![\s\S])/); }
function attemptId(value) { return pattern(value, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}(?![\s\S])/); }
function opaque(value) { return pattern(value, /^[A-Za-z0-9_.:-]{1,160}(?![\s\S])/); }
function positive(value) { return Number.isSafeInteger(value) && value > 0; }

function fault(code) {
  return Object.assign(new Error(code), {code});
}

function record(value) {
  const fields = FIELDS.get(value?.kind);
  if (!fields || !value || typeof value !== 'object' || Array.isArray(value)) throw fault('journal_invalid');
  const keys = [...BASE, ...fields];
  if (Reflect.ownKeys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key)) ||
      value.version !== 1 || !attemptId(value.attemptId)) throw fault('journal_invalid');
  if (fields.includes('commandDigest') && !digest(value.commandDigest)) throw fault('journal_invalid');
  if (fields.includes('draftId') && (!id(value.draftId) || !positive(value.writerGeneration))) throw fault('journal_invalid');
  if (fields.includes('fileId') && !id(value.fileId) || fields.includes('uploadId') && !id(value.uploadId)) throw fault('journal_invalid');
  if (value.kind === 'send_execute' && (!id(value.requestId) || value.requestRevision !== 1)) throw fault('journal_invalid');
  if (value.kind === 'deletion_prepare' && (!Number.isSafeInteger(value.managementGeneration) ||
      value.managementGeneration < 0 || value.managementGeneration >= Number.MAX_SAFE_INTEGER)) throw fault('journal_invalid');
  if (fields.includes('operationId') && (!id(value.operationId) || !positive(value.previewGeneration) ||
      value.revision < value.previewGeneration)) throw fault('journal_invalid');
  if (value.kind === 'deletion_execute' && (value.revision <= value.previewGeneration ||
      !['execute', 'recover'].includes(value.mode))) throw fault('journal_invalid');
  if (value.kind === 'draft_resume') {
    if (!(value.step === 'recovering' && value.revision === null || value.step === 'resuming' && positive(value.revision))) {
      throw fault('journal_invalid');
    }
  } else if (fields.includes('revision') && !positive(value.revision)) throw fault('journal_invalid');
  return Object.fromEntries(keys.map(key => [key, value[key]]));
}

function records(value) {
  if (!Array.isArray(value) || value.length > MAX_RECORDS) throw fault('journal_capacity');
  const rows = Array.from(value, record);
  const attempts = new Set(), commands = new Set(), slots = new Set();
  for (const row of rows) {
    const slot = row.kind.startsWith('deletion_') ? 'deletion:' + row.fileId :
      row.kind === 'draft_create' ? 'create' : `${row.kind === 'draft_resume' ? 'recovery' : 'content'}:${row.draftId}`;
    if (attempts.has(row.attemptId) || slots.has(slot) || commands.has(row.commandDigest)) throw fault('journal_conflict');
    attempts.add(row.attemptId);
    slots.add(slot);
    if (row.commandDigest) commands.add(row.commandDigest);
  }
  return rows;
}

function canReplace(from, to) {
  if (from.draftId !== to.draftId) return false;
  const sameAttempt = from.attemptId === to.attemptId;
  if (from.kind === 'deletion_confirm' && to.kind === 'deletion_execute') {
    return sameAttempt && from.fileId === to.fileId && from.operationId === to.operationId &&
      from.previewGeneration === to.previewGeneration && to.revision > from.revision && to.mode === 'execute';
  }
  if (from.kind === 'send_prepare' && to.kind === 'send_execute') {
    return sameAttempt && from.revision === to.revision && from.writerGeneration === to.writerGeneration &&
      from.commandDigest === to.commandDigest;
  }
  if (from.kind === 'upload_reserve' && to.kind === 'upload_put') {
    return sameAttempt && from.writerGeneration === to.writerGeneration && to.revision === from.revision + 1;
  }
  if (from.kind === 'upload_put' && to.kind === 'draft_remove') {
    return !sameAttempt && from.fileId === to.fileId && to.writerGeneration > from.writerGeneration && to.revision > from.revision;
  }
  if (from.kind !== 'draft_resume' || to.kind !== 'draft_resume' || to.writerGeneration < from.writerGeneration) return false;
  if (from.step === 'recovering') {
    return sameAttempt ? to.step === 'resuming' && to.writerGeneration === from.writerGeneration : to.step === 'recovering';
  }
  return !sameAttempt && to.step === 'resuming' &&
    (to.writerGeneration === from.writerGeneration ? to.revision >= from.revision : to.revision > from.revision);
}

function createNativeAgentAttachmentsJournal({storage, locks, crypto, events} = {}) {
  return Object.freeze({
    bind({ownerKey, destinationId, scopeId, isCurrent, changed} = {}) {
      let disposed = false;
      let name;
      const encoder = new TextEncoder();

      function guard() {
        if (disposed || typeof isCurrent !== 'function' || isCurrent() !== true) throw fault('journal_context_changed');
      }

      async function namespace() {
        guard();
        if (!name) {
          if (typeof ownerKey !== 'string' || !ownerKey || !opaque(destinationId) || !opaque(scopeId)) throw fault('journal_invalid');
          const bytes = encoder.encode(ownerKey);
          try {
            const hash = await crypto.subtle.digest('SHA-256', bytes);
            guard();
            const encoded = Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2, '0')).join('');
            if (!digest(encoded)) throw fault('journal_unavailable');
            name = PREFIX + encoded + ':' + encodeURIComponent(destinationId) + ':' + encodeURIComponent(scopeId);
          } finally {
            bytes.fill(0);
          }
        }
        return name;
      }

      function decode(raw) {
        if (raw === null) return [];
        if (typeof raw !== 'string' || raw.length > MAX_BYTES || encoder.encode(raw).length > MAX_BYTES) throw fault('journal_capacity');
        let value;
        try { value = JSON.parse(raw); } catch (_) { throw fault('journal_invalid'); }
        const rows = records(value);
        if (JSON.stringify(rows) !== raw) throw fault('journal_invalid');
        return rows;
      }

      async function locked(update) {
        try {
          const key = await namespace();
          guard();
          return await locks.request(key, {mode: 'exclusive'}, lock => {
            guard();
            if (!lock) throw fault('journal_unavailable');
            const prior = storage.getItem(key);
            const rows = decode(prior);
            if (!update) return rows;
            const after = records(update(rows));
            const encoded = JSON.stringify(after);
            if (encoder.encode(encoded).length > MAX_BYTES) throw fault('journal_capacity');
            guard();
            if (storage.getItem(key) !== prior) throw fault('journal_conflict');
            try {
              storage.setItem(key, encoded);
              if (storage.getItem(key) !== encoded) throw fault('journal_unavailable');
            } catch (_) {
              // Restore only a value this lock holder can still prove it wrote.
              try {
                guard();
                if (storage.getItem(key) === encoded) {
                  if (prior === null) storage.removeItem?.(key);
                  else storage.setItem(key, prior);
                }
              } catch (_) { /* The caller keeps its last verified hold. */ }
              throw fault('journal_unavailable');
            }
            guard();
            return after;
          });
        } catch (error) {
          if (typeof error?.code === 'string' && error.code.startsWith('journal_')) throw error;
          throw fault('journal_unavailable');
        }
      }

      function changeExact(expected, next) {
        const old = JSON.stringify(record(expected));
        return locked(rows => {
          const index = rows.findIndex(row => JSON.stringify(row) === old);
          if (index < 0) throw fault('journal_conflict');
          if (next === null) rows.splice(index, 1);
          else rows[index] = next;
          return rows;
        });
      }

      async function storageChanged(event) {
        if (!name || event.key !== null && event.key !== name || event.storageArea && event.storageArea !== storage) return;
        let rows, error;
        try { rows = await locked(); } catch (cause) { error = cause; }
        if (!disposed && typeof isCurrent === 'function' && isCurrent() === true && typeof changed === 'function') {
          try { changed(rows, error); } catch (_) { /* A listener cannot change persisted records. */ }
        }
      }

      events?.addEventListener?.('storage', storageChanged);
      return Object.freeze({
        read: () => locked(),
        async capture(value) {
          const row = record(value);
          if (row.kind === 'send_execute') throw fault('journal_conflict');
          return locked(rows => [...rows, row]);
        },
        async replace(expected, next) {
          const from = record(expected), to = record(next);
          if (!canReplace(from, to)) throw fault('journal_conflict');
          return changeExact(from, to);
        },
        async settle(expected) { return changeExact(expected, null); },
        dispose() {
          disposed = true;
          ownerKey = null;
          name = null;
          events?.removeEventListener?.('storage', storageChanged);
        },
      });
    },
  });
}

if (typeof module !== 'undefined' && module.exports) module.exports = {createNativeAgentAttachmentsJournal};
else globalThis.createNativeAgentAttachmentsJournal = createNativeAgentAttachmentsJournal;
})();
