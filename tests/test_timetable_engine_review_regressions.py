"""Persisted counterexamples found by the independent engine review."""

import pytest

from core.models import DeliveryBoard, SectionPlacement, TermSection, TermSectionMeeting
from core.services import timetable_local_search as sa
from core.services import timetable_optimizer_v2 as optimizer
from core.services.timetable_assignment_models import RiskTier, StudentProfile
from core.services.timetable_instructor_cap_repair import (
    repair_instructor_clashes,
    repair_instructor_daily_overloads,
)
from core.services.timetable_instructor_compaction import compact_instructor_schedules
from tests.test_instructor_compaction import _gappy_board


@pytest.fixture
def engine_settings(settings):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_LINKS_ENABLED = True
    settings.TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED = True
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_MAX_ROUNDS = 4
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 0


def _shared_board(scenario):
    original = SectionPlacement.objects.get(
        board__scenario=scenario, term_section__course_code="C2"
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario, label="Shared", nominal_term=2, program="AI"
    )
    copy = SectionPlacement.objects.create(
        board=board,
        term_section=original.term_section,
        day=original.day,
        start_time=original.start_time,
        end_time=original.end_time,
        room=original.room,
    )
    return original, copy


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_compaction_moves_shared_physical_copies_and_canonical_meeting_together():
    scenario = _gappy_board()
    SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_code="C1"
    ).update(is_locked=True)
    original, copy = _shared_board(scenario)
    meeting = TermSectionMeeting.objects.get(term_section=original.term_section)
    ids = {original.pk, copy.pk}
    created = {p.pk: p.created_at for p in (original, copy)}
    from core.models import TimetableRepairRun

    run = TimetableRepairRun.objects.create(scenario=scenario, target_placement=copy)
    instructor = meeting.instructor

    report = compact_instructor_schedules(scenario.pk)

    rows = list(SectionPlacement.objects.filter(term_section=original.term_section))
    meeting.refresh_from_db()
    assert report["search"]["moves_accepted"] == 1
    assert {(p.day, p.start_time, p.end_time) for p in rows} == {("MON", "10:30", "11:45")}
    assert (meeting.day, meeting.start_time, meeting.end_time) == ("MON", "10:30", "11:45")
    assert {p.pk for p in rows} == ids
    assert {p.pk: p.created_at for p in rows} == created
    assert meeting.instructor == instructor
    run.refresh_from_db()
    assert run.target_placement_id == copy.pk
    assert report["instructor_impact"]["total_idle_after"] == 15


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_compaction_lock_in_one_shared_copy_freezes_all_copies():
    scenario = _gappy_board()
    original, copy = _shared_board(scenario)
    copy.is_locked = True
    copy.save(update_fields=["is_locked"])
    before = list(SectionPlacement.objects.filter(term_section=original.term_section).values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section=original.term_section).values()
    )

    compact_instructor_schedules(scenario.pk)

    assert (
        list(SectionPlacement.objects.filter(term_section=original.term_section).values()) == before
    )
    assert (
        list(TermSectionMeeting.objects.filter(term_section=original.term_section).values())
        == meeting_before
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_compaction_keeps_already_divergent_shared_copies_frozen():
    scenario = _gappy_board()
    original, copy = _shared_board(scenario)
    copy.start_time, copy.end_time = "14:30", "15:45"
    copy.save(update_fields=["start_time", "end_time"])
    before = list(SectionPlacement.objects.filter(term_section=original.term_section).values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section=original.term_section).values()
    )

    compact_instructor_schedules(scenario.pk)

    assert (
        list(SectionPlacement.objects.filter(term_section=original.term_section).values()) == before
    )
    assert (
        list(TermSectionMeeting.objects.filter(term_section=original.term_section).values())
        == meeting_before
    )


