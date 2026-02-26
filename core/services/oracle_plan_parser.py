"""
Oracle Study Plan Parser (Service Module)
==========================================
Parses Oracle report exports (CSV/TSV/semicolon/pipe) for university
department study plans.  Handles ragged rows and deduplicates prerequisites.

This module is a pure library — no CLI, no I/O beyond reading the input file.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════

LEVEL_MAP: OrderedDict[str, tuple[str, int]] = OrderedDict(
    [
        ("FIRST", ("الأول", 1)),
        ("SECOND", ("الثاني", 2)),
        ("THIRD", ("الثالث", 3)),
        ("FOURTH", ("الرابع", 4)),
        ("FIFTH", ("الخامس", 5)),
        ("SIXTH", ("السادس", 6)),
        ("SEVENTH", ("السابع", 7)),
        ("EIGHTH", ("الثامن", 8)),
        ("NINTH", ("التاسع", 9)),
        ("TENTH", ("العاشر", 10)),
    ]
)

COURSE_KEYWORDS: list[str] = ["مقرر", "إختياري"]
MIN_COURSE_FIELDS: int = 41
CONTINUATION_FIELDS: int = 34

# Maps Oracle Arabic course-type labels → database type values.
COURSE_TYPE_MAP: dict[str, str] = {
    "مقرر من الخطة": "Mandatory",
    "إختياري حر": "Free Elective",
    "إختياري برنامج": "Program Elective",
    "إختياري جامعة": "University Elective",
}


# ═══════════════════════════════════════════════════════════════════════
#  Utility Functions
# ═══════════════════════════════════════════════════════════════════════


def detect_delimiter(first_line: str) -> str:
    """Auto-detect the delimiter used in the Oracle export."""
    candidates = [("\t", "TAB"), (";", "SEMI"), ("|", "PIPE"), (",", "COMMA")]
    best_delim, best_count = ",", 0
    for delim, _ in candidates:
        count = first_line.count(delim)
        if count > best_count:
            best_delim, best_count = delim, count
    if best_count == 0:
        raise ValueError("Cannot detect delimiter — file may not be a flat table")
    return best_delim


def strip_trailing_id(text: str) -> str:
    """Strip trailing Oracle system-ID numbers from Arabic names."""
    return re.sub(r"\s+\d+\s*$", "", text.strip())


def normalize_code(code: str) -> str:
    """Normalize a course code: UPPERCASE, no spaces."""
    return code.strip().upper().replace(" ", "")


def parse_prereq_field(raw_value: str, dept_code: str) -> str | None:
    """Convert Oracle prerequisite field to a normalized course code.

    Oracle stores ``'103-1'`` (course_num – system_id) + ``'CS'`` (dept).
    We produce ``'CS103'``.  Returns *None* for empty / dash values.
    """
    raw_value = raw_value.strip()
    dept_code = dept_code.strip()
    if not raw_value or raw_value == "-":
        return None
    course_num = raw_value.split("-")[0]
    return normalize_code(f"{dept_code}{course_num}") if dept_code else normalize_code(course_num)


def map_course_type(oracle_type: str) -> str:
    """Map an Oracle Arabic course-type label to the DB ``type`` value."""
    oracle_type = oracle_type.strip()
    if oracle_type in COURSE_TYPE_MAP:
        return COURSE_TYPE_MAP[oracle_type]
    if "إختياري" in oracle_type:
        return "Elective"
    return "Mandatory"


# ═══════════════════════════════════════════════════════════════════════
#  Core Parser
# ═══════════════════════════════════════════════════════════════════════


def parse_oracle_plan(filepath: str, encoding: str = "windows-1256") -> dict[str, Any]:
    """Parse an Oracle study plan export file.

    Args:
        filepath: Path to the Oracle export (CSV, TSV, semicolon, or pipe).
        encoding: File encoding (default ``windows-1256`` for Arabic Oracle).

    Returns:
        A dict with keys ``metadata``, ``levels``, ``summary``, ``warnings``.
    """
    with open(filepath, encoding=encoding) as f:
        content = f.read()

    lines = [line.rstrip("\r") for line in content.strip().split("\n")]
    if not lines:
        raise ValueError("Empty file")

    delim = detect_delimiter(lines[0])
    warnings: list[str] = []

    # ── Metadata from first line ──
    f0 = lines[0].split(delim)
    metadata: dict[str, str] = {
        "college_ar": strip_trailing_id(f0[3]) if len(f0) > 3 else "",
        "dept_ar": strip_trailing_id(f0[5]) if len(f0) > 5 else "",
        "major_ar": strip_trailing_id(f0[7]) if len(f0) > 7 else "",
        "study_type": f0[1].strip().split(" ")[0] if len(f0) > 1 else "",
    }

    # ── Line-by-line parsing ──
    courses: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in lines:
        fields = line.split(delim)
        n = len(fields)

        # COURSE LINE: 41+ fields with keyword at [29]
        if n >= MIN_COURSE_FIELDS and any(kw in fields[29].strip() for kw in COURSE_KEYWORDS):
            if current is not None:
                courses.append(current)

            level_en = fields[11].strip()
            level_ar, level_num = LEVEL_MAP.get(level_en, (level_en, 0))
            credits_raw = fields[35].strip()

            try:
                credits = int(credits_raw)
            except ValueError:
                credits = 0
                warnings.append(f"Non-numeric credits '{credits_raw}' for {fields[36]}{fields[39]}")

            raw_prereqs: list[str] = []
            prereq_label = fields[42].strip() if n > 42 else ""
            if prereq_label != "لايوجد" and n > 44:
                p = parse_prereq_field(
                    fields[44].strip(),
                    fields[45].strip() if n > 45 else "",
                )
                if p:
                    raw_prereqs.append(p)

            current = {
                "level_en": level_en,
                "level_ar": level_ar,
                "level_number": level_num,
                "seq": fields[37].strip(),
                "code": normalize_code(f"{fields[36].strip()}{fields[39].strip()}"),
                "code_ar": f"{fields[30].strip()} {fields[31].strip()}",
                "en_name": fields[40].strip().upper(),
                "ar_name": fields[38].strip(),
                "credits": credits,
                "delivery": fields[41].strip(),
                "course_type": fields[29].strip(),
                "_prereqs_raw": raw_prereqs,
                "prereqs": [],
            }
            continue

        # CONTINUATION LINE: exactly 34 fields, not a total row
        if (
            n == CONTINUATION_FIELDS
            and current is not None
            and not any("إجمالى" in f for f in fields)
        ):
            p = parse_prereq_field(
                fields[29].strip(),
                fields[30].strip() if n > 30 else "",
            )
            if p:
                current["_prereqs_raw"].append(p)
            continue

    # Flush last course
    if current is not None:
        courses.append(current)

    # ── Deduplicate prerequisites (Oracle doubles every prereq) ──
    for c in courses:
        seen: set[str] = set()
        c["prereqs"] = [
            p
            for p in c["_prereqs_raw"]
            if p not in seen and not seen.add(p)  # type: ignore[func-returns-value]
        ]
        del c["_prereqs_raw"]

    # ── Validate: prereqs should reference courses in this plan ──
    all_codes = {c["code"] for c in courses}
    for c in courses:
        for p in c["prereqs"]:
            if p not in all_codes:
                warnings.append(f"{c['code']}: prerequisite '{p}' not found in plan")

    # ── Group by level (preserving Oracle order) ──
    levels: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for c in courses:
        key = c["level_en"]
        if key not in levels:
            levels[key] = {
                "level_en": key,
                "level_ar": c["level_ar"],
                "level_number": c["level_number"],
                "courses": [],
                "total_credits": 0,
            }
        levels[key]["courses"].append(c)
        levels[key]["total_credits"] += c["credits"]

    return {
        "metadata": metadata,
        "levels": levels,
        "summary": {
            "total_courses": len(courses),
            "total_credits": sum(c["credits"] for c in courses),
            "total_levels": len(levels),
        },
        "warnings": warnings,
    }
