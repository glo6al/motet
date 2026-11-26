import {UUID, LIMITATION, TRANSITIONS, MAX_RECEIPT_REJECTIONS, pairing, sessionResponse, validClaim, matchingPrompt, matchingAnswer, turnIdentifier, publicStatus, globalConfiguration, receiptRecord, surfaceRoster, validNoticeClaim, noticeReceipt, lifecycleReceipt} from './protocol.js';

const GLOBAL_KEY = 'watch-owner-global-v1';
const legacyKey = id => 'watch-owner-v1:' + id;
const preferenceKey = id => 'watch-owner-preferences-v1:' + id;
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
    const platform = typeof $.session.info === 'function' ? (await $.session.info())?.platform : undefined;
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
// Receipts still to report: retained events neither acknowledged nor refused for good. A context with any keeps
// its earlier folder until they are reported, so the chat does not move away from replies it still holds.
async function unreported(requests, acknowledged = {}) {
  for (const request of Object.values(requests || {})) {
    for (const value of Array.isArray(request?.events) ? request.events : []) {
      const hash = await fingerprint(canonicalReceipt(value));
      if (acknowledged[hash] || acknowledged[await fingerprint(value)]) continue;
      if ((request.rejections?.[hash]?.count ?? 0) >= MAX_RECEIPT_REJECTIONS) continue;
      return true;
    }
  }
  return false;
}

// An automatic connection made by an earlier plugin from a folder inside this project, whose retained outcomes are
// not all reported yet: its folder, so the plugin reconnects there for receipts only before moving.
async function earlierReceipts($, state, id, project, config) {
  const keys = await $.store.keys();
  if (!Array.isArray(keys) || keys.length > 4096) return null;
  const rules = await pathRules($), contexts = new Map();
  for (const key of keys) {
    if (typeof key !== 'string' || ![shardPrefix(id), legacyShardPrefix(id)].some(prefix => key.startsWith(prefix))) continue;
    const shard = await $.store.get(key);
    if (!shard || shard.mode !== 'derived' || shard.native_id !== id || shard.grant_id !== config.grant_id ||
        shard.project === project || !insideFolder(shard.project, project, rules)) continue;
    const scope = JSON.stringify([shard.project, shard.enrollment_id]);
    if (!contexts.has(scope)) contexts.set(scope, []);
    contexts.get(scope).push(shard);
  }
  for (const shards of contexts.values()) {
    const acknowledged = Object.assign({}, ...shards.map(shard => shard.acknowledged || {}));
    for (const shard of shards) if (await unreported(shard.requests, acknowledged)) return shard.project;
  }
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
  if (typeof $.session.root === 'function') {
    let root = null;
    try { root = await $.session.root(); } catch { /* fall back to the stricter current folder */ }
    if (typeof root === 'string' && root) {
      const cwd = await $.session.cwd();
      return insideFolder(cwd, root, await pathRules($)) ? root : null;
    }
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
      const response = await $.http.fetch('http://127.0.0.1:' + port + '/claude-mod/' + path,
        {method:'POST',headers,body:JSON.stringify(body)});
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
  let now = 0, eligibilityEpoch = state.eligibilityEpoch;
  try {
    now = await $.clock.now();
    const id = await $.session.id(), project = await sessionProject($);
    checkEligibility(state,eligibilityEpoch);
    if (state.desktopBinding && (state.desktopBinding.native_id !== id || state.desktopBinding.project !== project)) {
      // Refresh only at our synchronous invalidation, never after awaited cleanup.
      eligibilityEpoch = invalidateEligibility(state);
    }
    const raw = await $.store.get(GLOBAL_KEY);
    checkEligibility(state,eligibilityEpoch);
    const config = globalConfiguration(raw) ? raw : null;
    const preference = UUID.test(id) ? await $.store.get(preferenceKey(id)) : null;
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
        !await unreported(state.data.requests, state.data.acknowledged)) {
      // Every outcome from the earlier folder is reported: leave that context and connect in the project folder.
      const data = state.data;
      suspend($,state);
      state.receiptOnly = false; state.retryAt = 0; state.failures = 0;
      if (data.mode === 'derived') {state.data = null; state.key = null;}
      else state.data = {...data, project};
      return;
    }
    if (now < state.retryAt) return;
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
          state.receiptOnly = await unreported(manual.requests);
          state.data = state.receiptOnly ? manual : {...manual, project}; state.key = legacyKey(id);
        }
      }
      if (state.data?.mode === 'derived' || (!state.data && config)) {
        if (!config || state.rejectedGrant === configurationId(config) || state.rejectedIdentity === await refusalKey($,state,id,project,config)) return;
        // Report an earlier folder's retained outcomes first (receipts only, no claims); then move.
        const earlier = await earlierReceipts($,state,id,project,config);
        checkEligibility(state,eligibilityEpoch);
        state.receiptOnly = earlier !== null;
        if (state.data && state.data.project !== (earlier ?? project)) {state.data = null; state.key = null;}
        await connectDerived($,state,id,earlier ?? project,config,eligibilityEpoch);
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
    if (error.status === 403 && state.config) state.rejectedGrant = configurationId(state.config);
    if (error.code === 'identity_rejected') {
      const id = await $.session.id().catch(()=>null), project = await sessionProject($).catch(()=>null);
      const key = await refusalKey($,state,id,project,state.config);
      if (state.eligibilityEpoch !== eligibilityEpoch) return;
      state.rejectedIdentity = key;
    }
    state.error = error.message === 'Motet recovery data limit.' ? 'Motet recovery data limit reached; automatic claims stopped.' :
      'Motet connection interrupted; existing attempts are never resubmitted.';
    backoff(state,now);
  } finally {state.supervising = false;}
}

