"""The timetable-feedback state machine, the claim, and the student's side of a case.

Every refusal is asserted by its code, and every write by what it left behind
(the status, the version, the message, the event), so a test cannot pass because
some other check refused first or because nothing was written at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from core.models import (
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
)
from core.services import rate_limit
from core.services import timetable_feedback_workflow as workflow

pytestmark = pytest.mark.django_db

SID = 4993001
OTHER_SID = 4993002
STATUSES = [
    "SUBMITTED",
    "IN_REVIEW",
    "NEEDS_INFORMATION",
    "FORWARDED",
    "ANSWERED",
    "CLOSED",
    "WITHDRAWN",
]
OPEN = ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED", "ANSWERED"]


def _case(status="SUBMITTED", student_id=SID, **fields):
    if status == "CLOSED":
        fields.setdefault("close_reason", "DUPLICATE")
    return TimetableFeedback.objects.create(
        student_id=student_id, academic_year="1448", term="1", status=status, **fields
    )


def _point(case, section_id=11, reason="TIME_CLASH"):
    return TimetableFeedbackPoint.objects.create(
        case=case,
        anchor_kind="SECTION",
        term_section_id=section_id,
        section_label="M1",
        anchor_key=f"SECTION|{section_id}",
        reason_code=reason,
        seen_fingerprint="a" * 64,
    )


def _refused(code, action, **kwargs):
    with pytest.raises(workflow.WorkflowRefused) as raised:
        action(**kwargs)
    assert raised.value.code == code, raised.value.code
    return raised.value


def _state(case):
    case.refresh_from_db()
    return case.status, case.version


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

EXPECTED = {
    "STAFF": {
        "SUBMITTED": {"IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED", "ANSWERED", "CLOSED"},
        "IN_REVIEW": {"NEEDS_INFORMATION", "FORWARDED", "ANSWERED", "CLOSED"},
        "NEEDS_INFORMATION": {"IN_REVIEW", "ANSWERED", "CLOSED"},
        "FORWARDED": {"IN_REVIEW", "ANSWERED", "CLOSED"},
        "ANSWERED": {"CLOSED"},
        "CLOSED": set(),
        "WITHDRAWN": set(),
    },
    "STUDENT": {
        "SUBMITTED": {"WITHDRAWN"},
        "IN_REVIEW": {"WITHDRAWN"},
        "FORWARDED": {"WITHDRAWN"},
        "NEEDS_INFORMATION": {"IN_REVIEW", "SUBMITTED", "WITHDRAWN"},
        "ANSWERED": {"IN_REVIEW", "SUBMITTED", "CLOSED", "WITHDRAWN"},
        "CLOSED": set(),
        "WITHDRAWN": set(),
    },
    "SYSTEM": {
        **{status: {"CLOSED"} for status in OPEN},
        "CLOSED": set(),
        "WITHDRAWN": set(),
    },
}


@pytest.mark.parametrize("actor", ["STAFF", "STUDENT", "SYSTEM"])
@pytest.mark.parametrize("from_status", STATUSES)
@pytest.mark.parametrize("to_status", STATUSES)
def test_every_move_is_allowed_exactly_when_its_actors_table_says(actor, from_status, to_status):
    expected = to_status in EXPECTED[actor][from_status]
    assert workflow.may_move(actor, from_status, to_status) is expected


def test_only_the_student_withdraws_and_only_staff_answer():
    assert not any(workflow.may_move("STAFF", s, "WITHDRAWN") for s in STATUSES)
    assert not any(workflow.may_move("SYSTEM", s, "WITHDRAWN") for s in STATUSES)
    for status in STATUSES:
        assert not workflow.may_move("STUDENT", status, "ANSWERED")
        assert not workflow.may_move("STUDENT", status, "NEEDS_INFORMATION")
        assert not workflow.may_move("STUDENT", status, "FORWARDED")


def test_a_reopened_case_goes_to_its_handler_or_back_to_the_queue():
    staff = User.objects.create_user("advisor-target")
    assert workflow.reopen_target(_case("ANSWERED")) == "SUBMITTED"
    assert workflow.reopen_target(_case("ANSWERED", student_id=OTHER_SID, handled_by=staff)) == (
        "IN_REVIEW"
    )


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def _scope(student_id=SID):
    return TimetableFeedback.objects.filter(student_id=student_id)


def test_a_claim_moves_the_case_and_bumps_its_version():
    case = _case("ANSWERED")
    workflow.cas_transition(
        _scope(),
        case,
        actor="STUDENT",
        now=timezone.now(),
        to_status="CLOSED",
        close_reason="OTHER",
    )
    assert _state(case) == ("CLOSED", 1)


def test_a_claim_without_a_move_only_bumps_the_version():
    case = _case("IN_REVIEW")
    workflow.cas_transition(_scope(), case, actor="STUDENT", now=timezone.now())
    assert _state(case) == ("IN_REVIEW", 1)


def test_a_claim_on_a_stale_version_is_refused():
    case = _case()
    TimetableFeedback.objects.filter(pk=case.pk).update(version=5)
    _refused(
        "CASE_CHANGED",
        workflow.cas_transition,
        scoped=_scope(),
        case=case,
        actor="STUDENT",
        now=timezone.now(),
        to_status="WITHDRAWN",
    )
    assert _state(case) == ("SUBMITTED", 5)


def test_a_claim_on_a_case_whose_status_changed_is_refused():
    case = _case()
    TimetableFeedback.objects.filter(pk=case.pk).update(status="IN_REVIEW")
    _refused(
        "CASE_CHANGED",
        workflow.cas_transition,
        scoped=_scope(),
        case=case,
        actor="STUDENT",
        now=timezone.now(),
        to_status="WITHDRAWN",
    )
    assert _state(case) == ("IN_REVIEW", 0)


def test_a_claim_through_another_students_scope_matches_nothing():
    theirs = _case(student_id=OTHER_SID)
    _refused(
        "CASE_CHANGED",
        workflow.cas_transition,
        scoped=_scope(SID),
        case=theirs,
        actor="STUDENT",
        now=timezone.now(),
        to_status="WITHDRAWN",
    )
    assert _state(theirs) == ("SUBMITTED", 0)


def test_a_move_the_actor_may_not_make_is_refused_before_anything_is_written():
    case = _case()
    _refused(
        "NOT_ALLOWED",
        workflow.cas_transition,
        scoped=_scope(),
        case=case,
        actor="STUDENT",
        now=timezone.now(),
        to_status="ANSWERED",
    )
    assert _state(case) == ("SUBMITTED", 0)


def test_a_status_passed_among_the_changes_is_refused_outright():
    """Otherwise it would reach the UPDATE without the actor's table being asked."""
    case = _case()
    with pytest.raises(TypeError):
        workflow.cas_transition(
            _scope(), case, actor="STUDENT", now=timezone.now(), status="ANSWERED"
        )
    assert _state(case) == ("SUBMITTED", 0)


