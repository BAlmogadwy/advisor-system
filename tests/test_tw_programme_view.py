"""6b: programme roles shared with the per-plan export (§7.4, sub-PR 6b).

Which placements a programme's per-plan file shows, and as what, is one rule
in ``timetable_programme_view``: the export draws its workbooks from
``programme_placement_roles``, and ``quality/?lens=1`` answers the same roles
for every board programme as the lens's ``programme_view`` (contract B).

PV1 holds the export's workbooks to an oracle recorded before the move
(``tests/fixtures/per_plan_export_digests.json``): the move changed no
workbook. PV2-PV9 pin the roles, the SHARED classification, the programme
set, an unassessed seating, the reads, one definition, and that the view
answers what the export's files hold.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services import timetable_per_plan_export as per_plan
from core.services import timetable_plan_lens as plan_lens
from core.services import timetable_programme_view as pview
from core.services.rbac import ROLE_SUPER_ADMIN
from tests.per_plan_export_digests_support import (
    SCENARIOS,
    SEARCHES,
    build,
    digests_of,
    first_difference,
    scenario_exports,
)
from tests.test_per_plan_export_grouping import _course, _row, _scenario
from tests.tw_scenario_seating_support import (
    apply_flags,
    build_seating_fixture,
    lens_url,
    quality_url,
    read,
)

DIGESTS = Path(__file__).resolve().parent / "fixtures" / "per_plan_export_digests.json"
LENS_LOGGER = "core.services.timetable_plan_lens"
VIEW_KEYS = ["programmes", "roles", "shared_roles", "placement_ids", "seating_assessed"]


@pytest.fixture
def http():
    user = User.objects.create_user(username="programme-view-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def _lens(rows: dict) -> dict:
    """A lens with these section rows (``{section or id: row}``), assessed."""
    return {
        "sections": {str(getattr(key, "id", key)): row for key, row in rows.items()},
        "seating": {"assessed": True, "students": 0},
    }


def _shared(row: dict) -> dict:
    return {**row, "shared": True}


def _placement(pid: int, ts_id: int, code: str, label: str, board: int = 10) -> SectionPlacement:
    """An unsaved placement of section ``ts_id`` (course ``code``) on ``board``."""
    section = TermSection(
        id=ts_id, course_code=code, course_key=code, course_name=code, section=label
    )
    return SectionPlacement(
        id=pid,
        board_id=board,
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
    )


def _flat(by_role: dict[str, list[int]]) -> dict[int, str]:
    return {pid: role for role, ids in by_role.items() for pid in ids}


def _placement_of(section: TermSection) -> int:
    return SectionPlacement.objects.get(term_section=section).id


def _logs(caplog, logger: str) -> int:
    return sum(1 for r in caplog.records if r.name == logger and r.levelno >= logging.ERROR)


# ── PV1: the export's workbooks are unchanged ────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_pv1_every_export_workbook_equals_the_oracle(monkeypatch, settings, name) -> None:
    """Per scenario and search: the file names, and each sheet's digest of its
    values, fills, fonts, merges and widths, in order."""
    recorded = json.loads(DIGESTS.read_text(encoding="utf-8"))[name]
    exports = scenario_exports(build(name, monkeypatch, settings))
    now = {search: digests_of(lines) for search, lines in exports.items()}
    difference = first_difference(recorded, now, (name,))
    assert difference is None, (
        f"{difference}\nDump both trees with TW_PER_PLAN_DUMP (tests/"
        "record_per_plan_export_digests.py) and compare them with "
        "tests/per_plan_export_digests_support.py to see the first line."
    )


def test_pv1_the_oracle_holds_every_scenario_and_search() -> None:
    """Not vacuous: every scenario and search is recorded, the SHARED search
    changes what some files show, and some exports draw term sheets."""
    recorded = json.loads(DIGESTS.read_text(encoding="utf-8"))
    assert list(recorded) == list(SCENARIOS)
    assert all(list(by_search) == list(SEARCHES) for by_search in recorded.values())
    shared = recorded["extra/shared_roles"]
    assert shared[""]["files"]["AI2.xlsx"] != shared["SHARED"]["files"]["AI2.xlsx"]
    sheets = [
        title
        for by_search in recorded.values()
        for result in by_search.values()
        for sheets in result.get("files", {}).values()
        for title in sheets
    ]
    assert sum(title.startswith("Term ") for title in sheets) > 80


def test_pv1_a_difference_is_named_where_it_is() -> None:
    """PV1 passes only when no digest differs: the comparison names the first
    file, sheet or key that does, and nothing when all are equal."""
    tree = {"": {"name": "a.zip", "files": {"AI2.xlsx": {"Term 2": "aa", "Term 3": "bb"}}}}
    same = json.loads(json.dumps(tree))
    assert first_difference(tree, same) is None
    changed = json.loads(json.dumps(tree))
    changed[""]["files"]["AI2.xlsx"]["Term 3"] = "cc"
    assert first_difference(tree, changed, ("x",)) == (
        "x / '' / 'files' / 'AI2.xlsx' / 'Term 3': recorded 'bb', now 'cc'"
    )
    missing = json.loads(json.dumps(tree))
    del missing[""]["files"]["AI2.xlsx"]["Term 2"]
    assert "keys ['Term 2', 'Term 3'] != ['Term 3']" in first_difference(tree, missing)
    assert first_difference({"error": "no programme"}, tree[""]) is not None


# ── PV2: each role, from the lens's seating ─────────────────────────────────


def test_pv2_each_role_comes_from_who_sits_in_the_section() -> None:
    plan_map = {code: 2 for code in ("AI236", "CS112", "ENGL214", "IS124")}
    placements = [
        _placement(1, 101, "AI236", "S1"),  # marked AI2: main whatever its share
        _placement(2, 102, "AI236", "S2"),
        _placement(3, 103, "CS112", "S1"),  # 7 of 10: main
        _placement(4, 104, "CS112", "S2"),
        _placement(5, 105, "ENGL214", "S1"),  # 69 of 100, nothing main: mixed
        _placement(6, 106, "ENGL214", "S2"),  # no AI2 student: not in the file
        _placement(7, 107, "IS124", "S1"),  # allocated to AI2, seats no AI2
        _placement(8, 108, "IS124", "S2"),
        _placement(9, 109, "MATH101", "M1"),  # out of plan, allocated to AI2
        _placement(10, 110, "MATH102", "M1"),  # out of plan, unallocated
        _placement(11, 111, "MATH103", "M1"),  # out of plan, no lens row
        _placement(12, 112, "MATH104", "M1"),  # out of plan, DS2's
    ]
    lens = _lens(
        {
            101: _row(mark="AI2", AI2=4, DS2=8),
            102: _row(AI2=5, DS2=5),
            103: _row(AI2=7, DS2=3),
            104: _row(AI2=2, DS2=28),
            105: _row(AI2=69, DS2=31),
            106: _row(DS2=10),
            107: _row(plans=("AI2",), DS2=25),
            108: _row(AI2=24),
            109: _row(plans=("AI2",), AI2=3),
            110: _row(),
            112: _row(plans=("DS2",), AI2=1, DS2=20),
        }
    )

    out = pview.programme_placement_roles(
        "AI2",
        placements,
        plan_map=plan_map,
        elective_terms={},
        board_terms={10: 2},
        plan_lens=lens,
    )

    assert out.roles == {
        1: "main",
        2: "other",
        3: "main",
        4: "other",
        5: "mixed",
        8: "main",
        9: "outside",
        10: "outside",
        11: "outside",
    }
    # The seats behind them, per plan-term sheet (the export's cell notes).
    assert out.seats[2][105] == pview.SectionSeat("mixed", 69, 100)
    assert set(out.seats) == {2}


def test_pv2_a_course_nobody_of_the_programme_sits_in_keeps_its_allocated_sections() -> None:
    placements = [_placement(1, 101, "CS112", "S1"), _placement(2, 102, "CS112", "S2")]
    lens = _lens({101: _row(plans=("AI2",)), 102: _row(plans=("DS2",), DS2=25)})

    out = pview.programme_placement_roles(
        "AI2",
        placements,
        plan_map={"CS112": 2},
        elective_terms={},
        board_terms={10: 2},
        plan_lens=lens,
    )

    assert out.roles == {1: "main"}


def test_pv2_role_lists_are_in_the_contracts_order_ascending_and_never_empty() -> None:
    lists = pview._role_lists({5: "other", 9: "main", 2: "main", 1: "outside", 3: "main"})

    assert lists == {"main": [2, 3, 9], "other": [5], "outside": [1]}
    assert list(lists) == ["main", "other", "outside"]  # no empty "mixed"
    assert pview.ROLE_ORDER == ("main", "mixed", "other", "outside")
    assert pview._role_lists({}) == {}


# ── PV3: an elective on two slot terms takes each sheet's role ──────────────


def test_pv3_each_placement_takes_its_own_sheets_role() -> None:
    """DS482 fills DS1 (T8) and DS3 (T10). Section X meets on both boards:
    alone on the T8 sheet it is the section seating most DS (mixed); on the
    T10 sheet it stands beside S2, all DS (main), so it is other there."""
    placements = [
        _placement(1, 201, "DS482", "S9", board=8),
        _placement(2, 201, "DS482", "S9", board=10),
        _placement(3, 202, "DS482", "S2", board=10),
    ]
    lens = _lens({201: _row(DS=6, AI=4), 202: _row(DS=25)})

    out = pview.programme_placement_roles(
        "DS",
        placements,
        plan_map={"DS482": 8},
        elective_terms={"DS482": {8, 10}},
        board_terms={8: 8, 10: 10},
        plan_lens=lens,
    )

    assert out.roles == {1: "mixed", 2: "other", 3: "main"}
    assert out.seats[8] == {201: pview.SectionSeat("mixed", 6, 10)}
    assert out.seats[10][201] == pview.SectionSeat("other", 6, 10)


# ── PV4: SHARED is the export's classification, never an intersection ──────


def _shared_fixture():
    """CS112: S1 (not shared) seats 2 AI2; S2 (shared) seats no AI2 but the
    lens allocates it to AI2. ENGL214: S1 (not shared) 20 of 30 AI2, S2
    (shared) 5 of 25: no main, so S1 is mixed and S2 other, until SHARED
    leaves S2 alone and makes it mixed."""
    scenario, [board] = _scenario()
    cs = _course(board, "CS112", ("S1", "SUN", "09:00"), ("S2", "MON", "09:00"))
    engl = _course(board, "ENGL214", ("S1", "TUE", "09:00"), ("S2", "TUE", "10:30"))
    lens = _lens(
        {
            cs["S1"]: _row(AI2=2),
            cs["S2"]: _shared(_row(plans=("AI2",), DS2=20)),
            engl["S1"]: _row(AI2=20, DS2=10),
            engl["S2"]: _shared(_row(AI2=5, DS2=20)),
        }
    )
    ids = {
        f"{code} {label}": _placement_of(s)
        for code, made in (("CS112", cs), ("ENGL214", engl))
        for label, s in made.items()
    }
    return scenario, lens, ids


@pytest.mark.django_db
def test_pv4_shared_roles_are_classified_after_shared_narrows_the_sections() -> None:
    scenario, lens, ids = _shared_fixture()

    view = pview.programme_view(scenario, lens)

    assert view["roles"]["AI2"] == {
        "main": [ids["CS112 S1"]],
        "mixed": [ids["ENGL214 S1"]],
        "other": [ids["ENGL214 S2"]],
    }
    # SHARED alone: S2 is AI2's main section (nobody of AI2 sits in the
    # shared ones, so the allocation decides), and ENGL214 S2 turns mixed.
    assert view["shared_roles"]["AI2"] == {
        "main": [ids["CS112 S2"]],
        "mixed": [ids["ENGL214 S2"]],
    }
    assert ids["CS112 S2"] not in _flat(view["roles"]["AI2"])
    assert view["roles"]["DS2"] == {
        "main": sorted([ids["CS112 S2"], ids["ENGL214 S2"]]),
        "other": [ids["ENGL214 S1"]],
    }
    assert view["shared_roles"]["DS2"] == {"main": sorted([ids["CS112 S2"], ids["ENGL214 S2"]])}


@pytest.mark.django_db
def test_pv4_the_view_has_contract_bs_shape() -> None:
    """Keys in contract B's order, roles in main, mixed, other, outside order,
    ids ascending, empty lists left out; a section the lens gives nobody is
    outside the plan of a board programme that does not hold its course."""
    scenario, lens, ids = _shared_fixture()
    math = TermSection.objects.create(
        scenario=scenario,
        course_code="MATH101",
        course_number="MATH101",
        course_key="MATH101",
        course_name="MATH101",
        section="M1",
        source_tag="tw_auto",
    )
    board = DeliveryBoard.objects.get(scenario=scenario)
    outside = SectionPlacement.objects.create(
        board=board, term_section=math, day="WED", start_time="13:00", end_time="14:15"
    )
    lens["sections"][str(math.id)] = _row(plans=("AI2",), AI2=1)
    DeliveryBoard.objects.create(scenario=scenario, label="Spare", program="IS", display_order=9)

    view = pview.programme_view(scenario, lens)

    assert list(view) == VIEW_KEYS
    assert list(view["roles"]["AI2"]) == ["main", "mixed", "other", "outside"]
    assert view["roles"]["AI2"]["outside"] == [outside.id]
    # IS has a board but no plan rows: the sections the lens gives nobody are
    # outside its plan, and under SHARED only the shared one of them.
    assert view["roles"]["IS"] == {
        "outside": sorted([ids["CS112 S1"], ids["ENGL214 S1"], ids["ENGL214 S2"]])
    }
    assert view["shared_roles"]["IS"] == {"outside": [ids["ENGL214 S2"]]}
    assert view["placement_ids"] == sorted([*ids.values(), outside.id])
    assert view["seating_assessed"] is True
    two = sorted([ids["CS112 S2"], ids["ENGL214 S2"]])
    assert view["roles"]["DS2"]["main"] == two  # ascending, whatever the course


# ── PV5: the export's programmes, in board order ────────────────────────────


@pytest.mark.django_db
def test_pv5_programmes_are_the_board_programmes_in_board_order_upper_cased() -> None:
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="Programmes", programs=["CS", "AI2"]
    )
    for order, program, term in ((2, "DS2", 4), (0, "AI2, ds", 2), (1, "AI,AI2", 3), (3, "DS", 1)):
        DeliveryBoard.objects.create(
            scenario=scenario,
            label=f"Term {term}",
            nominal_term=term,
            program=program,
            display_order=order,
        )
    ProgrammeRequirement.objects.create(
        program="DS", course_code="DS101", course_name="DS101", programme_term=1
    )
    section = TermSection.objects.create(
        scenario=scenario,
        course_code="DS101",
        course_number="DS101",
        course_key="DS101",
        course_name="DS101",
        section="S1",
        source_tag="tw_auto",
    )
    placement = SectionPlacement.objects.create(
        board=DeliveryBoard.objects.get(scenario=scenario, program="DS"),
        term_section=section,
        day="SUN",
        start_time="09:00",
        end_time="10:15",
    )
    # A programme only the students name (the lens's demand plans) has no file.
    lens = {**_lens({section: _row()}), "plans": ["AI2", "IS"]}

    view = pview.programme_view(scenario, lens)

    assert per_plan.get_scenario_programs(scenario) == ["AI2", "ds", "AI", "DS2", "DS"]
    assert view["programmes"] == ["AI2", "DS", "AI", "DS2"]
    assert list(view["roles"]) == view["programmes"] == list(view["shared_roles"])
    # "DS" is read as the first board spelt it ("ds"): that file has no plan
    # rows, so DS101 is outside it, as the file lists it.
    assert view["roles"]["DS"] == {"outside": [placement.id]}


# ── PV6: an unassessed seating is classified by allocation and marks ───────


def _ds_only_ai236():
    """The PR #158 board with AI2's plan, and AI236 wanted by DS alone: the
    lens allocates both its sections to DS, and S2 is marked AI2."""
    from tests._section_programme_fixture import AI2_IDS, build_marked_scenario

    scenario, _board, placements = build_marked_scenario(mark="AI2")
    for code in ("AI236", "IS124"):
        ProgrammeRequirement.objects.create(
            program="AI2", course_code=code, course_name=code, programme_term=2
        )
    ScenarioStudentCourseRequest.objects.filter(
        scenario=scenario, course_key="AI236", student_id__in=AI2_IDS
    ).delete()
    return scenario, {f"{c} {s}": p.id for (c, s), p in placements.items()}


@pytest.mark.django_db
@pytest.mark.parametrize("mark_on", [True, False])
def test_pv6_a_seating_that_was_not_assessed_gives_roles_by_allocation_and_marks(
    monkeypatch, settings, mark_on
) -> None:
    settings.TIMETABLE_SECTION_PROGRAMME_PREFERENCE = mark_on
    scenario, ids = _ds_only_ai236()

    def boom(*_args, **_kwargs):
        raise ValueError("the sections cannot be built")

    monkeypatch.setattr(plan_lens, "build_section_states_for_scenario", boom)

    lens = plan_lens.build_scenario_plan_lens(scenario.id, programme_view=True)
    view = lens["programme_view"]

    assert lens["seating"]["assessed"] is False
    assert view["seating_assessed"] is False
    # Nobody seated anywhere: AI2's sections are the ones the lens allocates
    # to it (IS124, shared by AI2 and DS) and, under the flag, the one marked
    # for it (AI236 S2); AI236 S1 is DS's.
    expected = sorted([ids["IS124 S1"], *([ids["AI236 S2"]] if mark_on else [])])
    assert view["roles"]["AI2"] == {"main": expected}


@pytest.mark.django_db
def test_pv6_an_assessed_seating_says_so() -> None:
    scenario, ids = _ds_only_ai236()

    view = plan_lens.build_scenario_plan_lens(scenario.id, programme_view=True)["programme_view"]

    assert view["seating_assessed"] is True
    assert ids["AI236 S1"] not in _flat(view["roles"]["AI2"])


# ── PV7: the reads the view adds ────────────────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize("programmes", ["AI2", "AI2,DS", "AI2,DS,IS"])
def test_pv7_the_view_adds_five_reads_and_two_per_programme(settings, http, programmes):
    """On top of 6a's ``?lens=1`` (quality plus four lens reads): the scenario,
    boards, placements, budgets and sections once, then each programme's plan
    rows and elective mappings, however many placements there are."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    DeliveryBoard.objects.filter(pk=fx.board.pk).update(program=programmes)
    count = len(programmes.split(","))
    _, plain = read(http, quality_url(fx))
    body, both = read(http, quality_url(fx, "?lens=1"))
    assert len(body["plan_lens"]["programme_view"]["programmes"]) == count
    assert len(body["plan_lens"]["programme_view"]["placement_ids"]) == 5
    assert len(both) == len(plain) + 4 + 5 + 2 * count
    view_reads = [q["sql"].split(" FROM ")[1].split()[0] for q in both[len(plain) + 4 :]]
    assert view_reads == [
        '"timetable_scenarios"',
        '"delivery_boards"',
        '"section_placements"',
        '"scenario_section_budgets"',
        '"term_sections"',
        *['"programme_requirements"', '"elective_term_mappings"'] * count,
    ]


