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


def test_a_students_exact_lookup_of_a_cross_programme_code_is_not_unknown():
    """The P0 an adversarial review caught before merge: the programme
    default leaked into the EXACT-code branch, so 418 of 490 real codes were
    "unknown" to an AI student - and then offered global-catalogue repairs,
    machine-generating the audited «ربما تقصد DS225».  Existence is a fact
    about the catalogue, not about the asker."""
    _seed()
    result = _lookup("CS201")  # a CS-programme course; the student is AI

    assert result["match_count"] >= 1
    assert "unknown_query" not in result
    assert "did_you_mean" not in result
    row = next(r for r in result["courses"] if r["course_code"] == "CS201")
    assert "CS" in row["programs"]


def test_a_code_living_only_in_the_course_table_is_not_unknown():
    """No requirement row anywhere, no elective row - the code still exists,
    and "unknown" from this tool licenses the model to deny it does."""
    _seed()
    Course.objects.create(course_code="GS111", description="Study Skills", credit_hours=2)
    from core.services.course_catalogue import invalidate_cache

    invalidate_cache()
    result = _lookup("GS111")

    assert "unknown_query" not in result
    assert result["match_count"] >= 1
    assert result["courses"][0]["course_name"] == "Study Skills"


def test_hyphen_and_arabic_digit_spellings_resolve_to_the_catalogue_row():
    """«MATH-243» and «MATH٢٤٣» are the same question as MATH243.  The first
    spelling reported both as unknown - the Arabic-Indic class the checker's
    normaliser learned long before the resolver did."""
    _seed()
    for spelling in ("MATH-243", "MATH٢٤٣"):
        result = _lookup(spelling, program="AI")
        assert result["match_count"] >= 1, spelling
        assert "unknown_query" not in result, spelling


def test_the_resolver_never_offers_the_code_itself():
    """distance > 0 is load-bearing now that real codes can reach the helper
    catalogue-wide: a distance-0 "repair" would be the tool contradicting
    itself about a code it just failed to match."""
    _seed()
    from core.services.virtual_advisor_capabilities import _did_you_mean

    assert _did_you_mean("MATH243") == []


def test_letter_distance_three_is_not_a_repair():
    """MXYZ243 is three letter edits from MATH243 - beyond repair, an
    invention.  Pins the advertised distance <= 2 bound."""
    _seed()
    from core.services.virtual_advisor_capabilities import _did_you_mean

    assert _did_you_mean("MXYZ243") == []


def test_a_two_letter_prefix_repair_keeps_at_least_one_typed_letter():
    """The review's live-DB sweep: 1306 of 2427 two-letter-prefix fabrications
    drew a "repair" sharing NOT ONE letter with what was typed - CS202 got
    EE202 and QZ202.  The distance budget scales with the prefix, so a
    2-letter prefix affords one edit and QZ113 gets nothing, while XS113
    (S survives) still repairs."""
    _seed()
    for code in ("CS113", "DS113", "IS113"):
        Course.objects.create(course_code=code, description=f"{code} course", credit_hours=3)
    from core.services.course_catalogue import invalidate_cache
    from core.services.virtual_advisor_capabilities import _did_you_mean

    invalidate_cache()
    assert _did_you_mean("QZ113") == []
    assert [row["candidate_code"] for row in _did_you_mean("XS113")] == [
        "CS113",
        "DS113",
        "IS113",
    ]


