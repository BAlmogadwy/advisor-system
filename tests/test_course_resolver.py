"""The typo resolver: grounded corrections, never nearest-neighbour guesses.

The audited production answers contain both halves of this class: MATE243
recommended as if it existed (the real course is MATH243), and «ربما تقصد
DS221 أو DS225» offered as help where DS225 does not exist.  The resolver
turns the first into a grounded correction and refuses to fuel the second:
letter repairs with EXACTLY matching digits are suggested as data; digit-
differing inventions get nothing, because CS202 is one edit from both CS201
and CS212 and a guess there is the invented-guidance class this replaces.
"""

from __future__ import annotations

import pytest

from core.models import Course, ProgrammeRequirement, Student
from core.services.rbac import ROLE_STUDENT
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 4_402_888


def _seed() -> None:
    Student.objects.create(
        student_id=SID,
        registration_no=str(SID),
        name="Resolver student",
        program="AI",
        section="M",
        status="active",
    )
    for code, name, program in (
        ("MATH243", "Discrete Mathematics", "AI"),
        ("CS201", "Programming II", "CS"),
        ("CS212", "Data Structures", "CS"),
        ("AI331", "Natural Language Processing", "AI"),
    ):
        Course.objects.create(course_code=code, description=name, credit_hours=3)
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=code,
            course_name=name,
            type="Mandatory",
            programme_term=3,
            credit_hours=3,
        )


def _lookup(query: str, *, program: str = "") -> dict:
    args: dict = {"query": query}
    if program:
        args["program"] = program
    return get_default_registry().execute(
        "lookup_course",
        args,
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": 1448, "term": 1},
    )


def test_a_letter_typo_with_matching_digits_gets_the_real_course_as_data():
    """MATE243 -> MATH243: same digits, one letter wrong, name included."""
    _seed()
    result = _lookup("MATE243", program="AI")

    assert result["ok"] is True
    assert result["unknown_query"] == "MATE243"
    assert [row["candidate_code"] for row in result["did_you_mean"]] == ["MATH243"]
    assert result["did_you_mean"][0]["candidate_name"] == "Discrete Mathematics"
    assert result["did_you_mean"][0]["distance"] == 1


def test_a_digit_differing_invention_gets_no_neighbour():
    """CS202 is one edit from CS201 AND CS212 - a guess is the old defect."""
    _seed()
    result = _lookup("CS202", program="CS")

    assert result["unknown_query"] == "CS202"
    assert result["did_you_mean"] == []


def test_a_real_code_carries_no_resolver_fields():
    """The resolver speaks only about codes the system does NOT recognise."""
    _seed()
    result = _lookup("MATH243", program="AI")

    assert result["match_count"] >= 1
    assert "unknown_query" not in result
    assert "did_you_mean" not in result


def test_the_unknown_token_is_never_evidence_but_the_candidate_is():
    """The split the resolver's safety rests on.

    The UNKNOWN token (unknown_query, and the raw query echo) must never gain
    existence through the resolver - "look the invention up, assert it
    anyway" is the audited CS202 behaviour.  The CANDIDATES are letter-repairs
    of catalogue-real codes, so naming one in a correction is grounded:
    without that, the truthful «لعلك تقصد MATH243» was refused as an
    unsupported mention.
    """
    from core.services.answer_consistency import (
        UNSUPPORTED_ACADEMIC_FACT,
        _course_codes_in_evidence,
        check_answer,
    )

    payload = {
        "tool": "lookup_course",
        "ok": True,
        "query": "MATE244",
        "match_count": 0,
        "courses": [],
        "unknown_query": "MATE244",
        "did_you_mean": [{"candidate_code": "MATH243", "candidate_name": "Discrete Mathematics"}],
    }
    assert _course_codes_in_evidence(payload) == {"MATH243"}

    # An answer RECOMMENDING an off-catalogue code is still flagged even when
    # the resolver payload mentions tokens; naming the real candidate passes.
    catalogue = frozenset({"MATH243", "AI331"})
    assert UNSUPPORTED_ACADEMIC_FACT in check_answer(
        "ننصحك بتسجيل مقرر ZZ909 هذا الفصل.",
        tool_results=[payload],
        question="ما هو MATE244؟",
        required_tools=set(),
        known_course_codes=catalogue,
    )
    assert (
        check_answer(
            "لا يوجد مقرر برمز MATE244؛ لعلك تقصد MATH243 (الرياضيات المتقطعة).",
            tool_results=[payload],
            question="ما هو MATE244؟",
            required_tools=set(),
            known_course_codes=catalogue,
        )
        == []
    )


def test_the_remote_projection_carries_the_resolver_fields():
    """A whitelist that drops the new keys leaves the PRODUCTION model unable
    to correct a typo the local path could - the silent-projector failure the
    privacy module documents."""
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    payload = {
        "tool": "lookup_course",
        "ok": True,
        "query": "MATE243",
        "match_count": 0,
        "courses": [],
        "unknown_query": "MATE243",
        "did_you_mean": [
            {
                "candidate_code": "MATH243",
                "candidate_name": "Discrete Mathematics",
                "distance": 1,
                "internal_note": "must not cross",
            }
        ],
    }
    out = project_tool_result_for_remote("lookup_course", payload, RemoteIdentityMap())

    assert out["unknown_query"] == "MATE243"
    assert out["did_you_mean"] == [
        {
            "candidate_code": "MATH243",
            "candidate_name": "Discrete Mathematics",
            "distance": 1,
        }
    ]


def test_a_student_lookup_defaults_to_their_own_programme():
    """Unscoped, a fuzzy search ranged over every programme's catalogue."""
    _seed()
    # CS courses exist; the AI student's un-programmed name search must not
    # surface them, while an explicit program argument still can.
    scoped = _lookup("Programming")
    assert all("CS" not in row["programs"] for row in scoped["courses"])

    explicit = _lookup("Programming", program="CS")
    assert any("CS" in row["programs"] for row in explicit["courses"])
