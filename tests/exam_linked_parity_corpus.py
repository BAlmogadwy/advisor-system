"""Boards for the linked-exams parity tests: with no links, nothing may change.

Every board here is generated from a fixed seed. The digests in
``test_exam_linked_parity.py`` were recorded by running these same boards
through origin/master (d6d9c13), before linked exams existed, so a digest that
stops matching means a function now returns something master did not.

A digest covers the whole answer, key order included - the persisted run is
``json.dumps`` without ``sort_keys``, so the order is part of what is saved.
The only things taken out are the ones that legitimately differ between two
runs of master itself (a timestamp, a random group id, database ids of rows
created by the harness) or that this change adds on purpose (the schema
version and the top-level ``linked_exams`` key).

Nothing here imports the linked-exams code: the runners take the functions
under test as arguments, so the recording ran the very same code on master.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import random
from collections.abc import Callable, Iterator

# ── digests ─────────────────────────────────────────────────────────────────


def _plain(value):
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"Unexpected {type(value).__name__} in a digested value")


def digest(value) -> str:
    """The persisted bytes of ``value``, hashed: key order counts."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_plain)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── pure boards ─────────────────────────────────────────────────────────────

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed"]
PERIOD_NAMES = ["08:00-10:00", "11:00-13:00", "14:00-16:00"]


def _slots(days: list[str], periods: list[str]) -> list[dict]:
    return [
        {"index": index, "day": day, "period": period}
        for index, (day, period) in enumerate((day, period) for day in days for period in periods)
    ]


def _buckets(rng: random.Random, courses: list[str]):
    """Programme-plan buckets, and the reverse index the way the build makes it."""
    plan_term_buckets: dict[tuple[str, int], set[str]] = {}
    for number in range(rng.randint(0, 3)):
        size = rng.randint(2, min(4, len(courses)))
        plan_term_buckets[(f"P{number}", rng.randint(1, 8))] = set(rng.sample(courses, size))
    course_buckets: dict[str, list[tuple[str, int]]] = {}
    for key, members in sorted(plan_term_buckets.items()):
        for course in sorted(members):
            course_buckets.setdefault(course, []).append(key)
    return plan_term_buckets, course_buckets


def pure_boards(count: int = 48) -> Iterator[dict]:
    """Small random boards: clashes, buckets, credits, pins and too few slots."""
    for number in range(count):
        rng = random.Random(7100 + number)
        courses = [f"C{index:02d}" for index in range(rng.randint(5, 14))]
        days = DAY_NAMES[: rng.randint(1, 4)]
        periods = PERIOD_NAMES[: rng.randint(1, 3)]
        slots = _slots(days, periods)
        students = list(range(1, rng.randint(12, 70)))
        enrolled = {
            course: set(rng.sample(students, rng.randint(1, min(15, len(students)))))
            for course in courses
        }
        plan_term_buckets, course_buckets = _buckets(rng, courses)
        credits = rng.choice(["none", "empty", "mixed", "mixed"])
        credit_map = (
            None
            if credits == "none"
            else {}
            if credits == "empty"
            else {course: rng.choice([2, 3, 4]) for course in courses}
        )
        pinned = [
            {"course_code": course, **{k: v for k, v in rng.choice(slots).items() if k != "index"}}
            for course in rng.sample(courses, rng.randint(0, 2))
        ]
        preferred = (
            {course: rng.randrange(len(slots) + 1) for course in courses}
            if rng.random() < 0.4
            else None
        )
        yield {
            "courses": courses,
            "days": days,
            "periods": periods,
            "slots": slots,
            "enrolled": enrolled,
            "plan_term_buckets": plan_term_buckets,
            "course_buckets": course_buckets,
            "credit_map": credit_map,
            "pinned": pinned,
            "preferred": preferred,
            "seed": rng.choice([None, rng.randint(0, 10**6)]),
            "max_per_day": rng.choice([1, 2, 2, 3]),
            "threshold": rng.choice([0, 0, 2]),
            # A board of the same courses at random places, clashes and all.
            "placements": {
                course: (None if rng.random() < 0.12 else rng.randrange(len(slots)))
                for course in courses
            },
            "pinned_share": rng.random(),
        }


def graph(board: dict, build_conflict_graph, apply_thin_conflict_policy):
    conflicts, adj = build_conflict_graph(board["enrolled"])
    relaxed, thin_report = apply_thin_conflict_policy(board["enrolled"], adj, board["threshold"])
    return conflicts, relaxed, thin_report


