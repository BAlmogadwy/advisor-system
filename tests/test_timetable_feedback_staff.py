"""The staff side of timetable feedback, at the service: who sees which case, what
waits on whom, and every staff move and refusal, with what each one leaves for the
student to see.

The pages are tested in test_timetable_feedback_staff_views.py and the races in
test_timetable_feedback_staff_postgres.py. The helpers here are plain functions,
not fixtures, so the other modules can import them.
"""

from __future__ import annotations

import ast
import itertools
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import RequestFactory
from django.utils import timezone

from core.models import (
    Student,
    TimetableFeedback,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
    UserScope,
)
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_workflow as workflow
from core.services.policy import require_student_scope
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_EXAM_COMMITTEE,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    set_user_scope,
)

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
PROG = "FBS"
ELSEWHERE = "FBX"
ADV = "ADV-71"
ADV_B = "ADV-72"
ADV_GONE = "ADV-73"
S_MINE = 4996001
S_OTHER_ADVISOR = 4996002
S_OTHER_PROG = 4996003
S_RAW_NAME = 4996004
S_BLANK = 4996005
S_MESSY = 4996006
S_TABBED = 4996009
S_GONE = 4996099
TAB, NBSP = chr(9), chr(0xA0)
STUDENTS = {
    S_MINE: (PROG, ADV),
    S_OTHER_ADVISOR: (PROG, ADV_B),
    S_OTHER_PROG: (ELSEWHERE, ADV),
    S_RAW_NAME: (PROG, "سعد الأحمد"),
    S_BLANK: (PROG, ""),
    # Stored with stray spaces and in lower case: matched as the record's own
    # scope check matches it, trimmed and upper-cased.
    S_MESSY: (" fbs ", " ADV-71 "),
    # Whitespace that is not a space, which the record check strips too.
    S_TABBED: (f"FBS{NBSP}", f"{TAB}ADV-71{NBSP}"),
}
_TERMS = itertools.count(1)


# ---------------------------------------------------------------------------
# Helpers, shared with the other staff test modules
# ---------------------------------------------------------------------------


def staff_user(username, role, *, advisor_id="", departments="", active=True, scope=True):
    """A user in ``role`` ('superuser' for Django's own flag, None for no group)."""
    ensure_role_groups()
    user = User.objects.create_user(
        username, first_name=username.title(), last_name="Staff", is_active=active
    )
    if role == "superuser":
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
    elif role:
        user.groups.add(Group.objects.get(name=role))
    if scope:
        set_user_scope(user.pk, advisor_id=advisor_id, departments=departments)
    return user


def make_people():
    return SimpleNamespace(
        advisor=staff_user("ttf-advisor", ROLE_ADVISOR, advisor_id=ADV),
        # A second account holding the same advisor id, as seeding makes them.
        twin=staff_user("ttf-advisor-2", ROLE_ADVISOR, advisor_id=ADV),
        other=staff_user("ttf-other", ROLE_ADVISOR, advisor_id=ADV_B),
        no_id=staff_user("ttf-no-id", ROLE_ADVISOR),
        department=staff_user("ttf-dept", ROLE_GENERAL_ADVISOR, departments=f"{PROG};CS"),
        # The department sees by programme only, whatever advisor id it holds.
        department_with_id=staff_user(
            "ttf-dept-id", ROLE_GENERAL_ADVISOR, advisor_id=ADV, departments=ELSEWHERE
        ),
        department_empty=staff_user("ttf-dept-empty", ROLE_GENERAL_ADVISOR),
        admin=staff_user("ttf-admin", ROLE_SUPER_ADMIN),
        superuser=staff_user("ttf-root", "superuser"),
        committee=staff_user(
            "ttf-committee", ROLE_EXAM_COMMITTEE, advisor_id=ADV, departments=PROG
        ),
        bare=staff_user("ttf-bare", None, scope=False),
        gone=staff_user("ttf-gone", ROLE_ADVISOR, advisor_id=ADV_GONE, active=False),
    )


def make_students(rows=None):
    for sid, (program, advisor_id) in (rows or STUDENTS).items():
        Student.objects.create(
            student_id=sid,
            name=f"Student {sid}",
            program=program,
            advisor_id=advisor_id,
            section="M",
            status="ACTIVE",
        )


def point(case, anchor="SECTION", n=0, *, note="", withdrawn=False, fingerprint="a" * 64):
    section = 1000 + n if anchor == "SECTION" else None
    return TimetableFeedbackPoint.objects.create(
        case=case,
        anchor_kind=anchor,
        term_section_id=section,
        section_label="M1" if section else "",
        course_key="SA101" if section else "",
        anchor_key=f"SECTION|{section}" if section else anchor,
        reason_code={"SECTION": "TIME_CLASH", "TIMETABLE": "LOAD_TOO_HIGH"}.get(
            anchor, "NO_FORECAST"
        ),
        seen_fingerprint=fingerprint,
        note=note,
        withdrawn_at=timezone.now() if withdrawn else None,
    )


def case_for(sid, status="SUBMITTED", *, anchors=("SECTION",), term=None, **fields):
    """A case in ``status`` with one live point per anchor, each term its own so
    several open cases of one student can stand side by side."""
    if status == "CLOSED":
        fields.setdefault("close_reason", "OTHER")
    fields.setdefault("last_student_activity_at", timezone.now() - timedelta(days=1))
    case = TimetableFeedback.objects.create(
        student_id=sid,
        academic_year="1448",
        term=term or str(next(_TERMS)),
        status=status,
        **fields,
    )
    for n, anchor in enumerate(anchors):
        point(case, anchor, n)
    return case


def as_staff(user):
    return staff_service.staff_of(user)


def fresh(case):
    return TimetableFeedback.objects.get(pk=case.pk)


@pytest.fixture
def people(db):
    return make_people()


@pytest.fixture
def students(db):
    make_students()


# ---------------------------------------------------------------------------
# Who sees which case
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped(people, students):
    return {
        "mine": case_for(S_MINE),
        "other_advisor": case_for(S_OTHER_ADVISOR),
        "assigned": case_for(S_OTHER_ADVISOR, assigned_advisor_id=ADV),
        "other_prog": case_for(S_OTHER_PROG),
        "raw": case_for(S_RAW_NAME),
        "blank": case_for(S_BLANK),
        "messy": case_for(S_MESSY),
        "tabbed": case_for(S_TABBED),
        "gone": case_for(S_GONE),
    }


