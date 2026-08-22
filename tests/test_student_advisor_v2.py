"""Student Advisor V2: one read-only agent, never a registration surface."""

from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings

from core.models import Student
from core.services.advisor_principal import AdvisorPrincipal
from core.services.answer_consistency import (
    EXACT_ACADEMIC_FIGURE_MISMATCH,
    REQUESTED_EVIDENCE_OMITTED,
    UNSUPPORTED_ACADEMIC_FACT,
    check_answer,
)
from core.services.llm_backend import ChatResult, LLMTimeout, ToolCallRequest, ToolChatResult
from core.services.policy_contract import requires_policy_contract
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import (
    _GRADUATION_UNSUPPORTED_INFERENCE,
    _UNCERTAINTY_MARKERS,
    FORBIDDEN_STUDENT_V2_TOOLS,
    STUDENT_V2_TOOL_NAMES,
    _apply_saudi_register,
    _claims_portal_action,
    _explicit_comparison_year_term,
    _humanise_internal_output_markers,
    _internal_output_markers,
    _mislabels_planning_baseline_as_current,
    _misstates_variant_omission,
    _normalise_course_comparison_args,
    _normalise_feasible_replacement_args,
    _normalise_graduation_scenario_args,
    _normalise_timetable_proposal_args,
    _policy_grounding,
    _requires_course_choice_comparison,
    _requires_feasible_course_replacements,
    _requires_graduation_progress,
    _requires_graduation_what_if,
    _requires_section_check,
    _requires_timetable_proposal,
    _safe_course_comparison_answer,
    _safe_graduation_answer,
    _section_answer_contradicts_evidence,
    _speculates_about_empty_recommendations,
    answer_student_advisor,
    answer_student_advisor_v2,
    execute_student_v2_tool,
    student_v2_tool_schemas,
)
from core.services.virtual_advisor import _answer_style
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


class RepairClient(FakeClient):
    def __init__(self, *turns: ToolChatResult, repair: str):
        super().__init__(*turns)
        self.repair = repair
        self.repair_messages: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs):
        self.repair_messages.append(messages)
        return ChatResult(
            content=self.repair,
            model="test-model",
            usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
        )


def test_v2_surface_is_small_self_scoped_and_read_only():
    assert not (set(STUDENT_V2_TOOL_NAMES) & FORBIDDEN_STUDENT_V2_TOOLS)
    assert "course_prerequisites" in STUDENT_V2_TOOL_NAMES
    assert "course_choice_comparison" in STUDENT_V2_TOOL_NAMES
    assert "feasible_course_replacements" in STUDENT_V2_TOOL_NAMES
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
    assert graduation_properties["planning_baseline_kind"]["enum"] == [
        "recommended_current_term",
        "registered_timetable",
    ]
    assert "academic_year" not in graduation_properties
    assert "term" not in graduation_properties
    comparison_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "course_choice_comparison"
    )
    comparison_params = comparison_schema["function"]["parameters"]
    assert comparison_params["properties"]["course_codes"]["minItems"] == 2
    assert comparison_params["properties"]["course_codes"]["maxItems"] == 4
    assert "academic_year" not in comparison_params["properties"]
    assert "term" not in comparison_params["properties"]
    replacement_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "feasible_course_replacements"
    )
    assert set(replacement_schema["function"]["parameters"]["properties"]) == {
        "remove_course",
        "add_course",
    }


def test_replacement_capability_fails_closed_on_unexpected_service_error(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("database connection details must not escape")

    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        explode,
    )

    result = execute_student_v2_tool(
        "feasible_course_replacements",
        {"remove_course": "DS341", "add_course": "CS285"},
        principal=_principal(),
        context={"academic_year": 1448, "term": 1},
    )

    assert result == {
        "ok": False,
        "error": ("The verified replacement check could not be completed from the recorded data."),
        "tool": "feasible_course_replacements",
    }


def test_replacement_capability_uses_server_term_not_model_override(monkeypatch):
    seen = {}

    def capture(student_id, academic_year, term, **kwargs):
        seen.update(
            student_id=student_id,
            academic_year=academic_year,
            term=term,
            **kwargs,
        )
        return {"status": "NOT_DETERMINABLE", "certified_replacements": []}

    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        capture,
    )

    execute_student_v2_tool(
        "feasible_course_replacements",
        {
            "remove_course": "DS341",
            "add_course": "CS285",
            "academic_year": 1447,
            "term": 2,
        },
        principal=_principal(),
        context={"academic_year": 1448, "term": 1},
    )

    assert seen["academic_year"] == 1448
    assert seen["term"] == 1


def test_replacement_capability_fails_closed_when_requested_term_differs_from_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(
        "core.services.course_replacement_feasibility.find_feasible_course_replacements",
        lambda *_args, **_kwargs: pytest.fail(
            "term-mismatched replacement must not reach academic or timetable services"
        ),
    )

    result = get_default_registry().execute(
        "feasible_course_replacements",
        {
            "remove_course": "DS341",
            "add_course": "CS285",
            "academic_year": 1447,
            "term": 2,
        },
        scope=_principal().as_scope(),
        ctx={
            "academic_year": 1448,
            "term": 1,
            "section_snapshot_academic_year": 1448,
            "section_snapshot_term": 1,
        },
    )

    assert result["status"] == "NOT_DETERMINABLE"
    assert result["academic_year"] == 1447
    assert result["term"] == 2
    assert result["certified_replacements"] == []
    assert result["rejected_replacements"][0]["timetable"]["reason_code"] == (
        "SECTION_SNAPSHOT_TERM_MISMATCH"
    )


def test_comparison_capability_uses_server_term_not_model_override(monkeypatch):
    seen = {}

    def capture(student_id, course_codes, academic_year, term, **kwargs):
        seen.update(
            student_id=student_id,
            course_codes=course_codes,
            academic_year=academic_year,
            term=term,
            **kwargs,
        )
        return {"ok": True, "candidates": []}

    monkeypatch.setattr("core.services.course_choice_comparison.compare_course_choices", capture)

    execute_student_v2_tool(
        "course_choice_comparison",
        {
            "course_codes": ["AI331", "DS341"],
            "academic_year": 1447,
            "term": 2,
        },
        principal=_principal(),
        context={"academic_year": 1448, "term": 1},
    )

    assert seen["academic_year"] == 1448
    assert seen["term"] == 1


def test_legacy_comparison_context_treats_configured_term_as_section_snapshot(monkeypatch):
    seen = {}

    def capture(student_id, course_codes, academic_year, term, **kwargs):
        seen.update(
            student_id=student_id,
            course_codes=course_codes,
            academic_year=academic_year,
            term=term,
            **kwargs,
        )
        return {"ok": True, "candidates": []}

    monkeypatch.setattr("core.services.course_choice_comparison.compare_course_choices", capture)

    get_default_registry().execute(
        "course_choice_comparison",
        {"course_codes": ["AI331", "DS341"]},
        scope=_principal().as_scope(),
        ctx={"academic_year": 1448, "term": 1},
    )

    assert seen["academic_year"] == 1448
    assert seen["term"] == 1
    assert seen["timetable_evidence_available"] is True


def test_direct_comparison_override_cannot_reuse_configured_section_snapshot(monkeypatch):
    seen = {}

    def capture(student_id, course_codes, academic_year, term, **kwargs):
        seen.update(
            student_id=student_id,
            course_codes=course_codes,
            academic_year=academic_year,
            term=term,
            **kwargs,
        )
        return {"ok": True, "candidates": []}

    monkeypatch.setattr("core.services.course_choice_comparison.compare_course_choices", capture)

    get_default_registry().execute(
        "course_choice_comparison",
        {
            "course_codes": ["AI331", "DS341"],
            "academic_year": 1447,
            "term": 2,
        },
        scope=_principal().as_scope(),
        ctx={"academic_year": 1448, "term": 1},
    )

    assert seen["academic_year"] == 1447
    assert seen["term"] == 2
    assert seen["timetable_evidence_available"] is False


def test_partial_explicit_snapshot_provenance_fails_closed(monkeypatch):
    seen = {}

    def capture(student_id, course_codes, academic_year, term, **kwargs):
        seen.update(**kwargs)
        return {"ok": True, "candidates": []}

    monkeypatch.setattr("core.services.course_choice_comparison.compare_course_choices", capture)

    get_default_registry().execute(
        "course_choice_comparison",
        {"course_codes": ["AI331", "DS341"]},
        scope=_principal().as_scope(),
        ctx={
            "academic_year": 1448,
            "term": 1,
            "section_snapshot_academic_year": 1448,
        },
    )

    assert seen["timetable_evidence_available"] is False


def test_replacement_snapshot_mismatch_answer_does_not_claim_academic_evaluation():
    from core.services.student_advisor_v2 import _safe_feasible_replacement_answer

    answer = _safe_feasible_replacement_answer(
        "English",
        [
            {
                "ok": True,
                "tool": "feasible_course_replacements",
                "baseline_kind": "NOT_EVALUATED",
                "requested_remove_course": "DS341",
                "requested_add_course": "CS285",
                "certified_replacements": [],
                "rejected_replacements": [
                    {
                        "academic": {"status": "NOT_EVALUATED"},
                        "timetable": {
                            "status": "NOT_DETERMINABLE",
                            "reason_code": "SECTION_SNAPSHOT_TERM_MISMATCH",
                        },
                    }
                ],
            }
        ],
    )

    assert "does not belong to the requested term" in answer
    assert "stopped before running the academic-improvement simulation" in answer
    assert "Academic evidence was evaluable" not in answer


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


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("وش أقدر أسجل هالترم؟", "Conversational Saudi Arabic"),
        ("وأبغى أعرف جدولي", "Conversational Saudi Arabic"),
        ("فودي بجدول خفيف", "Conversational Saudi Arabic"),
        ("شلون أرتب جدولي؟", "Conversational Saudi Arabic"),
        ("أبيك ترتب لي الجدول", "Conversational Saudi Arabic"),
        ("سويلي جدول خفيف", "Conversational Saudi Arabic"),
        ("عادي أنزل 21 ساعة؟", "Conversational Saudi Arabic"),
        ("مب فاهم ليش المقرر مقفل", "Conversational Saudi Arabic"),
        ("كم ترم باقي لي؟", "Conversational Saudi Arabic"),
        ("جدولي الحالي فيه تعارضات؟", "Conversational Saudi Arabic"),
        ("ما المقررات المتاحة لي؟", "Professional Saudi Arabic"),
        ("Which courses can I take?", "Plain English"),
    ],
)
def test_answer_style_mirrors_saudi_register(question: str, expected: str):
    assert _answer_style(question) == expected


