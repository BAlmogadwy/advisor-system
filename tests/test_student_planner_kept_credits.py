"""A course the student keeps counts at its real hours in the student planner.

`student_planner.build_student_options` priced a held course from the programme plan
alone and gave every other one DEFAULT_CREDITS, 3. A general-studies or free elective
taken against an elective row is in no plan and is usually 2 hours, so the week it was
kept in looked one hour fuller than the registrar's figure. On the local data of
2026-11-26 that was 1,615 of the 3,926 students with a registered 1448/1 timetable,
always too many, by 1 to 4 hours. The room left under the cap, the exact-target checks
and the per-course credits all read that figure, and the chat's timetable proposal kept
a plan-only table of its own, wrong by the same amounts.

Both now read `kept_course_credits`: the lookup that gives the baseline rows their
``credits``, which is the figure the staff planner totals. The one place the two totals
still differ is a course no source knows the hours of: the staff planner counts 0, the
student planner counts DEFAULT_CREDITS so a proposal never promises room the student
may not have. The measurement found no such course in 1448/1.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models import (
    Course,
    ElectiveCourse,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services import student_planner
from core.services.rbac import ROLE_STUDENT
from core.services.student_planner import (
    DEFAULT_CREDITS,
    PlannerRequest,
    build_student_options,
    kept_course_credits,
)
from core.services.student_sections import get_student_term_registration_summary
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

PROG = "KC"
SID = 4_690_201
YEAR, TERM = 1448, 1
REGISTRAR = "scraper_timetable"

#: code -> (meeting days, start, end). Nothing clashes with anything else, so what a
#: build does is decided by the hours alone.
SECTIONS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "KC101": (("SUN", "TUE"), "08:00", "09:15"),
    "KC102": (("MON", "WED"), "08:00", "09:15"),
    "GS201": (("SUN", "TUE"), "10:00", "11:15"),
    "ZZ900": (("MON", "WED"), "10:00", "11:15"),
    "KC103": (("SUN", "TUE"), "12:00", "13:15"),
    "KC104": (("MON", "WED"), "12:00", "13:15"),
}


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, TermSection]:
    Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Kept Credits Student",
        program=PROG,
        section="M",
        status="ACTIVE",
    )
    # The plan: KC101 3, KC102 4 (the course table says 3 for it: the plan answers
    # first), and two courses the student may add.
    for code, hours in (("KC101", 3), ("KC102", 4), ("KC103", 3), ("KC104", 3)):
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=code,
            programme_term=1,
            credit_hours=hours,
            type="Mandatory",
        )
    Course.objects.create(course_code="KC102", description="KC102", credit_hours=3)
    # Outside the plan: a general-studies elective the course table knows as 2 hours.
    Course.objects.create(course_code="GS201", description="Communication", credit_hours=2)
    # ZZ900 is in no plan and in no table: no source knows its hours.
    sections: dict[str, TermSection] = {}
    for code, (days, start, end) in SECTIONS.items():
        section = TermSection.objects.create(
            course_code=code,
            course_number="",
            course_key=code,
            course_name=code,
            section="M1",
            available_capacity=30,
            registered_count=0,
        )
        TermSectionProgram.objects.create(term_section=section, program=PROG)
        for day in days:
            TermSectionMeeting.objects.create(
                term_section=section, day=day, start_time=start, end_time=end
            )
        sections[code] = section
    monkeypatch.setattr("core.services.recommender.recommend_next_courses", lambda *_a, **_k: [])
    return sections


def _register(world: dict[str, TermSection], *codes: str) -> None:
    for code in codes:
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year=str(YEAR),
            term=str(TERM),
            term_section=world[code],
            source=REGISTRAR,
        )


def _staff_total() -> int:
    """The staff planner's registered total: each course once, at its row's hours."""
    summary = get_student_term_registration_summary(SID, str(YEAR), str(TERM))
    return int(summary["value"])


@pytest.fixture
def solver_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What the adapter handed the solver, which still runs for real."""
    calls: list[dict[str, Any]] = []
    real = student_planner.run_solver

    def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr("core.services.student_planner.run_solver", spy)
    return calls


