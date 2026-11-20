import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {supervisedVmProbe} from './fixture-vm-probe.mjs';
import {ATTACHMENT_SCENARIOS, createAttachmentsFixture} from './fixture.mjs';

const active = scenario => createAttachmentsFixture({enabled: true, search: '?demo', scenario});
const state = scenario => active(scenario).snapshot();
const command = (adapter, type, extra = {}) => {
  const {draft} = adapter.snapshot();
  return {type, draftId: draft.id, destination: draft.destination, revision: draft.revision,
    writerGeneration: draft.writerGeneration, ...extra};
};
const act = (adapter, type, extra) => adapter.act(command(adapter, type, extra));
// Compare legacy values independently of explicitly instance-scoped capability metadata.
const legacyValues = snapshot => {
  const s = JSON.parse(JSON.stringify(snapshot)); delete s.management;
  for (const c of s.officerAnswer?.citations || []) if (c.navigation.kind === 'preview') {
    delete c.navigation.authority; delete c.navigation.citationTarget.access;
  }
  return s;
};
const EXPECTED = `a01-agent a01-picker a01-officer a01-room a02-drop a02-preparing a03-one a03-five a03-relay a03-room-one a03-room-five a03-details a04-mixed a04-scanned a04-relay-folder a04-relay-officer a04-budget a04-folder a04-quota a04-recipient a05-saved a05-scope a05-resume a05-conflict a05-missing a06-preparing a06-queued a06-relay-unknown a06-relay-partial a06-unknown a06-partial a06-partial-capacity a06-cleaned a09-officer-private a09-officer-supplied a09-officer-unknown a09-officer-page a09-officer-image a09-officer-retained a09-officer-retained-earlier a09-officer-retained-unknown a04-officer-media a04-officer-model a08-officer-direct a08-officer-uncited a08-officer-followup a08-officer-forward a06-officer-retained a06-officer-rejected a07-answered a07-together a08-doc a08-compare a08-gone a08-forward a09-pdf a09-image a09-relay-pdf a09-relay-image a09-text a09-fallback a10-files a10-reattach a11-full a11-confirm a11-progress a11-error a11-done a11-storage a11-storage-set a11-expiry a11-expiry-all a11-expiry-set a11-expiry-all-set a11-storage-all-set a10-files-expiry a11-expiry-selection-changed a11-expiry-removal-started a11-expiry-partial a11-expiry-unconfirmed`.split(' ');

test('all 80 scenario keys are stable, JSON values and referentially complete', () => {
  assert.deepEqual(ATTACHMENT_SCENARIOS, EXPECTED);
  assert.equal(new Set(ATTACHMENT_SCENARIOS).size, 80);
  assert.ok(Object.isFrozen(ATTACHMENT_SCENARIOS));
  for (const key of EXPECTED) {
    const s = state(key);
    assert.deepEqual(JSON.parse(JSON.stringify(s)), s);
    assert.equal(s.schemaVersion, 1); assert.equal(s.fixture, true); assert.equal(s.enabled, true);
    assert.equal(s.scenario, key); assert.equal(s.capabilities.sample, true);
    assert.equal(s.capabilities.storageLimitBytes, 5_000_000_000);
    assert.equal(s.storage.limitBytes, 5_000_000_000);
    assert.equal(s.capabilities.maxFiles, 5);
    assert.equal(s.capabilities.maxFileBytes, 10 * 1_048_576);
    assert.equal(s.capabilities.maxDraftBytes, 20 * 1_048_576);
    if (!key.includes('expiry') && !key.endsWith('-set')) assert.ok(s.files.every(f => f.retention.expiresAt === null && f.availability !== 'expired'));
    assert.ok(Number.isInteger(s.filesCount) && s.filesCount >= 0);
    const ids = s.files.map(f => f.id);
    assert.equal(new Set(ids).size, ids.length, key);
    const references = [...s.draft.fileIds, ...s.messages.flatMap(m => m.fileIds),
      ...(s.filesList?.entries || []).flatMap(e => e.fileIds), ...(s.deleteDialog?.fileIds || []),
      ...(s.officerAnswer?.citations || []).map(c => c.fileId).filter(Boolean),
      ...[s.preview?.fileId, s.ui.detailsFileId].filter(Boolean)];
    references.forEach(id => assert.ok(ids.includes(id), `${key} references ${id}`));
    assert.ok(s.files.every(f => Number.isInteger(f.bytes) && f.bytes > 0));
    assert.ok(s.files.every(f => !Object.hasOwn(f, 'path') && !Object.hasOwn(f, 'token')));
    if (s.draft.canSend) {
      assert.equal(s.draft.editable, true);
      assert.ok(s.draft.fileIds.every(id => s.files.find(f => f.id === id).preparation === 'ready'));
    }
  }
});

test('off by default; explicit true and actual demo query key both required', () => {
  for (const options of [{}, {search: '?demo'}, {enabled: true}, {enabled: 1, search: '?demo'},
    {enabled: true, search: '?demonstration'}, {enabled: true, search: '?x=demo'},
    {enabled: true, search: '?demoish=1'}, {enabled: false, search: '?demo'}]) {
    const a = createAttachmentsFixture(options), s = a.snapshot();
    assert.equal(a.enabled, false); assert.equal(s.enabled, false); assert.equal(s.scenario, null);
    assert.equal(s.destination, null); assert.equal(s.draft, null); assert.deepEqual(s.files, []);
    assert.deepEqual(s.messages, []); assert.equal(s.filesCount, 0); assert.equal(s.capabilities.available, false);
    assert.deepEqual(a.selectScenario('a06-queued'), s);
    assert.throws(() => a.act({type: 'open_picker', draftId: 'none', destination: {}, revision: 1, writerGeneration: 1}), /unavailable/);
  }
  assert.equal(createAttachmentsFixture({enabled: true, search: '?other=x&demo'}).enabled, true);
  assert.throws(() => createAttachmentsFixture({scenario: 'unknown'}), /Unknown/);
  assert.throws(() => active('a01-agent').selectScenario('unknown'), /Unknown/);
});

test('snapshots and instances do not share mutable objects', () => {
  const a = active('a03-five'), b = active('a03-five');
  const first = a.snapshot(); first.files[0].name = 'modified'; first.draft.destination.scope.agentId = 'other'; first.messages.push({});
  assert.equal(a.snapshot().files[0].name, 'delivery-plan.pdf');
  assert.equal(b.snapshot().files[0].name, 'delivery-plan.pdf');
  const result = a.selectScenario('a06-queued'); result.messages[0].fileIds.length = 0;
  assert.equal(a.snapshot().messages[0].fileIds.length, 2);
});

test('preparation stays private, keeps text editable and blocks Send', () => {
  const s = state('a02-preparing');
  assert.equal(s.draft.editable, true); assert.equal(s.draft.canSend, false);
  assert.equal(s.files[0].uploadPercent, 62); assert.equal(s.files[0].preparation, 'uploading');
  assert.equal(s.files[1].preparation, 'preparing'); assert.equal(s.files[2].preparation, 'ready');
  assert.ok(s.files.every(f => f.availability === 'private' && f.availableTo === null));
  assert.deepEqual(s.messages, []);
  for (const key of ['a04-mixed', 'a04-budget', 'a04-folder', 'a04-quota', 'a04-recipient']) assert.equal(state(key).draft.canSend, false);
  assert.deepEqual(state('a04-mixed').files.filter(f => f.error).map(f => f.error.code), ['locked_pdf', 'unsupported_type']);
  assert.equal(state('a04-budget').files[1].error.code, 'officer_budget');
  assert.equal(state('a04-budget').capabilities.analysisOnly, true);
});

test('sending, queueing, uncertainty, partial failure and answers remain different claims', () => {
  const preparing = state('a06-preparing'), queued = state('a06-queued'), unknown = state('a06-unknown');
  assert.equal(preparing.messages[0].state, 'preparing_delivery');
  assert.ok(preparing.files.every(f => f.availability === 'saving' && !f.availableTo));
  assert.equal(queued.messages[0].state, 'queued');
  assert.ok(queued.files.every(f => f.availability === 'available' && f.availableTo === 'Ada' && f.retention.pinned));
  assert.equal(unknown.messages[0].statusText, 'Delivery unknown · check status; do not resend');
  assert.deepEqual(unknown.messages[0].actions, ['check_status']);
  assert.deepEqual(unknown.messages[0].recovery, {canCheck: true, canFinish: false, canDeleteSavedCopies: false});
  const partial = state('a06-partial');
  assert.equal(partial.messages[0].state, 'partial_not_sent');
  assert.deepEqual(partial.files.map(f => f.availability), ['available', 'not_saved']);
  assert.deepEqual(partial.messages[0].actions, ['finish_send', 'delete_saved_copies']);
  assert.equal(partial.filesCount, 1);
  const answered = state('a07-answered');
  assert.equal(answered.messages[0].state, 'answered');
  assert.ok(answered.files.every(f => !f.usedInAnswer && !f.retention.pinned && f.retention.expiresAt === null));
});

test('saved scopes and inaccessible drafts never claim implicit transfer or send', () => {
  const saved = state('a05-saved'), scope = state('a05-scope');
  assert.equal(saved.destination.kind, 'officer'); assert.deepEqual(saved.draft.fileIds, []);
  assert.equal(scope.destination.scope.kind, 'room'); assert.deepEqual(scope.draft.fileIds, []);
  assert.notEqual(saved.draft.id, scope.draft.id);
  for (const key of ['a05-resume', 'a05-conflict', 'a05-missing']) {
    const s = state(key); assert.equal(s.draft.editable, false); assert.equal(s.draft.canSend, false); assert.deepEqual(s.messages, []);
  }
  assert.ok(!state('a05-missing').draft.notice.includes('Nothing from it was sent'));
});

test('Motet separates upload evidence from activity and hides invalidated answers', () => {
  const doc = state('a08-doc'), compare = state('a08-compare'), gone = state('a08-gone');
  assert.ok(doc.officerAnswer.citations.every(c => c.kind === 'upload' && c.activityId === null));
  assert.deepEqual(compare.officerAnswer.citations.map(c => c.kind), ['upload', 'upload', 'activity', 'activity']);
  assert.deepEqual(compare.officerAnswer.citations[1].location, {lineStart: 10, lineEnd: 17});
  assert.ok(compare.files.every(f => f.usedInAnswer));
  assert.equal(gone.officerAnswer.text, null); assert.equal(gone.officerAnswer.sourceValidation, 'invalid');
  assert.ok(gone.officerAnswer.citations.every(c => c.available === false));
  assert.ok(gone.files.every(f => !f.usedInAnswer && !f.previewable)); assert.equal(gone.filesCount, 1);
  const forward = state('a08-forward');
  assert.equal(forward.officerAnswer.state, 'forwarding_refused'); assert.equal(forward.messages.length, 0);
  assert.equal(forward.draft.text, 'Send this to Ada.'); assert.equal(forward.files[0].availability, 'private');
});

test('previews have source coordinates and honest fallback without fabricated content', () => {
  assert.deepEqual(state('a09-pdf').preview.location, {page: 2});
  const text = state('a09-text').preview;
  assert.equal(text.payload.lines.length, text.lineCount);
  assert.equal(text.payload.lines[9], 'Round each cart line to the cent.');
  assert.equal(state('a09-image').preview.kind, 'image');
  const fallback = state('a09-fallback');
  assert.equal(Object.hasOwn(fallback.preview, 'content'), false); assert.equal(fallback.preview.payload.noticeParts.title, 'This file cannot be shown here');
  assert.equal(fallback.files.find(f => f.id === fallback.preview.fileId).availability, 'available');
});

test('Files history includes pinned, retained finished, deleted and missing; reattach is a new identity', () => {
  const s = state('a10-files');
  assert.equal(s.filesList.entries.length, 5);
  const old = s.filesList.entries.flatMap(e => e.fileIds).map(id => s.files.find(f => f.id === id));
  assert.deepEqual(old.map(f => f.availability), ['available', 'available', 'available', 'available', 'deleted', 'missing']);
  assert.ok(old.every(f => f.retention.expiresAt === null));
  assert.equal(s.filesCount, 6);
  const again = state('a10-reattach');
  assert.ok(!old.some(f => f.id === again.draft.fileIds[0])); assert.equal(again.messages.length, 0);
  assert.equal(again.files[0].availability, 'private');
});

test('delete progress and partial deletion release only removed bytes and never cancel queued messages', () => {
  const confirm = state('a11-confirm'), progress = state('a11-progress'), partial = state('a11-error'), done = state('a11-done');
  assert.ok(confirm.deleteDialog.pinnedWarning.includes('does not cancel'));
  assert.equal(confirm.deleteDialog.removes.length, 3); assert.equal(confirm.deleteDialog.retains.length, 3);
  assert.equal(progress.deleteDialog.releasedBytes, 0); assert.equal(progress.files[0].previewable, false);
  assert.deepEqual(partial.deleteDialog.copies.map(c => c.state), ['deleted', 'identity_changed']);
  assert.equal(partial.deleteDialog.copies[1].reason, 'The copy in Ada’s project folder was changed or moved. It no longer matches the file Motet saved, so Motet left it alone rather than delete something that may not be its own. Remove it yourself if you want it gone.');
  assert.equal(partial.deleteDialog.releasedBytes, partial.files[0].bytes);
  assert.equal(partial.files[0].availability, 'missing');
  assert.equal(done.deleteDialog.releasedBytes, done.files[0].bytes * 2);
  assert.equal(done.files[0].availability, 'deleted'); assert.equal(done.files[0].previewable, false);
  assert.equal(partial.storage.usedBytes - done.storage.usedBytes, done.files[0].bytes);
  for (const s of [confirm, progress, partial, done]) assert.equal(s.messages[0].state, 'queued');
});

test('retained finished table is available, kept in both copies, and ordered oldest recorded first', () => {
  const a = active('a10-files'), s = a.snapshot();
  assert.equal(s.storage.usedBytes, 1_200_000_000);
  const finished = s.management.storage.entries.filter(e => e.messageState === 'answered'
    && e.fileIds.some(id => s.files.find(f => f.id === id).availability === 'available'));
  assert.deepEqual(finished.map(e => e.fileIds), [['fixture-file-table'], ['fixture-file-shot']]);
  const table = s.files.find(f => f.id === 'fixture-file-table');
  assert.equal(table.availability, 'available'); assert.equal(table.retention.expiresAt, null);
  const source = historySource(a, table.id);
  assert.ok(source.actions.includes('open_preview')); assert.ok(source.actions.includes('request_delete'));
  assert.ok(!source.actions.includes('begin_reattach'));
  operation(a, 'request_delete', source);
  assert.deepEqual(deletion(a).copies.map(c => [c.kind, c.state]), [['stored', 'present'], ['workspace', 'present']]);
  operation(a, 'confirm_delete', deletion(a).authority);
  operation(a, 'advance_sample_deletion', deletion(a).authority);
  assert.equal(a.snapshot().storage.usedBytes, s.storage.usedBytes - 2 * table.bytes);
  assert.equal(a.snapshot().files.find(f => f.id === table.id).availability, 'deleted');
  const again = state('a10-reattach'), selected = again.files.find(f => f.id === again.draft.fileIds[0]);
  assert.equal(selected.name, 'site-survey-photos.pdf'); assert.equal(selected.availability, 'private');
  assert.notEqual(selected.id, 'fixture-file-big');
  assert.equal(again.files.find(f => f.id === 'fixture-file-big').availability, 'deleted');
});

test('storage management does not confuse failed/private drafts with old shared files', () => {
  const s = state('a11-full');
  assert.equal(s.storage.full, true); assert.equal(s.ui.storageOpen, true);
  const draftFiles = s.files.filter(f => s.draft.fileIds.includes(f.id));
  assert.ok(draftFiles.every(f => f.availability === 'private'));
  assert.equal(draftFiles.find(f => f.error).error.code, 'storage_full');
  assert.ok(s.messages.every(m => m.destination.agentId === 'ada'));
  assert.ok(s.management.history.entries.every(e => e.destination.agentId === 'ada'));
  const bram = s.management.storage.entries.find(e => e.destination.agentId === 'bram');
  assert.equal(bram.recoveryMessage.state, 'delivery_unknown'); assert.equal(bram.requestId, 'fixture-request-full-bram');
  assert.equal(s.filesCount, 3);
  assert.ok(!s.filesList.entries.flatMap(e => e.fileIds).some(id => s.draft.fileIds.includes(id)));
});

test('exact-file interactions preserve the rest of the draft and fence stale callbacks', () => {
  const a = active('a03-five'), before = a.snapshot();
  const stale = command(a, 'remove_file', {fileId: before.draft.fileIds[0]});
  act(a, 'show_details', {fileId: before.draft.fileIds[3]});
  assert.equal(a.snapshot().ui.detailsFileId, before.draft.fileIds[3]);
  assert.throws(() => a.act(stale), /draft changed/);
  act(a, 'remove_file', {fileId: before.draft.fileIds[0]});
  assert.deepEqual(a.snapshot().draft.fileIds, before.draft.fileIds.slice(1));
  assert.equal(a.snapshot().draft.text, before.draft.text);
  assert.throws(() => act(a, 'remove_file', {fileId: before.draft.fileIds[0]}), /not in/);
  const old = command(a, 'close_details'); a.selectScenario('a03-five');
  assert.throws(() => a.act(old), /draft changed/);
});

test('every action binds exact destination, draft ID, revision, writer and plain schema', () => {
  const a = active('a03-one');
  for (const patch of [{draftId: 'other'}, {revision: 99}, {writerGeneration: 99},
    {destination: {...a.snapshot().destination, agentId: 'bram'}}]) {
    assert.throws(() => a.act({...command(a, 'close_details'), ...patch}), /draft changed/);
  }
  assert.throws(() => a.act({...command(a, 'close_details'), bytes: 'not accepted'}), /field/);
  assert.throws(() => a.act({...command(a, 'edit_text'), text: {toString() { throw Error('not called'); }}}), /2000/);
  const accessor = command(a, 'close_details'); Object.defineProperty(accessor, 'type', {get() {throw Error('Getter ran');}});
  assert.throws(() => a.act(accessor), /field/);
  assert.throws(() => a.act(command(a, 'unknown')), /Unknown/);
  assert.throws(() => active('a05-conflict').act(command(active('a05-conflict'), 'open_picker')), /not editable/);
});

test('sample picker adds distinct fictional identities only and enforces limits atomically', () => {
  const a = active('a01-agent'); act(a, 'open_picker');
  assert.equal(a.snapshot().ui.pickerOpen, true);
  assert.deepEqual(a.snapshot().pickerFiles.map(f => f.id), ['fixture-file-plan', 'fixture-file-shot', 'fixture-file-req']);
  assert.equal(a.snapshot().draft.fileIds.length, 0);
  act(a, 'add_sample_files', {fileIds: ['fixture-file-plan', 'fixture-file-shot', 'fixture-file-req']});
  const s = a.snapshot(); assert.equal(s.draft.fileIds.length, 3); assert.equal(s.ui.pickerOpen, false);
  assert.equal(s.draft.text, 'Review the attached file'); assert.ok(s.draft.suggestedText);
  assert.ok(s.draft.fileIds.every(id => id.includes('-selected-')));
  assert.throws(() => act(a, 'add_sample_files', {fileIds: ['fixture-file-plan', 'fixture-file-plan', 'fixture-file-plan']}), /limits/);
  assert.deepEqual(a.snapshot(), s);
  assert.throws(() => act(a, 'add_sample_files', {fileIds: ['https://example.invalid/file']}), /Unknown sample/);
  assert.throws(() => act(a, 'add_sample_files', {fileIds: [new Uint8Array([1, 2])]}), /Unknown sample/);
  const getters = ['fixture-file-plan']; Object.defineProperty(getters, '0', {get() {throw Error('Getter ran');}});
  assert.throws(() => act(a, 'add_sample_files', {fileIds: getters}), /sample file IDs/);
});

