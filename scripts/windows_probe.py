#!/usr/bin/env python3
"""Windows probe: a read-only report of facts only a real Windows PC can tell us.

Run:  py scripts\\windows_probe.py

It reads the registry, folder listings, a few small settings files, app package manifests, file
permissions, and `--version` / `--help` output from command-line tools. It never opens or connects to any
named pipe: for the Codex app's control pipe it only checks that the name is listed. It writes no files,
opens no network connection, starts no app window, sends no message and changes nothing. The output is redacted (your profile path,
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


IO_REPARSE_TAG_APPEXECLINK = 0x8000001B


def not_run_reason(path, lstat=os.lstat):
    """Why a command must not be executed, or '' if it may be. An app execution alias (a link in
    %LOCALAPPDATA%\\Microsoft\\WindowsApps, or any file Windows marks as one) is told apart from a real
    file inside a Store app package (C:\\Program Files\\WindowsApps\\...); neither is run."""
    low = path.lower().replace('/', '\\')
    try:
        tag = getattr(lstat(path), 'st_reparse_tag', 0)
    except (OSError, ValueError):
        tag = 0
    if tag == IO_REPARSE_TAG_APPEXECLINK or '\\microsoft\\windowsapps\\' in low:
        return 'app execution alias, not run'
    if '\\windowsapps\\' in low:
        return 'file inside a Store app package, not run'
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


def parse_appx(out):
    """Rows of the Get-AppxPackage query: name|version|install location|family name|publisher id.
    ('|' cannot appear in a Windows path, a package name or a version.)"""
    rows = []
    for line in out.splitlines():
        parts = line.strip().split('|')
        if len(parts) >= 3 and parts[0]:
            parts += [''] * (5 - len(parts))
            rows.append({'name': parts[0], 'version': parts[1], 'location': parts[2], 'family': parts[3],
                         'publisher': parts[4]})
    return rows


def appx_rows():
    """Matching Appx packages as dicts; cached. Raises on failure."""
    if 'v' in _APPX:
        if isinstance(_APPX['v'], str):
            raise RuntimeError(_APPX['v'])
        return _APPX['v']
    script = ("Get-AppxPackage | Where-Object { $_.Name -match '%s' } | ForEach-Object "
              "{ $_.Name + '|' + $_.Version + '|' + $_.InstallLocation + '|' + $_.PackageFamilyName + '|' + $_.PublisherId }"
              % APPX_PATTERN)
    rc, out = run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script], timeout=25)
    if rc != 0:
        _APPX['v'] = out.strip() if rc is None else 'error: powershell exit %s' % rc
        raise RuntimeError(_APPX['v'])
    _APPX['v'] = parse_appx(out)
    return _APPX['v']


def appx_packages():
    """Matching Appx packages as (name, version, install location)."""
    return [(r['name'], r['version'], r['location']) for r in appx_rows()]


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
    try:
        lines.extend(check_codex_pipe(names))
    except Exception as e:
        lines.append('codex-ipc: error: %s' % type(e).__name__)
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


# ---------------------------------------------------------------- Windows security, read-only (ctypes)

WELL_KNOWN_SIDS = {
    'S-1-1-0': 'Everyone', 'S-1-3-0': 'CREATOR OWNER', 'S-1-5-7': 'Anonymous', 'S-1-5-4': 'INTERACTIVE',
    'S-1-5-11': 'Authenticated Users', 'S-1-5-18': 'SYSTEM', 'S-1-5-19': 'LOCAL SERVICE', 'S-1-5-20': 'NETWORK SERVICE',
    'S-1-5-32-544': 'Administrators', 'S-1-5-32-545': 'Users', 'S-1-15-2-1': 'ALL APPLICATION PACKAGES',
    'S-1-15-2-2': 'ALL RESTRICTED APPLICATION PACKAGES',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464': 'TrustedInstaller',
}
ACE_KINDS = {0: 'allow', 1: 'deny', 9: 'allow, with a condition', 10: 'deny, with a condition'}


def sid_text(raw):
    """A SID's S-1-... form from its bytes, without asking Windows (and so never a domain controller)."""
    if len(raw) < 8 or raw[0] != 1 or len(raw) < 8 + 4 * raw[1]:
        raise ValueError('not a SID')
    authority = int.from_bytes(raw[2:8], 'big')
    subs = [int.from_bytes(raw[8 + 4 * i:12 + 4 * i], 'little') for i in range(raw[1])]
    return 'S-1-%d' % authority + ''.join('-%d' % s for s in subs)


