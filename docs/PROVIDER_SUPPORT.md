# What works with what

Which apps Motet can read, message and control today, and the conditions a chat has to meet. Part of the [Motet README](../README.md).

## What is supported today

The current release starts with locally recorded coding sessions: **Claude Code, Codex, Cursor, and Antigravity**. It follows recently active sessions by default, not every historical chat in your provider accounts. Codex cloud work running on this computer has limited command observation, without its conversation. Ordinary Claude web chats, ChatGPT chats, and other providers’ full account histories are not imported.

Reading a session does not mean Motet can send to it or control it. Eligible local Codex chats support queued messages and some active-turn controls. Existing Claude Code chats can receive owner messages through the optional [Motet plugin](MESSAGING.md#messaging-an-existing-claude-chat); permission answering uses separate optional hooks. The [capability table](#current-provider-capabilities) spells out the differences. Broader conversation coverage and provider actions are on the [roadmap](ROADMAP.md), not implied by appearing on the stage.

## Current provider capabilities

Codex messaging, controls and native reads require macOS and an eligible local connection. Steering, interruption and native reads require Codex Desktop `26.1016.62342`, build `21477`. New chat and sending files to Codex require `26.1029.61003`, build `24614`. With one desktop build installed, only its group is available. Motet verifies the installed app files as well as the version and build. Claude permissions have separate paths for optional hooks and chats started by Motet.

| Capability | Local Codex | Claude Code | Cursor | Codex cloud on this computer | Antigravity |
| --- | --- | --- | --- | --- | --- |
| Observe recorded activity | Session logs | Session logs; optional hooks | Transcript; tool results not recorded | Commands from the process list | Native transcript; uncorrelated tool outcomes |
| Open the existing chat in its app | Supported local desktop route | Supported local desktop route | Unavailable | Unavailable | Optional Motet Navigation connector; macOS, verified app version |
| Send a direct message | Queue to an eligible existing chat | Queue to a connected existing chat through the optional Motet plugin | Unavailable | Unavailable | Unavailable |
| Receive agent-to-agent messages | Reachable chats through the shared MCP | Compatible Motet plugin with agent messaging authorized | Unavailable | Unavailable | Unavailable |
| Steer or stop the active turn through Motet | Supported when the control checks pass | Unavailable | Unavailable | Unavailable | Unavailable |
| Promote an already queued message with Steer | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |
| Read native questions | Supported, read-only, on the verified build | Unavailable | Unavailable | Unavailable | Unavailable |
| Answer permission prompts in Motet | Unavailable | Optional permission hooks with **Answer here** enabled, or the permission flow in a chat started by Motet | Unavailable | Unavailable | Unavailable |

You can still open an unsupported desk, inspect the evidence available there, and ask Motet about it when the assistant is configured. Direct **Send** stays disabled with an explanation. Use the original app for unsupported actions; **Open in…** works only where Motet has a supported route.

## Antigravity desks and monitors

Recently active local Antigravity conversations appear automatically, using the same desks and monitors as the other apps. No Motet plugin, MCP configuration, hook or per-chat enrollment is needed for observation. Motet follows native transcripts under `~/.gemini/{antigravity,antigravity-cli,antigravity-ide}/brain/<chat-id>/.system_generated/logs/transcript.jsonl`. The desktop app format has been verified locally; CLI and IDE sources use the same adapter when their records match that format.

Chat names, project folders, explicit parent relationships and activity state come from a bounded read-only query of the source's optional `conversation_summaries.db`. Missing, busy or incompatible metadata leaves a generic name and unknown facts without stopping transcript observation. Duplicate native chat IDs across source roots are excluded rather than merged. Provider-added prompt metadata and settings are removed from native user messages. Tool requests are visible, but their outcomes remain unknown because these transcripts do not correlate tool calls with results. A step marked DONE is not treated as a finished turn. Context usage and the selected model remain unknown when the native records do not establish them.

Motet can read these observations and, when memory is enabled, retain new activity under the same privacy and retention rules as other local logs. Antigravity's private storage is excluded from file previews. Messaging, cross-provider delivery, steering and permission answering are not connected; use Antigravity itself for those actions.

### Open an existing Antigravity chat

On **macOS with Antigravity 2.31.2**, open an Antigravity desk in Motet and choose **Connection → Install connector**. This installs **Motet Navigation** once for your user account and authorizes opening existing desktop chats. In Antigravity's **Customizations → Installed**, enable **Motet Navigation** once, then open **Extensions → Motet Navigation** in any existing chat. There is no per-chat installation, pairing code or target list. Motet does not rewrite Antigravity's settings file.

Motet displays **Open in Antigravity** only while the native panel has a live connection and the exact desktop chat can be verified. You can hide the panel after it connects. If the connection ends, reopen that panel and refresh Connection in Motet. Installation alone does not guarantee a connection after startup or after the app unloads a panel. CLI, IDE, ambiguous identities, other app versions and other platforms remain unavailable for opening; observation continues separately.

Opening requests navigation to the existing chat and brings the verified running app forward. It sends no message, starts no model turn and creates no chat. **Opening requested** reports dispatch, not independent confirmation of the selected chat. An uncertain request is never retried automatically.

Choose **Connection → Disconnect** to revoke further navigation. This leaves the native extension installed and its native toggle unchanged; **Enable connection** in Motet restores authorization. A navigation request already issued cannot be recalled. To remove the installation, first turn the Navigation extension off in Antigravity's Installed list, wait for it to stop, then run `python3 scripts/install_antigravity_open.py remove` from this repository. Updates to connector files also require turning it off first. The same script supports `install`, `status` and `disconnect`. Unrelated native settings are preserved. Private authorization and connection files stay under `~/.live-room/antigravity-open/`; credentials are excluded from public status, events and Motet context. See the [connector notes](../integrations/antigravity-open/README.md) for its protections and limits.

## Where support goes next

The next priorities are easier discovery and return to supported sessions, reliable messaging and steering across providers, and a clearer view of what each connection can do. Claude live controls and promoting a queued message with **Steer** need verified delivery routes before they can be enabled. Cursor actions and additional providers remain exploratory. Optional [Motet memory](MOTET.md#motet-memory) retains searchable history while Motet runs. Broader launch support, full-account imports and coordination across sessions are future work. See the [roadmap](ROADMAP.md#priorities) for scope and completion criteria.

## Files and New chat

**Motet file reading** ships enabled and requires the host’s private spending policy and an Anthropic API key. Once set up, you can keep a PDF or PNG in a Motet conversation without asking a question, then choose **Use in next question** in **Files**. Each file question sends only the files in its tray. Auto uses Haiku 5.5 for file questions and the answer identifies the actual model; other explicitly selected models are refused. File citations open explanatory metadata, without drawing or downloading the original.

File cards distinguish Motet's kept copy, files supplied for a particular answer, and that answer's cited sources. A valid answer without text is identified explicitly, with its citations still available.

The Files view can remove Motet's retained original, with explicit confirmation and status recovery. This does not remove the original you selected, a provider's copy, or your messages. Dependent answers stop being shown; uncertain cleanup is not reported as success. Local file management can remain available while file reading is off. See [Set up Motet file reading](MOTET_FILE_READING.md) for the local policy, supported formats and usage limits.

**Files to agents** ships enabled for eligible local Codex and plugin-connected Claude chats. Sending files to agents needs SQLite 3.45 or newer in the Python that runs Motet. If that Python has an older database library, Motet explains that file sending is unavailable while ordinary messaging remains available. Attach a PDF, PNG or JPEG in the agent's workspace composer. The draft is kept locally. Send saves copies to the verified project folder and queues the message with references to those files. Motet reports unconfirmed delivery as uncertain. The destination must be an eligible local parent chat in a regular Git checkout. Codex also requires macOS desktop version `26.1029.61003`, build `24614`, and a chat with GPT-6 Astra and compatible settings. A Claude chat needs a live attachment-capable Motet plugin connection. Claude sessions run by Motet do not qualify for this relay. The demo uses fictional files and never sends them.

**New chat** ships enabled in the workspace on macOS. Choose a verified folder from the workspace's project list, or use **Add project folder** to choose a folder in the Mac’s own folder window. This needs a browser on the Mac running Motet, with a supported browser in front and its local connection verified. Adding a folder saves it locally and never starts a chat; launching is a separate action. Add only folders you trust: a chat started there uses that folder's own settings and hooks. Saved folders appear in the sidebar and in Settings → Connections. If a folder moves or is replaced, add it again before starting a chat there. Removing saved folders is not available yet. On Codex Desktop version `26.1029.61003`, build `24614`, open a blank chat or use **Start working** with an instruction when that mode qualifies. **Claude run by Motet** launches local Claude Code with an instruction. Motet checks Claude Code `2.1.323` against a pinned executable hash and requires a verified readiness record for that Mac and user at `~/.live-room/managed-claude-readiness.json`. This release has no self-serve setup for that record. Blank managed Claude chats are unavailable. Motet shows the session on the stage only after it verifies it belongs to the selected folder. If a provider, app build, host or folder cannot be verified, the corresponding action stays unavailable.
