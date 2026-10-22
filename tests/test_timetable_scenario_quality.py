from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    Course,
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    PlannerJob,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    StudentCourse,
    TermSection,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    TimetableEvaluationResult,
)
from core.services.timetable_scenario_quality import (
    _assignment_clash_counts,
    _seat_tiers,
    build_scenario_quality,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def scenario():
    return TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        gender="M",
        programs=["AI"],
    )


@pytest.fixture
def http(monkeypatch):
    # Other generation tests mock the runner before URLconf is first imported.
    # Keep this endpoint fixture independent of that import-time binding.
    from core.services.planner_job_runner import submit_planner_job

    monkeypatch.setattr("core.planner_job_views.submit_planner_job", submit_planner_job)
    ensure_role_groups()
    user = User.objects.create_user(username="quality-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def place(
    scenario,
    code,
    *,
    board=None,
    start="09:00",
    end="10:15",
    room="R1",
    credit=3,
    demand=20,
    day="SUN",
    section="S1",
):
    board = board or DeliveryBoard.objects.create(
        scenario=scenario,
        label=code,
        nominal_term=1,
        program="AI",
    )
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code=code,
        course_key=code,
        section=section,
    )
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=code,
        planned_sections=1,
        credit_hours=credit,
        max_per_section=40,
        total_demand=demand,
    )
    return SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day=day,
        start_time=start,
        end_time=end,
        room=room,
    )


def test_audit_missing_buffered_lab_and_shared_rows_without_room_solve(scenario, monkeypatch):
    monkeypatch.setattr(
        "core.services.timetable_exact_rooming._solve_relaxed",
        Mock(
            side_effect=AssertionError("quality GET must not solve room assignments"),
        ),
    )
    Room.objects.create(room_code="LAB", room_type="lab", capacity=25, section="M", department="AI")
    lab = place(scenario, "CS323", room="LAB", end="10:40", credit=4, demand=24)
    place(scenario, "FE2", room="UNASSIGNED", start="11:00", end="12:15")
    other_board = DeliveryBoard.objects.create(scenario=scenario, label="shared", program="AI")
    SectionPlacement.objects.create(
        board=other_board,
        term_section=lab.term_section,
        day=lab.day,
        start_time=lab.start_time,
        end_time=lab.end_time,
        room=lab.room,
    )
    with CaptureQueriesContext(connection) as queries:
        quality = build_scenario_quality(scenario.id)
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
    rooms = quality["rooms"]
    assert rooms["assessed"]
    assert rooms["shortages"] == 2
    assert rooms["physical_meetings"] == 2
    assert rooms["missing"] == rooms["undersized"] == 1
    assert rooms["incompatible"] == rooms["clashes"] == 0
    lab_detail = next(d for d in rooms["details"] if d["course_code"] == "CS323")
    assert lab_detail["required_capacity"] == 26
    assert len(lab_detail["placement_ids"]) == 2
    assert quality["soft_seats"]["unresolved"] is None
    assert not quality["assigned_student_clashes"]["assessed"]


def test_room_kind_gender_department_and_partial_clashes_are_reported(scenario):
    Room.objects.create(room_code="R1", capacity=50, section="M", department="AI")
    Room.objects.create(room_code="BAD", room_type="lab", capacity=50, section="F", department="DS")
    place(scenario, "AI101")
    place(scenario, "AI102", start="10:00", end="11:15")
    place(scenario, "AI103", room="BAD", start="11:15", end="12:30")
    rooms = build_scenario_quality(scenario.id)["rooms"]
    assert rooms["shortages"] == 3
    assert rooms["clashes"] == 2
    assert rooms["incompatible"] == 1


def test_instructor_union_days_idle_excludes_online_and_nested_intervals(scenario, settings):
    settings.TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED = False
    teacher = Instructor.objects.create(full_name="Teacher", normalised_name="teacher")
    for code, start, end, day, online in (
        ("AI101", "09:00", "11:00", "SUN", False),
        ("AI102", "10:00", "10:30", "SUN", False),
        ("AI103", "12:00", "13:15", "SUN", False),
        ("AI104", "11:00", "12:00", "SUN", True),
        ("AI105", "09:00", "10:15", "MON", True),
    ):
        place(scenario, code, start=start, end=end, day=day)
        CourseInstructor.objects.create(
            program="AI",
            section="M",
            course_code=code,
            instructor=teacher,
        )
        if online:
            ProgrammeRequirement.objects.create(
                program="AI",
                course_code=code,
                is_online=True,
            )
    quality = build_scenario_quality(scenario.id)
    assert quality["instructor"] == {
        "working_days": 1,
        "idle_minutes": 60,
        "instructors": 1,
        "assessed": True,
    }
    assert quality["rooms"]["physical_meetings"] == 3


