/* Local browser receipt tests with fictional owner identities. */
'use strict';

const assert = require('node:assert/strict');
const {createHash, webcrypto} = require('node:crypto');
const fs = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const {createOfficerDocumentPending} = require('../src/web/officer-document-pending.js');

const OWNER = 'fictional-pending-owner';
const OTHER_OWNER = 'other-fictional-pending-owner';
const ORIGIN = 'a'.repeat(32);
const DRAFT = 'b'.repeat(32);
const SOURCE = 'c'.repeat(32);
const REQUEST = 'd'.repeat(32);
const STATION = 'station:x:fedcba98';
const CAPTURED = 'captured';
const PREPARING = 'prepare_may_have_started';
const EXECUTING = 'execute_may_have_started';

function hash(text) {
  return createHash('sha256').update(text).digest('hex');
}

function payload(n = 1, changes = {}) {
  return {
    id: `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
    conversationId: ORIGIN,
    draftId: DRAFT,
    question: 'Fictional submitted text.',
    files: [{id: SOURCE, name: 'fictional-source.pdf'}],
    prepareDigest: hash(`fictional-prepare-key-${n}`),
    submittedAt: 1794747600.125,
    ...changes
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(callback => { resolve = callback; });
  return {promise, resolve};
}

function origin() {
  const state = new Map(), tails = new Map(), listeners = new Set();
  const faults = {read: false, quota: false, discardWrite: false, badReadback: false};
  let readback = false;
  return {
    state, faults, listeners,
    storage: {
      getItem(key) {
        if (faults.read) throw new Error('Fictional storage denied');
        if (readback) { readback = false; return 'Fictional failed readback'; }
        return state.get(key) ?? null;
      },
      setItem(key, value) {
        if (faults.quota) throw new Error('Fictional storage quota');
        if (faults.discardWrite) return;
        state.set(key, value);
        if (faults.badReadback) { faults.badReadback = false; readback = true; }
      },
      removeItem(key) {
        state.delete(key);
        if (faults.badReadback) { faults.badReadback = false; readback = true; }
      }
    },
    locks: {
      request(key, options, callback) {
        assert.equal(options.mode, 'exclusive');
        const result = (tails.get(key) || Promise.resolve()).then(() => callback({name: key}));
        tails.set(key, result.catch(() => {}));
        return result;
      }
    },
    events: {
      addEventListener(type, callback) { assert.equal(type, 'storage'); listeners.add(callback); },
      removeEventListener(type, callback) { assert.equal(type, 'storage'); listeners.delete(callback); }
    }
  };
}

function tab(shared, options = {}) {
  const selected = {scopeId: 'room', key: OWNER, generation: 0};
  const emissions = [];
  const journal = createOfficerDocumentPending({
    storage: shared.storage,
    locks: shared.locks,
    events: shared.events,
    crypto: webcrypto,
    current: () => selected.key ? selected : null,
    changed: next => { emissions.push(next); if (options.changed) options.changed(next); },
    ...options.dependencies
  });
  return {journal, selected, emissions};
}

function capture(journal, value = payload(), fileLimit = 5) {
  return journal.capture(value, {fileLimit});
}

const code = name => error => error.code === name;

test('import serves a factory without reading host storage or installing listeners', () => {
  const path = require.resolve('../src/web/officer-document-pending.js');
  const fail = () => assert.fail('Module import opened browser state');
  const realm = {addEventListener: fail};
  Object.defineProperty(realm, 'localStorage', {get: fail});
  vm.runInNewContext(fs.readFileSync(path, 'utf8'), realm);
  assert.equal(typeof realm.createOfficerDocumentPending, 'function');
});

test('receipt survives reload and preserves phase, exact IDs, names and submitted time', async () => {
  const shared = origin(), first = tab(shared), original = payload();
  assert.equal(first.journal.snapshot().status, 'inactive');
  await first.journal.sync();
  const receipt = await capture(first.journal, original);
  assert.equal(receipt.ownerDigest, hash(OWNER));
  assert.equal(receipt.phase, CAPTURED);
  assert.equal(receipt.submittedAt, original.submittedAt);
  assert.equal(receipt.route, 'files');
  original.files[0].name = 'Unrelated future edit';
  assert.equal(receipt.files[0].name, 'fictional-source.pdf');
  assert.throws(() => { receipt.files[0].name = 'Cannot change'; }, TypeError);
  first.selected.generation++;
  await first.journal.markPrepare(receipt.id, ORIGIN);
  await first.journal.recordPrepared(receipt.id, REQUEST);
  const issued = await first.journal.markExecute(receipt.id, REQUEST);
  assert.equal(issued.phase, EXECUTING);
  first.journal.dispose();
  assert.equal(shared.listeners.size, 0);
  const reload = tab(shared);
  assert.deepEqual((await reload.journal.sync()).receipt, issued);
  const stored = [...shared.state.values()].join('');
  assert(!stored.includes(OWNER));
  assert(!stored.includes('fictional-prepare-key-1'));
  assert(!/capability|fingerprint|writerNonce|bytes|verified|delivery/i.test(stored));
  assert.equal(await reload.journal.settle(issued.id, {phase: EXECUTING, requestId: REQUEST}), null);
  assert.equal(shared.state.size, 0);
});

test('two simultaneous tabs cannot both capture one scope or overwrite other scopes', async () => {
  const shared = origin(), a = tab(shared), b = tab(shared), c = tab(shared);
  c.selected.scopeId = STATION;
  await Promise.all([a.journal.sync(), b.journal.sync(), c.journal.sync()]);
  const outcomes = await Promise.allSettled([capture(a.journal), capture(b.journal, payload(2)), capture(c.journal, payload(3))]);
  assert.equal(outcomes[0].status, 'fulfilled');
  assert.equal(outcomes[1].reason.code, 'pending_conflict');
  assert.equal(outcomes[2].status, 'fulfilled');
  assert.equal((await b.journal.sync()).receipt.id, payload().id);
  assert.equal((await c.journal.sync()).receipt.id, payload(3).id);
  assert.equal(JSON.parse([...shared.state.values()][0]).records.length, 2);
});

test('stale updates or settlement cannot skip phases, replace exact IDs or erase a newer record', async () => {
  const shared = origin(), a = tab(shared), b = tab(shared);
  await Promise.all([a.journal.sync(), b.journal.sync()]);
  const {id} = await capture(a.journal);
  await assert.rejects(b.journal.markExecute(id, REQUEST), code('pending_conflict'));
  await assert.rejects(b.journal.markPrepare(id, 'e'.repeat(32)), code('pending_conflict'));
  await a.journal.markPrepare(id, ORIGIN);
  await assert.rejects(a.journal.markPrepare(id, ORIGIN), code('pending_conflict'));
  await assert.rejects(b.journal.markExecute(id, REQUEST), code('pending_conflict'));
  await a.journal.recordPrepared(id, REQUEST);
  await assert.rejects(b.journal.recordPrepared(id, 'e'.repeat(32)), code('pending_conflict'));
  await a.journal.markExecute(id, REQUEST);
  await assert.rejects(a.journal.markExecute(id, REQUEST), code('pending_conflict'));
  await assert.rejects(b.journal.settle(id, {phase: PREPARING, requestId: REQUEST}), code('pending_conflict'));
  await a.journal.settle(id, {phase: EXECUTING, requestId: REQUEST});
  const newer = await capture(b.journal, payload(2));
  await assert.rejects(a.journal.settle(id, {phase: EXECUTING, requestId: REQUEST}), code('pending_conflict'));
  assert.equal((await a.journal.sync()).receipt.id, newer.id);
});

test('owner and scope changes hide prior content immediately and protect a waiting capture', async () => {
  const shared = origin(), a = tab(shared);
  await a.journal.sync();
  await capture(a.journal);
  a.selected.key = '';
  assert.deepEqual(a.journal.snapshot(), {status: 'inactive', receipt: null, error: null});
  a.selected.key = OTHER_OWNER;
  assert.equal((await a.journal.sync()).receipt, null);
  await capture(a.journal, payload(2));
  a.selected.key = OWNER;
  a.selected.scopeId = STATION;
  assert.equal(a.journal.snapshot().receipt, null);
  assert.equal((await a.journal.sync()).receipt, null);

  const entered = deferred(), release = deferred();
  const hold = shared.locks.request([...shared.state.keys()][0], {mode: 'exclusive'}, async () => {
    entered.resolve();
    await release.promise;
  });
  await entered.promise;
  const waiting = capture(a.journal, payload(3));
  a.selected.generation++;
  release.resolve();
  await hold;
  await assert.rejects(waiting, code('pending_context_changed'));
  assert.equal((await a.journal.sync()).receipt, null);

  a.selected.scopeId = 'room';
  assert.equal(a.journal.snapshot().receipt, null);
  assert.equal((await a.journal.sync()).receipt.id, payload().id);
  a.journal.invalidate();
  assert.equal(a.journal.snapshot().receipt, null);
  await assert.rejects(a.journal.markPrepare(payload().id, ORIGIN), code('pending_context_changed'));
});

test('storage events reread current data and ignore forged payloads and unrelated keys', async () => {
  const shared = origin(), writer = tab(shared), refreshed = deferred();
  const reader = tab(shared, {changed: next => { if (next.status === 'ready' && next.receipt) refreshed.resolve(next); }});
  await writer.journal.sync();
  await reader.journal.sync();
  const written = await capture(writer.journal);
  const callback = [...shared.listeners][1];
  const before = reader.emissions.length;
  callback({key: 'liveRoomShift', newValue: 'unrelated'});
  assert.equal(reader.emissions.length, before);
  callback({key: [...shared.state.keys()][0], newValue: JSON.stringify(payload(7))});
  assert.equal(reader.journal.snapshot().status, 'checking');
  assert.deepEqual((await refreshed.promise).receipt, written);
  reader.journal.dispose();
  assert.equal(shared.listeners.has(callback), false);
});

test('an owner change during digest prevents an old storage read or publication', async () => {
  const shared = origin(), started = deferred(), proceed = deferred();
  let reads = 0;
  const storage = {...shared.storage, getItem(key) { reads++; return shared.storage.getItem(key); }};
  const crypto = {subtle: {async digest(algorithm, bytes) {
    started.resolve();
    await proceed.promise;
    return webcrypto.subtle.digest(algorithm, bytes);
  }}};
  const a = tab(shared, {dependencies: {storage, crypto}});
  const loading = a.journal.sync();
  await started.promise;
  a.selected.key = OTHER_OWNER;
  assert.equal(a.journal.snapshot().status, 'inactive');
  proceed.resolve();
  assert.equal((await loading).receipt, null);
  assert.equal(reads, 0);
  assert.equal((await a.journal.sync()).status, 'ready');
  assert.equal(reads, 1);
});

test('storage denial, missing locks, quota and a silently dropped write produce no capture', async () => {
  const shared = origin();
  const unsupported = tab(shared, {dependencies: {locks: null}}).journal;
  assert.equal((await unsupported.sync()).error, 'pending_storage_unavailable');
  await assert.rejects(capture(unsupported), code('pending_storage_unavailable'));
  const {journal} = tab(shared);
  shared.faults.read = true;
  assert.equal((await journal.sync()).error, 'pending_storage_unavailable');
  shared.faults.read = false;
  await journal.sync();
  shared.faults.quota = true;
  await assert.rejects(capture(journal), code('pending_storage_write_failed'));
  assert.equal(journal.snapshot().receipt, null);
  assert.equal(shared.state.size, 0);
  shared.faults.quota = false;
  await journal.sync();
  shared.faults.discardWrite = true;
  await assert.rejects(capture(journal), code('pending_storage_write_failed'));
  assert.equal(shared.state.size, 0);
  shared.faults.discardWrite = false;
  assert.equal((await journal.sync()).status, 'ready');
  assert.equal((await capture(journal)).phase, CAPTURED);
});

test('a failed write/readback leaves the prior phase and cannot erase an issued receipt', async () => {
  const shared = origin(), {journal} = tab(shared);
  await journal.sync();
  const {id} = await capture(journal);
  const [key, raw] = [...shared.state.entries()][0];
  shared.faults.badReadback = true;
  await assert.rejects(journal.markPrepare(id, ORIGIN), code('pending_storage_write_failed'));
  assert.equal(shared.state.get(key), raw);
  assert.equal(journal.snapshot().receipt.phase, CAPTURED);
  await journal.sync();
  await journal.markPrepare(id, ORIGIN);
  await journal.recordPrepared(id, REQUEST);
  shared.faults.quota = true;
  await assert.rejects(journal.markExecute(id, REQUEST), code('pending_storage_write_failed'));
  assert.equal(journal.snapshot().receipt.phase, PREPARING);
  await assert.rejects(journal.settle(id, {phase: PREPARING, requestId: REQUEST}), code('pending_storage_write_failed'));
  shared.faults.quota = false;
  await journal.sync();
  const issued = await journal.markExecute(id, REQUEST);
  shared.faults.badReadback = true;
  await assert.rejects(journal.settle(id, {phase: EXECUTING, requestId: REQUEST}), code('pending_storage_write_failed'));
  assert.deepEqual((await journal.sync()).receipt, issued);
});

test('invalid or corrupt stored receipts fail closed without rewriting them', async () => {
  const shared = origin(), {journal} = tab(shared);
  await journal.sync();
  await capture(journal);
  const [key, legitimate] = [...shared.state.entries()][0];
  const cases = [
    raw => raw.replace('"schemaVersion":1', '"schemaVersion":7,"schemaVersion":1'),
    raw => raw.replace('fictional-source.pdf', '\\ud800'),
    raw => raw.replace('"scopeId":"room"', '"scopeId":"room\\n"'),
    raw => raw.replace('"submittedAt":1794747600.125', '"submittedAt":-10'),
    raw => raw.replace('"route":"files"', '"route":"ordinary"'),
    raw => raw.replace('"question":', '"requestCapability":"fictional", "question":'),
    raw => raw.replace(hash(OWNER), hash(OTHER_OWNER)),
    raw => raw.replace('"phase":"captured"', '"phase":"execute_may_have_started"'),
    raw => raw.replace(`"requestId":null`, `"requestId":"${REQUEST}"`),
    () => 'x'.repeat(320 * 1024 + 1)
  ];
  for (const corrupt of cases) {
    const raw = corrupt(legitimate);
    shared.state.set(key, raw);
    const reloaded = tab(shared).journal;
    assert.deepEqual(await reloaded.sync(), {status: 'unavailable', receipt: null, error: 'pending_storage_invalid'});
    await assert.rejects(capture(reloaded, payload(2)), code('pending_storage_invalid'));
    assert.equal(shared.state.get(key), raw);
    reloaded.dispose();
  }
});

test('captures accept bounded Unicode and reject excess, unsafe or undeclared fields', async () => {
  const shared = origin(), {journal} = tab(shared);
  await journal.sync();
  const valid = payload(1, {question: '🦉'.repeat(2000), files: [{id: SOURCE, name: '譜'.repeat(251) + '.pdf'}]});
  const admitted = await capture(journal, valid, 1);
  assert.equal(admitted.question, valid.question);
  assert.equal((await tab(shared).journal.sync()).receipt.files[0].name, valid.files[0].name);
  await journal.settle(admitted.id, {phase: CAPTURED, requestId: null});
  const extraSource = {id: 'e'.repeat(32), name: 'fictional-two.png'};
  for (const value of [payload(2, {question: '🦉'.repeat(2000) + '!'}), payload(2, {question: '\u0000'}),
    payload(2, {question: '\udc00'}), payload(2, {requestCapability: 'fictional'}),
    payload(2, {rawKey: 'fictional-prepare-key-2'}), payload(2, {files: [{id: SOURCE, name: '/fictional.pdf'}]}),
    payload(2, {files: [{id: SOURCE, name: 'fictional.pdf', bytes: 1}]}),
    payload(2, {files: new Array(1)}), payload(2, {submittedAt: Infinity}),
    payload(2, {draftId: null, question: " "}), payload(2, {files: [...payload().files, extraSource]})]) {
    await assert.rejects(capture(journal, value, 1), code('pending_invalid'));
  }
  assert.equal(shared.state.size, 0);
  const followUp = await capture(journal, payload(3, {draftId: null, conversationId: null}));
  assert.equal(followUp.requestId, null);
  assert.equal((await journal.markPrepare(followUp.id, ORIGIN)).conversationId, ORIGIN);
});

test('eight owner scopes are bounded without eviction or age-based clearing', async () => {
  const shared = origin(), a = tab(shared);
  for (let i = 1; i <= 8; i++) {
    a.selected.scopeId = `station:x:0000000${i}`;
    await a.journal.sync();
    await capture(a.journal, payload(i, {submittedAt: 0}));
  }
  a.selected.scopeId = 'room';
  await a.journal.sync();
  await assert.rejects(capture(a.journal, payload(9)), code('pending_capacity'));
  assert.equal(JSON.parse([...shared.state.values()][0]).records.length, 8);
  a.selected.scopeId = 'station:x:00000001';
  assert.equal((await a.journal.sync()).receipt.id, payload().id);
});
