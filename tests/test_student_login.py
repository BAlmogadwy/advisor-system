"""Student OTP login + role/scope isolation (Phase A + B)."""

import json
import re

import pytest
from django.contrib.auth.models import Group, User
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse

from core.authz import ROLE_ORDER
from core.models import Student, StudentLoginOTP
from core.services import student_otp
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_STUDENT,
    ensure_role_groups,
    get_user_role,
    get_user_scope,
)

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
SID = 4901234
OTHER = 4905678
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _deterministic_env(settings):
    """Local .env may enable the testing bypass / OTP redirect; tests must not depend
    on it. Per-test @override_settings still wins over these defaults."""
    settings.STUDENT_LOGIN_NO_OTP = False
    settings.STUDENT_OTP_REDIRECT_EMAIL = ""


@pytest.fixture
def students():
    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=SID, defaults={"name": "Test One", "program": "DS2", "section": "M"}
    )
    Student.objects.get_or_create(
        student_id=OTHER, defaults={"name": "Test Two", "program": "AI2", "section": "M"}
    )


def _code_from_mail():
    return re.search(r"(\d{6})", mail.outbox[-1].body).group(1)


@override_settings(
    EMAIL_BACKEND=LOCMEM, STUDENT_OTP_ASYNC_EMAIL=False, STUDENT_OTP_REDIRECT_EMAIL=""
)
def test_issue_hashes_and_emails(students):
    mail.outbox = []
    student_otp.issue_otp(SID, "1.2.3.4")
    assert mail.outbox[-1].to == [f"{SID}@taibahu.edu.sa"]
    code = _code_from_mail()
    assert len(code) == 6
    # plaintext code is never persisted, only its hash
    assert not StudentLoginOTP.objects.filter(code_hash=code).exists()
    assert StudentLoginOTP.objects.filter(student_id=SID, consumed=False).count() == 1


@override_settings(
    EMAIL_BACKEND=LOCMEM, STUDENT_OTP_ASYNC_EMAIL=False, STUDENT_OTP_REDIRECT_EMAIL=""
)
def test_verify_wrong_then_right_then_replay(students):
    mail.outbox = []
    student_otp.issue_otp(SID)
    code = _code_from_mail()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    assert student_otp.verify_otp(SID, wrong) is False
    assert student_otp.verify_otp(SID, code) is True
    assert student_otp.verify_otp(SID, code) is False  # single-use


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL="",
    STUDENT_OTP_MAX_ATTEMPTS=5,
)
def test_attempt_cap_locks_code(students):
    mail.outbox = []
    student_otp.issue_otp(SID)
    code = _code_from_mail()
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        student_otp.verify_otp(SID, wrong)
    assert student_otp.verify_otp(SID, code) is False  # correct code no longer accepted


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL="",
    STUDENT_OTP_MAX_SENDS=3,
)
def test_send_rate_limit(students):
    for _ in range(3):
        student_otp.issue_otp(SID)
    with pytest.raises(student_otp.OTPError):
        student_otp.issue_otp(SID)


def test_provision_is_safe_and_idempotent(students):
    u = student_otp.provision_student_user(SID)
    assert u.username == str(SID)
    assert get_user_role(u) == ROLE_STUDENT
    assert not u.is_staff and not u.is_superuser and not u.has_usable_password()
    assert student_otp.provision_student_user(SID).pk == u.pk  # idempotent


def test_provision_refuses_staff_username(students):
    staff = User.objects.create_user(username=str(SID), password="x", is_staff=True)
    with pytest.raises(student_otp.OTPError):
        student_otp.provision_student_user(SID)
    assert not staff.groups.filter(name=ROLE_STUDENT).exists()


def test_role_and_scope(students):
    u = student_otp.provision_student_user(SID)
    assert get_user_role(u) == ROLE_STUDENT
    assert ROLE_ORDER[ROLE_STUDENT] < ROLE_ORDER[ROLE_ADVISOR]
    assert get_user_scope(u)["student_id"] == SID


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL="",
    ALLOWED_HOSTS=["testserver"],
)
def test_full_http_login_and_isolation(students):
    c = Client()
    assert c.get("/student/login/").status_code == 200
    mail.outbox = []
    r = c.post("/student/login/", {"student_id": str(SID)})
    assert r.status_code == 200 and b'name="code"' in r.content
    code = _code_from_mail()
    r = c.post("/student/login/verify/", {"code": code})
    assert r.status_code == 302 and r.headers["Location"] == "/student/"
    assert c.get("/student/").status_code == 200
    # a logged-in student is denied every advisor endpoint (own id and others)
    assert c.get(f"/report/student-plan/?student_id={SID}").status_code == 403
    assert c.get(f"/report/student-plan/?student_id={OTHER}").status_code == 403
    assert c.get("/report/summary/").status_code == 403


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL="",
    ALLOWED_HOSTS=["testserver"],
)
def test_unknown_id_is_enumeration_resistant(students):
    c = Client()
    mail.outbox = []
    r = c.post("/student/login/", {"student_id": "9999999"})  # no such student
    assert r.status_code == 200 and b'name="code"' in r.content  # same OTP step as a real id
    assert mail.outbox == []  # but nothing was actually sent
    assert c.post("/student/login/verify/", {"code": "123456"}).status_code == 200  # cannot log in


