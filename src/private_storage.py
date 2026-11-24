"""Private local storage checks shared by macOS, Linux and Windows. No I/O at import.

One rule per system, decided here so every caller refuses the same things:

- POSIX: a folder or file is private when it is not a link, has the expected
  type, is owned by this user, and grants nothing to group or others
  (``mode & 0o077 == 0``). Files are opened with ``O_NOFOLLOW`` and the opened
  descriptor is checked again with ``fstat``.
- Windows: the same, with the access list (DACL) in place of mode bits, as the
  Deck Log has always checked it (``windows_security``): the owner must be this
  user and only plain allow entries may appear. A file's list must be protected
  (nothing inherited) and name only this user and SYSTEM; a folder's may also
  name Administrators and OWNER RIGHTS. Any reparse point (symbolic link,
  junction, mount point or other) is refused. Files are opened with
  ``FILE_FLAG_OPEN_REPARSE_POINT`` and every check is made on the opened handle,
  then its identity is compared with the name that was looked at.

Nothing here loosens or repairs an existing file or folder. Creation helpers make
new ones private from the start; anything already there is checked and refused.
"""
import ctypes
import errno
import os
import secrets
import stat
import tempfile

WIN = os.name == 'nt'
REPARSE_POINT = 0x400          # FILE_ATTRIBUTE_REPARSE_POINT: links, junctions, mount points, and other reparse data
DIRECTORY = 0x10               # FILE_ATTRIBUTE_DIRECTORY
SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID = 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4'

# Reasons a caller can turn into its own plain words. ``subject`` says whether the
# file itself or the folder holding it was refused.
REASONS = frozenset(('missing', 'link', 'not_regular', 'owner', 'access', 'links', 'changed', 'open', 'unavailable'))


class PrivateStorageError(OSError):
    """Refusal with a fixed reason. ``str()`` is the message only: never a path or system text."""

    def __init__(self, message, reason='unavailable', subject='file'):
        super().__init__(message)
        self.reason = reason if reason in REASONS else 'unavailable'
        self.subject = subject


def _refuse(reason, subject='file', message=None):
    return PrivateStorageError(message or 'private storage refused: %s %s' % (subject, reason), reason, subject)


# ---------------------------------------------------------------------------
# Windows access-list rule. The decision is a pure function so it can be tested
# anywhere; the collector below reads the real descriptor with Win32 calls.

def acl_verdict(owner_is_user, has_dacl, protected, aces, directory):
    """Raise unless the collected descriptor is private to this user.

    ``aces`` lists the DACL in order as (ace_type, sid_string, is_current_user).
    The messages are the Deck Log's own, kept for its callers and tests."""
    if not owner_is_user:
        raise PrivateStorageError('nonprivate storage owner', 'owner')
    if not has_dacl or (not directory and not protected):
        raise PrivateStorageError('private storage unavailable', 'access')
    # Python's private mkdir DACL uses OWNER RIGHTS (S-1-3-4).
    # It names this user because the owner was checked above.
    allowed = (SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID) if directory else (SYSTEM_SID,)
    for ace_type, sid, is_user in aces:
        if ace_type != 0:  # only plain access-allowed ACEs
            raise PrivateStorageError('private storage unavailable', 'access')
        if not is_user and sid not in allowed:
            raise PrivateStorageError('private storage unavailable', 'access')


_API = None


