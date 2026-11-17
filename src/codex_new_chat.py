"""Version-pinned Codex blank creation and Desktop-owned first-turn submission.

The short-lived stdio helper only creates and persists a blank identity. Work is
submitted once to that exact Desktop owner, which retains the native settings,
approvals and lifetime after WATCH disconnects. No direct-turn fallback exists.
"""
import contextlib
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import codex_transcript
import codex_desktop_control as _desktop
from new_chat_types import Capability, FrozenFolder, NewChatError, OpenResult, VerifiedIdentity, ExecutionResult


APP = Path('/Applications/ChatGPT.app')
BINARY = APP / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
VERSION, BUILD = '26.1029.61003', '24614'
ADAPTER_VERSION = 'codex-new-chat-v2/' + VERSION + '/' + BUILD
ASAR_SHA256 = '0c951258b7ab381ec601045ffb02159e53f424548dda18e6a4dcfd126557383b'
BINARY_SHA256 = '508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44'
MAX_LINE, MAX_TOTAL, MAX_FRAMES = 1024 * 1024, 8 * 1024 * 1024, 512
UUID = re.compile(codex_transcript.UUID + r'\Z')
DEFAULTS_MESSAGE = 'Starting Codex chats is unavailable until native default settings are verified.'
CREATION_MESSAGE = 'The creation outcome could not be confirmed. It will not be repeated automatically.'


class ProtocolError(Exception):
    """Internal errors carry no application output, request payloads or paths."""


def _uuid(value):
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def _folder(folder):
    if (not isinstance(folder, FrozenFolder) or not isinstance(folder.path, str)
            or not os.path.isabs(folder.path) or os.path.realpath(folder.path) != folder.path):
        raise NewChatError('folder_changed', 'The selected folder changed. Choose it again.')
    try:
        info = os.lstat(folder.path)
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (folder.device, folder.inode)):
            raise OSError()
    except OSError:
        raise NewChatError('folder_changed', 'The selected folder changed. Choose it again.') from None


