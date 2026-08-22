from __future__ import annotations

import pytest

from core.models import (
    Course,
    ElectiveCourse,
    ElectiveTermMapping,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
)
from core.services.rbac import ROLE_STUDENT

pytestmark = pytest.mark.django_db

SID = 991_440_6183
YEAR = "1448"
TERM = "1"


@pytest.fixture
def production_shape(monkeypatch: pytest.MonkeyPatch) -> Student:
    """4406183 shape: registered placeholder, mapped recommendation alias.

    AI1 is registrar evidence but is absent from StudentCourse.studying. AI2 is
    EXPECTED only. The recommender returns the concrete options AI463/AI464, so a
    string-only comparison would call both new.
    """
    student = Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Canonical integration student",
        program="AI",
        section="M",
        total_earned_credits=60,
    )
    for code, requirement_type, level in (
        ("AI1", "Program Elective", 7),
        ("AI2", "Program Elective", 8),
        ("AI331", "Mandatory", 7),
    ):
        course = Course.objects.create(
            course_code=code,
            description=code,
            credit_hours=3,
        )
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=code,
            type=requirement_type,
            programme_term=level,
            credit_hours=3,
        )
        StudentCourse.objects.create(student=student, course=course, status="not_taken")

    Prerequisite.objects.create(
        program="AI",
        course_code="AI331",
        prerequisite_course_code="AI1",
    )
    for slot, concrete in (("AI1", "AI463"), ("AI2", "AI464")):
        elective = ElectiveCourse.objects.create(
            course_code=concrete,
            course_name=f"Concrete {concrete}",
            programme="AI",
            category="Program Elective",
            credit_hours=3,
        )
        ElectiveTermMapping.objects.create(
            academic_year=YEAR,
            term=int(TERM),
            programme="AI",
            placeholder_code=slot,
            elective=elective,
        )

    registered = TermSection.objects.create(
        course_code="AI1",
        course_number="AI1",
        course_key="AI1",
        course_name="Programme Elective I",
        section="M6",
    )
    expected = TermSection.objects.create(
        course_code="AI2",
        course_number="AI2",
        course_key="AI2",
        course_name="Programme Elective II",
        section="M8",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=registered,
        source="scraper_timetable",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=expected,
        source="registration_plan_1448_t1",
    )

    recommendations = ["AI463", "AI464", "AI331"]
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: recommendations,
    )
    monkeypatch.setattr(
        "core.services.virtual_advisor.recommend_next_courses",
        lambda *_args, **_kwargs: recommendations,
    )
    return student


def _scope() -> dict[str, object]:
    return {"role": ROLE_STUDENT, "student_id": SID}


def _ctx() -> dict[str, int]:
    return {"academic_year": int(YEAR), "term": int(TERM)}


def test_recommendations_suppress_registered_and_expected_elective_aliases(
    production_shape: Student,
) -> None:
    from core.services.virtual_advisor_capabilities import _exec_recommend_courses

    result = _exec_recommend_courses({}, _scope(), _ctx())

    assert [row["course_code"] for row in result["recommendations"]] == ["AI331"]
    assert result["already_in_current_timetable"] == [
        {
            "course_code": "AI463",
            "course_name": "Concrete AI463",
            "credit_hours": 3,
            "match_kind": "ELECTIVE_ALIAS",
            "evidence_course_codes": ["AI1"],
        }
    ]
    assert result["already_in_expected_plan"] == [
        {
            "course_code": "AI464",
            "course_name": "Concrete AI464",
            "credit_hours": 3,
            "match_kind": "ELECTIVE_ALIAS",
            "evidence_course_codes": ["AI2"],
        }
    ]
    assert result["current_registered_credit_hours"] == 3


def test_my_progress_overlays_registered_but_not_expected_as_studying(
    production_shape: Student,
) -> None:
    from core.services.virtual_advisor_capabilities import _exec_my_progress

    result = _exec_my_progress({}, _scope(), _ctx())

    assert result["registered_requirement_course_codes"] == ["AI1"]
    assert result["expected_plan_course_codes"] == ["AI2"]
    assert result["counts"]["studying"] == 1
    assert "AI2" in result["elective_slots"]
    assert "AI1" not in result["elective_slots"]
    assert "AI331" in {row["code"] for row in result["prerequisites_satisfied"]}


def test_my_plan_by_term_uses_the_same_registered_only_status_overlay(
    production_shape: Student,
) -> None:
    from core.services.virtual_advisor_capabilities import _exec_my_plan_by_term

    result = _exec_my_plan_by_term({}, _scope(), _ctx())
    statuses = {
        row["course_code"]: row["status"] for level in result["terms"] for row in level["courses"]
    }

    assert statuses["AI1"] == "studying"
    assert statuses["AI2"] == "not_taken"
    assert result["registered_requirement_course_codes"] == ["AI1"]
    assert result["expected_plan_course_codes"] == ["AI2"]
    assert result["summary"]["studying"] == 1


