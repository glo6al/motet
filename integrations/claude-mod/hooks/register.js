import {UUID, LIMITATION, TRANSITIONS, MAX_RECEIPT_REJECTIONS, pairing, sessionResponse, validClaim, matchingPrompt, matchingAnswer, turnIdentifier, publicStatus, globalConfiguration, receiptRecord, surfaceRoster, validNoticeClaim, noticeReceipt, lifecycleReceipt} from './protocol.js';

const GLOBAL_KEY = 'watch-owner-global-v1';
// Every grant a /watch-setup receives is also kept under a key of its own, named by its Motet and grant id, so a key
// only ever holds that one grant and no write can replace another's. Motet holds one active grant at a time (a setup
// revokes the others), so when the shared grant is refused as revoked, a newer grant can be found here and used,
// without another setup. A key is removed only for its own grant: once Motet refuses that grant, or once the copy
// that saved it saves a newer one from the same Motet (which revoked it). Nothing removes another grant's key.
const grantPrefix = 'watch-owner-grant-v1:';
const grantKey = config => grantPrefix + config.port + ':' + config.grant_id;
const legacyKey = id => 'watch-owner-v1:' + id;
const preferenceKey = id => 'watch-owner-preferences-v1:' + id;
// An owner disconnect Motet has not been told yet is kept under a key of its own, one per command, written once by
// the copy that made it. Nothing else ever writes it, and only its delivery (or its own copy's newer decision for
// the chat) removes it, so no other write, however late, can erase it.
const pendingPrefix = id => 'watch-owner-pending-v1:' + id + ':';
const legacyShardPrefix = id => 'watch-owner-runtime-v2:' + id + ':';
const shardPrefix = id => 'watch-owner-runtime-v3:' + id + ':';
const noticePrefix = id => 'watch-reply-notice-runtime-v1:' + id + ':';
const MAX_DATA = 4 * 1024 * 1024;
const REJECTED_RECEIPT_RETRY_MS = 30000;
const copy = value => JSON.parse(JSON.stringify(value));
const configurationId = value => value ? JSON.stringify([value.grant_id,value.grant,value.port]) : '';
const validConflictIds = value => value === undefined ||
  Array.isArray(value) && value.every(id => typeof id === 'string' && UUID.test(id));

// Containment by the host's own path rules, from the platform the host reports ($.session.info()), never guessed
// from how a path is spelled. POSIX (macOS, Linux, WSL) separates on '/' only, so '\' is an ordinary character and
// '/project\outside' or '/\share\project\outside' is a sibling. Windows separates on both slashes and needs a drive
// (C:\) or share (\\server\share) path; case is compared exactly, which is stricter. An unknown platform accepts
// the project folder itself only. '.', '..' and device or long-path prefixes (\\?\, \\.\) are refused.
const WINDOWS_PATH = /^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/?.][^\\/]*[\\/][^\\/]+)/;

async function pathRules($) {
  try {
    // Called, never tested with typeof: the host refuses a module that reads a $ event as a value. A host without
    // info() throws here, which counts as an unknown platform.
    const platform = (await $.session.info())?.platform;
    return platform === 'windows' ? 'windows' : ['macos', 'linux', 'wsl'].includes(platform) ? 'posix' : 'unknown';
  } catch { return 'unknown'; }
}

function insideFolder(path, root, rules) {
  if (typeof path !== 'string' || typeof root !== 'string' || !root || path.includes('\0') || root.includes('\0')) return false;
  if (path === root) return true;
  let sep;
  if (rules === 'posix') {
    if (!root.startsWith('/') || !path.startsWith('/')) return false;
    sep = '/';
  } else if (rules === 'windows') {
    if (!WINDOWS_PATH.test(root) || !WINDOWS_PATH.test(path)) return false;
    sep = '\\'; path = path.replace(/\//g, sep); root = root.replace(/\//g, sep);
    if (path === root) return true;
  } else return false;
  if (path.split(sep).some(part => part === '.' || part === '..')) return false;
  return path.startsWith(root.endsWith(sep) ? root : root + sep);
}

// The chat's stable project folder. `$.session.cwd()` follows a shell `cd` into a subfolder, which would make the
// same chat look like another project; `$.session.root()` does not. The root counts only while the current folder
// is the root or inside it by name; otherwise there is no project (null), which matches no enrollment, so a chat
// that left its project stops. A host without `root()`, or one whose `root()` fails, keeps the stricter `cwd()`
// comparison.
// Receipts still to report, judged over every record of one connection context at once: an event counts unless
// some record acknowledges it or Motet refused it for good (MAX_RECEIPT_REJECTIONS times) in any record, the new
// receipt-only record included. A context with any keeps its earlier folder until they are reported or refused.
async function pendingReceipts(records) {
  const acknowledged = Object.assign({}, ...records.map(record => record?.acknowledged || {}));
  const refused = new Map();
  for (const record of records) for (const request of Object.values(record?.requests || {}))
    for (const [hash, rejection] of Object.entries(request?.rejections || {}))
      refused.set(hash, Math.max(refused.get(hash) ?? 0, rejection?.count ?? 0));
  for (const record of records) for (const request of Object.values(record?.requests || {})) {
    for (const value of Array.isArray(request?.events) ? request.events : []) {
      const hash = await fingerprint(canonicalReceipt(value));
      if (acknowledged[hash] || acknowledged[await fingerprint(value)]) continue;
      if ((refused.get(hash) ?? 0) >= MAX_RECEIPT_REJECTIONS) continue;
      return true;
    }
  }
  return false;
}

const receiptContext = (mode, project, grant) => JSON.stringify([mode, project, grant ?? null]);

// An automatic connection made by an earlier plugin from a folder inside this project, whose retained outcomes are
// not all reported yet: its folder, so the plugin reconnects there for receipts only before moving. A context Motet
// no longer holds (receipt_context_missing) is given up for this runtime; its records stay for recovery.
async function earlierReceipts($, state, id, project, config) {
  const keys = await $.store.keys();
  if (!Array.isArray(keys) || keys.length > 4096) return null;
  const rules = await pathRules($), contexts = new Map();
  const add = record => {
    if (!record || record.mode !== 'derived' || record.native_id !== id || record.grant_id !== config.grant_id ||
        record.project === project || !insideFolder(record.project, project, rules) ||
        state.abandonedReceipts.has(receiptContext('derived', record.project, config.grant_id))) return;
    const scope = JSON.stringify([record.project, record.enrollment_id]);
    if (!contexts.has(scope)) contexts.set(scope, []);
    contexts.get(scope).push(record);
  };
  for (const key of keys) {
    if (typeof key !== 'string' || ![shardPrefix(id), legacyShardPrefix(id)].some(prefix => key.startsWith(prefix))) continue;
    add(await $.store.get(key));
  }
  if (state.receiptOnly) add(state.data);              // the live receipt-only record, possibly newer than storage
  for (const records of contexts.values()) if (await pendingReceipts(records)) return records[0].project;
  return null;
}

// A refused registration is not tried again from the same place, so a refusal that repeats cannot storm Motet.
// The place is the chat's actual folder, not only its project: Motet judges the folder the chat is in, so after a
// move (back to the project folder, say) the chat registers again.
async function refusalKey($, state, id, project, config) {
  const cwd = await $.session.cwd().catch(()=>null);
  return JSON.stringify([id, project, cwd, state.folderProtocol, configurationId(config)]);
}

async function sessionProject($) {
  // A host without root() throws here, like one whose root() fails: both keep the stricter current folder.
  let root = null;
  try { root = await $.session.root(); } catch { /* fall back to the stricter current folder */ }
  if (typeof root === 'string' && root) {
    const cwd = await $.session.cwd();
    return insideFolder(cwd, root, await pathRules($)) ? root : null;
  }
  return $.session.cwd();
}

function trimHistory(state, data) {
  const requests = Object.values(data.requests);
  // Held proof keeps its own space within the byte cap, independently of history.
  const held = request => Object.values(request.rejections || {}).some(result => result.count >= MAX_RECEIPT_REJECTIONS) ||
    data.conflict_ids?.includes(request.request_id);
  let excess = requests.filter(request => !held(request)).length - 32;
  for (const request of requests) {
    if (excess <= 0) break;
    if (!held(request) && request !== state.flight && !request.events.length && (request.execution_completed || ['answered','dropped'].includes(request.phase))) {
      delete data.requests[request.request_id]; excess -= 1;
    }
  }
}

// Keep every official API call in a top-level $ function: the native compiler scans them.
async function save($, state, change) {
  const data = state.data, key = state.key;
  if (!data || !key) throw Error('Missing Motet receipt context.');
  const operation = state.writes.then(async () => {
    if (change) change(data);
    trimHistory(state, data);
    if (JSON.stringify(data).length > MAX_DATA) throw Error('Motet recovery data limit.');
    await $.store.set(key, copy(data));
  });
  state.writes = operation.catch(() => {});
  return operation;
}

function motetAnswer(response) {
  if (!response.ok) {
    const error = Error('Motet service unavailable.');
    let value;
    try {if (typeof response.text === 'string' && response.text.length <= 650000) value = JSON.parse(response.text);}
    catch { /* The HTTP status still distinguishes a request rejection. */ }
    error.code = typeof value?.error_code === 'string' ? value.error_code : '';
    error.status = response.status;
    throw error;
  }
  if (typeof response.text !== 'string' || response.text.length > 650000) throw Error('Invalid Motet response.');
  return JSON.parse(response.text);
}

// A ticket reservation, or a read of a chat's latest decision, goes to Motet outside the queue below: neither changes
// connection state, and a reservation's moment of arrival is the command's place in the owner's order, so it must
// not wait behind a poll, a registration or a receipt. One copy's reservations go out one at a time, in the order
// the owner gave the commands, on a chain of their own (queueReservation).
async function reservationPost($, body, credential) {
  return motetAnswer(await $.http.fetch('http://127.0.0.1:' + credential.port + '/claude-mod/decision',
    {method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer ' + credential.token},body:JSON.stringify(body)}));
}

async function post($, state, path, body, options = {}) {
  const port = options.port ?? state.data?.port;
  const token = options.anonymous ? null : (options.token ?? state.data?.token);
  const headers = {'Content-Type':'application/json'};
  if (token) headers.Authorization = 'Bearer ' + token;
  // Serialize network requests, including detached event/disconnect calls. The official
  // host bounds one fetch (redirects and body included) to 30 seconds.
  const operation = state.network.then(async () => {
    state.networkBusy = true;
    try {
      return motetAnswer(await $.http.fetch('http://127.0.0.1:' + port + '/claude-mod/' + path,
        {method:'POST',headers,body:JSON.stringify(body)}));
    } finally {state.networkBusy = false;}
  });
  state.network = operation.catch(() => {});
  return operation;
}

function identityBody(state, data = state.data, generation = state.generation) {
  return {generation,native_id:data.native_id,...data.mode === 'derived' ? {client_id:state.clientId} : {}};
}

function stop(state) {
  state.connected = false;
  state.peerProtocol = 0;
  state.noticeProtocol = 0;
  state.lifecycleProtocol = 0;
  state.recoverableTurns = [];
  state.epoch += 1;
}

function backoff(state, now) {
  state.failures += 1;
  state.retryAt = now + Math.min(30000, 2000 * 2 ** Math.min(state.failures - 1,4));
}

function suspend($, state) {
  const wasConnected = state.connected;
  const data = state.data;
  const body = data ? identityBody(state) : null;
  stop(state);
  if (wasConnected && data) {
    post($, state, 'disconnect', body, {port:data.port,token:data.token}).catch(() => {});
  }
}

async function fingerprint(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value)));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2,'0')).join('');
}

function canonicalReceipt(value) {
  if (value.kind !== 'completed') return value;
  const {request_id, attempt_id, kind, turn_id, outcome, answer, event_id, occurred_at} = value;
  return {request_id, attempt_id, kind, turn_id, outcome, answer, event_id, occurred_at};
}

function isUsableCompletion(value) {
  return value.kind === 'completed' && value.outcome === 'answered' && matchingAnswer(value.answer, value.request_id);
}

async function event($, state, request, kind, extras = {}, reason = '') {
  const data = state.data, at = await $.clock.now();
  if (state.data !== data || data.requests[request.request_id] !== request) throw Error('Motet receipt identity changed.');
  await save($, state, () => {
    if (request.execution_completed || (request.completed && kind !== 'answered')) return;
    if (kind === 'submitted' && ['started','answered','unknown','dropped'].includes(request.phase)) return;
    if (kind === 'answered') {request.completed = true; request.execution_completed = true; request.capture = 'answered';}
    if (kind === 'dropped') request.execution_completed = true;
    if (kind === 'completed') {
      request.execution_completed = true; request.capture = extras.outcome;
      request.completed = extras.outcome === 'answered';
    }
    request.phase = kind;
    request.journal = [...(request.journal || []),{at,kind,reason:kind==='completed'?extras.outcome:reason}].slice(-16);
    const value = {request_id:request.request_id,attempt_id:request.attempt_id,kind,...extras};
    if (!request.events.some(item => JSON.stringify(item) === JSON.stringify(value))) request.events.push(value);
  });
  if (state.data === data) flush($, state).catch(() => {state.error = 'Motet event delivery interrupted; retained receipts will be retried without resubmitting.';});
}

