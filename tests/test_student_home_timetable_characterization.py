"""What the student home screen derives from a student's term timetable, recorded.

Characterization tests: they were written against the view BEFORE the timetable
read moved into its own service, and they must pass unchanged after it.

The four main shapes -- expected only, registered only, both, and the
single-other-term fallback -- record the whole observable result: the panels
(which snapshot, which term, every meeting, the unscheduled sections), the two
difference lists, the recommendation split, and the rows the academic-summary
cards are given, in English and in Arabic, because the Arabic course-name swap
happens in the middle of this path. The other shapes assert only the facts that
shape changes: a staff WORKING mapping over the imported plan, an opposite-cohort
and an other-branch (YM/YF) section, a student with no recorded cohort, two loaded
terms, and a failed read of the configured term, of the fallback term, or of both.
"""

from __future__ import annotations

import logging

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
from core.services import student_otp, student_sections
from core.services.rbac import ensure_role_groups
from core.services.timetable_snapshots import Snapshot

SID = 4980101
PROG = "CHR"
YEAR = "1448"
TERM = "1"
PLAN = "registration_plan_1448_t1"
REGISTRAR = "scraper_timetable"

# The recommender is replaced by this fixed answer, so the split of it into
# "recommended", "already registered" and "already planned" is what is observed.
RECOMMENDED = ["SA101", "SB201", "SC201", "SD301", "SE401"]

AR_SA101 = "مقدمة في البرمجة"


@pytest.fixture
def world(db):
    """Five courses and the sections the scenarios link the student to."""
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={"name": "Characterized", "program": PROG, "section": "M", "gpa": 3.5},
    )
    names = {
        "SA101": "INTRO TO PROGRAMMING",
        "SB201": "DATA STRUCTURES",
        "SC201": "DISCRETE MATHEMATICS",
        "SD301": "SOFTWARE PROJECT",
        "SE401": "COMPILERS",
    }
    for code, name in names.items():
        Course.objects.update_or_create(
            course_code=code, defaults={"description": name, "credit_hours": 3}
        )
    for code in ("SA101", "SB201", "SC201", "SD301"):
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3, "type": "Mandatory"},
        )

    def section(code, label, meetings, course_name=None):
        made = TermSection.objects.create(
            source_tag="other",
            course_name=course_name if course_name is not None else names[code],
            course_code=code[:2],
            course_number=code[2:],
            course_key=code,
            section=label,
        )
        for day, start, end, room in meetings:
            TermSectionMeeting.objects.create(
                term_section=made,
                day=day,
                start_time=start,
                end_time=end,
                room=room,
                instructor="Staff",
            )
        return made

    sections = {
        "SA101-M1": section(
            "SA101",
            "M1",
            [("TUE", "09:00", "10:15", "A1"), ("SUN", "09:00", "10:15", "A1")],
        ),
        "SB201-M1": section("SB201", "M1", [("MON", "11:00", "12:15", "B2")]),
        "SC201-M1": section("SC201", "M1", [("WED", "13:00", "14:15", "C3")]),
        # A section the registrar lists with no meeting times at all.
        "SD301-M2": section("SD301", "M2", []),
        # Another cohort's section of a course the student also takes.
        "SA101-F1": section("SA101", "F1", [("SUN", "08:00", "09:15", "F9")]),
        # The other branch: never shown to anyone, whatever their cohort.
        "SB201-YM1": section("SB201", "YM1", [("MON", "08:00", "09:15", "Y9")]),
    }
    # Not linked to the student: the authoritative Arabic name the census finds
    # for SA101. SB201..SE401 have none, so they keep their stored names in Arabic.
    section("SA101", "M9", [("THU", "08:00", "09:15", "Z9")], course_name=AR_SA101)
    return sections


def _link(world, key, source, year=YEAR, term=TERM):
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=year,
        term=term,
        term_section=world[key],
        source=source,
    )


