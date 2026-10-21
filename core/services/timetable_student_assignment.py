"""Student→section assignment and the timetable objective.

This module owns two things that must agree with each other:

1. **The seating** — ``assign_students_to_sections`` walks students in
   risk-tier order and, per student, seats each recommended course via
   ``assign_courses_for_student``; ``repair_unresolved_assignments_shallow``
   then makes a second pass that may swap a blocking course to a different
   section to rescue an unresolved one.
2. **The objective** — ``evaluate_assignability_lexicographic`` scores the
   resulting board as a lexicographic tuple (smaller = better, most-significant
   position first). It is produced here in ONE place and consumed positionally
   in several others, so decode it with :func:`decode_score` / the accessors
   rather than fixed indices.

Objective layouts
-----------------
*Legacy* (``TIMETABLE_TIERED_OBJECTIVE_ENABLED`` off) — the canonical 6-tuple,
or 7 when the instructor-gap flag appends an idle term::

    (tier_a, unresolved_students, unassigned_courses, clashes,
     gap_minutes[+spread folded in], reserve) [, instructor_idle]

*Tiered* (flag on **and** a ``course_tiers`` map threaded in) — a fixed 9-tuple
that ranks resolution by course tier (T1 specialised major / T2 shared
foundation / T3 gen-ed, see ``timetable_course_tier``)::

    0 high-risk unresolved (RiskTier.A student with an unresolved T1|T2 course)
    1 student clashes
    2 Tier-1 unresolved (student, course) pairs        -> hard, drive to 0
    3 Tier-2 unresolved beyond the per-course tolerance
    4 student cost = real_gap_minutes + budget * soft_unresolved
    5 soft count (Tier-3 + Tier-2 within tolerance)
    6 reserve used
    7 same-course section spread (its own quality term)
    8 instructor idle minutes

Position 4 is a **bounded trade**, not strict priority: gaps and soft-tier
enrolment share one axis, so a soft course is seated exactly when doing so adds
fewer than ``TIMETABLE_TIERED_SOFT_GAP_BUDGET`` gap-minutes. Real gap is
recoverable as ``score[4] - budget * score[5]``. Setting the budget to 0
reproduces strict quality-first. Note real gap is **unbundled** from the
same-course spread pseudo-penalty (position 7); the legacy tuple sums them.

Tier-awareness in the seating
-----------------------------
``course_tiers`` is load-bearing on the *assignment*, not only the score: the
per-student course order leads with the tier rank so core courses are seated
before foundation/gen-ed (scarcity remains the within-tier tiebreak), and the
repair pass rescues core first. **Any caller that reconstructs seating must
thread the same tier map**, otherwise it models a board that was never built.

Byte-parity: with ``course_tiers`` absent every course ranks 0, so the sort keys
collapse to the original ordering and the legacy tuple is returned verbatim —
flag-off output is identical to pre-feature.

Programme grouping
------------------
A section may be marked "mainly for programme X"
(``SectionState.preferred_programme``, set only under
``TIMETABLE_SECTION_PROGRAMME_PREFERENCE``). After the seating and the repair
pass, ``_group_by_programme`` re-sections students who are ALREADY seated so X's
students sit in X's section and other programmes prefer the other sections. It
never adds, removes or swaps out a course, never raises a student's clash count
and never changes a course's free seats. An X student may take a reserve seat
in X's section when the move frees a regular seat in another section of the
same course (the spare seat only shifts; reported as ``reserve_seats_taken``).
That is not capacity pressure, so the pass's change to each section's reserve
use is handed back (``reserve_shift``) and every reserve reader subtracts it:
every hard position of the objective, reserve included, is the same or better
than without marks — on every board, by construction, with a guard behind it.
Student gap may change (the reported trade). With no marked section the pass is
never entered and the output is the frozen seating.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import islice, product
from operator import itemgetter

from core.services.timetable_assignment_models import (
    RiskTier,
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    StudentProfile,
    UnresolvedReason,
)
from core.services.timetable_flags import (
    get_tiered_soft_gap_budget,
    get_tiered_t2_tolerance,
    is_gap_regular_students_only_enabled,
    is_tiered_objective_enabled,
)
from core.services.timetable_pr4_instructor import is_instructor_gap_penalty_enabled
from core.services.timetable_same_course import (
    make_meeting_window,
    same_course_section_spread_penalty,
)

logger = logging.getLogger(__name__)


def build_sections_by_course(
    sections_by_id: dict[str, SectionState],
) -> dict[str, list[SectionState]]:
    out: dict[str, list[SectionState]] = defaultdict(list)
    for sec in sections_by_id.values():
        out[sec.course_code].append(sec)
    for course_code in out:
        out[course_code].sort(key=lambda s: s.section_id)
    return dict(out)


def build_sections_by_id(sections: list[SectionState]) -> dict[str, SectionState]:
    out: dict[str, SectionState] = {}
    for sec in sections:
        if sec.section_id in out:
            raise ValueError(f"Duplicate section_id detected: {sec.section_id}")
        out[sec.section_id] = sec
    return out


def reserve_allowed_for_tier(
    tier: RiskTier,
    reserves_released: bool,
    allow_tier_b: bool = True,
) -> bool:
    if reserves_released:
        return True
    if tier == RiskTier.A:
        return True
    if tier == RiskTier.B and allow_tier_b:
        return True
    return False


# Order a student's courses by tier when a tier map is supplied: core first.
# Lower rank = higher priority, so a rank comparison reads as "more important".
COURSE_TIER_RANK = {"T1": 0, "T2": 1, "T3": 2}


def _tier_rank(course_tiers: dict[str, str] | None, course_code: str) -> int:
    """Priority rank of a course (0=T1 core .. 2=T3 gen-ed); 0 with no map."""
    if not course_tiers:
        return 0
    return COURSE_TIER_RANK.get(course_tiers.get(course_code, "T1"), 0)


# ── Seating engine ───────────────────────────────────────────────────────
#
# Everything in this block is a pure speed-up of the original per-student
# seating loop and MUST stay bit-identical to it (assignments, unresolved
# reasons, gap totals, dict/set insertion order).
# ``tests/_reference_student_assignment.py`` is the frozen original and
# ``tests/test_student_assignment_parity.py`` asserts the two agree. The ideas,
# all exact:
#
# 1. Per-section facts (``(day, mask)`` pairs, campus meetings grouped by day)
#    are derived ONCE per evaluation instead of re-filtered per student/call.
# 2. Each student's campus day-chains are kept incrementally (``_Seat``): seating
#    or unseating a section touches only its own days, instead of re-deriving the
#    whole week for every candidate and every seat.
# 3. Ranking only prices what can still win: an option that uses the reserve
#    loses to any regular-seat option (position 1 of the score), and a lone
#    option needs no pricing at all.

_START = itemgetter(0)


def _sorted_gap(chain: list[tuple[int, int]]) -> int:
    """Idle minutes in a start-sorted ``[(start, end), ...]`` day chain.

    Same arithmetic as ``_compute_day_gap``: each meeting's gap is measured from
    the END of the meeting immediately before it in start order (not a running
    maximum), floored at zero.
    """
    if len(chain) < 2:
        return 0
    it = iter(chain)
    prev_end = next(it)[1]
    gap = 0
    for start, end in it:
        if start > prev_end:
            gap += start - prev_end
        prev_end = end
    return gap


class _SecInfo:
    """Per-section facts the seating loop needs, derived once per evaluation."""

    __slots__ = ("section", "dm", "campus")

    def __init__(self, section: SectionState) -> None:
        self.section = section
        # (day, mask) for EVERY meeting, online included: the clash/occupancy
        # model is deliberately online-blind, exactly like ``has_clash``.
        self.dm: tuple[tuple[int, int], ...] = tuple((m.day, m.mask) for m in section.meetings)
        by_day: dict[int, list[tuple[int, int]]] = {}
        for m in _campus_meetings(section):
            by_day.setdefault(m.day, []).append((m.start_min, m.end_min))
        # (day, meetings in section order, the same stable-sorted by start, gap of
        # those meetings alone). The "alone" gap is what the section adds on a day
        # the student has nothing else on campus, and the sorted copy is the
        # student's whole chain for that day when it is the first thing seated.
        campus = []
        for day, ms in by_day.items():
            ordered = sorted(ms, key=_START)
            campus.append((day, ms, ordered, _sorted_gap(ordered)))
        self.campus: tuple[tuple[int, list[tuple[int, int]], list[tuple[int, int]], int], ...] = (
            tuple(campus)
        )


class _InfoMap(dict):
    """``section_id -> _SecInfo`` for one evaluation, built lazily from the board."""

    __slots__ = ("sections_by_id",)

    def __init__(self, sections_by_id: dict[str, SectionState]) -> None:
        super().__init__()
        self.sections_by_id = sections_by_id

    def of(self, section: SectionState) -> _SecInfo:
        info = self.get(section.section_id)
        if info is None or info.section is not section:
            info = _SecInfo(section)
            self[section.section_id] = info
        return info

    def seated(self, section_id: str) -> _SecInfo:
        return self.of(self.sections_by_id[section_id])


class _Seat:
    """A student's campus day-chains, kept in step with ``section_ids``.

    ``by_day[d]`` holds the (start, end) of every seated ON-CAMPUS meeting on day
    ``d`` in stable start order — the list ``_compute_day_gap`` sorts.
    ``gap_by_day[d]`` is that chain's idle minutes and ``total`` their sum, i.e.
    exactly ``_compute_total_state_gap``.
    """

    __slots__ = ("by_day", "gap_by_day", "total")

    def __init__(self) -> None:
        self.by_day: dict[int, list[tuple[int, int]]] = {}
        self.gap_by_day: dict[int, int] = {}
        self.total = 0

    @classmethod
    def from_state(cls, state: StudentAssignmentState, infos: _InfoMap) -> _Seat:
        seat = cls()
        for sec_id in state.section_ids:
            for day, ms, _ordered, _alone in infos.seated(sec_id).campus:
                seat.by_day.setdefault(day, []).extend(ms)
        for day, chain in seat.by_day.items():
            chain.sort(key=_START)
            gap = _sorted_gap(chain)
            seat.gap_by_day[day] = gap
            seat.total += gap
        return seat

    def add(self, info: _SecInfo) -> None:
        by_day = self.by_day
        for day, ms, ordered, alone in info.campus:
            chain = by_day.get(day)
            if not chain:
                # First thing on campus that day: the chain is just this section's.
                by_day[day] = ordered.copy()
                self.total += alone - self.gap_by_day.get(day, 0)
                self.gap_by_day[day] = alone
                continue
            chain.extend(ms)
            chain.sort(key=_START)
            gap = _sorted_gap(chain)
            self.total += gap - self.gap_by_day[day]
            self.gap_by_day[day] = gap

    def drop(self, info: _SecInfo) -> None:
        for day, ms, _ordered, _alone in info.campus:
            chain = self.by_day[day]
            for meeting in ms:
                chain.remove(meeting)
            gap = _sorted_gap(chain)
            self.total += gap - self.gap_by_day[day]
            self.gap_by_day[day] = gap

    def added_gap(self, info: _SecInfo) -> int:
        """Gap minutes the section would add (``calculate_added_gap``)."""
        added = 0
        by_day = self.by_day
        for day, ms, _ordered, alone in info.campus:
            chain = by_day.get(day)
            if chain:
                added += _sorted_gap(sorted(chain + ms, key=_START)) - self.gap_by_day[day]
            else:
                added += alone
        return added


class _Seats(dict):
    """``student_id -> _Seat``, built lazily from each student's current state."""

    __slots__ = ("states", "infos")

    def __init__(self, states: dict[str, StudentAssignmentState], infos: _InfoMap) -> None:
        super().__init__()
        self.states = states
        self.infos = infos

    def __missing__(self, student_id: str) -> _Seat:
        seat = self[student_id] = _Seat.from_state(self.states[student_id], self.infos)
        return seat


