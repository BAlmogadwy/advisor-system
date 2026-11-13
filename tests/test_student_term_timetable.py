"""The student term-timetable service: what it returns, and when it refuses.

The home screen's behaviour through this service is pinned by
``test_student_home_timetable_characterization.py``. These tests pin the
service's own contract: the split by provenance class, the cohort rule as the
reader applies it, the two difference lists, a failed read being an error and
never an empty timetable, and which errors the home screen hides and which it
must not.
"""

from __future__ import annotations

import logging

import pytest
from django.test import Client
from django.urls import reverse

from core.models import Student, StudentTermSection, TermSection, TermSectionMeeting
from core.services import student_otp, student_sections, student_term_timetable
from core.services.rbac import ensure_role_groups
from core.services.student_term_timetable import (
    StudentTermTimetable,
    TimetableUnavailable,
    read_student_term_timetable,
)

SID = 4980201
OTHER_SID = 4980202
YEAR = "1448"
TERM = "1"
PLAN = "registration_plan_1448_t1"
REGISTRAR = "scraper_timetable"
AR_SA101 = "مقدمة في البرمجة"


# ---------------------------------------------------------------------------
# The derived facts. Pure: built from rows in hand.
# ---------------------------------------------------------------------------


def _meeting(code, section="M1", day="SUN", start="09:00", end="10:15"):
    return {
        "course_code": code,
        "section": section,
        "day": day,
        "start_time": start,
        "end_time": end,
    }


def _timetable(registered=(), forecast=()):
    return StudentTermTimetable(
        academic_year=YEAR,
        term=TERM,
        registered_rows=list(registered),
        forecast_rows=list(forecast),
    )


def test_codes_are_distinct_normalised_and_never_blank():
    timetable = _timetable(
        registered=[
            _meeting("SA101", day="SUN"),
            _meeting("SA101", day="TUE"),
            _meeting(" sb 201 ", section="M2"),
            _meeting("   "),
            {"section": "M3"},
        ],
        forecast=[_meeting("SC201")],
    )
    assert timetable.registered_codes == frozenset({"SA101", "SB201"})
    assert timetable.forecast_codes == frozenset({"SC201"})


def test_the_differences_are_sorted_and_go_both_ways():
    timetable = _timetable(
        registered=[_meeting("SB201"), _meeting("SZ999"), _meeting("SC201")],
        forecast=[_meeting("SD301"), _meeting("SB201"), _meeting("SA101")],
    )
    assert timetable.forecast_not_registered == ["SA101", "SD301"]
    assert timetable.registered_not_forecast == ["SC201", "SZ999"]


@pytest.mark.parametrize(
    ("registered", "forecast"),
    [((), [_meeting("SA101")]), ([_meeting("SA101")], ()), ((), ())],
)
def test_no_difference_is_stated_unless_both_snapshots_exist(registered, forecast):
    timetable = _timetable(registered=registered, forecast=forecast)
    assert timetable.forecast_not_registered == []
    assert timetable.registered_not_forecast == []


def test_presence_is_reported_per_snapshot():
    assert _timetable(registered=[_meeting("SA101")]).has_registration is True
    assert _timetable(registered=[_meeting("SA101")]).has_forecast is False
    assert _timetable(forecast=[_meeting("SA101")]).has_forecast is True
    assert _timetable(forecast=[_meeting("SA101")]).has_registration is False


def test_an_empty_term_names_its_term_as_text():
    empty = StudentTermTimetable.empty(1448, 1)
    assert (empty.academic_year, empty.term) == ("1448", "1")
    assert empty.registered_rows == [] and empty.forecast_rows == []
    assert not empty.has_registration and not empty.has_forecast


# ---------------------------------------------------------------------------
# The read. Against the database.
# ---------------------------------------------------------------------------