def _win_api():
    """advapi32/kernel32 with explicit argument and result types, built once."""
    global _API
    if _API is not None:
        return _API
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    PL = ctypes.POINTER(w.LPVOID)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.OpenProcessToken.restype = w.BOOL
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.GetTokenInformation.restype = w.BOOL
    adv.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = w.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, PL, ctypes.POINTER(w.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, PL, PL, PL, PL, PL]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, PL, PL, PL, PL, PL]
    adv.GetSecurityInfo.restype = w.DWORD
    adv.GetSecurityDescriptorControl.argtypes = [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    adv.GetSecurityDescriptorControl.restype = w.BOOL
    adv.GetAclInformation.argtypes = [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int]
    adv.GetAclInformation.restype = w.BOOL
    adv.GetAce.argtypes = [w.LPVOID, w.DWORD, PL]
    adv.GetAce.restype = w.BOOL
    adv.EqualSid.argtypes = [w.LPVOID, w.LPVOID]
    adv.EqualSid.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [w.LPVOID]
    kernel.LocalFree.restype = w.LPVOID

    class SA(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('descriptor', w.LPVOID), ('inherit', w.BOOL)]

    class FILETIME(ctypes.Structure):
        _fields_ = [('low', w.DWORD), ('high', w.DWORD)]

    class FILEINFO(ctypes.Structure):      # BY_HANDLE_FILE_INFORMATION
        _fields_ = [('attributes', w.DWORD), ('created', FILETIME), ('accessed', FILETIME), ('written', FILETIME),
                    ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD),
                    ('index_high', w.DWORD), ('index_low', w.DWORD)]

    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SA), w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SA)]
    kernel.CreateDirectoryW.restype = w.BOOL
    kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FILEINFO)]
    kernel.GetFileInformationByHandle.restype = w.BOOL
    kernel.GetFileType.argtypes = [w.HANDLE]
    kernel.GetFileType.restype = w.DWORD
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    kernel.SetFileInformationByHandle.restype = w.BOOL
    _API = (adv, kernel, w, SA, FILEINFO)
    return _API


def _unavailable(subject='file'):
    return PrivateStorageError('private storage unavailable', 'unavailable', subject)


class _CurrentUser:
    """The process token's user SID, held for the duration of a check."""

    def __enter__(self):
        adv, kernel, w = _win_api()[:3]
        self.token = w.HANDLE()
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(self.token)):  # TOKEN_QUERY
            raise _unavailable()
        try:
            needed, text = w.DWORD(), w.LPWSTR()
            adv.GetTokenInformation(self.token, 1, None, 0, ctypes.byref(needed))  # TokenUser
            self.buf = ctypes.create_string_buffer(max(1, needed.value))
            if not adv.GetTokenInformation(self.token, 1, self.buf, needed, ctypes.byref(needed)):
                raise _unavailable()
            self.sid = ctypes.cast(self.buf, ctypes.POINTER(w.LPVOID))[0]
            if not adv.ConvertSidToStringSidW(self.sid, ctypes.byref(text)):
                raise _unavailable()
            try:
                self.text = text.value
            finally:
                kernel.LocalFree(text)
        except BaseException:
            kernel.CloseHandle(self.token)
            raise
        return self

    def __exit__(self, *exc):
        _win_api()[1].CloseHandle(self.token)
        return False


class _PrivateAttributes:
    """SECURITY_ATTRIBUTES owned by this user with a protected this-user/SYSTEM DACL."""

    def __init__(self, user):
        self.user = user

    def __enter__(self):
        adv, kernel, w, SA = _win_api()[:4]
        sddl = 'O:%sD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;%s)' % (self.user.text, self.user.text)
        self.descriptor = w.LPVOID()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(self.descriptor), None):
            raise _unavailable()
        self.sa = SA(ctypes.sizeof(SA), self.descriptor, False)
        return self.sa

    def __exit__(self, *exc):
        _win_api()[1].LocalFree(self.descriptor)
        return False


