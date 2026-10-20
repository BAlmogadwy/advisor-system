"""One teacher per section: who owns a teacher (I4, I6, I10, I11, I16, I20).

The engine writes only ``source="engine"`` rows. Every other primary row in
the scenario is a pin it never touches; a section the scenario does not own
keeps its owner's teacher; a time lock does not fix the teacher; snapshot and
restore carry the engine rows exactly as they carry placements; a copy pins the
teachers it inherits; link edits act at read time, never by writing.
"""

from __future__ import annotations

import json
import uuid

import pytest

from core.models import (
    DeliveryBoard,
    PlannerJob,
    SectionInstructor,
    SectionPlacement,
    TermSectionMeeting,
)
from core.services import section_teacher_assignment as sta
from tests.one_teacher_support import (
    FLAG_ON,
    budget,
    engine_rows,
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


def _admin_client():
    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="one-teacher-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


def _rows(sc) -> list[tuple]:
    return sorted(
        SectionInstructor.objects.filter(scenario=sc).values_list(
            "pk", "course_key", "section", "instructor_id", "role", "source"
        )
    )


# ── I4: pins ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_i4_pins_of_every_kind_are_fixed_load_and_never_written(flags) -> None:
    alg = requirement("AI", "CS114", "ALGORITHMS")
    lab = requirement("AI", "AI401", "LAB COURSE", credit=4)
    sc, _board = scenario()
    a, b, c = instructor("Dr A"), instructor("Dr B"), instructor("Dr C")
    for person in (a, b, c):
        link("AI", "CS114", person, role="primary" if person is a else "co")
    link("AI", "AI401", b)
    budget(sc, alg, 5)  # D10: two each
    budget(sc, lab, 4, credit=4)
    pins = {
        (alg, "S1"): (a, "department"),
        (alg, "S2"): (a, "registrar_faculty_sections"),  # Dr A is at D10 now
        (alg, "S3"): (b, ""),
        (alg, "S4"): (c, "copied_frozen"),
        **{(lab, f"S{n}"): (b, "registrar") for n in (1, 2, 3, 4)},  # Dr B: 12 + 2 = 14
    }
    for (key, label), (person, source) in pins.items():
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section=label, instructor=person, source=source
        )
    before = _rows(sc)

    a_map = sta.load_assignment(sc.id)
    sta.persist_assignment(sc.id, sta.propose_layout_assignment(sc.id))
    after = sta.load_assignment(sc.id)

    for (key, label), (person, _source) in pins.items():
        assert a_map.status[f"{key}_{label}"] == sta.STATUS_PINNED
        assert after.teacher[f"{key}_{label}"] == person.id
    # S5: Dr A is at D10, Dr B at 14 of 15 sessions, so only Dr C fits.
    assert after.teacher[f"{alg}_S5"] == c.id
    assert sorted(set(_rows(sc)) - set(before)) == [
        r for r in _rows(sc) if r[5] == "engine"
    ]  # the only new row is the engine's
    assert [r for r in _rows(sc) if r[5] != "engine"] == before
    assert engine_rows(sc) == {(alg, "S5"): "Dr C"}
    assert sta.coverage_of(after)["d10_violated_by_pins"] == [[lab, b.id]]


# ── I6: a lock fixes the time, not the teacher ────────────────────────────


@pytest.mark.django_db
def test_i6_a_time_locked_sections_teacher_can_change(flags) -> None:
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    z = requirement("AI", "AI303", "Z COURSE")
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    link("AI", "AI302", a)
    link("AI", "AI303", b)
    for key in (x, y, z):
        budget(sc, key, 1)
    xs = section(sc, x, "S1")
    place(board, xs, [("MON", "08:00", "09:15")], locked=True)
    place(board, section(sc, y, "S1"), [("SUN", "08:00", "09:15")])  # Dr A works Sunday
    place(board, section(sc, z, "S1"), [("MON", "09:30", "10:45")])  # Dr B works Monday
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S1", instructor=a, source="engine"
    )

    result = sta.prepare_section_teachers(sc.id, phase="layout")

    # Dr B already works Monday; Dr A would add a day. The lock does not stop it.
    assert result["assignment"].teacher[f"{x}_S1"] == b.id
    assert engine_rows(sc)[(x, "S1")] == "Dr B"
    assert list(
        SectionPlacement.objects.filter(term_section=xs).values_list(
            "day", "start_time", "is_locked"
        )
    ) == [("MON", "08:00", True)]