test('cancel affects only uploading file and blocked Send cannot omit another file', () => {
  const a = active('a02-preparing'), initial = a.snapshot();
  assert.throws(() => act(a, 'send_sample'), /Send is off/);
  act(a, 'edit_text', {text: 'Keep this exact draft text.'});
  assert.throws(() => act(a, 'cancel_upload', {fileId: initial.draft.fileIds[1]}), /not uploading/);
  act(a, 'cancel_upload', {fileId: initial.draft.fileIds[0]});
  assert.equal(a.snapshot().draft.fileIds.length, 2); assert.equal(a.snapshot().draft.canSend, false);
  act(a, 'remove_file', {fileId: initial.draft.fileIds[1]});
  assert.equal(a.snapshot().draft.canSend, true);
  const sent = act(a, 'send_sample');
  assert.equal(sent.messages[0].text, 'Keep this exact draft text.');
  assert.deepEqual(sent.messages[0].fileIds, [initial.draft.fileIds[2]]);
  assert.equal(sent.messages[0].state, 'queued'); assert.equal(sent.draft.fileIds.length, 0);
  assert.notEqual(sent.draft.id, initial.draft.id); assert.equal(sent.filesCount, 1);
  assert.throws(() => act(a, 'send_sample'), /Write a message/);
});

test('Motet sample Send queues without inventing an answer or forwarding', () => {
  const a = active('a08-forward');
  const refused = a.snapshot();
  assert.throws(() => act(a, 'send_sample'), /forwarding request was refused/);
  assert.deepEqual(a.snapshot(), refused);
  act(a, 'edit_text', {text: 'My arbitrary edited question.'});
  const s = act(a, 'send_sample');
  assert.equal(s.officerAnswer, null); assert.equal(s.messages[0].destination.kind, 'officer');
  assert.equal(s.messages[0].text, 'My arbitrary edited question.');
  assert.equal(s.messages[0].state, 'queued'); assert.ok(s.files.every(f => !f.usedInAnswer));
});

test('all scenarios and in-memory actions run with effectful browser capabilities forbidden', async () => {
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  source = source.replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  const forbidden = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'localStorage', 'sessionStorage',
    'navigator', 'Notification', 'File', 'Blob', 'document', 'window', 'location', 'setTimeout', 'setInterval'];
  await supervisedVmProbe(source + `\nfor (const key of ATTACHMENT_SCENARIOS) {
    const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario:key});
    a.snapshot(); a.selectScenario(key);
  }
  const a = createAttachmentsFixture({enabled:true,search:'?demo'});
  const d = a.snapshot().draft;
  a.act({type:'edit_text',draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration,text:'Fictional text'});
  `, {forbidden});
});


test('text limits follow each destination, count Unicode code points and reject nulls without mutation', () => {
  for (const [key, limit] of [['a01-agent', 2000], ['a01-officer', 4000]]) {
    const a = active(key);
    assert.equal(a.snapshot().capabilities.maxTextChars, limit);
    act(a, 'edit_text', {text: '🌳'.repeat(limit)});
    const accepted = a.snapshot();
    assert.throws(() => act(a, 'edit_text', {text: '🌳'.repeat(limit + 1)}), /Unicode characters/);
    assert.throws(() => act(a, 'edit_text', {text: 'before\0after'}), /null character/);
    assert.deepEqual(a.snapshot(), accepted);
    const sent = act(a, 'send_sample');
    assert.equal([...sent.messages[0].text].length, limit);
  }
});


test('sample send updates an open Files history with the exact new request and manifest', () => {
  const a = active('a10-files'), before = a.snapshot();
  act(a, 'open_picker');
  act(a, 'add_sample_files', {fileIds: ['fixture-file-shot']});
  act(a, 'edit_text', {text: 'Compare this new fictional screenshot.'});
  const selected = a.snapshot().draft.fileIds.slice();
  const sent = act(a, 'send_sample'), message = sent.messages.at(-1), entry = sent.filesList.entries[0];
  assert.equal(sent.filesList.entries.length, before.filesList.entries.length + 1);
  assert.equal(entry.requestId, message.requestId); assert.equal(entry.text, message.text);
  assert.deepEqual(entry.fileIds, selected); assert.deepEqual(message.fileIds, selected);
  assert.equal(entry.messageState, 'queued'); assert.equal(sent.filesCount, before.filesCount + 1);
});

const messageCommand = (adapter, type, messageIndex = 0, extra = {}) => {
  const m = adapter.snapshot().messages[messageIndex];
  return {type, messageId: m.id, requestId: m.requestId, destination: m.destination,
    revision: m.revision, operationGeneration: m.operationGeneration, ...extra};
};
const messageAct = (adapter, type, messageIndex = 0) => adapter.act(messageCommand(adapter, type, messageIndex));
function rejectsUnchanged(adapter, input, pattern) {
  const before = adapter.snapshot();
  assert.throws(() => adapter.act(input), pattern);
  assert.deepEqual(adapter.snapshot(), before);
}
const messageIdentity = m => ({id: m.id, requestId: m.requestId, destination: m.destination,
  text: m.text, fileIds: m.fileIds, createdAt: m.createdAt});

test('phase 2 block and notice codes derive from canonical state, including after text edits and removal', () => {
  const cases = {
    'a01-agent': 'empty', 'a02-preparing': 'files_preparing', 'a03-one': null,
    'a04-mixed': 'file_errors', 'a04-budget': 'file_errors', 'a04-folder': 'folder_unreachable',
    'a04-recipient': 'recipient_unavailable', 'a05-resume': 'resume_required',
    'a05-conflict': 'draft_conflict', 'a05-missing': 'draft_unavailable',
  };
  for (const [key, code] of Object.entries(cases)) assert.equal(state(key).draft.blockCode, code, key);
  assert.equal(state('a05-saved').draft.noticeCode, 'saved_recipient');
  assert.equal(state('a05-scope').draft.noticeCode, 'saved_scope');
  assert.equal(state('a05-resume').draft.noticeCode, 'resume_available');
  const a = active('a04-folder');
  act(a, 'edit_text', {text: 'This draft text must be preserved exactly.'});
  assert.equal(a.snapshot().draft.blockCode, 'folder_unreachable');
  assert.equal(a.snapshot().draft.sendBlockedReason, 'Send is off until these files can be prepared for Ada. Your message and files are kept.');
  act(a, 'remove_file', {fileId: a.snapshot().draft.fileIds[0]});
  assert.equal(a.snapshot().draft.blockCode, 'folder_unreachable');
  act(a, 'remove_file', {fileId: a.snapshot().draft.fileIds[0]});
  assert.equal(a.snapshot().draft.blockCode, null); assert.equal(a.snapshot().draft.canSend, true);
  assert.equal(a.snapshot().draft.text, 'This draft text must be preserved exactly.');
});

test('preparation retry affects one exact failed file, preserves text and never grants missing Retry', () => {
  const a = active('a04-folder');
  act(a, 'edit_text', {text: 'Keep the plan and requirements together.'});
  const before = a.snapshot(), first = before.draft.fileIds[0], second = before.draft.fileIds[1];
  const stale = command(a, 'retry_preparation', {fileId: first});
  const result = act(a, 'retry_preparation', {fileId: first});
  assert.equal(result.files.find(f => f.id === first).preparation, 'ready');
  assert.equal(result.files.find(f => f.id === first).error, null);
  assert.equal(result.files.find(f => f.id === first).availability, 'private');
  assert.deepEqual(result.files.find(f => f.id === second), before.files.find(f => f.id === second));
  assert.equal(result.draft.text, before.draft.text); assert.deepEqual(result.draft.fileIds, before.draft.fileIds);
  assert.equal(result.draft.revision, before.draft.revision + 1); assert.equal(result.draft.canSend, false);
  assert.equal(result.draft.blockCode, 'folder_unreachable');
  assert.deepEqual(result.lastAction, {type: 'retry_preparation', targetId: first, outcome: 'preparation_ready'});
  rejectsUnchanged(a, stale, /draft changed/);
  rejectsUnchanged(a, command(a, 'retry_preparation', {fileId: first}), /does not offer/);
  act(a, 'retry_preparation', {fileId: second});
  assert.equal(a.snapshot().draft.canSend, true); assert.equal(a.snapshot().draft.blockCode, null);
  for (const key of ['a04-mixed', 'a04-budget']) {
    const b = active(key), failure = b.snapshot().files.find(f => f.error);
    rejectsUnchanged(b, command(b, 'retry_preparation', {fileId: failure.id}), /does not offer/);
  }
});

test('storage-full retry remains failed but advances the bound revision with a structured outcome', () => {
  const a = active('a04-quota'), before = a.snapshot(), file = before.files.find(f => f.error);
  const next = act(a, 'retry_preparation', {fileId: file.id});
  assert.deepEqual(next.files, before.files); assert.deepEqual(next.storage, before.storage);
  assert.equal(next.draft.text, before.draft.text); assert.equal(next.draft.revision, before.draft.revision + 1);
  assert.equal(next.draft.canSend, false); assert.equal(next.draft.blockCode, 'file_errors');
  assert.deepEqual(next.lastAction, {type: 'retry_preparation', targetId: file.id, outcome: 'preparation_blocked'});
  rejectsUnchanged(a, command(a, 'retry_preparation', {fileId: 'foreign-file'}), /not in/);
});

test('resume preserves saved identity, exact text and ordered files, and fences the previous writer', () => {
  for (const key of ['a05-resume', 'a05-conflict']) {
    const a = active(key), before = a.snapshot();
    assert.equal(before.draft.editable, false); assert.equal(before.draft.fileIds.length, 2);
    assert.ok(before.draft.text.length > 0);
    const oldWriter = command(a, 'edit_text', {text: 'stale writer'});
    const next = act(a, 'resume_draft');
    assert.equal(next.draft.id, before.draft.id); assert.equal(next.draft.text, before.draft.text);
    assert.deepEqual(next.draft.fileIds, before.draft.fileIds);
    assert.equal(next.draft.state, 'editing'); assert.equal(next.draft.editable, true);
    assert.equal(next.draft.revision, before.draft.revision + 1);
    assert.notEqual(next.draft.writerGeneration, before.draft.writerGeneration);
    assert.equal(next.draft.noticeCode, null); assert.equal(next.draft.blockCode, null);
    assert.ok(next.files.every(f => f.availability === 'private' && f.actions.includes('remove_from_draft')));
    assert.equal(next.messages.length, 0); assert.equal(next.lastAction.outcome, 'draft_resumed');
    rejectsUnchanged(a, oldWriter, /draft changed/);
    rejectsUnchanged(a, command(a, 'resume_draft'), /does not offer/);
  }
  const missing = active('a05-missing');
  rejectsUnchanged(missing, command(missing, 'resume_draft'), /does not offer/);
});

test('create draft does not transfer or delete earlier private files and never claims earlier delivery', () => {
  for (const key of ['a05-resume', 'a05-conflict', 'a05-missing']) {
    const a = active(key), before = a.snapshot();
    const old = command(a, 'create_draft');
    const next = act(a, 'create_draft');
    assert.notEqual(next.draft.id, before.draft.id);
    assert.notEqual(next.draft.writerGeneration, before.draft.writerGeneration);
    assert.deepEqual(next.draft.destination, before.draft.destination);
    assert.equal(next.draft.text, ''); assert.deepEqual(next.draft.fileIds, []);
    assert.deepEqual(next.files, before.files); assert.deepEqual(next.messages, before.messages);
    assert.equal(next.draft.blockCode, 'empty'); assert.equal(next.draft.noticeCode, null);
    assert.deepEqual(next.lastAction, {type: 'create_draft', targetId: next.draft.id, outcome: 'draft_created'});
    rejectsUnchanged(a, old, /draft changed/);
    if (before.files.length) rejectsUnchanged(a, command(a, 'remove_file', {fileId: before.files[0].id}), /not in/);
  }
});

test('message status check is independently bound, remains unknown and never edits the current draft', () => {
  const a = active('a06-unknown');
  act(a, 'edit_text', {text: 'An unrelated new composer draft.'});
  const before = a.snapshot(), stale = messageCommand(a, 'check_status');
  const next = messageAct(a, 'check_status');
  assert.equal(next.messages[0].state, 'delivery_unknown'); assert.equal(next.messages[0].deliveryPhase, 'unknown');
  assert.equal(next.messages[0].revision, before.messages[0].revision + 1);
  assert.deepEqual(messageIdentity(next.messages[0]), messageIdentity(before.messages[0]));
  assert.deepEqual(next.draft, before.draft); assert.deepEqual(next.files, before.files);
  assert.equal(next.lastAction.outcome, 'still_unknown');
  rejectsUnchanged(a, stale, /message changed/);
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /does not offer/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /does not offer/);
  rejectsUnchanged(a, {...messageCommand(a, 'check_status'), draftId: next.draft.id}, /field/);
});

test('message bindings reject foreign identity, destination, revision, generation and scenario ABA', () => {
  const a = active('a06-partial');
  const baseline = messageCommand(a, 'finish_send');
  for (const patch of [{messageId: 'other'}, {requestId: 'other'}, {revision: 0}, {operationGeneration: 0},
    {destination: {...baseline.destination, agentId: 'bram'}}]) {
    rejectsUnchanged(a, {...baseline, ...patch}, /message changed/);
  }
  rejectsUnchanged(a, command(a, 'finish_send'), /field/);
  a.selectScenario('a06-partial');
  rejectsUnchanged(a, baseline, /message changed/);
  assert.equal(a.snapshot().lastAction, null);
});

test('Finish completes the original manifest on the same request without consuming a new composer draft', () => {
  const a = active('a06-partial');
  act(a, 'add_sample_files', {fileIds: ['fixture-file-shot']});
  act(a, 'edit_text', {text: 'Do not use this new draft for the earlier send.'});
  const before = a.snapshot(), message = before.messages[0];
  const missingBytes = before.files.filter(f => message.fileIds.includes(f.id) && f.availability === 'not_saved').reduce((n, f) => n + f.bytes, 0);
  const next = messageAct(a, 'finish_send');
  assert.equal(next.messages.length, before.messages.length);
  assert.deepEqual(messageIdentity(next.messages[0]), messageIdentity(message));
  assert.equal(next.messages[0].state, 'queued'); assert.equal(next.messages[0].deliveryPhase, 'dispatched');
  assert.deepEqual(next.messages[0].recovery, {canCheck: false, canFinish: false, canDeleteSavedCopies: false});
  assert.deepEqual(next.draft, before.draft);
  assert.deepEqual(next.files.filter(f => before.draft.fileIds.includes(f.id)), before.files.filter(f => before.draft.fileIds.includes(f.id)));
  assert.ok(message.fileIds.every(id => next.files.some(f => f.id === id && f.availability === 'available' && f.availableTo === message.destination.label)));
  assert.equal(next.storage.usedBytes, before.storage.usedBytes + missingBytes);
  assert.equal(next.filesCount, 2); assert.equal(next.lastAction.outcome, 'queued_same_request');
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /does not offer/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /does not offer/);
});

test('saved-copy cleanup fences Finish, releases only workspace bytes and preserves private sources and composer', () => {
  const a = active('a06-partial');
  act(a, 'add_sample_files', {fileIds: ['fixture-file-shot']});
  const before = a.snapshot(), m = before.messages[0];
  const saved = before.files.filter(f => m.fileIds.includes(f.id) && f.availability === 'available');
  const finishBeforeCleanup = messageCommand(a, 'finish_send');
  const next = messageAct(a, 'delete_saved_copies');
  assert.deepEqual(messageIdentity(next.messages[0]), messageIdentity(m));
  assert.equal(next.messages.length, before.messages.length);
  assert.equal(next.messages[0].state, 'not_sent'); assert.equal(next.messages[0].statusText, 'Not sent');
  assert.equal(next.messages[0].deliveryPhase, 'cleanup_fenced');
  assert.deepEqual(next.messages[0].cleanup.fileIds, saved.map(f => f.id));
  const workspaceBytes = saved.reduce((n, f) => n + f.bytes, 0);
  assert.equal(next.messages[0].cleanup.releasedBytes, workspaceBytes);
  assert.equal(next.storage.usedBytes, before.storage.usedBytes - workspaceBytes);
  assert.equal(next.filesCount, before.filesCount);
  assert.ok(next.files.filter(f => m.fileIds.includes(f.id)).every(f => f.availability === 'not_saved' && f.preparation === 'ready'));
  assert.ok(next.files.every(f => f.availability !== 'deleted'));
  assert.deepEqual(next.draft, before.draft); assert.equal(next.deleteDialog, null);
  assert.deepEqual(next.files.find(f => f.id === before.draft.fileIds[0]), before.files.find(f => f.id === before.draft.fileIds[0]));
  assert.equal(next.lastAction.outcome, 'saved_copies_deleted');
  rejectsUnchanged(a, finishBeforeCleanup, /message changed/);
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /does not offer/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /does not offer/);
});

test('recovery cannot run for queued, preparing, answered or mismatched requests', () => {
  for (const key of ['a06-queued', 'a06-preparing', 'a07-answered']) {
    const a = active(key);
    for (const type of ['check_status', 'finish_send', 'delete_saved_copies'])
      rejectsUnchanged(a, messageCommand(a, type), /does not offer/);
  }
  const a = active('a11-full'), before = a.snapshot();
  const bram = before.management.storage.entries.find(e => e.destination.agentId === 'bram').recoveryMessage;
  a.act({type: 'check_status', messageId: bram.id, requestId: bram.requestId, destination: bram.destination, revision: bram.revision, operationGeneration: bram.operationGeneration});
  const next = a.snapshot();
  assert.deepEqual(next.filesList, before.filesList); assert.equal(next.filesCount, before.filesCount);
  assert.deepEqual(next.draft, before.draft); assert.deepEqual(next.messages, before.messages);
  assert.equal(next.management.storage.entries.find(e => e.requestId === bram.requestId).recoveryMessage.state, 'delivery_unknown');
  assert.equal(next.management.actionReceipt, null);
});

test('lastAction is detached, clears on accepted phase 1 actions and preserves on all rejection paths', () => {
  const a = active('a04-folder');
  const result = act(a, 'retry_preparation', {fileId: a.snapshot().draft.fileIds[0]});
  result.lastAction.outcome = 'corrupted';
  assert.equal(a.snapshot().lastAction.outcome, 'preparation_ready');
  rejectsUnchanged(a, command(a, 'retry_preparation', {fileId: 'foreign'}), /not in/);
  act(a, 'edit_text', {text: 'New text.'}); assert.equal(a.snapshot().lastAction, null);
  assert.equal(a.selectScenario('a06-partial').lastAction, null);
});

test('all phase 2 actions execute with browser effects forbidden and disabled adapters reject them', async () => {
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  source = source.replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  const forbidden = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'localStorage', 'sessionStorage',
    'navigator', 'Notification', 'File', 'Blob', 'document', 'window', 'location', 'setTimeout', 'setInterval'];
  await supervisedVmProbe(source + `
  const run = (scenario, type, isMessage = false) => {
    const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario});
    const s = a.snapshot(), d = s.draft, m = s.messages[0];
    const c = isMessage ? {type,messageId:m.id,requestId:m.requestId,destination:m.destination,revision:m.revision,operationGeneration:m.operationGeneration}
      : {type,draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration};
    if (type === 'retry_preparation') c.fileId = d.fileIds[0];
    a.act(c);
  };
  run('a04-folder','retry_preparation'); run('a05-resume','resume_draft'); run('a05-conflict','create_draft');
  run('a06-unknown','check_status',true); run('a06-partial','finish_send',true); run('a06-partial','delete_saved_copies',true);
  `, {forbidden});
  const disabled = createAttachmentsFixture();
  for (const [scenario, type, isMessage] of [['a04-folder', 'retry_preparation', false], ['a05-resume', 'resume_draft', false],
    ['a05-conflict', 'create_draft', false], ['a06-unknown', 'check_status', true], ['a06-partial', 'finish_send', true], ['a06-partial', 'delete_saved_copies', true]]) {
    const a = active(scenario);
    const c = isMessage ? messageCommand(a, type) : command(a, type, type === 'retry_preparation' ? {fileId: a.snapshot().draft.fileIds[0]} : {});
    rejectsUnchanged(disabled, c, /unavailable/);
  }
});


