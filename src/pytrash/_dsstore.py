"""Read-only `.DS_Store` reader (buddy allocator + B-tree), pure stdlib.

macOS records a trashed item's "Put Back" location in the trash folder's
`.DS_Store` as two records keyed by the item's on-disk name:

* `ptbL` -- the original parent directory, relative to the volume root
* `ptbN` -- the original file name

On current macOS (verified on Ventura) both are stored as `ustr` (a plain
UTF-16BE string), so recovering the origin needs no bookmark/alias decoding
and therefore no third-party dependency. This module reads just enough of the
format to pull those records out; it never writes.
"""

from __future__ import annotations

import struct


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self.buf = buf
        self.pos = pos

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def take(self, n: int) -> bytes:
        v = self.buf[self.pos : self.pos + n]
        self.pos += n
        return v


def _parse_value(r: _Reader, dtype: bytes) -> object:
    if dtype == b"ustr":
        n = r.u32()
        return r.take(n * 2).decode("utf-16-be")
    if dtype in (b"long", b"shor"):
        return r.u32()
    if dtype == b"blob":
        n = r.u32()
        return r.take(n)
    if dtype == b"bool":
        return r.take(1) != b"\x00"
    if dtype == b"type":
        return r.take(4)
    if dtype in (b"comp", b"dutc"):
        v = struct.unpack_from(">Q", r.buf, r.pos)[0]
        r.pos += 8
        return v
    raise ValueError(f"unknown .DS_Store dataType {dtype!r}")


def _read_record(r: _Reader) -> tuple[str, bytes, object]:
    nlen = r.u32()
    name = r.take(nlen * 2).decode("utf-16-be")
    struct_id = r.take(4)
    dtype = r.take(4)
    return name, struct_id, _parse_value(r, dtype)


def parse(buf: bytes) -> dict[str, dict[str, object]]:
    """Parse a `.DS_Store` image into `{filename: {structId: value}}`.

    Returns:
        Every record grouped by its filename, mapping each 4-char structure
        id (e.g. `"ptbL"`) to its decoded value.

    Raises:
        ValueError: The buffer is not a valid `.DS_Store` (bad magic or a
            corrupt allocator header).
    """
    _magic, bud = struct.unpack_from(">I4s", buf, 0)
    if bud != b"Bud1":
        raise ValueError("not a .DS_Store (bad Bud1 magic)")
    # Every stored offset is relative to byte 4 of the file, hence the +4.
    root_off, _root_len, root_off2 = struct.unpack_from(">III", buf, 8)
    if root_off != root_off2:
        raise ValueError("corrupt .DS_Store (root offset mismatch)")

    root = _Reader(buf, root_off + 4)
    block_count = root.u32()
    root.u32()  # unknown, always 0
    addresses = [root.u32() for _ in range(block_count)]
    # Addresses live in slabs of 256 slots; skip the zero padding.
    padded = ((block_count + 255) // 256) * 256
    root.pos += (padded - block_count) * 4

    ndirs = root.u32()
    dirs: dict[str, int] = {}
    for _ in range(ndirs):
        nlen = root.buf[root.pos]
        root.pos += 1
        dname = root.take(nlen).decode("ascii")
        dirs[dname] = root.u32()

    if "DSDB" not in dirs:
        return {}

    def block(num: int) -> _Reader:
        offset = (addresses[num] & ~0x1F) + 4
        return _Reader(buf, offset)

    hdr = block(dirs["DSDB"])
    root_node = hdr.u32()

    out: dict[str, dict[str, object]] = {}

    def walk(node_num: int) -> None:
        r = block(node_num)
        p = r.u32()
        count = r.u32()
        for _ in range(count):
            if p:  # internal node: child pointer precedes each record
                walk(r.u32())
            name, sid, val = _read_record(r)
            out.setdefault(name, {})[sid.decode("ascii")] = val
        if p:
            walk(p)

    walk(root_node)
    return out


def put_back_locations(buf: bytes, volume_root: str) -> dict[str, str]:
    """Map each trashed on-disk name to its recovered original absolute path.

    Args:
        buf: Raw bytes of a trash folder's `.DS_Store`.
        volume_root: Mount point the `ptbL` paths are relative to
            (`"/"` for the home trash, `/Volumes/<vol>` otherwise).

    Returns:
        `{trashed_name: original_absolute_path}` for every entry that has a
        `ptbL` record. Entries without one are omitted.
    """
    import os.path

    result: dict[str, str] = {}
    for name, fields in parse(buf).items():
        parent = fields.get("ptbL")
        if not isinstance(parent, str):
            continue
        orig_name = fields.get("ptbN")
        leaf = orig_name if isinstance(orig_name, str) else name
        result[name] = os.path.join(volume_root, parent, leaf)
    return result
