"""The ten postconditions, and the correct answers they must not refuse.

Every check runs over free prose, and a false positive does not soften an answer —
it replaces it with a referral. So each case below is paired: one answer that must be
caught, and one CORRECT answer about the same subject that must not be. The second
half is the harder one and the reason the checks are anchored on identifiers.
"""

from __future__ import annotations

import pytest

from core.services.answer_consistency import (
    ALL_CHECKS,
    CLAIMED_PLANNER_MUTATION,
    CLAIMED_REGISTRATION_MUTATION,
    CREDIT_CAP_CONTRADICTION,
    INVENTED_OPTION_CONTENTS,
    NOT_ON_FILE_TO_NOT_OFFERED,
    PREREQ_TO_REGISTRATION_LEAP,
    RETAINED_ADD_CONTRADICTION,
    SEAT_CLAIM,
    UNSUPPORTED_RECOMMENDATION,
    UNSUPPORTED_STUDENT_REQUEST,
    check_answer,
)

TIMETABLE = {
    "tool": "build_my_timetable",
    "ok": True,
    "student_requested_courses": [{"course_code": "AI352", "source": "STUDENT_REQUEST"}],
    "system_recommended_courses": [{"course_code": "CS323", "source": "SYSTEM_RECOMMENDATION"}],
    "retained_sections": [{"course_code": "AI331", "section": "M1", "credit_hours": 4}],
    "new_sections": [{"course_code": "AI352", "section": "M3", "credit_hours": 3}],
    "section_replacements": [],
    "unplaced_courses": [{"course_code": "GSE1", "reason_code": "NOT_ON_FILE"}],
    "credit_summary": {
        "retained_credit_hours": 4,
        "new_credit_hours": 3,
        "total_plan_credit_hours": 7,
        "new_courses_credit_cap": 6,
    },
}
PROGRESS = {"tool": "my_progress", "ok": True, "counts": {"open": 7}}


def _check(answer, results=(TIMETABLE,)):
    return check_answer(answer, tool_results=list(results))


def test_every_check_has_a_code_and_they_are_unique() -> None:
    assert len(ALL_CHECKS) == 10
    assert len(set(ALL_CHECKS)) == 10


# ── 1 / 2: the timetable's own shape ─────────────────────────────────────────


def test_a_course_kept_and_added_at_once_is_caught() -> None:
    facts = {
        **TIMETABLE,
        "retained_sections": [{"course_code": "CS323", "section": "M1", "credit_hours": 4}],
        "new_sections": [{"course_code": "CS323", "section": "M2", "credit_hours": 4}],
    }
    assert RETAINED_ADD_CONTRADICTION in _check("جدولك جاهز.", [facts])


def test_a_declared_replacement_is_not_a_contradiction() -> None:
    """The transition is allowed; asserting both memberships without it is not."""
    facts = {
        **TIMETABLE,
        "retained_sections": [{"course_code": "CS323", "section": "M1", "credit_hours": 4}],
        "new_sections": [{"course_code": "CS323", "section": "M2", "credit_hours": 4}],
        "section_replacements": [
            {"course_code": "CS323", "from_section": "M1", "to_section": "M2"}
        ],
    }
    assert RETAINED_ADD_CONTRADICTION not in _check("تم استبدال الشعبة.", [facts])


def test_a_section_the_payload_never_held_is_caught() -> None:
    assert INVENTED_OPTION_CONTENTS in _check("سجّل شعبة M9 لمقرر AI352.")


def test_the_real_sections_are_not_flagged() -> None:
    assert INVENTED_OPTION_CONTENTS not in _check("شعبة M1 محفوظة، وشعبة M3 مضافة.")


# ── 3 / 4: provenance ────────────────────────────────────────────────────────


def test_claiming_the_student_asked_for_a_recommended_course_is_caught() -> None:
    assert UNSUPPORTED_STUDENT_REQUEST in _check("أضفت CS323 الذي طلبته.")


