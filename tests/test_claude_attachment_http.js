'use strict';

// The real production client and journal use the server's actual owner and mod
// routes. The disposable Python process substitutes only the official mod host.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const {webcrypto} = require('node:crypto');
const {join} = require('node:path');
const {createNativeAgentAttachmentsClient} = require('../src/web/native-agent-attachments-client.js');
const {createNativeAgentAttachmentsJournal} = require('../src/web/native-agent-attachments-journal.js');

const KEY = 'fictional-owner-key';
const QUESTION = 'Review the fictional orchard attachment.';
const ANSWER = 'Fictional orchard answer.';

async function fixture(t) {
  const paths = {src: join(__dirname, '../src'), tests: __dirname};
  const child = spawn('python3', [join(__dirname, 'test_claude_attachment_integration.py'), '--browser-client-fixture'], {
    env: {...process.env, PYTHONPATH: paths.src + ':' + paths.tests, PYTHONDONTWRITEBYTECODE: '1'},
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const exited = once(child, 'exit');
  const lines = createInterface({input: child.stdout})[Symbol.asyncIterator]();
  let errors = '';
  child.stderr.on('data', bytes => { errors = (errors + bytes).slice(-4000); });
  t.after(async () => {
    child.stdin.end();
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    try { const [code, signal] = await exited; assert.equal(code, 0, signal || errors); }
    finally { clearTimeout(timer); }
  });
  async function read() {
    const result = await lines.next();
    assert(!result.done, errors);
    return JSON.parse(result.value);
  }
  async function mod(command) {
    child.stdin.write(JSON.stringify(command) + '\n');
    return read();
  }
  const {origin, destinationId} = await read();
  assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  return {origin, destinationId, mod};
}

test('production Claude client recovers a lost Send reply by GET and suppresses conflicting prose', {timeout: 18000}, async t => {
  const {origin, destinationId, mod} = await fixture(t);
  const journalStore = new Map(), requests = [];
  const storage = {getItem: k => journalStore.get(k) ?? null, setItem: (k, v) => journalStore.set(k, v)};
  const locks = {request(name, _, fn) { return Promise.resolve().then(() => fn({name})); }};
  const events = new EventTarget();
  const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto, events});
  let loseSendResponse = true, failCheck = false;
  const fetchAttachment = async (path, init) => {
    assert(path.startsWith('/api/attachments/') && !path.includes('agent-message'));
    requests.push({path, method: init.method});
    if (failCheck && init.method === 'GET' && /\/native\/requests\/[0-9a-f]{32}\?/.test(path)) {
      failCheck = false;
      const event = new Event('storage');
      Object.defineProperty(event, 'key', {value: null});
      events.dispatchEvent(event);
      await new Promise(resolve => setImmediate(resolve));
      throw Error('Fictional status read failed during a storage event.');
    }
    const response = await fetch(origin + path, init);
    if (loseSendResponse && init.method === 'POST' && /\/native\/requests\/[0-9a-f]{32}\/send$/.test(path)) {
      assert.equal(response.status, 200, await response.clone().text());
      loseSendResponse = false;
      await response.text();
      throw Error('Fictional lost Send acknowledgment.');
    }
    return response;
  };
  const client = createNativeAgentAttachmentsClient({enabled: true, demo: false, fetch: fetchAttachment,
    auth: () => KEY, crypto: webcrypto, AbortController, journal});
  t.after(() => client.dispose());
  const connected = await client.connect({destinationId});
  assert.equal(connected.error, null, JSON.stringify(connected.error));
  assert.deepEqual(connected.features, {drafts: true, send: true, files: false, deletion: false});
  const draft = await client.addFiles([new File(['%PDF-1.4\nFictional orchard attachment.\n%%EOF'], 'fictional.pdf',
    {type: 'application/pdf'})]);
  assert.equal(draft.error, null, JSON.stringify(draft.error));
  assert.deepEqual(draft.draft.files.map(f => f.state), ['ready']);
  const attempted = await client.send(QUESTION);
  assert(attempted.error, 'A committed fictional HTTP acknowledgment was lost.');
  assert(attempted.uncertain);
  assert(!loseSendResponse);
  assert(!JSON.stringify([...journalStore]).includes(QUESTION));
  assert(!JSON.stringify([...journalStore]).includes(KEY));
  const start = requests.length;
  let result = await client.refresh();
  assert.equal(result.error, null, JSON.stringify(result.error));
  assert.equal(result.uncertain, false);
  assert.equal(result.requests.length, 1);
  assert.equal(result.requests[0].execution, 'unobserved');
  assert.equal(result.requests[0].accepted, false);
  assert(requests.slice(start).every(r => r.method === 'GET'));
  assert.equal(requests.filter(r => /\/native\/requests\/[0-9a-f]{32}\/send$/.test(r.path)).length, 1);
  const handle = result.requests[0].actions.check;
  assert.deepEqual(await mod('claim'), {claimed: true});
  assert.deepEqual(await mod('complete'), {state: 'answered'});
  const beforeCheck = requests.length;
  result = await client.checkRequest(handle);
  assert.equal(result.error, null, JSON.stringify(result.error));
  assert.deepEqual(requests.slice(beforeCheck).map(({method}) => method), ['GET']);
  assert.deepEqual(result.requests[0].reply, {state: 'available', text: ANSWER});
  assert.equal(result.requests[0].delivery, 'completed');
  assert.equal(result.requests[0].execution, 'ended');
  assert.deepEqual(await mod('conflict'), {state: 'unknown'});
  result = await client.checkRequest(handle);
  assert.equal(result.error, null, JSON.stringify(result.error));
  assert.deepEqual(result.requests[0].reply, {state: 'withheld', text: null});
  assert.equal(result.requests[0].execution, 'ended');
  assert(!JSON.stringify(result).includes(ANSWER));
  failCheck = true;
  result = await client.checkRequest(handle);
  assert.equal(result.error?.code, 'network_error');
  assert.equal(result.busy, false);
  assert.deepEqual(requests.filter(r => r.method !== 'GET').map(r => r.method), ['POST','POST','PUT','PATCH','POST','POST']);
});

