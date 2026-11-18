'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const {webcrypto} = require('node:crypto');
const {join} = require('node:path');
const {createNativeAgentAttachmentsClient} = require('../src/web/native-agent-attachments-client.js');
const {createNativeAgentAttachmentsJournal} = require('../src/web/native-agent-attachments-journal.js');

const KEY = 'fictional-native-browser-fixture-key';
const ADA = {destinationId: 'x:00000000-0000-4000-8000-000000000001'};
const BRAM = {destinationId: 'fictional-bram'};
const SCOPE = 'fictional-orchard';
const REQUEST = '/api/attachments/native/requests';
const EXACT = id => REQUEST + '/' + id + '?' + new URLSearchParams({...ADA, scopeId: SCOPE});
const LIST = cursor => REQUEST + '?' + new URLSearchParams({...ADA, scopeId: SCOPE, ...(cursor ? {cursor} : {})});
const PREPARE = /\/send-intent$/;
const SEND = /\/send$/;
const DELETE_PREPARE = /\/deletion-intent$/;
const DELETE_CONFIRM = /\/deletions\/[a-f0-9]{32}\/confirm$/;
const DELETE_EXECUTE = /\/deletions\/[a-f0-9]{32}\/execute$/;
const pdf = (name = 'orchard.pdf') => new File(['%PDF-1.4\nFictional untouched PDF.\n%%EOF'], name, {type: 'application/pdf'});
const png = (name = 'orchard.png') => new File([Buffer.from('89504e470d0a1a0a', 'hex'), 'Fictional PNG'], name, {type: 'image/png'});
const jpg = (name = 'orchard.jpg') => new File([Buffer.from('ffd8ff', 'hex'), 'Fictional JPEG'], name, {type: 'image/jpeg'});

async function setup(t, {deletion = false} = {}) {
  const child = spawn('python3', [join(__dirname, 'native_agent_stage23_fixture.py'), ...(deletion ? ['--deletion'] : [])], {
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const exited = once(child, 'exit');
  let errors = '';
  child.stderr.on('data', bytes => { errors = (errors + bytes).slice(-6000); });
  t.after(async () => {
    child.stdin.end();
    const timeout = setTimeout(() => child.kill('SIGKILL'), 6000);
    try { const [code, signal] = await exited; assert.equal(code, 0, signal || errors); }
    finally { clearTimeout(timeout); }
  });
  const lines = createInterface({input: child.stdout});
  async function nextLine(phase, timeoutMs = 15000) {
    try {
      const result = await Promise.race([
        once(lines, 'line', {signal: AbortSignal.timeout(timeoutMs)}),
        exited.then(([code]) => { throw Error(`Fixture exited ${code}. ${errors}`); }),
      ]);
      return result[0];
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error(`Fixture ${phase} timed out after ${timeoutMs}ms.`, {cause: error});
      }
      throw error;
    }
  }
  const origin = await nextLine('startup');
  assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  async function rpc(action, fields = {}) {
    // Bulk seeds run real durable file lifecycles, outside client HTTP budgets.
    const phase = `RPC action=${action}` + (action === 'seed' ? ` count=${fields.count}` : '');
    const promised = nextLine(phase, action === 'seed' ? 60000 : 15000);
    child.stdin.write(JSON.stringify({action, ...fields}) + '\n');
    return JSON.parse(await promised);
  }
  const storageMap = new Map(), queue = new Map(), requests = [], snapshots = [];
  let fault = null, delayed = null, key = KEY, storageFault = null;
  const storage = {getItem: k => storageMap.get(k) ?? null, setItem(k, value) {
    if (storageFault && value.includes('"kind":"' + storageFault.kind + '"')) {
      if (storageFault.mode === 'drop') return;
      throw Error('Fictional storage failure.');
    }
    storageMap.set(k, value);
  }};
  const locks = {request(name, _, callback) {
    const next = (queue.get(name) ?? Promise.resolve()).then(() => callback({name}));
    queue.set(name, next.catch(() => {}));
    return next;
  }};
  async function transport(path, init) {
    assert(path.startsWith('/api/attachments/') && !/agent-message|\/preview/.test(path));
    requests.push({path, method: init.method, headers: {...init.headers}, body: init.body});
    const selected = fault && fault.test(path, init.method) ? fault : null;
    if (selected) fault = null;
    if (selected?.when === 'before') throw Error('Fictional transport loss.');
    if (selected?.when === 'delayed') {
      delayed = () => fetch(origin + path, {...init, signal: undefined});
      throw Error('Fictional delayed transport.');
    }
    const response = await fetch(origin + path, init);
    if (selected?.when === 'after') {
      assert(response.ok, await response.clone().text());
      await response.text();
      throw Error('Fictional response loss.');
    }
    if (selected?.when === 'replace') {
      const raw = await response.json();
      return new Response(JSON.stringify(selected.modify(raw)), {status: response.status, headers: {'Content-Type': 'application/json'}});
    }
    if (selected?.when === 'hold') {
      selected.enter(response.status);
      await selected.release;
    }
    return response;
  }
  function client(options = {}) {
    const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto});
    return createNativeAgentAttachmentsClient({enabled: true, demo: false, stage23Fixture: true, ...(deletion ? {deletionFixture: true} : {}),
      fetch: transport, auth: () => key, crypto: webcrypto, AbortController, journal,
      changed: s => snapshots.push(s), ...options});
  }
  return {rpc, client, requests, snapshots, storageMap, origin, setFault: value => { fault = value; },
    delayed: () => delayed(), key: value => { key = value; },
    failStorage: (kind, mode = 'throw') => { storageFault = kind === null ? null : {kind, mode}; }};
}

async function draft(f, files = [pdf()]) {
  const client = f.client();
  await client.connect(ADA);
  const state = await client.addFiles(files);
  assert.equal(state.error, null, JSON.stringify(state.error));
  assert.equal(state.draft.files.length, files.length);
  assert(state.draft.files.every(row => row.state === 'ready'));
  return client;
}
function posts(rows, pattern) {
  return rows.filter(row => row.method === 'POST' && pattern.test(row.path));
}
function onlyGets(rows) { assert(rows.length > 0 && rows.every(row => row.method === 'GET')); }

