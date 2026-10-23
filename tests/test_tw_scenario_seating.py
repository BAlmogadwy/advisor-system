"""6a: one seating serves the quality read and the plan lens (§7.4, sub-PR 6a).

``GET …/quality/?lens=1`` answers quality plus the plan lens built from the
quality read's own seating. Without ``lens`` both reads are exactly what they
were before (an oracle recorded at the base commit, key order and query count
included), except that the lens payload ends with ``seating``. The lens reads
its evidence from a seating's states through one derivation
(``seating_evidence``) and says whether the seating was assessed: a failure is
logged and never reads as "nobody sits here". A lens that cannot be built never
costs the quality read. From 6b the lens of ``?lens=1`` ends with
``programme_view`` (``tests/test_tw_programme_view.py``): without it, it
equals ``/plan-lens/``.
"""

from __future__ import annotations

import json
import logging
import re
import weakref
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import Student, TermSection
from core.services import timetable_plan_lens as plan_lens
from core.services import timetable_scenario_quality as quality
from core.services import timetable_student_assignment as ssa
from core.services.rbac import ROLE_SUPER_ADMIN
from core.services.timetable_assignment_models import StudentAssignmentState
from tests.tw_scenario_seating_support import (
    FLAG_MODES,
    NO_DEMAND,
    STUDENTS,
    apply_flags,
    build_seating_fixture,
    lens_url,
    quality_url,
    read,
    record_legacy_reads,
    remove_demand,
)

pytestmark = pytest.mark.django_db

ORACLE = Path(__file__).resolve().parent / "fixtures" / "tw_quality_legacy.json"
SEATED = {"assessed": True, "students": STUDENTS}
NOBODY = {"assessed": True, "students": 0}
FAILED = {"assessed": False, "students": 0, "reason": "assessment_failed"}
LENS_LOGGER = "core.services.timetable_plan_lens"
QUALITY_LOGGER = "core.services.timetable_scenario_quality"

PROGRAMMES = (
    'SELECT "students"."student_id" AS "student_id", "students"."program" AS "program" '
    'FROM "students"'
)
BUDGETS = 'SELECT "scenario_section_budgets"."id", "scenario_section_budgets"."scenario_id"'
SECTIONS = 'SELECT "term_sections"."id", "term_sections"."scenario_id"'
SCENARIO = 'SELECT "timetable_scenarios"."id"'
BOARDS = 'SELECT "delivery_boards"."id"'
PLACEMENTS = 'SELECT "section_placements"."id"'
REQUIREMENTS = 'SELECT "programme_requirements"."id"'
MAPPINGS = 'SELECT "elective_term_mappings"."id"'
#: The programme view's own reads (6b): the scenario row (the elective
#: mappings need its year and term), its boards, its placements and its course
#: keys (budgets, sections), then, per board programme, its plan rows and its
#: elective mappings (the fixture has one board programme, AI2).
VIEW_READS = [SCENARIO, BOARDS, PLACEMENTS, BUDGETS, SECTIONS]
PER_PROGRAMME = [REQUIREMENTS, MAPPINGS]
#: The reads ``?lens=1`` adds to the plain quality read, in order, when the
#: quality read hands over its seating and its demand rows: the seated
#: students' programmes, then the lens's own inputs (the demand's programmes,
#: budgets, sections), then the programme view's. Never the demand again,
#: never a second seating.
LENS_READS = [PROGRAMMES, PROGRAMMES, BUDGETS, SECTIONS, *VIEW_READS, *PER_PROGRAMME]


@pytest.fixture
def http():
    user = User.objects.create_user(username="seating-admin")
    user.groups.add(Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)[0])
    client = Client()
    client.force_login(user)
    return client


def _text(value) -> str:
    """JSON text, key order kept: equal texts are equal bodies in equal order."""
    return json.dumps(value, ensure_ascii=False)


def _reads(queries) -> list[str]:
    """Each query's SQL up to its first table (``… FROM "table"``), so the same
    read compares equal whatever its parameters (ids, the session's clock)."""
    out = []
    for query in queries:
        sql = query["sql"]
        match = re.search(r' FROM "\w+"', sql)
        out.append(sql[: match.end()] if match else sql)
    return out


def _logs(caplog, logger: str) -> int:
    return sum(1 for r in caplog.records if r.name == logger and r.levelno >= logging.ERROR)


