"""Builders shared by the weekly meeting pattern engine tests (not collected).

One board, three courses, every room type the engines choose between:

- **X** is CS112 PROGRAMMING I, a 3-credit course the scenario teaches as two
  100-minute meetings a week (``ScenarioSectionBudget.meeting_pattern = "2x100"``);
- its **twin** is CS112 PROGRAMMING II: the same registrar code, another course
  (CODE::NAME), left at the credit default (2 x 75), so any lookup by the bare code
  hands it X's shape;
- **AI201** is an ordinary 4-credit course (75 + 75 + a 100-minute lab), the one
  course whose 100-minute meeting belongs in a lab room.

Four lecture rooms and two labs, all for the board's programme, and seeded student
demand so the courses share students.
"""

from __future__ import annotations

import random
from collections import defaultdict

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS, placeable_slots
from core.services.timetable_meeting_shape import TWO_BY_100
from tests.test_section_day_spread import PRODUCTION_FLAGS

DAYS = ("SUN", "MON", "TUE", "WED", "THU")
PROGRAMME = "AI"

X_KEY = planner_course_key("CS112", "PROGRAMMING I")
TWIN_KEY = planner_course_key("CS112", "PROGRAMMING II")
LAB_KEY = planner_course_key("AI201", "MACHINE LEARNING")
# (key, code, name, credit hours, planned sections, meeting pattern, demand share)
COURSES = (
    (X_KEY, "CS112", "PROGRAMMING I", 3, 2, TWO_BY_100, 0.8),
    (TWIN_KEY, "CS112", "PROGRAMMING II", 3, 2, "", 0.6),
    (LAB_KEY, "AI201", "MACHINE LEARNING", 4, 1, "", 0.8),
)

LECTURE_ROOMS = ("L1", "L2", "L3", "L4")
LAB_ROOMS = ("LAB1", "LAB2")

#: The 100-minute columns an automatic placer may use, the online-only ones it must
#: never use, and the 75-minute lecture slots (default grid).
LONG_WINDOWS = tuple((s["start"], s["end"]) for s in placeable_slots(DEFAULT_LAB_SLOTS))
ONLINE_ONLY_STARTS = frozenset(s["start"] for s in DEFAULT_LAB_SLOTS if s.get("online_only"))
LECTURE_WINDOWS = tuple((s["start"], s["end"]) for s in placeable_slots(DEFAULT_SLOTS))

#: A smaller grid for the CP-SAT solves: three lecture slots, three 100-minute
#: columns and one online-only window, all taken from the default grid. A CP-SAT
#: model of the default grid needs several seconds of presolve before its first
#: solution; this one solves to optimality in well under a second.
SMALL_SLOT_CONFIG = [dict(s) for s in DEFAULT_SLOTS if s["start"] in ("09:00", "10:30", "13:00")]
SMALL_LAB_SLOT_CONFIG = [
    dict(s) for s in DEFAULT_LAB_SLOTS if s["start"] in ("09:00", "10:45", "13:00", "15:50")
]

# The production planner flags (day spread and the online windows are ON there).
FLAGS = {
    **PRODUCTION_FLAGS,
    "TIMETABLE_SECTION_DAY_SPREAD": True,
    "TIMETABLE_SECTION_DAY_SPREAD_WEIGHT": 40,
    "TIMETABLE_ONLINE_EVENING_WINDOWS": True,
}


def minutes(text: str) -> int:
    hours, mins = str(text)[:5].split(":")
    return int(hours) * 60 + int(mins)


