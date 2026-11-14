"""Who cannot register for a course, and the sections that take its times.

The pure rule (``timetable_registration_blockers``) on hand-made seatings, then
the quality read that serves it to the workspace (``?blockers=1``) on a real
scenario seated by the real evaluator. Without the parameter the quality read
is exactly what it was (``test_tw_scenario_seating`` holds its recorded oracle).
"""

from __future__ import annotations

import json
import logging

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TermSection,
    TimetableScenario,
)
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.timetable_assignment_models import (
    SectionMeeting,
    SectionState,
    StudentAssignmentState,
    UnresolvedReason,
)
from core.services.timetable_registration_blockers import registration_blockers
from core.services.timetable_scenario_quality import build_scenario_quality

DAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4}


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _section(course: str, label: str, *meetings: tuple[str, str, str]) -> SectionState:
    return SectionState(
        section_id=f"{course}_{label}",
        course_code=course,
        meetings=[
            SectionMeeting(day=DAYS[day], start_min=_minutes(start), end_min=_minutes(end))
            for day, start, end in meetings
        ],
        max_capacity=40,
        reserve_capacity=2,
    )


def _state(student: str, seated: tuple[str, ...], stuck: dict[str, str]) -> StudentAssignmentState:
    state = StudentAssignmentState(student_id=student)
    state.section_ids = set(seated)
    state.unresolved_courses = {
        course: UnresolvedReason(course, reason) for course, reason in stuck.items()
    }
    return state


# AI201's two sections both sit at 13:00; DS331's one section meets SUN, TUE and
# THU at 13:00, so it takes S1 (TUE+THU) and S2 (SUN) alike. MATH243 S2 meets
# only THU at 13:00: it takes S1 and leaves S2.
AI201_S1 = _section("AI201", "S1", ("TUE", "13:00", "14:15"), ("THU", "13:00", "14:15"))
AI201_S2 = _section("AI201", "S2", ("SUN", "13:00", "14:15"), ("WED", "13:00", "14:15"))
DS331_S1 = _section(
    "DS331", "S1", ("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15"), ("THU", "13:00", "14:40")
)
MATH243_S2 = _section("MATH243", "S2", ("MON", "13:00", "14:40"), ("THU", "13:00", "14:40"))
SECTIONS = [AI201_S1, AI201_S2, DS331_S1, MATH243_S2]


# ── the rule ─────────────────────────────────────────────────────────────────


def test_the_section_that_takes_every_section_of_a_course_is_named_first():
    states = {
        "1": _state("1", ("DS331_S1",), {"AI201": "all_clash"}),
        "2": _state("2", ("DS331_S1", "MATH243_S2"), {"AI201": "all_clash"}),
        "3": _state("3", ("DS331_S1",), {"AI201": "all_clash"}),
    }
    out = registration_blockers(states, SECTIONS)
    assert out["assessed"] is True
    assert (out["students"], out["requests"]) == (3, 3)
    (course,) = out["courses"]
    assert (course["course_key"], course["students"], course["reasons"]) == (
        "AI201",
        3,
        {"all_clash": 3},
    )
    assert [
        (b["course_code"], b["section"], b["students"], b["every_section"])
        for b in course["blocked_by"]
    ] == [("DS331", "S1", 3, 3), ("MATH243", "S2", 1, 0)]
    assert course["blocked_by_more"] == 0


def test_a_blocker_of_more_students_on_one_section_ranks_below_one_that_takes_every_section():
    states = {
        "1": _state("1", ("MATH243_S2",), {"AI201": "mixed_blockers"}),
        "2": _state("2", ("MATH243_S2",), {"AI201": "mixed_blockers"}),
        "3": _state("3", ("DS331_S1",), {"AI201": "all_clash"}),
    }
    blockers = registration_blockers(states, SECTIONS)["courses"][0]["blocked_by"]
    assert [(b["course_code"], b["students"], b["every_section"]) for b in blockers] == [
        ("DS331", 1, 1),
        ("MATH243", 2, 0),
    ]


