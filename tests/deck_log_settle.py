"""Wait for a real Deck Log writer to save test-fixture records, by progress.

A fixed time budget for a whole batch only measures how fast the test machine
is. These helpers instead fail when the archive reports a failure, when its
writer thread has gone, or when nothing moves for a long stall window; a slow
but working writer is always given the time it needs, up to a high ceiling
that still bounds a real hang.
"""
import os
import sys
import time

# No observable writer progress for this long means it is stuck.
STALL_SECONDS = 60.0 if os.name == 'nt' else 30.0
# Absolute bound for one wait, even while the writer keeps moving.
CEILING_SECONDS = 900.0
SLICE_SECONDS = 0.25
IDLE_POLL_SECONDS = 0.01


def _snapshot(history):
    with history.wake:
        return (len(history.queue), history.inflight, getattr(history, '_writer_step', None))


def _writer_idle(thread):
    # An empty queue alone does not prove the last record was saved: a writer
    # that crashes on it still takes it off the queue and clears its in-flight
    # count before the thread ends. A healthy writer always goes back to
    # waiting for work in DeckLog._writer; a crashed one never does. So only a
    # writer seen parked in that wait has finished cleanly.
    frame = sys._current_frames().get(thread.ident)
    return bool(frame is not None and frame.f_code.co_name == 'wait' and frame.f_back is not None
                and frame.f_back.f_code.co_name == '_writer')


def _describe(history, label, started, extra):
    thread = getattr(history, 'thread', None)
    return ('%s %s after %.1fs: reason=%r, available=%r, queued=%d, inflight=%d, step=%r, '
            'writer_alive=%r, last_error=%r' % (
                label, extra, time.monotonic() - started, history.reason, history.available,
                len(history.queue), history.inflight, getattr(history, '_writer_step', None),
                bool(thread and thread.is_alive()), getattr(history, 'last_error', None)))


def settle(history, label='Fictional archive', *, stall=None, ceiling=None, slice_seconds=SLICE_SECONDS):
    """Return '' once every queued record is saved, otherwise a diagnostic.

    Success means an empty queue, nothing in flight, and a live writer that
    has gone back to waiting for work. Progress is any change in the queue
    length, the in-flight count or the writer's current step. A reported
    reason, an unavailable archive, a closed archive (drained or not) or a dead
    writer fails at once; no progress for `stall` seconds or more than
    `ceiling` seconds in total fails with the last observed state.
    """
    stall = STALL_SECONDS if stall is None else stall
    ceiling = CEILING_SECONDS if ceiling is None else ceiling
    started = time.monotonic()
    seen, changed = None, started
    while True:
        state = _snapshot(history)
        now = time.monotonic()
        drained = not state[0] and not state[1]
        if history.reason or not history.available:
            return _describe(history, label, started, 'reported a failure')
        if history.closed:
            return _describe(history, label, started, 'was closed before its records were confirmed saved'
                             if drained else 'was closed with records still pending')
        thread = getattr(history, 'thread', None)
        if thread is None or not thread.is_alive():
            return _describe(history, label, started, 'has no running writer')
        if drained and _writer_idle(thread):
            return ''
        if state != seen:
            seen, changed = state, now
        elif now - changed >= stall:
            return _describe(history, label, started, 'has an empty queue but its writer did not return to waiting for %gs' % stall
                             if drained else 'made no progress for %gs' % stall)
        if now - started >= ceiling:
            return _describe(history, label, started, 'was still saving at the %gs ceiling' % ceiling)
        if drained:
            time.sleep(IDLE_POLL_SECONDS)
        else:
            history.flush(slice_seconds)


def close(history, label='Fictional archive', *, stall=None, timeout=2.0):
    """Close the archive and release its file, or fail saying why not.

    DeckLog.close leaves the database open when its writer does not stop in
    time, which on Windows keeps the temporary folder locked. Closing marks the
    archive closed, so the writer exits after its current record; wait for that
    while it is alive, then close again so the file is released. A writer that
    is still alive, or a database still open, after that is an AssertionError
    with the archive's state, never a silent leak.
    """
    started = time.monotonic()
    history.close(timeout)
    thread = getattr(history, 'thread', None)
    if history.db is not None and thread is not None:
        deadline = time.monotonic() + (STALL_SECONDS if stall is None else stall)
        while thread.is_alive() and time.monotonic() < deadline:
            thread.join(SLICE_SECONDS)
        history.close(timeout)
    if history.db is not None or (thread is not None and thread.is_alive()):
        raise AssertionError(_describe(history, label, started,
                                       'could not be closed (database open=%r)' % (history.db is not None)))


def close_and_clean(history, cleanup, label='Fictional archive', **options):
    """Close the archive, then always run `cleanup` (usually removing its folder).

    When the close fails, the open archive keeps its folder locked on Windows;
    that folder error is only a consequence, so the close failure is reported.
    """
    try:
        close(history, label, **options)
    except AssertionError:
        try:
            cleanup()
        except OSError:
            pass
        raise
    cleanup()
