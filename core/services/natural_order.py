"""The house natural order for labels: ``M2`` before ``M10``.

One place for it, so a section label, an exam code, a room code and a building
name sort the same way wherever they are listed. Pure Python; imports nothing
from the app, so any service can use it without pulling another subsystem in.
"""

from __future__ import annotations

import re


def natural_key(label: str) -> tuple:
    """M2 before M10 and "PHYS103 (2)" after "PHYS103 (1)": the house order."""
    return tuple(
        (1, int(part)) if part.isdecimal() else (0, part.casefold())
        for part in re.split(r"(\d+)", str(label or ""))
    )
