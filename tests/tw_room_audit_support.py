"""The room-audit fixture shared by ``test_tw_room_audit`` and its recorder (not collected).

A small scenario shaped like the QA copy's scenario 665, with the cases the QA
data does not have: shared meetings (copies in one room, in two rooms, roomed
plus unroomed, a kept copy in a room the inventory does not know), a course
with no budget row, a board with no programme, rooms with no department, no
gender or a blank type. Times are canonical ``HH:MM``; there are 12 rooms.

``run_legacy_corpus`` is what the oracle ``tests/fixtures/tw_room_audit_legacy.json``
records: room candidates through the view (no ``audit`` flag) for every subject,
at its own slot and at another one, bulk clean rooms (preview and apply) and
repair's room choice, in both teacher modes, plus the quality audit's own
outputs. Every database id is replaced by a fixture symbol before it is
compared or saved, so the oracle does not depend on autoincrement values.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from django.contrib.auth.models import Group, User
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from tests.one_teacher_support import FLAG_ON, place

ORACLE = Path(__file__).parent / "fixtures" / "tw_room_audit_legacy.json"

#: (symbol, label, programme). AI and DS have male students; DS and NP have
#: none, so their gender is "" (any); NP has no programme at all.
BOARDS = (("AI", "AI T3", "AI"), ("DS", "DS T3", "DS"), ("NP", "NP T3", ""))

#: (code, capacity, room_type, section, department)
ROOMS = (
    ("L40", 40, "lecture", "M", "AI,DS"),
    ("L60", 60, "lecture", "M", "AI,DS"),
    ("L30", 30, "lecture", "M", "AI,DS"),
    ("L25", 25, "lecture", "M", "AI"),
    ("L30D", 30, "lecture", "M", "DS"),
    ("LAB30", 30, "lab", "M", "AI,DS"),
    ("LF40", 40, "lecture", "F", "AI,DS"),
    ("LX40", 40, "lecture", "M", ""),
    ("LN40", 40, "lecture", "", "AI,DS"),
    ("LT40", 40, "  ", "M", "AI,DS"),
    ("BUSY40", 40, "lecture", "M", "AI,DS"),
    ("ELSE40", 40, "lecture", "M", "AI,DS"),
)

#: code -> (credit hours, budget (total_demand, planned_sections) or None,
#: available_capacity, section label). The audit's need is
#: int(ceil(total / planned) * 1.1), 44 when planned is 0, 40 with no budget.
COURSES = {
    "AI301": (3, (24, 1), 25, "M1"),  # need 26 against the legacy 25
    "AI302": (3, (20, 1), 35, "S1"),  # need 22 against the legacy 35
    "AI303": (3, (20, 1), 30, "S1"),
    "AI304": (3, (20, 1), 30, "S1"),
    "AI305": (3, (20, 1), 30, "S1"),
    "AI306": (3, None, 30, "M1"),  # no budget row: the default 40
    "AI307": (3, (100, 0), 30, "M1"),  # planned 0: max_per_section, buffered (44)
    "AI308": (3, (0, 1), 0, "M1"),  # no demand: need 0
    "AI309": (4, (24, 1), 28, "M1"),  # 4 credits: a lab at 100 minutes
    "AI310": (3, (20, 1), 30, "S1"),
    "DS311": (3, (20, 1), 30, "S1"),
    "AI312": (3, (20, 1), 30, "S1"),
    "OCC1": (3, (20, 1), 30, "M1"),
    "OCC2": (3, (20, 1), 30, "M1"),
}

ALL_DAY = [(day, "08:00", "18:00") for day in ("SUN", "MON", "TUE")]

#: (code, board, meetings, room)
PLACEMENTS = (
    ("AI301", "AI", [("SUN", "08:00", "09:15")], "L40"),
    ("AI302", "AI", [("SUN", "09:30", "10:45")], "L40"),
    ("AI302", "DS", [("SUN", "09:30", "10:45")], "L40"),
    ("AI303", "AI", [("SUN", "11:00", "12:15")], "L40"),
    ("AI303", "DS", [("SUN", "11:00", "12:15")], "L30D"),
    ("AI304", "AI", [("SUN", "12:30", "13:45")], "L40"),
    ("AI304", "DS", [("SUN", "12:30", "13:45")], ""),
    ("AI305", "AI", [("SUN", "14:00", "15:15")], "L40"),
    ("AI305", "DS", [("SUN", "14:00", "15:15")], "GHOST1"),
    ("AI306", "AI", [("SUN", "15:30", "16:45")], "L40"),
    ("AI307", "AI", [("MON", "08:00", "09:15")], "L40"),
    ("AI308", "AI", [("MON", "09:30", "10:45")], "UNASSIGNED"),
    ("AI309", "AI", [("MON", "11:00", "12:40")], "LAB30"),
    ("AI310", "NP", [("MON", "13:00", "14:15")], "L40"),
    ("DS311", "DS", [("MON", "14:30", "15:45")], "L30D"),
    ("AI312", "AI", [("TUE", "08:00", "09:15")], "L40"),
    ("AI312", "DS", [("TUE", "09:30", "10:45")], "L30D"),
    # In use all day: BUSY40 on the AI board, ELSE40 on the DS board.
    ("OCC1", "AI", ALL_DAY, "BUSY40"),
    ("OCC2", "DS", ALL_DAY, "ELSE40"),
)

#: Subjects previewed at their own slot (a room-only save), as (code, board).
OWN_SLOT_SUBJECTS = (
    ("AI301", "AI"),  # single board, label carries the gender
    ("AI302", "AI"),  # shared, both copies in one room
    ("AI303", "AI"),  # shared, the DS copy in another room (split)
    ("AI304", "AI"),  # shared, the DS copy unroomed
    ("AI304", "DS"),  # ... and the unroomed copy itself
    ("AI305", "AI"),  # shared, the DS copy in a room the inventory lacks
    ("AI305", "DS"),  # ... and that copy itself
    ("AI306", "AI"),  # no budget row
    ("AI307", "AI"),  # planned_sections 0
    ("AI308", "AI"),  # total_demand 0, room UNASSIGNED
    ("AI309", "AI"),  # a 4-credit lab meeting
    ("AI310", "NP"),  # the board with no programme
    ("DS311", "DS"),  # a board with no gender
)
#: Subjects previewed at another slot (a slot change), as ((code, board), (day, start, end)).
OTHER_SLOT_SUBJECTS = (
    (("AI312", "AI"), ("TUE", "09:30", "10:45")),  # joins its DS copy's meeting
    (("AI309", "AI"), ("MON", "16:00", "17:15")),  # 75 minutes: a lecture now
)
#: Repair's room choice, as ((code, board), (day, start, end) or None for its own slot).
REPAIR_SUBJECTS = (
    (("AI301", "AI"), None),
    (("AI310", "NP"), None),
    (("AI312", "AI"), ("TUE", "09:30", "10:45")),
    (("AI309", "AI"), ("MON", "16:00", "17:15")),
)
TEACHER_MODES = (("one_teacher_on", True), ("one_teacher_off", False))


# ── The fixture ───────────────────────────────────────────────────────────


def course_key(code: str) -> str:
    return f"{code}::COURSE {code}"


def build_fixture() -> TimetableScenario:
    """Create the scenario, its boards, rooms, budgets and placements."""
    sc = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="room audit", gender="M", programs=["AI", "DS"]
    )
    boards = {
        symbol: DeliveryBoard.objects.create(
            scenario=sc, label=label, nominal_term=3, program=program, display_order=order
        )
        for order, (symbol, label, program) in enumerate(BOARDS, start=1)
    }
    for student_id in (9001, 9002, 9003):
        Student.objects.create(student_id=student_id, name=f"S{student_id}", section="M")
        BoardStudentLink.objects.create(board=boards["AI"], student_id=student_id)
    for code, capacity, room_type, gender, department in ROOMS:
        Room.objects.create(
            room_code=code,
            capacity=capacity,
            room_type=room_type,
            section=gender,
            department=department,
            building="B1",
        )
    sections = {}
    for code, (credits, demand, available, label) in COURSES.items():
        if demand is not None:
            total, planned = demand
            ScenarioSectionBudget.objects.create(
                scenario=sc,
                course_key=course_key(code),
                course_code=code,
                course_name=f"COURSE {code}",
                department="AI",
                credit_hours=credits,
                planned_sections=planned,
                max_per_section=40,
                total_demand=total,
                programme_term=3,
            )
        sections[code] = TermSection.objects.create(
            scenario=sc,
            course_key=course_key(code),
            course_code=code,
            course_number=code,
            course_name=f"COURSE {code}",
            section=label,
            available_capacity=available,
            source_tag="tw_auto",
        )
    for code, board, meetings, room in PLACEMENTS:
        place(boards[board], sections[code], meetings, room=room)
    return sc


def placement_of(code: str, board: str, day: str | None = None) -> SectionPlacement:
    rows = SectionPlacement.objects.filter(
        term_section__course_code=code, board__label=dict((b[0], b[1]) for b in BOARDS)[board]
    )
    if day is not None:
        rows = rows.filter(day=day)
    return rows.select_related("board__scenario", "term_section").get()


# ── Id symbols ────────────────────────────────────────────────────────────


def symbols(scenario: TimetableScenario) -> dict[str, dict[int, str]]:
    """Every id the corpus can print -> its fixture symbol."""
    label_to_symbol = {label: symbol for symbol, label, _program in BOARDS}
    boards = {
        board.id: label_to_symbol.get(board.label, board.label)
        for board in DeliveryBoard.objects.filter(scenario=scenario)
    }
    return {
        "scenario": {scenario.id: "SCENARIO"},
        "board": boards,
        "term_section": {
            ts.id: f"{ts.course_code}/{ts.section}"
            for ts in TermSection.objects.filter(scenario=scenario)
        },
        "placement": {
            p.id: f"{p.term_section.course_code}@{boards[p.board_id]}@{p.day} {p.start_time}"
            for p in SectionPlacement.objects.filter(board__scenario=scenario).select_related(
                "term_section"
            )
        },
    }


_KIND_BY_KEY = {
    "id": "placement",
    "placement_id": "placement",
    "placement_ids": "placement",
    "moving_placement_ids": "placement",
    "board_id": "board",
    "board_ids": "board",
    "term_section_id": "term_section",
    "scenario_id": "scenario",
}


def _kind(key: str | None) -> str | None:
    if key is None:
        return None
    if key == "demand_id" or key.endswith("_demand_ids"):
        return "demand"
    return _KIND_BY_KEY.get(key)


def normalise(value: Any, table: dict[str, dict[int, str]], key: str | None = None) -> Any:
    """``value`` with every id replaced by its symbol; key order is kept.

    An id the table does not know raises ``KeyError``: an id that is not
    normalised must fail the comparison loudly, not pass by luck.
    """
    if isinstance(value, dict):
        return {k: normalise(v, table, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalise(v, table, key) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(normalise(v, table, key) for v in value)
    kind = _kind(key)
    if kind == "demand" and isinstance(value, str):
        term_section_id, rest = value.split("#", 1)
        return f"{table['term_section'][int(term_section_id)]}#{rest}"
    if kind is not None and isinstance(value, int) and not isinstance(value, bool):
        return table[kind][value]
    return value


def dumps(value: Any) -> str:
    """The comparison form: key order included, nothing sorted."""
    return json.dumps(value, ensure_ascii=False)


# ── Requests ──────────────────────────────────────────────────────────────


def admin_client() -> Client:
    user = User.objects.create_user(username="room-audit-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def set_teacher_mode(settings, on: bool) -> None:
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = on


def room_candidates_url(placement: SectionPlacement, slot=None, extra: str = "") -> str:
    url = f"/ops/tw/placements/{placement.id}/room-candidates/"
    params = []
    if slot is not None:
        day, start, end = slot
        params.append(f"day={day}&start_time={start}&end_time={end}")
    if extra:
        params.append(extra)
    return url + ("?" + "&".join(params) if params else "")


def counted(call: Callable[[], Any]) -> tuple[int, Any]:
    with CaptureQueriesContext(connection) as ctx:
        result = call()
    return len(ctx.captured_queries), result


def get_json(client: Client, url: str) -> tuple[int, dict]:
    queries, response = counted(lambda: client.get(url))
    assert response.status_code == 200, response.content
    return queries, json.loads(response.content)


@contextmanager
def rolled_back() -> Iterator[None]:
    """Run a write and undo it (a savepoint inside the test's transaction)."""
    with transaction.atomic():
        yield
        transaction.set_rollback(True)


def subject_name(code: str, board: str, slot=None) -> str:
    return f"{code}@{board}" + ("" if slot is None else "->" + " ".join(slot))


def run_legacy_corpus(client: Client, settings, scenario: TimetableScenario) -> dict:
    """Everything the oracle records, normalised (see the module docstring)."""
    from core.services.timetable_exact_rooming import build_meeting_demands
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
    from core.services.timetable_repair import _select_room_for_candidate
    from core.services.timetable_scenario_quality import _rooms
    from core.services.timetable_workspace import (
        apply_bulk_clean_room_assignments,
        preview_bulk_clean_room_assignments,
    )

    table = symbols(scenario)
    produced: dict[str, Any] = {}
    for mode, on in TEACHER_MODES:
        set_teacher_mode(settings, on)
        views: dict[str, Any] = {}
        subjects = [((code, board), None) for code, board in OWN_SLOT_SUBJECTS]
        for (code, board), slot in [*subjects, *OTHER_SLOT_SUBJECTS]:
            queries, body = get_json(client, room_candidates_url(placement_of(code, board), slot))
            views[subject_name(code, board, slot)] = {
                "queries": queries,
                "response": normalise(body, table),
            }
        queries, preview = counted(lambda: preview_bulk_clean_room_assignments(scenario.id))
        bulk_preview = {"queries": queries, "result": normalise(preview, table)}
        with rolled_back():
            queries, applied = counted(lambda: apply_bulk_clean_room_assignments(scenario.id))
        bulk_apply = {"queries": queries, "result": normalise(applied, table)}
        repair = {}
        for (code, board), slot in REPAIR_SUBJECTS:
            p = placement_of(code, board)
            day, start, end = slot or (p.day, p.start_time, p.end_time)
            repair[subject_name(code, board, slot)] = normalise(
                list(_select_room_for_candidate(p, day, start, end)), table
            )
        produced[mode] = {
            "room_candidates": views,
            "bulk_preview": bulk_preview,
            "bulk_apply": bulk_apply,
            "repair": repair,
        }
    evaluator = build_recovery_room_evaluator(scenario.id)
    produced["audit"] = {
        "demands": normalise([asdict(d) for d in build_meeting_demands(scenario.id)], table),
        "original_room_report": normalise(evaluator.original_room_report, table),
        "rooms": normalise(_rooms(scenario.id), table),
    }
    return produced
