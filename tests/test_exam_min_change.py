"""The minimum-change repair moves the fewest exams, and never the registrar's.

"Optimise from current" re-solves from an empty board: after a single drag it
moved a median of 77 of 167 courses on the real roster. This repair answers the
narrower question the registrar is actually asking after moving one exam.
"""

import pytest

from core.services.exam_min_change import find_violations, repair_minimum_change

# Two days of two periods: slots 0,1 are Sunday, 2,3 are Monday.
PERIODS = 2
SLOTS = 4


def _repair(placements, adj=None, buckets=None, protected=None, slot_count=SLOTS):
    return repair_minimum_change(
        placements=placements,
        adj=adj or {},
        slot_count=slot_count,
        periods_per_day=PERIODS,
        plan_term_buckets=buckets,
        protected=protected,
    )


def _clash(*pairs):
    adj: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        adj.setdefault(a, {})[b] = 1
        adj.setdefault(b, {})[a] = 1
    return adj


# ── finding the damage ──────────────────────────────────────────────


def test_a_shared_slot_between_conflicting_courses_is_a_breach():
    damaged, breaches = find_violations({"A": 0, "B": 0}, _clash(("A", "B")), None, PERIODS)
    assert damaged == {"A", "B"}
    assert breaches == 1


def test_conflicting_courses_in_different_slots_are_fine():
    assert find_violations({"A": 0, "B": 1}, _clash(("A", "B")), None, PERIODS) == (set(), 0)


def test_two_bucket_mates_on_one_day_are_a_breach_even_in_different_periods():
    damaged, breaches = find_violations({"A": 0, "B": 1}, {}, {("AI", 1): {"A", "B"}}, PERIODS)
    assert damaged == {"A", "B"}
    assert breaches == 1


def test_a_breach_is_counted_once_not_once_per_direction():
    _, breaches = find_violations({"A": 0, "B": 0}, _clash(("A", "B")), None, PERIODS)
    assert breaches == 1


# ── repairing it ────────────────────────────────────────────────────


def test_a_legal_board_is_left_exactly_as_it_is():
    result = _repair({"A": 0, "B": 1}, _clash(("A", "B")))
    assert result.moved == []
    assert result.placements == {"A": 0, "B": 1}
    assert result.violations_before == 0


def test_a_clash_is_repaired_by_moving_one_exam_not_both():
    result = _repair({"A": 0, "B": 0}, _clash(("A", "B")))
    assert len(result.moved) == 1, "One move clears a single clash; moving both is not minimal."
    assert result.violations_after == 0
    assert result.proven_minimal is True


def test_the_exam_the_registrar_moved_is_never_the_one_that_moves_back():
    """Without this, undoing the registrar's drag is a legal one-move repair."""
    result = _repair({"A": 0, "B": 0}, _clash(("A", "B")), protected={"A"})
    assert result.moved == ["B"]
    assert result.placements["A"] == 0
    assert result.violations_after == 0


def test_an_exam_uninvolved_in_any_clash_never_moves():
    """Only damaged exams are variables; everything else is a constant."""
    adj = _clash(("A", "B"))
    result = _repair({"A": 0, "B": 0, "C": 1, "D": 2}, adj, protected={"A"})
    assert result.placements["C"] == 1
    assert result.placements["D"] == 2
    assert result.movable == 1, "Only B was implicated and unprotected."


def test_a_nearer_slot_is_preferred_over_a_distant_one():
    """A one-period nudge must not score like a move to another day."""
    result = _repair({"A": 0, "B": 0}, _clash(("A", "B")), protected={"A"}, slot_count=4)
    assert result.placements["B"] == 1


def test_a_bucket_day_breach_is_repaired_onto_another_day():
    buckets = {("AI", 1): {"A", "B"}}
    result = _repair({"A": 0, "B": 1}, buckets=buckets, protected={"A"})
    assert result.moved == ["B"]
    assert result.placements["B"] // PERIODS != 0, "Still on Sunday alongside its bucket-mate."
    assert result.violations_after == 0


def test_a_repair_does_not_create_a_new_clash_with_a_frozen_neighbour():
    """B may not escape A by landing on C, which it also conflicts with."""
    adj = _clash(("A", "B"), ("B", "C"))
    result = _repair({"A": 0, "B": 0, "C": 1}, adj, protected={"A"})
    assert result.placements["B"] not in (0, 1)
    assert result.violations_after == 0


def test_the_same_board_always_gets_the_same_repair():
    """Equal-cost optima must resolve to one board, not whichever the solver found."""
    adj = _clash(("A", "B"), ("A", "C"))
    first = _repair({"A": 0, "B": 0, "C": 0}, adj, protected={"A"})
    for _ in range(3):
        again = _repair({"A": 0, "B": 0, "C": 0}, adj, protected={"A"})
        assert again.placements == first.placements


# ── when a repair is impossible ─────────────────────────────────────


