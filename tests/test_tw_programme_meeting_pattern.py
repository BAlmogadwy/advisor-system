"""A programme's default weekly meetings, as the timetable workspace reads it (PR 2, §6).

Owner, 2026-11-10: each programme sets how its 3-credit courses meet each week
(``ProgrammeRequirement.meeting_pattern``: "" not set, "standard" or "2x100"),
both campuses alike. A scenario's course takes what the scenario's programmes
that voted agree on; a programme that has not voted does not count; when they
differ the course keeps its credit hours' pattern (``timetable_meeting_shape``).
These tests pin what the workspace server makes of it:

* the budget rows, asked with the scenario (``department_for``), gain exactly
  two keys - ``programme_meeting_pattern`` and ``meeting_pattern_drift`` -
  and every other key, ``department`` (the academic department) included, is
  the default answer's, byte for byte; asked without, nothing changes;
* drift is only a difference from a value the programmes SET and that the
  course can take: a scenario's own 2 x 100 over programmes that set nothing
  (scenario 669's STAT103) is not drift, nor a 4-credit row or an English
  bundle;
* the programmes are read once, whatever the number of courses;
* the budget, weekly-meetings and synchronous Generate answers carry them;
* Add course left on "Programme default" (no ``meeting_pattern`` sent) takes
  the programmes' value fitted to the POSTED credit hours, never refused, and
  says what it applied; anything sent is the request's, audited as before;
* Generate hands the scheduler's build job every budget key's weekly meetings,
  "" included, and the classic build nothing new.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.models import (
    AuditLog,
    DeliveryBoard,
    PlannerJob,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.timetable_workspace import compute_scenario_budget

pytestmark = pytest.mark.django_db

#: The two keys the scenario's programmes add to a budget row.
PROGRAMME_KEYS = {"programme_meeting_pattern", "meeting_pattern_drift"}
STAT = "STAT103::PRINCIPLES_OF_STATISTICS"
LINEAR = "MATH243::LINEAR_ALGEBRA_I"
LOGIC = "COE226::DIGITAL_LOGIC"
DATA = "AI201::DATA_STRUCTURES"
ENGLISH = "ENG101::ENGLISH_LANGUAGE_SKILLS_I"
DEEP = "AI401::DEEP_LEARNING"
BUDGET_URL = "/ops/tw/scenarios/{}/budget/"
PATTERN_URL = "/ops/tw/scenarios/{}/budget/meeting-pattern/"


def _scenario(
    programs=("AI2", "DS2", "CS2"), name: str = "Programme defaults"
) -> TimetableScenario:
    return TimetableScenario.objects.create(
        academic_year="1448", term="2", name=name, gender="F", programs=list(programs)
    )


def _vote(program: str, code: str, name: str, pattern: str = "", *, credit: int = 3) -> str:
    """A study-plan row of `program` holding its vote; returns the course's identity."""
    ProgrammeRequirement.objects.create(
        program=program,
        course_code=code,
        course_name=name,
        credit_hours=credit,
        programme_term=2,
        meeting_pattern=pattern,
    )
    return planner_course_key(code, name)


def _budget(
    sc, key: str, *, credit: int = 3, pattern: str = "", department: str = "MATH"
) -> ScenarioSectionBudget:
    code = key.split("::", 1)[0]
    return ScenarioSectionBudget.objects.create(
        scenario=sc,
        course_key=key,
        course_code=code,
        course_name=key.split("::", 1)[-1].replace("_", " "),
        department=department,
        credit_hours=credit,
        planned_sections=2,
        max_per_section=40,
        total_demand=60,
        programme_term=2,
        meeting_pattern=pattern,
    )


def _rows(sc, *, with_programmes: bool = True) -> dict[str, dict]:
    rows = compute_scenario_budget(sc.id, department_for=sc if with_programmes else None)
    return {row["course_key"]: row for row in rows}