async function flush($, state, retryRequest = null) {
  if (state.flushing || state.recovering || !state.connected) return;
  const data = state.data, epoch = state.epoch;
  state.flushing = true;
  try {
    for (const request of retryRequest ? [retryRequest] : Object.values(data.requests)) {
      if (data.conflict_ids?.includes(request.request_id)) continue;
      let index = 0;
      while (index < request.events.length) {
        if (state.recovering || !state.connected || state.data !== data || state.epoch !== epoch) return;
        const value = request.events[index], receipt = canonicalReceipt(value);
        let hash = Object.keys(request.rejections || {}).length ? await fingerprint(receipt) : null;
        const rejection = request.rejections?.[hash];
        const held = rejection?.count >= MAX_RECEIPT_REJECTIONS;
        const deferral = state.transientReceiptRetries.get(value);
        const retryAt = Math.max(rejection?.retry_at ?? 0, deferral?.retry_at ?? 0);
        if (retryRequest ? !held : held || (retryAt && await $.clock.now() < retryAt)) {
          index += 1;
          continue;
        }
        if (state.recovering || !state.connected || state.data !== data || state.epoch !== epoch) return;
        if (value.kind === 'completed' && state.lifecycleProtocol !== 2) {
          state.error = 'Motet lifecycle recovery requires a compatible bridge.';
          index += 1;
          continue;
        }
        // Establish an earlier terminal disposition before a supplemental answer.
        // In particular, a withheld disposition must survive a reordered shard or retry.
        if (isUsableCompletion(receipt) && request.events.some(item => item.kind === 'completed' && !isUsableCompletion(item))) {
          index += 1;
          continue;
        }
        try {
          await post($, state, 'event', {...receipt,...identityBody(state)});
        } catch (error) {
          const requestError = [400,409].includes(error.status);
          if (error.status === 503 || (requestError && error.code === '')) {
            // Older bridges used untyped 400 even for storage faults. Defer
            // this event without charging its durable rejection budget.
            const now = await $.clock.now();
            state.transientReceiptRetries.set(value, {
              first_deferred_at:held ? undefined : (deferral?.first_deferred_at ?? now),
              retry_at:now + REJECTED_RECEIPT_RETRY_MS
            });
            index += 1;
            continue;
          }
          if (!requestError || !['receipt_rejected','invalid_request'].includes(error.code)) throw error;
          if (state.data !== data || state.epoch !== epoch) return;
          const retry_at = (await $.clock.now()) + REJECTED_RECEIPT_RETRY_MS;
          hash ??= await fingerprint(receipt);
          if (state.data !== data || state.epoch !== epoch) return;
          await save($, state, () => {
            request.rejections ??= {};
            request.rejections[hash] = {count:Math.min((rejection?.count || 0) + 1, MAX_RECEIPT_REJECTIONS), retry_at};
            // Keep the existing cooldown. The owner status follows the typed classification.
            if (deferral) delete deferral.first_deferred_at;
          });
          index += 1;
          continue;
        }
        if (state.data !== data || state.epoch !== epoch) return;
        if (data.mode === 'derived') hash ??= await fingerprint(receipt);
        if (state.data !== data || state.epoch !== epoch) return;
        await save($, state, () => {
          if (data.mode === 'derived') data.acknowledged[hash] = true;
          if (request.rejections) delete request.rejections[hash];
          if (request.events[index] === value) {
            request.events.splice(index, 1);
            state.transientReceiptRetries.delete(value);
          }
        });
      }
    }
  } finally {state.flushing = false;}
}

function validateReceiptIdentity(identities, id, record) {
  const prior = identities.get(id);
  const {attempt_id, envelope_digest, generation} = record;
  const turns = [record.turn_id, ...record.events.map(value => value.turn_id)].filter(value => value !== undefined);
  const turn = turns[0];
  if (turns.some(value => !turnIdentifier(value) || value !== turn) ||
      generation !== undefined && (typeof generation !== 'string' || !UUID.test(generation)) ||
      prior && (prior.attempt_id !== attempt_id || prior.envelope_digest !== envelope_digest ||
        prior.generation && generation && prior.generation !== generation ||
        prior.turn && turn && prior.turn !== turn)) throw Error('Conflicting Motet receipt identity.');
  const answer = record.completed ? record.events.find(value => value.kind === 'answered') : undefined;
  if (prior?.answer && answer && JSON.stringify(prior.answer) !== JSON.stringify(answer)) throw Error('Conflicting Motet answer.');
  const completionIds = prior?.completionIds ?? {};
  for (const value of record.events) {
    if (value.kind !== 'completed') continue;
    const role = isUsableCompletion(value) ? 'answer' : 'terminal';
    if (completionIds[role] && completionIds[role] !== value.event_id) throw Error('Conflicting Motet completion receipt.');
    completionIds[role] = value.event_id;
  }
  identities.set(id, {attempt_id, envelope_digest, generation:generation ?? prior?.generation,
    turn:turn ?? prior?.turn, answer:answer ?? prior?.answer, completionIds});
}

function checkReceiptIdentity(identities, completions, conflicts, id, record) {
  try {validateReceiptIdentity(identities, id, record);}
  catch {conflicts.add(id);}
  for (const value of record.events) {
    if (value.kind !== 'completed') continue;
    const content = JSON.stringify(canonicalReceipt(value));
    const prior = completions.get(value.event_id);
    if (prior && prior.content !== content) {
      conflicts.add(id);
      conflicts.add(prior.id);
    }
    if (!prior) completions.set(value.event_id, {id,content});
  }
}

async function recover($, state) {
  if (state.data.mode !== 'derived') return;
  state.recovering = true;
  try {await recoverReceipts($, state);}
  finally {state.recovering = false;}
}

async function recoverReceipts($, state) {
  const data = state.data;
  const keys = await $.store.keys();
  if (!Array.isArray(keys) || keys.length > 4096) throw Error('Motet recovery data limit.');
  let total = JSON.stringify(data).length;
  const shards = [];
  for (const key of keys) {
    if (typeof key !== 'string' || key === state.key) continue;
    const prefix = [shardPrefix(data.native_id),legacyShardPrefix(data.native_id)].find(prefix=>key.startsWith(prefix));
    if (!prefix) continue;
    if (!UUID.test(key.slice(prefix.length))) throw Error('Invalid Motet receipt namespace.');
    const shard = await $.store.get(key);
    total += JSON.stringify(shard ?? null).length;
    if (total > MAX_DATA) throw Error('Motet recovery data limit.');
    if (!shard || shard.mode !== 'derived' || shard.native_id !== data.native_id ||
        shard.project !== data.project || shard.grant_id !== data.grant_id || shard.enrollment_id !== data.enrollment_id) continue;
    if (!shard.requests || typeof shard.requests !== 'object' || Array.isArray(shard.requests) ||
        !validConflictIds(shard.conflict_ids)) throw Error('Invalid Motet receipt data.');
    shards.push({...shard, legacy:prefix===legacyShardPrefix(data.native_id)});
  }
  // Validate before writing; foreign runtime keys are read-only, even after lease takeover.
  const identities = new Map(), completions = new Map(), conflicts = new Set(data.conflict_ids);
  for (const shard of shards) for (const id of shard.conflict_ids || []) conflicts.add(id);
  for (const [id,record] of Object.entries(data.requests)) checkReceiptIdentity(identities, completions, conflicts, id, record);
  for (const shard of shards) for (const [id,record] of Object.entries(shard.requests)) {
    if (!(shard.legacy?receiptRecord(record,id):lifecycleReceipt(record,id))) throw Error('Invalid Motet receipt data.');
    checkReceiptIdentity(identities, completions, conflicts, id, record);
  }
  data.conflict_ids = [...conflicts];
  for (const shard of shards) for (const [id,record] of Object.entries(shard.requests)) {
    if (conflicts.has(id)) continue;
    let current = data.requests[id];
    if (!current) {
      current = {...copy(record),events:[],rejections:{},phase:record.execution_completed?'completed':record.completed?'answered':'unknown'};
      data.requests[id] = current;
    }
    if (!current.turn_id && record.turn_id) current.turn_id = record.turn_id;
    if (record.execution_completed && !current.execution_completed) {current.execution_completed=true; current.capture=record.capture; current.phase='completed';}
    if (record.completed && current.capture!=='withheld') {current.completed = true; current.phase = record.lifecycle_protocol===2?'completed':'answered';}
    for (const value of record.events) {
      const receipt = canonicalReceipt(value), content = JSON.stringify(receipt);
      const hash = await fingerprint(receipt);
      if (data.acknowledged[hash]) continue;
      // Old clients acknowledged the original property order.
      if (value.kind === 'completed' && content !== JSON.stringify(value) && data.acknowledged[await fingerprint(value)]) continue;
      const retainedEvent = current.events.find(item => JSON.stringify(canonicalReceipt(item)) === content);
      if (!retainedEvent) {
        if ((current.execution_completed || current.completed) && !['answered','completed','dropped'].includes(value.kind)) continue;
        current.events.push(copy(receipt));
      }
      const rejection = record.rejections?.[hash], previous = current.rejections?.[hash];
      if (rejection && (!previous || rejection.count > previous.count ||
          rejection.count === previous.count && rejection.retry_at > previous.retry_at)) {
        current.rejections ??= {};
        current.rejections[hash] = copy(rejection);
        const deferral = retainedEvent && state.transientReceiptRetries.get(retainedEvent);
        if (deferral && rejection.count >= MAX_RECEIPT_REJECTIONS) delete deferral.first_deferred_at;
      }
    }
    if (!current.execution_completed && !current.completed && current !== state.flight && !['dropped','unknown'].includes(record.phase)) {
      const value = {request_id:id,attempt_id:record.attempt_id,kind:'unknown'};
      const hash = await fingerprint(value);
      if (!data.acknowledged[hash] && !current.events.some(item => item.kind === 'unknown')) current.events.push(value);
    }
  }
  for (const request of Object.values(data.requests)) {
    if (!request.rejections || conflicts.has(request.request_id)) continue;
    const retained = new Set(await Promise.all(request.events.map(value=>fingerprint(canonicalReceipt(value)))));
    for (const hash of Object.keys(request.rejections)) {
      if (!retained.has(hash)) delete request.rejections[hash];
    }
    if (!Object.keys(request.rejections).length) delete request.rejections;
  }
  if (state.data !== data) return;
  await save($, state);
}

async function saveNotices($,state,change) {
  const data=state.noticeData,key=state.noticeKey;
  if (!data || !key) throw Error('Missing Motet notice context.');
  const operation=state.writes.then(async()=>{
    if(change)change(data);
    for(const [id,r] of Object.entries(data.requests))if(Object.keys(data.requests).length>32 && !r.events.length && r!==state.flight && ['submitted','dropped','unknown'].includes(r.phase))delete data.requests[id];
    if(JSON.stringify(data).length>MAX_DATA)throw Error('Motet recovery data limit.');
    await $.store.set(key,copy(data));
  });
  state.writes=operation.catch(()=>{});return operation;
}
async function noticeEvent($,state,request,kind) {
  await saveNotices($,state,()=>{
    if(['submitted','dropped','unknown'].includes(request.phase))return;
    request.phase=kind;
    request.events.push({request_id:request.request_id,attempt_id:request.attempt_id,kind});
  });
  flushNotices($,state).catch(()=>{});
}
async function flushNotices($,state) {
  if(state.noticeFlushing || !state.connected || state.noticeProtocol!==1 || !state.noticeData)return;
  const data=state.noticeData,epoch=state.epoch;state.noticeFlushing=true;
  try {
    for(const request of Object.values(data.requests))while(request.events.length && state.connected && state.noticeProtocol===1 && state.noticeData===data && state.epoch===epoch){
      const value=request.events[0];await post($,state,'event',{...value,...identityBody(state)});
      if(state.noticeData!==data || state.epoch!==epoch || state.noticeProtocol!==1)return;
      const hash=await fingerprint(value);
      await saveNotices($,state,()=>{data.acknowledged[hash]=true;if(request.events[0]===value)request.events.shift();});
    }
  } finally {state.noticeFlushing=false;}
}
async function recoverNotices($,state) {
  if(state.noticeProtocol!==1)return;
  if(state.noticeData && (state.submitting || state.flight?.kind==='peer_reply_notice' || state.noticeFlushing))return;
  const ordinary=state.data,prefix=noticePrefix(ordinary.native_id),key=prefix+state.clientId;
  const keys=await $.store.keys();if(!Array.isArray(keys)||keys.length>4096)throw Error('Motet recovery data limit.');
  let total=0;const records={},acknowledged={};
  for(const k of keys){
    if(typeof k!=='string'||!k.startsWith(prefix))continue;
    if(!UUID.test(k.slice(prefix.length)))throw Error('Invalid Motet notice namespace.');
    const shard=await $.store.get(k);total+=JSON.stringify(shard??null).length;
    if(total>MAX_DATA)throw Error('Motet recovery data limit.');
    if(shard?.kind!=='peer_reply_notice'||shard.native_id!==ordinary.native_id||shard.project!==ordinary.project||shard.grant_id!==ordinary.grant_id||shard.enrollment_id!==ordinary.enrollment_id)continue;
    if(!shard.requests||!shard.acknowledged||typeof shard.requests!=='object'||Array.isArray(shard.requests)||typeof shard.acknowledged!=='object'||Array.isArray(shard.acknowledged))throw Error('Invalid Motet notice receipts.');
    for(const [hash,v] of Object.entries(shard.acknowledged)){if(!/^[a-f0-9]{64}$/.test(hash)||v!==true)throw Error('Invalid Motet notice ACK.');acknowledged[hash]=true;}
    for(const [id,r] of Object.entries(shard.requests)){
      if(!noticeReceipt(r,id))throw Error('Invalid Motet notice receipt.');
      if(records[id] && (records[id].attempt_id!==r.attempt_id||records[id].envelope_digest!==r.envelope_digest||records[id].public_request_id!==r.public_request_id))throw Error('Conflicting Motet notice receipt.');
      if(!records[id])records[id]=copy(r);
      else for(const e of r.events)if(!records[id].events.some(v=>JSON.stringify(v)===JSON.stringify(e)))records[id].events.push(copy(e));
    }
  }
  for(const r of Object.values(records)){
    const terminal=r.events.find(e=>['submitted','dropped','unknown'].includes(e.kind));
    if(terminal)r.phase=terminal.kind;
    if(!['submitted','dropped','unknown'].includes(r.phase)){
      r.phase='unknown';r.events.push({request_id:r.request_id,attempt_id:r.attempt_id,kind:'unknown'});
    }
    const pending=[];for(const e of r.events)if(!acknowledged[await fingerprint(e)])pending.push(e);r.events=pending;
  }
  state.noticeKey=key;state.noticeData={kind:'peer_reply_notice',native_id:ordinary.native_id,project:ordinary.project,grant_id:ordinary.grant_id,enrollment_id:ordinary.enrollment_id,requests:records,acknowledged};
  await saveNotices($,state);await flushNotices($,state);
}
async function submitNotice($,state,request,envelope,epoch) {
  // Compatibility is ACK-only: an old server may still offer an admitted notice.
  // Never call prompt.submit, including after recovery or a protocol downgrade.
  try {
    await noticeEvent($,state,request,'dropped');
  }catch{try{await noticeEvent($,state,request,'unknown');}catch{}}
  finally{state.submitting=false;if(state.flight===request){state.flight=null;state.envelope=null;}}
}