def test_v2_pins_formal_arabic_answer_style_in_the_model_message():
    client = FakeClient(_answer_turn("أكيد، وش تحتاج؟"))

    result = answer_student_advisor_v2(
        question="هلا، وأبغى مساعدة في خطتي",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    prompt = client.messages[0][-1]["content"]
    assert "answer_language: Arabic" in prompt
    expected_style = "Formal Modern Standard Arabic for a Saudi academic context"
    assert f"answer_style: {expected_style}" in prompt
    assert result["agent"]["answer_style"] == expected_style


@pytest.mark.parametrize(
    "question",
    [
        "سوِّ لي أكثر من خيار للجدول، مو خيار واحد بس.",
        "أبغى جدول يشمل AI352 وAI371.",
        "سوي لي جدول خفيف للترم الجاي.",
        "سويلي جدول خفيف للترم الجاي.",
        "زبط لي جدول ثلاثة أيام.",
        "أبي جدول ما فيه محاضرات بدري.",
        "ودي بجدول بدون فراغات.",
    ],
)
def test_saudi_timetable_requests_require_planner_evidence(question: str):
    assert _requires_timetable_proposal(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "مو أبي جدول",
        "مو أبي جدول جديد، أبي أعرف وش مسجل",
        "ما أبغى بدائل، اعرض جدولي الحالي",
        "اعرض لي جدولي المسجل حاليًا",
        "جدولي الحالي فيه تعارضات؟",
    ],
)
def test_non_building_saudi_timetable_questions_do_not_force_a_proposal(question: str):
    assert _requires_timetable_proposal(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "كم ترم باقي لي؟",
        "باقي لي كم فصل وأخلص الخطة؟",
        "متى أخلص من الخطة؟",
        "كم باقي وأصير خريج؟",
        "أبي أخلص بأسرع وقت ممكن، كم ترم أقل شي باقي لي؟",
        "كم فصل دراسي متبقٍ لي تقريبًا حتى أنهي متطلبات الخطة؟",
    ],
)
def test_saudi_graduation_questions_require_simulation_evidence(question: str):
    assert _requires_graduation_progress(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "متى أخلص اختباراتي؟",
        "كم مادة أخلص هالترم؟",
    ],
)
def test_non_graduation_completion_questions_do_not_start_a_simulation(question: str):
    assert _requires_graduation_progress(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "إذا شلت DS341 هالترم، متى أتخرج؟",
        "لو ما نزلت DS341 الحين، يطول تخرجي؟",
        "لو حطيت MATH204 بدال DS341، وش يصير على تخرجي؟",
        "أشيل DS341 وأحط MATH204 مكانها، أتخرج أسرع؟",
    ],
)
def test_saudi_current_course_changes_require_what_if_evidence(question: str):
    assert _requires_graduation_what_if(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "هل مكان محاضرة DS341 بيأثر على تخرجي؟",
        "حطيت DS341 في جدولي، متى أتخرج؟",
    ],
)
def test_non_change_graduation_questions_do_not_start_a_what_if(question: str):
    assert _requires_graduation_what_if(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "يمديني أسجل 21 ساعة؟",
        "عادي أنزل 21 ساعة؟",
        "يصير آخذ 21 ساعة؟",
        "من أكلم عشان أرفع عذر؟",
        "النظام مو راضي يسجل المادة",
    ],
)
def test_saudi_policy_questions_keep_policy_grounding(question: str):
    assert requires_policy_contract(question) is True


def test_bare_saudi_filler_does_not_create_a_policy_question():
    assert requires_policy_contract("عادي") is False


@pytest.mark.parametrize(
    "question",
    [
        "النظام مو راضي يعرض جدولي",
        "الصفحة ما تخليني أشوف المحاضرات",
        "وش أخذت الترم الماضي؟",
        "لو حطيت MATH204 بدال DS341، وش بيصير على تخرجي؟",
    ],
)
def test_saudi_data_and_technical_questions_do_not_become_policy_questions(question: str):
    assert requires_policy_contract(question) is False


def test_saudi_safety_phrasing_is_still_detected():
    assert _claims_portal_action("تم تسجيلك في AI331") is True
    assert _claims_portal_action("تمّ تسجيلك في AI331") is True
    assert _claims_portal_action("سويت لك التسجيل") is True
    assert _claims_portal_action("خلاص سجلتك في AI331") is True
    assert _claims_portal_action("لم يتم تسجيلك في AI331") is False
    assert _claims_portal_action("ما سجلتك في AI331") is False
    assert _GRADUATION_UNSUPPORTED_INFERENCE.search("يمكن يحتاج ترم زيادة")
    assert _UNCERTAINTY_MARKERS.search("الدليل ما وضّح هالنقطة")

    section_result = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "courses": [{"course_code": "CS285", "sections_on_file": 3}],
    }
    assert _section_answer_contradicts_evidence(
        "ما عندنا بيانات شعب لـ CS285.",
        [section_result],
    )
    assert not _section_answer_contradicts_evidence(
        "ما فيه تعارض في شعبة M3.",
        [section_result],
    )
    profile_filtered_result = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "courses": [
            {
                "course_code": "AI113",
                "sections_on_file": 0,
                "recorded_sections_on_file": 2,
                "status": "NOT_MATCHING_STUDENT_PROFILE",
            }
        ],
    }
    assert _section_answer_contradicts_evidence(
        "ما في شعب مسجلة لمادة AI113 في بيانات النظام حاليًا.",
        [profile_filtered_result],
    )
    assert _section_answer_contradicts_evidence(
        "الدليل الإرشادي للطالب ما يذكر قاعدة خاصة بالاستعلام عن الشعب.",
        [profile_filtered_result],
    )

    empty_recommendations = {
        "tool": "recommend_courses",
        "ok": True,
        "recommendations": [],
    }
    assert _speculates_about_empty_recommendations(
        "المواد مو متاحة هالترم.",
        [empty_recommendations],
    )

    variant_result = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "alternatives": [
            {
                "unplaced_courses": [
                    {"course_code": "AI352", "reason_code": "OMITTED_IN_THIS_VARIANT"}
                ]
            }
        ],
    }
    assert _misstates_variant_omission(
        "ما فيه أي خيار يجمع كل المواد.",
        [variant_result],
    )


def test_verified_fallback_remains_formal_for_any_arabic_input_register():
    formal = (
        "تقدّر المحاكاة أنك تحتاج إلى 3 فصول إضافية. "
        "الحد الأدنى هو فصلان، ولا يضمن الطرح المستقبلي أو المقاعد. "
        "هذا سيناريو للقراءة فقط، ولم يتغير سجلك."
    )

    unchanged = _apply_saudi_register(
        formal,
        "Arabic",
        "Conversational Saudi Arabic",
    )

    assert unchanged == formal
    assert _apply_saudi_register(formal, "Arabic", "Professional Saudi Arabic") == formal


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
    assert "الشعبة M3 للمقرر CS285 مدرجة في الجدول المسجّل فعليًا" in result["answer"]
    assert "لا توجد شعب" not in result["answer"]
    assert result["agent"]["section_safe_fallback_used"] is True


