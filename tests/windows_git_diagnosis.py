"""What a failed Git for Windows lookup saw, for a test's failure message (tests only; Motet never runs these tools).

It names the Program Files roots tried, each place git.exe was looked for and whether a file is there, what the Git
folder under each root holds, the result of Motet's ownership and access check on each folder from Program Files
down to a program it found, ``where git``, and ``git --exec-path`` for a Git on PATH. So a CI run that cannot find
or accept Git says why, even where the lookup's own guess about the layout is wrong.
"""
import os
import shutil
import subprocess

import attachments_windows_git as wingit
import private_storage as ps


def _check(label, call):
    try:
        call()
        return '  %s: accepted' % label
    except OSError as error:
        return '  %s: refused (%s: %s)' % (label, type(error).__name__, error)


def _folder_check(folder):
    def call():
        with ps.open_folder(folder) as held:
            held.unwritable(system_owned=True)
    return _check(folder, call)


def _file_check(path):
    def call():
        fd = ps.open_no_links(path)
        try:
            ps.unwritable(fd, system_owned=True)
        finally:
            os.close(fd)
    return _check(path, call)


def _chain(root, executable):
    """Motet's check, one line per folder from ``root`` down to ``executable`` and for the program itself."""
    base = os.path.realpath(root)
    folders = [base]
    for name in os.path.relpath(os.path.dirname(executable), base).split(os.sep):
        folders.append(os.path.join(folders[-1], name))
    return [_folder_check(folder) for folder in folders] + [_file_check(executable)]


def _tool(argv):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return '%s: could not run (%s)' % (' '.join(argv), error)
    return '%s: exit %s, stdout %r, stderr %r' % (' '.join(argv), result.returncode, result.stdout[:2000],
                                                 result.stderr[:500])


def paths():
    """The lookup's own view: variables, roots, candidates, the Git folders' contents, and the check on a found one."""
    lines = ['%s=%r' % (name, os.environ.get(name))
             for name in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')]
    lines.append('roots tried: %r' % (wingit._roots(),))
    for candidate in wingit.candidates() + wingit.per_user_candidates():
        lines.append('%s: %s' % (candidate, 'file' if os.path.isfile(candidate) else
                                 'exists, not a file' if os.path.lexists(candidate) else 'absent'))
    for root in wingit._roots():
        folder = os.path.join(root, 'Git')
        try:
            lines.append('%s holds: %r' % (folder, sorted(os.listdir(folder))))
        except OSError as error:
            lines.append('%s: cannot list (%s)' % (folder, error))
    for root in wingit._roots():
        for program in wingit._programs():
            candidate = os.path.join(root, *program)
            if os.path.isfile(candidate):
                lines.append('check of %s:' % candidate)
                lines.extend(_chain(root, os.path.realpath(candidate)))
    return lines


def tools():
    """What Windows itself finds: ``where git``, and the Git on PATH's own folder."""
    lines = []
    where = shutil.which('where')
    lines.append(_tool([where, 'git']) if where else 'where: not available')
    git = shutil.which('git')
    lines.append(_tool([git, '--exec-path']) if git else 'git on PATH: none')
    return lines


def diagnosis():
    return '\n'.join(['Git for Windows lookup:'] + paths() + tools())