test('canonical full partial seed regains draft readiness only after verified workspace cleanup', () => {
  const a = active('a06-partial-capacity'), before = a.snapshot();
  const original = before.messages[0], originalFiles = original.fileIds.map(id => before.files.find(f => f.id === id));
  assert.equal(original.state, 'partial_not_sent'); assert.equal(original.deliveryPhase, 'pre_dispatch');
  assert.equal(before.storage.usedBytes, before.storage.limitBytes); assert.equal(before.storage.full, true);
  assert.equal(before.draft.canSend, false); assert.equal(before.draft.blockCode, 'storage_capacity');
  assert.equal(before.lastAction, null); assert.equal(before.management.actionReceipt, null);
  assert.deepEqual(before.draft.fileIds, ['fixture-file-shot', 'fixture-file-table']);
  assert.equal(before.draft.text, 'Keep this draft while the earlier file copies are cleared.');
  const selected = before.draft.fileIds.map(id => before.files.find(f => f.id === id));
  const requiredDeliveryBytes = selected.reduce((n, f) => n + f.bytes, 0);
  assert.equal(requiredDeliveryBytes, 668_672);
  assert.ok(before.storage.limitBytes - before.storage.usedBytes < requiredDeliveryBytes);
  rejectsUnchanged(a, command(a, 'send_sample'), /attachment storage/);
  const cleanup = messageCommand(a, 'delete_saved_copies');
  const after = a.act(cleanup), releasedBytes = originalFiles.filter(f => f.availability === 'available').reduce((n, f) => n + f.bytes, 0);
  assert.equal(releasedBytes, 1_887_437);
  assert.equal(after.messages.length, 1); assert.equal(after.messages[0].id, original.id);
  assert.equal(after.messages[0].requestId, original.requestId); assert.equal(after.messages[0].text, original.text);
  assert.deepEqual(after.messages[0].fileIds, original.fileIds);
  assert.equal(after.messages[0].state, 'not_sent'); assert.equal(after.messages[0].deliveryPhase, 'cleanup_fenced');
  assert.equal(after.messages[0].cleanup.releasedBytes, releasedBytes);
  assert.equal(after.storage.usedBytes, before.storage.usedBytes - releasedBytes);
  assert.equal(after.management.storage.backgroundBytes, before.management.storage.backgroundBytes);
  assert.ok(after.storage.limitBytes - after.storage.usedBytes >= requiredDeliveryBytes);
  assert.equal(after.draft.canSend, true); assert.equal(after.draft.blockCode, null);
  assert.deepEqual(after.draft, {...before.draft, canSend: true, blockCode: null, sendBlockedReason: null});
  assert.deepEqual(after.draft.fileIds.map(id => after.files.find(f => f.id === id)), selected);
  assert.deepEqual(after.lastAction, {type: 'delete_saved_copies', targetId: original.id, outcome: 'saved_copies_deleted'});
  rejectsUnchanged(a, cleanup, /message changed/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /does not offer/);
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /does not offer/);
  assert.equal(a.snapshot().storage.usedBytes, after.storage.usedBytes);
});

test('cleaned review scenario is the exact canonical cleanup result, without an owner action receipt', () => {
  const a = active('a06-partial');
  const afterAction = messageAct(a, 'delete_saved_copies');
  const selected = state('a06-cleaned');
  assert.deepEqual(legacyValues(selected), legacyValues({...afterAction, scenario: 'a06-cleaned', lastAction: null}));
  assert.notEqual(selected.management.instanceId, afterAction.management.instanceId);
  assert.equal(selected.messages[0].state, 'not_sent');
  assert.equal(selected.messages[0].deliveryPhase, 'cleanup_fenced');
  assert.equal(selected.messages[0].cleanup.state, 'saved_copies_deleted');
  assert.ok(selected.files.every(f => f.availability === 'not_saved'));
  const old = messageCommand(a, 'finish_send');
  const next = a.selectScenario('a06-cleaned');
  assert.equal(next.lastAction, null);
  assert.notEqual(next.messages[0].operationGeneration, old.operationGeneration);
  rejectsUnchanged(a, old, /message changed/);
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /does not offer/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /does not offer/);
  assert.equal(createAttachmentsFixture({scenario: 'a06-cleaned', search: '?demo'}).snapshot().enabled, false);
});


test('conflict tray permits bound metadata details only, without takeover or access to preview bytes', () => {
  const a = active('a05-conflict'), before = a.snapshot(), first = before.draft.fileIds[0];
  const stale = command(a, 'show_details', {fileId: first});
  const open = act(a, 'show_details', {fileId: first});
  assert.equal(open.ui.detailsFileId, first);
  assert.deepEqual(open.draft, {...before.draft, revision: before.draft.revision + 1});
  assert.deepEqual(open.files, before.files); assert.deepEqual(open.messages, before.messages);
  assert.equal(open.preview, null); assert.ok(open.files.every(f => !f.previewable && f.actions.length === 0));
  assert.equal(open.draft.state, 'conflict'); assert.equal(open.draft.editable, false);
  rejectsUnchanged(a, stale, /draft changed/);
  for (const [type, extra] of [['edit_text', {text: 'do not mutate'}], ['remove_file', {fileId: first}],
    ['retry_preparation', {fileId: first}], ['send_sample', {}], ['open_picker', {}]])
    rejectsUnchanged(a, command(a, type, extra), /not editable/);
  const close = act(a, 'close_details');
  assert.equal(close.ui.detailsFileId, null);
  assert.deepEqual(close.draft, {...before.draft, revision: before.draft.revision + 2});
  assert.deepEqual(close.files, before.files); assert.equal(close.preview, null);
  rejectsUnchanged(a, command(a, 'close_details'), /No selected private/);
  rejectsUnchanged(a, command(a, 'show_details', {fileId: 'foreign-file'}), /not in/);
  rejectsUnchanged(a, {...command(a, 'show_details', {fileId: first}), writerGeneration: 999}, /draft changed/);
});

test('read-only metadata exception does not authorize resume, missing or unsupported trays', () => {
  for (const key of ['a05-resume', 'a05-missing', 'a04-recipient']) {
    const a = active(key), before = a.snapshot();
    for (const [type, extra] of [['show_details', {fileId: before.draft.fileIds[0] || 'fixture-file-plan'}], ['close_details', {}]])
      rejectsUnchanged(a, command(a, type, extra), /not editable|unavailable/);
  }
});

// The approved room pictures reuse desk file metadata while binding a separate Officer scope.
test('room tray scenarios match desk manifests and remain private whole-room Motet analysis', () => {
  for (const [room, desk] of [['a03-room-one', 'a03-one'], ['a03-room-five', 'a03-five']]) {
    const s = state(room), reference = state(desk);
    assert.deepEqual(s.files, reference.files);
    assert.deepEqual(s.draft.fileIds, reference.draft.fileIds);
    assert.equal(s.draft.text, reference.draft.text);
    assert.equal(s.draft.suggestedText, reference.draft.suggestedText);
    assert.equal(s.ui.surface, 'room');
    assert.deepEqual(s.destination, state('a01-room').destination);
    assert.deepEqual(s.draft.destination, s.destination);
    assert.equal(s.destination.kind, 'officer'); assert.equal(s.destination.agentId, null);
    assert.deepEqual(s.destination.scope, {kind: 'room', agentId: null});
    assert.equal(s.capabilities.analysisOnly, true); assert.equal(s.capabilities.maxTextChars, 4000);
    assert.equal(s.draft.canSend, true); assert.equal(s.filesCount, 0);
    assert.deepEqual(s.messages, []); assert.equal(s.officerAnswer, null);
    assert.ok(s.files.every(f => f.availability === 'private' && f.availableTo === null));
  }
  assert.equal(state('a03-room-one').draft.text, 'Review the attached file');
  assert.equal(state('a03-room-one').draft.suggestedText, true);
});

test('room sample selection and Send keep exact authority, text and ordered files with a new empty draft', () => {
  for (const key of ['a01-room', 'a03-room-one', 'a03-room-five']) {
    const a = active(key);
    if (key === 'a01-room') {
      act(a, 'open_picker');
      const selected = act(a, 'add_sample_files', {fileIds: ['fixture-file-req', 'fixture-file-plan']});
      assert.equal(selected.draft.suggestedText, true);
      assert.equal(selected.draft.text, 'Review the attached file');
      assert.deepEqual(selected.draft.fileIds.map(id => selected.files.find(f => f.id === id).name),
        ['requirements.md', 'delivery-plan.pdf']);
    }
    act(a, 'edit_text', {text: 'Analyze these files for the whole room. Do not direct any agent.'});
    const before = a.snapshot(), stale = command(a, 'send_sample');
    for (const destination of [state('a01-agent').destination, state('a01-officer').destination,
      {...before.destination, scope: {kind: 'room', agentId: 'ada'}}])
      rejectsUnchanged(a, {...stale, destination}, /draft changed/);
    for (const extra of [{draftId: 'foreign'}, {revision: 0}, {writerGeneration: 0}])
      rejectsUnchanged(a, {...stale, ...extra}, /draft changed/);
    rejectsUnchanged(a, {...stale, fileIds: before.draft.fileIds}, /command field/);
    const sent = a.act(stale), m = sent.messages[0];
    assert.equal(m.state, 'queued'); assert.equal(m.text, before.draft.text);
    assert.deepEqual(m.fileIds, before.draft.fileIds); assert.deepEqual(m.destination, before.destination);
    assert.equal(m.destination.agentId, null); assert.equal(sent.officerAnswer, null);
    assert.equal(sent.capabilities.analysisOnly, true); assert.equal(sent.ui.surface, 'room');
    assert.equal(sent.messages.length, 1); assert.equal(sent.filesCount, before.draft.fileIds.length);
    assert.ok(sent.files.every(f => f.availability === 'available' && f.availableTo === null && !f.usedInAnswer));
    assert.deepEqual(sent.draft.destination, before.destination);
    assert.notEqual(sent.draft.id, before.draft.id);
    assert.notEqual(sent.draft.writerGeneration, before.draft.writerGeneration);
    assert.deepEqual(sent.draft.fileIds, []); assert.equal(sent.draft.text, '');
    assert.equal(sent.draft.canSend, false);
    rejectsUnchanged(a, stale, /draft changed/);
    rejectsUnchanged(a, command(a, 'send_sample'), /Write a message/);
  }
});

test('room actions reject bad selection and stale scope without changing the private draft', () => {
  const a = active('a03-room-one');
  const add = command(a, 'add_sample_files', {fileIds: ['fixture-file-shot']});
  rejectsUnchanged(a, {...add, destination: state('a01-officer').destination}, /draft changed/);
  rejectsUnchanged(a, {...add, fileIds: ['foreign-file']}, /Unknown sample/);
  rejectsUnchanged(a, {...add, fileIds: ['fixture-file-plan', 'fixture-file-shot', 'fixture-file-req', 'fixture-file-plan', 'fixture-file-shot']}, /limits/);
  const before = a.snapshot();
  const added = a.act(add);
  assert.deepEqual(added.draft.fileIds.slice(0, 1), before.draft.fileIds);
  assert.equal(added.draft.text, before.draft.text);
  assert.deepEqual(added.destination, before.destination);
  rejectsUnchanged(a, add, /draft changed/);
  const stale = command(a, 'send_sample'); a.selectScenario('a03-room-one');
  rejectsUnchanged(a, stale, /draft changed/);
  const full = active('a03-room-five');
  rejectsUnchanged(full, command(full, 'add_sample_files', {fileIds: ['fixture-file-shot']}), /limits/);
});

test('room selection and Send are inert without both demo gates and execute with effects forbidden', async () => {
  for (const scenario of ['a01-room', 'a03-room-one', 'a03-room-five']) {
    const enabled = active(scenario);
    for (const options of [{}, {enabled: true}, {search: '?demo'}, {enabled: true, search: '?demonstration'}]) {
      const a = createAttachmentsFixture({...options, scenario}), before = a.snapshot();
      assert.equal(before.enabled, false); assert.equal(before.draft, null);
      assert.deepEqual(a.selectScenario(scenario), before);
      for (const c of [command(enabled, 'send_sample'), command(enabled, 'add_sample_files', {fileIds: ['fixture-file-plan']})])
        rejectsUnchanged(a, c, /unavailable/);
    }
  }
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  source = source.replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  const forbidden = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'localStorage', 'sessionStorage',
    'navigator', 'Notification', 'File', 'Blob', 'document', 'window', 'location', 'setTimeout', 'setInterval'];
  await supervisedVmProbe(source + `
    for (const scenario of ['a01-room', 'a03-room-one', 'a03-room-five']) {
      const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario});
      const run = (type, extra = {}) => {
        const d = a.snapshot().draft;
        return a.act({type,draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration,...extra});
      };
      if (scenario === 'a01-room') {run('open_picker'); run('add_sample_files',{fileIds:['fixture-file-plan']});}
      run('send_sample');
    }
  `, {forbidden});
});


test('recorded A07 reply binds the exact retained message and never becomes a general answered default', () => {
  const s = state('a07-answered'), m = s.messages[0];
  assert.deepEqual(m.reply, {id: 'fixture-reply-1', messageId: m.id, requestId: m.requestId,
    destination: m.destination, author: {kind: 'agent', agentId: 'ada', label: 'Ada'},
    text: 'The plan asks for a review step that the requirements do not mention. Everything else lines up.', sample: true});
  assert.deepEqual(m.actions, []);
  for (const key of ATTACHMENT_SCENARIOS) {
    if (key !== 'a07-answered') assert.ok(state(key).messages.every(m => !Object.hasOwn(m, 'reply')), key);
  }
});

test('Motet historical attribution and correlation cover shared A09 answers without navigation authority', () => {
  for (const key of ['a08-doc', 'a08-compare', 'a08-gone', 'a09-pdf', 'a09-text']) {
    const s = state(key), answer = s.officerAnswer, m = s.messages[0];
    assert.deepEqual(answer.replyTo, {messageId: m.id, requestId: m.requestId, destination: m.destination}, key);
    assert.deepEqual(answer.model, {id: 'fixture-analysis-model', label: 'Haiku 5.5'}, key);
    assert.equal(answer.sample, true); assert.equal(m.state, 'answered');
    assert.deepEqual(answer.actions, []);
    for (const c of answer.citations) {
      const reason = key === 'a08-gone' ? 'source_unavailable'
        : c.kind === 'activity' ? 'fixture_activity_only' : 'phase4_file_preview';
      if (key !== 'a08-gone' && c.kind === 'upload') {
        assert.equal(c.navigation.kind, 'preview'); assert.equal(c.navigation.authority.kind, 'citation');
        assert.deepEqual(c.navigation.citationTarget.owner.fileIds, m.fileIds);
        assert.equal(c.navigation.citationTarget.owner.requestId, m.requestId);
        assert.deepEqual(c.navigation.citationTarget.location, c.location);
      } else assert.deepEqual(c.navigation, {kind: 'unavailable', reason}, `${key}: ${c.id}`);
      assert.equal(c.available, key !== 'a08-gone');
      if (c.kind === 'activity') assert.match(c.activityId, /^fixture-activity-/);
      assert.equal(Object.hasOwn(c, 'actions'), false);
    }
  }
  const gone = state('a08-gone');
  assert.equal(gone.officerAnswer.text, null);
  assert.equal(gone.officerAnswer.sourceValidation, 'invalid');
  assert.ok(gone.files.every(f => !f.usedInAnswer && !f.previewable));
});

test('refusal has no model or request correlation and preserves its unsent draft until edited', () => {
  const a = active('a08-forward'), before = a.snapshot();
  assert.equal(before.officerAnswer.replyTo, null); assert.equal(before.officerAnswer.model, null);
  assert.equal(before.officerAnswer.sample, true); assert.equal(before.officerAnswer.text, null);
  assert.deepEqual(before.officerAnswer.citations, []); assert.deepEqual(before.officerAnswer.actions, []);
  assert.deepEqual(before.messages, []);
  rejectsUnchanged(a, command(a, 'send_sample'), /refused/);
  const edited = act(a, 'edit_text', {text: 'An arbitrary revised question.'});
  assert.equal(edited.officerAnswer, null);
  assert.deepEqual(edited.files, before.files);
  assert.deepEqual(edited.draft.fileIds, before.draft.fileIds);
  const next = act(a, 'send_sample');
  assert.equal(next.officerAnswer, null); assert.equal(next.messages[0].state, 'queued');
  assert.equal(Object.hasOwn(next.messages[0], 'reply'), false);
});

test('additive reply metadata is detached across snapshots, actions, instances and scenario reselection', () => {
  for (const key of ['a07-answered', 'a08-compare']) {
    const a = active(key), initial = a.snapshot(), other = active(key);
    const damage = s => {
      const reply = s.messages[0].reply || s.officerAnswer.replyTo;
      reply.messageId = 'foreign'; reply.requestId = 'foreign'; reply.destination.scope.agentId = 'foreign';
      if (s.officerAnswer) {s.officerAnswer.model.label = 'foreign'; s.officerAnswer.citations[0].navigation.reason = 'foreign';}
      else s.messages[0].reply.author.label = 'foreign';
    };
    const snapshot = a.snapshot(); damage(snapshot);
    assert.deepEqual(a.snapshot(), initial); assert.deepEqual(legacyValues(other.snapshot()), legacyValues(initial));
    assert.notEqual(other.snapshot().management.instanceId, initial.management.instanceId);
    const result = act(a, 'edit_text', {text: 'An independent composer draft.'}), after = a.snapshot();
    damage(result); assert.deepEqual(a.snapshot(), after);
    const selected = a.selectScenario(key), clean = a.snapshot(); damage(selected);
    assert.deepEqual(a.snapshot(), clean);
    const reply = clean.messages[0].reply || clean.officerAnswer.replyTo;
    assert.equal(reply.messageId, clean.messages[0].id); assert.equal(reply.requestId, clean.messages[0].requestId);
    assert.deepEqual(reply.destination, clean.messages[0].destination);
    assert.equal(clean.draft.text, '');
  }
});

test('arbitrary sample sends preserve prior correlation while adding only unanswered queued messages', () => {
  for (const key of ['a07-answered', 'a08-doc', 'a08-compare', 'a08-gone']) {
    const a = active(key), before = a.snapshot();
    act(a, 'edit_text', {text: 'A new arbitrary sample message without a recorded answer.'});
    const next = act(a, 'send_sample'), queued = next.messages.at(-1);
    assert.deepEqual(next.messages.slice(0, -1), before.messages);
    assert.equal(queued.state, 'queued'); assert.equal(queued.text, 'A new arbitrary sample message without a recorded answer.');
    assert.notEqual(queued.id, before.messages[0].id); assert.notEqual(queued.requestId, before.messages[0].requestId);
    assert.equal(Object.hasOwn(queued, 'reply'), false); assert.equal(next.officerAnswer, null);
    assert.deepEqual(queued.destination, before.draft.destination);
    rejectsUnchanged(a, command(a, 'open_citation', {citationId: 'fixture-citation-plan-p. 2'}), /Unknown|Unsupported/);
  }
});


test('removing the last attachment clears only the untouched suggestion on every surface', () => {
  for (const key of ['a01-agent', 'a01-officer', 'a01-room', 'a03-one', 'a03-room-one']) {
    const a = active(key);
    if (!a.snapshot().draft.fileIds.length) act(a, 'add_sample_files', {fileIds: ['fixture-file-plan']});
    const before = a.snapshot();
    assert.equal(before.draft.text, 'Review the attached file');
    assert.equal(before.draft.suggestedText, true);
    const remove = command(a, 'remove_file', {fileId: before.draft.fileIds[0]});
    const after = a.act(remove);
    assert.equal(after.draft.text, '', key); assert.equal(after.draft.suggestedText, false, key);
    assert.deepEqual(after.draft.fileIds, []); assert.deepEqual(after.files, []);
    assert.equal(after.draft.canSend, false); assert.equal(after.draft.blockCode, 'empty');
    assert.equal(after.draft.revision, before.draft.revision + 1);
    rejectsUnchanged(a, remove, /draft changed/);
    const again = act(a, 'add_sample_files', {fileIds: ['fixture-file-plan']});
    assert.equal(again.draft.text, 'Review the attached file'); assert.equal(again.draft.suggestedText, true);
    assert.notEqual(again.draft.fileIds[0], before.draft.fileIds[0]);
  }
});

test('removing one of several files keeps the suggestion until the final file is removed', () => {
  for (const key of ['a01-agent', 'a01-officer', 'a01-room']) {
    const a = active(key);
    act(a, 'add_sample_files', {fileIds: ['fixture-file-plan', 'fixture-file-shot']});
    const first = act(a, 'remove_file', {fileId: a.snapshot().draft.fileIds[0]});
    assert.equal(first.draft.fileIds.length, 1); assert.equal(first.draft.text, 'Review the attached file');
    assert.equal(first.draft.suggestedText, true); assert.equal(first.draft.canSend, true);
    const last = act(a, 'remove_file', {fileId: first.draft.fileIds[0]});
    assert.equal(last.draft.text, ''); assert.equal(last.draft.suggestedText, false);
    assert.equal(last.draft.canSend, false);
  }
});

