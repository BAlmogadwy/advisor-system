"""Courses the portal registers on NO weekday.

Modelled after a live portal response in which a Program Elective placeholder
carried a section, 3 credits and a time of 14:30-15:45 with every day cell empty.

The validator refused the whole response for it:

    Timetable courses have no supported meeting day: AI1

which made that student, and every student holding a Program Elective placeholder,
a graduation project, or a course taught elsewhere, permanently unscrapable.
"""

from __future__ import annotations

import pytest

from core.services.student_timetable_ingest import (
    ValidatedTimetableResponse,
    validate_timetable_response,
)

STUDENT_ID = 9999999

#: A course row with a time and NO day markers — the real shape, reduced.
UNSCHEDULED_ROW = """
<tr>
  <td>2</td><td>مقرر اختياري برنامج (1)</td><td>AI</td><td>1</td>
  <td>3</td><td>M6</td><td>14:30</td><td>15:45</td>
  <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
  <td>-</td><td>-</td><td>-</td>
</tr>
"""

SCHEDULED_ROW = """
<tr>
  <td>1</td><td>Scheduled Course</td><td>VX</td><td>101</td>
  <td>3</td><td>M7</td><td>09:00</td><td>10:15</td>
  <td><img src="mark.jpg"></td><td></td><td></td><td></td><td></td>
  <td>B1</td><td>F1</td><td>R101</td>
</tr>
"""


def _timetable(rows: str, *, declared_credits: int) -> str:
    return f"""
    <html><body>
      <div>العام الدراسي : 1448 الفصل الدراسي : الأول</div>
      <table class="forumline">
        <tr><th>رقم الطالب</th><td>{STUDENT_ID}</td></tr>
        <tr><th>مجموع الوحدات المسجلة</th><td>{declared_credits}</td></tr>
      </table>
      <table class="forumline">
        <tr>
          <th>م</th><th>المادة</th><th>القسم</th><th>رقم</th><th>وحدات</th>
          <th>شعبة</th><th>من</th><th>إلى</th>
          <th>أحد</th><th>اثنين</th><th>ثلاثاء</th><th>أربعاء</th><th>خميس</th>
          <th>مبنى</th><th>دور</th><th>قاعة</th>
        </tr>
        {rows}
      </table>
    </body></html>
    """


def _validate(rows: str, *, declared_credits: int) -> ValidatedTimetableResponse:
    return validate_timetable_response(
        _timetable(rows, declared_credits=declared_credits),
        expected_student_id=str(STUDENT_ID),
        expected_registered_credits=declared_credits,
    )


def test_a_course_with_no_day_is_accepted_and_reported_as_unscheduled():
    """This is the exact response that failed the first live scrape."""
    result = _validate(SCHEDULED_ROW + UNSCHEDULED_ROW, declared_credits=6)

    assert result["schedule_state"] == "complete_schedule"
    assert [r["course_code"] for r in result["rows"]] == ["VX"]
    assert len(result["unscheduled"]) == 1
    entry = result["unscheduled"][0]
    assert (entry["course_code"], entry["course_number"]) == ("AI", "1")
    assert entry["section"] == "M6"
    assert entry["credits"] == "3"


def test_an_unscheduled_course_still_counts_toward_the_declared_credit_total():
    """The completeness proof is the credit reconciliation, NOT the day markers.

    Accepting a dayless course would be a real loosening if it also dropped out of
    that reconciliation — the response could then omit courses undetectably. It
    does not: declare 3 instead of 6 and the response is still refused.
    """
    with pytest.raises(ValueError, match="registered-credit total"):
        _validate(SCHEDULED_ROW + UNSCHEDULED_ROW, declared_credits=3)


def test_a_timetable_of_only_unscheduled_courses_is_still_a_timetable():
    result = _validate(UNSCHEDULED_ROW, declared_credits=3)
    assert result["rows"] == []
    assert len(result["unscheduled"]) == 1


