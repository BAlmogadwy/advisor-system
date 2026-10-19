"""One teacher per section: every consumer reads the one map (I1-I3, I5, I7, I13, I18, I22).

The section -> instructor builders' BODIES change, never their call sites, so
the placer, the searches, the repairs, the conflict badges, the manual-move
checks, the backstop and the graph twin all charge each section to its own
teacher. A section still to be decided charges nobody — and a stale meeting
name on it must not bring the old union back.
"""

from __future__ import annotations

import pytest

from core.models import (
    DeliveryBoard,
    SectionInstructor,
    SectionPlacement,
    TermSectionMeeting,
)
from core.services import section_teacher_assignment as sta
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    return settings


def _two_hours(start: str) -> str:
    return f"{int(start[:2]) + 1:02d}:{start[3:]}"


# ── I1 / I2 / I22: the builders ───────────────────────────────────────────


def _algorithms_board():
    """CS114 (5 sections, S1-S3 placed) for Dr A or Dr B; MATH203 with no link;
    a global registrar section of AI201 placed here, taught by Dr Reg; and a
    global section whose lecturer is a name no Instructor has."""
    alg = requirement("AI", "CS114", "ALGORITHMS")
    math = requirement("AI", "MATH203", "CALCULUS")
    sc, board = scenario()
    a, b, reg = instructor("Dr A"), instructor("Dr B"), instructor("Dr Reg")
    link("AI", "CS114", a)
    link("AI", "CS114", b, role="co")
    budget(sc, alg, 5)
    budget(sc, math, 1)
    for n, start in ((1, "08:00"), (2, "09:30"), (3, "11:00")):
        place(board, section(sc, alg, f"S{n}"), [("SUN", start, _two_hours(start))])
    global_known = section(None, "AI201", "G1")
    place(board, global_known, [("MON", "08:00", "09:00")])
    SectionInstructor.objects.create(
        scenario=None, course_key="AI201", section="G1", instructor=reg, source="registrar"
    )
    global_unknown = section(None, "AI202", "G2")
    place(board, global_unknown, [("MON", "08:00", "09:00")], instructor_name="Visiting Lecturer")
    return sc, board, alg, math, {"A": a, "B": b, "Reg": reg}


@pytest.mark.django_db
def test_i1_both_builders_charge_one_teacher_and_tbd_charges_nobody(flags) -> None:
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario
    from core.services.timetable_pr4_instructor import build_section_instructor_ids

    sc, _board, alg, math, people = _algorithms_board()

    pr4 = build_section_instructor_ids(sc)
    v2 = build_section_instructor_map_for_scenario(sc.id)

    assert all(len(ids) == 1 for ids in v2.values())
    taught = [pr4[f"{alg}|S{n}"] for n in (1, 2, 3, 4)]
    assert all(len(ids) == 1 for ids in taught)  # S4 is charged although unplaced
    assert sorted(len([t for t in taught if t == {p.id}]) for p in (people["A"], people["B"])) == [
        2,
        2,
    ]
    assert pr4[f"{alg}|S5"] == set()  # to be decided: present, empty
    assert f"{alg}_S5" not in v2
    assert f"{math}|S1" not in pr4 and f"{math}_S1" not in v2  # no links: absent
    assert pr4["AI201|G1"] == {people["Reg"].id}  # frozen, owner's registrar row
    assert v2["AI201_G1"] == frozenset({people["Reg"].id})
    assert "AI202|G2" not in pr4 and "AI202_G2" not in v2  # unknown frozen: name path


