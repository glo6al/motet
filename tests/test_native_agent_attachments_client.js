'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const {createNativeAgentAttachmentsClient} = require('../src/web/native-agent-attachments-client.js');
const {createNativeAgentAttachmentsJournal} = require('../src/web/native-agent-attachments-journal.js');

const DEST = 'fictional-ada', SCOPE = 'fictional-orchard', KEY = 'fictional-native-test-owner';
const ID = 'a'.repeat(32), CAP = 'b'.repeat(64), NONCE = 'c'.repeat(64);
const context = {kind: 'agent', destinationId: DEST, scopeId: SCOPE, modelId: null, conversationId: null};
const destination = {kind: 'agent', id: DEST, scopeId: SCOPE, modelId: null, conversationId: null, capabilityRevision: 'fictional-v1'};
const at = {createdAt: 100, updatedAt: 100, expiresAt: null};
const empty = () => ({id: ID, destination, state: 'active', revision: 1, writerGeneration: 1, ...at, text: '', files: []});
const positive = (value, status = 200) => new Response(JSON.stringify({schemaVersion: 1, fixture: false,
  serverTime: '2026-01-01T00:00:00.000001Z', ...value}), {status});
const selection = () => ({context, features: {drafts: true, send: false, files: false, deletion: false}});
const caps = () => ({available: true, kind: 'agent', destinationId: DEST, scopeId: SCOPE, modelId: null,
  capabilityRevision: 'fictional-v1', formats: ['application/pdf'], preparationModes: {'application/pdf': 'original_relay'},
  limits: {storageBytes: 10000, fileBytes: 100, draftBytes: 200, fileCount: 2, agentTextCharacters: 2000,
    officerTextCharacters: 4000, preparationFormats: ['text/plain', 'text/markdown']}});
const upload = (data = '%PDF-fictional') => new File([data], 'fictional.pdf', {type: 'application/pdf'});
const deferred = () => { let resolve; const value = new Promise(r => { resolve = r; }); return {value, resolve}; };

function setup(overrides = {}) {
  const requests = [], changes = [], store = new Map();
  const storage = {getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value)};
  const locks = {request: async (name, _, fn) => fn({name})};
  const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto});
  let route = overrides.route;
  let key = KEY;
  const client = createNativeAgentAttachmentsClient({enabled: true, demo: false, auth: () => key,
    crypto: webcrypto, AbortController, journal, changed: s => changes.push(s),
    async fetch(path, init) {
      requests.push({path, method: init.method, signal: init.signal});
      assert.equal(init.cache, 'no-store');
      assert.equal(init.redirect, 'error');
      assert.equal(init.headers['X-Live-Room-Key'], key);
      const injected = await route?.(path, init);
      if (injected) return injected;
      if (path.includes('/native/selection?')) return positive(selection());
      if (path.includes('/capabilities?')) return positive(caps());
      if (path.includes('/drafts?')) return positive({items: [], nextCursor: null});
      if (path.endsWith('/drafts') && init.method === 'POST') return positive({...empty(), capability: CAP, writerNonce: NONCE}, 201);
      if (path.includes('/native/receipts/')) return positive({match: null});
      assert.fail('Unexpected route ' + path);
    }, ...overrides.options});
  return {client, requests, changes, storage, store, newKey(next) { key = next; }, route(next) { route = next; }};
}

test('disabled, demo and construction are inert across public calls', async () => {
  let calls = 0;
  const fail = () => { calls += 1; throw Error('Site callback should remain inert.'); };
  for (const flags of [{enabled: false, demo: false}, {enabled: true, demo: true}, {enabled: true}]) {
    const c = createNativeAgentAttachmentsClient({...flags, auth: fail, fetch: fail, changed: fail, crypto: {randomUUID: fail},
      AbortController: fail, journal: {bind: fail}});
    assert.equal(c.snapshot().context, null);
    await c.connect({destinationId: DEST}); await c.refresh(); await c.createDraft(); await c.addFiles([upload()]);
    await c.openFileDetails('bad-handle'); c.invalidate(); c.dispose();
  }
  assert.equal(calls, 0);
});

