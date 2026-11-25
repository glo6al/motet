"""Isolated Windows folder chooser. No UI, COM or filesystem work on import.

The server runs this file with its own Python in isolated mode (-I), one process per folder window, and reads
JSON lines from it: {"state": "started"} once the window is about to show, then one of {"state": "selected",
"path": ...}, {"state": "cancelled"} or {"state": "failed"}. Its one argument is JSON data. The window is
Windows' own Explorer-style folder dialog (IFileOpenDialog with FOS_PICKFOLDERS), driven through ctypes COM on
this process's main thread, which is a single-threaded apartment. No script, shell or command line is built:
the folder hint and the title reach the dialog only as COM string arguments. A separate process keeps third-party
shell extensions the dialog loads out of the server, and lets the server end the window by ending the process.
"""
import json
import ntpath
import os
import re
import sys
import threading
import time
import uuid

TITLE = 'Choose a project folder for Motet. Add only folders you trust.'
LIFETIME = 120
CLSID_FILE_OPEN_DIALOG = 'dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7'
IID_IFILE_OPEN_DIALOG = 'd57c7288-d4ad-4768-be02-9d969532d960'
IID_ISHELL_ITEM = '43826d1e-e718-42ee-bc55-a1e261c37bfe'
FOS_NOCHANGEDIR, FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST = 0x8, 0x20, 0x40, 0x800
FOS_ALLNONSTORAGEITEMS = 0x80
SIGDN_FILESYSPATH = 0x80058000
CANCELLED = -2147023673                 # HRESULT_FROM_WIN32(ERROR_CANCELLED), 0x800704C7
# vtable slots: IUnknown 0-2, IModalWindow::Show 3, then IFileDialog in declaration order
SHOW, SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULT, RELEASE = 3, 9, 10, 12, 17, 20, 2
ITEM_DISPLAY_NAME = 5                   # IShellItem::GetDisplayName


def windows_path(value, *, share=True):
    """A folder path as the dialog gives it on Windows: a drive path (C:\\Projects\\Orchard) or, with `share`, a
    path inside a network share (\\\\server\\share\\Orchard). Never a device or long-path prefix (\\\\?\\, \\\\.\\),
    a relative or drive-relative path, a stream (a second ':'), '/', '.' or '..' parts, an empty part except at
    the end, or a control character."""
    if (type(value) is not str or not 3 <= len(value) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or '/' in value):
        return False
    if re.match(r'[A-Za-z]:\\', value):
        parts = value[3:].split('\\')
        if ':' in value[2:]:
            return False
    elif share and value.startswith('\\\\'):
        parts = value[2:].split('\\')
        if (len(parts) < 2 or ':' in value or parts[0] in ('', '.', '?') or not parts[1]
                or parts[0].strip('.') == ''):
            return False
    else:
        return False
    if parts == ['']:
        return True                     # a drive's root; the folder rules refuse it later, as they refuse '/'
    return all(p not in ('.', '..') for p in parts) and '' not in parts[:-1]


def initial_folder(hint, isdir=os.path.isdir):
    """The folder the window opens in: the hinted folder's parent, as on the Mac, when it is a drive path (a share
    is never contacted because of a hint) and an existing folder. Otherwise the dialog's own choice."""
    if not windows_path(hint, share=False):
        return ''
    parent = ntpath.dirname(hint.rstrip('\\')) if len(hint) > 3 else hint
    try:
        return parent if windows_path(parent, share=False) and isdir(parent) else ''
    except (OSError, ValueError):
        return ''


def dialog_options(current):
    """The options the window runs with: its own, plus folders only, real file system folders only, an existing
    path, and the process's working folder left alone. FOS_ALLNONSTORAGEITEMS is cleared."""
    return (current | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR) & ~FOS_ALLNONSTORAGEITEMS


