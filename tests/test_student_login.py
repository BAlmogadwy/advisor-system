"""Student OTP login + role/scope isolation (Phase A + B)."""

import hashlib
import hmac
import ipaddress
import json
import logging
import re
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core import mail
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from core.authz import ROLE_ORDER
from core.models import RateLimitBucket, Student, StudentLoginOTP
from core.services import rate_limit, student_otp
from core.services.rate_limit import STUDENT_OTP_SEND, STUDENT_OTP_VERIFY
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
RETIRED_TEST_INBOX = "operator@example.invalid"
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _safe_test_email_backend(settings):
    """OTP tests must never inherit a workstation's live SMTP configuration."""

    settings.EMAIL_BACKEND = LOCMEM
    settings.STUDENT_OTP_ASYNC_EMAIL = False


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


def _create_active_otp(*, student_id: int, code: str) -> StudentLoginOTP:
    now = timezone.now()
    return StudentLoginOTP.objects.create(
        student_id=student_id,
        code_hash=student_otp._hash(code),
        expires_at=now + timedelta(minutes=10),
        consumed=False,
        delivery_channel=StudentLoginOTP.DeliveryChannel.SENDGRID,
        delivery_status=StudentLoginOTP.DeliveryStatus.ACCEPTED,
        delivery_finished_at=now,
    )


def _use_locmem_for_http_student_login(monkeypatch) -> None:
    """Keep HTTP auth tests local while production views explicitly use SendGrid."""

    from core import student_auth_views

    def issue_via_locmem(student_id, ip="", **_delivery_options):
        return student_otp.issue_otp(
            student_id,
            ip,
            channel=student_otp.CHANNEL_SMTP,
            min_interval_seconds=0,
        )

    monkeypatch.setattr(student_auth_views, "issue_otp", issue_via_locmem)


@pytest.mark.parametrize(
    ("student_id", "expected"),
    [
        ("3500001", "3500001@taibahu.edu.sa"),
        ("4402162", "4402162@taibahu.edu.sa"),
        ("4500001", "tu4500001@taibahu.edu.sa"),
        ("٤٦٠٠٠٠١", "tu4600001@taibahu.edu.sa"),
        ("  ٠٠٤٧٠٠٠٠١  ", "tu4700001@taibahu.edu.sa"),
    ],
)
def test_student_email_uses_the_cohort_rule_and_ascii_normalization(student_id, expected):
    assert student_otp.student_email(student_id) == expected


@pytest.mark.parametrize(
    "student_id",
    [
        True,
        False,
        None,
        "",
        "   ",
        "44 02162",
        "44-02162",
        "44.02162",
        "④④02162",
        "4" * 11,
        -4402162,
        2_147_483_648,
    ],
)
def test_student_email_rejects_malformed_ids(student_id):
    with pytest.raises(ValueError):
        student_otp.student_email(student_id)


def test_otp_entry_points_fail_closed_for_malformed_student_ids():
    mail.outbox = []

    with pytest.raises(student_otp.OTPError):
        student_otp.issue_otp(True)
    with pytest.raises(student_otp.OTPError):
        student_otp.provision_student_user("44-02162")
    assert student_otp.verify_otp("not-an-id", "123456") is False
    assert mail.outbox == []
    assert not StudentLoginOTP.objects.exists()


@override_settings(EMAIL_BACKEND=LOCMEM, STUDENT_OTP_ASYNC_EMAIL=False)
def test_issue_hashes_and_emails(students):
    mail.outbox = []
    student_otp.issue_otp(SID, "1.2.3.4")
    assert mail.outbox[-1].to == [f"tu{SID}@taibahu.edu.sa"]
    code = _code_from_mail()
    assert len(code) == 6
    # plaintext code is never persisted, only its hash
    assert not StudentLoginOTP.objects.filter(code_hash=code).exists()
    otp = StudentLoginOTP.objects.get(student_id=SID, consumed=False)
    assert otp.request_ip == student_otp._request_ip_fingerprint("1.2.3.4")
    assert otp.request_ip != "1.2.3.4"
    assert re.fullmatch(r"[0-9a-f]{64}", otp.request_ip)


def test_otp_request_ip_fingerprint_normalizes_equivalent_ipv6_without_storing_it():
    expanded = "2001:0db8:0000:0000:0000:0000:0000:0042"
    compressed = "2001:db8::42"

    assert student_otp._request_ip_fingerprint(expanded) == (
        student_otp._request_ip_fingerprint(compressed)
    )
    assert expanded not in student_otp._request_ip_fingerprint(expanded)