test('real mixed originals, actual copy verification and one owner handoff', async t => {
  const f = await setup(t);
  const client = await draft(f, [pdf(), png(), jpg()]);
  await f.rpc('mode', {mode: 'queued'});
  const result = await client.send('Fictional mixed request.');
  assert.equal(result.error, null, JSON.stringify(result));
  assert.equal(result.draft, null);
  assert.deepEqual(result.uploads, []);
  assert.deepEqual(result.attempts, []);
  const [row] = result.requests;
  assert.equal(row.delivery, 'queued');
  assert.equal(row.accepted, true);
  assert.equal(row.execution, 'unobserved');
  assert.deepEqual(row.files.map(f => f.savedCopyState), ['available', 'available', 'available']);
  assert.equal(result.files.associationCount, 3);
  assert(result.files.items.every(f => f.savedCopyAvailable && f.management === null));
  assert.equal((await f.rpc('counters')).boundary, 1);
  const detail = await client.openFileDetails(result.files.items[0].detailsHandle);
  assert.equal(detail.detail.owner, 'retained');
  assert.equal(detail.detail.savedCopyAvailable, true);
  assert.equal(detail.detail.previewAvailable, false);
  assert.equal(posts(f.requests, PREPARE).length, 1);
  assert.equal(posts(f.requests, SEND).length, 1);
  const privateFields = /requestCapability|ownerRequestId|writerNonce|fingerprint|\bsourceCapability\b/;
  for (const snapshot of f.snapshots) assert(!privateFields.test(JSON.stringify(snapshot)));
  for (const raw of f.storageMap.values()) assert(!/orchard\.(pdf|jpg|png)|Fictional mixed|fingerprint|writerNonce/.test(raw));
  const before = f.requests.length;
  for (const method of ['finishRequest', 'cleanupSavedCopies', 'prepareDeletion', 'openDeletion', 'confirmDeletion']) {
    assert.equal((await client[method](row.actions.check)).error.code, 'unavailable');
  }
  assert.equal(f.requests.length, before);
});

test('Check reconciles real accepted-then-unknown, execution and guarded reply facts with GET only', async t => {
  const f = await setup(t);
  const client = await draft(f);
  let result = await client.send('Fictional work.');
  const {id, actions} = result.requests[0];
  assert.equal(result.requests[0].delivery, 'pending');
  assert.equal(result.requests[0].accepted, false);
  await f.rpc('outcome', {requestId: id, kind: 'accepted'});
  const mark = f.requests.length;
  result = await client.checkRequest(actions.check);
  assert.equal(result.requests[0].delivery, 'queued');
  await f.rpc('outcome', {requestId: id, kind: 'unknown'});
  result = await client.checkRequest(actions.check);
  assert.equal(result.requests[0].delivery, 'unknown');
  assert.equal(result.requests[0].accepted, true);
  await f.rpc('outcome', {requestId: id, kind: 'ended'});
  result = await client.checkRequest(actions.check);
  assert.equal(result.requests[0].execution, 'ended');
  assert.equal(result.requests[0].reply.state, 'unknown');
  await f.rpc('outcome', {requestId: id, kind: 'reply'});
  result = await client.checkRequest(actions.check);
  assert.deepEqual(result.requests[0].reply, {state: 'available', text: 'Fictional correlated reply.'});
  onlyGets(f.requests.slice(mark));
  assert.equal((await f.rpc('counters')).boundary, 1);
});

test('lost prepare stays at its exact prepared intent and a cold client cannot execute it', async t => {
  const f = await setup(t);
  const client = await draft(f);
  f.setFault({test: path => PREPARE.test(path), when: 'after'});
  let state = await client.send('Fictional lost prepare.');
  assert.equal(state.error.code, 'network_error');
  assert.equal(state.attempts[0].phase, 'preparing');
  assert.equal(posts(f.requests, SEND).length, 0);
  client.dispose();
  const cold = f.client();
  const start = f.requests.length;
  state = await cold.connect(ADA);
  assert.equal(state.attempts.length, 1);
  assert.equal(state.attempts[0].requestId, state.requests[0].id);
  assert.equal(state.requests[0].delivery, 'not_started');
  await cold.checkRequest(state.requests[0].actions.check);
  onlyGets(f.requests.slice(start));
  assert.equal((await f.rpc('counters')).boundary, 0);
});

test('an inconclusive prepare GET cannot clear a write which arrives afterwards', async t => {
  const f = await setup(t);
  const client = await draft(f);
  f.setFault({test: path => PREPARE.test(path), when: 'delayed'});
  let state = await client.send('Fictional delayed prepare.');
  assert.equal(state.attempts[0].requestId, null);
  state = await client.refresh();
  assert.equal(state.attempts[0].requestId, null);
  assert.equal(state.requests.length, 0);
  const delayed = await f.delayed();
  assert.equal(delayed.status, 201, await delayed.text());
  const start = f.requests.length;
  state = await client.refresh();
  assert.equal(state.attempts[0].requestId, state.requests[0].id);
  assert.equal(state.requests[0].delivery, 'not_started');
  onlyGets(f.requests.slice(start));
  assert.equal(posts(f.requests, SEND).length, 0);
});

test('lost execute distinguishes prepared readback from actual durable admission across reload', async t => {
  for (const when of ['delayed', 'after']) await t.test(when, async sub => {
    const f = await setup(sub);
    const client = await draft(f);
    f.setFault({test: path => SEND.test(path), when});
    const lost = await client.send('Fictional lost execute.');
    assert.equal(lost.error.code, 'network_error');
    assert.equal(lost.attempts[0].phase, 'sending');
    assert.equal(lost.draft.files.length, 1);
    client.dispose();
    const cold = f.client();
    const start = f.requests.length;
    let state = await cold.connect(ADA);
    if (when === 'delayed') {
      assert.equal(state.requests[0].delivery, 'not_started');
      assert.equal(state.attempts.length, 1);
      const response = await f.delayed();
      assert.equal(response.status, 200, await response.text());
      state = await cold.checkRequest(state.requests[0].actions.check);
    }
    assert.equal(state.requests[0].delivery, 'pending');
    assert.deepEqual(state.attempts, []);
    onlyGets(f.requests.slice(start));
    assert.equal((await f.rpc('counters')).boundary, 1);
  });
});

test('failed journal capture/transition and rekey do not issue the next mutation', async t => {
  for (const kind of ['send_prepare', 'send_execute', 'rekey']) await t.test(kind, async sub => {
    const f = await setup(sub);
    const client = await draft(f);
    if (kind !== 'rekey') {
      f.failStorage(kind);
      const result = await client.send('Fictional journal failure.');
      assert.equal(result.error.code, 'journal_unavailable');
      assert.equal(posts(f.requests, PREPARE).length, kind === 'send_prepare' ? 0 : 1);
    } else {
      let enter, release;
      const reached = new Promise(resolve => { enter = resolve; });
      f.setFault({test: path => PREPARE.test(path), when: 'hold', enter, release: new Promise(resolve => { release = resolve; })});
      const pending = client.send('Fictional private note.');
      assert.equal(await reached, 201);
      f.key('fictional-new-key');
      assert.equal(client.snapshot().context, null);
      release();
      const stale = await pending;
      assert.equal(stale.context, null);
      assert.deepEqual(stale.requests, []);
    }
    assert.equal(posts(f.requests, SEND).length, 0);
    assert.equal((await f.rpc('counters')).boundary, 0);
  });
});

