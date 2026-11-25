"""Explicit, verified setup for the optional English model; no import-time I/O.

POSIX keeps its checks: private (this user's, no group or other bits) regular files with one name and folders,
found without links. Windows applies the same rules through private_storage: every model file, folder and the
setup lock is opened through the no-link walk and checked private on its handle; files and staging folders are
created private from the start; the setup lock is a private lock file. Windows may briefly hold a just-written file
(Microsoft Defender scans it), so renames and removals of whole model folders are retried for a few seconds there.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MODEL_BYTES = 142465862
CHUNK_BYTES = 65536
_MODEL_URL = 'https://download.moonshine.ai/model/small-streaming-en/quantized_26_11_02/'


def _storage():
    """private_storage from Motet's src folder, imported on first use."""
    src = str(Path(__file__).resolve().parents[2] / 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    import private_storage
    return private_storage


def _windows():
    return _storage().WIN


def _binary(fd):
    """Windows: binary mode, so model bytes are written as they are (the C runtime's default is text mode)."""
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(fd, os.O_BINARY)
    return fd


def _create(destination):
    """A new file for model bytes: POSIX exactly as before; Windows private from the start (this user and SYSTEM)."""
    if _windows():
        return _binary(_storage().create_private_file(str(destination)))
    return os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)


# ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION: another program (a scanner) has the file open for now.
_SCANNER_BUSY = (32, 33)


def _windows_retry(action, *args):
    """Windows: a rename or removal that a scanner briefly blocks (a sharing or lock violation) is tried again for
    up to ten seconds. Any other error, access denied or an existing target included, raises at once."""
    deadline = time.monotonic() + 10
    while True:
        try:
            return action(*args)
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in _SCANNER_BUSY or time.monotonic() > deadline:
                raise
            time.sleep(.25)


def _unlink(path):
    """POSIX: Path.unlink, as before. Windows: retried while a scanner holds the file."""
    if _windows():
        return _windows_retry(path.unlink)
    return path.unlink()


