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


def test_the_two_figure_lower_bound_sentence_binds_to_its_own_words() -> None:
    """The review's reproduction on a completed single report: «الحد
    الأدنى: 2 فصل إضافي، أي 3 فصول بما فيها الفصل الحالي» quoted both
    lower_bound_* values exactly and was refused, because the two-figure
    branch chose its fields from the completeness heuristic alone."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "estimated_additional_terms": 3,
        "lower_bound_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 4,
        "lower_bound_terms_including_planning_baseline": 3,
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="كم فصلاً يتبقى لتخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    truthful_lower = "تحتاج على الأقل 2 فصل إضافي، أي 3 فصول بما فيها الفصل الحالي."
    truthful_estimate = "تحتاج 3 فصول إضافية، أي 4 فصول بما فيها الفصل الحالي."
    wrong_lower = "تحتاج على الأقل 3 فصول إضافية، أي 4 فصول بما فيها الفصل الحالي."
    assert run(truthful_lower) == []
    assert run(truthful_estimate) == []
    assert run(wrong_lower) != []


def test_english_lower_bound_words_bind_the_lower_bound_field() -> None:
    """«على الأقل» / "at least" / "minimum" are load-bearing entries of the
    lower-bound word list, not decoration around «الحد الأدنى»."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "estimated_additional_terms": 3,
        "lower_bound_additional_terms": 2,
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="How many terms remain?",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("You need at least 2 additional terms.") == []
    assert run("You need at least 5 additional terms.") != []


def test_a_baseline_side_value_is_supported_on_its_own() -> None:
    """The sides are load-bearing, not just their difference: before=2 with
    after=5 puts the difference at 3 and the top level at 5, so only the
    side read makes the truthful «كان الحد الأدنى 2 قبل التغيير» pass."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 5,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 2},
            "scenario": {"lower_bound_additional_terms": 5},
        },
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("كان الحد الأدنى للفصول الإضافية 2 قبل التغيير.") == []
    assert run("كان الحد الأدنى للفصول الإضافية 4 قبل التغيير.") != []


def test_an_improvement_difference_is_supported_in_absolute_value() -> None:
    """A scenario that SAVES a term states the difference too — the sides
    are 3 then 2, the difference the answer speaks is 1, and only the
    absolute value supports it; a signed difference would refuse every
    improvement sentence."""
    payload = {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": True,
        "lower_bound_additional_terms": 2,
        "what_if": {
            "valid": True,
            "baseline": {"lower_bound_additional_terms": 3},
            "scenario": {"lower_bound_additional_terms": 2},
        },
    }

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="لو حذفت مقرر TG102 هل يؤثر على تخرجي؟",
            required_tools=set(),
            known_course_codes=_CATALOGUE,
        )

    assert run("ينخفض الحد الأدنى للفصول الإضافية بمقدار 1 فصل بعد التغيير.") == []
    assert run("ينخفض الحد الأدنى للفصول الإضافية بمقدار 4 فصول بعد التغيير.") != []


def test_a_wrong_before_figure_in_the_comparison_still_flags(chained_plan: None) -> None:
    """Two-sided: admitting the baseline's figures must not admit inventions."""
    payload = _what_if_payload()
    answer = _safe_graduation_answer("Arabic", [payload], "")
    before = payload["what_if"]["baseline"]["lower_bound_additional_terms"]
    wrong = answer.replace(f": {before} قبل التغيير", ": 9 قبل التغيير")

    assert wrong != answer, "the composed answer must state the baseline lower bound"
    assert _check(wrong, payload) != []
