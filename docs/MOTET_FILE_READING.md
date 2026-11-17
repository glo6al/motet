# Motet file reading

Open the [workspace](http://127.0.0.1:8793/) on the computer running Motet. File reading requires macOS or Linux with private POSIX file permissions and a filesystem that supports the local storage checks. It is unavailable on Windows. Motet accepts original PDFs and PNGs. File questions use Haiku 5.5 when the picker is set to **Auto** or explicitly to **Haiku 5.5**. The answer shows the model it used.

1. Save your Anthropic API key in `~/.live-room/anthropic-key` as a private regular file with mode `0600`. Create `~/.live-room/officer-document-policy.json` as a private regular file with mode `0600`.
2. Restart Motet so it loads the policy. Open Motet’s conversation and choose **Attach files**, or drag a file onto the message box. A tray holds up to five files and 300,000 bytes total, counting both new and kept files.
3. Send a question to provide only the originals shown in its tray to Anthropic under your API key. Anthropic bills your API account for usage. Send a new attachment with an empty message to keep it in this conversation without a model call.
4. To add a kept file to a question, open **Files** and choose **Use in next question**. **Take out** removes it from the tray and leaves the kept copy in the conversation. **Ask a follow-up from your files** adds the files used by that answer that are still kept. Ordinary questions continue to use Motet’s regular conversation.

A question using only kept files can include up to eight earlier answers from the same conversation as text. Each earlier answer qualifies only if all the files it used are in the current tray. File questions have no access to the room’s recorded activity. Motet stores drafts and kept originals under `~/.live-room/attachments/`.

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

Use **Files** to remove a copy kept by Motet. Removing it also withdraws answers that depend on it. If a file question is unconfirmed, use **Look again** to check its existing request.
