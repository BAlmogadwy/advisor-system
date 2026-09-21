"""Exam committee access is a narrow role, independent of advisor privileges."""

import json
from io import BytesIO
from types import ModuleType
from zipfile import ZipFile

import pytest
from django.contrib.auth.models import Group
from django.http import JsonResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import include, path, resolve, reverse

from core.authz import ROLE_ORDER, role_required
from core.exam_views import exam_timetable_delete_view
from core.models import Course, ExamTimetableRun, ProgrammeRequirement, Student, UserScope
from core.services import exam_timetable
from core.services.policy import require_program_scope, require_student_scope
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_NAMES,
    ROLE_STUDENT,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    get_user_role,
    get_user_scope,
    set_user_scope,
)
from core.views import dev_role_switch_view
from tests.exam_source_factory import scraped_exam_registration
from tests.test_exam_department_export import make_department_data

pytestmark = pytest.mark.django_db
COMMITTEE = "EXAM_COMMITTEE"
PASSWORD = "CommitteeOps!8264"


@pytest.fixture(autouse=True)
def _fast_test_passwords(settings):
    # These tests exercise authentication/authorization, not password hashing cost.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _user(model, username, roles=(), **kwargs):
    ensure_role_groups()
    user = model.objects.create_user(username=username, password=PASSWORD, **kwargs)
    user.groups.add(*(Group.objects.get(name=role) for role in roles))
    return user


@pytest.fixture
def committee(django_user_model):
    return _user(django_user_model, "committee-member", [COMMITTEE])


@pytest.fixture
def committee_client(client, committee):
    client.force_login(committee)
    return client


@pytest.fixture
def saved_run():
    return ExamTimetableRun.objects.create(
        label="Shared exam history", result_json=json.dumps(make_department_data())
    )


def test_committee_group_is_seeded_without_advisor_tier_or_staff_flags(committee):
    assert COMMITTEE in ROLE_NAMES
    assert get_user_role(committee) == COMMITTEE
    assert ROLE_ORDER[COMMITTEE] == 0
    assert not committee.is_staff and not committee.is_superuser
    assert get_user_scope(committee) == {
        "role": COMMITTEE,
        "advisor_id": "",
        "departments": [],
        "student_id": None,
    }
    request = RequestFactory().get("/advisor-only/")
    request.user = committee
    protected = role_required(ROLE_ADVISOR)(lambda request: JsonResponse({"reached": True}))
    assert protected(request).status_code == 403


@pytest.mark.parametrize(
    "roles,expected",
    [
        ([COMMITTEE, ROLE_ADVISOR], COMMITTEE),
        ([COMMITTEE, ROLE_GENERAL_ADVISOR], COMMITTEE),
        ([COMMITTEE, ROLE_ADVISOR, ROLE_GENERAL_ADVISOR], COMMITTEE),
        ([COMMITTEE, ROLE_STUDENT], ROLE_STUDENT),
        ([COMMITTEE, ROLE_GENERAL_ADVISOR, ROLE_STUDENT], ROLE_STUDENT),
        ([COMMITTEE, ROLE_SUPER_ADMIN], ROLE_SUPER_ADMIN),
        ([COMMITTEE, ROLE_STUDENT, ROLE_SUPER_ADMIN], ROLE_SUPER_ADMIN),
        ([ROLE_ADVISOR], ROLE_ADVISOR),
        ([ROLE_GENERAL_ADVISOR], ROLE_GENERAL_ADVISOR),
        ([ROLE_GENERAL_ADVISOR, ROLE_STUDENT], ROLE_GENERAL_ADVISOR),
        ([ROLE_STUDENT], ROLE_STUDENT),
        ([], ROLE_ADVISOR),
    ],
)
def test_role_precedence_keeps_committee_isolated_and_existing_roles_intact(
    django_user_model, roles, expected
):
    user = _user(django_user_model, "role-precedence", roles)
    assert get_user_role(user) == expected


def test_django_superuser_retains_superadmin_override(django_user_model):
    user = _user(
        django_user_model, "superuser-override", [COMMITTEE], is_superuser=True, is_staff=True
    )
    assert get_user_role(user) == ROLE_SUPER_ADMIN