test('partial actual filesystem saving preserves the exact available sibling and charges', async t => {
  const f = await setup(t);
  const client = await draft(f, [pdf(), png()]);
  await f.rpc('mode', {mode: 'partial'});
  const result = await client.send('Fictional partial save.');
  const [request] = result.requests;
  assert.equal(request.reason, 'copy_failed');
  assert.equal(request.delivery, 'pending');
  assert.equal(request.accepted, false);
  assert.deepEqual(request.files.map(f => f.savedCopyState), ['available', 'unavailable']);
  assert.equal(result.files.associationCount, 1);
  assert.equal(result.files.unclassifiedAssociationCount, 1);
  assert.equal(result.files.state, 'incomplete');
  const counts = await f.rpc('counters');
  assert.equal(counts.boundary, 0);
  assert.equal(counts.quota, 2 * (pdf().size + png().size));
});

test('all three read paths withdraw copy availability without modifying charges or history', async t => {
  for (const kind of ['content', 'inode', 'unlink', 'parent', 'context']) await t.test(kind, async sub => {
    const f = await setup(sub);
    const client = await draft(f, [pdf(), png()]);
    const result = await client.send('Fictional physical copy.');
    const [row] = result.requests;
    const file = result.files.items.find(f => f.name === 'orchard.pdf');
    assert.equal(file.savedCopyAvailable, true);
    assert.equal((await client.openFileDetails(file.detailsHandle)).detail.savedCopyAvailable, true);
    const before = await f.rpc('counters');
    await f.rpc('damage', {requestId: row.id, kind});
    const first = f.requests.length;
    const exact = await client.checkRequest(row.actions.check);
    assert(['unavailable', 'unknown'].includes(exact.requests[0].files[0].savedCopyState));
    if (['content', 'inode', 'unlink'].includes(kind)) assert.equal(exact.requests[0].files[1].savedCopyState, 'available');
    const detail = await client.openFileDetails(file.detailsHandle);
    assert.equal(detail.detail.savedCopyAvailable, false);
    const page = await client.filesPage(detail.files.first);
    assert.equal(page.files.associationCount, 2);
    assert.equal(page.files.items.find(f => f.id === file.id).savedCopyAvailable, false);
    const after = await f.rpc('counters');
    assert.equal(after.quota, before.quota);
    assert.equal(after.boundary, before.boundary);
    onlyGets(f.requests.slice(first));
  });
});

test('retained scope, details handle, flags and protocol are closed', async t => {
  const f = await setup(t);
  const defaultClient = f.client({stage23Fixture: false});
  assert.equal((await defaultClient.connect(ADA)).error.code, 'invalid_response');
  const client = await draft(f);
  const s = await client.send('Fictional scope.');
  const latest = s.requests[0];
  const url = LIST() + '&scopeId=foreign';
  let raw = await fetch(f.origin + url, {headers: {'X-Live-Room-Key': KEY}});
  assert.equal(raw.status, 400);
  raw = await fetch(f.origin + EXACT(latest.id), {headers: {'X-Live-Room-Key': 'not-owner'}});
  assert.equal(raw.status, 401);
  const old = s.files.items[0].detailsHandle;
  await client.connect(BRAM);
  const stale = await client.openFileDetails(old);
  assert.equal(stale.detail, null);
  assert.deepEqual(stale.requests, []);
  assert.equal(stale.features.send, false);
});

test('committed intent without a mirror slot confers no saved request authority', async t => {
  const f = await setup(t);
  const client = await draft(f);
  await f.rpc('mode', {mode: 'mirror_slot'});
  let state = await client.send('Fictional limbo.');
  assert.equal(state.attempts[0].phase, 'preparing');
  assert.equal(state.attempts[0].requestId, null);
  assert.equal((await f.rpc('counters')).intents, 1);
  const before = f.requests.length;
  state = await client.refresh();
  assert.equal(state.attempts[0].requestId, null);
  assert.equal(state.requests.length, 0);
  assert.equal((await f.rpc('counters')).boundary, 0);
  onlyGets(f.requests.slice(before));
});

test('post-verification generation changes withhold positivity', async t => {
  const f = await setup(t);
  const client = await draft(f, [pdf(), png()]);
  let state = await client.send('Fictional generation drift.');
  const [row] = state.requests;
  assert.deepEqual(row.files.map(f => f.savedCopyState), ['available', 'available']);
  const base = await f.rpc('counters');
  await f.rpc('drift', {requestId: row.id});
  state = await client.checkRequest(row.actions.check);
  assert.equal(state.requests[0].files[0].savedCopyState, 'unknown');
  assert.equal(state.requests[0].files[1].savedCopyState, 'available');
  assert.equal((await f.rpc('counters')).quota, base.quota);
});

test('UTF-8 page cap, signed short continuations, and exact known GET beyond client traversal', async t => {
  const f = await setup(t);
  const roster = [pdf('a'.repeat(251) + '.pdf'), png('b'.repeat(251) + '.png'), jpg('c'.repeat(251) + '.jpg'),
    pdf('d'.repeat(251) + '.pdf'), jpg('e'.repeat(251) + '.jpg')];
  const first = await draft(f, roster);
  const submitted = 'T'.repeat(2000);
  let state = await first.send(submitted);
  const astral = state.requests[0];
  await f.rpc('outcome', {requestId: astral.id, kind: 'astral'});
  const second = await draft(f);
  state = await second.send('Fictional long escaped reply.');
  const escaped = state.requests.find(item => item.id !== astral.id);
  await f.rpc('outcome', {requestId: escaped.id, kind: 'escaped'});
  await f.rpc('seed', {count: 42, kind: 'escaped'});
  const headers = {'X-Live-Room-Key': KEY};
  const all = new Set(), cursors = new Set();
  let cursor = null, pages = 0, earlyCursor = null;
  do {
    const response = await fetch(f.origin + LIST(cursor), {headers});
    assert.equal(response.status, 200, await response.clone().text().then(text => text.slice(0, 120)));
    const bytes = Buffer.from(await response.arrayBuffer());
    assert(bytes.length <= 262144);
    assert.equal(Number(response.headers.get('Content-Length')), bytes.length);
    const page = JSON.parse(bytes.toString('utf8'));
    assert(page.items.length > 0 && page.items.length < 50);
    for (const item of page.items) {
      assert(!all.has(item.request.id));
      all.add(item.request.id);
    }
    cursor = page.nextCursor;
    if (cursor !== null) {
      earlyCursor ??= cursor;
      assert(!cursors.has(cursor));
      cursors.add(cursor);
    }
    pages += 1;
  } while (cursor !== null && pages < 60);
  assert.equal(cursor, null);
  assert.equal(all.size, 44);
  assert(pages > 20);
  const forgedScope = await fetch(f.origin + REQUEST + '?' + new URLSearchParams({...BRAM, scopeId: 'fictional-grove', cursor: earlyCursor}), {headers});
  assert.equal(forgedScope.status, 403);
  for (const [row, body] of [[astral, '🪴'.repeat(16000)], [escaped, '\u0003'.repeat(16000)]]) {
    const response = await fetch(f.origin + EXACT(row.id), {headers});
    assert.equal(response.status, 200);
    const bytes = Buffer.from(await response.arrayBuffer());
    assert(bytes.length <= 124162);
    const parsed = JSON.parse(bytes.toString('utf8')).observation.request;
    assert.equal(parsed.reply.text, body);
    if (row.id === astral.id) {
      assert.equal(parsed.text, submitted);
      assert.equal(parsed.files.length, 5);
    }
  }
  const start = f.requests.length;
  state = await second.refresh();
  assert.equal(state.error.code, 'requests_incomplete');
  assert.equal(state.requests.every(row => row.reply.text === null), true);
  assert.equal(f.requests.slice(start).filter(row => row.path.startsWith(REQUEST + '?')).length, 20);
  state = await second.checkRequest(state.requests.find(row => row.id === escaped.id).actions.check);
  assert.equal(state.requests.find(row => row.id === escaped.id).reply.text, '\u0003'.repeat(16000));
  onlyGets(f.requests.slice(start));
});

