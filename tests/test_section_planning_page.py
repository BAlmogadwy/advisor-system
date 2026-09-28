"""The Section Planning page as its view renders it, in English and in Arabic.

The markup the server writes (the scope form, labels, toggles, the status
line) is read with an HTML parser; what the page script draws is covered by
tests/frontend/section-planning.test.cjs, and the layout in Chromium by
tests/test_section_planning_browser.py.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from django.contrib.auth.models import Group, User
from django.test import Client

from core.services.rbac import (
    ROLE_GENERAL_ADVISOR,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)

pytestmark = pytest.mark.django_db


def _page(client: Client, language: str) -> BeautifulSoup:
    ensure_role_groups()
    ensure_scope_schema()
    user, _ = User.objects.get_or_create(username="sp-page")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    set_user_scope(user.id, advisor_id="", departments="")
    client.force_login(user)
    response = client.get("/section-planning/", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    soup = BeautifulSoup(response.content.decode("utf-8"), "html.parser")
    assert soup.html["lang"] == language
    return soup


@pytest.mark.parametrize("language", ["en", "ar"])
def test_scope_fields_are_a_form_whose_only_submit_is_generate(
    client: Client, language: str
) -> None:
    soup = _page(client, language)
    form = soup.find("form", id="spScopeForm")
    assert form is not None
    assert form.has_attr("novalidate"), "the page's own messages, not the browser's bubbles"
    fields = [field["id"] for field in form.find_all("input")]
    assert fields == ["spYear", "spSemester", "spProgram", "spSectionM", "spSectionF"]
    buttons = {button["id"]: button.get("type") for button in form.find_all("button")}
    assert buttons == {"spGenerate": "submit", "spReset": "button"}
    # Enter submits through the form's first submit button, which must be Generate;
    # the seat limits and their Save are never inside it.
    assert form.find(id="spAdvSaveDb") is None
    assert form.find(id="spAdvPanel") is None


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_section_is_a_required_choice_of_one_with_nothing_chosen(
    client: Client, language: str
) -> None:
    """One section per plan: Male (M) or Female (F), native radios, none checked."""
    soup = _page(client, language)
    group = soup.find(id="spSectionGroup")
    assert group.find_parent("form")["id"] == "spScopeForm"
    assert group["role"] == "radiogroup"
    assert group["aria-required"] == "true"
    assert soup.find(id=group["aria-labelledby"]).get_text(strip=True) == (
        "الشطر" if language == "ar" else "Section"
    )
    message = soup.find(id=group["aria-describedby"])
    assert message.get("role") == "alert" and message.get_text(strip=True) == ""
    radios = group.find_all("input")
    assert [(r["type"], r["name"], r["value"]) for r in radios] == [
        ("radio", "spSection", "M"),
        ("radio", "spSection", "F"),
    ]
    assert not any(r.has_attr("checked") for r in radios), "nothing chosen on a first visit"
    assert all(r.has_attr("required") for r in radios)
    labels = [soup.find("label", attrs={"for": r["id"]}).get_text(" ", strip=True) for r in radios]
    assert labels == (
        ["طلاب (M)", "طالبات (F)"] if language == "ar" else ["Male (M)", "Female (F)"]
    )
    # The old promise that both are planned, and summed, is gone from the page.
    text = soup.get_text(" ", strip=True)
    assert "M + F" not in text and "Total sections" not in text and "مجموع الشعب" not in text


@pytest.mark.parametrize("language", ["en", "ar"])
def test_scope_and_filter_fields_are_not_capped_at_the_compact_width(
    client: Client, language: str
) -> None:
    soup = _page(client, language)
    for field_id in ("spYear", "spSemester", "spProgram", "spDeptFilter", "spAdvSearch"):
        classes = soup.find(id=field_id).get("class", [])
        assert "form-control-compact" not in classes, field_id
    assert "sp-fb-year" in soup.find(id="spYear")["class"]
    assert "sp-fb-term" in soup.find(id="spSemester")["class"]
    assert "sp-fb-codes" in soup.find(id="spProgram")["class"]
    # Each scope field has its own label, and the page's rules are scoped to it.
    for field_id in ("spYear", "spSemester", "spProgram", "spDeptFilter"):
        assert soup.find("label", attrs={"for": field_id}) is not None, field_id
    assert soup.find(id="spScopeForm").find_parent(class_="sp-page") is not None
    assert soup.find(id="spResults").find_parent(class_="sp-page") is not None


@pytest.mark.parametrize("language", ["en", "ar"])
def test_toggles_fields_and_status_carry_their_names_states_and_roles(
    client: Client, language: str
) -> None:
    soup = _page(client, language)
    for toggle_id, panel_id in (("spToggleCaps", "spCapsWrap"), ("spToggleAdv", "spAdvPanel")):
        toggle = soup.find(id=toggle_id)
        assert toggle.name == "button" and toggle.get("type") == "button", toggle_id
        assert toggle.get("aria-expanded") == "false", toggle_id
        assert toggle.get("aria-controls") == panel_id, toggle_id
        assert soup.find(id=panel_id) is not None, panel_id
    for field_id in ("spCapLocal4", "spCapLocalOther", "spCapExternal"):
        label = soup.find("label", attrs={"for": field_id})
        assert label is not None and label.get_text(strip=True), field_id
    assert soup.find(id="spAdvSearch").get("aria-label")
    status = soup.find(id="spStatus")
    assert status.get("role") == "status"
    assert status.get("aria-live") == "polite"
    # In the page from the start (empty), so its first message is read out.
    assert "d-none" not in status.get("class", [])
    assert status.get_text(strip=True) == ""


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_department_summary_is_a_labelled_region_the_script_fills(
    client: Client, language: str
) -> None:
    soup = _page(client, language)
    summary = soup.find(id="spDeptSummary")
    region = summary.find_parent("section")
    title = soup.find(id=region["aria-labelledby"])
    assert title.get_text(strip=True) == (
        "ملخص الأقسام" if language == "ar" else "Department Summary"
    )
    assert soup.find(id="spDeptGrid") is None, "the old per-department cards are gone"


@pytest.mark.parametrize("language", ["en", "ar"])
def test_arrows_and_chevrons_follow_the_reading_direction(client: Client, language: str) -> None:
    soup = _page(client, language)
    flow = soup.find(class_="ph-chip-gray").get_text(strip=True)
    forward, backward = ("←", "→") if language == "ar" else ("→", "←")
    assert flow.count(forward) == 2 and backward not in flow, flow
    for toggle_id in ("spToggleCaps", "spToggleAdv"):
        chevron = soup.find(id=toggle_id).find("svg", recursive=False)
        assert chevron is not None and "sp-chev" in chevron["class"], toggle_id
        assert chevron.get("aria-hidden") == "true", toggle_id
