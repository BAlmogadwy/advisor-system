"""The adviser remembers what the student already saw, and nothing else.

Without this the interface only looks conversational: every turn was generated
with no knowledge of the previous ones, so «احتفظ بها» had no referent. These
tests are as much about what is EXCLUDED as what is passed — history is model
context, and everything in it is something the model may repeat.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.urls import reverse

from core.models import AdvisorConversation, AdvisorMessage, Student
from core.services.advisor_history import MAX_HISTORY_MESSAGES, load_visible_history
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

MINE = 9701001
THEIRS = 9701002


def _student(client, student_id: int = MINE):
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": "S", "program": "CS", "section": "M"},
    )
    client.force_login(student_otp.provision_student_user(student_id))


def _turn(conversation, question: str, answer: str | None, *, status=None):
    asked = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=question,
        status=status or AdvisorMessage.STATUS_COMPLETED,
    )
    if answer is None:
        return asked, None
    replied = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content=answer,
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    return asked, replied


def _answer(text="جواب.", **agent):
    return {
        "ok": True,
        "answer": text,
        "model": "stub",
        "citations": [],
        "cited_policy_ids": [],
        "agent": {"loop_used": True, "policy_grounding": "not_consulted", **agent},
    }


# ── 1-4. the pronoun has something to refer to ───────────────────


def test_a_follow_up_reaches_the_model_with_the_question_it_answers(client):
    """«احتفظ بها» is meaningless without the turn that asked."""
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(
        conversation,
        "أبي أسجل CS113",
        "هل ترغب في الاحتفاظ بشعبك الحالية وإضافة المقرر، أو إعادة بناء الجدول بالكامل؟",
    )

    seen = {}
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=lambda **kw: seen.update(kw) or _answer(),
    ):
        client.post(
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            data=json.dumps({"message": "احتفظ بها"}),
            content_type="application/json",
        )

    history = seen["history"]
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["content"] == "أبي أسجل CS113"
    assert "إعادة بناء الجدول" in history[1]["content"]
    # The question being asked now is the QUESTION, never also the history.
    assert all("احتفظ بها" not in h["content"] for h in history)


def test_the_current_question_is_not_duplicated_into_its_own_history(client):
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    seen = {}
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=lambda **kw: seen.update(kw) or _answer(),
    ):
        client.post(
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            data=json.dumps({"message": "سؤالي الأول"}),
            content_type="application/json",
        )
    assert seen["history"] == []


def test_a_retried_turn_does_not_appear_twice(client):
    """A resume reuses the SAME student row, which is already in the database.

    Excluding only "the newest message" would let the retry see its own question as
    history, and the model would answer as though it had already been asked.
    """
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "سؤال سابق", "جواب سابق")
    url = reverse("advisor_conversation_send", args=[str(conversation.id)])
    body = {"message": "سؤالي الجديد", "idempotency_key": "k1"}

    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=RuntimeError("model down"),
    ):
        client.post(url, data=json.dumps(body), content_type="application/json")

    seen = {}
    with mock.patch(
        "core.services.virtual_advisor.answer_virtual_advisor",
        side_effect=lambda **kw: seen.update(kw) or _answer(),
    ):
        client.post(url, data=json.dumps(body), content_type="application/json")

    contents = [h["content"] for h in seen["history"]]
    assert contents.count("سؤالي الجديد") == 0, contents
    assert contents == ["سؤال سابق", "جواب سابق"]


def test_the_exclusion_holds_even_for_a_settled_message():
    """The status filter hides the current question today, because it is PENDING
    while being generated. That makes the exclusion look redundant — until some
    future change marks the row COMPLETED before generation, at which point the
    model receives the question twice. Tested against the function's own contract
    rather than against today's status flow.
    """
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    asked, _ = _turn(conversation, "سؤال", "جواب")
    current = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="السؤال الحالي",
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    without = [h["content"] for h in load_visible_history(conversation)]
    assert "السؤال الحالي" in without, "the fixture did not reproduce the hazard"

    excluded = [
        h["content"] for h in load_visible_history(conversation, exclude_message_id=current.pk)
    ]
    assert "السؤال الحالي" not in excluded
    assert excluded == ["سؤال", "جواب"]


def test_every_message_claims_a_position_in_the_conversation():
    """`created_at` cannot order a thread: two messages from one request land in the
    same microsecond on a coarse clock, and the primary key is a random UUID — the
    tiebreak reversed question and answer. `sequence` is the only field guaranteed
    to increase."""
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    asked, replied = _turn(conversation, "سؤال", "جواب")

    asked.refresh_from_db()
    replied.refresh_from_db()
    assert asked.sequence >= 1
    assert replied.sequence == asked.sequence + 1

    # And the ordering it exists to guarantee. The collision is FORCED rather than
    # hoped for: whether two writes in one request land in the same microsecond
    # depends on the host clock, so asserting that it happened would be a test that
    # passes on Windows and flakes elsewhere.
    AdvisorMessage.objects.filter(conversation=conversation).update(created_at=asked.created_at)
    assert list(conversation.messages.values_list("role", flat=True)) == [
        AdvisorMessage.ROLE_STUDENT,
        AdvisorMessage.ROLE_ASSISTANT,
    ]


def test_the_default_ordering_declares_sequence_first():
    """Asserted on the emitted SQL, because the effect is invisible on SQLite.

    With equal `created_at` values SQLite falls back to rowid, which happens to be
    insertion order — so a Meta.ordering that dropped `sequence` would still look
    correct here and would be undefined on PostgreSQL, where row order with equal
    sort keys depends on the plan.
    """
    sql = str(AdvisorMessage.objects.all().query).upper()
    order_by = sql.split("ORDER BY", 1)[1]
    assert "SEQUENCE" in order_by, sql
    assert order_by.index("SEQUENCE") < order_by.index("CREATED_AT"), order_by


def test_a_second_turn_continues_the_numbering():
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "س1", "ج1")
    _turn(conversation, "س2", "ج2")
    assert list(conversation.messages.values_list("sequence", flat=True)) == [1, 2, 3, 4]


# ── 6. one conversation, one student ─────────────────────────────


def test_another_conversation_of_the_same_student_is_not_included(client):
    _student(client)
    other = AdvisorConversation.objects.create(student_id=MINE)
    _turn(other, "سؤال في محادثة أخرى", "جواب في محادثة أخرى")

    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "سؤال هنا", "جواب هنا")

    history = load_visible_history(conversation)
    assert [h["content"] for h in history] == ["سؤال هنا", "جواب هنا"]


def test_another_students_conversation_is_not_reachable_at_all(client):
    theirs = AdvisorConversation.objects.create(student_id=THEIRS)
    _turn(theirs, "سؤال طالب آخر", "جواب طالب آخر")

    mine = AdvisorConversation.objects.create(student_id=MINE)
    assert load_visible_history(mine) == []


# ── 7. history is model context, so it carries nothing internal ──


def test_only_the_two_visible_roles_are_passed(client):
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "سؤال", "جواب")
    for turn in load_visible_history(conversation):
        assert set(turn) == {"role", "content"}
        assert turn["role"] in {"user", "assistant"}


def test_policy_ids_are_stripped_from_remembered_answers(client):
    """A stored answer carries the machine half of its citation.

    Passed back verbatim, the model copies an id it did not retrieve THIS turn,
    `validate_citations` rejects it as NOT_RETRIEVED_THIS_REQUEST, and a correct
    follow-up becomes a citation refusal.
    """
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(
        conversation,
        "كم مرة أنسحب؟",
        "خمسة «الدليل الإرشادي للطالب والطالبة، ص 24 [TU.WITHDRAWAL.MAXIMUM]».",
    )
    remembered = load_visible_history(conversation)[1]["content"]

    assert "TU.WITHDRAWAL.MAXIMUM" not in remembered
    assert "[" not in remembered
    # The human-readable reference survives — that is what the sentence means.
    assert "الدليل الإرشادي" in remembered and "ص 24" in remembered


def test_a_grade_symbol_is_not_mistaken_for_a_policy_id():
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "س", "سيُرصد تقدير [W] ومعدلك [GPA] لا يتأثر.")
    remembered = load_visible_history(conversation)[1]["content"]
    assert "[W]" in remembered and "[GPA]" in remembered


# ── which turns count as "seen" ──────────────────────────────────


def test_a_question_that_was_never_answered_is_not_remembered(client):
    """Feeding back an unanswered question invites the model to believe it replied."""
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    _turn(conversation, "سؤال فشل", None, status=AdvisorMessage.STATUS_FAILED)
    _turn(conversation, "سؤال معلّق", None, status=AdvisorMessage.STATUS_PENDING)
    _turn(conversation, "سؤال ناجح", "جواب ناجح")

    assert [h["content"] for h in load_visible_history(conversation)] == [
        "سؤال ناجح",
        "جواب ناجح",
    ]


def test_an_abstention_is_remembered_because_the_student_saw_it(client):
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    asked = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content="هل أنسحب؟",
        status=AdvisorMessage.STATUS_COMPLETED,
    )
    AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content="لا يمكن للنظام البت في حالتك.",
        status=AdvisorMessage.STATUS_ABSTAINED,
    )
    assert "لا يمكن للنظام البت" in load_visible_history(conversation)[1]["content"]


def test_the_window_keeps_the_most_recent_turns_in_order(client):
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    for i in range(10):
        _turn(conversation, f"سؤال {i}", f"جواب {i}")

    history = load_visible_history(conversation)
    assert len(history) == MAX_HISTORY_MESSAGES
    # The newest, and still chronological — a history ending mid-exchange reads as
    # if the adviser ignored the last thing said.
    assert history[-1]["content"] == "جواب 9"
    assert history[0]["content"] == "سؤال 6"


# ── 4-5. the builder default ─────────────────────────────────────


def test_the_builder_keeps_current_sections_unless_told_otherwise():
    """ "أبي أسجل CS113" adds a course; it does not rebuild the week.

    Re-picking is the destructive reading of an ambiguous request, so it must be
    the one that has to be asked for.
    """
    from core.services.virtual_advisor_capabilities import _exec_build_my_timetable

    seen = {}

    def fake_build_plans(**kwargs):
        seen.update(kwargs)
        return {"options": []}

    Student.objects.get_or_create(
        student_id=MINE, defaults={"name": "S", "program": "CS", "section": "M"}
    )
    with (
        mock.patch("core.services.planner_builder.build_plans", fake_build_plans),
        mock.patch("core.services.recommender.recommend_next_courses", return_value=["CS113"]),
    ):
        _exec_build_my_timetable(
            {}, {"role": "STUDENT", "student_id": MINE}, {"academic_year": 1448, "term": 1}
        )
    assert seen["keep_registered"] is True


def test_replacing_the_whole_timetable_is_no_longer_possible_from_chat():
    """This test used to assert the opposite, and was right to at the time.

    The contract then was "keeping is the default, replacing must be asked for",
    and this proved asking worked. A live model then asked — off the single Arabic
    word «أكد», with no prior turn saying what was being confirmed — and the
    capability rebuilt. The requirement to confirm first lived in the tool's JSON
    description, which is an instruction to the model, not a gate.

    So the contract changed rather than the code being patched around: chat cannot
    authorise this at all. The planner draft path owns it, with a hashed one-use
    token bound to student + draft + version. See tests/test_advisor_rebuild_gate.py.
    """
    from core.services.virtual_advisor_capabilities import (
        REBUILD_REQUIRES_PLANNER_CONFIRMATION,
        _exec_build_my_timetable,
    )

    seen = {}

    def fake_build_plans(**kwargs):
        seen.update(kwargs)
        return {"options": []}

    Student.objects.get_or_create(
        student_id=MINE, defaults={"name": "S", "program": "CS", "section": "M"}
    )
    with (
        mock.patch("core.services.planner_builder.build_plans", fake_build_plans),
        mock.patch("core.services.recommender.recommend_next_courses", return_value=["CS113"]),
    ):
        out = _exec_build_my_timetable(
            {"keep_current_sections": False},
            {"role": "STUDENT", "student_id": MINE},
            {"academic_year": 1448, "term": 1},
        )

    assert seen == {}, "the builder ran for a rebuild chat cannot authorise"
    assert out["ok"] is False
    assert out["reason"] == REBUILD_REQUIRES_PLANNER_CONFIRMATION
    assert out["action"] == "OPEN_STUDENT_PLANNER"


def test_the_tool_no_longer_offers_the_model_a_way_to_replace():
    """Also inverted, and for the same reason.

    It used to assert the parameter's description said "confirmed" and "ask first".
    Those words were doing the work of an access control. The parameter is gone
    from the schema, so a compliant model cannot name it — and the executor refuses
    it regardless, because compliance is not something to depend on.
    """
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import get_default_registry

    schemas = get_default_registry().tool_schemas_for_scope({"role": ROLE_STUDENT})
    schema = next(s for s in schemas if s.get("function", s).get("name") == "build_my_timetable")
    fn = schema.get("function", schema)
    # The parameter is back as an INTENT SIGNAL — see
    # `test_advisor_rebuild_gate.test_the_parameter_is_an_intent_signal_that_can_never_rebuild`.
    # What must never return is a description that reads as permission.
    properties = fn["parameters"]["properties"]
    assert "keep_current_sections" in properties
    description = properties["keep_current_sections"]["description"].lower()
    assert "after the student has confirmed" not in description, "the instruction is back"
    assert "never changes a registration" in description

    # And the model is told where the decision does live, so it does not invent a
    # substitute — re-calling with must_include and the current courses left out.
    described = fn["description"].lower()
    assert "planner" in described
    assert "not available here at all" not in described, "the denial wording is back"
