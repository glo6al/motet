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

Section (g) is the one folder walk every caller uses to open a path with no link
followed anywhere in it (``open_folder``, ``open_no_links``): by ``dir_fd`` on POSIX,
and on Windows by opening each name relative to the folder handle above it
(``NtCreateFile``), holding those folders without delete sharing.

Section (h) changes a held folder: create, open-or-create, open to append, make a folder, remove, stat a name and
flush, each by the held folder (``dir_fd`` on POSIX, relative ``NtCreateFile`` and the opened handle on Windows), never by path. It
also holds the descriptor checks ``read_only`` and ``unwritable`` (nobody but the owner can change it).
"""
from contextlib import contextmanager
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
TRUSTED_INSTALLER_SID = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'

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


# Rights that change a file or folder: write or append data (add a file or subfolder), extended attributes, delete
# a child, attributes, delete, the access list, the owner, and the generic forms that include them.
_WRITE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
_SYSTEM_WRITERS = (SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID)
_INHERIT_ONLY_ACE = 0x8


def acl_write_verdict(owner_sid, owner_is_user, has_dacl, aces, system_owned=False):
    """Raise unless nobody but the owner and the system can change this file or folder: the Windows form of the POSIX
    rule "owned by this user (or by root), and ``mode & 0o022 == 0``". It is for things that are not private but must
    not be changed by others, such as a project folder or the Git program.

    ``aces`` lists the DACL in order as (ace_type, ace_flags, access_mask, sid_string, is_current_user). The owner must
    be this user, or with ``system_owned`` SYSTEM, Administrators or TrustedInstaller. An allow entry that grants any
    change right may name only the owner's side (this user unless ``system_owned``, and OWNER RIGHTS), SYSTEM,
    Administrators or TrustedInstaller. Deny entries only take rights away, and inherit-only entries do not apply to
    the object itself, so both are passed over; any other entry type is refused. A missing list grants everyone
    everything and is refused.

    Like the POSIX rule, this refuses more than it may seem to: a folder made directly under a drive root (C:\\src,
    say) usually inherits "Authenticated Users: Modify" from the root, so every signed-in user can change it, and it
    is refused, as a POSIX folder with ``mode & 0o022`` would be. Folders under the user's profile inherit only the
    user, SYSTEM and Administrators, and pass."""
    if system_owned:
        if owner_sid not in _SYSTEM_WRITERS:
            raise PrivateStorageError('storage owner is not the system', 'owner')
    elif not owner_is_user:
        raise PrivateStorageError('nonprivate storage owner', 'owner')
    if not has_dacl:
        raise PrivateStorageError('storage others can change', 'access')
    for ace_type, flags, mask, sid, is_user in aces:
        if flags & _INHERIT_ONLY_ACE or ace_type == 1:       # ACCESS_DENIED_ACE_TYPE
            continue
        if ace_type != 0:
            raise PrivateStorageError('storage others can change', 'access')
        if not mask & _WRITE_RIGHTS:
            continue
        if sid in _SYSTEM_WRITERS or sid == OWNER_RIGHTS_SID or (is_user and not system_owned):
            continue
        raise PrivateStorageError('storage others can change', 'access')


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
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    kernel.GetFileInformationByHandleEx.restype = w.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = w.DWORD
    kernel.GetDriveTypeW.argtypes = [w.LPCWSTR]
    kernel.GetDriveTypeW.restype = w.UINT
    kernel.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
    kernel.DuplicateHandle.restype = w.BOOL
    kernel.FlushFileBuffers.argtypes = [w.HANDLE]
    kernel.FlushFileBuffers.restype = w.BOOL
    kernel.GetVolumeInformationByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD),
                                                     ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
    kernel.GetVolumeInformationByHandleW.restype = w.BOOL

    # NtCreateFile opens a name relative to an open folder handle: Windows' equivalent of openat(2) with dir_fd.
    nt = ctypes.WinDLL('ntdll')
    UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK = _nt_structures()
    u32 = ctypes.c_uint32
    nt.NtCreateFile.argtypes = [ctypes.POINTER(w.HANDLE), u32, ctypes.POINTER(OBJECT_ATTRIBUTES),
                                ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, u32, u32, u32, u32, ctypes.c_void_p, u32]
    nt.NtCreateFile.restype = ctypes.c_int32
    nt.NtQueryInformationFile.argtypes = [w.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, u32, ctypes.c_int]
    nt.NtQueryInformationFile.restype = ctypes.c_int32
    nt.structures = (UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK)
    _API = (adv, kernel, w, SA, FILEINFO, nt)
    return _API


_NT_STRUCTURES = None


def _nt_structures():
    """UNICODE_STRING, OBJECT_ATTRIBUTES and IO_STATUS_BLOCK for NtCreateFile, in fixed-width types (Windows' USHORT
    and ULONG are 16 and 32 bits everywhere), so their layout can be checked on any 64-bit system."""
    global _NT_STRUCTURES
    if _NT_STRUCTURES is None:
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_uint16), ('MaximumLength', ctypes.c_uint16), ('Buffer', ctypes.c_void_p)]

        class OBJECT_ATTRIBUTES(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_uint32), ('RootDirectory', ctypes.c_void_p),
                        ('ObjectName', ctypes.POINTER(UNICODE_STRING)), ('Attributes', ctypes.c_uint32),
                        ('SecurityDescriptor', ctypes.c_void_p), ('SecurityQualityOfService', ctypes.c_void_p)]

        class IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]

        _NT_STRUCTURES = (UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK)
    return _NT_STRUCTURES


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
# Data access. Windows records a handle's sharing (and so refuses a conflicting rename or delete) only for an open
# that asks for data, execute or delete access; READ_CONTROL and FILE_READ_ATTRIBUTES alone hold nothing.
_FILE_LIST_DIRECTORY, _FILE_TRAVERSE, _SYNCHRONIZE = 0x1, 0x20, 0x100000
# A hold asks for traversal (FILE_EXECUTE), the least data access that makes Windows apply its sharing; only a folder
# that will be listed also asks for FILE_LIST_DIRECTORY. While held, other programs cannot rename or delete the
# folder; holds last only as long as one open (or one held folder object), so this blocks them only briefly.
_HOLD_ACCESS = _READ_CONTROL | _FILE_READ_ATTRIBUTES | _FILE_TRAVERSE
_LIST_ACCESS = _HOLD_ACCESS | _FILE_LIST_DIRECTORY
# A folder that will be changed is held "writable", which also asks for FILE_ADD_FILE: the write-class right
# FlushFileBuffers needs on the handle to flush the folder. Nothing else is asked for. A relative create is checked
# against the folder's access list (not the rights of the handle it is made through), and a removal against DELETE on
# the file itself; FILE_DELETE_CHILD in particular would refuse a user who has Modify but not Full Control.
_FILE_ADD_FILE = 0x2
_WRITE_HOLD = _FILE_ADD_FILE
_FILE_DISPOSITION_INFO = 4     # FILE_INFO_BY_HANDLE_CLASS FileDispositionInfo
_FILE_DISPOSITION_INFO_EX = 21  # FileDispositionInfoEx, with the FILE_DISPOSITION_FLAG_* values below
_DISPOSITION_DELETE, _DISPOSITION_POSIX_SEMANTICS, _DISPOSITION_IGNORE_READONLY = 0x1, 0x2, 0x10
_READONLY = 0x1                # FILE_ATTRIBUTE_READONLY
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
    """Delete the file or folder this handle has open (it must hold DELETE access), then close it. The deletion is by
    handle, so it can only remove what was created, whatever its name points to by then."""
    try:
        _win_set_delete(handle)
    except PrivateStorageError:
        pass                                      # best effort: the refusal that led here is what the caller sees
    finally:
        _win_close(handle)


# Tests set this to False to exercise the older deletion on a system that has the newer one.
_POSIX_DELETE = True


def _win_set_delete(handle):
    """Mark what this handle has open for deletion (the handle needs DELETE access). Returns 'posix' when the name went
    at once (FileDispositionInfoEx with POSIX semantics: Windows 10 1709 and later, on NTFS), or 'legacy' when the name
    stays, refusing new opens, until the last handle on it closes (FileDispositionInfo, the fallback where the newer
    call is not supported). Raises PrivateStorageError('open') if none is accepted.

    The newer call is tried first with FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE (Windows 10 1809 and later), so
    a file someone marked read-only is still deleted, then without it. The older call cannot delete a read-only file:
    it fails, and the caller decides (``NoLinkFolder.remove`` then leaves the file and reports 'changed')."""
    kernel = _win_api()[1]
    if _POSIX_DELETE:
        for value in (_DISPOSITION_DELETE | _DISPOSITION_POSIX_SEMANTICS | _DISPOSITION_IGNORE_READONLY,
                      _DISPOSITION_DELETE | _DISPOSITION_POSIX_SEMANTICS):
            flags = ctypes.c_uint32(value)
            if kernel.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags),
                                                 ctypes.sizeof(flags)):
                return 'posix'
    flag = ctypes.c_ubyte(1)                      # FILE_DISPOSITION_INFO.DeleteFile = TRUE
    if kernel.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, ctypes.byref(flag), ctypes.sizeof(flag)):
        return 'legacy'
    raise _refuse('open')


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
                # Without FILE_SHARE_DELETE nobody can rename or remove the folder while it is held (briefly blocking
                # other programs). The open asks for traversal, without which Windows would not apply the sharing.
                folder_handle = _win_open(os.path.dirname(path) or '.', _HOLD_ACCESS,
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
    regular file with one name, or PrivateStorageError is raised. On POSIX a failed open raises the system's own
    OSError unchanged (ELOOP for a link, ENOENT for a missing file without ``create``). On Windows the open does
    not follow a reparse point, so a link or junction is opened itself and refused as 'link'; a failed open raises
    OSError('private storage open failed') carrying ``winerror``. The Windows handle does not share delete, so while
    it is open nobody can rename or replace the file behind it."""
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


