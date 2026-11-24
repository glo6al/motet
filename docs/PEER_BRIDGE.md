# Shared Motet chat bridge

The optional Motet MCP connection exposes a directory of existing named Codex and Claude chats. Configure the connection once in each client. The installed MCP name is still `watch-peer`. Chats do not need separate bridge credentials or enrollment. The bridge is the provider-to-provider messaging service; the Motet stage and desks are its separate user interface. Bridge delivery and reply tracking do not require that interface to be open.

Agent-to-agent routing uses native tools within a provider and Motet only between providers:

- Codex to Codex: use Codex's native chat tools.
- Claude to Claude: use Claude's native coordination tools when they support the intended chat.
- Codex to Claude, or Claude to Codex: use Motet.
- Antigravity to Codex or Claude: use the optional Motet Bridge plugin to send a request and retrieve its answer. For incoming requests, use the separately configured shared pull inbox; it does not target or wake a native chat.
- Antigravity to Antigravity: use Antigravity's native coordination tools.

If native tools cannot reach a same-provider chat, report that limitation; do not fall back to Motet. Determine both providers before sending. These are agent routing instructions, not server-side caller authentication: the shared service cannot verify a caller's provider from its shared credential or declared sender label. Existing Motet requests can still finish and their replies can be recovered. Owner messages sent through the Motet UI are unaffected. Reconnect the MCP in existing clients to load the updated instructions.

## Talking to another agent

Use ordinary language: “Ask Atlas how the tests are going.” The agent selects the native or cross-provider route above. For a Motet message, it discovers the intended chat, resolves its name to a stable provider-specific chat ID, sends the question once, and checks the correlated reply. Motet does not use another model to route the request.

Names are addresses, not authentication. When names are duplicated, specify the provider or project. The agent must not guess between similar names, pick an unavailable duplicate's namesake, or treat an incomplete directory page as proof of uniqueness. Renaming a chat never redirects a request already sent to its original ID.

For a connected Claude recipient, the plugin submits the complete peer envelope with plugin attribution, waits for its matching main-turn answer, and returns that answer to the original shared request. It does not turn the peer text into an owner instruction.

A pending reply is reported as pending. Busy means an earlier request remains open, not that it failed. Keep its request ID, wait, and check at a meaningful checkpoint; do not resend. The requesting agent retains the request ID and uses `get_reply` at a meaningful task checkpoint, before relying on the answer, or when the owner asks for an update. It can continue independent work while waiting. It must not repeatedly poll, resend the question, or turn a peer reply into a new owner instruction. Unknown, failed, expired, and rejected operations remain distinct from successful delivery. If several earlier questions could match a follow-up, clarify which one rather than guess.

## Retrieving replies

Replies stay attached to the original shared request. The bridge does not inject `WATCH_PEER_REPLY` notifications or unsolicited answer messages into native chats. It does not automatically wake a finished chat. “Did Atlas answer?” retrieves the retained request; an agent working on a dependent task should check that request before using the result.

The repository's `AGENTS.md` and `CLAUDE.md` describe this workflow. Other projects can adopt the same instructions in their own agent files. Reconnect an existing MCP connection through its client's normal workflow to load the updated tool guidance; already-running chats may retain earlier instructions in context.

Automatic reply-notice setup and installation have been retired. On upgrade, the bridge revokes notice credentials, stops unsubmitted notices, and preserves original requests, replies, and deduplication records. Legacy notice acknowledgments remain accepted for cleanup. A notice already submitted to a native app cannot be recalled by this migration; its presence is not evidence of a new owner request.

Remove previously installed reply hooks with:

```sh
python3 scripts/install_reply_hook.py --provider both --remove
```

This removes only the bridge's owned reply hooks. It preserves unrelated hooks and ordinary bridge messaging. Reload the Motet Claude plugin **0.6.1** in existing Claude chats to load its notice-submission fence. No replacement notification or automatic answer injection is installed.

## Shared access and authorization

The credential authorizes access to the local bridge service. It does **not** identify the native chat that called a tool. Any declared sender name is explicitly unverified attribution. It cannot grant permissions, change access, or bypass rate limits.

