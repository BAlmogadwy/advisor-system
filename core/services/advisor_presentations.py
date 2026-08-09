"""Whitelisted, student-visible companions to durable adviser answers.

The prose remains the audit record of what was said. Timetable alternatives are
materially easier to use as a small interface, so this module creates a view model
from an already-scoped tool result. It is deliberately not a generic metadata bag:
unknown fields disappear, and the browser never receives raw tool results,
database ids, model traces, seat counts, or internal reason codes.
"""

from __future__ import annotations

from typing import Any

KIND_TIMETABLE = "timetable_proposals"
KIND_GRADUATION = "graduation_scenario"
_MAX_ALTERNATIVES = 9
_MAX_COURSES = 20
_MAX_MEETINGS = 80
_MAX_GRAPH_NODES = 160
_MAX_GRAPH_EDGES = 360
_MAX_UNRESOLVED = 80
_MAX_SIMULATED_TERMS = 18


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _items(value: Any, limit: int) -> list[Any] | tuple[Any, ...]:
    """Bound a JSON array and reject lookalike strings/objects safely."""
    if not isinstance(value, list | tuple):
        return ()
    return value[:limit]


def _course_rows(value: Any, limit: int = _MAX_COURSES) -> list[dict[str, Any]]:
    rows = []
    for row in _items(value, limit):
        if not isinstance(row, dict):
            continue
        code = _text(row.get("code") or row.get("course_code"), 32).upper()
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "name": _text(row.get("name") or row.get("course_name")),
                "credits": _number(row.get("credits")),
            }
        )
    return rows


