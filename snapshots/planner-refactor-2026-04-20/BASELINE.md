# Planner Refactor Baseline — 2026-04-20

Frozen copy of files likely to change during the upcoming planner refactor
discussed on 2026-04-20 (ChatGPT consultation, 3 rounds).

## Baseline commit

`47549c86a1feff33239363e99886f4af62a5e2b8` — matrix layout picker for
split-pane timetable workspace.

Git tag: `baseline/pre-planner-refactor-2026-04-20`

## Why this snapshot exists

The discussion converged on a 3-tier objective architecture (feasibility
gate → perturbation / lock movement → weighted soft sum) and a 10-step
refactor plan. Before touching the planner we want:

1. A diffable filesystem copy (no git tools needed to see what changed)
2. A git tag pointing at the exact commit
3. An explicit list of the files considered in scope

## Files in scope

### Planner core (`core/services/`)
- `timetable_autoplace.py` — main greedy planner (prayer fix, lock semantics, ordering)
- `timetable_rooming.py` — room oracle / assignment
- `timetable_solver.py` — CP-SAT feasibility solver
- `timetable_cpsat_polisher.py` — CP-SAT polish (Stage 4 neighbourhood repair)
- `timetable_local_search.py`, `_v2`, `_chains` — SA / LNS
- `timetable_optimizer_v2.py` — V2 optimiser pipeline
- `timetable_pattern_catalog.py` — meeting patterns
- `timetable_room_repair.py` — room repair pass
- `timetable_generate.py` — generation
- `timetable_candidate_eval.py` — scoring
- `timetable_overlap.py` — overlap checks (prayer)
- `timetable_pair_feasibility.py` — feasibility
- `timetable_load_balanced.py` — balancing
- `timetable_demand.py`, `timetable_export.py`, `timetable_workspace.py`,
  `timetable_assignment_models.py`, `timetable_student_assignment.py` — supporting

### Models / views / UI
- `core/models.py` — `SectionPlacement.is_locked`, may add audit tables
- `core/timetable_workspace_views.py` — JSON endpoints called from UI
- `core/templates/core/timetable_workspace_split.html` — split-pane workspace
- `static/js/page-timetable-workspace-split.js` — workspace JS

## Planned changes (from ChatGPT consultation)

Priority order:
1. Fix prayer-overlap check (span, not just start)
2. Implement real lock semantics (greedy must read `is_locked`)
3. Promote same-course overlap to correct tier
4. Replace `is_lab == (credits == 4)` with explicit `pattern_kind`
5. Introduce staged room oracle (Tier 0 / 1 / 2 / 3 cascade)
6. Dynamic hardness-first ordering (drop round-robin primary)
7. Warm-start / minimal-perturbation replanning
8. Diagnostics + decision traces
9. Retune weights via one-factor sweeps
10. CP-SAT neighbourhood repair as Stage 4

## Restore

The file-tree mirror that used to live under
`snapshots/planner-refactor-2026-04-20/core/` and `/static/` was
removed in PR9 (debt consolidation) — it was never tracked and had
drifted out of sync with the baseline branch.

To revert any single file, use the baseline branch directly:
```bash
git checkout baseline/pre-planner-refactor-2026-04-20 -- core/services/timetable_autoplace.py
```

To revert everything:
```bash
git checkout baseline/pre-planner-refactor-2026-04-20 -- core/ static/
```