def test_profile_filtered_sections_are_explained_without_policy_diversion(monkeypatch):
    tool_result = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "compared_against_term": "1448/1",
        "baseline_kind": "REGISTERED",
        "courses": [
            {
                "course_code": "AI113",
                "sections_on_file": 0,
                "recorded_sections_on_file": 2,
                "currently_registered_sections": [],
                "clash_free": [],
                "clashing": [],
                "status": "NOT_MATCHING_STUDENT_PROFILE",
            }
        ],
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, *, principal, context=None: tool_result,
    )
    wrong = (
        "ما في شعب مسجلة لمادة AI113 في بيانات النظام حاليًا. "
        "الدليل الإرشادي للطالب ما يذكر قاعدة خاصة بالاستعلام عن الشعب."
    )
    client = FakeClient(
        _tool_turn("my_clash_free_sections", {"course_code": "AI113"}),
        _answer_turn(wrong),
        _answer_turn(wrong),
    )

    result = answer_student_advisor_v2(
        question="ما الشُعب المدرجة لمقرر AI113؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert "توجد للمقرر AI113 شُعب مدرجة في بيانات النظام وعددها 2" in result["answer"]
    assert "لا تطابق برنامجك أو شطر الدراسة المسجّل في ملفك" in result["answer"]
    assert "الدليل الإرشادي" not in result["answer"]
    assert "توفر مقعد" in result["answer"]
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
            "أبي جدول 15 ساعة.",
            {},
            {"max_credits": 15},
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


def test_timetable_gap_hours_are_not_misread_as_a_credit_cap():
    arguments, normalisations = _normalise_timetable_proposal_args(
        "أبغى جدول فيه 3 ساعات فراغ بين المحاضرات.",
        {},
    )

    assert arguments == {}
    assert normalisations == []


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
    assert "لم يحسم المصدر هذه النقطة، ولا تكفي هذه القاعدة للحكم على حالة الطالب" in cleaned
    assert "السبب غير مدرج في بيانات النظام" in cleaned
    assert "إنشاء جدول مقترح" in cleaned
    assert "بيانات الحد الأعلى للساعات=15" in cleaned
    assert "إنشاء المقترح مع الإبقاء على الجدول المرجعي" in cleaned
    assert "المتطلب السابق الوحيد المتبقي" in cleaned
    assert "السجل يحمل" not in cleaned


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
        "lower_bound_terms_including_planning_baseline": 6,
        "estimated_additional_terms": None,
        "estimated_terms_including_planning_baseline": None,
        "max_credits_per_term": 18,
        "planning_baseline_courses_assumed_passed": [{"code": "AI113", "credits": 3}],
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


def _complete_graduation_result() -> dict[str, Any]:
    return {
        "tool": "graduation_progress",
        "ok": True,
        "planning_baseline_academic_year": 1448,
        "planning_baseline_term": 1,
        "simulation_completed": True,
        "estimated_additional_terms": 5,
        "estimated_terms_including_planning_baseline": 6,
        "max_credits_per_term": 18,
        "planning_baseline_courses_assumed_passed": [
            {"code": "DS321", "credits": 3},
            {"code": "DS341", "credits": 3},
        ],
        "term_plan": [
            {
                "sequence": 1,
                "academic_year": 1448,
                "term": 2,
                "course_codes": ["DS331", "MATH204"],
                "credits": 7,
            },
            {
                "sequence": 2,
                "academic_year": 1449,
                "term": 1,
                "course_codes": ["CS211"],
                "credits": 4,
            },
        ],
        "unresolved_requirements": [],
    }


@pytest.mark.parametrize(
    ("language", "unsafe", "required", "forbidden"),
    [
        (
            "English",
            "You have 5 terms after the expected plan, or 6 including the current one.",
            "planning baseline (1448/1)",
            ("current one",),
        ),
        (
            "Arabic",
            "باقي 5 فصول بعد الفصل الحالي، أو 6 فصول شاملة فصلك الحالي.",
            "فصل المقررات المرجعية المستخدمة في المحاكاة (1448/1)",
            ("الفصل الحالي", "فصلك الحالي"),
        ),
    ],
)
def test_complete_graduation_answer_cannot_call_planning_baseline_current(
    monkeypatch, language, unsafe, required, forbidden
):
    graduation = _complete_graduation_result()
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: graduation,
    )
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn(unsafe),
        _answer_turn(unsafe),
    )

    result = answer_student_advisor_v2(
        question=(
            "كم فصل متبقي لي حتى التخرج؟"
            if language == "Arabic"
            else "How many terms do I have left until graduation?"
        ),
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert required in result["answer"]
    assert all(text not in result["answer"] for text in forbidden)
    assert "DS321" in result["answer"]
    assert "DS341" in result["answer"]
    assert "1448/2" in result["answer"]
    assert "DS331" in result["answer"]
    assert "1449/1" in result["answer"]
    assert result["agent"]["graduation_reprompted"] is True
    assert result["agent"]["graduation_safe_fallback_used"] is True
    assert result["agent"]["graduation_baseline_label_corrected"] is True


def test_graduation_current_wording_guard_ignores_unrelated_current_fact():
    graduation = _complete_graduation_result()

    assert (
        _mislabels_planning_baseline_as_current(
            "The student currently has 86 earned credits; the planning baseline has 6 credits.",
            graduation,
        )
        is False
    )
    assert "planning baseline (1448/1)" in _safe_graduation_answer("English", [graduation])
    assert (
        _mislabels_planning_baseline_as_current(
            "The planning baseline is not your current term.", graduation
        )
        is False
    )


def test_graduation_baseline_guard_distinguishes_recommendations_from_registration():
    recommended = {
        **_complete_graduation_result(),
        "planning_baseline_kind": "recommended_current_term",
    }
    registered = {
        **_complete_graduation_result(),
        "planning_baseline_kind": "registered_timetable",
    }

    assert (
        _mislabels_planning_baseline_as_current(
            "The scenario uses the actual registered timetable.", recommended
        )
        is True
    )
    assert (
        _mislabels_planning_baseline_as_current(
            "The scenario uses the actual registered timetable for the current term.",
            registered,
        )
        is False
    )
    assert (
        _mislabels_planning_baseline_as_current(
            "The scenario uses the recommended courses.", registered
        )
        is True
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "| **Current** | 1448/1 | DS321 |",
        "| **الحالي** | 1448/1 | DS321 |",
        "**Current** — 1448/1",
        "Current | 1448/1",
        "**الحالي** — 1448/1",
        "الحالي | 1448/1",
        "Your current term is 1448/1.",
        "According to the scenario, your current term is 1448/1.",
        "For this scenario, the current term is 1448/1.",
        "In this plan, your current term is 1448/1.",
        "The simulation treats your current semester as 1448/1.",
        "حسب السيناريو، ترمك الحالي هو 1448/1.",
        "The current semester (1448/1) includes DS321.",
        "الفصل الدراسي الحالي هو 1448/1.",
        "الفصل الدراسي الحالي 1448/1 يشمل DS321.",
        "ترمك الحالي: 1448/1.",
        "The planning baseline is your current term.",
        "The planning baseline is the same as your current semester.",
        "The forecast starts after the current planning baseline.",
        "الفصل المرجعي للتخطيط هو الفصل الحالي.",
        "الفصل المرجعي للتخطيط نفس الترم الحالي.",
        "باقي 5 فصول بعد الأساس التخطيطي الحالي، أو 6 فصول بما فيها الفصل الحالي.",
        "المجموع باحتساب هالترم 6 فصول.",
        "المجموع شاملًا هالفصل 6 فصول.",
        "المجموع بعد الترم ذا 5 فصول.",
        "المجموع بعد الفصل هذا 5 فصول.",
        "المجموع بعد فصلي الحالي 5 فصول.",
    ],
)
def test_planning_baseline_guard_covers_standalone_and_saudi_current_labels(unsafe):
    graduation = {
        **_complete_graduation_result(),
        "planning_baseline_courses_assumed_passed": [],
    }

    assert _mislabels_planning_baseline_as_current(unsafe, graduation) is True


@pytest.mark.parametrize(
    "safe",
    [
        "The planning baseline differs from your current term.",
        "Use the planning baseline rather than your current term.",
        "The planning baseline is unlike the current semester.",
        "الفصل المرجعي للتخطيط يختلف عن فصلك الحالي.",
        "الفصل المرجعي للتخطيط ليس هو الترم الحالي.",
        "The planning baseline is not in the current term.",
        "This forecast is not for the current semester.",
        "The estimate is not after the current term.",
        "The estimate is not including the current term.",
        "الفصل المرجعي للتخطيط ليس في الفصل الحالي.",
    ],
)
def test_planning_baseline_guard_preserves_explicit_contrasts(safe):
    assert _mislabels_planning_baseline_as_current(safe, _complete_graduation_result()) is False


@pytest.mark.parametrize(
    ("answer", "forbidden"),
    [
        (
            "The forecast is ready. I cannot generate or send images; see the plan below.\n\nPlan.",
            "cannot generate or send images",
        ),
        (
            "الخطة جاهزة. لا يمكنني إنشاء أو إرسال صور؛ شوف التفاصيل تحت.\n\nالتفاصيل.",
            "لا يمكنني إنشاء أو إرسال صور",
        ),
    ],
)
def test_structured_presentation_removes_false_model_media_claim(answer, forbidden):
    from core.services.advisor_presentations import remove_false_media_incapability

    cleaned = remove_false_media_incapability(answer)

    assert forbidden not in cleaned
    assert "Plan." in cleaned or "التفاصيل." in cleaned


@pytest.mark.parametrize(
    ("answer", "required"),
    [
        (
            "I cannot send an image, but the verified plan estimates 6 terms including the planning baseline.",
            "verified plan estimates 6 terms",
        ),
        (
            "لا يمكنني إرسال صور، لكن الخطة الموثوقة تقدر 6 فصول باحتساب الأساس التخطيطي.",
            "الخطة الموثوقة تقدر 6 فصول",
        ),
    ],
)
def test_media_cleanup_preserves_same_line_planning_facts(answer, required):
    from core.services.advisor_presentations import remove_false_media_incapability

    cleaned = remove_false_media_incapability(answer)

    assert required in cleaned
    assert cleaned


@pytest.mark.parametrize(
    ("answer", "replacement"),
    [
        ("I cannot send an image.", "structured view"),
        ("ما أقدر أرسل لك صورة.", "العرض المنظم"),
    ],
)
def test_media_cleanup_replaces_a_claim_only_answer_with_coherent_text(answer, replacement):
    from core.services.advisor_presentations import remove_false_media_incapability

    cleaned = remove_false_media_incapability(answer)

    assert cleaned
    assert replacement in cleaned
    assert answer not in cleaned


@pytest.mark.parametrize(
    "answer",
    [
        "I cannot send an image containing grades; open the authenticated record.",
        "I cannot provide a transcript image; use the authenticated portal.",
        "I cannot send an image of your CGPA; use the authenticated portal.",
        "I cannot send an image of your academic standing; use the authenticated portal.",
        "I cannot send an image showing a failed-course result; use the authenticated portal.",
        "لا يمكنني إرسال صورة تحتوي درجات؛ افتح السجل الموثق.",
        "ما أقدر أرسل لك صورة كشف الدرجات؛ افتح البوابة الموثقة.",
        "ما أقدر أرسل لك صورة الإنذار الأكاديمي؛ افتح البوابة الموثقة.",
    ],
)
def test_media_cleanup_preserves_protected_record_image_refusals(answer):
    from core.services.advisor_presentations import remove_false_media_incapability

    assert remove_false_media_incapability(answer) == answer


@pytest.mark.parametrize(
    ("answer", "required"),
    [
        (
            "I can’t directly generate, send, or display an image, but the plan has 6 terms.",
            "the plan has 6 terms",
        ),
        ("I'm unable to send an image, but the plan has 6 terms.", "the plan has 6 terms"),
        ("I am not able to send you an image, but the plan has 6 terms.", "the plan has 6 terms"),
        ("I cannot send a timetable image, but the plan has 6 terms.", "the plan has 6 terms"),
        (
            "I cannot send a graduation-plan image, but the plan has 6 terms.",
            "the plan has 6 terms",
        ),
        ("I cannot send the image, but the plan has 6 terms.", "the plan has 6 terms"),
        ("I cannot send this image, but the plan has 6 terms.", "the plan has 6 terms"),
        ("I cannot send images to you, but the plan has 6 terms.", "the plan has 6 terms"),
        ("I cannot send an image here, but the plan has 6 terms.", "the plan has 6 terms"),
        ("مو قادر ارسل لك صورة، لكن الخطة فيها 6 فصول.", "الخطة فيها 6 فصول"),
        ("لا أقدر ارسل لك صورة؛ الخطة فيها 6 فصول.", "الخطة فيها 6 فصول"),
    ],
)
def test_media_cleanup_handles_common_model_and_saudi_forms(answer, required):
    from core.services.advisor_presentations import remove_false_media_incapability

    cleaned = remove_false_media_incapability(answer)

    assert required in cleaned
    assert cleaned != answer
    assert not cleaned.startswith(("but", "however", "لكن", "،", "because"))