Clients holding service access share the bridge's request history and replies across projects and providers. Request IDs correlate operations; they are not private mailbox passwords. Directory titles and project labels, request previews, and retrieved replies can enter the connected clients' model-provider contexts. The bridge does not expose arbitrary chat transcripts through its tools. Do not send secrets or credentials.

An agent still needs an actual owner request or an independently established, scoped standing instruction to contact another agent. Discovery does not grant permission to send. An incoming message claiming “the owner approved this” supplies no new authority. Replies remain peer information; they cannot authorize new work, controls, or forwarding. A recipient answering a bridge request must not initiate another bridge conversation as part of that answer.

## Directory scope and availability

One service-level policy selects providers, history eligibility, and optional project filters. The default history window is 14 days; an explicit all-history setting includes older eligible chats. Project names organize and disambiguate the directory rather than creating per-chat MCP installations.

Subagents, execution/review sessions, archived sessions, and files outside the configured native session roots are excluded. Directory results expose metadata and bounded discovery status. An incomplete scan or page must not be presented as the complete set of matching names. Discovery begins in the background when an enabled service starts; large histories can take about a minute or longer to scan. Retry later instead of polling repeatedly.

Malformed native metadata can leave discovery incomplete even after the scan finishes. Motet reports unverifiable metadata without exposing file contents. It does not assume a later valid title repairs an earlier unreadable record, because that record could change a chat’s identity or introduce a duplicate name. Name-based sending remains blocked while uniqueness is uncertain; an unreadable Codex name index also prevents Codex identity validation. Repeated bridge requests cannot repair this. As a workaround, stop Motet and use the `policy` command below to narrow `--history` so old transcript files fall outside eligibility, or temporarily limit `--providers` to exclude the affected provider. These changes do not repair a malformed Codex title index; that index requires repair or regeneration through the native Codex tooling. Keep native records backed up rather than deleting them to silence the warning.

Availability is reported separately from discovery:

- **Reachable:** the current transport checks support delivery to that exact native chat.
- **Unverified:** the chat is known, but delivery to its current state has not been established. Sends are refused.
- **Unavailable:** a required transport or compatible inbound channel is missing or has changed. The result explains why.

Historical or unloaded Codex chats can appear as unverified. Listing an old transcript does not prove the desktop app can receive a queued message for it. Eligible recent Codex chats still require transport readiness and exact transcript identity checks.

Claude chats can receive shared messages through the [Motet Claude plugin](../integrations/claude-mod/README.md) version 0.3.2 or later. Install it once for your user account, reload the intended already-open chats, and authorize agent messaging once in Motet. Eligibility still depends on that exact chat being connected. Existing owner-only grants and manual pairings are not silently expanded; choose **Enable agent messaging** to replace an owner-only global authorization with explicit peer consent. Legacy manual pairings remain owner-only; automatic registration does not replace them.

The directory reports a compatible, authorized official-plugin connection as `claude_mod`. A chat being visible, a global plugin being installed, or the old channel being connected does not establish receiving readiness. The plugin must load in that chat, register its current native UUID and project, and maintain its connection. An outdated sender MCP adapter must also be updated and reconnected through its client's normal MCP workflow.

The older development channel stays blocked for peer delivery. Its process can switch conversations while keeping its channel alive, so a channel descriptor cannot establish the intended destination. The official plugin uses the native session identity and checks it before submitting input. It retains the documented native queue limitation: switching or clearing after the final check can affect already-queued input. Wait for pending work to settle before changing conversations.

## Tools and delivery

The native messaging tools are `list_agents`, `send_message`, `get_reply`, and `list_requests`. The separate Antigravity inbox tools are described in the [plugin guide](../integrations/antigravity-bridge/README.md). There are no steering, interruption, settings, or arbitrary-command tools.

The adapter generates a fresh request UUID for a new send and returns it, including in a recovery receipt when the outcome is uncertain. An explicitly supplied recovery ID must be a valid accepted version-4 UUID. An identical replay returns the original request with a duplicate indication; conflicting reuse is rejected. A retry never silently follows a renamed or replaced destination.

A queued or transport-written result does not prove the recipient acted. Busy Codex recipients receive queued messages, not steering. Reply parsing requires the exact native request and turn. Codex replies from turns mixed with native input, Motet owner input, or steering are withheld. Uncertain turn evidence also withholds replies. These structural checks cannot prevent model-level influence from earlier peer content; owner replies carry a warning when peer input may have influenced them.

