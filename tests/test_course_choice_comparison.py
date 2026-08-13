from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.course_choice_comparison import (
    _graduation_evidence,
    _timetable_evidence,
    compare_course_choices,
)
from core.services.planner_builder import Meeting, _catalog_for_courses, build_plans

pytestmark = pytest.mark.django_db

SID = 4610192


def _catalog_section(
    *,
    course_code: str,
    section: str,
    meetings: list[Meeting],
    term_section_id: int = 1,
) -> dict[str, Any]:
    return {
        "term_section_id": term_section_id,
        "course_code": course_code,
        "course_key": course_code,
        "section": section,
        "meetings": meetings,
    }


def _graduation_result(terms: int | None = 4) -> dict[str, Any]:
    completed = terms is not None
    return {
        "simulation_completed": completed,
        "estimated_additional_terms": terms,
        "lower_bound_additional_terms": 3,
        "unresolved_requirements": [] if completed else [{"code": "AI499"}],
        "what_if": {"valid": True},
    }


def _install_public_service_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: list[dict[str, Any]] | None = None,
    dependents: dict[str, dict[str, Any]] | None = None,
    importance: dict[str, float] | None = None,
    timetable: dict[str, dict[str, Any]] | None = None,
    graduation_terms: dict[str, int | None] | None = None,
    use_real_timetable: bool = False,
) -> None:
    codes = ("AI331", "DS341")
    baseline_rows = list(baseline or [])
    monkeypatch.setattr(
        "core.services.course_choice_comparison.get_student_term_baseline",
        lambda *_args: baseline_rows,
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.build_unlock_report",
        lambda *_args, **_kwargs: {
            "program": "AI",
            "dependents": dependents
            or {
                "AI331": {"waiting_only_on_this": ["AI410", "AI420"], "on_chain_of_count": 3},
                "DS341": {"waiting_only_on_this": ["DS410"], "on_chain_of_count": 1},
            },
            "locked_courses": [],
        },
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.recommend_next_courses",
        lambda *_args, **_kwargs: list(codes),
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.program_downstream_importance_scores",
        lambda _program: importance or {"AI331": 4.0, "DS341": 1.0},
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.build_course_detail",
        lambda _sid, code, **_kwargs: {
            "kind": "COURSE",
            "course_code": code,
            "course_name": f"Course {code}",
            "credit_hours": 3,
            "your_status": "open_now",
        },
    )
    if timetable is not None:
        monkeypatch.setattr(
            "core.services.course_choice_comparison._timetable_evidence",
            lambda **_kwargs: timetable,
        )
    elif not use_real_timetable:
        monkeypatch.setattr(
            "core.services.course_choice_comparison._timetable_evidence",
            lambda **_kwargs: {
                "AI331": {
                    "status": "OK",
                    "sections_on_file": 2,
                    "clash_free_count": 1,
                    "clashing_count": 1,
                    "baseline_sections": [],
                },
                "DS341": {
                    "status": "OK",
                    "sections_on_file": 1,
                    "clash_free_count": 1,
                    "clashing_count": 0,
                    "baseline_sections": [],
                },
            },
        )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.build_graduation_report",
        lambda *_args, **_kwargs: {
            "program": "AI",
            "planning_baseline_courses_assumed_passed": [],
        },
    )
    terms_by_code = graduation_terms or {"AI331": 3, "DS341": 4}

    def fake_what_if(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        added = kwargs.get("add_current_courses") or []
        code = added[0] if added else "AI331"
        return _graduation_result(terms_by_code[code])

    monkeypatch.setattr(
        "core.services.course_choice_comparison.build_graduation_what_if",
        fake_what_if,
    )


@pytest.mark.parametrize(
    ("codes", "objective", "message"),
    [
        (["AI331"], "balanced", "two to four"),
        (["AI331", "AI 331"], "balanced", "different"),
        (["AI331", "DS341"], "fastest", "objective"),
    ],
)
def test_comparison_rejects_ambiguous_inputs(
    codes: list[str], objective: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        compare_course_choices(SID, codes, 1448, 1, objective=objective)


def test_comparison_keeps_dimensions_separate_and_has_no_mega_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(monkeypatch)

    result = compare_course_choices(
        SID,
        ["ai 331", "ds341"],
        1448,
        1,
        objective="unlock_impact",
    )

    assert result["ok"] is True
    assert result["verdict"] == "PREFERRED"
    assert result["preferred_course"] == "AI331"
    first = result["candidates"][0]
    assert first["impact"] == {
        "direct_unlock_count": 2,
        "chain_course_count": 3,
        "weighted_downstream_score": 4.0,
        "weighted_score_method": "sum_inverse_distance",
    }
    assert first["recommendation"] == {"state": "RECOMMENDED", "rank": 1}
    assert first["timetable"]["clash_free_count"] == 1
    assert first["graduation"]["estimated_additional_terms"] == 3
    assert "score" not in result
    assert all("score" not in row for row in result["candidates"])


def test_conflicting_unlock_dimensions_do_not_get_summed_into_a_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(
        monkeypatch,
        dependents={
            "AI331": {"waiting_only_on_this": ["AI410", "AI420"], "on_chain_of_count": 2},
            "DS341": {"waiting_only_on_this": ["DS410"], "on_chain_of_count": 4},
        },
        importance={"AI331": 2.0, "DS341": 5.0},
    )

    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1448,
        1,
        objective="unlock_impact",
    )

    assert result["verdict"] == "NOT_DETERMINABLE"
    assert result["preferred_course"] is None
    assert result["decision_basis"] == ["conflicting_unlock_dimensions"]


def test_timetable_objective_fails_closed_when_one_course_is_not_on_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(
        monkeypatch,
        timetable={
            "AI331": {
                "status": "OK",
                "sections_on_file": 2,
                "clash_free_count": 2,
                "clashing_count": 0,
                "baseline_sections": [],
            },
            "DS341": {
                "status": "NOT_ON_FILE",
                "sections_on_file": 0,
                "clash_free_count": 0,
                "clashing_count": 0,
                "baseline_sections": [],
            },
        },
    )

    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1448,
        1,
        objective="timetable_fit",
    )

    assert result["criterion_leaders"]["timetable_fit"] == []
    assert result["verdict"] == "NOT_DETERMINABLE"
    assert result["decision_basis"] == ["timetable_evidence_incomplete"]


