from __future__ import annotations

from evals.advisor.run_student_advising_ar_sa_bundle import build_diagnostic_report
from evals.advisor.student_advising_ar_sa_bundle import BundleData, BundleRecord


def _bundle(tmp_path) -> BundleData:
    raw = {
        "id": "sa_adv_000019",
        "split": "dev",
        "language": "ar-SA",
        "utterance_ar": "هل أقدر أكسر متطلب؟",
        "intent": "prerequisite_exception",
        "pii_removed": True,
        "provenance": {
            "type": "observed_normalized",
            "root_id": "root_007",
            "variant_of": None,
        },
    }
    row = BundleRecord(
        case_id="sa_adv_000019",
        root_id="root_007",
        split="dev",
        provenance_type="observed_normalized",
        question="هل أقدر أكسر متطلب؟",
        raw=raw,
    )
    return BundleData(path=tmp_path / "bundle.zip", sha256="ABC", rows=(row,))


def _policy_schema() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "policy_lookup",
                "description": "Look up governing policy evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        }
    ]


def test_build_report_reparses_candidate_through_typed_contract(tmp_path):
    candidate = {
        "rows": [
            {
                "case_id": "sa_adv_000019",
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["policy_rule"],
                    "evidence_requests": [
                        {"capability": "policy_lookup", "arguments": {"query": "متطلب"}}
                    ],
                    "clarification_kind": "none",
                    "clarification_question": "",
                },
                "model": "fixture-model",
            }
        ]
    }

    report = build_diagnostic_report(
        _bundle(tmp_path),
        candidate,
        advertised_tools=_policy_schema(),
        collection={"mode": "offline_replay", "provider_calls": 0},
    )

    assert report["diagnostic_valid"] is True
    assert report["typed_plan_errors"] == []
    assert report["candidate"]["records"]["rate"] == 1.0
    assert report["runner"]["evidence_tools_executed"] is False


def test_build_report_fails_closed_on_schema_invalid_arguments(tmp_path):
    candidate = {
        "rows": [
            {
                "case_id": "sa_adv_000019",
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["policy_rule"],
                    "evidence_requests": [
                        {
                            "capability": "policy_lookup",
                            "arguments": {"student_id": 1234567},
                        }
                    ],
                    "clarification_kind": "none",
                    "clarification_question": "",
                },
            }
        ]
    }

    report = build_diagnostic_report(
        _bundle(tmp_path),
        candidate,
        advertised_tools=_policy_schema(),
    )

    assert report["diagnostic_valid"] is False
    assert report["typed_plan_errors"][0]["case_id"] == "sa_adv_000019"
    assert "error_category" in report["typed_plan_errors"][0]


def test_repeated_capability_is_scored_safe_but_not_production_executable(tmp_path):
    raw = {
        "id": "sa_adv_000094",
        "split": "test",
        "language": "ar-SA",
        "utterance_ar": "بدل جودة الحياة بالكتابة الفنية بشكل آمن",
        "intent": "build_with_fixed_items",
        "pii_removed": True,
        "provenance": {
            "type": "observed_normalized",
            "root_id": "root_032",
            "variant_of": None,
        },
    }
    bundle = BundleData(
        path=tmp_path / "bundle.zip",
        sha256="ABC",
        rows=(
            BundleRecord(
                case_id="sa_adv_000094",
                root_id="root_032",
                split="test",
                provenance_type="observed_normalized",
                question=str(raw["utterance_ar"]),
                raw=raw,
            ),
        ),
    )
    candidate = {
        "rows": [
            {
                "case_id": "sa_adv_000094",
                "plan": {
                    "decision": "execute",
                    "requested_outcomes": ["course_catalogue"],
                    "evidence_requests": [
                        {"capability": "lookup_course", "arguments": {"query": "جودة الحياة"}},
                        {
                            "capability": "lookup_course",
                            "arguments": {"query": "الكتابة الفنية"},
                        },
                    ],
                    "clarification_kind": "none",
                    "clarification_question": "",
                },
            }
        ]
    }
    schema = [
        {
            "type": "function",
            "function": {
                "name": "lookup_course",
                "description": "Resolve a course name.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    report = build_diagnostic_report(bundle, candidate, advertised_tools=schema)

    assert report["artifact_complete"] is True
    assert report["typed_plans_valid"] is True
    assert report["all_plans_production_executable"] is False
    assert report["diagnostic_valid"] is False
    assert report["candidate"]["records"]["passed"] == 1