test('last-file removal preserves owner edits including text identical to the suggestion', () => {
  for (const key of ['a01-agent', 'a01-officer', 'a01-room']) {
    for (const text of ['My exact question.  ', 'Review the attached file', '', '  ']) {
      const a = active(key);
      act(a, 'add_sample_files', {fileIds: ['fixture-file-plan']});
      act(a, 'edit_text', {text});
      const after = act(a, 'remove_file', {fileId: a.snapshot().draft.fileIds[0]});
      assert.equal(after.draft.text, text, `${key}: ${JSON.stringify(text)}`);
      assert.equal(after.draft.suggestedText, false); assert.deepEqual(after.draft.fileIds, []);
      assert.equal(after.draft.canSend, Boolean(text.trim()));
    }
  }
});


test('together binds only Cleo in Lighthouse throughout message and separate draft', () => {
  const a = active('a07-together'), s = a.snapshot();
  const expected = {kind: 'agent', agentId: 'cleo', label: 'Cleo', projectId: 'fixture-lighthouse',
    projectLabel: 'Lighthouse', scope: {kind: 'agent', agentId: 'cleo'}, modelId: null};
  assert.deepEqual(s.destination, expected); assert.deepEqual(s.draft.destination, expected);
  assert.deepEqual(s.messages[0].destination, expected);
  assert.equal(s.draft.id, 'fixture-draft-agent-agent-cleo');
  assert.equal(s.draft.text, 'One more file for the same question.');
  assert.deepEqual(s.draft.fileIds, ['fixture-file-req']);
  assert.equal(s.messages[0].state, 'queued');
  assert.equal(s.messages[0].text, 'Here is the plan and the screen it describes.');
  assert.deepEqual(s.messages[0].fileIds, ['fixture-file-plan', 'fixture-file-shot']);
  assert.equal(s.messages[0].createdAt, '2030-11-14T15:58:00.000Z');
  assert.ok(s.files.filter(f => s.messages[0].fileIds.includes(f.id)).every(f => f.availableTo === 'Cleo'));
  assert.equal(s.files.find(f => f.id === 'fixture-file-req').availableTo, null);
  assert.deepEqual(Object.keys(s).sort(), Object.keys(state('a06-queued')).sort());
  assert.deepEqual(Object.keys(s.messages[0]).sort(), Object.keys(state('a06-queued').messages[0]).sort());
  const original = command(a, 'send_sample');
  for (const destination of [state('a01-agent').destination,
    {...expected, projectId: 'fixture-orchard', projectLabel: 'Orchard'},
    {...expected, projectId: 'foreign-project'}, {...expected, scope: {kind: 'agent', agentId: 'ada'}},
    {...expected, agentId: 'ada'}, {...expected, label: 'Ada'}]) {
    rejectsUnchanged(a, {...original, destination}, /draft changed/);
  }
  const sent = a.act(original);
  assert.deepEqual(sent.messages[0], s.messages[0]);
  assert.deepEqual(sent.messages[1].destination, expected);
  assert.deepEqual(sent.draft.destination, expected); assert.deepEqual(sent.destination, expected);
  assert.deepEqual(sent.messages[1].fileIds, s.draft.fileIds);
  assert.equal(sent.messages[1].state, 'queued'); assert.equal(Object.hasOwn(sent.messages[1], 'reply'), false);
  assert.equal(sent.files.find(f => f.id === 'fixture-file-req').availableTo, 'Cleo');
});

const NOTICE_PARTS = {
  'a08-gone': {title: 'This answer is no longer shown.',
    body: 'It used delivery-plan.pdf, which you deleted. To get an answer again, attach the file and ask again.'},
  'a08-forward': {title: 'To share this file with Ada, select Ada and attach it there.',
    body: 'Nothing was sent and nothing was passed on. Your draft and its file are still below. With a file attached, Motet can analyse it; it does not act on it.'},
};

test('approved notice parts join as exact plain text without new answer actions or restored sources', () => {
  for (const [key, expected] of Object.entries(NOTICE_PARTS)) {
    const a = active(key), before = a.snapshot(), answer = before.officerAnswer;
    assert.deepEqual(answer.noticeParts, expected);
    assert.equal(answer.notice, `${expected.title} ${expected.body}`);
    assert.equal(typeof answer.noticeParts.title, 'string'); assert.equal(typeof answer.noticeParts.body, 'string');
    assert.deepEqual(answer.actions, []); assert.equal(answer.text, null);
    rejectsUnchanged(a, command(a, 'attach_again'), /Unknown|Unsupported/);
    if (key === 'a08-gone') {
      assert.equal(answer.state, 'source_unavailable'); assert.equal(answer.sourceValidation, 'invalid');
      assert.ok(answer.citations.every(c => !c.available && c.navigation.kind === 'unavailable' && c.navigation.reason === 'source_unavailable'));
      assert.ok(before.files.every(f => !f.previewable && !f.usedInAnswer));
      assert.deepEqual(before.files[0].actions, ['attach_again']);
    } else {
      assert.equal(answer.state, 'forwarding_refused'); assert.equal(answer.replyTo, null); assert.equal(answer.model, null);
      assert.deepEqual(before.messages, []); assert.deepEqual(answer.citations, []);
      rejectsUnchanged(a, command(a, 'send_sample'), /refused/);
      assert.equal(a.snapshot().draft.text, 'Send this to Ada.');
      act(a, 'edit_text', {text: 'A changed question.'}); assert.equal(a.snapshot().officerAnswer, null);
    }
  }
  for (const key of ATTACHMENT_SCENARIOS.filter(k => !Object.hasOwn(NOTICE_PARTS, k) && k !== 'a08-officer-forward'))
    if (state(key).officerAnswer) assert.equal(Object.hasOwn(state(key).officerAnswer, 'noticeParts'), false);
});

test('notice parts remain detached through snapshot, accepted action and reselection results', () => {
  for (const key of Object.keys(NOTICE_PARTS)) {
    const a = active(key), b = active(key), initial = a.snapshot();
    const snapshot = a.snapshot(); snapshot.officerAnswer.noticeParts.title = 'Changed';
    assert.deepEqual(a.snapshot(), initial); assert.deepEqual(legacyValues(b.snapshot()), legacyValues(initial));
    assert.notEqual(b.snapshot().management.instanceId, initial.management.instanceId);
    const result = act(a, 'open_picker'), after = a.snapshot();
    result.officerAnswer.noticeParts.body = 'Changed'; assert.deepEqual(a.snapshot(), after);
    const selected = a.selectScenario(key), clean = a.snapshot();
    selected.officerAnswer.noticeParts.title = 'Changed'; assert.deepEqual(a.snapshot(), clean);
    assert.deepEqual(clean.officerAnswer.noticeParts, NOTICE_PARTS[key]);
  }
});

const operation = (a, type, authority, fields = {}) => a.act({type, authority, ...fields});
const currentSource = (a, fileId) => a.snapshot().management.sourceRefs.find(r => r.fileId === fileId).authority;
const historySource = (a, fileId) => a.snapshot().management.history.entries.flatMap(e => e.fileRefs).find(r => r.fileId === fileId).authority;
const storageSource = (a, fileId) => a.snapshot().management.storage.entries.flatMap(e => e.fileRefs).find(r => r.fileId === fileId).authority;
const fence = a => {const d = a.snapshot().draft; return {draftId: d.id, revision: d.revision, writerGeneration: d.writerGeneration, destination: d.destination};};
const openFiles = a => operation(a, 'open_files', a.snapshot().management.context);
const deletion = a => a.snapshot().management.deletions.at(-1);
const jsonCopy = value => JSON.parse(JSON.stringify(value));

test('phase4 authority schema rejects foreign instances, targets, malformed JSON and scenario ABA without mutation', () => {
  const a = active('a03-one'), b = active('a03-one'), source = currentSource(a, 'fixture-file-plan');
  rejectsUnchanged(b, {type: 'open_preview', authority: source}, /authority/);
  const mutations = [
    x => {x.extra = true;}, x => {x.target.extra = true;}, x => {x.target.owner.destination.projectId = 'foreign';},
    x => {x.target.owner.fileIds = ['fixture-file-req'];}, x => {x.target.sourceVersion++;},
    x => {x.revision = 0;}, x => {x.generation = NaN;}, x => {x.actions.push('request_delete');},
    x => {x.actions = ['open_preview', 'open_preview'];}, x => {x.target.owner.fileIds = new Array(1);},
    x => {Object.defineProperty(x.target, 'hidden', {value: true});},
    x => {x[Symbol('extra')] = true;}, x => {Object.defineProperty(x, 'target', {get() {throw Error('getter ran');}});},
    x => {Object.setPrototypeOf(x.target, {foreign: true});},
  ];
  for (const mutate of mutations) {const bad = jsonCopy(source); mutate(bad); rejectsUnchanged(a, {type: 'open_preview', authority: bad}, /authority|plain|safe|dense/);}
  rejectsUnchanged(a, {type: 'open_preview', authority: source, page: 2}, /field/);
  const stable = a.snapshot(); assert.deepEqual(a.snapshot(), stable);
  a.selectScenario('a03-one'); rejectsUnchanged(a, {type: 'open_preview', authority: source}, /authority/);
  operation(a, 'open_preview', currentSource(a, 'fixture-file-plan'));
  assert.equal(a.snapshot().preview.kind, 'pdf');
});

test('private source authority survives text only, revokes on membership, ownership, takeover and Send', () => {
  for (const key of ['a03-one', 'a03-five']) {
    const a = active(key), source = currentSource(a, 'fixture-file-plan');
    act(a, 'edit_text', {text: 'Only text changed.'});
    operation(a, 'open_preview', source); assert.equal(a.snapshot().preview.provenance.kind, 'draft');
    const preview = a.snapshot().preview.authority;
    act(a, 'remove_file', {fileId: 'fixture-file-plan'});
    assert.equal(a.snapshot().preview, null);
    rejectsUnchanged(a, {type: 'open_preview', authority: source}, /authority/);
    rejectsUnchanged(a, {type: 'preview_page', authority: preview, page: 2}, /authority/);
  }
  const a = active('a03-one'), before = currentSource(a, 'fixture-file-plan');
  act(a, 'add_sample_files', {fileIds: ['fixture-file-req']});
  rejectsUnchanged(a, {type: 'open_preview', authority: before}, /authority/);
  const owned = currentSource(a, 'fixture-file-plan'); act(a, 'send_sample');
  rejectsUnchanged(a, {type: 'open_preview', authority: owned}, /authority/);
  const sent = currentSource(a, 'fixture-file-plan'); assert.equal(sent.target.owner.kind, 'request');
  for (const scenario of ['a05-conflict', 'a05-resume']) {
    const a = active(scenario); assert.deepEqual(a.snapshot().management.sourceRefs, []);
    act(a, 'resume_draft'); assert.equal(a.snapshot().management.sourceRefs.length, 2);
  }
});

test('typed authored previews enforce PDF adjacency, literal lines, fixed drawing, zoom and honest fallback', () => {
  const a = active('a03-one'); operation(a, 'open_preview', currentSource(a, 'fixture-file-plan'));
  assert.equal(a.snapshot().preview.payload.page, 1);
  for (let page = 2; page <= 6; page++) {
    const p = a.snapshot().preview;
    rejectsUnchanged(a, {type: 'preview_page', authority: p.authority, page: page + 1}, /adjacent/);
    operation(a, 'preview_page', p.authority, {page});
    assert.equal(a.snapshot().preview.payload.page, page);
  }
  assert.equal(a.snapshot().preview.navigation.nextPage, null);
  const page2 = state('a09-pdf').preview;
  assert.ok(page2.provenance.label.includes('delivery-plan.pdf · p. 2'));
  assert.equal(page2.payload.page, 2); assert.equal(page2.payload.title, '2. Rollout');
  assert.equal(page2.payload.paragraphs[1].map(r => r.text).join(''), 'Prices are rounded on each cart line, never on the total. A four-item cart must show the same total in the table and at checkout.');
  assert.equal(page2.payload.paragraphs[1][0].highlight, true);
  const text = state('a09-text').preview; assert.equal(text.payload.lines.length, 21); assert.deepEqual(text.location, {lineStart: 10, lineEnd: 17});
  const image = active('a09-image'), initial = image.snapshot().preview;
  assert.deepEqual(initial.location, {image: true}); assert.equal(initial.payload.width, 1440); assert.equal(initial.payload.height, 900);
  assert.deepEqual(initial.payload.items.map(i => i.priceText), ['$16.99', '$16.99', '$16.99', '$16.99']); assert.equal(initial.payload.totalText, '$67.96');
  for (const zoom of [100, 150, 200]) operation(image, 'preview_zoom', image.snapshot().preview.authority, {zoom});
  rejectsUnchanged(image, {type: 'preview_zoom', authority: image.snapshot().preview.authority, zoom: 'fit'}, /adjacent/);
  const fallback = state('a09-fallback').preview;
  assert.equal(fallback.kind, 'fallback'); assert.deepEqual(fallback.authority.actions, ['close_preview']);
  assert.equal(fallback.provenance.kind, 'no_preview');
  assert.deepEqual(Object.keys(fallback.navigation).sort(), ['nextPage', 'nextZoom', 'previousPage', 'previousZoom', 'zoom']);
  assert.ok(Object.values(fallback.navigation).every(v => v === null));
});

test('history and global views bind independent authority, parent return and stale close/reopen callbacks', () => {
  const a = active('a10-files'), before = a.snapshot(), file = historySource(a, 'fixture-file-shot');
  rejectsUnchanged(a, {type: 'close_files', authority: before.management.context}, /authority/);
  rejectsUnchanged(a, {type: 'close_files', authority: before.management.history.authority}, /authority/);
  operation(a, 'open_storage', before.management.views.files.authority);
  assert.deepEqual(a.snapshot().management.views.storage.parent, {kind: 'history', id: before.management.views.files.authority.target.viewId, generation: before.management.views.files.authority.target.viewGeneration});
  operation(a, 'close_storage', a.snapshot().management.views.storage.authority);
  assert.deepEqual(a.snapshot().management.views.files, before.management.views.files);
  operation(a, 'close_files', a.snapshot().management.views.files.authority);
  assert.ok(a.snapshot().management.history.entries.every(e => e.fileRefs.every(r => r.authority === null)));
  rejectsUnchanged(a, {type: 'open_preview', authority: file}, /authority/);
  openFiles(a); rejectsUnchanged(a, {type: 'open_preview', authority: file}, /authority/);
  operation(a, 'open_preview', historySource(a, 'fixture-file-shot'));
  const preview = a.snapshot().preview.authority;
  operation(a, 'close_files', a.snapshot().management.views.files.authority);
  assert.equal(a.snapshot().preview, null); rejectsUnchanged(a, {type: 'close_preview', authority: preview}, /authority/);
  assert.deepEqual(a.snapshot().draft, before.draft);
  const room = active('a01-room'); openFiles(room);
  assert.equal(room.snapshot().management.history.destination.scope.kind, 'room');
});

test('global unknown status uses canonical request and open storage without altering Ada or actionReceipt', () => {
  const a = active('a11-full'), s = a.snapshot(), row = s.management.storage.entries.find(e => e.destination.agentId === 'bram'), m = row.recoveryMessage;
  assert.equal(s.filesCount, 3); assert.ok(s.messages.every(m => m.destination.agentId === 'ada'));
  const check = {type: 'check_status', messageId: m.id, requestId: m.requestId, destination: m.destination, revision: m.revision, operationGeneration: m.operationGeneration};
  const next = a.act(check);
  assert.deepEqual(next.messages, s.messages); assert.deepEqual(next.draft, s.draft);
  assert.equal(next.management.actionReceipt, s.management.actionReceipt);
  const current = next.management.storage.entries.find(e => e.requestId === m.requestId).recoveryMessage;
  assert.equal(current.state, 'delivery_unknown'); assert.equal(current.revision, m.revision + 1);
  rejectsUnchanged(a, check, /changed/);
  operation(a, 'close_storage', next.management.views.storage.authority);
  rejectsUnchanged(a, {...check, revision: current.revision}, /outside/);
});

test('deletion intent causally revokes answer, citations, previews and parallel confirmation before release', () => {
  const a = active('a09-pdf'), initial = a.snapshot(), citation = initial.officerAnswer.citations[0].navigation.authority;
  const preview = initial.preview.authority, direct = currentSource(a, 'fixture-file-plan');
  openFiles(a); const source = historySource(a, 'fixture-file-plan');
  operation(a, 'request_delete', source); const first = deletion(a);
  assert.ok(first.removes.every(line => !line.includes('project folder')));
  operation(a, 'request_delete', source); const second = deletion(a);
  rejectsUnchanged(a, {type: 'confirm_delete', authority: first.authority}, /authority/);
  operation(a, 'confirm_delete', second.authority);
  const pending = a.snapshot();
  assert.equal(pending.storage.usedBytes, initial.storage.usedBytes); assert.equal(pending.preview, null);
  assert.equal(pending.officerAnswer.text, null); assert.equal(pending.officerAnswer.state, 'source_unavailable');
  assert.equal(pending.officerAnswer.sourceValidation, 'invalid');
  assert.ok(pending.officerAnswer.citations.every(c => !c.available && c.navigation.reason === 'source_unavailable'));
  assert.ok(pending.files.every(f => !f.usedInAnswer));
  assert.deepEqual(pending.messages, initial.messages); assert.deepEqual(pending.draft, initial.draft);
  assert.deepEqual(pending.officerAnswer.model, initial.officerAnswer.model); assert.deepEqual(pending.officerAnswer.replyTo, initial.officerAnswer.replyTo);
  for (const [type, authority, fields] of [['open_citation', citation, {}], ['open_preview', direct, {}], ['preview_page', preview, {page: 3}], ['confirm_delete', first.authority, {}]])
    rejectsUnchanged(a, {type, authority, ...fields}, /authority|source changed/);
  const op = pending.management.deletions.find(o => o.id === second.id);
  operation(a, 'advance_sample_deletion', op.authority);
  const done = a.snapshot(); assert.equal(done.filesCount, initial.filesCount);
  assert.equal(done.storage.usedBytes, initial.storage.usedBytes - initial.files.find(f => f.id === 'fixture-file-plan').bytes);
  assert.equal(done.officerAnswer.text, null); assert.equal(done.preview, null);
  rejectsUnchanged(a, {type: 'advance_sample_deletion', authority: op.authority}, /authority/);
});

test('deletion dialog close preserves operation authority, advances once while hidden, and reopens result', () => {
  const a = active('a11-confirm'), before = a.snapshot(), original = deletion(a);
  operation(a, 'confirm_delete', original.authority);
  const pending = deletion(a), view = a.snapshot().deleteDialog.viewAuthority;
  operation(a, 'close_delete', view);
  assert.equal(a.snapshot().deleteDialog, null); assert.deepEqual(deletion(a).authority, pending.authority);
  for (let n = 0; n < 3; n++) assert.equal(a.snapshot().storage.usedBytes, before.storage.usedBytes);
  operation(a, 'advance_sample_deletion', pending.authority);
  const done = deletion(a); assert.equal(done.state, 'deleted'); assert.equal(a.snapshot().deleteDialog, null);
  assert.equal(done.releasedBytes, before.files[0].bytes * 2);
  operation(a, 'open_delete_result', done.authority);
  assert.equal(a.snapshot().deleteDialog.id, original.id);
  assert.equal(a.snapshot().deleteDialog.resultText, 'Local copies deleted');
  assert.equal(a.snapshot().messages[0].state, 'queued'); assert.equal(a.snapshot().filesCount, before.filesCount);
  rejectsUnchanged(a, {type: 'close_delete', authority: view}, /authority/);
  rejectsUnchanged(a, {type: 'advance_sample_deletion', authority: done.authority}, /authority/);
  const cancel = active('a11-confirm'), stable = cancel.snapshot(), op = deletion(cancel);
  operation(cancel, 'keep_file', stable.deleteDialog.viewAuthority);
  assert.equal(deletion(cancel).state, 'abandoned'); assert.deepEqual(deletion(cancel).authority.actions, []);
  assert.equal(cancel.snapshot().storage.usedBytes, stable.storage.usedBytes); assert.deepEqual(cancel.snapshot().files, stable.files);
  rejectsUnchanged(cancel, {type: 'confirm_delete', authority: op.authority}, /authority/);
});

