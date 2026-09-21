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

import importlib
from io import StringIO

import pytest
from django.core.management import call_command

from core.models import ElectiveCourse, Prerequisite, ProgrammeRequirement
from core.services.curriculum_integrity import (
    COURSE_NOT_IN_PLAN,
    PREREQUISITE_NOT_IN_PLAN,
    PROGRAMME_HAS_NO_PLAN,
    find_orphan_prerequisites,
)
from core.services.db_admin_ops import run_integrity_checks

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

    assert Prerequisite.objects.count() == 2
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


def test_an_elective_catalogue_course_is_not_an_orphan():
    """A plan carries placeholder slots; the catalogue is what resolves them.

    `eligibility._course_exists_in_program` already counts the elective catalogue
    as part of a programme. If this check did not, a prerequisite naming a real
    elective would be reported as unsatisfiable when it is perfectly takeable.
    """

    _plan("ELC", ("ELC101", 1, 3), ("ELC1", 6, 3), ("ELC401", 7, 3))
    ElectiveCourse.objects.create(
        programme="ELC", course_code="ELC330", course_name="Real elective", credit_hours=3
    )
    _prereq("ELC", "ELC401", "ELC330")
    _prereq("ELC", "ELC401", "ELC999")

    findings = find_orphan_prerequisites()

    # ELC330 is takeable; only the code that exists nowhere is reported.
    assert [f.referenced_course_code for f in findings] == ["ELC999"]


def test_the_database_integrity_sweep_reports_orphans():
    """The check needs a surface a human already opens, not only a command."""

    _build_second_cohort_maths_spine("MATH204")

    result = run_integrity_checks()

    assert result["orphan_prerequisites"] == 1
    assert result["orphan_prerequisite_details"] == [
        "DS2/MATH471 -> MATH204 [PREREQUISITE_NOT_IN_PLAN] "
        f"(prerequisites.id={Prerequisite.objects.get(prerequisite_course_code='MATH204').id})"
    ]
    assert "orphan_prerequisites" in result["advice"]


def test_the_integrity_sweep_is_quiet_when_the_curriculum_is_sound():
    _build_second_cohort_maths_spine("MATH106")

    result = run_integrity_checks()

    assert Prerequisite.objects.count() == 5
    assert result["orphan_prerequisites"] == 0
    assert result["orphan_prerequisite_details"] == []


def test_dirty_database_values_are_normalised_on_both_sides():
    """Normalisation is pinned on DATA, not only on the caller's argument.

    The module claims a database-side `program__in` filter would "silently miss a
    row stored as ds2 or with a stray space". That claim has to be tested where
    the dirt actually lives: in the stored `program` and `course_code` columns.
    Un-normalised, a single stray space turns a whole programme into a
    PROGRAMME_HAS_NO_PLAN finding, or skips it entirely.
    """

    ProgrammeRequirement.objects.create(
        program=" ds2 ",
        course_code=" math 106 ",
        course_name="Calculus II",
        type="Mandatory",
        programme_term=3,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="ds2",
        course_code="MATH471",
        course_name="Optimization",
        type="Mandatory",
        programme_term=8,
        credit_hours=3,
    )
    _prereq(" DS2 ", " math471 ", " math 106 ")

    assert ProgrammeRequirement.objects.count() == 2
    assert Prerequisite.objects.count() == 1
    assert find_orphan_prerequisites() == []
    # The same normalisation must let the caller find it by any spelling.
    assert find_orphan_prerequisites(programs=[" ds2 "]) == []


def test_a_dirty_row_that_is_genuinely_wrong_is_still_caught():
    """Normalisation must not become a way to swallow real faults."""

    ProgrammeRequirement.objects.create(
        program=" ds2 ",
        course_code=" math 106 ",
        course_name="Calculus II",
        type="Mandatory",
        programme_term=3,
        credit_hours=3,
    )
    ProgrammeRequirement.objects.create(
        program="ds2",
        course_code="MATH471",
        course_name="Optimization",
        type="Mandatory",
        programme_term=8,
        credit_hours=3,
    )
    _prereq(" DS2 ", " math471 ", " math 204 ")

    findings = find_orphan_prerequisites()

    assert [(f.program, f.course_code, f.referenced_course_code) for f in findings] == [
        ("DS2", "MATH471", "MATH204")
    ]


