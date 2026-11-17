'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const {createNativeAgentAttachmentsJournal} = require('../src/web/native-agent-attachments-journal.js');

const DRAFT = 'a'.repeat(32), FILE = 'b'.repeat(32), UPLOAD = 'c'.repeat(32);
const base = (kind, fields = {}) => ({version: 1, attemptId: webcrypto.randomUUID(), kind, ...fields});
const start = {draftId: DRAFT, revision: 3, writerGeneration: 1};
const create = () => base('draft_create', {commandDigest: 'e'.repeat(64)});
const reserve = () => base('upload_reserve', {...start, commandDigest: 'd'.repeat(64)});
function setup() {
  const map = new Map(), queue = new Map(), events = new EventTarget();
  const storage = {getItem: k => map.get(k) ?? null, setItem: (k, v) => map.set(k, v), removeItem: k => map.delete(k)};
  const locks = {request(name, {mode}, callback) {
    assert.equal(mode, 'exclusive');
    const task = (queue.get(name) ?? Promise.resolve()).then(() => callback({name}));
    queue.set(name, task.catch(() => {}));
    return task;
  }};
  const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto, events});
  const bind = (overrides = {}) => journal.bind({ownerKey: 'fictional-owner', destinationId: 'fictional-ada',
    scopeId: 'fictional:orchard', isCurrent: () => true, ...overrides});
  return {map, journal, storage, locks, events, bind};
}

test('construction is inert, scope and owner leases only read their own closed records', async () => {
  const {bind, map} = setup();
  const a = bind();
  assert.equal(map.size, 0);
  const record = create();
  await a.capture(record);
  const [key] = map.keys();
  assert.match(key, /^watchNativeAgentAttachments:v1:[0-9a-f]{64}:fictional-ada:fictional%3Aorchard$/);
  assert(!key.includes('fictional-owner'));
  assert.deepEqual(await bind().read(), [record]);
  assert.deepEqual(await bind({ownerKey: 'fictional-owner-two'}).read(), []);
  assert.deepEqual(await bind({scopeId: 'fictional:grove'}).read(), []);
  assert.deepEqual(await bind({destinationId: 'fictional-bram'}).read(), []);
  assert.deepEqual(JSON.parse(map.get(key)), [record]);
  a.dispose();
  await assert.rejects(a.read(), {code: 'journal_context_changed'});
});

test('contending tabs cannot both capture the same content slot', async () => {
  const {bind} = setup();
  const a = bind(), b = bind();
  const r1 = reserve(), r2 = base('draft_edit', start);
  const results = await Promise.allSettled([a.capture(r1), b.capture(r2)]);
  assert.equal(results.filter(v => v.status === 'fulfilled').length, 1);
  assert.equal(results.find(v => v.status === 'rejected').reason.code, 'journal_conflict');
  assert.equal((await a.read()).length, 1);
});

test('full-record CAS prevents late PUT from erasing or replacing the newer Remove', async () => {
  const {bind} = setup();
  const a = bind(), b = bind();
  const reserved = reserve();
  const put = {...base('upload_put', {...start, revision: 4, uploadId: UPLOAD, fileId: FILE}), attemptId: reserved.attemptId};
  const remove = base('draft_remove', {...start, revision: 5, writerGeneration: 2, fileId: FILE});
  await a.capture(reserved);
  await assert.rejects(a.replace(reserved, {...put, revision: 5}), {code: 'journal_conflict'});
  await a.replace(reserved, put);
  await assert.rejects(b.replace(put, {...remove, writerGeneration: 1}), {code: 'journal_conflict'});
  await assert.rejects(b.replace({...put, revision: 3}, remove), {code: 'journal_conflict'});
  await b.replace(put, remove);
  await assert.rejects(a.settle(put), {code: 'journal_conflict'});
  await assert.rejects(a.replace(put, {...remove, attemptId: webcrypto.randomUUID()}), {code: 'journal_conflict'});
  assert.deepEqual(await b.read(), [remove]);
});

test('recovering and resuming are separate bounded authority phases', async () => {
  const {bind} = setup();
  const lease = bind();
  const edit = base('draft_edit', start);
  const recovering = base('draft_resume', {...start, revision: null, step: 'recovering'});
  await lease.capture(edit);
  await lease.capture(recovering);
  const replacement = {...recovering, attemptId: webcrypto.randomUUID()};
  await lease.replace(recovering, replacement);
  const resume = {...replacement, revision: 3, step: 'resuming'};
  await assert.rejects(lease.replace(recovering, {...resume, attemptId: recovering.attemptId}), {code: 'journal_conflict'});
  await lease.replace(replacement, resume);
  await assert.rejects(lease.replace(resume, replacement), {code: 'journal_conflict'});
  const next = {...resume, attemptId: webcrypto.randomUUID(), revision: 4, writerGeneration: 2};
  await lease.replace(resume, next);
  await assert.rejects(lease.settle(resume), {code: 'journal_conflict'});
  assert.deepEqual(await lease.read(), [edit, next]);
});

