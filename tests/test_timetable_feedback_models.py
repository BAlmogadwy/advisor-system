"""The timetable-feedback tables: what the database itself refuses.

Every rule here is a database constraint, so each test writes the offending row
directly, with no service in front of it, and expects the database to refuse it.
Each refusal is paired with the nearest row that must still be allowed, so a
constraint that refused everything would fail too.

Runs on SQLite locally and on PostgreSQL in the ``planner (PostgreSQL 16)`` job,
because partial unique indexes and check constraints are translated per backend.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from core.models import (
    StudentTermSection,
    TermSection,
    TimetableFeedback,
    TimetableFeedbackCounter,
    TimetableFeedbackEvent,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
    allocate_escalation_reference,
    allocate_feedback_reference,
)

pytestmark = pytest.mark.django_db

SID = 4990101
YEAR = "1448"
TERM = "1"
FINGERPRINT = "a" * 64
OPEN = ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED", "ANSWERED"]
CLOSE_REASONS = [choice for choice, _label in TimetableFeedback.CloseReason.choices]


def _case(**overrides):
    fields = {"student_id": SID, "academic_year": YEAR, "term": TERM}
    fields.update(overrides)
    return TimetableFeedback.objects.create(**fields)


# What each refusal must name. A check constraint is named by both backends; a
# unique one is named by PostgreSQL and described by its columns on SQLite.
OPEN_CASE_RULE = (
    r"uq_ttf_one_open_case_per_term"
    r"|timetable_feedback\.student_id, timetable_feedback\.academic_year, timetable_feedback\.term"
)
POINT_RULE = (
    r"uq_ttfp_point"
    r"|timetable_feedback_points\.case_id, timetable_feedback_points\.anchor_key, "
    r"timetable_feedback_points\.reason_code, timetable_feedback_points\.seen_fingerprint"
)
REFERENCE_RULE = r"reference"
#: Word-bounded: "ck_ttfp_anchor" is also the start of "ck_ttfp_anchor_key".
ANCHOR_RULE = r"ck_ttfp_anchor\b"


def _refused(make, by):
    """The database refuses the row ``make`` writes, and the rule ``by`` is why.

    Naming the rule matters: a row can break two constraints at once, and a test
    that accepted any refusal would stay green with its own constraint deleted.
    """
    with pytest.raises(IntegrityError, match=by), transaction.atomic():
        make()


SECTION_POINT = {
    "anchor_kind": "SECTION",
    "term_section_id": 11,
    "section_label": "M1",
    "anchor_key": "SECTION|11",
    "reason_code": "TIME_CLASH",
}
TIMETABLE_POINT = {
    "anchor_kind": "TIMETABLE",
    "anchor_key": "TIMETABLE",
    "reason_code": "LOAD_TOO_HIGH",
}
NO_FORECAST_POINT = {
    "anchor_kind": "NO_FORECAST",
    "anchor_key": "NO_FORECAST",
    "reason_code": "NO_FORECAST",
}


def _point(case, base, **overrides):
    fields = {"case": case, "seen_fingerprint": FINGERPRINT, **base, **overrides}
    return TimetableFeedbackPoint.objects.create(**fields)


# ---------------------------------------------------------------------------
# Case numbers
# ---------------------------------------------------------------------------


def test_case_numbers_are_ttf_year_and_five_digits_counted_per_year():
    assert allocate_feedback_reference(year=2026) == "TTF-2026-00001"
    assert allocate_feedback_reference(year=2026) == "TTF-2026-00002"
    assert allocate_feedback_reference(year=2027) == "TTF-2027-00001"
    assert TimetableFeedbackCounter.objects.get(year=2026).last_number == 2


def test_a_case_is_numbered_when_it_is_saved():
    case = _case()
    assert re.fullmatch(r"TTF-\d{4}-\d{5}", case.reference), case.reference


def test_feedback_and_adviser_cases_count_separately():
    assert allocate_feedback_reference(year=2026) == "TTF-2026-00001"
    assert allocate_escalation_reference(year=2026) == "ADV-2026-00001"
    assert allocate_feedback_reference(year=2026) == "TTF-2026-00002"


def test_a_case_number_is_never_issued_twice():
    first = _case()
    _refused(lambda: _case(student_id=SID + 1, reference=first.reference), REFERENCE_RULE)


# ---------------------------------------------------------------------------
# One open case per student and term
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", OPEN)
def test_a_second_open_case_for_the_same_term_is_refused(status):
    _case(status=status)
    _refused(lambda: _case(), OPEN_CASE_RULE)


@pytest.mark.parametrize(
    ("status", "close_reason"), [("CLOSED", "ANSWER_ACCEPTED"), ("WITHDRAWN", "")]
)
def test_a_new_case_may_open_once_the_last_one_is_finished(status, close_reason):
    _case(status=status, close_reason=close_reason)
    _case()
    assert TimetableFeedback.objects.filter(student_id=SID).count() == 2


def test_open_cases_in_other_terms_or_of_other_students_do_not_collide():
    _case()
    _case(term="2")
    _case(academic_year="1449")
    _case(student_id=SID + 1)
    assert TimetableFeedback.objects.count() == 4


def test_any_number_of_finished_cases_may_share_a_term():
    _case(status="WITHDRAWN")
    _case(status="WITHDRAWN")
    _case(status="CLOSED", close_reason="DUPLICATE")
    assert TimetableFeedback.objects.count() == 3


# ---------------------------------------------------------------------------
# Status and close reason
# ---------------------------------------------------------------------------


def test_an_unknown_status_is_refused():
    _refused(lambda: _case(status="OPEN"), "ck_ttf_status_valid")


@pytest.mark.parametrize("reason", CLOSE_REASONS)
def test_a_closed_case_carries_a_known_reason(reason):
    _case(status="CLOSED", close_reason=reason)


def test_a_closed_case_without_a_reason_is_refused():
    _refused(lambda: _case(status="CLOSED"), "ck_ttf_close_reason")


def test_a_closed_case_with_an_unknown_reason_is_refused():
    _refused(lambda: _case(status="CLOSED", close_reason="BECAUSE"), "ck_ttf_close_reason")


@pytest.mark.parametrize("status", [*OPEN, "WITHDRAWN"])
def test_only_a_closed_case_carries_a_close_reason(status):
    _refused(lambda: _case(status=status, close_reason="DUPLICATE"), "ck_ttf_close_reason")


# ---------------------------------------------------------------------------
# What a point is about
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base", [SECTION_POINT, TIMETABLE_POINT, NO_FORECAST_POINT])
def test_each_kind_of_point_is_accepted_in_its_own_shape(base):
    _point(_case(), base)


@pytest.mark.parametrize(
    "overrides",
    [
        {"term_section_id": None},
        {"section_label": ""},
    ],
)
def test_a_section_point_must_name_its_section(overrides):
    case = _case()
    _refused(lambda: _point(case, SECTION_POINT, **overrides), ANCHOR_RULE)


@pytest.mark.parametrize("base", [TIMETABLE_POINT, NO_FORECAST_POINT])
@pytest.mark.parametrize("overrides", [{"term_section_id": 11}, {"section_label": "M1"}])
def test_a_point_about_no_section_names_none(base, overrides):
    case = _case()
    _refused(lambda: _point(case, base, **overrides), ANCHOR_RULE)


def test_an_unknown_anchor_kind_is_refused():
    """An unknown kind breaks both anchor rules; either may be the one reported."""
    case = _case()
    _refused(lambda: _point(case, SECTION_POINT, anchor_kind="COURSE"), r"ck_ttfp_anchor(_key)?\b")


@pytest.mark.parametrize(
    ("base", "anchor_key"),
    [
        (SECTION_POINT, "TIMETABLE"),
        (SECTION_POINT, "11"),
        (TIMETABLE_POINT, "SECTION|11"),
        (TIMETABLE_POINT, "NO_FORECAST"),
        (NO_FORECAST_POINT, "TIMETABLE"),
    ],
)
def test_the_anchor_key_must_agree_with_the_anchor_kind(base, anchor_key):
    case = _case()
    _refused(lambda: _point(case, base, anchor_key=anchor_key), "ck_ttfp_anchor_key")


@pytest.mark.parametrize("reason", ["TIME_CLASH", "OTHER", "LOAD_TOO_HIGH"])
def test_no_timetable_carries_no_other_reason(reason):
    case = _case()
    _refused(lambda: _point(case, NO_FORECAST_POINT, reason_code=reason), "ck_ttfp_no_forecast")


@pytest.mark.parametrize("base", [SECTION_POINT, TIMETABLE_POINT])
def test_the_no_timetable_reason_belongs_to_no_other_anchor(base):
    case = _case()
    _refused(lambda: _point(case, base, reason_code="NO_FORECAST"), "ck_ttfp_no_forecast")


# ---------------------------------------------------------------------------
# The same point twice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base", [SECTION_POINT, TIMETABLE_POINT, NO_FORECAST_POINT])
def test_the_same_live_point_about_the_same_timetable_is_refused(base):
    """Including the points with no section, which is what the anchor key is for."""
    case = _case()
    _point(case, base)
    _refused(lambda: _point(case, base), POINT_RULE)


def test_the_same_point_about_a_changed_timetable_is_new_evidence():
    case = _case()
    _point(case, SECTION_POINT)
    _point(case, SECTION_POINT, seen_fingerprint="b" * 64)
    assert case.points.count() == 2


def test_a_withdrawn_point_may_be_raised_again():
    case = _case()
    withdrawn = _point(case, SECTION_POINT)
    TimetableFeedbackPoint.objects.filter(pk=withdrawn.pk).update(withdrawn_at=withdrawn.created_at)
    _point(case, SECTION_POINT)
    _point(case, SECTION_POINT, withdrawn_at=withdrawn.created_at)
    assert case.points.count() == 3


def test_another_reason_or_another_case_is_another_point():
    case = _case()
    _point(case, SECTION_POINT)
    _point(case, SECTION_POINT, reason_code="TIME_UNSUITABLE")
    _point(_case(student_id=SID + 1), SECTION_POINT)
    assert TimetableFeedbackPoint.objects.count() == 3


# ---------------------------------------------------------------------------
# Who may write what into a case's conversation
# ---------------------------------------------------------------------------


def _message(case, **fields):
    return TimetableFeedbackMessage.objects.create(case=case, body="text", **fields)


def test_a_student_writes_visibly_and_as_nobody_but_themself():
    _message(_case(), audience="STUDENT_VISIBLE", author_kind="STUDENT")


def test_a_student_message_is_never_staff_only():
    case = _case()
    _refused(
        lambda: _message(case, audience="INTERNAL", author_kind="STUDENT"),
        "ck_ttfm_student_visible",
    )


def test_a_student_message_never_carries_a_staff_account():
    case = _case()
    staff = User.objects.create_user("advisor-1")
    _refused(
        lambda: _message(case, audience="STUDENT_VISIBLE", author_kind="STUDENT", author=staff),
        "ck_ttfm_student_visible",
    )


@pytest.mark.parametrize("author_kind", ["ADVISOR", "DEPARTMENT"])
@pytest.mark.parametrize("audience", ["INTERNAL", "STUDENT_VISIBLE"])
def test_staff_write_to_either_audience(author_kind, audience):
    staff = User.objects.create_user(f"staff-{author_kind}-{audience}")
    _message(_case(), audience=audience, author_kind=author_kind, author=staff)


# ---------------------------------------------------------------------------
# Anchored by value: the timetable and the roster may change underneath
# ---------------------------------------------------------------------------


def test_deleting_the_section_and_the_students_timetable_leaves_the_feedback():
    section = TermSection.objects.create(
        source_tag="other",
        course_name="DATA STRUCTURES",
        course_code="SB",
        course_number="201",
        course_key="SB201",
        section="M1",
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=section,
        source="registration_plan_1448_t1",
    )
    case = _case()
    point = _point(
        case,
        SECTION_POINT,
        term_section_id=section.pk,
        anchor_key=f"SECTION|{section.pk}",
        seen_snapshot={"sections": [{"term_section_id": section.pk, "section": "M1"}]},
    )

    section_id = section.pk
    section.delete()  # cascades to the student's StudentTermSection row

    assert not TermSection.objects.filter(pk=section_id).exists()
    assert not StudentTermSection.objects.filter(student_id=SID).exists()
    point.refresh_from_db()
    assert point.term_section_id == section_id
    assert point.seen_snapshot["sections"][0]["section"] == "M1"
    assert TimetableFeedback.objects.filter(pk=case.pk).exists()


def test_deleting_a_staff_account_keeps_the_case_its_thread_and_its_history():
    staff = User.objects.create_user("advisor-2")
    case = _case(handled_by=staff, handled_by_username="advisor-2")
    _message(case, audience="INTERNAL", author_kind="ADVISOR", author=staff, author_username="a2")
    TimetableFeedbackEvent.objects.create(
        case=case, kind="TAKEN", actor=staff, actor_username="advisor-2"
    )

    staff.delete()

    case.refresh_from_db()
    assert case.handled_by_id is None and case.handled_by_username == "advisor-2"
    message = case.messages.get()
    assert message.author_id is None and message.author_username == "a2"
    event = case.events.get()
    assert event.actor_id is None and event.actor_username == "advisor-2"


def test_deleting_a_case_takes_its_points_thread_and_history_with_it():
    case = _case()
    _point(case, TIMETABLE_POINT)
    _message(case, audience="STUDENT_VISIBLE", author_kind="STUDENT")
    TimetableFeedbackEvent.objects.create(case=case, kind="SUBMITTED")

    case.delete()

    assert not TimetableFeedbackPoint.objects.exists()
    assert not TimetableFeedbackMessage.objects.exists()
    assert not TimetableFeedbackEvent.objects.exists()


# ---------------------------------------------------------------------------
# The anchor key is exactly its anchor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor_key", ["SECTION|12", "SECTION|", "SECTION|011", "SECTION|11 ", "section|11"]
)
def test_a_section_anchor_key_is_exactly_its_own_section(anchor_key):
    """Anything else would let two keys for one section both pass the point rule."""
    case = _case()
    _refused(lambda: _point(case, SECTION_POINT, anchor_key=anchor_key), "ck_ttfp_anchor_key")


def test_a_section_id_beyond_32_bits_is_stored_and_anchored_exactly():
    """TermSection's key is a big integer, so the copies of it are too."""
    big = 2**31 + 7
    case = _case()
    point = _point(
        case,
        SECTION_POINT,
        term_section_id=big,
        anchor_key=f"SECTION|{big}",
        requested_term_section_id=big + 1,
    )
    point.refresh_from_db()
    assert (point.term_section_id, point.requested_term_section_id) == (big, big + 1)
    _refused(
        lambda: _point(
            case,
            SECTION_POINT,
            term_section_id=big,
            anchor_key=f"SECTION|{big + 1}",
            reason_code="OTHER",
        ),
        "ck_ttfp_anchor_key",
    )


