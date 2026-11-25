"""Compare actual registered and proposed sections using the graduation engine."""

from __future__ import annotations

import pytest

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.student_sections import get_student_term_baseline
from core.services.timetable_snapshots import Snapshot

pytestmark = pytest.mark.django_db
STUDENT_ID = 4_699_871
YEAR, TERM = 1448, 1


def _seed_curriculum():
    # The student is in programme term 5. ML leads to Applied ML, then to Capstone.
    # A separate final lab first comes due in programme term 8.
    student = Student.objects.create(
        student_id=STUDENT_ID,
        registration_no=str(STUDENT_ID),
        name="Planner Sample Student",
        program="PLN",
        section="M",
        status="ACTIVE",
        current_registered_credits=3,
    )
    courses = [
        ("CS301", "Algorithms", 5, None),
        ("AI331", "Machine Learning", 5, None),
        ("AI451", "Applied Machine Learning", 6, "AI331"),
        ("AI491", "Machine Learning Capstone", 7, "AI451"),
        ("AI480", "AI Systems Lab", 8, None),
        ("STAT305", "Applied Statistics", 5, None),
    ]
    for code, name, due, prerequisite in courses:
        Course.objects.create(course_code=code, description=name, credit_hours=3)
        ProgrammeRequirement.objects.create(
            program=student.program,
            course_code=code,
            course_name=name,
            programme_term=due,
            credit_hours=3,
            type="Mandatory",
        )
        if prerequisite:
            Prerequisite.objects.create(
                program=student.program,
                course_code=code,
                prerequisite_course_code=prerequisite,
            )
    sections = {}
    for code, label, day in [
        ("CS301", "M1", "SUN"),
        ("AI331", "M1", "MON"),
        ("AI331", "M2", "WED"),
        ("STAT305", "M1", "TUE"),
    ]:
        section = TermSection.objects.create(
            course_key=code,
            course_code=code[:-3],
            course_number=code[-3:],
            course_name=next(row[1] for row in courses if row[0] == code),
            section=label,
            available_capacity=30,
            registered_count=3,
        )
        TermSectionProgram.objects.create(term_section=section, program=student.program)
        TermSectionMeeting.objects.create(
            term_section=section,
            day=day,
            start_time="09:00",
            end_time="10:15",
        )
        sections[(code, label)] = section
    StudentTermSection.objects.create(
        student_id=STUDENT_ID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=sections["CS301", "M1"],
        source="scraper_timetable",
    )
    return sections


def _option(name, *sections):
    return {
        "name": name,
        "mappings": [{"term_section_id": s.pk, "course_key": s.course_key} for s in sections],
    }


def _compare(options, keep=True):
    from core.services.planner_graduation import compare_planner_graduation

    retained = (
        get_student_term_baseline(STUDENT_ID, str(YEAR), str(TERM), snapshot=Snapshot.EFFECTIVE)
        if keep
        else None
    )
    return compare_planner_graduation(
        student_id=STUDENT_ID,
        year=YEAR,
        term=TERM,
        options=options,
        retained_baseline=retained,
    )


def test_gateway_moves_graduation_earlier_but_section_time_does_not():
    sections = _seed_curriculum()
    result = _compare(
        [
            _option("A1", sections["AI331", "M1"]),
            _option("A2", sections["AI331", "M2"]),
            _option("B1", sections["STAT305", "M1"]),
        ]
    )
    assert result["baseline"]["terms_including_planning_term"] == 5
    assert result["baseline"]["completion_academic_year"] == 1450
    assert result["baseline"]["completion_term"] == 1
    for label in ("A1", "A2"):
        row = result["options"][label]
        assert row["terms_including_planning_term"] == 4
        assert (row["completion_academic_year"], row["completion_term"]) == (1449, 2)
        assert row["delta_terms"] == -1
        assert any(
            "AI451" in (r.get("course_code"), r.get("related_course_code")) for r in row["reasons"]
        )
    assert result["options"]["B1"]["delta_terms"] == 0


def test_ignore_registered_gateway_can_delay_graduation():
    sections = _seed_curriculum()
    StudentTermSection.objects.create(
        student_id=STUDENT_ID,
        academic_year=str(YEAR),
        term=str(TERM),
        term_section=sections["AI331", "M1"],
        source="scraper_timetable",
    )
    result = _compare([_option("C1", sections["STAT305", "M1"])], keep=False)
    assert result["baseline"]["terms_including_planning_term"] == 4
    assert result["options"]["C1"]["delta_terms"] == 1


