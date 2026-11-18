# Changelog

## 0.1.0 — 2026-11-18

- Follow recent, locally recorded activity from Claude Code, Codex, Cursor and Antigravity on the stage. See what is running, what is waiting for you and what the logs recorded about each session. Visibility into Codex cloud is limited to commands Motet can see running locally.
- Queue messages for eligible local Codex chats on macOS or for Claude Code chats connected through the optional plugin. Some Codex controls require a verified desktop build. Claude Code permission responses on macOS use separate optional hooks. Cursor and Antigravity sessions can be watched, and the optional macOS Antigravity connector can open existing chats in their app.
- Ask Motet about recorded work with your own Anthropic API key. Auto mode picks a model for the question. Optional local memory lets you revisit saved evidence and Motet conversations.
- The app runs locally with Python 3.9 or later and the standard library. macOS is the tested desktop platform. Windows coverage is limited to automated tests in CI.
