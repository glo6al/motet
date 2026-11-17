"""Hard-stubbed attachment admission proof, never a provider transport.

This only checks controlled fictional fixtures. Synthetic token bounds and the
repository's dated prices are test inputs, NOT verified live multimodal bounds or
current prices. No live toggle, endpoint, arbitrary transport callback or SDK is
provided. The durable ledger belongs solely to the disposable proof root.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import uuid

_SRC = Path(__file__).resolve().parents[2] / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from officer_request import BudgetPolicy, InputBound, RequestBudget, request_digest

MODEL = 'claude-haiku-5-5'
SLOTS = tuple(f'O{i}' for i in range(1, 9))
BILLING = {'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard', 'extras': False}
_POLICY = {'mode': 'strict_usd', 'usd_limit': '0.025', 'max_calls': 1,
           'max_output_tokens': 1024, 'max_input_tokens': 100000,
           'max_input_bytes': 524288, 'max_seconds': 120}
_MIME = {'application/pdf', 'image/png', 'image/jpeg', 'text/plain', 'text/markdown'}
_UNSET = object()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def load_pricing_snapshot():
    """Read the checked-in test snapshot. Never fetch or claim fresh pricing."""
    return json.loads((_SRC.parent / 'motet' / 'PRICING.json').read_text())


def synthetic_bound(body, tokens=100000):
    """A deliberately synthetic test input. It is not a token counter."""
    return InputBound(tokens, 'conservative', request_digest(body))


class ProofRejected(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


OfflineProofError = ProofRejected


@dataclass(frozen=True)
class ProofFile:
    path: Path
    sha256: str
    media_type: str


@dataclass(frozen=True)
class ProofRequest:
    slot: str
    body: dict
    input_bound: InputBound | None
    billing: dict | None
    expected_folder: Path
    actual_folder: Path
    files: tuple[ProofFile, ...] = ()
    recipient: str = 'officer'
    intent: str = 'analyze'
    synthetic: bool = True


class OfflineProofHarness:
    """Eight single-use synthetic slots; all successful calls go to a fixed stub."""

    def __init__(self, root: Path, pricing=_UNSET):
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise ProofRejected('disposable_root_required')
        self._requested_root = root.absolute()
        self.root = root.resolve(strict=True)
        self._root_identity = self._identity(self.root.stat())
        self.pricing = load_pricing_snapshot() if pricing is _UNSET else json.loads(_json(pricing))
        # No alternate rates can silently qualify this test run.
        if self.pricing is not None and self.pricing != load_pricing_snapshot():
            raise ProofRejected('pricing_snapshot_mismatch')
        self.path = self.root / 'offline-proof.sqlite'
        self.marker = self.root / '.offline-proof-run.json'
        if self.path.is_symlink() or self.marker.is_symlink():
            raise ProofRejected('unsafe_ledger_or_marker')
        ledger_exists, marker_exists = self.path.exists(), self.marker.exists()
        if ledger_exists != marker_exists:
            raise ProofRejected('incomplete_or_missing_run_state')
        new = not ledger_exists
        if new:
            # A durable exclusive marker precedes the ledger. A crash between these
            # steps leaves an incomplete run, never permission to reset capacity.
            marker = {'version': 1, 'stub_only': True, 'run_id': uuid.uuid4().hex}
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(_json(marker))
                out.flush()
                os.fsync(out.fileno())
            self._sync_root()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            self._sync_root()
        else:
            if not self.marker.is_file() or self.marker.stat().st_nlink != 1 or self.marker.stat().st_size > 1024:
                raise ProofRejected('unsafe_run_marker')
            try:
                marker = json.loads(self.marker.read_text())
            except (ValueError, OSError):
                raise ProofRejected('invalid_run_marker') from None
            if (not isinstance(marker, dict) or set(marker) != {'version', 'stub_only', 'run_id'}
                    or marker['version'] != 1 or marker['stub_only'] is not True
                    or not isinstance(marker['run_id'], str) or len(marker['run_id']) != 32
                    or any(c not in '0123456789abcdef' for c in marker['run_id'])):
                raise ProofRejected('invalid_run_marker')
        if not self.path.is_file() or self.path.stat().st_nlink != 1:
            raise ProofRejected('unsafe_ledger')
        self._marker_identity = self._identity(self.marker.stat())
        self._ledger_identity = self._identity(self.path.stat())
        self.binding = _json({'version': 1, 'stub_only': True, 'model': MODEL, 'run_id': marker['run_id'],
                              'pricing_digest': _digest(self.pricing), 'policy': _POLICY,
                              'slots': list(SLOTS), 'run_reserved_usd': '0.200'})
        try:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if new:
                    db.execute('CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, halted INTEGER NOT NULL CHECK(halted IN (0,1)))')
                    db.execute('INSERT INTO meta VALUES (1,?,0)', (self.binding,))
                    db.execute('CREATE TABLE slots (slot TEXT PRIMARY KEY, reserved_usd TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT, stub_attempts INTEGER NOT NULL, result TEXT)')
                    db.executemany('INSERT INTO slots VALUES (?, ?, ?, NULL, 0, NULL)', [(s, '0.025', 'unused') for s in SLOTS])
                self._validate(db)
                if not new and db.execute("SELECT count(*) FROM slots WHERE state='claimed'").fetchone()[0]:
                    db.execute("UPDATE slots SET state='unknown',result=? WHERE state='claimed'", (_json({'reason': 'unresolved_claim_on_open'}),))
                    db.execute('UPDATE meta SET halted=1 WHERE id=1')
                db.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise ProofRejected('ledger_invalid_or_binding_changed') from exc

    def _sync_root(self):
        fd = os.open(self.root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _identity(info):
        return {'device': info.st_dev, 'inode': info.st_ino}

    def _assert_ledger_identity(self):
        if (self.root.is_symlink() or self._identity(self.root.stat()) != self._root_identity
                or self.marker.is_symlink() or not self.marker.is_file()
                or self.marker.stat().st_nlink != 1
                or self._identity(self.marker.stat()) != self._marker_identity
                or self.path.is_symlink() or not self.path.is_file()
                or self.path.stat().st_nlink != 1
                or self._identity(self.path.stat()) != self._ledger_identity):
            raise ProofRejected('proof_root_or_ledger_identity_changed')

    @contextmanager
    def _db(self):
        self._assert_ledger_identity()
        connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            self._assert_ledger_identity()
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
                self._assert_ledger_identity()
        finally:
            connection.close()

    def _validate(self, db):
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ProofRejected('ledger_corrupt')
        meta = db.execute('SELECT * FROM meta').fetchall()
        rows = db.execute('SELECT * FROM slots ORDER BY slot').fetchall()
        if len(meta) != 1 or meta[0]['binding'] != self.binding or meta[0]['halted'] not in (0, 1):
            raise ProofRejected('ledger_binding_changed')
        if [row['slot'] for row in rows] != list(SLOTS):
            raise ProofRejected('ledger_slots_changed')
        for row in rows:
            if (row['reserved_usd'] != '0.025' or row['state'] not in ('unused', 'claimed', 'unknown', 'complete', 'overrun')
                    or type(row['stub_attempts']) is not int or row['stub_attempts'] not in (0, 1)):
                raise ProofRejected('ledger_record_invalid')
            if row['state'] == 'unused':
                if row['fingerprint'] is not None or row['stub_attempts'] != 0 or row['result'] is not None:
                    raise ProofRejected('ledger_unused_record_invalid')
            elif not isinstance(row['fingerprint'], str) or len(row['fingerprint']) != 64:
                raise ProofRejected('ledger_claim_invalid')
            elif row['state'] == 'claimed':
                if row['result'] is not None:
                    raise ProofRejected('ledger_claim_result_invalid')
            else:
                try:
                    result = json.loads(row['result'])
                except (ValueError, TypeError):
                    raise ProofRejected('ledger_result_invalid') from None
                if not isinstance(result, dict) or row['state'] in ('complete', 'overrun') and row['stub_attempts'] != 1:
                    raise ProofRejected('ledger_result_invalid')
                if row['state'] in ('overrun', 'unknown') and not meta[0]['halted']:
                    raise ProofRejected('ledger_halt_missing')
        return bool(meta[0]['halted'])

    def _inside(self, path, *, directory=False):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            try:
                relative = path.relative_to(self._requested_root)
                path = self.root / relative
            except ValueError:
                raise ProofRejected('fixture_outside_disposable_root') from None
        if '..' in relative.parts or any((self.root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts)+1)):
            raise ProofRejected('fixture_symlink_or_traversal')
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ProofRejected('fixture_missing') from None
        if resolved != path or directory and not path.is_dir():
            raise ProofRejected('fixture_folder_invalid')
        return resolved

    def _files(self, files):
        if not isinstance(files, tuple) or len(files) > 5:
            raise ProofRejected('invalid_fixture_files')
        manifest, total = [], 0
        for f in files:
            if not isinstance(f, ProofFile) or f.media_type not in _MIME:
                raise ProofRejected('unsupported_type')
            path = self._inside(f.path)
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 10 * 1048576:
                    raise ProofRejected('invalid_fixture_file')
                data = b''
                while len(data) <= 10 * 1048576:
                    part = os.read(fd, min(65536, 10 * 1048576 + 1 - len(data)))
                    if not part:
                        break
                    data += part
                after = os.fstat(fd)
            finally:
                os.close(fd)
            named = path.lstat()
            if (self._identity(named) != self._identity(info) or not stat.S_ISREG(named.st_mode)
                    or len(data) != info.st_size or after.st_mtime_ns != info.st_mtime_ns or after.st_size != info.st_size):
                raise ProofRejected('fixture_changed_while_reading')
            digest = hashlib.sha256(data).hexdigest()
            if digest != f.sha256:
                raise ProofRejected('fixture_digest_mismatch')
            if f.media_type == 'application/pdf':
                if not data.startswith(b'%PDF-') or b'%%EOF' not in data:
                    raise ProofRejected('invalid_fixture_pdf')
                if b'/Encrypt' in data:
                    raise ProofRejected('locked_pdf')
            elif f.media_type == 'image/png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ProofRejected('invalid_fixture_png')
            elif f.media_type == 'image/jpeg' and not (data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9')):
                raise ProofRejected('invalid_fixture_jpeg')
            elif f.media_type in ('text/plain', 'text/markdown'):
                try:
                    text = data.decode('utf-8', errors='strict')
                except UnicodeDecodeError:
                    raise ProofRejected('invalid_utf8') from None
                if '\0' in text:
                    raise ProofRejected('invalid_utf8_text')
            total += len(data)
            if total > 20 * 1048576:
                raise ProofRejected('draft_size_exceeded')
            manifest.append({'relative_path': str(path.relative_to(self.root)), 'sha256': digest,
                             'media_type': f.media_type, 'bytes': len(data), 'identity': self._identity(info), 'mtime_ns': info.st_mtime_ns})
        return manifest

    def run(self, request: ProofRequest, *, fault='none'):
        if not isinstance(request, ProofRequest) or request.synthetic is not True:
            raise ProofRejected('synthetic_fixture_only')
        if request.slot not in SLOTS:
            raise ProofRejected('unknown_slot')
        if fault not in ('none', 'lost_result', 'after_claim', 'usage_overrun'):
            raise ProofRejected('unknown_stub_fault')
        if request.recipient != 'officer':
            raise ProofRejected('unsupported_recipient')
        if request.intent != 'analyze':
            raise ProofRejected('officer_forwarding_refused')
        expected = self._inside(request.expected_folder, directory=True)
        actual = self._inside(request.actual_folder, directory=True)
        if expected != actual:
            raise ProofRejected('folder_or_worktree_mismatch')
        folder_identity = self._identity(expected.stat())
        manifest = self._files(request.files)
        body = json.loads(_json(request.body))
        if not isinstance(body, dict) or body.get('model') != MODEL:
            raise ProofRejected('unsupported_model')
        # No tool/action/URL transport configuration fits this proof body.
        if set(body) - {'model', 'max_tokens', 'system', 'messages'}:
            raise ProofRejected('unsupported_body_field')
        budget = RequestBudget(BudgetPolicy(**_POLICY), pricing=self.pricing)
        reservation = budget.reserve_call(body, 'answer', input_bound=request.input_bound, billing=request.billing)
        fingerprint = _digest({'body': body, 'manifest': manifest,
                               'folder': str(expected.relative_to(self.root)), 'recipient': request.recipient,
                               'intent': request.intent, 'folder_identity': folder_identity,
                               'input_bound': {'tokens': request.input_bound.tokens, 'basis': request.input_bound.basis,
                                               'request_digest': request.input_bound.request_digest},
                               'billing': request.billing})
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            halted = self._validate(db)
            if self._identity(self._inside(expected, directory=True).stat()) != folder_identity:
                raise ProofRejected('folder_changed_before_claim')
            for item in manifest:
                info = self._inside(self.root / item['relative_path']).stat()
                if (self._identity(info) != item['identity'] or info.st_mtime_ns != item['mtime_ns']
                        or info.st_size != item['bytes']):
                    raise ProofRejected('file_changed_before_claim')
            row = db.execute('SELECT * FROM slots WHERE slot=?', (request.slot,)).fetchone()
            if row['state'] != 'unused':
                if row['fingerprint'] != fingerprint:
                    raise ProofRejected('slot_fingerprint_conflict')
                return self._row(row)
            if halted:
                raise ProofRejected('run_halted')
            if db.execute("SELECT count(*) FROM slots WHERE state='claimed'").fetchone()[0]:
                raise ProofRejected('run_has_unresolved_claim')
            db.execute('UPDATE slots SET state=?,fingerprint=? WHERE slot=? AND state=?', ('claimed', fingerprint, request.slot, 'unused'))
            db.commit()  # Durable claim precedes even the fixed in-process stub.
        if fault == 'after_claim':
            return self._finish(request.slot, 'unknown', {'reason': 'fault_after_claim', 'budget': budget.receipt()}, halt=True)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            halted = self._validate(db)
            if halted:
                return self._row(db.execute('SELECT * FROM slots WHERE slot=?', (request.slot,)).fetchone())
            claimed = db.execute('UPDATE slots SET stub_attempts=1 WHERE slot=? AND state=? AND fingerprint=? AND stub_attempts=0',
                                 (request.slot, 'claimed', fingerprint))
            if claimed.rowcount != 1:
                # Another opener may have conservatively recovered our claim as
                # unknown. Never invoke a stub without its recorded attempt.
                return self._row(db.execute('SELECT * FROM slots WHERE slot=?', (request.slot,)).fetchone())
            db.commit()
        response = self._stub(fault)
        if fault == 'lost_result':
            return self._finish(request.slot, 'unknown', {'reason': 'stub_result_lost', 'budget': budget.receipt()}, halt=True)
        settled = budget.settle(reservation, response)
        receipt = budget.receipt()
        state = 'overrun' if receipt['exhausted'] else 'complete' if settled else 'unknown'
        return self._finish(request.slot, state, {'response': response, 'budget': receipt}, halt=state in ('overrun', 'unknown'))

    @staticmethod
    def _stub(fault):
        # Fixed synthetic metadata only. Never performs I/O or reads an answer key.
        return {'model': MODEL, 'content': [{'type': 'text', 'text': 'OFFLINE_STUB_ONLY'}],
                'usage': {'input_tokens': 100, 'output_tokens': 1025 if fault == 'usage_overrun' else 10,
                          'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                          'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0},
                          'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'}}

    def _finish(self, slot, state, result, *, halt=False):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._validate(db)
            db.execute('UPDATE slots SET state=?,result=? WHERE slot=? AND state=?', (state, _json(result), slot, 'claimed'))
            if halt:
                db.execute('UPDATE meta SET halted=1 WHERE id=1')
            db.commit()
        return self.status(slot)

    @staticmethod
    def _row(row):
        return {'slot': row['slot'], 'state': 'unknown' if row['state'] == 'claimed' else row['state'],
                'reserved_usd': row['reserved_usd'], 'stub_attempts': row['stub_attempts'],
                'network_transmissions': 0,
                'result': json.loads(row['result']) if row['result'] is not None else None}

    def status(self, slot):
        if slot not in SLOTS:
            raise ProofRejected('unknown_slot')
        with self._db() as db:
            self._validate(db)
            return self._row(db.execute('SELECT * FROM slots WHERE slot=?', (slot,)).fetchone())

    def summary(self):
        with self._db() as db:
            halted = self._validate(db)
            rows = db.execute('SELECT * FROM slots ORDER BY slot').fetchall()
        return {'offline': True, 'synthetic': True, 'stub_only': True, 'synthetic_bounds_only': True, 'network_transmissions': 0,
                'reserved_usd': '0.200', 'halted': halted,
                'stub_attempts': sum(row['stub_attempts'] for row in rows),
                'slots': [self._row(row) for row in rows],
                'qualification': 'Offline reservations are not live token bounds, fresh pricing, or invoices.'}
