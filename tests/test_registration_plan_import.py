"""Seeding registered timetables from a registration-plan workbook.

The importer's job is to refuse everything it cannot prove. Two distinctions carry
the whole design:

  * a section resolved by TIMES, one-to-one, or not at all — the workbook labels
    sections `AI:S1` while the database holds `M1`, so nothing matches as a string,
    and a near-match would seat a student in the wrong section of the right course;
  * a course with NO sections is a reported GAP, while a course WITH sections whose
    label did not resolve is a failure. Collapsing those hides the one that matters.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from core.models import (
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.registration_plan_import import (
    apply_plan,
    build_plan,
    parse_meetings,
)

pytestmark = pytest.mark.django_db

YEAR, TERM = "1448", "1"

Meeting = tuple[str, str, str]
WorkbookRow = tuple[object, ...]
SectionWorld = dict[str, TermSection]


def _section(
    course: str,
    meetings: Iterable[Meeting],
    section: str = "M1",
) -> TermSection:
    ts = TermSection.objects.create(
        course_code=course[:2],
        course_number=course[2:],
        course_key=course,
        course_name=course,
        section=section,
    )
    for day, start, end in meetings:
        TermSectionMeeting.objects.create(term_section=ts, day=day, start_time=start, end_time=end)
    return ts


@pytest.fixture
def world() -> SectionWorld:
    """Two sections of one course at different times, plus a single-section course."""
    Student.objects.update_or_create(
        student_id=700001, defaults={"name": "A", "program": "AI", "section": "M"}
    )
    Student.objects.update_or_create(
        student_id=700002, defaults={"name": "B", "program": "AI", "section": "M"}
    )
    return {
        "s1": _section("AI331", [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")], "M1"),
        "s2": _section("AI331", [("MON", "10:30", "11:45"), ("WED", "10:30", "11:45")], "M2"),
        "solo": _section("CS323", [("TUE", "13:00", "14:15")], "M1"),
    }


def _rosters(*rows: WorkbookRow) -> list[WorkbookRow]:
    return list(rows)


def _detail(*rows: WorkbookRow) -> list[WorkbookRow]:
    return list(rows)


# ── sections resolve on times, one-to-one ────────────────────────


def test_a_section_is_resolved_by_its_meeting_times(world: SectionWorld) -> None:
    """The labels cannot be matched: the workbook says `AI:S1`, the database `M1`."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "Mon 09:00-10:15", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert len(plan.links) == 1
    assert plan.links[0]["term_section_id"] == world["s1"].id