def _check_descriptor(user, directory, path=None, handle=None):
    adv, kernel, w = _win_api()[:3]
    owner, dacl, sd = w.LPVOID(), w.LPVOID(), w.LPVOID()
    if handle is None:
        failed = adv.GetNamedSecurityInfoW(path, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
    else:
        failed = adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
    if failed:
        raise _unavailable()
    try:
        if not adv.EqualSid(owner, user.sid):
            acl_verdict(False, False, False, [], directory)
        control, revision = w.WORD(), w.DWORD()
        if dacl and not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            raise _unavailable()
        protected = bool(control.value & 0x1000)  # SE_DACL_PROTECTED
        aces = []
        if dacl and (directory or protected):
            class ACLINFO(ctypes.Structure):
                _fields_ = [('count', w.DWORD), ('used', w.DWORD), ('free', w.DWORD)]
            info = ACLINFO()
            if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                raise _unavailable()
            for i in range(info.count):
                ace = w.LPVOID()
                if not adv.GetAce(dacl, i, ctypes.byref(ace)):
                    raise _unavailable()
                ace_type = ctypes.string_at(ace, 8)[0]
                if ace_type != 0:
                    aces.append((ace_type, None, False))
                    break
                ace_sid = w.LPVOID(ace.value + 8)
                value = w.LPWSTR()
                if not adv.ConvertSidToStringSidW(ace_sid, ctypes.byref(value)):
                    raise _unavailable()
                try:
                    aces.append((0, value.value, bool(adv.EqualSid(ace_sid, user.sid))))
                finally:
                    kernel.LocalFree(value)
        acl_verdict(True, bool(dacl), protected, aces, directory)
    finally:
        kernel.LocalFree(sd)


def windows_security(path, directory=False, create=False):
    """Protected current-user/SYSTEM DACL, checked with actual Win32 APIs.

    With ``create`` the folder or file is made (and must not exist yet) owned by
    this user with a protected private DACL, then checked. Without it the existing
    path is only checked. This is the Deck Log's check, moved here unchanged."""
    adv, kernel, w = _win_api()[:3]
    with _CurrentUser() as user:
        if create:
            with _PrivateAttributes(user) as sa:
                if directory:
                    if not kernel.CreateDirectoryW(path, ctypes.byref(sa)):
                        raise OSError('private storage unavailable')
                else:
                    handle = kernel.CreateFileW(path, 0xC0000000, 0, ctypes.byref(sa), 1, 0x80, None)
                    if handle == w.HANDLE(-1).value:
                        raise OSError('private storage unavailable')
                    kernel.CloseHandle(handle)
        _check_descriptor(user, directory, path=path)


def windows_handle_security(handle, directory=False):
    """``windows_security`` for an already opened handle (needs READ_CONTROL access)."""
    with _CurrentUser() as user:
        _check_descriptor(user, directory, handle=handle)


# Win32 constants for handle-based opens.
_GENERIC_READ, _GENERIC_WRITE, _READ_CONTROL, _FILE_READ_ATTRIBUTES = 0x80000000, 0x40000000, 0x20000, 0x80
_DELETE = 0x10000
_FILE_DISPOSITION_INFO = 4     # FILE_INFO_BY_HANDLE_CLASS FileDispositionInfo
_SHARE_READ, _SHARE_WRITE, _SHARE_DELETE = 1, 2, 4
_CREATE_NEW, _OPEN_EXISTING, _OPEN_ALWAYS = 1, 3, 4
_BACKUP_SEMANTICS, _OPEN_REPARSE_POINT, _ATTRIBUTE_NORMAL = 0x02000000, 0x00200000, 0x80
_FILE_TYPE_DISK = 1
_NOT_FOUND = (2, 3)            # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
_EXISTS = (80, 183)            # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS


def _win_open(path, access, share, disposition, flags, sa=None):
    """A raw handle that does not follow a reparse point at the last name; raises OSError with winerror."""
    kernel, w = _win_api()[1:3]
    handle = kernel.CreateFileW(path, access, share, ctypes.byref(sa) if sa is not None else None,
                                disposition, flags | _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT, None)
    if handle is None or handle == w.HANDLE(-1).value:
        code = ctypes.get_last_error()
        error = OSError('private storage open failed')
        error.winerror = code
        raise error
    return handle


def _win_close(handle):
    if handle is not None:
        _win_api()[1].CloseHandle(handle)


def _win_check_handle(handle, directory, subject, single_link=False):
    """Type, reparse and access list of an opened handle; returns BY_HANDLE_FILE_INFORMATION."""
    kernel, w, SA, FILEINFO = _win_api()[1:5]
    info = FILEINFO()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _unavailable(subject)
    if info.attributes & REPARSE_POINT:
        raise _refuse('link', subject)
    if bool(info.attributes & DIRECTORY) != directory or (not directory and kernel.GetFileType(handle) != _FILE_TYPE_DISK):
        raise _refuse('not_regular', subject)
    if single_link and not directory and info.links != 1:
        raise _refuse('links', subject)
    try:
        windows_handle_security(handle, directory)
    except PrivateStorageError as e:
        raise PrivateStorageError(str(e), e.reason, subject) from None
    return info


def _win_discard(handle):
    """Delete the file this handle has open (it must hold DELETE access), then close it. The deletion is by
    handle, so it can only remove the file that was created, whatever its name points to by then."""
    try:
        flag = ctypes.c_ubyte(1)                  # FILE_DISPOSITION_INFO.DeleteFile = TRUE
        _win_api()[1].SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, ctypes.byref(flag), ctypes.sizeof(flag))
    finally:
        _win_close(handle)