@pytest.mark.django_db
def test_i2_the_solver_reads_the_stored_teacher_never_the_union(flags) -> None:
    from core.services.timetable_solver import _instructor_ids_for_sections

    sc, board, alg, _math, people = _algorithms_board()
    sta.persist_assignment(sc.id, sta.load_assignment(sc.id))
    stored = dict(
        SectionInstructor.objects.filter(scenario=sc, source="engine").values_list(
            "section", "instructor_id"
        )
    )
    probe = [
        {"code": alg, "display_code": "CS114", "label": "S4"},  # unplaced, stored
        {"code": alg, "display_code": "CS114", "label": "S5"},  # to be decided
        {"code": alg, "display_code": "CS114", "label": "S9"},  # beyond the budget
    ]

    ids = _instructor_ids_for_sections(board, probe)

    assert ids == [{stored["S4"]}, set(), set()]
    assert {people["A"].id, people["B"].id} not in ids


@pytest.mark.django_db
def test_i22_a_scenario_without_programmes_is_staffed_through_its_boards(flags) -> None:
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario

    key = requirement("AI", "AI201", "ML")
    sc, board = scenario(programs=(), board_program="AI")
    person = instructor("Dr Board")
    link("AI", "AI201", person)
    budget(sc, key, 1)
    place(board, section(sc, key, "S1"), [("SUN", "08:00", "09:15")])

    assert build_section_instructor_map_for_scenario(sc.id) == {f"{key}_S1": frozenset({person.id})}


# ── I3: one fixture through every consumer ────────────────────────────────


def _union_would_clash_board():
    """X (Dr A or Dr B) and Y (Dr A only) overlap on Sunday. The union charges
    Dr A with X too, so it reports a clash; the one-teacher map gives X to Dr B.
    X's meeting rows still carry Dr A's name from the union era."""
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    sc, board = scenario()
    sc.slot_config = [{"start": "08:00", "end": "09:15"}, {"start": "09:30", "end": "10:45"}]
    sc.save()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    link("AI", "AI302", a)
    budget(sc, x, 1)
    budget(sc, y, 1)
    xs = section(sc, x, "S1")
    ys = section(sc, y, "S1")
    place(board, xs, [("SUN", "08:00", "09:15")], instructor_name="Dr A", room="R1")
    place(board, ys, [("SUN", "08:30", "09:45")], instructor_name="Dr A", room="R2")
    return sc, board, x, y, xs, ys, a, b


