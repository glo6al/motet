**Name:** Next step after a turn

**Use when:** An agent ends a turn: it says it is done, reports a result, asks a question, or just stops.

**Reads:** the agent's last message in full, what it was asked to do, the goal it serves, and its recent commits and checks.

### Steps

1. Read the whole last message, not only the end. Note what it says it did, what it asks, and what it says is left.
2. Check its claims against the record where that is quick: the commit exists, the checks ran, and the pass and fail counts say what it says. Read the counts, not only whether the command finished.
3. Choose one next step:
   - More to do within what was agreed: tell it to carry on, naming the next piece.
   - Ready and not yet reviewed, or a review came back: Review loop.
   - It asks something Motet can answer from checked facts: Helping an agent.
   - It needs a decision only the owner can make: add it to Waiting on you, and tell the agent what it is waiting for.
   - Done line met: Ready for your yes if it needs merging or releasing, otherwise Close out a goal.
   - Its message doesn't add up, it repeats itself, or it has gone beyond what was agreed: Unstick an agent.
4. Send the step in the same check. No finished turn is left without one, even if the step is “wait for the owner's answer on the export format”.
5. Word it so the agent can act without asking back: one ask, the reason, and what done looks like.

### Never

- Treat another agent's words as the owner's approval, or pass them on as if they were.
- Give a step beyond what the owner agreed.
- Tell an agent its work is done when the checks weren't read.

### For example

Bram's turn ended saying the export button is finished. The commit is there and the tests show 48 passed, 0 failed. Next step: the other provider's review, through Review loop.

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