def test_canonical_soft_seats_distinguish_demand_overlap_from_assigned_clashes(scenario, settings):
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    place(scenario, "AI101")
    place(scenario, "FE2")
    Student.objects.create(student_id=7001, registration_no="7001", name="Student", program="AI")
    for code in ("AI101", "FE2"):
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario,
            student_id=7001,
            course_key=code,
            course_code=code,
        )
    quality = build_scenario_quality(scenario.id)
    assert quality["soft_seats"]["assessed"]
    assert quality["soft_seats"]["unresolved"] == 1
    assert quality["assigned_student_clashes"]["assessed"]
    assert quality["assigned_student_clashes"]["count"] == 0
    assert quality["assigned_student_clashes"]["students"] == 0
    # The student has no failed course; the unseated request is the free elective.
    assert quality["seats"] == {
        "assessed": True,
        "layout": "tiered",
        "high_risk": 0,
        "required": 0,
        "flexible": 1,
        "total": 1,
    }


def test_failed_assessment_is_unknown_with_other_metrics_retained(scenario, monkeypatch):
    place(scenario, "AI101")
    monkeypatch.setattr(
        "core.services.timetable_scenario_quality.build_student_profiles_for_scenario",
        Mock(side_effect=ValueError("bad demand")),
    )
    quality = build_scenario_quality(scenario.id)
    assert quality["rooms"]["assessed"] and quality["instructor"]["assessed"]
    assert quality["soft_seats"]["unresolved"] is None
    assert quality["soft_seats"]["reason"] == "assessment_failed"


# ── seat tiers and the students behind the clash count ──────────────────

UNKNOWN_SEATS = {
    "assessed": False,
    "layout": None,
    "high_risk": None,
    "required": None,
    "flexible": None,
    "total": None,
}


def _evaluation(score, states=None):
    return TimetableEvaluationResult(
        candidate_id="quality-test",
        lexicographic_score=score,
        assignment_states=states or {},
        unresolved_student_ids=[],
        hotspot_courses=[],
        capacity_pressure_courses=[],
        reserve_heavy_sections=[],
    )


def test_seat_tiers_read_each_position_of_a_tiered_score():
    # Distinct values: a swapped position cannot cancel out.
    # (high-risk, clash, T1, T2 over tolerance, cost, soft, reserve, spread, idle)
    seats = _seat_tiers(_evaluation((2, 9, 3, 4, 500, 5, 8, 7, 6)))

    assert seats == {
        "assessed": True,
        "layout": "tiered",
        "high_risk": 2,
        "required": 7,  # T1 (3) + T2 over tolerance (4)
        "flexible": 5,  # T3 + T2 within tolerance, the soft count
        "total": 12,
    }


def test_seat_tiers_of_a_legacy_score_are_one_total():
    # (tier A students, blocked students, unassigned courses, clashes, gap, reserve)
    seats = _seat_tiers(_evaluation((1, 2, 6, 0, 100, 0)))

    assert seats == {"assessed": True, "layout": "legacy", "total": 6}


def _wants(student_id, codes, scenario):
    Student.objects.create(
        student_id=student_id, registration_no=str(student_id), name="S", program="AI"
    )
    for code in codes:
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario, student_id=student_id, course_key=code, course_code=code
        )


def test_tiered_seats_split_required_flexible_and_count_high_risk_students(scenario, settings):
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = True
    # Three courses at one hour: each student can hold one. AI101/AI102 are
    # specialised (T1, required), FE2 is a free elective (T3, flexible).
    for code in ("AI101", "AI102", "FE2"):
        place(scenario, code)
    _wants(7001, ("AI101", "AI102", "FE2"), scenario)
    _wants(7002, ("AI101", "AI102", "FE2"), scenario)
    # 7001 failed a course (a retake student is high-risk); 7002 did not.
    failed = Course.objects.create(course_code="AI090")
    StudentCourse.objects.create(student_id=7001, course=failed, status="failed")

    quality = build_scenario_quality(scenario.id)

    # Each of the two students keeps one seat, so loses one T1 and the T3.
    assert quality["seats"] == {
        "assessed": True,
        "layout": "tiered",
        "high_risk": 1,  # students, not requests: only 7001
        "required": 2,
        "flexible": 2,
        "total": 4,
    }
    assert quality["soft_seats"]["unresolved"] == quality["seats"]["flexible"]


