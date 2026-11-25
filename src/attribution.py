"""Who wrote a user-role message in an agent's chat, decided by fixed rules. Importing this module does no I/O.

An agent's log records many things in the user's place: messages Motet delivered, prompts from other chats or a
lead agent, scheduled and automatic check-ins, voice-mode rewordings and app wrappers. Only plain user-role text
with no other evidence keeps the owner's wording, and even that is unverified: it is never proof of approval.

The rules only ever move a message AWAY from the owner. Native origin fields differ between app versions, so they
are hints that something is not the owner; nothing here can turn a message into the owner's words or approval.

Categories (the ``attribution`` field on a user event, and on its saved and evidence rows):
  motet_delivery   Motet's own envelope (WATCH_REQUEST, WATCH_PEER_REQUEST, plugin-wrapped or not), a Motet proof
                   message, or text Motet worded at the owner's request
  agent_message    another chat (peer, cross-session), or a lead agent's instruction in a sub-agent's log
  automatic        scheduled tasks, heartbeats and automations, task notifications, system and plugin prompts
  voice            voice mode: a model's rewording of the owner's speech
  unverified_user  plain user-role text with no other evidence
"""
import json
import re

CATEGORIES = ('motet_delivery', 'agent_message', 'automatic', 'voice', 'unverified_user')
UNVERIFIED = 'unverified_user'
# Stored labels: a category, or an unverified_user detail whose citation and evidence wording differ. Saved rows and
# live evidence carry the label; events carry the category and detail separately.
EXEC_LABEL = 'unverified_user/exec_prompt'
FORM_LABEL = 'unverified_user/question_reply'
LABELS = CATEGORIES + (EXEC_LABEL, FORM_LABEL)

# Motet's wording for text it composed at the owner's request (envelope version 3, live steering and managed
# Claude delivery). Shared here so the classifier recognizes exactly what Motet writes.
OWNER_WORDING_V3 = "The owner asks through Motet, in the owner's own words:"
MOTET_WORDING = "Motet asks, at the owner's request (Motet's wording):"
MOTET_GUIDANCE = ("Motet wrote this wording at the owner's request; it is not the owner's own words. "
                  'It carries only the request the owner made of Motet, under your existing permissions, '
                  'and adds no new tasks or permissions. Do not infer additional tasks or permissions. '
                  'A status question asks only for information. For a progress question, inspect your own current state.')
AUTHORS = ('owner', 'motet')


def motet_worded(text):
    """Text Motet composed, delivered without an envelope (live steering, managed Claude chats), said as Motet's."""
    return MOTET_WORDING + '\n' + text + '\n' + MOTET_GUIDANCE


def motet_words(delivered):
    """The words inside motet_worded text, or None when the text is not in that form."""
    if (isinstance(delivered, str) and delivered.startswith(MOTET_WORDING + '\n')
            and delivered.endswith('\n' + MOTET_GUIDANCE)):
        return delivered[len(MOTET_WORDING) + 1:-len(MOTET_GUIDANCE) - 1]
    return None


PLUGIN_PREFIX = 'The watch-owner plugin sent a message:\n'
PLUGIN_SUFFIX = ("\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn "
                 "in the user's place. Address the message above.")
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_ENVELOPE = re.compile(r'(WATCH_REQUEST|WATCH_PEER_REQUEST|WATCH_MOD_PROOF_REQUEST|WATCH_PEER_REPLY|WATCH_REPLY) ('
                       + _UUID + r')(?:\n|\Z)', re.I)
_KINDS = {'WATCH_REQUEST': 'owner', 'WATCH_PEER_REQUEST': 'peer', 'WATCH_MOD_PROOF_REQUEST': 'proof',
          'WATCH_PEER_REPLY': 'reply', 'WATCH_REPLY': 'reply'}
_ATTACHMENT_RELAY = 'Attachment relay version: 3\n'
_CHANNEL = re.compile(r'<channel\b[^>]*>\s*', re.I)
_CHANNEL_END = re.compile(r'\s*</channel>\s*\Z', re.I)
# Leading machine tags that mark an automatic prompt, whatever the record's origin says.
_AUTOMATIC_TAGS = (('scheduled-task', 'scheduled'), ('side-sessions-event', 'side_session'),
                   ('heartbeat', 'heartbeat'), ('task-notification', 'task_notification'))
_LEAD_TAG = re.compile(r'\s*<([a-z][a-z0-9_-]{0,63})\b')
_HUMAN_SENDERS = ('human', 'sdk', 'user', '')


def _tag(text):
    m = _LEAD_TAG.match(text or '')
    return m.group(1).lower() if m else ''


