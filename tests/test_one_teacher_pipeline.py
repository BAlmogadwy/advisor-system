"""One teacher per section through the runs and the edits (I8, I9, I12, I14, I15,
I17, I19, I21).

Generate rebuilds the teachers with the board (pre-build map while candidates
are built and re-placed, the winner's map persisted with the winning board, a
final re-pick on the final board); Optimise Current reads the saved map before
anything is written, starts from a bounded re-pick inside the rollback guard,
and ends with a re-pick that is adopted only when it is no worse. Every phase
charges one pinned map; a rollback or a cancel brings the saved rows back.
Manual moves, repairs, global plans and safe-time moves never re-pick: they
keep the stored rows, and the backstop reports a clash they introduce.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

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
    engine_rows,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)
from tests.test_one_teacher_flag_parity import build_corpus

RUN = {"max_iterations": 3, "run_chain": False, "run_cpsat": False, "max_chain_iterations": 2}


@pytest.fixture
def flags(settings):
    for name, value in FLAG_ON.items():
        setattr(settings, name, value)
    settings.TIMETABLE_SECONDARY_RECOVERY_ENABLED = False
    settings.TIMETABLE_INSTRUCTOR_COMPACTION_TIME_BUDGET_SECONDS = 5
    return settings


def _rows(sc) -> list[tuple]:
    return sorted(
        SectionInstructor.objects.filter(scenario=sc).values_list(
            "course_key", "section", "instructor_id", "source"
        )
    )


def _names(sc) -> list[tuple]:
    return sorted(
        TermSectionMeeting.objects.filter(term_section__scenario=sc).values_list(
            "term_section__course_key", "term_section__section", "day", "instructor"
        )
    )


def _names_follow_the_map(sc) -> bool:
    from core.models import Instructor

    current = sta.load_assignment(sc.id)
    people = dict(Instructor.objects.values_list("id", "full_name"))
    for slot in current.slots:
        if slot.term_section_id is None or current.status[slot.key] not in ("engine", "tbd"):
            continue
        wanted = people.get(current.teacher[slot.key], "") if current.teacher[slot.key] else ""
        names = set(
            TermSectionMeeting.objects.filter(term_section_id=slot.term_section_id).values_list(
                "instructor", flat=True
            )
        )
        if names and names != {wanted}:
            return False
    return True


def _full_run(sc, **extra):
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    return run_v2_optimisation_guarded(
        sc.id, mode="full", strategies=["compact"], **{**RUN, **extra}
    )


def _current_run(sc, **extra):
    from core.services.timetable_v2_runner import run_v2_optimisation_guarded

    return run_v2_optimisation_guarded(sc.id, mode="current", **{**RUN, **extra})


# ── I17: Generate ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_i17_generate_rebuilds_persists_and_finally_repicks_the_teachers(
    flags, monkeypatch
) -> None:
    from core.services import timetable_autoplace, timetable_optimizer_v2
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate

    flags.TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED = True
    sc = build_corpus()
    calls: list[tuple] = []
    real_place = timetable_autoplace.auto_place_scenario
    real_persist_board = timetable_optimizer_v2.persist_section_states_to_scenario
    real_persist_teachers = sta._persist

    def placer(scenario_id, *args, **kwargs):
        pin = sta.current_pin(scenario_id)
        calls.append(("place", pin.report.get("engine") if pin else None))
        return real_place(scenario_id, *args, **kwargs)

    def persist_board(*args, **kwargs):
        calls.append(("board",))
        return real_persist_board(*args, **kwargs)

    def persist_teachers(scenario_id, assignment, **kwargs):
        calls.append(("teachers", assignment.report.get("engine")))
        depths.append(len(connection.savepoint_ids))  # how deep in atomic blocks
        return real_persist_teachers(scenario_id, assignment, **kwargs)

    depths: list[int] = []

    monkeypatch.setattr(timetable_autoplace, "auto_place_scenario", placer)
    monkeypatch.setattr(timetable_optimizer_v2, "persist_section_states_to_scenario", persist_board)
    monkeypatch.setattr(sta, "_persist", persist_teachers)

    result = _full_run(sc)

    assert result["safety_blocked"] is False
    teachers = result["section_teachers"]
    assert {"pre_build", "winner", "final", "saved", "coverage"} <= set(teachers)
    assert teachers["final"]["reason"] in {
        "adopted",
        "worse_than_kept_map",
        "adds_violations",
    }
    # Candidates and the re-placement run under the pre-build map; the winner's
    # map is stored right after its board, and the final map last.
    assert {c for c in calls if c[0] == "place"} == {("place", "pre_build")}
    board_at = calls.index(("board",))
    assert calls[board_at + 1] == ("teachers", "cpsat")
    assert calls[-1][0] == "teachers"
    # The winner's rows are written inside the board's own transaction.
    teacher_calls = [c for c in calls if c[0] == "teachers"]
    winner_at = teacher_calls.index(("teachers", "cpsat"))
    assert depths[winner_at] > depths[-1]
    assert engine_rows(sc)
    assert _names_follow_the_map(sc)
    # The reported score is the persisted board's, under the persisted map.
    from core.services.timetable_flags import is_tiered_objective_enabled
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_course_tier_map_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )
    from core.services.timetable_student_assignment import instructor_idle_of

    persisted = evaluate_generated_timetable_candidate(
        candidate_id="check",
        generated_sections=build_section_states_for_scenario(sc.id),
        student_profiles=build_student_profiles_for_scenario(sc.id),
        course_rigidity=build_course_rigidity_for_scenario(sc.id),
        section_instructor_ids=build_section_instructor_map_for_scenario(sc.id),
        course_tiers=build_course_tier_map_for_scenario(sc.id)
        if is_tiered_objective_enabled()
        else None,
    )
    assert list(persisted.lexicographic_score) == result["final_score"]
    # The winner was judged under its own teachers before anything read it.
    assert result["instructor_gap_metric"]["idle_minutes_before"] == instructor_idle_of(
        result["best_score"]
    )


@pytest.mark.django_db
def test_i17_the_final_repick_runs_without_any_reinstatement(flags) -> None:
    flags.TIMETABLE_REQUIRED_SECTION_GUARANTEE_ENABLED = False
    sc = build_corpus()

    result = _full_run(sc)

    assert "required_section_reinstatement" not in result
    final = result["section_teachers"]["final"]
    assert final["coverage"]["violations"] == {"clash_pairs": 0, "over_cap_sessions": 0}
    assert "persist" in final and final["reason"]
    assert sta.coverage_of(sta.load_assignment(sc.id)) == final["coverage"]


# ── Optimise Current ──────────────────────────────────────────────────────


def _built():
    """The corpus with a board, built by a flag-on Generate."""
    sc = build_corpus()
    assert _full_run(sc)["safety_blocked"] is False
    return sc


@pytest.mark.django_db
def test_i8_every_stage_of_optimise_current_charges_the_run_start_map(flags, monkeypatch) -> None:
    from core.services import (
        timetable_local_search_chains,
        timetable_local_search_v2,
        timetable_optimizer_v2,
    )

    sc = _built()
    state: dict = {"inside": False, "pin": None, "loads": [], "passed": []}
    real_load = sta.load_assignment
    real_optimise = timetable_optimizer_v2.optimise_current_timetable
    real_ls = timetable_local_search_v2.diagnostic_driven_local_search
    real_chain = timetable_local_search_chains.chain_local_search

    def load(scenario_id, **kwargs):
        loaded = real_load(scenario_id, **kwargs)
        if state["inside"]:
            state["loads"].append(loaded)
        return loaded

    def optimise(*args, **kwargs):
        state["inside"], state["pin"] = True, sta.current_pin(sc.id)
        try:
            return real_optimise(*args, **kwargs)
        finally:
            state["inside"] = False

    def ls(*args, **kwargs):
        state["passed"].append(kwargs["section_instructor_ids"])
        return real_ls(*args, **kwargs)

    def chain(*args, **kwargs):
        state["passed"].append(kwargs["section_instructor_ids"])
        return real_chain(*args, **kwargs)

    monkeypatch.setattr(sta, "load_assignment", load)
    monkeypatch.setattr(timetable_optimizer_v2, "optimise_current_timetable", optimise)
    monkeypatch.setattr(timetable_local_search_v2, "diagnostic_driven_local_search", ls)
    monkeypatch.setattr(timetable_local_search_chains, "chain_local_search", chain)

    result = _current_run(sc, run_chain=True)

    start = state["pin"]
    assert start is not None and start.report["engine"] == "stored"
    assert "run_start" in result["section_teachers"]
    # The baseline, the searches, the persist gate, the repairs, compaction and
    # the backstop all read the map from the database — and all got the pin.
    assert len(state["loads"]) >= 3
    assert all(loaded is start for loaded in state["loads"])
    assert state["passed"] and all(m == start.v2_map() for m in state["passed"])


def _cross_and_pinned():
    """Dr A pinned on Sunday 08:00, Dr B on Monday 08:00; X (either) on Sunday
    14:00, stored with Dr B. Giving X to Dr A saves Dr B a day and costs Dr A
    idle — a better week in total, a worse one for Dr A."""
    x = requirement("AI", "AI301", "X COURSE")
    p = requirement("AI", "AI401", "PIN A")
    q = requirement("AI", "AI402", "PIN B")
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")
    for key in (x, p, q):
        budget(sc, key, 1)
    for key, person, day, start in ((p, a, "SUN", "08:00"), (q, b, "MON", "08:00")):
        place(board, section(sc, key, "S1"), [(day, start, "09:15")])
        SectionInstructor.objects.create(
            scenario=sc, course_key=key, section="S1", instructor=person, source="department"
        )
    place(board, section(sc, x, "S1"), [("SUN", "14:00", "15:15")], instructor_name="Dr B")
    SectionInstructor.objects.create(
        scenario=sc, course_key=x, section="S1", instructor=b, source="engine"
    )
    return sc, x, a, b


@pytest.mark.django_db
def test_i9_the_final_repick_is_rejected_when_it_costs_a_teacher_and_kept_otherwise(
    flags,
) -> None:
    from core.services.timetable_optimisation_policy import instructor_schedule_metrics
    from core.services.timetable_optimizer_v2 import (
        _final_teacher_recompute,
        build_section_states_for_scenario,
    )
    from core.services.timetable_v2_runner import _RunTeachers, _week_gate

    sc, x, a, b = _cross_and_pinned()
    in_force = sta.load_assignment(sc.id)
    teachers = _RunTeachers(sc.id)
    teachers.saved = teachers.start = in_force
    before = instructor_schedule_metrics(
        build_section_states_for_scenario(sc.id), in_force.v2_map()
    )
    gate = _week_gate(sc.id, before, {}, teachers)

    kept, changed, report = _final_teacher_recompute(sc.id, in_force, gate=gate)

    assert (changed, report["adopted"], report["reason"]) == (
        False,
        False,
        "instructor_week_regression",
    )
    assert kept.teacher[f"{x}_S1"] == b.id
    assert engine_rows(sc) == {(x, "S1"): "Dr B"}

    adopted, changed, report = _final_teacher_recompute(sc.id, in_force)

    assert (changed, report["adopted"]) == (True, True)
    assert adopted.teacher[f"{x}_S1"] == a.id
    assert engine_rows(sc) == {(x, "S1"): "Dr A"}
    assert set(
        TermSectionMeeting.objects.filter(term_section__course_key=x).values_list(
            "instructor", flat=True
        )
    ) == {"Dr A"}


@pytest.mark.django_db
def test_i12_coverage_is_reported_and_a_tbd_rise_rolls_back(flags, monkeypatch) -> None:
    import dataclasses

    from core.services import timetable_optimizer_v2

    sc = _built()
    result = _current_run(sc)
    coverage = result["instructor_schedule_metric"]["coverage"]
    assert set(coverage) == {"before", "after"}
    assert coverage["after"]["tbd_beyond_d10"] == 0
    assert result["section_teachers"]["coverage"]["tbd"] == coverage["after"]["tbd"]
    regressions = result["safety_regression"]["regressions"]
    assert not any(r["metric"] == "teacher_coverage" for r in regressions)

    rows = _rows(sc)
    real = timetable_optimizer_v2._final_teacher_recompute

    def drops_a_teacher(scenario_id, in_force, **kwargs):
        stored, changed, final = real(scenario_id, in_force, **kwargs)
        key = next(k for k, s in stored.status.items() if s == sta.STATUS_ENGINE)
        worse = dataclasses.replace(
            stored,
            teacher={**stored.teacher, key: None},
            status={**stored.status, key: sta.STATUS_TBD},
        )
        return worse, True, final

    monkeypatch.setattr(timetable_optimizer_v2, "_final_teacher_recompute", drops_a_teacher)

    blocked = _current_run(sc)

    assert blocked["safety_blocked"] is True
    [row] = [
        r for r in blocked["safety_regression"]["regressions"] if r["metric"] == "teacher_coverage"
    ]
    assert row["after"] == row["before"] + 1
    assert _rows(sc) == rows  # the saved teachers are back


@pytest.mark.django_db
def test_i19_a_rollback_and_a_cancel_both_return_the_saved_map(flags, monkeypatch) -> None:
    from core.services import timetable_v2_runner
    from core.services.planner_job_snapshot import restore_planner_job, snapshot_planner_job

    sc = _built()
    # Make the saved names stale, as after a link edit: C2 rewrites them. And
    # drop one stored row, so the run's own persist really changes the table.
    TermSectionMeeting.objects.filter(term_section__scenario=sc).update(instructor="stale")
    SectionInstructor.objects.filter(scenario=sc, source="engine").order_by("pk").first().delete()
    saved_rows = _rows(sc)
    job_baseline = snapshot_planner_job(sc.id)
    order: list[str] = []
    real_snapshot = timetable_v2_runner.snapshot_scenario
    real_load, real_names, real_propose = (
        sta.load_assignment,
        sta.write_names,
        sta.propose_layout_assignment,
    )

    def tag(name, real):
        def wrapper(*args, **kwargs):
            order.append(name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(timetable_v2_runner, "snapshot_scenario", tag("snapshot", real_snapshot))
    monkeypatch.setattr(sta, "load_assignment", tag("load", real_load))
    monkeypatch.setattr(sta, "write_names", tag("names", real_names))
    monkeypatch.setattr(sta, "propose_layout_assignment", tag("propose", real_propose))
    monkeypatch.setattr(
        timetable_v2_runner,
        "optimiser_safety_regression",
        lambda before, after: {"blocked": True, "regressions": [{"metric": "forced"}]},
    )

    result = _current_run(sc)

    assert result["safety_blocked"] is True
    snapshot_at = order.index("snapshot")
    assert order[0] == "load"  # C0, before anything is written
    assert "names" in order[:snapshot_at]  # C2, so the snapshot holds the saved names
    assert order.index("propose") > snapshot_at  # C5, inside the guard
    assert _rows(sc) == saved_rows
    assert _names_follow_the_map(sc)
    rolled_back = sta.load_assignment(sc.id).teacher

    restore_planner_job(job_baseline)  # a cancel restores the job-start state

    assert _rows(sc) == saved_rows
    assert sta.load_assignment(sc.id).teacher == rolled_back


@pytest.mark.django_db
def test_i21_each_run_phase_reads_section_instructors_once(flags, monkeypatch) -> None:
    from core.services.timetable_v2_runner import _RunTeachers

    sc = _built()
    assert DeliveryBoard.objects.filter(scenario=sc).count() == 2
    reads: dict[str, int] = {}

    def counted(name):
        real = getattr(_RunTeachers, name)

        def wrapper(self, *args, **kwargs):
            with CaptureQueriesContext(connection) as ctx:
                out = real(self, *args, **kwargs)
            reads[name] = sum(
                1
                for q in ctx.captured_queries
                if "section_instructors" in q["sql"]
                and q["sql"].lstrip().upper().startswith("SELECT")
            )
            return out

        return wrapper

    for name in ("pre_step", "save", "finish"):
        monkeypatch.setattr(_RunTeachers, name, counted(name))

    _current_run(sc)

    assert reads == {"pre_step": 1, "save": 1, "finish": 1}


# ── I14 / I15: manual edits keep rows; manual creates get a teacher ──────


def _admin_client():
    from django.contrib.auth.models import Group, User
    from django.test import Client

    from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups

    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="pipeline-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_i14_a_manual_move_keeps_the_rows_and_the_backstop_reports_its_clash(flags) -> None:
    import json

    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    w = requirement("AI", "AI303", "W COURSE")
    sc, board = scenario()
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI301", b, role="co")  # X could go to Dr B — a re-pick would do that
    link("AI", "AI302", a)
    link("AI", "AI303", b)
    for key in (x, y, w):
        budget(sc, key, 1)
    place(board, section(sc, x, "S1"), [("SUN", "08:00", "09:15")])
    place(board, section(sc, y, "S1"), [("MON", "08:00", "09:15")])
    place(board, section(sc, w, "S1"), [("SUN", "08:30", "09:45")])  # Dr B is busy
    sta.prepare_section_teachers(sc.id, phase="layout")
    SectionPlacement.objects.filter(term_section__course_key=w).update(day="TUE")  # free now
    rows = _rows(sc)
    assert dict(((ck, s), t) for ck, s, t, _src in rows)[(x, "S1")] == a.id
    moved = SectionPlacement.objects.get(term_section__course_key=y)

    response = _admin_client().post(
        f"/ops/tw/placements/{moved.id}/move/",
        data=json.dumps({"day": "SUN", "start_time": "08:00", "end_time": "09:15"}),
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    assert _rows(sc) == rows  # Dr A keeps both: a manual move never re-picks
    backstop = response.json()["instructor_clash_backstop"]
    assert backstop["checked"] and backstop["count"] == 1


def _stored(sc) -> list[tuple]:
    """The rows by primary key: a delete-and-recreate is a change too."""
    return sorted(
        SectionInstructor.objects.filter(scenario=sc).values_list(
            "pk", "course_key", "section", "instructor_id", "source"
        )
    )


def _spy_teacher_writes(monkeypatch) -> list[int]:
    """Every persist of the teacher rows goes through ``sta._persist``."""
    writes: list[int] = []
    real = sta._persist

    def spy(scenario_id, *args, **kwargs):
        writes.append(scenario_id)
        return real(scenario_id, *args, **kwargs)

    monkeypatch.setattr(sta, "_persist", spy)
    return writes


def _spy_backstop(monkeypatch) -> dict[str, dict]:
    from core.services import timetable_instructor_backstop as backstop

    reports: dict[str, dict] = {}
    real = backstop.verify_persisted_scenario

    def spy(scenario_id, *, context, **kwargs):
        reports[context] = real(scenario_id, context=context, **kwargs)
        return reports[context]

    monkeypatch.setattr(backstop, "verify_persisted_scenario", spy)
    return reports


@pytest.mark.django_db
def test_i14_safe_time_moves_keep_the_rows_and_the_backstop_reports_their_clash(
    flags, monkeypatch
) -> None:
    from core.services import timetable_workspace as tws

    x = requirement("AI", "AI301", "X COURSE")
    y = requirement("AI", "AI302", "Y COURSE")
    w = requirement("AI", "AI303", "W COURSE")
    sc, board = scenario()
    other = DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="AI")
    a, b = instructor("Dr A"), instructor("Dr B")
    link("AI", "AI301", a)
    link("AI", "AI302", a)
    link("AI", "AI302", b, role="co")  # Y could go to Dr B — a re-pick would do that
    link("AI", "AI303", b)
    for key in (x, y, w):
        budget(sc, key, 1)
    place(other, section(sc, x, "S1"), [("SUN", "08:00", "09:15")], room="R2")
    place(board, section(sc, y, "S1"), [("MON", "08:00", "09:15")])
    place(board, section(sc, w, "S1"), [("MON", "08:30", "09:45")])  # Dr B is busy
    sta.prepare_section_teachers(sc.id, phase="layout")
    SectionPlacement.objects.filter(term_section__course_key=w).update(day="TUE")  # free now
    assert engine_rows(sc) == {(x, "S1"): "Dr A", (y, "S1"): "Dr A", (w, "S1"): "Dr B"}
    rows = _stored(sc)
    moved = SectionPlacement.objects.get(term_section__course_key=y)

    # The bulk fixer's candidates, as in test_bulk_safe_time_preview_and_apply:
    # Y to Sunday 08:00, clean on its own board. Dr A teaches X then, on the
    # other board, which the board-scoped validation cannot see.
    monkeypatch.setattr(
        tws,
        "build_scenario_builder_actions",
        lambda scenario_id, *, limit=18: {
            "actions": [
                {
                    "kind": "student_time_clash",
                    "board_id": board.id,
                    "placement_ids": [moved.id],
                    "score": 90000,
                }
            ]
        },
    )
    monkeypatch.setattr(
        tws,
        "preview_placement_slot_candidates",
        lambda placement_id: {
            "candidates": [
                {
                    "tone": "clean",
                    "day": "SUN",
                    "start": "08:00",
                    "end": "09:15",
                    "critical_count": 0,
                    "warning_count": 0,
                    "impact_improvement": 1,
                    "student_improvement": 0,
                    "critical_improvement": 1,
                    "warning_improvement": 0,
                    "primary_reason": "Clean improving target",
                }
            ]
        },
    )
    writes = _spy_teacher_writes(monkeypatch)

    result = tws.apply_bulk_safe_time_moves(sc.id, board_id=board.id)

    assert result["applied_count"] == 1, [row.get("reason") for row in result["skipped"]]
    assert _stored(sc) == rows and writes == []
    backstop = result["instructor_clash_backstop"]
    assert backstop["checked"] and backstop["count"] == 1


def _repair_fixture():
    """The repair suite's scenario, staffed: Dr A and Dr B may teach both courses."""
    from tests.test_timetable_repair_foundation import _fixture

    sc, placement = _fixture()
    sc.gender = "M"
    sc.save(update_fields=["gender"])
    a, b = instructor("Dr A"), instructor("Dr B")
    for code in ("AI101", "DS201"):
        link("AI", code, a)
        link("AI", code, b, role="co")
    return sc, placement


