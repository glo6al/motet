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

test('production Claude client recovers a lost Send reply by GET and suppresses conflicting prose', {timeout: 18000}, async t => {
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
  const journalStore = new Map(), requests = [];
  const storage = {getItem: k => journalStore.get(k) ?? null, setItem: (k, v) => journalStore.set(k, v)};
  const locks = {request(name, _, fn) { return Promise.resolve().then(() => fn({name})); }};
  const journal = createNativeAgentAttachmentsJournal({storage, locks, crypto: webcrypto});
  let loseSendResponse = true;
  const fetchAttachment = async (path, init) => {
    assert(path.startsWith('/api/attachments/') && !path.includes('agent-message'));
    requests.push({path, method: init.method});
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
  result = await client.checkRequest(handle);
  assert.equal(result.error, null, JSON.stringify(result.error));
  assert.deepEqual(result.requests[0].reply, {state: 'available', text: ANSWER});
  assert.equal(result.requests[0].delivery, 'completed');
  assert.equal(result.requests[0].execution, 'ended');
  assert.deepEqual(await mod('conflict'), {state: 'unknown'});
  result = await client.checkRequest(handle);
  assert.equal(result.error, null, JSON.stringify(result.error));
  assert.deepEqual(result.requests[0].reply, {state: 'withheld', text: null});
  assert.equal(result.requests[0].execution, 'ended');
  assert(!JSON.stringify(result).includes(ANSWER));
  assert.deepEqual(requests.filter(r => r.method !== 'GET').map(r => r.method), ['POST','POST','PUT','PATCH','POST','POST']);
  await modStop();

  async function modStop() { child.stdin.end(JSON.stringify('stop') + '\n'); }
});