test('real retained Files pages revoke stale handles and recover the first page after cursor refusal', async t => {
  const f = await setup(t);
  await f.rpc('seed', {count: 51, kind: 'catalogue'});
  const client = f.client();
  let state = await client.connect(ADA);
  assert.equal(state.files.state, 'complete');
  assert.equal(state.files.associationCount, 51);
  assert.equal(state.files.items.length, 50);
  const firstHandles = state.files.items.map(r => r.detailsHandle);
  const accepted = state.files.next;
  const beforeForgedCursor = f.requests.length;
  assert.equal((await client.filesPage('fictional-forged-cursor')).error.code, 'invalid_request');
  assert.equal(f.requests.length, beforeForgedCursor);
  state = await client.filesPage(accepted);
  assert.equal(state.files.items.length, 1);
  assert.equal(state.files.associationCount, 51);
  assert.equal(state.files.next, null);
  const count = f.requests.length;
  await client.openFileDetails(firstHandles[0]);
  assert.equal(f.requests.length, count);
  state = await client.filesPage(state.files.previous);
  assert.equal(state.files.items.length, 50);
  f.setFault({test: path => /\/files\?/.test(path), when: 'before'});
  state = await client.filesPage(state.files.next);
  assert.equal(state.files.state, 'incomplete');
  assert.equal(state.files.associationCount, null);
  assert.equal(state.files.items.length, 0);
  assert(state.files.first);
  state = await client.filesPage(state.files.first);
  assert.equal(state.files.items.length, 50);
  assert.equal(state.files.associationCount, 51);
  onlyGets(f.requests);
});

test('a scope switch during acknowledged execution withdraws every old request handle', async t => {
  const f = await setup(t);
  const client = await draft(f);
  let enter, release;
  const reached = new Promise(resolve => { enter = resolve; });
  f.setFault({test: path => SEND.test(path), when: 'hold', enter, release: new Promise(resolve => { release = resolve; })});
  const pending = client.send('Fictional withdrawn scope text.');
  assert.equal(await reached, 200);
  const mark = f.requests.length;
  let state = await client.connect(BRAM);
  assert.equal(state.context.destinationId, BRAM.destinationId);
  assert.deepEqual(state.requests, []);
  assert.deepEqual(state.attempts, []);
  release();
  state = await pending;
  assert.equal(state.context.destinationId, BRAM.destinationId);
  assert(!JSON.stringify(state).includes('Fictional withdrawn'));
  state = await client.connect(ADA);
  assert.equal(state.requests[0].text, 'Fictional withdrawn scope text.');
  assert.deepEqual(state.attempts, []);
  onlyGets(f.requests.slice(mark));
  assert.equal((await f.rpc('counters')).boundary, 1);
});

test('one-shot grant rejects a duplicate execute even after proven non-submission', async t => {
  const f = await setup(t);
  const client = await draft(f);
  await f.rpc('mode', {mode: 'not_submitted'});
  const state = await client.send('Fictional verified non-submission.');
  const row = state.requests[0];
  assert.equal(row.delivery, 'not_submitted');
  assert.equal(row.reason, 'proven_not_submitted');
  assert.equal(row.accepted, false);
  const [sent] = posts(f.requests, SEND);
  const repeat = await fetch(f.origin + sent.path, {method: 'POST', headers: sent.headers, body: sent.body});
  assert.equal(repeat.status, 409);
  assert.equal((await f.rpc('counters')).boundary, 1);
});

test('an old-epoch unclassified association cannot become a new owner Files count', async t => {
  const f = await setup(t);
  const client = await draft(f);
  f.setFault({test: path => PREPARE.test(path), when: 'after'});
  await client.send('Fictional old scope.');
  let state = await client.refresh();
  assert.equal(state.files.unclassifiedAssociationCount, 1);
  const row = state.requests[0];
  await f.rpc('retire_epoch', {requestId: row.id});
  state = await client.refresh();
  assert.equal(state.files.state, 'incomplete');
  assert.equal(state.files.associationCount, null);
  assert.equal(state.files.unclassifiedAssociationCount, null);
  assert.equal(state.attempts.length, 1);
});

test('closing retained details during its GET does not reopen them on late response', async t => {
  const f = await setup(t);
  const client = await draft(f);
  let state = await client.send('Fictional details.');
  let enter, release;
  const reached = new Promise(resolve => { enter = resolve; });
  f.setFault({test: path => /\/details\?/.test(path), when: 'hold', enter, release: new Promise(resolve => { release = resolve; })});
  const pending = client.openFileDetails(state.files.items[0].detailsHandle);
  assert.equal(await reached, 200);
  assert.equal((await client.closeFileDetails()).detail, null);
  release();
  assert.equal((await pending).detail, null);
});

async function deletionCase(t, count = 1) {
  const f = await setup(t, {deletion: true});
  const {seeded} = await f.rpc('seed', {count, kind: 'catalogue'});
  const client = f.client();
  const state = await client.connect(ADA);
  assert.equal(state.error, null, JSON.stringify(state.error));
  assert.deepEqual(state.features, {drafts: false, send: false, files: true, deletion: true});
  assert.equal(state.files.associationCount, count);
  assert.equal(state.draft, null);
  assert.deepEqual(state.requests, []);
  return {f, client, files: seeded};
}

function fileFor(client, file) {
  const row = client.snapshot().files.items.find(row => row.id === file.fileId && row.requestId === file.requestId);
  assert(row, 'the exact retained association must be visible');
  return row;
}

