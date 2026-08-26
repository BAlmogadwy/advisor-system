from __future__ import annotations

from typing import Any

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services import planner_builder
from core.services.advisor_presentations import timetable_presentation_from_tool_results
from core.services.answer_consistency import EvidenceValidationScope, check_answer
from core.services.llm_remote_privacy import RemoteIdentityMap, project_tool_result_for_remote
from core.services.planner_builder import build_plans
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import (
    _V21_MAX_CREDIT_PATTERNS,
    _V21_TARGET_CREDIT_PATTERNS,
    _legacy_v2_arguments,
    _normalise_timetable_proposal_args,
    _safe_timetable_proposal_fact_fragment,
    _safe_v21_planned_answer,
    _v21_argument_provenance_contract,
    _v21_credit_values,
    _v21_missing_explicit_constraint_paths,
    student_v2_tool_schemas,
    student_v21_tool_schemas,
)
from core.services.student_advisor_v21_plan import (
    PlannedCapabilityCall,
    StudentRequestOutcome,
    StudentTurnPlan,
    TurnPlanDecision,
    TurnPlanProvenanceError,
    validate_capability_argument_provenance,
)
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 7654321


@pytest.fixture
def exact_credit_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    student = Student.objects.create(
        student_id=SID,
        name="Exact target student",
        program="TG",
        section="M",
        status="active",
    )
    # Six three-credit courses make the Saudi BUILD-006 18-hour request
    # feasible. Two four-credit courses make a separate pre-top-k regression:
    # an unconstrained optimiser prefers 8 credits, while target=6 must find the
    # lower-scoring pair of three-credit courses inside the bounded search.
    course_rows = [
        ("TG101", 3),
        ("TG102", 3),
        ("TG103", 3),
        ("TG104", 3),
        ("TG105", 3),
        ("TG106", 3),
        ("TG401", 4),
        ("TG402", 4),
    ]
    slots = [
        ("SUN", "08:00", "09:00"),
        ("MON", "08:00", "09:00"),
        ("TUE", "08:00", "09:00"),
        ("WED", "08:00", "09:00"),
        ("THU", "08:00", "09:00"),
        ("SUN", "10:00", "11:00"),
        ("MON", "10:00", "11:00"),
        ("TUE", "10:00", "11:00"),
    ]
    sections: dict[str, TermSection] = {}
    for (code, credits), (day, start, end) in zip(course_rows, slots, strict=True):
        Course.objects.create(
            course_code=code,
            description=f"{code} exact-credit fixture",
            credit_hours=credits,
        )
        ProgrammeRequirement.objects.create(
            program="TG",
            course_code=code,
            course_name=f"{code} exact-credit fixture",
            type="Mandatory",
            programme_term=1,
            credit_hours=credits,
        )
        section = TermSection.objects.create(
            course_code=code,
            course_number="",
            course_key=code,
            course_name=f"{code} exact-credit fixture",
            section="M1",
            available_capacity=30,
            registered_count=0,
        )
        TermSectionProgram.objects.create(term_section=section, program="TG")
        TermSectionMeeting.objects.create(
            term_section=section,
            day=day,
            start_time=start,
            end_time=end,
        )
        sections[code] = section

    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )
    return {"student": student, "sections": sections}


def _execute_build(arguments: dict[str, Any]) -> dict[str, Any]:
    return get_default_registry().execute(
        "build_timetable_proposal",
        arguments,
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": 1448, "term": 1},
    )


def _build_plan(arguments: dict[str, Any]) -> StudentTurnPlan:
    return StudentTurnPlan(
        decision=TurnPlanDecision.EXECUTE,
        evidence_requests=(
            PlannedCapabilityCall(
                capability="build_timetable_proposal",
                arguments=arguments,
            ),
        ),
        requested_outcomes=(StudentRequestOutcome.TIMETABLE_BUILD,),
    )


