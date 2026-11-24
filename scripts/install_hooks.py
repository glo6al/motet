#!/usr/bin/env python3
"""Add (or with --remove, take out) Motet's Claude Code hooks in ~/.claude/settings.json.

  Notification       tells Motet a session wants attention. Observes only.
  Stop, SubagentStop tell Motet that Claude, or a sub-agent, is about to stop. That is provisional, not a finished
                     turn: another Stop hook can tell Claude to continue, and the log's own records of the turn's
                     end may arrive later; Motet labels it so. Observe only: they print nothing, so they cannot
                     keep Claude going or change what it does.
  PermissionRequest  asks Motet for your Allow / Deny and passes the answer back to Claude.
                     This one DOES decide prompts on your behalf when you press a button on the stage.
                     With the stage closed, or no answer, it prints nothing. Claude is then expected to ask in the app
                     as usual; that has not yet been confirmed against a real prompt (see docs/TRUST_BOUNDARY.md).

All send the key from ~/.live-room/header, which the server writes when it starts. Only entries carrying this script's
marker are ever touched; other hooks in the same group are left alone. The file is replaced atomically and a
uniquely named backup is written first. PORT is honoured (default 8793). MOTET_CLAUDE_SETTINGS names another settings
file in place of ~/.claude/settings.json (the tests use it).

On macOS and Linux the hooks are shell commands that use curl. On Windows they are Claude Code's "exec form": this
Python runs scripts/motet_hook.py directly, with no shell, and that script does what the curl commands do. Exec form
needs Claude Code 2.1.139 or later; the installer refuses an older claude it can find.

The hooks trust whatever process is listening on that port: they send it the key, and the PermissionRequest hook
passes on its Allow or Deny. Nothing here proves that process is Motet. While the server is not running, another
local program able to bind the port could collect the key and answer prompts. Stopping other local programs from
binding it is outside what this tool protects against; remove the hooks (--remove) if that matters on this machine.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from motet_hook import REPLIES      # the two decisions, defined once for both kinds of hook

MARK = '#live-room-hook'
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'motet_hook.py')
MIN_CLAUDE = (2, 1, 139)            # the release that added a hook's "args" (exec form)


def shell_commands(port):
    """The macOS and Linux hooks: POSIX shell, unchanged."""
    # The key is read by curl from a private file, so it never appears in a process's arguments. -f makes curl print
    # nothing when the server refuses or errors, so Claude never receives a non-decision as if it were one.
    HEAD = "-H 'Content-Type: application/json' -H @\"$HOME/.live-room/header\""
    URL = 'http://127.0.0.1:%s' % port
    # -q ignores any ~/.curlrc and --noproxy '*' ignores proxy settings, so the key is only ever sent to this machine.
    CURL = "curl -q -sf --noproxy '*'"
    TELL = "%s -m 1 -X POST %s --data-binary @- %s/hook >/dev/null 2>&1 || true %s" % (CURL, HEAD, URL, MARK)
    # The reply is collected first and printed only if the transfer succeeded and it is, byte for byte, one of the two
    # decisions the server sends (see permission() in src/server.py). Anything else, such as a reply cut part-way, an
    # error page or some other JSON, prints nothing, which leaves the prompt to Claude.
    ASK = ("r=$(%s -m 320 -X POST %s --data-binary @- %s/permission 2>/dev/null) && case \"$r\" in "
           "'%s'|'%s') printf '%%s' \"$r\";; esac; true %s" % (CURL, HEAD, URL, REPLIES[0], REPLIES[1], MARK))
    return {'command': TELL}, {'command': ASK}


def exec_commands(port, python):
    """The Windows hooks: Claude Code starts this Python on motet_hook.py itself, with no shell in between, so nothing
    depends on Git Bash, PowerShell or how either expands $HOME. -I keeps PYTHONPATH and similar settings out."""
    def entry(mode):
        return {'command': python, 'args': ['-I', HOOK, mode, port, MARK]}
    return entry('tell'), entry('ask')


def ours(h):
    """An entry this script installed, in either form: the marker ends the shell command, or is the last argument."""
    if not isinstance(h, dict):
        return False
    cmd, args = h.get('command'), h.get('args')
    return (isinstance(cmd, str) and MARK in cmd) or (isinstance(args, list) and any(isinstance(a, str) and MARK in a for a in args))


def windows_python(python):
    """Why this Python cannot be the hook's program on Windows, or ''."""
    low = python.replace('/', '\\').lower()
    if '\\windowsapps\\' in low:
        return ('This Python runs from the Microsoft Store folder (%s), whose path changes with each update and may not '
                'start without a shell. Install Python from python.org (docs/WINDOWS.md), then run this again with py.' % python)
    if low.endswith(('.cmd', '.bat')):
        return 'This Python is a .cmd or .bat file (%s), which Claude Code cannot start without a shell.' % python
    if low.rsplit('\\', 1)[-1] == 'pythonw.exe':
        return 'Run this with python.exe or py, not pythonw.exe, so the hook has a console to answer on.'
    if not os.path.isabs(python):
        return 'Python reports no full path for itself (%s), so the hook could not name it.' % python
    return ''


