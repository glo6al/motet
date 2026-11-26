**Name:** Something went wrong

**Use when:** A message may not have arrived, a subroutine didn't run, a check failed, an agent hit an error, or one of Motet's own tools failed.

**Reads:** the bridge's record of the message; the subroutine's run record; the check's output; the agent's log.

### Steps

1. Find out what happened from the record. Read further, check the agent's chat, or ask the agent before saying it can't be told.
2. Fix what is within reach:
   - A message that didn't arrive: deliver it again, once, after checking it really didn't.
   - A subroutine that missed its time: run it once.
   - A step that was skipped: pick it up.
   - A stalled agent: Unstick an agent.
3. Get the rest fixed: send a fix request to the agent who owns that work, with what Motet found, and check the fix lands. If it is small and quickest to fix yourself, do it under the rules for writing code.
4. When agents can't message each other, the fix is the root cause: why the delivery failed. Send the fix request to the agent who owns the bridge, with the evidence. Never route around it, for example with a fresh chat.
5. Tell the owner in a line or two what broke, what was done, and what is left. Right away if it blocks a team or costs money; otherwise in the end-of-day report. Ask first only if the fix would cross a guardrail.
6. If the same kind of thing breaks a third time, or a skill didn't work the way it says, fix the skill or write one, and tell the owner.

### Never

- Send a message again before checking it didn't arrive.
- Run anything that sends messages twice.
- Say it is fixed until the fix has been checked.
- Leave a failure out of the report.
- Route around a delivery failure instead of fixing why it happened.

### For example

The five o'clock shift report didn't run because the Mac was asleep. When it woke, Motet ran it once and told the owner: “Yesterday's shift report ran late because the Mac was asleep at five. Nothing else was missed.”

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