def test_graduation_objective_uses_only_complete_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(
        monkeypatch,
        graduation_terms={"AI331": 3, "DS341": None},
    )

    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1448,
        1,
        objective="graduation",
    )

    assert result["criterion_leaders"]["graduation_terms"] == []
    assert result["verdict"] == "NOT_DETERMINABLE"
    assert result["decision_basis"] == ["graduation_forecast_incomplete"]


def test_mixed_baseline_disables_only_timetable_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mixed = [
        {"course_code": "AI100", "section": "M1", "source": "mapped"},
        {
            "course_code": "AI200",
            "section": "M2",
            "source": "registration_plan_import",
        },
    ]
    _install_public_service_fakes(
        monkeypatch,
        baseline=mixed,
        use_real_timetable=True,
    )

    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1448,
        1,
        objective="unlock_impact",
    )

    assert result["baseline_kind"] == "MIXED_REVIEW_REQUIRED"
    assert {row["academic_status"] for row in result["candidates"]} == {"open_now"}
    assert {row["timetable"]["status"] for row in result["candidates"]} == {
        "MIXED_BASELINE_REVIEW_REQUIRED"
    }
    assert result["verdict"] == "PREFERRED"


def test_candidate_without_meetings_is_not_reported_clash_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [_catalog_section(course_code="AI331", section="M1", meetings=[])]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[],
        baseline_kind="EMPTY",
    )["AI331"]

    assert evidence["status"] == "NOT_DETERMINABLE"
    assert evidence["reason_code"] == "CANDIDATE_MEETING_DATA_INCOMPLETE"
    assert evidence["clash_free_count"] is None
    assert evidence["details"][0]["reason_codes"] == ["MISSING_MEETING_DATA"]


