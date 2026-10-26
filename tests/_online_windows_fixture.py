"""The online corpus and the recorded workloads for ``TIMETABLE_ONLINE_EVENING_WINDOWS``.

``build_online_corpus`` is ``test_one_teacher_flag_parity.build_corpus`` (two
boards, four programmes, linked lecturers, seeded demand) plus online courses:

- **GS101** (3 credits, two meetings) and **GSE102** (2 credits, one 100-minute
  meeting), online for AI and DS (the Term 4 board), sharing most of their
  students, so two online sections compete for the same windows;
- GS101 is taught by Dr Iman, who also teaches CS112 PROGRAMMING II on campus,
  and GSE102 by Dr Hani (AI201 for DS, DS210): the instructor rules meet the
  windows;
- **CYS300** is online for AI (Term 4, CYBER ETHICS) but on campus for AI2
  (Term 2, CYBER BASICS): one code, two planner identities, two deliveries.

The workloads below are recorded by ``tests/test_online_evening_windows.py``
(``-k record``) on the UNCHANGED engine (``17ef51d``) into
``tests/fixtures/online_evening_windows_flag_off_golden.json``. Never re-record
them from a branch that changes the planner: a golden that follows the code
proves nothing.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict

import pytest

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from tests.test_one_teacher_flag_parity import build_corpus, capture_board
from tests.test_section_day_spread import PRODUCTION_FLAGS

# (programme, code, name, programme term, credit hours, planned sections, online)
ONLINE_COURSES = (
    ("AI", "GS101", "ISLAMIC CULTURE", 4, 3, 2, True),
    ("DS", "GS101", "ISLAMIC CULTURE", 4, 3, 2, True),
    ("AI", "GSE102", "ARABIC WRITING", 4, 2, 2, True),
    ("DS", "GSE102", "ARABIC WRITING", 4, 2, 2, True),
    ("AI", "CYS300", "CYBER ETHICS", 4, 3, 1, True),
    ("AI2", "CYS300", "CYBER BASICS", 2, 3, 1, False),
)
ONLINE_LINKS = {
    ("AI", "GS101"): "Dr Iman",
    ("DS", "GS101"): "Dr Iman",
    ("AI", "GSE102"): "Dr Hani",
    ("DS", "GSE102"): "Dr Hani",
}
# The planner identities of the Term 4 courses that are online for that board.
ONLINE_KEYS = frozenset(
    planner_course_key(code, name)
    for _program, code, name, term, _credit, _planned, online in ONLINE_COURSES
    if online and term == 4
)

# The production planner flags (day spread is ON in production since #200).
ONLINE_FLAGS = {
    **PRODUCTION_FLAGS,
    "TIMETABLE_SECTION_DAY_SPREAD": True,
    "TIMETABLE_SECTION_DAY_SPREAD_WEIGHT": 40,
}

STRATEGIES = ("compact", "morning")
RUN_SEEDS = (3, 11, 29, None)  # None = the legacy unseeded call
GREEDY_CASES = [(strategy, seed) for strategy in STRATEGIES for seed in RUN_SEEDS]


def build_online_corpus(seed: int = 11) -> TimetableScenario:
    """``build_corpus(seed)`` plus the online courses above (seeded demand)."""
    scenario = build_corpus(seed)
    rng = random.Random(seed * 7 + 1)
    for program, code, name, term, credit, _planned, online in ONLINE_COURSES:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=code,
            course_name=name,
            programme_term=term,
            credit_hours=credit,
            is_online=online,
        )
    for (program, code), full_name in ONLINE_LINKS.items():
        CourseInstructor.objects.create(
            program=program,
            course_code=code,
            section="M",
            instructor=Instructor.objects.get(full_name=full_name),
        )
    first_id = 9_000_000 + seed * 1000
    demand: dict[str, int] = defaultdict(int)
    for program in ("DS", "AI", "AI2", "DS2"):
        students = Student.objects.filter(
            program=program, student_id__gt=first_id, student_id__lte=first_id + 999
        ).order_by("student_id")
        mine = [course for course in ONLINE_COURSES if course[0] == program]
        for student in students:
            for _program, code, name, _term, _credit, _planned, _online in mine:
                if rng.random() < 0.85:
                    key = planner_course_key(code, name)
                    demand[key] += 1
                    ScenarioStudentCourseRequest.objects.create(
                        scenario=scenario,
                        student_id=student.student_id,
                        course_key=key,
                        course_code=code,
                        course_name=name,
                    )
    for _program, code, name, term, credit, planned, _online in ONLINE_COURSES:
        key = planner_course_key(code, name)
        if ScenarioSectionBudget.objects.filter(scenario=scenario, course_key=key).exists():
            continue
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=key,
            course_code=code,
            course_name=name,
            department="AI",
            credit_hours=credit,
            planned_sections=planned,
            max_per_section=40,
            total_demand=demand[key],
            programme_term=term,
        )
    return scenario


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def run_greedy_case(builder, strategy: str, seed: int | None) -> dict:
    """A build, a lock, a rebuild (the rebuild meets every instructor filter)."""
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import reset_scenario
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = builder(seed or 11)
    with seeded_timetable_run(seed):
        first = auto_place_scenario(scenario.id, strategy=strategy)
        SectionPlacement.objects.filter(
            board__scenario=scenario,
            term_section__course_key=planner_course_key("CS114", "ALGORITHMS & DATA STRUCTURES"),
            term_section__section="S1",
        ).update(is_locked=True)
        reset_scenario(scenario.id, keep_locked=True)
        placed = auto_place_scenario(scenario.id, strategy=strategy)
    board = capture_board(scenario)
    return _plain(
        {
            "first_placed": first.get("total_placed"),
            "total_placed": placed.get("total_placed"),
            "placements": board["placements"],
            "meetings": board["meetings"],
        }
    )


class _Stop(Exception):
    """Raised by a spy once the relocate domains have been read."""


def _domain_rows(sections, valid_options, schedule, locked_idx) -> dict:
    out = {}
    for index, section in enumerate(sections):
        key = f"{section.get('course_key') or section['code']}|{section['label']}"
        out[key] = {
            "locked": index in (locked_idx or set()),
            "current": [
                [m["day"], m["start"], m["end"], m["slot_idx"]] for m in schedule.get(index, [])
            ],
            "domain": [
                [[o["day"], o["start"], o["end"], o["slot_idx"]] for o in meeting]
                for meeting in valid_options.get(index, [])
            ],
        }
    return out


def capture_relocate_domains(monkeypatch, board) -> dict:
    """The relocate domains simulated annealing and the load balancer build for
    every section of ``board`` (each stage is stopped at its first move)."""
    from core.services import timetable_load_balanced, timetable_local_search

    captured: dict = {}

    def spy(schedule, sections, valid_options, rng, locked_idx=None):
        captured["rows"] = _domain_rows(sections, valid_options, schedule, locked_idx)
        raise _Stop

    out = {}
    for name, module, run in (
        ("sa", timetable_local_search, timetable_local_search.optimize_board),
        ("lb", timetable_load_balanced, timetable_load_balanced.rebalance_board),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(module, "_generate_relocate_move", spy)
            captured.clear()
            with pytest.raises(_Stop):
                run(board.id, max_seconds=1)
        out[name] = captured["rows"]
    return _plain(out)


def capture_repair_domains(scenario) -> dict:
    """``_repair_slot_domain`` for every placement of the scenario."""
    from core.services.timetable_instructor_cap_repair import _repair_slot_domain
    from core.services.timetable_online import OnlineCourseLookup

    lookup = OnlineCourseLookup()
    out = {}
    for placement in (
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by("board__label", "term_section__course_key", "term_section__section", "day")
    ):
        key = "|".join(
            (
                placement.board.label,
                placement.term_section.course_key,
                placement.term_section.section,
                placement.day,
                str(placement.start_time),
            )
        )
        out[key] = [list(cell) for cell in _repair_slot_domain(scenario, placement, lookup)]
    return _plain(out)


def capture_guard_options(scenario) -> dict:
    """The one-teacher guard's placer options for every budget of the scenario."""
    from core.services.section_teacher_assignment import _load_static

    static = _load_static(scenario.id)
    return _plain(
        {
            key: [list(map(list, option)) for option in static.options(key, budget)]
            for key, budget in sorted(static.budgets.items())
        }
    )


def capture_domains(monkeypatch, scenario) -> dict:
    """A compact build, then every domain the flag may change, per board."""
    from core.services.timetable_autoplace import auto_place_scenario

    auto_place_scenario(scenario.id, strategy="compact")
    out: dict = {
        "repair": capture_repair_domains(scenario),
        "guard": capture_guard_options(scenario),
    }
    for board in DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order"):
        out[f"relocate:{board.label}"] = capture_relocate_domains(monkeypatch, board)
    return out