def close_lock(fd):
    """Release a lock taken by ``try_lock`` and close its descriptor. Windows may hold a byte lock for a while after
    its file closes, so it is released first there; a lock that was never taken is simply closed."""
    if WIN:
        try:
            unlock(fd)
        except OSError:
            pass   # not held: closing is all that is left
    os.close(fd)


# ---------------------------------------------------------------------------
# (f) SQLite's companion files. SQLite makes its rollback journal by name next to the database.

def sqlite_companions_plain(path):
    """False if a rollback journal, -wal or -shm file next to ``path`` is a link, junction, not a plain file, or a
    file with a second name, so SQLite would write through it to somewhere else. A plain leftover journal is fine:
    SQLite needs it to roll back an interrupted write."""
    for suffix in ('-journal', '-wal', '-shm'):
        try:
            info = os.lstat(path + suffix)
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & REPARSE_POINT
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return False
    return True


def companion_verdict(owner_sid, owner_is_user, has_dacl, aces):
    """Raise unless a SQLite companion file's collected descriptor is acceptable on Windows.

    SQLite creates its journal by name with no security attributes of its own, so the file takes the creating
    token's default owner (Administrators when Motet runs elevated) and inherits the folder's access list (private,
    but not protected). So the owner may be this user, Administrators or SYSTEM, and the folder rule applies to the
    list: plain allow entries for this user, SYSTEM, Administrators or OWNER RIGHTS only, protected or not. ``aces``
    is as for ``acl_verdict``: (ace_type, sid_string, is_current_user)."""
    if not (owner_is_user or owner_sid in (SYSTEM_SID, ADMINISTRATORS_SID)):
        raise PrivateStorageError('nonprivate storage owner', 'owner')
    acl_verdict(True, has_dacl, False, aces, True)


def _win_companion_check(handle):
    adv, kernel, w = _win_api()[:3]
    with _CurrentUser() as user:
        owner, dacl, sd = w.LPVOID(), w.LPVOID(), w.LPVOID()
        if adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd)):
            raise _unavailable()
        try:
            aces = [(t, sid, is_user) for t, flags, mask, sid, is_user in (_read_aces(dacl, user) if dacl else [])]
            companion_verdict(_sid_text(owner), bool(adv.EqualSid(owner, user.sid)), bool(dacl), aces)
        finally:
            kernel.LocalFree(sd)


def sqlite_companions_private(path, folder=None):
    """``sqlite_companions_plain``, and each companion that exists must also be private.

    POSIX: the attachment store's own rule: a regular file with one name, owned by this user, mode exactly 0600
    (SQLite gives its journal the database's mode). Windows: each is opened relative to its folder without following
    a reparse point and checked on its handle: a plain file with one name, and ``companion_verdict`` (owner this
    user, Administrators or SYSTEM; the folder rule for the list, which need not be protected). A leftover journal
    someone opened up to others is refused. A companion that is being removed (delete pending, still open in a
    scanner, say) counts as absent; any other failure to open one refuses. On Windows ``folder`` may be the held
    NoLinkFolder for ``path``'s folder (the walk refused any reparse point and checked it is a folder when it opened
    it); the companions are then opened through that handle instead of opening the folder again by path."""
    if not WIN:
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                info = os.lstat(path + suffix)
            except FileNotFoundError:
                continue
            try:
                posix_verdict(info, False, True)
            except PrivateStorageError:
                return False
            if stat.S_IMODE(info.st_mode) != 0o600:
                return False
        return True
    folder_path, base = os.path.split(path)
    if folder is not None:
        folder._held()
        parent, owned = folder._handles[-1], False
    else:
        try:
            parent = _win_open(folder_path or '.', _HOLD_ACCESS, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE,
                               _OPEN_EXISTING, 0)
        except OSError:
            return False
        try:
            _win_kind(_win_facts(parent), True, 'folder')      # a plain folder, not a junction to one
        except PrivateStorageError:
            _win_close(parent)
            return False
        owned = True
    try:
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                handle = _win_child(parent, base + suffix, _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                                    _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, None)
            except OSError as e:
                status = getattr(e, 'ntstatus', 0)
                if status in _NT_NOT_FOUND or status == _NT_DELETE_PENDING:
                    continue
                return False
            try:
                _win_kind(_win_facts(handle), False, 'file', True)
                _win_companion_check(handle)
            except PrivateStorageError:
                return False
            finally:
                _win_close(handle)
        return True
    finally:
        if owned:
            _win_close(parent)


# ---------------------------------------------------------------------------
# (g) Open a folder or file with no link followed anywhere in its path.
#
# POSIX: each folder is opened from "/" by handle (``O_DIRECTORY | O_NOFOLLOW`` with ``dir_fd``), so a link in
# any position fails the open, and each next name is looked up in the folder already opened, never by path again.
#
# Windows does the same with handles. The drive root is opened by name, and refused unless it is the root of a
# local volume (not a network drive, and not a ``subst`` drive that stands for a folder somewhere else). Every
# name below it is opened with ``NtCreateFile`` relative to the folder handle above (``RootDirectory``), one
# component at a time, with ``FILE_OPEN_REPARSE_POINT``: a reparse point (symbolic link, junction, mount point,
# cloud placeholder or other) is opened itself, never followed, and refused on its handle, as is the wrong type.
# So a name is never looked up by path again, and swapping a folder on the way changes nothing already opened.
# Folders are also held while the walk goes on, with traversal access (and listing only where a folder is listed)
# and without ``FILE_SHARE_DELETE``, so nobody can rename or remove them meanwhile (Windows applies sharing only to
# opens that ask for data or execute access). This briefly blocks other programs from renaming or deleting them. The last name is checked on its handle (type, a single name, and with ``private``
# the owner and access list), then handed out as a C runtime descriptor. A held folder is listed through its
# handle (``GetFileInformationByHandleEx``), never by path. Opening by handle also means paths longer than 260
# characters work below the root.
#
# Windows paths must be plain drive paths (``C:\...``). UNC shares (``\\server\share``) and device paths
# (``\\?\``, ``\\.\``) are refused: they skip or change Win32's normal path handling, and Motet's own folders do
# not need them. So are "." and "..", names Win32 would rewrite (a trailing dot or space), and names with ":",
# which open an alternate data stream. These shapes are checked on the path as given, before ``abspath`` (which
# on real Windows would silently drop a trailing dot, for example), so every Windows version refuses the same
# paths. A path whose last name is a reserved device name (CON, NUL and the like, with any extension) is refused
# for the same reason; names below a held folder need no device rule, since relative NT opens never treat them as
# devices.