def test_scrape_orchestration_classifies_an_unscheduled_course_as_current(monkeypatch):
    from core.management.commands import scrape_students

    study_data = [{"dept": "AI", "no": "1"}]
    unscheduled = {
        "course_code": "AI",
        "course_number": "1",
        "section": "M6",
        "credits": "3",
    }
    monkeypatch.setattr("core.services.student_parser.parse_study_plan", lambda _html: study_data)
    monkeypatch.setattr("core.services.student_parser.parse_student_profile", lambda _html: {})
    monkeypatch.setattr(
        "core.services.student_parser.parse_timetable_info",
        lambda _html: {"current_registered_credits": 3},
    )
    monkeypatch.setattr(
        "core.services.student_timetable_ingest.validate_timetable_response",
        lambda *_args, **_kwargs: {
            "current_registered_credits": 3,
            "rows": [],
            "unscheduled": [unscheduled],
            "schedule_state": "complete_schedule",
        },
    )
    monkeypatch.setattr(
        "core.services.course_classifier.classify_courses",
        lambda _study_data, current: {"current": set(current)},
    )
    monkeypatch.setattr(scrape_students, "_validate_study_plan", lambda _rows: (True, ""))
    monkeypatch.setattr(scrape_students, "_validate_study_plan_for_program", lambda *_args: None)
    monkeypatch.setattr(
        scrape_students, "_validate_student_profile", lambda *_args, **_kwargs: None
    )

    result = scrape_students._parse_and_validate_student_response(
        str(STUDENT_ID),
        "study",
        "timetable",
        "AI",
    )

    assert result["current_course_codes"] == {"AI1"}
    assert result["classification"]["current"] == {"AI1"}


def test_a_row_with_broken_times_is_still_refused():
    """Loosening the DAY rule must not loosen the rest of the row validation."""
    broken = UNSCHEDULED_ROW.replace("<td>14:30</td><td>15:45</td>", "<td>15:45</td><td>14:30</td>")
    with pytest.raises(ValueError, match="start time must be before end time"):
        _validate(broken, declared_credits=3)


def test_an_incomplete_day_column_set_is_still_refused():
    broken = UNSCHEDULED_ROW.replace(
        "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>\n  <td>-</td><td>-</td><td>-</td>",
        "<td>-</td><td>-</td>",
    )
    with pytest.raises(ValueError):
        _validate(broken, declared_credits=3)


# ---------------------------------------------------------------------------
# Persistence: `meetings=None` is not `meetings=[]`.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unscheduled_registration_never_deletes_a_sections_meetings():
    """`[]` computes an empty desired set and deletes every meeting the section
    has. A scrape of ONE student would then erase timetable data another source
    imported for a shared section."""
    from core.models import TermSection, TermSectionMeeting
    from core.services.student_timetable_ingest import _ensure_term_section

    section = TermSection.objects.create(
        source_tag="other",
        course_name="Shared",
        course_code="AI",
        course_number="1",
        course_key="AI1",
        section="M6",
    )
    TermSectionMeeting.objects.create(
        term_section=section, day="SUN", start_time="09:00", end_time="10:15", room="R1"
    )

    _ensure_term_section(
        course_key="AI1",
        course_code="AI",
        course_number="1",
        course_name="Shared",
        section="M6",
        meetings=None,
    )
    assert TermSectionMeeting.objects.filter(term_section=section).count() == 1, (
        "an unscheduled registration says nothing about the section's meetings"
    )

    # And the distinction is real: [] genuinely means "this section has none".
    _ensure_term_section(
        course_key="AI1",
        course_code="AI",
        course_number="1",
        course_name="Shared",
        section="M6",
        meetings=[],
    )
    assert TermSectionMeeting.objects.filter(term_section=section).count() == 0


@pytest.mark.django_db
def test_an_unscheduled_registration_is_linked_to_the_student():
    """It carries a section and credits and is counted in the portal's own total,
    so leaving it out would under-record what the student is registered in."""
    from core.models import ProgrammeRequirement, Student, StudentTermSection
    from core.services.student_timetable_ingest import ingest_student_timetable_html

    Student.objects.create(student_id=STUDENT_ID, name="T", program="AI", section="M")
    for code in ("VX101", "AI1"):
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, programme_term=1, credit_hours=3
        )

    html = _timetable(SCHEDULED_ROW + UNSCHEDULED_ROW, declared_credits=6)
    result = ingest_student_timetable_html(
        student_id=str(STUDENT_ID),
        timetable_html=html,
        study_plan_codes={"VX101", "AI1"},
        validated_response=_validate(SCHEDULED_ROW + UNSCHEDULED_ROW, declared_credits=6),
    )

    assert result["ok"] is True
    assert result["unscheduled_registrations"] == 1
    linked = {
        row.term_section.course_key
        for row in StudentTermSection.objects.filter(
            student_id=STUDENT_ID, source="scraper_timetable"
        ).select_related("term_section")
    }
    assert linked == {"VX101", "AI1"}