test('seeded deletion failure retains changed-copy quota and results use canonical transitions', () => {
  const failure = state('a11-error'), done = state('a11-done'), progress = state('a11-progress');
  assert.equal(failure.management.actionReceipt, null); assert.equal(done.management.actionReceipt, null);
  const op = failure.management.deletions[0];
  assert.deepEqual(op.copies.map(c => c.state), ['deleted', 'identity_changed']);
  assert.equal(op.copies[1].reason, 'The copy in Ada’s project folder was changed or moved. It no longer matches the file Motet saved, so Motet left it alone rather than delete something that may not be its own. Remove it yourself if you want it gone.');
  assert.equal(op.releasedBytes, op.copies[0].bytes);
  assert.equal(failure.storage.usedBytes, failure.management.storage.backgroundBytes + failure.management.storage.inventoryFiles.reduce((n, f) => n + f.bytes * (f.availableTo ? 2 : 1), 0) + failure.management.deletions[0].copies.filter(c => c.state !== 'deleted').reduce((n, c) => n + c.bytes, 0) + failure.files.find(f => f.id === 'fixture-file-req').bytes * 2);
  assert.equal(failure.files[0].availability, 'missing'); assert.equal(failure.files[0].previewable, false);
  assert.equal(done.files[0].availability, 'deleted'); assert.equal(progress.management.deletions[0].releasedBytes, 0);
});

test('reattachment uses fresh explicit choice, dual fences, exact empty replacement and cancellation', () => {
  const a = active('a10-files'), before = a.snapshot(), tombstone = before.files.find(f => f.id === 'fixture-file-big');
  const source = historySource(a, tombstone.id);
  assert.ok(source.actions.includes('begin_reattach'));
  operation(a, 'begin_reattach', source, {draftFence: fence(a)});
  let picker = a.snapshot().management.reattachPicker;
  assert.deepEqual(picker.sampleChoices.map(c => c.sampleId), ['fixture-file-plan', 'fixture-file-shot', 'fixture-file-req', 'fixture-file-table']);
  assert.deepEqual(a.snapshot().draft, before.draft);
  operation(a, 'cancel_reattach', picker.authority);
  assert.deepEqual(a.snapshot().draft, before.draft); assert.ok(a.snapshot().management.views.files);
  operation(a, 'begin_reattach', source, {draftFence: fence(a)});
  picker = a.snapshot().management.reattachPicker;
  const oldFence = fence(a);
  rejectsUnchanged(a, {type: 'select_reattach_sample', authority: picker.authority, draftFence: {...oldFence, revision: 0}, sampleId: 'fixture-file-table'}, /draft changed/);
  rejectsUnchanged(a, {type: 'select_reattach_sample', authority: picker.authority, draftFence: oldFence, sampleId: tombstone.id + '-old-bytes'}, /sample/);
  const selected = operation(a, 'select_reattach_sample', picker.authority, {draftFence: oldFence, sampleId: 'fixture-file-table'});
  assert.notEqual(selected.draft.id, before.draft.id); assert.notEqual(selected.draft.writerGeneration, before.draft.writerGeneration);
  assert.equal(selected.draft.text, ''); assert.equal(selected.draft.noticeCode, 'reattached_new_draft');
  assert.equal(selected.management.actionReceipt.outcome, 'new_draft'); assert.equal(selected.management.views.files, null);
  assert.equal(selected.management.reattachPicker, null); assert.deepEqual(selected.messages, before.messages);
  assert.deepEqual(selected.files.find(f => f.id === tombstone.id), tombstone);
  assert.notEqual(selected.draft.fileIds[0], tombstone.id); assert.equal(selected.filesCount, before.filesCount);
  assert.equal(selected.files.find(f => f.id === selected.draft.fileIds[0]).availability, 'private');
});

test('reattach nonempty append preserves whitespace/text/identity/prefix and stales changed draft picker', () => {
  for (const text of [' ', 'Keep this exact message.']) {
    const a = active('a10-files'); act(a, 'edit_text', {text}); act(a, 'add_sample_files', {fileIds: ['fixture-file-plan']});
    // Ordinary plus may suggest only when exact text is empty. Whitespace is owner text.
    const original = a.snapshot();
    operation(a, 'begin_reattach', historySource(a, 'fixture-file-big'), {draftFence: fence(a)});
    const old = a.snapshot().management.reattachPicker.authority, oldFence = fence(a);
    act(a, 'edit_text', {text: text + ' updated'});
    rejectsUnchanged(a, {type: 'select_reattach_sample', authority: old, draftFence: oldFence, sampleId: 'fixture-file-shot'}, /authority/);
    const picker = a.snapshot().management.reattachPicker;
    const before = a.snapshot().draft;
    const next = operation(a, 'select_reattach_sample', picker.authority, {draftFence: fence(a), sampleId: 'fixture-file-shot'});
    assert.equal(next.draft.id, original.draft.id); assert.equal(next.draft.writerGeneration, original.draft.writerGeneration);
    assert.equal(next.draft.text, before.text); assert.deepEqual(next.draft.fileIds.slice(0, -1), original.draft.fileIds);
    assert.equal(next.draft.noticeCode, null); assert.equal(next.management.actionReceipt.outcome, 'added_to_draft');
  }
  const a = active('a10-files'); act(a, 'edit_text', {text: ' '});
  const original = a.snapshot().draft;
  operation(a, 'begin_reattach', historySource(a, 'fixture-file-big'), {draftFence: fence(a)});
  const picker = a.snapshot().management.reattachPicker;
  const next = operation(a, 'select_reattach_sample', picker.authority, {draftFence: fence(a), sampleId: 'fixture-file-plan'});
  assert.equal(next.draft.id, original.id); assert.equal(next.draft.text, ' '); assert.equal(next.draft.noticeCode, null);
});

test('count migration is explicit, retained through cleanup/deletion, and reattached send adds one association', () => {
  const incomplete = active('a05-saved'), s = incomplete.snapshot();
  assert.equal(s.filesCount, 1); assert.equal(s.management.history.complete, false); assert.equal(s.management.history.unlistedAssociationCount, 1);
  assert.deepEqual(s.management.history.entries, []); assert.ok(!s.management.context.actions.includes('open_files'));
  rejectsUnchanged(incomplete, {type: 'open_files', authority: s.management.context}, /authority/);
  const again = active('a10-reattach'), before = again.snapshot();
  assert.equal(before.filesCount, 5); assert.equal(before.management.history.entries.length, 5);
  assert.ok(before.management.history.entries.every(e => e.fileIds.length === 1));
  assert.equal(before.management.history.complete, true);
  act(again, 'send_sample'); assert.equal(again.snapshot().filesCount, 6);
  const partial = active('a06-partial'); assert.equal(partial.snapshot().filesCount, 1);
  messageAct(partial, 'delete_saved_copies'); assert.equal(partial.snapshot().filesCount, 1);
});

test('canonical reads are observational and deep detached across every authority, payload and operation result', () => {
  for (const scenario of ['a09-pdf', 'a10-files', 'a11-progress', 'a11-error']) {
    const a = active(scenario), before = a.snapshot();
    for (let n = 0; n < 5; n++) assert.deepEqual(a.snapshot(), before);
    const corrupt = a.snapshot();
    corrupt.management.context.target.destination.projectId = 'foreign';
    corrupt.management.sourceRefs.length = 0;
    if (corrupt.preview) corrupt.preview.payload.paragraphs[0][0].text = 'corrupt';
    if (corrupt.management.deletions[0]) corrupt.management.deletions[0].copies[0].bytes = 0;
    assert.deepEqual(a.snapshot(), before);
  }
  const a = active('a10-files');
  const result = operation(a, 'open_storage', a.snapshot().management.views.files.authority), before = a.snapshot();
  result.management.storage.entries[0].destination.scope.agentId = 'foreign';
  assert.deepEqual(a.snapshot(), before);
});

test('answer removal revokes citation-opened preview while preserving independent historical file authority', () => {
  const a = active('a08-doc'), s = a.snapshot(), citation = s.officerAnswer.citations[0].navigation.authority;
  const source = currentSource(a, 'fixture-file-plan');
  operation(a, 'open_citation', citation);
  act(a, 'edit_text', {text: 'A completely new question.'});
  assert.ok(a.snapshot().preview); // Text-only composer edits do not invalidate the retained answer.
  act(a, 'send_sample');
  assert.equal(a.snapshot().officerAnswer, null); assert.equal(a.snapshot().preview, null);
  assert.ok(a.snapshot().files.every(f => !f.usedInAnswer));
  rejectsUnchanged(a, {type: 'open_citation', authority: citation}, /authority/);
  operation(a, 'open_preview', source); assert.equal(a.snapshot().preview.provenance.kind, 'file');
});

test('superseded confirmation cannot delete another file and progress survives a replacement dialog', () => {
  const a = active('a10-files'), before = a.snapshot();
  operation(a, 'request_delete', historySource(a, 'fixture-file-plan')); const first = deletion(a);
  operation(a, 'request_delete', historySource(a, 'fixture-file-shot')); const second = deletion(a);
  assert.equal(a.snapshot().management.deletions[0].state, 'abandoned');
  rejectsUnchanged(a, {type: 'confirm_delete', authority: first.authority}, /authority/);
  operation(a, 'confirm_delete', second.authority); const progress = deletion(a);
  operation(a, 'request_delete', historySource(a, 'fixture-file-plan'));
  assert.deepEqual(a.snapshot().management.deletions.find(o => o.id === progress.id).authority, progress.authority);
  operation(a, 'advance_sample_deletion', progress.authority);
  assert.equal(a.snapshot().files.find(f => f.id === 'fixture-file-plan').availability, 'available');
  assert.equal(a.snapshot().storage.usedBytes, before.storage.usedBytes - before.files.find(f => f.id === 'fixture-file-shot').bytes * 2);
});

test('readonly reattachment and limited or closed picker selections never change draft or history', () => {
  for (const scenario of ['a05-conflict', 'a05-resume']) {
    const a = active(scenario); openFiles(a);
    assert.ok(a.snapshot().management.history.entries.every(e => e.fileRefs.every(r => !r.authority.actions.includes('begin_reattach'))));
  }
  const a = active('a10-files'); act(a, 'add_sample_files', {fileIds: ['fixture-file-plan', 'fixture-file-plan', 'fixture-file-plan', 'fixture-file-plan', 'fixture-file-plan']});
  operation(a, 'begin_reattach', historySource(a, 'fixture-file-big'), {draftFence: fence(a)});
  const picker = a.snapshot().management.reattachPicker.authority;
  rejectsUnchanged(a, {type: 'select_reattach_sample', authority: picker, draftFence: fence(a), sampleId: 'fixture-file-table'}, /limits/);
  operation(a, 'close_files', a.snapshot().management.views.files.authority);
  assert.equal(a.snapshot().management.reattachPicker, null);
  rejectsUnchanged(a, {type: 'select_reattach_sample', authority: picker, draftFence: fence(a), sampleId: 'fixture-file-table'}, /authority/);
});

test('new phase4 workflow executes with effects forbidden and disabled commands remain inert', async () => {
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  source = source.replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  const forbidden = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'localStorage', 'sessionStorage',
    'navigator', 'Notification', 'File', 'Blob', 'document', 'window', 'location', 'setTimeout', 'setInterval'];
  await supervisedVmProbe(source + `
    const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario:'a09-pdf'});
    const run = (type, authority, extra = {}) => a.act({type,authority,...extra});
    run('open_files',a.snapshot().management.context);
    run('request_delete',a.snapshot().management.history.entries[0].fileRefs[0].authority);
    run('confirm_delete',a.snapshot().management.deletions[0].authority);
    run('close_delete',a.snapshot().deleteDialog.viewAuthority);
    run('advance_sample_deletion',a.snapshot().management.deletions[0].authority);
    run('open_delete_result',a.snapshot().management.deletions[0].authority);
  `, {forbidden});
  const enabled = active('a10-files'), disabled = createAttachmentsFixture();
  assert.equal(disabled.snapshot().management, null);
  rejectsUnchanged(disabled, {type: 'open_files', authority: enabled.snapshot().management.context}, /unavailable/);
});

test('phase2 recovery cannot race committed full-source deletion or release its copies twice', () => {
  const a = active('a06-partial'); openFiles(a);
  operation(a, 'request_delete', historySource(a, 'fixture-file-plan'));
  operation(a, 'confirm_delete', deletion(a).authority);
  const pending = a.snapshot();
  rejectsUnchanged(a, messageCommand(a, 'finish_send'), /recoverable/);
  rejectsUnchanged(a, messageCommand(a, 'delete_saved_copies'), /recoverable/);
  operation(a, 'advance_sample_deletion', deletion(a).authority);
  assert.equal(a.snapshot().storage.usedBytes, pending.storage.usedBytes - pending.files[0].bytes * 2);
  assert.equal(a.snapshot().messages[0].state, 'partial_not_sent');
});

test('D1 reopening a result abandons the superseded confirmation while retaining in-progress operations', () => {
  const a = active('a10-files');
  operation(a, 'request_delete', historySource(a, 'fixture-file-shot'));
  operation(a, 'confirm_delete', deletion(a).authority);
  operation(a, 'advance_sample_deletion', deletion(a).authority);
  const completed = deletion(a);
  operation(a, 'request_delete', historySource(a, 'fixture-file-plan'));
  const hidden = deletion(a), before = a.snapshot();
  operation(a, 'open_delete_result', completed.authority);
  const reopened = a.snapshot();
  assert.equal(reopened.deleteDialog.id, completed.id);
  assert.equal(reopened.management.deletions.find(o => o.id === hidden.id).state, 'abandoned');
  assert.equal(reopened.storage.usedBytes, before.storage.usedBytes); assert.deepEqual(reopened.files, before.files);
  rejectsUnchanged(a, {type: 'confirm_delete', authority: hidden.authority}, /authority/);
  operation(a, 'request_delete', historySource(a, 'fixture-file-plan'));
  operation(a, 'confirm_delete', deletion(a).authority);
  const pending = deletion(a);
  operation(a, 'open_delete_result', completed.authority);
  assert.deepEqual(a.snapshot().management.deletions.find(o => o.id === pending.id).authority, pending.authority);
  operation(a, 'advance_sample_deletion', pending.authority);
  assert.equal(deletion(a).state, 'deleted');
});

test('D2 missing workspace identity stays changed and counted while only the verified stored copy is removed', () => {
  for (const scenario of ['a10-files', 'a10-reattach']) {
    const a = active(scenario); if (!a.snapshot().management.views.files) openFiles(a);
    const before = a.snapshot(), old = before.files.find(f => f.id === 'fixture-file-old');
    assert.equal(old.availability, 'missing');
    operation(a, 'request_delete', historySource(a, old.id));
    const confirm = deletion(a);
    assert.deepEqual(confirm.copies.map(c => c.state), ['present', 'identity_changed']);
    assert.ok(confirm.removes.every(line => !line.includes('project folder')));
    const workspace = confirm.copies[1];
    operation(a, 'confirm_delete', confirm.authority);
    assert.deepEqual(deletion(a).copies.map(c => c.state), ['deleting', 'identity_changed']);
    assert.deepEqual(deletion(a).copies[1], workspace);
    operation(a, 'advance_sample_deletion', deletion(a).authority);
    const done = deletion(a), after = a.snapshot();
    assert.equal(done.state, 'partial_failure'); assert.equal(done.resultText, '1 copy was not removed');
    assert.deepEqual(done.copies.map(c => c.state), ['deleted', 'identity_changed']);
    assert.deepEqual(done.copies[1], workspace); assert.equal(done.releasedBytes, old.bytes);
    assert.equal(after.storage.usedBytes, before.storage.usedBytes - old.bytes);
    assert.equal(after.filesCount, before.filesCount); assert.equal(after.files.find(f => f.id === old.id).availability, 'missing');
    assert.ok(after.management.history.entries.flatMap(e => e.fileRefs).find(r => r.fileId === old.id).authority.actions.includes('begin_reattach'));
  }
});

test('D3 retry cannot allocate a file larger than newly freed capacity', () => {
  const a = active('a11-full');
  operation(a, 'request_delete', storageSource(a, 'fixture-file-shot'));
  operation(a, 'confirm_delete', deletion(a).authority);
  operation(a, 'advance_sample_deletion', deletion(a).authority);
  const before = a.snapshot();
  assert.equal(before.storage.limitBytes - before.storage.usedBytes, 1_310_720);
  const after = act(a, 'retry_preparation', {fileId: 'fixture-file-long'});
  assert.deepEqual(after.files, before.files); assert.deepEqual(after.messages, before.messages);
  assert.deepEqual(after.management.deletions, before.management.deletions);
  assert.deepEqual(after.storage, before.storage); assert.equal(after.lastAction.outcome, 'preparation_blocked');
  assert.equal(after.draft.canSend, false); assert.equal(after.draft.revision, before.draft.revision + 1);
});

test('D3 repeated sends stop before missing workspace allocation would exceed quota', async () => {
  // Near-full starting usage keeps this boundary proof bounded as the default grows.
  // The normal commands still allocate and account every stored/workspace copy.
  await quotaProbe(5_000_000_000 - 7 * 1_887_437, {body: `
    for (let n = 0; n < 3; n++) {
      run('add_sample_files',{fileIds:['fixture-file-plan']});run('send_sample');
      assert.ok(a.snapshot().storage.usedBytes<=a.snapshot().storage.limitBytes);
    }
    run('add_sample_files',{fileIds:['fixture-file-plan']});
    const before=a.snapshot();
    assert.equal(before.messages.length,3);assert.equal(before.draft.canSend,false);
    assert.equal(before.draft.blockCode,'storage_capacity');
    assert.equal(before.draft.sendBlockedReason,'Send is off until attachment storage has room for these files. Open Files to free some.');
    unchanged(()=>run('send_sample'));
    assert.ok(a.snapshot().storage.usedBytes<=a.snapshot().storage.limitBytes);
  `});
});

// Exercise exact-fit and one-byte-short admission using only a test-authored base
// usage seed. The real commands, ledger, parser and transition code are unchanged.
async function quotaProbe(usage, program) {
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  assert.equal(source.split('usedBytes: enabled ? 1_200_000_000 : 0').length, 2);
  source = source.replace('usedBytes: enabled ? 1_200_000_000 : 0', 'usedBytes: enabled ? quotaUsage : 0')
    .replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  await supervisedVmProbe(source + `
    const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario:quotaScenario});
    const run = (type,extra={}) => {const d=a.snapshot().draft;return a.act({type,draftId:d.id,destination:d.destination,revision:d.revision,writerGeneration:d.writerGeneration,...extra});};
    const step = (type,authority,extra={}) => a.act({type,authority,...extra});
    const unchanged = (fn) => {const before=JSON.stringify(a.snapshot());assert.throws(fn);assert.equal(JSON.stringify(a.snapshot()),before);};
    ${program.body}
  `, {assertions: true, globals: {quotaUsage: usage, quotaScenario: program.scenario || 'a01-agent'}});
}