class _CoursePairs(dict):
    """``course_code -> [(section, _SecInfo), ...]`` in ``sections_by_course`` order."""

    __slots__ = ("sections_by_course", "infos")

    def __init__(self, sections_by_course: dict[str, list[SectionState]], infos: _InfoMap) -> None:
        super().__init__()
        self.sections_by_course = sections_by_course
        self.infos = infos

    def __missing__(self, course_code: str) -> list[tuple[SectionState, _SecInfo]]:
        infos = self.infos
        pairs = self[course_code] = [
            (s, infos.of(s)) for s in self.sections_by_course.get(course_code, ())
        ]
        return pairs


def _clashes(occ: dict[int, int], info: _SecInfo) -> bool:
    for day, mask in info.dm:
        if occ[day] & mask:
            return True
    return False


def _seatable(
    pairs: list[tuple[SectionState, _SecInfo]], allow_reserve: bool
) -> list[tuple[SectionState, _SecInfo, bool]]:
    """The sections with a seat this student may take: ``can_enroll(allow_reserve)``.

    Each comes with ``uses_reserve`` (the seat is past the regular limit). Seat
    availability of a course's sections cannot change while one student is being
    seated (only that course's own pick touches them), so the result is reusable
    from the scarcity count through to the pick.
    """
    out = []
    for section, info in pairs:
        cur = section.current_enrollment
        cap = section.max_capacity
        if cur >= cap:
            continue
        uses_reserve = cur >= cap - section.reserve_capacity
        if uses_reserve and not allow_reserve:
            continue
        out.append((section, info, uses_reserve))
    return out


def _pick_among(
    seatable: list[tuple[SectionState, _SecInfo, bool]],
    occ: dict[int, int],
    seat: _Seat,
) -> tuple[SectionState, _SecInfo] | None:
    """The section ``rank_and_select_best_section`` would choose, or None.

    The original ranks every clash-free option by
    ``(0, uses_reserve, added_gap, fill_ratio_bp, section_id)`` and takes the
    minimum; ``section_id`` is unique so the order is total. Because
    ``uses_reserve`` leads, only the regular-seat options (or, failing that, the
    reserve ones) can win, and a single survivor needs no pricing.
    """
    regular: list[tuple[SectionState, _SecInfo, bool]] = []
    reserve: list[tuple[SectionState, _SecInfo, bool]] = []
    for option in seatable:
        for day, mask in option[1].dm:
            if occ[day] & mask:
                break
        else:
            (reserve if option[2] else regular).append(option)
    options = regular or reserve
    if not options:
        return None
    if len(options) == 1:
        return options[0][0], options[0][1]
    best_key = None
    best = options[0]
    for option in options:
        section = option[0]
        cap = max(1, section.max_capacity)
        key = (
            seat.added_gap(option[1]),
            int(((section.current_enrollment + 1) / cap) * 10000),
            section.section_id,
        )
        if best_key is None or key < best_key:
            best_key = key
            best = option
    return best[0], best[1]


def _apply(
    state: StudentAssignmentState,
    seat: _Seat,
    section: SectionState,
    info: _SecInfo,
) -> None:
    state.assigned_sections[section.course_code] = section.section_id
    state.section_ids.add(section.section_id)
    state.unresolved_courses.pop(section.course_code, None)
    occ = state.occupied_mask_by_day
    for day, mask in info.dm:
        occ[day] |= mask
    section.current_enrollment += 1
    section.enrolled_student_ids.add(state.student_id)
    seat.add(info)
    state.total_gap_minutes = seat.total


def _remove(
    state: StudentAssignmentState,
    seat: _Seat,
    section: SectionState,
    infos: _InfoMap,
) -> None:
    if state.assigned_sections.get(section.course_code) != section.section_id:
        return
    del state.assigned_sections[section.course_code]
    state.section_ids.discard(section.section_id)
    occ = {i: 0 for i in range(7)}
    state.occupied_mask_by_day = occ
    for rem_sec_id in state.section_ids:
        for day, mask in infos.seated(rem_sec_id).dm:
            occ[day] |= mask
    section.current_enrollment -= 1
    section.enrolled_student_ids.discard(state.student_id)
    seat.drop(infos.of(section))
    state.total_gap_minutes = seat.total


def _diagnose(
    pairs: list[tuple[SectionState, _SecInfo]],
    occ: dict[int, int],
    allow_reserve: bool,
) -> str:
    """``diagnose_unresolved`` with the clash test read off the cached masks."""
    if not pairs:
        return "no_sections"
    clash_only = 0
    full_only = 0
    reserve_only = 0
    feasible = 0
    for c, info in pairs:
        clash = _clashes(occ, info)
        cur = c.current_enrollment
        cap = c.max_capacity
        can_reserve = cur < cap  # can_enroll(allow_reserve=True)
        can_regular = can_reserve and cur < cap - c.reserve_capacity  # ...(False)
        can_with_policy = can_reserve if allow_reserve else can_regular
        if (not clash) and can_with_policy:
            feasible += 1
            continue
        if clash and (not can_with_policy):
            continue
        if clash:
            clash_only += 1
        elif not can_with_policy:
            if can_regular:
                full_only += 1
            else:
                if not allow_reserve and can_reserve:
                    reserve_only += 1
                else:
                    full_only += 1
    if feasible > 0:
        return "unknown"
    n = len(pairs)
    if clash_only == n:
        return "all_clash"
    if full_only == n:
        return "full"
    if reserve_only == n:
        return "reserve_only"
    return "mixed_blockers"