def _boom(*_args, **_kwargs):
    raise ValueError("English board copies must each use the same complete weekly pattern")


def _lens_and_view(lens: dict) -> tuple[dict, dict]:
    """``?lens=1``'s lens split: what ``/plan-lens/`` answers, and the
    programme view it ends with (6b)."""
    assert list(lens)[-1] == "programme_view"
    rest = dict(lens)
    view = rest.pop("programme_view")
    assert view is not None
    return rest, view


# ── SQ1: without the parameter, nothing changed ──────────────────────────────


def test_sq1_without_lens_both_reads_equal_the_oracle_in_every_flag_mode(settings, http):
    """The JSON of ``/quality/`` and ``/plan-lens/`` equals the oracle recorded
    at the base commit, key order included, and each request runs as many
    queries; the lens only gains ``seating``, as its last key."""
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
    fx = build_seating_fixture()
    for mode in FLAG_MODES:
        apply_flags(settings, mode)
        now = record_legacy_reads(http, fx)
        expected = oracle[mode]
        assert "plan_lens" not in now["quality"], mode
        assert _text(now["quality"]) == _text(expected["quality"]), mode
        lens = now["plan_lens"]["plan_lens"]
        assert list(lens)[-1] == "seating", mode
        assert lens.pop("seating") == SEATED, mode
        assert _text(now["plan_lens"]) == _text(expected["plan_lens"]), mode
        assert now["queries"] == expected["queries"], mode


# ── SQ2: one seating, the same lens ──────────────────────────────────────────


@pytest.mark.parametrize("mode", ["local", "production"])
def test_sq2_the_lens_from_quality_equals_plan_lens_from_one_seating(settings, http, mode):
    apply_flags(settings, mode)
    fx = build_seating_fixture()
    alone, _ = read(http, lens_url(fx))
    plain, _ = read(http, quality_url(fx))
    real = ssa.assign_students_to_sections
    with mock.patch.object(ssa, "assign_students_to_sections", wraps=real) as seat:
        both, _ = read(http, quality_url(fx, "?lens=1"))
    assert seat.call_count == 1
    assert list(both)[-1] == "plan_lens"
    lens, view = _lens_and_view(both.pop("plan_lens"))
    assert _text(both) == _text(plain)
    assert _text(lens) == _text(alone["plan_lens"])
    assert view["seating_assessed"] is True
    # Not a vacuous equality: students sit, and the mark's grouping shows
    # exactly when the flag is on.
    assert lens["seating"] == SEATED
    seated = {
        (row["course_key"], row["section"]): row["actual_plans"]
        for row in lens["sections"].values()
    }
    grouped = mode == "production"
    assert seated[("AI236", "S2")] == ({"DS": 6, "AI2": 4} if grouped else {"DS": 6, "AI2": 0})
    if mode == "production":
        assert lens["programme_grouping"]["marked_sections"] == ["AI236_S2"]
    else:
        assert "programme_grouping" not in lens


