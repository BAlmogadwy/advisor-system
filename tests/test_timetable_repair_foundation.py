from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.utils import timezone

from core.models import (
    BoardSectionVisibility,
    Course,
    DeliveryBoard,
    Prerequisite,
    ProgrammeRequirement,
    Room,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionPlacement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableRepairApproval,
    TimetableRepairCandidate,
    TimetableRepairCandidateMetric,
    TimetableRepairGlobalPlan,
    TimetableRepairGlobalPlanItem,
    TimetableRepairJob,
    TimetableRepairRejectedCandidate,
    TimetableRepairRun,
    TimetableRepairSnapshot,
    TimetableRepairStudentChange,
    TimetableScenario,
)
from core.services import timetable_repair as repair_service
from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)
from core.services.timetable_repair import (
    TimetableRepairOperationError,
    analyse_timetable_repair,
    apply_approved_repair_candidate,
    apply_global_repair_plan,
    approve_global_repair_plan,
    approve_repair_candidate,
    create_global_repair_plan,
    repair_candidate_detail,
    repair_run_detail,
    repair_run_report,
    rollback_global_repair_plan,
    rollback_repair_run,
    simulate_timetable_repair_scope,
)
from core.services.timetable_repair_domain import (
    build_repair_domain_snapshot,
    build_repair_solver_problem_input,
)
from core.services.timetable_repair_eligibility import (
    build_repair_eligibility_context,
    repair_eligibility_summary,
    repair_section_ineligibility_reasons,
)
from core.services.timetable_repair_jobs import (
    cancel_repair_job,
    get_repair_job,
    recover_stale_repair_jobs,
    retry_repair_job,
    run_repair_job,
    serialize_repair_job,
    submit_repair_analysis_job,
    submit_repair_simulation_job,
)

pytestmark = pytest.mark.django_db


def _add_scenario_course_requests(
    scenario: TimetableScenario,
    student_id: int,
    courses: list[str],
) -> None:
    for course in courses:
        ScenarioStudentCourseRequest.objects.update_or_create(
            scenario=scenario,
            student_id=student_id,
            course_key=course,
            defaults={
                "course_code": course,
                "primary_term": 1,
                "status": ScenarioStudentCourseRequest.STATUS_REQUESTED,
                "priority": ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                "source": "test",
            },
        )


def _login_general(client: Client) -> User:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="repair-general")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, advisor_id="", departments="AI")
    client.force_login(user)
    return user


def _fixture(*, locked: bool = False) -> tuple[TimetableScenario, SectionPlacement]:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Repair Foundation",
        status="draft",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI M T1",
        nominal_term=1,
        program="AI",
        target_size=30,
    )
    target = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S1",
        available_capacity=30,
        registered_count=20,
        source_tag="test",
    )
    alt = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S2",
        available_capacity=30,
        registered_count=20,
        source_tag="test",
    )
    other = TermSection.objects.create(
        scenario=scenario,
        course_code="DS201",
        course_number="DS201",
        course_key="DS201",
        course_name="Data",
        section="S1",
        available_capacity=30,
        registered_count=20,
        source_tag="test",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=target,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="AI100",
        is_locked=locked,
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=alt,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI101",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=other,
        day="WED",
        start_time="09:00",
        end_time="10:15",
        room="AI102",
    )
    for code in ["AI100", "AI101", "AI102", "AI103"]:
        Room.objects.create(
            room_code=code,
            capacity=35,
            room_type="lecture",
            section="M",
            department="AI",
        )
    Student.objects.create(student_id=1001, name="One", program="AI", section="M")
    Student.objects.create(student_id=1002, name="Two", program="AI", section="M")
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        term_section=target,
        source="test",
    )
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        term_section=other,
        source="test",
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=1001,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
        recommended_course_keys=["AI101", "DS201"],
    )
    _add_scenario_course_requests(scenario, 1001, ["AI101", "DS201"])
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=1002,
        primary_term=1,
        recommended_courses=["AI101"],
        recommended_course_keys=["AI101"],
    )
    _add_scenario_course_requests(scenario, 1002, ["AI101"])
    return scenario, placement


def _make_actual_unresolved_scope(
    scenario: TimetableScenario,
    *,
    unresolved_courses: list[str] | None = None,
) -> None:
    """Shape the fixture so scope simulation has real evaluator-unresolved rows."""

    unresolved_courses = unresolved_courses or ["AI101"]
    board = DeliveryBoard.objects.get(scenario=scenario, program="AI", nominal_term=1)
    blocker = TermSection.objects.create(
        scenario=scenario,
        course_code="AA000",
        course_number="AA000",
        course_key="AA000",
        course_name="Blocking Course",
        section="S1",
        available_capacity=30,
        registered_count=1,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=blocker,
        day="WED",
        start_time="09:00",
        end_time="10:15",
        room="AI103",
    )
    ScenarioStudentMap.objects.filter(scenario=scenario, student_id=1001).update(
        recommended_courses=["AI101"],
        recommended_course_keys=["AI101"],
    )
    ScenarioStudentCourseRequest.objects.filter(
        scenario=scenario,
        student_id=1001,
        course_key="DS201",
    ).delete()
    requested = ["AA000", *unresolved_courses]
    ScenarioStudentMap.objects.filter(scenario=scenario, student_id=1002).update(
        recommended_courses=requested,
        recommended_course_keys=requested,
    )
    ScenarioStudentCourseRequest.objects.filter(scenario=scenario, student_id=1002).delete()
    _add_scenario_course_requests(scenario, 1002, requested)
    SectionPlacement.objects.filter(
        board__scenario=scenario,
        term_section__course_key__in=unresolved_courses,
    ).update(day="WED", start_time="09:00", end_time="10:15")


def _single_course_flow_fixture() -> tuple[TimetableScenario, SectionPlacement, TermSection]:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Repair Flow Shortcut",
        status="draft",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI M Flow",
        nominal_term=1,
        program="AI",
        target_size=30,
    )
    target = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S1",
        available_capacity=1,
        registered_count=1,
        source_tag="test",
    )
    alt = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S2",
        available_capacity=1,
        registered_count=0,
        source_tag="test",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=target,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="AI100",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=alt,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI101",
    )
    for code in ["AI100", "AI101"]:
        Room.objects.create(
            room_code=code,
            capacity=35,
            room_type="lecture",
            section="M",
            department="AI",
        )
    Student.objects.create(student_id=3001, name="Flow One", program="AI", section="M")
    Student.objects.create(student_id=3002, name="Flow Two", program="AI", section="M")
    StudentTermSection.objects.create(
        student_id=3001,
        academic_year="1448",
        term="1",
        term_section=target,
        source="test",
    )
    for student_id in [3001, 3002]:
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=student_id,
            primary_term=1,
            recommended_courses=["AI101"],
            recommended_course_keys=["AI101"],
        )
        _add_scenario_course_requests(scenario, student_id, ["AI101"])
    return scenario, placement, alt


def _cascade_fixture() -> tuple[TimetableScenario, SectionPlacement]:
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Repair Cascade",
        status="draft",
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI M Cascade",
        nominal_term=1,
        program="AI",
        target_size=30,
    )
    target = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S1",
        available_capacity=1,
        registered_count=1,
        source_tag="test",
    )
    target_alt = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S2",
        available_capacity=1,
        registered_count=0,
        source_tag="test",
    )
    ds_current = TermSection.objects.create(
        scenario=scenario,
        course_code="DS201",
        course_number="DS201",
        course_key="DS201",
        course_name="Data",
        section="S1",
        available_capacity=1,
        registered_count=1,
        source_tag="test",
    )
    ds_alt = TermSection.objects.create(
        scenario=scenario,
        course_code="DS201",
        course_number="DS201",
        course_key="DS201",
        course_name="Data",
        section="S2",
        available_capacity=1,
        registered_count=0,
        source_tag="test",
    )
    math_current = TermSection.objects.create(
        scenario=scenario,
        course_code="MA101",
        course_number="MA101",
        course_key="MA101",
        course_name="Math",
        section="S1",
        available_capacity=1,
        registered_count=1,
        source_tag="test",
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=target,
        day="MON",
        start_time="09:00",
        end_time="10:15",
        room="AI100",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=target_alt,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI101",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ds_current,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI102",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=ds_alt,
        day="WED",
        start_time="09:00",
        end_time="10:15",
        room="AI103",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=math_current,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI104",
    )
    for code in ["AI100", "AI101", "AI102", "AI103", "AI104"]:
        Room.objects.create(
            room_code=code,
            capacity=35,
            room_type="lecture",
            section="M",
            department="AI",
        )
    Student.objects.create(student_id=1001, name="One", program="AI", section="M")
    Student.objects.create(student_id=1002, name="Two", program="AI", section="M")
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        term_section=target,
        source="test",
    )
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        term_section=ds_current,
        source="test",
    )
    StudentTermSection.objects.create(
        student_id=1002,
        academic_year="1448",
        term="1",
        term_section=math_current,
        source="test",
    )
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=1001,
        primary_term=1,
        recommended_courses=["AI101", "DS201"],
        recommended_course_keys=["AI101", "DS201"],
    )
    _add_scenario_course_requests(scenario, 1001, ["AI101", "DS201"])
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=1002,
        primary_term=1,
        recommended_courses=["AI101", "MA101"],
        recommended_course_keys=["AI101", "MA101"],
    )
    _add_scenario_course_requests(scenario, 1002, ["AI101", "MA101"])
    return scenario, placement


def test_repair_analysis_creates_readonly_audit_run() -> None:
    _scenario, placement = _fixture()

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 3},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    assert run.status == TimetableRepairRun.STATUS_COMPLETED
    assert run.solver_version == "repair-solver-cpsat-flow-adaptive-lns-v3"
    assert run.constraint_version == "repair-constraints-eligibility-capacity-conflict-v2"
    assert run.objective_version == "repair-objective-requested-quality-v3"
    assert run.summary_json["versions"] == {
        "solver": run.solver_version,
        "constraints": run.constraint_version,
        "objective": run.objective_version,
        "cache": "repair-cache-evaluator-baseline-v9",
    }
    assert run.summary_json["student_solver"]["exact_cp_sat_reallocation"] == "enabled_readonly"
    assert run.summary_json["candidate_evaluation"]["mode"] == "in_memory_then_audited_bulk_persist"
    assert run.summary_json["candidate_evaluation"]["ranking_strategy"] == (
        "lexicographic_protect_recover_requested_minimize_disruption_quality"
    )
    assert run.summary_json["candidate_evaluation"]["selected_candidate_count"] == 3
    assert run.summary_json["candidate_evaluation"]["budget"]["limit_seconds"] > 0
    assert run.summary_json["blocked_demand"]["version"] == "blocked-demand-v1"
    assert run.summary_json["blocked_demand"]["active_student_ids"] == [1002]
    assert run.summary_json["blocked_demand"]["source_counts"]["explicit_student_id"] == 1
    assert run.summary_json["assignment_snapshot_available"] is True
    assert run.candidates.count() == 3
    assert run.candidates.filter(status="feasible").exists()
    solved = run.candidates.filter(status="feasible", solver_status__in=["optimal", "feasible"])
    assert solved.exists()
    best = solved.order_by("score_rank").first()
    exact = best.metrics_json["exact_repair"]
    ranking = best.metrics_json["ranking"]
    evaluation = best.metrics_json["evaluation"]
    metric_keys = set(best.metric_rows.values_list("metric_key", flat=True))
    assert TimetableRepairCandidateMetric.objects.filter(candidate=best).exists()
    assert "exact_repair.blocked_recovered" in metric_keys
    assert "exact_repair.existing_lost" in metric_keys
    assert "evaluation.solver_invoked" in metric_keys
    blocked_metric = best.metric_rows.get(metric_key="exact_repair.blocked_recovered")
    assert blocked_metric.category == "exact_repair"
    assert blocked_metric.value_number == 1
    assert (
        run.summary_json["student_solver"]["solver_strategy_counts"][exact["solver_strategy"]] >= 1
    )
    assert ranking["score_rank"] == 1
    assert ranking["strategy"] == "exact_repair_lexicographic"
    assert [row["name"] for row in ranking["criteria"]][:3] == [
        "protect_existing",
        "minimize_unresolved",
        "recover_blocked",
    ]
    assert evaluation["solver_invoked"] is True
    assert evaluation["candidate_loop_mode"] == "in_memory_then_audited_bulk_persist"
    assert exact["existing_lost"] == 0
    assert exact["blocked_recovered"] == 1
    assert exact["requested_courses_recovered"] == 1
    assert exact["timetable_quality"]["policy"] == (
        "final-tier spare-capacity weak-slot day-balance preferences"
    )
    assert {"spare_capacity", "weak_slot", "day_balance"}.issubset(
        exact["timetable_quality"]["components"]
    )
    assert exact["protected_existing_assignments"] == 2
    assert exact["objective"]["strategy"] == "staged_lexicographic_cp_sat"
    assert exact["solver_budget"]["policy"] == "per_candidate_staged_cp_sat_budget"
    assert exact["solver_domain"]["version"] == "repair-solver-problem-input-v1"
    assert exact["solver_domain"]["exact_assignment_source_available"] is True
    assert exact["solver_domain"]["counts"]["sections"] >= 2
    assert "AI101" in exact["solver_domain"]["sections_by_course"]
    assert exact["warm_start"]["enabled"] is True
    assert [stage["name"] for stage in exact["objective"]["trace"]] == [
        "maximize_blocked_recovery",
        "maximize_requested_course_recovery",
        "minimize_moved_students",
        "minimize_section_changes",
        "minimize_timetable_quality_penalty",
    ]
    assert all("runtime_ms" in stage for stage in exact["objective"]["trace"])
    assert TimetableRepairStudentChange.objects.filter(
        candidate=best,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
    ).exists()
    assert not TimetableRepairStudentChange.objects.filter(
        candidate__run=run,
        change_type=TimetableRepairStudentChange.CHANGE_LOST,
    ).exists()
    assert TimetableRepairSnapshot.objects.filter(run=run, kind="before").exists()
    assert TimetableRepairSnapshot.objects.filter(run=run, kind="component").exists()
    assert TimetableRepairApproval.objects.filter(run=run, status="pending").exists()
    assert detail["apply_enabled"] is False
    assert detail["summary"]["component_counts"]["students"] >= 2
    assert detail["summary"]["component_counts"]["domain_students"] >= 2
    assert detail["summary"]["component_counts"]["domain_requests"] >= 1
    assert detail["summary"]["component_counts"]["domain_assignments"] >= 2
    first_candidate = next(
        row for row in detail["candidates"] if row["candidate_id"] == best.candidate_id
    )
    assert any(
        row["metric_key"] == "exact_repair.blocked_recovered"
        for row in first_candidate["metric_rows"]
    )
    assert detail["student_changes"]
    component = TimetableRepairSnapshot.objects.get(run=run, kind="component").payload_json
    assert component["blocked_demand"]["active_request_count"] == 1
    assert component["domain"]["version"] == "repair-domain-snapshot-v1"
    assert component["domain"]["counts"]["students"] >= 2
    assert "AI101" in component["domain"]["indexes"]["sections_by_course"]
    assert "AI101" in component["domain"]["indexes"]["requesting_students_by_course"]
    assert component["expansion"]["seed_counts"]["blocked_students"] == 1
    assert component["expansion"]["seed_counts"]["target_course_sections"] == 2
    assert component["expansion"]["depth_trace"]
    assert component["locked"]["locked_placement_count"] == 0