def _win_fd(handle, flags=0):
    """Hand the handle to a C runtime descriptor; closing the descriptor closes the handle."""
    import msvcrt
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _win_close(handle)
        raise


# ---------------------------------------------------------------------------
# (a) Is this folder or file private to the current user?

def posix_verdict(info, directory=False, single_link=False, subject='file'):
    """The POSIX rule for an lstat/fstat result (also used for opened descriptors)."""
    if stat.S_ISLNK(info.st_mode):
        raise _refuse('link', subject)
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise _refuse('not_regular', subject)
    if info.st_uid != os.getuid():
        raise _refuse('owner', subject)
    if info.st_mode & 0o077:
        raise _refuse('access', subject)
    if single_link and not directory and info.st_nlink != 1:
        raise _refuse('links', subject)


def check_private(path, directory=False, single_link=False, subject=None):
    """Raise PrivateStorageError unless ``path`` itself (not a link to it) is a private folder or regular file.

    On Windows the check is made on a handle opened without following a reparse point."""
    subject = subject or ('folder' if directory else 'file')
    if not WIN:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise _refuse('missing', subject) from None
        posix_verdict(info, directory, single_link, subject)
        return
    try:
        handle = _win_open(path, _READ_CONTROL | _FILE_READ_ATTRIBUTES, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, _OPEN_EXISTING, 0)
    except OSError as e:
        raise _refuse('missing' if getattr(e, 'winerror', 0) in _NOT_FOUND else 'open', subject) from None
    try:
        _win_check_handle(handle, directory, subject, single_link)
    finally:
        _win_close(handle)


def make_private_folder(path):
    """Create one new private folder (it must not exist): mode 0700 on POSIX, a private DACL on Windows."""
    if WIN:
        windows_security(path, True, True)
    else:
        os.mkdir(path, 0o700)


# ---------------------------------------------------------------------------
# (b) Open an existing private regular file for reading.

def open_private_file(path, folder=False, single_link=False, expect=None):
    """Open ``path`` for reading and return ``(fd, fstat_result)``; the caller closes ``fd``.

    The descriptor is the file that was checked: links are refused at the open,
    and type, owner and access are read from the opened file. ``folder`` also
    requires the containing folder to be private (on Windows the folder is held
    open, so it cannot be renamed or replaced, until the file is open).
    ``single_link`` refuses a file with more than one name. ``expect`` is an
    earlier stat result the opened file must match."""
    if not WIN:
        if folder:
            check_private(os.path.dirname(path) or '.', True)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))  # a pipe cannot hang the open
        except OSError as e:
            raise _refuse('missing' if e.errno == errno.ENOENT else
                          'link' if e.errno in (errno.ELOOP, errno.EMLINK) else 'open') from None
        try:
            info = os.fstat(fd)
            posix_verdict(info, False, single_link)
            if expect is not None and not os.path.samestat(expect, info):
                raise _refuse('changed')
        except BaseException:
            os.close(fd)
            raise
        return fd, info
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if folder and not os.path.lexists(os.path.dirname(path)):
            raise _refuse('missing', 'folder') from None
        raise _refuse('missing') from None
    except OSError:
        raise _refuse('open') from None
    folder_handle = None
    try:
        if folder:
            try:
                # Without FILE_SHARE_DELETE nobody can rename or remove the folder while it is held.
                folder_handle = _win_open(os.path.dirname(path) or '.', _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                                          _SHARE_READ | _SHARE_WRITE, _OPEN_EXISTING, 0)
            except OSError as e:
                raise _refuse('missing' if getattr(e, 'winerror', 0) in _NOT_FOUND else 'open', 'folder') from None
            _win_check_handle(folder_handle, True, 'folder')
        if getattr(before, 'st_file_attributes', 0) & REPARSE_POINT or stat.S_ISLNK(before.st_mode):
            raise _refuse('link')
        if not stat.S_ISREG(before.st_mode):
            raise _refuse('not_regular')
        try:
            # Share read only: no writer, rename or delete while it is read.
            handle = _win_open(path, _GENERIC_READ, _SHARE_READ, _OPEN_EXISTING, 0)
        except OSError as e:
            raise _refuse('missing' if getattr(e, 'winerror', 0) in _NOT_FOUND else 'open') from None
        try:
            _win_check_handle(handle, False, 'file', single_link)
        except BaseException:
            _win_close(handle)
            raise
        fd = _win_fd(handle)
        try:
            info = os.fstat(fd)
            if not info.st_ino or not os.path.samestat(before, info) or (expect is not None and not os.path.samestat(expect, info)):
                raise _refuse('changed')
        except BaseException:
            os.close(fd)
            raise
        return fd, info
    finally:
        _win_close(folder_handle)