def test_unfinished_forecast_has_no_precise_difference():
    sections = _seed_curriculum()
    Prerequisite.objects.create(
        program="PLN",
        course_code="AI480",
        prerequisite_course_code="NEVER100",
    )
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert result["baseline"]["status"] == "unresolved"
    assert result["options"]["A1"]["status"] == "unresolved"
    assert result["options"]["A1"]["delta_terms"] is None


def test_working_or_expected_timetable_cannot_stand_in_for_registration():
    sections = _seed_curriculum()
    StudentTermSection.objects.filter(student_id=STUDENT_ID).update(source="planner")
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert result["baseline"]["reason_code"] == "no_registered_timetable"
    assert result["options"]["A1"]["terms_including_planning_term"] == 4
    assert result["options"]["A1"]["delta_terms"] is None


def _row(year, term, credits, waiting=False):
    return {"academic_year": year, "term": term, "credits": credits, "waiting": waiting}


def test_credits_by_term_for_baseline_and_option_are_literal():
    sections = _seed_curriculum()
    result = _compare(
        [_option("A1", sections["AI331", "M1"]), _option("B1", sections["STAT305", "M1"])]
    )
    # Registered CS301 only: 3 cr now, a waiting term (AI331 must come first), then the rest.
    assert result["baseline"]["credits_by_term"] == [
        _row(1448, 1, 3),
        _row(1448, 2, 0, True),
        _row(1449, 1, 6),
        _row(1449, 2, 6),
        _row(1450, 1, 3),
    ]
    # CS301 + AI331 now (6 cr), then the gateway lets the chain run without a gap.
    assert result["options"]["A1"]["credits_by_term"] == [
        _row(1448, 1, 6),
        _row(1448, 2, 3),
        _row(1449, 1, 6),
        _row(1449, 2, 3),
    ]
    assert result["options"]["B1"]["credits_by_term"][:2] == [
        _row(1448, 1, 6),
        _row(1448, 2, 0, True),
    ]


def test_credits_by_term_length_and_last_entry_match_the_completion():
    sections = _seed_curriculum()
    result = _compare(
        [_option("A1", sections["AI331", "M1"]), _option("B1", sections["STAT305", "M1"])]
    )
    for row in (result["baseline"], *result["options"].values()):
        rows = row["credits_by_term"]
        assert len(rows) == row["terms_including_planning_term"]
        assert (rows[-1]["academic_year"], rows[-1]["term"]) == (
            row["completion_academic_year"],
            row["completion_term"],
        )
        assert rows[0]["waiting"] is False
        assert set(rows[0]) == {"academic_year", "term", "credits", "waiting"}


def test_credits_by_term_is_empty_when_unresolved_or_unavailable():
    sections = _seed_curriculum()
    Prerequisite.objects.create(
        program="PLN", course_code="AI480", prerequisite_course_code="NEVER100"
    )
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert result["baseline"]["status"] == "unresolved"
    assert result["baseline"]["credits_by_term"] == []
    assert result["options"]["A1"]["credits_by_term"] == []

    from core.services.planner_graduation import compare_planner_graduation

    summer = compare_planner_graduation(
        student_id=STUDENT_ID,
        year=YEAR,
        term=3,
        options=[_option("A1", sections["AI331", "M1"])],
    )
    assert summer["baseline"]["status"] == "unavailable"
    assert summer["baseline"]["credits_by_term"] == []
    assert summer["options"]["A1"]["credits_by_term"] == []


def _effects(result, name):
    return result["options"][name]["course_effects"]


def _compare_adding(options, add, keep=True):
    from core.services.planner_graduation import compare_planner_graduation

    retained = (
        get_student_term_baseline(STUDENT_ID, str(YEAR), str(TERM), snapshot=Snapshot.EFFECTIVE)
        if keep
        else None
    )
    return compare_planner_graduation(
        student_id=STUDENT_ID,
        year=YEAR,
        term=TERM,
        options=options,
        retained_baseline=retained,
        add=add,
    )


def test_postponing_the_gateway_costs_a_term_and_postponing_a_filler_costs_nothing():
    sections = _seed_curriculum()
    result = _compare([_option("AB", sections["AI331", "M1"], sections["STAT305", "M1"])])
    assert result["options"]["AB"]["terms_including_planning_term"] == 4
    assert _effects(result, "AB") == {
        "postponed": {
            "AI331": {"status": "complete", "terms_including_planning_term": 5, "delta_terms": 1},
            "STAT305": {"status": "complete", "terms_including_planning_term": 4, "delta_terms": 0},
        },
        "added": {},
    }