def test_timetable_objective_fails_closed_for_incomplete_candidate_meetings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(
        monkeypatch,
        use_real_timetable=True,
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [_catalog_section(course_code="AI331", section="M1", meetings=[])],
            "DS341": [
                _catalog_section(
                    course_code="DS341",
                    section="M1",
                    meetings=[Meeting(day="MON", start="09:00", end="10:15")],
                )
            ],
        },
    )

    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1448,
        1,
        objective="timetable_fit",
    )

    assert result["criterion_leaders"]["timetable_fit"] == []
    assert result["verdict"] == "NOT_DETERMINABLE"
    assert result["decision_basis"] == ["timetable_evidence_incomplete"]
    assert result["candidates"][0]["timetable"]["reason_code"] == (
        "CANDIDATE_MEETING_DATA_INCOMPLETE"
    )


@pytest.mark.parametrize(
    ("meeting", "reason_code"),
    [
        (Meeting(day="NODAY", start="09:00", end="10:15"), "INVALID_DAY"),
        (Meeting(day="MON", start="09:99", end="10:15"), "INVALID_TIME"),
        (Meeting(day="MON", start="10:15", end="09:00"), "INVALID_TIME_RANGE"),
    ],
)
def test_malformed_candidate_meeting_is_not_reported_clash_free(
    monkeypatch: pytest.MonkeyPatch,
    meeting: Meeting,
    reason_code: str,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [
                _catalog_section(
                    course_code="AI331",
                    section="M1",
                    meetings=[meeting],
                )
            ]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[],
        baseline_kind="EMPTY",
    )["AI331"]

    assert evidence["status"] == "NOT_DETERMINABLE"
    assert evidence["reason_code"] == "CANDIDATE_MEETING_DATA_INCOMPLETE"
    assert evidence["details"][0]["reason_codes"] == [reason_code]


def test_incomplete_baseline_meeting_is_not_silently_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [
                _catalog_section(
                    course_code="AI331",
                    section="M1",
                    meetings=[Meeting(day="MON", start="09:00", end="10:15")],
                )
            ]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[
            {
                "course_code": "CS285",
                "section": "M3",
                "day": "",
                "start_time": "",
                "end_time": "",
            }
        ],
        baseline_kind="REGISTERED",
    )["AI331"]

    assert evidence["status"] == "NOT_DETERMINABLE"
    assert evidence["reason_code"] == "BASELINE_MEETING_DATA_INCOMPLETE"
    assert evidence["clash_free_count"] is None
    assert evidence["details"] == [
        {
            "course_code": "CS285",
            "section": "M3",
            "reason_code": "MISSING_MEETING_DATA",
        }
    ]


def test_incomplete_baseline_for_same_course_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [
                _catalog_section(
                    course_code="AI331",
                    section="M2",
                    meetings=[Meeting(day="MON", start="09:00", end="10:15")],
                )
            ]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[
            {
                "course_code": "AI331",
                "section": "M1",
                "day": "",
                "start_time": "",
                "end_time": "",
            }
        ],
        baseline_kind="REGISTERED",
    )["AI331"]

    assert evidence["status"] == "NOT_DETERMINABLE"
    assert evidence["reason_code"] == "BASELINE_MEETING_DATA_INCOMPLETE"


def test_complete_candidate_and_baseline_meetings_still_produce_clash_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [
                _catalog_section(
                    course_code="AI331",
                    section="M1",
                    meetings=[Meeting(day="MON", start="09:00", end="10:15")],
                    term_section_id=1,
                ),
                _catalog_section(
                    course_code="AI331",
                    section="M2",
                    meetings=[Meeting(day="TUE", start="09:00", end="10:15")],
                    term_section_id=2,
                ),
            ]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[
            {
                "course_code": "CS285",
                "section": "M3",
                "day": "MON",
                "start_time": "09:30",
                "end_time": "10:40",
            }
        ],
        baseline_kind="REGISTERED",
    )["AI331"]

    assert evidence["status"] == "OK"
    assert evidence["sections_on_file"] == 2
    assert evidence["clash_free_count"] == 1
    assert evidence["clashing_count"] == 1


