# Motet, the assistant

How Motet is instructed, what evidence it answers from, what its questions cost, and its optional memory. Part of the [Motet README](../README.md).

## Motet instructions and skills

Motet's voice and working methods live in [`motet/INSTRUCTIONS.md`](../motet/INSTRUCTIONS.md), [`motet/VOCABULARY.md`](../motet/VOCABULARY.md), and five cards it loads from [`motet/skills/`](../motet/skills/): shift reports, stuck-agent checks, conflict checks, handovers, and Watch-rule drafts. The folder also holds thirteen skill templates for the agent core, listed below. Ask normally; Motet selects a card when useful. Each question snapshots the instructions and available cards, so an edit applies to the next question without changing one already running.

Only a short skill menu is sent initially. The read-only `load_skill` tool returns a selected card; it adds no stage actions or authority. Loading a card uses the existing request allowance and may add an API round. Tools, permission boundaries, redaction, dispatch, and request limits remain in Python. Missing or invalid core files use the complete built-in instructions; an unavailable card is omitted. A new skill file alone cannot add a tool or enable messaging.

### Templates for Motet's self

The `motet/` folder also holds the agreed templates for Motet's self, the files a future agent core will read in place of today's instructions. They are templates only: Motet does not read them yet, and nothing in Motet writes to `motet/`. `motet/INSTRUCTIONS.md` and `motet/VOCABULARY.md` stay the live instructions until the agent core switches over.

- `motet/SOUL.md`: Motet's persona, the values and habits it tries to hold to.
- `motet/RULES.md`: what Motet may do on its own, what it must ask the owner first, and what needs the owner's own yes.
- `motet/IDENTITY.md`: who Motet is, its job, each cycle, and how it knows things.
- `motet/VOCABULARY.next.md`: the agreed next version of `motet/VOCABULARY.md`. It replaces that file when the agent core switches over.
- `motet/ASKING_MOTET.md`: the short block every project's agent instruction files will carry about asking Motet for help.
- Thirteen new skills in `motet/skills/`, one for each point where work can stall: `morning-brief.md`, `project-sweep.md`, `end-of-day.md`, `kick-off.md`, `next-step.md`, `review-loop.md`, `ready-for-yes.md`, `close-out.md`, `unstick-an-agent.md`, `something-went-wrong.md`, `helping-an-agent.md`, `waiting-on-you.md` and `wake-and-deliver.md`. Only the five cards above are loaded today.

The templates say "the owner" and use fictional agents such as Ada, Bram and Cleo. An owner's own copies, which may use their name, live outside the repository, in `~/.live-room/motet-self/`.

### The self folder and its history

`scripts/motet_self.py` is the owner's command for that folder. Motet does not read it yet, and nothing in Motet's server, tools or engines can write it.

    python scripts/motet_self.py init --from FOLDER    create the folder, private, from your prepared copies (each becomes version 1); refuses an existing folder
    python scripts/motet_self.py scan                  record every file you edited directly since its last version
    python scripts/motet_self.py history [FILE]        list the versions of one file, or of every file
    python scripts/motet_self.py show FILE VERSION     print one version
    python scripts/motet_self.py restore FILE VERSION  make an earlier version current again, recorded as "restored from vN"

`init --from` takes your prepared copies of the self files, never the repository's `motet/` templates: a folder that holds `INSTRUCTIONS.md` or `VOCABULARY.next.md` is refused.

The folder, its files and its history are private from the start, with the same owner and permission checks as the rest of `~/.live-room` (the access list on Windows); anything that is not private is refused, never repaired. Every saved version is kept in `motet-self/.history/<file>/v000001.json` and so on, one record per version with who changed it, when and why. Each record holds the sha256 of the record before it, and `latest.json` beside them names the newest record, so a record that goes missing or is changed by hand is noticed: every command then refuses rather than guess. Records are never changed or removed. Each save is all or nothing: the new record is staged first, the file is replaced by rename, and a save that stopped partway is settled at the next change, so an interrupted save leaves the previous version in place. Server code that lists history later will use `src/motet_self_history.py`, which only reads; the writer, `src/motet_self.py`, is used by the owner's command alone.

If a command refuses because a file's history does not match its latest-version marker, nothing was changed. To recover, restore that file's `.history/<file>/` folder from a backup (for example Time Machine), or ask for help. Never edit the records by hand. If a command says the pending record (`.history/.pending.json`) is damaged, a link or a folder, move it aside by hand and run the command again.

Motet's model policy and price list are settings, not part of its self: they live in `config/MODEL_POLICY.md` and `config/PRICING.json`. Motet reads them but never writes them.

