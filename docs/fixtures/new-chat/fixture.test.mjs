import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createNewChatFixture, SCENARIOS } from './fixture.mjs';

const START = Date.parse('2030-01-01T12:00:00.000Z');
const choice = { provider: 'codex', folderId: 'fixture-design', folderRevision: '1' };
const fixture = scenario => createNewChatFixture({ scenario, now: () => START });
const rejects = (promise, code) => assert.rejects(promise, error => error.code === code);
async function prepared(demo, overrides = {}) {
  const selected = { ...choice, ...overrides };
  const intent = await demo.prepare(selected);
  return { requestId: intent.requestId, ...selected };
}
async function open(demo, overrides = {}, draft) {
  const input = await prepared(demo, overrides);
  return demo.open(draft === undefined ? input : { ...input, draft });
}

test('fictional context has exact folders, revisions, capabilities and defensive copies', async () => {
  const demo = fixture('waiting');
  const context = await demo.context();
  assert.equal(context.fixture, true);
  assert.deepEqual(context.folders.map(({ id, revision, displayPath }) => ({ id, revision, displayPath })), [
    { id: 'fixture-design', revision: '1', displayPath: '/fictional/projects/design-lab' },
    { id: 'fixture-notes', revision: '1', displayPath: '/fictional/projects/field-notes' },
  ]);
  assert.equal(context.providers.find(provider => provider.id === 'codex').supportsDraft, true);
  assert.equal(context.providers.find(provider => provider.id === 'claude_code').supportsDraft, false);
  assert.equal(context.providers.find(provider => provider.id === 'antigravity').unavailableReason.code, 'antigravity_creation_unproven');
  context.folders[0].revision = 'tampered';
  assert.equal((await demo.context()).folders[0].revision, '1');
});

for (const scenario of SCENARIOS) {
  test(`scenario ${scenario} reaches its advertised state with a 120-second deadline`, async () => {
    const demo = fixture(scenario);
    const opened = await open(demo, { provider: scenario === 'confirm' ? 'claude_code' : 'codex' });
    assert.equal(opened.createdAt, '2030-01-01T12:00:00.000Z');
    assert.equal(opened.deadlineAt, '2030-01-01T12:02:00.000Z');
    demo.advance(scenario === 'timeout' ? 120_000 : 1_000);
    const result = await demo.status(opened.attemptId);
    assert.equal(result.state, scenario);
    assert.equal(result.fixture, true);
    assert.equal(result.launch.attempts, scenario === 'unavailable' ? 0 : 1);
    assert.deepEqual(Object.keys(result).sort(), [
      'schemaVersion', 'attemptId', 'requestId', 'provider', 'folder', 'state', 'launch',
      'creation', 'uiOpen', 'existingSession',
      'observation', 'createdAt', 'deadlineAt', 'session', 'mismatch', 'reason', 'draft', 'capabilities', 'fixture',
    ].sort());
    if (scenario === 'added') {
      assert.equal(result.session.folderId, result.folder.id);
      assert.ok(result.session.roomAgentId);
    } else assert.equal(result.session, null);
    if (scenario !== 'mismatch') assert.equal(result.mismatch, null);
    if (scenario === 'confirm') {
      assert.equal(result.reason.code, 'native_folder_confirmation_required');
      assert.equal(result.reason.message, 'Confirm the selected folder in Claude’s window.');
    }
  });
}

test('prepare mints unique five-minute immutable intents without launching', async () => {
  const demo = fixture('waiting');
  const first = await demo.prepare(choice);
  const second = await demo.prepare(choice);
  assert.match(first.requestId, /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/);
  assert.notEqual(first.requestId, second.requestId);
  assert.equal(first.expiresAt, '2030-01-01T12:05:00.000Z');
  assert.equal(first.fixture, true);
  assert.equal(first.host.id, 'fixture-host');
  assert.equal(first.folder.id, choice.folderId);
  assert.equal(first.launch, undefined);
  const input = { requestId: first.requestId, ...choice };
  first.folder.id = 'tampered';
  await rejects(demo.open({ ...input, provider: 'claude_code' }), 'intent_mismatch');
  await rejects(demo.open({ ...input, folderId: 'fixture-notes' }), 'intent_mismatch');
  assert.equal((await demo.open(input)).folder.id, 'fixture-design');
});