// A claim may run only in the folder it was granted for: the folder this chat reported in the poll that carried
// the claim (Motet resolved it, links included, before granting), or, with an older Motet that does not check
// folders, the project folder itself.
async function claimFolderHolds($, state, granted) {
  if (await sessionProject($) !== state.data.project) return false;
  const cwd = await $.session.cwd();
  return state.folderProtocol === 1 && typeof granted === 'string' ? cwd === granted : cwd === state.data.project;
}

async function failRequest($, state, request) {
  try {if(request.kind==='peer_reply_notice')await noticeEvent($,state,request,'unknown');else await event($,state,request,'unknown');} catch { /* Server claim remains attempted. */ }
}

async function submit($, state, request, envelope, epoch, granted = null) {
  try {
    const id = await $.session.id(), now = await $.clock.now();
    if (!state.connected || state.epoch !== epoch || id !== state.data.native_id || !await claimFolderHolds($,state,granted)) {await failRequest($,state,request); return;}
    if (now / 1000 >= request.expires_at) {await event($,state,request,'dropped'); return;}
    await save($,state,()=>{request.attempted = true; request.phase = 'pending';});
    if (!state.connected || state.epoch !== epoch || await $.session.id() !== state.data.native_id || !await claimFolderHolds($,state,granted)) {await failRequest($,state,request); return;}
    state.envelope = envelope;
    const result = await $.prompt.submit({text:envelope,asUser:false});
    if (typeof result?.drop === 'string') {
      // A turn receipt outranks a late submit result. Never release a peer's
      // execution slot using a pre-start claim after native execution started.
      await event($,state,request,request.origin === 'peer' && request.turn_id ? 'unknown' : 'dropped');
    }
    else await event($,state,request,'submitted');
  } catch {await failRequest($,state,request);}
  finally {
    state.submitting = false;
    if (state.flight === request && (request.execution_completed || ['answered','dropped'].includes(request.phase) ||
        request.phase === 'unknown' && !(request.origin === 'peer' && request.turn_id))) {state.flight = null; state.envelope = null;}
  }
}

async function poll($, state) {
  const epoch = state.epoch;
  const folder = state.folderProtocol === 1 ? await $.session.cwd() : null;
  const response = await post($,state,'poll',{...identityBody(state),project:state.data.project,
    ...(typeof folder === 'string' ? {cwd:folder} : {}),accept:!state.receiptOnly && !state.flight && !state.submitting});
  if (!sessionResponse({...response,heartbeat_interval_ms:2000,lease_seconds:12},state.data.native_id) || response.generation !== state.generation) throw Error('Invalid Motet poll identity.');
  if (!state.connected || state.epoch !== epoch) return;
  const turns = response.recoverable_turns;
  state.recoverableTurns = Array.isArray(turns) && turns.length <= 32 ? turns : [];
  await completeRecoveredTurns($, state);
  if (!state.connected || state.epoch !== epoch || !response.claim) return;
  const claim = response.claim;
  if(claim.kind!==undefined){
    if(!validNoticeClaim(claim,{...state.data,generation:state.generation,reply_notice_protocol:state.noticeProtocol},await $.clock.now())||!state.noticeData)return;
    if(state.noticeData.requests[claim.request_id])return;
    const request={kind:'peer_reply_notice',request_id:claim.request_id,public_request_id:claim.public_request_id,attempt_id:claim.attempt_id,expires_at:claim.expires_at,phase:'claimed',events:[],envelope_digest:await fingerprint(claim.envelope)};
    await saveNotices($,state,data=>{data.requests[request.request_id]=request;});
    state.flight=request;
    state.submitting=true;submitNotice($,state,request,claim.envelope,epoch).catch(()=>{});return;
  }
  if (state.data.requests[claim.request_id]) {await failRequest($,state,state.data.requests[claim.request_id]); return;}
  const request = {request_id:claim.request_id,attempt_id:claim.attempt_id,expires_at:claim.expires_at,lifecycle_protocol:state.lifecycleProtocol,origin:claim.origin??'owner',
    generation:state.generation,attempted:false,phase:'claimed',events:[]};
  if (!validClaim(claim,{...state.data,generation:state.generation,peer_protocol:state.peerProtocol,peer_envelope_limit:state.peerEnvelopeLimit},await $.clock.now())) {
    if (UUID.test(claim.request_id) && UUID.test(claim.attempt_id)) {
      request.envelope_digest = await fingerprint(claim.envelope ?? '');
      await save($,state,data=>{data.requests[request.request_id] = request;});
      await event($,state,request,'dropped');
    }
    return;
  }
  request.envelope_digest = await fingerprint(claim.envelope);
  state.flight = request;
  await save($,state,data=>{data.requests[request.request_id] = request;});
  state.submitting = true;
  // The granted folder travels with this one submission and nowhere else, so nothing accumulates per claim.
  submit($,state,request,claim.envelope,epoch,typeof folder === 'string' ? folder : null).catch(()=>{});
}

async function activate($, state, response, eligibilityEpoch) {
  checkEligibility(state,eligibilityEpoch);
  if (!sessionResponse(response,state.data.native_id)) throw Error('Invalid Motet connection identity.');
  if (await $.session.id() !== state.data.native_id || !state.receiptOnly && await sessionProject($) !== state.data.project) throw Error('Native conversation changed.');
  checkEligibility(state,eligibilityEpoch);
  state.generation = response.generation;
  // Only this current derived connection can negotiate peer input. Saved receipts and
  // manual owner enrollments cannot enable it after reload or through a service echo.
  state.noticeProtocol = state.data.mode==='derived' && response.reply_notice_protocol===1 ? 1 : 0;
  state.noticeData=null;state.noticeKey=null;
  state.peerProtocol = state.data.mode === 'derived' && response.peer_protocol === 1 ? 1 : 0;
  state.peerEnvelopeLimit = state.data.mode === 'derived' && response.peer_envelope_limit === 100000 ? 100000 : 4000;
  // Folder protocol 1: polls report this chat's current folder, which Motet resolves before a claim in a subfolder.
  // Without it (an older Motet) only the project folder itself may receive.
  state.folderProtocol = response.folder_protocol === 1 ? 1 : 0;
  state.lifecycleProtocol = state.data.mode === 'derived' && response.lifecycle_protocol === 2 ? 2 : 0;
  if (response.decision_protocol === 1) state.decisionProtocol = 1;
  await save($,state,data=>{checkEligibility(state,eligibilityEpoch); data.active = true; data.generation = response.generation; data.peer_protocol = state.peerProtocol;});
  checkEligibility(state,eligibilityEpoch);
  state.connected = true; state.error = ''; state.failures = 0; state.retryAt = 0;
  if (state.data.mode === 'derived') await recover($,state);
  checkEligibility(state,eligibilityEpoch);
  for (const request of Object.values(state.data.requests)) {
    if (!state.data.conflict_ids?.includes(request.request_id) && request !== state.flight &&
        !request.execution_completed && !['answered','dropped','unknown'].includes(request.phase)) await event($,state,request,'unknown',{},'reconnect');
  }
  if (!state.submitting && state.flight && (state.flight.execution_completed || ['dropped','answered'].includes(state.flight.phase))) {state.flight = null; state.envelope = null;}
  state.recoverAt = (await $.clock.now()) + 10000;
  await flush($,state);
  if(state.noticeProtocol===1)await recoverNotices($,state);
}

async function connectDerived($, state, id, project, config, eligibilityEpoch) {
  state.config = config;
  async function registration(notices=false,lifecycle=false,largePeer=false){
    // Reserve before I/O, including while not connected: lost commits never reuse a tuple's episode.
    if(!Number.isSafeInteger(state.sequence+1))throw Error('Motet connection sequence exhausted.');
    const sequence=++state.sequence;
    const body={native_id:id,project,client_id:state.clientId,connection_seq:sequence,...state.receiptOnly?{receipt_only:true}:{},peer_protocol:1,...notices?{reply_notice_protocol:1}:{},...lifecycle?{lifecycle_protocol:2}:{},...largePeer?{peer_envelope_limit:100000}:{}};
    const response=await post($,state,'register',body,{port:config.port,token:config.grant});
    checkEligibility(state,eligibilityEpoch);
    if(!sessionResponse(response,id)||response.grant_id!==config.grant_id||!UUID.test(response.enrollment_id)||
       !/^[a-f0-9]{64}$/.test(response.token)||response.client_id!==state.clientId||response.connection_seq!==sequence)throw Error('Invalid Motet registration.');
    if(notices && response.reply_notice_protocol!==1)throw Error('Invalid Motet notice negotiation.');
    if(lifecycle && response.lifecycle_protocol!==2)throw Error('Invalid Motet lifecycle negotiation.');
    if(largePeer && response.peer_envelope_limit!==100000)throw Error('Invalid Motet peer capacity negotiation.');
    return response;
  }
  let response=await registration();
  if(response.supported_reply_notice_protocol===1 || response.supported_lifecycle_protocol===2 || response.supported_peer_envelope_limit===100000)response=await registration(response.supported_reply_notice_protocol===1,response.supported_lifecycle_protocol===2,response.supported_peer_envelope_limit===100000);
  if (await $.session.id() !== id || !state.receiptOnly && await sessionProject($) !== project) throw Error('Native conversation changed.');
  checkEligibility(state,eligibilityEpoch);
  const same = state.data?.mode === 'derived' && state.data.native_id === id && state.data.project === project &&
    state.data.grant_id === config.grant_id && state.data.enrollment_id === response.enrollment_id;
  if (!same) {
    await state.writes;
    const scope = JSON.stringify([id,project,config.grant_id,response.enrollment_id]);
    let key = state.shardKeys.get(scope) ?? shardPrefix(id) + state.clientId;
    // Returning to A after visiting B reuses this runtime's A namespace. Read
    // its retained receipts before any activation write can replace that row.
    const stored = await $.store.get(key);
    checkEligibility(state,eligibilityEpoch);
    let requests = {}, acknowledged = {}, conflict_ids = [];
    if (stored?.mode === 'derived' && stored.native_id === id && stored.project === project &&
        stored.grant_id === config.grant_id && stored.enrollment_id === response.enrollment_id) {
      if (JSON.stringify(stored).length > MAX_DATA || !stored.requests || typeof stored.requests !== 'object' || Array.isArray(stored.requests) ||
          !stored.acknowledged || typeof stored.acknowledged !== 'object' || Array.isArray(stored.acknowledged) ||
          !Object.entries(stored.requests).every(([requestId,record])=>lifecycleReceipt(record,requestId)) ||
          !Object.entries(stored.acknowledged).every(([hash,value])=>/^[a-f0-9]{64}$/.test(hash) && value === true) ||
          !validConflictIds(stored.conflict_ids)) throw Error('Invalid Motet receipt data.');
      requests = copy(stored.requests); acknowledged = copy(stored.acknowledged); conflict_ids = copy(stored.conflict_ids || []);
    } else if (stored !== undefined && stored !== null) {
      // A new grant or enrollment cannot replace evidence from the old one.
      key = shardPrefix(id) + crypto.randomUUID();
    }
    state.shardKeys.set(scope,key);
    state.key = key;
    state.data = {version:3,mode:'derived',native_id:id,project,port:config.port,grant_id:config.grant_id,
      enrollment_id:response.enrollment_id,token:response.token,requests,acknowledged,conflict_ids,active:true};
    // Persisted receipts are recovery evidence, never a restored native flight.
    state.flight = null; state.envelope = null; state.bound.clear();
  }
  state.config = config;
  await activate($,state,response,eligibilityEpoch);
}

// A transition command (clear, resume, exit, ...) may still be cancelled, as a /resume picker can be, so it only
// discards a resume that is pending: the conversation has not changed yet. session.end is the host's word that it
// did. Then a pending resume installs nothing, and a module that never saw a real start (a reload) starts no
// resume until session.start. A module that did see one, and whose process goes on under a new conversation
// (/clear, a resume), may still be repaired by its next event if its own start failed to register.
function discardPendingResume(state) {
  state.lifecycle += 1;
  state.transitionLifecycle = state.lifecycle;
}

// A reloaded module that never saw a real start, and resumed only after the transition command this end completes,
// armed itself inside that transition: its timer is cancelled, so the chat stays off as if it had not resumed. A
// timer it armed before that transition (after an earlier, cancelled one, say) survives, as a started one does.
function endLifecycle(state, reason) {
  const armedInTransition = !state.startSeen && state.resumed && state.timer &&
    state.resumedLifecycle === state.transitionLifecycle;
  discardPendingResume(state);
  state.ended = !state.startSeen || !['clear','resume'].includes(reason);
  if (armedInTransition) {state.timer.cancel(); state.timer = null; state.resumed = false;}
}

// Desktop evidence belongs only to this loaded runtime's exact native identity.
function invalidateEligibility(state) {
  state.desktopBinding = null;
  state.observedCompletions.clear();
  state.recoverableTurns = [];
  state.eligibilityEpoch += 1;
  return state.eligibilityEpoch;
}

