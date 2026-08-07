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


def test_a_load_figure_the_payload_never_stated_is_caught() -> None:
    # A CLAIMED load, not any number beside a credit word — see the narrowing at the
    # foot of this file for why the difference is the whole check.
    assert CREDIT_CAP_CONTRADICTION in _check("المجموع 12 ساعة.")


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


def test_a_total_from_no_source_at_all_is_still_a_contradiction() -> None:
    """Source-aware is not the same as permissive: a CLAIMED total is still checked."""
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
        "مجموع الخطة 7 ساعات.", tool_results=[facts, _POLICY], context=_CONTEXT
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


# ── the nested projectors, with sentinels at every depth ────────────────────


def _all_keys(value) -> set:
    """Every key at every depth. A projection is only fail-closed if it is closed
    all the way down, and a top-level assertion cannot see that."""
    found = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(key)
            found |= _all_keys(inner)
    elif isinstance(value, list):
        for item in value:
            found |= _all_keys(item)
    return found


def _all_strings(value) -> str:
    if isinstance(value, dict):
        return " ".join(_all_strings(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_all_strings(v) for v in value)
    return str(value)


SENTINELS = {
    "instructor_email": "must-not-leave@example.com",
    "internal_operator_note": "SECRET",
    "instructor": "Dr Someone",
    "room": "B101",
    "runtime_use_note": "operator only",
}


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        (
            "my_plan_by_term",
            {
                "tool": "my_plan_by_term",
                "ok": True,
                "student_id": 4400251,
                "program": "AI",
                "terms": [
                    {
                        "term": 6,
                        "courses": [
                            {
                                "course_code": "AI331",
                                "credit_hours": 4,
                                "status": "passed",
                                "student_id": 4400251,
                                **SENTINELS,
                            }
                        ],
                    }
                ],
            },
        ),
        (
            "my_clash_free_sections",
            {
                "tool": "my_clash_free_sections",
                "ok": True,
                "student_id": 4400251,
                "sections": [
                    {
                        "course_code": "AI352",
                        "section": "M1",
                        "meetings": [
                            {"day": "MON", "start_time": "09:00", "end_time": "10:15", **SENTINELS}
                        ],
                        "collisions": [{"course_code": "AI331", "day": "MON", **SENTINELS}],
                        **SENTINELS,
                    }
                ],
            },
        ),
    ],
)
def test_a_nested_structure_is_projected_all_the_way_down(tool: str, payload: dict) -> None:
    """The allowlist used to stop at the container.

    `terms`, `plan` and `sections` passed through whole, so the fail-closed design
    held at the top level and let anything through one level below it — including the
    staff names `_project_my_timetable` deliberately drops, arriving by another door.
    Sentinels are planted at EVERY depth because an assertion on the outer keys
    cannot see the inner ones.
    """
    from core.services.llm_remote_privacy import RemoteIdentityMap, project_tool_result_for_remote

    projected = project_tool_result_for_remote(tool, payload, RemoteIdentityMap())
    keys = _all_keys(projected)
    blob = _all_strings(projected)

    for sentinel in SENTINELS:
        assert sentinel not in keys, f"{tool} leaked {sentinel}"
    for value in SENTINELS.values():
        assert value not in blob, f"{tool} leaked the value of {value!r}"
    assert "student_id" not in keys, f"{tool} leaked an identity"
    assert "4400251" not in blob

    # …and it still carries what the answer actually needs.
    assert "course_code" in keys


# ── 8, narrowed: only CAP AND LOAD claims are figures this check owns ──

_FACTS = {
    **TIMETABLE,
    "credit_summary": {
        "retained_credit_hours": 15,
        "new_credit_hours": 0,
        "total_plan_credit_hours": 15,
        "new_courses_credit_cap": 19,
    },
}


def _credit(answer: str) -> list[str]:
    return check_answer(answer, tool_results=[_FACTS, _POLICY], context=_CONTEXT)


