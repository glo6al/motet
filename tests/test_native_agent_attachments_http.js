'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {request} = require('node:http');
const {createInterface} = require('node:readline');
const {webcrypto} = require('node:crypto');
const {join} = require('node:path');
const {createNativeAgentAttachmentsClient} = require('../src/web/native-agent-attachments-client.js');
const {createNativeAgentAttachmentsJournal} = require('../src/web/native-agent-attachments-journal.js');

const KEY = 'fictional-native-browser-fixture-key';
const ADA = {destinationId: 'fictional-ada'};
const BRAM = {destinationId: 'fictional-bram'};
const pdf = () => new File(['%PDF-1.4\nFictional untouched document.\n%%EOF'], 'fictional.pdf', {type: 'application/pdf'});
const png = () => new File([Buffer.from('89504e470d0a1a0a', 'hex'), 'Fictional image bytes'], 'fictional.png', {type: 'image/png'});

async function setup(t) {
  const child = spawn('python3', [join(__dirname, 'native_agent_reads_fixture.py')],
    {env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}, stdio: ['ignore', 'pipe', 'pipe']});
  const exited = once(child, 'exit');
  let errors = '';
  child.stderr.on('data', bytes => { errors = (errors + bytes).slice(-4000); });
  t.after(async () => {
    if (child.exitCode === null) child.kill('SIGTERM');
    const timeout = setTimeout(() => child.kill('SIGKILL'), 5000);
    try { const [code, signal] = await exited; assert.equal(code, 0, signal || errors); }
    finally { clearTimeout(timeout); }
  });
  const lines = createInterface({input: child.stdout});
  const timeout = AbortSignal.timeout(5000);
  const result = await Promise.race([once(lines, 'line', {signal: timeout}), exited.then(([code]) => { throw Error(`Fixture exited ${code}. ${errors}`); })]);
  const [origin] = result;
  lines.close();
  assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  const requests = [], storageMap = new Map(), queued = new Map();
  const storage = {getItem: k => storageMap.get(k) ?? null, setItem: (k, v) => storageMap.set(k, v)};
  const locks = {request(name, _, callback) {
    const task = (queued.get(name) ?? Promise.resolve()).then(() => callback({name}));
    queued.set(name, task.catch(() => {}));
    return task;
  }};
  let fault = null, delayed = null, stream = null;
  const transport = async (path, init) => {
    assert(path.startsWith('/api/attachments/'));
    assert(!/agent-message|send-intent|\/send$|\/preview/.test(path));
    requests.push({path, method: init.method});
    const selected = fault?.match(path, init.method) ? fault : null;
    if (selected) fault = null;
    if (selected?.when === 'before') throw Error('Fictional request transport loss.');
    if (selected?.when === 'delayed') {
      delayed = () => fetch(origin + path, init);
      throw Error('Fictional delayed request.');
    }
    if (selected?.when === 'stream') {
      const bytes = Buffer.from(init.body);
      return new Promise((resolve, reject) => {
        const req = request(origin + path, {method: 'PUT', signal: init.signal,
          headers: {...init.headers, 'Content-Length': String(bytes.length)}}, response => {
          const chunks = [];
          response.on('data', part => chunks.push(part));
          response.on('error', reject);
          response.on('end', () => resolve(new Response(Buffer.concat(chunks), {status: response.statusCode, headers: response.headers})));
        });
        req.setTimeout(5000, () => req.destroy(Error('Fictional stream did not settle.')));
        req.on('error', reject);
        req.write(bytes.subarray(0, 1));
        stream = {path, capability: init.headers['X-Attachment-Draft-Capability'], release: () => req.end(bytes.subarray(1))};
      });
    }
    const response = await fetch(origin + path, init);
    if (selected?.when === 'after') {
      assert(response.ok);
      await response.text();
      throw Error('Fictional committed response loss.');
    }
    return response;
  };
  function client(overrides = {}) {
    const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto});
    const value = createNativeAgentAttachmentsClient({enabled: true, demo: false, fetch: transport, auth: () => KEY,
      crypto: webcrypto, AbortController, journal, ...overrides});
    t.after(() => value.dispose());
    return value;
  }
  return {client, requests, storageMap, arm(when, match) { fault = {when, match}; }, async release() {
    const send = delayed; delayed = null;
    assert(send);
    const response = await send();
    assert.equal(response.status, 201);
    await response.text();
  }, async waitForReceiving() {
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      if (stream) {
        const read = await fetch(origin + stream.path, {headers: {'X-Live-Room-Key': KEY,
          'X-Attachment-Draft-Capability': stream.capability}});
        assert.equal(read.status, 200);
        if ((await read.json()).state === 'receiving') return stream.release;
      }
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert.fail('The real upload did not enter receiving.');
  }};
}

