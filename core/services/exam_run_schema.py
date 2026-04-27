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

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict, cast

# ---------------------------------------------------------------------------
# Version constant
# ---------------------------------------------------------------------------

EXAM_RUN_SCHEMA_VERSION: int = 3
"""Current schema version for ``ExamTimetableRun.result_json`` payloads.

Bump this whenever you add a key the UI or XLSX exporter will read but
older rows lack. Always pair the bump with a migrator function appended
to ``_MIGRATORS`` and a regression test.

Version history
---------------
- v1 (initial): ``schema_version`` + ``status`` discriminator. Defined
  the contract: every payload identifies its schema version, every
  consumer reads through ``load_normalised_run`` / ``normalise_exam_run_payload``.
- v2: registrar-facing status + multi-sitting tile.
  ``primary_status`` / ``status_flags`` / ``status_derivation_version``
  derived from the QA dict and run state; ``qa.multi_sitting_sections``
  + ``qa.multi_sitting_details`` surface the historically-invisible
  "this section needs N sittings" fact. Both are derivable from older
  v1 data so the v1->v2 migrator fills them on the read path; new
  builds populate them at write time.
- v3: telemetry block. ``qa.building_footprint`` captures
  buildings-per-slot, buildings-per-gender-per-slot, max-rooms-per-
  building-per-slot, cross-building clusters per dept, and a
  largest-slot footprint summary string. ``qa.enrolment_snapshot``
  captures snapshot_timestamp, students/courses/sections counts,
  fallback flag, synthetic-ALL count, source_hash. Both are
  display-only telemetry — they do NOT enter any ranking or
  scheduler decision. Old rows cannot reconstruct telemetry, so the
  v2->v3 migrator fills empty defaults; new builds populate at
  write time.
"""


# ---------------------------------------------------------------------------
# Display payload type
# ---------------------------------------------------------------------------

ExamRunPrimaryStatus = Literal[
    "clean",
    "clean_with_approved_thin_conflicts",
    "requires_room_action",
    "contains_overflow",
    "contains_manual_override",
    "infeasible",
    "unrenderable",
    "future_version_unrenderable",
]
"""Severity-ordered headline status for an exam-timetable run.

This is the **headline** — what the UI badge and the run-list filter
use. It is NOT exhaustive: several states can coexist (a run can have
both overflow AND unassigned rooms AND manual overrides AND multi-
sitting sections), and collapsing those into a single exclusive enum
would visually hide the lower-priority issues. The exhaustive list
is on ``status_flags`` (below).

Severity order, worst first (the derivation walks this list and picks
the first applicable):

1. ``"future_version_unrenderable"``: payload from a newer build.
2. ``"unrenderable"``: corrupt / missing / non-dict input.
3. ``"infeasible"``: feasibility pre-check failed; scheduler didn't run.
4. ``"contains_overflow"``: at least one course on the OVERFLOW day.
5. ``"requires_room_action"``: at least one section ``UNASSIGNED`` for
   a room, OR at least one multi-sitting section with incomplete
   sitting data (missing slot/room/student allocation).
6. ``"contains_manual_override"``: ``qa.manual_override_count > 0``
   (registrar pinned overrides created same-slot conflicts).
7. ``"clean_with_approved_thin_conflicts"``: realised thin clashes
   exist but no other issues.
8. ``"clean"``: no flags raised.

Multi-sitting alone does NOT promote a run to ``requires_room_action``:
multi-sitting is a legitimate capacity-resolution path when every
sitting has a room, a slot, and an audit-text record. It only triggers
``requires_room_action`` when a sitting is incomplete.
"""


