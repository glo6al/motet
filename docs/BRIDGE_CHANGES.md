# Bridge change log

This log has one entry for every change to Motet's agent-to-agent bridge (the `watch-peer` MCP bridge and the code around it). Its purpose is to make a repeated fix visible: when a new problem looks like an old one, the `Same cause as` line shows it.

Rule: every bridge change adds its entry in the same commit or the same pull request as the change. Entries are numbered in date order. This first version was written from git history, so it covers commits from 29 Oct to 26 Nov 2026 and groups small test-only fixes.

Two entries are dated from earlier private history that was published inside the release commit 758f647 on 17 Nov: BR-017 (12 Nov) and BR-018 (15 Nov). Grouped entries use the date of their last commit.

How to add an entry: take the next BR number, put the entry at the top of the entries below, copy the format of the entries below, and fill in `Kind` and `Same cause as`.

Kinds: `root cause` removes the cause. `mitigation` makes the symptom easier to live with, but the cause remains. `workaround` goes around a fault. `feature` is new behaviour, not a repair.

See also `docs/bridge/RECOVERY.md` and `docs/PEER_BRIDGE.md`.

## Open causes

- **Codex log format.** Codex owns the format. Records over 32 MiB and malformed records still withhold the reply, by design. Malformed native metadata can leave discovery incomplete. `docs/PEER_BRIDGE.md` ("Directory scope and availability") gives the workarounds: narrow `--history` or exclude the provider with `--providers`. A malformed Codex title index needs repair or regeneration through native Codex tooling; narrowing history does not repair it.
- **Idle Codex chats.** A Codex chat quiet for more than two hours counts as `unverified`, and sends are refused. The same refusal also covers an unfinished scan, an unreadable process list and an unknown chat.
- **Refused messages are not stored.** A refused message leaves no record that could be retried.
- **Replayed start receipt.** A replayed start receipt resets a hold's age, so the bell can come back. Not fixed.
- **Mixed input.** A native message arriving at the end of a bound Claude turn withholds the reply. This is a correct refusal, with a possible refinement.
- **Old uncertain rows.** A row can end `unknown` with an empty error after a gap, a temporary failure and a read.
- **Older plugins.** Subfolder receiving needs plugin 0.6.4 or later in each Claude chat, and Claude Code 2.1.323 loads only 0.6.5 of those (BR-046).
- **Reloaded terminal chats.** After `/reload-plugins`, a terminal chat that Claude does not start again cannot show the plugin it is interactive, so it stays disconnected until the chat restarts. Claude Desktop chats reconnect by themselves (BR-046).
- **Reloaded chat changed before its first event.** A reloaded chat that ends, is cleared or switches conversation before its first event stays disconnected until it restarts, and `/watch-connect` is not available in it until then (BR-046).
- **Owner decisions with an older Motet.** Against a Motet without decision tickets (BR-047), plugin 0.6.5 orders the owner's decisions itself. Then an earlier plugin copy's manual `/watch-connect` that passed its last check just before a reload's `/watch-disconnect` can still reach Motet, which holds a connection nobody renews for up to its 12-second lease. And with two Claude windows of one chat, an opt-out in either holds until `/watch-connect` is run in that window. Upgrading Motet closes both.
- **Downgrade.** Downgrading is unsafe against the current ledger. Do not run an older build on it.
- **Receipt discard.** The receipt-discard command is not built, and timed automatic clear is off.
- **Sender connections.** Sender MCP connections must reconnect to get new guidance.
- **Older managed supervisor.** A managed Claude chat started by an older Motet refuses messages that Motet words. Those messages stay unresolved, and 32 of them fill that chat's unresolved allowance until it restarts. See `docs/MESSAGING.md`.
- **Windows.** Tested by CI only so far. Codex recipients, offline recovery and the clear command stay off there.
- **Repeated rules in several copies.** Wrapper matching and folder comparison live in more than one place and have drifted before. Record size now has one shared limit.
- **Timing in tests.** Tests that guess machine speed turned the development branch red three times.

## Entries, newest first

### BR-048 · 27 Nov 2026

