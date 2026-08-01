"""current_term_registrations evidence in the verified student context.

Live bug (student 4404824, 2026-06-11): the chat reported 3 registered
courses because the plan-status ``studying`` set (StudentCourse) cannot
represent retakes — a course passed in an earlier term and re-registered
this term keeps status='passed' there. The Timetable Builder showed the
correct 5 sections because it reads StudentTermSection. The student
context now carries a section-level registration block from the same
source the Timetable Builder uses (get_student_term_baseline).
"""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.virtual_advisor import build_verified_student_context

pytestmark = pytest.mark.django_db

SID = 4404824


def _course(code: str, name: str, credits: int, plan_term: int = 4) -> Course:
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code=code,
        course_name=name,
        type="Core",
        programme_term=plan_term,
        credit_hours=credits,
    )
    return Course.objects.create(course_code=code, description=name, credit_hours=credits)


def _register(student_id: int, code: str, section: str, year: str = "1447", term: str = "2"):
    ts = TermSection.objects.create(
        course_code=code.rstrip("0123456789"),
        course_number=code[len(code.rstrip("0123456789")) :],
        course_key=code,
        course_name=code,
        section=section,
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year=year,
        term=term,
        term_section=ts,
        source="scraper_timetable",
    )
    return ts


def _make_retake_student() -> Student:
    student = Student.objects.create(
        student_id=SID,
        name="Retake Student",
        program="AI",
        section="M",
        gpa=2.7,
        total_earned_credits=88,
        current_registered_credits=16,
    )
    for code, name, credits, status in [
        ("CS289", "Software Engineering", 4, "passed"),
        ("GS103", "Islamic Studies", 2, "passed"),
        ("CS372", "Database Systems", 4, "studying"),
        ("ENGL214", "Technical Writing", 3, "studying"),
        ("MATH243", "Linear Algebra 1", 3, "studying"),
    ]:
        course = _course(code, name, credits)
        StudentCourse.objects.create(student=student, course=course, status=status)
    for code, section in [
        ("CS289", "M16"),
        ("CS372", "M2"),
        ("ENGL214", "M26"),
        ("GS103", "M5"),
        ("MATH243", "M17"),
    ]:
        _register(SID, code, section)
    return student


def test_retaken_courses_appear_in_current_registrations():
    _make_retake_student()
    context = build_verified_student_context(student_id=SID)

    block = context["course_evidence"]["current_term_registrations"]
    codes = {row["course_code"] for row in block["registrations"]}
    assert codes == {"CS289", "CS372", "ENGL214", "GS103", "MATH243"}
    assert block["registered_course_count"] == 5
    assert block["registered_credit_hours"] == 16
    assert block["academic_year"] == "1447"
    assert block["term"] == "2"
    assert block["source"] == "timetable_sections"

    by_code = {row["course_code"]: row for row in block["registrations"]}
    assert by_code["CS289"]["retake"] is True
    assert by_code["GS103"]["retake"] is True
    assert by_code["CS372"]["retake"] is False
    assert by_code["CS289"]["section"] == "M16"

    # The plan-status list stays as-is (still useful for plan progress).
    assert set(context["course_evidence"]["studying"]) == {"CS372", "ENGL214", "MATH243"}


def test_latest_term_wins_over_older_registrations():
    Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    course = _course("CS101", "Intro", 3)
    StudentCourse.objects.create(
        student=Student.objects.get(student_id=SID), course=course, status="passed"
    )
    _register(SID, "CS101", "M1", year="1446", term="2")
    _register(SID, "CS201", "M3", year="1447", term="1")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert block["academic_year"] == "1447"
    assert block["term"] == "1"
    assert {row["course_code"] for row in block["registrations"]} == {"CS201"}


def test_plan_status_fallback_when_no_timetable_rows():
    student = Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    course = _course("CS372", "Database Systems", 4)
    StudentCourse.objects.create(student=student, course=course, status="studying")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert block["source"] == "plan_status_fallback"
    assert block["academic_year"] is None and block["term"] is None
    assert {row["course_code"] for row in block["registrations"]} == {"CS372"}
    assert block["registrations"][0]["section"] == ""