@override_settings(EMAIL_BACKEND=LOCMEM, STUDENT_OTP_ASYNC_EMAIL=False)
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


def test_student_logout_redirects_to_student_login(students):
    client = Client()
    student = student_otp.provision_student_user(SID)
    client.force_login(student)

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("student_login")
    assert "_auth_user_id" not in client.session


def test_staff_logout_still_redirects_to_staff_login():
    client = Client()
    staff = User.objects.create_user(username="logout-advisor", password="unused")
    client.force_login(staff)

    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("login")
    assert "_auth_user_id" not in client.session


def test_student_portal_defaults_to_arabic_and_its_own_light_theme():
    client = Client()

    response = client.get(reverse("student_login"), HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
    html = response.content.decode()

    assert '<html lang="ar" dir="rtl" data-portal="student">' in html
    assert "var storageKey = studentPortal ? 'student-theme' : 'theme';" in html
    assert "t = studentPortal ? 'light'" in html
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "ar"


def test_shared_footer_uses_the_correct_arabic_name():
    template = (settings.BASE_DIR / "core/templates/core/base.html").read_text(encoding="utf-8")

    assert "تصميم وتطوير <strong>د. بسام المغذوي</strong>" in template
    assert "د. بسام المجدوي" not in template


def test_student_explicit_language_choice_is_respected():
    client = Client()
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"

    response = client.get(reverse("student_login"))

    assert '<html lang="en" dir="ltr" data-portal="student">' in response.content.decode()


def test_staff_login_keeps_the_existing_language_and_theme_defaults():
    response = Client().get(reverse("login"), HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
    html = response.content.decode()

    assert '<html lang="en" dir="ltr" data-portal="staff">' in html
    assert "var storageKey = studentPortal ? 'student-theme' : 'theme';" in html


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    ALLOWED_HOSTS=["testserver"],
)
def test_full_http_login_and_isolation(students, monkeypatch):
    _use_locmem_for_http_student_login(monkeypatch)
    c = Client()
    assert c.get("/student/login/").status_code == 200
    mail.outbox = []
    r = c.post("/student/login/", {"student_id": str(SID)})
    assert r.status_code == 200 and b'name="code"' in r.content
    assert r.context["email"] == f"tu{SID}@taibahu.edu.sa"
    assert mail.outbox[-1].to == [r.context["email"]]
    code = _code_from_mail()
    r = c.post("/student/login/verify/", {"code": code})
    assert r.status_code == 302 and r.headers["Location"] == "/student/"
    assert c.get("/student/").status_code == 200
    # a logged-in student is denied every advisor endpoint (own id and others)
    assert c.get(f"/report/student-plan/?student_id={SID}").status_code == 403
    assert c.get(f"/report/student-plan/?student_id={OTHER}").status_code == 403
    assert c.get("/report/summary/").status_code == 403


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_http_login_normalizes_arabic_digits_and_displays_the_canonical_44_email(
    students, monkeypatch
):
    _use_locmem_for_http_student_login(monkeypatch)
    legacy_id = 4402162
    Student.objects.create(student_id=legacy_id, name="Legacy cohort", program="AI", section="M")
    arabic_id = str(legacy_id).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    c = Client()
    mail.outbox = []

    response = c.post(
        "/student/login/",
        {"student_id": arabic_id},
        REMOTE_ADDR="10.22.0.1",
    )

    assert response.status_code == 200 and b'name="code"' in response.content
    assert response.context["student_id"] == str(legacy_id)
    assert response.context["email"] == f"{legacy_id}@taibahu.edu.sa"
    assert mail.outbox[-1].to == [response.context["email"]]


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    ALLOWED_HOSTS=["testserver"],
)
def test_unknown_id_is_enumeration_resistant(students):
    c = Client()
    mail.outbox = []
    r = c.post("/student/login/", {"student_id": "9999999"})  # no such student
    assert r.status_code == 200 and b'name="code"' in r.content  # same OTP step as a real id
    assert mail.outbox == []  # but nothing was actually sent
    assert c.post("/student/login/verify/", {"code": "123456"}).status_code == 200  # cannot log in


# ── SendGrid delivery + server-owned resend cooldown ──