def _home(monkeypatch, language):
    """GET the home screen; return the response and the cards' recorded inputs."""
    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {
            "academic_year": int(YEAR),
            "term": int(TERM),
            "currentYear": int(YEAR),
            "currentTerm": int(TERM),
        },
    )
    monkeypatch.setattr(
        "core.student_auth_views.recommend_next_courses",
        lambda student_id, year, term: list(RECOMMENDED),
    )
    from core.services.student_home_cards import build_student_home_cards as real_cards

    card_calls: list[dict] = []

    def recording_cards(**kwargs):
        card_calls.append(kwargs)
        return real_cards(**kwargs)

    monkeypatch.setattr("core.student_auth_views.build_student_home_cards", recording_cards)

    client = Client()
    client.force_login(student_otp.provision_student_user(SID))
    response = client.get(
        reverse("student_home"), headers={"accept-language": language}, SERVER_NAME="testserver"
    )
    assert response.status_code == 200, response.status_code
    return response, card_calls


def _observe(response, card_calls) -> dict:
    """Everything the screen derived from the timetable rows, as plain data."""
    ctx = response.context
    assert len(card_calls) == 1, card_calls
    call = card_calls[0]
    return {
        "timetable_term": (
            str(ctx["timetable_year"]),
            str(ctx["timetable_term"]),
            ctx["timetable_is_fallback"],
        ),
        "panels": [
            {
                "kind": panel["kind"],
                "term": (str(panel["academic_year"]), str(panel["term"]), panel["is_fallback"]),
                "days": [day["code"] for day in panel["timetable"]],
                "meetings": [
                    (
                        m["course_code"],
                        m["course_name"],
                        m["section"],
                        m["day"],
                        m["start_time"],
                        m["end_time"],
                        m["room"],
                        m["instructor"],
                        m["source"],
                    )
                    for m in panel["meetings"]
                ],
                "unscheduled": [
                    (row["course_code"], row["course_name"], row["section"], row["source"])
                    for row in panel["unscheduled"]
                ],
            }
            for panel in ctx["timetable_panels"]
        ],
        "has_registered": ctx["has_registered_timetable"],
        "has_expected": ctx["has_expected_timetable"],
        "expected_not_registered": list(ctx["expected_not_registered"]),
        "registered_not_expected": list(ctx["registered_not_expected"]),
        "recommendations": [
            (rec["code"], rec["name"], rec["credits"]) for rec in ctx["recommendations"]
        ],
        "already_current": list(ctx["recommendations_already_current"]),
        "already_expected": list(ctx["recommendations_already_expected"]),
        "cards_term": (str(call["academic_year"]), str(call["term"]), call["prefer_arabic_names"]),
        "cards_rows": [
            (row["course_code"], row["course_name"], row["section"], row["source"])
            for row in call["current_term_rows"]
        ],
    }


def _name(code: str, language: str) -> str:
    """The course name each language shows: only SA101 has an Arabic section name."""
    stored = {
        "SA101": "INTRO TO PROGRAMMING",
        "SB201": "DATA STRUCTURES",
        "SC201": "DISCRETE MATHEMATICS",
        "SD301": "SOFTWARE PROJECT",
        "SE401": "COMPILERS",
    }[code]
    return AR_SA101 if (language == "ar" and code == "SA101") else stored


def _sa101_meetings(language, source):
    return [
        ("SA101", _name("SA101", language), "M1", "SUN", "09:00", "10:15", "A1", "Staff", source),
        ("SA101", _name("SA101", language), "M1", "TUE", "09:00", "10:15", "A1", "Staff", source),
    ]


LANGUAGES = pytest.mark.parametrize("language", ["en", "ar"])


@LANGUAGES
def test_expected_only(world, monkeypatch, language):
    _link(world, "SA101-M1", PLAN)
    _link(world, "SB201-M1", PLAN)
    _link(world, "SD301-M2", PLAN)

    observed = _observe(*_home(monkeypatch, language))

    assert observed == {
        "timetable_term": (YEAR, TERM, False),
        "panels": [
            {
                "kind": "expected",
                "term": (YEAR, TERM, False),
                "days": ["SUN", "MON", "TUE"],
                "meetings": [
                    _sa101_meetings(language, "planned")[0],
                    (
                        "SB201",
                        _name("SB201", language),
                        "M1",
                        "MON",
                        "11:00",
                        "12:15",
                        "B2",
                        "Staff",
                        "planned",
                    ),
                    _sa101_meetings(language, "planned")[1],
                ],
                "unscheduled": [("SD301", _name("SD301", language), "M2", PLAN)],
            }
        ],
        "has_registered": False,
        "has_expected": True,
        # Only meaningful when BOTH snapshots exist.
        "expected_not_registered": [],
        "registered_not_expected": [],
        "recommendations": [
            ("SC201", _name("SC201", language), 3),
            ("SE401", _name("SE401", language), 3),
        ],
        "already_current": [],
        "already_expected": ["SA101", "SB201", "SD301"],
        "cards_term": (YEAR, TERM, language == "ar"),
        # A forecast never reaches the academic-summary cards.
        "cards_rows": [],
    }


