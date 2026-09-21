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
            "Which courses can I take this term?",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "What classes am I eligible to take this semester?",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "وش المواد اللي أقدر أنزلها هذا الترم؟",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "إيش المقررات اللي أنا مؤهل لها هالفصل؟",
            (),
            SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY,
        ),
        (
            "إذا ثبتنا `DS341-M2`، وش أفضل المواد اللي نضيفها معه؟",
            (PIN,),
            SemanticPolicyId.PINNED_COURSE_ADDITION_BALANCED,
        ),
        (
            "وش لازم أنجح فيه قبل `DS491`؟",
            (),
            SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS,
        ),
        (
            "عطيني سلسلة المتطلبات المرتبطة بـ `DS491`.",
            (),
            SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS,
        ),
        (
            "أبي مادة إضافية بس ما أبي شيء ما له أولوية.",
            (),
            SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK,
        ),
        (
            "فيه مادة مهمة أقدر أنزلها وما هي موجودة بجدولي؟",
            (),
            SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK,
        ),
        (
            "لو هدفنا أسرع تخرج ممكن، وش الجدول الأفضل لي؟",
            (),
            SemanticPolicyId.FASTEST_GRADUATION_TIMETABLE_REVIEW,
        ),
        (
            "هل زيادة مقرر واحد هذا الترم فعلاً تفرق في موعد تخرجي؟",
            (),
            SemanticPolicyId.ONE_COURSE_GRADUATION_IMPACT,
        ),
        (
            "ابنِ لي أفضل جدول ممكن لهذا الترم.",
            (),
            SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION,
        ),
        (
            "سو لي أكثر من خيار جدول وأعطني الأفضل.",
            (),
            SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION,
        ),
        (
            "Build several timetable options and give me the best one.",
            (),
            SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION,
        ),
        (
            "عندي مكان في الجدول، وش المواد اللي أقدر أضيفها؟",
            (),
            SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION,
        ),
        (
            "I have room in my timetable; which courses can I add?",
            (),
            SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION,
        ),
        (
            "هل فيه تبديل بين مقررين يخلي تخرجي أسرع؟",
            (),
            SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP,
        ),
        (
            "Is there a swap between two courses that would make me graduate faster?",
            (),
            SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP,
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
        "What are the prerequisites for DS491?",
        "What do I still need to pass before DS491 and AI331?",
        "I do not want to add a low-priority course.",
        "Build me a fastest-graduation timetable from scratch.",
        "Would dropping one course this term change my graduation date?",
        "Would adding DS341 this term change my graduation date?",
        "Build several 15-credit timetable options and give me the best one.",
        "وش المواد المتاحة لي حالياً حسب خطتي؟",
        "عطيني كل المواد اللي مستوفي متطلباتها.",
        "وش أقدر أنزل من المواد المتبقية لي؟",
        "هل فيه مقرر متاح لي نسيت أسجله؟",
        "من المواد الباقية، وش المفتوح لي حالياً؟",
        "هل يوجد جدول أفضل من جدولي الحالي يقلل فترة تخرجي؟",
        "أقدر أغير جدولي عشان أتخرج أسرع؟",
        "قارن جدولي الحالي بأفضل جدول ممكن للتخرج.",
        "وش أغير في جدولي عشان أقلل عدد الترمات المتبقية؟",
        "هل جدولي الحالي يسبب لي تأخير بدون ما أدري؟",
        "هل فيه مادة لازم أضيفها الآن عشان ما أتأخر مستقبلاً؟",
        "وش أكثر تعديل في جدولي الحالي ممكن يفيد تاريخ تخرجي؟",
        "هل أقدر أوفر ترم كامل إذا غيرت بعض المقررات؟",
        "لو حذفت DS341 وأضفت IS362، هل وضعي يصير أفضل؟",
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


@pytest.mark.parametrize(
    "question",
    [
        '"What do I still need to pass before DS491?"',
        "“I want one extra course, but I do not want anything with no academic priority.”",
        "‘If our goal is the fastest possible graduation, what is the best timetable for me?’",
        "«هل زيادة مقرر واحد هذا الترم فعلاً تفرق في موعد تخرجي؟»",
        "'Build several timetable options and give me the best one.'",
        '"What do I still need to pass before DS491?"?',
        '("Build several timetable options and give me the best one.")',
        "«هل زيادة مقرر واحد هذا الترم فعلاً تفرق في موعد تخرجي؟» .",
        '> "If our goal is the fastest possible graduation, what is the best timetable for me?"',
        "`I want one extra course, but I do not want anything with no academic priority.`",
        '>> "What do I still need to pass before DS491?"',
        '* "Build several timetable options and give me the best one."',
        '["I want one extra course, but I do not want anything with no academic priority."]',
        '{"Would adding one course this term actually change my graduation date?"}',
        '(("If our goal is the fastest possible graduation, what is the best timetable for me?"))',
        ">>> «سو لي أكثر من خيار جدول وأعطني الأفضل.»",
    ],
)
def test_whole_utterance_quotes_never_activate_semantic_policy(question: str) -> None:
    assert active_semantic_policy_ids(question, explicit_pins=()) == ()


def test_inline_course_code_quotes_do_not_disable_a_real_policy_request() -> None:
    assert active_semantic_policy_ids(
        "What do I still need to pass before `DS491`?",
        explicit_pins=(),
    ) == (SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS,)


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
    "question",
    [
        '"Which courses can I take this term?"',
        "Last term I asked which courses can I take this term?",
        "I am not asking which courses can I take this term.",
        "Which courses can I take this term that best fit my timetable?",
        "I have room in my timetable; which courses can I add?",
        "«وش المواد اللي أقدر أنزلها هذا الترم؟»",
        "الترم الماضي سألت وش المواد اللي أقدر أنزلها هذا الترم.",
        "مو قاعد أسأل وش المواد اللي أقدر أنزلها هذا الترم.",
        "وش المواد اللي أقدر أضيفها هذا الترم عشان أتخرج أسرع؟",
        "عندي مكان في الجدول، وش المواد اللي أقدر أضيفها؟",
    ],
)
def test_present_term_available_policy_resists_inactive_and_addition_context(
    question: str,
) -> None:
    assert SemanticPolicyId.PLAIN_AVAILABLE_COURSES_ONLY not in active_semantic_policy_ids(
        question,
        explicit_pins=(),
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
        (
            "What do I still need to pass before DS491?",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["prerequisite_information"],
                "evidence_requests": [
                    {"capability": "why_course_locked", "arguments": {"course_code": "DS491"}}
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["prerequisite_information"],
                "evidence_requests": [
                    {
                        "capability": "course_prerequisites",
                        "arguments": {"course_code": "DS491"},
                    }
                ],
            },
            SemanticPolicyId.PERSONALIZED_PREREQUISITE_ANALYSIS,
        ),
        (
            "I want one extra course, but I do not want anything with no academic priority.",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["course_addition"],
                "evidence_requests": [
                    {
                        "capability": "recommend_feasible_course_addition",
                        "arguments": {"objective": "unlock_impact"},
                    }
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["course_priority"],
                "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
            },
            SemanticPolicyId.PRIORITY_COURSE_ADDITION_UNLOCK,
        ),
        (
            "If our goal is the fastest possible graduation, what is the best timetable for me?",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["timetable_review"],
                "evidence_requests": [
                    {
                        "capability": "improve_current_timetable",
                        "arguments": {
                            "objective": "faster_graduation",
                            "credit_load_policy": "preserve",
                            "allow_course_replacements": True,
                        },
                    }
                ],
            },
            {
                "decision": "clarify",
                "clarification_kind": "timetable_preference",
                "requested_outcomes": ["timetable_build"],
                "evidence_requests": [],
            },
            SemanticPolicyId.FASTEST_GRADUATION_TIMETABLE_REVIEW,
        ),
        (
            "Would adding one course this term actually change my graduation date?",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["graduation_impact"],
                "evidence_requests": [
                    {
                        "capability": "recommend_feasible_course_addition",
                        "arguments": {"objective": "faster_graduation"},
                    }
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["graduation_forecast"],
                "evidence_requests": [{"capability": "graduation_progress", "arguments": {}}],
            },
            SemanticPolicyId.ONE_COURSE_GRADUATION_IMPACT,
        ),
        (
            "Build several timetable options and give me the best one.",
            (),
            {
                "decision": "clarify",
                "clarification_kind": "timetable_preference",
                "requested_outcomes": ["timetable_build"],
                "evidence_requests": [],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["timetable_build"],
                "evidence_requests": [{"capability": "build_timetable_proposal", "arguments": {}}],
            },
            SemanticPolicyId.BEST_TIMETABLE_PREFERENCE_CLARIFICATION,
        ),
        (
            "عندي مكان في الجدول، وش المواد اللي أقدر أضيفها؟",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["course_addition"],
                "evidence_requests": [
                    {
                        "capability": "recommend_feasible_course_addition",
                        "arguments": {"objective": "timetable_fit"},
                    }
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["available_courses"],
                "evidence_requests": [{"capability": "my_progress", "arguments": {}}],
            },
            SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION,
        ),
        (
            "هل فيه تبديل بين مقررين يخلي تخرجي أسرع؟",
            (),
            {
                "decision": "execute",
                "requested_outcomes": ["course_replacement"],
                "evidence_requests": [
                    {
                        "capability": "graduation_progress",
                        "arguments": {
                            "planning_baseline_kind": "recommended_current_term",
                            "search_better_replacements": True,
                        },
                    }
                ],
            },
            {
                "decision": "execute",
                "requested_outcomes": ["course_replacement", "graduation_impact"],
                "evidence_requests": [
                    {
                        "capability": "graduation_progress",
                        "arguments": {
                            "planning_baseline_kind": "recommended_current_term",
                            "search_better_replacements": True,
                        },
                    }
                ],
            },
            SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP,
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


@pytest.mark.parametrize(
    ("question", "pins", "policy", "outcomes", "calls"),
    [
        (
            "Which course is delaying me the most in my degree plan?",
            (),
            SemanticPolicyId.MOST_DELAYING_COURSE_PRIORITY,
            ["course_priority"],
            [("my_progress", {})],
        ),
        (
            "If I cannot take all the courses, what is the most important thing to register?",
            (),
            SemanticPolicyId.REGISTRATION_SHORTFALL_COURSE_PRIORITY,
            ["course_priority"],
            [("my_progress", {})],
        ),
        (
            "If I drop DS332 will my graduation be delayed?",
            (),
            SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
            ["graduation_impact"],
            [
                (
                    "rank_current_course_drop_impact",
                    {"objective": "least_graduation_delay", "course_codes": ["DS332"]},
                )
            ],
        ),
        (
            "Which is better to drop DS341 or DS321?",
            (),
            SemanticPolicyId.BALANCED_NAMED_DROP_IMPACT,
            ["course_drop_impact"],
            [
                (
                    "rank_current_course_drop_impact",
                    {"objective": "balanced", "course_codes": ["DS341", "DS321"]},
                )
            ],
        ),
        (
            "Will dropping DS332 block courses for me next term?",
            (),
            SemanticPolicyId.SINGLE_DROP_PREREQUISITE_CONTINUITY,
            ["course_drop_impact"],
            [
                (
                    "rank_current_course_drop_impact",
                    {"objective": "prerequisite_continuity", "course_codes": ["DS332"]},
                )
            ],
        ),
        (
            "My current timetable has DS332 and DS341 and DS321. If I must drop one course, choose the course with the least impact on my graduation date and explain why.",
            (),
            SemanticPolicyId.LEAST_DELAY_NAMED_DROP_SELECTION,
            ["course_drop_impact"],
            [
                (
                    "rank_current_course_drop_impact",
                    {
                        "objective": "least_graduation_delay",
                        "course_codes": ["DS332", "DS341", "DS321"],
                    },
                )
            ],
        ),
        (
            "I want DS341 section M2 included in every option.",
            (PIN,),
            SemanticPolicyId.PINNED_SECTION_EVERY_OPTION_BUILD,
            ["timetable_build"],
            [
                (
                    "build_timetable_proposal",
                    {
                        "mode": "from_scratch",
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [PIN],
                    },
                )
            ],
        ),
        (
            "Build me a new timetable from scratch with a maximum of 18 credits, pin DS341-M2, and prioritize courses that prevent graduation delay.",
            (PIN,),
            SemanticPolicyId.FRESH_PINNED_GRADUATION_PRIORITY_BUILD,
            ["timetable_build", "course_priority"],
            [
                (
                    "build_timetable_proposal",
                    {
                        "mode": "from_scratch",
                        "max_credits": 18,
                        "must_take_courses": ["DS341"],
                        "pinned_sections": [PIN],
                    },
                ),
                ("my_progress", {}),
            ],
        ),
        (
            "I have room in my timetable; which courses can I add?",
            (),
            SemanticPolicyId.TIMETABLE_SPACE_COURSE_ADDITION,
            ["course_addition"],
            [("recommend_feasible_course_addition", {"objective": "timetable_fit"})],
        ),
        (
            "Is there a swap between two courses that would make me graduate faster?",
            (),
            SemanticPolicyId.GRADUATION_IMPROVING_COURSE_SWAP,
            ["course_replacement"],
            [
                (
                    "graduation_progress",
                    {
                        "planning_baseline_kind": "recommended_current_term",
                        "search_better_replacements": True,
                    },
                )
            ],
        ),
    ],
)
def test_v20_closed_policy_exact_plan_and_extra_argument_rejected(
    question: str,
    pins: tuple[dict[str, str], ...],
    policy: SemanticPolicyId,
    outcomes: list[str],
    calls: list[tuple[str, dict]],
) -> None:
    assert active_semantic_policy_ids(question, explicit_pins=pins) == (policy,)
    requests = [{"capability": name, "arguments": args} for name, args in calls]
    plan = {"decision": "execute", "requested_outcomes": outcomes, "evidence_requests": requests}
    assert semantic_policy_violations(question, plan, explicit_pins=pins) == ()
    requests[0]["arguments"] = {**requests[0]["arguments"], "max_credits": 99}
    assert semantic_policy_violations(question, plan, explicit_pins=pins) == (policy,)


@pytest.mark.parametrize("wrapper", ['"{}"', '(("{}"))', ">>> «{}»", "```{}```"])
def test_v20_exact_families_ignore_full_utterance_examples(wrapper: str) -> None:
    question = wrapper.format("Which course is delaying me the most in my degree plan?")
    assert active_semantic_policy_ids(question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "Which is better to drop DS341 or DS341?",
        "أيهم أفضل أحذف DS341 أو DS341؟",
        "What happens if I withdraw from DS332 DS332?",
        "وش بيصير لو انسحبت من DS332 DS332؟",
        "If I drop DS332 DS332 will my graduation be delayed?",
        "لو حذفت DS332 DS332 هل يتأخر تخرجي؟",
        "My current timetable has DS332 and DS332 and DS321. If I must drop one course, choose the course with the least impact on my graduation date and explain why.",
        "جدولي الحالي فيه DS332 وDS332 وDS321. إذا اضطررت أحذف مقرر واحد، اختر المقرر الأقل تأثيراً على موعد تخرجي ووضح لي ليه.",
    ],
)
def test_v20_drop_families_reject_duplicate_code_cardinality(question: str) -> None:
    assert active_semantic_policy_ids(question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "```text\nI have room for one course; what should I choose?\n```",
        "~~~ar\nعندي مجال لمادة وحدة بس وش أختار\n~~~",
        "> ```\n> If I drop DS332 will my graduation be delayed?\n> ```",
        "> ~~~text\n> لو حذفت DS332 هل يتأخر تخرجي؟\n> ~~~",
    ],
)
def test_closed_policies_ignore_complete_multiline_fenced_examples(question: str) -> None:
    assert active_semantic_policy_ids(question) == ()


