"""Slice C (server half): the workspace SHOWS teaching-time rule violations.

Display only: a violation is a warning, never a refusal, and with the flag off or no
rule binding a charged teacher every answer is byte-identical.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import Instructor, ProgrammeRequirement
from core.services import timetable_instructor_availability as helper
from core.services.course_identity import planner_course_key
from core.services.instructor_availability import (
    KIND_UNAVAILABLE,
    TimeRule,
    compile_rules,
    mask_from_days,
)
from tests import one_teacher_support as ots
from tests._instructor_availability_fixture import AVAILABILITY_FLAGS, rule
from tests.test_instructor_availability_engine import _engine_row, _english_board, _sunday


@pytest.fixture
def av_on(settings):
    for name, value in AVAILABILITY_FLAGS.items():
        setattr(settings, name, value)
    return settings


def build_ruled(*, ruled: str = "a"):
    """AI111 (2 sections): S1 placed SUN+TUE 13:00-14:15, S2 not placed, both charged
    to Dr A by an engine row; Dr B is eligible but never charged. ``ruled`` names the
    teacher with the "no mornings" rule."""
    sc, board = ots.scenario("ruled", programs=("AI",))
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="AI111",
        course_name="INTRO",
        programme_term=2,
        credit_hours=3,
    )
    key = planner_course_key("AI111", "INTRO")
    ots.budget(sc, key, 2, code="AI111")
    a, b = ots.instructor("Dr A"), ots.instructor("Dr B")
    ots.link("AI", "AI111", a, role="primary")
    ots.link("AI", "AI111", b, role="co")
    s1 = ots.section(sc, key, "S1")
    s2 = ots.section(sc, key, "S2")
    ots.place(board, s1, [("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15")])
    _engine_row(sc, key, "S1", a)
    _engine_row(sc, key, "S2", a)
    rule({"a": a, "b": b}[ruled], KIND_UNAVAILABLE, end="13:00")
    return sc, board, key, s1, s2, a, b


def _plain_checker(charged, exempt=frozenset()):
    ruled = compile_rules([TimeRule(1, KIND_UNAVAILABLE, mask_from_days(("SUN",)), 0, 13 * 60)])
    return helper.RuleChecker(ruled, charged, exempt)


# ── Commit 1: the helper's display methods ────────────────────────────────


@pytest.mark.django_db
def test_meeting_blocked_judges_one_meeting_without_the_bundle(av_on) -> None:
    """H-1: an English section's writers' check expands to the whole bundle; the
    display judges each physical meeting alone."""
    sc, _key, a = _english_board("h-1")
    checker = helper.display_rule_checker(sc.id)
    sunday = _sunday(sc, _key)

    assert checker.blocked(sunday, "SUN", "13:00", "16:00") == (a.pk,)  # the bundle has THU
    assert checker.meeting_blocked((a.pk,), "SUN", "13:00", "16:00") == ()
    assert checker.meeting_blocked((a.pk,), "THU", "13:00", "16:00") == (a.pk,)
    assert checker.meeting_blocked((a.pk,), "THU", "12:00", "13:00") == ()  # touching
    assert checker.meeting_blocked((a.pk,), "THU", "bad", "16:00") == ()  # does not read
    assert checker.meeting_blocked((), "THU", "13:00", "16:00") == ()


def test_section_ids_follow_the_charged_teacher_and_the_exemption() -> None:
    """H-2: only a CHARGED, ruled teacher counts; an exempt section has none."""
    checker = _plain_checker(
        {"K_S1": frozenset({1}), "K_S3": frozenset({1}), "K_S4": frozenset({2})},
        frozenset({("K", "S3")}),
    )
    assert checker.section_ids("K", "S1") == (1,)
    assert checker.section_ids("K", "TBD") == ()  # charged to nobody
    assert checker.section_ids("K", "S4") == ()  # charged teacher has no rule
    assert checker.section_ids("K", "S3") == ()  # online on every board
    assert checker.section_ids("K", "S3", exempt=False) == (1,)  # the caller decides
    assert checker.section_ids("K", "S1", exempt=True) == ()


@pytest.mark.django_db
def test_binds_only_when_a_charged_teacher_has_a_rule(av_on) -> None:
    """H-3: a rule on an eligible-but-never-charged teacher makes a checker exist
    but does not bind; the display door then answers None."""
    sc, *_rest = build_ruled(ruled="b")
    assert helper.section_rule_checker(sc.id).binds is False
    assert helper.display_rule_checker(sc.id) is None

    from core.models import InstructorTimeRule

    InstructorTimeRule.objects.all().delete()
    ruled_a = Instructor.objects.get(full_name="Dr A")
    rule(ruled_a, KIND_UNAVAILABLE, end="13:00")
    checker = helper.display_rule_checker(sc.id)
    assert checker is not None and checker.binds
    assert checker.ruled_charged == frozenset({ruled_a.pk})


@pytest.mark.django_db
def test_teacher_names_are_read_once_per_checker(av_on) -> None:
    """H-4."""
    sc, _board, _key, _s1, _s2, a, _b = build_ruled()
    checker = helper.display_rule_checker(sc.id)
    with CaptureQueriesContext(connection) as captured:
        assert checker.teacher_name(a.pk) == "Dr A"
        assert checker.teacher_name(a.pk) == "Dr A"
        assert checker.teacher_name(999999) == ""
    assert sum('FROM "instructors"' in q["sql"] for q in captured) == 1
