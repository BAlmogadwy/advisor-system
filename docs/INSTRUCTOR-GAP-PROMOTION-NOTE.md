# Instructor Idle-Gap Penalty — Promotion Note

**Branch:** `feat/instructor-gap-minimisation`
**Theme:** a soft objective that minimises idle time gaps in each instructor's
daily schedule, strictly subordinate to every student outcome.

## What shipped

When `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED` is ON, the canonical candidate
evaluator (`evaluate_assignability_lexicographic`) appends **position 6** to the
`lexicographic_score` tuple = total instructor idle minutes (sum over each
instructor of the per-day gaps between their consecutive on-campus classes).
Because it is the lowest-priority element, it can never override positions 0–5
(student feasibility, student gaps, reserve) — instructor compaction is pursued
only on student-neutral moves and otherwise acts as a tie-break/ratchet.

- **One objective, every stage:** the term lives in the single evaluator, so
  portfolio ranking, LS-v2, chains, and the CP-SAT polisher's accept-gate all
  honour it automatically. The greedy adds a construction-time term (weight 1×,
  folded into its soft bucket) so initial boards are already compact.
- **Partial assignment:** only sections with a resolved `CourseInstructor`
  contribute; unassigned courses are invisible to the metric.
- **Data path:** `build_section_instructor_map_for_scenario` builds
  `{section_id: frozenset[instructor_id]}` once per run, keyed by the same
  `section_id` the evaluator uses, and is threaded to every evaluator call.

## Feature flag

| Flag | Default | Env kill-switch |
|---|---|---|
| `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED` | **false** | set `=false` → score tuple returns to the canonical 6-element shape, output byte-identical to pre-feature. Meaningful only with `TIMETABLE_INSTRUCTOR_LINKS_ENABLED=true`. |

## Behavioural changes (flag-on)

- **New score element:** `lexicographic_score` becomes a 7-tuple. Within any one
  run every stage is uniformly 7 elements (the map reaches every evaluator call);
  with the flag off every stage stays 6.
- **New payload key:** `instructor_gap_metric`
  `{idle_minutes_before, idle_minutes_after, idle_delta, affected_instructors}`,
  schema-stable zeros when off.
- **Rank tie-break (intentional):** in `rank_timetable_candidates` the sort key is
  `(lexicographic_score, quality_penalty, candidate_id)`. With a 7-tuple,
  instructor idle minutes now break ties among student-equal candidates *before*
  `quality_penalty` is consulted. This is deliberate — instructor gap is a real
  objective, quality is a softer secondary.
- **Pre-existing bug fixed:** the greedy passed `build_section_instructor_ids(scenario.id)`
  (an int) where the function reads `scenario.gender`/`.programs`; it now passes
  the object, so the links-keyed instructor clash actually engages.

## Rollback (tiered, cheapest first)

1. Env: `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED=false` on Render → restart. No redeploy.
2. Revert the promotion commit (keeps infra, flips default off).
3. Revert the merge.

## Acceptance

- `tests/test_instructor_gap_metric.py` — metric + evaluator position-6 + parity.
- `tests/test_instructor_gap_map.py` — map keys line up with `section_id`s.
- `tests/test_instructor_gap_pipeline.py` — uniform tuple length ON/OFF + end-to-end
  `instructor_gap_metric` (165-min gap reflected, zeroed when off).
- `tests/test_instructor_gap_greedy.py` — greedy gap-delta helpers.
- Flag-OFF byte parity guarded by `test_timetable_evaluation_layer.py` (`== 6-tuple`)
  and the pr3/pr5/pr6 acceptance packs (still green).
- Ops: `python manage.py instructor_gap_report <scenario_id>` — idle reduction with
  student score held constant (errors if students regress).

## Deferred (fast-follow)

- **CP-SAT internal span term** — per-(instructor,day) `first_start`/`last_end`
  variables minimising `Σ span`, for active compaction *during* polish. Not in
  this PR; the polisher's accept-gate already prevents instructor-gap regressions.
- Teaching-day-count minimisation was subsequently implemented in the separate,
  default-off `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` post-pass. It minimises
  maximum/total excess days above each instructor's cap/H2 lower bound before
  physical-campus span/idle; online teaching counts as work but not campus idle.

## Subsequent compaction verification

The post-pass now performs scenario-wide H9 and H15 persistence checks with an
output-sensitive interval sweep. Randomized tests match the quadratic reference
pair-for-pair, while a 10,000-row non-overlap case examines zero candidate pairs
and keeps a maximum active set of one. The complete project regression suite is
1,037 passed and 2 skipped with the feature still default-off.
