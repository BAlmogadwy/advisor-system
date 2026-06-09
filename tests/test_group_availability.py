"""Tests for the group-availability (common free-slot) finder.

Covers the aggregation service (busy/free cells, off-grid overlap, the lab
grid, not-found / no-schedule reporting, ID normalisation) and the page +
compute view wiring end-to-end via the Django test client.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from core.models import (
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.group_availability import (
    compute_group_availability,
    normalise_student_ids,
)

pytestmark = pytest.mark.django_db

YEAR = "1448"
TERM = "1"

# Lecture slot indices (DEFAULT_SLOTS): 0=09:00-10:15, 1=10:30-11:45,
# 2=13:00-14:15, 3=14:30-15:45, 4=16:00-17:15.


def _make_global_section(
    course_code: str, section: str, meetings: list[tuple[str, str, str]]
) -> TermSection:
    """Create a GLOBAL (scenario=NULL) section with the given meetings.

    ``get_student_term_baseline`` only reads scenario-NULL sections, matching
    the system-of-record registrations the finder is built on.
    """
    ts = TermSection.objects.create(
        course_code=course_code,
        course_number=course_code,
        course_key=course_code,
        section=section,
        course_name=course_code,
        source_tag="test",
    )
    for day, start, end in meetings:
        TermSectionMeeting.objects.create(term_section=ts, day=day, start_time=start, end_time=end)
    return ts


def _enrol(student_id: int, ts: TermSection, *, program: str = "CS") -> None:
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": f"Student {student_id}", "program": program},
    )
    StudentTermSection.objects.create(
        student_id=student_id, academic_year=YEAR, term=TERM, term_section=ts
    )


def _cell(result: dict, grid: str, day: str, slot_index: int) -> dict:
    return result["grids"][grid]["cells"][day][slot_index]


def test_shared_busy_slot_is_not_free_and_other_slot_is_free():
    sec = _make_global_section("CS101", "S1", [("MON", "09:00", "10:15")])
    _enrol(1001, sec)
    _enrol(1002, sec)

    result = compute_group_availability([1001, 1002], YEAR, TERM)

    assert result["requested_count"] == 2
    assert result["resolved_count"] == 2

    busy = _cell(result, "lecture", "MON", 0)
    assert busy["busy_count"] == 2
    assert busy["free"] is False

    free = _cell(result, "lecture", "TUE", 0)
    assert free["busy_count"] == 0
    assert free["free"] is True


def test_partial_overlap_counts_only_busy_students():
    sec = _make_global_section("CS201", "S1", [("MON", "10:30", "11:45")])
    _enrol(2001, sec)
    Student.objects.create(student_id=2002, name="Free", program="CS")
    # 2002 has no registered section → free everywhere.

    result = compute_group_availability([2001, 2002], YEAR, TERM)

    partial = _cell(result, "lecture", "MON", 1)
    assert partial["busy_count"] == 1
    assert partial["free"] is False
    # The busy student's other slots stay free.
    assert _cell(result, "lecture", "MON", 0)["free"] is True


def test_offgrid_meeting_marks_every_overlapping_slot():
    # 09:30–10:45 straddles lecture slot 0 (09:00-10:15) and slot 1 (10:30-11:45).
    sec = _make_global_section("CS301", "S1", [("TUE", "09:30", "10:45")])
    _enrol(3001, sec)

    result = compute_group_availability([3001], YEAR, TERM)

    assert _cell(result, "lecture", "TUE", 0)["busy_count"] == 1
    assert _cell(result, "lecture", "TUE", 1)["busy_count"] == 1
    # A non-overlapping slot is still free.
    assert _cell(result, "lecture", "TUE", 2)["free"] is True


def test_lab_grid_reflects_lab_length_meeting():
    # Lab slot 0 is 09:00-10:40.
    sec = _make_global_section("CS401", "S1", [("WED", "09:00", "10:40")])
    _enrol(4001, sec)

    result = compute_group_availability([4001], YEAR, TERM)

    assert _cell(result, "lab", "WED", 0)["busy_count"] == 1
    assert _cell(result, "lab", "WED", 0)["free"] is False
    # free_for_all_count drops below the full 25 cells when something is busy.
    assert result["grids"]["lab"]["free_for_all_count"] == 24


def test_scenario_scoped_section_is_included():
    """In this system, schedules live under a planning scenario — the finder
    must read scenario-owned sections, not only global (scenario-NULL) ones."""
    scenario = TimetableScenario.objects.create(academic_year=YEAR, term=TERM, name="S")
    ts = TermSection.objects.create(
        scenario=scenario,
        course_code="CS900",
        course_number="CS900",
        course_key="CS900",
        section="S1",
        course_name="CS900",
        source_tag="test",
    )
    TermSectionMeeting.objects.create(
        term_section=ts, day="MON", start_time="09:00", end_time="10:15"
    )
    Student.objects.create(student_id=9001, name="Scoped", program="CS")
    StudentTermSection.objects.create(
        student_id=9001, academic_year=YEAR, term=TERM, term_section=ts
    )

    result = compute_group_availability([9001])  # term auto-detected
    assert result["resolved_count"] == 1
    assert _cell(result, "lecture", "MON", 0)["busy_count"] == 1


def test_auto_detects_current_term_without_explicit_term():
    sec = _make_global_section("CS950", "S1", [("TUE", "13:00", "14:15")])
    _enrol(9501, sec)

    result = compute_group_availability([9501])  # no year/term passed
    assert result["academic_year"] == YEAR
    assert result["term"] == TERM
    assert _cell(result, "lecture", "TUE", 2)["busy_count"] == 1


def test_occupants_carry_course_identity():
    sec = _make_global_section("CS501", "S2", [("SUN", "13:00", "14:15")])
    _enrol(5001, sec)

    result = compute_group_availability([5001], YEAR, TERM)
    cell = _cell(result, "lecture", "SUN", 2)
    assert cell["busy_count"] == 1
    occ = cell["occupants"]
    assert len(occ) == 1
    assert occ[0]["student_id"] == 5001
    assert occ[0]["course_code"] == "CS501"
    assert occ[0]["section"] == "S2"


def test_not_found_vs_no_schedule_reporting():
    sec = _make_global_section("CS601", "S1", [("MON", "09:00", "10:15")])
    _enrol(6001, sec)
    Student.objects.create(student_id=6002, name="No sections", program="CS")  # exists, unenrolled

    result = compute_group_availability([6001, 6002, 9999], YEAR, TERM)

    assert result["requested_count"] == 3
    assert result["resolved_count"] == 1
    assert result["no_schedule"] == [6002]
    assert result["not_found"] == [9999]


def test_normalise_student_ids_dedupes_and_drops_nonnumeric():
    assert normalise_student_ids(["5", "5", 5, "x", 7, None]) == [5, 7]


def test_empty_group_returns_all_free():
    result = compute_group_availability([], YEAR, TERM)
    assert result["requested_count"] == 0
    assert result["grids"]["lecture"]["free_for_all_count"] == 25
    assert result["grids"]["lab"]["free_for_all_count"] == 25


# ── View wiring ──────────────────────────────────────────────


def _login_client() -> Client:
    user = get_user_model().objects.create_user(username="ga_tester", password="x")
    client = Client()
    client.force_login(user)
    return client


def test_page_renders_with_config():
    client = _login_client()
    resp = client.get(reverse("group_availability_page"))
    assert resp.status_code == 200
    assert b"groupAvailabilityConfig" in resp.content


def test_compute_endpoint_returns_grids():
    sec = _make_global_section("CS701", "S1", [("MON", "09:00", "10:15")])
    _enrol(7001, sec)
    client = _login_client()

    resp = client.post(
        reverse("group_availability_compute"),
        data=json.dumps({"student_ids": [7001]}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_count"] == 1
    assert body["grids"]["lecture"]["cells"]["MON"][0]["busy_count"] == 1


def test_compute_endpoint_rejects_empty_ids():
    client = _login_client()
    resp = client.post(
        reverse("group_availability_compute"),
        data=json.dumps({"student_ids": []}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_compute_endpoint_parses_freetext_ids():
    sec = _make_global_section("CS801", "S1", [("TUE", "10:30", "11:45")])
    _enrol(8001, sec)
    _enrol(8002, sec)
    client = _login_client()

    resp = client.post(
        reverse("group_availability_compute"),
        data=json.dumps({"student_ids": "8001, 8002\n8001"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["requested_count"] == 2  # deduped
    assert body["grids"]["lecture"]["cells"]["TUE"][1]["busy_count"] == 2
