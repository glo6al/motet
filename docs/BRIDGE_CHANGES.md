# Bridge change log

This log has one entry for every change to Motet's agent-to-agent bridge (the `watch-peer` MCP bridge and the code around it). Its purpose is to make a repeated fix visible: when a new problem looks like an old one, the `Same cause as` line shows it.

Rule: every bridge change adds its entry in the same commit or the same pull request as the change. Entries are numbered in date order. This first version was written from git history, so it covers commits from 29 Oct to 26 Nov 2026 and groups small test-only fixes.

Kinds: `root cause` removes the cause. `mitigation` makes the symptom easier to live with, but the cause remains. `workaround` goes around a fault. `feature` is new behaviour, not a repair.

See also `docs/bridge/RECOVERY.md` and `docs/PEER_BRIDGE.md`.

## Open causes

- **Codex log format.** Codex owns the format. Records over 32 MiB and malformed records still withhold the reply, by design. A refusal text and a narrower `--history` are the only help for malformed metadata (a workaround).
- **Idle Codex chats.** A Codex chat quiet for more than two hours counts as `unverified`, and sends are refused. The same refusal also covers an unfinished scan, an unreadable process list and an unknown chat.
- **Refused messages are not stored.** A refused message leaves no record that could be retried.
- **Replayed start receipt.** A replayed start receipt resets a hold's age, so the bell can come back. Not fixed.
- **Mixed input.** A native message arriving at the end of a bound Claude turn withholds the reply. This is a correct refusal, with a possible refinement.
- **Old uncertain rows.** A few old Claude rows have stayed uncertain since mid-November. A row can also end `unknown` with an empty error after a gap, a temporary failure and a read.
- **Older plugins.** Subfolder receiving needs plugin 0.6.4 in each Claude chat.
- **Windows.** Tested by CI only so far. Codex recipients, offline recovery and the clear command stay off there.
- **Repeated rules in several copies.** Wrapper matching and folder comparison live in more than one place and have drifted before. Record size now has one shared limit.
- **Timing in tests.** Tests that guess machine speed turned the development branch red three times.

## Entries, newest first

### BR-044 · 26 Nov 2026

Test-only fixes: Claude fixture leases tied to runner speed, and folder tests that assumed `/` as the root on Windows.

- Commits: e536f6e, f3bdc15
- Kind: root cause
- Area: tests

### BR-043 · 26 Nov 2026

A Claude chat working in a subfolder of its project was shown as unavailable, because of the exact-folder match added by the 5 Nov entry. Plugin 0.6.4 reports the working folder and Motet checks it fresh. Adds enrollment migration and a receipt-only mode. Older plugins keep the exact-folder rule.

- Commits: 0fa6255, 1147efd, 712f49b, 5b14057, ce481f9, ed1218b, 72026ed
- Kind: root cause
- Area: folder-matching

### BR-042 · 26 Nov 2026

Codex replies were withheld because compacted records of 7 to 15 MB exceeded the 1,000,000-byte limit. One shared 32 MiB limit now serves every reader, and a big record is read to its end. Idle Codex chats also get a truthful reason instead of a transport-fault message.

- Commits: 6ae5b26, 91e7612
- Kind: root cause
- Same cause as: BR-001, BR-017, BR-024, BR-031
- Area: codex-records

### BR-041 · 25 Nov 2026

Motet called agent and relay text "the owner's words". New attribution rules say who wrote each message, confirm a delivery word for word, and pick the first delivered copy by log order. Wrapper handling was again written separately.

- Commits: f02a81d, 3d5db62, 3322157, 0f1a115
- Kind: root cause
- Same cause as: BR-005, BR-020
- Area: reply-correlation

### BR-040 · 25 Nov 2026

Step 6c.5b: send files to Claude chats on Windows.

- Commits: 9e58738
- Kind: feature
- Area: windows

### BR-039 · 25 Nov 2026

Same-tick windows were refused on Windows because subtracting timestamps rounded wrongly. Windows are now compared without subtracting.

- Commits: 35bba2c
- Kind: root cause
- Area: windows

### BR-038 · 24 Nov 2026

Test-only fixes: wait for the simulated peer's reply, and hold transport test steps by event.

- Commits: f458b92, d235745
- Kind: root cause
- Area: tests

### BR-037 · 24 Nov 2026

Step 6c.4: the attachment namespace fence and owner runtime on Windows. Copied namespace records are refused with their own reason.

- Commits: 9150ca2, 69e99bb
- Kind: feature
- Area: windows

### BR-036 · 24 Nov 2026

Step 6a: the bridge opens on Windows for Claude recipients. Folder walk and credential read go through Motet's Python. Docs state what works and what stays off.

- Commits: d6f908a, 6e7501b, a83eff2, 07aab72, 47ab0cd, 20f575c
- Kind: feature
- Area: windows

