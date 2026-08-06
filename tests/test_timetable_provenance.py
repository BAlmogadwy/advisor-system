"""What a built timetable is allowed to assert, and what it may no longer hide.

Every case here is a failure that was live on 2026-08-06, not a hypothetical. The
executor-level cases run the real capability against the real solver and the real
catalogue: the defect they cover — a course the student is already registered in
being sent to the solver and pruned by the student's own baseline — is invisible to
a fixture, because a synthetic student has no registrations to collide with.
"""

from __future__ import annotations

import pytest

from core.services.timetable_provenance import (
    CHANGE_ADD,
    CHANGE_REPLACE_SECTION,
    CHANGE_RETAIN,
    OUTCOME_ALREADY_REGISTERED,
    OUTCOME_NOT_PLACED,
    SOURCE_CURRENT_REGISTRATION,
    SOURCE_STUDENT_REQUEST,
    SOURCE_SYSTEM_RECOMMENDATION,
    baseline_sections,
    build_timetable_facts,
)

pytestmark = pytest.mark.django_db


def _baseline_row(code: str, section: str, day: str, start: str, end: str, **extra):
    """One MEETING row, in the shape `get_student_term_baseline` really returns.

    Including the fields this module must drop: a fixture that omits `instructor`
    cannot catch a projection that leaks it.
    """
    return {
        "course_code": code,
        "course_key": code,
        "course_name": f"{code} NAME",
        "section": section,
        "day": day,
        "start_time": start,
        "end_time": end,
        "room": "B101",
        "instructor": "Dr Someone",
        "credits": 4,
        "term_section_id": extra.pop("term_section_id", hash((code, section)) % 10_000),
        **extra,
    }


def _facts(**kwargs):
    defaults = {
        "student_id": 1,
        "using_timetable_of_term": "1448/1",
        "requested_codes": [],
        "recommended_codes": [],
        "baseline": [],
        "mappings": [],
        "unscheduled": [],
        "credit_hours": {},
        "default_credits": 3,
        "cap": 0,
    }
    return build_timetable_facts(**{**defaults, **kwargs})


# ── baseline_sections ────────────────────────────────────────────────────────


