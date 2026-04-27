"""Tests for ``core.services.exam_run_schema`` — the lifecycle prerequisite.

These tests are the contract for every future schema-version bump:
identity, idempotency, defensive parsing, forward-compat, and the
discriminator inference that lets v0 (legacy unversioned) payloads
upgrade cleanly.

Test inventory
--------------
- ``test_identity_v1`` — a fully-formed v1 payload normalises to itself.
- ``test_v0_legacy_ok_payload_migrates_to_v1`` — a payload with no
  ``schema_version`` and no ``status`` but a real ``schedule`` migrates
  to v1 with ``status="ok"``.
- ``test_v0_legacy_feasibility_error_migrates`` — a v0 ``feasibility_error``
  payload gets ``status="feasibility_error"``.
- ``test_idempotent`` — ``normalise(normalise(x)) == normalise(x)``.
- ``test_none_input_returns_unrenderable_sentinel`` — defensive.
- ``test_non_dict_input_returns_unrenderable_sentinel`` — defensive.
- ``test_corrupt_json_via_load_normalised_run`` — defensive at the disk boundary.
- ``test_empty_result_json_returns_unrenderable`` — defensive.
- ``test_forward_compat_unknown_keys_preserved`` — future v2 keys survive.
- ``test_forward_compat_higher_schema_version_preserved`` — v2-claiming
  payload doesn't get downgraded by us.
- ``test_schema_version_always_present`` — across every input type.
- ``test_status_always_valid`` — across every input type.
- ``test_ok_defaults_safe_for_iteration`` — every list/dict default is
  the right type so ``len()`` / iteration / indexing never crash.
- ``test_feasibility_error_defaults_include_ok_keys`` — feasibility-error
  payloads still have ``schedule=[]`` etc. so consumers don't branch.
- ``test_unrenderable_sentinel_includes_ok_keys`` — same for the sentinel.
- ``test_stamp_schema_version_idempotent_on_caller_supplied_status``.
- ``test_load_normalised_run_with_real_built_payload`` — round-trip through
  json.dumps -> store -> load_normalised_run -> compare semantically.
- ``test_management_command_dry_run`` — reports counts, doesn't write.
- ``test_management_command_migrates_legacy_rows`` — actually rewrites v0
  rows to v1.
- ``test_management_command_idempotent`` — second run is a no-op.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from core.models import ExamTimetableRun
from core.services.exam_run_schema import (
    EXAM_RUN_SCHEMA_VERSION,
    STATUS_DERIVATION_VERSION,
    ExamRunDisplayPayload,
    derive_multi_sitting_details,
    derive_status_surface,
    load_normalised_run,
    normalise_exam_run_payload,
    stamp_schema_version,
)

# ---------------------------------------------------------------------------
# Pure normaliser tests (no DB)
# ---------------------------------------------------------------------------


def _v1_ok_payload() -> dict:
    """A canonical v1 ok-shape payload (no v2 status surface keys).

    Used to test the v1->v2 migration path.
    """
    return {
        "schema_version": 1,
        "status": "ok",
        "students_count": 42,
        "courses": ["CS101", "CS102"],
        "courses_count": 2,
        "conflicts": [],
        "conflicts_count": 0,
        "slots": [{"index": 0, "day": "SUN", "period": "P1"}],
        "schedule": [{"course_code": "CS101", "slot_index": 0, "day": "SUN", "period": "P1"}],
        "qa": {"total_courses": 2},
        "buckets_summary": [],
        "bucket_count": 1,
        "credit_map": {"CS101": 3, "CS102": 3},
        "seed": 42,
        "section_enrollment": {},
        "rooms_count": 0,
        "assign_rooms": {},
    }


def _v2_ok_payload() -> dict:
    """A canonical v2 ok-shape payload — already at current schema.

    Mirrors what the build site produces: every v2 derivation field is
    populated explicitly (including ``qa.manual_override_count = 0``)
    so the status derivation does not fall back to legacy inference.
    """
    return {
        "schema_version": 2,
        "status": "ok",
        "primary_status": "clean",
        "status_flags": [],
        "status_derivation_version": 1,
        "students_count": 42,
        "courses": ["CS101", "CS102"],
        "courses_count": 2,
        "conflicts": [],
        "conflicts_count": 0,
        "slots": [{"index": 0, "day": "SUN", "period": "P1"}],
        "schedule": [{"course_code": "CS101", "slot_index": 0, "day": "SUN", "period": "P1"}],
        "qa": {
            "total_courses": 2,
            "multi_sitting_sections": 0,
            "multi_sitting_details": [],
            "manual_override_count": 0,
            "manual_override_details": [],
        },
        "buckets_summary": [],
        "bucket_count": 1,
        "credit_map": {"CS101": 3, "CS102": 3},
        "seed": 42,
        "section_enrollment": {},
        "rooms_count": 0,
        "assign_rooms": {},
    }


def test_identity_v2() -> None:
    """A fully-formed v2 payload normalises to itself."""
    payload = _v2_ok_payload()
    out = normalise_exam_run_payload(payload)
    for key, value in payload.items():
        assert out[key] == value, f"key {key!r} mutated by normaliser"
    assert out["schema_version"] == 2


def test_v1_payload_migrates_to_v2() -> None:
    """A v1 payload with no status surface gets primary_status,
    status_flags, multi-sitting tile, and the version stamped to v2."""
    payload = _v1_ok_payload()
    out = normalise_exam_run_payload(payload)
    assert out["schema_version"] == 2
    assert out["status"] == "ok"
    # New v2 keys derived for legacy v1 input.
    assert "primary_status" in out
    assert "status_flags" in out
    assert "status_derivation_version" in out
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []


def test_normaliser_does_not_mutate_input() -> None:
    payload = _v2_ok_payload()
    snapshot = json.dumps(payload, sort_keys=True)
    normalise_exam_run_payload(payload)
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_v0_legacy_ok_payload_migrates_to_v1() -> None:
    legacy: dict = {
        # No schema_version, no status — pure v0 shape.
        "students_count": 10,
        "courses": ["CS101"],
        "courses_count": 1,
        "schedule": [{"course_code": "CS101", "slot_index": 0, "day": "SUN", "period": "P1"}],
        "qa": {},
    }
    out = normalise_exam_run_payload(legacy)
    assert out["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert out["status"] == "ok"
    # Real values preserved.
    assert out["students_count"] == 10
    assert out["courses"] == ["CS101"]
    # Defaults filled for missing keys.
    assert out["conflicts"] == []
    assert out["slots"] == []
    assert out["bucket_count"] == 0
    assert out["seed"] is None


def test_v0_legacy_feasibility_error_migrates() -> None:
    legacy: dict = {
        "feasibility_error": True,
        "violations": [{"bucket": "(CS, T1)", "courses": 4, "days": 3}],
        "courses_count": 12,
        "students_count": 200,
        "bucket_count": 5,
    }
    out = normalise_exam_run_payload(legacy)
    assert out["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert out["status"] == "feasibility_error"
    assert out["violations"] == [{"bucket": "(CS, T1)", "courses": 4, "days": 3}]
    # ok-shape defaults also present so consumers can index without branching.
    assert out["schedule"] == []
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []


def test_idempotent_on_v1_payload() -> None:
    payload = _v1_ok_payload()
    once = normalise_exam_run_payload(payload)
    twice = normalise_exam_run_payload(dict(once))
    assert dict(once) == dict(twice)


def test_idempotent_on_v0_payload() -> None:
    legacy = {"schedule": [], "qa": {}}
    once = normalise_exam_run_payload(legacy)
    twice = normalise_exam_run_payload(dict(once))
    assert dict(once) == dict(twice)


def test_idempotent_on_feasibility_error() -> None:
    legacy = {"feasibility_error": True, "violations": [{"x": 1}]}
    once = normalise_exam_run_payload(legacy)
    twice = normalise_exam_run_payload(dict(once))
    assert dict(once) == dict(twice)


# ---------------------------------------------------------------------------
# Defensive parsing
# ---------------------------------------------------------------------------


def test_none_input_returns_unrenderable_sentinel() -> None:
    out = normalise_exam_run_payload(None)
    assert out["status"] == "unrenderable"
    assert out["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert "error" in out
    # ok-shape defaults available so UI can render the empty-state cards.
    assert out["schedule"] == []
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []


@pytest.mark.parametrize(
    "garbage",
    [
        "string-not-dict",
        42,
        3.14,
        ["list", "not", "dict"],
        ("tuple", "not", "dict"),
        True,
    ],
)
def test_non_dict_input_returns_unrenderable_sentinel(garbage: object) -> None:
    out = normalise_exam_run_payload(garbage)
    assert out["status"] == "unrenderable"
    assert "error" in out
    assert out["schedule"] == []


def test_unrenderable_passthrough_is_idempotent() -> None:
    """A persisted unrenderable sentinel (shouldn't happen, but defensive)
    re-normalises to a valid unrenderable shape."""
    out = normalise_exam_run_payload({"status": "unrenderable", "error": "test"})
    assert out["status"] == "unrenderable"
    twice = normalise_exam_run_payload(dict(out))
    assert dict(out) == dict(twice)


# ---------------------------------------------------------------------------
# Forward-compat
# ---------------------------------------------------------------------------


def test_forward_compat_unknown_keys_preserved() -> None:
    """Keys this build does not yet know about (future v2 keys, third-party
    annotations) survive normalisation untouched."""
    payload = _v1_ok_payload()
    payload["future_v2_tile_data"] = {"summary": "from future"}
    payload["registrar_annotation"] = "approved by Dr X"
    out = normalise_exam_run_payload(payload)
    assert out["future_v2_tile_data"] == {"summary": "from future"}  # type: ignore[typeddict-item]
    assert out["registrar_annotation"] == "approved by Dr X"  # type: ignore[typeddict-item]


def test_forward_compat_higher_schema_version_preserved() -> None:
    """A payload claiming a future schema_version is not downgraded:
    we cannot run forward migrators, so we trust the higher number."""
    payload = _v1_ok_payload()
    payload["schema_version"] = 99
    payload["only_in_v99"] = "carried through"
    out = normalise_exam_run_payload(payload)
    # We don't downgrade — claimed version is preserved.
    assert out["schema_version"] >= 99
    assert out["only_in_v99"] == "carried through"  # type: ignore[typeddict-item]


def test_future_version_status_set_for_higher_schema_version() -> None:
    """Hardening: a payload at a higher schema_version is unsafe to
    render with our current consumers. Surface as an explicit
    'future_version_unrenderable' status so the UI shows a clear
    message and the XLSX exporter can refuse cleanly. The original
    payload data is preserved (forward-compat) for operator inspection,
    but the status forbids consumers from rendering it as if it were OK.
    """
    payload = _v1_ok_payload()
    payload["schema_version"] = 99
    out = normalise_exam_run_payload(payload)
    assert out["status"] == "future_version_unrenderable"
    assert "error" in out
    assert "99" in out.get("error", "")


def test_future_version_status_with_empty_input_uses_ok_defaults() -> None:
    """A future-version payload with no real data still gets safe
    defaults so any UI card that ignores the status discriminator
    can iterate without crashing."""
    out = normalise_exam_run_payload({"schema_version": 99})
    assert out["status"] == "future_version_unrenderable"
    assert out["schedule"] == []
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []


def test_future_version_preserves_original_schema_version() -> None:
    """Operators inspecting a future-version row need to see what build
    wrote it; we must NOT stamp it down to current."""
    out = normalise_exam_run_payload({"schema_version": 99})
    assert out["schema_version"] == 99


def test_future_version_preserves_unknown_keys() -> None:
    """Forward-compat: unknown keys from a future build survive
    normalisation so an operator can inspect them."""
    payload = {
        "schema_version": 99,
        "v99_only_field": {"summary": "from future"},
        "another_v99_field": [1, 2, 3],
    }
    out = normalise_exam_run_payload(payload)
    assert out["v99_only_field"] == {"summary": "from future"}  # type: ignore[typeddict-item]
    assert out["another_v99_field"] == [1, 2, 3]  # type: ignore[typeddict-item]


def test_future_version_idempotent() -> None:
    """Re-normalising a future-version payload yields the same status
    (it does NOT recover to ok just because we round-tripped it)."""
    once = normalise_exam_run_payload({"schema_version": 99, "x": 1})
    twice = normalise_exam_run_payload(dict(once))
    assert once["status"] == twice["status"] == "future_version_unrenderable"
    assert once["schema_version"] == twice["schema_version"] == 99


def test_invalid_schema_version_treated_as_legacy() -> None:
    """Strings, negatives, None — anything that isn't a non-negative int
    is treated as v0 (legacy)."""
    for bad in ["v1", -3, 3.14, None, [1]]:
        payload = {"schema_version": bad, "schedule": []}
        out = normalise_exam_run_payload(payload)
        assert out["schema_version"] == EXAM_RUN_SCHEMA_VERSION
        assert out["status"] == "ok"


# ---------------------------------------------------------------------------
# Invariants enforced across all paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_payload",
    [
        None,
        {},
        {"schema_version": 1, "status": "ok"},
        {"feasibility_error": True},
        "garbage",
        _v1_ok_payload(),
    ],
)
def test_schema_version_always_present(input_payload: object) -> None:
    out = normalise_exam_run_payload(input_payload)
    assert isinstance(out["schema_version"], int)
    assert out["schema_version"] >= EXAM_RUN_SCHEMA_VERSION


@pytest.mark.parametrize(
    "input_payload",
    [
        None,
        {},
        {"schema_version": 1, "status": "ok"},
        {"feasibility_error": True},
        {"schema_version": 99},  # future-version path
        "garbage",
        _v1_ok_payload(),
    ],
)
def test_status_always_valid(input_payload: object) -> None:
    out = normalise_exam_run_payload(input_payload)
    assert out["status"] in (
        "ok",
        "feasibility_error",
        "unrenderable",
        "future_version_unrenderable",
    )


def test_ok_defaults_safe_for_iteration() -> None:
    """Every default for an ok-shape payload is the type the UI / XLSX
    iterates over. ``len()``, iteration, and indexing must never crash."""
    out = normalise_exam_run_payload({})
    # Lists.
    for key in ("courses", "conflicts", "slots", "schedule", "buckets_summary"):
        assert isinstance(out[key], list)
        len(out[key])  # iteration safety
    # Dicts.
    for key in ("qa", "credit_map", "section_enrollment", "assign_rooms"):
        assert isinstance(out[key], dict)
    # Ints.
    for key in (
        "students_count",
        "courses_count",
        "conflicts_count",
        "bucket_count",
        "rooms_count",
    ):
        assert isinstance(out[key], int)
    # Optional.
    assert out["seed"] is None


def test_feasibility_error_defaults_include_ok_keys() -> None:
    """Feasibility-error payloads still carry empty schedule / qa / etc.
    so XLSX export and JSON serialisers don't need a status branch."""
    out = normalise_exam_run_payload({"feasibility_error": True})
    assert out["status"] == "feasibility_error"
    assert out["schedule"] == []
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []
    assert out["slots"] == []
    assert out["violations"] == []


def test_unrenderable_sentinel_includes_ok_keys() -> None:
    out = normalise_exam_run_payload(None)
    assert out["status"] == "unrenderable"
    assert out["schedule"] == []
    assert out["qa"].get("multi_sitting_sections") == 0
    assert out["qa"].get("multi_sitting_details") == []
    assert out["violations"] == []


# ---------------------------------------------------------------------------
# stamp_schema_version helper (write-side primitive)
# ---------------------------------------------------------------------------


def test_stamp_schema_version_sets_version_and_status_for_ok() -> None:
    payload: dict = {"schedule": [], "qa": {}}
    out = stamp_schema_version(payload)
    assert out["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert out["status"] == "ok"


def test_stamp_schema_version_sets_status_for_feasibility_error() -> None:
    payload: dict = {"feasibility_error": True, "violations": []}
    out = stamp_schema_version(payload)
    assert out["status"] == "feasibility_error"


def test_stamp_schema_version_does_not_overwrite_caller_status() -> None:
    """If the build site already set ``status``, ``stamp_schema_version``
    must respect it (in case future build sites use a more specific
    status, e.g. one of the run-status enum values added in step 3)."""
    payload: dict = {"status": "ok", "schedule": []}
    out = stamp_schema_version(payload)
    assert out["status"] == "ok"


def test_stamp_schema_version_returns_same_dict() -> None:
    """Returns the input mutated in-place, so callers can ignore the
    return value if they prefer."""
    payload: dict = {"schedule": []}
    out = stamp_schema_version(payload)
    assert out is payload


# ---------------------------------------------------------------------------
# load_normalised_run helper (disk boundary)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_load_normalised_run_with_real_built_payload() -> None:
    """A round-trip through json.dumps -> ExamTimetableRun -> load -> read
    produces the same semantic shape (modulo schema_version stamping)."""
    payload: dict = stamp_schema_version(
        {
            "students_count": 5,
            "courses": ["CS101"],
            "courses_count": 1,
            "conflicts": [],
            "conflicts_count": 0,
            "slots": [{"index": 0, "day": "SUN", "period": "P1"}],
            "schedule": [
                {
                    "course_code": "CS101",
                    "slot_index": 0,
                    "day": "SUN",
                    "period": "P1",
                }
            ],
            "qa": {"total_courses": 1},
            "buckets_summary": [],
            "bucket_count": 0,
            "credit_map": {"CS101": 3},
            "seed": 7,
            "section_enrollment": {},
            "rooms_count": 0,
            "assign_rooms": {},
        }
    )
    run = ExamTimetableRun.objects.create(
        label="t",
        result_json=json.dumps(payload, ensure_ascii=False),
    )
    loaded = load_normalised_run(run)
    assert loaded["status"] == "ok"
    assert loaded["students_count"] == 5
    assert loaded["schedule"] == payload["schedule"]
    assert loaded["schema_version"] == EXAM_RUN_SCHEMA_VERSION


@pytest.mark.django_db
def test_load_normalised_run_with_legacy_v0_row() -> None:
    """A row stored before this module existed (no schema_version) loads
    cleanly with status='ok' and defaults filled."""
    legacy = {
        "students_count": 3,
        "courses": [],
        "courses_count": 0,
        "schedule": [],
        # Missing: slots, qa, buckets_summary, credit_map, etc.
    }
    run = ExamTimetableRun.objects.create(
        label="legacy",
        result_json=json.dumps(legacy),
    )
    loaded = load_normalised_run(run)
    assert loaded["status"] == "ok"
    assert loaded["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    # Missing keys filled with safe defaults.
    assert loaded["slots"] == []
    assert loaded["qa"].get("multi_sitting_sections") == 0
    assert loaded["qa"].get("multi_sitting_details") == []
    assert loaded["bucket_count"] == 0


@pytest.mark.django_db
def test_load_normalised_run_with_corrupt_json() -> None:
    run = ExamTimetableRun.objects.create(
        label="corrupt",
        result_json="{not valid json}",
    )
    loaded = load_normalised_run(run)
    assert loaded["status"] == "unrenderable"
    assert loaded["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert "JSON" in loaded.get("error", "") or "json" in loaded.get("error", "")


@pytest.mark.django_db
def test_load_normalised_run_with_empty_result_json() -> None:
    run = ExamTimetableRun.objects.create(label="empty", result_json="")
    loaded = load_normalised_run(run)
    assert loaded["status"] == "unrenderable"


@pytest.mark.django_db
def test_load_normalised_run_with_default_empty_dict_string() -> None:
    """The model default is ``"{}"`` — that should normalise to a valid
    (empty) ok-shape payload, not unrenderable."""
    run = ExamTimetableRun.objects.create(label="default")
    loaded = load_normalised_run(run)
    assert loaded["status"] == "ok"
    assert loaded["schedule"] == []


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_management_command_no_rows() -> None:
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    assert "No ExamTimetableRun rows" in out.getvalue()


@pytest.mark.django_db
def test_management_command_dry_run_does_not_write() -> None:
    legacy = {"schedule": [], "qa": {}}
    run = ExamTimetableRun.objects.create(
        label="legacy",
        result_json=json.dumps(legacy),
    )
    original = run.result_json

    out = StringIO()
    call_command("normalise_exam_runs", "--dry-run", stdout=out)

    run.refresh_from_db()
    assert run.result_json == original
    output = out.getvalue()
    assert "[dry-run]" in output
    assert "Would migrate 1 row" in output


@pytest.mark.django_db
def test_management_command_migrates_legacy_row() -> None:
    legacy = {"schedule": [], "qa": {}}
    run = ExamTimetableRun.objects.create(
        label="legacy",
        result_json=json.dumps(legacy),
    )
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    run.refresh_from_db()
    payload = json.loads(run.result_json)
    assert payload["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert payload["status"] == "ok"
    assert "Migrated 1 row" in out.getvalue()


@pytest.mark.django_db
def test_management_command_idempotent() -> None:
    legacy = {"schedule": [], "qa": {}}
    ExamTimetableRun.objects.create(label="x", result_json=json.dumps(legacy))

    # First run migrates.
    call_command("normalise_exam_runs", stdout=StringIO())
    # Second run should be a no-op.
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    output = out.getvalue()
    assert "No rows needed migration" in output or "Migrated 0 row" in output


@pytest.mark.django_db
def test_management_command_reports_unrenderable_rows() -> None:
    """Corrupt JSON rows are reported but never silently overwritten."""
    ExamTimetableRun.objects.create(label="corrupt", result_json="{garbage")
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    output = out.getvalue()
    assert "unrenderable" in output


@pytest.mark.django_db
def test_management_command_force_re_migrates_current() -> None:
    """--force should re-write rows already at the current version."""
    payload = stamp_schema_version({"schedule": [], "qa": {}})
    run = ExamTimetableRun.objects.create(
        label="current",
        result_json=json.dumps(payload),
    )
    original_text = run.result_json

    # Without --force, no migration.
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    run.refresh_from_db()
    assert run.result_json == original_text

    # With --force, a re-write happens (even if content is identical, the
    # row goes through json.dumps again — defensive for canonical-order
    # changes in future).
    out = StringIO()
    call_command("normalise_exam_runs", "--force", stdout=out)
    run.refresh_from_db()
    payload_after = json.loads(run.result_json)
    assert payload_after["schema_version"] == EXAM_RUN_SCHEMA_VERSION


@pytest.mark.django_db
def test_management_command_with_explicit_ids() -> None:
    """Only process rows in --ids."""
    legacy_a = ExamTimetableRun.objects.create(label="a", result_json=json.dumps({"schedule": []}))
    legacy_b = ExamTimetableRun.objects.create(label="b", result_json=json.dumps({"schedule": []}))
    out = StringIO()
    call_command("normalise_exam_runs", "--ids", str(legacy_a.id), stdout=out)
    legacy_a.refresh_from_db()
    legacy_b.refresh_from_db()
    payload_a = json.loads(legacy_a.result_json)
    payload_b = json.loads(legacy_b.result_json)
    assert payload_a.get("schema_version") == EXAM_RUN_SCHEMA_VERSION
    assert "schema_version" not in payload_b


@pytest.mark.django_db
def test_management_command_appends_audit_trail_on_migration() -> None:
    """Every migrated row gains a ``normalisation_audit`` entry with
    timestamp, from/to versions, and source-hash."""
    legacy = {"schedule": []}  # v0
    run = ExamTimetableRun.objects.create(label="legacy", result_json=json.dumps(legacy))
    call_command("normalise_exam_runs", stdout=StringIO())
    run.refresh_from_db()
    payload = json.loads(run.result_json)
    audit = payload["normalisation_audit"]
    assert isinstance(audit, list)
    assert len(audit) == 1
    entry = audit[0]
    assert entry["from_version"] == 0
    assert entry["to_version"] == EXAM_RUN_SCHEMA_VERSION
    assert isinstance(entry["at"], str)
    assert isinstance(entry["source_hash"], str)
    assert len(entry["source_hash"]) == 64  # sha256 hex


@pytest.mark.django_db
def test_management_command_audit_trail_is_append_only() -> None:
    """A row already carrying audit history gains another entry rather
    than having its history overwritten. We simulate a row already
    migrated to v1 (schema_version stamped, prior audit recorded);
    --force triggers another v1->v1 entry."""
    pre_existing = [
        {
            "at": "2026-01-01T00:00:00+00:00",
            "from_version": 0,
            "to_version": 1,
            "source_hash": "a" * 64,
        }
    ]
    payload = {
        "schema_version": EXAM_RUN_SCHEMA_VERSION,
        "status": "ok",
        "schedule": [],
        "normalisation_audit": pre_existing,
    }
    run = ExamTimetableRun.objects.create(label="prior", result_json=json.dumps(payload))
    call_command("normalise_exam_runs", "--force", stdout=StringIO())
    run.refresh_from_db()
    new_payload = json.loads(run.result_json)
    audit = new_payload["normalisation_audit"]
    assert len(audit) == 2
    assert audit[0] == pre_existing[0]
    assert audit[1]["from_version"] == EXAM_RUN_SCHEMA_VERSION
    assert audit[1]["to_version"] == EXAM_RUN_SCHEMA_VERSION


@pytest.mark.django_db
def test_management_command_skips_future_version_rows() -> None:
    """Future-version rows came from a newer build; downgrading could
    corrupt data the newer build expects. Report but NOT modify."""
    future = {"schema_version": 99, "schedule": []}
    run = ExamTimetableRun.objects.create(label="future", result_json=json.dumps(future))
    original = run.result_json
    out = StringIO()
    call_command("normalise_exam_runs", stdout=out)
    run.refresh_from_db()
    assert run.result_json == original
    assert "schema_version higher" in out.getvalue()
    assert str(run.id) in out.getvalue()


# ---------------------------------------------------------------------------
# v2: registrar-facing status surface (primary_status + status_flags)
# ---------------------------------------------------------------------------


def test_status_surface_clean() -> None:
    """A clean payload with no defects: primary='clean', flags empty."""
    primary, flags = derive_status_surface(_v2_ok_payload())
    assert primary == "clean"
    assert flags == []


def test_status_surface_overflow_promotes_to_overflow() -> None:
    """An overflow entry on the schedule promotes the headline."""
    payload = _v2_ok_payload()
    payload["schedule"] = [
        {"course_code": "CS101", "day": "SUN", "slot_index": 0},
        {"course_code": "GS999", "day": "OVERFLOW", "slot_index": -1},
    ]
    primary, flags = derive_status_surface(payload)
    assert primary == "contains_overflow"
    assert "overflow" in flags


def test_status_surface_unassigned_rooms_promote_to_room_action() -> None:
    payload = _v2_ok_payload()
    payload["qa"] = {**payload["qa"], "unassigned_room_sections": 3}
    primary, flags = derive_status_surface(payload)
    assert primary == "requires_room_action"
    assert "room_action_required" in flags


def test_status_surface_complete_multi_sitting_does_not_require_room_action() -> None:
    """A multi-sitting section with all rooms assigned is a legitimate
    capacity-resolution path — flag only, not a room-action escalation."""
    payload = _v2_ok_payload()
    payload["qa"] = {
        **payload["qa"],
        "multi_sitting_sections": 1,
        "multi_sitting_details": [
            {
                "section": "GS101",
                "sittings": 2,
                "slots": ["MON:P1", "TUE:P1"],
                "rooms": ["A101", "B202"],
                "incomplete": False,
            }
        ],
    }
    primary, flags = derive_status_surface(payload)
    assert primary == "clean"
    assert "multi_sitting_required" in flags
    assert "room_action_required" not in flags


def test_status_surface_incomplete_multi_sitting_does_promote_room_action() -> None:
    payload = _v2_ok_payload()
    payload["qa"] = {
        **payload["qa"],
        "multi_sitting_sections": 1,
        "multi_sitting_details": [
            {
                "section": "GS101",
                "sittings": 2,
                "slots": ["MON:P1", ""],
                "rooms": ["A101", "UNASSIGNED"],
                "incomplete": True,
            }
        ],
    }
    primary, flags = derive_status_surface(payload)
    assert primary == "requires_room_action"
    assert "room_action_required" in flags
    assert "multi_sitting_required" in flags


def test_status_surface_manual_override_via_explicit_qa_signal() -> None:
    """v2 builds use qa.manual_override_count — no legacy_incomplete_qa flag."""
    payload = _v2_ok_payload()
    payload["qa"] = {**payload["qa"], "manual_override_count": 1}
    primary, flags = derive_status_surface(payload)
    assert primary == "contains_manual_override"
    assert "manual_override" in flags
    assert "legacy_incomplete_qa" not in flags


def test_status_surface_manual_override_inferred_from_legacy_v1_qa() -> None:
    """v1 rows lack qa.manual_override_count — infer from
    qa.conflict_count and raise legacy_incomplete_qa so the registrar
    knows the manual-override signal is approximate."""
    payload = _v2_ok_payload()
    qa = dict(payload["qa"])
    qa.pop("manual_override_count", None)
    qa["conflict_count"] = 2
    payload["qa"] = qa
    primary, flags = derive_status_surface(payload)
    assert primary == "contains_manual_override"
    assert "manual_override" in flags
    assert "legacy_incomplete_qa" in flags


def test_status_surface_thin_clashes_promote_to_clean_with_thin() -> None:
    payload = _v2_ok_payload()
    payload["qa"] = {**payload["qa"], "thin_clash_risk": [{"student": 1, "courses": ["A", "B"]}]}
    primary, flags = derive_status_surface(payload)
    assert primary == "clean_with_approved_thin_conflicts"
    assert "approved_thin_conflicts" in flags


def test_status_surface_severity_overflow_beats_room_action() -> None:
    """When multiple flags are raised, primary picks the most severe.
    Both flags remain on the list."""
    payload = _v2_ok_payload()
    payload["schedule"] = [{"course_code": "X", "day": "OVERFLOW", "slot_index": -1}]
    payload["qa"] = {**payload["qa"], "unassigned_room_sections": 2}
    primary, flags = derive_status_surface(payload)
    assert primary == "contains_overflow"
    # But room_action_required is still in the flags list — the
    # registrar can see both issues.
    assert "overflow" in flags
    assert "room_action_required" in flags


def test_status_surface_severity_room_action_beats_manual_override() -> None:
    payload = _v2_ok_payload()
    payload["qa"] = {
        **payload["qa"],
        "unassigned_room_sections": 1,
        "manual_override_count": 1,
    }
    primary, flags = derive_status_surface(payload)
    assert primary == "requires_room_action"
    assert "room_action_required" in flags
    assert "manual_override" in flags


def test_status_surface_feasibility_error_short_circuits() -> None:
    payload = {"status": "feasibility_error", "violations": []}
    primary, flags = derive_status_surface(payload)
    assert primary == "infeasible"
    # No other flags computed — feasibility_error short-circuits.
    assert flags == []


def test_status_surface_unrenderable_short_circuits() -> None:
    payload = {"status": "unrenderable"}
    primary, flags = derive_status_surface(payload)
    assert primary == "unrenderable"
    assert flags == []


def test_status_surface_future_version_short_circuits() -> None:
    payload = {"status": "future_version_unrenderable"}
    primary, flags = derive_status_surface(payload)
    assert primary == "future_version_unrenderable"
    assert flags == []


def test_status_surface_defensive_against_missing_qa() -> None:
    """A payload with no qa key at all still produces a valid surface.
    legacy_incomplete_qa is raised because we cannot measure most
    signals authoritatively without the qa dict — exactly the safety
    that flag is for."""
    primary, flags = derive_status_surface({"status": "ok", "schedule": []})
    assert primary == "clean"
    assert "legacy_incomplete_qa" in flags


def test_status_surface_legacy_flag_fires_on_older_source_schema() -> None:
    """Schema-based legacy trigger: a row that arrived from
    schema_version < current cannot have measured every v2 derivation
    field at write time, so it gets legacy_incomplete_qa even when no
    individual field is missing."""
    payload = _v2_ok_payload()  # has all v2 fields populated
    primary, flags = derive_status_surface(payload, source_schema_version=1)
    assert primary == "clean"
    assert "legacy_incomplete_qa" in flags


def test_status_surface_legacy_flag_quiet_on_current_source_schema() -> None:
    """A row authored at the current schema version is authoritative
    by definition — no legacy_incomplete_qa flag."""
    payload = _v2_ok_payload()
    primary, flags = derive_status_surface(payload, source_schema_version=2)
    assert primary == "clean"
    assert "legacy_incomplete_qa" not in flags


def test_status_surface_legacy_flag_quiet_when_source_unknown_and_complete() -> None:
    """source_schema_version=None means "write-time call". When all
    v2 fields are present we trust them and don't raise the legacy
    flag — that branch is for explicitly-tagged migrations."""
    payload = _v2_ok_payload()
    primary, flags = derive_status_surface(payload, source_schema_version=None)
    assert primary == "clean"
    assert "legacy_incomplete_qa" not in flags


def test_status_surface_legacy_flag_fires_on_v0_source() -> None:
    """A v0 row (no schema_version at all) is treated as source=0
    by the migrator, which is < current, so legacy_incomplete_qa fires."""
    payload = _v2_ok_payload()
    primary, flags = derive_status_surface(payload, source_schema_version=0)
    assert "legacy_incomplete_qa" in flags


# ---------------------------------------------------------------------------
# v2: multi-sitting details extraction
# ---------------------------------------------------------------------------


def test_derive_multi_sitting_empty_when_no_assign_rooms() -> None:
    assert derive_multi_sitting_details(None) == []
    assert derive_multi_sitting_details({}) == []
    assert derive_multi_sitting_details({"slot1": []}) == []


def test_derive_multi_sitting_detects_split_sections_via_slash_marker() -> None:
    """A section name like 'M5/1' / 'M5/2' indicates it was split by
    the oversize splitter; we collapse them into one logical section."""
    assign_rooms = {
        "MON:P1": [
            {
                "course_code": "GS101",
                "rooms": [
                    {
                        "section": "M5/1",
                        "room_code": "A101",
                        "student_count": 50,
                        "room_capacity": 60,
                    },
                    {
                        "section": "M5/2",
                        "room_code": "A102",
                        "student_count": 50,
                        "room_capacity": 60,
                    },
                ],
            }
        ]
    }
    details = derive_multi_sitting_details(assign_rooms)
    assert len(details) == 1
    d = details[0]
    assert d["section"] == "M5"
    assert d["enrolment"] == 100
    assert d["sittings"] == 2
    assert d["slots"] == ["MON:P1", "MON:P1"]
    assert d["rooms"] == ["A101", "A102"]
    assert d["incomplete"] is False
    assert "GS" not in d["audit_text"]  # the audit text references the section, not GS101
    assert "M5" in d["audit_text"]
    assert "2 sittings" in d["audit_text"]


def test_derive_multi_sitting_detects_split_via_explicit_split_from_field() -> None:
    """Even without a slash in the section name, an explicit
    ``_split_from`` field marks the entry as part of a multi-sitting."""
    assign_rooms = {
        "MON:P1": [
            {
                "course_code": "GS101",
                "rooms": [
                    {
                        "section": "X1",
                        "_split_from": "M5",
                        "room_code": "A101",
                        "student_count": 50,
                        "room_capacity": 60,
                    },
                    {
                        "section": "X2",
                        "_split_from": "M5",
                        "room_code": "A102",
                        "student_count": 50,
                        "room_capacity": 60,
                    },
                ],
            }
        ]
    }
    details = derive_multi_sitting_details(assign_rooms)
    assert len(details) == 1
    assert details[0]["section"] == "M5"


def test_derive_multi_sitting_marks_incomplete_when_room_unassigned() -> None:
    assign_rooms = {
        "MON:P1": [
            {
                "course_code": "GS101",
                "rooms": [
                    {
                        "section": "M5/1",
                        "room_code": "A101",
                        "student_count": 50,
                        "room_capacity": 60,
                    },
                    {
                        "section": "M5/2",
                        "room_code": "UNASSIGNED",
                        "student_count": 50,
                        "room_capacity": 0,
                    },
                ],
            }
        ]
    }
    details = derive_multi_sitting_details(assign_rooms)
    assert details[0]["incomplete"] is True
    assert "INCOMPLETE" in details[0]["audit_text"]


def test_derive_multi_sitting_ignores_singletons() -> None:
    """A single sub-entry that happens to match the heuristic is not
    a multi-sitting (we only recognise it when there are 2+)."""
    assign_rooms = {
        "MON:P1": [
            {
                "course_code": "GS101",
                "rooms": [
                    {
                        "section": "M5/1",
                        "room_code": "A101",
                        "student_count": 50,
                        "room_capacity": 60,
                    }
                ],
            }
        ]
    }
    details = derive_multi_sitting_details(assign_rooms)
    assert details == []


def test_derive_multi_sitting_sorted_alphabetically_for_determinism() -> None:
    assign_rooms = {
        "MON:P1": [
            {
                "course_code": "X",
                "rooms": [
                    {
                        "section": "Z3/1",
                        "room_code": "A1",
                        "student_count": 10,
                        "room_capacity": 20,
                    },
                    {
                        "section": "Z3/2",
                        "room_code": "A2",
                        "student_count": 10,
                        "room_capacity": 20,
                    },
                    {
                        "section": "A1/1",
                        "room_code": "B1",
                        "student_count": 10,
                        "room_capacity": 20,
                    },
                    {
                        "section": "A1/2",
                        "room_code": "B2",
                        "student_count": 10,
                        "room_capacity": 20,
                    },
                ],
            }
        ]
    }
    details = derive_multi_sitting_details(assign_rooms)
    assert [d["section"] for d in details] == ["A1", "Z3"]


# ---------------------------------------------------------------------------
# v1 -> v2 migrator integration via load_normalised_run
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_load_normalised_run_v1_row_gets_full_v2_surface() -> None:
    v1_payload = {
        "schema_version": 1,
        "status": "ok",
        "schedule": [],
        "qa": {"unassigned_room_sections": 2},
    }
    run = ExamTimetableRun.objects.create(label="legacy", result_json=json.dumps(v1_payload))
    loaded = load_normalised_run(run)
    assert loaded["schema_version"] == EXAM_RUN_SCHEMA_VERSION
    assert loaded["primary_status"] == "requires_room_action"
    assert "room_action_required" in loaded["status_flags"]
    # Migrated v1 row also carries legacy_incomplete_qa: the source
    # schema is older than current, so derived fields are not from
    # authoritative measurement.
    assert "legacy_incomplete_qa" in loaded["status_flags"]
    assert loaded["status_derivation_version"] == STATUS_DERIVATION_VERSION
    assert loaded["qa"].get("multi_sitting_sections") == 0


# ---------------------------------------------------------------------------
# Type-shape sanity (TypedDict is a runtime dict, not a class)
# ---------------------------------------------------------------------------


def test_typed_dict_is_runtime_compatible_with_dict() -> None:
    """The TypedDict declaration is a typing artefact only; at runtime the
    payload is a plain dict, so JSON serialisation works."""
    out = normalise_exam_run_payload({})
    encoded = json.dumps(out)
    decoded = json.loads(encoded)
    re_normalised = normalise_exam_run_payload(decoded)
    assert dict(out) == dict(re_normalised)


def test_display_payload_type_is_total_false() -> None:
    """``ExamRunDisplayPayload`` is declared ``total=False`` so all keys
    are optional at the type level (different statuses populate
    different subsets). This is a smoke test that the type is importable
    and resolves correctly."""
    assert ExamRunDisplayPayload.__total__ is False  # type: ignore[attr-defined]
