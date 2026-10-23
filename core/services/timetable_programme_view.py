"""
core/services/timetable_programme_view.py
Which placements a programme's per-plan file shows, and as what.

One rule for the per-plan export and the workspace's programme lens (§7.4,
sub-PR 6b; owner file rule 2026-10-21). A placement of a course in programme
P's plan sits on one plan-term sheet of P's file: its course's plan term, or,
for this term's mapped electives, the slot term of its board. Per sheet and
course, the sections are classified from the plan lens's seating
(:func:`classify_course_sections`) as ``main``, ``mixed`` or ``other``; a
section seating none of P's students is not in the file. A placement whose
course is in no sheet of P's plan is ``outside`` when the lens allocates its
section to P (or to nobody): the file lists its course as placed, on the
coverage sheet, and never draws it. The SHARED search narrows each sheet's
sections *before* they are classified, so it can change a role, not only
drop one.

:func:`programme_placement_roles` is that rule for one programme: the export
draws its workbooks from it, and :func:`programme_view` answers it for every
board programme on ``quality/?lens=1`` (the lens's ``programme_view``).

The export's helpers below moved here verbatim (the export re-imports every
name). This module imports neither OR-Tools nor the export.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from core.models import (
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.section_programme_preference import normalise_programme

# ── names and course keys ──────────────────────────────────────────────────


def _norm(value: object | None) -> str:
    return str(value or "").replace("\u00a0", " ").strip()


def _upper(value: object | None) -> str:
    return _norm(value).upper()


def _course_key_from_values(course_code: object | None, course_name: object | None = None) -> str:
    return planner_course_key(course_code, course_name)


def _requirement_course_key(req: ProgrammeRequirement) -> str:
    return _course_key_from_values(req.course_code, req.course_name)


def _budget_course_key(budget: ScenarioSectionBudget) -> str:
    return _norm(budget.course_key) or _course_key_from_values(
        budget.course_code,
        budget.course_name,
    )


def _term_section_course_key(section) -> str:
    if not section:
        return ""
    return _norm(getattr(section, "course_key", "")) or _course_key_from_values(
        getattr(section, "course_code", ""),
        getattr(section, "course_name", ""),
    )


def _course_code_from_key(course_key: str) -> str:
    return _upper(course_key.split("::", 1)[0])


# ── the scenario's programmes and courses ──────────────────────────────────


def get_scenario_programs(scenario: TimetableScenario) -> list[str]:
    """Return the unique list of programs targeted by a scenario's boards.

    Boards may carry comma-separated program lists (e.g. ``"DS,AI,AI2,DS2"``).
    Programs are returned in board ``display_order`` then alphabetical order
    of first appearance, so DS,AI,AI2,DS2 stays in the registrar's intended
    sequence rather than shuffling alphabetically.
    """
    return _board_programmes(_scenario_boards(scenario))


def _scenario_boards(scenario: TimetableScenario) -> list[DeliveryBoard]:
    """The scenario's boards in the order its programmes are read."""
    return list(DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order"))


def _board_programmes(boards: list[DeliveryBoard]) -> list[str]:
    """Each board's comma-separated programmes, first appearance first."""
    seen: list[str] = []
    for b in boards:
        for token in (b.program or "").split(","):
            p = token.strip()
            if p and p not in seen:
                seen.append(p)
    return seen


def scenario_course_keys(scenario: TimetableScenario) -> set[str]:
    """Every course the scenario budgets or has a section of, by planner key:
    the courses a plan's elective slots may be filled by this term."""
    course_keys = {
        _budget_course_key(b) for b in ScenarioSectionBudget.objects.filter(scenario=scenario)
    } | {_term_section_course_key(ts) for ts in TermSection.objects.filter(scenario=scenario)}
    course_keys.discard("")
    return course_keys


# ── the lens rows the SHARED token and the allocation read ─────────────────


def _section_passes_search(
    term_section_id: int | None,
    plan_lens: dict[str, Any],
    shared_only: bool,
) -> bool:
    """The ``SHARED`` search token: only sections the lens shares. A section
    with no lens row passes."""
    section_lens = (plan_lens.get("sections") or {}).get(str(term_section_id or ""))
    if not section_lens:
        return True
    return not (shared_only and not section_lens.get("shared"))


def _section_allocated_to_plan(
    term_section_id: int | None,
    program: str,
    plan_lens: dict[str, Any],
) -> bool:
    """The lens's demand allocation gives this section to ``program`` (or to
    nobody in particular). A section with no lens row passes."""
    section_lens = (plan_lens.get("sections") or {}).get(str(term_section_id or ""))
    if not section_lens:
        return True
    filter_plans = {_upper(plan) for plan in section_lens.get("filter_plans") or []}
    return _upper(program) in filter_plans if filter_plans else True


def _section_matches_plan_filter(
    term_section_id: int | None,
    program: str,
    plan_lens: dict[str, Any],
    shared_only: bool,
) -> bool:
    """Search and allocation together; used for out-of-plan placements only
    (in-plan ones are kept by who sits in them, see
    :func:`classify_course_sections`)."""
    return _section_passes_search(
        term_section_id, plan_lens, shared_only
    ) and _section_allocated_to_plan(term_section_id, program, plan_lens)


# ── who sits in each section (owner 2026-10-21) ──────────────────────────────

#: R1: a section is MAIN for programme P when at least this share of the
#: students the evaluator seats in it are P's (or it is marked for P).
MAIN_SECTION_SHARE = 0.70

ROLE_MAIN = "main"  # R1 (mark or share), or R2's no-one-seated fallback
ROLE_MIXED = "mixed"  # R2: no section is main, so the one seating most P stands in
ROLE_OTHER = "other"  # R4: not main, but seats at least one P student
#: Not in the programme's plan, kept by SHARED and the lens's allocation: the
#: file lists its course as placed (the coverage sheet), never draws it.
ROLE_OUTSIDE = "outside"


@dataclass(frozen=True)
class SectionSeat:
    """One section's role in one programme's file, and the seating behind it."""

    role: str
    programme_count: int  # the programme's students the evaluator seats here
    total: int  # everyone it seats here, UNKNOWN programme included


def _lens_seating(
    plan_lens: dict[str, Any], term_section_id: int, program: str
) -> tuple[int, int, str]:
    """``(program's students seated, everyone seated, mark)`` for one section.

    Read only from the lens row: ``actual_plans`` is the evaluator's seating
    (the course payload's ``plans`` is demand, not seating), and the row
    carries ``preferred_programme`` only under the flag, so with it off a
    stored mark is inert here as everywhere else. A section with no row
    (another scenario's, or a registrar section placed on our boards) seats
    nobody the workspace shows, so it reads as empty.
    """
    row = (plan_lens.get("sections") or {}).get(str(term_section_id))
    if not isinstance(row, dict):
        return 0, 0, ""
    target = normalise_programme(program)
    count = sum(
        int(n or 0)
        for plan, n in (row.get("actual_plans") or {}).items()
        if normalise_programme(plan) == target
    )
    mark = normalise_programme(row.get("preferred_programme"))
    return count, int(row.get("actual_total") or 0), mark


def classify_course_sections(
    program: str, section_ids: list[int], plan_lens: dict[str, Any]
) -> dict[int, SectionSeat]:
    """Each section's role for ``program`` in one course on one plan-term sheet.

    ``section_ids`` are the course's sections on that sheet in grid order
    (label, then id). A section missing from the result is not in the file.

    * R1 main: marked for the programme, or at least ``MAIN_SECTION_SHARE`` of
      the students seated in it are the programme's. Literal: a section marked
      for another programme that is 70% this one's is main here too, so one
      section may be main in two programmes' files.
    * R2 no course disappears: with the programme seated in the course but no
      main section, the section seating most of its students (then the higher
      share, then grid order) is main, labelled mixed. With none of its
      students seated anywhere in the course, the sections the lens allocates
      to it (and any marked for it) are main, as before seating counted.
    * R4 other: any other section seating at least one of its students.
    """
    target = normalise_programme(program)
    seats = {ts_id: _lens_seating(plan_lens, ts_id, program) for ts_id in section_ids}

    def marked(mark: str) -> bool:
        return bool(target) and mark == target

    out: dict[int, SectionSeat] = {}
    if not any(count for count, _total, _mark in seats.values()):
        for ts_id, (count, total, mark) in seats.items():
            if marked(mark) or _section_allocated_to_plan(ts_id, program, plan_lens):
                out[ts_id] = SectionSeat(ROLE_MAIN, count, total)
        return out

    for ts_id, (count, total, mark) in seats.items():
        if marked(mark) or (total and count / total >= MAIN_SECTION_SHARE):
            out[ts_id] = SectionSeat(ROLE_MAIN, count, total)
    if not out:
        grid_order = {ts_id: idx for idx, ts_id in enumerate(section_ids)}
        best = max(
            (ts_id for ts_id, (count, _total, _mark) in seats.items() if count),
            key=lambda ts_id: (
                seats[ts_id][0],
                seats[ts_id][0] / max(seats[ts_id][1], 1),
                -grid_order[ts_id],
            ),
        )
        out[best] = SectionSeat(ROLE_MIXED, seats[best][0], seats[best][1])
    for ts_id, (count, total, _mark) in seats.items():
        if ts_id not in out and count:
            out[ts_id] = SectionSeat(ROLE_OTHER, count, total)
    return out


# ── which plan-term sheet a placement belongs on ───────────────────────────


def get_plan_term_map(program: str) -> dict[str, int]:
    """Return ``{course_code -> programme_term}`` for one program's plan.

    Course codes are upper-cased so that comparisons against
    ``term_section.course_code`` are case-insensitive.  Duplicate rows in the
    requirement table (the same course appearing more than once for the same
    program) keep the smallest term — a defensive choice that lands the
    course earliest in the plan view.
    """
    out: dict[str, int] = {}
    for r in ProgrammeRequirement.objects.filter(program=program):
        if not r.course_code or r.programme_term is None:
            continue
        course_key = _requirement_course_key(r)
        if course_key not in out or (r.programme_term < out[course_key]):
            out[course_key] = r.programme_term
    return out


def get_plan_elective_terms(
    program: str,
    scenario: TimetableScenario,
    course_keys: set[str],
    plan_map: dict[str, int],
) -> tuple[dict[str, set[int]], dict[str, list[str]]]:
    """Place this term's real electives on the plan term of the slot they fill.

    A plan lists elective *slots* (AI1, AI2, ...), never the courses; the
    department's ElectiveTermMapping says which real courses fill each slot this
    term (e.g. 1448 T2: AI2 -> AI441, AI3 -> AI462). Without this the export
    filed those sections as "not in this plan" and no term sheet showed them.
    Build applies the same principle (a real elective inherits its slot's term);
    the mappings are read exactly as the elective resolver reads them.

    Returns ``(elective_terms, fills)``: ``{course key: {slot plan terms}}`` for
    every mapped elective the scenario actually has - a section on any of those
    terms is in place, so DS482 filling DS1 (T8) and DS3 (T10) is right on
    either board - and ``{slot key: [course keys filling it]}``. A course whose
    code is already in the plan keeps its own row and term.
    """
    from core.services.elective_resolver import elective_courses_by_placeholder

    mapped = elective_courses_by_placeholder(
        program, academic_year=scenario.academic_year, term=scenario.term
    )
    if not mapped:
        return {}, {}
    codes_by_slot = {_upper(slot): {_upper(c) for c in codes} for slot, codes in mapped.items()}

    slot_rows: dict[str, tuple[str, int]] = {}
    for r in ProgrammeRequirement.objects.filter(program=program):
        slot = _upper(r.course_code)
        if slot in codes_by_slot and r.programme_term is not None:
            slot_rows[slot] = (_requirement_course_key(r), r.programme_term)

    plan_codes = {_course_code_from_key(key) for key in plan_map}
    keys_by_code: dict[str, set[str]] = defaultdict(set)
    for key in course_keys:
        keys_by_code[_course_code_from_key(key)].add(key)

    elective_terms: dict[str, set[int]] = defaultdict(set)
    fills: dict[str, list[str]] = defaultdict(list)
    for slot, (slot_key, slot_term) in sorted(slot_rows.items(), key=lambda kv: kv[1][1]):
        for code in sorted(codes_by_slot[slot] - plan_codes):
            for key in sorted(keys_by_code.get(code, ())):
                elective_terms[key].add(slot_term)
                fills[slot_key].append(key)
    return dict(elective_terms), dict(fills)


def _classify_placement_term(
    course_key: str, board_term: int | None, plan_map: dict[str, int]
) -> tuple[int | None, str]:
    """Decide which sheet a placement belongs to and tag its plan-vs-board status.

    Returns ``(plan_term, status)`` where ``status`` is one of:

    * ``"matched"`` — the course is in this program's plan AND the board's
      ``nominal_term`` matches the plan term (correctly placed).
    * ``"misplaced"`` — the course is in the plan but the board term differs
      (e.g. AI113 in DS2's plan at term 5 but on the scenario's term-3 board).
    * ``"out-of-plan"`` — the course is not in this program's plan at all
      (will be skipped when filtering, but kept for the audit sheet).
    """
    key = _norm(course_key)
    plan_term = plan_map.get(key)
    if plan_term is None:
        return None, "out-of-plan"
    if board_term is None or board_term == plan_term:
        return plan_term, "matched"
    return plan_term, "misplaced"


def _placement_plan_term(
    course_key: str,
    board_term: int | None,
    plan_map: dict[str, int],
    elective_terms: dict[str, set[int]],
) -> tuple[int | None, str]:
    """The plan-term sheet a placement belongs on, and its plan-vs-board status.

    A mapped elective is in place on any of its slots' terms (see
    :func:`get_plan_elective_terms`); every other course goes by
    :func:`_classify_placement_term`. One decision for the classification
    pre-pass and the placement loop, so the two cannot diverge.
    """
    slot_terms = elective_terms.get(course_key)
    if slot_terms and board_term in slot_terms:
        return board_term, "matched"
    if slot_terms:
        return min(slot_terms), "misplaced"
    return _classify_placement_term(course_key, board_term, plan_map)


def programme_context(
    program: str, scenario: TimetableScenario, course_keys: set[str]
) -> tuple[dict[str, int], dict[str, set[int]], dict[str, list[str]]]:
    """``(plan_map, elective_terms, elective_fills)`` for one programme's file.

    The plan's terms, with this term's mapped electives on their slots' terms
    (a course already in the plan keeps its own row and term): see
    :func:`get_plan_term_map` and :func:`get_plan_elective_terms`.
    """
    plan_map = get_plan_term_map(program)
    elective_terms, elective_fills = get_plan_elective_terms(
        program, scenario, course_keys, plan_map
    )
    plan_map = {**{key: min(terms) for key, terms in elective_terms.items()}, **plan_map}
    return plan_map, elective_terms, elective_fills


# ── the roles of one programme's placements ────────────────────────────────


@dataclass(frozen=True)
class ProgrammeRoles:
    """What one programme's file shows.

    ``roles`` maps each placement the file draws (``main``, ``mixed``,
    ``other``) or lists (``outside``) to its role; a placement it neither draws
    nor lists has none. ``seats`` holds, per plan-term sheet, each kept
    section's :class:`SectionSeat` (the export's blocks and cell notes).
    """

    roles: dict[int, str]
    seats: dict[int, dict[int, SectionSeat]]


def programme_placement_roles(
    program: str,
    placements: list[SectionPlacement],
    *,
    plan_map: dict[str, int],
    elective_terms: dict[str, set[int]],
    board_terms: dict[int, int],
    plan_lens: dict[str, Any],
    shared_only: bool = False,
) -> ProgrammeRoles:
    """Each placement's role in ``program``'s per-plan file.

    The export's order of work: each course's sections on each plan-term
    sheet are classified once, from the lens's seating, before any placement
    is kept; SHARED narrows a sheet's sections first, so it can change a role,
    not only drop one. An in-plan placement takes its own sheet's role (an
    elective on two slot terms is judged on each sheet), or none when the sheet
    keeps no seat for its section. An out-of-plan placement is ``outside``
    when it passes SHARED and the lens allocates its section to ``program``
    (or to nobody). ``board_terms`` maps a board id to its ``nominal_term``
    (0 for none); ``plan_map`` and ``elective_terms`` come from
    :func:`programme_context`. The text search is not a role: the export
    applies it to what this keeps.
    """
    plan_terms: dict[int, int | None] = {}
    sheet_sections: dict[tuple[int, str], dict[int, str]] = defaultdict(dict)
    for p in placements:
        key = _term_section_course_key(p.term_section)
        plan_term, _status = _placement_plan_term(
            key, board_terms.get(p.board_id), plan_map, elective_terms
        )
        plan_terms[p.id] = plan_term
        if plan_term is None:
            continue
        if not _section_passes_search(p.term_section_id, plan_lens, shared_only):
            continue
        sheet_sections[(plan_term, key)][p.term_section_id] = p.term_section.section or "?"
    section_seats: dict[int, dict[int, SectionSeat]] = defaultdict(dict)
    for (plan_term, _key), labels in sheet_sections.items():
        grid_order = [
            ts_id for ts_id, _label in sorted(labels.items(), key=lambda kv: (kv[1], kv[0]))
        ]
        section_seats[plan_term].update(classify_course_sections(program, grid_order, plan_lens))

    roles: dict[int, str] = {}
    for p in placements:
        plan_term = plan_terms[p.id]
        if plan_term is None:
            if _section_matches_plan_filter(p.term_section_id, program, plan_lens, shared_only):
                roles[p.id] = ROLE_OUTSIDE
            continue
        seat = section_seats.get(plan_term, {}).get(p.term_section_id)
        if seat is not None:
            roles[p.id] = seat.role
    return ProgrammeRoles(roles=roles, seats=dict(section_seats))


# ── the workspace's programme view (contract B) ────────────────────────────

#: The order of a programme's role lists in :func:`programme_view`.
ROLE_ORDER = (ROLE_MAIN, ROLE_MIXED, ROLE_OTHER, ROLE_OUTSIDE)


def _role_lists(roles: dict[int, str]) -> dict[str, list[int]]:
    """``{role: [placement ids, ascending]}`` in ``ROLE_ORDER``; empty lists left out."""
    out: dict[str, list[int]] = {}
    for role in ROLE_ORDER:
        ids = sorted(pid for pid, placed in roles.items() if placed == role)
        if ids:
            out[role] = ids
    return out


def programme_view(scenario: TimetableScenario, plan_lens: dict[str, Any]) -> dict[str, Any]:
    """What each board programme's per-plan file shows, for the workspace.

    * ``programmes``: the export's set (:func:`get_scenario_programs`), the
      board programmes in board order, upper-cased. A programme that only the
      students or ``scenario.programs`` name has no file, so it is not here.
    * ``roles`` and ``shared_roles``: ``{P: {role: [placement ids]}}``, the
      export's classification without and with SHARED (which can change a
      role, so a reader never intersects one with the shared sections); ids
      ascending, empty lists left out, every programme present.
    * ``placement_ids``: every placement of the scenario at this read, so a
      placement with no role (not in P's file) is told from one this read
      never saw (unknown).
    * ``seating_assessed``: the lens's seating was evaluated; false, the roles
      follow the lens's allocation and the marks, as the export's do then.

    ``plan_lens`` is this read's lens (its ``sections`` and ``seating``). Reads
    the boards, the placements and the course keys once, then each programme's
    plan rows and elective mappings (:func:`programme_context`).
    """
    boards = _scenario_boards(scenario)
    board_terms = {board.id: (board.nominal_term or 0) for board in boards}
    placements = list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("term_section")
        .order_by("day", "start_time")
    )
    course_keys = scenario_course_keys(scenario)
    # The page names a programme by its upper-case code. Board programmes are
    # stored upper-case (the board form upper-cases them); were one spelt two
    # ways, the export would write two files and the view reads the first.
    spellings: dict[str, str] = {}
    for program in _board_programmes(boards):
        spellings.setdefault(_upper(program), program)
    roles: dict[str, dict[str, list[int]]] = {}
    shared_roles: dict[str, dict[str, list[int]]] = {}
    for code, program in spellings.items():
        plan_map, elective_terms, _fills = programme_context(program, scenario, course_keys)
        for shared_only, target in ((False, roles), (True, shared_roles)):
            target[code] = _role_lists(
                programme_placement_roles(
                    program,
                    placements,
                    plan_map=plan_map,
                    elective_terms=elective_terms,
                    board_terms=board_terms,
                    plan_lens=plan_lens,
                    shared_only=shared_only,
                ).roles
            )
    return {
        "programmes": list(spellings),
        "roles": roles,
        "shared_roles": shared_roles,
        "placement_ids": sorted(p.id for p in placements),
        "seating_assessed": bool(plan_lens["seating"]["assessed"]),
    }
