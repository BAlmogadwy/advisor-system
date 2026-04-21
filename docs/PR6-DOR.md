# PR6 — DEFINITION OF READY

## Theme

**Stage telemetry.**
Add timing and iteration visibility for the planner pipeline without changing placements, scores, rooming, or trace semantics.

PR5 answered:

- what stage last changed a section

PR6 answers:

- how long each stage took
- how much work each stage did

This is an **observability-only** PR.

---

## In scope

### 1. Stage telemetry payload

Add a new payload block under the existing planner result:

- `stage_telemetry.stage_ms`
- `stage_telemetry.stage_iterations`

Shape:

- `stage_ms = { greedy, sa, cpsat, chain, rooming_repair }`
- `stage_iterations = { greedy, sa, cpsat, chain, rooming_repair }`

Conventions:

- keys always present
- value `0` when stage did not run
- numeric types only
- milliseconds as integers
- iteration/work counts as integers

This is a new top-level sibling payload field, **not** mixed into `decision_trace`.

### 2. Standalone helper module

New module:

- `core/services/timetable_stage_telemetry.py`

Responsibilities:

- `empty_stage_telemetry()`
- `merge_stage_telemetry(...)`
- `record_stage_ms(...)`
- `record_stage_iterations(...)`

No planner logic in this module. Only telemetry shaping and aggregation.

### 3. Stage instrumentation

Instrument these stages only:

- `greedy`
- `sa`
- `cpsat`
- `chain`
- `rooming_repair`

Definitions:

- `greedy.ms` = wall time spent in initial board/scenario placement path
- `sa.ms` = wall time spent inside SA/local-search pass
- `cpsat.ms` = wall time spent in CP-SAT polish call
- `chain.ms` = wall time spent in chain search pass
- `rooming_repair.ms` = wall time spent in room assignment repair/recovery path

Iteration/work counts (frozen meanings):

- `greedy.iterations` = number of section-placement decisions attempted
- `sa.iterations` = number of SA iterations attempted
- `cpsat.iterations` = `1` if CP-SAT polish ran, else `0`
- `chain.iterations` = number of chain-search iterations attempted
- `rooming_repair.iterations` = number of placements reassigned by the repair pass

The key rule is consistency and documented meaning, not maximal detail.

### 4. Scenario-level aggregation

Where scenario-level payloads already merge board-level results, PR6 must aggregate telemetry by summing corresponding keys.

No per-board telemetry payload in PR6.

### 5. CLI acceptance surface

Add:

- `pr6_telemetry_report`

Purpose:

- print stage timing/work summary for a scenario fixture or scenario id
- human-readable operational check
- same pattern as PR3/PR5 acceptance reports

---

## Out of scope

- no placement or scoring changes
- no UI/dashboard work
- no async execution
- no retries / job state
- no new solver heuristics
- no new provenance codes
- no mutation of `decision_trace`
- no weight tuning
- no login hotfix work (P1 track `hotfix/login-500-render`)
- no mypy / dependency cleanup

---

## Flag plan

Single flag:

- `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED`

Lifecycle:

- default `False` through commits 2–7
- flipped to `True` in commit 8

Flag-off contract (chosen schema):

- `stage_telemetry` is **always present**
- when flag off: all values zero

This is the cleanest for consumers and parity helpers.

---

## Acceptance bars

### 1. Flag-off semantic parity

Against current master baseline (`31f22dd`):

- identical placements
- identical room assignments
- identical scores
- identical `decision_trace`
- identical `perturbation_metric`
- only PR6 telemetry fields may differ, and they must neutralise to zero under flag-off

Use a normalised parity helper similar to PR5's `strip_pr5_fields_for_parity`.

### 2. Flag-on observational only

With telemetry on:

- no placement change
- no rooming change
- no score change
- no feasibility-rate change

### 3. Overhead ceiling

- wallclock p95 ≤ **1.1x** current master baseline

Stricter than PR5 because PR6 is pure timing/work instrumentation.

### 4. Telemetry completeness

For scenario-pack runs:

- all 5 stage keys present in both `stage_ms` and `stage_iterations`
- values are non-negative integers
- when a stage runs, its `stage_ms` must be > 0 unless timing resolution genuinely rounds to zero on a tiny fixture; documented if so

### 5. Aggregation correctness

Scenario-level telemetry must equal the sum of board-level telemetry for the same run.

### 6. CLI/report parity

`pr6_telemetry_report` must agree with the planner payload values for the same scenario.

---

## Commit plan

### Commit 0

- `docs/PR6-DOR.md` (this file)

### Commit 1

- failing tests + scenario-pack additions
- tripwire for missing `timetable_stage_telemetry.py`
- parity fixtures
- telemetry-shape fixtures

### Commit 2

- `core/services/timetable_stage_telemetry.py`
- empty payload helpers
- merge helpers
- flag helper
- shape tests green

### Commit 3

- greedy stage timing/work capture

### Commit 4

- SA stage timing/work capture

### Commit 5

- CP-SAT stage timing/work capture

### Commit 6

- chain + rooming_repair timing/work capture

### Commit 7

- scenario aggregation
- `pr6_telemetry_report`
- acceptance-pack tests
- parity helper

### Commit 8

- flip `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED` default to `True`
- `docs/PR6-PROMOTION-NOTE.md`
- rollback notes
- no logic changes

---

## Tests

Core tests to include:

1. telemetry module shape / zero sentinel
2. flag-off zero telemetry
3. greedy populates only greedy keys
4. SA populates SA keys without changing output
5. CP-SAT populates CPSAT keys without changing output
6. chain populates chain keys without changing output
7. rooming_repair populates rooming keys without changing output
8. scenario-level sum equals board-level sum
9. semantic parity helper strips PR6 fields correctly
10. CLI output agrees with payload
11. flag-off parity fixture
12. flag-on no-behaviour-change fixture

---

## Rollback

Three tiers, same convention as PR3–PR5:

### 1. Env kill-switch

- `TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED=false`

### 2. Revert commit 8

- if the issue is only default-on promotion

### 3. Revert PR6 merge

- if telemetry logic itself is the regression source

---

## Non-goals / explicit deferrals

- async optimiser and background jobs → PR7
- stage dashboards → later
- solver-stat deep integration → later
- per-stage provenance UI → later
- performance optimisation based on telemetry → later

---

## Implementation cautions

- Use a **monotonic clock** (`time.monotonic()` or `time.perf_counter()`) consistently for all stage timing.
- Do **not** use `datetime.now()` / wallclock diffs — subject to NTP adjustments.

---

## Gate

- **Do not start PR6 implementation** (commits 1+) until the `/login/` 500 P1 traceback is classified per ChatGPT's decision rule:
  - traceback in shared middleware/settings/startup → pause PR6 coding
  - traceback in auth/admin/group/scope specific → keep PR6 moving

---

## Branch

`refactor/pr6-stage-telemetry` (base: master @ `31f22dd` = PR5 tip).