test('mismatch reports the actual other folder and existing independent monitor without attachment', async () => {
  for (const folderId of ['fixture-design', 'fixture-notes']) {
    const demo = fixture('mismatch');
    const opened = await open(demo, { folderId });
    assert.equal(opened.mismatch, null);
    demo.advance(1_000);
    const result = await demo.status(opened.attemptId);
    assert.equal(result.state, 'mismatch');
    assert.equal(result.session, null);
    assert.notEqual(result.mismatch.displayPath, result.folder.displayPath);
    assert.equal(result.mismatch.displayPath, folderId === 'fixture-design'
      ? '/fictional/projects/field-notes' : '/fictional/projects/design-lab');
    assert.equal(result.mismatch.sessionId, 'fixture-unmatched-session');
    assert.equal(result.mismatch.existingRoomAgentId, 'fixture-existing-other-agent');
  }
});

test('unknown, expired and old fixture IDs cannot launch; spent IDs replay after intent expiry', async () => {
  const demo = fixture('waiting');
  const input = await prepared(demo);
  await rejects(demo.open({ ...choice, requestId: '00000000-0000-4000-8000-000000000001' }), 'unknown_intent');
  demo.advance(300_000);
  await rejects(demo.open(input), 'intent_expired');
  await rejects(demo.open(input), 'intent_expired');
  const activeInput = await prepared(demo);
  const opened = await demo.open(activeInput);
  demo.advance(400_000);
  assert.equal((await demo.open(activeInput)).attemptId, opened.attemptId);
  assert.deepEqual((await demo.open(activeInput)).launch, opened.launch);
  await rejects(fixture('waiting').open(activeInput), 'unknown_intent');
});

test('late observation times out; explicit continuation may associate without relaunch', async () => {
  const demo = fixture('added');
  const opened = await open(demo);
  demo.advance(180_000);
  assert.equal((await demo.status(opened.attemptId)).state, 'timeout');
  const continued = await demo.keepWaiting(opened.attemptId);
  assert.equal(continued.state, 'waiting');
  const added = await demo.status(opened.attemptId);
  assert.equal(added.state, 'added');
  assert.deepEqual(added.launch, opened.launch);
});

test('confirm remains active until timeout and never attaches a session', async () => {
  const demo = fixture('confirm');
  const opened = await open(demo, { provider: 'claude_code' });
  demo.advance(119_999);
  assert.equal((await demo.status(opened.attemptId)).state, 'confirm');
  demo.advance(1);
  const expired = await demo.status(opened.attemptId);
  assert.equal(expired.state, 'timeout');
  assert.equal(expired.session, null);
});

test('request replay is idempotent, including deadline, reordered fields and UUID casing', async () => {
  const demo = fixture('waiting');
  const input = { ...await prepared(demo), draft: 'Fictional draft' };
  const first = await demo.open(input);
  const replay = await demo.open({ draft: input.draft, folderRevision: '1', folderId: 'fixture-design', provider: 'codex', requestId: input.requestId.toUpperCase() });
  assert.deepEqual(replay, first);
  demo.advance(120_000);
  const expired = await demo.open(input);
  assert.equal(expired.attemptId, first.attemptId);
  assert.equal(expired.state, 'timeout');
  assert.deepEqual(expired.launch, { attempts: 1, outcome: 'accepted' });
  await rejects(demo.open({ ...input, draft: 'Another draft' }), 'request_conflict');
  await rejects(demo.open({ ...input, folderId: 'fixture-notes' }), 'request_conflict');
  await rejects(demo.open({ ...input, provider: 'claude_code' }), 'request_conflict');
  const { draft, ...withoutDraft } = input;
  await rejects(demo.open(withoutDraft), 'request_conflict');
});

test('parallel duplicate opens reserve exactly one attempt after asynchronous digest', async () => {
  const demo = fixture('waiting');
  const input = { ...await prepared(demo), draft: 'Concurrent fictional draft' };
  const responses = await Promise.all(Array.from({ length: 16 }, () => demo.open(input)));
  for (const response of responses) assert.deepEqual(response, responses[0]);
  assert.equal(responses[0].launch.attempts, 1);
  const second = await open(demo);
  assert.equal(second.attemptId, 'fixture-attempt-0002');
});