def _six_courses() -> TimetableScenario:
    """One scenario with a course in every state the programmes can leave it in."""
    sc = _scenario()
    # Agreed on 2 x 100 (two votes); the scenario still has the credit default.
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _vote("DS2", "STAT103", "Principles of Statistics", "2x100")
    _budget(sc, STAT)
    # The programmes differ; CS2 has not voted.
    _vote("AI2", "MATH243", "Linear Algebra I", "2x100")
    _vote("DS2", "MATH243", "Linear Algebra I", "standard")
    _vote("CS2", "MATH243", "Linear Algebra I")
    _budget(sc, LINEAR)
    # Agreed on the standard pattern, set to 2 x 100 in the scenario.
    _vote("CS2", "COE226", "Digital Logic", "standard")
    _budget(sc, LOGIC, pattern="2x100", department="COE")
    # Nobody voted; the scenario chose 2 x 100 on its own (PR 1).
    _vote("AI2", "AI201", "Data Structures")
    _budget(sc, DATA, pattern="2x100", department="AI")
    # An English bundle wins over any vote.
    _vote("AI2", "ENG101", "English Language Skills I", "2x100")
    _budget(sc, ENGLISH, department="ENG")
    # A 4-credit course: no vote can exist (the database refuses one).
    _vote("AI2", "AI401", "Deep Learning", credit=4)
    _budget(sc, DEEP, credit=4, department="AI")
    # Two courses behind one typed code: never guessed between.
    _vote("AI2", "MGT324", "Entrepreneurship and Innovation", "2x100")
    _vote("DS2", "MGT324", "Entrepreneurship & Innovation", "2x100")
    _budget(sc, "MGT324", department="MGT")
    return sc


# ── the budget rows ────────────────────────────────────────────────────────


def test_with_its_programmes_each_row_gains_exactly_two_keys_and_keeps_every_other():
    sc = _six_courses()

    default = compute_scenario_budget(sc.id)
    enriched = compute_scenario_budget(sc.id, department_for=sc)

    # The default answer is the one every engine, export and check reads:
    # nothing of the programmes in it.
    assert all(not PROGRAMME_KEYS & set(row) for row in default)
    assert [set(row) - set(plain) for row, plain in zip(enriched, default, strict=True)] == [
        PROGRAMME_KEYS
    ] * len(default)
    # Everything else, whole: `department` stays the course's academic department.
    assert [
        {k: v for k, v in row.items() if k not in PROGRAMME_KEYS} for row in enriched
    ] == default
    assert [(row["course_code"], row["department"]) for row in enriched] == [
        ("AI201", "AI"),
        ("AI401", "AI"),
        ("COE226", "COE"),
        ("ENG101", "ENG"),
        ("MATH243", "MATH"),
        ("MGT324", "MGT"),
        ("STAT103", "MATH"),
    ]


def test_each_row_says_what_its_programmes_decided_and_whether_it_differs():
    sc = _six_courses()
    rows = _rows(sc)

    def block(key: str) -> dict:
        return rows[key]["programme_meeting_pattern"]

    assert block(STAT) == {
        "status": "agree",
        "meeting_pattern": "2x100",
        "label": "2 × 100 min",
        "label_ar": "٢ × ١٠٠ دقيقة",
        "programmes": [
            {"program": "AI2", "meeting_pattern": "2x100"},
            {"program": "DS2", "meeting_pattern": "2x100"},
        ],
    }
    assert block(LINEAR) == {
        "status": "mixed",
        "meeting_pattern": "",
        "label": "",
        "label_ar": "",
        "programmes": [
            {"program": "AI2", "meeting_pattern": "2x100"},
            {"program": "CS2", "meeting_pattern": ""},
            {"program": "DS2", "meeting_pattern": "standard"},
        ],
    }
    assert block(LOGIC) == {
        "status": "agree",
        "meeting_pattern": "standard",
        "label": "Standard (2 × 75 min)",
        "label_ar": "قياسي (٢ × ٧٥ دقيقة)",
        "programmes": [{"program": "CS2", "meeting_pattern": "standard"}],
    }
    assert block(DATA) == {
        "status": "none",
        "meeting_pattern": "",
        "label": "",
        "label_ar": "",
        "programmes": [{"program": "AI2", "meeting_pattern": ""}],
    }
    assert block("MGT324") == {
        "status": "ambiguous",
        "meeting_pattern": "",
        "label": "",
        "label_ar": "",
        "programmes": [],
    }
    assert {key: row["meeting_pattern_drift"] for key, row in rows.items()} == {
        STAT: True,  # agreed on 2 x 100, still 2 x 75
        LINEAR: False,  # they differ: nothing was agreed
        LOGIC: True,  # agreed on standard, set to 2 x 100 here
        DATA: False,  # nobody voted: the scenario's own 2 x 100 stands
        ENGLISH: False,  # its English bundle offers no choice
        DEEP: False,  # 4 credits: no choice
        "MGT324": False,  # two courses behind the code
    }