@pytest.mark.django_db
def test_i3_every_consumer_reads_the_one_teacher_map(flags) -> None:
    from core.services.timetable_graph_twin import build_scenario_graph_payload
    from core.services.timetable_instructor_backstop import scenario_instructor_clash_report
    from core.services.timetable_optimizer_v2 import build_section_instructor_map_for_scenario
    from core.services.timetable_pr4_instructor import build_section_instructor_ids
    from core.services.timetable_repair_eligibility import _section_instructor_policy
    from core.services.timetable_scenario_quality import build_scenario_quality
    from core.services.timetable_solver import _instructor_ids_for_sections
    from core.services.timetable_workspace import (
        compute_scenario_safety_summary,
        detect_board_conflicts,
        preview_placement_slot_candidates,
        preview_planned_section_slot_candidates,
        validate_placement,
    )

    sc, board, x, y, xs, ys, a, b = _union_would_clash_board()
    z = requirement("AI", "AI303", "Z COURSE")  # planned, not placed yet: Dr A's
    link("AI", "AI303", a)
    budget(sc, z, 1)
    one = sta.load_assignment(sc.id)
    assert one.teacher == {f"{x}_S1": b.id, f"{y}_S1": a.id, f"{z}_S1": a.id}

    assert build_section_instructor_ids(sc) == {
        f"{x}|S1": {b.id},
        f"{y}|S1": {a.id},
        f"{z}|S1": {a.id},
    }
    assert build_section_instructor_map_for_scenario(sc.id) == {
        f"{x}_S1": frozenset({b.id}),
        f"{y}_S1": frozenset({a.id}),
        f"{z}_S1": frozenset({a.id}),
    }
    assert _instructor_ids_for_sections(
        board, [{"code": x, "display_code": "AI301", "label": "S1"}]
    ) == [{b.id}]
    assert detect_board_conflicts(board.id)["instructor_clashes"] == []
    summary = compute_scenario_safety_summary(sc.id)
    assert summary["instructor_clashes_scenario"] == 0
    assert summary["same_board_conflicts"]["instructors"] == 0
    assert scenario_instructor_clash_report(sc.id)["count"] == 0
    names, conflicts = _section_instructor_policy(scenario_id=sc.id, target_section_ids={xs.id})
    assert names[xs.id] == {"Dr B"} and conflicts == {}
    y_placement = SectionPlacement.objects.get(term_section=ys)
    check = validate_placement(
        board.id, "SUN", "08:00", "09:15", "R9", ys.id, exclude_placement_id=y_placement.id
    )
    assert check["instructor_clashes"] == []

    def blamed_on_x(preview) -> list:
        return [
            e
            for c in preview["candidates"]
            for e in c.get("evidence", [])
            if e["kind"] == "instructor" and "AI301" in e["detail"]
        ]

    moves = preview_placement_slot_candidates(y_placement.id)
    at_x = next(c for c in moves["candidates"] if (c["day"], c["start"]) == ("SUN", "08:00"))
    assert at_x["critical_count"] == 0
    assert blamed_on_x(moves) == []
    planned = preview_planned_section_slot_candidates(
        board.id, course_code="AI303", course_key=z, section_label="S1"
    )
    assert blamed_on_x(planned) == []
    # Z has no meeting rows yet, so only the map knows it is Dr A's: it clashes
    # with Dr A's Y at Sunday 08:00.
    at_y = next(c for c in planned["candidates"] if (c["day"], c["start"]) == ("SUN", "08:00"))
    assert any(e["kind"] == "instructor" and "AI302" in e["detail"] for e in at_y["evidence"])
    graph = build_scenario_graph_payload(sc.id)
    x_node = next(
        n
        for n in graph["nodes"]
        if n["label"] == "TTSection" and n["props"]["term_section_id"] == xs.id
    )
    assert x_node["props"]["instructors"] == "Dr B"
    quality = build_scenario_quality(sc.id)
    assert quality["instructor"]["coverage"]["assigned"] == 3


@pytest.mark.django_db
def test_i3_the_same_fixture_clashes_under_the_union(flags) -> None:
    """Control: the fixture really does separate the two rules."""
    from core.services.timetable_workspace import (
        detect_board_conflicts,
        preview_placement_slot_candidates,
    )

    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    _sc, board, _x, _y, _xs, ys, _a, _b = _union_would_clash_board()

    assert len(detect_board_conflicts(board.id)["instructor_clashes"]) == 1
    moves = preview_placement_slot_candidates(SectionPlacement.objects.get(term_section=ys).id)
    at_x = next(c for c in moves["candidates"] if (c["day"], c["start"]) == ("SUN", "08:00"))
    assert at_x["critical_count"] == 1


# ── I5: frozen sections ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_i5_a_frozen_teacher_comes_from_the_owner_row_then_the_meeting_name(flags) -> None:
    key = requirement("AI", "AI201", "ML")
    sc, board = scenario()
    other, _other_board = scenario(name="owner")
    named, owner_rowed = instructor("Dr Named"), instructor("Dr Owner")
    link("AI", "AI201", named)
    budget(sc, key, 1)
    by_row = section(other, key, "S7")
    by_name = section(other, key, "S8")
    place(board, by_row, [("SUN", "08:00", "09:00")], instructor_name="Dr Named")
    place(board, by_name, [("MON", "08:00", "09:00")], instructor_name="Dr Named")
    SectionInstructor.objects.create(
        scenario=other, course_key=key, section="S7", instructor=owner_rowed, source="engine"
    )

    a = sta.load_assignment(sc.id)

    assert a.status[f"{key}_S7"] == sta.STATUS_FROZEN
    assert a.teacher[f"{key}_S7"] == owner_rowed.id  # by FK, whatever the name says
    assert a.teacher[f"{key}_S8"] == named.id
    sta.persist_assignment(sc.id, a)
    assert not SectionInstructor.objects.filter(scenario=sc, section__in=["S7", "S8"]).exists()


