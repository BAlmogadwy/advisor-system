"""Extract the academic calendar from the captured university page.

The calendar is the ONLY source that turns the student guide's relative offsets
("قبل بدء الدراسة بأسبوع", "منتصف الفصل السابق") into dates. Without it every
deadline question is unanswerable; with it, they become answerable for exactly the
term the capture covers — and no further.

That last point is the trap this extractor is built around. The page is a LIVE
university page showing one term. It is not an archive and it is not a PDF. So:

  * the capture is a snapshot with a date, recorded as such;
  * `covers` names the exact term, and nothing outside it may be answered;
  * every row keeps both the Hijri and Gregorian strings AS PRINTED, because the
    dual-calendar mapping is the university's, not ours to compute.

Usage:  python policies/tools/extract_calendar.py
"""

from __future__ import annotations

import hashlib
import html
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sources" / "TU_ACADEMIC_CALENDAR_1448_T1.html"
OUT = ROOT / "calendar" / "calendar_1448_t1.yaml"

EXPECTED_SHA256 = "d6297a4dd1eea5f4328274247c2a82cdb1dd9d20c1f2fae01356bebb768ad05d"

# "من 03-ربيع الأول-1448 - 16-أغسطس-2026"
# The month segment must allow SPACES: half the Hijri months are two words
# (ربيع الأول, ربيع الآخر, جمادى الأولى, جمادى الآخرة). Excluding spaces here
# silently drops 25 of 37 rows while the other 12 parse fine — a partial parse
# that looks like a working parse.
DATE_RE = re.compile(r"(\d{1,2}-[^-]+?-\d{4})\s*-\s*(\d{1,2}-[^-]+?-\d{4})")

# Rows whose audience is staff, not students. Kept (the source contains them) but
# marked, so a student-facing answer never surfaces "submit your course report".
STAFF_MARKERS = (
    "أعضاء هيئة التدريس",
    "من قبل الأقسام",
    "تقرير البرنامج",
    "تقرير المقرر",
    "إسناد شعب",
    "إغلاق أعمال",
)


def cell_text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def split_dates(raw: str) -> dict[str, str | None]:
    """Keep both calendars exactly as printed. Never compute one from the other."""
    m = DATE_RE.search(raw)
    if not m:
        return {"hijri": None, "gregorian": None, "as_printed": raw or None}
    return {"hijri": m.group(1), "gregorian": m.group(2), "as_printed": raw}


def main() -> int:
    if not SOURCE.exists():
        sys.exit(f"calendar source not found: {SOURCE}")
    actual = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if actual != EXPECTED_SHA256:
        sys.exit(
            f"calendar source hash mismatch\n  expected {EXPECTED_SHA256}\n  actual   {actual}"
        )

    doc = SOURCE.read_text(encoding="utf-8", errors="replace")
    table = re.search(r"<table.*?</table>", doc, re.S)
    if not table:
        sys.exit("no table found in the captured page — the page layout may have changed")

    events = []
    for row in re.findall(r"<tr.*?</tr>", table.group(0), re.S):
        cells = [cell_text(c) for c in re.findall(r"<t[dh].*?</t[dh]>", row, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 6 or cells[0] == "الحدث":
            continue
        title, weeks, start_raw, end_raw, kind, state = cells[:6]
        events.append(
            {
                "event_ar": title,
                "teaching_weeks": None if weeks == "-" else weeks,
                "starts": split_dates(start_raw),
                "ends": split_dates(end_raw),
                "category_ar": kind,
                "state_at_capture_ar": state,
                "audience": "STAFF" if any(m in title for m in STAFF_MARKERS) else "STUDENT",
            }
        )

    payload = {
        "document_id": "TU_ACADEMIC_CALENDAR_1448_T1",
        "title_ar": "التقويم الأكاديمي - جامعة طيبة",
        "authority_level": "OFFICIAL_ACADEMIC_CALENDAR",
        "covers": {
            "academic_year_hijri": "1448",
            "term": 1,
            "term_ar": "الفصل الدراسي الأول",
            "gregorian_span": "2026-08 .. 2027-01",
        },
        "scope_warning": (
            "SINGLE TERM ONLY. This capture covers الفصل الدراسي الأول 1448هـ. It says "
            "nothing about term 2, the summer term, or any other academic year. A deadline "
            "question outside that span is UNANSWERABLE from this source — do not "
            "extrapolate a date from one term to another."
        ),
        "capture": {
            "captured_at": "2026-08-01",
            "captured_from": "live university web page (not an archival PDF)",
            "source_file": "sources/TU_ACADEMIC_CALENDAR_1448_T1.html",
            "source_file_sha256": EXPECTED_SHA256,
            "note": (
                "A live page can change without notice. Every event was marked 'القادمة' "
                "(upcoming) at capture, which is consistent with a capture date before the "
                "earliest listed event (2026-08-09). Re-capture before relying on it in a "
                "later term."
            ),
        },
        "date_handling": {
            "dual_calendar": "Hijri and Gregorian are both recorded AS PRINTED by the university.",
            "never_compute": (
                "Do not derive one calendar from the other. Hijri-Gregorian alignment is an "
                "observational matter the university has already decided for these rows; a "
                "computed conversion can differ by a day and would silently move a deadline."
            ),
        },
        "verification": {
            "status": "EXTRACTED",
            "extracted_by": "policies/tools/extract_calendar.py",
            "extracted_at": "2026-08-01",
            "source_verified_by": None,
            "domain_reviewed_by": None,
            "authority_approved_by": None,
        },
        "counts": {
            "events": len(events),
            "student_facing": sum(1 for e in events if e["audience"] == "STUDENT"),
            "staff_facing": sum(1 for e in events if e["audience"] == "STAFF"),
        },
        "events": events,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    unparsed = [
        e["event_ar"]
        for e in events
        if e["starts"]["gregorian"] is None or e["ends"]["gregorian"] is None
    ]
    print(f"extracted {len(events)} events -> {OUT}")
    print(f"  student-facing: {payload['counts']['student_facing']}")
    print(f"  staff-facing:   {payload['counts']['staff_facing']}")
    print(f"  unparsed dates: {len(unparsed)}")
    if unparsed:
        # Fail loudly. A calendar with silently missing dates is worse than no
        # calendar: the rules that cite it would report "no deadline" as if the
        # source were silent, when in fact the parse dropped it.
        for title in unparsed:
            print(f"    ! {title[:90]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
