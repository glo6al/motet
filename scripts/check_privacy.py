#!/usr/bin/env python3
"""Publication privacy check: prevent committing personal paths, usernames, hostnames,
machine metadata, private project locations, or internal working records.

Enforces the AGENTS.md policy:
"Before committing or pushing, inspect the complete publication set, including agent instruction
files, for personal filesystem paths, private project names, credentials, and local investigation
evidence. Secret-pattern scanning alone is not a privacy review. Use repository-relative paths
and fictional examples in public documentation."

A finding is reported by its type and location only, so the report never republishes what it found
(the check runs in public CI logs). A location whose file name is itself a finding is withheld.
--show prints the matched text as well, on a local terminal only; it is ignored under CI.
To check private names locally, put one UTF-8 word per line in motet-private-names in the
directory printed by git rev-parse --git-common-dir. Use a regular file. Blank lines are ignored.
"""

import argparse
import collections
import getpass
import os
import re
import socket
import stat
import subprocess
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



def _combining_marks():
    """A regex class body of every Unicode combining mark, which continues the letter before it."""
    ranges, start = [], None
    for cp in range(sys.maxunicode + 2):
        mark = cp <= sys.maxunicode and unicodedata.category(chr(cp)).startswith('M')
        if mark and start is None:
            start = cp
        elif not mark and start is not None:
            ranges.append(chr(start) if start == cp - 1 else chr(start) + '-' + chr(cp - 1))
            start = None
    return ''.join(ranges)


# The characters of an account name, and its end: any letter, digit or combining mark in any script, underscore or
# hyphen continues the name, so 'demo-x' and a 'demo' followed by an accent, composed or not, are other accounts.
# Text is also compared in its composed (NFC) form, so both spellings of one name read the same.
ACCOUNT_CHARACTERS = r'\w\-' + _combining_marks()
ACCOUNT = '[' + ACCOUNT_CHARACTERS + ']+'
END = '(?![' + ACCOUNT_CHARACTERS + '])'

# Patterns known to be fictional demo data, test redaction assertions, or code identifiers.
# Only the matched text is exempt; the rest of its line is still scanned.
SAFE_EXCEPTIONS = [
    # Regexes in server.py
    re.compile(r'PATH_RX\s*='),
    re.compile(r'CD_RX\s*='),
    # Fictional users in demo/tests
    re.compile(r'[/\\]Users[/\\](demo|me)' + END),
    re.compile(r'[A-Za-z]:[/\\]Users[/\\](demo|me)' + END),
    re.compile(r'/home/(demo|me|person)' + END),
    # Python code using threading.local
    re.compile(r'threading\.local\b'),
    # Settings filenames
    re.compile(r'settings\.local\.json\b'),
]

# The fictional leaks this check's own test uses as fixtures, exempt only in the check and that exact test file.
FIXTURES = [
    re.compile(r'/Users/johnsmith' + END),
    re.compile(r'\b192\.168\.1\.100\b'),
    re.compile(r'\bmy-macbook-pro\.local\b'),
    re.compile(r'\.env\.local\b'),
]
FIXTURE_EXCEPTIONS = {'scripts/check_privacy.py': FIXTURES, 'tests/test_check_privacy.py': FIXTURES}

# Sensitive file path patterns that should never be tracked or staged
PROHIBITED_FILE_PATTERNS = [
    (re.compile(r'(^|/)\.env(\.[^/]+)?$'), 'environment variables file'),
    (re.compile(r'(^|/)\.claude(/|$)'), 'local Claude agent directory or settings'),
    (re.compile(r'(^|/)\.gemini(/|$)'), 'local Gemini agent directory or settings'),
    (re.compile(r'(^|/)archive/'), 'internal working history (archive/)'),
    (re.compile(r'(^|/)docs/investigations/'), 'local investigation evidence'),
    (re.compile(r'(^|/)docs/(plans|attachments)(/|$)', re.IGNORECASE), 'private planning and proof documents'),
    (re.compile(r'(^|/)docs/bridge(?:$|/(?!(?-i:RECOVERY\.md)$))', re.IGNORECASE), 'private bridge documents'),
    (re.compile(r'(^|/)brand/(reference|studies)/'), 'brand reference / process notes'),
    (re.compile(r'(^|/)brand/HANDOFF\.md$'), 'brand handoff note'),
    (re.compile(r'\.(pem|key|pfx|p12|pkcs12)$', re.IGNORECASE), 'private key or certificate file'),
    (re.compile(r'(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$'), 'SSH key file'),
    (re.compile(r'\.DS_Store$'), 'macOS desktop metadata (.DS_Store)'),
]

