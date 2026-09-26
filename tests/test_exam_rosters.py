"""Rosters rebuilt from the live lists: seat rule, flags, verdicts and gates.

Every saved run is test data (owner decision 1): nothing here exercises a
legacy path. The fixture run is made by the real build, so the saved keys the
roster reads are exactly the production ones.
"""

from __future__ import annotations

from datetime import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models import StudentTermSection, TermSection
from core.services.exam_rosters import (
    BASIS_NO_SEAT,
    BASIS_NOT_SCHEDULED,
    BASIS_SPLIT,
    BASIS_UNASSIGNED,
    BASIS_WHOLE,
    CHANGED,
    GONE,
    MATCHES,
    NEW,
    ListsTermMismatch,
    ListsUnavailable,
    RebuildRequired,
    RoomPart,
    build_roster_model,
    seat_students,
    start_time,
    student_exam_flags,
)
from core.services.exam_sections import resolve_exam_section_enrollment
from core.services.exam_timetable import _build_qa
from tests.exam_source_factory import scraped_exam_registration
from tests.exam_student_export_fixture import (
    FEMALE_CS2,
    MALE_AI,
    MALE_CS,
    MALE_IS,
    NO_COHORT,
    OVERLOADED,
    build_population,
    build_saved_run,
    save_payload,
    saved_payload,
)

pytestmark = pytest.mark.django_db


def _part(index: int, size: int, code: str | None = None, count: int = 2) -> RoomPart:
    return RoomPart(
        room_code=code or f"R{index}",
        room_group_index=index,
        room_group_count=count,
        student_count=size,
        slot_index=0,
        gender="M",
    )


# ── seat_students (owner decision 2) ───────────────────────────


def test_seat_rule_fills_parts_in_room_order_by_ascending_student_id():
    small, large = _part(1, 1, "SMALL"), _part(2, 50, "LARGE")
    members = list(range(5000, 5052))
    seats = seat_students(reversed(members), [large, small])
    assert seats[5000] is small
    assert [sid for sid in members if seats[sid] is large] == members[1:51]
    assert seats[5051] is None
    assert list(seats) == sorted(seats)


def test_seat_rule_never_absorbs_leftovers_into_spare_seats():
    only = RoomPart("R1", 1, 1, 5, 0, "M", capacity=40)
    seats = seat_students(range(10, 17), [only])
    assert [seats[sid] for sid in range(10, 15)] == [only] * 5
    assert seats[15] is None and seats[16] is None


def test_seat_rule_fills_real_rooms_before_unassigned_parts():
    unassigned = _part(1, 2, "UNASSIGNED")
    room = _part(2, 2, "ROOM")
    seats = seat_students([1, 2, 3, 4, 5], [unassigned, room])
    assert (seats[1], seats[2]) == (room, room)
    assert (seats[3], seats[4]) == (unassigned, unassigned)
    assert seats[5] is None


def test_seat_rule_is_exact_at_the_part_boundary():
    first, second = _part(1, 3), _part(2, 3)
    seats = seat_students(range(1, 7), [first, second])
    assert [seats[sid] for sid in range(1, 7)] == [first] * 3 + [second] * 3


# ── student_exam_flags ─────────────────────────────────────────


def _entry(code, slot, day, period):
    return {"course_code": code, "slot_index": slot, "day": day, "period": period}


SCHEDULE = [
    _entry("A", 0, "Sun", "08:00-10:00"),
    _entry("B", 0, "Sun", "08:00-10:00"),
    _entry("C", 1, "Sun", "13:00-15:00"),
    _entry("D", 2, "Mon", "08:00-10:00"),
    _entry("X", 9, "OVERFLOW", "Extra-9"),
    _entry("Y", 9, "OVERFLOW", "Extra-9"),
]


def test_clash_and_same_day_are_independent_whole_run_flags():
    flags = student_exam_flags(
        {"A": {1, 2}, "B": {1}, "C": {1, 3}, "D": {1, 2}, "X": {1}, "Y": {1}}, SCHEDULE
    )
    a, b, c = flags.sittings[(1, "A")], flags.sittings[(1, "B")], flags.sittings[(1, "C")]
    assert (a.clash_with, b.clash_with, c.clash_with) == (("B",), ("A",), ())
    assert a.same_day_with == (("13:00", "C"),) and b.same_day_with == (("13:00", "C"),)
    assert c.same_day_with == (("08:00", "A"), ("08:00", "B"))
    assert {a.exams_that_day, b.exams_that_day, c.exams_that_day} == {3}
    assert flags.sittings[(1, "D")].clash is False and flags.sittings[(1, "D")].same_day is False
    assert flags.clash_periods(1) == 1 and flags.day_has_clash(1, "Sun")


