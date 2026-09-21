"""Stable provenance for the captured inputs actually used by an exam result."""

from __future__ import annotations

import hashlib
import json

from core.services.exam_room_allocation import ROOM_ALLOCATION_POLICY_VERSION

EXAM_INPUT_POLICY_VERSION = 4


def _canonical_records(rows: list[dict]) -> list[dict]:
    """Order set-like source records without mutating the captured inputs."""
    return sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )


def fingerprint_exam_inputs(
    *,
    result: dict,
    enrolled_sets: dict[str, set[int]],
    course_meta: dict[str, dict],
    student_attribution: list[dict],
    rooms: list[dict],
    days: list[str],
    periods: list[str],
    max_per_day: int,
    thin_conflict_threshold: int,
) -> str:
    """Hash captured source values without querying again or hashing placements.

    Both a fresh build and fixed-placement evaluation use this exact contract.
    Position changes and report timestamps are intentionally not input drift.
    """
    inputs = {
        "evaluation_version": EXAM_INPUT_POLICY_VERSION,
        "room_allocation_version": ROOM_ALLOCATION_POLICY_VERSION,
        "enrollment_source": result["enrollment_source"],
        "enrolled": {code: sorted(sids) for code, sids in enrolled_sets.items()},
        "course_meta": {
            code: {**meta, "programs": sorted(meta.get("programs", []))}
            for code, meta in course_meta.items()
        },
        "credit_map": result["credit_map"],
        "section_enrollment": {
            code: _canonical_records(groups)
            for code, groups in result["section_enrollment"].items()
        },
        "student_attribution": sorted(student_attribution, key=lambda row: row["student_id"]),
        "operations_snapshot": result.get("operations_snapshot"),
        "rooms": sorted(rooms, key=lambda room: room["room_code"]),
        "buckets": _canonical_records(
            [
                {**bucket, "courses": sorted(bucket["courses"])}
                for bucket in result["buckets_summary"]
            ]
        ),
        "enrollment_scope": {
            **result["enrollment_scope"],
            "programs": sorted(result["enrollment_scope"].get("programs", [])),
            "sections": sorted(result["enrollment_scope"].get("sections", [])),
        },
        "days": days,
        "periods": periods,
        "max_per_day": max_per_day,
        "thin_conflict_threshold": thin_conflict_threshold,
        "assign_rooms": result["assign_rooms"],
        "seed": result["seed"],
    }
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