def test_repair_domain_snapshot_builds_solver_indexes() -> None:
    scenario, placement = _fixture()
    section_ids = list(
        TermSection.objects.filter(
            scenario=scenario, course_key__in=["AI101", "DS201"]
        ).values_list("id", flat=True)
    )

    snapshot = build_repair_domain_snapshot(
        scenario.id,
        student_ids=[1001, 1002],
        course_keys=["AI101", "DS201"],
        section_ids=section_ids,
    ).to_audit_payload()

    assert snapshot["version"] == "repair-domain-snapshot-v1"
    assert snapshot["counts"]["students"] == 2
    assert snapshot["counts"]["sections"] == 3
    assert snapshot["counts"]["placements"] == 3
    assert snapshot["counts"]["assignments"] == 2
    assert snapshot["counts"]["requests"] == 3
    assert set(snapshot["indexes"]["sections_by_course"]["AI101"]) == {
        placement.term_section_id,
        TermSection.objects.get(scenario=scenario, course_key="AI101", section="S2").id,
    }
    assert snapshot["indexes"]["students_by_section"][str(placement.term_section_id)] == [1001]
    assert "DS201" in snapshot["indexes"]["courses_by_student"]["1001"]
    assert snapshot["indexes"]["requested_courses_by_student"]["1002"] == ["AI101"]


def test_repair_solver_problem_input_builds_native_boundary() -> None:
    scenario, placement = _fixture()
    section_ids = list(
        TermSection.objects.filter(
            scenario=scenario, course_key__in=["AI101", "DS201"]
        ).values_list("id", flat=True)
    )

    problem = build_repair_solver_problem_input(
        scenario.id,
        target_course_key="AI101",
        student_ids=[1001, 1002],
        blocked_student_ids=[1002],
        course_keys=["AI101", "DS201"],
        section_ids=section_ids,
    )

    assert problem.to_audit_payload()["version"] == "repair-solver-problem-input-v1"
    assert problem.exact_assignment_source_available is True
    assert problem.current_by_student_course[1001]["AI101"] == placement.term_section_id
    assert problem.requested_courses_by_student[1002] == {"AI101"}
    assert "AI101" in problem.sections_by_course
    assert placement.term_section_id in problem.sections_by_course["AI101"]
    assert problem.total_current_by_section[placement.term_section_id] == 1
    assert not problem.duplicate_current_assignments
    assert not problem.missing_current_options


def test_repair_objective_recovers_additional_requested_courses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, placement = _fixture()
    ma = TermSection.objects.create(
        scenario=scenario,
        course_code="MA101",
        course_number="MA101",
        course_key="MA101",
        course_name="Math",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=ma,
        day="WED",
        start_time="11:00",
        end_time="12:15",
        room="AI104",
    )
    Room.objects.create(
        room_code="AI104",
        capacity=35,
        room_type="lecture",
        section="M",
        department="AI",
    )
    _add_scenario_course_requests(scenario, 1002, ["AI101", "MA101"])
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "room_candidate_count": 1,
                    "selected_room": "AI100",
                    "source_badge": "Clean",
                    "source_critical_count": 0,
                    "source_warning_count": 0,
                    "source_student_affected_count": 0,
                    "source_impact_score": 0,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["solver_strategy"] == "student_level_cp_sat"
    assert exact["blocked_recovered"] == 1
    assert exact["requested_courses_recovered"] == 2
    assert exact["additional_requested_courses_recovered"] == 1
    assert exact["unresolved_requested_courses"] == 0
    assert [stage["name"] for stage in exact["objective"]["trace"]][1] == (
        "maximize_requested_course_recovery"
    )
    assert TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1002,
        course_key="MA101",
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
        after_section_id=str(ma.id),
    ).exists()


def test_repair_quality_penalty_includes_weak_slots_and_day_balance() -> None:
    section_meetings = {
        10: [{"day": "MON", "start_time": "08:00", "end_time": "09:15"}],
        11: [{"day": "TUE", "start_time": "10:00", "end_time": "11:15"}],
        20: [{"day": "MON", "start_time": "10:00", "end_time": "11:15"}],
    }
    section_quality = repair_service._section_quality_costs(
        section_ids={10, 11, 20},
        capacity_by_section={10: 50, 11: 50, 20: 50},
        fixed_occupancy_by_section={10: 0, 11: 0, 20: 0},
        section_meetings=section_meetings,
    )
    assignment_quality = repair_service._assignment_quality_costs(
        student_ids=[1002],
        option_ids_by_student_course={(1002, "AI101"): [10, 11]},
        current_by_student_course={1002: {"DS201": 20}},
        section_meetings=section_meetings,
        section_quality_cost_by_id=section_quality,
    )

    assert section_quality[10] > section_quality[11]
    assert assignment_quality[(1002, "AI101", 10)] > assignment_quality[(1002, "AI101", 11)]
    breakdown = repair_service._quality_penalty_breakdown(
        {(1002, "AI101"): 10},
        assignment_quality_cost_by_key=assignment_quality,
        section_quality_cost_by_id=section_quality,
        section_quality_components_by_id=repair_service._section_quality_components(
            section_ids={10, 11, 20},
            capacity_by_section={10: 50, 11: 50, 20: 50},
            fixed_occupancy_by_section={10: 0, 11: 0, 20: 0},
            section_meetings=section_meetings,
        ),
    )
    assert breakdown["weak_slot"] > 0
    assert breakdown["day_balance"] > 0


def test_repair_candidate_worker_plan_enables_bounded_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repair_service, "_running_under_pytest", lambda: False)

    plan = repair_service._repair_candidate_worker_plan(
        {"max_candidate_workers": 3},
        selected_candidate_count=5,
    )

    assert plan["enabled"] is True
    assert plan["strategy"] == "thread_pool_in_memory_candidate_compute"
    assert plan["worker_count"] == 3
    assert plan["database_write_policy"] == "deferred_bulk_persist_after_ranking"


def test_repair_candidate_worker_plan_stays_serial_in_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "repair-worker-plan")

    plan = repair_service._repair_candidate_worker_plan(
        {"max_candidate_workers": 3},
        selected_candidate_count=5,
    )

    assert plan["enabled"] is False
    assert plan["strategy"] == "serial_in_memory_candidate_compute"
    assert plan["reason"] == "pytest_thread_isolation"


def test_repair_policy_context_classifies_priority_and_mobility() -> None:
    _scenario, placement = _fixture()
    Student.objects.filter(student_id=1001).update(status="graduating final year")
    Student.objects.filter(student_id=1002).update(status="manual approval")
    StudentTermSection.objects.filter(
        student_id=1001,
        term_section__scenario=placement.board.scenario,
        term_section__course_key="AI101",
    ).update(source="protected manual")
    sections = list(TermSection.objects.filter(scenario=placement.board.scenario))

    context = build_repair_eligibility_context(
        scenario_id=placement.board.scenario_id,
        student_ids=[1001, 1002],
        sections=sections,
    )
    summary = repair_eligibility_summary(context)

    assert context.students[1001].priority_group == "graduating"
    assert context.students[1001].graduation_priority is True
    assert context.students[1001].mobility_policy == "priority_minimise_disruption"
    assert context.students[1002].priority_group == "manual_approval"
    assert context.students[1002].protected is True
    assert context.students[1002].mobility_policy == "fixed"
    assert summary["priority_group_counts"] == {"graduating": 1, "manual_approval": 1}
    assert summary["mobility_policy_counts"] == {
        "fixed": 1,
        "priority_minimise_disruption": 1,
    }
    assert summary["protected_student_count"] == 1
    assert summary["graduation_priority_count"] == 1
    assert summary["protected_assignment_count"] == 1


def test_repair_eligibility_rejects_unplaced_closed_and_wrong_room_side() -> None:
    scenario, placement = _fixture()
    unplaced = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S3",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    closed = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S4",
        available_capacity=30,
        registered_count=0,
        source_tag="closed_by_department",
    )
    room_side = TermSection.objects.create(
        scenario=scenario,
        course_code="AI101",
        course_number="AI101",
        course_key="AI101",
        course_name="Intro AI",
        section="S5",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=closed,
        day="WED",
        start_time="13:00",
        end_time="14:15",
        room="AI100",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=room_side,
        day="THU",
        start_time="13:00",
        end_time="14:15",
        room="F100",
    )
    Room.objects.create(
        room_code="F100",
        capacity=35,
        room_type="lecture",
        section="F",
        department="AI",
    )

    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[unplaced, closed, room_side],
    )

    unplaced_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI101",
            section=unplaced,
            is_new_course=True,
        )
    }
    closed_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI101",
            section=closed,
            is_new_course=True,
        )
    }
    room_side_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI101",
            section=room_side,
            is_new_course=True,
        )
    }
    summary = repair_eligibility_summary(context)

    assert "UNPLACED_SECTION" in unplaced_codes
    assert "CLOSED_SECTION" in closed_codes
    assert "ROOM_SECTION_MISMATCH" in room_side_codes
    assert context.sections[room_side.id].room_sections == ("F",)
    assert (
        "sections_must_have_a_timetable_placement_before_student_reassignment" in summary["rules"]
    )


def test_repair_eligibility_uses_academic_course_code_for_history_and_prereqs() -> None:
    scenario, placement = _fixture()
    programming = TermSection.objects.create(
        scenario=scenario,
        course_code="CS111",
        course_number="CS111",
        course_key="CS111::PROGRAMMING_I",
        course_name="Programming I",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=programming,
        day="THU",
        start_time="09:00",
        end_time="10:15",
        room="AI100",
    )
    Prerequisite.objects.create(
        program="AI",
        course_code="CS111",
        prerequisite_course_code="AI099",
    )

    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[programming],
    )
    missing_prereq_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="CS111::PROGRAMMING_I",
            section=programming,
            is_new_course=True,
        )
    }

    assert context.sections[programming.id].academic_course_code == "CS111"
    assert "MISSING_PREREQUISITES" in missing_prereq_codes

    course, _ = Course.objects.get_or_create(course_code="CS111")
    StudentCourse.objects.get_or_create(
        student=Student.objects.get(student_id=1002),
        course=course,
        defaults={"status": "passed"},
    )
    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[programming],
    )
    already_taken_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="CS111::PROGRAMMING_I",
            section=programming,
            is_new_course=True,
        )
    }

    assert "COURSE_ALREADY_TAKEN_OR_STUDYING" in already_taken_codes


