"""Publishing elective mappings: what must be refused, and what must not be guessed.

A mapping is an academic decision. The importer's job is to refuse everything it
cannot prove, and — just as important — to never infer an instruction nobody wrote.
Omission is the one that matters most: a file listing two of a slot's three
approved courses is one somebody forgot to finish, and reading it as "withdraw the
third" costs a student an option they were entitled to.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement, Student
from core.services.elective_import import apply_plan, as_csv, build_plan
from core.services.elective_readiness import INVALID_MAPPING, NOT_PUBLISHED, READY, slot_status

pytestmark = pytest.mark.django_db

PROG = "IMP"
YEAR, TERM = "1448", "1"
HEADER = "academic_year,term,programme,slot_code,course_code,source_reference\n"


def _csv(*rows: str) -> str:
    return HEADER + "".join(r if r.endswith("\n") else r + "\n" for r in rows)


@pytest.fixture
def world():
    """Two 3-hour elective slots, one 2-hour slot, one mandatory course, a catalogue."""
    Student.objects.update_or_create(
        student_id=980001, defaults={"name": "I", "program": PROG, "section": "M"}
    )
    for code, type_, credits in (
        ("IE1", "Program Elective", 3),
        ("IE2", "Program Elective", 3),
        # A 2-hour PLACEHOLDER, for the credit rule. It used to be a Free Elective,
        # which is no longer a placeholder at all — students take FE/GSE as ordinary
        # courses — so that fixture was testing the wrong refusal.
        ("IE3", "Program Elective", 2),
        ("IF1", "Free Elective", 2),
        ("IM101", "Mandatory", 3),
    ):
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 7, "credit_hours": credits, "type": type_},
        )
    made = {}
    for code, credits, programme in (
        ("IX401", 3, PROG),
        ("IX402", 3, PROG),
        ("IX403", 2, PROG),
        ("OTHER401", 3, "ELSEWHERE"),
    ):
        made[code] = ElectiveCourse.objects.create(
            course_code=code, course_name=code, credit_hours=credits, programme=programme
        )
    return made


# ── validation refuses what it cannot prove ──────────────────────


def test_a_valid_file_plans_the_additions(world):
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"))
    assert plan.ok, [str(p) for p in plan.problems]
    assert len(plan.add) == 1 and not plan.retain and not plan.remove


def test_a_row_without_provenance_is_refused(world):
    """A mapping with no source cannot be audited, corrected or defended."""
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,"))
    assert not plan.ok
    assert any(p.code == "NO_SOURCE" for p in plan.problems)


def test_a_mandatory_requirement_is_not_a_slot(world):
    """Issue #55 defended at the import: the declared TYPE decides, not the code."""
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IM101,IX401,approved-1448"))
    assert any(p.code == "NOT_AN_ELECTIVE_SLOT" for p in plan.problems)


def test_a_course_from_another_programme_needs_explicit_approval(world):
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,OTHER401,approved-1448"))
    problem = next(p for p in plan.problems if p.code == "CROSS_PROGRAMME")
    assert "ELSEWHERE" in problem.detail


def test_a_credit_mismatch_is_refused(world):
    """A 3-hour course cannot fill a 2-hour placeholder — the student would not
    satisfy the requirement they chose it for."""
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE3,IX401,approved-1448"))
    problem = next(p for p in plan.problems if p.code == "CREDIT_MISMATCH")
    assert "2h" in problem.detail and "3h" in problem.detail

    # And the matching 2-hour course is accepted, so this is not a blanket refusal.
    assert build_plan(_csv(f"{YEAR},{TERM},{PROG},IE3,IX403,approved-1448")).ok


def test_unknown_programmes_slots_and_courses_are_refused(world):
    plan = build_plan(
        _csv(
            f"{YEAR},{TERM},NOSUCH,IE1,IX401,s",
            f"{YEAR},{TERM},{PROG},NOSLOT,IX401,s",
            f"{YEAR},{TERM},{PROG},IE1,NOCOURSE,s",
        )
    )
    assert {p.code for p in plan.problems} == {"NO_SUCH_SLOT", "NOT_IN_CATALOGUE"}


def test_a_bad_year_or_term_is_refused(world):
    plan = build_plan(_csv(f"14,{TERM},{PROG},IE1,IX401,s", f"{YEAR},9,{PROG},IE1,IX402,s"))
    assert {p.code for p in plan.problems} == {"BAD_YEAR", "BAD_TERM"}


