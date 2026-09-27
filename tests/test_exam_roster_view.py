"""Student lists on screen: the one serializer, parity with the file, navigator facts,
lookups, search and the viewing cache.

The fixture run is made by the real build (see ``exam_student_export_fixture``),
so every saved key the screen reads is exactly what production saves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest

from core.models import Student, StudentTermSection
from core.services import exam_roster_view as rv
from core.services import exam_student_export as export
from core.services.exam_rosters import build_roster_model
from tests.exam_source_factory import scraped_exam_registration
from tests.exam_student_export_fixture import (
    ALL_IDS,
    FEMALE_CS2,
    FEMALE_IS,
    MALE_AI,
    MALE_CS,
    MALE_IS,
    NO_COHORT,
    OVERLOADED,
    SENTINELS,
    build_population,
    build_saved_run,
    save_payload,
    saved_payload,
    student_name,
)

pytestmark = pytest.mark.django_db

#: The allowed screen row, written out: changing ``ROSTER_ROW_KEYS`` must fail here.
PINNED_ROW_KEYS = (
    "student_id",
    "name",
    "program",
    "department",
    "group",
    "exam",
    "section_key",
    "section",
    "section_status",
    "room",
    "room_basis",
    "part",
    "clash",
    "clash_with",
    "same_day",
    "same_day_with",
    "exams_that_day",
    "change",
)


@pytest.fixture
def run():
    build_population()
    return build_saved_run()


def _view(run):
    return rv.build_roster_view(run, built=0.0)


def _group(model, exam, section=None, gender=None):
    return next(
        g
        for g in model.groups
        if g.exam == exam
        and (section is None or g.section == section)
        and (gender is None or g.gender == gender)
    )


def _scope_rows(view, scope):
    options = rv.parse_view_scope({"scope": scope}, view.model)
    return rv.scope_payload(view, options, rv.select_scope(view, options))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9.\-]+", text))


def _late_changes():
    """Lists that no longer match the save, one of every kind of difference."""
    # Two late MATH101 M1 students: the section changed and they have no seat.
    for sid in (4401900, 4401901):
        Student.objects.create(student_id=sid, name=student_name(sid), program="AI", section="M")
        scraped_exam_registration(sid, "MATH101", section_label="M1")
    # A same-size swap in IS201 M3.
    StudentTermSection.objects.filter(
        student_id=MALE_IS[-1], term_section__course_key="IS201"
    ).delete()
    scraped_exam_registration(MALE_AI[0], "IS201", section_label="M3")
    # A section new since the save, and one gone.
    scraped_exam_registration(MALE_AI[1], "CS101", section_label="M9")
    StudentTermSection.objects.filter(term_section__section="F2").delete()


# ── The one serializer ─────────────────────────────────────────


def test_the_row_key_set_is_pinned_and_holds_no_other_student_field():
    assert rv.ROSTER_ROW_KEYS == PINNED_ROW_KEYS
    assert rv.SEARCH_MATCH_KEYS == ("student_id", "name", "program", "department", "exams")
    forbidden = {"level", "gpa", "registration_no", "nationality", "status", "advisor_id"}
    assert not forbidden & set(rv.ROSTER_ROW_KEYS)


def test_every_row_of_every_answer_has_exactly_the_allowed_keys(run):
    view = _view(run)
    rows = [rv.roster_row(s, view.model) for s in view.sittings]
    assert len(rows) == len(view.sittings) > 0
    assert all(tuple(row) == PINNED_ROW_KEYS for row in rows)
    for scope in (
        {"kind": "course", "exam": "MATH101"},
        {"kind": "room", "slot_index": 0, "room_code": "M-B"},
    ):
        assert all(tuple(row) == PINNED_ROW_KEYS for row in _scope_rows(view, scope)["rows"])
    looked_up = rv.lookup_payload(view, rv.lookup_sittings(view, OVERLOADED))["rows"]
    assert looked_up and all(tuple(row) == PINNED_ROW_KEYS for row in looked_up)
    matched, total = rv.search_students(view, "4401")
    assert all(
        tuple(match) == rv.SEARCH_MATCH_KEYS
        for match in rv.search_payload(view, matched, total)["matches"]
    )


def test_no_answer_carries_a_value_of_a_forbidden_student_field(run):
    view = _view(run)
    everything = json.dumps(
        [
            [rv.roster_row(s, view.model) for s in view.sittings],
            _scope_rows(view, {"kind": "course", "exam": "MATH101"}),
            rv.lookup_payload(view, rv.lookup_sittings(view, OVERLOADED)),
            rv.search_payload(view, *rv.search_students(view, "testname")),
            view.navigator,
        ],
        ensure_ascii=False,
    )
    for value in SENTINELS.values():
        assert str(value) not in everything


def test_a_row_says_what_the_sitting_is(run):
    view = _view(run)
    model = view.model
    rows = {
        (row["student_id"], row["exam"]): row
        for row in (rv.roster_row(s, model) for s in view.sittings)
    }
    m1 = _group(model, "MATH101", "M1", "M")
    first_part, second_part = m1.parts
    assert rows[(MALE_IS[0], "MATH101")] == {
        "student_id": MALE_IS[0],
        "name": student_name(MALE_IS[0]),
        "program": "IS",
        "department": "is",
        "group": "M",
        "exam": "MATH101",
        "section_key": m1.section_key,
        "section": "M1",
        "section_status": "mapped",
        "room": first_part.room_code,
        "room_basis": "split",
        "part": 1,
        "clash": True,
        "clash_with": ["IS201"],
        "same_day": False,
        "same_day_with": [],
        "exams_that_day": 2,
        "change": None,
    }
    in_second = sorted(m1.members)[first_part.student_count]
    assert in_second == MALE_AI[0]
    assert {
        k: rows[(in_second, "MATH101")][k] for k in ("program", "department", "room", "part")
    } == {
        "program": "AI",
        "department": "ai-ds",
        "room": second_part.room_code,
        "part": 2,
    }
    assert rows[(in_second, "MATH101")]["exams_that_day"] == 1
    # Three Sunday exams: a clash at 08:00 and a same-day exam at 13:00.
    overloaded = rows[(OVERLOADED, "MATH101")]
    assert overloaded["name"] == student_name(OVERLOADED)
    assert (overloaded["clash_with"], overloaded["same_day_with"]) == (["IS201"], ["CS101"])
    assert overloaded["exams_that_day"] == 3 and overloaded["part"] == 1
    # No cohort: a section the lists don't name, a part the timetable could not room.
    unrecorded = rows[(NO_COHORT, "MATH101")]
    assert (unrecorded["section"], unrecorded["section_status"]) == (None, "missing")
    assert (unrecorded["room"], unrecorded["room_basis"], unrecorded["part"]) == (
        None,
        "unassigned",
        1,
    )
    assert unrecorded["group"] == "U"
    assert rows[(FEMALE_CS2[0], "CS101")]["room_basis"] == "whole"


def test_changes_since_save_are_marked_on_their_rows(run):
    _late_changes()
    view = _view(run)
    rows = [rv.roster_row(s, view.model) for s in view.sittings]
    late = [r for r in rows if r["student_id"] in (4401900, 4401901)]
    assert [(r["change"], r["room"], r["room_basis"], r["part"]) for r in late] == [
        ("changed", None, "no_seat", None)
    ] * 2
    new = next(r for r in rows if (r["student_id"], r["exam"]) == (MALE_AI[1], "CS101"))
    assert (new["change"], new["room_basis"], new["section"]) == ("new", "no_seat", "M9")
    swapped = next(r for r in rows if (r["student_id"], r["exam"]) == (MALE_AI[0], "IS201"))
    assert swapped["change"] == "changed" and swapped["room_basis"] == "whole"
    # Gone sections have no rows; unchanged ones carry no marker.
    assert not any(r["section"] == "F2" for r in rows)
    assert {r["change"] for r in rows if r["exam"] == "PHYS103 (1)"} == {None}


def test_an_ambiguous_or_labelless_mapping_reads_as_the_file_words_it(run):
    view = _view(run)
    m1 = _group(view.model, "MATH101", "M1", "M")
    from dataclasses import replace

    assert rv.section_display(replace(m1, mapping_status="ambiguous")) == (None, "ambiguous")
    assert rv.section_display(replace(m1, section="")) == (None, "missing")
    assert rv.section_display(m1) == ("M1", "mapped")


# ── Parity: the screen says what the file says ─────────────────


def _file_rows(run, scope):
    """The export's Student exams rows for a scope, from a model built afresh."""
    model = build_roster_model(run)
    options = export.parse_export_options(
        {"scope": scope, "language": "en", "one_file_per_group": False}, model
    )
    prepared = export.prepare_export(
        model, options, generated_by="parity", generated_role="EXAM_COMMITTEE"
    )
    keys = [column.key for column in export.STUDENT_EXAMS.columns]
    assert len(prepared.files) == 1
    return [dict(zip(keys, row, strict=True)) for row in prepared.files[0].tables["StudentExams"]]


