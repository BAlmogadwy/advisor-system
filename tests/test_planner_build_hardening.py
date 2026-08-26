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
    TermSectionProgram,
)
from core.services import planner_builder
from core.services.planner_builder import SOLVER_BUDGET_SECONDS, build_plans
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.virtual_advisor_capabilities import _translate_unplaced

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


def test_all_clash_reason_keeps_its_structured_adviser_code(student: Student) -> None:
    """Changing explanatory prose must not silently turn a clash into OTHER."""
    ProgrammeRequirement.objects.create(program=PROG, course_code="CLASH", credit_hours=3)
    _section("CLASH", "M1", day="MON", start="09:00", end="09:50")
    baseline = [
        {
            "course_key": "BUSY",
            "section": "M1",
            "day": "MON",
            "start_time": "09:00",
            "end_time": "09:50",
            "term_section_id": 999,
        }
    ]

    result = build_plans(
        YEAR,
        TERM,
        [{"course_code": "CLASH", "credits": 3, "must_take": True}],
        baseline,
        True,
    )

    assert result["options"] == []
    [unplaced] = result["unscheduled"]
    assert unplaced["reason_code"] == "ALL_SECTIONS_CLASH"
    assert _translate_unplaced(unplaced["reason"], unplaced["reason_code"])[0] == (
        "ALL_SECTIONS_CLASH"
    )


# ── the solvers are bounded ──────────────────────────────────────────────────


def test_the_solver_budget_is_shared_by_the_whole_request() -> None:
    """One budget per request, not per solver.

    Three methods x three variants is nine searches; a per-search cap would
    multiply into a request nine times longer than the number suggests.
    """
    assert 0 < SOLVER_BUDGET_SECONDS <= 30