def _assign_courses(
    student: StudentProfile,
    state: StudentAssignmentState,
    seat: _Seat,
    pairs_by_course: _CoursePairs,
    course_rigidity: dict[str, float],
    reserves_released: bool,
    course_tiers: dict[str, str] | None,
) -> None:
    allow_reserve = reserve_allowed_for_tier(student.risk_tier, reserves_released)
    assigned = state.assigned_sections
    unresolved = state.unresolved_courses

    if unresolved:
        for course_code in student.recommended_courses:
            if course_code not in assigned:
                unresolved.pop(course_code, None)

    unassigned = set(student.recommended_courses).difference(assigned)

    occ = state.occupied_mask_by_day
    occupied = any(occ.values())
    seatable_by_course = {}
    keys: dict[str, tuple[int, int, float, str]] = {}
    for course_code in unassigned:
        seatable = seatable_by_course[course_code] = _seatable(
            pairs_by_course[course_code], allow_reserve
        )
        if occupied:
            feasible_count = sum(1 for _s, info, _r in seatable if not _clashes(occ, info))
        else:
            feasible_count = len(seatable)
        # Tier rank leads the key so a student's Tier-1 (core) courses are seated
        # BEFORE Tier-2/Tier-3 — otherwise the scarcity heuristic can hand a
        # scarce foundation/gen-ed course a slot that a core course needed, and
        # the core seat is lost. Without a tier map every course ranks 0, so the
        # ordering collapses to the original (feasible_count, -rigidity, code).
        keys[course_code] = (
            _tier_rank(course_tiers, course_code),
            feasible_count,
            -course_rigidity.get(course_code, 0.0),
            course_code,
        )

    for course_code in sorted(unassigned, key=keys.__getitem__):
        best = _pick_among(seatable_by_course[course_code], state.occupied_mask_by_day, seat)
        if best is not None:
            _apply(state, seat, best[0], best[1])
        else:
            reason_str = _diagnose(
                pairs_by_course[course_code], state.occupied_mask_by_day, allow_reserve
            )
            unresolved[course_code] = UnresolvedReason(course_code, reason_str)


def _blocking_courses(
    state: StudentAssignmentState, candidate_info: _SecInfo, infos: _InfoMap
) -> list[str]:
    """``find_blocking_assigned_courses`` over the cached ``(day, mask)`` pairs."""
    cand_dm = candidate_info.dm
    blockers: list[str] = []
    for assigned_sec_id in state.section_ids:
        assigned = infos.seated(assigned_sec_id)
        if any(da == db and ma & mb for da, ma in cand_dm for db, mb in assigned.dm):
            blockers.append(assigned.section.course_code)
    return blockers


def _repair(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    pairs_by_course: _CoursePairs,
    seats: dict[str, _Seat],
    course_tiers: dict[str, str] | None,
) -> None:
    infos = pairs_by_course.infos
    sections_by_id = infos.sections_by_id
    ordered_students = sorted(
        states.keys(),
        key=lambda sid: (profiles[sid].risk_tier, profiles[sid].intra_tier_score, sid),
        reverse=True,
    )

    for student_id in ordered_students:
        state = states[student_id]
        if not state.unresolved_courses:
            continue
        seat = seats[student_id]
        student = profiles[student_id]
        allow_reserve = reserve_allowed_for_tier(student.risk_tier, reserves_released=True)
        unresolved_list = list(state.unresolved_courses.keys())
        if course_tiers:
            # Repair core first: a Tier-1 course gets first claim on the limited
            # swap budget, so it is not spent rescuing a gen-ed course instead.
            # Key on the tier ALONE — Python's sort is stable, so the existing
            # order (unresolved_courses is insertion-ordered by the scarcity key
            # from assign_courses_for_student) is preserved *within* each tier.
            # Adding a course-code tiebreak here would silently replace that
            # deliberate hardest-first ordering with alphabetical.
            unresolved_list.sort(key=lambda c: _tier_rank(course_tiers, c))

        for unres_course in unresolved_list:
            if unres_course in state.assigned_sections:
                state.unresolved_courses.pop(unres_course, None)
                continue
            candidate_pairs = pairs_by_course[unres_course]
            direct = _pick_among(
                _seatable(candidate_pairs, allow_reserve), state.occupied_mask_by_day, seat
            )
            if direct is not None:
                _apply(state, seat, direct[0], direct[1])
                continue

            repaired = False
            for candidate, candidate_info in candidate_pairs:
                if not candidate.can_enroll(allow_reserve):
                    continue
                blocking_courses = sorted(set(_blocking_courses(state, candidate_info, infos)))
                if len(blocking_courses) != 1:
                    continue
                blocking_course = blocking_courses[0]
                # NOTE: deliberately NO tier guard on eviction. The swap below
                # only commits if the displaced course finds an alternative
                # section (else it rolls back), so a higher-tier course never
                # loses its seat — it merely moves. Blocking the swap would
                # forfeit an unresolved seat to protect a section choice.
                #
                # That trade is clearly right when the course being repaired is
                # T1 (objective position 2) or T2-over-tolerance (position 3),
                # since both outrank the gap-minutes (position 4) the relocation
                # costs. For a SOFT repair (T3 / T2-within-tolerance) the seat
                # lives at position 5, *below* gap — so it is only worth it while
                # the added gap stays under the soft-gap budget. Measured on scn
                # 642 the swaps came in at ~116 gap-min per soft seat against a
                # 120 budget, i.e. roughly break-even, so it is left ungated;
                # a budget-aware gate here is a possible future refinement.
                # Guarding it outright pushed Tier-2 over-tolerance 53 -> 73 and
                # soft 59 -> 93 for zero core gain.
                blocking_section_id = state.assigned_sections.get(blocking_course)
                if not blocking_section_id:
                    continue
                blocking_section = sections_by_id[blocking_section_id]
                blocking_info = infos.of(blocking_section)
                _remove(state, seat, blocking_section, infos)
                if _clashes(state.occupied_mask_by_day, candidate_info) or (
                    not candidate.can_enroll(allow_reserve)
                ):
                    _apply(state, seat, blocking_section, blocking_info)
                    continue
                _apply(state, seat, candidate, candidate_info)
                alt_pairs = [
                    pair
                    for pair in pairs_by_course[blocking_course]
                    if pair[0].section_id != blocking_section.section_id
                ]
                alt = _pick_among(
                    _seatable(alt_pairs, allow_reserve), state.occupied_mask_by_day, seat
                )
                if alt is not None:
                    _apply(state, seat, alt[0], alt[1])
                    state.unresolved_courses.pop(unres_course, None)
                    repaired = True
                    break
                _remove(state, seat, candidate, infos)
                _apply(state, seat, blocking_section, blocking_info)
            if not repaired and unres_course not in state.assigned_sections:
                reason = _diagnose(candidate_pairs, state.occupied_mask_by_day, allow_reserve)
                state.unresolved_courses[unres_course] = UnresolvedReason(unres_course, reason)


# ── Programme grouping ("mainly for programme") ──────────────────────────
#
# A post-pass over a FINISHED seating, run only when some section is marked.
# Each student keeps exactly the courses they were seated in; only which section
# of a marked course they sit in may change. That is what makes it safe to rank
# below seating: unresolved courses (and so every tier position) are untouched,
# a step is taken only if the student's clash-pair count does not rise, and a
# student moves only into a regular seat, out of a section already using its
# reserve (net zero), or — their programme's own marked section only — into its
# reserve while leaving a regular seat of the same course. That last case never
# changes the course's free seats; physical reserve use rises by at most the
# number of such moves, and a regular seat wins over one when both group equally
# well. Reserve readers (objective, reserve-heavy listing) subtract the pass's
# per-section change, so they read the seating as before grouping. The frozen
# seating code above is not touched; with no mark this block is never entered.

#: Bounds on the work per evaluation (the pass runs inside the optimiser loop).
GROUPING_MAX_ROUNDS = 4
GROUPING_MAX_JOINT_COMBINATIONS = 256


