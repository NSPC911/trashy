"""trashy: a cross-platform recycle bin: send *and* restore.

Unlike send-to-trash-only libraries, this exposes the full round trip:

    >>> import trashy
    >>> trashy.recycle(["notes.txt"])
    >>> entries = trashy.entries()
    >>> trashy.restore([entries[0]])

Every platform is backed by stdlib + `ctypes` only, no third-party deps.
"""

from __future__ import annotations

import os
import sys

from ._type import RecycleBinLike, TrashEntry

if sys.platform == "win32":
    from ._windows import WindowsRecycleBin as RecycleBin
elif sys.platform == "darwin":
    from ._macos import MacRecycleBin as RecycleBin
elif sys.platform.startswith("linux") or os.name == "posix":
    from ._linux import LinuxRecycleBin as RecycleBin
else:
    raise NotImplementedError(f"unsupported platform: {sys.platform!r}")


__all__ = [
    "RecycleBinLike",
    "RecycleBin",
    "TrashEntry",
    "recycle",
    "entries",
    "restore",
    "main",
]

bin = RecycleBin()


def recycle(items: list[str | os.PathLike]) -> None:
    """Move `items` to the recycle bin. See `RecycleBin.recycle`."""
    bin.recycle(items)


def entries() -> list[TrashEntry]:
    """List the recycle bin. See `RecycleBin.entries`.

    Returns:
        A list of `TrashEntry"""
    return bin.entries()


def restore(items: list[TrashEntry]) -> None:
    """Restore `items` from the bin. See `RecycleBin.restore`."""
    bin.restore(items)


def main() -> None:
    for entry in entries():
        when = entry.deleted_at.isoformat() if entry.deleted_at else "?"
        print(f"{when}\t{entry.original_path or entry.name}")
