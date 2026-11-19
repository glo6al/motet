#!/usr/bin/env python3
"""Register the outbound Motet MCP plugin in a local Antigravity installation."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from watch_peer import ROOT, AntigravityPathError, antigravity_config, claude_channel


if os.name == 'posix':
    import fcntl

    DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

PLUGIN = 'motet-bridge'
TEMPLATE = ROOT / 'integrations' / 'antigravity-bridge'
STAMP = '.motet-install.json'
FILE_LIMIT = 65536
SUPPORTED = sys.platform in ('darwin', 'linux')


class InstallError(Exception):
    pass


def check_directory(fd, path, private=False):
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.getuid() or mode & 0o022 or (private and mode != 0o700):
        raise InstallError(f'Antigravity plugin directory must be owner-controlled: {path}.')


def open_directory(stack, parent, name, private=False, *, path=None):
    path = name if path is None else path
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError:
        raise InstallError(f'Cannot open Antigravity plugin directory {path}.') from None
    stack.callback(os.close, fd)
    check_directory(fd, path, private)
    return fd


@contextmanager
def plugin_parent(home, install=False, remove=False):
    with ExitStack() as stack:
        try:
            fd = claude_channel._directory(home)
        except OSError:
            raise InstallError(f'Cannot open Antigravity home directory {home}.') from None
        stack.callback(os.close, fd)
        check_directory(fd, home)
        gemini_path = home / '.gemini'
        config_path = gemini_path / 'config'
        plugins_path = config_path / 'plugins'
        try:
            gemini = open_directory(stack, fd, '.gemini', path=gemini_path)
            config = open_directory(stack, gemini, 'config', path=config_path)
        except FileNotFoundError:
            if install:
                raise InstallError(f'Set up Antigravity before installing this plugin. Missing {config_path}.') from None
            yield None
            return
        if install:
            try:
                os.mkdir('plugins', 0o700, dir_fd=config)
            except FileExistsError:
                pass
            except OSError:
                raise InstallError(f'Cannot create Antigravity plugin directory {plugins_path}.') from None
        try:
            plugins = open_directory(stack, config, 'plugins', path=plugins_path)
        except FileNotFoundError:
            yield None
            return
        fcntl.flock(plugins, fcntl.LOCK_EX if install or remove else fcntl.LOCK_SH)
        yield plugins


def encoded(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def stamp(files):
    return encoded({'version': 1, 'files': {name: hashlib.sha256(raw).hexdigest()
                                            for name, raw in sorted(files.items())}})


def read_asset(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > FILE_LIMIT):
            raise InstallError('The plugin contains an unsafe file; it was preserved.')
        raw = file.read(FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise InstallError('The plugin contains an oversized file; it was preserved.')
        return raw


def installed_assets(plugin, rules):
    if (set(os.listdir(plugin)) != {STAMP, 'plugin.json', 'mcp_config.json', 'rules'}
            or set(os.listdir(rules)) != {'AGENTS.md'}):
        raise InstallError('The plugin contains missing or unrelated files; it was preserved.')
    files = {'plugin.json': read_asset(plugin, 'plugin.json'),
             'mcp_config.json': read_asset(plugin, 'mcp_config.json'),
             'rules/AGENTS.md': read_asset(rules, 'AGENTS.md')}
    if read_asset(plugin, STAMP) != stamp(files):
        raise InstallError('The plugin files differ from the installed hashes; they were preserved.')
    return files


def write_asset(directory, name, raw):
    temporary = '.motet-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=directory)


def rename_exclusive(source_dir, source, target_dir, target):
    """Publish or unpublish an entire directory without replacing any target."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renameatx_np
        exclusive = 0x4  # RENAME_EXCL
    else:
        rename = getattr(libc, 'renameat2', None)
        exclusive = 1  # RENAME_NOREPLACE
        if rename is None:
            raise InstallError('The operating system does not support exclusive directory publication.')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_dir, os.fsencode(source), target_dir, os.fsencode(target), exclusive):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def staging(parent):
    # The plugin lives one level below this hidden directory until the rename.
    # Even a stopped process leaves no manifest at a recognized plugin path.
    name = '.motet-bridge-' + uuid.uuid4().hex
    os.mkdir(name, 0o700, dir_fd=parent)
    try:
        with ExitStack() as stack:
            yield open_directory(stack, parent, name, private=True)
    finally:
        try:
            os.rmdir(name, dir_fd=parent)
        except OSError as error:
            if error.errno != errno.ENOTEMPTY:
                raise


