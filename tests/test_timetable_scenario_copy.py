"""A workspace copy must be a faithful draft with no mutable source edges."""

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, User
from django.db.models import Q
from django.test import Client

from core.models import (
    BoardSectionVisibility,
    BoardStudentLink,
    DeliveryBoard,
    Instructor,
    PlannerJob,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    ScenarioStudentMap,
    SectionInstructor,
    SectionPlacement,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableRepairGlobalPlan,
    TimetableRepairJob,
    TimetableRepairRun,
    TimetableScenario,
)
from core.services.board_ownership import external_shared_section_ids
from core.services.planner_job_runner import submit_planner_job
from core.services.rbac import ROLE_GENERAL_ADVISOR, ROLE_STUDENT, ROLE_SUPER_ADMIN
from core.services.timetable_board_persistence import reset_scenario
from core.services.timetable_course_policy import ENGLISH_INTENSIVE_POLICY
from core.services.timetable_mutation_guard import ScenarioPlannerBusy, scenario_resource_ids
from core.services.timetable_scenario_copy import ScenarioCopyError, copy_timetable_scenario

pytestmark = pytest.mark.django_db


@pytest.fixture
def http():
    user = User.objects.create_user(username="scenario-copy-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def post(client, source_id, payload=None):
    return client.post(
        f"/ops/tw/scenarios/{source_id}/copy/",
        json.dumps(payload or {}),
        content_type="application/json",
    )


def section(scenario, key, label="S1"):
    return TermSection.objects.create(
        scenario=scenario,
        source_tag="tw_auto" if scenario else "scraper_timetable",
        source_file="source.xlsx",
        course_key=key,
        course_code=key,
        course_number="101",
        course_name=f"Name {key}",
        section=label,
        available_capacity=35,
        registered_count=7,
        created_at="original section metadata",
    )


@pytest.fixture
def populated():
    source = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Original",
        status="published",
        gender="M",
        programs=["AI", "DS"],
        slot_config=[{"start": "09:00", "end": "10:15", "label": "Morning"}],
        lab_slot_config=[{"start": "13:00", "end": "14:40"}],
        blocked_slots=[{"day": "TUE", "start": "12:00", "end": "13:00"}],
        notes="Original notes",
        created_by="original-author",
    )
    first = DeliveryBoard.objects.create(
        scenario=source,
        label="AI Term1",
        program="AI",
        nominal_term=1,
        target_size=30,
        display_order=2,
        notes="Board notes",
    )
    second = DeliveryBoard.objects.create(
        scenario=source,
        label="DS special",
        program="DS",
        nominal_term=3,
        board_type="special",
        display_order=4,
    )
    local = section(source, "CS101")
    global_section = section(None, "MATH105", "M4")
    english = section(source, "ENG101")
    unplaced = section(source, "UNPLACED101")
    visible = section(None, "VISIBLE101", "M3")
    other = TimetableScenario.objects.create(name="Foreign owner")
    foreign = section(other, "FOREIGN101", "M2")
    other_board = DeliveryBoard.objects.create(scenario=other, label="External")
    for board in (first, second):
        SectionPlacement.objects.create(
            board=board,
            term_section=local,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="R1",
            is_locked=(board == first),
        )
    for physical, board in ((global_section, first), (foreign, second)):
        SectionPlacement.objects.create(
            board=board,
            term_section=physical,
            day="MON",
            start_time="10:30",
            end_time="11:45",
            room="R2",
        )
    SectionPlacement.objects.create(
        board=other_board,
        term_section=global_section,
        day="MON",
        start_time="10:30",
        end_time="11:45",
        room="R2",
    )
    for physical in (local, global_section, foreign, unplaced):
        TermSectionMeeting.objects.create(
            term_section=physical,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            building="Building",
            floor_wing="West/2",
            room="R1",
            instructor="Free text instructor",
            created_at="original meeting metadata",
        )
    for meeting in ENGLISH_INTENSIVE_POLICY.patterns[0].meetings:
        values = meeting.to_dict()
        TermSectionMeeting.objects.create(
            term_section=english,
            day=values["day"],
            start_time=values["start"],
            end_time=values["end"],
            instructor="English instructor",
        )
        SectionPlacement.objects.create(
            board=first,
            term_section=english,
            day=values["day"],
            start_time=values["start"],
            end_time=values["end"],
            is_locked=(values["day"] == "SUN"),
        )
    for physical, program in ((local, "AI"), (local, "DS"), (global_section, "AI")):
        TermSectionProgram.objects.create(
            term_section=physical,
            program=program,
            assignment_source="imported",
        )
    BoardSectionVisibility.objects.create(board=second, term_section=local)
    BoardSectionVisibility.objects.create(board=first, term_section=visible)
    ScenarioStudentMap.objects.create(
        scenario=source,
        student_id=111,
        primary_term=1,
        is_cross_term=True,
        recommended_courses=["CS101"],
        recommended_course_keys=["CS101"],
    )
    ScenarioStudentCourseRequest.objects.create(
        scenario=source,
        student_id=111,
        course_key="CS101",
        course_code="CS101",
        course_name="CS",
        primary_term=1,
        status="blocked",
        priority="special_case",
        reason_blocked="capacity",
        reason_detail="Keep this reason",
        source="manual",
        source_payload={"context": ["source"]},
    )
    ScenarioSectionBudget.objects.create(
        scenario=source,
        course_key="CS101",
        course_code="CS101",
        course_name="CS",
        department="AI",
        credit_hours=3,
        planned_sections=2,
        max_per_section=35,
        total_demand=40,
        programme_term=1,
    )
    for board, link_type in ((first, "primary"), (second, "visitor")):
        BoardStudentLink.objects.create(board=board, student_id=111, link_type=link_type)
    instructor = Instructor.objects.create(
        full_name="Instructor", normalised_name="instructor", email="copy@example.invalid"
    )
    for owner, key in (
        (source, "CS101"),
        (source, "FUTURE101"),
        (None, "MATH105"),
        (other, "FOREIGN101"),
    ):
        SectionInstructor.objects.create(
            scenario=owner,
            course_key=key,
            section="M4" if key == "MATH105" else "M2" if key == "FOREIGN101" else "S1",
            instructor=instructor,
            role="primary",
            source="manual",
        )
    for physical in (local, global_section):
        StudentTermSection.objects.create(
            student_id=111,
            academic_year="1448",
            term="1",
            term_section=physical,
            source="scraper_timetable",
        )
    PlannerJob.objects.create(
        id=uuid4(), scenario=source, mode="optimise_current", status="succeeded"
    )
    TimetableRepairRun.objects.create(scenario=source, status="completed")
    TimetableRepairGlobalPlan.objects.create(scenario=source, request_signature="old")
    TimetableRepairJob.objects.create(scenario=source, kind="repair_analysis", status="succeeded")
    return source


