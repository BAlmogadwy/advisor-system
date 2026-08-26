"""Regressions for the planner build review.

Each test pins a defect that shipped. The theme is that the build trusted
things it should have derived or rejected: the client's idea of the student's
timetable, the client's numbers, the client's uniqueness, and the database's
timetable rows even when those rows were unusable.
"""

import json
import time

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
)
from core.services.planner_builder import SOLVER_BUDGET_SECONDS, build_plans
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

pytestmark = pytest.mark.django_db

PROG = "BLD"
YEAR, TERM = "1448", "1"
SID = 4460001


def _client() -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="build-hardening")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    c = Client(SERVER_NAME="localhost")
    c.force_login(user)
    return c


def _section(
    course_key: str, section: str, *, day: str | None, start: str, end: str
) -> TermSection:
    row = TermSection.objects.create(
        course_code=course_key,
        course_number="",
        course_key=course_key,
        course_name=course_key,
        section=section,
    )
    if day is not None:
        TermSectionMeeting.objects.create(term_section=row, day=day, start_time=start, end_time=end)
    return row


@pytest.fixture()
def student() -> Student:
    return Student.objects.create(
        student_id=SID, name="Build Student", program=PROG, section="M", status="ACTIVE"
    )


def _post(c: Client, **overrides) -> tuple[int, dict]:
    body = {
        "student_id": str(SID),
        "academic_year": YEAR,
        "term": TERM,
        "mode": "ignore",
        "program_sections_only": False,
        "shortlist": [],
        "baseline": [],
    }
    body.update(overrides)
    res = c.post("/ops/planner/build/", data=json.dumps(body), content_type="application/json")
    try:
        return res.status_code, res.json()
    except ValueError:  # a raw HTML 500 is itself the defect
        return res.status_code, {}


# ── broken timetable data must disqualify a section, not privilege it ────────


def test_a_section_with_no_meetings_is_not_schedulable(student: Student) -> None:
    """It used to be scheduled as 'free all week' and reported conflict-free.

    A section with no meetings cannot collide with anything, so the optimiser
    actively preferred it: the course with the worst data ranked highest.
    """
    ProgrammeRequirement.objects.create(program=PROG, course_code="GHOST", credit_hours=3)
    _section("GHOST", "M1", day=None, start="", end="")

    result = build_plans(
        YEAR, TERM, [{"course_code": "GHOST", "credits": 3, "must_take": True}], [], False
    )
    scheduled_codes = {m["course_code"] for o in result["options"] for m in o.get("mappings", [])}
    assert "GHOST" not in scheduled_codes
    assert result["summary"]["scheduled"] == 0


def test_a_section_with_a_garbled_time_is_not_schedulable(student: Student) -> None:
    """'aa:bb' is truthy, so the presence check never caught it.

    _to_minutes returns -1 for it, and _overlap reads a negative start as 'no
    valid meeting' — an invisible, always-free slot with no marker at all.
    """
    ProgrammeRequirement.objects.create(program=PROG, course_code="GARB", credit_hours=3)
    _section("GARB", "M1", day="MON", start="aa:bb", end="cc:dd")

    result = build_plans(
        YEAR, TERM, [{"course_code": "GARB", "credits": 3, "must_take": True}], [], False
    )
    scheduled_codes = {m["course_code"] for o in result["options"] for m in o.get("mappings", [])}
    assert "GARB" not in scheduled_codes


def test_a_clean_section_still_schedules(student: Student) -> None:
    """The converse: the gate must reject bad data, not good data."""
    ProgrammeRequirement.objects.create(program=PROG, course_code="GOOD", credit_hours=3)
    _section("GOOD", "M1", day="MON", start="09:00", end="09:50")

    result = build_plans(
        YEAR, TERM, [{"course_code": "GOOD", "credits": 3, "must_take": True}], [], False
    )
    assert result["summary"]["scheduled"] == 1


# ── the solvers are bounded ──────────────────────────────────────────────────


def test_the_solver_budget_is_shared_by_the_whole_request() -> None:
    """One budget per request, not per solver.

    Three methods x three variants is nine searches; a per-search cap would
    multiply into a request nine times longer than the number suggests.
    """
    assert 0 < SOLVER_BUDGET_SECONDS <= 30


#: Days used to spread the stress fixture's sections apart.
_STRESS_DAYS = ["SUN", "MON", "TUE", "WED", "THU"]


