"""Explicit, verified setup for the optional English model; no import-time I/O."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MODEL_BYTES = 142465862
CHUNK_BYTES = 65536
_MODEL_URL = 'https://download.moonshine.ai/model/small-streaming-en/quantized_26_11_02/'


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
    """Return published model metadata. This never contacts a download service."""
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


def _path(model_dir):
    # The caller supplies its canonical private state directory. Refusing links
    # also keeps an explicit setup action from writing through a replaced path.
    path = Path(os.path.abspath(os.fspath(model_dir)))
    if path == path.parent:
        raise AssetsError('unsafe_model_path', 'Choose a model folder inside private helper state.')
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
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            output = os.fdopen(fd, 'wb')
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
        if output is not None:
            output.close()


def _existing_files(path, info):
    if not path.exists():
        return
    known = {item['filename'] for item in info['files']}
    try:
        for entry in path.iterdir():
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
def _download_lock(path):
    """OS-held lock releases on process exit; its private file contains no key."""
    lock_path = path.parent / ('.' + path.name + '.download.lock')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = None
    locked = False
    try:
        if lock_path.exists() or lock_path.is_symlink():
            _private(lock_path.lstat())
        fd = os.open(str(lock_path), flags, 0o600)
        _private(os.fstat(fd))
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AssetsError('download_busy', 'Another model setup is already running.') from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AssetsError('download_busy', 'Another model setup is already running.') from None
        locked = True
        yield
    except OSError:
        raise AssetsError('unsafe_model_path', 'The model setup lock is unavailable.') from None
    finally:
        if fd is not None:
            if locked and os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
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
    request = urllib.request.Request(item['url'], headers={'User-Agent': 'moonshine-voice/0.1.6'})
    digest = hashlib.sha256()
    count = 0
    try:
        with opener.open(request, timeout=30) as response:
            final = response.geturl()
            parsed = urllib.parse.urlsplit(final)
            if parsed.scheme != 'https' or parsed.netloc != 'download.moonshine.ai':
                raise AssetsError('download_failed', 'The model service returned an unexpected location.')
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
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
        stage = Path(tempfile.mkdtemp(prefix='.' + path.name + '.staging-', dir=str(path.parent)))
        os.chmod(str(stage), 0o700)
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
                            target.unlink()
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
                    backup = Path(tempfile.mkdtemp(prefix='.' + path.name + '.previous-', dir=str(path.parent)))
                    backup.rmdir()
                    os.replace(str(path), str(backup))
            if not active:
                try:
                    os.replace(str(stage), str(path))
                    active = True
                except BaseException:
                    if backup is not None:
                        os.replace(str(backup), str(path))
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
                shutil.rmtree(str(stage))
            if backup is not None and backup.exists() and active:
                shutil.rmtree(str(backup))