EVERY_CASE = {
    "mine",
    "other_advisor",
    "assigned",
    "other_prog",
    "raw",
    "blank",
    "messy",
    "tabbed",
    "gone",
}


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        ("advisor", {"mine", "assigned", "other_prog", "messy", "tabbed"}),
        ("twin", {"mine", "assigned", "other_prog", "messy", "tabbed"}),
        ("other", {"other_advisor", "assigned"}),
        ("no_id", set()),
        ("department", {"mine", "other_advisor", "assigned", "raw", "blank", "messy", "tabbed"}),
        ("department_with_id", {"other_prog"}),
        ("department_empty", set()),
        ("admin", EVERY_CASE),
        ("superuser", EVERY_CASE),
        ("committee", set()),
        ("bare", set()),
    ],
)
def test_each_person_sees_exactly_the_cases_their_grounds_give(people, scoped, who, expected):
    visible = staff_service.visible_to(as_staff(getattr(people, who)))
    names = {name for name, case in scoped.items() if visible.filter(pk=case.pk).exists()}
    assert names == expected


def test_a_student_or_nobody_is_not_staff(db):
    from core.services import student_otp

    Student.objects.create(student_id=S_MINE, program=PROG, advisor_id=ADV)
    student = student_otp.provision_student_user(S_MINE)
    assert staff_service.staff_of(student) is None
    assert staff_service.staff_of(SimpleNamespace(is_authenticated=False)) is None
    assert not staff_service.visible_to(None).exists()


@pytest.mark.parametrize(
    "who",
    [
        "advisor",
        "other",
        "no_id",
        "department",
        "department_with_id",
        "department_empty",
        "admin",
        "committee",
        "bare",
    ],
)
def test_the_cases_of_a_student_are_visible_exactly_where_their_record_is(people, scoped, who):
    """The same grounds as `require_student_scope`, which guards the student's own
    pages: a case is visible where the record is, apart from the department's
    assignments, which that check knows nothing about."""
    user = getattr(people, who)
    visible = staff_service.visible_to(as_staff(user))
    request = RequestFactory().get("/")
    request.user = user
    for sid in STUDENTS:
        allowed = require_student_scope(request, sid) is None
        assert visible.filter(student_id=sid, assigned_advisor_id="").exists() == allowed, sid


def test_a_case_of_a_student_with_no_record_is_the_administrators_alone(people, scoped):
    for who in ("advisor", "department", "other"):
        assert (
            not staff_service.visible_to(as_staff(getattr(people, who)))
            .filter(pk=scoped["gone"].pk)
            .exists()
        )


def test_a_student_moved_to_another_advisor_takes_their_cases_along(people, scoped):
    advisor, other = as_staff(people.advisor), as_staff(people.other)
    Student.objects.filter(student_id=S_MINE).update(advisor_id=ADV_B)
    assert not staff_service.visible_to(advisor).filter(pk=scoped["mine"].pk).exists()
    assert staff_service.visible_to(other).filter(pk=scoped["mine"].pk).exists()


@pytest.mark.parametrize(
    ("who", "reason"),
    [
        ("no_id", "NO_ADVISOR_ID"),
        ("department_empty", "NO_DEPARTMENTS"),
        ("advisor", ""),
        ("department", ""),
        ("admin", ""),
    ],
)
def test_an_empty_scope_says_why(people, who, reason):
    assert staff_service.why_nothing_is_visible(as_staff(getattr(people, who))) == reason
    assert staff_service.why_nothing_is_visible(None) == "NOT_STAFF"


def test_the_record_link_opens_exactly_where_the_record_check_allows(people, students):
    request = RequestFactory().get("/")
    for who in ("advisor", "other", "department", "department_with_id", "admin"):
        user = getattr(people, who)
        request.user = user
        for sid in STUDENTS:
            row = Student.objects.filter(student_id=sid).values("program", "advisor_id").first()
            allowed = require_student_scope(request, sid) is None
            assert staff_service.may_open_student_record(as_staff(user), row) == allowed, (who, sid)
        assert not staff_service.may_open_student_record(as_staff(user), None)


def test_accounts_holding_an_id_count_the_active_advisors_only(people):
    """ADV is held by two advisors, a department account and a committee account;
    only the advisors see cases by it. A superuser holding it does not either."""
    staff_user("ttf-root-with-id", "superuser", advisor_id=ADV)
    assert staff_service.accounts_holding(ADV) == 2
    # Stored with a no-break space by some import: `get_user_scope` strips it.
    UserScope.objects.filter(user=people.twin).update(advisor_id=f"{ADV}{NBSP}")
    assert staff_service.accounts_holding(ADV) == 2
    assert staff_service.accounts_holding(ADV_GONE) == 0
    assert staff_service.accounts_holding("") == 0
    assert staff_service.advisor_id_exists(ADV)
    assert staff_service.advisor_id_exists(f"  {ADV} ")
    assert not staff_service.advisor_id_exists(ADV_GONE)
    assert not staff_service.advisor_id_exists("")


def test_unrouted_cases_are_those_no_advisor_account_can_see(people, scoped):
    Student.objects.create(student_id=4996007, program=PROG, advisor_id=ADV_GONE)
    held_by_nobody_active = case_for(4996007)
    # Held, but by a department account, which sees nothing by an advisor id.
    staff_user("ttf-dept-holder", ROLE_GENERAL_ADVISOR, advisor_id="ADV-DEPT")
    Student.objects.create(student_id=4996008, program=PROG, advisor_id="ADV-DEPT")
    held_by_the_department = case_for(4996008)
    assigned_to_nobody_active = case_for(S_RAW_NAME, assigned_advisor_id=ADV_GONE)
    assigned_away = case_for(S_RAW_NAME, assigned_advisor_id=ADV_B)
    unrouted = TimetableFeedback.objects.filter(staff_service.unrouted_q())
    assert set(unrouted.values_list("pk", flat=True)) == {
        scoped["raw"].pk,
        scoped["blank"].pk,
        scoped["gone"].pk,
        held_by_nobody_active.pk,
        held_by_the_department.pk,
        assigned_to_nobody_active.pk,
    }
    assert assigned_away.pk not in unrouted.values_list("pk", flat=True)


# ---------------------------------------------------------------------------
# What waits on whom
# ---------------------------------------------------------------------------


