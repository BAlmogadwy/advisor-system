import json
import uuid

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_repair import (
    _hard_feasibility_rejections_for_move_set,
    _placements_for_move_scope,
)
from core.services.timetable_section_policy import (
    SectionPolicyError,
    create_policy_section_bundle,
    placement_snapshot,
    remove_policy_section_bundle,
    switch_policy_section_bundle,
    sync_policy_meeting_changes,
)
from core.services.timetable_workspace import (
    check_publish_readiness,
    compute_scenario_safety_summary,
    create_planned_section_placements,
    preview_placement_room_candidates,
    preview_placement_slot_candidates,
)

pytestmark = pytest.mark.django_db
AM = "ENG_INTENSIVE_V1_AM"
PM = "ENG_INTENSIVE_V1_PM"


@pytest.fixture
def context():
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="English", nominal_term=1, program="AI"
    )
    section = TermSection.objects.create(
        scenario=scenario, course_code="ENG101", course_key="ENG101", section="M1"
    )
    return scenario, board, section


def bundle(context):
    _, board, section = context
    return create_policy_section_bundle(
        board, section, day="SUN", start_time="09:00", end_time="12:00"
    )


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="english-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def post(http, path, data):
    return http.post(path, json.dumps(data), content_type="application/json")


def test_anchor_create_expands_complete_lecture_and_partial_create_is_atomic(context, http):
    _, board, section = context
    path = "/ops/tw/placements/create/"
    base = {"board_id": board.id, "term_section_id": section.id}
    response = post(
        http, path, {**base, "meetings": [{"day": "SUN", "start": "09:00", "end": "12:00"}]}
    )
    assert response.status_code == 400
    assert not SectionPlacement.objects.exists()
    assert not TermSectionMeeting.objects.exists()
    response = post(http, path, {**base, "day": "SUN", "start_time": "09:00", "end_time": "12:00"})
    assert response.status_code == 201, response.content
    data = response.json()
    assert len(data["placements"]) == 4
    assert data["section_policy"]["current_pattern_id"] == AM
    assert "instructor_clash_backstop" in data
    assert data["validation"]["critical_count"] == 0
    assert TermSectionMeeting.objects.count() == 4


def test_switch_preserves_identity_rooms_locks_instructor_and_complete_history(context):
    rows = bundle(context)
    for index, row in enumerate(rows):
        row.room = f"Dedicated-{index}"
        row.save()
        TermSectionMeeting.objects.filter(term_section=row.term_section, day=row.day).update(
            instructor="Lecturer"
        )
    before = [placement_snapshot(row) for row in rows]
    result = switch_policy_section_bundle(rows[0], pattern_id=PM)
    assert {row.id for row in result["placements"]} == {row.id for row in rows}
    assert {row.start_time for row in result["placements"]} == {"13:00"}
    assert {row.room for row in result["placements"]} == {row["room"] for row in before}
    assert result["before_section_policy"]["current_pattern_id"] == AM
    assert set(TermSectionMeeting.objects.values_list("instructor", flat=True)) == {"Lecturer"}
    assert set(TermSectionMeeting.objects.values_list("start_time", flat=True)) == {"13:00"}
    switch_policy_section_bundle(result["placement"], pattern_id=AM, placements=before)
    assert set(SectionPlacement.objects.values_list("start_time", flat=True)) == {"09:00"}


def test_companion_lock_prevents_switch_remove_and_invalid_anchor_never_writes(context):
    rows = bundle(context)
    rows[-1].is_locked = True
    rows[-1].save()
    for operation in (
        lambda: switch_policy_section_bundle(rows[0], pattern_id=PM),
        lambda: remove_policy_section_bundle(rows[0]),
        lambda: switch_policy_section_bundle(
            rows[0], day="TUE", start_time="13:00", end_time="16:00", override=True
        ),
    ):
        with pytest.raises(SectionPolicyError):
            operation()
    assert SectionPlacement.objects.count() == 4
    assert set(SectionPlacement.objects.values_list("start_time", flat=True)) == {"09:00"}


def test_remove_and_undo_recreate_all_per_day_metadata(context):
    rows = bundle(context)
    for index, row in enumerate(rows):
        row.room, row.is_locked = f"Dedicated-{index}", index == 2
        row.save()
    removed = remove_policy_section_bundle(rows[0], override=True)
    assert set(removed["removed_ids"]) == {row.id for row in rows}
    assert not SectionPlacement.objects.exists()
    assert TermSectionMeeting.objects.count() == 4
    recreated = create_policy_section_bundle(
        context[1], context[2], meetings=removed["removed_placements"]
    )
    assert {(row.day, row.room, row.is_locked) for row in recreated} == {
        (row["day"], row["room"], row["is_locked"]) for row in removed["removed_placements"]
    }


