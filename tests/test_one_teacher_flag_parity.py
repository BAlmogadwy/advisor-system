"""With ``TIMETABLE_ONE_TEACHER_PER_SECTION`` OFF, the timetable is today's.

One teacher per section changes what every instructor rule charges, so it ships
behind a flag that defaults OFF, and OFF must be exactly the course-identity
union of ``74a9a6c`` (the branch the feature starts from). Three checks:

- **P1 â€” no new reads.** With the flag OFF, no planner, display, persistence or
  copy path issues SQL against ``section_instructors`` beyond what it issued at
  ``74a9a6c`` (only the scenario copy reads that table, and only its own rows).
- **P2 â€” same maps.** The three section -> instructor builders return exactly
  the union bodies vendored below, on a seeded random corpus.
- **P3 â€” same board.** A greedy build of the corpus reproduces the golden
  recorded at ``74a9a6c`` byte for byte: placements, meeting rows (instructor
  names included) and the maps. Greedy only â€” CP-SAT rooms differ between
  Windows and Linux (see ``test_exam_linked_rooms_parity``).

The golden lives in ``tests/fixtures/one_teacher_flag_off_golden.json`` and was
recorded by ``tests/record_one_teacher_flag_off_golden.py`` at ``74a9a6c``. To
check it, check that commit out and run::

    ONE_TEACHER_GOLDEN_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/record_one_teacher_flag_off_golden.py

then diff ``<file>`` against the fixture. Never re-record it from a branch that
changes the planner: a golden that follows the code proves nothing.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key

GOLDEN = Path(__file__).parent / "fixtures" / "one_teacher_flag_off_golden.json"

# The production instructor flags, with the new rule explicitly OFF.
FLAG_OFF = {
    "TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED": True,
    "TIMETABLE_INSTRUCTOR_DAILY_CAP": 3,
    "TIMETABLE_ONE_TEACHER_PER_SECTION": False,
}

PROGRAMS = ["DS", "AI", "AI2", "DS2"]
# (programmes, code, name, programme term, credit hours, planned sections)
COURSES = (
    (("AI2", "DS2"), "CS112", "PROGRAMMING I", 2, 3, 5),
    (("AI2", "DS2"), "CS114", "ALGORITHMS & DATA STRUCTURES", 2, 3, 5),
    (("AI2", "DS2"), "AI124", "INTRO TO AI", 2, 3, 3),
    (("AI2",), "MATH203", "CALCULUS II", 2, 3, 2),
    (("AI", "DS"), "CS112", "PROGRAMMING II", 4, 3, 2),
    (("AI", "DS"), "AI201", "MACHINE LEARNING", 4, 4, 4),
    (("DS",), "DS210", "DATA VISUALISATION", 4, 3, 2),
)
# (programme, code) -> lecturer. Shared courses get one lecturer per programme,
# exactly the lab scenario's shape; MATH203 has none.
LINKS = {
    ("AI2", "CS112"): "Dr Amal",
    ("DS2", "CS112"): "Dr Badr",
    ("AI2", "CS114"): "Dr Dana",
    ("DS2", "CS114"): "Dr Eyad",
    ("AI2", "AI124"): "Dr Fahd",
    ("DS2", "AI124"): "Dr Amal",
    ("AI", "AI201"): "Dr Ghada",
    ("DS", "AI201"): "Dr Hani",
    ("DS", "DS210"): "Dr Hani",
    ("AI", "CS112"): "Dr Iman",
}


def build_corpus(seed: int = 11) -> TimetableScenario:
    """A two-board, four-programme scenario with shared, single-programme and
    unlinked courses, CS112 as two identities, and seeded student demand."""
    rng = random.Random(seed)
    for programs, code, name, term, credit, _planned in COURSES:
        for program in programs:
            ProgrammeRequirement.objects.create(
                program=program,
                course_code=code,
                course_name=name,
                programme_term=term,
                credit_hours=credit,
            )
    people = {
        name: Instructor.objects.create(full_name=name, normalised_name=name.casefold())
        for name in sorted(set(LINKS.values()))
    }
    for (program, code), name in LINKS.items():
        CourseInstructor.objects.create(
            program=program, course_code=code, section="M", instructor=people[name]
        )
    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name=f"parity corpus {seed}", gender="M", programs=PROGRAMS
    )
    DeliveryBoard.objects.create(
        scenario=scenario, label="Term 2", nominal_term=2, program="AI2,DS2", display_order=1
    )
    DeliveryBoard.objects.create(
        scenario=scenario, label="Term 4", nominal_term=4, program="AI,DS", display_order=2
    )
    for index in range(7):
        Room.objects.create(
            room_code=f"L{index + 1}",
            capacity=30 + 5 * index,
            room_type="lecture",
            department="AI,DS,AI2,DS2",
        )
    for index in range(3):
        Room.objects.create(
            room_code=f"LAB{index + 1}", capacity=40, room_type="lab", department="AI,DS,AI2,DS2"
        )
    demand: dict[str, int] = defaultdict(int)
    student_id = 9_000_000 + seed * 1000
    for program in PROGRAMS:
        mine = [course for course in COURSES if program in course[0]]
        for _ in range(18):
            student_id += 1
            Student.objects.create(
                student_id=student_id,
                program=program,
                section="M",
                gpa=round(rng.uniform(1.0, 4.0), 2),
                total_earned_credits=rng.choice((30, 60, 90, 110)),
            )
            for _programs, code, name, _term, _credit, _planned in mine:
                if rng.random() < 0.8:
                    key = planner_course_key(code, name)
                    demand[key] += 1
                    ScenarioStudentCourseRequest.objects.create(
                        scenario=scenario,
                        student_id=student_id,
                        course_key=key,
                        course_code=code,
                        course_name=name,
                    )
    for _programs, code, name, term, credit, planned in COURSES:
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


def _names() -> dict[int, str]:
    return dict(Instructor.objects.values_list("id", "full_name"))


def capture_board(scenario: TimetableScenario) -> dict:
    """Everything the flag must not change, keyed by natural identities only
    (row ids differ between SQLite and PostgreSQL)."""
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario
    from core.services.timetable_pr4_instructor import build_section_instructor_ids
    from core.services.timetable_solver import _instructor_ids_for_sections

    names = _names()
    placements = sorted(
        list(row)
        for row in SectionPlacement.objects.filter(board__scenario=scenario).values_list(
            "board__label",
            "term_section__course_key",
            "term_section__section",
            "day",
            "start_time",
            "end_time",
            "room",
            "is_locked",
        )
    )
    meetings = sorted(
        list(row)
        for row in TermSectionMeeting.objects.filter(term_section__scenario=scenario).values_list(
            "term_section__course_key",
            "term_section__section",
            "day",
            "start_time",
            "end_time",
            "room",
            "instructor",
        )
    )
    pr4 = {
        key: sorted(names[i] for i in ids)
        for key, ids in sorted(build_section_instructor_ids(scenario).items())
    }
    v2 = {
        key: sorted(names[i] for i in ids)
        for key, ids in sorted(build_section_instructor_map_for_scenario(scenario.id).items())
    }
    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 2")
    probe = [
        {"code": planner_course_key(code, name), "display_code": code, "label": f"S{n}"}
        for _programs, code, name, _term, _credit, planned in COURSES
        for n in (1, planned + 1)
    ]
    solver = [sorted(names[i] for i in ids) for ids in _instructor_ids_for_sections(board, probe)]
    return {
        "placements": placements,
        "meetings": meetings,
        "pr4_map": pr4,
        "v2_map": v2,
        "solver_ids": solver,
    }


def run_golden_build(scenario: TimetableScenario) -> dict:
    """The recorded workload: a greedy build, a lock, a rebuild, then a reconcile.

    The first build creates the sections; only a rebuild sees them all, so only
    the rebuild exercises the instructor clash and daily-cap filters (on a fresh
    scenario the placer has no section to resolve links for yet).
    """
    from core.services.course_instructor_assignment import reconcile_scenario_instructors
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import reset_scenario

    first = auto_place_scenario(scenario.id, strategy="compact")
    SectionPlacement.objects.filter(
        board__scenario=scenario,
        term_section__course_key=planner_course_key("CS114", "ALGORITHMS & DATA STRUCTURES"),
        term_section__section="S1",
    ).update(is_locked=True)
    reset_scenario(scenario.id, keep_locked=True)
    placed = auto_place_scenario(scenario.id, strategy="compact")
    reconciled = reconcile_scenario_instructors(scenario)
    return {
        "first_placed": first.get("total_placed"),
        "total_placed": placed.get("total_placed"),
        "reconciled": reconciled,
        **capture_board(scenario),
    }


@pytest.fixture
def flag_off(settings):
    for name, value in FLAG_OFF.items():
        setattr(settings, name, value)
    return settings


# â”€â”€ P3 â€” the greedy golden â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@pytest.mark.django_db
def test_p3_flag_off_greedy_build_matches_the_recorded_golden(flag_off) -> None:
    scenario = build_corpus()

    produced = json.loads(json.dumps(run_golden_build(scenario)))

    assert produced == json.loads(GOLDEN.read_text(encoding="utf-8"))


# â”€â”€ P2 â€” the union bodies, vendored from 74a9a6c â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _reference_pr4_union(scenario) -> dict[str, set[int]]:
    from core.services.course_identity import course_display_code
    from core.services.timetable_pr4_instructor import section_instructor_resolver

    out: dict[str, set[int]] = {}
    gender = getattr(scenario, "gender", "")
    if not gender:
        return out
    resolve = section_instructor_resolver(list(getattr(scenario, "programs", []) or []), gender)
    for course_key, course_code, course_number, section in TermSection.objects.filter(
        scenario=scenario
    ).values_list("course_key", "course_code", "course_number", "section"):
        ids = resolve(course_key, course_display_code(course_key, course_code, course_number))
        if ids:
            out[f"{course_key}|{section}"] = ids
    return out


def _reference_v2_union(scenario_id: int) -> dict[str, frozenset[int]]:
    from core.services.course_identity import course_display_code
    from core.services.timetable_pr4_instructor import section_instructor_resolver

    scenario = TimetableScenario.objects.filter(id=scenario_id).first()
    if scenario is None or not scenario.gender or not scenario.programs:
        return {}
    resolve = section_instructor_resolver(list(scenario.programs), scenario.gender)
    out: dict[str, frozenset[int]] = {}
    for course_key, course_code, course_number, section in TermSection.objects.filter(
        scenario_id=scenario_id
    ).values_list("course_key", "course_code", "course_number", "section"):
        ids = resolve(course_key, course_display_code(course_key, course_code, course_number))
        if ids:
            out[f"{course_key or course_code}_{section}"] = frozenset(ids)
    return out


def _reference_solver_union(board, sections) -> list[set[int]]:
    from core.services.timetable_pr4_instructor import section_instructor_resolver
    from core.services.timetable_solver import programmes_for_board

    resolved = _reference_pr4_union(board.scenario)
    fallback = section_instructor_resolver(
        [*(board.scenario.programs or []), *programmes_for_board(board)],
        board.scenario.gender,
    )
    return [
        resolved.get(f"{s['code']}|{s['label']}") or fallback(s["code"], s["display_code"])
        for s in sections
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("seed", [3, 11, 29])
def test_p2_flag_off_builders_are_the_course_identity_union(flag_off, seed) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario
    from core.services.timetable_pr4_instructor import build_section_instructor_ids
    from core.services.timetable_solver import _instructor_ids_for_sections

    scenario = build_corpus(seed)
    auto_place_scenario(scenario.id, strategy="compact")
    rng = random.Random(seed)
    # Unplaced extra labels too: the solver's fallback path.
    probe = [
        {"code": planner_course_key(code, name), "display_code": code, "label": f"S{n}"}
        for _programs, code, name, _term, _credit, planned in COURSES
        for n in rng.sample(range(1, planned + 3), 3)
    ]

    assert build_section_instructor_ids(scenario) == _reference_pr4_union(scenario)
    assert build_section_instructor_map_for_scenario(scenario.id) == _reference_v2_union(
        scenario.id
    )
    for board in DeliveryBoard.objects.filter(scenario=scenario):
        assert _instructor_ids_for_sections(board, probe) == _reference_solver_union(board, probe)


# â”€â”€ P1 â€” no new reads of section_instructors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _section_instructor_sql(queries) -> list[str]:
    return [q["sql"] for q in queries if "section_instructors" in q["sql"]]


def _admin_client():
    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="parity-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_p1_flag_off_paths_never_read_section_instructors(flag_off) -> None:
    from core.services.course_instructor_assignment import (
        apply_primary_instructor,
        reconcile_scenario_instructors,
    )
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario
    from core.services.timetable_export import export_scenario_xlsx
    from core.services.timetable_graph_twin import build_scenario_graph_payload
    from core.services.timetable_instructor_backstop import scenario_instructor_clash_report
    from core.services.timetable_required_sections import reinstate_missing_required_sections
    from core.services.timetable_scenario_quality import build_scenario_quality
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded
    from core.services.timetable_workspace import (
        compute_scenario_safety_summary,
        detect_board_conflicts,
        preview_placement_slot_candidates,
        preview_planned_section_slot_candidates,
        validate_placement,
    )
    from core.timetable_workspace_views import _placement_to_dict

    scenario = build_corpus()
    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 2")
    client = _admin_client()

    def placement():
        return (
            SectionPlacement.objects.filter(board=board)
            .select_related("term_section")
            .order_by("id")
            .first()
        )

    steps = {
        "auto_place_scenario": lambda: auto_place_scenario(scenario.id, strategy="compact"),
        "reinstate": lambda: reinstate_missing_required_sections(scenario.id),
        "apply_primary_instructor": lambda: apply_primary_instructor(
            placement().term_section, scenario, board, placement().term_section.course_code
        ),
        "reconcile": lambda: reconcile_scenario_instructors(scenario),
        "reconcile_view": lambda: client.post(
            "/ops/instructors/course-assignments/reconcile/",
            data=json.dumps({"scenario_id": scenario.id}),
            content_type="application/json",
        ),
        "snapshot_restore": lambda: restore_scenario(scenario.id, snapshot_scenario(scenario.id)),
        "safety_summary": lambda: compute_scenario_safety_summary(scenario.id),
        "detect_board_conflicts": lambda: detect_board_conflicts(board.id),
        "quality": lambda: build_scenario_quality(scenario.id),
        "backstop": lambda: scenario_instructor_clash_report(scenario.id),
        "placement_to_dict": lambda: _placement_to_dict(placement()),
        "validate_placement": lambda: validate_placement(
            board.id,
            "SUN",
            "08:00",
            "09:15",
            "",
            placement().term_section_id,
            exclude_placement_id=placement().id,
        ),
        "preview_placement": lambda: preview_placement_slot_candidates(placement().id),
        "preview_planned": lambda: preview_planned_section_slot_candidates(
            board.id,
            course_code="CS112",
            course_key=planner_course_key("CS112", "PROGRAMMING I"),
            section_label="S9",
        ),
        "graph_twin": lambda: build_scenario_graph_payload(scenario.id),
        "export": lambda: export_scenario_xlsx(scenario.id),
        "runner_current": lambda: run_v2_optimisation_guarded(
            scenario.id, mode="current", max_iterations=2, run_chain=False, run_cpsat=False
        ),
        "runner_full": lambda: run_v2_optimisation_guarded(
            scenario.id,
            mode="full",
            max_iterations=2,
            run_chain=False,
            run_cpsat=False,
            strategies=["compact"],
        ),
    }
    reads = {}
    for name, step in steps.items():
        with CaptureQueriesContext(connection) as ctx:
            step()
        reads[name] = _section_instructor_sql(ctx.captured_queries)

    assert reads == {name: [] for name in steps}


@pytest.mark.django_db
def test_p1_flag_off_copy_reads_only_its_existing_section_instructor_queries(flag_off) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_scenario_copy import copy_timetable_scenario

    scenario = build_corpus()
    auto_place_scenario(scenario.id, strategy="compact")

    with CaptureQueriesContext(connection) as ctx:
        copy_timetable_scenario(scenario.id, name="parity copy")

    reads = _section_instructor_sql(ctx.captured_queries)
    # One locked read of the source's own rows; nothing is shared or global, so
    # neither the external lookup nor an insert runs.
    assert len(reads) == 1
    assert "FROM" in reads[0].upper() and "scenario_id" in reads[0]
