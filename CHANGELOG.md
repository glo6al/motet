# Changelog

## 0.2.0 — Unreleased

Changes since 0.1.0 and its follow-up fixes.

### Upgrading from 0.1.0

1. Stop Motet. If you might go back to 0.1.0 and keep your Memory, save a copy of the Memory archive now, as [rolling back the Memory archive](docs/MOTET.md#rolling-back-the-memory-archive) describes. 0.2.0 upgrades it the first time it starts.
2. Update and start the Motet server first.
3. Then update the Claude plugin to 0.6.5 in Claude Code's plugin manager ([plugin guide](integrations/claude-mod/README.md)). Go straight to 0.6.5 from 0.6.1 or 0.6.4: current Claude Code does not load 0.6.4.
4. Run `/reload-plugins` in each open Claude chat. In Claude Desktop the chat then reconnects by itself at its next activity, unless you clear or switch its conversation (`/clear`, `/resume`) before it has. A chat in a terminal reconnects only if Claude starts the plugin again. If `/watch-status` does not show a chat connected, close it and open it again.
5. If you use Motet file reading, replace your policy with this release's one. Keep `usdLimit`, `scopeId` and `scopeRevision` exactly as they were, so your budget carries forward ([Motet file reading](docs/MOTET_FILE_READING.md)).

### Going back to 0.1.0

- **Memory.** 0.1.0 cannot open the upgraded archive. It shows Memory as "Storage unavailable" and records nothing. Restore the copy you saved, or start 0.1.0 with fresh Memory. Never change the archive's version number by hand ([steps](docs/MOTET.md#rolling-back-the-memory-archive)).
- **Agent-to-agent bridge.** Do not run 0.1.0 against a message database that holds bridge messages, and do not go back while bridge requests are unresolved. The supported way back is to keep 0.2.0 and its database and switch the bridge off ([bridge rollback](docs/PEER_BRIDGE.md#rollback-and-privacy), [completion and reconnects](docs/PEER_BRIDGE.md#completion-and-reconnects), [clearing a stuck request](docs/bridge/RECOVERY.md)).
- **Chats Motet started.** Close Claude chats that Motet started before going back. 0.1.0 refuses the messages Motet worded for them ([messaging](docs/MESSAGING.md#asking-an-agent)).

### What is new

- **Windows does much more.** Besides watching, Motet now answers, and it can message Claude chats through the plugin, answer Allow and Deny, run the bridge with Claude chats as recipients, start a Claude chat from New chat, read a PDF or PNG, send files to Claude chats (with Git for Windows), and show previews with their files. All of this is checked by automated tests only, not yet on a real PC. Codex messaging and file sending stay Mac-only ([Windows](docs/WINDOWS.md), [trial guide](docs/WINDOWS_TRIAL.md)).
- **Linux:** Claude messaging, and the bridge with Claude chats as recipients, checked by automated tests.
- **Who wrote it.** Each message records who wrote it. Motet no longer describes a message it, another chat or an automatic prompt wrote as yours ([messaging](docs/MESSAGING.md#asking-an-agent)).
- **Claude plugin 0.6.5.** It loads in current Claude Code again. After a plugin reload it reconnects by itself in Claude Desktop; a terminal chat may need a restart (step 4). Motet orders and remembers `/watch-connect`, `/watch-setup` and `/watch-disconnect`: in one window in the order you type them, and across windows in the order Motet receives them. A disconnect Motet has not heard of yet stays in force until Motet has it, or until Motet records a later reconnect from the same window. A Claude chat keeps receiving messages after it moves into a subfolder of its own project.
- **Codex.** Controls and New chat now work with Codex Desktop `26.1103.60933`, build `25894`. Sending files to Codex still needs `26.1029.61003`, build `24614`. Large Codex records are read instead of withholding the reply, and an idle Codex chat gives a truthful reason.
- **Plan approvals** go to the agent's app instead of offering Allow.
- **Memory** refuses an archive from a newer Motet and leaves it unchanged.
- **Motet file reading** accepts this release's policy from 2026-11-24 to 2026-12-24.
- **Stop a chat Motet started.** A Claude chat that New chat started can be stopped from **Stop this chat** in the menu under its name, on macOS and Windows. Motet ends Claude and the programs it runs in its own group (on Windows, its job), denies its waiting request, keeps its history, and the chat takes no new messages. On a Mac, a program the chat started in a session of its own is not stopped and can keep running, even after the stop is confirmed. If Motet cannot confirm the end, the desk says the chat may still be running and offers **Try again** or a Terminal command, both hidden once Claude itself has ended ([New chat](docs/PROVIDER_SUPPORT.md)).

## 0.1.0 — 2026-11-18

Published commit: [295f8de](https://github.com/glo6al/motet/commit/295f8dec9e81745d1c43bb6e7fcd5204bb325921).

- Follow recent, locally recorded activity from Claude Code, Codex, Cursor and Antigravity on the stage. See what is running, what is waiting for you and what the logs recorded about each session. Visibility into Codex cloud is limited to commands Motet can see running locally.
- Queue messages for eligible local Codex chats on macOS or for Claude Code chats connected through the optional plugin. Some Codex controls require a verified desktop build. Claude Code permission responses on macOS use separate optional hooks. Cursor and Antigravity sessions can be watched, and the optional macOS Antigravity connector can open existing chats in their app.
- Ask Motet about recorded work with your own Anthropic API key. Auto mode picks a model for the question. Optional local memory lets you revisit saved evidence and Motet conversations.
- The app runs locally with Python 3.9 or later and the standard library. macOS is the tested desktop platform. Windows coverage is limited to automated tests in CI.
- Check SQLite before starting file delivery to agents, explain when a newer Python is needed, and preserve ordinary messaging across restarts. Bind the local server without a hostname lookup.

### Follow-up fixes

- Refresh message-queue checks before admitting a message, so Motet's own request bookkeeping does not cause a false refusal. Keep definite refusals distinct from uncertain delivery, and identify the recipient when a relayed message was not sent.
- Hide complete, correlated Claude plugin request echoes from the conversation and queue history while keeping the original message and reply visible.
