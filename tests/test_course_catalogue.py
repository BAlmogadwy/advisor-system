"""The catalogue is ONE load: the floor and the resolver's index cannot skew.

An adversarial review broke the previous design deterministically: two
independently-warmed TTL caches over the same tables, so after an
uninvalidated write (admin delete, manage.py shell, a data migration) the
resolver could offer a candidate the existence floor had already dropped -
re-opening the laundering hole the resolver's admissibility rests on.  The
candidates-are-catalogue-real invariant is now structural (candidates are
keys of the very mapping the floor is built from), and these tests pin it
through the resolver's REAL output, plus the two normalisation gaps found in
the same review: Arabic-Indic digits in a catalogue row, and rows whose every
source has a blank name.
"""

from __future__ import annotations

import pytest

from core.models import Course, ProgrammeRequirement
from core.services.course_catalogue import (
    invalidate_cache,
    known_course_codes,
    known_courses,
    normalise_catalogue_code,
)
from core.services.virtual_advisor_capabilities import _did_you_mean

pytestmark = pytest.mark.django_db


def test_resolver_candidates_stay_inside_the_floor_even_across_a_stale_write():
    """The reviewer's reproduction, on the fixed design.

    Warm the resolver's index, delete a course WITHOUT invalidating, and the
    floor must still recognise every candidate the resolver offers - the two
    views may be stale together, never split.
    """
    Course.objects.create(course_code="MATH243", description="Discrete Mathematics")
    invalidate_cache()
    assert dict(known_courses())["MATH243"] == "Discrete Mathematics"

    Course.objects.all().delete()  # deliberately NOT invalidated

    candidates = {row["candidate_code"] for row in _did_you_mean("MATE243")}
    assert candidates == {"MATH243"}
    assert candidates <= known_course_codes()


def test_the_two_views_are_one_load():
    Course.objects.create(course_code="AI331", description="NLP")
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="STAT305",
        course_name="",
        type="Mandatory",
        programme_term=5,
        credit_hours=3,
    )
    invalidate_cache()
    assert {code for code, _name in known_courses()} == set(known_course_codes())


def test_arabic_indic_digits_reach_the_floor_in_the_checkers_spelling():
    """«MATH٢٤٣» stripped digits before folding them and became "MATH" - a
    permanently "invented" row.  Fold first, like the checker does."""
    from core.services.answer_consistency import _normalise_course_token

    Course.objects.create(course_code="MATH٢٤٣", description="Discrete Mathematics")
    invalidate_cache()

    assert normalise_catalogue_code("MATH٢٤٣") == "MATH243"
    assert "MATH243" in known_course_codes()
    # Parity with the checker's own token normalisation - the comparison the
    # floor's membership test actually performs.
    assert _normalise_course_token("MATH٢٤٣") == "MATH243"


def test_an_empty_name_requirement_row_is_still_repairable():
    """STAT305 with a blank name everywhere was on the floor but absent from
    the resolver's index, so STAP305 got no repair.  One load closes that:
    the key survives with an empty display name."""
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="STAT305",
        course_name="",
        type="Mandatory",
        programme_term=5,
        credit_hours=3,
    )
    invalidate_cache()

    assert "STAT305" in known_course_codes()
    rows = _did_you_mean("STAP305")
    assert [row["candidate_code"] for row in rows] == ["STAT305"]
    assert rows[0]["candidate_name"] == ""


def test_a_non_empty_name_from_any_source_beats_a_blank_one():
    """Priority stays requirement -> elective -> Course, but a blank never
    shadows a real name a later source carries."""
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="STAT305",
        course_name="",
        type="Mandatory",
        programme_term=5,
        credit_hours=3,
    )
    Course.objects.create(course_code="STAT305", description="Probability & Statistics")
    invalidate_cache()

    assert dict(known_courses())["STAT305"] == "Probability & Statistics"
