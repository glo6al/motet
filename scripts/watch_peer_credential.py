#!/usr/bin/env python3
"""Print one Motet bridge credential for the bridge adapter, after Motet's own privacy checks. Windows only in use.

Node cannot read a Windows access list, so on Windows integrations/claude/peer.mjs runs this script (with ``-I``)
instead of opening its credential file itself. The file is read through watch_peer.read_credential: its folder is
held open, the file is opened without following a link or junction, and its owner, protected this-user/SYSTEM
access list and single name are checked on the opened handle (on macOS and Linux: owner and mode instead). Only a
shared-service credential (version 2) is printed, as one line of JSON on standard output. A refusal prints one
fixed line on standard error, never a path or the file's contents.

Usage: python -I scripts/watch_peer_credential.py <absolute path to the credential file>
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))

REFUSED = 'Motet bridge credential refused: it must be a private shared-service credential file owned by you.'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 1 or not os.path.isabs(argv[0]) or os.path.abspath(argv[0]) != argv[0]:
            raise ValueError()
        import watch_peer
        credential = watch_peer.read_credential(argv[0])
        if credential.get('version') != 2:
            raise ValueError()
    except Exception:
        print(REFUSED, file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(credential, sort_keys=True, separators=(',', ':')) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
