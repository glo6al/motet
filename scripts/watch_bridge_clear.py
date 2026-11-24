#!/usr/bin/env python3
"""Inspect or explicitly retire one unresolved Bridge attempt through its running owner server.

No provider calls, direct database writes, retry, redirect, or offline fallback.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import uuid

MAX_RESPONSE = 16384
CONFIRM = 'retire_unknown_without_resend'
# Kept in step with peer_bridge.BRIDGE_UNAVAILABLE; this script reads nothing from src.
UNAVAILABLE = ('Bridge holds cannot be inspected or cleared on Windows. The agent-to-agent bridge is not available on '
               'Windows yet, so no chat can be on hold through it here. Nothing was changed.')


def bridge_available():
    """The bridge runs only where chats can be found by handle-based folder walk: macOS and Linux for now."""
    return os.name == 'posix'


def canonical_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def read_owner_key(state_dir):
    """Open private POSIX state/key without following links, including ancestors."""
    if os.name != 'posix':
        raise ValueError('Online owner recovery requires a private POSIX key file on this host.')
    path = Path(os.path.abspath(os.path.expanduser(str(state_dir))))
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Owner state directory must be private and owned by this user.')
        fd = os.open('key', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('Owner key must be a private regular file owned by this user.')
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    if len(raw) > 4096:
        raise ValueError('Owner key file is invalid.')
    try:
        key = raw.decode('ascii').rstrip('\r\n')
    except UnicodeError:
        raise ValueError('Owner key file is invalid.') from None
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Owner key file is invalid.')
    return key


def owner_request(port, key, route, data):
    """One loopback HTTP attempt. http.client never follows redirects or proxies."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        body = json.dumps(data, allow_nan=False).encode('utf-8')
        connection.request('POST', '/bridge-admin/v1/' + route, body=body,
                           headers={'X-Live-Room-Key': key, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE:
            # Do not echo an untrusted body, headers, redirects or credential text.
            raise ValueError('Owner server refused the request or returned an invalid response.')
        result = json.loads(raw.decode('utf-8'))
        if not isinstance(result, dict):
            raise ValueError('Owner server returned an invalid response.')
        return result
    except (OSError, http.client.HTTPException, UnicodeError, ValueError, RecursionError):
        raise ValueError('Owner request could not be confirmed. No retry or offline fallback was attempted.') from None
    finally:
        connection.close()


def safe_receipt(result, selector, identifier, expected_agent, apply):
    """Print only validated metadata; never trust a server to redact arbitrary text."""
    transport_id = result.get('transport_id')
    if result.get(selector) != identifier or not canonical_id(transport_id):
        raise ValueError('Owner response did not identify the requested attempt.')
    agent = result.get('agent')
    if not isinstance(agent, str) or agent[:2] not in ('x:', 'c:') or not canonical_id(agent[2:]):
        raise ValueError('Owner response did not identify an exact recipient.')
    if expected_agent and agent != expected_agent:
        raise ValueError('Owner response recipient did not match.')
    out = {selector: identifier, 'transport_id': transport_id, 'agent': agent}
    if apply:
        if result.get('status') not in ('retired', 'already_retired') or result.get('provider_outcome') != 'unknown':
            raise ValueError('Owner response did not confirm retirement.')
        out.update(status=result['status'], provider_outcome='unknown',
                   explanation='Owner retirement releases this reservation only. Nothing was cancelled or resent; late replies are withheld.')
    else:
        if (result.get('execution') not in ('reserved', 'submitted', 'running', 'uncertain', 'completed', 'dropped', 'retired')
                or result.get('capture') not in ('pending', 'captured', 'unusable', 'withheld')
                or type(result.get('retained_payload')) is not bool or type(result.get('eligible')) is not bool):
            raise ValueError('Owner response contained invalid lifecycle metadata.')
        out.update({k: result[k] for k in ('execution', 'capture', 'retained_payload', 'eligible')})
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True, help='Running Motet port on 127.0.0.1')
    parser.add_argument('--state-dir', default=str(Path.home() / '.live-room'), help='Private owner state directory (key only; no database access)')
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument('--transport-id', help='Full exact Bridge transport UUID')
    selector.add_argument('--request-id', help='Full public request UUID from a shared Motet request or busy refusal')
    parser.add_argument('--expected-agent', help='Exact x: or c: native recipient ID; required to apply')
    parser.add_argument('--apply', action='store_true', help='Explicitly retire this unresolved attempt, keeping provider outcome unknown')
    args = parser.parse_args(argv)
    selector = 'request_id' if args.request_id is not None else 'transport_id'
    identifier = getattr(args, selector)
    if not 1 <= args.port <= 65535 or not canonical_id(identifier):
        parser.error('Use a valid port and full canonical request or transport UUID.')
    if args.expected_agent and (args.expected_agent[:2] not in ('x:', 'c:') or not canonical_id(args.expected_agent[2:])):
        parser.error('Use the exact native recipient identifier.')
    if args.apply and not args.expected_agent:
        parser.error('--apply requires --expected-agent from inspection.')
    if not bridge_available():
        # Refused before the owner key is read or anything is sent: nothing can be on hold without the bridge.
        print(UNAVAILABLE, file=sys.stderr)
        return 1
    try:
        key = read_owner_key(args.state_dir)
        data = {selector: identifier}
        if args.apply:
            data.update(agent=args.expected_agent, confirm=CONFIRM)
        result = owner_request(args.port, key, 'retire' if args.apply else 'inspect', data)
        print(json.dumps(safe_receipt(result, selector, identifier, args.expected_agent, args.apply), indent=2))
    except (OSError, ValueError):
        # Exception strings from local I/O/network stacks can expose paths or headers.
        print('Bridge owner request could not be confirmed. No retry or offline fallback was attempted.'
              + (' Retirement outcome is unknown; inspect the same attempt before acting again.' if args.apply else ''), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