def test_a_duplicate_row_is_refused(world):
    plan = build_plan(
        _csv(f"{YEAR},{TERM},{PROG},IE1,IX401,s", f"{YEAR},{TERM},{PROG},IE1,IX401,s")
    )
    problem = next(p for p in plan.problems if p.code == "DUPLICATE")
    assert "line 2" in problem.detail


def test_a_missing_column_rejects_the_file_outright(world):
    plan = build_plan("academic_year,term,programme,slot_code,course_code\n1448,1,IMP,IE1,IX401\n")
    assert any(p.code == "MISSING_COLUMNS" for p in plan.problems)


def test_problems_report_their_own_line(world):
    """A closure over the loop variable would blame the last line for everything."""
    plan = build_plan(
        _csv(
            f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
            f"{YEAR},{TERM},{PROG},IM101,IX401,approved-1448",
            f"{YEAR},{TERM},{PROG},IE2,NOCOURSE,approved-1448",
        )
    )
    assert {p.line for p in plan.problems} == {3, 4}


# ── all-or-nothing, and dry runs write nothing ───────────────────


def test_one_bad_row_offers_no_plan_at_all(world):
    """A partial publication leaves a slot half-approved with no record of which
    half — and the readiness gate opens on it."""
    plan = build_plan(
        _csv(
            f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
            f"{YEAR},{TERM},{PROG},IE2,NOCOURSE,approved-1448",
        )
    )
    assert not plan.ok
    assert plan.add == [] and plan.retain == [] and plan.remove == []


def test_applying_an_invalid_plan_is_refused(world):
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,NOCOURSE,s"))
    with pytest.raises(ValueError):
        apply_plan(plan)
    assert ElectiveTermMapping.objects.count() == 0