class BuildingFootprintTelemetry(TypedDict, total=False):
    """v3: building/floor utilisation telemetry — display-only.

    Every metric is derived from the assign_rooms output at write
    time. Telemetry never enters scheduler placement, multistart
    Pareto ranking, or any decision logic — it surfaces operational
    pain so the registrar can decide whether building affinity is
    worth promoting to a real objective later.

    All metrics default to empty (``{}`` / ``""``) on legacy rows
    migrated to v3 because we cannot reconstruct slot-level
    building usage from the historic data. The registrar UI
    renders empty-state copy for those rows.
    """

    buildings_used_per_slot: dict[str, list[str]]
    """``slot_key -> sorted list of distinct building names``."""

    buildings_used_per_gender_per_slot: dict[str, dict[str, list[str]]]
    """``slot_key -> {"M": [...], "F": [...]}`` distinct buildings."""

    max_rooms_per_building_per_slot: dict[str, int]
    """``slot_key -> the max number of rooms used in any one building``."""

    cross_building_clusters_per_dept: dict[str, int]
    """``department -> count of slots where that dept's exams span >=2 buildings``."""

    largest_slot_footprint_summary: str
    """Human-readable headline, e.g. ``"Tuesday P2 uses 6 rooms across 4 buildings"``."""


class EnrolmentSnapshotTelemetry(TypedDict, total=False):
    """v3: integrity record for the enrolment data the build read.

    Lets the registrar answer "why does this exported schedule
    differ from what I expected?" by comparing the snapshot against
    the live data at any time. ``source_hash`` is sha256 of a
    canonical (sorted) JSON serialisation of the enrolled-sets dict;
    two builds against identical input produce the same hash.

    Defaults to empty/zero on legacy rows where the build did not
    record a snapshot.
    """

    snapshot_timestamp: str
    """ISO-8601 UTC of the moment the build read enrolment input."""

    students_count: int
    """Distinct student-ids across all enrolled-sets."""

    courses_count: int
    """Number of courses in the enrolment input."""

    sections_count: int
    """Number of (course, section) pairs in the section_enrollment dict."""

    fallback_used: bool
    """True when the build path used StudentCourse.status='studying'
    fallback because StudentTermSection had no rows for the term."""

    synthetic_all_sections_count: int
    """When ``fallback_used`` is True the fallback path emits a
    synthetic ``"ALL"`` section per course; this is that count.
    Zero on the canonical TermSection-driven path."""

    source_hash: str
    """sha256 hex of the canonical enrolment input. 64 chars."""


RegistrarStatusFlag = Literal[
    "approved_thin_conflicts",
    "room_action_required",
    "overflow",
    "manual_override",
    "multi_sitting_required",
    "legacy_incomplete_qa",
]
"""Non-exclusive flags for everything the registrar should know about.

A run can carry any combination of these. The UI shows them as a row
of badges next to the primary-status headline; that way no signal is
lost just because a more severe one took the headline.

- ``approved_thin_conflicts``: ``qa.thin_clash_risk`` non-empty.
- ``room_action_required``: at least one section UNASSIGNED for a
  room OR at least one multi-sitting section is incomplete.
- ``overflow``: at least one OVERFLOW-day schedule entry.
- ``manual_override``: ``qa.manual_override_count > 0`` (or, for
  legacy v1 rows lacking that field, inferred from the existing
  ``qa.conflict_count``; ``legacy_incomplete_qa`` is also raised in
  that case so the registrar knows the signal is approximate).
- ``multi_sitting_required``: ``qa.multi_sitting_sections > 0``.
  Distinct from ``room_action_required``: a complete multi-sitting
  with all rooms+slots+student-allocation is a valid registrar plan,
  not a defect.
- ``legacy_incomplete_qa``: the payload's QA dict lacks one or more
  keys this version of the derivation expects (e.g. v1 rows missing
  ``qa.manual_override_count``). The status is computed best-effort
  with safe defaults, but the registrar is told the signal is partial.
"""

# Backwards-compatible aliases retained for any consumer that imported
# pre-rename names. Internally everything uses ``ExamRunPrimaryStatus``.
# ``RegistrarPrimaryStatus`` was the v2 working name; ``RegistrarRunStatus``
# was the pre-flag-system enum name.
RegistrarPrimaryStatus = ExamRunPrimaryStatus
RegistrarRunStatus = ExamRunPrimaryStatus

