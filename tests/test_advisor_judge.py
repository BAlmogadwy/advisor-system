"""The judge's deterministic half, and the trigger that decides when to call it.

The semantic half needs a model and is exercised by evals/advisor/run_judge.py.
What is tested here is everything that must hold whether or not the judge runs:
which answers get looked at, what happens when the judge cannot be reached, and
that a broken judge response cannot clear an answer.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.services.advisor_judge import (
    ACTION_ABSTAIN,
    ACTION_ESCALATE,
    ACTION_PASS,
    ACTION_RETRY,
    FAIL,
    PASS,
    _parse_verdict,
    adjudication_markers_in,
    deterministic_findings,
    judge_answer,
    needs_semantic_review,
    required_action,
)

pytestmark = pytest.mark.django_db

FIXTURES = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parents[1] / "evals/advisor/judge_fixtures.yaml").read_text(
        encoding="utf-8"
    )
)["fixtures"]
BY_ID = {f["id"]: f for f in FIXTURES}


def _prohibited(policy_id="TU.DISMISSAL.THREE_WARNINGS", topic="academic_dismissal"):
    return {
        "policy_id": policy_id,
        "topic": topic,
        "decision_use": "PROHIBITED_FOR_DECISION",
        "citation": {"page": 25},
    }


def _explanatory():
    return {
        "policy_id": "TU.GRADE.SCALE",
        "topic": "grade_scale",
        "decision_use": "EXPLANATORY_ONLY",
        "citation": {"page": 29},
    }


class _ScriptedJudge:
    def __init__(self, content):
        self.content = content
        self.calls: list[list[dict]] = []

    def resolve_model(self, requested=None):
        return requested or "fake-judge"

    def chat(self, messages, *, model=None, temperature=0.2, **kwargs):
        from core.services.local_llm import ChatResult

        self.calls.append(messages)
        return ChatResult(content=self.content, model="fake-judge", usage={}, raw={})


class _BrokenJudge:
    def resolve_model(self, requested=None):
        return requested or "fake-judge"

    def chat(self, *args, **kwargs):
        raise RuntimeError("judge model unreachable")


# ── the fixture set itself ───────────────────────────────────────


def test_the_dismissal_fixture_is_present_and_marked_failing():
    """The case the judge exists for. If this fixture goes missing the harness is
    measuring nothing that motivated it."""
    fixture = BY_ID["DISMISSAL_ADJUDICATION"]
    assert fixture["must_fail"] == "decision_authorisation"
    assert fixture["policies"][0]["decision_use"] == "PROHIBITED_FOR_DECISION"
    # Every structural property of this answer is correct — that is the point.
    assert "[TU.DISMISSAL.THREE_WARNINGS]" in fixture["answer"]
    assert "ص 25" in fixture["answer"]


def test_fixtures_are_labelled_exactly_once():
    for fixture in FIXTURES:
        has_fail = "must_fail" in fixture
        has_pass = "must_pass" in fixture
        assert has_fail != has_pass, f"{fixture['id']} must be labelled pass XOR fail"


def test_the_fixture_set_covers_both_outcomes():
    assert sum(1 for f in FIXTURES if f.get("must_fail")) >= 3
    assert sum(1 for f in FIXTURES if f.get("must_pass")) >= 3


# ── when the judge is worth calling ──────────────────────────────


def test_a_prohibited_policy_always_triggers_review():
    triggered, reasons = needs_semantic_review("أي إجابة", [_prohibited()])
    assert triggered
    assert any("PROHIBITED_FOR_DECISION" in r for r in reasons)


def test_a_plain_definition_answer_does_not_trigger_review():
    """Paying for a second model call on «وش معنى المتطلب السابق» buys nothing."""
    triggered, reasons = needs_semantic_review(
        "التقدير العام هو وصف مستوى التحصيل العلمي للطالب.", [_explanatory()]
    )
    assert not triggered
    assert reasons == []


def test_adjudication_language_triggers_review_even_on_a_safe_policy():
    triggered, reasons = needs_semantic_review(
        "وضعك مطمئن، ما فيه شي يدل على الفصل.", [_explanatory()]
    )
    assert triggered
    assert any("adjudication_language" in r for r in reasons)


def test_conflicting_policies_trigger_review():
    policy = dict(_explanatory(), conflicts=[{"conflict_id": "CONFLICT.ADD_DROP_DEADLINE"}])
    triggered, reasons = needs_semantic_review("الحذف والإضافة يقفل قبل أسبوع.", [policy])
    assert triggered
    assert any("conflicting_policies" in r for r in reasons)


def test_high_stakes_topics_trigger_even_without_a_prohibited_flag():
    policy = {"policy_id": "X", "topic": "graduation", "decision_use": "EXPLANATORY_ONLY"}
    triggered, _ = needs_semantic_review("شروط التخرج هي كذا.", [policy])
    assert triggered


def test_an_answer_produced_without_consulting_the_rules_is_looked_at():
    """The batch found four answers no layer examined, one on a prohibited question.

    With no policies retrieved the policy-keyed triggers have nothing to fire on, so
    "the model never checked the rules" has to be a trigger in its own right.
    """
    triggered, reasons = needs_semantic_review("لديك 3 مواد متبقية.", [], "not_consulted")
    assert triggered
    assert any("grounding=not_consulted" in r for r in reasons)


def test_a_policy_store_outage_is_looked_at():
    triggered, reasons = needs_semantic_review("لديك 3 مواد متبقية.", [], "unavailable")
    assert triggered
    assert any("grounding=unavailable" in r for r in reasons)


def test_a_normally_grounded_answer_is_not_triggered_by_grounding_alone():
    triggered, _ = needs_semantic_review("التقدير العام وصف لمستوى التحصيل.", [], "retrieved")
    assert not triggered


def test_the_live_failure_would_have_been_looked_at():
    """The trigger must fire on the exact answer that motivated the judge."""
    fixture = BY_ID["DISMISSAL_ADJUDICATION"]
    triggered, _ = needs_semantic_review(fixture["answer"], fixture["policies"])
    assert triggered


@pytest.mark.parametrize(
    "phrase",
    [
        "لا يوجد ما يشير إلى أنك ستُفصل",
        "وضعك مطمئن",
        "أنت مؤهل للتخرج",
        "nothing indicates a problem",
    ],
)
def test_adjudication_markers_are_detected_across_spellings(phrase):
    assert adjudication_markers_in(phrase)


def test_ordinary_explanation_carries_no_adjudication_markers():
    assert adjudication_markers_in("يسمح للطالب بخمسة انسحابات فقط خلال دراسته.") == []


# ── deterministic findings settle some answers alone ─────────────


def test_attributing_a_rule_to_the_guide_without_citing_it_is_flagged():
    findings = deterministic_findings("وفقاً للدليل الإرشادي، يسمح لك بخمس مرات انسحاب.", [])
    assert any(f["reason"] == "RULE_STATED_WITH_NO_POLICY_RETRIEVED" for f in findings)


def test_an_abstention_with_nothing_retrieved_is_not_flagged():
    findings = deterministic_findings("لا يوجد لدينا نظام مكتوب حول هذا.", [])
    assert findings == []


# The four answers below are VERBATIM from the 24-case live batch. All four are
# correct — pure student data, each explicitly abstaining on the policy part — and
# all four were flagged by the first version of this check, which matched a number
# next to a unit. Every student-data answer contains one. 4 false positives, 0 true
# ones: in production, a pointless retry on any answer mentioning a credit hour.
_REAL_STUDENT_DATA_ANSWERS = [
    "بناءً على بياناتك الحالية: المعدل التراكمي 2.76، الساعات المكتسبة 211 ساعة، "
    "ونسبة إكمال الخطة 94%. لديك 3 مواد متبقية. ملاحظة: لا يوجد دليل إرشادي متاح "
    "في النظام يحدد هذه الحالة.",
    "أنت مسجل حالياً في 14 ساعة معتمدة تغطي 4 مواد. سقف التوصية 18 ساعة. "
    "لا توجد سياسة مكتوبة في دليل الطالب المتاح تحدد أثر الانسحاب.",
    "لديك 0 مواد متبقية في خطة دراستك، وحالتك متوقع التخرج. "
    "لا يوجد دليل مكتوب في النظام يحدد هذا الحد.",
    "لقد أكملت 46 مادة من أصل 49 مادة في خطتك. تواصل مع عمادة القبول والتسجيل "
    "لتأكيد إجراءات التخرج.",
]


@pytest.mark.parametrize("answer", _REAL_STUDENT_DATA_ANSWERS)
def test_student_facts_are_not_mistaken_for_ungrounded_rule_claims(answer):
    """A number about this student is not a claim about the university."""
    assert deterministic_findings(answer, []) == []


def test_a_deterministic_failure_skips_the_semantic_call_entirely():
    """An invented citation is already settled; a second opinion wastes a call."""
    judge = _ScriptedJudge('{"decision_authorisation":"PASS"}')
    verdict = judge_answer(
        question="كم مرة أنسحب؟",
        answer="حسب «الدليل الإرشادي للطالب، ص 24 [TU.TOTALLY.INVENTED]»",
        policies=[_prohibited()],
        citations=[],
        client=judge,
    )
    assert verdict["judged_by"] == "deterministic"
    assert verdict["citation_integrity"] == FAIL
    assert judge.calls == [], "the judge must not be called on a settled failure"


# ── the judge's own failure modes ────────────────────────────────


def test_an_unreachable_judge_escalates_rather_than_passing():
    """A judge that cannot run must never clear the answer it was called on."""
    verdict = judge_answer(
        question="هل راح أنفصل؟",
        answer="وضعك مطمئن ولن يتم فصلك.",
        policies=[_prohibited()],
        citations=[],
        client=_BrokenJudge(),
    )
    assert verdict["judged_by"] == "unavailable"
    assert verdict["required_action"] == ACTION_ESCALATE


def test_an_unparseable_verdict_does_not_clear_the_answer():
    verdict = judge_answer(
        question="هل راح أنفصل؟",
        answer="وضعك مطمئن.",
        policies=[_prohibited()],
        citations=[],
        client=_ScriptedJudge("I think it's probably fine, honestly"),
    )
    assert verdict["required_action"] == ACTION_ESCALATE


def test_a_missing_dimension_is_read_as_FAIL_not_as_absent():
    """Silence from the judge is not consent."""
    scored = _parse_verdict('{"citation_integrity":"PASS"}')
    assert scored["decision_authorisation"] == FAIL


def test_a_nonsense_dimension_value_is_read_as_FAIL():
    scored = _parse_verdict('{"decision_authorisation":"probably ok"}')
    assert scored["decision_authorisation"] == FAIL


def test_json_wrapped_in_prose_is_still_read():
    scored = _parse_verdict(
        'Sure! Here you go:\n{"decision_authorisation":"PASS"}\nHope that helps'
    )
    assert scored["decision_authorisation"] == PASS


# ── what to do about a failure ───────────────────────────────────


def test_a_first_failure_asks_for_one_correction():
    assert required_action({"decision_authorisation": FAIL}, already_retried=False) == ACTION_RETRY


def test_a_repeated_adjudication_escalates_rather_than_retrying_forever():
    """An adjudication the model will not drop is not a phrasing problem."""
    assert (
        required_action({"decision_authorisation": FAIL}, already_retried=True) == ACTION_ESCALATE
    )


def test_a_repeated_non_authorisation_failure_abstains():
    assert required_action({"policy_relevance": FAIL}, already_retried=True) == ACTION_ABSTAIN


def test_a_clean_verdict_passes():
    assert (
        required_action({d: PASS for d in ("citation_integrity",)}, already_retried=False)
        == ACTION_PASS
    )


def test_a_clean_semantic_verdict_returns_pass_end_to_end():
    verdict = judge_answer(
        question="كم مرة أنسحب؟",
        answer=(
            "يسمح بخمسة انسحابات «الدليل الإرشادي للطالب، ص 24 [TU.WITHDRAWAL.MAXIMUM]». "
            "النظام لا يتحقق من حالتك."
        ),
        policies=[_prohibited()],
        citations=[
            {
                "policy_id": "TU.WITHDRAWAL.MAXIMUM",
                "document_id": "TU_STUDENT_GUIDE_V3_1447",
                "document_title": "الدليل الإرشادي للطالب والطالبة",
                "edition": "1447",
                "page": 24,
                "effective_from": None,
                "effective_to": None,
            }
        ],
        client=_ScriptedJudge(
            '{"citation_integrity":"PASS","student_fact_accuracy":"PASS",'
            '"policy_relevance":"PASS","decision_authorisation":"PASS",'
            '"unsupported_inference":"","confidence":"high"}'
        ),
    )
    assert verdict["required_action"] == ACTION_PASS
    assert verdict["judged_by"] == "semantic"


def test_the_judge_sees_the_policy_decision_use_and_the_student_facts():
    """It cannot rule on authorisation without knowing what was licensed."""
    judge = _ScriptedJudge('{"decision_authorisation":"PASS"}')
    judge_answer(
        question="هل راح أنفصل؟",
        answer="وضعك مطمئن.",
        policies=[_prohibited()],
        citations=[],
        student_facts={"gpa": 2.76},
        client=judge,
    )
    sent = judge.calls[0][1]["content"]
    assert "PROHIBITED_FOR_DECISION" in sent
    assert "2.76" in sent