@pytest.mark.parametrize(
    ("votes", "budget_pattern", "credit", "drift"),
    [
        # Scenario 669: STAT103 set to 2 x 100 per scenario, its programmes unset.
        ((("AI2", ""), ("DS2", "")), "2x100", 3, False),
        # Back on 2 x 75 where the programmes agreed on 2 x 100.
        ((("AI2", "2x100"), ("DS2", "2x100")), "", 3, True),
        ((("AI2", "2x100"), ("DS2", "")), "", 3, True),  # an unset one does not count
        ((("AI2", "2x100"), ("DS2", "2x100")), "2x100", 3, False),
        ((("AI2", "standard"),), "2x100", 3, True),
        ((("AI2", "standard"),), "", 3, False),
        ((("AI2", "standard"), ("DS2", "2x100")), "", 3, False),  # they differ
        # Added by hand at 4 credits under the code the programmes set: not a choice.
        ((("AI2", "2x100"), ("DS2", "2x100")), "", 4, False),
    ],
)
def test_drift_is_only_a_difference_from_a_value_the_programmes_set_and_the_course_can_take(
    votes, budget_pattern, credit, drift
):
    sc = _scenario(programs=("AI2", "DS2"))
    for program, pattern in votes:
        _vote(program, "STAT103", "Principles of Statistics", pattern)
    key = STAT if credit == 3 else "STAT103"
    _budget(sc, key, credit=credit, pattern=budget_pattern)

    assert _rows(sc)[key]["meeting_pattern_drift"] is drift


def test_only_the_scenarios_own_programmes_decide():
    # AI (not in the scenario) chose standard; the scenario's AI2 chose 2 x 100.
    sc = _scenario(programs=("AI2",))
    _vote("AI", "STAT103", "Principles of Statistics", "standard")
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _budget(sc, STAT)

    block = _rows(sc)[STAT]["programme_meeting_pattern"]

    assert (block["status"], block["meeting_pattern"]) == ("agree", "2x100")
    assert block["programmes"] == [{"program": "AI2", "meeting_pattern": "2x100"}]


def test_an_older_scenario_without_programmes_reads_its_boards_labels():
    sc = _scenario(programs=())
    DeliveryBoard.objects.create(scenario=sc, label="Term 2", nominal_term=2, program="ai2,DS2")
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _vote("DS2", "STAT103", "Principles of Statistics", "2x100")
    _budget(sc, STAT)

    assert _rows(sc)[STAT]["programme_meeting_pattern"]["status"] == "agree"

    bare = _scenario(programs=(), name="No programmes anywhere")
    _budget(bare, STAT)
    with CaptureQueriesContext(connection) as reads:
        row = _rows(bare)[STAT]
    assert (row["programme_meeting_pattern"]["status"], row["meeting_pattern_drift"]) == (
        "none",
        False,
    )
    assert not [q for q in reads.captured_queries if "programme_requirements" in q["sql"]]


def _reads(call) -> list[str]:
    with CaptureQueriesContext(connection) as ctx:
        call()
    return [q["sql"] for q in ctx.captured_queries]