_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)


def folder_walk_available():
    """True where folders can be opened by handle without following links: POSIX with ``dir_fd``, or Windows once
    the Win32 calls and the C runtime's handle conversion load (loaded on first use, never at import)."""
    if WIN:
        try:
            _win_api()
            import msvcrt
        except (OSError, AttributeError, ImportError):
            return False
        return hasattr(msvcrt, 'open_osfhandle')
    return (os.name == 'posix' and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW')
            and os.open in os.supports_dir_fd)


def _plain_name(name, windows):
    """One path component naming a child: never the folder itself, its parent, or (on Windows) a data stream."""
    if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\0' in name:
        return False
    if windows:
        # No device-name rule: below the drive root every name is opened relative to a folder handle, where the NT
        # layer never maps CON, NUL and the like to devices; and a whole path that Win32's abspath turns into a
        # device path (\\.\CON) is refused by _win_parts.
        return not ('\\' in name or ':' in name or name[-1] in '. ')
    return os.sep not in name


def _win_parts(path):
    """The drive root and the names below it of a plain absolute drive path; anything else is refused.

    ``C:\\Users\\example`` gives ``('C:\\', ['Users', 'example'])`` and ``D:\\`` gives ``('D:\\', [])``."""
    import ntpath
    if not isinstance(path, str) or '\0' in path:
        raise _refuse('unavailable', 'folder')
    path = path.replace('/', '\\')
    drive, rest = ntpath.splitdrive(path)
    if (path.startswith('\\\\') or len(drive) != 2 or drive[1] != ':' or not ('A' <= drive[0].upper() <= 'Z')
            or not rest.startswith('\\')):
        raise _refuse('unavailable', 'folder')       # UNC, device (\\?\, \\.\) and drive-relative paths
    names = rest[1:].split('\\') if rest != '\\' else []
    if names and names[-1] == '':
        names.pop()                                  # one trailing separator
    if not all(_plain_name(name, True) for name in names):
        raise _refuse('unavailable', 'folder')
    return drive + '\\', names


def _win_facts(handle):
    """Attributes, link count and file type of an opened handle."""
    kernel, w, SA, FILEINFO = _win_api()[1:5]
    info = FILEINFO()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _unavailable()
    return info.attributes, info.links, kernel.GetFileType(handle)


def _win_kind(facts, directory, subject, single_link=False):
    """Refuse a reparse point, the wrong type (a device is not a file), or a file with a second name."""
    attributes, links, file_type = facts
    if attributes & REPARSE_POINT:
        raise _refuse('link', subject)
    if bool(attributes & DIRECTORY) != directory or (not directory and file_type != _FILE_TYPE_DISK):
        raise _refuse('not_regular', subject)
    if single_link and not directory and links != 1:
        raise _refuse('links', subject)


_NT_FILE_OPEN, _NT_FILE_CREATE, _NT_FILE_OPEN_IF = 1, 2, 3     # NtCreateFile CreateDisposition
_NT_DIRECTORY_FILE, _NT_SYNCHRONOUS_IO_NONALERT, _NT_NON_DIRECTORY_FILE, _NT_OPEN_REPARSE_POINT = 0x1, 0x20, 0x40, 0x200000
_OBJ_CASE_INSENSITIVE = 0x40
_NT_NOT_FOUND = (0xC0000034, 0xC000003A)        # STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND
_NT_WRONG_TYPE = (0xC0000103, 0xC00000BA)       # STATUS_NOT_A_DIRECTORY, STATUS_FILE_IS_A_DIRECTORY
_NT_COLLISION = 0xC0000035                      # STATUS_OBJECT_NAME_COLLISION: the name exists
_NT_DELETE_PENDING = 0xC0000056                 # STATUS_DELETE_PENDING: removed, but still open somewhere
_NT_SHARING_VIOLATION = 0xC0000043
_NT_ACCESS_DENIED = 0xC0000022
_DRIVE_REMOTE = 4


def _missing(subject):
    error = FileNotFoundError(errno.ENOENT, 'private storage: %s missing' % subject)
    error.subject = subject
    return error


def _win_child(parent, name, access, share, directory, disposition=_NT_FILE_OPEN, options=0, descriptor=None,
               information=None):
    """Open ``name`` (one component) inside the folder ``parent`` is a handle to, never following a reparse point
    there. ``directory`` True asks for a folder, False for anything but a folder, None for either. ``disposition``
    is NtCreateFile's (open, create, open-or-create); ``options`` adds create options; ``descriptor`` is the security
    descriptor a new file or folder gets (ignored when the name exists). A list given as ``information`` receives
    what the open did (``_FILE_OPENED`` or ``_FILE_CREATED``). Raises OSError carrying ``ntstatus``."""
    adv, kernel, w, SA, FILEINFO, nt = _win_api()
    UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK = nt.structures
    raw = name.encode('utf-16-le')
    buffer = ctypes.create_string_buffer(raw + b'\0\0')
    text = UNICODE_STRING(len(raw), len(raw) + 2, ctypes.cast(buffer, w.LPVOID))
    attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), parent, ctypes.pointer(text), _OBJ_CASE_INSENSITIVE,
                                   descriptor, None)
    handle, status_block = w.HANDLE(), IO_STATUS_BLOCK()
    kind = 0 if directory is None else _NT_DIRECTORY_FILE if directory else _NT_NON_DIRECTORY_FILE
    options |= kind | _NT_SYNCHRONOUS_IO_NONALERT | _NT_OPEN_REPARSE_POINT
    status = nt.NtCreateFile(ctypes.byref(handle), access | _SYNCHRONIZE, ctypes.byref(attributes), ctypes.byref(status_block),
                             None, 0, share, disposition, options, None, 0) & 0xFFFFFFFF
    if status >= 0x80000000 or not handle.value:     # NT_ERROR / NT_WARNING
        if handle.value:
            _win_close(handle.value)
        error = OSError('private storage open failed')
        error.ntstatus = status
        raise error
    if information is not None:
        information.append(status_block.Information)
    return handle.value


_FILE_OPENED, _FILE_CREATED = 1, 2     # IO_STATUS_BLOCK.Information after NtCreateFile


def _win_root_verified(handle, root):
    """Refuse a drive whose root is not the root of a local volume: a network drive, or a ``subst`` drive standing for
    a folder (whose path could pass through links that this walk never saw)."""
    kernel, w = _win_api()[1:3]
    if kernel.GetDriveTypeW(root) == _DRIVE_REMOTE:
        raise _refuse('unavailable', 'folder')
    buffer = ctypes.create_unicode_buffer(1024)
    length = kernel.GetFinalPathNameByHandleW(handle, buffer, 1024, 0x4)   # FILE_NAME_NORMALIZED | VOLUME_NAME_NONE
    if not length or length >= 1024 or buffer.value != '\\':
        raise _refuse('unavailable', 'folder')