def _complete_lower_bound_answer() -> str:
    return (
        "The lower bound is at least 5 additional terms, or 6 including the "
        "planning baseline. DS492 still needs the 147-credit gate, and MATH471 still "
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
        "لو انسحبت من DS341، هل سيتأخر تخرجي؟",
        "لو أخذت MATH204 بدل DS341 هل يتأخر التخرج؟",
        "هل فيه مقرر أقدر أستبدله عشان أتخرج أفضل؟",
    ],
)
def test_current_course_graduation_questions_require_what_if_evidence(question):
    assert _requires_graduation_what_if(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "آخذ AI331 ولا DS341؟",
        "وش أفضل لي الحين: AI331 أو DS341؟",
        "قارن لي AI331 وDS341 حسب تأثيرهم على خطتي.",
        "وش الخيار اللي يفتح مواد أكثر: AI331 ولا DS341؟",
        "أيهم متطلباته مكتملة: AI331 ولا DS341؟",
        "إذا هدفي أتخرج أسرع، آخذ AI331 ولا DS341؟",
        "Which is better for me, AI331 or DS341?",
        "Rank AI331, CS372 and AI352 by plan impact.",
    ],
)
def test_explicit_course_choices_require_comparison_evidence(question):
    assert _requires_course_choice_comparison(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "وش الأفضل بينهم؟",
        "هل آخذ AI331؟",
        "لو أخذت MATH204 بدل DS341 هل يتأخر التخرج؟",
        "ابنِ لي جدولًا فيه AI331 أو DS341.",
    ],
)
def test_ambiguous_or_owned_scenarios_do_not_use_course_comparison_gate(question):
    assert _requires_course_choice_comparison(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "بناءً على جدولي، وش أقدر أبدل بدون تعارض؟",
        "إذا شلت DS341، وش أفضل بديل يدخل مع باقي شعبي؟",
        "هل فيه تبديل يحسن مسار تخرجي ويعطيني جدول كامل؟",
        "إذا بدلت DS341 بـ CS285، هل يتحسن تخرجي ويدخل في جدولي؟",
        "If I replace DS341 with CS285, will it improve graduation and fit my timetable?",
        "Replace DS341 with CS285 if it fits.",
        "Make sure the DS341 replacement fits.",
        "Replace DS341 with the best course that fits.",
        "Find a course fitting my schedule to replace DS341.",
        "Substitute DS341 with CS285 if it fits.",
        "Use CS285 as a replacement for DS341 if it fits.",
        "Switch DS341 to CS285 if it fits.",
        "Drop DS341 and take CS285 if it fits.",
        "Can I take CS285 instead of DS341 if it fits my timetable?",
        "Which current course can I replace without a clash?",
        "What course can I replace so my timetable still fits?",
        "إذا بدلت DS341 بـ CS285، هل يضبط مع جدولي؟",
        "أبغى أبدل DS341 بمقرر يمشي مع دوامي.",
        "هل بديل DS341 يركب على جدولي؟",
        "آخذ MATH204 بدل DS341 ويناسب جدولي.",
        "أحط MATH204 مكان DS341 إذا يناسب جدولي.",
    ],
)
def test_complete_timetable_replacement_questions_require_two_gate_evidence(question):
    assert _requires_feasible_course_replacements(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "إذا شلت DS341 هل يتأخر تخرجي؟",
        "أي شعبة من CS285 ما تتعارض؟",
        "ابنِ لي جدول جديد من الصفر.",
        "قارن DS341 وCS285.",
        "Replace DS341 with CS285 to improve graduation.",
        "Which CS285 section fits my timetable?",
        "Replace DS341 with CS285 if it fits my degree plan.",
        "Replace DS341 with a course fitting my career goals.",
        "Replace DS341 with a fitting course for my career.",
        "Swap DS341 for the course that best fits my interests.",
        "Replace DS341 because CS285 is a better career fit.",
        "Replace DS341 because CS285 is a better degree-plan fit.",
        "Replace this word because it fits the sentence.",
        "Replace the course description with this paragraph if it fits.",
        "Replace the course title with CS285 if it fits.",
        "Replace section M1 with M2 for DS341.",
        "Can I replace DS341 section M1 with M2 without a clash?",
    ],
)
def test_academic_only_or_timetable_only_requests_do_not_use_two_gate_swap(question):
    assert _requires_feasible_course_replacements(question) is False


@pytest.mark.parametrize(
    ("question", "model_arguments", "expected"),
    [
        (
            "إذا بدلت DS341 بـ CS285، هل يتحسن تخرجي ويدخل في جدولي؟",
            {"remove_course": "WRONG1", "add_course": "WRONG2"},
            {"remove_course": "DS341", "add_course": "CS285"},
        ),
        (
            "إذا شلت DS341، وش أفضل بديل يدخل مع باقي شعبي؟",
            {"add_course": "MADE999"},
            {"remove_course": "DS341"},
        ),
        (
            "وش أقدر أشيل من جدولي وآخذ CS285 بدون تعارض؟",
            {"remove_course": "MADE999"},
            {"add_course": "CS285"},
        ),
        (
            "بناءً على جدولي، وش أقدر أبدل بدون تعارض؟",
            {"remove_course": "MADE999", "add_course": "FAKE100"},
            {},
        ),
        (
            "Substitute DS341 with CS285 if it fits.",
            {"remove_course": "WRONG1", "add_course": "WRONG2"},
            {"remove_course": "DS341", "add_course": "CS285"},
        ),
        (
            "Use CS285 as a replacement for DS341 if it fits.",
            {},
            {"remove_course": "DS341", "add_course": "CS285"},
        ),
        (
            "Switch DS341 to CS285 if it fits.",
            {},
            {"remove_course": "DS341", "add_course": "CS285"},
        ),
        (
            "Drop DS341 and take CS285 if it fits.",
            {},
            {"remove_course": "DS341", "add_course": "CS285"},
        ),
        (
            "آخذ MATH204 بدل DS341 ويناسب جدولي.",
            {},
            {"remove_course": "DS341", "add_course": "MATH204"},
        ),
        (
            "أحط MATH204 مكان DS341 إذا يناسب جدولي.",
            {},
            {"remove_course": "DS341", "add_course": "MATH204"},
        ),
    ],
)
def test_replacement_arguments_are_bound_only_to_student_wording(
    question, model_arguments, expected
):
    normalised, _ = _normalise_feasible_replacement_args(question, model_arguments)
    assert normalised == expected


def test_replacement_normalizer_discards_model_term_override():
    normalised, reasons = _normalise_feasible_replacement_args(
        "If I replace DS341 with CS285, will it fit my timetable?",
        {
            "remove_course": "DS341",
            "add_course": "CS285",
            "academic_year": 1447,
            "term": 2,
        },
    )

    assert normalised == {"remove_course": "DS341", "add_course": "CS285"}
    assert "discarded_model_term_override" in reasons


def _certified_replacement_result() -> dict[str, Any]:
    return {
        "tool": "feasible_course_replacements",
        "ok": True,
        "academic_year": 1448,
        "term": 1,
        "baseline_kind": "REGISTERED",
        "status": "CERTIFIED_SWAPS_FOUND",
        "requested_remove_course": "DS341",
        "requested_add_course": "CS285",
        "academic_search": {"search_truncated": False},
        "certification_search": {"search_truncated": False},
        "certified_replacements": [
            {
                "remove_course": {
                    "course_code": "DS341",
                    "course_name": "DATA PRIVACY",
                    "credits": 3,
                },
                "add_course": {
                    "course_code": "CS285",
                    "course_name": "SOFTWARE ENGINEERING",
                    "credits": 3,
                },
                "outside_plan_addition": False,
                "academic_improvement": {
                    "proven_improvement": True,
                    "timing_effect": "EARLIER",
                    "terms_saved": 1,
                    "blockers_resolved": ["CS385"],
                    "blockers_improved": [],
                    "blockers_introduced": [],
                },
                "timetable": {
                    "status": "COMPLETE_CLASH_FREE",
                    "certified_options": [
                        {
                            "planner_options": ["A1"],
                            "complete_sections": [
                                {
                                    "course_code": "CS285",
                                    "course_name": "SOFTWARE ENGINEERING",
                                    "section": "M3",
                                    "credits": 3,
                                    "meetings": [
                                        {"day": "Sunday", "start": "09:00", "end": "10:15"}
                                    ],
                                }
                            ],
                            "meetings": [
                                {
                                    "course_code": "CS285",
                                    "section": "M3",
                                    "day": "Sunday",
                                    "start": "09:00",
                                    "end": "10:15",
                                }
                            ],
                            "scheduled_courses": 1,
                            "target_courses": 1,
                            "credit_hours": 3,
                            "days_on_campus": 1,
                            "days": ["Sunday"],
                            "earliest_start": "09:00",
                            "latest_end": "10:15",
                        }
                    ],
                },
            }
        ],
        "rejected_replacements": [],
        "limitations": [],
    }


def test_replacement_cannot_answer_before_fresh_two_gate_evidence(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute(name, arguments, **kwargs):
        calls.append((name, arguments))
        return _certified_replacement_result()

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _answer_turn("أكيد بدّلها، ما فيه تعارض."),
        _tool_turn(
            "feasible_course_replacements",
            {"remove_course": "CS285", "add_course": "DS341"},
        ),
    )

    result = answer_student_advisor_v2(
        question="إذا بدلت DS341 بـ CS285، هل يتحسن تخرجي ويدخل في جدولي؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert calls == [
        ("feasible_course_replacements", {"remove_course": "DS341", "add_course": "CS285"})
    ]
    assert result["answer"].startswith(
        "**الخلاصة:** عدد الاستبدالات التي ثبت تحسنها أكاديميًا وأمكن إنشاء "
        "جدول مكتمل لها بلا تعارضات بالاستناد إلى الجدول المسجّل فعليًا: 1."
    )
    assert (
        "تشير المحاكاة المكتملة إلى تقليص المدة المتبقية بما يعادل "
        "1 من الفصول الدراسية" in result["answer"]
    )
    assert "CS285 M3" in result["answer"]
    assert (
        "لا تتضمن هذه البيانات تأكيدًا لطرح الشعبة حاليًا أو لوجود مقعد شاغر "
        "أو لاستيفاء جميع شروط التسجيل" in result["answer"]
    )
    assert "النتيجة للقراءة فقط، ولم يُحذف أو يُضف أو يُسجّل أي مقرر." in result["answer"]
    assert result["presentation"]["kind"] == "timetable_proposals"
    assert result["presentation"]["replacement"]["add_course"]["course_code"] == "CS285"
    assert result["agent"]["replacement_grounding_required"] is True
    assert result["agent"]["replacement_reprompted"] is True
    assert result["agent"]["replacement_safe_fallback_used"] is True
    assert result["agent"]["iterations"] == 2


@pytest.mark.parametrize(
    ("question", "expected_codes", "expected_objective"),
    [
        ("آخذ ai-331 ولا DS 341؟", ["AI331", "DS341"], "balanced"),
        ("وش يفتح أكثر AI331 أو DS341؟", ["AI331", "DS341"], "unlock_impact"),
        ("أيهم يناسب جدولي AI331 ولا DS341؟", ["AI331", "DS341"], "timetable_fit"),
        ("أيهم يخليني أتخرج أسرع AI331 أو DS341؟", ["AI331", "DS341"], "graduation"),
        ("أي وحدة تأجيلها يضرني أقل: AI331 أو DS341؟", ["AI331", "DS341"], "graduation"),
    ],
)
def test_comparison_arguments_are_bound_to_explicit_codes_and_objective(
    question, expected_codes, expected_objective
):
    normalised, reasons = _normalise_course_comparison_args(
        question,
        {"course_codes": ["WRONG100"], "objective": "balanced"},
    )
    assert normalised["course_codes"] == expected_codes
    assert normalised["objective"] == expected_objective
    assert "explicit_course_codes" in reasons