@pytest.mark.django_db
def test_i5_an_unknown_frozen_teacher_keeps_the_free_text_clash(flags) -> None:
    from core.services.timetable_workspace import detect_board_conflicts

    sc, board = scenario()
    for label, start in (("G1", "08:00"), ("G2", "08:30")):
        ts = section(None, "AI202", label)
        place(board, ts, [("MON", start, _two_hours(start))], instructor_name="Visiting Lecturer")

    a = sta.load_assignment(sc.id)

    assert {a.status["AI202_G1"], a.status["AI202_G2"]} == {sta.STATUS_FROZEN_UNKNOWN}
    assert sta.coverage_of(a)["frozen_unknown"][0]["name"] == "Visiting Lecturer"
    assert len(detect_board_conflicts(board.id)["instructor_clashes"]) == 1


def _held_by_the_registrar():
    """AI201 S1 is not placed, and its one time is Sunday + Tuesday 08:00; the
    registrar's own section of another course, placed on this board, holds Dr
    Reg (AI201's one teacher) every Sunday 08:00."""
    key = requirement("AI", "AI201", "ML")
    sc, board = _sunday_tuesday_scenario()
    sc.slot_config = [{"start": "08:00", "end": "09:15"}]  # one time a day
    sc.save()
    reg = instructor("Dr Reg")
    link("AI", "AI201", reg)
    budget(sc, key, 1)
    held = section(None, "AI999", "R1")
    place(board, held, [("SUN", "08:00", "09:15")], room="R0")
    SectionInstructor.objects.create(
        scenario=None, course_key="AI999", section="R1", instructor=reg, source="registrar"
    )
    return sc, board, key, reg


@pytest.mark.django_db
def test_i5_the_placer_treats_a_global_sections_teacher_as_busy(flags) -> None:
    from core.services.timetable_autoplace import auto_place_board

    sc, board, key, reg = _held_by_the_registrar()
    # The department has fixed Dr Reg on AI201 S1, so the section is his.
    SectionInstructor.objects.create(
        scenario=sc, course_key=key, section="S1", instructor=reg, source="department"
    )

    result = auto_place_board(board.id)

    assert result["placed"] == 0
    assert result["room_failures"][0]["reason"] == "INSTRUCTOR_CLASH"
    assert not TermSectionMeeting.objects.filter(
        term_section__course_key=key, day="SUN", start_time="08:00"
    ).exists()


@pytest.mark.django_db
def test_i5_a_section_its_one_teacher_cannot_fit_is_placed_to_be_decided(flags) -> None:
    """Unpinned, AI201 S1 is to be decided (Dr Reg has no time left for it): the
    retry places it for the students, charges nobody — Dr Reg is not booked on
    top of the registrar's section — and the coverage reports why."""
    from core.services.timetable_autoplace import auto_place_board

    sc, board, key, _reg = _held_by_the_registrar()
    before = sta.load_assignment(sc.id)
    assert before.status[f"{key}_S1"] == sta.STATUS_TBD
    assert before.tbd_reason[f"{key}_S1"] == "clash"

    assert auto_place_board(board.id)["placed"] == 1

    after = sta.load_assignment(sc.id)
    assert after.status[f"{key}_S1"] == sta.STATUS_TBD
    assert sta.violations(after) == {"clash_pairs": 0, "over_cap_sessions": 0}
    assert sta.coverage_of(after)["tbd_reasons"]["clash"] == 1


# ── I7: the required-section retry ────────────────────────────────────────


SLOTS = (
    ("08:00", "09:15"),
    ("09:30", "10:45"),
    ("11:00", "12:15"),
    ("12:30", "13:45"),
    ("14:00", "15:15"),
)