def test_two_sections_may_carry_the_same_reason_about_the_same_timetable():
    """Both sides of one clash, for instance: two points, not a duplicate."""
    case = _case()
    _point(case, SECTION_POINT)
    _point(case, SECTION_POINT, term_section_id=12, anchor_key="SECTION|12")
    assert case.points.count() == 2


def test_a_section_and_the_whole_timetable_may_both_say_other():
    case = _case()
    _point(case, SECTION_POINT, reason_code="OTHER")
    _point(case, TIMETABLE_POINT, reason_code="OTHER")
    assert case.points.count() == 2


# ---------------------------------------------------------------------------
# Known values only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", TimetableFeedbackPoint.Reason.values)
def test_every_known_reason_is_accepted(reason):
    base = NO_FORECAST_POINT if reason == "NO_FORECAST" else TIMETABLE_POINT
    _point(_case(), base, reason_code=reason)


def test_an_unknown_reason_is_refused():
    case = _case()
    _refused(lambda: _point(case, TIMETABLE_POINT, reason_code="BOGUS"), "ck_ttfp_reason_valid")


def test_an_unknown_audience_is_refused():
    case = _case()
    _refused(
        lambda: _message(case, audience="student_visible", author_kind="ADVISOR"),
        "ck_ttfm_audience_valid",
    )