def test_a_courses_own_credit_hours_are_not_a_cap_claim() -> None:
    """The second live TT08 refusal, and the reason the first fix was incomplete.

    Source-awareness fixed WHICH authority each number was compared against, but not
    WHICH numbers were claims at all — so «AI352 مقرر بثلاث ساعات», an ordinary true
    sentence, was a "cap contradiction" because 3 is not 15, 18 or 19. A course's
    credit hours assert nothing about a limit or a load, and this check has no
    business reading them.
    """
    assert CREDIT_CAP_CONTRADICTION not in _credit("مقرر AI352 بثلاث ساعات، أي 3 ساعات معتمدة.")
    assert CREDIT_CAP_CONTRADICTION not in _credit(
        "AI352 is 3 credit hours; your retained load is 15, the advisory limit is 18, "
        "and the regulatory maximum is 19."
    )


def test_each_cap_claim_is_still_checked_against_its_own_owner() -> None:
    """And the narrowing did not weaken any of them.

    Adding every course's credits to the allowed set would have passed the test above
    and made the check meaningless — «الحد الأعلى 3 ساعات» would then sail through.
    Each of these states a limit or a total, so each is compared to the one source
    entitled to state it.
    """
    assert CREDIT_CAP_CONTRADICTION in _credit("the regulatory maximum is 18")
    assert CREDIT_CAP_CONTRADICTION in _credit("the advisory limit is 19")
    assert CREDIT_CAP_CONTRADICTION in _credit("your proposed total is 19")
    assert CREDIT_CAP_CONTRADICTION in _credit("الحد الأعلى 3 ساعات معتمدة.")
    assert CREDIT_CAP_CONTRADICTION not in _credit("the regulatory maximum is 19")
    assert CREDIT_CAP_CONTRADICTION not in _credit("the advisory limit is 18")
    assert CREDIT_CAP_CONTRADICTION not in _credit("your proposed total is 15")


# ── the rejected draft, as a diagnostic that cannot become a leak ─────────────


def test_a_rejected_draft_excerpt_goes_through_the_boundarys_own_sanitiser() -> None:
    """Why the diagnostic reuses `sanitise_messages` instead of its own regex.

    Two paid canary runs were spent guessing which number tripped the credit check,
    because the refusal replaced the text that contained it. Writing the draft to the
    trace fixes that — but a draft is model output, so it is exactly the material the
    remote boundary exists to filter. Routing it through the boundary means anything
    the transport learns to protect, the trace protects on the same day.
    """
    from core.services.virtual_advisor import _safe_excerpt

    class _Boundary:
        def sanitise_messages(self, messages):
            return [
                {**m, "content": m["content"].replace("4012345", "STUDENT_REF_1")} for m in messages
            ]

    assert (
        _safe_excerpt(_Boundary(), "الرقم 4012345 والمجموع 19") == "الرقم STUDENT_REF_1 والمجموع 19"
    )
    assert len(_safe_excerpt(_Boundary(), "x" * 2000)) == 1500


def test_a_sanitiser_failure_withholds_the_excerpt_rather_than_the_refusal() -> None:
    """A diagnostic must never be able to break the path it is diagnosing."""
    from core.services.virtual_advisor import _safe_excerpt

    class _Broken:
        def sanitise_messages(self, messages):
            raise RuntimeError("boom")

    assert _safe_excerpt(_Broken(), "الرقم 4012345") == "<excerpt withheld: sanitiser failed>"


def test_the_digits_inside_a_course_code_are_not_a_load_figure() -> None:
    """What the first rejected-draft capture showed, on its first run.

    «الشعب المسجلة والمحتفظ بها: AI1، AI331، CS323، CS372 … الساعات المحتفظ بها 15»
    names the retained courses before it names the retained hours. Reading the first
    digit after the phrase read the 1 in AI1, and eleven of the fifty offline answers
    were refused for a load figure no one had claimed. A claim's number is a number,
    not a character inside an identifier — and it can come after the list, so every
    occurrence of the phrase is considered, not just the first.
    """
    answer = (
        "الشعب المسجلة حاليًا والمحتفظ بها: AI1، AI331، CS323، CS372. "
        "الساعات المحتفظ بها 15 والمضافة 0."
    )
    assert CREDIT_CAP_CONTRADICTION not in _credit(answer)
    # A longer retained list pushes the real figure past the window of the FIRST
    # mention. Reading only that mention would not merely miss the claim — it would
    # stop CHECKING it, so a wrong figure would pass silently. Asserted on a wrong
    # figure for exactly that reason: absence proves nothing here.
    long_list = (
        "الشعب المحتفظ بها: AI1، AI331، CS323، CS372، AI221، CS111، GSE1، FE1، MATH201، STAT305. "
    )
    assert CREDIT_CAP_CONTRADICTION not in _credit(long_list + "الساعات المحتفظ بها 15.")
    assert CREDIT_CAP_CONTRADICTION in _credit(long_list + "الساعات المحتفظ بها 12.")
    # And a genuinely wrong figure in the same shape is still caught.
    assert CREDIT_CAP_CONTRADICTION in _credit(
        "الشعب المحتفظ بها: AI1، CS323. الساعات المحتفظ بها 12."
    )


