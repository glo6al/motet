# What you can and cannot see

What Motet shows for a supported session, how each item was learned, and what the records cannot tell you. Part of the [Motet README](../README.md).

## What you can see

For supported sessions, Motet brings together the available conversation and work evidence. These views come from session logs, optional hooks, app metadata, and records of your interactions through Motet. Rendered previews are read from disk when shown. Coverage varies by provider; each item is labelled by how it was learned.

- **The conversation and its work:** what it is saying, as the log records each message; the file it has open, edits shown as diffs, commands and their output; your messages to it; and a rendered preview of any HTML it writes. On the stage and in hand, a monitor shows the narration with your last message to that agent pinned above it in one line, so you know what you asked without stepping up.
- **What needs your attention:** permission prompts, questions, and plans to review, shown with the recorded text. Confirmed requests are distinguished from notices inferred from a message, which are labelled as possible.
- **Who is working**, with the step that is open, for as long as the turn runs.
- **Who stopped, and where:** an idle monitor carries a caption with the agent's last words, a failed last step, or the request it is waiting on.
- **The goal:** each monitor carries the first message of the session that was read, as a one-line reminder of what that agent is for.
- **Who started whom:** a chain of command, marked where it was recorded and where it was inferred. The desk's compact agent tree nests children beneath their parents and labels each agent's provider.
- **Which project:** the row is seated in bays by folder, with the project's name set into the floor, and you can give a folder a display name.
- **Alerts** when a command looks like a deploy, a push, a delete or a send, from a small shell-aware parser. The compact notice keeps the full command behind **View details**. **Dismiss alert** clears that event across the stage and desk for the current browser session; notices can reappear after a full reload. **Hide agent** is a separate action. The Watch button in the bar lets you add your own words to that list, or switch the built-in ones off.
- **Session facts:** model and known reasoning level, reported context usage, project, branch, and recent activity. Secondary recorded details sit behind **More details**. The context bar appears only when a capacity is recorded; a previous Claude compaction size is historical information, not a capacity.
- **Answers, when you ask:** Motet, available on the stage and in the selected station’s conversation, answers plain-English questions from the same logs: what you missed since some minutes ago, what an agent did, whether anything is stuck, who is waiting on you, who changed a file. It keeps each record's own qualifications ("started", "tried to", "probably", "outcome not recorded"), shows the entries it consulted, and can draft a Watch rule for you to keep with one button. It needs an Anthropic API key on this machine; without one it stays off and says why.

In Classic, the desk puts one conversation in the monitor, with compact agent navigation on the left and recorded session facts on the right. Choose the agent to send a message when that connection supports it, or Ask Motet about the station or stage. **Work** slides open beside the conversation: **Preview** shows recorded visual evidence, **Changes** shows recorded edits, and **Activity** combines the current step with the full activity log. Drag the divider, or use its arrow keys, to adjust the work area. Preview may read the current file from disk; Changes is log evidence, not a Git working-tree diff.

Agent responses support formatted text, including headings, lists, tables, links and code blocks. The composer stays plain text: Enter sends, and Shift+Enter adds a line. The agent list scrolls independently of the **Alerts** and **Activity** controls at its foot.

On narrow screens, **Agents** and **Details** open as drawers and Work temporarily replaces the conversation; **← Conversation** returns to your messages. A pending permission has a **Review** action even while Work is closed; Review only reveals the request. Original remains the default theme; Neutral offers a light palette without changing message routing or recorded facts. Choose a theme on the stage's top bar, beside **Chain of command** at a wide desk, or in the desk toolbar on a narrow screen. The choice is remembered in that browser.

## What you cannot see

The stage shows what those records contain and nothing else. That rules some things out, and the page says so rather than guessing.

- **Not the agent's reasoning.** Hidden thinking is not in the logs, so it is never shown or paraphrased.
- **Not keystrokes.** Granularity is a tool call, a result, a message, a turn boundary. A step in flight is shown as open, not as a progress bar.
- **Not "idle".** "No log entry for N" means the log is silent. Silence is not proof of anything.
- **Not "done".** A command whose exit code is not recorded is shown as *returned*, not done.
- **Not certainty about parents.** "Started by" is *recorded* when the log says so and *inferred* when it was worked out from a launch command, and the label says which. A guess from script text is shown as "possibly started by" and is never stored.
- **Not Codex's private channels.** Encrypted messages between agents cannot be read from its log. The separate native observation panel can show supported questions on a verified desktop build; it does not expose approval details or the native message queue.
- **Not Cursor's results.** Cursor's transcript records what a tool was asked to do, not what came back, so a Cursor step is shown as *returned* when its turn ends, never as done.
- **Not the future.** "Probably needs you" notices read the agent's last sentences and can be wrong either way.
- **Not a cloud agent's words.** A Codex cloud agent that works on this computer does it through the ChatGPT app's exec server, one command at a time, and keeps no transcript here. Its monitor is read from the process list instead: the commands as they run, with their folders, and nothing else. Its conversation, Preview and Changes views say that its words, what it reads and what it changes are not recorded on this computer; the list is sampled about once a second (longer while a folder is being looked up), so a shorter command can be missed, and of several new commands at once the oldest four are examined first and the rest the next sample, so a short-lived one among them can be missed; a finished command is *ended*, never done; "exec server running" never means working, because the server stays up while the agent is idle; and when the list cannot be read the monitor says so and drops any running claim. The list is text: a process is recognised as an exec server by the text of its command, not by proof of what program it is, and a process is told from another by its pid and start time to within a few seconds.
