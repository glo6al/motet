"""Read exactly one attachment attempt from the installed Codex rollout format.

The app appends to its own index and transcript. WATCH retains only the pinned
source identity and the hash of the complete prefix preceding the queue call.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re
import stat

import codex_transcript


# The whole rollout is read into memory (no partial reads, so no read-ahead is needed), so MAX_SOURCE bounds this
# reader; it stays at 16 MiB for the same memory reason as codex_attachment_evidence. A compaction record is a
# normal record: the line limit is the shared rollout record limit.
MAX_SOURCE = 16 * 1024 * 1024
MAX_LINE = codex_transcript.MAX_RECORD
MAX_LINES = 16_384

_INDEX = 'state_5.sqlite'
_SOURCE_FIELDS = {'ordinal', 'payload', 'timestamp', 'type'}
_START_FIELDS = {
    'type', 'turn_id', 'root_turn_id', 'started_at', 'collaboration_mode_kind',
    'model_context_window', 'turn_attribution',
}
_COMPLETE_FIELDS = {
    'type', 'turn_id', 'root_turn_id', 'started_at', 'completed_at',
    'duration_ms', 'time_to_first_token_ms', 'last_agent_message',
}
_USER_FIELDS = {'type', 'role', 'id', 'content', 'internal_chat_message_metadata_passthrough'}
_INPUT_FIELDS = {'turn_id', 'create_time', 'content_item_kinds', 'content_item_metadata'}
_INPUT_METADATA_FIELDS = {'client_authored', 'mcp_attribution', 'retained_source', 'user_input_order'}
_CONTEXT_KINDS = {
    'agents_md.instructions': {'type': 'agents_md'},
    'environments.environment_context': {'type': 'harness', 'hook': False},
}
_EVENT_DIAGNOSTICS = {
    'thread_settings_applied', 'token_count', 'agent_message', 'agent_reasoning',
    'exec_command_begin', 'exec_command_end', 'task_aborted', 'turn_aborted',
    'view_image_tool_call', 'mcp_tool_call_begin', 'mcp_tool_call_end',
}
_RESPONSE_DIAGNOSTICS = {
    'additional_tools', 'reasoning', 'function_call', 'function_call_output',
    'custom_tool_call', 'custom_tool_call_output', 'web_search_call', 'computer_call',
    'computer_call_output', 'image_generation_call', 'local_shell_call', 'mcp_list_tools',
    'mcp_call', 'compaction',
}


class EvidenceUnavailable(ValueError):
    """Fixed source fault without transcript content or local paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(value, code='source_unavailable'):
    if not value:
        raise EvidenceUnavailable(code)


def _uuid(value):
    return type(value) is str and re.fullmatch(codex_transcript.UUID, value) is not None


def _hex_digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _path(value):
    return (type(value) is str and len(value) <= 4096 and '\0' not in value
            and os.path.isabs(value) and os.path.abspath(value) == value)


def _identity(info):
    return (info.st_dev, info.st_ino)