def _floor(value):
    return int(value) if value and re.fullmatch(r"-?\d+", str(value)) else None


def _screen_as_file(row, payload):
    """A screen row, worded as the English file words it."""
    exam = payload["exams"][row["exam"]]
    rooms = {(r["slot_index"], r["room_code"]): r for r in payload["rooms"]}
    room = rooms.get((exam["slot_index"], row["room"])) if row["room"] else None
    departments = payload["departments"]
    return {
        "student_id": row["student_id"],
        "name": row["name"],
        "program": row["program"],
        "department": departments[row["department"]]["en"],
        "group": export._w(row["group"], "en"),
        "exam": row["exam"],
        "course_code": exam["course_code"],
        "course_name": exam["name"],
        "section": row["section"] or export._w(row["section_status"], "en"),
        "day_no": exam["day_no"],
        "day": exam["day"] or export._w("not_scheduled", "en"),
        "period": exam["period"],
        "start": exam["start"],
        "exam_room": row["room"],
        "building": room["building"] if room else None,
        "floor": _floor(room["floor"]) if room else None,
        "room_basis": export._w(row["room_basis"], "en"),
        "online": export._yes(exam["online"], "en"),
        "clash": export._yes(row["clash"], "en"),
        "clash_with": " · ".join(row["clash_with"]) or None,
        "same_day": export._yes(row["same_day"], "en"),
        "same_day_with": " · ".join(
            f"{payload['exams'][code]['start']} {code}" for code in row["same_day_with"]
        )
        or None,
        "exams_that_day": row["exams_that_day"],
        "change": {"changed": "Section changed", "new": "New since save"}.get(row["change"]),
    }


def _file_as_compared(row):
    compared = {key: row[key] for key in _SCREEN_KEYS}
    compared["start"] = row["start"].strftime("%H:%M") if row["start"] else None
    return compared