def test_v20_real_request_with_inline_course_code_still_activates() -> None:
    assert active_semantic_policy_ids("If I drop `DS332` will my graduation be delayed?") == (
        SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
    )


@pytest.mark.parametrize(
    "question",
    [
        "> I have room for one course; what should I choose?",
        "> وش بيصير لو انسحبت من DS332؟",
        "    Which course is delaying me the most in my degree plan?",
        "\tلو حذفت DS332 هل يتأخر تخرجي؟",
        "~~Which is better to drop DS341 or DS321?~~",
        r"\"If I drop DS332 will my graduation be delayed?\"",
        "＂Which course is delaying me the most in my degree plan?＂",
        "「عندي مجال لمادة وحدة بس وش أختار؟」",
        "《If I drop DS332 will my graduation be delayed?》",
        "[Which course is delaying me the most in my degree plan?]()",
        "![لو حذفت DS332 هل يتأخر تخرجي؟]()",
        '"عندي مكان في الجدول، وش المواد اللي أقدر أضيفها؟"',
        "«هل فيه تبديل بين مقررين يخلي تخرجي أسرع؟»",
    ],
)
def test_closed_policies_ignore_additional_whole_utterance_wrappers(question: str) -> None:
    assert active_semantic_policy_ids(question) == ()


def test_inline_emphasis_does_not_disable_a_real_request() -> None:
    assert active_semantic_policy_ids("If I drop **DS332** will my graduation be delayed?") == (
        SemanticPolicyId.SINGLE_DROP_GRADUATION_DELAY,
    )