def sid_label(sid, mine=''):
    """Who a SID is, in words. This user is 'you'. Another account keeps only its last number, and an app package or
    capability only its kind, so no machine or domain number is shown; nothing is looked up."""
    if mine and sid == mine:
        return 'you'
    if sid in WELL_KNOWN_SIDS:
        # a long SID would read as a key to redact(), and its kind is already said by its name
        return '%s (%s)' % (WELL_KNOWN_SIDS[sid], sid if len(sid) < 24 else '-'.join(sid.split('-')[:4]) + '-...')
    parts = sid.split('-')
    if sid.startswith('S-1-5-21-') and len(parts) >= 5:
        rid = int(parts[-1]) if parts[-1].isdigit() else 0
        if 500 <= rid <= 503:
            return 'a built-in account (S-1-5-21-...-%s)' % parts[-1]
        if rid < 1000:
            return 'a domain group (S-1-5-21-...-%s)' % parts[-1]
        return 'another account (S-1-5-21-...-%s)' % parts[-1]
    if sid.startswith('S-1-5-5-'):
        return 'this logon session (S-1-5-5-...)'
    if sid.startswith('S-1-5-80-'):
        return 'a Windows service (S-1-5-80-...)'
    if sid.startswith('S-1-15-2-'):
        return 'an app package (S-1-15-2-...)'
    if sid.startswith('S-1-15-3-'):
        return 'an app capability (S-1-15-3-...)'
    if sid.startswith('S-1-16-'):
        return 'integrity level (%s)' % sid
    return 'other (%s)' % ('-'.join(parts[:4]) + ('-...' if len(parts) > 4 else ''))


def rights(mask):
    """An access mask in words, with the number."""
    if mask & 0x10000000 or mask & 0x1F01FF == 0x1F01FF:
        words = ['full control']
    else:
        words = [w for w, bits, generic in (('read', 0x120089, 0x80000000), ('write', 0x120116, 0x40000000),
                                             ('execute', 0x1200A0, 0x20000000))
                 if mask & bits == bits or mask & generic]
        if mask & 0x10000 and 'write' not in words:
            words.append('delete')
        if not words:
            words = ['some rights']
    return '%s (0x%x)' % (', '.join(words), mask)


def parse_acl(raw):
    """(kind, flags, mask, SID) for each entry of an ACL given as bytes."""
    if len(raw) < 8:
        raise ValueError('not an ACL')
    count, pos, out = int.from_bytes(raw[4:6], 'little'), 8, []
    for _ in range(count):
        kind, flags, size = raw[pos], raw[pos + 1], int.from_bytes(raw[pos + 2:pos + 4], 'little')
        if size < 8 or pos + size > len(raw):
            raise ValueError('bad ACL entry')
        sid = ''
        if kind in ACE_KINDS:
            mask = int.from_bytes(raw[pos + 4:pos + 8], 'little')
            sid = sid_text(raw[pos + 8:pos + size])
        else:
            mask = 0
        out.append((kind, flags, mask, sid))
        pos += size
    return out


def acl_lines(owner, aces, mine, indent='  '):
    """An owner and access list as lines. `aces` None means no access list at all (anyone may open it)."""
    lines = ['%sowner: %s' % (indent, sid_label(owner, mine) if owner else 'unknown')]
    if aces is None:
        return lines + ['%saccess list: none (anyone may open it)' % indent]
    if not aces:
        return lines + ['%saccess list: empty (no one may open it)' % indent]
    for kind, flags, mask, sid in aces[:24]:
        if kind not in ACE_KINDS:
            lines.append('%sentry of type %d (not shown)' % (indent, kind))
            continue
        lines.append('%s%s %s: %s%s' % (indent, ACE_KINDS[kind], sid_label(sid, mine), rights(mask),
                                        ' (inherited by contents only)' if flags & 0x8 else ''))
    if len(aces) > 24:
        lines.append('%s... %d more entries' % (indent, len(aces) - 24))
    return lines


