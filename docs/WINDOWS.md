# Installing and trying Motet on Windows

Motet shows your coding agents (Claude Code, Codex, Cursor) on a browser stage with one monitor per agent, updated as they work. This guide takes you from a Windows PC with nothing installed to that stage. It also lists what we would like you to check, because it has so far been run on **one real Windows 11 PC** ([report #4](https://github.com/glo6al/motet/issues/4)), plus automated tests in CI. More reports are still welcome, especially from other setups. The goal is for Windows to match the Mac. Windows comes first, then Linux.

Allow about fifteen minutes. You need no administrator rights unless your installer asks for them, and you install nothing besides Python and Git.

---

## What you need

| | Why | Minimum |
|---|---|---|
| Windows 10 or 11 | | 64-bit |
| **Python** | runs the server; nothing else is installed into it | 3.12 or later recommended. 3.9 to 3.11 run it too, but show no rendered previews (see [what differs on Windows](SETUP.md#windows)) |
| **Git** | to download the project (optional: a ZIP download works too) | any recent version |
| A browser | the stage is a web page | Chrome or Edge recommended |
| **Windows Terminal** | only for the optional terminal view | included in Windows 11; free in the Microsoft Store for Windows 10 |
| At least one agent that has been used on this PC | the stage shows their sessions | Claude Code, Codex CLI or Cursor |

No agent sessions yet? You can still try it: the demo (step 5) needs none.

All the commands below are for **PowerShell**. To open it, press **Start**, type `PowerShell`, and press Enter. Paste one block at a time. Lines starting with `#` are notes, and PowerShell ignores them.

---

## Step 1: Install Python

**Check first.** You may already have it:

```powershell
py --version
```

If that prints `Python 3.12` or higher (for example `Python 3.12.7`), skip to step 2. (3.9 to 3.11 also run it, without rendered previews, as the table says.)

If it says `py` is not recognised, or shows a version below 3.9, choose **one** of these:

**Option A: winget** (built into Windows 10 since 2021 and into Windows 11)

```powershell
winget install --id Python.Python.3.12 -e
```

Accept the prompts. When it finishes, **close PowerShell and open a new one**, so that it picks up the new install.

**Option B: the python.org installer**

1. Go to <https://www.python.org/downloads/windows/> and download the latest **Windows installer (64-bit)** for Python 3.12 or 3.13.
2. Run it. On the first screen, **tick "Add python.exe to PATH"** at the bottom, then click **Install Now**.
3. When it says "Setup was successful", close it, and **open a new PowerShell window**.

> Avoid the Microsoft Store's "python" shortcut. If typing `python` opens the Store, that is Windows' placeholder, not Python. Install with option A or B, then use `py`, which is the launcher both options install.

**Check it worked:**

```powershell
py --version
```

---

## Step 2: Install Git (or skip it and use a ZIP)

**Check first:**

```powershell
git --version
```

If that prints a version, skip to step 3. Otherwise:

**Option A: winget**

```powershell
winget install --id Git.Git -e --scope machine
```

`--scope machine` installs Git for all users, in Program Files. Sending files to a Claude chat needs that: Motet does not use a Git installed only for your account, which any program you run could change.

Then **close PowerShell and open a new one**.

**Option B: the installer.** Download it from <https://git-scm.com/download/win>, run it, and accept the defaults (they install it for all users). Then open a new PowerShell window.

**Option C: no Git.** Use the ZIP download described in step 3.

---

## Step 3: Download Motet

```powershell
cd $HOME
git clone https://github.com/glo6al/motet.git
cd motet
```

**Without Git:**
1. Open <https://github.com/glo6al/motet>, click **Code**, then **Download ZIP**.
2. Right-click the archive and choose **Extract All…**. Extract it to your home folder.
3. Open the extracted folder.

```powershell
cd "$HOME\motet-main"
```

**Check you are in the right place:**

```powershell
dir src\server.py
```

That should list one file.

---

## Step 4: Check the tests pass on your machine

This step is optional but useful: it is the same check CI runs. It takes a few seconds and does not touch your real settings.

```powershell
py tests\test_parsers.py
```

The last line should be `all ok`. If it says `FAIL …` instead, that line is the first thing to put in your report.

---

## Step 5: Start the server

```powershell
py src\server.py
```

You should see:

```
Motet on http://127.0.0.1:8793/  — open it and press Unlock (sessions active in the last 20 min)
```

**Leave this window open.** The server runs until you close the window or press **Ctrl+C** in it.

It listens on `127.0.0.1` only, so only this PC can reach it. Windows Firewall should therefore not ask about it. If it does ask, please tell us, and choose **Cancel** or **Don't allow**: nothing else needs to reach it.

Now open <http://127.0.0.1:8793/> in your browser:

- **To see your own agents:** press **Unlock**. The server asks Windows to open a small unlock page in your default browser, and that tab becomes the unlocked stage. It shows every Claude Code, Codex or Cursor session that has written to its log in the last 20 minutes.
  - If **Unlock opens an editor instead of a browser** (Notepad, VS Code, …), your default app for `.html` files is not a browser. Please tell us. To fix it: **Settings → Apps → Default apps**, find `.html`, and choose your browser. Then press Unlock again; it allows one try a minute.
- **To try it without any agents:** open <http://127.0.0.1:8793/?demo>. It plays a made-up fleet of agents, and it needs no unlock.

**Moving around:** two-finger scroll, drag, or the arrow keys turn the row. Hover over a monitor to bring it closer, click it to open that agent's desk, and press Escape to step back.

---

## Step 6 (optional): Settings

Settings are environment variables, set in the same PowerShell window **before** starting the server:

```powershell
$env:WINDOW_MIN = "60"     # show sessions active in the last 60 minutes (default 20)
$env:PORT = "8794"         # use another port (default 8793); then open http://127.0.0.1:8794/
py src\server.py
```

These last only for that PowerShell window.

**To ask Motet questions**, save your Anthropic API key in `%USERPROFILE%\.live-room\anthropic-key` and make that file yours alone, as [Keys and settings on Windows](MOTET.md#keys-and-settings-on-windows) shows: two PowerShell lines that ask for the key, then one `icacls` command. Restart the server afterwards. Motet never loosens or repairs the file's permissions itself; if they are wrong it stays off and says why.

To give a project folder a friendlier name on the stage, create `%USERPROFILE%\.live-room\projects.json` containing, for example:

```json
{"my-long-folder-name": "Website"}
```

---

## Step 7 (optional): The terminal view

The stage also runs in a terminal. With the server running, open a **second** window in **Windows Terminal** (the older blue PowerShell console may show odd characters instead of redrawing), go to the project folder, and run:

```powershell
py src\watch_tty.py            # a board, one row per agent
py src\watch_tty.py --tail     # one line per event
py src\watch_tty.py --once     # one line, e.g. "2 waiting · 3 working"
```

```powershell
py src\watch_tty.py --tab      # Windows Terminal: the board in a new window, then one tab per agent
py src\watch_tty.py --split    # the same in one tab: the board on top, each agent in a pane
```

`--tab` and `--split` run `wt.exe`, Windows Terminal's own command, and stop with a plain message if it is missing. They pass the port on to each tab for you. A tab or pane is titled with its agent's name and project, the bell rings when an agent starts waiting on you, and its tab shows a full progress ring in the warning state until it stops waiting (Windows Terminal cannot recolour a tab from inside it, so there is no amber tab or "2 waiting" badge as in iTerm2). Each new tab or pane hands the focus back to the board. They need Windows Terminal 1.11 or later (named windows came in 1.8, and the pane moves they use in 1.11). They refuse to start if the Motet folder or Python's path holds `;`, `%`, `"` or a control character, which Windows Terminal would treat as part of its own commands, or holds a space and ends in `\`. Not yet tried on a real PC. If you used `$env:PORT`, set it in this window too.

---

## Where things are

| What | Where |
|---|---|
| The server's key and settings | `%USERPROFILE%\.live-room\` (a folder the server creates gets a private access list; an older one follows your profile's default permissions, which keep other non-administrator accounts out unless they have been changed. Motet's answers and agent messaging check the folder and stay off if other accounts can open it) |
| Claude Code's logs (read) | `%USERPROFILE%\.claude\projects\` |
| Codex's logs (read) | `%USERPROFILE%\.codex\sessions\` |
| Cursor's transcripts (read) | `%USERPROFILE%\.cursor\projects\<project>\agent-transcripts\` |

Motet only **reads** the agents' folders, and it reports nothing anywhere. To check that a tool has logs it can read:

```powershell
dir "$env:USERPROFILE\.claude\projects"
dir "$env:USERPROFILE\.codex\sessions"
dir "$env:USERPROFILE\.cursor\projects"
```

**Agents running inside WSL** (Linux on Windows) keep their logs in the Linux home folder, not in `C:\Users\<you>`, so a server started from PowerShell will not see them. Running the server inside WSL has not been tried.

**Codex with `CODEX_HOME` set** keeps its logs elsewhere. Motet only looks in `%USERPROFILE%\.codex`.

---

## What does not work on Windows yet

Motet **watches** on Windows: the live stage, risk flags, previews (with their styles, scripts and images, Python 3.12 or later), your own app icons and Unlock all work. **Motet's own answers** and its model menu work too, once the key file is private (step 6); so far only the automated tests have checked them on Windows. Everything that **acts** on your agents is switched off for now, apart from Allow / Deny, messaging a Claude chat, sending files to a Claude chat, the bridge between agents with Claude chats as recipients, and starting a Claude chat from New chat, which are new and not yet checked on a real PC (see below). The goal is parity with the Mac, and these are still to come:

- **Messaging Codex chats.** It needs the Codex desktop app on a Mac for now; Send stays off and says so.
- **Parts of the bridge between agents:** Codex chats as recipients, the Antigravity plugin and inbox, and the offline recovery and clear commands (see below); and the older Claude channel launcher (`scripts/watch_claude.py`).
- **New chat for Codex.** It needs the Codex desktop app on a Mac. On Windows, New chat offers Claude only and says why Codex is not offered.
- **Sending files to a Codex chat.** It needs the Codex desktop app on a Mac; a Codex chat's message box says so.
- **Motet's own file reading now starts on Windows, but is not yet checked on a real PC.** Ask Motet about a PDF or PNG as on the Mac, once its key and its reading policy are saved privately and while the policy's dates are current (the guide gives them): [Motet file reading](MOTET_FILE_READING.md#on-windows) gives the PowerShell line and the `icacls` command for the policy file. Without the policy, or with one that is not yours alone, file reading stays off and ordinary questions still send. Motet keeps the files in its private storage in `%USERPROFILE%\.live-room\attachments`, which holds itself to the same rules as messaging: no link or junction anywhere in its path, owned by you, with an access list naming only you and SYSTEM (Administrators may also appear on the folders); it must be on an NTFS drive (not ReFS, a Dev Drive or a FAT USB stick), and it is refused inside a OneDrive placeholder folder. While Motet runs, the storage folder and the folders above it cannot be renamed or deleted. If a file is open in another program when Motet removes it, Motet tries again later. If the storage cannot start, a chat's message box says "Motet could not open its private file storage". Automated tests on Windows cover these checks; nobody has tried them on a real PC yet. If you try it, please tell us what happened.
- **Dictation's speech engine.** The dictation helper, its private state, Motet's proxy and model setup now run on Windows, checked by automated tests but not yet on a real PC. The speech engine (`moonshine-voice` 0.1.6) has a Windows build only for 64-bit Intel and AMD PCs, and nobody has run it on Windows yet; Windows on ARM has none, so there the helper reports the engine as not installed and typing works as usual. See [local dictation on Windows](DICTATION.md#on-windows).
- **Codex cloud agents** on the stage, which are found through the process list.
- **Open in Antigravity.**

Details on the rest:

- **Starting a Claude chat from New chat is switched on on Windows, but is not yet checked on a real PC.** As on the Mac, it starts only after the PC's owner has written a readiness record for this PC, this Windows account and the pinned Claude Code build ([owner steps](#claude-run-by-motet-owner-steps)); Motet has no button or command that writes it. It needs Python 3.12 or later: with an older Python it stays off. It also needs `%USERPROFILE%\.live-room` to be yours alone; if it is not (a folder an earlier Motet made from an administrator window, say), it stays off, New chat says why, and so does the server window when it starts (`Claude run by Motet is off: …`). If Motet has never started a Claude chat on this PC, the Claude chats you started yourself are watched, messaged and answered as usual either way. **If it has** (its records are in `%USERPROFILE%\.live-room\managed-claude`), Motet cannot tell those chats from yours while it is off, so to stay safe it hides every Claude chat from the stage and leaves Allow / Deny to Claude's own prompt. To recover, fix the cause and restart Motet: start it with Python 3.12 or later, or make the folder yours alone (see the troubleshooting row "Claude run by Motet is off"). Motet starts Claude Code 2.1.323 itself, from the one place the native installer is expected to keep it, after checking the whole file against that build's published SHA-256. A project folder may be on a network share, another drive, a `subst` drive or in a OneDrive folder, as a Mac project may be on a network volume. Motet's own state stays in `%USERPROFILE%\.live-room` on a local disk, with the stricter rules described below. One difference from the Mac: a Claude chat Motet starts ends together with the small supervisor that watches it, and so does everything that chat started, so nothing is left running. On the Mac the chat keeps running if its supervisor stops, and its outcome is marked uncertain. If the program Motet runs in does not let that supervisor outlive its window, Motet says so and starts nothing; try starting Motet from an ordinary terminal window or a shortcut. Its folder window (for **Add project folder**) is Windows' own Explorer folder window, run in a small separate Python process so the server never waits on it, which closes by itself after 120 seconds. A folder on a local drive or inside a network share (`\\server\share\folder`) can be chosen; a drive or share root cannot. The window may open behind your browser; if it does, it flashes in the taskbar. When Motet suggests a starting folder, it skips any suggestion inside a network share or on a drive letter mapped to one, so no network server is contacted unless you open that folder yourself. You can try the window on its own (see the checklist below). The project list also offers folders of Claude chats already on the stage; Codex chats' folders are not offered on Windows. **Stop this chat** in the menu under the chat's name ends such a chat for good, as on the Mac: Motet ends its whole job, so everything the chat started ends with it, and keeps its history. If Motet cannot confirm the end, the desk offers **Try again** or a PowerShell command that names the chat by its session. Automated tests on Windows start a whole chat with a stand-in for Claude Code (one message, one Allow, Stop, and Stop through the desk's route) and check that nothing it started is left running; nobody has tried it with the real Claude Code on a real PC yet.
- **Sending files to a Claude chat now works on Windows, but is not yet checked on a real PC.** Attach a PDF, PNG or JPEG in a plugin-connected Claude chat as on the Mac: Send copies it into `.watch-attachments` in the chat's project folder and tells Claude where it is. As on the Mac, the project must be a regular Git checkout, and Motet checks with Git that the copies are excluded from it. It needs Git for Windows installed for all users (step 2); Motet finds the 64-bit build (including the new layout of Git for Windows 2.56 and later), the ARM64 build and the older 32-bit one; without it, or with a Git installed only for your account, the message box says what is missing. The project folder, its `.git` and `.git\info` folders and the `.git\info\exclude` file must be yours and changeable by you alone: a project cloned from an administrator window belongs to Administrators, and one made directly under a drive (such as `C:\src`) can be changed by every signed-in user, and either is refused. A folder or file reached through a link or junction is refused too: open the chat from the folder's real location. The chat's message box names the folder or file and the reason in plain words. Copies another program has open are left and removed later. Motet also keeps records in `.live-room` that keep these files apart from managed Claude chats. If that folder holds such records made by Motet on another computer or in another folder (copied or moved from a Mac, say), agent messaging stays off and says "Agent messaging cannot start here: its state folder holds attachment records made by Motet on another computer or in another folder, and they cannot be used here." If another program is using those records when Motet starts, it says "Agent messaging could not start: another program was using its attachment records. Restart Motet to try again." Automated tests on Windows cover these steps; nobody has tried them on a real PC yet.
- **Messaging a Claude chat now starts on Windows, but is not yet checked on a real PC.** It goes through the optional Motet plugin for Claude Code, as on the Mac: install it once with the two commands in [the plugin guide](../integrations/claude-mod/README.md#install-once-for-your-user-account), giving the full Windows path to the `integrations\claude-mod` folder, then choose **Set up Claude** in Motet. Automated tests check it on Windows, including the plugin talking to a real Motet server, but nobody has yet tried it with a real Claude Code on a real PC, so we do not know yet whether Claude Code loads the plugin on Windows at all. If you try it, please tell us what happened. Two differences from the Mac: the chat must be on Motet's stage before it can connect (an idle chat cannot connect from its saved details yet), and sending it files needs Git for Windows (see the item above). Motet keeps messages in `%USERPROFILE%\.live-room`. Motet checks the folder each time it starts: it must not be a link or junction, it must be owned by you, and its access list may name only you, SYSTEM, Administrators and the folder's owner. A folder Motet creates now starts with a private access list (you and SYSTEM only). A folder made by an earlier Motet follows your user profile's permissions, which normally pass. The message database and its lock must also name only you and SYSTEM; Motet creates them that way. Motet opens this folder through the same handle-by-handle walk as the bridge: the folders above it (your profile) are resolved once, so a profile moved with a junction or a `subst` drive is fine, but `.live-room` itself must not be a link or junction, must resolve to a local disk (not a network drive), must not sit inside a OneDrive or other cloud folder whose files are placeholders, and every folder from the drive down must be one your account may open. If anything fails the check, messaging stays off and the page says why. If you first started Motet from an administrator window, the folder may belong to the Administrators group; make it yours with `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"`.
- **Answering permission prompts from the stage (Allow / Deny), and the hooks behind it, are not yet checked on a real PC.** `py scripts\install_hooks.py` now installs them: Claude Code starts Python directly on `scripts\motet_hook.py`, with no shell in between. They need Claude Code 2.1.139 or later, and Python from winget or python.org, not the Microsoft Store. The installer refuses the Store's Python. It also refuses an older Claude Code when it can read the version (from `claude.exe`, or from an npm install's own files); otherwise it says the version could not be checked. The hooks name your Python and the Motet folder by their full paths, so if you move the folder or change Python, run `py scripts\install_hooks.py` again. Automated tests check them on Windows, but nobody has yet answered a real Claude Code prompt this way on a real PC. If you try it, please tell us what happened. `py scripts\install_hooks.py --remove` takes them out. Without them Motet still watches, and Claude asks in its own app as usual.
- **The bridge between agents now starts on Windows, but is not yet checked on a real PC.** As on Linux, Claude chats can receive (through the Motet plugin, once agent messaging is set up in the chat) and Codex chats cannot (that needs the Codex desktop app on a Mac), so in practice a Codex chat can ask a Claude chat. Motet first resolves the chat folders themselves (`%USERPROFILE%\.claude\projects` and `%USERPROFILE%\.codex`) once, so a junction or `subst` drive at or above them is fine. Below them it opens each folder by handle, looking up each next name inside the folder it already has open, and it refuses any link, junction or other reparse point it meets, so a project folder that is a junction inside them is not listed. OneDrive and other cloud files that are only placeholders on this PC are reparse points too, so chats kept inside such a folder are not listed either. The chat folders must resolve to a local disk: on a network drive they are not read. Set it up with `py scripts\watch_peer.py init` and `config`, as the [bridge guide](PEER_BRIDGE.md#installation-and-rollout) shows. Node cannot read a Windows access list, so the configuration `config` prints names your Python too: the bridge's MCP adapter asks it to read the credential file the same way (by handle, no link or junction) and to check that it is yours alone. If you move the Motet folder or change Python, run `config` again. Still off on Windows: the Antigravity plugin and inbox, the offline recovery and clear commands (to free a chat on hold, use **Chats on hold** in Settings → Connections), and Claude sessions started with `py scripts\watch_claude.py`: that older channel's files are written by Node, which cannot make a file private to you on Windows, so Motet would not trust them. Use the Motet plugin instead. If you try the bridge, please tell us what happened.
- **Paths longer than 260 characters.** The bridge's folder walk opens names one at a time, so long chat paths below the drive work there. Other parts of Motet open files by their whole path, and on a PC without Windows' long-path setting a file further than 260 characters from the drive is reported as missing. Please tell us if that happens to you.
- **Rendered previews now bring their styles, scripts, images and fonts, but are not yet checked on a real PC.** Motet opens the page's folder the same way the bridge opens chat folders: from the drive down, one folder at a time by handle, refusing any link or junction on the way and inside it, and keeping the folder from being renamed while a file is read. Previews need Python 3.12 or later. A page or file reached through a link or junction, with more than one name (such as files pnpm hard-links into `node_modules`), inside a private folder (`AppData`, `.live-room`, `.ssh` and the like), or on a network drive is refused. A page on a `subst` drive (a drive letter standing for a folder) is shown under the folder the drive stands for (resolved once, as for the bridge); not yet checked on a real PC. A page saved straight into your user folder, or above it, is shown without the files beside it, also when your user folder is really somewhere else behind a junction. So is a page in a project folder that holds the real place of a private folder (such as `%USERPROFILE%\dotfiles` when `.claude` is a junction into it); this applies on the Mac and Linux too. **OneDrive:** with Files On-Demand (the default), OneDrive marks the folders and files it syncs as cloud placeholders, which Windows treats like links, so a page in a OneDrive folder, including Documents or Desktop when OneDrive backs them up, is likely refused entirely. We have not checked this on a real PC. ReFS/Dev Drive folders have not been tried. Automated tests check the rest on Windows; please tell us what happens on yours.
- **App icons** work as on the Mac: put your own `claude`, `codex`, `antigravity` or `cursor` `.svg` or `.png` in `%USERPROFILE%\.live-room\app-icons` (see [the tour](TOUR.md)). A junction in place of `.live-room` or `app-icons`, or a link in place of an icon, is refused. Checked by automated tests, not yet on a real PC.
- **Desktop app details** (session titles, permission mode, when you last looked at a session) come from the Claude desktop app's own records. On one Windows 11 PC they were under `%LOCALAPPDATA%\Claude-Data\<profile>\claude-code-sessions`, and `%APPDATA%\Claude` was empty, so Motet looks in both. Only the folder layout was checked there, not that the records read correctly.

---

## Troubleshooting

| You see | Try |
|---|---|
| `py` is not recognised | Install Python (step 1), then open a **new** PowerShell window. |
| `Port 8793 is busy …` | Another copy is running (look for its window), or one just stopped and Windows is still holding the port. Wait a minute, or use `$env:PORT = "8794"`. |
| The stage is empty | Use an agent, then reload the page within 20 minutes, or set `$env:WINDOW_MIN = "120"`. Check the agent's log folder exists (see "Where things are"). |
| "Motet is locked" | Press **Unlock**, and use the **new** tab it opens. |
| Unlock opens an editor | See step 5: set your browser as the default for `.html`. |
| A preview says not found, or a page looks unstyled | Previews need Python 3.12 or later. See "What does not work on Windows yet" for what is refused (links, junctions, network drives, `AppData`), and note that a page saved straight into your user folder gets no styles or images. Tell us which folder kind it was in. |
| Odd characters in the terminal view | Use Windows Terminal. |
| "Motet is off: the key file is not yours alone" | Run the `icacls … /inheritance:r /grant:r` command in [Keys and settings on Windows](MOTET.md#keys-and-settings-on-windows), then restart the server. |
| "Motet is off: the key file is not owned by you" | In Command Prompt run `icacls "%USERPROFILE%\.live-room\anthropic-key" /setowner "%USERDOMAIN%\%USERNAME%"`, then the command above, then restart the server. |
| "Motet is off: the folder holding the key file is not owned by you", or the Motet model choice cannot be saved | The server was probably first started from an administrator window. In Command Prompt run `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"`, then restart the server. |
| "Motet is off: other accounts can open the folder holding the key file" | In Command Prompt run `icacls "%USERPROFILE%\.live-room" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F"`, then restart the server. |
| "Claude run by Motet is off: the .live-room folder in your home folder is not yours alone" (in the server window, or in New chat) | In Command Prompt run `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"` (if an administrator window made the folder), then `icacls "%USERPROFILE%\.live-room" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F"` (if other accounts can open it), then restart the server. These are the commands the server's own messages give for that folder. |
| "Claude run by Motet on Windows needs Python 3.12 or later" | Start the server with Python 3.12 or later (`py -3.12 src\server.py`, for example). |
| "Agent messaging needs a private readable state folder and database" | If Motet was first started from an administrator window, run `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"` in Command Prompt. If other accounts can open the folder, run `icacls "%USERPROFILE%\.live-room" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F"`. Then restart the server. |
| `~/.live-room is a link` | `%USERPROFILE%\.live-room` is a link or junction. Remove it; the server creates a fresh one. |

**To stop the server**, press **Ctrl+C** in its window (or close the window). **To remove Motet completely**, delete the project folder and `%USERPROFILE%\.live-room`. It changes nothing else on your PC.

---

## Help us finish Windows support

Some Windows features depend on facts only a real Windows PC can tell us (which apps are installed, how they name their pipes and links, and so on). If you have a Windows 11 PC, a one-minute check helps a lot:

```powershell
py scripts\windows_probe.py
```

Read the output, then paste it into a new [issue](https://github.com/glo6al/motet/issues/new) titled `Windows: probe output`.

**What it does:** it prints a short report: your Windows and Python versions; whether the Codex, Claude, Cursor and Antigravity apps and command-line tools are installed (and their versions); which `.codex` folders exist; the names of related named pipes; which `claude://`, `codex://`, `cursor://` and `antigravity://` links have a handler; whether Git Bash and Windows Terminal are there; and which of those programs are running. To find that out it lists installed packages through PowerShell (`Get-AppxPackage`; PowerShell may update its own caches while doing so), lists running programs with `tasklist`, and runs `--version` or `--help` on a command-line tool only when it is a real program (never an app alias or a script shim). If a Codex program is running it checks that program's command line only for the presence of the words `exec-server` and `app-server`, and never prints the command line. For the Codex app it also reads the OpenAI packages' manifests (their app ids, programs, `codex://` link and any command alias), which file and package each ChatGPT or Codex process runs from, who may open the app's bundled `codex.exe` and who signed it (without running it), and whether the app's control pipe `codex-ipc` is listed. The probe never opens or connects to that pipe, or any other: it only checks that the name is in the list of pipes. Accounts other than yours are shown by kind, never by name.

**What it does not do:** it changes nothing of yours, writes no files, makes no network connection, opens no app window and sends no message. It reads no file contents beyond what each check needs, and prints none. It only checks whether `%USERPROFILE%\.claude\settings.json` has a hooks section, and prints just the hook event names. Your profile path, user name, computer name, e-mail addresses and anything that looks like a key are replaced before printing, but redaction is not perfect: **please read the output before you post it** and remove anything you would rather not share.

### Claude run by Motet: owner steps

New chat starts a Claude chat on Windows only after the owner of this PC has written a readiness record for this PC, this Windows account and the pinned Claude Code build, as on the Mac. Motet has no button or command that writes it, and it never writes one itself. Motet must run on Python 3.12 or later. Do this once, as your normal account (not in an administrator window):

1. **Install Claude Code 2.1.323 with the native installer**, for your account. Motet uses only `%USERPROFILE%\.local\share\claude\versions\2.1.323` (with or without `.exe`). **That location is our best guess and is unverified until a real-PC run.** Motet checks the file against the published SHA-256 of 2.1.323's build for your PC's architecture (64-bit Intel or AMD, or ARM), and refuses a file with more than one name (a hard link) or one reached through a link or junction. It never uses npm's `claude.cmd` (or a `.bat`), an app alias in a `WindowsApps` folder, or the copy of `claude.exe` inside the Claude desktop app.
2. **Start Motet once** so that it makes its private `%USERPROFILE%\.live-room` folder, then stop it with **Ctrl+C**. If that folder already existed, an earlier Motet (or an administrator window) may have left it owned by someone else or open to other accounts; make it yours alone first with the two commands in the troubleshooting row "Claude run by Motet is off".
3. **Write the record.** In PowerShell, in the Motet folder, run:
   ```powershell
   py -c "import json, os, sys; sys.path.insert(0, 'src'); import claude_managed as m, private_storage as ps; record = json.dumps(m.local_configuration_identity()).encode('ascii'); fd = ps.create_private_file(os.path.join(os.path.expanduser('~'), '.live-room', m.READINESS_FILENAME)); os.write(fd, record); os.close(fd); print('Wrote', m.READINESS_FILENAME)"
   ```
   It writes `%USERPROFILE%\.live-room\managed-claude-readiness.json`, readable by you and SYSTEM only. The record holds no path, name or key: a fingerprint of this PC's hardware ID and your Windows account, and the pinned version and its SHA-256. It refuses to replace a record that is already there; delete the old one first if you need to write it again (after a new PC, a new account or a new pinned build, for example).
4. **Start Motet again.** It reads the record once, when it starts. **Claude** in New chat is then offered when the installed Claude Code passes the check; otherwise it stays off and says why. The first check reads the whole program (about 350 MB) and allows up to 60 seconds while Microsoft Defender scans it.

Still open, and still to check on a real PC:

- **Where Claude Code is installed** (step 1), and whether the installer hard-links the version file: Motet refuses a program file with more than one name.
- **How long the check takes** with Microsoft Defender scanning.
- **Git Bash.** Claude Code on Windows prefers Git Bash for its shell commands (`CLAUDE_CODE_GIT_BASH_PATH`). Motet does not set it: a chat Motet starts uses Claude Code's own default. Whether Motet should point it at the Git for Windows it already checks is still open.
- **That Claude Code on Windows speaks to Motet as it does on the Mac**: the stream-json input and output, waiting for Motet's claim before it starts, Motet asking it for its session and folder, permission requests answered through Motet, and your messages echoed back.
- **Python 3.12 or later only.** Before 3.12, Python shortens Windows file IDs, which Motet compares to tell a chat's transcript from any other, as it does for previews. So no chat Motet starts can carry identities recorded by an older Python. Moving those comparisons to identities read from file handles, as the folder's already is, is a known follow-up.

## Your feedback

Please tell us what happened, even if it was "everything worked". [Open an issue](https://github.com/glo6al/motet/issues/new) titled `Windows: …`.

**Please include:**

1. Windows version (**Settings → System → About**: for example "Windows 11 23H2"), and the output of `py --version`.
2. Which agents you use: Claude Code (terminal, desktop app, or IDE), Codex, Cursor, or Antigravity.
3. The last line of `py tests\test_parsers.py` (step 4).
4. What happened at each step, and where it went wrong, with the exact error text. Copy text rather than screenshots where you can.

**Things only a real Windows PC can answer.** Each one is a yes/no or a short paste:

- [ ] Did Windows Firewall ask about `python.exe` when the server started?
- [ ] Did **Unlock** open your browser, and did the stage fill with your agents?
- [ ] Did each agent show the right project folder name? Cursor agents especially: Motet reads a Cursor folder name such as `c-Users-demo-proj` as `C:\Users\demo\proj`, from names other people have posted, but has not seen it on a real PC. Projects on a network drive or share, and folder names with dots or spaces, are not named yet. Please paste the output of `dir "$env:USERPROFILE\.cursor\projects"`.
- [ ] Is there a Claude desktop app sessions folder? Paste the output of:
  ```powershell
  dir "$env:APPDATA\Claude" ; dir "$env:APPDATA\Claude\claude-code-sessions" -ErrorAction SilentlyContinue
  dir "$env:LOCALAPPDATA\Claude-Data\*\claude-code-sessions" -ErrorAction SilentlyContinue
  ```
- [ ] Did a page an agent wrote show as a rendered preview at its desk, with its styles and images? Which folder was it in (local disk, OneDrive, network, Dev Drive)?
- [ ] After **Ctrl+C**, could you start the server again straight away, or did it say the port was busy, and for how long?
- [ ] Does `py src\new_chat_folder_chooser.py --trial` open Windows' folder window in front of your other windows, and print `ok` after you choose a project folder (or `Cancelled.` after Cancel)? It prints no path and saves nothing.
- [ ] Did the terminal view draw properly in Windows Terminal? Did `--tab` and `--split` open the board and one tab or pane per agent, and did a waiting agent's tab show the warning ring?
- [ ] Did "Open in Claude" or "Open in Codex" at a desk open the app?

**Security:** if you find a way for an agent, a page it wrote, or another program to reach something this guide or the README says it cannot (the key in `%USERPROFILE%\.live-room`, files outside a preview's folder, the port), please **do not post it publicly**. Report it privately through the repository's [security advisory page](https://github.com/glo6al/motet/security/advisories/new).

Thank you for trying it.
