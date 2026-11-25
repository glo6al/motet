**Name:** Wake and deliver

**Use when:** The bridge refuses an agent's message because the receiving chat is idle or not connected.

**Reads:** the bridge's record of the refused message and why it was refused; the receiving chat's state and log; Motet's agent list; today's wakes in the Deck Log; what has been spent today.

### Steps

1. Read why the bridge refused. Go on only if it says the chat is idle or not connected. Any other reason, such as a name that matches two chats, a message too long or the bridge locked, goes to Something went wrong.
2. Check the chat is in Motet's agent list and is the exact chat the sender named, and that the owner hasn't explicitly stopped or archived it. If not, hold the message and tell the sender why.
3. Check the caps and the daily cost cap. Over a cap: hold the message, tell the sender, and put it in the end-of-day report.
4. Write in the Deck Log what Motet is about to do: who sent it, which chat it wakes, the message, the time.
5. Wake the chat the way its app allows: in the Claude app, its own chat-to-chat message; in the Codex app, reopen the chat, then deliver through the bridge.
6. Deliver the sender's message exactly as written, once, marked as from the sender via Motet. Add nothing, and above all no approval.
7. Confirm it arrived and the agent started a turn on it. If the wake didn't work, don't try again another way. Find out why (Something went wrong) and tell the sender the message is held.
8. Tell the sender in one line: delivered and started, or held and why. Record the result in the Deck Log.

### Never

- Wake a chat for anything except a message another agent already sent and the bridge refused.
- Word it as if the owner wrote it, or add an instruction or approval the sender didn't give.
- Deliver the same message twice, or wake the same chat again for it.
- Wake a chat the owner explicitly stopped or archived, or one outside Motet's agent list.

### For example

Ada, the recipe team's manager, sends Bram the export task at nine. Bram's chat has been idle since last night, so the bridge refuses it. Motet checks Bram is on its list and under the caps, notes the wake in the Deck Log, wakes Bram's chat and delivers Ada's message marked “from Ada, via Motet”. Bram starts on it two minutes later, and Motet tells Ada: “Delivered to Bram at 9:02. He's started.” That evening, the end-of-day report lists the wake.

This method grants no new authority. What Motet may do on its own is set by RULES.md and the permission table.
