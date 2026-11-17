"""Guarded owner directory maintenance. No native transport or outcome mirror."""
import sqlite3
import threading

from attachments_owner_guards import OwnerAttachmentGuard, OwnerGuardError
from attachments_owner_transport import LedgerError, OwnerAttachmentLedger


def _schedule(checkpoint, stopped):
    def run():
        while not stopped.is_set():
            checkpoint()
            stopped.wait(0.25)
    return threading.Thread(target=run, name='watch-owner-attachment-directory', daemon=True)


class OwnerAttachmentRuntime:
    def __init__(self, ledger, scheduler):
        self.ledger = ledger
        self.stopped = threading.Event()
        self._running_step = threading.Lock()
        self._started = False
        self._worker = scheduler(self.checkpoint, self.stopped)
        self.state = 'checking'
        self.native_checkpoint = None

    def start(self):
        if self._started or self.stopped.is_set():
            return
        self._worker.start()
        self._started = True

    def stop(self):
        self.stopped.set()
        if self._started or self._worker.is_alive():
            self._worker.join()
            self._started = False
        # Also wait for an injected scheduler's in-flight checkpoint.
        with self._running_step:
            self.state = 'stopped'

    def checkpoint(self):
        self.refresh_directory()
        checkpoint = self.native_checkpoint
        if checkpoint is not None and not self.stopped.is_set():
            checkpoint()

    def refresh_directory(self):
        if self.stopped.is_set() or not self._running_step.acquire(blocking=False):
            return
        try:
            if self.stopped.is_set():
                return
            ledger, directory = self.ledger, self.ledger.directory
            with ledger.lock:
                try:
                    if not directory.building:
                        with ledger._transaction():
                            current = directory.matches(directory.epoch())
                        if current and directory.ready:
                            self.state = 'ready'
                            return
                        directory.begin_rebuild()
                    complete = directory.rebuild_step()
                    if complete and directory.ready:
                        self.state = 'ready'
                    else:
                        self.state = 'checking' if directory.building else 'recovery_required'
                except (LedgerError, OwnerGuardError, sqlite3.Error):
                    directory.abandon()
                    self.state = 'recovery_required'
        finally:
            self._running_step.release()


def bind_owner_runtime(store, *, allow_install, scheduler=_schedule, _native_permission=None):
    """Bind only after guarded base startup, before the ordinary worker starts."""
    with store._native_owner_lock:
        if store._native_owner_stopped:
            return None
        if store._native_owner_runtime is not None:
            if _native_permission is not None and store._native_owner_runtime.ledger._native_permission is not _native_permission:
                raise OwnerGuardError('Owner attachment authority is already bound.')
            return store._native_owner_runtime
        with store.lock:
            guard = store._attachment_guard
            if guard is None:
                if not allow_install:
                    return None
                if store.db.in_transaction:
                    raise OwnerGuardError('Owner attachment setup requires its own writer.')
                try:
                    store.db.execute('BEGIN IMMEDIATE')
                    guard = OwnerAttachmentGuard.install(store.db, store.lock)
                    store.db.commit()
                except (OwnerGuardError, sqlite3.Error):
                    store.db.rollback()
                    # A commit error can arrive after the complete schema was
                    # installed. Protect it before the ordinary worker starts.
                    store._attachment_guard = OwnerAttachmentGuard.probe(store.db, lock=store.lock)
                    store._native_owner_error = 'setup_unavailable'
                    return None
                store._attachment_guard = guard
            else:
                guard.require_unshadowed()
                guard.require_schema()
                guard.require_neutral()
            if guard.capacity is not None:
                raise OwnerGuardError('Owner attachment capacity is already bound.')
            try:
                ledger = OwnerAttachmentLedger(store.db, store.lock, guard=guard, maintenance_only=True,
                                               _native_permission=_native_permission)
                runtime = OwnerAttachmentRuntime(ledger, scheduler)
            except (LedgerError, sqlite3.Error, OSError, RuntimeError):
                guard.capacity = None
                store._native_owner_error = 'maintenance_unavailable'
                return None
            store._native_owner_runtime = runtime
        try:
            runtime.start()
        except (OSError, RuntimeError):
            runtime.stop()
            with store.lock:
                guard.capacity = None
                store._native_owner_runtime = None
                store._native_owner_error = 'maintenance_unavailable'
            return None
        return runtime
