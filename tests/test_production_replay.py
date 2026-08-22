"""The audited production fabrications, replayed against the current checker.

Every answer below is reconstructed from a REAL production answer that shipped
to a student and was recorded PASS (audit of 2026-08-22, 59 answers, 8 carrying
catalogue-invented tokens, plus the Telegram relational fabrication).  The gate
is two-sided: each fabrication must flag, and the accompanying true answer
about the same evidence must not - a checker that buys recall by refusing true
answers is the failure mode the design principle forbids.

The invented-course-NAME class (student 4552135, names-not-codes follow-up) is
deliberately absent: its verifier is the claim layer's name-binding check,
which ships behind the shadow flag in a later PR.  This file must grow that
case when it does.
"""

from __future__ import annotations

import pytest

from core.services.answer_consistency import (
    UNSUPPORTED_ACADEMIC_FACT,
    check_answer,
)

pytestmark = pytest.mark.django_db

#: The real catalogue slice around the fabrications.  MATH243 exists; MATE243
#: never did.  DS221 exists; DS225, CS202, CS212, DS351/361/371, DS101, DS201
#: never did (488-course catalogue, verified against production).
CATALOGUE = frozenset(
    {
        "MATH243",
        "AI1",
        "AI331",
        "AI433",
        "CS323",
        "CS424",
        "GS104",
        "MGT405",
        "DS221",
        "DS331",
        "IS252",
        "STAT301",
    }
)


def _clean(answer: str, *, question: str, tools=()) -> list[str]:
    return check_answer(
        answer,
        tool_results=list(tools),
        question=question,
        required_tools=set(),
        known_course_codes=CATALOGUE,
    )


def test_mate243_with_an_invented_section_is_flagged():
    """Student 4502529 was told MATE243/M1 existed; the course is MATH243."""
    violations = _clean(
        "يمكنك تسجيل مقرر **(MATE243)** في الشعبة **M1** هذا الفصل.",
        question="هل أقدر أسجل مادة الرياضيات؟",
    )
    assert UNSUPPORTED_ACADEMIC_FACT in violations

    # Correcting the student's own typo is the TRUE answer and must pass.
    assert (
        _clean(
            "لا يوجد مقرر برمز MATE243 في النظام؛ الرمز الصحيح هو MATH243.",
            question="متى محاضرة MATE243؟",
        )
        == []
    )


def test_nonexistent_cs202_cs212_are_flagged():
    """Student 4610352 received **CS202** and **CS212**; neither exists."""
    violations = _clean(
        "ستدرس لاحقًا **CS202** ثم **CS212** ضمن مسار البرمجة.",
        question="ما مواد البرمجة القادمة؟",
    )
    assert UNSUPPORTED_ACADEMIC_FACT in violations


def test_the_invented_ds_numbering_guide_is_flagged():
    """Student 4552135 got a fabricated 'your program numbering' guide.

    A numbering SCHEME has nothing to resolve and no tool was called - the
    catalogue floor is the only check that can see this class, which is why it
    runs outside the evidence gate.
    """
    violations = _clean(
        "المقررات في برنامجك تتبع هذا الترقيم عادة: **DS101**: مقدمة في علم "
        "البيانات، **DS201**: هياكل البيانات، ثم DS351 وDS361 وDS371.",
        question="اشرح لي أرقام المقررات.",
    )
    assert UNSUPPORTED_ACADEMIC_FACT in violations


def test_typo_speculation_naming_a_nonexistent_code_is_flagged():
    """'You probably meant DS225' - offered as help, DS225 does not exist."""
    violations = _clean(
        "قد يكون في الرمز خطأ مطبعي، فربما تقصد **DS221** أو **DS225**.",
        question="ما هو مقرر DS222؟",
    )
    assert UNSUPPORTED_ACADEMIC_FACT in violations

    # Naming only the REAL near miss is the grounded version and must pass.
    assert (
        _clean(
            "لا يوجد مقرر برمز DS222؛ ربما تقصد DS221.",
            question="ما هو مقرر DS222؟",
        )
        == []
    )


