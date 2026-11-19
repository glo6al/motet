"""Optional local recognizer process; importing this module starts nothing."""
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

MAX_FRAME = 1024 * 1024
MAX_EVENTS = 32
MAX_TEXT = 8192
MESSAGES = {
    'dependency_missing': 'The optional dictation runtime is not installed.',
    'runtime_version': 'The optional dictation runtime needs the supported version.',
    'model_error': 'The local dictation model could not be loaded.',
    'worker_failed': 'The local dictation worker is unavailable.',
    'worker_timeout': 'The local dictation worker did not answer in time.',
    'protocol_error': 'The local dictation worker returned an invalid response.',
    'recognition_failed': 'The local recognizer could not finish this take.',
    'invalid_request': 'Invalid local dictation request.',
}


class RuntimeUnavailable(Exception):
    """A safe error code; native diagnostics and input are never exposed."""
    def __init__(self, code='worker_failed'):
        self.code = code if type(code) is str and code in MESSAGES else 'worker_failed'
        super().__init__(MESSAGES[self.code])


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate protocol key')
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError('Non-finite protocol value')


def _parse(raw):
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                       parse_constant=_invalid_constant)
    if type(value) is not dict:
        raise ValueError('Expected a protocol object')
    return value


def _finite(value, minimum=0):
    try:
        return type(value) in (float, int) and math.isfinite(value) and value >= minimum
    except (OverflowError, ValueError):
        return False


def _validate_result(value):
    if (set(value) != {'id', 'ok', 'epoch', 'events', 'commandMs'}
            or type(value['epoch']) is not int or not 0 <= value['epoch'] <= 1000000
            or not _finite(value['commandMs']) or type(value['events']) is not list
            or len(value['events']) > MAX_EVENTS):
        raise RuntimeUnavailable('protocol_error')
    for event in value['events']:
        if (type(event) is not dict
                or set(event) != {'type', 'epoch', 'lineId', 'text', 'complete', 'audioSeconds', 'decodeMs'}
                or event['type'] != 'line' or type(event['epoch']) is not int
                or event['epoch'] != value['epoch'] or type(event['lineId']) is not str
                or not 1 <= len(event['lineId']) <= 64 or type(event['text']) is not str
                or len(event['text']) > MAX_TEXT or type(event['complete']) is not bool
                or not _finite(event['audioSeconds']) or not _finite(event['decodeMs'])):
            raise RuntimeUnavailable('protocol_error')


class _Channel:
    def __init__(self):
        self.messages = queue.Queue(maxsize=2)
        self.failed = threading.Event()
        self.closed = threading.Event()