def test_comparison_arguments_discard_forged_model_term_override():
    normalised, reasons = _normalise_course_comparison_args(
        "Which is better for me, AI331 or DS341?",
        {
            "course_codes": ["AI331", "DS341"],
            "academic_year": 1447,
            "term": 2,
        },
    )

    assert "academic_year" not in normalised
    assert "term" not in normalised
    assert "discarded_model_term_override" in reasons


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Compare AI331 and DS341 for 1447/2.", (1447, 2)),
        ("قارن AI331 وDS341 في ١٤٤٧/٢", (1447, 2)),
        ("Compare AI331 and DS341 for 2026/2.", None),
        ("Compare AI331 and DS341 for 1447/4.", None),
        ("Compare AI331 and DS341; I graduated in 1447.", None),
        ("I took CS113 in 1447/2; compare AI331 and DS341 now.", None),
        ("I studied in 1447/2. For comparison, AI331 or DS341?", None),
        ("Not for 1447/2 but compare AI331 and DS341 for 1448/1.", None),
        ("Compare AI331 and DS341 in term 1447/2.", (1447, 2)),
        ("Compare AI331 and DS341 for term 1447/2.", (1447, 2)),
        ("For 1447/2, compare AI331 and DS341.", (1447, 2)),
        ("In 1447/2 compare AI331 and DS341.", (1447, 2)),
    ],
)
def test_explicit_comparison_term_parser_is_conservative(question, expected):
    assert _explicit_comparison_year_term(question) == expected


@pytest.mark.parametrize(
    ("question", "expected_year", "expected_term"),
    [
        ("Compare AI331 and DS341.", 1448, 1),
        ("Compare AI331 and DS341 for 1447/2.", 1447, 2),
        ("قارن AI331 وDS341 في ١٤٤٧/٢", 1447, 2),
        ("Compare AI331 and DS341 for 2026/2.", 1448, 1),
        ("Compare AI331 and DS341 for 1447/4.", 1448, 1),
    ],
)
def test_comparison_execution_uses_only_trusted_student_term_context(
    monkeypatch, question, expected_year, expected_term
):
    seen = {}

    def fake_execute(name, arguments, *, principal, context=None):
        seen.update(name=name, arguments=arguments, context=dict(context or {}))
        return _comparison_result()

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn(
            "course_choice_comparison",
            {
                "course_codes": ["AI331", "DS341"],
                "academic_year": 1446,
                "term": 3,
            },
        )
    )

    answer_student_advisor_v2(
        question=question,
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert seen["name"] == "course_choice_comparison"
    assert "academic_year" not in seen["arguments"]
    assert "term" not in seen["arguments"]
    assert seen["context"] == {
        "academic_year": expected_year,
        "term": expected_term,
        "section_snapshot_academic_year": 1448,
        "section_snapshot_term": 1,
    }


def _comparison_result() -> dict[str, Any]:
    return {
        "tool": "course_choice_comparison",
        "ok": True,
        "program": "AI",
        "academic_year": 1448,
        "term": 1,
        "objective": "unlock_impact",
        "baseline_kind": "EMPTY",
        "verdict": "PREFERRED",
        "preferred_course": "AI331",
        "criterion_leaders": {"direct_unlock": ["AI331"]},
        "decision_basis": ["direct_unlock"],
        "limitations": [],
        "candidates": [
            {
                "course_code": "AI331",
                "course_name": "KNOWLEDGE REPRESENTATION",
                "credit_hours": 4,
                "kind": "COURSE",
                "academic_status": "open_now",
                "prerequisite_ready": True,
                "missing_prerequisites": [],
                "recommendation": {"state": "RECOMMENDED", "rank": 1},
                "impact": {
                    "direct_unlock_count": 3,
                    "chain_course_count": 5,
                    "weighted_downstream_score": 5.5,
                    "weighted_score_method": "sum_inverse_distance",
                },
                "timetable": {
                    "status": "OK",
                    "sections_on_file": 2,
                    "clash_free_count": 1,
                    "clashing_count": 1,
                    "baseline_sections": [],
                },
                "graduation": {
                    "status": "COMPLETED",
                    "simulation_completed": True,
                    "estimated_additional_terms": 4,
                    "lower_bound_additional_terms": 4,
                    "unresolved_requirements": [],
                },
            },
            {
                "course_code": "DS341",
                "course_name": "DATA PRIVACY",
                "credit_hours": 3,
                "kind": "COURSE",
                "academic_status": "blocked",
                "prerequisite_ready": False,
                "missing_prerequisites": [{"kind": "MISSING_COURSE", "course_code": "DS225"}],
                "recommendation": {"state": "NOT_RECOMMENDED", "rank": None},
                "impact": {
                    "direct_unlock_count": 0,
                    "chain_course_count": 1,
                    "weighted_downstream_score": 1.0,
                    "weighted_score_method": "sum_inverse_distance",
                },
                "timetable": {
                    "status": "NOT_ON_FILE",
                    "sections_on_file": 0,
                    "clash_free_count": 0,
                    "clashing_count": 0,
                    "baseline_sections": [],
                },
                "graduation": {
                    "status": "NOT_DETERMINABLE",
                    "simulation_completed": False,
                    "estimated_additional_terms": None,
                    "lower_bound_additional_terms": 4,
                    "unresolved_requirements": [{"code": "DS492"}],
                },
            },
        ],
    }


@pytest.mark.parametrize(
    ("language", "reason_code", "expected"),
    [
        (
            "English",
            "CANDIDATE_MEETING_DATA_INCOMPLETE",
            "candidate-section meeting data is incomplete or invalid",
        ),
        (
            "Arabic",
            "BASELINE_MEETING_DATA_INCOMPLETE",
            "بيانات مواعيد الجدول المرجعي ناقصة أو غير صالحة",
        ),
    ],
)
def test_safe_course_comparison_explains_indeterminate_timetable_evidence(
    language: str,
    reason_code: str,
    expected: str,
) -> None:
    result = _comparison_result()
    result["verdict"] = "NOT_DETERMINABLE"
    result["preferred_course"] = None
    result["candidates"][0]["timetable"] = {
        "status": "NOT_DETERMINABLE",
        "reason_code": reason_code,
        "reason": "The service supplied a bounded explanation.",
        "sections_on_file": 2,
        "clash_free_count": None,
        "clashing_count": None,
        "baseline_sections": [],
    }

    answer = _safe_course_comparison_answer(language, [result])

    assert expected in answer
    assert "0 recorded section(s), 0 individually clash-free" not in answer


def test_course_comparison_cannot_answer_before_fresh_evidence(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute(name, arguments, **kwargs):
        calls.append((name, arguments))
        return _comparison_result()

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _answer_turn("AI331 is definitely better."),
        _tool_turn(
            "course_choice_comparison",
            {"course_codes": ["DS341"], "objective": "balanced"},
        ),
    )

    result = answer_student_advisor_v2(
        question="وش يفتح أكثر AI331 ولا DS341؟",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert calls == [
        (
            "course_choice_comparison",
            {"course_codes": ["AI331", "DS341"], "objective": "unlock_impact"},
        )
    ]
    assert result["answer"].startswith(
        "**الخلاصة:** يتقدّم AI331 وفق الهدف الذي حددته والبيانات التي أمكن التحقق منها."
    )
    assert "مؤشر أثر الخطة أداة تخطيط داخلية، وليس ترتيبًا رسميًا من الجامعة." in result["answer"]
    assert "هذه المقارنة للقراءة فقط، ولم يسجّل النظام أي مقرر أو يغيّره." in result["answer"]
    assert result["agent"]["course_comparison_grounding_required"] is True
    assert result["agent"]["course_comparison_reprompted"] is True
    assert result["agent"]["course_comparison_safe_fallback_used"] is True
    assert result["agent"]["iterations"] == 2


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
        (
            "إذا شلت DS341 هالترم، متى أتخرج؟",
            {},
            {"remove_current_courses": ["DS341"]},
            "explicit_omission",
        ),
        (
            "لو ما نزلت DS٣٤١ الحين، يطول تخرجي؟",
            {},
            {"remove_current_courses": ["DS341"]},
            "explicit_omission",
        ),
        (
            "لو حطيت MATH204 بدال DS341، وش يصير؟",
            {},
            {
                "remove_current_courses": ["DS341"],
                "add_current_courses": ["MATH204"],
            },
            "explicit_replacement",
        ),
        (
            "أشيل DS341 وأحط MATH204 مكانها، أتخرج أسرع؟",
            {},
            {
                "remove_current_courses": ["DS341"],
                "add_current_courses": ["MATH204"],
            },
            "explicit_replacement",
        ),
    ],
)
def test_graduation_scenario_arguments_follow_explicit_student_wording(
    question, model_arguments, expected, reason
):
    arguments, normalisation = _normalise_graduation_scenario_args(question, model_arguments)

    assert {k: v for k, v in arguments.items() if k != "planning_baseline_kind"} == expected
    assert arguments["planning_baseline_kind"] in {
        "recommended_current_term",
        "registered_timetable",
    }
    assert normalisation == reason


@pytest.mark.parametrize(
    ("question", "expected_kind", "expected_change"),
    [
        (
            "When will I graduate?",
            "recommended_current_term",
            {},
        ),
        (
            "Based on my current timetable, when will I graduate?",
            "registered_timetable",
            {},
        ),
        (
            "بناءً على جدولي المسجل فعليًا، متى أتخرج؟",
            "registered_timetable",
            {},
        ),
        (
            "If I dropped DS341, would my graduation plan change?",
            "registered_timetable",
            {"remove_current_courses": ["DS341"]},
        ),
        (
            "If I withdrew from DS341, would graduation be delayed?",
            "registered_timetable",
            {"remove_current_courses": ["DS341"]},
        ),
        (
            "If I skip DS341 from the recommended plan, when will I graduate?",
            "recommended_current_term",
            {"remove_current_courses": ["DS341"]},
        ),
        (
            "If I add MATH204 to the recommended courses, when will I graduate?",
            "recommended_current_term",
            {"add_current_courses": ["MATH204"]},
        ),
    ],
)
def test_graduation_baseline_is_deterministic_from_student_wording(
    question: str,
    expected_kind: str,
    expected_change: dict[str, Any],
) -> None:
    arguments, _ = _normalise_graduation_scenario_args(
        question,
        {
            "planning_baseline_kind": "wrong-model-choice",
            "academic_year": 1440,
            "term": 3,
            "remove_current_courses": ["FAKE100"],
            "add_current_courses": ["FAKE200"],
            "search_better_replacements": True,
        },
    )

    assert arguments == {
        "planning_baseline_kind": expected_kind,
        **expected_change,
    }


