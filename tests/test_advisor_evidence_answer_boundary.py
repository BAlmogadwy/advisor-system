from __future__ import annotations

import json

import pytest

from core.models import AdvisorConversation, AdvisorMessage, Student
from core.services.advisor_history import load_latest_profile_presentation
from core.services.advisor_presentations import KIND_TIMETABLE, normalise_presentation
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_turn import CREATED, run_advisor_turn
from core.services.llm_remote_privacy import (
    RemoteIdentityMap,
    project_tool_result_for_remote,
)
from core.services.rbac import ROLE_STUDENT

pytestmark = pytest.mark.django_db

SID = 9894201
WEB_PROFILE = "web-evidence-v1"


def _presentation(course_code: str, **extra: object) -> dict[str, object]:
    return {
        "kind": KIND_TIMETABLE,
        "planning_term": "1448-1",
        "mode": "from_scratch",
        "baseline_kind": "EMPTY",
        "alternatives": [
            {
                "planner_options": ["A"],
                "scheduled_courses": 1,
                "target_courses": 1,
                "total_credit_hours": 3,
                "courses": [
                    {
                        "course_code": course_code,
                        "course_name": f"Course {course_code}",
                        "section": "M1",
                        "credits": 3,
                    }
                ],
                "meetings": [
                    {
                        "course_code": course_code,
                        "course_name": f"Course {course_code}",
                        "section": "M1",
                        "day": "SUN",
                        "start": "09:00",
                        "end": "10:15",
                    }
                ],
            }
        ],
        "constraints_satisfied": True,
        **extra,
    }


def _question(conversation: AdvisorConversation, profile: str, content: str) -> AdvisorMessage:
    return AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_STUDENT,
        content=content,
        generation_profile=profile,
        status=AdvisorMessage.STATUS_COMPLETED,
    )


def _answer(
    conversation: AdvisorConversation,
    question: AdvisorMessage,
    *,
    course_code: str,
    status: str,
    **presentation_extra: object,
) -> AdvisorMessage:
    return AdvisorMessage.objects.create(
        conversation=conversation,
        in_reply_to=question,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content=f"Answer for {course_code}",
        presentation=_presentation(course_code, **presentation_extra),
        status=status,
    )


def test_remote_timetable_projection_keeps_safe_registration_identity_and_unscheduled_courses():
    projected = project_tool_result_for_remote(
        "my_timetable",
        {
            "tool": "my_timetable",
            "ok": True,
            "registered_course_count": 4,
            "registered_credit_hours": 12,
            "meetings": [
                {
                    "course_code": "AI331",
                    "section": "M1",
                    "day": "SUN",
                    "start": "09:00",
                    "end": "10:15",
                },
                {
                    "course_code": "BR401",
                    "section": "YF4",
                    "day": "MON",
                    "start": "11:00",
                    "end": "12:15",
                },
            ],
            "registrations": [
                {
                    "course_code": "AI331",
                    "course_name": "Artificial Intelligence",
                    "section": "M1",
                    "credits": 3,
                    "meeting_count": 1,
                    "scheduled": True,
                },
                {
                    "course_code": "DS372",
                    "course_name": "Data Topics",
                    "section": "M2",
                    "credits": 3,
                    "meeting_count": 0,
                    "scheduled": False,
                },
                {
                    "course_code": "BR401",
                    "course_name": "Other Branch Scheduled",
                    "section": "YF4",
                    "credits": 3,
                    "meeting_count": 1,
                    "scheduled": True,
                },
                {
                    "course_code": "BR402",
                    "course_name": "Other Branch Unscheduled",
                    "section": "YM5",
                    "credits": 3,
                    "meeting_count": 0,
                    "scheduled": False,
                },
            ],
            "courses_without_a_time": ["ds372", "BR402", "NOT_REGISTERED"],
        },
        RemoteIdentityMap(),
    )

    assert [row["course_code"] for row in projected["registrations"]] == [
        "AI331",
        "DS372",
    ]
    assert [row["course_code"] for row in projected["meetings"]] == ["AI331"]
    assert projected["courses_without_a_time"] == ["DS372"]
    assert projected["registered_course_count"] == 2
    assert projected["registered_credit_hours"] == 6