def test_a_separator_in_the_stored_code_still_finds_its_row():
    """The row side of the fold: the exact arms compare the RAW stored value,
    so a row stored as «AI-463» or with Arabic-Indic digits matched nothing -
    a silent empty for a code the floor recognises.  The miss-path fallback
    scans with the floor's own normalisation and emits the floor-key
    spelling."""
    _seed()
    Course.objects.create(course_code="AI-463", description="Applied AI", credit_hours=3)
    ProgrammeRequirement.objects.create(
        program="AI",
        course_code="PHYS٢١٠",
        course_name="Physics II",
        type="Mandatory",
        programme_term=2,
        credit_hours=3,
    )
    from core.services.course_catalogue import invalidate_cache

    invalidate_cache()

    hyphen = _lookup("AI463")
    assert "unknown_query" not in hyphen
    assert hyphen["match_count"] == 1
    assert hyphen["courses"][0]["course_code"] == "AI463"
    assert hyphen["courses"][0]["course_name"] == "Applied AI"

    arabic = _lookup("PHYS210")
    assert "unknown_query" not in arabic
    assert arabic["match_count"] == 1
    row = arabic["courses"][0]
    assert row["course_code"] == "PHYS210"
    assert row["course_name"] == "Physics II"
    assert row["programs"] == ["AI"]


def test_a_stale_floor_still_blocks_a_false_unknown():
    """The catalogue-floor guard's own pin.  In the cache-TTL window after an
    uninvalidated delete, the fallback scan misses (the rows are gone) but
    the warm floor still recognises the code - and the lookup must answer
    empty, never DECLARE unknown: "unknown" licenses the model to deny the
    course exists, and a licence issued from a 60-second race is still a
    licence."""
    _seed()
    from core.services.course_catalogue import invalidate_cache, known_course_codes

    invalidate_cache()
    assert "MATH243" in known_course_codes()  # warm the floor

    from core.models import ElectiveCourse

    Course.objects.all().delete()
    ProgrammeRequirement.objects.all().delete()
    ElectiveCourse.objects.all().delete()  # deliberately NOT invalidated

    result = _lookup("MATH243")
    assert result["match_count"] == 0
    assert "unknown_query" not in result
    assert "did_you_mean" not in result


def test_at_most_three_candidates_survive_and_ties_break_by_code():
    """Pins the advertised cap.  Four catalogue codes sit at distance 1 from
    XS113; exactly three come back, in code order."""
    _seed()
    for code in ("CS113", "DS113", "IS113", "MS113"):
        Course.objects.create(course_code=code, description=f"{code} course", credit_hours=3)
    from core.services.course_catalogue import invalidate_cache
    from core.services.virtual_advisor_capabilities import _did_you_mean

    invalidate_cache()
    rows = _did_you_mean("XS113")
    assert [row["candidate_code"] for row in rows] == ["CS113", "DS113", "IS113"]


def test_the_projection_validates_and_caps_the_resolver_rows_independently():
    """The projector is the privacy boundary; it must hold even against a
    producer that stops behaving - malformed rows drop, extras cap at 3."""
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )

    well_formed = [
        {"candidate_code": f"C{i}", "candidate_name": f"Course {i}", "distance": 1}
        for i in range(5)
    ]
    payload = {
        "tool": "lookup_course",
        "ok": True,
        "query": "MATE243",
        "match_count": 0,
        "courses": [],
        "unknown_query": "MATE243",
        "did_you_mean": [
            "not-a-row",
            {"candidate_code": 7, "candidate_name": "typed wrong", "distance": 1},
            {"candidate_code": "MATH243", "candidate_name": "ok", "distance": "1"},
            # bool IS an int to isinstance - the boundary must not let a
            # True cross where a distance belongs.
            {"candidate_code": "BOOL1", "candidate_name": "bool", "distance": True},
            *well_formed,
        ],
    }
    out = project_tool_result_for_remote("lookup_course", payload, RemoteIdentityMap())

    assert out["did_you_mean"] == well_formed[:3]


def test_a_student_lookup_defaults_to_their_own_programme():
    """Unscoped, a fuzzy search ranged over every programme's catalogue."""
    _seed()
    # CS courses exist; the AI student's un-programmed name search must not
    # surface them, while an explicit program argument still can.
    scoped = _lookup("Programming")
    assert all("CS" not in row["programs"] for row in scoped["courses"])

    explicit = _lookup("Programming", program="CS")
    assert any("CS" in row["programs"] for row in explicit["courses"])