function checkEligibility(state, expected) {
  if (expected !== undefined && state.eligibilityEpoch !== expected) throw Error('Motet eligibility changed.');
}

async function readSurfaces($, state, eligibilityEpoch) {
  let surfaces = null;
  try {surfaces = surfaceRoster(await $.session.surfaces());} catch { /* Unknown stays fail-closed. */ }
  checkEligibility(state,eligibilityEpoch);
  state.currentSurfaces = surfaces;
  return surfaces;
}

async function supervisor($, state) {
  if (state.supervising || state.connecting || state.networkBusy || state.flushing || state.disconnecting) return;
  state.supervising = true;
  let now = 0, eligibilityEpoch = state.eligibilityEpoch, attempt = null, decidedFor = null;
  try {
    now = await $.clock.now();
    const id = await $.session.id(), project = await sessionProject($);
    decidedFor = id;
    checkEligibility(state,eligibilityEpoch);
    if (state.desktopBinding && (state.desktopBinding.native_id !== id || state.desktopBinding.project !== project)) {
      // Refresh only at our synchronous invalidation, never after awaited cleanup.
      eligibilityEpoch = invalidateEligibility(state);
    }
    const raw = await $.store.get(GLOBAL_KEY);
    checkEligibility(state,eligibilityEpoch);
    const config = await currentGrant($,state,raw);
    checkEligibility(state,eligibilityEpoch);
    const preference = UUID.test(id) ? await currentPreference($,state,id) : null;
    checkEligibility(state,eligibilityEpoch);
    const changed = state.data && (id !== state.data.native_id || project !== state.data.project && !state.receiptOnly ||
      (state.data.mode === 'derived' && configurationId(config) !== configurationId(state.config)));
    if (changed || preference?.disconnected === true || !UUID.test(id)) {
      if (preference?.disconnected === true || !UUID.test(id)) eligibilityEpoch = invalidateEligibility(state);
      suspend($,state);
      if (state.submitting) return;
      if (state.flight) await failRequest($,state,state.flight);
      checkEligibility(state,eligibilityEpoch);
      state.flight = null; state.envelope = null;
      state.data = null; state.key = null; state.bound.clear();
      state.retryAt = 0; state.failures = 0;
      if (preference?.disconnected === true && UUID.test(id)) await deliverPendingOptOut($,state,id).catch(() => {});
      // Off because of a disconnect this copy did not make: say so, so /watch-status explains it.
      if (preference?.disconnected === true && UUID.test(id) && state.ownDecisions.get(id) !== true && !state.error) state.error = STORED_OFF;
      if (preference?.disconnected === true || !UUID.test(id)) return;
    }
    const surfaces = await readSurfaces($,state,eligibilityEpoch);
    const currentId = await $.session.id(), currentProject = await sessionProject($);
    checkEligibility(state,eligibilityEpoch);
    if (currentId !== id || currentProject !== project) {
      invalidateEligibility(state);
      suspend($,state);
      return;
    }
    if (project === null) return;                     // outside its own project: nothing connects until it returns
    if (surfaces === null) return;
    if (surfaces.includes('desktop') && UUID.test(id)) state.desktopBinding = {native_id:id,project};
    if (!state.interactive && !(state.desktopBinding?.native_id === id && state.desktopBinding.project === project)) return;
    if (state.connected && state.receiptOnly && !state.flushing && !state.recovering &&
        (state.data.mode === 'derived' ? await earlierReceipts($,state,id,project,state.config) !== state.data.project
                                       : !await pendingReceipts([state.data]))) {
      // Every outcome from the earlier folder is reported: leave that context and connect in the project folder.
      const data = state.data;
      suspend($,state);
      state.receiptOnly = false; state.receiptKey = null; state.retryAt = 0; state.failures = 0;
      if (data.mode === 'derived') {state.data = null; state.key = null;}
      else state.data = {...data, project};
      return;
    }
    if (now < state.retryAt) return;
    // Motet refused this chat because its owner opted it out in another window or copy: wait for /watch-connect, or
    // until Motet shows a newer reconnect from another window.
    if (!state.connected && state.serverOptedOut.has(id)) {
      const latest = await motetDecision($,state,id);
      checkEligibility(state,eligibilityEpoch);
      if (!latest || latest.optedOut) return;
      state.serverOptedOut.delete(id);
    }
    if (!state.connected) {
      if (state.submitting) return;
      if (!state.data) {
        const manual = await $.store.get(legacyKey(id));
        checkEligibility(state,eligibilityEpoch);
        if (manual?.native_id === id && manual.project === project && manual.active) {state.data = manual; state.key = legacyKey(id);}
        // A manual pairing made from a subfolder before 0.6.3 names that subfolder. Offer the project root on the
        // same manual connection; Motet moves it only after proving the root is this chat's launch folder, and it
        // stays a manual (owner-only) connection. Nothing is saved until Motet accepts.
        else if (manual?.native_id === id && manual.active && typeof project === 'string' && insideFolder(manual.project, project, await pathRules($))) {
          state.receiptKey = receiptContext('manual', manual.project);
          state.receiptOnly = !state.abandonedReceipts.has(state.receiptKey) && await pendingReceipts([manual]);
          state.data = state.receiptOnly ? manual : {...manual, project}; state.key = legacyKey(id);
        }
      }
      if (state.data?.mode === 'derived' || (!state.data && config)) {
        if (!config || state.rejectedGrants.has(configurationId(config)) || state.rejectedIdentity === await refusalKey($,state,id,project,config)) return;
        // Report an earlier folder's retained outcomes first (receipts only, no claims); then move.
        const earlier = await earlierReceipts($,state,id,project,config);
        checkEligibility(state,eligibilityEpoch);
        state.receiptOnly = earlier !== null;
        state.receiptKey = earlier === null ? null : receiptContext('derived', earlier, config.grant_id);
        if (state.data && state.data.project !== (earlier ?? project)) {state.data = null; state.key = null;}
        // The place this attempt is made from, taken before any network call: a refusal is remembered for it only.
        attempt = await refusalKey($,state,id,project,config);
        checkEligibility(state,eligibilityEpoch);
        await connectDerived($,state,id,earlier ?? project,config,eligibilityEpoch);
        // Another copy's grant worked where the shared one was refused: share it, so the other chats use it too.
        if (configurationId(config) !== configurationId(raw)) await $.store.set(GLOBAL_KEY,config).catch(() => {});
      } else {
        const stored = state.data ?? await $.store.get(legacyKey(id));
        checkEligibility(state,eligibilityEpoch);
        if (!stored || stored.native_id !== id || stored.project !== project && !state.receiptOnly || !stored.active) return;
        state.key = legacyKey(id); state.data = stored;
        const response = await post($,state,'connect',{native_id:id,project:stored.project,...state.receiptOnly?{receipt_only:true}:{}});
        checkEligibility(state,eligibilityEpoch);
        await activate($,state,response,eligibilityEpoch);
      }
      return;
    }
    if (state.data.mode === 'derived' && now >= state.recoverAt) {
      await recover($,state);if(state.noticeProtocol===1)await recoverNotices($,state); state.recoverAt = now + 10000;
      checkEligibility(state,eligibilityEpoch);
    }
    await flush($,state);
    checkEligibility(state,eligibilityEpoch);
    await flushNotices($,state);
    await poll($,state);
  } catch (error) {
    if (state.eligibilityEpoch !== eligibilityEpoch) return;
    stop(state);
    if (error.status === 403 && state.config) {
      state.rejectedGrants.add(configurationId(state.config));
      forgetGrant($,state.config).catch(() => {});
    }
    // Never re-read the folder here: the chat may have moved while the request was in flight, and the refusal
    // belongs to the folder the attempt was made from.
    if (error.code === 'identity_rejected' && attempt !== null) state.rejectedIdentity = attempt;
    if (error.code === 'decision_superseded' && UUID.test(decidedFor ?? '')) {
      state.serverOptedOut.add(decidedFor);
      state.decisionReads.delete(decidedFor);
      recordSuperseded(state,decidedFor);
      state.error = OTHER_WINDOW;
      return;
    }
    if (error.code === 'receipt_context_missing' && state.receiptOnly) {
      // Motet holds no connection for that earlier folder any more. Stop asking for it; its records stay stored
      // for recovery, and the chat moves to its project folder on the next attempt.
      if (state.receiptKey) state.abandonedReceipts.add(state.receiptKey);
      state.receiptOnly = false; state.receiptKey = null;
      if (state.data?.mode === 'derived') {state.data = null; state.key = null;}
      else if (state.data) {
        const project = await sessionProject($).catch(()=>null);
        if (state.eligibilityEpoch !== eligibilityEpoch) return;
        state.data = typeof project === 'string' ? {...state.data, project} : null;
        if (!state.data) state.key = null;
      }
    }
    state.error = error.message === 'Motet recovery data limit.' ? 'Motet recovery data limit reached; automatic claims stopped.' :
      'Motet connection interrupted; existing attempts are never resubmitted.';
    backoff(state,now);
  } finally {state.supervising = false;}
}

// The owner's connection decisions for this chat (/watch-connect, /watch-setup, /watch-disconnect) take effect in
// the order they were made, by a counter, never by the clock (which can be set back). Each command takes its number
// when it starts, before anything is awaited. A decision takes effect when its preference is written; once a newer
// one has, an older command changes nothing more, so an older connect or setup that finishes late cannot clear a
// later opt-out, nor a late disconnect undo a later reconnect. A newer command that gives up without deciding (a
// bad code, a failed setup) overtakes nothing, so an opt-out always takes effect unless a newer decision did.
function reserveDecision(state) {
  state.decision += 1;
  return state.decision;
}

const overtaken = (state, decision, id) => (state.appliedDecisions.get(id) ?? 0) > decision;

// Motet orders the owner's decisions for every plugin copy and Claude window (decision protocol 1). Each owner
// command reserves a ticket the moment it knows its chat, before anything else it waits on, and later commits its
// decision under that ticket; Motet applies a commit only if no decision with a newer ticket was committed first,
// so a command that finishes late (an earlier copy's after a reload, another window's) cannot undo a later one.
// Motet refuses every new connection while the chat is opted out. The credential is this chat's manual pairing,
// or the shared grant; one Motet refuses is set aside and the next is tried. Without one, or with an older Motet
// that has no tickets, the plugin's own ordering below is all there is.
async function decisionCredential($, state, id) {
  const manual = await $.store.get(legacyKey(id));
  if (manual?.native_id === id && typeof manual.token === 'string' && Number.isInteger(manual.port) &&
      !state.rejectedDecisionTokens.has(manual.token)) return {port:manual.port,token:manual.token};
  const config = await currentGrant($,state,await $.store.get(GLOBAL_KEY));
  return config && !state.rejectedGrants.has(configurationId(config)) ? {port:config.port,token:config.grant,grant:config} : null;
}

// Motet's answer to one decision request, or null where Motet cannot order decisions (no credential it accepts,
// or an older Motet). Throws when Motet cannot be reached, and with decision_superseded when a newer decision won.
async function decisionRequest($, state, id, body, port = null) {
  if (state.decisionProtocol === 0) return null;
  for (let tries = 0; tries < 4; tries++) {
    const credential = await decisionCredential($,state,id);
    if (!credential) return null;
    // A ticket is valid only on the Motet that issued it.
    if (port !== null && credential.port !== port) throw Error('The ticket was issued by another Motet.');
    let response;
    try {
      response = body.reserve ? await reservationPost($,{native_id:id,...body},credential) :
        await post($,state,'decision',{native_id:id,...body},{port:credential.port,token:credential.token});
    } catch (error) {
      // An older Motet refuses the route itself; once this Motet has shown tickets, a refusal is only an error.
      if (error.status === 403 && error.code === 'invalid_request' && state.decisionProtocol !== 1) {state.decisionProtocol = 0; return null;}
      if (error.status === 403 && error.code === 'access_denied') {
        if (credential.grant) {
          state.rejectedGrants.add(configurationId(credential.grant)); forgetGrant($,credential.grant).catch(() => {});
        } else state.rejectedDecisionTokens.add(credential.token);
        continue;
      }
      throw error;
    }
    if (response?.decision_protocol !== 1 || !Number.isSafeInteger(response.decision_seq) || response.decision_seq < 1 ||
        (body.reserve ? response.reserved !== true : response.opted_out !== body.opted_out)) throw Error('Invalid Motet decision.');
    state.decisionProtocol = 1;
    return response;
  }
  return null;
}

// Each owner command has a fixed identity: this copy and the command's number here. Motet gives one identity the
// same ticket however often it is asked, so a lost answer and a retry return the original place, never a newer one.
const commandId = (state, decision) => state.clientId + ':' + decision;

function queueReservation(state, task) {
  const operation = state.reservationChain.then(task);
  state.reservationChain = operation.catch(() => {});
  return operation;
}

function refuseCredential($, state, credential) {
  if (credential.grant) {state.rejectedGrants.add(configurationId(credential.grant)); forgetGrant($,credential.grant).catch(() => {});}
  else state.rejectedDecisionTokens.add(credential.token);
}

function reservedTicket(response) {
  return response?.decision_protocol === 1 && response.reserved === true && Number.isSafeInteger(response.decision_seq) &&
    response.decision_seq >= 1 ? response.decision_seq : null;
}