def test_the_programmes_are_read_once_whatever_the_number_of_courses():
    sc = _scenario()
    board = DeliveryBoard.objects.create(scenario=sc, label="Term 2", nominal_term=2)
    # Something placed keeps compute_scenario_budget's own reads constant (with
    # nothing placed it reads the placed meetings again per course).
    ts = TermSection.objects.create(
        scenario=sc, course_key=DATA, course_code="AI201", section="S1", source_tag="tw_auto"
    )
    SectionPlacement.objects.create(
        board=board, term_section=ts, day="SUN", start_time="09:00", end_time="10:15"
    )
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _budget(sc, STAT)

    def with_programmes() -> list[str]:
        return _reads(lambda: compute_scenario_budget(sc.id, department_for=sc))

    def default() -> list[str]:
        return _reads(lambda: compute_scenario_budget(sc.id))

    one = (len(with_programmes()), len(default()))
    for number in range(5):
        _vote("AI2", f"MATH10{number}", f"Algebra {number}", "2x100")
        _budget(sc, planner_course_key(f"MATH10{number}", f"Algebra {number}"))
    _budget(sc, DEEP, credit=4)

    assert (len(with_programmes()), len(default())) == one
    assert len([sql for sql in with_programmes() if "programme_requirements" in sql]) == 1
    assert not [sql for sql in default() if "programme_requirements" in sql]


# ── the answers that carry it ──────────────────────────────────────────────


@pytest.fixture
def http() -> Client:
    ensure_role_groups()
    user = User.objects.create_user(username="programme-defaults-admin")
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _post(client: Client, url: str, payload: dict):
    return client.post(url, json.dumps(payload), content_type="application/json")


def test_the_budget_and_weekly_meetings_answers_carry_the_programmes(http):
    sc = _six_courses()

    def keyed(rows) -> dict[str, dict]:
        return {row["course_key"]: row for row in rows}

    read = keyed(http.get(BUDGET_URL.format(sc.id)).json()["budget"])
    assert read[STAT]["programme_meeting_pattern"]["status"] == "agree"
    assert (read[STAT]["meeting_pattern_drift"], read[STAT]["department"]) == (True, "MATH")

    # "Use programme default": the agreed value through the weekly-meetings
    # endpoint; its answer says the course no longer differs.
    saved = _post(http, PATTERN_URL.format(sc.id), {"course_key": STAT, "meeting_pattern": "2x100"})
    assert saved.status_code == 200, saved.content
    row = keyed(saved.json()["budget"])[STAT]
    assert (row["meeting_pattern"], row["meeting_pattern_drift"]) == ("2x100", False)
    assert row["programme_meeting_pattern"]["meeting_pattern"] == "2x100"

    added = _post(http, BUDGET_URL.format(sc.id), {"course_key": STAT, "sections": 1})
    assert added.status_code == 200, added.content
    assert keyed(added.json()["budget"])[STAT]["meeting_pattern_drift"] is False


# ── Add course: "Programme default" ────────────────────────────────────────


def _added(client: Client, sc, payload: dict) -> tuple[dict, ScenarioSectionBudget, dict]:
    response = _post(client, BUDGET_URL.format(sc.id), payload)
    assert response.status_code == 200, response.content
    key = str(payload.get("course_key") or payload["course_code"]).strip().upper()
    audit = AuditLog.objects.filter(action="tw.scenario.budget.add").order_by("-id").first()
    return (
        response.json(),
        ScenarioSectionBudget.objects.get(scenario=sc, course_key=key),
        json.loads(audit.details_json),
    )


@pytest.fixture
def stat_agreed() -> TimetableScenario:
    """AI2 and DS2 agree STAT103 meets 2 x 100; CS2 has not voted."""
    sc = _scenario()
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _vote("DS2", "STAT103", "Principles of Statistics", "2x100")
    _vote("CS2", "STAT103", "Principles of Statistics")
    return sc