def test_a_student_with_a_full_section_in_the_way_never_makes_a_blocker_decisive():
    # DS331 takes both AI201 sections, but for a mixed student some section is
    # (also) full: moving DS331 alone gives them no seat.
    states = {"1": _state("1", ("DS331_S1",), {"AI201": "mixed_blockers"})}
    (blocker,) = registration_blockers(states, SECTIONS)["courses"][0]["blocked_by"]
    assert (blocker["course_code"], blocker["students"], blocker["every_section"]) == (
        "DS331",
        1,
        0,
    )


def test_a_full_course_and_one_with_no_section_name_no_blocker():
    states = {
        "1": _state("1", ("DS331_S1",), {"AI201": "full", "AI999": "no_sections"}),
        "2": _state("2", (), {"AI201": "full"}),
    }
    out = registration_blockers(states, SECTIONS)
    assert (out["students"], out["requests"]) == (2, 3)
    rows = {c["course_key"]: c for c in out["courses"]}
    assert rows["AI201"]["reasons"] == {"full": 2}
    assert rows["AI201"]["blocked_by"] == []
    assert rows["AI999"]["reasons"] == {"no_sections": 1}
    assert rows["AI999"]["blocked_by"] == []
    assert rows["AI999"]["placements"] == []
    # The most stuck students first.
    assert [c["course_key"] for c in out["courses"]] == ["AI201", "AI999"]


def test_courses_with_as_many_students_come_by_code():
    states = {
        "1": _state("1", (), {"MATH243": "full", "AI201": "full"}),
    }
    out = registration_blockers(states, SECTIONS)
    assert [c["course_key"] for c in out["courses"]] == ["AI201", "MATH243"]
    assert (out["students"], out["requests"]) == (1, 2)


def test_meetings_that_only_touch_or_sit_on_other_days_block_nothing():
    a = _section("AI101", "S1", ("SUN", "09:00", "10:15"))
    touching = _section("CS101", "S1", ("SUN", "10:15", "11:30"))
    other_day = _section("CS102", "S1", ("MON", "09:00", "10:15"))
    states = {"1": _state("1", ("CS101_S1", "CS102_S1"), {"AI101": "all_clash"})}
    course = registration_blockers(states, [a, touching, other_day])["courses"][0]
    assert course["blocked_by"] == []


def test_a_section_of_the_course_itself_is_never_its_blocker():
    states = {"1": _state("1", ("AI201_S1",), {"AI201": "all_clash"})}
    course = registration_blockers(states, SECTIONS)["courses"][0]
    assert course["blocked_by"] == []


def test_a_seated_section_the_seating_does_not_know_is_ignored():
    states = {"1": _state("1", ("GONE_S9", "DS331_S1"), {"AI201": "all_clash"})}
    course = registration_blockers(states, SECTIONS)["courses"][0]
    assert [b["course_code"] for b in course["blocked_by"]] == ["DS331"]


def test_blockers_beyond_the_limit_are_counted_not_listed():
    states = {"1": _state("1", ("DS331_S1", "MATH243_S2"), {"AI201": "all_clash"})}
    course = registration_blockers(states, SECTIONS, blocker_limit=1)["courses"][0]
    assert [b["course_code"] for b in course["blocked_by"]] == ["DS331"]
    assert course["blocked_by_more"] == 1


def test_a_course_and_its_blockers_name_their_placements_and_boards():
    key = "AI201::FUNDAMENTALS_OF_AI"
    s1 = _section(key, "S1", ("TUE", "13:00", "14:15"))
    s2 = _section(key, "S2", ("SUN", "13:00", "14:15"))
    ds = _section("DS331::DATA_MINING", "S1", ("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15"))
    placements = [
        {"id": 12, "board_id": 8, "course_key": key, "section": "S2"},
        {"id": 11, "board_id": 8, "course_key": key, "section": "S1"},
        {"id": 31, "board_id": 6, "course_key": "DS331::DATA_MINING", "section": "S1"},
        {"id": 30, "board_id": 6, "course_key": "DS331::DATA_MINING", "section": "S1"},
        {"id": 99, "board_id": 6, "course_key": "DS331::DATA_MINING", "section": "S2"},
    ]
    states = {"1": _state("1", ("DS331::DATA_MINING_S1",), {key: "all_clash"})}
    (course,) = registration_blockers(states, [s1, s2, ds], placements)["courses"]
    assert course["course_code"] == "AI201"
    assert course["placements"] == [{"id": 11, "board_id": 8}, {"id": 12, "board_id": 8}]
    (blocker,) = course["blocked_by"]
    assert (blocker["course_key"], blocker["course_code"], blocker["section"]) == (
        "DS331::DATA_MINING",
        "DS331",
        "S1",
    )
    assert blocker["placements"] == [{"id": 30, "board_id": 6}, {"id": 31, "board_id": 6}]
    assert blocker["every_section"] == 1


