"""``TIMETABLE_ONLINE_EVENING_WINDOWS``: online courses meet only in Online 1, 2 and 3.

Owner request 2026-10-26: online courses go only in the evening windows, Online 1
(15:50-17:30), Online 2 (17:40-19:20) and Online 3 (19:30-21:10), and nothing
else changes. It ships behind a flag that defaults OFF:

- **Flag OFF = baseline.** On a corpus WITH online courses, the greedy builds,
  the two CP-SAT models, the annealing / load-balancer relocate domains, the
  cap-repair slot domains and the one-teacher guard's options all reproduce a
  golden recorded on the UNCHANGED engine (``17ef51d``).
- **Flag ON without an online course = baseline.** On the corpus without online
  courses the same captures (and the day-spread golden's greedy builds and
  CP-SAT models) are unchanged.
- **Flag ON.** Every automatic placer gives an online section's meetings only
  the windows (whole 100-minute windows, any weekday) and never a daytime slot;
  other sections never get a window.

The golden lives in ``tests/fixtures/online_evening_windows_flag_off_golden.json``.
To re-check it, check out ``17ef51d`` plus the fixture commit and run::

    ONLINE_EVENING_GOLDEN_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/test_online_evening_windows.py -k record

then diff ``<file>`` against the fixture. Never re-record it from a branch that
changes the planner.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

from core.models import (
    CourseInstructor,
    DeliveryBoard,
    Instructor,
    ProgrammeRequirement,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    Student,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from tests._online_windows_fixture import (
    GREEDY_CASES,
    ONLINE_FLAGS,
    ONLINE_KEYS,
    build_online_corpus,
    capture_domains,
    capture_guard_options,
    capture_relocate_domains,
    capture_repair_domains,
    run_greedy_case,
)
from tests.test_one_teacher_flag_parity import build_corpus
from tests.test_section_day_spread import GREEDY_CASES as DAY_SPREAD_GREEDY_CASES
from tests.test_section_day_spread import _model_fingerprint, make_capture_models

GOLDEN = Path(__file__).parent / "fixtures" / "online_evening_windows_flag_off_golden.json"
RECORD_ENV = "ONLINE_EVENING_GOLDEN_RECORD"

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def flag_off(settings):
    for name, value in ONLINE_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = False
    return settings


@pytest.fixture
def capture_models(monkeypatch):
    return make_capture_models(monkeypatch)


def _case_key(strategy: str, seed: int | None) -> str:
    return f"{strategy}|{'unseeded' if seed is None else seed}"


# ── recorder (skipped unless asked) ───────────────────────────────────


def _merge_into_record(key: str, value) -> None:
    target = Path(os.environ[RECORD_ENV])
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    data[key] = value
    target.write_text(
        json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_record_online_greedy_golden(flag_off, strategy, seed) -> None:
    _merge_into_record(
        f"online:greedy:{_case_key(strategy, seed)}",
        run_greedy_case(build_online_corpus, strategy, seed),
    )


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_online_cpsat_golden(flag_off, capture_models) -> None:
    for name, model in capture_models(build_online_corpus(11)).items():
        _merge_into_record(f"online:cpsat:{name}", _model_fingerprint(model))


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_online_domains_golden(flag_off, monkeypatch) -> None:
    _merge_into_record("online:domains", capture_domains(monkeypatch, build_online_corpus(11)))


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_plain_domains_golden(flag_off, monkeypatch) -> None:
    _merge_into_record("plain:domains", capture_domains(monkeypatch, build_corpus(11)))


# ── flag OFF = the baseline (Golden A) ─────────────────────────────────


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_flag_off_online_greedy_build_matches_the_recorded_golden(flag_off, strategy, seed) -> None:
    produced = run_greedy_case(build_online_corpus, strategy, seed)

    assert produced == _golden()[f"online:greedy:{_case_key(strategy, seed)}"]


@pytest.mark.django_db
def test_flag_off_online_cpsat_models_match_the_recorded_golden(flag_off, capture_models) -> None:
    produced = {
        f"online:cpsat:{name}": _model_fingerprint(model)
        for name, model in capture_models(build_online_corpus(11)).items()
    }

    golden = _golden()
    assert produced == {key: golden[key] for key in produced}


@pytest.mark.django_db
def test_flag_off_online_domains_match_the_recorded_golden(flag_off, monkeypatch) -> None:
    """Annealing / load-balancer relocate domains, cap-repair slot domains and the
    one-teacher guard's options, on a corpus with online courses."""
    produced = capture_domains(monkeypatch, build_online_corpus(11))

    assert produced == _golden()["online:domains"]


# ── flag ON without an online course = the baseline (Golden B) ─────────


@pytest.fixture
def flag_on(flag_off):
    flag_off.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    return flag_off


@pytest.fixture
def flag_on_spread_off(settings):
    """The day-spread golden's flags (recorded before this flag existed), plus
    this flag ON."""
    from tests.test_section_day_spread import PRODUCTION_FLAGS

    for name, value in PRODUCTION_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECTION_DAY_SPREAD = False
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    return settings