def programme_grouping_cost(programme: str, mark: str, own: bool) -> int:
    """How far one seat is from what the marks ask for (0 = as asked).

    ``programme`` is the student's, ``mark`` the section's ("" = any) and
    ``own`` whether the course has a section marked for the student's programme.
    Sitting in another programme's section costs 1, and so does sitting outside
    one's own programme's section when the course has one: 0 in one's own
    section, 1 in an unmarked one, 2 in another programme's; a student whose
    programme has no marked section prefers an unmarked one. When every section
    is marked for someone else the student is indifferent and gap decides.

    ``scheduler.seating.grouping_cost`` is the same table, kept separately
    because the scheduler stays isolated from this engine; a test pins the two.
    """
    return int(bool(mark) and mark != programme) + int(own and mark != programme)


def _seat_rule_allows(source: SectionState, dest: SectionState, matched: bool = False) -> bool:
    """A grouping move may take a seat in ``dest``, given it leaves ``source``.

    Never past capacity. A reserve seat only in two cases:

    * the student leaves a section that is itself over its regular limit — then
      reserve use is net zero;
    * ``matched`` — the student's programme is ``dest``'s mark (owner,
      2026-10-21: purity is wanted where it costs no seat) — and the student
      leaves a regular seat in another section of the SAME course. The course's
      free seats are unchanged: the spare seat only shifts from the marked
      section to the one the student left. Reserve use rises by one; the pass
      counts each such move (``_raises_reserve``) and the guard holds it to that.

    No risk-tier gate: the pass runs after ``_repair``, which seats every tier
    with reserves released, and the student already holds a seat in this course.
    """
    cur = dest.current_enrollment
    if cur >= dest.max_capacity:
        return False
    if cur < dest.max_capacity - dest.reserve_capacity:
        return True
    if source.current_enrollment > source.max_capacity - source.reserve_capacity:
        return True
    return matched and source.course_code == dest.course_code


def _raises_reserve(source: SectionState, dest: SectionState) -> bool:
    """Would one student moving ``source`` → ``dest`` raise reserve use (by one)?

    Yes exactly when the seat taken is past ``dest``'s regular limit and the seat
    left is within ``source``'s — the matched case of ``_seat_rule_allows``.
    """
    return (
        dest.current_enrollment >= dest.max_capacity - dest.reserve_capacity
        and source.current_enrollment <= source.max_capacity - source.reserve_capacity
    )


def _meeting_pairs(dms: list[tuple[tuple[int, int], ...]]) -> int:
    """Pairs of meetings that share a day and overlap — the objective's clash count.

    Exactly ``evaluate_assignability_lexicographic``'s per-student count: every
    meeting of every section (online included), a section overlapping itself too.
    """
    masks_by_day: dict[int, list[int]] = {}
    for dm in dms:
        for day, mask in dm:
            masks_by_day.setdefault(day, []).append(mask)
    pairs = 0
    for masks in masks_by_day.values():
        for i in range(len(masks) - 1):
            first = masks[i]
            for other in masks[i + 1 :]:
                if first & other:
                    pairs += 1
    return pairs


def _gap_after(seat: _Seat, drop: list[_SecInfo], add: list[_SecInfo]) -> int:
    """The student's total gap if ``drop`` were unseated and then ``add`` seated.

    Pure: works on copies of the day chains. Trying a move on the live ``_Seat``
    and undoing it could reorder meetings that start together, which changes
    ``_sorted_gap``. The order (drop all, then add in order) is exactly what
    ``_remove``/``_apply`` do when the move is taken, so the figure is exact.
    """
    days: set[int] = set()
    for info in (*drop, *add):
        days.update(day for day, _ms, _ordered, _alone in info.campus)
    total = seat.total
    for day in days:
        chain = list(seat.by_day.get(day) or ())
        for info in drop:
            for d, ms, _ordered, _alone in info.campus:
                if d == day:
                    for meeting in ms:
                        chain.remove(meeting)
        for info in add:
            for d, ms, _ordered, _alone in info.campus:
                if d == day:
                    chain.extend(ms)
        chain.sort(key=_START)
        total += _sorted_gap(chain) - seat.gap_by_day.get(day, 0)
    return total


