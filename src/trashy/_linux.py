"""Linux / *nix backend implementing the FreeDesktop.org Trash spec.

https://specifications.freedesktop.org/trash-spec/latest/

Pure stdlib: a recycle bin here is just a pair of `files/` and `info/`
directories plus one `.trashinfo` sidecar per item, so no ctypes is needed.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from datetime import datetime
from typing import Callable
from urllib.parse import quote, unquote

from ._type import TrashEntry

_INFO_EXT = ".trashinfo"


def _xdg_data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _home_trash() -> str:
    return os.path.join(_xdg_data_home(), "Trash")


def _device_of(path: str) -> int:
    """`st_dev` of `path`, walking up to the nearest existing ancestor."""  # ruff:ignore[docstring-missing-returns]
    path = os.path.realpath(path)
    while not os.path.lexists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return os.lstat(path).st_dev


def _mount_point(path: str) -> str:
    """Return the mount point (top directory) containing `path`."""  # ruff:ignore[docstring-missing-returns]
    path = os.path.realpath(path)
    dev = _device_of(path)
    while True:
        parent = os.path.dirname(path)
        if (
            parent == path
            or not os.path.lexists(parent)
            or (os.lstat(parent).st_dev != dev)
        ):
            return path
        path = parent


def _trash_dir_for(path: str) -> str:
    """Pick the trash directory that should hold `path`.

    Items on the same filesystem as the home trash go there; items on another
    volume go to `$topdir/.Trash-$uid` so the rename stays on one device.
    Returns:
        The path to the trash directory that should hold `path`.
    """
    home = _home_trash()
    if _device_of(path) == _device_of(home):
        return home
    top = _mount_point(path)
    return os.path.join(top, f".Trash-{os.getuid()}")


def _ensure_dirs(trash_dir: str) -> tuple[str, str]:
    files = os.path.join(trash_dir, "files")
    info = os.path.join(trash_dir, "info")
    os.makedirs(files, exist_ok=True)
    os.makedirs(info, exist_ok=True)
    return files, info


def _unique_name(info_dir: str, files_dir: str, base: str) -> str:
    """Find a name free in both `info/` and `files/` (spec: no clobber).

    Args:
        info_dir: path to the `info/` directory
        files_dir: path to the `files/` directory
        base: desired name (basename of the original file)

    Returns:
        A name that does not exist in either directory, appending `.N` if needed.
    """
    candidate = base
    n = 1
    while os.path.exists(
        os.path.join(info_dir, candidate + _INFO_EXT)
    ) or os.path.lexists(os.path.join(files_dir, candidate)):
        root, ext = os.path.splitext(base)
        candidate = f"{root}.{n}{ext}"
        n += 1
    return candidate


class LinuxRecycleBin:
    def recycle(self, items: list) -> None:
        for item in items:
            src = os.path.abspath(os.fspath(item))
            if not os.path.lexists(src):
                raise FileNotFoundError(src)

            trash_dir = _trash_dir_for(src)
            files_dir, info_dir = _ensure_dirs(trash_dir)

            name = _unique_name(info_dir, files_dir, os.path.basename(src))
            info_path = os.path.join(info_dir, name + _INFO_EXT)

            # Write the info file first so a crash never leaves an orphan
            # data file with no way to know where it came from.
            deleted_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with open(info_path, "x", encoding="utf-8") as fh:
                fh.write(
                    f"[Trash Info]\nPath={quote(src)}\nDeletionDate={deleted_at}\n"
                )
            shutil.move(src, os.path.join(files_dir, name))

    def entries(self) -> list[TrashEntry]:
        out: list[TrashEntry] = []
        for trash_dir in self._all_trash_dirs():
            info_dir = os.path.join(trash_dir, "info")
            files_dir = os.path.join(trash_dir, "files")
            if not os.path.isdir(info_dir):
                continue
            for entry in os.listdir(info_dir):
                if not entry.endswith(_INFO_EXT):
                    continue
                info_path = os.path.join(info_dir, entry)
                data = self._parse_info(info_path)
                if data is None:
                    continue
                original, deleted_at = data
                name = entry[: -len(_INFO_EXT)]
                data_path = os.path.join(files_dir, name)
                size = None
                with contextlib.suppress(OSError):
                    size = os.path.getsize(data_path)
                out.append(
                    TrashEntry(
                        name=name,
                        original_path=original,
                        deleted_at=deleted_at,
                        size=size,
                        _handle=info_path,
                    )
                )
        out.sort(key=lambda e: e.deleted_at or datetime.min, reverse=True)
        return out

    def restore(
        self,
        items: list[TrashEntry],
        on_exist: Callable[[Exception], bool] = lambda x: False,
    ) -> None:
        for entry in items:
            info_path = entry._handle
            trash_dir = os.path.dirname(os.path.dirname(info_path))
            name = os.path.basename(info_path)[: -len(_INFO_EXT)]
            data_path = os.path.join(trash_dir, "files", name)

            if not os.path.lexists(data_path):
                exc = FileNotFoundError(
                    f"trashed data missing for {entry.name!r}: {data_path}"
                )
                if not on_exist(exc):
                    raise exc
            dest = entry.original_path
            if not dest:
                raise ValueError(
                    f"cannot restore {entry.name!r}: original path unknown"
                )
            if os.path.lexists(dest):
                exc = FileExistsError(
                    f"cannot restore {entry.name!r}: {dest} already exists"
                )
                if not on_exist(exc):
                    raise exc
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.lexists(dest):
                shutil.rmtree(dest) if os.path.isdir(dest) else os.remove(dest)
            shutil.move(data_path, dest)
            os.remove(info_path)

    # -- helpers -----------------------------------------------------------

    def _all_trash_dirs(self) -> list[str]:
        dirs = [_home_trash()]
        uid = os.getuid()
        seen_dev: set[int] = set()
        for line in self._mounts():
            try:
                dev = os.lstat(line).st_dev
            except OSError:
                continue
            if dev in seen_dev:
                continue
            seen_dev.add(dev)
            candidate = os.path.join(line, f".Trash-{uid}")
            if os.path.isdir(candidate):
                dirs.append(candidate)
        return dirs

    @staticmethod
    def _mounts() -> list[str]:
        points: list[str] = []
        try:
            with open("/proc/mounts", encoding="utf-8") as fh:
                for row in fh:
                    parts = row.split()
                    if len(parts) >= 2:
                        points.append(parts[1].replace("\\040", " "))
        except OSError:
            pass
        return points

    @staticmethod
    def _parse_info(info_path: str) -> tuple[str, datetime | None] | None:
        original = None
        deleted_at: datetime | None = None
        try:
            with open(info_path, encoding="utf-8") as fh:
                for line in fh:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key == "Path":
                        original = unquote(value)
                    elif key == "DeletionDate":
                        try:
                            deleted_at = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            deleted_at = None
        except OSError:
            return None
        if original is None:
            return None
        return original, deleted_at
