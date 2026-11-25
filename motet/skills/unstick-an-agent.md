**Name:** Unstick an agent

**Use when:** An agent is quiet past the owner's limit, repeats itself, says something that doesn't add up, or is working beyond what the owner agreed; or another skill sends it here.

**Reads:** the Stuck agent check; the agent's log and last message; what it was asked to do; its project manager.

### Steps

1. Run the Stuck agent check. Tell apart: waiting on a permission prompt (only the owner can answer it: add it to Waiting on you); waiting on another agent (check the message arrived, and pass it on if not); waiting on the owner; working (a long command still running); stuck (no progress, or repeating); and working beyond what was agreed.
2. If the record can't tell, ask the agent once where it stands: what it is doing, what is left, and what it is waiting for.
3. If the project has a manager, and the manager isn't the one stuck, tell the manager and let it act first. Step in yourself when the manager is stuck too, or hasn't acted by the next check.
4. Nudge: remind the agent what it was asked and what is left, and pass on any message that went unanswered.
5. If it is still stuck, keeps repeating, or is working beyond what was agreed: write down where it was (branch, last commit, what is done, what is left) with the Handover skill, stop it, and start a fresh chat from that handover. Tell the owner each time, and why.
6. If it is stuck on something small in the code, fix it yourself: your own branch, the project's rules, the other provider's review. Tell the agent that owns the work what you changed, so no one edits the same file twice.
7. At the next check, make sure the agent, or the fresh chat, is moving again.

### Never

- Call an agent stuck from silence alone.
- Answer an agent's own permission prompt.
- Stop an agent for any reason other than those in RULES.md.
- Change the code that enforces Motet's own permissions, cost cap or rules.

### For example

Bram has been quiet for forty minutes. His log shows the same failing test run three times with no change in between. Motet tells the recipe manager, who doesn't act by the next check. Motet writes down where Bram was, stops him, starts a fresh chat with that handover, and tells the owner: “Bram was repeating the same failing test, so I restarted him from his last commit.”

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
