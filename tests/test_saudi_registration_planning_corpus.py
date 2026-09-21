from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import yaml

from core.services.student_advisor_v2 import STUDENT_V21_TOOL_NAMES
from core.services.student_advisor_v21_plan import (
    UNSUPPORTED_REQUEST_OUTCOMES,
    StudentRequestOutcome,
)

CORPUS = Path("evals/advisor/saudi_registration_planning_corpus_v1.yaml")
FROZEN_SHA256 = "a4971174b1d498451f0c96a15b4326e3ed320f1c8afbd1131c79aa17f6a21941"
FROZEN_OWNER_UTTERANCES_SHA256 = "da44b1f8e8442b88b4571f33470627c2543d8038efcef7b78ed659a60fe99b71"


def _payload() -> dict:
    return yaml.safe_load(CORPUS.read_text(encoding="utf-8"))


def test_owner_supplied_saudi_corpus_is_frozen_and_complete() -> None:
    raw = CORPUS.read_bytes()
    payload = yaml.safe_load(raw)
    cases = payload["cases"]

    assert hashlib.sha256(raw).hexdigest() == FROZEN_SHA256
    assert payload["meta"]["corpus_version"] == "1.1.15"
    assert payload["meta"]["total_count"] == 108
    assert len(cases) == 108
    assert len({case["id"] for case in cases}) == 108
    assert [case["source_ordinal"] for case in cases] == list(range(1, 109))
    assert Counter(case["kind"] for case in cases) == {"bullet": 104, "composite": 4}
    assert Counter(case["category_id"] for case in cases) == {
        "eligibility": 10,
        "prerequisites": 9,
        "available": 10,
        "priority": 10,
        "add_one": 8,
        "drop": 10,
        "current_review": 10,
        "build": 9,
        "pin": 8,
        "accelerate": 10,
        "what_if": 10,
        "composite": 4,
    }


def test_grounding_snapshot_matches_the_inspected_current_registration() -> None:
    payload = _payload()
    grounding = payload["grounding"]
    facts = grounding["observed_facts"]

    assert grounding["inspected_read_only"] == "2026-08-24"
    assert facts["registered_credit_hours"] == 17
    assert facts["registered_courses"] == [
        {"course_code": "DS321", "section": "M4", "credits": 4},
        {"course_code": "DS332", "section": "M4", "credits": 4},
        {"course_code": "DS341", "section": "M2", "credits": 3},
        {"course_code": "MATH471", "section": "M3", "credits": 3},
        {"course_code": "STAT301", "section": "M28", "credits": 3},
    ]
    assert sum(row["credits"] for row in facts["registered_courses"]) == 17
    assert facts["expected_plan_only_courses"] == []
    assert "expected_plan_only_course" not in facts

    profiles = grounding["grounding_profiles"]
    assert profiles["registered_current"] == {"XXXX": "DS321"}
    assert profiles["registered_pair"] == {"XXXX": "DS341", "YYYY": "DS321"}
    assert profiles["current_triple"] == {
        "CS371": "DS332",
        "AI331": "DS341",
        "DS341": "DS321",
    }
    assert "expected_plan" not in profiles


def test_grounding_patch_preserves_every_owner_supplied_utterance_verbatim() -> None:
    import json

    cases = _payload()["cases"]
    serialized = json.dumps(
        [case["utterance_ar"] for case in cases],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert hashlib.sha256(serialized).hexdigest() == FROZEN_OWNER_UTTERANCES_SHA256


def test_current_registration_grounding_cases_have_exact_closed_contracts() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}

    drop = cases["SA-DROP-007"]
    assert drop["utterance_ar"] == "أيهم أفضل أحذف: `XXXX` أو `YYYY`؟"
    assert drop["grounded_utterance_ar"] == ("أيهم أفضل أحذف: `DS341` أو `DS321`؟")
    assert drop["contract"]["support"] == "read_only_partial"
    assert drop["contract"]["required_controls"] == {
        "rank_current_course_drop_impact": {
            "objective": "balanced",
            "course_codes": ["DS341", "DS321"],
        }
    }
    assert drop["contract"]["acceptable_plans"] == [
        {
            "required_controls": {
                "rank_current_course_drop_impact": {
                    "objective": "least_graduation_delay",
                    "course_codes": ["DS341", "DS321"],
                }
            }
        }
    ]

    composite_add = cases["SA-COMPOSITE-001"]
    assert composite_add["utterance_ar"].startswith("عندي 15 ساعة حالياً")
    assert composite_add["grounded_utterance_ar"].startswith("عندي 17 ساعة حالياً")
    assert composite_add["contract"]["support"] == "read_only_partial"

    composite_drop = cases["SA-COMPOSITE-002"]
    assert "`CS371` و`AI331` و`DS341`" in composite_drop["utterance_ar"]
    assert "`DS332` و`DS341` و`DS321`" in composite_drop["grounded_utterance_ar"]
    assert composite_drop["contract"]["support"] == "read_only_partial"
    assert composite_drop["contract"]["required_controls"] == {
        "rank_current_course_drop_impact": {
            "objective": "least_graduation_delay",
            "course_codes": ["DS332", "DS341", "DS321"],
        }
    }

    removal_controls = {
        "graduation_progress": {
            "planning_baseline_kind": "registered_timetable",
            "remove_current_courses": ["DS321"],
        }
    }
    for case_id, support in (
        ("SA-WHATIF-002", "supported"),
        ("SA-WHATIF-003", "read_only_partial"),
    ):
        case = cases[case_id]
        assert case["grounding_profile"] == "registered_current"
        assert case["contract"]["support"] == support
        assert case["contract"]["required_controls"] == removal_controls


