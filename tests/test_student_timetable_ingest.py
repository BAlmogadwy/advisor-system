from __future__ import annotations

import pytest

from core.models import Student, TermSection, TermSectionMeeting
from core.services.student_timetable_ingest import ingest_student_timetable_html

# Minimal portal-shaped timetable: one course (CS112 F13) whose single row marks
# TWO day columns (Sunday + Tuesday) at the same time -> two meetings.
_HTML = """
<div>العام الدراسي : 1447 الفصل الدراسي : الثاني</div>
<table class="forumline">
<tr>
  <th>م</th><th>المادة</th><th>رمز</th><th>رقم</th><th>ساعات</th><th>شعبة</th>
  <th>من</th><th>إلى</th>
  <th>أحد</th><th>اثنين</th><th>ثلاثاء</th><th>أربعاء</th><th>خميس</th>
  <th>مبنى</th><th>دور</th><th>قاعة</th>
</tr>
<tr>
  <td>1</td><td>Intro to CS</td><td>CS</td><td>112</td><td>3</td><td>F13</td>
  <td>13:00</td><td>14:40</td>
  <td><img src="x.jpg"></td><td></td><td><img src="x.jpg"></td><td></td><td></td>
  <td>B1</td><td>F1</td><td>830</td>
</tr>
</table>
"""


@pytest.mark.django_db
def test_ingest_keeps_all_meetings_per_section():
    """Regression: a section marked on Sun+Tue must keep BOTH meetings.

    The old ingest created the section on its first parsed row and dropped the
    rest, so every section ended up with exactly one meeting.
    """
    Student.objects.create(student_id=999001, name="T", section="F", program="AI2")
    res = ingest_student_timetable_html("999001", _HTML, study_plan_codes={"CS112"})
    assert res["ok"], res
    ts = TermSection.objects.get(scenario__isnull=True, course_key="CS112", section="F13")
    days = sorted(TermSectionMeeting.objects.filter(term_section=ts).values_list("day", flat=True))
    assert days == ["SUN", "TUE"], days


@pytest.mark.django_db
def test_ingest_backfills_meetings_on_existing_section():
    """Re-ingesting backfills meetings an earlier partial scrape missed."""
    ts = TermSection.objects.create(
        course_key="CS112",
        section="F13",
        course_code="CS",
        course_number="112",
        course_name="Intro to CS",
        source_tag="scraper_timetable",
        source_file="x",
        created_at="",
        updated_at="",
    )
    TermSectionMeeting.objects.create(
        term_section=ts,
        day="SUN",
        start_time="13:00",
        end_time="14:40",
        room="830",
        building="",
        floor_wing="",
        instructor="",
        created_at="",
        updated_at="",
    )
    Student.objects.create(student_id=999002, name="T", section="F", program="AI2")
    ingest_student_timetable_html("999002", _HTML, study_plan_codes={"CS112"})
    days = sorted(TermSectionMeeting.objects.filter(term_section=ts).values_list("day", flat=True))
    assert days == ["SUN", "TUE"], days  # TUE backfilled onto the pre-existing section
