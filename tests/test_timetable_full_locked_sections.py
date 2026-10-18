"""Full rebuild must retain complete physical contact patterns around a time lock."""

import pytest

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.timetable_autoplace import WEEKDAYS, auto_place_board
from core.services.timetable_board_persistence import reset_scenario
from core.services.timetable_optimizer_v2 import ALL_STRATEGIES
from core.services.timetable_v2_runner import run_v2_optimisation_guarded

pytestmark = pytest.mark.django_db


@pytest.fixture
def partial_sections(settings):
    settings.TIMETABLE_ENFORCE_LOCKS = True
    settings.TIMETABLE_CANDIDATE_GEN_BUDGET_SECONDS = 0.01
    settings.TIMETABLE_LOCAL_SEARCH_TIME_LIMIT_SECONDS = 0
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0.01
    settings.TIMETABLE_SECONDARY_RECOVERY_SECONDS = 0.01
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="Partial locks", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="T1", program="AI", nominal_term=1
    )
    for kind in ("lecture", "lab"):
        Room.objects.create(
            room_code=kind, capacity=50, room_type=kind, department="AI", section="M"
        )
    Student.objects.create(
        student_id=990001, registration_no="990001", name="Student", program="AI"
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=990001,
        primary_term=1,
        recommended_courses=["MATH105", "PHYS103", "CS101"],
    )
    patterns = {
        "MATH105": [("SUN", "10:30", "11:45"), ("TUE", "13:00", "14:15")],
        "PHYS103": [
            ("SUN", "09:00", "10:15"),
            ("THU", "09:00", "10:40"),
            ("TUE", "09:00", "10:15"),
        ],
        "CS101": [("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")],
    }
    for code, pattern in patterns.items():
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario, student_id=990001, course_key=code, course_code=code, primary_term=1
        )
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_name=code,
            department="AI",
            credit_hours=4 if code == "PHYS103" else 3,
            planned_sections=1,
            max_per_section=30,
            total_demand=1,
            programme_term=1,
        )
        section = TermSection.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_number=code,
            course_name=code,
            section="S1",
            source_tag="tw_auto",
            available_capacity=30,
        )
        for i, (day, start, end) in enumerate(pattern):
            room = "lab" if end == "10:40" else "lecture"
            SectionPlacement.objects.create(
                board=board,
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
                room=room,
                is_locked=code != "CS101" and i == 0,
            )
            TermSectionMeeting.objects.create(
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
                room=room,
                instructor="",
                building="Original",
                floor_wing="F1",
            )
    return scenario, board


def _times(section_ids):
    return list(
        SectionPlacement.objects.filter(term_section_id__in=section_ids)
        .order_by("pk")
        .values("id", "board_id", "term_section_id", "day", "start_time", "end_time", "is_locked")
    )