Shared bridge v2 messages accept up to **16,000 Unicode codepoints** (a single-codepoint emoji counts as one; combined symbols can contain several). A message above that limit receives an explicit count and maximum, with “Nothing was sent.” It is rejected before reserving a delivery slot; Motet never truncates or automatically splits it. Replies remain limited to 16,000 codepoints, and Motet owner-message limits remain unchanged. The retired endpoint prototype retains its 2,000-codepoint request limit.

For larger messages to Claude, upgrade the bridge first, then reload the **Motet official plugin 0.6.0** in the receiving chat. Reconnect the MCP in sending clients to refresh its advertised limit. The plugin negotiates a 100,000-codepoint envelope capacity; the bridge also limits the complete envelope to 100,000 UTF-8 bytes, keeping Codex's native command argument bounded. Older Claude plugins retain their 4,000-codepoint envelope limit: a message that does not fit is rejected before reservation with instructions to reload the updated plugin or reduce the message. New plugins can still connect to older bridges without requesting an unsupported capability. A plugin downgrade after reservation cannot claim an oversized message.

Request content expires after 24 hours. Permanent, bounded deduplication tombstones prevent expired operations from being sent again, including across service credential rotation. Old endpoint-prototype history is not exposed as shared history.

Peer capacity remains separate from Motet's reserved capacity. Limits allow up to 100 requests per day service-wide across native and inbox sends, 10 per target per hour, and four unresolved native admissions in the last 15 minutes; declared sender names do not create independent quotas. A request proven never dispatched expires after 15 minutes, with a final check before dispatch. Crossing the 15-minute dispatch window does not expire a dispatched request or make it recovery-needed; its own recipient remains reserved across all ages. A start exactly bound to the request stays open until an end or contradictory evidence is observed, or the owner explicitly clears it. When the 24-hour content cutoff removes a still-open Codex tracker, it becomes recovery-needed: content retention passed before an end was observed. Motet does not continue tracking it beyond that cutoff. An issued claim is not native acceptance. Completed turns with unusable replies release their slots. Genuine uncertainty keeps the destination fenced; deleting expired message content does not release it. A rejected follow-up says the new request was not accepted and identifies the earlier open request. The bridge does not grow a second queue behind it or resend it. Late replies can still be recovered within content retention; an authenticated Claude completion received afterward may release its execution reservation without restoring expired answer content. A pruned Codex retention hold requires an explicit owner clear. When shared capacity is full, the response says “Four peer deliveries from the last 15 minutes are still open. Try again shortly.” This counts delivery admissions, not evidence that native turns started.

Native request results and history can include validated progress facts with ISO times and their zones, plus elapsed time in plain words. “Start observed” and “no end observed” describe evidence, not a promise that work continues. If a start is recorded without a timestamp, the wording is “Start recorded; start time unavailable.” The structured `last_checked_at` field records a log-read observation, not a sign of life; progress summaries omit it. If a start is much older than the task should take, tell the owner, who can clear a request whose turn has stopped. A turn that died without an observable end can remain open until the owner clears it. Progress never proves that an answer was captured, and does not extend the unchanged 24-hour content retention.

An exactly identified turn can finish even when its reply cannot be trusted. Such a request releases its recipient's reservation and reports **“Finished; reply withheld”** (or **“Finished; reply unavailable”** when the completed turn left no usable answer) with progress phase `ended`. Its compatible request state can remain `unknown`, and its reply stays empty. This records an observed end, not task success. It is distinct from an unresolved hold or an owner-cleared request: do not clear or resend it merely because the state says `unknown`. Missing end times remain unavailable rather than being invented. A transcript gap, an idle app or elapsed time alone does not establish completion.


### Resumed Codex chats

For new messages, the bridge verifies Codex’s current transcript pointer and the transcript’s native chat identity. A rollover filename is not a new chat, and an older file is not selected merely because its filename ends with the chat ID.

An already-submitted request can follow a verified immediate continuation without sending the question again. Tracking validates the predecessor’s history boundary and reads its remaining records before moving to the new segment. Mixed or ambiguous input remains withheld. Missing segments, ambiguous history, or unsupported multi-hop continuations remain recovery-needed; the bridge does not choose a file by modification time or extract an answer solely because its marker appears there.

