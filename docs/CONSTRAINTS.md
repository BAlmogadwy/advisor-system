# Timetable Workspace — Constraint Registry

The single source of truth for every constraint the Workspace planner applies.
One row per constraint: its **type**, where its **source of truth** lives, the
**stages** that enforce it, the **flag** that gates it (if any), and the
**default**. Keep this in sync when adding or changing a planner constraint.

The planner is a matheuristic: greedy construction → portfolio ranking → local
search (LS-v2 / chain) → CP-SAT polish, with per-strategy variants (compact,
morning, balanced, optimal, hybrid, load-balanced, adaptive). "All stages"
below means: greedy `auto_place_board`, per-board CP-SAT `timetable_solver`,
global polisher `timetable_cpsat_polisher`, load-balanced `timetable_load_balanced`,
old SA `timetable_local_search`, and the catalog-driven LS-v2 / chain.

## Hard constraints (violations never accepted)

| Constraint | Type | Source of truth | Enforced in | Flag | Default |
|---|---|---|---|---|---|
| One meeting per day per section | hard | meeting-pattern enumeration | greedy option-gen; CP-SAT all-different-days; SA/load-balanced/polisher domains | — | always |
| Credit-hour meeting patterns (3cr→2×75, 4cr→2×75+100, 2cr→1×100, 1cr→1×75) | hard | `get_meeting_pattern` (`timetable_autoplace.py`) | all stages | — | always |
| Same-course sections never overlap (instructor double-booking) | hard | bitmask overlap | greedy hard-filter; CP-SAT/SA/load-balanced/polisher; **polisher also HARD vs fixed/locked siblings** | — | always |
| Real instructor clash (same named instructor, overlapping times) | hard | `TermSectionMeeting.instructor` | greedy scoring-loop filter | `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | true |
| **Blocked slots** (`scenario.blocked_slots` = `[{day,start}]`) — a true slot-domain exclusion | hard | `TimetableScenario.blocked_slots`; set via `blocked_slot_keys()` (`timetable_validation.py`) | **all stages, by construction** (greedy enumerator, CP-SAT slot build, SA/load-balanced `valid_options`, polisher slot build) | — | always (no-op when empty) |
| **Locks** (`SectionPlacement.is_locked`) — section freeze, NOT a slot ban | hard | `is_locked` rows on the board (read directly per stage) | **all stages + all persist paths**: locked sections never moved/relocated; preserved through the `tw_auto` delete/recreate; `respect_locked` rooming | `TIMETABLE_ENFORCE_LOCKS` | **true** (latent until rows are locked) |

Note on locks vs blocked slots: a **blocked slot** removes a `(day,start)` cell
from every section's domain. A **lock** freezes one section in place and seeds
its occupancy — two *different* courses may still legally share a `(day,start)`
in different rooms with disjoint students, so locks are enforced as
"freeze + seed", never as a blanket cell ban.

## Soft constraints (penalised, not forbidden)

| Constraint | Where | Notes |
|---|---|---|
| Cross-course student overlap | greedy 6-tuple (semi-hard, pos. 3), CP-SAT/polisher penalty | weighted by shared-student count |
| Idle-gap minutes between on-campus meetings | greedy gap term; CP-SAT gap penalty | extra penalty for ≥60-min gaps crossing the 13:00 midday boundary |
| Same-course back-to-back preference | `timetable_same_course.py` | miss = 5000; different-day = 1000; overlap = 10000 |
| Online-courses-late, time-consistency, day-spacing, slot-density | greedy 6-tuple + CP-SAT | scheduling-quality preferences |
| Quality policy v1 (`weak_slot`, `day_balance`, `spare_capacity`, `section_balance`, `room_change`, `student_day_overload`) | `timetable_quality.py` | ranks equal hard outcomes |

## Room feasibility (rooming pass, PR2 oracle)

Checked in order, most-specific reason wins (`timetable_room_oracle.py`): **type**
(labs need lab rooms; "is lab" = duration ≥ 80 on a 4-credit course) → **gender**
→ **capacity** → **capacity buffer** (`demand × TIMETABLE_CAPACITY_BUFFER`, default
1.1) → **occupancy**. Gated by `TIMETABLE_PR2_ROOM_ORACLE_ENABLED` (true). Soft
room-stability preference keeps a section in the same room across its meetings.

## Prayer — grid-compliant by construction, NO runtime rule

The planner uses **fixed slot grids**, and no slot's start time falls inside a
prayer window, so a per-meeting prayer rule can never fire. Rather than a
dormant runtime rule, compliance is guaranteed at the **source**:
`assert_slot_grid_prayer_compliant()` (`timetable_validation.py`) rejects any
grid that starts a **lecture in 11:30–12:59** or a **lab in 11:10–12:59**. The
default `DEFAULT_SLOTS` / `DEFAULT_LAB_SLOTS` pass by construction; the guard
only fires on a hand-edited non-compliant grid. (Soft idle-gap penalties around
the 13:00 midday boundary are scheduling-quality, unrelated to a prayer rule.)

## Pipeline acceptance gates (meta-constraints)

| Gate | Where | Guarantee |
|---|---|---|
| Evaluator gate (CP-SAT polisher) | `timetable_cpsat_polisher.py` | persists only on a strict lexicographic improvement confirmed by the real student-assignment evaluator |
| **SA evaluator gate** | `optimize_and_persist_board` (`timetable_local_search.py`) | SA rolls back any strict regression of the canonical student-assignment score vs the greedy/CP-SAT baseline — it can never persist a worse student outcome by chasing its private gap-cost |
| Persist-what-you-evaluated | `optimise_scenario_timetable_v2` step 5 | the evaluated winner's sections are persisted verbatim (atomic), so the saved board cannot drift from the reported `final_score` |
| Safety-regression rollback | V2 view | snapshots placements and rolls back a candidate that worsens the student outcome or hard operational constraints |
| Publish readiness | `check_publish_readiness` (`timetable_workspace.py`) | blocks publish on: no boards / empty board / critical conflicts (overlap, instructor) / room clashes / `UNASSIGNED` rooms / **placements on blocked slots** |

## Flags (config/settings.py)

| Flag | Default | Effect |
|---|---|---|
| `TIMETABLE_ENFORCE_LOCKS` | true | enforce registrar locks across all stages + persist paths |
| `TIMETABLE_CAPACITY_BUFFER` | 1.1 | room-sizing multiplier |
| `TIMETABLE_PR2_ROOM_ORACLE_ENABLED` | true | typed room-feasibility reasons |
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | true | real instructor-clash hard filter |
| `TIMETABLE_LAB_HEURISTIC_UNIFIED` | true | unified lab-room predicate |
| `TIMETABLE_PR3_WARM_START_ENABLED` / decision-trace / stage-telemetry / async (PR6/7/8) | true | observability + async shims |

Superseded: the flag-gated prayer-overlap runtime rule
(`TIMETABLE_ENFORCE_PRAYER_OVERLAP_RULE`, `TIMETABLE_PRAYER_WINDOWS`) is dormant
(absent from settings → off, no windows configured) and replaced by the
slot-grid compliance guard above. Its remaining helpers
(`prayer_overlap_rejection`, the `PRAYER_OVERLAP` sentinel) are slated for
deletion in a follow-up cleanup.