class AssetsError(Exception):
    """A setup error whose code/message are safe to show without local paths."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _checked_info(info):
    try:
        if (type(info) is not dict or info.get('schemaVersion') != 1
                or info.get('packageVersion') != '0.1.6'
                or info.get('modelId') != 'small-streaming-en/quantized_26_11_02'
                or info.get('licence') != 'MIT'
                or type(info.get('bytes')) is not int
                or not 0 < info['bytes'] < 512 * 1024 * 1024
                or type(info.get('files')) is not list or not 1 <= len(info['files']) <= 16):
            raise ValueError()
        names = set()
        for item in info['files']:
            name = item['filename']
            if (type(name) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', name)
                    or name in names or type(item['size']) is not int
                    or not 0 < item['size'] < 256 * 1024 * 1024
                    or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                    or item['url'] != _MODEL_URL + name or item.get('licence') != 'MIT'):
                raise ValueError()
            names.add(name)
        if sum(item['size'] for item in info['files']) != info['bytes']:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise AssetsError('invalid_manifest', 'The model manifest is invalid.') from None
    return info


def model_info():
    """Return the pinned model manifest and its recorded provenance. No network I/O."""
    try:
        path = Path(__file__).with_name('model_manifest.json')
        return _checked_info(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, UnicodeError, ValueError):
        raise AssetsError('invalid_manifest', 'The model manifest is unavailable.') from None


def _private(item, directory=False):
    if ((directory and not stat.S_ISDIR(item.st_mode))
            or (not directory and not stat.S_ISREG(item.st_mode))
            or (os.name == 'posix' and (item.st_uid != os.getuid() or item.st_mode & 0o077))
            or (not directory and item.st_nlink != 1)):
        raise AssetsError('unsafe_model_path', 'Model setup requires private regular files and directories.')


def _windows_path(path):
    """Windows: the parent folder (and the model folder, if there) reached with no link and private."""
    ps = _storage()
    try:
        with ps.open_folder(str(path.parent), private=True) as parent:
            try:
                parent.folder(path.name, private=True).close()
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        raise AssetsError('model_missing', 'Create the private helper state folder before model setup.') from None
    except OSError:
        raise AssetsError('unsafe_model_path', 'Model setup requires private regular files and directories.') from None
    return path


def _path(model_dir):
    # The caller supplies its canonical private state directory. Refusing links
    # also keeps an explicit setup action from writing through a replaced path.
    path = Path(os.path.abspath(os.fspath(model_dir)))
    if path == path.parent:
        raise AssetsError('unsafe_model_path', 'Choose a model folder inside private helper state.')
    if _windows():
        return _windows_path(path)
    current = Path(path.anchor)
    try:
        for component in path.parts[1:-1]:
            current /= component
            mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise AssetsError('unsafe_model_path', 'Model folder ancestors must be directories without links.')
        _private(path.parent.lstat(), directory=True)
        if path.exists() or path.is_symlink():
            _private(path.lstat(), directory=True)
    except FileNotFoundError:
        raise AssetsError('model_missing', 'Create the private helper state folder before model setup.') from None
    except OSError:
        raise AssetsError('unsafe_model_path', 'The private model folder is unavailable.') from None
    return path


def _open_read(path):
    if _windows():
        # One name, no link on the way, private on its handle; held folders cannot change under the open.
        try:
            fd = _storage().open_no_links(str(path), single_link=True, private=True)
        except FileNotFoundError:
            raise AssetsError('model_missing', 'The English model has not been installed.') from None
        except OSError:
            raise AssetsError('unsafe_model_path', 'Model files must be private regular files without links.') from None
        try:
            return os.fdopen(fd, 'rb'), os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    try:
        before = path.lstat()
        _private(before)
        fd = os.open(str(path), flags)
        try:
            after = os.fstat(fd)
            _private(after)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise AssetsError('unsafe_model_path', 'The model file changed during inspection.')
            return os.fdopen(fd, 'rb'), after
        except BaseException:
            os.close(fd)
            raise
    except FileNotFoundError:
        raise AssetsError('model_missing', 'The English model has not been installed.') from None
    except OSError:
        raise AssetsError('unsafe_model_path', 'Model files must be readable regular files without links.') from None


def _copy_verified(source, item, destination=None):
    """Verify a model file and optionally copy it into the unpublished bundle."""
    handle, metadata = _open_read(source)
    if metadata.st_size != item['size']:
        handle.close()
        raise AssetsError('model_integrity', 'An English model file has an unexpected size.')
    digest = hashlib.sha256()
    count = 0
    output = None
    try:
        if destination is not None:
            output = os.fdopen(_create(destination), 'wb')
        with handle:
            while True:
                chunk = handle.read(min(CHUNK_BYTES, item['size'] + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > item['size']:
                    raise AssetsError('model_integrity', 'An English model file changed during verification.')
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        if count != item['size'] or digest.hexdigest() != item['sha256']:
            raise AssetsError('model_integrity', 'An English model file failed its integrity check.')
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    finally:
        handle.close()
        if output is not None:
            output.close()


def _windows_existing_files(path, info):
    ps = _storage()
    known = {item['filename'] for item in info['files']}
    try:
        with ps.open_folder(str(path), private=True, listing=True) as held:
            for name in held.names():
                if name == '.DS_Store':
                    # Finder metadata copied from a Mac is not model input: ignored only as a regular file with one
                    # name reached without a link, never as a link, folder or substitute for a model file.
                    os.close(held.open(name, single_link=True))
                    continue
                if name not in known:
                    raise AssetsError('unsafe_model_path', 'The model folder contains unrelated files.')
                os.close(held.open(name, single_link=True, private=True))
    except FileNotFoundError:
        return
    except AssetsError:
        raise
    except OSError:
        raise AssetsError('unsafe_model_path', 'The model folder cannot be inspected safely.') from None


def _existing_files(path, info):
    if _windows():
        return _windows_existing_files(path, info)
    if not path.exists():
        return
    known = {item['filename'] for item in info['files']}
    try:
        for entry in path.iterdir():
            if entry.name == '.DS_Store':
                metadata = entry.lstat()
                # Finder metadata is not model input. Ignore only this exact
                # regular owner file, never links, special files or substitutes
                # for required manifest entries.
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or os.name == 'posix' and metadata.st_uid != os.getuid()):
                    raise AssetsError('unsafe_model_path', 'Unsafe model folder metadata.')
                continue
            if entry.name not in known:
                raise AssetsError('unsafe_model_path', 'The model folder contains unrelated files.')
            _private(entry.lstat())
    except OSError:
        raise AssetsError('unsafe_model_path', 'The model folder cannot be inspected safely.') from None


def validate_model(model_dir):
    """Verify the selected model, raising AssetsError for absence or corruption."""
    info = _checked_info(model_info())
    path = _path(model_dir)
    if not path.exists():
        raise AssetsError('model_missing', 'The English model has not been installed.')
    _existing_files(path, info)
    for item in info['files']:
        _copy_verified(path / item['filename'], item)
    return True


@contextlib.contextmanager
def _windows_download_lock(path):
    """Windows: a private lock file (no link, this user's alone) beside the model folder, locked without waiting."""
    ps = _storage()
    fd = None
    try:
        try:
            fd = ps.open_lock_file(str(path.parent / ('.' + path.name + '.download.lock')))
            held = ps.try_lock(fd)
        except OSError:
            raise AssetsError('unsafe_model_path', 'The model setup lock is unavailable.') from None
        if not held:
            raise AssetsError('download_busy', 'Another model setup is already running.')
        yield
    finally:
        if fd is not None:
            ps.close_lock(fd)


@contextlib.contextmanager
def _download_lock(path):
    """OS-held lock releases on process exit; its private file contains no key."""
    if _windows():
        with _windows_download_lock(path):
            yield
        return
    lock_path = path.parent / ('.' + path.name + '.download.lock')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = None
    try:
        if lock_path.exists() or lock_path.is_symlink():
            _private(lock_path.lstat())
        fd = os.open(str(lock_path), flags, 0o600)
        _private(os.fstat(fd))
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AssetsError('download_busy', 'Another model setup is already running.') from None
        yield
    except OSError:
        raise AssetsError('unsafe_model_path', 'The model setup lock is unavailable.') from None
    finally:
        if fd is not None:
            os.close(fd)


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != 'https' or target.netloc != 'download.moonshine.ai'
                or target.username is not None or target.password is not None):
            raise AssetsError('download_failed', 'The model service returned an unexpected redirect.')
        return super().redirect_request(request, response, code, message, headers, newurl)