def test_repair_eligibility_enforces_room_inventory_and_online_policy() -> None:
    scenario, placement = _fixture()
    no_room = TermSection.objects.create(
        scenario=scenario,
        course_code="AI102",
        course_number="AI102",
        course_key="AI102",
        course_name="No Room",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    stale_room = TermSection.objects.create(
        scenario=scenario,
        course_code="AI103",
        course_number="AI103",
        course_key="AI103",
        course_name="Missing Inventory",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    room_mismatch = TermSection.objects.create(
        scenario=scenario,
        course_code="AI104",
        course_number="AI104",
        course_key="AI104",
        course_name="Room Mismatch",
        section="S1",
        available_capacity=30,
        registered_count=30,
        source_tag="test",
    )
    online_section = TermSection.objects.create(
        scenario=scenario,
        course_code="ON101",
        course_number="ON101",
        course_key="ON101",
        course_name="Online Course",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    Room.objects.create(
        room_code="LABX",
        capacity=5,
        room_type="lab",
        section="M",
        department="DS",
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="ON101",
        course_name="Online Course",
        type="Core",
        programme_term=1,
        credit_hours=3,
        is_online=True,
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=no_room,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="UNASSIGNED",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=stale_room,
        day="TUE",
        start_time="13:00",
        end_time="14:15",
        room="MISSING100",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=room_mismatch,
        day="WED",
        start_time="13:00",
        end_time="14:15",
        room="LABX",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=online_section,
        day="THU",
        start_time="13:00",
        end_time="14:15",
        room="AI100",
    )

    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[no_room, stale_room, room_mismatch, online_section],
    )

    no_room_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI102",
            section=no_room,
        )
    }
    stale_room_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI103",
            section=stale_room,
        )
    }
    mismatch_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI104",
            section=room_mismatch,
        )
    }
    online_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="ON101",
            section=online_section,
        )
    }
    summary = repair_eligibility_summary(context)

    assert "PHYSICAL_ROOM_REQUIRED" in no_room_codes
    assert "ROOM_INVENTORY_MISSING" in stale_room_codes
    assert "ROOM_TYPE_MISMATCH" in mismatch_codes
    assert "ROOM_CAPACITY_MISMATCH" in mismatch_codes
    assert "ROOM_DEPARTMENT_MISMATCH" in mismatch_codes
    assert "ONLINE_SECTION_HAS_PHYSICAL_ROOM" in online_codes
    assert context.sections[online_section.id].online is True
    assert "assigned_rooms_must_exist_in_room_inventory" in summary["rules"]


def test_repair_eligibility_enforces_canonical_cohort_and_campus_policy() -> None:
    scenario, placement = _fixture()
    campus_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI201",
        course_number="AI201",
        course_key="AI201",
        course_name="Campus Policy",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    Room.objects.create(
        room_code="SAT100",
        capacity=35,
        room_type="lecture",
        section="M",
        department="AI",
        building="SAT",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=campus_section,
        day="THU",
        start_time="14:45",
        end_time="16:00",
        room="SAT100",
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=1002,
        course_key="AI201",
        course_code="AI201",
        primary_term=2,
        is_cross_term=False,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
        source_payload={"campus": "MAIN"},
    )

    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[campus_section],
    )
    codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI201",
            section=campus_section,
            is_new_course=True,
        )
    }
    summary = repair_eligibility_summary(context)

    assert "COHORT_TERM_MISMATCH" in codes
    assert "CAMPUS_MISMATCH" in codes
    assert context.sections[campus_section.id].board_terms == (1,)
    assert context.sections[campus_section.id].campus_codes == ("SAT",)
    assert "campus_policy_uses_explicit_request_campus_when_supplied" in summary["rules"]
    assert "request_primary_term_matches_section_board_term_unless_cross_term" in summary["rules"]


def test_repair_eligibility_enforces_manual_visibility_and_instructor_policy() -> None:
    scenario, placement = _fixture()
    other_board = DeliveryBoard.objects.create(
        scenario=scenario,
        label="AI Term 2 Manual Cohort",
        nominal_term=2,
        program="AI",
        target_size=30,
    )
    restricted = TermSection.objects.create(
        scenario=scenario,
        course_code="AI202",
        course_number="AI202",
        course_key="AI202",
        course_name="Manual Cohort",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=restricted,
        day="THU",
        start_time="13:00",
        end_time="14:15",
        room="AI100",
    )
    BoardSectionVisibility.objects.create(board=other_board, term_section=restricted)
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=1002,
        course_key="AI202",
        course_code="AI202",
        primary_term=1,
        is_cross_term=False,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
    )

    instructor_section = TermSection.objects.create(
        scenario=scenario,
        course_code="AI203",
        course_number="AI203",
        course_key="AI203",
        course_name="Instructor Policy",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    conflict_section = TermSection.objects.create(
        scenario=scenario,
        course_code="DS999",
        course_number="DS999",
        course_key="DS999",
        course_name="Conflict Section",
        section="S1",
        available_capacity=30,
        registered_count=0,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=instructor_section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="AI101",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=conflict_section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="AI102",
    )
    TermSectionMeeting.objects.create(
        term_section=instructor_section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="AI101",
        instructor="Dr Shared",
    )
    TermSectionMeeting.objects.create(
        term_section=conflict_section,
        day="MON",
        start_time="13:00",
        end_time="14:15",
        room="AI102",
        instructor="Dr Shared",
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=scenario,
        student_id=1002,
        course_key="AI203",
        course_code="AI203",
        primary_term=1,
        is_cross_term=False,
        status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
        priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
        source="test",
    )

    context = build_repair_eligibility_context(
        scenario_id=scenario.id,
        student_ids=[1002],
        sections=[restricted, instructor_section],
    )
    restricted_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI202",
            section=restricted,
            is_new_course=True,
        )
    }
    instructor_codes = {
        row["code"]
        for row in repair_section_ineligibility_reasons(
            context,
            student_id=1002,
            course_key="AI203",
            section=instructor_section,
            is_new_course=True,
        )
    }
    summary = repair_eligibility_summary(context)

    assert "MANUAL_COHORT_RESTRICTION" in restricted_codes
    assert context.sections[restricted.id].visibility_restricted is True
    assert context.sections[restricted.id].visible_board_ids == (other_board.id,)
    assert "INSTRUCTOR_CONFLICT" in instructor_codes
    assert context.sections[instructor_section.id].instructors == ("Dr Shared",)
    assert context.sections[instructor_section.id].instructor_conflicts
    assert "manual_board_visibility_restricts_section_cohorts_when_configured" in summary["rules"]
    assert "instructor_conflicts_reject_new_automated_reassignments" in summary["rules"]


def test_repair_analysis_reuses_cached_completed_run_when_state_unchanged() -> None:
    _scenario, placement = _fixture()

    first = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    second = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert second["run"]["id"] == first["run"]["id"]
    assert (
        TimetableRepairRun.objects.filter(
            target_placement=placement,
            status=TimetableRepairRun.STATUS_COMPLETED,
        ).count()
        == 1
    )


def test_repair_analysis_cache_invalidates_when_timetable_changes() -> None:
    _scenario, placement = _fixture()

    first = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    placement.room = "AI103"
    placement.save(update_fields=["room"])

    second = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    stale_detail = repair_run_detail(first["run"]["id"])

    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is False
    assert second["run"]["id"] != first["run"]["id"]
    assert stale_detail["run_freshness"]["status"] == "stale"
    assert stale_detail["run_freshness"]["requires_rerun"] is True
    assert any(
        row["code"] == "REPAIR_RUN_STALE"
        for row in stale_detail["run_freshness"]["blocking_reasons"]
    )
    assert (
        TimetableRepairRun.objects.filter(
            target_placement=placement,
            status=TimetableRepairRun.STATUS_COMPLETED,
        ).count()
        == 2
    )


def test_repair_balanced_mode_uses_balanced_objective_policy() -> None:
    _scenario, placement = _fixture()

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        mode=TimetableRepairRun.MODE_BALANCED,
        limits={"max_candidates": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    assert run.mode == TimetableRepairRun.MODE_BALANCED
    candidate = run.candidates.get(candidate_id="cand_001")
    exact = candidate.metrics_json["exact_repair"]
    assert exact["mode"] == TimetableRepairRun.MODE_BALANCED
    assert exact["mode_policy"]["existing_course_policy"] == "hard_protected_no_loss"
    assert exact["mode_policy"]["analysis_only"] is False
    assert exact["objective"]["strategy"] == "balanced_lexicographic_cp_sat"
    assert [stage["name"] for stage in exact["objective"]["trace"]] == [
        "maximize_blocked_recovery",
        "maximize_requested_course_recovery",
        "minimize_section_changes",
        "minimize_moved_students",
        "minimize_timetable_quality_penalty",
    ]


def test_repair_simulation_mode_is_analysis_only() -> None:
    _scenario, placement = _fixture()

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        mode=TimetableRepairRun.MODE_SIMULATION,
        limits={"max_candidates": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    candidate = run.candidates.get(candidate_id="cand_001")
    exact = candidate.metrics_json["exact_repair"]
    assert run.mode == TimetableRepairRun.MODE_SIMULATION
    assert exact["mode_policy"]["analysis_only"] is True
    assert exact["objective"]["strategy"] == "simulation_lexicographic_cp_sat"

    with pytest.raises(TimetableRepairOperationError) as exc:
        approve_repair_candidate(run.id, candidate.candidate_id)

    assert exc.value.code == "REPAIR_SIMULATION_ONLY"
    assert (
        TimetableRepairApproval.objects.filter(
            run=run,
            status=TimetableRepairApproval.STATUS_APPROVED,
        ).count()
        == 0
    )


def test_repair_scope_simulation_scans_actual_unresolved_course_demand() -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])

    result = simulate_timetable_repair_scope(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
    )

    assert result["simulation_version"] == "repair-scope-simulation-v1"
    assert result["api_contract"]["version"] == "repair-simulation-api-contract-v1"
    assert (
        result["api_contract"]["endpoint_templates"]["targeted_analysis"]
        == "/ops/tw/repair/analyse/"
    )
    assert result["analysis_only"] is True
    assert result["governance"]["analysis_only"] is True
    assert result["governance"]["apply_allowed"] is False
    assert result["governance"]["approval_allowed"] is False
    assert result["scope"]["selected_target_count"] == 1
    assert result["demand"]["source"] == "current_assignment_unresolved"
    assert result["demand"]["actual_unresolved_student_count"] == 1
    assert result["demand"]["total_unserved_requests"] == 1
    assert result["demand"]["total_unresolved_requests"] == 1
    assert result["domain"]["version"] == "repair-domain-snapshot-v1"
    assert result["domain"]["counts"]["requests"] >= 1
    assert "AI101" in result["domain"]["indexes"]["sections_by_course"]
    assert result["aggregate"]["selected_target_count"] == 1
    assert result["aggregate"]["scanned_run_count"] == 1
    assert result["aggregate"]["blocked_recovered_best_sum"] == 1
    assert result["aggregate"]["existing_lost_best_sum"] == 0
    assert result["aggregate"]["zero_harm_opportunity_count"] == 1
    assert result["batch_plan"]["version"] == "repair-simulation-batch-plan-v1"
    assert result["batch_plan"]["analysis_only"] is True
    assert result["batch_plan"]["selected_count"] == 1
    assert result["batch_plan"]["estimated_totals"]["blocked_recovered"] == 1
    assert result["batch_plan"]["selected"][0]["course_key"] == "AI101"
    assert result["batch_plan"]["admin_summary"]["selected_count"] == 1
    assert result["batch_plan"]["admin_summary"]["estimated_blocked_recovered"] == 1
    assert result["batch_plan"]["action_queue"][0]["action"] == "run_fresh_targeted_analysis"
    assert result["batch_plan"]["action_queue"][0]["payload"] == {
        "placement_id": result["selected_targets"][0]["placement_id"],
        "blocked_student_ids": [1002],
        "mode": TimetableRepairRun.MODE_CONSERVATIVE,
    }
    assert result["batch_plan"]["selected"][0]["links"]["candidate_detail"].endswith("/cand_001/")
    assert result["selected_targets"][0]["course_key"] == "AI101"
    assert result["selected_targets"][0]["blocked_student_ids"] == [1002]
    assert len(result["runs"]) == 1
    run = TimetableRepairRun.objects.get(id=result["runs"][0]["run_id"])
    assert run.mode == TimetableRepairRun.MODE_SIMULATION
    assert (
        run.summary_json["student_solver"]["best_candidate_metrics"]["mode_policy"]["analysis_only"]
        is True
    )
    assert result["best_opportunities"][0]["best_candidate"]["metrics"]["blocked_recovered"] == 1


def test_simulation_target_selection_covers_more_unresolved_courses_before_extra_sections() -> None:
    def fake_placement(pk: int, course: str, section: str):
        return SimpleNamespace(
            id=pk,
            term_section_id=pk * 10,
            board_id=1,
            board=SimpleNamespace(label="AI T1", program="AI", nominal_term=1),
            term_section=SimpleNamespace(
                course_key=course,
                course_code=course,
                section=section,
            ),
            day="MON",
            start_time="09:00",
            end_time="10:15",
            room=f"AI{pk:03d}",
        )

    targets = repair_service._simulation_placement_targets(
        placements=[
            fake_placement(1, "AI101", "S1"),
            fake_placement(2, "AI101", "S2"),
            fake_placement(3, "DS201", "S1"),
        ],
        blocked_by_course={
            "AI101": [1001, 1002, 1003],
            "DS201": [2001, 2002],
        },
        max_placements=2,
        max_students=20,
    )

    assert [row["course_key"] for row in targets] == ["AI101", "DS201"]
    assert [row["target_selection_round"] for row in targets] == [
        "first_unresolved_course_pass",
        "first_unresolved_course_pass",
    ]


