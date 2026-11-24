#!/usr/bin/env python3
"""Create or inspect the private Antigravity inbox configuration on disk."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_channel
import private_storage
import peer_inbox
from install_antigravity_bridge import InstallError, SUPPORTED, rename_exclusive


def create(directory):
    value = {'version': 1, 'inbox_id': str(uuid.uuid4()), 'label': peer_inbox.LABEL,
             'receiver_key': secrets.token_hex(32)}
    temporary = '.antigravity-inbox-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(peer_inbox.canonical(value) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            rename_exclusive(directory, temporary, directory, peer_inbox.CONFIG_NAME)
        except FileExistsError:
            return peer_inbox.read_config(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
    return value


def setup(action, state_dir):
    import fcntl

    try:
        directory = private_storage.open_no_links(state_dir, directory=True)
    except FileNotFoundError:
        if action == 'status':
            return {'state': 'not_configured'}
        raise
    try:
        claude_channel._private(os.fstat(directory), True)
        fcntl.flock(directory, fcntl.LOCK_EX if action == 'init' else fcntl.LOCK_SH)
        try:
            value = peer_inbox.read_config(directory)
        except FileNotFoundError:
            if action == 'status':
                return {'state': 'not_configured'}
            value = create(directory)
        return {'state': 'configured', 'inbox_id': value['inbox_id'],
                'inbox_revision': peer_inbox.revision(value), 'label': value['label']}
    finally:
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('action', choices=('init', 'status'))
    parser.add_argument('--state-dir', default=str(Path.home() / '.live-room'))
    args = parser.parse_args(argv)
    if not SUPPORTED:
        print(json.dumps({'state': 'unsupported'}))
        return 1
    try:
        result = setup(args.action, os.path.expanduser(args.state_dir))
    except InstallError as error:
        print(json.dumps({'state': 'unavailable', 'message': str(error)}))
        return 1
    except (OSError, ValueError, claude_channel.ChannelError):
        print(json.dumps({'state': 'unavailable', 'message': 'Check the private Motet state directory and inbox configuration.'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
