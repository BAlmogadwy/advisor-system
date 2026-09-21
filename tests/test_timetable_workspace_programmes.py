from __future__ import annotations

import json
from typing import Any, Protocol

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db


class _TestResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _section(
    code: str,
    label: str,
    *,
    scenario: TimetableScenario | None = None,
) -> TermSection:
    letters = "".join(character for character in code if character.isalpha())
    numbers = "".join(character for character in code if character.isdigit())
    return TermSection.objects.create(
        scenario=scenario,
        source_tag="test",
        course_name=code,
        course_code=letters,
        course_number=numbers,
        course_key=code,
        section=label,
    )


def _client() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="workspace-program-admin", password="password")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def test_board_picker_intersects_global_sections_with_programme_membership() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Main")
    other_scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Other")
    ai_board = DeliveryBoard.objects.create(scenario=scenario, label="AI", program=" ai ")
    ds_board = DeliveryBoard.objects.create(scenario=scenario, label="DS", program="DS")

    ai_only = _section("AI221", "M1")
    ds_only = _section("DS341", "M1")
    shared = _section("CS285", "M3")
    local = _section("LOCAL1", "S1", scenario=scenario)
    foreign_local = _section("FOREIGN1", "S1", scenario=other_scenario)
    TermSectionProgram.objects.create(term_section=ai_only, program="AI")
    TermSectionProgram.objects.create(term_section=ds_only, program="DS")
    TermSectionProgram.objects.create(term_section=shared, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="DS")

    client = _client()
    ai_response = client.get(f"/ops/tw/boards/{ai_board.id}/unplaced/")
    ds_response = client.get(f"/ops/tw/boards/{ds_board.id}/unplaced/")

    assert ai_response.status_code == 200
    assert ds_response.status_code == 200
    ai_ids = {row["term_section_id"] for row in ai_response.json()["unplaced"]}
    ds_ids = {row["term_section_id"] for row in ds_response.json()["unplaced"]}
    assert ai_ids == {ai_only.id, shared.id, local.id}
    assert ds_ids == {ds_only.id, shared.id, local.id}
    assert foreign_local.id not in ai_ids | ds_ids


def test_board_placement_rejects_foreign_programme_and_accepts_shared_section() -> None:
    scenario = TimetableScenario.objects.create(academic_year="1448", term="1", name="Main")
    board = DeliveryBoard.objects.create(scenario=scenario, label="DS", program="DS")
    ai_only = _section("AI221", "M1")
    shared = _section("CS285", "M3")
    TermSectionProgram.objects.create(term_section=ai_only, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="AI")
    TermSectionProgram.objects.create(term_section=shared, program="DS")
    client = _client()

    def place(section: TermSection) -> _TestResponse:
        return client.post(
            "/ops/tw/placements/create/",
            data=json.dumps(
                {
                    "board_id": board.id,
                    "term_section_id": section.id,
                    "day": "SUN",
                    "start_time": "09:00",
                    "end_time": "10:15",
                    "room": "R1",
                }
            ),
            content_type="application/json",
        )

    rejected = place(ai_only)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "SECTION_NOT_AVAILABLE_TO_BOARD"
    assert not SectionPlacement.objects.filter(board=board, term_section=ai_only).exists()

    accepted = place(shared)
    assert accepted.status_code == 201
    assert SectionPlacement.objects.filter(board=board, term_section=shared).exists()
