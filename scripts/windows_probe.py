#!/usr/bin/env python3
"""Windows probe: a read-only report of facts only a real Windows PC can tell us.

Run:  py scripts\\windows_probe.py

It reads the registry, folder listings, a few small settings files and `--version` / `--help`
output from command-line tools. It writes no files, opens no network connection, starts no
app window, sends no message and changes nothing. The output is redacted (your profile path,
user name, computer name, e-mail addresses and key-like strings) but READ IT before posting.
"""

import csv
import io
import json
import os
import platform
import re
import subprocess
import sys
import time

BUDGET_SECONDS = 50          # the whole run stays well under a minute
_START = time.time()
APPX_PATTERN = 'OpenAI|ChatGPT|Codex|Anthropic|Claude|Cursor|Antigravity'


# ---------------------------------------------------------------- redaction

def _path_regex(path):
    parts = [p for p in re.split(r'[\\/]+', path) if p]
    if not parts:
        return None
    body = r'[\\/]+'.join(re.escape(p) for p in parts)
    lead = r'(?:\\\\\?\\)?' if re.match(r'^[A-Za-z]:$', parts[0]) else ''
    return re.compile(lead + body, re.IGNORECASE)


COMMON_WORDS = {'user', 'users', 'admin', 'administrator', 'code', 'test', 'guest', 'default', 'public', 'owner',
                'home', 'name', 'root', 'main', 'work', 'dev', 'me', 'my', 'pc', 'laptop', 'desktop'}
EDGE = r'\\/._~@-'


def _name_regex(name):
    """How a name is found in the output.
    4+ characters and not an everyday word: anywhere (a pipe name may embed it).
    2-3 characters and not an everyday word: anywhere it is not part of a longer word of letters.
    1 character, or an everyday word: only next to a path or name separator, so ordinary words are left alone."""
    esc = re.escape(name)
    common = name.lower() in COMMON_WORDS
    if len(name) >= 4 and not common:
        return re.compile(esc, re.IGNORECASE)
    if len(name) >= 2 and not common:
        return re.compile(r'(?<![A-Za-z])' + esc + r'(?![A-Za-z])', re.IGNORECASE)
    edge = '[' + EDGE + ']'
    return re.compile('(?:(?<=' + edge + ')' + esc + r'(?![A-Za-z0-9])|(?<![A-Za-z0-9])' + esc + '(?=' + edge + '))',
                      re.IGNORECASE)


EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
UUID = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
PREFIXED_KEY = re.compile(
    r'\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|AKIA|AIza)[-_A-Za-z0-9]{8,}')
JWT = re.compile(r'\beyJ[\w-]{5,}\.[\w-]{5,}(?:\.[\w-]*)?')
BEARER = re.compile(r'(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}')
# A secret label followed by any value, unless the value is only one of the probe's own status phrases.
ASSIGNED = re.compile(r'(?im)\b(token|secret|password|passwd|api[_-]?key|apikey)(["\']?\s*[=:]\s*["\']?)'
                      r'(?!(?:not|found|yes|no|none|set|error|n/a|skipped)(?=$|[\s;(]))[^\s<"\']+')
LONG_HEX = re.compile(r'\b[0-9a-fA-F]{16,}\b')
LONG_RUN = re.compile(r'[A-Za-z0-9+/_=-]{32,}')


def _short_path(path):
    """The 8.3 form of a path, read-only, on Windows; '' when unavailable."""
    if os.name != 'nt' or not path:
        return ''
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        return buf.value if 0 < n < 1024 else ''
    except Exception:
        return ''


def _name_variants(name):
    """A name as it may appear: as given, without separators, dotted, joined and URL-encoded."""
    if not name:
        return []
    out = {name}
    out.add(re.sub(r'[\s._-]+', '', name))
    for sep in ('.', '_', '-'):
        out.add(re.sub(r'\s+', sep, name))
    out.add(name.replace(' ', '%20'))
    try:
        from urllib.parse import quote
        out.add(quote(name))
    except Exception:
        pass
    return [v for v in out if v]