@LANGUAGES
def test_registered_only(world, monkeypatch, language):
    _link(world, "SA101-M1", REGISTRAR)
    _link(world, "SC201-M1", REGISTRAR)

    observed = _observe(*_home(monkeypatch, language))

    assert observed == {
        "timetable_term": (YEAR, TERM, False),
        "panels": [
            {
                "kind": "registered",
                "term": (YEAR, TERM, False),
                "days": ["SUN", "TUE", "WED"],
                "meetings": [
                    _sa101_meetings(language, "current")[0],
                    _sa101_meetings(language, "current")[1],
                    (
                        "SC201",
                        _name("SC201", language),
                        "M1",
                        "WED",
                        "13:00",
                        "14:15",
                        "C3",
                        "Staff",
                        "current",
                    ),
                ],
                "unscheduled": [],
            }
        ],
        "has_registered": True,
        "has_expected": False,
        "expected_not_registered": [],
        "registered_not_expected": [],
        "recommendations": [
            ("SB201", _name("SB201", language), 3),
            ("SD301", _name("SD301", language), 3),
            ("SE401", _name("SE401", language), 3),
        ],
        "already_current": ["SA101", "SC201"],
        "already_expected": [],
        "cards_term": (YEAR, TERM, language == "ar"),
        "cards_rows": [
            ("SA101", _name("SA101", language), "M1", REGISTRAR),
            ("SA101", _name("SA101", language), "M1", REGISTRAR),
            ("SC201", _name("SC201", language), "M1", REGISTRAR),
        ],
    }


@LANGUAGES
def test_both_snapshots_with_differences(world, monkeypatch, language):
    _link(world, "SA101-M1", PLAN)
    _link(world, "SB201-M1", PLAN)
    _link(world, "SB201-M1", REGISTRAR)
    _link(world, "SC201-M1", REGISTRAR)

    observed = _observe(*_home(monkeypatch, language))

    sb201 = (
        "SB201",
        _name("SB201", language),
        "M1",
        "MON",
        "11:00",
        "12:15",
        "B2",
        "Staff",
    )
    assert observed == {
        "timetable_term": (YEAR, TERM, False),
        "panels": [
            {
                "kind": "registered",
                "term": (YEAR, TERM, False),
                "days": ["MON", "WED"],
                "meetings": [
                    (*sb201, "current"),
                    (
                        "SC201",
                        _name("SC201", language),
                        "M1",
                        "WED",
                        "13:00",
                        "14:15",
                        "C3",
                        "Staff",
                        "current",
                    ),
                ],
                "unscheduled": [],
            },
            {
                "kind": "expected",
                "term": (YEAR, TERM, False),
                "days": ["SUN", "MON", "TUE"],
                "meetings": [
                    _sa101_meetings(language, "planned")[0],
                    (*sb201, "planned"),
                    _sa101_meetings(language, "planned")[1],
                ],
                "unscheduled": [],
            },
        ],
        "has_registered": True,
        "has_expected": True,
        "expected_not_registered": ["SA101"],
        "registered_not_expected": ["SC201"],
        "recommendations": [
            ("SD301", _name("SD301", language), 3),
            ("SE401", _name("SE401", language), 3),
        ],
        "already_current": ["SB201", "SC201"],
        "already_expected": ["SA101", "SB201"],
        "cards_term": (YEAR, TERM, language == "ar"),
        "cards_rows": [
            ("SB201", _name("SB201", language), "M1", REGISTRAR),
            ("SC201", _name("SC201", language), "M1", REGISTRAR),
        ],
    }


