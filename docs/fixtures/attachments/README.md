# Attachment page fixture

This module supplies fictional JSON states for the attachment interface, including file selection,
original-file relay, Motet file questions, deletion and optional expiry. It is not an upload or
messaging implementation. It never authenticates, reads real files, starts timers, connects to a server,
stores data, asks permission, notifies, or calls a provider. Importing it does not activate anything.

```js
import {createAttachmentsFixture, ATTACHMENT_SCENARIOS} from './fixture.mjs';

const fixture = createAttachmentsFixture({
  enabled: true,       // Default is false. Only literal true enables it.
  search: '?demo',     // Explicit query; never read from the global browser location.
  scenario: 'a03-five',
});
const view = fixture.snapshot();
const next = fixture.act({
  type: 'remove_file',
  draftId: view.draft.id,
  destination: view.draft.destination,
  revision: view.draft.revision,
  writerGeneration: view.draft.writerGeneration,
  fileId: view.draft.fileIds[0],
});
// next has the same text and the other four files. Nothing was deleted on disk.
```

Both `enabled === true` and an actual `demo` query key are required. Without either, every snapshot remains
disabled with no destination, draft, files or messages; `selectScenario` cannot activate it, and `act` refuses.
An unknown scenario is an error, including while disabled. `snapshot`, `selectScenario`, and `act` return
independent JSON values. Mutating one does not alter the fixture or another instance. The adapter itself has
`schemaVersion: 1`, `fixture: true` and `enabled`.

See [the fixture module](fixture.mjs) and its [tests](fixture.test.mjs) for the snapshot and action shapes. The previews, dates, quotas and format lists here are sample data.
The samples use Ada and Bram in Orchard, plus Cleo in Lighthouse for `a07-together`. `a07-together` leaves existing approval/pending controls to the page;
attachment actions never answer a permission or steer an agent.

## Retention and capacity samples

The default is no timed expiry for sent agent or Motet files and reachable resumable drafts. Default
samples have `retention.expiresAt:null`; explicit-expiry scenarios alone carry owner-selected dates.
The ordinary pricing-table row remains available. Deleted survey and missing old-plan rows still provide
bound Attach again actions. `a10-reattach` selects a fresh private survey, preserving its deleted history.
The fictional explicit-expiry actions below model a user-selected date. There is no real timer,
scheduler, filesystem deletion or production persistence. Read-only snapshots never advance time.

Capacity is 5 GB (5,000,000,000 bytes), with a 1.2 GB (1,200,000,000-byte) baseline sample usage.
The existing copy ledger adds and releases actual fictional copy bytes. Per-file 10 MiB, per-draft 20 MiB
and five-file limits are unchanged. Full samples have `usedBytes === limitBytes`; no automatic eviction.
Storage retains unfinished rows together, followed by finished rows oldest authored request date first,
then request ID. These sample dates do not establish actual runtime completion times.

