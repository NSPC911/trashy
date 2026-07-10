"""macOS backend using only `ctypes` + stdlib.

`recycle` drives Foundation's `-[NSFileManager trashItemAtURL:...]` through
the Objective-C runtime, which keeps the "Put Back" metadata that Finder needs.
`entries` lists `~/.Trash` directly.

`restore` is the genuinely awkward one: macOS stores the original location as
Finder-managed metadata, not a readable sidecar, so a fully clean restore-to-
original relies on Finder. That path is left unimplemented here rather than
shipped as something that silently restores to the wrong place; see the note in
`MacRecycleBin.restore`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from datetime import datetime
from typing import Callable

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
                raise OSError(f"failed to trash {path!r}")

    def entries(self) -> list[TrashEntry]:
        trash = os.path.expanduser("~/.Trash")
        out: list[TrashEntry] = []
        try:
            names = os.listdir(trash)
        except OSError:
            return out
        for name in names:
            full = os.path.join(trash, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            out.append(
                TrashEntry(
                    name=name,
                    # macOS does not expose the origin as readable metadata.
                    original_path=None,
                    deleted_at=datetime.fromtimestamp(st.st_mtime),
                    size=st.st_size,
                    _handle=full,
                )
            )
        out.sort(key=lambda e: e.deleted_at or datetime.min, reverse=True)
        return out

    def restore(self, items: list[TrashEntry], on_exist: Callable[[Exception], bool] = lambda x: False) -> None:
        raise NotImplementedError(
            "restore-to-original on macOS needs Finder's Put Back metadata, "
            "which is not readable from a sidecar. Use AppleScript, e.g.:\n"
            "    osascript -e 'tell application \"Finder\" to "
            "set toRestore to (every item of trash) ... put back'\n"
            "or restore to an explicit directory with shutil.move()."
        )