def test_sq2b_with_nothing_marked_the_lens_from_quality_reports_no_grouping(settings, http):
    """Production flags, no section marked: the grouping did nothing, so the
    lens from quality's seating says ``programme_grouping: null``, as
    ``/plan-lens/`` does, never a report of its own."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    TermSection.objects.filter(scenario=fx.scenario).update(preferred_programme="")
    alone, _ = read(http, lens_url(fx))
    real = ssa.assign_students_to_sections
    with mock.patch.object(ssa, "assign_students_to_sections", wraps=real) as seat:
        both, _ = read(http, quality_url(fx, "?lens=1"))
    assert seat.call_count == 1  # the lens came from quality's evaluation
    lens, view = _lens_and_view(both["plan_lens"])
    assert view["seating_assessed"] is True
    assert lens["seating"] == SEATED
    assert "programme_grouping" in lens and lens["programme_grouping"] is None
    assert alone["plan_lens"]["programme_grouping"] is None
    assert _text(lens) == _text(alone["plan_lens"])


# ── SQ3: exactly the lens's own reads ────────────────────────────────────────


@pytest.mark.parametrize("mode", list(FLAG_MODES))
def test_sq3_the_lens_adds_only_its_own_reads(settings, http, mode):
    apply_flags(settings, mode)
    fx = build_seating_fixture()
    _, plain = read(http, quality_url(fx))
    _, both = read(http, quality_url(fx, "?lens=1"))
    assert len(both) == len(plain) + len(LENS_READS)
    assert _reads(both[: len(plain)]) == _reads(plain)
    added = both[len(plain) :]
    assert [q["sql"][: len(prefix)] for q, prefix in zip(added, LENS_READS, strict=True)] == (
        LENS_READS
    )


# ── SQ4: a seating that cannot be built is unknown ───────────────────────────


def test_sq4_a_seating_that_cannot_be_built_is_unknown_on_both_reads(
    settings, http, monkeypatch, caplog
):
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    monkeypatch.setattr(quality, "build_section_states_for_scenario", _boom)
    monkeypatch.setattr(plan_lens, "build_section_states_for_scenario", _boom)
    caplog.set_level(logging.ERROR)

    plain, _ = read(http, quality_url(fx))
    # Quality as today: the instructor step fails; seats are unassessed, with
    # no reason of their own.
    assert plain["instructor"]["assessed"] is False
    assert plain["instructor"]["reason"] == "assessment_failed"
    assert plain["seats"]["assessed"] is False and "reason" not in plain["seats"]

    caplog.clear()
    both, _ = read(http, quality_url(fx, "?lens=1"))
    assert _logs(caplog, LENS_LOGGER) == 1
    lens, view = _lens_and_view(both.pop("plan_lens"))
    assert _text(both) == _text(plain)
    assert lens["seating"] == FAILED
    assert view["seating_assessed"] is False
    assert all(row["actual_total"] == 0 for row in lens["sections"].values())

    caplog.clear()
    alone, _ = read(http, lens_url(fx))
    assert _logs(caplog, LENS_LOGGER) == 1
    assert _logs(caplog, QUALITY_LOGGER) == 0
    assert alone["plan_lens"]["seating"] == FAILED
    assert _text(lens) == _text(alone["plan_lens"])


def test_sq4b_a_demand_read_that_raises_leaves_only_the_seats_unknown(
    settings, http, monkeypatch, caplog
):
    """The quality read's own demand read raises: HTTP 200, the three seat
    parts unknown (``assessment_failed``), logged once, the instructor part
    assessed. With the lens, quality hands it nothing, so it seats on its own
    and equals ``/plan-lens/``."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    alone, _ = read(http, lens_url(fx))
    monkeypatch.setattr(quality, "load_scenario_course_demands", _boom)
    caplog.set_level(logging.ERROR)
    caplog.clear()
    plain, _ = read(http, quality_url(fx))  # 200, or ``read`` fails
    assert _logs(caplog, QUALITY_LOGGER) == 1
    for part in ("soft_seats", "seats", "assigned_student_clashes"):
        assert plain[part]["assessed"] is False, part
        assert plain[part]["reason"] == "assessment_failed", part
    assert plain["instructor"]["assessed"] is True

    real = ssa.assign_students_to_sections
    with mock.patch.object(ssa, "assign_students_to_sections", wraps=real) as seat:
        both, _ = read(http, quality_url(fx, "?lens=1"))
    assert seat.call_count == 1  # quality never seated: the lens seated once
    lens, view = _lens_and_view(both.pop("plan_lens"))
    assert _text(both) == _text(plain)
    assert view["seating_assessed"] is True
    assert lens["seating"] == SEATED
    assert _text(lens) == _text(alone["plan_lens"])


# ── SQ5: nobody to seat is assessed ──────────────────────────────────────────


def test_sq5_no_demand_is_assessed_with_nobody_seated(settings, http):
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    remove_demand(fx)
    _, plain_queries = read(http, quality_url(fx))
    both, both_queries = read(http, quality_url(fx, "?lens=1"))
    alone, _ = read(http, lens_url(fx))
    assert both["seats"]["reason"] == "no_student_demand"
    lens, view = _lens_and_view(both["plan_lens"])
    assert lens["seating"] == NOBODY
    assert alone["plan_lens"]["seating"] == NOBODY
    assert _text(lens) == _text(alone["plan_lens"])
    assert view["seating_assessed"] is True
    # Nobody to seat: the lens reads only budgets and sections (no programme
    # of nobody), never the demand or the seating's inputs again; then the
    # programme view its own.
    own_reads = [BUDGETS, SECTIONS, *VIEW_READS, *PER_PROGRAMME]
    added = both_queries[len(plain_queries) :]
    assert [q["sql"][: len(p)] for q, p in zip(added, own_reads, strict=True)] == own_reads