def _stamp(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _file_owner(info):
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise EvidenceUnavailable('source_owner_changed')


def _directory(path, identity=None):
    _need(_path(path) and os.path.realpath(path) == path)
    info = os.lstat(path)
    _need(stat.S_ISDIR(info.st_mode))
    _file_owner(info)
    if identity is not None:
        _need(_identity(info) == identity, 'source_identity_changed')
    return _identity(info)


@dataclass(frozen=True)
class SourceFloor:
    thread_id: str
    project: str = field(repr=False)
    project_device: int
    project_inode: int
    native_root: str = field(repr=False)
    root_device: int
    root_inode: int
    sessions_device: int
    sessions_inode: int
    index_device: int
    index_inode: int
    rollout: str = field(repr=False)
    rollout_device: int
    rollout_inode: int
    end: int
    ordinal: int
    prefix_sha256: str

    def __post_init__(self):
        _need(_uuid(self.thread_id) and all(_path(p) for p in (self.project, self.native_root, self.rollout))
              and self.rollout.startswith(os.path.join(self.native_root, 'sessions') + os.sep)
              and _hex_digest(self.prefix_sha256), 'invalid_floor')
        for name in ('project_device', 'project_inode', 'root_device', 'root_inode', 'sessions_device',
                     'sessions_inode', 'index_device', 'index_inode', 'rollout_device', 'rollout_inode'):
            value = getattr(self, name)
            _need(type(value) is int and (0 if name.endswith('_device') else 1) <= value < 2**64,
                  'invalid_floor')
        _need(type(self.end) is int and 0 < self.end <= MAX_SOURCE
              and type(self.ordinal) is int and 0 <= self.ordinal < MAX_LINES, 'invalid_floor')

    def to_record(self):
        return asdict(self)

    @classmethod
    def from_record(cls, value):
        _need(type(value) is dict and set(value) == set(cls.__dataclass_fields__), 'invalid_floor')
        return cls(**value)


@dataclass(frozen=True)
class Observation:
    state: str
    turn_id: str | None = None
    reply: str | None = field(default=None, repr=False)
    _floor: SourceFloor | None = field(default=None, repr=False)
    _stamp: tuple | None = field(default=None, repr=False)


def _index_identity(native_root):
    src, info = codex_transcript.open_regular(native_root, os.path.join(native_root, _INDEX))
    with src:
        _file_owner(info)
    return _identity(info)


def _open_bound(floor):
    native_root = floor.native_root
    sessions = os.path.join(native_root, 'sessions')
    _directory(native_root, (floor.root_device, floor.root_inode))
    _directory(sessions, (floor.sessions_device, floor.sessions_inode))
    _directory(floor.project, (floor.project_device, floor.project_inode))
    identity = (floor.index_device, floor.index_inode)
    _need(_index_identity(native_root) == identity, 'source_identity_changed')
    _, src, info, meta = codex_transcript.open_pinned(sessions, floor.rollout, floor.thread_id, floor.project)
    try:
        _file_owner(info)
        _need('history_base' not in meta and _identity(info) == (floor.rollout_device, floor.rollout_inode),
              'source_identity_changed')
        _need(_index_identity(native_root) == identity, 'source_identity_changed')
        _directory(native_root, (floor.root_device, floor.root_inode))
        _directory(sessions, (floor.sessions_device, floor.sessions_inode))
        _directory(floor.project, (floor.project_device, floor.project_inode))
        return src, info
    except BaseException:
        src.close()
        raise


def _snapshot(src, info):
    _need(0 < info.st_size <= MAX_SOURCE, 'source_byte_limit')
    src.seek(0)
    raw = src.read(info.st_size)
    _need(len(raw) == info.st_size and _stamp(os.fstat(src.fileno())) == _stamp(info),
          'source_changed_during_read')
    return raw


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        _need(key not in value, 'invalid_record')
        value[key] = item
    return value


def _strict(line):
    try:
        record = json.loads(line.decode('utf-8'), object_pairs_hook=_unique_pairs)
        # Reject non-finite numbers and lone surrogate escapes before any text
        # enters the request or reply comparison.
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return record
    except (UnicodeError, ValueError, RecursionError, TypeError, OverflowError):
        raise EvidenceUnavailable('invalid_record') from None


def _record(line):
    _need(0 < len(line) <= MAX_LINE, 'source_line_limit')
    record = _strict(line)
    _need(type(record) is dict and set(record) in (_SOURCE_FIELDS, _SOURCE_FIELDS | {'metadata'})
          and type(record['ordinal']) is int and record['ordinal'] >= 0
          and type(record['timestamp']) is str and 0 < len(record['timestamp']) <= 128
          and type(record['type']) is str and type(record['payload']) is dict
          and ('metadata' not in record or type(record['metadata']) is dict), 'invalid_record')
    return record


def prepare(native_root, thread_id, project, project_device, project_inode):
    """Pin a stable, complete native source before issuing the queue command."""
    try:
        native_root = os.fspath(native_root)
        project = os.fspath(project)
        _need(_uuid(thread_id))
        root_device, root_inode = _directory(native_root)
        sessions = os.path.join(native_root, 'sessions')
        sessions_device, sessions_inode = _directory(sessions)
        _directory(project, (project_device, project_inode))
        index_device, index_inode = _index_identity(native_root)
        entries = codex_transcript.index(native_root, thread_id, limit=1)
        _need(type(entries) is dict and set(entries) == {thread_id})
        entry = entries[thread_id]
        rollout = codex_transcript.selected(entry)
        _need(entry['cwd'] == project and _path(rollout))
        _, src, info, meta = codex_transcript.open_pinned(sessions, rollout, thread_id, project)
        with src:
            _file_owner(info)
            _need('history_base' not in meta, 'source_continuation')
            raw = _snapshot(src, info)
        _need(raw.endswith(b'\n'), 'source_incomplete')
        header = _record(raw[:raw.index(b'\n')])
        _need(header['ordinal'] == 0 and header['type'] == 'session_meta', 'invalid_header')
        count = raw.count(b'\n')
        _need(0 < count <= MAX_LINES, 'source_line_limit')
        last = raw[raw.rfind(b'\n', 0, len(raw) - 1) + 1:-1]
        ordinal = _record(last)['ordinal']
        # The installed format starts at ordinal zero. A segmented or gapped
        # history is outside this delivery evidence reader.
        _need(ordinal == count - 1, 'source_ordinal_gap')
        floor = SourceFloor(thread_id, project, project_device, project_inode, native_root,
                            root_device, root_inode, sessions_device, sessions_inode, index_device,
                            index_inode, rollout, info.st_dev, info.st_ino, len(raw), ordinal, _hash(raw))
        _verify_stamp(floor, _stamp(info))
        return floor
    except (OSError, codex_transcript.Unavailable, TypeError):
        raise EvidenceUnavailable('source_unavailable') from None


def _read_from(floor):
    src, info = _open_bound(floor)
    with src:
        raw = _snapshot(src, info)
    _need(len(raw) >= floor.end and _hash(raw[:floor.end]) == floor.prefix_sha256,
          'source_prefix_changed')
    return raw, _stamp(info)


def _check_root(native_root, floor):
    _need(type(floor) is SourceFloor and os.fspath(native_root) == floor.native_root, 'invalid_floor')


def _verify_stamp(floor, expected):
    src, info = _open_bound(floor)
    with src:
        _need(_stamp(info) == expected and _stamp(os.fstat(src.fileno())) == expected,
              'source_changed_during_read')


def revalidate(native_root, floor):
    """The pre-dispatch source must still end precisely at the prepared floor."""
    try:
        _check_root(native_root, floor)
        raw, stamp = _read_from(floor)
        _need(len(raw) == floor.end, 'source_floor_advanced')
        _verify_stamp(floor, stamp)
    except (OSError, codex_transcript.Unavailable, TypeError):
        raise EvidenceUnavailable('source_unavailable') from None


def verify_observation(native_root, floor, result):
    """Recheck the exact observed source stamp before committing delivery facts."""
    try:
        _check_root(native_root, floor)
        _need(type(result) is Observation and result.state in ('ended', 'reply')
              and result._floor == floor and result._stamp is not None, 'invalid_observation')
        _verify_stamp(floor, result._stamp)
    except (OSError, codex_transcript.Unavailable, TypeError):
        raise EvidenceUnavailable('source_unavailable') from None


def _body_digest(text, owner_request, envelope_sha256):
    if not text:
        return False
    marker = 'WATCH_REQUEST ' + owner_request
    matches = _hash(text.encode('utf-8')) == envelope_sha256
    has_marker = text.split('\n', 1)[0] == marker
    _need(matches == has_marker, 'ambiguous_envelope')
    return matches


def _raw_user(record, owner_request, envelope_sha256):
    payload = record['payload']
    _need(set(payload) == _USER_FIELDS, 'opaque_input')
    nested = payload['internal_chat_message_metadata_passthrough']
    _need(type(nested) is dict and set(nested) == _INPUT_FIELDS and _uuid(nested['turn_id']), 'invalid_turn')
    turn = nested['turn_id']
    content, kinds, content_metadata = payload['content'], nested['content_item_kinds'], nested['content_item_metadata']
    _need(type(content) is list and type(kinds) is list and type(content_metadata) is list
          and len(content) == len(kinds) == len(content_metadata) and bool(content), 'opaque_input')
    body = codex_transcript.pure_text(content, 'user')
    matches = _body_digest(body, owner_request, envelope_sha256)
    metadata = record.get('metadata')
    if not matches and metadata is None and all(
            type(kind) is str and kind in _CONTEXT_KINDS and detail == {'provenance': _CONTEXT_KINDS[kind]}
            for kind, detail in zip(kinds, content_metadata)) and body:
        return turn, 'context', False
    if matches:
        _need(kinds == ['user.text'] and content_metadata == [{'provenance': {'type': 'user'}}]
              and type(metadata) is dict and set(metadata) == _INPUT_METADATA_FIELDS
              and metadata['client_authored'] is False and type(metadata['user_input_order']) is int
              and metadata['user_input_order'] >= 0, 'unverified_queue_input')
        source = metadata['retained_source']
        _need(type(payload['id']) is str and bool(payload['id']) and type(source) is dict
              and set(source) == {'complete', 'id', 'revision'} and source['complete'] is True
              and source['id'] == {'message_id': payload['id'], 'turn_id': turn, 'role': 'user'}
              and type(source['revision']) is str and 0 < len(source['revision']) <= 256,
              'unverified_queue_input')
    return turn, 'input', matches


def _item_user(payload, thread_id, owner_request, envelope_sha256):
    _need(_uuid(payload.get('turn_id')) and payload.get('thread_id') == thread_id and
          type(payload['item'].get('id')) is str and bool(payload['item']['id']), 'opaque_input')
    content = payload['item'].get('content')
    _need(type(content) is list and bool(content), 'opaque_input')
    if any(type(part) is not dict or set(part) != {'type', 'text', 'text_elements'} or
           part['type'] != 'text' or type(part['text']) is not str or part['text_elements'] != []
           for part in content):
        return payload['turn_id'], False
    text = ''.join(part['text'] for part in content)
    return payload['turn_id'], _body_digest(text, owner_request, envelope_sha256)


def _queued_turn(payload, turn):
    attribution = payload.get('turn_attribution')
    return (payload.get('root_turn_id') == turn and type(attribution) is dict and
            attribution.get('turn_id') == turn and attribution.get('root_turn_id') == turn and
            'parent_turn_id' in attribution and attribution['parent_turn_id'] is None and
            'initiating_agent_path' in attribution and attribution['initiating_agent_path'] in (None, '/root') and
            attribution.get('turn_trigger') == 'queue')


def _semantic_events(record, floor, owner_request, envelope_sha256):
    typ, payload = record['type'], record['payload']
    kind = payload.get('type')
    if typ == 'response_item':
        if kind == 'message' and payload.get('role') == 'user':
            turn, label, matching = _raw_user(record, owner_request, envelope_sha256)
            return turn, label, matching
        if (kind == 'message' and payload.get('role') in ('assistant', 'developer', 'system', 'tool')
                or type(kind) is str and kind in _RESPONSE_DIAGNOSTICS and payload.get('role') != 'user'):
            return None
    if typ == 'event_msg':
        if kind in ('task_started', 'task_complete', 'task_aborted', 'turn_aborted'):
            turn = payload.get('turn_id')
            _need(_uuid(turn), 'invalid_turn')
            if kind == 'task_started':
                _need(set(payload) <= _START_FIELDS, 'invalid_start')
                return turn, 'start', _queued_turn(payload, turn)
            if kind == 'task_complete':
                _need(set(payload) <= _COMPLETE_FIELDS, 'invalid_terminal')
                reply = payload.get('last_agent_message')
                _need(reply is None or type(reply) is str, 'invalid_terminal')
                return turn, 'end', (payload.get('root_turn_id') == turn, reply)
            return turn, 'abort', None
        if kind in ('item_started', 'item_completed'):
            item = payload.get('item')
            _need(type(item) is dict and type(item.get('type')) is str, 'unsupported_record')
            if item['type'] == 'UserMessage':
                turn, matched = _item_user(payload, floor.thread_id, owner_request, envelope_sha256)
                return (turn, 'item', matched) if kind == 'item_completed' else (turn, 'item_start', matched)
            return None
        if type(kind) is str and kind in _EVENT_DIAGNOSTICS:
            return None
    if typ in ('turn_context', 'token_usage_record', 'world_state'):
        return None
    raise EvidenceUnavailable('unsupported_record')


def _verified_reply(text, owner_request):
    if not text:
        return None
    marker = 'WATCH_REPLY ' + owner_request
    lines = text.split('\n')
    if [line for line in lines if 'WATCH_REPLY' in line] != [marker]:
        return None
    body = '\n'.join(line for line in lines if line != marker)
    if body.strip() and len(body) <= 16_000 and len(body.encode('utf-8')) <= 64_000:
        return body
    return None


def _classify(raw, floor, owner_request, envelope_sha256):
    events, matching = {}, None
    for offset, line in enumerate(raw.split(b'\n')[:-1], floor.ordinal + 1):
        _need(offset < MAX_LINES, 'source_line_limit')
        record = _record(line)
        _need(record['ordinal'] == offset, 'source_ordinal_gap')
        semantic = _semantic_events(record, floor, owner_request, envelope_sha256)
        if semantic is None:
            continue
        turn, role, value = semantic
        events.setdefault(turn, {}).setdefault(role, []).append((offset, value))
        if role == 'input' and value:
            _need(matching is None, 'ambiguous_input')
            matching = turn
    if matching is None:
        return 'waiting', None, None
    # A separate native user event carrying the same exact envelope on another
    # turn contradicts the raw input, even before that turn has a retained item.
    _need(not any(value for turn, history in events.items() if turn != matching
                  for label in ('item_start', 'item') for _, value in history.get(label, ())),
          'ambiguous_input')
    event = events[matching]
    starts, inputs, items, ends = (event.get(key, ()) for key in ('start', 'input', 'item', 'end'))
    _need(len(starts) == len(inputs) == 1 and len(items) <= 1 and len(ends) <= 1
          and not event.get('abort'), 'ambiguous_turn')
    start, queued = starts[0]
    input_position = inputs[0][0]
    _need(queued and start < input_position
          and all(start < position < input_position for position, _ in event.get('context', ())),
          'invalid_turn_order')
    item_starts = event.get('item_start', ())
    _need(len(item_starts) <= 1 and all(start < position and value for position, value in item_starts),
          'ambiguous_turn')
    if items:
        item_position, confirmed = items[0]
        _need(confirmed and input_position < item_position
              and all(position < item_position for position, _ in item_starts), 'ambiguous_turn')
    if not ends:
        return 'waiting', None, None
    end, (root_matches, reply) = ends[0]
    _need(items and root_matches and items[0][0] < end, 'invalid_terminal')
    verified_reply = _verified_reply(reply, owner_request)
    return ('reply', matching, verified_reply) if verified_reply else ('ended', matching, None)


def observe(native_root, floor, owner_request_id, envelope_sha256):
    """Return a terminal fact only for a unique, explicitly bound native turn."""
    try:
        _check_root(native_root, floor)
        _need(_uuid(owner_request_id) and _hex_digest(envelope_sha256), 'invalid_attempt')
        raw, stamp = _read_from(floor)
        if not raw.endswith(b'\n'):
            _need(len(raw) - raw.rfind(b'\n') <= MAX_LINE, 'source_line_limit')
            return Observation('waiting')
        state, turn_id, reply = _classify(raw[floor.end:], floor, owner_request_id, envelope_sha256)
        _verify_stamp(floor, stamp)
        return Observation(state, turn_id, reply, floor, stamp) if turn_id is not None else Observation(state)
    except EvidenceUnavailable as exc:
        return Observation('waiting' if exc.code == 'source_changed_during_read' else 'unavailable')
    except (OSError, codex_transcript.Unavailable, TypeError):
        return Observation('unavailable')