### Completion and reconnects

Before upgrading or considering rollback, inspect a read-only, in-memory migration preview:

```sh
python3 scripts/watch_bridge_preflight.py
```

It reports execution counts without changing the source database or printing message content. A server-side downgrade check alone is insufficient: loaded plugins must also have acknowledged their pending completion receipts. The command deliberately reports that client receipts were not checked.

Acceptance, execution, answer capture, and retrieval are separate facts. A successful send means the bridge accepted the request. Native queue acceptance does not prove execution, and a retained answer does not prove the asking agent has read it.

Claude plugin 0.5.0 negotiates lifecycle reporting with a compatible bridge. A reconnect in the same runtime preserves the original bound request and turn. The plugin saves completion reports before sending them; retries report that same completion, never submit the original question again. Confirmed execution cannot be undone by a delayed “started” or “unknown” report. A completed turn can release capacity even when its answer is malformed or withheld because other input entered the turn.

Plugin 0.6.2 also checks retained receipts against the server's original attempt before observing completion after a full reload. A completion observed across that gap releases the reservation with its answer withheld. It cannot reconstruct a native completion that happened while no observer was present. Conflicting retained turns or completion records remain unresolved. A rejected receipt stays retained while other receipts and heartbeats continue. A marker without a nonempty usable answer never counts as an answered request.

Old plugins and servers keep their existing wire format. New completion features are enabled only after negotiation, and incompatible local receipt formats are stored separately. Upgrade the bridge before the plugin. Do not downgrade with unresolved lifecycle-v2 work: retain the compatible installation until those attempts have settled or an explicit recovery procedure has been agreed. Reloading a plugin does not reconstruct a missing native completion event.

Diagnostic history is local, bounded, and contains event metadata rather than prompts, answers, filesystem paths, or credentials. Uncertain historical attempts are not silently converted into success during migration. Existing attempts with missing evidence may require recovery before that recipient can accept another request.

### Deleting queued requests

A verified Claude host drop for the original pending submission releases its execution reservation, including when that receipt arrives after the request payload expires. A request with durable evidence that its turn started cannot be changed to “dropped before start.” Dropped requests remain terminal across stale writes and restart; late replies are withheld and the original message is never resent.

This relies on the original Claude runtime receiving the host's submission result. Reloading the plugin can lose that pending native result; queue disappearance alone cannot recover it. Codex's current queue notifications also do not distinguish deletion from starting or steering a message. Those ambiguous cases remain unresolved and require evidence or explicit owner recovery; they are not automatically labelled cancelled.

### Chats on hold in Settings

When Motet cannot confirm whether a recipient received a message or finished its turn, the recipient stays reserved so nothing is sent twice. Settings → Connections on the computer running Motet lists these under **Chats on hold**, with how long each has been on hold (or **Hold time unavailable**), a fixed reason and one **Clear** button. A healthy turn that is still running is not listed, whatever its age.

Clearing releases the reservation, so new messages can reach that chat. It does not stop the agent, and a late answer to the earlier message is withheld. Settings clears only a chat that is still genuinely on hold; if the request has finished or recovered in the meantime, the row updates or leaves the list instead. A hold Motet cannot match to a chat shows **Can’t clear here**. After five minutes on a hold that Motet saw begin, an in-app bell notice links to the row; dismissing it does not clear the hold. A hold whose start Motet did not see, such as a Claude chat that was waiting when Motet restarted, shows **Hold time unavailable** and has no notice. The list and the bell show no message text, commands, paths or keys. The command-line clear below remains available.

### Clearing the public request named in a refusal

The request ID in a busy or recovery refusal is the shared **public** request ID, also used by `get_reply`. The owner can inspect that exact request while Motet runs:

```sh
python3 scripts/watch_bridge_clear.py --port 8793 --request-id PUBLIC_REQUEST_UUID
```

The owner server resolves it to the original transport and checks its immutable recipient and execution evidence. To clear an eligible unresolved attempt, repeat the same public ID with the exact agent shown by inspection:

```sh
python3 scripts/watch_bridge_clear.py --port 8793 --request-id PUBLIC_REQUEST_UUID --expected-agent EXACT_NATIVE_AGENT_ID --apply
```