# ── security-fix regressions (from the adversarial review) ──


def test_identity_is_immutable_across_username_change(students):
    """CRITICAL fix: identity comes from UserScope.student_id, not the mutable username."""
    u = student_otp.provision_student_user(SID)
    assert get_user_scope(u)["student_id"] == SID
    u.username = str(OTHER)  # attacker renames toward a victim's id
    u.save(update_fields=["username"])
    u.refresh_from_db()
    assert get_user_scope(u)["student_id"] == SID  # still bound to the real id, NOT OTHER


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_student_cannot_change_username(students):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.post(
        reverse("profile_change_username"),
        data=json.dumps({"new_username": "hacker"}),
        content_type="application/json",
    )
    assert r.status_code == 403
    u.refresh_from_db()
    assert u.username == str(SID)


def test_username_matching_a_student_id_is_reserved(students):
    advisor = User.objects.create_user(username="adv1", password="x", is_staff=True)
    c = Client()
    c.force_login(advisor)
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        r = c.post(
            reverse("profile_change_username"),
            data=json.dumps({"new_username": str(OTHER)}),
            content_type="application/json",
        )
    assert r.status_code == 400  # cannot claim a real student's id as a username


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_dashboard_redirects_student_home(students):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get(f"/?student_id={OTHER}")
    assert r.status_code == 302 and r.headers["Location"] == "/student/"  # no IDOR, no advisor UI


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_group_availability_denies_student(students):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.post(
        reverse("group_availability_compute"),
        data=json.dumps({"student_ids": [OTHER]}),
        content_type="application/json",
    )
    assert r.status_code == 403  # registrar tool, not for students


# ── Phase C: student home content ──


def test_weekly_timetable_orders_days_and_splits_unscheduled():
    from core.student_auth_views import _weekly_timetable

    rows = [
        {"course_code": "B", "day": "THU", "start_time": "09:00"},
        {"course_code": "A", "day": "MON", "start_time": "13:00"},
        {"course_code": "A", "day": "MON", "start_time": "08:00"},
        {"course_code": "C", "day": "SUN", "start_time": "10:00"},
        {"course_code": "Z", "day": "", "start_time": ""},  # unscheduled
        {"course_code": "Y", "day": "Sunday", "start_time": "11:00"},  # full-name form
    ]
    days, unscheduled = _weekly_timetable(rows)
    assert [d["code"] for d in days] == ["SUN", "MON", "THU"]  # week order, not alphabetical
    assert [m["start_time"] for m in days[1]["meetings"]] == ["08:00", "13:00"]  # sorted by time
    assert [m["course_code"] for m in days[0]["meetings"]] == ["C", "Y"]  # SUN and "Sunday" merge
    assert [r["course_code"] for r in unscheduled] == ["Z"]


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_student_home_shows_own_data_only(students):
    """The home page derives everything from the session identity."""
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/")
    assert r.status_code == 200
    ctx = r.context
    assert ctx["student_id"] == SID
    assert ctx["student"].student_id == SID
    # a client-supplied id must never change whose data is shown
    r2 = c.get(f"/student/?student_id={OTHER}")
    assert r2.context["student_id"] == SID


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_student_home_renders_empty_states(students):
    """A student with no timetable/recommendations still gets a valid page."""
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/")
    assert r.status_code == 200
    assert r.context["timetable"] == []
    body = r.content.decode()
    assert "No registered timetable" in body or "لا يوجد جدول" in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_portal_renders_populated_and_hides_other_gender(students):
    """A POPULATED portal: own-gender + ungendered sections render, other-gender never."""
    from core.models import StudentTermSection, TermSection, TermSectionMeeting

    Student.objects.filter(student_id=SID).update(section="M")
    made = []
    for code, sect in (("CS101", "M1"), ("CS102", ""), ("CS103", "F9")):
        ts = TermSection.objects.create(course_code=code, course_name=code + " name", section=sect)
        TermSectionMeeting.objects.create(
            term_section=ts,
            day="MON",
            start_time="09:00",
            end_time="10:15",
            room="R1",
            instructor="Dr X",
        )
        StudentTermSection.objects.create(
            student_id=SID, academic_year="1448", term="1", term_section=ts
        )
        made.append(ts)
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    body = c.get("/student/").content.decode()
    assert "CS101" in body and "CS102" in body  # own gender + ungendered
    assert "CS103" not in body  # other-gender section filtered out
    for ts in made:
        ts.delete()


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_unlinked_student_gets_terminal_page_not_redirect_loop(students):
    from core.models import UserScope

    u = student_otp.provision_student_user(SID)
    UserScope.objects.filter(user_id=u.id).update(student_id=None)
    c = Client()
    c.force_login(u)
    r = c.get("/student/")
    assert r.status_code == 409  # terminal, not a 302 loop
    assert b"not linked" in r.content or "غير مرتبط".encode() in r.content


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_student_home_is_never_cached(students):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    assert "no-store" in c.get("/student/").headers.get("Cache-Control", "")


