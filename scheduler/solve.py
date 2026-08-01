"""The planner — CP-SAT over student conflict, not over rooms.

Measured on the live cohorts, this is what a solver here actually has to fix:

* hard constraints are **trivial** — naive first-fit satisfies all eight;
* students are **catastrophically broken** — 96–100% of them clash.

So the objective is student conflict, and everything else is a side condition.

**A correction to an earlier reading of the data.** This file used to claim rooms
were saturated, that first-fit hit 100% of the theoretical maximum, and that the
unroomed count was therefore a proven floor. That was wrong on every cohort
measured since. The male estate runs at **53% utilisation** while still leaving
meetings unroomed, because "how many rooms exist" was never the binding question:
a room must also be **big enough** and **open to the programme**. Ten of its
unroomed meetings can never be roomed at any time by any timetable (four CS
courses need labs seating 26–35; both lab rooms seat 25), and the rest were the
model's own doing — see the Hall-condition constraint below, and
`scheduler.rooms` for the decomposition that separates the two.

**Why this is fast where the old engine was not.** The old evaluator re-seated all
390 students to score a single candidate move: 22 ms, times ~10⁴ moves, is 3.7
minutes of pure scoring per click. Here a move is scored against a precomputed
course-pair conflict matrix — an O(1) lookup built once from demand. Exact seating
is used to *confirm* a finished board, never inside the loop.

**The objective.** For two courses sharing students, a student clashes only if the
sections they end up in collide. With `na` and `nb` sections spread over the week,
a colliding section pair costs an expected `shared / (na*nb)` students. Minimising
the sum of those expectations therefore rewards **spreading sibling sections**,
which is exactly the lever the timetable has. Treating a course as one block would
instead demand that *every* section of A avoid *every* section of B — far more
constrained than reality, and unsatisfiable at this density.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from itertools import combinations

from scheduler.domain import DeliveryMode, Snapshot
from scheduler.domain.board import Board, Placement
from scheduler.domain.calendar import Day, TimeWindow

#: Expected-clash weights are fractional; CP-SAT is integral. Scale, then round.
_SCALE = 1000

#: A room shortfall is priced by the room-TIME it leaves uncovered, normalised to
#: one ordinary lecture. Without a normaliser the price would depend on where the
#: grid happens to cut the day rather than on anything about rooms.
_REFERENCE_MEETING_MINUTES = 75

#: A course pair shared by fewer students than this is not worth a variable.
#: The gap term is quadratic in sections, so it is spent on the pairs that
#: actually shape a student's week.
_MIN_SHARED_FOR_GAP_TERM = 5

#: How long a break still counts as "back to back". This grid runs 09:00-10:15
#: then 10:30, so consecutive teaching slots carry a 15-minute changeover; the
#: next real gap is 55 minutes, to the afternoon.
_ADJACENT_GAP_MINUTES = 20

#: Ceiling on the union-closure of room-compatibility families. The closure is
#: what makes Hall's condition bind on unions; the cap keeps a pathological
#: estate from generating 2^n sets. Exceeding it only weakens the pressure — the
#: constraint is soft, so an incomplete family set can never reject a legal board.
_MAX_ROOM_FAMILIES = 96


@dataclass
class SolveResult:
    board: Board
    status: str
    proven_optimal: bool
    expected_clashes: float
    wall_time_seconds: float
    unplaced: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)
    #: Things the planner GAVE UP, as distinct from things it did. Separate from
    #: `notes` because notes are a mixed channel — every successful two-pass run
    #: appends one — and a screen that renders all of them as warnings teaches
    #: the reader to ignore warnings. Anything in here is a compromise the caller
    #: did not ask for and must be told about.
    warnings: list[str] = field(default_factory=list)
    #: The clash total in the solver's own integer units. `expected_clashes` is
    #: recomputed from the board in exact arithmetic, so the two differ slightly
    #: wherever a weight was rounded. Budgets must be expressed in *this* one, or
    #: a zero-tolerance ceiling can reject the very board it was derived from.
    clash_score: int = 0
    #: Total room-shortfall in solver units, the same currency the budget uses.
    #: Not a meeting count — it is the summed per-instant deficiency — so it is
    #: only ever compared against itself between passes.
    room_shortfall_score: int = 0
    #: The minimised objective in solver units — a blend of every enabled term.
    #: Never report this as a clash count; it exists so a caller (or a test) can
    #: tell which terms were actually in the objective at all.
    objective_value: float = 0.0

    def summary(self) -> dict:
        return {
            "status": self.status,
            "proven_optimal": self.proven_optimal,
            "expected_clashes": round(self.expected_clashes, 1),
            "wall_time_seconds": round(self.wall_time_seconds, 2),
            "unplaced": list(self.unplaced),
            **self.board.summary(),
        }


def _atoms(windows: list[TimeWindow]) -> list[tuple[int, int]]:
    """Split the day at every window boundary.

    Two meetings overlap iff they share an atom, so occupancy becomes a linear
    per-atom question instead of a quadratic per-pair one.
    """
    marks = sorted({m for w in windows for m in (w.start, w.end)})
    return list(zip(marks, marks[1:], strict=False))


def solve(
    snapshot: Snapshot,
    *,
    time_limit_seconds: float = 60.0,
    workers: int = 8,
    seed: int = 0,
    # A class with nowhere to meet cannot be taught; a student clash is a
    # timetable somebody can still work around. The original 5 was a guess that
    # nobody tested, and it left meetings unroomed that the estate could hold.
    # Swept on the live cohorts: at 5 the male board leaves 11 unroomed against a
    # floor of 10; from 20 upward it reaches the floor exactly and stays there.
    unroomed_penalty: int = 20 * _SCALE,
    fix: Board | None = None,
    free_sections: frozenset[str] | None = None,
    break_symmetry: bool = True,
    hint: Board | None = None,
    alpha: float = 0.9,
    day_weight: int = 100 * _SCALE,
    span_weight: int = 300,  # 0.3 expected clashes per idle minute — see D11
    max_working_days: int | dict[int, int] | None = None,
    max_clash_score: int | None = None,
    max_room_shortfall: int | None = None,
    sibling_adjacency_weight: int = 0,
    student_adjacency_weight: int = 0,
    max_time_of_day_slots: int | None = None,
    max_time_of_day_minutes: int | None = None,
    exact_block_tiers: frozenset[str] = frozenset(),
    tier_weights: dict[str, float] | None = None,
    same_offering_penalty: int | None = None,
) -> SolveResult:
    """Choose a day/time for every meeting, minimising expected student clashes."""
    from ortools.sat.python import cp_model

    started = time.perf_counter()
    offerings = snapshot.offerings_by_id
    sections_by_offering = snapshot.sections_by_offering
    demand = snapshot.demand_index

    # ── enumerate each meeting and its legal cells ────────────────────────
    # A "cell" is a concrete (day, window). Legal cells follow duration and
    # delivery (D6/D9): a 100-minute lecture uses the 100-minute in-person
    # family; online uses its own late-day family.
    meetings: list[tuple[str, int, object, object]] = []  # (section_id, idx, req, offering)
    cells: dict[tuple[str, int], list[tuple[Day, TimeWindow]]] = {}
    for section in snapshot.sections:
        offering = offerings[section.offering_id]
        index = 0
        for requirement in offering.requirements:
            for _ in range(requirement.count_per_week):
                index += 1
                legal = [
                    (s.day, s.window)
                    for s in snapshot.grid.day_windows_for(
                        requirement.duration, requirement.delivery
                    )
                    # A requirement may be confined to a SUBSET of its family's
                    # starts (D19). It never adds one: the grid remains the sole
                    # authority on what times exist.
                    if not requirement.allowed_starts
                    or s.window.start in requirement.allowed_starts
                ]
                meetings.append((section.id, index, requirement, offering))
                cells[(section.id, index)] = legal

    # ── D19: sections whose hours are given, not chosen ───────────────────
    #
    # ENG101/ENG102 occupy the full morning every day, every section in the same
    # hours. Five rules below assume the solver is CHOOSING an hour, and each is
    # suspended for these sections at the point it is imposed, with the reason
    # stated there. They are suspensions of rules about choice, not licence to
    # break anything physical: rooms (H9), instructor clash (H7) and student
    # clash all still bind, because those are about the world rather than about
    # our preferences.
    fixed_block_sections = {
        section.id
        for section in snapshot.sections
        if offerings[section.offering_id].occupies_fixed_block
    }

    model = cp_model.CpModel()
    x: dict[tuple[str, int, Day, TimeWindow], object] = {}
    for section_id, index, _req, _off in meetings:
        options = []
        for day, window in cells[(section_id, index)]:
            var = model.new_bool_var(f"x_{section_id}_{index}_{day.value}_{window.start}")
            x[(section_id, index, day, window)] = var
            options.append(var)
        if options:
            model.add_exactly_one(options)

    # ── LNS support: pin everything except a chosen neighbourhood ─────────
    # Destroy/repair works by fixing most of an incumbent and re-solving a small
    # window exactly. Passing `fix` (an incumbent) with `free_sections` (the
    # window) turns this same model into the repair subproblem, so the neighbour-
    # hood is solved to optimality rather than perturbed heuristically.
    if fix is not None:
        pinned = {
            (p.section_id, p.meeting_index): (p.day, p.window)
            for p in fix.placements
            if free_sections is None or p.section_id not in free_sections
        }
        for (sid, idx), (day, window) in pinned.items():
            var = x.get((sid, idx, day, window))
            if var is not None:
                model.add(var == 1)

    # ── warm start: seed the search with a known feasible board ───────────
    #
    # Measured caveat: hinting a *poor* board hurts. Seeding the naive
    # first-fit (807 expected clashes) made the median result WORSE — 81.5
    # vs 74.8 — because the search anchors near the hint. Only hint a board
    # already known to be good, e.g. re-optimising after a small data change.
    if hint is not None:
        for p in hint.placements:
            var = x.get((p.section_id, p.meeting_index, p.day, p.window))
            if var is not None:
                model.add_hint(var, 1)

    # ── H2: at most one meeting per section per day ───────────────────────
    #
    # EXEMPT for D19 sections: meeting twice in one morning is the entire point
    # of an intensive language course. They get the weaker rule that is actually
    # physical — no two meetings of one section in the SAME cell — which, since
    # their allowed starts are mutually disjoint, is enough to keep a section
    # from being in two places at once.
    per_section_day: dict[tuple[str, Day], list] = {}
    per_section_cell: dict[tuple[str, Day, int], list] = {}
    for (section_id, _index, day, window), var in x.items():
        if section_id in fixed_block_sections:
            per_section_cell.setdefault((section_id, day, window.start), []).append(var)
        else:
            per_section_day.setdefault((section_id, day), []).append(var)
    for vars_ in per_section_day.values():
        model.add_at_most_one(vars_)
    for vars_ in per_section_cell.values():
        model.add_at_most_one(vars_)

    # ── symmetry breaking between interchangeable sibling sections ────────
    #
    # Sibling sections of one offering are identical in every respect the model
    # can see, so every solution has a factorial number of equivalent copies.
    # Measured on this cohort: 29 of 42 courses have siblings, giving ~2.2e17
    # permutations of each distinct solution. A complete solver explores them.
    #
    # Ordering siblings by the cell index of their first meeting removes those
    # copies without removing a single genuinely different timetable — but ONLY
    # among siblings the model truly cannot distinguish.
    #
    # Two sibling sections stopped being interchangeable the moment the objective
    # gained instructor terms: H7, H8, `works` and `spans` are all keyed on
    # `section.instructor_id`, which stays with the section while the cells move.
    # `assign_instructors` deliberately splits siblings across people (at most 2
    # sections of a course each, D10, with the overflow left unlinked), so a
    # course with four sections routinely has two owners. Ordering across that
    # boundary forbids every board where the second instructor teaches earlier in
    # the week than the first — which are genuinely different timetables, and
    # exactly the ones the gap objective is hunting for.
    #
    # So the chain runs within each instructor's own group (unlinked sections
    # form their own group, being interchangeable with each other).
    if break_symmetry and fix is None:
        cell_rank = {
            (day.index, window.start): i
            for i, (day, window) in enumerate(
                sorted(
                    {(sl.day, sl.window) for sl in snapshot.grid.slots},
                    key=lambda dw: (dw[0].index, dw[1].start),
                )
            )
        }
        interchangeable: list[list[str]] = []
        for siblings in sections_by_offering.values():
            # EXEMPT for D19: their siblings occupy IDENTICAL cells by design, so
            # ordering them by the cell of their first meeting demands a strict
            # increase that no feasible board can supply.
            if any(sec.id in fixed_block_sections for sec in siblings):
                continue
            by_owner: dict[object, list[str]] = {}
            for section in siblings:
                by_owner.setdefault(section.instructor_id, []).append(section.id)
            interchangeable.extend(sorted(g) for g in by_owner.values() if len(g) > 1)

        for ordered_ids in interchangeable:
            for first, second in zip(ordered_ids, ordered_ids[1:], strict=False):
                lhs = [
                    var * cell_rank[(day.index, window.start)]
                    for (sid, idx, day, window), var in x.items()
                    if sid == first and idx == 1
                ]
                rhs = [
                    var * cell_rank[(day.index, window.start)]
                    for (sid, idx, day, window), var in x.items()
                    if sid == second and idx == 1
                ]
                if lhs and rhs:
                    model.add(sum(lhs) <= sum(rhs))

    # ── sibling sections of one course, taught back to back ───────────────
    #
    # Owner rule: instructors are only ever linked to the department's OWN
    # courses (AI*, DS*) — 24 of 88 sections here. The other 73% are service
    # courses run by other departments, and nobody in this system knows who
    # teaches them. For those, the request is that the several sections of one
    # course sit back to back, so that whoever does teach them teaches them in
    # one stretch rather than being called in three times.
    #
    # This is the same care the instructor objective gives named staff, extended
    # to the staff the data cannot name. It is a REWARD rather than a
    # constraint, because it directly opposes the student-clash term: that term
    # wins by SPREADING sibling sections across the week, which is exactly what
    # this pulls against. The weight is therefore a declared exchange rate, and
    # its cost in student clashes is measured rather than assumed.
    #
    # "Back to back" means the next teaching slot, not literally touching: this
    # grid runs 09:00-10:15 then 10:30, so consecutive slots carry a 15-minute
    # changeover. Anything up to `_ADJACENT_GAP_MINUTES` counts as consecutive;
    # a 75-minute wait to the afternoon does not.
    adjacency_terms: list[object] = []
    if sibling_adjacency_weight:
        cells_of: dict[tuple[str, int], list] = {}
        for (section_id, index, day, window), var in x.items():
            cells_of.setdefault((section_id, index), []).append((day, window, var))

        for offering_id in sorted(sections_by_offering):
            siblings = sorted(s.id for s in sections_by_offering[offering_id])
            if len(siblings) < 2:
                continue
            if offerings[offering_id].occupies_fixed_block:
                # D19: every section occupies every cell of the block, so which
                # cell holds "meeting #3" of S1 versus of S2 is an artefact of
                # the solver's own numbering — the board is identical either
                # way. Left in, this spends hundreds of booleans and a matching
                # per index steering a decision that changes nothing.
                continue
            # Scoped to this offering: sections belong to exactly one course, so
            # a shared dict would only ever be re-scanned and filtered.
            paired_with: dict[tuple[str, int], list] = {}
            indexes = {i for (sid, i) in cells_of if sid in siblings}
            for index in sorted(indexes):
                for first, second in combinations(siblings, 2):
                    consecutive = []
                    for day_a, window_a, var_a in cells_of.get((first, index), ()):
                        for day_b, window_b, var_b in cells_of.get((second, index), ()):
                            if day_b is not day_a:
                                continue
                            # max(), not min(): whichever meeting is later
                            # gives the positive value, and the other ordering
                            # is a large negative. Taking the minimum picked
                            # that negative every time, so nothing was ever
                            # counted adjacent and the whole term was inert.
                            # Both negative means they overlap, which H10
                            # forbids anyway, and the bound below rejects it.
                            gap = max(
                                window_b.start - window_a.end,
                                window_a.start - window_b.end,
                            )
                            if not 0 <= gap <= _ADJACENT_GAP_MINUTES:
                                continue
                            flag = model.new_bool_var(f"adj_{first}_{second}_{index}_{day_a.value}")
                            model.add_bool_and([var_a, var_b]).only_enforce_if(flag)
                            consecutive.append(flag)
                    if consecutive:
                        together = model.new_bool_var(f"btb_{first}_{second}_{index}")
                        model.add_max_equality(together, consecutive)
                        paired_with.setdefault((first, index), []).append(together)
                        paired_with.setdefault((second, index), []).append(together)
                        adjacency_terms.append(together)

            # Owner rule: "if the course has more than 2 sections, try to keep
            # each 2 sections back to back — not all must be back to back; with
            # 3 sections, make 2 back to back and the last can be separated."
            #
            # So this is a MATCHING, not a chain: a section pairs with at most
            # one sibling. Rewarding every adjacent combination instead would
            # push four sections into one long consecutive block (three pairs
            # rather than two), which is not what was asked and costs the
            # student objective far more — that objective wins by spreading
            # siblings apart. Three sections therefore yield one pair and a
            # leftover, exactly as described.
            for flags in paired_with.values():
                if len(flags) > 1:
                    model.add_at_most_one(flags)

    # ── student waiting time: pull co-demanded courses together ───────────
    #
    # Found by actually seating students, which nothing had ever done. The board
    # is essentially clash-free — 0 to 1 student affected per cohort — but every
    # student loses around 600 minutes a week to gaps BETWEEN classes, and no
    # objective anywhere was looking at it. Killing clashes spreads sections
    # apart, and students pay for the space.
    #
    # The clash term already knows which courses share students; it uses that to
    # keep their sections from OVERLAPPING. This points the same weight at the
    # next question — having avoided a collision, are the two classes close
    # together or four hours apart? Adjacent is rewarded, on the identical
    # `shared / (na * nb)` currency, so a pair a hundred students share counts
    # for a hundred times more than one that two students share.
    #
    # It complements the clash term rather than fighting it: adjacent means
    # non-overlapping. Only strong pairs are modelled, because a pair shared by
    # one student is not worth a variable.
    student_gap_terms: list[tuple[object, int]] = []
    if student_adjacency_weight:
        cells_by_section: dict[str, list] = {}
        for (section_id, _index, day, window), var in x.items():
            cells_by_section.setdefault(section_id, []).append((day, window, var))

        for a, b in combinations(sorted(offerings.values(), key=lambda o: o.id), 2):
            # REGULAR students only (owner rule, 2026-07-28). A student taking a
            # coherent term-N block has a week that can be made compact; one
            # picking up leftovers from terms 3, 5 and 7 has a scattered set by
            # construction, and pulling their courses together drags the board
            # around for a tidiness that is not achievable. 238 of 390 on the
            # live male cohort are regular by the project's own rule.
            #
            # CLASHES are still counted for everybody — the guarantee that a
            # student can register at all is not restricted to anybody.
            shared = demand.shared_regular_students(a.id, b.id)
            if shared < _MIN_SHARED_FOR_GAP_TERM:
                continue
            if a.is_fully_online and b.is_fully_online:
                continue  # neither is on campus, so there is no walk to shorten
            sa_list = sections_by_offering.get(a.id, ())
            sb_list = sections_by_offering.get(b.id, ())
            if not sa_list or not sb_list:
                continue
            weight = max(1, round(_SCALE * shared / (len(sa_list) * len(sb_list))))
            for sa in sa_list:
                for sb in sb_list:
                    near = []
                    for day_a, window_a, var_a in cells_by_section.get(sa.id, ()):
                        for day_b, window_b, var_b in cells_by_section.get(sb.id, ()):
                            if day_b is not day_a:
                                continue
                            gap = max(
                                window_b.start - window_a.end,
                                window_a.start - window_b.end,
                            )
                            if not 0 <= gap <= _ADJACENT_GAP_MINUTES:
                                continue
                            flag = model.new_bool_var("")
                            model.add_bool_and([var_a, var_b]).only_enforce_if(flag)
                            near.append(flag)
                    if near:
                        together = model.new_bool_var("")
                        model.add_max_equality(together, near)
                        student_gap_terms.append((together, weight))

    # ── a section keeps the same hour all week ────────────────────────────
    #
    # Owner rule: "for a section, let's say the first lecture was 9am — the next
    # lecture for that section is not good after noon, or late like after 15:00.
    # Preferably keep it at the same time slot if possible; if not, one slot
    # before or after, not too far."
    #
    # Nothing in the model had any opinion about this, and the measurement says
    # exactly that: with no rule, a section landed on the same hour 16-18% of the
    # time and within one slot 34-38%, against the 14.3% and 38.8% that INDEPENDENT
    # placement would give on this seven-start grid. The model was **indifferent**,
    # not actively scattering — every other term treats a section's two weekly
    # meetings as unrelated events, since the clash term only cares what sits on
    # top of a meeting and the instructor terms only care which DAYS are used.
    # (An earlier comment here claimed the clash term actively wins by scattering.
    # The chance-level baseline does not support that, and the clash cost of the
    # ceiling is better explained by losing placement freedom in general.)
    #
    # Modelled as the DRIFT of a section within its own week: the spread between
    # its earliest and its latest start. Same slot every day is zero drift.
    #
    # Drift is bounded TWICE, in two different units, because the grid gives
    # "one slot before or after" and "not too far" genuinely different answers.
    # The 75-minute lecture family declares
    #
    #     09:00  10:30  10:50  13:00  14:30  14:45  16:00
    #
    # whose rank-adjacent steps are 90, 20, 130, 90, 15 and 75 minutes. No single
    # minute threshold expresses "one slot": set to the smallest step (15) it
    # forbids 09:00->10:30, which the rule permits; set to the largest (130) it
    # permits 10:50->13:00, which crosses noon and is the move being complained
    # about. So `max_time_of_day_slots` counts RANK — how many declared starts
    # apart the meetings are — and `max_time_of_day_minutes` bounds the real gap
    # alongside it. Rank alone leaves precisely one bad pair on this grid, and it
    # showed up as the worst case in every measured run at 130 minutes.
    #
    # Compared WITHIN a timing family, which the grid defines by DURATION and
    # delivery (D6: timing follows duration, room follows kind). A 75-minute
    # lecture and a 100-minute lab are drawn from different declared families with
    # different start times, and demanding they line up would be a rule the grid
    # cannot satisfy. Two meetings of equal duration share a family even if one is
    # declared a lab and the other a lecture — that is the same rule D6 states,
    # not an oversight.
    #
    # Minutes and ranks are built independently, because each is dead weight
    # without a consumer: with only a rank ceiling asked for, the minute variables
    # would be ~250 surplus integers and equalities added to PASS 1 — the pass
    # whose only job is to find the working-day floor inside half the budget, and
    # whose answer is then frozen as pass 2's hard budget.
    want_minutes = max_time_of_day_minutes is not None
    want_ranks = max_time_of_day_slots is not None
    if want_minutes or want_ranks:
        by_family: dict[tuple[str, int, object], list[int]] = {}
        for section_id, index, requirement, _off in meetings:
            by_family.setdefault(
                (section_id, requirement.duration, requirement.delivery), []
            ).append(index)

        starts_by_section: dict[tuple[str, int], list[tuple[int, object]]] = {}
        for (section_id, index, _day, window), var in x.items():
            starts_by_section.setdefault((section_id, index), []).append((window.start, var))

        blocked_sections = {
            section.id
            for section in snapshot.sections
            if offerings[section.offering_id].tier in exact_block_tiers
            and not offerings[section.offering_id].is_fully_online
        }
        # EXEMPT for D19: a section that fills every morning cell uses the whole
        # block by design, so a "stay within one slot of your usual hour"
        # ceiling is both meaningless and unsatisfiable for it.
        blocked_sections |= fixed_block_sections
        for (section_id, duration, delivery), indexes in sorted(
            by_family.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))
        ):
            if section_id in blocked_sections:
                # The block rule below is stricter and differently shaped: it
                # pins the slots the whole PAIR occupies while letting the two
                # sections swap between them, which a per-section drift ceiling
                # would forbid outright. Applying both would ban the alternation
                # the owner explicitly asked for.
                continue
            if len(indexes) < 2:
                continue  # one meeting a week cannot drift
            starts = sorted({w.start for w in snapshot.grid.windows_for(duration, delivery)})
            if len(starts) < 2:
                continue  # a single legal time — drift is structurally zero
            rank_of = {start: i for i, start in enumerate(starts)}
            low, high = starts[0], starts[-1]

            counted = 0
            chosen_starts, chosen_ranks = [], []
            for index in sorted(indexes):
                options = starts_by_section.get((section_id, index))
                if not options:
                    continue
                counted += 1
                if want_minutes:
                    start_var = model.new_int_var(low, high, f"tod_{section_id}_{index}")
                    model.add(start_var == sum(var * start for start, var in options))
                    chosen_starts.append(start_var)
                if want_ranks:
                    rank_var = model.new_int_var(0, len(starts) - 1, f"todr_{section_id}_{index}")
                    model.add(rank_var == sum(var * rank_of[start] for start, var in options))
                    chosen_ranks.append(rank_var)
            if counted < 2:
                continue

            tag = f"{section_id}_{duration}_{delivery.value}"
            if want_minutes:
                earliest = model.new_int_var(low, high, f"tod_lo_{tag}")
                latest = model.new_int_var(low, high, f"tod_hi_{tag}")
                model.add_min_equality(earliest, chosen_starts)
                model.add_max_equality(latest, chosen_starts)
                drift = model.new_int_var(0, high - low, f"tod_drift_{tag}")
                model.add(drift == latest - earliest)
                model.add(drift <= max_time_of_day_minutes)

            # The rank ceiling, in the owner's own units: 0 is "the same slot
            # every day", 1 is "one slot before or after".
            #
            # A ceiling rather than a price, because a ceiling is a guarantee and
            # a price is a preference the search can outbid — and this search is
            # already competing against a hard clash budget inside a 45-second
            # half-pass. (A priced version was tried at two seeds and gave 20%
            # and 36% of sections on a fixed hour. That sample establishes
            # neither a level nor its variance and the run was not preserved, so
            # it is not evidence for anything; the argument here is from what the
            # constraint IS, not from that measurement.)
            if want_ranks:
                first = model.new_int_var(0, len(starts) - 1, f"todr_lo_{tag}")
                last = model.new_int_var(0, len(starts) - 1, f"todr_hi_{tag}")
                model.add_min_equality(first, chosen_ranks)
                model.add_max_equality(last, chosen_ranks)
                model.add(last - first <= max_time_of_day_slots)

    # ── a course's sections own a fixed block of slots ────────────────────
    #
    # Owner rule, 2026-07-28. The same-hour ceiling above is per SECTION, and
    # that is right for this department's own courses (T1), where we control the
    # instructor. For everything else the instructors come from other
    # departments and teach elsewhere in the week, so a course whose slots move
    # about is unmanageable for them:
    #
    #   * a course with ONE section keeps that section on one slot, every day;
    #   * two sections kept back to back own an exact PAIR of slots, the same
    #     two every day — and may SWAP which of them each section sits in.
    #
    #         Monday      13:00  S1     14:30  S2
    #         Wednesday   13:00  S2     14:30  S1     <- same block, swapped
    #
    # The other department sees 13:00 and 14:30 occupied every week without
    # exception. Which section is in which is our business, not theirs, and the
    # swap costs nothing while giving the search somewhere to move.
    #
    # Note this is a rule about the PAIR, not the whole course: a third section
    # stands alone with its own single slot and is not tied to the pair's days.
    # Scoping it to the course would force every section of a four-section course
    # onto the same days, which is a far heavier constraint than was asked for.
    #
    # ONLINE IS EXEMPT (owner). GS and GSE already run in their own three evening
    # windows, consume no room and create no campus travel, so pinning them buys
    # nobody anything and only spends the freedom in the one family with slack.
    if exact_block_tiers:
        # Keyed on DURATION as well as time. A 100-minute lab and a 75-minute
        # lecture both start at 09:00, so keying on the start alone put them in
        # one bucket and the block constraint then policed a mixture of two
        # families that have different legal times and different block widths.
        # Measured consequence: CS113's pair filled one slot on Sunday and two on
        # Thursday, which the constraint is supposed to make impossible.
        cells_at: dict[tuple[str, int, object, int], list] = {}
        for (section_id, _index, day, window), var in x.items():
            cells_at.setdefault((section_id, window.duration, day, window.start), []).append(var)

        for offering_id in sorted(sections_by_offering):
            offering = offerings[offering_id]
            if offering.tier not in exact_block_tiers or offering.is_fully_online:
                continue
            # EXEMPT for D19. The block rule gives a PAIR of sections a pair of
            # slots to share and alternate between; these sections each occupy
            # every slot of their block, which is a different arrangement of the
            # same idea and already stricter than anything D17 would impose.
            if offering.occupies_fixed_block:
                continue
            siblings = sorted(sections_by_offering[offering_id], key=lambda s: s.index)
            # Pairs by section order — S1+S2, S3+S4, leftover last. Structural
            # rather than chosen by the solver, so the other department can rely
            # on it and so the answer does not move between runs.
            groups = [siblings[i : i + 2] for i in range(0, len(siblings), 2)]

            for requirement in offering.requirements:
                starts = sorted(
                    {
                        w.start
                        for w in snapshot.grid.windows_for(
                            requirement.duration, requirement.delivery
                        )
                    }
                )
                if len(starts) < 2:
                    continue  # one legal time: the block is forced anyway
                for group_index, group in enumerate(groups):
                    ids = [section.id for section in group]
                    tag = f"{offering_id}_{group_index}_{requirement.duration}"
                    uses = {start: model.new_bool_var(f"blk_{tag}_{start}") for start in starts}
                    # The block is exactly as wide as the group: one slot for a
                    # lone section, two for a pair.
                    model.add(sum(uses.values()) == len(ids))

                    for day in snapshot.grid.days():
                        here = {
                            start: [
                                var
                                for section_id in ids
                                for var in cells_at.get(
                                    (section_id, requirement.duration, day, start), ()
                                )
                            ]
                            for start in starts
                        }
                        runs = model.new_bool_var(f"blkday_{tag}_{day.value}")
                        for start in starts:
                            occupied = sum(here[start]) if here[start] else 0
                            # Never outside the block. REDUNDANT, kept for
                            # propagation: a meeting at an unblocked slot sets
                            # `runs`, which forces every blocked slot filled
                            # too, so the day would need more meetings than H2
                            # allows this group. Deleting it therefore changes
                            # no feasible board and no test can see it — said
                            # here so nobody later reads the missing test as an
                            # oversight and removes the line.
                            model.add(occupied <= uses[start])
                            # ...and `runs` is 1 iff the group meets that day
                            if here[start]:
                                model.add(runs >= occupied)
                            # ...and on a day it runs, EVERY slot of the block is
                            # filled. This is what makes the block identical
                            # every day, and what lets the two sections swap.
                            model.add(occupied >= uses[start] + runs - 1)
                        any_here = [v for start in starts for v in here[start]]
                        if any_here:
                            model.add(runs <= sum(any_here))
                        else:
                            model.add(runs == 0)

    # ── occupancy atoms, per delivery family ──────────────────────────────
    all_windows = [s.window for s in snapshot.grid.slots]
    atoms = _atoms(all_windows)

    def covering(day: Day, window: TimeWindow) -> list[tuple[Day, int, int]]:
        return [(day, a, b) for a, b in atoms if a < window.end and window.start < b]

    # busy[section, day, atom] — EVERY meeting, online included. Online consumes
    # no room and cannot clash for a student (D9), but it is still a real
    # commitment of a section and an instructor, so H10/H7/H8 bind on it.
    # Excluding it here let sibling online sections stack on one slot.
    busy: dict[tuple[str, Day, int, int], object] = {}
    contributions: dict[tuple[str, Day, int, int], list] = {}
    for (section_id, index, day, window), var in x.items():
        requirement = next(r for (sid, i, r, _o) in meetings if sid == section_id and i == index)
        for key in covering(day, window):
            contributions.setdefault((section_id, key[0], key[1], key[2]), []).append(var)
    for key, vars_ in contributions.items():
        flag = model.new_bool_var(f"busy_{key[0]}_{key[1].value}_{key[2]}")
        busy[key] = flag
        model.add_max_equality(flag, vars_)

    same_offering_terms: list[object] = []
    # ── H10: sibling sections of one offering never overlap ───────────────
    #
    # HARD by default, but it is worth knowing what it costs. Two sections of one
    # course CAN physically run at once — different rooms, different instructors,
    # different students — and the reasons usually given are already covered
    # elsewhere: an instructor teaching both is H7, the rooms are H9, and a
    # student takes only one section. What the rule really buys is CHOICE: if
    # every section sits at the same hour, a student who clashes with one clashes
    # with all of them, and the expected-clash objective already rewards spreading
    # siblings for exactly that reason.
    #
    # As a hard rule it is not free. It separates every meeting of every sibling,
    # so a course with 17 sections meeting three times a week needs 51 mutually
    # free cells against a week that holds 25 — and four of the twelve programme
    # groups here become unbuildable at any budget. `same_offering_penalty`
    # exists so that trade can be measured rather than argued about.
    for siblings in sections_by_offering.values():
        # EXEMPT for D19: the owner's rule IS that every section sits at the same
        # time. H10 buys student choice between sections; a course that owns the
        # whole morning offers no choice of hour to begin with, and forbidding
        # the overlap would make it unschedulable rather than more flexible.
        if any(sec.id in fixed_block_sections for sec in siblings):
            continue
        for sa, sb in combinations(sorted(s.id for s in siblings), 2):
            for day, start, end in {(k[1], k[2], k[3]) for k in busy if k[0] in (sa, sb)}:
                a = busy.get((sa, day, start, end))
                b = busy.get((sb, day, start, end))
                if a is None or b is None:
                    continue
                if same_offering_penalty is None:
                    model.add(a + b <= 1)
                else:
                    together = model.new_bool_var("")
                    model.add(together >= a + b - 1)
                    same_offering_terms.append(together)

    # ── room capacity, as Hall's condition ────────────────────────────────
    #
    # Without any such constraint the solver is *room-blind* — the exact defect
    # measured in the old engine, where `rooms_by_id` was None at every search
    # site and the optimiser happily created boards that could not be roomed.
    # Minimising student clash alone pushes meetings into the same few good
    # windows, which is precisely where rooms run out.
    #
    # Counting rooms per KIND is not enough, and the difference is not academic.
    # A section needing 42 seats can use exactly one room here; a DS-only class
    # cannot use the AI-only rooms. Counting all eight lecture rooms told the
    # solver it had room when it did not, and the shortfall then surfaced as
    # "congestion" the report promised a reschedule could fix.
    #
    # The right condition is Hall's: for ANY set R of rooms, the meetings whose
    # compatible rooms all lie inside R can never outnumber R at one instant.
    # Every subset is exponential, but only the sets that actually arise matter
    # — compatibility is decided by (kind, capacity threshold, programmes), so a
    # handful of distinct sets covers the real structure, and the per-kind rule
    # is simply the largest of them.
    sections_by_id = {s.id: s for s in snapshot.sections}
    compatible_rooms: dict[tuple[str, int], frozenset[str]] = {}
    for section_id, index, requirement, offering in meetings:
        # `needs_shared_room`, not the delivery mode: an ENG101 meeting is
        # in-person but housed in the course's own rooms (D19), so it must not
        # be counted against a supply it never draws on.
        if not requirement.needs_shared_room:
            continue
        section = sections_by_id.get(section_id)
        need = snapshot.policy.required_room_capacity(section.capacity) if section else 0
        compatible_rooms[(section_id, index)] = frozenset(
            room.id
            for room in snapshot.rooms
            if room.kind is requirement.kind
            and room.capacity >= need
            and (offering.programs & room.programs)
        )

    # Hall's condition binds on UNIONS too, so the distinct compatible sets are
    # not enough on their own. With rooms L1{AI}, L2{AI,DS}, L3{DS} and two
    # AI-only plus two DS-only meetings at one instant, {L1,L2} holds 2<=2 and
    # {L2,L3} holds 2<=2, yet four meetings need three rooms and one must go
    # unroomed. Only {L1,L2,L3} — a union of the two — reveals it. The closure is
    # capped: beyond the cap the family set is merely incomplete, which weakens
    # the pressure but can never make a legal board illegal, since every
    # constraint here is soft anyway.
    families: set[frozenset[str]] = {s for s in compatible_rooms.values() if s}
    for kind in {r.kind for r in snapshot.rooms}:  # the per-kind rule, still enforced
        whole = frozenset(r.id for r in snapshot.rooms_of_kind(kind))
        if whole:
            families.add(whole)
    frontier = sorted(families, key=lambda f: (len(f), sorted(f)))
    while frontier and len(families) < _MAX_ROOM_FAMILIES:
        left = frontier.pop()
        for right in sorted(families, key=lambda f: (len(f), sorted(f))):
            union = left | right
            if union not in families:
                families.add(union)
                frontier.append(union)
                if len(families) >= _MAX_ROOM_FAMILIES:
                    break

    concurrent: dict[tuple[frozenset[str], Day, int, int], list] = {}
    for (section_id, index, day, window), var in x.items():
        mine = compatible_rooms.get((section_id, index))
        if not mine:
            continue  # online, or provably unroomable — no room is ever consumed
        for family in families:
            if mine <= family:
                for _d, start, end in covering(day, window):
                    concurrent.setdefault((family, day, start, end), []).append(var)

    # SOFT, never hard (D7): a room shortage must not block the build. The female
    # cohort is short 29 lecture room-periods for the whole week, so a hard bound
    # makes the model INFEASIBLE and produces no timetable at all.
    # Instead, exceeding the room count is permitted and penalised, so the
    # solver packs to the rooms it has and the overflow surfaces as unroomed
    # meetings rather than as failure.
    #
    # ONE shortfall variable per (day, atom), lower-bounded by every family's
    # deficiency and paid for once. Two earlier mistakes are both avoided here:
    #
    #  * charging each family separately double-billed a single overflow, because
    #    families nest — a meeting that only fits R1 is inside {R1} and inside
    #    {R1,R2}. Summing deficiencies over nested sets can exceed the true
    #    number of stranded meetings, and the solver would then prefer a board
    #    that strands MORE of them but spreads them out. Hall's deficiency is a
    #    MAXIMUM over sets, not a sum, which is what the shared variable encodes;
    #
    #  * charging a flat price per atom made the cost depend on where the grid
    #    happened to cut. A 09:00-10:15 lecture covers one atom; an identical
    #    10:30-11:45 lecture covers four, because a lab ends at 10:40, another
    #    starts at 10:45 and an alternative lecture starts at 10:50 — none of
    #    which is a fact about rooms. That priced the same stranded meeting at
    #    20 or 80. Pricing by atom LENGTH instead makes the total depend on how
    #    much room-time is missing, which is the thing actually in short supply.
    overflow_terms: list[tuple[object, int]] = []
    shortfall: dict[tuple[Day, int, int], object] = {}
    for (family, day, start, end), vars_ in sorted(
        concurrent.items(), key=lambda kv: (kv[0][1].index, kv[0][2], kv[0][3], sorted(kv[0][0]))
    ):
        limit = len(family)
        if not limit or len(vars_) <= limit:
            continue
        key = (day, start, end)
        var = shortfall.get(key)
        if var is None:
            var = model.new_int_var(0, len(meetings), f"short_{day.value}_{start}_{end}")
            shortfall[key] = var
        model.add(sum(vars_) - limit <= var)

    for (_day, start, end), var in sorted(
        shortfall.items(), key=lambda kv: (kv[0][0].index, kv[0][1])
    ):
        minutes = max(1, end - start)
        overflow_terms.append(
            (var, max(1, round(unroomed_penalty * minutes / _REFERENCE_MEETING_MINUTES)))
        )

    # The third epsilon-constraint. Rooms get the same treatment as working days
    # and student clashes because they lose the same way: a later pass told to
    # push hard on one objective will pay for it out of whichever quantity is
    # only *priced* rather than *bounded*. Measured — with rooms merely priced,
    # the gap pass raised the unroomed count on every run, so the guard that
    # rejects such boards fired every time and the entire gap improvement was
    # thrown away (idle 645 -> 3255). Bounded instead, the gap term is free to
    # work inside a board that stays roomable.
    if max_room_shortfall is not None and shortfall:
        model.add(sum(shortfall.values()) <= max_room_shortfall)

    # ── H7/H8: instructor clash and daily cap, over assigned sections only ─
    by_instructor: dict[int, list[str]] = {}
    for section in snapshot.sections:
        if section.instructor_id is not None:
            by_instructor.setdefault(section.instructor_id, []).append(section.id)
    for _iid, section_ids in by_instructor.items():
        for sa, sb in combinations(sorted(section_ids), 2):
            for day, start, end in {(k[1], k[2], k[3]) for k in busy if k[0] in (sa, sb)}:
                a = busy.get((sa, day, start, end))
                b = busy.get((sb, day, start, end))
                if a is not None and b is not None:
                    model.add(a + b <= 1)
        for day in snapshot.grid.days():
            same_day = [
                var for (sid, _i, d, _w), var in x.items() if sid in section_ids and d is day
            ]
            if same_day:
                model.add(sum(same_day) <= 3)

    # ── instructor quality: working days, then campus span ────────────────
    #
    # Working days before idle minutes, deliberately: each additional day is a
    # commute, so two days with an hour's gap beats four days with none. Excess
    # is judged against each instructor's own proven floor elsewhere; here we
    # simply minimise the days actually used.
    #
    # Span is the tertiary term. Teaching minutes on a day are fixed once the
    # assignment is fixed, so minimising (last_end - first_start) is exactly
    # minimising idle time, with fewer variables than modelling idle directly.
    works: dict[tuple[int, Day], object] = {}
    spans: list[object] = []
    delivery_of = {
        section.id: offerings[section.offering_id].requirements[0].delivery
        for section in snapshot.sections
        if offerings[section.offering_id].requirements
    }
    by_instructor_sections: dict[int, list[str]] = {}
    for section in snapshot.sections:
        if section.instructor_id is not None:
            by_instructor_sections.setdefault(section.instructor_id, []).append(section.id)

    for instructor_id, section_ids in sorted(by_instructor_sections.items()):
        for day in snapshot.grid.days():
            # Campus presence only. `works` is a commute proxy and `span` is
            # time spent waiting AT the university, and an online session is
            # neither — teaching it from home does not turn a free day into a
            # travelled one, and it cannot fill a gap between two campus
            # classes. Instructor CLASH and the daily cap are enforced over
            # every meeting further up, because a person still cannot be in two
            # places at once and still teaches the session either way.
            same_day = [
                (var, window)
                for (sid, _i, d, window), var in x.items()
                if sid in section_ids
                and d is day
                and delivery_of.get(sid) is DeliveryMode.IN_PERSON
            ]
            if not same_day:
                continue
            flag = model.new_bool_var(f"works_{instructor_id}_{day.value}")
            works[(instructor_id, day)] = flag
            model.add_max_equality(flag, [v for v, _w in same_day])

            first = model.new_int_var(0, 24 * 60, f"first_{instructor_id}_{day.value}")
            last = model.new_int_var(0, 24 * 60, f"last_{instructor_id}_{day.value}")
            for var, window in same_day:
                model.add(first <= window.start).only_enforce_if(var)
                model.add(last >= window.end).only_enforce_if(var)
            span = model.new_int_var(0, 24 * 60, f"span_{instructor_id}_{day.value}")
            model.add(span >= last - first)  # >= 0 by domain, so an idle day costs 0
            spans.append(span)

    # An epsilon-constraint budget on working days. Tuning the day/idle weights
    # against each other was measurably unstable — the same weight held the day
    # floor on one seed and lost it on the next — so the caller can instead fix
    # the day count it already achieved and let the idle term push as hard as it
    # likes underneath. Feasibility is free: whatever produced the budget is
    # itself a solution that satisfies it.
    # A dict caps each instructor separately; a bare int caps only the scenario
    # total. The total alone is a weaker promise than it looks — pass 2 can take
    # a day off one instructor and hand it to another while the sum holds, so
    # somebody's week gets worse and no check notices. `plan()` therefore passes
    # the per-instructor mapping.
    if max_working_days is not None and works:
        if isinstance(max_working_days, dict):
            per_instructor: dict[int, list] = {}
            for (instructor_id, _day), flag in works.items():
                per_instructor.setdefault(instructor_id, []).append(flag)
            for instructor_id, flags in per_instructor.items():
                cap = max_working_days.get(instructor_id)
                if cap is not None:
                    model.add(sum(flags) <= cap)
        else:
            model.add(sum(works.values()) <= max_working_days)

    # ── objective: expected student clashes ───────────────────────────────
    penalties: list[tuple[object, int]] = []
    pair_count = 0
    #: Default 1.0 everywhere: every collision counts fully unless a caller says
    #: otherwise, so this cannot quietly discount anything by accident.
    tiers = tier_weights or {}
    for a, b in combinations(sorted(offerings.values(), key=lambda o: o.id), 2):
        shared = demand.shared_students(a.id, b.id)
        if not shared:
            continue
        sa_list = [s.id for s in sections_by_offering.get(a.id, ())]
        sb_list = [s.id for s in sections_by_offering.get(b.id, ())]
        if not sa_list or not sb_list:
            continue
        # Expected students lost per colliding section pair, scaled by how much
        # the collision actually costs the student.
        #
        # Owner rule, 2026-07-28: T2 and T3 courses are taught in other sections
        # right across the college, so a student who cannot fit one here will
        # find a seat elsewhere. Protecting those collisions at full price
        # spends the whole board's quality on a problem the registrar can solve
        # by other means — and on the live male cohort it is 47% of the entire
        # clash objective for T3 alone, against 16% for T1 against T1.
        #
        # The pair takes the LOWER of the two tiers' weights: if either course
        # can be picked up elsewhere, the clash is resolvable, so the pair is
        # only as serious as its most relocatable member.
        severity = min(tiers.get(a.tier, 1.0), tiers.get(b.tier, 1.0))
        if severity <= 0.0:
            continue
        weight = max(1, round(_SCALE * severity * shared / (len(sa_list) * len(sb_list))))
        for sa in sa_list:
            for sb in sb_list:
                shared_atoms = {(k[1], k[2], k[3]) for k in busy if k[0] == sa} & {
                    (k[1], k[2], k[3]) for k in busy if k[0] == sb
                }
                if not shared_atoms:
                    continue
                clash = model.new_bool_var(f"c_{sa}_{sb}")
                for day, start, end in shared_atoms:
                    model.add(
                        clash >= busy[(sa, day, start, end)] + busy[(sb, day, start, end)] - 1
                    )
                penalties.append((clash, weight))
                pair_count += 1
    # Weighted, not lexicographic — the weights ARE the policy, and they were
    # set by measurement rather than taste. Trade-off measured on the live M
    # cohort (45s, alpha=0.9), varying day_weight:
    #
    #     day_weight   clashes   instructor days
    #        (none)       80.6        23
    #          20         85.1        22
    #          50         88.8        20
    #         100         97.4        19  <- proven floor
    #
    # So an instructor-day costs roughly FOUR expected student clashes. The
    # default reaches the floor because the owner's stated priority is minimum
    # instructor gap; lower day_weight buys student quality back if that changes.
    #
    # Working days and idle gaps genuinely OPPOSE each other, so the second
    # weight is a real policy choice and not a refinement of the first: spreading
    # sessions over more days shortens each day and cuts gaps, while packing them
    # into fewer days creates gaps. Measured on the same cohort, varying the
    # per-minute cost of idle time:
    #
    #     idle cost/min   clashes   days   idle minutes
    #        0 (dead)        90.5     19       2495
    #        0.05            99.9     19       1615
    #        0.2             96.1     19       1430
    #        0.3 (default)  102.5     19        950   <- knee
    #        0.4             94.1     20       1035
    #        1               98.2     20        730
    #        3               97.2     25        420
    #
    # 0.3 is the knee — the most gap-cutting pressure that still left every
    # instructor on their proven minimum number of days in that sweep. It is the
    # default here, but it is NOT how the planner gets its result, because the
    # knee turned out not to be reproducible: re-run across three seeds it gave
    # 19, 20 and 20 working days. A weight cannot promise anything about a
    # quantity it merely trades against. `plan()` therefore freezes the day count
    # as a hard budget and ignores this trade-off entirely; the weight only still
    # matters to callers using `solve()` directly.
    #
    # Minimising span IS minimising idle exactly, not approximately: summed over
    # instructor-days, span = teaching + idle, and total teaching is constant
    # once every session is placed somewhere.
    #
    # Pure instructor optimisation (alpha=0) is catastrophic for students
    # (741 vs 81) and gains nothing, since 19 is already the floor.
    #
    # `alpha` is applied as a RATIO on the instructor side, never as a multiplier
    # on both sides. Scaling both and casting to int silently destroyed the span
    # term: int(2 * 0.1) is 0, and the max(1, ...) floor then billed a minute of
    # an instructor's idle time at 1 point against 10,000 points for a working
    # day, so the solver was free to ignore gaps entirely. Idle sat at 2250
    # minutes against a 400-minute lower bound. Keeping the student weights at
    # face value and scaling only the instructor weights keeps every coefficient
    # in one currency (1 point = one thousandth of an expected clash) and keeps
    # small weights representable.
    # The second epsilon-constraint: a ceiling on student harm. Freezing the day
    # count let the gap term push hard, and it promptly started paying for gaps
    # with clashes (132 against ~104 on one seed). A budget makes that trade
    # bounded and visible instead of whatever the weights happened to allow.
    if max_clash_score is not None and penalties:
        model.add(sum(var * w for var, w in penalties) <= max_clash_score)

    use_students = alpha > 0.0
    use_instructors = alpha < 1.0
    ratio = (1.0 - alpha) / alpha if 0.0 < alpha < 1.0 else 1.0

    # The max(1, ...) floor exists so a small-but-real weight cannot round away
    # to nothing. It must NOT apply to a weight of zero, which is a caller saying
    # "switch this term off": floored to 1, a disabled term still priced every
    # idle minute, and `plan()`'s first pass — which passes span_weight=0
    # precisely to isolate the working-day question — was quietly optimising gaps
    # the whole time. Zero is off; anything else keeps the floor.
    objective = [var * w for var, w in overflow_terms]
    objective += [v * (same_offering_penalty or 0) for v in same_offering_terms]
    if use_students:
        objective += [var * w for var, w in penalties]
    if use_instructors:
        if day_weight:
            objective += [flag * max(1, round(day_weight * ratio)) for flag in works.values()]
        if span_weight:
            objective += [span * max(1, round(span_weight * ratio)) for span in spans]
    # Negative: CP-SAT minimises, so a reward is a cost avoided.
    objective += [flag * -sibling_adjacency_weight for flag in adjacency_terms]
    objective += [
        flag * -max(1, round(weight * student_adjacency_weight / _SCALE))
        for flag, weight in student_gap_terms
    ]
    if objective:
        model.minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = seed
    status = solver.solve(model)
    elapsed = time.perf_counter() - started

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # A PROVEN infeasibility and a timeout are different facts and must not
        # share a sentence. "No assignment found within 120s" reads as "give it
        # longer", and for INFEASIBLE no budget will ever help — the answer is
        # that some rule cannot be satisfied at all, which is a decision for a
        # human. This is N10: infeasible always carries the reason.
        if status == cp_model.INFEASIBLE:
            note = (
                "INFEASIBLE: no timetable satisfies these rules, at any time "
                "budget. Longer solving cannot help; a constraint or the input "
                "has to change."
            )
        else:
            note = f"no assignment found within {time_limit_seconds:.0f}s"
        return SolveResult(
            Board(()),
            solver.status_name(status),
            False,
            float("inf"),
            elapsed,
            notes=[note],
        )

    placements: list[Placement] = []
    unplaced: list[str] = []
    for section_id, index, requirement, offering in meetings:
        chosen = next(
            (
                (day, window)
                for (sid, i, day, window), var in x.items()
                if sid == section_id and i == index and solver.value(var)
            ),
            None,
        )
        if chosen is None:
            unplaced.append(f"{section_id}#M{index}")
            continue
        section = next(s for s in snapshot.sections if s.id == section_id)
        placements.append(
            Placement(
                section_id=section_id,
                offering_id=offering.id,
                meeting_index=index,
                kind=requirement.kind,
                delivery=requirement.delivery,
                day=chosen[0],
                window=chosen[1],
                room_id=None,
                instructor_id=section.instructor_id,
                uses_shared_room=requirement.uses_shared_room,
            )
        )

    board = Board(tuple(placements))
    return SolveResult(
        board=board,
        status=solver.status_name(status),
        proven_optimal=status == cp_model.OPTIMAL,
        # Recomputed from the board, NOT read off the objective. Once instructor
        # days and span share the objective, `objective_value` is a blend and
        # reporting it under this name would overstate student harm.
        expected_clashes=expected_clashes(snapshot, board) if penalties else 0.0,
        wall_time_seconds=elapsed,
        unplaced=tuple(unplaced),
        notes=[f"{pair_count} conflicting section pairs modelled"],
        # Only meaningful while the clash vars are under objective pressure: they
        # are lower-bounded by their meetings, so with alpha=0 nothing stops the
        # solver leaving one set spuriously.
        clash_score=(
            sum(int(solver.value(var)) * w for var, w in penalties)
            if penalties and alpha > 0.0
            else 0
        ),
        objective_value=solver.objective_value if objective else 0.0,
        room_shortfall_score=sum(int(solver.value(v)) for v in shortfall.values()),
    )


def expected_clashes(snapshot: Snapshot, board: Board) -> float:
    """Expected students clashing on a finished board.

    The same measure the solver minimises, computed independently from the board
    alone — so a naive baseline and a solved board are scored on identical terms.
    Online sessions are excluded: they cannot clash for a student (D9).
    """
    demand = snapshot.demand_index
    sections_by_offering = snapshot.sections_by_offering
    by_section: dict[str, list[Placement]] = {}
    for p in board.placements:
        # Online included: it occupies the student's time even though it
        # occupies no room, and it now runs at the same declared hours.
        by_section.setdefault(p.section_id, []).append(p)

    total = 0.0
    offerings = sorted(snapshot.offerings_by_id.values(), key=lambda o: o.id)
    for a, b in combinations(offerings, 2):
        shared = demand.shared_students(a.id, b.id)
        if not shared:
            continue
        sa_list = [s.id for s in sections_by_offering.get(a.id, ())]
        sb_list = [s.id for s in sections_by_offering.get(b.id, ())]
        if not sa_list or not sb_list:
            continue
        per_pair = shared / (len(sa_list) * len(sb_list))
        for sa in sa_list:
            for sb in sb_list:
                if any(
                    pa.overlaps(pb)
                    for pa in by_section.get(sa, ())
                    for pb in by_section.get(sb, ())
                ):
                    total += per_pair
    return total


def assign_rooms(snapshot: Snapshot, board: Board) -> Board:
    """Assign rooms to a time-fixed board — a separate stage, by design.

    Once times are fixed, rooming is a clean interval-assignment problem, and on
    this data it is not the binding constraint: first-fit already reaches 100% of
    the theoretical lecture-room maximum. Meetings that find no free compatible
    room keep their time and stay unroomed (D7) rather than being dropped.

    Largest demand first, smallest sufficient room — so the few big sections get
    the scarce big rooms before smaller ones consume them.
    """
    offerings = snapshot.offerings_by_id
    sections = {s.id: s for s in snapshot.sections}
    busy: dict[tuple[str, Day], list[TimeWindow]] = {}
    out: list[Placement] = []

    ordered = sorted(
        board.placements,
        key=lambda p: (
            not p.needs_room,
            -sections[p.section_id].capacity if p.section_id in sections else 0,
            p.id,
        ),
    )
    for p in ordered:
        if not p.needs_room:
            out.append(p)
            continue
        section = sections.get(p.section_id)
        offering = offerings.get(p.offering_id)
        need = snapshot.policy.required_room_capacity(section.capacity) if section else 0
        options = sorted(
            (
                r
                for r in snapshot.rooms
                if r.kind is p.kind
                and r.capacity >= need
                and offering is not None
                and (offering.programs & r.programs)
            ),
            key=lambda r: (r.capacity, r.id),
        )
        chosen = None
        for room in options:
            if all(not p.window.overlaps(w) for w in busy.get((room.id, p.day), [])):
                chosen = room.id
                busy.setdefault((room.id, p.day), []).append(p.window)
                break
        out.append(p if chosen is None else replace(p, room_id=chosen))
    return Board(tuple(sorted(out, key=lambda x: x.id)))


def instructor_metrics(snapshot: Snapshot, board: Board) -> dict:
    """Instructor schedule quality — **always** reported with its coverage.

    Partial instructor linkage is permanent (D5), so a bare "19 working days"
    is not a statement about the timetable. Every figure here carries the
    denominator it was computed over.
    """
    from scheduler.instructors import instructor_floor_days, section_min_days

    offerings = snapshot.offerings_by_id
    by_instructor: dict[int, list[Placement]] = {}
    for p in board.placements:
        if p.instructor_id is not None:
            by_instructor.setdefault(p.instructor_id, []).append(p)

    sections_of: dict[int, set[str]] = {}
    for s in snapshot.sections:
        if s.instructor_id is not None:
            sections_of.setdefault(s.instructor_id, set()).add(s.id)

    rows, total_days, total_floor, total_idle = [], 0, 0, 0
    for instructor_id, placements in sorted(by_instructor.items()):
        # Campus presence only, matching what the model minimises: an online
        # session is neither a commute nor time spent waiting at the university.
        by_day: dict[Day, list[Placement]] = {}
        for p in placements:
            if p.delivery is not DeliveryMode.IN_PERSON:
                continue
            by_day.setdefault(p.day, []).append(p)
        idle = 0
        for _day, items in by_day.items():
            span = max(i.window.end for i in items) - min(i.window.start for i in items)
            idle += span - sum(i.window.duration for i in items)
        # DAYS the biggest section needs, not its meeting count: a fixed-block
        # section (D19) meets twice a morning, so counting meetings gave a
        # "proven floor" of 10 days in a 5-day week. `section_min_days` is the
        # single definition, shared with `instructors.py`.
        largest = max(
            (
                section_min_days(offerings[s.offering_id])
                for s in snapshot.sections
                if s.id in sections_of.get(instructor_id, set())
            ),
            default=0,
        )
        # Sessions counted from the SNAPSHOT, not from the board. Taking them
        # from the board lets a partial timetable re-benchmark itself against
        # its own contents: lose a class and the floor drops with it, so the
        # board still reads "at the proven floor" while teaching less than it
        # should. Measured: dropping one of four sessions halved the reported
        # idle and left `at_proven_floor` True.
        owed = sum(
            sum(r.count_per_week for r in offerings[s.offering_id].requirements)
            for s in snapshot.sections
            if s.id in sections_of.get(instructor_id, set())
        )
        floor = instructor_floor_days(owed, largest)
        rows.append(
            {
                "instructor_id": instructor_id,
                "sessions": len(placements),
                "working_days": len(by_day),
                "floor_days": floor,
                "excess_days": len(by_day) - floor,
                "idle_minutes": idle,
            }
        )
        total_days += len(by_day)
        total_floor += floor
        total_idle += idle

    assigned = sum(1 for s in snapshot.sections if s.instructor_id is not None)

    # Coverage against ALL sections is a misleading headline. The department
    # staffs only its OWN courses (D12) — everything else is a service course
    # run elsewhere, and no instructor for it will ever exist in this data. So
    # "26% of 88" reads as three quarters of the data missing when the true
    # picture is 23 of 24 staffable sections filled. Both numbers are published:
    # the first is the scope of the instructor figures, the second is the only
    # one that says whether anything is actually absent.
    staffable_offerings = {o for i in snapshot.instructors for o in i.eligible_offerings}
    staffable = sum(
        len(snapshot.sections_by_offering.get(offering_id, ()))
        for offering_id in staffable_offerings
    )
    return {
        "instructors": len(rows),
        "working_days": total_days,
        "floor_days": total_floor,
        "excess_days": total_days - total_floor,
        "at_proven_floor": total_days == total_floor,
        "idle_minutes": total_idle,
        "coverage": {
            "sections_assigned": assigned,
            "sections_total": len(snapshot.sections),
            "sections_staffable": staffable,
            "percent": round(100.0 * assigned / len(snapshot.sections), 1)
            if snapshot.sections
            else 0.0,
            "percent_of_staffable": round(100.0 * assigned / staffable, 1) if staffable else 0.0,
        },
        "per_instructor": rows,
    }


def plan(
    snapshot: Snapshot,
    *,
    time_limit_seconds: float = 90.0,
    gap_weight: int = 3 * _SCALE,
    clash_tolerance: float = 0.05,
    # Default ON. Measured over three seeds per weight on the live male cohort,
    # reporting medians because a single run of this solver is a lottery:
    #
    #   weight   service pairs back to back   expected clashes   days
    #      0            15%  [9-19]            101.6 [93-104]    19 = floor
    #      1            54%  [54-67]           106.0 [99-113]    19 = floor
    #      3            70%  [65-80]           101.5 [99-109]    19 = floor
    #     10            83%  [70-93]           115.6 [96-117]    19 = floor
    #
    # At 3 the pairing gain is unambiguous — the ranges do not overlap at all —
    # while the clash medians are indistinguishable from having it switched off.
    # It is close to free, so it is on. At 10 the pairing keeps improving but
    # clashes start to move, and that is a trade for the owner to opt into.
    sibling_adjacency_weight: int = 3 * _SCALE,
    # Default OFF, and it stays off until the owner says otherwise. Measured on
    # the live male cohort, seating real students (two seeds, medians):
    #
    #   weight   student waiting   instructor idle   clash-free   days
    #      0        573 min            1262            100%       19 = floor
    #      1        465 min (-19%)     1532 (+21%)     100%       19 = floor
    #      3        498 min            2100 (+66%)     100%       19 = floor
    #     10        474 min            2078            100%       19 = floor
    #
    # Students and instructors are pulling on the SAME lever in opposite
    # directions — packing a student's day means spreading an instructor's, and
    # the reverse. The owner's stated priority is the instructor timetable with
    # the minimum gap possible, so buying students 19% at the instructors'
    # expense is not this system's call to make by default. Weight 1 is the only
    # setting worth offering: 3 and 10 are worse for BOTH parties.
    student_adjacency_weight: int = 0,
    max_time_of_day_slots: int | None = 1,
    #: D21 — how many days beyond the proven floor an instructor may take if it
    #: buys a more compact day. Default 0: days stay lexicographically first, as
    #: they have been. See the note at the pass-2 budget below.
    day_slack: int = 0,
    exact_block_tiers: frozenset[str] = frozenset({"T2", "T3"}),
    # Owner rule: T2 and T3 courses run in other sections right across the
    # college, so a student who cannot fit one here finds a seat elsewhere.
    # Measured on the M cohort: discounting them leaves instructor idle and
    # student waiting unchanged, and COLLAPSES the seed-to-seed spread on the
    # collisions that matter — T1-against-T1 went from 1.8-16.0 undiscounted to
    # 3.0-3.8. A number nobody can predict is worse than a slightly higher one
    # they can.
    tier_weights: dict[str, float] | None = None,
    # OFF by default, and that is a measured decision rather than caution. It
    # does what it claims -- the worst wander goes 130 -> 90 minutes and the one
    # noon-crossing pair on this grid disappears -- and it also takes instructor
    # idle from a median 955 minutes to 2700 and sibling back-to-back pairing
    # from 42.9% to 4.8%. The owner's stated priority is the instructor
    # timetable, so switching it on is their call, not this default's. See D14.
    max_time_of_day_minutes: int | None = None,
    **kwargs,
) -> SolveResult:
    """Plan in two passes: settle the working days, then attack the gaps.

    Working days and idle gaps pull against each other — spreading an
    instructor's sessions over more days shortens each day and cuts idle time,
    while packing them into fewer days creates gaps. Balancing the two with
    relative weights was measured to be **unstable**: at the weight that looked
    best, three seeds produced 19, 20 and 20 working days. A weight cannot
    promise anything about a quantity it merely trades against.

    So days are settled first and then frozen as a **budget**, not a preference:

    1. minimise working days (with clashes weighted as usual);
    2. re-solve with ``working days <= whatever pass 1 achieved``, and only then
       let the gap term push as hard as it likes.

    Pass 2 cannot fail and cannot regress the day count: pass 1's own board
    satisfies the budget, so it is always available as a fallback, and it is fed
    in as a hint so pass 2 starts from it rather than searching blind.

    The gap weight is deliberately much larger than anything tuned by hand would
    dare. That freedom has to be paid for somewhere, and measurement showed it
    was coming out of the students: with only the day budget in place, one seed
    reached 620 idle minutes but 132 expected clashes against roughly 104. So
    pass 2 carries a **second** budget — clashes may rise by at most
    ``clash_tolerance`` above what pass 1 achieved. Gaps are then minimised
    inside both ceilings, and what the students give up is a declared number
    rather than a side effect.

    Rooms are bounded the same way, and for the same reason. Left merely priced,
    they were the next thing the gap term spent: every run raised the unroomed
    count, the guard below rejected every one of those boards, and the whole gap
    improvement was discarded (idle 645 -> 3255). Three budgets — days, clashes,
    rooms — leave gaps as the only quantity still free to move, which is what
    makes the second pass worth running at all.
    """
    tier_weights = tier_weights or {"T1": 1.0, "T2": 0.5, "T3": 0.2}
    half = max(1.0, time_limit_seconds / 2)
    # Pass 1 answers ONE question — how few days can these instructors work —
    # so nothing else may pull on it. Measured: with the back-to-back reward
    # active here, the day count left the proven floor from weight 3 upward
    # (19 -> 20 -> 21), and because pass 2's budget is derived from pass 1, the
    # loss was then locked in and no later pass could recover it. Preferences
    # belong in pass 2, where days are already a hard budget and cannot be spent.
    first = solve(
        snapshot,
        time_limit_seconds=half,
        span_weight=0,
        sibling_adjacency_weight=0,
        max_time_of_day_slots=max_time_of_day_slots,
        max_time_of_day_minutes=max_time_of_day_minutes,
        exact_block_tiers=exact_block_tiers,
        tier_weights=tier_weights,
        **kwargs,
    )
    # Unlike every other preference, the time-of-day ceiling belongs in BOTH
    # passes. Pass 2's clash budget is derived from pass 1's score, so a ceiling
    # applied only later would be measured against a total achieved without it,
    # and pass 2 would be infeasible on arrival — silently falling back to a
    # pass-1 board that ignores the rule entirely.
    #
    # And unlike the other hard budgets, this one can genuinely have no solution:
    # it is the caller's policy, not something derived from a board already in
    # hand. A cohort that cannot meet it should still get a timetable, with the
    # compromise stated rather than discovered later on screen.
    if not first.board.placements and (
        max_time_of_day_slots is not None
        or max_time_of_day_minutes is not None
        or exact_block_tiers
    ):
        relaxed = solve(
            snapshot,
            time_limit_seconds=half,
            span_weight=0,
            sibling_adjacency_weight=0,
            max_time_of_day_slots=None,
            max_time_of_day_minutes=None,
            exact_block_tiers=frozenset(),
            tier_weights=tier_weights,
            **kwargs,
        )
        if relaxed.board.placements:
            asked = []
            if max_time_of_day_slots is not None:
                asked.append(f"{max_time_of_day_slots} slot(s)")
            if max_time_of_day_minutes is not None:
                asked.append(f"{max_time_of_day_minutes} minutes")
            relaxed.warnings.append(
                "no timetable keeps every section within "
                + " and ".join(asked)
                + " of itself; the rule was dropped"
            )
            # The failed attempt spent real time. Carrying it keeps the reported
            # figure a description of the run rather than of the last pass in it.
            relaxed.wall_time_seconds += first.wall_time_seconds
            # BOTH ceilings, not just the one named in the warning. Leaving the
            # minute bound on pass 2 measures it against a pass-1 total achieved
            # without it, so pass 2 is infeasible on arrival and the gap pass —
            # the entire reason plan() runs twice — silently never executes.
            max_time_of_day_slots = None
            max_time_of_day_minutes = None
            exact_block_tiers = frozenset()
            first = relaxed
        else:
            # Says which suspect has been ruled out. Without it the caller reads
            # "no assignment found" and reasonably blames the newest rule.
            #
            # `solve()` returns an empty board for a TIMEOUT as well as for a
            # proven INFEASIBLE, so the status has to be consulted before naming
            # a cause: telling somebody their data is impossible when the solver
            # merely ran out of seconds sends them to fix the wrong thing.
            proven = relaxed.status.upper().startswith("INFEASIBLE")
            first.warnings.append(
                "dropping the same-hour rule did not help either — this cohort is "
                + (
                    "infeasible for another reason; run sch_validate"
                    if proven
                    else f"unsolved after {relaxed.wall_time_seconds:.0f}s "
                    f"({relaxed.status}); try a longer budget before assuming the "
                    "data is at fault"
                )
            )
            first.wall_time_seconds += relaxed.wall_time_seconds
    if not first.board.placements:
        return first

    # Whatever the passes above actually spent, pass 2 gets the remainder — so
    # `time_limit_seconds` stays an upper bound on plan(). It stopped being one
    # the moment the relaxation retry was added: two half-budgets plus a third
    # made a "120 second" plan a 180 second one, and plan_portfolio multiplies
    # that by the seed count.
    remaining = max(1.0, time_limit_seconds - first.wall_time_seconds)

    per_instructor = _days_by_instructor(snapshot, first.board)
    if not per_instructor:  # no instructor data — nothing to protect, nothing to gain
        return first
    budget = sum(per_instructor.values())

    # Derived from the solver's own units, so a zero tolerance still admits pass
    # 1's board rather than rejecting it on a rounding difference.
    #
    # A score of zero means one of two completely different things, and reading
    # them as one was a real defect.
    #
    # When `alpha == 0` ("instructors only") the clash booleans carry no
    # objective pressure, so their values are arbitrary — they are only
    # lower-bounded — and a ceiling derived from them is meaningless. Worse, a
    # ceiling of 0 would read as "no student may ever clash", instantly
    # infeasible on a cohort where nearly everyone does; pass 2 would return
    # nothing and the mode would spend half its budget achieving exactly that.
    #
    # But when students ARE in the objective, a score of zero means pass 1 found
    # a **clash-free** board, and a ceiling of zero is then exactly right: do not
    # make it worse. Testing the score instead of the mode dropped the ceiling
    # precisely when it had the most to protect, and review reproduced the
    # consequence — a provably clash-free timetable converted into a clashing one
    # by the gap pass, reported as a clean success with nothing in `warnings`.
    # Pass 2 cannot become infeasible from it: pass 1's own board scores zero.
    bound_clashes = float(kwargs.get("alpha", 0.9)) > 0.0
    ceiling = math.floor(first.clash_score * (1.0 + clash_tolerance)) if bound_clashes else None
    second = solve(
        snapshot,
        time_limit_seconds=remaining,
        span_weight=gap_weight,
        # ── D21: let an instructor buy compactness with a day ──────────
        #
        # Pass 1 settles the working-day FLOOR and pass 2 has always been
        # forbidden from spending it, which is right when days are what matters
        # most. But it makes one week unreachable: three long days with holes in
        # them beats five short ones by this ordering, and the owner's own
        # hand-built boards choose the opposite — Dr Abdullah teaches 09:00-12:25
        # across five mornings rather than the three days the solver gives him,
        # and his idle falls from ~170 minutes to 65 for it.
        #
        # `day_slack` opens exactly that trade and nothing else. The budget below
        # is relaxed per instructor, and the guard after pass 2 still discards
        # any board where the extra day did NOT buy less idle — so a longer week
        # has to pay for itself.
        max_working_days=(
            {i: n + day_slack for i, n in per_instructor.items()} if day_slack else per_instructor
        ),
        max_clash_score=ceiling,
        max_room_shortfall=first.room_shortfall_score,
        sibling_adjacency_weight=sibling_adjacency_weight,
        student_adjacency_weight=student_adjacency_weight,
        max_time_of_day_slots=max_time_of_day_slots,
        max_time_of_day_minutes=max_time_of_day_minutes,
        exact_block_tiers=exact_block_tiers,
        tier_weights=tier_weights,
        hint=first.board,
        **kwargs,
    )
    # A meeting whose (duration, delivery) has no declared window gets no
    # variables at all: it lands in `unplaced` and vanishes from the board. The
    # fully-empty case is guarded in three places; the PARTIAL case was guarded
    # nowhere, and it is the dangerous one — every metric here is computed over
    # the board, so a timetable missing classes scores BETTER than one teaching
    # them. Warn rather than fail: the board is still the best available answer,
    # and the caller needs to know it is short.
    for outcome in (first, second):
        if outcome.unplaced and not any("could not be placed" in w for w in outcome.warnings):
            outcome.warnings.append(
                f"{len(outcome.unplaced)} meeting(s) could not be placed at all — "
                "every figure below is measured over what WAS placed, so it "
                f"flatters this board: {', '.join(sorted(outcome.unplaced)[:5])}"
            )
    total = first.wall_time_seconds + second.wall_time_seconds
    if not second.board.placements:
        first.notes.append("gap pass found nothing; kept the working-day board")
        first.wall_time_seconds = total
        return first

    # Pass 1 remains the fallback: pass 2 is only an improvement if it actually
    # cut idle time without making anyone's week longer. Checked per instructor,
    # not on the total, so a day moved from one person to another is caught.
    before = instructor_metrics(snapshot, first.board)["idle_minutes"]
    after = instructor_metrics(snapshot, second.board)["idle_minutes"]
    discard = reason_to_discard_gap_pass(
        days_before=per_instructor,
        days_after=_days_by_instructor(snapshot, second.board),
        rooms_before=unroomed_count(snapshot, first.board),
        rooms_after=unroomed_count(snapshot, second.board),
        idle_before=before,
        idle_after=after,
        day_slack=day_slack,
    )
    if discard:
        first.notes.append(discard)
        first.wall_time_seconds = total
        return first

    second.wall_time_seconds = total
    second.notes.append(f"two-pass: {budget} working days held; idle {before} -> {after} min")
    return second


def reason_to_discard_gap_pass(
    *,
    days_before: dict[int, int],
    days_after: dict[int, int],
    rooms_before: int,
    rooms_after: int,
    idle_before: int,
    idle_after: int,
    day_slack: int = 0,
) -> str | None:
    """Why pass 2's board should be thrown away, or None to keep it.

    Separated from `plan()` so the policy can be tested without contriving a
    pathological board. Every one of these guards only fires when the gap pass
    returns something WORSE under budgets that should have prevented it, so on
    any healthy fixture they are unreachable — which is exactly how three of
    them survived a mutation audit while looking well covered.

    Order matters and is the same order the rest of the engine uses:

    1. **Nobody's week gets longer** — by more than `day_slack` days (D21).
       Compared per instructor, never on the total, so a day moved from one
       person to another is caught rather than cancelling out.

       `day_slack` is the whole of D21 on this side: at 0 this is the original
       rule and no week may grow at all. Above 0 an instructor may take that
       many extra days IF the idle check below still shows a net gain — which is
       what makes it a trade rather than a licence, since a longer week that
       does not buy less idle is still discarded on rule 3.
    2. **Rooms before gaps.** Pass 2 is pushed hard on idle time and one
       unroomed meeting was measured to be worth about sixty idle minutes to
       it, so without this it will quietly sell a classroom for a shorter wait.
    3. **It has to have worked.** An unimproved board is pass 1's with extra
       wall time spent.
    """
    grew = sorted(i for i, n in days_after.items() if n > days_before.get(i, 0))
    worse = [i for i in grew if days_after[i] > days_before.get(i, 0) + max(0, day_slack)]
    if worse:
        return (
            f"gap pass lengthened the week for instructor(s) {worse} by more than "
            f"{day_slack} day(s); discarded"
        )
    if rooms_after > rooms_before:
        return f"gap pass left {rooms_after} meetings unroomed vs {rooms_before}; discarded"
    if grew and idle_after >= idle_before:
        # STRICTLY better, not merely no worse. Equal idle is an acceptable
        # outcome when nobody's week changed — it is pass 1's board by another
        # route — but once somebody is being asked to come in on an extra day,
        # "no worse" means they paid for nothing.
        return (
            f"gap pass lengthened the week for instructor(s) {grew} without "
            f"improving idle ({idle_after} vs {idle_before}); discarded"
        )
    if idle_after > idle_before:
        return f"gap pass did not improve idle ({idle_after} vs {idle_before}); discarded"
    return None


def unroomed_count(snapshot: Snapshot, board: Board) -> int:
    """Meetings this board leaves with nowhere to meet.

    Every stage that chooses between boards has to look at this. A class with no
    room cannot be taught, so trading rooms for shorter instructor gaps or fewer
    student clashes is not a trade any of these stages is entitled to make
    silently — and until this existed, both the second planning pass and the
    portfolio selector could do exactly that without anything noticing.
    """
    # Uses the SAME assigner the caller will ultimately publish with. Selecting
    # on the greedy count while delivering the exact one made the two disagree
    # (a portfolio note claiming 11 unroomed beside a board with 10), and a
    # comparison against a number nobody ships is not a comparison at all.
    from scheduler.rooms import assign_rooms_exact

    roomed = assign_rooms_exact(snapshot, board, time_limit_seconds=5.0)
    return sum(1 for p in roomed.placements if p.needs_room and p.room_id is None)


def _days_by_instructor(snapshot: Snapshot, board: Board) -> dict[int, int]:
    """How many distinct days each instructor works on this board."""
    seen: dict[int, set] = {}
    for p in board.placements:
        if p.instructor_id is not None:
            seen.setdefault(p.instructor_id, set()).add(p.day)
    return {instructor_id: len(days) for instructor_id, days in seen.items()}


def _working_days(snapshot: Snapshot, board: Board) -> int | None:
    """Total instructor working days on a board, or None if nobody is assigned."""
    per = _days_by_instructor(snapshot, board)
    return sum(per.values()) if per else None


def astray_count(drift: dict, ceiling: int | None) -> int:
    """How many sections broke the same-hour ceiling that was actually asked for.

    Takes the ceiling rather than assuming one, and reads the per-section
    histogram rather than the headline `within_one_slot`. That headline is
    measured against a literal 1, which is right for the default and wrong for
    every other setting: under `--same-time-slots 2` a board that honoured the
    rule at spread 2 would have scored worse than one that abandoned the rule and
    happened to land at spread 1, and under `--same-time-slots 0` the two would
    have been indistinguishable. Ranking boards by a rule nobody chose is worse
    than not ranking them at all.
    """
    if ceiling is None:
        return 0  # no rule was asked for, so nothing can break it
    spreads = drift.get("by_rank_spread")
    if spreads is None:  # a plan stored before the histogram existed
        if ceiling <= 0:
            return drift["sections_with_several_meetings"] - drift["same_slot"]
        if ceiling == 1:
            return drift["sections_with_several_meetings"] - drift["within_one_slot"]
        return 0
    return sum(count for spread, count in spreads.items() if spread > ceiling)


def choose_run(runs: list[dict], *, idle_band: float = 0.10) -> dict:
    """Pick one board out of several, by a stated order of preference.

    Separated from the search so the policy can be read, argued with and tested
    without spending minutes of solver time to produce two boards to compare.
    Each run is a dict of already-measured facts: ``days`` (per-instructor
    counts), ``unroomed``, ``astray``, ``metrics['idle_minutes']``, ``clashes``.

    1. **Never lengthen anyone's week.** Compared per instructor rather than on
       the total, so a day moved from one person to another is not mistaken for
       neutral.
    2. **Then rooms**, because a class with nowhere to meet cannot be taught at
       all — without this the selector would strand five classes to save a
       minute of waiting.
    3. **Then sections that wander off their hour.** Normally every board scores
       zero here, because the ceiling inside `plan()` is hard — but `plan()`
       drops that ceiling when it cannot be met, and "cannot be met" is decided
       under a time limit, so one seed can return having kept the rule and
       another having abandoned it. Ranking on idle minutes alone could then
       prefer the one that gave the rule up.
    4. **Then the shortest total waiting**, the owner's stated priority — but
       treating idle times within ``idle_band`` of the best as equivalent and
       breaking that tie on student clashes. Without the band a board saving one
       minute of waiting would beat one saving thirty clashes.
    """
    contenders = list(runs)
    for rank in (
        lambda r: sorted(r["days"], reverse=True),
        lambda r: r["unroomed"],
        lambda r: r["astray"],
    ):
        best = min(rank(r) for r in contenders)
        contenders = [r for r in contenders if rank(r) == best]

    best_idle = min(r["metrics"]["idle_minutes"] for r in contenders)
    cutoff = best_idle * (1.0 + idle_band) + 1e-9
    within = [r for r in contenders if r["metrics"]["idle_minutes"] <= cutoff] or contenders
    return min(within, key=lambda r: (r["clashes"], r["metrics"]["idle_minutes"]))


def plan_portfolio(
    snapshot: Snapshot,
    *,
    seeds: tuple[int, ...] = (1, 2, 3),
    time_limit_seconds: float = 120.0,
    idle_band: float = 0.10,
    **kwargs,
) -> SolveResult:
    """Run the planner from several starting points and keep the best board.

    CP-SAT gives no useful lower bound on this objective — the clash-indicator
    relaxation is 0, so optimality can never be proven and the only honest
    comparison is empirical. What the measurements *do* show is a wide spread
    between runs that differ solely by random seed: on the live cohort, expected
    clashes ranged 78 to 110 and idle time 645 to 1935. A single run is therefore
    a lottery ticket, and re-running is the cheapest quality available.

    **Choosing between boards is itself a policy decision**, so it is made
    explicitly rather than by a scalar nobody can interpret, and it lives in
    `choose_run` — separate from the search, so it can be read and tested
    without spending minutes of solver time to produce two boards to compare.
    """
    runs: list[dict] = []
    for seed in seeds:
        result = plan(snapshot, time_limit_seconds=time_limit_seconds, seed=seed, **kwargs)
        if not result.board.placements:
            continue
        drift = time_of_day_drift(snapshot, result.board)
        runs.append(
            {
                "result": result,
                "metrics": instructor_metrics(snapshot, result.board),
                "clashes": expected_clashes(snapshot, result.board),
                "unroomed": unroomed_count(snapshot, result.board),
                "astray": astray_count(drift, kwargs.get("max_time_of_day_slots", 1)),
                "days": list(_days_by_instructor(snapshot, result.board).values()),
            }
        )
    if not runs:
        return plan(snapshot, time_limit_seconds=time_limit_seconds, **kwargs)

    best = choose_run(runs, idle_band=idle_band)
    chosen = best["result"]
    chosen.notes.append(
        f"portfolio of {len(runs)}: kept days {best['metrics']['working_days']}, "
        f"unroomed {best['unroomed']}, idle {best['metrics']['idle_minutes']}, "
        f"clashes {best['clashes']:.1f} (spread across runs: unroomed "
        f"{min(r['unroomed'] for r in runs)}-{max(r['unroomed'] for r in runs)}, idle "
        f"{min(r['metrics']['idle_minutes'] for r in runs)}-"
        f"{max(r['metrics']['idle_minutes'] for r in runs)}, clashes "
        f"{min(r['clashes'] for r in runs):.0f}-{max(r['clashes'] for r in runs):.0f})"
    )
    return chosen


def _max_matching(pairs: list[tuple[str, str]]) -> int:
    """Largest set of adjacent pairs that share no section.

    The owner's rule pairs sections two at a time, so a course with three
    sections can only ever achieve ONE pair however its adjacencies fall.
    Counting raw adjacencies instead would report 3 sections in one consecutive
    block as two successes when the rule asks for one.

    Exhaustive: sibling counts here are 2-4, so there is nothing to gain from a
    cleverer algorithm and plenty to lose from getting one subtly wrong.
    """
    if not pairs:
        return 0
    best = 0
    for index, (a, b) in enumerate(pairs):
        rest = [p for p in pairs[index + 1 :] if a not in p and b not in p]
        best = max(best, 1 + _max_matching(rest))
    return best


def sibling_adjacency(snapshot: Snapshot, board: Board) -> dict:
    """How many sibling sections were paired back to back, against how many could be.

    The denominator is the achievable maximum -- floor(n/2) pairs per course per
    weekly meeting -- not every combination of siblings. A course with three
    sections can reach one pair and no more, so scoring it out of three would
    make a perfect timetable look like a third of one.
    """
    by_offering = snapshot.sections_by_offering
    placed: dict[tuple[str, int], list] = {}
    for p in board.placements:
        placed.setdefault((p.section_id, p.meeting_index), []).append(p)

    achieved = achievable = 0
    per_course: dict[str, tuple[int, int]] = {}
    for offering_id, siblings in sorted(by_offering.items()):
        ids = sorted(s.id for s in siblings)
        if len(ids) < 2:
            continue
        offering = snapshot.offerings_by_id[offering_id]
        if offering.occupies_fixed_block:
            # Same reason as the objective: pairing siblings by meeting index is
            # meaningless when every section holds every cell. ENG101 alone
            # contributed 20 "achievable" pairs against a typical course's 2-3,
            # which is published as the back-to-back percentage.
            continue
        code = offering.course_code
        hits = target = 0
        indexes = {i for (sid, i) in placed if sid in ids}
        for index in sorted(indexes):
            present = [sid for sid in ids if placed.get((sid, index))]
            target += len(present) // 2
            adjacent = []
            for first, second in combinations(present, 2):
                pa, pb = placed[(first, index)][0], placed[(second, index)][0]
                if pa.day is not pb.day:
                    continue
                gap = max(
                    pb.window.start - pa.window.end,
                    pa.window.start - pb.window.end,
                )
                if 0 <= gap <= _ADJACENT_GAP_MINUTES:
                    adjacent.append((first, second))
            hits += _max_matching(adjacent)
        if target:
            per_course[code] = (hits, target)
            achieved += hits
            achievable += target

    return {
        "pairs_back_to_back": achieved,
        "pairs_achievable": achievable,
        "percent": round(100.0 * achieved / achievable, 1) if achievable else 0.0,
        "per_course": per_course,
    }


def time_of_day_drift(snapshot: Snapshot, board: Board) -> dict:
    """How far a section's weekly meetings wander across the day.

    Reported from the finished board rather than from the solver, so it says
    what the timetable actually does and not what the objective was told to
    want. Drift is measured within a requirement family — the spread between the
    earliest and latest start of a section's lectures, and separately of its
    labs — because those are drawn from different declared families and were
    never expected to line up.

    ``same_slot`` and ``within_one_slot`` are the two outcomes the owner asked
    for; ``worst_minutes`` is the section that wanders furthest, which is the
    one worth looking at when the numbers disappoint.

    "Within one slot" is counted in RANK — how many declared starts apart the
    meetings are — because this grid's lecture family runs 09:00, 10:30, 10:50,
    13:00, 14:30, 14:45, 16:00, and no number of minutes separates "the next
    slot" from "after lunch".
    """
    # D19 sections are excluded outright: a course that owns a whole block of
    # the day uses every slot in it by definition, so "wander" is 90 minutes for
    # every one of them and they drag the same-hour percentage down for keeping
    # exactly the rule they were given.
    fixed = {
        section.id
        for section in snapshot.sections
        if snapshot.offerings_by_id[section.offering_id].occupies_fixed_block
    }
    families: dict[tuple[str, int, object], list[int]] = {}
    for placement in board.placements:
        if placement.section_id in fixed:
            continue
        families.setdefault(
            (placement.section_id, placement.window.duration, placement.delivery), []
        ).append(placement.window.start)

    total = same = within = 0
    drift_minutes = 0
    worst: tuple[int, str] = (0, "")
    by_rank_spread: dict[int, int] = {}
    for (section_id, duration, delivery), starts in sorted(
        families.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))
    ):
        if len(starts) < 2:
            continue
        legal = sorted({w.start for w in snapshot.grid.windows_for(duration, delivery)})
        if len(legal) < 2:
            continue
        rank_of = {start: i for i, start in enumerate(legal)}
        ranks = [rank_of[s] for s in starts if s in rank_of]
        spread = max(starts) - min(starts)
        total += 1
        drift_minutes += spread
        if spread == 0:
            same += 1
        if ranks:
            rank_spread = max(ranks) - min(ranks)
            by_rank_spread[rank_spread] = by_rank_spread.get(rank_spread, 0) + 1
            if rank_spread <= 1:
                within += 1
        if spread > worst[0]:
            worst = (spread, section_id)

    return {
        "sections_with_several_meetings": total,
        "same_slot": same,
        "within_one_slot": within,
        # How many sections sit 0, 1, 2 ... declared slots from themselves. Kept
        # as a histogram so a ceiling other than 1 can still be scored: reporting
        # only "within one slot" forced `astray_count` to decline to rank a
        # portfolio whenever the caller asked for anything else.
        "by_rank_spread": dict(sorted(by_rank_spread.items())),
        "percent_same_slot": round(100.0 * same / total, 1) if total else 0.0,
        "percent_within_one_slot": round(100.0 * within / total, 1) if total else 0.0,
        "mean_drift_minutes": round(drift_minutes / total, 1) if total else 0.0,
        "worst_minutes": worst[0],
        "worst_section": worst[1],
    }