STATUS_DERIVATION_VERSION: int = 1
"""Version of the rules used to derive ``primary_status`` and
``status_flags`` from a payload. Bump when the rules change so a
consumer can detect that an older row's status was computed under a
different policy. Independent of ``EXAM_RUN_SCHEMA_VERSION`` — payload
shape can stay stable while the derivation rules evolve.
"""


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
    # v2: registrar-facing health signals (derived). Always present.
    primary_status: ExamRunPrimaryStatus
    status_flags: list[RegistrarStatusFlag]
    status_derivation_version: int

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


def _migrate_v2_to_v3(payload: dict[str, Any]) -> dict[str, Any]:
    """v2 -> v3: fill empty telemetry defaults for legacy rows.

    Telemetry (building footprint + enrolment snapshot) cannot be
    reconstructed from data the older build did not record (no
    timestamp, no per-room building lookup, no canonical input
    hash). The migrator fills empty/zero defaults so the UI cards
    render empty-state copy gracefully; new builds populate the
    real values at write time via ``derive_building_footprint`` /
    ``compute_enrolment_snapshot``.
    """
    payload.setdefault("schema_version", 3)
    qa = payload.get("qa")
    if isinstance(qa, dict):
        qa.setdefault("building_footprint", _empty_building_footprint())
        qa.setdefault("enrolment_snapshot", _empty_enrolment_snapshot())
    return payload


def _empty_building_footprint() -> dict[str, Any]:
    return {
        "buildings_used_per_slot": {},
        "buildings_used_per_gender_per_slot": {},
        "max_rooms_per_building_per_slot": {},
        "cross_building_clusters_per_dept": {},
        "largest_slot_footprint_summary": "",
    }


def _empty_enrolment_snapshot() -> dict[str, Any]:
    return {
        "snapshot_timestamp": "",
        "students_count": 0,
        "courses_count": 0,
        "sections_count": 0,
        "fallback_used": False,
        "synthetic_all_sections_count": 0,
        "source_hash": "",
    }