def test_legacy_seats_report_only_the_total(scenario, settings):
    settings.TIMETABLE_TIERED_OBJECTIVE_ENABLED = False
    place(scenario, "AI101")
    place(scenario, "FE2")
    _wants(7001, ("AI101", "FE2"), scenario)

    quality = build_scenario_quality(scenario.id)

    assert quality["seats"] == {"assessed": True, "layout": "legacy", "total": 1}


def test_seats_and_clash_students_are_unknown_not_zero_without_demand(scenario):
    place(scenario, "AI101")

    quality = build_scenario_quality(scenario.id)

    assert quality["seats"] == {**UNKNOWN_SEATS, "reason": "no_student_demand"}
    clashes = quality["assigned_student_clashes"]
    assert (clashes["count"], clashes["students"], clashes["assessed"]) == (None, None, False)
    assert clashes["reason"] == "no_student_demand"


def test_a_failed_assessment_leaves_seats_and_clash_students_unknown(scenario, monkeypatch):
    place(scenario, "AI101")
    monkeypatch.setattr(
        "core.services.timetable_scenario_quality.build_student_profiles_for_scenario",
        Mock(side_effect=ValueError("bad demand")),
    )

    quality = build_scenario_quality(scenario.id)

    assert quality["seats"] == {**UNKNOWN_SEATS, "reason": "assessment_failed"}
    assert quality["assigned_student_clashes"]["students"] is None
    assert quality["assigned_student_clashes"]["count"] is None


def _section_state(section_id, day, start, end):
    return SectionState(
        section_id=section_id,
        course_code=section_id,
        meetings=[SectionMeeting(day=day, start_min=start, end_min=end)],
        max_capacity=40,
        reserve_capacity=0,
    )


def test_clash_counts_are_pairs_and_unique_students():
    # A, B and C overlap one another on day 0; D is on another day.
    sections = [
        _section_state("A", 0, 540, 615),
        _section_state("B", 0, 560, 640),
        _section_state("C", 0, 600, 660),
        _section_state("D", 1, 540, 615),
    ]
    states = {
        "s1": StudentAssignmentState(student_id="s1", section_ids={"A", "B", "C"}),  # 3 pairs
        "s2": StudentAssignmentState(student_id="s2", section_ids={"A", "D"}),  # none
        "s3": StudentAssignmentState(student_id="s3", section_ids={"A", "B"}),  # 1 pair
        "s4": StudentAssignmentState(student_id="s4", section_ids=set()),
    }

    pairs, students = _assignment_clash_counts(_evaluation((0,) * 6, states), sections)

    assert (pairs, students) == (4, 2)


def test_quality_reports_count_and_students_under_their_own_keys(scenario, monkeypatch):
    place(scenario, "AI101")
    _wants(7001, ("AI101",), scenario)
    monkeypatch.setattr(
        "core.services.timetable_scenario_quality._assignment_clash_counts",
        lambda evaluation, sections: (7, 3),
    )

    clashes = build_scenario_quality(scenario.id)["assigned_student_clashes"]

    assert (clashes["count"], clashes["students"], clashes["assessed"]) == (7, 3, True)


def test_quality_endpoint_role_method_and_missing_scenario(http, scenario):
    response = http.get(f"/ops/tw/scenarios/{scenario.id}/quality/")
    assert response.status_code == 200
    assert response.json()["ok"]
    assert response.json()["scenario_id"] == scenario.id
    assert http.post(f"/ops/tw/scenarios/{scenario.id}/quality/").status_code == 405
    assert http.get("/ops/tw/scenarios/999999/quality/").status_code == 404
    low_role = User.objects.create_user(username="quality-unprivileged")
    http.force_login(low_role)
    assert http.get(f"/ops/tw/scenarios/{scenario.id}/quality/").status_code == 403