def test_closing_without_a_reason_is_refused_before_the_database_is_asked():
    case = _case("ANSWERED")
    with pytest.raises(ValueError, match="close_reason"):
        workflow.cas_transition(
            _scope(), case, actor="STAFF", now=timezone.now(), to_status="CLOSED"
        )
    assert _state(case) == ("ANSWERED", 0)


@pytest.mark.parametrize("to_status", ["IN_REVIEW", None])
def test_a_close_reason_without_closing_is_refused_before_the_database_is_asked(to_status):
    case = _case("SUBMITTED")
    with pytest.raises(ValueError, match="close_reason"):
        workflow.cas_transition(
            _scope(),
            case,
            actor="STAFF",
            now=timezone.now(),
            to_status=to_status,
            close_reason="DUPLICATE",
        )
    assert _state(case) == ("SUBMITTED", 0)


def test_an_empty_close_reason_is_no_reason_at_all():
    case = _case("SUBMITTED")
    workflow.cas_transition(
        _scope(), case, actor="STAFF", now=timezone.now(), to_status="IN_REVIEW", close_reason=""
    )
    assert _state(case) == ("IN_REVIEW", 1)


def test_a_claim_through_a_scope_that_joins_another_table_keeps_to_that_scope():
    """Staff will claim through scopes like "cases handled by active staff". The
    PostgreSQL race file proves such a claim still refuses a stale version."""
    active, retired = User.objects.create_user("advisor-on"), User.objects.create_user("gone")
    User.objects.filter(pk=retired.pk).update(is_active=False)
    mine = _case("SUBMITTED", handled_by=active)
    theirs = _case("SUBMITTED", student_id=OTHER_SID, handled_by=retired)
    joined = TimetableFeedback.objects.filter(handled_by__is_active=True)

    workflow.cas_transition(joined, mine, actor="STAFF", now=timezone.now(), to_status="IN_REVIEW")
    _refused(
        "CASE_CHANGED",
        workflow.cas_transition,
        scoped=joined,
        case=theirs,
        actor="STAFF",
        now=timezone.now(),
        to_status="IN_REVIEW",
    )

    assert (_state(mine), _state(theirs)) == (("IN_REVIEW", 1), ("SUBMITTED", 0))