def test_sq5b_without_lens_a_read_with_no_demand_equals_the_oracle(settings, http):
    """Nobody to seat, no ``lens``: both reads equal the base commit's, query
    counts included. Quality reads the (empty) demand once and hands it to the
    profile builder, which never reads it again."""
    expected = json.loads(ORACLE.read_text(encoding="utf-8"))[NO_DEMAND]
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    remove_demand(fx)
    now = record_legacy_reads(http, fx)
    assert _text(now["quality"]) == _text(expected["quality"])
    lens = now["plan_lens"]["plan_lens"]
    assert list(lens)[-1] == "seating"
    assert lens.pop("seating") == NOBODY
    assert _text(now["plan_lens"]) == _text(expected["plan_lens"])
    assert now["queries"] == expected["queries"]


# ── SQ6: the parameter counts only when it is exactly "1" ────────────────────


@pytest.mark.parametrize(
    "query", ["?lens=true", "?lens=0", "?lens=", "?lens=yes", "?lens=1.0", "?lens=%201"]
)
def test_sq6_only_lens_1_adds_the_lens(settings, http, query):
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    plain, _ = read(http, quality_url(fx))
    other, _ = read(http, quality_url(fx, query))
    assert "plan_lens" not in other
    assert _text(other) == _text(plain)
    with_lens, _ = read(http, quality_url(fx, "?lens=1"))
    assert "plan_lens" in with_lens


# ── SQ8: a quality failure that is not the seating's ─────────────────────────


def test_sq8_an_instructor_failure_still_gives_the_lens_its_own_seating(
    settings, http, monkeypatch
):
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    monkeypatch.setattr(quality, "instructor_schedule_metrics", _boom)
    alone, _ = read(http, lens_url(fx))
    real = ssa.assign_students_to_sections
    with mock.patch.object(ssa, "assign_students_to_sections", wraps=real) as seat:
        both, _ = read(http, quality_url(fx, "?lens=1"))
    assert both["instructor"]["reason"] == "assessment_failed"
    assert both["seats"]["assessed"] is False
    assert seat.call_count == 1  # quality never seated: the lens seated once
    lens, _view = _lens_and_view(both["plan_lens"])
    assert lens["seating"] == SEATED
    assert _text(lens) == _text(alone["plan_lens"])