test('quota, unexpected fields and unverifiable readback fail closed', async () => {
  const {bind, map, storage} = setup();
  const a = bind();
  for (const key of ['capability', 'writerNonce', 'name', 'text', 'bytes', 'prepareKey', 'unknown']) {
    await assert.rejects(a.capture({...create(), [key]: 'fictional-private'}), {code: 'journal_invalid'});
  }
  await assert.rejects(a.capture({...create(), commandDigest: 'a'.repeat(64) + '\n'}), {code: 'journal_invalid'});
  assert.equal(map.size, 0);
  const set = storage.setItem;
  storage.setItem = () => { throw new Error('fictional quota'); };
  await assert.rejects(a.capture(create()), {code: 'journal_unavailable'});
  storage.setItem = () => {};
  await assert.rejects(a.capture(create()), {code: 'journal_unavailable'});
  assert.equal(map.size, 0);
  storage.setItem = set;
  const row = create();
  await a.capture(row);
  const [key] = map.keys();
  map.set(key, '😀'.repeat(2200));
  await assert.rejects(a.read(), {code: 'journal_capacity'});
  map.set(key, '{invalid');
  await assert.rejects(a.settle(row), {code: 'journal_invalid'});
  assert.equal(map.get(key), '{invalid');
  map.set(key, JSON.stringify([row]).replace('{"version":1,', '{"version":1,"version":1,'));
  await assert.rejects(a.read(), {code: 'journal_invalid'});
});

test('the ninth record is refused without evicting another draft', async () => {
  const {bind} = setup();
  const lease = bind();
  for (let i = 1; i <= 8; i++) await lease.capture(base('draft_edit', {...start, draftId: String(i).repeat(32)}));
  await assert.rejects(lease.capture(create()), {code: 'journal_capacity'});
  assert.equal((await lease.read()).length, 8);
});

test('a recoverable failed settlement readback restores the exact previous hold', async () => {
  const {bind, map, storage} = setup();
  const lease = bind(), hold = reserve();
  await lease.capture(hold);
  let glitch = false;
  storage.setItem = (key, value) => { map.set(key, value); glitch = value === '[]'; };
  storage.getItem = key => {
    if (glitch) { glitch = false; return '{unverified'; }
    return map.get(key) ?? null;
  };
  await assert.rejects(lease.settle(hold), {code: 'journal_unavailable'});
  assert.deepEqual(await bind().read(), [hold]);
});

test('storage events reread exact scope and pending locks honor context retirement', async () => {
  const {bind, locks, events, map} = setup();
  let active = true;
  let observed;
  const change = new Promise(resolve => { observed = resolve; });
  const a = bind({isCurrent: () => active, changed: (rows, error) => observed({rows, error})});
  await a.read();
  const next = create();
  await bind().capture(next);
  const [key] = map.keys();
  const event = new Event('storage');
  Object.defineProperty(event, 'key', {value: key});
  events.dispatchEvent(event);
  assert.deepEqual(await change, {rows: [next], error: undefined});
  let enter;
  locks.request = async (name, _, callback) => {
    await new Promise(resolve => { enter = resolve; });
    return callback({name});
  };
  const pending = a.settle(next);
  while (!enter) await new Promise(resolve => setImmediate(resolve));
  active = false;
  enter();
  await assert.rejects(pending, {code: 'journal_context_changed'});
  assert.deepEqual(JSON.parse(map.get(key)), [next]);
});

test('only the same prepared attempt and frozen draft fence can transition to Send', async () => {
  const {bind, map} = setup();
  const a = bind(), b = bind();
  const prepared = base('send_prepare', {...start, commandDigest: '8'.repeat(64)});
  const sending = {...prepared, kind: 'send_execute', requestId: FILE, requestRevision: 1};
  await assert.rejects(a.capture(sending), {code: 'journal_conflict'});
  await a.capture(prepared);
  for (const change of [{revision: 4}, {writerGeneration: 2}, {commandDigest: '9'.repeat(64)},
    {attemptId: webcrypto.randomUUID()}]) {
    await assert.rejects(b.replace(prepared, {...sending, ...change}), {code: 'journal_conflict'});
  }
  for (const change of [{requestRevision: 2}, {fingerprint: '8'.repeat(64)}, {text: 'Fictional body'}]) {
    await assert.rejects(a.replace(prepared, {...sending, ...change}), {code: 'journal_invalid'});
  }
  await a.replace(prepared, sending);
  await assert.rejects(b.settle(prepared), {code: 'journal_conflict'});
  await assert.rejects(b.replace(sending, {...sending, requestId: UPLOAD}), {code: 'journal_conflict'});
  assert.deepEqual(await b.read(), [sending]);
  assert(!JSON.stringify([...map.values()]).includes('fingerprint'));
});