@pytest.fixture
def waiting(people, students):
    now = timezone.now()
    nf_and_section = case_for(S_MINE, anchors=("NO_FORECAST", "TIMETABLE"))
    nf_section_withdrawn = case_for(S_MINE, anchors=("NO_FORECAST",))
    point(nf_section_withdrawn, "TIMETABLE", 5, withdrawn=True)
    return {
        "new": case_for(S_MINE),
        "taken_by_me": case_for(
            S_MINE, "IN_REVIEW", handled_by=people.advisor, handled_advisor_id=ADV
        ),
        "taken_by_twin": case_for(
            S_MINE, "IN_REVIEW", handled_by=people.twin, handled_advisor_id=ADV
        ),
        "taken_by_department": case_for(
            S_MINE, "IN_REVIEW", handled_by=people.department, handled_advisor_id=""
        ),
        "in_review_free": case_for(S_MINE, "IN_REVIEW"),
        "asked": case_for(S_MINE, "NEEDS_INFORMATION"),
        "answered": case_for(S_MINE, "ANSWERED", answered_at=now),
        "forwarded": case_for(S_MINE, "FORWARDED", handled_by=people.advisor),
        "closed": case_for(S_MINE, "CLOSED"),
        "no_forecast_only": case_for(S_MINE, anchors=("NO_FORECAST",)),
        "no_forecast_and_more": nf_and_section,
        "no_forecast_left_alone": nf_section_withdrawn,
        "unrouted_new": case_for(S_RAW_NAME),
        "unrouted_taken_by_colleague": case_for(S_BLANK, "IN_REVIEW", handled_by=people.admin),
        "unrouted_taken_by_me": case_for(S_BLANK, "IN_REVIEW", handled_by=people.department),
        # Its advisor took it, then the student moved to another advisor.
        "taken_by_moved_away": case_for(
            S_MINE, "IN_REVIEW", handled_by=people.other, handled_advisor_id=ADV_B
        ),
        # Its handler's account was switched off. Held by no advisor id, so only
        # the switched-off account, not the advisor's own id, frees it.
        "taken_by_deactivated": case_for(
            S_MINE, "IN_REVIEW", handled_by=people.gone, handled_advisor_id=""
        ),
        # The department handed it to a colleague, who took it: theirs, not ours.
        "assigned_and_taken": case_for(
            S_MINE,
            "IN_REVIEW",
            assigned_advisor_id=ADV_B,
            handled_by=people.other,
            handled_advisor_id=ADV_B,
        ),
    }


def _waiting_names(staff, cases):
    queue = staff_service.visible_to(staff).filter(staff_service.needs_action_q(staff))
    return {name for name, case in cases.items() if queue.filter(pk=case.pk).exists()}


def test_an_advisor_is_waited_on_by_new_cases_and_their_own_not_the_departments(people, waiting):
    advisor = as_staff(people.advisor)
    assert _waiting_names(advisor, waiting) == {
        "new",
        "taken_by_me",
        "taken_by_twin",
        "in_review_free",
        "no_forecast_and_more",
        "taken_by_moved_away",
        "taken_by_deactivated",
    }
    assert staff_service.action_count(advisor) == 7


def test_the_department_is_waited_on_by_forwarded_unrouted_and_no_timetable_cases(people, waiting):
    department = as_staff(people.department)
    assert _waiting_names(department, waiting) == {
        "forwarded",
        "taken_by_department",
        "no_forecast_only",
        "no_forecast_left_alone",
        "unrouted_new",
        "unrouted_taken_by_me",
    }
    assert staff_service.action_count(department) == 6
    assert staff_service.action_count(None) == 0


def test_a_reply_after_the_student_moved_waits_on_their_new_advisor(people, students):
    case = case_for(S_MINE)
    staff_service.ask(as_staff(people.advisor), case.reference, version=0, body="Which one?")
    untaken = TimetableFeedback.objects.filter(staff_service.untaken_q())
    assert not untaken.filter(pk=case.pk).exists()
    Student.objects.filter(student_id=S_MINE).update(advisor_id=ADV_B)
    # The old advisor's handling lapses with the move.
    assert untaken.filter(pk=case.pk).exists()
    workflow.reply(student_id=S_MINE, reference=case.reference, body="M2, please.")
    case.refresh_from_db()
    assert (case.status, case.handled_by) == ("IN_REVIEW", people.advisor)
    old, new = as_staff(people.advisor), as_staff(people.other)
    assert not staff_service.visible_to(old).filter(pk=case.pk).exists()
    assert _waiting_names(new, {"case": case}) == {"case"}
    # The department sees it, but it is the student's advisor's.
    assert _waiting_names(as_staff(people.department), {"case": case}) == set()


def test_the_departments_handling_is_never_tied_to_an_advisor_id(people, students):
    """A department account that also holds an advisor id handles by its
    programmes, so its handling never lapses when the student's advisor changes."""
    case = case_for(S_OTHER_PROG)
    staff_service.take(as_staff(people.department_with_id), case.reference, version=0)
    case.refresh_from_db()
    assert (case.handled_by, case.handled_advisor_id) == (people.department_with_id, "")
    Student.objects.filter(student_id=S_OTHER_PROG).update(advisor_id=ADV_B)
    assert not TimetableFeedback.objects.filter(staff_service.untaken_q(), pk=case.pk).exists()


def test_the_tabs_group_by_status_for_everyone(people, waiting):
    admin = as_staff(people.admin)
    visible = staff_service.visible_to(admin)

    def names(tab):
        rows = visible.filter(staff_service.tab_q(tab, admin))
        return {name for name, case in waiting.items() if rows.filter(pk=case.pk).exists()}

    assert names("student") == {"asked", "answered"}
    assert names("department") == {"forwarded"}
    assert names("closed") == {"closed"}
    assert names("open") == set(waiting) - {"closed"}
    assert names("all") == set(waiting)
    assert names("nonsense") == names("action")


def test_a_student_who_wrote_after_staff_shows_as_such():
    now = timezone.now()
    rows = [
        (None, None, False),
        (now, None, True),
        (now, now - timedelta(minutes=1), True),
        (now - timedelta(minutes=1), now, False),
        (now, now, False),
    ]
    for n, (student_at, staff_at, expected) in enumerate(rows):
        case = TimetableFeedback.objects.create(
            student_id=4996100 + n,
            academic_year="1448",
            term="1",
            last_student_activity_at=student_at,
            last_staff_action_at=staff_at,
        )
        matches = TimetableFeedback.objects.filter(
            staff_service.student_wrote_since_staff_q(), pk=case.pk
        ).exists()
        assert matches is expected, (student_at, staff_at)


# ---------------------------------------------------------------------------
# Taking a case
# ---------------------------------------------------------------------------


def _changed_only_by_staff(before, after, now):
    assert after.version == before.version + 1
    assert after.updated_at == now
    assert after.last_staff_action_at == now


def _claimed_without_acting(before, after, now):
    """Housekeeping: the case is claimed (version, last change) but nobody has
    acted on it for the student, so the student's "wrote since" mark stays."""
    assert after.version == before.version + 1
    assert after.updated_at == now
    assert (after.last_staff_action_at, after.first_staff_action_at) == (
        before.last_staff_action_at,
        before.first_staff_action_at,
    )