@pytest.mark.parametrize(
    "route",
    [
        "exam_timetable_page",
        "exam_timetable_filters",
        "exam_timetable_list",
        "profile_page",
        "profile_me",
    ],
)
def test_committee_can_read_allowed_pages_and_apis(committee_client, route):
    response = committee_client.get(reverse(route))
    assert response.status_code == 200, response.content


@pytest.mark.parametrize(
    "next_path", ["/", "/user-management/", "/timetable-workspace/", "https://invalid.example/"]
)
def test_committee_password_login_always_lands_on_exam_page(client, committee, next_path):
    response = client.post(
        reverse("login"), {"username": committee.username, "password": PASSWORD, "next": next_path}
    )
    assert response.status_code == 302
    assert response.url == reverse("exam_timetable_page")
    authenticated = client.get(reverse("login"), {"next": next_path})
    assert authenticated.status_code == 302
    assert authenticated.url == reverse("exam_timetable_page")


def test_dashboard_redirects_only_get_for_committee(committee_client):
    response = committee_client.get(reverse("dashboard"))
    assert response.status_code == 302
    assert response.url == reverse("exam_timetable_page")
    assert committee_client.post(reverse("dashboard"), {}).status_code == 403


@pytest.mark.parametrize(
    "route",
    [
        "admin:index",
        "admin:login",
        "user_management_page",
        "users_list",
        "users_create",
        "users_update_role",
        "users_set_password",
        "users_delete",
        "settings_defaults",
        "timetable_workspace_page",
        "section_plan_page",
        "section_plan_generate",
        "student_home",
        "student_login",
        "student_otp_verify",
        "advisor_inbox",
        "advisor_portfolio_page",
        "report_summary",
        "virtual_advisor_page",
        "audit_explorer_api",
        "db_backup_snapshot",
        "dev_role_switch",
        "health",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_committee_direct_requests_outside_allowlist_are_denied_before_view(
    committee_client, route, method, settings, monkeypatch
):
    settings.DEBUG = True
    monkeypatch.setenv("ALLOW_DEV_ROLE_SWITCH", "true")
    url = reverse(route)
    assert resolve(url).view_name == route
    response = getattr(committee_client, method)(url, {"role": ROLE_SUPER_ADMIN})
    assert response.status_code == 403, (route, method, response.status_code)


def test_delete_guard_denies_committee_without_middleware(committee, saved_run):
    request = RequestFactory().post(
        reverse("exam_timetable_delete", args=[saved_run.pk]),
        {"confirm": "DELETE"},
        content_type="application/json",
    )
    request.user = committee
    response = exam_timetable_delete_view(request, saved_run.pk)
    assert response.status_code == 403
    assert ExamTimetableRun.objects.filter(pk=saved_run.pk).exists()


def test_policy_guards_deny_stale_advisor_scope_before_student_lookup(committee, monkeypatch):
    committee.groups.add(
        Group.objects.get(name=ROLE_ADVISOR), Group.objects.get(name=ROLE_GENERAL_ADVISOR)
    )
    set_user_scope(committee.pk, advisor_id="STALE", departments="AI,CS")
    request = RequestFactory().get("/guard-probe/")
    request.user = committee

    def forbidden(*args, **kwargs):
        pytest.fail("Restricted committee policy attempted a student lookup")

    monkeypatch.setattr(Student.objects, "filter", forbidden)
    for response in (require_student_scope(request, 9001), require_program_scope(request, "AI")):
        assert response.status_code == 403
        assert json.loads(response.content)["reason_code"] == "EXAM_COMMITTEE_EXAM_ONLY"


def test_dev_switch_guard_denies_committee_with_debug_and_real_opt_in(
    committee, settings, monkeypatch
):
    settings.DEBUG = True
    monkeypatch.setenv("ALLOW_DEV_ROLE_SWITCH", "true")
    request = RequestFactory().post(reverse("dev_role_switch"), {"role": ROLE_SUPER_ADMIN})
    request.user = committee
    response = dev_role_switch_view(request)
    assert response.status_code == 403
    assert get_user_role(committee) == COMMITTEE
    assert not committee.groups.filter(name=ROLE_SUPER_ADMIN).exists()


def test_allowlist_matches_full_resolved_name_without_path_or_namespace_bypass(committee_client):
    reached = []

    def probe(request):
        reached.append(request.path)
        return JsonResponse({"ok": True})

    test_urls = ModuleType("committee_security_test_urls")
    test_urls.urlpatterns = [
        path("ops/exam-timetable/unapproved/", probe, name="unapproved_exam_tool"),
        path("prefix-lookalike/", probe, name="exam_timetable_page_extra"),
        path("unnamed/", probe),
        path(
            "namespaced/",
            include(
                ([path("page/", probe, name="exam_timetable_page")], "other"), namespace="other"
            ),
        ),
        path("allowed-with-another-path/", probe, name="exam_timetable_page"),
    ]
    with override_settings(ROOT_URLCONF=test_urls):
        for url in (
            "/ops/exam-timetable/unapproved/",
            "/prefix-lookalike/",
            "/unnamed/",
            "/namespaced/page/",
        ):
            for method in ("get", "post"):
                assert getattr(committee_client, method)(url).status_code == 403
        assert reached == []
        assert committee_client.get("/allowed-with-another-path/").status_code == 200
        assert reached == ["/allowed-with-another-path/"]


def test_two_committee_users_share_history_copy_load_and_exports(
    committee_client, committee, django_user_model, saved_run, tmp_path, monkeypatch
):
    second_user = _user(django_user_model, "committee-second", [COMMITTEE])
    second = Client()
    second.force_login(second_user)
    source_text = saved_run.result_json
    copied = committee_client.post(
        reverse("exam_timetable_copy", args=[saved_run.pk]),
        {"label": "Committee shared copy"},
        content_type="application/json",
    )
    assert copied.status_code == 201
    new_id = copied.json()["run_id"]
    for active in (committee_client, second):
        ids = {row["id"] for row in active.get(reverse("exam_timetable_list")).json()["runs"]}
        assert {saved_run.pk, new_id}.issubset(ids)
        for run_id in (saved_run.pk, new_id):
            loaded = active.get(reverse("exam_timetable_detail", args=[run_id]))
            assert loaded.status_code == 200
            assert (
                loaded.json()["operations_snapshot"]
                == make_department_data()["operations_snapshot"]
            )
            options = active.get(reverse("exam_department_options", args=[run_id]))
            assert options.status_code == 200
            assert {item["id"] for item in options.json()["departments"]} == {"ai-ds", "cs"}
            response = active.post(
                reverse("exam_department_export", args=[run_id]),
                {"departments": ["ai-ds"], "genders": ["M", "F"], "language": "en"},
                content_type="application/json",
            )
            assert response.status_code == 200
            with ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
                assert archive.testzip() is None
            response.close()
    monkeypatch.setattr(exam_timetable, "RUNTIME_DIR", tmp_path)
    master = second.get(reverse("exam_timetable_export", args=[saved_run.pk]))
    assert master.status_code == 200
    with ZipFile(BytesIO(b"".join(master.streaming_content))) as archive:
        assert archive.testzip() is None
    master.close()
    deleted = second.post(
        reverse("exam_timetable_delete", args=[new_id]),
        {"confirm": "DELETE"},
        content_type="application/json",
    )
    assert deleted.status_code == 403
    assert ExamTimetableRun.objects.filter(pk=new_id).exists()
    saved_run.refresh_from_db()
    assert saved_run.result_json == source_text


def test_committee_can_preview_build_check_and_save_a_shared_exam(committee_client):
    student = Student.objects.create(student_id=9001, program="AI", section="F")
    course = Course.objects.create(
        course_code="CS101", description="Committee exam", credit_hours=3
    )
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="CS101",
        course_name="Committee exam",
        programme_term=1,
        credit_hours=3,
    )
    scraped_exam_registration(student, course)
    scope = {"programs": ["AI"], "sections": ["F"]}
    preview = committee_client.post(
        reverse("exam_timetable_preview_courses"), scope, content_type="application/json"
    )
    assert preview.status_code == 200, preview.content
    payload = {
        **scope,
        "label": "Committee build",
        "days": ["Sun"],
        "periods": ["08:00-10:00"],
        "assign_rooms": False,
    }
    built = committee_client.post(
        reverse("exam_timetable_build"), payload, content_type="application/json"
    )
    assert built.status_code == 200, built.content
    result = built.json()
    assert result["schedule"] and result["run_id"]
    loaded = {
        **payload,
        "previous_run_id": result["run_id"],
        "base_schedule": result["schedule"],
        "mode": "save_loaded_changes",
    }
    checked = committee_client.post(
        reverse("exam_timetable_draft_impact"), loaded, content_type="application/json"
    )
    assert checked.status_code == 200, checked.content
    loaded["expected_input_fingerprint"] = checked.json()["input_fingerprint"]
    saved = committee_client.post(
        reverse("exam_timetable_build"), loaded, content_type="application/json"
    )
    assert saved.status_code == 200, saved.content
    assert saved.json()["run_id"] != result["run_id"]


def test_committee_has_own_profile_language_and_logout_only(
    committee_client, committee, django_user_model
):
    other = _user(django_user_model, "unchanged-other-user", [ROLE_ADVISOR])
    original_password = other.password
    renamed = committee_client.post(
        reverse("profile_change_username"),
        {"new_username": "committee-renamed", "user_id": other.pk},
        content_type="application/json",
    )
    assert renamed.status_code == 200
    committee.refresh_from_db()
    other.refresh_from_db()
    assert committee.username == "committee-renamed"
    assert other.username == "unchanged-other-user"
    changed = committee_client.post(
        reverse("profile_change_password"),
        {"current_password": PASSWORD, "new_password": "NewCommittee!99381", "user_id": other.pk},
        content_type="application/json",
    )
    assert changed.status_code == 200
    committee.refresh_from_db()
    other.refresh_from_db()
    assert committee.check_password("NewCommittee!99381")
    assert other.password == original_password
    assert committee_client.get(reverse("profile_me")).json()["user"]["id"] == committee.pk
    assert (
        committee_client.post(
            reverse("set_language"), {"language": "ar", "next": reverse("exam_timetable_page")}
        ).status_code
        == 302
    )
    assert committee_client.post(reverse("logout")).status_code == 302
    assert committee_client.get(reverse("exam_timetable_page")).status_code == 302


def test_committee_mutations_keep_csrf_protection(committee, saved_run, settings):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(committee)
    url = reverse("exam_timetable_copy", args=[saved_run.pk])
    assert strict.post(url, {}, content_type="application/json").status_code == 403
    strict.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32
    assert (
        strict.post(url, {}, content_type="application/json", HTTP_X_CSRFTOKEN="a" * 32).status_code
        == 201
    )


@pytest.mark.parametrize("role", [ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_STUDENT])
def test_existing_non_superadmin_roles_do_not_gain_exam_access(
    client, django_user_model, saved_run, role
):
    user = _user(django_user_model, "existing-role", [role])
    client.force_login(user)
    assert client.get(reverse("exam_timetable_page")).status_code == 403
    assert client.get(reverse("exam_timetable_detail", args=[saved_run.pk])).status_code == 403
    assert (
        client.post(
            reverse("exam_timetable_copy", args=[saved_run.pk]), {}, content_type="application/json"
        ).status_code
        == 403
    )


def test_superadmin_retains_user_management_dashboard_and_delete(
    client, django_user_model, saved_run
):
    admin = _user(django_user_model, "existing-superadmin", [ROLE_SUPER_ADMIN])
    client.force_login(admin)
    assert client.get(reverse("dashboard")).status_code == 200
    assert client.get(reverse("users_list")).status_code == 200
    assert (
        client.post(
            reverse("exam_timetable_delete", args=[saved_run.pk]),
            {"confirm": "DELETE"},
            content_type="application/json",
        ).status_code
        == 200
    )
    assert not ExamTimetableRun.objects.filter(pk=saved_run.pk).exists()


def test_superadmin_can_create_and_assign_committee_without_scopes(client, django_user_model):
    admin = _user(django_user_model, "role-assignment-admin", [ROLE_SUPER_ADMIN])
    client.force_login(admin)
    created = client.post(
        reverse("users_create"),
        {
            "username": "committee-created",
            "password": PASSWORD,
            "role": COMMITTEE,
            "advisor_id": "A987",
            "departments": "AI,CS",
        },
        content_type="application/json",
    )
    assert created.status_code == 200, created.content
    user = django_user_model.objects.get(username="committee-created")
    assert get_user_role(user) == COMMITTEE
    assert not user.is_staff and not user.is_superuser
    assert get_user_scope(user)["advisor_id"] == ""
    assert get_user_scope(user)["departments"] == []
    created_scope = UserScope.objects.get(user_id=user.pk)
    assert created_scope.advisor_id == created_scope.departments == ""
    advisor = _user(django_user_model, "advisor-to-committee", [ROLE_ADVISOR], is_staff=True)
    set_user_scope(advisor.pk, advisor_id="A123", departments="AI,CS")
    assigned = client.post(
        reverse("users_update_role"),
        {
            "username": advisor.username,
            "role": COMMITTEE,
            "advisor_id": "A456",
            "departments": "DS",
        },
        content_type="application/json",
    )
    assert assigned.status_code == 200
    advisor.refresh_from_db()
    assert get_user_role(advisor) == COMMITTEE
    scope = UserScope.objects.get(user_id=advisor.pk)
    assert scope.advisor_id == scope.departments == ""
    assert not advisor.is_staff and not advisor.is_superuser
    listed = next(
        item
        for item in client.get(reverse("users_list")).json()["items"]
        if item["username"] == advisor.username
    )
    assert (
        listed["role"] == COMMITTEE and listed["departments"] == [] and listed["advisor_id"] == ""
    )


def test_committee_assignment_cannot_retain_django_superuser_power(client, django_user_model):
    admin = _user(django_user_model, "assignment-admin", [ROLE_SUPER_ADMIN])
    target = _user(
        django_user_model, "django-superuser-target", [], is_superuser=True, is_staff=True
    )
    client.force_login(admin)
    response = client.post(
        reverse("users_update_role"),
        {"username": target.username, "role": COMMITTEE},
        content_type="application/json",
    )
    assert response.status_code == 409
    target.refresh_from_db()
    assert target.is_superuser and target.is_staff
    assert get_user_role(target) == ROLE_SUPER_ADMIN
    assert not target.groups.filter(name=COMMITTEE).exists()


def test_last_group_superadmin_cannot_be_replaced_by_committee(client, django_user_model):
    admin = _user(django_user_model, "last-group-admin", [ROLE_SUPER_ADMIN])
    client.force_login(admin)
    response = client.post(
        reverse("users_update_role"),
        {"username": admin.username, "role": COMMITTEE},
        content_type="application/json",
    )
    assert response.status_code == 409
    assert get_user_role(admin) == ROLE_SUPER_ADMIN
    assert not admin.groups.filter(name=COMMITTEE).exists()


@pytest.mark.parametrize("role", [COMMITTEE, ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_STUDENT])
def test_only_superadmin_can_create_or_assign_committee(client, django_user_model, role):
    actor = _user(django_user_model, "nonadmin-role-actor", [role])
    target = _user(django_user_model, "role-target", [ROLE_ADVISOR])
    client.force_login(actor)
    create = client.post(
        reverse("users_create"),
        {"username": "forbidden-committee", "password": PASSWORD, "role": COMMITTEE},
        content_type="application/json",
    )
    update = client.post(
        reverse("users_update_role"),
        {"username": target.username, "role": COMMITTEE},
        content_type="application/json",
    )
    assert create.status_code == update.status_code == 403
    assert not django_user_model.objects.filter(username="forbidden-committee").exists()
    assert get_user_role(target) == ROLE_ADVISOR