test('deletion serializes one file slot without interfering with another file or draft', async () => {
  const {bind} = setup();
  const a = bind(), b = bind();
  const prepare = base('deletion_prepare', {fileId: FILE, managementGeneration: 0, commandDigest: '4'.repeat(64)});
  const competing = base('deletion_confirm', {fileId: FILE, operationId: UPLOAD, previewGeneration: 1,
    revision: 1, commandDigest: '5'.repeat(64)});
  const results = await Promise.allSettled([a.capture(prepare), b.capture(competing)]);
  assert.equal(results.filter(row => row.status === 'fulfilled').length, 1);
  assert.equal(results.find(row => row.status === 'rejected').reason.code, 'journal_conflict');
  await a.capture(base('deletion_prepare', {fileId: UPLOAD, managementGeneration: 5, commandDigest: '6'.repeat(64)}));
  await a.capture(base('draft_remove', {...start, fileId: FILE}));
  assert.equal((await a.read()).length, 3);
  assert.deepEqual(await bind({scopeId: 'fictional:other'}).read(), []);
  assert.deepEqual(await bind({ownerKey: 'fictional-second-owner'}).read(), []);
  assert.deepEqual(await bind({destinationId: 'fictional-bram'}).read(), []);
});

test('deletion Confirm advances to Execute only by complete exact CAS and cannot settle the new hold', async () => {
  const {bind, map} = setup();
  const a = bind(), b = bind();
  const confirm = base('deletion_confirm', {fileId: FILE, operationId: UPLOAD, previewGeneration: 3,
    revision: 3, commandDigest: '4'.repeat(64)});
  const execute = {version: 1, attemptId: confirm.attemptId, kind: 'deletion_execute', fileId: FILE,
    operationId: UPLOAD, previewGeneration: 3, revision: 4, mode: 'execute'};
  await a.capture(confirm);
  for (const patch of [{attemptId: webcrypto.randomUUID()}, {fileId: DRAFT}, {operationId: DRAFT},
    {previewGeneration: 2}, {revision: 3, previewGeneration: 2}, {mode: 'recover'}]) {
    await assert.rejects(b.replace(confirm, {...execute, ...patch}), {code: 'journal_conflict'});
  }
  await assert.rejects(b.replace({...confirm, commandDigest: '7'.repeat(64)}, execute), {code: 'journal_conflict'});
  await a.replace(confirm, execute);
  await assert.rejects(b.settle(confirm), {code: 'journal_conflict'});
  await assert.rejects(b.replace(confirm, execute), {code: 'journal_conflict'});
  assert.deepEqual(await b.read(), [execute]);
  const [[, stored]] = map;
  assert.deepEqual(JSON.parse(stored), [execute]);
  await b.settle(execute);
  const explicit = {...execute, attemptId: webcrypto.randomUUID(), revision: 5, mode: 'recover'};
  await b.capture(explicit);
  assert.deepEqual(await a.read(), [explicit]);
});

test('deletion correlation rejects authority, metadata, invalid revisions and unsafe generations', async () => {
  const {bind} = setup();
  const a = bind();
  const prepare = base('deletion_prepare', {fileId: FILE, managementGeneration: 2, commandDigest: '4'.repeat(64)});
  const execute = base('deletion_execute', {fileId: FILE, operationId: UPLOAD, revision: 4, previewGeneration: 3, mode: 'execute'});
  for (const row of [
    {...prepare, commandKey: 'fictional-raw-key'}, {...prepare, managementGeneration: -1},
    {...prepare, managementGeneration: Number.MAX_SAFE_INTEGER}, {...execute, mode: 'retry'},
    {...execute, revision: 3}, {...execute, previewGeneration: 0}, {...execute, commandDigest: '8'.repeat(64)},
    {...execute, fingerprint: 'b'.repeat(64)}, {...execute, capability: 'b'.repeat(64)},
    {...execute, fileName: 'fictional.pdf'}, {...execute, bytes: 25}, {...execute, reason: null},
  ]) await assert.rejects(a.capture(row), {code: 'journal_invalid'});
  assert.deepEqual(await a.read(), []);
});