def test_plain_graduation_uses_literal_system_current_term_and_strips_model_scenario(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "core.settings_views.load_defaults",
        lambda: {
            "academic_year": 1450,
            "term": 2,
            "currentYear": 1448,
            "currentTerm": 1,
        },
    )
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def fake_execute(name, arguments, **kwargs):
        assert name == "graduation_progress"
        calls.append((dict(arguments), dict(kwargs["context"])))
        return {
            **_complete_graduation_result(),
            "planning_baseline_kind": "recommended_current_term",
        }

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", fake_execute)
    client = FakeClient(
        _tool_turn(
            "graduation_progress",
            {
                "academic_year": 1440,
                "term": 3,
                "planning_baseline_kind": "registered_timetable",
                "remove_current_courses": ["FAKE100"],
            },
        ),
        _answer_turn(
            "The scenario estimates 5 additional terms, or 6 terms including the "
            "system's current-term recommended courses (1448/1)."
        ),
    )

    answer_student_advisor_v2(
        question="When will I graduate?",
        principal=_principal(),
        academic_year=1450,
        term=2,
        llm_client=client,
    )

    assert calls == [
        (
            {"planning_baseline_kind": "recommended_current_term"},
            {"academic_year": 1448, "term": 1},
        )
    ]


def test_safe_what_if_states_term_count_and_independent_term_plan_delta() -> None:
    result = {
        "tool": "graduation_progress",
        "ok": True,
        "planning_baseline_kind": "registered_timetable",
        "what_if": {
            "mode": "explicit_changes",
            "valid": True,
            "validation_errors": [],
            "removed_current_courses": [{"code": "DS341"}],
            "added_current_courses": [],
            "outside_plan_additions": [],
            "baseline": {"lower_bound_additional_terms": 4},
            "scenario": {"lower_bound_additional_terms": 4},
            "comparison": {
                "timing_effect": "SAME",
                "term_difference": 0,
                "terms_saved": 0,
                "plan_changed": True,
                "term_plan_changes": [
                    {
                        "code": "DS341",
                        "before": {
                            "academic_year": 1448,
                            "term": 1,
                            "sequence": 0,
                            "baseline": True,
                        },
                        "after": {
                            "academic_year": 1448,
                            "term": 2,
                            "sequence": 1,
                            "baseline": False,
                        },
                        "became_unresolved": False,
                    }
                ],
            },
        },
    }

    answer = _safe_graduation_answer("English", [result])

    assert "actual registered timetable" in answer
    assert "estimated number of terms is unchanged" in answer
    assert "term-by-term course plan changes" in answer
    assert "DS341: baseline term (1448/1) → 1448/2" in answer


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

    assert executed == [
        (
            "graduation_progress",
            {
                "planning_baseline_kind": "recommended_current_term",
                "remove_current_courses": ["DS225"],
            },
        )
    ]
    assert result["agent"]["tools_called"][0] == {
        "name": "graduation_progress",
        "arguments": {
            "planning_baseline_kind": "recommended_current_term",
            "remove_current_courses": ["DS225"],
        },
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
        {
            "planning_baseline_kind": "recommended_current_term",
            "remove_current_courses": ["DS341"],
        },
    )
    assert "DS341 is not in the planning-baseline timetable" in result["answer"]
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
        "الحد الأدنى هو 5 فصول إضافية أو 6 مع الفصل المرجعي للتخطيط. DS492 يحتاج 147 ساعة، "
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
    assert "هذه محاكاة للقراءة فقط" in result["answer"]
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

    assert "Planning-baseline scenario: remove DS341 and add MATH204" in result["answer"]
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
            "arguments": {
                "planning_baseline_kind": "registered_timetable",
                "search_better_replacements": True,
            },
            "scenario_normalization": "open_replacement_search",
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
            "planning_baseline_academic_year": 1448,
            "planning_baseline_term": 1,
            "max_credits_per_term": 18,
            "lower_bound_additional_terms": 5,
            "lower_bound_terms_including_planning_baseline": 6,
            "simulation_completed": False,
            "planning_baseline_courses_assumed_passed": [
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
                    "registered_credits_at_planning_baseline": 18,
                },
                "scenario": {
                    "lower_bound_additional_terms": 5,
                    "registered_credits_at_planning_baseline": 18,
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
    assert "section" not in projected["planning_baseline_courses_assumed_passed"][0]
    assert "current_courses_assumed_passed" not in projected
    assert projected["planning_baseline_academic_year"] == 1448
    assert projected["planning_baseline_term"] == 1
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
    assert safe["planning_term"] == "1448/1"
    assert safe["band_labels"]["1"] == "Planning baseline 1448/1"
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


def _exact_timetable_result() -> dict[str, Any]:
    courses = (
        ("AI1", "M6", "SUN", "14:30", "15:45", "M7"),
        ("AI433", "M6", "MON", "13:00", "14:15", "M6"),
        ("CS424", "M9", "TUE", "10:30", "11:45", "T07"),
        ("GS104", "M18", "WED", "08:00", "09:15", "G12"),
        ("MGT405", "M7", "THU", "10:50", "12:30", "L03"),
    )
    return {
        "tool": "my_timetable",
        "ok": True,
        "academic_year": 1448,
        "term": 1,
        "schedule_kind": "REGISTERED",
        "is_expected_plan": False,
        "registered_course_count": 5,
        "registered_credit_hours": 14,
        "meetings": [
            {
                "course_code": code,
                "section": section,
                "day": day,
                "start": start,
                "end": end,
                "room": room,
            }
            for code, section, day, start, end, room in courses
        ],
    }


def _natural_exact_timetable_answer() -> str:
    return (
        "يحتوي جدولك المسجّل فعليًا على 5 مقررات بإجمالي 14 ساعة: "
        "AI1 في الشعبة M6 الساعة 14:30 في القاعة M7، وAI433، وCS424، "
        "وGS104، وMGT405."
    )


def test_exact_timetable_no_tool_answer_is_reprompted_before_shipping(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    client = FakeClient(
        _answer_turn("لقد عرضته لك بالفعل."),
        _tool_turn("my_timetable", {}),
        _answer_turn(_natural_exact_timetable_answer()),
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا قبل أن تبني أي بدائل.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("يحتوي جدولك")
    assert result["agent"]["evidence_tool_reprompted"] is True
    assert result["agent"]["evidence_validation_outcome"] == "passed"


def test_correct_tool_but_ignored_result_gets_one_bounded_repair(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn("لقد عرضته لك بالفعل."),
        repair=_natural_exact_timetable_answer(),
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["answer"].startswith("يحتوي جدولك")
    assert result["agent"]["evidence_validation_repair_attempted"] is True
    assert result["agent"]["evidence_validation_outcome"] == "repaired"
    assert REQUESTED_EVIDENCE_OMITTED in result["agent"]["evidence_validation_violations"]
    assert len(client.repair_messages) == 1


def test_fabricated_timetable_facts_are_replaced_by_verified_fallback(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    fabricated = "جدولك يحتوي على AI331 في الشعبة F11 الساعة 09:00 في القاعة X99 مع الدكتور أحمد."
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn(fabricated),
        repair=fabricated,
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] == "verified_fallback"
    assert UNSUPPORTED_ACADEMIC_FACT in result["agent"]["evidence_validation_violations"]
    assert "AI331" not in result["answer"]
    assert "F11" not in result["answer"]
    assert "AI1" in result["answer"]
    assert "14 ساعة" in result["answer"]


def test_repair_cannot_bypass_citation_or_internal_marker_gates(monkeypatch):
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    unsafe_repair = (
        _natural_exact_timetable_answer() + " السبب الداخلي NOT_ON_FILE. [TU.FAKE.POLICY]"
    )
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn("لقد عرضته لك بالفعل."),
        repair=unsafe_repair,
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] == "verified_fallback"
    assert "TU.FAKE.POLICY" not in result["answer"]
    assert "NOT_ON_FILE" not in result["answer"]
    assert "AI1" in result["answer"]


def test_an_empty_repair_body_is_a_failed_repair_not_a_clean_one(monkeypatch):
    """A repair that says nothing has not fixed anything.

    Blank text short-circuits check_answer before the evidence postconditions
    run, so an empty repair would otherwise discard the violating answer, ship
    nothing, and record the turn as a clean "repaired" PASS with the student's
    timetable card still attached beside the silence.
    """
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn("جدولك يحتوي على AI331 في الشعبة F11."),
        repair="   ",
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] != "repaired"
    assert result["answer"].strip()
    # The verified facts survive; the fabricated section does not.
    assert "AI1" in result["answer"]
    assert "F11" not in result["answer"]


def test_a_blank_answer_is_never_recorded_as_a_pass(monkeypatch):
    """An answer that was never written must not be filed as a good one.

    check_answer reports no violations for blank text, which is right - a claim
    nobody made cannot contradict evidence - so without a terminal guard the
    turn lands as PASS/COMPLETED, and derive_outcome then denies the student
    the human review that an abstention would have granted.
    """
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    # Every turn the model is given, including any re-prompt and the forced
    # final rescue, produces nothing.
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn("   "),
        _answer_turn("   "),
        _answer_turn("   "),
        _answer_turn("   "),
        repair="   ",
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] == "abstained"
    assert result["agent"]["grounding_refused"] is True
    assert "لم أتمكن" in result["answer"]
    assert result["presentation"] is None


def test_unverifiable_answer_abstains_instead_of_shipping_invented_facts(monkeypatch):
    """The terminal branch: nothing verifiable exists, so nothing is asserted.

    When the required evidence tool fails there is no repair worth attempting and
    no verified fallback to render.  The student must receive the abstention, not
    the model's unverifiable draft.  Recording the turn as an abstention while
    still showing that draft would reproduce exactly the production failure this
    boundary exists to stop: a wrong answer filed as a safe one.
    """
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: {
            "tool": name,
            "ok": False,
            "error": "Capability unavailable.",
        },
    )
    fabricated = "جدولك يحتوي على AI331 في الشعبة F11 الساعة 09:00 في القاعة X99 بإجمالي 14 ساعة."
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn(fabricated),
        # The failed capability is re-prompted once; the model repeats its draft.
        _answer_turn(fabricated),
        repair=fabricated,
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] == "abstained"
    assert result["agent"]["grounding_refused"] is True
    # The abstention is what the STUDENT reads, not merely what the row records.
    assert "لم أتمكن" in result["answer"]
    assert "AI331" not in result["answer"]
    assert "F11" not in result["answer"]
    assert "X99" not in result["answer"]
    assert "14" not in result["answer"]
    assert result["presentation"] is None
    assert result["agent"]["evidence_audit"]["validation"]["outcome"] == "abstained"


def test_changed_credit_total_fails_but_natural_arabic_exact_facts_pass():
    evidence = _exact_timetable_result()
    changed = "يحتوي الجدول المسجّل فعليًا على 5 مقررات بإجمالي 12 ساعة."

    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        changed,
        tool_results=[evidence],
        question="اعرض جدولي",
        required_tools={"my_timetable"},
    )
    assert (
        check_answer(
            _natural_exact_timetable_answer(),
            tool_results=[evidence],
            question="اعرض جدولي",
            required_tools={"my_timetable"},
        )
        == []
    )


