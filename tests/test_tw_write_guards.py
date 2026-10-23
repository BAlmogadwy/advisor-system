"""Stage 3, B1: the server's write guards on the Timetable Workspace.

Two rules the page enforced alone, or not at all:

* **One section per label in the scenario** (D-3). A planned create
  (``create-planned``) of a label already placed on any board of the scenario
  is refused with 409 ``SECTION_LABEL_IN_USE`` and ``{course_code, label,
  next_free}``, whichever section holds the label (the scenario's own or a
  global one). A label whose placements were all removed is free again: Redo
  of an undone create reads it back. Placing an existing section
  (``create``) may put the section itself on another board or back where it
  was, never beside another section of its course under the same label.
  ``next_free`` follows the page's own rule, pinned by
  ``tests/fixtures/section_label_cases.json`` (also run in jsdom).
* **No removal of an externally owned placement** (D-17). A section another
  scenario also places is owned there: Remove answers 409
  ``PLACEMENT_EXTERNALLY_OWNED``, override or not, before the lock check.

Every view test runs twice: with the defaults, and under the three flags
production runs with (render.yaml). The PostgreSQL race is in
``test_tw_write_guards_postgres.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_section_policy import create_policy_section_bundle
from core.services.timetable_workspace import (
    SectionLabelInUse,
    create_planned_section_placements,
    first_free_section_labels,
    placed_section_labels,
)

pytestmark = pytest.mark.django_db

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "section_label_cases.json").read_text(encoding="utf-8")
)["cases"]

# The flags production runs with (render.yaml).
PRODUCTION_FLAGS = {
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
    "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": True,
    "TIMETABLE_SECTION_PROGRAMME_PREFERENCE": True,
}


@pytest.fixture(params=["defaults", "production flags"], autouse=True)
def flags(request, settings):
    if request.param == "production flags":
        for name, value in PRODUCTION_FLAGS.items():
            setattr(settings, name, value)
    return request.param


@pytest.fixture
def http():
    ensure_role_groups()
    user = User.objects.create_user(username="write-guard-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def ws():
    """A scenario with two boards and one 3-credit course (one meeting a week)."""
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"], name="Write guards"
    )
    term2 = DeliveryBoard.objects.create(scenario=sc, label="Term 2", nominal_term=2, program="AI")
    term4 = DeliveryBoard.objects.create(
        scenario=sc, label="Term 4", nominal_term=4, program="AI", display_order=2
    )
    ScenarioSectionBudget.objects.create(
        scenario=sc,
        course_key="AI201",
        course_code="AI201",
        course_name="Data Structures",
        department="AI",
        credit_hours=3,
        planned_sections=5,
        max_per_section=40,
        total_demand=120,
        programme_term=2,
    )
    return sc, term2, term4


def post(http, path, body):
    return http.post(path, json.dumps(body), content_type="application/json")


def create(http, board, label, day="SUN", start="09:00", end="10:15", code="AI201"):
    return post(
        http,
        "/ops/tw/placements/create-planned/",
        {
            "board_id": board.id,
            "course_code": code,
            "course_key": code,
            "course_name": "Data Structures",
            "section_label": label,
            "day": day,
            "start_time": start,
            "end_time": end,
        },
    )


def remove(http, placement_id, **body):
    return post(http, f"/ops/tw/placements/{placement_id}/remove/", body)


def written():
    """Every row a create or a remove could touch, in full."""
    return (
        list(TermSection.objects.order_by("pk").values()),
        list(SectionPlacement.objects.order_by("pk").values()),
        list(TermSectionMeeting.objects.order_by("pk").values()),
    )


def global_section(label, *, code="AI201", program="AI"):
    """A registrar section: no scenario, linked to the board's programme."""
    ts = TermSection.objects.create(
        scenario=None,
        course_key=code,
        course_code=code,
        course_number=code,
        course_name="Data Structures",
        section=label,
        source_tag="other",
    )
    TermSectionProgram.objects.create(term_section=ts, program=program)
    return ts


def placed_at(board, ts, day="SUN", start="09:00", end="10:15", *, locked=False):
    return SectionPlacement.objects.create(
        board=board,
        term_section=ts,
        day=day,
        start_time=start,
        end_time=end,
        room="R1",
        is_locked=locked,
    )


def refusal(response):
    assert response.status_code == 409, response.content
    return response.json()["error"]


# ── The free-label rule, one table for the server and the page ────────────


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_free_label_rule_follows_the_shared_case_table(case):
    assert first_free_section_labels(case["used"], case["count"]) == case["free"]


def test_the_case_table_covers_the_rule():
    # The table itself must keep the shapes the rule is about, or a trimmed
    # table would still pass both implementations.
    names = {case["name"] for case in CASES}
    assert {
        "a gap is filled first",
        "trimmed and upper-cased",
        "other letters take no S-number",
        "S10 in natural order",
        "none wanted",
    } <= names


# ── A planned create: one section per label in the scenario ─────────────────


