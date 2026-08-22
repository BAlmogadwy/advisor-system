"""Every capability, driven through the real loop, with the request captured.

WHY A MATRIX AND NOT A PER-PROJECTOR TEST

A projector test asserts what a function returns. This asserts what a PROVIDER
receives, which is a different claim: it goes through the registry, the boundary,
the message builder and `json.dumps`, and it is the only form of the claim that
stays true when somebody adds a serialisation step in between.

HOW IT FINDS WHAT A HAND-WRITTEN TEST WOULD MISS

Each capability's local result is seeded with CANARIES — a name, a student id, an
email, a phone number, an adviser id, a risk score, an internal note — planted at
several depths, including inside lists and nested objects. The assertion is not
"the fields I thought of are gone"; it is "no canary survives anywhere in the
serialised request". A projector that copies an unexpected sub-object, or a new
`json.dumps` that reaches past the projection, fails here without anybody having
predicted the shape it would take.

`test_every_registry_capability_is_covered` is what keeps this honest: a new
capability with no entry in `REMOTE_POLICY` and no row here fails immediately,
rather than being silently untested until someone notices.
"""

from __future__ import annotations

import json

import pytest

from core.models import Student
from core.services import virtual_advisor as va
from core.services import virtual_advisor_capabilities as caps
from core.services.advisor_remote_boundary import RemoteToolBoundary
from core.services.llm_remote_privacy import (
    REMOTE_POLICY,
    RemoteExposure,
    RemoteIdentityMap,
)
from core.services.rbac import ROLE_ADVISOR, ROLE_STUDENT
from tests.test_llm_remote_execution_boundary import (
    ExecutionSpy,
    ScriptedClient,
    _call,
)

pytestmark = pytest.mark.django_db

MINE = 4502156
OTHER = 4502157
FIXED = "TESTNONCE0001"

STUDENT_SCOPE = {"role": ROLE_STUDENT, "student_id": MINE}
ADVISER_SCOPE = {"role": ROLE_ADVISOR, "advisor_id": "ADV-1", "departments": ["AI"]}

#: Planted in every seeded result. Each is a thing the owner's brief lists as
#: never-send; each is distinctive enough that finding it in a request is proof
#: rather than coincidence.
CANARIES = {
    "name": "عبدالله بن محمد القحطاني",
    "latin_name": "Abdullah Alqahtani",
    "student_id": str(MINE),
    "other_student_id": str(OTHER),
    "email": "s4502156@taibahu.edu.sa",
    "phone": "0551234567",
    "advisor_id": "ADV-CANARY-77",
    "risk_score": "0.8731",
    "needs_attention": "CANARY_NEEDS_ATTENTION",
    "internal_note": "CANARY_RUNTIME_USE_NOTE",
    "national_id": "1098765432",
}