def test_a_clash_between_two_frozen_exams_is_reported_not_papered_over():
    """Both were placed by the registrar; moving an innocent third exam cannot help."""
    result = _repair({"A": 0, "B": 0, "C": 1}, _clash(("A", "B")), protected={"A", "B"})
    assert result.moved == []
    assert result.violations_after == 1
    assert result.placements["C"] == 1


def test_an_exam_with_no_legal_slot_goes_to_overflow_not_back_onto_the_clash():
    """B conflicts with a frozen exam in every slot there is."""
    adj = _clash(("B", "W"), ("B", "X"), ("B", "Y"), ("B", "Z"))
    result = _repair(
        {"W": 0, "X": 1, "Y": 2, "Z": 3, "B": 0},
        adj,
        protected={"W", "X", "Y", "Z"},
    )
    assert result.unseated == ["B"]
    assert "B" not in result.placements, "Leaving B on slot 0 would re-publish the clash."
    assert result.violations_after == 0


def test_a_repair_needs_somewhere_to_put_things():
    with pytest.raises(ValueError):
        _repair({"A": 0}, slot_count=0)


# ── the server mode ─────────────────────────────────────────────────

DAYS = ["Sun", "Mon"]
PERIOD_NAMES = ["08:00-10:00", "13:00-15:00"]


def _entry(code, slot):
    return {
        "course_code": code,
        "course_identity": code,
        "course_name": f"{code} name",
        "day": DAYS[slot // 2],
        "period": PERIOD_NAMES[slot % 2],
        "slot_index": slot,
    }


def _run_mode(monkeypatch, board, adj, source_placements, pinned=None):
    """Drive the view's minimum-change path with controlled solver inputs."""
    from core import exam_views

    captured = {}

    def fake_inputs(base_entries, days, periods, programs, sections, threshold):
        return exam_views._LoadedSolverInputs(
            meta_by_course={e["course_code"]: e for e in base_entries},
            course_list=sorted(e["course_code"] for e in base_entries),
            enrolled_sets={},
            adj=adj,
            plan_term_buckets={},
            course_buckets={},
            credit_map={},
            slots=[
                {"index": i, "day": d, "period": p}
                for i, (d, p) in enumerate((d, p) for d in days for p in periods)
            ],
        )

    def fake_rebuild(**kwargs):
        captured.update(kwargs)
        return {"qa": {"marker": True}, "schedule": kwargs["schedule_raw"], "run_id": 7}

    monkeypatch.setattr(exam_views, "_loaded_solver_inputs", fake_inputs)
    monkeypatch.setattr(exam_views, "_rebuild_loaded_schedule", fake_rebuild)
    result = exam_views._minimum_change_schedule(
        label="Repair",
        days=DAYS,
        periods=PERIOD_NAMES,
        max_per_day=2,
        schedule_raw=board,
        selected_courses=[e["course_code"] for e in board],
        pinned=pinned or [],
        assign_rooms=False,
        seed=None,
        thin_conflict_threshold=0,
        source_placements=source_placements,
    )
    return result, captured


def test_the_repair_never_runs_the_invigilator_post_pass(monkeypatch):
    """That pass relocates exams to flatten staff load - the opposite of this mode."""
    board = [_entry("A", 0), _entry("B", 0)]
    _, captured = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0})
    assert captured["rebalance_invigilators"] is False
    assert captured["rebuild_mode"] == "minimum_change_from_loaded"


def test_an_exam_the_registrar_dragged_is_protected_without_being_pinned(monkeypatch):
    """A was at slot 1 in the saved run and has been dragged onto B."""
    board = [_entry("A", 0), _entry("B", 0)]
    result, captured = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 1, "B": 0})
    placed = {e["course_code"]: e["slot_index"] for e in captured["schedule_raw"]}
    assert placed["A"] == 0, "The registrar's drag must survive the repair."
    assert placed["B"] != 0
    assert [m["course_code"] for m in result["minimum_change"]["moves"]] == ["B"]


def test_a_pinned_exam_is_protected_even_if_it_never_moved(monkeypatch):
    board = [_entry("A", 0), _entry("B", 0)]
    pin = [{"course_code": "B", "day": "Sun", "period": "08:00-10:00"}]
    result, _ = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0}, pinned=pin)
    assert [m["course_code"] for m in result["minimum_change"]["moves"]] == ["A"]


def test_the_move_report_is_a_response_key_never_part_of_qa(monkeypatch):
    """The frontend compares qa between Build and a fixed-time Check.

    A Check cannot reproduce how a board was repaired, so a repair report in qa
    would mark every saved run as changed and gate XLSX export.
    """
    board = [_entry("A", 0), _entry("B", 0)]
    result, _ = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 1, "B": 0})
    assert "minimum_change" in result
    assert "minimum_change" not in result["qa"]
    move = result["minimum_change"]["moves"][0]
    assert move["from"] == {"day": "Sun", "period": "08:00-10:00"}
    assert result["minimum_change"]["violations_after"] == 0