@pytest.mark.parametrize("left_out", [{}, {"meeting_pattern": None}])
def test_a_new_course_left_on_programme_default_takes_what_its_programmes_agreed(
    http, stat_agreed, left_out
):
    body, budget, audit = _added(
        http, stat_agreed, {"course_code": "stat103", "credit_hours": 3, "sections": 2, **left_out}
    )

    assert budget.meeting_pattern == "2x100"
    assert body["meeting_pattern_applied"] == {
        "value": "2x100",
        "source": "programmes",
        "status": "agree",
    }
    assert audit == {
        "scenario_id": stat_agreed.id,
        "course_key": "STAT103",
        "sections": 2,
        "meeting_pattern": "2x100",
        "meeting_pattern_source": "programmes",
    }
    row = next(r for r in body["budget"] if r["course_key"] == "STAT103")
    assert (row["meeting_pattern"], row["meeting_pattern_drift"]) == ("2x100", False)


def test_a_value_that_does_not_fit_the_posted_credit_hours_is_the_credit_default_never_a_refusal(
    http, stat_agreed
):
    # A mistyped 4: nothing was sent, so nothing is refused.
    body, budget, audit = _added(http, stat_agreed, {"course_code": "STAT103", "credit_hours": 4})

    assert budget.meeting_pattern == ""
    assert body["meeting_pattern_applied"] == {"value": "", "source": "standard", "status": "agree"}
    assert audit == {
        "scenario_id": stat_agreed.id,
        "course_key": "STAT103",
        "sections": 1,
        "meeting_pattern": "",
    }


@pytest.mark.parametrize(
    ("sent", "stored"), [("standard", ""), ("", ""), ("Standard", ""), ("2x100", "2x100")]
)
def test_a_choice_sent_is_the_requests_and_audited_as_before(http, stat_agreed, sent, stored):
    body, budget, audit = _added(
        http, stat_agreed, {"course_code": "STAT103", "credit_hours": 3, "meeting_pattern": sent}
    )

    assert budget.meeting_pattern == stored
    assert body["meeting_pattern_applied"] == {
        "value": stored,
        "source": "request",
        "status": "agree",
    }
    assert audit == {
        "scenario_id": stat_agreed.id,
        "course_key": "STAT103",
        "sections": 1,
        "meeting_pattern": stored,
    }


def test_a_shape_sent_against_programmes_that_chose_standard_is_the_requests(http):
    sc = _scenario()
    _vote("AI2", "STAT103", "Principles of Statistics", "standard")

    body, budget, _audit = _added(
        http, sc, {"course_code": "STAT103", "credit_hours": 3, "meeting_pattern": "2x100"}
    )

    assert budget.meeting_pattern == "2x100"
    assert body["meeting_pattern_applied"]["source"] == "request"
    row = next(r for r in body["budget"] if r["course_key"] == "STAT103")
    assert row["meeting_pattern_drift"] is True