def run_schedule(board: dict, schedule_fn: Callable, adj: dict) -> str:
    progress: list[tuple[int, int]] = []
    entries = schedule_fn(
        board["courses"],
        adj,
        board["slots"],
        enrolled_sets=board["enrolled"],
        max_per_day=board["max_per_day"],
        plan_term_buckets=board["plan_term_buckets"],
        course_buckets=board["course_buckets"],
        pinned=board["pinned"],
        credit_map=board["credit_map"],
        preferred_slots=board["preferred"],
        seed=board["seed"],
        on_placed=lambda done, total: progress.append((done, total)),
    )
    return digest({"entries": entries, "progress": progress})


def board_entries(board: dict) -> list[dict]:
    """The board's random placements as schedule entries, OVERFLOW included."""
    slots = board["slots"]
    entries = []
    overflow = len(slots)
    for course in board["courses"]:
        slot = board["placements"][course]
        if slot is None:
            entries.append(
                {
                    "course_code": course,
                    "course_identity": f"{course}::name",
                    "slot_index": overflow,
                    "day": "OVERFLOW",
                    "period": f"Extra-{overflow}",
                }
            )
            overflow += 1
        else:
            entries.append(
                {
                    "course_code": course,
                    "course_identity": f"{course}::name",
                    "slot_index": slot,
                    "day": slots[slot]["day"],
                    "period": slots[slot]["period"],
                }
            )
    return sorted(entries, key=lambda entry: (entry["slot_index"], entry["course_code"]))


def run_qa(board: dict, build_qa, attach, signature, thin_report: list[dict]) -> str:
    entries = board_entries(board)
    qa = build_qa(
        board["enrolled"],
        entries,
        max_per_day=board["max_per_day"],
        plan_term_buckets=board["plan_term_buckets"],
        credit_map=board["credit_map"],
    )
    attach(qa, board["enrolled"], entries, board["threshold"], thin_report)
    load = signature(
        board["enrolled"],
        entries,
        max_per_day=board["max_per_day"],
        plan_term_buckets=board["plan_term_buckets"],
        credit_map=board["credit_map"],
    )
    return digest({"qa": qa, "signature": load})


def _sections(board: dict) -> dict[str, list[dict]]:
    """Every course's students split into a male and a female section."""
    sections = {}
    for number, course in enumerate(board["courses"], 1):
        students = sorted(board["enrolled"][course])
        male = [sid for sid in students if sid % 2]
        female = [sid for sid in students if not sid % 2]
        rows = []
        for gender, members in (("F", female), ("M", male)):
            if members:
                rows.append(
                    {
                        "section": f"{gender}{number}",
                        "section_key": f"term-section:{gender}{number}",
                        "gender": gender,
                        "mapping_status": "mapped",
                        "student_count": len(members) * 4,
                        "preferred_room": "",
                    }
                )
        sections[course] = rows
    return sections


# Plenty of rooms of every size, so allocation never needs the CP-SAT repair
# and the parity stays about the pass, not about solver search order.
ROOMS = [
    {"room_code": f"{gender}-{size}-{copy}", "capacity": size, "section": gender}
    for gender in ("F", "M")
    for size in (20, 40, 80)
    for copy in range(8)
]


def run_post_pass(board: dict, assign_rooms, rebalance, context_factory, adj: dict) -> str:
    entries = board_entries(board)
    sections = _sections(board)
    rng = random.Random(board["seed"] or 17)
    pinned = {course for course in board["courses"] if rng.random() < board["pinned_share"] / 3}
    context = context_factory(64)
    assign_rooms(entries, sections, ROOMS, allocation_context=context)
    progress: list[tuple[int, int]] = []
    moves = rebalance(
        entries,
        sections,
        ROOMS,
        board["slots"],
        adj,
        board["plan_term_buckets"],
        board["course_buckets"],
        max_trials=40,
        pinned_courses=pinned,
        allocation_context=context,
        enrolled_sets=board["enrolled"],
        credit_map=board["credit_map"],
        max_per_day=board["max_per_day"],
        caller="parity",
        on_trial=lambda done, total: progress.append((done, total)),
    )
    return digest({"moves": moves, "entries": entries, "progress": progress})


# ── minimum-change repair boards ────────────────────────────────────────────


def _clash(*pairs) -> dict[str, dict[str, int]]:
    adj: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        adj.setdefault(a, {})[b] = 1
        adj.setdefault(b, {})[a] = 1
    return adj


