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
import shutil
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


def _name_regex(name):
    if len(name) >= 4:
        return re.compile(re.escape(name), re.IGNORECASE)
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])', re.IGNORECASE)


EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
UUID = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
PREFIXED_KEY = re.compile(
    r'\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|AKIA|AIza)[-_A-Za-z0-9]{8,}')
JWT = re.compile(r'\beyJ[\w-]{5,}\.[\w-]{5,}(?:\.[\w-]*)?')
BEARER = re.compile(r'(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}')
ASSIGNED = re.compile(r'(?i)\b(token|secret|password|passwd|api[_-]?key|apikey|key)(\s*[=:]\s*)\S+')
LONG_HEX = re.compile(r'\b[0-9a-fA-F]{16,}\b')
LONG_RUN = re.compile(r'[A-Za-z0-9+/_=-]{32,}')


def make_context(env=None):
    """The things to hide, gathered from the environment (or from a given dict)."""
    env = os.environ if env is None else env
    profile = env.get('USERPROFILE') or os.path.expanduser('~')
    user = env.get('USERNAME') or env.get('USER') or ''
    if not user:
        try:
            import getpass
            user = getpass.getuser()
        except Exception:
            user = ''
    pc = env.get('COMPUTERNAME') or platform.node() or ''
    return {
        'profile': profile,
        'localappdata': env.get('LOCALAPPDATA') or '',
        'appdata': env.get('APPDATA') or '',
        'user': user,
        'pc': pc,
    }


def redact(text, ctx=None):
    """Return text with personal paths, names, e-mails and key-like strings replaced."""
    if text is None:
        return ''
    ctx = ctx or make_context()
    text = str(text)
    text = EMAIL.sub('<email>', text)
    paths = [(ctx.get('localappdata'), '%LOCALAPPDATA%'),
             (ctx.get('appdata'), '%APPDATA%'),
             (ctx.get('profile'), '%USERPROFILE%')]
    paths = [(p, n) for p, n in paths if p and len(p) > 3]
    paths.sort(key=lambda pn: -len(pn[0]))          # longest first: AppData lives inside the profile
    for path, name in paths:
        rx = _path_regex(path)
        if rx:
            text = rx.sub(name, text)
    user = ctx.get('user') or ''
    aliases = [user]
    profile = ctx.get('profile') or ''
    base = [p for p in re.split(r'[\\/]+', profile) if p][-1:] if profile else []
    aliases += base
    if len(user) > 6:
        aliases.append(user[:6] + '~1')               # the 8.3 short form of a long name
    names = {a: '<user>' for a in aliases if a and len(a) >= 2}
    pc = ctx.get('pc') or ''
    if len(pc) >= 2:
        names[pc] = '<pc>'
    for alias in sorted(names, key=len, reverse=True):      # longest first: a PC name may contain the user name
        text = _name_regex(alias).sub(names[alias], text)
    text = UUID.sub('<id>', text)
    text = JWT.sub('<key>', text)
    text = PREFIXED_KEY.sub('<key>', text)
    text = BEARER.sub(lambda m: m.group(1) + ' <key>', text)
    text = ASSIGNED.sub(lambda m: m.group(1) + m.group(2) + '<key>', text)
    text = LONG_HEX.sub('<id>', text)
    text = LONG_RUN.sub('<key>', text)
    return text


# ---------------------------------------------------------------- helpers

def remaining():
    return BUDGET_SECONDS - (time.time() - _START)


def run(cmd, timeout=10):
    """Run a command without a shell. Returns (exit code, combined output) or (None, 'error: ...')."""
    left = remaining()
    if left < 1:
        return None, 'error: skipped, time budget used up'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=min(timeout, left), creationflags=flags)
    except subprocess.TimeoutExpired:
        return None, 'error: timed out'
    except Exception as e:
        return None, 'error: %s' % type(e).__name__
    return p.returncode, p.stdout.decode('utf-8', 'replace')


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
    rc, out = run(['powershell', '-NoProfile', '-Command', script], timeout=25)
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
    lines = []
    rc, out = run([path, '--version'])
    lines.append('%s --version: %s' % (label, first_line(out) if rc == 0 else
                                       (out if rc is None else 'error: exit %s' % rc)))
    for args in (['queue', '--help'], ['app-server', '--help']):
        rc, out = run([path] + args)
        if rc is None:
            lines.append('%s %s: %s' % (label, ' '.join(args), out))
        else:
            lines.append('%s %s: %s (exit %s); first line: %s' % (
                label, ' '.join(args), 'ok' if rc == 0 else 'failed', rc, first_line(out) or '(none)'))
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

    on_path = shutil.which('codex')
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

    cli = shutil.which('claude')
    if cli:
        rc, out = run([cli, '--version'])
        lines.append('claude on PATH: found: %s' % cli)
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
                lines.append('  hooks key: found; events: %s' % (', '.join(sorted(events)) or '(none)'))
            elif hooks is not None:
                lines.append('  hooks key: found (not an object)')
            else:
                lines.append('  hooks key: not found')
        except Exception as e:
            lines.append('  hooks key: error: could not read (%s)' % type(e).__name__)

    where = shutil.which('bash')
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
        lines.append('%s on PATH: %s' % (cli, 'found: %s' % shutil.which(cli) if shutil.which(cli) else 'not found'))
    return lines


def check_editors():
    # Versions come from the registry; the editors' own launchers are not run, since they can open a window.
    return (check_editor('Cursor', 'Cursor', ['cursor'], ['cursor'])
            + check_editor('Antigravity', 'Antigravity', ['antigravity'], ['antigravity']))


def check_terminal():
    wt = shutil.which('wt')
    return ['wt.exe on PATH: %s' % ('found: %s' % wt if wt else 'not found')]


def check_moonshine():
    return ['Moonshine wheel lookup: skipped (needs network)']


def check_processes():
    lines = []
    rc, out = run(['tasklist', '/fo', 'csv', '/nh'])
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
    if shutil.which('wmic'):
        rc, out = run(['wmic', 'process', 'where', "name='codex.exe'", 'get', 'commandline'])
        if rc == 0:
            cmdlines, how = [l.strip() for l in out.splitlines()[1:] if l.strip()], 'wmic'
    if cmdlines is None:
        rc, out = run(['powershell', '-NoProfile', '-Command',
                       "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
                       "ForEach-Object { $_.CommandLine }"], timeout=15)
        if rc == 0:
            cmdlines, how = [l.strip() for l in out.splitlines() if l.strip()], 'PowerShell Get-CimInstance'
        else:
            lines.append('codex.exe command line: error: %s' % (out if rc is None else 'powershell exit %s' % rc))
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
