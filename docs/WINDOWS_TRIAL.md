# Windows real-PC trial: Claude run by Motet

For anyone with a real Windows PC who is willing to help. Automated tests on GitHub's Windows machines drive a
stand-in for Claude Code; this trial uses the real Claude Code 2.1.323 and checks what those tests cannot.

- Code under test: the latest `dev` branch.
- Time: about 45 minutes, most of it the install and the Defender scan.
- What you collect stays on your PC: the output you paste into your text file is not sent anywhere unless you
  paste it into the issue yourself.

**What goes over the network.** The trial itself is not offline:

- It downloads software: Python and Git for Windows if you do not have them, the Motet `dev` branch from GitHub,
  and the Claude Code installer script and Claude Code 2.1.323 from Anthropic (step 5).
- You sign in to Claude Code with your own account (step 6).
- The trial chats (steps 16 to 24) are real Claude Code chats. As with any Claude Code chat, their instructions,
  what Claude reads in the trial folder, and Claude's replies go to Anthropic under your account, and use its
  normal allowance. Claude Code may also send Anthropic its usual usage data and look for updates.
- Depending on your settings, Microsoft Defender may check the new Claude Code program with Microsoft's cloud
  service when it scans it.

Use only the throwaway trial folder, so no real project's files are read.

Use a normal PowerShell window (not "Run as administrator") for every step. Keep a text file open and paste each
step's output into it under the step number.

## Before you start

1. Have ready: Windows 11, Python 3.12 or later from python.org or winget (not the Microsoft Store), Git for
   Windows, and a checkout of Motet on the latest `dev` (`git checkout dev; git pull` in the Motet folder).
2. Check the architecture:
   ```powershell
   $env:PROCESSOR_ARCHITECTURE
   [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
   (Get-CimInstance Win32_OperatingSystem) | Select-Object Caption, BuildNumber, OSArchitecture
   py --version
   ```
   Expected: `AMD64` / `X64` on an Intel or AMD PC, `ARM64` / `Arm64` on an ARM PC.
3. Close every other Claude Code window and the Claude desktop app, so that any `claude.exe` you see later is the
   one Motet started.
4. Make a throwaway project folder, not a real project:
   ```powershell
   mkdir "$env:USERPROFILE\motet-trial"; Set-Content "$env:USERPROFILE\motet-trial\README.md" "Trial folder."
   ```

## Install Claude Code 2.1.323 natively

5. Install that exact version with the native installer (not npm):
   ```powershell
   & ([scriptblock]::Create((irm https://claude.ai/install.ps1))) 2.1.323
   ```
   If the installer refuses the version or installs another one, stop and report its last lines.
6. Sign in to Claude Code yourself, once, in the trial folder. Motet never handles your login.
   ```powershell
   cd "$env:USERPROFILE\motet-trial"; claude --version; claude
   ```
   `claude --version` should print 2.1.323. Sign in, then type `/exit`.

## The installed file: name, path, links, hash

7. List what the installer wrote:
   ```powershell
   Get-ChildItem "$env:USERPROFILE\.local\share\claude\versions" -Force |
     Format-List Name, Length, Attributes, LinkType, Target
   Get-ChildItem "$env:USERPROFILE\.local\bin" -Force | Format-List Name, Length, Attributes, LinkType, Target
   Get-Command claude -All | Format-List CommandType, Source
   ```
   Motet accepts only `%USERPROFILE%\.local\share\claude\versions\2.1.323` or `...\2.1.323.exe`. Note which name
   exists, and whether it is a file or a folder.
8. Count the file's names (Motet refuses a program file with more than one name). Use the exact name from step 7:
   ```powershell
   $exe = "$env:USERPROFILE\.local\share\claude\versions\2.1.323"     # add .exe if that is the real name
   py -c "import os, sys; print('links:', os.stat(sys.argv[1]).st_nlink)" $exe
   fsutil hardlink list $exe
   (Get-Item $exe -Force).Attributes
   ```
   Expected: `links: 1`, one line from fsutil, and no `ReparsePoint` in the attributes.