def _poisoned(tool: str) -> dict:
    """A plausible result for `tool`, with a canary wherever one could hide.

    Deliberately over-stuffed and slightly wrong-shaped in places. A projector
    that reads the fields it expects produces a clean payload from this; one that
    copies a branch wholesale does not.
    """
    student_block = {
        "student_id": MINE,
        "name": CANARIES["name"],
        "email": CANARIES["email"],
        "phone": CANARIES["phone"],
        "advisor_id": CANARIES["advisor_id"],
        "national_id": CANARIES["national_id"],
        "status": "ACTIVE",
        "program": "AI",
        "section": "M",
        "gpa": 3.4,
        "total_earned_credits": 78,
    }
    common = {
        "tool": tool,
        "ok": True,
        "student_id": MINE,
        "name": CANARIES["name"],
        "advisor_name": CANARIES["latin_name"],
        "advisor_email": CANARIES["email"],
        "risk_score": float(CANARIES["risk_score"]),
        "needs_attention": CANARIES["needs_attention"],
        "runtime_use_note": CANARIES["internal_note"],
        "_debug": {"raw_row": student_block, "trace": [CANARIES["email"]]},
    }
    shapes: dict[str, dict] = {
        "get_student_context": {"student_context": {"student": student_block}},
        "find_students": {
            "count": 2,
            "rows": [
                {**student_block, "student_id": MINE},
                {**student_block, "student_id": OTHER, "name": CANARIES["latin_name"]},
            ],
        },
        "my_advisor": {
            "advisor": {
                "advisor_id": CANARIES["advisor_id"],
                "name": CANARIES["latin_name"],
                "email": CANARIES["email"],
                "phone": CANARIES["phone"],
                "office": "B12",
            }
        },
        "my_timetable": {
            "meetings": [
                {
                    "course_code": "AI221",
                    "day": "SUN",
                    "start": "09:00",
                    "instructor": CANARIES["latin_name"],
                    "instructor_email": CANARIES["email"],
                    "room": "2-14",
                }
            ]
        },
        "policy_lookup": {
            "policies": [
                {
                    "policy_id": "P-001",
                    "statement_ar": "نص اللائحة",
                    "decision_use": "PERMITTED",
                    "runtime_use_note": CANARIES["internal_note"],
                    "notes": CANARIES["internal_note"],
                    "citation": {"policy_id": "P-001", "page": 23},
                    "authority": {
                        "level": "UNIVERSITY",
                        "approved_by": CANARIES["latin_name"],
                        "approved_at": "2026-01-01",
                    },
                }
            ],
            "direct_policy_evidence": [],
            "citable": [{"policy_id": "P-001", "page": 23}],
        },
        "lookup_course": {
            "courses": [
                {
                    "course_code": "AI221",
                    "credit_hours": 3,
                    "created_by": CANARIES["latin_name"],
                    "owner_email": CANARIES["email"],
                }
            ]
        },
        "course_prerequisites": {
            "course_code": "AI221",
            "options": [{"course_code": "AI111", "added_by": CANARIES["latin_name"]}],
            "per_program": [{"program": "AI", "reviewer": CANARIES["latin_name"]}],
        },
        "course_choice_comparison": {
            "program": "AI",
            "academic_year": 1448,
            "term": 1,
            "objective": "balanced",
            "baseline_kind": "REGISTERED",
            "verdict": "PREFERRED",
            "preferred_course": "AI331",
            "criterion_leaders": {"direct_unlock": ["AI331"]},
            "candidates": [
                {
                    "course_code": "AI331",
                    "course_name": "Knowledge Representation",
                    "academic_status": "open_now",
                    "prerequisite_ready": True,
                    "missing_prerequisites": [],
                    "recommendation": {"state": "RECOMMENDED", "rank": 1},
                    "impact": {"direct_unlock_count": 3, "reviewer": CANARIES["name"]},
                    "timetable": {"status": "OK", "sections_on_file": 2},
                    "graduation": {
                        "simulation_completed": True,
                        "estimated_additional_terms": 4,
                    },
                    "student_id": MINE,
                    "advisor_email": CANARIES["email"],
                }
            ],
        },
        "feasible_course_replacements": {
            "academic_year": 1448,
            "term": 1,
            "baseline_kind": "REGISTERED",
            "requested_remove_course": "DS341",
            "requested_add_course": "AI331",
            "status": "CERTIFIED_SWAPS_FOUND",
            "academic_search": {
                "pairs_evaluated": 12,
                "search_truncated": False,
                "academic_improvements_found": 2,
                "private_student_id": MINE,
            },
            "certification_search": {
                "academic_candidates_received": 2,
                "timetable_candidates_checked": 2,
                "certified_result_limit": 5,
                "search_truncated": False,
                "reviewed_by": CANARIES["latin_name"],
            },
            "certified_replacements": [
                {
                    "remove_course": {
                        "course_code": "DS341",
                        "course_name": "Data Privacy",
                        "credits": 3,
                        "student_id": MINE,
                    },
                    "add_course": {
                        "course_code": "AI331",
                        "course_name": "Knowledge Representation",
                        "credits": 3,
                        "owner_email": CANARIES["email"],
                    },
                    "academic_improvement": {
                        "proven_improvement": True,
                        "timing_effect": "EARLIER",
                        "terms_saved": 1,
                        "blockers_resolved": ["AI401"],
                        "internal_note": CANARIES["internal_note"],
                    },
                    "timetable": {
                        "status": "COMPLETE_CLASH_FREE",
                        "certified_options": [
                            {
                                "planner_options": ["A1", {"owner": CANARIES["name"]}],
                                "scheduled_courses": 2,
                                "target_courses": 2,
                                "credit_hours": 6,
                                "days_on_campus": 2,
                                "days": ["SUN", {"student_id": OTHER}],
                                "complete_sections": [
                                    {
                                        "course_code": "AI331",
                                        "course_name": "Knowledge Representation",
                                        "section": "M2",
                                        "credits": 3,
                                        "source": "catalogue",
                                        "section_id": OTHER,
                                        "instructor": CANARIES["latin_name"],
                                        "meetings": [
                                            {
                                                "day": "SUN",
                                                "start": "09:00",
                                                "end": "10:15",
                                                "room": CANARIES["phone"],
                                            }
                                        ],
                                    }
                                ],
                                "meetings": [
                                    {
                                        "course_code": "AI331",
                                        "section": "M2",
                                        "day": "SUN",
                                        "start": "09:00",
                                        "end": "10:15",
                                        "internal_section_id": OTHER,
                                        "advisor_id": CANARIES["advisor_id"],
                                    }
                                ],
                                "private_blob": student_block,
                            }
                        ],
                    },
                    "student_id": MINE,
                    "risk_score": CANARIES["risk_score"],
                }
            ],
            "rejected_replacements": [],
            "limitations": [
                "Capacity is deliberately ignored because the snapshot does not reserve a seat."
            ],
            "student_id": MINE,
            "private_blob": student_block,
        },
    }
    return {**common, **shapes.get(tool, {"rows": [student_block], "summary": CANARIES["name"]})}