async function explicitDisconnect($, state) {
  state.disconnecting += 1;
  invalidateEligibility(state);
  try {
    const id = await $.session.id();
    if (UUID.test(id)) await $.store.set(preferenceKey(id),{version:1,disconnected:true});
    const data = state.data;
    suspend($,state);
    if (data && data.mode !== 'derived') await save($,state,()=>{data.active = false;});
  } finally {state.disconnecting -= 1;}
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

export function register(on) {
  const state = {clientId:crypto.randomUUID(),sequence:-1,data:null,key:null,config:null,generation:null,connected:false,epoch:0,eligibilityEpoch:0,desktopBinding:null,disconnecting:0,
    timer:null,peerProtocol:0,lifecycleProtocol:0,folderProtocol:0,receiptOnly:false,bound:new Map(),noticeProtocol:0,noticeData:null,noticeKey:null,noticeFlushing:false,writes:Promise.resolve(),network:Promise.resolve(),networkBusy:false,flushing:false,supervising:false,
    submitting:false,flight:null,envelope:null,error:'',connecting:false,interactive:false,failures:0,retryAt:0,recoverAt:0,
    observedCompletions:new Map(),recoverableTurns:[],shardKeys:new Map(),transientReceiptRetries:new WeakMap(),recovering:false,
    rejectedGrant:'',rejectedIdentity:'',startSeen:false,startupInteractive:null,startupSurface:null,timerTicks:0,currentSurfaces:null};
  on('session.start',async($,e,next)=>{
    invalidateEligibility(state);
    state.interactive = e.isInteractive === true;
    state.startSeen = true;
    state.startupInteractive = typeof e.isInteractive === 'boolean' ? e.isInteractive : null;
    state.startupSurface = surfaceRoster([e.surface])?.[0] ?? null;
    state.currentSurfaces = null;
    state.timerTicks = 0;
    try {
      state.timer?.cancel();
      state.timer = null;
      await $.command.register({name:'watch-setup',description:'Authorize automatic Motet connections once for your local Claude chats.',argumentHint:'<port:setup-code>',immediate:true});
      await $.command.register({name:'watch-connect',description:'Reconnect this chat, or use a legacy Motet pairing code.',argumentHint:'[port:pair-code]',immediate:true});
      await $.command.register({name:'watch-status',description:'Show Motet connection status without secrets.',immediate:true});
      await $.command.register({name:'watch-retry-receipts',description:'Retry held receipts in this original Motet enrollment.',argumentHint:'<request-id>',immediate:true});
      await $.command.register({name:'watch-disconnect',description:'Keep this chat disconnected until explicit reconnect.',immediate:true});
      state.timer = $.clock.every(2000,()=>{
        // Count actual dispatch before any supervisor gate, never imply installation is dispatch.
        state.timerTicks = Math.min(Number.MAX_SAFE_INTEGER,state.timerTicks + 1);
        return supervisor($,state);
      });
    } catch {state.error = 'Motet command registration failed.';}
    return next(e);
  });
  on('command.run',async($,e,next)=>{
    if (TRANSITIONS.has(e.command)) {invalidateEligibility(state); suspend($,state); return next(e);}
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
      try {await explicitDisconnect($,state); return {text:'Motet disconnected. ' + LIMITATION};}
      catch {return {text:'Motet disconnect could not be saved; readiness expires when its heartbeat stops.'};}
    }
    if (state.connecting || state.submitting || (state.connected && state.flight && ['pending','claimed','submitted','started'].includes(state.flight.phase))) return {text:'An existing Motet attempt is unresolved; nothing was resubmitted.'};
    state.connecting = true;
    invalidateEligibility(state);
    try {
      const id = await $.session.id(), project = await sessionProject($);
      if (!UUID.test(id)) throw Error('Native identity unavailable.');
      if (project === null) return {text:'This chat is working outside its project folder. Return to the project folder and try again.'};
      const pair = e.args.trim() ? pairing(e.args) : null;
      if (e.args.trim() && !pair) return {text:'Use the exact port:code command from Motet.'};
      if (e.command === 'watch-setup') {
        if (!pair) return {text:'Use the exact /watch-setup command from Motet.'};
        const config = await post($,state,'setup',{code:pair.code},{port:pair.port,anonymous:true});
        if (!globalConfiguration(config) || config.port !== pair.port) throw Error('Invalid Motet setup response.');
        await $.store.set(GLOBAL_KEY,config);
        state.rejectedGrant = ''; state.rejectedIdentity = '';
        await $.store.set(preferenceKey(id),{version:1,disconnected:false});
        suspend($,state);
        state.data = null; state.key = null; state.config = config; state.receiptOnly = false;
        state.flight = null; state.envelope = null; state.bound.clear();
        state.retryAt = 0; state.failures = 0;
        return {text:'Motet automatic connections authorized. Loaded eligible chats will connect automatically. ' + LIMITATION};
      }
      await $.store.set(preferenceKey(id),{version:1,disconnected:false});
      state.rejectedIdentity = ''; state.retryAt = 0; state.failures = 0;
      stop(state);
      if (pair) {
        state.key = legacyKey(id);
        state.data = {native_id:id,project,port:pair.port,requests:{},active:false};
        const response = await post($,state,'enroll',{code:pair.code,native_id:id,project},{anonymous:true});
        if (typeof response.token !== 'string' || response.token.length < 16 || !UUID.test(response.enrollment_id)) throw Error('Invalid enrollment.');
        state.data.token = response.token; state.data.enrollment_id = response.enrollment_id;
        state.flight = null; state.envelope = null; state.bound.clear();
        await activate($,state,response);
        return {text:'Motet connected to this existing chat. ' + LIMITATION};
      }
      if (state.data?.mode !== 'derived') {
        const stored = await $.store.get(legacyKey(id));
        // An owner-requested reconnect of this chat's manual pairing, including one an earlier plugin made from a
        // subfolder: it offers the project folder on the same manual credential, and Motet moves it only to the
        // proven launch folder. It stays manual; nothing is saved unless Motet accepts.
        if (stored?.native_id === id && (stored.project === project || insideFolder(stored.project, project, await pathRules($)))) {
          state.receiptOnly = stored.project !== project && await unreported(stored.requests);
          state.key = legacyKey(id); state.data = {...stored, project:state.receiptOnly ? stored.project : project, active:true};
          await activate($,state,await post($,state,'connect',{native_id:id,project:state.data.project,...state.receiptOnly?{receipt_only:true}:{}}));
          return {text:state.receiptOnly ? 'Motet reconnected this chat to report its earlier replies; it then moves to this project folder. ' + LIMITATION :
            'Motet connected to this existing chat. ' + LIMITATION};
        }
        state.data = null; state.key = null;
        if (stored?.native_id === id) return {text:"This chat's manual Motet pairing is for a folder outside this project, so it was not reconnected. Pair this chat again from Motet."};
      }
      return {text:'Motet reconnect scheduled for this existing chat. ' + LIMITATION};
    } catch {stop(state); return {text:'Motet connection failed. If setup was interrupted, get a fresh setup command. No request was resubmitted.'};}
    finally {state.connecting = false;}
  }).catch(($,e,next)=>{stop(state); return ['watch-connect','watch-disconnect','watch-status','watch-setup','watch-retry-receipts'].includes(e.command)?{text:'Motet command failed; no automatic request retry.'}:next(e);});
  on('prompt.submit',async($,e,next)=>{
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('session.receive',async($,e,next)=>{
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('turn.start',async($,e,next)=>{
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
  on('session.end',async($,e,next)=>{invalidateEligibility(state); suspend($,state); return next(e);});
}