@pytest.mark.parametrize("author_kind", ["student", ""])
def test_an_unknown_author_kind_cannot_slip_a_student_note_into_staff_only(author_kind):
    """A misspelt STUDENT would otherwise pass the visibility rule, which compares
    against the exact value."""
    case = _case()
    _refused(
        lambda: _message(case, audience="INTERNAL", author_kind=author_kind),
        "ck_ttfm_author_kind_valid",
    )


@pytest.mark.parametrize("kind", TimetableFeedbackEvent.Kind.values)
def test_every_known_event_kind_is_accepted(kind):
    TimetableFeedbackEvent.objects.create(case=_case(), kind=kind)


def test_an_unknown_event_kind_is_refused():
    case = _case()
    _refused(
        lambda: TimetableFeedbackEvent.objects.create(case=case, kind="BOGUS"),
        "ck_ttfe_kind_valid",
    )


def test_an_event_names_the_point_and_the_message_it_is_about():
    case = _case()
    point = _point(case, TIMETABLE_POINT)
    message = _message(case, audience="STUDENT_VISIBLE", author_kind="STUDENT")
    TimetableFeedbackEvent.objects.create(
        case=case, kind="POINT_ADDED", point_id=point.pk, message_id=message.pk
    )
    event = case.events.get()
    assert (event.point_id, event.message_id) == (point.pk, message.pk)