@pytest.mark.parametrize(
    "question",
    [
        '# "Which course is delaying me the most in my degree plan?"',
        '*"If I drop DS332 will my graduation be delayed?"*',
        '***"عندي مجال لمادة وحدة بس وش أختار؟"***',
        '(((*"وش بيصير لو انسحبت من DS332؟"*)))',
        "\\“Which course is delaying me the most in my degree plan?\\”",
        '\u200b"If I drop DS332 will my graduation be delayed?"\ufeff',
        '#\u2060 \u202a"لو حذفت DS332 هل يتأخر تخرجي؟"\u202c',
        '*\u200b"Which is better to drop DS341 or DS321?"\u2060*',
        '\x00"عندي مجال لمادة وحدة بس وش أختار؟"\x00',
    ],
)
def test_closed_policies_ignore_heading_emphasis_and_control_quote_wrappers(
    question: str,
) -> None:
    assert active_semantic_policy_ids(question) == ()


@pytest.mark.parametrize(
    "question",
    [
        "Build me a new timetable from scratch with a maximum of 0 credits, pin DS341-M2, and prioritize courses that prevent graduation delay.",
        "Build me a new timetable from scratch with a maximum of 100 credits, pin DS341-M2, and prioritize courses that prevent graduation delay.",
        "ابن لي جدول جديد من الصفر بحد أقصى 0 ساعة ثبت فيه DS341-M2 وأعط الأولوية للمقررات اللي تمنع تأخر التخرج",
        "ابن لي جدول جديد من الصفر بحد أقصى 100 ساعة ثبت فيه DS341-M2 وأعط الأولوية للمقررات اللي تمنع تأخر التخرج",
        "If I drop ABCDEFG332 will my graduation be delayed?",
        "If I drop DS332A will my graduation be delayed?",
        "لو حذفت ABCDEFG332 هل يتأخر تخرجي؟",
        "لو حذفت DS332A هل يتأخر تخرجي؟",
    ],
)
def test_v20_policy_literal_boundaries_decline_unreachable_values(question: str) -> None:
    pins = ({"course_code": "DS341", "section_label": "M2"},) if "DS341" in question else ()
    assert active_semantic_policy_ids(question, explicit_pins=pins) == ()
