from __future__ import annotations

import json
from typing import Any

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.llm_remote_privacy import (
    RemoteIdentityMap,
    project_tool_result_for_remote,
)
from core.services.rbac import ROLE_STUDENT
from core.services.student_sections import (
    get_student_term_baseline,
    replace_student_term_sections,
)
from core.services.timetable_snapshots import Snapshot
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

COURSE_CODE = "BR901"


@pytest.fixture
def branch_section_world() -> dict[str, Any]:
    """Production-shaped local and other-branch rows for one plan course.

    Student.section is the authoritative local cohort.  M/F are this branch;
    YM/YF are deliberately mixed between complete and zero-meeting rows so an
    exclusion implemented only while iterating meetings cannot pass this suite.
    """
    students = {
        "M": Student.objects.create(
            student_id=9701901,
            registration_no="9701901",
            name="Male branch-isolation student",
            program="AI",
            section="M",
            status="active",
        ),
        "F": Student.objects.create(
            student_id=9701902,
            registration_no="9701902",
            name="Female branch-isolation student",
            program="AI",
            section="F",
            status="active",
        ),
    }
    Course.objects.create(
        course_code=COURSE_CODE,
        description="Branch isolation test course",
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code=COURSE_CODE,
        course_name="Branch isolation test course",
        type="Mandatory",
        programme_term=1,
        credit_hours=3,
    )

    sections: dict[str, TermSection] = {}

    def make_section(label: str, meeting: tuple[str, str, str] | None) -> None:
        section = TermSection.objects.create(
            course_code=COURSE_CODE,
            course_number=COURSE_CODE,
            course_key=COURSE_CODE,
            course_name="Branch isolation test course",
            section=label,
            available_capacity=30,
            registered_count=0,
        )
        TermSectionProgram.objects.create(term_section=section, program="AI")
        if meeting is not None:
            day, start, end = meeting
            TermSectionMeeting.objects.create(
                term_section=section,
                day=day,
                start_time=start,
                end_time=end,
            )
        sections[label] = section

    # The only certifiable local choice for each cohort.
    make_section("M1", ("SUN", "09:00", "10:15"))
    make_section("F1", ("MON", "09:00", "10:15"))
    # Correct-cohort records with no meeting evidence must not be called
    # clash-free or selected in a proposal.
    make_section("M0", None)
    make_section("F0", None)
    # Other-branch rows must be excluded before meeting completeness matters.
    make_section("YM4", ("TUE", "11:00", "12:15"))
    make_section("YM5", None)
    make_section("YF4", ("WED", "11:00", "12:15"))
    make_section("YF5", None)

    return {"students": students, "sections": sections}


def _execute(student: Student, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return get_default_registry().execute(
        tool,
        args,
        scope={"role": ROLE_STUDENT, "student_id": student.student_id},
        ctx={"academic_year": 1448, "term": 1},
    )


def _section_labels(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("section") or "").strip().upper() for row in rows}


@pytest.mark.parametrize(("cohort", "expected"), [("M", "M1"), ("F", "F1")])
def test_advisor_section_lookup_uses_student_cohort_and_drops_other_branch(
    branch_section_world: dict[str, Any], cohort: str, expected: str
) -> None:
    student = branch_section_world["students"][cohort]

    result = _execute(
        student,
        "my_clash_free_sections",
        {"course_code": COURSE_CODE},
    )

    assert result["ok"] is True, result
    course = result["courses"][0]
    assert _section_labels(course["clash_free"]) == {expected}
    assert course["clashing"] == []

    # In particular, neither a YM/YF row with meetings nor one with zero
    # meetings may leak through as an apparently all-day-free option.
    surfaced = json.dumps(course, ensure_ascii=False)
    assert "YM4" not in surfaced
    assert "YM5" not in surfaced
    assert "YF4" not in surfaced
    assert "YF5" not in surfaced
    assert f"{cohort}0" not in _section_labels(course["clash_free"])


@pytest.mark.parametrize(("cohort", "expected"), [("M", "M1"), ("F", "F1")])
def test_timetable_proposal_selects_only_complete_local_cohort_sections(
    branch_section_world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    cohort: str,
    expected: str,
) -> None:
    student = branch_section_world["students"][cohort]
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    result = _execute(
        student,
        "build_timetable_proposal",
        {
            "mode": "from_scratch",
            "course_codes": [COURSE_CODE],
            "must_take_courses": [COURSE_CODE],
            "max_credits": 18,
        },
    )

    assert result["ok"] is True, result
    assert result["constraints_satisfied"] is True, result
    assert result["alternatives"], result
    selected = {
        str(row.get("section") or "").strip().upper()
        for alternative in result["alternatives"]
        for row in alternative.get("courses") or []
        if row.get("course_code") == COURSE_CODE
    }
    assert selected == {expected}

    projected = project_tool_result_for_remote(
        "build_timetable_proposal", result, RemoteIdentityMap()
    )
    projected_selected = {
        str(row.get("section") or "").strip().upper()
        for alternative in projected.get("alternatives") or []
        for row in alternative.get("courses") or []
        if row.get("course_code") == COURSE_CODE
    }
    assert projected_selected == {expected}


@pytest.mark.parametrize("label", ["YM4", "YM5", "YF4", "YF5"])
@pytest.mark.parametrize("cohort", ["M", "F"])
def test_other_branch_section_cannot_be_pinned_for_either_student_cohort(
    branch_section_world: dict[str, Any], cohort: str, label: str
) -> None:
    student = branch_section_world["students"][cohort]

    result = _execute(
        student,
        "build_timetable_proposal",
        {
            "mode": "from_scratch",
            "course_codes": [COURSE_CODE],
            "must_take_courses": [COURSE_CODE],
            "pinned_sections": [{"course_code": COURSE_CODE, "section_label": label}],
        },
    )

    assert result["ok"] is False, result
    assert result["constraints_satisfied"] is False
    assert result["constraint_failures"]
    assert result["constraint_failures"][0]["section_label"] == label


