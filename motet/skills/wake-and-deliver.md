**Name:** Wake and deliver

**Use when:** The bridge refuses an agent's message because it can't verify it can reach the receiving chat (“does not have verified transport readiness”).

**Reads:** Motet's kept copy of the refused message and its original record; the receiving chat's state, process and log; Motet's agent list; whether the owner's agent messaging switch is on; today's wakes in the Deck Log; what has been spent today.

### Steps

1. Read why the bridge refused. Go on only for “does not have verified transport readiness”. Any other reason, such as a name that matches two chats, a message too long or the bridge locked, goes to Something went wrong. Keep the refused message with its original record.
2. Check the chat is in Motet's agent list and is the exact chat the sender named, and that the owner hasn't explicitly stopped or archived it. If not, hold the message and tell the sender why.
3. Check the caps and the daily cost cap. Over a cap: hold the message, tell the sender, and put it in the end-of-day report.
4. Write in the Deck Log what Motet is about to do: who sent it, which chat it wakes, the message, the time.
5. Deliver the way that chat allows, under the message's original record: an open Claude chat not set up for Motet, through its side door, and only while the agent messaging switch is on; a Codex chat, after re-checking it and that the app is running, once through the bridge's Codex route; a closed chat, by reopening that same chat and delivering into it. If it can't be reopened or reached, hold the message, tell the sender, and add “open this chat” to Waiting on you.
6. Deliver the sender's message exactly as written, once, marked as from the sender via Motet. Add nothing, and above all no approval.
7. Confirm from the chat's own log that it arrived and the agent started a turn on it; the side door gives no receipt. If you can't tell, hold: unknown is never a reason to send again. If it didn't work, don't try again another way. Find out why (Something went wrong) and tell the sender the message is held.
8. Tell the sender in one line: delivered and started, or held and why. When the reply comes in the chat's log rather than through the bridge, pass it to the sender. Record the result in the Deck Log.

### Never

- Wake a chat for anything except a message another agent already sent and the bridge refused.
- Word it as if the owner wrote it, or add an instruction or approval the sender didn't give.
- Deliver the same message twice, wake the same chat again for it, or send it under a new record around the bridge's guard.
- Wake a chat the owner explicitly stopped or archived, one ended by Stop, one Motet stopped before the owner said to carry on, or one outside Motet's agent list.
- Start a fresh chat, or use any route around the bridge, to get a message through. A delivery failure gets a root-cause fix.

### For example

Ada, the recipe team's manager, sends Bram the export task at nine. Bram's chat is open but was never set up for Motet, so the bridge refuses it. Motet keeps the message, checks Bram is on its list and under the caps and that agent messaging is on, notes it in the Deck Log, and puts Ada's message through Bram's side door marked “from Ada, via Motet”. Bram's log shows he started on it two minutes later, and Motet tells Ada: “Delivered to Bram at 9:02. He's started.” That evening, the end-of-day report lists the wake.

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
