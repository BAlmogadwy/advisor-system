"""Arabic and account-boundary regressions for the shared profile page."""

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from core.models import Student
from core.services import student_otp
from core.services.rbac import ensure_role_groups


def test_student_profile_explains_otp_login_without_password_controls(db):
    ensure_role_groups()
    student_id = 4880123
    Student.objects.create(student_id=student_id, name="Profile Student", program="DS")
    client = Client()
    client.force_login(student_otp.provision_student_user(student_id))

    response = client.get(
        reverse("profile_page"),
        headers={"accept-language": "ar"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "الدخول إلى بوابة الطالب" in body
    assert "لا تستخدم بوابة الطالب كلمة مرور حساب Microsoft الجامعي" in body
    assert "pfNewUsername" not in body
    assert "pfNewPwd" not in body


def test_staff_profile_uses_the_actual_eight_character_password_minimum(db):
    ensure_role_groups()
    user = get_user_model().objects.create_user("profile-advisor", password="OldPass123!")
    client = Client()
    client.force_login(user)

    response = client.get(
        reverse("profile_page"),
        headers={"accept-language": "ar"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "pfNewUsername" in body
    assert "8 أحرف على الأقل" in body
    assert "6 أحرف على الأقل" not in body
