// Fictional, gated page states only. No upload, persistence, send or deletion runs here.
export const ATTACHMENT_SCENARIOS = Object.freeze([
  'a01-agent', 'a01-picker', 'a01-officer', 'a01-room',
  'a02-drop', 'a02-preparing', 'a03-one', 'a03-five', 'a03-relay', 'a03-room-one', 'a03-room-five', 'a03-details',
  'a04-mixed', 'a04-scanned', 'a04-relay-folder', 'a04-relay-officer', 'a04-budget', 'a04-folder', 'a04-quota', 'a04-recipient',
  'a05-saved', 'a05-scope', 'a05-resume', 'a05-conflict', 'a05-missing',
  'a06-preparing', 'a06-queued', 'a06-relay-unknown', 'a06-relay-partial', 'a06-unknown', 'a06-partial', 'a06-partial-capacity', 'a06-cleaned',
  'a09-officer-private', 'a09-officer-supplied', 'a09-officer-unknown', 'a09-officer-page', 'a09-officer-image', 'a09-officer-retained', 'a09-officer-retained-earlier', 'a09-officer-retained-unknown',
  'a04-officer-media', 'a04-officer-model', 'a08-officer-direct', 'a08-officer-uncited', 'a08-officer-followup', 'a08-officer-forward', 'a06-officer-retained', 'a06-officer-rejected',
  'a07-answered', 'a07-together', 'a08-doc', 'a08-compare', 'a08-gone', 'a08-forward',
  'a09-pdf', 'a09-image', 'a09-relay-pdf', 'a09-relay-image', 'a09-text', 'a09-fallback', 'a10-files', 'a10-reattach',
  'a11-full', 'a11-confirm', 'a11-progress', 'a11-error', 'a11-done',
  'a11-storage', 'a11-storage-set', 'a11-expiry', 'a11-expiry-all', 'a11-expiry-set',
  'a11-expiry-all-set', 'a11-storage-all-set', 'a10-files-expiry',
  'a11-expiry-selection-changed', 'a11-expiry-removal-started', 'a11-expiry-partial', 'a11-expiry-unconfirmed',
]);
// Pure authored samples: these are deliberately not decoded document/image bytes.
let attachmentInstanceSequence = 0;
const REQUIREMENT_LINES = ['# Requirements', '', '## Pricing', '', '- Show the seasonal price on every catalogue card.', '- Keep the list price visible, struck through.', '', '## Rounding', '', 'Round each cart line to the cent.', 'Never round the cart total.', '', 'The table and the checkout must use', 'the same shared pricing function.', '', 'A four-item cart must give the same', 'total in both places.', '', '## Out of scope', '', '- Currency conversion.'];
function authoredLocation(key, location) {
  if (!location || typeof location !== 'object') return false;
  if (key === 'plan') return Object.keys(location).length === 1 && Number.isSafeInteger(location.page) && location.page >= 1 && location.page <= 6;
  if (key === 'req') return Object.keys(location).length === 2 && Number.isSafeInteger(location.lineStart) && Number.isSafeInteger(location.lineEnd) && location.lineStart >= 1 && location.lineEnd >= location.lineStart && location.lineEnd <= REQUIREMENT_LINES.length;
  return key === 'shot' && Object.keys(location).length === 1 && location.image === true;
}
function authoredPreview(key, page = 1, recipient = 'Ada', privateDraft = false) {
  const common = {sample: true, representation: 'authored_sample'};
  if (key === 'plan') {
    const pages = [
      ['1. Delivery plan', ['This fictional plan covers seasonal prices in the Orchard catalogue and checkout.', 'Use the requirements and the pricing table to check each rollout step.']],
      ['2. Rollout', ['The new rounding goes to the catalogue first, then to checkout. Each step is checked against the pricing table before the next one starts.', 'Prices are rounded on each cart line, never on the total. A four-item cart must show the same total in the table and at checkout.', 'Rollout starts once the pricing suite passes and one review is complete. No date is fixed in this plan.']],
      ['3. Catalogue', ['Show the seasonal price on each catalogue card. Keep the list price visible.', 'Check a four-item selection against the shared pricing table.']],
      ['4. Checkout', ['Round each cart line to the cent before adding the lines.', 'Compare the table and checkout totals for the same four items.']],
      ['5. Review', ['Record the pricing test result and one review before rollout.', 'A started test is not a passing result.']],
      ['6. Scope', ['Currency conversion is outside this fictional delivery plan.', 'This sample describes intended work, not recorded agent activity.']],
    ];
    if (!Number.isSafeInteger(page) || page < 1 || page > pages.length) throw new Error('Unavailable authored page.');
    const [title, text] = pages[page - 1];
    return {kind: 'pdf', payload: {...common, page, title, paragraphs: text.map((t, i) => page === 2 && i === 1
      ? [{text: 'Prices are rounded on each cart line, never on the total.', highlight: true}, {text: ' A four-item cart must show the same total in the table and at checkout.', highlight: false}]
      : [{text: t, highlight: false}]), footer: `Orchard delivery plan · sample document · page ${page}`}};
  }
  if (key === 'req') return {kind: 'text', payload: {...common, lines: REQUIREMENT_LINES.slice()}};
  if (key === 'shot') return {kind: 'image', payload: {...common, drawing: 'orchard_checkout', width: 1440, height: 900,
    title: 'Orchard checkout', cartLabel: 'Your cart · 4 items', items: [1, 2, 3, 4].map(i => ({label: `Seasonal item ${i}`, priceText: '$16.99'})),
    totalLabel: 'Total', totalText: '$67.96', payLabel: 'Pay now', sampleLabel: 'Sample image', alt: 'Sample checkout screenshot'}};
  return {kind: 'fallback', payload: {...common, noticeParts: {title: 'This file cannot be shown here',
    body: privateDraft ? 'Its pages could not be drawn in this window. The file itself is unchanged and is still private until you press Send.' : `Its pages could not be drawn in this window. The file itself is unchanged and is still available to ${recipient}.`},
    footer: 'No preview was made. Nothing is guessed about what the file contains.'}};
}