def test_grounded_cases_are_executable_on_the_v21_read_only_surface() -> None:
    cases = _payload()["cases"]
    advertised = set(STUDENT_V21_TOOL_NAMES)

    for case in cases:
        grounded = case["grounded_utterance_ar"]
        contract = case["contract"]
        assert grounded.strip()
        assert "XXXX" not in grounded
        assert "YYYY" not in grounded
        assert contract["support"] in {
            "supported",
            "read_only_partial",
            "capability_gap",
        }
        assert contract["mode"] in {"execute", "clarify", "direct", "unsupported"}
        assert contract["requested_outcomes"]
        assert len(contract["requested_outcomes"]) == len(set(contract["requested_outcomes"]))
        assert set(contract["requested_outcomes"]) <= {
            outcome.value for outcome in StudentRequestOutcome
        }
        if contract["mode"] == "unsupported":
            assert set(contract["requested_outcomes"]) <= {
                outcome.value for outcome in UNSUPPORTED_REQUEST_OUTCOMES
            }
            assert contract["expected_tools"] == []
        assert set(contract["expected_tools"]) <= advertised
        for alternative in contract.get("acceptable_plans", []):
            outcomes = alternative.get("requested_outcomes", contract["requested_outcomes"])
            tools = alternative.get("expected_tools", contract["expected_tools"])
            assert outcomes
            assert set(outcomes) <= {outcome.value for outcome in StudentRequestOutcome}
            assert set(tools) <= advertised


def test_only_adjudicated_cases_have_closed_acceptable_plan_alternatives() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}
    expected = {
        "SA-ELIG-001",
        "SA-AVAILABLE-005",
        "SA-PRIORITY-007",
        "SA-ADDONE-004",
        "SA-DROP-004",
        "SA-DROP-005",
        "SA-DROP-007",
        "SA-DROP-008",
        "SA-BUILD-003",
        "SA-WHATIF-006",
    }

    assert {
        case_id for case_id, case in cases.items() if case["contract"].get("acceptable_plans")
    } == expected
    assert cases["SA-ELIG-001"]["contract"]["acceptable_plans"] == [
        {
            "requested_outcomes": [
                "course_eligibility",
                "timetable_feasibility",
            ],
            "expected_tools": [
                "why_course_locked",
                "my_clash_free_sections",
            ],
        }
    ]
    assert cases["SA-AVAILABLE-005"]["contract"]["acceptable_plans"] == [
        {"required_controls": {"recommend_feasible_course_addition": {"objective": "balanced"}}}
    ]


def test_corrected_canonical_outcomes_cover_explicit_user_intent() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}
    expected = {
        "SA-ELIG-003": ["course_eligibility", "prerequisite_information"],
        "SA-ELIG-005": ["prerequisite_information"],
        "SA-ELIG-007": ["prerequisite_information"],
        "SA-PREREQ-004": ["prerequisite_information"],
        "SA-AVAILABLE-006": ["course_priority", "available_courses"],
        "SA-AVAILABLE-007": ["course_priority", "available_courses"],
        "SA-ADDONE-005": ["course_addition"],
        "SA-DROP-006": ["graduation_impact"],
        "SA-REVIEW-010": ["timetable_review"],
        "SA-ACCEL-001": ["timetable_review"],
        "SA-ACCEL-002": ["timetable_review"],
        "SA-ACCEL-003": ["timetable_review"],
        "SA-ACCEL-004": ["course_replacement"],
        "SA-ACCEL-006": ["timetable_review"],
        "SA-ACCEL-007": ["timetable_review"],
        "SA-ACCEL-009": ["timetable_review"],
        "SA-ACCEL-010": ["graduation_impact"],
        "SA-WHATIF-010": ["graduation_impact"],
        "SA-COMPOSITE-004": ["timetable_review"],
    }

    assert {
        case_id: cases[case_id]["contract"]["requested_outcomes"] for case_id in expected
    } == expected


def test_personalized_prerequisite_questions_use_lock_explanations() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}

    for case_id in ("SA-ELIG-007", "SA-PREREQ-004"):
        contract = cases[case_id]["contract"]
        assert contract["requested_outcomes"] == ["prerequisite_information"]
        assert contract["expected_tools"] == ["why_course_locked"]


