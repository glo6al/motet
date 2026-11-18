"""Check stock Python 3.9 compatibility, then run positive attachment proofs.

Older official 3.9 builds link an SQLite without the required JSON semantics.
The stock runtime must pass the actual refusal and ordinary-messaging tests
before CI provisions a pinned Python 3.9 with supported SQLite for the full suite.
"""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import tempfile
from urllib.parse import quote
from urllib.request import urlopen
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
RELEASE = '20251031'
# SHA256 values are the immutable release asset digests published by GitHub.
BUILDS = {
    ('Darwin', 'arm64'): (
        'aarch64-apple-darwin',
        '1dca0e37d56b7da3ec6a7d2f75cc72a2df3bd05751606300934b2dc6fd7026ea',
    ),
    ('Windows', 'AMD64'): (
        'x86_64-pc-windows-msvc',
        '6898722ab4242e304daf1490e2545bee7e7befacef82a9362f9496c0ee616ece',
    ),
}


def stock_sqlite_supported():
    sys.path.insert(0, str(ROOT / 'src'))
    from attachments_owner_marker import sqlite_marker_available

    with closing(sqlite3.connect(':memory:')) as db:
        supported = sqlite_marker_available(db)
    print(f'Stock test interpreter: Python {sys.version.split()[0]}, '
          f'SQLite {sqlite3.sqlite_version}; native attachment support: {supported}', flush=True)
    return supported


# These three cases require an installed attachment guard and run in their
# original complete file suites after the attachment runtime is selected.
ATTACHMENT_POSITIVE = {
    'test_owner_actions.OwnerActionsTests.test_direct_capacity_refusal_is_distinct_from_a_lost_commit_response',
    'test_claude_peer_backend.ClaudePeerBackendTests.test_held_body_cannot_be_reconstructed_as_pruned_installed',
    'test_claude_peer_backend.ClaudePeerBackendTests.test_installed_genuinely_pruned_completion_keeps_existing_receipt_path',
}


def stock_core_mixed_suites():
    """Also exercise the ordinary cases in files that contain attachment cases."""
    print(f'Stock core tests: Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}', flush=True)
    sys.path.insert(0, str(ROOT / 'tests'))
    suite = unittest.TestSuite()
    all_ids = set()
    for name in ('test_owner_actions.OwnerActionsTests',
                 'test_claude_peer_backend.ClaudePeerBackendTests'):
        for test in unittest.defaultTestLoader.loadTestsFromName(name):
            identity = test.id()
            all_ids.add(identity)
            if identity not in ATTACHMENT_POSITIVE:
                suite.addTest(test)
    if not ATTACHMENT_POSITIVE <= all_ids:
        raise AssertionError('The stock mixed-suite selection is out of date.')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


def download_verified(url, path, expected):
    for attempt in range(3):
        digest = hashlib.sha256()
        try:
            with urlopen(url, timeout=60) as response, path.open('xb') as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    digest.update(chunk)
                    output.write(chunk)
            break
        except HTTPError as error:
            if error.code not in (408, 429) and error.code < 500:
                raise
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise
        print('Retrying the CI test-runtime download after a connection failure.', flush=True)
        time.sleep(2)
    if digest.hexdigest() != expected:
        raise ValueError(f'Unexpected archive checksum for {path.name}.')


def main():
    if sys.argv[1:] == ['--stock-core']:
        stock_core_mixed_suites()
        return
    if sys.argv[1:]:
        raise SystemExit('Unknown CI runtime option.')
    if sys.version_info[:2] != (3, 9):
        raise SystemExit('This CI compatibility step requires Python 3.9.')
    supported = stock_sqlite_supported()
    for filename in ('test_native_sqlite_support.py', 'test_managed_sqlite_upgrade.py'):
        subprocess.run([sys.executable, str(ROOT / 'tests' / filename)],
                       cwd=ROOT, check=True, timeout=120)
    if supported:
        print('The stock Python 3.9 will run the full suite.', flush=True)
        return

    architecture, expected = BUILDS[(platform.system(), platform.machine())]
    filename = f'cpython-3.9.25+{RELEASE}-{architecture}-install_only_stripped.tar.gz'
    root = Path(tempfile.mkdtemp(prefix='motet-python39-', dir=os.environ['RUNNER_TEMP']))
    archive = root / filename
    url = f'https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/'
    download_verified(url + quote(filename, safe=''), archive, expected)
    subprocess.run(['tar', '-xf', str(archive), '-C', str(root)], check=True, timeout=60)

    windows = platform.system() == 'Windows'
    binary_dir = root / 'python' if windows else root / 'python' / 'bin'
    python = binary_dir / ('python.exe' if windows else 'python3')
    if windows and not (binary_dir / 'python3.exe').is_file():
        shutil.copyfile(python, binary_dir / 'python3.exe')
    subprocess.run([str(python), '-c', '''
from contextlib import closing
import sqlite3
import sys
sys.path.insert(0, 'src')
from attachments_owner_marker import sqlite_marker_available
assert sys.version_info[:3] == (3, 9, 25)
with closing(sqlite3.connect(':memory:')) as db:
    db.execute('CREATE VIRTUAL TABLE fixture_fts USING fts5(text)')
    assert sqlite_marker_available(db)
print(f'Attachment test interpreter: Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}', flush=True)
'''], cwd=ROOT, check=True, timeout=30)
    # GitHub steps call python, and Node fixtures call python3. Validate both
    # aliases before making this directory available to later steps.
    for executable in ('python.exe', 'python3.exe') if windows else ('python', 'python3'):
        subprocess.run([str(binary_dir / executable), '-c',
                        'import sys; assert sys.version_info[:3] == (3, 9, 25)'], check=True, timeout=30)
    with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as path_file:
        path_file.write(str(binary_dir) + '\n')


if __name__ == '__main__':
    main()