_SCREEN_KEYS = (
    "student_id",
    "name",
    "program",
    "department",
    "group",
    "exam",
    "course_code",
    "course_name",
    "section",
    "day_no",
    "day",
    "period",
    "start",
    "exam_room",
    "building",
    "floor",
    "room_basis",
    "online",
    "clash",
    "clash_with",
    "same_day",
    "same_day_with",
    "exams_that_day",
    "change",
)


def _scopes(model):
    m1 = _group(model, "MATH101", "M1", "M")
    unrecorded = _group(model, "MATH101", gender="U")
    return [
        {"kind": "room", "slot_index": 0, "room_code": m1.parts[0].room_code},
        {"kind": "room", "slot_index": 0, "room_code": m1.parts[1].room_code},
        {"kind": "room", "slot_index": 0, "room_code": "M-A"},
        {"kind": "room", "slot_index": 1, "room_code": "F-A"},
        {"kind": "course", "exam": "MATH101"},
        {"kind": "course", "exam": "IS201"},
        {"kind": "course", "exam": "CS101"},
        {"kind": "course", "exam": "PHYS103 (2)"},
        {"kind": "section", "exam": "MATH101", "section_key": m1.section_key, "gender": "M"},
        {
            "kind": "section",
            "exam": "MATH101",
            "section_key": unrecorded.section_key,
            "gender": "U",
        },
    ]


def _assert_parity(run):
    view = _view(run)
    compared = 0
    for scope in _scopes(view.model):
        if scope["kind"] == "room" and (scope["slot_index"], scope["room_code"]) not in (
            view.model.saved.rooms
        ):
            continue
        payload = _scope_rows(view, scope)
        screen = [_screen_as_file(row, payload) for row in payload["rows"]]
        try:
            file_rows = [_file_as_compared(row) for row in _file_rows(run, scope)]
        except export.EmptyScope:
            file_rows = []
        assert screen == file_rows, scope
        assert payload["counts"]["rows"] == len(file_rows)
        compared += len(screen)
    return compared


def test_screen_rows_equal_the_export_student_exams_rows_scope_by_scope(run):
    assert _assert_parity(run) > 80


def test_parity_holds_when_the_lists_changed_since_the_save(run):
    _late_changes()
    assert _assert_parity(run) > 80
    view = _view(run)
    math = _scope_rows(view, {"kind": "course", "exam": "MATH101"})
    assert math["counts"]["no_seat"] == 2 and math["counts"]["changed"] == 32


def test_a_split_room_lists_only_its_own_part_in_seat_order(run):
    view = _view(run)
    m1 = _group(view.model, "MATH101", "M1", "M")
    first, second = m1.parts
    ordered = sorted(m1.members)
    one = _scope_rows(view, {"kind": "room", "slot_index": 0, "room_code": first.room_code})
    two = _scope_rows(view, {"kind": "room", "slot_index": 0, "room_code": second.room_code})
    assert [r["student_id"] for r in one["rows"]] == ordered[: first.student_count]
    assert [r["student_id"] for r in two["rows"]] == ordered[first.student_count :]
    [section] = two["sections"]
    assert section["rows"] == second.student_count and section["now"] == len(ordered)
    # ID ranges come from this scope's rows only: part 1 names no ID here.
    assert [(p["position"], p["rows"], p["first_id"], p["last_id"]) for p in section["parts"]] == [
        (1, 0, None, None),
        (2, second.student_count, ordered[first.student_count], ordered[-1]),
    ]
    assert two["room"]["seated_now"] == second.student_count == two["counts"]["rows"]


def test_a_course_lists_every_section_including_gone_and_new(run):
    _late_changes()
    view = _view(run)
    cs101 = _scope_rows(view, {"kind": "course", "exam": "CS101"})
    tabs = {(s["section"], s["gender"]): s for s in cs101["sections"]}
    assert (tabs[("F2", "F")]["membership"], tabs[("F2", "F")]["rows"]) == ("gone", 0)
    assert (tabs[("M9", "M")]["membership"], tabs[("M9", "M")]["saved"]) == ("new", 0)
    assert tabs[("M9", "M")]["no_seat"] == 1 and tabs[("M2", "M")]["membership"] == "matches"
    assert set(cs101["exams"]) >= {"CS101", "MATH101", "IS201"}, "flags' other exams"


def test_scope_parsing_takes_the_exports_rules_and_three_kinds_only(run):
    view = _view(run)
    for body, field in [
        ({"scope": {"kind": "all"}}, "scope.kind"),
        ({"scope": {"kind": "day", "day": "Sun"}}, "scope.kind"),
        ({"scope": {"kind": "course", "exam": "NOPE101"}}, "scope.exam"),
        ({"scope": {"kind": "room", "slot_index": 0, "room_code": "F-C"}}, "scope.room_code"),
        ({"scope": {"kind": "room", "slot_index": "0", "room_code": "M-A"}}, "scope.slot_index"),
        ({"scope": {"kind": "course", "exam": "MATH101", "student_id": 1}}, "scope"),
        ({"scope": {"kind": "course", "exam": "MATH101"}, "student_id": 1}, "body"),
        ([], "body"),
    ]:
        with pytest.raises(export.ExportOptionsError) as caught:
            rv.parse_view_scope(body, view.model)
        assert caught.value.field == field, body


# ── Navigator facts ────────────────────────────────────────────


def test_navigator_facts_never_name_a_student(run):
    _late_changes()
    text = json.dumps(_view(run).navigator, ensure_ascii=False)
    everyone = [*ALL_IDS, 4401900, 4401901]
    assert not {str(sid) for sid in everyone} & _tokens(text)
    assert not any(student_name(sid) in text for sid in everyone)


