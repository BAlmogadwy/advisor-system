"""Planned sections in the Timetable Workspace: label bookkeeping and re-creation.

Two server facts behind the "required sections" list:

* ``compute_scenario_budget`` names, for every course, the section labels in use
  across the whole scenario, in natural order, so the page can propose a free
  label instead of ``S{used + 1}`` (S1 and S3 used: S2 is free, then S4).
* ``create_planned_section_placements`` reuses a section that was created
  before. Taking a section off the board leaves its meeting rows, so a section
  created with pattern A, removed, and created again with pattern B used to hold
  A's rows and B's. The leftovers go, but only for a section the workspace made
  (``tw_planned``, or ``tw_auto`` from the engines) that this scenario owns and
  that is on no board of any scenario. A registrar's, imported or scraped
  section, a section owned elsewhere, or one still placed, keeps its rows.
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
    TimetableScenario,
)
from core.services import timetable_workspace as tws
from core.services.section_source_tags import (
    AUTO_SECTION_SOURCE_TAG,
    PLANNED_SECTION_SOURCE_TAG,
    WORKSPACE_SECTION_SOURCE_TAGS,
)
from core.services.timetable_workspace import (
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


def test_a_section_placed_by_another_scenarios_board_keeps_its_rows():
    # Meeting rows belong to the section, not to a scenario: another scenario's
    # board still shows this section, so the rows stay.
    sc, board, _other, key = _workspace()
    term_section = _create(board, key, "S2", PATTERN_A)["term_section"]
    _remove_from_board(term_section)
    _sc2, board2 = scenario(name="somewhere else")
    SectionPlacement.objects.create(
        board=board2,
        term_section=term_section,
        day="SUN",
        start_time="08:00",
        end_time="09:15",
        room="R1",
    )

    _create(board, key, "S2", PATTERN_B)

    assert _meeting_rows(term_section) == _rows(PATTERN_A + PATTERN_B)


@pytest.mark.parametrize("tag", ["tw_auto", "tw_scheduler"])
def test_an_engine_made_section_with_no_placement_is_replaced(tag):
    sc, board, _other, key = _workspace()
    engine_made = section(sc, key, "S2")  # what autoplace and the solver write: tw_auto
    assert engine_made.source_tag == AUTO_SECTION_SOURCE_TAG == "tw_auto"
    engine_made.source_tag = tag  # or the scheduler's
    engine_made.save(update_fields=["source_tag"])
    for day, start, end in PATTERN_A:
        TermSectionMeeting.objects.create(
            term_section=engine_made, day=day, start_time=start, end_time=end
        )

    result = _create(board, key, "S2", PATTERN_B)

    assert result["term_section"].id == engine_made.id  # reused, not created
    assert _meeting_rows(engine_made) == _rows(PATTERN_B)


@pytest.mark.parametrize(
    "tag", ["other", "department", "external", "scraper_timetable", "f_optimised_plan"]
)
def test_a_section_the_registrar_or_an_import_made_keeps_its_rows(tag):
    # The tags the section imports, the scrape and the F-plan import write.
    assert tag not in WORKSPACE_SECTION_SOURCE_TAGS
    sc, board, _other, key = _workspace()
    existing = TermSection.objects.create(
        scenario=sc,
        course_key=key,
        course_code="AI401",
        course_number="AI401",
        course_name="PLANNED COURSE",
        section="S2",
        source_tag=tag,
    )
    for day, start, end in PATTERN_A:
        TermSectionMeeting.objects.create(
            term_section=existing, day=day, start_time=start, end_time=end, instructor="Registrar"
        )

    result = _create(board, key, "S2", PATTERN_B)

    assert result["term_section"].id == existing.id  # reused, not created
    assert _meeting_rows(existing) == _rows(PATTERN_A + PATTERN_B)


def test_a_section_owned_by_another_scenario_or_by_none_is_never_touched():
    # Not reachable through the create (its get_or_create is scoped to the
    # scenario), so the guard is tested on the helper: a workspace-made tag and
    # no placement are not enough without ownership.
    sc, _board, _other, _key = _workspace()
    other_sc, _other_board = scenario(name="owner elsewhere")
    cases = [(other_sc, "AI501"), (None, "AI502"), (sc, "AI503")]
    sections = {}
    for owner, code in cases:
        sections[code] = TermSection.objects.create(
            scenario=owner,
            course_key=code,
            course_code=code,
            course_number=code,
            course_name=code,
            section="S2",
            source_tag=PLANNED_SECTION_SOURCE_TAG,
        )
        TermSectionMeeting.objects.create(
            term_section=sections[code], day="SUN", start_time="08:00", end_time="09:15"
        )

    for ts in sections.values():
        tws._drop_stale_workspace_meetings(ts, sc.id)

    assert _meeting_rows(sections["AI501"]) == [("SUN", "08:00", "09:15")]  # another scenario's
    assert _meeting_rows(sections["AI502"]) == [("SUN", "08:00", "09:15")]  # nobody's
    assert _meeting_rows(sections["AI503"]) == []  # ours: swept (the control)


def test_an_english_policy_section_is_recreated_with_its_four_rows():
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(scenario=sc, label="English", nominal_term=1, program="AI")
    days = ("SUN", "MON", "WED", "THU")

    def english(start, end):
        return create_planned_section_placements(
            board.id,
            course_code="ENG101",
            course_key="ENG101",
            course_name="English",
            section_label="M2",
            capacity=25,
            meetings=[{"day": day, "start_time": start, "end_time": end} for day in days],
        )

    term_section = english("09:00", "12:00")["term_section"]
    _remove_from_board(term_section)
    assert _meeting_rows(term_section) == [(day, "09:00", "12:00") for day in sorted(days)]
    old_ids = set(
        TermSectionMeeting.objects.filter(term_section=term_section).values_list("id", flat=True)
    )

    again = english("13:00", "16:00")

    assert again["term_section"].id == term_section.id
    assert len(again["placements"]) == 4
    assert _meeting_rows(term_section) == [(day, "13:00", "16:00") for day in sorted(days)]
    # Replaced, not repointed in place: none of the old rows survives.
    new_ids = set(
        TermSectionMeeting.objects.filter(term_section=term_section).values_list("id", flat=True)
    )
    assert len(new_ids) == 4 and not (new_ids & old_ids)


def test_a_failure_after_the_drop_restores_the_old_rows(monkeypatch):
    sc, board, _other, key = _workspace()
    term_section = _create(board, key, "S2", PATTERN_A)["term_section"]
    _remove_from_board(term_section)
    seen = []

    def explode(**_kwargs):
        seen.append(_meeting_rows(term_section))
        raise RuntimeError("validation failed after the old rows were dropped")

    monkeypatch.setattr(tws, "validate_placement", explode)

    with pytest.raises(RuntimeError):
        _create(board, key, "S2", PATTERN_B)

    # At the failure A's rows were already gone and B's first row was written ...
    assert seen == [[("MON", "11:00", "12:15")]]
    # ... and the whole create rolled back: A's rows are back, nothing of B's.
    assert _meeting_rows(term_section) == _rows(PATTERN_A)
    assert not SectionPlacement.objects.filter(term_section=term_section).exists()


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


def test_natural_key_is_one_function_whichever_module_it_is_taken_from():
    from core.services.exam_rosters import natural_key as from_exams
    from core.services.natural_order import natural_key

    assert from_exams is natural_key
    assert sorted(["M10", "M2", "PHYS103 (2)", "PHYS103 (1)", "m1"], key=natural_key) == [
        "m1",
        "M2",
        "M10",
        "PHYS103 (1)",
        "PHYS103 (2)",
    ]


def test_the_scheduler_keeps_the_tag_the_workspace_names_for_it():
    # The clean-room scheduler imports nothing of the workspace: its bridge keeps
    # its own value, and this keeps the two the same.
    from core.services.section_source_tags import (
        SCHEDULER_SECTION_SOURCE_TAG,
        WORKSPACE_SECTION_SOURCE_TAGS,
    )
    from scheduler import bridge

    assert bridge.SOURCE_TAG == SCHEDULER_SECTION_SOURCE_TAG
    assert SCHEDULER_SECTION_SOURCE_TAG in WORKSPACE_SECTION_SOURCE_TAGS