def test_simulation_run_row_does_not_treat_unsolved_candidate_as_zero_unresolved() -> None:
    row = repair_service._simulation_run_row(
        {
            "placement_id": 10,
            "term_section_id": 20,
            "course_key": "AI101",
            "section": "S1",
            "blocked_student_count": 7,
            "blocked_student_ids": [1, 2, 3, 4, 5, 6, 7],
        },
        {
            "run": {"id": "run-1"},
            "candidates": [
                {
                    "candidate_id": "cand_001",
                    "score_rank": 1,
                    "status": TimetableRepairCandidate.STATUS_NOT_SOLVED,
                    "solver_status": "too_large",
                    "metrics": {},
                }
            ],
            "student_changes": [],
        },
    )

    metrics = row["best_candidate"]["metrics"]
    assert metrics["exact_solved"] is False
    assert metrics["blocked_recovered"] == 0
    assert metrics["unresolved_blocked"] == 7


def test_repair_scope_simulation_batch_plan_avoids_student_overlap() -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101", "DS201"])

    result = simulate_timetable_repair_scope(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        course_keys=["AI101", "DS201"],
        max_placements=3,
        limits={"max_candidates": 1, "max_batch_opportunities": 3},
    )

    assert result["aggregate"]["blocked_recovered_best_sum"] >= 2
    assert result["batch_plan"]["selection_policy"] == (
        "exact_cp_sat_zero_harm_non_overlapping_students_sections_courses"
    )
    assert result["batch_plan"]["optimizer"]["used"] is True
    assert result["batch_plan"]["selected_count"] == 1
    assert result["batch_plan"]["estimated_totals"]["blocked_recovered"] == 1
    assert result["batch_plan"]["admin_summary"]["skip_reason_counts"]["STUDENT_OVERLAP"] >= 1
    assert any(row["code"] == "STUDENT_OVERLAP" for row in result["batch_plan"]["skipped"])
    assert result["batch_plan"]["overlap_guards"]["student_overlap"] is True


def test_global_repair_plan_converts_simulation_into_applyable_batch() -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])

    plan = create_global_repair_plan(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
    )

    assert plan["global_plan_version"] == "repair-global-plan-v1"
    assert plan["summary"]["primary_objective"] == "minimize_unresolved_students"
    assert plan["summary"]["estimated_totals"]["distinct_target_students_recovered"] == 1
    assert plan["summary"]["estimated_totals"]["blocked_recovered"] == 1
    assert plan["summary"]["estimated_totals"]["existing_lost"] == 0
    assert plan["summary"]["governance"]["cross_board_is_not_primary_objective"] is True
    assert plan["plan"]["status"] == TimetableRepairGlobalPlan.STATUS_DRAFT
    assert len(plan["items"]) == 1
    assert plan["items"][0]["status"] == TimetableRepairGlobalPlanItem.STATUS_READY

    plan_id = plan["plan"]["id"]
    approved = approve_global_repair_plan(plan_id)
    assert approved["plan"]["status"] == TimetableRepairGlobalPlan.STATUS_APPROVED
    assert approved["items"][0]["status"] == TimetableRepairGlobalPlanItem.STATUS_APPROVED

    applied = apply_global_repair_plan(plan_id)
    assert applied["plan"]["status"] == TimetableRepairGlobalPlan.STATUS_APPLIED
    assert applied["items"][0]["status"] == TimetableRepairGlobalPlanItem.STATUS_APPLIED
    assert StudentTermSection.objects.filter(
        student_id=1002,
        term_section__scenario=scenario,
        term_section__course_key="AI101",
    ).exists()

    rolled_back = rollback_global_repair_plan(plan_id)
    assert rolled_back["plan"]["status"] == TimetableRepairGlobalPlan.STATUS_ROLLED_BACK
    assert rolled_back["items"][0]["status"] == TimetableRepairGlobalPlanItem.STATUS_ROLLED_BACK
    assert not StudentTermSection.objects.filter(
        student_id=1002,
        term_section__scenario=scenario,
        term_section__course_key="AI101",
    ).exists()


def test_global_repair_plan_api_contract(client: Client) -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])
    _login_general(client)

    response = client.post(
        "/ops/tw/repair/global-plans/",
        data=json.dumps(
            {
                "scenario_id": scenario.id,
                "program": "AI",
                "nominal_term": 1,
                "max_placements": 1,
                "limits": {"max_candidates": 1},
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["global_plan_version"] == "repair-global-plan-v1"
    assert payload["api_contract"]["primary_objective"] == "minimize_unresolved_students"
    assert payload["summary"]["estimated_totals"]["blocked_recovered"] == 1
    plan_id = payload["plan"]["id"]

    approve_response = client.post(
        f"/ops/tw/repair/global-plans/{plan_id}/approve/",
        data="{}",
        content_type="application/json",
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["plan"]["status"] == TimetableRepairGlobalPlan.STATUS_APPROVED


def test_simulation_batch_plan_uses_exact_selection_over_greedy_rank() -> None:
    rows = [
        _fake_simulation_opportunity(
            index=1,
            course_key="A",
            blocked_recovered=5,
            affected_students=[1, 2],
            section_ids=[10],
        ),
        _fake_simulation_opportunity(
            index=2,
            course_key="B",
            blocked_recovered=4,
            affected_students=[1],
            section_ids=[20],
        ),
        _fake_simulation_opportunity(
            index=3,
            course_key="C",
            blocked_recovered=4,
            affected_students=[2],
            section_ids=[30],
        ),
    ]

    plan = repair_service._simulation_batch_plan(
        opportunity_rows=rows,
        max_opportunities=2,
    )

    assert (
        plan["selection_policy"]
        == "exact_cp_sat_zero_harm_non_overlapping_students_sections_courses"
    )
    assert plan["optimizer"]["used"] is True
    assert plan["selected_count"] == 2
    assert plan["admin_summary"]["selected_count"] == 2
    assert len(plan["action_queue"]) == 2
    assert {row["course_key"] for row in plan["selected"]} == {"B", "C"}
    assert plan["estimated_totals"]["blocked_recovered"] == 8
    assert any(
        row["course_key"] == "A" and row["code"] == "STUDENT_OVERLAP" for row in plan["skipped"]
    )


def _fake_simulation_opportunity(
    *,
    index: int,
    course_key: str,
    blocked_recovered: int,
    affected_students: list[int],
    section_ids: list[int],
) -> dict:
    return {
        "run_id": f"run-{index}",
        "placement_id": index,
        "term_section_id": section_ids[0],
        "course_key": course_key,
        "section": f"S{index}",
        "best_candidate": {
            "candidate_id": "cand_001",
            "placement": {
                "day": "MON",
                "start_time": "09:00",
                "end_time": "10:15",
                "room": f"R{index}",
            },
            "metrics": {
                "existing_lost": 0,
                "blocked_recovered": blocked_recovered,
                "requested_courses_recovered": blocked_recovered,
                "students_moved": len(affected_students),
                "section_changes": blocked_recovered,
                "quality_penalty": 0,
            },
            "impact": {
                "affected_student_ids": affected_students,
                "moved_student_ids": affected_students,
                "target_recovered_student_ids": affected_students,
                "section_ids": section_ids,
                "course_keys": [course_key],
                "change_count": len(affected_students),
            },
        },
    }


def test_repair_reports_solver_profile_compression_metadata() -> None:
    scenario, placement = _fixture()
    Student.objects.create(student_id=1003, name="Three", program="AI", section="M")
    ScenarioStudentMap.objects.create(
        scenario=scenario,
        student_id=1003,
        primary_term=1,
        recommended_courses=["AI101"],
        recommended_course_keys=["AI101"],
    )
    _add_scenario_course_requests(scenario, 1003, ["AI101"])

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002, 1003],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    compression = candidate.metrics_json["exact_repair"]["profile_compression"]
    exact = candidate.metrics_json["exact_repair"]
    assert exact["solver_strategy"] == "profile_pattern_cp_sat"
    assert exact["variables"] < exact["student_level_variables"]
    assert exact["profile_solver"]["strategy"] == "profile_pattern_cp_sat"
    assert exact["warm_start"]["strategy"] == "profile_current_pattern_cp_sat_hint"
    assert exact["warm_start"]["used"] is True
    assert compression["solver_used"] is True
    assert compression["enabled"] is True
    assert compression["strategy"] == "solver_option_signature_v1"
    assert compression["student_count"] == 3
    assert compression["profile_count"] == 2
    assert compression["largest_profile_size"] == 2
    assert (
        compression["estimated_profile_variable_count"]
        < compression["student_level_variable_count"]
    )
    assert compression["estimated_variable_reduction"] > 0
    assert any(
        row["student_count"] == 2 and set(row["sample_student_ids"]) == {1002, 1003}
        for row in compression["sample_profiles"]
    )


def test_repair_conflicts_use_at_most_one_slot_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, placement = _fixture()
    ma = TermSection.objects.create(
        scenario=scenario,
        course_code="MA101",
        course_number="MA101",
        course_key="MA101",
        course_name="Math",
        section="S1",
        available_capacity=30,
        registered_count=20,
        source_tag="test",
    )
    SectionPlacement.objects.create(
        board=placement.board,
        term_section=ma,
        day="TUE",
        start_time="09:00",
        end_time="10:15",
        room="AI103",
    )
    StudentTermSection.objects.create(
        student_id=1001,
        academic_year="1448",
        term="1",
        term_section=ma,
        source="test",
    )
    ScenarioStudentMap.objects.filter(scenario=scenario, student_id=1001).update(
        recommended_courses=["AI101", "DS201", "MA101"],
        recommended_course_keys=["AI101", "DS201", "MA101"],
    )
    _add_scenario_course_requests(scenario, 1001, ["AI101", "DS201", "MA101"])
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "scan_limit": 1,
                    "hard_rejection_count": 0,
                    "room_policy_clean": True,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    policy = candidate.metrics_json["exact_repair"]["conflict_policy"]
    assert policy["strategy"] == "equal_slot_at_most_one_then_pairwise_overlap"
    assert policy["too_large"] is False
    assert policy["at_most_one_constraints"] >= 1
    assert policy["logical_conflict_edges"] >= 1
    assert any(row["strategy"] == "at_most_one" for row in policy["samples"])


def test_repair_conflict_edge_limit_marks_candidate_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, placement = _fixture()
    for code, room in [("MA101", "AI103"), ("PH101", "AI104")]:
        section = TermSection.objects.create(
            scenario=scenario,
            course_code=code,
            course_number=code,
            course_key=code,
            course_name=code,
            section="S1",
            available_capacity=30,
            registered_count=20,
            source_tag="test",
        )
        SectionPlacement.objects.create(
            board=placement.board,
            term_section=section,
            day="TUE",
            start_time="09:00",
            end_time="10:15",
            room=room,
        )
        StudentTermSection.objects.create(
            student_id=1001,
            academic_year="1448",
            term="1",
            term_section=section,
            source="test",
        )
        Room.objects.get_or_create(
            room_code=room,
            section="M",
            defaults={"capacity": 35, "room_type": "lecture", "department": "AI"},
        )
    ScenarioStudentMap.objects.filter(scenario=scenario, student_id=1001).update(
        recommended_courses=["AI101", "DS201", "MA101", "PH101"],
        recommended_course_keys=["AI101", "DS201", "MA101", "PH101"],
    )
    _add_scenario_course_requests(scenario, 1001, ["AI101", "DS201", "MA101", "PH101"])
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "scan_limit": 1,
                    "hard_rejection_count": 0,
                    "room_policy_clean": True,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1, "max_conflict_edges": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert candidate.status == TimetableRepairCandidate.STATUS_NOT_SOLVED
    assert candidate.solver_status == "too_large"
    assert exact["conflict_policy"]["too_large"] is True
    assert exact["conflict_policy"]["max_conflict_edges"] == 1


def test_locked_target_candidates_are_rejected_before_solver() -> None:
    _scenario, placement = _fixture(locked=True)

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 2},
    )

    assert detail["summary"]["feasible_candidate_count"] == 0
    assert detail["summary"]["rejected_candidate_count"] == 2
    rejected = TimetableRepairRejectedCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_key="cand_001",
    )
    assert any(reason["code"] == "TARGET_PLACEMENT_LOCKED" for reason in rejected.reasons_json)


