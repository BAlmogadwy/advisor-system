"""A route is the server's decision, not something the model paraphrases.

Written from a live failure. A student asked for a timetable ignoring their
current registration; `build_my_timetable` refused correctly and returned
`REBUILD_REQUIRES_PLANNER_CONFIRMATION` / `OPEN_STUDENT_PLANNER` — "available,
through the planner, after a confirmation". The loop handed that to the model,
which answered «لا يمكن للنظام بناء جدول يتجاهل تسجيلك الحالي» and told the
student to delete their courses in the university portal.

The dangerous part is the last clause. The rebuild produces a PLANNING DRAFT and
never touches the registration record; a student following that advice loses
real seats to obtain a draft. So the assertions below are not about wording —
they are that the model is never asked.
"""

from __future__ import annotations

import json

import pytest

from core.models import Student
from core.services import virtual_advisor as va
from core.services import virtual_advisor_capabilities as caps
from core.services.advisor_actions import OPEN_STUDENT_PLANNER, handoff_for
from core.services.advisor_principal import AdvisorPrincipal
from core.services.rbac import ROLE_STUDENT
from tests.test_llm_remote_execution_boundary import ExecutionSpy, ScriptedClient, _call

pytestmark = pytest.mark.django_db

MINE = 7301001

REFUSAL = {
    "tool": "build_my_timetable",
    "ok": False,
    "reason": "REBUILD_REQUIRES_PLANNER_CONFIRMATION",
    "error": "Rebuilding without the student's current sections requires confirmation.",
    "action": "OPEN_STUDENT_PLANNER",
}


@pytest.fixture(autouse=True)
def _student() -> None:
    Student.objects.get_or_create(
        student_id=MINE, defaults={"name": "طالب", "program": "AI", "section": "M"}
    )


def _principal() -> AdvisorPrincipal:
    return AdvisorPrincipal(role=ROLE_STUDENT, student_id=MINE)


def _ask(client, question: str = "ابني لي جدول وتجاهل المسجل حاليا") -> dict:
    return va.answer_virtual_advisor(
        question=question, principal=_principal(), academic_year=1448, term=1, client=client
    )


def test_the_model_never_sees_the_route_and_never_writes_the_answer(monkeypatch) -> None:
    """THE test. The provider is not asked a second time, and the answer is a
    constant from this repository rather than anything a model produced."""
    ExecutionSpy(monkeypatch, result=REFUSAL)
    client = ScriptedClient(
        [_call("build_my_timetable", {"keep_current_sections": False})],
        final="لا يمكن للنظام بناء جدول يتجاهل تسجيلك الحالي.",  # what it said live
    )
    payload = _ask(client)

    # ONE provider call: the turn that requested the tool. No continuation.
    assert len(client.requests) == 1, "the model was asked to interpret the route"
    assert payload["answer"] != client.final
    assert payload["agent"]["action_handoff"] == OPEN_STUDENT_PLANNER


def test_the_answer_says_the_feature_exists_and_registration_is_untouched() -> None:
    """The three failures of the live answer, asserted as their opposites."""
    handoff = handoff_for(REFUSAL)
    assert handoff is not None
    arabic = handoff.answer("Arabic")

    # 1. it is available — a confirmation requirement, not a denial.
    assert "لا يمكن" not in arabic
    assert "تأكيد" in arabic and "المخطط الدراسي" in arabic
    # 2. the student is routed INTO the planner, not out to the portal…
    assert "افتح المخطط الدراسي" in arabic
    # 3. …and told plainly that nothing was changed. This is the sentence that
    #    matters most: the live answer's advice would have cost real seats.
    assert "لن يحذف أو يغيّر تسجيلك الرسمي" in arabic
    assert handoff.registration_modified is False


def test_the_interface_gets_a_structured_action_not_prose(monkeypatch) -> None:
    """A button cannot be rendered from a sentence, and a sentence is what the
    UI would have had to parse."""
    ExecutionSpy(monkeypatch, result=REFUSAL)
    payload = _ask(ScriptedClient([_call("build_my_timetable", {"keep_current_sections": False})]))

    assert payload["action"] == {
        "type": "OPEN_STUDENT_PLANNER",
        "intent": "REBUILD_WITHOUT_CURRENT_SECTIONS",
        "requires_confirmation": True,
        "registration_modified": False,
    }