@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), DAY_SPREAD_GREEDY_CASES)
def test_flag_on_without_an_online_course_greedy_build_matches_the_day_spread_golden(
    flag_on_spread_off, strategy, seed
) -> None:
    from tests.test_section_day_spread import _golden as day_spread_golden
    from tests.test_section_day_spread import run_greedy_case as plain_greedy_case

    produced = plain_greedy_case(strategy, seed)

    assert produced == day_spread_golden()[f"greedy:{_case_key(strategy, seed)}"]


@pytest.mark.django_db
def test_flag_on_without_an_online_course_cpsat_models_match_the_day_spread_golden(
    flag_on_spread_off, capture_models
) -> None:
    from tests.test_section_day_spread import _golden as day_spread_golden

    produced = {
        f"cpsat:{name}": _model_fingerprint(model)
        for name, model in capture_models(build_corpus(11)).items()
    }

    golden = day_spread_golden()
    assert produced == {key: golden[key] for key in produced}


@pytest.mark.django_db
def test_flag_on_without_an_online_course_domains_match_the_recorded_golden(
    flag_on, monkeypatch
) -> None:
    produced = capture_domains(monkeypatch, build_corpus(11))

    assert produced == _golden()["plain:domains"]


# ── the helper ────────────────────────────────────────────────────────

WINDOWS = [
    ("Online 1", "15:50", "17:30"),
    ("Online 2", "17:40", "19:20"),
    ("Online 3", "19:30", "21:10"),
]
WINDOW_CELLS = {(start, end) for _label, start, end in WINDOWS}
DAYS = ("SUN", "MON", "TUE", "WED", "THU")


def _windows(slot_config=None, lab_slot_config=None, **kwargs):
    from core.services.timetable_online_windows import active_online_windows

    return active_online_windows(slot_config or [], lab_slot_config or [], **kwargs)


def _labs_without_windows() -> list[dict]:
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS

    return [dict(slot) for slot in DEFAULT_LAB_SLOTS if not slot.get("online_only")]