def test_a_label_placed_on_another_board_is_refused_and_nothing_is_written(http, ws):
    _sc, term2, term4 = ws
    assert create(http, term2, "S1").status_code == 201
    before = written()

    error = refusal(create(http, term4, "S1", day="MON"))

    assert error["code"] == "SECTION_LABEL_IN_USE"
    assert error["details"] == {"course_code": "AI201", "label": "S1", "next_free": "S2"}
    assert written() == before


def test_the_same_board_on_another_day_is_refused(http, ws):
    _sc, term2, _term4 = ws
    assert create(http, term2, "S1").status_code == 201
    before = written()

    error = refusal(create(http, term2, "S1", day="TUE", start="13:00", end="14:15"))

    assert error["code"] == "SECTION_LABEL_IN_USE"
    assert written() == before


def test_a_label_whose_placements_were_all_removed_is_free_again(http, ws):
    # The Redo of an undone create (page-timetable-workspace.js applyHistoryAction,
    # section_create): the section's placements are gone, the section is not.
    _sc, term2, term4 = ws
    made = create(http, term2, "S1").json()
    assert remove(http, made["placement"]["id"]).status_code == 200
    section = TermSection.objects.get(course_key="AI201", section="S1")
    assert not SectionPlacement.objects.filter(term_section=section).exists()

    again = create(http, term4, "S1", day="WED")

    assert again.status_code == 201, again.content
    assert again.json()["placement"]["term_section_id"] == section.id  # reused
    assert TermSection.objects.filter(course_key="AI201", section="S1").count() == 1


def test_a_placed_global_section_holds_its_label(http, ws):
    _sc, term2, term4 = ws
    placed_at(term2, global_section("S1"))
    before = written()

    error = refusal(create(http, term4, "S1", day="MON"))

    assert (error["code"], error["details"]["next_free"]) == ("SECTION_LABEL_IN_USE", "S2")
    assert written() == before  # no scenario-owned S1 left behind either
    assert create(http, term4, "S2", day="MON").status_code == 201  # the control


def test_next_free_is_the_first_label_no_placed_section_holds(http, ws):
    _sc, term2, term4 = ws
    for label, day in (("S1", "SUN"), ("S2", "MON"), ("S4", "TUE")):
        assert create(http, term2, label, day=day).status_code == 201
    before = written()

    error = refusal(create(http, term4, "S2", day="WED"))

    assert error["details"] == {"course_code": "AI201", "label": "S2", "next_free": "S3"}
    assert written() == before


def test_labels_and_course_keys_are_compared_trimmed_and_upper_cased(http, ws):
    sc, term2, term4 = ws
    odd = TermSection.objects.create(
        scenario=sc,
        course_key=" ai201 ",
        course_code="AI201",
        course_number="AI201",
        course_name="Data Structures",
        section=" s1 ",
        source_tag="other",
    )
    placed_at(term2, odd)
    # A section with no course key is its course code's (as the budget counts it).
    keyless = TermSection.objects.create(
        scenario=sc, course_key="", course_code="AI201", section="S2", source_tag="other"
    )
    placed_at(term2, keyless, day="MON")
    assert placed_section_labels(sc.id, "AI201") == {"S1": {odd.id}, "S2": {keyless.id}}
    before = written()

    error = refusal(create(http, term4, "S1", day="TUE"))

    assert error["code"] == "SECTION_LABEL_IN_USE"
    assert error["details"]["next_free"] == "S3"
    # Neither the odd " s1 " nor the keyless S2 is the exact row the create
    # looks up, so each refused create made a section first: none is left.
    assert written() == before
    assert refusal(create(http, term4, "s2 ", day="TUE"))["details"]["label"] == "S2"
    assert written() == before


def test_a_label_of_another_course_or_another_scenario_is_free(http, ws):
    sc, term2, term4 = ws
    # Another course of this scenario holds S1 ...
    other_course = TermSection.objects.create(
        scenario=sc, course_key="AI202", course_code="AI202", section="S1", source_tag="tw_auto"
    )
    placed_at(term2, other_course)
    # ... and another scenario places this course's S1.
    elsewhere = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"], name="Elsewhere"
    )
    elsewhere_board = DeliveryBoard.objects.create(scenario=elsewhere, label="Term 2", program="AI")
    theirs = TermSection.objects.create(
        scenario=elsewhere, course_key="AI201", course_code="AI201", section="S1"
    )
    placed_at(elsewhere_board, theirs)

    made = create(http, term4, "S1", day="MON")

    assert made.status_code == 201, made.content


def test_the_service_raises_a_value_error_naming_the_free_label(ws):
    _sc, term2, term4 = ws

    def make(board, day):
        return create_planned_section_placements(
            board.id,
            course_code="AI201",
            course_key="AI201",
            course_name="Data Structures",
            section_label="s1",
            capacity=30,
            meetings=[{"day": day, "start_time": "09:00", "end_time": "10:15"}],
        )

    make(term2, "SUN")
    before = written()
    with pytest.raises(SectionLabelInUse) as caught:
        make(term4, "MON")

    assert written() == before
    assert isinstance(caught.value, ValueError)
    assert (caught.value.label, caught.value.next_free) == ("S1", "S2")
    assert str(caught.value) == (
        "AI201 S1 is already placed in this scenario. The next free section is S2."
    )


