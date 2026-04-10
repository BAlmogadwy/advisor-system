"""
core/services/timetable_pair_feasibility.py
Pair-feasibility checker using max-flow for timetable hotspot detection.

For high-overlap course pairs (e.g. CS372 vs AI331 with 30 shared students),
checks whether the shared students can be distributed across sections such
that no student has overlapping meetings.

Uses a bipartite matching / max-flow approach:
  - Source → course_A sections (capacity = section capacity)
  - course_A section → course_B section edge ONLY if their times don't overlap
  - course_B sections → Sink (capacity = section capacity)
  - max_flow >= shared_count means the pair is feasible

This detects structural infeasibilities that soft penalties can't fix —
e.g. if ALL sections of CS372 overlap with ALL sections of AI331, then
30 students literally can't take both courses.
"""

from __future__ import annotations

from collections import defaultdict

from core.models import DeliveryBoard, ScenarioSectionBudget, SectionPlacement
from core.services.timetable_overlap import OverlapMatrix
from core.services.timetable_workspace import _time_mask


def _sections_overlap(meetings_a: list[dict], meetings_b: list[dict]) -> bool:
    """Check if two sections' meeting times overlap using bitmask AND."""
    mask_a = 0
    for m in meetings_a:
        mask_a |= _time_mask(m["day"], m["start_time"], m["end_time"])
    mask_b = 0
    for m in meetings_b:
        mask_b |= _time_mask(m["day"], m["start_time"], m["end_time"])
    return bool(mask_a & mask_b)


def _bfs_augment(graph: dict[str, dict[str, int]], source: str, sink: str) -> int:
    """BFS-based max-flow (Edmonds-Karp). Returns max flow value."""
    from collections import deque

    total_flow = 0
    while True:
        # BFS to find augmenting path
        parent: dict[str, str] = {}
        visited = {source}
        queue = deque([source])
        found = False
        while queue and not found:
            u = queue.popleft()
            for v, cap in graph.get(u, {}).items():
                if v not in visited and cap > 0:
                    visited.add(v)
                    parent[v] = u
                    if v == sink:
                        found = True
                        break
                    queue.append(v)
        if not found:
            break

        # Find bottleneck
        path_flow = float("inf")
        node = sink
        while node != source:
            prev = parent[node]
            path_flow = min(path_flow, graph[prev][node])
            node = prev

        # Update residual graph
        node = sink
        while node != source:
            prev = parent[node]
            graph[prev][node] -= path_flow
            if node not in graph:
                graph[node] = {}
            graph[node][prev] = graph.get(node, {}).get(prev, 0) + path_flow
            node = prev

        total_flow += path_flow

    return total_flow


def check_pair_feasibility(
    board_id: int,
    overlap_matrix: OverlapMatrix,
    threshold: int = 15,
) -> list[dict]:
    """Check all high-overlap course pairs on a board for section-assignment feasibility.

    For each pair with >= threshold shared students, builds a bipartite
    max-flow graph to determine if shared students can be distributed
    across non-overlapping section combinations.

    Parameters
    ----------
    board_id : int
        PK of the DeliveryBoard.
    overlap_matrix : OverlapMatrix
        Pre-computed course overlap matrix.
    threshold : int
        Minimum shared students to trigger feasibility check.

    Returns
    -------
    list[dict]
        List of infeasible/at-risk pairs::

            [{
                "course_a": str,
                "course_b": str,
                "shared_students": int,
                "max_assignable": int,
                "feasible": bool,
                "sections_a": int,
                "sections_b": int,
            }, ...]
    """
    try:
        board = DeliveryBoard.objects.select_related("scenario").get(id=board_id)
    except DeliveryBoard.DoesNotExist:
        return []

    # Get all placements grouped by course → section → meetings
    placements = list(
        SectionPlacement.objects.filter(board=board)
        .select_related("term_section")
        .order_by("term_section__course_code", "term_section__section")
    )

    # Build: course_code → {section_label: [{day, start_time, end_time}]}
    course_sections: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for p in placements:
        course_sections[p.term_section.course_code][p.term_section.section].append(
            {"day": p.day, "start_time": p.start_time, "end_time": p.end_time}
        )

    # Get section capacities
    budget_map = {
        b.course_code: b.max_per_section
        for b in ScenarioSectionBudget.objects.filter(
            scenario=board.scenario, programme_term=board.nominal_term
        )
    }

    results = []

    # Check each high-overlap pair
    for (code_a, code_b), shared in sorted(overlap_matrix.items(), key=lambda x: -x[1]):
        if shared < threshold:
            continue
        if code_a not in course_sections or code_b not in course_sections:
            continue

        secs_a = course_sections[code_a]
        secs_b = course_sections[code_b]

        if not secs_a or not secs_b:
            continue

        cap_a = budget_map.get(code_a, 40)
        cap_b = budget_map.get(code_b, 40)

        # Build max-flow graph
        # Nodes: SOURCE, a_S1, a_S2, ..., b_S1, b_S2, ..., SINK
        graph: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for sec_label_a, meetings_a in secs_a.items():
            node_a = f"a_{sec_label_a}"
            # Source → A section (capacity = min of section cap, shared count)
            graph["SOURCE"][node_a] = min(cap_a, shared)

            for sec_label_b, meetings_b in secs_b.items():
                node_b = f"b_{sec_label_b}"
                # A → B edge only if sections DON'T overlap in time
                if not _sections_overlap(meetings_a, meetings_b):
                    graph[node_a][node_b] = shared  # capacity = shared students

        for sec_label_b in secs_b:
            node_b = f"b_{sec_label_b}"
            # B section → Sink (capacity = min of section cap, shared count)
            graph[node_b]["SINK"] = min(cap_b, shared)

        # Run max-flow
        max_flow = _bfs_augment(graph, "SOURCE", "SINK")

        feasible = max_flow >= shared

        results.append(
            {
                "course_a": code_a,
                "course_b": code_b,
                "shared_students": shared,
                "max_assignable": max_flow,
                "feasible": feasible,
                "sections_a": len(secs_a),
                "sections_b": len(secs_b),
            }
        )

    return results


def find_infeasible_hotspots(board_id: int, overlap_matrix: OverlapMatrix) -> list[dict]:
    """Return only the infeasible pairs that need repair."""
    results = check_pair_feasibility(board_id, overlap_matrix)
    return [r for r in results if not r["feasible"]]