def _m(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def test_the_default_columns_give_the_three_windows_in_order(flag_on) -> None:
    assert _windows() == [
        {"label": label, "start": start, "end": end, "slot_idx": index}
        for index, (label, start, end) in enumerate(WINDOWS)
    ]


def test_the_flag_off_gives_no_window(flag_off) -> None:
    assert _windows() == []


def test_columns_without_an_online_window_give_none_and_say_so(flag_on, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="core.services.timetable_online_windows"):
        assert _windows([], _labs_without_windows()) == []
        assert caplog.records == []  # nobody named: no warning
        assert _windows([], _labs_without_windows(), warn_for="board 'Term 4'") == []
    assert len(caplog.records) == 1
    assert "board 'Term 4'" in caplog.records[0].getMessage()


def test_only_drawable_online_only_columns_are_windows(flag_on) -> None:
    labs = _labs_without_windows()
    online = {"label": "Online 1", "start": "15:50", "end": "17:30", "online_only": True}
    # A policy column is never a window.
    assert _windows([], [*labs, {**online, "policy_only": True}]) == []
    # 70 minutes in the lab list: a 70-minute meeting is drawn as a lecture, so
    # the grid could never show it in this column.
    assert _windows([], [*labs, {**online, "end": "17:00"}]) == []
    # Shadowed: an earlier lab column with the same start is where the grid draws.
    assert _windows([], [*labs, {"label": "X", "start": "15:50", "end": "17:30"}, online]) == []
    # An online-only lecture column is drawn as itself, so it is a window (the
    # readiness check counts it too: test_a_lecture_column_flagged_online_only_is_a_window_too).
    lecture = {"label": "Eve", "start": "18:00", "end": "19:15", "online_only": True}
    assert _windows([lecture], labs) == [
        {"label": "Eve", "start": "18:00", "end": "19:15", "slot_idx": 0}
    ]
    # Repeated windows are offered once; stored times are read as HH:MM.
    assert _windows([], [*labs, online, dict(online), {**online, "start": "15:50:00"}]) == [
        {"label": "Online 1", "start": "15:50", "end": "17:30", "slot_idx": 0}
    ]


def test_window_options_cover_distinct_days_and_whole_windows(flag_on) -> None:
    from core.services.timetable_online_windows import online_window_meeting_options

    windows = _windows()
    for count, expected in ((1, 15), (2, 90), (3, 270)):
        options = online_window_meeting_options(count, windows, set())
        assert len(options) == expected
        assert len({json.dumps(option, sort_keys=True) for option in options}) == expected
        for option in options:
            assert len(option) == count
            assert len({meeting["day"] for meeting in option}) == count
            for meeting in option:
                assert set(meeting) == {"day", "start", "end", "slot_idx"}
                window = windows[meeting["slot_idx"]]
                assert (meeting["start"], meeting["end"]) == (window["start"], window["end"])
    # Like the daytime generator: non-adjacent days first, then the windows in order.
    first = online_window_meeting_options(2, windows, set())[:3]
    assert [[(m["day"], m["start"]) for m in option] for option in first] == [
        [("SUN", "15:50"), ("TUE", "15:50")],
        [("SUN", "15:50"), ("TUE", "17:40")],
        [("SUN", "15:50"), ("TUE", "19:30")],
    ]
    again = online_window_meeting_options(2, windows, set())
    assert again == online_window_meeting_options(2, windows, set())
    assert online_window_meeting_options(2, [], set()) == []


def test_blocked_window_cells_are_never_offered(flag_on) -> None:
    from core.services.timetable_online_windows import (
        online_window_cells,
        online_window_meeting_options,
        online_window_relocate_domain,
    )

    windows = _windows()
    blocked = {("SUN", "15:50"), ("THU", "19:30")}
    options = online_window_meeting_options(1, windows, blocked)
    assert len(options) == 13
    assert all((o[0]["day"], o[0]["start"]) not in blocked for o in options)
    assert all(
        (m["day"], m["start"]) not in blocked
        for option in online_window_meeting_options(2, windows, blocked)
        for m in option
    )
    cells = online_window_cells(windows, blocked)
    assert len(cells) == 13 and all((c["day"], c["start"]) not in blocked for c in cells)
    assert [c["day"] for c in cells[:2]] == ["SUN", "SUN"]  # day by day
    domain = online_window_relocate_domain(windows, blocked, "15:50", "17:30")
    assert [(c["day"], c["start"]) for c in domain] == [(c["day"], c["start"]) for c in cells]


def test_a_mover_offers_only_windows_of_the_meetings_own_length(flag_on) -> None:
    from core.services.timetable_online_windows import online_window_relocate_domain
    from core.services.timetable_workspace import _time_mask

    windows = _windows()
    domain = online_window_relocate_domain(windows, set(), "09:00", "10:40")
    assert len(domain) == 15
    for cell in domain:
        assert (cell["start"], cell["end"]) in WINDOW_CELLS
        assert cell["mask"] == _time_mask(cell["day"], cell["start"], cell["end"])
    # A 75-minute meeting (placed in the day before the flag) has no window of
    # its length: a mover leaves it where it is rather than change its length.
    assert online_window_relocate_domain(windows, set(), "09:00", "10:15") == []


# ── shared helpers for the flag-ON tests ──────────────────────────────


def _rows(scenario):
    return list(
        SectionPlacement.objects.filter(board__scenario=scenario)
        .select_related("board", "term_section")
        .order_by("board__label", "term_section__course_key", "term_section__section", "day")
    )


def _online_for_board(placement) -> bool:
    from core.services.timetable_online import OnlineCourseLookup

    return OnlineCourseLookup().is_online_course_for_board(
        placement.board, placement.term_section.course_code
    )


def _assert_windows_only_for_online(scenario) -> None:
    """Every online meeting is a whole window the grid draws as that window, with
    no room; no other meeting is drawn in a window."""
    from core.services.timetable_slot_config import (
        effective_lab_slots,
        effective_lecture_slots,
        online_window,
    )

    lecture, lab = effective_lecture_slots(scenario), effective_lab_slots(scenario)
    rows = _rows(scenario)
    assert rows
    for row in rows:
        window = online_window(row.day, row.start_time, row.end_time, lecture, lab)
        where = (row.board.label, row.term_section.course_key, row.day, row.start_time)
        if _online_for_board(row):
            assert (row.start_time, row.end_time) in WINDOW_CELLS, where
            assert window is not None, where
            assert row.room == "", where
        else:
            assert window is None, where
            assert (row.start_time, row.end_time) not in WINDOW_CELLS, where


def _online_sections_placed(scenario) -> dict[str, int]:
    return {
        key: SectionPlacement.objects.filter(board__scenario=scenario, term_section__course_key=key)
        .values("term_section_id")
        .distinct()
        .count()
        for key in sorted(ONLINE_KEYS)
    }


PLANNED_ONLINE = {
    planner_course_key("CYS300", "CYBER ETHICS"): 1,
    planner_course_key("GS101", "ISLAMIC CULTURE"): 2,
    planner_course_key("GSE102", "ARABIC WRITING"): 2,
}


def _times(scenario, code: str) -> list[tuple[str, str, str, str]]:
    return sorted(
        (r.term_section.section, r.day, r.start_time, r.end_time)
        for r in _rows(scenario)
        if r.term_section.course_code == code
    )


def _meetings_of(scenario, code: str) -> list[tuple[str, int, int]]:
    return [
        (r.day, _m(r.start_time), _m(r.end_time))
        for r in _rows(scenario)
        if r.term_section.course_code == code
    ]


# ── flag ON: the greedy placer ────────────────────────────────────────


@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_flag_on_greedy_puts_online_sections_only_in_windows(flag_on, strategy, seed) -> None:
    run_greedy_case(build_online_corpus, strategy, seed)
    scenario = TimetableScenario.objects.get(name=f"parity corpus {seed or 11}")

    _assert_windows_only_for_online(scenario)
    assert _online_sections_placed(scenario) == PLANNED_ONLINE
    # CYS300 is on campus for Term 2 (CYBER BASICS) and online for Term 4.
    cys = [r for r in _rows(scenario) if r.term_section.course_code == "CYS300"]
    in_windows = {r.board.label for r in cys if (r.start_time, r.end_time) in WINDOW_CELLS}
    assert in_windows == {"Term 4"}
    assert any(r.board.label == "Term 2" for r in cys)


def test_the_window_scorer_has_no_late_nudge_and_keeps_the_overlap_penalty(flag_on) -> None:
    from core.services.timetable_autoplace import _score_option, _time_mask

    options = [
        [{"day": "SUN", "start": w["start"], "end": w["end"], "slot_idx": w["slot_idx"]}]
        for w in _windows()
    ]

    def score(option, **kwargs):
        return _score_option(option, [], {}, set(), my_code="A", is_online=True, **kwargs)

    assert [score(o)[3] for o in options] == [10, 9, 8]  # today's nudge, where it applies
    assert [score(o, late_nudge=False)[3] for o in options] == [0, 0, 0]
    # Two online courses sharing students in the same window still cost the overlap.
    placed = [("B", _time_mask("SUN", "15:50", "17:30"))]
    matrix = {("A", "B"): 7}

    def clash(option):
        return _score_option(
            option,
            placed,
            {},
            set(),
            my_code="A",
            is_online=True,
            overlap_matrix=matrix,
            late_nudge=False,
        )[5]

    assert clash(options[0]) == 7 and clash(options[1]) == 0


def _tiny_online_scenario(
    *,
    planned: int = 1,
    extra_online: tuple = (),
    students: int = 0,
    blocked: list | None = None,
) -> TimetableScenario:
    """One board (AI, Term 4) on the default columns: GS101 (3 credits, online,
    ``planned`` sections), CS101 (3 credits, on campus) and ``extra_online``
    ``(code, credit hours, planned)`` online courses, each demanded by the same
    ``students`` students; ``blocked`` is the scenario's blocked cells."""
    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="2",
        name="online tiny",
        gender="M",
        programs=["AI"],
        slot_config=[],
        lab_slot_config=[],
        blocked_slots=blocked or [],
    )
    DeliveryBoard.objects.create(
        scenario=scenario, label="Term 4", nominal_term=4, program="AI", display_order=1
    )
    courses = [("GS101", 3, planned, True), ("CS101", 3, 1, False)]
    courses += [(code, hours, sections, True) for code, hours, sections in extra_online]
    for code, hours, sections, online in courses:
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=code,
            programme_term=4,
            credit_hours=hours,
            is_online=online,
        )
        key = planner_course_key(code, code)
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=key,
            course_code=code,
            course_name=code,
            department="AI",
            credit_hours=hours,
            planned_sections=sections,
            max_per_section=40,
            total_demand=max(students, 10),
            programme_term=4,
        )
        for index in range(students):
            student_id = 7_100_000 + index
            Student.objects.get_or_create(
                student_id=student_id,
                defaults={
                    "program": "AI",
                    "section": "M",
                    "gpa": 3.0,
                    "total_earned_credits": 60,
                },
            )
            ScenarioStudentCourseRequest.objects.create(
                scenario=scenario,
                student_id=student_id,
                course_key=key,
                course_code=code,
                course_name=code,
            )
    Room.objects.get_or_create(
        room_code="L1", defaults={"capacity": 60, "room_type": "lecture", "department": "AI"}
    )
    Room.objects.get_or_create(
        room_code="LAB1", defaults={"capacity": 60, "room_type": "lab", "department": "AI"}
    )
    return scenario