_WIN = {}


def win():
    """kernel32, advapi32 and friends with their argument types, loaded once (Windows only)."""
    if 'api' in _WIN:
        return _WIN['api']
    import ctypes
    from ctypes import wintypes as w
    k, a = ctypes.WinDLL('kernel32', use_last_error=True), ctypes.WinDLL('advapi32', use_last_error=True)
    H, D, P = w.HANDLE, w.DWORD, ctypes.c_void_p
    k.CloseHandle.argtypes, k.CloseHandle.restype = [H], w.BOOL
    k.OpenProcess.argtypes, k.OpenProcess.restype = [D, w.BOOL, D], H
    k.QueryFullProcessImageNameW.argtypes = [H, D, w.LPWSTR, ctypes.POINTER(D)]
    k.QueryFullProcessImageNameW.restype = w.BOOL
    k.LocalFree.argtypes, k.LocalFree.restype = [P], P
    k.GetCurrentProcess.argtypes, k.GetCurrentProcess.restype = [], H
    k.CreateToolhelp32Snapshot.argtypes, k.CreateToolhelp32Snapshot.restype = [D, D], H
    a.OpenProcessToken.argtypes, a.OpenProcessToken.restype = [H, D, ctypes.POINTER(H)], w.BOOL
    a.GetTokenInformation.argtypes = [H, ctypes.c_int, P, D, ctypes.POINTER(D)]
    a.GetTokenInformation.restype = w.BOOL
    a.GetLengthSid.argtypes, a.GetLengthSid.restype = [P], D
    a.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, ctypes.c_int, D, ctypes.POINTER(P), ctypes.POINTER(P),
                                        ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P)]
    a.GetNamedSecurityInfoW.restype = D
    try:
        k.GetPackageFamilyName.argtypes = [H, ctypes.POINTER(ctypes.c_uint32), w.LPWSTR]
        k.GetPackageFamilyName.restype = ctypes.c_long
    except AttributeError:
        pass                                        # before Windows 8
    _WIN['api'] = (ctypes, w, k, a)
    return _WIN['api']


def _sid_at(ptr):
    ctypes, _, _, a = win()
    return sid_text(ctypes.string_at(ptr, a.GetLengthSid(ptr)))


def _security(get):
    """(owner SID, ACEs or None) through GetNamedSecurityInfoW (`get` fills the pointers)."""
    ctypes, _, k, _ = win()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    err = get(ctypes.byref(owner), ctypes.byref(dacl), ctypes.byref(sd))
    if err:
        raise OSError('error %d' % err)
    try:
        sid = _sid_at(owner) if owner.value else ''
        if not dacl.value:
            return sid, None
        size = int.from_bytes(ctypes.string_at(dacl.value + 2, 2), 'little')
        return sid, parse_acl(ctypes.string_at(dacl.value, size))
    finally:
        k.LocalFree(sd)


OWNER_AND_DACL = 0x1 | 0x4                          # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION


def path_security(path):
    _, _, _, a = win()
    return _security(lambda o, d, s: a.GetNamedSecurityInfoW(path, 1, OWNER_AND_DACL, o, None, d, None, s))  # SE_FILE_OBJECT


def token_sid(process):
    """The account a process runs as, from its token. '' when it cannot be read."""
    ctypes, w, k, a = win()
    token = w.HANDLE()
    if not a.OpenProcessToken(process, 0x8, ctypes.byref(token)):          # TOKEN_QUERY
        return ''
    try:
        buf, need = ctypes.create_string_buffer(256), w.DWORD()
        if not a.GetTokenInformation(token, 1, buf, 256, ctypes.byref(need)):   # TokenUser
            return ''
        return _sid_at(ctypes.c_void_p.from_buffer(buf).value)
    finally:
        k.CloseHandle(token)