class Dialog:
    """IFileOpenDialog through ctypes. Every method runs on the thread that called open()."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.ole32, self.shell32 = ctypes.WinDLL('ole32'), ctypes.WinDLL('shell32')
        self.guid = type('GUID', (ctypes.Structure,), {'_fields_': [
            ('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]})
        o, s = self.ole32, self.shell32
        o.CoInitializeEx.argtypes, o.CoInitializeEx.restype = [ctypes.c_void_p, wintypes.DWORD], ctypes.c_long
        o.CoUninitialize.argtypes, o.CoUninitialize.restype = [], None
        o.CoCreateInstance.argtypes = [ctypes.POINTER(self.guid), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(self.guid), ctypes.POINTER(ctypes.c_void_p)]
        o.CoCreateInstance.restype = ctypes.c_long
        o.CoTaskMemFree.argtypes, o.CoTaskMemFree.restype = [ctypes.c_void_p], None
        s.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(self.guid),
                                                  ctypes.POINTER(ctypes.c_void_p)]
        s.SHCreateItemFromParsingName.restype = ctypes.c_long
        self.ptr, self.initialized = None, False

    def _id(self, text):
        return self.guid.from_buffer_copy(uuid.UUID(text).bytes_le)

    def _call(self, ptr, slot, argtypes, *args):
        ctypes = self.ctypes
        table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[slot])
        return method(ptr, *args)

    @staticmethod
    def _check(hr):
        if hr < 0:
            raise OSError('COM call failed: 0x%08X' % (hr & 0xFFFFFFFF))
        return hr

    def open(self):
        ctypes = self.ctypes
        # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE: the dialog needs a single-threaded apartment
        self._check(self.ole32.CoInitializeEx(None, 0x2 | 0x4))
        self.initialized = True
        ptr = ctypes.c_void_p()
        self._check(self.ole32.CoCreateInstance(ctypes.byref(self._id(CLSID_FILE_OPEN_DIALOG)), None, 1,     # CLSCTX_INPROC_SERVER
                                                ctypes.byref(self._id(IID_IFILE_OPEN_DIALOG)), ctypes.byref(ptr)))
        self.ptr = ptr

    def options(self):
        value = self.wintypes.DWORD()
        self._check(self._call(self.ptr, GET_OPTIONS, [self.ctypes.POINTER(self.wintypes.DWORD)], self.ctypes.byref(value)))
        return value.value

    def configure(self, initial):
        self._check(self._call(self.ptr, SET_OPTIONS, [self.wintypes.DWORD], dialog_options(self.options())))
        self._check(self._call(self.ptr, SET_TITLE, [self.wintypes.LPCWSTR], TITLE))
        if initial:
            item = self.ctypes.c_void_p()
            if self.shell32.SHCreateItemFromParsingName(initial, None, self.ctypes.byref(self._id(IID_ISHELL_ITEM)),
                                                        self.ctypes.byref(item)) >= 0 and item.value:
                try:
                    self._call(self.ptr, SET_FOLDER, [self.ctypes.c_void_p], item)
                finally:
                    self._call(item, RELEASE, [])

    def show(self):
        ctypes = self.ctypes
        hr = self._call(self.ptr, SHOW, [ctypes.c_void_p], None)
        if hr == CANCELLED:
            return {'state': 'cancelled'}
        self._check(hr)
        item, text = ctypes.c_void_p(), ctypes.c_void_p()
        self._check(self._call(self.ptr, GET_RESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(item)))
        try:
            self._check(self._call(item, ITEM_DISPLAY_NAME, [self.wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)],
                                   SIGDN_FILESYSPATH, ctypes.byref(text)))
            try:
                path = ctypes.wstring_at(text.value)
            finally:
                self.ole32.CoTaskMemFree(text)
        finally:
            self._call(item, RELEASE, [])
        return {'state': 'selected', 'path': path}

    def close(self):
        if self.ptr is not None and self.ptr.value:
            self._call(self.ptr, RELEASE, [])
        self.ptr = None
        if self.initialized:
            self.ole32.CoUninitialize()
            self.initialized = False


def parent_alive(pid):
    """A function that says whether process `pid` still runs, holding a handle to it, so a new process given the
    same number later is never taken for it."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32')
    k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    handle = k.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE
    if not handle:
        return lambda: False
    return lambda: k.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running


def helper(data, *, dialog=None, emit=None, parent=os.getppid, alive=None, lifetime=LIFETIME, exit=os._exit):
    """One folder window. A watchdog thread ends the process if the server goes away, or when the window has been
    open for `lifetime` seconds (then it reports cancelled, as the Mac does). Only the first terminal frame counts."""
    out = threading.Lock()
    done = []

    def say(value, final=True):
        with out:
            if done:
                return
            if final:
                done.append(value)
            (emit or (lambda v: print(json.dumps(v, ensure_ascii=True), flush=True)))(value)

    if type(data) is not dict or type(data.get('parent')) is not int or parent() != data['parent']:
        say({'state': 'cancelled'})
        return
    alive = alive or parent_alive(data['parent'])
    deadline = time.monotonic() + lifetime
    finished = threading.Event()
    opened = threading.Event()

    def watch():
        while not finished.wait(.1):
            if not alive():
                exit(0)                 # nobody is left to read an answer
            if time.monotonic() >= deadline:
                say({'state': 'cancelled' if opened.is_set() else 'failed'})
                exit(0)
    threading.Thread(target=watch, daemon=True).start()
    dialog = dialog or Dialog()
    try:
        initial = initial_folder(data.get('initial'))
        dialog.open()
        dialog.configure(initial)
        opened.set()
        say({'state': 'started'}, final=False)
        value = dialog.show()
        if value['state'] == 'selected' and not windows_path(value.get('path')):
            value = {'state': 'failed'}
        say(value)
    except Exception:
        say({'state': 'failed'})
    finally:
        finished.set()
        try:
            dialog.close()
        except Exception:
            pass


if __name__ == '__main__':
    try:
        helper(json.loads(sys.argv[1]))
    except Exception:
        print('{"state":"failed"}', flush=True)