const MiB = 1_048_576;
const NOW = '2030-11-14T15:58:00.000Z';
const SAMPLE_ZONE = 'Etc/UTC', INVENTORY_PAGE_SIZE = 20;
const EXPIRY_SCENARIOS = new Set(['a11-storage', 'a11-storage-set', 'a11-expiry', 'a11-expiry-all', 'a11-expiry-set', 'a11-expiry-all-set', 'a11-storage-all-set', 'a10-files-expiry', 'a11-expiry-selection-changed', 'a11-expiry-removal-started', 'a11-expiry-partial', 'a11-expiry-unconfirmed']);
const expiryPolicy = () => ({generation: 1, date: null, timeZone: null, deadlineAt: null, state: 'none', operationId: null, removedAt: null, removalTiming: null});
function sampleDeadline(date) {
  if (typeof date !== 'string' || !/^2030-\d{2}-\d{2}$/.test(date)) throw new Error('Choose a valid sample date in 2030.');
  const stamp = Date.parse(date + 'T00:00:00.000Z');
  if (!Number.isFinite(stamp) || new Date(stamp).toISOString().slice(0, 10) !== date) throw new Error('Choose a valid calendar date.');
  return new Date(stamp + 86_400_000).toISOString();
}
function sampleDateLabel(date) { return Number(date.slice(8)) + ' ' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][Number(date.slice(5, 7)) - 1]; }
const TXT = 'Compare the plan with the requirements. Tell me what is missing.';
const clone = value => JSON.parse(JSON.stringify(value));
const id = key => `fixture-file-${key}`;
const destination = (kind = 'agent', agent = 'ada', scope = 'agent') => ({
  kind, agentId: kind === 'agent' ? agent : null,
  label: kind === 'officer' ? 'Motet' : agent === 'cleo' ? 'Cleo' : agent === 'bram' ? 'Bram' : agent === 'cloud' ? 'Codex cloud' : 'Ada',
  projectId: 'fixture-orchard', projectLabel: 'Orchard',
  scope: {kind: scope, agentId: scope === 'agent' ? agent : null},
  modelId: kind === 'officer' ? 'fixture-analysis-model' : null,
});
const capability = (available = true, analysisOnly = false) => ({
  available, reason: available ? null : 'Attachments are disabled.',
  formats: available ? ['application/pdf', 'image/png', 'image/jpeg', 'text/plain', 'text/markdown'] : [],
  maxFiles: 5, maxFileBytes: 10 * MiB, maxDraftBytes: 20 * MiB, storageLimitBytes: 5_000_000_000,
  preparationModes: available ? Object.fromEntries(['application/pdf', 'image/png', 'image/jpeg', 'text/plain', 'text/markdown'].map(media => [media, 'authored_sample'])) : {},
  analysisOnly, maxTextChars: analysisOnly ? 4000 : 2000, sample: true,
});
const RELAY_READY = 'Ready to share as it is. Motet has not opened this file. It stays private until you press Send.';
const relayOfficerIssue = name => `${name} cannot go to Motet yet. Send it to an agent, or remove it to ask Motet in words.`;
const FILES = {
  plan: ['delivery-plan.pdf', 'application/pdf', 1_887_437],
  shot: ['checkout-screenshot.png', 'image/png', 655_360],
  photo: ['orchard-label.jpg', 'image/jpeg', 262_144],
  req: ['requirements.md', 'text/markdown', 4_096],
  long: ['orchard-delivery-plan-2030-q4-revision-3-reviewed-by-finance.pdf', 'application/pdf', 2_516_582],
  table: ['pricing-table-export.txt', 'text/plain', 13_312],
  locked: ['supplier-contract.pdf', 'application/pdf', 931_840],
  docx: ['meeting-notes.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 49_152],
  big: ['site-survey-photos.pdf', 'application/pdf', 9_856_614],
  old: ['delivery-plan-draft.pdf', 'application/pdf', 1_153_434],
  scan: ['signed-delivery-note.pdf', 'application/pdf', 1_153_434],
};
function file(key) {
  const [name, mediaType, bytes] = FILES[key];
  return {id: id(key), name, mediaType, bytes, preparation: 'ready', preparationKind: 'authored_sample', previewAvailable: false, preparationText: null, uploadPercent: null,
    officerDelivery: null, officerRefusal: null,
    error: null, availability: 'private', availableTo: null, usedInAnswer: false,
    previewable: true, retention: {pinned: false, reason: null, expiresAt: null, expiryPolicy: expiryPolicy()},
    changedAt: null, actions: ['preview', 'remove_from_draft']};
}
function configureRelay(s) {
  const formats = s.destination.kind === 'officer' ? ['text/plain', 'text/markdown']
    : ['application/pdf', 'image/png', 'image/jpeg', 'text/plain', 'text/markdown'];
  s.capabilities.formats = formats;
  s.capabilities.preparationModes = Object.fromEntries(formats.map(media => [media,
    ['application/pdf', 'image/png', 'image/jpeg'].includes(media) ? 'original_relay' : 'text']));
  for (const f of s.files) {
    if (['plan', 'shot', 'photo'].some(key => f.id === id(key))) f.preparationKind = 'original_relay';
    else if (s.capabilities.preparationModes[f.mediaType] === 'text') f.preparationKind = 'text';
  }
}
function selectedSample(s, key) {
  const f = file(key), mode = s.capabilities.preparationModes[f.mediaType];
  if (['original_relay', 'text', 'officer_direct'].includes(mode)) f.preparationKind = mode;
  if (mode === 'officer_direct') { f.officerDelivery = {state: 'not_sent', requestId: null}; f.bytes = key === 'shot' ? 64000 : 128000; }
  return f;
}
function relayPreview(recipient, privateDraft) {
  return {kind: 'fallback', payload: {sample: true, representation: 'authored_sample',
    noticeParts: {title: 'No preview for this kind of file yet', body: privateDraft ? RELAY_READY
      : `Motet passes it to ${recipient} as it is and does not open it. The file is unchanged and is still available to ${recipient}.`},
    footer: 'Nothing was checked or drawn.'}};
}
// Direct-input qualification is fictional here. No real provider is enabled.
function configureOfficerDirect(s, delivery = 'not_sent', requestId = null) {
  s.capabilities.formats = ['application/pdf', 'image/png'];
  s.capabilities.preparationModes = {'application/pdf': 'officer_direct', 'image/png': 'officer_direct'};
  s.capabilities.modelId = s.destination.modelId;
  s.capabilities.supportedModelLabel = 'Haiku 5.5';
  s.capabilities.capabilityRevision = 'fixture-officer-direct-v1';
  for (const f of s.files) {
    f.preparationKind = 'officer_direct';
    f.officerDelivery = {state: delivery, requestId};
    f.bytes = f.mediaType === 'image/png' ? 64000 : 128000;
    f.availableTo = null;
  }
}
function officerDirectAnswer(s, uncited = false, followup = false) {
  officer(s);
  const m = s.messages[0];
  addFiles(s, 'shot'); m.fileIds.push(id('shot'));
  const shot = s.files.find(f => f.id === id('shot'));
  Object.assign(shot, {availability: 'available', retention: {pinned: false, reason: null, expiresAt: null}});
  configureOfficerDirect(s, 'confirmed', m.requestId);
  s.officerAnswer.citations = [uploadCitation('plan', {page: 2}, 'p. 2')];
  if (!uncited) s.officerAnswer.citations.push(uploadCitation('shot', {image: true}, 'whole image'));
  s.officerAnswer.text = uncited ? 'The plan calls for rounding on each cart line. This answer cites the PDF; the other supplied file remains a dependency.'
    : 'The plan calls for rounding on each cart line. The supplied image is referenced as a whole; Motet does not draw it here.';
  // Seed-only complete dependencies; initializePhase4 moves these into private fixture state.
  s.officerConsumedFiles = [id('plan'), id('shot')];
  if (followup) {
    m.createdAt = '2030-10-01T12:00:00.000Z';
    s.officerAnswer.text = 'The retained plan still calls for rounding on each cart line. This fictional follow-up uses the same files after reopening the conversation.';
  }
}
function officerDirectPreview(f, citationTarget, retainedPrior = null) {
  const state = citationTarget ? citationTarget.location?.page ? 'cited_page' : 'cited_whole'
    : retainedPrior !== null ? 'retained_local' : ({not_sent: 'unsent', retained_local: 'retained_local', confirmed: 'supplied', unknown: 'unknown'})[f.officerDelivery?.state];
  const prior = state === 'retained_local' ? retainedPrior || 'unknown' : null;
  const bodies = {
    unsent: RELAY_READY,
    retained_local: {
      none: 'This file is kept in this Motet conversation. Motet was not asked, so nothing was sent to its model. Motet has not opened this file.',
      confirmed: 'This file is kept in this Motet conversation. Motet was not asked this time; an earlier question already gave it this file. Motet has not opened this file.',
      unknown: 'This file is kept in this Motet conversation. Motet was not asked this time. Motet has not opened this file.',
    }[prior],
    supplied: 'This file was supplied to the model as it is. Motet does not open this kind of file.',
    unknown: 'Motet could not confirm that the model received this file. Motet does not open this kind of file.',
    cited_page: `Motet cited page ${citationTarget?.location?.page} of this file. Motet does not open this kind of file, so it cannot show the page.`,
    cited_whole: 'Motet used this image as a whole. Motet does not open this kind of file.',
  };
  if (!Object.hasOwn(bodies, state)) throw new Error('Motet input delivery could not be verified.');
  return {kind: 'fallback', officerState: state, officerPriorDelivery: prior, payload: {sample: true, representation: 'authored_sample',
    noticeParts: {title: 'No preview for this kind of file yet', body: bodies[state]}, footer: 'Nothing was checked or drawn.'}};
}
function draft(dest) {
  return {id: `fixture-draft-${dest.kind}-${dest.scope.kind}-${dest.scope.agentId || 'room'}`,
    revision: 1, writerGeneration: 1, state: 'editing', destination: clone(dest), text: '',
    suggestedText: false, fileIds: [], editable: true, canSend: false,
    sendBlockedReason: 'Write a message or attach a file.', blockCode: 'empty', notice: null, noticeCode: null, actions: []};
}
function base(enabled = true, scenario = null) {
  const dest = enabled ? destination() : null;
  return {schemaVersion: 1, fixture: true, enabled, scenario, lastAction: null, destination: dest,
    capabilities: capability(enabled), draft: dest ? draft(dest) : null, files: [], messages: [],
    management: null, officerAnswer: null, pickerFiles: [], filesCount: 0, filesList: null, deleteDialog: null, preview: null,
    ui: {surface: 'desk', pickerOpen: false, dropActive: false, detailsFileId: null, storageOpen: false},
    storage: {usedBytes: enabled ? 1_200_000_000 : 0, limitBytes: 5_000_000_000, full: false}};
}
function setDestination(s, dest, surface = 'desk') {
  s.destination = dest;
  s.draft = draft(dest);
  s.ui.surface = surface;
  s.capabilities.analysisOnly = dest.kind === 'officer';
  s.capabilities.maxTextChars = dest.kind === 'officer' ? 4000 : 2000;
}
function addFiles(s, ...keys) {
  for (const key of keys) if (!s.files.some(f => f.id === id(key))) s.files.push(file(key));
  return keys.map(key => s.files.find(f => f.id === id(key)));
}
function setDraft(s, keys, text = TXT) {
  addFiles(s, ...keys);
  Object.assign(s.draft, {fileIds: keys.map(id), text, canSend: true, sendBlockedReason: null});
}
const ERRORS = {
  scanned_pdf_unreadable: ['is a scan with no readable text, so Ada cannot read it. Attach a version with selectable text, or remove it.', []],
  locked_pdf: ['is protected by a password, so it cannot be read. Unlock it and attach it again, or remove it.', []],
  unsupported_type: ['is a Word file. Attach a PDF, PNG, JPEG, text or Markdown file, or remove it.', []],
  officer_budget: ['is too large for Motet to process with the current limit. Reduce it or remove it.', []],
  folder_unreachable: ['could not be prepared: Motet cannot save files in Ada’s project folder right now.', ['retry_preparation']],
  storage_full: ['could not be stored: attachment storage on this computer is full.', ['manage_files', 'retry_preparation']],
};
function failFile(s, key, code) {
  const f = addFiles(s, key)[0], [message, actions] = ERRORS[code];
  Object.assign(f, {preparation: 'failed', previewable: false,
    error: {code, message: `${f.name} ${message}`, actions: [...actions, 'remove_from_draft']},
    actions: [...actions, 'remove_from_draft']});
  Object.assign(s.draft, {canSend: false, sendBlockedReason:
    'Send is off until these files are fixed or removed. To send the text alone, remove them first.'});
}
const STATUS = {
  preparing_delivery: 'Preparing delivery · saving 2 files in Ada’s project folder',
  queued: 'Queued · waiting for a reply',
  delivery_unknown: 'Delivery unknown · check status; do not resend',
  partial_not_sent: 'Message not sent; some files were saved for the agent',
  answered: 'Answered',
};
function messageOperation(state, generation = 1) {
  return {revision: 1, operationGeneration: generation,
    deliveryPhase: state === 'delivery_unknown' ? 'unknown' : ['partial_not_sent', 'preparing_delivery'].includes(state) ? 'pre_dispatch' : 'dispatched',
    cleanup: {state: 'none', fileIds: [], releasedBytes: 0}};
}
function sent(s, state, keys = ['plan', 'req'], text = TXT) {
  const files = addFiles(s, ...keys);
  const partial = state === 'partial_not_sent', unknown = state === 'delivery_unknown';
  const message = {id: 'fixture-message-1', requestId: 'fixture-request-1',
    destination: clone(s.destination), text, fileIds: keys.map(id), createdAt: NOW, state,
    ...messageOperation(state),
    statusText: STATUS[state], recovery: {canCheck: unknown, canFinish: partial, canDeleteSavedCopies: partial},
    actions: unknown ? ['check_status'] : partial ? ['finish_send', 'delete_saved_copies'] : []};
  s.messages.push(message);
  files.forEach((f, i) => {
    f.availability = state === 'preparing_delivery' ? 'saving' : partial && i > 0 ? 'not_saved' : 'available';
    f.availableTo = f.availability === 'available' ? s.destination.label : null;
    f.previewable = f.availability === 'available';
    f.actions = f.previewable ? ['preview'] : [];
    f.retention = state === 'answered'
      ? {pinned: false, reason: null, expiresAt: null}
      : {pinned: true, reason: state, expiresAt: null};
  });
  return message;
}
function uploadCitation(key, location, suffix) {
  const name = FILES[key][0];
  return {id: `fixture-citation-${key}-${suffix}`, kind: 'upload', fileId: id(key), activityId: null,
    label: `${name} · ${suffix}`, location, available: true,
    navigation: {kind: 'unavailable', reason: 'phase4_file_preview'}};
}
function officer(s, compare = false) {
  setDestination(s, destination('officer'), 'officer');
  const message = sent(s, 'answered', compare ? ['plan', 'req'] : ['plan'], compare
    ? 'Compare this plan with Ada’s recorded work.' : 'What does the delivery plan say about when rollout starts?');
  s.files.forEach(f => {f.availableTo = null; f.usedInAnswer = true;});
  s.officerAnswer = {id: 'fixture-answer-1', state: 'available', analysisOnly: true,
    replyTo: {messageId: message.id, requestId: message.requestId, destination: clone(message.destination)},
    model: {id: 'fixture-analysis-model', label: 'Haiku 5.5'}, sample: true,
    sourceValidation: 'current', notice: null, actions: [], text: compare
      ? 'The plan and the requirements both ask for rounding on each cart line. Ada’s log shows an edit to season.ts that rounds each line, and a pricing test run that has no result yet.\n\nYour files say what is wanted. Only the recorded activity shows what Ada did, and it does not yet show the tests passing.'
      : 'The plan gives no date. It says rollout starts once the pricing suite passes and one review is complete (p. 2), and that the catalogue goes first, then checkout (p. 2).\n\nThis answer is drawn from your file only. It says nothing about what any agent has done.',
    citations: [uploadCitation('plan', {page: 2}, 'p. 2'), compare
      ? uploadCitation('req', {lineStart: 10, lineEnd: 17}, 'lines 10–17')
      : uploadCitation('plan', {page: 4}, 'p. 4')]};
  if (compare) for (const [key, label] of [
    ['edit', 'Ada · edited season.ts · 3:50 PM'], ['tests', 'Ada · tests started · 3:53 PM'],
  ]) s.officerAnswer.citations.push({id: `fixture-citation-${key}`, kind: 'activity', fileId: null,
    activityId: `fixture-activity-${key}`, label, location: null, available: true,
    navigation: {kind: 'unavailable', reason: 'fixture_activity_only'}});
}
function unavailableFile(f, state, date) {
  Object.assign(f, {availability: state, availableTo: null, usedInAnswer: false, previewable: false,
    changedAt: date, actions: ['attach_again'], retention: {pinned: false, reason: null, expiresAt: null}});
}
function history(s) {
  sent(s, 'queued');
  const [shot, table, big, old] = addFiles(s, 'shot', 'table', 'big', 'old');
  Object.assign(shot, {availability: 'available', availableTo: 'Ada', actions: ['preview', 'delete_local_copies'],
    retention: {pinned: false, reason: null, expiresAt: null}});
  Object.assign(table, {availability: 'available', availableTo: 'Ada', actions: ['preview', 'delete_local_copies'],
    retention: {pinned: false, reason: null, expiresAt: null}});
  unavailableFile(big, 'deleted', '2030-10-31T09:05:00.000Z');
  unavailableFile(old, 'missing', '2030-10-28T14:31:00.000Z');
  s.filesList = {destination: clone(s.destination), nextCursor: null, entries: [
    {requestId: 'fixture-request-1', createdAt: NOW, text: TXT, messageState: 'queued', fileIds: [id('plan'), id('req')]},
    {requestId: 'fixture-request-day-six', createdAt: '2030-11-08T10:12:00.000Z', text: 'Review the attached file', messageState: 'answered', fileIds: [shot.id]},
    {requestId: 'fixture-request-table-history', createdAt: '2030-11-03T16:40:00.000Z', text: 'Check the pricing table against the export.', messageState: 'answered', fileIds: [table.id]},
    {requestId: 'fixture-request-deleted', createdAt: '2030-10-30T09:05:00.000Z', text: 'Does the survey match the plan?', messageState: 'answered', fileIds: [big.id]},
    {requestId: 'fixture-request-missing', createdAt: '2030-10-28T14:31:00.000Z', text: 'Review the attached file', messageState: 'partial_not_sent', fileIds: [old.id]},
  ]};
}
function deletion(s, state) {
  sent(s, 'queued');
  const f = s.files.find(f => f.id === id('plan'));
  const copyState = state === 'deleted' ? 'deleted' : state === 'deleting' ? 'deleting' : 'present';
  s.deleteDialog = {state, fileIds: [f.id], requestId: 'fixture-request-1',
    pinnedWarning: 'Ada’s message is still queued. If you delete this file now, Ada may not be able to read it when its turn comes. Deleting the file does not cancel the message.',
    removes: ['Motet’s stored copy of the file.', 'The copy saved in Ada’s project folder.',
      'Motet’s use of it as a source, and answers that depend on it.'],
    retains: ['Messages already in the agent’s own chat.', 'Activity history, and copies an agent made.',
      'Anything already sent to a model provider.'],
    copies: ['stored', 'workspace'].map(kind => ({fileId: f.id, kind, state: copyState, reason: null})),
    releasedBytes: 0, actions: state === 'confirm' ? ['keep_file', 'delete_local_copies'] : ['close']};
  if (state === 'partial_failure') {
    s.deleteDialog.copies[0].state = 'deleted';
    Object.assign(s.deleteDialog.copies[1], {state: 'identity_changed',
      reason: 'The copy in Ada’s project folder was changed or moved. It no longer matches the file Motet saved, so Motet left it alone rather than delete something that may not be its own. Remove it yourself if you want it gone.'});
    s.deleteDialog.releasedBytes = f.bytes;
    // A changed workspace copy is not still a verified available attachment.
    unavailableFile(f, 'missing', NOW);
  } else if (state === 'deleted') {
    s.deleteDialog.releasedBytes = 2 * f.bytes;
    unavailableFile(f, 'deleted', NOW);
  } else if (state === 'deleting') {
    // Deletion intent fences publication even while cleanup is incomplete.
    f.previewable = false; f.actions = []; f.usedInAnswer = false;
  }
  s.storage.usedBytes -= s.deleteDialog.releasedBytes;
}
function makeScenario(key) {
  const s = base(true, key);
  switch (key) {
    case 'a01-agent': break;
    case 'a01-picker': s.ui.pickerOpen = true; s.pickerFiles = ['plan', 'shot', 'req'].map(file); break;
    case 'a01-officer': setDestination(s, destination('officer'), 'officer'); break;
    case 'a01-room': setDestination(s, destination('officer', null, 'room'), 'room'); break;
    case 'a02-drop': s.ui.dropActive = true; setDraft(s, [], 'Here is the plan we agreed.'); break;
    case 'a02-preparing': {
      setDraft(s, ['plan', 'shot', 'req']);
      Object.assign(s.files[0], {preparation: 'uploading', uploadPercent: 62, previewable: false, actions: ['cancel_upload']});
      Object.assign(s.files[1], {preparation: 'preparing', previewable: false, actions: ['remove_from_draft']});
      Object.assign(s.draft, {canSend: false, sendBlockedReason: 'Send is off while 2 files are still being prepared.'}); break;
    }
    case 'a03-one': setDraft(s, ['plan'], 'Review the attached file'); s.draft.suggestedText = true; break;
    case 'a03-five': setDraft(s, ['plan', 'shot', 'req', 'long', 'table']); break;
    case 'a03-relay': setDraft(s, ['plan', 'shot', 'photo', 'req']); configureRelay(s); break;
    case 'a03-room-one':
      setDestination(s, destination('officer', null, 'room'), 'room');
      setDraft(s, ['plan'], 'Review the attached file'); s.draft.suggestedText = true; break;
    case 'a03-room-five':
      setDestination(s, destination('officer', null, 'room'), 'room');
      setDraft(s, ['plan', 'shot', 'req', 'long', 'table']); break;
    case 'a03-details': setDraft(s, ['long', 'plan', 'req']); s.ui.detailsFileId = id('long'); break;
    case 'a04-relay-folder':
      setDraft(s, ['plan', 'shot']); configureRelay(s); failFile(s, 'plan', 'folder_unreachable'); break;
    case 'a04-relay-officer':
      setDestination(s, destination('officer'), 'officer'); setDraft(s, ['plan', 'shot']); configureRelay(s);
      for (const f of s.files) Object.assign(f, {preparation: 'failed', previewable: false,
        error: {code: 'recipient_unavailable', message: relayOfficerIssue(f.name), actions: ['remove_from_draft']}, actions: ['remove_from_draft']});
      break;
    case 'a04-scanned': setDraft(s, ['plan', 'scan']); failFile(s, 'scan', 'scanned_pdf_unreadable'); break;
    case 'a04-mixed': setDraft(s, ['plan', 'locked', 'docx']); failFile(s, 'locked', 'locked_pdf'); failFile(s, 'docx', 'unsupported_type'); break;
    case 'a04-budget': setDestination(s, destination('officer'), 'officer'); setDraft(s, ['plan', 'big'], 'Does the survey match the plan?'); failFile(s, 'big', 'officer_budget'); break;
    case 'a04-folder': setDraft(s, ['plan', 'req']); failFile(s, 'plan', 'folder_unreachable'); failFile(s, 'req', 'folder_unreachable'); break;
    case 'a04-quota': setDraft(s, ['plan', 'long']); failFile(s, 'long', 'storage_full'); s.storage.usedBytes = s.storage.limitBytes; s.storage.full = true; break;
    case 'a04-recipient':
      setDestination(s, destination('agent', 'cloud'));
      s.capabilities = capability(false); s.capabilities.reason = 'Files cannot be attached for Codex cloud: it has no session on this computer to receive them.';
      s.draft.editable = false; s.draft.sendBlockedReason = s.capabilities.reason; break;
    case 'a05-saved':
      setDestination(s, destination('officer'), 'officer');
      s.draft.noticeCode = 'saved_recipient'; s.draft.notice = 'Your Ada draft is saved. It is not shared with Motet.'; break;
    case 'a05-scope':
      setDestination(s, destination('officer', null, 'room'), 'room');
      s.draft.noticeCode = 'saved_scope'; s.draft.notice = 'Your draft about Ada is saved. This one is for the whole room.'; break;
    case 'a05-resume':
      setDraft(s, ['plan', 'req']);
      s.files.forEach(f => {f.actions = []; f.previewable = false;});
      Object.assign(s.draft, {state: 'resume_available', editable: false, sendBlockedReason: 'Resume this draft or create a new draft.',
        notice: 'A draft for Ada was open in another window. 2 files and a message: “Compare the plan with the requirements…”. Resuming makes this window the one that can edit it.', actions: ['resume_draft', 'create_draft']}); break;
    case 'a05-conflict':
      setDraft(s, ['plan', 'req']);
      Object.assign(s.draft, {state: 'conflict', editable: false, canSend: false,
        sendBlockedReason: 'This draft was resumed in another window, so it cannot be changed here.',
        notice: 'This draft is being edited in another window. Nothing has been merged. Resume it here to take it over, or start a separate draft.', actions: ['resume_draft', 'create_draft']});
      s.files.forEach(f => {f.actions = []; f.previewable = false;}); break;
    case 'a05-missing':
      Object.assign(s.draft, {state: 'unavailable', editable: false, canSend: false,
        sendBlockedReason: 'Start a new draft.', notice: 'Draft unavailable—attach the files again. This window can no longer reach the earlier draft.', actions: ['create_draft']}); break;
    case 'a06-relay-unknown': sent(s, 'delivery_unknown', ['plan', 'shot']); configureRelay(s); break;
    case 'a06-relay-partial': sent(s, 'partial_not_sent', ['plan', 'shot']); configureRelay(s); break;
    case 'a06-preparing': sent(s, 'preparing_delivery'); break;
    case 'a06-queued': sent(s, 'queued'); break;
    case 'a06-unknown': sent(s, 'delivery_unknown'); break;
    case 'a06-partial': sent(s, 'partial_not_sent'); break;
    case 'a06-partial-capacity':
      sent(s, 'partial_not_sent');
      setDraft(s, ['shot', 'table'], 'Keep this draft while the earlier file copies are cleared.');
      // A near-capacity seed enters the ordinary copy ledger before readiness is derived.
      // Clearing the prior request's verified workspace copy frees this draft's delivery bytes.
      s.storage.usedBytes = s.storage.limitBytes; break;
    case 'a07-answered': {
      const message = sent(s, 'answered');
      message.reply = {id: 'fixture-reply-1', messageId: message.id, requestId: message.requestId,
        destination: clone(message.destination), author: {kind: 'agent', agentId: 'ada', label: 'Ada'},
        text: 'The plan asks for a review step that the requirements do not mention. Everything else lines up.', sample: true};
      break;
    }
    case 'a07-together':
      setDestination(s, {...destination('agent', 'cleo'), projectId: 'fixture-lighthouse', projectLabel: 'Lighthouse'});
      sent(s, 'queued', ['plan', 'shot'], 'Here is the plan and the screen it describes.');
      setDraft(s, ['req'], 'One more file for the same question.'); break;
    case 'a09-officer-private':
    case 'a04-officer-media':
    case 'a04-officer-model':
    case 'a08-officer-forward': {
      setDestination(s, destination('officer'), 'officer');
      setDraft(s, key === 'a04-officer-media' ? ['plan', 'photo'] : ['plan'], key === 'a08-officer-forward' ? 'Send this to Ada.' : 'Review the attached file');
      configureOfficerDirect(s);
      if (key === 'a04-officer-model') { s.destination.modelId = 'fixture-unqualified-model'; s.draft.destination.modelId = s.destination.modelId; s.capabilities.formats = []; s.capabilities.preparationModes = {}; s.capabilities.modelId = s.destination.modelId; }
      if (['a04-officer-media', 'a04-officer-model'].includes(key)) {
        for (const f of s.files.filter(f => key === 'a04-officer-model' || f.mediaType === 'image/jpeg')) {
          const reason = key === 'a04-officer-model' ? 'model' : 'media';
          f.officerRefusal = {reason, supportedModelLabel: reason === 'model' ? 'Haiku 5.5' : null};
          f.preparation = 'failed'; f.previewable = false;
          f.error = {code: `officer_${reason}_unavailable`, message: reason === 'model'
            ? `${f.name} cannot go to Motet on this model. Choose Haiku 5.5, send it to an agent, or remove it.`
            : `${f.name} cannot go to Motet yet: Motet takes PDF and PNG files. Send it to an agent, or remove it.`, actions: ['remove_from_draft']};
        }
      }
      if (key === 'a08-officer-forward') {
        s.officerAnswer = {id: 'fixture-answer-refused', state: 'forwarding_refused', text: null, replyTo: null, model: null, sample: true,
          noticeParts: {title: 'To share this file with Ada, select Ada and attach it there.', body: 'Nothing was sent and nothing was passed on. Your draft and its file are still below. With a file attached, Motet can analyse it; it does not act on it.'},
          analysisOnly: true, sourceValidation: 'current', citations: [], actions: []};
      }
      if (key === 'a09-officer-private') s.preview = {fileId: id('plan'), kind: 'fallback', location: null};
      break;
    }
    case 'a09-officer-supplied':
    case 'a09-officer-unknown':
    case 'a09-officer-retained':
    case 'a09-officer-retained-earlier':
    case 'a09-officer-retained-unknown':
    case 'a06-officer-retained':
    case 'a06-officer-rejected': {
      setDestination(s, destination('officer'), 'officer');
      const local = ['a09-officer-retained', 'a09-officer-retained-earlier', 'a09-officer-retained-unknown', 'a06-officer-retained'].includes(key), supplied = key === 'a09-officer-supplied';
      const m = sent(s, supplied ? 'answered' : 'delivery_unknown', ['plan']);
      if (local || key === 'a06-officer-rejected') {
        m.state = local ? 'retained_local' : 'failed'; m.deliveryPhase = local ? 'pre_dispatch' : 'unknown';
        m.statusText = local ? 'Kept in this conversation · Motet was not asked' : 'Not answered';
        if (!local) m.failureParts = {title: "Motet's model rejected this request",
          body: 'Nothing was answered. Your file is kept in this conversation, so you can ask again when you are ready. Motet could not confirm whether the model received the file.'};
        m.errorCode = local ? null : 'provider_rejected'; m.actions = []; m.recovery = {canCheck: false, canFinish: false, canDeleteSavedCopies: false};
      }
      configureOfficerDirect(s, local ? 'retained_local' : supplied ? 'confirmed' : 'unknown', m.requestId);
      if (local) {
        const prior = key === 'a09-officer-retained-earlier' ? 'confirmed' : key === 'a09-officer-retained-unknown' ? 'unknown' : 'none';
        // Explicit authored evidence for this retained attempt; absent history never means no prior send.
        s.officerRetainedPrior = [{requestId: m.requestId, fileId: id('plan'), prior}];
        if (prior === 'confirmed') {
          const earlier = {...clone(m), id: 'fixture-message-earlier', requestId: 'fixture-request-earlier',
            createdAt: '2030-10-01T12:00:00.000Z', state: 'answered', deliveryPhase: 'dispatched', statusText: 'Answered'};
          s.messages.push(earlier);
          s.files[0].officerDelivery = {state: 'confirmed', requestId: earlier.requestId};
        }
      }
      s.files.forEach(f => { f.retention = {pinned: false, reason: null, expiresAt: null}; });
      if (key !== 'a06-officer-retained') s.preview = {fileId: id('plan'), kind: 'fallback', location: null}; break;
    }
    case 'a08-officer-direct':
    case 'a08-officer-uncited':
    case 'a08-officer-followup':
    case 'a09-officer-page':
    case 'a09-officer-image':
      officerDirectAnswer(s, key === 'a08-officer-uncited', key === 'a08-officer-followup');
      if (key === 'a09-officer-page') s.preview = {fileId: id('plan'), kind: 'fallback', location: {page: 2}};
      if (key === 'a09-officer-image') s.preview = {fileId: id('shot'), kind: 'fallback', location: {image: true}};
      break;
    case 'a08-doc': officer(s); break;
    case 'a08-compare': officer(s, true); break;
    case 'a08-gone':
      officer(s); unavailableFile(s.files[0], 'deleted', '2030-11-13T12:00:00.000Z');
      Object.assign(s.officerAnswer, {state: 'source_unavailable', text: null, sourceValidation: 'invalid',
        noticeParts: {title: 'This answer is no longer shown.',
          body: 'It used delivery-plan.pdf, which you deleted. To get an answer again, attach the file and ask again.'},
        actions: []});
      s.officerAnswer.citations.forEach(c => {c.available = false; c.navigation = {kind: 'unavailable', reason: 'source_unavailable'};}); break;
    case 'a08-forward':
      setDestination(s, destination('officer'), 'officer'); setDraft(s, ['plan'], 'Send this to Ada.');
      s.officerAnswer = {id: 'fixture-answer-refused', state: 'forwarding_refused', text: null,
        replyTo: null, model: null, sample: true,
        noticeParts: {title: 'To share this file with Ada, select Ada and attach it there.',
          body: 'Nothing was sent and nothing was passed on. Your draft and its file are still below. With a file attached, Motet can analyse it; it does not act on it.'},
        analysisOnly: true, sourceValidation: 'current', citations: [], actions: []}; break;
    case 'a09-relay-pdf':
    case 'a09-relay-image':
      sent(s, 'queued', ['plan', 'shot']); configureRelay(s);
      s.preview = {fileId: id(key === 'a09-relay-pdf' ? 'plan' : 'shot'), kind: 'fallback', location: null}; break;
    case 'a09-pdf':
      officer(s, true); s.preview = {fileId: id('plan'), kind: 'pdf', location: {page: 2}, pageCount: 6, lineCount: null,
        content: '2. Rollout\n\nThe new rounding goes to the catalogue first, then to checkout. Each step is checked against the pricing table before the next one starts.\n\nPrices are rounded on each cart line, never on the total. A four-item cart must show the same total in the table and at checkout.\n\nRollout starts once the pricing suite passes and one review is complete. No date is fixed in this plan.', unavailableReason: null}; break;
    case 'a09-image':
      sent(s, 'answered', ['plan', 'shot']); s.preview = {fileId: id('shot'), kind: 'image', location: null,
        pageCount: null, lineCount: null, content: 'Fictional Orchard checkout: four seasonal items at $16.99 each; total $67.96.', unavailableReason: null}; break;
    case 'a09-text':
      officer(s, true); s.preview = {fileId: id('req'), kind: 'text', location: {lineStart: 10, lineEnd: 17}, pageCount: null, lineCount: 21,
        content: ['# Requirements', '', '## Pricing', '', '- Show the seasonal price on every catalogue card.', '- Keep the list price visible, struck through.', '', '## Rounding', '', 'Round each cart line to the cent.', 'Never round the cart total.', '', 'The table and the checkout must use', 'the same shared pricing function.', '', 'A four-item cart must give the same', 'total in both places.', '', '## Out of scope', '', '- Currency conversion.'].join('\n'), unavailableReason: null}; break;
    case 'a09-fallback':
      sent(s, 'answered', ['plan', 'long']); s.preview = {fileId: id('long'), kind: 'fallback', location: null,
        pageCount: null, lineCount: null, content: null, unavailableReason: 'This file cannot be shown here. Its pages could not be drawn in this window. The file itself is unchanged and is still available to Ada.'}; break;
    case 'a10-files': history(s); break;
    case 'a10-reattach':
      setDraft(s, ['big'], ''); s.files[0].id = 'fixture-file-big-reattached'; s.draft.fileIds = [s.files[0].id];
      s.draft.id = 'fixture-draft-ada-reattached';
      s.draft.notice = 'Attached again as a new draft for Ada. Nothing is sent until you press Send.'; break;
    case 'a11-full': {
      setDraft(s, ['plan', 'long']); failFile(s, 'long', 'storage_full');
      s.storage.usedBytes = s.storage.limitBytes; s.storage.full = true; s.ui.storageOpen = true;
      const queued = sent(s, 'queued', ['plan']);
      s.destination = destination('agent', 'bram'); const unknown = sent(s, 'delivery_unknown', ['req']); s.destination = destination();
      const [shot] = addFiles(s, 'shot');
      Object.assign(shot, {availability: 'available', availableTo: 'Ada', actions: ['preview', 'delete_local_copies'], retention: {pinned: false, reason: null, expiresAt: null}});
      // Use a distinct historical identity, never treat the failed draft as an available copy.
      const historicalLong = file('long'); historicalLong.id = 'fixture-file-long-history';
      Object.assign(historicalLong, {availability: 'available', availableTo: 'Ada', actions: ['preview', 'delete_local_copies'], retention: {pinned: false, reason: null, expiresAt: null}}); s.files.push(historicalLong);
      queued.id = 'fixture-message-full-ada'; queued.requestId = 'fixture-request-full-ada';
      unknown.id = 'fixture-message-full-bram'; unknown.requestId = 'fixture-request-full-bram';
      const queuedPlan = s.files.find(f => f.id === id('plan'));
      queuedPlan.actions.push('delete_local_copies');
      const draftPlan = file('plan'); draftPlan.id = 'fixture-file-plan-new-draft'; s.files.push(draftPlan);
      s.draft.fileIds = [draftPlan.id, id('long')];
      s.files.find(f => f.id === id('req')).actions.push('check_status', 'delete_local_copies');
      s.filesList = {destination: clone(s.destination), nextCursor: null, entries: [
        ...s.messages.map(m => ({requestId: m.requestId, createdAt: m.createdAt, text: m.text, messageState: m.state, fileIds: m.fileIds.slice()})),
        {requestId: 'fixture-request-full-shot', createdAt: '2030-11-08T10:12:00.000Z', text: 'Review the attached file', messageState: 'answered', fileIds: [shot.id]},
        {requestId: 'fixture-request-full-long', createdAt: '2030-11-09T10:12:00.000Z', text: 'Review the attached file', messageState: 'answered', fileIds: [historicalLong.id]},
      ]}; break;
    }
    case 'a11-confirm': deletion(s, 'confirm'); break;
    case 'a11-progress': deletion(s, 'deleting'); break;
    case 'a11-error': deletion(s, 'partial_failure'); break;
    case 'a11-done': deletion(s, 'deleted'); break;
    default: throw new RangeError('Unknown attachment scenario.');
  }
  const sharedIds = new Set([...s.messages.flatMap(m => m.fileIds), ...(s.filesList?.entries || []).flatMap(e => e.fileIds)]);
  s.filesCount = [...sharedIds].filter(fileId => s.files.find(f => f.id === fileId)?.availability !== 'not_saved').length;
  if (key === 'a05-saved') s.filesCount = 1;
  if (key === 'a10-reattach') s.filesCount = 5;
  deriveDraft(s);
  if (s.officerAnswer?.noticeParts) {
    const {title, body} = s.officerAnswer.noticeParts;
    s.officerAnswer.notice = `${title} ${body}`;
  }
  return s;
}

// Derive presentation reasons from canonical draft/file state, never from notice text.
function deriveDraft(s) {
  const d = s.draft;
  if (!d) return;
  const files = d.fileIds.map(fileId => s.files.find(f => f.id === fileId));
  let code = null, reason = null;
  const states = {
    resume_available: ['resume_required', 'resume_available', 'Resume this draft or create a new draft.'],
    conflict: ['draft_conflict', 'draft_conflict', 'This draft was resumed in another window, so it cannot be changed here.'],
    unavailable: ['draft_unavailable', 'draft_unavailable', 'Start a new draft.'],
  };
  if (states[d.state]) {
    [code, d.noticeCode, reason] = states[d.state];
  } else if (!s.capabilities.available) {
    code = 'recipient_unavailable'; reason = s.capabilities.reason;
  } else if (files.some(f => f?.error?.code === 'recipient_unavailable')) {
    code = 'recipient_unavailable'; reason = 'Send is off until these files are fixed or removed. To send the text alone, remove them first.';
  } else if (files.some(f => f?.error?.code === 'folder_unreachable')) {
    code = 'folder_unreachable'; reason = 'Send is off until these files can be prepared for Ada. Your message and files are kept.';
  } else if (files.some(f => !f || f.error || ['failed', 'cancelled'].includes(f.preparation) || f.availability !== 'private')) {
    code = 'file_errors'; reason = files.filter(f => f?.error).length === 1 && files.some(f => f?.error?.code === 'scanned_pdf_unreadable')
      ? 'Send is off until this file is fixed or removed. To send the text alone, remove it first.'
      : 'Send is off until these files are fixed or removed. To send the text alone, remove them first.';
  } else if (files.some(f => f.preparation !== 'ready')) {
    const count = files.filter(f => f.preparation !== 'ready').length;
    code = 'files_preparing'; reason = `Send is off while ${count} ${count === 1 ? 'file is' : 'files are'} still being prepared.`;
  } else if (!d.text.trim() && files.length === 0) {
    code = 'empty'; reason = 'Write a message or attach a file.';
  }
  d.blockCode = code;
  d.sendBlockedReason = reason;
  d.canSend = d.editable && d.state === 'editing' && code === null;
}

// A command is a small JSON value. Reject accessors, prototypes and unexpected payloads
// before inspecting values; no browser File/Blob, event, callback or encoded bytes fit here.
function objectFields(value, keys, required = keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) throw new TypeError('Expected a plain command object.');
  const descriptors = Object.getOwnPropertyDescriptors(value);
  if (Reflect.ownKeys(descriptors).some(k => typeof k !== 'string' || !keys.includes(k) || !Object.hasOwn(descriptors[k], 'value'))
      || required.some(k => !Object.hasOwn(descriptors, k))) throw new TypeError('Unsupported or missing command field.');
}
function exactDestination(actual, expected) {
  objectFields(actual, ['kind', 'agentId', 'label', 'projectId', 'projectLabel', 'scope', 'modelId']);
  objectFields(actual.scope, ['kind', 'agentId']);
  return ['kind', 'agentId', 'label', 'projectId', 'projectLabel', 'modelId'].every(k => actual[k] === expected[k])
    && actual.scope.kind === expected.scope.kind && actual.scope.agentId === expected.scope.agentId;
}
const COMMAND_FIELDS = Object.freeze({
  open_picker: [], close_picker: [], add_sample_files: ['fileIds'], remove_file: ['fileId'],
  cancel_upload: ['fileId'], show_details: ['fileId'], close_details: [], edit_text: ['text'], send_sample: [],
  retry_preparation: ['fileId'], resume_draft: [], create_draft: [],
});
const MESSAGE_ACTIONS = Object.freeze(['check_status', 'finish_send', 'delete_saved_copies']);
const MESSAGE_BINDING_FIELDS = ['type', 'messageId', 'requestId', 'destination', 'revision', 'operationGeneration'];
const BINDING_FIELDS = ['type', 'draftId', 'destination', 'revision', 'writerGeneration'];