def _short_stems(name):
    """The first six characters an 8.3 short name may keep (spaces and dots are dropped, _ and - stay)."""
    stems = set()
    for pattern in (r'[^A-Za-z0-9_-]', r'[^A-Za-z0-9]'):
        stem = re.sub(pattern, '', name or '')[:6]
        if stem:
            stems.add(stem)
    return stems


def make_context(env=None):
    """The things to hide, gathered from the environment (or from a given dict)."""
    real = env is None
    env = os.environ if real else env
    profile = env.get('USERPROFILE') or os.path.expanduser('~')
    user = env.get('USERNAME') or env.get('USER') or ''
    if not user:
        try:
            import getpass
            user = getpass.getuser()
        except Exception:
            user = ''
    pc = env.get('COMPUTERNAME') or platform.node() or ''
    ctx = {
        'profile': profile,
        'localappdata': env.get('LOCALAPPDATA') or '',
        'appdata': env.get('APPDATA') or '',
        'user': user,
        'pc': pc,
    }
    if real:
        ctx['short_paths'] = [(_short_path(ctx[k]), n) for k, n in
                              (('localappdata', '%LOCALAPPDATA%'), ('appdata', '%APPDATA%'),
                               ('profile', '%USERPROFILE%'))]
    return ctx


def redact(text, ctx=None):
    """Return text with personal paths, names, e-mails and key-like strings replaced.

    Placeholders are put in as sentinels and swapped in at the very end, so a later step
    can never rewrite part of an earlier placeholder."""
    if text is None:
        return ''
    ctx = ctx or make_context()
    text = str(text).replace('\x00', '')
    marks = []

    def mark(value):
        marks.append(value)
        return '\x00%d\x00' % (len(marks) - 1)

    text = EMAIL.sub(lambda m: mark('<email>'), text)
    paths = [(ctx.get('localappdata'), '%LOCALAPPDATA%'),
             (ctx.get('appdata'), '%APPDATA%'),
             (ctx.get('profile'), '%USERPROFILE%')]
    paths += list(ctx.get('short_paths') or [])
    paths = [(p, n) for p, n in paths if p and len(p) > 3]
    paths.sort(key=lambda pn: -len(pn[0]))          # longest first: AppData lives inside the profile
    for path, name in paths:
        rx = _path_regex(path)
        if rx:
            text = rx.sub(lambda m, n=name: mark(n), text)
    user = ctx.get('user') or ''
    profile = ctx.get('profile') or ''
    base = [p for p in re.split(r'[\\/]+', profile) if p][-1:] if profile else []
    names = {}
    for a in [user] + base:
        for v in _name_variants(a):
            names[v] = '<user>'
    pc = ctx.get('pc') or ''
    if pc:
        names[pc] = '<pc>'
    for alias in sorted(names, key=len, reverse=True):      # longest first: a PC name may contain the user name
        text = _name_regex(alias).sub(lambda m, v=names[alias]: mark(v), text)
    for stem in _short_stems(user):                          # 8.3 short forms: first six characters, ~1, ~2 ...
        text = re.compile(r'(?<![A-Za-z0-9])' + re.escape(stem) + r'~\d+(?![A-Za-z0-9])', re.IGNORECASE).sub(
            lambda m: mark('<user>'), text)
    text = UUID.sub('<id>', text)
    text = JWT.sub('<key>', text)
    text = PREFIXED_KEY.sub('<key>', text)
    text = BEARER.sub(lambda m: m.group(1) + ' <key>', text)
    text = ASSIGNED.sub(lambda m: m.group(1) + m.group(2) + '<key>', text)
    text = LONG_HEX.sub('<id>', text)
    text = LONG_RUN.sub('<key>', text)
    return re.sub('\x00(\\d+)\x00', lambda m: marks[int(m.group(1))], text)


# ---------------------------------------------------------------- helpers

def remaining():
    return BUDGET_SECONDS - (time.time() - _START)


SYSTEM32 = os.path.join(os.environ.get('SystemRoot') or r'C:\Windows', 'System32')
POWERSHELL = os.path.join(SYSTEM32, 'WindowsPowerShell', 'v1.0', 'powershell.exe')