def test_the_other_section_of_the_same_course_resolves_to_the_other_row(
    world: SectionWorld,
) -> None:
    """Proves the match is on times and not on 'first section of this course'."""
    plan = build_plan(
        _rosters(("AI331", "AI:S2", "Mon 10:30-11:45; Wed 10:30-11:45", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S2", "Mon 10:30-11:45", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.links[0]["term_section_id"] == world["s2"].id


def test_times_matching_two_sections_is_refused_not_guessed(world: SectionWorld) -> None:
    """A duplicate makes the resolution ambiguous. Seating the student in either
    would be a coin flip they cannot see."""
    _section("AI331", [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")], "M3")
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "AMBIGUOUS_SECTION" for p in plan.problems), plan.problems


def test_a_time_mismatch_on_a_multi_section_course_is_refused(world: SectionWorld) -> None:
    """The workbook moved some section times. Where the course has one section the
    fallback is unambiguous; where it has two, it is not."""
    plan = build_plan(
        _rosters(("AI331", "AI:S9", "Fri 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S9", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "TIME_MISMATCH" for p in plan.problems), plan.problems
    assert plan.time_disagreements, "the disagreement was not reported"


def test_a_time_mismatch_is_refused_unless_the_operator_opts_in(
    world: SectionWorld,
) -> None:
    """The workbook moved some section times; a student seated on a mismatch is
    seated in a section matching nothing they were told.

    The first version linked it silently whenever the course had one section. It is
    a real case and a real hazard, so it is now a decision an operator takes after
    reading the disagreement report — not a default."""
    args = (
        _rosters(("CS323", "AI:S1", "Fri 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "CS323", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    refused = build_plan(*args)
    assert not refused.ok
    assert any(p.code == "TIME_MISMATCH" for p in refused.problems), refused.problems
    assert refused.time_disagreements, "the disagreement was not reported"

    accepted = build_plan(*args, accept_moved_times=True)
    assert accepted.ok, [str(p) for p in accepted.problems]
    assert accepted.links[0]["term_section_id"] == world["solo"].id
    assert len(accepted.time_disagreements) == 1
    assert accepted.time_disagreements[0]["course"] == "CS323"


# -- what a data-integrity review found the first version got wrong --------


def test_a_duplicate_section_label_is_refused_not_silently_overwritten(
    world: SectionWorld,
) -> None:
    """THE most severe defect the review found.

    Two roster rows sharing `(course, label)` each resolved cleanly and the second
    silently replaced the first in the map — so every student matched to the first
    was seated in the second section, with `plan.ok` True and no diagnostic
    anywhere. This module docstring cites a bare `AI:` as a real workbook value,
    which is exactly the shape that collides."""
    plan = build_plan(
        _rosters(
            ("AI331", "AI:", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, ""),
            ("AI331", "AI:", "Mon 10:30-11:45; Wed 10:30-11:45", "", "-", 1, ""),
        ),
        _detail((700001, "AI", "AI331", "Core", "AI:", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "DUPLICATE_SECTION_LABEL" for p in plan.problems), plan.problems


def test_a_student_is_never_seated_in_the_other_cohorts_section(world: SectionWorld) -> None:
    """Sections are gender-segregated and the gender is the leading letter of the
    LABEL, which time matching never sees. Nothing checked it, so a male student
    could be seated in the sole `F2` section — latent only because every section on
    file today is `M1..M4`, while the wider section data holds 415 F sections."""
    female = _section("PHYS101", [("SUN", "08:00", "09:15")], "F1")
    plan = build_plan(
        _rosters(("PHYS101", "AI:S1", "Sun 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "PHYS101", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert female.section == "F1"
    assert not plan.ok
    assert any(p.code == "COHORT_MISMATCH" for p in plan.problems), plan.problems
    assert not plan.links, "a student was seated across the cohort boundary"


def test_an_unresolvable_cohort_is_refused_rather_than_guessed(world: SectionWorld) -> None:
    """`student_gender_strict` refuses instead of falling back to all-pass, because
    every real section is gendered — so an unresolved cohort is a total failure and
    not a partial one. The importer follows the same rule."""
    Student.objects.filter(student_id=700001).update(section="")
    _section("PHYS101", [("SUN", "08:00", "09:15")], "M1")
    plan = build_plan(
        _rosters(("PHYS101", "AI:S1", "Sun 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "PHYS101", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "UNKNOWN_COHORT" for p in plan.problems), plan.problems


def test_a_scenario_owned_section_is_never_a_match_target(world: SectionWorld) -> None:
    """`get_student_term_baseline` — the reader every screen goes through — filters
    scenario sections out. A draft timetable winning the time match would produce a
    row that is written and then invisible everywhere."""
    from core.models import TimetableScenario

    scenario = TimetableScenario.objects.create(academic_year=YEAR, term=TERM, name="draft")
    draft = _section("CHEM101", [("MON", "08:00", "09:15")], "M1")
    TermSection.objects.filter(pk=draft.pk).update(scenario=scenario)

    plan = build_plan(
        _rosters(("CHEM101", "AI:S1", "Mon 08:00-09:15", "", "-", 1, "")),
        _detail((700001, "AI", "CHEM101", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "NO_SUCH_COURSE" for p in plan.problems), plan.problems


def test_the_dry_run_reports_what_the_apply_would_delete(world: SectionWorld) -> None:
    """The first version reported only what it would WRITE, so a dry run could say
    "1 link for 1 student, 0 problems" while the apply removed two rows — and
    against the real database it would have removed all 1081 with no number shown
    anywhere."""
    StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["s2"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.replaces == 2, plan.summary()
    assert "REPLACING 2" in plan.summary()
    result = apply_plan(plan, YEAR, TERM)
    assert result["removed"] == plan.replaces


def test_the_written_count_is_measured_not_promised(world: SectionWorld) -> None:
    """`return len(plan.links)` was a promise. With `ignore_conflicts` a link the
    dry run printed could be dropped by the database while the caller was told it
    had been written."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    result = apply_plan(plan, YEAR, TERM)
    assert (
        result["written"]
        == StudentTermSection.objects.filter(
            student_id=700001, academic_year=YEAR, term=TERM
        ).count()
    )


def test_the_same_physical_section_can_be_recorded_in_two_terms(world: SectionWorld) -> None:
    """A real current row and an expected future row have distinct meanings."""
    StudentTermSection.objects.create(
        student_id=700001, academic_year="1447", term="2", term_section=world["s1"]
    )
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok, plan.problems
    apply_plan(plan, YEAR, TERM)
    assert set(
        StudentTermSection.objects.filter(
            student_id=700001,
            term_section=world["s1"],
        ).values_list("academic_year", "term")
    ) == {("1447", "2"), (YEAR, TERM)}


def test_expected_import_lands_beside_registrar_rows_without_replacing_them(
    world: SectionWorld,
) -> None:
    """This used to refuse outright, and the refusal was right at the time: a term
    held one snapshot, so writing the plan meant destroying the registration.

    Both snapshots now coexist, and importing a corrected plan into an
    already-registered term is the case the expected-versus-registered comparison
    exists for. So the import proceeds, the registrar row is untouched, and the
    operator is told what they are importing into rather than blocked.
    """
    real = StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source="scraper_timetable",
    )
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )

    assert plan.ok, plan.problems
    assert not any(p.code == "TARGET_TERM_HAS_REGISTRAR_ROWS" for p in plan.problems)
    assert any(n.code == "TARGET_TERM_HAS_REGISTRAR_ROWS" for n in plan.notices), plan.notices

    apply_plan(plan, YEAR, TERM)

    assert StudentTermSection.objects.filter(pk=real.pk).exists()
    assert set(
        StudentTermSection.objects.filter(
            student_id=700001, academic_year=YEAR, term=TERM
        ).values_list("source", flat=True)
    ) == {"scraper_timetable", f"registration_plan_{YEAR}_t{TERM}"}


def test_a_registration_with_no_student_id_is_refused(world: SectionWorld) -> None:
    """Counting it was not enough.

    openpyxl returns `None` for the continuation cells of a MERGED range, which is
    how a hand-authored plan usually writes one id across several course rows. The
    first fix counted those rows instead of dropping them silently — which turns a
    silent omission into a reported one, and still lets an incomplete authoritative
    timetable be applied. A row carrying a course or a section but no student is a
    registration with nobody attached to it, and forward-filling from a blank is a
    guess about whose registration it is."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
            (None, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "BLANK_STUDENT_ID" for p in plan.problems), plan.problems


def test_an_entirely_empty_row_is_counted_and_ignored(world: SectionWorld) -> None:
    """The trailing rows a spreadsheet carries. They hold no registration, so there
    is nothing to refuse — but they are still counted, or conservation cannot
    prove every row was accounted for."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
            (None, None, None, None, None, None, None, None, None),
            ("", "", "", "", "", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert plan.blank_rows == 2, plan.summary()
    assert plan.detail_rows_read == 3
    assert "2 blank" in plan.summary()


def test_every_detail_row_is_accounted_for(world: SectionWorld) -> None:
    """A conservation check. Without one, any future branch that drops a row keeps
    the summary looking healthy."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "Mon 09:00-10:15", "", "", ""),
            (700001, "AI", "AI331", "Core", "AI:S1", "Wed 09:00-10:15", "", "", ""),
            (700001, "AI", "AI491", "Project", "-", "no timeslot", "", "", ""),
            (700001, "AI", "GSE1", "Online elective", "online", "Sun 15:50-17:30", "", "", ""),
            (None, None, None, None, None, None, None, None, None),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert plan.detail_rows_read == 5
    assert (
        len(plan.links)
        + plan.blank_rows
        + plan.duplicate_rows
        + plan.skipped_unplaceable
        + sum(len(v) for v in plan.uncovered.values())
    ) == plan.detail_rows_read


def test_the_source_records_the_term_that_was_actually_written(world: SectionWorld) -> None:
    """`source` is a row only provenance marker — the field an operator uses to
    find and undo this import. It was a hardcoded literal, so a 1449/2 import
    claimed to be `registration_plan_1448_t1`."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        "1449",
        "2",
    )
    apply_plan(plan, "1449", "2")
    row = StudentTermSection.objects.get(student_id=700001)
    assert row.source == "registration_plan_1449_t2"


def test_applying_to_a_different_term_than_the_plan_was_built_for_is_refused(
    world: SectionWorld,
) -> None:
    """The DELETE used the arguments passed to `apply_plan`; the INSERT used values
    baked into the links by `build_plan`. Nothing checked they agreed, so a
    mismatched pair emptied one term and wrote into another."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    with pytest.raises(ValueError, match="built for"):
        apply_plan(plan, "1449", "2")


def test_apply_plan_refuses_an_unknown_student_even_when_called_directly(
    world: SectionWorld,
) -> None:
    """`apply_plan` is exported and `StudentTermSection.student_id` is a plain
    integer with no foreign key, so a direct call could write an orphan link. This
    database already holds 722 orphans of that class.

    The plan handed in here is VALID — `build_plan` would refuse an unknown id, so
    a plan that fails validation tests the wrong guard. This one is tampered with
    afterwards, which is exactly the shape of the call `apply_plan` has to survive
    on its own."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok
    plan.students.add(999999)
    plan.links.append(dict(plan.links[0], student_id=999999))

    with pytest.raises(ValueError, match="unknown student"):
        apply_plan(plan, YEAR, TERM)
    assert StudentTermSection.objects.count() == 0, "a rejected plan still wrote rows"


def test_a_second_apply_changes_nothing(world: SectionWorld) -> None:
    """Idempotency, measured on the rows a student can see rather than on primary
    keys — a delete-and-insert churns the keys and changes nothing else."""
    args = (
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    apply_plan(build_plan(*args), YEAR, TERM)
    fields = ("student_id", "academic_year", "term", "term_section_id", "source")
    first = sorted(StudentTermSection.objects.values_list(*fields))
    apply_plan(build_plan(*args), YEAR, TERM)
    second = sorted(StudentTermSection.objects.values_list(*fields))
    assert first == second and len(first) == 1


# ── the two kinds of "cannot place" ──────────────────────────────


def test_rows_with_no_timeslot_are_skipped_not_failed(world: SectionWorld) -> None:
    """`Project` and `Foundation retake` carry section `—` and say so themselves."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI491", "Project", "—", "no timeslot (graduation project)", "", "", ""),
            (700001, "AI", "CS111", "Foundation retake", "—", "first-year schedule", "", "", ""),
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok
    assert plan.skipped_unplaceable == 2
    assert len(plan.links) == 1


def test_a_course_with_no_sections_is_a_reported_gap_not_a_failure(
    world: SectionWorld,
) -> None:
    """`GSE1` and `FE2` have real times in the plan and no section anywhere. The
    term is still seedable; the gap is what the operator must be told."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (
                700001,
                "AI",
                "GSE1",
                "Online elective",
                "online (evening)",
                "Sun 15:50-17:30 (online)",
                "",
                "",
                "",
            ),
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert "GSE1" in plan.uncovered
    assert plan.uncovered["GSE1"][0]["times"] == [("SUN", "15:50", "17:30")], plan.uncovered
    assert len(plan.links) == 1


def test_a_course_that_HAS_sections_but_did_not_resolve_still_fails(
    world: SectionWorld,
) -> None:
    """The distinction that matters. `AI331` exists, so an unresolved label is a
    mapping fault — seeding around it would put a student somewhere arbitrary."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "SOME:OTHER", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert any(p.code == "UNRESOLVED_SECTION" for p in plan.problems)
    assert "AI331" not in plan.uncovered, "a mapping fault was filed as a coverage gap"


# ── writing ──────────────────────────────────────────────────────


def test_applying_replaces_only_the_students_in_the_plan(world: SectionWorld) -> None:
    """A student absent from the workbook keeps what they have; the replacement
    boundary must not expand beyond students the imported detail sheet considered."""
    StudentTermSection.objects.create(
        student_id=700002, academic_year=YEAR, term=TERM, term_section=world["solo"]
    )
    StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    result = apply_plan(plan, YEAR, TERM)
    assert result["removed"] == 1 and result["written"] == 1

    mine = list(
        StudentTermSection.objects.filter(student_id=700001).values_list(
            "term_section_id", flat=True
        )
    )
    assert mine == [world["s1"].id], "the replaced student kept a stale row"
    assert StudentTermSection.objects.filter(student_id=700002).count() == 1, (
        "a student absent from the plan was emptied"
    )


def test_reimport_removes_stale_links_for_students_now_only_on_nonphysical_rows(
    world: SectionWorld,
) -> None:
    """The detail sheet, not successful section resolution, defines replacement.

    A student's revised plan may contain only an uncovered online course or a
    deliberately non-physical project. Neither produces a link, but both prove
    that the workbook considered the student. Their old expected link must not
    survive as if it were still part of the new plan.
    """
    for student_id in (700001, 700002):
        StudentTermSection.objects.create(
            student_id=student_id,
            academic_year=YEAR,
            term=TERM,
            term_section=world["solo"],
            source=f"registration_plan_{YEAR}_t{TERM}",
        )

    plan = build_plan(
        _rosters(),
        _detail(
            (
                700001,
                "AI",
                "GSE1",
                "Online elective",
                "online (evening)",
                "Sun 15:50-17:30 (online)",
                "",
                "",
                "",
            ),
            (700002, "AI", "AI491", "Project", "—", "no timeslot", "", "", ""),
        ),
        YEAR,
        TERM,
    )

    assert plan.ok, [str(p) for p in plan.problems]
    assert plan.students == set(), "non-physical rows must not become section links"
    assert plan.replacement_scope == {700001, 700002}
    assert plan.replaces == 2

    result = apply_plan(plan, YEAR, TERM)

    assert result == {"removed": 2, "written": 0, "students": 0}
    assert not StudentTermSection.objects.filter(
        student_id__in=(700001, 700002), academic_year=YEAR, term=TERM
    ).exists()


def test_a_nonphysical_workbook_student_never_loses_registrar_rows(
    world: SectionWorld,
) -> None:
    """A student in the workbook with no placeable row is still in the replacement
    scope, so their EXPECTED rows are cleared. Their registrar rows are not: the
    scope is a class scope, and the plan importer owns exactly one class.

    This is the strongest form of the protection the old refusal provided, kept
    once the refusal itself became unnecessary -- the delete can no longer reach a
    registrar row, so nothing has to be blocked to keep one safe.
    """
    real = StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source="scraper_timetable",
    )
    stale_plan = StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    plan = build_plan(
        _rosters(),
        _detail((700001, "AI", "AI491", "Project", "—", "no timeslot", "", "", "")),
        YEAR,
        TERM,
    )

    assert plan.students == set()
    assert plan.replacement_scope == {700001}
    assert plan.ok, plan.problems

    apply_plan(plan, YEAR, TERM)

    assert StudentTermSection.objects.filter(pk=real.pk).exists()
    assert not StudentTermSection.objects.filter(pk=stale_plan.pk).exists()


def test_applying_reconciles_observed_programmes_for_old_and_new_sections(
    world: SectionWorld,
) -> None:
    """The plan importer writes StudentTermSection directly, bypassing the normal
    replacement service. Programme ownership must move with the registration,
    while authoritative imported memberships remain untouched.
    """
    StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    TermSectionProgram.objects.create(
        term_section=world["solo"],
        program="AI",
        assignment_source="observed",
    )
    TermSectionProgram.objects.create(
        term_section=world["solo"],
        program="DS",
        assignment_source="import",
    )
    TermSectionProgram.objects.create(
        term_section=world["s1"],
        program="STALE",
        assignment_source="observed",
    )
    TermSectionProgram.objects.create(
        term_section=world["s1"],
        program="DS",
        assignment_source="import",
    )

    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    apply_plan(plan, YEAR, TERM)

    assert set(world["solo"].program_links.values_list("program", "assignment_source")) == {
        ("DS", "import")
    }
    assert set(world["s1"].program_links.values_list("program", "assignment_source")) == {
        ("AI", "observed"),
        ("DS", "import"),
    }


def test_an_invalid_plan_cannot_be_applied(world: SectionWorld) -> None:
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "NOPE", "", "", "", "")),
        YEAR,
        TERM,
    )
    with pytest.raises(ValueError):
        apply_plan(plan, YEAR, TERM)
    assert StudentTermSection.objects.count() == 0


def test_an_unknown_student_is_named_and_never_created(world: SectionWorld) -> None:
    """Caught in `build_plan` by name. It used to reach `check_students_exist`,
    which is fine — but once the cohort check landed, an id with no `Student` row
    was reported as an unresolvable COHORT, which is true and useless."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((999999, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert not plan.ok
    assert [p.code for p in plan.problems] == ["UNKNOWN_STUDENT"], plan.problems
    assert "999999" in str(plan.problems[0])
    assert not Student.objects.filter(student_id=999999).exists()


def test_the_same_section_twice_for_one_student_is_one_seat(world: SectionWorld) -> None:
    """A lecture row and a lab row name the same section. That is one registration."""
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "Mon 09:00-10:15", "", "", ""),
            (700001, "AI", "AI331", "Core", "AI:S1", "Wed 09:00-10:15", "Sun 10:30-12:10", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert len(plan.links) == 1


def test_the_term_written_is_the_term_asked_for(world: SectionWorld) -> None:
    plan = build_plan(
        _rosters(("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, "")),
        _detail((700001, "AI", "AI331", "Core", "AI:S1", "", "", "", "")),
        "1449",
        "2",
    )
    apply_plan(plan, "1449", "2")
    row = StudentTermSection.objects.get(student_id=700001)
    assert (row.academic_year, row.term) == ("1449", "2")


# ── the parser ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("Mon 09:00-10:15", {("MON", "09:00", "10:15")}),
        (
            "Mon 09:00-10:15; Wed 09:00-10:15",
            {("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")},
        ),
        ("Sun 15:50-17:30 (online)", {("SUN", "15:50", "17:30")}),
        ("no timeslot (graduation project)", set()),
        ("", set()),
        (None, set()),
    ],
)
def test_meeting_times_are_parsed_from_the_workbook_prose(
    cell: object,
    expected: set[Meeting],
) -> None:
    assert parse_meetings(cell) == expected


# -- round three: what the second review found still contradicted the contract --


def test_two_sections_at_the_same_start_but_different_ends_are_distinguished(
    world: SectionWorld,
) -> None:
    """The END time is part of a section identity, and the first version threw it
    away: the regex captured it, `parse_meetings` returned only `(DAY, START)`, and
    the database projection selected only `start_time`. So a 75-minute lecture and
    a 100-minute one starting at the same moment were the same section."""
    short = _section("STAT305", [("TUE", "09:00", "10:15")], "M1")
    long_one = _section("STAT305", [("TUE", "09:00", "10:40")], "M2")

    plan = build_plan(
        _rosters(("STAT305", "AI:S2", "Tue 09:00-10:40", "", "-", 1, "")),
        _detail((700001, "AI", "STAT305", "Core", "AI:S2", "", "", "", "")),
        YEAR,
        TERM,
    )
    assert plan.ok, [str(p) for p in plan.problems]
    assert plan.links[0]["term_section_id"] == long_one.id, (
        "the 100-minute section was not distinguished from the 75-minute one"
    )
    assert plan.links[0]["term_section_id"] != short.id


def test_a_short_write_rolls_back_instead_of_committing(
    world: SectionWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The postcondition used to be checked AFTER the transaction closed, so the
    delete and the short write had already committed and the exception reported a
    failure the database had made permanent.

    Here `bulk_create` is made to insert fewer rows than planned. The student must
    keep the registrations they had, and none of the new ones may survive."""
    from django.db.models.query import QuerySet

    kept = StudentTermSection.objects.create(
        student_id=700001,
        academic_year=YEAR,
        term=TERM,
        term_section=world["solo"],
        source=f"registration_plan_{YEAR}_t{TERM}",
    )
    plan = build_plan(
        _rosters(
            ("AI331", "AI:S1", "Mon 09:00-10:15; Wed 09:00-10:15", "", "-", 1, ""),
            ("AI331", "AI:S2", "Mon 10:30-11:45; Wed 10:30-11:45", "", "-", 1, ""),
        ),
        _detail(
            (700001, "AI", "AI331", "Core", "AI:S1", "", "", "", ""),
            (700002, "AI", "AI331", "Core", "AI:S2", "", "", "", ""),
        ),
        YEAR,
        TERM,
    )
    assert len(plan.links) == 2

    real_bulk_create = QuerySet.bulk_create

    def short_bulk_create(
        self: QuerySet[StudentTermSection],
        objs: Iterable[StudentTermSection],
        *args: Any,
        **kwargs: Any,
    ) -> list[StudentTermSection]:
        return real_bulk_create(self, list(objs)[:1], *args, **kwargs)

    monkeypatch.setattr(QuerySet, "bulk_create", short_bulk_create)

    with pytest.raises(ValueError, match="rows exist after the write"):
        apply_plan(plan, YEAR, TERM)

    monkeypatch.undo()
    surviving = list(
        StudentTermSection.objects.filter(academic_year=YEAR, term=TERM).values_list(
            "student_id", "term_section_id"
        )
    )
    assert surviving == [(700001, world["solo"].id)], (
        f"the rollback did not restore the prior registration: {surviving}"
    )
    assert StudentTermSection.objects.filter(pk=kept.pk).exists() or surviving == [
        (700001, world["solo"].id)
    ]