def test_repair_candidate_generation_scans_past_early_room_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _fixture()
    rows = [
        {
            "rank": 1,
            "kind": "lect",
            "day": "SUN",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 0,
            "impact_score": 0,
            "badge": "Clean",
            "evidence": [],
        },
        {
            "rank": 2,
            "kind": "lect",
            "day": "TUE",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 0,
            "impact_score": 1,
            "badge": "Clean",
            "evidence": [],
        },
    ]

    monkeypatch.setattr(
        repair_service,
        "preview_placement_slot_candidates",
        lambda _placement_id: {"candidates": rows},
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    def fake_select_room(_placement, day: str, _start: str, _end: str, **_kwargs):
        if day == "SUN":
            return (
                "",
                [{"code": "NO_POLICY_CLEAN_ROOM", "message": "No clean room is available"}],
                {"selected_room": "", "policy_clean": False},
            )
        return "AI100", [], {"selected_room": "AI100", "policy_clean": True}

    monkeypatch.setattr(repair_service, "_select_room_for_candidate", fake_select_room)
    monkeypatch.setattr(
        repair_service,
        "hard_feasibility_rejections",
        lambda _placement, **kwargs: list(kwargs.get("room_reasons") or []),
    )

    def fake_solve(_run, candidate, _placement, *, component, limits):
        metrics = dict(candidate.get("metrics") or {})
        metrics["exact_repair"] = {
            "enabled": True,
            "solver_status": "optimal",
            "existing_lost": 0,
            "blocked_recovered": 0,
            "unresolved_blocked": 0,
            "students_moved": 0,
            "section_changes": 0,
        }
        candidate["metrics"] = metrics
        candidate["solver_status"] = "optimal"
        candidate["status"] = TimetableRepairCandidate.STATUS_FEASIBLE
        return metrics

    monkeypatch.setattr(repair_service, "solve_conservative_student_repair", fake_solve)

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(run_id=detail["run"]["id"])
    assert candidate.day == "TUE"
    assert candidate.room == "AI100"
    assert candidate.status == TimetableRepairCandidate.STATUS_FEASIBLE
    assert candidate.metrics_json["generation"]["source_rank"] == 2


def test_move_scope_all_sessions_includes_lab_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _fixture()
    lab = SectionPlacement.objects.create(
        board=placement.board,
        term_section=placement.term_section,
        day="WED",
        start_time="13:00",
        end_time="14:40",
        room="AI103",
    )
    rows = [
        {
            "rank": 1,
            "kind": "lect",
            "day": "TUE",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 0,
            "impact_score": 0,
            "badge": "Clean",
            "evidence": [],
        }
    ]
    monkeypatch.setattr(
        repair_service,
        "preview_placement_slot_candidates",
        lambda _placement_id: {"candidates": rows},
    )
    monkeypatch.setattr(
        repair_service,
        "_select_room_for_candidate",
        lambda _placement, _day, _start, _end, **_kwargs: (
            "AI100",
            [],
            {"selected_room": "AI100", "policy_clean": True},
        ),
    )
    monkeypatch.setattr(repair_service, "hard_feasibility_rejections", lambda *_args, **_kwargs: [])

    prepared = repair_service._prepare_repair_candidate_rows(
        placement,
        limits={"max_candidates": 1},
        move_scope="all_sessions",
    )

    moves = prepared[0]["move_scope_payload"]["moves"]
    by_id = {int(move["placement_id"]): move for move in moves}
    assert set(by_id) == {placement.id, lab.id}
    assert by_id[placement.id]["day"] == "TUE"
    assert by_id[lab.id]["day"] == "THU"
    assert by_id[lab.id]["start"] == "13:00"
    assert prepared[0]["generation"]["move_scope"]["scope"] == "all_sessions"


def test_move_scope_lectures_only_excludes_lab_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _fixture()
    lecture = SectionPlacement.objects.create(
        board=placement.board,
        term_section=placement.term_section,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
        room="AI102",
    )
    lab = SectionPlacement.objects.create(
        board=placement.board,
        term_section=placement.term_section,
        day="WED",
        start_time="13:00",
        end_time="14:40",
        room="AI103",
    )
    rows = [
        {
            "rank": 1,
            "kind": "lect",
            "day": "TUE",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 0,
            "impact_score": 0,
            "badge": "Clean",
            "evidence": [],
        }
    ]
    monkeypatch.setattr(
        repair_service,
        "preview_placement_slot_candidates",
        lambda _placement_id: {"candidates": rows},
    )
    monkeypatch.setattr(
        repair_service,
        "_select_room_for_candidate",
        lambda _placement, _day, _start, _end, **_kwargs: (
            "AI100",
            [],
            {"selected_room": "AI100", "policy_clean": True},
        ),
    )
    monkeypatch.setattr(repair_service, "hard_feasibility_rejections", lambda *_args, **_kwargs: [])

    prepared = repair_service._prepare_repair_candidate_rows(
        placement,
        limits={"max_candidates": 1},
        move_scope="lectures_only",
    )

    moves = prepared[0]["move_scope_payload"]["moves"]
    by_id = {int(move["placement_id"]): move for move in moves}
    assert set(by_id) == {placement.id, lecture.id}
    assert lab.id in prepared[0]["move_scope_payload"]["excluded_placement_ids"]
    assert by_id[lecture.id]["day"] == "MON"
    assert prepared[0]["generation"]["move_scope"]["scope"] == "lectures_only"


def test_candidate_section_meetings_use_scoped_move_set() -> None:
    _scenario, placement = _fixture()
    lab = SectionPlacement.objects.create(
        board=placement.board,
        term_section=placement.term_section,
        day="WED",
        start_time="13:00",
        end_time="14:40",
        room="AI103",
    )
    candidate = {
        "day": "TUE",
        "start_time": "09:00",
        "end_time": "10:15",
        "room": "AI100",
        "metrics": {
            "move_scope": {
                "scope": "all_sessions",
                "moves": [
                    {
                        "placement_id": placement.id,
                        "day": "TUE",
                        "start": "09:00",
                        "end": "10:15",
                        "room": "AI100",
                    },
                    {
                        "placement_id": lab.id,
                        "day": "THU",
                        "start": "13:00",
                        "end": "14:40",
                        "room": "AI103",
                    },
                ],
            }
        },
    }

    meetings = repair_service._candidate_section_meetings(
        placement.board.scenario_id,
        placement,
        candidate,
        section_ids={placement.term_section_id},
    )

    scoped = {
        (row["day"], row["start_time"], row["end_time"])
        for row in meetings[placement.term_section_id]
    }
    assert ("TUE", "09:00", "10:15") in scoped
    assert ("THU", "13:00", "14:40") in scoped
    assert ("WED", "13:00", "14:40") not in scoped


def test_repair_candidate_selection_prefers_actual_unresolved_student_improvement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _fixture()
    rows = [
        {
            "rank": 1,
            "kind": "lect",
            "day": "SUN",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 0,
            "impact_score": 0,
            "badge": "Clean",
            "evidence": [],
        },
        {
            "rank": 2,
            "kind": "lect",
            "day": "TUE",
            "start": "09:00",
            "end": "10:15",
            "critical_count": 0,
            "warning_count": 0,
            "student_affected_count": 4,
            "impact_score": 4,
            "badge": "Mixed",
            "evidence": [],
        },
    ]

    monkeypatch.setattr(
        repair_service,
        "preview_placement_slot_candidates",
        lambda _placement_id: {"candidates": rows},
    )
    monkeypatch.setattr(
        repair_service,
        "_select_room_for_candidate",
        lambda _placement, _day, _start, _end, **_kwargs: (
            "AI100",
            [],
            {"selected_room": "AI100", "policy_clean": True},
        ),
    )
    monkeypatch.setattr(repair_service, "hard_feasibility_rejections", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        repair_service,
        "_student_outcome_rows",
        lambda *_args, **_kwargs: {
            ("SUN", "09:00"): {
                "student_outcome": {
                    "blocked_students_delta": 0,
                    "unresolved_course_delta": 0,
                    "newly_unblocked_student_count": 0,
                    "improved_student_count": 0,
                    "newly_blocked_student_count": 0,
                    "worsened_student_count": 0,
                    "actual_clash_delta": 0,
                },
            },
            ("TUE", "09:00"): {
                "student_outcome": {
                    "blocked_students_delta": -2,
                    "unresolved_course_delta": -2,
                    "newly_unblocked_student_count": 2,
                    "improved_student_count": 2,
                    "newly_blocked_student_count": 0,
                    "worsened_student_count": 0,
                    "actual_clash_delta": 0,
                },
            },
        },
    )

    def fake_solve(_run, candidate, _placement, *, component, limits):
        metrics = dict(candidate.get("metrics") or {})
        metrics["exact_repair"] = {
            "enabled": True,
            "solver_status": "optimal",
            "existing_lost": 0,
            "blocked_recovered": 1,
            "unresolved_blocked": 0,
            "requested_courses_recovered": 1,
            "students_moved": 0,
            "section_changes": 1,
        }
        candidate["metrics"] = metrics
        candidate["solver_status"] = "optimal"
        candidate["status"] = TimetableRepairCandidate.STATUS_FEASIBLE
        return metrics

    monkeypatch.setattr(repair_service, "solve_conservative_student_repair", fake_solve)

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(run_id=detail["run"]["id"])
    assert candidate.day == "TUE"
    assert candidate.metrics_json["generation"]["source_rank"] == 2
    assert candidate.metrics_json["generation"]["student_outcome_preselection"] == {
        "enabled": True,
        "selection_rank": 1,
        "candidate_budget": 1,
        "strategy": "actual_unresolved_students_first",
        "outcome_available": True,
    }
    assert detail["summary"]["candidate_evaluation"]["selected_candidate_count"] == 1


def test_repair_solver_respects_capacity_and_prefers_no_disruption() -> None:
    _scenario, placement = _fixture()
    placement.term_section.available_capacity = 1
    placement.term_section.registered_count = 1
    placement.term_section.save(update_fields=["available_capacity", "registered_count"])
    alt = TermSection.objects.get(
        scenario=placement.board.scenario,
        course_key="AI101",
        section="S2",
    )
    alt.available_capacity = 1
    alt.registered_count = 0
    alt.save(update_fields=["available_capacity", "registered_count"])

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    candidate = run.candidates.get(candidate_id="cand_001")
    exact = candidate.metrics_json["exact_repair"]
    assert exact["existing_lost"] == 0
    assert exact["blocked_recovered"] == 1
    assert exact["students_moved"] == 0
    assert not TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        change_type=TimetableRepairStudentChange.CHANGE_MOVED,
    ).exists()
    assert TimetableRepairStudentChange.objects.get(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
    ).after_section_id == str(alt.id)


