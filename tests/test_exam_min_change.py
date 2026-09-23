"""The minimum-change repair moves the fewest exams, and never the registrar's.

"Optimise from current" re-solves from an empty board: after a single drag it
moved a median of 77 of 167 courses on the real roster. This repair answers the
narrower question the registrar is actually asking after moving one exam.
"""

import itertools
import random

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


def _where(slot):
    return (DAYS[slot // 2], PERIOD_NAMES[slot % 2])


def _run_mode(monkeypatch, board, adj, source_placements, pinned=None, carried=None):
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
        # Mirror the real save: extra provenance is merged into the result.
        return {
            "qa": {"marker": True},
            "schedule": kwargs["schedule_raw"],
            "run_id": 7,
            **(kwargs.get("extra") or {}),
        }

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
        # Saved placements are (day, period): slot numbers shift whenever a
        # period is added, which once made the whole board look hand-moved.
        source_placements={
            code: place if isinstance(place, tuple) else _where(place)
            for code, place in source_placements.items()
        },
        carried_protection=carried,
    )
    return result, captured


def test_the_repair_never_runs_the_invigilator_post_pass(monkeypatch):
    """That pass relocates exams to flatten staff load - the opposite of this mode."""
    board = [_entry("A", 0), _entry("B", 0)]
    _, captured = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0})
    assert captured["rebalance_invigilators"] is False
    assert captured["rebuild_mode"] == "minimum_change_from_loaded"


def test_an_exam_the_registrar_dragged_is_protected_without_being_pinned(monkeypatch):
    """Z was at slot 1 in the saved run and has been dragged onto A.

    Z sorts after A on purpose: the tie-break keeps the earlier code in place,
    so with the exams the other way round this passed with no protection at all.
    """
    board = [_entry("A", 0), _entry("Z", 0)]
    result, captured = _run_mode(monkeypatch, board, _clash(("A", "Z")), {"A": 0, "Z": 1})
    placed = {e["course_code"]: e["slot_index"] for e in captured["schedule_raw"]}
    assert placed["Z"] == 0, "The registrar's drag must survive the repair."
    assert placed["A"] != 0
    assert [m["course_code"] for m in result["minimum_change"]["moves"]] == ["A"]


@pytest.mark.parametrize(
    "saved",
    [{"A": 0, "Z": ("OVERFLOW", "Extra-9")}, {"A": 0}],
    ids=["dragged-out-of-overflow", "absent-from-the-saved-run"],
)
def test_an_exam_placed_by_hand_from_off_the_board_is_the_registrars(monkeypatch, saved):
    """Overflow origins were dropped from the saved placements, and a missing
    entry then read as "not moved" - so the exam a registrar most often places
    by hand was the one the repair felt free to move. Z sorts last, so the
    tie-break cannot hide a lost protection."""
    board = [_entry("A", 0), _entry("Z", 0)]
    result, captured = _run_mode(monkeypatch, board, _clash(("A", "Z")), saved)
    placed = {e["course_code"]: e["slot_index"] for e in captured["schedule_raw"]}
    assert placed["Z"] == 0, "The exam placed by hand was moved"
    assert [m["course_code"] for m in result["minimum_change"]["moves"]] == ["A"]


def test_the_report_states_what_the_engine_actually_did(monkeypatch):
    """Every field, read against the board the view hands on to be saved.

    The page's own tests feed it hand-written reports, so this is the only
    place the server's side of that contract is held.
    """
    board = [_entry("A", 0), _entry("B", 0), _entry("F1", 1), _entry("F2", 2), _entry("F3", 3)]
    adj = _clash(("A", "B"), ("B", "F1"), ("B", "F2"), ("B", "F3"))
    saved = {"A": 3, "B": 0, "F1": 1, "F2": 2, "F3": 3}
    result, captured = _run_mode(monkeypatch, board, adj, saved)
    report = result["minimum_change"]
    placed = {e["course_code"]: e for e in captured["schedule_raw"]}
    assert len(report["moves"]) == 2
    for move in report["moves"]:
        entry = placed[move["course_code"]]
        assert move["to"] == {"day": entry["day"], "period": entry["period"]}
        assert move["to"] != move["from"]
    assert report["widened"] is True, "F-exams outside any clash stepped aside"
    assert report["proven_minimal"] is False
    assert report["status"] == "FEASIBLE"
    assert report["violations_after"] == 0
    assert "minimum_change" not in captured.get("qa", {})