# ── PV8: one definition, called once per file ───────────────────────────────


def test_pv8_the_roles_are_defined_only_in_the_programme_view_module() -> None:
    source = inspect.getsource(per_plan)
    for name in (
        "classify_course_sections",
        "programme_placement_roles",
        "_lens_seating",
        "_section_passes_search",
        "_section_allocated_to_plan",
        "_section_matches_plan_filter",
        "get_plan_term_map",
        "get_plan_elective_terms",
        "_placement_plan_term",
        "_classify_placement_term",
        "get_scenario_programs",
        "programme_context",
        "scenario_course_keys",
    ):
        assert f"def {name}(" not in source, name
        assert getattr(per_plan, name) is getattr(pview, name), name
    assert "class SectionSeat" not in source and per_plan.SectionSeat is pview.SectionSeat
    assert "MAIN_SECTION_SHARE = " not in source
    assert per_plan.MAIN_SECTION_SHARE is pview.MAIN_SECTION_SHARE


@pytest.mark.django_db
@pytest.mark.parametrize("search, shared", [("", False), ("SHARED", True)])
def test_pv8_the_export_reads_each_file_from_the_roles_function_once(
    monkeypatch, settings, search, shared
) -> None:
    scenario_id = build("extra/shared_roles", monkeypatch, settings)
    calls: list[tuple[str, bool]] = []
    real = per_plan.programme_placement_roles

    def spy(program, placements, **kwargs):
        calls.append((program, kwargs["shared_only"]))
        return real(program, placements, **kwargs)

    monkeypatch.setattr(per_plan, "programme_placement_roles", spy)
    path, _name, is_zip = per_plan.export_scenario_per_plan(scenario_id, search=search)
    Path(path).unlink(missing_ok=True)

    assert is_zip
    assert calls == [("AI2", shared), ("DS2", shared)]