def _drive(tool: str, scope: dict) -> tuple[str, dict, int]:
    """Run ONE tool call through the real loop. Returns (sent, local, executions)."""
    boundary = RemoteToolBoundary(
        scope=scope, identities=RemoteIdentityMap(nonce=FIXED), known_names=(CANARIES["name"],)
    )
    client = ScriptedClient([_call(tool, {})])
    telemetry = {"tools_called": [], "boundary_refusals": [], "iterations": 0}
    with pytest.MonkeyPatch.context() as patch:
        spy = ExecutionSpy(patch, result=_poisoned(tool))
        _, _, local, _provider = va._run_agent_loop(
            llm=client,
            resolved_model="m",
            messages=[{"role": "user", "content": "س"}],
            scope=scope,
            ctx={"academic_year": 1448, "term": 1},
            telemetry=telemetry,
            boundary=boundary,
        )
        return client.sent_text, (local[0] if local else {}), spy.count


@pytest.fixture(autouse=True)
def _roster() -> None:
    Student.objects.create(
        student_id=MINE, name=CANARIES["name"], program="AI", section="M", advisor_id="ADV-1"
    )


def _scope_for(tool: str) -> dict:
    allowed = caps.get_default_registry().capabilities[tool].allowed_roles
    return STUDENT_SCOPE if ROLE_STUDENT in allowed else ADVISER_SCOPE


TRANSMITTED = sorted(n for n, e in REMOTE_POLICY.items() if e is not RemoteExposure.DENY)
WITHHELD = sorted(n for n, e in REMOTE_POLICY.items() if e is RemoteExposure.DENY)


@pytest.mark.parametrize("tool", TRANSMITTED)
def test_no_canary_survives_the_projection(tool: str) -> None:
    sent, local, executions = _drive(tool, _scope_for(tool))

    assert executions == 1, "a transmitted capability should run"
    leaked = sorted(label for label, value in CANARIES.items() if value in sent)
    assert leaked == [], f"{tool} leaked {leaked} to the provider"

    # The local half is untouched — the evidence panel and the audit record still
    # have what they were built to show. A matrix that only proved things were
    # ABSENT would pass equally well on a boundary that deleted everything.
    assert CANARIES["name"] in json.dumps(local, ensure_ascii=False, default=str)