@pytest.mark.parametrize(
    ("status", "target"), [("SUBMITTED", "IN_REVIEW"), ("FORWARDED", "IN_REVIEW")]
)
def test_taking_a_new_or_forwarded_case_puts_it_in_review(people, students, status, target):
    now = timezone.now()
    case = case_for(S_MINE, status)
    staff_service.take(as_staff(people.advisor), case.reference, version=0, now=now)
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert after.status == target
    assert (after.handled_by, after.handled_by_username, after.handled_advisor_id) == (
        people.advisor,
        "ttf-advisor",
        ADV,
    )
    assert after.first_staff_action_at == now
    # Taking is not news to the student.
    assert after.last_student_visible_at is None
    event = after.events.get()
    assert (event.kind, event.from_value, event.to_value) == ("TAKEN", status, target)
    assert (event.actor, event.actor_username, event.actor_label, event.actor_advisor_id) == (
        people.advisor,
        "ttf-advisor",
        "Ttf-Advisor Staff",
        ADV,
    )


@pytest.mark.parametrize("status", ["IN_REVIEW", "NEEDS_INFORMATION"])
def test_taking_over_from_a_colleague_keeps_the_status(people, students, status):
    earlier = timezone.now() - timedelta(hours=2)
    case = case_for(
        S_MINE,
        status,
        handled_by=people.twin,
        handled_advisor_id=ADV,
        first_staff_action_at=earlier,
    )
    staff_service.take(as_staff(people.department), case.reference, version=0)
    after = fresh(case)
    assert after.status == status
    assert (after.handled_by, after.handled_advisor_id) == (people.department, "")
    # The first staff action stays the first.
    assert after.first_staff_action_at == earlier
    assert list(after.events.values_list("from_value", "to_value")) == [(status, status)]


def test_a_case_already_yours_is_not_taken_again(people, students):
    case = case_for(S_MINE, "IN_REVIEW", handled_by=people.advisor)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.take(as_staff(people.advisor), case.reference, version=0)
    assert refused.value.code == "ALREADY_YOURS"
    assert fresh(case).version == 0


@pytest.mark.parametrize("status", ["ANSWERED", "CLOSED", "WITHDRAWN"])
def test_an_answered_or_finished_case_cannot_be_taken(people, students, status):
    case = case_for(S_MINE, status, answered_at=timezone.now())
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.take(as_staff(people.advisor), case.reference, version=0)
    assert (refused.value.code, refused.value.status) == ("NOT_ALLOWED", 409)
    assert not fresh(case).events.exists()


# ---------------------------------------------------------------------------
# What every action refuses alike
# ---------------------------------------------------------------------------


ALL_ACTIONS = {
    "take": lambda staff, ref, v: staff_service.take(staff, ref, version=v),
    "note": lambda staff, ref, v: staff_service.add_note(staff, ref, version=v, body="A note."),
    "ask": lambda staff, ref, v: staff_service.ask(staff, ref, version=v, body="Which one?"),
    "answer": lambda staff, ref, v: staff_service.answer(staff, ref, version=v, body="Done."),
    "forward": lambda staff, ref, v: staff_service.forward(staff, ref, version=v, body="Dept."),
    "close": lambda staff, ref, v: staff_service.close(
        staff, ref, version=v, reason="DUPLICATE", body="Same as another."
    ),
    "assign": lambda staff, ref, v: staff_service.assign(staff, ref, version=v, advisor_id=ADV_B),
    "redact": lambda staff, ref, v: staff_service.redact(
        staff, ref, version=v, remove="private", point_id=str(fresh_point(ref).pk)
    ),
}


def fresh_point(reference):
    return TimetableFeedbackPoint.objects.filter(case__reference=reference).first()


@pytest.mark.parametrize("action", sorted(ALL_ACTIONS))
def test_a_case_outside_the_scope_is_not_found_and_untouched(people, students, action):
    case = case_for(S_OTHER_ADVISOR)
    case.points.update(note="something private")
    with pytest.raises(workflow.WorkflowRefused) as refused:
        ALL_ACTIONS[action](as_staff(people.department_with_id), case.reference, 0)
    assert (refused.value.code, refused.value.status) == ("CASE_NOT_FOUND", 404)
    after = fresh(case)
    assert (after.version, after.status) == (0, "SUBMITTED")
    assert not after.events.exists() and not after.messages.exists()


@pytest.mark.parametrize("action", sorted(ALL_ACTIONS))
@pytest.mark.parametrize("version", [1, "", "zero", None])
def test_a_case_changed_since_the_page_showed_it_is_refused(people, students, action, version):
    case = case_for(S_MINE)
    case.points.update(note="something private")
    with pytest.raises(workflow.WorkflowRefused) as refused:
        ALL_ACTIONS[action](as_staff(people.admin), case.reference, version)
    assert (refused.value.code, refused.value.status) == ("CASE_CHANGED", 409)
    after = fresh(case)
    assert after.version == 0 and not after.events.exists() and not after.messages.exists()


@pytest.mark.parametrize("action", sorted(ALL_ACTIONS))
def test_every_action_on_a_case_in_scope_is_taken_at_the_version_shown(people, students, action):
    case = case_for(S_MINE)
    case.points.update(note="something private")
    ALL_ACTIONS[action](as_staff(people.admin), case.reference, 0)
    assert fresh(case).version == 1


@pytest.mark.parametrize(
    "reference",
    ["", "TTF-2026", "ttf-2026-x", "TTF-20A6-00001", chr(0xD800), None],
    ids=["empty", "short", "lower-x", "letter", "surrogate", "none"],
)
def test_text_that_cannot_be_a_reference_is_not_found(people, reference):
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.take(as_staff(people.admin), reference, version=0)
    assert refused.value.code == "CASE_NOT_FOUND"


STATUSES = [
    "SUBMITTED",
    "IN_REVIEW",
    "NEEDS_INFORMATION",
    "FORWARDED",
    "ANSWERED",
    "CLOSED",
    "WITHDRAWN",
]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("action", sorted(ALL_ACTIONS))
@pytest.mark.parametrize("who", ["advisor", "department"])
def test_a_page_offers_an_action_exactly_where_the_action_takes_it(
    people, students, status, action, who
):
    """The read-only check and the action agree in every status, for an advisor and
    for the department: the page never offers what would be refused."""
    staff = as_staff(getattr(people, who))
    case = case_for(S_MINE, status, answered_at=timezone.now())
    case.points.update(note="something private")
    foreseen = staff_service.staff_action_refusal(staff, case, action)
    try:
        ALL_ACTIONS[action](staff, case.reference, 0)
    except workflow.WorkflowRefused as refused:
        assert foreseen is not None and foreseen.code == refused.code, (foreseen, refused.code)
    else:
        assert foreseen is None