@pytest.mark.django_db
def test_i14_a_repair_apply_and_its_rollback_keep_the_rows(flags, monkeypatch) -> None:
    from core.services.timetable_repair import (
        analyse_timetable_repair,
        apply_approved_repair_candidate,
        approve_repair_candidate,
        rollback_repair_run,
    )

    sc, placement = _repair_fixture()
    sta.prepare_section_teachers(sc.id, phase="layout")
    rows = _stored(sc)
    assert len(rows) == 3
    created = analyse_timetable_repair(
        placement_id=placement.id, blocked_student_ids=[1002], limits={"max_candidates": 1}
    )
    run_id = created["run"]["id"]
    candidate_id = created["candidates"][0]["candidate_id"]
    approve_repair_candidate(run_id, candidate_id)
    before = (placement.day, placement.start_time)
    writes = _spy_teacher_writes(monkeypatch)
    reports = _spy_backstop(monkeypatch)

    apply_approved_repair_candidate(run_id, candidate_id)

    placement.refresh_from_db()
    assert (placement.day, placement.start_time) != before  # the repair moved it
    assert _stored(sc) == rows and writes == []
    assert reports["repair_candidate_apply"]["checked"] is True

    rollback_repair_run(run_id)

    placement.refresh_from_db()
    assert (placement.day, placement.start_time) == before
    assert _stored(sc) == rows and writes == []