def test_every_navigator_count_is_the_count_of_the_list_it_opens(run):
    _late_changes()
    view = _view(run)
    nav = view.navigator
    for room in nav["rooms"]:
        rows = _scope_rows(
            view, {"kind": "room", "slot_index": room["slot_index"], "room_code": room["room_code"]}
        )["rows"]
        assert room["seated_now"] == len(rows)
        assert room["flags"] == {
            "clash": sum(r["clash"] for r in rows),
            "same_day": sum(r["same_day"] for r in rows),
        }
        assert sum(s["now"] for s in room["sections"]) == len(rows)
    for exam in nav["exams"]:
        rows = _scope_rows(view, {"kind": "course", "exam": exam["code"]})["rows"]
        assert exam["students"] == len(rows)
        assert exam["flags"]["clash"] == sum(r["clash"] for r in rows)
        assert exam["flags"]["no_seat"] == sum(r["room_basis"] == "no_seat" for r in rows)
        assert sum(exam["programs"].values()) == len(rows)
    assert sum(slot["sittings"] for slot in nav["slots"]) == nav["totals"]["sittings"]
    assert sum(day["sittings"] for day in nav["days"]) == nav["totals"]["sittings"]
    # A room's list never holds a student without a seat; each No seat item
    # counts exactly the No seat rows of the section list it opens, and
    # together they hold every such student of the run once.
    for room in nav["rooms"]:
        scope = {"kind": "room", "slot_index": room["slot_index"], "room_code": room["room_code"]}
        assert _scope_rows(view, scope)["counts"]["no_seat"] == 0
    assert nav["no_seat"], "the late changes leave students without a seat"
    for item in nav["no_seat"]:
        scope = {key: item[key] for key in ("exam", "section_key", "gender")}
        payload = _scope_rows(view, {"kind": "section", **scope})
        assert item["no_seat"] == payload["counts"]["no_seat"] > 0
        assert item["now"] == len(payload["rows"])
        assert item["slot_index"] == payload["exams"][item["exam"]]["slot_index"]
    assert sum(item["no_seat"] for item in nav["no_seat"]) == nav["check"]["no_seat"]
    # A period's students are distinct, as a day's: a clash sits twice, counts once.
    for slot in nav["slots"]:
        ids = {
            row["student_id"]
            for code in slot["exams"]
            for row in _scope_rows(view, {"kind": "course", "exam": code})["rows"]
        }
        assert slot["students"] == len(ids)
    sunday_morning = nav["slots"][0]
    assert sunday_morning["exams"] == ["IS201", "MATH101"]
    assert sunday_morning["students"] < sunday_morning["sittings"]


def test_navigator_rooms_seats_and_review_markers(run):
    _late_changes()
    nav = _view(run).navigator
    rooms = {(r["slot_index"], r["room_code"]): r for r in nav["rooms"]}
    model = build_roster_model(run)
    m1 = _group(model, "MATH101", "M1", "M")
    for position, part in enumerate(m1.parts, 1):
        room = rooms[(0, part.room_code)]
        assert room["seated_at_save"] == part.student_count == room["seated_now"]
        [section] = room["sections"]
        assert (section["part"], section["parts"], section["gender"]) == (position, 2, "M")
        assert tuple(section) == (
            "exam",
            "section",
            "section_status",
            "gender",
            "membership",
            "part",
            "parts",
            "saved",
            "now",
        )
        # The section changed; its students without a seat are in no room.
        assert room["review"] == ["changed"]
    is201 = rooms[(0, "M-A")]
    assert is201["flags"] == {"clash": 11, "same_day": 1} and is201["review"] == ["changed"]
    exams = {e["code"]: e for e in nav["exams"]}
    [physics_room] = exams["PHYS103 (1)"]["rooms"]
    assert rooms[(2, physics_room)]["review"] == []
    assert exams["MATH101"]["review"] == ["changed", "no_seat", "not_assigned", "not_recorded"]
    assert exams["PHYS103 (1)"]["review"] == []
    assert exams["MATH101"]["saved"] == 43 and exams["MATH101"]["students"] == 45
    assert exams["MATH101"]["sections"] == 3 and exams["CS101"]["sections"] == 3
    assert nav["not_assigned"] == [
        {
            "slot_index": 0,
            "exam": "MATH101",
            "section_key": _group(model, "MATH101", gender="U").section_key,
            "gender": "U",
            "section": None,
            "section_status": "missing",
            "saved": 1,
            "now": 1,
        }
    ]
    assert nav["check"]["status"] == "changed" and nav["check"]["no_seat"] == 3
    # Where they are instead: under their period, the NEW section's too.
    m9 = _group(model, "CS101", "M9", "M")
    assert nav["no_seat"] == [
        {
            "slot_index": 0,
            "exam": "MATH101",
            "section_key": m1.section_key,
            "gender": "M",
            "section": "M1",
            "section_status": "mapped",
            "membership": "changed",
            "saved": 30,
            "now": 32,
            "no_seat": 2,
        },
        {
            "slot_index": 1,
            "exam": "CS101",
            "section_key": m9.section_key,
            "gender": "M",
            "section": "M9",
            "section_status": "mapped",
            "membership": "new",
            "saved": 0,
            "now": 1,
            "no_seat": 1,
        },
    ]
    assert not m9.parts, "a NEW section has no saved part, so no room ever lists it"