@pytest.fixture
def sections(db):
    ensure_role_groups()
    for sid in (SID, OTHER_SID):
        Student.objects.update_or_create(
            student_id=sid,
            defaults={"name": f"Student {sid}", "program": "CHR", "section": "M", "gpa": 3.0},
        )
    made = {}
    for code, label, name in (
        ("SA101", "M1", "INTRO TO PROGRAMMING"),
        ("SB201", "M1", "DATA STRUCTURES"),
        ("SC201", "M1", "DISCRETE MATHEMATICS"),
        ("SA101", "F1", "INTRO TO PROGRAMMING"),
        ("SB201", "YM1", "DATA STRUCTURES"),
    ):
        section = TermSection.objects.create(
            source_tag="other",
            course_name=name,
            course_code=code[:2],
            course_number=code[2:],
            course_key=code,
            section=label,
        )
        TermSectionMeeting.objects.create(
            term_section=section,
            day="SUN",
            start_time="09:00",
            end_time="10:15",
            room="A1",
            instructor="Staff",
        )
        made[f"{code}-{label}"] = section
    # The authoritative Arabic name for SA101, on a section nobody is linked to.
    TermSection.objects.create(
        source_tag="other",
        course_name=AR_SA101,
        course_code="SA",
        course_number="101",
        course_key="SA101",
        section="M9",
    )
    return made


def _link(sections, key, source, student_id=SID, year=YEAR, term=TERM):
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year=year,
        term=term,
        term_section=sections[key],
        source=source,
    )


def _codes(rows):
    return sorted({row["course_code"] for row in rows})


def _labels(rows):
    return sorted(row["section"] for row in rows)


def test_the_read_splits_by_provenance_class(sections):
    _link(sections, "SA101-M1", PLAN)
    _link(sections, "SB201-M1", REGISTRAR)

    timetable = read_student_term_timetable(SID, int(YEAR), int(TERM))

    assert (timetable.academic_year, timetable.term) == (YEAR, TERM)
    assert _codes(timetable.registered_rows) == ["SB201"]
    assert _codes(timetable.forecast_rows) == ["SA101"]


def test_a_staff_working_mapping_is_the_forecast_and_never_a_registration(sections):
    _link(sections, "SA101-M1", PLAN)
    _link(sections, "SB201-M1", "planner")
    _link(sections, "SC201-M1", REGISTRAR)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert _codes(timetable.forecast_rows) == ["SB201"], "WORKING outranks the imported plan"
    assert _codes(timetable.registered_rows) == ["SC201"]


def test_the_imported_plan_is_the_forecast_when_there_is_no_staff_mapping(sections):
    _link(sections, "SA101-M1", PLAN)
    _link(sections, "SC201-M1", REGISTRAR)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert _codes(timetable.forecast_rows) == ["SA101"]


def test_registrar_rows_alone_leave_no_forecast(sections):
    _link(sections, "SC201-M1", REGISTRAR)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert timetable.forecast_rows == []
    assert timetable.has_registration is True


def test_another_cohorts_and_the_other_branchs_sections_are_not_read(sections):
    _link(sections, "SA101-F1", PLAN)
    _link(sections, "SB201-YM1", PLAN)
    _link(sections, "SA101-M1", PLAN)
    _link(sections, "SA101-F1", REGISTRAR)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert _labels(timetable.forecast_rows) == ["M1"]
    assert timetable.registered_rows == [], "another cohort's row is not this student's record"


def test_with_no_recorded_cohort_both_cohorts_are_read_but_never_the_other_branch(sections):
    Student.objects.filter(student_id=SID).update(section="")
    _link(sections, "SA101-F1", PLAN)
    _link(sections, "SB201-YM1", PLAN)
    _link(sections, "SA101-M1", PLAN)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert _labels(timetable.forecast_rows) == ["F1", "M1"]


def test_only_the_named_students_rows_are_read(sections):
    _link(sections, "SA101-M1", PLAN, student_id=OTHER_SID)
    _link(sections, "SB201-M1", PLAN)

    timetable = read_student_term_timetable(SID, YEAR, TERM)

    assert _codes(timetable.forecast_rows) == ["SB201"]


def test_only_the_named_term_is_read(sections):
    _link(sections, "SA101-M1", PLAN, year="1447", term="2")
    _link(sections, "SB201-M1", PLAN)

    assert _codes(read_student_term_timetable(SID, YEAR, TERM).forecast_rows) == ["SB201"]
    assert _codes(read_student_term_timetable(SID, "1447", "2").forecast_rows) == ["SA101"]


@pytest.mark.parametrize(
    ("prefer_arabic", "expected"), [(False, "INTRO TO PROGRAMMING"), (True, AR_SA101)]
)
def test_arabic_names_are_used_only_when_asked_for_in_both_snapshots(
    sections, prefer_arabic, expected
):
    _link(sections, "SA101-M1", PLAN)
    _link(sections, "SA101-M1", REGISTRAR)

    timetable = read_student_term_timetable(SID, YEAR, TERM, prefer_arabic_names=prefer_arabic)

    assert [row["course_name"] for row in timetable.forecast_rows] == [expected]
    assert [row["course_name"] for row in timetable.registered_rows] == [expected]


