"""The owner's writer for Motet's self folder: install, record edits, restore. Used only by scripts/motet_self.py.

Nothing in the server, Motet's tools or an engine imports this module; server code reads history only through
motet_self_history, which cannot write. History is append-only: a version record, once written, is never changed or
removed, and a restore is recorded as a new version.

Each change is all or nothing, under one lock (``.history/.lock``):

1. any change an earlier run left half done is settled (see ``_recover``);
2. the new version record is written to ``.history/.pending.json`` (a private temporary file, flushed, then renamed);
3. for a save or restore, the working file is replaced by rename: this is the moment the save happens;
4. the record is written as ``.history/<file>/v<N>.json`` and ``latest.json`` is moved to it;
5. the pending record is removed.

If a save stops before step 3, the working file and history are untouched and the pending record is dropped at the
next change. If it stops after step 3, the next change finds the working file (or the new record) matching the
pending record and finishes steps 4 and 5. Every file and folder is created private from the start
(private_storage), and anything that is not private is refused, never repaired.
"""
from contextlib import contextmanager
import json
import os
import secrets
import shutil
import stat
import time

import private_storage
from motet_self_history import (AUTHORS, CORE_FILES, HISTORY, LATEST, MAX_FILE_BYTES, MAX_REASON, MAX_RECORD_BYTES,
                                PENDING, SCHEMA, SKILLS, SelfError, binary, check_folders, check_name, decode_text,
                                digest, history_dir, load_history, parse_record, read_private, self_folder, sha256,
                                skill_name, tracked_names, version_file, working_path)

LOCK = '.lock'
# The repository's template folder holds these; the owner's prepared copies never do.
TEMPLATE_ONLY = ('INSTRUCTIONS.md', 'VOCABULARY.next.md')


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _fsync_folder(folder):
    if os.name == 'nt':
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _retry(operation, source, target):
    for tries in range(5):      # Windows refuses while another program (an editor, a scanner) has the file open
        try:
            operation(source, target)
            return
        except PermissionError:
            if os.name != 'nt' or tries == 4:
                raise
            time.sleep(0.1)


def _make_folder(path):
    private_storage.make_private_folder(path)
    _fsync_folder(os.path.dirname(path))


def _ensure_folder(path):
    if not os.path.lexists(path):
        _make_folder(path)
    private_storage.check_private(path, directory=True)