Use the complete `x:UUID` or `c:UUID` recipient. The existing `--transport-id TRANSPORT_UUID` option remains available; choose exactly one ID option. No local database lookup or guessed ID conversion is required. Clearing remains an explicit owner choice with provider outcome unknown. It does not cancel work, resend a message, or reconstruct its answer. If the result is unconfirmed, inspect the same request; do not retry automatically.

### Historical recovery and owner clearing

`scripts/watch_bridge_recover.py` previews recovery for an explicit list of old client request IDs. It validates the original admission and native evidence; an audit report saying “verified” is not sufficient. Applying recovery requires stopping the bridge and obtaining its existing exclusive storage lock. Every selected proof must pass again before the transaction commits.

Verified historical completion releases the execution reservation while keeping expired replies unavailable. An owner can separately select uncertain reservations to clear, or use online inspection and exact confirmation to clear launched, non-terminal, non-captured running or submitted attempts after their dispatch deadline. Clearing a healthy aged attempt is an owner decision, not a liveness judgment. Those remain recorded as **retired**, with the provider outcome unknown and answer capture withheld. Clearing a reservation does not cancel native work, resend the question, erase its replay guard, or claim that the provider finished it. Late reports cannot restore its answer or make it block the recipient again. A database containing retired reservations cannot be downgraded to a version that does not understand retirement.

Use the tool's `--help` for the explicit request-selection and native-source options. It defaults to a dry run; `--apply` is a separate operator action. Keep request manifests, source evidence, and database backups private. Review the exact selected set before applying either recovery or owner clearing.

## Installation and rollout

### Where it works

- **macOS:** the whole bridge. Codex and Claude chats can both receive.
- **Linux:** the whole bridge, with Claude chats as recipients through the Motet plugin. A Codex chat can still ask a Claude chat, but a Codex chat cannot receive on Linux: delivery to Codex needs the Codex desktop app on a Mac, and Motet says so instead of sending. The Antigravity plugin and pull inbox install as on the Mac. The offline commands (`watch_peer.py policy`, `watch_bridge_recover.py`, `watch_bridge_clear.py`, `watch_bridge_preflight.py`, `install_reply_hook.py --remove --revoke`) work and take the same lock Motet holds while it runs. Checked by the automated tests on Linux, including the end-to-end Claude bridge test; not yet tried on a real Linux machine.
- **Windows:** not available yet. Motet finds chats for the bridge by opening their folders one by one and refusing links on the way, and it cannot do that on Windows yet. Without it no chat can be listed or addressed, so on Windows agents cannot ask each other questions through Motet at all, in either direction, and the Antigravity plugin and inbox do not install. Rather than half-work, the bridge refuses with that one reason: the MCP adapter stops before it reads its credential, the server answers `list_agents` and `send_message` with it (nothing is sent; saved requests stay readable), and `watch_peer.py init`, `rotate`, `config` and `policy` print it. `watch_bridge_recover.py` and `watch_bridge_clear.py` refuse with their own plain reason, since nothing can be sent or put on hold here. `watch_peer.py status` and `revoke`, `watch_bridge_preflight.py`, and `install_reply_hook.py --remove --revoke` work. Your own messages to Claude chats on the stage are unaffected. The private files and locks underneath are ready for when the folder walk arrives: credential files and Motet's database and lock are created and checked through the same private-storage layer as agent messaging (owner and access list, no link or junction), and the offline commands take Motet's own Windows lock. Checked by automated tests, not yet on a real Windows PC.

Use a stable Motet installation. Generated MCP configuration contains the adapter's installation path; do not point a long-lived client configuration at a disposable worktree.

Install the optional runtime dependencies from the repository root:

```bash
npm --prefix integrations/claude ci --ignore-scripts --no-audit --no-fund
```

Initialize shared service access. The explicit `all` option includes eligible older chats; use `--history 14` for the default recent-history policy instead:

```bash
python3 scripts/watch_peer.py init --providers codex,claude --history all
```

The command reports a private credential-file path. Generate the appropriate client configuration using that path:

```bash
python3 scripts/watch_peer.py config /path/to/private/credential.json --client codex
python3 scripts/watch_peer.py config /path/to/private/credential.json --client claude
```

