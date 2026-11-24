"""The Virtual Advisor page and its endpoints speak Arabic in Arabic mode.

English mode must stay exactly as it was; Arabic mode must show no English UI
label. The endpoints keep their English ``error`` and, in Arabic mode only, add
``error_display`` (and ``error_detail``, a Latin technical name) for the page.
"""

import json
import re
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services import llm_backend
from core.services import virtual_advisor_copy as copy
from core.services.rbac import (
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "core" / "virtual_advisor_views.py"

#: Every English UI label of the page body (markup only; data is never here).
ENGLISH_LABELS = (
    "Local LLM",
    "Scoped student data",
    "A private academic advisor powered by the local model",
    "Local Model",
    "Runtime",
    ">Check<",
    "Checking local server...",
    "Endpoint",
    "Auto from LM Studio",
    ">Model<",
    "Auto loaded model",
    "Verified Context",
    ">Student<",
    "Student ID",
    ">Year<",
    ">Term<",
    "Optional",
    "Add a student ID when you want",
    "Verified Tools",
    "Result Evidence",
    "Ask a dataset question",
    ">Conversation<",
    "Ask the Local Advisor",
    ">Clear<",
    "I am connected to the local advisor layer",
    ">Question<",
    "Ask about graduation readiness",
    ">Ask<",
    ">Prompts<",
    "Fast Tests",
    "Graduation risk",
    "Next courses",
    "Prerequisite gaps",
    "Explain this student",
    "Which courses should this student",
    "Summarize the student",
    "Virtual advisor workspace",
    "Advisor context",
    "Local model",
    "Verified advisor results",
    "Advisor conversation",
    "Fast advisor prompts",
)


def _login(client: Client, *, language: str) -> None:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username=f"va-i18n-{language}")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    client.cookies["advisor_language"] = language


def _body(html: str) -> str:
    """The page's own markup: from the workspace to the page config script."""
    start = html.index('<section class="va-shell"')
    end = html.index("window.virtualAdvisorConfig")
    return html[start:end]


def _page(client: Client, language: str) -> str:
    _login(client, language=language)
    response = client.get("/virtual-advisor/")
    assert response.status_code == 200
    return response.content.decode("utf-8")


def test_arabic_page_has_arabic_title_and_headings_and_no_english_label(client: Client) -> None:
    html = _page(client, "ar")
    assert re.search(r"<title>[^<]*المرشد الأكاديمي الافتراضي", html)
    for arabic in (
        "بيئة التشغيل",
        "أدلة النتائج",
        "طرح سؤال على المرشد المحلي",
        "اختبارات سريعة",
        "الرقم الجامعي",
        'aria-label="مساحة عمل المرشد الافتراضي"',
        'placeholder="اختياري"',
    ):
        assert arabic in html, arabic
    body = _body(html)
    leaked = [label for label in ENGLISH_LABELS if label in body]
    assert leaked == []
    # The dataset example stays English on purpose (the tool planner reads English
    # only), isolated left-to-right inside the Arabic sentence.
    assert '<bdi dir="ltr">“Find students with 90+ hours and CS323 passed.”</bdi>' in body
    # A prompt button puts its question in the box: Arabic in Arabic mode.
    assert 'data-va-example="ما مخاطر تخرج هذا الطالب؟' in body


def test_english_page_is_unchanged(client: Client) -> None:
    html = _page(client, "en")
    assert re.search(r"<title>[^<]*Virtual Advisor", html)
    body = _body(html)
    for english in (
        "<h2>Runtime</h2>",
        "<span>Checking local server...</span>",
        "<h2>Ask the Local Advisor</h2>",
        'id="vaSend">Ask</button>',
        'placeholder="Optional"',
        'aria-label="Virtual advisor workspace"',
        "data-va-example=\"Explain this student's graduation risk using only verified "
        'credits and missing requirements.">Graduation risk</button>',
        "Ask a dataset question, for example: “Find students with 90+ hours and CS323 passed.”",
    ):
        assert english in html, english
    assert not re.search(r"[؀-ۿ]", body)


@pytest.mark.parametrize("language", ["ar", "en"])
def test_health_error_has_arabic_only_in_arabic_mode(
    client: Client, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    _login(client, language=language)
    health = {
        "ok": False,
        "error_category": "LLMUnavailable",
        "error": "local is not reachable (ConnectionRefusedError).",
        "models": [],
    }
    monkeypatch.setattr("core.virtual_advisor_views.check_local_llm_health", lambda: dict(health))
    data = client.get("/ops/virtual-advisor/health/").json()
    assert data["error"] == health["error"]
    if language == "ar":
        assert data["error_display"] == "تعذّر الوصول إلى خادم النموذج المحلي"
        assert data["error_detail"] == "ConnectionRefusedError"
    else:
        assert "error_display" not in data
        assert "error_detail" not in data


@pytest.mark.parametrize("language", ["ar", "en"])
def test_chat_errors_have_arabic_only_in_arabic_mode(
    client: Client, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    _login(client, language=language)
    url = "/ops/virtual-advisor/chat/"

    empty = client.post(url, data=json.dumps({"message": " "}), content_type="application/json")
    assert empty.status_code == 400
    assert empty.json()["error"] == "message is required"
    if language == "ar":
        assert empty.json()["error_display"] == "يُرجى كتابة السؤال."
    else:
        assert "error_display" not in empty.json()

    def unreachable(**_kwargs: object) -> dict:
        raise llm_backend.LLMUnavailable("local is not reachable (ConnectionRefusedError).")

    monkeypatch.setattr("core.virtual_advisor_views.answer_virtual_advisor", unreachable)
    down = client.post(url, data=json.dumps({"message": "hello"}), content_type="application/json")
    assert down.status_code == 503
    payload = down.json()
    assert payload["error"] == "local is not reachable (ConnectionRefusedError)."
    assert payload["source"] == "local_llm"
    if language == "ar":
        assert payload["error_display"] == "تعذّر الوصول إلى خادم النموذج المحلي"
        assert payload["error_detail"] == "ConnectionRefusedError"
    else:
        assert "error_display" not in payload
        assert "error_detail" not in payload


def test_every_message_the_views_write_has_arabic() -> None:
    source = VIEWS.read_text(encoding="utf-8")
    messages = set(re.findall(r'"error": "([^"]+)"', source))
    messages |= {f"{field} must be an integer" for field in ("student_id", "academic_year", "term")}
    assert messages
    assert sorted(m for m in messages if copy.view_error_display(m) is None) == []


def test_every_llm_error_class_has_arabic() -> None:
    def walk(cls: type) -> list[type]:
        return [cls, *(sub for child in cls.__subclasses__() for sub in walk(child))]

    # Every class in llm_backend has its own sentence; a subclass defined elsewhere
    # (e.g. llm_remote_privacy.UnverifiedIdentity) inherits its parent's.
    backend_names = {
        cls.__name__ for cls in walk(llm_backend.LLMError) if cls.__module__ == llm_backend.__name__
    }
    assert sorted(name for name in backend_names if name not in copy.LLM_ERRORS) == []
    mro = ["UnverifiedIdentity", *(c.__name__ for c in llm_backend.LLMPrivacyError.__mro__)]
    assert copy.llm_error_fields(mro, "x")["error_display"] == copy.LLM_ERRORS["LLMPrivacyError"]
    # A reason with no named cause falls back to the class name as the detail.
    assert copy.llm_error_fields(["LLMTimeout"], "local request timed out.") == {
        "error_display": copy.LLM_ERRORS["LLMTimeout"],
        "error_detail": "LLMTimeout",
    }
