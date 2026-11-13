"""Taking a student's feedback on their expected timetable in.

What the student saw (grouping, identities, the fingerprint, the frozen
snapshot), the lists the form offers, and every check ``submit_point`` makes,
in order. Each refusal is asserted by its code, so a test cannot pass because a
DIFFERENT check happened to refuse.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db.models import F
from django.utils import timezone

from core.models import (
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackPoint,
    TimetableScenario,
)
from core.services import rate_limit
from core.services import timetable_feedback as feedback
from core.services.student_term_timetable import (
    TimetableUnavailable,
    read_student_term_timetable,
)

pytestmark = pytest.mark.django_db

SID = 4991001
EMPTY_SID = 4991002
PROG = "FBK"
YEAR = "1448"
TERM = "1"
PLAN = "registration_plan_1448_t1"


@pytest.fixture(autouse=True)
def _accepting(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    settings.TIMETABLE_FEEDBACK_ACCEPTING = True
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = ()
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))


def _section(code, label, meetings, *, name=None, programme=PROG, scenario=None):
    section = TermSection.objects.create(
        source_tag="other",
        course_name=name if name is not None else f"COURSE {code}",
        course_code=code[:2],
        course_number=code[2:],
        course_key=code,
        section=label,
        scenario=scenario,
    )
    for day, start, end, room in meetings:
        TermSectionMeeting.objects.create(
            term_section=section,
            day=day,
            start_time=start,
            end_time=end,
            room=room,
            instructor="Staff",
        )
    if programme:
        TermSectionProgram.objects.create(term_section=section, program=programme)
    return section


@pytest.fixture
def world(db):
    for sid in (SID, EMPTY_SID):
        Student.objects.create(
            student_id=sid,
            name=f"Student {sid}",
            program=PROG,
            section="M",
            status="منتظم",
            advisor_id="ADV-7",
            total_earned_credits=60,
            current_registered_credits=0,
        )
    plan = {
        "SA101": ("INTRO TO PROGRAMMING", "Mandatory"),
        "SB201": ("DATA STRUCTURES", "Mandatory"),
        "SC201": ("DISCRETE MATHEMATICS", "Mandatory"),
        "SD301": ("SOFTWARE DESIGN", "Mandatory"),
        "SE401": ("PROGRAM ELECTIVE I", "Program Elective"),
        "SF499": ("GRADUATION PROJECT", "Mandatory"),
        "SH100": ("ORIENTATION", "Mandatory"),
    }
    for code, (name, kind) in plan.items():
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=name,
            type=kind,
            programme_term=1,
            credit_hours=3,
        )
    Prerequisite.objects.create(program=PROG, course_code="SD301", prerequisite_course_code="SB201")
    passed = Course.objects.create(course_code="SH100", description="ORIENTATION", credit_hours=3)
    StudentCourse.objects.create(student_id=SID, course=passed, status="passed")

    scenario = TimetableScenario.objects.create(academic_year=YEAR, term=TERM, name="draft")
    s = {
        "SA101-M1": _section(
            "SA101",
            "M1",
            [("SUN", "09:00", "10:15", "A1"), ("TUE", "09:00", "10:15", "A1")],
            name="INTRO TO PROGRAMMING",
        ),
        "SA101-M2": _section(
            "SA101", "M2", [("MON", "13:00", "14:15", "B1")], name="INTRO TO PROGRAMMING"
        ),
        "SA101-M5": _section(
            "SA101", "M5", [("SUN", "09:30", "10:00", "B5")], name="INTRO TO PROGRAMMING"
        ),
        "SA101-F1": _section(
            "SA101", "F1", [("MON", "13:00", "14:15", "F1")], name="INTRO TO PROGRAMMING"
        ),
        "SA101-M3": _section(
            "SA101",
            "M3",
            [("WED", "13:00", "14:15", "B3")],
            name="INTRO TO PROGRAMMING",
            programme=None,
        ),
        "SA101-M4": _section(
            "SA101",
            "M4",
            [("WED", "13:00", "14:15", "B4")],
            name="INTRO TO PROGRAMMING",
            scenario=scenario,
        ),
        "SA101-M6": _section("SA101", "M6", [], name="INTRO TO PROGRAMMING"),
        "SB201-M1": _section(
            "SB201", "M1", [("SUN", "10:15", "11:30", "B2")], name="DATA STRUCTURES"
        ),
        "SB201-M2": _section(
            "SB201", "M2", [("SUN", "09:30", "10:45", "B2")], name="DATA STRUCTURES"
        ),
        "SC201-M1": _section(
            "SC201", "M1", [("WED", "08:00", "09:15", "C3")], name="DISCRETE MATHEMATICS"
        ),
        "SD301-M1": _section(
            "SD301", "M1", [("THU", "10:00", "11:15", "D1")], name="SOFTWARE DESIGN"
        ),
        "SE401-M1": _section("SE401", "M1", [("THU", "13:00", "14:15", "E1")], name="AI ELECTIVE"),
        "SH100-M1": _section("SH100", "M1", [("THU", "08:00", "09:15", "H1")], name="ORIENTATION"),
        "SG777-M1": _section("SG777", "M1", [("MON", "08:00", "09:15", "G1")], name="ELSEWHERE"),
        "SN000-M1": _section("SN000", "M1", [], name="NO TIMES YET"),
    }
    for key in ("SA101-M1", "SB201-M1", "SG777-M1", "SN000-M1"):
        StudentTermSection.objects.create(
            student_id=SID, academic_year=YEAR, term=TERM, term_section=s[key], source=PLAN
        )
    return s


def _timetable(sid=SID):
    return read_student_term_timetable(sid, YEAR, TERM)


def _fingerprint(sid=SID, language="en"):
    timetable = _timetable(sid)
    sections = feedback.seen_sections(timetable, programme=PROG, language=language)
    return feedback.forecast_fingerprint(
        academic_year=YEAR,
        term=TERM,
        forecast_class=timetable.forecast_class,
        sections=sections,
    )


def _refused(code, **kwargs):
    with pytest.raises(feedback.FeedbackRefused) as raised:
        feedback.submit_point(**kwargs)
    assert raised.value.code == code, raised.value.code
    return raised.value


def _base(world, **overrides):
    fields = {
        "student_id": SID,
        "anchor": f"SECTION|{world['SA101-M1'].pk}",
        "reason": "TIME_CLASH",
        "seen_fingerprint": _fingerprint(),
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# What the student saw
# ---------------------------------------------------------------------------


def _seen(world, language="en"):
    sections = feedback.seen_sections(_timetable(), programme=PROG, language=language)
    return {section.term_section_id: section for section in sections}


def test_the_forecast_is_seen_section_by_section(world):
    seen = _seen(world)
    sa = seen[world["SA101-M1"].pk]

    assert sorted(seen) == sorted(
        world[key].pk for key in ("SA101-M1", "SB201-M1", "SG777-M1", "SN000-M1")
    )
    assert sa.course_key == "SA101" and sa.section == "M1"
    assert sa.meetings == (("SUN", "09:00", "10:15", "A1"), ("TUE", "09:00", "10:15", "A1"))
    assert sa.section_course_identity == "SA101::INTRO TO PROGRAMMING"
    assert sa.plan_course_identity == "SA101::INTRO_TO_PROGRAMMING"
    assert sa.identity_certain is True
    assert (sa.credits, sa.credits_known) == (3, True)
    assert seen[world["SN000-M1"].pk].meetings == ()


def test_an_off_plan_course_has_no_known_credits_and_no_certain_identity(world):
    elsewhere = _seen(world)[world["SG777-M1"].pk]
    assert (elsewhere.credits, elsewhere.credits_known) == (0, False)
    assert elsewhere.identity_certain is False
    assert elsewhere.plan_course_identity == "SG777"


def test_a_code_recorded_under_two_names_is_not_a_certain_identity(world):
    _section("SA101", "M7", [("THU", "15:00", "16:15", "Z7")], name="أساسيات البرمجة")
    seen = _seen(world)[world["SA101-M1"].pk]
    assert seen.identity_certain is False
    # The physical identity keeps Arabic; the project's planner key would drop it.
    assert feedback.section_course_identity("SA101", "أساسيات البرمجة") == "SA101::أساسيات البرمجة"


def test_the_name_shown_in_arabic_is_kept_beside_the_stored_name(world):
    _section("SA101", "M8", [], name="مقدمة في البرمجة")
    sa = _seen(world, language="ar")[world["SA101-M1"].pk]
    assert sa.stored_course_name == "INTRO TO PROGRAMMING"
    assert sa.displayed_course_name == "مقدمة في البرمجة"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("SUN", "SUN"), (" sun ", "SUN"), ("Sunday", "SUN"), ("الأحد", "SUN"), ("الخميس", "THU")],
)
def test_days_have_one_spelling(value, expected):
    assert feedback.normalise_day(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("9:00", "09:00"), ("09:00", "09:00"), ("09:00:00", "09:00")]
)
def test_times_have_one_spelling(value, expected):
    assert feedback.normalise_time(value) == expected


# ---------------------------------------------------------------------------
# The fingerprint: what changes it and what does not
# ---------------------------------------------------------------------------


def test_the_fingerprint_survives_a_reimport_that_recreates_the_rows(world):
    before = _fingerprint()
    rows = list(
        StudentTermSection.objects.filter(student_id=SID).values("term_section_id", "source")
    )
    StudentTermSection.objects.filter(student_id=SID).delete()
    for row in rows:
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            term_section_id=row["term_section_id"],
            source=row["source"],
        )
    assert _fingerprint() == before


def test_the_fingerprint_ignores_names_language_and_instructors(world):
    before = _fingerprint()
    assert _fingerprint(language="ar") == before
    ProgrammeRequirement.objects.filter(program=PROG, course_code="SA101").update(
        course_name="RENAMED"
    )
    TermSection.objects.filter(pk=world["SA101-M1"].pk).update(course_name="RENAMED TOO")
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"]).update(instructor="Someone")
    assert _fingerprint() == before


def test_a_co_taught_meeting_counted_twice_is_one_meeting(world):
    before = _fingerprint()
    TermSectionMeeting.objects.create(
        term_section=world["SA101-M1"],
        day="SUN",
        start_time="09:00",
        end_time="10:15",
        room="A1",
        instructor="Second teacher",
    )
    assert _fingerprint() == before


def test_spelling_differences_in_a_label_or_a_time_change_nothing(world):
    before = _fingerprint()
    TermSection.objects.filter(pk=world["SA101-M1"].pk).update(section=" M1")
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"], day="TUE").update(
        start_time="9:00"
    )
    assert _fingerprint() == before


@pytest.mark.parametrize(
    "change",
    ["room", "start", "label", "working", "section"],
)
def test_a_real_change_to_the_timetable_changes_the_fingerprint(world, change):
    before = _fingerprint()
    sa = world["SA101-M1"]
    if change == "room":
        TermSectionMeeting.objects.filter(term_section=sa, day="SUN").update(room="A2")
    elif change == "start":
        TermSectionMeeting.objects.filter(term_section=sa, day="SUN").update(start_time="09:30")
    elif change == "label":
        TermSection.objects.filter(pk=sa.pk).update(section="M9")
    elif change == "working":
        StudentTermSection.objects.create(
            student_id=SID, academic_year=YEAR, term=TERM, term_section=sa, source="planner"
        )
    elif change == "section":
        StudentTermSection.objects.filter(student_id=SID, term_section=sa).update(
            term_section=world["SA101-M2"]
        )
    assert _fingerprint() != before


def test_clashes_are_paired_by_section_on_the_day_and_touching_is_not_a_clash(world):
    StudentTermSection.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, term_section=world["SB201-M2"], source=PLAN
    )
    clashes = feedback.clashes_between(_seen(world).values())

    def pair(first, second):
        a, b = sorted((world[first].pk, world[second].pk))
        return {"a_term_section_id": a, "b_term_section_id": b, "day": "SUN"}

    # SA101-M1 09:00-10:15, SB201-M1 10:15-11:30, SB201-M2 09:30-10:45 (all SUN).
    assert sorted(
        clashes, key=lambda c: (c["a_term_section_id"], c["b_term_section_id"])
    ) == sorted(
        [pair("SA101-M1", "SB201-M2"), pair("SB201-M1", "SB201-M2")],
        key=lambda c: (c["a_term_section_id"], c["b_term_section_id"]),
    )
    # SB201-M1 starts exactly when SA101-M1 ends: touching, not clashing.
    assert pair("SA101-M1", "SB201-M1") not in clashes


# ---------------------------------------------------------------------------
# The frozen snapshot
# ---------------------------------------------------------------------------


def _snapshot_of(world):
    reference = feedback.submit_point(**_base(world)).reference
    return TimetableFeedback.objects.get(reference=reference).points.get().seen_snapshot


def test_the_snapshot_holds_what_the_student_saw_and_no_instructor(world):
    snapshot = _snapshot_of(world)
    feedback.validate_point_snapshot(snapshot)
    assert snapshot["forecast_class"] == "expected"
    assert snapshot["programme"] == PROG and snapshot["cohort"] == "M"
    assert "Staff" not in str(snapshot)
    assert snapshot["facts"]["clashes"] == []  # SA101 ends as SB201 starts: touching only
    assert snapshot["facts"]["timetabled_credits"] == 6  # SA101 + SB201; SG777, SN000 unknown
    assert snapshot["facts"]["unknown_credit_sections"] == 2


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda s: s.update(instructor="Staff"),
        lambda s: s["sections"][0].update(instructor="Staff"),
        lambda s: s["sections"][0].update(registered_count=30),
        lambda s: s["facts"].update(other_students=[1]),
        lambda s: s.pop("cohort"),
        lambda s: s.update(schema=2),
    ],
)
def test_the_snapshot_refuses_anything_outside_its_allowlist(world, corrupt):
    snapshot = _snapshot_of(world)
    corrupt(snapshot)
    with pytest.raises(ValueError):
        feedback.validate_point_snapshot(snapshot)


def test_the_frozen_snapshot_does_not_follow_later_changes(world):
    reference = feedback.submit_point(**_base(world)).reference
    point = TimetableFeedback.objects.get(reference=reference).points.get()
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"]).update(room="MOVED")
    StudentTermSection.objects.filter(student_id=SID).delete()
    point.refresh_from_db()
    rooms = {m[3] for s in point.seen_snapshot["sections"] for m in s["meetings"]}
    assert "A1" in rooms and "MOVED" not in rooms


# ---------------------------------------------------------------------------
# The lists the form offers
# ---------------------------------------------------------------------------


def test_the_catalogue_is_the_programmes_and_the_cohorts_with_times(world):
    labels = {(s.course_key, s.section) for s in feedback.catalogue_sections_for(SID, PROG)}
    assert ("SA101", "M2") in labels
    assert ("SA101", "F1") not in labels, "the other cohort"
    assert ("SA101", "M3") not in labels, "no programme link"
    assert ("SA101", "M4") not in labels, "a scenario's section"
    assert ("SA101", "M6") not in labels, "no meeting times"


def test_a_student_with_no_recorded_cohort_is_offered_no_section(world):
    Student.objects.filter(student_id=SID).update(section="")
    assert feedback.catalogue_sections_for(SID, PROG) == []


def test_missing_course_candidates_are_plan_courses_with_a_section_this_term(world):
    codes = [
        c["course_key"]
        for c in feedback.missing_course_candidates(
            SID, PROG, _timetable(), feedback.catalogue_sections_for(SID, PROG)
        )
    ]
    assert codes == ["SC201", "SD301"]
    # SA101/SB201 are in the forecast, SE401 is the placeholder, SF499 has no
    # section, SH100 is passed.


# ---------------------------------------------------------------------------
# Taking a point in
# ---------------------------------------------------------------------------


def test_a_first_point_opens_the_terms_case(world):
    result = feedback.submit_point(**_base(world, note="It clashes with my lab."))

    case = TimetableFeedback.objects.get(reference=result.reference)
    point = case.points.get()
    assert result.created_case is True and result.duplicate is False
    assert (case.student_id, case.academic_year, case.term) == (SID, YEAR, TERM)
    assert case.status == "SUBMITTED" and case.version == 1
    assert case.routed_advisor_id == "ADV-7"
    assert (case.program_at_submission, case.cohort_at_submission) == (PROG, "M")
    assert case.standing_at_submission == "منتظم"
    assert case.has_registration_at_submission is False
    assert case.submitted_language == "en"
    assert point.anchor_key == f"SECTION|{world['SA101-M1'].pk}"
    assert point.section_course_identity == "SA101::INTRO TO PROGRAMMING"
    assert point.seen_fingerprint == _fingerprint()
    assert list(case.events.values_list("kind", "point_id")) == [
        ("SUBMITTED", None),
        ("POINT_ADDED", point.pk),
    ]


def test_a_second_point_joins_the_open_case(world):
    first = feedback.submit_point(**_base(world))
    second = feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))

    assert second.reference == first.reference and second.created_case is False
    case = TimetableFeedback.objects.get(reference=first.reference)
    assert case.points.count() == 2 and case.version == 2
    assert case.events.filter(kind="SUBMITTED").count() == 1


def test_the_exact_same_point_returns_its_reference_and_writes_nothing(world):
    first = feedback.submit_point(**_base(world, note="same"))
    again = feedback.submit_point(**_base(world, note="same"))

    assert again.duplicate is True and again.reference == first.reference
    assert again.point_id == first.point_id
    assert TimetableFeedbackPoint.objects.count() == 1


def test_the_same_point_with_another_note_is_directed_to_a_follow_up(world):
    first = feedback.submit_point(**_base(world, note="first"))
    refused = _refused("ALREADY_RAISED", **_base(world, note="second"))
    assert refused.reference == first.reference and refused.status == 409


def test_the_same_point_about_a_changed_timetable_is_a_new_point(world):
    feedback.submit_point(**_base(world))
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"], day="SUN").update(room="A9")
    feedback.submit_point(**_base(world))
    assert TimetableFeedbackPoint.objects.count() == 2


def test_a_stale_form_is_refused_with_the_new_fingerprint_and_spends_an_attempt(world):
    stale = _fingerprint()
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"], day="SUN").update(room="A9")

    refused = _refused("TIMETABLE_CHANGED", **_base(world, seen_fingerprint=stale))

    assert refused.status == 409 and refused.fingerprint == _fingerprint()
    assert not TimetableFeedback.objects.exists()
    assert rate_limit.RateLimitBucket.objects.filter(
        key=f"{rate_limit.TIMETABLE_FEEDBACK_ATTEMPT}:{SID}"
    ).exists()


def test_a_failed_read_is_unavailable_and_never_no_timetable(world, monkeypatch):
    def failing(*_args, **_kwargs):
        raise TimetableUnavailable("down")

    monkeypatch.setattr(feedback, "read_student_term_timetable", failing)
    refused = _refused(
        "TIMETABLE_UNAVAILABLE",
        student_id=EMPTY_SID,
        anchor="NO_FORECAST",
        reason="NO_FORECAST",
        seen_fingerprint="x",
    )
    assert refused.status == 503
    assert not TimetableFeedback.objects.exists()


@pytest.mark.parametrize("anchor", ["SECTION|999999", "SECTION|", "SECTION|abc", "COURSE|1", ""])
def test_an_anchor_outside_the_forecast_is_refused(world, anchor):
    _refused("UNKNOWN_ANCHOR", **_base(world, anchor=anchor))


def test_another_students_section_is_not_an_anchor(world):
    _refused("UNKNOWN_ANCHOR", **_base(world, anchor=f"SECTION|{world['SC201-M1'].pk}"))


def test_the_whole_timetable_is_no_anchor_without_one(world):
    _refused(
        "UNKNOWN_ANCHOR",
        student_id=EMPTY_SID,
        anchor="TIMETABLE",
        reason="LOAD_TOO_HIGH",
        seen_fingerprint=_fingerprint(EMPTY_SID),
    )


@pytest.mark.parametrize("reason", ["LOAD_TOO_HIGH", "MISSING_COURSE", "NO_FORECAST", "BOGUS"])
def test_a_reason_that_does_not_fit_a_section_is_refused(world, reason):
    _refused("REASON_DOES_NOT_FIT", **_base(world, reason=reason))


def test_a_section_with_times_cannot_be_said_to_have_none(world):
    _refused("REASON_DOES_NOT_FIT", **_base(world, reason="SECTION_HAS_NO_TIMES"))
    result = feedback.submit_point(
        **_base(world, anchor=f"SECTION|{world['SN000-M1'].pk}", reason="SECTION_HAS_NO_TIMES")
    )
    assert result.created_case


def test_other_needs_a_note_and_a_note_has_a_limit(world):
    _refused("NOTE_REQUIRED", **_base(world, reason="OTHER", note="   "))
    _refused("NOTE_TOO_LONG", **_base(world, note="x" * (feedback.NOTE_MAX_CHARS + 1)))


@pytest.mark.parametrize(
    "requested",
    ["SA101-F1", "SA101-M3", "SA101-M4", "SA101-M1", "SB201-M2", "missing", "SA101-M6"],
)
def test_a_requested_section_the_form_did_not_offer_is_one_generic_refusal(world, requested):
    wanted = world[requested].pk if requested in world else 987654
    refused = _refused(
        "NOT_OFFERED",
        **_base(world, reason="PREFER_OTHER_SECTION", requested_term_section_id=wanted),
    )
    assert refused.status == 400


def test_another_section_must_be_named_for_prefer_other_and_only_where_it_fits(world):
    _refused("NOT_OFFERED", **_base(world, reason="PREFER_OTHER_SECTION"))
    _refused(
        "NOT_OFFERED",
        **_base(world, reason="ALREADY_PASSED", requested_term_section_id=world["SA101-M2"].pk),
    )


def test_a_requested_section_is_recorded_with_what_it_would_clash_with(world):
    calm = feedback.submit_point(
        **_base(
            world, reason="PREFER_OTHER_SECTION", requested_term_section_id=world["SA101-M2"].pk
        )
    )
    point = TimetableFeedbackPoint.objects.get(pk=calm.point_id)
    assert (point.requested_term_section_id, point.requested_section_label) == (
        world["SA101-M2"].pk,
        "M2",
    )
    assert point.seen_snapshot["facts"]["requested_clashes"] == []

    busy = feedback.submit_point(
        **_base(world, reason="TIME_UNSUITABLE", requested_term_section_id=world["SA101-M5"].pk)
    )
    snapshot = TimetableFeedbackPoint.objects.get(pk=busy.point_id).seen_snapshot
    # SA101-M5 meets SUN 09:30-10:00: inside nothing else of the forecast but SA101
    # itself, which is the section being replaced, so it does not count.
    assert snapshot["facts"]["requested_clashes"] == []


def test_a_requested_section_that_clashes_with_the_rest_is_recorded_not_refused(world):
    StudentTermSection.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, term_section=world["SC201-M1"], source=PLAN
    )
    late = _section("SA101", "M10", [("WED", "08:30", "09:45", "B10")], name="INTRO TO PROGRAMMING")
    result = feedback.submit_point(
        **_base(world, reason="TIME_CLASH", requested_term_section_id=late.pk)
    )
    snapshot = TimetableFeedbackPoint.objects.get(pk=result.point_id).seen_snapshot
    assert snapshot["facts"]["requested_clashes"] == [world["SC201-M1"].pk]


@pytest.mark.parametrize("course", ["SE401", "SF499", "SH100", "SA101", "ZZ999", ""])
def test_a_missing_course_the_form_did_not_offer_is_refused(world, course):
    _refused(
        "NOT_OFFERED",
        **_base(world, anchor="TIMETABLE", reason="MISSING_COURSE", requested_course_key=course),
    )


def test_a_missing_course_is_recorded_with_its_missing_prerequisites(world):
    result = feedback.submit_point(
        **_base(world, anchor="TIMETABLE", reason="MISSING_COURSE", requested_course_key="sd301")
    )
    point = TimetableFeedbackPoint.objects.get(pk=result.point_id)
    assert (point.requested_course_key, point.requested_plan_course_identity) == (
        "SD301",
        "SD301::SOFTWARE_DESIGN",
    )
    assert point.anchor_key == "TIMETABLE" and point.term_section_id is None
    assert point.seen_snapshot["facts"]["requested_prerequisites_missing"] == ["SB201"]


def test_no_timetable_is_heard_only_from_a_student_with_nothing_at_all(world):
    result = feedback.submit_point(
        student_id=EMPTY_SID,
        anchor="NO_FORECAST",
        reason="NO_FORECAST",
        seen_fingerprint=_fingerprint(EMPTY_SID),
    )
    point = TimetableFeedbackPoint.objects.get(pk=result.point_id)
    assert (point.anchor_kind, point.reason_code) == ("NO_FORECAST", "NO_FORECAST")
    assert point.seen_snapshot["forecast_class"] == "none"


def test_no_timetable_is_refused_beside_a_forecast_or_a_registration(world):
    _refused("NO_FORECAST_NOT_ALLOWED", **_base(world, anchor="NO_FORECAST", reason="NO_FORECAST"))
    StudentTermSection.objects.create(
        student_id=EMPTY_SID,
        academic_year=YEAR,
        term=TERM,
        term_section=world["SC201-M1"],
        source="scraper_timetable",
    )
    _refused(
        "NO_FORECAST_NOT_ALLOWED",
        student_id=EMPTY_SID,
        anchor="NO_FORECAST",
        reason="NO_FORECAST",
        seen_fingerprint=_fingerprint(EMPTY_SID),
    )


def test_a_term_allows_three_cases(world):
    for _ in range(3):
        TimetableFeedback.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            status="CLOSED",
            close_reason="ANSWER_ACCEPTED",
        )
    _refused("TOO_MANY_CASES", **_base(world))


def test_a_new_case_waits_a_day_after_a_withdrawal(world):
    now = timezone.now()
    TimetableFeedback.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        status="WITHDRAWN",
        closed_at=now - timedelta(hours=23),
    )
    _refused("COOLING_DOWN", **_base(world, now=now))
    assert feedback.submit_point(**_base(world, now=now + timedelta(hours=2))).created_case


def test_a_case_holds_eight_live_points(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK, (100, 3600))
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_DAILY, (100, 86_400))
    reasons = [
        "TIME_CLASH",
        "TIME_UNSUITABLE",
        "ALREADY_PASSED",
        "PREREQUISITE_NOT_MET",
        "NOT_IN_MY_PLAN",
    ]
    for reason in reasons:
        feedback.submit_point(**_base(world, reason=reason))
    for reason in ("LOAD_TOO_HIGH", "LOAD_TOO_LOW", "TOO_MANY_CAMPUS_DAYS"):
        feedback.submit_point(**_base(world, anchor="TIMETABLE", reason=reason))
    _refused("TOO_MANY_POINTS", **_base(world, anchor="TIMETABLE", reason="GRADUATION_CONCERN"))


@pytest.mark.parametrize(
    ("enabled", "accepting", "pilot", "taken"),
    [
        (False, True, (), False),
        (True, False, (), False),
        (True, True, ("AI2",), False),
        (True, True, ("AI2", PROG), True),
        (True, True, (), True),
    ],
)
def test_feedback_is_taken_only_while_switched_on_for_the_programme(
    world, settings, enabled, accepting, pilot, taken
):
    settings.TIMETABLE_FEEDBACK_ENABLED = enabled
    settings.TIMETABLE_FEEDBACK_ACCEPTING = accepting
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = pilot
    if taken:
        assert feedback.submit_point(**_base(world)).created_case
    else:
        refused = _refused("NOT_ACCEPTING", **_base(world))
        assert refused.status == 403


def test_attempts_are_limited_before_the_forecast_is_read(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_ATTEMPT, (1, 3600))
    reads = []
    real = feedback.read_student_term_timetable

    def counting(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(feedback, "read_student_term_timetable", counting)
    fields = _base(world)
    reads.clear()
    feedback.submit_point(**fields)
    refused = _refused("RATE_LIMITED", **fields)
    assert refused.status == 429 and refused.retry_after > 0
    assert len(reads) == 1, "the refused attempt never rebuilt the forecast"


def test_accepted_points_are_limited_per_hour_duplicates_included(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK, (2, 3600))
    feedback.submit_point(**_base(world))
    feedback.submit_point(**_base(world))  # the duplicate spends too
    _refused("RATE_LIMITED", **_base(world, reason="TIME_UNSUITABLE"))


def test_an_unknown_student_is_refused(world):
    _refused("UNKNOWN_STUDENT", **_base(world, student_id=4991999))


def test_the_case_and_its_events_are_untouched_by_a_refusal(world):
    feedback.submit_point(**_base(world))
    case = TimetableFeedback.objects.get()
    version, events = case.version, case.events.count()
    _refused("REASON_DOES_NOT_FIT", **_base(world, reason="LOAD_TOO_HIGH"))
    case.refresh_from_db()
    assert (case.version, case.events.count()) == (version, events)


def test_a_point_is_written_in_arabic_when_the_student_wrote_in_arabic(world):
    result = feedback.submit_point(
        **_base(world, language="ar-SA", seen_fingerprint=_fingerprint())
    )
    case = TimetableFeedback.objects.get(reference=result.reference)
    snapshot = case.points.get().seen_snapshot
    assert case.submitted_language == "ar" and snapshot["submitted_language"] == "ar"


def test_an_event_is_written_for_every_point(world):
    feedback.submit_point(**_base(world))
    feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))
    kinds = list(TimetableFeedbackEvent.objects.values_list("kind", flat=True))
    assert kinds.count("POINT_ADDED") == 2 and kinds.count("SUBMITTED") == 1


def test_the_forecasts_source_alone_changes_the_fingerprint(world):
    """A staff mapping of exactly the same sections replaces the imported plan: the
    same sections at the same times, but a point would be about another forecast."""
    before = _fingerprint()
    for row in list(StudentTermSection.objects.filter(student_id=SID, source=PLAN)):
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            term_section_id=row.term_section_id,
            source="planner",
        )
    assert _timetable().forecast_class == "working"
    assert _fingerprint() != before


@pytest.mark.parametrize("moved", ["version", "closed"])
def test_a_case_that_moved_on_meanwhile_is_not_written_into(world, monkeypatch, moved):
    first = feedback.submit_point(**_base(world))
    stale = TimetableFeedback.objects.get(reference=first.reference)
    if moved == "version":
        TimetableFeedback.objects.filter(pk=stale.pk).update(version=F("version") + 1)
    else:
        TimetableFeedback.objects.filter(pk=stale.pk).update(
            status="CLOSED", close_reason="DUPLICATE"
        )
    monkeypatch.setattr(feedback, "_open_case", lambda **_kwargs: (stale, False))

    _refused("CASE_CHANGED", **_base(world, reason="TIME_UNSUITABLE"))

    assert TimetableFeedbackPoint.objects.count() == 1


def test_the_same_point_landing_between_check_and_write_is_answered_as_the_duplicate(
    world, monkeypatch
):
    first = feedback.submit_point(**_base(world, note="same"))
    real = feedback._live_point
    calls = []

    def missed_once(case, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else real(case, **kwargs)

    monkeypatch.setattr(feedback, "_live_point", missed_once)

    again = feedback.submit_point(**_base(world, note="same"))

    assert again.duplicate is True and again.point_id == first.point_id
    assert TimetableFeedbackPoint.objects.count() == 1
    assert len(calls) == 2, "the write was refused by the database and the point re-read"
    assert TimetableFeedback.objects.get().version == 1, "the claim was undone with the write"


# ---------------------------------------------------------------------------
# Review round 1: what was asked for instead is part of the point
# ---------------------------------------------------------------------------


def test_two_missing_courses_are_two_points(world):
    first = feedback.submit_point(
        **_base(world, anchor="TIMETABLE", reason="MISSING_COURSE", requested_course_key="SC201")
    )
    second = feedback.submit_point(
        **_base(world, anchor="TIMETABLE", reason="MISSING_COURSE", requested_course_key="SD301")
    )
    assert second.duplicate is False and second.point_id != first.point_id
    assert sorted(TimetableFeedbackPoint.objects.values_list("request_key", flat=True)) == [
        "COURSE|SC201",
        "COURSE|SD301",
    ]


def test_two_preferred_sections_are_two_points(world):
    for key in ("SA101-M2", "SA101-M5"):
        result = feedback.submit_point(
            **_base(world, reason="PREFER_OTHER_SECTION", requested_term_section_id=world[key].pk)
        )
        assert result.duplicate is False
    assert TimetableFeedbackPoint.objects.count() == 2


def test_asking_for_a_section_after_raising_a_clash_is_a_new_point(world):
    feedback.submit_point(**_base(world))
    later = feedback.submit_point(**_base(world, requested_term_section_id=world["SA101-M2"].pk))
    assert later.duplicate is False
    point = TimetableFeedbackPoint.objects.get(pk=later.point_id)
    assert point.requested_term_section_id == world["SA101-M2"].pk
    assert point.request_key == f"SECTION|{world['SA101-M2'].pk}"


def test_the_same_request_with_another_note_is_already_raised(world):
    fields = {"anchor": "TIMETABLE", "reason": "MISSING_COURSE", "requested_course_key": "SD301"}
    feedback.submit_point(**_base(world, note="first", **fields))
    _refused("ALREADY_RAISED", **_base(world, note="second", **fields))


# ---------------------------------------------------------------------------
# Review round 1: an answered case is reopened by a new point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("handled", "expected"), [(False, "SUBMITTED"), (True, "IN_REVIEW")])
def test_a_new_point_reopens_an_answered_case(world, handled, expected):
    from django.contrib.auth.models import User

    first = feedback.submit_point(**_base(world))
    staff = User.objects.create_user("advisor-reopen") if handled else None
    TimetableFeedback.objects.filter(reference=first.reference).update(
        status="ANSWERED", handled_by=staff
    )

    result = feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))

    case = TimetableFeedback.objects.get(reference=first.reference)
    assert result.reopened is True and case.status == expected
    reopened = case.events.get(kind="REOPENED")
    assert (reopened.from_value, reopened.to_value, reopened.point_id) == (
        "ANSWERED",
        expected,
        result.point_id,
    )


def test_a_point_into_a_case_still_open_reopens_nothing(world):
    feedback.submit_point(**_base(world))
    result = feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))
    assert result.reopened is False
    assert not TimetableFeedbackEvent.objects.filter(kind="REOPENED").exists()


# ---------------------------------------------------------------------------
# Review round 1: whose case, which term
# ---------------------------------------------------------------------------


def test_each_student_has_their_own_case(world):
    StudentTermSection.objects.create(
        student_id=EMPTY_SID,
        academic_year=YEAR,
        term=TERM,
        term_section=world["SB201-M1"],
        source=PLAN,
    )
    mine = feedback.submit_point(**_base(world))
    theirs = feedback.submit_point(
        student_id=EMPTY_SID,
        anchor=f"SECTION|{world['SB201-M1'].pk}",
        reason="TIME_UNSUITABLE",
        seen_fingerprint=_fingerprint(EMPTY_SID),
    )
    assert theirs.created_case is True and theirs.reference != mine.reference
    assert TimetableFeedback.objects.get(reference=theirs.reference).student_id == EMPTY_SID


def test_each_term_has_its_own_case(world, monkeypatch):
    autumn = feedback.submit_point(**_base(world))
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term="2",
        term_section=world["SA101-M1"],
        source="registration_plan_1448_t2",
    )
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, "2"))
    timetable = read_student_term_timetable(SID, YEAR, "2")
    fingerprint = feedback.forecast_fingerprint(
        academic_year=YEAR,
        term="2",
        forecast_class=timetable.forecast_class,
        sections=feedback.seen_sections(timetable, programme=PROG),
    )

    spring = feedback.submit_point(**_base(world, seen_fingerprint=fingerprint))

    assert spring.created_case is True and spring.reference != autumn.reference
    assert TimetableFeedback.objects.get(reference=spring.reference).term == "2"


def test_only_this_students_cases_in_this_term_count_toward_the_cap(world):
    for student_id, term in ((EMPTY_SID, TERM), (SID, "2")):
        for _ in range(3):
            TimetableFeedback.objects.create(
                student_id=student_id,
                academic_year=YEAR,
                term=term,
                status="CLOSED",
                close_reason="DUPLICATE",
            )
    assert feedback.submit_point(**_base(world)).created_case is True


def test_a_recent_withdrawal_counts_even_without_a_closing_time(world):
    # closed_at is empty, so the cool-down is measured from updated_at: now.
    TimetableFeedback.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, status="WITHDRAWN"
    )
    _refused("COOLING_DOWN", **_base(world))


def test_only_a_withdrawal_starts_the_cool_down(world):
    TimetableFeedback.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        status="CLOSED",
        close_reason="ANSWER_ACCEPTED",
        closed_at=timezone.now(),
    )
    assert feedback.submit_point(**_base(world)).created_case is True


@pytest.mark.parametrize("refusal", ["COOLING_DOWN", "TOO_MANY_CASES"])
def test_a_refused_new_case_spends_no_success_budget(world, refusal):
    if refusal == "COOLING_DOWN":
        TimetableFeedback.objects.create(
            student_id=SID, academic_year=YEAR, term=TERM, status="WITHDRAWN"
        )
    else:
        for _ in range(3):
            TimetableFeedback.objects.create(
                student_id=SID,
                academic_year=YEAR,
                term=TERM,
                status="CLOSED",
                close_reason="DUPLICATE",
            )
    _refused(refusal, **_base(world))
    spent = set(
        rate_limit.RateLimitBucket.objects.filter(key__endswith=f":{SID}").values_list(
            "key", flat=True
        )
    )
    assert spent == {f"{rate_limit.TIMETABLE_FEEDBACK_ATTEMPT}:{SID}"}


# ---------------------------------------------------------------------------
# Review round 1: the fingerprint and the snapshot, more closely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["day", "end", "add_untimed", "remove_untimed"])
def test_the_fingerprint_follows_days_end_times_and_untimed_sections(world, change):
    before = _fingerprint()
    sa = world["SA101-M1"]
    if change == "day":
        TermSectionMeeting.objects.filter(term_section=sa, day="TUE").update(day="WED")
    elif change == "end":
        TermSectionMeeting.objects.filter(term_section=sa, day="TUE").update(end_time="10:30")
    elif change == "add_untimed":
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            term_section=world["SA101-M6"],
            source=PLAN,
        )
    else:
        StudentTermSection.objects.filter(student_id=SID, term_section=world["SN000-M1"]).delete()
    assert _fingerprint() != before


def test_the_fingerprint_names_its_term():
    common = {"forecast_class": "expected", "sections": []}
    first = feedback.forecast_fingerprint(academic_year=YEAR, term="1", **common)
    second = feedback.forecast_fingerprint(academic_year=YEAR, term="2", **common)
    assert first != second


def test_a_scenarios_own_spelling_does_not_make_an_identity_uncertain(world):
    scenario = TimetableScenario.objects.create(academic_year=YEAR, term=TERM, name="other")
    _section("SA101", "M12", [], name="SOMETHING ELSE", scenario=scenario)
    assert _seen(world)[world["SA101-M1"].pk].identity_certain is True


def test_the_snapshot_freezes_standing_registration_and_the_differences(world):
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=world["SC201-M1"],
        source="scraper_timetable",
    )
    result = feedback.submit_point(**_base(world))
    snapshot = TimetableFeedbackPoint.objects.get(pk=result.point_id).seen_snapshot
    assert snapshot["standing"] == "منتظم"
    assert snapshot["has_registration"] is True
    assert snapshot["forecast_not_registered"] == ["SA101", "SB201", "SG777", "SN000"]
    assert snapshot["registered_not_forecast"] == ["SC201"]


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda s: s["facts"]["clashes"].append(
            {"a_term_section_id": 1, "b_term_section_id": 2, "day": "SUN", "instructor": "x"}
        ),
        lambda s: s["sections"][0]["meetings"].append(["SUN", "09:00", "10:00", "A1", "Staff"]),
        lambda s: s["facts"]["requested_clashes"].append("SA101"),
        lambda s: s["facts"]["requested_prerequisites_missing"].append(5),
    ],
)
def test_the_snapshot_refuses_odd_shapes_below_the_top_level(world, corrupt):
    snapshot = _snapshot_of(world)
    corrupt(snapshot)
    with pytest.raises(ValueError):
        feedback.validate_point_snapshot(snapshot)


# ---------------------------------------------------------------------------
# Review round 1: refusals that must be 400s, and in the right order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "reason", "student"),
    [("TIMETABLE", "TIME_CLASH", SID), ("NO_FORECAST", "OTHER", EMPTY_SID)],
)
def test_a_reason_for_another_anchor_is_refused(world, anchor, reason, student):
    _refused(
        "REASON_DOES_NOT_FIT",
        student_id=student,
        anchor=anchor,
        reason=reason,
        note="context",
        seen_fingerprint=_fingerprint(student),
    )


def test_a_section_without_a_label_is_no_anchor(world):
    blank = _section("SZ900", "", [("THU", "15:00", "16:15", "Z1")], name="UNLABELLED")
    StudentTermSection.objects.create(
        student_id=SID, academic_year=YEAR, term=TERM, term_section=blank, source=PLAN
    )
    _refused("UNKNOWN_ANCHOR", **_base(world, anchor=f"SECTION|{blank.pk}"))


def test_a_crafted_numeral_is_no_anchor_rather_than_a_server_error(world):
    _refused("UNKNOWN_ANCHOR", **_base(world, anchor="SECTION|²"))


def test_a_section_that_left_the_forecast_while_the_student_wrote_is_a_changed_timetable(world):
    stale = _fingerprint()
    StudentTermSection.objects.filter(student_id=SID, term_section=world["SA101-M1"]).delete()
    refused = _refused("TIMETABLE_CHANGED", **_base(world, seen_fingerprint=stale))
    assert refused.fingerprint == _fingerprint()


def test_a_note_of_exactly_the_limit_is_taken(world):
    result = feedback.submit_point(**_base(world, note="x" * feedback.NOTE_MAX_CHARS))
    assert len(TimetableFeedbackPoint.objects.get(pk=result.point_id).note) == 1000


def test_the_daily_budget_is_its_own_limit(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_DAILY, (1, 86_400))
    feedback.submit_point(**_base(world))
    _refused("RATE_LIMITED", **_base(world, reason="TIME_UNSUITABLE"))


def test_a_withdrawn_point_may_be_raised_again_and_frees_its_place(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK, (100, 3600))
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK_DAILY, (100, 86_400))
    first = feedback.submit_point(**_base(world))
    TimetableFeedbackPoint.objects.filter(pk=first.point_id).update(withdrawn_at=timezone.now())

    again = feedback.submit_point(**_base(world))
    assert again.duplicate is False and again.point_id != first.point_id

    for reason in ("TIME_UNSUITABLE", "ALREADY_PASSED", "PREREQUISITE_NOT_MET", "NOT_IN_MY_PLAN"):
        feedback.submit_point(**_base(world, reason=reason))
    for reason in ("LOAD_TOO_HIGH", "LOAD_TOO_LOW"):
        feedback.submit_point(**_base(world, anchor="TIMETABLE", reason=reason))
    # Seven live points and one withdrawn: the eighth live point still fits.
    assert feedback.submit_point(**_base(world, anchor="TIMETABLE", reason="TOO_MANY_CAMPUS_DAYS"))


def test_an_in_lock_refusal_leaves_the_case_untouched(world):
    feedback.submit_point(**_base(world, note="first"))
    case = TimetableFeedback.objects.get()
    version, events = case.version, case.events.count()
    _refused("ALREADY_RAISED", **_base(world, note="second"))
    case.refresh_from_db()
    assert (case.version, case.events.count()) == (version, events)


def test_the_point_recovery_answers_a_different_note_as_already_raised(world, monkeypatch):
    feedback.submit_point(**_base(world, note="first"))
    case = TimetableFeedback.objects.get()
    real = feedback._live_point
    calls = []

    def missed_once(case, **kwargs):
        calls.append(1)
        return None if len(calls) == 1 else real(case, **kwargs)

    monkeypatch.setattr(feedback, "_live_point", missed_once)
    _refused("ALREADY_RAISED", **_base(world, note="second"))
    case.refresh_from_db()
    assert case.version == 1, "the claim was undone with the write"


# ---------------------------------------------------------------------------
# Review round 1: the lists, more closely
# ---------------------------------------------------------------------------


def test_a_placeholder_spelt_in_capitals_is_still_a_placeholder(world):
    ProgrammeRequirement.objects.filter(program=PROG, course_code="SE401").update(
        type="PROGRAM ELECTIVE"
    )
    codes = [
        c["course_key"]
        for c in feedback.missing_course_candidates(
            SID, PROG, _timetable(), feedback.catalogue_sections_for(SID, PROG)
        )
    ]
    assert "SE401" not in codes


def test_missing_prerequisites_are_read_normalised(world):
    Prerequisite.objects.filter(program=PROG, course_code="SD301").update(
        prerequisite_course_code="SB201, SH100"
    )
    result = feedback.submit_point(
        **_base(world, anchor="TIMETABLE", reason="MISSING_COURSE", requested_course_key="SD301")
    )
    facts = TimetableFeedbackPoint.objects.get(pk=result.point_id).seen_snapshot["facts"]
    assert facts["requested_prerequisites_missing"] == ["SB201"]


def test_a_section_of_another_course_with_the_same_code_is_not_offered(world):
    """The same course is the same CODE::NAME, never the bare code."""
    other = _section("SA101", "M11", [("THU", "15:00", "16:15", "Z2")], name="أساسيات البرمجة")
    _refused(
        "NOT_OFFERED",
        **_base(world, reason="PREFER_OTHER_SECTION", requested_term_section_id=other.pk),
    )


def test_the_way_in_imports_only_what_it_reads_with():
    """An allowlist, not a denylist: any new import here is a decision a reviewer
    must see, and nothing that writes a timetable may be among them. It writes
    feedback rows, and rate-limit rows through ``rate_limit``, and nothing else."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(feedback))
    modules = set()
    from_student_sections = set()
    from_services = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            if node.module == "core.services.student_sections":
                from_student_sections.update(alias.name for alias in node.names)
            if node.module == "core.services":
                from_services.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    # `from core.services import ...` names whole service modules; only the rate
    # limiter, which writes nothing but its own counters, may come in that way.
    assert from_services == {"rate_limit"}

    assert modules == {
        "__future__",
        "hashlib",
        "json",
        "unicodedata",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "django.conf",
        "django.db",
        "django.db.models",
        "django.utils",
        "core.models",
        "core.services",
        "core.services.course_identity",
        "core.services.eligibility",
        "core.services.planner_drafts",
        "core.services.section_programmes",
        "core.services.student_helpers",
        "core.services.student_sections",
        "core.services.student_term_timetable",
    }
    assert from_student_sections <= {
        "UnknownStudentGender",
        "arabic_term_section_course_names",
        "cohort_of_student_section",
        "gender_section_filter",
        "normalize_section_label",
        "section_course_key",
        "section_is_available_to_student",
        "student_gender_strict",
    }