async function prepareRetained(client, file) {
  const handle = fileFor(client, file).management?.deleteHandle;
  assert(handle, 'the accepted management row must authorize Prepare');
  const value = await client.prepareDeletion(handle);
  assert.equal(value.error, null, JSON.stringify(value.error));
  assert.equal(value.deletion?.state, 'confirm');
  assert(value.deletion.confirmHandle);
  return value.deletion;
}

async function openRetained(client, file) {
  const handle = fileFor(client, file).management?.operationHandle;
  assert(handle, 'the accepted management row must identify its operation');
  const value = await client.openDeletion(handle);
  assert.equal(value.error, null, JSON.stringify(value.error));
  assert(value.deletion);
  assert.equal(value.deletion.confirmHandle, null);
  return value.deletion;
}

function deletionHolds(f) {
  return [...f.storageMap.values()].flatMap(raw => JSON.parse(raw)).filter(row => row.kind.startsWith('deletion_'));
}

test('native deletion uses real Files receipts and one fresh Confirm gesture performs one Execute', async t => {
  const {f, client, files: [target, neighbor]} = await deletionCase(t, 2);
  const initial = fileFor(client, target);
  assert.equal(initial.savedCopyState, 'unknown');
  assert.equal(initial.savedCopyAvailable, false);
  const prepared = await prepareRetained(client, target);
  assert.deepEqual(prepared.copies.map(p => [p.state, p.chargedBytes, p.releasedBytes]),
    [['pending', initial.bytes, 0], ['pending', initial.bytes, 0]]);
  assert.equal(prepared.sourceUnavailable, false);
  const between = await f.rpc('counters');
  assert.deepEqual([between.prepare, between.confirm, between.execute], [1, 0, 0]);
  const after = await client.confirmDeletion(prepared.confirmHandle);
  assert.equal(after.error, null, JSON.stringify(after.error));
  assert.equal(after.deletion?.state, 'deleted');
  assert.equal(after.deletion.releasedBytes, 2 * initial.bytes);
  assert.deepEqual(after.deletion.copies.map(p => [p.state, p.chargedBytes, p.releasedBytes]),
    [['removed', 0, initial.bytes], ['removed', 0, initial.bytes]]);
  assert.equal(after.deletion.sourceUnavailable, true);
  assert.equal(after.files.associationCount, 2);
  assert.equal(fileFor(client, target).savedCopyState, 'removed');
  assert.equal(fileFor(client, neighbor).savedCopyState, 'unknown');
  assert(fileFor(client, neighbor).management.deleteHandle);
  assert.equal(fileFor(client, target).management.deleteHandle, null);
  assert.deepEqual(deletionHolds(f), []);
  const confirm = posts(f.requests, DELETE_CONFIRM)[0];
  const execute = posts(f.requests, DELETE_EXECUTE)[0];
  assert(confirm.headers['X-Attachment-Deletion-Capability']);
  assert.equal(execute.headers['X-Attachment-Deletion-Capability'], undefined);
  assert.equal(execute.headers['Idempotency-Key'], undefined);
  for (const snapshot of f.snapshots) {
    assert(!JSON.stringify(snapshot).includes(confirm.headers['X-Attachment-Deletion-Capability']));
    assert(!JSON.stringify(snapshot).includes(JSON.parse(confirm.body).fingerprint));
  }
  await client.confirmDeletion(prepared.confirmHandle);
  assert.equal(posts(f.requests, DELETE_CONFIRM).length, 1);
  assert.equal(posts(f.requests, DELETE_EXECUTE).length, 1);
  const retained = await client.openFileDetails(fileFor(client, target).detailsHandle);
  assert.equal(retained.detail?.fileId, target.fileId);
  assert.equal(retained.detail?.savedCopyAvailable, false);
  const counts = await f.rpc('counters');
  assert.deepEqual([counts.prepare, counts.confirm, counts.execute, counts.send, counts.boundary], [1, 1, 1, 0, 0]);
});

test('lost deletion Prepare identifies its exact preview through GET and cannot recover a grant', async t => {
  for (const when of ['delayed', 'after']) await t.test(when, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    f.setFault({test: path => DELETE_PREPARE.test(path), when});
    const interrupted = await client.prepareDeletion(fileFor(client, target).management.deleteHandle);
    assert.equal(interrupted.error.code, 'network_error');
    assert.equal(interrupted.deletion, null);
    const [hold] = deletionHolds(f);
    assert.deepEqual(Object.keys(hold), ['version', 'attemptId', 'kind', 'fileId', 'managementGeneration', 'commandDigest']);
    assert.equal(hold.fileId, target.fileId);
    assert.equal(hold.kind, 'deletion_prepare');
    assert.match(hold.commandDigest, /^[0-9a-f]{64}$/);
    assert.notEqual(hold.commandDigest, posts(f.requests, DELETE_PREPARE)[0].headers['Idempotency-Key']);
    client.dispose();
    const warm = f.client(), start = f.requests.length;
    let state = await warm.connect(ADA);
    if (when === 'delayed') {
      assert.equal(state.attempts[0].phase, 'preparing_deletion');
      assert.equal(fileFor(warm, target).management.deleteHandle, null);
      assert.deepEqual(deletionHolds(f), [hold]);
      assert.equal((await f.rpc('counters')).prepare, 0);
      const release = await f.delayed();
      assert.equal(release.status, 201, await release.text());
      state = await warm.refresh();
    }
    assert.deepEqual(state.attempts, []);
    assert.equal(state.deletion, null);
    const read = await openRetained(warm, target);
    assert.equal(read.state, 'confirm');
    assert.deepEqual(read.actions, {execute: null, recover: null});
    onlyGets(f.requests.slice(start));
    assert.deepEqual(deletionHolds(f), []);
    assert.equal(posts(f.requests, DELETE_PREPARE).length, 1);
    assert.equal(posts(f.requests, DELETE_CONFIRM).length, 0);
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
    const newPreview = await prepareRetained(warm, target);
    assert.equal(newPreview.id, read.id);
    assert.equal(newPreview.revision, read.revision + 1);
  });
});

