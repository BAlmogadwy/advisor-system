"""The rendered student home screen: what it may say about a student.

Asserted through the HTML, because every defect this replaces was invisible at the
service layer. `eligible_now` was a correct number computed by a correct function
and rendered under a heading — «متاحة للتسجيل هذا الفصل» — that prerequisites
cannot support. Nothing but reading the page catches that.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services import student_otp
from core.services.rbac import ensure_role_groups

pytestmark = pytest.mark.django_db

SID = 4960001
PROG = "SHM"


@pytest.fixture
def student():
    """A -> B, A -> C, D needing A and E, one placeholder, one passed course."""
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={"name": "Home", "program": PROG, "section": "M", "gpa": 4.01},
    )
    for code in ("SA101", "SB201", "SC201", "SD301", "SE101"):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": code, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3, "type": "Mandatory"},
        )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="SP1",
        defaults={"programme_term": 7, "credit_hours": 3, "type": "Program Elective"},
    )
    for course, prereq in (
        ("SB201", "SA101"),
        ("SC201", "SA101"),
        ("SD301", "SA101"),
        ("SD301", "SE101"),
    ):
        Prerequisite.objects.update_or_create(
            program=PROG, course_code=course, prerequisite_course_code=prereq
        )
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="SE101"),
        defaults={"status": "passed", "programme_term": 1},
    )
    yield


def _page(gpa=None, language="ar"):
    if gpa is not None:
        Student.objects.filter(student_id=SID).update(gpa=gpa)
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    response = client.get(
        reverse("student_home"), headers={"accept-language": language}, SERVER_NAME="testserver"
    )
    assert response.status_code == 200, response.status_code
    return response.content.decode()


# ── GPA and its band ─────────────────────────────────────────────


def test_a_classified_gpa_shows_its_approved_band(student):
    body = _page(gpa=4.01)
    assert "4.01" in body
    assert "جيد جداً" in body
    assert "28" in body, "the page cites no page number for the band table"


@pytest.mark.parametrize(
    ("gpa", "band"), [(5.0, "ممتاز"), (4.5, "ممتاز"), (4.49, "جيد جداً"), (2.0, "مقبول")]
)
def test_boundary_values_render_the_higher_band(student, gpa, band):
    assert band in _page(gpa=gpa)


def test_a_gpa_below_the_table_shows_the_value_and_no_warning(student):
    """The number is theirs; the silence is the table's. Nothing on this screen may
    imply the academic-warning regime — `TU.DISMISSAL.THREE_WARNINGS` carries
    `never_infer`, and the count it needs does not exist in the schema."""
    body = _page(gpa=1.75)
    assert "1.75" in body
    assert "لا ينطبق تقدير عام على هذا المعدل في الجدول المعتمد." in body
    for band in ("ممتاز", "جيد جداً", "مقبول"):
        assert band not in body, f"invented a band: {band}"
    for warning in ("إنذار", "تحذير", "خطر", "الفصل من الجامعة", "متعثر"):
        assert warning not in body, f"warning language reached the student: {warning}"


# ── the unlock card ──────────────────────────────────────────────


def test_the_unlock_card_counts_only_what_this_course_alone_blocks(student):
    """SD301 needs SA101 AND SE101. SE101 is passed, so SA101 frees all three."""
    body = _page()
    assert "SA101" in body
    assert "3" in body

    StudentCourse.objects.filter(student_id=SID, course__course_code="SE101").update(
        status="not_taken"
    )
    body = _page()
    # SD301 now waits on SE101 too, so SA101 frees two, not three.
    assert "يتيح لك مباشرةً 2" in body or "immediately frees 2" in body, (
        "the count still promises a course that stays blocked"
    )


# ── the plan state ───────────────────────────────────────────────


def test_the_states_are_mutually_exclusive_and_carry_no_denominator(student):
    body = _page()
    assert "مستوفية للمتطلبات" in body
    assert "يفصلها متطلب واحد" in body
    assert "تحتاج أكثر من متطلب" in body
    assert "متطلبات اختيارية لم يُحدّد مقررها" in body
    # NO official denominator anywhere.
    assert "من أصل" not in body
    assert "المتطلبات المصنفة في بيانات خطتك الحالية" in body


def test_placeholders_are_never_called_courses(student):
    body = _page()
    slot_line = body[body.index("متطلبات اختيارية لم يُحدّد مقررها") :][:200]
    for wrong in ("محجوب", "متاح", "مجتاز"):
        assert wrong not in slot_line, f"a placeholder was labelled {wrong}"


# ── the claims that must not be produced ─────────────────────────


def test_no_registration_permission_is_claimed_from_prerequisites(student):
    """`eligible_now` was a correct number under a heading the data cannot support.
    Prerequisites establish neither offering, nor publication, nor a seat."""
    body = _page()
    for claim in ("متاحة للتسجيل", "تستطيع تسجيل", "يمكنك التسجيل", "Available to register"):
        assert claim not in body, claim


def test_no_term_is_claimed_for_the_recommendations(student):
    body = _page()
    assert "المقررات المقترحة للفصل القادم" not in body
    assert "المقررات المقترحة لك" in body
    assert "بحسب بيانات الخطة الحالية" in body


def test_no_adviser_triage_signal_reaches_the_html(student):
    body = _page()
    for internal in ("risk_score", "needs_attention", "attention_reasons", "high_priority_missing"):
        assert internal not in body, internal
    for wording in ("تحتاج إلى انتباه", "خطورة", "needs attention"):
        assert wording not in body, wording


def test_no_raw_template_syntax_reaches_the_student(student):
    """Django's `{# … #}` is SINGLE-LINE. A multi-line one renders as visible text.

    This screen produced exactly that while being written — six of them, and two
    leaked the very phrases the comments were explaining the removal of. It has
    happened on the adviser screen, the planner and the course detail; it is a
    test here rather than a habit.
    """
    body = _page()
    for marker in ("{#", "#}", "{%", "%}", "{{", "}}"):
        assert marker not in body, marker


def test_the_academic_summary_renders_each_fact_once(student):
    """The raw Student row and the evidence-backed home cards used to render two
    GPAs and two contradictory-looking registered-credit figures."""
    body = _page()
    assert body.count('class="k">المعدل التراكمي') == 1
    assert body.count('class="k">ساعات هذا الفصل') == 1
    assert 'class="k">الساعات المسجّلة' not in body
    assert 'class="k">الحالة' not in body


# ── degradation ──────────────────────────────────────────────────


def test_the_page_survives_the_card_service_failing(student, monkeypatch):
    """A student's only page must degrade, never 500."""
    monkeypatch.setattr(
        "core.student_auth_views.build_student_home_cards",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    response = client.get(reverse("student_home"), SERVER_NAME="testserver")
    assert response.status_code == 200
    assert response.context["home_cards"] is None


# ── the card reads THE CONFIGURED TERM ───────────────────────────
#
# `virtual_advisor._current_term_registrations` reports the student's LATEST
# stored term by design — its docstring says "the chat's configured term is the
# term being planned FOR and may differ from the term being studied". Calling it
# from a tile labelled «الفصل المحدد في النظام» showed one term's hours under
# another term's label. The rendered tests did not catch it because every student
# I checked happened to have their latest term equal to the configured one.


def _register(student_id, code, year, term, credits=3, section="M1", meetings=3):
    """One TermSection with several meetings, so the credit sum cannot be a row count."""
    from core.models import (
        Course,
        ProgrammeRequirement,
        StudentTermSection,
        TermSection,
        TermSectionMeeting,
    )

    # The baseline reads credits from ProgrammeRequirement for the student's
    # programme, keyed on course code — TermSection carries no credit column.
    Course.objects.update_or_create(
        course_code=code, defaults={"description": code, "credit_hours": credits}
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code=code,
        defaults={"programme_term": 5, "credit_hours": credits, "type": "Mandatory"},
    )
    ts = TermSection.objects.create(
        course_code=code[:2],
        course_number=code[2:],
        course_key=code,
        course_name=code,
        section=section,
    )
    for i in range(meetings):
        TermSectionMeeting.objects.create(
            term_section=ts,
            day=["SUN", "MON", "WED"][i % 3],
            start_time=f"{9 + i:02d}:00",
            end_time=f"{10 + i:02d}:00",
        )
    StudentTermSection.objects.create(
        student_id=student_id, academic_year=str(year), term=str(term), term_section=ts
    )
    return ts


def test_hours_from_an_earlier_term_are_not_shown_as_the_configured_term(student):
    """The exact mismatch. 8 hours stored in 1447/2, nothing in 1448/1.

    The private adviser helper returns the 1447/2 figure because that is the
    latest term it can find. The dashboard must not show it under a label that
    says «the term configured in the system».
    """
    from core.services.student_home_cards import build_student_home_cards

    _register(SID, "SX401", 1447, 2, credits=5)
    _register(SID, "SX402", 1447, 2, credits=3)

    card = build_student_home_cards(SID, 1448, 1)["registered_hours"]
    assert card["value"] != 8, "an earlier term's hours are being shown as the configured term"
    assert card["known"] is False
    assert card["academic_year"] == "1448" and card["term"] == "1"


def test_the_configured_term_wins_when_both_terms_have_rows(student):
    """8 hours in 1447/2 and 3 in 1448/1 — the card shows 3."""
    from core.services.student_home_cards import build_student_home_cards

    _register(SID, "SX401", 1447, 2, credits=5)
    _register(SID, "SX402", 1447, 2, credits=3)
    _register(SID, "SX403", 1448, 1, credits=3)

    card = build_student_home_cards(SID, 1448, 1)["registered_hours"]
    assert card["value"] == 3, card
    assert card["course_count"] == 1
    assert card["known"] is True


def test_credits_are_counted_once_per_course_not_once_per_meeting(student):
    """The baseline is one row per MEETING. Summing rows multiplies a 3-hour
    course by its three weekly sessions."""
    from core.services.student_home_cards import build_student_home_cards

    _register(SID, "SX404", 1448, 1, credits=4, meetings=3)
    card = build_student_home_cards(SID, 1448, 1)["registered_hours"]
    assert card["value"] == 4, f"counted meetings, not courses: {card}"


def test_no_rows_for_the_configured_term_is_unknown_not_zero(student):
    """An empty baseline can mean registered nothing, registration not open, the
    import not run, or incomplete section mappings. There is no completeness
    marker to tell them apart, so «—» is true in all four and 0 in only one."""
    from core.services.student_home_cards import build_student_home_cards

    card = build_student_home_cards(SID, 1448, 1)["registered_hours"]
    assert card["known"] is False
    assert card["value"] is None
    assert card["source"] == "no_term_registration_evidence"

    body = _page()
    hours_block = body[body.index("ساعات هذا الفصل") :][:400]
    assert "—" in hours_block, "an unknown figure was rendered as a number"


def test_changing_the_configured_term_changes_what_is_queried(student):
    """Guards the wiring itself: if the arguments were ignored again, both calls
    would return the same figure."""
    from core.services.student_home_cards import build_student_home_cards

    _register(SID, "SX405", 1447, 2, credits=5)
    _register(SID, "SX406", 1448, 1, credits=2)

    assert build_student_home_cards(SID, 1448, 1)["registered_hours"]["value"] == 2
    assert build_student_home_cards(SID, 1447, 2)["registered_hours"]["value"] == 5


def test_the_dashboard_does_not_import_private_adviser_internals():
    """The architectural boundary, not just today's behaviour.

        student dashboard -> shared student-registration service

    A private name reached across from `virtual_advisor` is how the semantics
    diverged in the first place: that helper is free to change its term-selection
    rule for chat's benefit, and the dashboard would silently follow.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("core/services/student_home_cards.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and "virtual_advisor" in (node.module or ""):
            private = [a.name for a in node.names if a.name.startswith("_")]
            assert not private, f"private adviser internals imported: {private}"
