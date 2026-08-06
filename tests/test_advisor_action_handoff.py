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


def test_a_chat_rebuild_request_never_places_a_section() -> None:
    """The handoff explains a refusal; it must not become the refusal.

    Asserted as the outcome rather than as the guard, because the guard is not
    the only thing standing here: `build_my_timetable` returns early when the
    recommender has nothing to schedule, BEFORE the `keep_current_sections`
    check. A student with an empty plan therefore gets an ordinary empty result
    rather than the routed refusal — safe, since nothing is rebuilt either way,
    but it means a test aimed at the guard can pass while never reaching it.

    What must hold for every shape of student is the outcome: a chat request
    that asks to drop current sections never produces a placement.
    """
    result = caps.get_default_registry().execute(
        "build_my_timetable",
        {"keep_current_sections": False},
        scope={"role": ROLE_STUDENT, "student_id": MINE},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert not result.get("placed"), "chat rebuilt a timetable without the current sections"
    if result.get("ok") is False:
        assert result["reason"] == "REBUILD_REQUIRES_PLANNER_CONFIRMATION"


def test_the_handoff_text_names_no_student_and_no_identifier() -> None:
    """It is server-authored and skips the output contract, so its safety is a
    property of the constant rather than of a check that runs over it."""
    for handoff in (handoff_for(REFUSAL),):
        assert handoff is not None
        body = json.dumps([handoff.answer("Arabic"), handoff.answer("English")], ensure_ascii=False)
        assert not any(ch.isdigit() for ch in body), "a digit in a fixed referral is suspect"
        assert "STUDENT_REF" not in body
        assert "REDACTED" not in body
