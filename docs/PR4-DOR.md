# PR4 — Definition of Ready

**Branch:** `refactor/pr4-instructor-realism-semantic-cleanup`
**Base:** master @ `cec5988` (post-PR3 decision-trace + warm-start)
**Theme:** Data realism + semantic cleanup. Make instructor conflicts real, make the prayer rule a single-source predicate, centralise the lab-room heuristic, retire a dead counter.

This DoR is the pre-code agreement on scope, non-scope, acceptance bar, flag plan, and commit sequence. No code lands on this branch until ChatGPT signs off. This version incorporates six amendments (A1–A6) resolved on 2026-04-20.

---

## Why this PR, why now

PR3 closed the *explainability* loop — every placed section carries a `decision_trace` with chosen slot + up to 3 rejected alternatives, and warm-start minimises churn across re-runs. The trace now ships `INSTRUCTOR_CLASH` as a defined rejection-code sentinel, but **no live code-path emits it** — the fixture is skipped. PR4 makes that emission real, and cleans up three adjacent semantic debts PR3 surfaced but did not fix:

1. **`INSTRUCTOR_CLASH` is dead.** The planner never emits the code at runtime. Instructor-level clashes today are discovered implicitly by the scoring penalty — they reduce a candidate's score but leave no typed trace entry. Registrars reading `decision_trace` cannot see "rejected because instructor was teaching another section."
2. **Two prayer-rule predicates co-exist.** The PR1 configured-windows rule is authoritative for rejection reasons, but a legacy `_start_is_blocked(start_hour, start_minute)` hardcoded against 11:35–12:59 still runs as a *filter* inside `auto_place_board` and `cpsat_polisher`. Two sources, one of which is not operator-configurable. Day-differentiated prayer windows diverge easily from that hardcoded window, so the two rules can silently disagree.
3. **The "this is a lab" test is spread across three duration literals.** Planner, rooming heuristic, and XLSX export each carry their own `duration > 80` check. PR3's `ROOM_HEURISTIC_MISMATCH` code catches disagreement between planner and oracle but not between oracle and export, and each literal is a latent source of semantic drift.
4. **`lecture_room_reject_due_to_buffer_count` is a vestigial counter.** Replaced functionally by `buffer_only_rejects` during PR2, retained only for backwards-compat on four test assertions. Live code no longer reads it.

PR4 resolves these four in one branch because they share surface area (planner scoring loop, candidate filtering, rejection-code emission). Splitting them would require re-testing the same surface four times.

---

## In scope

### 1. Instructor identity plumbing

**Source.** `TermSectionMeeting.instructor` — existing text field, already populated by the term ingestion pipeline. No new model field. No migration.

**Normalisation discipline (A6).** The string is treated as an **opaque single string**. Normalisation is strictly:

```python
def _normalise_instructor(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped.casefold() if stripped else None
```

No comma-splitting. No "/" or "&" delimiter heuristics. No multi-instructor parsing. A string like `"Dr. Smith / Dr. Jones"` is one opaque instructor id, not two. This is a *deliberate* scope boundary — parsing multi-instructor strings is a data problem we have not scoped.

**If commit 2 encounters non-trivial multi-instructor strings in real data**, I will stop and raise a scope question before proceeding. Acceptable outcomes:

- **Data proves a delimiter rule** → amend the normaliser in a scoped follow-up commit with ChatGPT sign-off.
- **Data is heterogeneous** → defer multi-instructor clash detection to PR5, ship PR4 with single-string semantics.
- **Data is clean** → continue as planned.

This prevents silent overreach into a parsing problem we have not agreed to solve.

**Runtime schedule dict.** A per-run `instructor_schedule: dict[str, set[tuple[str, int]]]` keyed by `_normalise_instructor(meeting.instructor)` → `{(day, start_minute), ...}`. Populated from already-placed sections during `auto_place_board`. The dict is transient — built per run, never persisted.

### 2. Real `INSTRUCTOR_CLASH` emission

**Where.** In the candidate-scoring inner loop of `auto_place_board` (the greedy placer), before the room-assignment step. When a candidate slot's normalised instructor id is already in `instructor_schedule` at that `(day, start_minute)`, the candidate is rejected with `rejection_code=INSTRUCTOR_CLASH` and a `rejection_context={"clashing_section_code": "..."}`.

**Decision-trace wiring.** Reuses the existing PR3 trace-capture machinery — the rejected candidate is eligible to appear in the chosen section's `decision_trace[...].alternatives` list (top-3 by score). No new trace plumbing.

**Flag.** `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` (default `False` in commit 3, promoted to `True` at end-of-PR). Env-overridable. When `False`, instructor-level conflicts are not emitted — behaviour identical to master cec5988.

