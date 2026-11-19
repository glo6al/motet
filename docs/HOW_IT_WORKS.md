# How Motet works

Where the stage gets what it shows, and where each part lives in the repository. Part of the [Motet README](../README.md).

## How it works

- **A log appears on the stage** when its file changed within the window and, for Claude Code, Codex and Antigravity logs, the last record that states a time is within the window by the log's own clock, so touching a file whose last timed record is outside the window does not bring it in. Only a record's own timestamp counts, never one quoted inside it; a log whose last timed record cannot be found is not admitted until one is written; Cursor's transcripts state no time and are judged by the file alone; a pinned log is followed regardless.
- **The server** (`src/server.py` and its provider adapters, standard library only) follows `~/.claude/projects/**.jsonl`, `~/.codex/sessions/**.jsonl` and `~/.cursor/projects/*/agent-transcripts/*/*.jsonl`, turns each record into an event, and streams events to the page over server-sent events on 127.0.0.1. A Cursor transcript records the request to run a tool, not the tool's result, so those steps are shown as returned when the turn ends, not as done. It records no link that opens the chat. Antigravity native JSONL and optional read-only SQLite metadata also feed the same event stream; see [Antigravity desks and monitors](PROVIDER_SUPPORT.md#antigravity-desks-and-monitors) for coverage. Local observation runs without an external service. Optional Motet requests use the Anthropic API, and supported actions use their provider adapters; see the [trust boundary](TRUST_BOUNDARY.md). Rendered previews are the one place agent-written code runs; the trust boundary says what they can and cannot reach.
- **The page** (`src/web/index.html`, one file, no build step) draws the stage with CSS 3D transforms. The camera stands where it would for five monitors and never backs away, so the monitors in front of you stay readable however many agents there are; the rest of the row curves out of frame and you turn to it. The fleet strip at the top is the map: one chip per project, one lit cell per agent.
- **The hooks** (`scripts/install_hooks.py`) are small shell commands Claude Code runs at Notification, Stop, SubagentStop and PermissionRequest. They post to the server with the key from a private header file.
- **Codex cloud agents** are found in the process list (macOS and Linux; `ps` about once a second on its own thread, and `lsof` once per new command for its folder). The list names every process; only this user's are considered, and only an exec server recognised by the text of its executable and subcommand, and its direct children, are kept: their pid, start time, command line and working folder. A folder name lsof had to escape is not trusted and shows as not readable. OpenAI marks that server experimental, so this can change under us.
- **Motet** appears on the stage as a plate on the bottom band (lamp, name, and the stage's own count of confirmed requests waiting on you, with requests the log only suggests counted separately as possible) that opens upward (click it, or press `/`) into a panel where you ask about the stage in plain English: what you missed, what an agent is doing, whether anything is stuck, who changed a file. Three one-tap chips carry the questions you ask most ("What did I miss?", "Anything stuck?", "Who is waiting on me?"). The server answers through the Anthropic API (automatic Haiku Q&A and Sonnet summaries; no routing-mode setup is required) with read-only tools over what the stage already holds (the agents, a bounded timeline each, everything since some minutes ago, a search across kept steps, whether a desk can be opened), keeping each record's own qualifications (a guessed command stays "probably", a failed change "tried to", an inferred parent "inferred"), and the panel shows the log entries it consulted as chips that jump to them (or say the entry is no longer on the desk). It can use the stage controls on your screen (filter, go to a desk, dismiss, draft a text for your clipboard, draft a Watch rule into the Watch panel, where only your press on Keep this rule saves it) and relay an explicitly requested message to a local Codex conversation. A separate interpreter sees only your latest request, not logs or earlier replies; the normal log-reading tools cannot send, approve or pin anything. The target, complete question and delivery status appear in the panel. It is off, and says why, unless an API key sits in `~/.live-room/anthropic-key` as a plain file only you can read, no key-shaped string sits anywhere in the project tree, and every file there could be read. Not offered on Windows yet.
- **Motet model:** ask normally; Motet chooses its model automatically, with no routing-mode click or setup. It uses Haiku for ordinary Q&A and Sonnet for summaries and handovers, according to `motet/MODEL_POLICY.md`. The existing intent check uses Haiku and classifies only your latest request; logs and skill cards cannot select a model. The answer keeps one model through all its tool rounds. Alternatively, choose any concrete model returned by your configured Anthropic API account to use that model for both the intent check and answer. Your selection is saved privately in `~/.live-room/officer-model.json`; older saved model choices automatically adopt routing when the updated server starts, without rewriting the private settings file. Only a new explicit manual choice overrides routing. The selector shows **Auto** during automatic routing and the selected model name for a manual override. A question already running keeps its original plan. A missing policy or unavailable required model prevents automatic routing rather than silently substituting another model. This changes only Motet. Use the single agent chooser on the left to select a message recipient.
- **At a station**, the conversation panel shows recorded agent messages, queue observations and direct-message delivery records in the agent lane, and Motet answers in its own lane. The composer explicitly selects the agent or Motet; changing recipient or scope does not send anything. Each address keeps its own draft, and submitted messages keep their original target even if you change stations. Press `/` to ask Motet. Recorded evidence opens in the work area, and citations reveal their exact conversation or activity entry. Pending answers do not move you unexpectedly: suggested screen changes wait for your click when you have navigated away.
- **Everything in a log is treated as untrusted.** An agent can write anything into its own transcript. Counts are validated and text is escaped before it reaches the page, and the page's script runs under a per-request nonce so that nothing arriving as data can run even where an escape is missed.
- **Faces:** each monitor's avatar is a machine head drawn from the session id, so an agent looks the same every day. Its visor light only repeats what the monitor border says: scanning while a step is open, steady while the agent waits on you, dim between steps, dark when the log is silent. It has no expressions, because the logs record none.
- **Project names:** a folder can be shown under another name by listing it in `~/.live-room/projects.json` as `{"folder name": "shown as"}`, read when a window connects. Double-clicking a chip sets a name for that browser only. Neither is in the public files.
- **What counts as a risk, a check, or "probably needs you"** is the Watch button in the bar. Type a command or a sentence and the panel draws the banner the stage would give it; if that is wrong, click what it should be and the stage learns those words (`make release` as a deploy, `want me to commit` as asking you). Your rules are listed as sentences with a remove; the built-in rules are folded below, each with a switch and a sample of what it watches. The words are kept in `~/.live-room/watch.json`; the shipped patterns stay in `src/watch.json`. Who started whom is not in either file.
- **Links between agents**, once made, are kept in `~/.live-room/links.json` (agent ids, the parent, a one-line reason, and whether it was recorded or inferred; never prompt text or folders) and restored at start-up. A recorded link replaces an inferred one; an inferred one never replaces anything; a stored link is dropped after thirty days without the child being seen. "Unlink" at the desk drops a stored inferred link.

## Project layout

    src/server.py            the server: follows the logs, serves the page, holds permission prompts
    src/officer_instructions.py  bounded instruction loader and built-in fallback
    src/officer_policy.py    fixed model policy and per-question routing snapshots
    src/officer_usage.py     private usage ledger and cost estimates for Motet’s model calls
    motet/                   Motet voice, vocabulary, and five lazy skill cards
    motet/MODEL_POLICY.md    Haiku Q&A and Sonnet summary model mapping
    motet/PRICING.json       dated standard API rates used for estimates
    src/watch.json           deploy, push, delete, send, checks, and "probably needs you"
    src/codex_desktop_control.py  version-checked local Codex steering and interruption
    src/web/index.html       the stage (one page, no build step)
    src/watch_tty.py         the stage in a terminal: board, tail, or a one-line count
    src/iterm-profile.json   the iTerm2 profile watch_tty.py --tab installs
    tests/test_parsers.py    the shell parser, watch.json, one Cursor transcript, terminal escaping, the key and previews (run on every push)
    tests/test_officer_messaging.py  durable requests, queue failures and reply correlation
    tests/test_peer_reply_delivery.py  immutable return routing and one-way notice attempts
    tests/test_peer_reply_integration.py  real MCP/hook/HTTP/plugin path with mocked native turns
    tests/test_officer_routing.py    isolated intent interpretation and dispatch retries
    tests/test_officer_controls.py   durable control outcomes and active-turn checks
    tests/test_officer_fulfillment.py  verified pause fulfillment, migration and concurrency
    tests/test_officer_instructions.py  instruction fallback, snapshots, and skill boundaries
    tests/test_officer_evals.py  actual demo evidence and counterfactual golden checks
    tests/test_officer_policy.py  automatic routing and manual overrides
    tests/test_officer_usage.py  usage completeness, privacy, retention, and estimates
    tests/test_codex_desktop_control.py  bounded desktop bridge and failure handling
    tests/test_page.js       UI logic regressions: routing, drafts, histories, capability checks, requests, navigation, layout state and mutation checks (Node)
    scripts/install_hooks.py adds or removes the Claude Code hooks
    scripts/install_reply_hook.py removes legacy user-level reply hooks
    scripts/officer_usage.py inspect or delete local Motet usage records
    scripts/check_secrets.py refuses to publish a key: names, link text and contents, any size, in the working tree and in what git has staged (the staged copy is what a commit publishes); anything it cannot examine fails it. Run before any commit (the tests run it too)
    docs/ROADMAP.md          what is planned and what is unproven
    docs/EVIDENCE.md         what you can and cannot see
    docs/PROVIDER_SUPPORT.md what works with which app, and the conditions
    docs/SETUP.md            running it, the hooks, the terminal view, Windows differences
    docs/TOUR.md             the stage and the desk, in Workspace and Classic
    docs/MOTET.md            the assistant: instructions, evidence, cost, memory
    docs/MESSAGING.md        messaging, steering, native questions, the Claude plugin
    docs/TRUST_BOUNDARY.md   what Motet can reach and what leaves the computer
    docs/HOW_IT_WORKS.md     this page
    docs/WINDOWS.md          installing and trying it on Windows, and what to report
    docs/media/              the README wordmark and screenshots