// A command's place in the owner's order, reserved as its very first step, before it reads which chat it is for
// (that read can be slow), on this copy's reservation chain. Motet issues tickets from one counter for every chat, so
// a reservation names no chat; the credential is any this computer holds: this copy's manual pairing or grant, the
// shared grant, or a stored manual pairing. The ticket records the Motet (its port) that issued it. Across windows
// and copies the order is the order in which Motet receives the reservations, so commands from two windows given
// within the same moment are ordered by when Motet receives them. Once one request has had no clear answer (Motet
// may or may not hold a ticket for it), every later request for the command on that Motet only asks for the ticket
// Motet already holds (recover): an absent one (the first request never arrived, or its record expired or was
// dropped to keep the table bounded) never becomes a fresh, later place. Answers {seq, port}; {failed, port} when no
// answer came at all; {unplaced, port} when Motet holds no ticket for the command after an unclear request; null
// with no sign-in stored at all (then no opt-out can exist at Motet to misorder) or an older Motet. Each answer
// lists, as uncertain, the Motets an unclear request went to.
async function reserveEarly($, state, decision) {
  if (state.decisionProtocol === 0) return null;
  const tried = new Set(), uncertain = new Set();
  const answer = value => value && {...value,uncertain:[...uncertain]};
  async function* candidates() {
    const manual = state.data;
    if (manual && manual.mode !== 'derived') yield {port:manual.port,token:manual.token};
    if (globalConfiguration(state.config)) yield {port:state.config.port,token:state.config.grant,grant:state.config};
    const shared = await currentGrant($,state,await $.store.get(GLOBAL_KEY));
    if (shared) yield {port:shared.port,token:shared.grant,grant:shared};
    const keys = await $.store.keys();
    if (!Array.isArray(keys) || keys.length > 4096) return;
    for (const key of keys.filter(key => typeof key === 'string' && key.startsWith(legacyKey(''))).slice(0, 32)) {
      const record = await $.store.get(key);
      if (record && typeof record.token === 'string') yield {port:record.port,token:record.token};
    }
  }
  try {
    for await (const credential of candidates()) {
      if (typeof credential.token !== 'string' || !Number.isInteger(credential.port) || tried.has(credential.token)) continue;
      tried.add(credential.token);
      if (credential.grant ? state.rejectedGrants.has(configurationId(credential.grant)) : state.rejectedDecisionTokens.has(credential.token)) continue;
      for (let tries = 0; ; tries++) {
        try {
          const seq = reservedTicket(await reservationPost($,reservation(state,decision,credential.port,uncertain),credential));
          if (!seq) return null;
          state.decisionProtocol = 1;
          return answer({seq,port:credential.port});
        } catch (error) {
          if (error.code === 'not_reserved') return answer({unplaced:true,port:credential.port});
          if (error.status === 403 && error.code === 'access_denied') {refuseCredential($,state,credential); break;}
          if (error.status === 403 && error.code === 'invalid_request' && state.decisionProtocol !== 1) {state.decisionProtocol = 0; return null;}
          if (unclear(error)) uncertain.add(credential.port);
          if (tries >= 1) return answer({failed:true,port:credential.port});
        }
      }
    }
  } catch { /* No stored sign-in could be read. */ }
  return uncertain.size ? answer({failed:true,port:[...uncertain].at(-1)}) : null;
}

// A refusal Motet answered (4xx) reserved nothing; any other failure (no answer, a server error, an unreadable
// answer) may have reached Motet and reserved a ticket.
const unclear = error => !(error?.status >= 400 && error.status < 500);

// The reservation request for a command on one Motet: an ordinary one, or recover-only once an earlier request for
// the command on that Motet had no clear answer.
function reservation(state, decision, port, uncertain, id = null) {
  return {reserve:true,...id ? {native_id:id} : {},command_id:commandId(state,decision),...uncertain.has(port) ? {recover:true} : {}};
}

// The command's place on the Motet its decision is committed to, once it knows its chat. An early ticket from that
// Motet stands, and so does an early finding that Motet holds none ({unplaced}). On a Motet where any request for
// this command had no clear answer, only the ticket Motet already holds is asked for (recover); Motet holding none
// means the command is not given a later place: {unplaced}. Otherwise (the early ticket came from another Motet, or
// none was reserved before) the command reserves here, as the same command. {unreachable} when Motet cannot be
// reached; {ticket:null} where Motet orders nothing (no credential for this chat, or an older Motet).
async function placeCommand($, state, id, decision, early) {
  const uncertain = new Set(early?.uncertain ?? []);
  for (let tries = 0; tries < 4; tries++) {
    if (state.decisionProtocol === 0) return {ticket:null};
    const credential = await decisionCredential($,state,id);
    if (!credential) return {ticket:null};
    if (early?.seq && early.port === credential.port) return {ticket:{seq:early.seq,port:early.port}};
    if (early?.unplaced && early.port === credential.port) return {ticket:null,unplaced:true};
    let lost = false;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const body = reservation(state,decision,credential.port,uncertain,id);
        const seq = reservedTicket(await queueReservation(state,() => reservationPost($,body,credential)));
        if (!seq) return {ticket:null};
        state.decisionProtocol = 1;
        return {ticket:{seq,port:credential.port}};
      } catch (error) {
        if (error.code === 'not_reserved') return {ticket:null,unplaced:true};
        if (error.status === 403 && error.code === 'access_denied') {refuseCredential($,state,credential); lost = false; break;}
        if (error.status === 403 && error.code === 'invalid_request' && state.decisionProtocol !== 1) {state.decisionProtocol = 0; return {ticket:null};}
        if (unclear(error)) uncertain.add(credential.port);
        lost = true;
      }
    }
    if (lost) return {ticket:null,unreachable:true};
  }
  return {ticket:null};
}

// A ticket stays with the command that reserved it. One copy's reservations go out in the order the owner gave the
// commands, so their tickets follow that order. Should an older command still end up with a later ticket than a
// newer one (its first request never reached Motet, say), it gives way to every newer command of this copy that has
// reserved and is still running for the same chat (or for a chat not yet read), and the newer command stands as the
// owner's later one. An older reconnect then changes nothing. An older opt-out is kept with its own ticket, and
// applies if the newer command ends without deciding (a failed connect, say): it is committed then, under its own
// ticket, so it can never move above a newer decision from another window. No command waits for another, and no
// ticket ever moves to another command. A command running for a chat that /clear has since replaced does not hold
// back the new chat's commands once its chat is known.
function reservedInOrder(state, decision, ticket, id) {
  for (const [running, chat] of state.runningReservations) if (running > decision && (chat === null || chat === id)) return false;
  if (ticket) state.runningReservations.set(decision, id);
  return true;
}

// Called as each owner command ends: an opt-out that gave way to it (or to a newer command that has since ended)
// applies now unless a newer decision did.
function settleYieldedOptOut($, state, id, decision) {
  state.runningReservations.delete(decision);
  for (const [chat, yielded] of [...state.yieldedOptOuts]) {
    let blocked = false;
    for (const [running, of] of state.runningReservations) if (running > yielded.decision && (of === null || of === chat)) blocked = true;
    if (blocked) continue;
    state.yieldedOptOuts.delete(chat);
    if (overtaken(state,yielded.decision,chat)) continue;
    savePreference($,state,chat,true,yielded.decision,yielded.ticket,{offline:yielded.offline === true}).then(saved => {
      if (saved !== true || overtaken(state,yielded.decision,chat)) return;
      const data = state.data;
      suspend($,state);
      if (data && data.mode !== 'derived') save($,state,()=>{data.active = false;}).catch(() => {});
    }).catch(() => {});
  }
}

// What Motet did with this copy's own decision for a chat, kept the same way on every path (a command's commit, a
// pending opt-out told later, a recovered ticket, a pairing). Committed: the copy holds the ticket and the Motet
// that issued it, and nothing stays pending. Refused because a newer decision exists: the copy drops its own
// decision for the chat, so from then on it follows Motet and the shared record.
function recordCommitted(state, id, seq, port) {
  state.tickets.set(id, seq);
  state.ticketPorts.set(id, port);
  state.ownCommands.delete(id);
}

function recordSuperseded(state, id) {
  state.ownDecisions.delete(id);
  state.tickets.delete(id);
  state.ticketPorts.delete(id);
  state.ownCommands.delete(id);
}

// Commits this decision at Motet under the ticket its command reserved, on the Motet that issued it. A decision with
// no ticket is not committed: Motet then decides by the chat's current decision. Answers the ticket number, null
// where Motet does not order this decision, or 'superseded' when a decision with a newer ticket was committed.
async function commitDecision($, state, id, optedOut, ticket) {
  if (!ticket) return null;
  try {
    const response = await decisionRequest($,state,id,{opted_out:optedOut,decision_seq:ticket.seq},ticket.port);
    return response ? response.decision_seq : null;
  } catch (error) {
    if (error.code === 'decision_superseded') return 'superseded';
    throw error;
  }
}

// Preference writes run one at a time, and a write is skipped when a newer decision was already written, so a slow
// write cannot land after a newer one; each commits its decision at Motet first (unless Motet committed it already,
// as a pairing does). Each record names the copy of the plugin that wrote it and carries its ticket and the Motet
// that issued it. An opt-out Motet could not be told is kept as pending, with its ticket or its command's identity,
// and told on a later contact. The answer is true while this decision stands once the writes queued behind it have
// run, false when a newer one in this copy replaced it, and 'elsewhere' when Motet holds a newer decision from
// another window or copy.
async function savePreference($, state, id, disconnected, decision, ticket = null, {offline = false, committed = false, outcome = {}} = {}) {
  const operation = state.preferenceWrites.then(async () => {
    if (overtaken(state,decision,id)) return false;
    let seq = null, pending = false;
    if (committed) seq = ticket?.seq ?? null;
    else if (offline) pending = disconnected;
    else {
      try {
        const result = await commitDecision($,state,id,disconnected,ticket);
        if (result === 'superseded') {recordSuperseded(state,id); return 'elsewhere';}
        seq = result;
      } catch {pending = disconnected; outcome.unconfirmed = !disconnected;}
      // An opt-out Motet has not committed, for any reason (no sign-in on this computer, every sign-in refused, no
      // ticket), is kept pending like an offline one, so it is delivered, handed off and superseded the same way and
      // never held only in this copy's memory. Only an older Motet, which orders nothing, keeps it unticketed.
      if (disconnected && !seq && state.decisionProtocol !== 0) pending = true;
    }
    const own = state.pendingOptOuts.get(id) ?? new Map();
    if (pending) {
      const command = commandId(state,decision);
      await $.store.set(pendingPrefix(id) + command,{version:1,native_id:id,command_id:command,writer:state.clientId,
        ...ticket ? {decision_seq:ticket.seq,decision_port:ticket.port} : {}});
      own.set(pendingPrefix(id) + command, decision); state.pendingOptOuts.set(id, own);
    }
    await $.store.set(preferenceKey(id),{version:1,disconnected,writer:state.clientId,
      ...seq ? {decision_seq:seq,decision_port:ticket.port} : {},
      ...pending ? {pending:true,command_id:commandId(state,decision),...ticket ? {decision_seq:ticket.seq,decision_port:ticket.port} : {}} : {}});
    // This copy's own older pending opt-outs for the chat give way to this newer decision of the owner's.
    for (const [key, older] of own) if (older < decision) {try {await $.store.delete(key); own.delete(key);} catch { /* Removed on its next decision. */ }}
    state.appliedDecisions.set(id, Math.max(state.appliedDecisions.get(id) ?? 0, decision));
    state.ownDecisions.set(id, disconnected);
    if (seq) recordCommitted(state,id,seq,ticket.port);
    else {
      state.ticketPorts.delete(id);
      if (pending && ticket) state.tickets.set(id, ticket.seq); else state.tickets.delete(id);
      if (pending) state.ownCommands.set(id, commandId(state,decision)); else state.ownCommands.delete(id);
    }
    if (!disconnected) state.serverOptedOut.delete(id);
    return true;
  });
  state.preferenceWrites = operation.catch(() => {});
  const result = await operation;
  if (result !== true) return result;
  await state.preferenceWrites;
  return !overtaken(state,decision,id);
}

// The grant to use: the shared one, unless Motet refused it in this runtime; then another copy's grant that Motet
// has not refused. Motet decides which grant is current, so no ordering is needed between the copies' writes.
async function currentGrant($, state, raw) {
  const shared = globalConfiguration(raw) ? raw : null;
  if (!shared || !state.rejectedGrants.has(configurationId(shared))) return shared;
  const keys = await $.store.keys();
  if (!Array.isArray(keys) || keys.length > 4096) return shared;
  for (const key of keys.filter(key => typeof key === 'string' && key.startsWith(grantPrefix))) {
    const candidate = await $.store.get(key);
    if (globalConfiguration(candidate) && !state.rejectedGrants.has(configurationId(candidate))) return candidate;
  }
  return shared;
}

// A grant Motet refused stays revoked: its own key is dropped, so it is not tried again. That key holds only this
// grant, so the delete can never remove a newer one written meanwhile.
async function forgetGrant($, config) {
  if (!globalConfiguration(config)) return;
  if (configurationId(await $.store.get(grantKey(config))) === configurationId(config)) await $.store.delete(grantKey(config));
}

// Another copy can deliver this copy's pending disconnect. Once its exact key is gone and the shared record
// contains an ordered result, stop asserting the old unticketed decision from memory. A legacy delivery removes
// the key without an ordered result, so its own opt-out still stands. Failed reads never establish delivery.
async function reconcileOwnPending($, state, id, stored) {
  const command = state.ownCommands.get(id);
  if (!command || state.ownDecisions.get(id) !== true) return stored;
  const key = pendingPrefix(id) + command;
  if (await $.store.get(key) !== undefined) return stored;
  stored = await $.store.get(preferenceKey(id));
  if (stored?.version !== 1 || typeof stored.disconnected !== 'boolean' || stored.pending === true ||
      !Number.isSafeInteger(stored.decision_seq) || stored.decision_seq < 1 ||
      !Number.isInteger(stored?.decision_port) || stored.decision_port < 1 || stored.decision_port > 65535) return stored;
  const operation = state.preferenceWrites.then(() => {
    // A newer local command may have completed during either read; only retire the captured command.
    if (state.ownCommands.get(id) !== command || state.ownDecisions.get(id) !== true) return false;
    recordSuperseded(state,id);
    const pending = state.pendingOptOuts.get(id);
    pending?.delete(key);
    if (pending?.size === 0) state.pendingOptOuts.delete(id);
    return true;
  });
  state.preferenceWrites = operation.catch(() => {});
  // Reconciliation can queue a preference write of its own, so it must run outside the operation above.
  return await operation ? $.store.get(preferenceKey(id)) : stored;
}