# ---------------------------------------------------------------------------
# (c) Create a new private file for writing.

def create_private_file(path):
    """Create ``path`` (it must not exist; a link there counts as existing) private from the start.
    Returns a descriptor open for writing; the caller closes it."""
    if not WIN:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            os.fchmod(fd, 0o600)       # a umask can only remove bits; this puts the owner's back
            posix_verdict(os.fstat(fd), False, True)
        except BaseException:
            _posix_discard(fd, path)
            raise
        return fd
    with _CurrentUser() as user, _PrivateAttributes(user) as sa:
        # DELETE access lets a failed check remove the new file through this same handle.
        handle = _win_open(path, _GENERIC_READ | _GENERIC_WRITE | _DELETE, 0, _CREATE_NEW, _ATTRIBUTE_NORMAL, sa)
    try:
        _win_check_handle(handle, False, 'file', True)
        import msvcrt
        return msvcrt.open_osfhandle(handle, 0)
    except BaseException:
        _win_discard(handle)
        raise


def _posix_discard(fd, path):
    """Close a file just created at ``path`` and remove that name, but only while it still names this file."""
    try:
        mine = os.fstat(fd)
    except OSError:
        mine = None
    os.close(fd)
    try:
        if mine is not None and os.path.samestat(os.lstat(path), mine):
            os.unlink(path)
    except OSError:
        pass


def create_private_temp(directory, prefix):
    """A new private file with a unique name in ``directory``: ``(fd, path)``, ready to be renamed into place."""
    if not WIN:
        fd, path = tempfile.mkstemp(prefix=prefix, dir=directory)
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            os.unlink(path)
            raise
        return fd, path
    for _ in range(100):
        path = os.path.join(directory, prefix + secrets.token_hex(8))
        try:
            return create_private_file(path), path
        except OSError as e:
            if getattr(e, 'winerror', 0) not in _EXISTS:
                raise
    raise FileExistsError(errno.EEXIST, 'no unique private file name')


# ---------------------------------------------------------------------------
# (d) Open an existing private file for reading and writing, or create one.

def open_private_rw(path, create=False):
    """Open ``path`` for reading and writing (never through a link) and return its descriptor; the caller closes it.

    With ``create`` a missing file is made private from the start (mode 0600, or a protected this-user/SYSTEM
    access list); an existing file is only checked, never repaired. Either way the opened file must be a private
    regular file with one name. A failed open itself raises the system's own OSError, unchanged. On Windows the
    handle does not share delete, so while it is open nobody can rename or replace the file behind it."""
    if not WIN:
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | (os.O_CREAT if create else 0)
        fd = os.open(path, flags, 0o600)
        try:
            posix_verdict(os.fstat(fd), False, True)
        except BaseException:
            os.close(fd)
            raise
        return fd
    with _CurrentUser() as user, _PrivateAttributes(user) as sa:   # the access list applies only if the file is new
        handle = _win_open(path, _GENERIC_READ | _GENERIC_WRITE, _SHARE_READ | _SHARE_WRITE,
                           _OPEN_ALWAYS if create else _OPEN_EXISTING, _ATTRIBUTE_NORMAL, sa if create else None)
    try:
        _win_check_handle(handle, False, 'file', True)
    except BaseException:
        _win_close(handle)
        raise
    return _win_fd(handle)


# ---------------------------------------------------------------------------
# (e) A non-blocking exclusive lock on a private lock file. Agent messaging holds one for its lifetime.

def open_lock_file(path):
    """Open or create a private lock file (never through a link) and return its descriptor."""
    return open_private_rw(path, create=True)


def try_lock(fd):
    """Take an exclusive lock without waiting: True if held now, False if another holder has it."""
    if WIN:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)          # msvcrt locks bytes from the current position
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in (errno.EACCES, getattr(errno, 'EDEADLOCK', errno.EDEADLK)):
                return False
            raise
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


def unlock(fd):
    """Release a lock taken by ``try_lock`` (closing the descriptor also releases it)."""
    if WIN:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
