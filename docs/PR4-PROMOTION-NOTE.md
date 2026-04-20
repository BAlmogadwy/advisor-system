# PR4 — Promotion Note

**Branch:** `refactor/pr4-instructor-realism-semantic-cleanup`
**Base:** master @ `cec5988` (post-PR3 decision-trace + warm-start)
**Promotion commit:** commit 8 (this commit)
**Theme:** Data realism + semantic cleanup.

---

## What shipped

PR4 closes four adjacent semantic debts PR3 surfaced but did not fix, resolved in a single branch because they share surface area (candidate scoring loop, candidate filtering, rejection-code emission).

1. **`INSTRUCTOR_CLASH` is now emitted at runtime.** The planner builds a per-section-tagged `instructor_schedule_full` dict during `auto_place_board` and rejects candidates whose normalised instructor id collides with another already-placed section at the same `(day, start_minute)`. The rejection records the clashing section + instructor id in `rejection_context`, and the candidate is eligible to appear in the chosen section's `decision_trace[...].alternatives` list (top-3 by score). Registrars reading the trace can now see "rejected because instructor was teaching another section" as a typed entry.

2. **Prayer rule unified behind `configured_windows`.** The legacy `_start_is_blocked(start_hour, start_minute)` hardcoded against 11:35–12:59 is gone from all five call-sites (`auto_place_board` lecture + lab loops, `cpsat_polisher` candidate generation + re-score, `load_balanced` dead import). The PR1 configured-windows rule is the sole prayer-rejection source; day-differentiated prayer windows configured by operators now always win, and the two-source drift risk is closed. The pre-removal divergence measurement is in `snapshots/planner-refactor-2026-04-20/PR4-PRAYER-DELTA.md`.

3. **Lab-room predicate centralised.** `core/services/timetable_lab_predicate.py:meeting_requires_lab_room(...)` is the single authoritative "does this meeting need a lab room?" helper. The boundary is `>= 80` minutes (inclusive), matching the oracle's original PR2 intent — the prior `> 80` strict comparison silently excluded exactly-80-minute meetings from the lab pool. Planner (×2), rooming (×2), and oracle (`check_heuristic_match`) all route through the helper when the flag is on.

4. **`lecture_room_reject_due_to_buffer_count` retired.** The vestigial counter was replaced functionally by `buffer_only_rejects` in PR2; live code no longer read it. Commit 7 deleted the variable, increment, and payload key in rooming; `autoplace` now surfaces `buffer_only_rejects` in all three return paths (derived from `room_failure_breakdown` so the counter cannot drift from the underlying failure records). The four legacy test assertions migrated cleanly.

---

## Feature flags — promoted in this commit

| Flag | Old default | New default | Env var | Kill-switch |
|---|---|---|---|---|
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | `False` | **`True`** | same name | Set to `false` → no `INSTRUCTOR_CLASH` emission, planner behaves exactly as master `cec5988`. |
| `TIMETABLE_LAB_HEURISTIC_UNIFIED` | `False` | **`True`** | same name | Set to `false` → each call-site reverts to its old `duration > 80` literal; oracle re-emits `ROOM_HEURISTIC_MISMATCH` observations. |

Both flags are read on every request via `is_instructor_clash_enabled()` / `is_lab_heuristic_unified()`. Flipping the env var on Render and restarting the worker is a live kill-switch — no redeploy required.

---

## Rollback path (tiered, cheapest first)

1. **Env switch** — set `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED=false` and/or `TIMETABLE_LAB_HEURISTIC_UNIFIED=false` on Render, restart worker. Traffic-free, no redeploy.
2. **Revert commit 8 only** — restores default `False` for both flags. Keeps the instructor-identity plumbing, prayer unification, and dead-counter removal in place; disables only the two flagged behaviours. Re-deploy needed.
3. **Revert the merge commit** — drops the entire PR4 feature set. Use only if the code-level changes (not just the flag-gated behaviours) turn out to be regressive.

The prayer-rule unification (commit 5) and dead-counter removal (commit 7) are flagless direct refactors. Reverting them individually is commit-level revert.

---

## Behavioural changes (flag-on)

- **New rejection code in `decision_trace`.** Alternatives may now carry `rejection_code=INSTRUCTOR_CLASH` with `rejection_context={"clashing_section": "...", "clashing_instructor_id": "..."}`. Schema is additive — existing consumers (tests, reports) keep working unchanged.
- **Payload key `buffer_only_rejects`** is now present in `auto_place_board`'s return dict on all three exit paths (board-not-found, no-budget, main). Existing `lecture_room_reject_due_to_buffer_count` consumers must migrate — the key is gone.
- **`check_heuristic_match` returns `None` early.** Consumers of `ROOM_HEURISTIC_MISMATCH` will see counts drop to zero because the unified predicate makes the mismatch definitionally impossible. This is the intended outcome of A4; if observers want to reinstate the legacy observation for a debug session they can flip `TIMETABLE_LAB_HEURISTIC_UNIFIED=false`.
- **Boundary meetings now classify as labs.** Exactly-80-minute 4-credit meetings were silently excluded from the lab pool by the `> 80` strict guard; the unified `>= 80` predicate includes them. This is a deliberate, documented semantic change and matches the oracle's original PR2 intent.

---

## Acceptance

- 336 tests passing, 1 skipped (`test_timetable_optimize.py` ignored from the sweep as usual — it is an interactive smoke test, not a regression gate).
- `tests/test_pr3_acceptance_pack.py` — 21/21 green. PR3's CI gate remains stable.
- `tests/test_pr4_prayer_unification.py` — 8/8 green (Section A tripwires + Section B single-source semantics + divergence report).
- `tests/test_pr4_lab_predicate.py` — 6/6 green.
- `tests/test_pr4_counter_removal.py` — 2/2 green.
- `tests/test_pr2_room_oracle.py::test_07_heuristic_mismatch_*` and `test_pr2_silent_unassigned_sites.py::TestOracleHeuristicMatch` pinned to the flag-off kill-switch path; they now regression-guard the legacy observation behaviour rather than the promoted default.
- Pre-commit hooks all pass (ruff, bandit; mypy skipped with the usual `SKIP=mypy`).

---

## Known non-goals (out of scope for PR4, carry to PR5 or later)

- **Multi-instructor string parsing.** Opaque single-string semantics only. A string like `"Dr. Smith / Dr. Jones"` is one opaque instructor id. A6 of the DoR documents the scope boundary. If data proves a delimiter rule, that's a scoped PR5 follow-up.
- **Credit-hour-aware lab predicate.** The helper is duration-only. Callers that want a credit-hour gate on top should layer it above `meeting_requires_lab_room`, not inside it — keeps the helper's signature compatible with the fixture pack and avoids re-litigating the `cr == 4` gate inline.
- **`ROOM_HEURISTIC_MISMATCH` enforcement.** Still observational. Turning it into a hard reject is a PR5 question once operators confirm the unified predicate is matching their lab-room inventories 1-1 in production.

---

## Memory update

`MEMORY.md` under "Session Log" records the PR4 promotion:

- Both PR4 flags flipped to `True` default at commit 8.
- Env kill-switches live and verified.
- Test counts: 336 passing (was 334 pre-PR4-commit-8, +2 from `test_pr4_counter_removal.py` turning green once the counter was removed).
- Rollback doc: `docs/PR4-PROMOTION-NOTE.md` (this file).