def unwrap(text):
    """The message inside known delivery wrappers: Claude Code's plugin attribution and a channel tag."""
    text = text if isinstance(text, str) else ''
    if text.startswith(PLUGIN_PREFIX):
        text = text[len(PLUGIN_PREFIX):]
        if text.endswith(PLUGIN_SUFFIX):
            text = text[:-len(PLUGIN_SUFFIX)]
    m = _CHANNEL.match(text)
    if m:
        text = _CHANNEL_END.sub('', text[m.end():])
    return text


def delivered_form(text):
    """The exact message a delivered envelope arrived as, for comparing with Motet's stored envelope."""
    return unwrap(text).strip()


def envelope_of(text):
    """(kind, request_id) for a Motet envelope at the start of the message, or None. kind is owner, attachment,
    peer, proof or reply (Motet's notice carrying another chat's answer)."""
    raw = unwrap(text).lstrip()
    m = _ENVELOPE.match(raw)
    if not m:
        return None
    kind = _KINDS[m.group(1).upper()]
    if kind == 'owner' and raw[m.end():].startswith(_ATTACHMENT_RELAY):
        kind = 'attachment'
    return kind, m.group(2).lower()


def from_text(text):
    """Text-only rules, usable wherever only the message text survives. (category, detail) or None."""
    text = text if isinstance(text, str) else ''
    found = envelope_of(text)
    if found:
        return 'motet_delivery', found[0]
    inner = unwrap(text).lstrip()
    if inner.startswith(MOTET_WORDING + '\n'):
        return 'motet_delivery', 'motet_wording'
    if _CHANNEL.match(text.lstrip()):
        # A channel server passed this in; without a Motet envelope inside, it is not the owner typing.
        return 'automatic', 'channel'
    tag = _tag(inner)
    for name, detail in _AUTOMATIC_TAGS:
        if tag == name:
            return 'automatic', detail
    if tag == 'realtime_delegation':
        return 'voice', 'realtime_delegation'
    if tag == 'send_user_message_question_reply':
        # The app's wrapper around answers given in its question form. Who typed or picked the answer is not
        # recorded beyond the wrapper, so it stays unverified and is described as a form answer.
        return UNVERIFIED, 'question_reply'
    if inner.startswith('[Cross-session message'):
        return 'agent_message', 'cross_session'
    if inner.startswith('[Artifact comment sent to'):
        # A comment someone left on a published page, passed to the chat by the app; the commenter is not checked.
        return 'automatic', 'artifact_comment'
    return None


def _field(value):
    return value.strip().lower() if isinstance(value, str) else ''


def claude(record, text, *, sub_agent=False):
    """(category, detail) for one user-role text block of a Claude Code record.

    ``sub_agent`` is true for text in a sub-agent's log after its first prompt: the lead agent wrote it."""
    record = record if isinstance(record, dict) else {}
    origin = record.get('origin') if isinstance(record.get('origin'), dict) else {}
    kind, name = _field(origin.get('kind')), _field(origin.get('name'))
    prompt_source, turn_origin = _field(record.get('promptSource')), _field(record.get('turnOrigin'))
    found = from_text(text)
    if found and found[0] == 'motet_delivery' and (kind in ('peer', 'coordinator') or turn_origin == 'peer' or sub_agent):
        # Motet never delivers into another chat's relay or a sub-agent: envelope-shaped text from there is that
        # chat's or the lead agent's message, whatever it looks like.
        return 'agent_message', 'parent' if kind == 'coordinator' or sub_agent else 'peer'
    if found and found[0] != UNVERIFIED:
        return found
    if kind == 'plugin':
        return ('motet_delivery', 'plugin') if name.startswith('watch-') else ('automatic', 'plugin')
    if kind == 'peer' or turn_origin == 'peer':
        return 'agent_message', 'peer'
    if kind == 'coordinator':
        return 'agent_message', 'parent'
    if kind in ('task-notification', 'task_notification') or turn_origin == 'task_notification':
        return 'automatic', 'task_notification'
    if turn_origin == 'scheduled' or kind == 'scheduled':
        return 'automatic', 'scheduled'
    if turn_origin in ('system', 'host_synthetic') or prompt_source == 'system':
        return 'automatic', 'system'
    if record.get('isMeta'):
        return 'automatic', 'system'
    if kind and kind not in _HUMAN_SENDERS:
        # An origin this version of Motet does not know is still not the owner typing.
        return 'automatic', 'other'
    if sub_agent:
        return 'agent_message', 'parent'
    return found or (UNVERIFIED, '')