def test_one_row_per_section_not_one_row_per_meeting() -> None:
    """The baseline repeats a section once per meeting, credits and all.

    Measured on the controlled evaluation record: 11 rows for 4 sections, every row carrying the
    section's full credit hours. Summing credits over the raw rows charges a
    4-credit course 12 hours, which is how a 15-hour week becomes 42.
    """
    rows = baseline_sections(
        [
            _baseline_row("AI331", "M1", "MON", "09:00", "10:15"),
            _baseline_row("AI331", "M1", "WED", "09:00", "10:15"),
            _baseline_row("AI331", "M1", "SUN", "10:30", "12:10"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["meetings"] == ["MON 09:00-10:15", "WED 09:00-10:15", "SUN 10:30-12:10"]


def test_two_sections_of_one_course_are_two_rows_not_a_silent_winner() -> None:
    """Collapsing on the course alone would drop one under a rule nobody wrote."""
    rows = baseline_sections(
        [
            _baseline_row("CS323", "M1", "MON", "13:00", "14:15"),
            _baseline_row("CS323", "M2", "TUE", "13:00", "14:15"),
        ]
    )
    assert sorted(r["section"] for r in rows) == ["M1", "M2"]


def test_the_staff_name_and_the_room_do_not_survive() -> None:
    """`_project_my_timetable` drops instructor names on purpose. Enforcing it where
    the rows are BUILT means the remote allowlist is not the only thing between a
    member of staff's name and an external provider."""
    row = baseline_sections([_baseline_row("AI331", "M1", "MON", "09:00", "10:15")])[0]
    assert "instructor" not in row
    assert "room" not in row
    assert "Dr Someone" not in str(row)


def test_a_section_with_no_meetings_says_so_rather_than_showing_a_blank_slot() -> None:
    rows = baseline_sections([_baseline_row("AI1", "M1", "", "", "")])
    assert rows[0]["meetings"] == []


# ── provenance ───────────────────────────────────────────────────────────────


def test_what_the_student_asked_for_is_not_what_the_system_chose() -> None:
    """TT21, «الجدول أضاف مقررًا أنا ما طلبته، من وين جاء؟».

    The old payload concatenated both into `requested`, so the answer the student
    wanted was not merely missing — it was contradicted, because the field asserted
    they had asked for all of it. Live, the model concluded the system keeps no
    record of prior requests and sent the student to the registrar.
    """
    facts = _facts(
        requested_codes=["AI352"],
        recommended_codes=["AI331", "CS323"],
        credit_hours={"AI352": 3, "AI331": 4, "CS323": 4},
    )
    assert [c["course_code"] for c in facts.student_requested_courses] == ["AI352"]
    assert [c["course_code"] for c in facts.system_recommended_courses] == ["AI331", "CS323"]
    assert {c["source"] for c in facts.student_requested_courses} == {SOURCE_STUDENT_REQUEST}
    assert {c["source"] for c in facts.system_recommended_courses} == {SOURCE_SYSTEM_RECOMMENDATION}


def test_a_course_in_both_lists_belongs_to_the_student() -> None:
    """`dict.fromkeys(wanted + recommended)` gave the student's list precedence by
    accident of ordering. Split into two fields it has to be decided on purpose, and
    the student naming a course the recommender also chose is still the student
    asking for it."""
    facts = _facts(requested_codes=["AI331"], recommended_codes=["AI331", "CS323"])
    assert [c["course_code"] for c in facts.student_requested_courses] == ["AI331"]
    assert [c["course_code"] for c in facts.system_recommended_courses] == ["CS323"]


def test_the_sections_the_student_already_holds_appear_in_the_answer() -> None:
    """The tool description promises "It ALWAYS keeps the sections the student is
    already registered in". `planner_builder` uses the baseline as an occupancy mask
    and never adds it to `mappings`, so no field of the old result contained them:
    the model was asked to assert a retention it was given no evidence of."""
    facts = _facts(
        baseline=[
            _baseline_row("AI331", "M1", "MON", "09:00", "10:15"),
            _baseline_row("CS323", "M1", "MON", "13:00", "14:15"),
        ],
        credit_hours={"AI331": 4, "CS323": 4},
    )
    assert [r["course_code"] for r in facts.retained_sections] == ["AI331", "CS323"]
    assert {r["change"] for r in facts.retained_sections} == {CHANGE_RETAIN}
    assert {r["source"] for r in facts.retained_sections} == {SOURCE_CURRENT_REGISTRATION}


def test_a_newly_scheduled_section_is_an_add_not_a_retain() -> None:
    facts = _facts(
        recommended_codes=["MATH204"],
        mappings=[
            {
                "course_code": "MATH204",
                "section": "M1",
                "term_section_id": 77,
                "meetings": [{"day": "TUE", "start_time": "08:00", "end_time": "09:15"}],
            }
        ],
        credit_hours={"MATH204": 3},
    )
    assert [r["change"] for r in facts.new_sections] == [CHANGE_ADD]
    assert facts.new_sections[0]["meetings"] == ["TUE 08:00-09:15"]
    assert facts.credit_summary == {
        "retained_credit_hours": 0,
        "new_credit_hours": 3,
        "total_plan_credit_hours": 3,
        "new_courses_credit_cap": None,
    }


def test_a_different_section_of_a_held_course_is_a_replacement_with_both_ends_named() -> None:
    """The transition, ENCODED — which is why there is no `retained.isdisjoint(new)`
    assertion. Live on TT10 the answer said «تم الاحتفاظ بـ CS323-M1» and «CS323: شعبة
    M2» in the same breath, because the payload could express both memberships and
    not the move between them. Unreachable from chat today; the planner's
    replace-section workflow is the caller that will produce it.
    """
    facts = _facts(
        recommended_codes=["CS323"],
        baseline=[_baseline_row("CS323", "M1", "MON", "13:00", "14:15", term_section_id=11)],
        mappings=[
            {
                "course_code": "CS323",
                "section": "M2",
                "term_section_id": 22,
                "meetings": [{"day": "MON", "start_time": "10:30", "end_time": "11:45"}],
            }
        ],
        credit_hours={"CS323": 4},
    )
    assert [r["change"] for r in facts.new_sections] == [CHANGE_REPLACE_SECTION]
    assert facts.section_replacements == (
        {
            "course_code": "CS323",
            "from_section": "M1",
            "to_section": "M2",
            "source": SOURCE_SYSTEM_RECOMMENDATION,
        },
    )
    # The section given up is NOT also reported as kept. That pair, asserted at once,
    # is the contradiction the encoding exists to make unrepresentable.
    assert [r["course_code"] for r in facts.retained_sections] == []


def test_the_same_section_again_is_a_retain_and_not_a_replacement() -> None:
    facts = _facts(
        recommended_codes=["CS323"],
        baseline=[_baseline_row("CS323", "M1", "MON", "13:00", "14:15", term_section_id=11)],
        mappings=[{"course_code": "CS323", "section": "M1", "term_section_id": 11, "meetings": []}],
        credit_hours={"CS323": 4},
    )
    assert facts.section_replacements == ()
    assert [r["change"] for r in facts.new_sections] == [CHANGE_ADD]


def test_a_relabelled_section_is_not_a_replacement_of_itself() -> None:
    """With no id to compare, the label decides — folded, because it is not normalised.

    Issue #54 records three classifiers in this repo disagreeing about whether a
    section is «M1» or « M1». A raw label comparison turns one space, or one letter's
    case, into a report that the student's section was swapped for itself.
    """
    facts = _facts(
        recommended_codes=["CS323"],
        baseline=[_baseline_row("CS323", " m1", "MON", "13:00", "14:15", term_section_id=None)],
        mappings=[
            {"course_code": "CS323", "section": "M1", "term_section_id": None, "meetings": []}
        ],
        credit_hours={"CS323": 4},
    )
    assert facts.section_replacements == ()
    assert [r["change"] for r in facts.new_sections] == [CHANGE_ADD]


def test_a_missing_section_id_does_not_manufacture_a_replacement() -> None:
    """`held_id != placed_id` reads a MISSING id as a difference.

    A solver mapping with no `term_section_id` would then supersede every section the
    student holds of that course — telling them their registration was swapped, on
    the strength of a comparison against nothing.
    """
    facts = _facts(
        recommended_codes=["CS323"],
        baseline=[_baseline_row("CS323", "M1", "MON", "13:00", "14:15", term_section_id=11)],
        mappings=[
            {"course_code": "CS323", "section": "M1", "term_section_id": None, "meetings": []}
        ],
        credit_hours={"CS323": 4},
    )
    assert facts.section_replacements == ()
    assert [r["course_code"] for r in facts.retained_sections] == ["CS323"]


def test_an_already_registered_course_is_not_reported_as_a_clash() -> None:
    """Measured at 33 of 95 unplaced rows across 20 students: a course the student is
    sitting in, reported ALL_SECTIONS_CLASH because the solver pruned the student's
    own section against the student's own baseline. «AI331: جميع الشعب المتاحة تتعارض
    مع جدولك الحالي» is true, useless, and reads as "you cannot take this"."""
    facts = _facts(
        recommended_codes=["AI331"],
        baseline=[_baseline_row("AI331", "M1", "MON", "09:00", "10:15")],
        credit_hours={"AI331": 4},
    )
    rows = {r["course_code"]: r for r in facts.unplaced_courses}
    assert rows["AI331"]["outcome"] == OUTCOME_ALREADY_REGISTERED
    assert rows["AI331"]["sections"] == ["M1"]
    assert "reason_code" not in rows["AI331"]


def test_a_genuinely_unplaceable_course_keeps_its_reason_and_its_source() -> None:
    facts = _facts(
        recommended_codes=["GSE1"],
        unscheduled=[{"course_code": "GSE1", "reason_code": "NOT_ON_FILE", "reason": "…"}],
        credit_hours={"GSE1": 2},
    )
    row = facts.unplaced_courses[0]
    assert row["outcome"] == OUTCOME_NOT_PLACED
    assert row["reason_code"] == "NOT_ON_FILE"
    assert row["source"] == SOURCE_SYSTEM_RECOMMENDATION


# ── credits ──────────────────────────────────────────────────────────────────


def test_the_summary_separates_hours_already_held_from_hours_this_build_adds() -> None:
    facts = _facts(
        recommended_codes=["MATH204"],
        baseline=[_baseline_row("AI331", "M1", "MON", "09:00", "10:15")],
        mappings=[
            {"course_code": "MATH204", "section": "M1", "term_section_id": 5, "meetings": []}
        ],
        credit_hours={"AI331": 4, "MATH204": 3},
        cap=18,
    )
    assert facts.credit_summary == {
        "retained_credit_hours": 4,
        "new_credit_hours": 3,
        "total_plan_credit_hours": 7,
        "new_courses_credit_cap": 18,
    }


def test_a_credit_figure_that_came_from_the_fallback_says_so() -> None:
    """The executor used two fallbacks for one fact: `DEFAULT_CREDITS` when charging
    the solver's cap and `None` when reporting. A course with no ProgrammeRequirement
    row under the student's programme was charged 3 hours and reported as 0, so the
    cap and the summary disagreed silently."""
    facts = _facts(
        requested_codes=["MATH105"],
        mappings=[
            {"course_code": "MATH105", "section": "M2", "term_section_id": 9, "meetings": []}
        ],
        credit_hours={},
        default_credits=3,
    )
    assert facts.new_sections[0]["credit_hours"] == 3
    assert facts.new_sections[0]["credits_estimated"] is True
    assert facts.credit_summary["new_credit_hours"] == 3


def test_a_known_credit_figure_is_not_marked_as_a_guess() -> None:
    facts = _facts(requested_codes=["AI331"], credit_hours={"AI331": 4})
    assert "credits_estimated" not in facts.student_requested_courses[0]


def test_no_cap_is_null_rather_than_zero() -> None:
    """`max_credits` omitted reaches the solver as 0, meaning "no ceiling". Reporting
    that as `cap: 0` would read as a ceiling of zero hours."""
    assert _facts(cap=0).credit_summary["new_courses_credit_cap"] is None


# ── the payload is a copy ────────────────────────────────────────────────────


# ── the remote backend sees the same contract ────────────────────────────────


def test_the_provenance_survives_the_remote_projection() -> None:
    """A local/remote divergence here is invisible without this test.

    The projector is a strict allowlist built key by key, so every field added to
    the executor is silently dropped on the Alibaba backend until it is named
    there — the local model would answer with provenance and the remote one
    without, and nothing would go red. `note` is in the list for the same reason:
    it carries the rule that a partial result "must be reported as such, never as a
    failure", and the remote model was being held to it with the sentence removed.
    """
    from core.services.llm_remote_privacy import RemoteIdentityMap, project_tool_result_for_remote

    facts = _facts(
        requested_codes=["AI352"],
        recommended_codes=["AI331"],
        baseline=[_baseline_row("AI331", "M1", "MON", "09:00", "10:15")],
        mappings=[{"course_code": "AI352", "section": "M1", "term_section_id": 3, "meetings": []}],
        credit_hours={"AI352": 3, "AI331": 4},
    )
    local = {"ok": True, **facts.as_payload(), "note": "a partial result is normal", "tool": "x"}
    remote = project_tool_result_for_remote("build_my_timetable", local, RemoteIdentityMap())

    for key in (
        "student_requested_courses",
        "system_recommended_courses",
        "retained_sections",
        "new_sections",
        "section_replacements",
        "unplaced_courses",
        "credit_summary",
        "using_timetable_of_term",
        "note",
    ):
        assert key in remote, f"{key} is dropped on the remote backend and nowhere else"
    assert remote["retained_sections"][0]["course_code"] == "AI331"
    # `fixed_sections` is ABSENT here on purpose. The chat path cannot pin a section,
    # so an empty list would be a claim it has no basis for; the projector carries the
    # key only when a caller supplied one.
    assert "fixed_sections" not in local
    assert "fixed_sections" not in remote
    # And the identity rule still holds: no student id, no staff name.
    assert "student_id" not in remote
    assert "Dr Someone" not in str(remote)


def test_mutating_the_payload_cannot_change_the_facts() -> None:
    facts = _facts(requested_codes=["AI331"], credit_hours={"AI331": 4})
    payload = facts.as_payload()
    payload["student_requested_courses"].clear()
    payload["credit_summary"]["total_plan_credit_hours"] = 999
    assert len(facts.as_payload()["student_requested_courses"]) == 1
    assert facts.as_payload()["credit_summary"]["total_plan_credit_hours"] == 0


def test_an_unattributable_course_does_not_claim_the_system_recommended_it() -> None:
    """The fallback used to be SYSTEM_RECOMMENDATION, and that is a provenance CLAIM.

    A course the solver returned that appears in neither input list cannot be
    attributed, and saying the system recommended it is the same defect as the
    `requested` field this module replaced, one layer down — a row asserting it knows
    where it came from. Unreachable today, because the shortlist is built from those
    two lists; named so that it stays unreachable rather than silently mislabelled.
    """
    facts = _facts(
        mappings=[{"course_code": "ZZ999", "section": "M1", "term_section_id": 9, "meetings": []}],
        unscheduled=[{"course_code": "YY888", "reason_code": "NOT_ON_FILE", "reason": "…"}],
    )
    assert facts.new_sections[0]["source"] == "UNATTRIBUTED"
    assert facts.unplaced_courses[0]["source"] == "UNATTRIBUTED"


# ── the four invariants, executable ──────────────────────────────────────────


def _verified(**kwargs):
    from core.services.timetable_provenance import verify

    baseline = kwargs.pop("baseline", [])
    keep = kwargs.pop("keep_current", True)
    facts = _facts(baseline=baseline, **kwargs)
    verify(
        facts,
        baseline_codes={
            (r["course_code"], r.get("section", "")) for r in baseline_sections(baseline)
        },
        keep_current=keep,
    )
    return facts


def test_an_unattributable_course_is_refused_rather_than_explained() -> None:
    """UNATTRIBUTED is an internal contract failure, not a value a student may read.

    It means the solver returned a course that was in neither input list, so the
    payload cannot say where it came from. Shipping it would put "the system
    recommended this" one paraphrase away from an answer that knows nothing of the
    kind — the `requested` defect this module replaced, one layer down.
    """
    from core.services.timetable_provenance import TimetableProvenanceError

    with pytest.raises(TimetableProvenanceError, match="UNATTRIBUTED"):
        _verified(
            mappings=[
                {"course_code": "ZZ999", "section": "M1", "term_section_id": 9, "meetings": []}
            ]
        )


def test_keeping_the_current_sections_cannot_produce_a_replacement() -> None:
    """The mode's whole promise. A replacement here is the TT10 contradiction."""
    from core.services.timetable_provenance import TimetableProvenanceError

    base = [_baseline_row("CS323", "M1", "MON", "13:00", "14:15", term_section_id=11)]
    with pytest.raises(TimetableProvenanceError, match="keep-current"):
        _verified(
            recommended_codes=["CS323"],
            baseline=base,
            mappings=[
                {"course_code": "CS323", "section": "M2", "term_section_id": 22, "meetings": []}
            ],
            credit_hours={"CS323": 4},
            keep_current=True,
        )


def test_a_retained_section_must_come_from_the_baseline() -> None:
    from core.services.timetable_provenance import TimetableProvenanceError, verify

    facts = _facts(
        baseline=[_baseline_row("AI331", "M1", "MON", "09:00", "10:15")],
        credit_hours={"AI331": 4},
    )
    with pytest.raises(TimetableProvenanceError, match="baseline"):
        verify(facts, baseline_codes=set(), keep_current=True)


def test_the_credit_totals_must_reconcile_with_the_rows() -> None:
    from dataclasses import replace

    from core.services.timetable_provenance import TimetableProvenanceError, verify

    facts = _facts(
        recommended_codes=["MATH204"],
        mappings=[
            {"course_code": "MATH204", "section": "M1", "term_section_id": 5, "meetings": []}
        ],
        credit_hours={"MATH204": 3},
    )
    tampered = replace(
        facts, credit_summary={**facts.credit_summary, "total_plan_credit_hours": 99}
    )
    with pytest.raises(TimetableProvenanceError, match="total"):
        verify(tampered, baseline_codes=set(), keep_current=True)