# ---------------------------------------------------------------------------
# Comments for colleagues
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", STATUSES)
def test_a_comment_can_be_added_in_any_status_and_is_never_the_students(people, students, status):
    now = timezone.now()
    case = case_for(S_MINE, status, answered_at=now - timedelta(days=1))
    message = staff_service.add_note(
        as_staff(people.advisor), case.reference, version=0, body="  Called the student.  ", now=now
    )
    assert (message.audience, message.author_kind, message.body) == (
        "INTERNAL",
        "ADVISOR",
        "Called the student.",
    )
    assert (message.author, message.author_username, message.author_label) == (
        people.advisor,
        "ttf-advisor",
        "Ttf-Advisor Staff",
    )
    assert message.body_script == "en"
    after = fresh(case)
    _claimed_without_acting(case, after, now)
    assert after.status == status
    assert after.last_student_visible_at is None
    # The student wrote a day ago and nobody has answered: still marked so.
    assert TimetableFeedback.objects.filter(
        staff_service.student_wrote_since_staff_q(), pk=case.pk
    ).exists()
    event = after.events.get()
    assert (event.kind, event.message_id) == ("NOTE_ADDED", message.pk)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("", "BODY_REQUIRED"),
        ("   ", "BODY_REQUIRED"),
        (chr(0x200B) * 3, "BODY_REQUIRED"),
        ("x" * (staff_service.STAFF_TEXT_MAX_CHARS + 1), "BODY_TOO_LONG"),
        ("bad" + chr(0) + "text", "BODY_INVALID"),
        ("lone " + chr(0xD800), "BODY_INVALID"),
    ],
)
@pytest.mark.parametrize("action", ["note", "ask", "answer", "forward", "close"])
def test_text_that_cannot_be_sent_is_refused_and_writes_nothing(
    people, students, body, code, action
):
    case = case_for(S_MINE)
    write = {
        "note": staff_service.add_note,
        "ask": staff_service.ask,
        "answer": staff_service.answer,
        "forward": staff_service.forward,
    }.get(action)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        if write:
            write(as_staff(people.advisor), case.reference, version=0, body=body)
        else:
            staff_service.close(
                as_staff(people.advisor), case.reference, version=0, reason="OTHER", body=body
            )
    assert (refused.value.code, refused.value.status) == (code, 400)
    after = fresh(case)
    assert after.version == 0 and not after.messages.exists() and not after.events.exists()


def test_text_at_the_limit_is_taken():
    assert len(staff_service.clean_staff_text("y" * staff_service.STAFF_TEXT_MAX_CHARS)) == (
        staff_service.STAFF_TEXT_MAX_CHARS
    )


# ---------------------------------------------------------------------------
# Asking and answering: what the student sees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW"])
def test_asking_waits_on_the_student_who_sees_it_as_new(people, students, status):
    now = timezone.now()
    case = case_for(S_MINE, status)
    message = staff_service.ask(
        as_staff(people.advisor),
        case.reference,
        version=0,
        body="Which section suits you?",
        now=now,
    )
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert after.status == "NEEDS_INFORMATION"
    assert after.last_student_visible_at == now
    assert workflow.has_unread(after)
    assert (message.audience, message.author_kind) == ("STUDENT_VISIBLE", "ADVISOR")
    # The asker handles the case, so the reply comes back to them.
    assert after.handled_by == people.advisor
    assert workflow.reopen_target(after) == "IN_REVIEW"
    event = after.events.get()
    assert (event.kind, event.message_id, event.from_value, event.to_value) == (
        "STAFF_MESSAGE",
        message.pk,
        status,
        "NEEDS_INFORMATION",
    )


@pytest.mark.parametrize("status", ["NEEDS_INFORMATION", "FORWARDED", "ANSWERED"])
def test_a_question_is_asked_only_of_a_case_waiting_on_staff(people, students, status):
    case = case_for(S_MINE, status, answered_at=timezone.now())
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.ask(as_staff(people.admin), case.reference, version=0, body="Why?")
    assert refused.value.code == "NOT_ALLOWED"


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED"])
def test_an_answer_starts_the_students_window_and_shows_as_new(people, students, status):
    now = timezone.now()
    case = case_for(S_MINE, status, answer_helpful=False)
    message = staff_service.answer(
        as_staff(people.advisor), case.reference, version=0, body="It cannot move.", now=now
    )
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert after.status == "ANSWERED"
    assert after.answered_at == now
    assert workflow.reopen_until(after) == now + timedelta(
        days=int(settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS)
    )
    # A new answer has not been judged yet.
    assert after.answer_helpful is None
    assert after.last_student_visible_at == now
    assert after.handled_by == people.advisor
    assert (message.audience, message.body) == ("STUDENT_VISIBLE", "It cannot move.")
    assert list(after.events.values_list("kind", "from_value", "to_value")) == [
        ("STAFF_MESSAGE", status, "ANSWERED")
    ]


@pytest.mark.parametrize(("language", "index"), [("ar", 1), ("en", 0), ("", 0)])
def test_a_ready_made_answer_is_sent_in_the_language_the_student_wrote_in(
    people, students, language, index
):
    case = case_for(S_MINE, submitted_language=language)
    message = staff_service.answer(
        as_staff(people.advisor), case.reference, version=0, template="NOTED_FOR_PLANNING"
    )
    assert message.body == copy.REPLY_TEMPLATES["NOTED_FOR_PLANNING"][index]
    assert message.body_script == ("ar" if index else "en")


def test_typed_words_win_over_a_ready_made_answer(people, students):
    case = case_for(S_MINE, submitted_language="ar")
    message = staff_service.answer(
        as_staff(people.advisor),
        case.reference,
        version=0,
        body="My own words.",
        template="NOTED_FOR_PLANNING",
    )
    assert message.body == "My own words."


def test_an_unknown_ready_made_answer_is_refused_before_anything_is_read(people, students):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.answer(
            as_staff(people.advisor), case.reference, version=0, template="MADE_UP"
        )
    assert (refused.value.code, refused.value.status) == ("TEMPLATE_UNKNOWN", 400)
    assert fresh(case).version == 0


@pytest.mark.parametrize(
    ("who", "case_name", "kind"),
    [
        ("advisor", "mine", "ADVISOR"),
        ("twin", "mine", "ADVISOR"),
        # Assigned to them, but not their student: the department to the student.
        ("advisor", "assigned", "DEPARTMENT"),
        ("department", "mine", "DEPARTMENT"),
        ("admin", "mine", "DEPARTMENT"),
        # Holding the student's own advisor id makes it their advisor, whatever the role.
        ("department_with_id", "other_prog", "ADVISOR"),
    ],
)
def test_the_student_reads_their_advisor_only_when_it_is_their_advisor(
    people, scoped, who, case_name, kind
):
    case = scoped[case_name]
    message = staff_service.ask(
        as_staff(getattr(people, who)), case.reference, version=0, body="A question."
    )
    assert message.author_kind == kind


