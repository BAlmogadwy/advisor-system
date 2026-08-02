"""The gate that decides whether an elective screen may be shown to a programme.

It exists because the answer today is "no, for every programme": 2 of 28 slots are
ready and 2035 student-slot pairs would see an empty screen. A gate that says yes
too easily is worse than no gate, so each condition is tested on its own — a
readiness report that only ever answers "not ready" would pass a naive test while
proving nothing.
"""

from __future__ import annotations

import pytest

from core.management.commands.elective_mapping_readiness import readiness
from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement, Student

pytestmark = pytest.mark.django_db

PROG = "RDY"


def _slot(code="RE1", credits=3, type_="Program Elective", program=PROG):
    ProgrammeRequirement.objects.update_or_create(
        program=program,
        course_code=code,
        defaults={"programme_term": 7, "credit_hours": credits, "type": type_},
    )


def _option(code="RX401", credits=3, programme=PROG):
    return ElectiveCourse.objects.create(
        course_code=code, course_name=code, credit_hours=credits, programme=programme
    )


#: The term the report defaults to, so the fixtures are published where it looks.
YEAR, TERM = "1448", "1"


def _map(elective, slot="RE1", programme=PROG, year=YEAR, term=TERM):
    ElectiveTermMapping.objects.create(
        programme=programme,
        placeholder_code=slot,
        elective_id=elective.id,
        academic_year=year,
        term=term,
    )


def _row(program=PROG, slot="RE1"):
    rows = readiness(YEAR, TERM)
    return next(r for r in rows if r["programme"] == program and r["slot"] == slot)


@pytest.fixture(autouse=True)
def _a_student():
    Student.objects.update_or_create(
        student_id=970001, defaults={"name": "R", "program": PROG, "section": "M"}
    )


def test_a_mapped_slot_with_an_active_option_is_ready():
    """The positive case. Without it, a gate stuck on "no" would pass every test."""
    _slot()
    _map(_option())
    r = _row()
    assert r["mapping_exists"] and r["active_options"] == 1
    assert r["ready"] is True, r["problems"]
    assert r["students"] == 1


def test_an_unmapped_slot_is_not_ready():
    _slot()
    r = _row()
    assert r["mapping_exists"] is False and r["active_options"] == 0
    assert r["ready"] is False


def test_the_schema_forbids_a_mapping_that_points_at_nothing():
    """Written as a Python guard first; the database already does it.

    Deleting the elective cascades the mapping away, so a dangling row cannot
    exist — and the guard could only have been tested by removing the constraint
    that makes it unnecessary. The check is gone from the report; the constraint is
    asserted here so its removal is a failing test rather than a silent regression.
    """
    _slot()
    elective = _option()
    _map(elective)
    assert ElectiveTermMapping.objects.count() == 1
    elective.delete()
    assert ElectiveTermMapping.objects.count() == 0, "the FK stopped cascading"
    assert _row()["mapping_exists"] is False


def test_the_schema_forbids_a_duplicate_mapping():
    """`uq_elective_mapping` on (year, term, programme, placeholder, elective)."""
    from django.db import IntegrityError, transaction

    _slot()
    e = _option()
    _map(e)
    with pytest.raises(IntegrityError), transaction.atomic():
        _map(e)


def test_a_cross_programme_mapping_is_not_ready():
    """Another programme's elective offered under this one's slot."""
    _slot()
    _map(_option(code="OTHER401", programme="SOMEWHERE"))
    r = _row()
    assert r["ready"] is False
    assert any("cross-programme" in p for p in r["problems"])
    # A blank programme is unset, not cross-programme — a known, separate gap.
    ElectiveTermMapping.objects.all().delete()
    _map(_option(code="BLANK401", programme=""))
    assert not any("cross-programme" in p for p in _row()["problems"])


def test_a_credit_mismatch_is_not_ready():
    """A 3-hour slot filled by a 2-hour course does not satisfy the requirement."""
    _slot(credits=3)
    _map(_option(code="SHORT401", credits=2))
    r = _row()
    assert r["ready"] is False
    assert any("credit mismatch" in p for p in r["problems"])


def test_only_declared_elective_types_are_slots():
    """Issue #55, at the gate: no code-shape inference anywhere near this."""
    _slot(code="GS104", type_="Mandatory")
    _slot(code="FE9", type_="Free Elective")
    codes = {r["slot"] for r in readiness(YEAR, TERM) if r["programme"] == PROG}
    assert "GS104" not in codes, "a Mandatory course was treated as an elective slot"
    assert "FE9" in codes


def test_a_mapping_published_for_another_term_does_not_count():
    """The model is term-scoped and the first version of this report was not.

    Without the filter, a slot mapped for last year reads as ready and the screen
    ships showing options that are not on offer.
    """
    _slot()
    _map(_option(), year="1447", term="2")
    r = _row()
    assert r["mapping_exists"] is False, "a past term's mapping was counted as current"
    assert r["ready"] is False
    assert next(x for x in readiness("1447", "2") if x["slot"] == "RE1")["ready"] is True


def test_the_gate_can_fail_a_build():
    """`--fail-on-unready` is what makes this a gate rather than a report."""
    from django.core.management import call_command

    _slot()  # unmapped
    with pytest.raises(SystemExit):
        call_command(
            "elective_mapping_readiness",
            "--with-students",
            "--fail-on-unready",
            "--year",
            YEAR,
            "--term",
            TERM,
        )

    _map(_option())
    call_command(
        "elective_mapping_readiness",
        "--with-students",
        "--fail-on-unready",
        "--year",
        YEAR,
        "--term",
        TERM,
    )
