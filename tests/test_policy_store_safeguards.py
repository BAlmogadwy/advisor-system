"""Policy store failures must preserve obligations and fail closed at real entrypoints."""

import json

import pytest

from core.models import Course, ProgrammeRequirement, Student
from core.services import llm_backend, policy_contract, policy_store
from core.services.advisor_principal import AdvisorPrincipal
from core.services.llm_backend import ChatResult, ToolChatResult
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import answer_student_advisor_v2
from core.services.student_advisor_v21 import answer_student_advisor_v21
from core.services.virtual_advisor import answer_virtual_advisor
from tests.test_student_advisor_v21 import ScriptedClient, _planner_turn

pytestmark = pytest.mark.django_db
SID = 4999911
BAD = "يسمح لك بالانسحاب من أي مقرر بلا حدود."


class FakeClient:
    backend = "local"
    supports_assistant_prefill = True

    def __init__(self, answer=BAD):
        self.calls = []
        self.answer = answer

    def resolve_model(self, requested_model=None):
        return "audit-fake"

    def chat_with_tools(self, messages, **kwargs):
        self.calls.append(messages)
        return ToolChatResult(
            content=self.answer,
            tool_calls=(),
            model="audit-fake",
            usage={},
            assistant_message={"role": "assistant", "content": self.answer},
        )

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return ChatResult(content=self.answer, model="audit-fake", usage={})


def boom(*args, **kwargs):
    raise OSError("synthetic_policy_store_outage")


@pytest.fixture(autouse=True)
def fixture_db_and_network(monkeypatch, settings):
    monkeypatch.setattr(llm_backend, "_http_open", boom)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    settings.STUDENT_ADVISOR_V2_ENABLED = True
    settings.STUDENT_ADVISOR_V21_ENABLED = True
    settings.VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED = False
    settings.LLM_BACKEND = "local"
    from core.services import rbac

    rbac._groups_ensured = False
    Student.objects.create(student_id=SID, name="Audit Synthetic", program="CS", section="M")
    Course.objects.create(course_code="CS101", description="Audit Foundations", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="CS",
        course_code="CS101",
        course_name="Audit Foundations",
        credit_hours=3,
        programme_term=1,
    )
    from core.services.course_catalogue import invalidate_cache

    invalidate_cache()


def install_fault(monkeypatch, fault):
    if fault == "topic_resolution":
        monkeypatch.setattr(policy_store.PolicyStore, "resolve_topics", boom)
    elif fault == "store_access":
        monkeypatch.setattr(policy_store, "get_policy_store", boom)
        monkeypatch.setattr(policy_contract, "get_policy_store", boom)
    elif fault == "contract_only":
        monkeypatch.setattr(policy_contract, "get_policy_store", boom)
    elif fault in {"query_lookup_only", "id_lookup_only"}:
        original = policy_store.PolicyStore.lookup

        def fail_query(self, *args, **kwargs):
            if kwargs.get("query" if fault == "query_lookup_only" else "policy_ids"):
                return boom()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(policy_store.PolicyStore, "lookup", fail_query)


