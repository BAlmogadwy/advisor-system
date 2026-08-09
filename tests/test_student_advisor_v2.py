"""Student Advisor V2: one read-only agent, never a registration surface."""

from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings

from core.models import Student
from core.services.advisor_principal import AdvisorPrincipal
from core.services.llm_backend import ChatResult, LLMTimeout, ToolCallRequest, ToolChatResult
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import (
    FORBIDDEN_STUDENT_V2_TOOLS,
    STUDENT_V2_TOOL_NAMES,
    _humanise_internal_output_markers,
    _internal_output_markers,
    _normalise_graduation_scenario_args,
    _normalise_timetable_proposal_args,
    _policy_grounding,
    _requires_graduation_what_if,
    _requires_section_check,
    _requires_timetable_proposal,
    answer_student_advisor,
    answer_student_advisor_v2,
    execute_student_v2_tool,
    student_v2_tool_schemas,
)
from core.services.virtual_advisor_capabilities import get_default_registry

SID = 4901234
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _student_record() -> None:
    Student.objects.get_or_create(
        student_id=SID,
        defaults={"name": "V2 Test Student", "program": "CS", "section": "M"},
    )


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID)


def _tool_turn(name: str, arguments: dict[str, Any]) -> ToolChatResult:
    call = ToolCallRequest(
        id="call_1",
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )
    return ToolChatResult(
        content="",
        tool_calls=(call,),
        model="test-model",
        usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
    )


def _answer_turn(content: str) -> ToolChatResult:
    return ToolChatResult(
        content=content,
        tool_calls=(),
        model="test-model",
        usage={"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
        assistant_message={"role": "assistant", "content": content},
    )


class FakeClient:
    backend = "local"
    supports_assistant_prefill = True

    def __init__(self, *turns: ToolChatResult):
        self.turns = list(turns)
        self.schemas: list[list[dict[str, Any]]] = []
        self.messages: list[list[dict[str, Any]]] = []

    def resolve_model(self, requested_model=None):
        return requested_model or "test-model"

    def chat_with_tools(self, messages, *, tools, **kwargs):
        self.schemas.append(tools)
        self.messages.append(messages)
        return self.turns.pop(0)

    def chat(self, messages, **kwargs):  # pragma: no cover - rescue path is not used here
        raise AssertionError("forced final answer was not expected")


def test_v2_surface_is_small_self_scoped_and_read_only():
    assert not (set(STUDENT_V2_TOOL_NAMES) & FORBIDDEN_STUDENT_V2_TOOLS)
    assert "course_prerequisites" in STUDENT_V2_TOOL_NAMES
    registry = get_default_registry()
    assert all(registry.capabilities[name].read_only for name in STUDENT_V2_TOOL_NAMES)

    schemas = student_v2_tool_schemas()
    assert [schema["function"]["name"] for schema in schemas] == list(STUDENT_V2_TOOL_NAMES)
    for schema in schemas:
        parameters = schema["function"]["parameters"]
        assert "student_id" not in parameters.get("properties", {})
        assert "student_id" not in parameters.get("required", [])
    graduation_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "graduation_progress"
    )
    graduation_properties = graduation_schema["function"]["parameters"]["properties"]
    assert {
        "remove_current_courses",
        "add_current_courses",
        "search_better_replacements",
    } <= set(graduation_properties)


def test_v2_refuses_to_become_generic_when_the_student_record_is_missing():
    Student.objects.filter(student_id=SID).delete()
    client = FakeClient(_answer_turn("This must never be reached."))

    with pytest.raises(ValueError, match="No student record"):
        answer_student_advisor_v2(
            question="What can I take?",
            principal=_principal(),
            academic_year=1448,
            term=1,
            llm_client=client,
        )

    assert client.messages == []


def test_named_course_and_section_requires_fresh_section_evidence():
    assert _requires_section_check("ابغى شعبة M3 لمقرر CS285") is True
    assert _requires_section_check("Does section M3 of CS285 fit?") is True
    assert _requires_section_check("Tell me about CS285") is False


def _recorded_current_section_result(name: str) -> dict[str, Any]:
    return {
        "tool": name,
        "ok": True,
        "compared_against_term": "1448/1",
        "courses": [
            {
                "course_code": "CS285",
                "sections_on_file": 3,
                "currently_registered_sections": ["M3"],
                "clash_free": [
                    {
                        "section": "M3",
                        "meetings": ["SUN 13:00-14:15"],
                        "is_current_section": True,
                    }
                ],
                "clashing": [
                    {
                        "section": "M1",
                        "meetings": ["SUN 09:00-10:40"],
                        "is_current_section": False,
                        "conflicts": [],
                    },
                    {
                        "section": "M2",
                        "meetings": ["SUN 10:30-12:10"],
                        "is_current_section": False,
                        "conflicts": [],
                    },
                ],
                "status": "OK",
            }
        ],
    }