def test_repair_solver_uses_min_cost_flow_for_simple_one_course_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement, alt = _single_course_flow_fixture()
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "room_candidate_count": 1,
                    "selected_room": "AI100",
                    "source_badge": "Clean",
                    "source_critical_count": 0,
                    "source_warning_count": 0,
                    "source_student_affected_count": 0,
                    "source_impact_score": 0,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[3002],
        limits={"max_candidates": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    candidate = run.candidates.get(candidate_id="cand_001")
    exact = candidate.metrics_json["exact_repair"]
    assert candidate.status == TimetableRepairCandidate.STATUS_FEASIBLE
    assert exact["solver_strategy"] == "min_cost_flow"
    assert exact["min_cost_flow"]["used"] is True
    assert exact["blocked_recovered"] == 1
    assert exact["existing_lost"] == 0
    assert exact["students_moved"] == 0
    assert TimetableRepairStudentChange.objects.get(
        candidate=candidate,
        student_id=3002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
    ).after_section_id == str(alt.id)


def test_repair_solver_uses_lns_when_full_component_exceeds_variable_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _fixture()
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "room_candidate_count": 1,
                    "selected_room": "AI100",
                    "source_badge": "Clean",
                    "source_critical_count": 0,
                    "source_warning_count": 0,
                    "source_student_affected_count": 0,
                    "source_impact_score": 0,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1, "max_variables": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    candidate = run.candidates.get(candidate_id="cand_001")
    exact = candidate.metrics_json["exact_repair"]
    assert candidate.status == TimetableRepairCandidate.STATUS_FEASIBLE
    assert exact["solver_strategy"] == "large_neighbourhood_cp_sat"
    assert exact["large_neighbourhood"]["used"] is True
    assert exact["warm_start"]["strategy"] == "current_assignment_cp_sat_hint"
    assert exact["warm_start"]["used"] is True
    assert exact["large_neighbourhood"]["neighbourhood_reason"]
    assert exact["large_neighbourhood"]["neighbourhood_count"] >= 1
    assert exact["large_neighbourhood"]["strategy"] == "adaptive_large_neighbourhood_cp_sat"
    assert exact["large_neighbourhood"]["adaptive"]["enabled"] is True
    assert exact["large_neighbourhood"]["adaptive"]["strategy"] == (
        "adaptive_family_weighted_neighbourhood_ordering"
    )
    assert exact["large_neighbourhood"]["adaptive"]["reward_policy"]["primary"] == (
        "minimize_unresolved_students"
    )
    assert exact["large_neighbourhood"]["adaptive"]["family_weights"]
    assert exact["large_neighbourhood"]["adaptive"]["learned_neighbourhoods"]
    assert exact["large_neighbourhood"]["iteration_count"] >= 1
    attempts = exact["large_neighbourhood"]["attempts"]
    assert attempts
    assert {row["name"] for row in attempts}
    assert all(row.get("reason") for row in attempts)
    assert all("relaxed_student_count" in row for row in attempts)
    assert all("adaptive_family" in row for row in attempts)
    assert all("adaptive_weight_before" in row for row in attempts)
    assert all("adaptive_weight_after" in row for row in attempts)
    assert all("adaptive_family_weight_before" in row for row in attempts)
    assert all("adaptive_family_weight_after" in row for row in attempts)
    assert all("adaptive_reward" in row for row in attempts)
    assert exact["large_neighbourhood"]["neighbourhood"] in {row["name"] for row in attempts}
    assert exact["large_neighbourhood"]["unresolved_blocked"] == 0
    assert exact["blocked_recovered"] == 1
    assert exact["existing_lost"] == 0


def test_adaptive_lns_weights_learn_by_neighbourhood_family() -> None:
    first = {
        "name": "small_direct",
        "adaptive_family": "target_course",
        "origin": "base",
        "student_ids": [1],
    }
    second = {
        "name": "larger_capacity",
        "adaptive_family": "capacity_pressure",
        "origin": "base",
        "student_ids": [1, 2, 3],
    }
    stats = {
        "small_direct": repair_service._initial_lns_neighbourhood_stat(first),
        "larger_capacity": repair_service._initial_lns_neighbourhood_stat(second),
    }
    family_stats = {
        "target_course": repair_service._initial_lns_family_stat("target_course"),
        "capacity_pressure": repair_service._initial_lns_family_stat("capacity_pressure"),
    }

    reward = repair_service._adaptive_lns_reward(
        attempt={
            "blocked_recovered": 3,
            "unresolved_blocked": 0,
            "requested_courses_recovered": 3,
            "students_moved": 1,
            "section_changes": 1,
            "status": "optimal",
        },
        score=(3, 0, 3, -1, -1, 0, -10),
        improved=True,
    )
    repair_service._update_lns_neighbourhood_stat(
        stats["larger_capacity"],
        family_stats["capacity_pressure"],
        attempt={"status": "optimal"},
        score=(3, 0, 3, -1, -1, 0, -10),
        improved=True,
        reward=reward,
    )

    ordered = sorted(
        [first, second],
        key=lambda spec: repair_service._adaptive_lns_spec_sort_key(
            spec,
            stats=stats,
            family_stats=family_stats,
            best_score=(3, 0, 3, -1, -1, 0, -10),
        ),
    )
    assert ordered[0]["name"] == "larger_capacity"
    assert family_stats["capacity_pressure"]["weight"] > family_stats["target_course"]["weight"]
    assert family_stats["capacity_pressure"]["average_reward"] > 0


def test_lns_neighbourhood_builder_includes_maturity_frontiers() -> None:
    specs = repair_service._build_lns_neighbourhood_specs(
        student_ids=[1, 2, 3, 4, 5],
        student_set={1, 2, 3, 4, 5},
        blocked_new=[1],
        current_by_student_course={
            2: {"AI101": 10, "DS201": 20},
            3: {"AI101": 11},
            4: {"CS301": 30},
            5: {"DS201": 21, "CS301": 31},
        },
        option_ids_by_student_course={
            (1, "AI101"): [10, 11],
            (2, "AI101"): [10, 11],
            (3, "AI101"): [10, 11],
        },
        target_course="AI101",
        section_meetings={
            10: [{"day": "SUN", "start_time": "09:00", "end_time": "10:00"}],
            11: [{"day": "MON", "start_time": "09:00", "end_time": "10:00"}],
            30: [{"day": "SUN", "start_time": "09:30", "end_time": "10:30"}],
        },
        capacity_by_section={10: 1, 11: 40},
        fixed_occupancy_by_section={},
        max_lns_students=5,
    )

    by_name = {spec["name"]: spec for spec in specs}
    assert {"target_course_direct", "capacity_pressure", "target_conflict_frontier"}.issubset(
        by_name
    )
    assert {"multi_course_frontier", "whole_component_capped", "blocked_only"}.issubset(by_name)
    assert by_name["capacity_pressure"]["student_ids"] == [1, 2]
    assert by_name["target_conflict_frontier"]["student_ids"] == [1, 4]
    assert all(spec["reason"] for spec in specs)


def test_repair_solver_supports_multi_course_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scenario, placement = _cascade_fixture()
    monkeypatch.setattr(
        repair_service,
        "_prepare_repair_candidate_rows",
        lambda _placement, *, limits, **_kwargs: [
            {
                "source_row": {
                    "rank": 1,
                    "kind": "lect",
                    "day": "THU",
                    "start": "13:00",
                    "end": "14:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "student_affected_count": 0,
                    "impact_score": 0,
                    "badge": "Clean",
                    "evidence": [],
                },
                "source_index": 1,
                "day": "THU",
                "start": "13:00",
                "end": "14:15",
                "selected_room": "AI100",
                "room_payload": {
                    "selected_room": "AI100",
                    "policy_clean": True,
                    "is_online": False,
                },
                "rejections": [],
                "generation": {
                    "source_rank": 1,
                    "source_candidate_count": 1,
                    "scan_limit": 1,
                    "hard_rejection_count": 0,
                    "room_policy_clean": True,
                },
            }
        ],
    )
    monkeypatch.setattr(repair_service, "_student_outcome_rows", lambda *_args, **_kwargs: {})

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["existing_lost"] == 0
    assert exact["blocked_recovered"] == 1
    assert exact["students_moved"] == 1
    assert exact["existing_section_changes"] == 2
    assert exact["cascade"]["requires_multi_course_cascade"] is True
    assert exact["cascade"]["multi_course_student_count"] == 1
    assert exact["cascade"]["multi_course_student_ids"] == [1001]
    assert exact["cascade"]["max_changed_courses_per_student"] == 2
    assert exact["cascade"]["touched_courses"] == ["AI101", "DS201"]

    assert TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1001,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_MOVED,
    ).exists()
    assert TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1001,
        course_key="DS201",
        change_type=TimetableRepairStudentChange.CHANGE_MOVED,
    ).exists()
    assert TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
    ).exists()


def test_repair_solver_blocks_gender_ineligible_new_registration() -> None:
    _scenario, placement = _fixture()
    placement.term_section.section = "M1"
    placement.term_section.save(update_fields=["section"])
    alt = TermSection.objects.get(
        scenario=placement.board.scenario,
        course_key="AI101",
        section="S2",
    )
    alt.section = "M2"
    alt.save(update_fields=["section"])
    Student.objects.filter(student_id=1002).update(section="F")

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["blocked_recovered"] == 0
    assert exact["unresolved_blocked"] == 1
    assert exact["eligibility_policy"]["rejection_counts"]["SECTION_GENDER_MISMATCH"] >= 2
    assert exact["unresolved_diagnostics"]["reason_counts"]["NO_ELIGIBLE_TARGET_SECTION"] == 1
    unresolved = TimetableRepairStudentChange.objects.get(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_UNRESOLVED,
    )
    assert (
        unresolved.details_json["unresolved_reason"]["ineligible_summary"][
            "SECTION_GENDER_MISMATCH"
        ]
        >= 2
    )
    assert TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_UNRESOLVED,
    ).exists()
    assert not TimetableRepairStudentChange.objects.filter(
        candidate=candidate,
        student_id=1002,
        change_type=TimetableRepairStudentChange.CHANGE_NEWLY_REGISTERED,
    ).exists()


def test_repair_solver_blocks_program_ineligible_new_registration() -> None:
    _scenario, placement = _fixture()
    Student.objects.filter(student_id=1002).update(program="DS")

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["blocked_recovered"] == 0
    assert exact["unresolved_blocked"] == 1
    assert exact["eligibility_policy"]["rejection_counts"]["PROGRAM_MISMATCH"] >= 2
    assert exact["unresolved_diagnostics"]["reason_counts"]["NO_ELIGIBLE_TARGET_SECTION"] == 1


def test_repair_solver_blocks_missing_prerequisites_for_new_registration() -> None:
    _scenario, placement = _fixture()
    Prerequisite.objects.create(
        program="AI",
        course_code="AI101",
        prerequisite_course_code="AI099",
    )

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["blocked_recovered"] == 0
    assert exact["unresolved_blocked"] == 1
    assert exact["eligibility_policy"]["rejection_counts"]["MISSING_PREREQUISITES"] >= 2
    unresolved = TimetableRepairStudentChange.objects.get(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_UNRESOLVED,
    )
    assert unresolved.details_json["unresolved_reason"]["code"] == "NO_ELIGIBLE_TARGET_SECTION"
    assert (
        unresolved.details_json["unresolved_reason"]["ineligible_summary"]["MISSING_PREREQUISITES"]
        >= 2
    )


def test_repair_solver_blocks_protected_students_from_new_registration() -> None:
    _scenario, placement = _fixture()
    Student.objects.filter(student_id=1002).update(status="manual approval")

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["blocked_recovered"] == 0
    assert exact["unresolved_blocked"] == 1
    assert exact["eligibility_policy"]["rejection_counts"]["PROTECTED_STUDENT"] >= 2
    assert exact["unresolved_diagnostics"]["reason_counts"]["NO_ELIGIBLE_TARGET_SECTION"] == 1


def test_repair_solver_explains_unresolved_capacity_block() -> None:
    _scenario, placement = _fixture()
    placement.term_section.available_capacity = 1
    placement.term_section.registered_count = 1
    placement.term_section.save(update_fields=["available_capacity", "registered_count"])
    alt = TermSection.objects.get(
        scenario=placement.board.scenario,
        course_key="AI101",
        section="S2",
    )
    alt.available_capacity = 0
    alt.registered_count = 0
    alt.save(update_fields=["available_capacity", "registered_count"])

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    candidate = TimetableRepairCandidate.objects.get(
        run_id=detail["run"]["id"],
        candidate_id="cand_001",
    )
    exact = candidate.metrics_json["exact_repair"]
    assert exact["blocked_recovered"] == 0
    assert exact["unresolved_blocked"] == 1
    assert exact["unresolved_diagnostics"]["reason_counts"]["NO_CAPACITY_AFTER_REPAIR"] == 1
    unresolved = TimetableRepairStudentChange.objects.get(
        candidate=candidate,
        student_id=1002,
        course_key="AI101",
        change_type=TimetableRepairStudentChange.CHANGE_UNRESOLVED,
    )
    assert unresolved.details_json["unresolved_reason"]["code"] == "NO_CAPACITY_AFTER_REPAIR"


def test_repair_solver_uses_evaluator_baseline_without_exact_assignment_source() -> None:
    _scenario, placement = _fixture()
    StudentTermSection.objects.filter(term_section__scenario=placement.board.scenario).delete()

    detail = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )

    run = TimetableRepairRun.objects.get(id=detail["run"]["id"])
    candidate = run.candidates.get(candidate_id="cand_001")
    assert detail["summary"]["assignment_snapshot_available"] is False
    assert candidate.status == TimetableRepairCandidate.STATUS_FEASIBLE
    assert candidate.solver_status in {"optimal", "feasible"}
    exact = candidate.metrics_json["exact_repair"]
    solver_domain = exact["solver_domain"]
    assert solver_domain["exact_assignment_source_available"] is False
    assert solver_domain["assignment_source"] == "current_evaluator_assignment"
    assert solver_domain["assignment_source_summary"]["scope"] == (
        "whole_scenario_then_bounded_repair_slice"
    )
    assert solver_domain["assignment_source_summary"]["global_student_count"] >= 2
    assert solver_domain["counts"]["current_assignments"] >= 1
    assert candidate.student_changes.exists()


def test_repair_approve_apply_and_rollback_restore_state() -> None:
    _scenario, placement = _fixture()
    before_slot = {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room,
    }
    created = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    run_id = created["run"]["id"]
    candidate_id = created["candidates"][0]["candidate_id"]

    approved = approve_repair_candidate(run_id, candidate_id)

    assert approved["apply_enabled"] is True
    candidate = TimetableRepairCandidate.objects.get(
        run_id=run_id,
        candidate_id=candidate_id,
    )

    applied = apply_approved_repair_candidate(run_id, candidate_id)

    placement.refresh_from_db()
    assert {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room,
    } == {
        "day": candidate.day,
        "start_time": candidate.start_time,
        "end_time": candidate.end_time,
        "room": candidate.room,
    }
    assert StudentTermSection.objects.filter(
        student_id=1002,
        term_section__scenario=placement.board.scenario,
        term_section__course_key="AI101",
        source__startswith="timetable_repair:",
    ).exists()
    assert applied["summary"]["application"]["status"] == "applied"
    assert applied["rollback_preflight"]["status"] == "ready"
    assert applied["rollback_preflight"]["rollback_ready"] is True
    assert applied["rollback_preflight"]["candidate_id"] == candidate_id
    assert any(row["event"] == "repair_candidate_applied" for row in applied["audit_timeline"])
    assert TimetableRepairApproval.objects.filter(
        run_id=run_id,
        candidate=candidate,
        status=TimetableRepairApproval.STATUS_APPLIED,
    ).exists()

    rolled_back = rollback_repair_run(run_id)

    placement.refresh_from_db()
    assert {
        "day": placement.day,
        "start_time": placement.start_time,
        "end_time": placement.end_time,
        "room": placement.room,
    } == before_slot
    assert not StudentTermSection.objects.filter(
        student_id=1002,
        term_section__scenario=placement.board.scenario,
        term_section__course_key="AI101",
    ).exists()
    assert rolled_back["summary"]["rollback"]["status"] == "rolled_back"
    assert any(
        row["event"] == "repair_candidate_rolled_back" for row in rolled_back["audit_timeline"]
    )
    assert TimetableRepairApproval.objects.filter(
        run_id=run_id,
        candidate=candidate,
        status=TimetableRepairApproval.STATUS_ROLLED_BACK,
    ).exists()


def test_repair_rollback_readiness_detects_modified_repair_owned_assignment() -> None:
    _scenario, placement = _fixture()
    created = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    run_id = created["run"]["id"]
    candidate_id = created["candidates"][0]["candidate_id"]
    approve_repair_candidate(run_id, candidate_id)
    apply_approved_repair_candidate(run_id, candidate_id)

    row = StudentTermSection.objects.get(
        student_id=1002,
        term_section__scenario=placement.board.scenario,
        term_section__course_key="AI101",
    )
    row.source = "manual"
    row.save(update_fields=["source", "updated_at"])

    detail = repair_run_detail(run_id)
    readiness = detail["rollback_preflight"]
    assert readiness["status"] == "blocked"
    assert readiness["rollback_ready"] is False
    assert any(
        row["code"] == "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT"
        for row in readiness["blocking_reasons"]
    )

    with pytest.raises(TimetableRepairOperationError) as exc:
        rollback_repair_run(run_id)

    assert exc.value.code == "REPAIR_ROLLBACK_STALE_STUDENT_ASSIGNMENT"