test('wrong selection or unsupported flags fail before capability or draft reads', async () => {
  for (const bad of [
    {...selection(), context: {...context, destinationId: 'fictional-other'}},
    {...selection(), context: {...context, modelId: 'fictional-model'}},
    {...selection(), features: {...selection().features, send: true}},
    {...selection(), context: {...context, unknown: 'not projected'}},
  ]) {
    const {client, requests} = setup({route: () => positive(bad)});
    const s = await client.connect({destinationId: DEST});
    assert.equal(s.error.code, 'invalid_response');
    assert.equal(s.context, null);
    assert.equal(s.draftRead, 'incomplete');
    assert.equal(requests.length, 1);
  }
});

test('retained summary and unavailable qualification are separate', async () => {
  const {client, requests} = setup({route(path) {
    if (path.includes('/capabilities?')) return positive({available: false, destinationId: DEST, scopeId: SCOPE,
      modelId: null, reason: 'Private wire reason is stripped.'});
    if (path.includes('/drafts?')) return positive({items: [{id: ID, destination, state: 'active', revision: 2, writerGeneration: 1,
      ...at, fileCount: 1, totalBytes: 20}], nextCursor: null});
  }});
  const s = await client.connect({destinationId: DEST});
  assert.equal(s.draftRead, 'complete');
  assert.equal(s.qualification.state, 'unavailable');
  assert(!JSON.stringify(s).includes('Private wire reason'));
  assert.equal(s.savedDrafts[0].id, ID);
  assert(s.savedDrafts[0].readOnly);
  assert.equal(s.draft, null);
  assert.equal((await client.createDraft()).error.code, 'destination_unavailable');
  assert(requests.every(r => r.method === 'GET'));
  s.savedDrafts[0].id = 'changed externally';
  assert.equal(client.snapshot().savedDrafts[0].id, ID);
});

test('unsupported SQLite is a typed read refusal without any file write', async () => {
  const expected = 'Sending files to agents is unavailable because this Python uses an older database library. Install a newer Python and use it to restart Motet.';
  const {client, requests} = setup({route: () => positive({error: {
    code:'sqlite_unavailable', message:'Untrusted wire prose.', retryable:false}}, 503)});
  const s = await client.connect({destinationId:DEST});
  assert.equal(s.error.code, 'sqlite_unavailable');
  assert.equal(s.error.message, expected);
  assert.equal(s.context, null);
  assert(Object.values(s.features).every(value => value === false));
  assert.equal(s.busy, false);
  assert.equal(s.uncertain, false);
  assert.equal(s.uploads.length + s.attempts.length + s.requests.length, 0);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, 'GET');
});

test('malformed draft page leaves listing incomplete and cannot resume a guessed draft', async () => {
  const {client, requests} = setup({route(path) {
    if (path.includes('/drafts?')) return positive({items: [{id: ID, destination: {...destination, scopeId: 'fictional-wrong'},
      state: 'active', revision: 1, writerGeneration: 1, ...at, fileCount: 0, totalBytes: 0}], nextCursor: null});
  }});
  const result = await client.connect({destinationId: DEST});
  assert.equal(result.draftRead, 'incomplete');
  assert.equal(result.error.code, 'invalid_response');
  await client.resume(ID);
  assert(requests.every(r => r.method === 'GET'));
});

test('a gate-off reply reaches an incomplete safe state without raw server prose', async () => {
  const {client} = setup({route: () => positive({error: {code: 'attachments_disabled', message: 'Fictional private reason', retryable: false}}, 503)});
  const s = await client.connect({destinationId: DEST});
  assert.equal(s.draftRead, 'incomplete');
  assert.equal(s.error.code, 'unavailable');
  assert(!JSON.stringify(s).includes('Fictional private reason'));
});