@pytest.mark.parametrize(
    "fault",
    [
        "healthy",
        "topic_resolution",
        "store_access",
        "contract_only",
        "query_lookup_only",
        "id_lookup_only",
    ],
)
@pytest.mark.parametrize("entry", ["legacy", "v2", "v21_policy", "v21_data", "v21_recommend"])
def test_policy_store_failures_at_real_service_entrypoints(monkeypatch, entry, fault):
    question = "الانسحاب من المقرر"
    principal = AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID)
    install_fault(monkeypatch, fault)
    if entry.startswith("v21"):
        tool = {
            "v21_policy": "policy_lookup",
            "v21_data": "my_progress",
            "v21_recommend": "recommend_courses",
        }[entry]
        question = {
            "v21_policy": question,
            "v21_data": "اعرض تقدمي في الخطة الدراسية",
            "v21_recommend": "اقترح لي مقررات للفصل القادم",
        }[entry]
        client = ScriptedClient(
            _planner_turn(
                "execute",
                [
                    {
                        "capability": tool,
                        "arguments": {"query": question} if tool == "policy_lookup" else {},
                    }
                ],
            )
        )
    else:
        client = FakeClient()
    kwargs = {"question": question, "principal": principal, "academic_year": 1448, "term": 1}
    if entry == "legacy":
        result = answer_virtual_advisor(**kwargs, client=client)
    elif entry == "v2":
        result = answer_student_advisor_v2(**kwargs, llm_client=client)
    else:
        result = answer_student_advisor_v21(**kwargs, llm_client=client)
    agent = result["agent"]
    assert BAD not in result["answer"]
    if entry in {"legacy", "v2", "v21_policy"}:
        assert agent["policy_required"] is True
    if entry == "v21_policy" and fault in {"topic_resolution", "store_access", "query_lookup_only"}:
        assert agent["evidence_validation_outcome"] == "abstained"
    if entry == "v21_data":
        assert agent["policy_required"] is False
        assert agent["evidence_validation_outcome"] == "passed"
    if entry == "v21_recommend" and fault in {"store_access", "id_lookup_only"}:
        assert agent["citation_refused"] is True
        assert agent["credit_policy_unavailable"] is True
        assert agent["evidence_validation_outcome"] == "abstained"
        assert "لا توجد توصية جديدة" in result["answer"]
        assert "regulatory_max_credit_hours" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "mode", ["supported", "uncited", "background", "not_citable", "validator_down"]
)
def test_v2_requires_an_allowed_governing_citation(monkeypatch, mode):
    from core.services import student_advisor_v2 as runtime
    from core.services.virtual_advisor import _seed_policy_evidence

    evidence, _ = _seed_policy_evidence("الانسحاب من المقرر")
    policy_id = evidence["direct_policy_evidence"][0]["policy_id"]
    answer = f"راجع القاعدة الموثقة للانسحاب من المقرر. [{policy_id}]"
    if mode == "uncited":
        answer = BAD
    elif mode == "background":
        evidence["background_policy_evidence"] = evidence["direct_policy_evidence"]
        evidence["direct_policy_evidence"] = []
    elif mode == "not_citable":
        evidence["citable"] = []
    elif mode == "validator_down":
        monkeypatch.setattr(policy_store.PolicyStore, "validate_citations", boom)
    monkeypatch.setattr(runtime, "_seed_policy_evidence", lambda *args: (evidence, "retrieved"))
    result = answer_student_advisor_v2(
        question="الانسحاب من المقرر",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID),
        academic_year=1448,
        term=1,
        llm_client=FakeClient(answer),
    )
    assert result["agent"]["policy_required"] is True
    assert result["agent"]["citation_refused"] is (mode != "supported")
    if mode == "supported":
        assert result["answer"] == answer
        assert policy_id in result["cited_policy_ids"]
    else:
        assert result["agent"]["evidence_validation_outcome"] == "abstained"
        assert result["answer"] != answer
        assert result["cited_policy_ids"] == []