def _migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: derive registrar status surface + multi-sitting tile.

    Adds ``primary_status``, ``status_flags``, ``status_derivation_version``,
    ``qa.multi_sitting_sections``, and ``qa.multi_sitting_details``. All
    derivable from existing v1 data:

    - ``overflow``: from schedule entries with ``day == "OVERFLOW"``
    - ``room_action_required``: from ``qa.unassigned_room_sections``
      and incomplete-multi-sitting detection
    - ``manual_override``: from ``qa.manual_override_count`` if present,
      else inferred best-effort from ``qa.conflict_count``. The
      derivation always raises ``legacy_incomplete_qa`` for migrated
      rows because they could not have authored the v2 fields at write
      time (they're filled here from inference, not authoritative
      measurement).
    - ``approved_thin_conflicts``: from ``qa.thin_clash_risk``
    - ``multi_sitting_required``: from assign_rooms entries with
      split-section markers (sections containing ``/`` from
      ``_split_oversized_sections``)

    Migrator runs at read time for legacy v1 rows. New v2 builds
    populate these directly at write time so the migrator is a no-op
    on the happy path. The migrator does not strip or rename any v1
    key (forward-compat by construction).
    """
    # Capture source version BEFORE stamping so derive_status_surface
    # can mark the row legacy_incomplete_qa appropriately.
    raw_source_version = payload.get("schema_version")
    if isinstance(raw_source_version, int) and raw_source_version >= 0:
        source_version = raw_source_version
    else:
        source_version = 0

    payload.setdefault("schema_version", 2)
    qa = payload.get("qa")

    # Multi-sitting tile derivation, before status flags so the flags
    # function sees the populated count. Reads from ``payload["schedule"]``
    # — the per-entry ``rooms`` list is what carries the section labels
    # we detect splits from. ``payload["assign_rooms"]`` is a config
    # bool, NOT the per-slot data (a long-standing naming quirk).
    if isinstance(qa, dict):
        if "multi_sitting_sections" not in qa or "multi_sitting_details" not in qa:
            schedule = payload.get("schedule")
            details = derive_multi_sitting_details(schedule)
            qa.setdefault("multi_sitting_sections", len(details))
            qa.setdefault("multi_sitting_details", details)

    # Status surface derivation. Pass the original source version so
    # the legacy_incomplete_qa flag can fire on schema-based criteria,
    # not just on missing-field fallback inference.
    if (
        "primary_status" not in payload
        or "status_flags" not in payload
        or "status_derivation_version" not in payload
    ):
        primary, flags = derive_status_surface(payload, source_schema_version=source_version)
        payload.setdefault("primary_status", primary)
        payload.setdefault("status_flags", flags)
        payload.setdefault("status_derivation_version", STATUS_DERIVATION_VERSION)

    return payload


_MIGRATORS: list[Migrator] = [
    _migrate_v0_to_v1,
    _migrate_v1_to_v2,
    _migrate_v2_to_v3,
    # _migrate_v3_to_v4 goes here when we bump to v4.
]


# ---------------------------------------------------------------------------
# Status-surface derivation (used by both the v1->v2 migrator and the
# build site at write time).
# ---------------------------------------------------------------------------


def _normalise_room_input(input_: Any) -> dict[str, list[dict[str, Any]]]:
    """Accept either a schedule list or a pre-grouped dict.

    Returns a ``{slot_key: [{"course_code": ..., "rooms": [...]}, ...]}``
    dict regardless of input form. Callers can pass:

    - A v3 schedule list (each entry has ``day``, ``period``,
      ``course_code``, ``rooms``) — typical production case.
    - A pre-grouped dict — legacy/test-fixture form.

    Anything else returns ``{}`` (defensive).
    """
    if isinstance(input_, dict):
        return input_
    if isinstance(input_, list):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in input_:
            if not isinstance(entry, dict):
                continue
            day = str(entry.get("day", ""))
            period = str(entry.get("period", ""))
            slot_key = f"{day}:{period}" if day or period else ""
            grouped[slot_key].append(
                {
                    "course_code": entry.get("course_code", ""),
                    "rooms": entry.get("rooms", []) or [],
                }
            )
        return dict(grouped)
    return {}


def derive_multi_sitting_details(
    schedule_or_assign_rooms: Any,
) -> list[dict[str, Any]]:
    """Extract multi-sitting sections from a schedule list or pre-grouped dict.

    A section is considered "multi-sitting" when its room-assignment
    output contains entries whose ``section`` name carries a split
    marker (``"/"`` for oversize-split, ``"a"``/``"b"`` for halve-split,
    or non-empty ``_split_from`` field). For each such logical section
    we emit one detail entry summarising:

    - ``section``: the original section label.
    - ``enrolment``: total student count across all sittings.
    - ``max_room_cap``: the largest room capacity encountered for any
      sub-sitting (informational; helps the registrar see why splitting
      was necessary).
    - ``sittings``: count of sub-entries this logical section produced.
    - ``slots``: list of slot keys (``"day:period"``) the sub-sittings
      occupy. Same slot repeated = within-slot room split. Different
      slots = across-slot multi-sitting.
    - ``rooms``: list of assigned room codes.
    - ``incomplete``: True when at least one sub-sitting is missing a
      room or slot (drives ``room_action_required``).
    - ``audit_text``: human-readable summary suitable for the QA tile.

    Defensive: an unexpected input shape returns ``[]`` rather than
    raising — partial schedules should not break the status derivation.
    """
    by_slot = _normalise_room_input(schedule_or_assign_rooms)
    if not by_slot:
        return []

    by_logical: dict[str, dict[str, Any]] = {}

    # ``by_slot`` is structured by slot. Each slot has a list of
    # course entries; each entry has its own ``rooms`` list with
    # per-section sub-rows. The exact shape depends on the build site,
    # but the relevant invariants are: there's a slot key, and each
    # course's entry has a ``rooms`` list of dicts. Walk defensively.
    for slot_key, slot_entries in by_slot.items():
        if not isinstance(slot_entries, list):
            continue
        for entry in slot_entries:
            if not isinstance(entry, dict):
                continue
            rooms = entry.get("rooms")
            if not isinstance(rooms, list):
                continue
            for r in rooms:
                if not isinstance(r, dict):
                    continue
                section_label = str(r.get("section", ""))
                split_from = r.get("_split_from")
                # A section qualifies as multi-sitting if either:
                # (a) its name carries the "/" or trailing "a"/"b"
                #     split marker introduced by _split_oversized_sections
                #     or the in-place halver, OR
                # (b) it has a non-empty ``_split_from`` field.
                logical: str | None = None
                if isinstance(split_from, str) and split_from:
                    logical = split_from
                elif "/" in section_label:
                    logical = section_label.rsplit("/", 1)[0]
                elif section_label and section_label[-1] in ("a", "b") and len(section_label) > 1:
                    # Heuristic — only treat as split if there's another
                    # entry with the trailing-stripped name. We resolve
                    # this in the second pass below.
                    pass
                if logical is None:
                    continue
                bucket = by_logical.setdefault(
                    logical,
                    {
                        "section": logical,
                        "enrolment": 0,
                        "max_room_cap": 0,
                        "sittings": 0,
                        "slots": [],
                        "rooms": [],
                        "incomplete": False,
                    },
                )
                bucket["enrolment"] += int(r.get("student_count", 0) or 0)
                bucket["max_room_cap"] = max(
                    bucket["max_room_cap"],
                    int(r.get("room_capacity", 0) or 0),
                )
                bucket["sittings"] += 1
                bucket["slots"].append(str(slot_key))
                room_code = str(r.get("room_code", ""))
                bucket["rooms"].append(room_code)
                if room_code in ("", "UNASSIGNED"):
                    bucket["incomplete"] = True
                if not slot_key:
                    bucket["incomplete"] = True

    out: list[dict[str, Any]] = []
    for detail in by_logical.values():
        if detail["sittings"] < 2:
            # A single sub-entry isn't really a multi-sitting — likely
            # caught by our heuristic above on a section that happens
            # to end in "a"/"b" without a sibling.
            continue
        slot_summary = ", ".join(detail["slots"])
        room_summary = ", ".join(detail["rooms"])
        detail["audit_text"] = (
            f"Section {detail['section']} requires "
            f"{detail['sittings']} sittings "
            f"({detail['enrolment']} students, "
            f"slots: {slot_summary}, rooms: {room_summary})"
        )
        if detail["incomplete"]:
            detail["audit_text"] += " — INCOMPLETE: missing room or slot data"
        out.append(detail)
    out.sort(key=lambda d: d["section"])
    return out


def derive_building_footprint(
    schedule_or_assign_rooms: Any,
    *,
    department_of: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Compute the v3 building-footprint telemetry block.

    Accepts either a schedule list (each entry having ``day``,
    ``period``, ``course_code``, ``rooms``) or a pre-grouped
    ``{slot_key: [course_entry, ...]}`` dict.

    Defensive: returns ``_empty_building_footprint()`` when the input
    structure is unexpected. Pure: no DB access, no I/O.

    Each room entry in ``rooms`` is expected to carry ``room_code``,
    ``gender`` and (after v3 build enrichment) ``building`` fields.
    Entries lacking ``building`` are skipped — the metric reflects only
    rooms whose building is known. Legacy rows (which never recorded
    ``building`` per entry) therefore get empty metrics, matching the
    "telemetry cannot be reconstructed" rule.

    ``department_of`` defaults to splitting the course code at the
    first run of digits ("CS101" -> "CS"). Override for institutions
    with a different prefix convention.

    Returns the dict shape declared by ``BuildingFootprintTelemetry``.
    """
    by_slot = _normalise_room_input(schedule_or_assign_rooms)
    if not by_slot:
        return _empty_building_footprint()

    if department_of is None:
        department_of = _default_department_of

    buildings_per_slot: dict[str, set[str]] = defaultdict(set)
    buildings_per_gender_per_slot: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"M": set(), "F": set(), "": set()}
    )
    rooms_per_building_per_slot: dict[str, Counter[str]] = defaultdict(Counter)
    rooms_per_slot: Counter[str] = Counter()
    # dept -> set of slot_keys where dept's exams span >=2 buildings
    dept_cross_building_slots: dict[str, set[str]] = defaultdict(set)

    for slot_key, slot_entries in by_slot.items():
        if not isinstance(slot_entries, list):
            continue
        slot_str = str(slot_key)
        # dept -> set of buildings used by that dept in this slot
        dept_buildings_this_slot: dict[str, set[str]] = defaultdict(set)

        for entry in slot_entries:
            if not isinstance(entry, dict):
                continue
            course_code = str(entry.get("course_code", ""))
            dept = department_of(course_code) if course_code else ""

            rooms = entry.get("rooms")
            if not isinstance(rooms, list):
                continue
            for r in rooms:
                if not isinstance(r, dict):
                    continue
                building = r.get("building")
                if not isinstance(building, str) or not building:
                    continue
                # Skip the UNASSIGNED sentinel for footprint purposes —
                # it isn't a real building.
                if str(r.get("room_code", "")).upper() == "UNASSIGNED":
                    continue
                gender = str(r.get("gender", "")).upper()
                if gender not in ("M", "F"):
                    gender = ""

                buildings_per_slot[slot_str].add(building)
                buildings_per_gender_per_slot[slot_str][gender].add(building)
                rooms_per_building_per_slot[slot_str][building] += 1
                rooms_per_slot[slot_str] += 1
                if dept:
                    dept_buildings_this_slot[dept].add(building)

        for dept, buildings in dept_buildings_this_slot.items():
            if len(buildings) >= 2:
                dept_cross_building_slots[dept].add(slot_str)

    # Build the largest-slot summary deterministically: pick the slot
    # whose room count is highest; tie-break by slot key alphabetically.
    largest_slot_summary = ""
    if rooms_per_slot:
        best_slot, best_count = max(
            rooms_per_slot.items(),
            key=lambda kv: (kv[1], -ord_summary(kv[0])),
        )
        building_count = len(buildings_per_slot.get(best_slot, set()))
        largest_slot_summary = (
            f"{best_slot} uses {best_count} rooms across "
            f"{building_count} building" + ("s" if building_count != 1 else "")
        )

    return {
        "buildings_used_per_slot": {
            slot: sorted(blds) for slot, blds in buildings_per_slot.items()
        },
        "buildings_used_per_gender_per_slot": {
            slot: {g: sorted(b) for g, b in genders.items() if b}
            for slot, genders in buildings_per_gender_per_slot.items()
        },
        "max_rooms_per_building_per_slot": {
            slot: max(counter.values()) if counter else 0
            for slot, counter in rooms_per_building_per_slot.items()
        },
        "cross_building_clusters_per_dept": {
            dept: len(slots) for dept, slots in dept_cross_building_slots.items()
        },
        "largest_slot_footprint_summary": largest_slot_summary,
    }