@pytest.mark.django_db
def test_flag_on_greedy_without_a_tie_breaker_takes_the_first_window_not_the_last(
    flag_on,
) -> None:
    """The late nudge is gone for window options: on an empty board every window
    ties, and the first declared one wins (with the nudge, Online 3 would)."""
    from core.services.timetable_autoplace import auto_place_board

    scenario = _tiny_online_scenario()
    auto_place_board(DeliveryBoard.objects.get(scenario=scenario).id, strategy="compact")

    assert _times(scenario, "GS101") == [
        ("S1", "SUN", "15:50", "17:30"),
        ("S1", "TUE", "15:50", "17:30"),
    ]
    _assert_windows_only_for_online(scenario)


@pytest.mark.django_db
def test_flag_on_greedy_never_falls_back_to_the_day_when_the_windows_are_full(flag_on) -> None:
    """Fifteen window cells a week, and sibling sections never overlap: eight
    two-meeting sections of one course cannot all fit. The ones that do not are
    left unplaced, each with its reason, rather than put in the day."""
    from core.services.timetable_autoplace import auto_place_board

    scenario = _tiny_online_scenario(planned=8)
    result = auto_place_board(DeliveryBoard.objects.get(scenario=scenario).id, strategy="compact")

    gs = _times(scenario, "GS101")
    placed = len({section for section, *_ in gs})
    assert placed >= 6 and placed + result["skipped"] == 8
    assert all((start, end) in WINDOW_CELLS for _s, _d, start, end in gs)
    reasons = [failure["reason"] for failure in result["room_failures"]]
    assert reasons == ["SAME_COURSE_INSTRUCTOR_CLASH"] * result["skipped"]
    _assert_windows_only_for_online(scenario)