@pytest.mark.parametrize("entry", ["v2", "v21"])
def test_mixed_request_keeps_verified_progress_during_policy_outage(monkeypatch, entry):
    from core.services import student_advisor_v2 as runtime
    from tests.test_student_advisor_v2 import _answer_turn, _tool_turn

    question = "اعرض تقدمي، وكم مرة يسمح بالانسحاب من المقرر؟"
    progress = {
        "tool": "my_progress",
        "ok": True,
        "counts": {"open": 1, "locked": 0},
        "prerequisites_satisfied": [{"code": "CS101"}],
        "prerequisite_blocked": [],
    }
    original = runtime.execute_student_v2_tool
    monkeypatch.setattr(
        runtime,
        "execute_student_v2_tool",
        lambda name, arguments, **kwargs: progress
        if name == "my_progress"
        else original(name, arguments, **kwargs),
    )
    install_fault(monkeypatch, "topic_resolution")
    if entry == "v21":
        fake = ScriptedClient(
            _planner_turn(
                "execute",
                [
                    {"capability": "my_progress", "arguments": {}},
                    {"capability": "policy_lookup", "arguments": {"query": question}},
                ],
            )
        )
        call = answer_student_advisor_v21
    else:
        fake = ScriptedClient(_tool_turn("my_progress", {}), _answer_turn(BAD))
        call = answer_student_advisor_v2
    result = call(
        question=question,
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID),
        academic_year=1448,
        term=1,
        llm_client=fake,
    )
    assert "CS101" in result["answer"]
    assert BAD not in result["answer"]
    assert result["agent"]["policy_required"] is True
    assert result["agent"]["citation_refused"] is True
    assert result["agent"]["evidence_validation_outcome"] == "abstained"


@pytest.mark.parametrize(
    "suffix", ["", " Can I withdraw without permission?", " هل يسمح بالانسحاب؟"]
)
def test_v2_standalone_course_question_does_not_exempt_added_policy(suffix):
    from core.services.student_advisor_v2 import _v2_policy_required

    assert _v2_policy_required("What can I take next?" + suffix) is bool(suffix)


@pytest.mark.parametrize("fault", ["topic_resolution", "store_access"])
def test_legacy_agent_loop_also_refuses_policy_during_outage(monkeypatch, settings, fault):
    settings.VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED = True
    install_fault(monkeypatch, fault)
    result = answer_virtual_advisor(
        question="الانسحاب من المقرر",
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID),
        academic_year=1448,
        term=1,
        client=FakeClient(),
    )
    assert result["agent"]["loop_used"] is True
    assert result["agent"]["policy_required"] is True
    assert BAD not in result["answer"]
    if fault == "store_access":
        assert result["policy_part"]["status"] == "ABSTAINED"
        assert "regulatory_max_credit_hours" not in json.dumps(result, ensure_ascii=False)


def test_missing_expected_graduate_backing_record_is_unavailable(monkeypatch):
    from core.services.credit_policy import (
        BACKING_POLICY_IDS,
        EXPECTED_GRADUATE_STATUS,
        credit_policy_evidence,
    )
    from core.services.virtual_advisor import _credit_policy_evidence_citations

    original = policy_store.PolicyStore.lookup

    def missing_one(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result["policies"] = [
            row
            for row in result["policies"]
            if row["policy_id"] != BACKING_POLICY_IDS["expected_graduate"]
        ]
        return result

    monkeypatch.setattr(policy_store.PolicyStore, "lookup", missing_one)
    evidence = credit_policy_evidence(12, [], term=1, student_status=EXPECTED_GRADUATE_STATUS)
    result = _credit_policy_evidence_citations({"recommendation_policy": evidence})
    assert result["ok"] is False
    assert result["error_code"] == "CREDIT_POLICY_UNAVAILABLE"
    assert result["citable"] == []


_INSTITUTIONAL_QUESTIONS = [
    "أقدر أسجل شعبتين بينها تعارض بسيط؟",
    "أقدر أغير الشعبة بسبب تعارض الاختبارات؟",
    "الحذف والإضافة على بوابة الطالب لمقررات التخصص الأساسي يفتح متى بالضبط؟ يعني كم يوم عندي قبل ما تبدأ الدراسة في الترم الأول 1448؟",
    "متى يفتح تسجيل الرغبات للفصل الثاني؟ وياليت تعطيني المواد اللي المفروض أحطها.",
    "When does course registration open?",
]


@pytest.mark.parametrize("question", _INSTITUTIONAL_QUESTIONS)
@pytest.mark.parametrize("entry", ["legacy", "v2"])
def test_institutional_permission_and_calendar_questions_keep_the_contract(
    monkeypatch, entry, question
):
    from core.services.advisor_intent import route_intent

    contract = policy_contract.build_policy_contract_state(
        question,
        [],
        grounding_state="unavailable",
        intent=route_intent(question),
    )
    assert contract.required is True
    install_fault(monkeypatch, "topic_resolution")
    kwargs = dict(
        question=question,
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID),
        academic_year=1448,
        term=1,
    )
    fake = FakeClient()
    result = (
        answer_virtual_advisor(**kwargs, client=fake)
        if entry == "legacy"
        else answer_student_advisor_v2(**kwargs, llm_client=fake)
    )
    assert result["agent"]["policy_required"] is True
    assert BAD not in result["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "متى يفتح لي مقرر AI352 إذا اجتزت AI331؟",
        "أقدر أغير لون الجدول؟",
        "أقدر أسجل ملاحظة عن جدولي؟",
        "أقدر أشوف جدولي؟",
        "وش المقررات اللي أقدر أسجلها هذا الفصل؟",
        "What can I take next?",
        "When will AI352 open after I pass AI331?",
    ],
)
def test_record_and_interface_requests_do_not_require_institutional_policy(question):
    from core.services.advisor_intent import route_intent

    state = policy_contract.build_policy_contract_state(
        question,
        [],
        grounding_state="none_matched",
        intent=route_intent(question),
    )
    assert state.required is False