9. Check the hash and size:
   ```powershell
   Get-FileHash -Algorithm SHA256 $exe; (Get-Item $exe).Length
   ```
   Expected for x64: `5064E6E0572DCA86257363CA04D48653EEF20EF27F1BD04B504ED7199ED3FD13`, 367634080 bytes.
   Expected for ARM64: `3071C54F466E40E668C273B53ED8942EFCE35BF069D66A69668F74299B0CB888`.

## Does Motet find it, and how long does the check take

10. In the Motet folder:
    ```powershell
    py -c "import os, sys; sys.path.insert(0, 'src'); import claude_managed_windows as w; p = w.locate_claude(os.path.expanduser('~')); print(os.path.basename(p), w.refused_location(p), w.native_architecture())"
    ```
    Expected: the file name from step 7, `None`, and `x64` or `arm64`.
11. Time Motet's full check (hash through a handle that stops writers), twice:
    ```powershell
    py -c "import os, sys, time; sys.path.insert(0, 'src'); import claude_managed_windows as w; p = w.locate_claude(os.path.expanduser('~')); t = time.monotonic(); fd = w.open_pinned(p, w.windows_pin()); os.close(fd); print('verified in %.1f s' % (time.monotonic() - t))"
    Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled, AMServiceEnabled
    ```
    Paste both times (the first is usually the slow one) and the Defender line. Motet allows 60 seconds. If it
    fails, paste only the last line (the error type and message).

## The readiness record

12. Start Motet once so that it makes its private folder, then stop it with Ctrl+C:
    ```powershell
    py src\server.py
    ```
13. Write the record exactly as docs/WINDOWS.md ("Claude run by Motet: owner steps") says:
    ```powershell
    py -c "import json, os, sys; sys.path.insert(0, 'src'); import claude_managed as m, private_storage as ps; record = json.dumps(m.local_configuration_identity()).encode('ascii'); fd = ps.create_private_file(os.path.join(os.path.expanduser('~'), '.live-room', m.READINESS_FILENAME)); os.write(fd, record); os.close(fd); print('Wrote', m.READINESS_FILENAME)"
    icacls "$env:USERPROFILE\.live-room\managed-claude-readiness.json"
    py -c "import os, sys; sys.path.insert(0, 'src'); import claude_managed as m; print('ready:', m.load_configuration_readiness(os.path.expanduser('~/.live-room'), enabled=True))"
    ```
    Expected: `Wrote ...`, an access list naming only you and `NT AUTHORITY\SYSTEM`, and `ready: True`. Do not
    paste the record's contents.

## One chat: start, one message, one Allow or Deny, Stop

14. Start Motet (`py src\server.py`), open http://127.0.0.1:8793/ and press **Unlock**. Open **New chat**.
    Is **Claude** offered? If not, paste the reason the page shows, word for word. Codex should not be offered,
    with a reason saying it needs macOS: paste that line too.
15. **Add project folder** and choose `motet-trial` in the Explorer window. Did the window open in front? Did
    the folder appear in the list?
16. Start a Claude chat in `motet-trial` with this instruction:
    `Create a file named notes.txt containing the word hello.`
    Note the time. Did the chat appear on the stage, and how many seconds did that take? If it failed, paste the
    words the page shows.
17. A permission request for **Write** should appear in Motet (not in any Claude window). Press **Allow**.
    Then check:
    ```powershell
    Get-Content "$env:USERPROFILE\motet-trial\notes.txt"
    ```
    Did the reply appear in Motet? Did the message show as answered (not "unknown")?
18. While the chat is still open, record its processes (no command lines are printed):
    ```powershell
    Get-CimInstance Win32_Process -Filter "Name='claude.exe'" | Select-Object ProcessId, ParentProcessId
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Select-Object ProcessId, ParentProcessId, @{n='supervisor'; e={$_.CommandLine -like '*claude_managed_runner.py*'}}
    ```
    Write down the claude.exe IDs and the supervisor's ID (the python.exe row with `supervisor` True). The
    claude.exe's parent should be the supervisor.