test('invalid file batches are rejected before bytes, digest, or allocation', async () => {
  const {client, requests} = setup();
  await client.connect({destinationId: DEST});
  let reads = 0;
  const file = {name: 'fictional.pdf', type: 'application/pdf', size: 5, arrayBuffer: async () => { reads++; return new ArrayBuffer(5); }};
  for (const bad of [
    [file, {...file, type: 'text/plain'}], [file, {...file, name: 'unsafe/name.pdf'}],
    [file, {...file, size: 101}], [file, file, file],
  ]) {
    const s = await client.addFiles(bad);
    assert(s.error);
    assert.deepEqual(s.uploads, []);
  }
  assert.equal(reads, 0);
  assert(requests.every(r => r.method === 'GET'));
});

test('details are local for a reading or failed selection and retire when it is removed', async () => {
  const {client, requests} = setup();
  await client.connect({destinationId: DEST});
  const entered = deferred(), read = deferred();
  const body = {name: 'fictional.pdf', type: 'application/pdf', size: 5,
    async arrayBuffer() { entered.resolve(); return read.value; }};
  const adding = client.addFiles([body]);
  await entered.value;
  const row = client.snapshot().uploads[0];
  assert.equal(row.state, 'reading');
  let detail = (await client.openFileDetails(row.detailsHandle)).detail;
  assert.equal(detail.owner, 'selection');
  assert.equal(detail.fileId, null);
  assert.equal(detail.draftId, null);
  read.resolve(new ArrayBuffer(4));
  const done = await adding;
  assert.equal(done.uploads[0].state, 'failed');
  assert.equal(done.uploads[0].error.code, 'file_unavailable');
  assert.equal((await client.removeSelection(row.id)).detail, null);
  assert.equal((await client.openFileDetails(row.detailsHandle)).error.code, 'invalid_request');
  assert.deepEqual(requests.filter(r => r.method !== 'GET').map(r => r.path), ['/api/attachments/drafts']);
});

test('corrupt storage and a failed pre-dispatch capture preserve known reads and block writes', async () => {
  const env = setup();
  const {client, requests, store, storage} = env;
  assert.equal((await client.connect({destinationId: DEST})).draftRead, 'complete');
  storage.setItem = () => { throw Error('Fictional full storage.'); };
  let s = await client.createDraft();
  assert.equal(s.error.code, 'journal_unavailable');
  assert(s.uncertain);
  assert(requests.every(r => r.method === 'GET'));
  storage.setItem = (k, v) => store.set(k, v);
  s = await client.refresh();
  assert(!s.uncertain);
  assert.equal(s.draftRead, 'complete');
  assert.equal((await client.createDraft()).draft.id, ID);
  const [scopeKey] = store.keys();
  store.set(scopeKey, '{broken');
  env.route((path, init) => path.endsWith('/drafts/' + ID) && init.method === 'GET' ? positive(empty()) : undefined);
  s = await client.refresh();
  assert(s.draft.readOnly);
  assert.equal(s.error.code, 'journal_unavailable');
  assert(s.uncertain);
  assert.equal(store.get(scopeKey), '{broken');
});

test('rekey aborts a dispatched mutation and suppresses a late authority acknowledgement', async () => {
  const posted = deferred(), reply = deferred();
  const env = setup({route(path, init) {
    if (path.endsWith('/drafts') && init.method === 'POST') { posted.resolve(init.signal); return reply.value; }
  }});
  const {client, store, changes} = env;
  await client.connect({destinationId: DEST});
  const creating = client.createDraft();
  const signal = await posted.value;
  env.newKey('fictional-rotated-owner');
  assert.equal(client.snapshot().context, null);
  assert(signal.aborted);
  reply.resolve(positive({...empty(), capability: CAP, writerNonce: NONCE}, 201));
  assert.equal((await creating).draft, null);
  assert(changes.every(s => s.draft === null));
  const original = JSON.parse([...store.values()][0]);
  assert.equal(original[0].kind, 'draft_create');
  const entered = await client.connect({destinationId: DEST});
  assert.equal(entered.attempts.length, 0);
  assert.deepEqual(JSON.parse([...store.values()][0]), original);
});

