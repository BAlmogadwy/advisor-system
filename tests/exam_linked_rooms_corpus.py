"""Boards for the shared-rooms parity tests: with no links, rooming may not change.

Linked courses may share a room (policy version 2). Without links the
allocator, the room QA, the invigilator counts, the building footprint and
every export must return, byte for byte, what origin/master (84704ad) returned
before sharing existed. The digests in ``test_exam_linked_rooms_parity.py``
were recorded by running these very boards through master.

Nothing here imports code that master lacks: the functions under test are
imported from modules master has, or passed in. What is taken out of an
answer is only what differs between two runs of master itself - the
timestamps a workbook's ``docProps/core.xml`` carries, and the clock a roster
view reads.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import random
import zipfile
from collections.abc import Callable, Iterator

from tests.exam_linked_parity_corpus import _plain, _sections, board_entries, digest, pure_boards

# ── one period at a time: rooms too few and too small ───────────────────────


def period_boards(count: int = 60) -> Iterator[dict]:
    """One cohort's demands against a tight inventory, so the allocator must
    consolidate, search, split sections and leave students unseated."""
    for number in range(count):
        rng = random.Random(9100 + number)
        gender = rng.choice(["M", "F"])
        demands = []
        for course in range(rng.randint(1, 6)):
            code = f"Q{course:02d}"
            for section in range(rng.randint(1, 3)):
                demands.append(
                    {
                        "course_code": code,
                        "course_identity": f"{code}::name",
                        "section": f"{gender}{section + 1}",
                        "section_key": f"term-section:{number * 100 + course * 10 + section}",
                        "gender": gender,
                        "mapping_status": "mapped",
                        "student_count": rng.randint(1, 45),
                        "preferred_room": rng.choice(["", "", f"{gender}-20-0"]),
                    }
                )
        rooms = [
            {"room_code": f"{gender}-{size}-{copy}", "capacity": size, "section": gender}
            for size in rng.sample([10, 15, 20, 30, 40, 60], rng.randint(1, 4))
            for copy in range(rng.randint(1, 3))
        ]
        yield {"demands": demands, "rooms": rooms}


def run_period(board: dict, allocate_period: Callable, context_factory: Callable) -> str:
    return digest(allocate_period(board["demands"], board["rooms"], context_factory()))


# ── whole boards: rooms, invigilators, room QA and the building footprint ───

ROOMS_WITH_BUILDINGS = [
    {
        "room_code": f"{gender}-{size}-{copy}",
        "capacity": size,
        "section": gender,
        "building": "B1" if copy % 2 else "B2",
        "floor": str(copy % 3),
    }
    for gender in ("F", "M")
    for size in (20, 40, 80)
    for copy in range(8)
]
#: Too few seats for every board: sections split and some go unseated.
TIGHT_ROOMS = [
    {
        "room_code": f"{gender}-{size}-{copy}",
        "capacity": size,
        "section": gender,
        "building": "B3",
        "floor": "1",
    }
    for gender in ("F", "M")
    for size in (10, 30)
    for copy in range(2)
]


def run_room_reports(
    board: dict,
    rooms: list[dict],
    assign_rooms: Callable,
    rebalance: Callable,
    context_factory: Callable,
    room_qa: Callable,
    footprint: Callable,
    adj: dict,
) -> str:
    """Rooms and the invigilator pass on a board, then what the reports say."""
    entries = board_entries(board)
    sections = _sections(board)
    context = context_factory(64)
    assign_rooms(entries, sections, rooms, allocation_context=context)
    moves = rebalance(
        entries,
        sections,
        rooms,
        board["slots"],
        adj,
        board["plan_term_buckets"],
        board["course_buckets"],
        max_trials=12,
        allocation_context=context,
        enrolled_sets=board["enrolled"],
        credit_map=board["credit_map"],
        max_per_day=board["max_per_day"],
        caller="parity",
    )
    # As the build does: each room row learns its building and floor.
    meta = {room["room_code"]: room for room in rooms}
    for entry in entries:
        for row in entry.get("rooms") or []:
            known = meta.get(row.get("room_code"))
            if known:
                row.setdefault("building", known["building"])
                row.setdefault("floor", known["floor"])
    return digest(
        {
            "moves": moves,
            "entries": entries,
            "room_qa": room_qa(entries, rooms),
            "footprint": footprint(entries),
        }
    )


def room_boards() -> Iterator[dict]:
    """Every pure board once with rooms to spare, then a third of them tight."""
    for number, board in enumerate(pure_boards()):
        yield board, ROOMS_WITH_BUILDINGS
        if number % 3 == 0:
            yield board, TIGHT_ROOMS


# ── a saved run, and everything exported from it ────────────────────────────

POPULATION_DAYS = ["Sun", "Mon", "Tue"]
POPULATION_PERIODS = ["08:00-10:00", "13:00-15:00"]
#: (code, name, credits, department programmes that sit it)
POPULATION_COURSES = [
    ("CS101", "PROGRAMMING I", 3, ("CS", "IS")),
    ("AI212", "DATA STRUCTURES", 3, ("AI",)),
    ("AI225", "DATA STRUCTURES", 3, ("CS",)),
    ("IS102", "INFORMATION SYSTEMS", 3, ("IS",)),
    ("CYB152", "SECURITY BASICS", 2, ("AI", "CS")),
    ("MATH106", "CALCULUS", 4, ("AI", "CS", "IS")),
    ("STAT301", "PROBABILITY", 3, ("AI", "CS")),
]
FIXED_RUN_ID = 777001
FIXED_AT = dt.datetime(2026, 9, 27, 9, 30, tzinfo=dt.UTC)


def create_rooms_population(models, seed: int = 20260928) -> None:
    """Department and external courses, a code two plans name differently
    (PHYS103 (1) and (2)), both cohorts, and rooms in two buildings.

    Every id that reaches a result or an export is explicit, so a digest never
    depends on which tests ran first.
    """
    rng = random.Random(seed)
    for code, name, credits, _programs in POPULATION_COURSES:
        models.Course.objects.create(course_code=code, description=name, credit_hours=credits)
    models.Course.objects.create(course_code="PHYS103", description="PHYSICS", credit_hours=4)
    plans = {}
    for number, (code, name, _credits, programs) in enumerate(POPULATION_COURSES):
        for program in programs:
            models.ProgrammeRequirement.objects.create(
                program=program,
                course_code=code,
                course_name=name,
                programme_term=number % 4 + 1,
                is_online=code == "CYB152" and program == "CS",
            )
            plans.setdefault(program, []).append(code)
    for program, name in (("AI", "GENERAL PHYSICS"), ("CS", "PHYSICS FOR COMPUTING")):
        models.ProgrammeRequirement.objects.create(
            program=program, course_code="PHYS103", course_name=name, programme_term=5
        )
        plans[program].append("PHYS103")
    sections: dict[tuple[str, str], object] = {}
    next_id = 88001
    for code in [code for code, *_ in POPULATION_COURSES] + ["PHYS103"]:
        for label in ("M1", "M2", "F1"):
            sections[(code, label)] = models.TermSection.objects.create(
                id=next_id,
                course_key=code,
                course_code=code,
                course_number="",
                course_name=code,
                section=label,
                source_tag="scraper_timetable",
            )
            next_id += 1
    for number in range(150):
        program = ("AI", "CS", "IS")[number % 3]
        gender = "M" if number % 5 < 3 else "F"
        student = models.Student.objects.create(
            student_id=880000 + number,
            name=f"STUDENT {number:03d}",
            program=program,
            section=gender,
        )
        for code in sorted(rng.sample(plans[program], rng.randint(2, min(5, len(plans[program]))))):
            label = "F1" if gender == "F" else rng.choice(["M1", "M1", "M2"])
            models.StudentTermSection.objects.create(
                student_id=student.student_id,
                academic_year="1448",
                term="1",
                term_section=sections[(code, label)],
                source="scraper_timetable",
            )
    for gender in ("M", "F"):
        for size, copies in ((12, 3), (25, 3), (35, 2), (50, 1)):
            for copy in range(copies):
                models.Room.objects.create(
                    room_code=f"{gender}{size}-{copy}",
                    capacity=size,
                    section=gender,
                    building="North" if copy % 2 else "South",
                    floor=copy + 1,
                )


def save_fixed_run(models, result: dict):
    """Persist a build's result the way the build does, under a fixed id and time."""
    result = json.loads(json.dumps(result, ensure_ascii=False, default=_plain))
    result.pop("run_id", None)
    run = models.ExamTimetableRun.objects.create(
        id=FIXED_RUN_ID, label="Rooms parity", result_json=json.dumps(result, ensure_ascii=False)
    )
    models.ExamTimetableRun.objects.filter(pk=run.pk).update(created_at=FIXED_AT)
    run.refresh_from_db()
    return run


