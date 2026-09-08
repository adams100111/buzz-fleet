"""Parse human durations like 45m or 1h30m into seconds."""

from __future__ import annotations

import re

_UNIT = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d+)([hms])")


def parse_duration(text: str) -> int:
    s = text.strip()
    if s.isdigit():
        total = int(s)
    else:
        total, pos, last = 0, 0, -1
        for m in _PART.finditer(s):
            if m.start() != pos:
                raise ValueError(f"invalid duration {text!r}")
            idx = "hms".index(m.group(2))
            if idx <= last:
                raise ValueError(f"invalid duration {text!r}: units must go h, m, s")
            last = idx
            total += int(m.group(1)) * _UNIT[m.group(2)]
            pos = m.end()
        if pos != len(s):
            raise ValueError(f"invalid duration {text!r}")
    if total <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return total