When one Claude window disconnected while Motet was unavailable, another window could later deliver that pending decision. The originating window did not learn that delivery had finished and kept overriding a newer reconnect with its old local opt-out. It now relinquishes that old decision once its exact pending entry is gone and the shared record contains a settled Motet decision, then follows the shared record and Motet's ordering. An undelivered entry, an unreadable entry, or an older Motet's unticketed result still preserves the opt-out. A newer command in the same window keeps its own decision. A disconnect made while the computer had no Motet sign-in Motet accepts (before setup, or after the stored grant was refused) had no pending entry, so the same override happened through it; every disconnect Motet has not committed now gets one, whatever the reason, and only an older Motet, which orders nothing, keeps an opt-out unticketed. A `/watch-setup` delivers the chat's pending disconnects with its new grant before the owner's next command, a window kept off by a stored disconnect says so in `/watch-status` even after its own reconnect, and a reconnect whose commit got no answer says it is unconfirmed instead of scheduled.

- Commits: 93b59c4, and the commit that keeps every uncommitted disconnect pending
- Kind: root cause
- Related: BR-047 (pending decisions delivered by another copy)
- Area: claude-delivery

### BR-047 · 27 Nov 2026

The owner's connect and disconnect decisions for a Claude chat could not be ordered by the plugin alone across plugin copies (an earlier copy still finishing a command after a reload) or across two Claude windows of one chat: the plugin's store cannot compare and set, and an earlier copy's request may already be on its way. Motet now orders them with two-phase tickets (`POST /claude-mod/decision`, with the chat's own credential or the shared grant). Each owner `/watch-connect`, `/watch-setup` and `/watch-disconnect` has a fixed identity (its plugin copy and its number there) and reserves a ticket as its first step, before it reads its chat, from one counter Motet keeps for every chat (`claude_mod_decision_counter`, started above every earlier per-chat ticket). A reservation is repeatable: Motet keeps each command's ticket for a day (`claude_mod_reservations`), so a lost answer and a retry return the original ticket (at most 4,096 are kept, oldest forgotten first). After any request that got no clear answer, the plugin only asks for the ticket Motet already holds (`recover`), so a reconnect or setup whose first request Motet never received, or whose ticket Motet has since forgotten, is ended with nothing changed rather than given a later place; a disconnect in that case still takes effect in the chat and is placed when Motet first hears of it, the safe way round for an opt-out. One copy's reservations go out one at a time in the order the owner typed the commands, directly rather than behind the plugin's other requests. A ticket is used only with the Motet that issued it. The command commits its decision for its chat under its ticket later; Motet applies a commit only if no decision with a newer ticket was committed first for that chat. Commands from two windows given within the same moment are ordered by when Motet receives their reservations; any sign-in stored on the computer can reserve, and only with none stored at all (so no opt-out can exist to misorder) does a command reserve after reading its chat. Motet keeps one row per chat (`claude_mod_decisions`: last committed ticket, opted out, opt-out ticket), written only inside its transactions, and a plugin copy can read it. An opt-out ends the chat's connections at once and fails the messages not yet claimed, saying nothing was submitted. While a chat is opted out, Motet refuses every new connection (`/connect`, automatic `/register`) and the polls of a connection made before the opt-out (`decision_superseded`), before any membership or lease exists. A pairing is committed by `/enroll` itself, only once Motet accepts its code, under the later of the command's ticket and the ticket its code took when the owner made it; a refused code changes nothing, and a pairing placed before the opt-out is refused. Whenever Motet commits one of a copy's decisions, on any path, the copy keeps its ticket and the Motet that issued it in memory and in the shared record, and when Motet refuses one because a newer decision exists, the copy drops its own and follows Motet. The plugin follows the newer ticket in its shared record; a copy with no decision of its own checks a stored opt-out against Motet's latest decision and follows Motet's when it is newer. It keeps an opt-out Motet has not committed (Motet unreachable, or no sign-in Motet accepts) under a store key of its own, one per command, that no other write replaces and that holds the chat off in every copy until it is delivered; whichever copy next reaches Motet tells Motet and removes only that key. It keeps each setup's grant under a key named by that grant, removed only for that grant, so no cleanup can delete a newer one. It sets aside a credential Motet refuses, and after another window's opt-out waits until `/watch-connect` or until Motet shows a reconnect, saying so. With an older Motet it keeps its own ordering, where a foreign opt-out now beats its reconnect.

- Commits: 10e8677, d5ca7b6, 9844b4d, d606400, 9bc5f0d, 41caa52, c09eeb7, f308e59, de2649e, b39bd14, 1294d9c, b793fdc, 2701c20, 1c76e7e, 4b1bd42, and the commit that keeps pending disconnects and grants under keys of their own
- Kind: root cause
- Related: BR-046 (the plugin-only ordering this replaces where Motet supports it)
- Area: claude-delivery