def _build(
    *codes: str,
    cap: int = 18,
    target: int | None = None,
    override: tuple[tuple[str, int], ...] = (),
) -> dict[str, Any]:
    return build_student_options(
        PlannerRequest(
            student_id=SID,
            year=YEAR,
            term=TERM,
            must_include=tuple(codes),
            keep_current_sections=True,
            max_credits=cap,
            target_credits=target,
            include_recommendations=False,
            course_credits_override=override,
        )
    )


def _by_course(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {str(row["course_code"]): int(row["credits"]) for row in rows}


# ── the shared lookup ────────────────────────────────────────────────────────────


def test_kept_course_credits_prices_each_kind_of_held_course(
    django_assert_num_queries: Any,
) -> None:
    ElectiveCourse.objects.create(
        programme=PROG, course_code="KC463", course_name="Data Mining", credit_hours=3
    )
    # The programme's own catalogue answers before the course table does.
    Course.objects.create(course_code="KC463", description="Data Mining", credit_hours=4)

    assert kept_course_credits(PROG, ["KC101", "kc 102", "GS201", "KC463", "ZZ900", ""]) == {
        "KC101": 3,  # in the plan
        "KC102": 4,  # in the plan: the plan, not the course table's 3
        "GS201": 2,  # outside the plan: the course table
        "KC463": 3,  # outside the plan: the programme's elective catalogue
        "ZZ900": DEFAULT_CREDITS,  # no source knows it
    }
    with django_assert_num_queries(0):
        assert kept_course_credits(PROG, []) == {}


# ── the adapter ──────────────────────────────────────────────────────────────────


def test_a_kept_course_outside_the_plan_counts_its_real_hours(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    _register(world, "KC101", "GS201")  # 3 + 2: the registrar's total is 5
    assert _staff_total() == 5

    result = _build("KC103", cap=8)

    # 8 - 5 hours kept leaves room for KC103. At 3 hours for the 2-hour course the
    # adapter saw 2 hours of room and could not place it.
    assert solver_calls[0]["max_credits"] == 3
    option = result["alternatives"][0]
    assert option["credit_hours"] == 8
    expected = {"KC101": 3, "GS201": 2, "KC103": 3}
    assert _by_course(option["courses"]) == expected
    assert _by_course(option["meetings"]) == expected
    # The kept part of the total is the staff planner's number.
    kept = {code: hours for code, hours in expected.items() if code != "KC103"}
    assert sum(kept.values()) == _staff_total()
    assert [c["source"] for c in option["courses"] if c["course_code"] == "KC103"] == ["proposed"]


def test_with_nothing_to_add_the_kept_total_is_the_staff_total(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "KC102", "GS201")

    option = _build()["alternatives"][0]

    assert option["credit_hours"] == _staff_total() == 9
    assert _by_course(option["courses"]) == {"KC101": 3, "KC102": 4, "GS201": 2}


def test_an_exact_target_counts_the_kept_courses_real_hours(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    _register(world, "KC101", "GS201")  # 5 kept

    result = _build("KC103", target=8)

    # 8 in all, 5 of them kept: the solver is asked for exactly one 3-hour course.
    # With the 2-hour course counted as 3 it was asked for 2 and could never answer.
    assert solver_calls[0]["target_credits"] == 3
    assert result["target_credits_satisfied"] is True
    assert result["alternatives"]
    assert {row["credit_hours"] for row in result["alternatives"]} == {8}


def test_a_target_equal_to_the_real_kept_total_is_met_by_the_kept_courses_alone(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "GS201")

    result = _build(target=5)

    # At 6 hours the retained baseline looked like it already exceeded the target.
    assert result["constraint_failures"] == []
    assert result["target_credits_satisfied"] is True
    assert [row["credit_hours"] for row in result["alternatives"]] == [5]


def test_a_kept_plan_course_counts_the_plans_hours_not_the_course_tables(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    _register(world, "KC102")  # the plan says 4, the course table 3

    option = _build("KC103", cap=10)["alternatives"][0]

    assert solver_calls[0]["max_credits"] == 6
    assert _by_course(option["courses"])["KC102"] == 4
    assert option["credit_hours"] == 7


def test_a_kept_course_no_source_knows_counts_the_fallback(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    """The measurement found none in 1448/1, so the fallback is unchanged: a guess
    that errs towards a fuller week, never room the student may not have."""
    _register(world, "ZZ900")
    # The one place the two totals differ, on purpose: nothing says what it weighs.
    assert _staff_total() == 0

    option = _build("KC103", cap=9)["alternatives"][0]

    assert solver_calls[0]["max_credits"] == 9 - DEFAULT_CREDITS
    assert _by_course(option["courses"])["ZZ900"] == DEFAULT_CREDITS


def test_a_plan_row_with_no_hours_counts_the_fallback_as_it_always_did(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    ProgrammeRequirement.objects.filter(program=PROG, course_code="KC101").update(credit_hours=None)
    _register(world, "KC101")

    option = _build("KC103", cap=9)["alternatives"][0]

    assert solver_calls[0]["max_credits"] == 9 - DEFAULT_CREDITS
    assert _by_course(option["courses"])["KC101"] == DEFAULT_CREDITS


def test_a_figure_the_caller_supplies_beats_the_catalogue(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    """The course-replacement check hands in the graduation forecast's own figure."""
    _register(world, "GS201")

    option = _build("KC103", cap=12, override=(("GS201", 4),))["alternatives"][0]

    assert solver_calls[0]["max_credits"] == 8
    assert _by_course(option["courses"])["GS201"] == 4


def test_a_rebuild_holds_nothing_so_it_prices_nothing(
    world: dict[str, TermSection], solver_calls: list[dict[str, Any]]
) -> None:
    _register(world, "KC101", "GS201")

    build_student_options(
        PlannerRequest(
            student_id=SID,
            year=YEAR,
            term=TERM,
            must_include=("KC103",),
            keep_current_sections=False,
            max_credits=18,
            include_recommendations=False,
        )
    )

    assert solver_calls[0]["max_credits"] == 18


# ── the chat's timetable proposal ────────────────────────────────────────────────


def _proposal(arguments: dict[str, Any]) -> dict[str, Any]:
    return get_default_registry().execute(
        "build_timetable_proposal",
        {"mode": "around_current", **arguments},
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": YEAR, "term": TERM},
    )


def test_the_chat_proposal_counts_a_kept_course_outside_the_plan_at_its_real_hours(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "GS201")

    result = _proposal({"course_codes": ["KC103"], "max_credits": 8})

    assert result["ok"] is True
    assert result["baseline_credit_hours"] == result["current_credit_hours"] == _staff_total()
    assert _by_course(result["baseline_sections"]) == {"KC101": 3, "GS201": 2}
    # With 5 hours kept and a cap of 8 there is room for KC103; at 6 there was none.
    option = result["alternatives"][0]
    assert option["proposed_credit_hours"] == 3
    assert option["total_credit_hours"] == 8
    assert [row["course_code"] for row in option["courses"]] == ["KC103"]


def test_the_chat_proposal_meets_an_exact_target_with_the_real_kept_hours(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "GS201")

    result = _proposal({"course_codes": ["KC103"], "max_credits": 18, "target_credits": 8})

    assert result["target_credit_status"] == "SATISFIED"
    assert result["target_credits_satisfied"] is True
    assert {row["total_credit_hours"] for row in result["alternatives"]} == {8}


def test_the_chat_proposal_keeping_the_week_at_the_real_total_needs_no_addition(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "GS201")

    result = _proposal({"course_codes": [], "max_credits": 18, "target_credits": 5})

    # At 6 hours this read RETAINED_BASELINE_EXCEEDS_TARGET.
    assert result["baseline_credit_hours"] == 5
    assert result["target_credit_status"] == "SATISFIED"
    assert result["status"] == "NO_ADDITIONAL_COURSES"


def test_the_chat_proposal_counts_a_kept_course_no_source_knows_at_the_fallback(
    world: dict[str, TermSection],
) -> None:
    _register(world, "KC101", "ZZ900")

    result = _proposal({"course_codes": [], "max_credits": 18})

    assert result["baseline_credit_hours"] == 3 + DEFAULT_CREDITS
    assert _by_course(result["baseline_sections"]) == {"KC101": 3, "ZZ900": DEFAULT_CREDITS}