def test_exams_already_in_overflow_are_counted_in_the_report(monkeypatch):
    parked = {**_entry("P", 0), "day": "OVERFLOW", "period": "Extra-9", "slot_index": 9}
    board = [_entry("A", 0), _entry("B", 1), parked]
    result, _ = _run_mode(monkeypatch, board, {}, {"A": 0, "B": 1, "P": ("OVERFLOW", "Extra-9")})
    assert result["minimum_change"]["already_overflow"] == 1


def test_a_pin_is_counted_but_never_carried_into_the_next_repair(monkeypatch):
    """Carrying a pin would keep an exam frozen after the registrar unpins it."""
    board = [_entry("A", 0), _entry("B", 0)]
    pin = [{"course_code": "B", "day": "Sun", "period": "08:00-10:00"}]
    result, _ = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0}, pinned=pin)
    assert result["minimum_change"]["protected_count"] == 1
    assert result["minimum_change_protected"] == []


@pytest.mark.parametrize(
    ("board", "adj", "saved"),
    [
        ([_entry("A", 0), _entry("B", 1)], {}, {"A": 0, "B": 1}),
        # Both exams were dragged, so the only clash is between the registrar's own.
        ([_entry("A", 0), _entry("B", 0)], _clash(("A", "B")), {"A": 1, "B": 2}),
    ],
    ids=["nothing-to-repair", "only-the-registrars-own-exams-clash"],
)
def test_a_repair_that_moves_nothing_saves_nothing(monkeypatch, board, adj, saved):
    """Saving the submitted board again added a duplicate run to the history
    and, with unsaved drags on it, saved them without the review Save asks for."""
    result, captured = _run_mode(monkeypatch, board, adj, saved)
    assert result["saved"] is False
    assert captured == {}, "The board was evaluated and saved anyway"
    assert result["minimum_change"]["moves"] == []


def test_a_repair_that_never_finds_a_board_saves_nothing(monkeypatch):
    from core.services import exam_min_change as engine

    monkeypatch.setattr(
        engine, "_solve", lambda model, objective, budget: (engine.cp_model.UNKNOWN, None)
    )
    board = [_entry("A", 0), _entry("B", 0)]
    result, captured = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0})
    assert result["saved"] is False
    assert captured == {}
    assert result["minimum_change"]["status"] == "UNKNOWN"


def test_a_pinned_exam_is_protected_even_if_it_never_moved(monkeypatch):
    board = [_entry("A", 0), _entry("B", 0)]
    pin = [{"course_code": "B", "day": "Sun", "period": "08:00-10:00"}]
    result, _ = _run_mode(monkeypatch, board, _clash(("A", "B")), {"A": 0, "B": 0}, pinned=pin)
    assert [m["course_code"] for m in result["minimum_change"]["moves"]] == ["A"]


def test_an_exam_with_nowhere_legal_to_go_is_written_as_overflow(monkeypatch):
    parked = {**_entry("P", 0), "day": "OVERFLOW", "period": "Extra-9", "slot_index": 9}
    board = [_entry("W", 0), _entry("X", 1), _entry("Y", 2), _entry("Z", 3), _entry("B", 0), parked]
    adj = _clash(("B", "W"), ("B", "X"), ("B", "Y"), ("B", "Z"))
    frozen = {"W": 0, "X": 1, "Y": 2, "Z": 3, "B": 0, "P": ("OVERFLOW", "Extra-9")}
    pins = [
        {"course_code": c, "day": _entry(c, s)["day"], "period": _entry(c, s)["period"]}
        for c, s in [("W", 0), ("X", 1), ("Y", 2), ("Z", 3)]
    ]
    result, captured = _run_mode(monkeypatch, board, adj, frozen, pinned=pins)
    b = next(e for e in captured["schedule_raw"] if e["course_code"] == "B")
    assert b["day"] == "OVERFLOW"
    assert b["slot_index"] > 9, "It must sit past every real slot and every parked exam."
    assert result["minimum_change"]["unseated"] == ["B"]


