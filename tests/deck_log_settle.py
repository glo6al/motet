"""Wait for a real Deck Log writer to save test-fixture records, by progress.

A fixed time budget for a whole batch only measures how fast the test machine
is. These helpers instead fail when the archive reports a failure, when its
writer thread has gone, or when nothing moves for a long stall window; a slow
but working writer is always given the time it needs, up to a high ceiling
that still bounds a real hang.
"""
import os
import time

# No observable writer progress for this long means it is stuck.
STALL_SECONDS = 60.0 if os.name == 'nt' else 30.0
# Absolute bound for one wait, even while the writer keeps moving.
CEILING_SECONDS = 900.0
SLICE_SECONDS = 0.25


def _snapshot(history):
    with history.wake:
        return (len(history.queue), history.inflight, getattr(history, '_writer_step', None))


def _describe(history, label, started, extra):
    thread = getattr(history, 'thread', None)
    return ('%s %s after %.1fs: reason=%r, available=%r, queued=%d, inflight=%d, step=%r, '
            'writer_alive=%r, last_error=%r' % (
                label, extra, time.monotonic() - started, history.reason, history.available,
                len(history.queue), history.inflight, getattr(history, '_writer_step', None),
                bool(thread and thread.is_alive()), getattr(history, 'last_error', None)))


def settle(history, label='Fictional archive', *, stall=None, ceiling=None, slice_seconds=SLICE_SECONDS):
    """Return '' once every queued record is saved, otherwise a diagnostic.

    Progress is any change in the queue length, the in-flight count or the
    writer's current step. A reported reason, an unavailable archive, a closed
    archive or a dead writer fails at once; no progress for `stall` seconds or
    more than `ceiling` seconds in total fails with the last observed state.
    """
    stall = STALL_SECONDS if stall is None else stall
    ceiling = CEILING_SECONDS if ceiling is None else ceiling
    started = time.monotonic()
    seen, changed = None, started
    while True:
        state = _snapshot(history)
        now = time.monotonic()
        if history.reason or not history.available:
            return _describe(history, label, started, 'reported a failure')
        if not state[0] and not state[1]:
            return ''
        if history.closed:
            return _describe(history, label, started, 'was closed with records still pending')
        thread = getattr(history, 'thread', None)
        if thread is None or not thread.is_alive():
            return _describe(history, label, started, 'has no running writer')
        if state != seen:
            seen, changed = state, now
        elif now - changed >= stall:
            return _describe(history, label, started, 'made no progress for %.0fs' % stall)
        if now - started >= ceiling:
            return _describe(history, label, started, 'was still saving at the %.0fs ceiling' % ceiling)
        history.flush(slice_seconds)


def close(history, *, stall=None):
    """Close the archive and its file even if a write was still in flight.

    DeckLog.close leaves the database open when its writer does not stop in
    time, which on Windows keeps the temporary folder locked. Closing marks the
    archive closed, so the writer exits after its current record; wait for that
    while it is alive, then close again so the file is released.
    """
    history.close()
    thread = getattr(history, 'thread', None)
    if history.db is None or thread is None:
        return
    deadline = time.monotonic() + (STALL_SECONDS if stall is None else stall)
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(SLICE_SECONDS)
    history.close()
