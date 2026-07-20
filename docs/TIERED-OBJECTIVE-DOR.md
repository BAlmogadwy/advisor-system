# PR-3 — Tiered Lexicographic Objective: Definition of Ready

> **Status: MERGED.** Objective — PR #40 (merge `6299ea8`). Tier-aware seating —
> PR #41 (merge `0940576`). Flag `TIMETABLE_TIERED_OBJECTIVE_ENABLED` is still
> **default ON as of 2026-07-16** (`=false` is the kill-switch). See
> [§ Tier-aware seating](#tier-aware-seating-pr-41) for the second half of the
> policy and the two traps it documents.

Third structural PR of the 2026-07 engine-strengthening plan. PR-1 made the two
board *representations* consistent; PR-2 made the hard-*rule* implementations
consistent. PR-3 fixes the *objective*: what the optimiser is trying to
maximise. The strict "enrol every student at any cost" policy inflated student
gaps by up to +233% to seat the last few low-value enrolments. This replaces the
flat objective with one that ranks resolution by **course tier**, keyed off the
real programme curriculum.

## Problem (verified against current master, 2026-07-14)

`evaluate_assignability_lexicographic` (`timetable_student_assignment.py`)
returns a 6-tuple `(tier_a_unresolved, unresolved_students, unassigned_courses,
clashes, gap_minutes, reserve)` where every "unresolved" position is flat: a
Tier-A student's specialised major course and a general-education elective a
student can take in any of a dozen other sections count exactly the same, and
both dominate `gap_minutes` lexicographically. So the optimiser will wreck many
students' schedules to seat one more student in *any* course.

Two secondary defects compound it:

- **Position 4 is overloaded.** `gap_minutes` folds real student idle minutes
  together with the same-course *spread* pseudo-penalty (1000/5000/10000-unit
  magnitudes from `timetable_same_course.py`). The "+233% gap" figure is partly
  this fold, not real student harm — but nothing could tell them apart.
- The instructor-idle 7th position is a proven no-op for real compaction.

## Policy (product owner, 2026-07-14)

Resolution priority depends on the course, and the course's tier is derived from
the curriculum, not hard-coded:

- **Tier 1 — specialised major courses.** A course required by its own major's
  plan and shared by **≤ 2** programme plans. Enrolment is hard; never traded.
- **Tier 2 — shared foundations.** `MATH`/`STAT` by prefix, plus any course
  required by **> 2** plans (a "service course" signal — e.g. CS111/CS112 intro
  programming, shared by 11–12 plans). Up to `tolerance` (default 3) unresolved
  seats **per course** are acceptable; the excess is near-hard.
- **Tier 3 — general education & free electives.** `ENGL`/`GS`/`GSE`/`FE`. Soft:
  a student can take these in another section university-wide, so they are
  resolved only when it costs no schedule quality.

Empirically (scenarios 632–639) this yields **194 T1 / 37 T2 / 15 T3** courses;
the raw-plan-count vs major-family-collapsed threshold only moves one course
(PHYS104), so the rule is robust. Two courses (`AI463`, `DS487`) have no
`ProgrammeRequirement` rows → default **T1** (least-shared).

## Design

**Flag** `TIMETABLE_TIERED_OBJECTIVE_ENABLED` (**default ON** as of 2026-07-16;
`=false` ⇒ byte-identical to pre-feature). `TIMETABLE_TIERED_T2_TOLERANCE` (default 3, floored at 0).
`TIMETABLE_TIERED_SOFT_GAP_BUDGET` (default 120, floored at 0). Readers in
`timetable_flags.py`: `is_tiered_objective_enabled`, `get_tiered_t2_tolerance`,
`get_tiered_soft_gap_budget`.

**Tuple layouts.** The objective is produced in ONE place and consumed
positionally in ~8. When OFF, the producer returns the original 6-tuple (or 7
under the instructor-gap flag), verbatim. When ON *and* a per-run `course_tiers`
map is threaded, it returns a fixed 9-tuple (smaller = better, most-significant
first):

| idx | term | rank |
|-----|------|------|
| 0 | high-risk unresolved (RiskTier.A student with an unresolved T1\|T2 course) | HARD |
| 1 | student double-booking clashes (retained; ~always 0) | HARD |
| 2 | Tier-1 unresolved (student, course) pairs | HARD → 0 |
| 3 | Tier-2 over tolerance: Σ over T2 courses of max(0, unresolved − tolerance) | near-hard |
| 4 | **bounded student cost** = `real_gap_minutes + budget × soft_unresolved` | quality ⇄ soft |
| 5 | soft count: Tier-3 unresolved + Tier-2 within-tolerance (tie-break; recovers real gap) | soft |
| 6 | reserve used | tie-break |
| 7 | same-course section spread (its own quality term) | tie-break |
| 8 | instructor idle-minutes (0 unless the instructor-gap flag is on) | lowest |

`course_tiers=None` forces the legacy path even when the flag is on, so any
un-threaded caller is safe (it just scores the legacy objective).

**Bounded trade (position 4).** Strict quality-first (gap ranked above soft)
over-drops gen-ed: on an already-clean board (scn 625) it shed 23 gen-ed seats
for a 1% gap gain. So gaps and soft-tier enrolment share *one* position, blended:
`student_cost = real_gap_minutes + budget × soft_unresolved`. Because the
optimiser compares score *deltas*, seating one more soft course (soft −1, saving
`budget`) against the gap it introduces (ΔG) is accepted iff `ΔG < budget` — a
bounded per-student trade, independent of absolute magnitudes. `budget = 0`
recovers strict quality-first; large budget approaches "gen-ed above gaps". The
pure real gap stays reportable: `real_gap = score[4] − budget × score[5]`, which
`decode_score` recovers. Shadow-tune `budget` via
`tiered_objective_report --soft-budget N`.

**Byte-parity proof (OFF path).** `real_gap_minutes + spread` reproduces the old
in-loop `total_gap_minutes` plus the line-490 fold exactly (deterministic
integer add); `_compute_same_course_section_spread` still runs once; the `base`
tuple + optional idle append are the old lines. Verified on scenario 632: the
score is identical with `course_tiers` supplied or `None`, and the unbundling
identity `legacy_gap == tiered_gap + tiered_spread` (82120 == 79690 + 2430)
holds. Full suite: **943 passed / 2 skipped** with the flag at its OFF default.

## Classifier + per-run map

`timetable_course_tier.py` — `classify_course_tier(bare_code, count, default)`
is pure (prefix rules beat the count rule); `program_count_by_code()` is an
`lru_cache(maxsize=1)` global read of `ProgrammeRequirement`, invalidated by a
`post_save`/`post_delete` signal on that model (`apps.py`). `bulk_create` (the
bootstrap migration) bypasses the signal, but the process restarts post-migrate,
so the cache is fresh. `build_course_tier_map_for_scenario(scenario_id)`
(`timetable_optimizer_v2.py`) builds a `{SectionState.course_code: tier}` dict
once per run, keyed by the planner `course_key or course_code` identity the
evaluator sees, so the hot loop is a single `.get(key, "T1")` — no DB, no string
work per eval.

## Threading

Built once per run at each optimise entrypoint and threaded (same object) to
every evaluator reachable during a live ON run: V2 rank / local-search / chain /
CP-SAT; the SA gate (`_sa_scenario_score`, so the adaptive Full Rebuild scores
the tiered objective, not the legacy one); the cap + clash repair passes;
instructor compaction (whose `gates_ok` is now layout-aware — it guards the hard
prefix + reserve via `reserve_used_of` + soft-unresolved so compaction can't
strand a soft-tier student); and the interactive move-preview.

Positional consumers made layout-aware via shared accessors
(`is_tiered_score`, `reserve_used_of`, `instructor_idle_of`,
`strip_instructor_idle`, `decode_score`): `summarise_evaluation`, compaction
report, `_gap_pos6`, `instructor_gap_report`, `timetable_student_blockers`, the
`timetable_v2_runner` safety gate + rollback, and the two workspace JS decoders
(`decodeScore` — legacy branch byte-identical, tiered branch tier-labelled).

**Safety-gate decision.** `timetable_v2_runner.optimiser_student_outcome_regression`
guards the hard block at positions 0-3 in both layouts (correct labels chosen by
`is_tiered_score`); under the tiered layout the **soft tier (position 5) is
deliberately NOT gated** — the policy deprioritises T3 / Tier-2-within-tolerance
enrolments, so trading them for T1/T2/gap gains must not trigger a rollback. The
tiered tuple carries no total-unresolved-students headcount (its position 1 is
the clash count), so the rollback restores `unresolved_students` from a
`baseline_unresolved_students` value captured at optimise time, and
`summarise_evaluation` derives `blocked_students` from the eval's own
`unresolved_student_ids` — never from a tuple position.

## Tests

`tests/test_course_tier.py` (classifier purity, prefix-beats-count, >2 boundary,
orphan default, DB count map, signal + manual cache invalidation, map builder
keyed by course identity) and `tests/test_tiered_objective.py` (flag default
off, **flag-off byte parity with and without the map**, 9-tuple shape, tier
decomposition + tolerance knife-edge, high-risk T1/T2 override vs T3-only
exclusion, spread unbundling, the **bounded-trade blend** — student_cost =
gap + budget×soft, cheap gen-ed seated / expensive dropped, budget=0 ⇒ strict
quality-first — layout-aware accessors, and the layout-aware v2 safety gate).
Discriminating regressions per tier boundary (T1 dominates 50×T3; tolerance
crossover flips soft→near-hard).

## Validation / rollout

`python manage.py tiered_objective_report [scn...] [--fast]` runs the full
pipeline OFF vs ON inside a **rolled-back transaction** (read-only, no persist),
re-scores each board through the tiered lens, and reports T1/T2/high-risk
unresolved, real gap, spread, and students-moved deltas. Promote to default-ON
only after the report shows ON drives T1 unresolved → 0 and high-risk ≤ OFF at
every scenario, with gap/churn deltas the product owner signs off on. `=false`
env override is the live kill-switch.

## Tier-aware seating (PR #41)

The objective above **scores** by tier, but the assignment that *produces* those
outcomes was tier-blind — so only half the policy shipped in PR #40.
`assign_courses_for_student` ordered a student's courses purely by scarcity
(`feasible_count, -rigidity, course_code`), letting a scarce **Tier-2** service
course claim the slot a **Tier-1 core** course needed.

**How it surfaced.** On a live IS,IS2 build (scn 642), adding a 3rd STAT305
section fixed the Tier-2 shortfall (20 → 1 unresolved) but pushed core
unresolved **3 → 5/6/9** at every candidate slot. *Adding capacity making core
worse* is the diagnostic signature of tier-blind seating.

**The fix.** Lead the sort key with the tier rank (`T1=0 < T2=1 < T3=2`), keeping
scarcity as the within-tier tiebreak; repair unresolved courses core-first.
Measured: core unresolved **3 → 1** from the ordering alone, and adding the
STAT305 section then cut Tier-2 over-tolerance **53 → 31** at **zero** core cost.

### Invariant: reconstructions must thread the tier map

`course_tiers` is now load-bearing on the **assignment**, not just the score.
Before PR #41 a caller could omit it safely because `assignment_states` were
identical either way. That is no longer true — **any caller that reconstructs
seating must thread the same map or it models a board that was never built.**
Threaded in `timetable_plan_lens._section_assignment_evidence` (drives plan
owner/`filter_plans`, which scope plan-filtered repairs and per-plan export),
`timetable_repair._actual_unresolved_students_by_course`, and the
`timetable_repair_domain` baseline.

### Two traps — do not re-enter

1. **Do not add a course-code tiebreak to the repair tier-sort.**
   `state.unresolved_courses` is insertion-ordered by the scarcity key, so
   `sort(key=(tier_rank, course_code))` silently replaces the deliberate
   hardest-first ordering with alphabetical. Key on the tier alone and rely on
   Python's stable sort.
2. **Do not add a tier guard to the repair eviction swap.** The swap only
   commits when the displaced course finds an alternative section (else it rolls
   back), so a higher-tier course never loses its seat — it merely *moves*.
   Guarding it forfeits real unresolved seats to protect a section choice:
   measured on scn 642 it pushed Tier-2 over-tolerance **53 → 73** and soft
   **59 → 93** for **zero** core gain. The reasoning is exact for T1 (position 2)
   and T2-over (position 3), which outrank the gap-minutes (position 4) a
   relocation costs; a *soft* seat sits at position 5 — **below** gap — so soft
   swaps are only worth it inside the soft-gap budget (measured ~116 gap-min per
   soft seat against a 120 budget, i.e. break-even). A budget-aware gate there is
   a possible future refinement.

## Out of scope

A distinct graduating-senior signal (currently RiskTier.A is the high-risk
proxy — a profile-construction change if a separate flag is wanted). Threading
the tiered objective into the separate Timetable *Repair* subsystem
(`timetable_repair*.py`) — it evaluates legacy tuples among itself and never
compares against a threaded tiered tuple.