def test_scenario_reload_discovers_latest_active_job_read_only(http, scenario, settings):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_CURRENT,
        status=PlannerJob.STATUS_QUEUED,
    )
    active = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_V2_CURRENT,
        status=PlannerJob.STATUS_RUNNING,
        last_stage_seen="cpsat",
    )
    PlannerJob.objects.filter(pk=active.pk).update(
        submitted_at=active.submitted_at + timedelta(seconds=1),
    )
    PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_FULL_REBUILD,
        status=PlannerJob.STATUS_SUCCEEDED,
    )
    response = http.get(f"/ops/tw/scenarios/{scenario.id}/")
    assert response.json()["active_job"]["job_id"] == str(active.id)
    assert response.json()["active_job"]["scenario_id"] == scenario.id
    assert response.json()["active_job"]["last_stage_seen"] == "cpsat"
    assert "result_json" not in response.json()["active_job"]
    active.refresh_from_db()
    assert active.status == PlannerJob.STATUS_RUNNING
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = False
    assert http.get(f"/ops/tw/scenarios/{scenario.id}/").json()["active_job"] is None


@pytest.mark.parametrize("status", [PlannerJob.STATUS_QUEUED, PlannerJob.STATUS_RUNNING])
def test_active_job_rejects_manual_mutations_and_second_submission_without_writes(
    http,
    scenario,
    settings,
    status,
    monkeypatch,
):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    placement = place(scenario, "AI101")
    other = TimetableScenario.objects.create(academic_year="1448", term="1", name="other")
    job = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_CURRENT,
        status=status,
    )
    dispatch = Mock()
    monkeypatch.setattr("core.services.planner_job_runner.dispatch_planner_job", dispatch)
    prefix = f"/ops/tw/scenarios/{scenario.id}"
    writes = [
        (f"{prefix}/slots/update/", {"slot_config": []}),
        (f"{prefix}/publish/", {}),
        (f"{prefix}/blocked-slots/", {"day": "SUN", "start": "09:00"}),
        (f"{prefix}/clean-room-assignments/apply/", {}),
        (f"{prefix}/safe-time-moves/apply/", {}),
        ("/ops/tw/boards/create/", {"scenario_id": scenario.id, "label": "new"}),
        (
            "/ops/tw/placements/create/",
            {
                "board_id": placement.board_id,
                "term_section_id": placement.term_section_id,
                "day": "MON",
                "start_time": "11:00",
                "end_time": "12:15",
                "scenario_id": other.id,
            },
        ),
        (
            "/ops/tw/placements/create-planned/",
            {
                "board_id": placement.board_id,
                "course_code": "AI102",
                "section_label": "S1",
                "day": "MON",
                "start_time": "11:00",
                "end_time": "12:15",
                "scenario_id": other.id,
            },
        ),
        (f"/ops/tw/placements/{placement.id}/move/", {"day": "MON"}),
        (f"/ops/tw/placements/{placement.id}/remove/", {}),
        (f"/ops/tw/placements/{placement.id}/lock/", {}),
        (f"{prefix}/optimise-v2/", {"mode": "current", "async_run": True}),
        ("/planner-jobs/", {"scenario_id": scenario.id, "mode": "optimise_current"}),
    ]
    for url, payload in writes:
        response = http.post(url, data=json.dumps(payload), content_type="application/json")
        assert response.status_code == 409, (url, response.content)
        body = response.json()["error"]
        assert body["code"] == "SCENARIO_BUSY"
        assert body["details"]["active_job"]["job_id"] == str(job.id)
    placement.refresh_from_db()
    scenario.refresh_from_db()
    assert (placement.day, placement.start_time, placement.is_locked) == ("SUN", "09:00", False)
    assert scenario.status == "draft" and not scenario.blocked_slots
    assert SectionPlacement.objects.count() == 1
    assert TermSection.objects.count() == 1
    assert DeliveryBoard.objects.count() == 1
    assert PlannerJob.objects.count() == 1
    dispatch.assert_not_called()


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_terminal_job_restores_manual_edit_and_submission(
    http, scenario, settings, status, monkeypatch
):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    placement = place(scenario, "AI101")
    PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode=PlannerJob.MODE_OPTIMISE_CURRENT,
        status=status,
    )
    response = http.post(
        f"/ops/tw/placements/{placement.id}/lock/", data="{}", content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["is_locked"]
    dispatch = Mock()
    monkeypatch.setattr("core.planner_job_views.dispatch_planner_job", dispatch)
    response = http.post(
        "/planner-jobs/",
        data=json.dumps(
            {
                "scenario_id": scenario.id,
                "mode": "optimise_current",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert PlannerJob.objects.filter(scenario=scenario, status="queued").count() == 1
    dispatch.assert_called_once()


def test_service_submission_serializes_duplicate_check(scenario):
    from core.services.planner_job_runner import submit_planner_job
    from core.services.timetable_mutation_guard import ScenarioPlannerBusy

    first = submit_planner_job(scenario_id=scenario.id, mode="optimise_current")
    with pytest.raises(ScenarioPlannerBusy) as busy:
        submit_planner_job(scenario_id=scenario.id, mode="optimise_current")
    assert busy.value.job.id == first
    assert PlannerJob.objects.count() == 1


def test_published_scenario_remains_read_only_with_active_job(http, scenario, settings):
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = True
    placement = place(scenario, "AI101")
    scenario.status = "published"
    scenario.save(update_fields=["status"])
    PlannerJob.objects.create(id=uuid.uuid4(), scenario=scenario, mode="optimise_current")
    response = http.post(
        f"/ops/tw/placements/{placement.id}/lock/", data="{}", content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    response = http.post(
        f"/ops/tw/scenarios/{scenario.id}/optimise-v2/", data="{}", content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"


@pytest.mark.parametrize("kind", ["candidate", "global_plan"])
def test_preapproved_repair_apply_and_applied_rollback_are_blocked_by_planner(http, kind):
    from core.models import StudentTermSection
    from core.services.timetable_repair import (
        analyse_timetable_repair,
        approve_global_repair_plan,
        approve_repair_candidate,
        create_global_repair_plan,
    )
    from tests.test_timetable_repair_foundation import _fixture, _make_actual_unresolved_scope

    scenario, placement = _fixture()
    if kind == "candidate":
        created = analyse_timetable_repair(
            placement_id=placement.id,
            blocked_student_ids=[1002],
            limits={"max_candidates": 1},
        )
        run_id = created["run"]["id"]
        candidate_id = created["candidates"][0]["candidate_id"]
        assert approve_repair_candidate(run_id, candidate_id)["apply_enabled"]
        apply_url = f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/"
        rollback_url = f"/ops/tw/repair/runs/{run_id}/rollback/"
        detail_url = f"/ops/tw/repair/runs/{run_id}/"
    else:
        _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])
        created = create_global_repair_plan(
            scenario_id=scenario.id,
            program="AI",
            nominal_term=1,
            max_placements=1,
            limits={"max_candidates": 1},
        )
        plan_id = created["plan"]["id"]
        assert approve_global_repair_plan(plan_id)["plan"]["status"] == "approved"
        apply_url = f"/ops/tw/repair/global-plans/{plan_id}/apply/"
        rollback_url = f"/ops/tw/repair/global-plans/{plan_id}/rollback/"
        detail_url = f"/ops/tw/repair/global-plans/{plan_id}/"
    job = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario=scenario,
        mode="optimise_current",
        status="queued",
    )
    before = list(SectionPlacement.objects.order_by("id").values())
    before_students = list(StudentTermSection.objects.order_by("id").values())
    response = http.post(apply_url, data="{}", content_type="application/json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_BUSY"
    assert list(SectionPlacement.objects.order_by("id").values()) == before
    assert list(StudentTermSection.objects.order_by("id").values()) == before_students
    assert http.get(detail_url).status_code == 200

    # Once the planner finishes, the already approved candidate still applies.
    job.status = "succeeded"
    job.save(update_fields=["status"])
    response = http.post(apply_url, data="{}", content_type="application/json")
    assert response.status_code == 200, response.content
    applied = list(SectionPlacement.objects.order_by("id").values())
    applied_students = list(StudentTermSection.objects.order_by("id").values())
    job.status = "running"
    job.save(update_fields=["status"])
    response = http.post(rollback_url, data="{}", content_type="application/json")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["active_job"]["job_id"] == str(job.id)
    assert list(SectionPlacement.objects.order_by("id").values()) == applied
    assert list(StudentTermSection.objects.order_by("id").values()) == applied_students
    job.status = "succeeded"
    job.save(update_fields=["status"])
    assert http.post(rollback_url, data="{}", content_type="application/json").status_code == 200
