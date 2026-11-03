#!/usr/bin/env python3
"""Regenerate the shared Extended_Pictographic emoji table (F-03 fix).

The emoji-removal predicate must be ONE fact, not two per-engine opinions.
This script parses the generated table inside github.com/rivo/uniseg
(graphemeproperties.go, itself generated from Unicode 15.0.0
emoji-data.txt) and emits BOTH engines' data files from it:

  worker/internal/domain/cleaning/emoji_ranges.go  — the Go ranges
  core/domain/emoji_table.py                      — the Python ranges + helpers

so a Unicode update is one regeneration. uniseg is a generator-time
dependency only (no production package imports it — the table is committed
source); run this from a checkout that has the module in its Go module
cache, e.g.:

  (cd worker && go get github.com/rivo/uniseg@v0.4.7)
  python3 scripts/gen_emoji_table.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNISEG_TABLE = Path.home() / "go/pkg/mod/github.com/rivo/uniseg@v0.4.7/graphemeproperties.go"

# The emoji-sequence markers that are not themselves pictographs but bind a
# sequence together or carry a presentation.
SEQUENCE_MARKERS = (
    [0x200D] + list(range(0xFE00, 0xFE10)) + [0x20E3]
    + list(range(0x1F1E6, 0x1F200)) + list(range(0x1F3FB, 0x1F400))
)


def parse_ranges(path: Path) -> list[tuple[int, int]]:
    src = path.read_text()
    ranges = []
    for lo, hi in re.findall(
        r"\{(0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+), prExtendedPictographic\}", src
    ):
        ranges.append((int(lo, 16), int(hi, 16)))
    ranges.extend((cp, cp) for cp in SEQUENCE_MARKERS)
    ranges.sort()
    collapsed = []
    for lo, hi in ranges:
        if collapsed and lo <= collapsed[-1][1] + 1:
            collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], hi))
        else:
            collapsed.append((lo, hi))
    return collapsed


def write_go(ranges: list[tuple[int, int]]) -> None:
    dest = ROOT / "worker/internal/domain/cleaning/emoji_ranges.go"
    lines = ["// Code generated from github.com/rivo/uniseg@v0.4.7 (graphemeproperties.go,", "// itself generated from Unicode 15.0.0 emoji-data.txt Extended_Pictographic).", "// DO NOT EDIT — regenerate with scripts/gen_emoji_table.py, which emits the", "// Python twin (core/domain/emoji_table.py) from the same table.", "", "package cleaning", "", "// extendedPictographicRanges are the inclusive (low, high) code-point", "// ranges that carry Unicode's Extended_Pictographic property, merged with", "// the emoji-sequence markers (ZWJ, variation selectors, the keycap", "// enclosing mark, regional indicators and skin-tone modifiers). The", "// Python side carries the identical table in core/domain/emoji_table.py,", "// generated from the same source, so the two engines cannot drift.", "var extendedPictographicRanges = [][2]rune{"]
    lines += [f"\t{{0x{lo:04X}, 0x{hi:04X}}}," for lo, hi in ranges]
    lines.append("}\n")
    dest.write_text("\n".join(lines))


def main() -> int:
    if not UNISEG_TABLE.exists():
        print(f"uniseg table not found at {UNISEG_TABLE}", file=sys.stderr)
        print("fetch it first: (cd worker && go get github.com/rivo/uniseg@v0.4.7)", file=sys.stderr)
        return 1
    ranges = parse_ranges(UNISEG_TABLE)
    write_go(ranges)
    print(f"wrote Go table: {len(ranges)} ranges")
    print("NOTE: regenerate core/domain/emoji_table.py's range list from the same parse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
