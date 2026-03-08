"""
Oracle Study Plan Parser (Service Module)
==========================================
Parses Oracle study-plan text exports using regex-based extraction.
Splits by level markers (المستوى:), extracts courses with their
prerequisites, credits, and delivery method.

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

# Maps Oracle Arabic course-type labels → database type values.
COURSE_TYPE_MAP: dict[str, str] = {
    "مقرر من الخطة": "Mandatory",
    "إختياري حر": "Free Elective",
    "إختياري برنامج": "Program Elective",
    "إختياري تخصص": "Program Elective",
    "إختياري جامعة": "University Elective",
    "مقرر حر": "Free Elective",
}

# Main regex to extract course rows from Oracle text.
# Groups: 1=type, 2=ar_dept, 3=course_num, 4-6=nums, 7=credits,
#         8=dept_code, 9=seq, 10=ar_name, 11=num, 12=en_name,
#         13=delivery, 14=prereq_indicator, 15=completed_hours, 16=prereq_codes
COURSE_PATTERN = re.compile(
    r"(مقرر من الخطة|إختياري جامعة|إختياري تخصص|مقرر حر|إختياري\s*جامعة|إختياري\s*تخصص)\s+"
    r"([أ-ي]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([A-Z]+)\s+(\d+)\s+"
    r"([أ-ي\s\:\(\)\-\w]+?)\s+(\d+)\s+([A-Z0-9\s\:\(\)\&,\-]+?)\s+"
    r"(تعليم\s*حضوري|تعليم\s*إلكتروني)\s+"
    r"(لايوجد|الساعات و المواد)\s+"
    r"(\d+)\s+"
    r"([A-Z0-9\s\-]*)?"
)

# Level header inside each chunk after splitting by المستوى:
LEVEL_HEADER_RE = re.compile(r":\s*Level\s*([أ-ي]+)\s+([A-Z]+)")

# Metadata patterns (found in the text header before the first level)
META_COLLEGE_RE = re.compile(r"(?:الكلية|كلية)\s*[:\-]?\s*(.+?)(?:\n|$)")
META_DEPT_RE = re.compile(r"(?:القسم|قسم)\s*[:\-]?\s*(.+?)(?:\n|$)")
META_MAJOR_RE = re.compile(r"(?:التخصص|تخصص)\s*[:\-]?\s*(.+?)(?:\n|$)")
META_STUDY_RE = re.compile(r"(?:نوع الدراسة|نظام الدراسة)\s*[:\-]?\s*(.+?)(?:\n|$)")


# ═══════════════════════════════════════════════════════════════════════
#  Utility Functions
# ═══════════════════════════════════════════════════════════════════════


def normalize_code(code: str) -> str:
    """Normalize a course code: UPPERCASE, no spaces."""
    return code.strip().upper().replace(" ", "")


def map_course_type(oracle_type: str) -> str:
    """Map an Oracle Arabic course-type label to the DB ``type`` value."""
    oracle_type = oracle_type.strip()
    if oracle_type in COURSE_TYPE_MAP:
        return COURSE_TYPE_MAP[oracle_type]
    if "إختياري" in oracle_type:
        return "Elective"
    return "Mandatory"


def _extract_metadata(header_text: str) -> dict[str, str]:
    """Extract college, department, major, study type from the text header."""
    metadata: dict[str, str] = {
        "college_ar": "",
        "dept_ar": "",
        "major_ar": "",
        "study_type": "",
    }
    m = META_COLLEGE_RE.search(header_text)
    if m:
        metadata["college_ar"] = m.group(1).strip()
    m = META_DEPT_RE.search(header_text)
    if m:
        metadata["dept_ar"] = m.group(1).strip()
    m = META_MAJOR_RE.search(header_text)
    if m:
        metadata["major_ar"] = m.group(1).strip()
    m = META_STUDY_RE.search(header_text)
    if m:
        metadata["study_type"] = m.group(1).strip()
    return metadata


def _extract_prereqs(match: re.Match) -> list[str]:
    """Extract prerequisite course codes from a regex match.

    Handles both hour-based requirements and specific course prerequisites.
    """
    prereqs: list[str] = []
    req_type = match.group(14).strip()
    completed_hours = match.group(15).strip()
    prereq_string = match.group(16).strip() if match.group(16) else ""

    # Hour-based prerequisite (e.g. "must complete 144 hours")
    if completed_hours != "0":
        prereqs.append(f"{completed_hours} (Hours)")

    # Specific course code prerequisites
    if req_type != "لايوجد":
        prereq_matches = re.findall(r"(\d+)-\d+\s+([A-Z]+)", prereq_string)
        for num, code in prereq_matches:
            prereqs.append(normalize_code(f"{code}{num}"))

    return prereqs


# ═══════════════════════════════════════════════════════════════════════
#  Core Parser
# ═══════════════════════════════════════════════════════════════════════


def parse_oracle_plan(
    filepath: str | None = None,
    encoding: str = "windows-1256",
    *,
    content: str | None = None,
) -> dict[str, Any]:
    """Parse an Oracle study plan export file.

    Args:
        filepath: Path to the Oracle text export.
        encoding: File encoding (default ``windows-1256`` for Arabic Oracle).
        content: Raw file content as a string (if provided, *filepath* is ignored).

    Returns:
        A dict with keys ``metadata``, ``levels``, ``summary``, ``warnings``.
    """
    if content is None:
        if not filepath:
            raise ValueError("Either filepath or content must be provided")
        with open(filepath, encoding=encoding) as f:
            content = f.read()

    if not content.strip():
        raise ValueError("Empty file")

    warnings: list[str] = []

    # ── Split by level markers ──
    level_chunks = content.split("المستوى:")

    # Header is everything before the first level marker
    header_text = level_chunks[0] if level_chunks else ""
    metadata = _extract_metadata(header_text)

    # ── Parse courses from each level chunk ──
    courses: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    seq_counter = 0

    for chunk in level_chunks[1:]:
        # Extract level name from chunk header
        level_match = LEVEL_HEADER_RE.search(chunk)
        level_en = level_match.group(2).strip().upper() if level_match else "UNKNOWN"
        level_ar, level_num = LEVEL_MAP.get(level_en, (level_en, 0))

        for match in COURSE_PATTERN.finditer(chunk):
            dept_code = match.group(8).strip()
            course_num = match.group(3).strip()
            code = normalize_code(f"{dept_code}{course_num}")

            # Deduplicate courses (Oracle can repeat them)
            if code in seen_codes:
                continue
            seen_codes.add(code)

            seq_counter += 1
            credits_raw = match.group(7).strip()
            try:
                credits = int(credits_raw)
            except ValueError:
                credits = 0
                warnings.append(f"Non-numeric credits '{credits_raw}' for {code}")

            ar_name = match.group(10).strip()
            en_name = match.group(12).strip().upper()
            course_type = match.group(1).strip()
            delivery = match.group(13).strip()
            ar_dept = match.group(2).strip()

            prereqs = _extract_prereqs(match)

            courses.append(
                {
                    "level_en": level_en,
                    "level_ar": level_ar,
                    "level_number": level_num,
                    "seq": str(seq_counter),
                    "code": code,
                    "code_ar": f"{ar_dept} {course_num}",
                    "en_name": en_name,
                    "ar_name": ar_name,
                    "credits": credits,
                    "delivery": delivery,
                    "course_type": course_type,
                    "prereqs": prereqs,
                }
            )

    if not courses and not level_chunks[1:]:
        warnings.append("No level markers (المستوى:) found in file")

    # ── Validate: prereqs should reference courses in this plan ──
    all_codes = {c["code"] for c in courses}
    for c in courses:
        for p in c["prereqs"]:
            if "(Hours)" not in p and p not in all_codes:
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