def test_exact_18_credit_target_returns_only_exact_fixture_alternatives(
    exact_credit_world: dict[str, Any],
) -> None:
    result = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": [f"TG10{index}" for index in range(1, 7)],
            "must_take_courses": ["TG101"],
            "pinned_sections": [{"course_code": "TG101", "section_label": "M1"}],
            "max_credits": 18,
            "target_credits": 18,
        }
    )

    assert result["ok"] is True
    assert result["status"] == "PROPOSALS_GENERATED"
    assert result["target_credits"] == 18
    assert result["target_credits_satisfied"] is True
    assert result["target_credit_status"] == "SATISFIED"
    assert result["constraints_satisfied"] is True
    assert result["must_take_courses"] == ["TG101"]
    assert result["pinned_sections"] == [{"course_code": "TG101", "section_label": "M1"}]
    assert result["alternatives"]
    assert {row["total_credit_hours"] for row in result["alternatives"]} == {18}
    assert result["search"] == {
        "bounded": True,
        "planner_methods": ["A", "B", "C"],
        "alternatives_per_method": 3,
        "exact_target_enforced": True,
    }


def test_generic_fifteen_credit_build_rebuilds_instead_of_retaining_over_cap_baseline(
    exact_credit_world: dict[str, Any],
) -> None:
    # This mirrors SA-BUILD-005: the student asks to build a capped timetable
    # without saying to retain or adjust the current sections.  The registered
    # fixture starts above the cap, so around_current would be a false hard
    # negative; from_scratch must return real read-only alternatives instead.
    for code in ("TG101", "TG102", "TG103", "TG104", "TG401"):
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=exact_credit_world["sections"][code],
            source="scraper_timetable",
        )

    result = _execute_build({"mode": "from_scratch", "max_credits": 15})

    assert result["ok"] is True
    assert result["mode"] == "from_scratch"
    assert result["baseline_kind"] == "REGISTERED"
    assert result["baseline_credit_hours"] == 16
    assert result["status"] == "PROPOSALS_GENERATED"
    assert result["constraints_satisfied"] is True
    assert result["alternatives"]
    assert all(0 < int(option["total_credit_hours"]) <= 15 for option in result["alternatives"])


def test_from_scratch_pin_preserves_only_the_pinned_section_and_can_change_the_rest(
    exact_credit_world: dict[str, Any],
) -> None:
    pinned = exact_credit_world["sections"]["TG101"]
    replaceable = exact_credit_world["sections"]["TG102"]
    TermSectionMeeting.objects.filter(term_section=replaceable).update(
        day="SUN",
        start_time="08:00",
        end_time="09:00",
    )
    alternate = TermSection.objects.create(
        course_code="TG102",
        course_number="",
        course_key="TG102",
        course_name="TG102 exact-credit fixture",
        section="M2",
        available_capacity=30,
        registered_count=0,
    )
    TermSectionProgram.objects.create(term_section=alternate, program="TG")
    TermSectionMeeting.objects.create(
        term_section=alternate,
        day="MON",
        start_time="08:00",
        end_time="09:00",
    )
    for section in (pinned, replaceable):
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=section,
            source="scraper_timetable",
        )

    result = _execute_build(
        {
            "mode": "from_scratch",
            "must_take_courses": ["TG101", "TG102"],
            "pinned_sections": [{"course_code": "TG101", "section_label": "M1"}],
        }
    )

    assert result["status"] == "PROPOSALS_GENERATED"
    assert result["alternatives"]
    selected = [
        {(row["course_code"], row["section"]) for row in option["courses"]}
        for option in result["alternatives"]
    ]
    assert all(("TG101", "M1") in option for option in selected)
    assert any(("TG102", "M2") in option and ("TG102", "M1") not in option for option in selected)


