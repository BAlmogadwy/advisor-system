"""Parse the registrar's faculty-sections report (``facultySectionsAvilableSeats.do``).

This report is the **only** source that names an instructor per section.  The
student timetable page does not carry instructor identity at all — see
``student_timetable_ingest`` — so scraping cannot substitute for it.

Layout.  Each section is one 13-cell row.  Arabic is right-to-left, so the cells
arrive in the reverse of their visual order::

    0 المسجل  1 المتاح  2 الخميس  3 الاربعاء  4 الثلاثاء  5 الاثنين  6 الاحد
    7 استاذ المادة  8 الشعبة  9 اسم المادة  10 رقم المقرر  11 رمز القسم  12 م

The report does carry a labelled header row, but it has **12** cells against the
data's 13 — رمز المادة spans the department and number columns — so the labels do
not line up with the cells they describe and cannot be used to map them.  Columns
are therefore read positionally, and every row is **shape-validated** before it is
trusted: a row whose department, number, section label and serial do not match
their expected forms is rejected and counted.  A registrar column change surfaces
as ``skipped``, never as silently transposed data.
"""

from __future__ import annotations

import codecs
import email
import re
from dataclasses import dataclass
from typing import Any

_CELL_COUNT = 13
_I_REGISTERED, _I_CAPACITY = 0, 1
_I_THU, _I_WED, _I_TUE, _I_MON, _I_SUN = 2, 3, 4, 5, 6
_I_INSTRUCTOR, _I_SECTION, _I_COURSE_NAME = 7, 8, 9
_I_NUMBER, _I_DEPT, _I_SERIAL = 10, 11, 12

#: A department code is Latin letters ("CS", "MATH", "GRPH").
_DEPT_RE = re.compile(r"^[A-Za-z]{2,8}$")
#: A course number is digits, occasionally with a trailing letter.
_NUMBER_RE = re.compile(r"^[0-9]{1,4}[A-Za-z]?$")
#: A section label is a campus prefix then digits.  The prefix is one or two
#: letters: 984 of the 1148 global sections are "M27"/"F3", but 164 (14.3%) use a
#: two-letter campus — YF 87, YM 74, OF 2, OM 1.  A one-letter pattern silently
#: rejected every one of those.
_SECTION_RE = re.compile(r"^[A-Za-z]{1,2}[0-9]{1,3}$")
#: The report's own row counter.
_SERIAL_RE = re.compile(r"^[0-9]{1,5}$")
#: A row this close to 13 cells is a damaged data row worth counting; further
#: from it and the row is page chrome.
_NEAR_MISS_MIN, _NEAR_MISS_MAX = 10, 16

#: Rows carrying the operator's print header are noise, not data.
_HEADER_MARKER = "@taibahu.edu.sa"
#: The registrar prints an em dash for "no value".
_EMPTY_MARKERS = {"", "-", "--", "–", "—"}


@dataclass(frozen=True)
class FacultySectionRow:
    """One section as the registrar publishes it."""

    course_key: str
    section: str
    instructor: str
    course_name: str
    capacity: int | None
    registered: int | None
    meetings: dict[str, str]

    @property
    def has_instructor(self) -> bool:
        return bool(self.instructor)


