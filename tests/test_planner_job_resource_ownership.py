"""Regression probes for shared ownership and synchronous planner leases."""

import json

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    PlannerJob,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.planner_job_runner import cancel_planner_job, run_planner_job, submit_planner_job
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.timetable_mutation_guard import ScenarioPlannerBusy
from core.services.timetable_section_policy import create_policy_section_bundle
from core.services.timetable_workspace import preview_placement_slot_candidates

pytestmark = pytest.mark.django_db


@pytest.fixture
def http():
    user = User.objects.create_user(username="resource-review-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def post(http, path, payload):
    return http.post(path, json.dumps(payload), content_type="application/json")


@pytest.fixture
def shared():
    owner = TimetableScenario.objects.create(name="Section owner")
    other = TimetableScenario.objects.create(name="Historical board owner")
    own_board = DeliveryBoard.objects.create(scenario=owner, label="Owner")
    external_board = DeliveryBoard.objects.create(scenario=other, label="Historical")
    section = TermSection.objects.create(
        scenario=owner, course_key="AI111", course_code="AI111", section="S1"
    )
    external = SectionPlacement.objects.create(
        board=external_board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="BASELINE",
    )
    return owner, other, own_board, external_board, section, external


def test_cancel_cannot_overwrite_an_accepted_external_board_edit(shared, http, monkeypatch):
    owner, other, _, _, _, external = shared
    job_id = submit_planner_job(scenario_id=owner.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def pipeline(*args, **kwargs):
        response = post(http, f"/ops/tw/placements/{external.id}/move/", {"room": "EDIT"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SCENARIO_BUSY"
        with pytest.raises(ScenarioPlannerBusy):
            submit_planner_job(scenario_id=other.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        assert cancel_planner_job(job_id)
        return {}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    external.refresh_from_db()
    assert external.room == "BASELINE"
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_CANCELLED
    assert (
        post(http, f"/ops/tw/placements/{external.id}/move/", {"room": "AFTER"}).status_code == 200
    )


def test_related_lease_survives_section_replacement(shared, http, monkeypatch):
    owner, other, _, _, section, external = shared
    job_id = submit_planner_job(scenario_id=owner.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
    assert PlannerJob.objects.get(pk=job_id).params["owned_scenario_ids"] == sorted(
        [owner.id, other.id]
    )

    def pipeline(*args, **kwargs):
        section.delete()
        assert (
            post(
                http,
                f"/ops/tw/scenarios/{other.id}/blocked-slots/",
                {"day": "SUN", "start": "09:00"},
            ).status_code
            == 409
        )
        with pytest.raises(ScenarioPlannerBusy):
            submit_planner_job(scenario_id=other.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        assert cancel_planner_job(job_id)
        return {}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    external.refresh_from_db()
    assert external.room == "BASELINE"
    other.refresh_from_db()
    assert not other.blocked_slots


def test_creating_a_new_copy_cannot_bypass_existing_shared_lease(http):
    owner = TimetableScenario.objects.create(name="First copy")
    other = TimetableScenario.objects.create(name="New copy")
    first = DeliveryBoard.objects.create(scenario=owner, label="First", program="AI")
    second = DeliveryBoard.objects.create(scenario=other, label="Second", program="AI")
    section = TermSection.objects.create(course_key="AI111", course_code="AI111", section="S1")
    TermSectionProgram.objects.create(term_section=section, program="AI")
    SectionPlacement.objects.create(
        board=first, term_section=section, day="SUN", start_time="09:00", end_time="10:15"
    )
    submit_planner_job(scenario_id=owner.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
    response = post(
        http,
        "/ops/tw/placements/create/",
        {
            "board_id": second.id,
            "term_section_id": section.id,
            "day": "MON",
            "start_time": "09:00",
            "end_time": "10:15",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_BUSY"
    assert not SectionPlacement.objects.filter(board=second).exists()


def test_unrelated_scenario_remains_editable(shared, http):
    owner, _, _, _, _, _ = shared
    unrelated = TimetableScenario.objects.create(name="Independent")
    submit_planner_job(scenario_id=owner.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
    response = post(
        http, f"/ops/tw/scenarios/{unrelated.id}/blocked-slots/", {"day": "SUN", "start": "09:00"}
    )
    assert response.status_code == 200


def test_caller_cannot_override_resource_ownership(shared):
    owner, other, _, _, section, _ = shared
    job_id = submit_planner_job(
        scenario_id=owner.id,
        mode=PlannerJob.MODE_OPTIMISE_CURRENT,
        params={"owned_scenario_ids": [], "owned_term_section_ids": []},
    )
    assert PlannerJob.objects.get(pk=job_id).params["owned_scenario_ids"] == sorted(
        [owner.id, other.id]
    )
    assert PlannerJob.objects.get(pk=job_id).params["owned_term_section_ids"] == [section.id]


@pytest.mark.parametrize("attempt", ["manual_copy", "planner_submit"])
def test_full_reset_retains_lease_on_disconnected_global_section(http, monkeypatch, attempt):
    owner = TimetableScenario.objects.create(name="Reset owner")
    other = TimetableScenario.objects.create(name="New global copy")
    first = DeliveryBoard.objects.create(scenario=owner, label="First", program="AI")
    second = DeliveryBoard.objects.create(scenario=other, label="Second", program="AI")
    section = TermSection.objects.create(
        course_key="AI111", course_code="AI111", section="S1", source_tag="tw_auto"
    )
    TermSectionProgram.objects.create(term_section=section, program="AI")
    original = SectionPlacement.objects.create(
        board=first, term_section=section, day="SUN", start_time="09:00", end_time="10:15"
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        instructor="Original teacher",
    )
    before_meetings = list(TermSectionMeeting.objects.filter(term_section=section).values())
    job_id = submit_planner_job(scenario_id=owner.id, mode=PlannerJob.MODE_FULL_REBUILD)
    job = PlannerJob.objects.get(pk=job_id)
    assert job.params["owned_scenario_ids"] == [owner.id]
    assert job.params["owned_term_section_ids"] == [section.id]
    observed_copy_statuses = []

    def pipeline(*args, **kwargs):
        # The real full-rebuild runner already removed A's sole global copy.
        assert not SectionPlacement.objects.filter(board=first).exists()
        assert TermSection.objects.filter(pk=section.id).exists()
        if attempt == "manual_copy":
            response = post(
                http,
                "/ops/tw/placements/create/",
                {
                    "board_id": second.id,
                    "term_section_id": section.id,
                    "day": "TUE",
                    "start_time": "10:30",
                    "end_time": "11:45",
                },
            )
            observed_copy_statuses.append(response.status_code)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "SCENARIO_BUSY"
            assert not SectionPlacement.objects.filter(board=second).exists()
        else:
            # A historical/imported board copy can appear independently of the
            # create API. Submission must compare the physical lease as well.
            SectionPlacement.objects.create(
                board=second, term_section=section, day="SUN", start_time="09:00", end_time="10:15"
            )
            with pytest.raises(ScenarioPlannerBusy):
                submit_planner_job(scenario_id=other.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        assert cancel_planner_job(job_id)
        return {}

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    if attempt == "manual_copy":
        assert observed_copy_statuses == [409]
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_CANCELLED
    assert list(TermSectionMeeting.objects.filter(term_section=section).values()) == before_meetings
    original.refresh_from_db()
    assert original.day == "SUN" and original.start_time == "09:00"
    assert not PlannerJob.objects.filter(scenario=other).exists()


@pytest.mark.parametrize("async_enabled", [False, True])
def test_sync_optimisation_owns_scenario_and_preserves_api_shape(
    shared, http, monkeypatch, settings, async_enabled
):
    owner, _, _, _, _, _ = shared
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = async_enabled
    summary = {"mode": "current", "placements": 0}

    def pipeline(scenario_id, **kwargs):
        assert PlannerJob.objects.get(scenario_id=scenario_id).status == PlannerJob.STATUS_RUNNING
        with pytest.raises(ScenarioPlannerBusy):
            submit_planner_job(scenario_id=scenario_id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        assert (
            post(
                http,
                f"/ops/tw/scenarios/{scenario_id}/blocked-slots/",
                {"day": "SUN", "start": "09:00"},
            ).status_code
            == 409
        )
        return summary

    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    response = post(http, f"/ops/tw/scenarios/{owner.id}/optimise-v2/", {})
    assert response.status_code == 200, response.content
    job = PlannerJob.objects.get(scenario=owner)
    assert response.json() == {"ok": True, "optimisation": {**summary, "seed": job.params["seed"]}}
    assert PlannerJob.objects.get(scenario=owner).status == PlannerJob.STATUS_SUCCEEDED
    assert (
        post(
            http, f"/ops/tw/scenarios/{owner.id}/blocked-slots/", {"day": "SUN", "start": "09:00"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("failure", ["error_result", "exception"])
def test_sync_failure_releases_ownership_and_preserves_error_response(
    shared, http, monkeypatch, failure
):
    owner, other, _, _, _, _ = shared

    def pipeline(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError("solver failed")
        return {"error": "No course profiles"}

    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    response = post(http, f"/ops/tw/scenarios/{owner.id}/optimise-v2/", {})
    assert response.status_code == (500 if failure == "exception" else 400)
    assert response.json()["error"]["code"] == (
        "OPTIMISER_ERROR" if failure == "exception" else "OPTIMISER_NO_DATA"
    )
    assert PlannerJob.objects.get(scenario=owner).status == PlannerJob.STATUS_FAILED
    assert (
        post(
            http, f"/ops/tw/scenarios/{other.id}/blocked-slots/", {"day": "SUN", "start": "09:00"}
        ).status_code
        == 200
    )


def test_english_preview_excludes_own_copies_but_checks_real_teacher_sessions():
    scenario = TimetableScenario.objects.create(name="English preview")
    first = DeliveryBoard.objects.create(scenario=scenario, label="First")
    second = DeliveryBoard.objects.create(scenario=scenario, label="Second")
    section = TermSection.objects.create(
        scenario=scenario, course_code="ENG101", course_key="ENG101", section="S1"
    )
    rows = create_policy_section_bundle(
        first, section, day="SUN", start_time="09:00", end_time="12:00"
    )
    create_policy_section_bundle(second, section, day="SUN", start_time="09:00", end_time="12:00")
    TermSectionMeeting.objects.filter(term_section=section).update(instructor="Teacher")
    other_section = TermSection.objects.create(
        scenario=scenario, course_key="AI111", course_code="AI111", section="S1"
    )
    TermSectionMeeting.objects.create(
        term_section=other_section,
        day="SUN",
        start_time="13:00",
        end_time="14:15",
        instructor="Teacher",
    )
    SectionPlacement.objects.create(
        board=second, term_section=other_section, day="SUN", start_time="13:00", end_time="14:15"
    )
    candidates = {
        row["pattern_id"]: row
        for row in preview_placement_slot_candidates(rows[0].id)["candidates"]
    }
    assert candidates["ENG_INTENSIVE_V1_AM"]["critical_count"] == 0
    afternoon = candidates["ENG_INTENSIVE_V1_PM"]
    assert afternoon["critical_count"] == 1
    assert afternoon["evidence"][0]["kind"] == "instructor"


@pytest.mark.parametrize("owner_kind", ["global", "other_scenario"])
def test_real_sa_cancel_restores_nonowned_canonical_meeting_identity(monkeypatch, owner_kind):
    from core.services.timetable_local_search import optimize_and_persist_board
    from tests.test_instructor_compaction import _gappy_board

    scenario = _gappy_board()
    board = DeliveryBoard.objects.get(scenario=scenario)
    sections = list(TermSection.objects.filter(scenario=scenario).order_by("course_key"))
    TermSection.objects.filter(pk__in=[row.id for row in sections]).update(source_tag="tw_auto")
    nonowned = sections[1]
    other = (
        TimetableScenario.objects.create(name="Canonical owner")
        if owner_kind == "other_scenario"
        else None
    )
    nonowned.scenario = other
    nonowned.save(update_fields=["scenario"])
    TermSectionMeeting.objects.filter(term_section=nonowned).update(
        building="Historical building",
        floor_wing="Historical floor",
        created_at="created",
        updated_at="updated",
    )
    before_meetings = list(TermSectionMeeting.objects.order_by("pk").values())
    before_placements = list(SectionPlacement.objects.order_by("pk").values())
    candidate = {
        "status": "optimized",
        "cost_before": 100,
        "cost_after": 0,
        "sections": [{"term_section_id": row.id} for row in sections],
        "schedule": {
            0: [{"day": "MON", "start": "09:00", "end": "10:15"}],
            1: [{"day": "MON", "start": "10:30", "end": "11:45"}],
        },
    }
    monkeypatch.setattr(
        "core.services.timetable_local_search.optimize_board", lambda *args, **kwargs: candidate
    )
    job_id = submit_planner_job(scenario_id=scenario.id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)

    def pipeline(*args, **kwargs):
        result = optimize_and_persist_board(board.id)
        assert not result.get("persist_rolled_back") and not result.get("sa_evaluator_rolled_back")
        assert TermSectionMeeting.objects.get(term_section=nonowned).start_time == "10:30"
        assert cancel_planner_job(job_id)
        return result

    monkeypatch.setattr("core.services.timetable_autoplace.auto_place_scenario", pipeline)
    run_planner_job(job_id)
    assert PlannerJob.objects.get(pk=job_id).status == PlannerJob.STATUS_CANCELLED
    assert list(TermSectionMeeting.objects.order_by("pk").values()) == before_meetings
    assert list(SectionPlacement.objects.order_by("pk").values()) == before_placements


@pytest.mark.parametrize("engine", ["classic", "scheduler"])
@pytest.mark.parametrize("outcome", ["success", "error_result", "exception"])
def test_sync_generation_owns_scenario_for_both_engines(
    shared, http, monkeypatch, settings, engine, outcome
):
    owner, _, _, _, _, _ = shared
    settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED = False
    monkeypatch.setattr(
        "core.timetable_workspace_views.generate_workspace_scenario",
        lambda **kwargs: {
            "scenario": {"id": owner.id},
            "boards": [],
            "section_budget": [],
            "student_summary": {"classified": 1},
        },
    )
    summary = {"engine": engine, "instructor_days": 2}

    def pipeline(scenario_id, **kwargs):
        assert PlannerJob.objects.get(scenario_id=scenario_id).status == PlannerJob.STATUS_RUNNING
        with pytest.raises(ScenarioPlannerBusy):
            submit_planner_job(scenario_id=scenario_id, mode=PlannerJob.MODE_OPTIMISE_CURRENT)
        assert (
            post(
                http,
                f"/ops/tw/scenarios/{scenario_id}/blocked-slots/",
                {"day": "SUN", "start": "09:00"},
            ).status_code
            == 409
        )
        if outcome == "exception":
            raise RuntimeError("generation failed")
        return {"error": "generation failed"} if outcome == "error_result" else summary

    monkeypatch.setattr("core.services.timetable_v2_runner.run_v2_optimisation_guarded", pipeline)
    monkeypatch.setattr("scheduler.bridge.build_into_scenario", pipeline)
    response = post(
        http,
        "/ops/tw/generate-workspace/",
        {
            "year": 1448,
            "semester": 2,
            "program": "AI",
            "section": "M",
            "engine": engine,
            "defer_build": True,
        },
    )
    assert response.status_code == (201 if outcome == "success" else 500), response.content
    job = PlannerJob.objects.get(scenario=owner)
    assert job.status == (
        PlannerJob.STATUS_SUCCEEDED if outcome == "success" else PlannerJob.STATUS_FAILED
    )
    assert job.mode == (
        PlannerJob.MODE_SCHEDULER_BUILD
        if engine == "scheduler"
        else PlannerJob.MODE_OPTIMISE_V2_FULL
    )
    if outcome == "success":
        assert response.json()["build_summary"] == {**summary, "seed": job.params["seed"]}
        assert "build_job_id" not in response.json()
    else:
        assert response.json()["error"]["code"] == "GENERATE_FAILED"
    assert (
        post(
            http, f"/ops/tw/scenarios/{owner.id}/blocked-slots/", {"day": "SUN", "start": "09:00"}
        ).status_code
        == 200
    )