def test_latest_profile_presentation_is_settled_same_profile_and_normalised():
    conversation = AdvisorConversation.objects.create(student_id=SID)

    old_question = _question(conversation, WEB_PROFILE, "old web question")
    _answer(
        conversation,
        old_question,
        course_code="OLD101",
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    cross_profile_question = _question(conversation, "telegram-safe-v1", "telegram question")
    _answer(
        conversation,
        cross_profile_question,
        course_code="CROSS999",
        status=AdvisorMessage.STATUS_COMPLETED,
    )

    unsettled_question = _question(conversation, WEB_PROFILE, "failed web question")
    _answer(
        conversation,
        unsettled_question,
        course_code="FAILED999",
        status=AdvisorMessage.STATUS_FAILED,
    )

    latest_question = _question(conversation, WEB_PROFILE, "latest settled web question")
    raw_latest = _presentation(
        "NEW202",
        internal_trace={"student_id": SID, "database_table": "student_courses"},
    )
    AdvisorMessage.objects.create(
        conversation=conversation,
        in_reply_to=latest_question,
        role=AdvisorMessage.ROLE_ASSISTANT,
        content="latest answer",
        presentation=raw_latest,
        status=AdvisorMessage.STATUS_ABSTAINED,
    )

    loaded = load_latest_profile_presentation(
        conversation,
        channel_profile=WEB_PROFILE,
    )

    assert loaded == normalise_presentation(raw_latest)
    assert loaded["alternatives"][0]["courses"][0]["course_code"] == "NEW202"
    assert "internal_trace" not in loaded


def test_grounding_refusal_turn_persists_abstention_and_clears_presentation(monkeypatch):
    Student.objects.create(
        student_id=SID,
        name="Evidence Boundary Student",
        program="AI",
        section="M",
    )
    conversation = AdvisorConversation.objects.create(student_id=SID)
    supplied_presentation = _presentation("AI331")
    assert normalise_presentation(supplied_presentation), "the fixture must be renderable"

    supplied_response = {
        "ok": True,
        "answer": "I could not verify this answer against the available evidence.",
        "model": "boundary-test-model",
        "citations": [],
        "cited_policy_ids": [],
        "presentation": supplied_presentation,
        "agent": {
            "loop_used": True,
            "policy_required": False,
            "policy_grounding": "not_consulted",
            "grounding_refused": True,
        },
    }
    monkeypatch.setattr(
        "core.services.student_advisor_v2.answer_student_advisor",
        lambda **_kwargs: supplied_response,
    )

    turn = run_advisor_turn(
        principal=AdvisorPrincipal(role=ROLE_STUDENT, student_id=SID),
        conversation=conversation,
        question="Build a timetable for me.",
        idempotency_key="grounding-refusal-boundary",
        channel_profile=WEB_PROFILE,
    )

    assert turn.outcome == CREATED
    assert turn.assistant_message is not None
    assistant = AdvisorMessage.objects.get(pk=turn.assistant_message.pk)
    assert assistant.status == AdvisorMessage.STATUS_ABSTAINED
    assert assistant.final_disposition == "ABSTAIN"
    assert assistant.reason_codes == ["OUTPUT_NOT_GROUNDED"]
    assert assistant.presentation == {}


def test_a_prior_card_reaches_a_remote_provider_without_the_academic_record():
    """The follow-up card is projected, not echoed whole.

    graduation_progress's own projector sends a remote provider aggregate
    counts and deliberately withholds scenario_graph. The prior-presentation
    card is built from the UNPROJECTED local result, so re-sending it verbatim
    on every later turn would deliver through the side door the per-course
    passed/studying map the front door refuses.
    """
    from core.services.llm_remote_privacy import project_prior_presentation

    card = {
        "kind": "graduation_scenario",
        "program": "AI",
        "planning_term": "1448/1",
        "graph": {
            "nameOf": {"CS111": "Programming"},
            "termOf": {"CS111": 1},
            "statusOf": {"CS111": "passed", "MATH101": "passed", "AI331": "open"},
            "items": [{"id": "CS111"}],
            "extraNodes": ["GS311"],
        },
    }

    projected = project_prior_presentation(card)
    graph = projected["graph"]

    # What the re-render legitimately needs survives ...
    assert graph["nameOf"] == {"CS111": "Programming"}
    assert graph["termOf"] == {"CS111": 1}
    assert projected["program"] == "AI"
    # ... and the student's academic record does not.
    assert "statusOf" not in graph
    assert "items" not in graph
    assert "extraNodes" not in graph
    assert "passed" not in json.dumps(projected, ensure_ascii=False)