@dataclass(frozen=True)
class ParseResult:
    rows: tuple[FacultySectionRow, ...]
    skipped: int
    duplicates: int

    @property
    def with_instructor(self) -> tuple[FacultySectionRow, ...]:
        return tuple(r for r in self.rows if r.has_instructor)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode(raw: bytes) -> str:
    """Decode the saved page.  ``.mhtml`` wraps the HTML in a MIME part."""
    head = raw[:200].lstrip()
    if head.startswith(b"From:") or b"Snapshot-Content-Location:" in raw[:400]:
        message = email.message_from_bytes(raw)
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_type() != "text/html":
                continue
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                codecs.lookup(charset)
            except LookupError:
                # The saved MHTML names a codec Python does not have.  That is
                # the operator's browser being odd, not a reason to abort.
                charset = "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        if parts:
            return "\n".join(parts)
    for encoding in ("utf-8", "cp1256"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _row_is_wellformed(cells: list[str]) -> bool:
    return (
        _DEPT_RE.match(cells[_I_DEPT]) is not None
        and _NUMBER_RE.match(cells[_I_NUMBER]) is not None
        and _SECTION_RE.match(cells[_I_SECTION]) is not None
        and _SERIAL_RE.match(cells[_I_SERIAL]) is not None
    )


def _to_row(cells: list[str]) -> FacultySectionRow:
    instructor = cells[_I_INSTRUCTOR]
    if instructor in _EMPTY_MARKERS:
        instructor = ""
    meetings = {
        day: cells[idx]
        for day, idx in (
            ("sun", _I_SUN),
            ("mon", _I_MON),
            ("tue", _I_TUE),
            ("wed", _I_WED),
            ("thu", _I_THU),
        )
        if cells[idx] not in _EMPTY_MARKERS
    }
    return FacultySectionRow(
        course_key=f"{cells[_I_DEPT]}{cells[_I_NUMBER]}".upper(),
        section=cells[_I_SECTION].upper(),
        instructor=instructor,
        course_name=cells[_I_COURSE_NAME],
        capacity=_int_or_none(cells[_I_CAPACITY]),
        registered=_int_or_none(cells[_I_REGISTERED]),
        meetings=meetings,
    )


def _mergeable_number(a: int | None, b: int | None) -> bool:
    """True when two printings of one figure can be reconciled.

    Equal values, or one of them empty (``0``/``None``) — the reprint artefact.
    Two different non-zero figures are a disagreement, not an artefact.
    """
    return a == b or not a or not b


def _merge_repeat(
    previous: FacultySectionRow, current: FacultySectionRow
) -> FacultySectionRow | None:
    """Reconcile two printings of one section.

    Returns the row to keep, or ``None`` when the two genuinely disagree.

    The only difference this absorbs is the one the report actually produces: a
    figure printed as ``0`` (or absent) on one pass and in full on another.  Two
    *different* real figures are a registrar contradiction, not a reprint, and an
    unconditional ``max()`` would have swallowed them — so ``_mergeable_number``
    requires one side to be empty.  A difference in instructor, course name or
    meetings is always a contradiction.
    """
    if previous == current:
        return previous
    if (
        previous.instructor != current.instructor
        or previous.course_name != current.course_name
        or previous.meetings != current.meetings
    ):
        return None
    if not _mergeable_number(previous.capacity, current.capacity) or not _mergeable_number(
        previous.registered, current.registered
    ):
        return None
    return FacultySectionRow(
        course_key=previous.course_key,
        section=previous.section,
        instructor=previous.instructor,
        course_name=previous.course_name,
        capacity=max(previous.capacity or 0, current.capacity or 0),
        registered=max(previous.registered or 0, current.registered or 0),
        meetings=previous.meetings,
    )


def parse_faculty_sections(raw: bytes) -> ParseResult:
    """Extract every well-formed section row from a saved faculty-sections page.

    The report is paginated for printing, so the same section appears several
    times; identical repeats are collapsed.

    One repeat is expected and benign: a section's capacity renders as ``0`` in
    one pass and as the real figure in another (40 of 412 on the 1448/1 male
    report).  Those are merged by keeping the larger capacity, and are **not**
    counted as contradictions — otherwise the counter would always be non-zero
    and would stop meaning anything.  ``duplicates`` therefore counts only a
    genuine disagreement: the same section published twice with a different
    instructor, course name or meeting pattern, which is a registrar-side
    contradiction the caller must see.
    """
    from bs4 import BeautifulSoup  # imported lazily: only the importer needs it

    soup = BeautifulSoup(_decode(raw), "html.parser")
    # ``dict`` preserves insertion order, so it is both the dedupe index and the
    # ordered result — no separate list to keep in step with it.
    seen: dict[tuple[str, str], FacultySectionRow] = {}
    skipped = 0
    duplicates = 0

    # Walk rows ONCE.  ``find_all`` is recursive and the report nests tables four
    # deep, so iterating tables and then their rows visited every row four times —
    # 1700 visits over 425 distinct <tr>.  The dedupe hid that for row data but
    # multiplied ``skipped`` and ``duplicates`` by four, turning both counters into
    # numbers no operator could act on.  ``recursive=False`` on the cells keeps a
    # cell that itself contains a table from contributing its descendants' cells to
    # the shape check.
    for tr in soup.find_all("tr"):
        cells = [
            _clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"], recursive=False)
        ]
        if len(cells) != _CELL_COUNT:
            # A row that is nearly the right width is a damaged data row (a
            # colspan collapses 13 cells to 12); count it.  Anything far off is
            # page chrome and is not worth reporting.
            if _NEAR_MISS_MIN <= len(cells) <= _NEAR_MISS_MAX:
                skipped += 1
            continue
        if any(_HEADER_MARKER in c for c in cells):
            continue
        if not _row_is_wellformed(cells):
            skipped += 1
            continue
        row = _to_row(cells)
        key = (row.course_key, row.section)
        previous = seen.get(key)
        if previous is None:
            seen[key] = row
            continue
        merged = _merge_repeat(previous, row)
        if merged is None:
            duplicates += 1
        else:
            seen[key] = merged

    return ParseResult(rows=tuple(seen.values()), skipped=skipped, duplicates=duplicates)


def parse_faculty_sections_file(path: str) -> ParseResult:
    with open(path, "rb") as handle:
        return parse_faculty_sections(handle.read())


def summarise(result: ParseResult) -> dict[str, Any]:
    """A small report for the command's output and for tests to assert on."""
    rows = result.rows
    with_instructor = result.with_instructor
    campuses: dict[str, int] = {}
    for row in rows:
        campuses[row.section[:1]] = campuses.get(row.section[:1], 0) + 1
    return {
        "sections": len(rows),
        "with_instructor": len(with_instructor),
        "distinct_instructors": len({r.instructor for r in with_instructor}),
        "distinct_courses": len({r.course_key for r in rows}),
        "campuses": campuses,
        "skipped_malformed": result.skipped,
        "contradictory_duplicates": result.duplicates,
    }