def test_exact_target_is_enforced_inside_search_before_top_k(
    exact_credit_world: dict[str, Any],
) -> None:
    result = build_plans(
        "1448",
        "1",
        [
            {"course_code": "TG401", "credits": 4, "status": "Eligible"},
            {"course_code": "TG402", "credits": 4, "status": "Eligible"},
            {"course_code": "TG101", "credits": 3, "status": "Eligible"},
            {"course_code": "TG102", "credits": 3, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=8,
        target_credits=6,
        gender="M",
        program="TG",
        require_complete_meetings=True,
    )

    assert result["options"], result
    credit_by_code = {"TG401": 4, "TG402": 4, "TG101": 3, "TG102": 3}
    assert {
        sum(credit_by_code[row["course_code"]] for row in option["mappings"])
        for option in result["options"]
    } == {6}
    assert all(
        {row["course_code"] for row in option["mappings"]} == {"TG101", "TG102"}
        for option in result["options"]
    )
    assert {option["method"] for option in result["options"]} == {"A", "B", "C"}


def test_exact_target_survives_no_ortools_fallback(
    exact_credit_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(planner_builder, "cp_model", None)
    result = build_plans(
        "1448",
        "1",
        [
            {"course_code": "TG401", "credits": 4, "status": "Eligible"},
            {"course_code": "TG402", "credits": 4, "status": "Eligible"},
            {"course_code": "TG101", "credits": 3, "status": "Eligible"},
            {"course_code": "TG102", "credits": 3, "status": "Eligible"},
        ],
        [],
        False,
        max_credits=8,
        target_credits=6,
        gender="M",
        program="TG",
        require_complete_meetings=True,
    )
    assert result["options"]
    assert {option["method"] for option in result["options"]} == {"A", "B", "C"}
    assert all(
        {row["course_code"] for row in option["mappings"]} == {"TG101", "TG102"}
        for option in result["options"]
    )


def test_unreachable_exact_target_is_a_bounded_typed_negative(
    exact_credit_world: dict[str, Any],
) -> None:
    result = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": [f"TG10{index}" for index in range(1, 7)],
            "max_credits": 18,
            "target_credits": 17,
        }
    )

    assert result["ok"] is True
    assert result["status"] == "CONSTRAINTS_UNSATISFIED"
    assert result["target_credit_status"] == "NO_EXACT_ALTERNATIVE"
    assert result["target_credits_satisfied"] is False
    assert result["constraints_satisfied"] is False
    assert result["alternatives"] == []
    assert result["search"]["bounded"] is True
    assert "bounded Planner A1-C3 search" in result["constraint_failures"][0]["reason"]


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_projected_exact_target_negative_composes_without_course_relation_leak(
    exact_credit_world: dict[str, Any], language: str
) -> None:
    local = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": [f"TG10{index}" for index in range(1, 7)],
            "max_credits": 18,
            "target_credits": 17,
        }
    )
    remote = project_tool_result_for_remote("build_timetable_proposal", local, RemoteIdentityMap())
    answer, complete, scopes = _safe_v21_planned_answer(
        language,
        [remote],
        "",
        planned_tools=("build_timetable_proposal",),
    )
    assert complete is True
    assert answer
    assert not any(code in answer for code in ("TG101", "TG102", "TG103", "TG104"))
    assert (
        check_answer(
            answer,
            tool_results=[remote],
            question="أبغى جدول 17 ساعة",
            required_tools={"build_timetable_proposal"},
            known_course_codes=frozenset(f"TG10{index}" for index in range(1, 7)),
            evidence_scopes=(
                EvidenceValidationScope(
                    answer=scopes[0][1],
                    tool_results=(remote,),
                    required_tools=frozenset({"build_timetable_proposal"}),
                ),
            ),
        )
        == []
    )


def test_cross_field_target_failures_do_not_call_solver(
    exact_credit_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a proven target contradiction must not call the solver")

    monkeypatch.setattr("core.services.student_planner.build_student_options", forbidden)
    over_cap = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": ["TG101"],
            "max_credits": 18,
            "target_credits": 19,
        }
    )
    assert over_cap["target_credit_status"] == "TARGET_EXCEEDS_EFFECTIVE_MAX"
    assert over_cap["status"] == "CONSTRAINTS_UNSATISFIED"
    assert over_cap["alternatives"] == []

    for code in ("TG101", "TG102", "TG103", "TG104"):
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=exact_credit_world["sections"][code],
            source="scraper_timetable",
        )
    retained = _execute_build(
        {
            "mode": "around_current",
            "course_codes": [],
            "max_credits": 18,
            "target_credits": 9,
        }
    )
    assert retained["baseline_credit_hours"] == 12
    assert retained["target_credit_status"] == "RETAINED_BASELINE_EXCEEDS_TARGET"
    assert retained["status"] == "CONSTRAINTS_UNSATISFIED"
    assert retained["alternatives"] == []