def test_valid_dictionary_meeting_shape_is_compared_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    section = _catalog_section(course_code="AI331", section="M1", meetings=[])
    section["meetings"] = [{"day": "MON", "start": "09:00", "end": "10:15"}]
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {"AI331": [section]},
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[],
        baseline_kind="EMPTY",
    )["AI331"]

    assert evidence["status"] == "OK"
    assert evidence["clash_free_count"] == 1


@pytest.mark.parametrize("day", ["MONSTER", "Monday typo"])
def test_day_prefix_is_not_accepted_as_a_valid_meeting_day(
    monkeypatch: pytest.MonkeyPatch,
    day: str,
) -> None:
    monkeypatch.setattr(
        "core.services.course_choice_comparison.student_gender_strict",
        lambda _student_id: "M",
    )
    monkeypatch.setattr(
        "core.services.course_choice_comparison._catalog_for_courses",
        lambda *_args, **_kwargs: {
            "AI331": [
                _catalog_section(
                    course_code="AI331",
                    section="M1",
                    meetings=[Meeting(day=day, start="09:00", end="10:15")],
                )
            ]
        },
    )

    evidence = _timetable_evidence(
        student_id=SID,
        program="AI",
        codes=["AI331"],
        academic_year=1448,
        term=1,
        baseline=[],
        baseline_kind="EMPTY",
    )["AI331"]

    assert evidence["status"] == "NOT_DETERMINABLE"
    assert evidence["details"][0]["reason_codes"] == ["INVALID_DAY"]


def test_cross_term_comparison_suppresses_current_section_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_public_service_fakes(monkeypatch)
    result = compare_course_choices(
        SID,
        ["AI331", "DS341"],
        1447,
        2,
        objective="timetable_fit",
        timetable_evidence_available=False,
    )

    assert result["verdict"] == "NOT_DETERMINABLE"
    assert result["criterion_leaders"]["timetable_fit"] == []
    assert {row["timetable"]["reason_code"] for row in result["candidates"]} == {
        "SECTION_SNAPSHOT_TERM_MISMATCH"
    }


def test_graduation_scenarios_share_one_non_candidate_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], list[str]]] = []

    def fake_what_if(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            (
                list(kwargs["remove_current_courses"]),
                list(kwargs["add_current_courses"]),
            )
        )
        return _graduation_result(4)

    monkeypatch.setattr(
        "core.services.course_choice_comparison.build_graduation_what_if",
        fake_what_if,
    )
    _graduation_evidence(
        student_id=SID,
        codes=["AI331", "DS341"],
        academic_year=1448,
        term=1,
        baseline_report={
            "planning_baseline_courses_assumed_passed": [
                {"code": "AI331"},
                {"code": "DS341"},
                {"code": "CS285"},
            ]
        },
        candidate_meta={
            "AI331": ("COURSE", "studying"),
            "DS341": ("COURSE", "studying"),
        },
    )

    assert calls == [(["DS341"], []), (["AI331"], [])]


def test_real_service_executes_only_read_queries() -> None:
    Student.objects.create(
        student_id=SID,
        name="Read only",
        program="AI",
        section="M",
        total_earned_credits=0,
        current_registered_credits=0,
    )
    for code in ("AI331", "DS341"):
        course = Course.objects.create(
            course_code=code,
            description=f"Course {code}",
            credit_hours=3,
        )
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=f"Course {code}",
            credit_hours=3,
            programme_term=1,
            type="Mandatory",
        )
        StudentCourse.objects.create(
            student_id=SID,
            course=course,
            status="not_taken",
            programme_term=1,
        )

    with CaptureQueriesContext(connection) as captured:
        result = compare_course_choices(SID, ["AI331", "DS341"], 1448, 1)

    mutating = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
    ]
    assert result["ok"] is True
    assert mutating == []