These samples cover retention, expiry, cleanup and follow-up states in memory. For live Motet storage
and follow-ups, see [Motet file reading](../../MOTET_FILE_READING.md). For copies sent to agents, see
the [trust boundary](../../../README.md#trust-boundary).

## Scenarios

The exported frozen `ATTACHMENT_SCENARIOS` array is the source of all **80** scenario keys, in accepted screen order:

| Group | Keys |
| --- | --- |
| Entry | `a01-agent`, `a01-picker`, `a01-officer`, `a01-room` |
| Preparation | `a02-drop`, `a02-preparing` |
| Ready | `a03-one`, `a03-five`, `a03-relay`, `a03-room-one`, `a03-room-five`, `a03-details` |
| Issues | `a04-mixed`, `a04-scanned`, `a04-relay-folder`, `a04-relay-officer`, `a04-budget`, `a04-folder`, `a04-quota`, `a04-recipient` |
| Drafts | `a05-saved`, `a05-scope`, `a05-resume`, `a05-conflict`, `a05-missing` |
| Delivery | `a06-preparing`, `a06-queued`, `a06-relay-unknown`, `a06-relay-partial`, `a06-unknown`, `a06-partial`, `a06-partial-capacity`, `a06-cleaned` |
| Motet direct input | `a09-officer-private`, `a09-officer-supplied`, `a09-officer-unknown`, `a09-officer-page`, `a09-officer-image`, `a09-officer-retained`, `a09-officer-retained-earlier`, `a09-officer-retained-unknown`, `a04-officer-media`, `a04-officer-model`, `a08-officer-direct`, `a08-officer-uncited`, `a08-officer-followup`, `a08-officer-forward`, `a06-officer-retained`, `a06-officer-rejected` |
| Sent files | `a07-answered`, `a07-together` |
| Motet | `a08-doc`, `a08-compare`, `a08-gone`, `a08-forward` |
| Preview | `a09-pdf`, `a09-image`, `a09-relay-pdf`, `a09-relay-image`, `a09-text`, `a09-fallback` |
| History | `a10-files`, `a10-reattach`, `a10-files-expiry` |
| Storage | `a11-full`, `a11-confirm`, `a11-progress`, `a11-error`, `a11-done`, `a11-storage`, `a11-storage-set`, `a11-storage-all-set` |
| Expiry | `a11-expiry`, `a11-expiry-all`, `a11-expiry-set`, `a11-expiry-all-set`, `a11-expiry-selection-changed`, `a11-expiry-removal-started`, `a11-expiry-partial`, `a11-expiry-unconfirmed` |

`a06-partial-capacity` is a bounded canonical capacity-test state, not a new design or production capability.
It has one earlier partial request (plan and requirements) and a separate ready current draft (screenshot,
pricing table, in that order). Its initial full 5 GB usage is seeded before ordinary copy-ledger construction;
normal readiness checks block Send. One standard `delete_saved_copies` action on that partial request releases
exactly 1,887,437 workspace-copy bytes. The current draft then has room for its 668,672 delivery bytes without
changing its identity, revision, writer, text or file order, and without sending either message. Consumers use
the existing message action binding; no DTO quota override, repeated sends or new action is needed.

`a06-cleaned` is built through the same validated saved-copy cleanup transition as the button, not a second
implementation of its result. The canonical state is `not_sent` with
`cleanup_fenced` and `saved_copies_deleted`. Selecting it yields no `lastAction` receipt and cannot enable Finish
or another cleanup. It adds no control or live capability.

The `a11-error` changed-copy warning explains why Motet left the copy untouched and gives the next step:
“It no longer matches the file Motet saved, so Motet left it alone rather than delete something that may not be its own. Remove it yourself if you want it gone.”

The fixture includes explicitly authored typed PDF, text, checkout drawing and fallback payloads, not real
file binaries. The [phase 4 operations](#phase-4-operations) describe how those samples are accessed.
Six authored PDF pages
are bounded to 1–6; text uses 21 literal lines and inclusive cited range 10–17; image starts at fit with
adjacent fit/100/150/200 steps. The checkout drawing is a fixed DOM/CSS sample, not SVG or a live Pay now
control. No URLs, fetched images, scripts, frames, active HTML or file decoding. Rendering/focus remain
page responsibilities. The stage tray includes one-file and five-file samples scoped to the whole stage.

## Replies and source navigation

Only the seeded A07 answer adds `message.reply`; answered alone never implies text. Motet answers bind
exact retained message/request/full destination and historical model, with a separate answer revision for
source validity. Available validated uploads may advertise exact phase-4 citation-preview authority;
activity IDs remain fictional and navigation unavailable. Deleted sources null dependent answer text,
invalidate sources and disable citation navigation, while retaining historical correlation/model/labels.
The fixture uses the recorded IDs as supplied. New sample questions queue without synthesized answers. Editing clears the old
forwarding refusal; no natural-language forwarding classifier is provided.

## Demo agent and project bindings

The fictional demo bindings are exact per agent and project:

| Fixture agent | Existing page demo agent | Exact fixture project |
| --- | --- | --- |
| ada | c:demo-ada | fixture-orchard |
| bram | x:demo-bram | fixture-orchard |
| cloud | e:demo-dot | fixture-orchard |
| cleo | c:demo-cleo | fixture-lighthouse |

`a07-together` belongs to Cleo in Lighthouse. Its root, draft and retained message destinations
carry that exact identity, including `scope.agentId:'cleo'`; sent files say `availableTo:'Cleo'`.
The separate composer draft keeps its one file. Page compatibility must validate the selected
binding's exact project; accepting either project for any agent is insufficient. This table grants
no native lookup or permission. Approval and native pending controls remain the page's responsibility;
attachment snapshots add no permission, pending or steering state.

Only `source_unavailable` and `forwarding_refused` Motet answers add optional
`noticeParts:{title:string,body:string}`. Both parts are plain text, never HTML; style by existing
`answer.state`, without splitting prose or inventing text. Legacy `notice` is exactly
`noticeParts.title + ' ' + noticeParts.body`. The refusal body includes “With a file attached,
Motet can analyse it; it does not act on it.” Its text remains null, its draft remains unsent and
its model/request correlation stays null. The gone answer has empty `actions`, null text, invalid
sources and unavailable citations; its historical source may advertise separately bound reattachment. Neither
notice introduces a callable action or restores deleted content. Returned notice parts are detached
JSON values. Other seeded answers omit `noticeParts`.

## In-memory actions

Every draft action supplies `type`, `draftId`, `destination`, `revision`, and `writerGeneration`, plus only the fields
listed below. The destination is the complete snapshot value, not a name lookup. A mutation of an existing draft advances
its revision. Send/Create replace the draft identity and may start the new draft at revision 1; the old identity and writer binding no longer authorize edits. Read the returned snapshot before the next action; stale callbacks reject without changing state.
Selecting a scenario rotates writer generation too, so callbacks from an earlier copy of that scenario reject.
Non-editable drafts reject ordinary edits. Their explicitly advertised Resume/Create actions may act on them; a missing draft never offers Resume. A conflict tray also permits bound metadata-only `show_details`/`close_details` for its selected private files, without takeover or preview access. The page cannot override that by changing its copy.

| Type | Additional fields | Effect in the fictional state |
| --- | --- | --- |
| `open_picker`, `close_picker` | none | Open/close the sample picker. |
| `add_sample_files` | `fileIds: string[]` | Add 1–5 static samples within the draft/recipient limits. |
| `remove_file` | `fileId` | Remove exactly one selected non-uploading private draft file. |
| `cancel_upload` | `fileId` | Remove exactly one uploading draft file. |
| `show_details` | `fileId` | Select details for that exact draft file. |
| `close_details` | none | Close details. |
| `edit_text` | `text` | Keep the owner's exact text, validating current recipient limits. |
| `send_sample` | none | Queue the exact ready draft text/manifest in the fictional state, then open a new empty draft. |
| `retry_preparation` | `fileId` | Retry one selected failed file only when it advertises that action. |
| `resume_draft` | none | Take over the saved draft, preserving its exact text, ID and ordered files while rotating writer generation. |
| `create_draft` | none | Open a distinct empty draft without transferring or deleting earlier private files. |

`pickerFiles` holds the three choices only while the picker is open, and is empty otherwise. Their stable sample
IDs are `fixture-file-plan`, `fixture-file-shot`, and `fixture-file-req`. They are selection templates, independent
of `files`, which normalizes current drafts, messages and history. Each accepted selection gets a new file ID;
a previously sent file with the same filename is never reused or changed. Real File/Blob objects, URLs, byte
arrays and extra fields reject. Opening the picker does not select a file. Selecting into an empty text field
adds the editable suggestion “Review the attached file”. The limits are 2000 Unicode code points for an agent
and 4000 for Motet; null characters reject. There is no automatic truncation. Removing the final draft file clears the text only while
`draft.suggestedText` still marks the untouched suggestion; removing an earlier file does not. Any accepted
`edit_text` takes ownership of the text, even when it equals the suggestion exactly. That text is preserved.
The shared removal/cancellation handler returns the canonical empty-draft and Send state; the page must use
that returned text rather than reconstructing or clearing it locally.

`send_sample` checks every selected file; blocked files are never omitted. It records no actual send. It updates
an open Files history with the same request and manifest. Motet requests queue only, without synthesizing an
answer to arbitrary text. The seeded `a08-forward` refusal blocks Send and preserves the draft. Editing its
text clears that old refusal; this fixture has no natural-language forwarding classifier and proves no semantic
screening capability. Stage samples accept the same bound sample selection and Send actions. They remain
analysis-only Motet requests for the whole stage: no agent delivery, stage action or live call is performed.

`filesCount` supplies the closed Files-entry count. Draft files and picker choices do not count. The count can
include gone files represented in history; its presence is not a claim that their bytes remain available.
`a05-saved` deliberately keeps count 1 as incomplete unlisted fictional history: no fabricated row and no
open-Files authority. `a10-reattach` has five explicit one-file historical entries and an additional new
private survey selection; count remains 5 until Send, then 6. `a11-full` counts Ada's three associations;
Bram's fourth association stays global only. Partial cleanup preserves count 1, while Finish makes 2.

## Phase 2 state and recovery actions

`draft.blockCode` is a structured reason (`empty`, `files_preparing`, `file_errors`, `folder_unreachable`,
`recipient_unavailable`, `resume_required`, `draft_conflict`, `draft_unavailable`, `storage_capacity`, or null). `noticeCode` is
`saved_recipient`, `saved_scope`, `resume_available`, `draft_conflict`, `draft_unavailable`, `reattached_new_draft`, or null.
These are derived from canonical states after edits/removals/retries; the page must not parse fallback prose.
In particular, editing text does not erase a folder-specific block. A successful retry prepares only its exact
selected file in private staging. A storage-full retry remains blocked while storage is full. Both advance the
draft revision. Locked/type/budget failures never acquire Retry.

In a `conflict` draft only, a metadata chip can open details for an exact selected private file. Closing requires
those details to be open. These actions retain the conflict, writer generation, text, files, read-only status and
lack of preview access; they advance only the draft revision to fence stale callbacks. They do not authorize
editing, removal, retry or Send. `resume_available`, missing and unsupported drafts do not get this exception.

The resume sample carries its actual two fictional saved files and text, with `editable:false`; the page can
show the banner without rendering its tray as editable. Resume preserves that saved draft and rotates its writer
generation. Create instead uses a new ID and empty text/manifest. The older private file metadata remains, but
its IDs are not in the new draft and cannot be edited through it. Neither action sends anything or makes a claim
about prior delivery.

Messages now expose `revision`, `operationGeneration`, `deliveryPhase` and
`cleanup:{state,fileIds,releasedBytes}`. Recovery actions use the message binding below, **not** composer draft
identity. Extra draft fields reject. Each action validates the exact retained message and advertised capability,
then state-specific preconditions; it cannot be redirected to a new composer draft.

```js
const view = fixture.snapshot();
const message = view.messages[0];
fixture.act({
  type: 'check_status', // The uncertain sample advertises this action.
  messageId: message.id,
  requestId: message.requestId,
  destination: message.destination,
  revision: message.revision,
  operationGeneration: message.operationGeneration,
});
```

| Message action | Guard and fictional result |
| --- | --- |
| `check_status` | Exact uncertain message advertising this action remains uncertain; its revision advances. No resend. |
| `finish_send` | Proven `partial_not_sent`/`pre_dispatch` plus `canFinish` completes the original missing copies and queues the **same** request, text and ordered manifest. |
| `delete_saved_copies` | Same proven pre-dispatch partial request plus its cleanup capability removes only its saved workspace copies, fences Finish, and records `not_sent`/`cleanup_fenced`. |

Finish and cleanup are mutually exclusive. Their first accepted result disables both recovery capabilities;
fresh repeated commands and stale commands both reject. Neither action creates a message, request ID or native
attempt. They do not edit, clear or consume the new composer draft. Unknown delivery offers status checking
only. Scenario reselection rotates message operation generations to reject callbacks from an earlier instance.

Cleanup reports `cleanup.state:'saved_copies_deleted'`, the exact materialized file IDs and only the workspace
bytes released. Their availability becomes `not_saved`; retained source bytes are **not** claimed deleted.
The old message remains in the history index. `filesCount` is an index count, so cleanup does not equate a smaller
byte quota with erased history. This action never opens the phase-4 delete dialog or enables Manage files.

Every snapshot has `lastAction`, initially null. Phase-2 actions return `{type,targetId,outcome}` with one of
`preparation_ready`, `preparation_blocked`, `draft_resumed`, `draft_created`, `still_unknown`,
`queued_same_request`, or `saved_copies_deleted`. Create targets the newly created draft ID; other results target
the exact draft/file/message acted on. Accepted phase-1 actions clear any old result. Rejected commands leave the
whole snapshot unchanged, including the result. These are fictional transitions, not live receipts.

## Phase 4 operations

Management commands use exactly `{type,authority,...listedFields}` and copied current authority, not the legacy draft
binding. Per-factory `instanceId` fences new authorities across instances in one module; every page command
also needs exact client-object/lane fencing across replacement or module realms. Legacy command shapes
are preserved and are not claimed to gain this namespace. Snapshot reads are detached and observational:
no generation allocation, cleanup progression, timer, result synthesis or mutation.

`management.sourceRefs` supplies direct preview-only rights for supported editable private/current sent
sources. `management.history` projects the exact active destination; `management.storage` retains global
ownership. Opened views supply their own generation-bound rights. Context/catalogue/view authorities
are distinct; display file actions never confer permission. Historical message-less rows carry null
message IDs. Global Bram Check status uses its retained legacy message binding and stays unknown;
Ada's composer/messages are preserved. Global Finish/cleanup is not authorized.

The command families are:

- Files/storage: `open_files`, `close_files`, `open_storage`, `close_storage`.
- Preview: `open_preview`, `open_citation`, `preview_page`, `preview_zoom`, `close_preview`.
- Deletion: `request_delete`, `confirm_delete`, `keep_file`, `close_delete`, `open_delete_result`,
  `advance_sample_deletion`.
- Fresh selection: `begin_reattach`, `cancel_reattach`, `select_reattach_sample`.

Every successful new action returns a complete detached snapshot and a monotonic matching
`management.actionReceipt`; rejected commands preserve all state. Copy the result into the accepted
consumer state only after typed transition/identity validation. Do not use scenario selection as an
interaction, mutate a shadow manifest, or assume repaint refreshes canonical state. All exact command
fields and authority targets are exercised in [the fixture tests](fixture.test.mjs).

Deletion operations outlive their separate dialogs. Request/confirmation releases no bytes; confirm
immediately fences sources, clears preview and invalidates dependent answer/use labels/citations.
Closing confirms nothing and never cancels cleanup; a closed unconfirmed operation becomes abandoned.
Only explicit exactly bound `advance_sample_deletion` consumes immutable seeded per-copy outcomes.
Release verified owned-copy bytes once; changed/failed copies remain accounted and unavailable. Usage
is the sum of accounted owned copies, not Files count. The former opaque background balance is fully materialized as bounded fictional inventory; `backgroundBytes` is zero. Request state/history/model
attribution remain intact. Previously returned detached data cannot be erased from caller memory;
consumers must discard invalidated DOM/cache and reject old callbacks.

Reattachment begins only in opened same-recipient Files with exact history/source authority and current
`DraftFence`. Its picker advertises fresh plan/shot/req/table templates; no tombstone bytes are restored.
Begin/cancel preserve draft; cancel leaves Files open. Selection rechecks both picker/history and exact
current draft, limits and writable capability. Exactly empty text (`=== ''`, not trimmed) plus zero files
replaces the empty draft ID/writer and adds `reattached_new_draft` notice. Nonempty selection preserves
ID/writer/text/file prefix, appends a fresh private file and increments revision without that notice.
No plus-picker suggestion, implicit Send, A05 takeover or draft switching. Success closes picker/Files;
receipt says `new_draft` or `added_to_draft`. Other same-destination saved drafts remain untouched.

The coupled page adapter must validate/adopt every returned snapshot and preserve unrelated state, with
only the prescribed reattachment composer change allowed, plus capacity-derived readiness after a validated
copy-ledger capacity change: only canSend/blockCode/sendBlockedReason may change. Draft identity, revision,
writer, destination, text, ordered files, editability and notices remain unchanged; view-only actions cannot use
this exception. This lets verified cleanup make ready files sendable again without treating the draft as edited. Its demo progression registry survives lane
teardown, keyed by exact client + instance + deletion operation. At most one checkpoint may call advance;
mark attempted before calling. Close/re-entry can schedule only never-attempted operations after canonical
validation. A throw or malformed/unadopted result never authorizes automatic retry. One read-only
reconciliation may establish completion; otherwise retain local “Completion could not be verified” across
close/re-entry with no Retry/second mutation. That is consumer presentation, not a fixture backend state.

## Claims kept separate

- Ready draft files are private. Available agent files are materialized copies, not proof the agent read them.
- Queueing, uncertain delivery, partial materialization and recorded answers remain distinct message states.
- Unknown delivery offers status checking, never the partial-failure recovery actions.
- The partial sample represents a proven pre-dispatch stop. Its Finish action describes recovery of the same
  authorized request. The phase-2 actions above simulate that result in memory only; no real recovery or deletion runs.
- Motet citations distinguish uploaded sources from recorded activity. Deleted sources hide dependent answer
  text. Used-in-answer never implies an agent opened a file.
- Delete samples keep queued messages queued. Partial cleanup releases only removed-copy bytes, leaves a changed
  workspace copy alone and reports it missing rather than available. Local deletion never claims to erase native
  messages, activity, agent-made copies, or provider retention.

Run the focused offline checks with:

```sh
node --test docs/fixtures/attachments/fixture.test.mjs
```

The focused suite is the verification entry point for scenario references, detached/default-off state,
exact actions, authority fences, bounded authored content, deletion, counts and recovery. These checks
cover fixture behavior only.

When ready files require delivery copies that will not fit, `draft.blockCode` is `storage_capacity`, `canSend` is false, and `sendBlockedReason` is "Send is off until attachment storage has room for these files. Open Files to free some." The existing blocked-Send presentation handles it; no additional control exists. Every new copy allocation is checked incrementally before mutation, accounting for copies already present.


## Explicit expiry and complete storage inventory

The expiry samples cover setting and clearing a date for one file or for all files currently stored. `File.retention`
adds `expiryPolicy` with generation, nullable chosen date/zone/deadline, typed state, operation ID, actual
`removedAt`, and `removalTiming`. `expiresAt` is a compatibility projection of the deadline, never a
permission to delete. The ordinary default is `state:'none'`; the dated scenes are explicit choices.

`management.expiryDialog` carries the exact selected IDs, source/policy generations, computed file count
and physical bytes, proposed date, minimum date, fixture zone, rights and typed reason. Open a single
file through its **storage source authority**, or All through the **open storage view authority**.
Set/Clear/Refresh/Close use the currently exposed expiry-dialog authority. The commands are
`open_expiry`, `open_expiry_all`, `set_expiry` (`date` only), `clear_expiry`,
`refresh_expiry_selection`, `close_expiry`. They return the existing matching `actionReceipt` shape.
No page code should infer rights from a date label or assemble its own file list.

An undated unfinished file has no individual first-date setter. All can date it, after which its own
Expires/Due since link can change or remove the date even while unfinished. Neither setting nor clearing
changes delivery state. Once cleanup intent exists, its date cannot change. One committed cleanup blocks
an entire All mutation, with Close/Check again instead of silent omissions. A changed roster, policy,
source or copy generation applies nothing on the first confirmation; it refreshes the exact selection,
preserves the proposed date and asks for a second explicit press. Later attachments do not inherit a date.

All captures the complete canonical retained-file inventory, including other destinations and reachable
drafts. `management.storage.entries` remains the existing known request list; `draftEntries` describes
retained drafts; `inventoryFiles` supplies additional fictional file metadata. `inventoryPage.entries`
pages the other-destination request rows, 20 at a time, with only advertised previous/next offsets accepted
by `storage_inventory_page`. `inventoryComplete:true`, `inventoryFileCount` and zero `backgroundBytes`
mean there is no hidden physical balance omitted from All. Each background file is at most 10 MiB.
The baseline has **62 retained logical files, 1,200,000,000 physical bytes**; the full sample has 244 files
at 5,000,000,000 bytes. The fixture calculates both counts. Active Files
history and its count remain scoped. Inventory metadata includes historical unavailable records after
cleanup; `inventoryFileCount` and the All selection count only files with remaining accounted copies.

The fixed fictional clock starts at 14 November 2030 in `Etc/UTC`. Today and future dates resolve to the
exclusive UTC boundary after the selected calendar day. Dates outside the authored 2030 range, invalid
calendar values and past dates reject unchanged. This proves neither production timezone/DST handling
nor restart scheduling. **Only offline fixture tests/scenario controls** may use the factory's additional
`advanceSample` seam; it is not a page command, backend route or provider evidence:

```js
fixture.advanceSample({type:'advance_clock', at:'2030-12-01T00:00:00.000Z'});
fixture.advanceSample({type:'evaluate_expiry'});
// A fictional authored completion, never a real status check:
fixture.advanceSample({type:'complete_request', requestId:'fixture-request-1'});
```

These checkpoints evaluate current policy and every request/draft dependency. Queued, unknown, preparing,
Finish-eligible and resumable files defer indefinitely until verified fictional settlement. Eligible
expiry enters the existing deletion intent and source/answer invalidation path. Copy outcomes still
advance only with the exact `advance_sample_deletion` authority. No automatic retry or cancellation.
Only complete verified removal becomes `expired`/`removed`; partial and unknown outcomes retain
accounted bytes and unavailable sources, without successful-removal wording. `date` and `removedAt`
remain distinct. `removalTiming:'on_date'|'delayed'` and the operation's `cause.deferred` support honest
chosen-date versus actual-removal copy. Manual deletion keeps its existing wording.

`a04-scanned` describes the fictional Ada adapter that cannot read this particular scan. It has one
precise `scanned_pdf_unreadable` error, Remove only, no preview and a singular Send-is-off line. It does
not claim that every provider lacks scanned-PDF support; no OCR output is fabricated.

The expiry/scanned tests cover exact physical accounting, dates and foreign/stale authority rejection,
zero-effect first confirmation, future/overdue unfinished edits, retained draft and multiple-request
protection, cleanup intent races, partial/unknown accounting, revoked Motet sources, and forbidden
external effects. They are offline contract tests, not provider, persistence or production scheduler proof.


## Original-file relay samples

Seven relay scenarios use explicit `capabilities.preparationModes` and file
`preparationKind`, `previewAvailable` and `preparationText`. Native
PDF/PNG/JPEG samples retain their original relay mode through retry, Send,
status and reattachment. Their preview surface contains metadata-only fallback,
never authored pages, pixels or extracted text. `previewable` can allow that
fallback while `previewAvailable` stays false. Motet PDF/image selection is
refused in the relay profile. Existing rendered studies use the fixture-only
`authored_sample` mode and are not production qualification.

## Motet direct-input samples

The additive `a09-officer-*`, `a04-officer-*`, `a08-officer-*` and
`a06-officer-retained` and `a06-officer-rejected` cases expose explicit delivery/refusal/preview states for
the Motet PDF/PNG input path. They have no rendered document or image preview and make no provider requests.
The retained message says “Kept in this conversation · Motet was not
asked”; its metadata fallback describes the local save. `a09-officer-retained-earlier` keeps confirmed earlier supply while
its retained preview says “not asked this time”; `a09-officer-retained-unknown`
uses the neutral sentence. `Preview.officerPriorDelivery` explicitly selects
none, confirmed or unknown only for retained-local previews, and is null
otherwise. Missing history is never treated as evidence that nothing was sent.
The rejected message says “Not answered” and provides explicit `failureParts`
for the failure block, with no Retry. Its delivery remains unknown. The failure card uses the
exact retained message and file IDs.

Reattachment advertises current-profile sample sizes, identical to selection
and storage charging: 128,000 bytes for the direct PDF, 64,000 for the direct
PNG. Native relay and text sample sizes are unchanged. Unsupported choices
still refuse without mutating the draft; the page's exact-byte guard remains.
