# Roadmap

**One workspace for your AI conversations—across Claude, Codex, Cursor, and more.**

Motet brings supported sessions, their context, and their available capabilities into one interface. The stage provides an overview; the desk brings a conversation and its work together. Multi-agent supervision is one use of that workspace. The broader goal is to help you find, understand, and continue a conversation without having to remember which tool holds it.

This is a working study. The sections below distinguish shipped behavior, investigated gaps, proposed priorities, and longer-term ideas. Priorities are a direction for planning, not release dates or promises of access to a provider's private interfaces. The [capability table](PROVIDER_SUPPORT.md#current-provider-capabilities) is the reference for what users can do today.

## What is next

Planned work after v0.1.0, nearest first. "Designed" means the screens are drawn and agreed; it does not mean the work has started. The numbered [priorities](#priorities) further down are the longer view.

### Designed, not built

- **Dictation on more devices.** Optional local desktop dictation uses a separately installed free recognizer and displays editable words before Send. Phone capture needs a reviewed secure connection; broader native-platform support also remains. See [local dictation](DICTATION.md).
- **A live browser at the desk.** A real page you open and operate yourself in a desk's Browser tab, kept apart from the record of what the agent's tools fetched. Nothing opens until you ask.
- **Permission requests at phone width.** The same request cards on a phone-sized screen. Today a request can only be answered on the computer that runs Motet; answering from another device has to be proven safe before this is built.

### Asked for, not designed

- **Enable file delivery for Claude setups first created with an older Python.** Design an explicit, owner-approved adoption or migration of their existing managed-chat storage before enabling files. Do not migrate automatically. Preserve ordinary messaging, retained identities and every existing file-protection check throughout. The notice people see today will also be reworded, because it applies to every agent and not only Claude.
- **Steer a queued message, answer an agent's own questions, and change its model or effort from Motet.** Still to be evaluated; see [priority 2](#2-make-actions-reliable-within-the-conversation).
- **Find and return to older sessions.** See [priority 1](#1-make-supported-conversations-easy-to-find-and-return-to).
- **Git state per session, New chat for more apps, linked handoffs and review verdicts, and optional holds.** See [priority 4](#4-deepen-work-context-and-coordination).

## Available today

- **Supported local sessions:** observation of Claude Code and Codex session logs and Cursor and Antigravity transcripts. Recently active sessions appear by default; this is not an import of every chat or full account history. Codex cloud work on this computer has command observation only.
- **A shared conversation interface:** recorded messages with formatted responses, provider labels, project context, and parent–child relationships. The conversation is central at the desk; the composer remains plain text.
- **A roomier message box:** on the desktop stage, suggested questions sit above the box. The top row carries recipient and model choices and an arrow that opens the conversation. Your words, microphone and Send sit below.
- **Work beside the conversation:** a sliding, resizable panel with Preview, Changes, and Activity. Evidence is limited to what the provider records; Changes shows recorded edits, not the Git working tree. Narrow layouts switch between conversation and work.
- **Overview and attention:** the stage, project filters, compact agent navigation, fixed Alerts/Activity controls, possible-request notices, and optional browser notifications. Relationships and requests are labelled as recorded or inferred.
- **Original and Neutral themes:** the same interaction model with different palettes. Original remains the default.
- **Direct messaging where verified:** messages queue to eligible local Codex chats. Motet can also send to an exact local Claude chat that has registered through the Motet plugin and is connected. The stage and desk share drafts, histories and delivery records for each recipient.
- **Motet:** questions about recorded context, cited evidence, and explicit supported message/control requests. Local Codex steering and interruption are available through Motet on the verified macOS desktop build. This sends a new instruction or stops a turn; it does not promote an existing queued message.
- **New chat:** on verified macOS hosts, open a blank Codex chat or start work in a verified project folder, including one you choose in the Mac's folder window. Claude run by Motet starts work only with a supported local executable and a verified per-computer readiness record.
- **Saved project folders:** remove a folder you added from Settings → Connections → Project folders. Motet only forgets the folder; the folder itself, its chats and recorded work stay intact. A connected app that still knows the folder can keep it available.
- **Attachments:** keep a PDF or PNG in a Motet conversation or ask a file question with a current local spending policy. Eligible local Codex and plugin-connected Claude chats can receive PDF, PNG and JPEG files from the workspace. See [Files and New chat](PROVIDER_SUPPORT.md#files-and-new-chat) for host and chat requirements.
- **Questions and permissions:** optional Claude Code permission hooks support Allow/Deny when Answer here is enabled. Supported native Codex questions and selected model/effort can be observed on the verified build; answering those questions or changing those settings is not enabled.
- **Optional peer messaging:** the shared MCP bridge handles cross-provider messages between reachable Codex chats and eligible Claude chats. A Claude recipient must have a current plugin connection with agent messaging authorized. Peer messages confer no new authority and cannot invoke controls. An optional Antigravity plugin can send requests to Codex and Claude and retrieve replies; a separately configured shared pull inbox stores incoming requests, but never targets or wakes a native Antigravity chat. On Linux the bridge runs with Claude recipients only (Codex receives only on a Mac), checked by automated tests. On Windows it refuses with one reason until the handle-based folder walk exists there; its credential files and offline locks already use the shared private-storage layer, checked by automated tests, not yet on a real Windows PC.
- **Recorded session facts:** model, branch, context counts, summarisation markers, step timing, and stop hooks where available. Missing facts are shown as not recorded.
- **A read-only terminal view:** a board and event tail from the same stream, plus optional iTerm2 tabs or panes. Its validation limitations are listed below.

## Priorities

### 1. Make supported conversations easy to find and return to

The first product priority is dependable access to existing sessions across tools. The current activity window is useful for watching work, but is not a complete conversation library.

- Improve session discovery and navigation across providers and projects, with a clear distinction between a session, its child agents, and other sessions sharing a folder.
- Make recorded-history coverage and unavailable content understandable before someone mistakes a partial transcript for the full conversation.
- Give Motet durable, searchable memory from supported native logs while the app runs, with source citations, explicit coverage, opt-in recording, and retention/deletion controls. Historical questions belong in the normal Motet conversation; the memory UI is limited to settings and inspecting a cited source. Full-account and historical-log imports remain outside the shipped scope.

**Completion criterion:** a user can identify the intended supported session, see where it comes from and how much history is available, and return to it without confusing it with another conversation. Any new storage needs explicit retention and privacy rules.

### 2. Make actions reliable within the conversation

Provider integration work is already under investigation; these are unresolved capabilities, not completed features waiting for a button.

- **Claude messaging:** expand tested coverage of reconnection, identity changes and interrupted delivery. Keep both owner messages and peer delivery bound to the selected chat.
- **Queued-message steering:** send normally, then offer **Steer** on the queued message when supported. Motet uses that term across providers. Steering must consume that exact queued message once, without also leaving it to run later. If the run has finished, deliver normally. No provider-specific wording or substitute stop-only action should stand in for this flow.
- **Native questions and settings:** evaluate supported ways to answer ordinary questions and change selected model/effort, preserving the distinction between a selection for the next turn and the model recorded for past work.
- **Capability clarity:** keep observation, messaging, steering, interruption, question answering, and permission approval separate. An unavailable action needs an explanation and an appropriate route back to the original tool when available.

**Completion criterion:** each enabled action targets the intended session, exposes pending/confirmed/uncertain outcomes, survives retries without duplicate effects, and passes independent review and provider-specific verification. Direct actions should be available where the user is conversing, without requiring an Ask Motet detour.

### 3. Broaden provider and conversation coverage

Depth of support matters as much as adding a provider name. Candidates must expose a usable, verifiable source of conversation data or an action interface. No unverified integration is promised.

| Connection | Current boundary | Next support question |
| --- | --- | --- |
| Local Codex | Logs, eligible queued messages, verified active-turn controls; native reads only | Can Motet safely promote the exact queued message and support native replies/settings? |
| Claude Code | Logs, optional permission hooks and messaging to eligible connected local chats | How much supported lifecycle coverage can the exact-chat plugin provide? |
| Cursor | Transcript observation without tool results or direct messaging | What verified routes exist for session targeting, results, and actions? |
| Antigravity | Local transcripts, optional read-only metadata and verified desktop navigation | What evidence and actions can be supported across its desktop, CLI and IDE sources? |
| Codex cloud on this computer | Local commands only, no conversation | Is there an authorized interface for conversation history and actions? |
| Ordinary web chats and additional providers | Not imported | What access, permissions, and session identity guarantees are available? |

**Completion criterion:** every added connection has an explicit support matrix, visible limitations, a defined privacy boundary, and tests against real supported behavior. A provider badge must never imply all of its chats or capabilities are available.

### 4. Deepen work context and coordination

Once users can reliably find and interact with existing sessions, extend what they can do across them:

- Git state per coding session: branch, uncommitted files, and attributable changes as a basis for possible editing conflicts.
- Expand the existing New chat flow to cover more providers and support files in the initial instruction.
- Explicit handoffs and independent review verdicts linked to the relevant conversation and work. The existing peer bridge is a transport foundation, not a complete coordination workflow.
- Optional holds on deploy, push, and delete, only after the approval interface and provider hook behavior are verified. This expands the permission boundary and remains a later investigation.

**Completion criterion:** coordination preserves the selected session, owner intent, evidence, and delivery state. It must not create implicit authority between agents or silently move work to another provider.

## Validation and platform work

These run alongside the product priorities; broader messaging support does not remove them.

- **Windows:** run on one real Windows 11 PC ([report #4](https://github.com/glo6al/motet/issues/4)) and tested in CI. Watching works there, and Motet's own answers are checked so far only by the automated tests; the hooks (Allow / Deny) now install there and are checked by automated tests, but not yet against a real Claude Code prompt on a real PC; messaging a Claude chat through the plugin now starts there and on Linux, checked on Windows by automated tests but not yet on a real PC (including whether Claude Code loads the plugin there); Codex messaging stays Mac-only for now; everything else that acts on agents (the bridge, New chat, files, dictation) is not available on Windows yet (the bridge refuses with one plain reason, and its private files and locks are already ported, checked by automated tests, not yet on a real Windows PC), and more real-PC reports are needed. Preview assets need a directory-handle-based folder walk; until then previews serve the page alone, with Python 3.12 or later required. File IDs can be reused after deletion; 8.3 names, `subst` drives, and restart/port behavior still need testing. See [what differs on Windows](SETUP.md#windows) and the [Windows guide](WINDOWS.md) for setup and current restrictions.
- **Real devices and scale:** measure large session collections and previews; test actual trackpad gestures, physical phones, and short windows. Browser emulation and synthetic gesture events do not establish those results.
- **Provider operations:** the recorded live checks cover specific Codex steering/interruption cases and one Allow and one Deny on real Claude prompts. Expand coverage of provider updates, unavailable or stale targets, lost replies, and the native app's behavior while a request is held. Open-in-app links still need end-to-end validation.
- **Preview compatibility:** previews are known not to render in Claude desktop's built-in browser pane; they render in Chrome. This needs continued compatibility testing, not a claim of universal browser support.
- **Terminal view:** its first independent review findings were repaired, but the repairs have not had a second review. Amber tabs and badges have been checked as output codes, not against a real waiting agent.
- **Publication:** changes require independent review. Public examples and recordings use fictional data; live conversation text and command output can contain personal paths. Private investigation evidence stays local and ignored.

## Known limits

- Granularity is whatever the logs record: tool calls, their results, messages and turn boundaries, never keystrokes. For Codex, a step in flight is read from script text and marked unconfirmed until its finished record arrives. Hidden reasoning is not shown, because it is not in the logs.
- "No log entry for N" means the log is silent; it does not prove an agent is idle.
- Touching a log whose last timed record is outside the window does not make it active: for Claude Code and Codex the last timed record decides, by the log's own clock, so a log written by a clock more than the window behind this machine's stays out.
- Which agent started which is recorded for some cases and inferred for others; inferred links are labelled.
- Alerts and "probably needs you" notices are heuristics and can be wrong in either direction.
- Codex: messages between agents can be encrypted in its log, and permission prompts are not visible. Motet tracks questions it queues itself; this does not expose every queue in Codex.
- Without the opt-in Deck Log, Motet's evidence is limited to the stage's last few thousand steps. With it, Motet can also search retained selected native-log events and daily digests. Capture covers recording intervals while Motet runs, not older imports or offline work; omissions, retention, exclusions, and storage limits can leave gaps. Answers remain a model's reading of recorded evidence and can be wrong, so retained sources are shown beside them. Spoken replies are not available; dictating a draft requires the optional local helper. On Windows, Motet's key file and settings are checked by their access list instead of owner-only permissions.
- Codex cloud agents keep no transcript on this computer. Their monitor is read from the process list (the ChatGPT app's `codex exec-server`, which OpenAI marks experimental): commands and folders as they run, never their words, what they read, what they changed, or whether a command succeeded. The list is sampled about once a second, so a shorter command can be missed; of several new commands at once the oldest four are examined first, so a short-lived one among the rest can be missed; a process is told from another by pid and start time to within a few seconds; the monitor says when the list cannot be read. Not on Windows. A Changes view for such a session would need an OS file watcher with a trustworthy record of who wrote what; three reviews showed a folder listing cannot give that honestly, so it is not attempted.
- Another program running as the same user that can read `~/.live-room/` or drive the browser can do what you can do here. Closing that needs an operating-system boundary this tool does not have.
- macOS; Windows watches (one real PC so far, plus CI) and answers (checked so far only by the automated tests) and has hooks for Allow / Deny that are checked only by automated tests so far, not on a real PC; nothing else that acts on agents is available there yet. Phone-width layouts have been checked in browser emulation; physical phones and very short windows still have limited coverage.
- Known and left as they are: only the last ~200 KB of a log is replayed on first sight (the log panel says "earlier history not loaded" when steps are missing); the row re-spaces when agents come and go; interrupting a camera move jumps to its target; content shifts when a notice bar appears; partial log replay cannot reconstruct every earlier in-flight call or plan; the installer cannot fully rule out another program saving `settings.json` in the instant it writes; the review history is kept in the project's private archive, not here. Message and control records owned by Motet have separate persisted status recovery.


## Longer-term ideas

- A visible record of a correction reaching a session and the work that followed it.
- Fleet-wide running cost estimates where reliable cumulative usage and pricing inputs are available. API accounting for Motet’s own model calls is separate; native context-window snapshots must not be presented as billable usage or subscription charges.
- A stage overview that uses portrait-screen height more effectively.
- Broader transcript fixtures and provider compatibility checks.
- Motet acting on a standing request from the owner: watching for a named event, following up with an agent, or keeping two agents in step. Not agreed or designed. It would stay inside the rules below: the owner decides in advance what it may do, and nothing in a log or a peer message can widen that.
- Running Motet's own questions on a model provider other than Anthropic.
- Everything on Windows, then Linux as a tested platform.

## Rules that carry forward

The person stays in charge. A conversation, log entry, or peer message cannot authorize new actions. Permission decisions remain opt-in per window; messaging and controls require an explicit owner request through an authenticated route. Recorded, inferred, accepted, delivered, answered, and completed are different states. Motet must preserve those differences as support grows.