class Runtime:
    """Serialized, deadline-bounded stdio adapter around an explicit interpreter.

    The optional interpreter and model paths are supplied by the owner-state
    boundary. No dependency discovery, download or process starts in __init__.
    A timed-out child is retired before any subsequent command can run. An
    explicit start may create a fresh generation with its own response channel.
    """
    def __init__(self, python, model, worker=None, timeout=15.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('A finite bounded runtime timeout is required')
        self.python = Path(python)
        self.model = Path(model)
        self.worker = Path(worker) if worker is not None else Path(__file__).with_name('worker.py')
        self.timeout = float(timeout)
        self.load_ms = None
        self._process = None
        self._channel = None
        self._ready = False
        self._sequence = 0
        self._lock = threading.RLock()

    @property
    def ready(self):
        return bool(self._ready and self._process is not None
                    and self._process.poll() is None and self._channel is not None
                    and not self._channel.failed.is_set())

    @staticmethod
    def _environment():
        # The worker needs no owner credentials, proxy settings or user Python
        # configuration. Keep only basic platform/locale process settings.
        allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL')
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                           HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
        return environment

    @staticmethod
    def _read(process, channel):
        try:
            while not channel.closed.is_set():
                raw = process.stdout.readline(MAX_FRAME + 1)
                if not raw or len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                    raise ValueError('Invalid protocol frame')
                value = _parse(raw)
                try:
                    channel.messages.put_nowait(value)
                except queue.Full:
                    raise ValueError('Unsolicited protocol responses') from None
        except (OSError, ValueError, UnicodeError, RecursionError):
            if not channel.closed.is_set():
                channel.failed.set()
                try:
                    channel.messages.put_nowait(None)
                except queue.Full:
                    pass

    def _receive(self, request_id, deadline):
        channel = self._channel
        while True:
            if channel is None:
                raise RuntimeUnavailable('protocol_error')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeUnavailable('worker_timeout')
            try:
                value = channel.messages.get_nowait()
            except queue.Empty:
                if channel.failed.is_set():
                    raise RuntimeUnavailable('protocol_error')
                try:
                    value = channel.messages.get(timeout=remaining)
                except queue.Empty:
                    raise RuntimeUnavailable('worker_timeout') from None
            if value is None or type(value.get('id')) is not int or value['id'] != request_id:
                raise RuntimeUnavailable('protocol_error')
            return value

    def start(self):
        with self._lock:
            if self.ready:
                return
            self._retire()
            self.load_ms = None
            self._sequence = 0
            channel = _Channel()
            self._channel = channel
            try:
                self._process = subprocess.Popen(
                    [os.fspath(self.python.absolute()), '-I', '-B', os.fspath(self.worker.absolute()),
                     '--model', os.fspath(self.model.absolute())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0, env=self._environment())
            except (OSError, ValueError):
                self._retire()
                raise RuntimeUnavailable('dependency_missing') from None
            threading.Thread(target=self._read, args=(self._process, channel), daemon=True).start()
            try:
                value = self._receive(0, time.monotonic() + self.timeout)
                if value.get('ready') is not True:
                    raise RuntimeUnavailable(value.get('code'))
                if (set(value) != {'id', 'ready', 'loadMs'} or not _finite(value['loadMs'])
                        or channel.failed.is_set()):
                    raise RuntimeUnavailable('protocol_error')
                self.load_ms = value['loadMs']
                self._ready = True
            except RuntimeUnavailable:
                self._retire()
                raise

    @staticmethod
    def _write(process, raw, complete, failed):
        try:
            view = memoryview(raw)
            while view:
                written = process.stdin.write(view)
                if not written:
                    raise OSError('Worker pipe closed')
                view = view[written:]
        except (OSError, ValueError):
            failed.set()
        finally:
            complete.set()

    def call(self, action, **payload):
        if action not in ('start', 'audio', 'stop', 'cancel', 'reset') or 'id' in payload or 'action' in payload:
            raise RuntimeUnavailable('invalid_request')
        with self._lock:
            if not self.ready:
                self._retire()
                raise RuntimeUnavailable('worker_failed')
            self._sequence += 1
            try:
                raw = json.dumps({'id': self._sequence, 'action': action, **payload},
                                 ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
            except (ValueError, TypeError, OverflowError):
                raise RuntimeUnavailable('invalid_request') from None
            if len(raw) > MAX_FRAME:
                raise RuntimeUnavailable('invalid_request')
            deadline = time.monotonic() + self.timeout
            complete, failed = threading.Event(), threading.Event()
            writer = threading.Thread(target=self._write, args=(self._process, raw, complete, failed), daemon=True)
            writer.start()
            try:
                if not complete.wait(max(0, deadline - time.monotonic())):
                    raise RuntimeUnavailable('worker_timeout')
                if failed.is_set():
                    raise RuntimeUnavailable('worker_failed')
                value = self._receive(self._sequence, deadline)
                if value.get('ok') is not True:
                    if (set(value) != {'id', 'ok', 'code'} or value.get('ok') is not False
                            or type(value.get('code')) is not str or value['code'] not in MESSAGES):
                        raise RuntimeUnavailable('protocol_error')
                    raise RuntimeUnavailable(value['code'])
                _validate_result(value)
                if self._channel.failed.is_set():
                    raise RuntimeUnavailable('protocol_error')
                return {key: value[key] for key in ('epoch', 'events', 'commandMs')}
            except RuntimeUnavailable:
                # Do not leave a possibly half-consumed audio stream or a late
                # response available to the next request, even after a safe error.
                self._retire()
                raise

    def _retire(self):
        self._ready = False
        process, channel = self._process, self._channel
        self._process = None
        self._channel = None
        if channel is not None:
            channel.closed.set()
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=.5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                except OSError:
                    pass
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def close(self):
        with self._lock:
            self._retire()