def test_the_f01_timetable_table_is_flagged():
    """Student 4552135 got a timetable table with F01-F04 sections and times.

    Real codes exist for DS331/IS252, but the sections are invented and the
    turn carried no schedule evidence - both the bare-label section check and
    the schedule-claim check see it now.
    """
    violations = _clean(
        "جدولك المقترح:\n| DS331 | F01 | 08:00 | 10:00 |\n| IS252 | F02 | 10:00 | 12:00 |",
        question="اعرض جدولي.",
    )
    assert UNSUPPORTED_ACADEMIC_FACT in violations


def test_the_telegram_relational_fabrication_is_flagged():
    """Student 4406183's timetable was built from REAL codes in FALSE relations.

    Production truth was five registered courses; the answer scheduled them at
    invented times.  No catalogue check can see this - every code is real - so
    the schedule-claim-without-schedule-evidence check is the defence.
    """
    fabricated = (
        "جدولك هذا الفصل: AI1 الأحد 08:00، وAI433 الاثنين 10:00، "
        "وCS424 الثلاثاء 12:00، وGS104 الأربعاء 09:00، وMGT405 الخميس 11:00."
    )
    violations = _clean(fabricated, question="اعرض جدولي على تيليجرام.")
    assert UNSUPPORTED_ACADEMIC_FACT in violations

    # The same sentence WITH the real timetable behind it is the true answer.
    timetable = {
        "tool": "my_timetable",
        "ok": True,
        "schedule_kind": "REGISTERED",
        "registrations": [
            {"course_code": "AI1", "section": "M6"},
            {"course_code": "AI433", "section": "M6"},
            {"course_code": "CS424", "section": "M9"},
            {"course_code": "GS104", "section": "M18"},
            {"course_code": "MGT405", "section": "M7"},
        ],
        "meetings": [
            {"course_code": "AI1", "section": "M6", "day": "SUN", "start": "08:00", "end": "09:15"},
            {
                "course_code": "AI433",
                "section": "M6",
                "day": "MON",
                "start": "10:00",
                "end": "11:15",
            },
        ],
    }
    supported = "من جدولك المسجل: AI1 يوم الأحد 08:00، وAI433 يوم الاثنين 10:00."
    assert _clean(supported, question="اعرض جدولي.", tools=[timetable]) == []


def test_the_telegram_channel_profile_gets_the_same_boundary(monkeypatch):
    """channel_profile=telegram_safe runs the SAME evidence pipeline.

    telegram_safe is live in production and carried the single worst audited
    fabrication.  This drives the full V2 turn on that profile with a model
    that fabricates a timetable, and asserts the fabrication is not shipped.
    """
    from core.models import Student
    from core.services.student_advisor_v2 import answer_student_advisor_v2
    from tests.test_student_advisor_v2 import (
        SID,
        RepairClient,
        _answer_turn,
        _exact_timetable_result,
        _principal,
        _tool_turn,
    )

    Student.objects.get_or_create(
        student_id=SID,
        defaults={"name": "Replay Student", "program": "CS", "section": "M"},
    )
    monkeypatch.setattr(
        "core.services.student_advisor_v2.execute_student_v2_tool",
        lambda name, arguments, **kwargs: _exact_timetable_result(),
    )
    fabricated = "جدولك يحتوي على AI331 في الشعبة F11 الساعة 11:30."
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
        channel_profile="telegram_safe",
    )

    assert "F11" not in result["answer"]
    assert "11:30" not in result["answer"]
    assert result["agent"]["evidence_validation_outcome"] in {
        "repaired",
        "verified_fallback",
        "abstained",
    }