def test_a_student_visible_write_never_moves_the_unread_mark_back(people, students):
    """A write that took its time early and committed late must not land behind a
    look the student has already marked."""
    later = timezone.now()
    case = case_for(S_MINE, last_student_visible_at=later)
    staff_service.ask(
        as_staff(people.advisor),
        case.reference,
        version=0,
        body="Which?",
        now=later - timedelta(minutes=5),
    )
    assert fresh(case).last_student_visible_at == later


def test_the_mark_moves_forward_from_an_earlier_one(people, students):
    earlier = timezone.now() - timedelta(days=1)
    now = timezone.now()
    case = case_for(S_MINE, last_student_visible_at=earlier)
    staff_service.answer(as_staff(people.advisor), case.reference, version=0, body="Done.", now=now)
    assert fresh(case).last_student_visible_at == now


# ---------------------------------------------------------------------------
# Forwarding and closing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW"])
def test_forwarding_passes_the_case_with_a_reason_the_student_never_reads(people, students, status):
    now = timezone.now()
    case = case_for(S_MINE, status)
    message = staff_service.forward(
        as_staff(people.advisor), case.reference, version=0, body="Needs a new section.", now=now
    )
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert (after.status, after.forwarded_at) == ("FORWARDED", now)
    assert after.last_student_visible_at is None
    assert message.audience == "INTERNAL"
    assert after.handled_by == people.advisor
    assert list(after.events.values_list("kind", "message_id", "to_value")) == [
        ("FORWARDED", message.pk, "FORWARDED")
    ]


def test_forwarding_keeps_whoever_already_handles_the_case(people, students):
    case = case_for(S_MINE, "IN_REVIEW", handled_by=people.twin, handled_advisor_id=ADV)
    staff_service.forward(as_staff(people.advisor), case.reference, version=0, body="Dept.")
    after = fresh(case)
    assert (after.handled_by, after.handled_advisor_id) == (people.twin, ADV)


@pytest.mark.parametrize("status", ["NEEDS_INFORMATION", "FORWARDED", "ANSWERED"])
def test_only_a_case_waiting_on_staff_is_forwarded(people, students, status):
    case = case_for(S_MINE, status, answered_at=timezone.now())
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.forward(as_staff(people.admin), case.reference, version=0, body="Dept.")
    assert refused.value.code == "NOT_ALLOWED"


@pytest.mark.parametrize("reason", ["DUPLICATE", "NOT_APPLICABLE", "OTHER", "duplicate "])
@pytest.mark.parametrize(
    "status", ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED", "ANSWERED"]
)
def test_closing_says_why_to_the_student_and_explains_to_colleagues(
    people, students, reason, status
):
    now = timezone.now()
    case = case_for(S_MINE, status, answered_at=now - timedelta(days=1))
    message = staff_service.close(
        as_staff(people.advisor), case.reference, version=0, reason=reason, body="Done.", now=now
    )
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert (after.status, after.close_reason, after.closed_at) == (
        "CLOSED",
        reason.strip().upper(),
        now,
    )
    assert after.last_student_visible_at == now
    assert message.audience == "INTERNAL"
    assert list(after.events.values_list("kind", "message_id", "from_value", "to_value")) == [
        ("STATUS_CHANGED", message.pk, status, "CLOSED")
    ]


@pytest.mark.parametrize(
    "reason", ["", None, "ANSWER_ACCEPTED", "TERM_ENDED", "NO_REPLY_AFTER_ANSWER", "MADE_UP"]
)
def test_only_the_staffs_own_reasons_close_a_case(people, students, reason):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.close(
            as_staff(people.advisor), case.reference, version=0, reason=reason, body="Done."
        )
    assert (refused.value.code, refused.value.status) == ("CLOSE_REASON_REQUIRED", 400)
    assert fresh(case).status == "SUBMITTED"


@pytest.mark.parametrize("status", ["CLOSED", "WITHDRAWN"])
def test_a_finished_case_is_not_closed_again(people, students, status):
    case = case_for(S_MINE, status)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.close(
            as_staff(people.admin), case.reference, version=0, reason="OTHER", body="Done."
        )
    assert refused.value.code == "NOT_ALLOWED"


# ---------------------------------------------------------------------------
# The department: assigning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["SUBMITTED", "IN_REVIEW", "NEEDS_INFORMATION"])
def test_the_department_assigns_a_case_to_an_advisor_who_then_sees_it(people, students, status):
    now = timezone.now()
    case = case_for(S_RAW_NAME, status, handled_by=people.department, handled_advisor_id="")
    other = as_staff(people.other)
    assert not staff_service.visible_to(other).filter(pk=case.pk).exists()
    staff_service.assign(
        as_staff(people.department), case.reference, version=0, advisor_id=f" {ADV_B} ", now=now
    )
    after = fresh(case)
    _changed_only_by_staff(case, after, now)
    assert (after.status, after.assigned_advisor_id) == (status, ADV_B)
    # Nobody handles it until the advisor takes it.
    assert (after.handled_by, after.handled_by_username, after.handled_advisor_id) == (None, "", "")
    assert staff_service.visible_to(other).filter(pk=case.pk).exists()
    assert list(after.events.values_list("kind", "from_value", "to_value")) == [
        ("ASSIGNED", "", ADV_B)
    ]


def test_assigning_a_forwarded_case_brings_it_back_from_the_department(people, students):
    case = case_for(S_RAW_NAME, "FORWARDED")
    staff_service.assign(as_staff(people.admin), case.reference, version=0, advisor_id=ADV_B)
    assert fresh(case).status == "IN_REVIEW"


def test_reassigning_takes_the_case_from_the_previous_advisor(people, students):
    case = case_for(S_RAW_NAME, assigned_advisor_id=ADV)
    advisor = as_staff(people.advisor)
    assert staff_service.visible_to(advisor).filter(pk=case.pk).exists()
    staff_service.assign(as_staff(people.department), case.reference, version=0, advisor_id=ADV_B)
    assert not staff_service.visible_to(advisor).filter(pk=case.pk).exists()
    assert list(fresh(case).events.values_list("from_value", "to_value")) == [(ADV, ADV_B)]


@pytest.mark.parametrize("target", ["", "  ", "ADV-NOBODY", ADV_GONE, None])
def test_a_case_is_assigned_only_to_an_id_an_active_account_holds(people, students, target):
    case = case_for(S_RAW_NAME)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.assign(
            as_staff(people.department), case.reference, version=0, advisor_id=target
        )
    assert (refused.value.code, refused.value.status) == ("UNKNOWN_ADVISOR", 400)
    assert fresh(case).version == 0


def test_assigning_to_the_id_it_already_has_is_refused(people, students):
    case = case_for(S_RAW_NAME, assigned_advisor_id=ADV_B)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.assign(
            as_staff(people.department), case.reference, version=0, advisor_id=ADV_B
        )
    assert refused.value.code == "ALREADY_ASSIGNED"
    assert fresh(case).version == 0