@pytest.mark.django_db
def test_flag_on_an_unavoidable_student_clash_is_placed_in_a_window(flag_on) -> None:
    """Students cannot attend two online classes at once: with two window cells
    open (the rest blocked) and three one-meeting online courses the same students
    need, two take the free cells and the third still goes in a window (the clash
    is a cost the hard tiers count), never in the day and never unplaced."""
    from core.services.timetable_autoplace import auto_place_board

    open_cells = {("SUN", "15:50"), ("SUN", "17:40")}
    blocked = [
        {"day": day, "start": start}
        for day in DAYS
        for _label, start, _end in WINDOWS
        if (day, start) not in open_cells
    ]
    scenario = _tiny_online_scenario(
        planned=0,
        extra_online=(("GSE1", 2, 1), ("GSE2", 2, 1), ("GSE3", 2, 1)),
        students=12,
        blocked=blocked,
    )
    result = auto_place_board(DeliveryBoard.objects.get(scenario=scenario).id, strategy="compact")

    cells = [(d, s) for code in ("GSE1", "GSE2", "GSE3") for _x, d, s, _e in _times(scenario, code)]
    assert result["skipped"] == 0 and len(cells) == 3
    assert set(cells) == open_cells
    _assert_windows_only_for_online(scenario)


@pytest.mark.django_db
def test_flag_on_two_online_courses_sharing_students_take_different_windows(flag_on) -> None:
    from core.services.timetable_autoplace import auto_place_board

    scenario = _tiny_online_scenario(extra_online=(("GSE102", 2, 1),), students=12)
    auto_place_board(DeliveryBoard.objects.get(scenario=scenario).id, strategy="compact")

    gs, gse = _meetings_of(scenario, "GS101"), _meetings_of(scenario, "GSE102")
    assert len(gs) == 2 and len(gse) == 1
    for day, start, end in gs:
        assert not any(d == day and s < end and start < e for d, s, e in gse)
    _assert_windows_only_for_online(scenario)


@pytest.mark.django_db
def test_flag_on_a_locked_daytime_online_section_keeps_its_time(flag_off) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import reset_scenario

    scenario = build_online_corpus(11)
    auto_place_scenario(scenario.id, strategy="compact")
    gs_key = planner_course_key("GS101", "ISLAMIC CULTURE")
    locked = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_key=gs_key, term_section__section="S1"
    )
    before = sorted(locked.values_list("day", "start_time", "end_time"))
    assert before and all((s, e) not in WINDOW_CELLS for _d, s, e in before)  # in the day
    locked.update(is_locked=True)
    reset_scenario(scenario.id, keep_locked=True)

    flag_off.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    auto_place_scenario(scenario.id, strategy="compact")

    assert sorted(locked.values_list("day", "start_time", "end_time")) == before
    others = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_key__in=ONLINE_KEYS
    ).exclude(term_section__course_key=gs_key, term_section__section="S1")
    assert others.count() == 6  # GS101 S2, CYS300 S1 (two meetings each), GSE102 S1 and S2
    assert all((s, e) in WINDOW_CELLS for s, e in others.values_list("start_time", "end_time"))


@pytest.mark.django_db
def test_flag_on_columns_without_windows_build_exactly_as_with_the_flag_off(
    flag_off, caplog
) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_board_persistence import reset_scenario
    from tests.test_one_teacher_flag_parity import capture_board

    scenario = build_online_corpus(11)
    TimetableScenario.objects.filter(pk=scenario.pk).update(lab_slot_config=_labs_without_windows())
    auto_place_scenario(scenario.id, strategy="compact")
    reset_scenario(scenario.id, keep_locked=True)
    auto_place_scenario(scenario.id, strategy="compact")
    off = capture_board(scenario)
    reset_scenario(scenario.id, keep_locked=True)

    flag_off.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    with caplog.at_level(logging.WARNING, logger="core.services.timetable_online_windows"):
        auto_place_scenario(scenario.id, strategy="compact")

    assert capture_board(scenario) == off
    # ... and that is today's daytime placement, every online section placed.
    assert _online_sections_placed(scenario) == PLANNED_ONLINE
    online_rows = SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_key__in=ONLINE_KEYS
    )
    assert not any(
        (s, e) in WINDOW_CELLS for s, e in online_rows.values_list("start_time", "end_time")
    )
    # Once per board with an online course: Term 4 (CYS300 is on campus for Term 2).
    assert [r.getMessage().split(" has no ")[0] for r in caplog.records] == [
        f"TIMETABLE_ONLINE_EVENING_WINDOWS is on but board 'Term 4' of scenario {scenario.id}"
    ]