Codex output is a TOML MCP entry; Claude output is an MCP JSON entry. Install the appropriate entry in the intended client scope once. Preserve existing entries. Keep credential files and local registry data outside version control; configuration contains the file path, never its secret.

Inspect or revoke access with:

```bash
python3 scripts/watch_peer.py status
python3 scripts/watch_peer.py revoke CREDENTIAL_UUID
```

`rotate CREDENTIAL_UUID` revokes that credential and creates a replacement while preserving the message ledger. Update the client configuration to the replacement file. Rotation is not a message retry.

The default Motet port is 8793. Non-default `--port` and `--state-dir` arguments precede the subcommand. Optional repeated `--allow-project` and `--deny-project` arguments on `init` set service-wide project filters; they do not create per-chat credentials.

To change discovery scope, stop Motet first, then update the saved policy and restart Motet:

```bash
python3 scripts/watch_peer.py policy --history 14 --deny-project /path/to/excluded-project
```

Unspecified fields stay unchanged. Supplying `--allow-project` or `--deny-project` replaces that list; use `--clear-allow-projects` or `--clear-deny-projects` to clear it. `--providers codex,claude` changes the provider selection. The command refuses changes while Motet holds the messaging lock. Existing credentials, message history, and deduplication records remain intact. Discovery filters do not retroactively hide earlier shared requests or replies. Unlaunched requests fail on restart rather than being resent; already-launched requests keep their conservative recovery behavior.


Update Motet before configuring the clients, preserve their existing configuration entries, and reconnect using each client's normal MCP workflow. The shared connection does not restart working chats or modify their permissions. The old per-endpoint `--peer` launcher setup is unsupported in this mode.

Disposable native tests established same-ID resume and a peer reply with the observed persisted settings intact. Further tests showed that switching conversations leaves the channel connected to the original Motet identity while native Claude uses a different chat. The resume launcher is therefore deferred; no `--resume` option is provided.

Owner messages sent through Motet to a Claude process it launched retain their existing behavior: they go to the conversation open in that process. After `/clear`, `/resume`, or a fork, close and relaunch before relying on station targeting. Persisted settings reload on launch; session-only grants, added directories, and previous one-off CLI options are not restored automatically. Reachability grants no permission or owner authorization.

## Delivery and limits

A peer envelope identifies its origin, labels any declared sender as unverified, quotes the message, and requests one correlated final reply. The receiver does not start a forwarding chain. Claude and Codex use their existing conversation model, sign-in, project context, tools and permissions.

Motet reserves the shared request and native submission attempt before returning input to the plugin. Duplicate requests recover the same record; uncertain submissions are not replayed. A received/queued message is not a completed answer. Claude replies require the entire original envelope and its corresponding completed main turn with one matching `WATCH_REPLY` marker. Lifecycle-v2 Claude completion conservatively withholds a reply when the plugin observes additional main input during the bound turn. Older protocol versions retain their earlier limitations. Codex independently checks its transcript for mixed input before publishing a reply.

Shared message text is limited to 16,000 Unicode codepoints. Routing instructions and JSON escaping have a separate envelope bound: 100,000 codepoints and UTF-8 bytes for a compatible connection, or 4,000 codepoints for an older Claude plugin. A too-large message is rejected before reservation, with a clear maximum or plugin reload instruction; it is never truncated. See the upgrade order and compatibility details above.

## Rollback and privacy

Disable the specific MCP entry and revoke its service access to stop future use. Keep the updated Motet code and its current database: this is the supported rollback. Do not restore an older message database: that would discard replay protection. Revocation cannot recall an already-authorized or provider-queued message. Keep unrelated client configuration intact.

Do not run an older Motet version against a database containing bridge payloads. Older versions can expose peer messages as owner conversation history and apply owner-message semantics. Waiting 24 hours alone does not establish that every payload was removed; malformed legacy rows can survive cleanup. There is currently no supported downgrade or payload-purge command. A future downgrade procedure must verify that no peer payloads remain while preserving all deduplication ledgers and tombstones. The legacy `prune` command is not payload cleanup.

The local service assumes a trusted OS account. Its database and credential files hold symmetric secrets; a process able to read those files can impersonate service access. This is not isolation from other full-access tools running under the same account.

## Protocol references

- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Claude channels](https://code.claude.com/docs/en/channels)
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