def my_sid():
    try:
        return token_sid(win()[2].GetCurrentProcess())
    except Exception:
        return ''


def process_facts(pid):
    """{'image', 'family', 'owner'} for a process, each '' when Windows does not say."""
    ctypes, w, k, _ = win()
    facts = {'image': '', 'family': '', 'owner': ''}
    handle = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        facts['error'] = 'cannot be opened (error %d)' % ctypes.get_last_error()
        return facts
    try:
        buf, size = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
        if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            facts['image'] = buf.value
        if hasattr(k, 'GetPackageFamilyName'):
            name, length = ctypes.create_unicode_buffer(256), ctypes.c_uint32(256)
            rc = k.GetPackageFamilyName(handle, ctypes.byref(length), name)
            facts['family'] = name.value if rc == 0 else ('none' if rc == 15700 else 'unknown (error %d)' % rc)
        facts['owner'] = token_sid(handle)
    finally:
        k.CloseHandle(handle)
    return facts


def processes_named(names):
    """(pid, name) of running processes whose image name is one of `names`, from a Toolhelp snapshot."""
    ctypes, w, k, _ = win()

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', w.DWORD), ('cntThreads', w.DWORD),
                    ('th32ParentProcessID', w.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', w.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]
    first, following = k.Process32FirstW, k.Process32NextW
    first.argtypes = following.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    first.restype = following.restype = w.BOOL
    snap = k.CreateToolhelp32Snapshot(0x2, 0)                 # TH32CS_SNAPPROCESS
    if not snap or snap == w.HANDLE(-1).value:
        raise OSError('no process snapshot')
    wanted, found = {n.lower() for n in names}, []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = first(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                found.append((entry.th32ProcessID, entry.szExeFile))
            ok = following(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return found


def signature(path):
    """(status, signer) of a file's own Authenticode signature, through WinVerifyTrust with no UI, no revocation check
    and cached URLs only (so no network). The file is read, never run."""
    import ctypes
    from ctypes import wintypes as w
    t = ctypes.WinDLL('wintrust')
    c = ctypes.WinDLL('crypt32')

    class FileInfo(ctypes.Structure):
        _fields_ = [('cbStruct', w.DWORD), ('pcwszFilePath', w.LPCWSTR), ('hFile', w.HANDLE), ('pgKnownSubject', ctypes.c_void_p)]

    class Data(ctypes.Structure):
        _fields_ = [('cbStruct', w.DWORD), ('pPolicyCallbackData', ctypes.c_void_p), ('pSIPClientData', ctypes.c_void_p),
                    ('dwUIChoice', w.DWORD), ('fdwRevocationChecks', w.DWORD), ('dwUnionChoice', w.DWORD),
                    ('pFile', ctypes.POINTER(FileInfo)), ('dwStateAction', w.DWORD), ('hWVTStateData', w.HANDLE),
                    ('pwszURLReference', w.LPCWSTR), ('dwProvFlags', w.DWORD), ('dwUIContext', w.DWORD),
                    ('pSignatureSettings', ctypes.c_void_p)]

    class Guid(ctypes.Structure):
        _fields_ = [('a', w.DWORD), ('b', w.WORD), ('c', w.WORD), ('d', ctypes.c_ubyte * 8)]

    class Signer(ctypes.Structure):
        _fields_ = [('cbStruct', w.DWORD), ('sftVerifyAsOf', w.FILETIME), ('csCertChain', w.DWORD), ('pasCertChain', ctypes.c_void_p)]

    class Cert(ctypes.Structure):
        _fields_ = [('cbStruct', w.DWORD), ('pCert', ctypes.c_void_p)]
    import uuid
    action = Guid.from_buffer_copy(uuid.UUID('00aac56b-cd44-11d0-8cc2-00c04fc295ee').bytes_le)    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    info = FileInfo(ctypes.sizeof(FileInfo), path, None, None)
    data = Data(cbStruct=ctypes.sizeof(Data), dwUIChoice=2, fdwRevocationChecks=0, dwUnionChoice=1,
                pFile=ctypes.pointer(info), dwStateAction=1, dwProvFlags=0x10 | 0x1000)
    t.WinVerifyTrust.argtypes, t.WinVerifyTrust.restype = [w.HWND, ctypes.POINTER(Guid), ctypes.POINTER(Data)], ctypes.c_long
    t.WTHelperProvDataFromStateData.argtypes, t.WTHelperProvDataFromStateData.restype = [w.HANDLE], ctypes.c_void_p
    t.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD]
    t.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(Signer)
    c.CertGetNameStringW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p, w.LPWSTR, w.DWORD]
    c.CertGetNameStringW.restype = w.DWORD
    rc = t.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    status = {0: 'valid', -2146762496: 'not signed (no signature of its own)',
              -2146869232: 'signed, but the signature does not match the file',
              -2146762487: 'signed, by a certificate that is not trusted here'}.get(rc, 'not valid (0x%08x)' % (rc & 0xFFFFFFFF))
    signer = ''
    try:
        if rc == 0 or rc == -2146762487:
            prov = t.WTHelperProvDataFromStateData(data.hWVTStateData)
            sgnr = t.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
            if sgnr and sgnr.contents.csCertChain and sgnr.contents.pasCertChain:
                cert = Cert.from_address(sgnr.contents.pasCertChain).pCert
                buf = ctypes.create_unicode_buffer(256)
                if cert and c.CertGetNameStringW(cert, 4, 0, None, buf, 256) > 1:          # CERT_NAME_SIMPLE_DISPLAY_TYPE
                    signer = buf.value
    finally:
        data.dwStateAction = 2                                                              # WTD_STATEACTION_CLOSE
        t.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    return status, signer


def parse_manifest(raw):
    """From an AppxManifest.xml (bytes): each application's id, executable and entry point, and the protocols
    (codex://) and execution aliases it declares. Elements are matched by local name, whatever their namespace."""
    import xml.etree.ElementTree as ET
    if len(raw) > 4000000:
        raise ValueError('manifest too large')
    root = ET.fromstring(raw)
    local = lambda el: el.tag.rsplit('}', 1)[-1]
    apps = []
    for app in root.iter():
        if local(app) != 'Application':
            continue
        row = {'id': app.get('Id') or '', 'exe': app.get('Executable') or '', 'entry': app.get('EntryPoint') or '',
               'protocols': [], 'aliases': []}
        for el in app.iter():
            if local(el) == 'Protocol' and el.get('Name'):
                row['protocols'].append(el.get('Name'))
            elif local(el) == 'ExecutionAlias' and el.get('Alias'):
                row['aliases'].append(el.get('Alias'))
        apps.append(row)
    return apps


def manifest_lines(apps):
    if not apps:
        return ['    applications: none declared']
    lines = []
    for a in apps[:12]:
        lines.append('    application %s: executable %s%s' % (a['id'] or '?', a['exe'] or '(none)',
                                                            '; entry point %s' % a['entry'] if a['entry'] else ''))
        lines.append('      protocols: %s' % (', '.join(p + '://' for p in a['protocols']) or 'none'))
        lines.append('      execution aliases: %s' % (', '.join(a['aliases']) or 'none'))
    return lines


def check_openai_packages():
    try:
        rows = [r for r in appx_rows() if r['name'].lower().startswith('openai.')]
    except Exception as e:
        return ['OpenAI packages: %s' % (str(e) if str(e).startswith('error') else 'error: %s' % e)]
    if not rows:
        return ['OpenAI packages: not found']
    lines = []
    for r in rows:
        lines.append('Package %s %s' % (r['name'], r['version']))
        lines.append('  family: %s; publisher id: %s' % (r['family'] or '?', r['publisher'] or '?'))
        manifest = os.path.join(r['location'], 'AppxManifest.xml') if r['location'] else ''
        if not manifest:
            lines.append('  manifest: no install location')
            continue
        try:
            with open(manifest, 'rb') as f:
                raw = f.read(4000001)
            lines.extend(manifest_lines(parse_manifest(raw)))
        except FileNotFoundError:
            lines.append('  manifest: not found')
        except PermissionError:
            lines.append('  manifest: error: access denied')
        except Exception as e:
            lines.append('  manifest: error: %s' % type(e).__name__)
    return lines


APP_PROCESSES = ('ChatGPT.exe', 'Codex.exe', 'codex.exe')


def process_lines(rows, mine):
    """Rows of (name, facts) grouped by name, image and package, with counts. Owners are 'you' or a label."""
    groups = {}
    for name, f in rows:
        key = (name, f.get('image') or '', f.get('family') or '', f.get('owner') or '', f.get('error') or '')
        groups[key] = groups.get(key, 0) + 1
    lines = []
    for (name, image, family, owner, error), n in sorted(groups.items()):
        if error:
            lines.append('%s x%d: %s' % (name, n, error))
            continue
        lines.append('%s x%d: %s' % (name, n, image or '(image path not readable)'))
        lines.append('  package: %s; runs as: %s' % (family or 'unknown', sid_label(owner, mine) if owner else 'unknown'))
    return lines


def check_app_processes():
    found = processes_named(APP_PROCESSES)
    if not found:
        return ['ChatGPT.exe, Codex.exe, codex.exe: not running']
    mine = my_sid()
    return process_lines([(name, process_facts(pid)) for pid, name in found[:80]], mine)


def check_codex_pipe(names=None):
    """Whether codex-ipc is in the pipe listing. The pipe is never opened: opening a pipe is a client connection
    the Codex app would accept and answer, so the probe only reads the listing's names."""
    if names is None:
        names, why = list_dir('\\\\.\\pipe\\\\')
        if names is None:
            return ['codex-ipc: %s' % why]
    found = 'codex-ipc' in [n.lower() for n in names]
    return ['codex-ipc (the Codex app\'s control pipe): %s' % ('found' if found else 'not found'),
            '  (only the name was looked for in the pipe list; the probe never opens or connects to a pipe)']


def check_codex_cli_file():
    lines = []
    try:
        rows = [r for r in appx_rows() if r['name'].lower().startswith('openai.') and r['location']]
    except Exception as e:
        rows = []
        lines.append('OpenAI packages: %s' % (str(e) if str(e).startswith('error') else 'error: %s' % e))
    mine, any_cli = my_sid(), False
    for r in rows:
        path = os.path.join(r['location'], 'app', 'resources', 'codex.exe')
        if not os.path.isfile(path):
            continue
        any_cli = True
        lines.append('%s: found (not run)' % path)
        try:
            owner, aces = path_security(path)
            lines.extend(acl_lines(owner, aces, mine))
        except Exception as e:
            lines.append('  permissions: error: %s' % (str(e) or type(e).__name__))
        try:
            status, signer = signature(path)
            lines.append('  signature: %s; signer: %s' % (status, signer or 'unknown'))
            lines.append('  (a file inside an app package usually has no signature of its own, since the package is '
                         'signed as a whole, so "not signed (no signature of its own)" is expected here)')
        except Exception as e:
            lines.append('  signature: unknown (%s)' % type(e).__name__)
    if not any_cli:
        lines.append('app\\resources\\codex.exe in an OpenAI package: not found')
    control = os.path.join(os.path.expanduser('~'), '.codex', 'app-server-control')
    lines.append('%%USERPROFILE%%\\.codex\\app-server-control: %s' % ('found' if os.path.lexists(control) else 'not found'))
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
    ('11. OpenAI app packages', check_openai_packages),
    ('12. ChatGPT and Codex processes', check_app_processes),
    ('13. Codex bundled command-line tool', check_codex_cli_file),
]

NEW_SECTIONS_NOTE = ('New in this version: section 4 also says whether the Codex app\'s control pipe (codex-ipc) is '
                     'listed (the probe never opens or connects to it). Section 11 lists the OpenAI app packages and what their manifests declare, section 12 '
                     'which file and package each ChatGPT or Codex process runs from, and section 13 who may open '
                     'Codex\'s bundled codex.exe and who signed it (it is not run). "you" means your own account.')
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
    out = ['Motet Windows probe (read-only; nothing was changed, written or sent)', NEW_SECTIONS_NOTE]
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