def test_clash_projection_keeps_section_evidence_without_people_or_rooms() -> None:
    """A safe projection must not become an empty projection.

    The executor's real keys are ``compared_against_term`` and ``courses``. Keeping
    obsolete singular keys made Alibaba see no M3 even while the local catalogue
    contained it.
    """
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
        known_names=(CANARIES["name"],),
    )
    projected = boundary.project_tool_result(
        "my_clash_free_sections",
        {
            "tool": "my_clash_free_sections",
            "ok": True,
            "student_id": MINE,
            "compared_against_term": "1448/1",
            "courses": [
                {
                    "course_code": "CS285",
                    "sections_on_file": 3,
                    "recorded_sections_on_file": 3,
                    "currently_registered_sections": ["M3"],
                    "status": "OK",
                    "clash_free": [
                        {
                            "section": "M3",
                            "meetings": [
                                "SUN 13:00-14:15",
                                {
                                    "day": "MON",
                                    "start_time": "13:00",
                                    "end_time": "14:15",
                                    "instructor": CANARIES["latin_name"],
                                    "room": "B-214",
                                },
                            ],
                            "is_current_section": True,
                            "instructor": CANARIES["name"],
                            "room": "B-214",
                        }
                    ],
                    "clashing": [
                        {
                            "section": "M1",
                            "meetings": ["SUN 09:00-10:40"],
                            "is_current_section": False,
                            "conflicts": [
                                {
                                    "section_meeting": "SUN 09:00-10:40",
                                    "conflicts_with": "CS113 M4",
                                    "registered_meeting": "SUN 10:00-11:15",
                                    "instructor": CANARIES["latin_name"],
                                }
                            ],
                        }
                    ],
                },
                {
                    "course_code": "AI113",
                    "sections_on_file": 0,
                    "recorded_sections_on_file": 2,
                    "status": "NOT_MATCHING_STUDENT_PROFILE",
                    "programs": ["AI2", "DS2"],
                    "available_capacity": 25,
                    "clash_free": [],
                    "clashing": [],
                },
            ],
        },
    )

    assert projected["compared_against_term"] == "1448/1"
    course = projected["courses"][0]
    assert course["course_code"] == "CS285"
    assert course["sections_on_file"] == 3
    assert course["recorded_sections_on_file"] == 3
    assert course["currently_registered_sections"] == ["M3"]
    assert course["clash_free"][0] == {
        "section": "M3",
        "meetings": [
            "SUN 13:00-14:15",
            {"day": "MON", "start_time": "13:00", "end_time": "14:15"},
        ],
        "is_current_section": True,
    }
    assert course["clashing"][0]["conflicts"][0]["conflicts_with"] == "CS113 M4"
    filtered = projected["courses"][1]
    assert filtered == {
        "course_code": "AI113",
        "sections_on_file": 0,
        "recorded_sections_on_file": 2,
        "status": "NOT_MATCHING_STUDENT_PROFILE",
        "clash_free": [],
        "clashing": [],
    }
    sent = json.dumps(projected, ensure_ascii=False)
    assert CANARIES["name"] not in sent
    assert CANARIES["latin_name"] not in sent
    assert "B-214" not in sent
    assert str(MINE) not in sent


def test_replacement_projection_keeps_proof_without_internal_section_identity() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
        known_names=(CANARIES["name"],),
    )
    projected = boundary.project_tool_result(
        "feasible_course_replacements",
        _poisoned("feasible_course_replacements"),
    )

    assert projected["status"] == "CERTIFIED_SWAPS_FOUND"
    assert projected["certification_search"] == {
        "academic_candidates_received": 2,
        "timetable_candidates_checked": 2,
        "certified_result_limit": 5,
        "search_truncated": False,
    }
    replacement = projected["certified_replacements"][0]
    assert replacement["remove_course"]["course_code"] == "DS341"
    assert replacement["add_course"]["course_code"] == "AI331"
    assert replacement["academic_improvement"]["blockers_resolved"] == ["AI401"]
    option = replacement["timetable"]["certified_options"][0]
    assert option["planner_options"] == ["A1"]
    assert option["days"] == ["SUN"]
    assert option["complete_sections"][0]["section"] == "M2"
    assert option["complete_sections"][0]["meetings"] == [
        {"day": "SUN", "start": "09:00", "end": "10:15"}
    ]

    encoded = json.dumps(projected, ensure_ascii=False, default=str)
    assert "section_id" not in encoded
    assert "internal_section_id" not in encoded
    assert "instructor" not in encoded
    assert "private_blob" not in encoded
    leaked = sorted(label for label, value in CANARIES.items() if value in encoded)
    assert leaked == []