@pytest.mark.django_db
def test_i14_a_global_plan_apply_and_its_rollback_keep_the_rows(flags, monkeypatch) -> None:
    from core.services.timetable_repair import (
        apply_global_repair_plan,
        approve_global_repair_plan,
        create_global_repair_plan,
        rollback_global_repair_plan,
    )
    from tests.test_timetable_repair_foundation import _make_actual_unresolved_scope

    sc, placement = _repair_fixture()
    _make_actual_unresolved_scope(sc, unresolved_courses=["AI101"])
    sta.prepare_section_teachers(sc.id, phase="layout")
    rows = _stored(sc)
    assert rows
    plan_id = create_global_repair_plan(
        scenario_id=sc.id,
        program="AI",
        nominal_term=1,
        max_placements=1,
        limits={"max_candidates": 1},
    )["plan"]["id"]
    approve_global_repair_plan(plan_id)
    layout = sorted(SectionPlacement.objects.filter(board__scenario=sc).values_list("id", "day"))
    writes = _spy_teacher_writes(monkeypatch)
    reports = _spy_backstop(monkeypatch)

    apply_global_repair_plan(plan_id)

    assert (
        sorted(SectionPlacement.objects.filter(board__scenario=sc).values_list("id", "day"))
        != layout
    )  # the plan moved a section
    assert _stored(sc) == rows and writes == []
    assert reports["repair_global_plan_apply"]["checked"] is True

    rollback_global_repair_plan(plan_id)

    assert _stored(sc) == rows and writes == []


