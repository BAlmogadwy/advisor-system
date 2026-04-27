"""Exam-timetable run payload schema versioning + normalisation.

Why this module exists
----------------------
``ExamTimetableRun.result_json`` is a JSON blob persisted at build time and
re-read by the detail view + the XLSX exporter. Every new QA tile, every
new telemetry field, every new run-status semantic adds keys to this blob.
Old runs stored *before* a key existed must continue to render — silently
breaking historic registrar artefacts is the single most likely lifecycle
failure as the feature evolves.

Design contract
---------------
1. ``EXAM_RUN_SCHEMA_VERSION`` is a monotonically increasing integer.
2. Every freshly-built payload carries ``schema_version`` set to the current
   constant. Older payloads either lack the key entirely (treated as v0) or
   carry an older int.
3. ``normalise_exam_run_payload(raw)`` walks a chain of versioned
   ``_migrate_v{N}_to_v{N+1}`` functions to produce a typed display payload
   at the current version. Migrators are pure ``dict -> dict``; they do
   not call out to the database, scheduler, or any external service.
4. **Forward-compat:** unknown keys (from a future version this build does
   not yet know about) are preserved verbatim. The default-filler uses
   ``setdefault`` so it never overwrites caller-supplied values.
5. **Defensive:** ``raw`` may be ``None``, a non-dict, or malformed JSON.
   The normaliser returns a sentinel display payload tagged
   ``status="unrenderable"`` rather than raising. The UI shows an honest
   "this run could not be rendered" card instead of a 500.
6. **Idempotent:** ``normalise(normalise(x)) == normalise(x)`` for any
   input. Migrators only run once per call (because a normalised payload
   already has the current ``schema_version``), and the default-filler
   uses ``setdefault``.

Read-side rule
--------------
No template, view, XLSX exporter, or test fixture reads
``ExamTimetableRun.result_json`` directly. They all go through
``load_normalised_run(run)``, which deserialises and normalises in one
call. This invariant is what makes the schema honest — every consumer
sees the same shape regardless of how old the row is.

Adding a new key in a future version
------------------------------------
Suppose v2 introduces ``qa.thin_clash_risk_summary``:

1. Bump ``EXAM_RUN_SCHEMA_VERSION`` to ``2``.
2. Append ``_migrate_v1_to_v2(payload)`` to ``_MIGRATORS`` that fills the
   default for the new key on any payload at version <= 1.
3. Update ``_fill_defaults`` to set the v2 default for fresh payloads
   that didn't go through migration (e.g. brand-new ``"ok"`` payloads).
4. Update the build site to populate the new key directly when running
   new code (the migrator becomes a fallback for legacy rows).
5. Add a regression test that an existing v1 fixture migrates cleanly.

Both old and new runs render correctly throughout this process — that is
the entire point of this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal, TypedDict, cast

# ---------------------------------------------------------------------------
# Version constant
# ---------------------------------------------------------------------------

EXAM_RUN_SCHEMA_VERSION: int = 1
"""Current schema version for ``ExamTimetableRun.result_json`` payloads.

Bump this whenever you add a key the UI or XLSX exporter will read but
older rows lack. Always pair the bump with a migrator function appended
to ``_MIGRATORS`` and a regression test.
"""


# ---------------------------------------------------------------------------
# Display payload type
# ---------------------------------------------------------------------------

ExamRunStatus = Literal[
    "ok",
    "feasibility_error",
    "unrenderable",
    "future_version_unrenderable",
]
"""Discriminator for which payload shape a consumer is looking at.

- ``"ok"``: the scheduler ran and produced a schedule + QA + room plan.
- ``"feasibility_error"``: the pre-scheduling feasibility check failed
  and the scheduler did not run; payload carries ``violations`` only.
- ``"unrenderable"``: the stored payload was missing, corrupt, or not a
  dict. Sentinel produced by the normaliser; never persisted.