# ── I10: snapshot / restore, and a refused rebuild ────────────────────────


def _shared_out_board():
    """X S1 belongs to this scenario but is also placed on another scenario's
    board, so its teacher is frozen here; Y and Z are ordinary choices."""
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    sc, board = scenario()
    _other, other_board = scenario(name="borrower")
    a, b = instructor("Dr A"), instructor("Dr B")
    for code in ("AI301", "AI302"):
        link("AI", code, a)
        link("AI", code, b, role="co")
    budget(sc, x, 1)
    budget(sc, y, 2)
    xs = section(sc, x, "S1")
    place(board, xs, [("SUN", "08:00", "09:15")], instructor_name="Dr A")
    SectionPlacement.objects.create(
        board=other_board, term_section=xs, day="SUN", start_time="08:00", end_time="09:15"
    )
    place(board, section(sc, y, "S1"), [("MON", "08:00", "09:15")])
    place(board, section(sc, y, "S2"), [("TUE", "08:00", "09:15")])
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S1", instructor=a, source="engine"
    )
    return sc, board, x, y, a, b


@pytest.mark.django_db
def test_i10_restore_brings_back_the_engine_rows_and_leaves_a_frozen_row_alone(flags) -> None:
    from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario

    sc, _board, x, y, a, b = _shared_out_board()
    sta.persist_assignment(sc.id, sta.load_assignment(sc.id))
    frozen_row = SectionInstructor.objects.get(scenario=sc, course_key=x)
    saved_rows, saved_names = (
        _rows(sc),
        sorted(
            TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
                "term_section__section", "day", "instructor"
            )
        ),
    )
    snapshot = snapshot_scenario(sc.id)
    assert len(snapshot.section_teachers) == len(saved_rows)

    # The run re-picks and renames everything it may (never the frozen X).
    SectionInstructor.objects.filter(scenario=sc, course_key=y).update(instructor=b)
    TermSectionMeeting.objects.filter(term_section__course_key=y).update(instructor="Dr B")
    restore_scenario(sc.id, snapshot)

    assert _rows(sc) == saved_rows
    assert SectionInstructor.objects.get(scenario=sc, course_key=x).pk == frozen_row.pk
    assert (
        sorted(
            TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
                "term_section__section", "day", "instructor"
            )
        )
        == saved_names
    )


@pytest.mark.django_db
def test_i10_a_rebuild_the_ownership_check_refuses_deletes_no_row(flags) -> None:
    sc, *_rest = _shared_out_board()
    sta.persist_assignment(sc.id, sta.load_assignment(sc.id))
    before = _rows(sc)

    with pytest.raises(ValueError, match="another scenario"):
        sta.prepare_section_teachers(sc.id, phase="rebuild")

    assert _rows(sc) == before


@pytest.mark.django_db
def test_i10_flag_off_snapshots_carry_no_teacher_rows(flags) -> None:
    from core.services.timetable_board_persistence import restore_scenario, snapshot_scenario

    sc, *_rest = _shared_out_board()
    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    snapshot = snapshot_scenario(sc.id)
    SectionInstructor.objects.filter(scenario=sc).delete()
    restore_scenario(sc.id, snapshot)

    assert snapshot.section_teachers is None
    assert not SectionInstructor.objects.filter(scenario=sc).exists()