def test_provenance_is_stripped_before_it_can_reach_an_evaluator():
    """Regression: a new context key reached evaluate_exam_schedule and 500'd Check."""
    from core import exam_views

    context = {
        "days": ["Sun"],
        "source_input_fingerprint": "f" * 64,
        "source_placements": {},
        "source_repair_protected": [],
    }
    provenance = exam_views._split_provenance(context)
    assert set(provenance) == set(exam_views._PROVENANCE_KEYS)
    assert context == {"days": ["Sun"]}


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
    """Pins protection of dragged exams, and the ring that makes room.

    A was dragged onto B, and every other slot holds one of B's conflict
    neighbours, so B cannot move on its own. Unprotected, the cheapest repair
    is to move A straight back off the slot the registrar chose. Protected, A
    stays and a neighbour steps aside for B.

    This test once asserted that B went to OVERFLOW - locking in a defect. The
    damage-scoped model froze every undamaged exam, so it could not move F1
    out of the way, and it claimed that result was proven minimal. On the real
    167-course board that sent an exam to OVERFLOW on 162 of 4,170 drags that
    each had a legal repair of two to six moves.
    """
    adj = _clash(("A", "B"), ("B", "F1"), ("B", "F2"), ("B", "F3"))
    result = _repair({"A": 0, "B": 0, "F1": 1, "F2": 2, "F3": 3}, adj, protected={"A"})
    assert result.placements["A"] == 0, "The registrar's drag was undone."
    assert result.unseated == [], "A legal two-move repair exists; OVERFLOW is not needed."
    assert result.violations_after == 0
    assert result.widened is True
    assert len(result.moved) == 2
    # The floor is one move (B must leave A), and two were needed, so the
    # repair is legal but cannot claim to be the proven minimum.
    assert result.proven_minimal is False


def test_the_view_protects_a_dragged_exam_even_where_undoing_it_is_cheapest(monkeypatch):
    """The view derives protection by diffing against the saved run.

    A sat at slot 3 in the saved run and was dragged onto B at slot 0. B is
    boxed in by conflict neighbours, so an unprotected repair would simply move
    A back off the slot the registrar chose; protected, a neighbour makes room.
    """
    board = [_entry("A", 0), _entry("B", 0), _entry("F1", 1), _entry("F2", 2), _entry("F3", 3)]
    adj = _clash(("A", "B"), ("B", "F1"), ("B", "F2"), ("B", "F3"))
    saved = {"A": 3, "B": 0, "F1": 1, "F2": 2, "F3": 3}
    result, captured = _run_mode(monkeypatch, board, adj, saved)
    placed = {e["course_code"]: e for e in captured["schedule_raw"]}
    assert placed["A"]["slot_index"] == 0, "The registrar's drag was undone."
    assert placed["B"]["day"] != "OVERFLOW", "A neighbour could make room; OVERFLOW is not needed."
    assert result["minimum_change"]["violations_after"] == 0
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


# ── proving minimality, and not claiming it when it cannot be proven ─


def test_a_single_drag_onto_a_frozen_exam_is_proven_minimal():
    """The everyday case: one drag, and each clash forces its free partner to
    move. Those forced moves do not overlap, so the floor is exact and the UI
    may truthfully say "the fewest possible"."""
    adj = _clash(("P", "D1"), ("P", "D2"), ("P", "D3"))
    result = _repair({"P": 0, "D1": 0, "D2": 0, "D3": 0}, adj, protected={"P"}, slot_count=6)
    assert len(result.moved) == 3
    assert result.proven_minimal is True


def test_a_repair_the_scope_may_have_beaten_is_not_claimed_as_minimal():
    """C clashes with B, D and E in slot 0, and with X1-X3 in every other slot.

    Damage-scoping may only move B-E, so it moves B, D and E. Moving C and X1
    would have taken two. The repair is legal, but claiming it minimal was a
    lie the UI then repeated as "the fewest possible".
    """
    adj = _clash(("C", "B"), ("C", "D"), ("C", "E"), ("C", "X1"), ("C", "X2"), ("C", "X3"))
    result = _repair({"B": 0, "C": 0, "D": 0, "E": 0, "X1": 1, "X2": 2, "X3": 3}, adj)
    assert result.violations_after == 0
    assert result.proven_minimal is False