def test_blocked_interval_and_mixed_planned_bundle_rejected_without_sections(context):
    scenario, board, section = context
    scenario.blocked_slots = [{"day": "MON", "start": "10:20", "end": "10:25"}]
    scenario.save()
    with pytest.raises(SectionPolicyError):
        bundle(context)
    meetings = [
        {"day": day, "start": "09:00", "end": "12:00"} for day in ("SUN", "MON", "WED", "THU")
    ]
    meetings[-1].update(start="13:00", end="16:00")
    with pytest.raises(SectionPolicyError):
        create_planned_section_placements(
            board.id,
            course_code="ENG102",
            course_key="ENG102",
            course_name="English",
            capacity=25,
            section_label="M2",
            meetings=meetings,
        )
    assert TermSection.objects.count() == 1
    assert not SectionPlacement.objects.exists()


def test_moves_expand_bundle_and_preapproved_partial_repair_is_rejected(context, http):
    rows = bundle(context)
    scoped, excluded = _placements_for_move_scope(rows[0], "single_session")
    assert len(scoped) == 4 and not excluded
    move = {
        "placement_id": rows[0].id,
        "day": rows[0].day,
        "start": "13:00",
        "end": "16:00",
        "room": "",
    }
    reasons = _hard_feasibility_rejections_for_move_set(
        placements_by_id={rows[0].id: rows[0]}, moves=[move], planning_scope=None
    )
    assert any(row["code"] == "COURSE_SECTION_PATTERN_REQUIRED" for row in reasons)
    response = post(
        http,
        f"/ops/tw/placements/{rows[0].id}/move/",
        {
            "day": rows[0].day,
            "start_time": "13:00",
            "end_time": "16:00",
        },
    )
    assert response.status_code == 200, response.content
    assert len(response.json()["before_placements"]) == 4
    assert "instructor_clash_backstop" in response.json()
    assert set(SectionPlacement.objects.values_list("start_time", flat=True)) == {"13:00"}


def test_preview_choices_and_no_room_requirement_do_not_become_online(context):
    from core.services.timetable_scenario_quality import build_scenario_quality

    rows = bundle(context)
    preview = preview_placement_slot_candidates(rows[0].id)
    assert preview["section_policy"]["current_pattern_id"] == AM
    assert {row["pattern_id"] for row in preview["candidates"]} == {AM, PM}
    assert all(len(row["meetings"]) == 4 for row in preview["candidates"])
    rooms = preview_placement_room_candidates(rows[0].id)
    assert rooms["summary"]["room_required"] is False
    assert not rooms["summary"]["is_online"]
    summary = compute_scenario_safety_summary(context[0].id)
    assert summary["physical_unassigned_rooms"] == 0
    assert build_scenario_quality(context[0].id)["rooms"]["shortages"] == 0


def test_publish_rejects_four_rows_in_wrong_pattern(context):
    rows = bundle(context)
    rows[-1].start_time, rows[-1].end_time = "13:00", "16:00"
    rows[-1].save()
    readiness = check_publish_readiness(context[0].id)
    assert not readiness["ready"]
    assert any("English section pattern" in message for message in readiness["blockers"])


def test_repair_metadata_sync_is_reversible_without_altering_instructors(context):
    rows = bundle(context)
    before = [placement_snapshot(row) for row in rows]
    TermSectionMeeting.objects.update(instructor="Lecturer", building="English Centre")
    for row in rows:
        row.start_time, row.end_time = "13:00", "16:00"
        row.save()
    mapping = {row.id: row for row in rows}
    sync_policy_meeting_changes(mapping, before)
    changed = [placement_snapshot(row) for row in rows]
    for row in rows:
        row.start_time, row.end_time = "09:00", "12:00"
        row.save()
    sync_policy_meeting_changes(mapping, changed)
    assert set(TermSectionMeeting.objects.values_list("start_time", "instructor", "building")) == {
        ("09:00", "Lecturer", "English Centre"),
    }


