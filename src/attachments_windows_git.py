"""Git for Windows, as the workspace copies use it (step 6c.5b).

macOS and Linux use /usr/bin/git, which must belong to root and not be writable by group or others. Windows has no
such fixed place. Motet uses Git for Windows installed for all users, under Program Files, and runs its own program
directly (``Git\\mingw64\\bin\\git.exe``; ``Git\\cmd\\git.exe`` only starts that one). Every folder from Program Files
down to it, and the program itself, must belong to the system (SYSTEM, Administrators or TrustedInstaller) and be
changeable by nobody else (``unwritable(system_owned=True)``). Each is opened through the no-link walk, so a junction
or link anywhere on the way is refused, and Windows loads git.exe's libraries from that checked folder first.

A Git installed only for this account (under %LOCALAPPDATA%) is refused: any program the user runs could change it,
and Motet would then run that changed program with access to the workspace. The refusals carry fixed words the page
shows as they are.
"""
import os

import private_storage as ps

PROGRAM = ('Git', 'mingw64', 'bin', 'git.exe')
WHY = {
    'attachments_git_missing': ('Sending files to Claude chats needs Git for Windows installed for all users (in '
                                'Program Files). Ordinary messages still work.'),
    'attachments_git_per_user': ('Sending files to Claude chats needs Git for Windows installed for all users (in '
                                 'Program Files). Git installed only for your account can be changed by any program '
                                 'you run, so Motet does not use it. Ordinary messages still work.'),
    'attachments_git_unsafe': ('Sending files is unavailable because the Git in Program Files can be changed by '
                               'programs you run. Reinstall Git for Windows for all users. Ordinary messages still '
                               'work.'),
}


class GitUnavailable(ValueError):
    def __init__(self, code):
        self.code, self.words = code, WHY[code]
        super().__init__(code)


def _roots():
    """Program Files as Windows names it (the 64-bit one first, also from a 32-bit Python)."""
    roots = []
    for name in ('ProgramW6432', 'ProgramFiles'):
        value = os.environ.get(name)
        if value and os.path.isabs(value) and value not in roots:
            roots.append(value)
    return roots


def checked(executable):
    """Raise GitUnavailable('attachments_git_unsafe') unless ``executable`` (a resolved absolute path to git.exe) and
    every folder from Program Files down to it are held without a link on the way, belong to the system and can be
    changed by nobody else."""
    root = next((r for r in _roots() if os.path.normcase(executable).startswith(os.path.normcase(
        os.path.join(os.path.realpath(r), '')))), None)
    if root is None:
        raise GitUnavailable('attachments_git_unsafe')
    base = os.path.realpath(root)
    folders = [base]
    for name in os.path.relpath(os.path.dirname(executable), base).split(os.sep):
        folders.append(os.path.join(folders[-1], name))
    try:
        for folder in folders:
            with ps.open_folder(folder) as held:
                held.unwritable(system_owned=True)
        # Not one name only: Git for Windows installs its built-in commands as hard links of git.exe. A name is
        # added to a file only by someone who may change it, and the file's owner and list are checked here.
        fd = ps.open_no_links(executable)
        try:
            ps.unwritable(fd, system_owned=True)
        finally:
            os.close(fd)
    except OSError:
        raise GitUnavailable('attachments_git_unsafe') from None


def find():
    """The checked git.exe of Git for Windows installed for all users, as a resolved absolute path."""
    for root in _roots():
        candidate = os.path.join(root, *PROGRAM)
        if os.path.isfile(candidate):
            executable = os.path.realpath(candidate)
            checked(executable)
            return executable
    local = os.environ.get('LOCALAPPDATA')
    if local and os.path.isfile(os.path.join(local, 'Programs', *PROGRAM)):
        raise GitUnavailable('attachments_git_per_user')
    raise GitUnavailable('attachments_git_missing')


def refusal():
    """None when a usable Git is installed, else (code, words) for the page."""
    try:
        find()
    except GitUnavailable as error:
        return error.code, error.words
    return None


def environment(executable):
    """The whole environment Git runs with: only its own folder and Windows' on PATH, Windows' own folder (which
    Windows programs need), the C locale, no system or global configuration and no prompts."""
    system = os.environ.get('SystemRoot') or 'C:\\Windows'
    return {'PATH': os.path.dirname(executable) + os.pathsep + os.path.join(system, 'System32'), 'SystemRoot': system,
            'LANG': 'C', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
