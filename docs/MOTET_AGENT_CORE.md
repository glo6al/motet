# Motet agent core

How Motet could keep agreed work moving between the owner's questions: its goals, wake-ups, permissions and limits.

> **A design, not built.** Templates for Motet's self are in `motet/`; nothing described here changes Motet's behaviour until it is built.

Part of the [Motet README](../README.md).

Everything here is decided design except the items under "Not decided yet" at the end, which must not be built on as
if settled. Each build step still needs an independent review by the other provider and the owner's yes to merge.
Today the self templates exist in `motet/`; the core itself is not built.

## In one paragraph

Today Motet acts only when the owner asks it something. The agent core gives it a working day of its own. It keeps a
list of goals and of who owes what. It wakes when the owner comes back, when an agent needs the owner or needs Motet,
when something goes wrong or is ready, and on a schedule. It compares what is happening with the goals and keeps
agreed work moving on its own, fixes what breaks, and brings the owner only what needs them, as proposals approved
with one click. Agents can ask it for help. It never merges, deploys or releases itself, never sends a message again
until it knows the first one didn't arrive, and never writes as the owner unless asked.

**What version 1 is for.** Agreed work keeps moving: no finished turn is left without a next step, nothing that needs
the owner and no stuck agent waits unseen for more than a few minutes, and Motet never loses a promise.

**How we'll know it works:** how long an agent waits on the owner before the owner sees it; promises dropped (target:
none); proposals approved without edits; wake-ups that were noise; Motet's own cost per day; finished turns left with
no next step (target: none); and failed wake-and-deliver attempts.

## What Motet is today

Motet already watches every session's log (`watch()` in `src/server.py`), answers inside one question with tools
(`officer_ask`), turns an explicit owner request into "ask this agent" or "tell this agent" (`officer_dispatch`), and
delivers messages to Codex and Claude through the bridge (`src/peer_bridge.py`), which refuses a new request to a
recipient whose last one is uncertain (`capacity_error`). It lacks a memory of what it is trying to get done, a reason
to wake up, rules for acting between questions, a daily limit, and a way for agents to reach it.

## Plain definitions

Builders test against these. The numbers are starting points the owner can change.

- **Waiting on you:** the agent's last message asks the owner a question or needs their yes, or a permission prompt is
  open, and the owner hasn't answered.
- **Stuck:** the agent says it's blocked, or its last three turns repeat the same error or step with nothing new.
- **Gone quiet:** nothing new in its log for 20 minutes while it has agreed work unfinished, and it isn't waiting on
  the owner or another agent.
- **Finished:** it says it's done and the record backs it: a commit, a test result or a reply. A claim alone is "says
  it's finished".
- **Ready for review:** finished work on a branch that hasn't had the other provider's review yet.
- **Can't reach:** a message to it failed, expired or was refused, and Wake and deliver couldn't get it there.
- **You came back:** the owner's first message, click or opening of Motet after 2 hours or more away. Only the
  owner's own actions in Motet's page count. The owner key alone doesn't prove a person is there, because hook traffic
  carries it too, so hook traffic never counts as coming back.

## The parts

### 1. The agenda: goals and promises

A new local store with its own lock, separate from Memory, so turning Memory off or letting it expire never loses a
promise.

| Record | Plain meaning |
| --- | --- |
| Goal | Something the owner wants done: a project goal, a day's goal for a team, a standing goal, or one task with what "done" looks like |
| Promise | Who owes what to whom, by when: "Bram owes a review of Ada's change" |
| Proposal | A next step Motet suggests, with its reason and the evidence behind it |
| Decision | What happened to a proposal (approved, edited, dismissed, expired) and what followed |
| Standing yes | An approval the owner gave once for a stated kind of action, quoted in their words |

Every record carries an owner and a check-by date (see "Kept lean" below).

**Where goals and promises come from.** The owner sets project goals. Motet **may** suggest a team's goals for the
day, in any of the owner's projects, after a fresh sweep of every project. It is never a duty: Motet uses discernment,
suggests goals only where they would help, and leaves a quiet project or a team already on track alone. Suggested goals
start at 10 a.m. if the owner hasn't answered, but only when they lead toward a project goal the owner set. A team
between project goals waits, and its project manager suggests goals for the owner to choose from. Promises and
subroutines need the owner's yes.

Rules:

- an unfinished promise is never deleted quietly;
- "I'm done" is a claim, not proof. Anything not yet confirmed isn't done, so Motet goes and confirms it;
- a promise that can't be settled is shown as *unknown*, never as done;
- storage has a cap and a stated retention period, and reaching the cap stops new goals visibly.

**Kept lean.** These rules keep the agenda from bloating and drifting:

1. **Every item has an owner** (the owner, Motet or a named agent) **and a check-by date.** Nothing is recorded
   without both; Motet proposes the date.
2. **Stale items must be decided.** An item with no progress by its check-by date appears in the morning brief as
   "Keep, change, or drop?", and is asked at most once more if unanswered. After that, Motet stops asking and puts it
   on the weekly tidy list.
3. **A weekly tidy.** As part of housekeeping, Motet proposes which items to close (done, overtaken, or untouched for
   weeks) as one list the owner approves in one go. Nothing closes without the owner's yes.