# ---------------------------------------------------------------------------
# What a student may write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "BODY_REQUIRED"),
        ("   ", "BODY_REQUIRED"),
        ("x" * (workflow.STUDENT_TEXT_MAX_CHARS + 1), "BODY_TOO_LONG"),
        ("a" + chr(0) + "b", "BODY_INVALID"),
        ("a" + chr(27) + "b", "BODY_INVALID"),
        # Shows nothing: zero-width spaces, a direction mark, a byte-order mark.
        (chr(0x200B) + chr(0x200B), "BODY_REQUIRED"),
        (chr(0xFEFF) + " " + chr(0x200F), "BODY_REQUIRED"),
        # A lone surrogate, as a JSON body can carry: no driver can encode it.
        ("ok " + chr(0xD800), "BODY_INVALID"),
        # Blank-looking letters and symbols, and combining marks with no letter.
        (chr(0x3164) + chr(0x115F), "BODY_REQUIRED"),
        (chr(0x2800) * 3, "BODY_REQUIRED"),
        (chr(0xFE0F), "BODY_REQUIRED"),
        (chr(0x034F) + chr(0x064E), "BODY_REQUIRED"),
        (chr(0x20DD), "BODY_REQUIRED"),
        # DEL and the C1 controls: nothing on their own, and never storable.
        (chr(0x7F), "BODY_REQUIRED"),
        (chr(0x80) + chr(0x81), "BODY_REQUIRED"),
        ("a" + chr(0x7F) + "b", "BODY_INVALID"),
        ("a" + chr(0x85) + "b", "BODY_INVALID"),
        ("a" + chr(0x9F) + "b", "BODY_INVALID"),
    ],
)
def test_student_text_is_refused_unless_visible_bounded_and_storable(text, code):
    _refused(code, workflow.clean_student_text, text=text)


@pytest.mark.parametrize(
    "code_point",
    # Listed here, not read from the module: a test that takes its cases from the
    # set under test shrinks with it.
    [0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800],
)
def test_every_blank_looking_character_shows_nothing(code_point):
    assert not workflow.visible(chr(code_point) * 2)
    assert workflow.visible(chr(code_point) + "x")


def test_student_text_keeps_its_line_breaks_its_marks_and_its_full_length():
    text = "line one" + chr(10) + "line" + chr(9) + "two"
    assert workflow.clean_student_text(text) == text
    longest = "x" * workflow.STUDENT_TEXT_MAX_CHARS
    assert workflow.clean_student_text(longest) == longest
    marked = chr(0x200F) + "مرحبا" + chr(0x200E)
    assert workflow.clean_student_text(marked) == marked


@pytest.mark.parametrize(
    "text",
    [
        # Arabic with tashkeel and tatweel, and Arabic-Indic digits.
        chr(0x0645) + chr(0x064E) + chr(0x0640) + chr(0x0631) + chr(0x062D) + chr(0x0628),
        chr(0x0661) + chr(0x0662) + chr(0x0663),
        # A zero-width non-joiner inside a word.
        "mi" + chr(0x200C) + "crowave",
        # An emoji with its variation selector, a ZWJ family, a flag, a keycap.
        "ok " + chr(0x2764) + chr(0xFE0F),
        chr(0x1F468) + chr(0x200D) + chr(0x1F469) + chr(0x200D) + chr(0x1F467),
        chr(0x1F1F8) + chr(0x1F1E6),
        "1" + chr(0xFE0F) + chr(0x20E3),
        # Direction isolates around a course code, and an accented letter.
        chr(0x2068) + "CS112" + chr(0x2069),
        "cafe" + chr(0x0301),
    ],
)
def test_legitimate_text_in_either_language_is_kept(text):
    assert workflow.clean_student_text(text) == text


def test_student_text_is_trimmed_before_it_is_measured_and_kept():
    padding = "  " + chr(10) + chr(9)
    assert workflow.clean_student_text(padding + "Thanks." + padding) == "Thanks."
    longest = "x" * workflow.STUDENT_TEXT_MAX_CHARS
    assert workflow.clean_student_text(" " + longest + chr(10)) == longest


def test_text_no_driver_can_encode_is_refused_before_anything_is_written():
    case = _case("SUBMITTED")
    _refused(
        "BODY_INVALID",
        workflow.follow_up,
        student_id=SID,
        reference=case.reference,
        body="clash " + chr(0xDC00),
    )
    assert _state(case) == ("SUBMITTED", 0)
    assert not TimetableFeedbackMessage.objects.exists()


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("شكرا لك", "ar"),
        ("thank you", "en"),
        ("شكرا thanks", "mixed"),
        ("2026 !", ""),
        # A heart emoji carries a variation selector, and pasted text a BOM:
        # neither is an Arabic letter, whatever block it sits in.
        ("thanks " + chr(0x2764) + chr(0xFE0F), "en"),
        (chr(0xFEFF) + "hello", "en"),
        # Arabic letters outside the main block still count...
        (chr(0xFEFB), "ar"),
        (chr(0x0750), "ar"),
        # ...but Arabic-Indic digits and harakat are not letters.
        (chr(0x0661) + chr(0x0662) + " ok", "en"),
        (chr(0x0645) + chr(0x064E), "ar"),
    ],
)
def test_the_script_of_a_message_is_recorded_for_staff(text, script):
    assert workflow.body_script(text) == script