def _sunday_tuesday_scenario():
    """Five lecture slots, with Monday/Wednesday/Thursday blocked, so every
    two-meeting option is Sunday + Tuesday at one of five times."""
    sc, board = scenario()
    sc.slot_config = [{"start": s, "end": e} for s, e in SLOTS]
    sc.lab_slot_config = []
    sc.blocked_slots = [{"day": d, "start": s} for d in ("MON", "WED", "THU") for s, _e in SLOTS]
    sc.save()
    from core.models import Room

    for code in ("R1", "R2", "R3"):
        Room.objects.create(room_code=code, capacity=60, room_type="lecture", department="AI")
    return sc, board


def _retry_board():
    """CS114 needs five sections (D10: two each for Dr A and Dr B, one to be
    decided); S1-S3 hold 08:00-12:15 and S4/S5 exist but are not placed. Dr A
    teaches AI501 at 12:30 and Dr B AI502 at 14:00 — the only other times — so
    the union (both on every CS114 section) leaves S4 and S5 nowhere to go."""
    alg = requirement("AI", "CS114", "ALGORITHMS")
    sc, board = _sunday_tuesday_scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "CS114", a)
    link("AI", "CS114", b, role="co")
    budget(sc, alg, 5)
    for n, (start, end) in enumerate(SLOTS[:3], start=1):
        place(board, section(sc, alg, f"S{n}"), [("SUN", start, end), ("TUE", start, end)])
    section(sc, alg, "S4")
    section(sc, alg, "S5")
    for code, person, (start, end) in (("AI501", a, SLOTS[3]), ("AI502", b, SLOTS[4])):
        key = requirement("AI", code, f"BUSY {code}")
        link("AI", code, person)
        budget(sc, key, 1)
        place(board, section(sc, key, "S1"), [("SUN", start, end), ("TUE", start, end)], room="R3")
    return sc, board, alg, {"A": a, "B": b}


@pytest.mark.django_db
def test_i7_one_teacher_charging_lets_the_retry_place_what_the_union_cannot(flags) -> None:
    from core.services.timetable_required_sections import reinstate_missing_required_sections

    sc, _board, alg, people = _retry_board()
    sta.prepare_section_teachers(sc.id, phase="layout")
    before = sta.load_assignment(sc.id)
    s4_teacher = before.teacher[f"{alg}_S4"]
    assert s4_teacher in {people["A"].id, people["B"].id}
    assert before.status[f"{alg}_S5"] == sta.STATUS_TBD

    result = reinstate_missing_required_sections(sc.id)

    placed = set(
        SectionPlacement.objects.filter(term_section__course_key=alg).values_list(
            "term_section__section", flat=True
        )
    )
    assert placed == {"S1", "S2", "S3", "S4", "S5"}
    assert not [r for r in result["after_missing"] if r["course_key"] == alg]
    after = sta.load_assignment(sc.id)
    assert after.teacher[f"{alg}_S4"] == s4_teacher  # charged, and kept
    assert after.status[f"{alg}_S5"] == sta.STATUS_TBD
    assert sta.violations(after) == {"clash_pairs": 0, "over_cap_sessions": 0}


def _only_teacher_board():
    """U (AI601) has no section placed and only Dr A teaches it; the placer
    would put it on Sunday + Tuesday at 08:00, 09:30 or 11:00 — exactly where
    V (AI602, Dr A or Dr B) has its three sections. Dr A already works Sunday
    and Tuesday (a department pin at 14:00), Dr B only Monday. The fewest days
    give Dr A all three V sections, and then U can go nowhere."""
    sc, board = _sunday_tuesday_scenario()
    sc.slot_config = [{"start": s, "end": e} for s, e in SLOTS[:3]]
    sc.save()
    a, b = instructor("Dr A"), instructor("Dr B")
    only = requirement("AI", "AI601", "ONLY DR A")
    shared = requirement("AI", "AI602", "EITHER")
    link("AI", "AI601", a)
    link("AI", "AI602", a)
    link("AI", "AI602", b, role="co")
    budget(sc, only, 1)
    budget(sc, shared, 3)
    for n, (start, end) in enumerate(SLOTS[:3], start=1):
        place(board, section(sc, shared, f"S{n}"), [("SUN", start, end), ("TUE", start, end)])
    for code, person, meetings in (
        ("AI603", a, [("SUN", "14:00", "15:15"), ("TUE", "14:00", "15:15")]),
        ("AI604", b, [("MON", "08:00", "09:15")]),
    ):
        key = requirement("AI", code, f"PINNED {code}")
        budget(sc, key, 1)
        place(board, section(sc, key, "S1"), meetings, room="R3")
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section="S1", instructor=person, source="department"
        )
    return sc, only, shared, {"A": a, "B": b}