def test_graduation_replacement_and_credit_load_boundaries_are_typed() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}

    replacement = cases["SA-ACCEL-004"]["contract"]
    assert replacement["requested_outcomes"] == ["course_replacement"]
    assert replacement["expected_tools"] == ["graduation_progress"]
    assert replacement["required_controls"]["graduation_progress"] == {
        "planning_baseline_kind": "recommended_current_term",
        "search_better_replacements": True,
    }
    assert "acceptable_plans" not in replacement

    for case_id in ("SA-WHATIF-008", "SA-WHATIF-009"):
        contract = cases[case_id]["contract"]
        assert contract["mode"] == "unsupported"
        assert contract["requested_outcomes"] == ["credit_load_comparison"]
        assert contract["expected_tools"] == []

    noncompletion = cases["SA-WHATIF-004"]["contract"]
    assert noncompletion["required_controls"]["graduation_progress"] == {
        "planning_baseline_kind": "registered_timetable",
        "noncompletion_current_courses": ["DS332"],
    }
    assert "remove_current_courses" not in noncompletion["required_controls"]["graduation_progress"]

    non_enrolment = cases["SA-WHATIF-002"]["contract"]
    assert non_enrolment["required_controls"]["graduation_progress"] == {
        "planning_baseline_kind": "registered_timetable",
        "remove_current_courses": ["DS321"],
    }
    assert (
        "noncompletion_current_courses"
        not in non_enrolment["required_controls"]["graduation_progress"]
    )

    top_five = cases["SA-PRIORITY-010"]["contract"]
    assert top_five["requested_outcomes"] == ["course_priority"]
    assert top_five["expected_tools"] == ["my_progress"]
    assert top_five["required_controls"] == {"my_progress": {"priority_limit": 5}}


def test_literal_builder_caps_modes_and_section_pins_are_typed() -> None:
    cases = {case["id"]: case for case in _payload()["cases"]}
    pin = {"course_code": "DS341", "section_label": "M2"}

    assert cases["SA-BUILD-001"]["contract"]["required_controls"] == {
        "build_timetable_proposal": {"mode": "from_scratch"}
    }
    assert cases["SA-BUILD-005"]["contract"]["required_controls"] == {
        "build_timetable_proposal": {"mode": "from_scratch", "max_credits": 15}
    }
    assert cases["SA-BUILD-006"]["contract"]["required_controls"] == {
        "build_timetable_proposal": {"mode": "from_scratch", "target_credits": 18}
    }
    assert cases["SA-BUILD-006"]["contract"]["support"] == "read_only_partial"
    for case_id, kind in (
        ("SA-BUILD-002", "timetable_preference"),
        ("SA-BUILD-007", "timetable_load"),
        ("SA-BUILD-008", "timetable_preference"),
    ):
        contract = cases[case_id]["contract"]
        assert contract["mode"] == "clarify"
        assert contract["clarification_kind"] == kind
        assert contract["requested_outcomes"] == ["timetable_build"]
        assert contract["expected_tools"] == []

    for case_id in ("SA-BUILD-003", "SA-BUILD-004", "SA-BUILD-006", "SA-BUILD-009"):
        assert (
            cases[case_id]["contract"]["required_controls"]["build_timetable_proposal"]["mode"]
            == "from_scratch"
        )

    single_pin_controls = {
        "build_timetable_proposal": {
            "mode": "from_scratch",
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        }
    }
    for index in (1, 2, 3, 4, 5, 6):
        assert cases[f"SA-PIN-{index:03d}"]["contract"]["required_controls"] == single_pin_controls
    assert cases["SA-PIN-007"]["contract"]["required_controls"] == {
        "recommend_feasible_course_addition": {
            "objective": "balanced",
            "pinned_sections": [pin],
        }
    }
    assert cases["SA-PIN-008"]["contract"]["required_controls"] == {
        "build_timetable_proposal": {
            "mode": "from_scratch",
            "must_take_courses": ["DS341", "DS432"],
            "pinned_sections": [
                pin,
                {"course_code": "DS432", "section_label": "M3"},
            ],
        }
    }

    assert cases["SA-COMPOSITE-003"]["contract"]["required_controls"] == {
        "build_timetable_proposal": {
            "mode": "from_scratch",
            "max_credits": 18,
            "must_take_courses": ["DS341"],
            "pinned_sections": [pin],
        }
    }

    # Numeric context and comparison values are not silently reclassified as
    # total timetable ceilings.
    assert (
        "max_credits"
        not in cases["SA-COMPOSITE-001"]["contract"]["required_controls"][
            "recommend_feasible_course_addition"
        ]
    )
    for case_id in ("SA-WHATIF-008", "SA-WHATIF-009"):
        assert "required_controls" not in cases[case_id]["contract"]