function writes(trace) { return trace.filter(r => r.method !== 'GET'); }
function ok(snapshot) { assert.equal(snapshot.error, null, JSON.stringify(snapshot.error)); return snapshot; }

test('actual client, journal and loopback store serve Stage 1 only', async t => {
  const env = await setup(t), changes = [];
  const a = env.client({changed: s => changes.push(s)});
  let s = ok(await a.connect(ADA));
  assert.deepEqual(s.features, {drafts: true, send: false, files: false, deletion: false});
  assert.equal(s.qualification.state, 'available');
  assert.equal(s.draftRead, 'complete');
  assert.deepEqual(writes(env.requests), []);
  const preflight = env.requests.length;
  s = await a.addFiles([pdf(), new File(['text'], 'fictional.txt', {type: 'text/plain'})]);
  assert.equal(s.error.code, 'unsupported_type');
  assert.equal(env.requests.length, preflight);
  assert.deepEqual(s.uploads, []);
  s = ok(await a.addFiles([pdf(), png()]));
  assert.deepEqual(s.draft.files.map(f => f.state), ['ready', 'ready']);
  assert.deepEqual(s.uploads.map(f => f.state), ['ready', 'ready']);
  assert(s.uploads.every(row => row.progress === null));
  assert(!s.draft.readOnly);
  assert(changes.some(change => change.uploads.some(row => row.state === 'reading')));
  assert(changes.some(change => change.uploads.some(row => row.state === 'reserved')));
  const [one, two] = s.draft.files;
  const [local] = s.uploads;
  assert.notEqual(one.detailsHandle, local.detailsHandle);
  assert.equal((await a.openFileDetails(local.detailsHandle)).detail.owner, 'selection');
  assert.equal((await a.openFileDetails(one.detailsHandle)).detail.owner, 'draft');
  assert.equal((await a.send('Fictional text.')).error.code, 'unavailable');
  s = ok(await a.editText('Fictional unsent text.'));
  assert.equal(s.draft.text, 'Fictional unsent text.');
  assert.equal(s.unsavedEdit, null);
  s = ok(await a.removeFile(one.id));
  assert.deepEqual(s.draft.files.map(f => f.id), [two.id]);
  assert.equal(s.detail, null);
  assert.equal((await a.openFileDetails(one.detailsHandle)).error.code, 'invalid_request');
  assert(!JSON.stringify(changes).includes(KEY));
  for (const word of ['writerNonce', 'fingerprint', 'capability\"', 'sourceCapability', 'commandDigest']) {
    assert(!JSON.stringify(changes).includes(word), word);
  }
  assert.deepEqual([...env.storageMap.values()].map(JSON.parse).flat(), []);
  a.dispose();
  const b = env.client();
  s = ok(await b.connect(ADA));
  assert.equal(s.draft, null);
  const summary = s.savedDrafts[0];
  assert(summary && summary.readOnly);
  s = ok(await b.resume(summary.id));
  assert.equal(s.draft.files[0].name, 'fictional.png');
  assert.equal(s.draft.writerGeneration, summary.writerGeneration + 1);
  s = ok(await b.discardDraft());
  assert.equal(s.draft, null);
  assert.deepEqual(s.attempts, []);
  assert.equal(writes(env.requests).filter(r => r.method === 'PUT').length, 2);
});

test('a null receipt cannot retire a delayed allocation', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  env.arm('delayed', (path, method) => method === 'POST' && path.endsWith('/drafts'));
  let s = await a.createDraft();
  assert.equal(s.attempts[0].phase, 'creating');
  s = await a.refresh();
  assert.equal(s.attempts[0].phase, 'creating');
  assert.deepEqual(s.savedDrafts, []);
  await env.release();
  s = ok(await a.refresh());
  assert.equal(s.attempts.length, 0);
  assert.equal(s.draft, null);
  assert.equal(s.savedDrafts.length, 1);
  assert.equal(writes(env.requests).length, 1);
});

test('lost reserve acknowledgement never forwards bytes through refresh or cold Resume', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  env.arm('after', (path, method) => method === 'POST' && path.endsWith('/upload-intents'));
  let s = await a.addFiles([pdf()]);
  assert.equal(s.attempts[0].phase, 'reserving');
  assert.equal(s.uploads[0].fileId, null);
  a.dispose();
  const b = env.client();
  s = ok(await b.connect(ADA));
  assert.equal(s.attempts[0].phase, 'reserving');
  assert.equal(s.draft, null);
  assert.equal(s.savedDrafts[0].fileCount, 1);
  s = ok(await b.resume(s.savedDrafts[0].id));
  assert.equal(s.draft.files[0].state, 'reserved');
  assert.equal(s.attempts.length, 0);
  assert.equal(env.requests.filter(r => r.method === 'PUT').length, 0);
  s = ok(await b.removeFile(s.draft.files[0].id));
  assert.deepEqual(s.draft.files, []);
});