@pytest.mark.parametrize(
    ("status", "arguments", "register_codes"),
    [
        (
            "TARGET_EXCEEDS_EFFECTIVE_MAX",
            {
                "mode": "from_scratch",
                "course_codes": ["TG101"],
                "max_credits": 18,
                "target_credits": 19,
            },
            (),
        ),
        (
            "RETAINED_BASELINE_EXCEEDS_TARGET",
            {
                "mode": "around_current",
                "course_codes": [],
                "max_credits": 18,
                "target_credits": 9,
            },
            ("TG101", "TG102", "TG103", "TG104"),
        ),
    ],
)
def test_projected_cross_field_target_negative_composes_and_checks(
    exact_credit_world: dict[str, Any],
    status: str,
    arguments: dict[str, Any],
    register_codes: tuple[str, ...],
) -> None:
    for code in register_codes:
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=exact_credit_world["sections"][code],
            source="scraper_timetable",
        )
    local = _execute_build(arguments)
    assert local["target_credit_status"] == status
    remote = project_tool_result_for_remote("build_timetable_proposal", local, RemoteIdentityMap())
    for language in ("Arabic", "English"):
        answer, complete, scopes = _safe_v21_planned_answer(
            language,
            [remote],
            "",
            planned_tools=("build_timetable_proposal",),
        )
        assert complete is True
        assert (
            check_answer(
                answer,
                tool_results=[remote],
                question="Build an exact-credit timetable.",
                required_tools={"build_timetable_proposal"},
                known_course_codes=frozenset(f"TG10{index}" for index in range(1, 7)),
                evidence_scopes=(
                    EvidenceValidationScope(
                        answer=scopes[0][1],
                        tool_results=(remote,),
                        required_tools=frozenset({"build_timetable_proposal"}),
                    ),
                ),
            )
            == []
        )


@pytest.mark.parametrize("invalid_target", [True, 0, -1, "not-a-number"])
def test_invalid_exact_target_is_rejected(
    exact_credit_world: dict[str, Any], invalid_target: Any
) -> None:
    result = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": ["TG101"],
            "max_credits": 18,
            "target_credits": invalid_target,
        }
    )
    assert result["ok"] is False
    assert "target_credits" in result["error"]


def test_retained_baseline_equal_to_target_is_satisfied_without_additions(
    exact_credit_world: dict[str, Any],
) -> None:
    for code in ("TG101", "TG102", "TG103", "TG104"):
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=exact_credit_world["sections"][code],
            source="scraper_timetable",
        )
    result = _execute_build(
        {
            "mode": "around_current",
            "course_codes": [],
            "max_credits": 18,
            "target_credits": 12,
        }
    )
    assert result["status"] == "NO_ADDITIONAL_COURSES"
    assert result["target_credit_status"] == "SATISFIED"
    assert result["target_credits_satisfied"] is True
    assert result["baseline_credit_hours"] == 12
    assert result["alternatives"] == []

    remote = project_tool_result_for_remote("build_timetable_proposal", result, RemoteIdentityMap())
    assert remote["no_additional_courses"] is True
    for language in ("Arabic", "English"):
        answer, complete, scopes = _safe_v21_planned_answer(
            language,
            [remote],
            "",
            planned_tools=("build_timetable_proposal",),
        )
        assert complete is True
        assert "12" in answer
        assert (
            check_answer(
                answer,
                tool_results=[remote],
                question="Keep my current timetable at exactly 12 credits.",
                required_tools={"build_timetable_proposal"},
                known_course_codes=frozenset(f"TG10{index}" for index in range(1, 7)),
                evidence_scopes=(
                    EvidenceValidationScope(
                        answer=scopes[0][1],
                        tool_results=(remote,),
                        required_tools=frozenset({"build_timetable_proposal"}),
                    ),
                ),
            )
            == []
        )


def test_incomplete_meeting_cannot_be_hidden_by_exact_credit_total(
    exact_credit_world: dict[str, Any],
) -> None:
    TermSectionMeeting.objects.filter(term_section=exact_credit_world["sections"]["TG106"]).delete()
    result = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": [f"TG10{index}" for index in range(1, 7)],
            "must_take_courses": ["TG106"],
            "max_credits": 18,
            "target_credits": 18,
        }
    )
    assert result["status"] == "CONSTRAINTS_UNSATISFIED"
    assert result["target_credit_status"] == "NO_EXACT_ALTERNATIVE"
    assert result["alternatives"] == []
    assert any(row["course_code"] == "TG106" for row in result["constraint_failures"])


