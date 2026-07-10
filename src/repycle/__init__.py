"""repycle — a cross-platform recycle bin: send *and* restore.

Unlike send-to-trash-only libraries, this exposes the full round trip:

    >>> import repycle
    >>> repycle.recycle(["notes.txt"])
    >>> entries = repycle.entries()
    >>> repycle.restore([entries[0]])

Every platform is backed by stdlib + ``ctypes`` only — no third-party deps.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from os import PathLike

from ._type import RecycleBin, TrashEntry

__all__ = [
    "RecycleBin",
    "TrashEntry",
    "get_recycle_bin",
    "recycle",
    "entries",
    "restore",
    "main",
]


@lru_cache(maxsize=1)
def get_recycle_bin() -> RecycleBin:
    """Return the recycle-bin backend for the current platform."""
    if sys.platform == "win32":
        from ._windows import WindowsRecycleBin

        return WindowsRecycleBin()
    if sys.platform == "darwin":
        from ._macos import MacRecycleBin

        return MacRecycleBin()
    if sys.platform.startswith("linux") or os_is_posix():
        from ._linux import LinuxRecycleBin

        return LinuxRecycleBin()
    raise NotImplementedError(f"unsupported platform: {sys.platform!r}")


def os_is_posix() -> bool:
    import os

    return os.name == "posix"


def recycle(items: list[str | PathLike]) -> None:
    """Move ``items`` to the recycle bin. See :meth:`RecycleBin.recycle`."""
    get_recycle_bin().recycle(items)


def entries() -> list[TrashEntry]:
    """List the recycle bin. See :meth:`RecycleBin.entries`."""
    return get_recycle_bin().entries()


def restore(items: list[TrashEntry]) -> None:
    """Restore ``items`` from the bin. See :meth:`RecycleBin.restore`."""
    get_recycle_bin().restore(items)


def main() -> None:
    for entry in entries():
        when = entry.deleted_at.isoformat() if entry.deleted_at else "?"
        print(f"{when}\t{entry.original_path or entry.name}")