### 3. Prayer-rule unification

**Problem.** `_start_is_blocked(start_hour, start_minute)` in `core/services/timetable_autoplace.py` is a leftover start-window check hardcoded against 11:35–12:59. It runs as a **filter** at 5 call-sites:

- `core/services/timetable_autoplace.py` — lecture placement candidate loop
- `core/services/timetable_autoplace.py` — lab placement candidate loop
- `core/services/cpsat_polisher.py:158` — CP-SAT candidate generation
- `core/services/cpsat_polisher.py:166` — CP-SAT re-score pass
- `core/services/load_balanced.py` — module-level import (unused but retained)

The configured-windows rule (`PRAYER_OVERLAP` rejection in `prayer_rule.py`) is authoritative. The filter is redundant when the configured rule is on, and silently divergent when it is off (legacy kills candidates the configured rule would permit).

**Commit 4 — prayer overlap/divergence measurement.** Before deleting anything, produce a scenario-pack report:

- **(a)** Meetings the legacy filter would have killed (across the frozen PR3 scenario pack).
- **(b)** Meetings the configured rule would kill (same pack).
- **(c)** Set-difference in both directions: `legacy \ configured` and `configured \ legacy`.

The report is an **explained-delta** attached to the PR description (and saved as `snapshots/planner-refactor-2026-04-20/PR4-PRAYER-DELTA.md`). It does NOT assert one filter is a subset of the other — day-differentiated prayer windows can diverge from a hardcoded window in either direction. The goal is to *document* the divergence with cause, not constrain its shape.

**Commit 5 — removal.** Delete `_start_is_blocked` and all 5 call-sites. The configured-windows rule becomes the single prayer-rejection source.

### 4. `meeting_requires_lab_room()` helper

**Problem.** The predicate "does this meeting need a lab room" is spelled three different ways:

- `core/services/timetable_autoplace.py` — `meeting.duration > 80` inside the placer candidate filter.
- `core/services/timetable_room_oracle.py` — `meeting.duration > 80` inside the oracle's capability match.
- `core/services/xlsx_export.py` — `meeting.duration > 80` inside the lab-sheet selector.

Three literals, three places, same intent. The `ROOM_HEURISTIC_MISMATCH` code in PR3's room-oracle exists partly to detect when these fall out of sync.

**Commit 6.** Introduce `core/services/timetable_lab_predicate.py::meeting_requires_lab_room(meeting) -> bool`. The name intentionally describes the room requirement, not a section-level attribute (sections have both lab and lecture meetings — the predicate is per-meeting). Route all three call-sites through the helper. Update the oracle's `ROOM_HEURISTIC_MISMATCH` comparison to the same predicate.

**Flag.** `TIMETABLE_LAB_HEURISTIC_UNIFIED` (default `False`, promoted to `True`). When `False`, each call-site uses its old literal — the helper is present but dormant. The flag protects the export sheet from sudden re-layout if the unified predicate's semantics drift.

### 5. Remove `lecture_room_reject_due_to_buffer_count`

**Commit 7.** The counter was replaced functionally by `buffer_only_rejects` in PR2. Four test assertions still reference it:

- `tests/test_timetable_capacity_buffer.py` — 3 assertions
- `tests/test_pr2_silent_unassigned_sites.py` — 1 assertion

All 4 are updated to consume `buffer_only_rejects` instead. The counter is removed from the payload, the summation path, and the per-run log line. No flag — direct removal, rollback is revert-commit-7.

### 6. Scenario-pack additions

**Commit 1** adds:

- `pr4_instructor_clash.json` — two sections same instructor overlapping slot. Unskips the existing PR3 fixture test.
- `pr4_prayer_divergence_a.json` — configured rule kills a slot the legacy filter permits (day-differentiated window later than 12:59).
- `pr4_prayer_divergence_b.json` — legacy filter kills a slot the configured rule permits (exception day with no prayer window).
- `pr4_lab_predicate_mismatch.json` — meeting with `duration == 80` exactly, which lands on the boundary of the old `>80` literal and exposes oracle/export drift if any branch is off-by-one.

### 7. Tests

Fixture-backed, one per new code path:

1. `TestInstructorClashEmission::test_overlapping_instructor_produces_clash_code` — fixture 1, asserts `INSTRUCTOR_CLASH` appears in the second section's trace alternatives.
2. `TestInstructorClashFlag::test_flag_off_suppresses_emission` — flag off → no `INSTRUCTOR_CLASH` entries, placement decisions unchanged.
3. `TestInstructorNormalisation::test_whitespace_and_case_fold_equivalent` — `"Dr. Smith"`, `"dr. smith "`, `"DR. SMITH"` collide; `"Dr. Smith / Dr. Jones"` does NOT match `"Dr. Smith"` (opaque-string discipline).
4. Five targeted tests for `_start_is_blocked` removal (A1) — one per former call-site, asserting the candidate is not excluded by the old rule after removal.
5. `TestPrayerSingleSource::test_configured_rule_is_sole_prayer_source` — trace rejection-codes for prayer-related rejections are all `PRAYER_OVERLAP`; none are attributed to the legacy filter.
6. `TestPrayerDivergenceReport::test_delta_matches_fixture_counts` — fixtures A and B produce the expected (a)/(b)/(c) counts in the divergence report.
7. `TestLabPredicateUnified::test_all_three_sites_agree_on_boundary_meeting` — fixture with `duration == 80`; planner, oracle, export all classify identically.
8. `TestLabPredicateFlag::test_flag_off_preserves_old_literal_behaviour` — with flag off, each site uses its old literal.
9. `TestCounterRemoval::test_buffer_only_rejects_replaces_old_counter` — 4 updated assertions pass against `buffer_only_rejects`.

Existing PR1/PR2/PR3 tests must continue to pass unmodified. The PR3 acceptance pack (`tests/test_pr3_acceptance_pack.py`) remains the CI gate for everything PR3 shipped.

---

## Out of scope

- **Multi-instructor parsing.** Opaque single-string semantics only (A6). If data proves a delimiter, that's a scoped follow-up.
- **New CP-SAT constraints.** No OR-Tools changes.
- **Weight / objective-function tuning.** PR4 adds a *typed rejection* for instructor clashes, not a *scoring penalty change*.
- **UI surface for any of the four capabilities.** Same pattern as PR3 — registrars read via logs, JSON payload, and management commands.
- **Baseline DB persistence.** Deferred from PR3, still deferred.
- **V2 deep-phase instrumentation.** Chain-search / CP-SAT polish / ranking stages do NOT gain `INSTRUCTOR_CLASH` emission — same scope-cap as PR3 trace-capture.
- **Per-student timetable generation.** Still deferred.

---

## Acceptance bar (measurable, CI-enforceable)

Six bars. All must hold before merge.

1. **`INSTRUCTOR_CLASH` fixture unskipped and green.** The fixture-1 test asserts the code appears in the second section's trace alternatives with `rejection_context.clashing_section_code` populated.

2. **`_start_is_blocked` removal.** Targeted tests (one per former call-site) demonstrate no call-site still depends on the legacy filter (A1). Code-review checklist item confirms the function definition and all 5 call-sites are gone. Grep is a development aid, not a formal gate — the targeted tests are the gate.

3. **Prayer single-source semantics** — three-part, all must hold (A3):
   - **3a)** After legacy-filter removal, no candidate is excluded solely by the old start-window rule. Asserted by the 5 targeted removal tests in bar #2 and by `TestPrayerSingleSource`.
   - **3b)** The configured-windows rule is the single prayer-rejection source — telemetry attribution is unambiguous. Asserted by scanning trace rejection-codes across the scenario pack; every prayer-related rejection carries `PRAYER_OVERLAP`, never a legacy-filter origin.
   - **3c)** Scenario-pack measurement documents any count delta against the legacy-filter era, with written explanation of the delta's cause. The `PR4-PRAYER-DELTA.md` report is the artefact.

4. **`ROOM_HEURISTIC_MISMATCH` reduction.** Count on the scenario pack is reduced materially versus master cec5988, OR any remaining cases are individually explained in the promotion note. The helper unification should eliminate all planner/oracle/export disagreement; any residual is a different heuristic (e.g. room-type preference, not duration), which must be documented.

5. **Feasible-rate floor.** `>= 99%` of PR3 baseline (post-merge master cec5988) on the scenario pack.

6. **Performance.** Planner wallclock p95 `<= 1.3x` PR3 baseline. Measured with the PR3 perf harness extended for PR4's two new flags.

---

## Flag plan

| Flag | Default (commit 3) | Default (commit 8) | Controls |
|---|---|---|---|
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | `False` | `True` | Gates real-time emission of `INSTRUCTOR_CLASH` during candidate scoring. Off = PR3 behaviour (scoring penalty only, no typed trace entry). |
| `TIMETABLE_LAB_HEURISTIC_UNIFIED` | `False` | `True` | Gates routing of planner/oracle/export through `meeting_requires_lab_room()`. Off = each call-site uses its old `duration > 80` literal. |

Rationale for two flags: instructor-clash emission is a behaviour-observable change (trace-payload gains entries, `decision_trace` drilldowns show new rejection codes); lab-predicate unification is also behaviour-observable (a `duration == 80` meeting may re-classify). They roll out independently.

Rollback paths below.

---

## Rollback