def test_a_shape_sent_for_other_credit_hours_is_refused_as_before(http, stat_agreed):
    response = _post(
        http,
        BUDGET_URL.format(stat_agreed.id),
        {"course_code": "STAT103", "credit_hours": 4, "meeting_pattern": "2x100"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "VALIDATION_BUDGET",
        "message": "STAT103 has 4 credit hours; 2 × 100 min a week is only for 3-credit courses.",
    }
    assert not stat_agreed.section_budgets.exists()


def test_programmes_that_differ_leave_a_new_course_on_its_credit_default(http):
    sc = _scenario()
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    _vote("DS2", "STAT103", "Principles of Statistics", "standard")

    body, budget, audit = _added(http, sc, {"course_code": "STAT103", "credit_hours": 3})

    assert budget.meeting_pattern == ""
    assert body["meeting_pattern_applied"] == {"value": "", "source": "standard", "status": "mixed"}
    assert "meeting_pattern_source" not in audit


def test_a_typed_code_behind_two_courses_is_never_guessed(http):
    sc = _scenario()
    _vote("AI2", "MGT324", "Entrepreneurship and Innovation", "2x100")
    _vote("DS2", "MGT324", "Entrepreneurship & Innovation", "2x100")

    body, budget, _audit = _added(http, sc, {"course_code": "MGT324", "credit_hours": 3})

    assert (budget.meeting_pattern, body["meeting_pattern_applied"]["status"]) == ("", "ambiguous")


def test_an_exact_identity_posted_is_read_as_that_course(http):
    sc = _scenario(programs=("CYP", "CYP2"))
    _vote("CYP", "MATH243", "Linear Algebra I", "standard")
    exact = _vote("CYP2", "MATH243", "Linear Algebra", "2x100")

    body, budget, _audit = _added(
        http, sc, {"course_key": exact, "course_code": "MATH243", "credit_hours": 3}
    )

    assert budget.meeting_pattern == "2x100"
    assert body["meeting_pattern_applied"] == {
        "value": "2x100",
        "source": "programmes",
        "status": "agree",
    }


def test_an_english_bundle_never_takes_a_programme_value(http):
    sc = _scenario()
    _vote("AI2", "ENG101", "English Language Skills I", "2x100")

    body, budget, _audit = _added(http, sc, {"course_code": "ENG101", "credit_hours": 3})

    assert budget.meeting_pattern == ""
    assert body["meeting_pattern_applied"]["source"] == "standard"


def test_a_scenario_without_programmes_adds_a_course_exactly_as_before(http):
    sc = _scenario(programs=())
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")

    body, budget, audit = _added(http, sc, {"course_code": "STAT103", "credit_hours": 3})

    assert budget.meeting_pattern == ""
    assert body["meeting_pattern_applied"] == {"value": "", "source": "standard", "status": "none"}
    assert audit == {
        "scenario_id": sc.id,
        "course_key": "STAT103",
        "sections": 1,
        "meeting_pattern": "",
    }


def test_more_sections_of_a_course_already_there_apply_nothing(http, stat_agreed):
    _budget(stat_agreed, "STAT103")

    body, budget, audit = _added(http, stat_agreed, {"course_key": "STAT103", "sections": 1})

    assert (budget.meeting_pattern, body["meeting_pattern_applied"]) == ("", None)
    assert "meeting_pattern_source" not in audit
    # A read answers no such key.
    assert "meeting_pattern_applied" not in http.get(BUDGET_URL.format(stat_agreed.id)).json()


# ── "Use programme default(s)": the programmes' value as it is NOW ─────────


def _use_default(client: Client, sc, key: str, **more):
    return _post(client, PATTERN_URL.format(sc.id), {"course_key": key, **more})


def _pattern_audits() -> list[dict]:
    return [
        json.loads(row.details_json)
        for row in AuditLog.objects.filter(action="tw.scenario.budget.meeting_pattern").order_by(
            "id"
        )
    ]


@pytest.mark.parametrize(
    ("key", "old", "new"),
    [
        # Agreed on 2 x 100; the scenario has the credit default.
        (STAT, "", "2x100"),
        # Agreed on the standard pattern; the scenario set 2 x 100.
        (LOGIC, "2x100", ""),
    ],
)
@pytest.mark.parametrize("expected", [True, False])
def test_use_programme_default_saves_what_the_programmes_decide_now(http, key, old, new, expected):
    sc = _six_courses()
    more = {"expected_meeting_pattern": new} if expected else {}

    response = _use_default(http, sc, key, use_programme_default=True, **more)

    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["meeting_pattern"], body["previous"], body["changed"]) == (new, old, True)
    assert body["meeting_pattern_source"] == "programmes"
    assert body["mismatched_sections"] == []
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key=key).meeting_pattern == new
    row = next(r for r in body["budget"] if r["course_key"] == key)
    assert row["meeting_pattern_drift"] is False
    # Audited as an explicit save is, and says the value was the programmes'.
    assert _pattern_audits() == [
        {
            "scenario_id": sc.id,
            "course_key": key,
            "old": old,
            "new": new,
            "mismatched": 0,
            "meeting_pattern_source": "programmes",
        }
    ]