def _minutes(rows):
    def minute(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    return sum(minute(row["end_time"]) - minute(row["start_time"]) for row in rows)


def test_real_full_portfolio_keeps_150_and_250_minutes_and_rebuilds_other_sections(
    partial_sections,
):
    scenario, board = partial_sections
    protected_ids = list(
        TermSection.objects.filter(scenario=scenario)
        .exclude(course_code="CS101")
        .values_list("pk", flat=True)
    )
    before = _times(protected_ids)
    other_before = set(
        SectionPlacement.objects.filter(board=board, term_section__course_code="CS101").values_list(
            "pk", flat=True
        )
    )
    report = run_v2_optimisation_guarded(
        scenario.pk,
        mode="full",
        strategies=list(ALL_STRATEGIES),
        max_iterations=0,
        run_chain=False,
        run_cpsat=False,
    )
    assert not report.get("error")
    assert not report.get("safety_blocked"), report.get("safety_regression")
    assert _times(protected_ids) == before
    assert {
        _minutes([r for r in _times(protected_ids) if r["term_section_id"] == sid])
        for sid in protected_ids
    } == {150, 250}
    other_after = set(
        SectionPlacement.objects.filter(board=board, term_section__course_code="CS101").values_list(
            "pk", flat=True
        )
    )
    assert len(other_after) == 2 and other_after.isdisjoint(other_before)


@pytest.mark.parametrize("method", ["greedy", "cpsat"])
def test_unlocked_companion_occupancy_blocks_a_regenerated_same_course_sibling(
    partial_sections, method
):
    from core.services.timetable_solver import solve_board

    scenario, board = partial_sections
    SectionPlacement.objects.filter(board=board).exclude(
        term_section__course_code="MATH105"
    ).delete()
    ScenarioSectionBudget.objects.filter(scenario=scenario).exclude(course_code="MATH105").delete()
    ScenarioSectionBudget.objects.filter(scenario=scenario).update(planned_sections=2)
    scenario.slot_config = [{"start": "10:30", "end": "11:45"}, {"start": "13:00", "end": "14:15"}]
    allowed = {("SUN", "10:30"), ("MON", "13:00"), ("TUE", "13:00")}
    scenario.blocked_slots = [
        {"day": d, "start": slot["start"]}
        for d in WEEKDAYS
        for slot in scenario.slot_config
        if (d, slot["start"]) not in allowed
    ]
    scenario.save(update_fields=["slot_config", "blocked_slots"])
    reset_scenario(scenario.pk, keep_locked=True)
    if method == "greedy":
        auto_place_board(board.pk, strategy="compact")
        assert not SectionPlacement.objects.filter(board=board, term_section__section="S2").exists()
    else:
        report = solve_board(board.pk, time_limit_seconds=1)
        assert report["status"] == "infeasible"
        assert not report["placements"]


@pytest.mark.parametrize("writer", ["solver", "sa", "balance"])
def test_lock_on_another_owned_board_preserves_all_physical_copies(
    partial_sections, monkeypatch, writer
):
    from core.services import timetable_load_balanced as balance
    from core.services import timetable_local_search as sa
    from core.services.timetable_solver import persist_solver_result

    scenario, board = partial_sections
    ts = TermSection.objects.get(scenario=scenario, course_code="MATH105")
    shared = DeliveryBoard.objects.create(
        scenario=scenario, label="Shared", program="AI", nominal_term=2
    )
    for p in SectionPlacement.objects.filter(board=board, term_section=ts):
        SectionPlacement.objects.create(
            board=shared,
            term_section=ts,
            day=p.day,
            start_time=p.start_time,
            end_time=p.end_time,
            room=p.room,
        )
    before = _times([ts.pk])
    reset_scenario(scenario.pk, keep_locked=True)
    if writer == "solver":
        persist_solver_result(shared.pk, {"status": "optimal", "placements": []})
    elif writer == "sa":
        candidate = sa.optimize_board(shared.pk, max_seconds=0)
        assert candidate["status"] == "optimized"
        assert all(section["is_locked"] for section in candidate["sections"])
        monkeypatch.setattr(sa, "optimize_board", lambda *a, **k: candidate)
        report = sa.optimize_and_persist_board(shared.pk, max_seconds=0)
        assert not report.get("persist_rolled_back")
    else:
        candidate = balance.rebalance_board(shared.pk, max_seconds=0)
        assert candidate["status"] == "optimized"
        assert all(section["is_locked"] for section in candidate["sections"])
        monkeypatch.setattr(balance, "rebalance_board", lambda *a, **k: candidate)
        report = balance.rebalance_and_persist_board(shared.pk, max_seconds=0)
        assert not report.get("persist_rolled_back")
    assert _times([ts.pk]) == before


def test_fixed_companion_occupancy_remains_flag_gated(partial_sections, settings):
    from core.services.timetable_solver import _board_fixed_locked_meetings

    scenario, board = partial_sections
    settings.TIMETABLE_ENFORCE_LOCKS = False
    assert _board_fixed_locked_meetings(board) == []


@pytest.mark.parametrize("repair_pass", ["cap", "clash"])
@pytest.mark.parametrize("enforce", [False, True])
def test_mandatory_repair_preserves_locked_section_companions_when_enabled(
    partial_sections, settings, repair_pass, enforce
):
    from core.models import CourseInstructor, Instructor
    from core.services.timetable_instructor_cap_repair import (
        repair_instructor_clashes,
        repair_instructor_daily_overloads,
    )

    scenario, board = partial_sections
    settings.TIMETABLE_ENFORCE_LOCKS = enforce
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 1 if repair_pass == "cap" else 3
    teacher = Instructor.objects.create(full_name="Dr Fixed", normalised_name="dr fixed")
    for code in ("MATH105", "PHYS103"):
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=teacher, role="primary"
        )
    if repair_pass == "clash":
        section = TermSection.objects.get(scenario=scenario, course_code="PHYS103")
        SectionPlacement.objects.filter(term_section=section, day="TUE").update(
            start_time="13:00", end_time="14:15"
        )
        TermSectionMeeting.objects.filter(term_section=section, day="TUE").update(
            start_time="13:00", end_time="14:15"
        )
    protected_ids = list(
        TermSection.objects.filter(scenario=scenario)
        .exclude(course_code="CS101")
        .values_list("pk", flat=True)
    )
    before = _times(protected_ids)
    report = (
        repair_instructor_daily_overloads if repair_pass == "cap" else repair_instructor_clashes
    )(scenario.pk)
    if enforce:
        assert _times(protected_ids) == before
        assert not report["repaired"]
        assert report["locked_blocked"]
        assert report["remaining_violations" if repair_pass == "cap" else "remaining_clashes"] > 0
    else:
        assert report["repaired"]
        assert _times(protected_ids) != before