- ``"future_version_unrenderable"``: the stored payload's
  ``schema_version`` is *higher* than ``EXAM_RUN_SCHEMA_VERSION``,
  which means it was written by a newer build of the application.
  This build cannot safely render it (forward migrators we don't
  have may have renamed or repurposed keys). The original
  ``schema_version`` is preserved in the returned payload so an
  operator inspecting the row can identify the source build.
"""


class ExamRunDisplayPayload(TypedDict, total=False):
    """The shape every consumer sees after normalisation.

    All keys are optional at the type level (``total=False``) because the
    three payload statuses populate different subsets — a feasibility error
    has no schedule, an unrenderable sentinel has no violations, and so on.
    Consumers should branch on ``status`` first, then read the keys
    relevant to that status.

    The dynamic view-layer fields (``run_id``, ``label``, ``created_at``)
    are also declared here because the detail view injects them after
    normalisation; declaring them keeps the type honest at every read
    site.
    """

    schema_version: int
    status: ExamRunStatus

    # ── status="ok" payload ────────────────────────────────────────────
    students_count: int
    courses: list[str]
    courses_count: int
    conflicts: list[dict[str, Any]]
    conflicts_count: int
    slots: list[dict[str, Any]]
    schedule: list[dict[str, Any]]
    qa: dict[str, Any]
    buckets_summary: list[dict[str, Any]]
    bucket_count: int
    credit_map: dict[str, int]
    seed: int | None
    section_enrollment: dict[str, list[dict[str, Any]]]
    rooms_count: int
    assign_rooms: dict[str, Any]

    # ── status="feasibility_error" payload ─────────────────────────────
    feasibility_error: bool
    violations: list[dict[str, Any]]

    # ── status="unrenderable" sentinel ─────────────────────────────────
    error: str

    # ── injected by the view layer (not stored on the run) ─────────────
    run_id: int
    label: str
    created_at: str


# ---------------------------------------------------------------------------
# Migrator chain
# ---------------------------------------------------------------------------

Migrator = Callable[[dict[str, Any]], dict[str, Any]]


def _migrate_v0_to_v1(payload: dict[str, Any]) -> dict[str, Any]:
    """v0 (unversioned legacy) -> v1 (status discriminator + version stamp).

    v0 = anything stored before this module existed. We can recognise three
    shapes by their keys:

    - ``feasibility_error: True`` -> ``status="feasibility_error"``
    - otherwise (has ``schedule`` / ``qa`` / etc.) -> ``status="ok"``
    - the unrenderable sentinel is never persisted, so we won't see it
      from disk.

    This migrator does not strip or rename any v0 key; it only stamps the
    version and infers the discriminator. Forward-compat by construction.
    """
    payload.setdefault("schema_version", 1)
    if "status" not in payload:
        payload["status"] = "feasibility_error" if payload.get("feasibility_error") else "ok"
    return payload


_MIGRATORS: list[Migrator] = [
    _migrate_v0_to_v1,
    # _migrate_v1_to_v2 goes here when we bump to v2.
]
"""Ordered list of migrators. Index N migrates from version N to N+1.

