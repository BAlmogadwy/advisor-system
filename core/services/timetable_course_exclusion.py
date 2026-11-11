"""Which courses never appear on a timetable board (classic Generate's rule).

Certain courses are not delivered in a regular classroom setting, so classic
Generate never budgets them. Three exclusion layers:

1. ``EXCLUDED_COURSE_CODES`` - explicit course codes (currently empty; can be
   populated with codes like "FE1", "FE2" for free-elective placeholders).
2. ``EXCLUDED_SUFFIXES`` - course numbers ending with these digits: the 101
   university-level courses (managed centrally) and the capstone /
   graduation-project sequences (e.g. CS490, CS491, CS492).
3. ``EXCLUDED_NAME_KEYWORDS`` / ``EXCLUDED_NAME_PATTERNS_PROJECT`` - fuzzy match
   against the course name (English and Arabic) to catch training,
   cooperative, internship, and graduation-project courses. The name is the
   student's own programme plan's name for that code (the catalogue
   description only when the plan has none): one display code can name two
   courses, e.g. CYB386 is CYP's internship but CYP2's machine-learning class.

``is_generate_excluded_course`` applies all three layers. It lives here, not in
``timetable_generate``, so the pages that set a programme's weekly meetings for
its courses (Section Planning) and the curriculum importers can ask the same
question without importing Generate. The new scheduler's own rule
(``scheduler.intake.is_unscheduled_course``) is a subset of layer 3 - pinned by
a test, because core never imports scheduler code.
"""

from __future__ import annotations

from core.services.student_helpers import normalize_code

EXCLUDED_COURSE_CODES: frozenset[str] = frozenset()  # None currently hardcoded
EXCLUDED_SUFFIXES = ("101", "490", "491", "492")
EXCLUDED_NAME_KEYWORDS = frozenset(
    {
        "graduation",
        "practical training",
        "training",
        "cooperative",
        "co-operative",
        "coop",
        "internship",
        "تخرج",
        "مشروع تخرج",
        "تدريب",
    }
)
# Patterns that indicate a capstone/project course.
# Note: plain "project" is intentionally omitted to avoid false-positives
# like "Project Management" — only multi-word capstone patterns are listed.
EXCLUDED_NAME_PATTERNS_PROJECT = ("project i", "project ii", "graduation project")


def is_generate_excluded_course(code: str, course_name: object) -> bool:
    """Determine whether a course should be excluded from timetable scheduling.

    A course is excluded if **any** of the following are true:

    1. Its normalised code is in the ``EXCLUDED_COURSE_CODES`` set.
    2. The numeric portion of the code ends with one of the
       ``EXCLUDED_SUFFIXES`` (490, 491, 492 — capstone sequences).
    3. ``course_name`` contains any of the ``EXCLUDED_NAME_KEYWORDS`` or
       ``EXCLUDED_NAME_PATTERNS_PROJECT``.

    Args:
        code: Raw course code string (e.g. "CS 490", "ARAB101").
        course_name: The name of THIS course identity -- the student's
            programme plan name for ``code`` (catalogue description only
            when the plan has none). Never looked up by the bare code: a
            display code is not a course identity.

    Returns:
        True if the course should be kept off the timetable, False otherwise.
    """
    c = normalize_code(code)

    # Layer 1: explicit code blacklist
    if c in EXCLUDED_COURSE_CODES:
        return True

    # Layer 2: numeric suffix check (e.g. "490" in "CS490")
    digits = "".join(ch for ch in c if ch.isdigit())
    if digits.endswith(EXCLUDED_SUFFIXES):
        return True

    # Layer 3: keyword / pattern match against this identity's name
    name = str(course_name or "").lower()
    if name:
        if any(kw in name for kw in EXCLUDED_NAME_KEYWORDS):
            return True
        if any(pat in name for pat in EXCLUDED_NAME_PATTERNS_PROJECT):
            return True
    return False