def test_a_programme_change_alone_marks_the_exam_not_its_rooms(run):
    # Same students, so every room still holds exactly who it held; the saved
    # department counts no longer hold, so the exam needs review.
    Student.objects.filter(student_id=MALE_AI[0]).update(program="CS")
    nav = _view(run).navigator
    exams = {e["code"]: e for e in nav["exams"]}
    assert "changed" in exams["MATH101"]["review"]
    assert exams["CS101"]["review"] == [] and exams["IS201"]["review"] == []
    m1 = _group(build_roster_model(run), "MATH101", "M1", "M")
    rooms = {(r["slot_index"], r["room_code"]): r for r in nav["rooms"]}
    assert [rooms[(0, part.room_code)]["review"] for part in m1.parts] == [[], []]
    assert nav["check"]["status"] == "changed" and nav["check"]["program_mix_changed"] == 1


def test_a_roomed_section_the_lists_do_not_name_needs_review():
    build_population()
    late = 4401950
    Student.objects.create(student_id=late, name=student_name(late), program="CS", section="M")
    # A female section label on a male student: the lists name no usable section.
    scraped_exam_registration(late, "MATH101", section_label="F1")
    run = build_saved_run()
    view = _view(run)
    unnamed = next(
        g
        for g in view.model.groups
        if (g.exam, g.gender) == ("MATH101", "M") and g.mapping_status != "mapped"
    )
    assert unnamed.members == (late,)
    [part] = unnamed.parts
    assert part.assigned
    row = next(rv.roster_row(s, view.model) for s in view.sittings if s.student_id == late)
    assert (row["section"], row["section_status"], row["room"], row["room_basis"]) == (
        None,
        "missing",
        part.room_code,
        "whole",
    )
    rooms = {(r["slot_index"], r["room_code"]): r for r in view.navigator["rooms"]}
    assert "not_recorded" in rooms[(0, part.room_code)]["review"]
    # The room names the section by its cohort, as the row (``group``) does.
    [named] = [s for s in rooms[(0, part.room_code)]["sections"] if s["section_status"] != "mapped"]
    assert (named["section"], named["section_status"], named["gender"]) == (None, "missing", "M")
    scope = {"kind": "room", "slot_index": 0, "room_code": part.room_code}
    payload = _scope_rows(view, scope)
    assert [_screen_as_file(r, payload) for r in payload["rows"]] == [
        _file_as_compared(r) for r in _file_rows(run, scope)
    ]


def test_navigator_rooms_read_building_floor_then_code_blanks_last(run):
    nav = _view(run).navigator
    first_slot = [r["room_code"] for r in nav["rooms"] if r["slot_index"] == 0]
    # Building "172" floor 1 (A), "172" no floor (B), then no building (C).
    assert first_slot == sorted(first_slot, key=lambda c: ("ABC".index(c[-1]), c))
    slots = [r["slot_index"] for r in nav["rooms"]]
    assert slots == sorted(slots)


def test_navigator_periods_include_empty_ones_and_group_exams_by_slot(run):
    data = saved_payload(run)
    data["slots"].append({"index": 4, "day": "Tue", "period": "08:00-10:00"})
    save_payload(run, data)
    nav = _view(run).navigator
    assert [(s["slot_index"], s["day"], s["period"], s["start"]) for s in nav["slots"]] == [
        (0, "Sun", "08:00-10:00", "08:00"),
        (1, "Sun", "13:00-15:00", "13:00"),
        (2, "Mon", "08:00-10:00", "08:00"),
        (3, "Mon", "13:00-15:00", "13:00"),
        (4, "Tue", "08:00-10:00", "08:00"),
    ]
    assert [s["exams"] for s in nav["slots"]] == [
        ["IS201", "MATH101"],
        ["CS101"],
        ["PHYS103 (1)"],
        ["PHYS103 (2)"],
        [],
    ]
    assert nav["slots"][-1]["sittings"] == 0 and nav["slots"][-1]["rooms"] == 0
    assert [(d["day"], d["day_no"], d["weekday"]) for d in nav["days"]] == [
        ("Sun", 1, 6),
        ("Mon", 2, 0),
        ("Tue", 3, 1),
    ]
    assert [e["code"] for e in nav["exams"]] == [
        "IS201",
        "MATH101",
        "CS101",
        "PHYS103 (1)",
        "PHYS103 (2)",
    ]


def test_an_exam_the_timetable_could_not_place_is_listed_apart(run):
    data = saved_payload(run)
    entry = next(e for e in data["schedule"] if e["course_code"] == "CS101")
    entry.update(day="OVERFLOW", period="Extra-9", slot_index=9, rooms=[])
    save_payload(run, data)
    nav = _view(run).navigator
    assert nav["not_scheduled"] == ["CS101"]
    cs101 = next(e for e in nav["exams"] if e["code"] == "CS101")
    assert cs101["review"] == ["not_scheduled"] and cs101["slot_index"] is None
    assert all("CS101" not in s["exams"] for s in nav["slots"])


def test_programs_and_departments_are_offered_with_counts(run):
    nav = _view(run).navigator
    assert nav["programs"] == [
        {"program": "AI", "department": "ai-ds", "students": 10},
        {"program": "CS", "department": "cs", "students": 11},
        {"program": "CS2", "department": "cs", "students": 6},
        {"program": "IS", "department": "is", "students": 16},
    ]
    assert set(nav["departments"]) == {"ai-ds", "cs", "is"}
    assert nav["departments"]["cs"]["en"] == "Computer Science"


# ── Lookup and search ──────────────────────────────────────────