def ord_summary(slot_key: str) -> int:
    """Tie-breaker helper: sum of ord codes for deterministic ordering.

    Pure helper for ``derive_building_footprint``. We want stable
    output across Python versions; ``max()`` with a numeric key
    plus a small-but-deterministic secondary key gives that.
    """
    return sum(ord(c) for c in slot_key)


def _default_department_of(course_code: str) -> str:
    """Split a course code at the first digit run.

    "CS101" -> "CS"; "MATH200" -> "MATH"; "GS-101" -> "GS-".

    Pure string manipulation; no domain knowledge of which prefixes
    are "departmental" vs "general studies" etc. The footprint
    function uses this only for cross-building-cluster grouping; a
    stricter mapping can be passed via ``department_of=...``.
    """
    for i, ch in enumerate(course_code):
        if ch.isdigit():
            return course_code[:i]
    return course_code


def compute_enrolment_snapshot(
    enrolled_sets: dict[str, Any],
    *,
    sections_count: int = 0,
    fallback_used: bool = False,
    synthetic_all_sections_count: int = 0,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Compute the v3 enrolment-snapshot telemetry block.

    Pure (modulo ``datetime.now`` when ``timestamp`` is omitted).
    The ``source_hash`` is sha256 of a canonical JSON serialisation
    of ``enrolled_sets`` (course codes sorted, student id lists
    sorted) — two builds against the same input produce the same
    hash.

    ``students_count`` is the count of distinct student ids across
    all enrolled sets. ``courses_count`` is ``len(enrolled_sets)``.
    ``sections_count`` is provided by the caller because section
    bookkeeping lives outside ``enrolled_sets`` in this codebase.

    ``fallback_used`` and ``synthetic_all_sections_count`` are
    plumbed through verbatim — the schema function does not
    second-guess the build site about which path produced the data.
    """
    if not isinstance(enrolled_sets, dict):
        enrolled_sets = {}

    distinct_students: set[Any] = set()
    canonical: dict[str, list[Any]] = {}
    for course_code in sorted(enrolled_sets.keys()):
        sids = enrolled_sets[course_code]
        try:
            sid_list = sorted(sids)
        except TypeError:
            # Heterogeneous types in the set — fall back to string-sorted
            # so the canonical form stays deterministic.
            sid_list = sorted(sids, key=lambda s: str(s))
        canonical[course_code] = sid_list
        distinct_students.update(sid_list)

    canonical_text = json.dumps(canonical, ensure_ascii=False, sort_keys=False)
    source_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()

    if timestamp is None:
        timestamp = datetime.now(UTC)

    return {
        "snapshot_timestamp": timestamp.isoformat(),
        "students_count": len(distinct_students),
        "courses_count": len(enrolled_sets),
        "sections_count": int(sections_count),
        "fallback_used": bool(fallback_used),
        "synthetic_all_sections_count": int(synthetic_all_sections_count),
        "source_hash": source_hash,
    }


def derive_status_surface(
    payload: dict[str, Any],
    source_schema_version: int | None = None,
) -> tuple[ExamRunPrimaryStatus, list[RegistrarStatusFlag]]:
    """Compute ``(primary_status, status_flags)`` for a payload.

    Uses ``payload["status"]`` (technical) plus the QA dict to derive
    the registrar-facing surface. Returns the primary status (the
    headline) and the full flags list (everything the registrar should
    know, regardless of priority).

    Parameters
    ----------
    payload
        The (already-migrated, possibly stamped) payload dict. The
        function reads ``status``, ``schedule``, and ``qa`` keys.
    source_schema_version
        The ORIGINAL ``schema_version`` of the payload BEFORE any
        migration ran. Used to set ``legacy_incomplete_qa`` whenever
        the payload arrived from a schema version older than current
        (because a derivation can be "complete" only relative to the
        rules of the schema that wrote it). Pass ``None`` for
        write-time callers that built the payload at the current
        version directly.

    The function is defensive: missing keys default to safe values.
    The ``legacy_incomplete_qa`` flag fires when ANY of:

    - ``source_schema_version < EXAM_RUN_SCHEMA_VERSION`` (the row was
      written under an older schema and could not have had every
      derivation field populated authoritatively at write time);
    - critical derivation fields are missing (e.g. ``qa`` is missing
      altogether, or ``qa.manual_override_count`` is absent);
    - we had to fall back to inferring a signal (e.g. inferring manual
      override count from ``qa.conflict_count``).

    This matches the peer-review-confirmed semantic: the flag tells the
    registrar the difference between *zero observed* and *not measured
    in this schema*.
    """
    status = payload.get("status")

    # Technical sentinels short-circuit: a payload that can't be
    # rendered cannot have meaningful registrar flags beyond its own
    # unrenderable / future-version state.
    if status == "future_version_unrenderable":
        return ("future_version_unrenderable", [])
    if status == "unrenderable":
        return ("unrenderable", [])
    if status == "feasibility_error":
        return ("infeasible", [])

    # ── Compute non-exclusive flags from QA + schedule ──────────────────
    flags: list[RegistrarStatusFlag] = []
    legacy_incomplete = False

    # Schema-based legacy trigger: any payload whose source schema is
    # older than current is by definition incomplete relative to the
    # current derivation rules. The migrator may fill defaults, but
    # those defaults are not authoritative measurements.
    if source_schema_version is not None and source_schema_version < EXAM_RUN_SCHEMA_VERSION:
        legacy_incomplete = True

    qa_raw = payload.get("qa")
    if isinstance(qa_raw, dict):
        qa = qa_raw
    else:
        qa = {}
        # No QA at all means we cannot measure most of the derivation
        # signals — degrade to legacy-incomplete rather than pretending
        # the run is "clean".
        legacy_incomplete = True
    schedule = payload.get("schedule") if isinstance(payload.get("schedule"), list) else []

    overflow_count = sum(1 for e in schedule if isinstance(e, dict) and e.get("day") == "OVERFLOW")
    if overflow_count > 0:
        flags.append("overflow")

    unassigned_rooms = int(qa.get("unassigned_room_sections", 0) or 0)
    multi_sitting_count = int(qa.get("multi_sitting_sections", 0) or 0)
    multi_sitting_details = qa.get("multi_sitting_details") or []
    incomplete_sittings = sum(
        1 for d in multi_sitting_details if isinstance(d, dict) and d.get("incomplete")
    )

    if multi_sitting_count > 0:
        flags.append("multi_sitting_required")

    # Multi-sitting alone is a legitimate plan; only promote it to
    # room_action_required when the multi-sitting itself is incomplete
    # OR when we have outright UNASSIGNED rooms.
    if unassigned_rooms > 0 or incomplete_sittings > 0:
        flags.append("room_action_required")

    # Manual override: prefer the explicit qa.manual_override_count
    # signal added in v2 builds. Fall back to qa.conflict_count for
    # legacy v1 rows (with legacy_incomplete_qa flag raised — the
    # source-schema check above usually catches this already, but the
    # fallback also fires when a v2-claimed row arrived missing the
    # field for any reason, which we still treat as partial).
    if "manual_override_count" in qa:
        manual_override_count = int(qa.get("manual_override_count", 0) or 0)
    else:
        manual_override_count = int(qa.get("conflict_count", 0) or 0)
        legacy_incomplete = True
    if manual_override_count > 0:
        flags.append("manual_override")

    thin_clash_risk = qa.get("thin_clash_risk") or []
    if isinstance(thin_clash_risk, list) and thin_clash_risk:
        flags.append("approved_thin_conflicts")

    if legacy_incomplete:
        flags.append("legacy_incomplete_qa")

    # ── Pick the headline ───────────────────────────────────────────────
    # Severity order, worst first. The first applicable wins.
    if overflow_count > 0:
        primary: ExamRunPrimaryStatus = "contains_overflow"
    elif unassigned_rooms > 0 or incomplete_sittings > 0:
        primary = "requires_room_action"
    elif manual_override_count > 0:
        primary = "contains_manual_override"
    elif flags and "approved_thin_conflicts" in flags:
        primary = "clean_with_approved_thin_conflicts"
    else:
        primary = "clean"

    return (primary, flags)


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
    # v2 status surface defaults so consumers never KeyError when
    # reading the headline or flag list. The actual derivation runs in
    # the v1->v2 migrator (or at write time for fresh v2 builds);
    # these defaults only fire if a caller hand-crafts a payload
    # without status data — the surface stays consistent.
    payload.setdefault("primary_status", "clean")
    payload.setdefault("status_flags", [])
    payload.setdefault("status_derivation_version", STATUS_DERIVATION_VERSION)
    qa_dict = payload.get("qa")
    if isinstance(qa_dict, dict):
        qa_dict.setdefault("multi_sitting_sections", 0)
        qa_dict.setdefault("multi_sitting_details", [])
        # v3: telemetry blocks. Defaults are empty so UI iterates
        # without crashing on rows older than v3 (or fresh rows
        # whose build site was somehow unable to compute them).
        qa_dict.setdefault("building_footprint", _empty_building_footprint())
        qa_dict.setdefault("enrolment_snapshot", _empty_enrolment_snapshot())


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
        "primary_status": "unrenderable",
        "status_flags": [],
        "status_derivation_version": STATUS_DERIVATION_VERSION,
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
        "primary_status": "future_version_unrenderable",
        "status_flags": [],
        "status_derivation_version": STATUS_DERIVATION_VERSION,
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