def build_scenario(
    seed: int = 11,
    *,
    x_pattern: str = TWO_BY_100,
    planned: dict[str, int] | None = None,
    small_grid: bool = False,
) -> TimetableScenario:
    """The board above. ``x_pattern=""`` leaves X at the credit default too;
    ``planned`` overrides sections per course key; ``small_grid`` uses the small grid."""
    rng = random.Random(seed)
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name=f"meeting pattern {seed}",
        programs=[PROGRAMME],
        slot_config=SMALL_SLOT_CONFIG if small_grid else [],
        lab_slot_config=SMALL_LAB_SLOT_CONFIG if small_grid else [],
    )
    DeliveryBoard.objects.create(
        scenario=scenario, label="Term 1", nominal_term=1, program=PROGRAMME, display_order=1
    )
    for code in LECTURE_ROOMS:
        Room.objects.get_or_create(
            room_code=code,
            defaults={"capacity": 40, "room_type": "lecture", "department": PROGRAMME},
        )
    for code in LAB_ROOMS:
        Room.objects.get_or_create(
            room_code=code, defaults={"capacity": 40, "room_type": "lab", "department": PROGRAMME}
        )
    demand: dict[str, int] = defaultdict(int)
    first_id = 7_000_000 + seed * 1000
    for offset in range(1, 25):
        student = Student.objects.create(
            student_id=first_id + offset, program=PROGRAMME, section="M", gpa=3.0
        )
        for key, code, name, _hours, _planned, _pattern, share in COURSES:
            if rng.random() < share:
                demand[key] += 1
                ScenarioStudentCourseRequest.objects.create(
                    scenario=scenario,
                    student_id=student.student_id,
                    course_key=key,
                    course_code=code,
                    course_name=name,
                )
    for key, code, name, hours, sections, pattern, _share in COURSES:
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=key,
            course_code=code,
            course_name=name,
            department=PROGRAMME,
            credit_hours=hours,
            planned_sections=(planned or {}).get(key, sections),
            max_per_section=40,
            total_demand=demand[key],
            programme_term=1,
            meeting_pattern=x_pattern if key == X_KEY else pattern,
        )
    return scenario


def board_of(scenario: TimetableScenario) -> DeliveryBoard:
    return DeliveryBoard.objects.get(scenario=scenario, label="Term 1")


def planned_sections(scenario: TimetableScenario) -> dict[str, int]:
    return dict(
        ScenarioSectionBudget.objects.filter(scenario=scenario).values_list(
            "course_key", "planned_sections"
        )
    )


def placed_sections(scenario: TimetableScenario) -> dict[str, dict[str, list[tuple]]]:
    """course_key -> section -> [(day, start, end, room)], sorted."""
    out: dict[str, dict[str, list[tuple]]] = defaultdict(lambda: defaultdict(list))
    rows = SectionPlacement.objects.filter(board__scenario=scenario).values_list(
        "term_section__course_key", "term_section__section", "day", "start_time", "end_time", "room"
    )
    for key, label, day, start, end, room in rows:
        out[key][label].append((day, str(start)[:5], str(end)[:5], room))
    return {key: {label: sorted(m) for label, m in by.items()} for key, by in out.items()}


def room_types() -> dict[str, str]:
    return dict(Room.objects.values_list("room_code", "room_type"))


def assert_two_by_100(meetings: list[tuple], where: object = "") -> None:
    """Two meetings, each a whole 100-minute column, on two different days."""
    assert len(meetings) == 2, (where, meetings)
    assert len({day for day, *_rest in meetings}) == 2, (where, meetings)
    for _day, start, end, *_room in meetings:
        assert (start, end) in LONG_WINDOWS, (where, meetings)
        assert start not in ONLINE_ONLY_STARTS, (where, meetings)
        assert minutes(end) - minutes(start) == 100, (where, meetings)


def assert_scenario_shapes(scenario: TimetableScenario) -> None:
    """Every section of the scenario is placed, in its course's weekly shape and rooms.

    X: 2 x 100 in lecture rooms; its twin: 2 x 75 in lecture rooms; AI201: two
    75-minute lectures in lecture rooms and its 100-minute lab in a lab room.
    """
    placed = placed_sections(scenario)
    planned = planned_sections(scenario)
    types = room_types()
    assert {key: len(sections) for key, sections in placed.items()} == planned, placed
    for key, sections in placed.items():
        for label, meetings in sections.items():
            where = (key, label)
            if key == X_KEY:
                assert_two_by_100(meetings, where)
            elif key == TWIN_KEY:
                assert len(meetings) == 2, where
                assert len({m[0] for m in meetings}) == 2, (where, meetings)
                assert all((s, e) in LECTURE_WINDOWS for _d, s, e, _r in meetings), meetings
            else:
                lengths = sorted(minutes(e) - minutes(s) for _d, s, e, _r in meetings)
                assert lengths == [75, 75, 100], (where, meetings)
            for _day, start, end, room in meetings:
                assert room in types, (where, meetings)  # never UNASSIGNED or empty
                lab_meeting = key == LAB_KEY and minutes(end) - minutes(start) == 100
                assert types[room] == ("lab" if lab_meeting else "lecture"), (where, meetings)


def solver_shapes(result: dict) -> dict[str, list[list[tuple]]]:
    """course_key -> [the meetings of each section] from a CP-SAT result."""
    out: dict[str, list[list[tuple]]] = defaultdict(list)
    for placement in result["placements"]:
        out[placement["course_code"]].append(
            sorted((m["day"], m["start"], m["end"]) for m in placement["meetings"])
        )
    return dict(out)