def _win_list(handle):
    """The names in the folder this handle is open on, read through the handle (FILE_FULL_DIR_INFO records)."""
    kernel = _win_api()[1]
    names, info_class, size = [], 15, 65536            # FileFullDirectoryRestartInfo, then FileFullDirectoryInfo
    buffer = (ctypes.c_ulonglong * (size // 8))()       # the records need 8-byte alignment
    while True:
        if not kernel.GetFileInformationByHandleEx(handle, info_class, buffer, size):
            if ctypes.get_last_error() == 18:          # ERROR_NO_MORE_FILES
                return names
            raise _unavailable('folder')
        info_class = 14
        names.extend(_full_dir_names(bytes(buffer)))


def _full_dir_names(raw):
    """The names in one buffer of FILE_FULL_DIR_INFO records, without "." and "..". Each record: NextEntryOffset
    (ULONG, 0 on the last), FileIndex, six LARGE_INTEGER times and sizes, FileAttributes at 56, FileNameLength (bytes)
    at 60, EaSize at 64, then the UTF-16 name at 68. A record that runs past the buffer is refused."""
    names, offset = [], 0
    while True:
        if offset + 68 > len(raw):
            raise _unavailable('folder')
        following = int.from_bytes(raw[offset:offset + 4], 'little')
        length = int.from_bytes(raw[offset + 60:offset + 64], 'little')
        if offset + 68 + length > len(raw) or length % 2 or (following and following < 68 + length):
            raise _unavailable('folder')
        name = raw[offset + 68:offset + 68 + length].decode('utf-16-le', 'surrogatepass')
        if name not in ('.', '..'):
            names.append(name)
        if not following:
            return names
        offset += following


def _win_checked(handle, directory, subject, single_link=False, private=False):
    """Check an opened handle (type, reparse point, one name, and with ``private`` the owner and access list);
    closes it and raises on refusal."""
    try:
        _win_kind(_win_facts(handle), directory, subject, single_link)
        if private:
            _win_private(handle, directory, subject)
    except BaseException:
        _win_close(handle)
        raise
    return handle


def _win_private(handle, directory, subject):
    """The owner and access-list rule on an opened handle, refused as ``subject``."""
    try:
        windows_handle_security(handle, directory)
    except PrivateStorageError as e:
        raise PrivateStorageError(str(e), e.reason, subject) from None


def _hold_access(listing, writable):
    return (_LIST_ACCESS if listing else _HOLD_ACCESS) | (_WRITE_HOLD if writable else 0)


def _win_open_root(root, private=False, listing=False, writable=False):
    """The drive root, held, checked and verified to be a local volume's root."""
    try:
        handle = _win_open(root, _hold_access(listing, writable), _SHARE_READ | _SHARE_WRITE, _OPEN_EXISTING, 0)
    except OSError as e:
        if getattr(e, 'winerror', 0) in _NOT_FOUND:
            raise _missing('folder') from None
        raise _refuse('open', 'folder') from None
    _win_checked(handle, True, 'folder', private=private)
    try:
        _win_root_verified(handle, root)
    except BaseException:
        _win_close(handle)
        raise
    return handle


def _win_open_checked(parent, name, directory, subject, single_link=False, private=False, listing=False, writable=False):
    """Open the child ``name`` of the held folder ``parent`` and check it. A folder is held (data access, no delete
    sharing); a file shares everything, since its handle is what is read. A missing name (or one being removed, still
    open elsewhere) raises FileNotFoundError; the wrong type ``not_regular``; any other failed open
    PrivateStorageError('open')."""
    if directory:   # held: no delete sharing, which briefly blocks other programs renaming or deleting it
        access, share = _hold_access(listing, writable), _SHARE_READ | _SHARE_WRITE
    else:
        access, share = _GENERIC_READ, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE
    try:
        handle = _win_child(parent, name, access, share, directory)
    except OSError as e:
        status = getattr(e, 'ntstatus', 0)
        if status in _NT_NOT_FOUND or status == _NT_DELETE_PENDING:
            raise _missing(subject) from None
        raise _refuse('not_regular' if status in _NT_WRONG_TYPE else 'open', subject) from None
    return _win_checked(handle, directory, subject, single_link, private)


def _posix_kind(info, directory, subject, single_link=False, private=False):
    if private:
        posix_verdict(info, directory, single_link, subject)
    elif directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise _refuse('not_regular', subject)
    elif single_link and not directory and info.st_nlink != 1:
        raise _refuse('links', subject)


class NoLinkFolder:
    """A folder opened with no link followed on the way, held open until ``close()``; use it with ``with``.

    ``open`` and ``folder`` open a child by name through this held folder, and ``names`` lists it. On Windows a
    folder from ``folder()`` relies on its parent's hold, so it refuses to work once its parent is closed.

    A folder opened with ``writable=True`` can also be changed through (section h): ``create``, ``open_or_create``,
    ``open_append``, ``mkdir``, ``remove`` and ``sync``. ``child_stat`` reads a name without following it."""

    def __init__(self, path, windows, fd=None, handles=None, parent=None, listing=True, writable=False):
        self.path, self._windows, self._fd, self._handles, self._parent = path, windows, fd, handles or [], parent
        self._listing, self._writable = listing, writable
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._windows:
            handles, self._handles = self._handles, []
            for handle in reversed(handles):
                _win_close(handle)
        else:
            os.close(self._fd)

    def _held(self):
        folder = self
        while folder is not None:
            if folder.closed:
                raise _refuse('changed', 'folder')
            folder = folder._parent

    def stat(self):
        """The held folder's own stat result, read through its handle."""
        self._held()
        if not self._windows:
            return os.fstat(self._fd)
        return _win_fstat(self._handles[-1])

    def names(self):
        """The names in this folder, read through its handle while it is held. On Windows the folder must have been
        opened with ``listing=True`` (holds otherwise ask only for traversal)."""
        self._held()
        if not self._windows:
            return os.listdir(self._fd)
        if not self._listing:
            raise _refuse('unavailable', 'folder')
        return _win_list(self._handles[-1])

    def open(self, name, directory=False, single_link=False, private=False, subject=None):
        """A read-only descriptor for the child ``name``: a regular file (with one name only, if ``single_link``),
        or with ``directory`` a folder. ``private`` also requires this user as owner and a private mode or access
        list. A missing child raises FileNotFoundError; on POSIX a link raises the system's own OSError (ELOOP)."""
        subject = subject or ('folder' if directory else 'file')
        self._held()
        if not _plain_name(name, self._windows):
            raise _refuse('unavailable', subject)
        if self._windows:
            # Binary, so a read returns the bytes as they are (no CRLF change, no stop at Ctrl-Z).
            return _win_fd(_win_open_checked(self._handles[-1], name, directory, subject, single_link, private),
                           os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        flags = os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
        fd = os.open(name, flags, dir_fd=self._fd)      # non-blocking: a pipe cannot hang the open
        try:
            _posix_kind(os.fstat(fd), directory, subject, single_link, private)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def folder(self, name, private=False, listing=False, writable=False, alone=False):
        """The child folder ``name``, held like this one; close it before this one. ``listing`` lets ``names()`` list
        it on Windows; ``writable`` lets it be changed through (section h). ``alone`` makes a hold that may outlive
        this folder (see ``open_folder``)."""
        self._held()
        if not _plain_name(name, self._windows):
            raise _refuse('unavailable', 'folder')
        if self._windows:
            handle = _win_open_checked(self._handles[-1], name, True, 'folder', private=private, listing=listing,
                                       writable=writable)
            return NoLinkFolder(self._child(name), True, handles=[handle], parent=None if alone else self,
                                listing=listing, writable=writable)
        return NoLinkFolder(os.path.join(self.path, name), False, fd=self.open(name, True, private=private),
                            writable=writable)

    # (h) Changing a held folder. Each call names one child of this folder and acts through the held folder, never by
    # path. On POSIX each opens with the same flags as the ``dir_fd`` call the attachment code makes today and refuses
    # at least what that code refuses; where a method does more (a mode set regardless of umask, a refused new file
    # removed again, an open that cannot hang), its docstring says so.

    def _changeable(self, name, subject='file'):
        """A held, writable folder and a plain child name, or a refusal before anything is opened."""
        self._held()
        if not self._writable:
            raise _refuse('unavailable', 'folder')       # open the folder with writable=True to change it
        if not _plain_name(name, self._windows):
            raise _refuse('unavailable', subject)

    def create(self, name):
        """Create the new regular file ``name`` here, private from the start, and return a descriptor open for reading
        and writing; the caller closes it. The name must not exist (a link there counts as existing): an existing
        name raises FileExistsError. A new file whose check fails is removed again before the refusal is raised.

        POSIX: ``os.open(name, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600, dir_fd=)``, then
        ``fchmod(0o600)`` as ``create_private_file`` does (so the mode does not depend on the umask) and the private
        rule on ``fstat`` (one name). Windows: a relative ``NtCreateFile`` that only creates, with a protected
        this-user/SYSTEM access list, never following a reparse point. The handle can delete its own file (used only
        to remove a refused one) and does not share delete, so nobody can rename or remove the file while it is open."""
        self._changeable(name)
        if self._windows:
            return _win_create(self._handles[-1], name)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o600, dir_fd=self._fd)
        try:
            os.fchmod(fd, 0o600)
            posix_verdict(os.fstat(fd), False, True)
        except BaseException:
            _posix_discard_at(fd, name, self._fd)
            raise
        return fd

    def open_or_create(self, name):
        """Open the regular file ``name`` here for reading and writing, creating it private from the start if it is
        missing, and return its descriptor; the caller closes it. An existing file is only checked, never repaired:
        it must be a private regular file with one name (lock files and databases).

        POSIX: ``os.open(name, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600, dir_fd=)`` (a link raises the system's
        own OSError, ELOOP), then the private rule on ``fstat``. Windows: a relative open-or-create with the private
        access list for a new file; a reparse point at the name is opened itself and refused as 'link'. The handle
        shares reading and writing but not delete, like ``open_private_rw``, and has no DELETE access (which would
        refuse every later open that does not share delete, such as SQLite's). So a new file that is refused is
        removed by name after its handle closes, only while the name still has that file's ID. A name whose removal
        is pending, still open elsewhere, cannot be made again until it is gone, and is refused as 'changed'."""
        self._changeable(name)
        if self._windows:
            return _win_open_or_create(self._handles[-1], name)
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | _CLOEXEC, 0o600, dir_fd=self._fd)
        try:
            posix_verdict(os.fstat(fd), False, True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_append(self, name, create=True):
        """Open the existing regular file ``name`` here to read it and append to it, or with ``create`` make it if it is
        missing, and return ``(fd, created)``; the caller closes ``fd``. The file need not be private (Git's
        ``.git/info/exclude``, for one), but nobody but its owner may change it: it must be a regular file with one
        name that passes ``unwritable`` (owned by this user, and on POSIX ``mode & 0o022 == 0``). A link is refused.

        POSIX, as the exclusion code opens it today: ``os.open(name, O_RDWR | O_APPEND | O_NOFOLLOW | O_CLOEXEC,
        dir_fd=)``; if that name is missing, ``O_RDWR | O_APPEND | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600``
        (so a name that appears meanwhile raises FileExistsError; a folder raises the system's own
        IsADirectoryError). Then the type, one name, owner and mode on ``fstat``. Windows: a relative open with read and append-only data access (writes go to the end whatever the
        offset), sharing read, write and delete as other programs such as Git expect, never following a reparse
        point; a missing name is created with the private access list. Then the type, one name and
        ``acl_write_verdict`` on the handle. On both, a refused file this call created is removed again."""
        self._changeable(name)
        if self._windows:
            return _win_open_append(self._handles[-1], name, create)
        created = False
        try:
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | _CLOEXEC, dir_fd=self._fd)
        except FileNotFoundError:
            if not create:
                raise
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o600,
                         dir_fd=self._fd)
            created = True
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _refuse('not_regular')
            if info.st_nlink != 1:
                raise _refuse('links')
            _posix_unwritable(info, False, 'file')
        except BaseException:
            if created:
                _posix_discard_at(fd, name, self._fd)
            else:
                os.close(fd)
            raise
        return fd, created

    def mkdir(self, name):
        """Make the new folder ``name`` here, private from the start; an existing name raises FileExistsError. Open it
        afterwards with ``folder(name, ...)``.

        POSIX: ``os.mkdir(name, 0o700, dir_fd=)``. Windows: a relative ``NtCreateFile`` that only creates a folder,
        with the protected this-user/SYSTEM access list, checked on its handle; a refused new folder is removed
        through that handle."""
        self._changeable(name, 'folder')
        if self._windows:
            _win_mkdir(self._handles[-1], name)
            return
        os.mkdir(name, 0o700, dir_fd=self._fd)

    def remove(self, name, expect):
        """Remove the regular file ``name`` here if it is the file ``expect`` recognises, and say what happened.

        ``expect(fd)`` is called with a read-only descriptor of the opened file (it may ``fstat`` and read it, and
        must not close it) and returns True for the file to remove. Returns:

        - 'removed': it was removed;
        - 'absent': there is no such name (on Windows also once an earlier, pending removal has finished);
        - 'changed': a link, something other than a regular file (a folder, pipe, socket or device), or a file
          ``expect`` did not recognise; nothing was removed. On Windows also a file marked read-only where only the
          older deletion is available, which cannot delete it;
        - 'busy' (Windows only): another program has the file open without sharing delete, or a removal is waiting
          for other programs to close it, so the name is still there; try again later.

        POSIX: open with ``O_NOFOLLOW | O_NONBLOCK`` by ``dir_fd`` (a pipe cannot hang it), ask ``expect``, check the
        name still names that same file (``stat`` with ``follow_symlinks=False``), then ``os.unlink(name, dir_fd=)``.
        POSIX checks and then unlinks by name, so a swap by the same user between those two steps is not caught;
        Windows has no such gap. Windows: open the name with DELETE access, sharing only reading (so no writer
        meanwhile), give ``expect`` a read-only duplicate, then delete through the handle that was checked, so only
        that file can be removed. The name goes at once where Windows supports POSIX deletion; where it falls back,
        the name stays until the last handle on it closes, and the result is 'busy' until then. Call ``sync()``
        afterwards to make the removal durable."""
        self._changeable(name)
        if self._windows:
            return _win_remove(self._handles[-1], name, expect)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=self._fd)
        except FileNotFoundError:
            return 'absent'
        except OSError as e:
            if e.errno in _NOT_A_FILE_OPEN:              # a link, socket or device at the name
                return 'changed'
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or not expect(fd):
                return 'changed'
            mine = os.fstat(fd)
            try:
                if not os.path.samestat(os.stat(name, dir_fd=self._fd, follow_symlinks=False), mine):
                    return 'changed'
                os.unlink(name, dir_fd=self._fd)
            except FileNotFoundError:
                return 'absent'
            return 'removed'
        finally:
            os.close(fd)

    def child_stat(self, name):
        """The stat result of the name ``name`` here, not following it (a link is reported as itself). A missing name
        raises FileNotFoundError. POSIX: ``os.stat(name, dir_fd=, follow_symlinks=False)``. Windows: the name is
        opened relative to the held folder with attribute access only, never following a reparse point, and read
        with ``fstat``; a name whose removal is pending counts as missing. Windows' ``fstat`` gives a reparse point
        the type of what it stands for (a file symbolic link reads as a file, a junction as a folder), so any reparse
        point is reported here as a link (``S_IFLNK``), as ``lstat`` reports a POSIX link; ``st_file_attributes``
        still carries the reparse flag."""
        self._held()
        if not _plain_name(name, self._windows):
            raise _refuse('unavailable')
        if not self._windows:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        try:
            handle = _win_child(self._handles[-1], name, _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                                _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, None)
        except OSError as e:
            status = getattr(e, 'ntstatus', 0)
            if status in _NT_NOT_FOUND or status == _NT_DELETE_PENDING:
                raise _missing('file') from None
            raise _refuse('open') from None
        try:
            info = _win_fstat(handle)
            return _as_link(info) if _win_facts(handle)[0] & REPARSE_POINT else info
        finally:
            _win_close(handle)

    def sync(self):
        """Flush this folder's own entries to disk, after a create or remove in it. POSIX: ``os.fsync`` on the folder.
        Windows: ``FlushFileBuffers`` on the held handle, which needs the write access a ``writable`` hold asks for."""
        self._held()
        if not self._writable:
            raise _refuse('unavailable', 'folder')
        if not self._windows:
            os.fsync(self._fd)
            return
        _win_flush(self._handles[-1])

    def file_system(self):
        """Windows: the name of the file system this folder is on ('NTFS', 'ReFS', 'FAT32' and so on), read through
        its handle. POSIX: None (nothing here depends on it)."""
        self._held()
        return _win_file_system(self._handles[-1]) if self._windows else None

    def unwritable(self, system_owned=False):
        """``unwritable`` for this held folder: nobody but its owner (and the system) can change it. POSIX: the owner
        is this user (or root with ``system_owned``) and ``mode & 0o022 == 0``, on ``fstat``. Windows:
        ``acl_write_verdict`` on the held handle's owner and access list (a hold has READ_CONTROL)."""
        self._held()
        if not self._windows:
            _posix_unwritable(os.fstat(self._fd), system_owned, 'folder')
            return
        try:
            windows_handle_unwritable(self._handles[-1], system_owned)
        except PrivateStorageError as e:
            raise PrivateStorageError(str(e), e.reason, 'folder') from None

    def identity(self):
        """This held folder's ``file_identity`` (on Windows read from its handle)."""
        self._held()
        return _win_identity(self._handles[-1]) if self._windows else _posix_identity(os.fstat(self._fd))

    def _child(self, name):
        return self.path + name if self.path.endswith('\\') else self.path + '\\' + name