test('D3 exact-fit versus one-byte-short staged selection, retry, reattachment, Send and Finish admission', async () => {
  const limit = 5_000_000_000, plan = 1_887_437, req = 4_096;
  await quotaProbe(limit - plan, {body: `
    run('add_sample_files',{fileIds:['fixture-file-plan']});
    assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);
    assert.equal(a.snapshot().draft.blockCode,'storage_capacity');
    unchanged(()=>run('send_sample'));
  `});
  await quotaProbe(limit - plan + 1, {body: `unchanged(()=>run('add_sample_files',{fileIds:['fixture-file-plan']}));`});
  await quotaProbe(limit - 2 * plan, {body: `
    run('add_sample_files',{fileIds:['fixture-file-plan']}); assert.equal(a.snapshot().draft.canSend,true);
    run('send_sample');assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);
    run('edit_text',{text:'Text alone needs no new copies.'});run('send_sample');
    assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);
  `});
  await quotaProbe(limit - plan, {scenario: 'a01-officer', body: `
    run('add_sample_files',{fileIds:['fixture-file-plan']});assert.equal(a.snapshot().draft.canSend,true);
    run('send_sample');assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);
  `});
  for (const extra of [0, 1]) {
    await quotaProbe(limit - plan + extra, {scenario: 'a04-folder', body: `
      const before=a.snapshot();run('retry_preparation',{fileId:'fixture-file-plan'});
      assert.equal(a.snapshot().lastAction.outcome,${JSON.stringify(extra ? 'preparation_blocked' : 'preparation_ready')});
      assert.equal(a.snapshot().storage.usedBytes,${extra ? 'before.storage.usedBytes' : 'before.storage.limitBytes'});
      assert.ok(a.snapshot().storage.usedBytes<=a.snapshot().storage.limitBytes);
    `});
    await quotaProbe(limit - req + extra, {scenario: 'a06-partial', body: `
      const m=a.snapshot().messages[0],command={type:'finish_send',messageId:m.id,requestId:m.requestId,destination:m.destination,revision:m.revision,operationGeneration:m.operationGeneration};
      ${extra ? 'unchanged(()=>a.act(command));' : "a.act(command);assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);assert.equal(a.snapshot().messages[0].state,'queued');"}
    `});
    await quotaProbe(limit - plan + extra, {scenario: 'a10-files', body: `
      const source=a.snapshot().management.history.entries.flatMap(e=>e.fileRefs).find(r=>r.fileId==='fixture-file-big').authority;
      const fence=()=>{const d=a.snapshot().draft;return{draftId:d.id,revision:d.revision,writerGeneration:d.writerGeneration,destination:d.destination};};
      step('begin_reattach',source,{draftFence:fence()});const picker=a.snapshot().management.reattachPicker.authority;
      ${extra ? "unchanged(()=>step('select_reattach_sample',picker,{draftFence:fence(),sampleId:'fixture-file-plan'}));" : "step('select_reattach_sample',picker,{draftFence:fence(),sampleId:'fixture-file-plan'});assert.equal(a.snapshot().storage.usedBytes,a.snapshot().storage.limitBytes);assert.equal(a.snapshot().draft.blockCode,'storage_capacity');"}
    `});
  }
});

test('D4 pinned warnings preserve queued wording and distinguish uncertain, partial and preparing requests', () => {
  const openings = {
    'a06-queued': 'Ada’s message is still queued.',
    'a06-unknown': 'Ada’s delivery status is unknown.',
    'a06-partial': 'This message was not delivered. Some files were copied into Ada’s project folder.',
    'a06-preparing': 'Copies are still being prepared in Ada’s project folder. The message is not queued yet.',
  };
  for (const [scenario, opening] of Object.entries(openings)) {
    const a = active(scenario); openFiles(a);
    operation(a, 'request_delete', historySource(a, 'fixture-file-plan'));
    assert.equal(deletion(a).pinnedWarning, `${opening} If you delete this file now, Ada may not be able to read it when its turn comes. Deleting the file does not cancel the message.`);
  }
});

// Explicit expiry uses the same sources, copy ledger and deletion operations as manual cleanup.
const openStorage = a => operation(a, 'open_storage', a.snapshot().management.context);
const expiry = a => a.snapshot().management.expiryDialog;
const policy = (a, id) => [...a.snapshot().files, ...a.snapshot().management.storage.inventoryFiles].find(f => f.id === id).retention.expiryPolicy;
const allPolicies = a => [...a.snapshot().files, ...a.snapshot().management.storage.inventoryFiles].map(f => [f.id, f.retention.expiryPolicy]);
const setAll = (a, date = '2030-11-30') => {
  if (!a.snapshot().management.views.storage) openStorage(a);
  operation(a, 'open_expiry_all', a.snapshot().management.views.storage.authority);
  return operation(a, 'set_expiry', expiry(a).authority, {date});
};
const expiryRef = (a, fileId) => {
  const s = a.snapshot().management.storage;
  return [...s.entries, ...s.draftEntries, ...s.inventoryPage.entries].flatMap(e => e.fileRefs).find(r => r.fileId === fileId).authority;
};
const setOne = (a, fileId, date = '2030-11-30') => {
  if (!a.snapshot().management.views.storage) openStorage(a);
  operation(a, 'open_expiry', expiryRef(a, fileId));
  return operation(a, 'set_expiry', expiry(a).authority, {date});
};
const finishCleanup = (a, fileId) => {
  const op = a.snapshot().management.deletions.find(o => o.fileIds.includes(fileId) && o.state === 'deleting');
  return operation(a, 'advance_sample_deletion', op.authority);
};

test('complete global inventory covers every retained byte and pages without altering scoped history', () => {
  for (const scenario of ATTACHMENT_SCENARIOS) {
    const a = active(scenario), before = a.snapshot(); openStorage(a);
    const s = a.snapshot(), inventory = s.management.storage;
    assert.equal(inventory.inventoryComplete, true); assert.equal(inventory.backgroundBytes, 0);
    assert.ok(inventory.inventoryFiles.every(f => f.bytes > 0 && f.bytes <= s.capabilities.maxFileBytes));
    assert.ok(inventory.inventoryFileCount <= 500);
    operation(a, 'open_expiry_all', s.management.views.storage.authority);
    assert.equal(expiry(a).accountedBytes, s.storage.usedBytes, scenario);
    assert.equal(expiry(a).fileCount, inventory.inventoryFileCount, scenario);
    assert.equal(new Set(expiry(a).fileIds).size, expiry(a).fileCount);
    assert.deepEqual(a.snapshot().management.history.entries.map(e => e.requestId), before.management.history.entries.map(e => e.requestId));
    assert.equal(a.snapshot().filesCount, before.filesCount);
  }
  const a = active('a11-storage'), seen = [];
  assert.equal(a.snapshot().management.storage.inventoryFileCount, 62);
  assert.equal(a.snapshot().storage.usedBytes, 1_200_000_000);
  for (;;) {
    const s = a.snapshot(), page = s.management.storage.inventoryPage;
    assert.ok(page.entries.length <= 20); seen.push(...page.entries.map(e => e.requestId));
    if (page.nextOffset === null) break;
    operation(a, 'storage_inventory_page', s.management.views.storage.authority, {offset: page.nextOffset});
  }
  assert.equal(seen.length, 57); assert.equal(new Set(seen).size, 57);
  rejectsUnchanged(a, {type: 'storage_inventory_page', authority: a.snapshot().management.views.storage.authority, offset: 9999}, /advertised/);
  assert.equal(state('a11-full').management.storage.inventoryFileCount, 244);
});

test('scanned PDF is one verified unsupported sample with Remove only and no invented preview', () => {
  const a = active('a04-scanned'), s = a.snapshot(), f = s.files.find(f => f.name === 'signed-delivery-note.pdf');
  assert.equal(f.bytes, 1_153_434); assert.equal(f.error.code, 'scanned_pdf_unreadable');
  assert.equal(f.error.message, 'signed-delivery-note.pdf is a scan with no readable text, so Ada cannot read it. Attach a version with selectable text, or remove it.');
  assert.equal(f.previewable, false); assert.deepEqual(f.actions, ['remove_from_draft']);
  assert.equal(s.management.sourceRefs.some(r => r.fileId === f.id), false);
  assert.equal(s.draft.sendBlockedReason, 'Send is off until this file is fixed or removed. To send the text alone, remove it first.');
  act(a, 'remove_file', {fileId: f.id}); assert.equal(a.snapshot().draft.canSend, true);
});

test('expiry commands validate strict dates, expose nullable policies and never use the host clock', () => {
  const a = active('a11-expiry'), s = a.snapshot(), auth = expiry(a).authority;
  assert.equal(expiry(a).minimumDate, '2030-11-14'); assert.equal(expiry(a).timeZone, 'Etc/UTC');
  assert.ok(allPolicies(a).every(([,p]) => p.date === null && p.deadlineAt === null && p.state === 'none'));
  for (const date of [null, 20301130, '2030-2-03', '2030-02-30', '2030-13-01', '2030-11-13', '2031-01-01', {toString: '2030-11-30'}])
    rejectsUnchanged(a, {type:'set_expiry', authority:auth, date}, /date|today|plain/);
  for (const field of ['timeZone', 'deadlineAt', 'fileIds', 'policyGeneration'])
    rejectsUnchanged(a, {type:'set_expiry', authority:auth, date:'2030-11-30', [field]: 'forged'}, /field/);
  operation(a, 'set_expiry', auth, {date: '2030-11-14'});
  const p = policy(a, 'fixture-file-shot');
  assert.equal(p.generation, 2); assert.equal(p.date, '2030-11-14'); assert.equal(p.deadlineAt, '2030-11-15T00:00:00.000Z');
  assert.equal(p.state, 'scheduled'); assert.equal(p.removedAt, null); assert.equal(p.removalTiming, null);
  assert.equal(a.snapshot().storage.usedBytes, s.storage.usedBytes); assert.deepEqual(a.snapshot().management.deletions, []);
  const stable = a.snapshot(); assert.deepEqual(a.snapshot(), stable);
  rejectsUnchanged(a, {type:'set_expiry', authority:auth, date:'2030-11-30'}, /authority/);
});

test('bulk capture covers canonical current files only and refreshes with zero effects on first changed confirmation', () => {
  const a = active('a11-expiry-all'), old = expiry(a), before = allPolicies(a);
  act(a, 'add_sample_files', {fileIds:['fixture-file-req']});
  const newId = a.snapshot().draft.fileIds[0];
  operation(a, 'set_expiry', old.authority, {date:'2030-11-22'});
  assert.equal(expiry(a).date,'2030-11-22');
  assert.equal(expiry(a).state, 'selection_changed'); assert.equal(expiry(a).fileCount, old.fileCount + 1);
  assert.equal(expiry(a).notice, 'The files stored have changed since you opened this. Check the new total, then set the date again.');
  for (const [id,p] of before) assert.deepEqual(policy(a,id),p);
  assert.ok(allPolicies(a).every(([,p]) => p.date === null));
  rejectsUnchanged(a, {type:'set_expiry', authority:old.authority, date:'2030-11-30'}, /authority/);
  operation(a, 'set_expiry', expiry(a).authority, {date:'2030-11-30'});
  assert.equal(policy(a, newId).date, '2030-11-30');
  act(a, 'add_sample_files', {fileIds:['fixture-file-shot']});
  const later = a.snapshot().draft.fileIds.at(-1); assert.equal(policy(a,later).date,null);
  assert.equal(policy(a,newId).date,'2030-11-30');
});

test('dated unfinished files can individually change or clear before intent, future and overdue', () => {
  for (const scenario of ['a06-queued', 'a06-unknown', 'a06-partial', 'a02-preparing', 'a05-resume']) {
    for (const overdue of [false,true]) {
      const a = active(scenario); openStorage(a);
      const retained = [...a.snapshot().files].filter(f => f.preparation === 'ready' && !f.error && f.availability !== 'deleted');
      const ids = retained.slice(0,2).map(f=>f.id); assert.ok(ids.length,scenario);
      assert.ok(ids.every(id => !expiryRef(a,id).actions.includes('open_expiry')));
      setAll(a,'2030-11-14');
      if (overdue) a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
      const other = ids[1] ? policy(a,ids[1]) : null;
      operation(a,'open_expiry',expiryRef(a,ids[0])); assert.equal(expiry(a).canClear,true);
      operation(a,'clear_expiry',expiry(a).authority);
      assert.equal(policy(a,ids[0]).date,null);
      if(other) assert.deepEqual(policy(a,ids[1]),other);
      assert.ok(!a.snapshot().management.deletions.some(o=>o.fileIds.some(id=>ids.includes(id))));
      if(other) assert.equal(other.state,overdue?'deferred':'scheduled');
    }
  }
});

test('resumable draft remains protected after new draft creation and due expiry never fakes completion', () => {
  const a = active('a05-resume'), old = a.snapshot().draft; setAll(a,'2030-11-14');
  act(a,'create_draft');
  a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
  assert.notEqual(a.snapshot().draft.id,old.id);
  assert.ok(a.snapshot().management.storage.draftEntries.some(d=>d.draftId===old.id));
  for(const id of old.fileIds){assert.equal(policy(a,id).state,'deferred'); assert.ok(expiryRef(a,id).actions.includes('open_expiry'));}
  assert.ok(!a.snapshot().management.deletions.some(o=>o.fileIds.some(id=>old.fileIds.includes(id))));
});

test('clear before deletion intent prevents expiry; clear or change after intent cannot alter it', () => {
  const a = active('a11-storage'); setOne(a,'fixture-file-shot','2030-11-14');
  operation(a,'open_expiry',expiryRef(a,'fixture-file-shot')); operation(a,'clear_expiry',expiry(a).authority);
  a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
  assert.deepEqual(a.snapshot().management.deletions,[]);
  setOne(a,'fixture-file-shot','2030-11-15');
  operation(a,'open_expiry',expiryRef(a,'fixture-file-shot')); const stale=expiry(a).authority;
  a.advanceSample({type:'advance_clock',at:'2030-11-16T00:00:00.000Z'});
  assert.equal(expiry(a).state,'removal_started'); assert.equal(expiry(a).canSet,false); assert.equal(expiry(a).canClear,false);
  assert.deepEqual(expiry(a).authority.actions,['close_expiry']);
  rejectsUnchanged(a,{type:'clear_expiry',authority:stale},/authority/);
  rejectsUnchanged(a,{type:'set_expiry',authority:expiry(a).authority,date:'2030-11-30'},/authority/);
});

test('one committed cleanup blocks the whole bulk action and refresh only rereads after completion', () => {
  const a=active('a11-expiry-removal-started'); operation(a,'close_expiry',expiry(a).authority);
  operation(a,'open_expiry_all',a.snapshot().management.views.storage.authority);
  const d=expiry(a), before=allPolicies(a);
  assert.equal(d.cleanupStartedCount,1); assert.equal(d.canClear,false); assert.equal(d.canSet,false);
  assert.equal(d.notice,'Removal has already started for 1 of these files, so a date cannot be set for all files right now. Check again when it has finished.');
  rejectsUnchanged(a,{type:'clear_expiry',authority:d.authority},/authority/);
  operation(a,'refresh_expiry_selection',d.authority); assert.deepEqual(allPolicies(a),before);
  finishCleanup(a,'fixture-file-table'); const after=allPolicies(a);
  assert.equal(expiry(a).state,'removal_started');
  assert.equal(expiry(a).cleanupStartedCount,1);
  assert.equal(expiry(a).reasonCode,'cleanup_started');
  assert.equal(expiry(a).canSet,false); assert.equal(expiry(a).canClear,false);
  assert.deepEqual(expiry(a).authority.actions,['close_expiry','refresh_expiry_selection']);
  rejectsUnchanged(a,{type:'set_expiry',authority:expiry(a).authority,date:'2030-11-30'},/authority/);
  operation(a,'refresh_expiry_selection',expiry(a).authority);
  assert.equal(expiry(a).state,'editing'); assert.ok(!expiry(a).fileIds.includes('fixture-file-table'));
  assert.deepEqual(allPolicies(a),after);
});

test('completed cleanup never re-enables a captured single-file expiry dialog', () => {
  const a=active('a11-expiry-removal-started'), before=expiry(a);
  finishCleanup(a,'fixture-file-table');
  const d=expiry(a), policies=allPolicies(a);
  assert.equal(d.state,'removal_started'); assert.equal(d.reasonCode,'cleanup_started');
  assert.equal(d.cleanupStartedCount,1); assert.equal(d.canSet,false); assert.equal(d.canClear,false);
  assert.deepEqual(d.authority.actions,['close_expiry']);
  assert.equal(d.fileCount,before.fileCount); assert.equal(d.accountedBytes,before.accountedBytes);
  assert.equal(d.notice,'Removal has already started for this file, so its date can no longer be changed.');
  rejectsUnchanged(a,{type:'clear_expiry',authority:d.authority},/authority/);
  rejectsUnchanged(a,{type:'set_expiry',authority:d.authority,date:'2030-11-30'},/authority/);
  assert.deepEqual(allPolicies(a),policies);
  operation(a,'close_expiry',d.authority); assert.equal(expiry(a),null);
});

test('expiry cleanup releases only verified bytes once; partial and unknown remain fenced and never expired', () => {
  for(const [scenario,outcome] of [['a10-files-expiry','deleted'],['a11-expiry-partial','partial_failure'],['a11-expiry-unconfirmed','unknown']]){
    const a=active(scenario),s=a.snapshot(),op=s.management.deletions[0],f=s.files.find(f=>f.id==='fixture-file-table');
    assert.equal(op.state,outcome); assert.equal(op.cause.kind,'owner_expiry'); assert.equal(op.cause.date,'2030-11-10');
    assert.equal(s.storage.usedBytes,1_200_000_000-op.releasedBytes);
    assert.equal(op.releasedBytes,outcome==='deleted'?f.bytes*2:f.bytes);
    assert.equal(f.availability,outcome==='deleted'?'expired':'missing');
    assert.equal(f.retention.expiryPolicy.removedAt,outcome==='deleted'?'2030-11-11T00:00:00.000Z':null);
    assert.equal(f.retention.expiryPolicy.removalTiming,outcome==='deleted'?'on_date':null);
    assert.ok(!op.authority.actions.includes('advance_sample_deletion'));
    rejectsUnchanged(a,{type:'advance_sample_deletion',authority:op.authority},/authority/);
    a.advanceSample({type:'evaluate_expiry'}); assert.equal(a.snapshot().management.deletions.length,1);
    assert.equal(a.snapshot().storage.usedBytes,s.storage.usedBytes);
  }
});

test('Motet expiry invalidates answers and citations with chosen date distinct from actual removal', () => {
  const a=active('a08-doc'); openStorage(a); const citation=a.snapshot().officerAnswer.citations[0].navigation.authority;
  operation(a,'open_citation',citation); setOne(a,'fixture-file-plan','2030-11-14');
  a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
  assert.equal(a.snapshot().preview,null); assert.equal(a.snapshot().officerAnswer.text,null);
  assert.ok(!a.snapshot().officerAnswer.notice.includes('which you deleted'));
  finishCleanup(a,'fixture-file-plan');
  assert.equal(a.snapshot().officerAnswer.noticeParts.body,'It used delivery-plan.pdf, which expired on 14 Nov, the date you set. To get an answer again, attach the file and ask again.');
  rejectsUnchanged(a,{type:'open_citation',authority:citation},/authority/);
  assert.ok(a.snapshot().officerAnswer.citations.every(c=>!c.available));
  const b=active('a06-queued'); setAll(b,'2030-11-14');
  b.advanceSample({type:'advance_clock',at:'2030-11-16T12:00:00.000Z'});
  const request=b.snapshot().messages[0]; assert.equal(request.state,'queued');
  assert.equal(policy(b,request.fileIds[0]).state,'deferred');
  b.advanceSample({type:'complete_request',requestId:request.requestId});
  finishCleanup(b,request.fileIds[0]); const p=policy(b,request.fileIds[0]);
  assert.equal(p.date,'2030-11-14'); assert.equal(p.removedAt,'2030-11-16T12:00:00.000Z'); assert.equal(p.removalTiming,'delayed');
});

test('expiry authority is instance/view bound and simulation seam is strictly fictional and monotonic', () => {
  const a=active('a11-expiry-all'),b=active('a11-expiry-all'),d=expiry(a);
  rejectsUnchanged(b,{type:'set_expiry',authority:d.authority,date:'2030-11-30'},/authority/);
  operation(a,'close_storage',a.snapshot().management.views.storage.authority); openStorage(a);
  rejectsUnchanged(a,{type:'set_expiry',authority:d.authority,date:'2030-11-30'},/authority/);
  for(const c of [{type:'advance_clock',at:'2029-01-01T00:00:00.000Z'},{type:'advance_clock',at:'2030-02-30T00:00:00.000Z'},
    {type:'advance_clock',at:'2030-11-15T00:00:00.000Z',provider:'real'},{type:'complete_request',requestId:'foreign'}, {type:'send_message'}]){
    const before=a.snapshot();assert.throws(()=>a.advanceSample(c));assert.deepEqual(a.snapshot(),before);
  }
  assert.throws(()=>createAttachmentsFixture().advanceSample({type:'evaluate_expiry'}),/unavailable/);
  const before=a.snapshot();a.advanceSample({type:'evaluate_expiry'});assert.deepEqual(a.snapshot(),before);
});

test('default files and draft associations never expire merely because fictional time advances', () => {
  for(const scenario of ['a07-answered','a08-doc','a05-resume','a10-files']){
    const a=active(scenario),before=a.snapshot();
    a.advanceSample({type:'advance_clock',at:'2030-12-31T23:59:59.000Z'});
    assert.equal(a.snapshot().storage.usedBytes,before.storage.usedBytes);
    assert.deepEqual(a.snapshot().management.deletions,[]);
    assert.ok(allPolicies(a).every(([,p])=>p.date===null));
    assert.deepEqual(a.snapshot().messages,before.messages);
  }
});