def _opener():
    # Environment proxy settings must never reroute model setup implicitly.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameOriginRedirect())


def _fetch(item, destination, opener, progress):
    request = urllib.request.Request(item['url'], headers={'User-Agent': 'Motet-dictation (moonshine-voice/0.1.6)'})
    digest = hashlib.sha256()
    count = 0
    try:
        with opener.open(request, timeout=30) as response:
            final = response.geturl()
            parsed = urllib.parse.urlsplit(final)
            if parsed.scheme != 'https' or parsed.netloc != 'download.moonshine.ai':
                raise AssetsError('download_failed', 'The model service returned an unexpected location.')
            with os.fdopen(_create(destination), 'wb') as output:
                while True:
                    chunk = response.read(min(CHUNK_BYTES, item['size'] + 1 - count))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > item['size']:
                        raise AssetsError('download_integrity', 'The downloaded model file is larger than expected.')
                    digest.update(chunk)
                    output.write(chunk)
                    progress(count)
                if count != item['size'] or digest.hexdigest() != item['sha256']:
                    raise AssetsError('download_integrity', 'The downloaded model file failed its integrity check.')
                output.flush()
                os.fsync(output.fileno())
    except AssetsError:
        raise
    except Exception:
        raise AssetsError('download_failed', 'The English model download did not complete. Retry setup.') from None


def _staging(path):
    """A new private staging folder beside the model folder. POSIX: mkdtemp, then 0700, as before. Windows: a
    unique name made private from the start."""
    if not _windows():
        stage = Path(tempfile.mkdtemp(prefix='.' + path.name + '.staging-', dir=str(path.parent)))
        os.chmod(str(stage), 0o700)
        return stage
    for _ in range(100):
        stage = path.parent / ('.' + path.name + '.staging-' + secrets.token_hex(8))
        try:
            _storage().make_private_folder(str(stage))
            return stage
        except FileExistsError:
            continue
    raise AssetsError('download_failed', 'The English model setup did not complete. Retry setup.')