@LANGUAGES
def test_a_staff_working_mapping_masks_the_imported_plan(world, monkeypatch, language):
    _link(world, "SA101-M1", PLAN)
    _link(world, "SB201-M1", "planner")

    observed = _observe(*_home(monkeypatch, language))

    assert observed["panels"] == [
        {
            "kind": "expected",
            "term": (YEAR, TERM, False),
            "days": ["MON"],
            "meetings": [
                (
                    "SB201",
                    _name("SB201", language),
                    "M1",
                    "MON",
                    "11:00",
                    "12:15",
                    "B2",
                    "Staff",
                    "planned",
                )
            ],
            "unscheduled": [],
        }
    ]
    assert observed["has_registered"] is False
    assert observed["has_expected"] is True
    assert observed["already_expected"] == ["SB201"]
    # The superseded import no longer counts as planned, so it is recommended again.
    assert observed["recommendations"] == [
        ("SA101", _name("SA101", language), 3),
        ("SC201", _name("SC201", language), 3),
        ("SD301", _name("SD301", language), 3),
        ("SE401", _name("SE401", language), 3),
    ]
    assert observed["cards_rows"] == []


@LANGUAGES
def test_other_cohort_and_other_branch_sections_are_never_shown(world, monkeypatch, language):
    """The cohort rule is applied twice: by the reader, then again by the screen.

    The screen's copy removes exactly what the reader already removed, so through
    this page it is unobservable. Its own rule is pinned on the pure function.
    """
    _link(world, "SA101-M1", PLAN)
    _link(world, "SA101-F1", PLAN)
    _link(world, "SB201-YM1", PLAN)
    _link(world, "SA101-F1", REGISTRAR)

    observed = _observe(*_home(monkeypatch, language))

    assert observed["panels"] == [
        {
            "kind": "expected",
            "term": (YEAR, TERM, False),
            "days": ["SUN", "TUE"],
            "meetings": _sa101_meetings(language, "planned"),
            "unscheduled": [],
        }
    ]
    # The F1 registrar row is another cohort's: it is not this student's registration.
    assert observed["has_registered"] is False
    assert observed["cards_rows"] == []
    assert observed["already_expected"] == ["SA101"]
    assert observed["expected_not_registered"] == []


@LANGUAGES
def test_a_student_with_no_recorded_cohort_sees_both_cohorts_but_not_the_other_branch(
    world, monkeypatch, language
):
    Student.objects.filter(student_id=SID).update(section="")
    _link(world, "SA101-M1", PLAN)
    _link(world, "SA101-F1", PLAN)
    _link(world, "SB201-YM1", PLAN)

    observed = _observe(*_home(monkeypatch, language))

    assert observed["panels"] == [
        {
            "kind": "expected",
            "term": (YEAR, TERM, False),
            "days": ["SUN", "TUE"],
            "meetings": [
                (
                    "SA101",
                    _name("SA101", language),
                    "F1",
                    "SUN",
                    "08:00",
                    "09:15",
                    "F9",
                    "Staff",
                    "planned",
                ),
                _sa101_meetings(language, "planned")[0],
                _sa101_meetings(language, "planned")[1],
            ],
            "unscheduled": [],
        }
    ]
    assert observed["already_expected"] == ["SA101"]


@LANGUAGES
def test_the_single_other_published_term_is_drawn_when_the_configured_term_is_empty(
    world, monkeypatch, language
):
    _link(world, "SA101-M1", REGISTRAR, year="1447", term="2")
    _link(world, "SB201-M1", PLAN, year="1447", term="2")

    observed = _observe(*_home(monkeypatch, language))

    assert observed == {
        "timetable_term": ("1447", "2", True),
        "panels": [
            {
                "kind": "registered",
                "term": ("1447", "2", True),
                "days": ["SUN", "TUE"],
                "meetings": _sa101_meetings(language, "current"),
                "unscheduled": [],
            },
            {
                "kind": "expected",
                "term": ("1447", "2", True),
                "days": ["MON"],
                "meetings": [
                    (
                        "SB201",
                        _name("SB201", language),
                        "M1",
                        "MON",
                        "11:00",
                        "12:15",
                        "B2",
                        "Staff",
                        "planned",
                    )
                ],
                "unscheduled": [],
            },
        ],
        # The fallback feeds the panels ONLY. Every configured-term fact stays empty.
        "has_registered": False,
        "has_expected": False,
        "expected_not_registered": [],
        "registered_not_expected": [],
        "recommendations": [
            ("SA101", _name("SA101", language), 3),
            ("SB201", _name("SB201", language), 3),
            ("SC201", _name("SC201", language), 3),
            ("SD301", _name("SD301", language), 3),
            ("SE401", _name("SE401", language), 3),
        ],
        "already_current": [],
        "already_expected": [],
        "cards_term": (YEAR, TERM, language == "ar"),
        "cards_rows": [],
    }