@pytest.mark.django_db
def test_i7_a_missing_sections_only_teacher_keeps_a_time_free_for_the_retry(flags) -> None:
    """Spec 8.3 on lab board P_G222: the days-first map gave instructor 298 all
    three AI124 sections, and DS331 S1 — only 298 teaches it — had none of its
    190 options left, so the retry could not place it."""
    from core.services.timetable_required_sections import reinstate_missing_required_sections

    flags.TIMETABLE_INSTRUCTOR_DAILY_CAP = 4  # Dr A's pin and three V sections fit a day
    sc, only, shared, people = _only_teacher_board()
    sta.prepare_section_teachers(sc.id, phase="layout")
    before = sta.load_assignment(sc.id)
    assert before.teacher[f"{only}_S1"] == people["A"].id
    assert sum(before.teacher[f"{shared}_S{n}"] == people["A"].id for n in (1, 2, 3)) < 3
    assert sta.coverage_of(before)["unplaced_teacher_busy"] == []

    result = reinstate_missing_required_sections(sc.id)

    assert not [r for r in result["after_missing"] if r["course_key"] == only]
    after = sta.load_assignment(sc.id)
    assert after.teacher[f"{only}_S1"] == people["A"].id
    assert sta.violations(after) == {"clash_pairs": 0, "over_cap_sessions": 0}


@pytest.mark.django_db
def test_i7_control_the_union_cannot_place_s4(flags) -> None:
    from core.services.timetable_required_sections import reinstate_missing_required_sections

    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    sc, _board, alg, _people = _retry_board()

    reinstate_missing_required_sections(sc.id)

    assert not SectionPlacement.objects.filter(
        term_section__course_key=alg, term_section__section__in=["S4", "S5"]
    ).exists()


# ── I13: the backstop counts sibling double-bookings ──────────────────────


def _siblings_pinned(first: str, second: str):
    key = requirement("AI", "AI301", "X COURSE")
    sc, board = scenario()
    people = {"A": instructor("Dr A"), "B": instructor("Dr B")}
    link("AI", "AI301", people["A"])
    link("AI", "AI301", people["B"], role="co")
    budget(sc, key, 2)
    for label, teacher, start in (("S1", first, "08:00"), ("S2", second, "08:30")):
        place(board, section(sc, key, label), [("SUN", start, _two_hours(start))])
        SectionInstructor.objects.create(
            scenario=sc,
            course_key=key,
            section=label,
            instructor=people[teacher],
            source="department",
        )
    return sc


@pytest.mark.django_db
def test_i13_overlapping_siblings_with_one_teacher_are_a_double_booking(flags) -> None:
    from core.services.timetable_instructor_backstop import scenario_instructor_clash_report

    assert scenario_instructor_clash_report(_siblings_pinned("A", "A").id)["count"] == 1


@pytest.mark.django_db
def test_i13_overlapping_siblings_with_different_teachers_are_not(flags) -> None:
    from core.services.timetable_instructor_backstop import scenario_instructor_clash_report

    report = scenario_instructor_clash_report(_siblings_pinned("A", "B").id)
    assert report["checked"] and report["count"] == 0


# ── I18: a stale name on a TBD section charges nobody ─────────────────────


