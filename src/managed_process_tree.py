"""End a managed Claude chat's process tree on a Mac (POSIX), by verified identity only. Importing this module does
nothing.

A process is named here by its identity, never by its number alone: the process ID, the time it started (from
``ps``, to the second), and this user's ID. Process IDs are reused, so every signal is sent only to a process that
``ps`` shows, just before the signal, with that same ID, start time and owner; one that has gone, or whose number
now belongs to another process, is left alone.

The tree is the root process, every process descended from it through parent links, and every member of the root's
process group (Claude starts in its own session, so its group is its own; a background process whose parent has
already ended stays in that group). It is read again before every round, so a process started while the chat is
being stopped is found too. A process that both left Claude's group (its own session) and lost its parent link (its
parent ended first) is no longer part of the tree and cannot be found: nothing records that it came from this chat.

Windows does not use this: Claude runs in a job there, and ending the job ends everything in it.
"""
import os
import signal
import subprocess
import time
from collections import namedtuple

PS = '/bin/ps'
GENTLE_SECONDS = 3        # how long the tree is given to end after SIGTERM
FIRM_SECONDS = 2          # and after SIGKILL

Process = namedtuple('Process', 'pid ppid pgid uid stat start command')


class TreeError(OSError):
    """``ps`` could not be read; nothing can be verified, so nothing is signalled."""


def _start(fields):
    """``lstart`` (``Wed Nov 26 07:05:01 2026`` in the C locale) as seconds since the epoch, local time."""
    return time.mktime(time.strptime(' '.join(fields), '%a %b %d %H:%M:%S %Y'))


def table(run=subprocess.run):
    """Every process this machine shows: ID, parent, group, owner, state, start time and command line."""
    try:
        output = run([PS, '-A', '-ww', '-o', 'pid=,ppid=,pgid=,uid=,stat=,lstart=,command='],
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     env=dict({'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'},
                              **({'TZ': os.environ['TZ']} if 'TZ' in os.environ else {})),   # start times as mktime reads them
                     timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise TreeError('process table unavailable') from error
    rows = {}
    for line in output.decode('utf-8', 'replace').splitlines():
        fields = line.split(None, 10)
        if len(fields) < 10:
            continue
        try:
            row = Process(int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3]), fields[4],
                          _start(fields[5:10]), fields[10] if len(fields) > 10 else '')
        except (ValueError, OverflowError):
            continue
        rows[row.pid] = row
    if not rows:
        raise TreeError('process table unavailable')
    return rows


def alive(row):
    """A zombie has ended; only its parent's reaping is left."""
    return row is not None and not row.stat.startswith('Z')


def same(row, known):
    """The process ``ps`` shows now is the one recorded: same ID, start time and owner."""
    return row is not None and (row.pid, row.start, row.uid) == (known.pid, known.start, known.uid)


def members(rows, root, known):
    """The tree, read from ``rows``: ``root`` (recorded identity or None once it has gone), ``known`` (identities
    found in earlier reads), their descendants, and the root's group while any recorded member is still in it."""
    uid = os.getuid()
    found = {}
    for item in ([root] if root is not None else []) + list(known.values()):
        row = rows.get(item.pid)
        if same(row, item):
            found[row.pid] = row
    group = root.pid if root is not None and root.pgid == root.pid else None
    if group is not None and any(row.pgid == group for row in found.values()):
        # The group's ID cannot be given to a new process while any member is left, so its members are its own.
        for row in rows.values():
            if row.pgid == group and row.uid == uid:
                found.setdefault(row.pid, row)
    grew = True
    while grew:
        grew = False
        for row in rows.values():
            if row.pid not in found and row.ppid in found and row.uid == uid and row.pid != row.ppid:
                found[row.pid] = row
                grew = True
    return {pid: row for pid, row in found.items() if row.uid == uid}


def _signal(rows, targets, number):
    for pid, known in targets.items():
        row = rows.get(pid)
        if same(row, known) and alive(row):
            try:
                os.kill(pid, number)
            except (ProcessLookupError, PermissionError):
                pass


def end(root, *, also=(), group_signal=None, gentle=GENTLE_SECONDS, firm=FIRM_SECONDS, read=table,
        clock=time.monotonic, sleep=time.sleep):
    """SIGTERM the tree, wait up to ``gentle`` seconds, SIGKILL what is left, wait up to ``firm``. ``root`` is the
    recorded identity of the chat's own process (a Process), or None once it has gone; ``also`` are identities from
    an earlier read of the tree, ended with theirs. ``group_signal(number)``, when given, also signals the root's
    group directly; only the supervisor gives it, while Claude is its own unreaped child (so neither its ID
    nor its group's can be reused). Returns the identities still running at the end: empty means the whole tree
    has ended."""
    known = {row.pid: row for row in also}
    for number, seconds in ((signal.SIGTERM, gentle), (signal.SIGKILL, firm)):
        rows = read()
        known.update(members(rows, root, known))
        if group_signal is not None:
            group_signal(number)
        _signal(rows, known, number)
        deadline = clock() + seconds
        while True:
            rows = read()
            known.update(members(rows, root, known))
            left = {pid: row for pid, row in known.items() if same(rows.get(pid), row) and alive(rows.get(pid))}
            if not left:
                return {}
            if clock() >= deadline:
                break
            sleep(.1)
    return left