/** Values only: explicit gate and query, no browser-global fallback or production adapter. */
export function createAttachmentsFixture(options = {}) {
  const enabled = options.enabled === true && typeof options.search === 'string'
    && new URLSearchParams(options.search).has('demo');
  const initial = options.scenario === undefined ? 'a01-agent' : options.scenario;
  if (!ATTACHMENT_SCENARIOS.includes(initial)) throw new RangeError('Unknown attachment scenario.');
  let generation = 1, sequence = 0;
  let state = base(false);
  // Canonical records are initialized on selection, then changed only by accepted actions.
  // Snapshot reads clone cached projections and never allocate authority or advance work.
  const instanceId = `fixture-instance-${++attachmentInstanceSequence}`;
  let model = null, receiptSequence = 0;
  const mint = prefix => `${instanceId}-${prefix}-${++generation}`;
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const activeDestination = dest => same(dest, state.destination);
  const requestOwner = r => ({kind: 'request', requestId: r.requestId, messageId: r.message?.id || null,
    destination: clone(r.destination), fileIds: r.fileIds.slice()});
  const draftOwner = () => ({kind: 'draft', draftId: state.draft.id, writerGeneration: state.draft.writerGeneration,
    destination: clone(state.draft.destination), fileIds: state.draft.fileIds.slice()});
  const draftFence = () => ({draftId: state.draft.id, revision: state.draft.revision,
    writerGeneration: state.draft.writerGeneration, destination: clone(state.draft.destination)});
  const allFiles = () => [...state.files, ...model.inventoryFiles];
  const findFile = fileId => state.files.find(f => f.id === fileId) || model.inventoryFiles.find(f => f.id === fileId);
  const writable = () => state.draft?.editable && state.draft.state === 'editing' && state.capabilities.available;
  const contextAccess = () => ({kind: 'context', id: model.contextId, generation: model.contextGeneration});
  function ownerFor(fileId) {
    if (writable() && state.draft.fileIds.includes(fileId)) return draftOwner();
    const r = model.requests.find(r => r.fileIds.includes(fileId) && activeDestination(r.destination));
    return r ? requestOwner(r) : null;
  }
  function sourceTarget(fileId, owner, access) {
    return {owner: clone(owner), fileId, sourceVersion: model.sources.get(fileId).version, access: clone(access)};
  }
  function authority(kind, target, actions, revision = 1) {
    const key = JSON.stringify([kind, target, actions, revision]);
    let a = model.authorityCache.get(key);
    if (!a) {
      a = {instanceId, kind, id: mint(kind), revision, generation: ++generation, actions: actions.slice(), target: clone(target)};
      model.authorityCache.set(key, a);
    }
    model.authorities.set(a.id, a);
    return a;
  }
  function addRequest(message, entry = null, dest = state.destination) {
    const requestId = message?.requestId || entry.requestId;
    const existing = model.requests.find(r => r.requestId === requestId);
    if (existing) { if (message) existing.message = message; return existing; }
    const r = {entryId: `fixture-entry-${requestId}`, requestId, destination: clone(message?.destination || dest),
      fileIds: (message?.fileIds || entry.fileIds).slice(), message, text: message?.text ?? entry.text,
      rank: model.requests.length, createdAt: message?.createdAt || entry.createdAt, messageState: message?.state || entry.messageState, counted: new Set()};
    for (const fileId of r.fileIds) if (findFile(fileId)?.availability !== 'not_saved') r.counted.add(fileId);
    model.requests.push(r); return r;
  }
  function addCopy(f, kind, outcome = 'deleted') {
    const copy = {id: mint('copy'), fileId: f.id, kind, identityGeneration: ++generation, bytes: f.bytes,
      owned: true, state: 'present', reason: null};
    model.copies.push(copy); model.outcomes.set(copy.id, outcome); return copy;
  }
  function copyAllocations(files, kinds, fresh = false) {
    const allocations = [];
    for (const f of files) for (const kind of kinds) {
      if (fresh || !model.copies.some(c => c.fileId === f.id && c.kind === kind && c.state !== 'deleted'))
        allocations.push({fileId: f.id, kind, bytes: f.bytes});
    }
    return allocations;
  }
  function hasCopyCapacity(allocations) {
    const bytes = allocations.reduce((n, c) => n + c.bytes, 0);
    return Number.isSafeInteger(bytes) && bytes >= 0 && state.storage.usedBytes <= state.storage.limitBytes - bytes;
  }
  function requireCopyCapacity(allocations) {
    if (!hasCopyCapacity(allocations)) throw new Error('Not enough attachment storage for these copies.');
  }
  function deliveryAllocations(files, destination) {
    return copyAllocations(files, destination.kind === 'agent' ? ['stored', 'workspace'] : ['stored']);
  }
  function retainCurrentDraft() {
    const d = state.draft;
    if (!d) return;
    if (d.fileIds.length && d.state !== 'unavailable') model.draftBindings.set(d.id, clone(d));
    else model.draftBindings.delete(d.id);
  }
  function hasRetainedCopy(fileId) { return model.copies.some(c => c.fileId === fileId && c.state !== 'deleted'); }
  function retainedIds() { return [...new Set(model.copies.filter(c => c.state !== 'deleted').map(c => c.fileId))].sort(); }
  function committedCleanup(fileId) { return model.deletions.some(o => o.fileIds.includes(fileId) && ['deleting', 'partial_failure', 'unknown', 'deleted'].includes(o.state)); }
  function finishedRequest(r) {
    const status = r.message?.state || r.messageState;
    return status === 'answered' || status === 'retained_local'
      || status === 'failed' && r.message?.errorCode === 'provider_rejected'
      || status === 'not_sent' && r.message?.deliveryPhase === 'cleanup_fenced';
  }
  function protectedFile(fileId) {
    if ([...model.draftBindings.values()].some(d => d.fileIds.includes(fileId))) return true;
    const refs = model.requests.filter(r => r.fileIds.includes(fileId));
    return !refs.length || refs.some(r => !finishedRequest(r));
  }
  function projectPolicy(f) {
    if (!model.policies.has(f.id)) model.policies.set(f.id, expiryPolicy());
    const p = model.policies.get(f.id);
    if (p.date && !p.operationId) p.state = p.deadlineAt <= model.now && protectedFile(f.id) ? 'deferred' : 'scheduled';
    f.retention.expiresAt = p.deadlineAt;
    f.retention.expiryPolicy = clone(p);
  }
  function materializeInventory() {
    let remaining = model.backgroundBytes, index = 0;
    while (remaining) {
      if (++index > 500) throw new Error('Fictional inventory exceeds its bounded record limit.');
      const paired = remaining >= 2, bytes = paired ? Math.min(10 * MiB, Math.floor(remaining / 2)) : remaining;
      const f = file('table');
      Object.assign(f, {id: `fixture-inventory-${index}`, name: `retained-reference-${String(index).padStart(3, '0')}.txt`, bytes,
        availability: 'available', availableTo: paired ? 'Archive agent' : null, previewable: true, actions: ['preview']});
      model.inventoryFiles.push(f);
      const dest = destination(paired ? 'agent' : 'officer', 'fixture-archive');
      dest.label = paired ? 'Archive agent' : 'Motet'; dest.projectId = 'fixture-archive'; dest.projectLabel = 'Sample archive';
      const r = addRequest(null, {requestId: `fixture-inventory-request-${index}`, fileIds: [f.id], text: 'Retained fictional reference.', createdAt: NOW, messageState: 'answered'}, dest);
      r.inventory = true;
      addCopy(f, 'stored'); if (paired) addCopy(f, 'workspace');
      remaining -= bytes * (paired ? 2 : 1);
    }
    model.backgroundBytes = 0;
  }
  function expirySelection(ids) {
    return ids.map(fileId => ({fileId, sourceVersion: model.sources.get(fileId).version,
      policyGeneration: model.policies.get(fileId).generation,
      copies: model.copies.filter(c => c.fileId === fileId && c.state !== 'deleted').map(c => ({id: c.id, generation: c.identityGeneration, bytes: c.bytes, state: c.state}))}));
  }
  function currentExpiryIds(d) { return d.mode === 'all' ? retainedIds() : d.fileIds.filter(hasRetainedCopy); }
  function captureExpiry(d, changed = false) {
    d.fileIds = currentExpiryIds(d); d.selected = expirySelection(d.fileIds);
    d.selectionGeneration++; d.revision++;
    d.state = changed ? 'selection_changed' : 'editing';
  }
  function openExpiry(mode, fileId = null) {
    const d = {id: mint('expiry-dialog'), revision: 1, mode, selectionId: mint('expiry-selection'), selectionGeneration: 1,
      storageId: model.views.storage.id, storageGeneration: model.views.storage.generation,
      fileIds: mode === 'all' ? retainedIds() : [fileId], state: 'editing', date: null};
    d.selected = expirySelection(d.fileIds);
    const dates = [...new Set(d.fileIds.map(id => model.policies.get(id).date).filter(Boolean))];
    d.date = dates.length === 1 ? dates[0] : model.now.slice(0, 10) > '2030-11-30' ? model.now.slice(0, 10) : '2030-11-30';
    model.expiryDialog = d;
  }
  function expiryProjection() {
    const d = model.expiryDialog;
    if (!d) return null;
    if (!model.views.storage || model.views.storage.id !== d.storageId || model.views.storage.generation !== d.storageGeneration) { model.expiryDialog = null; return null; }
    // A captured selection cannot regain date-editing rights when its cleanup
    // finishes. Bulk Check again explicitly recaptures surviving files; a single
    // removed file remains Close-only until its dialog is closed.
    const blocked = d.fileIds.filter(committedCleanup).length;
    const chosen = d.fileIds.filter(id => model.policies.get(id)?.date).length;
    const canSet = !!d.fileIds.length && !blocked, canClear = canSet && chosen > 0;
    const stateName = blocked ? 'removal_started' : d.state;
    const name = d.mode === 'single' ? findFile(d.fileIds[0])?.name || 'this file' : null;
    const bytes = d.selected.reduce((n, row) => n + row.copies.reduce((v, c) => v + c.bytes, 0), 0);
    const actions = ['close_expiry', ...(d.mode === 'all' ? ['refresh_expiry_selection'] : []), ...(canSet ? ['set_expiry'] : []), ...(canClear ? ['clear_expiry'] : [])];
    let notice = d.state === 'selection_changed' ? 'The files stored have changed since you opened this. Check the new total, then set the date again.' : null;
    if (blocked) notice = d.mode === 'single' ? 'Removal has already started for this file, so its date can no longer be changed.'
      : `Removal has already started for ${blocked} of these files, so a date cannot be set for all files right now. Check again when ${blocked === 1 ? 'it has' : 'they have'} finished.`;
    const summary = chosen ? `${chosen} of these files already ${chosen === 1 ? 'has' : 'have'} a date. Setting a date here replaces it; Remove the date clears it from every file. ` : '';
    return {authority: authority('expiry_dialog', {viewId: d.id, selectionId: d.selectionId, selectionGeneration: d.selectionGeneration, storageId: d.storageId, storageGeneration: d.storageGeneration}, actions, d.revision),
      state: stateName, mode: d.mode, selectionId: d.selectionId, selectionGeneration: d.selectionGeneration, fileIds: d.fileIds.slice(),
      selected: d.selected.map(({fileId, sourceVersion, policyGeneration}) => ({fileId, sourceVersion, policyGeneration})),
      fileCount: d.fileIds.length, accountedBytes: bytes, datedFileCount: chosen, cleanupStartedCount: blocked,
      date: d.date, minimumDate: model.now.slice(0, 10), timeZone: SAMPLE_ZONE, canSet, canClear,
      reasonCode: blocked ? 'cleanup_started' : d.state === 'selection_changed' ? 'selection_changed' : null, notice,
      title: d.mode === 'single' ? `Set an expiry date for ${name}` : 'Set an expiry date for all files',
      description: d.mode === 'single' ? 'On that date Motet deletes its local copies of this file, the same as Delete local copies.' : 'On that date Motet deletes its local copies of every file stored now.',
      detail: d.mode === 'single' ? 'The file is removed at the end of that day, by this computer’s clock. You can change or remove the date before then.'
        : summary + 'Files are removed at the end of that day, by this computer’s clock. A file whose message is still unfinished is kept until the message finishes. Files you attach later are not affected. You can change or remove the date before then.'};
  }
  function expiryAction(type, auth, command) {
    let outcome = type;
    if (type === 'open_expiry') openExpiry('single', auth.target.fileId);
    else if (type === 'open_expiry_all') openExpiry('all');
    else if (type === 'close_expiry') model.expiryDialog = null;
    else {
      const d = model.expiryDialog;
      if (type === 'set_expiry') {
        sampleDeadline(command.date);
        if (command.date < model.now.slice(0, 10)) throw new Error('Choose today or a future date.');
      }
      const current = currentExpiryIds(d);
      if (current.some(committedCleanup)) { d.state = 'removal_started'; d.revision++; outcome = 'cleanup_started'; }
      else if (type === 'refresh_expiry_selection') { captureExpiry(d); outcome = 'selection_refreshed'; }
      else if (!same(current, d.fileIds) || !same(expirySelection(current), d.selected)) { if (type === 'set_expiry') d.date = command.date; captureExpiry(d, true); outcome = 'selection_changed'; }
      else {
        for (const fileId of d.fileIds) {
          const p = model.policies.get(fileId), next = expiryPolicy(); next.generation = p.generation + 1;
          if (type === 'set_expiry') Object.assign(next, {date: command.date, timeZone: SAMPLE_ZONE, deadlineAt: sampleDeadline(command.date), state: 'scheduled'});
          model.policies.set(fileId, next);
        }
        outcome = type === 'set_expiry' ? 'expiry_set' : 'expiry_cleared'; model.expiryDialog = null;
      }
    }
    model.receipt = {sequence: ++receiptSequence, type, authorityId: auth.id, outcome};
    refresh(); return clone(state);
  }
  function expiryOwner(fileId) {
    const r = model.requests.find(r => r.fileIds.includes(fileId));
    return r ? requestOwner(r) : null;
  }
  function evaluateExpiry() {
    retainCurrentDraft();
    for (const fileId of retainedIds()) {
      const p = model.policies.get(fileId);
      if (!p?.date || p.deadlineAt > model.now || p.operationId || committedCleanup(fileId)) continue;
      if (protectedFile(fileId)) { p.state = 'deferred'; continue; }
      const owner = expiryOwner(fileId);
      if (!owner) { p.state = 'deferred'; continue; }
      const cause = {kind: 'owner_expiry', policyGeneration: p.generation, date: p.date, timeZone: p.timeZone, deadlineAt: p.deadlineAt, deferred: p.state === 'deferred'};
      const op = createDeletion(sourceTarget(fileId, owner, contextAccess()), cause, false);
      confirmDeletion(op);
    }
  }
  function expiredAnswerNotice(op) {
    const answer = state.officerAnswer;
    if (!answer || ![...model.answerDependencies].some(fileId => op.fileIds.includes(fileId))) return;
    const f = findFile(op.fileIds[0]), date = sampleDateLabel(op.cause.date), removed = sampleDateLabel(model.now.slice(0, 10));
    const why = op.cause.deferred ? `was removed on ${removed}; its expiry date was ${date}`
      : model.now <= op.cause.deadlineAt ? `expired on ${date}, the date you set` : `was removed on ${removed}; its expiry date was ${date}`;
    answer.noticeParts = {title: 'This answer is no longer shown.', body: `It used ${f.name}, which ${why}. To get an answer again, attach the file and ask again.`};
    answer.notice = `${answer.noticeParts.title} ${answer.noticeParts.body}`;
  }
  function simulateExpiry(command) {
    if (!enabled) throw new Error('Attachments are unavailable.');
    const fields = {advance_clock: ['at'], evaluate_expiry: [], complete_request: ['requestId']};
    if (!command || !Object.hasOwn(fields, command.type)) throw new Error('Unknown fictional checkpoint.');
    objectFields(command, ['type', ...fields[command.type]]); plainTree(command);
    if (command.type === 'advance_clock') {
      if (typeof command.at !== 'string' || !/^2030-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/.test(command.at)
        || !Number.isFinite(Date.parse(command.at)) || new Date(command.at).toISOString() !== command.at || command.at < model.now) throw new Error('Choose a monotonic authored time in 2030.');
      model.now = command.at;
    }
    if (command.type === 'complete_request') {
      const r = model.requests.find(r => r.requestId === command.requestId);
      if (!r || finishedRequest(r)) throw new Error('Choose an unfinished fictional request.');
      r.messageState = 'answered';
      if (r.message) { r.message.state = 'answered'; r.message.deliveryPhase = 'dispatched'; r.message.statusText = 'Answered'; r.message.actions = []; r.message.recovery = {canCheck: false, canFinish: false, canDeleteSavedCopies: false}; r.message.revision++; }
      for (const fileId of r.fileIds) if (!protectedFile(fileId)) { const f = findFile(fileId); f.retention.pinned = false; f.retention.reason = null; }
    }
    evaluateExpiry(); reconcileSources(); refresh(); return clone(state);
  }
  function initializeExpiryScenario(key) {
    model.views.files = key === 'a10-files-expiry' ? newView('history') : null;
    model.views.storage = key === 'a10-files-expiry' ? null : newView('storage');
    refresh();
    const choose = (mode, fileId, date) => {
      if (!model.views.storage) model.views.storage = newView('storage');
      openExpiry(mode, fileId); refresh();
      expiryAction('set_expiry', state.management.expiryDialog.authority, {date});
    };
    if (['a11-storage-set', 'a11-expiry-set', 'a10-files-expiry'].includes(key)) choose('single', id('shot'), '2030-11-30');
    if (['a11-expiry-all-set', 'a11-storage-all-set'].includes(key)) choose('all', null, '2030-11-30');
    if (key === 'a11-expiry' || key === 'a11-expiry-set') openExpiry('single', id('shot'));
    if (key === 'a11-expiry-all' || key === 'a11-expiry-all-set') openExpiry('all');
    if (key === 'a11-expiry-selection-changed') {
      openExpiry('all'); refresh(); model.policies.get(id('shot')).generation++;
      expiryAction('set_expiry', state.management.expiryDialog.authority, {date: '2030-11-30'});
    }
    if (['a10-files-expiry', 'a11-expiry-removal-started', 'a11-expiry-partial', 'a11-expiry-unconfirmed'].includes(key)) {
      model.now = '2030-11-09T15:58:00.000Z'; choose('single', id('table'), '2030-11-10');
      model.now = '2030-11-11T00:00:00.000Z'; evaluateExpiry();
      const op = model.deletions.find(o => o.fileIds.includes(id('table')) && o.cause.kind === 'owner_expiry');
      if (key === 'a11-expiry-removal-started') openExpiry('single', id('table'));
      else {
        if (key === 'a11-expiry-partial' || key === 'a11-expiry-unconfirmed') {
          const workspace = model.copies.find(c => c.fileId === id('table') && c.kind === 'workspace');
          model.outcomes.set(workspace.id, key === 'a11-expiry-partial' ? 'identity_changed' : 'unknown');
          replaceDeleteDialog(op);
        }
        advanceDeletion(op);
      }
      model.now = NOW;
      if (key === 'a10-files-expiry') { model.views.storage = null; model.expiryDialog = null; }
    }
    model.receipt = null; state.lastAction = null; refresh();
  }

  function reconcileSources() {
    const current = new Set(allFiles().map(f => f.id));
    for (const [fileId, source] of model.sources) if (!current.has(fileId) && source.signature !== 'removed') {
      source.signature = 'removed'; source.version++; source.deleted = true;
    }
    for (const f of allFiles()) {
      const owner = ownerFor(f.id);
      const signature = JSON.stringify([f.availability, f.preparation, f.preparationKind, f.previewable, f.error, f.officerDelivery, owner]);
      const old = model.sources.get(f.id);
      if (!old) model.sources.set(f.id, {version: 1, signature, key: model.fileKeys.get(f.id) || null, pending: false});
      else if (old.signature !== signature) { old.version++; old.signature = signature; }
    }
    if (state.preview) {
      const target = state.preview.authority.target, source = model.sources.get(target.source.fileId);
      if (!source || source.version !== target.source.sourceVersion
          || target.citationTarget && (!state.officerAnswer || state.officerAnswer.state !== 'available'
            || state.officerAnswer.id !== target.citationTarget.answerId || state.officerAnswer.revision !== target.citationTarget.answerRevision)) state.preview = null;
    }
  }
  function reconcileLegacy() {
    for (const message of state.messages) {
      const exists = model.requests.some(r => r.requestId === message.requestId);
      const r = addRequest(message);
      if (!exists) r.rank = --model.historyRank;
    }
    for (const r of model.requests) if (r.message) {
      r.messageState = r.message.state;
      for (const fileId of r.fileIds) if (findFile(fileId)?.availability === 'available') r.counted.add(fileId);
    }
    for (const f of allFiles()) {
      if (!model.sources.has(f.id)) {
        // Selected identities retain their explicitly selected sample key, never a filename lookup.
        const key = Object.keys(FILES).find(k => f.id.startsWith(`${id(k)}-selected-`));
        if (key) model.fileKeys.set(f.id, key);
        if (f.availability === 'private' && f.preparation === 'ready' && !f.error) addCopy(f, 'stored');
      }
      if (f.preparation === 'ready' && !f.error && ['private', 'available', 'not_saved'].includes(f.availability)
        && !model.copies.some(c => c.fileId === f.id && c.kind === 'stored' && c.state !== 'deleted')) addCopy(f, 'stored');
      if (f.availability === 'available' && f.availableTo && !model.copies.some(c => c.fileId === f.id && c.kind === 'workspace' && c.state !== 'deleted')) addCopy(f, 'workspace');
      if (f.availability === 'not_saved') for (const c of model.copies) if (c.fileId === f.id && c.kind === 'workspace') c.state = 'deleted';
    }
    for (const c of model.copies) if (!findFile(c.fileId)) c.state = 'deleted';
    reconcileSources(); refresh();
  }
  function sourceActions(f, access, owner) {
    const source = model.sources.get(f.id), result = [];
    const readable = !source.pending && !source.deleted && f.previewable && !f.error
      && ['private', 'available'].includes(f.availability)
      && (owner.kind !== 'draft' || writable() && same(owner, draftOwner()));
    if (readable) result.push('open_preview');
    if (access.kind !== 'context' && owner.kind === 'request' && !source.pending
      && model.copies.some(c => c.fileId === f.id && ['present'].includes(c.state))) result.push('request_delete');
    if (access.kind === 'storage' && hasRetainedCopy(f.id) && (model.policies.get(f.id)?.date || !protectedFile(f.id))) result.push('open_expiry');
    if (access.kind === 'history' && writable() && activeDestination(owner.destination)
      && ['expired', 'deleted', 'missing'].includes(f.availability)) result.push('begin_reattach');
    return result;
  }
  function ref(fileId, owner, view = null) {
    const f = findFile(fileId), source = model.sources.get(fileId);
    const access = view ? {kind: view.kind, id: view.id, generation: view.generation} : null;
    return {fileId, sourceVersion: source.version, authority: access
      ? authority('source', sourceTarget(fileId, owner, access), sourceActions(f, access, owner)) : null};
  }
  function entry(r, view = null, global = false) {
    const value = {entryId: r.entryId, requestId: r.requestId, messageId: r.message?.id || null,
      destination: clone(r.destination), text: r.text, createdAt: r.createdAt, messageState: r.message?.state || r.messageState,
      fileIds: r.fileIds.slice(), fileRefs: r.fileIds.map(fileId => ref(fileId, requestOwner(r), view))};
    if (global) value.recoveryMessage = null;
    if (global && r.message?.state === 'delivery_unknown' && r.message.actions.includes('check_status')) {
      const m = r.message;
      value.recoveryMessage = clone(m);
    }
    return value;
  }
  function viewAuthority(kind, view) {
    const target = {contextId: model.contextId, contextGeneration: model.contextGeneration,
      viewId: view?.id || null, viewGeneration: view?.generation || null};
    if (kind === 'history') target.destination = clone(state.destination);
    return authority(kind, target, view ? kind === 'history' ? ['close_files', 'open_storage'] : ['close_storage', 'storage_inventory_page', ...(retainedIds().length ? ['open_expiry_all'] : [])] : []);
  }
  function deletionAuthority(op) {
    const actions = op.state === 'confirm' ? ['confirm_delete'] : op.state === 'deleting'
      ? ['advance_sample_deletion', 'open_delete_result'] : op.state === 'abandoned' ? [] : ['open_delete_result'];
    return authority('deletion', {operationId: op.id, owner: op.owner, fileIds: op.fileIds,
      selectedSourceVersions: op.selectedSourceVersions}, actions, op.revision);
  }
  function publicOperation(op) {
    return {id: op.id, authority: deletionAuthority(op), state: op.state, fileIds: op.fileIds.slice(),
      cause: clone(op.cause), requestId: op.owner.requestId, destination: clone(op.owner.destination), pinnedWarning: op.pinnedWarning,
      removes: op.removes.slice(), retains: op.retains.slice(), copies: op.copyIds.map(copyId => clone(model.copies.find(c => c.id === copyId))),
      releasedBytes: op.releasedBytes, resultText: op.resultText};
  }
  function refresh() {
    model.authorities = new Map();
    retainCurrentDraft();
    for (const f of allFiles()) {
      projectPolicy(f);
      f.previewAvailable = !['original_relay', 'officer_direct'].includes(f.preparationKind) && f.previewable && !f.error
        && ['plan', 'shot', 'req'].includes(model.fileKeys.get(f.id));
      f.preparationText = f.preparation === 'ready' && f.availability === 'private' && !f.error
        ? ['original_relay', 'officer_direct'].includes(f.preparationKind) ? RELAY_READY : 'Ready to share. It stays private until you press Send.' : null;
      if (f.preparationKind === 'original_relay') f.usedInAnswer = false;
      if (f.preparationKind === 'officer_direct') f.usedInAnswer = state.officerAnswer?.state === 'available'
        && state.officerAnswer.citations.some(c => c.available && c.fileId === f.id) || false;
    }
    const active = model.requests.filter(r => activeDestination(r.destination));
    state.messages = active.filter(r => r.message).map(r => r.message);
    state.filesCount = active.reduce((n, r) => n + r.counted.size, model.unlisted);
    const accounted = model.copies.filter(c => c.state !== 'deleted').reduce((n, c) => n + c.bytes, 0);
    state.storage.usedBytes = model.backgroundBytes + accounted;
    state.storage.full = state.storage.usedBytes >= state.storage.limitBytes;
    deriveDraft(state);
    if (state.draft.canSend && !hasCopyCapacity(deliveryAllocations(state.draft.fileIds.map(fileId => state.files.find(f => f.id === fileId)), state.draft.destination))) {
      state.draft.canSend = false;
      state.draft.blockCode = 'storage_capacity';
      state.draft.sendBlockedReason = 'Send is off until attachment storage has room for these files. Open Files to free some.';
    }
    const context = authority('context', {destination: state.destination, contextId: model.contextId,
      contextGeneration: model.contextGeneration}, [...(!model.unlisted ? ['open_files'] : []), 'open_storage']);
    const orderedHistory = active.slice().sort((a, b) => a.rank - b.rank);
    const historyRows = orderedHistory.map(r => entry(r, model.views.files));
    // Keep unfinished requests together; finished samples are oldest recorded request first.
    // These authored dates are not a claim of observed runtime completion timestamps.
    const storageRequests = model.requests.filter(r => !r.inventory).sort((a, b) => {
      const aDone = finishedRequest(a);
      const bDone = finishedRequest(b);
      if (aDone !== bDone) return aDone ? 1 : -1;
      if (!aDone) return a.rank - b.rank;
      return a.createdAt.localeCompare(b.createdAt) || a.requestId.localeCompare(b.requestId);
    });
    const storageRows = storageRequests.map(r => entry(r, model.views.storage, true));
    const sourceRefs = [];
    for (const f of state.files) {
      const owner = ownerFor(f.id);
      if (!owner || owner.kind === 'request' && !state.messages.some(m => m.fileIds.includes(f.id))) continue;
      const target = sourceTarget(f.id, owner, contextAccess()), actions = sourceActions(f, target.access, owner).filter(a => a === 'open_preview');
      if (actions.length) sourceRefs.push({fileId: f.id, sourceVersion: target.sourceVersion, authority: authority('source', target, actions)});
    }
    if (state.officerAnswer) {
      const answer = state.officerAnswer;
      for (const c of answer.citations) {
        if (c.kind === 'activity') { c.navigation = {kind: 'unavailable', reason: answer.state === 'source_unavailable' ? 'source_unavailable' : 'fixture_activity_only'}; continue; }
        const r = model.requests.find(r => r.message?.id === answer.replyTo?.messageId && r.requestId === answer.replyTo?.requestId);
        const f = state.files.find(f => f.id === c.fileId), source = model.sources.get(c.fileId);
        if (answer.state !== 'available' || !r || !f || !c.available || !sourceActions(f, contextAccess(), requestOwner(r)).includes('open_preview')) {
          c.navigation = {kind: 'unavailable', reason: 'source_unavailable'}; continue;
        }
        const citationTarget = {answerId: answer.id, answerRevision: answer.revision, citationId: c.id,
          owner: requestOwner(r), fileId: f.id, sourceVersion: source.version, location: clone(c.location), access: contextAccess()};
        if (!(f.preparationKind === 'officer_direct' ? (f.officerDelivery?.state === 'confirmed' && (f.mediaType === 'application/pdf'
          ? Object.keys(c.location || {}).length === 1 && Number.isSafeInteger(c.location.page) && c.location.page > 0 && c.location.page <= 1000000
          : f.mediaType === 'image/png' && Object.keys(c.location || {}).length === 1 && c.location.image === true)) : authoredLocation(source.key, c.location))) { c.navigation = {kind: 'unavailable', reason: 'phase4_file_preview'}; continue; }
        c.navigation = {kind: 'preview', authority: authority('citation', citationTarget, ['open_citation']), citationTarget};
      }
    }
    if (state.preview) model.authorities.set(state.preview.authority.id, state.preview.authority);
    let picker = null;
    if (model.picker) {
      const p = model.picker;
      // Draft edits refresh the exposed picker fence without reviving any old callback.
      if (!model.views.files || model.views.files.id !== p.historyViewId || model.views.files.generation !== p.historyViewGeneration
        || model.sources.get(p.historicalSource.fileId)?.version !== p.historicalSource.sourceVersion || !writable()) model.picker = null;
      else picker = {authority: authority('reattach_picker', {viewId: p.id, historyViewId: p.historyViewId,
        historyViewGeneration: p.historyViewGeneration, historicalSource: p.historicalSource, draftFence: draftFence()},
        ['cancel_reattach', 'select_reattach_sample']), sampleChoices: ['plan', 'shot', 'req', 'table'].map(k => {
          const {id: sampleId, name, mediaType, bytes} = selectedSample(state, k);
          return {sampleId, name, mediaType, bytes};
        })};
    }
    const deletions = model.deletions.map(publicOperation);
    state.deleteDialog = null;
    if (model.deleteView) {
      const op = model.deletions.find(o => o.id === model.deleteView.operationId);
      state.deleteDialog = {...publicOperation(op), viewAuthority: authority('delete_dialog', {
        viewId: model.deleteView.id, operationId: op.id, operationRevision: op.revision}, op.state === 'confirm' ? ['keep_file', 'close_delete'] : ['close_delete'])};
    }
    const views = {files: model.views.files ? {authority: viewAuthority('history', model.views.files), parent: null} : null,
      storage: model.views.storage ? {authority: viewAuthority('storage', model.views.storage), parent: clone(model.views.storage.parent)} : null};
    state.management = {instanceId, sourceRefs, context, history: {authority: viewAuthority('history', null), destination: clone(state.destination),
      entries: historyRows, complete: !model.unlisted, unlistedAssociationCount: model.unlisted},
      storage: {authority: viewAuthority('storage', null), entries: storageRows, backgroundBytes: model.backgroundBytes,
        draftEntries: [...model.draftBindings.values()].filter(d => d.fileIds.some(hasRetainedCopy)).map(d => {
          const owner = {kind: 'draft', draftId: d.id, writerGeneration: d.writerGeneration, destination: clone(d.destination), fileIds: d.fileIds.slice()};
          return {entryId: 'fixture-draft-entry-' + d.id, draftId: d.id, destination: clone(d.destination), text: d.text,
            state: d.state, fileIds: d.fileIds.slice(), fileRefs: d.fileIds.map(fileId => ref(fileId, owner, model.views.storage))};
        }),
        inventoryComplete: true, inventoryFileCount: retainedIds().length, inventoryFiles: clone(model.inventoryFiles),
        inventoryPage: {offset: model.inventoryOffset, pageSize: INVENTORY_PAGE_SIZE, total: model.requests.filter(r => r.inventory).length,
          previousOffset: model.inventoryOffset ? Math.max(0, model.inventoryOffset - INVENTORY_PAGE_SIZE) : null,
          nextOffset: model.inventoryOffset + INVENTORY_PAGE_SIZE < model.requests.filter(r => r.inventory).length ? model.inventoryOffset + INVENTORY_PAGE_SIZE : null,
          entries: model.requests.filter(r => r.inventory).slice(model.inventoryOffset, model.inventoryOffset + INVENTORY_PAGE_SIZE).map(r => entry(r, model.views.storage, true))}},
      sampleClock: {now: model.now, timeZone: SAMPLE_ZONE, today: model.now.slice(0, 10)}, expiryDialog: expiryProjection(),
      deletions, reattachPicker: picker, views, actionReceipt: clone(model.receipt)};
    state.ui.storageOpen = !!model.views.storage;
    state.filesList = model.historyShown || model.views.files ? {destination: clone(state.destination), nextCursor: null,
      entries: orderedHistory.map(r => ({requestId: r.requestId, createdAt: r.createdAt, text: r.text,
        messageState: r.message?.state || r.messageState, fileIds: r.fileIds.slice()}))} : null;
  }
  function initializePhase4(key) {
    const seededPreview = state.preview, seededDialog = state.deleteDialog;
    state.preview = null; state.deleteDialog = null;
    model = {contextId: mint('context'), contextGeneration: ++generation, requests: [], sources: new Map(), fileKeys: new Map(),
      officerRetainedPrior: new Map((state.officerRetainedPrior || []).map(r => [`${r.requestId}:${r.fileId}`, r.prior])),
      copies: [], outcomes: new Map(), backgroundBytes: 0, inventoryFiles: [], inventoryOffset: 0,
      policies: new Map(), draftBindings: new Map(), expiryDialog: null, now: NOW, unlisted: key === 'a05-saved' ? 1 : 0,
      views: {files: null, storage: null}, picker: null, deletions: [], deleteView: null,
      historyShown: !!state.filesList, historyRank: 0, authorityCache: new Map(), authorities: new Map(), receipt: null};
    delete state.officerRetainedPrior;
    for (const k of Object.keys(FILES)) model.fileKeys.set(id(k), k);
    model.fileKeys.set('fixture-file-long-history', 'long'); model.fileKeys.set('fixture-file-plan-new-draft', 'plan');
    model.fileKeys.set('fixture-file-big-reattached', 'big');
    for (const message of state.messages) addRequest(message);
    for (const row of state.filesList?.entries || []) {
      const message = state.messages.find(m => m.requestId === row.requestId);
      addRequest(message || null, row, message?.destination || state.destination);
    }
    if (key === 'a10-reattach') {
      const retained = makeScenario('a10-files');
      const rows = retained.filesList.entries;
      // Five explicit associations; requirements belongs to the original manifest but is not this sample's retained row.
      rows[0].fileIds = [id('plan')];
      for (const row of rows) {
        for (const fileId of row.fileIds) if (!state.files.some(f => f.id === fileId)) state.files.push(clone(retained.files.find(f => f.id === fileId)));
        addRequest(null, row);
      }
      model.historyShown = true;
    }
    model.answerDependencies = new Set(state.officerConsumedFiles || state.officerAnswer?.citations.map(c => c.fileId).filter(Boolean) || []);
    delete state.officerConsumedFiles;
    if (state.officerAnswer) state.officerAnswer.revision = 1;
    for (const f of state.files) {
      if (!['expired', 'deleted'].includes(f.availability) && f.preparation === 'ready' && !f.error) {
        addCopy(f, 'stored');
        if (f.availableTo) addCopy(f, 'workspace');
        else if (f.availability === 'missing') {
          const copy = addCopy(f, 'workspace', 'identity_changed');
          copy.state = 'identity_changed';
          copy.reason = 'The saved workspace copy was moved or changed. Its current identity is not verified and it was left in place.';
        }
      }
    }
    model.backgroundBytes = state.storage.usedBytes - model.copies.reduce((n, c) => n + c.bytes, 0);
    if (!Number.isSafeInteger(model.backgroundBytes) || model.backgroundBytes < 0) throw new Error('Invalid fictional copy balance.');
    materializeInventory();
    reconcileSources();
    if (key === 'a10-files') model.views.files = newView('history');
    if (key === 'a11-full') model.views.storage = newView('storage');
    refresh();
    if (seededPreview) {
      const citation = state.officerAnswer?.citations.find(c => c.fileId === seededPreview.fileId && same(c.location, seededPreview.location));
      const target = citation?.navigation.kind === 'preview' ? citation.navigation.authority.target
        : state.management.sourceRefs.find(r => r.fileId === seededPreview.fileId)?.authority.target;
      if (target) openPreview(target, citation?.navigation.kind === 'preview' ? target : null, seededPreview.location);
    }
    if (seededDialog) {
      const r = model.requests.find(r => r.requestId === seededDialog.requestId);
      const op = createDeletion(sourceTarget(seededDialog.fileIds[0], requestOwner(r), contextAccess()));
      if (key !== 'a11-confirm') {
        if (key === 'a11-error') model.outcomes.set(model.copies.find(c => c.fileId === op.fileIds[0] && c.kind === 'workspace').id, 'identity_changed');
        confirmDeletion(op);
        if (['a11-error', 'a11-done'].includes(key)) advanceDeletion(op);
      }
    }
    refresh();
  }
  function newView(kind, parent = null) { return {kind, id: mint(`${kind}-view`), generation: ++generation, parent}; }
  function plainTree(value) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
    if (typeof value === 'number') { if (!Number.isSafeInteger(value)) throw new TypeError('Expected safe integer.'); return; }
    if (typeof value !== 'object' || !(Array.isArray(value) ? Object.getPrototypeOf(value) === Array.prototype : [Object.prototype, null].includes(Object.getPrototypeOf(value)))) throw new TypeError('Expected plain JSON.');
    const d = Object.getOwnPropertyDescriptors(value);
    if (Reflect.ownKeys(d).some(k => typeof k !== 'string' || !Object.hasOwn(d[k], 'value') || !d[k].enumerable && !(Array.isArray(value) && k === 'length'))) throw new TypeError('Expected plain JSON fields.');
    if (Array.isArray(value) && (Object.keys(d).length !== value.length + 1 || Object.keys(d).some(k => k !== 'length' && !/^(0|[1-9][0-9]*)$/.test(k)))) throw new TypeError('Expected dense array.');
    for (const k of Object.keys(d)) if (k !== 'length' || !Array.isArray(value)) plainTree(d[k].value);
  }
  function structuralEqual(a, b) {
    if (a === b) return true;
    if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
    const ak = Object.keys(a), bk = Object.keys(b);
    return ak.length === bk.length && ak.every(k => Object.hasOwn(b, k) && structuralEqual(a[k], b[k]));
  }
  function resolveAuthority(value, type) {
    plainTree(value);
    const current = model.authorities.get(value?.id);
    if (!current || !structuralEqual(value, current) || !current.actions.includes(type)) throw new Error('The attachment authority changed or does not offer that action.');
    return current;
  }
  function invalidateDependent(fileIds, cause = {kind: 'owner_delete'}) {
    const answer = state.officerAnswer;
    if (answer && [...model.answerDependencies].some(fileId => fileIds.includes(fileId))) {
      answer.state = 'source_unavailable'; answer.text = null; answer.sourceValidation = 'invalid'; answer.revision++;
      const f = state.files.find(f => fileIds.includes(f.id));
      answer.noticeParts = {title: 'This answer is no longer shown.', body: cause.kind === 'owner_delete'
        ? `It used ${f.name}, which you deleted. To get an answer again, attach the file and ask again.`
        : `It used ${f.name}, which is no longer available while its local copies are being removed. To get an answer again, attach the file and ask again.`};
      answer.notice = `${answer.noticeParts.title} ${answer.noticeParts.body}`; answer.actions = [];
      for (const c of answer.citations) { c.available = false; c.navigation = {kind: 'unavailable', reason: 'source_unavailable'}; }
      for (const f of state.files) if (answer.citations.some(c => c.fileId === f.id)) f.usedInAnswer = false;
    }
    if (state.preview && (fileIds.includes(state.preview.fileId) || state.preview.authority.target.citationTarget && answer?.state === 'source_unavailable')) state.preview = null;
  }
  function replaceDeleteDialog(op = null) {
    if (model.deleteView && model.deleteView.operationId !== op?.id) {
      const previous = model.deletions.find(o => o.id === model.deleteView.operationId);
      if (previous.state === 'confirm') { previous.state = 'abandoned'; previous.revision++; }
    }
    model.deleteView = op ? {id: mint('delete-dialog'), operationId: op.id} : null;
  }
  function pinnedWarning(target, f) {
    if (!f.retention.pinned) return null;
    const label = target.owner.destination.label;
    const request = model.requests.find(r => r.requestId === target.owner.requestId);
    const requestState = request?.message?.state || request?.messageState;
    const opening = {
      queued: `${label}’s message is still queued.`,
      delivery_unknown: `${label}’s delivery status is unknown.`,
      partial_not_sent: `This message was not delivered. Some files were copied into ${label}’s project folder.`,
      preparing_delivery: `Copies are still being prepared in ${label}’s project folder. The message is not queued yet.`,
    }[requestState] || 'These files are retained for an unfinished request.';
    return `${opening} If you delete this file now, ${label} may not be able to read it when its turn comes. Deleting the file does not cancel the message.`;
  }
  function createDeletion(target, cause = {kind: 'owner_delete'}, show = true) {
    const f = findFile(target.fileId);
    const op = {id: mint('deletion'), cause: clone(cause), owner: clone(target.owner), fileIds: [f.id],
      selectedSourceVersions: [{fileId: f.id, version: target.sourceVersion}], revision: 1, state: 'confirm',
      copyIds: model.copies.filter(c => c.fileId === f.id && c.state !== 'deleted').map(c => c.id), releasedBytes: 0, resultText: null,
      pinnedWarning: pinnedWarning(target, f),
      removes: [
        ...(model.copies.some(c => c.fileId === f.id && c.kind === 'stored' && c.state === 'present') ? ['Motet’s stored copy of the file.'] : []),
        ...(model.copies.some(c => c.fileId === f.id && c.kind === 'derived' && c.state === 'present') ? ['Motet’s derived copies of the file.'] : []),
        ...(model.copies.some(c => c.fileId === f.id && c.kind === 'workspace' && c.state === 'present') ? [`The copy saved in ${target.owner.destination.label}’s project folder.`] : []),
        'Motet’s use of it as a source, and answers that depend on it.'],
      retains: ['Messages already in the agent’s own chat.', 'Activity history, and copies an agent made.', 'Anything already sent to a model provider.']};
    model.deletions.push(op); if (show) replaceDeleteDialog(op); return op;
  }
  function confirmDeletion(op) {
    for (const v of op.selectedSourceVersions) {
      const source = model.sources.get(v.fileId);
      if (source.pending || source.version !== v.version) throw new Error('The selected source changed.');
    }
    for (const fileId of op.fileIds) {
      const source = model.sources.get(fileId), f = findFile(fileId);
      source.pending = true; source.version++; f.previewable = false; f.actions = []; f.usedInAnswer = false;
    }
    invalidateDependent(op.fileIds, op.cause);
    for (const copyId of op.copyIds) {
      const copy = model.copies.find(c => c.id === copyId);
      if (copy.state === 'present') copy.state = 'deleting';
    }
    op.state = 'deleting'; op.revision++;
    for (const fileId of op.fileIds) { const policy = model.policies.get(fileId); if (op.cause.kind === 'owner_expiry') { policy.state = 'deleting'; policy.operationId = op.id; } }
  }
  function advanceDeletion(op) {
    let released = 0;
    for (const copyId of op.copyIds) {
      const c = model.copies.find(c => c.id === copyId), outcome = model.outcomes.get(copyId);
      if (c.state !== 'deleting') continue;
      c.state = outcome;
      if (outcome === 'deleted') released += c.bytes;
      else c.reason = outcome === 'identity_changed' ? `The copy in ${op.owner.destination.label}’s project folder was changed or moved. It no longer matches the file Motet saved, so Motet left it alone rather than delete something that may not be its own. Remove it yourself if you want it gone.` : 'The fictional copy could not be removed.';
    }
    op.releasedBytes += released;
    const partial = op.copyIds.some(copyId => model.copies.find(c => c.id === copyId).state !== 'deleted');
    const unknown = op.copyIds.some(copyId => model.copies.find(c => c.id === copyId).state === 'unknown');
    op.state = unknown ? 'unknown' : partial ? 'partial_failure' : 'deleted'; op.revision++;
    op.resultText = unknown ? 'The removal could not be confirmed' : partial ? '1 copy was not removed' : 'Local copies deleted';
    for (const fileId of op.fileIds) {
      const f = findFile(fileId), policy = model.policies.get(fileId), expired = op.cause.kind === 'owner_expiry';
      unavailableFile(f, partial ? 'missing' : expired ? 'expired' : 'deleted', model.now);
      model.sources.get(fileId).pending = false;
      if (expired) { policy.state = unknown ? 'unknown' : partial ? 'partial_failure' : 'removed'; policy.removedAt = partial ? null : model.now; policy.removalTiming = partial ? null : op.cause.deferred || model.now > op.cause.deadlineAt ? 'delayed' : 'on_date'; }
    }
    if (!partial && op.cause.kind === 'owner_expiry') expiredAnswerNotice(op);
    reconcileSources();
  }
  function openPreview(target, citationTarget = null, initialLocation = null) {
    const source = citationTarget ? sourceTarget(target.fileId, target.owner, target.access) : target;
    const f = findFile(source.fileId), key = model.sources.get(f.id).key;
    const page = citationTarget?.location?.page || initialLocation?.page || 1;
    const relay = f.preparationKind === 'original_relay', direct = f.preparationKind === 'officer_direct';
    const location = relay || direct ? null : citationTarget ? clone(citationTarget.location) : key === 'shot' ? {image: true} : key === 'plan' ? {page} : null;
    const retained = source.owner.kind === 'request' && model.requests.find(r => r.requestId === source.owner.requestId
      && r.fileIds.includes(f.id) && same(r.destination, source.owner.destination) && r.message?.state === 'retained_local');
    const retainedPrior = retained ? model.officerRetainedPrior.get(`${retained.requestId}:${f.id}`) || 'unknown' : null;
    const spec = direct ? officerDirectPreview(f, citationTarget, retainedPrior) : relay ? relayPreview(source.owner.destination.label, source.owner.kind === 'draft')
      : authoredPreview(key, page, source.owner.destination.label, source.owner.kind === 'draft');
    const citation = citationTarget ? state.officerAnswer.citations.find(c => c.id === citationTarget.citationId) : null;
    const viewId = mint('preview-view');
    state.preview = {authority: null, fileId: f.id, sourceVersion: source.sourceVersion, kind: spec.kind,
      officerState: spec.officerState || null, officerPriorDelivery: spec.officerPriorDelivery || null, location, pageCount: spec.kind === 'pdf' ? 6 : null, lineCount: spec.kind === 'text' ? spec.payload.lines.length : null,
      payload: spec.payload, provenance: {kind: spec.kind === 'fallback' ? 'no_preview' : citationTarget ? 'citation' : source.owner.kind === 'draft' ? 'draft' : 'file',
        label: spec.kind === 'fallback' ? relay || direct ? 'Nothing was checked or drawn.' : 'No preview was made. Nothing is guessed about what the file contains.' : citationTarget ? `Opened from the citation “${citation.label}”. This is your file, not recorded activity.` : `Fictional preview of ${f.name}.`, sample: true, citationTarget: clone(citationTarget)}, navigation: {}};
    setPreviewAuthority(viewId, source, citationTarget, page, 'fit');
  }
  function setPreviewAuthority(viewId, source, citationTarget, page, zoom, revision = 1) {
    const p = state.preview, actions = ['close_preview'];
    if (p.kind === 'pdf') { actions.unshift('preview_page'); p.navigation = {previousPage: page > 1 ? page - 1 : null, nextPage: page < 6 ? page + 1 : null, zoom: null, previousZoom: null, nextZoom: null}; }
    else if (p.kind === 'image') { const steps = ['fit', 100, 150, 200], i = steps.indexOf(zoom); actions.unshift('preview_zoom'); p.navigation = {previousPage: null, nextPage: null, zoom, previousZoom: i ? steps[i - 1] : null, nextZoom: i < 3 ? steps[i + 1] : null}; }
    else p.navigation = {previousPage: null, nextPage: null, zoom: null, previousZoom: null, nextZoom: null};
    p.authority = authority('preview', {viewId, source, citationTarget}, actions, revision);
  }
  const PHASE4_FIELDS = {open_files: [], close_files: [], open_storage: [], close_storage: [], open_preview: [], open_citation: [],
    preview_page: ['page'], preview_zoom: ['zoom'], close_preview: [], request_delete: [], confirm_delete: [], keep_file: [], close_delete: [],
    open_delete_result: [], advance_sample_deletion: [], begin_reattach: ['draftFence'], cancel_reattach: [], select_reattach_sample: ['draftFence', 'sampleId'],
    open_expiry: [], open_expiry_all: [], set_expiry: ['date'], clear_expiry: [], refresh_expiry_selection: [], close_expiry: [], storage_inventory_page: ['offset']};
  function phase4Action(command) {
    objectFields(command, ['type', 'authority', ...PHASE4_FIELDS[command.type]]);
    plainTree(command);
    if (!enabled) throw new Error('Attachments are unavailable.');
    const a = resolveAuthority(command.authority, command.type), t = a.target, type = command.type;
    if (['open_expiry', 'open_expiry_all', 'set_expiry', 'clear_expiry', 'refresh_expiry_selection', 'close_expiry'].includes(type)) return expiryAction(type, a, command);
    if (type === 'storage_inventory_page' && (!Number.isSafeInteger(command.offset) || ![state.management.storage.inventoryPage.previousOffset, state.management.storage.inventoryPage.nextOffset].filter(v => v !== null).includes(command.offset))) throw new Error('Choose an advertised storage page.');
    let op = t.operationId ? model.deletions.find(o => o.id === t.operationId) : null, sample = null;
    if (['begin_reattach', 'select_reattach_sample'].includes(type)) {
      if (!writable() || !structuralEqual(command.draftFence, draftFence())) throw new Error('The attachment draft changed.');
      const source = type === 'begin_reattach' ? t : t.historicalSource;
      if (!activeDestination(source.owner.destination) || !model.views.files) throw new Error('The history destination changed.');
    }
    if (type === 'select_reattach_sample') {
      const key = ['plan', 'shot', 'req', 'table'].find(k => id(k) === command.sampleId);
      if (!key) throw new Error('Choose an advertised sample.');
      sample = selectedSample(state, key);
      const d = state.draft, total = d.fileIds.reduce((n, id) => n + state.files.find(f => f.id === id).bytes, sample.bytes);
      if (d.fileIds.length + 1 > state.capabilities.maxFiles || total > state.capabilities.maxDraftBytes || !hasCopyCapacity(copyAllocations([sample], ['stored'], true))
        || sample.bytes > state.capabilities.maxFileBytes || !state.capabilities.formats.includes(sample.mediaType)) throw new Error('These samples exceed the current attachment limits.');
    }
    if (type === 'preview_page' && (!Number.isSafeInteger(command.page) || ![state.preview.navigation.previousPage, state.preview.navigation.nextPage].includes(command.page))) throw new Error('Page is not an adjacent authored page.');
    if (type === 'preview_zoom' && ![state.preview.navigation.previousZoom, state.preview.navigation.nextZoom].filter(v => v !== null).includes(command.zoom)) throw new Error('Zoom is not an adjacent permitted step.');
    if (type === 'confirm_delete') for (const v of op.selectedSourceVersions) if (model.sources.get(v.fileId).pending || model.sources.get(v.fileId).version !== v.version) throw new Error('The selected source changed.');
    let outcome = type;
    if (type === 'open_files') { if (model.views.files) revokeViewPreview(model.views.files.id); model.picker = null; model.views.files = newView('history'); }
    if (type === 'close_files') { model.views.files = null; model.picker = null; if (model.views.storage) model.views.storage.parent = null; revokeViewPreview(t.viewId); }
    if (type === 'storage_inventory_page') model.inventoryOffset = command.offset;
    if (['open_storage', 'close_storage', 'close_files', 'request_delete'].includes(type)) model.expiryDialog = null;
    if (type === 'open_storage') { if (model.views.storage) revokeViewPreview(model.views.storage.id); model.views.storage = newView('storage', a.kind === 'history' ? {kind: 'history', id: t.viewId, generation: t.viewGeneration} : null); }
    if (type === 'close_storage') { revokeViewPreview(t.viewId); model.views.storage = null; }
    if (type === 'open_preview') openPreview(t);
    if (type === 'open_citation') openPreview(t, t);
    if (type === 'close_preview') state.preview = null;
    if (type === 'preview_page') {
      state.preview.payload = authoredPreview(model.sources.get(state.preview.fileId).key, command.page, t.source.owner.destination.label).payload;
      state.preview.location = {page: command.page};
      setPreviewAuthority(t.viewId, t.source, t.citationTarget, command.page, 'fit', a.revision + 1);
    }
    if (type === 'preview_zoom') setPreviewAuthority(t.viewId, t.source, t.citationTarget, 1, command.zoom, a.revision + 1);
    if (type === 'request_delete') op = createDeletion(t);
    if (type === 'confirm_delete') confirmDeletion(op);
    if (type === 'keep_file' || type === 'close_delete') {
      if (op.state === 'confirm') { op.state = 'abandoned'; op.revision++; }
      replaceDeleteDialog();
    }
    if (type === 'open_delete_result') replaceDeleteDialog(op);
    if (type === 'advance_sample_deletion') advanceDeletion(op);
    if (type === 'begin_reattach') model.picker = {id: mint('reattach-picker'), historyViewId: model.views.files.id,
      historyViewGeneration: model.views.files.generation, historicalSource: clone(t)};
    if (type === 'cancel_reattach') model.picker = null;
    if (type === 'select_reattach_sample') {
      const empty = state.draft.text === '' && state.draft.fileIds.length === 0;
      if (empty) {
        state.draft = draft(state.destination); state.draft.id = mint('reattached-draft'); state.draft.writerGeneration = ++generation;
        state.draft.noticeCode = 'reattached_new_draft'; state.draft.notice = `Attached again as a new draft for ${state.destination.label}. Nothing is sent until you press Send.`;
      } else if (state.draft.noticeCode === 'reattached_new_draft') { state.draft.noticeCode = null; state.draft.notice = null; }
      sample.id = `${sample.id}-selected-${++sequence}`; model.fileKeys.set(sample.id, ['plan', 'shot', 'req', 'table'].find(k => id(k) === command.sampleId));
      state.files.push(sample); state.draft.fileIds.push(sample.id); state.draft.revision++;
      model.picker = null; revokeViewPreview(model.views.files.id); model.views.files = null; outcome = empty ? 'new_draft' : 'added_to_draft';
      reconcileLegacy(); deriveDraft(state);
    }
    model.receipt = {sequence: ++receiptSequence, type, authorityId: a.id, outcome};
    reconcileSources(); refresh(); return clone(state);
  }
  function revokeViewPreview(viewId) {
    if (state.preview?.authority.target.source.access.id === viewId) state.preview = null;
  }

  function messageAction(command) {
    objectFields(command, MESSAGE_BINDING_FIELDS);
    if (!enabled) throw new Error('Attachments are unavailable.');
    const m = model.requests.find(r => r.message?.id === command.messageId)?.message;
    if (m && !activeDestination(m.destination) && (command.type !== 'check_status' || !model.views.storage || m.state !== 'delivery_unknown' || !m.actions.includes('check_status'))) throw new Error('This message is outside the active destination.');
    if (!m || m.requestId !== command.requestId || m.revision !== command.revision
        || m.operationGeneration !== command.operationGeneration || !exactDestination(command.destination, m.destination))
      throw new Error('The attachment message changed. Read its current state.');
    const type = command.type;
    if (!m.actions.includes(type)) throw new Error('This message does not offer that action.');
    const isPartial = m.state === 'partial_not_sent' && m.deliveryPhase === 'pre_dispatch';
    if (type === 'check_status' && !(m.state === 'delivery_unknown' && m.deliveryPhase === 'unknown' && m.recovery.canCheck))
      throw new Error('This message cannot be checked through this sample.');
    if (type === 'finish_send' && !(isPartial && m.recovery.canFinish)) throw new Error('This request cannot be finished.');
    if (type === 'delete_saved_copies' && !(isPartial && m.recovery.canDeleteSavedCopies)) throw new Error('These saved copies cannot be removed.');
    const files = m.fileIds.map(fileId => state.files.find(f => f.id === fileId));
    if (type !== 'check_status' && (m.destination.kind !== 'agent' || files.some(f => !f || f.preparation !== 'ready'
        || f.error || !['available', 'not_saved'].includes(f.availability)
        || model.sources.get(f.id)?.pending
        || (f.availability === 'available' && f.availableTo !== m.destination.label))))
      throw new Error('The original file manifest is not recoverable.');
    if (type === 'finish_send') requireCopyCapacity(deliveryAllocations(files, m.destination));
    let outcome;
    if (type === 'check_status') {
      outcome = 'still_unknown';
    } else if (type === 'finish_send') {
      for (const f of files) Object.assign(f, {availability: 'available', availableTo: m.destination.label,
        previewable: true, usedInAnswer: false, actions: ['preview'],
        retention: {pinned: true, reason: 'queued', expiresAt: null}});
      Object.assign(m, {state: 'queued', statusText: STATUS.queued, deliveryPhase: 'dispatched',
        recovery: {canCheck: false, canFinish: false, canDeleteSavedCopies: false}, actions: []});
      outcome = 'queued_same_request';
    } else {
      const saved = files.filter(f => f.availability === 'available');
      const releasedBytes = saved.reduce((sum, f) => sum + f.bytes, 0);
      for (const f of saved) Object.assign(f, {availability: 'not_saved', availableTo: null,
        previewable: false, usedInAnswer: false, actions: []});
      for (const f of files) f.retention = {pinned: false, reason: null, expiresAt: null};
      Object.assign(m, {state: 'not_sent', statusText: 'Not sent', deliveryPhase: 'cleanup_fenced',
        cleanup: {state: 'saved_copies_deleted', fileIds: saved.map(f => f.id), releasedBytes},
        recovery: {canCheck: false, canFinish: false, canDeleteSavedCopies: false}, actions: []});
      outcome = 'saved_copies_deleted';
    }
    m.revision++;
    state.lastAction = {type, targetId: m.id, outcome};
    reconcileLegacy();
    return clone(state);
  }
  function act(command) {
    objectFields(command, [...BINDING_FIELDS, ...MESSAGE_BINDING_FIELDS, 'fileId', 'fileIds', 'text', 'authority', 'page', 'zoom', 'draftFence', 'sampleId', 'date', 'offset'], ['type']);
    if (typeof command.type === 'string' && Object.hasOwn(PHASE4_FIELDS, command.type)) return phase4Action(command);
    objectFields(command, [...BINDING_FIELDS, ...MESSAGE_BINDING_FIELDS, 'fileId', 'fileIds', 'text'], ['type']);
    if (typeof command.type !== 'string') throw new RangeError('Unknown attachment action.');
    if (MESSAGE_ACTIONS.includes(command.type)) return messageAction(command);
    if (!Object.hasOwn(COMMAND_FIELDS, command.type)) throw new RangeError('Unknown attachment action.');
    objectFields(command, [...BINDING_FIELDS, ...COMMAND_FIELDS[command.type]]);
    if (!enabled || !state.draft || !state.capabilities.available) throw new Error('Attachments are unavailable.');
    const d = state.draft;
    if (command.draftId !== d.id || command.revision !== d.revision || command.writerGeneration !== d.writerGeneration
        || !exactDestination(command.destination, d.destination)) throw new Error('The attachment draft changed. Read its current state.');
    const type = command.type;
    if (['resume_draft', 'create_draft'].includes(type)) {
      const permittedStates = type === 'resume_draft' ? ['resume_available', 'conflict'] : ['resume_available', 'conflict', 'unavailable'];
      if (!permittedStates.includes(d.state) || !d.actions.includes(type)) throw new Error('This draft does not offer that action.');
      if (type === 'resume_draft') {
        if (d.fileIds.some(fileId => !state.files.some(f => f.id === fileId && f.availability === 'private')))
          throw new Error('The saved draft files are unavailable.');
        Object.assign(d, {state: 'editing', editable: true, writerGeneration: ++generation, revision: d.revision + 1,
          notice: null, noticeCode: null, actions: []});
        for (const f of state.files.filter(f => d.fileIds.includes(f.id))) {
          f.previewable = f.preparation === 'ready' && !f.error;
          f.actions = f.error ? f.error.actions.slice() : f.preparation === 'uploading' ? ['cancel_upload']
            : f.previewable ? ['preview', 'remove_from_draft'] : ['remove_from_draft'];
        }
      } else {
        state.draft = draft(d.destination);
        state.draft.id += `-created-${++sequence}`;
        state.draft.writerGeneration = ++generation;
      }
      state.ui.detailsFileId = null; state.ui.pickerOpen = false; state.pickerFiles = [];
      deriveDraft(state);
      state.lastAction = {type, targetId: state.draft.id, outcome: type === 'resume_draft' ? 'draft_resumed' : 'draft_created'};
      reconcileLegacy();
      return clone(state);
    }
    const readOnlyDetails = d.state === 'conflict' && d.editable === false
      && ['show_details', 'close_details'].includes(type);
    if ((!d.editable || d.state !== 'editing') && !readOnlyDetails) throw new Error('This draft is not editable in this window.');
    if (readOnlyDetails && type === 'close_details' && (!d.fileIds.includes(state.ui.detailsFileId)
        || !state.files.some(f => f.id === state.ui.detailsFileId && f.availability === 'private')))
      throw new Error('No selected private draft file details are open.');
    // Validate the complete operation before changing any data.
    let selected;
    if (['remove_file', 'cancel_upload', 'show_details', 'retry_preparation'].includes(type)) {
      if (typeof command.fileId !== 'string' || !d.fileIds.includes(command.fileId)) throw new Error('This file is not in the current draft.');
      selected = state.files.find(f => f.id === command.fileId);
      if (!selected || selected.availability !== 'private') throw new Error('This file is not a private draft file.');
      if (type === 'cancel_upload' && selected.preparation !== 'uploading') throw new Error('This file is not uploading.');
      if (type === 'remove_file' && selected.preparation === 'uploading') throw new Error('Cancel this upload instead.');
      if (type === 'retry_preparation' && !(selected.preparation === 'failed' && selected.error
          && ['folder_unreachable', 'storage_full'].includes(selected.error.code)
          && selected.actions.includes(type) && selected.error.actions.includes(type)))
        throw new Error('This file does not offer preparation retry.');
    }
    let samples;
    if (type === 'add_sample_files') {
      if (!Array.isArray(command.fileIds) || command.fileIds.length < 1 || command.fileIds.length > 5
          || Object.keys(command.fileIds).length !== command.fileIds.length
          || Reflect.ownKeys(command.fileIds).some(k => k !== 'length' && !/^[0-9]+$/.test(String(k)))
          || Object.values(Object.getOwnPropertyDescriptors(command.fileIds)).some(v => !Object.hasOwn(v, 'value')))
        throw new TypeError('Choose sample file IDs only.');
      samples = command.fileIds.map(fileId => {
        const key = ['plan', 'shot', 'req'].find(k => id(k) === fileId);
        if (!key) throw new RangeError('Unknown sample file.');
        return selectedSample(state, key);
      });
      const total = state.files.filter(f => d.fileIds.includes(f.id)).reduce((n, f) => n + f.bytes, 0)
        + samples.reduce((n, f) => n + f.bytes, 0);
      if (d.fileIds.length + samples.length > state.capabilities.maxFiles || total > state.capabilities.maxDraftBytes
          || !hasCopyCapacity(copyAllocations(samples, ['stored'], true)) || samples.some(f => !state.capabilities.formats.includes(f.mediaType) || f.bytes > state.capabilities.maxFileBytes))
        throw new Error('These samples exceed the current attachment limits.');
    }
    const text = type === 'edit_text' ? command.text : d.text;
    if (['edit_text', 'send_sample'].includes(type) && (typeof text !== 'string' || text.includes('\0') || [...text].length > state.capabilities.maxTextChars))
      throw new TypeError(`Sample message text must be at most ${state.capabilities.maxTextChars} Unicode characters and contain no null character.`);
    if (type === 'send_sample' && state.officerAnswer?.state === 'forwarding_refused') throw new Error('This forwarding request was refused. Select the agent and attach the file there.');
    if (type === 'send_sample' && (!d.canSend || d.fileIds.some(fileId => {
      const f = state.files.find(f => f.id === fileId);
      return !f || f.preparation !== 'ready' || f.availability !== 'private' || f.error;
    }))) throw new Error(d.sendBlockedReason || 'This draft is not ready.');

    if (type === 'send_sample') requireCopyCapacity(deliveryAllocations(d.fileIds.map(fileId => state.files.find(f => f.id === fileId)), d.destination));
    const retryBlocked = type === 'retry_preparation' && !hasCopyCapacity(copyAllocations([selected], ['stored']));
    state.lastAction = null;
    if (type === 'retry_preparation') {
      const blocked = retryBlocked;
      if (!blocked) Object.assign(selected, {preparation: 'ready', error: null, previewable: true, actions: ['preview', 'remove_from_draft']});
      state.lastAction = {type, targetId: selected.id, outcome: blocked ? 'preparation_blocked' : 'preparation_ready'};
    }
    if (type === 'open_picker') { state.ui.pickerOpen = true; state.pickerFiles = ['plan', 'shot', 'req'].map(key => selectedSample(state, key)); }
    if (type === 'close_picker') { state.ui.pickerOpen = false; state.pickerFiles = []; }
    if (type === 'show_details') state.ui.detailsFileId = selected.id;
    if (type === 'close_details') state.ui.detailsFileId = null;
    if (type === 'edit_text') { d.text = command.text; d.suggestedText = false; if (state.officerAnswer?.state === 'forwarding_refused') state.officerAnswer = null; }
    if (type === 'add_sample_files') {
      for (const f of samples) {
        const sampleKey = ['plan', 'shot', 'req'].find(k => id(k) === f.id);
        f.id = `${f.id}-selected-${++sequence}`;
        model.fileKeys.set(f.id, sampleKey);
        state.files.push(f); d.fileIds.push(f.id);
      }
      state.ui.pickerOpen = false; state.pickerFiles = [];
      if (!d.text) { d.text = 'Review the attached file'; d.suggestedText = true; }
    }
    if (type === 'remove_file' || type === 'cancel_upload') {
      d.fileIds = d.fileIds.filter(fileId => fileId !== selected.id);
      if (d.fileIds.length === 0 && d.suggestedText) { d.text = ''; d.suggestedText = false; }
      state.files = state.files.filter(f => f.id !== selected.id);
      if (state.ui.detailsFileId === selected.id) state.ui.detailsFileId = null;
    }
    d.revision++;
    if (type === 'send_sample') {
      const requestId = `fixture-request-action-${++sequence}`;
      state.messages.push({id: `fixture-message-action-${sequence}`, requestId, destination: clone(d.destination),
        text: d.text, fileIds: d.fileIds.slice(), createdAt: NOW, state: 'queued', ...messageOperation('queued', ++generation),
        statusText: STATUS.queued, recovery: {canCheck: false, canFinish: false, canDeleteSavedCopies: false}, actions: []});
      for (const fileId of d.fileIds) {
        const f = state.files.find(f => f.id === fileId);
        Object.assign(f, {availability: 'available', availableTo: state.destination.kind === 'agent' ? state.destination.label : null,
          usedInAnswer: false, actions: ['preview'], retention: {pinned: true, reason: 'queued', expiresAt: null}});
      }
      model.draftBindings.delete(d.id);
      state.draft = draft(state.destination);
      state.draft.id += `-after-send-${sequence}`;
      state.draft.writerGeneration = ++generation;
      state.ui.detailsFileId = null; state.ui.pickerOpen = false; state.pickerFiles = [];
      // An edited Officer prompt cannot acquire a canned answer. It is merely queued sample data.
      for (const f of state.files) if (state.officerAnswer?.citations.some(c => c.fileId === f.id)) f.usedInAnswer = false;
      state.officerAnswer = null; model.answerDependencies.clear();
      for (const fileId of d.fileIds) {
        const f = state.files.find(f => f.id === fileId);
        if (f.preparationKind === 'officer_direct') f.officerDelivery = {state: 'unknown', requestId};
      }
    }
    deriveDraft(state);
    reconcileLegacy();
    return clone(state);
  }
  function loadScenario(key) {
    state = makeScenario(EXPIRY_SCENARIOS.has(key) ? 'a10-files' : key === 'a06-cleaned' ? 'a06-partial' : ['a11-progress', 'a11-error', 'a11-done'].includes(key) ? 'a11-confirm' : key);
    state.scenario = key;
    initializePhase4(key);
    if (EXPIRY_SCENARIOS.has(key)) initializeExpiryScenario(key);
    if (key === 'a06-cleaned') {
      // Build the review state through the same validated transition as the button.
      const m = state.messages[0];
      messageAction({type: 'delete_saved_copies', messageId: m.id, requestId: m.requestId,
        destination: clone(m.destination), revision: m.revision, operationGeneration: m.operationGeneration});
      state.scenario = key;
      state.lastAction = null; // Selecting a picture is not an owner action receipt.
    }
  }
  if (enabled) loadScenario(initial);
  return Object.freeze({fixture: true, schemaVersion: 1, enabled,
    snapshot: () => clone(state), act,
    advanceSample: simulateExpiry,
    selectScenario(key) {
      if (!ATTACHMENT_SCENARIOS.includes(key)) throw new RangeError('Unknown attachment scenario.');
      if (enabled) {
        loadScenario(key);
        const nextGeneration = ++generation;
        if (state.draft) state.draft.writerGeneration = nextGeneration;
        for (const r of model.requests) if (r.message) r.message.operationGeneration = nextGeneration;
        reconcileLegacy();
      }
      return clone(state);
    },
  });
}