def test_repair_approve_revalidates_current_student_state() -> None:
    _scenario, placement = _fixture()
    created = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    run_id = created["run"]["id"]
    candidate_id = created["candidates"][0]["candidate_id"]

    StudentTermSection.objects.filter(
        student_id=1001,
        term_section=placement.term_section,
    ).delete()

    stale_detail = repair_run_detail(run_id)
    stale_candidate = stale_detail["candidates"][0]
    assert stale_candidate["preflight"]["status"] == "stale"
    assert stale_candidate["preflight"]["approve_ready"] is False
    assert any(
        row["code"] == "REPAIR_STALE_STUDENT_ASSIGNMENT"
        for row in stale_candidate["preflight"]["blocking_reasons"]
    )

    with pytest.raises(TimetableRepairOperationError) as exc:
        approve_repair_candidate(run_id, candidate_id)

    assert exc.value.code == "REPAIR_STALE_STUDENT_ASSIGNMENT"
    assert not TimetableRepairApproval.objects.filter(
        run_id=run_id,
        status=TimetableRepairApproval.STATUS_APPROVED,
    ).exists()


def test_repair_run_detail_round_trips() -> None:
    _scenario, placement = _fixture()
    created = analyse_timetable_repair(placement_id=placement.id, limits={"max_candidates": 1})

    detail = repair_run_detail(created["run"]["id"])

    assert detail["api_contract"]["version"] == "repair-api-contract-v1"
    assert detail["api_contract"]["student_identifier_policy"] == "student_ids_only"
    assert detail["api_contract"]["endpoints"]["run_detail"].endswith(
        f"/ops/tw/repair/runs/{created['run']['id']}/"
    )
    assert detail["run"]["id"] == created["run"]["id"]
    assert detail["run_freshness"]["status"] == "fresh"
    assert detail["run_freshness"]["recommendation_current"] is True
    assert detail["run_freshness"]["fingerprint_matches_analysis"] is True
    assert detail["summary"]["blocked_demand"]["source_counts"]["scenario_course_demand"] == 1
    assert len(detail["candidates"]) == 1
    assert (
        detail["candidates"][0]["admin_summary"]["candidate_id"]
        == detail["candidates"][0]["candidate_id"]
    )
    assert detail["candidates"][0]["admin_summary"]["metrics"]["solver_strategy"]
    assert {snap["kind"] for snap in detail["snapshots"]} == {"before", "component"}
    assert any(row["event"] == "repair_analysis_started" for row in detail["audit_timeline"])
    assert any(row["event"] == "repair_analysis_completed" for row in detail["audit_timeline"])
    assert any(row["message"] == "repair_analysis_completed" for row in detail["audit_logs"])


def test_repair_run_report_is_admin_evidence_package() -> None:
    _scenario, placement = _fixture()
    created = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    candidate_id = created["candidates"][0]["candidate_id"]

    report = repair_run_report(created["run"]["id"], candidate_id=candidate_id)

    assert report["report_version"] == "repair-report-v1"
    assert report["api_contract"]["version"] == "repair-api-contract-v1"
    assert report["api_contract"]["endpoints"]["candidate_detail"].endswith(
        f"/ops/tw/repair/runs/{created['run']['id']}/candidates/{candidate_id}/"
    )
    assert report["api_contract"]["endpoints"]["candidate_apply"].endswith(
        f"/ops/tw/repair/runs/{created['run']['id']}/candidates/{candidate_id}/apply/"
    )
    assert report["scope"]["student_identifier_policy"] == "student_ids_only"
    assert report["scope"]["candidate_id"] == candidate_id
    assert report["run"]["versions"]["solver"] == "repair-solver-cpsat-flow-adaptive-lns-v3"
    assert report["run_freshness"]["status"] == "fresh"
    assert report["safety"]["run_freshness"]["recommendation_current"] is True
    assert report["request"]["blocked_student_ids"] == [1002]
    assert report["summary"]["blocked_demand"]["active_request_count"] == 1
    assert (
        report["summary"]["candidate_evaluation"]["mode"] == "in_memory_then_audited_bulk_persist"
    )
    assert report["summary"]["candidate_evaluation"]["best_candidate_reason"]
    assert report["selected_candidate"]["candidate_id"] == candidate_id
    assert report["selected_candidate"]["metrics"]["solver_strategy"]
    assert report["selected_candidate"]["decision"]["approve_allowed"] is True
    assert (
        report["selected_candidate"]["decision"]["preflight"][
            "approval_runs_current_state_validation"
        ]
        is True
    )
    assert report["selected_candidate"]["preflight"]["status"] == "fresh"
    assert report["selected_candidate"]["preflight"]["approve_ready"] is True
    assert report["selected_candidate"]["preflight"]["current_state_valid"] is True
    assert report["selected_candidate"]["ranking"]["strategy"] == "exact_repair_lexicographic"
    assert report["selected_candidate"]["evaluation"]["solver_invoked"] is True
    assert any(row["event"] == "repair_analysis_completed" for row in report["audit"]["timeline"])
    assert report["audit"]["logs_truncated"] is False
    assert report["student_changes"]
    assert all(
        "student_id" in row and "student_name" not in row for row in report["student_changes"]
    )
    assert {row["kind"] for row in report["snapshot_inventory"]} == {"before", "component"}


def test_repair_candidate_detail_is_direct_evidence_package() -> None:
    _scenario, placement = _fixture()
    created = analyse_timetable_repair(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
    )
    candidate_id = created["candidates"][0]["candidate_id"]

    detail = repair_candidate_detail(created["run"]["id"], candidate_id)

    assert detail["candidate_detail_version"] == "repair-candidate-detail-v1"
    assert detail["api_contract"]["version"] == "repair-api-contract-v1"
    assert detail["api_contract"]["endpoints"]["candidate_approve"].endswith(
        f"/ops/tw/repair/runs/{created['run']['id']}/candidates/{candidate_id}/approve/"
    )
    assert detail["scope"]["candidate_id"] == candidate_id
    assert detail["scope"]["student_identifier_policy"] == "student_ids_only"
    assert detail["run"]["id"] == created["run"]["id"]
    assert detail["run_freshness"]["status"] == "fresh"
    assert detail["candidate"]["candidate_id"] == candidate_id
    assert detail["candidate"]["admin_summary"] == detail["report_candidate"]
    assert detail["report_candidate"]["candidate_id"] == candidate_id
    assert detail["student_changes"]
    assert detail["student_changes_total"] == detail["candidate"]["student_change_count"]
    assert all(
        "student_id" in row and "student_name" not in row for row in detail["student_changes"]
    )
    assert "newly_registered" in detail["student_change_type_counts"]
    assert any(
        row["message"] == "repair_candidate_student_solver_completed"
        for row in detail["audit"]["logs"]
    )
    assert {row["kind"] for row in detail["snapshot_inventory"]} == {"before", "component"}

    with pytest.raises(TimetableRepairOperationError) as exc:
        repair_candidate_detail(created["run"]["id"], "missing-candidate")
    assert exc.value.code == "REPAIR_CANDIDATE_NOT_FOUND"
    assert exc.value.status == 404


def test_repair_analyse_endpoint_contract(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)

    response = client.post(
        "/ops/tw/repair/analyse/",
        data={
            "placement_id": placement.id,
            "blocked_student_ids": [1002],
            "blocked_requests": [
                {
                    "student_id": 1002,
                    "course_key": "AI101",
                    "status": "blocked",
                    "priority": "graduating",
                    "reason_blocked": "capacity",
                }
            ],
            "limits": {"max_candidates": 1},
        },
        content_type="application/json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    assert payload["api_contract"]["version"] == "repair-api-contract-v1"
    assert payload["summary"]["candidate_count"] == 1
    assert payload["summary"]["blocked_demand"]["priority_counts"]["graduating"] == 1
    assert payload["summary"]["blocked_demand"]["rows"][0]["reason"] == "capacity"
    run_id = payload["run"]["id"]

    detail_response = client.get(f"/ops/tw/repair/runs/{run_id}/")
    assert detail_response.status_code == 200
    assert detail_response.json()["run"]["id"] == run_id

    candidate_id = payload["candidates"][0]["candidate_id"]
    candidate_response = client.get(f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/")
    assert candidate_response.status_code == 200
    candidate_payload = candidate_response.json()
    assert candidate_payload["candidate_detail_version"] == "repair-candidate-detail-v1"
    assert candidate_payload["api_contract"]["endpoints"]["candidate_apply"].endswith(
        f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/"
    )
    assert candidate_payload["candidate"]["candidate_id"] == candidate_id
    assert candidate_payload["student_changes"]

    missing_candidate_response = client.get(
        f"/ops/tw/repair/runs/{run_id}/candidates/missing-candidate/"
    )
    assert missing_candidate_response.status_code == 404
    assert missing_candidate_response.json()["error"]["code"] == "REPAIR_CANDIDATE_NOT_FOUND"

    report_response = client.get(f"/ops/tw/repair/runs/{run_id}/report/")
    assert report_response.status_code == 200
    assert report_response.json()["report_version"] == "repair-report-v1"


def test_repair_simulation_endpoint_contract(client: Client) -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])
    _login_general(client)

    response = client.post(
        "/ops/tw/repair/simulate/",
        data={
            "scenario_id": scenario.id,
            "program": "AI",
            "nominal_term": 1,
            "max_placements": 1,
            "limits": {"max_candidates": 1},
        },
        content_type="application/json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    simulation = payload["simulation"]
    assert simulation["analysis_only"] is True
    assert simulation["governance"]["apply_allowed"] is False
    assert simulation["aggregate"]["blocked_recovered_best_sum"] == 1
    assert simulation["scope"]["selected_target_count"] == 1
    assert simulation["runs"][0]["course_key"] == "AI101"
    assert (
        simulation["best_opportunities"][0]["best_candidate"]["metrics"]["blocked_recovered"] == 1
    )


def test_repair_analysis_job_runs_as_durable_evidence_job() -> None:
    _scenario, placement = _fixture()

    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )

    assert job.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert job.kind == TimetableRepairJob.KIND_ANALYSIS
    assert job.repair_run_id
    assert job.progress_json["stage"] == "succeeded"
    assert job.result_json["job_result_version"] == "repair-job-result-v1"
    assert job.result_json["analysis"]["run"]["id"] == str(job.repair_run_id)
    assert job.result_json["analysis"]["summary"]["candidate_count"] == 1


def test_repair_analysis_job_reuses_current_completed_result_without_solver_rerun() -> None:
    _scenario, placement = _fixture()

    first = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )
    assert TimetableRepairRun.objects.count() == 1

    second = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )

    assert second.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert second.attempt_count == 0
    assert second.repair_run_id == first.repair_run_id
    assert second.result_json["analysis"]["run"]["id"] == str(first.repair_run_id)
    assert second.progress_json["stage"] == "reused_completed_result"
    assert second.progress_json["reused_from_job_id"] == str(first.id)
    assert TimetableRepairJob.objects.count() == 2
    assert TimetableRepairRun.objects.count() == 1

    serialized = serialize_repair_job(second, include_result=True)
    assert serialized["reuse"]["reused"] is True
    assert serialized["reuse"]["reused_from_job_id"] == str(first.id)
    assert serialized["result"]["analysis"]["run"]["id"] == str(first.repair_run_id)


def test_repair_analysis_job_does_not_reuse_stale_completed_result() -> None:
    _scenario, placement = _fixture()

    first = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )
    placement.room = "AI103"
    placement.save(update_fields=["room"])

    second = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )

    assert second.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert second.repair_run_id != first.repair_run_id
    assert second.progress_json["stage"] == "succeeded"
    assert not second.progress_json.get("reused_from_job_id")
    assert TimetableRepairRun.objects.count() == 2


def test_repair_simulation_job_runs_as_durable_evidence_job() -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])

    job = submit_repair_simulation_job(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )

    assert job.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert job.kind == TimetableRepairJob.KIND_SIMULATION
    assert job.repair_run_id is None
    assert job.result_json["simulation"]["analysis_only"] is True
    assert job.result_json["simulation"]["governance"]["approval_allowed"] is False
    assert job.result_json["simulation"]["aggregate"]["scanned_run_count"] == 1
    assert job.result_json["simulation"]["scope"]["selected_target_count"] == 1
    assert (
        job.result_json["simulation"]["best_opportunities"][0]["best_candidate"]["metrics"][
            "blocked_recovered"
        ]
        == 1
    )


def test_repair_simulation_job_reuses_current_completed_result_without_solver_rerun() -> None:
    scenario, _placement = _fixture()
    _make_actual_unresolved_scope(scenario, unresolved_courses=["AI101"])

    first = submit_repair_simulation_job(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
        dispatch_inline=True,
    )
    run_count = TimetableRepairRun.objects.count()

    second = submit_repair_simulation_job(
        scenario_id=scenario.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )

    assert second.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert second.attempt_count == 0
    assert second.repair_run_id is None
    assert (
        second.result_json["simulation"]["runs"][0]["run_id"]
        == first.result_json["simulation"]["runs"][0]["run_id"]
    )
    assert second.progress_json["stage"] == "reused_completed_result"
    assert second.progress_json["reused_from_job_id"] == str(first.id)
    assert TimetableRepairJob.objects.count() == 2
    assert TimetableRepairRun.objects.count() == run_count


