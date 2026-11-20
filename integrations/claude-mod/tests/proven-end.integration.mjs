// The loaded plugin talks to a real WATCH server. The native hook host is fictional.
import assert from 'node:assert/strict';

let input = '';
for await (const part of process.stdin) input += part;
const config = JSON.parse(input);
const storage = new Map();
const TURN = 'fictional-original-turn';
const listeners = new Set();
let runtimeNumber = 0;

function notify() {
  for (const listener of listeners) listener();
}

function waitFor(label, check) {
  return new Promise((resolve, reject) => {
    const deadline = setTimeout(() => {
      listeners.delete(observe);
      reject(Error(`Claude fixture did not observe ${label}`));
    }, 5000);
    function observe() {
      const value = check();
      if (!value) return;
      clearTimeout(deadline);
      listeners.delete(observe);
      resolve(value);
    }
    listeners.add(observe);
    observe();
  });
}

async function runtime(native, {holdStartAck = false} = {}) {
  const {register} = await import(`../hooks/register.js?runtime=${++runtimeNumber}`);
  const hooks = new Map(), timers = [], calls = [], submissions = [];
  const writtenKeys = new Set();
  let discardCompletionAck = false;
  let releaseStartAck;
  const startAck = new Promise(resolve => {releaseStartAck = resolve;});
  const events = kind => calls.filter(item => item.method === 'event' && item.body.kind === kind);
  const request = id => [...writtenKeys].map(key => storage.get(key)?.requests?.[id]).find(Boolean);
  const host = {
    session: {id: async () => native, cwd: async () => config.project, surfaces: async () => ['desktop']},
    store: {
      keys: async () => [...storage.keys()],
      get: async key => structuredClone(storage.get(key)),
      set: async (key, value) => {
        storage.set(key, structuredClone(value));
        writtenKeys.add(key);
        notify();
      }
    },
    command: {register: async () => {}},
    clock: {now: async () => Date.now(), every: (_delay, run) => {
      const timer = {run, cancelled: false, cancel() {this.cancelled = true;}};
      timers.push(timer);
      return timer;
    }},
    http: {fetch: async (address, init) => {
      assert.equal(new URL(address).origin, `http://127.0.0.1:${config.port}`);
      const response = await fetch(address, init);
      const text = await response.text();
      const method = new URL(address).pathname.split('/').at(-1);
      const body = JSON.parse(init.body);
      const discarded = discardCompletionAck && method === 'event' && body.kind === 'completed';
      calls.push({method, body, status: response.status, value: JSON.parse(text), discarded});
      notify();
      if (holdStartAck && method === 'event' && body.kind === 'started') await startAck;
      if (discarded) {
        discardCompletionAck = false;
        assert.equal(response.status, 200, 'discard the response only after WATCH commits the end');
        throw Error('Fictional transport lost the committed completion acknowledgement');
      }
      return {ok: response.ok, status: response.status, headers: {}, text};
    }},
    prompt: {submit: async ({text, asUser}) => {
      assert.equal(asUser, false);
      submissions.push(text);
      notify();
      return {text};
    }}
  };
  register((name, handler) => {hooks.set(name, handler); return {catch() {}};});
  const call = (name, event) => hooks.get(name)(host, event, async () => ({}));
  await call('session.start', {cwd: config.project, surface: 'desktop', isInteractive: false});
  return {
    call, calls, submissions, events, request,
    acknowledged: (id, kind) => {
      const saved = request(id);
      return saved && events(kind).some(event => event.body.request_id === id && event.status === 200 && !event.discarded) &&
        !saved.events.some(event => event.kind === kind);
    },
    registration: () => calls.find(item => item.method === 'register' && item.status === 200 && item.value.lifecycle_protocol === 2),
    loseNextCompletionAck: () => {discardCompletionAck = true;},
    tick: async () => {for (const timer of timers) if (!timer.cancelled) await timer.run();},
    end: async () => {
      await call('session.end', {});
      // Epoch invalidation precedes release of a committed but unacknowledged start.
      releaseStartAck();
      await waitFor('disconnect response', () => calls.find(call => call.method === 'disconnect' && call.status === 200 && call.value.disconnected));
      hooks.clear();
      timers.length = 0;
    }
  };
}

async function reload(original) {
  const before = original.registration().value;
  await original.end();
  const replacement = await runtime(config.target);
  await replacement.tick();
  assert(replacement.registration(), 'replacement registration was not accepted');
  const current = replacement.registration().value;
  assert.notEqual(current.client_id, before.client_id);
  assert.notEqual(current.generation, before.generation);
  assert.equal(current.grant_id, before.grant_id);
  assert.equal(current.enrollment_id, before.enrollment_id);
  return replacement;
}

async function ownerRetire() {
  const response = await fetch(`http://127.0.0.1:${config.port}/bridge-admin/v1/retire`, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Live-Room-Key': config.owner_key},
    body: JSON.stringify({request_id: config.request_id, agent: `c:${config.target}`, confirm: 'retire_unknown_without_resend'})
  });
  const value = await response.json();
  assert.equal(response.status, 200, JSON.stringify(value));
  assert.equal(value.status, 'retired');
  assert.equal(value.provider_outcome, 'unknown');
}