def workbook_parts(content: bytes) -> dict[str, str]:
    """A workbook's parts, less the save timestamps openpyxl writes into core.xml.

    Without lxml, openpyxl streams each worksheet through a temporary file
    opened in text mode, so Windows writes every newline inside a cell as
    CRLF while Linux (CI, production) writes LF. That is the only thing
    turned back here: an XML reader already sees CRLF as LF (XML 1.0, 2.11),
    so the workbook Excel opens is the same either way.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {
            name: archive.read(name).decode("utf-8").replace("\r\n", "\n")
            for name in sorted(archive.namelist())
            if name != "docProps/core.xml"
        }


def archive_parts(content: bytes) -> dict[str, dict[str, str]]:
    """Each workbook of a zip, or the one workbook, by file name."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = sorted(archive.namelist())
        if all(name.endswith(".xlsx") for name in names):
            return {name: workbook_parts(archive.read(name)) for name in names}
    return {"workbook": workbook_parts(content)}


def export_digests(run, api) -> dict[str, str]:
    """Master Excel, Department files, the student-data export and the rosters.

    ``api`` carries the export functions, so master and the branch are driven
    by exactly the same calls.
    """
    digests = {}
    path = api.export_exam_timetable_xlsx(run.pk)
    try:
        digests["master_excel"] = digest(workbook_parts(path.read_bytes()))
    finally:
        path.unlink(missing_ok=True)
    data = api.load_normalised_run(run)
    for language in ("en", "ar"):
        options = api.department_export_options(data, language)
        content, _name, _type = api.export_department_workbooks(
            run,
            {
                "departments": [profile["id"] for profile in options["departments"]],
                "genders": options["genders"],
                "language": language,
                "dates": {"Sun": "2026-12-06", "Mon": "2026-12-07"},
            },
        )
        digests[f"department_{language}"] = digest(archive_parts(content))
    model = api.build_roster_model(run, now=FIXED_AT)
    for language in ("en", "ar"):
        options = api.parse_export_options(
            {"scope": {"kind": "all"}, "one_file_per_group": True, "language": language}, model
        )
        prepared = api.prepare_export(
            model, options, generated_by="parity", generated_role="EXAM_COMMITTEE", now=FIXED_AT
        )
        content, _name, _type = api.render_export(
            prepared, reference="EXR-PARITY01", audit_hash="0" * 64
        )
        digests[f"student_{language}"] = digest(archive_parts(content))
    view = api.build_roster_view(run, built=0.0)
    lists = {}
    for slot_index, room_code in sorted(view.model.saved.rooms):
        options = api.parse_view_scope(
            {"scope": {"kind": "room", "slot_index": slot_index, "room_code": room_code}},
            view.model,
        )
        payload = api.scope_payload(view, options, api.select_scope(view, options))
        payload.pop("lists_checked_at", None)
        lists[f"{slot_index}:{room_code}"] = payload
    digests["rosters"] = digest({"navigator": api.navigator_facts(view), "rooms": lists})
    return digests


def build_population_run(api, models, extra: dict | None = None):
    """The population's build: rooms, pins and the invigilator pass."""
    result = api.build_exam_timetable(
        "Rooms parity",
        days=POPULATION_DAYS,
        periods=POPULATION_PERIODS,
        max_per_day=2,
        programs=["AI", "CS", "IS"],
        pinned=[{"course_code": "MATH106", "day": "Sun", "period": POPULATION_PERIODS[0]}],
        seed=5,
        assign_rooms=True,
        rebalance_invigilators=True,
        thin_conflict_threshold=0,
        persist=False,
        **(extra or {}),
    )
    assert result.get("status") == "ok", result
    return result, save_fixed_run(models, result)