def repair_boards(find_violations) -> Iterator[dict]:
    """The PR #112 brute-force corpora, then denser boards that widen and overflow."""
    rng = random.Random(20260923)
    names = ["A", "B", "C", "D", "E", "F"]
    kept = 0
    while kept < 120:
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.3])
        buckets = {("AI", 1): set(rng.sample(names, 2))} if rng.random() < 0.5 else None
        placements = {name: rng.randrange(6) for name in names}
        protected = {rng.choice(names)} if rng.random() < 0.5 else set()
        damaged, _ = find_violations(placements, adj, buckets, 2)
        if not 2 <= len(damaged - protected) <= 4:
            continue
        kept += 1
        yield {
            "placements": placements,
            "adj": adj,
            "buckets": buckets,
            "protected": protected,
            "slot_count": 6,
            "periods_per_day": 2,
        }

    rng = random.Random(4471)
    names = ["A", "B", "C", "D", "E"]
    kept = 0
    while kept < 80:
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.35])
        buckets = {("AI", 1): set(rng.sample(names, 3))} if rng.random() < 0.6 else None
        placements = {name: rng.randrange(4) for name in names}
        protected = set(rng.sample(names, rng.randrange(3)))
        if not find_violations(placements, adj, buckets, 2)[1]:
            continue
        kept += 1
        yield {
            "placements": placements,
            "adj": adj,
            "buckets": buckets,
            "protected": protected,
            "slot_count": 4,
            "periods_per_day": 2,
        }

    for number in range(30):
        rng = random.Random(5200 + number)
        names = [f"X{index}" for index in range(rng.randint(6, 9))]
        periods = rng.randint(1, 3)
        slot_count = periods * rng.randint(1, 3)
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.45])
        buckets = {
            ("P", term): set(rng.sample(names, rng.randint(2, 3)))
            for term in range(rng.randint(0, 2))
        }
        yield {
            "placements": {name: rng.randrange(slot_count) for name in names},
            "adj": adj,
            "buckets": buckets or None,
            "protected": set(rng.sample(names, rng.randrange(3))),
            "slot_count": slot_count,
            "periods_per_day": periods,
        }


def run_repair(board: dict, repair) -> str:
    result = repair(
        placements=board["placements"],
        adj=board["adj"],
        slot_count=board["slot_count"],
        periods_per_day=board["periods_per_day"],
        plan_term_buckets=board["buckets"],
        protected=board["protected"],
    )
    return digest(dataclasses.asdict(result))


# ── "Fix with fewest moves", the view's own path ────────────────────────────

FIX_DAYS = ["Sun", "Mon", "Tue"]
FIX_PERIODS = ["08:00-10:00", "13:00-15:00"]