test('lost PUT and discard settle only from positive same-scope evidence', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  env.arm('after', (_, method) => method === 'PUT');
  let s = await a.addFiles([pdf()]);
  assert.equal(s.attempts[0].phase, 'uploading');
  const id = s.draft.id;
  assert.equal((await a.connect(BRAM)).attempts.length, 0);
  s = ok(await a.connect(ADA));
  assert.equal(s.attempts[0].phase, 'uploading');
  assert.equal(s.draft, null);
  s = ok(await a.resume(id));
  assert.equal(s.draft.files[0].state, 'ready');
  assert.equal(s.attempts.length, 0);
  env.arm('after', (path, method) => method === 'DELETE' && path.endsWith('/drafts/' + id));
  s = await a.discardDraft();
  assert.equal(s.attempts[0].phase, 'discarding');
  s = ok(await a.refresh());
  assert.equal(s.draft, null);
  assert.equal(s.draftRead, 'complete');
  assert.equal(s.attempts.length, 0);
  assert.equal(env.requests.filter(r => r.method === 'PUT').length, 1);
});

test('lost Resume rotates the writer and requires a new explicit choice', async t => {
  const env = await setup(t), old = env.client();
  await old.connect(ADA);
  const initial = ok(await old.addFiles([pdf()])).draft;
  const next = env.client();
  await next.connect(ADA);
  env.arm('after', (path, method) => method === 'POST' && path.endsWith('/resume'));
  let s = await next.resume(initial.id);
  assert.equal(s.attempts[0].phase, 'resuming');
  assert.equal(s.draft, null);
  const before = writes(env.requests).length;
  s = ok(await next.refresh());
  assert.equal(s.draft, null);
  assert.equal(s.savedDrafts[0].writerGeneration, initial.writerGeneration + 1);
  assert.equal(writes(env.requests).length, before);
  s = await old.refresh();
  assert(s.draft.readOnly);
  await old.editText('Fictional stale editor.');
  assert.equal(writes(env.requests).length, before);
  s = ok(await next.resume(initial.id));
  assert.equal(s.draft.writerGeneration, initial.writerGeneration + 2);
  assert.equal(s.draft.text, '');
});

test('real receiving PUT cannot erase the exact later Remove hold', async t => {
  const env = await setup(t), old = env.client(), next = env.client();
  await old.connect(ADA);
  env.arm('stream', (_, method) => method === 'PUT');
  const upload = old.addFiles([pdf()]);
  const release = await env.waitForReceiving();
  try {
    let s = ok(await next.connect(ADA));
    assert.equal(s.attempts[0].phase, 'uploading');
    s = ok(await next.resume(s.savedDrafts[0].id));
    assert.equal(s.draft.files[0].state, 'receiving');
    assert.equal(s.attempts[0].phase, 'uploading');
    env.arm('after', (path, method) => method === 'DELETE' && path.includes('/files/'));
    s = await next.removeFile(s.draft.files[0].id);
    assert.equal(s.attempts[0].phase, 'removing');
    const held = [...env.storageMap.values()].map(JSON.parse).flat();
    assert.equal(held.length, 1);
    assert.equal(held[0].kind, 'draft_remove');
    assert.equal(held[0].writerGeneration, 2);
    release();
    const first = await upload;
    assert.equal(first.error.code, 'draft_conflict');
    assert.deepEqual([...env.storageMap.values()].map(JSON.parse).flat(), held);
    s = ok(await next.refresh());
    assert.deepEqual(s.draft.files, []);
    assert.deepEqual(s.attempts, []);
    assert.equal(env.requests.filter(r => r.method === 'PUT').length, 1);
  } finally { release(); }
});

test('lost edit and recovery grant keep their own holds without replay', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  const id = ok(await a.addFiles([pdf()])).draft.id;
  env.arm('before', (_, method) => method === 'PATCH');
  let s = await a.editText('Fictional unacknowledged text.');
  assert.equal(s.attempts[0].phase, 'editing');
  assert.equal(s.attempts[0].submitted.text, 'Fictional unacknowledged text.');
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional unacknowledged text.'});
  assert(!JSON.stringify([...env.storageMap.values()]).includes('unacknowledged text'));
  s = ok(await a.refresh());
  assert.equal(s.attempts[0].phase, 'editing');
  assert.equal(s.draft.text, '');
  env.arm('after', (path, method) => method === 'POST' && path.endsWith('/recover'));
  s = await a.resume(id);
  assert.deepEqual(s.attempts.map(r => r.phase).sort(), ['editing', 'recovering']);
  const writesBefore = writes(env.requests).length;
  s = ok(await a.refresh());
  assert.equal(writes(env.requests).length, writesBefore);
  assert.deepEqual(s.attempts.map(r => r.phase).sort(), ['editing', 'recovering']);
  s = ok(await a.resume(id));
  assert.equal(s.draft.text, '');
  assert.deepEqual(s.attempts, []);
  assert.equal(s.uncertain, false);
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional unacknowledged text.'});
  env.arm('after', (_, method) => method === 'PATCH');
  s = await a.editText('Fictional committed text.');
  assert.equal(s.attempts[0].phase, 'editing');
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional committed text.'});
  s = ok(await a.refresh());
  assert.equal(s.draft.text, 'Fictional committed text.');
  assert.equal(s.unsavedEdit, null);
  assert.deepEqual(s.attempts, []);
  assert.equal(env.requests.filter(r => r.method === 'PATCH').length, 2);
});