def test_a_large_shortlist_returns_within_the_budget(student: Student) -> None:
    """The budget must be load-bearing, not decorative.

    The sections here are spread across days and hours so that most
    combinations are FEASIBLE — conflicts prune the search, so a fixture whose
    sections all collide finishes fast with or without a budget and proves
    nothing. Measured on this shape: 10.0s with the budget, and still running
    after 150s without it.
    """
    codes = []
    for i in range(14):
        code = f"EXP{i}"
        codes.append(code)
        ProgrammeRequirement.objects.create(program=PROG, course_code=code, credit_hours=3)
        for s_idx in range(5):
            hour = 8 + ((i * 3 + s_idx * 2) % 10)
            _section(
                code,
                f"M{s_idx}",
                day=_STRESS_DAYS[(i + s_idx) % len(_STRESS_DAYS)],
                start=f"{hour:02d}:00",
                end=f"{hour:02d}:50",
            )

    started = time.monotonic()
    build_plans(
        YEAR,
        TERM,
        [{"course_code": c, "credits": 3} for c in codes],
        [],
        False,
        max_credits=99,
    )
    elapsed = time.monotonic() - started
    assert elapsed < SOLVER_BUDGET_SECONDS * 2.5, (
        f"build took {elapsed:.1f}s against a {SOLVER_BUDGET_SECONDS}s budget"
    )


# ── the request contract ─────────────────────────────────────────────────────


def test_student_id_is_required(student: Student) -> None:
    """Without it there is no gender or programme filter to bound the search."""
    c = _client()
    status, body = _post(c, student_id="")
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_REQUIRED_FIELDS"


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("credits", {"shortlist": [{"course_code": "CS113", "credits": "three"}]}),
        ("score", {"shortlist": [{"course_code": "CS113", "score": "high"}]}),
        ("max_credits", {"max_credits": "lots"}),
    ],
)
def test_non_numeric_fields_are_a_400_not_a_500(
    student: Student, field: str, payload: dict
) -> None:
    """These coercions sat outside the try block and returned raw HTML 500s."""
    c = _client()
    status, body = _post(c, **payload)
    assert status == 400, f"{field} should be rejected, not crash"
    assert body["error"]["code"] == "VALIDATION_NUMERIC"


def test_a_duplicate_course_is_rejected(student: Student) -> None:
    """Duplicates made method A infeasible and let B/C double-book one course."""
    c = _client()
    status, body = _post(c, shortlist=[{"course_code": "CS113"}, {"course_code": "cs113"}])
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_SHORTLIST_DUPLICATE"


def test_an_oversized_shortlist_is_rejected(student: Student) -> None:
    c = _client()
    status, body = _post(c, shortlist=[{"course_code": f"X{i}"} for i in range(41)])
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_SHORTLIST_SIZE"


# ── the baseline is the server's, not the client's ───────────────────────────


def test_keep_mode_uses_the_database_baseline_not_the_request_body(
    student: Student,
) -> None:
    """The client's baseline is ignored entirely.

    'Keep Registered' promises the build avoids the student's real timetable.
    Taking that list from the body made the promise only as good as whatever
    the browser last held — a stale tab scheduled straight over registrations.
    """
    ProgrammeRequirement.objects.create(program=PROG, course_code="WANT", credit_hours=3)
    # The student is really registered MON 09:00, recorded by the registrar.
    busy = _section("BUSY", "M1", day="MON", start="09:00", end="09:50")
    StudentTermSection.objects.create(
        student_id=SID,
        term_section=busy,
        academic_year=YEAR,
        term=TERM,
        source="scraper_timetable",
    )
    # The only WANT section collides with it.
    _section("WANT", "M1", day="MON", start="09:00", end="09:50")

    c = _client()
    # The client claims the student has nothing registered.
    status, body = _post(
        c,
        mode="keep",
        baseline=[],
        shortlist=[{"course_code": "WANT", "credits": 3, "must_take": True}],
    )
    assert status == 200
    scheduled = {m["course_code"] for o in body.get("options", []) for m in o.get("mappings", [])}
    assert "WANT" not in scheduled, (
        "the build honoured the client's empty baseline instead of the registrar's"
    )


def test_a_junk_baseline_in_the_body_cannot_crash_the_build(student: Student) -> None:
    """It is not read at all now, so its shape cannot matter."""
    c = _client()
    status, _ = _post(c, mode="keep", baseline=["junk", 42, None])
    assert status == 200