# ── I11: a copy pins what it inherits ─────────────────────────────────────


@pytest.mark.django_db
def test_i11_a_copy_pins_the_frozen_teachers_and_keeps_the_same_map(flags) -> None:
    from core.services.timetable_scenario_copy import copy_timetable_scenario

    x = requirement("AI", "AI301", "X COURSE")
    sc, board = scenario()
    owner, _owner_board = scenario(name="owner")
    owner_rowed, named = instructor("Dr Owner"), instructor("Dr Named")
    link("AI", "AI301", owner_rowed)
    link("AI", "AI301", named, role="co")
    budget(sc, x, 1)
    foreign = section(owner, x, "S7")
    place(board, foreign, [("SUN", "08:00", "09:15")])
    SectionInstructor.objects.create(
        scenario=owner, course_key=x, section="S7", instructor=owner_rowed, source="engine"
    )
    global_named = section(None, "AI777", "G1")
    place(board, global_named, [("MON", "08:00", "09:15")], instructor_name="Dr Named")
    global_unknown = section(None, "AI778", "G2")
    place(board, global_unknown, [("TUE", "08:00", "09:15")], instructor_name="Visiting")
    source_map = sta.load_assignment(sc.id)

    copied = copy_timetable_scenario(sc.id, name="copy")

    copy_rows = {
        (ck, label): (teacher, source)
        for ck, label, teacher, source in SectionInstructor.objects.filter(
            scenario=copied.scenario
        ).values_list("course_key", "section", "instructor_id", "source")
    }
    assert copy_rows[(x, "S7")] == (owner_rowed.id, "copied_frozen")
    assert copy_rows[("AI777", "G1")] == (named.id, "copied_frozen")
    assert ("AI778", "G2") not in copy_rows
    assert copied.counts["teachers_released"] == 1
    copy_map = sta.load_assignment(copied.scenario.id)
    assert copy_map.teacher == source_map.teacher
    assert copy_map.status[f"{x}_S7"] == sta.STATUS_PINNED


# ── I16: link edits, deactivation and renames write nothing ───────────────


def _persisted_pair():
    x = requirement("AI", "AI301", "X COURSE")
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    budget(sc, x, 2)
    place(board, section(sc, x, "S1"), [("SUN", "08:00", "09:15")])
    place(board, section(sc, x, "S2"), [("SUN", "08:00", "09:15")])  # siblings at one time
    sta.prepare_section_teachers(sc.id, phase="layout")
    return sc, x, a, b


def _names(sc) -> list[tuple]:
    return sorted(
        TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
            "term_section__section", "instructor"
        )
    )