def test_adding_a_course_reports_its_forecast_and_what_cannot_be_forecast():
    sections = _seed_curriculum()
    result = _compare_adding(
        [_option("B1", sections["STAT305", "M1"])], ["AI331", "CS301", "AI451", "ZZZ999"]
    )
    added = _effects(result, "B1")["added"]
    # Registered CS301 is already in the option's course set: nothing to add.
    assert set(added) == {"AI331", "AI451", "ZZZ999"}
    assert added["AI331"] == {
        "status": "complete",
        "terms_including_planning_term": 4,
        "delta_terms": -1,
    }
    assert added["AI451"] == {"status": "unavailable", "reason_code": "unmet_course_prerequisites"}
    assert added["ZZZ999"] == {
        "status": "unavailable",
        "reason_code": "section_metadata_unavailable",
    }


def test_an_added_course_that_changes_nothing_has_a_zero_delta():
    sections = _seed_curriculum()
    result = _compare_adding([_option("A1", sections["AI331", "M1"])], ["STAT305"])
    assert _effects(result, "A1")["added"] == {
        "STAT305": {"status": "complete", "terms_including_planning_term": 4, "delta_terms": 0}
    }


def test_a_request_without_add_has_empty_added_effects():
    sections = _seed_curriculum()
    result = _compare([_option("A1", sections["AI331", "M1"])])
    assert _effects(result, "A1")["added"] == {}
    assert list(_effects(result, "A1")["postponed"]) == ["AI331"]


def test_a_registered_course_is_never_postponed():
    sections = _seed_curriculum()
    # Ignoring the registered week, an option may still contain the registered course itself.
    result = _compare([_option("R", sections["CS301", "M1"], sections["AI331", "M1"])], keep=False)
    assert list(_effects(result, "R")["postponed"]) == ["AI331"]


def test_an_option_that_did_not_finish_has_no_effects():
    sections = _seed_curriculum()
    Prerequisite.objects.create(
        program="PLN", course_code="AI480", prerequisite_course_code="NEVER100"
    )
    result = _compare_adding([_option("A1", sections["AI331", "M1"])], ["STAT305"])
    assert result["options"]["A1"]["status"] == "unresolved"
    assert _effects(result, "A1") == {"postponed": {}, "added": {}}


def test_at_most_eight_postponed_courses_in_code_order():
    _seed_curriculum()
    extra = []
    for number in range(1, 10):
        code = f"ELX{number}00"
        Course.objects.create(course_code=code, description=f"Extra {number}", credit_hours=3)
        ProgrammeRequirement.objects.create(
            program="PLN",
            course_code=code,
            course_name=f"Extra {number}",
            programme_term=8,
            credit_hours=3,
            type="Mandatory",
        )
        section = TermSection.objects.create(
            course_key=code,
            course_code=code[:3],
            course_number=code[3:],
            course_name=f"Extra {number}",
            section="M1",
            available_capacity=30,
            registered_count=3,
        )
        TermSectionProgram.objects.create(term_section=section, program="PLN")
        extra.append(section)
    result = _compare([_option("BIG", *extra)])
    assert list(_effects(result, "BIG")["postponed"]) == [f"ELX{n}00" for n in range(1, 9)]


def test_options_with_the_same_course_set_forecast_once(monkeypatch):
    from core.services import planner_graduation

    sections = _seed_curriculum()
    real = planner_graduation.build_graduation_report
    calls = []

    def counting(*args, **kwargs):
        calls.append(sorted(c["code"] for c in kwargs["_current_courses_override"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(planner_graduation, "build_graduation_report", counting)
    one = _compare_adding([_option("A1", sections["AI331", "M1"])], ["STAT305"])
    single = list(calls)
    calls.clear()
    two = _compare_adding(
        [_option("A1", sections["AI331", "M1"]), _option("A2", sections["AI331", "M2"])],
        ["STAT305"],
    )
    # baseline {CS301}, the option, and the one added course; postponing the gateway
    # is the baseline's own set, so it is served from the cache.
    assert single == [["CS301"], ["AI331", "CS301"], ["AI331", "CS301", "STAT305"]]
    assert calls == single
    assert _effects(one, "A1") == _effects(two, "A1") == _effects(two, "A2")