// The preference this copy follows. Another copy of the plugin (an earlier one still finishing a command after a
// reload) or another Claude window of this chat may have written the shared record since this copy's own decision.
// Where both carry Motet tickets, the newer ticket wins, as at Motet, and this copy's decision is written back only
// when its ticket is newer. Otherwise (an older Motet, or a decision Motet could not order) only an opt-out is
// asserted: this copy's own opt-out is written back and followed before anything connects, while a foreign opt-out
// over this copy's reconnect stands, since it may be a newer decision made in another window. Both fail closed.
async function currentPreference($, state, id) {
  const stored = await reconcileOwnPending($,state,id,await $.store.get(preferenceKey(id)));
  // An owner disconnect not yet told to Motet holds, whatever the shared record says, until it is delivered.
  const pending = await pendingOptOuts($,id);
  const held = value => !pending.length || value?.disconnected === true ? value :
    {version:1,disconnected:true,pending:true,writer:pending[0].entry.writer};
  if (!state.ownDecisions.has(id)) return pending.length ? held(stored) : reconcilePreference($,state,id,stored);
  const own = state.ownDecisions.get(id), ticket = state.tickets.get(id);
  let wins;
  if (ticket && Number.isSafeInteger(stored?.decision_seq)) wins = ticket > stored.decision_seq;
  else wins = own === true && stored?.disconnected !== true;
  if (!wins) return held(stored);
  const record = {version:1,disconnected:own,writer:state.clientId,
    ...ticket ? {decision_seq:ticket,...state.ticketPorts.has(id) ? {decision_port:state.ticketPorts.get(id)} : {}} : {},
    ...own && (!ticket || state.ownCommands.has(id)) ? {pending:true,...state.ownCommands.has(id) ? {command_id:state.ownCommands.get(id)} : {}} : {}};
  const operation = state.preferenceWrites.then(() =>
    state.ownDecisions.get(id) === own && state.tickets.get(id) === ticket ? $.store.set(preferenceKey(id),record) : undefined);
  state.preferenceWrites = operation.catch(() => {});
  try {await operation;} catch { /* This copy's decision still holds here; the next tick writes it again. */ }
  return held(record);
}

// A copy with no decision of its own for this chat does not trust a stored opt-out blindly: an older copy's late write
// may have put it over a newer reconnect. It checks the opt-out's ticket against the latest decision Motet holds
// for the chat, and follows Motet's when that is newer. A pending opt-out (not yet told to Motet) and one without
// a ticket (an older Motet's) stand, failing closed; a stored reconnect is never trusted over a newer opt-out
// either, since Motet refuses the connection.
async function reconcilePreference($, state, id, stored) {
  if (stored?.disconnected !== true || stored.pending === true || !Number.isSafeInteger(stored.decision_seq)) return stored;
  const latest = await motetDecision($,state,id);
  if (!latest || latest.seq <= stored.decision_seq || latest.optedOut) return stored;
  const record = {version:1,disconnected:false,writer:'motet',decision_seq:latest.seq,decision_port:latest.port};
  const operation = state.preferenceWrites.then(async () => {
    const current = await $.store.get(preferenceKey(id));
    if (current?.decision_seq === stored.decision_seq && current?.disconnected === true) await $.store.set(preferenceKey(id),record);
  });
  state.preferenceWrites = operation.catch(() => {});
  try {await operation;} catch { /* Followed here; written again later. */ }
  return record;
}

// The chat's owner disconnects not yet told to Motet, each under its own key.
async function pendingOptOuts($, id) {
  const keys = await $.store.keys(), prefix = pendingPrefix(id), found = [];
  if (!Array.isArray(keys)) return found;
  for (const key of keys.filter(key => typeof key === 'string' && key.startsWith(prefix))) {
    const entry = await $.store.get(key);
    if (entry?.version === 1 && entry.native_id === id && typeof entry.command_id === 'string' && key === prefix + entry.command_id) found.push({key,entry});
  }
  return found;
}

// An opt-out Motet has not been told yet (it was made while Motet could not be reached) is told on the next
// contact, at most every 30 seconds, by whichever copy reaches Motet first: under its own ticket if Motet issued one
// on that same Motet, else under its command's identity (Motet returns the original ticket if the first request
// did arrive, and otherwise places it now). Once Motet has it (committed, or refused for a newer decision), the
// shared record says what Motet holds and its pending key, and only that key, is removed. An older Motet, which
// orders nothing, has nothing to be told: the key is removed and the record (an opt-out) stands as it is.
// Answers the latest ticket it placed (0 for none). An owner reconnect calls it with skipOwn, leaving this copy's own
// older entries, which that newer decision replaces.
async function deliverPendingOptOut($, state, id, {skipOwn = false} = {}) {
  const now = await $.clock.now();
  if (now < state.pendingRetryAt) return 0;
  // Throttled only once a delivery can be tried: one made the moment a sign-in exists (a setup, say) is not delayed.
  if (!(await pendingOptOuts($,id)).length || !await decisionCredential($,state,id)) return 0;
  state.pendingRetryAt = now + 30000;
  let placed = 0;
  const operation = state.preferenceWrites.then(async () => {
    for (const {key,entry} of await pendingOptOuts($,id)) {
      if (skipOwn && state.pendingOptOuts.get(id)?.has(key)) continue;
      placed = Math.max(placed, await deliverOptOut($,state,id,key,entry) ?? 0);
    }
  });
  state.preferenceWrites = operation.catch(() => {});
  try {await operation; state.pendingRetryAt = 0;} catch { /* Told on a later contact. */ }
  return placed;
}

// An owner reconnect (/watch-connect, /watch-setup, an accepted pairing) about to be committed while another copy's
// disconnect for the chat is still pending: that disconnect was made earlier, when no sign-in or no Motet could place
// it. It is told to Motet first, and the reconnect then takes a place after it, so the owner's later command comes
// out last. Answers {ticket} to commit under (the command's own, or one taken after the delivery, under the
// command's second identity), {undelivered} when a pending disconnect could not be told (it stays, and so does
// the chat's disconnect), or the new placement's {unplaced} or {unreachable}.
const AFTER_PENDING = 1e12;
async function placeAfterPending($, state, id, decision, ticket) {
  const foreign = key => !state.pendingOptOuts.get(id)?.has(key);
  const before = (await pendingOptOuts($,id)).filter(({key}) => foreign(key)).map(({key}) => key);
  if (!before.length) return {ticket};
  state.pendingRetryAt = 0;
  const placed = await deliverPendingOptOut($,state,id,{skipOwn:true});
  const left = new Set((await pendingOptOuts($,id)).map(({key}) => key));
  if (before.some(key => left.has(key))) return {undelivered:true};
  if (ticket && ticket.seq > placed) return {ticket};
  return placeCommand($,state,id,AFTER_PENDING + decision,null);
}

async function deliverOptOut($, state, id, key, entry) {
  const own = state.pendingOptOuts.get(id)?.has(key) === true && state.ownDecisions.get(id) === true;
  const delivered = async record => {
    if (record) await $.store.set(preferenceKey(id),record);
    await $.store.delete(key);
    state.pendingOptOuts.get(id)?.delete(key);
  };
  if (state.decisionProtocol === 0) return delivered(null);
  let ticket = null;
  for (let tries = 0; tries < 4 && !ticket; tries++) {
    const credential = await decisionCredential($,state,id);
    if (!credential) return;
    if (Number.isSafeInteger(entry.decision_seq) && entry.decision_port === credential.port) {ticket = {seq:entry.decision_seq,port:credential.port}; break;}
    try {
      const seq = reservedTicket(await queueReservation(state,() => reservationPost($,{reserve:true,native_id:id,command_id:entry.command_id},credential)));
      if (!seq) return;
      state.decisionProtocol = 1;
      ticket = {seq,port:credential.port};
    } catch (error) {
      if (error.status === 403 && error.code === 'access_denied') {refuseCredential($,state,credential); continue;}
      if (error.status === 403 && error.code === 'invalid_request' && state.decisionProtocol !== 1) {state.decisionProtocol = 0; return delivered(null);}
      throw error;
    }
  }
  if (!ticket) return;
  const committed = await commitDecision($,state,id,true,ticket);
  if (committed === 'superseded') {
    // A decision with a newer ticket won at Motet: this opt-out is no longer the owner's latest. Its copy follows
    // Motet from now on, and the record says what Motet holds, or at least carries this opt-out's own ticket, so
    // every copy can see a newer one is in force. It is never left as an opt-out with no ticket.
    if (own) recordSuperseded(state,id);
    state.decisionReads.delete(id);
    const latest = await motetDecision($,state,id).catch(() => null);
    await delivered(latest && latest.seq > ticket.seq ?
      {version:1,disconnected:latest.optedOut,writer:'motet',decision_seq:latest.seq,decision_port:latest.port} :
      {version:1,disconnected:true,writer:entry.writer,decision_seq:ticket.seq,decision_port:ticket.port});
    return ticket.seq;
  } else if (committed) {
    if (own) recordCommitted(state,id,committed,ticket.port);
    await delivered({version:1,disconnected:true,writer:entry.writer,decision_seq:committed,decision_port:ticket.port});
    return committed;
  } else if (state.decisionProtocol === 0) await delivered(null);
  return 0;
}

// The latest decision Motet holds for this chat (decision protocol 1), read at most every 30 seconds; null when it
// cannot be read.
async function motetDecision($, state, id) {
  if (state.decisionProtocol === 0) return null;
  const now = await $.clock.now(), last = state.decisionReads.get(id);
  if (last && now - last.at < 30000) return last.value;
  let value = null;
  try {
    const credential = await decisionCredential($,state,id);
    if (credential) {
      const response = await reservationPost($,{native_id:id,query:true},credential);
      if (response?.decision_protocol === 1 && response.query === true && Number.isSafeInteger(response.decision_seq) &&
          typeof response.opted_out === 'boolean') value = {seq:response.decision_seq,optedOut:response.opted_out,port:credential.port};
    }
  } catch { /* Read again later. */ }
  state.decisionReads.set(id,{at:now,value});
  return value;
}

const YIELDED_OPT_OUT = 'A newer Motet command is still running; this disconnect applies if that one does not complete.';
const STORED_OFF = 'This chat is disconnected (by /watch-disconnect, here or in another Claude window). Run /watch-connect to reconnect it.';
const UNPLACED = 'Motet could not confirm when this command was given, so nothing was changed. Run it again.';
const UNREACHABLE = 'Motet could not be reached to place this command, so nothing was changed. Try again.';
const PENDING_UNDELIVERED = 'An earlier /watch-disconnect from another window could not be told to Motet yet, so this chat stays disconnected and this command changed nothing. Try again once Motet can be reached.';
const SETUP_KEPT_OFF = 'Motet automatic connections authorized. This chat stays disconnected: an earlier /watch-disconnect from another window could not be told to Motet yet, or this chat could not be placed after it. Run /watch-connect here once Motet can be reached. ';
const PAIRING_KEPT_OFF = 'Motet accepted the pairing, but this chat stays disconnected: an earlier /watch-disconnect from another window could not be told to Motet yet, or this chat could not be placed after it. Run /watch-connect here once Motet can be reached.';
const UNCONFIRMED = 'Motet stopped answering before it confirmed this reconnect. It takes effect only if Motet received it: check /watch-status, or run /watch-connect again.';
const PAIRING_REFUSED = 'Motet did not accept this pairing code, so nothing was changed. Get a fresh code from Motet.';
const NEWER_ELSEWHERE = 'A newer Motet command from another Claude window replaced this one; nothing was changed.';
const OTHER_WINDOW = 'This chat was disconnected from another Claude window. Run /watch-connect here to reconnect it, or close the other window.';

const SUPERSEDED = 'A newer Motet command replaced this one; nothing was changed.';
const ENDED_SUPERSEDED = 'A newer Motet command replaced this one; the connection Motet had just made for it was ended.';

const CONNECT_FAILED = 'Motet connection failed. If setup was interrupted, get a fresh setup command. No request was resubmitted.';

// Whether an owner connect's decision still stands just before it acts on Motet: no newer decision in this copy,
// the stored preference not set to disconnected since (by another copy, say), and no end, transition or opt-out.
// The answer is null while it stands, else the reply for the owner.
async function decisionStands($, state, id, decision, eligibilityEpoch) {
  const stored = overtaken(state,decision,id) ? null : await $.store.get(preferenceKey(id));
  if (!overtaken(state,decision,id) && stored?.disconnected === true && stored.writer !== state.clientId) return {text:OTHER_WINDOW};
  if (overtaken(state,decision,id) || stored?.disconnected === true) return {text:SUPERSEDED};
  return state.eligibilityEpoch === eligibilityEpoch ? null : {text:CONNECT_FAILED};
}

// Makes a connection Motet accepted for an owner connect current, only while that decision still stands. If a
// disconnect came while the request was on its way, the connection Motet just made is ended at once, so the chat
// is not left ready for work it will never receive. Returns whether the connection stands.
async function ownerActivate($, state, response, id, decision, eligibilityEpoch) {
  const data = state.data;
  let refusal = null;
  try {
    refusal = await decisionStands($,state,id,decision,eligibilityEpoch);
    if (refusal) throw Error('Owner decision no longer stands.');
    await activate($,state,response,eligibilityEpoch);
    refusal = await decisionStands($,state,id,decision,eligibilityEpoch);
    if (refusal) throw Error('Owner decision no longer stands.');
    return null;
  } catch (error) {
    refusal ??= await decisionStands($,state,id,decision,eligibilityEpoch).catch(() => ({text:CONNECT_FAILED}));
    if (!refusal) throw error;
    if (state.connected && state.data === data) suspend($,state);
    else if (data?.token && typeof response?.generation === 'string') {
      post($,state,'disconnect',identityBody(state,data,response.generation),{port:data.port,token:data.token}).catch(() => {});
    }
    stop(state);
    // Motet did connect for this command, and that connection was just ended: say so, rather than "nothing changed".
    return refusal.text === SUPERSEDED ? {text:ENDED_SUPERSEDED} : refusal;
  }
}