def _win_duplicate(handle, access=None):
    """A second handle on the same folder or file, for a descriptor that closes on its own: with the same access, or
    with ``access`` (which must be part of what the handle already has)."""
    kernel, w = _win_api()[1:3]
    process, copy = kernel.GetCurrentProcess(), w.HANDLE()
    if access is None:
        done = kernel.DuplicateHandle(process, handle, process, ctypes.byref(copy), 0, False, 2)   # DUPLICATE_SAME_ACCESS
    else:
        done = kernel.DuplicateHandle(process, handle, process, ctypes.byref(copy), access, False, 0)
    if not done:
        raise _unavailable('folder')
    return copy.value


def _win_fstat(handle):
    """``os.fstat`` of what ``handle`` has open, through a duplicate descriptor; ``handle`` stays open."""
    fd = _win_fd(_win_duplicate(handle), os.O_RDONLY)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _path_module():
    """Windows path rules on Windows (also when a test simulates it), the system's own elsewhere."""
    if WIN:
        import ntpath
        return ntpath
    return os.path


def _win_raw_refused(path):
    """Refuse a Windows path by its shape as given, before ``abspath`` rewrites it, so the answer does not depend on
    which Windows version normalises it how. Refused: UNC and device prefixes; a ":" anywhere but after a drive
    letter (a data stream); a name ending in a dot or space (Win32 silently drops those, so another name would be
    opened); and a last name that is a reserved device name, with or without an extension (some Windows versions
    turn it into a device path). "." and ".." are left to ``abspath``, which resolves them as Windows does."""
    import ntpath
    if not isinstance(path, str) or '\0' in path:
        return True
    path = path.replace('/', '\\')
    if path.startswith('\\\\'):
        return True
    drive, rest = ntpath.splitdrive(path)
    if drive and (len(drive) != 2 or drive[1] != ':'):
        return True
    names = [name for name in rest.split('\\') if name]
    for name in names:
        if name not in ('.', '..') and (':' in name or name[-1] in '. '):
            return True
    if names and names[-1].split('.', 1)[0].rstrip(' ').upper() in _DEVICE_NAMES:
        return True
    return False