def _create_fixture():
    """X (Dr A or Dr B) not placed yet; Dr A teaches Y every Sunday 08:00, and
    Dr B teaches Z on Monday — so by load and id alone Dr A would get X."""
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
    place(board, section(sc, y, "S1"), [("SUN", "08:00", "09:15")])
    place(board, section(sc, z, "S1"), [("MON", "11:00", "12:15")])
    return sc, board, x, a, b


@pytest.mark.django_db
def test_i15_a_planned_create_gets_the_teacher_it_does_not_clash_with(flags) -> None:
    from core.services.timetable_workspace import create_planned_section_placements

    sc, board, x, _a, _b = _create_fixture()

    create_planned_section_placements(
        board.id,
        course_code="AI301",
        course_key=x,
        course_name="X COURSE",
        section_label="S1",
        capacity=30,
        meetings=[{"day": "SUN", "start_time": "08:00", "end_time": "09:15", "room": "R1"}],
    )

    assert engine_rows(sc)[(x, "S1")] == "Dr B"
    assert set(
        TermSectionMeeting.objects.filter(term_section__course_key=x).values_list(
            "instructor", flat=True
        )
    ) == {"Dr B"}


@pytest.mark.django_db
def test_i15_a_manual_create_gets_the_teacher_it_does_not_clash_with(flags) -> None:
    import json

    sc, board, x, _a, _b = _create_fixture()
    ts = section(sc, x, "S1")
    TermSectionMeeting.objects.create(
        term_section=ts, day="SUN", start_time="08:00", end_time="09:15", room="R1"
    )

    response = _admin_client().post(
        "/ops/tw/placements/create/",
        data=json.dumps(
            {
                "board_id": board.id,
                "term_section_id": ts.id,
                "day": "SUN",
                "start_time": "08:00",
                "end_time": "09:15",
                "room": "R1",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 201, response.content
    assert engine_rows(sc)[(x, "S1")] == "Dr B"
    assert TermSectionMeeting.objects.get(term_section=ts).instructor == "Dr B"