def _set_otp_resend_session(
    client: Client,
    *,
    internal_student_id: int = SID,
    display_student_id: int = SID,
    available_at: float = 1050.0,
) -> None:
    session = client.session
    session["otp_student_id"] = internal_student_id
    session["otp_display_student_id"] = str(display_student_id)
    session["otp_display_email"] = student_otp.student_email(display_student_id)
    session["otp_resend_available_at"] = available_at
    session.save()


def _otp_ip_bucket_key(budget: str, raw_ip: str) -> str:
    normalized = ipaddress.ip_address(raw_ip).compressed.lower()
    digest = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{budget}:{int.from_bytes(digest, 'big', signed=False)}"


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_initial_login_uses_sendgrid_and_stores_enumeration_safe_resend_state(
    students, monkeypatch
):
    from core import student_auth_views

    calls = []
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 1000.0)
    monkeypatch.setattr(
        student_auth_views,
        "issue_otp",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    known = Client()
    known_response = known.post(
        reverse("student_login"),
        {"student_id": str(SID)},
        REMOTE_ADDR="10.31.0.1",
    )
    unknown = Client()
    unknown_id = 4999999
    unknown_response = unknown.post(
        reverse("student_login"),
        {"student_id": str(unknown_id)},
        REMOTE_ADDR="10.31.0.2",
    )

    assert calls == [
        (
            (SID, "10.31.0.1"),
            {
                "channel": student_otp.CHANNEL_SENDGRID,
                "min_interval_seconds": 50,
            },
        )
    ]
    for response, client, display_id, internal_id in (
        (known_response, known, SID, SID),
        (unknown_response, unknown, unknown_id, 0),
    ):
        assert response.status_code == 200
        assert response.context["step"] == "otp"
        assert response.context["resend_seconds"] == 50
        assert response.context["sent"] is True
        session = client.session
        assert session["otp_student_id"] == internal_id
        assert session["otp_display_student_id"] == str(display_id)
        assert session["otp_display_email"] == student_otp.student_email(display_id)
        assert session["otp_resend_available_at"] == 1050.0
        html = response.content.decode()
        assert 'id="otpResendBtn"' in html
        assert 'data-remaining="50"' in html
        assert 'data-deadline-ms="1050000"' in html
        assert "disabled" in html
        assert "countdownEndsAt - Date.now()" in html
        assert "visibilitychange" in html and "pageshow" in html


@override_settings(STUDENT_OTP_ASYNC_EMAIL=False, STUDENT_OTP_MAX_SENDS=3)
def test_failed_sendgrid_resend_preserves_the_prior_active_code(students, monkeypatch):
    previous_code = "123456"
    previous = _create_active_otp(student_id=SID, code=previous_code)

    def fail_sendgrid(*args, **kwargs):
        raise RuntimeError("private provider failure")

    monkeypatch.setattr(student_otp, "send_transactional_email", fail_sendgrid)

    student_otp.issue_otp(SID, channel=student_otp.CHANNEL_SENDGRID)

    previous.refresh_from_db()
    replacement = StudentLoginOTP.objects.exclude(pk=previous.pk).get(student_id=SID)
    assert previous.consumed is False
    assert previous.delivery_status == StudentLoginOTP.DeliveryStatus.ACCEPTED
    assert replacement.consumed is True
    assert replacement.delivery_status == StudentLoginOTP.DeliveryStatus.FAILED
    assert student_otp.verify_otp(SID, previous_code) is True


@override_settings(STUDENT_OTP_ASYNC_EMAIL=False, STUDENT_OTP_MAX_SENDS=3)
def test_sendgrid_acceptance_activates_only_the_newest_candidate(students, monkeypatch):
    dispatches: list[dict[str, object]] = []
    monkeypatch.setattr(
        student_otp,
        "_dispatch_code",
        lambda **kwargs: dispatches.append(kwargs),
    )

    student_otp.issue_otp(SID, channel=student_otp.CHANNEL_SENDGRID)
    student_otp.issue_otp(SID, channel=student_otp.CHANNEL_SENDGRID)

    first_id = int(dispatches[0]["otp_id"])
    newest_id = int(dispatches[1]["otp_id"])
    first = StudentLoginOTP.objects.get(pk=first_id)
    newest = StudentLoginOTP.objects.get(pk=newest_id)
    assert first.delivery_status == StudentLoginOTP.DeliveryStatus.CANCELLED
    assert first.consumed is True
    assert newest.delivery_status == StudentLoginOTP.DeliveryStatus.PENDING
    assert newest.consumed is True

    # The older provider request acknowledges after the replacement was queued.
    student_otp._accept_sendgrid_delivery(SID, first_id, "late-first")
    first.refresh_from_db()
    newest.refresh_from_db()
    assert first.delivery_status == StudentLoginOTP.DeliveryStatus.CANCELLED
    assert first.consumed is True
    assert newest.delivery_status == StudentLoginOTP.DeliveryStatus.PENDING
    assert newest.consumed is True

    student_otp._accept_sendgrid_delivery(SID, newest_id, "accepted-newest")
    first.refresh_from_db()
    newest.refresh_from_db()
    assert first.delivery_status == StudentLoginOTP.DeliveryStatus.CANCELLED
    assert first.consumed is True
    assert newest.delivery_status == StudentLoginOTP.DeliveryStatus.ACCEPTED
    assert newest.consumed is False
    assert newest.provider_message_id == "accepted-newest"


@override_settings(STUDENT_OTP_ASYNC_EMAIL=False, STUDENT_OTP_MAX_SENDS=3)
def test_successful_verification_cancels_inflight_replacement_and_late_acceptance(
    students, monkeypatch
):
    previous_code = "654321"
    previous = _create_active_otp(student_id=SID, code=previous_code)
    dispatches: list[dict[str, object]] = []
    monkeypatch.setattr(
        student_otp,
        "_dispatch_code",
        lambda **kwargs: dispatches.append(kwargs),
    )

    student_otp.issue_otp(SID, channel=student_otp.CHANNEL_SENDGRID)
    replacement_id = int(dispatches[0]["otp_id"])
    replacement_code = str(dispatches[0]["code"])

    assert student_otp.verify_otp(SID, previous_code) is True
    previous.refresh_from_db()
    replacement = StudentLoginOTP.objects.get(pk=replacement_id)
    assert previous.consumed is True
    assert replacement.delivery_status == StudentLoginOTP.DeliveryStatus.CANCELLED
    assert replacement.consumed is True

    # A provider 202 arriving after the old code was verified must be inert.
    student_otp._accept_sendgrid_delivery(SID, replacement_id, "late-after-login")
    replacement.refresh_from_db()
    assert replacement.delivery_status == StudentLoginOTP.DeliveryStatus.CANCELLED
    assert replacement.consumed is True
    assert replacement.provider_message_id == ""
    assert not StudentLoginOTP.objects.filter(student_id=SID, consumed=False).exists()
    assert student_otp.verify_otp(SID, replacement_code) is False


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_RESEND_DELAY_SECONDS=50,
)
def test_restarting_same_id_cannot_bypass_resend_cooldown(students, monkeypatch):
    monkeypatch.setattr(
        student_otp,
        "send_transactional_email",
        lambda *args, **kwargs: "test-message-id",
    )
    client = Client()
    first = client.post(
        reverse("student_login"),
        {"student_id": str(SID)},
        REMOTE_ADDR="10.31.1.1",
    )
    assert first.status_code == 200
    assert StudentLoginOTP.objects.filter(student_id=SID).count() == 1

    # Going back to the first step and posting the same id must not create a
    # second request before the same server-side 50-second minimum has elapsed.
    assert client.get(reverse("student_login")).status_code == 200
    restarted = client.post(
        reverse("student_login"),
        {"student_id": str(SID)},
        REMOTE_ADDR="10.31.1.1",
    )

    assert restarted.status_code == 200
    assert restarted.context["resend_seconds"] == 50
    assert StudentLoginOTP.objects.filter(student_id=SID).count() == 1


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STUDENT_OTP_RESPONSE_FLOOR_SECONDS=3.5,
)
def test_initial_known_and_unknown_ids_share_monotonic_response_floor(students, monkeypatch):
    from core import student_auth_views

    ticks = iter((10.0, 11.0, 20.0, 21.0))
    sleeps = []
    monkeypatch.setattr(student_auth_views, "_monotonic_now", lambda: next(ticks))
    monkeypatch.setattr(student_auth_views, "_sleep_seconds", sleeps.append)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    monkeypatch.setattr(student_auth_views, "issue_otp", lambda *args, **kwargs: None)

    known = Client().post(
        reverse("student_login"),
        {"student_id": str(SID)},
        REMOTE_ADDR="10.31.2.1",
    )
    unknown = Client().post(
        reverse("student_login"),
        {"student_id": "4999999"},
        REMOTE_ADDR="10.31.2.2",
    )

    assert known.status_code == unknown.status_code == 200
    assert known.context["step"] == unknown.context["step"] == "otp"
    assert sleeps == [pytest.approx(2.5), pytest.approx(2.5)]


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STUDENT_OTP_RESPONSE_FLOOR_SECONDS=3.5,
)
def test_resend_known_and_unknown_ids_share_monotonic_response_floor(students, monkeypatch):
    from core import student_auth_views

    ticks = iter((30.0, 31.25, 40.0, 41.25))
    sleeps = []
    monkeypatch.setattr(student_auth_views, "_monotonic_now", lambda: next(ticks))
    monkeypatch.setattr(student_auth_views, "_sleep_seconds", sleeps.append)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    monkeypatch.setattr(student_auth_views, "issue_otp", lambda *args, **kwargs: None)

    known_client = Client()
    _set_otp_resend_session(known_client, available_at=0)
    unknown_client = Client()
    _set_otp_resend_session(
        unknown_client,
        internal_student_id=0,
        display_student_id=4999999,
        available_at=0,
    )
    known = known_client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.31.3.1")
    unknown = unknown_client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.31.3.2")

    assert known.status_code == unknown.status_code == 200
    assert known.context["resent"] is unknown.context["resent"] is True
    assert sleeps == [pytest.approx(2.25), pytest.approx(2.25)]


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    STUDENT_OTP_RESPONSE_FLOOR_SECONDS=1.5,
)
def test_failed_verification_known_and_unknown_ids_share_monotonic_response_floor(
    students, monkeypatch
):
    from core import student_auth_views

    ticks = iter((10.0, 11.0, 20.0, 20.25))
    sleeps = []
    monkeypatch.setattr(student_auth_views, "_monotonic_now", lambda: next(ticks))
    monkeypatch.setattr(student_auth_views, "_sleep_seconds", sleeps.append)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)

    known_client = Client()
    _set_otp_resend_session(known_client)
    unknown_client = Client()
    _set_otp_resend_session(
        unknown_client,
        internal_student_id=0,
        display_student_id=4999999,
    )

    known = known_client.post(
        reverse("student_otp_verify"),
        {"code": "000000"},
        REMOTE_ADDR="10.31.4.1",
    )
    unknown = unknown_client.post(
        reverse("student_otp_verify"),
        {"code": "000000"},
        REMOTE_ADDR="10.31.4.2",
    )

    assert known.status_code == unknown.status_code == 200
    assert known.context["step"] == unknown.context["step"] == "otp"
    assert known.context["error"] == unknown.context["error"]
    assert sleeps == [pytest.approx(0.5), pytest.approx(1.25)]


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_resend_is_blocked_before_and_allowed_at_exact_server_boundary(students, monkeypatch):
    from core import student_auth_views

    client = Client()
    _set_otp_resend_session(client)
    calls = []

    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 1049.999)

    def quota_must_not_be_touched(*args, **kwargs):
        raise AssertionError("an early resend must not consume the IP budget")

    monkeypatch.setattr(student_auth_views, "_ip_throttled", quota_must_not_be_touched)
    monkeypatch.setattr(
        student_auth_views,
        "issue_otp",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    early = client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.32.0.1")
    assert early.status_code == 200
    assert early.context["resend_too_soon"] is True
    assert early.context["resend_seconds"] == 1
    assert calls == []
    assert client.session["otp_resend_available_at"] == 1050.0

    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 1050.0)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    boundary = client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.32.0.1")

    assert boundary.status_code == 200
    assert boundary.context["resent"] is True
    assert boundary.context["resend_seconds"] == 50
    assert calls == [
        (
            (SID, "10.32.0.1"),
            {
                "channel": student_otp.CHANNEL_SENDGRID,
                "min_interval_seconds": 50,
            },
        )
    ]
    assert client.session["otp_resend_available_at"] == 1100.0


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_resend_ignores_tampered_identity_email_and_provider(students, monkeypatch):
    from core import student_auth_views

    client = Client()
    _set_otp_resend_session(client, available_at=2000.0)
    calls = []
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 2000.0)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        student_auth_views,
        "issue_otp",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = client.post(
        reverse("student_otp_resend"),
        {
            "student_id": str(OTHER),
            "email": "attacker@example.com",
            "provider": "smtp",
            "channel": "smtp",
        },
        REMOTE_ADDR="10.33.0.1",
    )

    assert response.status_code == 200
    assert calls[0][0] == (SID, "10.33.0.1")
    assert calls[0][1]["channel"] == student_otp.CHANNEL_SENDGRID
    assert response.context["student_id"] == str(SID)
    assert response.context["email"] == student_otp.student_email(SID)
    assert "attacker@example.com" not in response.content.decode()


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_resend_requires_post_session_and_csrf(students, monkeypatch):
    from core import student_auth_views

    assert Client().get(reverse("student_otp_resend")).status_code == 405
    assert Client().post(reverse("student_otp_resend")).headers["Location"] == reverse(
        "student_login"
    )

    csrf_client = Client(enforce_csrf_checks=True)
    monkeypatch.setattr(student_auth_views, "issue_otp", lambda *args, **kwargs: None)
    page = csrf_client.get(reverse("student_login"))
    token = page.cookies["csrftoken"].value
    started = csrf_client.post(
        reverse("student_login"),
        {"student_id": str(SID), "csrfmiddlewaretoken": token},
        REMOTE_ADDR="10.34.0.1",
    )
    assert started.status_code == 200
    assert csrf_client.post(reverse("student_otp_resend")).status_code == 403


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_wrong_code_keeps_countdown_error_and_next_destination(students, monkeypatch):
    from core import student_auth_views

    client = Client()
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 3000.0)
    monkeypatch.setattr(student_auth_views, "issue_otp", lambda *args, **kwargs: None)
    destination = reverse("student_courses")
    assert client.get(f"{reverse('student_login')}?next={destination}").status_code == 200
    assert (
        client.post(
            reverse("student_login"),
            {"student_id": str(SID)},
            REMOTE_ADDR="10.35.0.1",
        ).status_code
        == 200
    )
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 3050.0)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    resent = client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.35.0.1")
    assert resent.status_code == 200 and resent.context["resent"] is True
    deadline = client.session["otp_resend_available_at"]
    assert deadline == 3100.0
    assert client.session["post_login_next"]["url"] == destination

    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 3071.25)
    monkeypatch.setattr(student_auth_views, "verify_otp", lambda *args, **kwargs: False)
    response = client.post(
        reverse("student_otp_verify"),
        {"code": "000000"},
        REMOTE_ADDR="10.35.0.1",
    )

    assert response.status_code == 200
    assert response.context["error"] == "رمز التحقق غير صحيح أو انتهت صلاحيته."
    assert response.context["resend_seconds"] == 29
    assert client.session["otp_resend_available_at"] == deadline
    assert client.session["post_login_next"]["url"] == destination
    html = response.content.decode()
    assert 'data-remaining="29"' in html
    assert 'id="otpResendBtn"' in html and "disabled" in html
    assert "استخدم الرمز الصادر عن آخر طلب إرسال ناجح" in html
    assert "فقد يظل الرمز السابق صالحًا" in html


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_unknown_id_resend_has_same_generic_success_and_resets_timer(monkeypatch):
    from core import student_auth_views

    client = Client()
    unknown_id = 4999999
    _set_otp_resend_session(
        client,
        internal_student_id=0,
        display_student_id=unknown_id,
        available_at=4000.0,
    )
    calls = []
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 4000.0)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        student_auth_views,
        "issue_otp",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.36.0.1")

    assert response.status_code == 200
    assert response.context["resent"] is True
    assert response.context["resend_seconds"] == 50
    assert calls == []
    assert client.session["otp_resend_available_at"] == 4050.0
    assert "استلمنا طلبك لإرسال رمز جديد" in response.content.decode()