def test_unmapped_studying_course_unioned_into_registrations():
    student = Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    mapped = _course("CS372", "Database Systems", 4)
    unmapped = _course("ENGL214", "Technical Writing", 3)
    StudentCourse.objects.create(student=student, course=mapped, status="studying")
    StudentCourse.objects.create(student=student, course=unmapped, status="studying")
    _register(SID, "CS372", "M2")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    codes = {row["course_code"] for row in block["registrations"]}
    assert codes == {"CS372", "ENGL214"}
    assert block["registered_credit_hours"] == 7


def test_multi_section_course_counts_credits_once():
    Student.objects.create(student_id=SID, name="S", program="AI", section="M")
    _course("CS372", "Database Systems", 4)
    _register(SID, "CS372", "M2")
    _register(SID, "CS372", "M2L")

    block = build_verified_student_context(student_id=SID)["course_evidence"][
        "current_term_registrations"
    ]
    assert len(block["registrations"]) == 2
    assert block["registered_course_count"] == 1
    assert block["registered_credit_hours"] == 4


def test_recommendation_policy_exposes_real_credit_limit():
    """The model invented a '21-credit standard' and subtracted current-term
    credits from it (live failure, 2026-06-11). The context must carry the
    actual recommender cap so load answers are grounded."""
    _make_retake_student()
    # Eligible for the planning term: odd plan term to match next-term parity
    # for student 44xxxxx asked about 1448/1 (real term 8 → next term 9, odd).
    _course("AI305", "Neural Networks", 3, plan_term=5)

    context = build_verified_student_context(student_id=SID, academic_year=1448, term=1)

    policy = context["recommendation_policy"]
    # The suggestion cap and the registration ceiling are DIFFERENT numbers.
    assert policy["max_recommended_credit_hours"] == 18
    assert policy["regulatory_max_credit_hours"] == 19
    assert policy["regulatory_min_credit_hours"] == 12
    assert "max_term_credit_hours" not in policy, (
        "the old key conflated the advisory cap with the university limit"
    )
    assert policy["recommended_credit_hours"] <= 18
    recs = context["recommendations"]
    assert {rec["course_code"] for rec in recs} == {"AI305"}
    assert recs[0]["credit_hours"] == 3
    assert policy["recommended_credit_hours"] == 3
    assert policy["credit_hours_unknown_for"] == []
    assert context["term_context"]["role"] == "planning_term_for_recommendations"


def test_recommend_capability_reports_credit_policy():
    from core.services.rbac import ROLE_SUPER_ADMIN
    from core.services.virtual_advisor_capabilities import get_default_registry

    _make_retake_student()
    _course("AI305", "Neural Networks", 3, plan_term=5)

    result = get_default_registry().execute(
        "recommend_courses",
        {"student_id": SID},
        scope={"role": ROLE_SUPER_ADMIN},
        ctx={"academic_year": 1448, "term": 1},
    )
    assert result["ok"] is True
    assert result["credit_policy"]["max_recommended_credit_hours"] == 18
    assert result["credit_policy"]["regulatory_max_credit_hours"] == 19
    assert "max_term_credit_hours" not in result["credit_policy"]
    assert result["credit_policy"]["recommended_credit_hours"] == 3
    assert result["credit_policy"]["credit_hours_unknown_for"] == []
    assert result["recommendations"][0]["credit_hours"] == 3