def test_nobody_stuck_is_an_empty_answer():
    states = {"1": _state("1", ("DS331_S1",), {})}
    assert registration_blockers(states, SECTIONS) == {
        "assessed": True,
        "students": 0,
        "requests": 0,
        "courses": [],
    }


# ── the quality read ─────────────────────────────────────────────────────────


@pytest.fixture
def scenario():
    return TimetableScenario.objects.create(
        academic_year="1448", term="2", gender="M", programs=["DS"], name="blockers"
    )


def _course(scenario, board, code, sections: dict[str, list[tuple[str, str, str]]]):
    """One budget for ``code``, and each section's meetings on ``board``."""
    ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_code=code,
        course_key=code,
        planned_sections=len(sections),
        credit_hours=3,
        max_per_section=40,
        total_demand=1,
    )
    ids = []
    for label, meetings in sections.items():
        ts = TermSection.objects.create(
            scenario=scenario, course_code=code, course_key=code, section=label
        )
        for day, start, end in meetings:
            ids.append(
                SectionPlacement.objects.create(
                    board=board, term_section=ts, day=day, start_time=start, end_time=end, room="R1"
                ).id
            )
    return sorted(ids)


def _stuck_on_ds331(scenario):
    """A DS student who wants DS331 (one section, SUN/TUE/THU 13:00) and AI201
    (two sections, both at 13:00): DS331 is seated first, and every AI201
    section then clashes with it."""
    term6 = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 6", nominal_term=6, program="DS"
    )
    term8 = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 8", nominal_term=8, program="DS"
    )
    ds331 = _course(
        scenario,
        term6,
        "DS331",
        {"S1": [("SUN", "13:00", "14:15"), ("TUE", "13:00", "14:15"), ("THU", "13:00", "14:15")]},
    )
    ai201 = _course(
        scenario,
        term8,
        "AI201",
        {
            "S1": [("TUE", "13:00", "14:15"), ("THU", "13:00", "14:15")],
            "S2": [("SUN", "13:00", "14:15"), ("WED", "13:00", "14:15")],
        },
    )
    Student.objects.create(student_id=7001, registration_no="7001", name="S", program="DS")
    for code in ("DS331", "AI201"):
        ScenarioStudentCourseRequest.objects.create(
            scenario=scenario, student_id=7001, course_key=code, course_code=code
        )
    return term6, term8, ds331, ai201


@pytest.mark.django_db
def test_quality_names_who_cannot_register_and_the_section_that_blocks_them(scenario):
    term6, term8, ds331, ai201 = _stuck_on_ds331(scenario)

    quality = build_scenario_quality(scenario.id, with_blockers=True)

    assert quality["registration_blockers"] == {
        "assessed": True,
        "students": 1,
        "requests": 1,
        "courses": [
            {
                "course_key": "AI201",
                "course_code": "AI201",
                "students": 1,
                "reasons": {"all_clash": 1},
                "placements": [{"id": i, "board_id": term8.id} for i in ai201],
                "blocked_by": [
                    {
                        "course_key": "DS331",
                        "course_code": "DS331",
                        "section": "S1",
                        "students": 1,
                        "every_section": 1,
                        "placements": [{"id": i, "board_id": term6.id} for i in ds331],
                    }
                ],
                "blocked_by_more": 0,
            }
        ],
    }
    # The same seating answers the quality: one request unseated.
    assert quality["seats"]["total"] == 1