test('a lost Claude refusal releases only the sealed intent on exact readback', {timeout: 18000}, async t => {
  const {origin, destinationId, mod} = await fixture(t);
  const journalStore = new Map(), requests = [];
  const storage = {getItem: k => journalStore.get(k) ?? null, setItem: (k, v) => journalStore.set(k, v)};
  const locks = {request(name, _, fn) { return Promise.resolve().then(() => fn({name})); }};
  let dropResponse = true;
  const sendPath = /\/native\/requests\/[0-9a-f]{32}\/send$/;
  const fetchAttachment = async (path, init) => {
    requests.push({path, method: init.method});
    const response = await fetch(origin + path, init);
    if (dropResponse && init.method === 'POST' && sendPath.test(path)) {
      dropResponse = false;
      const payload = await response.json();
      assert.equal(payload.observation.request.reason, 'earlier_delivery_unresolved');
      throw Error('Fictional lost refusal response.');
    }
    return response;
  };
  const client = () => {
    const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto});
    const instance = createNativeAgentAttachmentsClient({enabled: true, demo: false, fetch: fetchAttachment,
      auth: () => KEY, crypto: webcrypto, AbortController, journal});
    t.after(() => instance.dispose());
    return instance;
  };
  const before = client();
  assert.equal((await before.connect({destinationId})).error, null);
  const attached = await before.addFiles([new File(['%PDF-1.4\nFictional orchard attachment.\n%%EOF'], 'fictional.pdf',
    {type: 'application/pdf'})]);
  assert.equal(attached.draft.files[0].state, 'ready');
  assert.deepEqual(await mod('block'), {blocked: true});
  const refused = await before.send(QUESTION);
  assert.equal(refused.error.code, 'network_error');
  assert.equal(refused.uncertain, true);
  assert.equal(refused.attempts[0].phase, 'sending');
  assert.equal(refused.draft.files.length, 1);
  before.dispose();

  const beforeReadback = requests.length;
  const after = client();
  let recovered = await after.connect({destinationId});
  assert.equal(recovered.error.code, 'earlier_delivery_unresolved');
  assert.equal(recovered.uncertain, false);
  assert.deepEqual(recovered.attempts, []);
  assert.equal(recovered.savedDrafts.length, 1);
  assert.equal(recovered.requests.find(r => r.reason === 'earlier_delivery_unresolved').delivery, 'not_started');
  assert(requests.slice(beforeReadback).every(r => r.method === 'GET'));
  assert.equal(requests.filter(r => sendPath.test(r.path)).length, 1);

  assert.deepEqual(await mod('retire'), {retired: true});
  recovered = await after.resume(recovered.savedDrafts[0].id);
  assert.equal(recovered.error, null, JSON.stringify(recovered.error));
  assert.equal(recovered.draft.text, QUESTION);
  assert.equal(recovered.draft.files[0].name, 'fictional.pdf');
  const sent = await after.send(QUESTION);
  assert.equal(sent.error, null, JSON.stringify(sent.error));
  assert.equal(sent.uncertain, false);
  assert.equal(sent.draft, null);
  assert.equal(requests.filter(r => sendPath.test(r.path)).length, 2);
  assert.deepEqual(await mod('claim'), {claimed: true});
  assert.deepEqual(await mod('complete'), {state: 'answered'});
});