# One finding. path is repository-relative; line 0 means the whole file; commit is set for history scans.
Hit = collections.namedtuple('Hit', 'path line kind match snippet commit', defaults=(None,))

# Keep Git's output stable whatever the local configuration: fixed prefixes, no colour, renames as add and delete.
DIFF_OPTIONS = ['--no-color', '--no-ext-diff', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/']


PRIVATE_NAMES_FILE = 'motet-private-names'
NAME_WORD = re.compile(r'\b\w+\b')
# Match the dotted and dotless I variants accepted by Python's case-insensitive regexes.
I_VARIANTS = str.maketrans({'\u0130': 'i', '\u0131': 'i'})


class InternalAgentNames:
    def __init__(self, names):
        self.names = frozenset(unicodedata.normalize('NFC', name).translate(I_VARIANTS).casefold()
                               for name in names)

    def search(self, text):
        for word in NAME_WORD.finditer(text):
            normalized = word.group().translate(I_VARIANTS).casefold()
            if normalized in self.names:
                return word
        return None


def build_privacy_patterns(private_names=()):
    patterns = [
        ('internal note reference', re.compile(r'\bCHANGE-[0-9]+\b', re.IGNORECASE)),
    ]
    if private_names:
        patterns.append(('internal agent name', InternalAgentNames(private_names)))
    user = unicodedata.normalize('NFC', getpass.getuser())
    home = unicodedata.normalize('NFC', os.path.expanduser('~'))
    host = unicodedata.normalize('NFC', socket.gethostname())

    # User's personal username (if sufficiently distinctive)
    # 'runner' and 'runneradmin' are GitHub's CI accounts, not a person, and 'runner' is an ordinary word in this project.
    if len(user) >= 3 and user.lower() not in ('root', 'daemon', 'nobody', 'user', 'test', 'admin', 'runner', 'runneradmin'):
        patterns.append(('personal username', re.compile(r'\b' + re.escape(user) + r'\b', re.IGNORECASE)))

    # User's home directory
    if home and home != '/':
        patterns.append(('personal home path', re.compile(re.escape(home), re.IGNORECASE)))

    # Machine hostname and .local hostnames
    if host:
        host_stem = host.split('.')[0]
        if len(host_stem) >= 3 and host_stem.lower() not in ('localhost', 'macbook', 'linux', 'windows'):
            patterns.append(('machine hostname', re.compile(re.escape(host_stem), re.IGNORECASE)))
    patterns.append(('local network domain (.local)', re.compile(r'\b[A-Za-z0-9_-]+\.local\b', re.IGNORECASE)))

    # Real macOS / Linux / Windows user paths (excluding demo and me)
    patterns.append(('personal macOS user path', re.compile(r'/Users/(?!(?:demo|me)' + END + r')' + ACCOUNT, re.IGNORECASE)))
    patterns.append(('personal Linux home path', re.compile(r'/home/(?!(?:demo|me)' + END + r')' + ACCOUNT, re.IGNORECASE)))
    patterns.append(('personal Windows user path', re.compile(r'[A-Za-z]:[\\/]Users[\\/](?!(?:demo|me)' + END + r')' + ACCOUNT, re.IGNORECASE)))

    # Personal RFC 1918 IP addresses (except standard loopback/localhost)
    patterns.append(('private network IP address', re.compile(r'\b(192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[01])\.\d+\.\d+)\b')))

    return patterns


def mask_safe(line, path):
    """The line with every known-fictional or code-identifier span blanked, so only the rest is scanned."""
    for exception in SAFE_EXCEPTIONS + FIXTURE_EXCEPTIONS.get(path, []):
        line = exception.sub(lambda m: ' ' * len(m.group()), line)
    return line


def check_file_path(path):
    rel = os.path.relpath(path, ROOT).replace('\\', '/') if os.path.isabs(path) else path.replace('\\', '/')
    for pattern, description in PROHIBITED_FILE_PATTERNS:
        if pattern.search(rel):
            return description
    return None


def path_findings(path, patterns, commit=None):
    """Findings in a file's name: a prohibited location, or personal information in the name itself."""
    reason = check_file_path(path)
    if reason:
        return [Hit(path, 0, 'prohibited file path: ' + reason, path, '', commit)]
    masked = mask_safe(unicodedata.normalize('NFC', path), None)
    return [Hit(path, 0, label + ' in a file name', rx.search(masked).group(), '', commit) for label, rx in patterns if rx.search(masked)]


def scan_text(text, path, patterns, commit=None, first_line=1):
    hits = []
    for line_no, line in enumerate(text.split('\n'), first_line):   # only a newline ends a line, as in Git
        masked = mask_safe(unicodedata.normalize('NFC', line), path)
        for label, rx in patterns:
            found = rx.search(masked)
            if found:
                hits.append(Hit(path, line_no, label, found.group(), line.strip()[:140], commit))
    return hits


def c_unquote(name):
    """A path as Git prints it in a patch header, with C-style quoting undone; None if it cannot be read."""
    if not name.startswith('"'):
        return name
    if len(name) < 2 or not name.endswith('"'):
        return None
    out, i, body = bytearray(), 0, name[1:-1]
    escapes = {'a': 7, 'b': 8, 't': 9, 'n': 10, 'v': 11, 'f': 12, 'r': 13, '"': 34, '\\': 92}
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out += ch.encode('utf-8', 'surrogateescape')
            i += 1
        elif body[i + 1:i + 4].isdigit() and len(body[i + 1:i + 4]) == 3:
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        elif body[i + 1:i + 2] in escapes:
            out.append(escapes[body[i + 1]])
            i += 2
        else:
            return None
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError:
        return None


HUNK_RX = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def scan_diff_lines(diff_text, patterns, commit=None):
    """Scan the added lines of a patch. Each is located by the new file's name and line number."""
    return scan_patch(diff_text, patterns, commit)[0]


def scan_patch(diff_text, patterns, commit=None):
    """The findings in a patch's added lines, and the names of the files it showed a hunk for."""
    hits, hunked = [], set()
    current_file, line_no, in_header = None, 0, False
    for line in diff_text.split('\n'):
        if line.startswith('diff --git '):
            current_file, line_no, in_header = None, 0, True
        elif in_header and line.startswith('+++ '):
            name = line[4:].rstrip('\t')
            if name == '/dev/null':
                current_file = None
            else:
                name = c_unquote(name)
                if name is None or not name.startswith('b/'):
                    hits.append(Hit('', 0, 'unreadable file name in diff', '', '', commit))
                    current_file = ''
                else:
                    current_file = name[2:]
        elif line.startswith('@@ '):
            in_header = False
            if current_file:
                hunked.add(current_file)
            found = HUNK_RX.match(line)
            if found:
                line_no = int(found.group(1))
            else:
                hits.append(Hit(current_file or '', 0, 'unreadable diff hunk', '', '', commit))
        elif in_header:
            continue
        elif line.startswith('+'):
            if current_file is None:
                hits.append(Hit('', 0, 'added line outside a file in diff', '', '', commit))
            else:
                hits.extend(scan_text(line[1:], current_file, patterns, commit, line_no))
            line_no += 1
        elif line.startswith(' '):
            line_no += 1
    return hits, hunked


def scan_file_contents(file_path, patterns):
    """Scan one file on disk (kept for direct use; --all reads Git's own copies)."""
    rel_path = os.path.relpath(file_path, ROOT).replace('\\', '/')
    hits = path_findings(rel_path, patterns)
    if any(h.kind.startswith('prohibited file path') for h in hits):
        return hits
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            hits.extend(scan_text(f.read(), rel_path, patterns))
    except (OSError, UnicodeError) as e:
        hits.append(Hit(rel_path, 0, 'unreadable file', type(e).__name__, ''))
    return hits


def git(*args, data=None):
    """Raw bytes of a Git command run in this repository; raises on failure."""
    return subprocess.run(['git', '-C', ROOT, '--no-replace-objects', '-c', 'core.quotePath=true', *args],
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


def load_private_names():
    """Read the optional local word list from Git metadata shared by linked worktrees."""
    directory = os.fsdecode(git('rev-parse', '--git-common-dir')).rstrip('\r\n')
    path = os.path.join(ROOT, directory, PRIVATE_NAMES_FILE)
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError

    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    names = []
    with os.fdopen(os.open(path, flags), encoding='utf-8') as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(entry, opened):
            raise ValueError
        for line in source:
            name = unicodedata.normalize('NFC', line.strip())
            if name:
                if not NAME_WORD.fullmatch(name):
                    raise ValueError
                names.append(name)
    return names


def read_blobs(oids):
    """The content of each object id, in order, from one git cat-file --batch; None where Git has no blob."""
    out, pos, blobs = git('cat-file', '--batch', data=b''.join(oid + b'\n' for oid in oids)), 0, []
    for oid in oids:
        end = out.index(b'\n', pos)
        header = out[pos:end].split()
        if len(header) != 3 or header[0] != oid or header[1] != b'blob':
            return blobs + [None] * (len(oids) - len(blobs))   # the stream is no longer aligned with the list
        size = int(header[2])
        blobs.append(out[end + 1:end + 1 + size])
        pos = end + 1 + size + 1
    return blobs


def nul_paths(output):
    """Paths from Git's NUL-delimited output (-z), decoded; a name that is not UTF-8 stays visible as a finding."""
    paths = []
    for raw in output.split(b'\0'):
        if raw:
            try:
                paths.append(raw.decode('utf-8'))
            except UnicodeDecodeError:
                paths.append(None)
    return paths


def scan_diff(diff_args, patterns, commit=None):
    """Names and added content of one diff, e.g. ['--cached'] or [parent, commit]. Git shows every file as
    text, even one with NUL bytes or marked binary; a changed file it still shows no hunk for is scanned whole,
    from its blob or, for the working tree, from disk."""
    hits, changed = [], []
    fields = git('diff', *DIFF_OPTIONS, '--raw', '-z', '--no-abbrev', *diff_args, '--').split(b'\0')
    for meta, raw_path in zip(fields[0::2], fields[1::2]):
        parts = meta[1:].split()
        if not meta.startswith(b':') or len(parts) != 5:
            hits.append(Hit('', 0, 'unreadable diff listing', '', '', commit))
            break
        _, new_mode, old_oid, new_oid, status = parts
        try:
            path = raw_path.decode('utf-8')
        except UnicodeDecodeError:
            hits.append(Hit('', 0, 'file name is not UTF-8', '', '', commit))
            continue
        if status == b'D':
            continue
        hits.extend(path_findings(path, patterns, commit))
        if new_mode != b'160000' and old_oid != new_oid:   # not a submodule, nor a mode-only change
            changed.append((path, new_oid))
    patch = git('diff', *DIFF_OPTIONS, '--text', '--no-textconv', '-U0', *diff_args, '--').decode('utf-8', errors='replace')
    patch_hits, hunked = scan_patch(patch, patterns, commit)
    hits.extend(patch_hits)
    unshown = [(path, oid) for path, oid in changed if path not in hunked]
    stored = [(path, oid) for path, oid in unshown if oid.strip(b'0')]
    for (path, _), content in zip(stored, read_blobs([oid for _, oid in stored]) if stored else []):
        if content is None:
            hits.append(Hit(path, 0, 'unreadable changed file', '', '', commit))
        else:
            hits.extend(scan_text(content.decode('utf-8', errors='replace'), path, patterns, commit))
    for path, oid in unshown:
        if not oid.strip(b'0'):   # a working-tree file Git has not stored
            full = os.path.join(ROOT, path)
            try:
                content = os.readlink(full).encode('utf-8', 'surrogateescape') if os.path.islink(full) else open(full, 'rb').read()
                hits.extend(scan_text(content.decode('utf-8', errors='replace'), path, patterns, commit))
            except OSError as e:
                hits.append(Hit(path, 0, 'unreadable changed file', type(e).__name__, '', commit))
    return hits


def check_author_emails(commits):
    hits = []
    for commit in commits:
        try:
            emails = git('log', '-n', '1', '--format=%ae%n%ce', commit).decode('utf-8', errors='replace').splitlines()
            for email in emails:
                if email.lower().endswith('.local'):
                    hits.append(Hit('', 0, 'local author/committer email', email, '', commit))
        except (subprocess.SubprocessError, OSError) as e:
            hits.append(Hit('', 0, 'git command failed', type(e).__name__, '', commit))
    return hits


def check_staged(patterns):
    try:
        return scan_diff(['--cached'], patterns)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        return [Hit('', 0, 'git command failed', type(e).__name__, '')]


def check_rev_range(rev_range, patterns):
    """Scan each commit in the range on its own, so content added and later removed inside the range is
    still found. A merge is compared with its first parent; a root commit with the empty tree. Commit
    messages and author/committer emails are scanned too."""
    hits = []
    try:
        commits = git('rev-list', rev_range).decode().split()
        hits.extend(check_author_emails(commits))
        empty_tree = git('hash-object', '-t', 'tree', '--stdin', data=b'').decode().strip()
        for commit in commits:
            parents = git('rev-list', '--parents', '-n', '1', commit).decode().split()[1:]
            hits.extend(scan_diff([parents[0] if parents else empty_tree, commit], patterns, commit))
            message = git('log', '-n', '1', '--format=%B', commit).decode('utf-8', errors='replace')
            hits.extend(scan_text(message, '(commit message)', patterns, commit))
    except (subprocess.SubprocessError, OSError, IndexError, ValueError) as e:
        hits.append(Hit('', 0, 'git command failed', type(e).__name__, ''))
    return hits


def check_all_tracked(patterns):
    """Scan every tracked file as Git holds it in the index, read by object id, so no name is skipped."""
    hits, blobs = [], []
    try:
        for entry in git('ls-files', '-s', '-z').split(b'\0'):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b'\t')
            mode, oid = meta.split()[:2]
            try:
                path = raw_path.decode('utf-8')
            except UnicodeDecodeError:
                hits.append(Hit('', 0, 'file name is not UTF-8', '', ''))
                continue
            name_hits = path_findings(path, patterns)
            hits.extend(name_hits)
            if mode == b'160000' or any(h.kind.startswith('prohibited file path') for h in name_hits):
                continue   # a submodule has no content here; a prohibited file is already a finding
            blobs.append((path, oid))
        for (path, _), content in zip(blobs, read_blobs([oid for _, oid in blobs]) if blobs else []):
            if content is None:
                hits.append(Hit(path, 0, 'unreadable tracked file', '', ''))
            else:
                hits.extend(scan_text(content.decode('utf-8', errors='replace'), path, patterns))
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        hits.append(Hit('', 0, 'git command failed', type(e).__name__, ''))
    return hits


def printable(text):
    """Text that stays on one log line: control, format and line-separator characters are escaped."""
    return ''.join(c if c.isprintable() else c.encode('unicode_escape').decode('ascii') for c in text)


def safe_location(hit, patterns):
    """Where a finding is, without repeating anything private: a name that is itself a finding is withheld."""
    if hit.path == '(commit message)':
        loc = 'commit message'
    elif not hit.path or check_file_path(hit.path) or path_findings(hit.path, patterns):
        loc = '(file name withheld)' if hit.path else ''
    else:
        loc = printable(hit.path)
    if loc and hit.line:
        loc += ':%d' % hit.line
    if hit.commit:
        loc = '%s %s' % (hit.commit[:12], loc) if loc else hit.commit[:12]
    return loc or '(repository)'


def report(hits, patterns, show):
    print('PRIVACY LEAK DETECTED: Commit/publication refused.', file=sys.stderr)
    print('Found %d sensitive item(s):\n' % len(hits), file=sys.stderr)
    for hit in hits:
        print('  [%s] %s' % (hit.kind, safe_location(hit, patterns)), file=sys.stderr)
        if show:
            for text in (hit.match, hit.snippet):
                if text and text != hit.path:
                    print('      %s' % printable(text), file=sys.stderr)
    if not show:
        print('\nMatched text is not printed. Run with --show on your own machine to see it.', file=sys.stderr)
    print('\nPlease replace personal paths/names with repository-relative paths or fictional names before committing.', file=sys.stderr)


COMPARE_REF = 'refs/remotes/origin/main'


def comparison_base(ref=COMPARE_REF):
    """The commit the comparison branch names, or None when the branch conclusively does not exist. Raises
    LookupError when it exists but cannot be read: malformed or empty, a symbolic ref to nothing, a missing object."""
    def run(*args):
        return subprocess.run(['git', '-C', ROOT, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    exists = run('show-ref', '--exists', ref).returncode   # 0 present, 1 unreadable, 2 absent (Git 2.43 and later)
    if exists == 2:
        return None
    resolved = run('rev-parse', '--verify', '--quiet', ref + '^{commit}')
    if resolved.returncode == 0:
        return resolved.stdout.decode().strip()
    if exists in (0, 1) or stored_ref(ref):
        raise LookupError(ref)
    return None   # Git without --exists, and nothing stored under that name


def stored_ref(ref):
    """Whether a loose or packed ref of this name is stored, readable or not (for Git without show-ref --exists)."""
    def path(name):
        out = subprocess.run(['git', '-C', ROOT, 'rev-parse', '--git-path', name], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=True).stdout.decode().strip()
        return out if os.path.isabs(out) else os.path.join(ROOT, out)
    if os.path.lexists(path(ref)):
        return True
    try:
        with open(path('packed-refs'), 'rb') as packed:
            return any(line.split()[1:2] == [ref.encode()] for line in packed if not line.startswith((b'#', b'^')))
    except FileNotFoundError:
        return False


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--staged', action='store_true', help='Scan git staged changes (index)')
    parser.add_argument('--range', dest='rev_range', help='Scan each commit in a revision range (e.g. origin/main..HEAD)')
    parser.add_argument('--all', action='store_true', help='Scan all tracked files in repository')
    parser.add_argument('--show', action='store_true', help='Also print the matched text (local terminal only; ignored under CI)')
    args = parser.parse_args()

    show = args.show
    if show and (os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS')):
        print('--show is ignored under CI, so findings are never republished in a build log.', file=sys.stderr)
        show = False

    try:
        private_names = load_private_names()
    except subprocess.SubprocessError:
        print('PRIVACY CHECK FAILED: could not locate Git metadata.', file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError):
        print('PRIVACY CHECK FAILED: motet-private-names must be readable UTF-8 with one word per line.',
              file=sys.stderr)
        sys.exit(1)

    patterns = build_privacy_patterns(private_names)
    hits = []

    if args.rev_range:
        hits = check_rev_range(args.rev_range, patterns)
    elif args.all:
        hits = check_all_tracked(patterns)
    elif args.staged:
        hits = check_staged(patterns)
    else:
        # Default behavior:
        # 1. If staged changes exist, check staged changes.
        # 2. Check unpushed commits ahead of origin/main if origin/main exists.
        # 3. Otherwise check working diff.
        try:
            staged_output = git('diff', '--cached', '--name-only', '-z').strip(b'\0')
        except (subprocess.SubprocessError, OSError) as e:
            staged_output, hits = b'', [Hit('', 0, 'git command failed', type(e).__name__, '')]

        if staged_output:
            hits.extend(check_staged(patterns))
        elif not hits:
            # Check commits ahead of origin/main. Only a conclusively missing origin/main skips this.
            try:
                base = comparison_base()
            except (LookupError, OSError):
                hits.append(Hit('', 0, 'origin/main could not be read', '', ''))
            else:
                if base:
                    hits.extend(check_rev_range(base + '..HEAD', patterns))

            # Also check unstaged working changes
            try:
                hits.extend(scan_diff([], patterns))
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                hits.append(Hit('', 0, 'git command failed', type(e).__name__, ''))

    if hits:
        report(hits, patterns, show)
        sys.exit(1)
    else:
        print('clean: no personal data, internal references, or prohibited files detected.')
        sys.exit(0)


if __name__ == '__main__':
    main()