def test_every_tied_exam_is_swept_not_just_the_first_twelve():
    """Thirteen exams tied between two slots. A sweep capped at twelve left the
    thirteenth to the solver, and on heavy real edits that changed the board."""
    names = [f"B{index:02d}" for index in range(1, 14)]
    adj = _clash(*[("A", name) for name in names])
    result = _repair({"A": 5, **dict.fromkeys(names, 5)}, adj, protected={"A"}, slot_count=12)
    assert {result.placements[name] for name in names} == {4}, "Every tie resolves the same way"


def _brute_force(placements, adj, buckets, protected, slot_count):
    """Every legal placement of the damaged exams, ranked by the repair's own
    definition: fewest moves, then least distance, then the earliest slots
    read in code order. None when no legal placement exists."""
    damaged, _ = find_violations(placements, adj, buckets, PERIODS)
    scope = sorted(damaged - protected)
    best = None
    for slots in itertools.product(range(slot_count), repeat=len(scope)):
        board = {**placements, **dict(zip(scope, slots, strict=True))}
        if find_violations(board, adj, buckets, PERIODS)[1]:
            continue
        rank = (
            sum(board[course] != placements[course] for course in scope),
            sum(abs(board[course] - placements[course]) for course in scope),
            slots,
        )
        if best is None or rank < best[0]:
            best = (rank, board)
    return best and best[1]


def test_the_canonical_board_is_exactly_the_one_brute_force_picks():
    """Pins the tie-break sweep against its definition, not against itself.

    The sweep settles every exam up to the first one that can sit earlier in a
    single solve; done one exam at a time it cost 115 solves and 30 seconds on a
    real widened repair. Any shortcut in that bulk step - letting an exam move
    earlier by displacing one before it, or forgetting what is settled - shows
    up here as a different board.
    """
    rng = random.Random(20260923)
    names = ["A", "B", "C", "D", "E", "F"]
    compared = 0
    while compared < 120:
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.3])
        buckets = {("AI", 1): set(rng.sample(names, 2))} if rng.random() < 0.5 else None
        placements = {name: rng.randrange(6) for name in names}
        protected = {rng.choice(names)} if rng.random() < 0.5 else set()
        damaged, _ = find_violations(placements, adj, buckets, PERIODS)
        if not 2 <= len(damaged - protected) <= 4:
            continue
        expected = _brute_force(placements, adj, buckets, protected, 6)
        if expected is None:
            continue
        result = _repair(placements, adj, buckets, protected=protected, slot_count=6)
        assert result.widened is False
        assert result.placements == expected, (placements, sorted(adj.items()), buckets, protected)
        compared += 1


@pytest.mark.parametrize(
    ("placements", "slot_count", "seated", "unseated"),
    [
        ({"A": 0, "B": 0}, 1, {"A": 0}, ["B"]),
        ({"A": 0, "B": 0, "C": 0}, 2, {"A": 0, "B": 1}, ["C"]),
    ],
    ids=["one-slot", "two-slots"],
)
def test_which_exam_goes_to_overflow_is_decided_by_the_board(
    placements, slot_count, seated, unseated
):
    """Every exam clashes with every other, and there are more exams than slots.

    Which one leaves is a tie, settled like any other: in code order, each
    exam takes the earliest place it can, and OVERFLOW comes after every
    slot. The first sweep scored OVERFLOW as slot 0, so the earliest code was
    the one sent off the board.
    """
    adj = _clash(*itertools.combinations(sorted(placements), 2))
    result = _repair(placements, adj, slot_count=slot_count)
    assert result.unseated == unseated
    assert {code: result.placements[code] for code in seated} == seated
    assert result.violations_after == 0


def test_the_sweep_costs_one_solve_per_exam_it_changes_not_one_per_exam(monkeypatch):
    """Twenty exams clash with P at slot 2 and with Q at slot 1. Each could
    still sit at slot 0, which is earlier, but slot 3 is nearer - so no optimal
    repair puts any of them earlier than the solver already did. The sweep
    needs one solve to find that out, not twenty."""
    from core.services import exam_min_change as engine

    real = engine._solve
    calls = []

    def counted(model, objective, budget):
        if any(var.name.startswith("earlier_") for var in model.proto.variables):
            calls.append(1)
        return real(model, objective, budget)

    monkeypatch.setattr(engine, "_solve", counted)
    names = [f"D{index:02d}" for index in range(20)]
    adj = _clash(*[("P", name) for name in names], *[("Q", name) for name in names])
    result = _repair(
        {"P": 2, "Q": 1, **dict.fromkeys(names, 2)}, adj, protected={"P"}, slot_count=4
    )
    assert {result.placements[name] for name in names} == {3}
    assert len(calls) == 1