def test_a_status_change_that_skipped_the_version_still_stops_the_claim(world, monkeypatch):
    """The claim names the status it read as well as the version, so a writer that
    changed the status without bumping the version cannot slip a point into an
    answered case without reopening it."""
    first = feedback.submit_point(**_base(world))
    stale = TimetableFeedback.objects.get(reference=first.reference)  # read as SUBMITTED
    TimetableFeedback.objects.filter(pk=stale.pk).update(status="ANSWERED")  # version kept
    monkeypatch.setattr(feedback, "_open_case", lambda **_kwargs: (stale, False))

    _refused("CASE_CHANGED", **_base(world, reason="TIME_UNSUITABLE"))

    assert TimetableFeedbackPoint.objects.count() == 1


def test_a_finished_case_is_never_written_into_even_if_handed_in(world, monkeypatch):
    """Even a case object that agrees with the database, version and status, is
    refused once its status is a finished one."""
    first = feedback.submit_point(**_base(world))
    TimetableFeedback.objects.filter(reference=first.reference).update(
        status="CLOSED", close_reason="DUPLICATE"
    )
    finished = TimetableFeedback.objects.get(reference=first.reference)
    monkeypatch.setattr(feedback, "_open_case", lambda **_kwargs: (finished, False))

    _refused("CASE_CHANGED", **_base(world, reason="TIME_UNSUITABLE"))

    assert TimetableFeedbackPoint.objects.count() == 1