# ---------------------------------------------------------------------------
# Replying to a request for information
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("handled", "target"), [(False, "SUBMITTED"), (True, "IN_REVIEW")])
def test_a_reply_answers_the_request_and_returns_the_case(handled, target):
    staff = User.objects.create_user("advisor-reply") if handled else None
    case = _case("NEEDS_INFORMATION", handled_by=staff)

    message = workflow.reply(student_id=SID, reference=case.reference, body="Here it is.")

    assert _state(case) == (target, 1)
    assert (message.audience, message.author_kind, message.author_id) == (
        "STUDENT_VISIBLE",
        "STUDENT",
        None,
    )
    assert (message.body, message.body_script) == ("Here it is.", "en")
    event = case.events.get()
    assert (event.kind, event.message_id, event.from_value, event.to_value) == (
        "STUDENT_REPLY",
        message.pk,
        "NEEDS_INFORMATION",
        target,
    )
    assert case.last_student_activity_at is not None


@pytest.mark.parametrize("status", [s for s in STATUSES if s != "NEEDS_INFORMATION"])
def test_a_reply_is_only_an_answer_to_a_request(status):
    case = _case(status)
    _refused("NOT_ALLOWED", workflow.reply, student_id=SID, reference=case.reference, body="hi")
    assert _state(case) == (status, 0)
    assert not TimetableFeedbackMessage.objects.exists()


# ---------------------------------------------------------------------------
# Following up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW", "FORWARDED"])
def test_a_follow_up_is_added_while_the_case_is_handled(status):
    case = _case(status)
    message = workflow.follow_up(student_id=SID, reference=case.reference, body="One more thing.")
    assert _state(case) == (status, 1)
    event = case.events.get()
    assert (event.kind, event.message_id) == ("STUDENT_FOLLOWUP", message.pk)


@pytest.mark.parametrize("status", ["NEEDS_INFORMATION", "ANSWERED", "CLOSED", "WITHDRAWN"])
def test_a_follow_up_is_refused_where_a_reply_or_a_decision_is_wanted(status):
    case = _case(status)
    _refused("NOT_ALLOWED", workflow.follow_up, student_id=SID, reference=case.reference, body="hi")
    assert _state(case) == (status, 0)


def test_a_case_takes_five_follow_ups_and_replies_do_not_count(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (100, 3600))
    case = _case("NEEDS_INFORMATION")
    workflow.reply(student_id=SID, reference=case.reference, body="answering")
    for n in range(workflow.MAX_FOLLOW_UPS):
        workflow.follow_up(student_id=SID, reference=case.reference, body=f"follow-up {n}")
    _refused(
        "TOO_MANY_FOLLOW_UPS",
        workflow.follow_up,
        student_id=SID,
        reference=case.reference,
        body="one too many",
    )


# ---------------------------------------------------------------------------
# Withdrawing a point, or the case
# ---------------------------------------------------------------------------


def test_withdrawing_a_point_keeps_the_others():
    case = _case()
    first, second = _point(case, 11), _point(case, 12)

    workflow.withdraw_point(student_id=SID, reference=case.reference, point_id=str(first.pk))

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.withdrawn_at is not None and second.withdrawn_at is None
    assert _state(case) == ("SUBMITTED", 1)
    event = case.events.get()
    assert (event.kind, event.point_id) == ("POINT_WITHDRAWN", first.pk)


def test_the_last_live_point_is_not_withdrawn_alone():
    case = _case()
    first, second = _point(case, 11), _point(case, 12)
    TimetableFeedbackPoint.objects.filter(pk=first.pk).update(withdrawn_at=timezone.now())
    _refused(
        "LAST_POINT",
        workflow.withdraw_point,
        student_id=SID,
        reference=case.reference,
        point_id=str(second.pk),
    )
    second.refresh_from_db()
    assert second.withdrawn_at is None


@pytest.mark.parametrize("which", ["not_a_uuid", "other_case", "already_withdrawn"])
def test_a_point_that_is_not_this_cases_live_point_is_not_found(which):
    case = _case()
    _point(case, 11)
    mine = _point(case, 12)
    theirs = _point(_case(student_id=OTHER_SID), 13)
    if which == "not_a_uuid":
        point_id = "123"
    elif which == "other_case":
        point_id = str(theirs.pk)
    else:
        TimetableFeedbackPoint.objects.filter(pk=mine.pk).update(withdrawn_at=timezone.now())
        point_id = str(mine.pk)
    _refused(
        "POINT_NOT_FOUND",
        workflow.withdraw_point,
        student_id=SID,
        reference=case.reference,
        point_id=point_id,
    )