def test_exactly_two_exams_on_a_day_is_same_day_not_clash():
    flags = student_exam_flags({"A": {2}, "D": {2}, "C": {2}}, SCHEDULE)
    sitting = flags.sittings[(2, "A")]
    assert sitting.same_day and not sitting.clash and sitting.exams_that_day == 2
    assert flags.flagged(2)


def test_overflow_exams_never_flag_even_sharing_a_virtual_slot():
    flags = student_exam_flags({"X": {7}, "Y": {7}, "D": {7}}, SCHEDULE)
    assert (7, "X") not in flags.sittings and (7, "Y") not in flags.sittings
    assert flags.clash_periods(7) == 0 and not flags.flagged(7)
    assert flags.sittings[(7, "D")].exams_that_day == 1


def test_start_time_reads_only_a_real_clock_time():
    assert start_time("08:00-10:00") == time(8, 0)
    assert start_time(" 13:30 - 15:00") == time(13, 30)
    assert start_time("Extra-9") is None and start_time("25:00-26:00") is None


# ── The model on a real saved run ──────────────────────────────


@pytest.fixture
def saved_run():
    build_population()
    return build_saved_run()


def test_unchanged_lists_match_every_group_and_reproduce_saved_qa(saved_run):
    model = build_roster_model(saved_run)
    data = saved_payload(saved_run)
    assert {group.membership for group in model.groups} == {MATCHES}
    assert {group.program_mix for group in model.groups} == {MATCHES}
    assert model.lists_code_saved == model.lists_code_now and model.lists_match
    assert model.unchanged and model.differing_groups == []
    assert len(model.groups) == sum(len(rows) for rows in data["section_enrollment"].values())
    # The recomputed clash set is exactly the build's hard-constraint report.
    clashes = {(sid, slot) for sid in model.flags.days for slot in _clash_slots(model.flags, sid)}
    saved_clashes = {
        (row["student_id"], row["slot_index"]) for row in data["qa"]["same_slot_conflicts"]
    }
    assert clashes == saved_clashes and len(saved_clashes) == 17
    over = {
        (sid, day)
        for sid, days in model.flags.days.items()
        for day, exams in days.items()
        if len(exams) > data["qa"]["max_per_day"]
    }
    assert over == {(row["student_id"], row["day"]) for row in data["qa"]["overload_details"]}
    assert over == {(OVERLOADED, "Sun")}


def _clash_slots(flags, sid):
    slots = {}
    for exams in flags.days[sid].values():
        for slot, _exam, _start in exams:
            slots[slot] = slots.get(slot, 0) + 1
    return {slot for slot, count in slots.items() if count >= 2}


def test_flags_match_build_qa_on_the_live_population(saved_run):
    model = build_roster_model(saved_run)
    members = {code: set() for code in model.exams}
    for group in model.groups:
        members[group.exam].update(group.members)
    qa = _build_qa(members, saved_payload(saved_run)["schedule"], max_per_day=2)
    expected = {(row["student_id"], row["slot_index"]) for row in qa["same_slot_conflicts"]}
    assert expected == {
        (sid, s) for sid in model.flags.days for s in _clash_slots(model.flags, sid)
    }


def test_split_section_seats_by_ascending_id_and_unrecorded_cohort_is_unroomed(saved_run):
    model = build_roster_model(saved_run)
    m1 = next(g for g in model.groups if (g.exam, g.section, g.gender) == ("MATH101", "M1", "M"))
    parts = m1.parts
    assert [p.room_group_index for p in parts] == [1, 2]
    first = parts[0].student_count
    ordered = sorted(m1.members)
    assert all(m1.seats[sid] is parts[0] for sid in ordered[:first])
    assert all(m1.seats[sid] is parts[1] for sid in ordered[first:])
    assert {model.basis(m1, sid) for sid in ordered} == {BASIS_SPLIT}
    unrecorded = next(g for g in model.groups if g.gender == "U")
    assert unrecorded.members == (NO_COHORT,)
    assert model.basis(unrecorded, NO_COHORT) == BASIS_UNASSIGNED
    whole = next(g for g in model.groups if (g.exam, g.section) == ("CS101", "M2"))
    assert {model.basis(whole, sid) for sid in whole.members} == {BASIS_WHOLE}


