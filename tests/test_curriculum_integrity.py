"""A prerequisite must name a course its own programme's plan actually contains.

`DS2/MATH471 -> MATH204` broke this and nothing noticed. MATH204 is the FIRST
cohort's Calculus II; the second-cohort plans renumbered it MATH106, so no DS2
student could ever satisfy it, MATH471 was blocked forever, and the three credits
it carried put every DS2 student under the zero-slack `147(HOURS)` co-op gate.
All 191 DS2 students silently lost their graduation forecast.

These tests pin the detector, not the database: a scan asserted against an empty
test database passes vacuously and would stay green through the exact defect it
claims to guard. Every case below therefore builds a plan first and asserts on a
fixture that is provably non-empty.
"""

from __future__ import annotations

import pytest

from core.models import Prerequisite, ProgrammeRequirement
from core.services.curriculum_integrity import (
    COURSE_NOT_IN_PLAN,
    PREREQUISITE_NOT_IN_PLAN,
    PROGRAMME_HAS_NO_PLAN,
    find_orphan_prerequisites,
)

pytestmark = pytest.mark.django_db


def _plan(program: str, *courses: tuple[str, int, int]) -> None:
    for code, term, credits in courses:
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=code,
            course_name=code,
            type="Mandatory",
            programme_term=term,
            credit_hours=credits,
        )


def _prereq(program: str, course_code: str, cell: str) -> Prerequisite:
    return Prerequisite.objects.create(
        program=program, course_code=course_code, prerequisite_course_code=cell
    )


def _build_second_cohort_maths_spine(calculus_two_code: str) -> None:
    """The real DS2 shape: MATH105 -> MATH106 -> MATH243 -> MATH471.

    `calculus_two_code` is the cell MATH471 points at. MATH106 is the truth;
    MATH204 is the defect that shipped.
    """

    _plan(
        "DS2",
        ("MATH105", 1, 3),
        ("MATH106", 3, 3),
        ("MATH243", 4, 3),
        ("MATH471", 8, 3),
        ("STAT307", 5, 3),
        ("DS492", 10, 6),
    )
    _prereq("DS2", "MATH106", "MATH105")
    _prereq("DS2", "STAT307", "MATH106")
    _prereq("DS2", "MATH471", calculus_two_code)
    _prereq("DS2", "MATH471", "MATH243")
    _prereq("DS2", "DS492", "147(HOURS)")


def test_the_shipped_ds2_defect_is_detected():
    _build_second_cohort_maths_spine("MATH204")

    findings = find_orphan_prerequisites()

    assert [(f.program, f.course_code, f.referenced_course_code, f.reason) for f in findings] == [
        ("DS2", "MATH471", "MATH204", PREREQUISITE_NOT_IN_PLAN)
    ]


def test_the_repointed_ds2_spine_is_clean():
    _build_second_cohort_maths_spine("MATH106")

    # Guard against a vacuous pass: an empty database also reports no orphans.
    assert Prerequisite.objects.count() == 5
    assert ProgrammeRequirement.objects.count() == 6
    assert find_orphan_prerequisites() == []


def test_credit_hour_gates_are_not_course_codes():
    """`147(HOURS)` is a pseudo-prerequisite. Flagging it would drown the report."""

    _plan("HRS", ("HRS101", 1, 3), ("HRS490", 8, 6))
    _prereq("HRS", "HRS490", "147(HOURS)")
    _prereq("HRS", "HRS490", "90 HOURS")

    assert find_orphan_prerequisites() == []


def test_a_course_from_another_programmes_plan_is_still_an_orphan_here():
    """The exact shape of the DS2 defect: the code is real, just not in THIS plan."""

    _plan("DS", ("MATH204", 4, 3), ("MATH471", 7, 3))
    _plan("DS2", ("MATH106", 3, 3), ("MATH471", 8, 3))
    _prereq("DS", "MATH471", "MATH204")
    _prereq("DS2", "MATH471", "MATH204")

    findings = find_orphan_prerequisites()

    assert len(findings) == 1
    assert findings[0].program == "DS2"
    assert findings[0].referenced_course_code == "MATH204"
    assert findings[0].reason == PREREQUISITE_NOT_IN_PLAN


def test_a_governed_course_outside_the_plan_is_reported():
    _plan("GOV", ("GOV101", 1, 3))
    _prereq("GOV", "GOV999", "GOV101")

    findings = find_orphan_prerequisites()

    assert [(f.course_code, f.reason) for f in findings] == [("GOV999", COURSE_NOT_IN_PLAN)]


def test_a_comma_separated_cell_is_split_like_the_recommender_splits_it():
    """`get_program_prerequisites` splits on commas; a bad code may hide beside a good one."""

    _plan("CMA", ("CMA101", 1, 3), ("CMA201", 2, 3), ("CMA301", 3, 3))
    _prereq("CMA", "CMA301", "CMA101,CMA999")

    findings = find_orphan_prerequisites()

    assert [(f.referenced_course_code, f.reason) for f in findings] == [
        ("CMA999", PREREQUISITE_NOT_IN_PLAN)
    ]


def test_a_programme_with_no_plan_is_reported_once_not_once_per_row():
    _plan("HAS", ("HAS101", 1, 3))
    _prereq("HAS", "HAS101", "")
    for course in ("NOP201", "NOP301", "NOP401"):
        _prereq("NOP", course, "NOP101")

    findings = find_orphan_prerequisites()

    assert [(f.program, f.reason) for f in findings] == [("NOP", PROGRAMME_HAS_NO_PLAN)]


def test_programs_filter_narrows_the_scan():
    _plan("AAA", ("AAA101", 1, 3))
    _plan("BBB", ("BBB101", 1, 3))
    _prereq("AAA", "AAA101", "AAA999")
    _prereq("BBB", "BBB101", "BBB999")

    assert len(find_orphan_prerequisites()) == 2
    assert [f.program for f in find_orphan_prerequisites(programs=["BBB"])] == ["BBB"]
    # Programme identity is normalised, so a lowercase argument still matches.
    assert [f.program for f in find_orphan_prerequisites(programs=["bbb"])] == ["BBB"]
    assert find_orphan_prerequisites(programs=[]) == []


def test_findings_are_ordered_so_a_report_diffs_cleanly():
    _plan("ORD", ("ORD101", 1, 3), ("ORD201", 2, 3))
    _prereq("ORD", "ORD201", "ORD903")
    _prereq("ORD", "ORD101", "ORD902")
    _prereq("ORD", "ORD201", "ORD901")

    findings = find_orphan_prerequisites()

    assert [(f.course_code, f.referenced_course_code) for f in findings] == [
        ("ORD101", "ORD902"),
        ("ORD201", "ORD901"),
        ("ORD201", "ORD903"),
    ]