# ── flag ON: CP-SAT ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_flag_on_cpsat_puts_online_sections_in_windows_without_a_late_nudge(flag_on) -> None:
    """Both builders: an online section's only options are the window cells, and
    without the nudge the cheapest is the first window (with it, Online 3)."""
    from core.services.timetable_solver import solve_and_persist_board, solve_board_with_hints

    scenario = _tiny_online_scenario()
    board = DeliveryBoard.objects.get(scenario=scenario)
    hinted = solve_board_with_hints(board.id, [], time_limit_seconds=10)
    result = solve_and_persist_board(board.id, time_limit_seconds=10)

    assert result["status"] == "optimal" and hinted["status"] == "optimal"
    (gs,) = [p for p in hinted["placements"] if p["display_code"] == "GS101"]
    assert [(m["start"], m["end"]) for m in gs["meetings"]] == [("15:50", "17:30")] * 2
    placed = _times(scenario, "GS101")
    assert [(s, e) for _x, _d, s, e in placed] == [("15:50", "17:30")] * 2
    assert len({d for _x, d, _s, _e in placed}) == 2
    assert len(_times(scenario, "CS101")) == 2
    _assert_windows_only_for_online(scenario)


@pytest.mark.django_db
@pytest.mark.parametrize("builder", ("solve_board", "solve_board_with_hints"))
def test_flag_on_cpsat_option_domains_are_the_window_cells(flag_on, monkeypatch, builder) -> None:
    from core.services import timetable_solver

    seen: list = []
    real = timetable_solver._scoped_meeting_options

    def spy(section, meeting_index, ordinary):
        options = real(section, meeting_index, ordinary)
        seen.append((section["display_code"], [(o[2], o[3], o[4]) for o in options]))
        return options

    monkeypatch.setattr(timetable_solver, "_scoped_meeting_options", spy)
    board = DeliveryBoard.objects.get(scenario=_tiny_online_scenario())
    if builder == "solve_board":
        timetable_solver.solve_board(board.id, time_limit_seconds=5)
    else:
        timetable_solver.solve_board_with_hints(board.id, [], time_limit_seconds=5)

    cells = [(d, s, e) for d in DAYS for _l, s, e in WINDOWS]
    by_code: dict[str, list] = {}
    for code, options in seen:
        by_code.setdefault(code, []).append(options)
    assert by_code["GS101"] == [cells, cells]
    assert all((s, e) not in WINDOW_CELLS for opts in by_code["CS101"] for _d, s, e in opts)


# ── flag ON: annealing, the load balancer, the repairs, the guard ─────


@pytest.mark.django_db
def test_flag_on_relocate_repair_and_guard_domains(flag_on, monkeypatch) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_online_windows import online_window_meeting_options

    scenario = build_online_corpus(11)
    auto_place_scenario(scenario.id, strategy="compact")
    golden = _golden()["online:domains"]
    window_cells = [[d, s, e, i] for d in DAYS for i, (_l, s, e) in enumerate(WINDOWS)]

    # Term 2 has no online course: it is built and searched exactly as before.
    term2 = DeliveryBoard.objects.get(scenario=scenario, label="Term 2")
    assert capture_relocate_domains(monkeypatch, term2) == golden["relocate:Term 2"]
    term4 = DeliveryBoard.objects.get(scenario=scenario, label="Term 4")
    online_rows = 0
    for stage, rows in capture_relocate_domains(monkeypatch, term4).items():
        for key, row in rows.items():
            if key.split("|")[0] in ONLINE_KEYS:
                online_rows += 1
                assert row["domain"] == [window_cells] * len(row["current"]), (stage, key)
            else:
                assert all(
                    (cell[1], cell[2]) not in WINDOW_CELLS for m in row["domain"] for cell in m
                ), (stage, key)
    assert online_rows == 2 * 5  # both stages, five online sections

    for key, cells in capture_repair_domains(scenario).items():
        board, course_key = key.split("|")[:2]
        if board == "Term 4" and course_key in ONLINE_KEYS:
            assert cells == [[s, e] for _l, s, e in WINDOWS], key
        elif board == "Term 2":
            assert cells == golden["repair"][key], key
        else:
            assert all((s, e) not in WINDOW_CELLS for s, e in cells), key

    windows = _windows()
    for key, options in capture_guard_options(scenario).items():
        if key not in ONLINE_KEYS:
            assert options == golden["guard"][key], key
            continue
        hours = ScenarioSectionBudget.objects.get(scenario=scenario, course_key=key).credit_hours
        expected = sorted(
            sorted([DAYS.index(m["day"]), _m(m["start"]), _m(m["end"])] for m in option)
            for option in online_window_meeting_options({2: 1, 3: 2}[hours], windows, set())
        )
        assert sorted(options) == expected, key


