# PR4 Prayer-Rule Divergence Report

Measured 2026-04-20 against the PR4 prayer-divergence fixtures
(`pr4_prayer_divergence_a.json`, `pr4_prayer_divergence_b.json`) under
`snapshots/planner-refactor-2026-04-20/fixtures/`.

The purpose is to rebut any "one rule is a subset of the other"
assumption ahead of commit 5's removal of `_start_is_blocked`. Both
fixtures together force the bidirectional set-difference view so the
next commit can delete the legacy filter confidently.

## Rules compared

- **Legacy filter** — `_start_is_blocked(start_time)` in
  `core/services/timetable_autoplace.py`. Rejects a candidate iff
  `start_time ∈ [11:35, 12:59]` (hardcoded, day-independent, applies
  every day of the week).
- **Configured rule** — `prayer_overlap_rejection(meeting,
  prayer_windows)` in `core/services/timetable_validation.py`. Rejects
  iff the candidate meeting interval overlaps any same-day entry in
  `TIMETABLE_PRAYER_WINDOWS` (per-day, interval-based, configurable).

## Buckets

For each candidate slot in each fixture's `slot_pool` we classify the
outcome into one of the bidirectional set-difference buckets:

- **(a) legacy \ configured** — legacy rejects, configured permits.
  "Legacy over-rejects."
- **(b) configured \ legacy** — configured rejects, legacy permits.
  "Legacy under-rejects."
- **(c) legacy ∩ configured** — both reject. "Agreement region."

A slot that neither rule rejects is permitted and not counted in any
bucket.

## Per-fixture counts

### `pr4_prayer_divergence_a` (Thu 13:00–14:15 and Thu 14:30–15:45; prayer window Thu 12:30–13:45)

| Slot | Legacy rejects? | Configured rejects? | Bucket |
| ---- | --------------- | ------------------- | ------ |
| Thu 13:00–14:15 | no (13:00 > 12:59) | yes (overlaps 12:30–13:45) | **(b) configured \ legacy** |
| Thu 14:30–15:45 | no | no | permitted (not counted) |

- (a) legacy \ configured: **0**
- (b) configured \ legacy: **1**
- (c) legacy ∩ configured: **0**

### `pr4_prayer_divergence_b` (Fri 12:00–13:15 and Fri 13:30–14:45; no configured windows)

| Slot | Legacy rejects? | Configured rejects? | Bucket |
| ---- | --------------- | ------------------- | ------ |
| Fri 12:00–13:15 | yes (12:00 ∈ [11:35, 12:59]) | no (no window on Fri) | **(a) legacy \ configured** |
| Fri 13:30–14:45 | no | no | permitted (not counted) |

- (a) legacy \ configured: **1**
- (b) configured \ legacy: **0**
- (c) legacy ∩ configured: **0**

## Combined (bidirectional set-difference on the divergence fixtures)

| Bucket | Count |
| ------ | ----- |
| (a) legacy \ configured | **1** |
| (b) configured \ legacy | **1** |
| (c) legacy ∩ configured | **0** |

Both directions of the set-difference are non-empty, so neither rule
is a superset of the other. The legacy filter rejects slots the
configured rule permits (Fri 12:00 with no Friday window) and vice
versa (Thu 13:00 with a configured Thursday window). The agreement
region (c) is empty on this fixture pair because the fixtures were
constructed to isolate the divergence axes — the wider scenario pack
would produce a non-zero (c), but the divergence fixtures deliberately
avoid it to keep the rebuttal sharp.

## What this means for commit 5

The two rules are not equivalent. Commit 5 removes the legacy filter
and leaves the configured rule as the sole prayer-rejection source.
Behavioural change on live data is bounded by:

- bucket (a): previously-rejected candidates will now be permitted
  unless another rule (room, instructor, student) rejects them.
- bucket (b): previously-permitted candidates will now be rejected by
  the configured rule. Callers that relied on the legacy filter
  silently letting these through must have the configured rule
  covering them — which is the single-source-semantics guarantee the
  PR4 acceptance bar 3b enforces.

The configured rule is the canonical source going forward; the legacy
filter is treated as removable dead code after this report is in.

## Reproducibility

This report was computed directly from the fixture JSONs — no planner
run was required because both rules are pure functions of
`(day, start_time, end_time)` and the configured window list. The
analytical path is:

```
legacy = (start_minute ∈ [695, 779])                # 11:35–12:59
configured = ∃ w ∈ prayer_windows with              # interval overlap
             same_day(w, m) AND w.start < m.end AND w.end > m.start
```

To regenerate after changing a fixture, re-apply the two checks to
every `slot_pool` entry, classify into (a)/(b)/(c), and update the
tables above.