def test_replacement_projection_never_forwards_free_text_rejection_reason() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("feasible_course_replacements")
    result["rejected_replacements"] = [
        {
            "remove_course": {"course_code": "DS341"},
            "add_course": {"course_code": "CS285"},
            "academic": {"status": "PROVEN"},
            "timetable": {
                "status": "NOT_DETERMINABLE",
                "reason_code": "MISSING_MEETING_DATA",
                "reason": f"Student {MINE}: {CANARIES['email']}",
            },
        }
    ]

    timetable = boundary.project_tool_result("feasible_course_replacements", result)[
        "rejected_replacements"
    ][0]["timetable"]

    assert timetable["reason_code"] == "MISSING_MEETING_DATA"
    assert timetable["reason"].startswith("A selected section has missing")
    assert str(MINE) not in json.dumps(timetable)
    assert CANARIES["email"] not in json.dumps(timetable)


def test_replacement_projection_maps_snapshot_term_mismatch_to_fixed_text() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("feasible_course_replacements")
    result["rejected_replacements"] = [
        {
            "timetable": {
                "status": "NOT_DETERMINABLE",
                "reason_code": "section_snapshot_term_mismatch",
                "reason": f"Student {MINE}: {CANARIES['email']}",
            }
        }
    ]

    timetable = boundary.project_tool_result("feasible_course_replacements", result)[
        "rejected_replacements"
    ][0]["timetable"]

    assert timetable == {
        "status": "NOT_DETERMINABLE",
        "reason_code": "SECTION_SNAPSHOT_TERM_MISMATCH",
        "reason": (
            "The section catalogue is not verified for the requested term, so a "
            "replacement timetable cannot be certified."
        ),
    }


def test_replacement_projection_drops_unknown_rejection_reason() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("feasible_course_replacements")
    result["rejected_replacements"] = [
        {
            "timetable": {
                "status": "NOT_DETERMINABLE",
                "reason_code": f"PRIVATE_{MINE}",
                "reason": CANARIES["email"],
            }
        }
    ]

    timetable = boundary.project_tool_result("feasible_course_replacements", result)[
        "rejected_replacements"
    ][0]["timetable"]

    assert timetable == {"status": "NOT_DETERMINABLE"}


def test_replacement_projection_bounds_nested_reasons_and_limitations() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("feasible_course_replacements")
    public_limitation = (
        "Capacity is deliberately ignored because the snapshot does not reserve a "
        "seat. No result proves a live seat."
    )
    private_text = f"Student {MINE}: {CANARIES['email']}"
    result["rejected_replacements"] = [
        {
            "timetable": {
                "status": "NOT_DETERMINABLE",
                "reason_code": "BASELINE_SECTION_MAPPING_INCOMPLETE",
                "details": [
                    {
                        "reason_code": "BASELINE_MEETING_DATA_MISSING",
                        "course_code": "DS341",
                    },
                    {
                        "reason_code": private_text,
                        "course_code": "AI331",
                    },
                ],
            }
        }
    ]
    result["limitations"] = [public_limitation, private_text]

    projected = boundary.project_tool_result("feasible_course_replacements", result)
    timetable = projected["rejected_replacements"][0]["timetable"]

    assert timetable["details"] == [
        {
            "reason_code": "BASELINE_MEETING_DATA_MISSING",
            "course_code": "DS341",
        },
        {"course_code": "AI331"},
    ]
    assert projected["limitations"] == [public_limitation]
    encoded = json.dumps(projected, ensure_ascii=False)
    assert private_text not in encoded
    assert CANARIES["email"] not in encoded