@pytest.mark.parametrize("status", ["ANSWERED", "CLOSED", "WITHDRAWN"])
def test_no_point_is_withdrawn_once_the_case_is_answered_or_finished(status):
    """An answer covers the case as it stood; afterwards the case is accepted,
    reopened or withdrawn whole. Withdrawing a point would also move the time an
    answer without ``answered_at`` is measured from."""
    case = _case(status, answered_at=timezone.now())
    first = _point(case, 11)
    _point(case, 12)
    _refused(
        "NOT_ALLOWED",
        workflow.withdraw_point,
        student_id=SID,
        reference=case.reference,
        point_id=str(first.pk),
    )
    first.refresh_from_db()
    assert first.withdrawn_at is None
    assert _state(case) == (status, 0)


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED"])
def test_a_point_may_be_withdrawn_in_any_status_before_the_answer(status):
    case = _case(status)
    first = _point(case, 11)
    _point(case, 12)
    workflow.withdraw_point(student_id=SID, reference=case.reference, point_id=str(first.pk))
    first.refresh_from_db()
    assert first.withdrawn_at is not None


@pytest.mark.parametrize("status", OPEN)
def test_a_case_may_be_withdrawn_from_any_open_status(status):
    case = _case(status)
    now = timezone.now()
    workflow.withdraw_case(student_id=SID, reference=case.reference, now=now)
    case.refresh_from_db()
    assert (case.status, case.closed_at, case.version) == ("WITHDRAWN", now, 1)
    event = case.events.get()
    assert (event.kind, event.from_value, event.to_value) == ("WITHDRAWN", status, "WITHDRAWN")


@pytest.mark.parametrize("status", ["CLOSED", "WITHDRAWN"])
def test_a_finished_case_is_not_withdrawn(status):
    case = _case(status)
    _refused("NOT_ALLOWED", workflow.withdraw_case, student_id=SID, reference=case.reference)


# ---------------------------------------------------------------------------
# Accepting or reopening an answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("helpful", [True, False, None])
def test_accepting_the_answer_closes_the_case_and_keeps_the_verdict(helpful):
    case = _case("ANSWERED")
    now = timezone.now()
    workflow.confirm_answer(student_id=SID, reference=case.reference, helpful=helpful, now=now)
    case.refresh_from_db()
    assert (case.status, case.close_reason, case.answer_helpful, case.closed_at) == (
        "CLOSED",
        "ANSWER_ACCEPTED",
        helpful,
        now,
    )
    event = case.events.get()
    assert (event.kind, event.from_value, event.to_value) == (
        "ANSWER_CONFIRMED",
        "ANSWERED",
        "CLOSED",
    )


@pytest.mark.parametrize("status", [s for s in STATUSES if s != "ANSWERED"])
def test_only_an_answer_can_be_accepted(status):
    case = _case(status)
    _refused("NOT_ALLOWED", workflow.confirm_answer, student_id=SID, reference=case.reference)


@pytest.mark.parametrize(("handled", "target"), [(False, "SUBMITTED"), (True, "IN_REVIEW")])
def test_saying_the_answer_did_not_help_reopens_the_case(handled, target):
    staff = User.objects.create_user("advisor-reopen") if handled else None
    case = _case("ANSWERED", handled_by=staff, answered_at=timezone.now() - timedelta(days=3))

    message = workflow.reopen_answer(
        student_id=SID, reference=case.reference, body="It still clashes."
    )

    case.refresh_from_db()
    assert (case.status, case.answer_helpful) == (target, False)
    event = case.events.get()
    assert (event.kind, event.message_id, event.from_value, event.to_value) == (
        "REOPENED",
        message.pk,
        "ANSWERED",
        target,
    )


def test_an_answer_cannot_be_reopened_after_the_window(settings):
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    case = _case("ANSWERED", answered_at=timezone.now() - timedelta(days=15))
    _refused(
        "REOPEN_WINDOW_PASSED",
        workflow.reopen_answer,
        student_id=SID,
        reference=case.reference,
        body="Too late?",
    )
    assert _state(case) == ("ANSWERED", 0)


def test_an_answer_reopens_inside_the_window_even_on_its_last_day(settings):
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    now = timezone.now()
    case = _case("ANSWERED", answered_at=now - timedelta(days=14))
    workflow.reopen_answer(student_id=SID, reference=case.reference, body="Still wrong.", now=now)
    assert _state(case)[0] == "SUBMITTED"


@pytest.mark.parametrize("status", [s for s in STATUSES if s != "ANSWERED"])
def test_only_an_answer_can_be_reopened(status):
    """NEEDS_INFORMATION above all: the student's table lets it go back to the
    queue, so only this check stops a verdict on an answer never given."""
    case = _case(status, answered_at=timezone.now())
    _refused(
        "NOT_ALLOWED",
        workflow.reopen_answer,
        student_id=SID,
        reference=case.reference,
        body="Not helpful.",
    )
    assert _state(case) == (status, 0)
    assert not TimetableFeedbackMessage.objects.exists()
    assert not case.events.exists()