@pytest.mark.django_db
def test_i16_link_edits_deactivation_and_renames_write_nothing(flags) -> None:
    from core.services.course_instructor_assignment import set_course_instructors

    sc, x, a, b = _persisted_pair()
    rows, names = _rows(sc), _names(sc)
    assert {t for t in sta.load_assignment(sc.id).teacher.values()} == {a.id, b.id}
    in_force = sta.load_assignment(sc.id)
    client = _admin_client()

    with sta.pinned_assignment(sc.id, in_force):  # a running job
        set_course_instructors("AI", "AI301", "M", [b.id])  # Dr A's link removed
        assert sta.load_assignment(sc.id) is in_force
    assert (_rows(sc), _names(sc)) == (rows, names)
    after_edit = sta.load_assignment(sc.id)
    assert after_edit.report["stale_rows_ignored"] == 1  # Dr A's row, read as invalid
    assert a.id not in after_edit.teacher.values()

    set_course_instructors("AI", "AI301", "M", [a.id, b.id])
    response = client.post(
        "/ops/instructors/set-active/",
        data=json.dumps({"id": b.id, "is_active": False}),
        content_type="application/json",
    )
    assert response.status_code == 200
    response = client.post(
        "/ops/instructors/update/",
        data=json.dumps({"id": a.id, "full_name": "Dr A Renamed"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert (_rows(sc), _names(sc)) == (rows, names)
    after = sta.load_assignment(sc.id)
    assert b.id not in after.teacher.values()  # deactivated: dropped at read time
    assert a.id in after.teacher.values()  # renamed: kept, by FK


@pytest.mark.django_db
def test_i16_a_published_scenarios_rows_are_untouched(flags) -> None:
    from core.services.course_instructor_assignment import set_course_instructors

    sc, *_rest, b = _persisted_pair()
    sc.status = "published"
    sc.save()
    rows, names = _rows(sc), _names(sc)

    set_course_instructors("AI", "AI301", "M", [b.id])

    assert (_rows(sc), _names(sc)) == (rows, names)


@pytest.mark.django_db
def test_i16_reconcile_refuses_busy_and_published_scenarios_and_writes_names_only(flags) -> None:
    sc, x, a, b = _persisted_pair()
    client = _admin_client()
    url = "/ops/instructors/course-assignments/reconcile/"
    TermSectionMeeting.objects.filter(term_section__scenario=sc).update(instructor="stale")
    rows = _rows(sc)

    job = PlannerJob.objects.create(
        id=uuid.uuid4(), scenario=sc, mode=PlannerJob.MODE_OPTIMISE_V2_CURRENT, status="running"
    )
    busy = client.post(
        url, data=json.dumps({"scenario_id": sc.id}), content_type="application/json"
    )
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "SCENARIO_BUSY"
    job.status = "succeeded"
    job.save()
    sc.status = "published"
    sc.save()
    published = client.post(
        url, data=json.dumps({"scenario_id": sc.id}), content_type="application/json"
    )
    assert published.status_code == 409
    assert published.json()["error"]["code"] == "SCENARIO_PUBLISHED"
    assert {name for _label, name in _names(sc)} == {"stale"}

    sc.status = "draft"
    sc.save()
    done = client.post(
        url, data=json.dumps({"scenario_id": sc.id}), content_type="application/json"
    )
    assert done.status_code == 200
    assert _rows(sc) == rows
    teachers = dict(
        SectionInstructor.objects.filter(scenario=sc).values_list(
            "section", "instructor__full_name"
        )
    )
    assert dict(_names(sc)) == teachers


# ── I20: the scheduler bridge's teachers ──────────────────────────────────


@pytest.mark.django_db
def test_i20_bridge_rows_touch_only_bridge_keys_and_invalid_ones_are_rejected(flags) -> None:
    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    sc, _board = scenario()
    a, b, outsider = instructor("Dr A"), instructor("Dr B"), instructor("Dr Out")
    link("AI", "AI301", a)
    link("AI", "AI302", b)
    budget(sc, x, 2)
    budget(sc, y, 1)
    SectionInstructor.objects.create(
        scenario=sc, course_key=y, section="S1", instructor=b, source="engine"
    )  # another engine's row: not the bridge's to touch
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S2", instructor=a, source="engine"
    )  # the bridge's previous row for X S2

    report = sta.persist_bridge_assignment(
        sc.id, {(x, "S1"): a.id, (x, "S2"): outsider.id, (x, "S9"): a.id}
    )

    assert engine_rows(sc) == {(x, "S1"): "Dr A", (y, "S1"): "Dr B"}
    assert sorted((r["label"], r["reason"]) for r in report["bridge_rejected"]) == [
        ("S2", "not_eligible"),
        ("S9", "not_a_slot"),
    ]
    assert DeliveryBoard.objects.filter(scenario=sc).count() == 1
    # The bridge's own entry point does nothing with the rule off.
    flags.TIMETABLE_ONE_TEACHER_PER_SECTION = False
    assert sta.bridge_section_teachers(sc.id, {(x, "S1"): None}) is None
    assert engine_rows(sc) == {(x, "S1"): "Dr A", (y, "S1"): "Dr B"}