test('policy-generation drift and physical-copy changes require a new explicit confirmation', () => {
  const seeded=active('a11-expiry-selection-changed'); assert.equal(expiry(seeded).state,'selection_changed');
  assert.ok(allPolicies(seeded).every(([,p])=>p.date===null));
  assert.equal(seeded.snapshot().management.actionReceipt,null);
  operation(seeded,'set_expiry',expiry(seeded).authority,{date:'2030-11-30'});
  assert.ok(expiry(seeded)===null); assert.ok(allPolicies(seeded).some(([,p])=>p.date==='2030-11-30'));
  const a=active('a03-one'); setAll(a); operation(a,'open_expiry_all',a.snapshot().management.views.storage.authority);
  const d=expiry(a),before=allPolicies(a); act(a,'send_sample');
  operation(a,'clear_expiry',d.authority);
  assert.equal(expiry(a).state,'selection_changed');
  assert.ok(expiry(a).accountedBytes>d.accountedBytes); // Added workspace copy of the same logical file.
  assert.deepEqual(allPolicies(a),before); // No partial clear on changed source/copy generation.
  operation(a,'clear_expiry',expiry(a).authority);
  assert.ok(allPolicies(a).every(([,p])=>p.date===null));
  assert.equal(a.snapshot().messages[0].state,'queued');
});

test('two unfinished dated files can be changed individually without weakening the other protection', () => {
  for(const overdue of [false,true]){
    const a=active('a06-unknown'),ids=a.snapshot().messages[0].fileIds; assert.equal(ids.length,2);
    setAll(a,'2030-11-14'); if(overdue)a.advanceSample({type:'advance_clock',at:'2030-11-15T12:00:00.000Z'});
    const other=policy(a,ids[1]);setOne(a,ids[0],'2030-11-30');
    assert.equal(policy(a,ids[0]).date,'2030-11-30');assert.deepEqual(policy(a,ids[1]),other);
    operation(a,'open_expiry',expiryRef(a,ids[0]));operation(a,'clear_expiry',expiry(a).authority);
    assert.equal(policy(a,ids[0]).date,null);assert.deepEqual(policy(a,ids[1]),other);
    assert.equal(a.snapshot().messages[0].state,'delivery_unknown');
    assert.ok(!a.snapshot().management.deletions.some(o=>o.fileIds.some(id=>ids.includes(id))));
  }
});

test('bulk clear includes protected draft files while later additions keep their independent policy', () => {
  const a=active('a03-one');setAll(a);operation(a,'open_expiry_all',a.snapshot().management.views.storage.authority);
  const d=expiry(a),ids=d.fileIds.slice();operation(a,'clear_expiry',d.authority);
  assert.ok(ids.every(id=>policy(a,id).date===null));
  assert.equal(a.snapshot().draft.fileIds.length,1);assert.equal(a.snapshot().draft.canSend,true);
  rejectsUnchanged(a,{type:'clear_expiry',authority:d.authority},/authority/);
});

test('expiry simulation and page operations cannot perform external effects or read the wall clock', async () => {
  let source=await readFile(new URL('./fixture.mjs',import.meta.url),'utf8');
  source=source.replace('export const ATTACHMENT_SCENARIOS','const ATTACHMENT_SCENARIOS').replace('export function createAttachmentsFixture','function createAttachmentsFixture');
  const sandbox={URLSearchParams,Date:class extends Date{static now(){throw Error('Wall clock forbidden');}}};
  for(const name of ['fetch','XMLHttpRequest','WebSocket','EventSource','localStorage','sessionStorage','navigator','Notification','File','Blob','document','window','location','setTimeout','setInterval'])
    Object.defineProperty(sandbox,name,{get(){throw Error('Forbidden access: '+name);}});
  vm.runInNewContext(source+`
    for(const scenario of ['a11-expiry-all','a04-scanned','a11-expiry-partial','a11-expiry-unconfirmed']){
      const a=createAttachmentsFixture({enabled:true,search:'?demo',scenario});
      if(a.snapshot().management.expiryDialog){
        a.act({type:'set_expiry',authority:a.snapshot().management.expiryDialog.authority,date:'2030-11-14'});
        a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
        for(const op of a.snapshot().management.deletions.filter(o=>o.state==='deleting'))a.act({type:'advance_sample_deletion',authority:op.authority});
      }
      a.snapshot();
    }
  `,sandbox);
});

test('all canonical request references protect a shared logical source until every one finishes', async () => {
  // Author one additional fictional request through the canonical seed builder. This does not expose
  // a page command for inventing provider evidence or create another public scenario.
  let source=await readFile(new URL('./fixture.mjs',import.meta.url),'utf8');
  source=source.replace('export const ATTACHMENT_SCENARIOS','const ATTACHMENT_SCENARIOS').replace('export function createAttachmentsFixture','function createAttachmentsFixture');
  const anchor='    if (state.officerAnswer) state.officerAnswer.revision = 1;';
  assert.equal(source.split(anchor).length,2);
  source=source.replace(anchor,anchor+`
    if (key === 'a06-queued') addRequest(null, {requestId:'fixture-request-second-reference', fileIds:[id('plan')],text:'Fictional shared reference.',createdAt:NOW,messageState:'delivery_unknown'});
  `);
  const context=vm.createContext({URLSearchParams});
  vm.runInContext(source+"; globalThis.testAdapter=createAttachmentsFixture({enabled:true,search:'?demo',scenario:'a06-queued'});",context);
  const invoke=(method,command)=>{context.serialized=JSON.stringify(command);return vm.runInContext(`testAdapter.${method}(JSON.parse(serialized))`,context);};
  const a={snapshot:()=>context.testAdapter.snapshot(),act:c=>invoke('act',c),advanceSample:c=>invoke('advanceSample',c)};
  openStorage(a);setAll(a,'2030-11-14');
  a.advanceSample({type:'advance_clock',at:'2030-11-15T00:00:00.000Z'});
  a.advanceSample({type:'complete_request',requestId:a.snapshot().messages[0].requestId});
  assert.equal(policy(a,'fixture-file-plan').state,'deferred');
  assert.ok(!a.snapshot().management.deletions.some(o=>o.fileIds.includes('fixture-file-plan')));
  assert.ok(a.snapshot().management.deletions.some(o=>o.fileIds.includes(a.snapshot().messages[0].fileIds[1])));
  a.advanceSample({type:'complete_request',requestId:'fixture-request-second-reference'});
  assert.equal(policy(a,'fixture-file-plan').state,'deleting');
});

test('expiry owner-action receipts bind the exact accepted authority and use the existing receipt shape', () => {
  const a=active('a11-expiry');const auth=expiry(a).authority;
  operation(a,'set_expiry',auth,{date:'2030-11-30'});
  assert.deepEqual(a.snapshot().management.actionReceipt,{sequence:1,type:'set_expiry',authorityId:auth.id,outcome:'expiry_set'});
});

test('every File-shaped value includes a detached expiry policy, including initial and interactive picker choices', () => {
  const expected={generation:1,date:null,timeZone:null,deadlineAt:null,state:'none',operationId:null,removedAt:null,removalTiming:null};
  const check=value=>{
    if(!value||typeof value!=='object')return;
    if(typeof value.id==='string'&&typeof value.mediaType==='string'&&typeof value.bytes==='number'){
      assert.ok(value.retention?.expiryPolicy,`missing expiry policy for ${value.id}`);
      assert.deepEqual(Object.keys(value.retention.expiryPolicy).sort(),Object.keys(expected).sort());
      assert.equal(value.retention.expiresAt,value.retention.expiryPolicy.deadlineAt);
    }
    for(const child of Object.values(value))check(child);
  };
  for(const scenario of ATTACHMENT_SCENARIOS)check(state(scenario));
  for(const scenario of ['a01-picker','a01-agent']){
    const a=active(scenario);if(scenario==='a01-agent')act(a,'open_picker');
    const s=a.snapshot();assert.equal(s.pickerFiles.length,3);
    for(const f of s.pickerFiles)assert.deepEqual(f.retention.expiryPolicy,expected);
    s.pickerFiles[0].retention.expiryPolicy.date='2030-11-30';
    assert.deepEqual(s.pickerFiles[1].retention.expiryPolicy,expected);
    assert.deepEqual(a.snapshot().pickerFiles[0].retention.expiryPolicy,expected);
    act(a,'add_sample_files',{fileIds:['fixture-file-plan']});check(a.snapshot());
    setAll(a);act(a,'open_picker');check(a.snapshot());
    assert.ok(a.snapshot().files.some(f=>f.retention.expiryPolicy.date==='2030-11-30'));
    assert.ok(a.snapshot().pickerFiles.every(f=>f.retention.expiryPolicy.date===null));
  }
});

test('native relay samples declare preparation mode and exact approved ready wording', () => {
  const s = state('a03-relay');
  assert.equal(s.draft.canSend, true);
  assert.equal(s.destination.kind, 'agent');
  assert.equal(s.capabilities.sample, true);
  for (const f of s.files.filter(f => f.preparationKind === 'original_relay')) {
    assert.equal(s.capabilities.preparationModes[f.mediaType], 'original_relay');
    assert.equal(f.preparation, 'ready'); assert.equal(f.previewAvailable, false);
    assert.equal(f.preparationText, 'Ready to share as it is. Motet has not opened this file. It stays private until you press Send.');
    assert.equal(f.usedInAnswer, false); assert.equal(f.availableTo, null);
  }
  assert.deepEqual(s.files.filter(f => f.preparationKind === 'original_relay').map(f => f.mediaType), ['application/pdf', 'image/png', 'image/jpeg']);
  assert.equal(s.files.find(f => f.mediaType === 'text/markdown').preparationKind, 'text');
  assert.equal(state('a09-pdf').preview.kind, 'pdf');
  assert.equal(state('a09-image').preview.kind, 'image');
  assert.equal(state('a09-pdf').files[0].preparationKind, 'authored_sample');
});

test('relay PDF and image fallback expose no pages pixels locations or reading claim', () => {
  for (const key of ['a09-relay-pdf', 'a09-relay-image']) {
    const s = state(key), p = s.preview;
    assert.equal(p.kind, 'fallback'); assert.equal(p.location, null);
    assert.equal(p.pageCount, null); assert.equal(p.lineCount, null);
    assert.deepEqual(p.payload.noticeParts, {title:'No preview for this kind of file yet',
      body:'Motet passes it to Ada as it is and does not open it. The file is unchanged and is still available to Ada.'});
    assert.equal(p.payload.footer, 'Nothing was checked or drawn.');
    assert.equal(p.provenance.kind, 'no_preview'); assert.equal(p.provenance.citationTarget, null);
    assert.deepEqual(p.authority.actions, ['close_preview']);
    assert.deepEqual(Object.keys(p.payload).sort(), ['footer','noticeParts','representation','sample']);
    assert.equal(s.files.find(f => f.id === p.fileId).availableTo, 'Ada');
    assert.equal(s.files.find(f => f.id === p.fileId).usedInAnswer, false);
  }
  const a = active('a03-relay');
  operation(a, 'open_preview', currentSource(a, 'fixture-file-plan'));
  assert.equal(a.snapshot().preview.payload.noticeParts.body, 'Ready to share as it is. Motet has not opened this file. It stays private until you press Send.');
  assert.equal(a.snapshot().preview.location, null);
  assert.equal(a.snapshot().messages.length, 0);
});

test('relay metadata survives send finish unknown status and local preparation retry', () => {
  const a = active('a03-relay'); act(a, 'send_sample');
  const s = a.snapshot(); assert.equal(s.messages[0].state, 'queued');
  for (const f of s.files.filter(f => f.preparationKind === 'original_relay')) {
    assert.equal(f.previewAvailable, false); assert.equal(f.availableTo, 'Ada'); assert.equal(f.preparationText, null);
  }
  for (const [key, action] of [['a06-relay-unknown','check_status'], ['a06-relay-partial','finish_send']]) {
    const b = active(key), m = b.snapshot().messages[0];
    b.act({type:action,messageId:m.id,requestId:m.requestId,destination:m.destination,revision:m.revision,operationGeneration:m.operationGeneration});
    for (const f of b.snapshot().files) { assert.equal(f.preparationKind,'original_relay'); assert.equal(f.previewAvailable,false); }
    assert.equal(b.snapshot().messages[0].state, action === 'check_status' ? 'delivery_unknown' : 'queued');
  }
  const retry = active('a04-relay-folder');
  assert.equal(retry.snapshot().files[0].preparationKind, 'original_relay');
  act(retry,'retry_preparation',{fileId:'fixture-file-plan'});
  assert.equal(retry.snapshot().files[0].preparation, 'ready');
  assert.equal(retry.snapshot().files[0].previewAvailable, false);
  operation(retry,'open_preview',currentSource(retry,'fixture-file-plan'));
  assert.equal(retry.snapshot().preview.kind,'fallback');
});

test('new selections in a relay context keep the explicit mode and refuse unqualified Motet files', () => {
  const a = active('a03-relay');
  for (const fileId of a.snapshot().draft.fileIds) act(a, 'remove_file', {fileId});
  act(a,'open_picker');
  assert.equal(a.snapshot().pickerFiles[0].preparationKind,'original_relay');
  act(a,'add_sample_files',{fileIds:['fixture-file-plan','fixture-file-shot']});
  assert.ok(a.snapshot().files.every(f=>f.preparationKind==='original_relay' && !f.previewAvailable));
  const officer = active('a04-relay-officer'), expected='Send is off until these files are fixed or removed. To send the text alone, remove them first.';
  assert.equal(officer.snapshot().draft.canSend,false);
  assert.equal(officer.snapshot().draft.blockCode,'recipient_unavailable');
  assert.equal(officer.snapshot().draft.sendBlockedReason,expected);
  assert.deepEqual(officer.snapshot().capabilities.formats,['text/plain','text/markdown']);
  assert.deepEqual(officer.snapshot().files.map(f=>f.error.message), [
    'delivery-plan.pdf cannot go to Motet yet. Send it to an agent, or remove it to ask Motet in words.',
    'checkout-screenshot.png cannot go to Motet yet. Send it to an agent, or remove it to ask Motet in words.',
  ]);
  for(const f of officer.snapshot().files) {
    assert.equal(f.preparationKind,'original_relay'); assert.equal(f.error.code,'recipient_unavailable');
    assert.equal(f.preparation,'failed'); assert.equal(f.previewAvailable,false);
    assert.deepEqual(Object.keys(f.error).sort(),['actions','code','message']);
    assert.deepEqual(f.error.actions,['remove_from_draft']);
  }
  rejectsUnchanged(officer,command(officer,'send_sample'),/Send is off/);
  rejectsUnchanged(officer,command(officer,'retry_preparation',{fileId:'fixture-file-plan'}),/does not offer/);
  rejectsUnchanged(officer,command(officer,'add_sample_files',{fileIds:['fixture-file-shot']}),/limits/);
  for(const fileId of officer.snapshot().draft.fileIds) act(officer,'remove_file',{fileId});
  assert.equal(officer.snapshot().draft.canSend,true); // Existing text may still be sent without these files.
  assert.equal(officer.snapshot().messages.length,0);
});

test('relay fallback rights revoke on Send deletion and scenario change', () => {
  const a = active('a03-relay'), old = currentSource(a,'fixture-file-plan');
  operation(a,'open_preview',old); act(a,'send_sample');
  assert.equal(a.snapshot().preview,null);
  rejectsUnchanged(a,{type:'open_preview',authority:old},/authority/);
  openFiles(a); const history = historySource(a,'fixture-file-plan');
  operation(a,'open_preview',history); assert.equal(a.snapshot().preview.kind,'fallback');
  operation(a,'request_delete',history);
  operation(a,'confirm_delete',a.snapshot().deleteDialog.authority);
  assert.equal(a.snapshot().preview,null);
  rejectsUnchanged(a,{type:'open_preview',authority:history},/authority/);
  const before = a.snapshot().management.instanceId;
  a.selectScenario('a09-relay-image');
  assert.equal(a.snapshot().management.instanceId,before);
  rejectsUnchanged(a,{type:'open_preview',authority:history},/authority/);
});

test('relay scenarios perform no file decode network or external work', async () => {
  let source=await readFile(new URL('./fixture.mjs',import.meta.url),'utf8');
  source=source.replace('export const ATTACHMENT_SCENARIOS','const ATTACHMENT_SCENARIOS').replace('export function createAttachmentsFixture','function createAttachmentsFixture');
  const sandbox={URLSearchParams};
  for(const name of ['fetch','XMLHttpRequest','WebSocket','File','Blob','Image','createImageBitmap','OffscreenCanvas','Buffer','document','window','navigator','localStorage','setTimeout'])
    Object.defineProperty(sandbox,name,{get(){throw Error('Forbidden access: '+name);}});
  vm.runInNewContext(source+`
    for(const scenario of ATTACHMENT_SCENARIOS.filter(k=>k.includes('relay'))){
      const a=createAttachmentsFixture({enabled:true,search:'?demo',scenario});
      const s=a.snapshot();
      for(const ref of s.management.sourceRefs) if(ref.authority?.actions.includes('open_preview')) {
        a.act({type:'open_preview',authority:ref.authority});
        if(a.snapshot().files.find(f=>f.id===ref.fileId).preparationKind==='original_relay' && a.snapshot().preview.kind!=='fallback') throw Error('decoded');
      }
    }
  `,sandbox);
});

test('reattaching a deleted native PDF or image reapplies its relay profile without restoring old source rights', () => {
  for (const [scenario, fileId] of [['a09-relay-pdf','fixture-file-plan'], ['a09-relay-image','fixture-file-shot']]) {
    const a = active(scenario); openFiles(a);
    const old = historySource(a,fileId);
    operation(a,'request_delete',old); operation(a,'confirm_delete',deletion(a).authority);
    operation(a,'advance_sample_deletion',deletion(a).authority);
    assert.equal(a.snapshot().files.find(f=>f.id===fileId).availability,'deleted');
    rejectsUnchanged(a,{type:'open_preview',authority:old},/authority/);
    operation(a,'begin_reattach',historySource(a,fileId),{draftFence:fence(a)});
    const choices = a.snapshot().management.reattachPicker.sampleChoices;
    assert.deepEqual(choices.map(f=>[f.sampleId,f.bytes]), [
      ['fixture-file-plan',1887437],['fixture-file-shot',655360],['fixture-file-req',4096],['fixture-file-table',13312],
    ]);
    const advertised = choices.find(f=>f.sampleId===fileId), beforeBytes = a.snapshot().storage.usedBytes;
    const picker = a.snapshot().management.reattachPicker.authority;
    const wrongDestination = structuredClone(fence(a)); wrongDestination.destination.projectId='fictional-other-project';
    rejectsUnchanged(a,{type:'select_reattach_sample',authority:picker,draftFence:wrongDestination,sampleId:fileId},/draft changed/);
    operation(a,'select_reattach_sample',picker,{draftFence:fence(a),sampleId:fileId});
    const s = a.snapshot(), newId=s.draft.fileIds[0], selected=s.files.find(f=>f.id===newId);
    assert.notEqual(newId,fileId); assert.equal(selected.preparationKind,'original_relay');
    assert.equal(selected.bytes,advertised.bytes); assert.equal(s.storage.usedBytes-beforeBytes,advertised.bytes);
    assert.equal(selected.previewAvailable,false); assert.equal(selected.availableTo,null); assert.equal(selected.availability,'private');
    assert.equal(s.files.find(f=>f.id===fileId).availability,'deleted');
    operation(a,'open_preview',currentSource(a,newId));
    const preview=a.snapshot().preview;
    assert.equal(preview.kind,'fallback'); assert.equal(preview.location,null);
    assert.equal(preview.pageCount,null); assert.equal(preview.lineCount,null);
    assert.deepEqual(Object.keys(preview.payload).sort(),['footer','noticeParts','representation','sample']);
    assert.deepEqual(preview.authority.actions,['close_preview']);
    assert.equal(preview.provenance.kind,'no_preview'); assert.equal(preview.provenance.citationTarget,null);
    rejectsUnchanged(a,{type:'open_preview',authority:old},/authority/);
    act(a,'send_sample');
    const sentFile=a.snapshot().files.find(f=>f.id===newId);
    assert.equal(sentFile.preparationKind,'original_relay'); assert.equal(sentFile.previewAvailable,false);
    assert.equal(sentFile.availableTo,'Ada'); assert.equal(sentFile.usedInAnswer,false);
  }
});

