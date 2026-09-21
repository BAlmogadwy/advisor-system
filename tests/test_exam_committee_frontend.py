"""Rendered navigation and user-administration interactions for exam access."""

import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from django.utils import translation

from core import sidebar_context

ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))

    def attr(self, tag, attr):
        return [attrs[attr] for name, attrs in self.items if name == tag and attr in attrs]


@pytest.mark.parametrize("language", ["en", "ar"])
def test_committee_sidebar_has_only_exams_and_account_controls(language):
    with translation.override(language):
        html = render_to_string(
            "core/partials/sidebar.html",
            {
                "role": "EXAM_COMMITTEE",
                "user": SimpleNamespace(is_authenticated=True, username="exam-member"),
                # Even an over-broad caller cannot expose unrelated navigation.
                "can_exam_timetable": True,
                "can_db_admin": True,
                "can_admin_advisors": True,
                "can_view_portfolio": True,
                "can_graduation_planning": True,
                "can_section_planning": True,
            },
        )
    elements = Elements(html)
    assert elements.attr("a", "href") == ["/exam-timetable/", "/profile/"]
    assert elements.attr("form", "action") == ["/logout/", "/i18n/setlang/"]
    assert "themeToggle" in elements.attr("button", "id")
    assert ("لجنة الاختبارات" if language == "ar" else "Exam Committee") in html
    assert "EXAM_COMMITTEE" not in html


@pytest.mark.parametrize("language", ["en", "ar"])
def test_committee_profile_labels_are_localized(language):
    with translation.override(language):
        html = render_to_string(
            "core/profile.html",
            {
                "role": "EXAM_COMMITTEE",
                "user": SimpleNamespace(is_authenticated=True, username="exam-member"),
            },
        )
    label = "لجنة الاختبارات" if language == "ar" else "Exam Committee"
    header_role = html.split('class="ph-chip ph-chip-role">', 1)[1].split("</span>", 1)[0].strip()
    profile_role = html.split('id="pfRoleBadge">', 1)[1].split("</span>", 1)[0].strip()
    assert header_role == label
    assert profile_role == label
    assert Elements(html).attr("a", "href").count("/exam-timetable/") == 1


@pytest.mark.parametrize(
    ("role", "can_exam", "can_delete"),
    [
        ("EXAM_COMMITTEE", True, False),
        ("SUPER_ADMIN", True, True),
        ("GENERAL_ACADEMIC_ADVISOR", False, False),
        ("ADVISOR", False, False),
        ("STUDENT", False, False),
    ],
)
def test_sidebar_permission_context_keeps_exam_deletion_admin_only(
    monkeypatch, role, can_exam, can_delete
):
    monkeypatch.setattr(sidebar_context, "ensure_role_groups", lambda: None)
    monkeypatch.setattr(sidebar_context, "ensure_scope_schema", lambda: None)
    monkeypatch.setattr(sidebar_context, "get_user_scope", lambda user: {"role": role})
    context = sidebar_context.get_sidebar_context(SimpleNamespace(user=object()))
    assert context["can_exam_timetable"] is can_exam
    assert context["can_delete_exam_timetable"] is can_delete
    if role == "EXAM_COMMITTEE":
        assert not any(
            context[key]
            for key in (
                "can_admin_advisors",
                "can_view_portfolio",
                "can_graduation_planning",
                "can_db_admin",
                "can_section_planning",
            )
        )


@pytest.mark.parametrize("capability", [None, False, True])
def test_exam_template_passes_explicit_delete_capability_with_default_deny(capability):
    context = {"role": "SUPER_ADMIN"}
    if capability is not None:
        context["can_delete_exam_timetable"] = capability
    html = render_to_string("core/exam_timetable.html", context)
    main = next(attrs for tag, attrs in Elements(html).items if tag == "main")
    assert main["data-can-delete-exam-timetable"] == ("true" if capability else "false")


@pytest.mark.parametrize("language", ["en", "ar"])
def test_user_management_frontend_interactions(tmp_path, language):
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules/jsdom/package.json").is_file():
        pytest.skip("User management DOM tests require Node.js and npm ci dependencies")
    with translation.override(language):
        html = render_to_string(
            "core/user_management.html", {"LANGUAGE_CODE": language, "role": "SUPER_ADMIN"}
        )
    fixture = tmp_path / f"users-{language}.html"
    fixture.write_text(html, encoding="utf-8")
    result = subprocess.run(
        [node, "--test", "tests/frontend/user-management.test.cjs"],
        cwd=ROOT,
        env={**os.environ, "USER_TEST_HTML": str(fixture), "USER_TEST_LANGUAGE": language},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