def test_an_english_section_label_in_use_is_refused_whole(http):
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"], name="English"
    )
    first = DeliveryBoard.objects.create(
        scenario=sc, label="English 1", nominal_term=1, program="AI"
    )
    second = DeliveryBoard.objects.create(
        scenario=sc, label="English 2", nominal_term=2, program="AI"
    )
    days = ("SUN", "MON", "WED", "THU")

    def english(board, start, end):
        return post(
            http,
            "/ops/tw/placements/create-planned/",
            {
                "board_id": board.id,
                "course_code": "ENG101",
                "course_key": "ENG101",
                "course_name": "English",
                "section_label": "M2",
                "capacity": 25,
                "meetings": [{"day": day, "start_time": start, "end_time": end} for day in days],
            },
        )

    assert english(first, "09:00", "12:00").status_code == 201
    before = written()

    error = refusal(english(second, "13:00", "16:00"))

    assert error["code"] == "SECTION_LABEL_IN_USE"
    assert error["details"]["label"] == "M2"
    assert written() == before


# ── Placing an existing section: itself again, never a second one ──────────


def test_placing_another_section_under_a_placed_label_is_refused(http, ws):
    _sc, term2, term4 = ws
    assert create(http, term2, "S1").status_code == 201
    registrar = global_section("S1")
    before = written()

    response = post(
        http,
        "/ops/tw/placements/create/",
        {
            "board_id": term4.id,
            "term_section_id": registrar.id,
            "day": "MON",
            "start_time": "09:00",
            "end_time": "10:15",
        },
    )

    error = refusal(response)
    assert error["code"] == "SECTION_LABEL_IN_USE"
    assert error["details"] == {"course_code": "AI201", "label": "S1", "next_free": "S2"}
    assert written() == before


def test_placing_the_same_section_again_is_never_refused(http, ws):
    _sc, term2, term4 = ws
    made = create(http, term2, "S1").json()["placement"]

    def place(board, day):
        return post(
            http,
            "/ops/tw/placements/create/",
            {
                "board_id": board.id,
                "term_section_id": made["term_section_id"],
                "day": day,
                "start_time": "09:00",
                "end_time": "10:15",
            },
        )

    # On another board (a section shared by two boards) ...
    assert place(term4, "MON").status_code == 201
    # ... and back where it was, as Undo of a Remove puts it.
    assert remove(http, made["id"]).status_code == 200
    assert place(term2, "SUN").status_code == 201


# ── Remove: another scenario's section stays where it is ────────────────────


@pytest.fixture
def external(ws):
    """S1 placed here and by another scenario, which owns its time and room."""
    sc, term2, _term4 = ws
    section = TermSection.objects.create(
        scenario=sc, course_key="AI201", course_code="AI201", section="S1", source_tag="tw_auto"
    )
    owner = TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["AI"], name="Owner"
    )
    owner_board = DeliveryBoard.objects.create(scenario=owner, label="Owner", program="AI")
    return section, term2, owner_board


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize("body", [{}, {"override": True}], ids=["plain", "override"])
def test_removing_an_externally_owned_placement_is_refused(http, external, locked, body):
    section, board, owner_board = external
    mine = placed_at(board, section, locked=locked)
    placed_at(owner_board, section)
    before = written()

    error = refusal(remove(http, mine.id, **body))

    # Before the lock check: an override unlocks a lock, not another scenario's section.
    assert error["code"] == "PLACEMENT_EXTERNALLY_OWNED"
    assert written() == before


def test_an_english_bundle_owned_elsewhere_is_not_removed(http):
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"], name="English here"
    )
    board = DeliveryBoard.objects.create(scenario=sc, label="English", nominal_term=1, program="AI")
    section = TermSection.objects.create(
        scenario=sc, course_code="ENG101", course_key="ENG101", section="M1"
    )
    rows = create_policy_section_bundle(
        board, section, day="SUN", start_time="09:00", end_time="12:00"
    )
    owner = TimetableScenario.objects.create(
        academic_year="1448", term="1", gender="M", programs=["AI"], name="English owner"
    )
    owner_board = DeliveryBoard.objects.create(scenario=owner, label="Owner", program="AI")
    placed_at(owner_board, section, start="09:00", end="12:00")
    before = written()

    error = refusal(remove(http, rows[0].id, override=True))

    assert error["code"] == "PLACEMENT_EXTERNALLY_OWNED"
    assert written() == before


def test_a_section_this_scenario_alone_places_is_still_removed(http, external):
    section, board, _owner_board = external
    mine = placed_at(board, section)

    response = remove(http, mine.id)

    assert response.status_code == 200, response.content
    assert not SectionPlacement.objects.filter(pk=mine.pk).exists()


def test_a_published_scenario_still_answers_first(http, external):
    section, board, owner_board = external
    mine = placed_at(board, section)
    placed_at(owner_board, section)
    TimetableScenario.objects.filter(pk=board.scenario_id).update(status="published")
    before = written()

    response = remove(http, mine.id)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    assert written() == before
