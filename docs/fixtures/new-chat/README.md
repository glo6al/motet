# New chat page fixture

This pure ES module provides seven fictional page states without launching an app, contacting a host, registering an agent, or changing a live stage. It is documentation/demo code only. Nothing imports it into the server or production bridge. All folders, host identifiers, sessions, and agents are invented.

```js
import { createNewChatFixture } from './fixture.mjs';

const adapter = createNewChatFixture({
  scenario: 'added',
  now: () => Date.parse('2030-01-01T12:00:00.000Z'),
});
const context = await adapter.context();
const selected = {
  provider: 'codex',
  folderId: context.folders[0].id,
  folderRevision: context.folders[0].revision,
};
const intent = await adapter.prepare(selected); // No launch.
const attempt = await adapter.open({
  ...selected,
  requestId: intent.requestId,
  draft: 'Review this fictional project.',
});
adapter.advance(1_000); // Fixture control only; never a proposed server endpoint.
const result = await adapter.status(attempt.attemptId);
```

`now` is an optional clock function returning epoch milliseconds or a `Date`; the default uses the current clock. `advance(ms)` adds a nonnegative offset. Status calls drive fixture observation; `advance` does not run a background observer. If the next observation occurs at or after the deadline, it times out before processing a scheduled result. An explicit continuation can observe that result within the new window. The scenario is selected once per fixture instance. Create a new instance to switch scenarios. Context is a defensive copy of the exact fictional folder inventory, host, provider availability, safe unavailable reasons, and draft capabilities.

| Scenario | Observable behavior |
| --- | --- |
| `waiting` | Accepted launch; waiting until the 120-second observation deadline. |
| `confirm` | Claude Code only: after one second, an explicitly observed native folder-confirmation prompt; session stays null. Expires at the deadline. |
| `added` | After one second, a verified matching fictional session and agent on the stage are attached. |
| `timeout` | Accepted launch; expires after 120 seconds without a matching observation. |
| `mismatch` | After one second, an observed chat has a different folder; separate mismatch evidence identifies its actual folder and an already independently observed monitor. No session or agent is attached by this attempt. |
| `failed` | Immediate fictional launch rejection; observation complete. |
| `unavailable` | Immediate unavailable response; launch count is zero. |

Codex and Claude Code are available on the normal fictional host. Antigravity and ChatGPT Work remain unavailable in every scenario. The `unavailable` scenario disables all providers. A native Claude confirmation must be an observed signal in a real implementation; selecting a provider or seeing a composer does not establish that signal. This fixture does not establish live capability.

All adapter operations except `advance` return promises. `prepare({provider, folderId, folderRevision})` mints a fresh UUID intent for an immutable provider/folder choice and returns `{schemaVersion:1, fixture:true, requestId, provider, folder, expiresAt, host}`. It never launches, including for unavailable providers. The first open must occur before the five-minute intent expiry. An arbitrary UUID, an old fixture instance's UUID, and an expired never-opened intent cannot launch. The fixture uses built-in Web Crypto (`randomUUID` and `subtle`), available in modern secure browser contexts and Node.

`open` accepts only `requestId`, `provider`, `folderId`, `folderRevision`, and optional `draft`. The request ID must come from `prepare`; the provider/folder must match that intent and the folder ID and string revision must match context exactly. Arbitrary paths, unknown fields, stale revisions, unknown providers, changed intent choices, and drafts longer than 8,000 Unicode code points are rejected with a `NewChatFixtureError` carrying a stable `code`. Empty draft text normalizes to omitted: `provided:false`, `disposition:'not_provided'`, and the same idempotent payload.

Replaying the same request ID and payload returns the same attempt at its current state, even after intent expiry. A changed payload is rejected. Payload field order and UUID casing do not create new attempts. Duplicate concurrent opens also share one attempt: after asynchronous HMAC calculation, the adapter checks and reserves the intent without another asynchronous boundary. The fixture stores at most 128 intents and attempts in memory and rejects new intents at capacity, retaining issued IDs and replay behavior rather than evicting them. It has no persistence, imports, network calls, app launches, process execution, timers, or background observers.

The live service retains ordinary attempt details for 24 hours and an expired-attempt marker for another 24 hours. Unknown or expired IDs cannot authorize a new launch. Managed Claude ownership has separate retention. This fixture keeps its state in memory.

Every response has the versioned attempt contract: `schemaVersion`, `attemptId`, `requestId`, `provider`, the selected `folder`, `state`, `launch`, `observation`, ISO `createdAt`/`deadlineAt`, nullable `session`, `mismatch`, and `reason`, `draft`, `capabilities`, and `fixture: true`. Only `added` attaches a matching session and agent on the stage. Response objects cannot mutate fixture state.