def _normalise_graduation_presentation(payload: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the graph snapshot rendered below a graduation answer."""
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        return {}

    nodes: list[str] = []
    for value in _items(graph.get("extraNodes"), _MAX_GRAPH_NODES):
        code = _text(value, 32).upper()
        if code and code not in nodes:
            nodes.append(code)

    items = []
    for row in _items(graph.get("items"), _MAX_GRAPH_EDGES):
        if not isinstance(row, dict):
            continue
        course = _text(row.get("course_code"), 32).upper()
        prereq = _text(row.get("prerequisite_course_code"), 32).upper()
        if not course or not prereq:
            continue
        missing = [code for code in (course, prereq) if code not in nodes]
        if len(nodes) + len(missing) > _MAX_GRAPH_NODES:
            continue
        nodes.extend(missing)
        items.append(
            {
                "course_code": course,
                "prerequisite_course_code": prereq,
            }
        )

    if not nodes:
        return {}

    raw_terms = graph.get("termOf") if isinstance(graph.get("termOf"), dict) else {}
    raw_names = graph.get("nameOf") if isinstance(graph.get("nameOf"), dict) else {}
    raw_status = graph.get("statusOf") if isinstance(graph.get("statusOf"), dict) else {}
    statuses = {"passed", "studying", "open", "locked"}
    term_of: dict[str, int] = {}
    name_of: dict[str, str] = {}
    status_of: dict[str, str] = {}
    for code in nodes:
        term_value = _optional_number(raw_terms.get(code))
        if term_value is not None and term_value <= _MAX_SIMULATED_TERMS + 2:
            term_of[code] = term_value
        name = _text(raw_names.get(code))
        if name:
            name_of[code] = name
        status = _text(raw_status.get(code), 16).lower()
        if status in statuses:
            status_of[code] = status

    labels = {}
    raw_labels = payload.get("band_labels")
    if isinstance(raw_labels, dict):
        for key, value in raw_labels.items():
            try:
                band = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= band <= _MAX_SIMULATED_TERMS + 2:
                label = _text(value, 80)
                if label:
                    labels[str(band)] = label

    unresolved = []
    for row in _items(payload.get("unresolved_requirements"), _MAX_UNRESOLVED):
        if not isinstance(row, dict):
            continue
        code = _text(row.get("code"), 32).upper()
        if not code:
            continue
        gate = row.get("credit_hour_gate")
        safe_gate = {}
        if isinstance(gate, dict):
            safe_gate = {
                "required": _number(gate.get("required")),
                "effective": _number(gate.get("effective_in_scenario") or gate.get("effective")),
                "remaining": _number(gate.get("remaining")),
            }
        unresolved.append(
            {
                "code": code,
                "name": _text(row.get("name")),
                "missing_prerequisites": [
                    _text(value, 32).upper()
                    for value in _items(
                        row.get("missing_course_prerequisites") or row.get("missing_prerequisites"),
                        20,
                    )
                    if _text(value, 32)
                ],
                "credit_hour_gate": safe_gate,
            }
        )

    return {
        "kind": KIND_GRADUATION,
        "program": _text(payload.get("program"), 32),
        "planning_term": _text(payload.get("planning_term"), 24),
        "simulation_completed": payload.get("simulation_completed") is True,
        "estimated_terms_including_current": _optional_number(
            payload.get("estimated_terms_including_current")
        ),
        "lower_bound_terms_including_current": _number(
            payload.get("lower_bound_terms_including_current")
        ),
        "max_credits_per_term": _number(payload.get("max_credits_per_term")),
        "graph": {
            "items": items,
            "termOf": term_of,
            "nameOf": name_of,
            "statusOf": status_of,
            "extraNodes": nodes,
        },
        "band_labels": labels,
        "unresolved_requirements": unresolved,
        "removed_current_courses": _course_rows(payload.get("removed_current_courses")),
        "added_current_courses": _course_rows(payload.get("added_current_courses")),
        # Server-owned boundary: this is never an actionable or saved plan.
        "read_only": True,
    }


def normalise_presentation(payload: Any) -> dict[str, Any]:
    """Return the only structured adviser payload the student UI may render."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("kind") == KIND_GRADUATION:
        return _normalise_graduation_presentation(payload)
    if payload.get("kind") != KIND_TIMETABLE:
        return {}

    mode = _text(payload.get("mode"), 32)
    current_sections = []
    for row in _items(payload.get("current_sections"), _MAX_COURSES):
        if not isinstance(row, dict):
            continue
        current_sections.append(
            {
                "course_code": _text(row.get("course_code"), 32),
                "course_name": _text(row.get("course_name")),
                "section": _text(row.get("section"), 32),
                "credits": _number(row.get("credits")),
                "meetings": [
                    _text(value, 80) for value in _items(row.get("meetings"), _MAX_MEETINGS)
                ],
            }
        )

    alternatives = []
    for row in _items(payload.get("alternatives"), _MAX_ALTERNATIVES):
        if not isinstance(row, dict):
            continue
        courses = [
            {
                "course_code": _text(course.get("course_code"), 32),
                "course_name": _text(course.get("course_name")),
                "section": _text(course.get("section"), 32),
                "credits": _number(course.get("credits")),
            }
            for course in _items(row.get("courses"), _MAX_COURSES)
            if isinstance(course, dict)
        ]
        meetings = [
            {
                "course_code": _text(meeting.get("course_code"), 32),
                "course_name": _text(meeting.get("course_name")),
                "section": _text(meeting.get("section"), 32),
                "day": _text(meeting.get("day"), 12).upper(),
                "start": _text(meeting.get("start"), 12),
                "end": _text(meeting.get("end"), 12),
            }
            for meeting in _items(row.get("meetings"), _MAX_MEETINGS)
            if isinstance(meeting, dict)
        ]
        unplaced = [
            {
                "course_code": _text(course.get("course_code"), 32),
                "course_name": _text(course.get("course_name")),
                # Natural-language display reason only. `reason_code` is an
                # implementation detail and is intentionally not copied.
                "reason": _text(course.get("reason"), 500),
            }
            for course in _items(row.get("unplaced_courses"), _MAX_COURSES)
            if isinstance(course, dict)
        ]
        alternatives.append(
            {
                "planner_options": [
                    _text(name, 8).upper()
                    for name in _items(row.get("planner_options"), _MAX_ALTERNATIVES)
                    if _text(name, 8)
                ],
                "scheduled_courses": _number(row.get("scheduled_courses")),
                "target_courses": _number(row.get("target_courses")),
                "proposed_credit_hours": _number(row.get("proposed_credit_hours")),
                "total_credit_hours": _number(row.get("total_credit_hours")),
                "days_on_campus": _number(row.get("days_on_campus")),
                "days": [
                    _text(day, 12).upper() for day in _items(row.get("days"), 7) if _text(day, 12)
                ],
                "earliest_start": _text(row.get("earliest_start"), 12),
                "latest_end": _text(row.get("latest_end"), 12),
                "courses": courses,
                "meetings": meetings,
                "unplaced_courses": unplaced,
            }
        )

    # The solver returns the student's baseline in from-scratch mode for
    # comparison and provenance. Those sections are not fixed or retained, so
    # presenting them under "Current retained sections" is materially false.
    if mode == "from_scratch":
        current_sections = []

    if not alternatives and not current_sections:
        return {}
    return {
        "kind": KIND_TIMETABLE,
        "planning_term": _text(payload.get("planning_term"), 24),
        "mode": mode,
        "current_credit_hours": _number(payload.get("current_credit_hours")),
        "credit_ceiling": _number(payload.get("credit_ceiling")),
        "current_sections": current_sections,
        "alternatives": alternatives,
        "no_additional_courses": payload.get("no_additional_courses") is True,
        # Server-owned constants, never copied from a model or client.
        "can_save": False,
        "can_register": False,
    }


def timetable_presentation_from_tool_results(
    tool_results: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """Project the newest successful timetable build into its durable view model."""
    for result in reversed(list(tool_results or [])):
        if not isinstance(result, dict):
            continue
        if result.get("tool") != "build_timetable_proposal" or not result.get("ok"):
            continue
        return normalise_presentation(
            {
                "kind": KIND_TIMETABLE,
                "planning_term": result.get("planning_term"),
                "mode": result.get("mode"),
                "current_credit_hours": result.get("current_credit_hours"),
                "credit_ceiling": result.get("credit_ceiling"),
                "current_sections": result.get("current_sections"),
                "alternatives": result.get("alternatives"),
                "no_additional_courses": result.get("no_additional_courses"),
            }
        )
    return {}


def graduation_presentation_from_tool_results(
    tool_results: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """Turn one verified graduation simulation into the existing graph language.

    A replacement search has several candidate scenarios and therefore has no
    single honest map. It remains prose/timetable evidence until the student
    chooses one replacement and runs that explicit scenario.
    """
    for result in reversed(list(tool_results or [])):
        if not isinstance(result, dict):
            continue
        if result.get("tool") != "graduation_progress" or not result.get("ok"):
            continue

        what_if = result.get("what_if")
        if isinstance(what_if, dict):
            if what_if.get("valid") is not True:
                return {}
            if what_if.get("mode") == "replacement_search":
                return {}

        source_graph = result.get("scenario_graph")
        if not isinstance(source_graph, dict):
            return {}

        raw_status = (
            source_graph.get("statusOf") if isinstance(source_graph.get("statusOf"), dict) else {}
        )
        raw_names = (
            source_graph.get("nameOf") if isinstance(source_graph.get("nameOf"), dict) else {}
        )
        passed = {
            _text(code, 32).upper()
            for code, status in raw_status.items()
            if str(status).lower() == "passed" and _text(code, 32)
        }
        current_rows = _course_rows(result.get("current_courses_assumed_passed"))
        current = {row["code"] for row in current_rows}

        future: dict[str, int] = {}
        future_names: dict[str, str] = {}
        band_labels = {
            "0": "Completed before the scenario",
            "1": (
                f"Current {int(result.get('scenario_academic_year') or 0)}/"
                f"{int(result.get('scenario_term') or 0)}"
            ),
        }
        for planned in _items(result.get("term_plan"), _MAX_SIMULATED_TERMS):
            if not isinstance(planned, dict):
                continue
            sequence = _number(planned.get("sequence"))
            if not sequence:
                continue
            band = sequence + 1
            band_labels[str(band)] = (
                f"Projected {int(planned.get('academic_year') or 0)}/"
                f"{int(planned.get('term') or 0)}"
            )
            for course in _course_rows(planned.get("courses")):
                future[course["code"]] = band
                if course["name"]:
                    future_names[course["code"]] = course["name"]

        visible = passed | current | set(future)
        if not visible:
            return {}

        term_of = {code: 0 for code in passed}
        term_of.update({code: 1 for code in current})
        term_of.update(future)
        status_of = {code: "passed" for code in passed}
        status_of.update({code: "studying" for code in current})
        status_of.update({code: "open" for code in future})
        name_of = {
            _text(code, 32).upper(): _text(name)
            for code, name in raw_names.items()
            if _text(code, 32).upper() in visible and _text(name)
        }
        name_of.update({row["code"]: row["name"] for row in current_rows if row["name"]})
        name_of.update(future_names)

        edges = []
        for row in _items(source_graph.get("items"), _MAX_GRAPH_EDGES):
            if not isinstance(row, dict):
                continue
            course = _text(row.get("course_code"), 32).upper()
            prereq = _text(row.get("prerequisite_course_code"), 32).upper()
            if course in visible and prereq in visible:
                edges.append(
                    {
                        "course_code": course,
                        "prerequisite_course_code": prereq,
                    }
                )

        removed = what_if.get("removed_current_courses") if isinstance(what_if, dict) else []
        added = what_if.get("added_current_courses") if isinstance(what_if, dict) else []
        return normalise_presentation(
            {
                "kind": KIND_GRADUATION,
                "program": result.get("program"),
                "planning_term": band_labels["1"].removeprefix("Current "),
                "simulation_completed": result.get("simulation_completed"),
                "estimated_terms_including_current": result.get(
                    "estimated_terms_including_current"
                ),
                "lower_bound_terms_including_current": result.get(
                    "lower_bound_terms_including_current"
                ),
                "max_credits_per_term": result.get("max_credits_per_term"),
                "graph": {
                    "items": edges,
                    "termOf": term_of,
                    "nameOf": name_of,
                    "statusOf": status_of,
                    "extraNodes": sorted(visible),
                },
                "band_labels": band_labels,
                "unresolved_requirements": result.get("unresolved_requirements"),
                "removed_current_courses": removed,
                "added_current_courses": added,
            }
        )
    return {}