test('lost Confirm reconciles committed revocation without Execute until explicit continuation', async t => {
  for (const when of ['delayed', 'after']) await t.test(when, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    const prepared = await prepareRetained(client, target);
    f.setFault({test: path => DELETE_CONFIRM.test(path), when});
    const lost = await client.confirmDeletion(prepared.confirmHandle);
    assert.equal(lost.error.code, 'network_error');
    assert.equal(lost.deletion.confirmHandle, null);
    assert.equal(lost.attempts[0].phase, 'confirming_deletion');
    const [hold] = deletionHolds(f);
    assert.deepEqual(Object.keys(hold), ['version', 'attemptId', 'kind', 'fileId', 'operationId', 'previewGeneration', 'revision', 'commandDigest']);
    assert.equal(hold.operationId, prepared.id);
    const request = posts(f.requests, DELETE_CONFIRM)[0];
    assert(!JSON.stringify(hold).includes(request.headers['X-Attachment-Deletion-Capability']));
    assert(!JSON.stringify(hold).includes(JSON.parse(request.body).fingerprint));
    client.dispose();
    const warm = f.client(), start = f.requests.length;
    let state = await warm.connect(ADA);
    if (when === 'delayed') {
      assert.deepEqual(deletionHolds(f), [hold]);
      const old = await openRetained(warm, target);
      assert.equal(old.state, 'confirm');
      assert.deepEqual(old.actions, {execute: null, recover: null});
      assert.equal((await f.rpc('counters')).confirm, 0);
      const release = await f.delayed();
      assert.equal(release.status, 200, await release.text());
      state = await warm.refresh();
    }
    assert.deepEqual(state.attempts, []);
    const read = await openRetained(warm, target);
    assert.equal(read.state, 'deleting');
    assert.equal(read.sourceUnavailable, true);
    assert.equal(read.releasedBytes, 0);
    assert(read.actions.execute);
    assert.equal(read.actions.recover, null);
    assert.equal(fileFor(warm, target).management.deleteHandle, null);
    onlyGets(f.requests.slice(start));
    assert.equal(posts(f.requests, DELETE_CONFIRM).length, 1);
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
    const outcome = await warm.executeDeletion(read.actions.execute);
    assert.equal(outcome.error, null, JSON.stringify(outcome.error));
    assert.equal(outcome.deletion.state, 'deleted');
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 1);
  });
});

test('lost Execute holds the exact unkeyed starting fence until newer physical evidence', async t => {
  for (const when of ['delayed', 'after']) await t.test(when, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    const prepared = await prepareRetained(client, target);
    f.setFault({test: path => DELETE_EXECUTE.test(path), when});
    const lost = await client.confirmDeletion(prepared.confirmHandle);
    assert.equal(lost.error.code, 'network_error');
    assert.equal(lost.attempts[0].phase, 'executing_deletion');
    const [hold] = deletionHolds(f);
    assert.deepEqual(Object.keys(hold), ['version', 'attemptId', 'kind', 'fileId', 'operationId', 'previewGeneration', 'revision', 'mode']);
    assert.equal(hold.kind, 'deletion_execute');
    assert.equal(hold.revision, prepared.revision + 1);
    client.dispose();
    const warm = f.client(), start = f.requests.length;
    let state = await warm.connect(ADA);
    if (when === 'delayed') {
      assert.deepEqual(deletionHolds(f), [hold]);
      const read = await openRetained(warm, target);
      assert.equal(read.revision, hold.revision);
      assert.equal(read.releasedBytes, 0);
      assert.deepEqual(read.actions, {execute: null, recover: null});
      const release = await f.delayed();
      assert.equal(release.status, 200, await release.text());
      const duplicate = await f.delayed();
      assert.equal(duplicate.status, 409, await duplicate.text());
      state = await warm.refresh();
    }
    assert.deepEqual(state.attempts, []);
    const outcome = await openRetained(warm, target);
    assert(outcome.revision > hold.revision);
    assert.equal(outcome.state, 'deleted');
    assert.equal(fileFor(warm, target).savedCopyState, 'removed');
    onlyGets(f.requests.slice(start));
    const counts = await f.rpc('counters');
    assert.deepEqual([counts.confirm, counts.execute], [1, 1]);
  });
});

test('old exact deletion preview cannot confirm a newer slot generation from another tab', async t => {
  const {f, client, files: [target]} = await deletionCase(t);
  const first = await prepareRetained(client, target);
  const peer = f.client();
  await peer.connect(ADA);
  const second = await prepareRetained(peer, target);
  assert.equal(first.id, second.id);
  assert.equal(second.revision, first.revision + 1);
  const refused = await client.confirmDeletion(first.confirmHandle);
  assert(refused.error);
  assert.equal(refused.attempts[0].phase, 'confirming_deletion');
  assert.equal((await f.rpc('counters')).confirm, 0);
  const start = f.requests.length;
  const refreshed = await client.refresh();
  assert.deepEqual(refreshed.attempts, []);
  assert.equal(refreshed.deletion, null);
  const reopened = await openRetained(client, target);
  assert.equal(reopened.revision, second.revision);
  assert.equal(reopened.state, 'confirm');
  assert.equal(reopened.confirmHandle, null);
  onlyGets(f.requests.slice(start));
  await client.confirmDeletion(first.confirmHandle);
  assert.equal(posts(f.requests, DELETE_CONFIRM).length, 1);
  const success = await peer.confirmDeletion(second.confirmHandle);
  assert.equal(success.deletion?.state, 'deleted', JSON.stringify(success.error));
});

test('deletion journal verifies capture and transition before each possible POST', async t => {
  const kinds = ['deletion_prepare', 'deletion_confirm', 'deletion_execute', 'explicit_execute'];
  const faults = kinds.flatMap(kind => ['throw', 'drop'].map(mode => [kind, mode]));
  for (const [kind, mode] of faults) await t.test(kind + ' ' + mode, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    if (kind === 'deletion_prepare') {
      f.failStorage(kind, mode);
      const state = await client.prepareDeletion(fileFor(client, target).management.deleteHandle);
      assert.equal(state.error.code, 'journal_unavailable');
      assert.equal(posts(f.requests, DELETE_PREPARE).length, 0);
      return;
    }
    const prepared = await prepareRetained(client, target);
    let outcome;
    if (kind === 'explicit_execute') {
      f.setFault({test: path => DELETE_CONFIRM.test(path), when: 'after'});
      await client.confirmDeletion(prepared.confirmHandle);
      await client.refresh();
      const view = await openRetained(client, target);
      f.failStorage('deletion_execute', mode);
      outcome = await client.executeDeletion(view.actions.execute);
    } else {
      f.failStorage(kind, mode);
      outcome = await client.confirmDeletion(prepared.confirmHandle);
    }
    assert.equal(outcome.error?.code, 'journal_unavailable');
    assert.equal(posts(f.requests, DELETE_CONFIRM).length, kind === 'deletion_confirm' ? 0 : 1);
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
    const counts = await f.rpc('counters');
    assert.equal(counts.confirm, kind === 'deletion_confirm' ? 0 : 1);
    assert.equal(counts.execute, 0);
    if (kind === 'deletion_execute') {
      assert.equal(deletionHolds(f)[0].kind, 'deletion_confirm');
      assert.equal(outcome.deletion.sourceUnavailable, true);
    }
    f.failStorage(null);
    const start = f.requests.length;
    await client.refresh();
    onlyGets(f.requests.slice(start));
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
  });
});

function delayedAcknowledgement(f, pattern) {
  let enter, release;
  const entered = new Promise(resolve => { enter = resolve; });
  const wait = new Promise(resolve => { release = resolve; });
  f.setFault({test: path => pattern.test(path), when: 'hold', enter, release: wait});
  return {entered, release};
}