# ── the locality rule, applied to both checks that read prose ─────────────────


def test_a_load_phrase_does_not_reach_into_the_list_beneath_it() -> None:
    """TT16, refused live on a correct answer.

    «لديك حاليًا:» followed by a bulleted list, each item carrying its own credit
    hours, asserts no current load at all. Reading past the line break turned the
    first course's 3 into a claimed load and contradicted the true 15. A claim binds
    to a number in its own clause; the answer's structure says where that ends.
    """
    listed = """لديك حاليًا:
- **AI1** - PROGRAM ELECTIVE COURSE I (الشعبة M1): 3 ساعات
- **AI331** - MACHINE LEARNING (الشعبة M1): 4 ساعات"""
    assert CREDIT_CAP_CONTRADICTION not in _credit(listed)
    # Run-together bullets are the same structure without the newlines, and models
    # emit both. A boundary that only understood newlines would pass this one.
    assert CREDIT_CAP_CONTRADICTION not in _credit("لديك حاليًا: - AI1 3 ساعات - AI331 4 ساعات")
    # And a real load claim in its own clause is still read, and still checked.
    assert CREDIT_CAP_CONTRADICTION not in _credit("لديك حاليًا 15 ساعة مسجلة.")
    assert CREDIT_CAP_CONTRADICTION in _credit("لديك حاليًا 12 ساعة مسجلة.")


def test_each_structural_boundary_stops_a_claim_on_its_own() -> None:
    """All four boundaries, each proved separately.

    The bulleted case alone does not prove them: a newline followed by "- " is also
    matched by the bullet rule, so three of the four could be deleted and the TT16
    test would still pass. Each shape below puts the phrase in a clause with no
    number of its own and a number in the NEXT one — which is exactly the reading
    error that refused TT16, in the four forms an answer can produce it.
    """
    unbulleted_list = """لديك حاليًا:
3 ساعات لمقرر AI1"""
    assert CREDIT_CAP_CONTRADICTION not in _credit(unbulleted_list)
    assert CREDIT_CAP_CONTRADICTION not in _credit("هذه هي الساعات المحتفظ بها. 3 ساعات لمقرر AI1.")
    assert CREDIT_CAP_CONTRADICTION not in _credit(
        "الساعات المحتفظ بها موضحة أدناه؛ 3 ساعات لـ AI1."
    )
    assert CREDIT_CAP_CONTRADICTION not in _credit("الساعات المحتفظ بها - 3 ساعات لمقرر AI1")