test('a newer writer’s saved text and the local unsaved edit remain separate', async t => {
  const env = await setup(t), a = env.client(), b = env.client();
  await a.connect(ADA);
  const id = ok(await a.addFiles([pdf()])).draft.id;
  env.arm('before', (_, method) => method === 'PATCH');
  await a.editText('Fictional local edit.');
  await b.connect(ADA);
  ok(await b.resume(id));
  ok(await b.editText('Fictional newer saved text.'));
  let s = await a.refresh();
  assert(s.draft.readOnly);
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional local edit.'});
  s = ok(await a.resume(id));
  assert.equal(s.draft.text, 'Fictional newer saved text.');
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional local edit.'});
  assert.deepEqual(s.attempts, []);
  assert.equal(env.requests.filter(r => r.method === 'PATCH').length, 2);
  s = ok(await a.editText(s.unsavedEdit.text));
  assert.equal(s.draft.text, 'Fictional local edit.');
  assert.equal(s.unsavedEdit, null);
});

test('local dismissal preserves the PATCH hold and never performs a request', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  const id = ok(await a.addFiles([pdf()])).draft.id;
  env.arm('before', (_, method) => method === 'PATCH');
  const held = await a.editText('Fictional dismissed text.');
  const before = env.requests.length;
  assert.equal((await a.discardUnsavedEdit('0'.repeat(32))).error.code, 'invalid_request');
  let s = ok(await a.discardUnsavedEdit(id));
  assert.equal(s.unsavedEdit, null);
  assert.equal(s.draft.text, '');
  assert.equal(s.uncertain, true);
  assert.deepEqual(s.attempts, [{...held.attempts[0], submitted: null}]);
  assert.equal(env.requests.length, before);
  ok(await a.refresh());
  s = ok(await a.resume(id));
  assert.equal(s.unsavedEdit, null);
  assert.deepEqual(s.attempts, []);
});

test('cold reload and context retirement retain exact correlation without local editor text', async t => {
  const env = await setup(t), a = env.client(), cold = env.client();
  await a.connect(ADA);
  const id = ok(await a.addFiles([pdf()])).draft.id;
  env.arm('before', (_, method) => method === 'PATCH');
  const local = await a.editText('Fictional memory-only text.');
  assert.equal(local.unsavedEdit.draftId, id);
  let s = ok(await cold.connect(ADA));
  assert.equal(s.unsavedEdit, null);
  assert.equal(s.draft, null);
  assert.deepEqual(s.attempts, [{...local.attempts[0], submitted: null}]);
  assert.equal((await a.connect(BRAM)).unsavedEdit, null);
  s = ok(await a.connect(ADA));
  assert.deepEqual(s.attempts, [{...local.attempts[0], submitted: null}]);
  assert.equal(s.unsavedEdit, null);
  assert(!JSON.stringify([...env.storageMap.values()]).includes('memory-only text'));
  s = ok(await a.resume(id));
  assert.equal(s.draft.text, '');
  assert.equal(s.unsavedEdit, null);
});

test('a positive discard receipt removes the unsaved edit for its exact draft', async t => {
  const env = await setup(t), a = env.client();
  await a.connect(ADA);
  const id = ok(await a.addFiles([pdf()])).draft.id;
  env.arm('before', (_, method) => method === 'PATCH');
  await a.editText('Fictional draft-only text.');
  ok(await a.refresh());
  let s = ok(await a.resume(id));
  assert.deepEqual(s.unsavedEdit, {draftId: id, text: 'Fictional draft-only text.'});
  env.arm('after', (path, method) => method === 'DELETE' && path.endsWith('/drafts/' + id));
  s = await a.discardDraft();
  assert.equal(s.attempts[0].phase, 'discarding');
  assert.equal(s.unsavedEdit.draftId, id);
  s = ok(await a.refresh());
  assert.equal(s.draft, null);
  assert.equal(s.unsavedEdit, null);
  assert.deepEqual(s.attempts, []);
  assert.equal(ok(await a.createDraft()).unsavedEdit, null);
});