# ── PV9: the view answers what the export's files hold ──────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name",
    [
        "extra/shared_roles",
        "extra/elective_two_sheets",
        "grouping/cs112_two_boards",
        "electives/later_slot_board",
        "electives/board_none_of_slots",
    ],
)
def test_pv9_each_programmes_roles_are_its_files(monkeypatch, settings, name) -> None:
    """The roles the export drew each file from (with and without SHARED)
    are the view's, for the same lens: the view hands the roles function the
    export's plan terms, electives, board terms and lens."""
    scenario_id = build(name, monkeypatch, settings)
    drawn: dict[tuple[str, bool], dict[int, str]] = {}
    real = per_plan.programme_placement_roles

    def spy(program, placements, **kwargs):
        out = real(program, placements, **kwargs)
        drawn[(program.upper(), kwargs["shared_only"])] = out.roles
        return out

    monkeypatch.setattr(per_plan, "programme_placement_roles", spy)
    for search in ("", "SHARED"):
        path, _name, _zip = per_plan.export_scenario_per_plan(scenario_id, search=search)
        Path(path).unlink(missing_ok=True)
    lens = per_plan.build_scenario_plan_lens(scenario_id)  # the export's, seated by the fixture
    view = pview.programme_view(TimetableScenario.objects.get(id=scenario_id), lens)

    assert drawn, "the export drew nothing"
    assert {(p, False) for p in view["programmes"]} | {
        (p, True) for p in view["programmes"]
    } == set(drawn)
    for programme in view["programmes"]:
        assert _flat(view["roles"][programme]) == drawn[(programme, False)], programme
        assert _flat(view["shared_roles"][programme]) == drawn[(programme, True)], programme