def test_valid_no_room_english_can_publish_and_recreate_another_complete_pattern(context):
    rows = bundle(context)
    SectionPlacement.objects.update(room="UNASSIGNED")
    assert check_publish_readiness(context[0].id)["ready"]
    remove_policy_section_bundle(rows[0])
    TermSectionMeeting.objects.update(instructor="Lecturer")
    create_policy_section_bundle(
        context[1], context[2], day="MON", start_time="13:00", end_time="16:00"
    )
    assert TermSectionMeeting.objects.count() == 4
    assert set(TermSectionMeeting.objects.values_list("start_time", "instructor")) == {
        ("13:00", "Lecturer")
    }
    assert check_publish_readiness(context[0].id)["ready"]


def test_shared_board_copies_switch_and_remove_together(context):
    rows = bundle(context)
    other = DeliveryBoard.objects.create(
        scenario=context[0], label="Other", nominal_term=1, program="AI"
    )
    copies = create_policy_section_bundle(
        other, context[2], day="MON", start_time="09:00", end_time="12:00"
    )
    result = switch_policy_section_bundle(copies[0], pattern_id=PM)
    assert len(result["placements"]) == 8
    assert set(SectionPlacement.objects.values_list("start_time", flat=True)) == {"13:00"}
    assert check_publish_readiness(context[0].id)["ready"]
    removed = remove_policy_section_bundle(rows[0])
    assert len(removed["removed_ids"]) == 8
    assert TermSectionMeeting.objects.count() == 4


def test_switch_route_preserves_published_and_busy_guards(context, http):
    from core.models import PlannerJob

    rows = bundle(context)
    path = f"/ops/tw/placements/{rows[0].id}/switch-pattern/"
    job = PlannerJob.objects.create(
        id=uuid.uuid4(), scenario=context[0], mode="optimise_current", status="queued"
    )
    response = post(http, path, {"pattern_id": PM})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_BUSY"
    job.status = "succeeded"
    job.save()
    context[0].status = "published"
    context[0].save()
    assert post(http, path, {"pattern_id": PM}).status_code == 400
    assert set(SectionPlacement.objects.values_list("start_time", flat=True)) == {"09:00"}


def test_ordinary_course_still_creates_and_moves_single_meeting(context, http):
    context[2].course_code, context[2].course_key = "AI221", "AI221"
    context[2].save()
    response = post(
        http,
        "/ops/tw/placements/create/",
        {
            "board_id": context[1].id,
            "term_section_id": context[2].id,
            "day": "TUE",
            "start_time": "09:00",
            "end_time": "10:15",
        },
    )
    assert response.status_code == 201
    row = SectionPlacement.objects.get()
    assert (
        post(
            http,
            f"/ops/tw/placements/{row.id}/move/",
            {
                "day": "THU",
                "start_time": "13:00",
                "end_time": "14:15",
            },
        ).status_code
        == 200
    )
    assert SectionPlacement.objects.count() == 1


def test_preapproved_partial_candidate_apply_is_rejected_before_any_write(context):
    from core.models import (
        TimetableRepairApproval,
        TimetableRepairCandidate,
        TimetableRepairRun,
        TimetableRepairStudentChange,
    )
    from core.services.timetable_repair import (
        TimetableRepairOperationError,
        apply_approved_repair_candidate,
    )

    rows = bundle(context)
    before = [dict(placement_snapshot(row), placement_id=row.id) for row in rows]
    run = TimetableRepairRun.objects.create(
        scenario=context[0],
        target_placement=rows[0],
        target_section=context[2],
        before_snapshot={"placements": before},
    )
    candidate = TimetableRepairCandidate.objects.create(
        run=run,
        candidate_id="previously-approved",
        status="feasible",
        solver_status="optimal",
        day=rows[0].day,
        start_time="13:00",
        end_time="16:00",
    )
    TimetableRepairStudentChange.objects.create(
        candidate=candidate, student_id=100, course_key="ENG101", change_type="unchanged"
    )
    approval = TimetableRepairApproval.objects.create(
        run=run, candidate=candidate, status="approved"
    )
    with pytest.raises(TimetableRepairOperationError) as error:
        apply_approved_repair_candidate(run.id, candidate.candidate_id)
    assert error.value.code == "REPAIR_CANDIDATE_STALE"
    assert any(
        row["code"] == "COURSE_SECTION_PATTERN_REQUIRED"
        for row in error.value.details["rejections"]
    )
    assert [placement_snapshot(row) for row in SectionPlacement.objects.order_by("id")] == [
        placement_snapshot(row) for row in rows
    ]
    approval.refresh_from_db()
    assert approval.status == "approved"
    assert set(TermSectionMeeting.objects.values_list("start_time", flat=True)) == {"09:00"}