class _Grouping:
    """One run of the grouping pass over a seated board (see the block comment)."""

    def __init__(
        self,
        states: dict[str, StudentAssignmentState],
        profiles: dict[str, StudentProfile],
        pairs_by_course: _CoursePairs,
        seats: dict[str, _Seat],
    ) -> None:
        self.states = states
        self.seats = seats
        self.infos = pairs_by_course.infos
        self.sections_by_id = self.infos.sections_by_id
        self.mark = {
            sid: str(sec.preferred_programme or "").strip().upper()
            for sid, sec in self.sections_by_id.items()
        }
        self.courses = sorted(
            {sec.course_code for sid, sec in self.sections_by_id.items() if self.mark[sid]}
        )
        self.pairs = {course: pairs_by_course[course] for course in self.courses}
        self.own = {
            course: {self.mark[s.section_id] for s, _info in self.pairs[course]} - {""}
            for course in self.courses
        }
        self.programme = {
            sid: str(profiles[sid].department or "").strip().upper() for sid in states
        }
        # The seater's own priority order: risk tier, intra-tier score, id.
        self.ordered = [
            p.student_id
            for p in sorted(
                (profiles[sid] for sid in states),
                key=lambda s: (s.risk_tier, s.intra_tier_score, s.student_id),
                reverse=True,
            )
        ]
        self.rank = {sid: i for i, sid in enumerate(self.ordered)}
        # Undo log: the ORIGINAL containers of everything touched, so a revert
        # restores the exact objects (and their iteration order).
        self.saved_states: dict[str, tuple] = {}
        self.saved_sections: dict[str, tuple[int, set[str]]] = {}
        self.pairs_before: dict[str, int] = {}
        # Each student's current clash-pair count, dropped when they move.
        self.pair_cache: dict[str, int] = {}
        self.moves = 0
        self.swaps = 0
        # Moves that took a marked reserve seat and left a regular one (each
        # raises reserve use by one); the guard holds reserve use to this.
        self.reserve_taken = 0

    def matched(self, sid: str, section: SectionState) -> bool:
        """Is ``section`` marked for this student's own programme?"""
        mark = self.mark[section.section_id]
        return bool(mark) and mark == self.programme[sid]

    def allows(self, sid: str, source: SectionState, dest: SectionState) -> bool:
        return _seat_rule_allows(source, dest, self.matched(sid, dest))

    # ── costs ──
    def cost(self, sid: str, course: str, section_id: str) -> int:
        programme = self.programme[sid]
        return programme_grouping_cost(
            programme, self.mark[section_id], bool(programme) and programme in self.own[course]
        )

    def total_cost(self) -> tuple[int, int, int]:
        """(grouping cost, seated pairs in marked courses, pairs with cost > 0)."""
        total = seated = outside = 0
        for sid, state in self.states.items():
            for course in self.courses:
                section_id = state.assigned_sections.get(course)
                if section_id is None:
                    continue
                c = self.cost(sid, course, section_id)
                total += c
                seated += 1
                outside += c > 0
        return total, seated, outside

    def student_pairs(self, section_ids) -> int:
        return _meeting_pairs([self.infos.seated(x).dm for x in section_ids])

    def current_pairs(self, sid: str) -> int:
        pairs = self.pair_cache.get(sid)
        if pairs is None:
            pairs = self.pair_cache[sid] = self.student_pairs(self.states[sid].section_ids)
        return pairs

    def clash_ok(self, sid: str, drop: list[SectionState], add: list[SectionState]) -> bool:
        """Would the student's clash-pair count stay the same or fall?

        The usual student has no clash at all: their meetings' time bits are then
        disjoint, so taking a section's bits out of the occupancy is exact and
        the question is whether the new sections fit the rest (and each other,
        and themselves) — a few mask tests. Anyone else is counted in full.
        """
        state = self.states[sid]
        current = self.current_pairs(sid)
        if current == 0:
            occ = dict(state.occupied_mask_by_day)
            for section in drop:
                for day, mask in self.infos.of(section).dm:
                    occ[day] &= ~mask
            for section in add:
                for day, mask in self.infos.of(section).dm:
                    if occ[day] & mask:
                        return False
                    occ[day] |= mask
            return True
        dropped = {s.section_id for s in drop}
        kept = [x for x in state.section_ids if x not in dropped]
        return self.student_pairs([*kept, *(s.section_id for s in add)]) <= current

    # ── mutation with an undo log ──
    def touch(self, sid: str, sections: list[SectionState]) -> None:
        state = self.states[sid]
        if sid not in self.saved_states:
            self.pairs_before[sid] = self.current_pairs(sid)
            self.saved_states[sid] = (
                state.assigned_sections,
                state.section_ids,
                state.occupied_mask_by_day,
                state.total_gap_minutes,
            )
            state.assigned_sections = dict(state.assigned_sections)
            state.section_ids = set(state.section_ids)
            state.occupied_mask_by_day = dict(state.occupied_mask_by_day)
        for section in sections:
            if section.section_id not in self.saved_sections:
                self.saved_sections[section.section_id] = (
                    section.current_enrollment,
                    section.enrolled_student_ids,
                )
                section.enrolled_student_ids = set(section.enrolled_student_ids)

    def move(self, sid: str, changes: list[tuple[SectionState, SectionState]]) -> None:
        """Re-section one student: drop every source, then seat every destination."""
        self.touch(sid, [s for pair in changes for s in pair])
        self.pair_cache.pop(sid, None)
        state, seat = self.states[sid], self.seats[sid]
        for source, _dest in changes:
            _remove(state, seat, source, self.infos)
        for _source, dest in changes:
            _apply(state, seat, dest, self.infos.of(dest))

    def revert(self) -> None:
        for sid, (assigned, section_ids, occ, gap) in self.saved_states.items():
            state = self.states[sid]
            state.assigned_sections = assigned
            state.section_ids = section_ids
            state.occupied_mask_by_day = occ
            state.total_gap_minutes = gap
        for section_id, (enrollment, enrolled) in self.saved_sections.items():
            section = self.sections_by_id[section_id]
            section.current_enrollment = enrollment
            section.enrolled_student_ids = enrolled

    # ── step A: move one student ──
    def best_move(self, sid: str) -> list[tuple[SectionState, SectionState]] | None:
        state = self.states[sid]
        held = [c for c in self.courses if c in state.assigned_sections]
        costs = {c: self.cost(sid, c, state.assigned_sections[c]) for c in held}
        current = sum(costs.values())
        if not current:
            return None
        seat = self.seats[sid]
        best_key = None
        best: list[tuple[SectionState, SectionState]] | None = None
        for course in held:
            if not costs[course]:
                continue
            source = self.sections_by_id[state.assigned_sections[course]]
            source_info = self.infos.of(source)
            for dest, dest_info in self.pairs[course]:
                if dest is source:
                    continue
                new_cost = current - costs[course] + self.cost(sid, course, dest.section_id)
                if new_cost >= current or not self.allows(sid, source, dest):
                    continue
                if not self.clash_ok(sid, [source], [dest]):
                    continue
                # A regular seat wins over a reserve one that groups as well.
                key = (
                    new_cost,
                    int(_raises_reserve(source, dest)),
                    _gap_after(seat, [source_info], [dest_info]),
                    dest.section_id,
                )
                if best_key is None or key < best_key:
                    best_key, best = key, [(source, dest)]
        if best is not None or len(held) < 2:
            return best
        # No single move helps: try the student's marked courses together (a
        # student's AI236 section can decide which IS124 section is clash-free).
        sources = [self.sections_by_id[state.assigned_sections[c]] for c in held]
        options = [[s for s, _info in self.pairs[c]] for c in held]
        for combo in islice(product(*options), GROUPING_MAX_JOINT_COMBINATIONS):
            changes = [(s, d) for s, d in zip(sources, combo, strict=True) if d is not s]
            if len(changes) < 2:
                continue  # single moves were all tried above
            new_cost = sum(
                self.cost(sid, c, d.section_id) for c, d in zip(held, combo, strict=True)
            )
            if new_cost >= current or not all(self.allows(sid, s, d) for s, d in changes):
                continue
            if not self.clash_ok(sid, [s for s, _d in changes], [d for _s, d in changes]):
                continue
            gap = _gap_after(
                seat,
                [self.infos.of(s) for s, _d in changes],
                [self.infos.of(d) for _s, d in changes],
            )
            raises = sum(_raises_reserve(s, d) for s, d in changes)
            key = (new_cost, raises, gap, tuple(d.section_id for d in combo))
            if best_key is None or key < best_key:
                best_key, best = key, changes
        return best

    def move_round(self) -> int:
        accepted = 0
        for sid in self.ordered:
            changes = self.best_move(sid)
            if changes:
                # Counted here, not in ``move``: a swap's two halves pass through
                # ``move`` too and leave every section's count as it was.
                self.reserve_taken += sum(_raises_reserve(s, d) for s, d in changes)
                self.move(sid, changes)
                self.moves += 1
                accepted += 1
        return accepted

    # ── step B: swap two students within one course ──
    def swap_side(self, holders: list[str], source: SectionState, dest: SectionState, bound: int):
        """Sorted (Δcost, Δgap, rank, sid) for moving each holder source→dest.

        Only holders whose Δcost is below ``bound`` can be in a swap that lowers
        the total, and only they are priced. A holder's figures read only their
        own sections, so they hold whoever the partner is.
        """
        course = source.course_code
        out = []
        for sid in holders:
            delta = self.cost(sid, course, dest.section_id) - self.cost(
                sid, course, source.section_id
            )
            if delta >= bound or not self.clash_ok(sid, [source], [dest]):
                continue
            seat = self.seats[sid]
            gap = _gap_after(seat, [self.infos.of(source)], [self.infos.of(dest)]) - seat.total
            out.append((delta, gap, self.rank[sid], sid))
        out.sort()
        return out

    def best_delta(self, holders: list[str], source: SectionState, dest: SectionState) -> int:
        course = source.course_code
        return min(
            (
                self.cost(sid, course, dest.section_id) - self.cost(sid, course, source.section_id)
                for sid in holders
            ),
            default=0,
        )

    def swap_round(self) -> int:
        accepted = 0
        for course in self.courses:
            sections = [s for s, _info in self.pairs[course]]
            if len(sections) < 2:
                continue
            holders: dict[str, list[str]] = {s.section_id: [] for s in sections}
            for sid in self.ordered:
                section_id = self.states[sid].assigned_sections.get(course)
                if section_id in holders:
                    holders[section_id].append(sid)
            for i, first in enumerate(sections):
                for second in sections[i + 1 :]:
                    there, back = holders[first.section_id], holders[second.section_id]
                    if not there or not back:
                        continue
                    up_best = self.best_delta(there, first, second)
                    down_best = self.best_delta(back, second, first)
                    if up_best + down_best >= 0:
                        continue
                    ups = self.swap_side(there, first, second, -down_best)
                    downs = self.swap_side(back, second, first, -up_best)
                    for up, down in zip(ups, downs, strict=False):
                        if up[0] + down[0] >= 0:
                            break
                        u, v = up[3], down[3]
                        self.move(u, [(first, second)])
                        self.move(v, [(second, first)])
                        holders[first.section_id].remove(u)
                        holders[second.section_id].remove(v)
                        holders[first.section_id].append(v)
                        holders[second.section_id].append(u)
                        self.swaps += 1
                        accepted += 1
                    holders[first.section_id].sort(key=self.rank.__getitem__)
                    holders[second.section_id].sort(key=self.rank.__getitem__)
        return accepted

    # ── guard ──
    def marked_sections(self) -> list[SectionState]:
        return [s for course in self.courses for s, _info in self.pairs[course]]

    def guard_failures(self, enrolment_before: dict[str, int], reserve_before: int) -> list[str]:
        failures = []
        sections = self.marked_sections()
        if any(s.current_enrollment > s.max_capacity for s in sections):
            failures.append("a section is over its capacity")
        for course in self.courses:
            after = sum(s.current_enrollment for s, _info in self.pairs[course])
            if after != enrolment_before[course]:
                failures.append(f"{course} enrolment changed")
        if sum(s.reserve_used() for s in sections) > reserve_before + self.reserve_taken:
            failures.append("reserve use rose past the marked seats taken")
        for sid, before in self.pairs_before.items():
            if self.student_pairs(self.states[sid].section_ids) > before:
                failures.append(f"student {sid} gained a clash")
        return failures

    def run(self, report: dict | None, reserve_shift: dict[str, int] | None = None) -> None:
        sections = self.marked_sections()
        enrolment_before = {
            course: sum(s.current_enrollment for s, _info in self.pairs[course])
            for course in self.courses
        }
        reserve_by_section = {s.section_id: s.reserve_used() for s in sections}
        reserve_before = sum(reserve_by_section.values())
        cost_before, seated, _outside = self.total_cost()
        for _round in range(GROUPING_MAX_ROUNDS):
            if not (self.move_round() + self.swap_round()):
                break
        failures = self.guard_failures(enrolment_before, reserve_before)
        if failures:
            # A safety net only: every step above already keeps these.
            logger.error("programme grouping reverted: %s", "; ".join(failures))
            self.revert()
        elif reserve_shift is not None:
            # What the pass did to each section's reserve use. Re-sectioning
            # within a course never changes its free seats, so it is not
            # capacity pressure: reserve readers subtract this (see
            # ``assign_students_to_sections``).
            for section in sections:
                shift = section.reserve_used() - reserve_by_section[section.section_id]
                if shift:
                    reserve_shift[section.section_id] = shift
        cost_after, _seated, outside = self.total_cost()
        if report is None:
            return
        gap_change = sum(
            self.states[sid].total_gap_minutes - saved[3]
            for sid, saved in self.saved_states.items()
        )
        regrouped = sum(
            1
            for sid, saved in self.saved_states.items()
            if self.states[sid].assigned_sections != saved[0]
        )
        report.update(
            {
                "marked_sections": sorted(
                    s.section_id for s in sections if self.mark[s.section_id]
                ),
                "marked_courses": list(self.courses),
                "seats_in_marked_courses": seated,
                "students_regrouped": 0 if failures else regrouped,
                "moves": 0 if failures else self.moves,
                "swaps": 0 if failures else self.swaps,
                "reserve_seats_taken": 0 if failures else self.reserve_taken,
                "grouping_cost_before": cost_before,
                "grouping_cost_after": cost_after,
                "seats_outside_preference": outside,
                "student_gap_minutes_change": 0 if failures else gap_change,
                "guard_reverted": bool(failures),
            }
        )