def test_a_lookup_shows_every_exam_of_the_student_in_timetable_order(run):
    view = _view(run)
    payload = rv.lookup_payload(view, rv.lookup_sittings(view, OVERLOADED))
    assert payload["found"] is True
    assert [row["exam"] for row in payload["rows"]] == [
        "IS201",
        "MATH101",
        "CS101",
        "PHYS103 (1)",
    ]
    assert {row["student_id"] for row in payload["rows"]} == {OVERLOADED}
    assert payload["counts"]["clash"] == 2 and payload["counts"]["same_day"] == 3
    assert set(payload["exams"]) == {"IS201", "MATH101", "CS101", "PHYS103 (1)"}
    slots = {code: facts["slot_index"] for code, facts in payload["exams"].items()}
    assert [(r["slot_index"], r["room_code"]) for r in payload["rooms"]] == sorted(
        (slots[row["exam"]], row["room"]) for row in payload["rows"]
    )
    missing = rv.lookup_payload(view, rv.lookup_sittings(view, 4499999))
    assert missing["found"] is False and missing["rows"] == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("4401001", 4401001),
        (4401001, 4401001),
        (" 4401001 ", 4401001),
        ("".join(chr(0x0660 + int(d)) for d in "4401001"), 4401001),
        ("".join(chr(0x06F0 + int(d)) for d in "4401001"), 4401001),
    ],
)
def test_a_student_id_is_read_in_either_digit_set(raw, expected):
    assert rv.parse_lookup({"student_id": raw}) == rv.LookupRequest("student", expected)


@pytest.mark.parametrize(
    "body, field",
    [
        ({"student_id": True}, "student_id"),
        ({"student_id": 4401001.0}, "student_id"),
        ({"student_id": "44O1001"}, "student_id"),
        ({"student_id": "-4401001"}, "student_id"),
        ({"student_id": "1" * 13}, "student_id"),
        ({"student_id": ""}, "student_id"),
        ({"student_id": "4401001", "query": "4401"}, "body"),
        ({}, "body"),
        ({"student_ids": ["4401001"]}, "body"),
        ([4401001], "body"),
        ({"query": "440"}, "query"),
        ({"query": "ab"}, "query"),
        ({"query": " a  b "}, "query"),
        # Letters are counted, not characters, and every word needs two.
        ({"query": "e e e"}, "query"),
        ({"query": "s t e"}, "query"),
        ({"query": "---"}, "query"),
        ({"query": "a-b"}, "query"),
        ({"query": "ahmad m"}, "query"),
        # Digits beside letters are a course or room code, never a name.
        ({"query": "MATH101"}, "query"),
        ({"query": "testname 4401"}, "query"),
        ({"query": "M-B12"}, "query"),
        ({"query": "x" * 65}, "query"),
        ({"query": "abc\x00def"}, "query"),
        ({"query": "abc\ud800"}, "query"),
        ({"query": 4401}, "query"),
    ],
)
def test_lookups_refuse_what_they_cannot_read(body, field):
    with pytest.raises(rv.RosterRequestError) as caught:
        rv.parse_lookup(body)
    assert caught.value.field == field and caught.value.code == "invalid_request"


def test_digits_search_ids_by_prefix_and_is_capped(run):
    view = _view(run)
    request = rv.parse_lookup({"query": "4401 00"})
    assert request == rv.LookupRequest("search", query="440100")
    matched, total = rv.search_students(view, request.query)
    assert matched == MALE_CS[: rv.SEARCH_LIMIT] and total == 9
    payload = rv.search_payload(view, matched, total)
    assert payload["more"] is True and payload["total"] == 9
    assert rv.search_payload(view, *rv.search_students(view, "4401001"))["more"] is False
    assert payload["matches"][0] == {
        "student_id": MALE_CS[0],
        "name": student_name(MALE_CS[0]),
        "program": "CS",
        "department": "cs",
        "exams": 4,
    }
    assert rv.search_students(view, "4402007") == ([FEMALE_IS[0]], 1)
    assert rv.search_students(view, "4499") == ([], 0)
    assert rv.search_students(view, "1001") == ([], 0), "a prefix, never a substring"


def test_names_are_searched_word_by_word_with_prefix_matches_first(run):
    Student.objects.filter(student_id=MALE_IS[0]).update(name="TESTNAME FIRST")
    view = _view(run)
    request = rv.parse_lookup({"query": "  TestName   Student "})
    assert request.query == "testname student"
    matched, total = rv.search_students(view, request.query)
    assert total == len(ALL_IDS) - 1, "every name but the renamed one, which has no 'student'"
    assert MALE_IS[0] not in matched
    first, total = rv.search_students(view, "testname")
    assert first[0] == MALE_IS[0] and total == len(ALL_IDS)
    # Hyphens and apostrophes belong to names; a word needs two letters.
    assert rv.parse_lookup({"query": "Al-Harbi O'Neil"}).query == "al-harbi o'neil"
    assert rv.parse_lookup({"query": "al ab"}).query == "al ab"


