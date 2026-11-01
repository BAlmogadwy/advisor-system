"""Extend saved plans without rebuilding or moving their existing timetable."""

import json
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    PlannerJob,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    user = User.objects.create_user(username="course-add-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    scenario = TimetableScenario.objects.create(name="Saved", academic_year="1448", term="1")
    return client, scenario


def post(client, scenario, **payload):
    return client.post(
        f"/ops/tw/scenarios/{scenario.pk}/budget/",
        json.dumps(payload),
        content_type="application/json",
    )


def test_new_course_can_be_placed_after_build(setup):
    client, scenario = setup
    board = DeliveryBoard.objects.create(scenario=scenario, nominal_term=1, label="Board")
    response = post(
        client,
        scenario,
        course_code="ai999",
        course_name="New course",
        credit_hours=3,
        max_per_section=30,
        programme_term=1,
        sections=2,
    )
    assert response.status_code == 200
    row = response.json()["budget"][0]
    assert (row["course_key"], row["remaining_sections"], row["total_demand"]) == ("AI999", 2, 0)
    response = client.post(
        "/ops/tw/placements/create-planned/",
        json.dumps(
            {
                "board_id": board.pk,
                "course_code": "AI999",
                "section_label": "S1",
                "capacity": 30,
                "meetings": [{"day": "SUN", "start_time": "09:00", "end_time": "10:15"}],
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert SectionPlacement.objects.filter(board=board).count() == 1


def test_extra_sections_preserve_saved_meetings_and_metadata(setup):
    client, scenario = setup
    board = DeliveryBoard.objects.create(scenario=scenario, nominal_term=1, label="Board")
    budget = ScenarioSectionBudget.objects.create(
        scenario=scenario, course_code="AI101", planned_sections=1, max_per_section=35
    )
    section = TermSection.objects.create(
        scenario=scenario, course_code="AI101", course_key="AI101", section="S3"
    )
    placement = SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        is_locked=True,
    )
    before = list(SectionPlacement.objects.values())
    response = post(client, scenario, course_key="AI101", sections=2, max_per_section=99)
    assert response.status_code == 200
    budget.refresh_from_db()
    assert (budget.planned_sections, budget.max_per_section) == (3, 35)
    assert list(SectionPlacement.objects.values()) == before
    assert response.json()["budget"][0]["used_section_labels"] == ["S3"]
    assert placement.pk


@pytest.mark.parametrize(
    "payload",
    [
        {"course_code": ""},
        {"course_code": "AI101", "sections": 0},
        {"course_code": "AI101", "sections": "oops"},
        {"course_code": "AI101", "max_per_section": -1},
        {"course_code": "AI101", "credit_hours": 0},
        {"course_code": "AI101", "programme_term": 99},
    ],
)
def test_invalid_addition_is_atomic(setup, payload):
    client, scenario = setup
    assert post(client, scenario, **payload).status_code == 400
    assert not scenario.section_budgets.exists()


def test_published_and_running_scenarios_are_protected(setup):
    client, scenario = setup
    scenario.status = "published"
    scenario.save()
    response = post(client, scenario, course_code="AI101")
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    scenario.status = "draft"
    scenario.save()
    PlannerJob.objects.create(
        id=uuid4(), scenario=scenario, mode="optimise_current", status="queued"
    )
    response = post(client, scenario, course_code="AI101")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_BUSY"
    assert not scenario.section_budgets.exists()


def test_addition_requires_advisor_role(setup):
    _, scenario = setup
    client = Client()
    user = User.objects.create_user(username="ordinary-user")
    client.force_login(user)
    assert post(client, scenario, course_code="AI101").status_code == 403