@pytest.mark.django_db
def test_flag_on_guard_gives_a_course_online_on_some_boards_both_option_sets(flag_on) -> None:
    from core.services.section_teacher_assignment import _load_static
    from core.services.timetable_autoplace import generate_course_meeting_options

    scenario = TimetableScenario.objects.create(
        academic_year="1448", term="2", name="mixed", gender="M", programs=["AI", "AI2"]
    )
    DeliveryBoard.objects.create(scenario=scenario, label="A", nominal_term=4, program="AI")
    DeliveryBoard.objects.create(scenario=scenario, label="B", nominal_term=4, program="AI2")
    ProgrammeRequirement.objects.create(
        program="AI", course_code="IS482", course_name="X", programme_term=4, is_online=True
    )
    budget = ScenarioSectionBudget.objects.create(
        scenario=scenario,
        course_key="IS482",
        course_code="IS482",
        department="AI",
        credit_hours=3,
        planned_sections=1,
        max_per_section=40,
        total_demand=10,
        programme_term=4,
    )

    options = _load_static(scenario.id).options("IS482", budget)

    daytime = {
        tuple(sorted((DAYS.index(m["day"]), _m(m["start"]), _m(m["end"])) for m in option))
        for option in generate_course_meeting_options("IS482", 3, [], [], [], course_key="IS482")
    }
    assert daytime < set(options)
    assert len(options) == len(daytime) + 90
    # Online on its only board: the windows alone.
    DeliveryBoard.objects.filter(scenario=scenario, label="B").delete()
    windows_only = _load_static(scenario.id).options("IS482", budget)
    assert len(windows_only) == 90
    assert all((_m("15:50") <= start) for option in windows_only for _d, start, _e in option)


@pytest.mark.django_db
@pytest.mark.parametrize("seed", (3, 11, 29))
def test_flag_on_annealing_and_the_load_balancer_keep_online_meetings_in_windows(
    flag_on, seed
) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_load_balanced import rebalance_and_persist_board
    from core.services.timetable_local_search import optimize_and_persist_board
    from core.services.timetable_run_seed import seeded_timetable_run

    scenario = build_online_corpus(seed)
    with seeded_timetable_run(seed):
        auto_place_scenario(scenario.id, strategy="compact")
        for board in DeliveryBoard.objects.filter(scenario=scenario).order_by("display_order"):
            optimize_and_persist_board(board.id, max_seconds=0.5)
            _assert_windows_only_for_online(scenario)
            rebalance_and_persist_board(board.id, max_seconds=0.5)
            _assert_windows_only_for_online(scenario)
    assert _online_sections_placed(scenario) == PLANNED_ONLINE


def _overloaded_online_teacher() -> TimetableScenario:
    """Dr Online teaches CS101 S1 at 09:00 on SUN and WED (locked) and three GS101
    sections whose first meetings fill SUN's three windows: four sessions on SUN
    against a cap of three."""
    scenario = _tiny_online_scenario(planned=3)
    teacher = Instructor.objects.create(full_name="Dr Online", normalised_name="dr online")
    for code in ("GS101", "CS101"):
        CourseInstructor.objects.create(
            program="AI", course_code=code, section="M", instructor=teacher
        )
    board = DeliveryBoard.objects.get(scenario=scenario)

    def put(code, section, day, start, end, locked=False):
        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=planner_course_key(code, code),
            section=section,
            defaults={"course_code": code, "course_number": code, "course_name": code},
        )
        TermSectionMeeting.objects.create(term_section=ts, day=day, start_time=start, end_time=end)
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            day=day,
            start_time=start,
            end_time=end,
            is_locked=locked,
        )

    put("CS101", "S1", "SUN", "09:00", "10:15", locked=True)
    put("CS101", "S1", "WED", "09:00", "10:15", locked=True)
    for index, (_label, start, end) in enumerate(WINDOWS):
        put("GS101", f"S{index + 1}", "SUN", start, end)
        put("GS101", f"S{index + 1}", "TUE", start, end)
    return scenario


@pytest.fixture
def cap_flags(settings):
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 3
    settings.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    settings.TIMETABLE_SECTION_DAY_SPREAD = True
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    return settings


@pytest.mark.django_db
def test_flag_on_the_cap_repair_moves_an_online_session_only_to_another_window(
    cap_flags,
) -> None:
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    scenario = _overloaded_online_teacher()
    report = repair_instructor_daily_overloads(scenario.id)

    assert report["detected"] and report["remaining_violations"] == 0
    assert report["unplaced"] == []
    gs = _times(scenario, "GS101")
    assert len(gs) == 6 and all((s, e) in WINDOW_CELLS for _x, _d, s, e in gs)
    assert sum(d == "SUN" for _x, d, _s, _e in gs) == 2
    assert _times(scenario, "CS101") == [
        ("S1", "SUN", "09:00", "10:15"),
        ("S1", "WED", "09:00", "10:15"),
    ]


@pytest.mark.django_db
def test_flag_off_the_cap_repair_still_cannot_move_an_online_session(cap_flags) -> None:
    """Today's rule on the default columns: no explicit window, so the online
    session is kept and the violation reported (the flag is what changes it)."""
    from core.services.timetable_instructor_cap_repair import repair_instructor_daily_overloads

    cap_flags.TIMETABLE_ONLINE_EVENING_WINDOWS = False
    scenario = _overloaded_online_teacher()
    before = _times(scenario, "GS101")
    report = repair_instructor_daily_overloads(scenario.id)

    assert report["detected"] and report["unplaced"] == []
    assert _times(scenario, "GS101") == before


# ── flag ON: the full optimiser and the readiness check ───────────────


