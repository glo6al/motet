"""Isolated Moonshine worker. Audio and text are never persisted by this code."""
import argparse
from array import array
import base64
import binascii
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

SUPPORTED_VERSION = '0.1.6'
MAX_FRAME = 1024 * 1024
MAX_PCM = 64000
MAX_EVENTS = 32
MAX_TEXT = 8192


class WorkerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _network_guard(event, _args):
    # This rejects Python network operations, not sockets opened inside native
    # code. The recognizer loads explicit local assets and no download API.
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                 'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg'):
        raise WorkerError('recognition_failed')


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate protocol key')
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError('Non-finite protocol value')


def parse_request(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                           parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerError('invalid_request') from None
    if type(value) is not dict or type(value.get('id')) is not int or value['id'] < 1:
        raise WorkerError('invalid_request')
    return value


def validate_request(value):
    action = value.get('action')
    if action == 'start':
        if (set(value) != {'id', 'action', 'interval'} or type(value['interval']) not in (float, int)
                or value['interval'] not in (.25, .5)):
            raise WorkerError('invalid_request')
    elif action == 'audio':
        if set(value) != {'id', 'action', 'pcm'} or type(value['pcm']) is not str or len(value['pcm']) > 85336:
            raise WorkerError('invalid_request')
    elif action in ('stop', 'cancel', 'reset'):
        if set(value) != {'id', 'action'}:
            raise WorkerError('invalid_request')
    else:
        raise WorkerError('invalid_request')
    return action


def decode_pcm(value):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise WorkerError('invalid_request') from None
    if not raw or len(raw) > MAX_PCM or len(raw) % 4:
        raise WorkerError('invalid_request')
    samples = array('f')
    samples.frombytes(raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    if any(not math.isfinite(sample) or abs(sample) > 1 for sample in samples):
        raise WorkerError('invalid_request')
    return samples


class RecognitionEngine:
    def __init__(self, transcriber, changed_type, completed_type, error_type):
        self.transcriber = transcriber
        self.changed_type = changed_type
        self.completed_type = completed_type
        self.error_type = error_type
        self.stream = None
        self.epoch = 0
        self.audio_seconds = 0.0
        self.interval = .25
        self.events = []
        self.callback_error = False

    def close_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.remove_all_listeners()
            finally:
                stream.close()

    def new_stream(self):
        stream = self.transcriber.create_stream(update_interval=self.interval)
        self.stream = stream
        epoch = self.epoch

        def listener(event):
            if self.stream is not stream or self.epoch != epoch:
                return
            try:
                if isinstance(event, self.error_type):
                    self.callback_error = True
                elif isinstance(event, (self.changed_type, self.completed_type)):
                    line = event.line
                    if type(line.text) is not str or len(line.text) > MAX_TEXT or len(self.events) >= MAX_EVENTS:
                        self.callback_error = True
                        return
                    self.events.append({'type': 'line', 'epoch': epoch, 'lineId': str(line.line_id),
                                        'text': line.text, 'complete': isinstance(event, self.completed_type),
                                        'audioSeconds': round(self.audio_seconds, 6)})
            except Exception:
                # The binding catches listener exceptions. Carry this flag back
                # through the response rather than raising inside the callback.
                self.callback_error = True

        stream.add_listener(listener)
        stream.start()

    def handle(self, request):
        action = validate_request(request)
        self.events = []
        self.callback_error = False
        began = time.monotonic()
        try:
            if action == 'start':
                self.close_stream()
                self.interval = request['interval']
                self.epoch = 1
                self.audio_seconds = 0.0
                self.new_stream()
            elif action == 'audio':
                if self.stream is None:
                    raise WorkerError('invalid_request')
                samples = decode_pcm(request['pcm'])
                self.audio_seconds += len(samples) / 16000
                self.stream.add_audio(samples, sample_rate=16000)
            elif action == 'reset':
                if self.stream is None:
                    raise WorkerError('invalid_request')
                self.close_stream()
                self.epoch += 1
                self.new_stream()
            elif action == 'stop':
                if self.stream is not None:
                    self.stream.stop()
                self.close_stream()
            elif action == 'cancel':
                self.close_stream()
            if self.callback_error:
                raise WorkerError('recognition_failed')
            elapsed = round((time.monotonic() - began) * 1000, 3)
            for event in self.events:
                event['decodeMs'] = elapsed
            return {'epoch': self.epoch, 'events': self.events, 'commandMs': elapsed}
        except Exception as error:
            try:
                self.close_stream()
            except Exception:
                pass
            if isinstance(error, WorkerError):
                raise
            raise WorkerError('recognition_failed') from None

    def close(self):
        try:
            self.close_stream()
        finally:
            self.transcriber.close()


def load_engine(model):
    # This release eagerly evaluates a union annotation in its transcriber
    # module, despite the upstream package metadata advertising Python >=3.8.
    # The supervisor stays stdlib-only on Python 3.9; its optional worker does not.
    if sys.version_info < (3, 10):
        raise WorkerError('runtime_version')
    try:
        version = importlib.metadata.version('moonshine-voice')
    except importlib.metadata.PackageNotFoundError:
        raise WorkerError('dependency_missing') from None
    if version != SUPPORTED_VERSION:
        raise WorkerError('runtime_version')
    try:
        from moonshine_voice import Transcriber, ModelArch
        from moonshine_voice.transcriber import LineTextChanged, LineCompleted, Error
    except (ImportError, OSError):
        raise WorkerError('dependency_missing') from None
    try:
        transcriber = Transcriber(model_path=model, model_arch=ModelArch.SMALL_STREAMING, update_interval=.25)
    except Exception:
        raise WorkerError('model_error') from None
    return RecognitionEngine(transcriber, LineTextChanged, LineCompleted, Error)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--model', type=Path, required=True)
    args = parser.parse_args()
    # A dedicated descriptor keeps native stdout away from the JSON protocol.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def respond(value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
        if len(raw.encode()) > MAX_FRAME:
            raise WorkerError('recognition_failed')
        protocol.write(raw)
        protocol.flush()

    sys.addaudithook(_network_guard)
    engine = None
    try:
        began = time.monotonic()
        engine = load_engine(args.model)
        respond({'id': 0, 'ready': True, 'loadMs': round((time.monotonic() - began) * 1000, 3)})
    except Exception as error:
        respond({'id': 0, 'ready': False,
                 'code': error.code if isinstance(error, WorkerError) else 'worker_failed'})
        return 2
    try:
        while True:
            raw = sys.stdin.buffer.readline(MAX_FRAME + 1)
            if not raw:
                return 0
            if len(raw) > MAX_FRAME or not raw.endswith(b'\n'):
                return 2
            request_id = None
            try:
                request = parse_request(raw)
                request_id = request['id']
                result = engine.handle(request)
                respond({'id': request_id, 'ok': True, **result})
            except Exception as error:
                if request_id is None:
                    return 2
                try:
                    engine.close_stream()
                except Exception:
                    pass
                respond({'id': request_id, 'ok': False,
                         'code': error.code if isinstance(error, WorkerError) else 'recognition_failed'})
    finally:
        try:
            engine.close()
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
