"""PR6 commit 2 — stage-telemetry helper module.

Observability-only companion to PR5's ``timetable_solver_codes``.
Collects wall-time and iteration/work counts for the five stages of the
V2 pipeline so payload consumers can answer "how long did each stage
take" and "how much work did each stage do" without ever affecting
placement, rooming, or scoring.

Stages instrumented (frozen set, PR6 DoR §3):

- ``greedy``          — initial board/scenario placement path
- ``sa``              — SA / local-search pass
- ``cpsat``           — CP-SAT polish call
- ``chain``           — chain-search pass
- ``rooming_repair``  — room assignment repair / recovery path

Payload shape (schema-stable, keys always present)::

    {
        "stage_ms":         {stage: int (milliseconds, >=0)},
        "stage_iterations": {stage: int (work count,  >=0)},
    }

A stage that did not run reports ``0``. Values are always integers.

Flag contract (PR6 DoR §Flag plan):

- ``TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED`` gates whether callers write
  real values into the payload. When ``False`` (default through commits
  2-7), callers use ``empty_stage_telemetry()`` as a zeroed-out
  sentinel. The ``stage_telemetry`` block itself is **always present**
  so consumers never need an ``if "stage_telemetry" in payload`` guard.
- Flag flips to ``True`` at commit 8.

Timing discipline (PR6 DoR §Implementation cautions):

- Callers MUST use ``time.monotonic()`` or ``time.perf_counter()`` —
  not ``datetime.now()`` — so NTP adjustments can't produce negative
  or wildly inflated deltas.
- **Prefer ``time.perf_counter()``** for stage timing.
  ``time.monotonic()`` on Windows rounds to the ~15 ms OS scheduler
  tick, which made tiny fixtures report ``0 ms`` on the commit-3
  greedy run even though real DB-write work was happening.
  ``perf_counter()`` is also guaranteed monotonic (Python stdlib
  contract) but offers sub-microsecond resolution on every supported
  platform, so the metric stays meaningful regardless of fixture size.
  All PR6 stage instrumentation (commits 3–6) uses ``perf_counter``.
- This module stays clock-agnostic: it only stores the integer ms
  caller hands it. Keeping the monotonic discipline at the call site
  matches PR5's pattern (no implicit wall-time in helper modules).

Sub-millisecond clamp convention (PR6 commit 6):

- A few stages (notably ``rooming_repair``) can finish in under 1 ms
  on trivial fixtures. Raw ``int(elapsed_s * 1000)`` would truncate
  such runs to ``0``, collapsing "ran for 0.1 ms" into "did not run".
- Call sites that want to preserve the "``0`` means did not run"
  invariant therefore clamp with ``max(1, int(...))`` when the stage
  is known to have executed (telemetry is on and work was observed).
  So a value of ``1`` can mean *"ran but faster than our millisecond
  resolution"* — not exactly 1 ms wall time.
- This is an observability-only convention; the raw ms write path
  in ``record_stage_ms`` is unchanged and trusts the caller's integer.
"""

from __future__ import annotations

from typing import Literal

from django.conf import settings

StageKey = Literal["greedy", "sa", "cpsat", "chain", "rooming_repair"]

STAGE_KEYS: tuple[StageKey, ...] = (
    "greedy",
    "sa",
    "cpsat",
    "chain",
    "rooming_repair",
)

STAGE_TELEMETRY_ENABLED_SETTING = "TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED"


def empty_stage_telemetry() -> dict[str, dict[str, int]]:
    """Return a fresh zero-valued telemetry payload.

    Both subkeys are always present with all five stage keys set to
    ``0``. This is also the flag-off sentinel used by acceptance-bar #1
    (flag-off parity): payload is present, values neutralise to zero.
    """
    return {
        "stage_ms": {k: 0 for k in STAGE_KEYS},
        "stage_iterations": {k: 0 for k in STAGE_KEYS},
    }


def record_stage_ms(
    telemetry: dict[str, dict[str, int]],
    stage: StageKey,
    ms: int,
) -> None:
    """Overwrite ``stage_ms[stage]`` with the given integer milliseconds.

    Overwrite (not accumulate) because the canonical use is a single
    stop-the-clock write at stage exit. Aggregation across boards is
    handled by ``merge_stage_telemetry``.
    """
    telemetry["stage_ms"][stage] = int(ms)


def record_stage_iterations(
    telemetry: dict[str, dict[str, int]],
    stage: StageKey,
    iterations: int,
) -> None:
    """Overwrite ``stage_iterations[stage]`` with the given integer count."""
    telemetry["stage_iterations"][stage] = int(iterations)


def merge_stage_telemetry(
    a: dict[str, dict[str, int]],
    b: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Return a new telemetry payload whose values are ``a + b`` per key.

    Used for scenario-level aggregation where the scenario payload is
    the per-key sum of its boards' payloads. Associative, so fold order
    across multiple boards does not matter.
    """
    out = empty_stage_telemetry()
    for k in STAGE_KEYS:
        out["stage_ms"][k] = int(a["stage_ms"].get(k, 0)) + int(b["stage_ms"].get(k, 0))
        out["stage_iterations"][k] = int(a["stage_iterations"].get(k, 0)) + int(
            b["stage_iterations"].get(k, 0)
        )
    return out


def is_stage_telemetry_enabled() -> bool:
    """Return whether PR6 telemetry population is active.

    Reads ``settings.TIMETABLE_PR6_STAGE_TELEMETRY_ENABLED``. Default
    ``False`` in commits 2-7; commit 8 flips the default to ``True``.
    Production can flip via the env var without a redeploy.

    When ``False``:
    - Callers still emit the ``stage_telemetry`` block — it just stays
      at the zeroed ``empty_stage_telemetry()`` value.
    - Per-stage instrumentation short-circuits before any clock reads,
      so there is no measurable overhead.
    """
    return bool(getattr(settings, STAGE_TELEMETRY_ENABLED_SETTING, False))


__all__ = [
    "STAGE_KEYS",
    "STAGE_TELEMETRY_ENABLED_SETTING",
    "empty_stage_telemetry",
    "is_stage_telemetry_enabled",
    "merge_stage_telemetry",
    "record_stage_iterations",
    "record_stage_ms",
]