def test_naming_the_course_the_student_really_asked_for_is_not_flagged() -> None:
    assert UNSUPPORTED_STUDENT_REQUEST not in _check("أضفت AI352 الذي طلبته.")


def test_claiming_the_system_recommended_a_requested_course_is_caught() -> None:
    assert UNSUPPORTED_RECOMMENDATION in _check("اقترح النظام AI352.")


# ── 5 / 6: the two leaps ─────────────────────────────────────────────────────


def test_prerequisites_satisfied_may_not_become_permission() -> None:
    assert PREREQ_TO_REGISTRATION_LEAP in _check("يمكنك التسجيل في AI352.", [PROGRESS])


def test_saying_you_cannot_promise_registration_is_not_a_claim() -> None:
    answer = "استوفيت المتطلبات السابقة، لكن لا يمكنك التسجيل بناءً على هذا وحده."
    assert PREREQ_TO_REGISTRATION_LEAP not in check_answer(answer, tool_results=[PROGRESS])


def test_not_on_file_may_not_become_not_offered() -> None:
    assert NOT_ON_FILE_TO_NOT_OFFERED in _check("GSE1 غير مطروح هذا الفصل.")


def test_explaining_not_on_file_correctly_is_not_flagged() -> None:
    answer = "GSE1 لا توجد له شعبة في بياناتنا؛ هذا لا يعني أنه غير مطروح."
    assert NOT_ON_FILE_TO_NOT_OFFERED not in _check(answer)


# ── 7: the weakest of the ten ────────────────────────────────────────────────


def test_a_seat_claim_is_caught() -> None:
    assert SEAT_CLAIM in _check("شعبة M3 فيه مقاعد متاحة.")


def test_the_correct_seat_disclaimer_is_not_a_seat_claim() -> None:
    """The check the module calls its weakest. A correct answer explains the limit
    using the same word, so the negator window is what separates them."""
    assert SEAT_CLAIM not in _check("لا يوجد مقاعد متاحة في بياناتنا، فلا أستطيع تأكيد ذلك.")


# ── 8: credits ───────────────────────────────────────────────────────────────


def test_a_credit_figure_the_payload_never_stated_is_caught() -> None:
    assert CREDIT_CAP_CONTRADICTION in _check("الجدول يحتوي 12 ساعة.")


def test_the_payloads_own_figures_are_not_flagged() -> None:
    assert CREDIT_CAP_CONTRADICTION not in _check("أضفنا 3 ساعات، والمجموع 7 ساعات.")


# ── 9 / 10: mutations the adviser never performs ─────────────────────────────


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ("حفظت الخيار الثاني كمفضل.", CLAIMED_PLANNER_MUTATION),
        ("I saved the option for you.", CLAIMED_PLANNER_MUTATION),
        ("سجلت لك المقررات في البوابة.", CLAIMED_REGISTRATION_MUTATION),
        ("I have registered you.", CLAIMED_REGISTRATION_MUTATION),
    ],
)
def test_a_claimed_mutation_is_caught(answer: str, code: str) -> None:
    assert code in _check(answer)


def test_offering_the_planner_is_not_claiming_the_save() -> None:
    """The hand-off wording every route uses. If this ever trips, every planner
    referral in the product becomes a refusal."""
    answer = (
        "افتح المخطط الدراسي لحفظ الخيار المفضل. لن يحذف أو يغيّر تسجيلك الرسمي، "
        "ولم أسجل لك أي مقرر."
    )
    assert _check(answer) == []


def test_an_answer_with_no_timetable_evidence_is_left_alone() -> None:
    """Most checks compare against a payload. With none, an answer that made no
    timetable claim cannot contradict one — and must not be refused for it."""
    assert check_answer("مرحبًا، كيف أستطيع مساعدتك؟", tool_results=[]) == []