### BR-046 · 27 Nov 2026

A Claude chat stopped receiving after the plugin was reloaded in it, and stayed unreachable. Two causes. First, Claude Code refused to load plugin 0.6.4 at all ("hooks module did not load"), because it tested two optional Claude features with `typeof $.session.root` and `typeof $.session.info`; the plugin now calls them and keeps the same fallbacks. Second, a reload stops the plugin's heartbeat, and Claude does not always start a reloaded plugin again, which was the only place the heartbeat and the `/watch-*` commands were set up. Plugin 0.6.5 puts them back on the first event the reloaded plugin sees, with every startup check kept and nothing resubmitted. A start or resume still registering when the chat ends or is cleared installs nothing, an owner `/watch-connect` in flight then does not connect, and after an end only a real start arms a reloaded plugin again. The owner's `/watch-connect`, `/watch-setup` and `/watch-disconnect` now take effect in the order given, so an older one that finishes late cannot undo a later opt-out. A superseded owner connect sends nothing to Motet, and one overtaken in flight ends the connection Motet made. When a late setup answer leaves a revoked grant shared, the plugin finds the current grant another copy saved, as Motet decides, without another setup.

- Commits: 97b96d7, 82b7e5f, c2290ec, 6df9b38, 1319a44, f50d3ba, 719ee8f, d013326, ee76a75, cf6722f, 7fda9e9
- Kind: root cause
- Related: BR-044 (added the feature checks that the host refused), BR-025 and BR-014 (receipts and completions across reloads and reconnects, a different failure)
- Area: claude-delivery

### BR-045 · 26 Nov 2026

Test-only fixes: Claude fixture leases tied to runner speed, and folder tests that assumed `/` as the root on Windows.

- Commits: e536f6e, f3bdc15
- Kind: root cause
- Area: tests

### BR-044 · 26 Nov 2026

A Claude chat working in a subfolder of its project was shown as unavailable, because of the exact-folder checks (room veto and idle proof) added by the 5 Nov entry. Plugin 0.6.4 reports the working folder and Motet checks it fresh. Adds enrollment migration and a receipt-only mode. Older plugins keep the exact-folder rule.

- Commits: 0fa6255, 1147efd, 712f49b, 5b14057, ce481f9, ed1218b, 72026ed
- Kind: root cause
- Area: folder-matching

### BR-043 · 26 Nov 2026

Codex replies were withheld because compacted records of 7 to 15 MB exceeded the 1,000,000-byte limit. One shared 32 MiB limit now serves every reader, and a big record is read to its end. Idle Codex chats also get a truthful reason instead of a transport-fault message.

- Commits: 6ae5b26, 91e7612
- Kind: root cause
- Same cause as: BR-001, BR-015, BR-018, BR-032
- Related: BR-025 (compaction-format and upgrade recovery, a different failure)
- Area: codex-records

### BR-042 · 25 Nov 2026

Motet called agent and relay text "the owner's words". New attribution rules say who wrote each message, confirm a delivery word for word, and pick the first delivered copy by log order. Wrapper handling was again written separately.

- Commits: f02a81d, 3d5db62, 3322157, 0f1a115
- Kind: root cause
- Same cause as: BR-005, BR-021
- Area: reply-correlation

### BR-041 · 25 Nov 2026

Step 6c.5b: send files to Claude chats on Windows.

- Commits: 9e58738
- Kind: feature
- Area: windows

### BR-040 · 25 Nov 2026

Two five-second proof windows in the attachment request ledger could be refused because subtracting timestamps rounds above 5. They are now compared without subtracting. The same commit makes the attachment-record fence say why it refused: a busy lock is retryable, and a record from another folder gets the copied-records reason.

- Commits: 35bba2c
- Kind: root cause
- Area: attachments

### BR-039 · 24 Nov 2026

Test-only fixes: wait for the simulated peer's reply, and hold transport test steps by event.

- Commits: f458b92, d235745
- Kind: root cause
- Area: tests

### BR-038 · 24 Nov 2026

Step 6c.4: the attachment namespace fence and owner runtime on Windows. Copied namespace records are refused with their own reason.

- Commits: 9150ca2, 69e99bb
- Kind: feature
- Area: windows

### BR-037 · 24 Nov 2026

Step 6a: the bridge opens on Windows for Claude recipients. Folder walk and credential read go through Motet's Python. Docs state what works and what stays off.

