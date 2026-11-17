// Fictional, in-memory page fixture. This module does not launch or contact anything.
export const SCENARIOS = Object.freeze([
  'waiting', 'confirm', 'added', 'timeout', 'mismatch', 'failed', 'unavailable',
]);

const WINDOW_MS = 120_000;
const RESULT_DELAY_MS = 1_000;
const INTENT_MS = 300_000;
const MAX_ATTEMPTS = 128;
const MAX_DRAFT_LENGTH = 8_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FOLDERS = [
  { id: 'fixture-design', revision: '1', label: 'Design lab', displayPath: '/fictional/projects/design-lab' },
  { id: 'fixture-notes', revision: '1', label: 'Field notes', displayPath: '/fictional/projects/field-notes' },
];
const PROVIDERS = [
  { id: 'codex', label: 'Codex', available: true, supportsDraft: true, unavailableReason: null },
  { id: 'claude_code', label: 'Claude Code', available: true, supportsDraft: false, unavailableReason: null },
  { id: 'antigravity', label: 'Antigravity', available: false, supportsDraft: false, unavailableReason: { code: 'antigravity_creation_unproven', message: 'Starting an Antigravity chat from Motet is not available yet.' } },
  { id: 'chatgpt_work', label: 'ChatGPT Work', available: false, supportsDraft: false, unavailableReason: { code: 'work_local_folder_unavailable', message: 'ChatGPT Work does not yet have a verified local-folder launch path in Motet.' } },
];
const TERMINAL = new Set(['added', 'mismatch', 'failed', 'unavailable']);
const HOST = { id: 'fixture-host', label: 'Demo workstation' };
const copy = value => JSON.parse(JSON.stringify(value));

export class NewChatFixtureError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'NewChatFixtureError';
    this.code = code;
  }
}

const fail = (code, message) => { throw new NewChatFixtureError(code, message); };

function exactObject(value, allowed, required, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    fail('invalid_input', `${label} must be a plain object.`);
  }
  if (Reflect.ownKeys(value).some(key => typeof key !== 'string' || !allowed.includes(key))) {
    fail('unknown_field', `${label} contains an unknown field.`);
  }
  if (required.some(key => !Object.hasOwn(value, key))) {
    fail('invalid_input', `${label} is missing a required field.`);
  }
}

/**
 * Create a bounded fictional adapter. `now` is a clock function returning epoch
 * milliseconds or a Date. `advance` adds an offset to that clock for page demos.
 */