def test_only_the_exam_with_nowhere_to_go_is_sent_to_overflow():
    """B clashes with a frozen exam in every slot; C and D merely clash with each
    other. Leaving an exam costs no distance, so without the level that counts
    the unseated, C or D would go to OVERFLOW too instead of moving one period."""
    adj = _clash(("B", "W"), ("B", "X"), ("B", "Y"), ("B", "Z"), ("C", "D"))
    result = _repair(
        {"W": 0, "X": 1, "Y": 2, "Z": 3, "B": 0, "C": 1, "D": 1},
        adj,
        protected={"W", "X", "Y", "Z"},
    )
    assert result.unseated == ["B"]
    assert len(result.moved) == 1
    assert result.violations_after == 0


def test_a_bucket_mate_steps_to_another_day_to_make_room():
    """B can only go to Monday, where its bucket-mate M sits. The ring must
    include bucket-mates, not just conflict neighbours."""
    result = _repair(
        {"A": 0, "B": 0, "F": 1, "M": 2},
        _clash(("A", "B"), ("B", "F")),
        buckets={("AI", 1): {"B", "M"}},
        protected={"A", "F"},
    )
    assert result.unseated == []
    assert result.violations_after == 0
    assert result.widened is True


def test_a_second_ring_is_tried_before_anything_goes_to_overflow():
    """B needs F to move, and F needs G to move: two rings out."""
    adj = _clash(("A", "B"), ("B", "F"), ("B", "Y"), ("B", "Z"), ("F", "A"), ("F", "G"), ("F", "Z"))
    result = _repair(
        {"A": 0, "B": 0, "F": 1, "G": 2, "Y": 2, "Z": 3}, adj, protected={"A", "Y", "Z"}
    )
    assert result.unseated == []
    assert result.violations_after == 0
    assert len(result.moved) == 3


def test_a_repair_that_runs_out_of_work_moves_nothing_and_says_so(monkeypatch):
    from core.services import exam_min_change as engine

    monkeypatch.setattr(engine, "_REPAIR_WORK_BUDGET", 0.0)
    result = _repair({"A": 0, "B": 0}, _clash(("A", "B")))
    assert result.status == "UNKNOWN"
    assert result.proven_minimal is False
    assert result.moved == []
    assert result.placements == {"A": 0, "B": 0}


def test_every_solve_is_charged_to_the_repairs_budget():
    from ortools.sat.python import cp_model

    from core.services.exam_min_change import _Budget, _solve

    model = cp_model.CpModel()
    x = model.new_int_var(0, 50, "x")
    model.add(x >= 7)
    budget = _Budget(1.0)
    status, solver = _solve(model, x, budget)
    assert status == cp_model.OPTIMAL
    assert budget.remaining == pytest.approx(1.0 - solver.deterministic_time)
    assert budget.remaining < 1.0


def test_a_sliver_of_budget_buys_no_solve():
    """A solve given almost no work can hand back its hint having spent
    nothing, and the tie-break sweep would then ask again forever."""
    from core.services.exam_min_change import _DETERMINISTIC_LIMIT, _Budget

    assert _Budget(0.01).limit() == 0.0
    assert _Budget(0.5).limit() == 0.5
    assert _Budget(100.0).limit() == _DETERMINISTIC_LIMIT


