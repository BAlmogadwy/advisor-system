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