@pytest.mark.parametrize(
    ("age", "passed"),
    [
        (timedelta(0), False),
        (timedelta(days=14), False),
        (timedelta(days=14, seconds=1), True),
    ],
)
def test_the_window_includes_its_last_moment(settings, age, passed):
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    now = timezone.now()
    case = _case("ANSWERED", answered_at=now - age)
    assert workflow.answer_window_passed(case, now) is passed


def test_a_settled_answer_is_closed_by_the_system_with_its_reason():
    now = timezone.now()
    case = _case("ANSWERED", answered_at=now - timedelta(days=30))

    workflow.close_settled_answer(case, now=now)

    case.refresh_from_db()
    assert (case.status, case.close_reason, case.closed_at, case.version) == (
        "CLOSED",
        "NO_REPLY_AFTER_ANSWER",
        now,
        1,
    )
    event = case.events.get()
    assert (event.kind, event.from_value, event.to_value, event.actor_id) == (
        "AUTO_CLOSED",
        "ANSWERED",
        "CLOSED",
        None,
    )


def test_reopening_needs_a_reason():
    case = _case("ANSWERED", answered_at=timezone.now())
    _refused(
        "BODY_REQUIRED",
        workflow.reopen_answer,
        student_id=SID,
        reference=case.reference,
        body="  ",
    )


# ---------------------------------------------------------------------------
# Whose case, and how often
# ---------------------------------------------------------------------------


ACTIONS = [
    ("reply", {"body": "hi"}, "NEEDS_INFORMATION"),
    ("follow_up", {"body": "hi"}, "SUBMITTED"),
    ("withdraw_case", {}, "SUBMITTED"),
    ("confirm_answer", {}, "ANSWERED"),
    ("reopen_answer", {"body": "hi"}, "ANSWERED"),
]


@pytest.mark.parametrize(("action", "extra", "status"), ACTIONS)
def test_another_students_case_is_not_found(action, extra, status):
    theirs = _case(status, student_id=OTHER_SID, answered_at=timezone.now())
    _refused(
        "CASE_NOT_FOUND",
        getattr(workflow, action),
        student_id=SID,
        reference=theirs.reference,
        **extra,
    )
    assert _state(theirs) == (status, 0)


def test_another_students_point_cannot_be_withdrawn_through_their_reference():
    theirs = _case(student_id=OTHER_SID)
    point = _point(theirs, 11)
    _point(theirs, 12)
    _refused(
        "CASE_NOT_FOUND",
        workflow.withdraw_point,
        student_id=SID,
        reference=theirs.reference,
        point_id=str(point.pk),
    )


def test_a_reference_is_found_however_it_is_typed():
    case = _case()
    workflow.withdraw_case(student_id=SID, reference="  " + case.reference.lower() + " ")
    assert _state(case)[0] == "WITHDRAWN"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("TTF-2026-00012", "TTF-2026-00012"),
        (" ttf-2026-00012 ", "TTF-2026-00012"),
        ("TTF-2026-123456", "TTF-2026-123456"),
        ("TTF-2026", ""),
        ("TTF-26-00012", ""),
        ("XYZ-2026-00012", ""),
        ("TTF-2026-", ""),
        # Arabic-Indic digits are digits to Python, but no reference holds them.
        ("TTF-" + chr(0x0662) + "026-00012", ""),
        # A lone surrogate never reaches the query, where it could not be encoded.
        ("TTF-2026-0001" + chr(0xD800), ""),
        (None, ""),
    ],
)
def test_only_text_shaped_like_a_reference_is_looked_up(typed, expected):
    assert workflow.case_reference(typed) == expected


def test_a_reference_no_driver_can_encode_is_simply_not_found():
    _case()
    _refused(
        "CASE_NOT_FOUND",
        workflow.withdraw_case,
        student_id=SID,
        reference="TTF-2026-0001" + chr(0xDC00),
    )


def test_a_students_actions_share_one_budget(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (1, 3600))
    first, second = _case("SUBMITTED"), _case("SUBMITTED", student_id=OTHER_SID)
    workflow.follow_up(student_id=SID, reference=first.reference, body="once")
    refused = _refused(
        "RATE_LIMITED", workflow.withdraw_case, student_id=SID, reference=first.reference
    )
    assert refused.status == 429 and refused.retry_after > 0
    # Another student's budget is their own.
    workflow.withdraw_case(student_id=OTHER_SID, reference=second.reference)


STUDENT_ACTIONS = [
    "reply",
    "follow_up",
    "withdraw_point",
    "withdraw_case",
    "confirm_answer",
    "reopen_answer",
]


