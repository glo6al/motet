# Motet file reading

Open the [workspace](http://127.0.0.1:8793/) on the computer running Motet. File reading works on macOS and Linux with private POSIX file permissions, and on Windows on an NTFS drive with private access lists (see [On Windows](#on-windows) below; on Windows it is new and not yet checked on a real PC). If Motet's private file storage cannot start (on a computer where the storage checks fail, or on Windows on a drive that is not NTFS), the workspace offers no file button for Motet and says why in a chat's message box; restarting Motet tries the storage again. Motet accepts original PDFs and PNGs. File questions use Haiku 5.5 when the picker is set to **Auto** or explicitly to **Haiku 5.5**. The answer shows the model it used.

1. Save your Anthropic API key in `~/.live-room/anthropic-key` as a private regular file with mode `0600`. Create `~/.live-room/officer-document-policy.json` as a private regular file with mode `0600`.
2. Restart Motet so it loads the policy. If the policy is missing or unreadable when Motet starts, or outside its dates, file reading stays off: a staged file question says Motet cannot take it with the current setting, and ordinary questions still send. Open Motet’s conversation and choose **Attach files**, or drag a file onto the message box. A tray holds up to five files and 300,000 bytes total, counting both new and kept files.
3. Send a question to provide only the originals shown in its tray to Anthropic under your API key. Anthropic bills your API account for usage. Send a new attachment with an empty message to keep it in this conversation without a model call.
4. To add a kept file to a question, open **Files** and choose **Use in next question**. **Take out** removes it from the tray and leaves the kept copy in the conversation. **Ask a follow-up from your files** adds the files used by that answer that are still kept. Ordinary questions continue to use Motet’s regular conversation.

A question using only kept files can include up to eight earlier answers from the same conversation as text. Each earlier answer qualifies only if all the files it used are in the current tray. File questions have no access to the stage’s recorded activity. Motet stores drafts and kept originals under `~/.live-room/attachments/` (`%USERPROFILE%\.live-room\attachments\` on Windows).

This example gives file questions a one-time **USD 6.00 total** allowance. Choose your own limit before the policy is registered.

```json
{
  "version": 1,
  "evidenceId": "haiku-pdf-png-2026-11-15",
  "scopeId": "officer-files",
  "scopeRevision": "v1",
  "usdLimit": "6.00"
}
```

Each attempted file question reserves up to **USD 1.11** against the local allowance. The reservation is a budget check, not an invoice. It does not reset automatically. An unknown outcome may hold its reservation. This release accepts the example policy from **2026-11-15 at 00:00 UTC** until **2026-11-22 at 00:00 UTC**, exclusive. Once that qualification expires, installing the example again will not restore file questions. Update to a Motet build with a current qualification and use its policy instructions.

## On Windows

File reading on Windows is new: automated tests on Windows cover it, but nobody has tried it on a real PC yet. If you do, please tell us what happened. The key file and the policy file live in `%USERPROFILE%\.live-room`. Windows has no owner-only file modes, so Motet checks each file's access list instead, as it does for the key (see [Keys and settings on Windows](MOTET.md#keys-and-settings-on-windows)): the file must be owned by you, give access only to you (and the system account), not take its permissions from its folder, and have only one name. A link, junction or other reparse point in place of the file or of the folder is refused. Motet never changes the permissions of a file you made; if the policy is refused, file reading stays off as described above.

Save the key first, as [Keys and settings on Windows](MOTET.md#keys-and-settings-on-windows) shows. Then write the policy as plain UTF-8 text in PowerShell (Windows PowerShell's `>` writes UTF-16, which Motet does not read), with your own limit in place of `6.00`:

```powershell
Set-Content -NoNewline -Encoding ascii "$env:USERPROFILE\.live-room\officer-document-policy.json" '{"version":1,"evidenceId":"haiku-pdf-png-2026-11-15","scopeId":"officer-files","scopeRevision":"v1","usdLimit":"6.00"}'
```

Then make the file yours alone. Run this, and the `/setowner` command below if you need it, in a normal Command Prompt, not one opened with "Run as administrator": in an administrator window `%USERNAME%` can be the administrator's account, which would give the file to the wrong account.

```
icacls "%USERPROFILE%\.live-room\officer-document-policy.json" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:F"
```

If you made it from an administrator window, it may belong to the Administrators group; make it yours first with `icacls "%USERPROFILE%\.live-room\officer-document-policy.json" /setowner "%USERDOMAIN%\%USERNAME%"`, then run the command above. Restart Motet so it loads the policy.

Use **Files** to remove a copy kept by Motet. Removing it also withdraws answers that depend on it. If a file question is unconfirmed, use **Look again** to check its existing request.
