"""Read-only view of Motet's self folder and the version history of its files.

Motet's self lives in ``~/.live-room/motet-self/``: SOUL.md, RULES.md, IDENTITY.md, VOCABULARY.md, ASKING_MOTET.md
and skills/*.md. Every saved version of each file is kept in ``motet-self/.history/<file>/v000001.json`` and so on,
one JSON record per version, never changed or removed once written. Each record holds the sha256 of the record
before it (a chain), and ``.history/<file>/latest.json`` names the newest record and its sha256, so a missing,
changed or extra record is noticed and refused rather than silently skipped.

This module only reads. It has no function that creates, changes or removes anything, so the server can list history
without gaining a way to write it. Only the owner's command (scripts/motet_self.py, through src/motet_self.py)
writes the self folder. Every folder and file read must be private (private_storage): a link, a file with a second
name, or anything another user could change is refused, never repaired.
"""
import hashlib
import json
import os
import re

import private_storage

FOLDER_NAME = 'motet-self'
HISTORY = '.history'
PENDING = '.pending.json'
LATEST = 'latest.json'
CORE_FILES = ('SOUL.md', 'RULES.md', 'IDENTITY.md', 'VOCABULARY.md', 'ASKING_MOTET.md')
SKILLS = 'skills'
AUTHORS = frozenset(('owner', 'motet'))
SCHEMA = 1
MAX_FILE_BYTES = 256 * 1024
MAX_RECORD_BYTES = 2 * MAX_FILE_BYTES
MAX_REASON = 200
MAX_VERSIONS = 999999
_SKILL_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\.md\Z')
# Windows treats these names as devices whatever their extension, so they never name a skill.
_RESERVED = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % n for n in range(1, 10)] + ['lpt%d' % n for n in range(1, 10)])
_VERSION_NAME = re.compile(r'v([0-9]{6})\.json\Z')
_NUMBER = re.compile(r'[vV]?([0-9]{1,6})\Z')
_AT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_KEY_SHAPE = re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')


class SelfError(Exception):
    """A refusal in plain words. The message never carries file content or system text."""


def self_folder(home=None):
    return os.path.join(home or os.path.expanduser('~'), '.live-room', FOLDER_NAME)


def skill_name(base):
    return bool(_SKILL_NAME.fullmatch(base)) and base.split('.')[0] not in _RESERVED


def check_name(name):
    """The only names the self folder tracks: the five core files and skills/<name>.md. Returns the name with '/'."""
    if not isinstance(name, str):
        raise SelfError('Not a file of Motet\'s self.')
    name = name.replace('\\', '/')
    if name in CORE_FILES:
        return name
    folder, _, base = name.partition('/')
    if folder == SKILLS and skill_name(base):
        return name
    raise SelfError('Not a file of Motet\'s self: use SOUL.md, RULES.md, IDENTITY.md, VOCABULARY.md, '
                    'ASKING_MOTET.md or skills/<name>.md.')


def parse_version(value):
    match = _NUMBER.fullmatch(str(value))
    if not match or not 1 <= int(match.group(1)) <= MAX_VERSIONS:
        raise SelfError('A version is a number such as 3 or v3.')
    return int(match.group(1))


def version_file(number):
    return 'v%06d.json' % number


def _parts(name):
    return check_name(name).split('/')


def working_path(root, name):
    return os.path.join(root, *_parts(name))


def history_dir(root, name):
    return os.path.join(root, HISTORY, *_parts(name))


def check_folders(root, name=None, history=True):
    """Refuse unless the self folder (and the folders on the way to ``name`` and its history) are private."""
    try:
        private_storage.check_private(root, directory=True)
        if history:
            private_storage.check_private(os.path.join(root, HISTORY), directory=True)
        if name is not None and '/' in check_name(name):
            private_storage.check_private(os.path.join(root, SKILLS), directory=True)
            if history and os.path.lexists(os.path.join(root, HISTORY, SKILLS)):
                private_storage.check_private(os.path.join(root, HISTORY, SKILLS), directory=True)
    except private_storage.PrivateStorageError as e:
        raise SelfError('Motet\'s self folder is missing or not private (%s %s); nothing was read or changed.'
                        % (e.subject, e.reason)) from None


def read_private(path, limit):
    """Bytes of a private regular file with one name, or SelfError."""
    try:
        fd, info = private_storage.open_private_file(path, single_link=True)
    except private_storage.PrivateStorageError as e:
        raise SelfError('A self file is missing or not private (%s); nothing was changed.' % e.reason) from None
    try:
        if info.st_size > limit:
            raise SelfError('A self file is larger than Motet allows.')
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise SelfError('A self file is larger than Motet allows.')
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(fd)