async function explicitDisconnect($, state) {
  const decision = reserveDecision(state);
  let id = null, result = false;
  // Kept, so a disconnect that ends up changing nothing does not cost a Desktop chat its evidence.
  const binding = state.desktopBinding;
  state.disconnecting += 1;
  invalidateEligibility(state);
  try {
    // Its place in the owner's order at Motet, the command's first step, on this copy's reservation chain.
    const early = await queueReservation(state,() => reserveEarly($,state,decision));
    if (early?.seq) state.runningReservations.set(decision, null);
    id = await $.session.id();
    if (overtaken(state,decision,id)) return result = false;
    const placement = UUID.test(id) ? await placeCommand($,state,id,decision,early) : {ticket:null};
    // A disconnect always takes effect here. One whose place Motet cannot confirm, or that cannot reach Motet, is
    // kept pending and told to Motet later, under the ticket Motet gives it then: placing an opt-out when Motet first
    // hears of it can only beat a reconnect made at about the same moment, the safe way round for an opt-out.
    const offline = placement.unreachable === true || placement.unplaced === true;
    const ticket = placement.ticket;
    if (overtaken(state,decision,id)) return result = false;
    if (!reservedInOrder(state,decision,ticket,id)) {
      const earlier = state.yieldedOptOuts.get(id);
      if (!earlier || earlier.decision < decision) state.yieldedOptOuts.set(id,{decision,ticket,offline});
      return result = 'yielded';
    }
    const saved = UUID.test(id) ? await savePreference($,state,id,true,decision,ticket,{offline}) : true;
    if (saved !== true) return result = saved;
    if (overtaken(state,decision,id)) return result = false;
    const data = state.data;
    suspend($,state);
    if (data && data.mode !== 'derived') await save($,state,()=>{data.active = false;});
    return result = true;
  } finally {
    state.disconnecting -= 1;
    if (result !== true && binding && !state.desktopBinding && (!id || binding.native_id === id)) state.desktopBinding = binding;
    settleYieldedOptOut($,state,id,decision);
  }
}

async function retryHeldReceipts($, state, id) {
  const data = state.data, epoch = state.epoch;
  if (!UUID.test(id)) return {text:'Use /watch-retry-receipts with the full request ID from /watch-status.'};
  if (!state.connected || state.connecting || state.supervising || state.networkBusy || state.flushing || state.recovering) {
    return {text:'Wait for the original Motet connection to be ready, then use /watch-status before retrying receipts.'};
  }
  if (data.conflict_ids?.includes(id)) return {text:'The original receipts conflict. Inspect this request with the bridge recovery tools in /watch-status.'};
  const request = data.requests[id];
  if (!request?.events.length || !Object.values(request.rejections || {}).some(value=>value.count >= MAX_RECEIPT_REJECTIONS)) {
    return {text:'This enrollment has no held receipt for that request ID. Check /watch-status.'};
  }
  state.connecting = true;
  try {
    if (await $.session.id() !== data.native_id || await sessionProject($) !== data.project ||
        state.data !== data || state.epoch !== epoch) return {text:'Open the original Motet conversation and check /watch-status.'};
    if (data.mode === 'derived') await recover($,state);
    if (state.data !== data || state.epoch !== epoch) return {text:'Open the original Motet conversation and check /watch-status.'};
    if (data.conflict_ids?.includes(id)) return {text:'The original receipts conflict. Inspect this request with the bridge recovery tools in /watch-status.'};
    await flush($,state,request);
    return {text:'Receipt retry check finished. Use /watch-status to see any receipts still held.'};
  } catch {
    return {text:'The original connection could not confirm the receipt. Use /watch-status to check it.'};
  } finally {state.connecting = false;}
}

async function observeInput($,state,e) {
  if (e.agentId !== undefined || !state.data || !state.bound.size) return;
  const id=state.data.native_id, project=state.data.project;
  for (const bound of state.bound.values()) {
    const request=bound.request;
    if (bound.native_id!==id || bound.project!==project || state.data.native_id!==id || state.data.project!==project ||
        state.data.requests[request.request_id]!==request || request.origin!=='peer' || request.execution_completed || request.mixed_input) continue;
    // Native admissions can be queued or steer the running turn. Conservatively withhold
    // its peer answer in either case; never expose owner input to another provider.
    request.mixed_input=true;
    await save($,state);
  }
}

async function completeRecoveredTurns($, state) {
  const data = state.data;
  if (state.lifecycleProtocol !== 2 || data?.mode !== 'derived' || !state.observedCompletions.size) return;
  for (const proof of state.recoverableTurns) {
    if (!proof || ![proof.request_id,proof.attempt_id,proof.generation].every(value => typeof value === 'string' && UUID.test(value)) ||
        !turnIdentifier(proof.turn_id) ||
        state.recoverableTurns.filter(item => item?.turn_id === proof.turn_id).length !== 1) continue;
    const observed = state.observedCompletions.get(proof.turn_id);
    const request = data.requests[proof.request_id];
    if (!observed || observed.native_id !== data.native_id || observed.project !== data.project ||
        data.conflict_ids?.includes(proof.request_id) ||
        !request || request.execution_completed || request.completed || request.origin !== 'peer' ||
        request.lifecycle_protocol !== 2 || request.attempted !== true ||
        request.attempt_id !== proof.attempt_id || request.generation !== proof.generation ||
        request.turn_id !== proof.turn_id || state.bound.has(proof.turn_id)) continue;
    // The authenticated server and the original plugin receipt agree on the
    // turn. Input isolation across an observation gap cannot be verified.
    await event($, state, request, 'completed', {
      turn_id:proof.turn_id, outcome:'withheld', answer:'',
      event_id:crypto.randomUUID(), occurred_at:observed.occurred_at
    });
    if (state.data !== data) return;
    state.observedCompletions.delete(proof.turn_id);
  }
}

const REGISTRATION_FAILED = 'Motet command registration failed.';
// Only the registration failure is cleared by a later successful registration; any other error stays.
function clearRegistrationError(state) {
  if (state.error === REGISTRATION_FAILED) state.error = '';
}

async function registerCommands($) {
  await $.command.register({name:'watch-setup',description:'Authorize automatic Motet connections once for your local Claude chats.',argumentHint:'<port:setup-code>',immediate:true});
  await $.command.register({name:'watch-connect',description:'Reconnect this chat, or use a legacy Motet pairing code.',argumentHint:'[port:pair-code]',immediate:true});
  await $.command.register({name:'watch-status',description:'Show Motet connection status without secrets.',immediate:true});
  await $.command.register({name:'watch-retry-receipts',description:'Retry held receipts in this original Motet enrollment.',argumentHint:'<request-id>',immediate:true});
  await $.command.register({name:'watch-disconnect',description:'Keep this chat disconnected until explicit reconnect.',immediate:true});
}

// Whether an awaited start or resume still owns the module: no session.start, session.end or transition command
// has come since it began. A continuation that lost ownership installs nothing and records nothing about itself.
function ownsLifecycle(state, lifecycle) {
  return state.lifecycle === lifecycle && !state.ended;
}

// The one supervisor timer: any earlier one is cancelled first, so at most one ever runs.
function startTimer($, state) {
  state.timer?.cancel();
  state.timer = $.clock.every(2000,()=>{
    // Count actual dispatch before any supervisor gate, never imply installation is dispatch.
    state.timerTicks = Math.min(Number.MAX_SAFE_INTEGER,state.timerTicks + 1);
    return supervisor($,state);
  });
}

// A plugin reload in a live chat runs register() again, and its old timer stops, but the host may not send this
// module a new session.start (it does so for a changed module only). The first event the reloaded module sees
// then puts back its commands and its timer. It proves nothing about the chat: no startup facts are invented, so
// eligibility still needs current Desktop evidence, and every tick keeps the same checks (the opt-out, identity,
// folder rules, receipts only). A session.start before or after this wins and keeps one timer.
// A resume belongs to the lifecycle it began in: session.start, session.end and every transition command change it,
// so a resume still registering then installs nothing. A transition command starts no resume itself, and after
// session.end later events start none either, except to repair a started module (see endLifecycle).
async function resume($, state) {
  if (state.timer || state.resuming || state.ended) return;
  const lifecycle = state.lifecycle;
  state.resuming = true;
  try {
    await registerCommands($);
    if (state.timer || !ownsLifecycle(state,lifecycle)) return;
    clearRegistrationError(state);
    state.resumed = true;
    state.resumedLifecycle = lifecycle;
    startTimer($,state);
  } catch {if (ownsLifecycle(state,lifecycle)) state.error = REGISTRATION_FAILED;}
  finally {state.resuming = false;}
}