test('fixed typed create refusal settles only its record and never invents a saved draft', async () => {
  const {client, store} = setup({route(path, init) {
    if (path.endsWith('/drafts') && init.method === 'POST') {
      return positive({error: {code: 'draft_capacity', message: 'Fictional raw capacity prose', retryable: false}}, 429);
    }
  }});
  await client.connect({destinationId: DEST});
  const s = await client.createDraft();
  assert.equal(s.error.code, 'draft_capacity');
  assert.equal(s.draft, null);
  assert(!s.uncertain);
  assert.deepEqual([...store.values()].map(JSON.parse), [[]]);
});

test('storage recovery can retire its exact proven never-dispatched capture', async () => {
  const {client, requests, storage, store} = setup();
  await client.connect({destinationId: DEST});
  let unreadable = false;
  storage.setItem = (key, value) => { store.set(key, value); unreadable = true; };
  storage.getItem = key => { if (unreadable) throw Error('Fictional read failure'); return store.get(key) ?? null; };
  const failed = await client.createDraft();
  assert(failed.uncertain);
  assert.equal(JSON.parse([...store.values()][0])[0].kind, 'draft_create');
  unreadable = false;
  storage.setItem = (key, value) => store.set(key, value);
  const recovered = await client.refresh();
  assert.equal(recovered.draft, null);
  assert(!recovered.uncertain);
  assert(requests.every(r => r.method === 'GET' && !r.path.includes('/receipts/')));
  assert.deepEqual(JSON.parse([...store.values()][0]), []);
});

test('failure to record the cold recovery grant cannot start Resume', async () => {
  const env = setup({route(path, init) {
    if (path.includes('/drafts?')) return positive({items: [{id: ID, destination, state: 'active', revision: 1, writerGeneration: 1,
      ...at, fileCount: 0, totalBytes: 0}], nextCursor: null});
    if (path.endsWith('/recover') && init.method === 'POST') {
      env.storage.setItem = () => { throw Error('Fictional quota loss.'); };
      return positive({id: ID, revision: 1, writerGeneration: 1, recoveryCapability: CAP});
    }
  }});
  await env.client.connect({destinationId: DEST});
  const result = await env.client.resume(ID);
  assert.equal(result.error.code, 'journal_unavailable');
  assert.equal(result.draft, null);
  assert.equal(result.attempts[0].phase, 'recovering');
  assert.deepEqual(env.requests.filter(r => r.method !== 'GET').map(r => r.path), ['/api/attachments/drafts/' + ID + '/recover']);
  assert(!JSON.stringify([...env.store.values()]).includes(CAP));
});

test('staged projection refuses fabricated queue, unavailable prose, foreign rows and deletion enablement', async () => {
  const valid = {destinationId: DEST, scopeId: SCOPE, draftFence: {revision: 7, writerGeneration: 1},
    draftAdmission: 'matched', request: {id: '1'.repeat(32), draftId: ID, revision: 8, createdAt: 100,
      text: 'Fictional sent text.', preparation: 'materialized', files: [{fileId: '2'.repeat(32), name: 'orchard.pdf',
        mediaType: 'application/pdf', bytes: 5, savedCopyState: 'unknown'}], delivery: 'queued', accepted: true,
      execution: 'ended', reply: {state: 'available', text: 'Exact fixture reply.'}, reason: null}};
  const corruptions = [
    row => { row.request.accepted = false; },
    row => { row.request.reply.state = 'withheld'; },
    row => { row.request.reply.text = null; },
    row => { row.scopeId = 'foreign-scope'; },
    row => { row.request.files.push({...row.request.files[0]}); },
    row => { row.request.reply.text = '🪴'.repeat(16001); },
    row => { row.request.ownerRequestId = 'private-fixture-owner-id'; },
  ];
  for (const corrupt of corruptions) {
    const invalid = structuredClone(valid);
    corrupt(invalid);
    const {client, requests} = setup({options: {stage23Fixture: true}, route(path) {
      if (path.includes('/native/selection?')) return positive({...selection(), features: {...selection().features, send: true}});
      if (path.includes('/native/requests?')) return positive({items: [invalid], nextCursor: null});
    }});
    const snapshot = await client.connect({destinationId: DEST});
    assert.equal(snapshot.error?.code, 'requests_incomplete');
    assert.deepEqual(snapshot.requests, []);
    assert(requests.every(r => r.method === 'GET'));
  }
  const {client, requests} = setup({options: {stage23Fixture: true}, route() {
    return positive({...selection(), features: {...selection().features, files: true, deletion: true}});
  }});
  const denied = await client.connect({destinationId: DEST});
  assert.equal(denied.context, null);
  assert.equal(denied.error.code, 'invalid_response');
  assert.equal(requests.length, 1);
});