`mismatch` is null except in the mismatch result, where it contains `{sessionId, displayPath, existingRoomAgentId}`. Its actual display path differs from the selected folder. `session` remains null: mismatch evidence is not a success binding. The fictional `existingRoomAgentId` represents a monitor already observed independently by Motet, never a monitor added by this attempt. The page can offer “Show it on its own” by navigating that existing desk when the ID is nonnull; hide that action if no existing monitor is known. This needs no new endpoint or action.

At the deadline the observation stops with `state: 'timeout'`; status and replay never launch again. `keepWaiting(attemptId)` is available only after timeout and resets the observation deadline to the current time plus 120 seconds on the same attempt. It preserves the original creation time and launch count. An explicitly stopped attempt cannot resume or add a session later. `stopWaiting(attemptId)` is idempotent and leaves completed results stable. Added, mismatch, failed, and unavailable results are stable; timeout changes only through explicit continuation or stop. A wall-clock correction cannot undo observed progress.

Drafts are optional local text. Only Codex accepts nonempty drafts and reports `prefill_requested` after an accepted launch; this records fixture intent, never delivery, verification, or sending. Other providers reject nonempty drafts with `draft_unsupported` before dispatch or spending the intent. Unavailable or rejected Codex launches report `unverified`. The protocol's `unsupported` disposition remains available for capability display; an unsupported submission is an error. Draft content is not included in responses, sessions, or error messages. It exists transiently in the input and hashing operation; retained fixture records contain only draft presence and an HMAC-SHA256 payload digest under an ephemeral per-instance key. The adapter exposes no send operation. This in-memory fixture does not implement the production store's retention, authentication, or crash durability.

The live New chat routes correspond to these fixture methods. The fixture calls the methods in memory.

| Live route | Fixture method |
| --- | --- |
| `GET /api/new-chat/context` | `context()` |
| `POST /api/new-chat/intents` | `prepare(body)` |
| `POST /api/new-chat/attempts` | `open(body)` |
| `GET /api/new-chat/attempts/:id` | `status(id)` |
| `POST /api/new-chat/attempts/:id/observe`, body `{ "action": "continue" }` | `keepWaiting(id)` |
| `POST /api/new-chat/attempts/:id/observe`, body `{ "action": "stop" }` | `stopWaiting(id)` |

Fixture scenario selection and `advance` stay outside that route contract. The live service validates requests independently, correlates native evidence with the selected folder and request, and checks provider capabilities before a launch. The fixture simulates these states in memory.

Run the built-in Node tests from the repository root:

```sh
node --test docs/fixtures/new-chat/fixture.test.mjs
```


### Creation versus opening

Attempt snapshots also expose `creation: {outcome}`, `uiOpen: {attempts, outcome}` and nullable `existingSession: {id, folderId}`. A verified existing native session is separate from `session`, which still requires stage admission. `uiOpen.outcome: accepted` records a handoff request, not visual confirmation. `capabilities.openExisting` is false in this fixture.

Use `createNewChatFixture({scenario: 'failed', failurePhase: 'ui'})` to render a created chat whose window could not be opened. It returns `failed`, `creation.outcome: verified`, `uiOpen.outcome: rejected`, a verified `existingSession`, and no stage binding. It must not display “nothing was created” or offer automatic recreation. The default failure phase is `creation`, which demonstrates a rejected request before creation. These are two variants of the same failed state, not a new eighth state.

The live release enables New chat for verified local providers on macOS, and for Claude Code only on Windows. Codex requires desktop version `26.1103.60933`, build `25894`, on macOS. Starting Claude Code from Motet requires the verified Claude Code `2.1.323` executable for the host's platform and a validated `~/.live-room/managed-claude-readiness.json` (`%USERPROFILE%\.live-room\managed-claude-readiness.json` on Windows) for the host and user. This release has no self-serve setup for that record. Live New chat routes require the owner key. A server started with `WATCH_DEMO=1` refuses launches. The browser’s `?demo` flag selects the fictional UI, which has no live-adapter fallback and needs no owner credential. See [Files and New chat](../../PROVIDER_SUPPORT.md#files-and-new-chat).


## Managed Claude companion

`managed-fixture.mjs` exports `createManagedClaudeFixture(scenario)` for the managed Claude request and response shapes.
It is fictional and in-memory; it never calls HTTP, starts a process or enables the feature. It changes no page
appearance. The existing seven discovery scenarios remain in `fixture.mjs`. Run the companion checks with:

```sh
node docs/fixtures/new-chat/managed-fixture.test.mjs
```

The companion mirrors `context`, `prepare`, `open`, `status`, `managedStatus`, and `decidePermission`. Select
provider `claude_code`, mode `work`, and the folder ID/revision from context. For example:

