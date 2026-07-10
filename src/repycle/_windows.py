"""Windows backend using only ``ctypes`` + stdlib.

Two mechanisms are combined:

* ``recycle`` calls ``SHFileOperationW`` with ``FOF_ALLOWUNDO`` so the OS
  writes correct ``$I`` metadata (we never hand-move files into the bin).
* ``entries`` / ``restore`` work directly against the per-SID
  ``<drive>:\\$Recycle.Bin\\<SID>`` folders. On Vista+ each recycled item is a
  self-contained ``$Ixxxxxx`` (metadata) + ``$Rxxxxxx`` (data) pair with no
  central index, so restoring is just moving ``$R`` back and deleting the pair
  — no COM / ``undelete`` verb plumbing required.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
from ctypes import wintypes
from datetime import datetime, timedelta, timezone

from ._type import TrashEntry

# -- SHFileOperationW -----------------------------------------------------

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


# 1601-01-01 .. 1970-01-01 is a fixed offset; FILETIME counts 100ns ticks.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _filetime_to_dt(ft: int) -> datetime | None:
    if ft <= 0:
        return None
    return _FILETIME_EPOCH + timedelta(microseconds=ft / 10)


class WindowsRecycleBin:
    def recycle(self, items: list) -> None:
        paths = [os.path.abspath(os.fspath(i)) for i in items]
        for p in paths:
            if not os.path.lexists(p):
                raise FileNotFoundError(p)
        # pFrom is a double-null-terminated, null-separated list.
        buf = "\0".join(paths) + "\0\0"

        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = buf
        op.pTo = None
        op.fFlags = (
            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        )

        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0:
            raise OSError(f"SHFileOperation failed (code 0x{rc:X})")
        if op.fAnyOperationsAborted:
            raise OSError("recycle operation was aborted")

    def entries(self) -> list[TrashEntry]:
        out: list[TrashEntry] = []
        for sid_dir in self._sid_dirs():
            try:
                names = os.listdir(sid_dir)
            except OSError:
                continue
            for name in names:
                if not name.startswith("$I"):
                    continue
                info_path = os.path.join(sid_dir, name)
                parsed = self._parse_info(info_path)
                if parsed is None:
                    continue
                original, size, deleted_at = parsed
                out.append(
                    TrashEntry(
                        name=os.path.basename(original),
                        original_path=original,
                        deleted_at=deleted_at,
                        size=size,
                        _handle=info_path,
                    )
                )
        out.sort(key=lambda e: e.deleted_at or datetime.min, reverse=True)
        return out

    def restore(self, items: list[TrashEntry]) -> None:
        for entry in items:
            info_path = entry._handle
            data_path = self._data_path(info_path)
            if not os.path.lexists(data_path):
                raise FileNotFoundError(
                    f"trashed data missing for {entry.name!r}: {data_path}"
                )
            dest = entry.original_path
            if os.path.lexists(dest):
                raise FileExistsError(
                    f"cannot restore {entry.name!r}: {dest} already exists"
                )
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(data_path, dest)
            try:
                os.remove(info_path)
            except OSError:
                pass

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _data_path(info_path: str) -> str:
        """``$Ixxxxxx`` -> its sibling ``$Rxxxxxx`` data file/dir."""
        d, name = os.path.split(info_path)
        return os.path.join(d, "$R" + name[2:])

    def _sid_dirs(self) -> list[str]:
        sid = self._current_sid()
        out: list[str] = []
        for drive in self._drives():
            root = os.path.join(drive, "$Recycle.Bin")
            if not os.path.isdir(root):
                continue
            if sid:
                cand = os.path.join(root, sid)
                if os.path.isdir(cand):
                    out.append(cand)
            else:
                try:
                    for child in os.listdir(root):
                        cand = os.path.join(root, child)
                        if os.path.isdir(cand):
                            out.append(cand)
                except OSError:
                    pass
        return out

    @staticmethod
    def _drives() -> list[str]:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [
            f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)
        ]

    @staticmethod
    def _current_sid() -> str | None:
        TOKEN_QUERY = 0x0008
        TokenUser = 1
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                TOKEN_QUERY,
                ctypes.byref(token),
            ):
                return None
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                token, TokenUser, None, 0, ctypes.byref(size)
            )
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, TokenUser, buf, size, ctypes.byref(size)
            ):
                return None
            # TOKEN_USER starts with SID_AND_ATTRIBUTES; first pointer is Sid.
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_ptr = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(
                sid_ptr, ctypes.byref(str_ptr)
            ):
                return None
            value = str_ptr.value
            kernel32.LocalFree(str_ptr)
            return value
        except OSError:
            return None
        finally:
            if token:
                kernel32.CloseHandle(token)

    @staticmethod
    def _parse_info(path: str) -> tuple[str, int | None, datetime | None] | None:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            return None
        if len(blob) < 24:
            return None
        version, size, ft = struct.unpack_from("<qqq", blob, 0)
        if version == 1:
            # Fixed 260-wchar (520-byte) path field.
            raw = blob[24:24 + 520]
        elif version == 2:
            if len(blob) < 28:
                return None
            (nchars,) = struct.unpack_from("<i", blob, 24)
            raw = blob[28:28 + nchars * 2]
        else:
            return None
        original = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        if not original:
            return None
        return original, (size if size >= 0 else None), _filetime_to_dt(ft)