A payload at version V is normalised by running ``_MIGRATORS[V:]`` in
order. A v0 payload runs every migrator; a v1 payload runs none.
"""


# ---------------------------------------------------------------------------
# Default-fillers (used after migration to guarantee no KeyError downstream)
# ---------------------------------------------------------------------------


def _fill_ok_defaults(payload: dict[str, Any]) -> None:
    """Fill defaults for every key the ``status="ok"`` UI/XLSX reads.

    Uses ``setdefault`` exclusively so caller-provided values are never
    overwritten. The defaults are *empty/zero* equivalents so consumers
    can iterate / count / serialise without conditional guards.
    """
    payload.setdefault("students_count", 0)
    payload.setdefault("courses", [])
    payload.setdefault("courses_count", 0)
    payload.setdefault("conflicts", [])
    payload.setdefault("conflicts_count", 0)
    payload.setdefault("slots", [])
    payload.setdefault("schedule", [])
    payload.setdefault("qa", {})
    payload.setdefault("buckets_summary", [])
    payload.setdefault("bucket_count", 0)
    payload.setdefault("credit_map", {})
    payload.setdefault("section_enrollment", {})
    payload.setdefault("rooms_count", 0)
    payload.setdefault("assign_rooms", {})
    payload.setdefault("seed", None)


def _fill_feasibility_error_defaults(payload: dict[str, Any]) -> None:
    """Fill defaults for the ``status="feasibility_error"`` shape.

    Also fills ``ok``-shape defaults (empty schedule, qa, slots, etc.) so
    every consumer can read those keys unconditionally — the registrar UI
    branches on ``status`` to pick which cards to show, but downstream
    code (XLSX export, count queries, JSON serialisation) does not need
    a status check before indexing into ``payload["schedule"]``.
    """
    payload.setdefault("feasibility_error", True)
    payload.setdefault("violations", [])
    payload.setdefault("courses_count", 0)
    payload.setdefault("students_count", 0)
    payload.setdefault("bucket_count", 0)
    _fill_ok_defaults(payload)


# ---------------------------------------------------------------------------
# Sentinel for unrenderable payloads
# ---------------------------------------------------------------------------


def _unrenderable(reason: str) -> ExamRunDisplayPayload:
    """Build the sentinel payload for missing / corrupt / non-dict input.

    The sentinel carries every "ok"-shape default key so the UI can render
    its empty-state cards without conditional guards, plus an ``error``
    message the registrar sees on the "could not render" card.
    """
    payload: dict[str, Any] = {
        "schema_version": EXAM_RUN_SCHEMA_VERSION,
        "status": "unrenderable",
        "error": reason,
        "violations": [],
    }
    _fill_ok_defaults(payload)
    return cast(ExamRunDisplayPayload, payload)


def _future_version_unrenderable(
    raw_payload: dict[str, Any], future_version: int
) -> ExamRunDisplayPayload:
    """Build the sentinel for payloads at a higher schema_version than we know.

    Preserves the original ``schema_version`` so an operator inspecting
    the row can identify the source build (which is the whole reason the
    payload survived a downgrade scenario). Carries every "ok"-shape
    default so consumers can render their empty-state cards uniformly,
    plus an ``error`` message naming the version mismatch.
    """
    payload: dict[str, Any] = {
        "schema_version": future_version,
        "status": "future_version_unrenderable",
        "error": (
            f"Payload schema_version={future_version} is higher than this "
            f"build's EXAM_RUN_SCHEMA_VERSION={EXAM_RUN_SCHEMA_VERSION}. "
            "The payload was written by a newer build whose migrators we "
            "do not have. Upgrade the application or rebuild the run."
        ),
        "violations": [],
    }
    # Preserve any forward-compat keys the future build added so
    # operators can inspect them (e.g. via the management command's
    # logging) without modifying them.
    for key, value in raw_payload.items():
        if key not in payload:
            payload[key] = value
    _fill_ok_defaults(payload)
    return cast(ExamRunDisplayPayload, payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalise_exam_run_payload(raw: Any) -> ExamRunDisplayPayload:
    """Normalise a raw exam-run payload to the current display shape.

    Accepts ``None``, a non-dict, or a dict at any past schema version
    (and tolerates payloads at a *future* schema version by passing
    unknown keys through verbatim — see the module docstring on
    forward-compat).

    Always returns an ``ExamRunDisplayPayload`` with ``schema_version``
    set to ``EXAM_RUN_SCHEMA_VERSION`` and a valid ``status``
    discriminator. Never raises on malformed input — returns the
    ``"unrenderable"`` sentinel instead.
    """
    if raw is None:
        return _unrenderable("payload is None")
    if not isinstance(raw, dict):
        return _unrenderable(f"payload is not a dict (got {type(raw).__name__})")

    # Determine source version. Anything that isn't a non-negative int is
    # treated as v0 (the unversioned legacy era); deliberately *not* an
    # error — the whole point is graceful upgrade of pre-versioning rows.
    raw_version = raw.get("schema_version")
    if isinstance(raw_version, int) and raw_version >= 0:
        version = raw_version
    else:
        version = 0

    # Hardening: a payload claiming a higher schema_version than this
    # build knows about is unsafe to render with our current consumers.
    # Forward migrators we don't have may have renamed or repurposed
    # keys; silently passing the payload through with our defaults could
    # corrupt the operator's view of a newer-build run. Surface it as a
    # distinct sentinel status that the UI can render as a clear "this
    # run was created by a newer build" card and the XLSX exporter can
    # refuse cleanly.
    if version > EXAM_RUN_SCHEMA_VERSION:
        return _future_version_unrenderable(raw, version)

    # Mutate a copy so the caller's dict is untouched. Idempotency relies
    # on this: normalising a payload should not change the original.
    payload: dict[str, Any] = dict(raw)

    for migrator in _MIGRATORS[version:]:
        payload = migrator(payload)

    # Stamp current version and ensure status is set even if migrators
    # somehow skipped it (defensive — should be impossible with the
    # current migrator chain, but cheap insurance for future migrators).
    payload["schema_version"] = EXAM_RUN_SCHEMA_VERSION
    if payload.get("status") not in (
        "ok",
        "feasibility_error",
        "unrenderable",
        "future_version_unrenderable",
    ):
        payload["status"] = "feasibility_error" if payload.get("feasibility_error") else "ok"

    status: ExamRunStatus = payload["status"]
    if status == "ok":
        _fill_ok_defaults(payload)
    elif status == "feasibility_error":
        _fill_feasibility_error_defaults(payload)
    elif status == "unrenderable":
        # Persisted-unrenderable should be impossible (we never store the
        # sentinel) but if a caller hand-crafts one, fill ok defaults so
        # the UI's empty-state cards still have something to read.
        _fill_ok_defaults(payload)
    elif status == "future_version_unrenderable":
        # Reached only if a caller hand-crafts this status on a payload
        # that wasn't routed through ``_future_version_unrenderable``.
        # Fill ok-shape defaults so the UI can still render empty cards.
        _fill_ok_defaults(payload)

    return cast(ExamRunDisplayPayload, payload)


def load_normalised_run(run: Any) -> ExamRunDisplayPayload:
    """Deserialise + normalise a stored ``ExamTimetableRun`` in one call.

    This is the **only** approved read path for ``run.result_json``.
    Templates, views, XLSX exporters, management commands, and tests all
    go through this helper — never through ``json.loads(run.result_json)``
    directly. That single-read-path invariant is what makes the schema
    contract honest.

    ``run`` is typed ``Any`` rather than ``ExamTimetableRun`` to keep the
    schema module free of model imports (avoiding any risk of a circular
    import with ``core.services.exam_timetable`` or migrations). The
    duck-typed contract: ``run`` must have a ``result_json`` attribute
    holding either a JSON string or ``None``.
    """
    raw_json = getattr(run, "result_json", None)
    if not raw_json:
        return _unrenderable("result_json is empty or missing")
    try:
        decoded = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _unrenderable(f"result_json is not valid JSON: {exc}")
    return normalise_exam_run_payload(decoded)


def stamp_schema_version(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``schema_version`` and infer ``status`` on a freshly-built dict.

    Used at the build site (``build_exam_timetable``) so every newly
    persisted payload is at the current schema version with a valid
    discriminator. Returns the same dict mutated in-place plus a
    convenience return value, so callers can write::

        result = stamp_schema_version({...})

    or::

        stamp_schema_version(result)
        ExamTimetableRun.objects.create(result_json=json.dumps(result))

    interchangeably.
    """
    payload["schema_version"] = EXAM_RUN_SCHEMA_VERSION
    if "status" not in payload:
        payload["status"] = "feasibility_error" if payload.get("feasibility_error") else "ok"
    return payload