test('deletion Confirm acknowledgment cannot cross owner or destination changes and Close stays presentation-only', async t => {
  for (const change of ['owner', 'destination', 'close']) await t.test(change, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    const prepared = await prepareRetained(client, target);
    const paused = delayedAcknowledgement(f, DELETE_CONFIRM);
    const pending = client.confirmDeletion(prepared.confirmHandle);
    try {
      assert.equal(await paused.entered, 200);
      assert.equal((await f.rpc('counters')).execute, 0);
      if (change === 'owner') {
        f.key('fictional-replacement-owner');
        assert.equal(client.snapshot().context, null);
      } else if (change === 'destination') {
        await client.connect(BRAM);
        assert.equal(client.snapshot().features.deletion, false);
      } else {
        const closed = await client.closeDeletion();
        assert.equal(closed.deletion, null);
        assert.equal(closed.attempts[0].phase, 'confirming_deletion');
      }
    } finally { paused.release(); }
    const result = await pending;
    assert.equal(result.deletion, null);
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, change === 'close' ? 1 : 0);
    if (change !== 'close') {
      f.key(KEY);
      const warm = f.client(), start = f.requests.length;
      await warm.connect(ADA);
      const view = await openRetained(warm, target);
      assert.equal(view.state, 'deleting');
      assert(view.actions.execute);
      onlyGets(f.requests.slice(start));
    }
  });
});

test('an unproven fresh bit or inconsistent D1 accounting never starts physical deletion', async t => {
  for (const variant of ['replayed', 'wrong-revision', 'malformed-release']) await t.test(variant, async sub => {
    const {f, client, files: [target]} = await deletionCase(sub);
    const prepared = await prepareRetained(client, target);
    f.setFault({test: path => DELETE_CONFIRM.test(path), when: 'replace', modify(raw) {
      if (variant === 'replayed') raw.confirmedNow = false;
      if (variant === 'wrong-revision') raw.operation.revision += 1;
      if (variant === 'malformed-release') {
        raw.operation.pieces[0].chargedBytes -= 1;
        raw.operation.pieces[0].releasedBytes = 1;
        raw.operation.releasedBytes = 1;
      }
      return raw;
    }});
    const result = await client.confirmDeletion(prepared.confirmHandle);
    assert.equal(result.error?.code ?? null, variant === 'replayed' ? null : 'invalid_response');
    assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
    assert.equal((await f.rpc('counters')).confirm, 1);
    const start = f.requests.length;
    await client.refresh();
    const restored = await openRetained(client, target);
    assert.equal(restored.state, 'deleting');
    assert.equal(restored.releasedBytes, 0);
    assert(restored.actions.execute);
    onlyGets(f.requests.slice(start));
  });
});

test('real native deletion keeps changed or unresolved workspace copies charged and exact history intact', async t => {
  for (const kind of ['content', 'resolver_unavailable']) await t.test(kind, async sub => {
    const {f, client, files: [target, neighbor]} = await deletionCase(sub, 2);
    const size = fileFor(client, target).bytes;
    const prepared = await prepareRetained(client, target);
    await f.rpc('damage', {requestId: target.requestId, kind});
    const result = await client.confirmDeletion(prepared.confirmHandle);
    assert.equal(result.error, null, JSON.stringify(result.error));
    assert.equal(result.deletion.state, 'partial_failure');
    assert.equal(result.deletion.releasedBytes, size);
    assert.deepEqual(result.deletion.copies, [
      {kind: 'watch_original', state: 'removed', bytes: size, chargedBytes: 0, releasedBytes: size, reason: null},
      {kind: 'workspace', state: kind === 'content' ? 'changed' : 'unknown', bytes: size, chargedBytes: size,
        releasedBytes: 0, reason: kind === 'content' ? 'identity_changed' : 'workspace_unavailable'},
    ]);
    assert.deepEqual(result.deletion.actions, {execute: null, recover: null});
    assert.equal(result.files.associationCount, 2);
    assert.equal(fileFor(client, target).savedCopyState, 'unknown');
    assert.equal(fileFor(client, target).management.deleteHandle, null);
    assert(fileFor(client, neighbor).management.deleteHandle);
    const start = f.requests.length;
    await client.refresh();
    const current = await openRetained(client, target);
    assert.deepEqual(current.copies, result.deletion.copies);
    const detail = await client.openFileDetails(fileFor(client, target).detailsHandle);
    assert.equal(detail.detail?.savedCopyAvailable, false);
    onlyGets(f.requests.slice(start));
    const counters = await f.rpc('counters');
    assert.deepEqual([counters.confirm, counters.execute, counters.send, counters.boundary], [1, 1, 0, 0]);
  });
});

test('same-file deletion contention is held while another retained association can proceed', async t => {
  const {f, client, files: [target, neighbor]} = await deletionCase(t, 2);
  const peer = f.client();
  await peer.connect(ADA);
  const oldTarget = fileFor(peer, target).management.deleteHandle;
  const paused = delayedAcknowledgement(f, DELETE_PREPARE);
  const pending = client.prepareDeletion(fileFor(client, target).management.deleteHandle);
  try {
    assert.equal(await paused.entered, 201);
    const refused = await peer.prepareDeletion(oldTarget);
    assert.equal(refused.error.code, 'journal_conflict');
    assert.equal(posts(f.requests, DELETE_PREPARE).length, 1);
    const other = await prepareRetained(peer, neighbor);
    assert.equal(other.fileId, neighbor.fileId);
    assert.equal(fileFor(peer, target).management.deleteHandle, null);
    assert(deletionHolds(f).some(record => record.fileId === target.fileId && record.kind === 'deletion_prepare'));
  } finally { paused.release(); }
  const acknowledged = await pending;
  assert.equal(acknowledged.deletion?.fileId, target.fileId);
  assert(acknowledged.deletion.confirmHandle);
  assert.deepEqual(deletionHolds(f), []);
  assert.equal((await f.rpc('counters')).prepare, 2);
});

test('closing a deletion Prepare or Open wait never reopens or recreates an authority', async t => {
  const {f, client, files: [target]} = await deletionCase(t);
  const prepareWait = delayedAcknowledgement(f, DELETE_PREPARE);
  const pending = client.prepareDeletion(fileFor(client, target).management.deleteHandle);
  try {
    assert.equal(await prepareWait.entered, 201);
    await client.closeDeletion();
  } finally { prepareWait.release(); }
  assert.equal((await pending).deletion, null);
  assert.deepEqual(deletionHolds(f), []);
  const operationHandle = fileFor(client, target).management.operationHandle;
  const openWait = delayedAcknowledgement(f, /\/deletions\/[a-f0-9]{32}\?/);
  const opening = client.openDeletion(operationHandle);
  try {
    assert.equal(await openWait.entered, 200);
    await client.closeDeletion();
  } finally { openWait.release(); }
  assert.equal((await opening).deletion, null);
  assert.equal(posts(f.requests, DELETE_CONFIRM).length, 0);
  assert.equal(posts(f.requests, DELETE_EXECUTE).length, 0);
});

