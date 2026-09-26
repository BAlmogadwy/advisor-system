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

LRM = "\u200e"
RLM = "\u200f"


def ltr_run(text: object) -> str:
    """Wrap one code, time or ID run for an Arabic cell: LRM, the run, RLM."""
    return f"{LRM}{text}{RLM}"
