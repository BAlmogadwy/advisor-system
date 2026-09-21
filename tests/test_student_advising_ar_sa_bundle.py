from __future__ import annotations

import json
import zipfile

import pytest

from evals.advisor.student_advising_ar_sa_bundle import (
    JSONL_NAME,
    README_NAME,
    SCHEMA_NAME,
    BundleData,
    BundleRecord,
    BundleValidationError,
    audit_report,
    expected_plan,
    load_bundle,
    planner_cases,
    record_category,
    score_results,
)


def _row(
    case_id: str,
    root_id: str,
    question: str,
    *,
    provenance_type: str = "observed_normalized",
    variant_of: str | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "split": "dev",
        "language": "ar-SA",
        "utterance_ar": question,
        "intent": "fixture_intent",
        "pii_removed": True,
        "provenance": {
            "type": provenance_type,
            "root_id": root_id,
            "variant_of": variant_of,
        },
    }


def _write_bundle(tmp_path, rows, *, extra_entries=None):
    target = tmp_path / "bundle.zip"
    entries = {
        JSONL_NAME: "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        SCHEMA_NAME: "{}",
        README_NAME: "fixture",
        **(extra_entries or {}),
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return target


def _record(case_id: str, root_id: str, question: str) -> BundleRecord:
    raw = _row(case_id, root_id, question)
    return BundleRecord(
        case_id=case_id,
        root_id=root_id,
        split="dev",
        provenance_type="observed_normalized",
        question=question,
        raw=raw,
    )


def test_load_bundle_validates_root_links_without_extracting(tmp_path):
    observed = _row("sa_adv_000001", "root_007", "هل أقدر أكسر متطلب؟")
    variant = _row(
        "sa_adv_000002",
        "root_007",
        "كيف أطلب استثناء متطلب؟",
        provenance_type="synthetic_variant",
        variant_of="sa_adv_000001",
    )
    path = _write_bundle(tmp_path, [observed, variant])

    bundle = load_bundle(path, strict_release=False)

    assert len(bundle.rows) == 2
    assert bundle.sha256
    assert not (tmp_path / JSONL_NAME).exists()


def test_load_bundle_rejects_unexpected_or_traversal_entries(tmp_path):
    path = _write_bundle(
        tmp_path,
        [_row("sa_adv_000001", "root_007", "هل أقدر أكسر متطلب؟")],
        extra_entries={"../escape.txt": "unsafe"},
    )

    with pytest.raises(BundleValidationError, match="closed contract"):
        load_bundle(path, strict_release=False)


def test_variant_must_point_to_observed_record(tmp_path):
    observed = _row("sa_adv_000001", "root_007", "هل أقدر أكسر متطلب؟")
    variant = _row(
        "sa_adv_000002",
        "root_007",
        "كيف أطلب استثناء متطلب؟",
        provenance_type="synthetic_variant",
        variant_of="sa_adv_999999",
    )
    path = _write_bundle(tmp_path, [observed, variant])

    with pytest.raises(BundleValidationError, match="variant parent"):
        load_bundle(path, strict_release=False)


def test_root_004_does_not_leak_inherited_course_code():
    coded = _record("sa_adv_000010", "root_004", "MATH 204 يفتح أي مادة؟")
    name_only = _record(
        "sa_adv_000012",
        "root_004",
        "تفاضل ٢ متطلب لأي مقررات بعده بالخطة؟",
    )

    assert record_category(coded) == "directly_scorable"
    assert expected_plan(coded)["evidence_requests"][0]["arguments"] == {"course_code": "MATH204"}
    assert record_category(name_only) == "capability_gap"
    assert expected_plan(name_only) is None


def test_audit_and_planner_projection_keep_gold_out_of_messages(tmp_path):
    rows = (
        _record("sa_adv_000019", "root_007", "هل أقدر أكسر متطلب؟"),
        _record("sa_adv_000091", "root_031", "سجل المواد بدلًا عني"),
        _record("sa_adv_000001", "root_001", "سؤال يحتاج مقاعد حية"),
    )
    bundle = BundleData(path=tmp_path / "source.zip", sha256="ABC", rows=rows)

    report = audit_report(bundle)
    cases = planner_cases(bundle)

    assert report["compatibility"]["record_counts"] == {
        "capability_gap": 1,
        "directly_scorable": 1,
        "transactional_safety": 1,
    }
    assert {case["id"] for case in cases} == {"sa_adv_000019", "sa_adv_000091"}
    assert all("raw" not in case and "expected" not in case for case in cases)


def test_score_results_canonicalizes_course_code_and_server_bound_policy_query(tmp_path):
    rows = (
        _record("sa_adv_000010", "root_004", "MATH 204 يفتح أي مادة؟"),
        _record("sa_adv_000019", "root_007", "هل أقدر أكسر متطلب؟"),
        _record("sa_adv_000091", "root_031", "سجل المواد بدلًا عني"),
    )
    bundle = BundleData(path=tmp_path / "source.zip", sha256="ABC", rows=rows)
    candidate = {
        "rows": [
            {
                "case_id": "sa_adv_000010",
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["course_eligibility"],
                    "evidence_requests": [
                        {
                            "capability": "why_course_locked",
                            "arguments": {"course_code": "MATH 204"},
                        }
                    ],
                },
            },
            {
                "case_id": "sa_adv_000019",
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["policy_rule"],
                    "evidence_requests": [
                        {
                            "capability": "policy_lookup",
                            "arguments": {"query": "متطلب"},
                        }
                    ],
                },
            },
            {
                "case_id": "sa_adv_000091",
                "plan": {
                    "decision": "direct",
                    "requested_outcomes": ["general_conversation"],
                    "evidence_requests": [],
                },
            },
        ]
    }

    report = score_results(bundle, candidate)

    assert report["coverage"]["complete"] is True
    assert report["records"] == {"passed": 3, "total": 3, "rate": 1.0}


def test_score_results_rejects_extra_tool_and_unknown_case(tmp_path):
    row = _record("sa_adv_000019", "root_007", "هل أقدر أكسر متطلب؟")
    bundle = BundleData(path=tmp_path / "source.zip", sha256="ABC", rows=(row,))
    candidate = {
        "rows": [
            {
                "case_id": row.case_id,
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["policy_rule", "degree_progress"],
                    "evidence_requests": [
                        {"capability": "policy_lookup", "arguments": {}},
                        {"capability": "my_progress", "arguments": {}},
                    ],
                },
            },
            {
                "case_id": "sa_adv_999999",
                "plan": {
                    "decision": "direct",
                    "requested_outcomes": ["general_conversation"],
                },
            },
        ]
    }

    report = score_results(bundle, candidate)

    assert report["coverage"]["complete"] is False
    assert report["coverage"]["unknown"] == ["sa_adv_999999"]
    assert report["records"]["passed"] == 0