def codex(text, *, child=False, thread_source='', exec_run=False):
    """(category, detail) for one Codex UserMessage text. ``child`` is a thread with a parent thread;
    ``exec_run`` a ``codex exec`` session, whose prompt came from whoever started the run."""
    found = from_text(text)
    source = _field(thread_source)
    child = child or source in ('subagent', 'agent_created_thread')
    if found and found[0] == 'motet_delivery' and child:
        return 'agent_message', 'parent'        # Motet never delivers into a child thread
    if found and found[0] != UNVERIFIED:
        return found
    if child:
        return 'agent_message', 'parent'
    if source in ('automation', 'heartbeat', 'scheduled'):
        return 'automatic', 'heartbeat' if source == 'heartbeat' else 'scheduled'
    if exec_run and not found:
        return UNVERIFIED, 'exec_prompt'
    return found or (UNVERIFIED, '')


def normal(value):
    """A known attribution value, or automatic for anything unrecognized: an unknown label is never the owner.
    (A row with no label at all goes through of_row, which applies the text rules.)"""
    return value if value in CATEGORIES else 'automatic'


def stored_label(category, detail=''):
    """The label saved for an event's category and detail, or None for an unknown category."""
    if category == UNVERIFIED and detail in ('exec_prompt', 'question_reply'):
        return UNVERIFIED + '/' + detail
    return category if category in CATEGORIES else None


def split_label(value):
    """(category, detail) for a stored label, or None."""
    if value not in LABELS:
        return None
    category, _, detail = value.partition('/')
    return category, detail


def label_of_row(row):
    """The label of a saved or live user-role row. A row from before attributions were recorded gets only the
    text rules, so a Motet envelope or an automatic prompt in old history is still not called the owner."""
    row = row if isinstance(row, dict) else {}
    if row.get('attribution') in LABELS:
        return row['attribution']
    found = from_text(row.get('text'))
    return stored_label(*found) if found else UNVERIFIED


def of_row(row):
    """The category of a saved or live user-role row (see label_of_row)."""
    return split_label(label_of_row(row))[0]


def display_text(text, category, detail=''):
    """The words worth showing for a message: an envelope's inner words, a question-form answer's question and
    answer, or the text as it is."""
    if category == 'motet_delivery':
        return inner_text(text)
    if detail == 'question_reply':
        m = re.search(r'<send_user_message_question_reply>(.*?)</send_user_message_question_reply>', text or '', re.S)
        try:
            items = json.loads(m.group(1)) if m else None
        except ValueError:
            items = None
        if isinstance(items, list) and items and all(isinstance(i, dict) for i in items):
            return '; '.join('asked "%s", answered "%s"' % (str(i.get('question') or '')[:200], str(i.get('answer') or '')[:400])
                             for i in items[:4])
    return text


def inner_text(text):
    """The words a Motet envelope carries, for describing it; other text is returned unchanged."""
    raw = unwrap(text if isinstance(text, str) else '').lstrip()
    found = _ENVELOPE.match(raw)
    if not found:
        if raw.startswith(MOTET_WORDING + '\n'):
            body = raw[len(MOTET_WORDING) + 1:]
            return body[:-len(MOTET_GUIDANCE) - 1] if body.endswith('\n' + MOTET_GUIDANCE) else body
        return text
    body = raw[found.end():]
    if body.startswith(_ATTACHMENT_RELAY):
        m = re.search(r'\nOwner instruction:\n(.*?)\n\nAttachment references:\n', body, re.S)
        return m.group(1) if m else body
    if _KINDS[found.group(1).upper()] == 'reply':
        return body
    if found.group(1).upper() == 'WATCH_PEER_REQUEST':
        m = re.search(r'(?:^Message: |^Peer text \(JSON string\): )(".*")$', body, re.M)
        if m:
            try:
                value = json.loads(m.group(1))
                if isinstance(value, str):
                    return value
            except ValueError:
                pass
        return body
    first, _, rest = body.partition('\n')
    if first.endswith(':') and rest:
        for end in ('\nReply in your final answer with WATCH_REPLY ', "\nUse this channel's reply tool with request_id "):
            if end in rest:
                return rest.split(end, 1)[0]
        return rest
    return body


def declared_sender(text):
    """The sender a peer envelope declares (unverified), or ''."""
    raw = unwrap(text if isinstance(text, str) else '')
    m = re.search(r'^(?:Origin: peer\. Sender \(UNVERIFIED\): |Declared sender \(UNVERIFIED\): |Reply destination: owner-enrolled endpoint )(".*?")\.?$',
                  raw, re.M)
    if not m:
        return ''
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return ''
    return value[:80] if isinstance(value, str) and value != 'not supplied' else ''


def envelope_author(text):
    """owner or motet when a version 3 owner envelope states who wrote the words, else ''."""
    raw = unwrap(text if isinstance(text, str) else '').lstrip()
    found = _ENVELOPE.match(raw)
    if not found or found.group(1).upper() != 'WATCH_REQUEST':
        return ''
    line = raw[found.end():].split('\n', 1)[0]
    return 'owner' if line == OWNER_WORDING_V3 else 'motet' if line == MOTET_WORDING else ''


