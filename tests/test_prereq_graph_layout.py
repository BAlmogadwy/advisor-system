"""Unit tests for the prerequisite dependency-graph layout helpers.

These back ``core.report_views._render_prereq_graph`` (the XLSX-export PNG) and
mirror the on-screen renderer in ``static/js/page-dashboard.js``: the vertical
axis is the declared programme term, credit-hour gates and undeclared courses
get an inferred row, and prereqs at/after their dependent are flagged.
"""

import pytest

from core.report_views import (
    _pg_build_edges,
    _pg_build_slots,
    _pg_gate_hours,
    _pg_order_slots,
    _pg_term_rows,
    _render_prereq_graph,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ── gate detection ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "code,expected",
    [
        ("144(HOURS)", "144"),
        ("144 (HOURS)", "144"),
        ("  80(hours) ", "80"),
        ("90(HOUR)", "90"),
        ("CS103", None),
        ("HOURS", None),
        ("(HOURS)", None),
    ],
)
def test_gate_hours(code, expected):
    assert _pg_gate_hours(code) == expected


# ── term rows ───────────────────────────────────────────────────────────────
def test_term_rows_respect_declared_terms():
    row, inferred = _pg_term_rows({"A", "B"}, {}, {"A": 5, "B": 6})
    assert row == {"A": 5, "B": 6}
    assert inferred == set()


def test_term_rows_infer_one_before_earliest_dependent():
    # G has no declared term; A and B (its dependents) sit at 5 and 6.
    # It must land at min(5, 6) - 1 == 4, NOT max - 1 == 5, and be flagged.
    row, inferred = _pg_term_rows({"A", "B", "G"}, {"G": ["A", "B"]}, {"A": 5, "B": 6})
    assert row["G"] == 4
    assert "G" in inferred


def test_term_rows_orphan_parks_above_the_floor():
    # Z has neither a declared term nor any dependent — it parks at floor - 1.
    row, inferred = _pg_term_rows({"A", "B", "Z"}, {}, {"A": 5, "B": 6})
    assert row["Z"] == 4  # floor (5) - 1
    assert "Z" in inferred


# ── edge warn classification ────────────────────────────────────────────────
def test_build_edges_warns_only_on_declared_same_or_later_term():
    # rows are (course, prerequisite); an edge points prereq -> course.
    row = {"A": 5, "B": 5, "C": 6, "G": 6}
    inferred = {"G"}
    edges = _pg_build_edges([("B", "A"), ("C", "A"), ("C", "G")], row, inferred)
    warn = {(e["f"], e["t"]): e["warn"] for e in edges}
    # A(5) -> B(5): both declared, same term -> unsatisfiable, warn.
    assert warn[("A", "B")] is True
    # A(5) -> C(6): earlier prereq -> fine, no warn.
    assert warn[("A", "C")] is False
    # G is inferred (no declared term): 6 >= 6 would trip the threshold, but an
    # inferred endpoint must never be reported as a declared-term conflict.
    assert warn[("G", "C")] is False


def test_render_is_deterministic_same_process():
    pytest.importorskip("PIL")
    rows = [("IS345", "80(HOURS)"), ("IS345", "IS251"), ("IS346", "IS345"), ("IS490", "IS345")]
    term_of = {"IS345": 7, "IS346": 8, "IS490": 8}
    a = _render_prereq_graph(rows, "IS", term_of).getvalue()
    b = _render_prereq_graph(rows, "IS", term_of).getvalue()
    assert a == b


# ── routing slots ───────────────────────────────────────────────────────────
def test_build_slots_adds_a_routing_point_per_crossed_band():
    edges = [{"f": "F", "t": "T", "warn": False}]
    slots, up, dn, chain = _pg_build_slots(edges, {"F": 3, "T": 6}, 3, 6)
    # F@3 -> T@6 crosses bands 4 and 5, so the chain is F, r4, r5, T.
    assert chain[0] == ["F", " d0@4", " d0@5", "T"]
    assert sum(1 for s in slots[4] if s["kind"] == "route") == 1
    assert sum(1 for s in slots[5] if s["kind"] == "route") == 1


def test_build_slots_skips_warning_edges():
    edges = [{"f": "F", "t": "T", "warn": True}]
    slots, up, dn, chain = _pg_build_slots(edges, {"F": 6, "T": 6}, 6, 6)
    assert chain[0] is None
    assert all(s["kind"] == "node" for band in slots.values() for s in band)


# ── crossing minimisation ───────────────────────────────────────────────────
def _order(row_map, edge_pairs):
    edges = [{"f": f, "t": t, "warn": row_map[f] >= row_map[t]} for f, t in edge_pairs]
    lo, hi = min(row_map.values()), max(row_map.values())
    slots, up, dn, chain = _pg_build_slots(edges, row_map, lo, hi)
    _pg_order_slots(slots, up, dn, edges, chain)
    return {k: [s["id"] for s in v] for k, v in slots.items()}


def test_order_slots_removes_an_obvious_crossing():
    # A@1->Y@2 and B@1->X@2 cross under the alphabetical seed [X, Y];
    # ordering must flip band 2 to [Y, X].
    ordered = _order({"A": 1, "B": 1, "X": 2, "Y": 2}, [("A", "Y"), ("B", "X")])
    assert ordered[1] == ["A", "B"]
    assert ordered[2] == ["Y", "X"]


def test_order_slots_is_deterministic():
    args = ({"A": 1, "B": 1, "X": 2, "Y": 2}, [("A", "Y"), ("B", "X")])
    assert _order(*args) == _order(*args)


# ── full PNG render ─────────────────────────────────────────────────────────
def test_render_returns_png_bytes():
    pytest.importorskip("PIL")
    rows = [("CS112", "CS111"), ("CS211", "CS112")]
    buf = _render_prereq_graph(rows, "CS", {"CS111": 3, "CS112": 4, "CS211": 5})
    assert buf is not None
    assert buf.getvalue()[:8] == PNG_SIG


def test_render_empty_rows_is_none():
    assert _render_prereq_graph([], "CS", {}) is None


def test_render_handles_warning_edge():
    # AI201 is a prereq of AI212 but both are declared in term 5 — unsatisfiable.
    # The bowed amber warning path must render without error.
    pytest.importorskip("PIL")
    buf = _render_prereq_graph([("AI212", "AI201")], "AI", {"AI201": 5, "AI212": 5})
    assert buf is not None
    assert buf.getvalue()[:8] == PNG_SIG


def test_render_falls_back_when_no_terms_declared():
    pytest.importorskip("PIL")
    buf = _render_prereq_graph([("B", "A")], "X", {})
    assert buf is not None
    assert buf.getvalue()[:8] == PNG_SIG


def test_render_places_gates_and_inferred_without_error():
    pytest.importorskip("PIL")
    # 80(HOURS) is a credit-hour gate; IS251 has no declared term here.
    rows = [("IS345", "80(HOURS)"), ("IS345", "IS251"), ("IS346", "IS345")]
    term_of = {"IS345": 7, "IS346": 8}
    buf = _render_prereq_graph(rows, "IS", term_of)
    assert buf is not None
    assert buf.getvalue()[:8] == PNG_SIG