def test_remote_projection_defensively_drops_other_branch_rows() -> None:
    """Absolute branch exclusion survives even a malformed local payload.

    Ordinarily the executor has already filtered these rows.  This direct
    projector test prevents a future alternate executor or compatibility payload
    from transmitting YM/YF evidence to the answer model.
    """
    clash_projected = project_tool_result_for_remote(
        "my_clash_free_sections",
        {
            "tool": "my_clash_free_sections",
            "ok": True,
            "courses": [
                {
                    "course_code": COURSE_CODE,
                    "status": "OK",
                    "clash_free": [
                        {"section": "M1", "meetings": ["SUN 09:00-10:15"]},
                        {"section": "YM4", "meetings": ["TUE 11:00-12:15"]},
                        {"section": "YF5", "meetings": []},
                    ],
                    "clashing": [{"section": "YF4", "meetings": ["WED 11:00-12:15"]}],
                }
            ],
        },
        RemoteIdentityMap(),
    )
    proposal_projected = project_tool_result_for_remote(
        "build_timetable_proposal",
        {
            "tool": "build_timetable_proposal",
            "ok": True,
            "alternatives": [
                {
                    "option": 1,
                    "courses": [
                        {"course_code": COURSE_CODE, "section": "M1", "credits": 3},
                        {"course_code": COURSE_CODE, "section": "YM4", "credits": 3},
                        {"course_code": COURSE_CODE, "section": "YF5", "credits": 3},
                    ],
                    "meetings": [
                        {
                            "course_code": COURSE_CODE,
                            "section": "M1",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                        },
                        {
                            "course_code": COURSE_CODE,
                            "section": "YF4",
                            "day": "WED",
                            "start": "11:00",
                            "end": "12:15",
                        },
                    ],
                }
            ],
        },
        RemoteIdentityMap(),
    )

    assert _section_labels(clash_projected["courses"][0]["clash_free"]) == {"M1"}
    assert clash_projected["courses"][0]["clashing"] == []
    assert _section_labels(proposal_projected["alternatives"][0]["courses"]) == {"M1"}
    assert _section_labels(proposal_projected["alternatives"][0]["meetings"]) == {"M1"}


@pytest.mark.parametrize(("cohort", "expected"), [("M", "M1"), ("F", "F1")])
def test_stored_student_baseline_keeps_only_the_students_local_cohort(
    branch_section_world: dict[str, Any], cohort: str, expected: str
) -> None:
    student = branch_section_world["students"][cohort]
    sections = branch_section_world["sections"]
    for section in (sections[expected], sections["YM4"], sections["YF4"]):
        StudentTermSection.objects.create(
            student_id=student.student_id,
            academic_year="1448",
            term="1",
            term_section=section,
            source="scraper_timetable",
        )

    rows = get_student_term_baseline(
        student.student_id,
        "1448",
        "1",
        snapshot=Snapshot.REGISTERED,
    )

    assert {str(row.get("section") or "") for row in rows} == {expected}


def test_snapshot_writer_drops_other_branch_section_ids(
    branch_section_world: dict[str, Any],
) -> None:
    student = branch_section_world["students"]["M"]
    sections = branch_section_world["sections"]

    result = replace_student_term_sections(
        student.student_id,
        "1448",
        "1",
        [sections["M1"].id, sections["YM4"].id, sections["YF4"].id],
        source="scraper_timetable",
        replace_source_across_terms="scraper_timetable",
    )

    assert result == {"inserted": 1, "excluded_other_branch": 2}
    assert set(
        StudentTermSection.objects.filter(student_id=student.student_id).values_list(
            "term_section__section", flat=True
        )
    ) == {"M1"}


def test_every_reader_of_a_stored_link_agrees_the_other_branch_is_excluded(
    branch_section_world: dict[str, Any],
) -> None:
    """One student, one stored other-branch link, four independent readers.

    The exclusion first landed on the student-facing timetable only, so a
    student whose timetable screen showed nothing was still booked solid on the
    group-availability screen, still occupied an elective slot in the resolver,
    and was still sized into an exam room - four surfaces disagreeing about the
    same person. A rule enforced on some readers of a row is not a rule.
    """
    from core.services.elective_resolver import _get_timetable_courses
    from core.services.group_availability import _load_meetings_by_student
    from core.services.planner_builder import _catalog_for_courses

    student = branch_section_world["students"]["M"]
    sections = branch_section_world["sections"]
    StudentTermSection.objects.create(
        student_id=student.student_id,
        academic_year="1448",
        term="1",
        term_section=sections["YM4"],
        source="scraper_timetable",
    )

    meetings, _enrolled = _load_meetings_by_student([student.student_id], "1448", "1")
    assert meetings.get(student.student_id, []) == []

    assert _get_timetable_courses(student.student_id, academic_year="1448", term="1") == set()

    baseline = get_student_term_baseline(student.student_id, "1448", "1", snapshot=Snapshot.ANY)
    assert _section_labels(baseline) == set()

    # The staff build path takes no student, and used to skip the filter
    # entirely on that branch - which is where an unscoped catalogue would
    # quietly re-offer the sections every other reader had stopped showing.
    catalogue = _catalog_for_courses("1448", "1", [COURSE_CODE], "", None)
    offered = {str(row["section"]).upper() for row in catalogue.get(COURSE_CODE, [])}
    assert offered == {"M1", "M0", "F1", "F0"}