```js
import { createManagedClaudeFixture } from './managed-fixture.mjs';
const fixture = createManagedClaudeFixture('permission_multiple');
const context = await fixture.context();
const folder = context.folders[0];
const selection = {provider: 'claude_code', mode: 'work', folderId: folder.id, folderRevision: folder.revision};
const intent = await fixture.prepare(selection);
const attempt = await fixture.open({...selection, requestId: intent.requestId, instruction: 'Fictional orchard task.'});
const run = await fixture.managedStatus(attempt.managed.runId);
const request = run.permissions[0];
const receipt = await fixture.decidePermission(run.runId, request.id, crypto.randomUUID(), 'deny');
```

Create a separate fixture instance for each screen. Scenario selection is a design harness control, not a server
route or user capability. Returned objects are defensive copies; the instances do not share mutable state. The
clock is fixed at `1893499500` Unix seconds for repeatable screenshots. It does not tick, deny requests in the
background, or simulate provider work. Choose a terminal scenario to display that outcome. Format server times
for the user's locale; do not start a fresh timeout when the page mounts.

| Scenario | Screen / facts to exercise |
| --- | --- |
| `starting`, `working`, `failed` | Existing startup, working and exited-run examples. |
| `permission` | Early permission before admission: `session: null`, reachable through `managed.runId`. |
| `permission_desk` | One actionable request in an admitted session. |
| `permission_multiple` | Three pending requests, oldest first, separate identities/deadlines/answers. |
| `permission_long` | Complete input containing 214 content lines; nothing is truncated. |
| `owner_allowed`, `owner_denied` | Owner decision with `decisionState: written`. |
| `deadline_denied` | Denial written at the deadline, reason `timeout`. |
| `withdrawn`, `turn_ended`, `native_exit` | Distinct no-longer-asked reasons; no decision was made. |
| `oversized` | Automatic denial, full input not retained (`inputAvailable: false`). |
| `unsupported_tool`, `capacity` | Automatic denial for unsupported tool or approval/evidence capacity. |
| `inactive_turn` | Automatic denial of a tool request outside an active turn. |
| `owner_allow_unknown`, `owner_deny_unknown` | Owner's answer could not be confirmed; never label it written. |
| `automatic_deny_unknown` | Motet tried to deny at the deadline; write outcome unknown. |
| `uncertain_readable` | Last-seen pending evidence remains readable, but neither answer is available. |
| `uncertain_unreadable` | Contact lost, no readable evidence; empty permissions and `evidenceAvailable: false`. |
| `uncertain` | Compatibility alias of the readable contact-loss example. |
| `history` | Latest eight of ten fictional terminal rows, plus one pending row; no full-history promise. |

Before launch, `context.limits.managedPermissionTimeoutSeconds` supplies the same authoritative 120-second limit
for the dialog copy. The attempt supplies `managed.runId` for early permissions. In production, the selected
agent's owner capabilities also carry `managed: {runId}` bound to its admitted native identity; use that binding
to restore desk status after the banner is dismissed or the page reloads, including during a disconnect. Do not
infer a run ID from an agent name. This fixture has no separate owner-capabilities route.

Every `managedStatus` response has `timeoutSeconds: 120`, `serverTime` (Unix seconds), `evidenceAvailable`, run
state/recovery flags, permissions and capability flags. Each permission carries `createdAt`, `expiresAt`, nullable
`decision`, `resolutionReason`, `decisionState` and `resolvedAt`, and `inputAvailable`, alongside the original
`id`, `tool`, `input`, `state`, `canAllow`, `canDeny`. The timestamps are Unix seconds, unlike the ISO timestamps
in the older discovery fixture. Pending requests have null decision/resolution fields. A lost-contact pending
row keeps its original deadline and evidence but disables its actions: the page must not claim that deadline
actually caused a denial. `evidenceAvailable: true` means the retained snapshot was readable, not that all of its
historical request inputs were retained. Inspect each row's `inputAvailable` too. Do not render an unavailable
input's empty object as complete approval evidence.

Terminal reasons distinguish `owner`, `timeout`, `withdrawn`, `turn_ended`, `native_exit`, `oversized`,
`unsupported_tool`, `capacity`, and `inactive_turn`. The production contract additionally permits `shutdown`.
`decisionState: written` records the response written to Claude; it does not establish that the tool ran.
`unknown` never becomes a successful answer merely because a decision was chosen. Automatic decisions are
always denials. Cancelled rows have no decision or decision-write state.

A successful `decidePermission` returns `state: written`, `decisionState: written`, `resolutionReason: owner`,
`resolvedAt`, and the request/permission/decision binding. Identical request-ID/permission/decision replay returns
the retained receipt without changing timestamps. A changed decision, another decision ID for the same permission,
or reuse of one decision ID against a different permission is rejected. Only the selected request resolves;
others remain pending. Terminal and uncertain scenarios cannot accept answers. This fixture does not simulate
message sending; a normal message is not a permission decision in the real implementation either.

`uiOpen.outcome` remains `not_required`; this never claims a Claude Desktop window opened. Existing method names
remain compatible. No phone/remote-access capability is implied by these in-memory examples.