def test_cp_sat_uses_the_shared_remaining_budget(
    student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alternative CP-SAT runs must not each receive a fresh eight seconds."""
    ProgrammeRequirement.objects.create(program=PROG, course_code="BUDGET", credit_hours=3)
    _section("BUDGET", "M1", day="MON", start="09:00", end="09:50")
    observed_limits: list[float] = []

    class ExpiringDeadline:
        def __init__(self, _seconds: float) -> None:
            self.expired = False

        def reached(self) -> bool:
            return self.expired

        def remaining(self) -> float:
            return 0.25

    class Parameters:
        max_time_in_seconds: float
        num_search_workers: int
        randomize_search: bool
        random_seed: int

    class RecordingSolver:
        def __init__(self) -> None:
            self.parameters = Parameters()

        def Solve(self, _model: object) -> int:  # noqa: N802 - OR-Tools API name
            observed_limits.append(self.parameters.max_time_in_seconds)
            return int(planner_builder.cp_model.UNKNOWN)

    monkeypatch.setattr(planner_builder, "_SolverDeadline", ExpiringDeadline)
    monkeypatch.setattr(planner_builder.cp_model, "CpSolver", RecordingSolver)

    result = build_plans(
        YEAR,
        TERM,
        [{"course_code": "BUDGET", "credits": 3, "must_take": True}],
        [],
        False,
    )

    assert observed_limits == [0.25]
    assert result["options"] == []
    assert result["unscheduled"][0]["reason_code"] == "SEARCH_BUDGET_EXHAUSTED"
    assert result["summary"]["hard_constraint_failures"] == []


def test_bitmask_timeout_is_not_reported_as_infeasible(
    student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired DFS search has not proved a must-take course impossible."""
    ProgrammeRequirement.objects.create(program=PROG, course_code="DFS", credit_hours=3)
    _section("DFS", "M1", day="MON", start="09:00", end="09:50")
    calls: list[str] = []

    def no_cp_result(*_args: object, **_kwargs: object) -> tuple[list[dict], list[dict]]:
        calls.append("A")
        # Satisfy the must-take check without emitting an option signature, so
        # this test isolates method B's timeout classification.
        return [{"course_code": "DFS", "course_key": "DFS", "term_section_id": 0}], []

    def expired_dfs(
        *_args: object, deadline: planner_builder._SolverDeadline, **_kwargs: object
    ) -> tuple[list[dict], list[dict]]:
        calls.append("B")
        deadline.expired = True
        return [], [
            {
                "course_code": "DFS",
                "reason": "Could not fit with chosen constraints/objective",
                "details": [],
            }
        ]

    def forbidden_c(*_args: object, **_kwargs: object) -> tuple[list[dict], list[dict]]:
        raise AssertionError("method C must not start after the shared deadline expires")

    monkeypatch.setattr(planner_builder, "_cp_build_option", no_cp_result)
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_b", expired_dfs)
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_c", forbidden_c)

    result = build_plans(
        YEAR,
        TERM,
        [{"course_code": "DFS", "credits": 3, "must_take": True}],
        [],
        False,
    )

    assert calls == ["A", "B"]
    assert result["options"] == []
    assert result["unscheduled"][0]["reason_code"] == "SEARCH_BUDGET_EXHAUSTED"
    assert result["summary"]["hard_constraint_failures"] == []


def test_valid_best_so_far_survives_the_deadline_without_a_fake_conflict(
    student: Student, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete plan found at the boundary stays complete and conflict-free."""
    ProgrammeRequirement.objects.create(program=PROG, course_code="FOUND", credit_hours=3)
    section = _section("FOUND", "M1", day="MON", start="09:00", end="09:50")

    def no_cp_result(*_args: object, **_kwargs: object) -> tuple[list[dict], list[dict]]:
        return [{"course_code": "FOUND", "course_key": "FOUND", "term_section_id": 0}], []

    def completed_dfs(
        *_args: object, deadline: planner_builder._SolverDeadline, **_kwargs: object
    ) -> tuple[list[dict], list[dict]]:
        deadline.expired = True
        return [
            {
                "course_code": "FOUND",
                "course_key": "FOUND",
                "section": "M1",
                "term_section_id": section.id,
                "meetings": [],
            }
        ], []

    def forbidden_c(*_args: object, **_kwargs: object) -> tuple[list[dict], list[dict]]:
        raise AssertionError("method C must not start after the shared deadline expires")

    monkeypatch.setattr(planner_builder, "_cp_build_option", no_cp_result)
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_b", completed_dfs)
    monkeypatch.setattr(planner_builder, "_bitmask_build_option_c", forbidden_c)

    result = build_plans(
        YEAR,
        TERM,
        [{"course_code": "FOUND", "credits": 3, "must_take": True}],
        [],
        False,
    )

    [option] = result["options"]
    assert option["name"] == "B1"
    assert option["unscheduled"] == []
    assert result["summary"]["scheduled"] == result["summary"]["target"] == 1
    assert result["summary"]["conflicts"] == 0


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


# ── the write path reports what it did ───────────────────────────────────────


def _applyable(course_key: str, section: str, day: str, start: str, end: str) -> TermSection:
    """A section the student is actually allowed to be given."""
    row = _section(course_key, section, day=day, start=start, end=end)
    TermSectionProgram.objects.create(term_section=row, program=PROG)
    return row


def test_apply_reports_the_planner_sections_it_removed(student: Student) -> None:
    """A REPLACE that says nothing about what it replaced.

    Applying option B after option A silently deleted A's courses: the write is
    scoped to the planner snapshot class and removes anything not in the option,
    with no mention in the response.
    """
    old = _applyable("OLDC", "M1", "SUN", "09:00", "09:50")
    new = _applyable("NEWC", "M1", "MON", "09:00", "09:50")
    StudentTermSection.objects.create(
        student_id=SID, term_section=old, academic_year=YEAR, term=TERM, source="planner"
    )

    c = _client()
    res = c.post(
        "/ops/planner/save-student-sections/",
        data=json.dumps(
            {
                "student_id": str(SID),
                "academic_year": YEAR,
                "term": TERM,
                "term_section_ids": [new.id],
                "confirm_replace": True,
            }
        ),
        content_type="application/json",
    )
    assert res.status_code == 200, res.content[:300]
    body = res.json()
    assert body["removed_count"] == 1
    assert body["removed"][0]["course_code"] == "OLDC"


def test_apply_reports_every_working_section_the_replace_removes(student: Student) -> None:
    """Auto-mapped rows share the write's WORKING class and must not disappear silently."""
    auto_mapped = _applyable("AUTOC", "M1", "SUN", "09:00", "09:50")
    registrar = _applyable("REGC", "M1", "TUE", "09:00", "09:50")
    replacement = _applyable("NEWC", "M1", "MON", "09:00", "09:50")
    StudentTermSection.objects.create(
        student_id=SID,
        term_section=auto_mapped,
        academic_year=YEAR,
        term=TERM,
        source="auto_from_studying",
    )
    registrar_link = StudentTermSection.objects.create(
        student_id=SID,
        term_section=registrar,
        academic_year=YEAR,
        term=TERM,
        source="scraper_timetable",
    )

    c = _client()
    res = c.post(
        "/ops/planner/save-student-sections/",
        data=json.dumps(
            {
                "student_id": str(SID),
                "academic_year": YEAR,
                "term": TERM,
                "term_section_ids": [replacement.id],
                "confirm_replace": True,
            }
        ),
        content_type="application/json",
    )

    assert res.status_code == 200, res.content[:300]
    body = res.json()
    assert body["removed_count"] == 1
    assert body["removed"] == [
        {
            "term_section_id": auto_mapped.id,
            "course_code": "AUTOC",
            "section": "M1",
        }
    ]
    assert not StudentTermSection.objects.filter(
        student_id=SID, term_section=auto_mapped, academic_year=YEAR, term=TERM
    ).exists()
    assert StudentTermSection.objects.filter(
        student_id=SID,
        term_section=replacement,
        academic_year=YEAR,
        term=TERM,
        source="planner",
    ).exists()
    assert StudentTermSection.objects.filter(pk=registrar_link.pk).exists()


def test_apply_reports_a_clash_with_the_registered_timetable(student: Student) -> None:
    """The build ran against the timetable as it was THEN.

    Between building and applying, the registrar snapshot can move. Applying a
    stale option used to write the clash with nothing checking; it is now
    reported alongside the result (reported, not blocked - an adviser may plan
    a change they intend to make at the registrar).
    """
    registered = _applyable("REGC", "M1", "MON", "09:00", "09:50")
    StudentTermSection.objects.create(
        student_id=SID,
        term_section=registered,
        academic_year=YEAR,
        term=TERM,
        source="scraper_timetable",
    )
    clashing = _applyable("PLNC", "M1", "MON", "09:30", "10:20")

    c = _client()
    res = c.post(
        "/ops/planner/save-student-sections/",
        data=json.dumps(
            {
                "student_id": str(SID),
                "academic_year": YEAR,
                "term": TERM,
                "term_section_ids": [clashing.id],
                "confirm_replace": True,
            }
        ),
        content_type="application/json",
    )
    assert res.status_code == 200, res.content[:300]
    clashes = res.json()["clashes_with_registered"]
    assert len(clashes) == 1
    assert clashes[0]["course_code"] == "PLNC"
    assert clashes[0]["clashes_with"] == "REGC"