@pytest.mark.django_db
def test_flag_on_full_optimiser_run_keeps_online_in_windows_and_readiness_silent(
    flag_on,
) -> None:
    from core.services.timetable_optimizer_v2 import optimise_scenario_timetable_v2
    from core.services.timetable_workspace import check_publish_readiness

    scenario = build_online_corpus(11)
    result = optimise_scenario_timetable_v2(
        scenario.id,
        strategies=["compact", "morning"],
        max_search_iterations=5,
        max_chain_iterations=2,
        cpsat_time_limit=2.0,
        cpsat_hotspot_only=True,
        chain_time_limit_seconds=2.0,
    )

    assert result.get("status") != "error", result
    _assert_windows_only_for_online(scenario)
    assert _online_sections_placed(scenario) == PLANNED_ONLINE

    def online_items():
        return [
            item
            for item in check_publish_readiness(scenario.id)["warning_items"]
            if item["code"] == "online_window_room_class"
        ]

    assert online_items() == []
    # A course that is not online, put in a window by hand, is still a warning.
    row = next(r for r in _rows(scenario) if r.board.label == "Term 2")
    SectionPlacement.objects.filter(pk=row.pk).update(start_time="17:40", end_time="19:20")
    assert [item["params"]["placement_id"] for item in online_items()] == [row.pk]


# ── nothing else reads the flag ───────────────────────────────────────


def test_the_flag_is_off_by_default_and_not_turned_on_in_production(settings) -> None:
    from core.services.timetable_flags import is_online_evening_windows_enabled

    settings_source = (REPO / "config" / "settings.py").read_text(encoding="utf-8")
    assert re.search(
        r"TIMETABLE_ONLINE_EVENING_WINDOWS = os\.getenv\(\s*"
        r'"TIMETABLE_ONLINE_EVENING_WINDOWS",\s*"false"',
        settings_source,
    )
    render = (REPO / "render.yaml").read_text(encoding="utf-8")
    assert "TIMETABLE_ONLINE_EVENING_WINDOWS" not in render
    del settings.TIMETABLE_ONLINE_EVENING_WINDOWS
    assert is_online_evening_windows_enabled() is False
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    assert is_online_evening_windows_enabled() is True


FLAG_READS = re.compile(
    r"is_online_evening_windows_enabled|ONLINE_EVENING_WINDOWS_SETTING"
    r"|settings\.TIMETABLE_ONLINE_EVENING_WINDOWS|[\"']TIMETABLE_ONLINE_EVENING_WINDOWS[\"']"
)
WINDOW_CALLS = re.compile(r"\bactive_online_windows\(")


def _sources_matching(pattern: re.Pattern) -> set[str]:
    found = set()
    for root in ("config", "core", "scheduler"):
        for path in (REPO / root).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(path.relative_to(REPO).as_posix())
    return found


def test_only_the_flags_module_and_the_window_helper_read_the_flag() -> None:
    assert _sources_matching(FLAG_READS) == {
        "config/settings.py",
        "core/services/timetable_flags.py",
        "core/services/timetable_online_windows.py",
    }


def test_only_the_automatic_placers_and_movers_ask_for_the_windows() -> None:
    """Manual moves, validation, the previews, the readiness check, exports, the
    evaluator and the scheduler package never do."""
    assert _sources_matching(WINDOW_CALLS) == {
        "core/services/timetable_online_windows.py",
        "core/services/timetable_autoplace.py",
        "core/services/timetable_solver.py",
        "core/services/timetable_local_search.py",
        "core/services/timetable_load_balanced.py",
        "core/services/timetable_instructor_cap_repair.py",
        "core/services/section_teacher_assignment.py",
    }


@pytest.mark.django_db
def test_the_flag_never_changes_manual_move_validation_or_the_previews(flag_off) -> None:
    from core.services.timetable_autoplace import auto_place_scenario
    from core.services.timetable_workspace import (
        preview_placement_slot_candidates,
        preview_planned_section_slot_candidates,
        validate_placement,
    )

    scenario = build_online_corpus(11)
    auto_place_scenario(scenario.id, strategy="compact")
    board = DeliveryBoard.objects.get(scenario=scenario, label="Term 4")
    gs_key = planner_course_key("GS101", "ISLAMIC CULTURE")
    online = (
        SectionPlacement.objects.filter(board=board, term_section__course_key=gs_key)
        .order_by("id")
        .first()
    )

    def answers():
        verdicts = [
            validate_placement(
                board.id,
                "MON",
                start,
                end,
                "",
                online.term_section_id,
                exclude_placement_id=online.id,
            )
            for start, end in (("17:40", "19:20"), ("09:00", "10:15"))
        ]
        previews = [
            preview_placement_slot_candidates(online.id, include_verdict=True, offer_manual=True),
            preview_planned_section_slot_candidates(
                board.id, course_code="GS101", course_key=gs_key, section_label="S9"
            ),
        ]
        return json.loads(json.dumps({"verdicts": verdicts, "previews": previews}, default=str))

    off = answers()
    flag_off.TIMETABLE_ONLINE_EVENING_WINDOWS = True
    on = answers()

    assert on == off