def installed_claude_version():
    """The version of the claude command on PATH, or None if there is none or it cannot be read quickly. A .cmd
    shim (an npm install on Windows) needs a shell to run, so it is not asked."""
    exe = shutil.which('claude')
    if not exe or (os.name == 'nt' and not exe.lower().endswith('.exe')):
        return None
    try:
        out = subprocess.run([exe, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', out)
    return tuple(int(x) for x in m.groups()) if m else None


def read(path):
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:     # closed at once: Windows will not replace a file that is still open
        return f.read()


def run(remove=False, win=None, settings=None, port=None, python=None, claude_version=installed_claude_version):
    """Install, or with remove take out, the hooks. Returns the line to print; exits with a message on refusal."""
    win = os.name == 'nt' if win is None else win
    P = os.path.abspath(settings or os.environ.get('MOTET_CLAUDE_SETTINGS') or os.path.expanduser('~/.claude/settings.json'))
    PORT = port if port is not None else os.environ.get('PORT', '8793')
    if not PORT.isdigit():
        sys.exit('PORT must be a number')
    notes = []
    if win:
        python = python or sys.executable
        why = windows_python(python)
        if why and not remove:
            sys.exit(why)
        TELL, ASK = exec_commands(PORT, python)
        if not remove:
            have = claude_version() if claude_version else None
            need = '.'.join(map(str, MIN_CLAUDE))
            if have is not None and have < MIN_CLAUDE:
                sys.exit('Claude Code %s is installed; these hooks need %s or later. Update Claude Code, then run this again.'
                         % ('.'.join(map(str, have)), need))
            notes.append('These hooks need Claude Code %s or later%s. The Claude desktop app carries its own copy; keep '
                         'it up to date too.' % (need, '' if have else ' (no claude command found to check)'))
            notes.append('Checked by automated tests only, not yet against a real Claude Code prompt on a Windows PC.')
    else:
        TELL, ASK = shell_commands(PORT)

    before = read(P)
    d = json.loads(before) if before.strip() else {}
    if before:
        shutil.copy(P, '%s.live-room-backup-%s-%d' % (P, time.strftime('%Y%m%d-%H%M%S'), os.getpid()))
    hooks = d.setdefault('hooks', {})
    for ev, cmd, extra in (('Notification', TELL, {}), ('Stop', TELL, {}), ('SubagentStop', TELL, {}), ('PermissionRequest', ASK, {'timeout': 330})):
        groups = []
        for g in hooks.get(ev, []):
            mine = [h for h in g.get('hooks', []) if ours(h)]
            rest = [h for h in g.get('hooks', []) if not ours(h)]
            if rest or not mine:                       # keep the group, minus only our own entries
                groups.append(dict(g, hooks=rest) if mine else g)
        if not remove:
            groups.append({'hooks': [dict(dict({'type': 'command'}, **cmd), **extra)]})
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if not hooks:
        d.pop('hooks')
    os.makedirs(os.path.dirname(P), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(P), prefix='.settings-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(d, f, indent=2)
    # Compare again at the last moment. This narrows, but cannot fully close, the window in which another program
    # could save settings.json; Claude Code offers no lock to coordinate on.
    now = read(P)
    if now != before:
        os.unlink(tmp)
        sys.exit('settings.json changed while this was running; nothing written. Run it again.')
    try:
        os.replace(tmp, P)
    except OSError:
        os.unlink(tmp)
        raise
    return '\n'.join(['%s -> %s' % ('removed' if remove else 'installed', P)] + notes)


if __name__ == '__main__':
    print(run(remove='--remove' in sys.argv))
