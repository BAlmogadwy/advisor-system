import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from pytest import MonkeyPatch

from core.services.rbac import ROLE_GENERAL_ADVISOR, ensure_role_groups

pytestmark = pytest.mark.django_db

client = Client()


def _login_general(departments: str = "AI") -> None:
    """A general adviser WITH departments, which is what a real one has.

    This used to leave the scope row unset. That mattered once the roster views
    started failing closed: `core/services/policy.py` has always denied a general
    adviser with no departments (`if not allowed or ... -> 403`), while these two
    views read the empty list as "no filter" and served everything. The fixture was
    quietly exercising the inconsistent half.
    """
    from core.services.rbac import set_user_scope

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-general")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, departments=departments)
    client.force_login(user)


def test_a_general_adviser_with_no_departments_cannot_export() -> None:
    """Scoped to no departments is scoped to nothing, not to everything.

    Matches `require_student_scope`, which has always denied this case.
    """
    _login_general(departments="")
    response = client.get("/export/students-by-advisor.csv?advisor_id=A001")
    assert response.status_code == 403, response.content[:200]


def test_export_students_by_advisor_csv(monkeypatch: MonkeyPatch) -> None:
    _login_general()
    seen: dict[str, object] = {}

    def fake_list(advisor_id: str, **kwargs: object) -> dict[str, object]:
        seen["advisor_id"] = advisor_id
        seen.update(kwargs)
        return {
            "advisor_id": advisor_id,
            "mapping_ready": True,
            "items": [
                {
                    "student_id": 4410001,
                    "registration_no": "R-001",
                    "name": "Student One",
                    "program": "AI",
                    "section": "M",
                    "status": "active",
                    "gpa": 3.42,
                    "total_earned_credits": 64,
                    "total_registered_credits": 70,
                    "current_term_registered_hours": 15,
                    "has_high_priority_missing": True,
                    "needs_attention": True,
                    "risk_score": 11.3,
                    "attention_reasons": ["low_gpa", "high_priority_missing"],
                    "missing_courses_compact": "CS211(4.00); AI201(2.50)",
                }
            ],
        }

    monkeypatch.setattr("core.report_views.list_students_by_advisor", fake_list)

    response = client.get(
        "/export/students-by-advisor.csv?advisor_id=A001&search=44&focus=risk&program_filter=AI"
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert (
        "advisor_id,student_id,registration_no,name,program,section,status,gpa,total_earned_credits,total_registered_credits,current_term_registered_hours,has_high_priority_missing,needs_attention,risk_score,attention_reasons,missing_courses_compact"
        in text
    )
    assert (
        "A001,4410001,R-001,Student One,AI,M,active,3.42,64,70,15,True,True,11.3,low_gpa,high_priority_missing,CS211(4.00); AI201(2.50)"
        in text.replace('"', "")
    )

    assert seen["advisor_id"] == "A001"
    assert seen["search"] == "44"
    assert seen["focus"] == "risk"
    assert seen["program_filter"] == "AI"
