#!/usr/bin/env python3
"""Install Motet's self folder and keep the version history of its files. The owner's command; Motet never runs it.

Motet's self lives in ~/.live-room/motet-self/: SOUL.md, RULES.md, IDENTITY.md, VOCABULARY.md, ASKING_MOTET.md
and skills/*.md. Every saved version of each file is kept in motet-self/.history/, with who changed it, when and why.
History is never rewritten or deleted: a restore is recorded as a new version. If the history of a file and its
latest-version marker disagree (a record missing or changed by hand), every command refuses rather than guess.

  init --from FOLDER        create the folder, private, from your prepared copies; each becomes version 1.
                            FOLDER must be your prepared copies, never the repository's motet/ templates
                            (a folder holding INSTRUCTIONS.md or VOCABULARY.next.md is refused).
                            Refuses if the self folder already exists.
  scan                      record every file you edited directly since its last version.
  history [FILE]            list the versions of one file, or of every file.
  show FILE VERSION         print one version of a file.
  restore FILE VERSION      make an earlier version current again (recorded as "restored from vN").

FILE is SOUL.md, RULES.md, IDENTITY.md, VOCABULARY.md, ASKING_MOTET.md or skills/<name>.md. VERSION is 3 or v3.
Nothing here asks a model, and nothing in Motet's server, tools or engines can write these files.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import motet_self
import motet_self_history as history


def main(argv=None):
    parser = argparse.ArgumentParser(prog='motet_self.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', metavar='COMMAND')
    commands.required = True
    init = commands.add_parser('init', help='create the private self folder from your prepared copies')
    init.add_argument('--from', dest='source', required=True, metavar='FOLDER',
                      help='your prepared copies: SOUL.md, RULES.md, IDENTITY.md, VOCABULARY.md, ASKING_MOTET.md and skills/ '
                           '(not the repository\'s motet/ folder)')
    commands.add_parser('scan', help='record files you edited directly as new versions')
    listing = commands.add_parser('history', help='list recorded versions')
    listing.add_argument('file', nargs='?', metavar='FILE')
    show = commands.add_parser('show', help='print one version of a file')
    show.add_argument('file', metavar='FILE')
    show.add_argument('version', metavar='VERSION')
    restore = commands.add_parser('restore', help='make an earlier version current again')
    restore.add_argument('file', metavar='FILE')
    restore.add_argument('version', metavar='VERSION')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            names = motet_self.init(args.source)
            print('Installed %d files in %s, each as version 1.' % (len(names), history.self_folder()))
        elif args.command == 'scan':
            for name, status in motet_self.scan():
                print('%-32s %s' % (name, status))
        elif args.command == 'history':
            motet_self.settle()
            for name, rows in history.list_history(name=args.file).items():
                print(name)
                for row in rows:
                    print('  v%-5d %s  %-5s  %s' % (row['version'], row['at'], row['author'], row['reason']))
        elif args.command == 'show':
            motet_self.settle()
            root = history.self_folder()
            history.check_folders(root, args.file)
            record = history.read_version(root, history.check_name(args.file), history.parse_version(args.version))
            out = getattr(sys.stdout, 'buffer', None)
            if out is not None:
                sys.stdout.flush()
                out.write(record['content'].encode('utf-8'))
            else:
                sys.stdout.write(record['content'])
        elif args.command == 'restore':
            number = motet_self.restore(args.file, history.parse_version(args.version))
            print('Restored %s from v%d as v%d.' % (history.check_name(args.file), history.parse_version(args.version), number))
    except history.SelfError as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError:
        print('A file in Motet\'s self folder could not be read or written; that change did not complete.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