def _ready(action):
    """A case ``action`` succeeds on, and the arguments that make it succeed."""
    status = {
        "reply": "NEEDS_INFORMATION",
        "confirm_answer": "ANSWERED",
        "reopen_answer": "ANSWERED",
    }.get(action, "SUBMITTED")
    case = _case(status, answered_at=timezone.now())
    extra = {"body": "hi"} if action in ("reply", "follow_up", "reopen_answer") else {}
    if action == "withdraw_point":
        extra = {"point_id": str(_point(case, 11).pk)}
        _point(case, 12)
    return case, extra


@pytest.mark.parametrize("action", STUDENT_ACTIONS)
def test_every_student_action_spends_the_budget(action, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_REPLY, (1, 3600))
    case, extra = _ready(action)

    getattr(workflow, action)(student_id=SID, reference=case.reference, **extra)

    refused = _refused(
        "RATE_LIMITED", getattr(workflow, action), student_id=SID, reference=case.reference, **extra
    )
    assert refused.status == 429


@pytest.mark.parametrize(
    ("action", "status", "extra", "code"),
    [
        ("reply", "NEEDS_INFORMATION", {"body": ""}, "BODY_REQUIRED"),
        ("follow_up", "SUBMITTED", {"body": ""}, "BODY_REQUIRED"),
        ("reopen_answer", "ANSWERED", {"body": ""}, "BODY_REQUIRED"),
        ("withdraw_point", "SUBMITTED", {"point_id": "not-a-point"}, "POINT_NOT_FOUND"),
    ],
)
def test_input_refused_before_the_case_is_read_spends_nothing(action, status, extra, code):
    case = _case(status, answered_at=timezone.now())
    _refused(code, getattr(workflow, action), student_id=SID, reference=case.reference, **extra)
    assert not rate_limit.RateLimitBucket.objects.exists()


def test_every_event_belongs_to_its_case_and_nothing_else_is_written():
    case = _case("SUBMITTED")
    workflow.follow_up(student_id=SID, reference=case.reference, body="note")
    assert TimetableFeedbackEvent.objects.exclude(case=case).count() == 0
    assert TimetableFeedbackMessage.objects.exclude(case=case).count() == 0


@pytest.mark.parametrize(
    ("action", "extra", "status"), [*ACTIONS, ("withdraw_point", {}, "SUBMITTED")]
)
def test_every_student_action_stamps_the_case_with_its_own_time(action, extra, status):
    """The queue sorts on these. A time five minutes back tells the action's own
    ``now`` apart from the clock at the moment of writing."""
    now = timezone.now() - timedelta(minutes=5)
    case = _case(status, answered_at=now - timedelta(days=1))
    if action == "withdraw_point":
        extra = {"point_id": str(_point(case, 11).pk)}
        _point(case, 12)

    getattr(workflow, action)(student_id=SID, reference=case.reference, now=now, **extra)

    case.refresh_from_db()
    assert (case.last_student_activity_at, case.updated_at, case.version) == (now, now, 1)


def test_an_answer_without_its_time_is_timed_from_the_cases_last_change(settings):
    """An answered case should carry ``answered_at``. One that does not, set by
    hand say, is timed from its last change: neither refused outright nor open to
    reopening for ever."""
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    now = timezone.now()
    recent, stale = _case("ANSWERED"), _case("ANSWERED", student_id=OTHER_SID)
    TimetableFeedback.objects.filter(pk=recent.pk).update(updated_at=now - timedelta(days=2))
    TimetableFeedback.objects.filter(pk=stale.pk).update(updated_at=now - timedelta(days=20))

    workflow.reopen_answer(student_id=SID, reference=recent.reference, body="Wrong.", now=now)
    assert _state(recent)[0] == "SUBMITTED"
    _refused(
        "REOPEN_WINDOW_PASSED",
        workflow.reopen_answer,
        student_id=OTHER_SID,
        reference=stale.reference,
        body="Wrong.",
        now=now,
    )
    assert _state(stale) == ("ANSWERED", 0)


# ---------------------------------------------------------------------------
# What a page may ask before it offers an action
# ---------------------------------------------------------------------------


def _offered(action, status, edge):
    """A case in ``status``, two live points (one with the edge "one point"), an
    answer given now (thirty days ago with "window passed"), and every follow-up
    used with "follow-ups used"; and the arguments ``action`` takes."""
    answered = timezone.now() - timedelta(days=30 if edge == "window passed" else 0)
    case = _case(status, answered_at=answered)
    points = [_point(case, 11)] + ([] if edge == "one point" else [_point(case, 12)])
    if edge == "follow-ups used":
        for _ in range(workflow.MAX_FOLLOW_UPS):
            TimetableFeedbackEvent.objects.create(
                case=case, kind=TimetableFeedbackEvent.Kind.STUDENT_FOLLOWUP
            )
    extra = {"body": "hi"} if action in ("reply", "follow_up", "reopen_answer") else {}
    if action == "withdraw_point":
        extra = {"point_id": str(points[0].pk)}
    return case, extra