export function register(on) {
  const state = {clientId:crypto.randomUUID(),sequence:-1,data:null,key:null,config:null,generation:null,connected:false,epoch:0,eligibilityEpoch:0,desktopBinding:null,disconnecting:0,
    timer:null,peerProtocol:0,lifecycleProtocol:0,folderProtocol:0,receiptOnly:false,receiptKey:null,abandonedReceipts:new Set(),bound:new Map(),noticeProtocol:0,noticeData:null,noticeKey:null,noticeFlushing:false,writes:Promise.resolve(),network:Promise.resolve(),networkBusy:false,flushing:false,supervising:false,
    submitting:false,flight:null,envelope:null,error:'',connecting:false,interactive:false,failures:0,retryAt:0,recoverAt:0,
    observedCompletions:new Map(),recoverableTurns:[],shardKeys:new Map(),transientReceiptRetries:new WeakMap(),recovering:false,
    rejectedGrants:new Set(),rejectedDecisionTokens:new Set(),runningReservations:new Map(),yieldedOptOuts:new Map(),rejectedIdentity:'',decision:0,appliedDecisions:new Map(),ownDecisions:new Map(),decisionProtocol:undefined,tickets:new Map(),ticketPorts:new Map(),serverOptedOut:new Set(),pendingRetryAt:0,reservationChain:Promise.resolve(),savedGrant:null,pendingOptOuts:new Map(),decisionReads:new Map(),ownCommands:new Map(),preferenceWrites:Promise.resolve(),lifecycle:0,transitionLifecycle:null,resumedLifecycle:null,ended:false,resuming:false,resumed:false,startSeen:false,startupInteractive:null,startupSurface:null,timerTicks:0,currentSurfaces:null};
  on('session.start',async($,e,next)=>{
    invalidateEligibility(state);
    state.lifecycle += 1;
    const lifecycle = state.lifecycle;
    state.ended = false;
    state.resumed = false;
    state.interactive = e.isInteractive === true;
    state.startSeen = true;
    state.startupInteractive = typeof e.isInteractive === 'boolean' ? e.isInteractive : null;
    state.startupSurface = surfaceRoster([e.surface])?.[0] ?? null;
    state.currentSurfaces = null;
    state.timerTicks = 0;
    try {
      state.timer?.cancel();
      state.timer = null;
      await registerCommands($);
      // A later start, a session.end or a transition command that came while this start was registering owns the
      // module now: this start installs nothing. After a /clear or a resume, a started module is then repaired by
      // its next event, as after a start that failed to register; after an exit it stays off.
      if (!ownsLifecycle(state,lifecycle)) return next(e);
      clearRegistrationError(state);
      startTimer($,state);
      // A resume that installed its timer while this start was registering is replaced: this timer is the start's.
      state.resumed = false;
    } catch {if (ownsLifecycle(state,lifecycle)) state.error = REGISTRATION_FAILED;}
    return next(e);
  });
  on('command.run',async($,e,next)=>{
    if (TRANSITIONS.has(e.command)) {discardPendingResume(state); invalidateEligibility(state); suspend($,state); return next(e);}
    resume($,state).catch(()=>{});
    const owner = ['composer','bridge'].includes(e.origin?.kind);
    if (e.command === 'watch-status') {
      await readSurfaces($,state);
      let now = null;
      if (owner) {
        try {now = await $.clock.now();} catch { /* A missing age does not change connection state. */ }
      }
      return {text:JSON.stringify(publicStatus(state,owner,now))};
    }
    if (!['watch-connect','watch-disconnect','watch-setup','watch-retry-receipts'].includes(e.command)) return next(e);
    if (!owner) return {text:'Motet connection commands require direct owner input.'};
    if (e.command === 'watch-retry-receipts') return retryHeldReceipts($,state,e.args.trim());
    if (e.command === 'watch-disconnect') {
      try {
        const done = await explicitDisconnect($,state);
        return {text:done === true ? 'Motet disconnected. ' + LIMITATION : done === 'elsewhere' ? NEWER_ELSEWHERE :
          done === 'yielded' ? YIELDED_OPT_OUT : SUPERSEDED};
      }
      catch {return {text:'Motet disconnect could not be saved; readiness expires when its heartbeat stops.'};}
    }
    if (state.connecting || state.submitting || (state.connected && state.flight && ['pending','claimed','submitted','started'].includes(state.flight.phase))) return {text:'An existing Motet attempt is unresolved; nothing was resubmitted.'};
    state.connecting = true;
    const decision = reserveDecision(state);
    let id = null, changed = false;
    // A session.end, transition or opt-out that comes while this command waits on Motet changes this epoch, and
    // activation then refuses: an owner connect never connects a chat that has ended or changed conversation.
    const eligibilityEpoch = invalidateEligibility(state);
    try {
      // Its place in the owner's order at Motet, taken before anything else this command waits on (its chat, the
      // folder, Motet's setup answer, the pairing). If the command then fails, Motet has only the reserved ticket.
      const early = await queueReservation(state,() => reserveEarly($,state,decision));
      if (early?.seq) state.runningReservations.set(decision, null);
      id = await $.session.id();
      if (overtaken(state,decision,id)) return {text:SUPERSEDED};
      if (!UUID.test(id)) throw Error('Native identity unavailable.');
      const placement = await placeCommand($,state,id,decision,early);
      // A command whose place Motet cannot confirm ends here, with nothing changed: it is never given a later one.
      if (placement.unplaced) return {text:UNPLACED};
      if (placement.unreachable) return {text:UNREACHABLE};
      const ticket = placement.ticket;
      if (overtaken(state,decision,id) || !reservedInOrder(state,decision,ticket,id)) return {text:SUPERSEDED};
      const project = await sessionProject($);
      if (project === null) return {text:'This chat is working outside its project folder. Return to the project folder and try again.'};
      const pair = e.args.trim() ? pairing(e.args) : null;
      if (e.args.trim() && !pair) return {text:'Use the exact port:code command from Motet.'};
      if (e.command === 'watch-setup') {
        if (!pair) return {text:'Use the exact /watch-setup command from Motet.'};
        const config = await post($,state,'setup',{code:pair.code},{port:pair.port,anonymous:true});
        if (!globalConfiguration(config) || config.port !== pair.port) throw Error('Invalid Motet setup response.');
        // The one-use code is spent: its grant is kept even if a /watch-disconnect came meanwhile, and the setup is
        // never repeated. (No second connect or setup can run in this copy while this one does.)
        await $.store.set(grantKey(config),config);
        await $.store.set(GLOBAL_KEY,config);
        // This copy's previous grant from the same Motet was revoked by this setup: drop its key (only that key).
        const previous = state.savedGrant;
        state.savedGrant = config;
        if (previous && previous.port === config.port && grantKey(previous) !== grantKey(config)) $.store.delete(grantKey(previous)).catch(() => {});
        state.rejectedGrants.delete(configurationId(config)); state.rejectedIdentity = '';
        // Another window's disconnect kept pending because no sign-in could place it is told to Motet first, with the
        // new grant, and this setup's reconnect is placed after it.
        const after = overtaken(state,decision,id) ? {ticket} : await placeAfterPending($,state,id,decision,ticket);
        if (!after.ticket && (after.undelivered || after.unplaced || after.unreachable)) {
          state.config = config; state.retryAt = 0; state.failures = 0;
          return {text:SETUP_KEPT_OFF + LIMITATION};
        }
        const outcome = {};
        const saved = await savePreference($,state,id,false,decision,after.ticket,{outcome});
        // A later decision about this chat stands: the grant serves the other chats, and this one stays as it is.
        if (saved !== true) {
          state.config = config; state.retryAt = 0; state.failures = 0;
          return {text:'Motet automatic connections authorized. Your later Motet command in this chat still stands, so this chat was left as it is. ' + LIMITATION};
        }
        changed = true;
        suspend($,state);
        state.data = null; state.key = null; state.config = config; state.receiptOnly = false;
        state.flight = null; state.envelope = null; state.bound.clear();
        state.retryAt = 0; state.failures = 0;
        if (outcome.unconfirmed) return {text:'Motet automatic connections authorized. Motet stopped answering before it confirmed this chat\'s reconnect: check /watch-status, or run /watch-connect here again. ' + LIMITATION};
        return {text:'Motet automatic connections authorized. Loaded eligible chats will connect automatically. ' + LIMITATION};
      }
      if (pair) {
        // A pairing is checked by Motet before anything changes: the code is sent first, and Motet commits the
        // reconnect (under this command's ticket) only once it accepts the code. A refused code leaves the chat's
        // decision, here and at Motet, exactly as it was. A superseded command sends nothing.
        if (overtaken(state,decision,id)) return {text:SUPERSEDED};
        if (state.eligibilityEpoch !== eligibilityEpoch) return {text:CONNECT_FAILED};
        let response;
        try {
          response = await post($,state,'enroll',{code:pair.code,native_id:id,project,
            ...state.decisionProtocol === 1 && ticket && ticket.port === pair.port ? {decision_seq:ticket.seq} : {}},{port:pair.port,anonymous:true});
        } catch (error) {
          if (error.code === 'decision_superseded') {recordSuperseded(state,id); return {text:OTHER_WINDOW};}
          if (error.status === 403) return {text:PAIRING_REFUSED};
          throw error;
        }
        if (typeof response.token !== 'string' || response.token.length < 16 || !UUID.test(response.enrollment_id)) throw Error('Invalid enrollment.');
        const enrolled = {native_id:id,project,port:pair.port,requests:{},active:false,token:response.token,enrollment_id:response.enrollment_id};
        // Another window's disconnect kept pending because no sign-in could place it is told to Motet now, with this
        // pairing, and the reconnect is placed again after it (that disconnect ends the connection the pairing made;
        // the heartbeat connects again on the pairing). If it cannot be told, the chat stays disconnected.
        if (!overtaken(state,decision,id) && (await pendingOptOuts($,id)).some(({key}) => !state.pendingOptOuts.get(id)?.has(key))) {
          const endPairing = () => post($,state,'disconnect',{generation:response.generation,native_id:id},{port:pair.port,token:response.token}).catch(() => {});
          await $.store.set(legacyKey(id),enrolled);
          const after = await placeAfterPending($,state,id,decision,null);
          if (after.undelivered || after.unplaced || after.unreachable) {endPairing(); return {text:PAIRING_KEPT_OFF};}
          const outcome = {};
          const saved = await savePreference($,state,id,false,decision,after.ticket,{outcome});
          if (saved === 'elsewhere') {endPairing(); return {text:OTHER_WINDOW};}
          if (saved !== true) {endPairing(); return {text:ENDED_SUPERSEDED};}
          state.rejectedIdentity = ''; state.retryAt = 0; state.failures = 0;
          changed = true;
          stop(state);
          state.key = legacyKey(id); state.data = {...enrolled,active:true};
          await $.store.set(legacyKey(id),state.data);
          state.flight = null; state.envelope = null; state.bound.clear();
          return {text:outcome.unconfirmed ? UNCONFIRMED : 'Motet reconnect scheduled for this existing chat. ' + LIMITATION};
        }
        const saved = await savePreference($,state,id,false,decision,ticket && ticket.port === pair.port ? ticket : null,{committed:true});
        if (saved !== true) {
          // A newer command replaced this one while Motet paired it: end the connection Motet just made.
          post($,state,'disconnect',{generation:response.generation,native_id:id},{port:pair.port,token:response.token}).catch(() => {});
          return {text:ENDED_SUPERSEDED};
        }
        state.rejectedIdentity = ''; state.retryAt = 0; state.failures = 0;
        changed = true;
        stop(state);
        state.key = legacyKey(id); state.data = enrolled;
        state.flight = null; state.envelope = null; state.bound.clear();
        const refusal = await ownerActivate($,state,response,id,decision,eligibilityEpoch);
        if (refusal) return refusal;
        return {text:'Motet connected to this existing chat. ' + LIMITATION};
      }
      // Another window's pending disconnect is told to Motet first, and this reconnect is placed after it.
      const after = overtaken(state,decision,id) ? {ticket} : await placeAfterPending($,state,id,decision,ticket);
      if (after.undelivered) return {text:PENDING_UNDELIVERED};
      if (after.unplaced) return {text:UNPLACED};
      if (after.unreachable) return {text:UNREACHABLE};
      const outcome = {};
      const saved = await savePreference($,state,id,false,decision,after.ticket,{outcome});
      if (saved === 'elsewhere') return {text:OTHER_WINDOW};
      if (!saved) return {text:SUPERSEDED};
      state.rejectedIdentity = ''; state.retryAt = 0; state.failures = 0;
      changed = true;
      stop(state);
      if (state.data?.mode !== 'derived') {
        const stored = await $.store.get(legacyKey(id));
        // An owner-requested reconnect of this chat's manual pairing, including one an earlier plugin made from a
        // subfolder: it offers the project folder on the same manual credential, and Motet moves it only to the
        // proven launch folder. It stays manual; nothing is saved unless Motet accepts.
        if (stored?.native_id === id && (stored.project === project || insideFolder(stored.project, project, await pathRules($)))) {
          const receiptKey = receiptContext('manual', stored.project);
          const receiptOnly = stored.project !== project && !state.abandonedReceipts.has(receiptKey) &&
            await pendingReceipts([stored]);
          // Checked after every awaited step and before anything changes or is sent: a superseded command sends
          // no connect request, so Motet never becomes ready for it.
          const before = await decisionStands($,state,id,decision,eligibilityEpoch);
          if (before) return before;
          state.receiptKey = receiptKey; state.receiptOnly = receiptOnly;
          state.key = legacyKey(id); state.data = {...stored, project:state.receiptOnly ? stored.project : project, active:true};
          let response;
          try {
            response = await post($,state,'connect',{native_id:id,project:state.data.project,...state.receiptOnly?{receipt_only:true}:{},
              ...state.decisionProtocol === 1 && state.tickets.has(id) ? {decision_seq:state.tickets.get(id)} : {}});
          } catch (error) {
            // Motet refused it before any connection existed: a newer opt-out came from another window or copy.
            if (error.code === 'decision_superseded') {stop(state); recordSuperseded(state,id); return {text:OTHER_WINDOW};}
            throw error;
          }
          const refusal = await ownerActivate($,state,response,id,decision,eligibilityEpoch);
          if (refusal) return refusal;
          return {text:state.receiptOnly ? 'Motet reconnected this chat to report its earlier replies; it then moves to this project folder. ' + LIMITATION :
            'Motet connected to this existing chat. ' + LIMITATION};
        }
        if (overtaken(state,decision,id)) return {text:SUPERSEDED};
        state.data = null; state.key = null;
        if (stored?.native_id === id) return {text:"This chat's manual Motet pairing is for a folder outside this project, so it was not reconnected. Pair this chat again from Motet."};
      }
      if (overtaken(state,decision,id)) return {text:SUPERSEDED};
      if (outcome.unconfirmed) return {text:UNCONFIRMED};
      return {text:'Motet reconnect scheduled for this existing chat. ' + LIMITATION};
    // A command that failed after it began changing the connection ends it, and tells Motet, so no lease is left that
    // nobody renews. One that failed before (a mistyped setup code, say) leaves the connection as it was.
    } catch {if (changed) suspend($,state); return {text:CONNECT_FAILED};}
    finally {state.connecting = false; settleYieldedOptOut($,state,id,decision);}
  }).catch(($,e,next)=>{stop(state); return ['watch-connect','watch-disconnect','watch-status','watch-setup','watch-retry-receipts'].includes(e.command)?{text:'Motet command failed; no automatic request retry.'}:next(e);});
  on('prompt.submit',async($,e,next)=>{
    resume($,state).catch(()=>{});
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('session.receive',async($,e,next)=>{
    resume($,state).catch(()=>{});
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('turn.start',async($,e,next)=>{
    resume($,state).catch(()=>{});
    try {
      const request = state.flight, data = state.data, eligibility = state.eligibilityEpoch;
      if (request && data && request.kind!=='peer_reply_notice' && !request.turn_id && e.agentId === undefined &&
          turnIdentifier(e.turnId) && matchingPrompt(e.text,state.envelope) &&
          await $.session.id() === data.native_id && await sessionProject($) === data.project &&
          state.data === data && state.flight === request && state.eligibilityEpoch === eligibility && !request.turn_id) {
        request.turn_id = e.turnId;
        state.bound.set(e.turnId,{request,native_id:state.data.native_id,project:state.data.project,enrollment_id:state.data.enrollment_id});
        await event($,state,request,'started',{turn_id:e.turnId});
      }
    } catch { /* Motet cannot block native admission. */ }
    return next(e);
  });
  on('turn.complete',async($,e,next)=>{
    resume($,state).catch(()=>{});
    try {
      if (e.agentId !== undefined || !turnIdentifier(e.turnId)) return next(e);
      const bound = state.bound.get(e.turnId), request = bound?.request, data=state.data, eligibility=state.eligibilityEpoch;
      const native=await $.session.id(), project=await sessionProject($);
      if (state.eligibilityEpoch!==eligibility) return next(e);
      if (request && !request.execution_completed && data && state.data === data && data.requests[request.request_id] === request &&
          bound.native_id === state.data.native_id && bound.project === state.data.project && bound.enrollment_id === state.data.enrollment_id &&
          native === bound.native_id && project === bound.project) {
        const outcome = request.mixed_input ? 'withheld' : e.isAborted ? 'aborted' :
          e.reason === 'answer' && matchingAnswer(e.answer,request.request_id) ? 'answered' : 'unusable';
        if (request.lifecycle_protocol === 2) {
          await event($,state,request,'completed',{turn_id:e.turnId,outcome,answer:outcome==='answered'?e.answer:'',event_id:crypto.randomUUID(),occurred_at:await $.clock.now()});
        } else if (outcome === 'answered') await event($,state,request,'answered',{turn_id:e.turnId,answer:e.answer});
        else {await event($,state,request,'unknown'); await save($,state,()=>{request.execution_completed=true;request.capture=outcome;});}
        state.bound.delete(e.turnId);
        if (!state.submitting && state.flight === request) {state.flight = null; state.envelope = null;}
      } else if (!bound && UUID.test(native) && typeof project === 'string' && project) {
        const occurred_at = await $.clock.now();
        if (state.eligibilityEpoch !== eligibility) return next(e);
        state.observedCompletions.set(e.turnId, {native_id:native, project, occurred_at});
        if (state.observedCompletions.size > 32) state.observedCompletions.delete(state.observedCompletions.keys().next().value);
        await completeRecoveredTurns($, state);
      }
    } catch { /* Retained completion can replay without a new prompt. */ }
    return next(e);
  });
  on('session.attach',async($,e,next)=>{resume($,state).catch(()=>{}); return next(e);});
  on('session.end',async($,e,next)=>{endLifecycle(state,e.reason); invalidateEligibility(state); suspend($,state); return next(e);});
}