@pytest.mark.parametrize("who", ["advisor", "twin", "other"])
def test_only_the_department_assigns(people, students, who):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.assign(
            as_staff(getattr(people, who)), case.reference, version=0, advisor_id=ADV_B
        )
    assert (refused.value.code, refused.value.status) == ("DEPARTMENT_ONLY", 403)


@pytest.mark.parametrize("status", ["ANSWERED", "CLOSED", "WITHDRAWN"])
def test_an_answered_or_finished_case_is_not_assigned(people, students, status):
    case = case_for(S_MINE, status, answered_at=timezone.now())
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.assign(as_staff(people.admin), case.reference, version=0, advisor_id=ADV_B)
    assert refused.value.code == "NOT_ALLOWED"


# ---------------------------------------------------------------------------
# The department: removing words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "passages", "expected"),
    [
        ("My mother is ill on Sundays.", ["mother is ill"], "My […] on Sundays."),
        ("ill, ill, ill", ["ill"], "[…], […], […]"),
        # Overlapping and touching passages become one marker.
        ("abcdef", ["bcd", "cde"], "a[…]f"),
        ("abcdef", ["ab", "cd"], "[…]ef"),
        ("a…b", ["a", "…"], "[…]b"),
        ("whole", ["whole"], "[…]"),
    ],
)
def test_removing_words_replaces_each_passage_and_keeps_the_rest(text, passages, expected):
    assert staff_service.redacted(text, passages) == expected


def test_a_passage_never_matches_inside_a_marker_another_one_left():
    """Passages are matched on the text as it was, so the marker one leaves is not
    there for the next to match."""
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.redacted("x y", ["x", "…"])
    assert refused.value.code == "REDACT_TEXT_NOT_FOUND"


def test_a_passage_not_in_the_text_is_refused():
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.redacted("Some text.", ["text", "absent"])
    assert (refused.value.code, refused.value.status) == ("REDACT_TEXT_NOT_FOUND", 400)


def test_passages_are_one_per_line_trimmed_and_blank_lines_ignored():
    assert staff_service.redaction_passages(" one \r\n\r\ntwo\rthree\n  ") == [
        "one",
        "two",
        "three",
    ]
    assert staff_service.redaction_passages(None) == []


def test_the_department_removes_words_from_a_students_message(people, students):
    now = timezone.now()
    case = case_for(S_MINE, "IN_REVIEW")
    message = TimetableFeedbackMessage.objects.create(
        case=case,
        audience="STUDENT_VISIBLE",
        author_kind="STUDENT",
        body="I care for my sick father on Mondays.",
    )
    staff_service.redact(
        as_staff(people.department),
        case.reference,
        version=0,
        remove="my sick father",
        message_id=str(message.pk),
        now=now,
    )
    message.refresh_from_db()
    assert (message.body, message.redacted_at) == ("I care for […] on Mondays.", now)
    after = fresh(case)
    _claimed_without_acting(case, after, now)
    assert after.status == "IN_REVIEW"
    assert list(after.events.values_list("kind", "message_id", "point_id")) == [
        ("REDACTED", message.pk, None)
    ]


def test_the_department_removes_words_from_a_points_details(people, students):
    now = timezone.now()
    case = case_for(S_MINE)
    target = case.points.get()
    target.note = "Clashes with my job at the clinic."
    target.save()
    staff_service.redact(
        as_staff(people.admin),
        case.reference,
        version=0,
        remove="at the clinic",
        point_id=str(target.pk),
        now=now,
    )
    target.refresh_from_db()
    assert (target.note, target.note_redacted_at) == ("Clashes with my job […].", now)
    assert list(fresh(case).events.values_list("kind", "point_id")) == [("REDACTED", target.pk)]


@pytest.mark.parametrize(
    ("remove", "code"),
    [("", "REDACT_NOTHING"), (" \n ", "REDACT_NOTHING"), ("not there", "REDACT_TEXT_NOT_FOUND")],
)
def test_a_removal_that_matches_nothing_changes_nothing(people, students, remove, code):
    case = case_for(S_MINE)
    target = case.points.get()
    target.note = "Keep this."
    target.save()
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.redact(
            as_staff(people.department),
            case.reference,
            version=0,
            remove=remove,
            point_id=str(target.pk),
        )
    assert refused.value.code == code
    target.refresh_from_db()
    assert (target.note, target.note_redacted_at) == ("Keep this.", None)
    assert fresh(case).version == 0


def test_words_are_removed_only_from_this_cases_own_text(people, students):
    case, other = case_for(S_MINE), case_for(S_MINE)
    elsewhere = TimetableFeedbackMessage.objects.create(
        case=other, audience="STUDENT_VISIBLE", author_kind="STUDENT", body="secret"
    )
    for target in (
        {"message_id": str(elsewhere.pk)},
        {"point_id": str(other.points.get().pk)},
        {"message_id": "not-an-id"},
        {"point_id": "12"},
        {},
    ):
        with pytest.raises(workflow.WorkflowRefused) as refused:
            staff_service.redact(
                as_staff(people.department), case.reference, version=0, remove="secret", **target
            )
        assert (refused.value.code, refused.value.status) == ("REDACT_TARGET_NOT_FOUND", 404)
    elsewhere.refresh_from_db()
    assert elsewhere.body == "secret"
    assert fresh(case).version == 0


@pytest.mark.parametrize("who", ["advisor", "other"])
def test_only_the_department_removes_words(people, students, who):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.redact(
            as_staff(getattr(people, who)),
            case.reference,
            version=0,
            remove="x",
            point_id=str(case.points.get().pk),
        )
    assert (refused.value.code, refused.value.status) == ("DEPARTMENT_ONLY", 403)


# ---------------------------------------------------------------------------
# Opening a case
# ---------------------------------------------------------------------------


def test_an_opening_is_kept_once_an_hour_per_person(people, students):
    case = case_for(S_MINE)
    advisor, twin = as_staff(people.advisor), as_staff(people.twin)
    start = timezone.now()
    assert staff_service.record_view(advisor, case, now=start) is True
    # Half an hour on: the same person's opening is not kept again.
    assert staff_service.record_view(advisor, case, now=start + timedelta(minutes=30)) is False
    assert staff_service.record_view(twin, case, now=start) is True
    # More than an hour after it, it is.
    assert staff_service.record_view(advisor, case, now=start + timedelta(minutes=61)) is True
    viewed = case.events.filter(kind="VIEWED")
    assert viewed.filter(actor=people.advisor).count() == 2
    assert viewed.filter(actor=people.twin).count() == 1
    # Opening is not a change: the version and the case's own times stay.
    after = fresh(case)
    assert (after.version, after.last_staff_action_at) == (0, None)