OFFERS = [(action, status, "") for action in STUDENT_ACTIONS for status in STATUSES] + [
    ("follow_up", "SUBMITTED", "follow-ups used"),
    ("withdraw_point", "SUBMITTED", "one point"),
    ("reopen_answer", "ANSWERED", "window passed"),
]


@pytest.mark.parametrize(("action", "status", "edge"), OFFERS)
def test_the_question_a_page_asks_gets_the_answer_the_action_gives(action, status, edge, settings):
    """`student_action_refusal` is the action's own check, so in every status a
    page offers exactly what the action then takes, and refuses it with the same
    code. Asking writes nothing and spends nothing."""
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    case, extra = _offered(action, status, edge)
    before = _state(case)
    events = TimetableFeedbackEvent.objects.count()

    foreseen = workflow.student_action_refusal(case, action)

    assert _state(case) == before
    assert TimetableFeedbackEvent.objects.count() == events
    assert not rate_limit.RateLimitBucket.objects.exists()
    try:
        getattr(workflow, action)(student_id=SID, reference=case.reference, **extra)
    except workflow.WorkflowRefused as refused:
        assert foreseen is not None, f"offered, then refused {refused.code}"
        assert (foreseen.code, foreseen.status) == (refused.code, refused.status)
    else:
        assert foreseen is None, f"withheld ({foreseen.code}), but the action was taken"


@pytest.mark.parametrize(
    ("status", "answered_days_ago", "waits"),
    [
        ("NEEDS_INFORMATION", 0, True),
        ("ANSWERED", 1, True),
        ("ANSWERED", 14, True),
        ("ANSWERED", 15, False),
        ("SUBMITTED", 0, False),
        ("IN_REVIEW", 0, False),
        ("FORWARDED", 0, False),
        ("CLOSED", 0, False),
        ("WITHDRAWN", 0, False),
    ],
)
def test_a_case_waits_on_the_student_for_a_reply_or_an_answer_in_its_window(
    status, answered_days_ago, waits, settings
):
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    now = timezone.now()
    case = _case(status, answered_at=now - timedelta(days=answered_days_ago))
    assert workflow.waits_on_student(case, now) is waits


# ---------------------------------------------------------------------------
# What the student has not seen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("visible", "seen", "unread"),
    [
        (None, None, False),
        (None, 0, False),
        (0, None, True),
        (0, 0, False),
        (0, -1, True),
        (-1, 0, False),
    ],
)
def test_a_case_is_unread_while_what_the_student_may_read_is_newer_than_their_look(
    visible, seen, unread
):
    """Minutes from now; None is a field never set. The query and the in-memory
    rule answer alike, so a list, a badge and a banner can never disagree."""
    now = timezone.now()
    case = _case(
        last_student_visible_at=None if visible is None else now + timedelta(minutes=visible),
        student_seen_at=None if seen is None else now + timedelta(minutes=seen),
    )
    assert workflow.has_unread(case) is unread
    assert TimetableFeedback.objects.filter(workflow.unread_q(), pk=case.pk).exists() is unread


def test_opening_a_case_marks_what_the_page_read_as_seen_and_disturbs_no_claim():
    now = timezone.now()
    case = _case(last_student_visible_at=now - timedelta(minutes=5))
    TimetableFeedback.objects.filter(pk=case.pk).update(updated_at=now - timedelta(days=3))
    case.refresh_from_db()
    claim = (case.version, case.updated_at)

    workflow.mark_seen(case)

    case.refresh_from_db()
    assert case.student_seen_at == now - timedelta(minutes=5)  # what was read, not the clock
    assert not workflow.has_unread(case)
    assert (case.version, case.updated_at) == claim


def test_a_write_landing_while_the_page_renders_stays_unread():
    now = timezone.now()
    case = _case(last_student_visible_at=now - timedelta(minutes=5))
    read = TimetableFeedback.objects.get(pk=case.pk)
    TimetableFeedback.objects.filter(pk=case.pk).update(last_student_visible_at=now)

    workflow.mark_seen(read)

    case.refresh_from_db()
    assert workflow.has_unread(case)


def test_the_seen_mark_never_moves_back_and_needs_something_to_mark():
    now = timezone.now()
    nothing = _case(student_id=OTHER_SID)
    workflow.mark_seen(nothing)
    nothing.refresh_from_db()
    assert nothing.student_seen_at is None

    case = _case(last_student_visible_at=now - timedelta(minutes=5), student_seen_at=now)
    workflow.mark_seen(case)
    case.refresh_from_db()
    assert case.student_seen_at == now