def test_an_exam_with_nowhere_legal_to_go_is_written_as_overflow(monkeypatch):
    board = [_entry("W", 0), _entry("X", 1), _entry("Y", 2), _entry("Z", 3), _entry("B", 0)]
    adj = _clash(("B", "W"), ("B", "X"), ("B", "Y"), ("B", "Z"))
    frozen = {"W": 0, "X": 1, "Y": 2, "Z": 3, "B": 0}
    pins = [
        {"course_code": c, "day": _entry(c, s)["day"], "period": _entry(c, s)["period"]}
        for c, s in [("W", 0), ("X", 1), ("Y", 2), ("Z", 3)]
    ]
    result, captured = _run_mode(monkeypatch, board, adj, frozen, pinned=pins)
    b = next(e for e in captured["schedule_raw"] if e["course_code"] == "B")
    assert b["day"] == "OVERFLOW"
    assert b["slot_index"] >= SLOTS, "Overflow indices must sit past every real slot."
    assert result["minimum_change"]["unseated"] == ["B"]


def test_provenance_is_stripped_before_it_can_reach_an_evaluator():
    """Regression: a new context key reached evaluate_exam_schedule and 500'd Check."""
    from core import exam_views

    context = {"days": ["Sun"], "source_input_fingerprint": "f" * 64, "source_placements": {}}
    provenance = exam_views._split_provenance(context)
    assert set(provenance) == set(exam_views._PROVENANCE_KEYS)
    assert context == {"days": ["Sun"]}


@pytest.mark.parametrize("view", ["exam_timetable_build_view", "exam_timetable_draft_impact_view"])
def test_every_consumer_of_the_loaded_context_strips_its_provenance(view):
    """Popping each key by hand at each call site is how the 500 happened."""
    import inspect

    from core import exam_views

    source = inspect.getsource(getattr(exam_views, view))
    assert "_loaded_request_context(" in source
    assert "_split_provenance(" in source
    assert 'context.pop("source_' not in source


# ── tie-breaking, and where "nearest" and "lowest" part ways ────────


def test_the_nearest_slot_wins_even_when_it_is_not_the_lowest_numbered():
    """Pins the distance objective.

    With A frozen at slot 3, B's nearest escapes are 2 and 4, while the lowest
    free slot is 0 - three periods away. Minimising moves alone treats 0 and 2
    as equal, and the tie-break sweep would then pick 0.
    """
    result = _repair({"A": 3, "B": 3}, _clash(("A", "B")), protected={"A"}, slot_count=6)
    assert abs(result.placements["B"] - 3) == 1


def test_between_equally_near_slots_the_earlier_one_is_chosen():
    """Pins the canonical sweep: slots 2 and 4 are tied, so the result must not
    depend on which optimum the solver happened to reach first."""
    result = _repair({"A": 3, "B": 3}, _clash(("A", "B")), protected={"A"}, slot_count=6)
    assert result.placements["B"] == 2


def test_the_repair_moves_an_innocent_exam_before_it_undoes_the_registrar():
    """Pins protection of dragged exams.

    A was dragged onto B. B has frozen neighbours in every other slot, so it
    cannot move. Unprotected, the cheapest repair is to move A straight back
    off the slot the registrar chose; protected, A must stay and B is the one
    that cannot be seated.
    """
    adj = _clash(("A", "B"), ("B", "F1"), ("B", "F2"), ("B", "F3"))
    result = _repair({"A": 0, "B": 0, "F1": 1, "F2": 2, "F3": 3}, adj, protected={"A"})
    assert result.placements["A"] == 0, "The registrar's drag was undone."
    assert result.unseated == ["B"]


def test_the_view_protects_a_dragged_exam_even_where_undoing_it_is_cheapest(monkeypatch):
    """The view derives protection by diffing against the saved run.

    A sat at slot 3 in the saved run and was dragged onto B at slot 0. B is
    boxed in by frozen neighbours, so an unprotected repair would simply move A
    back off the slot the registrar chose.
    """
    board = [_entry("A", 0), _entry("B", 0), _entry("F1", 1), _entry("F2", 2), _entry("F3", 3)]
    adj = _clash(("A", "B"), ("B", "F1"), ("B", "F2"), ("B", "F3"))
    saved = {"A": 3, "B": 0, "F1": 1, "F2": 2, "F3": 3}
    result, captured = _run_mode(monkeypatch, board, adj, saved)
    placed = {e["course_code"]: e for e in captured["schedule_raw"]}
    assert placed["A"]["slot_index"] == 0, "The registrar's drag was undone."
    assert placed["B"]["day"] == "OVERFLOW"
    assert result["minimum_change"]["protected_count"] == 1


def test_ties_resolve_the_same_way_whatever_the_solver_reaches_first():
    """Pins the canonical sweep with a board where it actually matters.

    B's nearest legal slots are 2 and 4, a tie. Without the sweep, OR-Tools
    9.15 returns 4 on this board - and across 1,273 random boards the sweep
    changed the answer on 538 of them, so an unswept repair would hand the
    registrar a different board from one solver version to the next.
    """
    adj = _clash(("A", "C"), ("B", "C"))
    result = _repair({"A": 0, "B": 3, "C": 3}, adj, slot_count=10)
    assert result.placements["B"] == 2
