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
    ExamRunDisplayPayload,
    load_normalised_run,
    normalise_exam_run_payload,
    stamp_schema_version,
)

# ---------------------------------------------------------------------------
# Pure normaliser tests (no DB)
# ---------------------------------------------------------------------------


def _v1_ok_payload() -> dict:
    """A canonical v1 ok-shape payload to compare against."""
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


def test_identity_v1() -> None:
    payload = _v1_ok_payload()
    out = normalise_exam_run_payload(payload)
    # Every input key survives untouched.
    for key, value in payload.items():
        assert out[key] == value, f"key {key!r} mutated by normaliser"
    # schema_version still 1.
    assert out["schema_version"] == 1


def test_normaliser_does_not_mutate_input() -> None:
    payload = _v1_ok_payload()
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
    assert out["qa"] == {}


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
    assert out["qa"] == {}


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
        "garbage",
        _v1_ok_payload(),
    ],
)
def test_status_always_valid(input_payload: object) -> None:
    out = normalise_exam_run_payload(input_payload)
    assert out["status"] in ("ok", "feasibility_error", "unrenderable")


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
    assert out["qa"] == {}
    assert out["slots"] == []
    assert out["violations"] == []


def test_unrenderable_sentinel_includes_ok_keys() -> None:
    out = normalise_exam_run_payload(None)
    assert out["status"] == "unrenderable"
    assert out["schedule"] == []
    assert out["qa"] == {}
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
    assert loaded["qa"] == {}
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