@pytest.mark.django_db
def test_i18_a_tbd_section_with_a_stale_meeting_name_is_not_charged(flags) -> None:
    from core.services.course_instructor_assignment import reconcile_scenario_instructors
    from core.services.timetable_workspace import (
        compute_scenario_safety_summary,
        detect_board_conflicts,
    )

    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    sc, board = scenario()
    a = instructor("Dr A")
    link("AI", "AI301", a)  # Y has no link: its registrar text names Dr A
    budget(sc, x, 4)  # D10: Dr A holds two of the four
    budget(sc, y, 1)
    for n, (day, start) in enumerate((("SUN", "08:00"), ("MON", "08:00"), ("TUE", "08:00")), 1):
        place(board, section(sc, x, f"S{n}"), [(day, start, "09:15")], instructor_name="Dr A")
    place(board, section(sc, y, "S1"), [("TUE", "08:30", "09:45")], instructor_name="Dr A")

    one = sta.load_assignment(sc.id)
    assert one.status[f"{x}_S3"] == sta.STATUS_TBD  # still named Dr A from the union era
    assert one.status[f"{y}_S1"] == sta.STATUS_NO_LINKS

    before = compute_scenario_safety_summary(sc.id)
    assert detect_board_conflicts(board.id)["instructor_clashes"] == []
    reconcile_scenario_instructors(sc)
    after = compute_scenario_safety_summary(sc.id)

    assert TermSectionMeeting.objects.get(term_section__section="S3").instructor == ""
    assert TermSectionMeeting.objects.get(term_section__course_key=y).instructor == "Dr A"
    assert before == after
    assert DeliveryBoard.objects.filter(scenario=sc).count() == 1


def _tbd_with_stale_names_board():
    """X (Dr A only, five sections: D10 lets Dr A hold two) has S1 on Sunday,
    S2 on Monday, and S3 and S4 overlapping on Tuesday 08:00-09:45; S5 is not
    placed. Dr A takes S1 and S2, so S3, S4 and S5 are to be decided. Y (Dr
    A's) sits on Tuesday 08:00 too. Every meeting row still says "Dr A", as it
    did under the union — the state after a link edit, a deactivation, or
    switching the rule on before anyone presses Reconcile."""
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    sc, board = scenario()
    sc.slot_config = [{"start": "08:00", "end": "09:15"}, {"start": "09:30", "end": "10:45"}]
    sc.save()
    a = instructor("Dr A")
    link("AI", "AI301", a)
    link("AI", "AI302", a)
    budget(sc, x, 5)
    budget(sc, y, 1)
    sections = {}
    for label, day, start, end, room in (
        ("S1", "SUN", "08:00", "09:15", "R1"),
        ("S2", "MON", "08:00", "09:15", "R1"),
        ("S3", "TUE", "08:00", "09:15", "R1"),
        ("S4", "TUE", "08:30", "09:45", "R2"),
    ):
        sections[label] = section(sc, x, label)
        place(board, sections[label], [(day, start, end)], instructor_name="Dr A", room=room)
    sections["Y"] = section(sc, y, "S1")
    place(board, sections["Y"], [("TUE", "08:00", "09:15")], instructor_name="Dr A", room="R3")
    one = sta.load_assignment(sc.id)
    assert [one.status[f"{x}_S{n}"] for n in range(1, 6)] == ["engine"] * 2 + ["tbd"] * 3
    assert one.teacher[f"{y}_S1"] == a.id
    return sc, board, x, y, sections


@pytest.mark.django_db
def test_i18_the_repair_report_neither_names_nor_charges_a_tbd_section(flags) -> None:
    from core.services.timetable_repair_eligibility import _section_instructor_policy

    sc, _board, _x, _y, sections = _tbd_with_stale_names_board()
    s3, s4, ys = sections["S3"], sections["S4"], sections["Y"]

    names, conflicts = _section_instructor_policy(
        scenario_id=sc.id, target_section_ids={s3.id, s4.id, ys.id}
    )

    # Two overlapping sections to be decided are not Dr A's double-booking, and
    # neither is a section to be decided next to Dr A's own Y.
    assert conflicts == {}
    assert s3.id not in names and s4.id not in names
    assert names[ys.id] == {"Dr A"}