def test_an_explicit_save_is_answered_and_audited_exactly_as_before(http):
    sc = _six_courses()

    response = _post(
        http, PATTERN_URL.format(sc.id), {"course_key": STAT, "meeting_pattern": "2x100"}
    )

    assert response.status_code == 200, response.content
    assert set(response.json()) == {
        "ok",
        "course_key",
        "meeting_pattern",
        "previous",
        "changed",
        "budget",
        "mismatched_sections",
    }
    assert _pattern_audits() == [
        {"scenario_id": sc.id, "course_key": STAT, "old": "", "new": "2x100", "mismatched": 0}
    ]


@pytest.mark.parametrize(
    ("key", "reason", "said"),
    [
        (LINEAR, "mixed", "MATH243's programmes differ on its weekly meetings"),
        (DATA, "none", "No programme of this scenario has set AI201's weekly meetings."),
        ("MGT324", "ambiguous", "MGT324 names two courses in this scenario's programmes"),
    ],
)
def test_a_course_with_no_programme_default_is_refused_and_left_as_it_was(http, key, reason, said):
    sc = _six_courses()
    before = ScenarioSectionBudget.objects.get(scenario=sc, course_key=key).meeting_pattern

    response = _use_default(http, sc, key, use_programme_default=True)

    assert response.status_code == 409, response.content
    error = response.json()["error"]
    assert (error["code"], error["details"]["reason"]) == ("PROGRAMME_DEFAULT_UNAVAILABLE", reason)
    assert said in error["message"]
    assert error["details"]["programme_meeting_pattern"]["status"] == reason
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key=key).meeting_pattern == before
    assert _pattern_audits() == []


def test_a_stale_page_never_saves_a_value_the_programmes_no_longer_hold(http):
    """WS-4: a tab drew STAT103 as differing from 2 x 100; Section Planning changed it since."""
    sc = _six_courses()
    stat = ScenarioSectionBudget.objects.get(scenario=sc, course_key=STAT)

    # Both programmes now say standard: the page's 2 x 100 is refused, and why.
    ProgrammeRequirement.objects.filter(course_code="STAT103").update(meeting_pattern="standard")
    changed = _use_default(
        http, sc, STAT, use_programme_default=True, expected_meeting_pattern="2x100"
    )
    assert changed.status_code == 409, changed.content
    error = changed.json()["error"]
    assert error["code"] == "PROGRAMME_DEFAULT_CHANGED"
    assert (error["details"]["expected"], error["details"]["meeting_pattern"]) == ("2x100", "")

    # Now they differ: there is nothing to use.
    ProgrammeRequirement.objects.filter(program="AI2", course_code="STAT103").update(
        meeting_pattern="2x100"
    )
    differ = _use_default(
        http, sc, STAT, use_programme_default=True, expected_meeting_pattern="2x100"
    )
    assert differ.status_code == 409, differ.content
    assert differ.json()["error"]["code"] == "PROGRAMME_DEFAULT_UNAVAILABLE"
    stat.refresh_from_db()
    assert stat.meeting_pattern == ""
    assert _pattern_audits() == []


def test_the_programmes_value_is_checked_as_an_explicit_one(http):
    sc = _six_courses()

    # AI2 set ENG101 to 2 x 100, but its fixed English bundle wins.
    response = _use_default(http, sc, ENGLISH, use_programme_default=True)

    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == "VALIDATION_MEETING_PATTERN"
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key=ENGLISH).meeting_pattern == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"use_programme_default": True, "meeting_pattern": "2x100"},
        {"use_programme_default": True, "meeting_pattern": ""},
        {"use_programme_default": "yes"},
        {"use_programme_default": 1},
        {"use_programme_default": False},
        {"meeting_pattern": "2x100", "expected_meeting_pattern": "2x100"},
        {"use_programme_default": True, "expected_meeting_pattern": "3x50"},
    ],
)
def test_a_body_that_mixes_or_misnames_the_modes_is_refused(http, payload):
    sc = _six_courses()

    response = _use_default(http, sc, STAT, **payload)

    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == "VALIDATION_MEETING_PATTERN"
    assert ScenarioSectionBudget.objects.get(scenario=sc, course_key=STAT).meeting_pattern == ""
    assert _pattern_audits() == []