def same_directory(parent, name, fd):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return os.path.samestat(info, os.fstat(fd))


def assets(plugin, rules, files):
    return ((rules, 'AGENTS.md', files['rules/AGENTS.md']),
            (plugin, 'mcp_config.json', files['mcp_config.json']), (plugin, STAMP, stamp(files)),
            (plugin, 'plugin.json', files['plugin.json']))


def remove_assets(entries):
    for directory, name, raw in reversed(entries):
        if read_asset(directory, name) != raw:
            raise InstallError('The plugin files changed during removal; the changed file was preserved.')
        os.unlink(name, dir_fd=directory)


def publish_plugin(parent, files):
    with staging(parent) as stage, ExitStack() as stack:
        os.mkdir('payload', 0o700, dir_fd=stage)
        plugin = open_directory(stack, stage, 'payload', private=True)
        os.mkdir('rules', 0o700, dir_fd=plugin)
        rules = open_directory(stack, plugin, 'rules', private=True)
        written = []
        try:
            for entry in assets(plugin, rules, files):
                write_asset(*entry)
                written.append(entry)
            rename_exclusive(stage, 'payload', parent, PLUGIN)
        finally:
            if same_directory(stage, 'payload', plugin):
                remove_assets(written)
                os.rmdir('rules', dir_fd=plugin)
                os.rmdir('payload', dir_fd=stage)


def remove_plugin(parent, plugin, files):
    with staging(parent) as stage, ExitStack() as stack:
        if not same_directory(parent, PLUGIN, plugin):
            raise InstallError('The plugin directory changed during removal.')
        rename_exclusive(parent, PLUGIN, stage, 'payload')
        try:
            if not same_directory(stage, 'payload', plugin):
                raise InstallError('The plugin directory changed during removal.')
            rules = open_directory(stack, plugin, 'rules', private=True)
            if installed_assets(plugin, rules) != files:
                raise InstallError('The plugin files changed during removal.')
        except (OSError, InstallError):
            rename_exclusive(stage, 'payload', parent, PLUGIN)
            raise
        remove_assets(assets(plugin, rules, files))
        os.rmdir('rules', dir_fd=plugin)
        os.rmdir('payload', dir_fd=stage)


def update(action, home, expected=None):
    with plugin_parent(home, install=action == 'install', remove=action == 'remove') as parent:
        if parent is None:
            return 'absent'
        with ExitStack() as stack:
            try:
                plugin = open_directory(stack, parent, PLUGIN, private=True)
            except FileNotFoundError:
                if action == 'install':
                    publish_plugin(parent, expected)
                    return 'registered'
                return 'absent'
            try:
                rules = open_directory(stack, plugin, 'rules', private=True)
                files = installed_assets(plugin, rules)
            except FileNotFoundError:
                raise InstallError('The plugin is incomplete; its files were preserved.') from None
            if action == 'install' and files != expected:
                raise InstallError('A different managed plugin is registered. Remove it before installing this configuration.')
            if action == 'remove':
                remove_plugin(parent, plugin, files)
                return 'absent'
            return 'registered'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('action', choices=('install', 'status', 'remove'))
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--credential', type=Path)
    parser.add_argument('--inbox-credential', type=Path)
    parser.add_argument('--node', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'install' and (args.credential is None or args.node is None):
        parser.error('install requires --credential and --node')
    if args.action != 'install' and any((args.credential, args.inbox_credential, args.node, args.repo)):
        parser.error('--credential, --node and --repo apply to install only')
    if not SUPPORTED:
        print(json.dumps({'state': 'unsupported', 'message': 'This installer requires macOS or Linux.'}))
        return 1
    try:
        files = None
        if args.action == 'install':
            settings = antigravity_config(args.credential, args.node, args.repo or ROOT, args.inbox_credential)
            files = {'plugin.json': (TEMPLATE / 'plugin.json').read_bytes(),
                     'rules/AGENTS.md': (TEMPLATE / 'rules/AGENTS.md').read_bytes(),
                     'mcp_config.json': encoded(settings)}
        result = update(args.action, args.home.expanduser(), files)
    except (InstallError, AntigravityPathError) as error:
        print(json.dumps({'state': 'unavailable', 'message': str(error)}))
        return 1
    except (OSError, ValueError, claude_channel.ChannelError):
        print(json.dumps({'state': 'unavailable', 'message': 'Check the local plugin paths, Node executable and private Motet credential.'}))
        return 1
    print(json.dumps({'state': result}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
