// The loaded plugin talks to a real WATCH server. The native hook host is fictional.
import assert from 'node:assert/strict';

let input = '';
for await (const part of process.stdin) input += part;
const config = JSON.parse(input);
const storage = new Map();
const TURN = 'fictional-original-turn';
let runtimeNumber = 0;

async function until(check, drive = async () => {}) {
  for (let attempt = 0; attempt < 300; attempt++) {
    if (check()) return;
    await drive();
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw Error('Claude recovery fixture did not settle');
}

async function runtime(native) {
  const {register} = await import(`../hooks/register.js?runtime=${++runtimeNumber}`);
  const hooks = new Map(), timers = [], calls = [], submissions = [];
  let discardCompletionAck = false;
  const host = {
    session: {id: async () => native, cwd: async () => config.project, surfaces: async () => ['desktop']},
    store: {
      keys: async () => [...storage.keys()],
      get: async key => structuredClone(storage.get(key)),
      set: async (key, value) => {storage.set(key, structuredClone(value));}
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
      return {text};
    }}
  };
  register((name, handler) => {hooks.set(name, handler); return {catch() {}};});
  const call = (name, event) => hooks.get(name)(host, event, async () => ({}));
  await call('session.start', {cwd: config.project, surface: 'desktop', isInteractive: false});
  return {
    call, calls, submissions,
    events: kind => calls.filter(item => item.method === 'event' && item.body.kind === kind),
    registration: () => calls.find(item => item.method === 'register' && item.status === 200 && item.value.lifecycle_protocol === 2),
    loseNextCompletionAck: () => {discardCompletionAck = true;},
    tick: async () => {for (const timer of timers) if (!timer.cancelled) await timer.run();},
    end: async () => {
      await call('session.end', {});
      await until(() => calls.some(call => call.method === 'disconnect' && call.status === 200 && call.value.disconnected));
      hooks.clear();
      timers.length = 0;
    }
  };
}

async function reload(original) {
  const before = original.registration().value;
  await original.end();
  const replacement = await runtime(config.target);
  await until(() => replacement.registration(), replacement.tick);
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
await until(() => sibling.registration(), sibling.tick);
const original = await runtime(config.target);
await until(() => original.events('submitted').some(event => event.status === 200), original.tick);
assert.equal(original.submissions.length, 1);
const [envelope] = original.submissions;
const [, transport] = /^WATCH_PEER_REQUEST ([0-9a-f-]+)\n/.exec(envelope);
const started = {text: envelope, turnId: TURN};
await original.call('turn.start', started);
await until(() => original.events('started').some(event => event.status === 200));
const originalStart = original.events('started')[0].body;
assert.equal(originalStart.request_id, transport);
const completion = {turnId: TURN, answer: `WATCH_REPLY ${transport}\n${config.answer}`, isAborted: false, reason: 'answer'};

let replacement;
if (config.mode === 'lost_ack') {
  await original.call('session.receive', {text: config.owner_input, origin: {kind: 'composer'}});
  original.loseNextCompletionAck();
  await original.call('turn.complete', completion);
  await until(() => original.events('completed').length === 1);
  const first = original.events('completed')[0];
  assert(first.discarded);
  assert.equal(first.status, 200);
  const retained = [...storage.values()].flatMap(shard => shard.requests?.[transport]?.events ?? []);
  assert(retained.some(event => event.event_id === first.body.event_id), 'the committed receipt is still pending locally');
  replacement = await reload(original);
  await until(() => replacement.events('completed').some(event => event.status === 200), replacement.tick);
  assert.equal(original.events('completed').length, 1);
  assert.equal(replacement.events('completed').length, 1);
  const {generation: originalGeneration, client_id: originalClient, ...committed} = first.body;
  const {generation, client_id, ...replayed} = replacement.events('completed')[0].body;
  assert.notEqual(generation, originalGeneration);
  assert.notEqual(client_id, originalClient);
  assert.deepEqual(replayed, committed);
} else {
  replacement = await reload(original);
  const proofPoll = () => replacement.calls.find(call => call.method === 'poll' && call.status === 200 && call.value.recoverable_turns?.length);
  await until(proofPoll, replacement.tick);
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
  await until(() => replacement.events('completed').some(event => event.status === 200));
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
await until(() => [...storage.values()].some(shard => {
  const request = shard.requests?.[transport];
  return request?.execution_completed && request.events.length === 0;
}));
const completionPosts = [...original.events('completed'), ...replacement.events('completed')];
for (const {body} of completionPosts) {
  assert.equal(body.outcome, 'withheld');
  assert.equal(body.answer, '');
}
assert.equal(original.submissions.length, 1);
assert.equal(sibling.submissions.length, 0);
assert.equal(replacement.submissions.length, 0);
console.log(JSON.stringify({originalStart, completion: receipt, completionPosts: completionPosts.length}));
