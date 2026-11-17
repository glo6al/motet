"""Classify owner payload representations without building a Python JSON tree."""
import re
import sqlite3


CLEAR, MARKER, UNCERTAIN = 0, 1, 2
MAX_DEPTH = 1024
_KEY = b'attachment_owner_version'
_WHITESPACE = b' \t\n\r'
_STRING_RUN = re.compile(rb'[\x20-\x21\x23-\x5b\x5d-\x7e]+')
_NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_ESCAPE = {ord(k): ord(v) for k, v in zip('"\\/bfnrt', '"\\/\b\f\n\r\t')}
_OBJECT_FIRST, _OBJECT_KEY, _OBJECT_AFTER, _ARRAY_FIRST, _ARRAY_VALUE, _ARRAY_AFTER = range(6)


def _space(raw, pos):
    while pos < len(raw) and raw[pos] in _WHITESPACE:
        pos += 1
    return pos


def _hex_digit(value):
    if 48 <= value <= 57:
        return value - 48
    lower = value | 32
    return lower - 87 if 97 <= lower <= 102 else -1


def _string(raw, pos, root_key=False):
    """Scan the string after its opening quote and match only the fixed root key."""
    matched = 0 if root_key else -1
    while pos < len(raw):
        run = _STRING_RUN.match(raw, pos)
        if run:
            end = run.end()
            if matched >= 0:
                next_match = matched + end - pos
                if next_match <= len(_KEY) and raw.startswith(_KEY[matched:next_match], pos):
                    matched = next_match
                else:
                    matched = -1
            pos = end
            continue
        token = raw[pos]
        if token == ord('"'):
            return pos + 1, matched == len(_KEY)
        if token != ord('\\') or pos + 1 >= len(raw):
            return -1, False
        escaped = raw[pos + 1]
        pos += 2
        if escaped == ord('u'):
            if pos + 4 > len(raw):
                return -1, False
            decoded = 0
            for _ in range(4):
                digit = _hex_digit(raw[pos])
                if digit < 0:
                    return -1, False
                decoded = decoded * 16 + digit
                pos += 1
        else:
            decoded = _ESCAPE.get(escaped)
            if decoded is None:
                return -1, False
        if matched >= 0:
            matched = matched + 1 if matched < len(_KEY) and _KEY[matched] == decoded else -1
    return -1, False


def lexical_payload_state(raw):
    """Inspect a legacy ASCII JSON object using a bounded auxiliary grammar stack.

    SQLite marshals the complete input bytes of the connection-local fallback.
    The detector stores no unbounded token, parsed value or result.
    """
    if type(raw) is not bytes:
        return UNCERTAIN
    pos = _space(raw, 0)
    if pos == len(raw) or raw[pos] != ord('{'):
        return UNCERTAIN
    pos += 1
    stack = bytearray([_OBJECT_FIRST])
    while stack:
        pos = _space(raw, pos)
        if pos == len(raw):
            return UNCERTAIN
        state, token = stack[-1], raw[pos]
        if state in (_OBJECT_AFTER, _ARRAY_AFTER):
            closing = ord('}') if state == _OBJECT_AFTER else ord(']')
            if token == closing:
                stack.pop()
            elif token == ord(','):
                stack[-1] = _OBJECT_KEY if state == _OBJECT_AFTER else _ARRAY_VALUE
            else:
                return UNCERTAIN
            pos += 1
            continue
        if (state == _OBJECT_FIRST and token == ord('}')) or (state == _ARRAY_FIRST and token == ord(']')):
            stack.pop()
            pos += 1
            continue
        if state in (_OBJECT_FIRST, _OBJECT_KEY):
            if token != ord('"'):
                return UNCERTAIN
            pos, marker = _string(raw, pos + 1, root_key=len(stack) == 1)
            if pos < 0:
                return UNCERTAIN
            pos = _space(raw, pos)
            if pos == len(raw) or raw[pos] != ord(':'):
                return UNCERTAIN
            if marker:
                return MARKER  # An exact root-key witness survives later damage.
            pos = _space(raw, pos + 1)
            if pos == len(raw):
                return UNCERTAIN
            stack[-1] = _OBJECT_AFTER
            token = raw[pos]
        else:
            stack[-1] = _ARRAY_AFTER
        if token in (ord('{'), ord('[')):
            if len(stack) + 1 >= MAX_DEPTH:
                return UNCERTAIN
            stack.append(_OBJECT_FIRST if token == ord('{') else _ARRAY_FIRST)
            pos += 1
        elif token == ord('"'):
            pos, _ = _string(raw, pos + 1)
            if pos < 0:
                return UNCERTAIN
        elif raw.startswith(b'true', pos) or raw.startswith(b'null', pos):
            pos += 4
        elif raw.startswith(b'false', pos):
            pos += 5
        else:
            number = _NUMBER.match(raw, pos)
            if not number:
                return UNCERTAIN
            pos = number.end()
    return CLEAR if _space(raw, pos) == len(raw) else UNCERTAIN


def sqlite_payload_state(column):
    """SQL for a trusted, static column, suitable for persistent triggers."""
    # SQLite's JSON parser does not certify UTF-8. For a clear result, both the
    # byte/text length and the family of exact allowed ASCII bytes must agree.
    # Enumerate keys and compare bytes. JSON path lookup can truncate a key
    # at an escaped NUL, which would mistake a different key for the marker.
    return (f'(CASE WHEN typeof({column}) <> \'text\' THEN {UNCERTAIN} '
            f'WHEN NOT json_valid({column}) THEN {UNCERTAIN} '
            f'WHEN json_type({column}) <> \'object\' THEN {UNCERTAIN} '
            f'WHEN EXISTS (SELECT 1 FROM json_each({column}) AS root_field '
            f'WHERE CAST(root_field.key AS BLOB) = CAST(\'attachment_owner_version\' AS BLOB)) THEN {MARKER} '
            f'WHEN length(CAST({column} AS BLOB)) = length({column}) '
            f'AND {column} NOT GLOB (\'*[^ -~\' || char(9,10,13) || \']*\') THEN {CLEAR} '
            f'ELSE {UNCERTAIN} END)')


def sqlite_marker_available(db):
    statement = f'SELECT {sqlite_payload_state("sample.body")} FROM (SELECT CAST(? AS TEXT) AS body) AS sample'
    fixtures = (
        (b' \t\r\n{"question":"\\ud83d\\ude00"}\r\n', CLEAR),
        (b'{"x":{"attachment_owner_version":null}}', CLEAR),
        (br'{"\u0061ttachment_owner_version":null}', MARKER),
        (br'{"attachment_owner_version\u0000":null}', CLEAR),
        (br'{"\u0061ttachment_owner_version":null,"attachment_owner_version":0}', MARKER),
        (br'{"attachment_owner_version":null,"\u0061ttachment_owner_version":0}', MARKER),
        (b'{"x":"\x00"}', UNCERTAIN),
        (b'{"x":"\x7f"}', UNCERTAIN),
        (b'{"x":"\xff"}', UNCERTAIN),
        ('{"x":"é"}'.encode('utf-8'), UNCERTAIN),
        (b'[{"attachment_owner_version":1}]', UNCERTAIN),
    )
    try:
        return all(db.execute(statement, (raw,)).fetchone() == (state,) for raw, state in fixtures)
    except sqlite3.OperationalError:
        return False