test('timeout is stable until continuation; continuation observes the same launch only', async () => {
  const demo = fixture('timeout');
  const input = await prepared(demo);
  const first = await demo.open(input);
  demo.advance(120_000);
  const expired = await demo.status(first.attemptId);
  assert.deepEqual(expired.capabilities, { keepWaiting: true, stopWaiting: false, openExisting: false });
  demo.advance(30_000);
  assert.deepEqual(await demo.status(first.attemptId), expired);
  const continued = await demo.keepWaiting(first.attemptId);
  assert.equal(continued.state, 'waiting');
  assert.equal(continued.observation, 'active');
  assert.equal(continued.attemptId, first.attemptId);
  assert.equal(continued.createdAt, first.createdAt);
  assert.equal(continued.deadlineAt, '2030-01-01T12:04:30.000Z');
  assert.deepEqual(continued.launch, first.launch);
  demo.advance(120_000);
  assert.equal((await demo.status(first.attemptId)).state, 'timeout');
  assert.deepEqual((await demo.open(input)).launch, first.launch);
});

test('stopping prevents late adds; repeated stop and replay never resume observation', async () => {
  const demo = fixture('added');
  const input = await prepared(demo);
  const opened = await demo.open(input);
  const stopped = await demo.stopWaiting(opened.attemptId);
  assert.equal(stopped.observation, 'stopped');
  assert.deepEqual(stopped.capabilities, { keepWaiting: false, stopWaiting: false, openExisting: false });
  demo.advance(180_000);
  assert.deepEqual(await demo.status(opened.attemptId), stopped);
  assert.deepEqual(await demo.open(input), stopped);
  assert.deepEqual(await demo.stopWaiting(opened.attemptId), stopped);
  await rejects(demo.keepWaiting(opened.attemptId), 'cannot_continue');
});

test('explicitly stopping an expired attempt prevents continuation', async () => {
  const demo = fixture('timeout');
  const opened = await open(demo);
  demo.advance(120_000);
  const stopped = await demo.stopWaiting(opened.attemptId);
  assert.equal(stopped.capabilities.keepWaiting, false);
  await rejects(demo.keepWaiting(opened.attemptId), 'cannot_continue');
});

for (const scenario of ['added', 'mismatch', 'failed', 'unavailable']) {
  test(`terminal ${scenario} snapshots are stable and cannot continue`, async () => {
    const demo = fixture(scenario);
    const opened = await open(demo);
    demo.advance(1_000);
    const settled = await demo.status(opened.attemptId);
    demo.advance(500_000);
    assert.deepEqual(await demo.status(opened.attemptId), settled);
    assert.deepEqual(await demo.stopWaiting(opened.attemptId), settled);
    await rejects(demo.keepWaiting(opened.attemptId), 'cannot_continue');
    assert.equal(settled.session !== null, scenario === 'added');
  });
}

test('unavailable providers accept intents but never launch', async () => {
  for (const provider of ['antigravity', 'chatgpt_work']) {
    const demo = fixture('added');
    const result = await open(demo, { provider });
    assert.equal(result.state, 'unavailable');
    assert.deepEqual(result.launch, { attempts: 0, outcome: 'not_attempted' });
    assert.equal(result.draft.disposition, 'not_provided');
    demo.advance(150_000);
    assert.deepEqual(await demo.status(result.attemptId), result);
  }
});

test('draft reports Codex prefill intent only and never exposes or sends content', async () => {
  const demo = fixture('waiting');
  const codex = await open(demo, {}, 'PRIVATE FICTIONAL DRAFT');
  assert.deepEqual(codex.draft, { provided: true, disposition: 'prefill_requested' });
  assert.ok(!JSON.stringify(codex).includes('PRIVATE FICTIONAL DRAFT'));
  for (const provider of ['claude_code', 'antigravity', 'chatgpt_work']) {
    const input = await prepared(demo, { provider });
    await rejects(demo.open({ ...input, draft: 'PRIVATE FICTIONAL DRAFT' }), 'draft_unsupported');
    // Rejection did not spend the intent or create a launch attempt.
    const withoutDraft = await demo.open(input);
    assert.deepEqual(withoutDraft.draft, { provided: false, disposition: 'not_provided' });
    if (provider !== 'claude_code') assert.equal(withoutDraft.launch.attempts, 0);
  }
  assert.deepEqual((await open(demo)).draft, { provided: false, disposition: 'not_provided' });
  assert.deepEqual((await open(demo, {}, '')).draft, { provided: false, disposition: 'not_provided' });
  const emptyInput = await prepared(demo, { provider: 'claude_code' });
  const empty = await demo.open({ ...emptyInput, draft: '' });
  assert.deepEqual(await demo.open(emptyInput), empty);
  const unicode = await open(demo, {}, '🪴'.repeat(8_000));
  assert.equal(unicode.draft.disposition, 'prefill_requested');
  assert.deepEqual(Object.keys(demo).sort(), ['advance', 'context', 'prepare', 'keepWaiting', 'open', 'status', 'stopWaiting'].sort());
  const source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /\b(?:import|fetch|process|localStorage|sessionStorage|XMLHttpRequest|WebSocket)\b/);
});