def _backup_name(path):
    """An unused name for the previous model folder. POSIX: mkdtemp's, released again, as before."""
    if not _windows():
        backup = Path(tempfile.mkdtemp(prefix='.' + path.name + '.previous-', dir=str(path.parent)))
        backup.rmdir()
        return backup
    return path.parent / ('.' + path.name + '.previous-' + secrets.token_hex(8))


def _replace(source, target):
    if _windows():
        return _windows_retry(os.replace, str(source), str(target))
    return os.replace(str(source), str(target))


def _remove_tree(folder):
    """POSIX: shutil.rmtree, as before. Windows: retried while a scanner holds a file; staging that a later setup
    does not trust is left behind if it still cannot be removed."""
    if not _windows():
        shutil.rmtree(str(folder))
        return
    try:
        _windows_retry(shutil.rmtree, str(folder))
    except OSError:
        pass


def download_model(model_dir, accept_bytes, progress=None, opener=None):
    """Install a complete verified bundle only after explicit size confirmation.

    Existing matching files are copied into an unpublished staging folder. No
    caller sees a mixture of old and new files. A failed transfer leaves the
    previous folder untouched. The fixed manifest never upgrades a valid model.
    """
    info = _checked_info(model_info())
    if type(accept_bytes) is not int or accept_bytes != info['bytes']:
        raise AssetsError('confirmation_required', 'Confirm the exact model download size before setup.')
    path = _path(model_dir)
    progress = progress or (lambda event: None)
    with _download_lock(path):
        _existing_files(path, info)
        if path.exists():
            try:
                validate_model(path)
            except AssetsError as exc:
                if exc.code not in ('model_missing', 'model_integrity'):
                    raise
            else:
                progress({'phase': 'complete', 'file': None, 'completedBytes': info['bytes'],
                          'totalBytes': info['bytes'], 'downloadedBytes': 0})
                return {'state': 'installed', 'bytes': info['bytes'], 'downloadedBytes': 0,
                        'modelId': info['modelId']}
        stage = _staging(path)
        backup = None
        completed = 0
        downloaded = 0
        active = False
        try:
            for item in info['files']:
                def report(received, phase='downloading'):
                    progress({'phase': phase, 'file': item['filename'],
                              'completedBytes': min(completed + received, info['bytes'] - 1),
                              'totalBytes': info['bytes'], 'downloadedBytes': downloaded + received})
                target = stage / item['filename']
                reused = False
                if path.exists():
                    try:
                        _copy_verified(path / item['filename'], item, target)
                        reused = True
                    except AssetsError as exc:
                        if exc.code not in ('model_missing', 'model_integrity'):
                            raise
                        if target.exists():
                            _unlink(target)
                if not reused:
                    report(0)
                    _fetch(item, target, opener or _opener(), report)
                    downloaded += item['size']
                completed += item['size']
                progress({'phase': 'verified', 'file': item['filename'],
                          'completedBytes': min(completed, info['bytes'] - 1),
                          'totalBytes': info['bytes'], 'downloadedBytes': downloaded})
            # Recheck before publishing; this also detects any unexpected local
            # mutation of a staged file while the transfer was in progress.
            for item in info['files']:
                _copy_verified(stage / item['filename'], item)
            if path.exists():
                # A valid selected bundle never needs replacement or an upgrade.
                try:
                    validate_model(path)
                    active = True
                except AssetsError as exc:
                    if exc.code not in ('model_missing', 'model_integrity'):
                        raise
                if not active:
                    backup = _backup_name(path)
                    _replace(path, backup)
            if not active:
                try:
                    _replace(stage, path)
                    active = True
                except BaseException:
                    if backup is not None:
                        _replace(backup, path)
                        backup = None
                    raise
            progress({'phase': 'complete', 'file': None, 'completedBytes': info['bytes'],
                      'totalBytes': info['bytes'], 'downloadedBytes': downloaded})
            return {'state': 'installed', 'bytes': info['bytes'], 'downloadedBytes': downloaded,
                    'modelId': info['modelId']}
        except AssetsError:
            raise
        except Exception:
            raise AssetsError('download_failed', 'The English model setup did not complete. Retry setup.') from None
        finally:
            if stage.exists():
                _remove_tree(stage)
            if backup is not None and backup.exists() and active:
                _remove_tree(backup)
