"""Saved, aggregate programme evidence for department exam operations.

Capture this while resolving the exact scraped exam population. Export readers
must consume the saved snapshot, never reconstruct historic membership from
current student records. Teaching-section totals deliberately do not assert
which programmes occupy a split or shared exam room.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

from core.services.exam_sections import EXAM_ENROLLMENT_SOURCE

EXAM_OPERATIONS_SNAPSHOT_VERSION = 1


def build_exam_operations_snapshot(
    schedule_entries: list[dict], captured_sections: dict[str, list[dict]]
) -> dict:
    """Assemble canonical-course evidence without queries or student identifiers.

    ``captured_sections`` is populated by ``resolve_exam_section_enrollment``
    during its existing membership resolution. A blank programme remains an
    explicit unknown bucket rather than being silently dropped or guessed.
    """
    courses = {}
    for entry in sorted(schedule_entries, key=lambda row: row["course_code"]):
        code = entry["course_code"]
        sections = deepcopy(captured_sections[code])
        counts: Counter = Counter()
        for section in sections:
            for program, count in section["program_counts"].items():
                counts[(program, section["gender"])] += count
        courses[code] = {
            "course_identity": entry["course_identity"],
            "source_course_code": entry["source_course_code"],
            "course_name": entry.get("course_name", ""),
            "program_counts": [
                {"program": program, "gender": gender, "student_count": count}
                for (program, gender), count in sorted(counts.items())
            ],
            "sections": sections,
        }
    return {
        "version": EXAM_OPERATIONS_SNAPSHOT_VERSION,
        "enrollment_source": EXAM_ENROLLMENT_SOURCE,
        "courses": courses,
    }