### BR-035 · 23 Nov 2026

Windows could not remove the database because a preflight connection was never closed. It is now closed in `finally`.

- Commits: ce73254
- Kind: root cause
- Area: windows

### BR-034 · 23 Nov 2026

Windows and Linux port, steps 5a and 5b: Claude messaging starts there, private storage is shared, Codex work is refused off the Mac, and the bridge is refused on Windows with one stated reason.

- Commits: c7b1eba, 77b4210, f797aae, b937469, 68f66ef, 6a973c5, a23141c, eaf604a
- Kind: feature
- Area: windows

### BR-033 · 20 Nov 2026

Test-only fixes: coordinator fixture reads, and recovery proofs that counted a legitimate receipt replay as a second submission.

- Commits: ef49174, d6ca073
- Kind: root cause
- Area: tests

### BR-032 · 20 Nov 2026

Held chats were only visible in a terminal. Settings now lists chats on hold, with a Clear button and a bell notice. Docs updated.

- Commits: 6fafa9e, 6dd4a53, 60ac5a0, 3a51133
- Kind: mitigation
- Area: recovery

### BR-031 · 20 Nov 2026

Verifies that a proven turn end settles a request end to end, including after an oversized Codex record and a restart. The reply stays withheld.

- Commits: 960afdd
- Kind: mitigation
- Same cause as: BR-001, BR-017, BR-024
- Area: codex-records

### BR-030 · 19 Nov 2026

Quieter relay conversation in Workspace. Reply history is kept.

- Commits: 877a9f9, 7f9c619
- Kind: root cause

### BR-029 · 19 Nov 2026

Follow-ups: clear stale transcript warnings, report startup inbox timeouts, show deferred receipt counts and ages, clear transient receipt status on import.

- Commits: 2f23f97, 7116539, e2a4681, 77bf32e
- Kind: root cause
- Area: claude-delivery

### BR-028 · 19 Nov 2026

Stage three: `get_reply` shows progress, refusals name an id the clear tool accepts, and refusal text is short enough for older adapters.

- Commits: 1188657, 0bf5282, 19eab79, 6cbc935
- Kind: root cause
- Area: recovery

### BR-027 · 19 Nov 2026

Requests locked after 15 minutes on long work, because one age limit served dispatch, running work and the hold. Started work no longer expires by age. Owner clear still works for aged attempts. Replaces telling agents to answer "noted" to dodge the limit.

- Commits: cdefa15, e039633, ff82b25
- Kind: root cause
- Area: capacity

### BR-026 · 19 Nov 2026

Stage one of progress tracking: records verified progress. No change to dispatch.

- Commits: e71b2c8, 4d91193, 8cd0f57
- Kind: feature
- Area: capacity

### BR-025 · 19 Nov 2026

Review follow-ups to the previous entry. Untyped errors stranded a receipt for good. A brief transcript read failure flipped live requests to unknown. Both fixed before release.

- Commits: fb9c63a, e08f59c
- Kind: root cause
- Area: claude-delivery

### BR-024 · 19 Nov 2026

Fixes four lock causes: Claude submission receipts lost after a reload, a Codex upgrade check that stopped tracking after a compacted record, folders not made canonical, and legacy marker-only answers refused. Adds an optional inbox for another provider.

- Commits: 08769f6
- Kind: root cause
- Same cause as: BR-001, BR-017
- Area: codex-records

### BR-023 · 19 Nov 2026

Restored replies said "time not recorded". The stored time is now exposed and the page uses it.

- Commits: f5b531d
- Kind: root cause

### BR-022 · 18 to 19 Nov 2026

Test-only fixes: transport fixtures with timing guesses, split Unicode in peer fixtures, test reporter format and elapsed-time reads.

- Commits: 96e53d4, 3632b56, 90c6975, a0b140e
- Kind: root cause
- Area: tests

### BR-021 · 18 Nov 2026

Released 0.1.0 refused messages with "could not verify capacity" through a second writer. Admission now does one bounded rebuild before its transaction. This fixes the cause, not one writer.

- Commits: 8db096e
- Kind: root cause
- Same cause as: BR-018
- Area: capacity

### BR-020 · 18 Nov 2026

The conversation page showed the full plugin wrapper because its matcher expected the envelope at the very start. The page now recognises the wrapper.

- Commits: fb9b2a7
- Kind: mitigation
- Same cause as: BR-005
- Area: reply-correlation

### BR-019 · 18 Nov 2026

Messaging and files broke after an old-SQLite start or a Python upgrade. Ordinary messaging now continues; files are refused with a plain reason.

- Commits: 50d1970, 295f8de
- Kind: root cause
- Area: capacity

### BR-018 · 17 Nov 2026