19. **Stop.** The page has no Stop control for a chat Motet started (neither has the Mac's), so stop it with
    Motet's own shutdown call, from a second PowerShell window in the Motet folder:
    ```powershell
    py -c "import glob, json, os, sys, uuid; sys.path.insert(0, 'src'); import claude_managed as m; home = os.path.expanduser('~'); state = os.path.join(home, '.live-room'); runs = glob.glob(os.path.join(state, 'managed-claude', '*-*-*-*-*')); run = max(runs, key=lambda d: json.load(open(os.path.join(d, 'manifest.json')))['createdAt']); s = m.ManagedClaude(state, home=home, enabled=True, configuration_ready=True).shutdown(os.path.basename(run), str(uuid.uuid4())); print(s['state'], s['nativeExit'])"
    ```
    Expected: `exited` and an exit record. Paste the line.
20. Confirm nothing is left behind (use the IDs from step 18):
    ```powershell
    Get-Process -Id <claude ids>, <supervisor id> -ErrorAction SilentlyContinue
    Get-Process claude -ErrorAction SilentlyContinue | Select-Object Id
    ```
    Expected: nothing for both. What does the page show for the chat now?

## Closing the supervisor ends Claude and everything it started

21. Start a second chat in `motet-trial` with:
    `Run the command ping -n 120 127.0.0.1 and tell me when it finishes.`
    When the permission request appears, note whether the tool is **Bash** or **PowerShell** (Motet does not set
    `CLAUDE_CODE_GIT_BASH_PATH`, so this shows Claude Code's own choice), then press **Allow**.
22. While ping runs, repeat step 18, and also:
    ```powershell
    Get-Process ping -ErrorAction SilentlyContinue | Select-Object Id
    ```
23. Close the Motet server window (Ctrl+C) and wait 10 seconds. Is claude.exe still running? It should be: the
    supervisor is detached from the server's window. Start Motet again: does the chat come back on the stage?
24. Now end the supervisor itself, as a crash would:
    ```powershell
    Stop-Process -Id <supervisor id> -Force
    Start-Sleep 5
    Get-Process -Id <claude ids> -ErrorAction SilentlyContinue
    Get-Process ping -ErrorAction SilentlyContinue
    ```
    Expected: nothing for both. claude.exe and ping.exe end with the supervisor. What does the page show for the
    chat? (Expected: its outcome is uncertain; Motet does not restart it.)

## The Claude Code behaviours CI cannot prove

25. Answer each with yes or no, from steps 16 to 20:
    - **stream-json in and out:** the chat started and its reply appeared in Motet.
    - **`--await-claim`, then `claim_session` and `get_status`:** the chat reached the stage. Motet admits it only
      after Claude confirms its session ID and folder. A failure here shows "The exact Claude session and folder
      could not be verified" or "The launch outcome is unknown".
    - **`--permission-prompt-tool stdio`:** the Write request appeared in Motet and Allow worked.
    - **`--replay-user-messages`:** the message showed as answered, not "unknown".
26. If anything in steps 14 to 24 failed, paste this summary of the newest run. It prints states only, no text:
    ```powershell
    py -c "import glob, json, os, sys; sys.path.insert(0, 'src'); import claude_managed_runner as r; state = os.path.expanduser('~/.live-room'); runs = glob.glob(os.path.join(state, 'managed-claude', '*-*-*-*-*')); run = max(runs, key=lambda d: json.load(open(os.path.join(d, 'manifest.json')))['createdAt']); s = r.read_snapshot(run); print(json.dumps({'state': s['state'], 'identityVerified': s['identityVerified'], 'nativeExit': s['nativeExit'], 'messages': [x['state'] for x in s['messages']], 'permissions': [(p['tool'], p['state'], p['decision']) for p in s['permissions']]}))"
    ```
27. List the versions folder again (step 7, first command). Is 2.1.323 still there, or did Claude Code update
    itself and remove it?

## What to paste back

28. Paste, by step number: 2, 7, 8, 9, 10, 11, 13 (the `Wrote`, icacls and `ready:` lines), 14, 15, 16, 17,
    18, 19, 20, 21 (Bash or PowerShell), 22, 23, 24, 25, 26 (only if something failed) and 27.
29. **Before you paste, redact usernames**: replace your Windows user name, your PC name and any e-mail address
    with `<user>`, `<pc>` and `<email>` everywhere, including inside paths and icacls output.
30. Never paste the readiness record's contents, anything from `.live-room` (the token, ledgers, registry), your
    Claude login, or the chat's transcript.
