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
winget install --id Git.Git -e
```

Then **close PowerShell and open a new one**.

**Option B: the installer.** Download it from <https://git-scm.com/download/win>, run it, and accept the defaults. Then open a new PowerShell window.

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

`--tab` and `--split` are for iTerm2 on macOS; on Windows they say so and stop. If you used `$env:PORT`, set it in this window too.

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

Motet **watches** on Windows: the live stage, risk flags, previews (with their styles, scripts and images, Python 3.12 or later), your own app icons and Unlock all work. **Motet's own answers** and its model menu work too, once the key file is private (step 6); so far only the automated tests have checked them on Windows. Everything that **acts** on your agents is switched off for now, apart from Allow / Deny, messaging a Claude chat, and the bridge between agents with Claude chats as recipients, which are new and not yet checked on a real PC (see below). The goal is parity with the Mac, and these are still to come:

- **Messaging Codex chats.** It needs the Codex desktop app on a Mac for now; Send stays off and says so.
- **Parts of the bridge between agents:** Codex chats as recipients, the Antigravity plugin and inbox, and the offline recovery and clear commands (see below); and the older Claude channel launcher (`scripts/watch_claude.py`).
- **New chat.**
- **Sending files to a chat.** You cannot attach files to a Claude or Codex chat on Windows yet: its message box shows "Sending files is not available on Windows yet. Ordinary messages still work." Ordinary messages still send.
- **Motet's own file reading now starts on Windows, but is not yet checked on a real PC.** Ask Motet about a PDF or PNG as on the Mac, once its key and its reading policy are saved privately and while the policy's dates are current (the guide gives them): [Motet file reading](MOTET_FILE_READING.md#on-windows) gives the PowerShell line and the `icacls` command for the policy file. Without the policy, or with one that is not yours alone, file reading stays off and ordinary questions still send. Motet keeps the files in its private storage in `%USERPROFILE%\.live-room\attachments`, which holds itself to the same rules as messaging: no link or junction anywhere in its path, owned by you, with an access list naming only you and SYSTEM (Administrators may also appear on the folders); it must be on an NTFS drive (not ReFS, a Dev Drive or a FAT USB stick), and it is refused inside a OneDrive placeholder folder. While Motet runs, the storage folder and the folders above it cannot be renamed or deleted. If a file is open in another program when Motet removes it, Motet tries again later. If the storage cannot start, a chat's message box says "Motet could not open its private file storage". Automated tests on Windows cover these checks; nobody has tried them on a real PC yet. If you try it, please tell us what happened.
- **Dictation.** The whole feature is off on Windows, not only the speech engine.
- **Codex cloud agents** on the stage, which are found through the process list.
- **Open in Antigravity.**
- **iTerm2 terminal tabs.** iTerm2 is Mac only.

Details on the rest:

- **Messaging a Claude chat now starts on Windows, but is not yet checked on a real PC.** It goes through the optional Motet plugin for Claude Code, as on the Mac: install it once with the two commands in [the plugin guide](../integrations/claude-mod/README.md#install-once-for-your-user-account), giving the full Windows path to the `integrations\claude-mod` folder, then choose **Set up Claude** in Motet. Automated tests check it on Windows, including the plugin talking to a real Motet server, but nobody has yet tried it with a real Claude Code on a real PC, so we do not know yet whether Claude Code loads the plugin on Windows at all. If you try it, please tell us what happened. Two differences from the Mac: the chat must be on Motet's stage before it can connect (an idle chat cannot connect from its saved details yet), and sending it files is still off. Motet keeps messages in `%USERPROFILE%\.live-room`. Motet checks the folder each time it starts: it must not be a link or junction, it must be owned by you, and its access list may name only you, SYSTEM, Administrators and the folder's owner. A folder Motet creates now starts with a private access list (you and SYSTEM only). A folder made by an earlier Motet follows your user profile's permissions, which normally pass. The message database and its lock must also name only you and SYSTEM; Motet creates them that way. Motet opens this folder through the same handle-by-handle walk as the bridge: the folders above it (your profile) are resolved once, so a profile moved with a junction or a `subst` drive is fine, but `.live-room` itself must not be a link or junction, must resolve to a local disk (not a network drive), must not sit inside a OneDrive or other cloud folder whose files are placeholders, and every folder from the drive down must be one your account may open. If anything fails the check, messaging stays off and the page says why. If you first started Motet from an administrator window, the folder may belong to the Administrators group; make it yours with `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"`.
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

**What it does:** it prints a short report: your Windows and Python versions; whether the Codex, Claude, Cursor and Antigravity apps and command-line tools are installed (and their versions); which `.codex` folders exist; the names of related named pipes; which `claude://`, `codex://`, `cursor://` and `antigravity://` links have a handler; whether Git Bash and Windows Terminal are there; and which of those programs are running. To find that out it lists installed packages through PowerShell (`Get-AppxPackage`; PowerShell may update its own caches while doing so), lists running programs with `tasklist`, and runs `--version` or `--help` on a command-line tool only when it is a real program (never an app alias or a script shim). If a Codex program is running it checks that program's command line only for the presence of the words `exec-server` and `app-server`, and never prints the command line.

**What it does not do:** it changes nothing of yours, writes no files, makes no network connection, opens no app window and sends no message. It reads no file contents beyond what each check needs, and prints none. It only checks whether `%USERPROFILE%\.claude\settings.json` has a hooks section, and prints just the hook event names. Your profile path, user name, computer name, e-mail addresses and anything that looks like a key are replaced before printing, but redaction is not perfect: **please read the output before you post it** and remove anything you would rather not share.

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
- [ ] Did each agent show the right project folder name? Cursor agents especially: we have not seen how Cursor names folders on Windows. Please paste the output of `dir "$env:USERPROFILE\.cursor\projects"`.
- [ ] Is there a Claude desktop app sessions folder? Paste the output of:
  ```powershell
  dir "$env:APPDATA\Claude" ; dir "$env:APPDATA\Claude\claude-code-sessions" -ErrorAction SilentlyContinue
  dir "$env:LOCALAPPDATA\Claude-Data\*\claude-code-sessions" -ErrorAction SilentlyContinue
  ```
- [ ] Did a page an agent wrote show as a rendered preview at its desk, with its styles and images? Which folder was it in (local disk, OneDrive, network, Dev Drive)?
- [ ] After **Ctrl+C**, could you start the server again straight away, or did it say the port was busy, and for how long?
- [ ] Did the terminal view draw properly in Windows Terminal?
- [ ] Did "Open in Claude" or "Open in Codex" at a desk open the app?

**Security:** if you find a way for an agent, a page it wrote, or another program to reach something this guide or the README says it cannot (the key in `%USERPROFILE%\.live-room`, files outside a preview's folder, the port), please **do not post it publicly**. Report it privately through the repository's [security advisory page](https://github.com/glo6al/motet/security/advisories/new).

Thank you for trying it.