test('deletion requires both fixture options and an exact Files-only Send-off selection', async () => {
  const valid = {context, features: {drafts: false, files: true, send: false, deletion: true}};
  for (const [options, result] of [
    [{deletionFixture: true}, valid], [{stage23Fixture: true}, valid],
    [{stage23Fixture: true, deletionFixture: true}, {...valid, features: {...valid.features, files: false}}],
    [{stage23Fixture: true, deletionFixture: true}, {...valid, features: {...valid.features, drafts: true, send: true}}],
  ]) {
    const {client, requests} = setup({options, route: () => positive(result)});
    const rejected = await client.connect({destinationId: DEST});
    assert.equal(rejected.error.code, 'invalid_response');
    assert.equal(rejected.context, null);
    assert.equal(requests.length, 1);
    for (const method of ['prepareDeletion', 'openDeletion', 'confirmDeletion', 'executeDeletion', 'closeDeletion']) {
      await client[method]('fictional-handle');
    }
    assert.equal(requests.length, 1);
  }
});

test('deletion opt-in rejects unverified management without removing accurate Files metadata', async () => {
  const row = {id: ID, requestId: '2'.repeat(32), name: 'fictional.pdf', mediaType: 'application/pdf', bytes: 3,
    cataloguedAt: 100, savedCopyAvailable: false, savedCopyState: 'unknown', retentionPinned: false,
    preparationKind: 'original_relay', previewAvailable: false, expiresAt: null};
  const managed = {fileId: ID, requestId: row.requestId, managementGeneration: 0,
    sourceAvailable: true, canPrepare: true, operation: null};
  for (const items of [[managed, {...managed}], [{...managed, capability: CAP}], [{...managed, operation: {id: ID, previewGeneration: 1}}]]) {
    const {client, requests} = setup({options: {stage23Fixture: true, deletionFixture: true}, route(path) {
      if (path.includes('/native/selection?')) return positive({context, features: {drafts: false, files: true, send: false, deletion: true}});
      if (path.includes('/native/files/management?')) return positive({destinationId: DEST, scopeId: SCOPE, items, nextCursor: null});
      if (path.includes('/files?')) return positive({destinationId: DEST, scopeId: SCOPE, items: [row],
        associationCount: 1, unclassifiedAssociationCount: 0, nextCursor: null});
    }});
    const result = await client.connect({destinationId: DEST});
    assert.equal(result.error.code, 'deletion_incomplete');
    assert.equal(result.files.associationCount, 1);
    assert.equal(result.files.items[0].management, null);
    assert.equal(result.files.items[0].savedCopyAvailable, false);
    assert.equal(result.deletion, null);
    assert.equal(requests.length, 3);
  }
  const {client} = setup({options: {stage23Fixture: true, deletionFixture: true}, route(path) {
    if (path.includes('/native/selection?')) return positive({context, features: {drafts: false, files: true, send: false, deletion: true}});
    if (path.includes('/native/files/management?')) return positive({destinationId: DEST, scopeId: SCOPE,
      items: [{...managed, requestId: '3'.repeat(32)}], nextCursor: null});
    if (path.includes('/files?')) return positive({destinationId: DEST, scopeId: SCOPE, items: [row],
      associationCount: 1, unclassifiedAssociationCount: 0, nextCursor: null});
  }});
  const unmatched = await client.connect({destinationId: DEST});
  assert.equal(unmatched.error, null);
  assert.equal(unmatched.files.items[0].management, null);
});