@pytest.mark.parametrize(
    ("adj", "placements", "buckets", "damaged", "expected"),
    [
        (
            _clash(
                ("A", "B"), ("A", "C"), ("B", "C"), ("B", "D"), ("C", "D"), ("B", "W"), ("B", "X")
            ),
            {"A": 0, "B": 0, "C": 0, "D": 1, "W": 2, "X": 3},
            None,
            {"A", "B", "C"},
            # D is outside the damage and stays; A keeps its own slot; B has
            # nowhere left (A, D, W and X hold every slot); C takes slot 2.
            {"A": 0, "B": SLOTS, "C": 2, "D": 1},
        ),
        (
            # C's nearest clash-free slot is 1, but that is Sunday, and its
            # bucket-mate A already sits on Sunday: C must go to Monday.
            _clash(("A", "C")),
            {"A": 0, "C": 0},
            {("AI", 1): {"A", "C"}},
            {"A", "C"},
            {"A": 0, "C": 2},
        ),
    ],
    ids=["clashes", "bucket-days"],
)
def test_the_escape_starts_from_a_board_that_is_already_legal(
    adj, placements, buckets, damaged, expected
):
    """Every exam outside the damage keeps its slot; each damaged one takes the
    nearest legal slot, or OVERFLOW only when none is left."""
    from ortools.sat.python import cp_model

    from core.services.exam_min_change import _build_model, _hint_escape

    scope = sorted(expected)
    built = _build_model(scope, placements, adj, buckets, SLOTS, PERIODS, allow_unseated=True)
    _hint_escape(built, scope, damaged, placements, adj, buckets, PERIODS)

    solver = cp_model.CpSolver()
    solver.parameters.fix_variables_to_their_hinted_value = True
    status = solver.solve(built.model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE), "The start is not a legal board"
    assert {course: built.slot_of(solver, course) for course in scope} == expected


def test_the_escape_attempt_is_always_given_that_start(monkeypatch):
    from core.services import exam_min_change as engine

    real = engine._hint_escape
    calls = []

    def spy(built, *args):
        calls.append(bool(built.unseated))
        return real(built, *args)

    monkeypatch.setattr(engine, "_hint_escape", spy)
    adj = _clash(("B", "W"), ("B", "X"), ("B", "Y"), ("B", "Z"))
    _repair({"W": 0, "X": 1, "Y": 2, "Z": 3, "B": 0}, adj, protected={"W", "X", "Y", "Z"})
    assert calls == [True], "Only the escape model, and always the escape model"


def test_the_work_budget_is_shared_by_every_solve_in_a_repair(monkeypatch):
    """Each solve may spend at most what the whole repair has left."""
    from core.services import exam_min_change as engine

    real = engine._solve
    limits = []

    def watched(model, objective, budget):
        limits.append(budget.limit())
        return real(model, objective, budget)

    monkeypatch.setattr(engine, "_solve", watched)
    monkeypatch.setattr(engine, "_REPAIR_WORK_BUDGET", 0.3)
    _repair({"A": 0, "B": 0}, _clash(("A", "B")))
    assert limits and all(limit <= 0.3 for limit in limits)
    assert sum(limits) > 0


# ── the floor behind "the fewest possible" ──────────────────────────


def test_a_crowded_day_with_no_frozen_exam_needs_all_but_one_moved():
    result = _repair({"A": 0, "B": 0, "C": 1}, buckets={("AI", 1): {"A", "B", "C"}}, slot_count=6)
    assert len(result.moved) == 2
    assert result.proven_minimal is True


def test_a_day_held_by_frozen_exams_needs_only_the_free_one_moved():
    result = _repair(
        {"P": 0, "Q": 1, "B": 0}, buckets={("AI", 1): {"P", "Q", "B"}}, protected={"P", "Q"}
    )
    assert result.moved == ["B"]
    assert result.proven_minimal is True


def test_a_clash_between_two_frozen_exams_adds_nothing_to_the_floor():
    result = _repair(
        {"P": 0, "Q": 0, "A": 1, "B": 1}, _clash(("P", "Q"), ("A", "B")), protected={"P", "Q"}
    )
    assert len(result.moved) == 1
    assert result.violations_after == 1
    assert result.proven_minimal is True


