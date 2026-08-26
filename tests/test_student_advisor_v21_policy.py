from __future__ import annotations

import pytest

from core.services.student_advisor_v21_policy import (
    SemanticPolicyId,
    active_semantic_policy_ids,
    semantic_policy_violations,
)

PIN = {"course_code": "DS341", "section_label": "M2"}


@pytest.mark.parametrize(
    ("question", "explicit_pins", "expected"),
    [
        (
            "هل فيه متطلب متزامن مع `DS491`؟",
            (),
            SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED,
        ),
        (
            "عندي مجال لمادة وحدة بس، وش أختار؟",
            (),
            SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED,
        ),
        (
            "I have room for one course; what should I choose?",
            (),
            SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED,
        ),
        (
            "وش المواد اللي أنا مؤهل لها بس مو موجودة في جدولي؟",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "Which courses am I eligible for but are not in my timetable?",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "وش المواد اللي أنا مؤهل لها بس مو موجودة في جدولي؟ ما أبي ترتيب.",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "إذا ثبتنا `DS341-M2`، وش أفضل المواد اللي نضيفها معه؟",
            (PIN,),
            SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED,
        ),
    ],
)
def test_closed_semantic_policy_families_activate_only_from_trusted_context(
    question: str,
    explicit_pins: tuple[dict[str, str], ...],
    expected: SemanticPolicyId,
) -> None:
    assert active_semantic_policy_ids(
        question,
        explicit_pins=explicit_pins,
    ) == (expected,)


@pytest.mark.parametrize(
    "question",
    [
        "What does corequisite mean?",
        'The handbook quotes "corequisite"; what are the prerequisites for DS491?',
        "Last term I asked about a corequisite; now list the prerequisites for DS491.",
        "I am not asking about corequisites; what are the prerequisites for DS491?",
        "What are the prerequisite and corequisite requirements for DS491?",
        "هل لـ DS491 متطلب متزامن وما متطلباته السابقة؟",
        "Does DS491 have a corequisite, and what are its ordinary prerequisites?",
        "ما معنى «متطلب متزامن»؟",
        "If I fail one course, what happens?",
        "I failed one course; which should I retake?",
        "لازم أحذف مادة وحدة، وش أختار؟",
        "I must drop one course. Which should I choose?",
        "I am one course short of graduating; how many terms remain?",
        "I need one course section; which should I choose?",
        "Should I take DS341?",
        "I have room for one course; which best fits my timetable?",
        "Compare one course with another and tell me which to drop.",
        "وش أفضل المواد المتاحة لي للتسجيل الحين؟",
        "فيه مقررات مهمة أقدر أسجلها وما نزلتها؟",
        "من المواد المتاحة لي، أي وحدة أهم أضيفها؟",
        "ابنِ لي أفضل جدول ممكن لهذا الترم.",
    ],
)
def test_adjacent_or_inactive_requests_do_not_activate_a_closed_policy(
    question: str,
) -> None:
    assert active_semantic_policy_ids(question, explicit_pins=()) == ()


@pytest.mark.parametrize(
    ("question", "pins"),
    [
        ("Do not pin DS341-M2; which courses are best to add?", ()),
        ("Pin M2 and tell me the best courses to add.", ()),
        ("Pin DS341-M2 and build the best timetable around it.", (PIN,)),
        ("Pin DS341-M2, but do not pin it; what are the best courses to add?", ()),
    ],
)
def test_pinned_addition_policy_requires_final_active_exact_pins_and_not_a_build(
    question: str,
    pins: tuple[dict[str, str], ...],
) -> None:
    assert active_semantic_policy_ids(question, explicit_pins=pins) == ()


def test_english_available_no_ranking_still_activates_available_only() -> None:
    question = "Which courses am I eligible for but aren't in my timetable, without ranking?"
    assert active_semantic_policy_ids(question, explicit_pins=()) == (
        SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
    )


def test_english_available_contracted_no_ranking_still_activates_available_only() -> None:
    question = "Which courses am I eligible for but are not in my timetable? Don't rank them."
    assert active_semantic_policy_ids(question, explicit_pins=()) == (
        SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
    )


@pytest.mark.parametrize(
    ("question", "pins", "correct_plan", "wrong_plan", "policy"),
    [
        (
            "هل فيه متطلب متزامن مع DS491؟",
            (),
            {
                "decision": "unsupported",
                "requested_outcomes": ["unsupported_request"],
                "evidence_requests": [],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["prerequisite_information"],
                "evidence_requests": [
                    {"capability": "course_prerequisites", "arguments": {"course_code": "DS491"}}
                ],
            },
            SemanticPolicyId.STANDALONE_COREQUISITE_UNSUPPORTED,
        ),
        (
            "عندي مجال لمادة وحدة بس، وش أختار؟",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["course_addition"],
                "evidence_requests": [
                    {
                        "capability": "recommend_feasible_course_addition",
                        "arguments": {"objective": "balanced"},
                    }
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["course_priority"],
                "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
            },
            SemanticPolicyId.SINGLE_COURSE_CHOICE_BALANCED,
        ),
        (
            "وش المواد اللي أنا مؤهل لها بس مو موجودة في جدولي؟",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["available_courses"],
                "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["available_courses", "course_priority"],
                "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
            },
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "إذا ثبتنا DS341-M2، وش أفضل المواد اللي نضيفها معه؟",
            (PIN,),
            {
                "decision": "execute",
                "requested_outcomes": ["course_addition"],
                "evidence_requests": [
                    {
                        "capability": "recommend_feasible_course_addition",
                        "arguments": {"objective": "balanced", "pinned_sections": [PIN]},
                    }
                ],
            },
            {
                "decision": "clarify",
                "requested_outcomes": ["timetable_build"],
                "evidence_requests": [],
            },
            SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED,
        ),
    ],
)
def test_closed_policy_validator_compares_without_rewriting(
    question: str,
    pins: tuple[dict[str, str], ...],
    correct_plan: dict,
    wrong_plan: dict,
    policy: SemanticPolicyId,
) -> None:
    assert (
        semantic_policy_violations(
            question,
            correct_plan,
            explicit_pins=pins,
        )
        == ()
    )
    assert semantic_policy_violations(
        question,
        wrong_plan,
        explicit_pins=pins,
    ) == (policy,)