const sibling = await runtime(config.sibling);
await sibling.call('command.run', {command: 'watch-setup', args: `${config.port}:${config.code}`, origin: {kind: 'composer'}});
await sibling.tick();
assert(sibling.registration(), 'sibling registration was not accepted');
const replayStart = config.mode === 'start_ack_replay';
const original = await runtime(config.target, {holdStartAck: replayStart});
await original.tick();
assert(original.registration(), 'original registration was not accepted');
await original.tick();
const envelope = await waitFor('original prompt submission', () => original.submissions[0]);
const [, transport] = /^WATCH_PEER_REQUEST ([0-9a-f-]+)\n/.exec(envelope);
await waitFor('stored submission acknowledgement', () => original.acknowledged(transport, 'submitted'));
assert.equal(original.submissions.length, 1);
const started = {text: envelope, turnId: TURN};
await original.call('turn.start', started);
const receivedStart = await waitFor('original start response', () => original.events('started')[0]);
assert.equal(receivedStart.status, 200);
if (replayStart) {
  assert(original.request(transport).events.some(event => event.kind === 'started'), 'start must survive the reload');
} else {
  await waitFor('stored start acknowledgement', () => original.acknowledged(transport, 'started'));
}
const originalStart = receivedStart.body;
assert.equal(originalStart.request_id, transport);
const completion = {turnId: TURN, answer: `WATCH_REPLY ${transport}\n${config.answer}`, isAborted: false, reason: 'answer'};

let replacement;
if (config.mode === 'lost_ack') {
  await original.call('session.receive', {text: config.owner_input, origin: {kind: 'composer'}});
  original.loseNextCompletionAck();
  await original.call('turn.complete', completion);
  const first = await waitFor('first completion response', () => original.events('completed')[0]);
  assert(first.discarded);
  assert.equal(first.status, 200);
  const retained = [...storage.values()].flatMap(shard => shard.requests?.[transport]?.events ?? []);
  assert(retained.some(event => event.event_id === first.body.event_id), 'the committed receipt is still pending locally');
  replacement = await reload(original);
  await waitFor('replayed completion response', () => replacement.events('completed').find(event => event.status === 200));
  assert.equal(original.events('completed').length, 1);
  assert.equal(replacement.events('completed').length, 1);
  const {generation: originalGeneration, client_id: originalClient, ...committed} = first.body;
  const {generation, client_id, ...replayed} = replacement.events('completed')[0].body;
  assert.notEqual(generation, originalGeneration);
  assert.notEqual(client_id, originalClient);
  assert.deepEqual(replayed, committed);
} else {
  replacement = await reload(original);
  await replacement.tick();
  const proofPoll = () => replacement.calls.find(call => call.method === 'poll' && call.status === 200 && call.value.recoverable_turns?.length);
  assert(proofPoll(), 'replacement did not receive the original turn proof');
  const [proof] = proofPoll().value.recoverable_turns;
  assert.deepEqual(proofPoll().value.recoverable_turns, [{request_id: transport,
    attempt_id: originalStart.attempt_id, generation: originalStart.generation, turn_id: TURN}]);
  assert.equal(proofPoll().value.claim, null);

  // Exercise both a different turn and a child completion for the original turn.
  await replacement.call('turn.complete', {...completion, turnId: 'fictional-unrelated-turn'});
  await replacement.call('turn.complete', {...completion, agentId: 'fictional-child'});
  await replacement.tick();
  assert.equal(replacement.events('completed').length, 0);
  if (config.mode === 'retired_race') await ownerRetire();
  await replacement.call('turn.complete', completion);
  await waitFor('replacement completion response', () => replacement.events('completed').find(event => event.status === 200));
  assert.equal(replacement.events('completed').length, 1);
  const [event] = replacement.events('completed');
  assert.equal(event.body.request_id, proof.request_id);
  assert.equal(event.body.attempt_id, proof.attempt_id);
  assert.equal(event.body.turn_id, proof.turn_id);
  assert.equal(event.value.retired === true, config.mode === 'retired_race');
}

const receipt = replacement.events('completed')[0].body;
const current = replacement.registration().value;
assert.equal(receipt.generation, current.generation);
assert.equal(receipt.client_id, current.client_id);
await waitFor('stored completion acknowledgement', () => replacement.acknowledged(transport, 'completed'));
assert(replacement.request(transport).execution_completed);
const completionPosts = [...original.events('completed'), ...replacement.events('completed')];
for (const {body} of completionPosts) {
  assert.equal(body.outcome, 'withheld');
  assert.equal(body.answer, '');
}
const runtimes = [original, sibling, replacement];
const submissions = runtimes.map(runtime => runtime.submissions.length);
assert.deepEqual(submissions, [1, 0, 0], 'exact native prompt submission counts');
const starts = runtimes.map(runtime => runtime.events('started').map(({body, status}) => ({body, status})));
assert.deepEqual(starts.map(posts => posts.length), [1, 0, Number(replayStart)]);
if (replayStart) {
  const {generation: beforeGeneration, client_id: beforeClient, ...initial} = originalStart;
  const {generation, client_id, ...replayed} = starts[2][0].body;
  assert.deepEqual(replayed, initial);
  assert.notEqual(generation, beforeGeneration);
  assert.notEqual(client_id, beforeClient);
  assert.equal(generation, current.generation);
  assert.equal(client_id, current.client_id);
}
console.log(JSON.stringify({originalStart, completion: receipt, completionPosts: completionPosts.length, submissions, starts}));
