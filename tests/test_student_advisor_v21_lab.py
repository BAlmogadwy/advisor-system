import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from core.models import Student
from core.services.rbac import ROLE_STUDENT, get_user_scope

pytestmark = pytest.mark.django_db

LAB_URL = "/ops/dev/student-advisor-v21/"
STUDENT_ID = 4901234


def _superuser(username: str = "v21-lab-root") -> User:
    return User.objects.create_superuser(username=username, password="not-used", email="")


def _student() -> Student:
    return Student.objects.create(
        student_id=STUDENT_ID,
        name="V2.1 Lab Student",
        program="AI",
        section="M",
    )


@override_settings(DEBUG=False, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_is_not_found_outside_debug() -> None:
    client = Client()
    client.force_login(_superuser())

    assert client.get(LAB_URL).status_code == 404


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=False)
def test_lab_is_not_found_without_explicit_opt_in() -> None:
    client = Client()
    client.force_login(_superuser())

    assert client.get(LAB_URL).status_code == 404


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_is_not_found_for_a_non_loopback_peer() -> None:
    client = Client()
    client.force_login(_superuser())

    assert client.get(LAB_URL, REMOTE_ADDR="192.0.2.40").status_code == 404


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_requires_an_authenticated_user_after_environment_guards_pass() -> None:
    response = Client().get(LAB_URL, REMOTE_ADDR="127.0.0.1")

    assert response.status_code == 302
    assert response.url.startswith(f"{reverse('login')}?next=")


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_staff_login_returns_to_the_requested_lab_launcher() -> None:
    password = "local-lab-password"
    user = User.objects.create_superuser(
        username="v21-login-root",
        password=password,
        email="",
    )
    client = Client()

    login_page = client.get(
        f"{reverse('login')}?next={LAB_URL}",
        REMOTE_ADDR="127.0.0.1",
    )
    assert login_page.status_code == 200
    assert f'name="next" value="{LAB_URL}"'.encode() in login_page.content

    response = client.post(
        reverse("login"),
        {"username": user.username, "password": password, "next": LAB_URL},
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 302
    assert response.url == LAB_URL


def test_staff_login_rejects_an_external_next_destination() -> None:
    password = "local-lab-password"
    user = User.objects.create_superuser(
        username="v21-external-next-root",
        password=password,
        email="",
    )

    response = Client().post(
        reverse("login"),
        {
            "username": user.username,
            "password": password,
            "next": "https://example.test/steal-session",
        },
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 302
    assert response.url == reverse("dashboard")


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_refuses_an_authenticated_non_superuser() -> None:
    client = Client()
    client.force_login(User.objects.create_user(username="v21-lab-staff"))

    assert client.get(LAB_URL, REMOTE_ADDR="127.0.0.1").status_code == 403


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_returns_an_existing_linked_student_session_to_the_prompt_page() -> None:
    _student()
    from core.services.student_otp import provision_student_user

    client = Client()
    client.force_login(provision_student_user(STUDENT_ID))

    response = client.get(LAB_URL, REMOTE_ADDR="127.0.0.1")

    assert response.status_code == 302
    assert response.url == f"{reverse('student_advisor')}?lab=1"


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=True)
def test_lab_post_is_csrf_protected() -> None:
    _student()
    client = Client(enforce_csrf_checks=True)
    client.force_login(_superuser())

    response = client.post(
        LAB_URL,
        {"student_id": str(STUDENT_ID)},
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 403
    assert get_user_scope(response.wsgi_request.user)["role"] != ROLE_STUDENT


@override_settings(
    DEBUG=True,
    ALLOW_DEV_STUDENT_ADVISOR_LAB=True,
    STUDENT_ADVISOR_V2_ENABLED=True,
    STUDENT_ADVISOR_V21_ENABLED=True,
    LLM_BACKEND="local",
)
def test_superuser_can_enter_the_real_student_adviser_in_lab_mode() -> None:
    _student()
    client = Client(enforce_csrf_checks=True)
    client.force_login(_superuser())

    launcher = client.get(LAB_URL, REMOTE_ADDR="127.0.0.1")
    assert launcher.status_code == 200
    assert b'name="student_id"' in launcher.content
    assert b"V2.1 enabled" in launcher.content
    csrf_token = client.cookies["csrftoken"].value

    switched = client.post(
        LAB_URL,
        {"student_id": str(STUDENT_ID)},
        HTTP_X_CSRFTOKEN=csrf_token,
        REMOTE_ADDR="127.0.0.1",
    )

    assert switched.status_code == 302
    assert switched.url == f"{reverse('student_advisor')}?lab=1"
    session_user = User.objects.get(pk=int(client.session["_auth_user_id"]))
    scope = get_user_scope(session_user)
    assert not session_user.is_superuser
    assert scope == {
        "role": ROLE_STUDENT,
        "advisor_id": "",
        "departments": [],
        "student_id": STUDENT_ID,
    }

    page = client.get(switched.url, REMOTE_ADDR="127.0.0.1")
    assert page.status_code == 200
    html = page.content.decode()
    assert 'id="saQuestion"' in html
    assert 'id="saV21LabBanner"' in html
    assert "V2.1 enabled" in html
    assert "Regex false positive" in html
    assert "tool_results" not in html


@override_settings(DEBUG=True, ALLOW_DEV_STUDENT_ADVISOR_LAB=False)
def test_lab_query_does_not_reveal_the_banner_when_lab_is_disabled() -> None:
    _student()
    from core.services.student_otp import provision_student_user

    client = Client()
    client.force_login(provision_student_user(STUDENT_ID))

    page = client.get(
        f"{reverse('student_advisor')}?lab=1",
        REMOTE_ADDR="127.0.0.1",
    )

    assert page.status_code == 200
    assert b'id="saV21LabBanner"' not in page.content