def test_the_fallback_applies_the_cohort_rule_too(world, monkeypatch):
    _link(world, "SA101-M1", REGISTRAR, year="1447", term="2")
    _link(world, "SA101-F1", REGISTRAR, year="1447", term="2")
    _link(world, "SB201-YM1", REGISTRAR, year="1447", term="2")

    observed = _observe(*_home(monkeypatch, "en"))

    assert observed["timetable_term"] == ("1447", "2", True)
    assert [m[2] for m in observed["panels"][0]["meetings"]] == ["M1", "M1"]


def test_the_fallback_is_not_used_while_two_terms_are_loaded(world, monkeypatch):
    _link(world, "SA101-M1", REGISTRAR, year="1447", term="2")
    _link(world, "SB201-M1", REGISTRAR, year="1447", term="1")

    observed = _observe(*_home(monkeypatch, "en"))

    assert observed["timetable_term"] == (YEAR, TERM, False)
    assert observed["panels"] == []


@pytest.fixture
def failing_term_read(monkeypatch):
    """Make every ``Snapshot.ANY`` timetable read raise, as a database error would.

    Patched where ``get_student_term_baseline`` looks it up at call time, so the
    failure reaches the read whichever module the caller imported it into.
    """
    real = student_sections.select_snapshot_rows

    def failing(rows, snapshot):
        if snapshot is Snapshot.ANY:
            raise RuntimeError("timetable store unavailable")
        return real(rows, snapshot)

    monkeypatch.setattr(student_sections, "select_snapshot_rows", failing)


def test_a_failed_read_of_the_configured_term_degrades_to_no_timetable(
    world, monkeypatch, failing_term_read, caplog
):
    _link(world, "SA101-M1", PLAN)
    _link(world, "SB201-M1", REGISTRAR)

    with caplog.at_level(logging.ERROR, logger="core.student_auth_views"):
        observed = _observe(*_home(monkeypatch, "en"))

    assert observed["timetable_term"] == (YEAR, TERM, False)
    assert observed["panels"] == []
    assert observed["has_registered"] is False
    assert observed["has_expected"] is False
    assert observed["cards_rows"] == []
    messages = [record.getMessage() for record in caplog.records]
    assert f"student timetable load failed for {SID}" in messages
    assert not any("fallback" in message for message in messages)


def test_a_failed_read_of_the_fallback_term_keeps_its_label_and_draws_nothing(
    world, monkeypatch, failing_term_read, caplog
):
    _link(world, "SA101-M1", REGISTRAR, year="1447", term="2")

    with caplog.at_level(logging.ERROR, logger="core.student_auth_views"):
        observed = _observe(*_home(monkeypatch, "en"))

    assert observed["timetable_term"] == ("1447", "2", True)
    assert observed["panels"] == []
    messages = [record.getMessage() for record in caplog.records]
    assert f"student timetable load failed for {SID}" in messages
    assert f"student fallback timetable failed for {SID}" in messages


def test_an_empty_configured_term_with_a_failed_fallback_read_draws_nothing(
    world, monkeypatch, caplog
):
    """The configured read succeeds and is empty; only the fallback's read fails."""
    _link(world, "SA101-M1", REGISTRAR, year="1447", term="2")
    real = student_sections.select_snapshot_rows

    def failing_when_rows_exist(rows, snapshot):
        if snapshot is Snapshot.ANY and rows:
            raise RuntimeError("timetable store unavailable")
        return real(rows, snapshot)

    monkeypatch.setattr(student_sections, "select_snapshot_rows", failing_when_rows_exist)

    with caplog.at_level(logging.ERROR, logger="core.student_auth_views"):
        observed = _observe(*_home(monkeypatch, "en"))

    assert observed["timetable_term"] == ("1447", "2", True)
    assert observed["panels"] == []
    assert observed["has_registered"] is False
    assert observed["cards_rows"] == []
    messages = [record.getMessage() for record in caplog.records]
    assert f"student timetable load failed for {SID}" not in messages
    assert f"student fallback timetable failed for {SID}" in messages