# ── Generate hands the scheduler the budget's weekly meetings ──────────────


@pytest.fixture
def generate(http, monkeypatch):
    """A Generate whose scaffold is stubbed with three budget rows."""
    sc = _scenario(programs=("AI2",))
    ScenarioSectionBudget.objects.create(
        scenario=sc, course_key=STAT, course_code="STAT103", credit_hours=3, meeting_pattern="2x100"
    )
    _vote("AI2", "STAT103", "Principles of Statistics", "2x100")
    rows = [
        {"course_key": STAT, "course_code": "STAT103", "meeting_pattern": "2x100"},
        {"course_key": LINEAR, "course_code": "MATH243", "meeting_pattern": ""},
        {"course_key": "AI101", "course_code": "AI101"},
    ]
    scaffold = Mock(
        return_value={
            "scenario": {"id": sc.pk},
            "boards": [],
            "section_budget": rows,
            "student_summary": {"classified": 1},
        }
    )
    monkeypatch.setattr("core.timetable_workspace_views.generate_workspace_scenario", scaffold)
    submit = Mock(return_value="job-1")
    monkeypatch.setattr("core.services.planner_job_runner.submit_planner_job", submit)
    monkeypatch.setattr("core.services.planner_job_runner.dispatch_planner_job", Mock())
    run = Mock(return_value={"engine": "stub"})
    monkeypatch.setattr("core.services.planner_job_runner.run_planner_job_synchronously", run)

    def post(*, engine: str, deferred: bool):
        monkeypatch.setattr(
            "core.services.planner_job_runner.is_async_planner_enabled", lambda: deferred
        )
        body = {"year": 1448, "semester": 2, "program": "AI2", "section": "F"}
        response = _post(
            http,
            "/ops/tw/generate-workspace/",
            {**body, "engine": engine, "defer_build": deferred},
        )
        assert response.status_code == 201, response.content
        return response

    return sc, scaffold, submit, run, post


@pytest.mark.parametrize("deferred", [True, False])
def test_the_scheduler_build_is_handed_every_budget_keys_weekly_meetings(generate, deferred):
    _sc, _scaffold, submit, run, post = generate

    post(engine="scheduler", deferred=deferred)

    call = submit if deferred else run
    params = call.call_args.kwargs["params"]
    assert call.call_args.kwargs["mode"] == PlannerJob.MODE_SCHEDULER_BUILD
    assert params["meeting_patterns"] == {STAT: "2x100", LINEAR: "", "AI101": ""}


@pytest.mark.parametrize("deferred", [True, False])
def test_the_classic_build_is_handed_nothing_new(generate, deferred):
    _sc, _scaffold, submit, run, post = generate

    post(engine="classic", deferred=deferred)

    call = submit if deferred else run
    assert "meeting_patterns" not in call.call_args.kwargs["params"]


def test_a_scaffold_without_budget_rows_hands_an_empty_map(generate):
    sc, scaffold, submit, _run, post = generate
    scaffold.return_value = {
        "scenario": {"id": sc.pk},
        "boards": [],
        "student_summary": {"classified": 1},
    }

    post(engine="scheduler", deferred=True)

    assert submit.call_args.kwargs["params"]["meeting_patterns"] == {}


def test_a_synchronous_build_answers_the_budget_with_its_programmes(generate):
    _sc, _scaffold, _submit, _run, post = generate

    budget = post(engine="classic", deferred=False).json()["section_budget"]

    assert [(row["course_key"], row["meeting_pattern_drift"]) for row in budget] == [(STAT, False)]
    assert budget[0]["programme_meeting_pattern"]["status"] == "agree"
