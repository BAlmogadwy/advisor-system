"""A programme's default weekly meetings for its courses: the read side.

Each programme votes on how a 3-credit course of its study plan meets each week
(``ProgrammeRequirement.meeting_pattern``: ``""`` not set, ``"standard"`` or
``"2x100"``; owner, 2026-11-10: set per programme, both campuses alike). This
module turns those rows into the rule's input and answers, for a scenario's
courses, what its programmes decided. The rule itself is pure and lives in
``timetable_meeting_shape`` (the new scheduler reads it too); this side only
reads the database and names things for the pages.

Who decides a scenario's course: the study-plan rows of the SCENARIO'S
programmes that teach that course identity (CODE::NAME). Plan ownership, not
demand: it is the one definition classic Generate, the scheduler's intake and
the workspace can all compute from the same rows (their demand differs:
withdrawn students, elective resolution, unpublished slots).

Which rows may vote at all (:func:`ineligibility`): a 3-credit, named course
that is not an elective slot, not left off the timetable by classic Generate
(``timetable_course_exclusion``) and not an English course with a fixed weekly
bundle. Section Planning offers only those; the importers keep a vote only on
them; the integrity check lists any other row that still holds one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.models import DeliveryBoard, ProgrammeRequirement, TimetableScenario
from core.services.course_identity import planner_course_key
from core.services.student_helpers import is_elective_slot, normalize_code
from core.services.timetable_course_exclusion import is_generate_excluded_course
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_meeting_shape import (
    AGREE,
    PROGRAMME_NOT_SET,
    PROGRAMME_STANDARD,
    SHAPES,
    ProgrammeRow,
    ProgrammeShape,
    budget_form,
    department_budget_value,
    programme_shape_for,
)

#: The ProgrammeRequirement columns the rule reads.
ROW_FIELDS = ("program", "course_code", "course_name", "credit_hours", "type", "meeting_pattern")

#: Why a study-plan row cannot vote on its weekly meetings.
NOT_THREE_CREDITS = "not_three_credits"
NOT_NAMED = "not_named"
PLACEHOLDER = "placeholder"
EXCLUDED = "excluded"
POLICY = "policy"

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _credits(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def ineligibility(row: Mapping[str, Any]) -> str | None:
    """Why this study-plan row cannot vote on its weekly meetings, or None.

    ``row`` is a ProgrammeRequirement as a mapping (``.values(*ROW_FIELDS)``).
    """
    if _credits(row.get("credit_hours")) != 3:
        return NOT_THREE_CREDITS
    name = str(row.get("course_name") or "").strip()
    if not name:
        return NOT_NAMED
    if is_elective_slot(row.get("type")):
        return PLACEHOLDER
    code = row.get("course_code")
    if is_generate_excluded_course(str(code or ""), name):
        return EXCLUDED
    if get_course_meeting_policy(code) is not None:
        return POLICY
    return None


def row_identity(row: Mapping[str, Any]) -> str:
    """The course identity (CODE::NAME) a study-plan row names."""
    return planner_course_key(row.get("course_code"), row.get("course_name"))


def pre_key(rows: Iterable[Mapping[str, Any]]) -> list[ProgrammeRow]:
    """Study-plan rows as the rule's input (one per programme and course)."""
    return [
        ProgrammeRow(
            programme=str(row.get("program") or "").strip().upper(),
            identity=row_identity(row),
            meeting_pattern=str(row.get("meeting_pattern") or ""),
            credit_hours=row.get("credit_hours"),
            named=bool(str(row.get("course_name") or "").strip()),
        )
        for row in rows
    ]


def normalise_key(key: object) -> str:
    """A budget's course key in the form ``planner_course_key`` gives it."""
    text = str(key or "").strip()
    if "::" in text:
        code, name = text.split("::", 1)
        return planner_course_key(code, name)
    return normalize_code(text)


def scenario_programmes(scenario: TimetableScenario) -> list[str]:
    """The programmes a scenario was generated for, upper-cased and sorted.

    ``scenario.programs`` when it has them; an older scenario has ``[]`` there,
    so its boards' programme labels ("DS,AI,AI2,DS2") stand in; else none.
    """
    programmes = {str(p).strip().upper() for p in (scenario.programs or []) if str(p).strip()}
    if not programmes:
        labels = DeliveryBoard.objects.filter(scenario=scenario).values_list("program", flat=True)
        for label in labels:
            programmes.update(p.strip().upper() for p in str(label or "").split(",") if p.strip())
    return sorted(programmes)