def test_provenance_binds_to_the_assertion_that_makes_it() -> None:
    """TT03/TT07, refused live on correct answers.

    The rule was global: once «طلبت» appeared anywhere, every course code anywhere in
    the answer had to be a student request. A timetable answer names both kinds in
    one breath — what the student asked for, and what was carried over from the
    current registration — so naming the second kind was a provenance violation.
    """
    facts = {
        **TIMETABLE,
        "student_requested_courses": [{"course_code": "AI352"}, {"course_code": "AI371"}],
        "retained_sections": [{"course_code": "AI331"}, {"course_code": "CS323"}],
    }
    both_kinds = """المقررات التي طلبتها:
- AI352
- AI371
والشعب التي احتفظت بها من تسجيلك الحالي:
- AI331 شعبة M1
- CS323 شعبة M2"""
    assert UNSUPPORTED_STUDENT_REQUEST not in check_answer(both_kinds, tool_results=[facts])
    # A sentence that names its own courses owns those and does not adopt the list
    # below it -- the shape TT03 actually produced.
    inline = """المقررات التي طلبتها هي AI352 وAI371.
الشعب المحفوظة:
- AI331 شعبة M1"""
    assert UNSUPPORTED_STUDENT_REQUEST not in check_answer(inline, tool_results=[facts])
    # The sharpest form: the assertion names its courses AND ends in a colon that
    # introduces a different list. It owns what it named, not what follows.
    named_then_listed = """المقررات التي طلبتها هي AI352 وAI371، والشعب المحفوظة:
- AI331 شعبة M1
- CS323 شعبة M2"""
    assert UNSUPPORTED_STUDENT_REQUEST not in check_answer(named_then_listed, tool_results=[facts])
    # And the narrowing is not a waiver: a course attributed to the student that the
    # student never requested is still caught, in either shape.
    assert UNSUPPORTED_STUDENT_REQUEST in check_answer(
        "المقررات التي طلبتها هي AI352 وAI331.", tool_results=[facts]
    )
    listed_wrong = """المقررات التي طلبتها:
- AI352
- AI331"""
    assert UNSUPPORTED_STUDENT_REQUEST in check_answer(listed_wrong, tool_results=[facts])


def test_an_arabic_conjunction_does_not_hide_a_course_code() -> None:
    r"""A `\b` word boundary never fires between «و» and «A», because Arabic letters
    are word characters. Every check in this module was blind to the second code in
    «AI352 وAI371» — a provenance rule a model could walk past by writing a
    conjunction."""
    facts = {**TIMETABLE, "student_requested_courses": [{"course_code": "AI352"}]}
    assert UNSUPPORTED_STUDENT_REQUEST in check_answer(
        "المقررات التي طلبتها هي AI352 وAI331.", tool_results=[facts]
    )


# ── what the adversarial review found, after the first locality fix ───────────


def test_a_number_a_course_already_owns_is_not_a_load_claim() -> None:
    """The TT16 refusal, in the four shapes enumerating separators would have missed.

    A first fix broke the claim at newlines, bullets, sentence ends and semicolons.
    A review then wrote the same list comma-joined, in parentheses, on one line — and
    it was refused again, because no separator rule can anticipate how a model will
    punctuate a list. The principle is not punctuation: if a course is named between
    the claim and the number, the number is that course's.
    """
    for listed in (
        "لديك حاليًا: AI1 (3 ساعات)، AI331 (4 ساعات)",
        "لديك حاليًا AI1 3 ساعات و AI331 4 ساعات",
        "الساعات المحتفظ بها — AI1 بواقع 3 ساعات",
    ):
        assert CREDIT_CAP_CONTRADICTION not in _credit(listed), listed
    # And a colon before the figure is NOT a boundary, because that is how a real cap
    # is written. Breaking on the character would have silenced the check instead.
    assert CREDIT_CAP_CONTRADICTION not in _credit("الحد الأعلى: 19 ساعة")
    assert CREDIT_CAP_CONTRADICTION in _credit("الحد الأعلى: 18 ساعة")


def test_a_clock_time_is_not_a_credit_figure() -> None:
    """`_fold` strips the colon, so «09:00» becomes «09 00» and a timetable answer —
    which is nothing but clock times — hands the check a load of 9."""
    assert CREDIT_CAP_CONTRADICTION not in _credit("الساعات المحتفظ بها تبدأ 09:00 يوم الأحد.")
    assert _credit("المحاضرة 10:15، والساعات المحتفظ بها 15.") == []


def test_two_attributions_in_one_sentence_own_different_courses() -> None:
    """A single sentence can carry both provenances. Binding each to the whole LINE
    gave both codes to both, so the answer was refused twice — once for claiming the
    student requested a recommended course, once for the reverse."""
    facts = {
        **TIMETABLE,
        "student_requested_courses": [{"course_code": "AI352"}],
        "system_recommended_courses": [{"course_code": "CS323"}],
    }
    correct = "المقررات التي طلبتها AI352، واقترح النظام CS323."
    assert UNSUPPORTED_STUDENT_REQUEST not in check_answer(correct, tool_results=[facts])
    assert UNSUPPORTED_RECOMMENDATION not in check_answer(correct, tool_results=[facts])
    # Swapping them is still caught, in the same sentence shape.
    swapped = "المقررات التي طلبتها CS323، واقترح النظام AI352."
    assert UNSUPPORTED_STUDENT_REQUEST in check_answer(swapped, tool_results=[facts])
    assert UNSUPPORTED_RECOMMENDATION in check_answer(swapped, tool_results=[facts])


