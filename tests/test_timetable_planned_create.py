"""Planned sections in the Timetable Workspace: label bookkeeping and re-creation.

Two server facts behind the "required sections" list:

* ``compute_scenario_budget`` names, for every course, the section labels in use
  across the whole scenario, in natural order, so the page can propose a free
  label instead of ``S{used + 1}`` (S1 and S3 used: S2 is free, then S4).
* ``create_planned_section_placements`` reuses a planned section that was
  created before. Taking a section off the board leaves its meeting rows, so a
  section created with pattern A, removed, and created again with pattern B used
  to hold A's rows and B's. The leftovers go, but only for a section the
  workspace planned that is on no board; a registrar's or auto-built section, or
  one still placed, keeps its rows.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
)
from core.services.timetable_workspace import (
    PLANNED_SECTION_SOURCE_TAG,
    compute_scenario_budget,
    create_planned_section_placements,
)
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    engine_rows,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)

pytestmark = pytest.mark.django_db

# One 4-credit course needs a complete 3-meeting pattern, one meeting per day.
PATTERN_A = (
    ("SUN", "08:00", "09:15"),
    ("TUE", "08:00", "09:15"),
    ("THU", "08:00", "09:15"),
)
# Shares THU with A at a different time, so no row of B can be mistaken for A's.
PATTERN_B = (
    ("MON", "11:00", "12:15"),
    ("WED", "11:00", "12:15"),
    ("THU", "11:00", "12:15"),
)


def _workspace():
    """A scenario with two boards and one 4-credit required course."""
    sc, board = scenario()
    other = DeliveryBoard.objects.create(
        scenario=sc, label="Term 4", nominal_term=4, program="AI", display_order=2
    )
    key = requirement("AI", "AI401", "PLANNED COURSE", credit=4)
    budget(sc, key, 3, credit=4)
    return sc, board, other, key


def _create(board, key, label, pattern, *, code="AI401"):
    return create_planned_section_placements(
        board.id,
        course_code=code,
        course_key=key,
        course_name="PLANNED COURSE",
        section_label=label,
        capacity=30,
        meetings=[
            {"day": day, "start_time": start, "end_time": end, "room": ""}
            for day, start, end in pattern
        ],
    )


def _remove_from_board(term_section):
    """What removing a section from the board does: the placements go, nothing else."""
    SectionPlacement.objects.filter(term_section=term_section).delete()


def _meeting_rows(term_section):
    return sorted(
        TermSectionMeeting.objects.filter(term_section=term_section).values_list(
            "day", "start_time", "end_time"
        )
    )


def _rows(pattern):
    return sorted(pattern)


def _budget_row(sc, key):
    return next(row for row in compute_scenario_budget(sc.id) if row["course_key"] == key)


# ── P1: the labels in use ─────────────────────────────────────────────────


def test_used_section_labels_are_listed_in_natural_order_across_every_board():
    sc, board, other, key = _workspace()
    place(board, section(sc, key, "S1"), [("SUN", "08:00", "09:15")])
    place(board, section(sc, key, "S10"), [("MON", "08:00", "09:15")])
    place(other, section(sc, key, "S3"), [("TUE", "08:00", "09:15")])

    row = _budget_row(sc, key)

    # Natural order (S3 before S10), and S3 -- placed on the OTHER board -- is in.
    assert row["used_section_labels"] == ["S1", "S3", "S10"]
    assert row["used_sections"] == 3


def test_a_course_with_no_sections_has_an_empty_label_list():
    sc, board, _other, key = _workspace()
    free = requirement("AI", "AI402", "UNPLACED COURSE")
    budget(sc, free, 1)
    only = requirement("AI", "AI403", "OTHER COURSE")
    budget(sc, only, 1)
    place(board, section(sc, only, "S2"), [("SUN", "10:00", "11:15")])

    rows = {row["course_key"]: row for row in compute_scenario_budget(sc.id)}

    assert set(rows) == {key, free, only}
    assert rows[free]["used_section_labels"] == []
    assert rows[key]["used_section_labels"] == []
    # A label belongs to its own course only.
    assert rows[only]["used_section_labels"] == ["S2"]


def test_a_section_that_is_on_no_board_is_not_in_use():
    sc, board, _other, key = _workspace()
    place(board, section(sc, key, "S1"), [("SUN", "08:00", "09:15")])
    section(sc, key, "S2")  # created earlier, taken off every board

    row = _budget_row(sc, key)

    assert row["used_section_labels"] == ["S1"]
    assert row["used_sections"] == 1


def test_the_budget_endpoint_carries_the_label_list_to_the_page():
    from django.contrib.auth.models import Group, User

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    sc, board, other, key = _workspace()
    place(board, section(sc, key, "S10"), [("SUN", "08:00", "09:15")])
    place(other, section(sc, key, "S2"), [("MON", "08:00", "09:15")])
    ensure_role_groups()
    user = User.objects.create(username="planned-create-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)

    response = client.get(f"/ops/tw/scenarios/{sc.id}/budget/")

    assert response.status_code == 200, response.content
    rows = json.loads(response.content)["budget"]
    assert [row["used_section_labels"] for row in rows] == [["S2", "S10"]]


# ── P2: creating a planned section again ──────────────────────────────────


def test_recreating_a_removed_planned_section_holds_only_the_new_pattern():
    sc, board, _other, key = _workspace()
    first = _create(board, key, "S2", PATTERN_A)
    term_section = first["term_section"]
    assert term_section.source_tag == PLANNED_SECTION_SOURCE_TAG == "tw_planned"
    _remove_from_board(term_section)
    # The leftover this fixes: the section is off the board, A's rows are still there.
    assert _meeting_rows(term_section) == _rows(PATTERN_A)

    second = _create(board, key, "S2", PATTERN_B)

    assert second["term_section"].id == term_section.id  # reused, not created
    assert _meeting_rows(term_section) == _rows(PATTERN_B)
    assert sorted(
        SectionPlacement.objects.filter(term_section=term_section).values_list(
            "day", "start_time", "end_time"
        )
    ) == _rows(PATTERN_B)
    assert TermSection.objects.filter(scenario=sc, course_key=key, section="S2").count() == 1


def test_a_section_still_placed_elsewhere_in_the_scenario_keeps_its_rows():
    sc, board, other, key = _workspace()
    term_section = _create(board, key, "S2", PATTERN_A)["term_section"]
    # Still on `board`; the same label is created again on the other board.

    _create(other, key, "S2", PATTERN_B)

    assert SectionPlacement.objects.filter(term_section=term_section, board=board).count() == 3
    assert _meeting_rows(term_section) == _rows(PATTERN_A + PATTERN_B)


def test_one_teacher_mode_rows_are_replaced_and_the_teacher_is_named_again(settings):
    # A planned section's rows are the workspace's own: the teacher name on them
    # is derived, and the create writes it back. So a name on the old rows must
    # not keep them (in production one-teacher is on and every linked course
    # carries a name).
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    sc, board, _other, key = _workspace()
    link("AI", "AI401", instructor("Dr A"))
    term_section = _create(board, key, "S2", PATTERN_A)["term_section"]
    _remove_from_board(term_section)
    old = TermSectionMeeting.objects.filter(term_section=term_section)
    assert {row.instructor for row in old} == {"Dr A"}  # the rows a name would have kept

    _create(board, key, "S2", PATTERN_B)

    assert _meeting_rows(term_section) == _rows(PATTERN_B)
    assert set(
        TermSectionMeeting.objects.filter(term_section=term_section).values_list(
            "instructor", flat=True
        )
    ) == {"Dr A"}
    assert engine_rows(sc)[(key, "S2")] == "Dr A"


def test_a_section_the_workspace_did_not_plan_keeps_its_rows():
    sc, board, _other, key = _workspace()
    existing = section(sc, key, "S2")  # source_tag "tw_auto": not a planned section
    for day, start, end in PATTERN_A:
        TermSectionMeeting.objects.create(
            term_section=existing, day=day, start_time=start, end_time=end
        )
    assert existing.source_tag != PLANNED_SECTION_SOURCE_TAG

    _create(board, key, "S2", PATTERN_B)

    assert _meeting_rows(existing) == _rows(PATTERN_A + PATTERN_B)


def test_a_newly_created_section_is_not_swept():
    sc, board, _other, key = _workspace()

    with CaptureQueriesContext(connection) as queries:
        result = _create(board, key, "S2", PATTERN_A)

    deletes = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith("DELETE") and "term_section_meetings" in q["sql"]
    ]
    assert deletes == []
    assert _meeting_rows(result["term_section"]) == _rows(PATTERN_A)