test('rejects unknown fields, invalid requests, missing folders and stale revisions', async () => {
  const demo = fixture('waiting');
  const input = await prepared(demo);
  for (const invalid of [null, [], 'bad', {}, { ...input, extra: true }]) await assert.rejects(demo.open(invalid));
  for (const invalid of [null, [], {}, { ...choice, draft: 'not allowed' }]) await assert.rejects(demo.prepare(invalid));
  await rejects(demo.open({ ...input, requestId: 'not-a-uuid' }), 'invalid_request_id');
  await rejects(demo.open({ ...input, provider: 'unknown' }), 'unknown_provider');
  await rejects(demo.open({ ...input, folderId: '/arbitrary/path' }), 'unknown_folder');
  await rejects(demo.open({ ...input, folderRevision: '0' }), 'stale_folder');
  await rejects(demo.open({ ...input, folderRevision: 1 }), 'stale_folder');
  for (const draft of [undefined, null, 10, {}, 'x'.repeat(8_001)]) await rejects(demo.open({ ...input, draft }), 'invalid_draft');
  for (const method of ['status', 'stopWaiting', 'keepWaiting']) await rejects(demo[method]('missing'), 'unknown_attempt');
  await rejects(open(fixture('confirm')), 'scenario_provider_mismatch');
  assert.throws(() => createNewChatFixture({ scenario: 'unknown' }), { code: 'invalid_scenario' });
  assert.throws(() => createNewChatFixture({ now: 1 }), { code: 'invalid_clock' });
  assert.throws(() => createNewChatFixture({ unexpected: true }), { code: 'unknown_field' });
  for (const invalid of [-1, NaN, Infinity, '1']) assert.throws(() => demo.advance(invalid), { code: 'invalid_advance' });
});

test('bounded memory retains issued and spent IDs and rejects new work at capacity', async () => {
  const demo = fixture('waiting');
  const input = await prepared(demo);
  const first = await demo.open(input);
  for (let number = 2; number <= 128; number++) await open(demo);
  await rejects(demo.prepare(choice), 'fixture_capacity');
  assert.deepEqual(await demo.open(input), first);
});

test('injected Date clock, rollback and detached responses preserve progress', async () => {
  let clock = START;
  const demo = createNewChatFixture({ scenario: 'added', now: () => new Date(clock) });
  const opened = await open(demo);
  opened.folder.id = 'tampered';
  clock += 1_000;
  const added = await demo.status(opened.attemptId);
  clock -= 10_000;
  assert.deepEqual(await demo.status(opened.attemptId), added);
  assert.equal(added.folder.id, 'fixture-design');
  await rejects(createNewChatFixture({ now: () => new Date('invalid') }).prepare(choice), 'invalid_clock');
});


test('UI opening failure retains verified existing identity without claiming room admission or offering recreation', async () => {
  const demo = createNewChatFixture({ scenario: 'failed', failurePhase: 'ui', now: () => START });
  const input = await prepared(demo);
  const result = await demo.open({ ...input, draft: 'Leave unsent.' });
  assert.equal(result.state, 'failed');
  assert.equal(result.creation.outcome, 'verified');
  assert.equal(result.launch.outcome, 'accepted'); // Creation receipt accepted; only UI failed.
  assert.deepEqual(result.uiOpen, { attempts: 1, outcome: 'rejected' });
  assert.equal(result.session, null);
  assert.equal(result.existingSession.folderId, choice.folderId);
  assert.equal(result.reason.code, 'session_created_ui_open_failed');
  assert.equal(result.capabilities.openExisting, false);
  demo.advance(180000);
  assert.deepEqual(await demo.open({ ...input, draft: 'Leave unsent.' }), result);
  assert.equal(JSON.stringify(result).includes('Leave unsent.'), false);
});
