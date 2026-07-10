"""macOS backend using only `ctypes` + stdlib.

`recycle` drives Foundation's `-[NSFileManager trashItemAtURL:...]` through
the Objective-C runtime, which keeps the "Put Back" metadata that Finder needs.
`entries` lists the home trash (`~/.Trash`) plus every mounted volume's
per-user trash (`/Volumes/<vol>/.Trashes/<uid>`).

`restore` is a plain filesystem move out of the trash -- no Finder, no
`osascript`, and therefore no Accessibility permission (which would let any
app drive other apps / synthesize input; far scarier than the Full Disk Access
that reading the trash already needs). Finder records each item's original
path as ``ptbL``/``ptbN`` records in the trash's `.DS_Store`; we read those
directly (see `_dsstore`) to populate `entry.original_path`, so restore can
put the item back where it came from. When the origin is unknown (no
`.DS_Store` record), restore falls back to a caller-supplied `dest` directory.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import struct
from datetime import datetime
from typing import Callable

from . import _dsstore
from ._type import TrashEntry


def _load_objc():  # noqa
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))  # ty: ignore[invalid-argument-type]
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))  # ty: ignore[invalid-argument-type]

    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _msg(objc, receiver, selector, restype, argtypes, *args):  # noqa
    send = objc.objc_msgSend
    send.restype = restype
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
    sel = objc.sel_registerName(selector.encode())
    return send(receiver, sel, *args)


def _error_text(objc, err) -> str:  # noqa
    """Pull a human string out of an NSError* returned via an out-param.

    Returns:
        The error's `localizedDescription`, or `"unknown error"` if none.
    """
    if not err or not err.value:
        return "unknown error"
    desc = _msg(objc, err, "localizedDescription", ctypes.c_void_p, [])
    if not desc:
        return "unknown error"
    utf8 = _msg(objc, desc, "UTF8String", ctypes.c_char_p, [])
    return utf8.decode("utf-8", "replace") if utf8 else "unknown error"


class MacRecycleBin:
    def recycle(self, items: list) -> None:
        objc = _load_objc()
        cls_str = objc.objc_getClass(b"NSString")
        cls_url = objc.objc_getClass(b"NSURL")
        cls_fm = objc.objc_getClass(b"NSFileManager")

        fm = _msg(
            objc, cls_fm, "defaultManager", ctypes.c_void_p, []
        )

        for item in items:
            path = os.path.abspath(os.fspath(item))
            if not os.path.lexists(path):
                raise FileNotFoundError(path)

            ns_path = _msg(
                objc,
                cls_str,
                "stringWithUTF8String:",
                ctypes.c_void_p,
                [ctypes.c_char_p],
                path.encode("utf-8"),
            )
            url = _msg(
                objc,
                cls_url,
                "fileURLWithPath:",
                ctypes.c_void_p,
                [ctypes.c_void_p],
                ns_path,
            )
            err = ctypes.c_void_p(0)
            ok = _msg(
                objc,
                fm,
                "trashItemAtURL:resultingItemURL:error:",
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
                url,
                None,
                ctypes.byref(err),
            )
            if not ok:
                raise OSError(
                    f"failed to trash {path!r}: {_error_text(objc, err)}"
                )

    def entries(self) -> list[TrashEntry]:
        out: list[TrashEntry] = []
        # Home trash is authoritative: surface a TCC/permission failure here
        # rather than pretending the Trash is empty.
        out.extend(
            self._scan_trash(
                os.path.expanduser("~/.Trash"), strict=True, volume_root="/"
            )
        )
        # Every mounted volume keeps a per-user trash; these are best-effort
        # (an unreadable USB stick shouldn't nuke the whole listing).
        uid = os.getuid()
        try:
            volumes = os.listdir("/Volumes")
        except OSError:
            volumes = []
        for vol in volumes:
            vol_root = os.path.join("/Volumes", vol)
            vol_trash = os.path.join(vol_root, ".Trashes", str(uid))
            out.extend(
                self._scan_trash(
                    vol_trash, strict=False, volume_root=vol_root
                )
            )
        out.sort(key=lambda e: e.deleted_at or datetime.min, reverse=True)
        return out

    def restore(
        self,
        items: list[TrashEntry],
        on_exist: Callable[[Exception], bool] = lambda x: False,
        dest: str | os.PathLike | None = None,
    ) -> None:
        # A restore is just moving the item back out of the trash -- no Finder
        # and no Accessibility, only the Full Disk Access that reading the
        # trash already needs. macOS won't hand us the original path, so we
        # restore to `dest` when given, else to `entry.original_path`.
        for entry in items:
            src = entry._handle
            if dest is not None:
                target = os.path.join(os.fspath(dest), entry.name)
            elif entry.original_path:
                target = entry.original_path
            else:
                exc = ValueError(
                    f"cannot restore {entry.name!r}: macOS does not record the "
                    "original path in a readable form. Pass dest=<dir> to "
                    "choose where to restore it."
                )
                if not on_exist(exc):
                    raise exc
                continue

            if not os.path.lexists(src):
                exc = FileNotFoundError(
                    f"trashed data missing for {entry.name!r}: {src}"
                )
                if not on_exist(exc):
                    raise exc
                continue
            if os.path.lexists(target):
                exc = FileExistsError(
                    f"cannot restore {entry.name!r}: {target} already exists"
                )
                if not on_exist(exc):
                    raise exc
                # on_exist opted to overwrite.
                shutil.rmtree(target) if os.path.isdir(target) else os.remove(
                    target
                )
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.move(src, target)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _put_back_map(trash: str, volume_root: str) -> dict[str, str]:
        """Recover original paths from this trash's `.DS_Store`, if readable.

        Returns:
            ``{trashed_name: original_path}``; empty when the sidecar is
            missing, unreadable, or malformed (recovery is best-effort).
        """
        try:
            with open(os.path.join(trash, ".DS_Store"), "rb") as fh:
                return _dsstore.put_back_locations(fh.read(), volume_root)
        except (OSError, ValueError, IndexError, struct.error):
            return {}

    @staticmethod
    def _scan_trash(
        trash: str, strict: bool, volume_root: str
    ) -> list[TrashEntry]:
        out: list[TrashEntry] = []
        try:
            names = os.listdir(trash)
        except FileNotFoundError:
            # No trash folder here == nothing trashed on this volume.
            return out
        except PermissionError as exc:
            # ~/.Trash (and volume .Trashes) are TCC-protected on macOS
            # 10.14+. Reading them needs Full Disk Access for the process
            # doing the reading (your terminal, IDE, or the embedding app) --
            # otherwise the OS returns EPERM and the trash looks empty.
            if not strict:
                return out
            raise PermissionError(
                f"cannot read {trash}: {exc.strerror}. Grant Full Disk Access "
                "to the app running this (System Settings > Privacy & "
                "Security > Full Disk Access), then restart it."
            ) from exc
        except OSError:
            return out
        # Finder records each item's "Put Back" origin in the trash's
        # .DS_Store; recover it so restore can go home without a bookmark.
        put_back = MacRecycleBin._put_back_map(trash, volume_root)
        for name in names:
            if name == ".DS_Store":
                continue
            full = os.path.join(trash, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            out.append(
                TrashEntry(
                    name=name,
                    original_path=put_back.get(name),
                    deleted_at=datetime.fromtimestamp(st.st_mtime),
                    size=st.st_size,
                    _handle=full,
                )
            )
        return out