_DEVICE_NAMES = frozenset(['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'] + ['COM%d' % n for n in range(1, 10)]
                          + ['LPT%d' % n for n in range(1, 10)])


def _absolute(path):
    """``path`` made absolute; on Windows its raw shape is checked first (see ``_win_raw_refused``)."""
    if WIN and _win_raw_refused(path):
        raise _refuse('unavailable', 'folder')
    return _path_module().abspath(path)


def open_folder(path, private=False, listing=False, writable=False, alone=False):
    """Hold the folder at ``path`` (made absolute) with no link followed anywhere on the way; the caller closes
    it. ``private`` checks the folder itself (owner, and mode or access list); ``listing`` lets ``names()`` list it on
    Windows; ``writable`` lets it be changed through (section h), and on Windows asks for that access on its hold
    only. A missing folder raises FileNotFoundError; on POSIX a link raises the system's own OSError (ELOOP).

    ``alone`` is for a hold kept for a long time (attachment storage keeps its folders for its lifetime): on Windows
    the folders above are let go once this one is open, so only this folder's own handle is kept. NTFS refuses to
    rename or remove a folder while anything below it is open (the WindowsWalk tests check this on real Windows),
    so that one handle still keeps every folder above it in place, without blocking them all for as long. On POSIX a
    held folder is one descriptor either way."""
    path = _absolute(path)
    if WIN:
        root, names = _win_parts(path)
        handles, current = [], root
        try:
            handles.append(_win_open_root(root, private=private and not names, listing=listing and not names,
                                          writable=writable and not names))
            for n, name in enumerate(names):
                last = n == len(names) - 1
                current = current + name if current.endswith('\\') else current + '\\' + name
                handles.append(_win_open_checked(handles[-1], name, True, 'folder', private=private and last,
                                                 listing=listing and last, writable=writable and last))
        except BaseException:
            for handle in reversed(handles):
                _win_close(handle)
            raise
        if alone:
            for handle in reversed(handles[:-1]):
                _win_close(handle)
            handles = handles[-1:]
        return NoLinkFolder(current, True, handles=handles, listing=listing, writable=writable)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        for part in path.split(os.sep):
            if part:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
        if private:
            posix_verdict(os.fstat(fd), True, subject='folder')
    except BaseException:
        os.close(fd)
        raise
    return NoLinkFolder(path, False, fd=fd, writable=writable)


def anchored(path):
    """``path`` with the folders above its last name resolved once (``realpath``), for a walk over Motet's own
    folders on Windows. Those folders are the user's profile and above, which are often moved with a junction; they
    are resolved here, then the walk opens the resolved path by handle and holds it, so nothing in it can change
    afterwards, and a link or junction at the last name itself is still refused."""
    module = _path_module()
    path = _absolute(path)
    return module.join(module.realpath(module.dirname(path)), module.basename(path))


def open_no_links(path, directory=False, single_link=False, private=False):
    """A read-only descriptor for ``path`` with no link followed anywhere in it; the caller closes it.

    A regular file by default (``single_link`` refuses a second name), or with ``directory`` the folder itself (on
    Windows that descriptor holds the folder and serves ``os.fstat``; to open its children use ``open_folder``).
    ``private`` also checks the opened file or folder's owner, and mode or access list. A missing name raises
    FileNotFoundError and refusals raise PrivateStorageError; on POSIX a link raises the system's own OSError."""
    path = _absolute(path)
    if directory:
        folder = open_folder(path, private)
        if folder._windows:
            handle = folder._handles.pop()           # the folder's own handle becomes the descriptor
            folder.close()                           # and its ancestors are released
            return _win_fd(handle, os.O_RDONLY)
        fd, folder._fd, folder.closed = folder._fd, None, True
        return fd
    parent, name = _path_module().split(path)
    with open_folder(parent) as folder:
        return folder.open(name, single_link=single_link, private=private)


# ---------------------------------------------------------------------------
# (h) Changing a held folder (``NoLinkFolder.create``, ``open_or_create``, ``open_append``, ``mkdir``, ``remove``,
# ``child_stat``, ``sync``), and two checks on an open descriptor (``read_only``, ``unwritable``).
#
# On Windows every change is one relative ``NtCreateFile`` under the held folder handle, with
# ``FILE_OPEN_REPARSE_POINT``, so no name is looked up by path and no reparse point is followed. New files and folders
# get the protected this-user/SYSTEM access list in the call that makes them, and are checked on their own handle; a
# refused new one is deleted through that handle. A removal opens the file with DELETE access, lets the caller check
# it on that handle, then deletes through the same handle (FileDispositionInfoEx with POSIX semantics, or the older
# FileDispositionInfo where that is not supported), so only the file that was checked can go. Changes go through a
# hold opened with ``writable=True``, which on Windows also asks for FILE_ADD_FILE on that one folder (the right
# FlushFileBuffers needs). ``open_append`` is for a file that is not private but that nobody else may change, such
# as Git's exclude file: it opens for appending and applies ``unwritable`` instead of the private rule.

def _posix_discard_at(fd, name, dir_fd):
    """Close a file just created as ``name`` in ``dir_fd`` and remove that name, but only while it names this file."""
    try:
        mine = os.fstat(fd)
    except OSError:
        mine = None
    os.close(fd)
    try:
        if mine is not None and os.path.samestat(os.stat(name, dir_fd=dir_fd, follow_symlinks=False), mine):
            os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