ADVERSARIAL_CASES = [
    # (name, answer, question, expected violations)
    # An acceptance review of the first floor implementation found each of
    # these either escaping (fabrications in the model's own formatting) or
    # being refused (true refusals) - every row here was a live defect once.
    (
        "english refusal passes",
        "I cannot confirm that AI1 meets at 08:00; I have no timetable data for you.",
        "when is AI1?",
        [],
    ),
    (
        "english fabrication flags",
        "Your AI1 lecture is on Sunday at 08:00.",
        "when is AI1?",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "bidun does not launder a schedule",
        "محاضرة AI1 يوم الأحد الساعة 08:00 بدون تعارض مع بقية جدولك.",
        "جدولي؟",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "proclitic wala refusal passes",
        "بيانات جدولك غير متوفرة الآن، ولا أستطيع تأكيد أن AI1 الساعة 08:00.",
        "جدولي؟",
        [],
    ),
    (
        "bullet layout cannot split code from time",
        "جدولك هذا الفصل\n**الأحد**\n- AI1 — 08:00 إلى 09:15\n**الاثنين**\n- AI433 — 10:00",
        "جدولي؟",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "invented sections need no time beside them",
        "مقرر AI331 مطروح في الشعبتين M3 وW2 حسب النظام.",
        "شعب AI331؟",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "question echo does not launder a recommendation",
        "مقرر MATE243 مقرر إجباري في خطتك، والشعبة M1 مفتوحة، وأنصحك بتسجيله.",
        "متى محاضرة MATE243؟",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "spaced code is still a code",
        "ستدرس لاحقًا CS 202 ضمن مسار البرمجة.",
        "ما القادم؟",
        [UNSUPPORTED_ACADEMIC_FACT],
    ),
    (
        "arabic-indic digits in a real code pass",
        "مقرر MATH٢٤٣ من مقررات الخطة.",
        "ماذا أدرس؟",
        [],
    ),
]


@pytest.mark.parametrize(
    ("name", "answer", "question", "expected"),
    ADVERSARIAL_CASES,
    ids=[row[0] for row in ADVERSARIAL_CASES],
)
def test_adversarial_review_corpus(name, answer, question, expected):
    assert _clean(answer, question=question) == expected


def test_the_applied_change_wording_is_a_mutation_claim():
    """«تم تطبيق التعديل المطلوب على جدولك» - the production ledger's wording.

    The adviser mutates nothing, so an affirmative mutation claim is
    unconditionally false; the original phrase list simply lacked this
    spelling and its siblings.
    """
    from core.services.answer_consistency import CLAIMED_PLANNER_MUTATION

    for wording in (
        "تم تطبيق التعديل المطلوب على جدولك بنجاح",
        "تم تحديث الجدول بنجاح.",
        "أضفت المقرر إلى جدولك.",
        "The requested timetable change has been applied successfully.",
    ):
        assert CLAIMED_PLANNER_MUTATION in _clean(wording, question="بدل الشعبة"), wording


def test_a_not_found_echo_cannot_prove_a_course_exists():
    """ok:True tool rows that ECHO the model's argument are not evidence.

    "Look the invented code up, get not-found, assert it anyway" was the
    audited CS202/CS212 model behaviour: course_prerequisites answers a failed
    lookup with the argument echoed beside "Course not found", and
    my_clash_free_sections keeps a NOT_ON_FILE row for the code it could not
    find.  Mining those as existence evidence let the model launder any
    invention by looking it up first.  policy_lookup is free-form and must
    contribute nothing at all.
    """
    fabricated = "مقرر MATE243 من المقررات الإجبارية في خطتك هذا الفصل."
    echoes = [
        {
            "tool": "course_prerequisites",
            "ok": True,
            "course_code": "MATE243",
            "per_program": [],
            "options": [],
            "note": "Course not found in any programme plan.",
        },
        {
            "tool": "my_clash_free_sections",
            "ok": True,
            "courses": [{"course_code": "MATE243", "status": "NOT_ON_FILE", "clash_free": []}],
        },
        {"tool": "policy_lookup", "ok": True, "applies_to": ["MATE243"]},
    ]
    for echo in echoes:
        assert UNSUPPORTED_ACADEMIC_FACT in _clean(
            fabricated, question="ماذا أدرس؟", tools=[echo]
        ), echo["tool"]

    # A RESOLVED row is real evidence: the same sentence about a course the
    # prerequisites tool actually found must pass even off-catalogue.
    resolved = {
        "tool": "course_prerequisites",
        "ok": True,
        "course_code": "XX999",
        "per_program": [{"program": "AI", "prerequisites": ["AI331"]}],
    }
    assert (
        _clean(
            "مقرر XX999 من المقررات الإجبارية في خطتك هذا الفصل.",
            question="ماذا أدرس؟",
            tools=[resolved],
        )
        == []
    )