def test_course_comparison_projection_keeps_safe_timetable_uncertainty_reason() -> None:
    """The remote writer must know why clash evidence was not determinable.

    Structured meeting details remain local because they are unnecessary for the
    explanation; a bounded reason code selects fixed public wording.
    """
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
        known_names=(CANARIES["name"],),
    )
    result = _poisoned("course_choice_comparison")
    result["candidates"][0]["timetable"] = {
        "status": "NOT_DETERMINABLE",
        "reason_code": "CANDIDATE_MEETING_DATA_INCOMPLETE",
        "reason": f"Student {MINE} instructor {CANARIES['email']}",
        "details": [
            {
                "section": "M1",
                "instructor": CANARIES["latin_name"],
                "room": "B-214",
            }
        ],
        "sections_on_file": 2,
        "clash_free_count": None,
        "clashing_count": None,
    }

    projected = boundary.project_tool_result("course_choice_comparison", result)
    timetable = projected["candidates"][0]["timetable"]

    assert timetable == {
        "status": "NOT_DETERMINABLE",
        "reason_code": "CANDIDATE_MEETING_DATA_INCOMPLETE",
        "reason": (
            "At least one recorded candidate section has missing or invalid meeting "
            "data, so the complete timetable choice cannot be compared."
        ),
        "sections_on_file": 2,
        "clash_free_count": None,
        "clashing_count": None,
        "baseline_sections": [],
    }
    assert "details" not in timetable
    encoded = json.dumps(projected, ensure_ascii=False)
    assert str(MINE) not in encoded
    assert CANARIES["email"] not in encoded
    assert CANARIES["latin_name"] not in encoded
    assert result["candidates"][0]["timetable"]["reason"] == (
        f"Student {MINE} instructor {CANARIES['email']}"
    )


@pytest.mark.parametrize(
    ("reason_code", "public_reason"),
    [
        (
            "BASELINE_MEETING_DATA_INCOMPLETE",
            "At least one retained baseline section has missing or invalid meeting "
            "data, so clashes cannot be certified.",
        ),
        (
            "CANDIDATE_MEETING_DATA_INCOMPLETE",
            "At least one recorded candidate section has missing or invalid meeting "
            "data, so the complete timetable choice cannot be compared.",
        ),
        (
            "MIXED_BASELINE_REVIEW_REQUIRED",
            "Registered and expected-plan rows are mixed, so there is no single "
            "timetable baseline to compare against.",
        ),
        (
            "COHORT_UNRESOLVED",
            "The student's section cohort is unresolved, so eligible catalogue "
            "sections cannot be selected safely.",
        ),
        (
            "NOT_ON_FILE",
            "No section for this course is recorded in the current catalogue snapshot; "
            "this is not proof that the university does not offer it.",
        ),
        (
            "SECTION_SNAPSHOT_TERM_MISMATCH",
            "The section catalogue is the configured current snapshot, not the explicit "
            "comparison term, so timetable fit cannot be certified.",
        ),
    ],
)
def test_course_comparison_projection_maps_known_reason_codes_to_fixed_text(
    reason_code: str,
    public_reason: str,
) -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("course_choice_comparison")
    result["candidates"][0]["timetable"] = {
        "status": "NOT_DETERMINABLE",
        "reason_code": reason_code.lower(),
        "reason": f"Student {MINE}: {CANARIES['email']}",
    }

    timetable = boundary.project_tool_result("course_choice_comparison", result)["candidates"][0][
        "timetable"
    ]

    assert timetable["reason_code"] == reason_code
    assert timetable["reason"] == public_reason
    assert str(MINE) not in json.dumps(timetable, ensure_ascii=False)
    assert CANARIES["email"] not in json.dumps(timetable, ensure_ascii=False)


def test_course_comparison_projection_drops_unknown_reason_code_and_text() -> None:
    boundary = RemoteToolBoundary(
        scope=STUDENT_SCOPE,
        identities=RemoteIdentityMap(nonce=FIXED),
    )
    result = _poisoned("course_choice_comparison")
    raw_timetable = {
        "status": "NOT_DETERMINABLE",
        "reason_code": f"PRIVATE_{MINE}_{CANARIES['email']}",
        "reason": f"Student {MINE}: {CANARIES['latin_name']}",
        "details": [{"instructor": CANARIES["latin_name"]}],
    }
    result["candidates"][0]["timetable"] = raw_timetable

    timetable = boundary.project_tool_result("course_choice_comparison", result)["candidates"][0][
        "timetable"
    ]

    assert timetable == {"status": "NOT_DETERMINABLE", "baseline_sections": []}
    assert result["candidates"][0]["timetable"] is raw_timetable
    assert result["candidates"][0]["timetable"]["reason"] == (
        f"Student {MINE}: {CANARIES['latin_name']}"
    )