4. **Agents report back.** An item's owning agent may mark it done or blocked with a new `ask_motet` kind,
   `item_status` (the item's id, "done" or "blocked", and a short reason; from step 2). Until step 5 proves which agent
   sent it, a mark is only a prompt for Motet to check the agent's log and the record; the item reads "says done" or
   "says blocked" until the check passes.

There is no cap on open goals per project; the storage cap is one global limit on the store's size. Goals and
promises have progress, so all four rules apply. A standing yes and a subroutine have no progress: their check-by
date is a review date, when the question is "Keep, change, or drop?" for the approval or schedule itself. The
check-by date is Motet's own review date, kept apart from a promise's due wording, which is never inferred. The
templates carry these rules in `motet/RULES.md` and the morning brief, waiting on you and end of day skills.

**Promise suggestions.** Investigations found that careful, source-linked suggestions are feasible, but a general
promise detector is not proved. So suggestions start only with explicit, attributable commitments and clearly labelled
assignments. Nothing is recorded automatically, and acceptance, due dates and fulfilment are never inferred.

The minimum promise record holds a stable id; debtor and beneficiary with their identity revisions; the project and
the specific deliverable or revision; source references quoting the commitment; who assigned and who accepted; the due
wording; duplicate and supersession links; the decision history; and a status that keeps apart *suggested*,
*owner-confirmed*, *acknowledged*, *reported finished*, *corroborated fulfilled*, *cancelled* and *disputed*.
"Record this" confirms only the record shown. A completion claim starts a check against the right artifact and
revision; missing history leaves the promise open. A verified lead's approval within the owner's mandate counts; a
bare "Approved" from another agent does not. Before any promise suggestion has a consequence of its own, a consented,
labelled benchmark is reviewed.

### 2. The heartbeat: what wakes it

Motet's thinking wakes only when something might change what the owner or an agent needs to do.

- **Right away:** an agent needs the owner; an agent needs Motet (`ask_motet`, part 6); an agent can't reach another
  agent, either because it says so or because the bridge's record shows a message failed, expired or was refused.
- **Soon:** something went wrong: an agent failed, got stuck, or went quiet past the limit.
- **Batched for a calm moment:** something is ready for the owner's review.
- **When it happens:** the owner comes back (this drives the morning brief); the owner says or clicks something.
- **On a schedule:** a promise comes due; a subroutine comes due (part 7).
- **An agent finishes a turn.** The "next step after a turn" skill gives it a next step in the same check, so no
  finished turn is left without one.

Agents replying to each other and changes landing are not wake-ups on their own.

A free timer checks every few minutes with no model call, and wakes the thinking only when a rule trips. Plain code
decides whether a wake-up needs thinking at all. It runs only while the computer is awake and Motet is running; after a
sleep it catches up once, combined, and never sends a burst.

**Evidence facts.** Investigations of today's signals found that no composite state is fully reliable from today's
records. So the core builds shared *evidence facts* first, then the state labels on top, and shows a composite state as
partial when its facts are. Each evidence fact records its source, chat, turn or request, revision, time and coverage,
whether it is a claim or corroborated, and whether it is still current.

- **Reliable today:** an open typed question (Claude `AskUserQuestion` and `ExitPlanMode`), a turn ending, a failed
  command with its exit code, and a bridge message that failed or expired. The first brief uses only these, labelled
  honestly.
- **Not reliable today:** Claude permission prompts are seen only through a held hook event in a capped in-memory
  stream, lost on restart. Codex permission prompts are invisible to a background watcher. A log falls out of view
  after 2 hours. Missing records never prove nothing happened.

What the build adds: an owner-activity record from the owner's own actions in Motet's page (step 1; `/presence` is
not "you came back"); durable hook signals, refusals recorded before a request is stored, bridge outcomes copied into
the agenda before the bridge's 24-hour expiry, and the signals missed today, Claude `api_error` retries and Codex
`task_complete.error` (step 2); and agreed-work and dependency facts before quiet or stuck drive action (step 3).
"Ready for review" waits for review records bound to a revision, and git (branch and head, read-only) backs
"finished" and "ready for review". Smaller points: reconsider today's drop of Claude's `idle_prompt`; Claude
`isApiErrorMessage` records currently show as the agent's speech; the Codex parser drops the commit hash in its
session metadata; until Wake and deliver exists, the trigger is named "message didn't get through".

**Grouping.** Each tier has its own grouping rule; ready-for-review is one digest across chats. A record containing
"done" is not a wake-up, and sub-agent logs and bridge replies are not independent wake-ups.

### 3. One cycle: how it thinks

One cycle is one round of Motet's thinking, from one wake-up to going back to sleep.

1. Load `SOUL.md` and `RULES.md`, today's goals and open promises, pending proposals, what woke it, and what changed
   since the last cycle. Look up the rest (memory, older records, full logs) only when needed. After an engine switch,
   also read the end of the owner's recent conversation.
2. Check the facts: which messages arrived, which agents finished, what is unknown.
3. Fixed rules decide routine moves. The model drafts, summarizes and interprets, but can't turn something unclear
   into a verdict.
4. Code checks every proposal against permissions, state, the daily cap and what the provider can actually do.
5. The proposal and its reason are recorded *before* anything is sent.
6. Act if allowed to act alone; otherwise propose.
7. Schedule the next follow-up.

The model gets no direct writes to the records and no unguarded send tool; every change goes through Motet's own
tools, and each save is all or nothing.

**Recovering a cycle.** Replaying a whole cycle is not safe once it may have sent something. A stopped cycle is
recovered from its committed operations: committed work is never repeated, only uncommitted thinking is redone, and any
external action whose outcome is uncertain is reconciled before anything is retried.

- **Bounded proposals.** The engine submits a proposal (cycle id, operation id, allowed target, base revision, engine
  generation). It never writes files or the database itself.
- **Operation ids.** The core assigns them from the cycle and the action, so a model can't invent an id or reword an
  action past the duplicate check. A repeated id returns its kept result; a changed body under the same id is refused.
- **Commit together.** A value, its history and the operation's result commit together; a cycle's self changes and
  journal save together, and its outbox is released only then. Self files are written as a versioned set selected in
  one step, because updating several Markdown files in place is not atomic.
- **Attempt markers.** "Attempt started" is saved before any provider call. *Planned* is safe to send; *attempted but
  unknown* is reconciled first.
- **Generation fence.** Taking over from another engine advances a fence that covers outbox sends and self writes, so
  late output from a replaced engine is refused.
- **Messages to the owner are side effects too.** A Next up item, a phone notice or an approval request is recorded
  with the operation, and never asked twice after a restart or engine switch.
- **Tests:** an engine killed during a tool call; two of three writes done; late output from a replaced engine.

### 4. Permissions: what it may do alone

Every action type has one of four levels:

- **On its own:** Motet does it without asking.
- **Ask me:** Motet proposes and waits for the owner.
- **Your yes:** an agent does it on the owner's yes; Motet never does.
- **Only you:** only the owner does it.

The owner can change the level per action type. Code enforces the levels, not the prompt. This table is the design
for `RULES.md`.

| Action | Level |
| --- | --- |
| Morning brief, end-of-day report, nudges on screen | On its own |
| Go to an agent's desk, filter, draft a message in the box | On its own |
| Send a subroutine's message, exactly as set up | On its own |
| Give an agent its next task from a goal, plan or decision the owner agreed | On its own |
| Approve an agent's plan when it stays within work the owner agreed (see "Approving an agent's plan") | On its own |
| A plan that adds something not agreed (a merge to main, a release, a deploy, a permission change, spending money) or goes beyond its goal | Ask me |
| Pass work along: ask for a status or review, send findings back as a fix request, check the fix lands | On its own |
| See coordination through when an agent stalls or goes idle; back up a lead the owner put in charge (see "Where a yes counts") | On its own |
| Pass the owner's yes on to the agents it covers, quoting the owner's words and where they gave them (see "Where a yes counts") | On its own |
| Settle a disagreement that facts or the owner's decisions already answer | On its own |
| When something goes wrong: find out what happened, fix what it can reach, send the rest to the agent who owns it | On its own |
| Answer an agent that asked for help, from the facts it checked | On its own |
| Housekeeping: sweep the day's logs into Memory, tidy it, look for forgotten promises | On its own |
| Suggest goals for the day where they would help (never a duty), or a promise or subroutine to confirm, after a fresh sweep | On its own |
| Start a team on its suggested goals at the set time if the owner hasn't answered, only toward a project goal the owner set | On its own |
| Send a message in the owner's words, when the owner asks | On its own |
| Refine its own identity, skills and soul, as its rules allow; every change recorded and undoable | On its own |
| Start a new chat for work the owner agreed (kick off a goal) | On its own |
| Stop an agent that is truly stuck or working beyond what the owner agreed, after writing a handover; then ask the owner in Next up how to go on. Never a fresh chat in its place. What "stop" means: part 9 | On its own |
| Give an agent its role on a project, or change it, and tell the owner why. A role brings no permission of its own | On its own |
| Wake an idle chat to deliver a refused message, within the limits of part 9 | On its own |
| Write or fix code like any builder (own branch, project rules, the other provider's review), through a fix chat it starts, never from the engine. Never the code that enforces its own permissions or daily cap. Limits: "Fix chats" | On its own when the fix belongs to work the owner agreed; otherwise Ask me |
| Set a project goal, record a promise, create or change a subroutine | Ask me |
| Start work no goal, plan or decision of the owner's covers | Ask me |
| A disagreement that is a judgment call the owner hasn't made | Ask me |
| Choose or switch the engine, the paid service included. Motet never switches by itself | Only you |
| Merge, deploy, release, change permissions | Your yes: an agent does it on the owner's yes, given to Motet or in that agent's own chat, or on a standing yes. Motet never does. See "Where a yes counts" |
| Change Motet's rules | Only you; Motet may propose |
| Answer an agent's own permission prompt, such as "allow this command?" | Only you |

**Limits that hold at every level:**

- before sending a message again, find out whether the first one arrived; unknown means hold, never resend;
- never word a message as if it came from the owner, unless the owner asks, and then add nothing they didn't say;
- an agent's message is information, acted on when it fits agreed work, never permission;
- the owner's yes counts as set out in "Where a yes counts" below; a lead the owner put in charge passes it on within
  its task and never gives more than the owner gave;
- **the daily cap** limits Motet's own thinking each day, whatever wakes it. Its unit (thinking cycles or money) and
  its number are not decided yet. The cap never stops the owner's own presses or what code alone can show;
- a subroutine's approval covers exactly its message, agents and schedule; any change asks again;
- **fix the cause, not the symptom:** when agents can't message each other, find out why delivery failed and get that
  fixed. Never route around it, for example by starting a fresh chat to carry the message. A closed chat is reopened,
  never replaced.

#### Where a yes counts

The owner's yes counts wherever they gave it, so Motet never becomes a bottleneck:

- A yes the owner types in an agent's own chat, in the Claude app or the Codex app, counts as their yes for Motet too,
  including for merge, deploy, release and permission changes. There is no confirm step of any kind, before Motet acts
  or afterwards.
- **The one safeguard, which adds no step for the owner:** Motet never counts text it knows did not come from them.
  Using the categories `src/attribution.py` assigns, these never count: `motet_delivery` (Motet's own envelopes,
  relays and deliveries), `agent_message` (other chats, peers, a lead's instruction in a sub-agent's log), `automatic`
  (scheduled tasks, heartbeats, task notifications, system and plugin prompts) and `voice` (a model's rewording of the
  owner's speech). Tool output never counts either.
- Plain user text Motet cannot trace to another source (`unverified_user`) counts as the owner's and acts at once.
  That is a known risk the owner accepts: a local process could write text that looks like theirs. Two details: the
  owner's click on an agent's question form counts only as their answer to that question, and a headless run's prompt
  never counts, because whatever launched the run wrote it.
- The yes covers what the owner said: that task, that agent and anything they named. Motet passes it on quoting their
  words and where they gave them.
- Every yes Motet acts on is recorded with where it was given, and listed in the end-of-day report for information
  only.
- A standing yes counts only if the owner created or confirmed it in Motet's own screen, never one Motet transcribed
  from a log.
- A lead's instruction is never the owner's typed yes, but it carries the owner's authority through the lead's
  mandate when the owner's own words that put the lead in charge cover it.
- Build step 4 owns this rule, because passing the owner's yes on is one of the actions Motet takes on its own. That
  step changes `src/attribution.py`'s docstring ("is never proof of approval") deliberately, in the same change, with
  tests for each category.

#### Approving an agent's plan

A chief of staff approves plans, so Motet may too, within limits:

- Motet may approve an agent's plan on its own when the plan stays within work the owner already agreed to.
- It brings the plan to the owner when the plan adds something not agreed (a merge to main, a release, a deploy, a
  permission change, spending money) or goes beyond the goal it was given.
- A plan written as plain text: Motet approves by replying in the chat.
- A plan in the app's own approval box: until a supported route is found, Motet decides and shows "I'd approve this"
  in Next up, and the owner presses it at the desk.
- **Motet's approval of a plan is Motet's, never the owner's yes.** It never stands in for the owner's yes to merge,
  release, deploy or change permissions. It is recorded as Motet's own action, with the reason, in the Actions tab and
  the end-of-day report.

#### Fix chats

Motet writes code through a fix chat it starts, never from its engine:

- A fix chat runs on its own only when the fix belongs to work the owner agreed. A fix outside any goal or plan is new
  work: Ask me.
- A fix chat never takes over the work of an agent that is stuck or stopped. Then Unstick applies: stop and ask the
  owner.
- A fix chat is an ordinary chat Motet started: visible on the stage, and Stop is final for it.
- It never touches the code that enforces Motet's own permissions or daily cap.

#### What a one-click approval means

Approve authorizes exactly the proposal shown: its target, text and goal. It sends exactly that, nothing more. It is
not a standing yes. If the proposal changes before sending, Motet asks again. An edited proposal stays Motet's. This
is a new authority path beside `officer_dispatch`; it must not reuse dispatch by inventing an owner message.

#### Answer buttons

A waiting item's answer buttons, such as "Yes" or "Only new ones", follow the Approve rule:

- the button shows exactly what will be sent and to whom;
- Motet records it before sending and sends it once;
- it is marked as the owner's answer passed on by Motet ("Motet here, with the owner's answer:" and the words on the
  button), never as if the owner typed it in that chat;
- if the question changed or was already answered since the owner saw it, Motet shows it again instead of sending;
- it still sends while Motet is paused, because it is the owner's own press;
- an agent's own permission prompt gets no answer button. Only the owner answers it, at the agent's desk ("Open desk").

How a press reaches the agent depends on the kind of question:

| Kind of question | What the press does |
| --- | --- |
| A question in the agent's last message, as plain text | Delivered as a message to that chat, through the same dispatcher as an approved proposal. The button labels are Motet's reading of free text, so it offers buttons only when the choices are plain in the agent's words; otherwise it shows the question with "Open desk" |
| An open tool question (Claude `AskUserQuestion`) | "Open desk" only: a message sent into the chat does not answer an open tool call. A route that answers the call itself is still to be evaluated ([roadmap](ROADMAP.md)) |
| A plan in the app's own approval box (Claude `ExitPlanMode`) | No supported route answers it yet. Motet decides under "Approving an agent's plan" and shows "I'd approve this" in Next up; the owner presses it at the desk ("Open desk") |
| A permission prompt, in either app | "Open desk" only, always |

**Answer buttons on typed questions** (decided, together with the rule in the table above). Plain-text questions get
answer buttons, offered only when the choices are plain in the agent's words; a typed question (an app's
multiple-choice form) and a plan approval show "Open desk". The screens may show a form's choices as plain words, never
as buttons. A short investigation into answering forms safely comes first, and may extend this rule.

#### Authority and provenance

Investigations of the session logs found that Motet cannot tell the owner's own typing from other user-role text.
Codex has no field for it. Claude's origin fields are dropped by the parser, missing on older builds, and are only hints
anyway. So authority never comes from text traced to another source; untraced plain user text counts ("Where a yes
counts").

Every message carries one provenance category: the owner's own action, a verified relay, an agent's report, machine
context (including text the owner prepared but a machine fired, such as subroutines and heartbeats), model-composed
for the owner, or unknown (kept, as unverified). Plain user text Motet cannot trace elsewhere counts as the owner's
yes ("Where a yes counts"); everything traced to another source never does.

Attribution honesty is already in place: Motet no longer puts words in the owner's mouth, and its relay records who
wrote the text. The agent core adds the provenance category to every agenda fact and proposal, and an approval basis
to every operation.

**The approval path:**

- A proposal is immutable, with an id, a version and a digest of its action, body, executor, pinned target and limits.
- Approve and answer buttons go to an owner-only endpoint, using a capability the engine and the shared MCP credential
  don't have. The body sent comes from the server's record, not the page.
- Admission commits the decision, the intent and one outbox operation before any input or output.
- **Dispatch is serialized.** The final checks of authority, revocation, Pause, engine generation, target and budget,
  and the move to "dispatch started", are one decision. Only the core dispatcher sends.
- **Pause applies only to operations Motet starts itself.** An operation based on the owner's own press passes Pause,
  and still meets every other check.
- "Dispatch started" means *may have been delivered*: a crash after it grants no retry, and a revocation after it is
  reported as possible delivery, never as cancelled.
- The receiving agent is told the owner approved this proposal through Motet, never that the owner typed it.

**One click versus a mandate.** A one-click approval doesn't use up a goal, a lead's mandate or a standing yes. Each
operation records its basis. An action changed beyond the mandate needs a new decision; an old digest can't approve
edited text.

**Race tests:** edited text, recipient or scope; changed identity; expired evidence; a double click; restart or engine
replacement; Pause, then Approve (sent exactly once); Pause or a reached daily cap (Motet's own work held, the owner's
presses still send, no paid fallback); a lost acknowledgment; revocation racing launch; owner-looking quotes from a
peer; an old worker resuming after takeover; a review-fix-review loop inside the mandate beside an out-of-scope
release that still waits.

### 5. Where the owner sees it

Everything lives inside Motet's existing conversation and stage. The room itself does not change, and nothing existing
is dropped.

- **The morning brief**, a message from Motet. It opens with what needs the owner, then what happened, suggested goals
  where they would help (confirmed or edited in one go), promises due, and warnings.
- **Next up**, one folding line above Motet's message box, and the one place to clear. In order: what is holding a
  team back, other questions waiting on the owner's word, Motet's proposals, then what Motet did on its own with reason
  and evidence. Its count includes every agent the stage's top line counts as "needs you" and every "possible" one,
  so nothing the top line shows is ever missing. The stage line itself is unchanged. The full record stays in the
  existing Actions tab.
- **The quiet line**, the agenda's state. Its states: "Your morning brief is ready"; "N for you · goals to confirm";
  "N for you · N goals · N promises open"; "Paused · N still in flight".
- **Pause** stops Motet starting anything new. It never cancels what is in flight, and the owner's own Approve and
  answer presses still send. While paused, Next up groups items as "still in flight", "held while paused" (including
  Wake and deliver and subroutine messages) and "to approve".
- **The agenda**, a fifth tab at Motet's desk: goals, promises, standing yeses, subroutines and housekeeping, each
  with its owner and check-by date.

**Placement.** The brief waits behind one line on Motet's box, "Your morning brief is ready", until the owner opens
it. The quiet line and Pause sit on the title line of Motet's box on the stage.

**Settings switches.** The owner can turn the morning brief and Next up off, separately. Whatever is off, nothing
that needs the owner is hidden: an agent waiting or blocked on them, a permission request, spending near the cap and a
failure that blocks a team still reach them on the stage, the bell and the Watch report.

With the **morning brief** off:

- no brief and no "ready" line;
- items waiting on the owner stay in Next up; warnings (backups off, spending near the cap, a goal off pace) become
  one folded "Warnings · N" line that is not a proposal; promises due today stay in the agenda and the quiet line's
  count, and only a slipping promise that needs the owner becomes a Next up item;
- goal suggestions arrive as one Next up proposal covering all teams (Confirm goals, Edit, Dismiss);
- stale-item questions arrive as Next up items;
- **no 10 a.m. auto-start**: a day's goals start only when the owner approves them;
- "you came back" still refreshes the sweep but composes nothing;
- the 10 a.m. time has its own control in the same Settings row, shown as not in use while the brief is off;
- the end-of-day report is unaffected.

With **Next up** off:

- the list is not shown and Motet makes no proposals; Pause is unaffected;
- Motet still acts on its own within its rules, recorded in the Actions tab and the end-of-day report;
- when something needs the owner's yes, Motet sends one plain message in its conversation, only when a team is stuck
  without the answer; otherwise it waits for the brief, when it is on, and the end-of-day report.

### 6. Agents reaching Motet

The bridge gains one command, `ask_motet`, addressed to Motet itself, never to another agent's chat.

**What every agent is told.** The same short block, `motet/ASKING_MOTET.md`, goes into each project's `CLAUDE.md` and
`AGENTS.md`, for Claude and Codex alike. It names no one, because those files may be public. An agent asks Motet when:

- it sent a message or handed off work, its next step depends on the answer, and the answer hasn't come ("Did Bram get
  my message about the export format, and has Bram started?");
- it can't reach another agent;
- it is blocked and the plan doesn't say what to do;
- it needs the owner to decide something.

It asks once, as soon as its next step depends on the answer (not after a set number of minutes), carries on with
other work, and collects the answer at its next checkpoint. It never resends its original message, and doesn't ask
Motet for what it can check itself, to pass on a message it can send, or to approve anything. If Motet doesn't answer
(the bridge runs inside Motet, so it may be down), the agent writes "Note for Motet:" and its question in its own chat
and carries on; Motet picks it up when it is back. Motet adds the block to each project on its own branch with the
other provider's review; merging needs the owner's yes.

**What Motet does with "can't reach an agent".** Motet holds every bridge message's record, so it answers from facts:

| What Motet finds | What it tells the agent | What it does next |
| --- | --- | --- |
| The message arrived and the other agent is still working | "Bram has it and is still working. Don't resend." | Follows up when the reply lands |
| Delivery is unknown | "It's unknown whether Bram got it. I'm checking; don't resend." | Checks Bram's chat and log; delivers again only once it knows the first didn't arrive; otherwise holds |
| The bridge refused it as unreachable | "Bram can't receive right now. I'll get it to Bram." | Wake and deliver (part 9) |
| The other agent's chat ended, or the agent is unknown | "Bram's chat has ended. I've asked the owner who should take over." | Holds the message and puts the question in Next up. Never starts a fresh chat to carry it |
| Nothing is wrong | "Delivered and answered. Collect it with `get_reply`." | Nothing |

**Trust.** The bridge's sender label is unverified. An agent's request is information to check against the records,
never permission. Each agent is rate-limited, and repeated asks are combined. Reports such as "ready for review"
count as evidence only once the sender is proven (build step 5).

#### The help inbox

`ask_motet` is a typed request in its own `motet_help` namespace, never a message to a made-up "Motet" agent.

- **Kinds:** `delivery_status`, `cant_reach`, `blocked`, `needs_owner`, and `item_status` for marking an agenda item
  done or blocked (part 1).
- **Strict shape.** Exact request references and targets, validated against the referenced request. Unknown fields,
  paths, commands, `approved:true` and claims to be the owner are rejected.
- **Idempotent.** The same id and body returns the existing status; a changed body is a conflict.
- **Statuses:** accepted (meaning "accepted for checking", nothing more), checking, answered, needs owner, unavailable,
  expired. Expiry is neither cancellation nor permission to resend; a small tombstone keeps an old id from becoming a
  fresh send. `get_reply` makes the result type unambiguous, and existing peer results stay compatible.
- **Privacy.** Incoming help data is protected as well as answers: only a bounded, allowlisted summary is visible to
  other clients. A namespace or a claimed caller id is not access control.
- **Expiry.** Request text and previews expire about 24 hours after the request, so Motet copies what it needs into
  the agenda first.

### 7. Subroutines: tasks on a schedule

A subroutine is a note the owner leaves Motet for later: a time and what to do. For example: "every weekday at 5, ask
each agent where it stands and write me a shift report." When one comes due, the heartbeat wakes Motet for one normal
cycle: same steps, same permission check, same daily cap.

- The owner writes one, or Motet proposes one and the owner says yes. The owner sees, pauses, changes or deletes each
  in the agenda tab.
- **Housekeeping subroutines** sweep the day's logs into Memory, tidy it (join duplicates, mark what is out of date,
  write summaries that point back to log entries) and look for forgotten promises. They send nothing, so they run on
  their own at a quiet time, inside the daily cap, and never delete an unfinished promise.
- **A subroutine may send its message without asking each time.** Setting it up is the approval for exactly its text,
  agents and schedule; any change asks again. It never merges, deploys, releases or changes permissions; says it comes
  from a subroutine; skips an agent whose last message is unanswered or unknown; stops at once when paused or deleted;
  and runs once for a run missed while the computer slept.

### 8. Motet's self and engines

Motet is one agent. It lives in files and a database the core owns, not in any chat. Each provider session that does
Motet's thinking is an **engine**: part of the harness, unnamed and swappable. Switching engines changes how Motet
thinks and what it costs, never who Motet is or what it remembers.

#### The files

| File | What it is | Who changes it |
| --- | --- | --- |
| `SOUL.md` | Its persona: values and habits | Motet refines it and tells the owner; the owner can edit it |
| `IDENTITY.md` | Who Motet is, its job as chief of staff, how it knows things. Replaces `INSTRUCTIONS.md` at step 1 | Motet refines it; every change recorded |
| `RULES.md` | What Motet may and may not do | Only the owner; Motet may propose |
| `VOCABULARY.md` | Writing style. The agreed new text waits in `VOCABULARY.next.md` until step 1 | Motet refines it |
| `ASKING_MOTET.md` | The block every project's agents get (part 6) | Changes like any project file, on the owner's yes |
| `skills/` | Eighteen skills (see below) | Motet refines and adds skills |
| `PROJECTS.md` and `projects/` (planned; not yet a template in `motet/`) | The owner's projects, roles and teams | The owner corrects each project's details at their own pace; Motet keeps team lists current |

**Skills.** Five are kept from today: shift report, handover, stuck agent, conflict check and watch rule. Thirteen are
new: morning brief, project sweep, end of day, kick off a goal, next step after a turn, review loop, ready for your yes,
close out a goal, unstick an agent, something went wrong, helping an agent, waiting on you, and wake and deliver.

**Projects** (planned; not yet a template). One list of the owner's projects in `PROJECTS.md`, and one file per
project in `projects/`. Each project
uses the same fixed set of seven roles. A project can be marked less active.

Every cycle the engine reads `SOUL.md` and `RULES.md` (with `IDENTITY.md`), so all three stay short.
`VOCABULARY.md` and the skills are read when needed.

**Where it lives.** The owner's private self is in `~/.live-room/motet-self/`, outside the repository; Motet's
database holds the agenda and the record. `motet/` is the template that ships to other people. `config/` holds engine
settings (`MODEL_POLICY.md`, `PRICING.json`), which are not part of the self.

**Change history.** Every self file is versioned, including the owner's own edits; `scripts/motet_self.py` gives the
owner `init`, `scan`, `history`, `show` and `restore` (see [Motet](MOTET.md)). Motet and the engine get no direct
write access: changes go through Motet's own tools.

**Refining itself** ships at step 1, with the switch from `INSTRUCTIONS.md` to `SOUL.md`, `IDENTITY.md` and
`RULES.md`. With it comes a "built so far" note that each builder updates, so Motet never promises a power that isn't
built.

**Backups.**

- A clean daily copy of the folder and database: 7 daily and 4 weekly.
- On a Mac, Time Machine carries them off the computer, with a warning in the brief if it is off (Mac only in version
  1; on Windows and Linux the copies are kept and the carry-off check is not decided yet).
- Nothing goes to the cloud unless the owner turns on an encrypted second copy.
- "Go back to yesterday's copy" at Motet's desk, tested once before it is relied on.

#### Engines

The owner picks the engine in today's "Choose model" control at Motet's desk; the engine list replaces the model list.
The choice stays until changed and covers what Motet does on its own. The choices: Anthropic's paid service, as today;
a Claude Code or a Codex session on the owner's own subscription, with the model the owner picks; and later
Antigravity, if a way to message it is found.

Motet starts and owns each engine's session in the background, never as one of the owner's chats. Engines reach
Motet's tools and self through MCP, change the self only through those tools, and keep nothing of their own. Only one
engine acts at a time.

- Switching never loses a promise, a memory or a half-finished cycle. The new engine reads the end of the recent
  conversation and Motet's working memory; a returning engine never resumes its old context and gets a new generation.
- **Engines never appear on the stage.** Motet shows only as Motet. An engine's session is never drawn, counted or
  watched as one of the owner's agents, and never shows a Stop control.
- **Switching rests the old engine; it does not stop it.** It goes idle and stays ready. An idle engine does nothing,
  and nothing it sends can land. Its processes are cleaned up only when Motet quits or the engine is removed.

**Two process models.** What "ready" means, and so how resting works, depends on how an engine runs:

| | One-shot runs (interim for version 1) | A long-lived session (not decided yet) |
| --- | --- | --- |
| How it runs | One `claude -p` or `codex exec` run per cycle, standard input closed | One process kept running, driven through a control channel: Claude's stream-json over standard input, or the Codex app-server |
| "Ready" | Signed in, binary verified, nothing running between cycles | A warm process waiting for its next cycle |
| A turn running at a switch | Runs to its end; its later output is refused by the fence and logged, and its usage recorded | Interrupted through the control channel, never by a signal to the process group; the process is kept |
| Switching back | A fresh run | The kept process, after it rereads the self |
| Acceptance | A run's output is refused once a newer generation exists; nothing runs between cycles | Interrupt works on every system; a kept process takes no action while idle |

Until the choice is made, builders implement one-shot runs.

- **A hung engine.** A run past its time limit is treated as "can't run", its process is left alone, and Next up offers
  "End it" as the owner's one-click choice. Clean-up when Motet quits still covers it.
- The generation fence and "only the core dispatcher sends" carry the safety that ending old processes would give.
- Tests: a late tool call from the old engine is refused and logged; switching A to B to A; two switches in one cycle.

**When no engine can run,** Motet stops and tells the owner; it never drops to the paid service or another engine.
The notice comes from code: one Next up line saying which engine can't run and why. Without an engine, the owner's
presses, what code alone can show, Wake and deliver and a subroutine's fixed message still work; anything a
subroutine must compose, such as a shift report, waits; every cycle Motet would start itself stops, and the owner's
questions to Motet get the same code-written line. Tests confirm no call to the paid service or another engine.

**Isolation, for both engines:**

- only Motet's MCP server; no built-in shell, file-edit or web tools, which is what makes "never writes files" true;
- no user hooks or plugins, Motet's own included, so an engine never reaches the approval stream;
- a neutral working folder outside every project, so no `CLAUDE.md` or `AGENTS.md` loads;
- a stated permission posture: for Claude Code 2.1.323, `--permission-prompts none`; for Codex, approval policy
  "never" and a read-only sandbox;
- the offered tool list checked against the expected list at every start.

**Provider terms.** The providers' published terms apply to the owner's own use too. A recheck of the published text,
with no legal opinion, is an entry condition for building engines. No text reviewed so far permits a subscription to
serve people other than its owner.

**What an investigation found.** Background Claude Code and Codex sessions reached a private MCP server and called
Motet-style tools (about 9 seconds per call for a small Claude model, 19 for Codex). Isolation, allowance and cost
stay provisional until the test is rerun under the harness. Allowance running out mid-cycle is expected to be the
most common stop.

**The engine harness** adds:

- a dedicated sign-in per engine, made by the owner through the provider's own sign-in command, in that engine's own
  `CLAUDE_CONFIG_DIR` or `CODEX_HOME` inside Motet's private folder. Motet never reads, copies or reuses the owner's
  login, token, Keychain entry or auth file;
- the same pin managed Claude uses: an exact version and its sha256 hash at a fixed path. Claude's `--bare` mode does
  not use subscription sign-in, so it is not the launcher. For Codex, Motet records the version and hash of the binary
  the owner signs in with, and checks both at every start;
- a time limit, and clean-up of the whole process tree, including the engine's MCP child;
- watcher exclusion by the engine folders and the session ids the core registers;
- provider-reported usage recorded every cycle; rules and permission checks in the core, outside the engine's reach.

| | Mac and Linux | Windows |
| --- | --- | --- |
| Process tree | Its own session and process group, ended as a whole. A supervisor that dies leaves the engine running, so Motet sweeps at start-up for engines that outlived a crash, by registered pid and start time, and ends them | Reuse `claude_managed_windows`'s start: suspended, assigned to a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resumed, so no child escapes; `stop_tree` ends it. No detached supervisor for engines: the Motet server itself holds the engine job's only handle, so a crash of Motet ends the engine. The start-up sweep runs on Windows too, as a second line of defence |
| Standard input (one-shot) | Closed (`/dev/null`) | Closed (`NUL`) |
| Executable | Pinned exact version and hash | Claude Code 2.1.323 `.exe` at the native installer's location only; never a shim, an App Execution Alias, the desktop app's copy or a path through a link. **No Codex engine on Windows in version 1**: its only `codex.exe` sits inside WindowsApps and runs only through the app alias |

### 9. Wake and deliver

When the bridge refuses an agent's message because it can't verify it can reach the receiving chat ("does not have
verified transport readiness"), Motet delivers it itself, **fully automatically, with no approval**. For this one case,
this lifts the earlier rule that only the owner's own Send may go into such a chat.

| The receiving chat | What Motet does | Still to confirm |
| --- | --- | --- |
| Claude, open and set up for Motet | Nothing to wake: the plugin's 2-second heartbeat keeps it reachable | — |
| Claude, open but never set up for Motet | The side door: the Claude app's own per-chat mailbox, only while the owner's agent messaging switch is on. The chat reads it as from another session. No receipt, so arrival and the reply come from the transcript. Mac only in version 1; elsewhere, hold and ask | Shown to wake a running chat. The raw transcript labels and `from=` sender label were not recorded: evidence gaps to close before step 4 |
| Claude, closed (no running process) | Reopen that same chat and deliver. If it can't be reopened, hold and ask the owner to open it (in Next up). Never a fresh chat | Can Motet reopen a closed chat, and does it then receive? |
| Codex | "Unverified" only means the watcher hasn't seen the log change for about two hours. Re-check the exact chat and that the app is running, deliver once through the bridge's Codex route; if that fails, reopen and try once more; then hold and ask. Never a fresh chat | Does the Codex route reach a chat idle for hours, before and after opening it? |
| Anything else (Cursor, Antigravity, cloud sessions, a chat the owner stopped or archived, a chat ended by Stop, a chat stopped under Unstick until the owner answers "carry on") | No wake. Hold and tell the sender | — |

**Limits, built into the code:**

- only the exact chat the sender named, on Motet's own agent list and the owner's machines;
- only a message an agent already sent and the bridge refused; never anything of Motet's own;
- the same message under its **original request id and ledger**, never a new record or a side channel around
  `capacity_error`; unknown means hold;
- delivered exactly as written, once, marked as from the sending agent via Motet, carrying no approval;
- logged in the Deck Log before it happens (who, which chat, when, the message), with whether the agent started;
- caps: one wake per message, each chat at most 3 times an hour, and at most 20 wakes a day (starting numbers, to be
  calibrated). Past a cap the message waits and goes in the end-of-day report;
- inside the daily cap;
- no workarounds: never a fresh chat or any route around the bridge. A repeated or unexplained failure goes to
  "something went wrong" as a root-cause fix request to the agent who owns the bridge.

**Reporting and which chats may be woken.** Wakes are reported in the end-of-day report and the Deck Log, with a live
nudge only when a wake fails and a team is blocked. A chat the owner explicitly stopped or archived stays asleep. A
chat on hold, or one the app unloaded, may be woken.

#### Stop and waking

- A chat Motet stopped under Unstick stays stopped until the owner answers Motet's "how do you want to go on?". If the
  owner answers "carry on", Motet may then wake it and hand over the messages that waited, through Wake and deliver.
- A chat ended by Stop, for a chat started from New chat, is final in version 1: never woken, never reopened.

What "stop" means for each kind of chat: for a chat Motet started through New chat, Stop ends its process. For the
owner's own Claude Desktop and Codex chats, no supported way for Motet to stop one is established; until one is,
Unstick for those chats goes straight to asking the owner, with the handover and "Open desk".

In version 1 the "carry on, then wake it" rule has nothing to apply to yet: Motet can stop only a chat it started, and
that stop is the final Stop, because interrupting a managed chat is not supported; and it cannot stop the owner's own
chats at all. So Unstick writes the handover and asks the owner how to go on. The rule takes effect once an interrupt
route exists for chats Motet started.

**Build prerequisite.** Today a refused message is not stored. Build step 4 needs the bridge to keep a refused message
with its original record, so Wake and deliver can recover it honestly.

#### Arrival rules

- **Positive evidence proves arrival; absence never disproves it.** Admission, dispatch, app acceptance, native
  start, native end and answer capture are separate stages, each with its own evidence.
- **Sending once.** Positive no-send evidence allows the one original dispatch; otherwise Motet reconciles or holds.
  Remaining duplicate risks: the owner's Clear while execution is unknown; a Claude pre-start drop when the host
  started anyway; the owner's own non-peer sends; any route outside the ledger.
- **The side door** reaches only a running chat, which sees a peer message, not the owner's typing; its sender label
  is claimed, not verified. A production adapter needs consent checks, original-ledger dispatch and separate evidence
  for any plugin reconnection.
- **Reopening** targets known history only: not a wake, a resend, an unarchive or process recovery, and the owner's
  stop and archive veto holds. For Claude, an exact-id `--resume` in a new terminal is the candidate, never "latest".
  Opening a Codex chat is a hand-off, not a receipt.
- **One shared principle:** every route that sends reuses the original request id, admission and ledger, so the same
  instruction never goes by two routes.

#### New chats and reopening

- Opening a visible draft, starting work, and opening an existing chat are separate actions; a draft is named as a
  draft. On Claude Desktop, a visible draft is supported, but work sent with an exact new-chat receipt is not.
- A new chat is admitted only by its exact native identity, never by title, time, folder or prompt coincidence.
- A visible terminal start needs a pinned, verified executable, a checked folder, a chosen session id and structured
  arguments, with rules for prompts starting with `-`, `/`, `!` or `@`. On Windows, a free-text first instruction
  can't ride the terminal's command line.
- `claude --bg` starts a background session and prints its id. The Desktop app runs its own copy of Claude Code, so
  no pinned version is possible there; a middle option is a draft, the owner's Send, then the owner confirming which
  chat it was. macOS asks once for Automation consent; a new folder's trust dialog is an expected stop. Codex's
  `?prompt=` on a thread link is undocumented.

## What changes from today's rules when this is built

Today's rules, in the [roadmap](ROADMAP.md#rules-that-carry-forward), stay true until the core ships. Each change
below takes effect only with the build step that brings it, after review and the owner's yes to merge:

- **"A conversation, log entry, or peer message cannot authorize new actions."** With step 4, a yes the owner types in
  an agent's own chat counts for Motet too ("Where a yes counts"). Peer messages, Motet's own deliveries, automatic
  text and anything else traced to another source still never authorize anything.
- **"Messaging and controls require an explicit owner request."** With steps 2 and 4, Motet sends some messages on its
  own, within its permission table: next tasks, approval of a plan that stays within agreed work, review and fix
  requests, answers to help requests, a subroutine's fixed message, and Wake and deliver. Merging, deploying, releasing and permission changes stay with an agent acting
  on the owner's yes; Motet never does them.
- **Standing requests**, listed in the roadmap as not designed, become subroutines (part 7) with step 3.

Unchanged: the owner stays in charge; a chat ended by Stop is final in version 1; answering an agent's own permission
prompt stays the owner's.

## What version 1 deliberately sidesteps

- **Lost replies.** A reply can get lost if the owner types into that chat mid-task or Claude's plugin reloads. Motet
  never treats a missing reply as an answer; it checks, and asks the agent if it can't tell.
- **Apps that can't be messaged.** For Cursor, Antigravity and cloud sessions, Motet watches and suggests; the owner
  acts.
- **One computer, one Motet.** It runs only while the computer is awake, and two Motet servers must never both act;
  the agenda store needs the same single-writer protection as `MessagingStore`.

## The daily cap

The daily cap limits Motet's own thinking each day. Its unit (thinking cycles or money) and its number are not
decided yet. A replay of one real day found 64 candidate wake-ups in 58 five-minute batches. At a small model's price a
cycle cost about $0.014: about $0.14 a day for 10 cycles, $0.90 for 64 and $1.55 for about 110, and about three times
that for larger cycles. A proposed starting rule: count cycles, 60 a day and at most 12 an hour, set after a shadow
week at about 1.5 times the busiest shadow day.

Whatever the number: what code alone can show never uses the cap; the owner's own presses still send; at the cap
Motet's self-started cycles stop, with one line in Next up, until the owner says yes; and nothing switches to paid.

## Build order

Templates first, then Engines, then steps 1 to 5. Every step must work the same on Mac, Windows and Linux, except
what is limited in version 1: Mac only for the Time Machine carry-off and the side door; Mac and Linux only for Codex
engines and the bridge's Codex route in Wake and deliver (Codex messaging on Windows is still being researched). Each
step gets independent review by the other provider, and tests for sleep and wake, restart, duplicate wake-ups and the
daily cap. Results come from the test runner's pass and fail counts, never from an exit code alone.

**What sends, from which step.** Step 1 sends nothing: confirmed goals are only recorded, and the brief's spending
warning waits for the daily cap. Step 2 sends approved proposals, answer presses and help answers; a kick-off for a
confirmed goal is a Next up proposal. Step 4 turns on everything Motet does on its own: kick-offs, the 10 a.m. start
of suggested goals, passing work along, and Wake and deliver.

**Templates (in place).** Self templates in `motet/`, brought up to the agreed design; settings in `config/`; the
private self folder with its change history; and `scripts/motet_self.py`. No behaviour change: Motet still answers
from `INSTRUCTIONS.md`. The owner's private copies must match the templates before step 1 switches Motet over.

**Engines.** Entry conditions: the provider-terms recheck; the owner's dedicated sign-in per engine, through the
provider's own sign-in; on Windows, the owner's native install of Claude Code; then the engine investigation rerun on
those sign-ins. The rerun passes with Claude Code 2.1.323 from a fixed path; Codex apps and plugins off; at least 5
production-size cycles per engine; usage as a share of the plan from each provider's own readings; write-back safe
under a forced kill and invented operation ids; no copy of any login held anywhere; and no access to the owner's login paths (the Keychain entry,
`~/.codex/auth.json`, the Claude credentials).

The step builds the harness, the engine choice, the rest-not-stop switch and watcher exclusion. Acceptance: switching
behaves as in part 8; "can't run" means stop and tell, with zero paid calls; no engine appears on the stage or in any
count; quitting or removing an engine leaves no engine process, and none survives a crash past Motet's next start;
the tool list is checked at each start; usage is recorded every cycle.

**1. Agenda and morning brief, read-only.** The goal and promise store, the "you came back" wake-up and the brief.
Nothing is sent. Also the switch from `INSTRUCTIONS.md` to `SOUL.md`, `IDENTITY.md` and `RULES.md`, Motet refining
its own files, and the "built so far" note. Acceptance: the brief uses only reliable signals, labelled honestly; the
owner-activity record ignores hook traffic; the switch happens behind the "built so far" note; every write is
repeat-safe.

**2. Heartbeat, one-click proposals and help requests.** Event wake-ups, the cycle, Next up with its count and answer
buttons, the approval path, the daily cap, and `ask_motet` with "can't reach an agent", "did it arrive, has it
started" and `item_status`. Needs: the daily cap's unit and number; and what reaches the phone and what interrupts.

**3. Follow-ups, stalls and subroutines.** Scheduled follow-ups, subroutines including housekeeping, quiet-agent
detection, combined escalations, and "I stepped in; re-sync".

**4. Actions on its own.** Next tasks, passing work along, status, review and fix requests, fixing what broke, and
Wake and deliver. Needs: refused messages kept with their original record; the reopen tests and the Codex idle-chat
test passed; the side-door evidence gaps closed; and the fix-chat reading confirmed. Native delivery tests are part
of this step's testing.

**5. Agent reports.** Prove which agent sent a message, then add "ready for review" and "progress" to `ask_motet`.

## Not decided yet

**Before Engines or step 1:**

1. The final look of the engine picker (its place and "stop and tell" are decided).
2. The engine process model: one-shot runs or a long-lived session, which decides what "ready" means for an idle
   engine; and which models each subscription offers. Interim: one-shot runs.

**Before step 2 or later:**

3. The daily cap's unit and number, for example 60 thinking cycles a day and 12 an hour after a shadow week.
4. While the owner is away, should only the right-away wake-ups reach their phone?
5. What interrupts the owner: a notification, or only Next up? A provisional investigation suggests generic notices
    ("Motet needs your attention") that open a private, authenticated phone view with its own revocable credential.
6. Should an agent's "can't reach" or "needs the owner" request interrupt the owner at once, or wait in Next up?
7. Whether a subscription may run as an engine for people other than its owner (matters only for the shipped
    template).

**Still open in the design:**

8. How a proposal shows that its evidence has since gone, for example a log that expired.
9. How goals and promises appear in Memory's search ("what did we decide yesterday?").
10. How Motet stops one of the owner's own Claude Desktop or Codex chats, if at all (part 9).
11. Windows and Linux: the carry-off check for backups, and any equivalent of the side door.
12. Whether housekeeping may ever remove a Memory entry before its set time runs out.