def test_a_dry_run_writes_nothing(world, tmp_path):
    path = tmp_path / "m.csv"
    path.write_text(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"), encoding="utf-8")
    call_command("import_elective_mappings", str(path))
    assert ElectiveTermMapping.objects.count() == 0

    call_command("import_elective_mappings", str(path), "--apply")
    assert ElectiveTermMapping.objects.count() == 1


def test_an_invalid_file_fails_the_command(world, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(_csv(f"{YEAR},{TERM},{PROG},IE1,NOCOURSE,s"), encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("import_elective_mappings", str(path), "--apply")
    assert ElectiveTermMapping.objects.count() == 0


def test_re_importing_the_same_file_creates_no_duplicates(world, tmp_path):
    path = tmp_path / "m.csv"
    path.write_text(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"), encoding="utf-8")
    call_command("import_elective_mappings", str(path), "--apply")
    call_command("import_elective_mappings", str(path), "--apply")
    assert ElectiveTermMapping.objects.count() == 1

    plan = build_plan(path.read_text(encoding="utf-8"))
    assert plan.add == [] and len(plan.retain) == 1


# ── omission never means deletion ────────────────────────────────


def test_omitting_a_row_does_not_remove_it(world):
    """The rule that protects a student from a half-finished file."""
    both = _csv(
        f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
        f"{YEAR},{TERM},{PROG},IE1,IX402,approved-1448",
    )
    apply_plan(build_plan(both))
    assert ElectiveTermMapping.objects.count() == 2

    partial = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"))
    assert partial.remove == [], "omission was read as an instruction to delete"
    apply_plan(partial)
    assert ElectiveTermMapping.objects.count() == 2


def test_replacement_requires_explicit_authorisation(world, tmp_path):
    both = _csv(
        f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
        f"{YEAR},{TERM},{PROG},IE1,IX402,approved-1448",
    )
    apply_plan(build_plan(both))

    partial_text = _csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448")
    replacing = build_plan(partial_text, replace_year=YEAR, replace_term=TERM)
    assert len(replacing.remove) == 1
    apply_plan(replacing)
    assert ElectiveTermMapping.objects.count() == 1

    # And the command refuses half the instruction.
    path = tmp_path / "m.csv"
    path.write_text(partial_text, encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("import_elective_mappings", str(path), "--apply", "--replace-year", YEAR)


def test_replacement_is_scoped_to_the_named_term(world):
    apply_plan(build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448")))
    apply_plan(build_plan(_csv(f"1447,2,{PROG},IE1,IX402,approved-1447")))
    assert ElectiveTermMapping.objects.count() == 2

    replacing = build_plan(
        _csv(f"{YEAR},{TERM},{PROG},IE1,IX402,approved-1448"),
        replace_year=YEAR,
        replace_term=TERM,
    )
    assert {r["academic_year"] for r in replacing.remove} == {YEAR}
    apply_plan(replacing)
    assert ElectiveTermMapping.objects.filter(academic_year="1447").count() == 1


def test_what_was_removed_can_be_written_back(world):
    """Reversal is built from what was REMOVED, not from the file that removed it:
    the point is to restore the state that existed, not replay the instruction."""
    apply_plan(
        build_plan(
            _csv(
                f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
                f"{YEAR},{TERM},{PROG},IE1,IX402,approved-1448",
            )
        )
    )
    replacing = build_plan(
        _csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"),
        replace_year=YEAR,
        replace_term=TERM,
    )
    reversal = as_csv(replacing.remove)
    apply_plan(replacing)
    assert ElectiveTermMapping.objects.count() == 1

    assert "IX402" in reversal
    restored = build_plan(reversal)
    assert restored.ok, [str(p) for p in restored.problems]
    apply_plan(restored)

    # Counting rows is not enough: a reversal that restored the wrong year, term or
    # course would also make this two. The state has to come back as it was.
    back = {
        (
            m["academic_year"],
            str(m["term"]),
            m["programme"],
            m["placeholder_code"],
            m["elective_id"],
        )
        for m in ElectiveTermMapping.objects.values(
            "academic_year", "term", "programme", "placeholder_code", "elective_id"
        )
    }
    assert back == {
        (YEAR, TERM, PROG, "IE1", world["IX401"].id),
        (YEAR, TERM, PROG, "IE1", world["IX402"].id),
    }


# ── the gate opens only for what was actually published ──────────


def test_readiness_flips_only_after_a_successful_commit(world):
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == NOT_PUBLISHED

    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448"))
    assert plan.ok
    # Planned but not applied: the gate must not move.
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == NOT_PUBLISHED

    apply_plan(plan)
    status, options, _ = slot_status(PROG, "IE1", YEAR, TERM)
    assert status == READY and [o["course_code"] for o in options] == ["IX401"]


def test_a_failed_validation_leaves_the_prior_state_untouched(world):
    apply_plan(build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448")))
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == READY

    bad = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,NOCOURSE,s"))
    with pytest.raises(ValueError):
        apply_plan(bad)
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == READY
    assert ElectiveTermMapping.objects.count() == 1


def test_one_ready_slot_does_not_activate_its_siblings(world):
    apply_plan(build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448")))
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == READY
    assert slot_status(PROG, "IE2", YEAR, TERM)[0] == NOT_PUBLISHED
    assert slot_status(PROG, "IF1", YEAR, TERM)[0] == NOT_PUBLISHED


def test_a_mapping_for_another_term_does_not_open_the_gate(world):
    apply_plan(build_plan(_csv(f"1447,2,{PROG},IE1,IX401,approved-1447")))
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == NOT_PUBLISHED
    assert slot_status(PROG, "IE1", "1447", "2")[0] == READY


def test_a_mapping_for_another_programme_does_not_open_the_gate(world):
    ProgrammeRequirement.objects.update_or_create(
        program="OTHERP",
        course_code="IE1",
        defaults={"programme_term": 7, "credit_hours": 3, "type": "Program Elective"},
    )
    ElectiveCourse.objects.create(
        course_code="IX401", course_name="IX401", credit_hours=3, programme="OTHERP"
    )
    apply_plan(build_plan(_csv(f"{YEAR},{TERM},OTHERP,IE1,IX401,approved-1448")))
    assert slot_status("OTHERP", "IE1", YEAR, TERM)[0] == READY
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == NOT_PUBLISHED


def test_the_student_payload_still_exposes_only_a_boolean(world):
    """Publishing must not change what a student can see about the machinery."""
    import json

    from core.services.course_detail import build_course_detail

    apply_plan(build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448")))
    for slot in ("IE1", "IE2"):
        payload = build_course_detail(980001, slot, academic_year=YEAR, term=TERM)
        assert "mapping_status" not in payload
        assert isinstance(payload["mapping_ready"], bool)
        body = json.dumps(payload, ensure_ascii=False)
        for state in ("NOT_PUBLISHED", "INVALID_MAPPING", "MAPPED_BUT_EMPTY", "READY"):
            assert state not in body


def test_a_write_that_fails_midway_removes_nothing(world, monkeypatch):
    """Replacement deletes and then inserts. Without one transaction around both, a
    failure between them withdraws a student's options and puts nothing back."""
    apply_plan(
        build_plan(
            _csv(
                f"{YEAR},{TERM},{PROG},IE1,IX401,approved-1448",
                f"{YEAR},{TERM},{PROG},IE1,IX402,approved-1448",
            )
        )
    )
    before = set(ElectiveTermMapping.objects.values_list("id", flat=True))
    assert len(before) == 2

    replacing = build_plan(
        _csv(f"{YEAR},{TERM},{PROG},IE2,IX401,approved-1448"),
        replace_year=YEAR,
        replace_term=TERM,
    )
    assert len(replacing.remove) == 2 and len(replacing.add) == 1

    def explode(*a, **k):
        raise RuntimeError("the insert half failed")

    monkeypatch.setattr(ElectiveTermMapping.objects, "bulk_create", explode)
    with pytest.raises(RuntimeError):
        apply_plan(replacing)

    assert set(ElectiveTermMapping.objects.values_list("id", flat=True)) == before, (
        "the delete survived a failed insert"
    )
    assert slot_status(PROG, "IE1", YEAR, TERM)[0] == READY


def test_free_and_university_electives_are_not_placeholders(world):
    """They are declared electives and are taken as ORDINARY COURSES.

    111 students have passed `FE1`, 139 `GSE1`, 64 `FE2`, 50 `GSE2` — under those
    very codes, which is not something you can do to a placeholder. Publishing
    options against one is refused, and the student sees their own status instead
    of "options not yet published" for a requirement they may already have passed.
    """
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IF1,IX403,approved-1448"))
    problem = next(p for p in plan.problems if p.code == "NOT_AN_ELECTIVE_SLOT")
    assert "Free Elective" in problem.detail

    # NOT `slot_status(...)[0] == NOT_PUBLISHED`. That was the assertion here and it
    # proved nothing: `NOT_PUBLISHED` is returned from BOTH branches — "not a slot"
    # and "a slot with nothing mapped" — so it holds under either rule. The
    # discriminating value is the one the `[0]` threw away.
    status, options, problems = slot_status(PROG, "IF1", YEAR, TERM)
    assert status == NOT_PUBLISHED and options == []
    assert problems == ["not an elective slot for this programme"], problems
    assert slot_status(PROG, "IE1", YEAR, TERM)[2] == [], (
        "a real unmapped slot must not report the same problem, or this says nothing"
    )


def test_only_the_program_elective_type_is_a_placeholder(world):
    from core.services.student_helpers import is_elective_slot

    assert is_elective_slot("Program Elective") is True
    assert is_elective_slot("  programme elective  ") is True
    for not_a_slot in ("Free Elective", "University Elective", "Mandatory", "", None):
        assert is_elective_slot(not_a_slot) is False, not_a_slot


def test_an_unowned_catalogue_entry_does_not_open_the_gate(world):
    """The write path refused it and the read path let it through.

    Five `ElectiveCourse` rows carry `programme=''`. `_problems` skipped them --
    `if o["programme"] and ...` -- so a mapping onto one read as READY at runtime
    while `build_plan` rejected the identical row as `CROSS_PROGRAMME` with owner
    `(unset)`. A row written by any other route (admin, shell, an older import)
    would therefore have shown students an option the importer will not publish.
    """
    orphan = ElectiveCourse.objects.create(
        course_code="IX9",
        course_name="Unowned",
        credit_hours=3,
        programme="",
    )
    ElectiveTermMapping.objects.create(
        programme=PROG,
        placeholder_code="IE1",
        elective_id=orphan.id,
        academic_year=YEAR,
        term=TERM,
    )
    status, options, problems = slot_status(PROG, "IE1", YEAR, TERM)
    assert status == INVALID_MAPPING, status
    assert options == [], "an unverifiable option was handed to the caller"
    assert any("no programme" in p for p in problems), problems

    # And the two paths now agree, which is the whole point.
    plan = build_plan(_csv(f"{YEAR},{TERM},{PROG},IE1,IX9,approved-1448"))
    assert not plan.ok
    assert [p.code for p in plan.problems] == ["CROSS_PROGRAMME"], plan.problems

    # The student still learns nothing about which of the two it was.
    from core.services.elective_readiness import NOT_READY_AR, student_message

    assert student_message(status) == NOT_READY_AR