def test_same_size_swap_is_changed_and_new_section_has_no_seat(saved_run):
    swapped_out = MALE_IS[-1]
    StudentTermSection.objects.filter(
        student_id=swapped_out, term_section__course_key="IS201"
    ).delete()
    scraped_exam_registration(MALE_AI[0], "IS201", section_label="M3")
    scraped_exam_registration(MALE_AI[1], "CS101", section_label="M9")
    model = build_roster_model(saved_run)
    verdicts = {(g.exam, g.section, g.gender): g for g in model.groups}
    m3 = verdicts[("IS201", "M3", "M")]
    assert m3.membership == CHANGED and len(m3.members) == m3.saved_count
    new = verdicts[("CS101", "M9", "M")]
    assert new.membership == NEW and new.saved_count == 0 and new.parts == ()
    assert model.basis(new, MALE_AI[1]) == BASIS_NO_SEAT
    assert verdicts[("MATH101", "M1", "M")].membership == MATCHES
    assert model.lists_code_saved != model.lists_code_now and not model.lists_match


def test_gone_section_and_vanished_exam_are_reported_not_refused(saved_run):
    StudentTermSection.objects.filter(term_section__section="F2").delete()
    StudentTermSection.objects.filter(term_section__section="M5").delete()
    model = build_roster_model(saved_run)
    verdicts = {(g.exam, g.section, g.gender): g for g in model.groups}
    assert verdicts[("CS101", "F2", "F")].membership == GONE
    assert verdicts[("CS101", "F2", "F")].members == ()
    assert model.missing_exams == ("PHYS103 (2)",)
    assert not model.exams["PHYS103 (2)"].in_lists
    assert verdicts[("PHYS103 (2)", "M5", "M")].membership == GONE


def test_programme_change_inside_one_identity_changes_mix_not_membership(saved_run):
    from core.models import Student

    Student.objects.filter(student_id=MALE_AI[0]).update(program="CS")
    model = build_roster_model(saved_run)
    m1 = next(g for g in model.groups if (g.exam, g.section) == ("MATH101", "M1"))
    assert m1.membership == MATCHES
    assert m1.program_mix == CHANGED
    assert m1.live_program_counts["CS"] == m1.saved_program_counts["CS"] + 1
    # The students match, so the lists codes do; what a reader is told
    # "matches" must still not: the saved department counts no longer hold.
    assert model.lists_match and model.lists_code_saved == model.lists_code_now
    assert not model.unchanged
    assert [(g.exam, g.section) for g in model.differing_groups] == [("MATH101", "M1")]
    assert model.changed_groups == []


def test_changed_split_section_moves_its_boundary_and_leftovers_have_no_seat(saved_run):
    for sid in (4401900, 4401901):
        from core.models import Student

        Student.objects.create(student_id=sid, name="LATE", program="AI", section="M")
        scraped_exam_registration(sid, "MATH101", section_label="M1")
    model = build_roster_model(saved_run)
    m1 = next(g for g in model.groups if (g.exam, g.section) == ("MATH101", "M1"))
    assert m1.membership == CHANGED and len(m1.members) == m1.saved_count + 2
    assert [model.basis(m1, sid) for sid in m1.members[-2:]] == [BASIS_NO_SEAT] * 2
    seated = [sid for sid in m1.members if m1.seats[sid] is not None]
    assert len(seated) == m1.saved_count


def test_overflow_exam_rows_are_not_scheduled(saved_run):
    data = saved_payload(saved_run)
    entry = next(e for e in data["schedule"] if e["course_code"] == "CS101")
    entry.update(day="OVERFLOW", period="Extra-9", slot_index=9, rooms=[])
    save_payload(saved_run, data)
    model = build_roster_model(saved_run)
    cs101 = [g for g in model.groups if g.exam == "CS101"]
    assert {model.basis(g, sid) for g in cs101 for sid in g.members} == {BASIS_NOT_SCHEDULED}
    assert all((sid, "CS101") not in model.flags.sittings for g in cs101 for sid in g.members)
    assert model.exams["CS101"].day_no is None


# ── Full saved scope, never a filtered rebuild ─────────────────