def _group_by_programme(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    pairs_by_course: _CoursePairs,
    seats: dict[str, _Seat],
    report: dict | None = None,
    reserve_shift: dict[str, int] | None = None,
) -> None:
    """Re-section seated students towards their programme's marked section."""
    _Grouping(states, profiles, pairs_by_course, seats).run(report, reserve_shift)


def assign_students_to_sections(
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
    sections_by_course: dict[str, list[SectionState]],
    course_rigidity: dict[str, float],
    course_tiers: dict[str, str] | None = None,
    grouping_report: dict | None = None,
    reserve_shift: dict[str, int] | None = None,
) -> tuple[dict[str, StudentAssignmentState], list[str]]:
    """Seat every student, then group marked sections by programme.

    ``grouping_report`` (optional) is filled by the grouping pass, and only when
    some section carries a "mainly for programme" mark; it stays empty otherwise.

    ``reserve_shift`` (optional, pass a fresh dict per seating) receives, for
    each section whose reserve use the grouping pass changed, ``after - before``.
    Grouping only re-sections students within a course, so a course's free seats
    never change: a marked section filling its reserve is not capacity pressure.
    Every reader of reserve use as a signal subtracts it — the objective
    (``evaluate_assignability_lexicographic``) and the reserve-heavy listing
    (``timetable_candidate_eval.extract_reserve_heavy_sections``) — so each
    section reads as it did before grouping. Sections keep their physical counts.
    """
    states = {sid: StudentAssignmentState(student_id=sid) for sid in profiles}
    pairs_by_course = _CoursePairs(sections_by_course, _InfoMap(sections_by_id))
    # Fresh states hold no sections, so every student starts with an empty chain.
    seats = {sid: _Seat() for sid in profiles}

    sorted_students = sorted(
        profiles.values(),
        key=lambda s: (s.risk_tier, s.intra_tier_score, s.student_id),
        reverse=True,
    )

    tier_ab = [p for p in sorted_students if p.risk_tier >= RiskTier.B]
    tier_c = [p for p in sorted_students if p.risk_tier == RiskTier.C]

    for student in tier_ab:
        _assign_courses(
            student,
            states[student.student_id],
            seats[student.student_id],
            pairs_by_course,
            course_rigidity,
            False,
            course_tiers,
        )

    for student in tier_c:
        _assign_courses(
            student,
            states[student.student_id],
            seats[student.student_id],
            pairs_by_course,
            course_rigidity,
            True,
            course_tiers,
        )

    _repair(states, profiles, pairs_by_course, seats, course_tiers)

    # O(sections) check: with no mark the pass is never entered (byte-identical).
    if any(getattr(sec, "preferred_programme", "") for sec in sections_by_id.values()):
        _group_by_programme(
            states, profiles, pairs_by_course, seats, grouping_report, reserve_shift
        )

    unresolved_ids = [sid for sid, st in states.items() if st.unresolved_courses]
    return states, unresolved_ids


def assign_courses_for_student(
    student: StudentProfile,
    state: StudentAssignmentState,
    sections_by_course: dict[str, list[SectionState]],
    sections_by_id: dict[str, SectionState],
    course_rigidity: dict[str, float],
    reserves_released: bool,
    course_tiers: dict[str, str] | None = None,
) -> None:
    infos = _InfoMap(sections_by_id)
    _assign_courses(
        student,
        state,
        _Seat.from_state(state, infos),
        _CoursePairs(sections_by_course, infos),
        course_rigidity,
        reserves_released,
        course_tiers,
    )


def rank_and_select_best_section(
    candidates: list[SectionState],
    state: StudentAssignmentState,
    allow_reserve: bool,
    sections_by_id: dict[str, SectionState],
) -> tuple[SectionState, tuple] | None:
    infos = _InfoMap(sections_by_id)
    seat = _Seat.from_state(state, infos)
    pairs = [(s, infos.of(s)) for s in candidates]
    picked = _pick_among(_seatable(pairs, allow_reserve), state.occupied_mask_by_day, seat)
    if picked is None:
        return None
    best, best_info = picked
    uses_reserve = 1 if best.current_enrollment >= best.regular_limit() else 0
    cap = max(1, best.max_capacity)
    fill_ratio_bp = int(((best.current_enrollment + 1) / cap) * 10000)
    score_tuple = (0, uses_reserve, seat.added_gap(best_info), fill_ratio_bp, best.section_id)
    return best, score_tuple


def apply_assignment(
    state: StudentAssignmentState,
    section: SectionState,
    sections_by_id: dict[str, SectionState],
) -> None:
    infos = _InfoMap(sections_by_id)
    seat = _Seat.from_state(state, infos)
    _apply(state, seat, section, infos.of(section))


def remove_assignment(
    state: StudentAssignmentState,
    section: SectionState,
    sections_by_id: dict[str, SectionState],
) -> None:
    infos = _InfoMap(sections_by_id)
    _remove(state, _Seat.from_state(state, infos), section, infos)


def meetings_clash(meetings_a: list[SectionMeeting], meetings_b: list[SectionMeeting]) -> bool:
    for a in meetings_a:
        for b in meetings_b:
            if a.day == b.day and (a.mask & b.mask):
                return True
    return False


def find_blocking_assigned_courses(
    state: StudentAssignmentState,
    candidate: SectionState,
    sections_by_id: dict[str, SectionState],
) -> list[str]:
    blockers: list[str] = []
    for assigned_sec_id in state.section_ids:
        assigned_sec = sections_by_id[assigned_sec_id]
        if meetings_clash(candidate.meetings, assigned_sec.meetings):
            blockers.append(assigned_sec.course_code)
    return blockers


def repair_unresolved_assignments_shallow(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    sections_by_course: dict[str, list[SectionState]],
    sections_by_id: dict[str, SectionState],
    course_tiers: dict[str, str] | None = None,
) -> None:
    infos = _InfoMap(sections_by_id)
    pairs_by_course = _CoursePairs(sections_by_course, infos)
    _repair(states, profiles, pairs_by_course, _Seats(states, infos), course_tiers)


def _campus_meetings(section: SectionState) -> list[SectionMeeting]:
    """The section's meetings that actually occupy a student's day ON CAMPUS.

    An ONLINE section is attended remotely, so it neither creates nor bridges a
    campus gap — its sessions must not appear in any gap chain. Counting them
    overstates ``gap_minutes`` for every student holding an online course, and
    worse, lets the optimiser "close" a gap by moving an in-person class next to
    an online one, which changes nothing for the student.

    ``is_online`` is resolved once when the states are built and is only ever set
    when ``TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED`` is on, so with the flag off
    this is the identity function and the legacy numbers are reproduced exactly.
    """
    return [] if section.is_online else section.meetings


