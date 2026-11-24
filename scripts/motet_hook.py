#!/usr/bin/env python3
"""Motet's Claude Code hook in Python, for systems where the shell hooks cannot run (Windows).

  <python> -I motet_hook.py tell <port>   passes the hook's JSON to Motet's /hook and prints nothing.
  <python> -I motet_hook.py ask <port>    passes it to /permission and prints Motet's Allow or Deny, but only
                                          if the reply is, byte for byte, one of the two decisions below.

It does what the curl hooks in install_hooks.py do, and nothing more: the same endpoints, the same two headers
(Content-Type and the key from ~/.live-room/header), and nothing printed on any doubt. It also asks the server to
close the connection after its reply, and reads that reply in full before closing, so the server never meets a
reset connection. Arguments after the port (the installer's marker) are ignored, except --test-timeout=SECONDS,
which only the tests pass: it can shorten the time allowed, never lengthen it. It always exits 0 and never writes
a file.

http.client is used rather than urllib because urllib honours proxy settings; the key must never leave this
machine. The key is never printed or logged.
"""
import http.client, json, os, re, sys, threading

# The two replies the server sends (permission() in src/server.py) and the only output this hook may print.
# install_hooks.py builds its shell hooks from this same list, so the two kinds of hook cannot drift apart.
REPLIES = [json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': d}}) for d in
           ({'behavior': 'allow'}, {'behavior': 'deny', 'message': 'Denied by the user from Motet.'})]
MAX_BODY = 200_000            # src/server.py refuses a larger body
SECONDS = {'tell': 1, 'ask': 320}       # the curl hooks' -m values
ROUTES = {'tell': '/hook', 'ask': '/permission'}
TEST_TIMEOUT = '--test-timeout='     # a test-only argument; the installed command never carries it, and no setting can


def limit(mode, argv):
    """The whole-run time allowed: curl's -m value, or less if a test asks for less."""
    seconds = SECONDS[mode]
    for arg in argv[3:]:
        if arg.startswith(TEST_TIMEOUT):
            try:
                seconds = min(seconds, float(arg[len(TEST_TIMEOUT):]))     # min() keeps the limit when given NaN
            except ValueError:
                pass
    return seconds


def read_key():
    """The value from the header file exactly as the server writes it ('X-Live-Room-Key: <key>' and a newline),
    or None. Anything else is treated as no key."""
    path = os.path.join(os.path.expanduser('~'), '.live-room', 'header')     # the server's HOME, the same way
    with open(path, 'rb') as f:
        raw = f.read(1024)
    m = re.fullmatch(rb'X-Live-Room-Key: ([\x21-\x7e]{1,400})\r?\n?', raw)
    return m.group(1).decode('ascii') if m else None


def run(argv):
    if len(argv) < 3 or argv[1] not in ROUTES or not argv[2].isdigit() or not 0 < int(argv[2]) < 65536:
        return None
    mode, port = argv[1], int(argv[2])
    seconds = limit(mode, argv)
    # A whole-run limit, as curl's -m is: a server sending a byte at a time cannot hold the hook past it.
    # Leaving this way prints nothing, because output is only ever written after the reply has been checked.
    timer = threading.Timer(seconds, os._exit, (0,))
    timer.daemon = True
    timer.start()
    stdin = sys.stdin.buffer
    body = stdin.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        while stdin.read(65536):                 # take the rest so Claude Code is not left writing to a closed pipe
            pass
        return None
    try:
        key = read_key()
    except OSError:
        return None
    if not key:
        return None
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=seconds)
    try:
        # A bytes body is sent with a Content-Length, as curl --data-binary does; the server requires one.
        conn.request('POST', ROUTES[mode], body=body,
                     headers={'Content-Type': 'application/json', 'X-Live-Room-Key': key, 'Connection': 'close'})
        resp = conn.getresponse()
        length = resp.getheader('Content-Length', '')
        whole = length.isdigit() and int(length) <= 4096
        # Every reply is read, used or not: closing with part of it unread would reset a connection the server is
        # still writing to, and the server would print that as an error. A whole read raises if the body is cut short.
        try:
            reply = resp.read() if whole else resp.read(65536)
        except (http.client.HTTPException, OSError):
            return None
    finally:
        conn.close()
    if mode == 'tell' or resp.status != 200 or not whole:
        return None
    if reply in [r.encode() for r in REPLIES]:
        timer.cancel()
        return reply
    return None


def main():
    reply = None
    try:
        reply = run(sys.argv)
    except BaseException:
        reply = None
    if reply:
        try:
            sys.stdout.buffer.write(reply)
            sys.stdout.buffer.flush()
        except BaseException:
            pass
    # Leave without Python's own shutdown, which could report a closed stdout on stderr and exit non-zero.
    os._exit(0)


if __name__ == '__main__':
    main()