def test_the_finding_carries_the_code_and_describes_itself():
    """`describe()` is rendered on the DB admin screen, so its text is a contract."""

    _plan("DSC", ("DSC101", 1, 3), ("DSC201", 2, 3))
    row = _prereq("DSC", "DSC201", "DSC999")

    finding = find_orphan_prerequisites()[0]

    assert finding.prerequisite_id == row.id
    assert finding.referenced_course_code == "DSC999"
    assert finding.describe() == (
        f"DSC/DSC201 -> DSC999 [{PREREQUISITE_NOT_IN_PLAN}] (prerequisites.id={row.id})"
    )


def test_one_row_wrong_on_both_sides_yields_both_findings():
    _plan("BTH", ("BTH101", 1, 3))
    _prereq("BTH", "BTH900", "BTH999")

    findings = find_orphan_prerequisites()

    assert [(f.course_code, f.referenced_course_code, f.reason) for f in findings] == [
        ("BTH900", "BTH900", COURSE_NOT_IN_PLAN),
        ("BTH900", "BTH999", PREREQUISITE_NOT_IN_PLAN),
    ]


def test_the_command_exits_non_zero_only_when_asked_and_only_on_a_fault():
    """The `--fail-on-orphan` flag is documented as a gate; pin that it gates."""

    _build_second_cohort_maths_spine("MATH204")

    # Reporting alone never fails, so the audit is safe to run anywhere.
    out = StringIO()
    call_command("curriculum_integrity_report", stdout=out)
    assert "MATH204" in out.getvalue()

    with pytest.raises(SystemExit):
        call_command("curriculum_integrity_report", "--fail-on-orphan", stdout=StringIO())

    # Narrowed to a clean programme, the same fault is out of scope.
    clean = StringIO()
    call_command("curriculum_integrity_report", "--program", "AI2", stdout=clean)
    assert "No orphan prerequisites" in clean.getvalue()


def _migration_0066():
    """The migration module, imported by path: its name starts with a digit."""

    return importlib.import_module("core.migrations.0066_repoint_ds2_math471_calculus_prerequisite")


class _FakeApps:
    """`apps.get_model` over the real models.

    The migration touches no schema, so the historical model and the live one are
    the same shape; this exercises the real branches without a migration harness.
    """

    @staticmethod
    def get_model(app_label: str, model_name: str):
        assert (app_label, model_name) == ("core", "Prerequisite")
        return Prerequisite


def test_the_migration_repoints_the_shipped_row():
    _build_second_cohort_maths_spine("MATH204")

    _migration_0066().forwards(_FakeApps, None)

    assert set(
        Prerequisite.objects.filter(course_code="MATH471").values_list(
            "prerequisite_course_code", flat=True
        )
    ) == {"MATH106", "MATH243"}
    assert find_orphan_prerequisites() == []


def test_the_migration_deletes_rather_than_violating_the_unique_constraint():
    """The branch the docstring works hardest to justify: BOTH rows already exist."""

    _build_second_cohort_maths_spine("MATH204")
    _prereq("DS2", "MATH471", "MATH106")

    _migration_0066().forwards(_FakeApps, None)

    assert not Prerequisite.objects.filter(prerequisite_course_code="MATH204").exists()
    assert (
        Prerequisite.objects.filter(
            program="DS2", course_code="MATH471", prerequisite_course_code="MATH106"
        ).count()
        == 1
    )


def test_the_migration_is_a_no_op_when_there_is_nothing_to_repoint():
    """A rebuilt online database migrates against empty curriculum tables."""

    _migration_0066().forwards(_FakeApps, None)

    assert Prerequisite.objects.count() == 0


def test_reversing_the_migration_never_re_introduces_math204():
    """The obvious mirror would CREATE the defect on a database that never had it.

    A rebuilt online database no-ops on the way forward and receives the corrected
    row from the release seed. An unconditional reverse would then break all 191
    DS2 forecasts on a database that was correct a moment earlier.
    """

    _build_second_cohort_maths_spine("MATH106")

    _migration_0066().backwards(_FakeApps, None)

    assert not Prerequisite.objects.filter(prerequisite_course_code="MATH204").exists()
    assert find_orphan_prerequisites() == []


def test_the_migration_matches_a_dirty_programme_value():
    """An exact `program="DS2"` filter would no-op here and report success.

    That is the worst outcome available: 191 students left broken by a migration
    that says OK. The sibling checker normalises for this reason; so does this.
    """

    _plan(
        "DS2",
        ("MATH106", 3, 3),
        ("MATH243", 4, 3),
        ("MATH471", 8, 3),
    )
    Prerequisite.objects.create(
        program=" ds2 ", course_code=" math 471 ", prerequisite_course_code=" math204 "
    )

    _migration_0066().forwards(_FakeApps, None)

    assert Prerequisite.objects.get().prerequisite_course_code == "MATH106"
    assert find_orphan_prerequisites() == []