def test_recommendation_cap_is_below_the_registration_ceiling():
    """18 is what we SUGGEST; 19 is what the university lets a student register.

    Guarding the distinction itself rather than either number: if someone later
    "tidies" these into one constant, a student asking how many hours they may
    register gets told 18 and quietly loses a unit they are entitled to.
    """
    from core.services.credit_policy import (
        RECOMMENDED_MAX_CREDITS,
        REGULATORY_MAX_CREDITS,
        REGULATORY_MIN_CREDITS,
        credit_policy_evidence,
    )

    assert RECOMMENDED_MAX_CREDITS < REGULATORY_MAX_CREDITS
    assert REGULATORY_MIN_CREDITS < RECOMMENDED_MAX_CREDITS

    evidence = credit_policy_evidence(recommended_credit_hours=15, unknown_for=[], term=1)
    assert evidence["max_recommended_credit_hours"] == RECOMMENDED_MAX_CREDITS
    assert evidence["regulatory_max_credit_hours"] == REGULATORY_MAX_CREDITS
    assert "max_term_credit_hours" not in evidence
    # The note must tell the model the two differ, or the field names alone will
    # not stop it presenting the cap as the limit.
    assert "regulatory_max_credit_hours" in evidence["note"]
    assert "max_recommended_credit_hours" in evidence["note"]


def test_both_recommenders_share_one_cap():
    """recommender and recommender_batch held the literal 18 independently."""
    from core.services import recommender, recommender_batch
    from core.services.credit_policy import RECOMMENDED_MAX_CREDITS

    assert recommender.MAX_CREDITS == RECOMMENDED_MAX_CREDITS
    assert recommender_batch.MAX_CREDITS == RECOMMENDED_MAX_CREDITS


def test_advisor_prompts_teach_the_distinction():
    from core.services.virtual_advisor import SYSTEM_PROMPT, SYSTEM_PROMPT_AGENT

    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_AGENT):
        assert "regulatory_max_credit_hours" in prompt
        assert "max_recommended_credit_hours" in prompt
        assert "max_term_credit_hours" not in prompt


def test_summer_term_publishes_no_registration_limit():
    """Term 3's real cap is 9. Asserting 12..19 there overstates by three.

    The first version of this module documented "summer is not modelled" in a
    docstring and then served 19 anyway, because the warning lived in a constant
    nothing imported. Absence of the key is what makes the prompt's "say the system
    does not define one" branch fire.
    """
    from core.services.credit_policy import SUMMER_TERM, credit_policy_evidence

    ev = credit_policy_evidence(recommended_credit_hours=9, unknown_for=[], term=SUMMER_TERM)
    assert "regulatory_max_credit_hours" not in ev
    assert "regulatory_min_credit_hours" not in ev
    assert "regulatory_range_unknown" in ev
    assert "9" in ev["regulatory_range_unknown"]


def test_unknown_term_does_not_assume_a_main_term():
    """A caller that cannot say which term it is may not publish a term limit."""
    from core.services.credit_policy import credit_policy_evidence

    for term in (None, 0, 7, "1"):
        ev = credit_policy_evidence(recommended_credit_hours=12, unknown_for=[], term=term)
        assert "regulatory_max_credit_hours" not in ev, f"term={term!r} leaked a limit"
        assert "regulatory_range_unknown" in ev


def test_expected_graduates_get_the_unresolved_16_hour_qualification():
    """The source records a SEPARATE 16-hour ceiling for متوقع تخرجه, unresolved."""
    from core.services.credit_policy import EXPECTED_GRADUATE_STATUS, credit_policy_evidence

    ev = credit_policy_evidence(
        recommended_credit_hours=12, unknown_for=[], term=1,
        student_status=EXPECTED_GRADUATE_STATUS,
    )
    assert ev["qualification"]["unresolved"] is True
    assert "16" in ev["qualification"]["detail_ar"]

    ordinary = credit_policy_evidence(
        recommended_credit_hours=12, unknown_for=[], term=1, student_status="ACTIVE"
    )
    assert "qualification" not in ordinary