@override_settings(ALLOWED_HOSTS=["testserver"], STUDENT_OTP_RESEND_DELAY_SECONDS=50)
def test_resend_failure_uses_accurate_generic_copy(students, monkeypatch):
    from core import student_auth_views

    client = Client()
    _set_otp_resend_session(client, available_at=5000.0)
    monkeypatch.setattr(student_auth_views, "_now_timestamp", lambda: 5000.0)
    monkeypatch.setattr(student_auth_views, "_ip_throttled", lambda *args, **kwargs: False)

    def fail_send(*args, **kwargs):
        raise student_otp.OTPError("delivery_failed")

    monkeypatch.setattr(student_auth_views, "issue_otp", fail_send)
    response = client.post(reverse("student_otp_resend"), REMOTE_ADDR="10.37.0.1")
    html = response.content.decode()

    assert response.status_code == 200
    assert response.context["resent"] is True
    assert "استلمنا طلبك لإرسال رمز جديد" in html
    assert "أُرسل رمز جديد" not in html
    assert "الخدمة الاحتياطية" not in html


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_initial_and_resend_share_durable_hmac_ip_budget_without_storing_raw_ip(monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, STUDENT_OTP_SEND, (8, 900))
    expanded_ip = "2001:0db8:0000:0000:0000:0000:0000:0042"
    compressed_ip = "2001:db8::42"
    expected_key = _otp_ip_bucket_key(STUDENT_OTP_SEND, compressed_ip)
    client = Client()

    # Seven initial requests spend seven units. Unknown ids deliberately exercise
    # the same enumeration-safe request path without invoking the mail provider.
    for offset in range(7):
        response = client.post(
            reverse("student_login"),
            {"student_id": str(4980000 + offset)},
            REMOTE_ADDR=expanded_ip,
        )
        assert response.status_code == 200 and response.context["step"] == "otp"

    # The resend route uses a differently formatted representation of the same
    # IPv6 address and must consume the eighth unit from that exact shared row.
    session = client.session
    session["otp_resend_available_at"] = 0
    session.save()
    eighth = client.post(reverse("student_otp_resend"), REMOTE_ADDR=compressed_ip)
    assert eighth.status_code == 200 and eighth.context["resent"] is True

    blocked = client.post(
        reverse("student_login"),
        {"student_id": "4980099"},
        REMOTE_ADDR=compressed_ip,
    )
    assert blocked.status_code == 200 and blocked.context["step"] == "id"
    assert "تم تجاوز العدد المسموح" in blocked.content.decode()

    bucket = RateLimitBucket.objects.get(key=expected_key)
    assert bucket.count == 8
    assert RateLimitBucket.objects.filter(key__startswith=f"{STUDENT_OTP_SEND}:").count() == 1
    assert expanded_ip not in bucket.key and compressed_ip not in bucket.key
    assert "." not in bucket.key  # neither an IPv4 address nor an IPv6 textual form


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_verify_uses_separate_durable_15_per_15_minute_hmac_ip_budget(students, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, STUDENT_OTP_VERIFY, (15, 900))
    raw_ip = "203.0.113.77"
    expected_key = _otp_ip_bucket_key(STUDENT_OTP_VERIFY, raw_ip)
    client = Client()
    _set_otp_resend_session(client, available_at=9999999999.0)

    for _ in range(15):
        response = client.post(
            reverse("student_otp_verify"),
            {"code": "000000"},
            REMOTE_ADDR=raw_ip,
        )
        assert response.status_code == 200 and response.context["step"] == "otp"

    blocked = client.post(
        reverse("student_otp_verify"),
        {"code": "000000"},
        REMOTE_ADDR=raw_ip,
    )
    assert blocked.status_code == 200 and blocked.context["step"] == "id"
    assert "تم تجاوز العدد المسموح" in blocked.content.decode()

    bucket = RateLimitBucket.objects.get(key=expected_key)
    assert bucket.count == 15
    assert raw_ip not in bucket.key
    assert RateLimitBucket.objects.filter(key__startswith=f"{STUDENT_OTP_VERIFY}:").count() == 1