def _observed_folder(path):
    """Read an actual native cwd for mismatch evidence, never for creation input."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path:
        raise ValueError()
    info = os.lstat(path)
    observed = FrozenFolder('', '', '', path, info.st_dev, info.st_ino)
    _folder(observed)
    return observed


def _regular(path, maximum):
    if os.path.realpath(path) != os.fspath(path):
        raise ValueError()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise ValueError()
        return os.fdopen(fd, 'rb'), info
    except BaseException:
        os.close(fd)
        raise


def _installed_build():
    """Only bundle metadata and executable bytes; no native config or credentials."""
    if sys.platform != 'darwin':
        return Capability(False, 'unsupported_platform', 'Codex New chat currently requires macOS.', ADAPTER_VERSION)
    try:
        source, _ = _regular(APP / 'Contents/Info.plist', 1024 * 1024)
        with source:
            info = plistlib.loads(source.read())
        if (info.get('CFBundleIdentifier') != 'com.openai.codex'
                or info.get('CFBundleShortVersionString') != VERSION
                or str(info.get('CFBundleVersion')) != BUILD):
            raise ValueError()
        deadline = time.monotonic() + 8
        for path, expected in ((APP / 'Contents/Resources/app.asar', ASAR_SHA256),
                               (BINARY, BINARY_SHA256)):
            source, before = _regular(path, 1024 * 1024 * 1024)
            if path == BINARY and not before.st_mode & 0o111:
                source.close()
                raise ValueError()
            digest = hashlib.sha256()
            with source:
                while True:
                    if time.monotonic() >= deadline:
                        raise ValueError()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(source.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or digest.hexdigest() != expected):
                raise ValueError()
        return Capability(True, version=ADAPTER_VERSION)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return Capability(False, 'app_version_unsupported', 'This Codex build is not verified for New chat.', ADAPTER_VERSION)


class StdioTransport:
    """Bounded JSONL on one owned child. Never retries or approves server requests.

    There is no caller-selectable executable, shell, config, model or permissions.
    """
    METHODS = frozenset(('initialize', 'thread/start', 'thread/name/set'))

    def __init__(self, folder, timeout=30):
        _folder(folder)
        self.timeout = min(30, max(.01, timeout))
        self.process = subprocess.Popen([str(BINARY), 'app-server', '--listen', 'stdio://'],
                                        cwd=folder.path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self._close_lock = threading.Lock()
        self.closed = False
        self.selector = None
        try:
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
            self.selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stderr.fileno(), False)
            os.set_blocking(self.process.stdin.fileno(), False)
        except Exception:
            self.close()
            raise ProtocolError() from None
        self.buffer = bytearray()
        self.total = self.frames = self.sequence = 0
        self.deadline = time.monotonic() + self.timeout

    def _write(self, value):
        raw = (json.dumps(value, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
        if len(raw) > MAX_LINE or self.closed:
            raise ProtocolError()
        view = memoryview(raw)
        while view:
            if time.monotonic() >= self.deadline:
                raise ProtocolError()
            try:
                count = os.write(self.process.stdin.fileno(), view)
                if count <= 0:
                    raise ProtocolError()
                view = view[count:]
            except BlockingIOError:
                # Input frames are tiny; wait for writable readiness with a deadline.
                with selectors.DefaultSelector() as writer:
                    writer.register(self.process.stdin, selectors.EVENT_WRITE)
                    if not writer.select(max(0, self.deadline - time.monotonic())):
                        raise ProtocolError()
            except OSError:
                raise ProtocolError() from None

    def notify_initialized(self):
        self._write({'method': 'initialized', 'params': {}})

    def _line(self):
        while True:
            position = self.buffer.find(b'\n')
            if position >= 0:
                if position > MAX_LINE:
                    raise ProtocolError()
                line = bytes(self.buffer[:position])
                del self.buffer[:position + 1]
                return line
            if len(self.buffer) > MAX_LINE:
                raise ProtocolError()
            remaining = self.deadline - time.monotonic()
            if self.closed or remaining <= 0:
                raise ProtocolError()
            events = self.selector.select(remaining)
            if not events:
                raise ProtocolError()
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    raise ProtocolError() from None
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == 'stdout':
                        raise ProtocolError()
                    continue
                self.total += len(chunk)
                if self.total > MAX_TOTAL:
                    raise ProtocolError()
                if key.data == 'stdout':
                    self.buffer.extend(chunk)
                # Stderr counts against the budget but is never retained or exposed.

    def request(self, method, params):
        if method not in self.METHODS:
            raise ProtocolError()
        self.sequence += 1
        ident = self.sequence
        self._write({'id': ident, 'method': method, 'params': params})
        while True:
            self.frames += 1
            if self.frames > MAX_FRAMES:
                raise ProtocolError()
            try:
                def unique(pairs):
                    out = {}
                    for key, value in pairs:
                        if key in out:
                            raise ValueError()
                        out[key] = value
                    return out
                value = json.loads(self._line(), object_pairs_hook=unique,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise ProtocolError() from None
            if not isinstance(value, dict):
                raise ProtocolError()
            if 'method' in value:
                # Server requests can ask for approval/elicitation: never answer one.
                if ('id' in value or not isinstance(value['method'], str)
                        or not 0 < len(value['method']) <= 160
                        or not isinstance(value.get('params', {}), dict)
                        or value['method'].startswith(('turn/', 'item/agentMessage'))):
                    raise ProtocolError()
                continue
            if (type(value.get('id')) is not int or value['id'] != ident
                    or 'error' in value or not isinstance(value.get('result'), dict)):
                raise ProtocolError()
            return value['result']

    def close(self):
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            # Do not poll/reap first: retaining the child PID prevents group-id
            # reuse. Stop descendants even if their leader already exited or
            # they ignore TERM. Forced cleanup may interrupt native end hooks.
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            time.sleep(.05)
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except OSError:
                with contextlib.suppress(OSError):
                    os.kill(self.process.pid, signal.SIGTERM)
            time.sleep(.05)
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                with contextlib.suppress(OSError):
                    os.kill(self.process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=.5)
            if self.selector is not None:
                self.selector.close()
            for stream in (self.process.stdout, self.process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()


class DesktopConnection(_desktop._Connection):
    """Existing private framing, with a separate current-build gate and allowlist.

    No approval, elicitation response, steering, interrupt or settings write is
    supported. Those remain with the Desktop owner, including after disconnect.
    """
    METHODS = {'initialize': 0, 'thread-owner-discovery': 1,
               'thread-follower-read-model-settings': 1, 'thread-follower-start-turn': 2}

    def request(self, method, params, client, version, owner=None):
        if method not in self.METHODS or version != self.METHODS[method]:
            raise ProtocolError()
        return super().request(method, params, client, version, owner)

    def _send(self, message, mutation=False):
        method = message.get('method')
        if message.get('type') == 'request' and method not in self.METHODS:
            raise ProtocolError()
        return super()._send(message, mutation=method == 'thread-follower-start-turn')


def _settings(value):
    if (not isinstance(value, dict) or not {'model', 'reasoningEffort', 'mode', 'modelProvider'} <= value.keys()
            or not isinstance(value['model'], str) or not 0 < len(value['model']) <= 256
            or value['mode'] not in ('default', 'plan', None)
            or any(value[key] is not None and (not isinstance(value[key], str) or len(value[key]) > 256)
                   for key in ('reasoningEffort', 'modelProvider'))):
        raise ProtocolError()
    return value


@dataclass(frozen=True)
class TestDependencies:
    """Complete in-process fake environment, never assembled from an owner request.

    No defaults are supplied: fixtures must provide every dependency and an
    explicit settings receipt. Native StdioTransport is refused in this mode.
    """
    build_check: object
    settings: object
    transport_factory: object
    verifier: object
    opener: object
    desktop_factory: object = None


class Adapter:
    def __init__(self, home=None, *, _test_dependencies=None):
        if _test_dependencies is not None and not isinstance(_test_dependencies, TestDependencies):
            raise TypeError('Complete test dependencies are required.')
        self.home = Path(home) if home is not None else Path.home()
        self._test = _test_dependencies
        self._lock = threading.Lock()
        self._transports = set()
        self._expected_folders = OrderedDict()
        self._closed = False

    def _build(self):
        return self._test.build_check() if self._test else _installed_build()

    def preflight(self):
        if self._closed:
            return Capability(False, 'adapter_closed', 'Codex New chat is closed.', ADAPTER_VERSION)
        try:
            build = self._build()
            if not isinstance(build, Capability) or not build.available:
                return build if isinstance(build, Capability) else Capability(False, 'app_version_unsupported', version=ADAPTER_VERSION)
            if self._test is None:
                # Current pinned source and the native proof establish omitted
                # thread settings resolve from the selected cwd's configuration.
                # Desktop adoption then owns normal permission/user-input flows.
                _desktop._verify_endpoint(self.home / '.codex/ipc/ipc.sock')
                return Capability(True, version=ADAPTER_VERSION, supports_work=True)
            settings = self._test.settings()
            if (type(settings) is not dict or set(settings) != {'contract', 'verified'}
                    or settings['contract'] != 'fictional-native-defaults-v1'
                    or settings['verified'] is not True):
                return Capability(False, 'native_defaults_unverified', DEFAULTS_MESSAGE, ADAPTER_VERSION)
            return Capability(True, version=ADAPTER_VERSION, supports_work=callable(self._test.desktop_factory))
        except Exception:
            return Capability(False, 'app_version_unsupported', 'Codex New chat is unavailable.', ADAPTER_VERSION)

    def create_once(self, folder, record_native_receipt):
        capability = self.preflight()
        if not capability.available:
            raise NewChatError(capability.code, capability.message)
        _folder(folder)
        if not callable(record_native_receipt):
            raise NewChatError('receipt_unavailable', 'A durable native receipt is required.')
        transport = None
        try:
            transport = self._test.transport_factory(folder) if self._test else StdioTransport(folder)
            if self._test and isinstance(transport, StdioTransport):
                raise ProtocolError()
            with self._lock:
                if self._closed:
                    raise ProtocolError()
                self._transports.add(transport)
            transport.request('initialize', {'clientInfo': {'name': 'watch_new_chat', 'version': '1'},
                                             'capabilities': {}})
            transport.notify_initialized()
            _folder(folder)
            response = transport.request('thread/start', {'cwd': folder.path, 'ephemeral': False})
            thread = response.get('thread') if isinstance(response, dict) else None
            actual_cwd = response.get('cwd') if isinstance(response, dict) else None
            if (not isinstance(thread, dict) or not _uuid(thread.get('id'))
                    or not isinstance(actual_cwd, str) or len(actual_cwd) > 4096
                    or '\0' in actual_cwd or not os.path.isabs(actual_cwd)):
                raise ProtocolError()
            native_id = thread['id']
            # Preserve the valid RPC identity even when its folder is wrong.
            # The service persists this evidence before raising its mismatch.
            record_native_receipt(native_id, actual_cwd)
            if (thread.get('sessionId', native_id) != native_id
                    or thread.get('forkedFromId') is not None
                    or thread.get('ephemeral', False) is not False
                    or thread.get('turns', []) != []
                    or thread.get('cwd', actual_cwd) != actual_cwd):
                raise ProtocolError()
            if actual_cwd != folder.path:
                raise NewChatError('folder_mismatch', 'The native chat was created in a different folder.')
            _folder(folder)
            transport.request('thread/name/set', {'threadId': native_id, 'name': 'New chat'})
        except NewChatError:
            raise
        except Exception:
            raise NewChatError('creation_unknown', CREATION_MESSAGE) from None
        finally:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
                with self._lock:
                    self._transports.discard(transport)
        # Persistence must outlive the creator. No history RPC, message or turn is
        # needed to establish it; inspect the independent pinned native evidence.
        identity = self.verify_existing(native_id, folder)
        if identity is None:
            raise NewChatError('creation_unverified', 'The chat identity was received, but its saved identity is not verified.')
        if (identity.cwd != folder.path
                or (identity.folder_device, identity.folder_inode) != (folder.device, folder.inode)):
            raise NewChatError('folder_mismatch', 'The saved native chat belongs to a different folder.')
        return identity

    def verify_existing(self, native_id, folder):
        """Return actual pinned evidence, including a verified folder mismatch.

        Callers must compare cwd and folder identity with their frozen selection.
        A mismatched identity never authorizes opening through this adapter.
        """
        if not _uuid(native_id):
            return None
        try:
            _folder(folder)
            build = self._build()
            if not isinstance(build, Capability) or not build.available:
                return None
            if self._test:
                proof = self._test.verifier(native_id, folder)
            else:
                sessions = str(self.home / '.codex/sessions')
                # New chat requires a native index; never accept legacy fallback.
                entries = codex_transcript.index(str(self.home / '.codex'), native_id)
                if entries is None:
                    return None
                entry = entries.get(native_id)
                path = codex_transcript.selected(entry)
                actual = _observed_folder(entry['cwd'])
                _, source, info, meta = codex_transcript.open_pinned(sessions, path, native_id, actual.path)
                source.close()
                proof = VerifiedIdentity(native_id, meta['cwd'], path, info.st_dev, info.st_ino,
                                         ADAPTER_VERSION, actual.device, actual.inode)
            _folder(folder)
            actual = _observed_folder(proof.cwd) if isinstance(proof, VerifiedIdentity) else None
            if (not isinstance(proof, VerifiedIdentity) or proof.native_id != native_id
                    or proof.adapter_version != ADAPTER_VERSION
                    or (proof.folder_device, proof.folder_inode) != (actual.device, actual.inode)
                    or not isinstance(proof.source_path, str) or not os.path.isabs(proof.source_path)
                    or type(proof.source_device) is not int or type(proof.source_inode) is not int):
                return None
            with self._lock:
                existing = self._expected_folders.get(native_id)
                if existing is not None and existing != folder:
                    return None
                self._expected_folders[native_id] = folder
                self._expected_folders.move_to_end(native_id)
                if len(self._expected_folders) > 1024:
                    self._expected_folders.popitem(last=False)
            return proof
        except Exception:
            return None

    def _ready_owner(self, native_id):
        """Retry only read-only adoption checks within one original deadline.

        OS URL acceptance can precede Desktop stream-owner initialization. Each
        socket shares this deadline; malformed settings/identity never authorize
        a turn. No durable submission claim exists anywhere in this method.
        """
        deadline = time.monotonic() + 15
        for attempt in range(4):
            connection = None
            try:
                connection = (self._test.desktop_factory() if self._test else
                              DesktopConnection(self.home / '.codex/ipc/ipc.sock'))
                if self._test and isinstance(connection, DesktopConnection):
                    raise ProtocolError()
                # Base framing consults this deadline before every read/write.
                connection.deadline = deadline
                initialized = connection.request('initialize', {'clientType': 'watch-new-chat'}, 'initializing-client', 0)
                client = initialized['result']['clientId']
                if not _uuid(client) or initialized['handledByClientId'] != client:
                    raise ProtocolError()
                reply = connection.request('thread-owner-discovery',
                    {'hostId': 'local', 'conversationId': native_id}, client, 1)
                owner = reply['handledByClientId']
                if not _uuid(owner) or owner == client or reply['result'].get('supportsUntrustedAppInput') is not True:
                    raise ProtocolError()
                _settings(connection.request('thread-follower-read-model-settings',
                    {'conversationId': native_id}, client, 1, owner)['result'].get('settings'))
                return connection, client, owner
            except (_desktop._Invalid, OSError):
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                remaining = deadline - time.monotonic()
                if self._closed or attempt == 3 or remaining <= 0:
                    raise ProtocolError() from None
                time.sleep(min(.75, remaining))
            except Exception:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                raise
        raise ProtocolError()

    def start_work_once(self, identity, instruction, claim_submission, record_turn_receipt):
        """One Desktop-owned submission; callbacks commit before/after the wire.

        A lost reply or service restart never retries. Closing this connection
        never cancels the Desktop's turn, tools, approvals or integrations.
        """
        connection = None
        try:
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8000:
                raise ProtocolError()
            instruction.encode('utf-8', errors='strict')
            if not self.preflight().supports_work:
                return ExecutionResult('not_attempted', code='work_unavailable')
            with self._lock:
                folder = self._expected_folders.get(identity.native_id)
            if folder is None or self.verify_existing(identity.native_id, folder) != identity:
                return ExecutionResult('not_attempted', code='identity_unavailable')
            connection, client, owner = self._ready_owner(identity.native_id)
            # Freeze folder and native identity immediately before the separately
            # durable first-turn reservation. There is no turn/start stdio path.
            if self.verify_existing(identity.native_id, folder) != identity:
                return ExecutionResult('not_attempted', code='identity_unavailable')
            claim_submission()
            response = connection.request('thread-follower-start-turn',
                {'conversationId': identity.native_id,
                 'turnStart': {'request': {'threadId': identity.native_id,
                     'input': [{'type': 'text', 'text': instruction, 'text_elements': []}]},
                     'context': {'inheritThreadSettings': True}}}, client, 2, owner)
            result = response['result']['result']
            turn = result['turn']
            if not isinstance(turn, dict) or not _uuid(turn.get('id')):
                raise ProtocolError()
            record_turn_receipt(turn['id'])
            status = {'inProgress': 'accepted', 'completed': 'completed', 'failed': 'failed',
                      'interrupted': 'needs_attention'}.get(turn.get('status'))
            if status is None:
                raise ProtocolError()
            return ExecutionResult(status, turn['id'], 'execution_failed' if status == 'failed' else '')
        except Exception:
            attempted = connection is not None and connection.mutation_attempted
            return ExecutionResult('unknown' if attempted else 'not_attempted',
                                   code='execution_unknown' if attempted else 'owner_unavailable')
        finally:
            instruction = None
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()

    def execution_status(self, identity, turn_id):
        """Bounded read of this pinned thread only; no submission or owner RPC.

        Completion is tied to the retained turn ID, never to text, timestamps or
        another thread. Large/partial/unrecognized evidence remains uncertain.
        """
        if not _uuid(turn_id):
            return None
        try:
            with self._lock:
                folder = self._expected_folders.get(identity.native_id)
            if folder is None or self.verify_existing(identity.native_id, folder) != identity:
                return None
            if self._test:
                return None
            _, source, info, _ = codex_transcript.open_pinned(str(self.home / '.codex/sessions'),
                identity.source_path, identity.native_id, folder.path)
            outcome = None
            with source:
                start = max(0, info.st_size - 2 * 1024 * 1024)
                source.seek(start)
                if start:
                    source.readline(MAX_LINE + 1)
                budget = 2 * 1024 * 1024
                while budget > 0:
                    line = source.readline(min(MAX_LINE + 1, budget + 1))
                    if not line:
                        break
                    budget -= len(line)
                    if budget < 0 or len(line) > MAX_LINE or not line.endswith(b'\n'):
                        return None
                    record = json.loads(line)
                    payload = record.get('payload', {})
                    if record.get('type') != 'event_msg' or payload.get('turn_id') != turn_id:
                        continue
                    kind = payload.get('type')
                    if kind == 'task_complete':
                        outcome = 'completed'
                    elif kind == 'turn_aborted':
                        outcome = 'needs_attention'
                    elif kind in ('turn_failed', 'task_failed'):
                        outcome = 'failed'
            return ExecutionResult(outcome, turn_id) if outcome else None
        except Exception:
            return None

    def open_existing(self, identity, draft=None):
        if not isinstance(identity, VerifiedIdentity) or not _uuid(identity.native_id):
            return OpenResult('rejected', 'identity_changed', 'The saved Codex identity is unavailable.')
        if draft is not None and (not isinstance(draft, str) or len(draft) > 8000):
            return OpenResult('rejected', 'invalid_draft', 'The draft is invalid or too long.')
        try:
            if draft is not None:
                draft.encode('utf-8', errors='strict')
        except UnicodeError:
            return OpenResult('rejected', 'invalid_draft', 'The draft is invalid or too long.')
        if self._closed:
            return OpenResult('rejected', 'adapter_closed', 'Codex New chat is closed.')
        with self._lock:
            folder = self._expected_folders.get(identity.native_id)
        if (folder is None or identity.cwd != folder.path
                or (identity.folder_device, identity.folder_inode) != (folder.device, folder.inode)):
            return OpenResult('rejected', 'folder_mismatch', 'The saved identity is not bound to the selected folder.')
        current = self.verify_existing(identity.native_id, folder)
        if current != identity:
            return OpenResult('rejected', 'identity_changed', 'The saved Codex identity or selected folder changed.')
        url = 'codex://threads/' + identity.native_id
        if draft:
            # A single encoded query value; nothing submits or starts a turn.
            url += '?prompt=' + quote(draft, safe='', encoding='utf-8', errors='strict')
        try:
            if self._test:
                result = self._test.opener(url)
                if not isinstance(result, OpenResult) or result.outcome not in ('accepted', 'unknown', 'rejected'):
                    raise ValueError()
                return result
            process = subprocess.Popen(['/usr/bin/open', '-a', str(APP), url],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGTERM)
                time.sleep(.05)
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=.5)
                return OpenResult('unknown', 'open_unknown', 'The chat exists; its opening could not be confirmed.')
            return OpenResult('accepted') if code == 0 else OpenResult('rejected', 'open_rejected', 'The chat exists, but Codex could not be opened.')
        except Exception:
            return OpenResult('unknown', 'open_unknown', 'The chat exists; its opening could not be confirmed.')

    def close(self):
        with self._lock:
            self._closed = True
            transports = tuple(self._transports)
        for transport in transports:
            with contextlib.suppress(Exception):
                transport.close()