def calculate_added_gap(
    state: StudentAssignmentState,
    candidate: SectionState,
    sections_by_id: dict[str, SectionState],
) -> int:
    # An online candidate adds no campus gap at all, and online sections already
    # seated must not anchor the chain either (both handled by ``_SecInfo``).
    infos = _InfoMap(sections_by_id)
    return _Seat.from_state(state, infos).added_gap(infos.of(candidate))


def _compute_day_gap(meetings: list[SectionMeeting]) -> int:
    if len(meetings) <= 1:
        return 0
    sorted_m = sorted(meetings, key=lambda x: x.start_min)
    gap = 0
    for i in range(len(sorted_m) - 1):
        gap += max(0, sorted_m[i + 1].start_min - sorted_m[i].end_min)
    return gap


def _compute_total_state_gap(
    state: StudentAssignmentState,
    sections_by_id: dict[str, SectionState],
) -> int:
    total = 0
    meetings_by_day: dict[int, list[SectionMeeting]] = defaultdict(list)
    for sec_id in state.section_ids:
        # Online sessions are attended remotely — they never form a campus gap.
        for m in _campus_meetings(sections_by_id[sec_id]):
            meetings_by_day[m.day].append(m)
    for day_meetings in meetings_by_day.values():
        total += _compute_day_gap(day_meetings)
    return total


def _compute_instructor_idle_minutes(
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]],
) -> int:
    """Total idle minutes between consecutive on-campus meetings, summed across
    every instructor and day.

    Mirrors the student day-gap metric (``_compute_total_state_gap``) but keyed
    by instructor: each instructor's meetings — gathered across all the sections
    they teach — are grouped by day and scored with ``_compute_day_gap``. Only
    sections present in ``section_instructor_ids`` contribute, so courses with no
    assigned instructor are naturally invisible. A section taught by N
    instructors counts its meetings toward each of those N instructors.

    "On-campus" is now enforced, not just documented: ONLINE sections are
    excluded, since teaching a session remotely does not strand the instructor
    on campus either side of it.
    """
    if not section_instructor_ids:
        return 0
    meetings_by_instructor_day: dict[tuple[int, int], list[SectionMeeting]] = defaultdict(list)
    for section_id, instructor_ids in section_instructor_ids.items():
        section = sections_by_id.get(section_id)
        if section is None:
            continue
        for instr_id in instructor_ids:
            for m in _campus_meetings(section):
                meetings_by_instructor_day[(instr_id, m.day)].append(m)
    total = 0
    for day_meetings in meetings_by_instructor_day.values():
        total += _compute_day_gap(day_meetings)
    return total


def diagnose_unresolved(
    candidates: list[SectionState],
    state: StudentAssignmentState,
    allow_reserve: bool,
) -> str:
    if not candidates:
        return "no_sections"
    clash_only = 0
    full_only = 0
    reserve_only = 0
    feasible = 0
    for c in candidates:
        clash = state.has_clash(c.meetings)
        can_regular = c.can_enroll(allow_reserve=False)
        can_with_policy = c.can_enroll(allow_reserve=allow_reserve)
        if (not clash) and can_with_policy:
            feasible += 1
            continue
        if clash and (not can_with_policy):
            continue
        if clash:
            clash_only += 1
        elif not can_with_policy:
            if can_regular:
                full_only += 1
            else:
                if not allow_reserve and c.can_enroll(allow_reserve=True):
                    reserve_only += 1
                else:
                    full_only += 1
    if feasible > 0:
        return "unknown"
    n = len(candidates)
    if clash_only == n:
        return "all_clash"
    if full_only == n:
        return "full"
    if reserve_only == n:
        return "reserve_only"
    return "mixed_blockers"


def _compute_same_course_section_spread(
    sections_by_id: dict[str, SectionState],
) -> int:
    """Penalty for scattering same-course sections across the week.

    Registrar rule: the same instructor typically teaches every section
    of a course, so (a) two sections at the same (day, slot) are
    instructor-clash and already hard-rejected upstream, and (b)
    sections *should* be consecutive on the same day so the instructor
    doesn't get a scattered schedule.

    The adjacency requirement is intentionally stronger than a normal
    student day-gap penalty:

    - two sections of a course should form one back-to-back pair
    - three or more sections should have at least one back-to-back pair

    Penalty per pair of sections of the same course, summed over every
    pair of sections of every multi-section course:

    - same day, back-to-back/consecutive slot    → 0
    - same day, gap 1-30 min                      → 30
    - same day, gap 31-120 min                    → 120
    - same day, gap > 120 min                     → gap_minutes
    - different days                              → 1000
    - same-day overlap                            → 10000

    A course that fails the required adjacent-pair rule receives an
    additional 5000-minute-equivalent penalty. A small passing-time gap
    up to 15 minutes counts as consecutive, matching the split-screen
    bundle recommender.

    Expressed in "minutes-equivalent" so the result can be folded into
    ``total_gap_minutes`` without changing the tuple shape.
    """
    by_course = defaultdict(list)
    for sec in sections_by_id.values():
        by_course[sec.course_code].append(
            [
                make_meeting_window(sec.course_code, m.day, m.start_min, m.end_min, sec.section_id)
                for m in sec.meetings
            ]
        )
    return same_course_section_spread_penalty(by_course)


# ── Objective tuple layouts ──────────────────────────────────────────────
# Legacy (tiered flag OFF): the canonical 6-tuple, or a 7-tuple when the
# instructor-gap flag adds a trailing idle term:
#   (tier_a, unres_students, unassigned_courses, clashes,
#    gap_minutes[+spread fold], reserve) [, instr_idle]
# Tiered (tiered flag ON): a fixed-length 9-tuple whose positions rank
# resolution by course tier. Real student gap-minutes sits *between* the tiers
# so the optimiser stops wrecking schedules for low-value (T3 / over-tolerance)
# enrolments. Smaller is better at every position, most-significant first.
TIERED_LEN = 9
TI_HIGHRISK = 0  # HARD: high-risk (RiskTier.A) students with an unresolved T1|T2 course
TI_CLASH = 1  # HARD: student double-booking (retained; ~always 0)
TI_T1 = 2  # HARD -> 0: unresolved (student, T1-course) pairs
TI_T2_OVER = 3  # near-hard: sum over T2 courses of max(0, unresolved - tolerance)
# Bounded student-cost: real_gap_minutes + soft_gap_budget * soft_unresolved.
# This is the position gaps and soft-tier enrolment TRADE against each other:
# the optimiser seats a soft course when it adds fewer than the budget in gap
# minutes. Pure real gap is recoverable as score[4] - budget * score[5].
TI_STUDENT_COST = 4
TI_SOFT = 5  # soft: T3 unresolved + T2 within-tolerance unresolved (pure count)
TI_RESERVE = 6  # reserve used
TI_SPREAD = 7  # same-course section spread, now its own quality term
TI_INSTR_IDLE = 8  # lowest priority; 0 unless the instructor-gap flag is on

# Legacy positions (flag OFF) for the shared accessors below.
_LEGACY_RESERVE = 5


def is_tiered_score(score: tuple[int, ...] | None) -> bool:
    """True when ``score`` is a tiered 9-tuple. Legacy is 6 or 7 — no collision.

    Tolerates ``None`` (returns False) so reporting-path accessors stay
    defensive, matching the guards the legacy decoders carried.
    """
    return score is not None and len(score) == TIERED_LEN


def reserve_used_of(score: tuple[int, ...] | None) -> int:
    """Reserve-used term, correct for either layout (idx 6 tiered / 5 legacy)."""
    if score is None:
        return 0
    if is_tiered_score(score):
        return int(score[TI_RESERVE])
    return int(score[_LEGACY_RESERVE]) if len(score) > _LEGACY_RESERVE else 0


def instructor_idle_of(score: tuple[int, ...] | None) -> int:
    """Instructor idle-minutes term: idx 8 tiered, trailing idx 6 legacy, else 0."""
    if score is None:
        return 0
    if is_tiered_score(score):
        return int(score[TI_INSTR_IDLE])
    return int(score[6]) if len(score) >= 7 else 0


def strip_instructor_idle(score: tuple[int, ...]) -> tuple[int, ...]:
    """Student + quality portion, dropping the trailing instructor-idle term.

    Lets an OFF-vs-ON comparison line up the substantive terms regardless of
    whether the instructor-gap term is present (8-slice tiered / 6-slice legacy).
    """
    return tuple(score[:8]) if is_tiered_score(score) else tuple(score[:6])