@contextmanager
def _private_descriptor():
    """The protected this-user/SYSTEM security descriptor a new file or folder gets, while it is needed."""
    with _CurrentUser() as user, _PrivateAttributes(user) as sa:
        yield sa.descriptor


def _created_error(error, subject):
    """The refusal for a failed relative create (FILE_CREATE): any existing name is FileExistsError, as POSIX's
    ``O_CREAT | O_EXCL`` or ``mkdir`` gives EEXIST for any existing name, links included. Windows reports some of
    them by their type instead of a collision: creating a file over a folder or a junction gives
    STATUS_FILE_IS_A_DIRECTORY, and a folder over a file STATUS_NOT_A_DIRECTORY. A name still being removed, open
    elsewhere, exists too."""
    status = getattr(error, 'ntstatus', 0)
    if status in (_NT_COLLISION, _NT_DELETE_PENDING) or status in _NT_WRONG_TYPE:
        exists = FileExistsError(errno.EEXIST, 'private storage: %s exists' % subject)
        exists.ntstatus = status
        return exists
    return _refuse('open', subject)


def _win_create(parent, name):
    with _private_descriptor() as descriptor:
        try:
            handle = _win_child(parent, name, _GENERIC_READ | _GENERIC_WRITE | _DELETE, _SHARE_READ | _SHARE_WRITE, False,
                                _NT_FILE_CREATE, descriptor=descriptor)
        except OSError as e:
            raise _created_error(e, 'file') from None
    try:
        _win_kind(_win_facts(handle), False, 'file', True)
        _win_private(handle, False, 'file')
    except BaseException:
        _win_discard(handle)
        raise
    return _win_fd(handle, getattr(os, 'O_BINARY', 0))


def _win_open_or_create(parent, name):
    outcome = []
    with _private_descriptor() as descriptor:
        try:
            handle = _win_child(parent, name, _GENERIC_READ | _GENERIC_WRITE, _SHARE_READ | _SHARE_WRITE, False,
                                _NT_FILE_OPEN_IF, descriptor=descriptor, information=outcome)
        except OSError as e:
            status = getattr(e, 'ntstatus', 0)
            if status == _NT_DELETE_PENDING:
                raise _refuse('changed') from None       # being removed, still open elsewhere: not made again yet
            raise _refuse('not_regular' if status in _NT_WRONG_TYPE else 'open') from None

    def check(opened):
        _win_kind(_win_facts(opened), False, 'file', True)
        _win_private(opened, False, 'file')
    _win_check_or_undo(parent, name, handle, outcome == [_FILE_CREATED], check)
    return _win_fd(handle, getattr(os, 'O_BINARY', 0))


_FILE_READ_DATA, _FILE_APPEND_DATA = 0x1, 0x4
_FILE_GENERIC_READ = 0x120089      # READ_CONTROL, read data, attributes and extended attributes, SYNCHRONIZE
_APPEND_ACCESS = _FILE_READ_DATA | _FILE_APPEND_DATA | _FILE_READ_ATTRIBUTES | _READ_CONTROL


def _win_open_append(parent, name, create):
    share, created = _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, False
    try:
        handle = _win_child(parent, name, _APPEND_ACCESS, share, False)
    except OSError as e:
        status = getattr(e, 'ntstatus', 0)
        if status in _NT_NOT_FOUND and create:
            with _private_descriptor() as descriptor:
                try:
                    handle = _win_child(parent, name, _APPEND_ACCESS, share, False, _NT_FILE_CREATE, descriptor=descriptor)
                except OSError as again:
                    raise _created_error(again, 'file') from None
            created = True
        elif status in _NT_NOT_FOUND or status == _NT_DELETE_PENDING:
            if create:                                   # still open elsewhere after its removal: not made again yet
                raise _created_error(e, 'file') from None
            raise _missing('file') from None
        else:
            raise _refuse('not_regular' if status in _NT_WRONG_TYPE else 'open') from None

    def check(opened):
        _win_kind(_win_facts(opened), False, 'file', True)
        try:
            windows_handle_unwritable(opened)
        except PrivateStorageError as e:
            raise PrivateStorageError(str(e), e.reason, 'file') from None
    _win_check_or_undo(parent, name, handle, created, check)
    return _win_fd(handle, os.O_APPEND | getattr(os, 'O_BINARY', 0)), created


def _win_file_id(handle):
    """The volume and file index of what ``handle`` has open: which file it is, whatever its name."""
    kernel, w, SA, FILEINFO = _win_api()[1:5]
    info = FILEINFO()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _unavailable()
    return info.volume, info.index_high, info.index_low


_FILETIME_UNIX_EPOCH = 116444736000000000      # 1601-01-01 to 1970-01-01 in 100 ns units


def _win_identity(handle):
    """``file_identity`` from one GetFileInformationByHandle call on the handle."""
    kernel, w, SA, FILEINFO = _win_api()[1:5]
    info = FILEINFO()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _unavailable()

    def nanoseconds(filetime):
        return (((filetime.high << 32) | filetime.low) - _FILETIME_UNIX_EPOCH) * 100
    return (info.volume, (info.index_high << 32) | info.index_low, (info.size_high << 32) | info.size_low,
            nanoseconds(info.written), nanoseconds(info.created))


def _posix_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_identity(fd):
    """(volume, file index, size, last write time in ns, fourth time in ns) of what the open descriptor ``fd`` has
    open, for identities that are stored and compared later.

    POSIX: ``st_dev``, ``st_ino``, ``st_size``, ``st_mtime_ns`` and ``st_ctime_ns`` of ``fstat``, as always. Windows:
    one GetFileInformationByHandle call on the handle: the 32-bit volume serial number, the 64-bit file index, the
    size, and the last write and creation times. Python's ``fstat`` is not used there because what it reports has
    changed between versions: since 3.12 ``st_dev`` is a 64-bit serial (often above 2**63) and ``st_ino`` may be a
    128-bit id, so an identity stored under one Python would not match under the next."""
    if WIN:
        return _win_identity(_win_handle_of(fd))
    return _posix_identity(os.fstat(fd))


def _win_check_or_undo(parent, name, handle, created, check):
    """Run ``check(handle)``. On a refusal close the handle and, if this call created the file, remove it again by
    name, but only while the name still has that file's ID (the handle has no DELETE access to do it itself)."""
    try:
        check(handle)
    except BaseException:
        file_id = None
        if created:
            try:
                file_id = _win_file_id(handle)
            except PrivateStorageError:
                pass
        _win_close(handle)
        if file_id is not None:
            _win_discard_named(parent, name, file_id)
        raise


def _win_discard_named(parent, name, file_id):
    try:
        handle = _win_child(parent, name, _DELETE | _FILE_READ_ATTRIBUTES, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE,
                            False)
    except OSError:
        return
    try:
        if _win_file_id(handle) == file_id:
            _win_set_delete(handle)
    except PrivateStorageError:
        pass
    finally:
        _win_close(handle)


def _win_mkdir(parent, name):
    with _private_descriptor() as descriptor:
        try:
            handle = _win_child(parent, name, _HOLD_ACCESS | _DELETE, _SHARE_READ | _SHARE_WRITE, True,
                                _NT_FILE_CREATE, descriptor=descriptor)
        except OSError as e:
            raise _created_error(e, 'folder') from None
    try:
        _win_kind(_win_facts(handle), True, 'folder')
        _win_private(handle, True, 'folder')
    except BaseException:
        _win_discard(handle)
        raise
    _win_close(handle)


def _close_fd(fd):
    os.close(fd)