@pytest.mark.parametrize("same_board", [True, False])
def test_room_metadata_does_not_reserve_room_or_hide_real_instructor_clashes(
    context, same_board, settings
):
    from core.models import CourseInstructor, Instructor, Room
    from core.services.timetable_instructor_backstop import verify_persisted_scenario
    from core.services.timetable_repair import hard_feasibility_rejections
    from core.services.timetable_scenario_quality import build_scenario_quality
    from core.services.timetable_workspace import detect_board_conflicts, validate_placement

    english = bundle(context)
    SectionPlacement.objects.filter(term_section=context[2]).update(room="R1", is_locked=True)
    TermSectionMeeting.objects.filter(term_section=context[2]).update(
        room="R1", instructor="Lecturer"
    )
    Room.objects.create(
        room_code="R1", room_type="lecture", capacity=50, section="M", department="AI"
    )
    board = (
        context[1]
        if same_board
        else DeliveryBoard.objects.create(
            scenario=context[0],
            label="Other",
            nominal_term=1,
            program="AI",
        )
    )
    section = TermSection.objects.create(
        scenario=context[0], course_code="AI221", course_key="AI221", section="M1"
    )
    ordinary = SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        instructor="Lecturer",
        room="R1",
    )
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    teacher = Instructor.objects.create(full_name="Lecturer", normalised_name="lecturer")
    for code in ("ENG101", "AI221"):
        CourseInstructor.objects.create(
            program="AI", section="M", course_code=code, instructor=teacher
        )
    validation = validate_placement(
        board.id, "SUN", "09:00", "10:15", "R1", section.id, exclude_placement_id=ordinary.id
    )
    assert not validation["room_clashes"]
    if same_board:
        assert validation["instructor_clashes"]
        assert detect_board_conflicts(board.id)["instructor_clashes"]
    assert not detect_board_conflicts(board.id)["room_clashes"]
    rooms = preview_placement_room_candidates(ordinary.id)
    assert next(row for row in rooms["candidates"] if row["room_code"] == "R1")["available"]
    audit = build_scenario_quality(context[0].id)["rooms"]
    assert audit["clashes"] == 0 and audit["shortages"] == 0
    assert verify_persisted_scenario(context[0].id, context="no_room_regression")["count"] == 1
    if same_board:
        assert not check_publish_readiness(context[0].id)["ready"]
    TermSectionMeeting.objects.filter(term_section=section).update(instructor="Other lecturer")
    CourseInstructor.objects.filter(course_code="AI221").delete()
    settings.TIMETABLE_ENFORCE_LOCKS = True
    assert not hard_feasibility_rejections(
        ordinary,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="R1",
        room_reasons=[],
        planning_scope=None,
    )
    assert check_publish_readiness(context[0].id)["ready"]
    english[0].refresh_from_db()
    assert english[
        0
    ].is_locked  # A no-room exemption never unlocks English time or instructor assignments.


def test_english_is_excluded_from_bulk_room_and_missing_room_actions(context):
    from core.services.timetable_workspace import (
        build_scenario_builder_actions,
        preview_bulk_clean_room_assignments,
    )

    bundle(context)
    SectionPlacement.objects.update(room="UNASSIGNED")
    assert preview_bulk_clean_room_assignments(context[0].id)["total_unassigned"] == 0
    assert not any(
        row["kind"] in {"unassigned_room", "room_clash"}
        for row in build_scenario_builder_actions(context[0].id)["actions"]
    )


def test_switch_route_refuses_a_section_another_scenario_also_places(context, http):
    rows = bundle(context)
    owner = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"], name="Owner"
    )
    owner_board = DeliveryBoard.objects.create(
        scenario=owner, label="Owner", nominal_term=1, program="AI"
    )
    SectionPlacement.objects.create(
        board=owner_board,
        term_section=context[2],
        day=rows[0].day,
        start_time=rows[0].start_time,
        end_time=rows[0].end_time,
    )
    before = list(SectionPlacement.objects.order_by("pk").values())
    response = post(http, f"/ops/tw/placements/{rows[0].id}/switch-pattern/", {"pattern_id": PM})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PLACEMENT_EXTERNALLY_OWNED"
    assert list(SectionPlacement.objects.order_by("pk").values()) == before