- Commits: d6f908a, 6e7501b, a83eff2, 07aab72, 47ab0cd, 20f575c
- Kind: feature
- Area: windows

### BR-036 · 23 Nov 2026

Windows could not remove the database because a preflight connection was never closed. It is now closed in `finally`.

- Commits: ce73254
- Kind: root cause
- Area: windows

### BR-035 · 23 Nov 2026

Windows and Linux port, steps 5a and 5b: Claude messaging starts there, private storage is shared, Codex work is refused off the Mac, and the bridge is refused on Windows with one stated reason.

- Commits: c7b1eba, 77b4210, f797aae, b937469, 68f66ef, 6a973c5, a23141c, eaf604a
- Kind: feature
- Area: windows

### BR-034 · 20 Nov 2026

Test-only fixes: coordinator fixture reads, and recovery proofs that counted a legitimate receipt replay as a second submission.

- Commits: ef49174, d6ca073
- Kind: root cause
- Area: tests

### BR-033 · 20 Nov 2026

Held chats were only visible in a terminal. Settings now lists chats on hold, with a Clear button and a bell notice. Docs updated.

- Commits: 6fafa9e, 6dd4a53, 60ac5a0, 3a51133
- Kind: mitigation
- Area: recovery

### BR-032 · 20 Nov 2026

Verifies that a proven turn end settles a request end to end, including after an oversized Codex record and a restart. The reply stays withheld.

- Commits: 960afdd
- Kind: mitigation
- Same cause as: BR-001, BR-018
- Related: BR-025 (compaction-format and upgrade recovery, a different failure)
- Area: codex-records

### BR-031 · 19 Nov 2026

Quieter relay conversation in Workspace. Reply history is kept.

- Commits: 877a9f9, 7f9c619
- Kind: feature
- Area: reply-correlation

### BR-030 · 19 Nov 2026

Follow-ups: clear stale transcript warnings, report startup inbox timeouts, show deferred receipt counts and ages, clear transient receipt status on import.

- Commits: 2f23f97, 7116539, e2a4681, 77bf32e
- Kind: root cause
- Area: claude-delivery

### BR-029 · 19 Nov 2026

Stage three: `get_reply` shows progress, refusals name an id the clear tool accepts, and refusal text is short enough for older adapters.

- Commits: 1188657, 0bf5282, 19eab79, 6cbc935
- Kind: root cause
- Area: recovery

### BR-028 · 19 Nov 2026

Requests locked after 15 minutes on long work, because one age limit served dispatch, running work and the hold. Started work no longer expires by age. Owner clear still works for aged attempts. Replaces a manual workaround.

- Commits: cdefa15, e039633, ff82b25
- Kind: root cause
- Area: capacity

### BR-027 · 19 Nov 2026

Stage one of progress tracking: records verified progress. No change to dispatch.

- Commits: e71b2c8, 4d91193, 8cd0f57
- Kind: feature
- Area: capacity

### BR-026 · 19 Nov 2026

Review follow-ups to the previous entry. Untyped errors stranded a receipt for good. A brief transcript read failure flipped live requests to unknown. Both fixed before release.

- Commits: fb9c63a, e08f59c
- Kind: root cause
- Area: claude-delivery

### BR-025 · 19 Nov 2026

Fixes four lock causes: Claude submission receipts lost after a reload, a Codex upgrade check that stopped tracking at an unknown or compacted record type (not a size problem), folders not made canonical, and legacy marker-only answers refused. Adds an optional inbox for another provider.

- Commits: 08769f6
- Kind: root cause
- Related: BR-018 (compaction-format and upgrade recovery, not the 1,000,000-byte limit)
- Area: codex-records

### BR-024 · 19 Nov 2026

Restored replies said "time not recorded". The stored time is now exposed and the page uses it.

- Commits: f5b531d
- Kind: root cause
- Area: reply-correlation

### BR-023 · 19 Nov 2026

Test-only fixes: transport fixtures with timing guesses, split Unicode in peer fixtures, test reporter format and elapsed-time reads.

- Commits: 96e53d4, 3632b56, 90c6975, a0b140e
- Kind: root cause
- Area: tests

### BR-022 · 18 Nov 2026

Released 0.1.0 refused messages with "could not verify capacity" through a second writer. Admission now does one bounded rebuild before its transaction. This fixes the cause, not one writer.

- Commits: 8db096e
- Kind: root cause
- Same cause as: BR-019
- Area: capacity

### BR-021 · 18 Nov 2026