@pytest.mark.parametrize(
    ("question", "values"),
    [
        ("أبي جدول 18 ساعة", [18]),
        ("أبغى جدول 18 ساعة", [18]),
        ("أريد جدول 18 ساعة", [18]),
        ("ودي جدول 18 ساعة", [18]),
        ("Build me an 18-credit timetable", [18]),
        ("Build schedule with exactly 18 credits", [18]),
        ("Build me a schedule for 18 credit hours", [18]),
        ("Make my timetable 18 credits", [18]),
        ("I want an 18 credit schedule", [18]),
        ("ما أبي جدول 18 ساعة؛ أبي جدول 15 ساعة", [15]),
        ("لا تسوي لي جدول 18 ساعة؛ سو لي جدول 15 ساعة", [15]),
        ("لا تسوي لي جدول 18 ساعة؛ خله بحد أقصى 15", []),
        ("تسوي لي جدول 18 ساعة", [18]),
        ("I do not want an 18-credit schedule; build a 15-credit schedule", [15]),
        ("Do not rebuild an 18-credit timetable; build a 15-credit timetable", [15]),
        ("Never recreate an 18-credit schedule; make a 15-credit schedule", [15]),
        ("عندي 15 ساعة حالياً", []),
        ("لو أخذت 12 ساعة بدل 18، هل أتأخر؟", []),
        ("أبغى أضيف مقرر 3 ساعات", []),
        ("I currently have 15 credits", []),
    ],
)
def test_target_extractor_is_positive_current_turn_and_role_bound(
    question: str, values: list[int]
) -> None:
    assert _v21_credit_values(question, _V21_TARGET_CREDIT_PATTERNS) == values


def test_negated_exact_target_does_not_hide_the_corrected_maximum() -> None:
    question = "لا تسوي لي جدول 18 ساعة؛ خله بحد أقصى 15"
    assert _v21_credit_values(question, _V21_TARGET_CREDIT_PATTERNS) == []
    assert _v21_credit_values(question, _V21_MAX_CREDIT_PATTERNS) == [15]


def test_v21_constraint_coverage_and_provenance_require_exact_target() -> None:
    question = "أبغى جدول 18 ساعة"
    assert _v21_missing_explicit_constraint_paths(
        _build_plan({"mode": "from_scratch"}), question
    ) == ("build_timetable_proposal.target_credits",)
    assert _v21_missing_explicit_constraint_paths(
        _build_plan({"mode": "from_scratch", "target_credits": 17}), question
    ) == ("build_timetable_proposal.target_credits",)
    assert (
        _v21_missing_explicit_constraint_paths(
            _build_plan({"mode": "from_scratch", "target_credits": 18}), question
        )
        == ()
    )

    contract = _v21_argument_provenance_contract(
        question,
        history=[],
        prior_presentation={},
        prior_course_names={},
    )
    assert (
        validate_capability_argument_provenance(
            "build_timetable_proposal",
            {"mode": "from_scratch", "target_credits": 18},
            contract=contract,
        )["target_credits"]
        == 18
    )
    with pytest.raises(TurnPlanProvenanceError):
        validate_capability_argument_provenance(
            "build_timetable_proposal",
            {"mode": "from_scratch", "target_credits": 17},
            contract=contract,
        )