def test_room_codes_and_admin_times_are_not_academic_claims():
    """LIB826 is a room and office hours are not a meeting.

    29 real rooms are course-code shaped and in no catalogue; deadlines and
    office-hour windows carry clock tokens beside course codes.  Both were
    refused as fabrications by the first floor/obligation implementation.
    """
    assert _clean("القاعة LIB826 مغلقة اليوم للصيانة.", question="أين المحاضرة؟") == []
    assert (
        _clean(
            "مقرر CS323 من متطلبات خطتك، وساعات مكتبي من 09:00 إلى 11:00.",
            question="متى أراجعك؟",
        )
        == []
    )
    assert (
        _clean(
            "يمكنك حذف مقرر CS323 حتى الساعة 23:59 من يوم الخميس وفق اللائحة.",
            question="متى آخر موعد للحذف؟",
        )
        == []
    )
    # The admin-time exemption must not swallow a MEETING claim.
    assert UNSUPPORTED_ACADEMIC_FACT in _clean(
        "محاضرة CS323 يوم الأحد من 09:00 إلى 10:15.",
        question="متى المحاضرة؟",
    )


def test_the_review_mutants_stay_dead():
    """Each block below kills one mutation an independent run proved surviving."""
    # M10: a clock token with NO course code near it must not trip the
    # schedule obligation.
    assert _clean("تبدأ محاضرتك الأولى الساعة 09:00 غالبًا.", question="متى أبدأ؟") == []

    # M9: «القاعة M7» stays a room even while a real section is named.
    clash_free = {
        "tool": "my_clash_free_sections",
        "ok": True,
        "courses": [
            {
                "course_code": "AI331",
                "status": "OK",
                "clash_free": [{"section": "M6", "meetings": ["SUN 09:00-10:15"]}],
            }
        ],
    }
    # No meeting claim here on purpose: the clash-free payload carries no
    # rooms, so pairing a room WITH a time would be an unsupported meeting
    # relation (correctly refused).  This pins only that «القاعة M7» is read
    # as a room, never as a claimed section.
    assert (
        _clean(
            "مقرر AI331 له الشعبة M6، والمحاضرة في القاعة M7.",
            question="أين المحاضرة؟",
            tools=[clash_free],
        )
        == []
    )

    # M5: build_my_timetable and feasible_course_replacements are
    # schedule-bearing - their presence satisfies the obligation.
    for tool in ("build_my_timetable", "feasible_course_replacements"):
        assert (
            _clean(
                "محاضرة AI331 يوم الأحد الساعة 09:00.",
                question="جدولي؟",
                tools=[
                    clash_free | {"tool": tool},
                ],
            )
            == []
        ), tool

    # M11: a bare section label inside an evidence list must not become a
    # course code.
    row = {"tool": "my_timetable", "ok": True, "course_codes": ["M6"]}
    from core.services.answer_consistency import _course_codes_in_evidence

    assert "M6" not in _course_codes_in_evidence(row)

    # M3/M4: payload figures are the graduation-shaped keys, compared as a
    # SUBSET - one matching number cannot vouch for a second invented one,
    # and True is not the number 1.
    comparison = {
        "tool": "course_choice_comparison",
        "ok": True,
        "direct_unlock_count": 5,
        "proven": True,
    }
    assert "exact_academic_figure_mismatch" in _clean(
        "بعد اجتيازه تنفتح لك 5 مقررات ويتبقى لتخرجك 99 فصلًا.",
        question="قارن لي.",
        tools=[comparison],
    )
    assert "exact_academic_figure_mismatch" in _clean(
        "يتبقى لتخرجك فصل دراسي واحد 1 فقط.",
        question="كم يتبقى؟",
        tools=[{"tool": "course_choice_comparison", "ok": True, "proven": True}],
    )


