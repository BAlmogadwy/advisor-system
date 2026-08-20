"""Regression contract for the student timetable presentation.

The dashboard and proposal planner show the same kind of academic evidence.  A
clock calendar on one page and an unrelated matrix on the other made identical
meetings look different, while the planner's desktop-only grid became unusable
on a phone.  These tests protect the shared presentation boundary without
asserting its colours or pixel layout.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services import student_otp
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
HOME_TEMPLATE = ROOT / "core/templates/core/student_home.html"
PLANNER_TEMPLATE = ROOT / "core/templates/core/student_planner.html"
BASE_TEMPLATE = ROOT / "core/templates/core/base.html"
HOME_PAGE_JS = ROOT / "static/js/page-student-home.js"
PLANNER_PAGE_JS = ROOT / "static/js/page-student-planner.js"
STUDENT_TIMETABLE_JS = ROOT / "static/js/student-timetable.js"
WEEK_GRID_JS = ROOT / "static/js/shared-timetable.js"
GLOBAL_CSS = ROOT / "static/css/global.css"

SID = 4660311
PROGRAM = "TTUNIFY"
YEAR = "1448"
TERM = "1"
MEETINGS = (
    ("UT101", "M1", "SUN", "09:05", "10:20"),
    # Deliberately overlaps UT101 and therefore must not replace it visually.
    ("UT102", "M2", "SUN", "09:35", "10:05"),
    # The complete Saudi week contract includes the weekend days too.
    ("UT103", "M3", "SAT", "13:10", "14:25"),
)


@pytest.fixture
def timetable_student(monkeypatch):
    ensure_role_groups()
    Student.objects.create(
        student_id=SID,
        name="Timetable Contract",
        program=PROGRAM,
        section="M",
        gpa=3.4,
    )
    for code, section, day, start, end in MEETINGS:
        Course.objects.create(
            course_code=code,
            description=f"{code} exact meeting",
            credit_hours=3,
        )
        ProgrammeRequirement.objects.create(
            program=PROGRAM,
            course_code=code,
            course_name=f"{code} exact meeting",
            type="Mandatory",
            programme_term=1,
            credit_hours=3,
        )
        term_section = TermSection.objects.create(
            course_code=code[:2],
            course_number=code[2:],
            course_key=code,
            course_name=f"{code} exact meeting",
            section=section,
        )
        TermSectionMeeting.objects.create(
            term_section=term_section,
            day=day,
            start_time=start,
            end_time=end,
            room="B-101",
        )
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            term_section=term_section,
            source="scraper_timetable",
        )

    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {"academic_year": int(YEAR), "term": int(TERM)},
    )
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    return client


def _home(client: Client, language: str = "en") -> str:
    response = client.get(
        reverse("student_home"),
        headers={"accept-language": language},
        SERVER_NAME="testserver",
    )
    assert response.status_code == 200
    return response.content.decode()


def _json_script(body: str, element_id: str) -> Any:
    match = re.search(
        rf'<script\b(?=[^>]*\bid="{re.escape(element_id)}")'
        rf'(?=[^>]*\btype="application/json")[^>]*>(.*?)</script>',
        body,
        flags=re.DOTALL,
    )
    assert match, f"missing JSON script #{element_id}"
    return json.loads(match.group(1))


def _meeting_tuples(value: Any) -> set[tuple[str, str, str, str]]:
    """Read either API-style or home-view-style meeting field names."""
    found: set[tuple[str, str, str, str]] = set()
    if isinstance(value, dict):
        code = value.get("course_code") or value.get("code")
        day = value.get("day") or value.get("day_code")
        start = value.get("start") or value.get("start_time")
        end = value.get("end") or value.get("end_time")
        if all((code, day, start, end)):
            found.add((str(code), str(day), str(start), str(end)))
        for child in value.values():
            found.update(_meeting_tuples(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_meeting_tuples(child))
    return found


def _plain(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def test_home_has_shared_visual_data_and_an_exact_semantic_fallback(timetable_student):
    body = _home(timetable_student)

    assert re.search(r'<[^>]+\bid="studentHomeTimetable-registered"[^>]*>', body)
    payload = _json_script(body, "studentHomeTimetableData-registered")
    expected = {(code, day, start, end) for code, _section, day, start, end in MEETINGS}
    assert expected <= _meeting_tuples(payload)

    # The enhanced calendar is progressive enhancement.  With JavaScript absent,
    # assistive technology still receives one exact row per recorded meeting.
    tables = re.findall(r"<table\b.*?</table>", body, flags=re.DOTALL | re.IGNORECASE)
    exact_table = None
    for table in tables:
        rows = re.findall(r"<tr\b.*?</tr>", table, flags=re.DOTALL | re.IGNORECASE)
        if all(
            any(code in _plain(row) and start in _plain(row) and end in _plain(row) for row in rows)
            for code, _section, _day, start, end in MEETINGS
        ):
            exact_table = table
            break
    assert exact_table is not None, "no semantic table preserves every exact meeting interval"
    assert "<caption" in exact_table.lower()
    assert re.search(r"<th\b[^>]*\bscope=", exact_table, flags=re.IGNORECASE)


def test_home_and_planner_both_use_the_shared_student_timetable_adapter():
    base = BASE_TEMPLATE.read_text(encoding="utf-8")
    home_template = HOME_TEMPLATE.read_text(encoding="utf-8")
    planner_template = PLANNER_TEMPLATE.read_text(encoding="utf-8")
    home_page = HOME_PAGE_JS.read_text(encoding="utf-8")
    planner_page = PLANNER_PAGE_JS.read_text(encoding="utf-8")
    adapter = STUDENT_TIMETABLE_JS.read_text(encoding="utf-8")

    assert "js/student-timetable.js" in base
    assert "js/page-student-home.js" in home_template
    assert "js/page-student-planner.js" in planner_template
    assert "StudentTimetable.render" in home_page
    assert "StudentTimetable.render" in planner_page
    assert re.search(r"(?:window|global)\.StudentTimetable\s*=", adapter)
    assert "render" in adapter


def test_arabic_direction_is_explicit_on_visual_and_semantic_timetables(timetable_student):
    body = _home(timetable_student, language="ar")
    planner = PLANNER_PAGE_JS.read_text(encoding="utf-8")

    host = re.search(r'<[^>]+\bid="studentHomeTimetable-registered"[^>]*>', body)
    assert host and re.search(r'\bdir="rtl"', host.group(0))

    tables = re.findall(r"<table\b.*?</table>", body, flags=re.DOTALL | re.IGNORECASE)
    timetable_tables = [
        table for table in tables if all(code in table for code in ("UT101", "UT103"))
    ]
    assert timetable_tables
    assert any(re.search(r'^<table\b[^>]*\bdir="rtl"', table) for table in timetable_tables)
    assert re.search(r"\bdir\s*:\s*AR\s*\?\s*['\"]rtl['\"]", planner)


def test_shared_adapter_supplies_a_mobile_agenda_to_the_planner():
    adapter = STUDENT_TIMETABLE_JS.read_text(encoding="utf-8")
    planner = PLANNER_PAGE_JS.read_text(encoding="utf-8")
    css = GLOBAL_CSS.read_text(encoding="utf-8")

    assert "StudentTimetable.render" in planner
    assert "student-timetable-calendar" in adapter
    assert "student-timetable-agenda" in adapter
    assert re.search(
        r"@media\s*\(max-width:[^)]*\).*?\.student-timetable-calendar\s*\{[^}]*display\s*:\s*none",
        css,
        flags=re.DOTALL,
    )
    assert re.search(
        r"@media\s*\(max-width:[^)]*\).*?\.student-timetable-agenda\s*\{[^}]*display\s*:\s*(?:grid|block|flex)",
        css,
        flags=re.DOTALL,
    )


def test_planner_does_not_disable_legitimate_snapshot_coexistence():
    planner = PLANNER_PAGE_JS.read_text(encoding="utf-8")

    assert "mixedBaseline" not in planner
    assert "MIXED_REVIEW_REQUIRED" not in planner
    assert "Building is paused" not in planner


def test_planner_arabic_copy_calls_registration_evidence_what_it_is():
    planner = PLANNER_PAGE_JS.read_text(encoding="utf-8")

    for phrase in (
        "نسخ قائمة المقررات والشُعب",
        "نوع الجدول",
        "متطلباته السابقة مستوفاة",
        "المقررات المدرجة",
        "أيام الحضور",
        "المقررات التي تعذّر إدراجها",
        "تعذّر إنشاء جدول مقترح يضم جميع المقررات المحدّدة.",
        "قائمة مرجعية للتحقق منها وإدخالها يدويًا في بوابة الجامعة (نسخها لا يسجّل أي مقرر):",
        "الجدول المسجّل فعليًا",
        "لا تتوفر في بياناتنا مواعيد للجدول المسجّل فعليًا في هذا الفصل.",
        "عرض مواعيد الجدول المسجّل فعليًا",
        "الاحتفاظ بشُعب الجدول المسجّل فعليًا",
        "الجدول المتوقع",
        "لا تتوفر في بياناتنا مواعيد للجدول المتوقع في هذا الفصل.",
        "عرض مواعيد الجدول المتوقع",
        "الاحتفاظ بشُعب الجدول المتوقع",
        "الجدول المقترح",
    ):
        assert phrase in planner

    for literal_translation in (
        "المصدر",
        "المقررات المجدولة",
        "جدولك الحالي",
        "شُعبي الحالية",
        "تسجيلك الحقيقي",
        "نسخ قائمة التسجيل",
        "خيارات الجدول",
        "ابنِ الخيارات",
        "أي شعبة مناسبة",
        "احتفظ بشُعبي المسجّلة",
    ):
        assert literal_translation not in planner


def test_shared_sources_keep_the_complete_week_overlaps_and_exact_minutes():
    engine = WEEK_GRID_JS.read_text(encoding="utf-8")
    adapter = STUDENT_TIMETABLE_JS.read_text(encoding="utf-8")

    day_order = re.search(r"DAY_ORDER\s*=\s*\[([^\]]+)]", engine)
    assert day_order
    declared_days = re.findall(r"['\"]([A-Z]{3})['\"]", day_order.group(1))
    assert declared_days == ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]

    # These are public semantic markers rather than CSS coordinates.  They prove
    # that every collision remains independently addressable and that the exact
    # endpoints survive the geometry pass, whatever layout technique is used.
    assert "deriveDays" in engine
    assert 'data-start-minute="' in engine
    assert 'data-end-minute="' in engine
    assert 'data-lane="' in engine
    assert 'data-lane-count="' in engine
    assert "overlap" in engine.lower()

    # The student adapter must print the source interval itself in both the
    # enhanced calendar and agenda; tick labels are never a substitute for it.
    assert "meeting.start" in adapter
    assert "meeting.end" in adapter
    assert "<time" in adapter