def test_progress_graduation_figures_and_phase_are_not_interchangeable():
    graduation = {
        "tool": "graduation_progress",
        "ok": True,
        "plan_courses_passed": 41,
        "plan_courses_total": 53,
        "courses_remaining": 12,
        "credits_remaining_in_plan": 31,
        "credits_earned_registrar": 126,
        "estimated_additional_terms": 1,
        "estimated_terms_including_planning_baseline": 2,
    }
    fabricated = (
        "أنجزت 32 من 40 مقررًا، وبقي 8 مقررات و24 ساعة، والساعات المكتسبة: 96، "
        "وتحتاج إلى فصلين إضافيين بإجمالي 3 فصول."
    )
    wrong_phase = "بعد اجتياز مقررات البداية، سيبقى 12 مقررًا و31 ساعة متبقية."

    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        fabricated,
        tool_results=[graduation],
        question="كم متبقي للخطة؟",
        required_tools={"graduation_progress"},
    )
    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        wrong_phase,
        tool_results=[graduation],
        question="كم متبقي للخطة؟",
        required_tools={"graduation_progress"},
    )


def test_a_registered_line_does_not_make_the_next_recommendation_unsupported():
    """Registered courses, then a recommendation: the commonest answer shape.

    The schedule scope carried by a heading used to stick to every later line,
    so a true recommendation inherited REGISTERED and had to appear in the
    timetable to survive. Both tools' evidence was present and both lines were
    true, and the answer was still an unsupported academic fact.
    """
    timetable = {
        "tool": "my_timetable",
        "ok": True,
        "schedule_kind": "REGISTERED",
        "registrations": [
            {"course_code": "AI331", "section": "M6"},
            {"course_code": "CS323", "section": "M9"},
        ],
    }
    recommendation = {
        "tool": "recommend_courses",
        "ok": True,
        "recommendation_count": 1,
        "recommendations": [
            {"course_code": "AI433", "course_name": "تعلم الآلة", "credit_hours": 3}
        ],
    }
    evidence = [timetable, recommendation]

    correct = "في الجدول المسجل لديك AI331 وCS323.\nولإكمال الخطة يوصي النظام بـ AI433."
    assert (
        check_answer(correct, tool_results=evidence, question="ماذا أسجل؟", required_tools=set())
        == []
    )

    # Clearing the scope must not stop a fabricated REGISTRATION being caught.
    invented = "في الجدول المسجل لديك AI331 وZZ999."
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        invented, tool_results=evidence, question="ماذا أسجل؟", required_tools=set()
    )


def test_arabic_spellings_do_not_hide_a_fabricated_figure_or_time():
    """Arabic-Indic digits and the broken plural «فصول» are not a bypass.

    Every other figure reader in the module already accepted Arabic-Indic
    digits, and «فصول» does not start with «فصل», so a fabricated term count or
    meeting time written the Arabic way was invisible while the identical
    fabrication in ASCII was caught.
    """
    graduation = {
        "tool": "graduation_progress",
        "ok": True,
        "estimated_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 3,
        "lower_bound_additional_terms": 2,
        "lower_bound_terms_including_planning_baseline": 3,
        "simulation_completed": True,
    }
    for fabricated in ("تحتاج إلى 9 فصول إضافية.", "تحتاج إلى ٩ فصول إضافية."):
        assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
            fabricated,
            tool_results=[graduation],
            question="كم يتبقى؟",
            required_tools=set(),
        ), fabricated
    assert (
        check_answer(
            "تحتاج إلى فصلين إضافيين.",
            tool_results=[graduation],
            question="كم يتبقى؟",
            required_tools=set(),
        )
        == []
    )

    timetable = {
        "tool": "my_timetable",
        "ok": True,
        "registered_sections": [{"course_code": "AI331", "section": "M6"}],
        "meetings": [
            {
                "course_code": "AI331",
                "section": "M6",
                "day": "SUN",
                "start": "09:00",
                "end": "10:15",
                "room": "B204",
            }
        ],
    }
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        "مقرر AI331 الشعبة M6 من الساعة ١١:٣٠ إلى ١٢:٤٥.",
        tool_results=[timetable],
        question="جدولي؟",
        required_tools=set(),
    )
    # The same true time in either spelling stays clean.
    for spelling in ("٠٩:٠٠ إلى ١٠:١٥", "09:00 إلى 10:15"):
        assert (
            check_answer(
                f"مقرر AI331 الشعبة M6 من الساعة {spelling}.",
                tool_results=[timetable],
                question="جدولي؟",
                required_tools=set(),
            )
            == []
        ), spelling


def test_section_listing_evidence_is_inside_the_boundary():
    """The tool that lists a course's sections must be checkable.

    my_clash_free_sections is the only tool that answers "which sections does
    this course have". While it sat outside EXACT_FACT_TOOLS every relational
    check was gated off for section questions, so an answer inventing a code, a
    section, a day, a time and a room returned zero violations - the open defect
    where the adviser invents sections and attributes them to "the system".
    """
    # The executor's REAL payload shape - key "courses", never "results".  An
    # earlier fixture used "results" and was the only test of the evidence
    # arm, so the arm read the wrong key in production and stayed green: a
    # test written to the bug it existed to prevent.
    evidence = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "courses": [
            {
                "course_code": "AI331",
                "currently_registered_sections": ["M6"],
                "status": "OK",
                "clash_free": [
                    {"section": "M1", "meetings": ["SUN 09:00-10:15"]},
                ],
            }
        ],
    }
    question = "ما شعب AI331 المتاحة لي؟"

    invented = "مقرر ZZ999 له الشعبة W2 يوم الاثنين 10:00-11:15 في القاعة X9."
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        invented,
        tool_results=[evidence],
        question=question,
        required_tools={"my_clash_free_sections"},
    )

    # A section quoted from the payload's bare-string lists is real evidence,
    # not an invention: coverage must not be bought by refusing true answers.
    truthful = "مقرر AI331 له الشعبة M1 يوم SUN من 09:00 إلى 10:15، وأنت في الشعبة M6."
    assert (
        check_answer(
            truthful,
            tool_results=[evidence],
            question=question,
            required_tools={"my_clash_free_sections"},
        )
        == []
    )

    # And the tool's admission bought TIME verification, not only code
    # verification - the original fix stopped at the code check, so a real
    # section at an invented time still passed every clock comparison.
    fabricated_time = "الشعبة M1 لمقرر AI331 تبدأ الساعة 11:30."
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        fabricated_time,
        tool_results=[evidence],
        question=question,
        required_tools={"my_clash_free_sections"},
    )


def test_a_real_time_attributed_to_the_wrong_section_is_a_false_relation():
    """The relation arm's unique job: real values, wrong pairing.

    Every VALUE here exists in the payload - both sections, both times - so
    the value-set checks pass and only the section-to-meeting RELATION is
    fabricated.  This is the probe that exposed the arm as dead code: it read
    payload key "results" while the executor emits "courses", so this exact
    answer returned zero violations against a real payload.
    """
    evidence = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "courses": [
            {
                "course_code": "AI331",
                "status": "OK",
                "clash_free": [
                    {"section": "M1", "meetings": ["SUN 09:00-10:15"]},
                    {"section": "M2", "meetings": ["MON 11:00-12:15"]},
                ],
            }
        ],
    }
    question = "ما شعب AI331؟"

    swapped = "الشعبة M1 لمقرر AI331 تجتمع يوم الاثنين من 11:00 إلى 12:15."
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        swapped,
        tool_results=[evidence],
        question=question,
        required_tools={"my_clash_free_sections"},
    )

    correct = "الشعبة M1 لمقرر AI331 تجتمع يوم الأحد من 09:00 إلى 10:15."
    assert (
        check_answer(
            correct,
            tool_results=[evidence],
            question=question,
            required_tools={"my_clash_free_sections"},
        )
        == []
    )


def test_a_fallback_that_fails_validation_abstains_rather_than_shipping(monkeypatch):
    """The last gate before abstention has to be a gate.

    The fallback is server-authored, so it should never violate - but the
    branch exists exactly for the case where it does, and dropping its
    violations check was invisible to every test. Without it the fallback ships
    whatever it produced, and the abstention below it becomes unreachable.
    """
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    monkeypatch.setattr(
        "core.services.student_advisor_v2._verified_evidence_fallback",
        lambda *args, **kwargs: "جدولك يحتوي على ZZ999 في الشعبة W7 الساعة 08:00.",
    )
    fabricated = "جدولك يحتوي على AI331 في الشعبة F11."
    client = RepairClient(
        _tool_turn("my_timetable", {}),
        _answer_turn(fabricated),
        repair=fabricated,
    )

    result = answer_student_advisor_v2(
        question="اعرض لي جدولي المسجل حاليًا.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert result["agent"]["evidence_validation_outcome"] == "abstained"
    assert "ZZ999" not in result["answer"]
    assert "W7" not in result["answer"]
    assert "لم أتمكن" in result["answer"]


def test_the_servers_own_graduation_answer_passes_its_own_validator():
    """A deterministic server-authored answer must never fail the checker.

    Both figure checks read the same clause. When only one of them disambiguated
    «فصل المقررات المرجعية ...: 3» - three TERMS, not three courses - the safe
    graduation answer was rejected by the very gate that was supposed to protect
    it, so repair regenerated the same text, the fallback returned the same text,
    and every incomplete simulation abstained after paying for an extra call.
    """
    graduation = {
        "tool": "graduation_progress",
        "ok": True,
        "plan_courses_passed": 32,
        "plan_courses_total": 48,
        "courses_remaining": 16,
        "credits_remaining_in_plan": 48,
        "estimated_additional_terms": 2,
        "estimated_terms_including_planning_baseline": 3,
        "lower_bound_additional_terms": 2,
        "lower_bound_terms_including_planning_baseline": 3,
        "simulation_completed": True,
        "credit_hour_gates": [{"code": "GS311", "required": 60, "remaining": 12}],
    }

    term_labelled_first = (
        "الإجمالي باحتساب فصل المقررات المرجعية المستخدمة في المحاكاة (1448/1): 3."
    )
    gate_shortfall = "الساعات المتبقية لاستيفاء الشرط: 12."
    plan_remainder = "الساعات المتبقية في خطتك 48 ساعة معتمدة."

    # A single sentence legitimately omits the rest of the graduation contract,
    # so completeness codes are expected here; what must never appear is a
    # figure mismatch against the evidence the sentence was built from.
    for sentence in (term_labelled_first, gate_shortfall, plan_remainder):
        assert EXACT_ACADEMIC_FIGURE_MISMATCH not in check_answer(
            sentence,
            tool_results=[graduation],
            question="كم متبقي للتخرج؟",
            required_tools={"graduation_progress"},
        ), sentence

    # The disambiguation must not become a blanket amnesty: a genuinely wrong
    # course count in the same shape is still caught.
    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        "أنجزت 99 مقررًا من الخطة.",
        tool_results=[graduation],
        question="كم متبقي للتخرج؟",
        required_tools={"graduation_progress"},
    )
    # And a gate figure that matches no gate is still caught.
    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        "الساعات المتبقية لاستيفاء الشرط: 7.",
        tool_results=[graduation],
        question="كم متبقي للتخرج؟",
        required_tools={"graduation_progress"},
    )