def test_rebuild_uses_the_saved_scope_so_later_out_of_scope_students_do_not_count():
    build_population()
    run = build_saved_run(programs=["CS", "IS"])
    assert saved_payload(run)["enrollment_scope"]["programs"] == ["CS", "IS"]
    model = build_roster_model(run)
    assert {g.membership for g in model.groups} == {MATCHES}
    assert not any(sid in FEMALE_CS2 or sid in MALE_AI for g in model.groups for sid in g.members)
    assert {code for code in model.exams} >= {"PHYS103 (1)", "PHYS103 (2)"}


# ── Term guard (section_enrollment only) and gates ─────────────


def test_term_comes_from_section_enrollment_never_from_schedule_entries(saved_run):
    data = saved_payload(saved_run)
    for entry in data["schedule"]:
        entry["term"] = 5
    save_payload(saved_run, data)
    assert build_roster_model(saved_run).saved.term == "1"
    for rows in data["section_enrollment"].values():
        for row in rows:
            row["term"] = "2"
    save_payload(saved_run, data)
    with pytest.raises(ListsTermMismatch) as refused:
        build_roster_model(saved_run)
    assert refused.value.code == "lists_term_mismatch"
    assert refused.value.live == ("1448", "1") and refused.value.saved == ("1448", "2")


def test_a_newer_live_term_refuses_instead_of_joining_another_terms_lists(saved_run):
    section = TermSection.objects.get(course_key="MATH101", section="M1")
    StudentTermSection.objects.create(
        student_id=MALE_CS[0],
        academic_year="1448",
        term="2",
        term_section=section,
        source="scraper_timetable",
    )
    with pytest.raises(ListsTermMismatch):
        build_roster_model(saved_run)


def test_no_live_lists_is_its_own_refusal(saved_run):
    StudentTermSection.objects.all().delete()
    with pytest.raises(ListsUnavailable) as refused:
        build_roster_model(saved_run)
    assert refused.value.code == "lists_unavailable"


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.update(status="unrenderable"),
        lambda d: d.update(enrollment_source="studying"),
        lambda d: d.pop("section_enrollment"),
        lambda d: d.pop("operations_snapshot"),
        lambda d: next(iter(d["section_enrollment"].values()))[0].pop("membership_fingerprint"),
        lambda d: next(iter(d["section_enrollment"].values()))[0].update(
            membership_fingerprint="not-a-hash"
        ),
        lambda d: d["schedule"][0]["rooms"][0]["section_parts"][0].pop("room_group_index"),
        lambda d: d["schedule"][0].update(course_name="A DIFFERENT NAME"),
    ],
    ids=[
        "status",
        "source",
        "no_sections",
        "no_snapshot",
        "no_fingerprint",
        "bad_fingerprint",
        "unnumbered_room_part",
        "identity_mismatch",
    ],
)
def test_a_run_lacking_what_the_roster_needs_gets_one_plain_refusal(saved_run, damage):
    data = saved_payload(saved_run)
    damage(data)
    save_payload(saved_run, data)
    with pytest.raises(RebuildRequired) as refused:
        build_roster_model(saved_run)
    assert refused.value.code == "rebuild_required"
    assert str(refused.value) == "Rebuild and save this timetable to export student data."


# ── One student query, allowed fields only ─────────────────────


def test_student_fields_are_read_once_and_never_a_forbidden_column(saved_run):
    with CaptureQueriesContext(connection) as captured:
        build_roster_model(saved_run)
    sql = [query["sql"] for query in captured.captured_queries]
    named = [q for q in sql if '"students"."name"' in q]
    assert len(named) == 1
    assert '"students"."program"' in named[0] and '"students"."section"' in named[0]
    for forbidden in (
        "gpa",
        "registration_no",
        "nationality",
        '"students"."status"',
        "total_registered_credits",
        "total_earned_credits",
        "current_registered_credits",
        "advisor_id",
    ):
        assert not any(forbidden in q for q in sql), forbidden


def test_members_out_exposes_exactly_the_fingerprinted_sets(saved_run):
    from core.services.exam_sections import _membership_fingerprint

    members: dict = {}
    groups = resolve_exam_section_enrollment(
        {"MATH101": set(MALE_CS + MALE_IS)},
        course_meta={"MATH101": {"source_course_code": "MATH101"}},
        members_out=members,
    )
    for row in groups["MATH101"]:
        ids = members["MATH101"][(row["section_key"], row["gender"])]
        assert _membership_fingerprint(set(ids)) == row["membership_fingerprint"]
        assert len(ids) == row["student_count"]
    assert "student_ids" not in str(groups)