def test_v21_schema_projection_and_legacy_v2_are_isolated() -> None:
    v2_builder = next(
        row
        for row in student_v2_tool_schemas()
        if row["function"]["name"] == "build_timetable_proposal"
    )
    v21_builder = next(
        row
        for row in student_v21_tool_schemas()
        if row["function"]["name"] == "build_timetable_proposal"
    )
    assert "target_credits" not in v2_builder["function"]["parameters"]["properties"]
    target_schema = v21_builder["function"]["parameters"]["properties"]["target_credits"]
    assert target_schema["type"] == "integer"
    assert target_schema["minimum"] == 1
    assert _legacy_v2_arguments(
        "build_timetable_proposal", {"mode": "from_scratch", "target_credits": 18}
    ) == {"mode": "from_scratch"}

    legacy, _legacy_reasons = _normalise_timetable_proposal_args(
        "أبغى جدول 18 ساعة", {"mode": "from_scratch"}
    )
    semantic, _semantic_reasons = _normalise_timetable_proposal_args(
        "أبغى جدول 18 ساعة",
        {"mode": "from_scratch", "target_credits": 18},
        semantic_plan=True,
    )
    assert legacy["max_credits"] == 18
    assert "max_credits" not in semantic
    assert semantic["target_credits"] == 18
    target_and_cap, _both_reasons = _normalise_timetable_proposal_args(
        "أبغى جدول 15 ساعة بحد أقصى 18 ساعة",
        {"mode": "from_scratch", "target_credits": 15, "max_credits": 18},
        semantic_plan=True,
    )
    assert target_and_cap["target_credits"] == 15
    assert target_and_cap["max_credits"] == 18
    assert _v21_credit_values("بحد أقصى 18 ساعة", _V21_MAX_CREDIT_PATTERNS) == [18]

    projected = project_tool_result_for_remote(
        "build_timetable_proposal",
        {
            "tool": "build_timetable_proposal",
            "ok": True,
            "target_credits": 18,
            "target_credits_satisfied": False,
            "target_credit_status": "NO_EXACT_ALTERNATIVE",
            "search": {
                "bounded": True,
                "planner_methods": ["A", "B", "C", "INJECTED"],
                "alternatives_per_method": 3,
                "exact_target_enforced": True,
                "secret": "drop me",
            },
            "alternatives": [],
            "constraint_failures": [],
        },
        RemoteIdentityMap(),
    )
    assert projected["target_credits"] == 18
    assert projected["target_credits_satisfied"] is False
    assert projected["target_credit_status"] == "NO_EXACT_ALTERNATIVE"
    assert projected["search"] == {
        "bounded": True,
        "planner_methods": ["A", "B", "C"],
        "alternatives_per_method": 3,
        "exact_target_enforced": True,
    }


def test_exact_target_flows_through_presentation_and_consistency_checker(
    exact_credit_world: dict[str, Any],
) -> None:
    local = _execute_build(
        {
            "mode": "from_scratch",
            "course_codes": [f"TG10{index}" for index in range(1, 7)],
            "max_credits": 18,
            "target_credits": 18,
        }
    )
    remote = project_tool_result_for_remote("build_timetable_proposal", local, RemoteIdentityMap())
    presentation = timetable_presentation_from_tool_results([local])
    assert presentation is not None
    assert presentation["target_credits"] == 18
    assert presentation["target_credits_satisfied"] is True
    assert {row["total_credit_hours"] for row in presentation["alternatives"]} == {18}

    for language in ("Arabic", "English"):
        answer, complete, scopes = _safe_v21_planned_answer(
            language,
            [remote],
            "",
            planned_tools=("build_timetable_proposal",),
        )
        assert complete is True
        assert (
            check_answer(
                answer,
                tool_results=[remote],
                question="Build me an 18-credit timetable.",
                required_tools={"build_timetable_proposal"},
                known_course_codes=frozenset(f"TG10{index}" for index in range(1, 7)),
                presentation=presentation,
                evidence_scopes=(
                    EvidenceValidationScope(
                        answer=scopes[0][1],
                        tool_results=(remote,),
                        required_tools=frozenset({"build_timetable_proposal"}),
                        presentation=presentation,
                    ),
                ),
            )
            == []
        )


@pytest.mark.parametrize("language", ["Arabic", "English"])
def test_exact_target_failure_fact_fragment_is_closed_and_localized(language: str) -> None:
    row = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "mode": "from_scratch",
        "target_credits": 18,
        "target_credits_satisfied": False,
        "target_credit_status": "NO_EXACT_ALTERNATIVE",
        "credit_ceiling": 19,
        "constraints_satisfied": False,
        "constraint_failures": [
            {
                "course_code": "",
                "section_label": "",
                "reason": "INTERNAL ENGLISH DIAGNOSTIC MUST NOT LEAK",
            }
        ],
        "alternatives": [],
        "unplaced_courses": [],
        "baseline_sections": [],
        "baseline_credit_hours": 0,
        "no_additional_courses": False,
    }
    text = _safe_timetable_proposal_fact_fragment(language, row)
    assert "18" in text
    assert "INTERNAL ENGLISH DIAGNOSTIC" not in text
    if language == "Arabic":
        assert "البحث المحدود" in text
    else:
        assert "bounded" in text and "A1-C3 search" in text