def test_arabic_names_match_across_hamza_tashkeel_tatweel_and_final_forms(run):
    # "أحمد" with a fatha and a tatweel, and "فاطمة" / "مصطفى": all written by code point.
    ahmad = chr(0x0623) + chr(0x062D) + chr(0x0640) + chr(0x0645) + chr(0x064E) + chr(0x062F)
    fatima = chr(0x0641) + chr(0x0627) + chr(0x0637) + chr(0x0645) + chr(0x0629)
    mustafa = chr(0x0645) + chr(0x0635) + chr(0x0637) + chr(0x0641) + chr(0x0649)
    Student.objects.filter(student_id=FEMALE_IS[0]).update(name=f"{fatima} {ahmad}")
    Student.objects.filter(student_id=MALE_IS[1]).update(name=f"{mustafa} {ahmad}")
    view = _view(run)
    plain_ahmad = chr(0x0627) + chr(0x062D) + chr(0x0645) + chr(0x062F)
    plain_fatima = chr(0x0641) + chr(0x0627) + chr(0x0637) + chr(0x0645) + chr(0x0647)
    plain_mustafa = chr(0x0645) + chr(0x0635) + chr(0x0637) + chr(0x0641) + chr(0x064A)
    query = rv.parse_lookup({"query": plain_ahmad}).query
    assert rv.search_students(view, query) == ([FEMALE_IS[0], MALE_IS[1]], 2)
    assert rv.search_students(view, rv.normalise_search_text(plain_fatima)) == (
        [FEMALE_IS[0]],
        1,
    )
    assert rv.search_students(view, rv.normalise_search_text(plain_mustafa))[0] == [MALE_IS[1]]


def test_search_only_finds_students_of_this_run(run):
    Student.objects.create(student_id=4401999, name="OUTSIDER TESTNAME", program="CS", section="M")
    view = _view(run)
    assert rv.search_students(view, "outsider") == ([], 0)
    assert rv.search_students(view, "4401999") == ([], 0)
    assert rv.lookup_sittings(view, 4401999) == []


#: Every key a lookup's audit row holds, written out: adding one (the search
#: text, a name) or dropping one must fail here.
LOOKUP_AUDIT_KEYS = {
    "run_id",
    "mode",
    "search_kind",
    "search_length",
    "student_id",
    "shown_student_ids",
    "matches",
    "lists_code_now",
    "lists_checked_at",
    "cached",
}


def test_lookup_audit_details_name_students_by_plain_id_and_never_keep_the_search(run):
    view = _view(run)
    shown = [*FEMALE_CS2, *FEMALE_IS][: rv.SEARCH_LIMIT]
    request = rv.parse_lookup({"query": "4402"})
    matched, total = rv.search_students(view, request.query)
    details = rv.lookup_audit_details(view, request, matched, total, cached=True)
    assert details == {
        "run_id": run.pk,
        "mode": "search",
        "search_kind": "id_prefix",
        "search_length": 4,
        "student_id": None,
        "shown_student_ids": shown,
        "matches": 12,
        "lists_code_now": view.model.lists_code_now,
        "lists_checked_at": view.checked_at.isoformat(),
        "cached": True,
    }
    # A name search: the students shown by ID, the words typed nowhere.
    name = rv.parse_lookup({"query": "  Student   TestName "})
    found = rv.search_students(view, name.query)
    details_name = rv.lookup_audit_details(view, name, *found, cached=False)
    assert set(details_name) == LOOKUP_AUDIT_KEYS
    assert (details_name["mode"], details_name["search_kind"]) == ("search", "name")
    assert (details_name["search_length"], details_name["student_id"]) == (15, None)
    assert details_name["shown_student_ids"] == found[0] and len(found[0]) == rv.SEARCH_LIMIT
    assert details_name["matches"] == len(ALL_IDS)
    # An exact lookup: the student asked for, and shown.
    student = rv.parse_lookup({"student_id": str(OVERLOADED)})
    details_id = rv.lookup_audit_details(view, student, [OVERLOADED], 1, cached=False)
    assert set(details_id) == LOOKUP_AUDIT_KEYS
    assert (details_id["mode"], details_id["search_kind"], details_id["search_length"]) == (
        "student",
        "id",
        7,
    )
    assert (details_id["student_id"], details_id["shown_student_ids"]) == (OVERLOADED, [OVERLOADED])
    assert details_id["matches"] == 1
    # Asked for and not found: who was looked up is still on record; no one was shown.
    missing = rv.lookup_audit_details(view, rv.LookupRequest("student", 4499999), [], 0, False)
    assert (missing["student_id"], missing["shown_student_ids"], missing["matches"]) == (
        4499999,
        [],
        0,
    )
    # Plain JSON numbers a person (or the Audit Explorer) reads as they are.
    text = json.dumps([details, details_name, details_id, missing])
    tokens = _tokens(text)
    assert {str(sid) for sid in [*shown, *found[0], OVERLOADED, 4499999]} <= tokens
    # No search text: not the digits typed (only whole IDs), not a word of the name.
    assert "4402" not in tokens
    assert "testname" not in text.lower() and name.query not in text.lower()
    # No name.
    assert not any(student_name(sid) in text for sid in ALL_IDS)


def test_scope_audit_details_hold_the_scope_and_counts_never_a_student(run):
    view = _view(run)
    options = rv.parse_view_scope({"scope": {"kind": "course", "exam": "MATH101"}}, view.model)
    sittings = rv.select_scope(view, options)
    details = rv.scope_audit_details(view, options, sittings, cached=False)
    assert details == {
        "run_id": run.pk,
        "scope": {"kind": "course", "value": "MATH101"},
        "rows": 43,
        "students": 43,
        "check": "matches",
        "lists_code_saved": view.model.lists_code_saved,
        "lists_code_now": view.model.lists_code_now,
        "lists_checked_at": view.checked_at.isoformat(),
        "cached": False,
    }


# ── The viewing cache ──────────────────────────────────────────