def decode_text(raw):
    """Self files are UTF-8 text without NUL; the bytes are kept as written (line endings included)."""
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise SelfError('A self file is not UTF-8 text.') from None
    if '\x00' in text:
        raise SelfError('A self file holds a NUL character.')
    if _KEY_SHAPE.search(text):
        raise SelfError('A self file holds something shaped like an API key; remove it first.')
    return text


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_record(raw, name, number):
    """A version record as written by the owner's command, or SelfError."""
    try:
        record = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        record = None
    keys = {'schema', 'file', 'version', 'previous', 'author', 'at', 'reason', 'sha256', 'content'}
    if (not isinstance(record, dict) or set(record) != keys or record['schema'] != SCHEMA
            or record['file'] != name or type(record['version']) is not int or record['version'] != number
            or not (record['previous'] is None if number == 1 else isinstance(record['previous'], str)
                    and _SHA.fullmatch(record['previous']))
            or record['author'] not in AUTHORS or not isinstance(record['at'], str) or not _AT.fullmatch(record['at'])
            or not isinstance(record['reason'], str) or not record['reason'] or len(record['reason']) > MAX_REASON
            or not isinstance(record['content'], str) or record['sha256'] != sha256(record['content'])):
        raise SelfError('A history record of %s is damaged; it was left as it is. Nothing was changed.' % name)
    return record


def parse_latest(raw, name):
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        value = None
    if (not isinstance(value, dict) or set(value) != {'file', 'version', 'record'} or value['file'] != name
            or type(value['version']) is not int or not 1 <= value['version'] <= MAX_VERSIONS
            or not isinstance(value['record'], str) or not _SHA.fullmatch(value['record'])):
        raise SelfError('The latest-version marker of %s is damaged; nothing was changed.' % name)
    return value


def _mismatch(name):
    return SelfError('The history of %s does not match its latest-version marker: a record may be missing, '
                     'changed or added by hand. Nothing was changed.' % name)


def pending_change(root):
    return os.path.lexists(os.path.join(root, HISTORY, PENDING))


def load_history(root, name):
    """Every record of ``name``, oldest first, after checking the chain and the latest marker. Empty when the file
    has no history yet. Raises SelfError when anything disagrees."""
    name = check_name(name)
    folder = history_dir(root, name)
    if not os.path.lexists(folder):
        return []
    try:
        private_storage.check_private(folder, directory=True)
        entries = os.listdir(folder)
    except (OSError, private_storage.PrivateStorageError):
        raise SelfError('A history folder is not private; nothing was read or changed.') from None
    numbers = sorted(int(m.group(1)) for m in map(_VERSION_NAME.fullmatch, entries) if m)
    if numbers != list(range(1, len(numbers) + 1)):
        raise _mismatch(name)
    marker = os.path.join(folder, LATEST)
    if not numbers:
        if os.path.lexists(marker):
            raise _mismatch(name)
        return []
    if not os.path.lexists(marker):
        raise _mismatch(name)
    latest = parse_latest(read_private(marker, 4096), name)
    records, previous = [], None
    for number in numbers:
        raw = read_private(os.path.join(folder, version_file(number)), MAX_RECORD_BYTES)
        record = parse_record(raw, name, number)
        if record['previous'] != previous:
            raise _mismatch(name)
        previous = digest(raw)
        records.append((record, previous))
    if latest['version'] != numbers[-1] or latest['record'] != previous:
        raise _mismatch(name)
    return records


def read_version(root, name, number):
    records = load_history(root, name)
    if not 1 <= number <= len(records):
        raise SelfError('There is no version %d of %s.' % (number, check_name(name)))
    return records[number - 1][0]


def tracked_names(root):
    """Every self file that exists in the folder or has history, core files first."""
    names = set()
    for name in CORE_FILES:
        if os.path.lexists(os.path.join(root, name)) or os.path.lexists(os.path.join(root, HISTORY, name)):
            names.add(name)
    for folder in (os.path.join(root, SKILLS), os.path.join(root, HISTORY, SKILLS)):
        if os.path.isdir(folder) and not os.path.islink(folder):
            names.update(SKILLS + '/' + base for base in os.listdir(folder) if skill_name(base))
    return [n for n in CORE_FILES if n in names] + sorted(n for n in names if n not in CORE_FILES)


def list_history(home=None, name=None):
    """Versions without their content: ``{file: [{version, author, at, reason, sha256}, ...]}``.

    With ``name``, only that file. Read-only; raises SelfError when the folder is missing or not private, when a
    change is still pending, or when a file's history and its latest marker disagree."""
    root = self_folder(home)
    check_folders(root, name)
    if pending_change(root):
        raise SelfError('A change to Motet\'s self is pending; run scan to settle it. If scan says the pending '
                        'record is damaged, a link or a folder, move .history/.pending.json aside by hand.')
    names = [check_name(name)] if name is not None else tracked_names(root)
    return {each: [{k: record[k] for k in ('version', 'author', 'at', 'reason', 'sha256')}
                   for record, _ in load_history(root, each)] for each in names}