def test_naming_a_recommended_course_without_a_provenance_claim_is_not_flagged() -> None:
    """The false-positive half of checks 3 and 4, and the one that matters most.

    A timetable answer names every course in the plan — that is what it is for. The
    violation is CLAIMING the student asked for one when they did not, so both halves
    are required: a provenance phrase AND a code that contradicts it. Dropping the
    phrase requirement turns every ordinary answer into a refusal, because every
    ordinary answer names a recommended course.
    """
    answer = "الجدول يضم CS323 يوم الأحد و AI352 يوم الاثنين، وكلاهما بلا تعارض."
    assert UNSUPPORTED_STUDENT_REQUEST not in _check(answer)
    assert UNSUPPORTED_RECOMMENDATION not in _check(answer)
    assert _check(answer) == []


# ── 8, rewritten: credit figures are checked against the source that owns them ──

_POLICY = {
    "tool": "policy_lookup",
    "ok": True,
    "direct_policy_evidence": [
        {"policy_id": "TU.LOAD.SEMESTER_RANGE", "text": "الحد الأعلى 19 ساعة والحد الأدنى 12 ساعة."}
    ],
}
_CONTEXT = {"recommendation_policy": {"max_recommended_credit_hours": 18}}


def test_the_three_credit_authorities_may_all_appear_in_one_answer() -> None:
    """TT08, the case this check refused on the live canary.

    «أريد تسجيل 19 ساعة» deserves an answer that states three true numbers from three
    different places: 19 requested and permitted, 18 advised by the recommender, 15
    already held. Only one of those is in `credit_summary`, and comparing them all
    against it called the other two contradictions — refusing the question this whole
    branch exists for.
    """
    facts = {
        **TIMETABLE,
        "credit_summary": {
            "retained_credit_hours": 15,
            "new_credit_hours": 0,
            "total_plan_credit_hours": 15,
            "new_courses_credit_cap": 19,
        },
    }
    answer = "طلبت 19 ساعة، والموصى به 18 ساعة، ولديك حاليًا 15 ساعة."
    assert CREDIT_CAP_CONTRADICTION not in check_answer(
        answer, tool_results=[facts, _POLICY], context=_CONTEXT
    )


def test_a_figure_from_no_source_at_all_is_still_a_contradiction() -> None:
    """Source-aware is not the same as permissive."""
    facts = {
        **TIMETABLE,
        "credit_summary": {
            "retained_credit_hours": 15,
            "new_credit_hours": 0,
            "total_plan_credit_hours": 15,
            "new_courses_credit_cap": 19,
        },
    }
    assert CREDIT_CAP_CONTRADICTION in check_answer(
        "الجدول يحتوي 7 ساعات.", tool_results=[facts, _POLICY], context=_CONTEXT
    )


def test_attributing_the_advisory_number_to_the_regulation_still_fails() -> None:
    """The reason "exempt anything with a citation" was the wrong repair.

    18 is a real number from a real source — it is the recommender's advice. Calling
    it «الحد الأعلى» attributes it to the لائحة, which says 19. The figure exists;
    the attribution is false, and misattribution is precisely what the citation
    contract exists to stop.
    """
    facts = {
        **TIMETABLE,
        "credit_summary": {
            "retained_credit_hours": 0,
            "new_credit_hours": 0,
            "total_plan_credit_hours": 0,
            "new_courses_credit_cap": 19,
        },
    }
    assert CREDIT_CAP_CONTRADICTION in check_answer(
        "الحد الأعلى 18 ساعة معتمدة.", tool_results=[facts, _POLICY], context=_CONTEXT
    )
    assert CREDIT_CAP_CONTRADICTION not in check_answer(
        "الحد الأعلى 19 ساعة معتمدة.", tool_results=[facts, _POLICY], context=_CONTEXT
    )


def test_credit_numbers_in_prose_with_no_timetable_are_not_checked() -> None:
    """«وش عندي بكرة الأحد؟» builds nothing, so nothing can contradict it."""
    answer = "مقرر AI221 بثلاث ساعات، والحد 19 ساعة معتمدة، صفحة 28."
    assert check_answer(answer, tool_results=[_POLICY], context=_CONTEXT) == []