def describe(category, detail='', text='', delivery=None):
    """How the Officer is told about a user-role message: a short lead-in ending in ': ', then the words.

    ``delivery`` is Motet's own record for an envelope's request ID when there is one: a dict with ``origin``
    (owner or peer), ``author`` (owner, motet or unknown), and ``words_match`` True only when the message is
    exactly the envelope Motet stored for that request; None when Motet holds no such record for this chat; False
    when its records could not be checked. A record whose words do not match confirms nothing. Only unverified_user keeps the owner's wording."""
    category, detail = normal(category), detail or ''
    if category == UNVERIFIED:
        if detail == 'question_reply':
            return "an answer recorded by the app's question form (the app's wrapper; not checked as the owner's own words): "
        if detail == 'exec_prompt':
            return 'a prompt given to a codex exec run (who wrote it is not recorded): '
        return 'the owner said: '
    if category == 'voice':
        return "a voice-mode message (a model's rewording of the owner's speech, not the owner's exact words): "
    if category == 'agent_message':
        if detail == 'parent':
            return "the lead agent's instruction (not the owner): "
        return 'a message from another chat (not the owner): '
    if category == 'automatic':
        return {'scheduled': 'a scheduled task prompt (automatic, not the owner typing): ',
                'heartbeat': 'an automatic check-in (not the owner): ',
                'task_notification': 'an automatic task notification (not the owner): ',
                'side_session': 'an automatic side-session event (not the owner): ',
                'plugin': 'a prompt a plugin submitted (automatic, not the owner typing): ',
                'artifact_comment': 'a comment left on a published page, passed in by the app (the commenter is not checked; not the owner typing): ',
                'channel': 'a message passed in by a channel server (not the owner typing): '}.get(
                    detail, 'an automatic system message (not the owner): ')
    # motet_delivery
    if detail == 'proof':
        return 'a connection check Motet sent (not the owner): '
    if detail == 'reply':
        return "a reply notice Motet delivered (another chat's answer, not the owner): "
    if detail == 'attachment':
        return ("a message with attachments in Motet's delivery form (its \"Owner instruction\" is not checked against "
                "Motet's attachment records here, so it is not confirmed as the owner's typing): ")
    if detail == 'motet_wording':
        return "a message Motet wrote at the owner's request (Motet's wording, not the owner's): "
    if detail == 'peer':
        sender = declared_sender(text)
        named = (' (it says it is from ' + sender + '; not verified)') if sender else ''
        if delivery and delivery.get('origin') == 'peer' and delivery.get('words_match') is True:
            return 'a message Motet delivered from another chat' + named + ', not from the owner: '
        if delivery is False:
            return "a message in Motet's delivery form from another chat" + named + " (Motet's records could not be checked just now); not from the owner: "
        return "a message shaped like Motet's delivery from another chat" + named + ", which Motet's records do not confirm; not from the owner: "
    if detail == 'plugin':
        return "a prompt Motet's plugin submitted (not the owner typing): "
    if delivery is False:
        return "a message in Motet's delivery form (Motet's records could not be checked just now), not the owner typing: "
    if not isinstance(delivery, dict) or delivery.get('words_match') is not True:
        return ("a message shaped like a Motet delivery that Motet's records do not confirm"
                + (" (it says Motet worded it)" if envelope_author(text) == 'motet' else '') + ', not the owner typing: ')
    if delivery.get('origin') == 'peer':
        return 'a message Motet delivered from another chat, not from the owner: '
    author = delivery.get('author')
    if author == 'owner':
        return 'a message the owner typed in Motet, which Motet delivered: '
    if author == 'motet':
        return "a message Motet wrote and delivered at the owner's request (Motet's wording, not the owner's): "
    return "a message Motet delivered for the owner (Motet's records do not say who wrote the wording): "


def citation_label(category):
    """The speaker shown for a user-role row in a citation; takes a category or a stored label."""
    if category == EXEC_LABEL:
        return 'prompt given to a codex exec run (who wrote it is not recorded)'
    if category == FORM_LABEL:
        return "answer recorded by the app's question form (not checked as the owner's own words)"
    return {'motet_delivery': 'message Motet delivered',
            'agent_message': 'message from another agent or chat',
            'automatic': 'automatic message, not the owner',
            'voice': "voice-mode message (a model's rewording of the owner's speech)"}.get(normal(category), 'owner message')


def evidence_value(category):
    """The ``attribution`` value an evidence record carries for a user-role row; takes a category or a label."""
    if category == EXEC_LABEL:
        return 'codex_exec_prompt'
    if category == FORM_LABEL:
        return 'question_form_answer'
    category = normal(category)
    return 'owner_message' if category == UNVERIFIED else category