@dataclass
class _Run:
    pk: int
    created_at: object = None


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Builds:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float]] = []

    def __call__(self, run, *, built):
        self.calls.append((run.pk, built))
        return rv.RosterView(
            model=None,  # type: ignore[arg-type]
            sittings=[],
            by_exam={},
            by_room={},
            by_student={},
            search_names={},
            arabic_names={},
            navigator={"run": run.pk},
            built=built,
        )


def _cache(**kwargs):
    clock, builds = _Clock(), _Builds()
    return rv.RosterViewCache(clock=clock, build=builds, **kwargs), clock, builds


def test_the_cache_serves_a_run_for_its_ttl_then_rebuilds():
    cache, clock, builds = _cache(ttl=60)
    first, cached = cache.get(_Run(1))
    assert cached is False and len(builds.calls) == 1
    clock.now += 59
    again, cached = cache.get(_Run(1))
    assert again is first and cached is True and len(builds.calls) == 1
    clock.now += 1  # exactly the TTL: expired
    fresh, cached = cache.get(_Run(1))
    assert fresh is not first and cached is False and builds.calls[-1] == (1, clock.now)
    assert rv.ROSTER_VIEW_TTL_SECONDS == 60


def test_refresh_rebuilds_unless_a_build_started_after_it_was_asked():
    cache, clock, builds = _cache()
    first, _ = cache.get(_Run(1))
    clock.now += 1
    refreshed, cached = cache.get(_Run(1), refresh=True)
    assert refreshed is not first and cached is False and len(builds.calls) == 2
    # Asked at the instant that build started: two Refresh presses, one build.
    same, cached = cache.get(_Run(1), refresh=True)
    assert same is refreshed and cached is False and len(builds.calls) == 2
    clock.now += 1
    assert cache.get(_Run(1), refresh=True)[0] is not refreshed and len(builds.calls) == 3


def test_a_request_that_waited_for_a_build_rechecks_the_ttl():
    cache, clock, builds = _cache(ttl=60)
    cache.get(_Run(1))  # built at 100
    clock.now = 159  # fresh when this request is swept ...
    sweep = cache._sweep

    def sweep_then_wait(keep=None):
        sweep(keep)
        clock.now = 160  # ... and expired once it holds the build lock

    cache._sweep = sweep_then_wait
    view, cached = cache.get(_Run(1))
    assert cached is False and [built for _pk, built in builds.calls] == [100, 160]


def test_a_run_is_keyed_by_its_id_and_saved_time():
    cache, _clock, builds = _cache()
    cache.get(_Run(1, created_at=None))

    class _Saved:
        def isoformat(self):
            return "2026-09-26T10:00:00+00:00"

    cache.get(_Run(1, created_at=_Saved()))
    assert len(builds.calls) == 2 and len(cache) == 2


def test_the_cache_holds_few_runs_least_recently_used_first_out():
    cache, clock, builds = _cache(max_runs=2)
    one, _ = cache.get(_Run(1))
    cache.get(_Run(2))
    assert cache.get(_Run(1))[0] is one
    cache.get(_Run(3))
    assert len(cache) == 2 and cache.get(_Run(1))[0] is one
    cache.get(_Run(2))
    assert [pk for pk, _built in builds.calls] == [1, 2, 3, 2]


def test_expired_runs_are_dropped_at_the_next_request():
    cache, clock, _builds = _cache(ttl=60, max_runs=5)
    cache.get(_Run(1))
    clock.now += 61
    cache.get(_Run(2))
    assert len(cache) == 1
    cache.get(_Run(3))
    clock.now += 30
    assert cache.get(_Run(3))[1] is True and len(cache) == 2
    # Both expire; a request for one of them frees the other too.
    clock.now += 31
    assert cache.get(_Run(3))[1] is False and len(cache) == 1


def test_a_build_held_too_long_answers_busy_and_the_lock_is_released_after_errors():
    cache, _clock, _builds = _cache(wait=0)
    assert cache.build_lock.acquire()
    try:
        with pytest.raises(rv.RosterBusy):
            cache.get(_Run(1))
    finally:
        cache.build_lock.release()

    def refuse(run, *, built):
        raise rv.RosterRequestError("refused")

    failing = rv.RosterViewCache(build=refuse, wait=0)
    with pytest.raises(rv.RosterRequestError):
        failing.get(_Run(1))
    assert not failing.build_lock.locked() and len(failing) == 0


def test_the_real_cache_builds_through_the_phase_one_model(run, monkeypatch):
    cache = rv.RosterViewCache()
    view, cached = cache.get(run)
    assert cached is False and view.model.saved.run_id == run.pk
    assert view.checked_at == view.model.checked_at
    assert cache.get(run) == (view, True)
    assert rv.freshness(view, True) == {
        "run": {
            "id": run.pk,
            "label": run.label,
            "saved_at": run.created_at.isoformat(),
            "academic_year": "1448",
            "term": "1",
        },
        "checked_at": view.model.checked_at.isoformat(),
        "cached": True,
        "cache_ttl_seconds": 60,
    }


def test_saved_slots_are_the_whole_grid_without_overflow(run):
    data = saved_payload(run)
    data["slots"].append({"index": 9, "day": "OVERFLOW", "period": "Extra-9"})
    save_payload(run, data)
    model = build_roster_model(run)
    assert model.saved.slots == (
        (0, "Sun", "08:00-10:00"),
        (1, "Sun", "13:00-15:00"),
        (2, "Mon", "08:00-10:00"),
        (3, "Mon", "13:00-15:00"),
    )
