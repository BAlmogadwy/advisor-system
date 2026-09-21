"""Display-only review metadata from the authoritative exam population.

This payload describes course relationships, not scheduling decisions. Its
edges always come from the complete conflict graph before any tiny-course
relaxation, so approving a scheduling exception never hides shared students.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from core.services.exam_sections import EXAM_ENROLLMENT_SOURCE

EXAM_REVIEW_VERSION = 1


def build_exam_review(conflicts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Publish aggregate overlaps from the existing full conflict graph.

    Callers supply ``build_conflict_graph`` edges after authoritative enrollment
    and course selection have been applied. ``course_a`` and ``course_b`` are
    the schedule's unique display keys, resolved through each schedule entry's
    ``course_identity``; they must never be reduced to bare source course codes.
    No student identifiers or curriculum-derived enrollment are exposed here.
    """
    return {
        "version": EXAM_REVIEW_VERSION,
        "enrollment_source": EXAM_ENROLLMENT_SOURCE,
        "student_overlaps": [
            {
                "course_a": edge["course_a"],
                "course_b": edge["course_b"],
                "shared_students": int(edge["shared"]),
            }
            for edge in sorted(conflicts, key=lambda edge: (edge["course_a"], edge["course_b"]))
        ],
    }