@pytest.mark.django_db
def test_the_blockers_come_only_when_asked_after_the_quality_and_before_the_lens(scenario):
    _stuck_on_ds331(scenario)
    plain = build_scenario_quality(scenario.id)
    asked = build_scenario_quality(scenario.id, with_blockers=True)
    assert "registration_blockers" not in plain
    assert list(asked) == [*plain, "registration_blockers"]
    rest = {key: value for key, value in asked.items() if key != "registration_blockers"}
    assert json.dumps(rest) == json.dumps(plain)

    both = build_scenario_quality(scenario.id, with_lens=True, with_blockers=True)
    assert list(both)[-2:] == ["registration_blockers", "plan_lens"]
    assert both["registration_blockers"] == asked["registration_blockers"]


@pytest.mark.django_db
def test_asking_for_the_blockers_costs_one_read_and_no_second_seating(scenario, monkeypatch):
    _stuck_on_ds331(scenario)
    import core.services.timetable_scenario_quality as quality_module

    seatings = []
    real = quality_module.evaluate_generated_timetable_candidate

    def counted(*args, **kwargs):
        seatings.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(quality_module, "evaluate_generated_timetable_candidate", counted)
    with CaptureQueriesContext(connection) as without:
        build_scenario_quality(scenario.id)
    with CaptureQueriesContext(connection) as asked:
        build_scenario_quality(scenario.id, with_blockers=True)
    assert seatings == [1, 1]
    assert len(asked.captured_queries) == len(without.captured_queries) + 1
    assert 'FROM "section_placements"' in asked.captured_queries[-1]["sql"]


@pytest.mark.django_db
def test_nobody_to_seat_leaves_the_blockers_unknown(scenario):
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Term 8", nominal_term=8, program="DS"
    )
    _course(scenario, board, "AI201", {"S1": [("TUE", "13:00", "14:15")]})
    blockers = build_scenario_quality(scenario.id, with_blockers=True)["registration_blockers"]
    assert blockers == {
        "assessed": False,
        "students": None,
        "requests": None,
        "courses": [],
        "reason": "no_student_demand",
    }


@pytest.mark.django_db
def test_a_failed_seating_leaves_the_blockers_unknown_with_the_rest(scenario, monkeypatch):
    _stuck_on_ds331(scenario)
    import core.services.timetable_scenario_quality as quality_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("seating failed")

    monkeypatch.setattr(quality_module, "evaluate_generated_timetable_candidate", boom)
    quality = build_scenario_quality(scenario.id, with_blockers=True)
    assert quality["seats"]["reason"] == "assessment_failed"
    assert quality["registration_blockers"] == {
        "assessed": False,
        "students": None,
        "requests": None,
        "courses": [],
        "reason": "assessment_failed",
    }


@pytest.mark.django_db
def test_a_failure_naming_the_blockers_never_costs_the_quality(scenario, monkeypatch, caplog):
    _stuck_on_ds331(scenario)
    import core.services.timetable_registration_blockers as module

    def boom(*_args, **_kwargs):
        raise RuntimeError("naming failed")

    monkeypatch.setattr(module, "registration_blockers", boom)
    with caplog.at_level(logging.ERROR, logger="core.services.timetable_scenario_quality"):
        quality = build_scenario_quality(scenario.id, with_blockers=True)
    assert quality["seats"]["assessed"] is True
    assert quality["seats"]["total"] == 1
    assert quality["registration_blockers"] == {
        "assessed": False,
        "students": None,
        "requests": None,
        "courses": [],
        "reason": "assessment_failed",
    }
    assert any("registration blockers" in record.getMessage() for record in caplog.records)


@pytest.fixture
def http():
    user = User.objects.create_user(username="blockers-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_endpoint_adds_the_blockers_only_for_exactly_blockers_1(scenario, http):
    _stuck_on_ds331(scenario)
    url = f"/ops/tw/scenarios/{scenario.id}/quality/"
    plain = http.get(url).json()
    assert "registration_blockers" not in plain
    for other in ("?blockers=0", "?blockers=true", "?blockers="):
        assert "registration_blockers" not in http.get(url + other).json(), other
    asked = http.get(url + "?blockers=1").json()
    assert asked["ok"] is True
    assert asked["registration_blockers"]["courses"][0]["blocked_by"][0]["course_code"] == "DS331"
    both = http.get(url + "?blockers=1&lens=1").json()
    assert list(both)[-2:] == ["registration_blockers", "plan_lens"]