def _win_remove(parent, name, expect):
    try:
        # DELETE access, and sharing only reading: an open writer (or anyone not sharing delete) makes this 'busy'.
        handle = _win_child(parent, name, _GENERIC_READ | _DELETE, _SHARE_READ, False)
    except OSError as e:
        status = getattr(e, 'ntstatus', 0)
        if status in _NT_NOT_FOUND:
            return 'absent'
        if status in (_NT_SHARING_VIOLATION, _NT_DELETE_PENDING):
            return 'busy'
        if status in _NT_WRONG_TYPE:
            return 'changed'
        raise _refuse('open') from None
    try:
        facts = _win_facts(handle)
        try:
            _win_kind(facts, False, 'file')
        except PrivateStorageError:
            return 'changed'
        # expect gets a duplicate that can only read, so it cannot change or delete what it is shown.
        fd = _win_fd(_win_duplicate(handle, _FILE_GENERIC_READ), os.O_RDONLY | getattr(os, 'O_BINARY', 0))
        try:
            if not expect(fd):
                return 'changed'
        finally:
            _close_fd(fd)
        try:
            way = _win_set_delete(handle)
        except PrivateStorageError:
            if facts[0] & _READONLY:
                return 'changed'          # only the older deletion is available, and it cannot delete a read-only file
            raise
    finally:
        _win_close(handle)
    if way == 'posix':
        return 'removed'
    # The older deletion: the name stays until every other handle on the file closes.
    try:
        probe = _win_child(parent, name, _FILE_READ_ATTRIBUTES, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, None)
    except OSError as e:
        status = getattr(e, 'ntstatus', 0)
        if status in _NT_NOT_FOUND:
            return 'removed'
        if status == _NT_DELETE_PENDING:
            return 'busy'
        raise _refuse('open') from None
    _win_close(probe)
    return 'removed'      # a new file already has the name, so the one that was checked is gone


def _win_flush(handle):
    if not _win_api()[1].FlushFileBuffers(handle):
        raise _unavailable('folder')


def _win_file_system(handle):
    kernel = _win_api()[1]
    name = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, name, len(name)):
        raise _unavailable('folder')
    return name.value


def check_private_fd(fd, directory=False, single_link=False):
    """Raise PrivateStorageError unless the open descriptor ``fd`` is a private folder or regular file (one name only,
    with ``single_link``): on POSIX ``posix_verdict`` on its ``fstat``; on Windows the type, reparse attribute, link
    count, owner and access list read on its handle (the descriptor needs READ_CONTROL and attribute access, which
    every descriptor from the walk and section (h) has). Windows' ``fstat`` has no owner or access bits, so this is
    how a caller checks a descriptor it already holds."""
    subject = 'folder' if directory else 'file'
    if not WIN:
        posix_verdict(os.fstat(fd), directory, single_link, subject)
        return
    handle = _win_handle_of(fd)
    _win_kind(_win_facts(handle), directory, subject, single_link)
    _win_private(handle, directory, subject)


def sync_file(fd):
    """Flush a file's data to disk: ``os.fsync`` (``FlushFileBuffers`` on Windows, which needs a descriptor that can
    write)."""
    os.fsync(fd)


# Access that can change a file: write or append data, extended attributes, attributes, delete, access list, owner.
_CHANGE_ACCESS = 0x2 | 0x4 | 0x10 | 0x100 | _DELETE | 0x40000 | 0x80000


def _win_handle_of(fd):
    import msvcrt
    return msvcrt.get_osfhandle(fd)


def _win_access_mask(handle):
    """The access a handle was granted (FileAccessInformation)."""
    nt = _win_api()[5]
    IO_STATUS_BLOCK = nt.structures[2]
    granted, status_block = ctypes.c_uint32(), IO_STATUS_BLOCK()
    status = nt.NtQueryInformationFile(handle, ctypes.byref(status_block), ctypes.byref(granted),
                                       ctypes.sizeof(granted), 8) & 0xFFFFFFFF
    if status >= 0x80000000:
        raise _unavailable()
    return granted.value


def read_only(fd):
    """True if the file descriptor ``fd`` (a file's, not a folder's) can read its file and cannot change it. POSIX:
    ``fcntl(F_GETFL) & O_ACCMODE == O_RDONLY``. Windows: the handle's granted access includes reading data and none
    of the change rights."""
    if WIN:
        granted = _win_access_mask(_win_handle_of(fd))
        return bool(granted & _FILE_READ_DATA) and not granted & _CHANGE_ACCESS
    import fcntl
    return fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY


def unwritable(fd, system_owned=False):
    """Raise PrivateStorageError unless nobody but the owner (and the system) can change what ``fd`` has open: for
    files and folders that are not private but must not be changed by others (a project folder, its Git folders, the
    Git program). POSIX: the owner is this user, or root with ``system_owned``, and ``mode & 0o022 == 0``. Windows:
    ``acl_write_verdict`` on the opened handle's owner and access list (the descriptor needs READ_CONTROL, which every
    descriptor from the walk has)."""
    if WIN:
        handle = _win_handle_of(fd)
        subject = 'folder' if _win_facts(handle)[0] & DIRECTORY else 'file'
        try:
            windows_handle_unwritable(handle, system_owned)
        except PrivateStorageError as e:
            raise PrivateStorageError(str(e), e.reason, subject) from None
        return
    info = os.fstat(fd)
    _posix_unwritable(info, system_owned, 'folder' if stat.S_ISDIR(info.st_mode) else 'file')


def _posix_unwritable(info, system_owned, subject):
    if info.st_uid != (0 if system_owned else os.getuid()):
        raise _refuse('owner', subject)
    if info.st_mode & 0o022:
        raise _refuse('access', subject)


# Opens that meet something other than a file: a link (ELOOP; EMLINK is FreeBSD's O_NOFOLLOW), a socket (ENXIO on
# Linux, EOPNOTSUPP on macOS) or a device that is not there (ENODEV).
_NOT_A_FILE_OPEN = frozenset(getattr(errno, name) for name in ('ELOOP', 'EMLINK', 'ENXIO', 'EOPNOTSUPP', 'ENODEV')
                             if hasattr(errno, name))


def _as_link(info):
    """``info`` with its type changed to a symbolic link, all else kept (Windows' fstat gives a reparse point the type
    of what it stands for)."""
    shown = ('st_mode', 'st_ino', 'st_dev', 'st_nlink', 'st_uid', 'st_gid', 'st_size')
    extra = {name: getattr(info, name) for name in dir(info) if name.startswith('st_') and name not in shown}
    fields = list(info)
    fields[0] = stat.S_IFLNK | stat.S_IMODE(info.st_mode)
    return os.stat_result(fields, extra)


def _sid_text(sid):
    adv, kernel, w = _win_api()[:3]
    text = w.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _unavailable()
    try:
        return text.value
    finally:
        kernel.LocalFree(text)


def _read_aces(dacl, user):
    """Every entry of a DACL as (ace_type, ace_flags, access_mask, sid_string, is_current_user). The SID is read for
    allow and deny entries (it follows the mask); other entry types carry None."""
    adv, kernel, w = _win_api()[:3]

    class ACLINFO(ctypes.Structure):
        _fields_ = [('count', w.DWORD), ('used', w.DWORD), ('free', w.DWORD)]
    info = ACLINFO()
    if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):   # AclSizeInformation
        raise _unavailable()
    aces = []
    for i in range(info.count):
        ace = w.LPVOID()
        if not adv.GetAce(dacl, i, ctypes.byref(ace)):
            raise _unavailable()
        head = ctypes.string_at(ace, 8)              # ACE_HEADER (type, flags, size), then the access mask
        ace_type, flags, mask = head[0], head[1], int.from_bytes(head[4:8], 'little')
        sid, is_user = None, False
        if ace_type in (0, 1):
            pointer = w.LPVOID(ace.value + 8)
            sid, is_user = _sid_text(pointer), bool(adv.EqualSid(pointer, user.sid))
        aces.append((ace_type, flags, mask, sid, is_user))
    return aces


def windows_handle_unwritable(handle, system_owned=False):
    """``acl_write_verdict`` for an opened handle (needs READ_CONTROL): its owner and every DACL entry with its mask."""
    adv, kernel, w = _win_api()[:3]
    with _CurrentUser() as user:
        owner, dacl, sd = w.LPVOID(), w.LPVOID(), w.LPVOID()
        if adv.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd)):
            raise _unavailable()
        try:
            aces = _read_aces(dacl, user) if dacl else []
            acl_write_verdict(_sid_text(owner), bool(adv.EqualSid(owner, user.sid)), bool(dacl), aces, system_owned)
        finally:
            kernel.LocalFree(sd)