- **Instructor-clash emission:** `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=false` — env kill-switch, no redeploy. Placement decisions bit-for-bit identical to PR3.
- **Lab predicate:** `TIMETABLE_LAB_HEURISTIC_UNIFIED=false` — env kill-switch. Export reverts to old `duration > 80` literal. Planner candidate filter and oracle unchanged (the helper is already installed; the flag just chooses which code-path reads it).
- **Prayer legacy-filter removal:** no kill-switch. Rollback = `git revert <commit-5-sha>`. Acceptable because the configured-windows rule is authoritative and has shipped since PR1 — removal is the semantic cleanup the two-rule regime was blocking.
- **Counter removal:** no kill-switch. Rollback = `git revert <commit-7-sha>`. Counter is dead in live code; only the 4 test assertions change.

All four rollbacks are independent — failure in one does not force rolling back the others.

---

## Commit plan (final, post-amendment)

| # | Commit | What lands |
|---|---|---|
| 0 | PR4 DoR | This file (`docs/PR4-DOR.md`). Branch initialised from master cec5988. |
| 1 | Failing tests + scenario-pack additions | The 9 test cases above as `test_pr4_*.py`; fixture JSONs under `snapshots/planner-refactor-2026-04-20/fixtures/pr4_*.json`; `INSTRUCTOR_CLASH` fixture unskip pre-wiring (skip-marker removed, test fails). Green-behind-flag (both PR4 flags default `False`). |
| 2 | Instructor identity plumbing | `_normalise_instructor()` + runtime `instructor_schedule` dict built during `auto_place_board`. Strip+casefold only. Opaque-string discipline documented in code + module docstring. No emission yet. *If commit 2 encounters non-trivial multi-instructor strings, stop and raise a scope question per A6.* |
| 3 | Real `INSTRUCTOR_CLASH` emission | Wire candidate-scoring loop to emit the code; trace-capture pulls rejected candidates into alternatives list. Flag-gated via `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED`. |
| 4 | Prayer overlap/divergence measurement | Scenario-pack report comparing `legacy \ configured` vs `configured \ legacy` in both directions. Artefact: `snapshots/planner-refactor-2026-04-20/PR4-PRAYER-DELTA.md`. Explained-delta commentary in the PR body. No legacy-filter removal yet. |
| 5 | Remove legacy `_start_is_blocked` | Delete function + 5 call-sites; the 5 targeted tests (per A1) turn green; configured-rule becomes the single prayer source. |
| 6 | Centralised `meeting_requires_lab_room()` helper | One authoritative predicate in `core/services/timetable_lab_predicate.py`; autoplace, rooming, export routed through it; oracle's `ROOM_HEURISTIC_MISMATCH` comparison updated. Flag-gated via `TIMETABLE_LAB_HEURISTIC_UNIFIED`. |
| 7 | Remove `lecture_room_reject_due_to_buffer_count` | Delete counter + update 4 test assertions to use `buffer_only_rejects`. Counter removal stays a code change, isolated from the docs-only closeout (A5). Matches PR3's pattern (code changes land in commit 7, docs in commit 8). |
| 8 | Promotion note (docs-only) | `docs/PR4-DOR.md` closeout block + `docs/PR4-PROMOTION-NOTE.md` with rollback narrative. Flags promoted to `True`. Memory update. No code change. |

Commits 1–2 are green-behind-flag (new structures unused at runtime). Commits 3 and 6 are additive behind flags. Commits 4, 5, 7 are direct refactors. Commit 8 is docs only.

---

## Split decision

**Status:** unified. PR4 stays single-PR unless commit 2 discovers multi-instructor strings in the data that require a parsing rule (per A6). In that case: stop, report findings, propose `PR4A` (instructor realism subset) + `PR4B` (semantic cleanup subset) and re-gate.

---

## Pre-condition / Sign-off

- [ ] ChatGPT reviews this DoR and approves scope / non-scope / acceptance bar. **Amendment round 1 (2026-04-20):** six amendments applied — (A1) grep replaced with targeted tests + review checklist for `_start_is_blocked` removal, (A2) commit-4 reworded to "measure overlap/divergence" with explained-delta report (not subset claim), (A3) acceptance bar #3 is 3-part (no-legacy-exclusion + single-source semantics + explained-delta artefact), (A4) helper named `meeting_requires_lab_room()` not `is_lab_section()`, (A5) commit 7 = counter removal + commit 8 = docs-only closeout, (A6) instructor string treated as opaque + strip+casefold only + stop-and-report on non-trivial multi-instructor data.
- [x] ChatGPT signed off on the amended DoR. Reply on 2026-04-20: *"Approved. Commit the DoR and start PR4 commit 1."*
- [ ] Commit 1 (failing tests + scenario pack) lands next.
