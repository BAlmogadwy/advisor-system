"""Keep left-to-right runs in reading order inside Arabic workbook text.

Excel does not implement the Unicode 6.3 isolates (U+2066..U+2069): it paints
them as visible LRI/PDI boxes and still reverses the digits (measured through
Excel's own PDF export). A left-to-right mark before a code, time or ID run
and a right-to-left mark after it keep 2026-09-24 00:10 and 08:00-10:00 in
reading order inside Arabic text, with nothing visible. The exam Department
files and student-data exports wrap such runs through :func:`ltr_run`; no
workbook writer emits an isolate.
"""

from __future__ import annotations

import unicodedata

LRM = "\u200e"
RLM = "\u200f"


def ltr_run(text: object) -> str:
    """Wrap one code, time or ID run for an Arabic cell: LRM, the run, RLM."""
    return f"{LRM}{text}{RLM}"


def end_rtl_run(text: str) -> str:
    """Close a right-to-left run inside a left-to-right cell with one LRM.

    Digits after an Arabic letter resolve as Arabic numbers (UAX #9 rule W2),
    so in an English cell a date or time that follows an Arabic day label such
    as "W1-الأحد" prints reversed. An LRM after the label restores European
    resolution for what follows. Text with no right-to-left letter is
    returned unchanged, so Latin labels never carry a mark.
    """
    if any(unicodedata.bidirectional(char) in ("R", "AL") for char in text):
        return f"{text}{LRM}"
    return text