def test_student_context_reconciles_readiness_and_recommendations_without_merging_expected(
    production_shape: Student,
) -> None:
    from core.services.virtual_advisor import build_verified_student_context

    context = build_verified_student_context(
        student_id=SID,
        academic_year=int(YEAR),
        term=int(TERM),
    )
    evidence = context["course_evidence"]

    assert evidence["studying"] == ["AI1"]
    assert evidence["registered_requirement_course_codes"] == ["AI1"]
    assert evidence["expected_plan_course_codes"] == ["AI2"]
    assert "AI463" in evidence["registered_or_equivalent_course_codes"]
    assert "AI464" in evidence["expected_plan_requirement_aliases"]
    remaining = {row["course_code"] for row in evidence["remaining_requirements"]}
    assert "AI1" not in remaining
    assert "AI2" in remaining
    assert [row["course_code"] for row in context["recommendations"]] == ["AI331"]
    assert context["recommendation_suppression"] == {
        "already_registered_or_equivalent": ["AI463"],
        "already_expected_or_equivalent": ["AI464"],
    }


def test_remote_qwen_projection_keeps_alias_provenance_and_expected_separation(
    production_shape: Student,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor import build_verified_student_context
    from core.services.virtual_advisor_capabilities import (
        _exec_my_progress,
        _exec_recommend_courses,
    )

    identities = RemoteIdentityMap()
    recommendation = PROJECTORS["recommend_courses"](
        _exec_recommend_courses({}, _scope(), _ctx()), identities
    )
    progress = PROJECTORS["my_progress"](_exec_my_progress({}, _scope(), _ctx()), identities)
    context = PROJECTORS["get_student_context"](
        {
            "ok": True,
            "student_context": build_verified_student_context(
                student_id=SID,
                academic_year=int(YEAR),
                term=int(TERM),
            ),
        },
        identities,
    )["student_context"]

    assert recommendation["already_in_current_timetable"][0]["match_kind"] == ("ELECTIVE_ALIAS")
    assert recommendation["already_in_current_timetable"][0]["evidence_course_codes"] == ["AI1"]
    assert recommendation["already_in_expected_plan"][0]["evidence_course_codes"] == ["AI2"]
    assert progress["registered_requirement_course_codes"] == ["AI1"]
    assert progress["expected_plan_course_codes"] == ["AI2"]
    assert context["course_evidence"]["studying"] == ["AI1"]
    assert context["course_evidence"]["expected_plan_course_codes"] == ["AI2"]


def test_sibling_elective_suppression_names_the_registered_concrete_course(
    production_shape: Student,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI463 occupying AI1 must give AI465 concrete registrar provenance."""
    from core.services.virtual_advisor_capabilities import _exec_recommend_courses

    StudentTermSection.objects.filter(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        source="scraper_timetable",
    ).delete()
    registered_concrete = TermSection.objects.create(
        course_code="AI463",
        course_number="AI463",
        course_key="AI463",
        course_name="Natural Language Processing",
        section="M6",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=registered_concrete,
        source="scraper_timetable",
    )
    sibling = ElectiveCourse.objects.create(
        course_code="AI465",
        course_name="Sibling elective",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
    )
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=int(TERM),
        programme="AI",
        placeholder_code="AI1",
        elective=sibling,
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: ["AI465"],
    )

    result = _exec_recommend_courses({}, _scope(), _ctx())

    assert result["recommendations"] == []
    assert result["already_in_current_timetable"] == [
        {
            "course_code": "AI465",
            "course_name": "Sibling elective",
            "credit_hours": 3,
            "match_kind": "ELECTIVE_ALIAS",
            "evidence_course_codes": ["AI463"],
        }
    ]


def test_context_uses_latest_registered_term_and_requested_expected_term_separately(
    production_shape: Student,
) -> None:
    from core.services.virtual_advisor import build_verified_student_context

    StudentTermSection.objects.filter(
        student_id=SID,
        source="scraper_timetable",
    ).update(academic_year="1447", term="2")
    concrete = ElectiveCourse.objects.get(programme="AI", course_code="AI463")
    ElectiveTermMapping.objects.create(
        academic_year="1447",
        term=2,
        programme="AI",
        placeholder_code="AI1",
        elective=concrete,
    )

    context = build_verified_student_context(
        student_id=SID,
        academic_year=int(YEAR),
        term=int(TERM),
    )
    evidence = context["course_evidence"]

    assert evidence["current_term_registrations"]["academic_year"] == "1447"
    assert evidence["current_term_registrations"]["term"] == "2"
    assert evidence["studying"] == ["AI1"]
    assert evidence["registered_requirement_course_codes"] == ["AI1"]
    assert evidence["expected_plan_course_codes"] == ["AI2"]
    assert "AI2" not in evidence["studying"]
    assert [row["course_code"] for row in context["recommendations"]] == ["AI331"]
    assert context["recommendation_suppression"] == {
        "already_registered_or_equivalent": ["AI463"],
        "already_expected_or_equivalent": ["AI464"],
    }


def test_unmapped_catalogue_elective_keeps_known_credits(
    production_shape: Student,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.services.academic_state import MetadataSource, build_student_academic_state
    from core.services.virtual_advisor_capabilities import _exec_recommend_courses

    ElectiveCourse.objects.create(
        course_code="AI499",
        course_name="Unmapped free elective",
        programme="AI",
        category="Free Elective",
        credit_hours=3,
    )
    section = TermSection.objects.create(
        course_code="AI499",
        course_number="AI499",
        course_key="AI499",
        course_name="Imported elective section",
        section="M9",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="scraper_timetable",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: ["AI499"],
    )

    state = build_student_academic_state(SID, YEAR, TERM)
    fact = state.course("AI499")
    result = _exec_recommend_courses({}, _scope(), _ctx())

    assert fact is not None
    assert fact.metadata.source is MetadataSource.ELECTIVE_CATALOGUE
    assert fact.metadata.credit_hours == 3
    assert result["already_in_current_timetable"][0]["credit_hours"] == 3
    assert result["current_registered_credit_hours"] == 6


def test_incomplete_programme_profile_preserves_existing_tool_failure_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.services.virtual_advisor import build_verified_student_context
    from core.services.virtual_advisor_capabilities import (
        _exec_my_plan_by_term,
        _exec_my_progress,
        _exec_recommend_courses,
    )

    student_id = SID + 99
    Student.objects.create(
        student_id=student_id,
        registration_no=str(student_id),
        name="Incomplete profile",
        program="",
        section="M",
    )
    scope = {"role": ROLE_STUDENT, "student_id": student_id}
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "core.services.virtual_advisor.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    recommendation = _exec_recommend_courses({}, scope, _ctx())
    progress = _exec_my_progress({}, scope, _ctx())
    plan = _exec_my_plan_by_term({}, scope, _ctx())
    context = build_verified_student_context(
        student_id=student_id,
        academic_year=int(YEAR),
        term=int(TERM),
    )

    assert recommendation["ok"] is False
    assert "no programme" in recommendation["error"]
    assert progress["ok"] is False
    assert "no programme" in progress["error"]
    assert plan == {
        "ok": False,
        "error": f"No degree plan found for student {student_id}.",
    }
    assert context["mode"] == "student"
    assert context["course_evidence"]["studying"] == []
    assert context["course_evidence"]["registered_requirement_course_codes"] == []
    assert context["course_evidence"]["expected_plan_course_codes"] == []


def test_lookup_and_prerequisites_resolve_concrete_elective_in_requested_term(
    production_shape: Student,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor_capabilities import (
        _exec_course_prerequisites,
        _exec_lookup_course,
    )

    future = ElectiveCourse.objects.create(
        course_code="AI466",
        course_name="Future elective",
        programme="AI",
        category="Program Elective",
        credit_hours=3,
        prerequisites_csv="AI331",
    )
    ElectiveTermMapping.objects.create(
        academic_year=YEAR,
        term=2,
        programme="AI",
        placeholder_code="AI1",
        elective=future,
    )

    lookup = _exec_lookup_course({"query": "AI463", "program": "AI"}, _scope(), _ctx())
    concrete = _exec_course_prerequisites({"course_code": "AI463"}, _scope(), _ctx())
    slot = _exec_course_prerequisites({"course_code": "AI1"}, _scope(), _ctx())

    assert lookup["courses"] == [
        {
            "course_code": "AI463",
            "course_name": "Concrete AI463",
            "credit_hours": 3,
            "programs": ["AI"],
            "fulfills_elective_slots": ["AI1"],
        }
    ]
    assert concrete["is_concrete_elective"] is True
    assert concrete["per_program"][0]["fulfills_elective_slots"] == ["AI1"]
    assert [row["course_code"] for row in slot["options"]] == ["AI463"]
    projected_lookup = PROJECTORS["lookup_course"](lookup, RemoteIdentityMap())
    projected_concrete = PROJECTORS["course_prerequisites"](concrete, RemoteIdentityMap())
    assert projected_lookup["courses"][0]["fulfills_elective_slots"] == ["AI1"]
    assert projected_concrete["per_program"][0]["fulfills_elective_slots"] == ["AI1"]


def test_why_course_locked_uses_registered_alias_but_keeps_expected_separate(
    production_shape: Student,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor_capabilities import _exec_why_course_locked

    placeholder = _exec_why_course_locked({"course_code": "AI1"}, _scope(), _ctx())
    concrete = _exec_why_course_locked({"course_code": "AI463"}, _scope(), _ctx())
    expected = _exec_why_course_locked({"course_code": "AI464"}, _scope(), _ctx())

    assert placeholder["status"] == "studying"
    assert placeholder["registered_evidence_course_codes"] == ["AI1"]
    assert concrete["status"] == "studying"
    assert concrete["requirement_course_code"] == "AI1"
    assert concrete["registered_evidence_course_codes"] == ["AI1"]
    assert expected["status"] == "EXPECTED_PLAN_ONLY"
    assert expected["expected_plan_evidence_course_codes"] == ["AI2"]
    assert expected["registered_evidence_course_codes"] == []
    projected = PROJECTORS["why_course_locked"](concrete, RemoteIdentityMap())
    assert projected["requirement_course_code"] == "AI1"
    assert projected["registered_evidence_course_codes"] == ["AI1"]


def test_course_comparison_classifies_registered_concrete_alias_as_studying(
    production_shape: Student,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor_capabilities import _exec_course_choice_comparison

    result = _exec_course_choice_comparison({"course_codes": ["AI463", "AI331"]}, _scope(), _ctx())
    by_code = {row["course_code"]: row for row in result["candidates"]}

    assert by_code["AI463"]["kind"] == "COURSE"
    assert by_code["AI463"]["requirement_course_code"] == "AI1"
    assert by_code["AI463"]["academic_status"] == "studying"
    assert by_code["AI463"]["recommendation"]["state"] == ("ALREADY_IN_CURRENT_TIMETABLE")
    assert by_code["AI463"]["graduation"]["status"] == "ALREADY_STUDYING"
    assert by_code["AI331"]["academic_status"] == "open_now"
    projected = PROJECTORS["course_choice_comparison"](result, RemoteIdentityMap())
    projected_by_code = {row["course_code"]: row for row in projected["candidates"]}
    assert projected_by_code["AI463"]["requirement_course_code"] == "AI1"
    assert projected_by_code["AI463"]["academic_status"] == "studying"


def test_replacement_rejects_placeholder_to_concrete_alias_as_no_academic_change(
    production_shape: Student,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor_capabilities import (
        _exec_feasible_course_replacements,
    )

    result = _exec_feasible_course_replacements(
        {"remove_course": "AI1", "add_course": "AI463"},
        _scope(),
        _ctx(),
    )

    assert result["status"] == "NO_ACADEMIC_CHANGE"
    assert result["certified_replacements"] == []
    academic = result["rejected_replacements"][0]["academic"]
    assert academic["reason_code"] == "SAME_ACADEMIC_REQUIREMENT"
    assert academic["requirement_course_codes"] == ["AI1"]
    projected = PROJECTORS["feasible_course_replacements"](result, RemoteIdentityMap())
    projected_academic = projected["rejected_replacements"][0]["academic"]
    assert projected_academic["reason_code"] == "SAME_ACADEMIC_REQUIREMENT"
    assert projected_academic["requirement_course_codes"] == ["AI1"]


def test_timetable_proposal_suppresses_registered_and_expected_recommendation_aliases(
    production_shape: Student,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.services.llm_remote_privacy import PROJECTORS, RemoteIdentityMap
    from core.services.virtual_advisor_capabilities import _exec_build_timetable_proposal

    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: ["AI463", "AI464"],
    )
    monkeypatch.setattr(
        "core.services.student_planner.build_student_options",
        lambda *_args, **_kwargs: {
            "alternatives": [],
            "unplaced": [],
            "constraint_failures": [],
            "generated": 0,
        },
    )

    result = _exec_build_timetable_proposal({}, _scope(), _ctx())

    assert result["system_recommended_courses"] == []
    assert result["system_recommendations_suppressed_registered"] == ["AI463"]
    assert result["system_recommendations_suppressed_expected"] == ["AI464"]
    assert result["no_additional_courses"] is True
    projected = PROJECTORS["build_timetable_proposal"](result, RemoteIdentityMap())
    assert projected["system_recommendations_suppressed_registered"] == ["AI463"]
    assert projected["system_recommendations_suppressed_expected"] == ["AI464"]
