"""Regression test for the `.DS_Store` put-back reader.

`trash.DS_Store` is a real trash-folder sidecar captured on macOS Ventura,
holding put-back records for two files: one deleted via Finder (`test.py`)
and one deleted via trashy's own `recycle()` (`probe.txt`). Both must resolve
to their original absolute paths -- that's the whole basis for restore-to-
origin on macOS.
"""

from pathlib import Path

from trashy import _dsstore

FIXTURE = Path(__file__).parent / "fixtures" / "trash.DS_Store"


def test_put_back_locations_home_volume() -> None:
    buf = FIXTURE.read_bytes()
    locations = _dsstore.put_back_locations(buf, "/")
    assert locations == {
        "probe.txt": "/private/tmp/probe.txt",
        "test.py": "/Users/nspc911/Git/test.py",
    }


def test_put_back_locations_uses_volume_root() -> None:
    buf = FIXTURE.read_bytes()
    locations = _dsstore.put_back_locations(buf, "/Volumes/Data")
    # ptbL paths are relative to the volume, so a non-root mount prefixes them.
    assert locations["probe.txt"] == "/Volumes/Data/private/tmp/probe.txt"


def test_parse_exposes_raw_records() -> None:
    records = _dsstore.parse(FIXTURE.read_bytes())
    assert records["test.py"]["ptbL"] == "Users/nspc911/Git/"
    assert records["test.py"]["ptbN"] == "test.py"