Plain-text sends were refused while peer history was retained. Peer reconciliation during a send made the capacity check stale. That one writer is removed. Adds the "could not verify capacity" refusal.

- Commits: 8c7d1fa
- Kind: mitigation
- Area: capacity

### BR-017 · 14 to 15 Nov 2026

A Codex chat was locked as "requires recovery" after an oversized log record. Two repairs: an exact turn end now settles the request, with the reply withheld; and the owner can clear a request through the running server (`scripts/watch_bridge_clear.py`, see `docs/bridge/RECOVERY.md`). The size limit itself was not changed. Published inside the release commit.

- Commits: 758f647
- Kind: mitigation
- Same cause as: BR-001
- Area: codex-records

### BR-016 · 12 Nov 2026

Retires the automatic reply-ready notices from the 5 Nov entry. Agents now check `get_reply` at task checkpoints. The bridge no longer puts text into chats by itself. Published inside the release commit; the original commit is not in this history, and the reason for the reversal is not recorded in git.

- Commits: 758f647
- Kind: root cause
- Area: reply-return

### BR-015 · 12 Nov 2026

Long messages were refused unclearly. Messages may now be 16,000 characters, with plain size errors.

- Commits: e96f2f7
- Kind: root cause

### BR-014 · 12 Nov 2026

Old uncertain attempts blocked chats for good. Adds an offline recovery script. Its reader uses a 32 MiB record limit, so it now differs from the 1,000,000-byte limit in the live reader.

- Commits: d46771f
- Kind: mitigation
- Area: recovery

### BR-013 · 12 Nov 2026

Completions were lost across Claude reconnects and Codex log rollovers. The plugin now follows a verified continuation. Also adds the `capacity_error` hold that keeps a chat with an uncertain request from receiving more.

- Commits: 70e6378
- Kind: root cause
- Area: claude-delivery

### BR-012 · 31 Oct to 12 Nov 2026

Test-only fixes: tests that relied on wall-clock load, scheduler turns or older Python and Windows behaviour. No product change.

- Commits: 1e7dc6d, 42a0808, a19b590, dc9db56, e4943bd
- Kind: root cause
- Area: tests

### BR-011 · 8 Nov 2026

Unresolved notices can be dismissed. Adds native messaging guidance.

- Commits: d98ca2d
- Kind: feature

### BR-010 · 5 Nov 2026

Shorter shared envelope with the same safeguards. Accepts the documented Claude tool-completion receipt format. Seeds cold return routes from a verified snapshot.

- Commits: 24b989e, 1884ef3, 5180601
- Kind: root cause
- Area: claude-delivery

### BR-009 · 5 Nov 2026

New: replies return to the asking chat automatically as notices. Retired on 12 Nov (see later entry).

- Commits: c539981
- Kind: feature

### BR-008 · 5 Nov 2026

Idle Claude chats could not receive, because receivers had to be in the room. Receiving now works from verified metadata. Side effect: it adds an exact project-folder match that later hid chats working in a subfolder.

- Commits: 7c86948
- Kind: root cause
- Area: folder-matching

### BR-007 · 4 Nov 2026

Verified Claude chats dropped their connection. Stale lifecycle checks are now discarded and the connection stays up in the background.

- Commits: b018242
- Kind: root cause
- Area: claude-delivery

### BR-006 · 4 Nov 2026

Adds global Claude plugin peer messaging: consent, protocol negotiation, atomic claims and correlated replies.

- Commits: 6069905
- Kind: feature

### BR-005 · 1 Nov 2026

Claude Code wraps plugin input in its own attribution text, so the request was not matched. The plugin now accepts the raw envelope or that exact wrapper. Only the plugin copy of the matching logic was fixed.

- Commits: 5b2ec75
- Kind: mitigation
- Area: reply-correlation

### BR-004 · 1 Nov 2026

Adds optional owner messaging to existing Claude chats through a plugin.

- Commits: e14d0ce, 9115bde
- Kind: feature

### BR-003 · 31 Oct 2026

Reports a peer reply that is blocked because owner tracking stopped. Shows unresolved queue outcomes apart from failures. Reports capabilities per chat and checks transcript eligibility safely.

- Commits: 2a5aa0f, 9871f94, a925574, d86cf62
- Kind: root cause
- Area: readiness-wording

### BR-002 · 31 Oct 2026

Adds bounded Codex controls, recovery of unresolved outcomes and the shared MCP bridge (`list_agents`, `send_message`, `get_reply`) with guarded Codex delivery.

- Commits: 74b8317, 7b7b3c9
- Kind: feature

### BR-001 · 29 Oct 2026

First Officer messaging to Codex and Claude chats. Adds the message ledger and the Codex reply tracker. It also sets a 1,000,000-byte limit on one Codex log record.

- Commits: 65b5eeb
- Kind: feature
- Area: codex-records
