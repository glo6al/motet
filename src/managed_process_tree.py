"""See, and end, a managed Claude chat's process tree on a Mac (POSIX). Importing this module does nothing.

Signals. The only signal sent here goes to Claude's own process group, and only by its supervisor, while Claude is
that supervisor's own unreaped child (``end``'s ``group_signal``). Claude starts in a session of its own, so its
process ID is its group's ID, and while Claude is unreaped (alive, or a zombie) neither number can be given to another
process. Everything still in that group (Claude, and what it started without leaving it) receives the signal.

No individual process is ever signalled by its number. macOS has no way to hold a process's identity across a
signal (Linux's pidfd has no Mac equivalent): any check of the number, start time or command line before ``kill``
leaves a moment in which the process can end, be reaped and its number be reused. So a process the group signal does
not reach (one that started a session of its own, or one left after Claude was reaped) is only observed, never
signalled, and is reported as still running: the owner's stop is then not confirmed.

Observation. A process is named by its identity: the process ID, the time it started (from ``ps``, to the second)
and this user's ID. The tree is the root process, every process descended from it through parent links, and every
member of the root's group while any recorded member is in it. It is read again on every observation, so a process
started while the chat is being stopped is seen too. A process that both left Claude's group and lost its parent
link (its parent ended first) is no longer part of the tree and cannot be seen: nothing records that it came from
this chat.

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
    """``ps`` could not be read; nothing can be seen, so nothing is confirmed."""


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


def running(rows, identities):
    """The recorded identities (Process rows, or ``[pid, start]`` pairs of this user's) still running in ``rows``."""
    uid = os.getuid()
    out = {}
    for item in identities:
        known = item if isinstance(item, Process) else Process(int(item[0]), 0, 0, uid, '', float(item[1]), '')
        row = rows.get(known.pid)
        if same(row, known) and alive(row):
            out[row.pid] = row
    return out


def end(root, *, group_signal, gentle=GENTLE_SECONDS, firm=FIRM_SECONDS, read=table, clock=time.monotonic,
        sleep=time.sleep):
    """The supervisor's end of the tree: ``group_signal(SIGTERM)``, then up to ``gentle`` seconds for the tree to
    end, then ``group_signal(SIGKILL)`` and up to ``firm`` seconds. ``root`` is Claude's identity, read while it is
    the supervisor's unreaped child; ``group_signal`` signals its group only while that is still so. Nothing else
    is signalled. Returns the identities of the tree still running at the end (empty: the whole tree has ended)."""
    known = {}
    left = {}
    for number, seconds in ((signal.SIGTERM, gentle), (signal.SIGKILL, firm)):
        known.update(members(read(), root, known))
        group_signal(number)
        deadline = clock() + seconds
        while True:
            rows = read()
            known.update(members(rows, root, known))
            left = running(rows, known.values())
            if not left:
                return {}
            if clock() >= deadline:
                break
            sleep(.1)
    return left