def test_repair_job_cancel_before_worker_start() -> None:
    _scenario, placement = _fixture()
    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )

    assert cancel_repair_job(job.id) is True
    run_repair_job(job.id)
    job = get_repair_job(job.id)

    assert job is not None
    assert job.status == TimetableRepairJob.STATUS_CANCELLED
    assert job.cancel_requested is True
    assert job.progress_json["stage"] == "cancelled_before_start"
    assert not TimetableRepairRun.objects.exists()


def test_repair_worker_requeues_stale_running_job_until_attempt_cap() -> None:
    _scenario, placement = _fixture()
    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    stale_at = timezone.now() - timedelta(hours=1)
    job.status = TimetableRepairJob.STATUS_RUNNING
    job.started_at = stale_at
    job.locked_at = stale_at
    job.heartbeat_at = stale_at
    job.locked_by = "dead-worker"
    job.attempt_count = 1
    job.progress_json = {"stage": "running", "percent": 10}
    job.save(
        update_fields=[
            "status",
            "started_at",
            "locked_at",
            "heartbeat_at",
            "locked_by",
            "attempt_count",
            "progress_json",
        ]
    )

    recovered = recover_stale_repair_jobs(
        stale_after_seconds=60,
        max_attempts=3,
        worker_id="recovery-test",
    )
    job.refresh_from_db()

    assert [row.id for row in recovered] == [job.id]
    assert job.status == TimetableRepairJob.STATUS_QUEUED
    assert job.locked_by == ""
    assert job.locked_at is None
    assert job.heartbeat_at is None
    assert job.progress_json["stage"] == "requeued_after_stale_worker"
    assert job.progress_json["previous_locked_by"] == "dead-worker"
    assert serialize_repair_job(job)["recovery"]["stale_recovery_count"] == 1

    run_repair_job(job.id, worker_id="live-worker")
    job.refresh_from_db()
    assert job.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert job.attempt_count == 2


def test_repair_worker_fails_stale_running_job_at_attempt_cap() -> None:
    _scenario, placement = _fixture()
    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    stale_at = timezone.now() - timedelta(hours=1)
    job.status = TimetableRepairJob.STATUS_RUNNING
    job.started_at = stale_at
    job.heartbeat_at = stale_at
    job.locked_by = "dead-worker"
    job.attempt_count = 3
    job.progress_json = {"stage": "running", "percent": 10}
    job.save(
        update_fields=[
            "status",
            "started_at",
            "heartbeat_at",
            "locked_by",
            "attempt_count",
            "progress_json",
        ]
    )

    recover_stale_repair_jobs(
        stale_after_seconds=60,
        max_attempts=3,
        worker_id="recovery-test",
    )
    job.refresh_from_db()

    assert job.status == TimetableRepairJob.STATUS_FAILED
    assert job.progress_json["stage"] == "failed_stale_max_attempts"
    assert "maximum worker attempts" in job.error_message


def test_repair_worker_cancels_stale_running_job_with_cancel_requested() -> None:
    _scenario, placement = _fixture()
    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    stale_at = timezone.now() - timedelta(hours=1)
    job.status = TimetableRepairJob.STATUS_RUNNING
    job.started_at = stale_at
    job.heartbeat_at = stale_at
    job.locked_by = "dead-worker"
    job.cancel_requested = True
    job.attempt_count = 1
    job.progress_json = {"stage": "running", "percent": 10}
    job.save(
        update_fields=[
            "status",
            "started_at",
            "heartbeat_at",
            "locked_by",
            "cancel_requested",
            "attempt_count",
            "progress_json",
        ]
    )

    recover_stale_repair_jobs(stale_after_seconds=60, worker_id="recovery-test")
    job.refresh_from_db()

    assert job.status == TimetableRepairJob.STATUS_CANCELLED
    assert job.progress_json["stage"] == "cancelled_after_stale_worker"
    assert job.finished_at is not None


def test_repair_job_retry_creates_fresh_audit_job() -> None:
    _scenario, placement = _fixture()
    failed = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    failed.status = TimetableRepairJob.STATUS_FAILED
    failed.attempt_count = 1
    failed.finished_at = timezone.now()
    failed.error_message = "synthetic failure"
    failed.progress_json = {"stage": "failed", "percent": 100}
    failed.save(
        update_fields=["status", "attempt_count", "finished_at", "error_message", "progress_json"]
    )

    retry = retry_repair_job(failed.id, max_attempts=3, dispatch_inline=False)

    assert retry is not None
    assert retry.id != failed.id
    assert retry.status == TimetableRepairJob.STATUS_QUEUED
    assert retry.attempt_count == 0
    assert retry.progress_json["stage"] == "queued_retry"
    assert retry.progress_json["retry_of_job_id"] == str(failed.id)
    assert serialize_repair_job(retry)["recovery"]["retry_of_job_id"] == str(failed.id)

    run_repair_job(retry.id)
    retry.refresh_from_db()
    assert retry.status == TimetableRepairJob.STATUS_SUCCEEDED
    assert retry.progress_json["retry_of_job_id"] == str(failed.id)
    assert serialize_repair_job(retry)["recovery"]["retry_of_job_id"] == str(failed.id)


def test_repair_job_retry_endpoint_creates_fresh_job(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)
    failed = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    failed.status = TimetableRepairJob.STATUS_FAILED
    failed.attempt_count = 1
    failed.finished_at = timezone.now()
    failed.error_message = "synthetic failure"
    failed.progress_json = {"stage": "failed", "percent": 100}
    failed.save(
        update_fields=["status", "attempt_count", "finished_at", "error_message", "progress_json"]
    )

    response = client.post(
        f"/ops/tw/repair/jobs/{failed.id}/retry/",
        data={"max_attempts": 3},
        content_type="application/json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    assert payload["job"]["job_id"] != str(failed.id)
    assert payload["job"]["status"] == TimetableRepairJob.STATUS_SUCCEEDED
    assert payload["job"]["recovery"]["retry_of_job_id"] == str(failed.id)
    assert payload["job"]["api_contract"]["endpoints"]["retry"].endswith(
        f"/ops/tw/repair/jobs/{payload['job']['job_id']}/retry/"
    )
    assert TimetableRepairJob.objects.count() == 2


def test_repair_job_retry_endpoint_rejects_non_terminal_job(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)
    queued = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )

    response = client.post(
        f"/ops/tw/repair/jobs/{queued.id}/retry/",
        data={},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPAIR_JOB_CANNOT_RETRY"
    assert TimetableRepairJob.objects.count() == 1


def test_repair_job_recover_stale_endpoint_requeues_running_job(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)
    job = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        dispatch_inline=False,
    )
    stale_at = timezone.now() - timedelta(hours=1)
    job.status = TimetableRepairJob.STATUS_RUNNING
    job.started_at = stale_at
    job.locked_by = "dead-worker"
    job.locked_at = stale_at
    job.heartbeat_at = stale_at
    job.attempt_count = 1
    job.progress_json = {"stage": "running", "percent": 10}
    job.save(
        update_fields=[
            "status",
            "started_at",
            "locked_by",
            "locked_at",
            "heartbeat_at",
            "attempt_count",
            "progress_json",
        ]
    )

    response = client.post(
        "/ops/tw/repair/jobs/recover-stale/",
        data={"stale_after_seconds": 60, "max_attempts": 3, "limit": 5},
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["jobs"][0]["job_id"] == str(job.id)
    assert payload["jobs"][0]["status"] == TimetableRepairJob.STATUS_QUEUED
    assert payload["jobs"][0]["progress"]["stage"] == "requeued_after_stale_worker"
    assert payload["jobs"][0]["recovery"]["stale_recovery_count"] == 1
    assert payload["jobs"][0]["api_contract"]["endpoints"]["recover_stale"].endswith(
        "/ops/tw/repair/jobs/recover-stale/"
    )


def test_repair_job_list_endpoint_filters_recent_jobs(client: Client) -> None:
    scenario, placement = _fixture()
    user = _login_general(client)
    analysis = submit_repair_analysis_job(
        placement_id=placement.id,
        blocked_student_ids=[1002],
        limits={"max_candidates": 1},
        requested_by=user,
        dispatch_inline=False,
    )
    simulation = submit_repair_simulation_job(
        scenario_id=scenario.id,
        course_keys=["AI101"],
        requested_by=user,
        dispatch_inline=False,
    )

    response = client.get(f"/ops/tw/repair/jobs/list/?scenario_id={scenario.id}&limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["api_contract"]["endpoints"]["list"] == "/ops/tw/repair/jobs/list/"
    assert payload["filters"]["scenario_id"] == scenario.id
    assert payload["filters"]["limit"] == 10
    assert payload["count"] == 2
    assert payload["has_more"] is False
    assert {job["job_id"] for job in payload["jobs"]} == {str(analysis.id), str(simulation.id)}

    kind_response = client.get(
        f"/ops/tw/repair/jobs/list/?scenario_id={scenario.id}&kind=repair_analysis&mine=1"
    )
    assert kind_response.status_code == 200
    kind_payload = kind_response.json()
    assert kind_payload["count"] == 1
    assert kind_payload["jobs"][0]["job_id"] == str(analysis.id)
    assert kind_payload["filters"]["submitted_by_id"] == user.id


def test_repair_job_list_endpoint_validates_filters(client: Client) -> None:
    _login_general(client)

    response = client.get("/ops/tw/repair/jobs/list/?status=unknown")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_REPAIR_JOB_STATUS"


def test_repair_job_endpoints_submit_poll_result_and_cancel(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)

    response = client.post(
        "/ops/tw/repair/jobs/",
        data={
            "kind": "repair_analysis",
            "placement_id": placement.id,
            "blocked_student_ids": [1002],
            "limits": {"max_candidates": 1},
        },
        content_type="application/json",
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    job_id = payload["job"]["job_id"]
    assert payload["job"]["status"] == TimetableRepairJob.STATUS_SUCCEEDED
    assert payload["job"]["api_contract"]["version"] == "repair-job-api-contract-v1"
    assert payload["job"]["api_contract"]["endpoints"]["result"].endswith(
        f"/ops/tw/repair/jobs/{job_id}/result/"
    )
    assert "result" not in payload["job"]

    poll_response = client.get(f"/ops/tw/repair/jobs/{job_id}/")
    assert poll_response.status_code == 200
    poll_payload = poll_response.json()
    assert poll_payload["job"]["status"] == TimetableRepairJob.STATUS_SUCCEEDED
    assert "result" not in poll_payload["job"]

    result_response = client.get(f"/ops/tw/repair/jobs/{job_id}/result/")
    assert result_response.status_code == 200
    result_payload = result_response.json()
    assert result_payload["job"]["api_contract"]["endpoints"]["poll"].endswith(
        f"/ops/tw/repair/jobs/{job_id}/"
    )
    assert result_payload["job"]["result"]["analysis"]["run"]["id"]
    assert (
        result_payload["job"]["result"]["analysis"]["api_contract"]["version"]
        == "repair-api-contract-v1"
    )

    cancel_response = client.post(
        f"/ops/tw/repair/jobs/{job_id}/cancel/",
        data={},
        content_type="application/json",
    )
    assert cancel_response.status_code == 404
    assert cancel_response.json()["error"]["code"] == "REPAIR_JOB_CANNOT_CANCEL"


def test_repair_apply_endpoints_require_approval_then_rollback(client: Client) -> None:
    _scenario, placement = _fixture()
    _login_general(client)
    analyse_response = client.post(
        "/ops/tw/repair/analyse/",
        data={
            "placement_id": placement.id,
            "blocked_student_ids": [1002],
            "limits": {"max_candidates": 1},
        },
        content_type="application/json",
    )
    payload = analyse_response.json()
    run_id = payload["run"]["id"]
    candidate_id = payload["candidates"][0]["candidate_id"]

    premature_apply = client.post(
        f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/",
        data={},
        content_type="application/json",
    )
    assert premature_apply.status_code == 409
    assert premature_apply.json()["error"]["code"] == "REPAIR_NOT_APPROVED"

    approve_response = client.post(
        f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/approve/",
        data={"notes": "test approval"},
        content_type="application/json",
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["apply_enabled"] is True
    approved_candidate = approve_response.json()["candidates"][0]
    assert approved_candidate["decision"]["approval_status"] == "approved"
    assert approved_candidate["decision"]["apply_allowed"] is True
    assert approved_candidate["preflight"]["apply_ready"] is True

    apply_response = client.post(
        f"/ops/tw/repair/runs/{run_id}/candidates/{candidate_id}/apply/",
        data={},
        content_type="application/json",
    )
    assert apply_response.status_code == 200
    assert apply_response.json()["summary"]["application"]["status"] == "applied"
    assert apply_response.json()["rollback_preflight"]["rollback_ready"] is True

    rollback_response = client.post(
        f"/ops/tw/repair/runs/{run_id}/rollback/",
        data={},
        content_type="application/json",
    )
    assert rollback_response.status_code == 200
    assert rollback_response.json()["summary"]["rollback"]["status"] == "rolled_back"