# ---------------------------------------------------------------------------
# Many cases at once
# ---------------------------------------------------------------------------


def test_picks_are_read_as_sent_each_case_once():
    assert staff_service.bulk_picks(
        ["TTF-2026-00001@3", "ttf-2026-00002@0", "TTF-2026-00001@9", "nonsense", "", None, "@4"]
    ) == [("TTF-2026-00001", "3"), ("TTF-2026-00002", "0")]


def test_a_bulk_answer_answers_each_case_in_its_students_language_and_lists_the_rest(
    people, students
):
    arabic = case_for(S_MINE, submitted_language="ar")
    english = case_for(S_MINE, submitted_language="en")
    stale = case_for(S_MINE)
    finished = case_for(S_MINE, "CLOSED")
    outside = case_for(S_OTHER_ADVISOR)
    picks = [
        (arabic.reference, "0"),
        (english.reference, "0"),
        (stale.reference, "7"),
        (finished.reference, "0"),
        (outside.reference, "0"),
    ]
    outcomes = staff_service.bulk_answer(
        as_staff(people.advisor), picks, template="REGISTERED_IN_PORTAL"
    )
    assert [(o.reference, o.refused) for o in outcomes] == [
        (arabic.reference, ""),
        (english.reference, ""),
        (stale.reference, "CASE_CHANGED"),
        (finished.reference, "NOT_ALLOWED"),
        (outside.reference, "CASE_NOT_FOUND"),
    ]
    for case, index in ((arabic, 1), (english, 0)):
        assert fresh(case).status == "ANSWERED"
        assert case.messages.get().body == copy.REPLY_TEMPLATES["REGISTERED_IN_PORTAL"][index]
    for case in (stale, outside):
        assert fresh(case).status == "SUBMITTED"


@pytest.mark.parametrize("bulk", ["answer", "close"])
def test_many_at_once_leave_a_case_someone_else_handles_to_them(people, students, bulk):
    mine = case_for(S_MINE, "IN_REVIEW", handled_by=people.advisor, handled_advisor_id=ADV)
    colleagues = case_for(S_MINE, "NEEDS_INFORMATION", handled_by=people.twin)
    nobodys = case_for(S_MINE)
    picks = [(case.reference, "0") for case in (mine, colleagues, nobodys)]
    advisor = as_staff(people.advisor)
    if bulk == "answer":
        outcomes = staff_service.bulk_answer(advisor, picks, template="NOTED_FOR_PLANNING")
    else:
        outcomes = staff_service.bulk_close_duplicates(advisor, picks, body="Dup.")
    assert [(o.reference, o.refused) for o in outcomes] == [
        (mine.reference, ""),
        (colleagues.reference, "HANDLED_BY_OTHER"),
        (nobodys.reference, ""),
    ]
    colleagues.refresh_from_db()
    assert (colleagues.status, colleagues.handled_by, colleagues.version) == (
        "NEEDS_INFORMATION",
        people.twin,
        0,
    )
    assert not colleagues.messages.exists()


def test_one_case_at_a_time_anyone_in_scope_may_still_act(people, students):
    """On the case's own page the handler is shown, and a colleague may answer."""
    case = case_for(S_MINE, "NEEDS_INFORMATION", handled_by=people.twin)
    staff_service.answer(as_staff(people.advisor), case.reference, version=0, body="Answered.")
    assert fresh(case).status == "ANSWERED"


def test_a_bulk_close_closes_each_as_a_duplicate_with_one_comment(people, students):
    first, second = case_for(S_MINE), case_for(S_MINE, "IN_REVIEW")
    outcomes = staff_service.bulk_close_duplicates(
        as_staff(people.advisor),
        [(first.reference, "0"), (second.reference, "0")],
        body="Same as TTF-2026-00001.",
    )
    assert [o.refused for o in outcomes] == ["", ""]
    for case in (first, second):
        after = fresh(case)
        assert (after.status, after.close_reason) == ("CLOSED", "DUPLICATE")
        assert after.messages.get().body == "Same as TTF-2026-00001."


@pytest.mark.parametrize(
    ("picks", "code"),
    [
        ([], "BULK_NOTHING"),
        ([("TTF-2026-00001", "0")] * (staff_service.BULK_MAX + 1), "BULK_TOO_MANY"),
    ],
)
def test_a_bulk_action_needs_between_one_case_and_a_page_of_them(people, picks, code):
    for act in (
        lambda: staff_service.bulk_answer(
            as_staff(people.admin), picks, template="NOTED_FOR_PLANNING"
        ),
        lambda: staff_service.bulk_close_duplicates(as_staff(people.admin), picks, body="Dup."),
    ):
        with pytest.raises(workflow.WorkflowRefused) as refused:
            act()
        assert (refused.value.code, refused.value.status) == (code, 400)


def test_a_full_page_is_one_bulk_action(people, students):
    cases = [case_for(S_MINE) for _ in range(staff_service.BULK_MAX)]
    outcomes = staff_service.bulk_close_duplicates(
        as_staff(people.advisor), [(case.reference, "0") for case in cases], body="Dup."
    )
    assert len(outcomes) == staff_service.BULK_MAX and not any(o.refused for o in outcomes)


def test_a_bulk_answer_with_an_unknown_template_answers_nothing(people, students):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.bulk_answer(as_staff(people.admin), [(case.reference, "0")], template="NOPE")
    assert refused.value.code == "TEMPLATE_UNKNOWN"
    assert fresh(case).status == "SUBMITTED"


def test_a_bulk_close_without_a_comment_closes_nothing(people, students):
    case = case_for(S_MINE)
    with pytest.raises(workflow.WorkflowRefused) as refused:
        staff_service.bulk_close_duplicates(
            as_staff(people.admin), [(case.reference, "0")], body=" "
        )
    assert refused.value.code == "BODY_REQUIRED"
    assert fresh(case).status == "SUBMITTED"


# ---------------------------------------------------------------------------
# What the staff side never touches
# ---------------------------------------------------------------------------

#: The timetable's own writers. Feedback reads the timetable; it never writes it.
TIMETABLE_WRITERS = {
    "replace_student_term_sections",
    "planner_save_student_sections_view",
    "StudentTermSection",
    "TermSection",
    "TermSectionMeeting",
    "TermSectionProgram",
    "ProgrammeRequirement",
    "TimetableScenario",
}


@pytest.mark.parametrize(
    "path",
    [
        "core/services/timetable_feedback_staff.py",
        "core/services/timetable_feedback_staff_copy.py",
        "core/staff_timetable_feedback_views.py",
    ],
)
def test_no_staff_feedback_module_imports_a_timetable_writer(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    assert imported, "the scan found no imports at all"
    assert not imported & TIMETABLE_WRITERS