def test_sq8b_a_failure_after_the_seating_makes_the_lens_seat_on_its_own(
    settings, http, monkeypatch
):
    """Quality's seating ran but reading it raised: quality reports the seats
    unknown and hands the lens nothing, so the lens seats on its own and still
    equals ``/plan-lens/``."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    monkeypatch.setattr(quality, "_seat_tiers", _boom)
    alone, _ = read(http, lens_url(fx))
    real = ssa.assign_students_to_sections
    with mock.patch.object(ssa, "assign_students_to_sections", wraps=real) as seat:
        both, _ = read(http, quality_url(fx, "?lens=1"))
    assert both["seats"]["reason"] == "assessment_failed"
    assert seat.call_count == 2
    lens, _view = _lens_and_view(both["plan_lens"])
    assert lens["seating"] == SEATED
    assert _text(lens) == _text(alone["plan_lens"])


# ── SQ9: a lens failure never costs the quality read ─────────────────────────


@pytest.mark.parametrize("broken", ["build_scenario_plan_lens", "seating_evidence"])
def test_sq9_a_lens_failure_never_costs_the_quality_read(
    settings, http, monkeypatch, caplog, broken
):
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    plain, _ = read(http, quality_url(fx))
    monkeypatch.setattr(quality, broken, _boom)
    caplog.set_level(logging.ERROR)
    caplog.clear()
    both, _ = read(http, quality_url(fx, "?lens=1"))  # 200, or ``read`` fails
    assert list(both)[-1] == "plan_lens"
    assert both.pop("plan_lens") is None
    assert _text(both) == _text(plain)
    assert _logs(caplog, QUALITY_LOGGER) == 1


# ── the derivation and the read's memory ─────────────────────────────────────


def _state(student_id: str, *section_ids: str) -> StudentAssignmentState:
    return StudentAssignmentState(student_id=student_id, section_ids=set(section_ids))


def test_seating_evidence_counts_each_seated_student_once_per_section_by_programme():
    for student_id, program in ((1001, "ai"), (1002, "DS")):
        Student.objects.create(
            student_id=student_id,
            registration_no=str(student_id),
            name=f"Student {student_id}",
            program=program,
            section="M",
            status="active",
        )
    states = {
        "1001": _state("1001", "AI101_S1", "AI102_S2", "CS_X::NAME_S3"),
        "1002": _state("1002", "AI101_S1"),
        "1003": _state("1003", "AI101_S2"),  # a roster id with no student row
        "1004": _state("1004"),  # seated nowhere
        "1005": _state("1005", "NOSECTION"),  # an id that names no section
        "V-1": _state("V-1", "AI101_S1"),  # not a roster id
    }
    grouping = {"moves": 2}
    evidence = plan_lens.seating_evidence(states, grouping=grouping)
    assert evidence.counts == {
        ("AI101", "S1"): Counter({"AI": 1, "DS": 1}),
        ("AI101", "S2"): Counter({"UNKNOWN": 1}),
        ("AI102", "S2"): Counter({"AI": 1}),
        ("CS_X::NAME", "S3"): Counter({"AI": 1}),
    }
    assert evidence.students == 3
    assert evidence.assessed is True and evidence.reason is None
    assert evidence.grouping == {"moves": 2}
    assert evidence.payload() == {"assessed": True, "students": 3}


def test_the_evaluation_is_dropped_before_the_lens_reads_its_own_inputs(settings, monkeypatch):
    """The lens's reads never sit in memory on top of every student's state:
    the evaluation is gone by the time the lens is built (the read's peak)."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    evaluations = []
    real_evaluate = quality.evaluate_generated_timetable_candidate

    def evaluate(**kwargs):
        result = real_evaluate(**kwargs)
        evaluations.append(weakref.ref(result))
        return result

    alive = []
    real_lens = quality.build_scenario_plan_lens

    def lens(*args, **kwargs):
        alive.append(evaluations[0]() is not None)
        return real_lens(*args, **kwargs)

    monkeypatch.setattr(quality, "evaluate_generated_timetable_candidate", evaluate)
    monkeypatch.setattr(quality, "build_scenario_plan_lens", lens)
    out = quality.build_scenario_quality(fx.scenario.pk, with_lens=True)
    assert alive == [False]
    assert out["plan_lens"]["seating"] == SEATED


def test_the_profile_builder_takes_demand_rows_already_read(settings):
    """The quality read reads the demand once and hands the rows to the
    profile builder (and on to the lens): the same profiles, no second read."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from core.services.timetable_demand import load_scenario_course_demands
    from core.services.timetable_optimizer_v2 import build_student_profiles_for_scenario

    apply_flags(settings, "production")
    fx = build_seating_fixture()
    rows = load_scenario_course_demands(fx.scenario.pk)
    with CaptureQueriesContext(connection) as own:
        read_here = build_student_profiles_for_scenario(fx.scenario.pk)
    with CaptureQueriesContext(connection) as given:
        handed = build_student_profiles_for_scenario(fx.scenario.pk, demand_rows=rows)
    assert handed == read_here and len(handed) == STUDENTS
    assert len(given) == len(own) - 1
    assert not any("scenario_student_course_requests" in q["sql"] for q in given)


@pytest.mark.parametrize("with_lens", [False, True])
def test_the_demand_rows_are_held_through_the_seating_only_for_a_lens(
    settings, monkeypatch, with_lens
):
    """Plain quality lets its demand rows go before the seating, as it always
    did; only a read with the lens holds them for the lens."""
    apply_flags(settings, "production")
    fx = build_seating_fixture()
    rows = []
    real_load = quality.load_scenario_course_demands

    def load(*args, **kwargs):
        out = real_load(*args, **kwargs)
        rows.append(weakref.ref(out[0]))
        return out

    alive = []
    real_evaluate = quality.evaluate_generated_timetable_candidate

    def evaluate(**kwargs):
        alive.append(rows[0]() is not None)
        return real_evaluate(**kwargs)

    monkeypatch.setattr(quality, "load_scenario_course_demands", load)
    monkeypatch.setattr(quality, "evaluate_generated_timetable_candidate", evaluate)
    out = quality.build_scenario_quality(fx.scenario.pk, with_lens=with_lens)
    assert alive == [with_lens]
    assert out["seats"]["assessed"] is True