def load_rows(programmes: Iterable[str]) -> list[ProgrammeRow]:
    """Every study-plan row of these programmes, as the rule's input (one query)."""
    wanted = sorted({str(p).strip().upper() for p in programmes if str(p).strip()})
    if not wanted:
        return []
    return pre_key(ProgrammeRequirement.objects.filter(program__in=wanted).values(*ROW_FIELDS))


def resolve_for_keys(
    keys: Iterable[str], programmes: Iterable[str], *, rows: list[ProgrammeRow] | None = None
) -> dict[str, ProgrammeShape]:
    """The programmes' decision for each course key, over these programmes' rows.

    One ProgrammeRequirement query (none when ``rows`` are given, or when there
    are no programmes: every key is then undecided).
    """
    rows = load_rows(programmes) if rows is None else rows
    return {key: programme_shape_for(normalise_key(key), rows) for key in keys}


def budget_value(
    decision: ProgrammeShape, *, course_key: object, course_code: object, credit_hours: object
) -> str:
    """The ``meeting_pattern`` a scenario budget takes from this decision."""
    policy = get_course_meeting_policy(course_code, course_key=course_key)
    return department_budget_value(decision, credit_hours, has_policy=policy is not None)


def is_drift(decision: ProgrammeShape, budget_row: Mapping[str, Any]) -> bool:
    """Does a scenario's course differ from what its programmes agreed on?

    Only when they agreed on a value someone SET (an unset programme decides
    nothing, so a scenario's own 2 x 100 over unset programmes is not drift),
    that value is a choice for this row (its ``meeting_pattern_choices``: no
    English bundle, the right credit hours), and the row holds something else.
    """
    if decision.status != AGREE or decision.meeting_pattern == PROGRAMME_NOT_SET:
        return False
    wanted = budget_form(decision.meeting_pattern)
    choices = budget_row.get("meeting_pattern_choices") or []
    return wanted in choices and wanted != str(budget_row.get("meeting_pattern") or "")


def _lengths_text(minutes: Iterable[int], *, arabic: bool) -> str:
    lengths = sorted(int(m) for m in minutes)
    if not lengths:
        return ""
    if len(lengths) > 1 and len(set(lengths)) == 1:
        text = f"{len(lengths)} × {lengths[0]}"
    else:
        text = " + ".join(str(m) for m in lengths)
    return f"{text.translate(_ARABIC_DIGITS)} دقيقة" if arabic else f"{text} min"


def standard_minutes(credit_hours: object = 3) -> list[int]:
    """The credit hours' own weekly meetings (3 credits: 75 and 75 minutes)."""
    from core.services.timetable_autoplace import MEETING_PATTERNS

    credits = _credits(credit_hours) or 3
    return list(MEETING_PATTERNS.get(credits, MEETING_PATTERNS[3]))


def programme_pattern_label(
    value: object, credit_hours: object = 3, *, arabic: bool = False
) -> str:
    """A programme's vote in words: "Not set", "Standard (2 × 75 min)", "2 × 100 min"."""
    text = str(value or "").strip().lower()
    if text in SHAPES:
        shape = SHAPES[text]
        return shape.label_ar if arabic else shape.label
    if text == PROGRAMME_STANDARD:
        minutes = _lengths_text(standard_minutes(credit_hours), arabic=arabic)
        return f"قياسي ({minutes})" if arabic else f"Standard ({minutes})"
    return "غير محدد" if arabic else "Not set"


def pattern_choice_options(credit_hours: object = 3) -> list[dict[str, str]]:
    """The three votes with their words, for the pages (minutes from the engine)."""
    return [
        {
            "value": value,
            "label": programme_pattern_label(value, credit_hours),
            "label_ar": programme_pattern_label(value, credit_hours, arabic=True),
        }
        for value in (PROGRAMME_NOT_SET, PROGRAMME_STANDARD, *SHAPES)
    ]


def decision_block(decision: ProgrammeShape, credit_hours: object = 3) -> dict[str, Any]:
    """The decision as the pages read it (``programme_meeting_pattern``)."""
    agreed = decision.status == AGREE and decision.meeting_pattern != PROGRAMME_NOT_SET
    return {
        "status": decision.status,
        "meeting_pattern": decision.meeting_pattern,
        "label": programme_pattern_label(decision.meeting_pattern, credit_hours) if agreed else "",
        "label_ar": (
            programme_pattern_label(decision.meeting_pattern, credit_hours, arabic=True)
            if agreed
            else ""
        ),
        "programmes": [
            {"program": program, "meeting_pattern": value} for program, value in decision.programmes
        ],
    }