def decode_score(score: tuple[int, ...]) -> dict[str, int | str]:
    """Named view of an objective tuple, correct for either layout.

    A legacy (6/7) tuple returns exactly today's keys and values, so UI decoders
    stay byte-identical when the flag is off. A tiered (9) tuple returns the
    tier breakdown plus legacy-compatible aliases so existing keys still render.
    """
    if is_tiered_score(score):
        soft = int(score[TI_SOFT])
        student_cost = int(score[TI_STUDENT_COST])
        # Pure real student gap recovered from the blended student-cost term:
        # student_cost = real_gap + budget * soft  =>  real_gap = cost - budget*soft.
        real_gap = student_cost - get_tiered_soft_gap_budget() * soft
        return {
            "layout": "tiered",
            "highrisk_unresolved": int(score[TI_HIGHRISK]),
            "actual_assigned_clashes": int(score[TI_CLASH]),
            "t1_unresolved": int(score[TI_T1]),
            "t2_unresolved_over_tol": int(score[TI_T2_OVER]),
            "student_cost": student_cost,
            "gap_minutes": real_gap,
            "soft_unresolved": soft,
            "reserve_used": int(score[TI_RESERVE]),
            "same_course_spread": int(score[TI_SPREAD]),
            "instructor_idle": int(score[TI_INSTR_IDLE]),
            # legacy-compatible aliases so existing UI keys still render sensibly:
            "unresolved_tier_a": int(score[TI_HIGHRISK]),
            "unresolved_courses": int(score[TI_T1] + score[TI_T2_OVER] + soft),
        }
    return {
        "layout": "legacy",
        "unresolved_tier_a": int(score[0]),
        "blocked_students": int(score[1]),
        "unresolved_courses": int(score[2]),
        "actual_assigned_clashes": int(score[3]),
        "gap_minutes": int(score[4]),
        "reserve_used": int(score[5]),
    }


def student_gap_split(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
) -> dict[str, int]:
    """Real student gap-minutes split into regular vs visitor (spread excluded).

    ``gap_minutes_regular`` is what the objective's gap term counts when
    ``TIMETABLE_GAP_REGULAR_STUDENTS_ONLY`` is on; ``gap_minutes_all`` is every
    student. Reporting only: it never feeds the score.
    """
    regular = visitors = n_visitors = 0
    for sid, state in states.items():
        if profiles[sid].is_visitor:
            visitors += state.total_gap_minutes
            n_visitors += 1
        else:
            regular += state.total_gap_minutes
    return {
        "gap_minutes_regular": regular,
        "gap_minutes_visitors": visitors,
        "gap_minutes_all": regular + visitors,
        "visitor_students": n_visitors,
        "regular_students": len(states) - n_visitors,
    }


def evaluate_assignability_lexicographic(
    states: dict[str, StudentAssignmentState],
    profiles: dict[str, StudentProfile],
    sections_by_id: dict[str, SectionState],
    section_instructor_ids: dict[str, frozenset[int]] | None = None,
    course_tiers: dict[str, str] | None = None,
    reserve_shift: dict[str, int] | None = None,
) -> tuple[int, ...]:
    # ``reserve_shift``: the grouping pass's per-section change in reserve use on
    # THIS seating (see ``assign_students_to_sections``); it is subtracted, so
    # the reserve position reads exactly as before grouping.
    # The tiered objective engages only when a per-run tier map is supplied AND
    # the flag is on. A missing map (course_tiers is None) forces the legacy
    # path even under the flag, so any un-threaded caller stays safe.
    tiered = course_tiers is not None and is_tiered_objective_enabled()
    tolerance = get_tiered_t2_tolerance() if tiered else 0
    soft_gap_budget = get_tiered_soft_gap_budget() if tiered else 0
    # Optional: visitors (cross-term students) leave the gap term only. Every
    # other position (seating, clashes, T1/T2, high-risk, reserve) is untouched.
    regular_gap_only = is_gap_regular_students_only_enabled()

    # Legacy accumulators (unchanged names/values; drive the OFF path).
    unresolved_tier_a = 0
    total_unresolved_students = 0
    total_unassigned_courses = 0
    total_clashes = 0
    real_gap_minutes = 0  # was total_gap_minutes; spread is NOT folded in here
    total_reserve_used = 0

    # Tiered accumulators.
    hr_unresolved = 0
    t1_unresolved = 0
    t2_unresolved_by_course: dict[str, int] = defaultdict(int)
    t3_unresolved = 0

    for sid, state in states.items():
        profile = profiles[sid]
        if state.unresolved_courses:
            total_unresolved_students += 1
            total_unassigned_courses += len(state.unresolved_courses)
            if profile.risk_tier == RiskTier.A:
                unresolved_tier_a += 1
            if tiered:
                is_high = profile.risk_tier == RiskTier.A
                hr_hit = False
                for course in state.unresolved_courses:  # key == SectionState.course_code
                    tier = course_tiers.get(course, "T1")
                    if tier == "T1":
                        t1_unresolved += 1
                    elif tier == "T2":
                        t2_unresolved_by_course[course] += 1
                    else:
                        t3_unresolved += 1
                    if is_high and tier in ("T1", "T2"):
                        hr_hit = True
                if hr_hit:
                    hr_unresolved += 1
        if not (regular_gap_only and profile.is_visitor):
            real_gap_minutes += state.total_gap_minutes

        # Pairs of this student's meetings that share a day and overlap in time.
        masks_by_day: dict[int, list[int]] = {}
        for sec_id in state.section_ids:
            for m in sections_by_id[sec_id].meetings:
                day_masks = masks_by_day.get(m.day)
                if day_masks is None:
                    masks_by_day[m.day] = [m.mask]
                else:
                    day_masks.append(m.mask)
        for day_masks in masks_by_day.values():
            for i in range(len(day_masks) - 1):
                mask_i = day_masks[i]
                for mask_j in day_masks[i + 1 :]:
                    if mask_i & mask_j:
                        total_clashes += 1

    for section in sections_by_id.values():
        total_reserve_used += section.reserve_used()
    if reserve_shift:
        total_reserve_used -= sum(reserve_shift.values())

    # The same-course section-spread penalty (minutes-equivalent). Computed once
    # in every path — the legacy tuple folds it into gap-minutes; the tiered
    # tuple carries it as its own quality term so real student gaps are visible.
    spread = _compute_same_course_section_spread(sections_by_id)

    instr_idle = 0
    if section_instructor_ids is not None and is_instructor_gap_penalty_enabled():
        instr_idle = _compute_instructor_idle_minutes(sections_by_id, section_instructor_ids)

    if tiered:
        t2_over = 0
        t2_within = 0
        for unresolved in t2_unresolved_by_course.values():
            over = max(0, unresolved - tolerance)
            t2_over += over
            t2_within += unresolved - over  # == min(unresolved, tolerance)
        soft_unresolved = t3_unresolved + t2_within
        # Bounded trade: gaps and soft-tier enrolment compete on one axis. Each
        # unresolved soft seat is worth `soft_gap_budget` gap-minutes, so the
        # optimiser seats it iff doing so adds less than the budget in gaps.
        student_cost = real_gap_minutes + soft_gap_budget * soft_unresolved
        return (
            hr_unresolved,  # 0  A HARD
            total_clashes,  # 1    HARD
            t1_unresolved,  # 2  B HARD -> 0
            t2_over,  # 3  C near-hard
            student_cost,  # 4  D real_gap + budget * soft (bounded trade)
            soft_unresolved,  # 5  E soft count (tie-break; recovers real gap)
            total_reserve_used,  # 6  F
            spread,  # 7  F
            instr_idle,  # 8    lowest (0 unless instr-gap flag on)
        )

    # ── OFF path: byte-identical to pre-feature behaviour ──
    # real_gap_minutes + spread reproduces the old in-loop total_gap_minutes
    # plus the line-490 fold, exactly (deterministic integer add).
    total_gap_minutes = real_gap_minutes + spread
    base = (
        unresolved_tier_a,
        total_unresolved_students,
        total_unassigned_courses,
        total_clashes,
        total_gap_minutes,
        total_reserve_used,
    )
    # Append the instructor idle-gap term as a strictly-lowest-priority 7th
    # element — only when the flag is ON *and* a section→instructor map is
    # supplied. Otherwise the tuple stays the canonical 6-element shape.
    if section_instructor_ids is not None and is_instructor_gap_penalty_enabled():
        return (*base, instr_idle)
    return base