def test_regulatory_figure_carries_its_own_basis():
    """A number the model asserts to a student must arrive with its provenance.

    Written only in a docstring, the caveat reached nobody — which is exactly how
    the first version shipped an unhedged regulatory claim.
    """
    from core.services.credit_policy import credit_policy_evidence

    ev = credit_policy_evidence(recommended_credit_hours=12, unknown_for=[], term=1)
    basis = ev["regulatory_basis"]
    assert basis["page"] == 23
    assert "NOT_REGISTRAR_VERIFIED" in basis["verification_status"]
    assert basis["hedge"]


def test_evidence_supplies_ready_made_arabic():
    """Both limits translate to الحد الأعلى; leaving it to the model loses the distinction."""
    from core.services.credit_policy import credit_policy_evidence

    ev = credit_policy_evidence(recommended_credit_hours=12, unknown_for=[], term=1)
    assert ev["phrasing_ar"]["recommended"].startswith("سقف التوصية")
    assert "الحد الأعلى" in ev["phrasing_ar"]["regulatory"]


def test_capability_description_never_binds_the_list_to_the_regulatory_limit():
    """The tool description is shipped to the model verbatim and had no test.

    That is precisely where the conflation survived a green suite: the evidence dict
    and both prompts were fixed while the string telling the model what the tool
    GUARANTEES still said the list was capped to the university limit.
    """
    import re

    from core.services.virtual_advisor_capabilities import get_default_registry

    desc = get_default_registry().capabilities["recommend_courses"].description
    assert "max_recommended_credit_hours" in desc
    # "capped"/"limit" must never share a sentence with the regulatory figure.
    for sentence in re.split(r"(?<=[.!?])\s+", desc):
        if "regulatory_max_credit_hours" in sentence:
            assert not re.search(r"\bcapped\b|\bcap\b", sentence), (
                f"'capped' shares a clause with the regulatory limit: {sentence!r}"
            )


def test_planner_does_not_advertise_the_advisory_cap_as_the_limit():
    """The planner drawer labels credit_cap 'الحد الأعلى للساعات' and CP-SAT enforces it."""
    import inspect

    from core import planner_views
    from core.services.credit_policy import RECOMMENDED_MAX_CREDITS, REGULATORY_MAX_CREDITS

    src = inspect.getsource(planner_views)
    assert '"credit_cap": 18' not in src, "hardcoded literal is back"
    assert "RECOMMENDED_MAX_CREDITS" in src
    assert "regulatory_max_credits" in src
    assert RECOMMENDED_MAX_CREDITS < REGULATORY_MAX_CREDITS


def test_regulatory_minimum_has_one_definition():
    from core.services import credit_shortfall_analysis
    from core.services.credit_policy import REGULATORY_MIN_CREDITS

    assert credit_shortfall_analysis.MIN_CREDITS == REGULATORY_MIN_CREDITS


def test_prompts_teach_the_arabic_distinction_and_the_absent_case():
    from core.services.virtual_advisor import SYSTEM_PROMPT, SYSTEM_PROMPT_AGENT

    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_AGENT):
        assert "سقف التوصية" in prompt, "no Arabic term reserved for the advisory cap"
        assert "ABSENT" in prompt, "no rule for a term with no known limit"
        assert "qualification" in prompt
        # The load-bearing half is the PROHIBITION. Asserting only that the good term
        # appears somewhere lets the rule be gutted while a worked example keeps the
        # phrase alive — which is exactly what a mutation run showed.
        rule = next(
            (ln for ln in prompt.splitlines() if "ARABIC TERMINOLOGY" in ln), ""
        )
        assert rule, "the Arabic terminology rule is gone"
        # Pin the INSTRUCTION, not the vocabulary. Both Arabic terms appear in the
        # worked example on the same line, so any check for their mere presence
        # survives the rule being inverted — a mutation run proved exactly that.
        assert "الحد الأعلى المسموح بتسجيله» for regulatory_max_credit_hours ONLY" in rule, (
            "the regulatory term is no longer reserved to the regulatory figure"
        )
        assert "recommendation cap «سقف التوصية» — never «الحد الأعلى»" in rule, (
            "the rule no longer forbids calling the recommendation cap الحد الأعلى"
        )
