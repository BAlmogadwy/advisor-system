"""Chat cannot authorise discarding a student's sections.

The requirement to confirm first used to live in the tool's JSON description — a
sentence addressed to the model, not a gate. `_exec_build_my_timetable` read
`bool(args.get("keep_current_sections", True))` and went straight to the solver.

Given the single Arabic word «أكد», with no prior turn establishing what was being
confirmed, a live model reasoned "this implies they want a full rebuild" and called
the capability with `keep_current_sections=false`. It rebuilt. Nothing stopped it.

The real control is on the planner draft path — `planner_drafts.issue_rebuild_token`,
hashed, one-use, bound to student + draft + version — built because a review of
PR #53 found a valid token authorising content the student had never seen. Chat is
not given a second one; it is refused and hands the student over.

Every test here asserts the SOLVER WAS NOT REACHED, not merely that the reply looked
like a refusal. A capability that refuses in its message and rebuilds anyway is the
defect wearing the fix's clothes.
"""

from __future__ import annotations

import pytest

from core.models import Course, ProgrammeRequirement, Student
from core.services.rbac import ROLE_STUDENT, ensure_role_groups
from core.services.virtual_advisor_capabilities import (
    REBUILD_REQUIRES_PLANNER_CONFIRMATION,
    get_default_registry,
)

pytestmark = pytest.mark.django_db

SID = 4970001
PROG = "RGT"


@pytest.fixture
def student():
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={
            "name": "Rebuild Gate",
            "program": PROG,
            "section": "M",
            "total_earned_credits": 60,
            "current_registered_credits": 0,
        },
    )
    for code, name in (("RG101", "Alpha"), ("RG201", "Beta")):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": name, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3, "type": "Mandatory"},
        )
    yield


@pytest.fixture
def solver_calls(monkeypatch):
    """Record every argument the solver is handed, and never actually run it.

    Patched where the capability RESOLVES it — the import is inside the function
    body, so patching the definition site is what the call actually sees.
    """
    calls: list[dict] = []

    def recording(**kwargs):
        calls.append(kwargs)
        return {"options": []}

    monkeypatch.setattr("core.services.student_planner.run_solver", recording)
    return calls


YEAR, TERM = 1448, 1


def _build(**args):
    return get_default_registry().execute(
        "build_my_timetable",
        {"student_id": SID, "academic_year": YEAR, "term": TERM, **args},
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={},
    )


def _schema():
    specs = get_default_registry().tool_schemas_for_scope({"role": ROLE_STUDENT, "student_id": SID})
    for spec in specs:
        fn = spec.get("function", spec)
        if fn.get("name") == "build_my_timetable":
            return fn
    raise AssertionError("build_my_timetable is not offered to a student")


# ── the argument is absent, or explicitly true: keep, and run ────


def test_an_omitted_argument_keeps_the_current_sections(student, solver_calls):
    _build(must_include=["RG101"])
    assert len(solver_calls) == 1, solver_calls
    assert solver_calls[0]["keep_current_sections"] is True


def test_an_explicit_true_keeps_the_current_sections(student, solver_calls):
    _build(must_include=["RG101"], keep_current_sections=True)
    assert len(solver_calls) == 1, solver_calls
    assert solver_calls[0]["keep_current_sections"] is True


# ── explicitly false: refuse, and do not reach the solver ────────


def test_an_explicit_false_never_reaches_the_solver(student, solver_calls):
    """The whole point. A refusal message with a rebuild behind it is not a fix."""
    _build(must_include=["RG101"], keep_current_sections=False)
    assert solver_calls == [], "the solver ran despite the refusal"


def test_an_explicit_false_returns_the_typed_outcome(student, solver_calls):
    out = _build(must_include=["RG101"], keep_current_sections=False)
    assert out["ok"] is False
    assert out["reason"] == REBUILD_REQUIRES_PLANNER_CONFIRMATION
    assert out["action"] == "OPEN_STUDENT_PLANNER"
    assert out["error"]


def test_the_refusal_is_distinguishable_from_every_other_failure(student, solver_calls):
    """`ok: False` alone is not a signal — `max_credits` produces one too.

    The caller has to route this ONE outcome to the planner, so it must be
    recognisable without matching on prose.
    """
    other = _build(must_include=["RG101"], max_credits="not-a-number")
    assert other["ok"] is False
    assert other.get("reason") != REBUILD_REQUIRES_PLANNER_CONFIRMATION


# ── malformed values are not silently read as "keep" ─────────────


@pytest.mark.parametrize("value", ["false", "False", 0, None, "no", "", [], 1, "true"])
def test_anything_that_is_not_exactly_true_is_refused(student, solver_calls, value):
    """`is not True`, not `is False`.

    An older client or a malformed tool call can send the STRING "false". Under
    `is False` that falls through to keep — a safe outcome reached by failing to
    parse the request, which is how a safe default stops being one. Note `1` and
    `"true"` are refused too: truthy is not the same as confirmed.
    """
    out = _build(must_include=["RG101"], keep_current_sections=value)
    assert solver_calls == [], f"{value!r} reached the solver"
    assert out["reason"] == REBUILD_REQUIRES_PLANNER_CONFIRMATION


# ── the model is no longer told the lever exists ─────────────────


def test_the_parameter_is_an_intent_signal_that_can_never_rebuild():
    """This test used to assert the parameter was ABSENT. It is back, and the
    invariant it defends is now stronger.

    Removing it was the right call at the time: it had been documented as "pass
    false ONLY after the student has confirmed", an instruction, and instructions
    are what the model got wrong when a single «أكد» triggered a rebuild. But
    hiding it also meant the model could not tell the SERVER that a rebuild was
    being asked for — so it answered the request itself, and live that produced a
    denial of a feature that exists plus advice to delete real registrations.

    The parameter is therefore an intent signal and nothing else. Supplying false
    can never rebuild: the executor forces keep=True and returns a route. What
    protects the student is the executor, which is where it always was — not the
    absence of a field, which only ever protected against a COMPLIANT caller.
    """
    properties = _schema()["parameters"]["properties"]
    assert "keep_current_sections" in properties
    assert properties["keep_current_sections"]["type"] == "boolean"
    assert "must_include" in properties, "the schema lost more than the one parameter"


def test_the_description_tells_the_model_where_confirmation_lives(student):
    """Removing the parameter without saying why invites the model to invent a
    substitute — a second `must_include` call with the current courses dropped."""
    description = _schema()["description"].lower()
    assert "planner" in description
    assert "always keeps" in description