def table_snapshot():
    tables = (
        TimetableScenario,
        DeliveryBoard,
        TermSection,
        TermSectionMeeting,
        TermSectionProgram,
        SectionInstructor,
        SectionPlacement,
        BoardSectionVisibility,
        ScenarioStudentMap,
        ScenarioStudentCourseRequest,
        ScenarioSectionBudget,
        BoardStudentLink,
        StudentTermSection,
    )
    return {table: list(table.objects.order_by("pk").values()) for table in tables}


def data(row, *ignored):
    return {
        f.attname: getattr(row, f.attname)
        for f in row._meta.concrete_fields
        if not f.primary_key
        and f.attname not in ignored
        and not getattr(f, "auto_now", False)
        and not getattr(f, "auto_now_add", False)
    }


def test_copy_is_complete_private_draft_and_keeps_all_source_rows(populated):
    source = populated
    before = table_snapshot()
    result = copy_timetable_scenario(source.pk, name="Independent", created_by="new-author")
    target = result.scenario
    assert target.pk != source.pk and target.status == "draft"
    assert target.name == "Independent" and target.created_by == "new-author"
    assert data(target, "name", "status", "created_by") == data(
        source, "name", "status", "created_by"
    )
    for table, rows in before.items():
        existing = {
            row["id"]: row for row in table.objects.filter(pk__in=[r["id"] for r in rows]).values()
        }
        assert existing == {row["id"]: row for row in rows}, table
    originals = TermSection.objects.filter(
        Q(scenario=source)
        | Q(placements__board__scenario=source)
        | Q(board_visibility__board__scenario=source)
    ).distinct()
    copied = {(s.course_key, s.section): s for s in target.term_sections.all()}
    assert len(copied) == originals.count() == 6
    assert set(originals.values_list("pk", flat=True)).isdisjoint(s.pk for s in copied.values())
    for old in originals:
        new = copied[(old.course_key, old.section)]
        assert data(new, "scenario_id") == data(old, "scenario_id")
        for relation in ("meetings", "program_links"):
            assert [data(r, "term_section_id") for r in getattr(new, relation).order_by("pk")] == [
                data(r, "term_section_id") for r in getattr(old, relation).order_by("pk")
            ]
    for old_board in source.boards.all():
        new_board = target.boards.get(label=old_board.label)
        assert data(new_board, "scenario_id") == data(old_board, "scenario_id")
        for old in old_board.placements.all():
            new = new_board.placements.get(
                term_section=copied[(old.term_section.course_key, old.term_section.section)],
                day=old.day,
                start_time=old.start_time,
            )
            assert data(new, "board_id", "term_section_id") == data(
                old, "board_id", "term_section_id"
            )
        assert [
            (r.student_id, r.link_type) for r in new_board.student_links.order_by("student_id")
        ] == [(r.student_id, r.link_type) for r in old_board.student_links.order_by("student_id")]
        assert set(
            new_board.visible_sections.values_list("term_section__course_key", flat=True)
        ) == set(old_board.visible_sections.values_list("term_section__course_key", flat=True))
    for table in (ScenarioStudentMap, ScenarioStudentCourseRequest, ScenarioSectionBudget):
        assert [data(r, "scenario_id") for r in table.objects.filter(scenario=target)] == [
            data(r, "scenario_id") for r in table.objects.filter(scenario=source)
        ]
    assert set(target.section_instructors.values_list("course_key", flat=True)) == {
        "CS101",
        "FUTURE101",
        "MATH105",
        "FOREIGN101",
    }
    assert not StudentTermSection.objects.filter(term_section__scenario=target).exists()
    assert not target.planner_jobs.exists()
    assert not target.repair_runs.exists()
    assert not target.repair_global_plans.exists()
    assert not target.repair_jobs.exists()
    assert not external_shared_section_ids(target.pk)
    assert scenario_resource_ids(target.pk)[0] == {target.pk}
    assert target.boards.filter(placements__term_section=copied[("CS101", "S1")]).count() == 2
    english = list(
        SectionPlacement.objects.filter(board__scenario=target, term_section__course_key="ENG101")
    )
    assert ENGLISH_INTENSIVE_POLICY.accepts(english) and all(not p.room for p in english)