The golden questions in `tests/fixtures/officer/golden_questions.json` use the existing fictional demo fleet. Run `python tests/test_officer_evals.py` for offline evidence checks. These do not prove model behavior, and the browser's demo answers remain scripted. The optional `scripts/eval_officer.py` runner lists cases without making requests; its explicit live mode uses the configured Motet API key and bills that API account. It tests fictional evidence only, without changing the saved model or sending agent messages.

## Motet evidence and follow-ups

Motet has shared read capabilities for discovering live and retained chats, searching or browsing a chosen period, counting matching saved observations, selecting an overview across chats and time, and expanding nearby discussion or linked operation results. These are reusable tools, rather than a fixed list of supported questions. No new service, embeddings, background model summaries or historical import is required.

Saved-event totals cover the full matching retained population when the source can establish it. Selected excerpts are identified separately. Live and retained counts stay separate unless provenance establishes that observations are the same. A successful operation is not proof of a completed goal, and recording gaps do not prove inactivity. Typed count fragments bind their source, unit, scope, period and completeness; reference checks do not certify the meaning of the surrounding model-written explanation.

After shared evidence reads, final answers use [Anthropic's structured output format](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) or the equivalent answer tool. Python then checks the supplied references and exact typed assertions before rendering the answer. Calendar dates and recent intervals are normalized locally, including daylight-saving boundaries; the model does not have to calculate Unix timestamps.

Run `python scripts/eval_officer_capabilities.py` to list the fictional broad-summary, count and follow-up cases without reading a key or making a model call. Its explicit live mode records private reports for human review. The [capability design and verification](MOTET_CAPABILITIES.md) describe the tests and their limits.

Source references accompany the current day's Motet conversation in this browser. The server signs the answer and its evidence metadata, then revalidates sources before later use. Edited, unsigned, expired or erased assistant text is excluded from future model replay; owner-written questions remain. Sources open in the existing memory viewer. Restarting Motet invalidates the old process's receipts, so Motet must query evidence again. Optional durable Motet history now retains sanitized questions and answers under the same memory controls; see [the backend contract](MOTET_HISTORY.md). Process-bound receipts are never restored as action authority.

Answers stay readable when observations change or source verification is temporarily unavailable, with a visible explanation of the uncertainty. Opening Memory or a source does not invalidate an answer. When saved input was actually deleted or expired, Motet replaces the affected model-written summary with freshly checked, attributed excerpts from surviving records; it omits earlier derived totals rather than guessing. This does not make another paid model call. Source links check the requested record independently.

Publication and deletion are ordered at a short server-side gate. A deletion committed first requires preparation from surviving records; a publication committed first is already published. The browser checks for later committed erasure independently of drawer navigation. Text already published may arrive later over the network; Motet cannot make browser display atomic with deletion or retract data already sent to a model.

Each request has one allowance covering intent checking and all answer rounds: at most seven model attempts, two minutes, 512 KiB of cumulative request input, 12,288 reserved output tokens, 64 KiB of supplied evidence and bounded local query work. Measured usage can reconcile a reservation; an unknown or unfinished attempt keeps its reservation. Summaries have a larger per-answer output allowance than quick questions. Reaching a limit produces a partial-result notice, rather than silently dropping agents and claiming a complete report.

The default policy uses these non-monetary limits and does not promise a dollar ceiling. `OFFICER_REQUEST_USD` explicitly selects strict estimated-dollar admission. A strict request cannot make a paid call without applicable dated rates and a validated request-specific input-token bound. The standard transport currently supplies no such bound, so strict mode refuses calls rather than guessing a price, changing models or silently reverting to the default policy. The usage ledger below still records actual attempts and available estimates.

## Motet usage and cost

Ask Motet how much it has used or spent. Motet records its own API attempts locally in `~/.live-room/officer-usage.sqlite3`, including the intent check and each answer round. It retains ninety days of model names, token counters, request stage and completion status, within a bounded store. It does not save prompts, answers, project names, file paths, credentials or provider response bodies in this ledger. This accounting is separate from Motet memory and begins with the updated server; it does not reconstruct past bills.

Token counts come from API responses. Dollar amounts are **estimates**, using dated public rates in `config/PRICING.json`, rather than a bill or account balance. Unsupported pricing, missing usage, timeouts, or incomplete tracking remain unknown; they are never counted as free. The estimate covers recognized standard global requests only, without account-specific discounts, credits or taxes. Set `OFFICER_USAGE=0` when launching Motet to stop recording new usage. Use `python scripts/officer_usage.py --help` for local inspection and deletion. Fleet-wide agent costs remain a longer-term item: a context-window counter alone cannot establish spend.

## Anthropic-reported usage and cost

Optionally connect an Anthropic Console Admin API key to ask Motet about actual organization usage and costs. Save the key separately in `~/.live-room/anthropic-admin-key`, in the private state directory, as an owner-only regular file (`chmod 600` on macOS/Linux; on Windows see [Keys and settings on Windows](#keys-and-settings-on-windows)). Keep the normal model key in `~/.live-room/anthropic-key`. Admin keys have broad provider permissions; Motet uses this credential only for fixed, read-only reporting requests. Never put either key in the repository or a conversation. [Anthropic's Admin key setup](https://platform.claude.com/docs/en/manage-claude/admin-api-keys) explains creation and expiration.

Ask naturally: **“What is my actual Anthropic token usage and cost this month, broken down by model?”** Other examples are **“Show yesterday's reported usage”** and **“Compare reported token use and cost for the last seven days.”** Calendar ranges cover at most 31 days; requested local dates and the actual UTC-hour reporting bounds are returned separately. This connection follows Motet's existing platform restrictions.

These reports cover the Anthropic organization accessible to that key, including usage outside Motet. They are separate from the estimates for Motet’s own model calls and cannot identify a particular Motet agent's costs. Reported cost is in USD after discounts and before credits; it is not an invoice, prepaid balance, subscription allowance or remaining spending budget. Motet does not read or change spending limits through this connection. [Anthropic's reporting documentation](https://platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api) describes coverage: data can lag by up to 24 hours and is subject to later reconciliation. Motet receives each report's refresh time; missing or incomplete results remain unknown rather than zero.

Only validated numerical aggregates, dates, fixed qualifications and optional public model labels reach Motet. The admin key, raw provider bodies, account identities, actor names, workspace and API-key identifiers, pagination tokens and provider error text are excluded. Reporting creates no raw-report database, cache or background polling, and grants no new peer messaging or account-management capability. Existing Motet conversation history can retain the aggregate answers under its normal retention rules. Removing the optional credential stops subsequent reporting requests without affecting the model key.

## Keys and settings on Windows

Motet's answers, its model menu and the Anthropic-reported usage report work on Windows too. Windows has no owner-only file modes, so Motet reads each key file's access list instead: the file must be owned by you, give access only to you (and the system account), and not take its permissions from its folder. The `.live-room` folder must be owned by you and not open to other accounts. A link, junction or other reparse point in place of the file or the folder is refused. Motet never changes the permissions of a file you made: it stays off and says why.

Save the key as plain text. In PowerShell, run these two lines and paste the key when asked, so it is not kept in PowerShell's command history (Windows PowerShell's `>` writes UTF-16, which Motet does not read as a key):

```powershell
$k = Read-Host "Paste your Anthropic API key"
Set-Content -NoNewline -Encoding ascii "$env:USERPROFILE\.live-room\anthropic-key" $k
```

Then make the file yours alone in Command Prompt:

```
icacls "%USERPROFILE%\.live-room\anthropic-key" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:F"
```

Use `anthropic-admin-key` in place of `anthropic-key` for the admin key. The account is written as `%USERDOMAIN%\%USERNAME%` because a bare name can pick a different, same-named account on a work PC. Motet creates its saved model choice, `officer-model.json`, private on its own.

If Motet says something is not owned by you, it was probably made from an administrator window, which can give it to the Administrators group. Each message names its fix; run it in Command Prompt, then restart the server:

- The key file is not owned by you: `icacls "%USERPROFILE%\.live-room\anthropic-key" /setowner "%USERDOMAIN%\%USERNAME%"`, then the command above.
- The folder holding the key file is not owned by you (for example, the server was first started from an administrator window): `icacls "%USERPROFILE%\.live-room" /setowner "%USERDOMAIN%\%USERNAME%"`. The same fix applies if the Motet model choice cannot be saved.
- Other accounts can open the folder holding the key file: `icacls "%USERPROFILE%\.live-room" /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F"`.

## Motet memory

Talk to Motet as usual: **“What did Atlas do yesterday?”** Motet can search its retained memory, explain gaps, and show the saved sources behind its answer, including activity from chats no longer on the stage. You do not need to browse an archive or select a history tool first. Motet uses the host’s local calendar dates to interpret words such as “yesterday.”

Open **Memory** in the toolbar to enable recording. Recording starts **off**, with a **30-day** default retention adjustable from 1 to 365 days. The small settings panel also lets you pause recording, exclude chats or projects, and delete saved activity by chat, project, or all memory. Pausing or turning recording off keeps earlier retained activity available to Motet. A source button beside an answer opens that saved entry directly; it does not depend on the live desk's short history.

The archive records selected events from the local Claude Code, Codex, Cursor, and Antigravity logs that Motet follows. It stores bounded, sanitized conversation excerpts, operation categories, known outcomes, safe project-relative file references, and source identities. Raw command, tool-output, code, diff, preview, and native-snapshot payload fields are excluded, as are screenshots and cloud/process-list or hook-only observations. Recognizable code blocks, preview-capability links, credential patterns, and suspicious payloads are removed or omitted from excerpts before storage. Arbitrary conversation prose can still quote private technical material or an unrecognized secret; redaction is not a guarantee that every sensitive detail is detected. Treat the retained excerpts as private data.

History survives a Motet restart. Capture runs only while Motet is running and recording is active. Starting, enabling, or resuming recording establishes a fresh boundary at the end of existing log files; earlier content and work performed while Motet was stopped are not imported. Coverage reports identify recording intervals, omissions, and failures. A silent or incomplete record does not prove that an agent was idle or that an operation succeeded.

The underlying local archive is called the Deck Log. Same-named chats remain distinct by provider, project, and chat identity; Motet asks for clarification when needed. Daily digests are computed from saved events without extra model calls. A Motet answer uses the existing model service and may send it relevant retained excerpts. Archived text remains untrusted evidence and cannot authorize messages, controls, or permission decisions. The shared peer MCP does not expose this memory.

The separate SQLite database is `~/.live-room/deck-log.sqlite3`, outside the repository. Its directory and files must pass private ownership/permission checks on Unix or protected access-control checks on Windows; unsafe storage leaves recording unavailable. Other programs running as the same operating-system user remain inside this privacy boundary. Recording is bounded by a 64 MiB SQLite page limit, 50,000 events, and a bounded queue; reaching a limit or encountering a write failure stops or drops capture with an explicit coverage warning. Journal files can temporarily require additional disk space. Text search uses SQLite FTS5 when available, with a disclosed bounded plain-search fallback.

Expiry and deletion remove matching archive records and search data; daily digests are derived from the remaining records. Minimal hashed deletion markers remain to prevent replay from restoring erased entries. Deleting or expiring any Deck Log source conservatively removes all durable Motet history prose, including unrelated earlier answers. It does not delete original provider logs, copies already open in a browser, provider-side records, or operating-system backups. The archive has its own storage and does not erase messaging delivery/recovery records.

### Rolling back the Memory archive

Motet 0.2.0 upgrades the Deck Log to schema 5 the first time it starts, even if recording is off. Your saved entries, saved Motet history and Memory settings are kept. Schema 5 adds a record of who wrote each message.

**Motet 0.1.0 cannot use a schema 5 archive.** 0.1.0 refuses to open it: Memory shows **Storage unavailable**, its status line says "private storage unavailable", nothing is recorded, and saved Memory and Motet history cannot be read. In this case that message is not a permissions problem; it means the archive was upgraded by a newer Motet. 0.1.0 leaves the file unchanged, so starting 0.2.0 again brings everything back. 0.1.0 is already published and cannot be changed to explain this. Never change the schema version by hand: 0.1.0 would then open the archive but fail every write and turn recording off.

Builds earlier than 0.1.0 used schema 3 and cannot open schema 4 or later.

From 0.2.0, Motet reads the archive's version through a read-only opening before it changes anything:

- **Written by a newer Motet:** Motet leaves `deck-log.sqlite3` and any `-journal`, `-wal` or `-shm` file next to it exactly as they are, records nothing and shows the status "archive from newer motet".
- **Changes waiting in a `-wal` file:** this Motet never writes in that mode, so the changes belong to whatever program last used the archive. Motet leaves every file as it is and shows "archive recovery pending". Start the program that last used the archive and stop it cleanly.
- **A `-journal` file left by a write that stopped part-way (a crash, say):** unless the archive already shows a newer version, SQLite first undoes that unfinished write, as any SQLite program would. Motet then checks the version again before it changes anything else.

To keep the option of going back with your existing Memory, stop Motet before upgrading and save a private copy of `deck-log.sqlite3` outside the repository. After a clean stop there should be no `-journal`, `-wal`, or `-shm` files next to it. If any remain, complete SQLite recovery with the old build and stop it cleanly before copying. Verify the copy matches the original byte for byte and passes SQLite `PRAGMA integrity_check`. Keep it under the same private permissions as the live archive.

To roll back, stop Motet again. Move the newer Deck Log and any SQLite sidecars together to a private location, then restore a copy of the verified pre-upgrade file under the original name with private permissions. Start the earlier build and check Memory settings and a known retained entry. The restored copy contains only what existed when it was taken, and normal expiry still applies; anything recorded by the newer build is not in it. If restoring it would undo a deletion or exclusion you made after the backup, keep the newer archive offline and start the older build with fresh Memory. Without a backup, the older build can only run with fresh Memory: move the newer archive and its sidecars aside together, and keep them to restore if you upgrade again. Do not mix SQLite sidecars from different copies. This procedure applies only to the Deck Log.