# ---------------------------------------------------------------------------
# Text copied from unbounded columns is stored whole
# ---------------------------------------------------------------------------

#: Longer than any bounded column here. Some advisor ids are whole Arabic names.
LONG = "مرشد أكاديمي باسم طويل جدا " * 40


def test_text_copied_from_unbounded_sources_is_stored_whole():
    """SQLite never enforces a length; PostgreSQL would refuse a bounded copy.

    So this test only bites in the PostgreSQL job, which runs this file.
    """
    case = _case(
        handled_advisor_id=LONG,
        assigned_advisor_id=LONG,
        routed_advisor_id=LONG,
        program_at_submission=LONG,
        standing_at_submission=LONG,
    )
    point = _point(
        case,
        SECTION_POINT,
        course_key=LONG,
        section_label=LONG,
        section_course_identity=LONG,
        plan_course_identity=LONG,
        requested_section_label=LONG,
        requested_course_key=LONG,
        requested_plan_course_identity=LONG,
    )
    message = _message(case, audience="INTERNAL", author_kind="ADVISOR", author_label=LONG)
    event = TimetableFeedbackEvent.objects.create(
        case=case,
        kind="ASSIGNED",
        actor_label=LONG,
        actor_advisor_id=LONG,
        from_value=LONG,
        to_value=LONG,
    )

    case.refresh_from_db()
    point.refresh_from_db()
    message.refresh_from_db()
    event.refresh_from_db()
    assert {case.handled_advisor_id, case.assigned_advisor_id, case.routed_advisor_id} == {LONG}
    assert {case.program_at_submission, case.standing_at_submission} == {LONG}
    assert {
        point.course_key,
        point.section_label,
        point.section_course_identity,
        point.plan_course_identity,
        point.requested_section_label,
        point.requested_course_key,
        point.requested_plan_course_identity,
    } == {LONG}
    assert message.author_label == LONG
    assert {event.actor_label, event.actor_advisor_id, event.from_value, event.to_value} == {LONG}