def _write(folder, name, data, new=False):
    """Write ``data`` to ``folder/name`` completely or not at all: a private temporary file, flushed, then renamed.
    With ``new`` an existing name is refused, so a version record is never overwritten."""
    target = os.path.join(folder, name)
    fd, temporary = private_storage.create_private_temp(folder, '.tmp-')
    try:
        try:
            binary(fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            private_storage.sync_file(fd)
        finally:
            os.close(fd)
        if os.path.lexists(target):
            if new:
                raise SelfError('A history record already exists; history is never rewritten.')
            private_storage.check_private(target, single_link=True)
        _retry(os.replace, temporary, target)
        temporary = None
        _fsync_folder(folder)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _record(name, number, previous, author, reason, content):
    if author not in AUTHORS:
        raise SelfError('A change is recorded as made by the owner or by Motet.')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON or not reason.isprintable():
        raise SelfError('Give a short reason on one line.')
    record = {'schema': SCHEMA, 'file': name, 'version': number, 'previous': previous, 'author': author,
              'at': _now(), 'reason': reason.strip(), 'sha256': sha256(content), 'content': content}
    data = json.dumps(record, ensure_ascii=False, indent=1).encode('utf-8') + b'\n'
    if len(data) > MAX_RECORD_BYTES:
        raise SelfError('That file is larger than Motet allows.')
    return data


def _latest(name, number, data):
    return json.dumps({'file': name, 'version': number, 'record': digest(data)}).encode('utf-8') + b'\n'


def _check_content(content):
    if not isinstance(content, str):
        raise SelfError('A self file is text.')
    raw = content.encode('utf-8')
    if len(raw) > MAX_FILE_BYTES:
        raise SelfError('That file is larger than Motet allows.')
    return decode_text(raw), raw


def _current(root, name):
    """The working file's text, or None when it does not exist."""
    path = working_path(root, name)
    if not os.path.lexists(path):
        return None
    return decode_text(read_private(path, MAX_FILE_BYTES))


def _history_folder(root, name):
    if '/' in name:
        _ensure_folder(os.path.join(root, HISTORY, SKILLS))
    folder = history_dir(root, name)
    _ensure_folder(folder)
    return folder


def _drop_pending(history):
    """The change is complete or never happened; a leftover pending record is settled at the next change."""
    try:
        os.unlink(os.path.join(history, PENDING))
        _fsync_folder(history)
    except OSError:
        pass


def _commit(root, name, records, content, author, reason, working=None):
    """Record ``content`` as the next version; with ``working`` (bytes) also replace the working file."""
    number = len(records) + 1
    data = _record(name, number, records[-1][1] if records else None, author, reason, content)
    history = os.path.join(root, HISTORY)
    folder = _history_folder(root, name)
    if os.path.lexists(os.path.join(history, PENDING)):
        raise SelfError('A pending record from the last change could not be cleared (.history/.pending.json); that '
                        'change was saved. Run the command again to settle it, or move .history/.pending.json aside '
                        'by hand if this keeps happening.')
    _write(history, PENDING, data, new=True)
    if working is not None:
        place = os.path.dirname(working_path(root, name))
        _ensure_folder(place)
        _write(place, os.path.basename(working_path(root, name)), working)     # the save happens here
    _write(folder, version_file(number), data, new=True)
    _write(folder, LATEST, _latest(name, number, data))
    _drop_pending(history)
    return number


def _recover(root):
    """Settle a change that stopped partway (see the module note). Never removes or rewrites a committed record."""
    history = os.path.join(root, HISTORY)
    pending = os.path.join(history, PENDING)
    if not os.path.lexists(pending):
        return
    if os.path.isdir(pending) and not os.path.islink(pending):
        raise SelfError('A folder sits where Motet keeps an unfinished change (.history/.pending.json). '
                        'Move it away by hand; nothing was changed.')
    try:
        private_storage.check_private(pending, single_link=True)
    except private_storage.PrivateStorageError:
        raise SelfError('The unfinished change in .history/.pending.json is a link or not private; it was left as it '
                        'is and nothing was changed. Move .history/.pending.json aside by hand.') from None
    raw = read_private(pending, MAX_RECORD_BYTES)
    try:
        value = json.loads(raw.decode('utf-8'))
        name = check_name(value['file'])
        record = parse_record(raw, name, value['version'])
    except (SelfError, ValueError, UnicodeError, TypeError, KeyError):
        raise SelfError('The unfinished change in .history/.pending.json is damaged; it was left as it is and '
                        'nothing was changed. Move .history/.pending.json aside by hand.') from None
    number = record['version']
    folder = _history_folder(root, name)
    path = os.path.join(folder, version_file(number))
    if os.path.lexists(path):
        if read_private(path, MAX_RECORD_BYTES) != raw:
            raise SelfError('The unfinished change does not match the history of %s; nothing was changed.' % name)
        _write(folder, LATEST, _latest(name, number, raw))       # the record was written; finish its marker
    else:
        records = load_history(root, name)
        before = records[-1][1] if records else None
        current = _current(root, name)
        if len(records) == number - 1 and record['previous'] == before and current is not None \
                and sha256(current) == record['sha256']:
            _write(folder, version_file(number), raw, new=True)
            _write(folder, LATEST, _latest(name, number, raw))
    os.unlink(pending)
    _fsync_folder(history)
    load_history(root, name)


@contextmanager
def _locked(root):
    check_folders(root)
    try:
        fd = private_storage.open_lock_file(os.path.join(root, HISTORY, LOCK))
    except OSError:
        raise SelfError('Motet\'s self folder could not be locked; nothing was changed.') from None
    try:
        if not private_storage.try_lock(fd):
            raise SelfError('Another change to Motet\'s self is in progress; nothing was changed. Try again.')
        _recover(root)
        yield
    finally:
        private_storage.close_lock(fd)


def settle(home=None):
    """Finish or drop a change that stopped partway, so history can be read."""
    with _locked(self_folder(home)):
        pass


def _save(root, name, content, author, reason):
    text, raw = _check_content(content)
    records = load_history(root, name)
    current = _current(root, name)
    if current is not None and (not records or sha256(current) != records[-1][0]['sha256']):
        _commit(root, name, records, current, 'owner', 'edited directly by the owner')    # keep an unrecorded edit
        records = load_history(root, name)
    return _commit(root, name, records, text, author, reason, working=raw)


def save(name, content, author, reason, home=None):
    """Replace a self file and record the new version. Returns its number."""
    name = check_name(name)
    root = self_folder(home)
    with _locked(root):
        check_folders(root, name)
        return _save(root, name, content, author, reason)


def restore(name, number, home=None):
    """Make an earlier version current again, recorded as a new version "restored from vN"."""
    name = check_name(name)
    root = self_folder(home)
    with _locked(root):
        check_folders(root, name)
        records = load_history(root, name)
        if not 1 <= number <= len(records):
            raise SelfError('There is no version %d of %s.' % (number, name))
        return _save(root, name, records[number - 1][0]['content'], 'owner', 'restored from v%d' % number)


def scan(home=None):
    """Record every self file whose content differs from its last recorded version as an owner edit.

    Every file is checked before anything is recorded, so a refusal records nothing. Returns ``[(file, status)]``
    with status 'recorded vN', 'new vN', 'unchanged' or 'missing'."""
    root = self_folder(home)
    with _locked(root):
        plan = []
        for name in tracked_names(root):
            check_folders(root, name)
            records = load_history(root, name)
            current = _current(root, name)
            if current is None:
                plan.append((name, 'missing', None))
            elif not records:
                plan.append((name, 'new', current))
            elif sha256(current) != records[-1][0]['sha256']:
                plan.append((name, 'recorded', current))
            else:
                plan.append((name, 'unchanged', None))
        results, recorded = [], []
        for name, status, current in plan:
            if current is not None:
                try:
                    number = _commit(root, name, load_history(root, name), current, 'owner',
                                     'found by scan' if status == 'new' else 'edited directly by the owner')
                except (SelfError, OSError) as e:
                    raise SelfError('%s Recorded before it stopped: %s.' % (
                        str(e) if isinstance(e, SelfError) else 'A file could not be written.',
                        ', '.join(recorded) or 'nothing')) from None
                recorded.append(name)
                status = '%s v%d' % (status, number)
            results.append((name, status))
    return results


def _source_files(source):
    """The owner's prepared copies: the five core files (all required) and skills/*.md."""
    def plain(path):
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & private_storage.REPARSE_POINT

    for name in TEMPLATE_ONLY:
        if os.path.lexists(os.path.join(source, name)):
            raise SelfError('That folder holds %s, so it looks like the repository\'s motet/ templates. Use the '
                            'prepared copies instead; nothing was installed.' % name)
    names = []
    for name in CORE_FILES:
        if not plain(os.path.join(source, name)):
            raise SelfError('The prepared copies need %s as a plain file; nothing was installed.' % name)
        names.append(name)
    skills = os.path.join(source, SKILLS)
    if os.path.isdir(skills) and not os.path.islink(skills):
        for base in sorted(os.listdir(skills)):
            if skill_name(base) and plain(os.path.join(skills, base)):
                names.append(SKILLS + '/' + base)
    files = {}
    for name in names:
        with open(os.path.join(source, *name.split('/')), 'rb') as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise SelfError('%s is larger than Motet allows; nothing was installed.' % name)
        files[name] = decode_text(raw)
    return files


def init(source, home=None):
    """Create the private self folder from the owner's prepared copies, each recorded as version 1.

    Refuses an existing folder. The folder is built under a temporary name next to it and renamed into place, so it
    appears complete or not at all. Returns the installed file names."""
    root = self_folder(home)
    live = os.path.dirname(root)
    if os.path.lexists(root):
        raise SelfError('Motet\'s self folder already exists; init never overwrites it. Nothing was changed.')
    files = _source_files(source)
    try:
        _ensure_folder(live)
    except OSError:
        raise SelfError('~/.live-room is not a private folder owned by you; nothing was installed.') from None
    building = os.path.join(live, '.motet-self-init-' + secrets.token_hex(6))
    try:
        _make_folder(building)
        for folder in (SKILLS, HISTORY):
            _make_folder(os.path.join(building, folder))
        for name, text in files.items():
            _write(os.path.dirname(working_path(building, name)), name.split('/')[-1], text.encode('utf-8'))
            data = _record(name, 1, None, 'owner', 'installed by init', text)
            folder = _history_folder(building, name)
            _write(folder, version_file(1), data, new=True)
            _write(folder, LATEST, _latest(name, 1, data))
        if os.path.lexists(root):
            raise SelfError('Motet\'s self folder appeared while installing; nothing was changed.')
        _retry(os.rename, building, root)
        building = None
        _fsync_folder(live)
    finally:
        if building is not None:
            shutil.rmtree(building, ignore_errors=True)
    return list(files)