def _sa_candidate(scenario, starts):
    board = DeliveryBoard.objects.get(scenario=scenario)
    sections = [
        {"code": ts.course_key, "label": ts.section, "term_section_id": ts.pk}
        for ts in TermSection.objects.filter(scenario=scenario).order_by("pk")
    ]
    schedule = {
        i: [{"day": day, "start": start, "end": end}]
        for i, sec in enumerate(sections)
        for day, start, end in [starts[sec["code"]]]
    }
    return board, {
        "status": "optimized",
        "schedule": schedule,
        "sections": sections,
        "cost_before": 200,
        "cost_after": 100,
        "improvements": 1,
    }


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_sa_persistence_accepts_teacher_idle_gain_with_student_gap_trade(monkeypatch):
    scenario = _gappy_board()
    SectionPlacement.objects.filter(
        board__scenario=scenario, term_section__course_code="C2"
    ).update(start_time="12:00", end_time="13:15")
    TermSectionMeeting.objects.filter(
        term_section__scenario=scenario, term_section__course_code="C2"
    ).update(start_time="12:00", end_time="13:15")
    board = DeliveryBoard.objects.get(scenario=scenario)
    section = TermSection.objects.create(
        scenario=scenario,
        course_key="C3",
        course_code="C3",
        course_name="C3",
        course_number="C3",
        section="S1",
        source_tag="tw_auto",
    )
    SectionPlacement.objects.create(
        board=board,
        term_section=section,
        day="MON",
        start_time="13:30",
        end_time="14:45",
        room="R1",
    )
    TermSectionMeeting.objects.create(
        term_section=section,
        day="MON",
        start_time="13:30",
        end_time="14:45",
    )
    TermSection.objects.filter(scenario=scenario).update(source_tag="tw_auto")
    profiles = {"bc": StudentProfile("bc", "AI", ["C2", "C3"], RiskTier.C, 0)}
    monkeypatch.setattr(optimizer, "build_student_profiles_for_scenario", lambda _: profiles)
    board, candidate = _sa_candidate(
        scenario,
        {
            "C1": ("MON", "09:00", "10:15"),
            "C2": ("MON", "10:30", "11:45"),
            "C3": ("MON", "13:30", "14:45"),
        },
    )
    monkeypatch.setattr(sa, "optimize_board", lambda *args, **kwargs: candidate)

    report = sa.optimize_and_persist_board(board.pk)

    assert report["sa_evaluator_rolled_back"] is False
    assert not report.get("persist_rolled_back")
    moved = SectionPlacement.objects.get(board=board, term_section__course_code="C2")
    assert moved.start_time == "10:30"


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("priority", [False, True])
def test_sa_persistence_legacy_switch_controls_teacher_day_guard(settings, monkeypatch, priority):
    settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = priority
    scenario = _gappy_board()
    TermSection.objects.filter(scenario=scenario).update(source_tag="tw_auto")
    board, candidate = _sa_candidate(
        scenario,
        {
            "C1": ("MON", "09:00", "10:15"),
            "C2": ("SUN", "09:00", "10:15"),
        },
    )
    monkeypatch.setattr(sa, "optimize_board", lambda *args, **kwargs: candidate)

    report = sa.optimize_and_persist_board(board.pk)

    assert bool(report.get("persist_rolled_back")) is priority
    moved = SectionPlacement.objects.get(board=board, term_section__course_code="C2")
    assert moved.day == ("MON" if priority else "SUN")


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_cap_and_clash_repairs_count_shared_copies_as_one_session(settings):
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 2
    scenario = _gappy_board()
    original, _copy = _shared_board(scenario)
    before = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario).values()
    )

    cap_report = repair_instructor_daily_overloads(scenario.pk)
    clash_report = repair_instructor_clashes(scenario.pk)

    assert cap_report["detected"] == []
    assert clash_report["detected"] == []
    assert clash_report["remaining_clashes"] == 0
    assert list(SectionPlacement.objects.filter(board__scenario=scenario).values()) == before
    assert (
        list(TermSectionMeeting.objects.filter(term_section__scenario=scenario).values())
        == meeting_before
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_real_cap_repair_moves_shared_copies_as_one_physical_session(settings):
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 1
    scenario = _gappy_board()
    original, copy = _shared_board(scenario)
    ids = {original.pk, copy.pk}
    meeting = TermSectionMeeting.objects.get(term_section=original.term_section)
    meeting_id = meeting.pk
    from core.models import TimetableRepairRun

    run = TimetableRepairRun.objects.create(scenario=scenario, target_placement=copy)

    report = repair_instructor_daily_overloads(scenario.pk)

    rows = list(SectionPlacement.objects.filter(term_section=original.term_section))
    meeting.refresh_from_db()
    assert len(report["repaired"]) == 1
    assert report["remaining_violations"] == 0
    assert {p.pk for p in rows} == ids
    assert len({(p.day, p.start_time, p.end_time, p.room) for p in rows}) == 1
    assert rows[0].day != "MON"
    assert (meeting.day, meeting.start_time, meeting.end_time) == (
        rows[0].day,
        rows[0].start_time,
        rows[0].end_time,
    )
    assert meeting.pk == meeting_id
    run.refresh_from_db()
    assert run.target_placement_id == copy.pk


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_cap_repair_lock_in_one_copy_freezes_shared_physical_meeting(settings):
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 1
    scenario = _gappy_board()
    original, copy = _shared_board(scenario)
    copy.is_locked = True
    copy.save(update_fields=["is_locked"])
    before = list(SectionPlacement.objects.filter(term_section=original.term_section).values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section=original.term_section).values()
    )

    report = repair_instructor_daily_overloads(scenario.pk)

    assert report["remaining_violations"] == 0
    assert (
        list(SectionPlacement.objects.filter(term_section=original.term_section).values()) == before
    )
    assert (
        list(TermSectionMeeting.objects.filter(term_section=original.term_section).values())
        == meeting_before
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize(
    "repair_pass", [compact_instructor_schedules, repair_instructor_daily_overloads]
)
def test_shared_copy_write_failure_restores_every_placement_and_canonical_meeting(
    settings,
    monkeypatch,
    repair_pass,
):
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = (
        1 if repair_pass is repair_instructor_daily_overloads else 3
    )
    scenario = _gappy_board()
    if repair_pass is compact_instructor_schedules:
        SectionPlacement.objects.filter(
            board__scenario=scenario, term_section__course_code="C1"
        ).update(is_locked=True)
    _original, copy = _shared_board(scenario)
    before = list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section__scenario=scenario).order_by("pk").values()
    )
    save = SectionPlacement.save

    def fail_second_copy(row, *args, **kwargs):
        if row.pk == copy.pk:
            raise RuntimeError("shared copy write failed")
        return save(row, *args, **kwargs)

    monkeypatch.setattr(SectionPlacement, "save", fail_second_copy)
    with pytest.raises(RuntimeError, match="shared copy write failed"):
        repair_pass(scenario.pk)

    assert (
        list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
        == before
    )
    assert (
        list(
            TermSectionMeeting.objects.filter(term_section__scenario=scenario)
            .order_by("pk")
            .values()
        )
        == meeting_before
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize(
    "repair_pass", [repair_instructor_daily_overloads, repair_instructor_clashes]
)
def test_mandatory_repairs_freeze_divergent_shared_section(settings, repair_pass):
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 1
    scenario = _gappy_board()
    original, copy = _shared_board(scenario)
    copy.start_time, copy.end_time = "14:30", "15:45"
    copy.save(update_fields=["start_time", "end_time"])
    before = list(SectionPlacement.objects.filter(term_section=original.term_section).values())
    meeting_before = list(
        TermSectionMeeting.objects.filter(term_section=original.term_section).values()
    )

    repair_pass(scenario.pk)

    assert (
        list(SectionPlacement.objects.filter(term_section=original.term_section).values()) == before
    )
    assert (
        list(TermSectionMeeting.objects.filter(term_section=original.term_section).values())
        == meeting_before
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_exact_physical_room_persistence_preserves_repair_run_placement_reference():
    from core.models import (
        TimetableRepairCandidate,
        TimetableRepairGlobalPlan,
        TimetableRepairGlobalPlanItem,
        TimetableRepairRun,
    )
    from core.services.timetable_secondary_recovery_runner import _persist_exact_room_result

    scenario = _gappy_board()
    placement = SectionPlacement.objects.get(
        board__scenario=scenario, term_section__course_code="C2"
    )
    run = TimetableRepairRun.objects.create(
        scenario=scenario,
        target_placement=placement,
        target_section=placement.term_section,
    )
    candidate = TimetableRepairCandidate.objects.create(
        run=run,
        candidate_id="engine-review",
        day="MON",
        start_time="10:30",
        end_time="11:45",
    )
    plan = TimetableRepairGlobalPlan.objects.create(
        scenario=scenario, request_signature="engine-review"
    )
    item = TimetableRepairGlobalPlanItem.objects.create(
        plan=plan,
        sequence=1,
        repair_run=run,
        candidate=candidate,
        placement=placement,
    )
    rows = list(SectionPlacement.objects.filter(pk=placement.pk).values())
    _persist_exact_room_result(
        scenario.pk,
        {
            "candidate_placements_by_id": {placement.pk: ("MON", "10:30", "11:45")},
            "assignment_by_placement_id": {placement.pk: "R1"},
        },
        rows,
    )

    run.refresh_from_db()
    assert run.target_placement_id == placement.pk
    item.refresh_from_db()
    assert item.placement_id == placement.pk
    placement.refresh_from_db()
    assert placement.created_at == rows[0]["created_at"]
    assert placement.updated_at == rows[0]["updated_at"]


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
def test_exact_physical_room_write_failure_restores_original_keys_and_audit(monkeypatch):
    from core.services.timetable_secondary_recovery_runner import _persist_exact_room_result

    scenario = _gappy_board()
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
    placement = rows[1]
    update = SectionPlacement.objects.bulk_update
    calls = 0

    def fail_final(rows, fields, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("final keys failed")
        return update(rows, fields, **kwargs)

    monkeypatch.setattr(SectionPlacement.objects, "bulk_update", fail_final)
    with pytest.raises(RuntimeError, match="final keys failed"):
        _persist_exact_room_result(
            scenario.pk,
            {
                "candidate_placements_by_id": {placement["id"]: ("MON", "10:30", "11:45")},
                "assignment_by_placement_id": {placement["id"]: "R1"},
            },
            rows,
        )
    assert (
        list(SectionPlacement.objects.filter(board__scenario=scenario).order_by("pk").values())
        == rows
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("merge", [False, True])
def test_exact_physical_room_slot_swap_and_merge_rejection(merge):
    from core.services.timetable_exact_rooming import ExactRoomingError
    from core.services.timetable_secondary_recovery_runner import _persist_exact_room_result

    scenario = _gappy_board()
    first = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C2")
    second = SectionPlacement.objects.create(
        board=first.board,
        term_section=first.term_section,
        day="WED",
        start_time="13:00",
        end_time="14:15",
        room="R1",
    )
    rows = list(
        SectionPlacement.objects.filter(term_section=first.term_section).order_by("pk").values()
    )
    result = {
        "candidate_placements_by_id": {
            first.pk: ("WED", "13:00", "14:15"),
            second.pk: ("WED" if merge else "MON", "13:00", "14:15"),
        },
        "assignment_by_placement_id": {first.pk: "R1", second.pk: "R1"},
    }
    if merge:
        with pytest.raises(ExactRoomingError, match="merge distinct"):
            _persist_exact_room_result(scenario.pk, result, rows)
        assert (
            list(
                SectionPlacement.objects.filter(term_section=first.term_section)
                .order_by("pk")
                .values()
            )
            == rows
        )
    else:
        assert _persist_exact_room_result(scenario.pk, result, rows) == 2
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.day, second.day) == ("WED", "MON")


def _external_copy(scenario, locked):
    from core.models import TimetableScenario

    original = SectionPlacement.objects.get(
        board__scenario=scenario, term_section__course_code="C2"
    )
    other = TimetableScenario.objects.create(
        academic_year="1448", term="1", name="External", gender="M", programs=["AI"]
    )
    board = DeliveryBoard.objects.create(
        scenario=other, label="External", nominal_term=1, program="AI"
    )
    copy = SectionPlacement.objects.create(
        board=board,
        term_section=original.term_section,
        day=original.day,
        start_time=original.start_time,
        end_time=original.end_time,
        room=original.room,
        is_locked=locked,
    )
    return original, copy


def _section_snapshot(section_id):
    return (
        list(SectionPlacement.objects.filter(term_section_id=section_id).order_by("pk").values()),
        list(TermSectionMeeting.objects.filter(term_section_id=section_id).order_by("pk").values()),
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("repair_pass", ["compaction", "cap", "clash"])
def test_external_scenario_copy_freezes_physical_section(settings, locked, repair_pass):
    scenario = _gappy_board()
    original, copy = _external_copy(scenario, locked)
    c1 = SectionPlacement.objects.get(board__scenario=scenario, term_section__course_code="C1")
    c1.is_locked = True
    if repair_pass == "clash":
        c1.start_time, c1.end_time = original.start_time, original.end_time
        TermSectionMeeting.objects.filter(term_section=c1.term_section).update(
            start_time=c1.start_time, end_time=c1.end_time
        )
    c1.save()
    settings.TIMETABLE_INSTRUCTOR_DAILY_CAP = 1
    before = _section_snapshot(original.term_section_id)
    report = {
        "compaction": compact_instructor_schedules,
        "cap": repair_instructor_daily_overloads,
        "clash": repair_instructor_clashes,
    }[repair_pass](scenario.pk)
    assert _section_snapshot(original.term_section_id) == before
    if repair_pass == "compaction":
        assert report["search"]["moves_accepted"] == 0
        assert (
            report["instructor_impact"]["total_idle_before"]
            == report["instructor_impact"]["total_idle_after"]
        )
    else:
        assert not report["repaired"]


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("writer", ["sa", "balance"])
def test_external_section_candidate_rejected_without_false_gain(
    settings, monkeypatch, locked, writer
):
    from core.services import timetable_load_balanced as balance

    scenario = _gappy_board()
    board, candidate = _sa_candidate(
        scenario, {"C1": ("MON", "09:00", "10:15"), "C2": ("MON", "10:30", "11:45")}
    )
    original, copy = _external_copy(scenario, locked)
    settings.TIMETABLE_ENFORCE_LOCKS = False
    before = _section_snapshot(original.term_section_id)
    if writer == "sa":
        monkeypatch.setattr(sa, "optimize_board", lambda *a, **k: candidate)
        result = sa.optimize_and_persist_board(board.pk)
        assert result["cost_after"] == result["cost_before"]
    else:
        candidate.update(score_before=200, score_after=100)
        monkeypatch.setattr(balance, "rebalance_board", lambda *a, **k: candidate)
        result = balance.rebalance_and_persist_board(board.pk)
        assert result["score_after"] == result["score_before"]
    assert result["persist_rolled_back"]
    assert result["improvements"] == 0
    assert "externally owned" in result["rollback_reasons"][0]
    assert _section_snapshot(original.term_section_id) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
def test_external_section_v2_and_canonical_sync_preserve_rows(locked):
    from core.services.timetable_assignment_models import SectionMeeting
    from core.services.timetable_board_persistence import sync_meetings_from_placements

    scenario = _gappy_board()
    original, copy = _external_copy(scenario, locked)
    before = _section_snapshot(original.term_section_id)
    states = {s.section_id: s for s in optimizer.build_section_states_for_scenario(scenario.pk)}
    states["C2_S1"].meetings = [SectionMeeting(day=0, start_min=630, end_min=705)]
    assert "C2_S1" in optimizer.build_locked_section_ids_for_scenario(scenario.pk)
    result = optimizer.persist_section_states_to_scenario(scenario.pk, states)
    assert result["updated"] == 0
    sync_meetings_from_placements(scenario.pk)
    assert _section_snapshot(original.term_section_id) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("room", ["R1", "", "UNASSIGNED"])
def test_external_section_room_oracle_and_writer_freeze_rooms(settings, locked, room):
    from core.services.timetable_recovery_rooming import build_recovery_room_evaluator
    from core.services.timetable_rooming import assign_rooms_to_board
    from core.services.timetable_secondary_recovery_runner import _persist_exact_room_result

    scenario = _gappy_board()
    original, copy = _external_copy(scenario, locked)
    SectionPlacement.objects.filter(term_section=original.term_section).update(room=room)
    original.refresh_from_db()
    before = _section_snapshot(original.term_section_id)
    oracle = build_recovery_room_evaluator(scenario.pk, count_only=False)
    result = oracle(None)
    assert result["assignment_by_placement_id"][original.pk] == room
    rows = list(SectionPlacement.objects.filter(board__scenario=scenario).values())
    _persist_exact_room_result(scenario.pk, result, rows)
    from core.services.timetable_secondary_recovery_runner import _sync_recovered_meetings

    _sync_recovered_meetings(
        rows,
        list(TermSectionMeeting.objects.filter(term_section__scenario=scenario).values()),
        result,
        scenario_id=scenario.pk,
    )
    assign_rooms_to_board(original.board_id, respect_locked=False)
    assert _section_snapshot(original.term_section_id) == before
    bad = {
        "candidate_placements_by_id": {
            original.pk: (original.day, original.start_time, original.end_time)
        },
        "assignment_by_placement_id": {original.pk: "R2"},
    }
    with pytest.raises(ValueError, match="externally owned"):
        _persist_exact_room_result(scenario.pk, bad, rows)
    assert _section_snapshot(original.term_section_id) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("entry", ["reset", "bridge"])
def test_rebuild_refuses_external_shared_sections_before_any_write(locked, entry):
    from core.services.timetable_board_persistence import reset_scenario
    from scheduler.bridge import build_into_scenario

    scenario = _gappy_board()
    original, copy = _external_copy(scenario, locked)
    before = (
        list(SectionPlacement.objects.order_by("pk").values()),
        list(TermSectionMeeting.objects.order_by("pk").values()),
    )
    with pytest.raises(ValueError, match="resolve shared section ownership"):
        if entry == "reset":
            reset_scenario(scenario.pk, keep_locked=False)
        else:
            build_into_scenario(
                scenario.pk, academic_year="1448", term=1, programs=["AI"], gender="M", seconds=0
            )
    assert (
        list(SectionPlacement.objects.order_by("pk").values()),
        list(TermSectionMeeting.objects.order_by("pk").values()),
    ) == before


@pytest.mark.django_db
@pytest.mark.usefixtures("engine_settings")
@pytest.mark.parametrize("locked", [False, True])
def test_sa_improves_owned_section_while_external_section_stays_identical(monkeypatch, locked):
    scenario = _gappy_board()
    board, candidate = _sa_candidate(
        scenario, {"C1": ("MON", "10:30", "11:45"), "C2": ("MON", "13:00", "14:15")}
    )
    original, copy = _external_copy(scenario, locked)
    TermSection.objects.filter(scenario=scenario, course_code="C1").update(source_tag="tw_auto")
    before = _section_snapshot(original.term_section_id)
    monkeypatch.setattr(sa, "optimize_board", lambda *a, **k: candidate)
    result = sa.optimize_and_persist_board(board.pk)
    assert not result.get("persist_rolled_back")
    assert result["improvements"] == 1
    assert (
        SectionPlacement.objects.get(board=board, term_section__course_code="C1").start_time
        == "10:30"
    )
    assert _section_snapshot(original.term_section_id) == before