def test_a_citations_page_number_is_not_a_credit_cap() -> None:
    """The live TT16 refusal, found only because the rejected draft was captured.

    «الحد الأعلى … الدليل الإرشادي … ص 23 … يتراوح بين 12 و19 ساعة» is a correct answer
    that cites its source, and the first number after the phrase was the PAGE. The
    unit is what separates a load figure from a page, an edition, a year or a section
    — and an adviser that cites the لائحة properly will always have those nearby.
    """
    cited = "الحد الأعلى المسموح به وفق الدليل الإرشادي، الإصدار الثالث 1447هـ، ص 23 يتراوح بين 12 و19 ساعة."
    assert CREDIT_CAP_CONTRADICTION not in _credit(cited)
    # A range states two true numbers about one limit; either may satisfy the claim.
    assert CREDIT_CAP_CONTRADICTION not in _credit("الحد الأعلى يتراوح بين 12 و19 ساعة.")
    # A wrong figure stated in hours is still caught, page number or no page number.
    assert CREDIT_CAP_CONTRADICTION in _credit("الحد الأعلى وفق الدليل ص 23 هو 18 ساعة.")
    # And the bare-number fallback survives, for claims that name no unit at all.
    assert CREDIT_CAP_CONTRADICTION in _credit("the regulatory maximum is 18")
    assert CREDIT_CAP_CONTRADICTION not in _credit("the regulatory maximum is 19")


def test_every_figure_stated_in_hours_is_a_candidate_not_just_the_first() -> None:
    """A clause can correct itself, and the correction is the claim.

    «ليس 8 ساعات بل 19 ساعة» states the true cap second. Reading only the first
    hour-stated number refuses an answer for the figure it explicitly denied.
    """
    assert CREDIT_CAP_CONTRADICTION not in _credit("الحد الأعلى ليس 8 ساعات بل 19 ساعة.")
    assert CREDIT_CAP_CONTRADICTION not in _credit("الحد الأعلى بين 12 ساعة و19 ساعة.")


def test_a_stray_number_that_happens_to_be_valid_cannot_launder_a_wrong_claim() -> None:
    """Why the unit requirement carries weight even though any candidate may match.

    «ص 19» is a page. If bare numbers were candidates, that 19 would satisfy the
    regulatory set and the answer's actual claim — 18 hours — would never be
    compared. Requiring the unit means only the figure stated AS HOURS is judged.
    """
    assert CREDIT_CAP_CONTRADICTION in _credit("الحد الأعلى وفق الدليل ص 19 هو 18 ساعة.")
    # And when the page is the ONLY number, the bare-number fallback would reach it —
    # so the page marker is stripped before any figure is read, not merely outranked.
    assert CREDIT_CAP_CONTRADICTION not in _credit("الحد الأعلى مذكور في الدليل ص 23.")


def test_the_term_being_planned_is_not_a_retained_load() -> None:
    """The last failure of the final live batch, and the third of its kind.

    «وهي مقررات مسجلة لديك حاليًا في الفصل 1448/1» names the TERM. Folding turns
    «1448/1» into «1448 1», the four-digit year is skipped, and the standalone «1»
    became a claimed current load against a true 15. A clock time, a cited page and
    an academic term are one defect three times: an adviser's sentences are full of
    numbers and almost none of them are credit hours.
    """
    assert CREDIT_CAP_CONTRADICTION not in _credit("وهي مقررات مسجلة لديك حاليًا في الفصل 1448/1:")
    # The term does not shield a real claim sharing the clause.
    assert CREDIT_CAP_CONTRADICTION in _credit("لديك حاليًا 12 ساعة في الفصل 1448/1.")
    assert CREDIT_CAP_CONTRADICTION not in _credit("لديك حاليًا 15 ساعة في الفصل 1448/1.")