def test_timetable_proposal_cannot_invent_not_on_file_sections_or_meetings():
    proposal = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "alternatives": [
            {
                "option": "A1",
                "planner_options": ["A1"],
                "courses": [],
                "meetings": [],
                "unplaced_courses": [{"course_code": "AI331", "reason_code": "NOT_ON_FILE"}],
            }
        ],
    }
    invented = "الخيار A1 يضع AI331 في الشعبة F01 الساعة 09:00 في القاعة X1 مع الدكتور أحمد."

    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        invented,
        tool_results=[proposal],
        question="ابن جدولا مقترحا",
    )


def test_recommendation_allows_an_individual_verified_course_credit():
    recommendation = {
        "tool": "recommend_courses",
        "ok": True,
        "recommendation_count": 2,
        "recommendations": [
            {"course_code": "AI331", "credit_hours": 3},
            {"course_code": "CS372", "credit_hours": 4},
        ],
    }

    assert (
        check_answer(
            "توصية النظام تتضمن AI331، وهو مقرر من 3 ساعات.",
            tool_results=[recommendation],
            question="ما توصية النظام؟",
        )
        == []
    )


def test_negative_instructor_disclaimer_is_not_treated_as_an_invention():
    evidence = _exact_timetable_result()

    assert (
        check_answer(
            "يظهر AI1 في الشعبة M6، لكن اسم المحاضر غير مسجل في البيانات المعروضة.",
            tool_results=[evidence],
            question="من يدرس AI1؟",
        )
        == []
    )


def test_named_course_change_follow_up_cannot_return_unchanged_graduation_baseline(
    monkeypatch,
):
    baseline = _complete_graduation_result()
    calls: list[dict[str, Any]] = []

    def execute(name, arguments, **kwargs):
        calls.append(dict(arguments))
        return baseline

    monkeypatch.setattr("core.services.student_advisor_v2.execute_student_v2_tool", execute)
    client = FakeClient(
        _tool_turn("graduation_progress", {}),
        _answer_turn("الخطة المرجعية لم تتغير، وما زال STAT301 ضمن مقررات البداية."),
        _answer_turn("الخطة المرجعية لم تتغير، وما زال STAT301 ضمن مقررات البداية."),
        _answer_turn("الخطة المرجعية لم تتغير، وما زال STAT301 ضمن مقررات البداية."),
    )

    result = answer_student_advisor_v2(
        question="لو أحذف الاحتمالات والإحصاء وأضيف ذكاء الأعمال",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
    )

    assert calls == [{"planning_baseline_kind": "registered_timetable"}]
    assert result["agent"]["graduation_what_if_required"] is True
    assert result["agent"]["graduation_what_if_missing"] is True
    assert "تعذّر تشغيل مقارنة التغيير المطلوب" in result["answer"]
    assert "STAT301" not in result["answer"]
    assert result["presentation"] is None


def test_schedule_relations_reject_a_meeting_swapped_between_real_courses():
    evidence = _exact_timetable_result()
    swapped = (
        "Registered timetable: AI1, section M6, Monday 13:00-14:15, room M6. "
        "AI433, section M6, Sunday 14:30-15:45, room M7."
    )

    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        swapped,
        tool_results=[evidence],
        question="Show my registered timetable.",
        required_tools={"my_timetable"},
    )


def test_exact_timetable_contract_rejects_a_partial_one_of_five_answer():
    assert REQUESTED_EVIDENCE_OMITTED in check_answer(
        "The registered timetable includes AI1.",
        tool_results=[_exact_timetable_result()],
        question="Show every registered course.",
        required_tools={"my_timetable"},
    )


@pytest.mark.parametrize(
    "claim",
    (
        "You need 2 additional terms.",
        "You have passed 12 courses from the plan.",
    ),
)
def test_graduation_metrics_cannot_relabel_another_real_value(claim):
    evidence = {
        "tool": "graduation_progress",
        "ok": True,
        "plan_courses_passed": 41,
        "plan_courses_total": 53,
        "courses_remaining": 12,
        "credits_remaining_in_plan": 31,
        "credits_earned_registrar": 126,
        "estimated_additional_terms": 1,
        "estimated_terms_including_planning_baseline": 2,
    }

    assert EXACT_ACADEMIC_FIGURE_MISMATCH in check_answer(
        claim,
        tool_results=[evidence],
        question="When will I graduate?",
        required_tools={"graduation_progress"},
    )


def test_unrelated_exploratory_timetable_does_not_own_policy_completeness():
    policy = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.LOAD.SEMESTER_RANGE",
                "text": "The regulatory maximum is 19 credits.",
            }
        ],
    }

    assert (
        check_answer(
            "The regulatory maximum is 19 credits.",
            tool_results=[policy, _exact_timetable_result()],
            question="What is the regulatory maximum?",
            required_tools=set(),
        )
        == []
    )


def test_matching_graduation_card_fulfils_the_exact_list_contract_relationally():
    evidence = _complete_graduation_result()
    matching = {
        "kind": "graduation_scenario",
        "estimated_terms_including_planning_baseline": 6,
        "removed_current_courses": [],
        "added_current_courses": [],
        "graph": {
            "extraNodes": ["DS331", "MATH204", "CS211"],
            "termOf": {"DS331": 2, "MATH204": 2, "CS211": 3},
        },
    }
    swapped_terms = {
        **matching,
        "graph": {
            **matching["graph"],
            "termOf": {"DS331": 3, "MATH204": 2, "CS211": 2},
        },
    }
    answer = "The verified graduation scenario is displayed in the card."

    assert (
        check_answer(
            answer,
            tool_results=[evidence],
            question="Show my graduation plan.",
            required_tools={"graduation_progress"},
            presentation=matching,
        )
        == []
    )
    assert REQUESTED_EVIDENCE_OMITTED in check_answer(
        answer,
        tool_results=[evidence],
        question="Show my graduation plan.",
        required_tools={"graduation_progress"},
        presentation=swapped_terms,
    )


def test_matching_proposal_card_fulfils_course_section_contract_but_swap_does_not():
    evidence = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "alternatives": [
            {
                "option": "A1",
                "courses": [{"course_code": "CS211", "section": "M2"}],
                "meetings": [],
                "unplaced_courses": [],
            }
        ],
    }
    card = {
        "kind": "timetable_proposals",
        "alternatives": [
            {
                "planner_options": ["A1"],
                "courses": [{"course_code": "CS211", "section": "M2"}],
                "unplaced_courses": [],
            }
        ],
    }
    answer = "The verified proposal is displayed in the timetable card."

    assert (
        check_answer(
            answer,
            tool_results=[evidence],
            question="Build a proposed timetable.",
            required_tools={"build_timetable_proposal"},
            presentation=card,
        )
        == []
    )
    card["alternatives"][0]["courses"][0]["section"] = "M9"
    assert REQUESTED_EVIDENCE_OMITTED in check_answer(
        answer,
        tool_results=[evidence],
        question="Build a proposed timetable.",
        required_tools={"build_timetable_proposal"},
        presentation=card,
    )


def test_prior_artifact_course_name_must_keep_its_typed_term_relation():
    evidence = {
        "tool": "present_prior_artifact",
        "ok": True,
        "view": "course_names_by_term",
        "terms": [
            {
                "term_index": 2,
                "term_label": "الفصل المتوقع 1448/2",
                "courses": [{"course_code": "DS321", "course_name": "ذكاء الأعمال"}],
            },
            {
                "term_index": 3,
                "term_label": "الفصل المتوقع 1449/1",
                "courses": [{"course_code": "STAT301", "course_name": "الاحتمالات والإحصاء"}],
            },
        ],
    }

    assert (
        check_answer(
            ("الفصل المتوقع 1448/2: ذكاء الأعمال.\nالفصل المتوقع 1449/1: الاحتمالات والإحصاء."),
            tool_results=[evidence],
            question="ضع أسماء المقررات بدل الرموز.",
            required_tools={"present_prior_artifact"},
        )
        == []
    )
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        ("الفصل المتوقع 1449/1: ذكاء الأعمال.\nالفصل المتوقع 1449/1: الاحتمالات والإحصاء."),
        tool_results=[evidence],
        question="ضع أسماء المقررات بدل الرموز.",
        required_tools={"present_prior_artifact"},
    )


def test_prior_graduation_card_can_be_rendered_with_names_instead_of_codes(monkeypatch):
    prior = {
        "kind": "graduation_scenario",
        "program": "AI",
        "planning_term": "1448/1",
        "planning_baseline_kind": "registered_timetable",
        "simulation_completed": True,
        "estimated_terms_including_planning_baseline": 2,
        "lower_bound_terms_including_planning_baseline": 2,
        "max_credits_per_term": 18,
        "graph": {
            "items": [],
            "extraNodes": ["STAT301", "DS321"],
            "termOf": {"STAT301": 1, "DS321": 2},
            "nameOf": {
                "STAT301": "الاحتمالات والإحصاء",
                "DS321": "ذكاء الأعمال",
            },
            "statusOf": {"STAT301": "studying", "DS321": "open"},
        },
        "band_labels": {
            "1": "الفصل المرجعي 1448/1",
            "2": "الفصل التقديري 1448/2",
        },
        "unresolved_requirements": [],
        "removed_current_courses": [],
        "added_current_courses": [],
    }
    answer = (
        "الفصل المرجعي 1448/1:\n- الاحتمالات والإحصاء\n\nالفصل التقديري 1448/2:\n- ذكاء الأعمال"
    )
    client = FakeClient(
        _tool_turn("present_prior_artifact", {"view": "course_names_by_term"}),
        _answer_turn(answer),
    )
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda *_args, **_kwargs: pytest.fail("the prior-artifact transform is server-local"),
    )

    result = answer_student_advisor_v2(
        question="حط أسماء المقررات بدل الرموز.",
        principal=_principal(),
        academic_year=1448,
        term=1,
        llm_client=client,
        prior_presentation=prior,
    )

    assert result["answer"] == answer
    assert result["agent"]["evidence_validation_outcome"] == "passed"
    assert result["agent"]["tools_called"][-1]["name"] == "present_prior_artifact"
    assert "present_prior_artifact" in {schema["function"]["name"] for schema in client.schemas[0]}