def test_the_fewest_possible_is_never_claimed_falsely():
    """Against the true minimum, brute-forced over every unprotected exam.

    "The fewest possible" is shown to the registrar. The floor must never rise
    above what any legal repair needs, and a repair claimed minimal must match
    the true minimum exactly.
    """
    rng = random.Random(4471)
    names = ["A", "B", "C", "D", "E"]
    checked = claimed = 0
    while checked < 80:
        adj = _clash(*[pair for pair in itertools.combinations(names, 2) if rng.random() < 0.35])
        buckets = {("AI", 1): set(rng.sample(names, 3))} if rng.random() < 0.6 else None
        placements = {name: rng.randrange(4) for name in names}
        protected = set(rng.sample(names, rng.randrange(3)))
        if not find_violations(placements, adj, buckets, PERIODS)[1]:
            continue
        free = sorted(set(names) - protected)
        fewest = None
        for slots in itertools.product(range(4), repeat=len(free)):
            board = {**placements, **dict(zip(free, slots, strict=True))}
            if not find_violations(board, adj, buckets, PERIODS)[1]:
                moves = sum(board[name] != placements[name] for name in free)
                fewest = moves if fewest is None else min(fewest, moves)
        if fewest is None:
            continue
        floor = _engine_floor(placements, adj, buckets, protected)
        assert floor <= fewest, (placements, sorted(adj.items()), buckets, protected)
        result = _repair(placements, adj, buckets, protected=protected)
        if result.proven_minimal:
            claimed += 1
            assert len(result.moved) == fewest, (
                placements,
                sorted(adj.items()),
                buckets,
                protected,
            )
        checked += 1
    assert claimed, "No board exercised the claim"


def _engine_floor(placements, adj, buckets, protected):
    from core.services.exam_min_change import _move_lower_bound

    return _move_lower_bound(protected, placements, adj, buckets, PERIODS)


def test_a_repair_found_before_a_later_level_fails_is_kept(monkeypatch):
    """The first level found a legal board; the next ran out of work. That board
    used to be discarded and the repair reported as a failure.

    Every model's second level fails here, the escape model's included, so the
    OVERFLOW fallback cannot quietly cover for a discarded board."""
    from core.services import exam_min_change as engine

    real = engine._solve
    models: list = []
    calls: dict[int, int] = {}

    def flaky(model, objective, budget):
        models.append(model)  # keep every model alive, so no id() is reused
        calls[id(model)] = calls.get(id(model), 0) + 1
        status, solver = real(model, objective, budget)
        return (engine.cp_model.UNKNOWN, solver) if calls[id(model)] == 2 else (status, solver)

    monkeypatch.setattr(engine, "_solve", flaky)
    result = _repair({"A": 0, "B": 0}, _clash(("A", "B")), protected={"A"})
    assert result.violations_after == 0, "The board the first level found was thrown away"
    assert result.moved == ["B"]
    assert result.unseated == []


def test_a_scope_that_times_out_is_widened_before_anything_goes_to_overflow(monkeypatch):
    """Only INFEASIBLE used to widen. A timeout at the first scope went straight
    to the escape model over that same scope, which can time out just the same,
    leaving the clash in place although a wider scope solves at once.

    Every model that moves B alone "times out" here; once F1 may move too, it
    solves."""
    from core.services import exam_min_change as engine

    real = engine._solve

    def solve(model, objective, budget):
        status, solver = real(model, objective, budget)
        widened = any(var.name.startswith("y_F1_") for var in model.proto.variables)
        return (status, solver) if widened else (engine.cp_model.UNKNOWN, solver)

    monkeypatch.setattr(engine, "_solve", solve)
    adj = _clash(("A", "B"), ("B", "F1"))
    result = _repair({"A": 0, "B": 0, "F1": 2}, adj, protected={"A"})
    assert result.violations_after == 0, "A timeout was treated as the end of the search"
    assert result.unseated == []
    assert result.widened is True


def test_a_solver_that_times_out_still_reaches_for_the_escape(monkeypatch):
    """Only INFEASIBLE used to trigger the OVERFLOW fallback, so a hard-rules
    model that timed out left every clash in place though a board existed."""
    from core.services import exam_min_change as engine

    real_solve = engine._solve

    def solve(model, objective, budget):
        status, solver = real_solve(model, objective, budget)
        # Every model without an escape "times out"; the escape model solves.
        escapes = any("ovf_" in var.name for var in model.proto.variables)
        return (status, solver) if escapes else (engine.cp_model.UNKNOWN, solver)

    monkeypatch.setattr(engine, "_solve", solve)
    adj = _clash(("B", "W"), ("B", "X"))
    result = _repair({"W": 0, "X": 1, "B": 0}, adj, protected={"W", "X"})
    assert result.status != "UNKNOWN", "A timeout must not end the repair while an escape exists"
    assert result.unseated == ["B"] or result.violations_after == 0