@pytest.mark.parametrize(
    "entry,fault,status",
    [
        ("v21_policy", "store_access", 201),
        ("v21_recommend", "store_access", 201),
        ("v2", "topic_resolution", 201),
        ("legacy", "topic_resolution", 200),
        ("legacy", "store_access", 200),
    ],
)
def test_policy_store_failures_at_http_entrypoints(
    monkeypatch, settings, client, django_user_model, entry, fault, status
):
    from django.urls import reverse

    from core.models import AdvisorConversation
    from core.services import rbac, student_otp, virtual_advisor
    from core.services import student_advisor_v2 as runtime

    question = "الانسحاب من المقرر"
    if entry == "legacy":
        user = django_user_model.objects.create_user(
            username="audit_staff", is_superuser=True, is_staff=True
        )
        client.force_login(user)
        client.raise_request_exception = False
        fake = FakeClient()
        monkeypatch.setattr(virtual_advisor, "get_llm_client", lambda: fake)
        url = reverse("virtual_advisor_chat")
        body = {"message": question, "student_id": SID, "academic_year": 1448, "term": 1}
    else:
        rbac.ensure_role_groups()
        client.force_login(student_otp.provision_student_user(SID))
        conversation = AdvisorConversation.objects.create(student_id=SID)
        url = reverse("advisor_conversation_send", args=[conversation.id])
        if entry == "v2":
            settings.STUDENT_ADVISOR_V21_ENABLED = False
            fake = FakeClient()
        else:
            tool = "recommend_courses" if entry == "v21_recommend" else "policy_lookup"
            if entry == "v21_recommend":
                question = "اقترح لي مقررات للفصل القادم"
            fake = ScriptedClient(
                _planner_turn(
                    "execute",
                    [
                        {
                            "capability": tool,
                            "arguments": {"query": question} if tool == "policy_lookup" else {},
                        }
                    ],
                )
            )
        monkeypatch.setattr(runtime, "get_llm_client", lambda: fake)
        body = {"message": question, "idempotency_key": "policy-audit"}
    install_fault(monkeypatch, fault)
    response = client.post(url, data=json.dumps(body), content_type="application/json")
    row = {
        "probe": "http",
        "entry": entry,
        "fault": fault,
        "status": response.status_code,
        "provider_calls": len(fake.calls),
    }
    if response.status_code != 500:
        row["body"] = response.json()
    row["bad_answer_survived"] = BAD in json.dumps(row.get("body", {}), ensure_ascii=False)
    assert response.status_code == status
    assert row["bad_answer_survived"] is False
