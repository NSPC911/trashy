from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from os import PathLike
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class TrashEntry:
    """A single item currently sitting in the recycle bin.

    Instances are produced by `RecycleBin.entries` and consumed by
    `RecycleBin.restore`. The `_handle` field is backend-private
    (a `.trashinfo` path on Linux, an `$I` path on Windows, ...) and is
    what actually lets restore find the trashed bytes; do not construct it
    yourself.
    """

    name: str
    """Human-facing name of the item as it appears in the bin."""

    original_path: str | None
    """Absolute path the item lived at before it was recycled. None if the platform does not record it."""

    deleted_at: datetime | None
    """When the item was recycled, if the platform records it."""

    size: int | None = None
    """Size in bytes of the original item, if known."""

    _handle: str = field(default="", repr=False)
    """Backend-private locator for the trashed data. Not part of the API."""


@runtime_checkable
class RecycleBinLike(Protocol):
    """Cross-platform recycle-bin backend contract.

    One concrete implementation exists per OS. Callers should go through the
    module-level helpers in `pytrash` rather than instantiating a backend
    directly.
    """

    def recycle(self, items: list[str | PathLike]) -> None:
        """Move the given items to the recycle bin.

        Args:
            items: File or directory paths to recycle.
        """
        ...

    def entries(self) -> list[TrashEntry]:
        """List everything currently in the recycle bin.

        Returns:
            The trashed items, most-recently-deleted first where the platform
            records deletion times.
        """
        ...

    def restore(
        self,
        items: list[TrashEntry],
        on_exist: Callable[[Exception], bool] = lambda x: False,
    ) -> None:
        """Restore items previously returned by `entries`.

        Args:
            items: Entries to move back to their original locations.
            on_error: Optional callback invoked with any exception raised
                while restoring an item. If it returns `True`, the restore
                will continue with the next item; if `False` or `None`,
                the restore will abort and propagate the exception.
        """
        ...