def test_every_response_carries_the_action_key(monkeypatch) -> None:
    """`None`, not absent — so a consumer tests one key rather than two shapes."""
    ExecutionSpy(monkeypatch, result={"tool": "my_progress", "ok": True})
    payload = _ask(ScriptedClient([_call("my_progress", {})]), question="كيف حالي؟")
    assert payload["action"] is None


def test_an_english_question_gets_the_english_handoff(monkeypatch) -> None:
    ExecutionSpy(monkeypatch, result=REFUSAL)
    payload = _ask(
        ScriptedClient([_call("build_my_timetable", {"keep_current_sections": False})]),
        question="Build me a timetable ignoring what I am registered in",
    )
    assert "study planner" in payload["answer"]
    assert "does not delete or change your official registration" in payload["answer"]


def test_an_ordinary_result_is_not_intercepted() -> None:
    """The loop is unchanged for the tools that return data — which is all of
    them but one. A router that fires on anything unexpected would replace real
    answers with a referral."""
    for result in (
        {"tool": "my_progress", "ok": True, "reason": "SOMETHING_ELSE"},
        {"tool": "build_my_timetable", "ok": True, "placed": []},
        {"ok": False, "error": "boom"},
        "not a dict",
        None,
    ):
        assert handoff_for(result) is None


def test_a_chat_rebuild_request_is_routed_before_any_work_happens() -> None:
    """The guard is the FIRST thing in the executor, and it has to be.

    It used to sit after an early return for "nothing to schedule", so a student
    with an empty plan asking for a rebuild got an ordinary empty result and no
    route. That was harmless while the model was told never to call — and became
    the likely path the moment the description told it to. Whether a rebuild is
    refused must not depend on how much the recommender happened to find first.

    This student has no courses at all, which is exactly the shape that used to
    slip past.
    """
    result = caps.get_default_registry().execute(
        "build_my_timetable",
        {"keep_current_sections": False},
        scope={"role": ROLE_STUDENT, "student_id": MINE},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert not result.get("placed"), "chat rebuilt a timetable without the current sections"
    assert result["ok"] is False
    assert result["reason"] == "REBUILD_REQUIRES_PLANNER_CONFIRMATION"
    assert result["action"] == "OPEN_STUDENT_PLANNER"


def test_the_handoff_text_names_no_student_and_no_identifier() -> None:
    """It is server-authored and skips the output contract, so its safety is a
    property of the constant rather than of a check that runs over it."""
    for handoff in (handoff_for(REFUSAL),):
        assert handoff is not None
        body = json.dumps([handoff.answer("Arabic"), handoff.answer("English")], ensure_ascii=False)
        assert not any(ch.isdigit() for ch in body), "a digit in a fixed referral is suspect"
        assert "STUDENT_REF" not in body
        assert "REDACTED" not in body


def test_the_tool_description_tells_the_model_to_call_not_to_answer() -> None:
    """The live failure, at its actual root.

    The description read "Discarding those sections and rebuilding the whole week
    is not available here at all: if the student asks for that, tell them to open
    the planner". The model obeyed it exactly — it never called, so the server
    never saw the request, and the model wrote the routing prose itself. The
    deterministic handoff was downstream of a call the description forbade.

    Two things must therefore stay true: the model is told to CALL, and the
    parameter that expresses the intent is actually advertised. Without the
    second, the intent is inexpressible and the route is unreachable.
    """
    capability = caps.get_default_registry().capabilities["build_my_timetable"]
    description = capability.description

    assert "keep_current_sections=false" in description, "the model is not told to call"
    assert "Do not answer that request yourself" in description
    assert "not available here at all" not in description, "the denial wording is back"

    properties = capability.parameters.get("properties") or {}
    assert "keep_current_sections" in properties, "the intent cannot be expressed"
    assert properties["keep_current_sections"]["type"] == "boolean"