# ---------------------------------------------------------------------------
# Review round 2
# ---------------------------------------------------------------------------


def test_a_students_third_case_still_takes_more_points(world):
    """The per-term cap stops a FOURTH case being opened, never a point joining
    the open third one."""
    for _ in range(2):
        TimetableFeedback.objects.create(
            student_id=SID,
            academic_year=YEAR,
            term=TERM,
            status="CLOSED",
            close_reason="DUPLICATE",
        )
    third = feedback.submit_point(**_base(world))
    more = feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))
    assert third.created_case is True
    assert more.created_case is False and more.reference == third.reference


@pytest.mark.parametrize("status", ["IN_REVIEW", "NEEDS_INFORMATION", "FORWARDED"])
def test_only_an_answered_case_is_reopened_by_a_new_point(world, status):
    first = feedback.submit_point(**_base(world))
    TimetableFeedback.objects.filter(reference=first.reference).update(status=status)

    result = feedback.submit_point(**_base(world, reason="TIME_UNSUITABLE"))

    case = TimetableFeedback.objects.get(reference=first.reference)
    assert result.reopened is False and case.status == status
    assert not case.events.filter(kind="REOPENED").exists()


def test_a_section_without_a_stored_key_is_read_as_the_reader_reads_it(world):
    """A course offered only by sections whose key was never stored is still offered."""
    for code, name in (("SJ210", "JAVA"), ("SK310", "KERNELS")):
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=name,
            type="Mandatory",
            programme_term=2,
            credit_hours=3,
        )
    split = TermSection.objects.create(
        source_tag="other",
        course_name="JAVA",
        course_code="SJ",
        course_number="210",
        course_key="",
        section="M1",
    )
    whole = TermSection.objects.create(
        source_tag="other",
        course_name="KERNELS",
        course_code="SK310",
        course_number="SK310",
        course_key="",
        section="M2",  # the global (key, section) pair is unique, blank key included
    )
    for section in (split, whole):
        TermSectionMeeting.objects.create(
            term_section=section, day="THU", start_time="13:00", end_time="14:15", room="K1"
        )
        TermSectionProgram.objects.create(term_section=section, program=PROG)

    codes = [
        c["course_key"]
        for c in feedback.missing_course_candidates(
            SID, PROG, _timetable(), feedback.catalogue_sections_for(SID, PROG)
        )
    ]
    assert {"SJ210", "SK310"} <= set(codes)


@pytest.mark.parametrize("bad", [chr(0), chr(7), chr(27)])
def test_a_note_with_a_control_character_is_refused(world, bad):
    _refused("NOTE_INVALID", **_base(world, note=f"a{bad}b"))


def test_a_note_keeps_its_line_breaks_and_tabs(world):
    note = "first line" + chr(10) + "second" + chr(9) + "line" + chr(13) + chr(10)
    result = feedback.submit_point(**_base(world, note=note))
    assert TimetableFeedbackPoint.objects.get(pk=result.point_id).note == note.strip()


def test_the_alternatives_are_the_same_course_in_the_catalogue_and_not_the_anchor(world):
    _section("SA101", "M11", [("THU", "15:00", "16:15", "Z2")], name="أساسيات البرمجة")
    anchor = _seen(world)[world["SA101-M1"].pk]
    labels = sorted(s.section for s in feedback.alternative_sections_for(SID, PROG, anchor))
    assert labels == ["M2", "M5"]