def test_copy_can_be_reset_and_deleted_without_changing_source(populated):
    source = populated
    before = table_snapshot()
    target = copy_timetable_scenario(source.pk).scenario
    reset_scenario(target.pk, keep_locked=False)
    target.delete()
    assert table_snapshot() == before


def test_http_editing_copy_changes_neither_source_placements_nor_canonical_meetings(
    populated, http
):
    original = table_snapshot()
    result = post(http, populated.pk).json()
    target_id = result["scenario"]["id"]
    copied = SectionPlacement.objects.filter(
        board__scenario_id=target_id, term_section__course_key="CS101", is_locked=True
    ).get()
    response = http.post(
        f"/ops/tw/placements/{copied.pk}/lock/", "{}", content_type="application/json"
    )
    assert response.status_code == 200 and not response.json()["is_locked"]
    response = http.post(
        f"/ops/tw/placements/{copied.pk}/move/",
        json.dumps({"day": "TUE", "start_time": "09:00", "end_time": "10:15", "room": ""}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    copied.refresh_from_db()
    assert copied.day == "TUE"
    for table, rows in original.items():
        assert (
            list(table.objects.filter(pk__in=[r["id"] for r in rows]).order_by("pk").values())
            == rows
        )


def test_conflicting_scope_instructor_metadata_is_rejected_without_losing_either_assignment(
    populated,
):
    teacher = Instructor.objects.create(full_name="Override", normalised_name="override")
    SectionInstructor.objects.create(
        scenario=populated, course_key="MATH105", section="M4", instructor=teacher, role="primary"
    )
    before = table_snapshot()
    with pytest.raises(ScenarioCopyError) as exc:
        copy_timetable_scenario(populated.pk)
    assert exc.value.code == "COPY_INSTRUCTOR_IDENTITY_CONFLICT"
    assert table_snapshot() == before


def test_identical_instructor_allocations_in_two_scopes_are_copied_once(populated):
    global_link = SectionInstructor.objects.get(scenario__isnull=True, course_key="MATH105")
    SectionInstructor.objects.create(scenario=populated, **data(global_link, "scenario_id"))
    target = copy_timetable_scenario(populated.pk).scenario
    assert target.section_instructors.filter(course_key="MATH105").count() == 1


def test_copy_retries_visibility_expansion_before_writing(populated, monkeypatch):
    from core.services import timetable_scenario_copy as service

    original = service._visible_section_ids
    calls = []

    def changing_visibility(scenario_id):
        calls.append(scenario_id)
        return () if len(calls) == 1 else original(scenario_id)

    monkeypatch.setattr(service, "_visible_section_ids", changing_visibility)
    result = copy_timetable_scenario(populated.pk)
    assert len(calls) == 4
    assert TimetableScenario.objects.filter(name="Original (copy)").count() == 1
    assert result.scenario.term_sections.filter(course_key="VISIBLE101").exists()


def test_copy_survives_deleting_original_and_other_owners(populated):
    target = copy_timetable_scenario(populated.pk).scenario
    count = SectionPlacement.objects.filter(board__scenario=target).count()
    TimetableScenario.objects.exclude(pk=target.pk).delete()
    assert target.term_sections.count() == 6
    assert SectionPlacement.objects.filter(board__scenario=target).count() == count == 8


def test_rollback_removes_partial_copy_after_a_late_failure(populated):
    before = table_snapshot()
    with patch.object(
        BoardStudentLink.objects, "bulk_create", side_effect=RuntimeError("write failed")
    ):
        with pytest.raises(RuntimeError, match="write failed"):
            copy_timetable_scenario(populated.pk)
    assert table_snapshot() == before


def test_ambiguous_physical_section_identity_is_rejected_before_creating_copy(populated):
    global_duplicate = section(None, "CS101")
    BoardSectionVisibility.objects.create(
        board=populated.boards.first(), term_section=global_duplicate
    )
    before = table_snapshot()
    with pytest.raises(ScenarioCopyError) as exc:
        copy_timetable_scenario(populated.pk)
    assert exc.value.code == "COPY_SECTION_IDENTITY_CONFLICT"
    assert table_snapshot() == before


@pytest.mark.parametrize("state", ["queued", "running"])
def test_active_shared_or_visibility_only_planner_prevents_copy(populated, state):
    visible = TermSection.objects.get(course_key="VISIBLE101")
    owner = TimetableScenario.objects.create(name="Visibility owner")
    board = DeliveryBoard.objects.create(scenario=owner, label="Owner")
    SectionPlacement.objects.create(
        board=board, term_section=visible, day="SUN", start_time="09:00", end_time="10:15"
    )
    job_id = submit_planner_job(scenario_id=owner.pk, mode="optimise_current")
    PlannerJob.objects.filter(pk=job_id).update(status=state)
    before = table_snapshot()
    with pytest.raises(ScenarioPlannerBusy):
        copy_timetable_scenario(populated.pk)
    assert table_snapshot() == before


def test_automatic_names_are_unique_and_manual_conflict_does_not_overwrite():
    source = TimetableScenario.objects.create(academic_year="1448", term="1", name="Empty")
    first = copy_timetable_scenario(source.pk).scenario
    second = copy_timetable_scenario(source.pk).scenario
    assert [first.name, second.name] == ["Empty (copy)", "Empty (copy 2)"]
    with pytest.raises(ScenarioCopyError) as exc:
        copy_timetable_scenario(source.pk, name=first.name)
    assert exc.value.code == "SCENARIO_EXISTS" and TimetableScenario.objects.count() == 3


@pytest.mark.parametrize("name", [None, "", "   ", "A" * 200, "مسودة مستقلة"])
def test_optional_and_unicode_names(name):
    source = TimetableScenario.objects.create(name="Original", academic_year="1448", term="1")
    target = copy_timetable_scenario(source.pk, name=name).scenario
    assert target.name == (name.strip() if name and name.strip() else "Original (copy)")


def test_long_legacy_source_name_gets_a_bounded_automatic_copy_name():
    source = TimetableScenario.objects.create(name="A" * 300)
    assert len(copy_timetable_scenario(source.pk).scenario.name) == 200


@pytest.mark.parametrize("name", [True, 17, [], {}, "A" * 201, "\ud800", "\x00"])
def test_invalid_names_are_rejected_without_creating_rows(name):
    source = TimetableScenario.objects.create(name="Original")
    before = table_snapshot()
    with pytest.raises(ScenarioCopyError) as exc:
        copy_timetable_scenario(source.pk, name=name)
    assert exc.value.code == "VALIDATION_NAME" and exc.value.status == 400
    assert table_snapshot() == before


def test_http_copy_preserves_published_source_and_returns_existing_shapes(populated, http):
    response = post(http, populated.pk, {"name": "Editable alternative"})
    assert response.status_code == 201
    body = response.json()
    assert body["ok"] and body["copied_from_scenario_id"] == populated.pk
    assert body["scenario"]["status"] == "draft"
    assert body["scenario"]["created_by"] == "scenario-copy-admin"
    assert len(body["boards"]) == 2
    assert body["copy_summary"]["placements"] == 8
    populated.refresh_from_db()
    assert populated.status == "published"


@pytest.mark.parametrize("role", [ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR])
def test_allowed_roles_can_copy(role, populated):
    user = User.objects.create_user(username=f"copy-{role}")
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    client = Client()
    client.force_login(user)
    assert post(client, populated.pk).status_code == 201


def test_wrong_role_and_anonymous_cannot_copy(populated):
    before = TimetableScenario.objects.count()
    client = Client()
    assert post(client, populated.pk).status_code == 302
    user = User.objects.create_user(username="copy-student")
    user.groups.add(Group.objects.get_or_create(name=ROLE_STUDENT)[0])
    client.force_login(user)
    assert post(client, populated.pk).status_code == 403
    assert TimetableScenario.objects.count() == before


def test_http_methods_missing_source_invalid_json_and_validation(http):
    assert http.get("/ops/tw/scenarios/999999/copy/").status_code == 405
    assert post(http, 999999).status_code == 404
    source = TimetableScenario.objects.create(name="Original")
    assert (
        http.post(
            f"/ops/tw/scenarios/{source.pk}/copy/", "[1]", content_type="application/json"
        ).status_code
        == 400
    )
    assert post(http, source.pk, {"name": False}).status_code == 400
    assert TimetableScenario.objects.count() == 1


def test_http_busy_source_is_not_copied(populated, http):
    PlannerJob.objects.create(
        id=uuid4(), scenario=populated, mode="optimise_current", status="running"
    )
    before = table_snapshot()
    response = post(http, populated.pk)
    assert response.status_code == 409 and response.json()["error"]["code"] == "SCENARIO_BUSY"
    assert table_snapshot() == before