# ---------------------------------------------------------------------------
# A failed read is an error, never "no timetable".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cause",
    [RuntimeError("timetable store unavailable"), KeyError("a defect inside the reader")],
)
def test_any_failure_of_the_reader_raises_and_keeps_its_cause(sections, monkeypatch, cause):
    _link(sections, "SA101-M1", PLAN)

    def failing(*_args, **_kwargs):
        raise cause

    # Patched on the reader's own module: the service imports it at the read
    # boundary, so this also proves the service looks it up there.
    monkeypatch.setattr(student_sections, "get_student_term_baseline", failing)

    with pytest.raises(TimetableUnavailable) as raised:
        read_student_term_timetable(SID, YEAR, TERM)
    assert raised.value.__cause__ is cause


def test_an_error_after_the_read_is_not_converted(sections, monkeypatch):
    """The name swap is not the reader. Its error propagates as itself, as it did
    from the inline code, so the home screen does not report it as "no timetable"."""
    _link(sections, "SA101-M1", PLAN)

    def broken_swap(_rows):
        raise ValueError("bad name data")

    monkeypatch.setattr(student_term_timetable, "prefer_arabic_timetable_course_names", broken_swap)

    with pytest.raises(ValueError, match="bad name data"):
        read_student_term_timetable(SID, YEAR, TERM, prefer_arabic_names=True)


# ---------------------------------------------------------------------------
# The home screen: which failures it hides, and which it must not.
# ---------------------------------------------------------------------------


def _get_home(monkeypatch, language="en"):
    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {"academic_year": int(YEAR), "term": int(TERM)},
    )
    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    return client.get(
        reverse("student_home"), headers={"accept-language": language}, SERVER_NAME="testserver"
    )


def _messages(caplog):
    return [record.getMessage() for record in caplog.records]


def test_the_home_screen_degrades_when_the_service_refuses(sections, monkeypatch, caplog):
    _link(sections, "SA101-M1", PLAN)

    def refusing(*_args, **_kwargs):
        raise TimetableUnavailable("timetable store unavailable")

    monkeypatch.setattr("core.student_auth_views.read_student_term_timetable", refusing)

    with caplog.at_level(logging.ERROR, logger="core.student_auth_views"):
        response = _get_home(monkeypatch)

    assert response.status_code == 200
    assert response.context["timetable_panels"] == []
    assert response.context["has_expected_timetable"] is False
    assert f"student timetable load failed for {SID}" in _messages(caplog)


def test_a_defect_in_the_configured_terms_read_path_is_not_hidden(sections, monkeypatch):
    """The configured term feeds every card. Only a failed READ may pass as "no
    timetable"; a broken name swap must surface, as it did before the service."""
    _link(sections, "SA101-M1", PLAN)

    def broken_swap(_rows):
        raise ValueError("bad name data")

    monkeypatch.setattr(student_term_timetable, "prefer_arabic_timetable_course_names", broken_swap)

    with pytest.raises(ValueError, match="bad name data"):
        _get_home(monkeypatch, language="ar")


def test_any_failure_in_the_fallback_term_only_drops_its_panel(sections, monkeypatch, caplog):
    """The fallback is a best-effort stand-in for another term: whatever breaks in
    it, the page still renders, labelled with that term, and the failure is logged."""
    _link(sections, "SA101-M1", PLAN, year="1447", term="2")

    def broken_swap(rows):
        if rows:  # the configured term is empty; only the fallback has rows
            raise ValueError("bad name data")
        return rows

    monkeypatch.setattr(student_term_timetable, "prefer_arabic_timetable_course_names", broken_swap)

    with caplog.at_level(logging.ERROR, logger="core.student_auth_views"):
        response = _get_home(monkeypatch, language="ar")

    assert response.status_code == 200
    assert (str(response.context["timetable_year"]), str(response.context["timetable_term"])) == (
        "1447",
        "2",
    )
    assert response.context["timetable_is_fallback"] is True
    assert response.context["timetable_panels"] == []
    assert f"student fallback timetable failed for {SID}" in _messages(caplog)
