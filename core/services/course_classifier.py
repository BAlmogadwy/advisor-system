import math
from typing import TypedDict

from core.services.student_helpers import normalize_code as global_normalize_code

PASSING_LETTER_GRADES = frozenset(
    {
        "A",
        "A+",
        "B",
        "B+",
        "C",
        "C+",
        "D",
        "D+",
        "NP",
        "\u0623",
        "\u0623+",
        "\u0628",
        "\u0628+",
        "\u062c",
        "\u062c+",
        "\u062f",
        "\u062f+",
        "\u0646\u062f",
    }
)
FAILING_LETTER_GRADES = frozenset(
    {
        "F",
        "FAIL",
        "FAILED",
        "DN",
        "NF",
        "\u062d",
        "\u0647\u062f",
        "\u0647",
    }
)
NON_OUTCOME_LETTER_GRADES = frozenset(
    {
        "IP",
        "IC",
        "W",
        "E",
        "\u0645",
        "\u0644",
        "\u0639",
        "\u0639\u0641",
    }
)
RECOGNISED_LETTER_GRADES = (
    PASSING_LETTER_GRADES | FAILING_LETTER_GRADES | NON_OUTCOME_LETTER_GRADES | {"TRS"}
)


class CourseResult(TypedDict):
    outcome: str | None
    grade: str
    mark: float | None
    has_snapshot: bool


def parse_course_result(course: dict) -> CourseResult:
    """Return the explicit academic result carried by one study-plan row.

    A numeric mark is stronger evidence than the letter column because the
    portal has historically returned combinations such as ``F`` with a passing
    numeric mark. A wholly blank result is not a new snapshot, so callers can
    preserve the last known pair. Unknown nonblank values fail closed instead of
    silently demoting a course or splicing fields from two different attempts.
    """
    raw_grade = str(course.get("letter") or "").strip().upper().replace("\u0640", "")
    raw_mark = str(course.get("marks") or "").strip()

    blank_tokens = {"", "-", "--", "\u2014", "N/A"}
    grade = "" if raw_grade in blank_tokens else raw_grade
    if grade and grade not in RECOGNISED_LETTER_GRADES:
        raise ValueError(f"Unrecognised course grade: {raw_grade!r}")

    mark: float | None = None
    has_mark = raw_mark.upper() not in blank_tokens
    if has_mark:
        try:
            candidate = float(raw_mark)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid course mark: {raw_mark!r}") from exc
        if not math.isfinite(candidate) or not 0 <= candidate <= 100:
            raise ValueError(f"Course mark is outside 0..100: {raw_mark!r}")
        mark = candidate

    has_snapshot = bool(grade or has_mark)

    outcome: str | None = None
    if grade == "TRS":
        outcome = "passed"
    elif mark is not None:
        outcome = "passed" if mark >= 60 else "failed"
    elif grade in PASSING_LETTER_GRADES:
        outcome = "passed"
    elif grade in FAILING_LETTER_GRADES:
        outcome = "failed"

    return {
        "outcome": outcome,
        "grade": grade,
        "mark": mark,
        "has_snapshot": has_snapshot,
    }


def classify_courses(study_plan_courses: list[dict], timetable_courses: set[str]) -> dict:
    passed: list[str] = []
    studying: list[str] = []
    failed: list[str] = []
    not_taken: list[str] = []

    normalized_timetable = {global_normalize_code(code) for code in timetable_courses}

    for course in study_plan_courses:
        dept = course["dept"]
        number = course["no"]
        course_code = global_normalize_code(f"{dept} {number}")
        result = parse_course_result(course)

        if result["outcome"] == "passed":
            passed.append(course_code)
        elif course_code in normalized_timetable:
            studying.append(course_code)
        elif result["outcome"] == "failed":
            failed.append(course_code)
        else:
            not_taken.append(course_code)

    return {
        "passed": passed,
        "studying": studying,
        "failed": failed,
        "not_taken": not_taken,
    }
