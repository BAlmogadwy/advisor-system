from __future__ import annotations

import json
from typing import Any

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableScenario,
)
from core.services.rbac import ROLE_STUDENT
from core.services.student_advisor_v2 import _normalise_timetable_proposal_args
from core.services.student_planner import PlannerRequest, build_student_options
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 4987101


@pytest.fixture
def timetable_world() -> dict[str, Any]:
    """A student-scoped catalogue with every pin boundary represented.

    The valid pin and each invalid lookalike deliberately share the requested
    course wherever possible.  A resolver that checks only ``course + label`` or
    only the section's leading M/F letter will therefore choose a real but
    unauthorised row and make one of the focused tests fail.
    """
    student = Student.objects.create(
        student_id=SID,
        name="Hard constraint student",
        program="AI",
        section="M",
        status="active",
    )

    for code, credits in (("REQ101", 3), ("OPT101", 3), ("REQ404", 3), ("OTH101", 3)):
        Course.objects.create(
            course_code=code,
            description=f"{code} test course",
            credit_hours=credits,
        )
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=f"{code} test course",
            type="Mandatory",
            programme_term=1,
            credit_hours=credits,
        )

    made: dict[str, TermSection] = {}

    def section(
        key: str,
        course_code: str,
        label: str,
        *,
        day: str,
        program: str | None = "AI",
        scenario: TimetableScenario | None = None,
    ) -> TermSection:
        row = TermSection.objects.create(
            scenario=scenario,
            course_code=course_code,
            course_number="",
            course_key=course_code,
            course_name=f"{course_code} test course",
            section=label,
            available_capacity=30,
            registered_count=0,
        )
        if program is not None:
            TermSectionProgram.objects.create(term_section=row, program=program)
        TermSectionMeeting.objects.create(
            term_section=row,
            day=day,
            start_time="09:00",
            end_time="10:00",
        )
        made[key] = row
        return row

    section("required_free", "REQ101", "M1", day="SUN")
    section("required_pin", "REQ101", "M2", day="MON")
    section("optional", "OPT101", "M1", day="TUE")
    section("wrong_program", "REQ101", "M3", day="WED", program="DS")
    section("wrong_gender", "REQ101", "F2", day="THU")
    section("wrong_course", "OTH101", "M5", day="WED")

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name="Private planner scenario",
    )
    section("scenario_only", "REQ101", "M4", day="THU", scenario=scenario)

    return {"student": student, "sections": made}


def _course_map(alternative: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("course_code") or ""): row for row in alternative.get("courses") or []}


