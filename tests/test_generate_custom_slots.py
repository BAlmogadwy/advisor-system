"""Generate a timetable with its own time slots, so its first build uses them.

Before this, a new draft always got the default columns and the only way to
other times was Edit time slots on the built draft, then a Full Rebuild that
the safety gate compares with the default-slot board (owner, 2026-10-27).
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import TimeSlotTemplate, TimetableScenario
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS
from core.services.timetable_generate import default_generate_slots

MORNING = [
    {"label": "08:30-09:45", "start": "08:30", "end": "09:45"},
    {"label": "", "start": "09:55", "end": "11:10"},
]


@pytest.fixture
def generate(monkeypatch):
    ensure_role_groups()
    user = User.objects.create_user(username="generate-slots-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    http = Client()
    http.force_login(user)
    scenario = TimetableScenario.objects.create(academic_year="1448", term="2", programs=["AI"])
    scaffold = Mock(
        return_value={
            "scenario": {"id": scenario.pk},
            "boards": [],
            "section_budget": [],
            "student_summary": {"classified": 1},
        }
    )
    monkeypatch.setattr("core.timetable_workspace_views.generate_workspace_scenario", scaffold)
    monkeypatch.setattr(
        "core.services.planner_job_runner.submit_planner_job", Mock(return_value="job")
    )
    monkeypatch.setattr("core.services.planner_job_runner.dispatch_planner_job", Mock())
    monkeypatch.setattr("core.services.planner_job_runner.is_async_planner_enabled", lambda: True)

    def post(**overrides):
        body = {"year": 1448, "semester": 2, "program": "AI", "defer_build": True, **overrides}
        return http.post(
            "/ops/tw/generate-workspace/", content_type="application/json", data=json.dumps(body)
        )

    return http, post, scaffold


@pytest.mark.django_db
def test_custom_columns_reach_the_new_draft_cleaned(generate):
    _, post, scaffold = generate
    labs = [
        {"label": "Lab A", "start": "08:30", "end": "10:10"},
        {"label": "Online 1", "start": "15:50", "end": "17:30"},
    ]
    assert post(slot_config=MORNING, lab_slot_config=labs).status_code == 201
    kwargs = scaffold.call_args.kwargs
    assert kwargs["slot_config"] == [
        {"label": "08:30-09:45", "start": "08:30", "end": "09:45"},
        {"label": "09:55-11:10", "start": "09:55", "end": "11:10"},
    ]
    # Sent as a plain row, the evening window keeps its online-only rule.
    assert kwargs["lab_slot_config"] == [
        {"label": "Lab A", "start": "08:30", "end": "10:10"},
        {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True},
    ]


@pytest.mark.django_db
def test_columns_left_out_or_empty_are_the_defaults(generate):
    _, post, scaffold = generate
    assert post().status_code == 201
    assert "slot_config" not in scaffold.call_args.kwargs
    assert "lab_slot_config" not in scaffold.call_args.kwargs
    assert post(slot_config=[], lab_slot_config=None).status_code == 201
    assert "slot_config" not in scaffold.call_args.kwargs
    assert "lab_slot_config" not in scaffold.call_args.kwargs


@pytest.mark.django_db
def test_invalid_columns_are_refused_before_any_scenario_is_created(generate):
    _, post, scaffold = generate
    before = TimetableScenario.objects.count()
    response = post(
        slot_config=[
            {"label": "ok", "start": "08:30", "end": "09:45"},
            {"label": "bad", "start": "10:00", "end": "09:00"},
        ],
        lab_slot_config="not a list",
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_SLOTS"
    assert {"list": "slot_config", "index": 1, "field": "end"}.items() <= error["details"][
        "errors"
    ][0].items()
    assert any(e["list"] == "lab_slot_config" for e in error["details"]["errors"])
    scaffold.assert_not_called()
    assert TimetableScenario.objects.count() == before


@pytest.mark.django_db
def test_defaults_are_the_admin_template_then_the_built_in_grid():
    assert default_generate_slots() == (list(DEFAULT_SLOTS), list(DEFAULT_LAB_SLOTS))
    TimeSlotTemplate.objects.create(name="Morning", slots=MORNING[:1], is_default=True)
    lectures, labs = default_generate_slots()
    assert lectures == MORNING[:1]
    assert labs == list(DEFAULT_LAB_SLOTS)


@pytest.mark.django_db
def test_the_page_carries_the_defaults_the_dialog_starts_from(generate):
    http, _, _ = generate
    page = http.get("/timetable-workspace/").content.decode()
    assert 'id="twGenDefaultSlots"' in page
    assert 'name="twGenSlots" value="custom"' in page
    payload = page.split('id="twGenDefaultSlots" type="application/json">', 1)[1].split(
        "</script>"
    )[0]
    assert json.loads(payload) == {
        "slot_config": list(DEFAULT_SLOTS),
        "lab_slot_config": list(DEFAULT_LAB_SLOTS),
    }


@pytest.mark.django_db
def test_the_scaffold_stores_the_chosen_columns_and_defaults_otherwise(monkeypatch):
    from core.services.timetable_generate import generate_workspace_scenario

    monkeypatch.setattr("core.services.timetable_generate.get_student_ids", lambda **_kw: [])
    monkeypatch.setattr(
        "core.services.recommender_batch.batch_recommend_multi_program",
        lambda student_ids, _year, _term: {},
    )
    chosen = generate_workspace_scenario(
        year=1448, semester=2, program="AI", run_autoplace=False, slot_config=MORNING[:1]
    )
    scenario = TimetableScenario.objects.get(pk=chosen["scenario"]["id"])
    assert scenario.slot_config == MORNING[:1]
    assert scenario.lab_slot_config == list(DEFAULT_LAB_SLOTS)

    plain = generate_workspace_scenario(
        year=1448, semester=2, program="AI", run_autoplace=False, scenario_name="plain"
    )
    scenario = TimetableScenario.objects.get(pk=plain["scenario"]["id"])
    assert scenario.slot_config == list(DEFAULT_SLOTS)