def test_real_comparison_batches_prerequisites_instead_of_querying_each_course() -> None:
    Student.objects.create(
        student_id=SID,
        name="Query bounded",
        program="QB",
        section="M",
        total_earned_credits=0,
        current_registered_credits=0,
    )
    for index, code in enumerate(("QB101", "QB102", "QB201", "QB202"), start=1):
        course = Course.objects.create(
            course_code=code,
            description=f"Course {code}",
            credit_hours=3,
        )
        ProgrammeRequirement.objects.create(
            program="QB",
            course_code=code,
            course_name=f"Course {code}",
            credit_hours=3,
            programme_term=index,
            type="Mandatory",
        )
        StudentCourse.objects.create(
            student_id=SID,
            course=course,
            status="not_taken",
            programme_term=index,
        )

    with CaptureQueriesContext(connection) as captured:
        result = compare_course_choices(SID, ["QB101", "QB102"], 1448, 1)

    prerequisite_queries = [
        query["sql"]
        for query in captured.captured_queries
        if 'FROM "prerequisites"' in query["sql"]
    ]
    assert result["ok"] is True
    # The whole comparison may build a baseline plus two what-if forecasts, but
    # programme prerequisites are snapshots, not an N-per-course inner loop.
    assert len(prerequisite_queries) <= 10
    assert len(captured.captured_queries) <= 80


def test_catalog_loads_all_section_meetings_in_one_query() -> None:
    for index in range(5):
        section = TermSection.objects.create(
            source_tag="test",
            course_code="AI",
            course_number="331",
            course_key="AI331",
            section=f"M{index + 1}",
            course_name="Course AI331",
        )
        TermSectionProgram.objects.create(
            term_section=section,
            program="AI",
            assignment_source="manual",
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="MON",
            start_time=f"{8 + index:02d}:00",
            end_time=f"{8 + index:02d}:50",
        )

    with CaptureQueriesContext(connection) as captured:
        catalog = _catalog_for_courses("1448", "1", ["AI331"], "M", "AI")

    assert len(catalog["AI331"]) == 5
    assert all(len(section["meetings"]) == 1 for section in catalog["AI331"])
    meeting_queries = [
        query["sql"]
        for query in captured.captured_queries
        if "TERM_SECTION_MEETINGS" in query["sql"].upper()
    ]
    assert len(meeting_queries) == 1
    # Catalogue query count stays constant as section count grows: one section
    # query plus one batched meeting query, not one meeting query per section.
    assert len(captured.captured_queries) <= 2


def test_catalog_preserves_partial_missing_meeting_evidence() -> None:
    section = TermSection.objects.create(
        source_tag="test",
        course_code="AI",
        course_number="331",
        course_key="AI331",
        section="M1",
        course_name="Course AI331",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="manual",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="WED",
        start_time="",
        end_time="",
    )

    catalog = _catalog_for_courses("1448", "1", ["AI331"], "M", "AI")

    row = catalog["AI331"][0]
    assert len(row["meetings"]) == 1
    assert row["meeting_issue_codes"] == ["MISSING_MEETING_DATA"]


def test_catalog_marks_a_section_with_no_meeting_rows_as_incomplete() -> None:
    section = TermSection.objects.create(
        source_tag="test",
        course_code="AI",
        course_number="331",
        course_key="AI331",
        section="M1",
        course_name="Course AI331",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="manual",
    )

    catalog = _catalog_for_courses("1448", "1", ["AI331"], "M", "AI")

    row = catalog["AI331"][0]
    assert row["meetings"] == []
    assert row["meeting_issue_codes"] == ["MISSING_MEETING_DATA"]


def test_planner_does_not_schedule_a_section_with_partial_meeting_data() -> None:
    section = TermSection.objects.create(
        source_tag="test",
        course_code="AI",
        course_number="331",
        course_key="AI331",
        section="M1",
        course_name="Course AI331",
    )
    TermSectionProgram.objects.create(
        term_section=section,
        program="AI",
        assignment_source="manual",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="09:00",
        end_time="10:15",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="WED",
        start_time="",
        end_time="",
    )

    result = build_plans(
        year="1448",
        term="1",
        shortlist=[{"course_code": "AI331", "credits": 3, "must_take": True}],
        baseline=[],
        keep_registered=False,
        strict_per_course=False,
        consider_capacity=False,
        max_credits=18,
        gender="M",
        program="AI",
        require_complete_meetings=True,
    )

    assert result["options"] == []
    assert result["unscheduled"][0]["course_code"] == "AI331"
    assert result["unscheduled"][0]["reason"] == ("Section meeting data is incomplete or invalid")
