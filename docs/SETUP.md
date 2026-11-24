# Setting up and running Motet

Starting the server, the demo, the optional hooks, moving around, the terminal view, and what differs on Windows. Part of the [Motet README](../README.md).

## Try it

Two minutes, no install.

The demos use fictional agents and projects. For public recordings, use `?demo` for Workspace or `?demo&ui=classic` for Classic. Live conversations and command output can contain personal information and absolute file paths.

```bash
git clone https://github.com/glo6al/motet.git && cd motet
python3 src/server.py
```

On Windows, use `py src/server.py` (or `python`) instead of `python3`. [docs/WINDOWS.md](WINDOWS.md) walks through installing Python and Git and trying it, step by step; see [Windows](#windows) for what differs.

Then open <http://127.0.0.1:8793/> for Workspace and press **Unlock**. The server asks your default browser to open a new, unlocked tab, and the stage fills with whatever Claude Code, Codex, Cursor or Antigravity sessions have been active in the last twenty minutes (`WINDOW_MIN` to change that, `PORT` to move it).

**No sessions of your own?** Open <http://127.0.0.1:8793/?demo>. A synthetic fleet plays through a sample of what the stage can show: working, waiting on you, a question, a hand-off, a sub-agent, last words. Open <http://127.0.0.1:8793/?demo&ui=classic> to follow the Classic walkthrough in [the tour](TOUR.md#the-stage-and-the-desk). One agent holds a permission request you can answer on the page: click its APPROVE tag, then Allow or Deny, and a scripted outcome follows; nothing is sent anywhere, and the switches in the bar work on the page only. For a recording, open Bram’s desk to show a formatted review with a comparison table and code, then Ada’s **Work → Changes** for the recorded pricing edit. Cleo and Dov demonstrate the parent–child tree; Cleo also has the simulated permission prompt. Reload `?demo` to restart the sample story. There is no rendered preview and no summarisation marker. Direct messages and Motet answers in the demo are also scripted; they do not contact agents or a model. The demo page reads no logs and needs no key, and the bar says so. The server behind it is the ordinary one: it still creates its key and follows your real logs in the background, because it is the same process; after loading its page and icons, the demo makes no live-data or action requests to it, and it keeps its own project names rather than this browser's.

**Optional hooks** (Claude Code; on Windows, see [Windows](#windows) below). In a second terminal, while the server is running:

```bash
python3 scripts/install_hooks.py            # --remove to undo
```

If you moved the server with `PORT`, run the installer with the same `PORT` and open the page on that port. The server, the hooks and the browser must all agree on it.

This adds Notification, Stop, SubagentStop and PermissionRequest hooks to your Claude Code settings. The first three only tell Motet things sooner than the log would. The fourth lets Motet answer permission prompts for you, and is off until you switch it on in a window. Read the [trust boundary](TRUST_BOUNDARY.md) first.

**Moving around:** two-finger scroll or drag to turn, or the arrow keys. Hover a monitor and it comes to your hand; click it to step up to its desk. The conversation is the main view. Open **Work** for **Preview**, **Changes** or **Activity**; when focus is outside the conversation and text fields, keys **1**, **2** and **3** open Activity, Preview and Changes respectively. Drag the Work divider or focus it and use the arrow keys to resize; double-click it to reset. **← Conversation** closes Work. Use **Back to the stage** to leave the desk; Escape first closes an active picker or drawer or handles conversation focus before leaving the desk.

**In a terminal instead.** With the server running, in another terminal:

```bash
python3 src/watch_tty.py            # a board: one row per agent, those waiting on you first
python3 src/watch_tty.py --tail     # one line per event, to read or grep
python3 src/watch_tty.py --once     # one line such as "2 waiting · 3 working", for a tmux or prompt status bar
python3 src/watch_tty.py --tab      # iTerm2: the board in a new window, then one tab per agent, each with its own log
python3 src/watch_tty.py --split    # iTerm2: the same in one tab, the board on top and each agent in a pane below
```

The board and the tail ring the terminal bell when an agent starts waiting on you. In iTerm2 an agent's tab or pane is titled with its name and project and turns amber while it waits on you, and the board's tab shows a "2 waiting" badge. The board opens a tab for each agent active in the server's window, and for each new one as it appears, without taking the focus; a view you close stays closed. Panes halve the largest one as agents arrive, and a tab too small to split again gets a new tab. Each view is one of the twelve connections the server allows, browser windows included, so the board opens at most six and says how many it left out; a view the server turns away waits for a free connection for up to a minute. Run one board at a time: a second one can take the connections your browser window needs. `--tab` copies `src/iterm-profile.json` to `~/Library/Application Support/iTerm2/DynamicProfiles/project-watch.json`, replacing any earlier copy, and the first time, macOS asks whether your terminal may control iTerm2. The terminal reads the same stream as the page, with the same key, and it only watches: it cannot answer a prompt. Use the same `PORT` as the server.

## Windows

The server is written to run on Windows with the same standard-library Python. It has been run on one real Windows 11 PC ([report #4](https://github.com/glo6al/motet/issues/4)), and its automated tests run in CI on every push. Watching works there; Motet's own answers are checked so far only by the automated tests. Allow and Deny are new on Windows and checked so far only by the automated tests. Messaging a Claude chat through the Motet plugin now starts on Windows too, also checked so far only by the automated tests, not yet on a real PC. Messaging Codex stays Mac-only for now: it needs the Codex desktop app on a Mac. The other things that act on your agents (the bridge between agents, New chat, files, dictation) are switched off for now, with parity with the Mac as the goal. More reports are welcome. To try it and tell us how it went, follow **[docs/WINDOWS.md](WINDOWS.md)**: installing everything from scratch, starting it, troubleshooting, and the questions only a real Windows PC can answer. What differs:

- **The hooks** (Allow / Deny, and the earlier notices they give) work differently. On macOS and Linux they are shell commands that use curl. On Windows, `py scripts\install_hooks.py` installs them so that Claude Code starts Python directly on `scripts/motet_hook.py`, with no shell in between; that script does what the curl commands do. This needs Claude Code 2.1.139 or later. The installer refuses an older version when it can read it: from `claude.exe --version`, or for an npm install from the package beside `claude.cmd`. Otherwise it says the version could not be checked. The hooks name your Python and this folder by their full paths, so after moving the folder or changing Python, run the installer again. It is checked by automated tests, including on Windows, but not yet against a real Claude Code prompt on a real PC. Without the hooks Motet still watches; Claude asks in its own app.
- **Messaging a Claude chat** keeps its records in `%USERPROFILE%\.live-room`. Motet checks the folder each time it starts: it must not be a link or junction, it must be owned by you, and its access list may name only you, SYSTEM, Administrators and the folder's owner. A folder Motet creates now starts with a private access list (you and SYSTEM only). A folder made by an earlier Motet follows your user profile's permissions, which normally pass. The message database and its lock must also name only you and SYSTEM; Motet creates them that way. If anything fails the check, messaging stays off and the page says why. If you first started Motet from an administrator window, the folder may belong to the Administrators group; make it yours with `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"`. Claude messaging also starts on Linux now, where these checks are the same owner-and-mode checks as on the Mac.
- **The bridge between agents** refuses on Windows with one plain reason: Motet cannot yet open chat folders one by one while refusing links there, so it cannot find any chat for the bridge, and agents cannot ask each other questions through Motet. The setup commands and the bridge's MCP adapter print that reason; your own messages to Claude chats are unaffected. Its private files and locks are prepared for Windows, checked by automated tests, not yet on a real Windows PC. On Linux the bridge works with Claude chats as recipients; Codex chats receive only on a Mac. See [where the bridge works](PEER_BRIDGE.md#where-it-works).
- **Motet's answers** need the key file made private by its access list: see [Keys and settings on Windows](MOTET.md#keys-and-settings-on-windows).
- **Unverified:** where Claude's desktop app keeps its session records on Windows (Motet looks in `%APPDATA%\Claude` and in `%LOCALAPPDATA%\Claude-Data\*`; on the one PC tried only the second held anything, and only the folder layout was checked), so titles, settings and last-focused times from it may be missing; and how Cursor names a project folder there, so a Cursor agent's folder may show as not recorded.
- **Private folders** for previews also include `AppData`.
- **Previews show the page only, and need Python 3.12 or later.** A page an agent wrote is rendered without the styles, scripts and images it links to from files beside it (inline ones still apply), because no folder is granted on Windows yet (see the trust boundary); before Python 3.12 nothing is previewed there.