def test_section_question_cannot_copy_an_old_history_answer_without_a_tool(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute(name, arguments, *, principal, context=None):
        calls.append((name, arguments))
        return _recorded_current_section_result(name)

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    wrong = "لا توجد شعب مسجلة في النظام لمقرر CS285."
    correct = "الشعبة M3 لمقرر CS285 موجودة بالفعل في جدولك الحالي."
    client = FakeClient(
        _answer_turn(wrong),
        _tool_turn("my_clash_free_sections", {"course_code": "CS285"}),
        _answer_turn(correct),
    )

    result = answer_student_advisor_v2(
        question="ابغى شعبة M3 لمقرر CS285",
        principal=_principal(),
        academic_year=1448,
        term=1,
        history=[{"role": "assistant", "content": wrong}],
        llm_client=client,
    )

    assert calls == [("my_clash_free_sections", {"course_code": "CS285"})]
    assert result["answer"] == correct
    assert result["agent"]["section_grounding_required"] is True
    assert result["agent"]["section_tool_reprompted"] is True


def test_section_answer_is_revised_when_it_denies_returned_sections(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, *, principal, context=None: _recorded_current_section_result(name),
    )
    wrong = "لا توجد شعب مسجلة في النظام لمقرر CS285."
    correct = "الشعبة M3 لمقرر CS285 موجودة بالفعل في جدولك الحالي."
    client = FakeClient(
        _tool_turn("my_clash_free_sections", {"course_code": "CS285"}),
        _answer_turn(wrong),
        _answer_turn(correct),
    )

    result = answer_student_advisor_v2(
        question="ابغى شعبة M3 لمقرر CS285",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"] == correct
    assert result["agent"]["section_evidence_reprompted"] is True


def test_section_contradiction_falls_back_to_verified_server_text(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, *, principal, context=None: _recorded_current_section_result(name),
    )
    wrong = "لا توجد شعب مسجلة في النظام لمقرر CS285."
    client = FakeClient(
        _tool_turn("my_clash_free_sections", {"course_code": "CS285"}),
        _answer_turn(wrong),
        _answer_turn(wrong),
    )

    result = answer_student_advisor_v2(
        question="ابغى شعبة M3 لمقرر CS285",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "M3" in result["answer"]
    assert "موجودة بالفعل في جدولك الحالي" in result["answer"]
    assert "لا توجد شعب" not in result["answer"]
    assert result["agent"]["section_safe_fallback_used"] is True


def test_policy_questions_receive_server_prefetched_governing_evidence(monkeypatch):
    evidence = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.TEST",
                "statement_ar": "قاعدة مباشرة",
                "decision_use": "EXPLANATORY_ONLY",
            }
        ],
        "citable": [],
        "policies": [],
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2._seed_policy_evidence",
        lambda _question, _scope: (evidence, "retrieved"),
    )
    client = FakeClient(_answer_turn("هذه قاعدة مشروطة وليست حكماً على حالتك."))

    result = answer_student_advisor_v2(
        question="هل تنطبق القاعدة علي؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    prompt = client.messages[0][-1]["content"]
    assert "verified_policy_evidence" in prompt
    assert "TU.TEST" in prompt
    assert result["agent"]["policy_prefetched"] is True
    assert result["agent"]["policy_grounding"] == "retrieved"


def test_prefetched_policy_gap_does_not_abstain_a_pure_timetable_question():
    required, grounding = _policy_grounding(
        "Build around my current sections",
        [{"tool": "policy_lookup", "ok": True, "policies": []}],
    )

    assert required is False
    assert grounding == "none_matched"


@pytest.mark.parametrize(
    "question",
    [
        "لا تبني لي جدولًا يتجاوز 15 ساعة.",
        "لا تغيّر الشعب التي اخترتها يدويًا، لكن غيّر باقي المقررات.",
        "Build a timetable with no more than 15 credits.",
    ],
)
def test_explicit_timetable_edits_require_real_planner_evidence(question):
    assert _requires_timetable_proposal(question) is True


@pytest.mark.parametrize(
    ("question", "model_arguments", "expected", "reasons"),
    [
        (
            "لا تبني لي جدولًا يتجاوز 15 ساعة.",
            {},
            {"max_credits": 15},
            ["explicit_credit_cap"],
        ),
        (
            "ابنِ أخف جدول بحد أقصى 12 ساعة.",
            {"max_credits": 18},
            {"max_credits": 12},
            ["explicit_credit_cap"],
        ),
        (
            "ابنِ جدولًا من الصفر وتجاهل جدولي الحالي.",
            {"mode": "around_current"},
            {"mode": "from_scratch"},
            ["explicit_from_scratch"],
        ),
        (
            "ابنِ حول جدولي الحالي وخلي الشعب كما هي.",
            {"mode": "from_scratch"},
            {"mode": "around_current"},
            ["explicit_around_current"],
        ),
    ],
)
def test_timetable_arguments_follow_explicit_student_constraints(
    question, model_arguments, expected, reasons
):
    arguments, normalisations = _normalise_timetable_proposal_args(question, model_arguments)

    assert arguments == expected
    assert normalisations == reasons


def test_internal_evidence_labels_are_rewritten_for_the_student(monkeypatch):
    evidence = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.AMBIGUOUS",
                "statement_ar": "قاعدة غير محسومة.",
                "decision_use": "PROHIBITED_FOR_DECISION",
                "source_is_unclear_on": "The source does not settle the point.",
            }
        ],
        "citable": [],
        "policies": [],
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2._seed_policy_evidence",
        lambda _question, _scope: (evidence, "retrieved"),
    )
    client = FakeClient(
        _answer_turn(
            "The record says source_leaves_unresolved: true and "
            "decision_use: PROHIBITED_FOR_DECISION."
        ),
        _answer_turn(
            "The source leaves this point unresolved, and it cannot decide your individual case."
        ),
    )

    result = answer_student_advisor_v2(
        question="هل تحسم هذه القاعدة حالتي؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert _internal_output_markers(result["answer"]) == []
    assert result["agent"]["internal_output_reprompted"] is True
    assert result["agent"]["internal_output_sanitized"] is False


def test_persistent_internal_labels_are_humanised_without_shipping_schema_names():
    answer = (
        "السجل يحمل `source_leaves_unresolved: true` و"
        "`decision_use: PROHIBITED_FOR_DECISION` والسبب NOT_ON_FILE. "
        "استُخدم build_timetable_proposal مع max_credits=15 ووضع around_current، "
        "والحقل sole_remaining_prerequisite."
    )

    cleaned = _humanise_internal_output_markers(answer, "Arabic")

    assert _internal_output_markers(cleaned) == []
    assert "المصدر يترك هذه النقطة غير محسومة" in cleaned
    assert "غير مسجل في بيانات النظام" in cleaned
    assert "منشئ مقترح الجدول" in cleaned
    assert "الحد الأقصى للساعات" in cleaned
    assert "المتطلب السابق الوحيد المتبقي" in cleaned


def test_an_unresolved_policy_forces_one_bounded_uncertainty_revision(monkeypatch):
    evidence = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.AMBIGUOUS",
                "statement_ar": "يرصد الرمز في السجل.",
                "decision_use": "EXPLANATORY_ONLY",
                "source_is_unclear_on": "The source does not settle its GPA effect.",
            }
        ],
        "citable": [],
        "policies": [],
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2._seed_policy_evidence",
        lambda _question, _scope: (evidence, "retrieved"),
    )
    client = FakeClient(
        _answer_turn("The symbol definitely has no GPA effect."),
        _answer_turn("The source does not settle the symbol's GPA effect."),
    )

    result = answer_student_advisor_v2(
        question="هل يؤثر الرمز على المعدل؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("The source does not settle")
    assert result["agent"]["policy_uncertainty_reprompted"] is True
    assert result["agent"]["iterations"] == 2


def _incomplete_graduation_result() -> dict[str, Any]:
    return {
        "tool": "graduation_progress",
        "ok": True,
        "simulation_completed": False,
        "lower_bound_additional_terms": 5,
        "lower_bound_terms_including_current": 6,
        "estimated_additional_terms": None,
        "estimated_terms_including_current": None,
        "max_credits_per_term": 18,
        "current_courses_assumed_passed": [{"code": "AI113", "credits": 3}],
        "unresolved_requirements": [
            {
                "code": "DS492",
                "missing_course_prerequisites": [],
                "credit_hour_gate": {
                    "required": 147,
                    "effective_in_scenario": 140,
                    "remaining": 7,
                },
            },
            {
                "code": "MATH471",
                "missing_course_prerequisites": ["MATH204"],
                "credit_hour_gate": None,
            },
        ],
    }


def _complete_lower_bound_answer() -> str:
    return (
        "The lower bound is at least 5 additional terms, or 6 including the "
        "current term. DS492 still needs the 147-credit gate, and MATH471 still "
        "needs MATH204. This assumes first-attempt passes and at most 18 credits "
        "per main term; offerings, seats, and registration permission are not guaranteed."
    )


def test_graduation_question_cannot_answer_before_calling_the_scenario(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _incomplete_graduation_result(),
    )
    client = FakeClient(
        _answer_turn("Your plan is 37% complete; ask the registrar for a term estimate."),
        _tool_turn("graduation_progress", {}),
        _answer_turn(_complete_lower_bound_answer()),
    )

    result = answer_student_advisor_v2(
        question="How many terms do I have left until graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("The lower bound is 5 additional terms")
    assert result["agent"]["graduation_grounding_required"] is True
    assert result["agent"]["graduation_tool_reprompted"] is True
    assert result["agent"]["graduation_safe_fallback_used"] is True
    assert result["agent"]["iterations"] == 3


@pytest.mark.parametrize(
    "question",
    [
        "If I did not take DS341 this term, would it delay my graduation?",
        "If I take MATH204 instead of DS341, will graduation be affected?",
        "Based on my current timetable, is there any course I can replace to improve graduation?",
        "إذا لم آخذ DS341 هذا الفصل، هل سيؤثر ذلك على تخرجي؟",
        "لو أخذت MATH204 بدل DS341 هل يتأخر التخرج؟",
        "هل فيه مقرر أقدر أستبدله عشان أتخرج أفضل؟",
    ],
)
def test_current_course_graduation_questions_require_what_if_evidence(question):
    assert _requires_graduation_what_if(question) is True


@pytest.mark.parametrize(
    ("question", "model_arguments", "expected", "reason"),
    [
        (
            "إذا ما أخذت DS225 هذا الترم، هل يؤثر على مدة إنهاء خطتي؟",
            {"add_current_courses": ["DS225"]},
            {"remove_current_courses": ["DS225"]},
            "explicit_omission",
        ),
        (
            "If I did not take DS-225 this term, would graduation be delayed?",
            {"add_current_courses": ["ds-225"]},
            {"remove_current_courses": ["DS225"]},
            "explicit_omission",
        ),
        (
            "إذا أخذت MATH204 بدل DS225 هذا الترم، هل تتحسن مدة إنهاء خطتي؟",
            {"remove_current_courses": ["MATH204"], "add_current_courses": ["DS225"]},
            {
                "remove_current_courses": ["DS225"],
                "add_current_courses": ["MATH204"],
            },
            "explicit_replacement",
        ),
        (
            "Replace DS225 with MATH204 to improve graduation.",
            {},
            {
                "remove_current_courses": ["DS225"],
                "add_current_courses": ["MATH204"],
            },
            "explicit_replacement",
        ),
        (
            "هل يوجد مقرر أستبدله حتى تتحسن خطة التخرج؟",
            {},
            {"search_better_replacements": True},
            "open_replacement_search",
        ),
    ],
)
def test_graduation_scenario_arguments_follow_explicit_student_wording(
    question, model_arguments, expected, reason
):
    arguments, normalisation = _normalise_graduation_scenario_args(question, model_arguments)

    assert arguments == expected
    assert normalisation == reason


def test_colloquial_arabic_omission_overrides_reversed_model_arguments(monkeypatch):
    graduation = {
        **_incomplete_graduation_result(),
        "what_if": {
            "mode": "explicit_changes",
            "valid": False,
            "validation_errors": [{"kind": "NOT_IN_CURRENT_TIMETABLE", "course_code": "DS225"}],
        },
    }
    executed = []

    def fake_execute(name, arguments, **kwargs):
        executed.append((name, dict(arguments)))
        return graduation

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("graduation_progress", {"add_current_courses": ["DS225"]}),
        _answer_turn("The scenario could not be run."),
    )

    result = answer_student_advisor_v2(
        question="إذا ما أخذت DS225 هذا الترم، هل يؤثر على مدة إنهاء خطتي؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert executed == [("graduation_progress", {"remove_current_courses": ["DS225"]})]
    assert result["agent"]["tools_called"][0] == {
        "name": "graduation_progress",
        "arguments": {"remove_current_courses": ["DS225"]},
        "scenario_normalization": "explicit_omission",
    }


def test_current_course_what_if_normalises_initial_baseline_call(monkeypatch):
    baseline = _incomplete_graduation_result()
    scenario = {
        **baseline,
        "what_if": {
            "mode": "explicit_changes",
            "valid": False,
            "validation_errors": [{"kind": "NOT_IN_CURRENT_TIMETABLE", "course_code": "DS341"}],
        },
    }
    executed = []

    def fake_execute(name, arguments, **kwargs):
        executed.append((name, dict(arguments)))
        if arguments.get("remove_current_courses") == ["DS341"]:
            return scenario
        return baseline

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn("Your unchanged timetable needs at least five terms."),
        _tool_turn("graduation_progress", {"remove_current_courses": ["DS341"]}),
        _answer_turn("DS341 is not in your current timetable."),
    )

    result = answer_student_advisor_v2(
        question="If I did not take DS341 this term, would it delay my graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert executed[-1] == (
        "graduation_progress",
        {"remove_current_courses": ["DS341"]},
    )
    assert "DS341 is not in the recorded current timetable" in result["answer"]
    assert result["agent"]["graduation_what_if_required"] is True
    assert result["agent"]["graduation_what_if_reprompted"] is False
    assert result["agent"]["graduation_what_if_missing"] is False


def test_current_course_what_if_fails_closed_if_model_never_runs_scenario(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _incomplete_graduation_result(),
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn("The unchanged baseline is five terms."),
        _answer_turn(_complete_lower_bound_answer()),
    )

    result = answer_student_advisor_v2(
        question="If I did not take DS341 this term, would it delay my graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "could not be run" in result["answer"]
    assert "lower bound" not in result["answer"]
    assert result["agent"]["graduation_what_if_missing"] is True


def test_incomplete_graduation_answer_must_keep_lower_bound_and_blockers(monkeypatch):
    graduation = _incomplete_graduation_result()
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: graduation,
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn("No precise estimate was returned. Ask the registrar."),
        _answer_turn(_complete_lower_bound_answer()),
    )

    result = answer_student_advisor_v2(
        question="How many terms do I have left until graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("The lower bound is 5 additional terms")
    assert result["agent"]["graduation_reprompted"] is True
    assert result["agent"]["graduation_safe_fallback_used"] is True
    assert result["agent"]["iterations"] == 3
    correction = client.messages[2][-1]["content"]
    assert "do not infer that one requires an extra term or special arrangement" in correction
    assert "no available time, place, section, or offering" in correction
    assert "scenario cap" in correction


def test_unsupported_graduation_inference_falls_back_to_exact_tool_facts(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _incomplete_graduation_result(),
    )
    speculative = (
        _complete_lower_bound_answer()
        + " DS492 may require an additional term or a special arrangement."
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn(speculative),
    )

    result = answer_student_advisor_v2(
        question="How many terms do I have left until graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "special arrangement" not in result["answer"]
    assert "DS492: 147-credit gate" in result["answer"]
    assert result["agent"]["graduation_safe_fallback_used"] is True


def test_arabic_allowed_load_or_missing_place_claim_uses_safe_summary(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _incomplete_graduation_result(),
    )
    unsafe_arabic = (
        "الحد الأدنى هو 5 فصول إضافية أو 6 مع الفصل الحالي. DS492 يحتاج 147 ساعة، "
        "وMATH471 يحتاج MATH204. استخدم الحد الأقصى المسموح وهو 18 ساعة، ولم يظهر "
        "للمقرر مكان في الفصول المحاكية."
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn(unsafe_arabic),
    )

    result = answer_student_advisor_v2(
        question="كم فصل متبقي لي حتى التخرج؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "الحد الأقصى المسموح" not in result["answer"]
    assert "مكان في الفصول" not in result["answer"]
    assert "سيناريو للقراءة فقط" in result["answer"]
    assert result["agent"]["graduation_safe_fallback_used"] is True


def test_current_term_what_if_answer_uses_structured_comparison(monkeypatch):
    graduation = {
        **_incomplete_graduation_result(),
        "what_if": {
            "mode": "explicit_changes",
            "valid": True,
            "validation_errors": [],
            "removed_current_courses": [{"code": "DS341", "credits": 3}],
            "added_current_courses": [{"code": "MATH204", "credits": 3, "in_degree_plan": False}],
            "outside_plan_additions": [{"code": "MATH204", "credits": 3, "in_degree_plan": False}],
            "baseline": {
                "lower_bound_additional_terms": 5,
                "registered_credits_now": 18,
            },
            "scenario": {
                "lower_bound_additional_terms": 5,
                "registered_credits_now": 18,
            },
            "comparison": {
                "timing_effect": "UNRESOLVED_IMPROVEMENT",
                "term_difference": None,
                "terms_saved": None,
                "blockers_resolved": [{"code": "MATH471"}],
                "blockers_improved": [{"code": "DS492"}],
                "blockers_introduced": [],
            },
            "timetable_check_required": True,
        },
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: graduation,
    )
    client = FakeClient(
        _tool_turn(
            "graduation_progress",
            {
                "remove_current_courses": ["DS341"],
                "add_current_courses": ["MATH204"],
            },
        ),
        _answer_turn("It should be better."),
    )

    result = answer_student_advisor_v2(
        question="If I replace DS341 with MATH204, will graduation be earlier?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "Current-term scenario: remove DS341 and add MATH204" in result["answer"]
    assert "does not yet prove earlier graduation" in result["answer"]
    assert "Blockers resolved in the simulation: MATH471" in result["answer"]
    assert "outside the degree-plan requirements" in result["answer"]
    assert "No course was removed, added, or registered" in result["answer"]
    assert result["agent"]["graduation_safe_fallback_used"] is True


def test_replacement_search_answer_does_not_claim_timetable_feasibility(monkeypatch):
    graduation = {
        **_incomplete_graduation_result(),
        "what_if": {
            "mode": "replacement_search",
            "valid": True,
            "validation_errors": [],
            "improving_replacements": [
                {
                    "remove_course": {"code": "DS341", "credits": 3},
                    "add_course": {
                        "code": "MATH204",
                        "credits": 3,
                        "in_degree_plan": False,
                    },
                    "comparison": {
                        "timing_effect": "UNRESOLVED_IMPROVEMENT",
                        "term_difference": None,
                        "terms_saved": None,
                        "blockers_resolved": [{"code": "MATH471"}],
                        "blockers_improved": [{"code": "DS492"}],
                    },
                }
            ],
            "timetable_check_required": True,
        },
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: graduation,
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {"search_better_replacements": True}),
        _answer_turn("Replace DS341; MATH204 fits your timetable."),
    )

    result = answer_student_advisor_v2(
        question="Can I replace a current course so graduation will be better?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "DS341 → MATH204" in result["answer"]
    assert "does not yet prove earlier graduation" in result["answer"]
    assert "Resolves: MATH471" in result["answer"]
    assert "Improves without fully resolving: DS492" in result["answer"]
    assert "Check sections and clashes with the timetable tool" in result["answer"]
    assert "fits your timetable" not in result["answer"]
    assert result["agent"]["tools_called"] == [
        {
            "name": "graduation_progress",
            "arguments": {"search_better_replacements": True},
        }
    ]
    assert result["agent"]["iterations"] == 1
    assert len(client.messages) == 1


def test_replacement_search_rejects_partial_blocker_progress_in_final_answer(monkeypatch):
    graduation = {
        **_incomplete_graduation_result(),
        "what_if": {
            "mode": "replacement_search",
            "valid": True,
            "validation_errors": [],
            "unproven_blocker_progress_pairs": 5,
            "improving_replacements": [],
            "no_proven_improvement": True,
            "timetable_check_required": False,
        },
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: graduation,
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {"search_better_replacements": True}),
        _answer_turn("Replace DS225 with MATH204."),
    )

    result = answer_student_advisor_v2(
        question="Can I replace a current course so graduation will be better?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "no one-for-one replacement proven" in result["answer"]
    assert "rejected 5 replacement(s)" in result["answer"]
    assert "complete graduation path did not improve" in result["answer"]
    assert "Replace DS225 with MATH204" not in result["answer"]
    assert result["agent"]["graduation_safe_fallback_used"] is True


def test_bad_policy_citation_is_removed_by_verified_graduation_summary(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _incomplete_graduation_result(),
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn(_complete_lower_bound_answer() + " [TU.FAKE.POLICY]"),
    )

    result = answer_student_advisor_v2(
        question="How many terms do I have left until graduation?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["graduation_safe_fallback_used"] is True
    assert result["answer"].startswith("The lower bound is 5 additional terms")
    assert "TU.FAKE.POLICY" not in result["answer"]


@pytest.mark.parametrize("tool_name", sorted(FORBIDDEN_STUDENT_V2_TOOLS))
def test_v2_refuses_timetable_registration_and_save_tools(tool_name):
    result = execute_student_v2_tool(tool_name, {}, principal=_principal())
    assert result == {
        "tool": tool_name,
        "ok": False,
        "error": "This capability is not available.",
    }


def test_single_agent_calls_evidence_tool_and_forces_session_identity(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_execute(name, arguments, *, principal, context=None):
        seen.update(
            name=name,
            arguments=arguments,
            student_id=principal.student_id,
            context=context,
        )
        return {"tool": name, "ok": True, "summary": {"open_courses": 3}}

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("my_progress", {"student_id": 9999999}),
        _answer_turn("You currently have three prerequisite-ready courses."),
    )

    result = answer_student_advisor_v2(
        question="What can I take next?",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("You currently have three")
    assert seen == {
        "name": "my_progress",
        "arguments": {},
        "student_id": SID,
        "context": {"academic_year": 1448, "term": 1},
    }
    assert result["agent"]["version"] == "student-v2"
    assert result["agent"]["read_only"] is True
    assert result["agent"]["portal_action"] == "student_manual_only"


def test_timetable_build_is_grounded_in_the_real_planner_tool(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute(name, arguments, *, principal, context=None):
        calls.append((name, arguments))
        return {
            "tool": name,
            "ok": True,
            "mode": arguments.get("mode"),
            "alternatives": [
                {
                    "option": 1,
                    "planner_options": ["A1", "B1"],
                    "courses": [{"course_code": "CS211", "section": "M2"}],
                    "meetings": [
                        {
                            "course_code": "CS211",
                            "section": "M2",
                            "day": "MON",
                            "start": "10:30",
                            "end": "11:45",
                        }
                    ],
                }
            ],
            "can_save": False,
            "can_register": False,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _answer_turn("I cannot view real section timetables."),
        _tool_turn("build_timetable_proposal", {"mode": "around_current"}),
        _answer_turn("Planner A1/B1 adds CS211-M2 on Monday 10:30–11:45 without a clash."),
    )
    result = answer_student_advisor_v2(
        question="Build around my current sections",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("Planner A1/B1 adds CS211-M2")
    assert calls == [("build_timetable_proposal", {"mode": "around_current"})]
    assert result["agent"]["timetable_grounding_required"] is True
    assert result["agent"]["timetable_reprompted"] is True
    assert result["agent"]["tools_called"][-1]["name"] == "build_timetable_proposal"
    assert result["presentation"]["kind"] == "timetable_proposals"
    assert result["presentation"]["alternatives"][0]["planner_options"] == ["A1", "B1"]
    assert result["presentation"]["can_save"] is False
    assert result["presentation"]["can_register"] is False


def test_timetable_credit_ceiling_overrides_missing_model_argument(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute(name, arguments, *, principal, context=None):
        calls.append((name, arguments))
        return {
            "tool": name,
            "ok": True,
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "scheduled_courses": 4,
                    "target_courses": 4,
                    "courses": [],
                    "meetings": [],
                    "unplaced_courses": [],
                }
            ],
            "can_save": False,
            "can_register": False,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _answer_turn("The university policy permits up to 19 credits."),
        _tool_turn("build_timetable_proposal", {}),
        _answer_turn("Planner A1 contains four courses and stays within 15 credits."),
    )

    result = answer_student_advisor_v2(
        question="لا تبني لي جدولًا يتجاوز 15 ساعة.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert calls == [("build_timetable_proposal", {"max_credits": 15})]
    assert result["agent"]["timetable_grounding_required"] is True
    assert result["agent"]["timetable_reprompted"] is True
    assert result["agent"]["tools_called"][0]["argument_normalizations"] == ["explicit_credit_cap"]


def test_timetable_answer_is_revised_when_it_hides_exact_planner_identities(monkeypatch):
    def fake_execute(name, arguments, *, principal, context=None):
        return {
            "tool": name,
            "ok": True,
            "alternatives": [
                {
                    "option": 1,
                    "planner_options": ["A1", "B1", "C1"],
                    "scheduled_courses": 0,
                    "target_courses": 2,
                    "courses": [],
                    "meetings": [],
                    "unplaced_courses": [
                        {
                            "course_code": "CS211",
                            "reason_code": "NOT_ON_FILE",
                            "reason": "No section is recorded in our data.",
                        }
                    ],
                }
            ],
            "can_save": False,
            "can_register": False,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("build_timetable_proposal", {"mode": "around_current"}),
        _answer_turn("No additional courses can be added."),
        _answer_turn("Planner A1/B1/C1 placed 0 of 2 additions. CS211 has no section recorded."),
    )

    result = answer_student_advisor_v2(
        question="Build around my current sections",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("Planner A1/B1/C1 placed 0 of 2")
    assert result["agent"]["timetable_format_reprompted"] is True
    assert result["agent"]["iterations"] == 3


def test_timetable_answer_is_revised_when_variant_omission_is_called_impossible(
    monkeypatch,
):
    def fake_execute(name, arguments, *, principal, context=None):
        return {
            "tool": name,
            "ok": True,
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "scheduled_courses": 2,
                    "target_courses": 2,
                    "unplaced_courses": [],
                },
                {
                    "planner_options": ["A2"],
                    "scheduled_courses": 1,
                    "target_courses": 2,
                    "unplaced_courses": [
                        {
                            "course_code": "AI221",
                            "reason_code": "OMITTED_IN_THIS_VARIANT",
                            "reason": "Another generated variant placed this course.",
                        }
                    ],
                },
            ],
            "can_save": False,
            "can_register": False,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("build_timetable_proposal", {"mode": "from_scratch"}),
        _answer_turn(
            "Planner A1 placed 2/2 and Planner A2 placed 1/2 because no clash-free "
            "section arrangement could accommodate all courses."
        ),
        _answer_turn(
            "Planner A1 placed 2/2. Planner A2 placed 1/2; that variant omitted AI221, "
            "which A1 placed."
        ),
    )

    result = answer_student_advisor_v2(
        question="Create a new timetable from scratch",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("Planner A1 placed 2/2")
    assert "could accommodate all" not in result["answer"]
    assert result["agent"]["timetable_variant_reprompted"] is True
    assert result["agent"]["iterations"] == 3


def test_arabic_day_count_impossibility_is_rejected_for_a_finite_planner_search():
    from core.services.student_advisor_v2 import _misstates_variant_omission

    evidence = [
        {
            "tool": "build_timetable_proposal",
            "ok": True,
            "alternatives": [{"unplaced_courses": [{"reason_code": "OMITTED_IN_THIS_VARIANT"}]}],
        }
    ]

    assert _misstates_variant_omission(
        "لا يمكن جمع المواد في يومين أو ثلاثة أيام، وجميع البدائل الممكنة خمسة أيام.",
        evidence,
    )
    assert _misstates_variant_omission(
        "لا يتوفر أي ترتيب يجمع مقرراتك في ثلاثة أيام فقط.",
        evidence,
    )


def test_empty_recommendation_answer_is_revised_when_it_invents_a_reason(monkeypatch):
    def fake_execute(name, arguments, *, principal, context=None):
        return {
            "tool": name,
            "ok": True,
            "recommendations": [],
            "already_in_current_timetable": [{"course_code": "AI113"}],
            "recommendation_state": "NO_NEW_SYSTEM_RECOMMENDATION",
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("recommend_courses", {}),
        _answer_turn("لا توجد مواد مفتوحة، وقد استوفيت الحد الاستشاري."),
        _answer_turn(
            "لا توجد توصية جديدة من النظام لهذا الفصل. إذا أردت فحص مقرر محدد فأرسل رمزه."
        ),
    )

    result = answer_student_advisor_v2(
        question="عندي ساعات فاضية، وش مادة أقدر أسجل؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("لا توجد توصية جديدة")
    assert result["agent"]["recommendation_reprompted"] is True


def test_structured_presentation_carries_times_without_forcing_duplicate_prose(monkeypatch):
    def fake_execute(name, arguments, *, principal, context=None):
        return {
            "tool": name,
            "ok": True,
            "alternatives": [
                {
                    "option": 1,
                    "planner_options": ["A1", "B1", "C1"],
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "courses": [{"course_code": "CS211", "section": "M2"}],
                    "meetings": [
                        {
                            "course_code": "CS211",
                            "section": "M2",
                            "day": "MON",
                            "start": "10:30",
                            "end": "11:45",
                        }
                    ],
                    "unplaced_courses": [],
                }
            ],
            "can_save": False,
            "can_register": False,
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn("build_timetable_proposal", {"mode": "from_scratch"}),
        _answer_turn("Planner A1/B1/C1 schedules CS211-M2."),
    )

    result = answer_student_advisor_v2(
        question="Create a new timetable from scratch",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"] == "Planner A1/B1/C1 schedules CS211-M2."
    meeting = result["presentation"]["alternatives"][0]["meetings"][0]
    assert (meeting["day"], meeting["start"], meeting["end"]) == (
        "MON",
        "10:30",
        "11:45",
    )
    assert result["agent"]["timetable_format_reprompted"] is False
    assert result["agent"]["iterations"] == 2


def test_remote_graduation_projection_keeps_the_scenario_but_not_student_identity():
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    projected = project_tool_result_for_remote(
        "graduation_progress",
        {
            "ok": True,
            "student_id": 4610192,
            "program": "DS2",
            "max_credits_per_term": 18,
            "lower_bound_additional_terms": 5,
            "simulation_completed": False,
            "current_courses_assumed_passed": [
                {
                    "code": "AI113",
                    "name": "AI Fundamentals",
                    "credits": 3,
                    "section": "M1",
                }
            ],
            "unresolved_requirements": [
                {
                    "code": "MATH471",
                    "missing_course_prerequisites": ["MATH204"],
                    "missing_prerequisites_outside_plan": ["MATH204"],
                    "credit_hour_gate": None,
                }
            ],
            "term_plan": [
                {
                    "sequence": 1,
                    "academic_year": 1448,
                    "term": 2,
                    "course_codes": ["GSE1"],
                    "credits": 2,
                    "courses": [{"code": "GSE1", "credits": 2}],
                }
            ],
            "scenario_graph": {
                "items": [
                    {
                        "course_code": "DS341",
                        "prerequisite_course_code": "DS225",
                    }
                ],
                "extraNodes": ["DS225", "DS341"],
            },
            "what_if": {
                "mode": "explicit_changes",
                "valid": True,
                "removed_current_courses": [{"code": "AI113", "credits": 3, "section": "M1"}],
                "added_current_courses": [
                    {
                        "code": "MATH204",
                        "credits": 3,
                        "in_degree_plan": False,
                        "section": "M9",
                        "source": "graduation_what_if",
                    }
                ],
                "baseline": {
                    "lower_bound_additional_terms": 5,
                    "registered_credits_now": 18,
                },
                "scenario": {
                    "lower_bound_additional_terms": 5,
                    "registered_credits_now": 18,
                },
                "comparison": {
                    "timing_effect": "UNRESOLVED_IMPROVEMENT",
                    "term_difference": None,
                    "blockers_resolved": [{"code": "MATH471"}],
                    "deferred_courses": [
                        {
                            "code": "AI113",
                            "future_sequence": 1,
                            "academic_year": 1448,
                            "term": 2,
                        }
                    ],
                },
            },
        },
        RemoteIdentityMap(),
    )

    assert "student_id" not in projected
    assert "scenario_graph" not in projected
    assert "section" not in projected["current_courses_assumed_passed"][0]
    assert projected["max_credits_per_term"] == 18
    assert projected["term_plan"][0]["course_codes"] == ["GSE1"]
    assert projected["unresolved_requirements"][0]["missing_prerequisites_outside_plan"] == [
        "MATH204"
    ]
    assert projected["what_if"]["comparison"]["timing_effect"] == ("UNRESOLVED_IMPROVEMENT")
    assert "section" not in projected["what_if"]["removed_current_courses"][0]
    assert "section" not in projected["what_if"]["added_current_courses"][0]
    assert "source" not in projected["what_if"]["added_current_courses"][0]


def test_remote_timetable_projection_keeps_planner_provenance_but_not_internal_ids():
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    projected = project_tool_result_for_remote(
        "build_timetable_proposal",
        {
            "ok": True,
            "tool": "build_timetable_proposal",
            "alternatives_generated": 9,
            "distinct_alternatives": 1,
            "alternatives": [
                {
                    "option": 1,
                    "planner_options": ["A1", "B1", "C1"],
                    "scheduled_courses": 1,
                    "target_courses": 2,
                    "courses": [
                        {
                            "course_code": "CS211",
                            "section": "M2",
                            "term_section_id": 987654,
                        }
                    ],
                    "meetings": [],
                    "unplaced_courses": [
                        {
                            "course_code": "AI221",
                            "reason_code": "ALL_SECTIONS_CLASH",
                            "reason": "Every recorded section clashes.",
                            "term_section_id": 123456,
                        }
                    ],
                }
            ],
        },
        RemoteIdentityMap(),
    )

    assert projected["alternatives_generated"] == 9
    assert projected["distinct_alternatives"] == 1
    assert projected["alternatives"][0]["planner_options"] == ["A1", "B1", "C1"]
    assert projected["alternatives"][0]["scheduled_courses"] == 1
    assert projected["alternatives"][0]["target_courses"] == 2
    assert projected["alternatives"][0]["unplaced_courses"][0]["course_code"] == "AI221"
    assert "term_section_id" not in str(projected)


def test_timetable_presentation_is_a_strict_student_visible_whitelist():
    from core.services.advisor_presentations import normalise_presentation

    safe = normalise_presentation(
        {
            "kind": "timetable_proposals",
            "student_id": 4901234,
            "can_save": True,
            "can_register": True,
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "term_section_id": 789,
                    "courses": [
                        {
                            "course_code": "CS211",
                            "section": "M2",
                            "term_section_id": 789,
                            "seat_count": 12,
                        }
                    ],
                    "meetings": [],
                    "unplaced_courses": [
                        {
                            "course_code": "AI221",
                            "reason_code": "INTERNAL_CODE",
                            "reason": "Compare another option.",
                        }
                    ],
                }
            ],
        }
    )

    serialised = str(safe)
    assert safe["can_save"] is False
    assert safe["can_register"] is False
    assert "student_id" not in serialised
    assert "term_section_id" not in serialised
    assert "seat_count" not in serialised
    assert "reason_code" not in serialised


def test_graduation_scenario_presentation_reuses_plan_edges_with_simulated_terms():
    from core.services.advisor_presentations import (
        graduation_presentation_from_tool_results,
        normalise_presentation,
    )

    safe = graduation_presentation_from_tool_results(
        [
            {
                "tool": "graduation_progress",
                "ok": True,
                "student_id": 4901234,
                "program": "DS2",
                "scenario_academic_year": 1448,
                "scenario_term": 1,
                "simulation_completed": False,
                "lower_bound_terms_including_current": 3,
                "max_credits_per_term": 18,
                "current_courses_assumed_passed": [
                    {"code": "DS225", "name": "Data Science", "credits": 4}
                ],
                "term_plan": [
                    {
                        "sequence": 1,
                        "academic_year": 1448,
                        "term": 2,
                        "courses": [{"code": "DS341", "name": "Governance", "credits": 3}],
                    }
                ],
                "unresolved_requirements": [
                    {
                        "code": "DS492",
                        "name": "Project",
                        "missing_course_prerequisites": ["MATH204"],
                        "credit_hour_gate": {"required": 147, "remaining": 7},
                        "internal_reason": "do not ship",
                    }
                ],
                "scenario_graph": {
                    "items": [
                        {
                            "course_code": "DS225",
                            "prerequisite_course_code": "CS113",
                        },
                        {
                            "course_code": "DS341",
                            "prerequisite_course_code": "DS225",
                        },
                        {
                            "course_code": "DS492",
                            "prerequisite_course_code": "DS341",
                        },
                    ],
                    "termOf": {"CS113": 2, "DS225": 5, "DS341": 7, "DS492": 10},
                    "nameOf": {"CS113": "Programming", "DS492": "Project"},
                    "statusOf": {
                        "CS113": "passed",
                        "DS225": "studying",
                        "DS341": "locked",
                        "DS492": "locked",
                    },
                    "extraNodes": ["CS113", "DS225", "DS341", "DS492"],
                    "database_id": 999,
                },
            }
        ]
    )

    assert safe["kind"] == "graduation_scenario"
    assert safe["read_only"] is True
    assert safe["graph"]["extraNodes"] == ["CS113", "DS225", "DS341"]
    assert safe["graph"]["termOf"] == {"CS113": 0, "DS225": 1, "DS341": 2}
    assert safe["graph"]["statusOf"] == {
        "CS113": "passed",
        "DS225": "studying",
        "DS341": "open",
    }
    assert safe["band_labels"]["2"] == "Projected 1448/2"
    assert safe["unresolved_requirements"][0]["code"] == "DS492"
    assert "student_id" not in str(safe)
    assert "database_id" not in str(safe)
    assert "internal_reason" not in str(safe)
    assert normalise_presentation(safe) == safe


def test_graduation_replacement_search_waits_for_one_selected_scenario_before_mapping():
    from core.services.advisor_presentations import (
        graduation_presentation_from_tool_results,
    )

    assert (
        graduation_presentation_from_tool_results(
            [
                {
                    "tool": "graduation_progress",
                    "ok": True,
                    "what_if": {"mode": "replacement_search", "valid": True},
                    "scenario_graph": {"extraNodes": ["DS225"]},
                }
            ]
        )
        == {}
    )


def test_from_scratch_presentation_does_not_label_baseline_as_retained():
    from core.services.advisor_presentations import normalise_presentation

    safe = normalise_presentation(
        {
            "kind": "timetable_proposals",
            "mode": "from_scratch",
            "current_sections": [{"course_code": "AI221", "section": "M1"}],
            "alternatives": [{"planner_options": ["A1"], "target_courses": 1}],
        }
    )

    assert safe["current_sections"] == []


def test_current_only_presentation_preserves_the_no_additional_course_state():
    from core.services.advisor_presentations import normalise_presentation

    safe = normalise_presentation(
        {
            "kind": "timetable_proposals",
            "mode": "around_current",
            "current_sections": [{"course_code": "AI113", "section": "M1"}],
            "alternatives": [],
            "no_additional_courses": True,
        }
    )

    assert safe["no_additional_courses"] is True
    assert safe["alternatives"] == []


def test_mixed_timetable_presentation_fails_closed():
    from core.services.advisor_presentations import normalise_presentation

    assert (
        normalise_presentation(
            {
                "kind": "timetable_proposals",
                "baseline_kind": "MIXED_REVIEW_REQUIRED",
                "baseline_sections": [{"course_code": "AI113", "section": "M1"}],
                "alternatives": [{"planner_options": ["A1"]}],
            }
        )
        == {}
    )


def test_safe_section_answer_refuses_a_mixed_baseline():
    from core.services.student_advisor_v2 import _safe_section_answer

    answer = _safe_section_answer(
        "English",
        [
            {
                "ok": True,
                "tool": "my_clash_free_sections",
                "baseline_kind": "MIXED_REVIEW_REQUIRED",
                "courses": [
                    {
                        "course_code": "AI113",
                        "sections_on_file": 1,
                        "currently_registered_sections": ["M1"],
                    }
                ],
            }
        ],
    )

    assert "both registrar and expected-plan rows" in answer
    assert "already in your current timetable" not in answer


def test_timetable_presentation_rejects_malformed_collection_shapes():
    from core.services.advisor_presentations import normalise_presentation

    assert (
        normalise_presentation(
            {
                "kind": "timetable_proposals",
                "current_sections": {"student_id": 4901234},
                "alternatives": {"A1": {"term_section_id": 789}},
            }
        )
        == {}
    )


def test_positive_portal_action_claim_is_never_shown():
    client = FakeClient(_answer_turn("I have registered you in AI331."))
    result = answer_student_advisor_v2(
        question="Register AI331 for me",
        principal=_principal(),
        llm_client=client,
    )
    assert "cannot register courses" in result["answer"]
    assert "main portal" in result["answer"]
    assert result["agent"]["portal_claim_refused"] is True


def test_tool_timeout_falls_back_to_verified_read_only_snapshot(monkeypatch):
    class TimeoutClient(FakeClient):
        def chat_with_tools(self, messages, *, tools, **kwargs):
            raise LLMTimeout("slow local model")

        def chat(self, messages, **kwargs):
            assert "Verified read-only student evidence" in messages[-2]["content"]
            return ChatResult(
                content="Your verified record shows two recommended courses.",
                model="test-model",
                usage={"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
            )

    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: {
            "tool": name,
            "ok": True,
            "recommendations": ["AI331", "CS372"],
        },
    )
    result = answer_student_advisor_v2(
        question="What should I take next?",
        principal=_principal(),
        llm_client=TimeoutClient(),
    )
    assert result["answer"].startswith("Your verified record")
    assert result["agent"]["tool_turn_error"] == "LLMTimeout"
    assert result["agent"]["fallback_seeded"] is True
    assert result["agent"]["tools_called"][-1]["name"] == "get_student_context"


@override_settings(STUDENT_ADVISOR_V2_ENABLED=False)
def test_feature_flag_preserves_legacy_generator(monkeypatch):
    expected = {"answer": "legacy", "model": "test"}
    monkeypatch.setattr(
        "core.services.virtual_advisor.answer_virtual_advisor", lambda **kwargs: expected
    )
    assert answer_student_advisor(question="hello", principal=_principal()) is expected


@override_settings(STUDENT_ADVISOR_V2_ENABLED=True)
def test_feature_flag_selects_v2_generator(monkeypatch):
    expected = {"answer": "v2", "model": "test"}
    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor_v2",
        lambda **kwargs: expected,
    )
    assert answer_student_advisor(question="hello", principal=_principal()) is expected