test('native deletion preserves actual catalogue counts and exact management joins across pages', async t => {
  const f = await setup(t, {deletion: true});
  const {seeded} = await f.rpc('seed', {count: 51, kind: 'catalogue'});
  await f.rpc('seed', {count: 1, kind: 'unclassified'});
  const client = f.client();
  const first = await client.connect(ADA);
  assert.equal(first.error, null, JSON.stringify(first.error));
  assert.equal(first.files.associationCount, 51);
  assert.equal(first.files.unclassifiedAssociationCount, 1);
  assert.equal(first.files.items.length, 50);
  assert(first.files.next);
  assert(first.files.items.every(row => row.management?.deleteHandle && !row.savedCopyAvailable));
  assert.equal(f.requests.filter(r => r.path.includes('/native/files/management?')).length, 2);
  const stale = first.files.items[0].management.deleteHandle;
  const second = await client.filesPage(first.files.next);
  assert.equal(second.files.items.length, 1);
  assert(second.files.previous);
  assert.equal(second.files.items[0].id, seeded[50].fileId);
  assert(second.files.items[0].management.deleteHandle);
  await client.prepareDeletion(stale);
  assert.equal(posts(f.requests, DELETE_PREPARE).length, 0);
  const last = await prepareRetained(client, seeded[50]);
  const result = await client.confirmDeletion(last.confirmHandle);
  assert.equal(result.deletion?.state, 'deleted', JSON.stringify(result.error));
  assert.deepEqual([result.files.associationCount, result.files.unclassifiedAssociationCount], [51, 1]);
  const reread = await client.filesPage(result.files.next);
  assert.equal(reread.files.items[0].savedCopyState, 'removed');
  assert.equal(reread.files.items[0].management.deleteHandle, null);
  assert.equal(reread.files.items[0].id, seeded[50].fileId);
});

test('dedicated deletion routes refuse foreign owner, scope, method and hidden writes before a kernel call', async t => {
  const {f, client, files: [target]} = await deletionCase(t);
  const path = '/api/attachments/native/files/' + target.fileId + '/deletion-intent';
  const base = {destinationId: ADA.destinationId, scopeId: SCOPE, managementGeneration: 0};
  for (const [status, body, headers] of [
    [401, base, {'X-Live-Room-Key': 'fictional-other-owner'}],
    [403, base, {Origin: 'http://fictional.invalid'}],
    [403, {...base, scopeId: 'fictional-grove'}, {}],
    [400, {...base, path: 'fictional-untrusted'}, {}],
    [400, base, {'X-Attachment-Request-Capability': 'c'.repeat(64)}],
  ]) {
    const response = await fetch(f.origin + path, {method: 'POST', headers: {'X-Live-Room-Key': KEY,
      'Idempotency-Key': webcrypto.randomUUID(), 'Content-Type': 'application/json', ...headers}, body: JSON.stringify(body)});
    assert.equal(response.status, status, await response.text());
  }
  const method = await fetch(f.origin + path, {headers: {'X-Live-Room-Key': KEY}});
  assert.equal(method.status, 405, await method.text());
  const unsupported = f.client({deletionFixture: false});
  const first = f.requests.length;
  const denied = await unsupported.connect(ADA);
  assert.equal(denied.context, null);
  assert.equal(denied.error.code, 'invalid_response');
  assert.equal(f.requests.length, first + 1);
  await client.send('Fictional refused native message.');
  await client.createDraft();
  assert.equal(posts(f.requests, SEND).length, 0);
  const counts = await f.rpc('counters');
  assert.deepEqual([counts.prepare, counts.confirm, counts.execute, counts.send, counts.boundary], [0, 0, 0, 0, 0]);
});

test('an exchanged Prepare receipt for a newer preview cannot settle the original digest hold', async t => {
  const {f, client, files: [target]} = await deletionCase(t);
  f.setFault({test: path => DELETE_PREPARE.test(path), when: 'after'});
  await client.prepareDeletion(fileFor(client, target).management.deleteHandle);
  const [held] = deletionHolds(f);
  const newer = await fetch(f.origin + '/api/attachments/native/files/' + target.fileId + '/deletion-intent', {
    method: 'POST', headers: {'X-Live-Room-Key': KEY, 'Idempotency-Key': webcrypto.randomUUID(), 'Content-Type': 'application/json'},
    body: JSON.stringify({destinationId: ADA.destinationId, scopeId: SCOPE, managementGeneration: held.managementGeneration + 1}),
  });
  assert.equal(newer.status, 201);
  const {operation} = await newer.json();
  f.setFault({test: path => path.includes('/native/receipts/deletion_prepare?'), when: 'replace', modify(raw) {
    assert.equal(raw.match, null);
    raw.match = {operationId: operation.id, previewGeneration: operation.previewGeneration};
    return raw;
  }});
  const cold = f.client(), start = f.requests.length;
  const observed = await cold.connect(ADA);
  assert.equal(observed.error?.code, 'deletion_incomplete');
  assert.equal(observed.deletion, null);
  assert.deepEqual(deletionHolds(f), [held]);
  assert.equal(fileFor(cold, target).management.deleteHandle, null);
  onlyGets(f.requests.slice(start));
  assert.equal((await f.rpc('counters')).execute, 0);
});

test('pending-source occupancy hints can change on the same real deletion revision', async t => {
  const {f, client, files: [target]} = await deletionCase(t);
  const prepared = await prepareRetained(client, target);
  f.setFault({test: path => DELETE_CONFIRM.test(path), when: 'after'});
  await client.confirmDeletion(prepared.confirmHandle);
  await client.refresh();
  const pending = await openRetained(client, target);
  const start = f.requests.length;
  f.setFault({test: path => /\/deletions\/[a-f0-9]{32}\?/.test(path), when: 'replace', modify(raw) {
    assert.equal(raw.operation.revision, pending.revision);
    assert.equal(raw.operation.pieces[0].state, 'pending');
    raw.operation.pieces[0].reason = 'source_in_use';
    return raw;
  }});
  const busy = await client.refresh();
  assert.equal(busy.error, null);
  assert.equal(busy.deletion.revision, pending.revision);
  assert.equal(busy.deletion.copies[0].reason, 'source_in_use');
  assert.equal(busy.deletion.releasedBytes, 0);
  const quiet = await client.refresh();
  assert.equal(quiet.error, null);
  assert.equal(quiet.deletion.copies[0].reason, null);
  assert.equal(quiet.deletion.revision, pending.revision);
  onlyGets(f.requests.slice(start));
  assert.equal((await f.rpc('counters')).execute, 0);
});
