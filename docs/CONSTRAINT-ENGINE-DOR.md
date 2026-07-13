# PR-2 — Constraint Engine: Definition of Ready

Second structural PR of the 2026-07 engine strengthening plan. Where PR-1
made the two board *representations* consistent, PR-2 makes the two-plus
*rule implementations* consistent: one interval-aware, delta-scoped predicate
per hard rule, consumed by every stage — greedy, local search, chain search,
CP-SAT, SA — instead of 5–7 divergent copies.

## Problem (verified against current master, 2026-07-13)

Each hard rule is re-implemented per stage, and the copies have drifted:

### Instructor clash — start-equality everywhere, but same-course is interval-aware
- `timetable_pr4_instructor.has_instructor_clash` keys on `(iid, day,
  **start_min**)` (exact start), and `count_instructor_clashes` likewise.
- The greedy filter (`timetable_autoplace.py:~1582`), CP-SAT
  (`timetable_cpsat_polisher.py:~344`), and SA (`timetable_local_search.py:~257`)
  all key on start-minute too.
- Meanwhile same-course overlap already uses **true interval overlap**
  (`timetable_same_course.meeting_gap_or_overlap`: `left.start < right.end and
  right.start < left.end`).

Consequence: an instructor teaching a 10:30–11:45 lecture and a 10:45–12:25 lab
(different `start_min`, overlapping intervals — the lecture/lab grids interleave
by design) is **genuinely double-booked but invisible to every clash gate**.
The 10:50 lecture slot (shipped in the same-course merge) makes this reachable
for lectures too (10:30–11:45 vs 10:50–12:05). PR-1's repair passes are
interval-aware; the *gates* are not.

### Whole-board absolute gates cause paralysis
`timetable_local_search_v2` / `timetable_local_search_chains` call
`has_instructor_clash(sections_by_id, …)` / `_has_same_course_overlap(…)` over
the **entire board** after each trial move. If the incoming board already holds
one unrelated violation, *every* move that doesn't clear it is rejected — the
stages silently no-op on exactly the boards that most need optimising (Optimise
Current ingests hand-edited boards).

### Identity-source drift
The greedy resolves a section's instructor as links ∪ free-text; the V2/SA
gates use `CourseInstructor` links only and ignore the LINKS flag — so a
free-text-only section is clash-protected during greedy placement but invisible
to the optimise stages.

## The engine

New pure module `core/services/timetable_constraints.py` — no DB, no Django
models; operates on the in-memory `SectionState`/meeting windows the stages
already hold. For each hard rule, two forms:

1. **Whole-board**: `clashes(board) -> list[Violation]` / `count`.
2. **Delta**: `move_introduces_clash(board, moved_section_ids) -> bool` — checks
   only the moved section(s) against the rest, so a pre-existing unrelated
   violation never blocks an unrelated improving move (kills the paralysis).

All predicates are **interval-aware** (reuse the `meeting_gap_or_overlap`
overlap test that same-course already trusts). One instructor-identity resolver
(links ∪ free-text, honouring the LINKS flag) shared by every stage.

Rules, in the order they'll be migrated:
- **instructor clash** (interval; the headline correctness fix)
- **instructor daily cap** (already interval-agnostic — count per day — but
  unify the identity source + delta form)
- **same-course overlap** (already interval; move it behind the engine so there
  is one home)
- **room feasibility** and **prayer/grid legality** (later slices)

## Staged delivery (each slice independently shippable + reviewable)

- **PR-2a — instructor clash → interval + unified + delta.** Extract the engine
  with the interval clash predicate; migrate `has_instructor_clash` /
  `count_instructor_clashes` and the greedy/LS/chain/CP-SAT/SA gates onto it;
  add the delta form to LS + chain so they stop whole-board-paralysing. This is
  the highest-value slice (a real correctness fix) and the seed of the engine.
- **PR-2b — instructor daily cap** onto the engine (identity + delta).
- **PR-2c — same-course** consolidation onto the engine (behaviour-preserving).
- **PR-2d — room feasibility + prayer/grid** predicates.

Behaviour-change note: PR-2a makes clash detection stricter (catches interval
overlaps it currently misses). Clash enforcement is already default-ON
(`TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=true`), so this changes production
optimiser output (more clash-creating moves correctly rejected). Guarded by the
existing flag → flag-off parity preserved; the stricter behaviour is the fix.

## PR-2a scope (the first slice — this PR)

**In:**
1. `timetable_constraints.py`: `instructor_clash_windows(...)`,
   `has_instructor_clash(board, section_instructor_ids)` (interval),
   `count_instructor_clashes(...)`, `move_introduces_instructor_clash(board,
   moved_ids, ...)`, and one `resolve_section_instructors(...)` identity helper.
2. Repoint `timetable_pr4_instructor.has_instructor_clash` /
   `count_instructor_clashes` to the engine (keep the names as thin
   re-exports for their existing callers/tests).
3. Migrate the greedy candidate filter, LS-v2 + chain gates (delta form),
   CP-SAT hard constraint, and SA hard check to the engine's interval predicate.
4. Unify the instructor-identity source across those sites.

**Out (later slices):** daily cap, same-course, room, prayer — PR-2b–d.

## Test plan (TDD)

`tests/test_constraints_instructor_clash.py`:
1. Interval overlap across interleaved grids is a clash (10:30–11:45 vs
   10:45–12:25; 10:30–11:45 vs 10:50–12:05) — the current start-equality misses.
2. Non-overlapping same-day sessions are NOT a clash (13:00 vs 14:30).
3. Different instructors sharing a slot are NOT a clash.
4. Delta form: a board with a pre-existing clash still accepts a move that
   doesn't touch the clashing sections (no paralysis), and rejects a move that
   *creates* a new clash.
5. Identity: a free-text-only section is clash-checked (parity with greedy).
6. Regression: existing `test_instructor_clash.py` stays green (semantics only
   widen from start-equality to interval; add fixtures that were false-negatives).

Gate: full suite green, ruff + bandit clean (`SKIP=mypy`), `manage.py check`.

## Rollback

Additive module + call-site redirects, behind the existing clash flag. Revert =
`git revert`. No migration, no schema change.
