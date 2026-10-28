"""Read a hand-made timetable into an existing workspace scenario.

WHY THIS IS NOT ``term_sections.import_term_sections_from_csv``

That importer writes the registrar's own snapshot: it hard-codes ``scenario=None``
and so can only ever produce global sections. A scenario is a different object -
boards, per-course budgets and the student links the whole page counts from - and
none of it is in a timetable. So this importer does not create the scenario: it
fills one that ``generate_workspace_scenario`` has already scaffolded from the
roster. A scenario made empty instead would open reporting no clashes and no
affected students, because nobody is in it.

A COURSE IS A CODE **AND** A NAME

The same registrar code is a different course in different plans: CS112 is
PROGRAMMING I in the second-cohort plans and PROGRAMMING II in the first. The
scenario keys everything - budgets, student requests, sections - by
``planner_course_key``, so ``CS112::PROGRAMMING_I``. A section written under the
bare code would be created, drawn, and joined to nobody: no student request
matches it, so the board reports no clashes and no affected students. That is
the exact silent failure this module exists to avoid, and it has bitten this
codebase before (the instructor lookup, PRs #151/#152). The key is therefore
resolved against the scenario's own plan, never assumed, and a code that matches
two courses is refused until the sheet names which one.

WHAT IT WRITES, AND WHAT IT LEAVES ALONE

:func:`apply_plan` calls ``create_planned_section_placements`` once per section
and nothing else, so every guard the page's own create runs applies here too: one
section per label in the scenario, one meeting per day, the ENG101/ENG102 policy,
and the published-scenario refusal. The whole import is one transaction under the
scenario write lock, so a refusal half way leaves nothing behind and the sheet can
simply be fixed and re-run.

The instructor column is read and checked against the staff table but is NOT
written: who teaches a section is a pinned decision in ``SectionInstructor`` with
its own one-primary invariants, and half of that is worse than none.

THE MEETING COUNT IS THE COURSE'S, NOT THE PAGE'S FALLBACK

``infer_required_meeting_count`` answers for the page, where a course's first
section is dropped into one cell: with nothing placed yet it reads 3 meetings for
a 4-credit course and 1 for everything else. A 3-credit course meets twice
(``MEETING_PATTERNS``), so on a freshly scaffolded scenario that fallback would
refuse every ordinary section in the file. The pattern is computed here from the
course's credit hours instead and passed in, which keeps the check and fixes the
answer. Only the COUNT is checked: a course whose meetings are longer than the
pattern's (a 3-credit course taught as two 100-minute blocks) imports as written.

CREDIT HOURS COME FROM THE SCENARIO, NOT THE FILE

The budget row Generate wrote is the authority, and a ``credits`` column only
answers for a course the plan does not have. A sheet that disagrees with the
budget is reported rather than obeyed: the engine, the teacher load and the
rooming all re-derive from the budget, so the sheet cannot win quietly.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.models import (
    DeliveryBoard,
    Instructor,
    Room,
    ScenarioSectionBudget,
    SectionPlacement,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.timetable_autoplace import get_meeting_pattern
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_slot_config import (
    GRID_DAYS,
    drawn_column,
    effective_lab_slots,
    effective_lecture_slots,
    is_lab_meeting,
)

#: The columns a sheet must carry. Everything else about a meeting is optional
#: or derived; without these there is no meeting.
REQUIRED_COLUMNS = ("board", "course_code", "section", "day", "start_time", "end_time")

#: Read when present. ``course_name`` settles a code that names two courses;
#: ``credits`` is the only one that changes what is accepted.
OPTIONAL_COLUMNS = (
    "course_name",
    "course_number",
    "course_key",
    "room",
    "instructor",
    "capacity",
    "credits",
    "preferred_programme",
    "locked",
)

#: Columns that describe the section rather than the meeting. Read once, from
#: the section's first row, so every row of that section must agree on them.
SECTION_COLUMNS = (
    "course_name",
    "course_number",
    "course_key",
    "capacity",
    "credits",
    "preferred_programme",
    "locked",
)

#: ``locked`` values, read strictly: a column nobody can mistype into silence.
_YES = frozenset({"1", "true", "yes", "y"})
_NO = frozenset({"", "0", "false", "no", "n"})

#: HH:MM or H:MM, ASCII digits only - the same digits the grid parses times by
#: (``_TIME`` in ``timetable_slot_config``). An Arabic-Indic digit reads as no
#: time at all rather than as a different one. A single-digit hour is accepted
#: and written back padded: Excel exports nine o'clock as ``9:00``, and the
#: exports compare stored times as text, so an unpadded one would quietly drop
#: out of the room and instructor sheets.
_TIME = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")

#: Excel's plain "CSV (Comma delimited)" is the system codepage, not UTF-8, and
#: these sheets carry Arabic course names. Tried in order.
_ENCODINGS = ("utf-8-sig", "cp1256", "utf-16")

#: Seats for a section the sheet does not size, when the budget cannot say.
DEFAULT_CAPACITY = 40


def _text(value: Any) -> str:
    return str(value or "").strip()


def _minutes(value: str) -> int | None:
    match = _TIME.match(value)
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def _hhmm(value: str) -> str:
    """A read time written the way every stored time is written."""
    match = _TIME.match(value)
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else value


def _whole(value: str) -> int | None:
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number)


@dataclass
class Problem:
    """Something that stops the import. Carries the line so the sheet can be fixed."""

    line: int | None
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}" if self.line else self.message


@dataclass
class PlannedSection:
    """One section of the sheet, as ``create_planned_section_placements`` wants it."""

    board_label: str
    board_id: int
    course_code: str
    course_key: str
    course_name: str
    section: str
    capacity: int
    required_meetings: int
    meetings: list[dict[str, str]]
    preferred_programme: str = ""
    locked: bool = False
    instructors: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)


@dataclass
class ImportPlan:
    """What the apply would do, and why it would not."""

    scenario_id: int
    sections: list[PlannedSection] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.sections)

    @property
    def meeting_count(self) -> int:
        return sum(len(s.meetings) for s in self.sections)


def _read_rows(
    csv_path: str | Path,
) -> tuple[list[tuple[int, dict[str, str]]], list[Problem], list[str]]:
    """The sheet's rows as ``(line number, row)``, headers trimmed and lower-cased."""

    path = Path(csv_path)
    text = None
    for encoding in _ENCODINGS:
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return [], [Problem(None, f"Could not read {path.name}: {exc}")], []
    if text is None:
        return (
            [],
            [
                Problem(
                    None,
                    f"{path.name} is not text this can read. Save it from Excel as "
                    '"CSV UTF-8 (Comma delimited)".',
                )
            ],
            [],
        )

    reader = csv.DictReader(text.splitlines())
    headers = [_text(h).lower() for h in (reader.fieldnames or [])]
    rows = []
    for number, row in enumerate(reader, start=2):  # line 1 is the header
        clean = {_text(k).lower(): v for k, v in row.items() if k is not None}
        if any(_text(v) for v in clean.values()):  # Excel leaves trailing ,,,,, rows
            # The sheet's own line number, so a problem names the line the
            # person sees, not the position among the rows that survived.
            rows.append((number, clean))

    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], [Problem(None, f"The sheet needs these columns: {', '.join(missing)}.")], []
    if not rows:
        return [], [Problem(None, "The sheet has a header row and nothing under it.")], []
    unknown = [h for h in headers if h and h not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    return rows, [], unknown


def _resolve_course(
    budgets_by_code: dict[str, list[ScenarioSectionBudget]],
    *,
    code: str,
    stated_key: str,
    stated_name: str,
) -> tuple[str | None, ScenarioSectionBudget | None, str]:
    """The scenario's own key for this course, the budget behind it, and why not.

    The sheet's own ``course_key`` wins when it is given. Otherwise the code is
    looked up in the scenario's plan, and a code that names two courses there is
    settled by the sheet's ``course_name`` built into the same identity the
    scenario was built with - never by picking one.
    """

    if stated_key:
        # A key is CODE or CODE::NAME; one naming another course would create a
        # section coded one thing and joined to another's students.
        if stated_key.split("::", 1)[0] != code:
            return (
                None,
                None,
                (
                    f"course_key {stated_key!r} is not a key of {code}. A key is the code, "
                    "or the code and the course name joined by '::'."
                ),
            )
        candidates = budgets_by_code.get(code, [])
        exact = [b for b in candidates if _text(b.course_key).upper() == stated_key]
        return stated_key, (exact[0] if exact else None), ""

    candidates = budgets_by_code.get(code, [])
    if len(candidates) == 1:
        return _text(candidates[0].course_key).upper(), candidates[0], ""
    if len(candidates) > 1:
        if stated_name:
            built = planner_course_key(code, stated_name).upper()
            exact = [b for b in candidates if _text(b.course_key).upper() == built]
            if len(exact) == 1:
                return built, exact[0], ""
        names = ", ".join(sorted(_text(b.course_name) or _text(b.course_key) for b in candidates))
        return (
            None,
            None,
            (
                f"{code} is {len(candidates)} different courses in this scenario ({names}). "
                "Put the course_name of the one you mean in the sheet."
            ),
        )
    return code, None, ""


def build_plan(
    csv_path: str | Path,
    scenario_id: int,
    *,
    allow_offgrid: bool = False,
    prefer_programme: str = "",
    allow_existing: bool = False,
) -> ImportPlan:
    """Read the sheet and say what importing it would do. Writes nothing.

    ``prefer_programme`` marks every section of the sheet "mainly for" that
    programme, which a per-section ``preferred_programme`` column overrides.
    A sheet is usually one plan's own timetable, so the mark belongs to the
    file rather than to each row. It never costs a seat: it seats that
    programme's students first and steers the others to the course's other
    sections (``section_programme_preference``).
    """

    from core.services.section_programme_preference import (
        allowed_programmes,
        normalise_programme,
    )
    from core.services.section_programme_preference import (
        is_enabled as programme_preference_enabled,
    )
    from core.services.timetable_workspace import placed_section_labels

    plan = ImportPlan(scenario_id=scenario_id)
    try:
        scenario = TimetableScenario.objects.get(id=scenario_id)
    except TimetableScenario.DoesNotExist:
        plan.problems.append(Problem(None, f"No scenario {scenario_id}."))
        return plan
    if scenario.status == "published":
        plan.problems.append(
            Problem(None, f"Scenario {scenario_id} is published and is read-only.")
        )
        return plan

    rows, problems, unknown = _read_rows(csv_path)
    plan.problems.extend(problems)
    if unknown:
        plan.warnings.append(f"Columns read by nothing, ignored: {', '.join(sorted(unknown))}.")
    if not rows:
        return plan

    boards = {_text(b.label).upper(): b for b in DeliveryBoard.objects.filter(scenario=scenario)}
    if not boards:
        plan.problems.append(
            Problem(None, f"Scenario {scenario_id} has no boards; generate the scaffold first.")
        )
        return plan

    budgets_by_code: dict[str, list[ScenarioSectionBudget]] = defaultdict(list)
    for budget in ScenarioSectionBudget.objects.filter(scenario=scenario):
        budgets_by_code[_text(budget.course_code).upper()].append(budget)

    # Generate builds a timetable by default, so a scenario is usually NOT
    # empty. Importing beside an engine's sections gives the course twice the
    # supply it should have - the sheet's F1 does not collide with the engine's
    # S1 - and the meeting sync afterwards rewrites every scenario-owned
    # section, not only the imported ones. Refused unless the caller says it
    # means it.
    placed = SectionPlacement.objects.filter(board__scenario=scenario).count()
    if placed and not allow_existing:
        plan.problems.append(
            Problem(
                None,
                f"Scenario {scenario_id} already holds {placed} placement(s). Importing "
                "beside them would double the supply of every course the sheet repeats. "
                "Clear the board first, or pass --allow-existing.",
            )
        )
        return plan

    allowed = allowed_programmes(scenario)
    file_programme = normalise_programme(prefer_programme)
    if file_programme and file_programme not in allowed:
        plan.problems.append(
            Problem(
                None,
                f"{file_programme} is not a programme of this scenario. It has: "
                + (", ".join(allowed) or "none"),
            )
        )
        return plan
    if file_programme and not programme_preference_enabled():
        plan.warnings.append(
            f"The mark 'mainly for {file_programme}' is stored but ignored here: "
            "TIMETABLE_SECTION_PROGRAMME_PREFERENCE is off in this environment."
        )

    lectures = effective_lecture_slots(scenario)
    labs = effective_lab_slots(scenario)
    # Compared case-insensitively, stored as the room list spells it: the
    # exports and the rooming passes match a room's code as text, so "a101"
    # would drop out of the room sheets and read as free to a later pass.
    known_rooms = {_text(r.room_code).upper(): _text(r.room_code) for r in Room.objects.all()}
    known_staff = {
        _text(i.normalised_name) for i in Instructor.objects.all() if _text(i.normalised_name)
    }

    grouped: dict[tuple[str, str, str], list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for index, row in rows:
        board_label = _text(row.get("board"))
        code = _text(row.get("course_code")).upper()
        section = _text(row.get("section")).upper()
        if not board_label or not code or not section:
            plan.problems.append(
                Problem(index, "board, course_code and section are all required on every row.")
            )
            continue
        if board_label.upper() not in boards:
            plan.problems.append(
                Problem(
                    index,
                    f"No board named {board_label!r} on this scenario. "
                    f"It has: {', '.join(sorted(_text(b.label) for b in boards.values()))}.",
                )
            )
            continue
        grouped[(board_label.upper(), code, section)].append((index, row))

    unknown_rooms: Counter = Counter()
    unknown_staff: Counter = Counter()
    label_holders: dict[str, dict[str, set[int]]] = {}

    for (board_upper, code, section), entries in sorted(grouped.items()):
        board = boards[board_upper]
        first = entries[0][1]
        first_line = entries[0][0]
        meetings: list[dict[str, str]] = []
        instructors: list[str] = []
        seen_days: set[str] = set()
        fatal = False

        # These describe the SECTION, not the meeting, so they are read from its
        # first row - and every other row must agree, or the sheet is saying two
        # things. A `locked` on the second row alone would otherwise be dropped
        # in silence, and the next rebuild would delete the section.
        disagreed = sorted(
            column
            for column in SECTION_COLUMNS
            if len({_text(row.get(column)) for _, row in entries}) > 1
        )
        if disagreed:
            plan.problems.append(
                Problem(
                    first_line,
                    f"{code} {section}: every row of one section must agree on "
                    f"{', '.join(disagreed)}; these rows do not "
                    f"(lines {', '.join(str(line) for line, _ in entries)}).",
                )
            )
            continue

        key, budget, why = _resolve_course(
            budgets_by_code,
            code=code,
            stated_key=_text(first.get("course_key")).upper(),
            stated_name=_text(first.get("course_name")),
        )
        if key is None:
            plan.problems.append(Problem(first_line, why))
            continue
        policy = get_course_meeting_policy(code, key)

        for line, row in entries:
            day = _text(row.get("day")).upper()[:3]
            start, end = _text(row.get("start_time")), _text(row.get("end_time"))
            if day not in GRID_DAYS:
                plan.problems.append(
                    Problem(
                        line, f"day {_text(row.get('day'))!r} is not one of {', '.join(GRID_DAYS)}."
                    )
                )
                fatal = True
                continue
            start_min, end_min = _minutes(start), _minutes(end)
            if start_min is None or end_min is None:
                plan.problems.append(
                    Problem(
                        line,
                        f"{start or '(blank)'}-{end or '(blank)'} is not HH:MM, 24-hour, in 0-9.",
                    )
                )
                fatal = True
                continue
            if end_min <= start_min:
                plan.problems.append(Problem(line, f"{start}-{end} does not end after it starts."))
                fatal = True
                continue
            if day in seen_days:
                plan.problems.append(
                    Problem(line, f"{code} {section} already meets on {day}; one meeting per day.")
                )
                fatal = True
                continue
            seen_days.add(day)
            start, end = _hhmm(start), _hhmm(end)
            room = _text(row.get("room"))
            if room:
                known = known_rooms.get(room.upper())
                if known is None:
                    unknown_rooms[room] += 1
                else:
                    room = known
            name = _text(row.get("instructor"))
            if name:
                instructors.append(name)
                if name.strip().casefold() not in known_staff:
                    unknown_staff[name] += 1
            # A course under a meeting policy draws in its own table, whatever
            # the columns are (``is_on_grid(policy_meeting=True)``).
            if (
                not allow_offgrid
                and not policy
                and drawn_column(day, start, end, lectures, labs) is None
            ):
                kind = "lab" if is_lab_meeting(start, end) else "lecture"
                plan.problems.append(
                    Problem(
                        line,
                        f"{day} {start}-{end} starts at no {kind} column of this scenario, "
                        "so the meeting would be saved and never drawn. Set the scenario's "
                        "time slots to match the sheet, or pass --allow-offgrid.",
                    )
                )
                fatal = True
                continue
            meetings.append({"day": day, "start_time": start, "end_time": end, "room": room})

        if fatal or not meetings:
            continue

        stated_credits = _text(first.get("credits"))
        if budget is not None and int(budget.credit_hours or 0) > 0:
            credits = int(budget.credit_hours)
            if stated_credits and _whole(stated_credits) != credits:
                plan.warnings.append(
                    f"{code}: the sheet says {stated_credits} credits, the scenario's plan says "
                    f"{credits}. The plan wins - the engine and the rooming read it, not the sheet."
                )
        elif stated_credits:
            credits = _whole(stated_credits) or 0
            if credits <= 0:
                plan.problems.append(
                    Problem(
                        first_line,
                        f"{code}: credits {stated_credits!r} is not a whole number above 0.",
                    )
                )
                continue
        else:
            plan.problems.append(
                Problem(
                    first_line,
                    f"{code} is not in this scenario's plan, so its credit hours are unknown "
                    "and its meeting pattern cannot be checked. Give it a credits column, "
                    "or leave the course out.",
                )
            )
            continue
        if budget is None:
            plan.warnings.append(
                f"{code} has no budget row in this scenario: no student asked for it, so its "
                "sections will seat nobody and will not count towards the plan."
            )

        required = len(policy.durations) if policy else len(get_meeting_pattern(credits, code))
        if len(meetings) != required:
            plan.problems.append(
                Problem(
                    first_line,
                    f"{code} {section} has {len(meetings)} meeting(s); a {credits}-credit course "
                    f"needs {required}. Fix the sheet, or set credits for this course.",
                )
            )
            continue
        if policy:
            from core.services.timetable_section_policy import (
                SectionPolicyError,
                assert_policy_rows,
            )

            try:
                assert_policy_rows(policy, meetings)
            except SectionPolicyError as exc:
                plan.problems.append(Problem(first_line, f"{code} {section}: {exc}"))
                continue

        if key not in label_holders:
            label_holders[key] = placed_section_labels(scenario_id, key)
        if section in label_holders[key]:
            plan.problems.append(
                Problem(
                    first_line,
                    f"{code} {section} is already placed in this scenario. Remove it there "
                    "first, or give this one another label.",
                )
            )
            continue

        stated_capacity = _text(first.get("capacity"))
        budget_capacity = int(getattr(budget, "max_per_section", 0) or 0)
        if stated_capacity:
            capacity = _whole(stated_capacity)
            if capacity is None or capacity < 0:
                plan.problems.append(
                    Problem(first_line, f"capacity {stated_capacity!r} is not a whole number.")
                )
                continue
            if budget_capacity and capacity != budget_capacity:
                plan.warnings.append(
                    f"{code} {section}: the sheet seats {capacity}, the scenario's plan sizes it "
                    f"at {budget_capacity}. Check and Optimise judge it by the plan's number."
                )
        else:
            capacity = budget_capacity or DEFAULT_CAPACITY

        stated_lock = _text(first.get("locked")).lower()
        if stated_lock not in _YES and stated_lock not in _NO:
            plan.problems.append(
                Problem(
                    first_line,
                    f"locked {_text(first.get('locked'))!r} is not yes or no.",
                )
            )
            continue
        locked = stated_lock in _YES

        stated_programme = _text(first.get("preferred_programme"))
        programme = normalise_programme(stated_programme) if stated_programme else file_programme
        if programme and programme not in allowed:
            plan.problems.append(
                Problem(
                    first_line,
                    f"{code} {section}: {programme} is not a programme of this scenario. "
                    "It has: " + (", ".join(allowed) or "none"),
                )
            )
            continue

        plan.sections.append(
            PlannedSection(
                board_label=_text(board.label),
                board_id=board.id,
                course_code=code,
                course_key=key,
                course_name=(
                    _text(first.get("course_name"))
                    or _text(getattr(budget, "course_name", ""))
                    or code
                ),
                section=section,
                capacity=capacity,
                required_meetings=required,
                meetings=meetings,
                preferred_programme=programme,
                locked=locked,
                instructors=instructors,
                lines=[line for line, _ in entries],
            )
        )

    labels: Counter = Counter((s.course_key, s.section) for s in plan.sections)
    for (key, section), count in sorted(labels.items()):
        if count > 1:
            plan.problems.append(
                Problem(
                    None,
                    f"{key} {section} appears on {count} boards. A section label is used once "
                    "per course in a scenario, whichever board holds it.",
                )
            )
    if unknown_rooms:
        plan.warnings.append(
            "Rooms not in the room list (saved as typed, left out of the room reports): "
            + ", ".join(f"{room} x{n}" for room, n in sorted(unknown_rooms.items()))
        )
    if unknown_staff:
        plan.warnings.append(
            "Teachers not in the staff list: "
            + ", ".join(f"{name} x{n}" for name, n in sorted(unknown_staff.items()))
        )
    if any(s.instructors for s in plan.sections):
        plan.warnings.append(
            "The instructor column is checked but NOT written: who teaches a section is a "
            "separate pinned decision. The scenario assigns its own teachers."
        )
    return plan


@dataclass
class SectionOutcome:
    """What became of one section of the plan."""

    section: PlannedSection
    placements: int = 0
    instructor_clashes: int = 0
    critical: int = 0


class ImportFailed(RuntimeError):
    """The import was rolled back. Carries the section it stopped on."""

    def __init__(self, section: PlannedSection | None, reason: str) -> None:
        where = (
            f"{section.course_code} {section.section} on {section.board_label}: " if section else ""
        )
        super().__init__(f"{where}{reason}")
        self.section = section
        self.reason = reason


def apply_plan(plan: ImportPlan) -> dict[str, Any]:
    """Write the plan, all of it or none of it.

    One transaction under the scenario write lock, the same lock the page's own
    writes take: a section refused half way rolls the whole import back, so the
    sheet is fixed and re-run rather than reconciled against a half-written
    board. A clash never refuses anything - a manual timetable is taken as given
    - so the clashes are counted and reported instead.
    """

    from core.services.timetable_board_persistence import sync_meetings_from_placements
    from core.services.timetable_mutation_guard import (
        conflicting_planner_job,
        scenario_resource_ids,
        scenario_write_scope,
    )
    from core.services.timetable_workspace import create_planned_section_placements

    if plan.problems:
        raise ValueError("The plan has problems; nothing was written.")
    if not plan.sections:
        raise ValueError("The plan has no sections; nothing was written.")

    outcomes: list[SectionOutcome] = []
    with scenario_write_scope(plan.scenario_id) as (scenarios, related):
        scenario = next((row for row in scenarios if row.pk == plan.scenario_id), None)
        if scenario is None:
            raise ImportFailed(None, f"scenario {plan.scenario_id} is gone.")
        if scenario.status == "published":
            raise ImportFailed(None, "the scenario is published and is read-only.")
        job = conflicting_planner_job(related, scenario_resource_ids(plan.scenario_id)[1])
        if job is not None:
            raise ImportFailed(
                None, f"a {job.mode} job is {job.status} on this scenario. Wait for it to finish."
            )

        for section in plan.sections:
            try:
                result = create_planned_section_placements(
                    section.board_id,
                    course_code=section.course_code,
                    course_key=section.course_key,
                    course_name=section.course_name,
                    section_label=section.section,
                    capacity=section.capacity,
                    meetings=section.meetings,
                    required_meetings=section.required_meetings,
                )
            except Exception as exc:  # rolled back with everything before it
                raise ImportFailed(section, f"{type(exc).__name__}: {exc}") from exc
            if section.preferred_programme:
                # Only a scenario's own section may carry the mark, and this
                # create made one (ck_term_sections_pref_scenario_only).
                term_section = result["term_section"]
                term_section.preferred_programme = section.preferred_programme
                term_section.save(update_fields=["preferred_programme"])
            if section.locked:
                # A lock freezes the day and the time, never the room: a rebuild
                # keeps these meetings where the sheet put them and may still
                # re-room them (``lock_time_only_contract``).
                SectionPlacement.objects.filter(
                    pk__in=[p.pk for p in result.get("placements") or []]
                ).update(is_locked=True)
            backstop = result.get("instructor_clash_backstop") or {}
            outcomes.append(
                SectionOutcome(
                    section=section,
                    placements=len(result.get("placements") or []),
                    instructor_clashes=len(backstop.get("involving_this_section") or []),
                    critical=sum(
                        int(v.get("critical_count") or 0)
                        for v in (result.get("validations") or [])
                        if isinstance(v, dict)
                    ),
                )
            )
        synced = sync_meetings_from_placements(plan.scenario_id)

    return {
        "outcomes": outcomes,
        "sections_created": len(outcomes),
        "placements": sum(o.placements for o in outcomes),
        "sections_with_instructor_clashes": sum(1 for o in outcomes if o.instructor_clashes),
        "critical_conflicts": sum(o.critical for o in outcomes),
        "meeting_sync": synced,
    }