The conversation page showed the full plugin wrapper because its matcher expected the envelope at the very start. The page now recognises the wrapper.

- Commits: fb9b2a7
- Kind: mitigation
- Same cause as: BR-005
- Area: reply-correlation

### BR-020 · 18 Nov 2026

Messaging and files broke after an old-SQLite start or a Python upgrade. Ordinary messaging now continues; files are refused with a plain reason.

- Commits: 50d1970, 295f8de
- Kind: root cause
- Area: capacity

### BR-019 · 17 Nov 2026

Plain-text sends were refused while peer history was retained. Peer reconciliation during a send made the capacity check stale. That one writer is removed. Adds the "could not verify capacity" refusal. Fixed at the root by BR-022.

- Commits: 8c7d1fa
- Kind: mitigation
- Area: capacity

### BR-018 · 15 Nov 2026

A Codex chat was locked as "requires recovery" after an oversized log record. Two repairs: an exact turn end now settles the request, with the reply withheld; and the owner can clear a request through the running server (`scripts/watch_bridge_clear.py`, see `docs/bridge/RECOVERY.md`). The size limit itself was not changed. Published inside the release commit.

- Commits: 758f647
- Kind: mitigation
- Same cause as: BR-001
- Area: codex-records

### BR-017 · 12 Nov 2026

Retires the automatic reply-ready notices from the 5 Nov entry. Agents now check `get_reply` at task checkpoints. The bridge no longer puts text into chats by itself. Published inside the release commit; the original commit is not in this history, and the reason for the reversal is not recorded in git.

- Commits: 758f647
- Kind: feature
- Area: reply-return

### BR-016 · 12 Nov 2026

Long messages were refused unclearly. Messages may now be 16,000 characters, with plain size errors.

- Commits: e96f2f7
- Kind: root cause

### BR-015 · 12 Nov 2026

Old uncertain attempts blocked chats for good. Adds an offline recovery script. Its reader uses a 32 MiB record limit, so it differed at that time from the 1,000,000-byte limit in the live reader.

- Commits: d46771f
- Kind: mitigation
- Area: recovery

### BR-014 · 12 Nov 2026

Completions were lost across Claude reconnects and Codex log rollovers. Two repairs: the Claude plugin keeps completion evidence across reconnects, and the Codex reader (`tracking_source()` in `src/codex_transcript.py`) follows a verified continuation. Also adds the `capacity_error` hold that keeps a chat with an uncertain request from receiving more.

- Commits: 70e6378
- Kind: root cause
- Area: claude-delivery

### BR-013 · 12 Nov 2026

Older Python (3.9) and Windows compatibility: an empty query no longer breaks the Antigravity broker, offline recovery refuses non-POSIX systems cleanly, and the Antigravity metadata refresh no longer assumes an earlier read, so its throttle runs only after a first metadata read.

- Commits: a19b590
- Kind: root cause
- Area: windows

### BR-012 · 12 Nov 2026

Test-only fixes: tests that relied on wall-clock load or scheduler turns. No product change. Grouped; dated by the last commit (first on 31 Oct).

- Commits: 1e7dc6d, 42a0808, dc9db56, e4943bd
- Kind: root cause
- Area: tests

### BR-011 · 8 Nov 2026

Unresolved notices can be dismissed. Adds native messaging guidance.

- Commits: d98ca2d
- Kind: feature
- Area: recovery

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

Idle Claude chats could not receive, because receivers had to be in the room. Receiving now works from verified metadata. Side effect: it adds a room veto and a canonical-project check for idle chats, both exact-folder, which later hid chats working in a subfolder.

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
- Area: claude-delivery

### BR-005 · 1 Nov 2026

Claude Code wraps plugin input in its own attribution text, so the request was not matched. The plugin now accepts the raw envelope or that exact wrapper. Only the plugin copy of the matching logic was fixed.

- Commits: 5b2ec75
- Kind: mitigation
- Area: reply-correlation

### BR-004 · 1 Nov 2026

Adds optional owner messaging to existing Claude chats through a plugin.

- Commits: e14d0ce, 9115bde
- Kind: feature
- Area: claude-delivery

### BR-003 · 31 Oct 2026

Reports a peer reply that is blocked because owner tracking stopped. Shows unresolved queue outcomes apart from failures. Reports capabilities per chat and checks transcript eligibility safely.

- Commits: 2a5aa0f, 9871f94, a925574, d86cf62
- Kind: mitigation
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