export function createNewChatFixture(options = {}) {
  exactObject(options, ['scenario', 'now', 'failurePhase'], [], 'Fixture options');
  const { scenario = 'waiting', now = () => Date.now(), failurePhase = 'creation' } = options;
  if (!['creation', 'ui'].includes(failurePhase) || failurePhase === 'ui' && scenario !== 'failed') {
    fail('invalid_failure_phase', 'UI opening failure requires the failed scenario.');
  }
  if (!SCENARIOS.includes(scenario)) fail('invalid_scenario', 'Choose one of the seven fixture scenarios.');
  if (typeof now !== 'function') fail('invalid_clock', 'now must be a clock function.');

  const attempts = new Map();
  const intents = new Map();
  let offset = 0;
  let lastTime = -Infinity;
  let payloadKey;

  async function digestPayload(payload) {
    payloadKey ??= globalThis.crypto.subtle.importKey('raw',
      globalThis.crypto.getRandomValues(new Uint8Array(32)),
      { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const digest = await globalThis.crypto.subtle.sign('HMAC', await payloadKey,
      new TextEncoder().encode(payload));
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  }

  function time() {
    const value = now();
    const milliseconds = (value instanceof Date ? value.getTime() : value) + offset;
    if (typeof milliseconds !== 'number' || !Number.isFinite(milliseconds)
      || Math.abs(milliseconds) > 8.64e15 - INTENT_MS) {
      fail('invalid_clock', 'The fixture clock must return a valid date.');
    }
    // A wall-clock correction cannot undo a previously observed transition.
    lastTime = Math.max(lastTime, milliseconds);
    return lastTime;
  }

  function capabilities(attempt) {
    return {
      openExisting: false, // No reviewed recovery endpoint exists yet.
      keepWaiting: attempt.state === 'timeout' && !attempt.cancelled,
      stopWaiting: !attempt.cancelled && attempt.observation === 'active',
    };
  }

  function snapshot(attempt) {
    const { cancelled, resultAt, ...response } = attempt;
    return copy({ ...response, capabilities: capabilities(attempt) });
  }

  function update(attempt, milliseconds) {
    if (attempt.cancelled || TERMINAL.has(attempt.state) || attempt.state === 'timeout') return;
    const deadline = Date.parse(attempt.deadlineAt);
    // Status drives fixture observation. An event unprocessed before expiry is late.
    if (milliseconds < deadline && milliseconds >= attempt.resultAt) {
      if (scenario === 'added') {
        attempt.state = 'added';
        attempt.observation = 'complete';
        attempt.creation.outcome = 'verified';
        attempt.existingSession = { id: `fixture-session-${attempt.attemptId.slice(-4)}`, folderId: attempt.folder.id };
        attempt.session = {
          id: `fixture-session-${attempt.attemptId.slice(-4)}`,
          folderId: attempt.folder.id,
          roomAgentId: `fixture-agent-${attempt.attemptId.slice(-4)}`,
        };
      } else if (scenario === 'confirm') {
        attempt.state = 'confirm';
        attempt.reason = {
          code: 'native_folder_confirmation_required',
          message: 'Confirm the selected folder in Claude’s window.',
        };
      } else if (scenario === 'mismatch') {
        attempt.state = 'mismatch';
        attempt.creation.outcome = 'verified';
        attempt.observation = 'complete';
        attempt.mismatch = {
          sessionId: 'fixture-unmatched-session',
          displayPath: FOLDERS.find(folder => folder.id !== attempt.folder.id).displayPath,
          existingRoomAgentId: 'fixture-existing-other-agent',
        };
        attempt.reason = {
          code: 'folder_mismatch',
          message: 'The observed chat belongs to another folder. It was not added.',
        };
      }
    }
    if (!TERMINAL.has(attempt.state) && milliseconds >= deadline) {
      attempt.state = 'timeout';
      attempt.observation = 'stopped';
      attempt.reason = {
        code: 'session_not_seen',
        message: 'No matching new chat was verified within two minutes.',
      };
    }
  }

  function get(attemptId) {
    if (typeof attemptId !== 'string' || !attempts.has(attemptId)) {
      fail('unknown_attempt', 'The fixture attempt does not exist.');
    }
    const attempt = attempts.get(attemptId);
    update(attempt, time());
    return attempt;
  }

  async function context() {
    return copy({
      schemaVersion: 1,
      fixture: true,
      host: HOST,
      folders: FOLDERS,
      providers: PROVIDERS.map(provider => scenario === 'unavailable'
        ? { ...provider, available: false, unavailableReason: { code: 'desktop_unavailable', message: 'Launching is unavailable on this fictional host.' } }
        : provider),
      limits: { observationWindowMs: WINDOW_MS, intentLifetimeMs: INTENT_MS },
    });
  }

  function choice(input) {
    const provider = PROVIDERS.find(item => item.id === input.provider);
    if (!provider) fail('unknown_provider', 'Choose a provider from fixture context.');
    const folder = FOLDERS.find(item => item.id === input.folderId);
    if (!folder) fail('unknown_folder', 'Choose a folder from fixture context.');
    if (input.folderRevision !== folder.revision) fail('stale_folder', 'Refresh the folder inventory before opening.');
    return { provider, folder };
  }

  async function prepare(input) {
    exactObject(input, ['provider', 'folderId', 'folderRevision'],
      ['provider', 'folderId', 'folderRevision'], 'Prepare request');
    const { provider, folder } = choice(input);
    if (intents.size >= MAX_ATTEMPTS) fail('fixture_capacity', 'Create a fresh fixture after 128 intents.');
    const milliseconds = time();
    let requestId;
    do { requestId = globalThis.crypto.randomUUID(); } while (intents.has(requestId));
    const response = {
      schemaVersion: 1, fixture: true, requestId, provider: provider.id,
      folder: copy(folder), expiresAt: new Date(milliseconds + INTENT_MS).toISOString(), host: copy(HOST),
    };
    intents.set(requestId, { response, payloadDigest: null, attemptId: null });
    return copy(response);
  }

  async function open(input) {
    exactObject(input, ['requestId', 'provider', 'folderId', 'folderRevision', 'draft'],
      ['requestId', 'provider', 'folderId', 'folderRevision'], 'Open request');
    if (typeof input.requestId !== 'string' || !UUID.test(input.requestId)) {
      fail('invalid_request_id', 'requestId must be a UUID.');
    }
    const requestId = input.requestId.toLowerCase();
    const { provider, folder } = choice(input);
    const hasDraft = Object.hasOwn(input, 'draft');
    if (hasDraft && typeof input.draft !== 'string') {
      fail('invalid_draft', `draft must be a string of at most ${MAX_DRAFT_LENGTH} Unicode characters.`);
    }
    let draftLength = 0;
    if (hasDraft) {
      for (const character of input.draft) {
        if (++draftLength > MAX_DRAFT_LENGTH) {
          fail('invalid_draft', `draft must be a string of at most ${MAX_DRAFT_LENGTH} Unicode characters.`);
        }
      }
    }
    const provided = draftLength > 0;
    // Only a keyed digest survives this call; raw draft text is never retained.
    const payloadDigest = await digestPayload(JSON.stringify([
      provider.id, folder.id, folder.revision, provided, provided ? input.draft : null,
    ]));
    const intent = intents.get(requestId);
    if (!intent) fail('unknown_intent', 'Prepare a new intent before opening a chat.');
    if (intent.attemptId) {
      if (intent.payloadDigest !== payloadDigest) fail('request_conflict', 'This requestId was already used with a different payload.');
      return snapshot(get(intent.attemptId));
    }
    if (provided && !provider.supportsDraft) {
      fail('draft_unsupported', 'This provider does not support a first-message draft.');
    }
    if (intent.response.provider !== provider.id || intent.response.folder.id !== folder.id
      || intent.response.folder.revision !== folder.revision) {
      fail('intent_mismatch', 'The provider and folder must match the prepared intent.');
    }
    if (attempts.size >= MAX_ATTEMPTS) fail('fixture_capacity', 'Create a fresh fixture after 128 attempts.');
    if (scenario === 'confirm' && provider.available && provider.id !== 'claude_code') {
      fail('scenario_provider_mismatch', 'The native folder-confirmation scenario requires Claude Code.');
    }
    const milliseconds = time();
    if (milliseconds >= Date.parse(intent.response.expiresAt)) fail('intent_expired', 'Prepare a new intent; this one expired before launch.');
    const unavailable = scenario === 'unavailable' || !provider.available;
    const failed = !unavailable && scenario === 'failed';
    const uiFailed = failed && failurePhase === 'ui';
    const attemptId = `fixture-attempt-${String(attempts.size + 1).padStart(4, '0')}`;
    const attempt = {
      schemaVersion: 1, attemptId, requestId, provider: provider.id,
      folder: copy(folder), state: unavailable ? 'unavailable' : failed ? 'failed' : 'waiting',
      launch: { attempts: unavailable ? 0 : 1, outcome: unavailable ? 'not_attempted' : failed && !uiFailed ? 'rejected' : 'accepted' },
      creation: { outcome: uiFailed ? 'verified' : unavailable || failed ? 'not_attempted' : 'unknown' },
      uiOpen: { attempts: unavailable || failed && !uiFailed ? 0 : 1,
        outcome: unavailable || failed && !uiFailed ? 'not_attempted' : uiFailed ? 'rejected' : 'accepted' },
      existingSession: uiFailed ? { id: `fixture-session-${attemptId.slice(-4)}`, folderId: folder.id } : null,
      observation: unavailable || failed ? 'complete' : 'active',
      createdAt: new Date(milliseconds).toISOString(), deadlineAt: new Date(milliseconds + WINDOW_MS).toISOString(),
      session: null, mismatch: null,
      reason: unavailable
        ? { code: 'provider_unavailable', message: 'This provider cannot launch on the fictional fixture host.' }
        : uiFailed ? { code: 'session_created_ui_open_failed', message: 'The chat was created, but Codex could not be opened.' }
          : failed ? { code: 'native_dispatch_rejected', message: 'The fictional launcher rejected this attempt.' } : null,
      draft: { provided, disposition: !provided ? 'not_provided'
        : unavailable || failed ? 'unverified' : 'prefill_requested' },
      fixture: true, cancelled: false, resultAt: milliseconds + RESULT_DELAY_MS,
    };
    attempts.set(attemptId, attempt);
    intent.attemptId = attemptId;
    intent.payloadDigest = payloadDigest;
    return snapshot(attempt);
  }

  async function status(attemptId) { return snapshot(get(attemptId)); }

  async function keepWaiting(attemptId) {
    const attempt = get(attemptId);
    if (!capabilities(attempt).keepWaiting) fail('cannot_continue', 'Only an expired, unstopped observation can continue.');
    attempt.state = 'waiting';
    attempt.observation = 'active';
    attempt.deadlineAt = new Date(time() + WINDOW_MS).toISOString();
    attempt.reason = null;
    return snapshot(attempt);
  }

  async function stopWaiting(attemptId) {
    const attempt = get(attemptId);
    if (!TERMINAL.has(attempt.state)) {
      attempt.cancelled = true;
      attempt.observation = 'stopped';
    }
    return snapshot(attempt);
  }

  function advance(milliseconds) {
    if (typeof milliseconds !== 'number' || !Number.isFinite(milliseconds) || milliseconds < 0) {
      fail('invalid_advance', 'advance requires a finite, nonnegative number of milliseconds.');
    }
    const previous = offset;
    offset += milliseconds;
    try { time(); } catch (error) { offset = previous; throw error; }
  }

  return Object.freeze({ context, prepare, open, status, keepWaiting, stopWaiting, advance });
}