@pytest.mark.parametrize("tool", WITHHELD)
def test_a_withheld_capability_neither_runs_nor_is_offered(tool: str) -> None:
    scope = _scope_for(tool)
    sent, _local, executions = _drive(tool, scope)

    assert executions == 0, f"{tool} is DENY and must not reach the registry"
    leaked = sorted(label for label, value in CANARIES.items() if value in sent)
    assert leaked == []

    boundary = RemoteToolBoundary(scope=scope, identities=RemoteIdentityMap(nonce=FIXED))
    offered = {
        s["function"]["name"]
        for s in boundary.tool_schemas(caps.get_default_registry().tool_schemas_for_scope(scope))
    }
    assert tool not in offered


def test_every_registry_capability_has_a_remote_decision() -> None:
    """The drift guard. A capability added to the registry without a
    `REMOTE_POLICY` entry would otherwise be untested here AND refused at runtime
    — safe, but discovered by a student getting an error rather than by CI."""
    registered = set(caps.get_default_registry().capabilities)
    undecided = sorted(registered - set(REMOTE_POLICY))
    assert undecided == [], f"no remote-exposure decision for: {undecided}"

    stale = sorted(set(REMOTE_POLICY) - registered)
    assert stale == [], f"remote policy names capabilities that no longer exist: {stale}"


@pytest.mark.parametrize("tool", TRANSMITTED)
def test_every_row_of_the_matrix_is_load_bearing(tool: str) -> None:
    """A matrix of green rows is also what a broken harness produces.

    Run per capability, not once: a canary that `get_student_context` carries
    proves nothing about the row for `my_timetable`, whose poisoned shape is
    different. Each row's own poisoned result is sent through the identity
    boundary, and each must leak — otherwise that row's green result above was
    measuring a payload with nothing in it to find.
    """
    from core.services.advisor_remote_boundary import LocalToolBoundary

    client = ScriptedClient([_call(tool, {})])
    telemetry = {"tools_called": [], "boundary_refusals": [], "iterations": 0}
    with pytest.MonkeyPatch.context() as patch:
        ExecutionSpy(patch, result=_poisoned(tool))
        va._run_agent_loop(
            llm=client,
            resolved_model="m",
            messages=[{"role": "user", "content": "س"}],
            scope=_scope_for(tool),
            ctx={"academic_year": 1448, "term": 1},
            telemetry=telemetry,
            boundary=LocalToolBoundary(),
        )
    leaked = sorted(label for label, value in CANARIES.items() if value in client.sent_text)
    assert "name" in leaked and "email" in leaked, (
        f"the canaries are not detectable in a {tool} payload, so its green row means nothing"
    )


def test_new_list_fields_are_element_typed_not_container_typed():
    """A dict smuggled into a whitelisted code list must not cross by reference.

    The module's premise is fails-closed-by-construction; these fields were
    held open by producer discipline alone - every producer happens to emit
    list[str] today, and nothing enforced it.
    """
    from core.services.llm_remote_privacy import project_tool_result_for_remote

    hostile = {
        "tool": "my_progress",
        "ok": True,
        "registered_requirement_course_codes": [
            "AI1",
            {"code": "AI1", "student_name": "Ahmed Canary", "national_id": "1098765432"},
        ],
        "expected_plan_course_codes": [{"gpa": 3.42}],
        "expected_plan_requirement_aliases": [["nested", 4400123]],
    }
    out = project_tool_result_for_remote("my_progress", hostile, RemoteIdentityMap())
    encoded = json.dumps(out, ensure_ascii=False)

    assert out["registered_requirement_course_codes"] == ["AI1"]
    assert out["expected_plan_course_codes"] == []
    assert out["expected_plan_requirement_aliases"] == []
    for needle in ("Ahmed Canary", "1098765432", "3.42", "4400123"):
        assert needle not in encoded, needle