def test_required_course_and_exact_pin_hold_in_every_student_alternative(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The student adapter must preserve both hard constraints through A/B/C.

    This is intentionally above the lower-level builder tests: a perfect builder
    still gives the wrong answer if ``PlannerRequest.required_courses`` or
    ``fixed_sections`` is dropped while translating the student request.
    """
    monkeypatch.setattr(
        "core.services.student_planner.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )
    pinned = timetable_world["sections"]["required_pin"]
    unpinned = timetable_world["sections"]["required_free"]

    result = build_student_options(
        PlannerRequest(
            student_id=SID,
            year=1448,
            term=1,
            must_include=("REQ101", "OPT101"),
            required_courses=("REQ101",),
            keep_current_sections=False,
            max_credits=18,
            include_recommendations=False,
            fixed_sections=(("REQ101", pinned.id),),
        )
    )

    assert result["alternatives"], result
    for alternative in result["alternatives"]:
        courses = _course_map(alternative)
        assert courses["REQ101"]["section"] == "M2"
        assert courses["REQ101"]["term_section_id"] == pinned.id
        assert all(
            row.get("term_section_id") != unpinned.id for row in alternative.get("courses") or []
        )


def test_impossible_required_course_emits_no_partial_alternative(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedulable optional course cannot disguise a missing hard course."""
    monkeypatch.setattr(
        "core.services.student_planner.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    result = build_student_options(
        PlannerRequest(
            student_id=SID,
            year=1448,
            term=1,
            must_include=("REQ404", "OPT101"),
            required_courses=("REQ404",),
            keep_current_sections=False,
            max_credits=18,
            include_recommendations=False,
        )
    )

    assert result["alternatives"] == []
    assert result["generated"] == 0
    assert result["reason"] == "NO_VALID_TIMETABLE"
    assert {row["course_code"] for row in result["unplaced"]} >= {"REQ404"}


def _proposal_args(section_label: str) -> dict[str, Any]:
    return {
        "mode": "from_scratch",
        "course_codes": ["REQ101", "OPT101"],
        "must_take_courses": ["REQ101"],
        "pinned_sections": [{"course_code": "REQ101", "section_label": section_label}],
        "max_credits": 18,
        "academic_year": 1448,
        "term": 1,
    }


def _execute_proposal(args: dict[str, Any]) -> dict[str, Any]:
    return get_default_registry().execute(
        "build_timetable_proposal",
        args,
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": 1448, "term": 1},
    )


def test_capability_resolves_label_forwards_hard_contract_and_returns_no_ids(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model names a label; only the server may turn it into a database id."""
    captured: list[PlannerRequest] = []
    pinned = timetable_world["sections"]["required_pin"]

    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    def fake_build(request: PlannerRequest) -> dict[str, Any]:
        captured.append(request)
        return {
            "generated": 1,
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "courses": [
                        {
                            "course_code": "REQ101",
                            "section": "M2",
                            "credits": 3,
                            "term_section_id": pinned.id,
                        }
                    ],
                    "meetings": [
                        {
                            "course_code": "REQ101",
                            "section": "M2",
                            "day": "MON",
                            "start": "09:00",
                            "end": "10:00",
                            "term_section_id": pinned.id,
                        }
                    ],
                    "credit_hours": 3,
                    "scheduled_courses": 1,
                    "target_courses": 2,
                    "unplaced": [
                        {
                            "course_code": "OPT101",
                            "reason_code": "OMITTED_IN_THIS_VARIANT",
                            "reason": "Omitted only from this alternative.",
                        }
                    ],
                    "days_on_campus": 1,
                    "days": ["MON"],
                    "earliest_start": "09:00",
                    "latest_end": "10:00",
                }
            ],
            "unplaced": [],
        }

    monkeypatch.setattr("core.services.student_planner.build_student_options", fake_build)

    out = _execute_proposal(_proposal_args("m2"))

    assert out["ok"] is True, out
    assert len(captured) == 1
    request = captured[0]
    assert request.must_include == ("REQ101", "OPT101")
    assert request.required_courses == ("REQ101",)
    assert dict(request.fixed_sections) == {"REQ101": pinned.id}

    # Safe evidence is explicit enough for the answer model to state the hard
    # contract, but contains labels rather than internal primary keys.
    assert out["must_take_courses"] == ["REQ101"]
    assert out["pinned_sections"] == [{"course_code": "REQ101", "section_label": "M2"}]
    assert out["constraints_satisfied"] is True
    assert out["constraint_failures"] == []
    assert "term_section_id" not in json.dumps(out, ensure_ascii=False)
    assert out["can_save"] is False
    assert out["can_register"] is False


def test_same_retained_pin_is_satisfied_without_a_duplicate_baseline_option(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = timetable_world["sections"]["required_pin"]
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=pinned,
        source="registration_plan_import",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    out = _execute_proposal(
        {
            "mode": "around_current",
            "course_codes": ["REQ101"],
            "must_take_courses": ["REQ101"],
            "pinned_sections": [{"course_code": "REQ101", "section_label": "M2"}],
        }
    )

    assert out["ok"] is True
    assert out["constraints_satisfied"] is True
    assert out["baseline_credit_hours"] == 3
    assert out["alternatives"] == []
    assert out["alternatives_generated"] == 0
    assert out["no_additional_courses"] is True


def test_around_current_proposal_separates_retained_and_added_totals(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = timetable_world["sections"]["required_free"]
    added = timetable_world["sections"]["optional"]
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=retained,
        source="mapped",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )

    def combined_builder(_request: PlannerRequest) -> dict[str, Any]:
        return {
            "generated": 1,
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "courses": [
                        {
                            "course_code": "REQ101",
                            "section": "M1",
                            "credits": 3,
                            "source": "current",
                            "term_section_id": retained.id,
                        },
                        {
                            "course_code": "OPT101",
                            "section": "M1",
                            "credits": 3,
                            "source": "proposed",
                            "term_section_id": added.id,
                        },
                    ],
                    "meetings": [
                        {
                            "course_code": "REQ101",
                            "section": "M1",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:00",
                            "source": "current",
                        },
                        {
                            "course_code": "OPT101",
                            "section": "M1",
                            "day": "TUE",
                            "start": "09:00",
                            "end": "10:00",
                            "source": "proposed",
                        },
                    ],
                    "credit_hours": 6,
                    "scheduled_courses": 2,
                    "target_courses": 2,
                    "course_count": 2,
                    "unplaced": [],
                    "days_on_campus": 2,
                    "days": ["SUN", "TUE"],
                    "earliest_start": "09:00",
                    "latest_end": "10:00",
                }
            ],
            "unplaced": [],
        }

    monkeypatch.setattr("core.services.student_planner.build_student_options", combined_builder)
    out = _execute_proposal({"mode": "around_current", "course_codes": ["OPT101"]})

    option = out["alternatives"][0]
    assert [row["course_code"] for row in option["courses"]] == ["OPT101"]
    assert [row["course_code"] for row in option["meetings"]] == ["OPT101"]
    assert option["scheduled_courses"] == 1
    assert option["target_courses"] == 1
    assert option["course_count"] == 2
    assert option["proposed_credit_hours"] == 3
    assert option["total_credit_hours"] == 6


def test_capability_exposes_impossible_hard_request_without_a_partial_result(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )
    args = {
        "mode": "from_scratch",
        "course_codes": ["REQ404", "OPT101"],
        "must_take_courses": ["REQ404"],
        "max_credits": 18,
        "academic_year": 1448,
        "term": 1,
    }

    out = _execute_proposal(args)

    assert out["ok"] is True, out
    assert out["constraints_satisfied"] is False
    assert out["alternatives"] == []
    assert any(row.get("course_code") == "REQ404" for row in out.get("constraint_failures") or [])
    assert "term_section_id" not in json.dumps(out, ensure_ascii=False)


@pytest.mark.parametrize(
    ("label", "boundary"),
    [
        ("M9", "missing section"),
        ("M5", "label belongs to another course"),
        ("M3", "section belongs to another programme"),
        ("F2", "section belongs to the other gender cohort"),
        ("M4", "scenario-owned section is not in the current global snapshot"),
    ],
)
def test_capability_rejects_unresolvable_or_unauthorised_pin_before_solver(
    timetable_world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    boundary: str,
) -> None:
    calls: list[PlannerRequest] = []

    def should_not_run(request: PlannerRequest) -> dict[str, Any]:
        calls.append(request)
        return {"alternatives": [], "unplaced": [], "generated": 0}

    monkeypatch.setattr("core.services.student_planner.build_student_options", should_not_run)
    out = _execute_proposal(_proposal_args(label))

    assert out["ok"] is False, boundary
    assert out["constraints_satisfied"] is False
    assert out["constraint_failures"], boundary
    assert calls == [], f"{boundary} reached the solver"
    assert "term_section_id" not in json.dumps(out, ensure_ascii=False)


def test_capability_rejects_model_supplied_section_id(
    timetable_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[PlannerRequest] = []
    pinned = timetable_world["sections"]["required_pin"]

    monkeypatch.setattr(
        "core.services.student_planner.build_student_options",
        lambda request: calls.append(request) or {},
    )
    args = _proposal_args("M2")
    args["pinned_sections"][0]["term_section_id"] = pinned.id

    out = _execute_proposal(args)

    assert out["ok"] is False
    assert calls == []
    assert "term_section_id" not in json.dumps(out, ensure_ascii=False)


def test_capability_schema_exposes_labels_not_database_ids() -> None:
    capability = get_default_registry().capabilities["build_timetable_proposal"]
    properties = capability.parameters["properties"]

    assert properties["must_take_courses"]["type"] == "array"
    pins = properties["pinned_sections"]
    assert pins["type"] == "array"
    item = pins["items"]
    assert set(item["required"]) == {"course_code", "section_label"}
    assert item["additionalProperties"] is False
    assert "term_section_id" not in item["properties"]


@pytest.mark.parametrize(
    "question",
    [
        ("I must take AI331, and pin section M2 for AI331. Build around my current timetable."),
        "لازم آخذ AI331، وثبّت لي شعبة M2 لمقرر AI331، وابنِ الجدول حول جدولي الحالي.",
    ],
)
def test_v2_normalises_explicit_must_take_and_pin_in_arabic_and_english(
    question: str,
) -> None:
    args, reasons = _normalise_timetable_proposal_args(question, {})

    assert args["course_codes"] == ["AI331"]
    assert args["must_take_courses"] == ["AI331"]
    assert args["pinned_sections"] == [{"course_code": "AI331", "section_label": "M2"}]
    assert args["mode"] == "around_current"
    assert "explicit_must_take_courses" in reasons
    assert "explicit_pinned_sections" in reasons


@pytest.mark.parametrize(
    "question",
    [
        "Pin section M2 and build around my current timetable.",
        "ثبّت لي شعبة M2 وابنِ الجدول حول جدولي الحالي.",
        "Pin sections M1 and M2 for AI331 and build a timetable.",
        "ثبّت شعبتي M1 وM2 لمقرر AI331 وابنِ جدولًا.",
    ],
)
def test_v2_never_guesses_an_ambiguous_pin(question: str) -> None:
    args, _reasons = _normalise_timetable_proposal_args(question, {})

    assert not args.get("pinned_sections")
    assert args.get("_constraint_input_error") == "AMBIGUOUS_PIN"


def test_pin_and_build_implies_the_exact_section_is_required() -> None:
    args, _reasons = _normalise_timetable_proposal_args(
        "Pin section M2 for AI331 and build around my current timetable.",
        {},
    )

    assert args["course_codes"] == ["AI331"]
    assert args["pinned_sections"] == [{"course_code": "AI331", "section_label": "M2"}]
    assert args["must_take_courses"] == ["AI331"]


def test_remote_projection_keeps_public_constraints_and_drops_section_ids() -> None:
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    projected = project_tool_result_for_remote(
        "build_timetable_proposal",
        {
            "tool": "build_timetable_proposal",
            "ok": True,
            "must_take_courses": ["AI331"],
            "pinned_sections": [
                {
                    "course_code": "AI331",
                    "section_label": "M2",
                    "term_section_id": 9812,
                }
            ],
            "constraints_satisfied": False,
            "constraint_failures": [
                {
                    "course_code": "AI331",
                    "section_label": "M2",
                    "reason": "The exact section clashes with a retained section.",
                    "term_section_id": 9812,
                }
            ],
        },
        RemoteIdentityMap(),
    )

    assert projected["must_take_courses"] == ["AI331"]
    assert projected["pinned_sections"] == [{"course_code": "AI331", "section_label": "M2"}]
    assert projected["constraint_failures"][0]["reason"].startswith("The exact")
    assert projected["constraints_satisfied"] is False
    assert "term_section_id" not in json.dumps(projected)


def test_constraint_failure_has_a_safe_structured_timetable_presentation() -> None:
    from core.services.advisor_presentations import normalise_presentation

    presentation = normalise_presentation(
        {
            "kind": "timetable_proposals",
            "mode": "from_scratch",
            "baseline_kind": "EMPTY",
            "alternatives": [],
            "must_take_courses": ["AI331"],
            "pinned_sections": [
                {
                    "course_code": "AI331",
                    "section_label": "M2",
                    "term_section_id": 9812,
                }
            ],
            "constraints_satisfied": False,
            "constraint_failures": [
                {
                    "course_code": "AI331",
                    "section_label": "M2",
                    "reason": "No valid option satisfies this exact pin.",
                    "reason_code": "INTERNAL",
                }
            ],
        }
    )

    assert presentation["kind"] == "timetable_proposals"
    assert presentation["alternatives"] == []
    assert presentation["must_take_courses"] == ["AI331"]
    assert presentation["pinned_sections"] == [{"course_code": "AI331", "section_label": "M2"}]
    assert presentation["constraint_failures"][0]["reason"].startswith("No valid")
    assert "term_section_id" not in json.dumps(presentation)
    assert "reason_code" not in json.dumps(presentation)