@override_settings(ALLOWED_HOSTS=["testserver"], DEBUG=True)
def test_student_cannot_dev_switch_to_super_admin(students, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_ROLE_SWITCH", "true")
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.post("/ops/dev/switch-role/", {"role": "SUPER_ADMIN"})
    assert r.status_code == 403
    u.refresh_from_db()
    assert not u.groups.filter(name="SUPER_ADMIN").exists()


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_admin_cannot_hijack_a_student_account(students):
    u = student_otp.provision_student_user(SID)
    admin = User.objects.create_superuser(username="root1", password="x", email="")
    c = Client()
    c.force_login(admin)
    r = c.post(
        reverse("users_set_password"),
        data=json.dumps({"username": str(SID), "new_password": "Str0ng!Passw0rd"}),
        content_type="application/json",
    )
    assert r.status_code == 409
    u.refresh_from_db()
    assert not u.has_usable_password()  # OTP login still works


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STUDENT_LOGIN_NO_OTP=True,
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
)
def test_no_otp_testing_mode_logs_in_directly(students):
    """Testing bypass: Uni ID alone signs in, and no code is ever emailed."""
    c = Client()
    mail.outbox = []
    r = c.post("/student/login/", {"student_id": str(SID)})
    assert r.status_code == 302 and r.headers["Location"] == "/student/"
    assert mail.outbox == []  # no OTP sent at all
    assert c.get("/student/").status_code == 200  # really authenticated
    assert not StudentLoginOTP.objects.filter(student_id=SID).exists()


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_LOGIN_NO_OTP=True)
def test_no_otp_mode_still_rejects_unknown_id(students):
    c = Client()
    r = c.post("/student/login/", {"student_id": "9999999"})
    assert r.status_code == 200  # stays on the form, no session
    assert c.get("/student/").status_code in (302, 409)  # not logged in


def test_no_otp_bypass_is_inert_without_debug(monkeypatch):
    """The bypass must be impossible to enable in production (DEBUG=False)."""
    import importlib

    monkeypatch.setenv("STUDENT_LOGIN_NO_OTP", "true")
    monkeypatch.setenv("DJANGO_DEBUG", "false")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "x" * 50)
    try:
        cfg = importlib.reload(importlib.import_module("config.settings"))
        assert cfg.DEBUG is False
        assert cfg.STUDENT_LOGIN_NO_OTP is False  # env alone cannot switch it on
    finally:
        # restore the real settings module for every later test
        monkeypatch.undo()
        importlib.reload(importlib.import_module("config.settings"))


def test_eligible_this_term_excludes_future_terms(students):
    """'Available this term' must exclude future-term courses that the whole-plan
    prerequisite count includes, and must be a superset of the credit-capped list."""
    from core.models import Course, ProgrammeRequirement, StudentCourse
    from core.services.recommender import eligible_next_term_courses, recommend_next_courses

    prog = "DS2"
    Student.objects.filter(student_id=SID).update(program=prog)
    for code, term in (("AAA101", 1), ("BBB201", 3), ("ZZZ901", 9)):
        Course.objects.get_or_create(course_code=code, defaults={"credit_hours": 3})
        ProgrammeRequirement.objects.get_or_create(
            program=prog,
            course_code=code,
            defaults={"programme_term": term, "credit_hours": 3, "type": "core"},
        )
    # student has passed nothing -> real term 1, so the coming term is 2 (even parity)
    eligible = eligible_next_term_courses(SID, 1448, 1)
    assert "ZZZ901" not in eligible  # a term-9 course is never "this term"
    capped = recommend_next_courses(SID, 1448, 1, resolve_electives=False)
    assert set(capped).issubset(set(eligible))  # recommendation ⊆ eligible
    StudentCourse.objects.filter(student_id=SID).delete()


def test_provision_refuses_advisor_group_account(students):
    from core.services.rbac import ROLE_ADVISOR

    ensure_role_groups()
    adv = User.objects.create_user(username=str(SID), password="x")
    adv.groups.add(Group.objects.get(name=ROLE_ADVISOR))
    with pytest.raises(student_otp.OTPError):
        student_otp.provision_student_user(SID)