def which(name, windows=None):
    """Find a command on PATH only (never the current folder). Returns a path or None.
    On Windows only names with an extension from PATHEXT count (an extensionless script beside name.cmd is no program)."""
    windows = (os.name == 'nt') if windows is None else windows
    if windows:
        exts = [e for e in (os.environ.get('PATHEXT') or '.COM;.EXE;.BAT;.CMD').split(';') if e]
    else:
        exts = ['']
    for d in os.environ.get('PATH', '').split(os.pathsep):
        d = d.strip('"')
        if not d or not os.path.isabs(d):
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if os.path.isfile(cand) and (windows or os.access(cand, os.X_OK)):
                return cand
    return None


def not_run_reason(path):
    """Why a command must not be executed, or '' if it may be."""
    low = path.lower().replace('/', '\\')
    if '\\windowsapps\\' in low:
        return 'app alias, not run'
    if low.endswith(('.cmd', '.bat')):
        return 'script shim, not run'
    return ''


def run(cmd, timeout=10):
    """Run a command without a shell. Returns (exit code, combined output) or (None, 'error: ...').

    The wait is bounded: on a timeout the whole process tree is killed (on Windows a direct kill leaves
    grandchildren holding the pipe open) and the output is read by a helper thread we do not wait for."""
    import threading
    left = remaining()
    if left < 1:
        return None, 'error: skipped, time budget used up'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=flags)
    except Exception as e:
        return None, 'error: %s' % type(e).__name__
    box = []

    def reader():
        try:
            box.append(p.stdout.read())
        except Exception:
            box.append(b'')
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(min(timeout, left))
    if t.is_alive():
        try:
            if os.name == 'nt':
                subprocess.run([os.path.join(SYSTEM32, 'taskkill.exe'), '/T', '/F', '/PID', str(p.pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                p.kill()
        except Exception:
            pass
        t.join(2)
        return None, 'error: timed out'
    try:
        rc = p.wait(timeout=2)
    except Exception:
        return None, 'error: timed out'
    return rc, (box[0] if box else b'').decode('utf-8', 'replace')


def first_line(text, limit=120):
    for line in (text or '').splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ''


def env_dir(name):
    return os.environ.get(name) or ''


def list_dir(path):
    """Names in a folder, or (None, reason)."""
    try:
        return sorted(os.listdir(path)), ''
    except FileNotFoundError:
        return None, 'not found'
    except PermissionError:
        return None, 'error: access denied'
    except OSError as e:
        return None, 'error: %s' % type(e).__name__


_APPX = {}


def appx_packages():
    """Matching Appx packages as (name, version, install location); cached. Raises on failure."""
    if 'v' in _APPX:
        if isinstance(_APPX['v'], str):
            raise RuntimeError(_APPX['v'])
        return _APPX['v']
    script = ("Get-AppxPackage | Where-Object { $_.Name -match '%s' } | ForEach-Object "
              "{ $_.Name + '|' + $_.Version + '|' + $_.InstallLocation }" % APPX_PATTERN)
    rc, out = run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script], timeout=25)
    if rc != 0:
        _APPX['v'] = out.strip() if rc is None else 'error: powershell exit %s' % rc
        raise RuntimeError(_APPX['v'])
    rows = []
    for line in out.splitlines():
        parts = line.strip().split('|', 2)
        if len(parts) == 3 and parts[0]:
            rows.append(tuple(parts))
    _APPX['v'] = rows
    return rows


def appx_matching(pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    return [r for r in appx_packages() if rx.search(r[0])]


def uninstall_entries(pattern):
    """(display name, display version) from the Uninstall registry keys. Names and versions only."""
    import winreg
    rx = re.compile(pattern, re.IGNORECASE)
    found = []
    roots = [(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_LOCAL_MACHINE, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
             (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')]
    for hive, sub in roots:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, name) as k:
                        disp = winreg.QueryValueEx(k, 'DisplayName')[0]
                        try:
                            ver = winreg.QueryValueEx(k, 'DisplayVersion')[0]
                        except OSError:
                            ver = '?'
                except OSError:
                    continue
                if rx.search(str(disp)):
                    found.append((str(disp), str(ver)))
    return found


def find_files(root, matcher, maxdepth=4, cap=4000):
    """Walk root to maxdepth looking for names that match. Returns (hits, problems)."""
    hits, problems, seen = [], [], [0]

    def walk(path, depth):
        if seen[0] > cap:
            return
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            problems.append('unreadable (access denied)')
            return
        except OSError as e:
            problems.append('unreadable (%s)' % type(e).__name__)
            return
        for e in entries:
            seen[0] += 1
            if matcher(e.name):
                hits.append(e.path)
            try:
                if depth < maxdepth and e.is_dir(follow_symlinks=False):
                    walk(e.path, depth + 1)
            except OSError:
                pass

    walk(root, 0)
    if seen[0] > cap:
        problems.append('search stopped at %d entries' % cap)
    return hits, problems


def program_dirs(pattern):
    """Install folders under LOCALAPPDATA\\Programs and Program Files whose name matches."""
    rx = re.compile(pattern, re.IGNORECASE)
    roots = [os.path.join(env_dir('LOCALAPPDATA'), 'Programs') if env_dir('LOCALAPPDATA') else '',
             env_dir('ProgramFiles'), env_dir('ProgramFiles(x86)')]
    out = []
    for root in roots:
        if not root:
            continue
        names, _ = list_dir(root)
        for n in names or []:
            if rx.search(n):
                out.append(os.path.join(root, n))
    return out


# ---------------------------------------------------------------- sections

def check_windows_python():
    lines = []
    try:
        v = sys.getwindowsversion()
        lines.append('Windows version: %d.%d build %d' % (v.major, v.minor, v.build))
    except Exception as e:
        lines.append('Windows version: error: %s' % type(e).__name__)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as k:
            def val(n):
                try:
                    return str(winreg.QueryValueEx(k, n)[0])
                except OSError:
                    return '?'
            lines.append('Windows release: %s %s (build %s.%s)' % (
                val('ProductName'), val('DisplayVersion'), val('CurrentBuildNumber'), val('UBR')))
    except Exception as e:
        lines.append('Windows release: error: %s' % type(e).__name__)
    lines.append('Python: %s (%s)' % (platform.python_version(), platform.architecture()[0]))
    lines.append('Machine: %s' % platform.machine())
    return lines


def probe_codex_cli(path, label):
    why = not_run_reason(path)
    if why:
        return ['%s: found (%s)' % (label, why)]
    lines = []
    rc, out = run([path, '--version'])
    lines.append('%s --version: %s' % (label, first_line(out) if rc == 0 else
                                       (out if rc is None else 'error: exit %s' % rc)))
    for sub in ('queue', 'app-server'):
        rc, out = run([path, sub, '--help'])
        if rc is None:
            lines.append('%s %s --help: %s' % (label, sub, out))
            continue
        usage = ''
        for line in out.splitlines():
            if re.match(r'(?i)^\s*usage:', line):
                usage = line
                break
        if rc != 0:
            verdict = 'failed'
        elif re.search(r'(?<![\w-])' + re.escape(sub) + r'(?![\w-])', usage):
            verdict = 'ok'
        else:
            verdict = 'exit 0 but the help is not for this subcommand (probably not supported)'
        lines.append('%s %s --help: %s (exit %s); first line: %s' % (label, sub, verdict, rc, first_line(out) or '(none)'))
    return lines


def check_codex_app():
    lines = []
    roots = []
    try:
        pk = appx_matching('OpenAI|ChatGPT|Codex')
        if pk:
            for name, ver, loc in pk:
                lines.append('Appx package: found: %s version %s' % (name, ver))
                lines.append('Install location: %s' % (loc or '(none)'))
                if loc:
                    roots.append(loc)
        else:
            lines.append('Appx package: not found')
    except Exception as e:
        lines.append('Appx package: %s' % (str(e) if str(e).startswith('error') else 'error: %s' % e))
    folders = program_dirs('OpenAI|ChatGPT|Codex')
    if folders:
        for f in folders:
            lines.append('Install folder: found: %s' % f)
            roots.append(f)
    else:
        lines.append('Install folders (Programs, Program Files): not found')
    try:
        for name, ver in uninstall_entries('OpenAI|ChatGPT|Codex'):
            lines.append('Uninstall entry: found: %s version %s' % (name, ver))
    except Exception as e:
        lines.append('Uninstall entry: error: %s' % type(e).__name__)

    exes = []
    if roots:
        for root in roots:
            hits, problems = find_files(
                root, lambda n: n.lower() in ('codex.exe', 'codex.cmd') or n.lower().startswith('codex-cli'))
            for h in hits:
                lines.append('Bundled codex file: found: %s' % h)
                if h.lower().endswith(('.exe', '.cmd')):
                    exes.append(h)
            if not hits:
                lines.append('Bundled codex file in %s: not found' % os.path.basename(root.rstrip('\\/')))
            for p in sorted(set(problems)):
                lines.append('Search in %s: %s' % (os.path.basename(root.rstrip('\\/')), p))
    else:
        lines.append('Bundled codex file: not searched (no install folder)')

    on_path = which('codex')
    lines.append('codex on PATH: %s' % ('found: %s' % on_path if on_path else 'not found'))
    probed = []
    for label, path in (('PATH codex', on_path), ('bundled codex', exes[0] if exes else None)):
        if path and path.lower() not in [p.lower() for p in probed]:
            probed.append(path)
            lines.extend(probe_codex_cli(path, label))
    if not probed:
        lines.append('codex CLI: not found, so not run')
    return lines


def check_codex_data():
    base = os.path.join(os.path.expanduser('~'), '.codex')
    names, why = list_dir(base)
    if names is None:
        return ['%%USERPROFILE%%\\.codex: %s' % ('not found' if why == 'not found' else why)]
    lines = ['%USERPROFILE%\\.codex: found']
    for sub in ('sessions', 'ipc'):
        lines.append('  %s: %s' % (sub, 'found' if sub in names else 'not found'))
    if 'ipc' in names:
        entries, why = list_dir(os.path.join(base, 'ipc'))
        if entries is None:
            lines.append('  ipc entries: %s' % why)
        else:
            lines.append('  ipc entries (%d): %s' % (len(entries), ', '.join(entries[:40]) or '(empty)'))
    return lines


def check_pipes():
    names, why = list_dir('\\\\.\\pipe\\\\')
    if names is None:
        return ['Named pipes: %s' % why]
    rx = re.compile('codex|openai|chatgpt|claude|anthropic|cursor|antigravity', re.IGNORECASE)
    hits = [n for n in names if rx.search(n)]
    lines = ['Named pipes scanned: %d, matching: %d' % (len(names), len(hits))]
    lines.extend('  pipe: found: %s' % n for n in hits[:60])
    if not hits:
        lines.append('  matching pipes: not found')
    return lines


def check_url_handlers():
    import winreg
    lines = []
    for scheme in ('claude', 'codex', 'cursor', 'antigravity'):
        for label, hive in (('HKCU', winreg.HKEY_CURRENT_USER), ('HKCR', winreg.HKEY_CLASSES_ROOT)):
            sub = (r'Software\Classes\%s' if label == 'HKCU' else r'%s') % scheme
            try:
                with winreg.OpenKey(hive, sub) as k:
                    try:
                        winreg.QueryValueEx(k, 'URL Protocol')
                        proto = 'URL Protocol set'
                    except OSError:
                        proto = 'no URL Protocol value'
                    try:
                        with winreg.OpenKey(k, r'shell\open\command') as c:
                            cmd = str(winreg.QueryValueEx(c, '')[0])
                    except OSError:
                        cmd = '(no open command)'
                lines.append('%s:// %s: found (%s): %s' % (scheme, label, proto, cmd))
            except FileNotFoundError:
                lines.append('%s:// %s: not found' % (scheme, label))
            except OSError as e:
                lines.append('%s:// %s: error: %s' % (scheme, label, type(e).__name__))
    return lines


def check_claude():
    lines = []
    try:
        pk = appx_matching('Anthropic|Claude')
        if pk:
            for name, ver, loc in pk:
                lines.append('Claude Appx: found: %s version %s at %s' % (name, ver, loc))
        else:
            lines.append('Claude Appx: not found')
    except Exception as e:
        lines.append('Claude Appx: %s' % (str(e) if str(e).startswith('error') else 'error: %s' % e))
    local = env_dir('LOCALAPPDATA')
    folder = os.path.join(local, 'AnthropicClaude') if local else ''
    names, why = list_dir(folder) if folder else (None, 'error: LOCALAPPDATA not set')
    if names is None:
        lines.append('%%LOCALAPPDATA%%\\AnthropicClaude: %s' % why)
    else:
        vers = [n for n in names if n.lower().startswith('app-')]
        lines.append('%%LOCALAPPDATA%%\\AnthropicClaude: found; versions: %s' % (', '.join(vers) or 'none'))
    try:
        for name, ver in uninstall_entries(r'^Claude\b|Anthropic'):
            lines.append('Uninstall entry: found: %s version %s' % (name, ver))
    except Exception as e:
        lines.append('Uninstall entry: error: %s' % type(e).__name__)

    cli = which('claude')
    if cli:
        lines.append('claude on PATH: found: %s' % cli)
        why = not_run_reason(cli)
        if why:
            lines.append('claude --version: not run (%s)' % why)
        else:
            rc, out = run([cli, '--version'])
            lines.append('claude --version: %s' % (first_line(out) if rc == 0 else
                                                   (out if rc is None else 'error: exit %s' % rc)))
    else:
        lines.append('claude on PATH: not found')

    settings = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
    if not os.path.isfile(settings):
        lines.append('%USERPROFILE%\\.claude\\settings.json: not found')
    else:
        lines.append('%USERPROFILE%\\.claude\\settings.json: found')
        try:
            with open(settings, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            hooks = data.get('hooks') if isinstance(data, dict) else None
            if isinstance(hooks, dict):
                events = [k if re.match(r'^[A-Za-z]{1,30}$', k) else '<other>' for k in hooks]
                lines.append('  hooks section: found; events: %s' % (', '.join(sorted(events)) or '(none)'))
            elif hooks is not None:
                lines.append('  hooks section: found (not an object)')
            else:
                lines.append('  hooks section: not found')
        except Exception as e:
            lines.append('  hooks section: error: could not read (%s)' % type(e).__name__)

    where = which('bash')
    lines.append('bash on PATH: %s' % ('found: %s' % where if where else 'not found'))
    gb = os.path.join(env_dir('ProgramFiles') or r'C:\Program Files', 'Git', 'bin', 'bash.exe')
    lines.append('Git Bash (Program Files\\Git\\bin\\bash.exe): %s' % ('found' if os.path.isfile(gb) else 'not found'))
    env = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    lines.append('CLAUDE_CODE_GIT_BASH_PATH: %s' % ('set: %s' % env if env else 'not set'))
    return lines


def check_editor(label, pattern, folder_names, cli_names):
    lines = []
    try:
        pk = appx_matching(pattern)
        lines.append('%s Appx: %s' % (label, 'found: ' + '; '.join('%s %s' % (p[0], p[1]) for p in pk) if pk else 'not found'))
    except Exception as e:
        lines.append('%s Appx: %s' % (label, str(e) if str(e).startswith('error') else 'error: %s' % e))
    folders = program_dirs('|'.join(folder_names))
    lines.append('%s install folder: %s' % (label, 'found: ' + '; '.join(folders) if folders else 'not found'))
    try:
        entries = uninstall_entries(pattern)
        lines.append('%s uninstall entry: %s' % (
            label, 'found: ' + '; '.join('%s version %s' % e for e in entries) if entries else 'not found'))
    except Exception as e:
        lines.append('%s uninstall entry: error: %s' % (label, type(e).__name__))
    for cli in cli_names:
        lines.append('%s on PATH: %s' % (cli, 'found: %s' % which(cli) if which(cli) else 'not found'))
    return lines


def check_editors():
    # Versions come from the registry; the editors' own launchers are not run, since they can open a window.
    return (check_editor('Cursor', 'Cursor', ['cursor'], ['cursor'])
            + check_editor('Antigravity', 'Antigravity', ['antigravity'], ['antigravity']))


def check_terminal():
    wt = which('wt')
    return ['wt.exe on PATH: %s' % ('found: %s' % wt if wt else 'not found')]


def check_moonshine():
    return ['Moonshine wheel lookup: skipped (needs network)']


def check_processes():
    lines = []
    rc, out = run([os.path.join(SYSTEM32, 'tasklist.exe'), '/fo', 'csv', '/nh'])
    if rc != 0:
        return ['Process list: %s' % (out if rc is None else 'error: tasklist exit %s' % rc)]
    counts = {}
    rx = re.compile(r'^(codex|chatgpt|claude|cursor|antigravity)', re.IGNORECASE)
    for row in csv.reader(io.StringIO(out)):
        if row and rx.match(row[0]):
            counts[row[0]] = counts.get(row[0], 0) + 1
    if counts:
        for name in sorted(counts):
            lines.append('Running: found: %s x%d' % (name, counts[name]))
    else:
        lines.append('Running codex/ChatGPT/Claude/Cursor/Antigravity processes: not found')
    if not any(n.lower() == 'codex.exe' for n in counts):
        lines.append('codex.exe command line: not checked (codex.exe not running)')
        return lines
    cmdlines, how = None, ''
    rc, out = run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command',
                   "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
                   "ForEach-Object { $_.CommandLine }"], timeout=15)
    if rc == 0:
        cmdlines, how = [l.strip() for l in out.splitlines() if l.strip()], 'PowerShell Get-CimInstance'
    else:
        err = out if rc is None else 'powershell exit %s' % rc
        wmic = os.path.join(SYSTEM32, 'wbem', 'wmic.exe')
        if os.path.isfile(wmic):
            rc, out = run([wmic, 'process', 'where', "name='codex.exe'", 'get', 'commandline'])
            if rc == 0:
                cmdlines, how = [l.strip() for l in out.splitlines()[1:] if l.strip()], 'wmic'
        if cmdlines is None:
            lines.append('codex.exe command line: error: %s' % err)
            return lines
    lines.append('codex.exe command lines readable (via %s): %s' % (how, 'yes (%d)' % len(cmdlines) if cmdlines else 'no'))
    joined = '\n'.join(cmdlines).lower()
    for word in ('exec-server', 'app-server'):
        lines.append('  word "%s" appears: %s' % (word, 'yes' if word in joined else 'no'))
    return lines


SECTIONS = [
    ('1. Windows and Python', check_windows_python),
    ('2. Codex desktop app', check_codex_app),
    ('3. Codex data folder', check_codex_data),
    ('4. Named pipes', check_pipes),
    ('5. URL handlers', check_url_handlers),
    ('6. Claude', check_claude),
    ('7. Cursor and Antigravity', check_editors),
    ('8. Windows Terminal', check_terminal),
    ('9. Moonshine', check_moonshine),
    ('10. Process list', check_processes),
]

FINAL_LINE = 'Please read this output before posting it, and remove anything you would rather not share.'


def main():
    if os.name != 'nt':
        print('This script is for Windows.')
        return 0
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    ctx = make_context()
    out = ['Motet Windows probe (read-only; nothing was changed, written or sent)']
    for label, fn in SECTIONS:
        out.append('')
        out.append('== %s ==' % label)
        try:
            out.extend(fn())
        except Exception as e:
            out.append('error: %s' % type(e).__name__)
    out.append('')
    out.append(FINAL_LINE)
    for line in out:
        print(redact(line, ctx))
    return 0


if __name__ == '__main__':
    sys.exit(main())