# ── the lens carries the view only on quality/?lens=1 ────────────────────────


@pytest.mark.django_db
def test_only_the_quality_read_with_the_lens_carries_the_view(settings, http) -> None:
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    alone, _ = read(http, lens_url(fx))
    both, _ = read(http, quality_url(fx, "?lens=1"))

    assert "programme_view" not in alone["plan_lens"]
    assert "programme_view" not in plan_lens.build_scenario_plan_lens(fx.scenario.pk)
    assert list(both["plan_lens"])[-2:] == ["seating", "programme_view"]
    view = both["plan_lens"]["programme_view"]
    assert list(view) == VIEW_KEYS
    assert view["programmes"] == ["AI2"]  # the board's; scenario.programs adds DS
    assert view["placement_ids"] == sorted(p.pk for p in fx.placements.values())


@pytest.mark.django_db
def test_a_view_that_cannot_be_built_is_null_and_costs_nothing_else(
    settings, http, monkeypatch, caplog
) -> None:
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    plain, _ = read(http, quality_url(fx))
    alone, _ = read(http, lens_url(fx))

    def boom(*_args, **_kwargs):
        raise ValueError("no view")

    monkeypatch.setattr(pview, "programme_view", boom)
    caplog.set_level(logging.ERROR)
    caplog.clear()
    both, _ = read(http, quality_url(fx, "?lens=1"))  # 200, or ``read`` fails

    lens = both.pop("plan_lens")
    assert list(lens)[-1] == "programme_view" and lens.pop("programme_view") is None
    assert json.dumps(lens) == json.dumps(alone["plan_lens"])
    assert json.dumps(both) == json.dumps(plain)
    assert _logs(caplog, LENS_LOGGER) == 1


# ── the module stands alone ──────────────────────────────────────────────────


def test_the_view_module_imports_neither_or_tools_nor_the_export() -> None:
    """Django set up, nothing timetable-shaped loaded yet: importing the view
    loads the view alone, never OR-Tools, the export or the lens."""
    snippet = (
        "import json, sys, django\n"
        "django.setup()\n"
        "before = set(sys.modules)\n"
        "import core.services.timetable_programme_view\n"
        "print(json.dumps(sorted(m for m in set(sys.modules) - before"
        " if m.split('.')[0] == 'ortools' or m.startswith('core.services.timetable_'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings"},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert json.loads(done.stdout) == ["core.services.timetable_programme_view"]
