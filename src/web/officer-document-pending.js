/* Local presentation receipts for submitted Officer file questions. */
(function () {
'use strict';

const PREFIX = 'watchOfficerPending:v1:';
const MAX_SCOPES = 8;
const MAX_RECEIPT_BYTES = 40 * 1024;
const MAX_OWNER_BYTES = MAX_SCOPES * MAX_RECEIPT_BYTES;
const CAPTURED = 'captured';
const PREPARING = 'prepare_may_have_started';
const EXECUTING = 'execute_may_have_started';
const INPUT_FIELDS = ['id', 'conversationId', 'draftId', 'question', 'files', 'prepareDigest', 'submittedAt'];
const RECORD_FIELDS = [...INPUT_FIELDS, 'schemaVersion', 'ownerDigest', 'scopeId', 'route', 'phase', 'requestId'];

function closed(value, fields) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Reflect.ownKeys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
}

function digest(value) {
  return typeof value === 'string' && value.length === 64 && /^[a-f0-9]{64}$/.test(value);
}

function documentId(value) {
  return typeof value === 'string' && value.length === 32 && /^[a-f0-9]{32}$/.test(value);
}

function attemptId(value) {
  return typeof value === 'string' && value.length === 36 &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(value);
}

function scopeId(value) {
  return typeof value === 'string' && /^(?:room|station:[cxkea]:[a-f0-9-]{8,64})(?![\s\S])/.test(value);
}

function boundedText(value, length) {
  return typeof value === 'string' && value.length <= length && !/[\u0000\uD800-\uDFFF]/u.test(value);
}

function validInput(value, fileLimit) {
  return attemptId(value.id) && digest(value.prepareDigest) &&
    (value.conversationId === null || documentId(value.conversationId)) &&
    (value.draftId === null || documentId(value.draftId)) && boundedText(value.question, 4000) &&
    Number.isFinite(value.submittedAt) && value.submittedAt >= 0 &&
    Array.isArray(value.files) && value.files.length <= fileLimit &&
    Array.from(value.files).every(file => closed(file, ['id', 'name']) && documentId(file.id) &&
      boundedText(file.name, 255) && file.name.trim() && !/[\\/\p{C}]/u.test(file.name) &&
      file.name !== '.' && file.name !== '..') &&
    new Set(value.files.map(file => file.id)).size === value.files.length &&
    (value.draftId !== null ? value.files.length > 0 : Boolean(value.question.trim()));
}

function validRecord(value, ownerDigest) {
  return closed(value, RECORD_FIELDS) && value.schemaVersion === 1 && value.ownerDigest === ownerDigest &&
    scopeId(value.scopeId) && value.route === 'files' && validInput(value, 5) &&
    (value.requestId === null || documentId(value.requestId)) &&
    ((value.phase === CAPTURED && value.requestId === null) ||
     (value.phase === PREPARING && value.conversationId !== null) ||
     (value.phase === EXECUTING && value.conversationId !== null && value.requestId !== null));
}

function frozenRecord(value) {
  return Object.freeze({...value, files: Object.freeze(value.files.map(file => Object.freeze({...file})))});
}

function pendingError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function view(status, receipt = null, error = null) {
  return Object.freeze({status, receipt, error});
}

function createOfficerDocumentPending(options) {
  const o = options || {};
  const encoder = new TextEncoder();
  let binding = null;
  let state = view('inactive');
  let disposed = false;

  function current() {
    const value = typeof o.current === 'function' ? o.current() : null;
    if (disposed || !value || typeof value.key !== 'string' || !value.key ||
        !scopeId(value.scopeId) || !Number.isSafeInteger(value.generation) || value.generation < 0) return null;
    return {key: value.key, scopeId: value.scopeId, generation: value.generation};
  }

  function matches(context, token) {
    return context && token && context.key === token.key && context.scopeId === token.scopeId;
  }

  function snapshot() {
    if (matches(current(), binding)) return state;
    binding = null;
    state = view('inactive');
    return state;
  }

  function changed() {
    if (typeof o.changed === 'function') {
      try { o.changed(snapshot()); } catch (_) { /* Presentation cannot alter a receipt. */ }
    }
  }

  function check(token, generation) {
    const context = current();
    if (token !== binding || !matches(context, token) ||
        (generation !== undefined && context.generation !== generation)) {
      throw pendingError('pending_context_changed');
    }
  }

  function invalidate() {
    binding = null;
    state = view('inactive');
    changed();
  }

  async function namespace(token) {
    if (token.ownerDigest) return token.ownerDigest;
    const bytes = encoder.encode(token.key);
    try {
      const hash = await o.crypto.subtle.digest('SHA-256', bytes);
      check(token);
      const ownerDigest = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
      if (!digest(ownerDigest)) throw pendingError('pending_storage_unavailable');
      token.ownerDigest = ownerDigest;
      return ownerDigest;
    } finally {
      bytes.fill(0);
    }
  }

  function read(key, ownerDigest) {
    let raw;
    try { raw = o.storage.getItem(key); }
    catch (_) { throw pendingError('pending_storage_unavailable'); }
    if (raw === null) return {raw, records: []};
    if (typeof raw !== 'string' || raw.length > MAX_OWNER_BYTES || encoder.encode(raw).length > MAX_OWNER_BYTES) {
      throw pendingError('pending_storage_invalid');
    }
    let data;
    try { data = JSON.parse(raw); }
    catch (_) { throw pendingError('pending_storage_invalid'); }
    if (!closed(data, ['schemaVersion', 'records']) || data.schemaVersion !== 1 ||
        !Array.isArray(data.records) || data.records.length > MAX_SCOPES || JSON.stringify(data) !== raw ||
        !data.records.every(record => validRecord(record, ownerDigest) &&
          encoder.encode(JSON.stringify(record)).length <= MAX_RECEIPT_BYTES) ||
        ['scopeId', 'id', 'prepareDigest'].some(field => new Set(data.records.map(record => record[field])).size !== data.records.length)) {
      throw pendingError('pending_storage_invalid');
    }
    return {raw, records: data.records};
  }

  function write(key, raw, records) {
    const value = records.length ? JSON.stringify({schemaVersion: 1, records}) : null;
    if (value !== null && encoder.encode(value).length > MAX_OWNER_BYTES) throw pendingError('pending_capacity');
    try {
      if (value === null) o.storage.removeItem(key);
      else o.storage.setItem(key, value);
      if (o.storage.getItem(key) !== value) throw pendingError('pending_storage_write_failed');
    } catch (_) {
      // Storage writes are synchronous. Restore the previous value if a write
      // returned without the expected readback. No transport can follow a failure.
      try {
        if (o.storage.getItem(key) !== raw) {
          if (raw === null) o.storage.removeItem(key);
          else o.storage.setItem(key, raw);
        }
      } catch (_) { /* The next verified read must establish storage again. */ }
      throw pendingError('pending_storage_write_failed');
    }
  }

  function storageReady() {
    if (!o.storage || typeof o.storage.getItem !== 'function' || typeof o.storage.setItem !== 'function' ||
        typeof o.storage.removeItem !== 'function' || !o.locks || typeof o.locks.request !== 'function' ||
        !o.crypto?.subtle || typeof o.crypto.subtle.digest !== 'function') {
      throw pendingError('pending_storage_unavailable');
    }
  }

  async function locked(token, generation, update) {
    try {
      storageReady();
      const ownerDigest = await namespace(token);
      check(token, generation);
      const key = PREFIX + ownerDigest;
      const result = await o.locks.request(key, {mode: 'exclusive'}, lock => {
        check(token, generation);
        if (!lock) throw pendingError('pending_storage_unavailable');
        const {raw, records} = read(key, ownerDigest);
        check(token, generation);
        const index = records.findIndex(record => record.scopeId === token.scopeId);
        const existing = index < 0 ? null : records[index];
        state = view('ready', existing && frozenRecord(existing));
        if (!update) return state.receipt;
        const next = update(existing, ownerDigest, records);
        if (next === existing) return state.receipt;
        if (next && (!validRecord(next, ownerDigest) || encoder.encode(JSON.stringify(next)).length > MAX_RECEIPT_BYTES)) {
          throw pendingError('pending_invalid');
        }
        if (index < 0) records.push(next);
        else if (next === null) records.splice(index, 1);
        else records[index] = next;
        check(token, generation);
        write(key, raw, records);
        state = view('ready', next && frozenRecord(next));
        return state.receipt;
      });
      check(token, generation);
      changed();
      check(token, generation);
      return result;
    } catch (error) {
      const code = typeof error?.code === 'string' && error.code.startsWith('pending_') ? error.code : 'pending_storage_unavailable';
      if (token === binding && matches(current(), token)) {
        if (code.startsWith('pending_storage_')) state = view('unavailable', state.receipt, code);
        changed();
      }
      throw pendingError(code);
    }
  }

  async function sync() {
    const context = current();
    if (!context) {
      if (binding) invalidate();
      return snapshot();
    }
    if (!matches(context, binding)) binding = {key: context.key, scopeId: context.scopeId, ownerDigest: null};
    const token = binding;
    const receipt = state.receipt?.scopeId === token.scopeId && state.receipt?.ownerDigest === token.ownerDigest ? state.receipt : null;
    state = view('checking', receipt);
    changed();
    try { await locked(token); } catch (_) { /* snapshot reports availability */ }
    return snapshot();
  }

  function mutate(update, capture = false) {
    const context = current();
    if (!matches(context, binding)) return Promise.reject(pendingError('pending_context_changed'));
    if (state.status !== 'ready' && (capture || state.status !== 'checking' || !binding.ownerDigest)) {
      return Promise.reject(pendingError(state.error || 'pending_storage_unavailable'));
    }
    return locked(binding, capture ? context.generation : undefined, update);
  }

  function updateExact(id, update) {
    if (!attemptId(id)) return Promise.reject(pendingError('pending_invalid'));
    return mutate(existing => {
      if (!existing || existing.id !== id) throw pendingError('pending_conflict');
      return update(existing);
    });
  }

  function storageChanged(event) {
    if (binding?.ownerDigest && (event.key === null || event.key === PREFIX + binding.ownerDigest)) void sync();
  }

  o.events?.addEventListener?.('storage', storageChanged);
  return Object.freeze({
    snapshot, sync, invalidate,
    dispose() {
      disposed = true;
      o.events?.removeEventListener?.('storage', storageChanged);
      invalidate();
    },
    capture(value, {fileLimit} = {}) {
      if (!Number.isInteger(fileLimit) || fileLimit < 1 || fileLimit > 5 ||
          !closed(value, INPUT_FIELDS) || !validInput(value, fileLimit)) {
        return Promise.reject(pendingError('pending_invalid'));
      }
      const input = frozenRecord(value);
      return mutate((existing, ownerDigest, records) => {
        if (existing || records.some(record => record.id === input.id || record.prepareDigest === input.prepareDigest)) {
          throw pendingError('pending_conflict');
        }
        if (records.length >= MAX_SCOPES) throw pendingError('pending_capacity');
        return {...input, schemaVersion: 1, ownerDigest, scopeId: binding.scopeId, route: 'files', phase: CAPTURED, requestId: null};
      }, true);
    },
    markPrepare(id, conversationId) {
      if (!documentId(conversationId)) return Promise.reject(pendingError('pending_invalid'));
      return updateExact(id, record => {
        if (record.phase !== CAPTURED || record.conversationId !== null && record.conversationId !== conversationId) {
          throw pendingError('pending_conflict');
        }
        return {...record, phase: PREPARING, conversationId};
      });
    },
    recordPrepared(id, requestId) {
      if (!documentId(requestId)) return Promise.reject(pendingError('pending_invalid'));
      return updateExact(id, record => {
        if (record.phase === CAPTURED || record.requestId !== null && record.requestId !== requestId) {
          throw pendingError('pending_conflict');
        }
        return record.requestId === requestId ? record : {...record, requestId};
      });
    },
    markExecute(id, requestId) {
      if (!documentId(requestId)) return Promise.reject(pendingError('pending_invalid'));
      return updateExact(id, record => {
        if (record.phase !== PREPARING || record.requestId !== requestId) throw pendingError('pending_conflict');
        return {...record, phase: EXECUTING};
      });
    },
    settle(id, proof) {
      if (!closed(proof, ['phase', 'requestId']) || ![CAPTURED, PREPARING, EXECUTING].includes(proof.phase) ||
          !(proof.requestId === null || documentId(proof.requestId))) {
        return Promise.reject(pendingError('pending_invalid'));
      }
      const {phase, requestId} = proof;
      return updateExact(id, record => {
        if (record.phase !== phase || record.requestId !== requestId) throw pendingError('pending_conflict');
        return null;
      });
    }
  });
}

if (typeof module !== 'undefined' && module.exports) module.exports = {createOfficerDocumentPending};
else globalThis.createOfficerDocumentPending = createOfficerDocumentPending;
})();
