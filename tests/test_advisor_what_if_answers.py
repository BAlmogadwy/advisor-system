"""A truthful graduation what-if comparison must be ANSWERABLE.

A live 2026-08-22 turn asked «لو حذفت مقرر CS424 هل يؤثر على تخرجي؟» and
abstained twice over: first the scenario extractor missed the conditional
past tense (fixed in student_advisor_v2), and then the checker refused the
deterministic safe composer's OWN truthful comparison - the what-if payload
is the scenario report at top level, the baseline it was compared against
lives only under ``what_if.baseline``, and neither the figure miner nor the
metric binder read it, so «1 قبل التغيير، مقابل 2 بعده» was measured against
the scenario side alone.

Real executor -> real composer -> real checker, like the build-answer suite:
this defect class is a checker written to an imagined payload shape.
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.answer_consistency import check_answer
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import _safe_graduation_answer
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 4_401_778
YEAR, TERM = 1448, 1

_CATALOGUE = frozenset({"TG101", "TG102", "TG103"})


@pytest.fixture
def chained_plan() -> None:
    """TG101 passed; TG102 registered this term; TG103 needs TG102.

    Dropping TG102 pushes it AND its dependant into future terms, so the
    baseline and the scenario genuinely disagree on every term estimate -
    the shape whose truthful comparison the checker refused.
    """
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="What-if student",
        program="TG",
        section="M",
        status="active",
    )
    courses = {}
    for code, programme_term in (("TG101", 1), ("TG102", 2), ("TG103", 3)):
        courses[code] = Course.objects.create(
            course_code=code, description=f"Course {code}", credit_hours=3
        )
        ProgrammeRequirement.objects.create(
            program="TG",
            course_code=code,
            course_name=f"Course {code}",
            type="Mandatory",
            programme_term=programme_term,
            credit_hours=3,
        )
    Prerequisite.objects.create(program="TG", course_code="TG103", prerequisite_course_code="TG102")
    StudentCourse.objects.create(student=student, course=courses["TG101"], status="passed")
    section = TermSection.objects.create(
        course_code="TG102",
        course_number="TG102",
        course_key="TG102",
        course_name="Course TG102",
        section="M1",
        available_capacity=30,
        registered_count=10,
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=section,
        source="scraper_timetable",
    )


def _what_if_payload() -> dict:
    return get_default_registry().execute(
        "graduation_progress",
        {
            "remove_current_courses": ["TG102"],
            "planning_baseline_kind": "registered_timetable",
        },
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": YEAR, "term": TERM},
    )


def _check(answer: str, payload: dict) -> list[str]:
    return check_answer(
        answer,
        tool_results=[payload],
        question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
        required_tools={"graduation_progress"},
        known_course_codes=_CATALOGUE,
    )


def test_the_scenario_really_disagrees_with_its_baseline(chained_plan: None) -> None:
    payload = _what_if_payload()
    what_if = payload["what_if"]

    assert what_if["valid"] is True
    baseline, scenario = what_if["baseline"], what_if["scenario"]
    # The composer's comparison line states exactly this pair - the
    # production shape («الحد الأدنى…: 1 قبل التغيير، مقابل 2 بعده»).
    assert baseline["lower_bound_additional_terms"] != scenario["lower_bound_additional_terms"], (
        "the fixture must produce a real before/after difference"
    )


def test_the_truthful_what_if_comparison_survives_the_checker(chained_plan: None) -> None:
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")

    assert answer, "the safe composer must produce a comparison answer"
    assert _check(answer, payload) == []


def test_the_stated_difference_between_the_sides_is_a_supported_figure() -> None:
    """«بمقدار 3 فصول» where the sides are 1 and 4: the difference is a
    server-computed comparative fact and equals NEITHER side.  Checker-side
    arithmetic, so a synthetic payload is the right instrument here."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 4,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 1},
            "scenario": {"lower_bound_additional_terms": 4},
        },
    }
    supported = "يرتفع الحد الأدنى للفصول الإضافية بمقدار 3 فصول بعد التغيير."
    unsupported = "يرتفع الحد الأدنى للفصول الإضافية بمقدار 5 فصول بعد التغيير."

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run(supported) == []
    assert run(unsupported) != []


def test_a_wrong_before_figure_in_the_comparison_still_flags(chained_plan: None) -> None:
    """Two-sided: admitting the baseline's figures must not admit inventions."""
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")
    before = payload["what_if"]["baseline"]["lower_bound_additional_terms"]
    wrong = answer.replace(f": {before} قبل التغيير", ": 9 قبل التغيير")

    assert wrong != answer, "the composed answer must state the baseline lower bound"
    assert _check(wrong, payload) != []