test('Motet reattachment advertises exactly the current sample bytes and charges them after deletion', () => {
  for (const [sampleId, bytes, mediaType] of [
    ['fixture-file-plan', 128000, 'application/pdf'],
    ['fixture-file-shot', 64000, 'image/png'],
  ]) {
    const a = active('a09-officer-supplied');
    operation(a, 'close_preview', a.snapshot().preview.authority); openFiles(a);
    const old = historySource(a, 'fixture-file-plan');
    operation(a, 'request_delete', old);
    operation(a, 'confirm_delete', deletion(a).authority);
    operation(a, 'advance_sample_deletion', deletion(a).authority);
    operation(a, 'close_delete', a.snapshot().deleteDialog.viewAuthority);
    operation(a, 'begin_reattach', historySource(a, 'fixture-file-plan'), {draftFence: fence(a)});
    const picker = a.snapshot().management.reattachPicker;
    assert.deepEqual(picker.sampleChoices.map(f => [f.sampleId, f.bytes]), [
      ['fixture-file-plan', 128000], ['fixture-file-shot', 64000],
      ['fixture-file-req', 4096], ['fixture-file-table', 13312],
    ]);
    for (const refused of ['fixture-file-req', 'fixture-file-table']) {
      rejectsUnchanged(a, {type: 'select_reattach_sample', authority: picker.authority,
        draftFence: fence(a), sampleId: refused}, /limits/);
    }
    const before = a.snapshot(), advertised = picker.sampleChoices.find(f => f.sampleId === sampleId);
    operation(a, 'select_reattach_sample', picker.authority, {draftFence: fence(a), sampleId});
    const after = a.snapshot(), selected = after.files.find(f => f.id === after.draft.fileIds[0]);
    assert.deepEqual({name: selected.name, mediaType: selected.mediaType, bytes: selected.bytes},
      {name: advertised.name, mediaType: advertised.mediaType, bytes: advertised.bytes});
    assert.equal(selected.bytes, bytes); assert.equal(selected.mediaType, mediaType);
    assert.equal(after.storage.usedBytes - before.storage.usedBytes, advertised.bytes);
    assert.equal(selected.preparationKind, 'officer_direct');
    assert.deepEqual(selected.officerDelivery, {state: 'not_sent', requestId: null});
    assert.equal(selected.availability, 'private'); assert.equal(selected.usedInAnswer, false);
    assert.notEqual(selected.id, sampleId);
    assert.equal(after.files.find(f => f.id === 'fixture-file-plan').availability, 'deleted');
    assert.equal(after.messages.length, before.messages.length);
    rejectsUnchanged(a, {type: 'open_preview', authority: old}, /authority/);
  }
});

test('Motet reattachment refuses original-file formats and accepts only its explicit current text profile', async () => {
  // Preserve a hypothetical historical Officer association, then narrow only its current capability.
  // This is an internal fictional seed; no runtime or provider support is inferred from it.
  let source=await readFile(new URL('./fixture.mjs',import.meta.url),'utf8');
  source=source.replace('export const ATTACHMENT_SCENARIOS','const ATTACHMENT_SCENARIOS').replace('export function createAttachmentsFixture','function createAttachmentsFixture');
  const anchor='    if (state.officerAnswer) state.officerAnswer.revision = 1;';
  assert.equal(source.split(anchor).length,2);
  source=source.replace(anchor,anchor+"\n    if(key==='a08-doc') configureRelay(state);");
  vm.runInNewContext(source+`
    const a=createAttachmentsFixture({enabled:true,search:'?demo',scenario:'a08-doc'});
    const run=(type,authority,extra={})=>a.act({type,authority,...extra});
    const sourceRef=()=>a.snapshot().management.history.entries.flatMap(e=>e.fileRefs).find(r=>r.fileId==='fixture-file-plan').authority;
    const fence=()=>{const d=a.snapshot().draft;return {draftId:d.id,revision:d.revision,writerGeneration:d.writerGeneration,destination:d.destination};};
    run('open_files',a.snapshot().management.context);
    run('request_delete',sourceRef());
    run('confirm_delete',a.snapshot().management.deletions[0].authority);
    run('advance_sample_deletion',a.snapshot().management.deletions[0].authority);
    run('begin_reattach',sourceRef(),{draftFence:fence()});
    const choices=a.snapshot().management.reattachPicker.sampleChoices;
    assert.deepEqual(choices.map(f=>[f.sampleId,f.bytes]),[
      ['fixture-file-plan',1887437],['fixture-file-shot',655360],['fixture-file-req',4096],['fixture-file-table',13312],
    ]);
    const picker=a.snapshot().management.reattachPicker.authority;
    for(const sampleId of ['fixture-file-plan','fixture-file-shot']) {
      const before=JSON.stringify(a.snapshot());
      assert.throws(()=>run('select_reattach_sample',picker,{draftFence:fence(),sampleId}),/limits/);
      assert.equal(JSON.stringify(a.snapshot()),before);
    }
    const beforeBytes=a.snapshot().storage.usedBytes;
    run('select_reattach_sample',picker,{draftFence:fence(),sampleId:'fixture-file-req'});
    const selected=a.snapshot().files.find(f=>f.id===a.snapshot().draft.fileIds[0]);
    assert.equal(selected.preparationKind,'text');
    assert.equal(selected.bytes,choices.find(f=>f.sampleId==='fixture-file-req').bytes);
    assert.equal(a.snapshot().storage.usedBytes-beforeBytes,selected.bytes);
    assert.equal(selected.mediaType,'text/markdown');
    assert.equal(selected.availableTo,null);
    assert.equal(a.snapshot().messages.length,1);
  `,{URLSearchParams,assert});
});

test('Motet direct fallback reports exact delivery and citation truth without drawing', () => {
  const cases = [
    ['private','unsent','not_sent','Ready to share as it is. Motet has not opened this file. It stays private until you press Send.'],
    ['supplied','supplied','confirmed','This file was supplied to the model as it is. Motet does not open this kind of file.'],
    ['unknown','unknown','unknown','Motet could not confirm that the model received this file. Motet does not open this kind of file.'],
    ['retained','retained_local','retained_local','This file is kept in this Motet conversation. Motet was not asked, so nothing was sent to its model. Motet has not opened this file.'],
    ['page','cited_page','confirmed','Motet cited page 2 of this file. Motet does not open this kind of file, so it cannot show the page.'],
    ['image','cited_whole','confirmed','Motet used this image as a whole. Motet does not open this kind of file.'],
  ];
  for (const [key, previewState, delivery, body] of cases) {
    const a=active('a09-officer-'+key), s=a.snapshot(), p=s.preview, f=s.files.find(f=>f.id===p.fileId);
    assert.equal(p.officerState,previewState); assert.equal(p.kind,'fallback');
    assert.equal(p.payload.noticeParts.title,'No preview for this kind of file yet');
    assert.equal(p.payload.noticeParts.body,body); assert.equal(p.payload.footer,'Nothing was checked or drawn.');
    assert.equal(p.location,null); assert.equal(p.pageCount,null); assert.equal(p.lineCount,null);
    assert.deepEqual(p.authority.actions,['close_preview']); assert.equal(f.previewAvailable,false);
    assert.equal(f.officerDelivery.state,delivery); assert.equal(f.preparationKind,'officer_direct');
    assert.equal(f.officerDelivery.requestId,delivery==='not_sent'?null:s.messages[0].requestId);
    assert.equal(Object.hasOwn(p.payload,'drawing'),false); assert.equal(Object.hasOwn(p.payload,'paragraphs'),false);
    rejectsUnchanged(a,{type:'preview_page',authority:p.authority,page:3},/authority/);
    rejectsUnchanged(a,{type:'preview_zoom',authority:p.authority,zoom:100},/authority/);
  }
});

test('Motet direct unsupported media/model preserve whole selection and explicit failure cause',()=>{
  for(const kind of ['media','model']) {
    const a=active('a04-officer-'+kind), s=a.snapshot(), refused=s.files.find(f=>f.officerRefusal);
    assert.equal(s.draft.canSend,false); assert.equal(refused.officerRefusal.reason,kind);
    assert.equal(refused.error.code,'officer_'+kind+'_unavailable');
    assert.equal(refused.officerDelivery.state,'not_sent'); assert.equal(s.draft.text,'Review the attached file');
    assert.deepEqual(s.messages,[]); rejectsUnchanged(a,command(a,'send_sample'),/fixed|removed/);
    if(kind==='media') {assert.equal(s.draft.fileIds.length,2); assert.equal(s.files[0].preparation,'ready');}
    else {assert.deepEqual(s.capabilities.formats,[]);assert.equal(refused.officerRefusal.supportedModelLabel,'Haiku 5.5');}
  }
  const s=state('a06-officer-rejected');
  assert.equal(s.messages[0].state,'failed');assert.equal(s.messages[0].errorCode,'provider_rejected');
  assert.equal(s.files[0].officerDelivery.state,'unknown');assert.equal(s.officerAnswer,null);
  assert.equal(s.preview.officerState,'unknown');assert.deepEqual(s.messages[0].actions,[]);
});

test('Motet direct private Send cannot manufacture a confirmed input or an answer',()=>{
  const a=active('a09-officer-private'); act(a,'send_sample'); const s=a.snapshot();
  assert.equal(s.officerAnswer,null); assert.equal(s.messages.length,1);
  assert.equal(s.files[0].officerDelivery.state,'unknown');
  assert.equal(s.files[0].officerDelivery.requestId,s.messages[0].requestId);
  assert.equal(s.files[0].usedInAnswer,false);assert.equal(s.files[0].availableTo,null);
  const source=s.management.sourceRefs.find(r=>r.fileId===s.files[0].id);
  const opened=a.act({type:'open_preview',authority:source.authority});
  assert.equal(opened.preview.officerState,'unknown');assert.equal(opened.preview.location,null);
});

test('uncited consumed Motet image invalidates complete answer at deletion confirmation',()=>{
  const a=active('a08-officer-uncited'), s=a.snapshot(), image=s.files.find(f=>f.mediaType==='image/png');
  assert.equal(image.officerDelivery.state,'confirmed');assert.equal(image.usedInAnswer,false);
  assert.ok(s.officerAnswer.citations.every(c=>c.fileId!==image.id));
  a.act({type:'open_files',authority:s.management.context});
  const rows=a.snapshot().management.history.entries;
  const source=rows.flatMap(r=>r.fileRefs).find(f=>f.fileId===image.id);
  assert.ok(source,'file is retained for this conversation');
  a.act({type:'request_delete',authority:source.authority});
  a.act({type:'confirm_delete',authority:a.snapshot().deleteDialog.authority});
  const after=a.snapshot();assert.equal(after.officerAnswer.state,'source_unavailable');
  assert.equal(after.officerAnswer.text,null);assert.ok(after.officerAnswer.citations.every(c=>!c.available));
  assert.ok(after.files.every(f=>!f.usedInAnswer)); assert.equal(after.storage.usedBytes,s.storage.usedBytes);
});

test('retained Motet files and forwarding remain analysis-only and conversations do not gain workspace copies',()=>{
  const s=state('a08-officer-followup');assert.equal(s.messages[0].createdAt,'2030-10-01T12:00:00.000Z');
  assert.equal(s.files.length,2);assert.ok(s.files.every(f=>f.retention.expiresAt===null&&f.availableTo===null));
  assert.deepEqual(s.officerAnswer.actions,[]);assert.equal(s.officerAnswer.analysisOnly,true);
  const a=active('a08-officer-forward');rejectsUnchanged(a,command(a,'send_sample'),/refused/);
  assert.equal(a.snapshot().draft.text,'Send this to Ada.');assert.equal(a.snapshot().files[0].officerDelivery.state,'not_sent');
});

test('Motet direct citations and stale model authorities cannot acquire previews or new sends',()=>{
  const a=active('a09-officer-page'), first=a.snapshot(), old=first.preview.authority;
  assert.equal(first.preview.provenance.citationTarget.location.page,2);
  assert.equal(first.officerAnswer.citations[0].navigation.citationTarget.owner.requestId,first.messages[0].requestId);
  a.selectScenario('a04-officer-model');
  rejectsUnchanged(a,{type:'close_preview',authority:old},/authority/);
  rejectsUnchanged(a,command(a,'send_sample'),/fixed|removed/);
  const b=active('a09-officer-unknown'), m=b.snapshot().messages[0];
  rejectsUnchanged(b,{type:'finish_send',messageId:m.id,requestId:m.requestId,destination:m.destination,
    revision:m.revision,operationGeneration:m.operationGeneration},/offer|unavailable|action/);
  assert.equal(b.snapshot().files[0].officerDelivery.state,'unknown');
});

test('no-call retention and explicit provider rejection are finished without becoming answered',()=>{
  for(const key of ['a09-officer-retained','a06-officer-retained','a06-officer-rejected']) {
    const a=active(key), before=a.snapshot(), m=before.messages[0];
    openStorage(a); const source=storageSource(a,before.files[0].id);
    assert.ok(source.actions.includes('open_expiry'),key);
    operation(a,'open_expiry',source);
    assert.equal(a.snapshot().management.expiryDialog.mode,'single');
    assert.throws(()=>a.advanceSample({type:'complete_request',requestId:m.requestId}),/unfinished/);
    assert.equal(a.snapshot().messages[0].state,m.state);assert.equal(a.snapshot().officerAnswer,null);
  }
  const a=active('a09-officer-unknown'), s=a.snapshot();openStorage(a);
  assert.equal(storageSource(a,s.files[0].id).actions.includes('open_expiry'),false);
  assert.equal(a.snapshot().files[0].officerDelivery.state,'unknown');
});


test('Motet retained and rejected messages expose exact request-scoped design facts', () => {
  for (const key of ['a06-officer-retained', 'a09-officer-retained']) {
    const s = state(key), m = s.messages[0], f = s.files[0];
    assert.equal(m.state, 'retained_local'); assert.equal(m.deliveryPhase, 'pre_dispatch');
    assert.equal(m.statusText, 'Kept in this conversation · Motet was not asked');
    assert.deepEqual(f.officerDelivery, {state: 'retained_local', requestId: m.requestId});
    assert.equal(s.messages.length, 1); assert.deepEqual(m.actions, []);
    assert.equal(m.failureParts, undefined); assert.equal(s.officerAnswer, null);
    if (key === 'a06-officer-retained') assert.equal(s.preview, null);
  }
  const s = state('a06-officer-rejected'), m = s.messages[0], f = s.files[0];
  assert.equal(m.state, 'failed'); assert.equal(m.errorCode, 'provider_rejected');
  assert.equal(m.statusText, 'Not answered');
  assert.deepEqual(m.failureParts, {title: "Motet's model rejected this request",
    body: 'Nothing was answered. Your file is kept in this conversation, so you can ask again when you are ready. Motet could not confirm whether the model received the file.'});
  assert.deepEqual(f.officerDelivery, {state: 'unknown', requestId: m.requestId});
  assert.deepEqual(m.actions, []);
  assert.deepEqual(m.recovery, {canCheck: false, canFinish: false, canDeleteSavedCopies: false});
  assert.equal(s.officerAnswer, null); assert.equal(s.preview.officerState, 'unknown');
  const unknown = state('a09-officer-unknown').messages[0];
  assert.equal(unknown.state, 'delivery_unknown'); assert.equal(unknown.errorCode, undefined);
  assert.equal(unknown.failureParts, undefined);
});


test('retained preview reports explicit prior delivery without weakening earlier confirmation', () => {
  const cases = [
    ['a09-officer-retained', 'none', 'This file is kept in this Motet conversation. Motet was not asked, so nothing was sent to its model. Motet has not opened this file.'],
    ['a09-officer-retained-earlier', 'confirmed', 'This file is kept in this Motet conversation. Motet was not asked this time; an earlier question already gave it this file. Motet has not opened this file.'],
    ['a09-officer-retained-unknown', 'unknown', 'This file is kept in this Motet conversation. Motet was not asked this time. Motet has not opened this file.'],
  ];
  for (const [key, prior, body] of cases) {
    const a = active(key), s = a.snapshot(), m = s.messages[0], f = s.files[0], old = s.preview.authority;
    assert.equal(s.preview.officerState, 'retained_local');
    assert.equal(s.preview.officerPriorDelivery, prior); assert.equal(s.preview.payload.noticeParts.body, body);
    assert.equal(s.preview.authority.target.source.owner.requestId, m.requestId);
    assert.equal(m.statusText, 'Kept in this conversation · Motet was not asked');
    assert.equal(m.state, 'retained_local'); assert.equal(m.deliveryPhase, 'pre_dispatch');
    assert.equal(Object.hasOwn(s, 'officerRetainedPrior'), false);
    assert.equal(Object.hasOwn(f, 'officerPriorDelivery'), false);
    if (prior === 'confirmed') {
      assert.deepEqual(f.officerDelivery, {state: 'confirmed', requestId: s.messages[1].requestId});
      assert.equal(s.messages[1].state, 'answered'); assert.equal(f.usedInAnswer, false);
    } else assert.deepEqual(f.officerDelivery, {state: 'retained_local', requestId: m.requestId});
    operation(a, 'close_preview', old); openFiles(a);
    const retainedRef = () => a.snapshot().management.history.entries.find(e => e.requestId === m.requestId).fileRefs[0].authority;
    operation(a, 'open_preview', retainedRef());
    assert.equal(a.snapshot().preview.officerPriorDelivery, prior);
    assert.equal(a.snapshot().preview.payload.noticeParts.body, body);
    operation(a, 'close_preview', a.snapshot().preview.authority);
    if (prior === 'confirmed') {
      const earlierRef = a.snapshot().management.history.entries.find(e => e.requestId === s.messages[1].requestId).fileRefs[0].authority;
      operation(a, 'open_preview', earlierRef);
      assert.equal(a.snapshot().preview.officerState, 'supplied');
      assert.equal(a.snapshot().preview.officerPriorDelivery, null);
      operation(a, 'close_preview', a.snapshot().preview.authority);
    }
    operation(a, 'request_delete', retainedRef()); operation(a, 'confirm_delete', deletion(a).authority);
    operation(a, 'advance_sample_deletion', deletion(a).authority);
    rejectsUnchanged(a, {type: 'close_preview', authority: old}, /authority/);
    operation(a, 'begin_reattach', retainedRef(), {draftFence: fence(a)});
    operation(a, 'select_reattach_sample', a.snapshot().management.reattachPicker.authority,
      {draftFence: fence(a), sampleId: 'fixture-file-plan'});
    const newId = a.snapshot().draft.fileIds[0]; operation(a, 'open_preview', currentSource(a, newId));
    assert.equal(a.snapshot().preview.officerState, 'unsent');
    assert.equal(a.snapshot().preview.officerPriorDelivery, null);
    assert.deepEqual(a.snapshot().files.find(f => f.id === newId).officerDelivery, {state: 'not_sent', requestId: null});
  }
  for (const key of ATTACHMENT_SCENARIOS) {
    const preview = state(key).preview;
    if (preview && preview.officerState !== 'retained_local') assert.equal(preview.officerPriorDelivery, null, key);
  }
});


test('missing retained prior-delivery evidence is unknown rather than never sent', async () => {
  let source = await readFile(new URL('./fixture.mjs', import.meta.url), 'utf8');
  source = source.replace('export const ATTACHMENT_SCENARIOS', 'const ATTACHMENT_SCENARIOS')
    .replace('export function createAttachmentsFixture', 'function createAttachmentsFixture');
  const seed = "s.officerRetainedPrior = [{requestId: m.requestId, fileId: id('plan'), prior}];";
  assert.equal(source.split(seed).length, 2);
  source = source.replace(seed, 's.officerRetainedPrior = [];');
  vm.runInNewContext(source + `
    const a = createAttachmentsFixture({enabled:true,search:'?demo',scenario:'a09-officer-retained'});
    const p = a.snapshot().preview;
    assert.equal(p.officerState, 'retained_local');
    assert.equal(p.officerPriorDelivery, 'unknown');
    assert.equal(p.payload.noticeParts.body, 'This file is kept in this Motet conversation. Motet was not asked this time. Motet has not opened this file.');
  `, {URLSearchParams, assert});
});