def test_otp_ip_budgets_are_coarse_shared_nat_backstops():
    assert rate_limit.LIMITS[STUDENT_OTP_SEND] == (2000, 900)
    assert rate_limit.LIMITS[STUDENT_OTP_VERIFY] == (5000, 900)


def _start_email_otp_login(client: Client, *, destination: str = "", ip: str) -> None:
    if destination:
        response = client.get(f"{reverse('student_login')}?next={destination}")
        assert response.status_code == 200
    mail.outbox = []
    response = client.post(
        reverse("student_login"),
        {"student_id": str(SID)},
        REMOTE_ADDR=ip,
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 1


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL=RETIRED_TEST_INBOX,
    TELEGRAM_LINK_OTP_REDIRECT_EMAIL=RETIRED_TEST_INBOX,
    TELEGRAM_ADVISOR_ENABLED=True,
    ALLOWED_HOSTS=["testserver"],
)
def test_retired_redirect_settings_cannot_change_telegram_link_otp_recipient(students, monkeypatch):
    from telegram_gateway import linking

    _use_locmem_for_http_student_login(monkeypatch)
    issued = linking.issue_link_token(telegram_user_id=70000001)
    destination = reverse("telegram_link_start", args=[issued.raw_token])
    client = Client()

    _start_email_otp_login(client, destination=destination, ip="10.21.0.1")

    email = mail.outbox[-1]
    assert email.to == [f"tu{SID}@taibahu.edu.sa"]
    assert "[testing] intended for" not in mail.outbox[-1].body


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
    STUDENT_OTP_REDIRECT_EMAIL=RETIRED_TEST_INBOX,
)
def test_retired_global_redirect_setting_is_inert_for_direct_otp(students):
    mail.outbox = []
    student_otp.issue_otp(SID)

    assert mail.outbox[-1].to == [f"tu{SID}@taibahu.edu.sa"]
    assert RETIRED_TEST_INBOX not in mail.outbox[-1].body


