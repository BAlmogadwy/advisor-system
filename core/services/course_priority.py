"""Canonical programme-level course dependency importance.

The score in this module describes a course's position in the programme's
prerequisite graph.  It is not student-specific readiness and it must not be
treated as a registration recommendation on its own.
"""

from collections import deque

from core.models import Prerequisite, ProgrammeRequirement
from core.services.student_helpers import normalize_code


def build_program_dependency_graph(program: str) -> dict[str, set[str]]:
    """Return the programme graph as ``prerequisite -> dependent courses``.

    Every declared programme requirement is seeded even when it has no edges.
    That distinction matters to callers: a plan leaf has a real importance
    score of ``0.0`` rather than being absent from the scoring universe.

    Prerequisite rows can also reference a course outside the declared plan, so
    both endpoints of every valid row are retained.  A comma-separated cell is
    interpreted as multiple prerequisite codes, matching the existing schema.
    """

    normalized_program = str(program or "").strip().upper()
    if not normalized_program:
        return {}

    plan_codes = ProgrammeRequirement.objects.filter(
        program=normalized_program,
    ).values_list("course_code", flat=True)
    graph: dict[str, set[str]] = {}
    for raw_code in plan_codes:
        code = normalize_code(raw_code)
        if code:
            graph.setdefault(code, set())

    rows = Prerequisite.objects.filter(
        program=normalized_program,
    ).values_list("course_code", "prerequisite_course_code")
    for raw_course, raw_prerequisites in rows:
        course = normalize_code(raw_course)
        if not course:
            continue
        graph.setdefault(course, set())
        if raw_prerequisites is None:
            continue
        for raw_prerequisite in str(raw_prerequisites).split(","):
            prerequisite = normalize_code(raw_prerequisite)
            if not prerequisite:
                continue
            graph.setdefault(prerequisite, set())
            graph[prerequisite].add(course)

    return graph


def dependency_distances(graph: dict[str, set[str]], source: str) -> dict[str, int]:
    """Return shortest downstream distances from ``source`` in ``graph``."""

    distances = {source: 0}
    queue: deque[str] = deque([source])
    while queue:
        current = queue.popleft()
        for dependent in graph.get(current, set()):
            if dependent in distances:
                continue
            distances[dependent] = distances[current] + 1
            queue.append(dependent)
    return distances


def compute_downstream_importance_scores(
    graph: dict[str, set[str]],
    discount: str = "1_over_d",
) -> dict[str, float]:
    """Score each graph node by all courses reachable downstream.

    ``none`` gives every reachable dependent weight 1. ``half_power_d`` gives
    distance ``d`` weight ``0.5 ** (d - 1)``.  ``1_over_d`` gives weight
    ``1 / d`` and remains the fallback for unknown legacy values, preserving
    the historical high-priority report behavior.
    """

    scores: dict[str, float] = {}
    for node in graph:
        score = 0.0
        for distance in dependency_distances(graph, node).values():
            if distance == 0:
                continue
            if discount == "none":
                score += 1.0
            elif discount == "half_power_d":
                score += 0.5 ** (distance - 1)
            else:
                score += 1.0 / distance
        scores[node] = score
    return scores


def program_downstream_importance_scores(
    program: str,
    discount: str = "1_over_d",
) -> dict[str, float]:
    """Build and score one programme's prerequisite dependency graph."""

    return compute_downstream_importance_scores(
        build_program_dependency_graph(program),
        discount=discount,
    )