def fix_boards() -> Iterator[dict]:
    """Boards the registrar has edited: drags, pins, exams already in OVERFLOW.

    The last twelve are crowded, with much of the board frozen, so repairs
    widen and send exams to OVERFLOW.
    """
    for number in range(36):
        rng = random.Random(8300 + number)
        crowded = number >= 24
        names = [
            f"K{index}" for index in range(rng.randint(8, 10) if crowded else rng.randint(4, 8))
        ]
        slot_count = len(FIX_DAYS) * len(FIX_PERIODS)
        density = 0.9 if crowded else 0.4
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < density])
        buckets = {("AI", 1): set(rng.sample(names, 2))} if rng.random() < 0.5 else {}
        board = []
        overflow = slot_count
        for name in names:
            slot = None if rng.random() < 0.15 else rng.randrange(slot_count)
            if slot is None:
                board.append(
                    {
                        "course_code": name,
                        "course_identity": name,
                        "course_name": f"{name} name",
                        "day": "OVERFLOW",
                        "period": f"Extra-{overflow}",
                        "slot_index": overflow,
                    }
                )
                overflow += 1
            else:
                board.append(
                    {
                        "course_code": name,
                        "course_identity": name,
                        "course_name": f"{name} name",
                        "day": FIX_DAYS[slot // 2],
                        "period": FIX_PERIODS[slot % 2],
                        "slot_index": slot,
                    }
                )
        source = {
            entry["course_code"]: (
                (entry["day"], entry["period"])
                if rng.random() < 0.7
                else (FIX_DAYS[rng.randrange(3)], FIX_PERIODS[rng.randrange(2)])
            )
            for entry in board
        }
        placed = [entry for entry in board if entry["day"] != "OVERFLOW"]
        pinned = [
            {"course_code": entry["course_code"], "day": entry["day"], "period": entry["period"]}
            for entry in rng.sample(placed, min(len(placed), rng.randint(0, 1)))
        ]
        yield {
            "board": board,
            "adj": adj,
            "buckets": buckets,
            "source": source,
            "pinned": pinned,
            "carried": sorted(
                rng.sample(names, rng.randint(3, 5) if crowded else rng.randint(0, 1))
            ),
        }

    # Two exams with nowhere legal to go, beside one already in OVERFLOW: each
    # is given the next Extra-n after the highest the board holds.
    frozen = [f"K{index}" for index in range(6)]
    board = [
        {
            "course_code": code,
            "course_identity": code,
            "course_name": f"{code} name",
            "day": FIX_DAYS[slot // 2],
            "period": FIX_PERIODS[slot % 2],
            "slot_index": slot,
        }
        for slot, code in enumerate(frozen)
    ]
    board += [
        {**board[0], "course_code": code, "course_identity": code, "course_name": f"{code} name"}
        for code in ("K6", "K7")
    ]
    board.append(
        {
            "course_code": "K8",
            "course_identity": "K8",
            "course_name": "K8 name",
            "day": "OVERFLOW",
            "period": "Extra-9",
            "slot_index": 9,
        }
    )
    yield {
        "board": board,
        "adj": _clash(("K6", "K7"), *[(code, mate) for code in ("K6", "K7") for mate in frozen]),
        "buckets": {},
        "source": {entry["course_code"]: (entry["day"], entry["period"]) for entry in board},
        "pinned": [],
        "carried": frozen,
    }


def run_fix(
    board: dict, views, patch: Callable[[str, object], None], extra: dict | None = None
) -> str:
    """Drive ``_minimum_change_schedule`` with the board's own graph and buckets.

    ``extra`` holds arguments master did not have, for the branch to pass.
    """
    captured: dict = {}

    def inputs(base_entries, days, periods, *_args, **_kwargs):
        return views._LoadedSolverInputs(
            meta_by_course={entry["course_code"]: entry for entry in base_entries},
            course_list=sorted(entry["course_code"] for entry in base_entries),
            enrolled_sets={},
            adj=board["adj"],
            plan_term_buckets=board["buckets"],
            course_buckets={},
            credit_map={},
            slots=_slots(days, periods),
        )

    def rebuild(**kwargs):
        captured.update(kwargs)
        return {"schedule": kwargs["schedule_raw"], **(kwargs.get("extra") or {})}

    patch("_loaded_solver_inputs", inputs)
    patch("_rebuild_loaded_schedule", rebuild)
    result = views._minimum_change_schedule(
        label="Parity",
        days=FIX_DAYS,
        periods=FIX_PERIODS,
        max_per_day=2,
        schedule_raw=[dict(entry) for entry in board["board"]],
        selected_courses=[entry["course_code"] for entry in board["board"]],
        pinned=board["pinned"],
        assign_rooms=False,
        seed=None,
        thin_conflict_threshold=0,
        source_placements=board["source"],
        carried_protection=board["carried"],
        **(extra or {}),
    )
    shown = {
        key: captured.get(key)
        for key in ("schedule_raw", "extra", "pinned", "rebalance_invigilators", "rebuild_mode")
    }
    return digest({"result": result, "rebuild": shown})


# ── a real database population ──────────────────────────────────────────────

POPULATION_DAYS = ["Sun", "Mon", "Tue", "Wed"]
POPULATION_PERIODS = ["08:00-10:00", "13:00-15:00"]
#: Plain courses, then one registrar code two study plans name differently: it
#: becomes two exams, "LX200 (1)" and "LX200 (2)".
POPULATION_COURSES = [f"LX{number}" for number in range(101, 110)]


def create_population(models, seed: int = 20260927) -> None:
    """Students, scraped registrations, plans and rooms for a small real build.

    Section rows get explicit primary keys: their ids appear in the result, and
    an id handed out by the database would depend on which tests ran first.
    """
    rng = random.Random(seed)
    for code in POPULATION_COURSES:
        models.Course.objects.create(
            course_code=code, description=code, credit_hours=rng.choice([2, 3, 4])
        )
    models.Course.objects.create(course_code="LX200", description="LX200", credit_hours=3)
    # At most three courses share a study-plan term, so four days always fit.
    for shift, program in enumerate(("AI", "CS")):
        for number, code in enumerate(POPULATION_COURSES):
            models.ProgrammeRequirement.objects.create(
                program=program,
                course_code=code,
                course_name=code,
                programme_term=(number + shift) % 4 + 1,
                is_online=code == "LX109",
            )
        models.ProgrammeRequirement.objects.create(
            program=program,
            course_code="LX200",
            course_name="Alpha" if program == "AI" else "Beta",
            programme_term=5,
        )
    sections: dict[tuple[str, str], object] = {}
    next_id = 90001
    for code in [*POPULATION_COURSES, "LX200"]:
        for label in ("M1", "F1"):
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
    for number in range(64):
        program = "AI" if number % 3 else "CS"
        gender = "M" if number % 2 else "F"
        student = models.Student.objects.create(
            student_id=770000 + number, program=program, section=gender
        )
        for code in sorted(rng.sample([*POPULATION_COURSES, "LX200"], rng.randint(2, 5))):
            models.StudentTermSection.objects.create(
                student_id=student.student_id,
                academic_year="1448",
                term="1",
                term_section=sections[(code, f"{gender}1")],
                source="scraper_timetable",
            )
    for gender in ("M", "F"):
        for size in (10, 20, 40):
            for copy in range(4):
                models.Room.objects.create(
                    room_code=f"R{gender}{size}-{copy}",
                    capacity=size,
                    section=gender,
                    building="B1" if copy % 2 else "B2",
                    floor=1,
                )


#: Keys that differ between two runs of master itself, or that linked exams
#: add on purpose; every other key of a result is part of the digest.
def comparable(result: dict) -> dict:
    result = json.loads(json.dumps(result, ensure_ascii=False, default=_plain))
    result.pop("schema_version", None)
    result.pop("linked_exams", None)
    result.pop("run_id", None)
    snapshot = (result.get("qa") or {}).get("enrolment_snapshot")
    if isinstance(snapshot, dict):
        snapshot["snapshot_timestamp"] = ""
    multistart = result.get("multistart")
    if isinstance(multistart, dict):
        for key in ("group_id", "schema_version"):
            multistart.pop(key, None)
    return result


def comparable_report(report: dict) -> dict:
    report = json.loads(json.dumps(report, ensure_ascii=False, default=_plain))
    report.pop("elapsed_seconds", None)
    report.pop("run_ids", None)
    for candidate in report.get("candidates", {}).values():
        candidate["payload"] = comparable(candidate["payload"])
    return report


def run_population(api, extra: dict | None = None) -> dict[str, str]:
    """Build, Check, Optimise, Fix and Multistart on the population: one digest each.

    ``api`` carries the functions under test, so master and the branch are
    driven by exactly the same calls; ``extra`` adds arguments master did not
    have to every one of them.
    """
    days, periods = POPULATION_DAYS, POPULATION_PERIODS
    pins = [{"course_code": "LX103", "day": "Mon", "period": periods[1]}]
    common = {"days": days, "periods": periods, "max_per_day": 2, **(extra or {})}
    build = api.build_exam_timetable(
        "Parity build",
        programs=["AI", "CS"],
        pinned=pins,
        seed=11,
        assign_rooms=True,
        rebalance_invigilators=True,
        thin_conflict_threshold=1,
        persist=False,
        **common,
    )
    digests = {"build": digest(comparable(build))}
    loaded = {
        **common,
        "schedule_raw": build["schedule"],
        "selected_courses": [entry["course_code"] for entry in build["schedule"]],
        "assign_rooms": True,
        "seed": 11,
        "thin_conflict_threshold": 1,
        "programs": ["AI", "CS"],
        "sections": [],
        "pinned": pins,
    }
    digests["check"] = digest(comparable(api.evaluate_exam_schedule(**loaded)))
    digests["optimise"] = digest(
        comparable(
            api.views._optimise_loaded_schedule(
                label="Parity optimise", save=lambda _label, _result: 1, **loaded
            )
        )
    )
    # Drag the first clash-free exam onto a neighbour's slot, then Fix it.
    placed = {entry["course_code"]: entry for entry in build["schedule"]}
    pinned_codes = {pin["course_code"] for pin in pins}
    edge = next(
        row
        for row in build["conflicts"]
        if not {row["course_a"], row["course_b"]} & pinned_codes
        and placed[row["course_a"]]["day"] != "OVERFLOW"
        and placed[row["course_b"]]["day"] != "OVERFLOW"
    )
    anchor = placed[edge["course_a"]]
    dragged = [
        {**entry, **{key: anchor[key] for key in ("day", "period", "slot_index")}}
        if entry["course_code"] == edge["course_b"]
        else entry
        for entry in build["schedule"]
    ]
    fixed = api.views._minimum_change_schedule(
        label="Parity fix",
        source_placements={code: (entry["day"], entry["period"]) for code, entry in placed.items()},
        carried_protection=[],
        save=lambda _label, _result: 1,
        **{**loaded, "schedule_raw": dragged},
    )
    digests["fix"] = digest(comparable(fixed))
    report = api.run_multistart(
        label="Parity multistart",
        programs=["AI", "CS"],
        pinned=pins,
        seeds=[3, 5],
        assign_rooms=True,
        thin_conflict_threshold=1,
        time_budget_s=600.0,
        **common,
    )
    digests["multistart"] = digest(comparable_report(api.report_to_dict(report)))
    return digests