@override_settings(
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
)
def test_otp_delivery_failure_log_contains_no_student_or_recipient_identifier(
    students, caplog, monkeypatch
):
    def fail_delivery(*args, **kwargs):
        raise RuntimeError(f"SMTP rejected tu{SID}@taibahu.edu.sa via {RETIRED_TEST_INBOX}")

    monkeypatch.setattr(student_otp, "send_mail", fail_delivery)
    with caplog.at_level(logging.ERROR, logger="core.services.student_otp"):
        student_otp.issue_otp(SID)

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "core.services.student_otp" and record.levelno >= logging.ERROR
    ]
    assert errors == ["student OTP email delivery failed"]
    assert str(SID) not in caplog.text
    assert f"tu{SID}@taibahu.edu.sa" not in caplog.text
    assert RETIRED_TEST_INBOX not in caplog.text


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
    assert r.context["timetable_panels"] == []
    body = r.content.decode()
    assert (
        "We do not yet have registered or expected timetable data for this term." in body
        or "لا تتوفر حاليًا بيانات للجدول المسجّل فعليًا أو الجدول المتوقع لهذا الفصل." in body
    )


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
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=ts,
            source="scraper_timetable",
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
    STUDENT_OTP_REDIRECT_EMAIL=RETIRED_TEST_INBOX,
    TELEGRAM_LINK_OTP_REDIRECT_EMAIL=RETIRED_TEST_INBOX,
    EMAIL_BACKEND=LOCMEM,
    STUDENT_OTP_ASYNC_EMAIL=False,
)
def test_retired_test_controls_cannot_bypass_otp_or_redirect_delivery(students, monkeypatch):
    _use_locmem_for_http_student_login(monkeypatch)
    c = Client()
    mail.outbox = []

    login_page = c.get("/student/login/")
    assert b"TESTING MODE" not in login_page.content
    r = c.post("/student/login/", {"student_id": str(SID)})
    assert r.status_code == 200 and b'name="code"' in r.content
    assert mail.outbox[-1].to == [f"tu{SID}@taibahu.edu.sa"]
    assert StudentLoginOTP.objects.filter(student_id=SID, consumed=False).exists()
    assert c.get("/student/").status_code in (302, 409)


def test_project_settings_do_not_define_retired_student_auth_controls():
    from config import settings as project_settings

    assert not hasattr(project_settings, "STUDENT_LOGIN_NO_OTP")
    assert not hasattr(project_settings, "STUDENT_OTP_REDIRECT_EMAIL")
    assert not hasattr(project_settings, "TELEGRAM_LINK_OTP_REDIRECT_EMAIL")


def test_delivery_fields_keep_database_defaults_for_rolling_deploy(students):
    """The old web process can still insert OTP rows after migration 0064.

    Render runs migrations before it switches traffic to the new release, so the
    database—not only the new Django model—must supply values for new NOT NULL
    columns omitted by the old INSERT statement.
    """

    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO core_student_login_otp
                (student_id, code_hash, created_at, expires_at, attempts,
                 consumed, request_ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [
                SID,
                "0" * 64,
                now,
                now,
                0,
                False,
                "",
            ],
        )
        inserted_id = cursor.fetchone()[0]

    row = StudentLoginOTP.objects.get(pk=inserted_id)
    assert row.delivery_channel == StudentLoginOTP.DeliveryChannel.SMTP
    assert row.delivery_status == StudentLoginOTP.DeliveryStatus.SKIPPED
    assert row.provider_message_id == ""


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