def test_a_cited_policy_figure_must_come_from_that_policy():
    """Class I from the audit: a wrong summer-hours figure with a REAL citation.

    The citation contract proved the id existed; nothing proved the number
    beside it was what the policy says.  The clause naming the id claims its
    provenance, so the named source is the only admissible authority for that
    clause's figures.
    """
    from core.services.answer_consistency import POLICY_FIGURE_MISMATCH

    policy = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.REG.SUMMER.1",
                "rule": "Summer registration is capped at 10 credit hours.",
                "statement_ar": "الحد الأعلى للتسجيل في الفصل الصيفي عشر ساعات (10) معتمدة.",
            }
        ],
    }

    # The credit-cap spelling is already owned by the legacy cap check when
    # policy evidence is present - EITHER code refusing it is correct.
    fabricated_cap = "الحد الأعلى في الفصل الصيفي هو 12 ساعة معتمدة. [TU.REG.SUMMER.1]"
    assert _clean(fabricated_cap, question="كم ساعة أسجل بالصيف؟", tools=[policy]) != []

    truthful = "الحد الأعلى في الفصل الصيفي هو 10 ساعات معتمدة. [TU.REG.SUMMER.1]"
    assert _clean(truthful, question="كم ساعة أسجل بالصيف؟", tools=[policy]) == []

    # The NEW binding owns what the cap check never saw: non-cap figure kinds
    # beside a citation.  Two courses in the policy text, three in the answer.
    count_policy = {
        "tool": "policy_lookup",
        "ok": True,
        "direct_policy_evidence": [
            {
                "policy_id": "TU.REG.SUMMER.2",
                "rule": "At most 2 courses may be taken in the summer term.",
                "statement_ar": "يسمح بتسجيل مقررين (2) كحد أقصى في الفصل الصيفي.",
            }
        ],
    }
    assert POLICY_FIGURE_MISMATCH in _clean(
        "يمكنك تسجيل 3 مقررات في الفصل الصيفي وفق [TU.REG.SUMMER.2].",
        question="كم مقررًا أسجل بالصيف؟",
        tools=[count_policy],
    )
    assert (
        _clean(
            "يمكنك تسجيل 2 مقررات كحد أقصى في الفصل الصيفي وفق [TU.REG.SUMMER.2].",
            question="كم مقررًا أسجل بالصيف؟",
            tools=[count_policy],
        )
        == []
    )

    # SUBSET, not overlap: one true figure must not vouch for a second
    # invented one in the same cited clause.  A citation clause speaks with
    # the policy's numbers only - personal-progress figures belong in their
    # own sentence, which is also how honest prose reads.
    assert POLICY_FIGURE_MISMATCH in _clean(
        "يسمح لك بتسجيل 2 مقررات صيفًا وسيتبقى أمامك 9 مقررات وفق [TU.REG.SUMMER.2].",
        question="كم مقررًا أسجل بالصيف؟",
        tools=[count_policy],
    )

    # A figure in an UNCITED clause is not this check's business - other
    # checks own it, and double-claiming would refuse ordinary prose.
    uncited = "عادة تكون الدراسة الصيفية أقصر من الفصل العادي."
    assert _clean(uncited, question="كم ساعة أسجل بالصيف؟", tools=[policy]) == []