@pytest.mark.django_db
def test_i18_manual_checks_never_clash_on_a_tbd_section(flags) -> None:
    from core.services.timetable_workspace import (
        preview_placement_slot_candidates,
        preview_planned_section_slot_candidates,
        validate_placement,
    )

    sc, board, x, _y, sections = _tbd_with_stale_names_board()
    s4 = SectionPlacement.objects.get(term_section=sections["S4"])
    y_placement = SectionPlacement.objects.get(term_section=sections["Y"])

    # S4 (to be decided) moved onto S3 (to be decided) and Y (Dr A's).
    onto_s3 = validate_placement(
        board.id, "TUE", "08:00", "09:15", "R9", sections["S4"].id, exclude_placement_id=s4.id
    )
    assert onto_s3["instructor_clashes"] == []
    # Y (Dr A's) moved onto S3 and S4, whose rows still say Dr A.
    y_onto = validate_placement(
        board.id,
        "TUE",
        "08:00",
        "09:15",
        "R9",
        sections["Y"].id,
        exclude_placement_id=y_placement.id,
    )
    assert y_onto["instructor_clashes"] == []

    def instructor_evidence(preview, blamed=("",)) -> list:
        return [
            e
            for c in preview["candidates"]
            for e in c.get("evidence", [])
            if e["kind"] == "instructor" and any(b in e["detail"] for b in blamed)
        ]

    moves = preview_placement_slot_candidates(s4.id)
    assert any((c["day"], c["start"]) == ("TUE", "08:00") for c in moves["candidates"])
    assert instructor_evidence(moves) == []
    # Y may clash with Dr A's S1 and S2 elsewhere, never with S3 or S4.
    y_moves = preview_placement_slot_candidates(y_placement.id)
    assert instructor_evidence(y_moves, blamed=("AI301-S1",))  # the control
    assert instructor_evidence(y_moves, blamed=("AI301-S3", "AI301-S4")) == []
    # S5, to be decided and not placed yet, may go anywhere teacher-wise.
    planned = preview_planned_section_slot_candidates(
        board.id, course_code="AI301", course_key=x, section_label="S5"
    )
    assert any((c["day"], c["start"]) == ("TUE", "08:00") for c in planned["candidates"])
    assert instructor_evidence(planned) == []


@pytest.mark.django_db
def test_i18_the_graph_twin_names_nobody_on_a_tbd_section(flags) -> None:
    from core.services.timetable_graph_twin import build_scenario_graph_payload

    sc, _board, _x, _y, sections = _tbd_with_stale_names_board()

    graph = build_scenario_graph_payload(sc.id)

    shown = {
        n["props"]["term_section_id"]: n["props"]["instructors"]
        for n in graph["nodes"]
        if n["label"] == "TTSection"
    }
    assert shown[sections["S1"].id] == "Dr A"
    assert shown[sections["S3"].id] == "" and shown[sections["S4"].id] == ""


@pytest.mark.django_db
def test_i3_the_planned_preview_reads_teachers_across_boards(flags) -> None:
    from core.services.timetable_workspace import preview_planned_section_slot_candidates

    sc, board, x, _y, _xs, _ys, a, _b = _union_would_clash_board()
    SectionPlacement.objects.filter(term_section__course_key=x).update(
        board=DeliveryBoard.objects.create(
            scenario=sc, label="Term 4", nominal_term=4, program="AI"
        )
    )
    z = requirement("AI", "AI303", "Z COURSE")
    link("AI", "AI303", a)
    budget(sc, z, 1)

    planned = preview_planned_section_slot_candidates(
        board.id, course_code="AI303", course_key=z, section_label="S1"
    )

    blamed = [
        e
        for c in planned["candidates"]
        for e in c.get("evidence", [])
        if e["kind"] == "instructor" and "AI301" in e["detail"]
    ]
    assert blamed == []  # X, on the other board, is Dr B's although its rows say Dr A
