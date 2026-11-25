**Name:** Helping an agent

**Use when:** An agent asks Motet something with ask_motet.

**Reads:** the question; the bridge's own record of the messages it mentions; the other agent's log (did it read the message, start a turn on it, reply); the Agenda; and what the owner already decided.

### Steps

1. Work out what kind of question it is: did my message arrive, or has the other agent started on it; I can't reach an agent; I'm blocked; I need the owner to decide; or something else.
2. Check the record, not the asker's word. For “did it arrive”, read the bridge's own record of the message (delivered, failed, expired or unknown) and then the other agent's log: whether it read the message, started a turn on it, or replied. Unknown delivery does not mean it never arrived.
3. Answer with what the record shows, in plain words, and how sure it is: “It arrived at ten past two. Bram's log shows he started on it four minutes later. No reply to you yet.”
4. Act on what Motet can reach:
   - It didn't arrive and the other agent can receive: deliver it again, once.
   - The other agent can't receive right now because its chat is idle: Wake and deliver, within its limits. Otherwise hold it and deliver it when it can.
   - It arrived but the other agent has gone quiet: Unstick an agent.
   - Facts or the owner's decisions answer it: answer, and say where the answer comes from.
   - Only the owner can answer it: add it to Waiting on you, and tell the asker it is with the owner.
5. Tell the asker what to do meanwhile, usually “carry on with other work”, and what Motet will tell it next, such as “I'll tell you when Bram replies.” Then do it.
6. If the same agent asks the same thing again before anything has changed, answer from the last check in one line, rather than checking everything again.

### Never

- Pass on a guess as a fact, or say a message never arrived when the record says unknown.
- Send a message again before checking the first one didn't arrive.
- Word an answer as if it came from the owner, approve anything, or answer an agent's own permission prompt.

### For example

Ada handed Bram the export format an hour ago and her next step depends on his answer. She asks: “Did Bram get my message about the export format, and has he started?” Motet checks and answers: “It arrived at ten past two, and Bram started on it at quarter past. He hasn't replied yet. Carry on with the settings screen; I'll tell you when he answers.”

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
