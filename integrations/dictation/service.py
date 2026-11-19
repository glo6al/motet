"""Bounded in-memory sessions. This module never opens a microphone or a file."""
from collections import deque
import contextlib
import math
import secrets
import struct
import threading
import time
import base64

# A fast, bounded admission wait, not a deadline for terminal completion.
# A refused Stop/Cancel does not itself end the take.
COMMAND_WAIT_SECONDS = .1


class ServiceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class DictationService:
    def __init__(self, runtime_factory, installation, clock=time.monotonic):
        self.factory, self.installation, self.clock = runtime_factory, installation, clock
        self.runtime = None
        self.state, self.reason = 'not_installed', 'dependency_missing'
        self.sessions = {}
        self.current = None
        self.command = threading.Lock()
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.load_ms = None
        self.loading_thread = None

    def load(self):
        with self.condition:
            if self.closed or self.loading_thread is not None and self.loading_thread.is_alive():
                return
            if self.state in ('ready', 'listening', 'finishing'):
                return
            self.state, self.reason = 'loading', None
            self.loading_thread = threading.Thread(target=self._load, daemon=True)
            self.loading_thread.start()

    def _load(self):
        runtime = None
        try:
            reason = self.installation()
            if reason:
                with self.condition:
                    if not self.closed:
                        self.state, self.reason = 'not_installed', reason
                return
            runtime = self.factory()
            with self.condition:
                if self.closed:
                    return
                self.runtime = runtime
            runtime.start()
            with self.condition:
                if not self.closed:
                    self.state, self.reason, self.load_ms = 'ready', None, runtime.load_ms
        except Exception as exc:
            with self.condition:
                if not self.closed:
                    self.reason = getattr(exc, 'code', 'worker_failed')
                    self.state = 'not_installed' if self.reason in ('dependency_missing', 'runtime_version', 'model_missing_or_invalid') else 'failed'
            if runtime is not None:
                runtime.close()
        finally:
            with self.condition:
                if self.closed and runtime is not None:
                    runtime.close()
                self.condition.notify_all()

    def status(self):
        with self.condition:
            return {'schemaVersion': 1, 'state': self.state, 'code': self.reason,
                    'loadMs': self.load_ms, 'microphone': 'client_owned',
                    'audioFormat': {'sampleRate': 16000, 'channels': 1, 'encoding': 'float32le'},
                    'maxPacketBytes': 64000, 'maxTakeSeconds': 180}

    @contextlib.contextmanager
    def _command(self):
        if not self.command.acquire(timeout=COMMAND_WAIT_SECONDS):
            raise ServiceError('busy', 'Another audio command is still running.', 409)
        try:
            if self.closed:
                raise ServiceError('not_running', 'The helper has stopped.', 503)
            yield
        finally:
            self.command.release()

    def _session(self, ident, active=True):
        if type(ident) is not str or ident not in self.sessions:
            raise ServiceError('unknown_session', 'This dictation session is unavailable.', 404)
        session = self.sessions[ident]
        if active and (session['ended'] or session is not self.current):
            raise ServiceError('ended', 'This dictation take has ended.', 409)
        return session

    def _emit(self, session, event):
        session['counter'] += 1
        session['events'].append(dict(event, id=session['counter']))
        self.condition.notify_all()

    def _end(self, session, code=None):
        if session['ended']:
            return
        session['ended'] = True
        session['touched'] = self.clock()
        if code:
            self._emit(session, {'type': 'error', 'code': code,
                                 'message': 'Dictation stopped. Keep the visible draft and start a new take.'})
        self._emit(session, {'type': 'ended', 'epoch': session['epoch']})

    def _call(self, session, action, **data):
        try:
            result = self.runtime.call(action, **data)
            if type(result) is not dict or type(result.get('events')) is not list:
                raise ValueError()
            if type(result.get('epoch')) is not int:
                raise ValueError()
            expected = session['epoch'] + (1 if action == 'reset' else 0)
            if result['epoch'] != expected:
                raise ValueError()
            with self.condition:
                if self.closed or session['ended']:
                    raise ServiceError('ended', 'This dictation take has ended.', 409)
                if action == 'reset':
                    session['epoch'] = expected
                if len(result['events']) > 128:
                    raise ValueError()
                for event in result['events']:
                    if type(event) is not dict:
                        raise ValueError()
                    if event.get('epoch') != session['epoch']:
                        continue
                    if (event.get('type') != 'line' or type(event.get('text')) is not str
                            or len(event['text']) > 8192 or type(event.get('lineId')) is not str
                            or len(event['lineId']) > 128 or type(event.get('complete')) is not bool):
                        raise ValueError()
                    clean = {name: event[name] for name in ('type', 'epoch', 'lineId', 'text', 'complete')}
                    for name in ('audioSeconds', 'decodeMs'):
                        value = event.get(name)
                        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                            clean[name] = value
                    self._emit(session, clean)
            return result
        except ServiceError:
            raise
        except Exception:
            with self.condition:
                if self.closed:
                    raise ServiceError('not_running', 'The helper has stopped.', 503) from None
                self._end(session, 'worker_failed')
                self.state, self.reason = 'failed', 'worker_failed'
            try:
                self.runtime.close()
            except Exception:
                pass
            raise ServiceError('worker_failed', 'Recognition failed. The visible draft should be kept.', 503) from None

    def start(self, interval):
        if type(interval) not in (int, float) or interval not in (.25, .5):
            raise ServiceError('invalid_interval', 'Choose an update interval of 0.25 or 0.5 seconds.')
        with self._command():
            with self.condition:
                if self.current and not self.current['ended']:
                    raise ServiceError('busy', 'A dictation take is already active.', 409)
                if self.state == 'failed':
                    self.load()
                    raise ServiceError('loading', 'Reloading the local recognizer. Try again when it is ready.', 503)
                if self.state != 'ready':
                    raise ServiceError(self.reason or self.state, 'The local recognizer is not ready.', 503)
                now = self.clock()
                self.sessions = {key: value for key, value in self.sessions.items() if now - value['touched'] < 60}
                while len(self.sessions) >= 4:
                    del self.sessions[next(iter(self.sessions))]
                session = {'id': secrets.token_hex(24), 'epoch': 1, 'sequence': 0, 'audioSeconds': 0,
                           'events': deque(maxlen=512), 'counter': 0, 'ended': False,
                           'created': now, 'touched': now}
                self.sessions[session['id']] = self.current = session
            self._call(session, 'start', interval=interval)
            with self.condition:
                if self.closed:
                    raise ServiceError('not_running', 'The helper has stopped.', 503)
                self.state = 'listening'
            return {'sessionId': session['id'], 'epoch': 1}

    def audio(self, ident, sequence, pcm):
        if type(sequence) is not int or sequence < 0:
            raise ServiceError('invalid_sequence', 'Audio packets need an ordered sequence number.')
        if type(pcm) is not bytes or not pcm or len(pcm) > 64000 or len(pcm) % 4:
            raise ServiceError('invalid_audio', 'Use bounded mono 16 kHz float32 PCM.')
        peak, squares, count = 0.0, 0.0, len(pcm) // 4
        for (sample,) in struct.iter_unpack('<f', pcm):
            if not math.isfinite(sample) or abs(sample) > 1:
                raise ServiceError('invalid_audio', 'PCM samples must be finite and normalized.')
            peak, squares = max(peak, abs(sample)), squares + sample * sample
        with self._command():
            with self.condition:
                session = self._session(ident)
                if sequence != session['sequence']:
                    raise ServiceError('out_of_order', 'The audio sequence changed. Start a new take.', 409)
                duration = count / 16000
                over = session['audioSeconds'] + duration > 180 or self.clock() - session['created'] > 180
            if over:
                self._call(session, 'cancel')
                with self.condition:
                    self._end(session, 'take_limit')
                    if not self.closed:
                        self.state = 'ready'
                raise ServiceError('take_limit', 'The three-minute take limit was reached.', 409)
            result = self._call(session, 'audio', pcm=base64.b64encode(pcm).decode('ascii'))
            with self.condition:
                if self.closed:
                    raise ServiceError('not_running', 'The helper has stopped.', 503)
                session['sequence'] += 1
                session['audioSeconds'] += duration
                session['touched'] = self.clock()
            return {'acceptedSequence': sequence, 'epoch': session['epoch'],
                    'level': {'peak': peak, 'rms': math.sqrt(squares / count)},
                    'commandMs': result.get('commandMs')}

    def control(self, ident, action):
        if action not in ('stop', 'cancel', 'reset'):
            raise ServiceError('invalid_control', 'Unknown dictation control.')
        with self._command():
            with self.condition:
                session = self._session(ident)
                session['touched'] = self.clock()
                if action == 'stop':
                    self.state = 'finishing'
            began = self.clock()
            self._call(session, action)
            with self.condition:
                if self.closed:
                    raise ServiceError('not_running', 'The helper has stopped.', 503)
                if action != 'reset':
                    self._end(session)
                    self.state = 'ready'
                return {'sessionId': ident, 'epoch': session['epoch'], 'ended': session['ended'],
                        'finalizeMs': round((self.clock() - began) * 1000, 2)}

    def events(self, ident, after):
        if type(after) is not int or after < 0:
            raise ServiceError('invalid_cursor', 'Use a valid event cursor.')
        with self.condition:
            session = self._session(ident, active=False)
            if after > session['counter']:
                raise ServiceError('invalid_cursor', 'The event cursor is ahead of this session.', 409)
            if session['events'] and after < session['events'][0]['id'] - 1:
                raise ServiceError('events_expired', 'Transcript events expired. Keep the visible draft.', 409)
            session['touched'] = self.clock()
            if not session['ended'] and after == session['counter']:
                self.condition.wait(timeout=.5)
            return {'sessionId': ident, 'epoch': session['epoch'], 'ended': session['ended'],
                    'events': [dict(event) for event in session['events'] if event['id'] > after]}

    def expire(self):
        # Healthy takes never contend with audio merely because the timer ran.
        with self.condition:
            now = self.clock()
            self.sessions = {key: value for key, value in self.sessions.items()
                             if not value['ended'] or now - value['touched'] < 60}
            candidate = self.current
            stale = candidate and not candidate['ended'] and (now - candidate['created'] > 180 or now - candidate['touched'] > 20)
        if not stale or not self.command.acquire(blocking=False):
            return
        try:
            with self.condition:
                session = self.current
                now = self.clock()
                deadline = session and now - session['created'] > 180
                stale = session is candidate and not session['ended'] and (deadline or now - session['touched'] > 20)
            if not stale:
                return
            try:
                self._call(session, 'cancel')
            except ServiceError:
                return
            with self.condition:
                self._end(session, 'take_limit' if deadline else 'disconnected')
                if not self.closed:
                    self.state = 'ready'
        finally:
            self.command.release()

    def close(self):
        with self.condition:
            self.closed = True
            self.state = 'not_running'
            if self.current:
                self._end(self.current)
            runtime = self.runtime
        if runtime is not None:
            runtime.close()
