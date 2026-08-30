"""Whitelisted, student-visible companions to durable adviser answers.

The prose remains the audit record of what was said. Timetable alternatives are
materially easier to use as a small interface, so this module creates a view model
from an already-scoped tool result. It is deliberately not a generic metadata bag:
unknown fields disappear, and the browser never receives raw tool results,
database ids, model traces, seat counts, or internal reason codes.
"""

from __future__ import annotations

import re
from typing import Any

from core.services.student_graduation import MAX_SIMULATED_TERMS

KIND_TIMETABLE = "timetable_proposals"
KIND_GRADUATION = "graduation_scenario"
_MAX_ALTERNATIVES = 9
_MAX_COURSES = 20
_MAX_MEETINGS = 80
_MAX_GRAPH_NODES = 160
_MAX_GRAPH_EDGES = 360
_MAX_UNRESOLVED = 80

_FALSE_MEDIA_INCAPABILITY = re.compile(
    r"(?:\b(?:I|we)(?:\s+can(?:not|['’]t)|\s+(?:am|are)\s+(?:not\s+able|unable)\s+to|"
    r"['’]m\s+(?:not\s+able|unable)\s+to)\s+"
    r"(?:directly\s+)?(?:generate|send|create|display|attach|provide)"
    r"(?:(?:\s*,\s*(?:(?:or|and)\s+)?|\s+(?:or|and)\s+)(?:directly\s+)?"
    r"(?:generate|send|create|display|attach|provide))*\s+"
    r"(?:(?:to\s+)?(?:you|the\s+student)\s+)?(?:an?|the|this)?\s*"
    r"(?:(?:timetable|graduation[-\s]+plan)\s+)?(?:image|photo|map|picture)s?\b|"
    r"(?:لا\s+أستطيع|ما\s+أقدر|لا\s+أقدر|ماني\s+قادر|مو\s+قادر|لا\s+يمكنني)\s+"
    r"(?:إنشاء|أنشئ|اسوي|أسوي|إرسال|ارسل|أرسل|عرض|أعرض|إرفاق|ارفق|أرفق)"
    r"(?:\s+أو\s+(?:إنشاء|أنشئ|أسوي|إرسال|أرسل|عرض|أعرض|إرفاق|أرفق))*"
    r"(?:\s+(?:لك|لكم))?\s+"
    r"(?:صورة|صور|خريطة|خرائط|مخطط))"
    r"(?:\s+(?:of|containing|with|showing)\s+[^,;،؛.!?؟\n]*)?"
    r"(?:\s+(?:to\s+you|here|in\s+(?:this|the)\s+(?:chat|channel)))?"
    r"(?:\s+(?:because|due\s+to)\s+[^,;،؛.!?؟\n]*)?"
    r"(?:\s*[,;،؛]\s*(?:but|however|لكن)\s+|\s*[.;،؛!?؟]\s*)?",
    re.IGNORECASE,
)

_PROTECTED_MEDIA_CONTEXT = re.compile(
    r"(?:\b(?:grade|mark|transcript|gpa|cgpa|score|failed[-\s]+course|"
    r"course[-\s]+result|academic\s+(?:record|standing)|probation|warning)s?\b|"
    r"(?:درجة|درجات|علامة|علامات|نتيجة|نتائج|كشف\s+الدرجات|"
    r"السجل\s+الأكاديمي|الحالة\s+الأكاديمية|الوضع\s+الأكاديمي|"
    r"المعدل(?:\s+التراكمي)?|رسوب|راسب|إنذار|تحذير))",
    re.IGNORECASE,
)


def remove_false_media_incapability(text: str) -> str:
    """Remove model claims contradicted by a structured presentation renderer."""
    original = str(text or "")

    def remove_claim(match: re.Match[str]) -> str:
        # A valid timetable/graduation card disproves a generic media incapability
        # claim, but it does not authorize grade, transcript, GPA, or other
        # protected-record imagery. Preserve those refusals verbatim.
        refusal = re.split(
            r"[,،;؛]\s*(?:(?:but|however|لكن)\b)?",
            original[match.start() :],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        if _PROTECTED_MEDIA_CONTEXT.search(refusal):
            return match.group(0)
        return ""

    cleaned = _FALSE_MEDIA_INCAPABILITY.sub(remove_claim, original)
    cleaned = re.sub(r"[ \t]+(?=\n)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip(" \t,;،؛")
    if cleaned:
        return cleaned

    # Never let transport cleanup turn a complete answer into an empty one, and
    # do not restore the false claim just to avoid silence. This neutral sentence
    # is true for every accepted structured presentation even when optional
    # Telegram media is disabled or later fails to render.
    if re.search(r"[\u0600-\u06ff]", original):
        return "تفاصيل الخطة موضحة في العرض المنظم."
    return "The plan details are shown in the structured view."


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


def _replacement_course(value: Any) -> dict[str, Any]:
    """Whitelist one course descriptor used by a certified replacement card."""
    if not isinstance(value, dict):
        return {}
    code = _text(value.get("course_code") or value.get("code"), 32).upper()
    if not code:
        return {}
    return {
        "course_code": code,
        "course_name": _text(value.get("course_name") or value.get("name")),
        "credits": _number(value.get("credits") or value.get("credit_hours")),
    }


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
        if term_value is not None and term_value <= MAX_SIMULATED_TERMS + 2:
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
            if 0 <= band <= MAX_SIMULATED_TERMS + 2:
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

    baseline_kind = _text(payload.get("planning_baseline_kind"), 32).lower()
    if baseline_kind not in {"recommended_current_term", "registered_timetable"}:
        # Legacy presentations predate explicit provenance and were built from
        # the registered timetable. Preserve that meaning rather than silently
        # relabelling an older card as a recommendation.
        baseline_kind = "registered_timetable"

    presentation = {
        "kind": KIND_GRADUATION,
        "program": _text(payload.get("program"), 32),
        "planning_term": _text(payload.get("planning_term"), 24),
        "planning_baseline_kind": baseline_kind,
        "planning_baseline_credits": _number(
            payload.get("planning_baseline_credits")
            if payload.get("planning_baseline_credits") is not None
            else payload.get("registered_credits_at_planning_baseline")
        ),
        "simulation_completed": payload.get("simulation_completed") is True,
        "estimated_terms_including_planning_baseline": _optional_number(
            payload.get("estimated_terms_including_planning_baseline")
            if payload.get("estimated_terms_including_planning_baseline") is not None
            else payload.get("estimated_terms_including_current")
        ),
        "lower_bound_terms_including_planning_baseline": _number(
            payload.get("lower_bound_terms_including_planning_baseline")
            if payload.get("lower_bound_terms_including_planning_baseline") is not None
            else payload.get("lower_bound_terms_including_current")
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
    if "noncompletion_current_courses" in payload:
        presentation["noncompletion_current_courses"] = _course_rows(
            payload.get("noncompletion_current_courses")
        )
    return presentation


def normalise_presentation(payload: Any) -> dict[str, Any]:
    """Return the only structured adviser payload the student UI may render."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("kind") == KIND_GRADUATION:
        return _normalise_graduation_presentation(payload)
    if payload.get("kind") != KIND_TIMETABLE:
        return {}

    mode = _text(payload.get("mode"), 32)
    baseline_kind = _text(payload.get("baseline_kind"), 32).upper() or "REGISTERED"
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        # The capability should already have refused this state.  Keep the view
        # model fail-closed as a second boundary so a stored/legacy payload cannot
        # render combined expected and registrar rows under a reassuring label.
        return {}
    if baseline_kind not in {"REGISTERED", "EXPECTED_PLAN", "EMPTY"}:
        return {}

    def section_rows(value: Any) -> list[dict[str, Any]]:
        rows = []
        for row in _items(value, _MAX_COURSES):
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "course_code": _text(row.get("course_code"), 32),
                    "course_name": _text(row.get("course_name")),
                    "section": _text(row.get("section"), 32),
                    "credits": _number(row.get("credits")),
                    "meetings": [
                        _text(item, 80) for item in _items(row.get("meetings"), _MAX_MEETINGS)
                    ],
                }
            )
        return rows

    baseline_sections = section_rows(payload.get("baseline_sections"))
    if not baseline_sections:
        legacy_field = (
            "expected_plan_sections" if baseline_kind == "EXPECTED_PLAN" else "current_sections"
        )
        baseline_sections = section_rows(payload.get(legacy_field))
    current_sections = baseline_sections if baseline_kind == "REGISTERED" else []
    expected_plan_sections = baseline_sections if baseline_kind == "EXPECTED_PLAN" else []

    must_take_courses = [
        _text(code, 32).upper()
        for code in _items(payload.get("must_take_courses"), _MAX_COURSES)
        if _text(code, 32)
    ]
    pinned_sections = [
        {
            "course_code": _text(row.get("course_code"), 32).upper(),
            "section_label": _text(row.get("section_label") or row.get("section"), 32).upper(),
        }
        for row in _items(payload.get("pinned_sections"), _MAX_COURSES)
        if isinstance(row, dict)
        and _text(row.get("course_code"), 32)
        and _text(row.get("section_label") or row.get("section"), 32)
    ]
    constraint_failures = [
        {
            "course_code": _text(row.get("course_code"), 32).upper(),
            "section_label": _text(row.get("section_label"), 32).upper(),
            "reason": _text(row.get("reason"), 500),
        }
        for row in _items(payload.get("constraint_failures"), _MAX_COURSES)
        if isinstance(row, dict)
    ]
    target_credits = _optional_number(payload.get("target_credits"))
    if target_credits is not None and target_credits <= 0:
        target_credits = None
    target_credit_status = _text(payload.get("target_credit_status"), 48).upper()
    if target_credit_status not in {
        "NOT_REQUESTED",
        "SATISFIED",
        "TARGET_EXCEEDS_EFFECTIVE_MAX",
        "RETAINED_BASELINE_EXCEEDS_TARGET",
        "NO_EXACT_ALTERNATIVE",
    }:
        target_credit_status = "NOT_REQUESTED" if target_credits is None else "NO_EXACT_ALTERNATIVE"

    replacement: dict[str, Any] = {}
    raw_replacement = payload.get("replacement")
    if isinstance(raw_replacement, dict):
        removed = _replacement_course(raw_replacement.get("remove_course"))
        added = _replacement_course(raw_replacement.get("add_course"))
        if removed and added:
            improvement = (
                raw_replacement.get("academic_improvement")
                if isinstance(raw_replacement.get("academic_improvement"), dict)
                else {}
            )
            replacement = {
                "remove_course": removed,
                "add_course": added,
                "outside_plan_addition": raw_replacement.get("outside_plan_addition") is True,
                "academic_improvement": {
                    "proven_improvement": improvement.get("proven_improvement") is True,
                    "terms_saved": _optional_number(improvement.get("terms_saved")),
                },
            }

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

    target_projection_mismatch = False
    if target_credits is not None:
        exact_alternatives = [
            row for row in alternatives if _number(row.get("total_credit_hours")) == target_credits
        ]
        target_projection_mismatch = len(exact_alternatives) != len(alternatives)
        alternatives = exact_alternatives
        if target_projection_mismatch:
            target_credit_status = "NO_EXACT_ALTERNATIVE"
            constraint_failures.append(
                {
                    "course_code": "",
                    "section_label": "",
                    "reason": (
                        "A timetable whose total differed from the exact credit target "
                        "was withheld from the presentation."
                    ),
                }
            )

    # The solver returns the student's baseline in from-scratch mode for
    # comparison and provenance. Those sections are not fixed or retained, so
    # presenting them under "Current retained sections" is materially false.
    if mode == "from_scratch":
        baseline_sections = []
        current_sections = []
        expected_plan_sections = []

    if (
        not alternatives
        and not baseline_sections
        and not must_take_courses
        and not pinned_sections
        and not constraint_failures
        and target_credits is None
    ):
        return {}
    baseline_target_satisfied = (
        target_credits is not None
        and payload.get("no_additional_courses") is True
        and _number(payload.get("baseline_credit_hours")) == target_credits
    )
    target_credits_satisfied = (
        target_credits is not None
        and not target_projection_mismatch
        and payload.get("target_credits_satisfied") is True
        and (bool(alternatives) or baseline_target_satisfied)
    )
    if target_credits is not None and not target_credits_satisfied:
        target_credit_status = (
            target_credit_status
            if target_credit_status
            in {
                "TARGET_EXCEEDS_EFFECTIVE_MAX",
                "RETAINED_BASELINE_EXCEEDS_TARGET",
                "NO_EXACT_ALTERNATIVE",
            }
            else "NO_EXACT_ALTERNATIVE"
        )
    presentation = {
        "kind": KIND_TIMETABLE,
        "planning_term": _text(payload.get("planning_term"), 24),
        "mode": mode,
        "baseline_kind": baseline_kind,
        "baseline_credit_hours": _number(payload.get("baseline_credit_hours")),
        "current_credit_hours": _number(payload.get("current_credit_hours")),
        "expected_plan_credit_hours": _number(payload.get("expected_plan_credit_hours")),
        "credit_ceiling": _number(payload.get("credit_ceiling")),
        "target_credits": target_credits,
        "target_credits_satisfied": target_credits_satisfied,
        "target_credit_status": target_credit_status,
        "baseline_sections": baseline_sections,
        "current_sections": current_sections,
        "expected_plan_sections": expected_plan_sections,
        "alternatives": alternatives,
        "must_take_courses": must_take_courses,
        "pinned_sections": pinned_sections,
        "constraints_satisfied": (
            payload.get("constraints_satisfied") is True
            and not target_projection_mismatch
            and (target_credits is None or target_credits_satisfied)
        ),
        "constraint_failures": constraint_failures,
        "no_additional_courses": payload.get("no_additional_courses") is True,
        # Server-owned constants, never copied from a model or client.
        "can_save": False,
        "can_register": False,
    }
    if replacement:
        presentation["replacement"] = replacement
    return presentation


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
                "baseline_kind": result.get("baseline_kind"),
                "baseline_credit_hours": result.get("baseline_credit_hours"),
                "current_credit_hours": result.get("current_credit_hours"),
                "expected_plan_credit_hours": result.get("expected_plan_credit_hours"),
                "credit_ceiling": result.get("credit_ceiling"),
                "target_credits": result.get("target_credits"),
                "target_credits_satisfied": result.get("target_credits_satisfied"),
                "target_credit_status": result.get("target_credit_status"),
                "baseline_sections": result.get("baseline_sections"),
                "current_sections": result.get("current_sections"),
                "expected_plan_sections": result.get("expected_plan_sections"),
                "alternatives": result.get("alternatives"),
                "must_take_courses": result.get("must_take_courses"),
                "pinned_sections": result.get("pinned_sections"),
                "constraints_satisfied": result.get("constraints_satisfied"),
                "constraint_failures": result.get("constraint_failures"),
                "no_additional_courses": result.get("no_additional_courses"),
            }
        )
    return {}


def replacement_timetable_presentation_from_tool_results(
    tool_results: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """Render the best certified replacement as complete timetable alternatives.

    The academic service ranks certified replacements, so the first row is the
    selected swap.  Every certified option already contains the retained sections
    plus the added course and complete meeting facts.  Projecting that full set as
    an alternative lets the existing web and Telegram renderers show the actual
    clash-free result without leaking database section ids or duplicating retained
    courses in a separate baseline block.
    """
    for result in reversed(list(tool_results or [])):
        if not isinstance(result, dict):
            continue
        if result.get("tool") != "feasible_course_replacements" or not result.get("ok"):
            continue

        certified = _items(result.get("certified_replacements"), 1)
        if not certified or not isinstance(certified[0], dict):
            return {}
        selected = certified[0]
        timetable = selected.get("timetable")
        if not isinstance(timetable, dict) or timetable.get("status") != "COMPLETE_CLASH_FREE":
            return {}

        alternatives = []
        for option in _items(timetable.get("certified_options"), _MAX_ALTERNATIVES):
            if not isinstance(option, dict):
                continue
            sections = [
                row
                for row in _items(option.get("complete_sections"), _MAX_COURSES)
                if isinstance(row, dict)
            ]
            names = {
                _text(row.get("course_code"), 32).upper(): _text(row.get("course_name"))
                for row in sections
                if _text(row.get("course_code"), 32)
            }
            courses = [
                {
                    "course_code": row.get("course_code"),
                    "course_name": row.get("course_name"),
                    "section": row.get("section"),
                    "credits": row.get("credits"),
                }
                for row in sections
            ]
            meetings = [
                {
                    "course_code": row.get("course_code"),
                    "course_name": names.get(_text(row.get("course_code"), 32).upper(), ""),
                    "section": row.get("section"),
                    "day": row.get("day"),
                    "start": row.get("start"),
                    "end": row.get("end"),
                }
                for row in _items(option.get("meetings"), _MAX_MEETINGS)
                if isinstance(row, dict)
            ]
            course_keys = {
                (
                    _text(row.get("course_code"), 32).upper(),
                    _text(row.get("section"), 32).upper(),
                )
                for row in sections
                if _text(row.get("course_code"), 32) and _text(row.get("section"), 32)
            }
            meeting_keys = {
                (
                    _text(row.get("course_code"), 32).upper(),
                    _text(row.get("section"), 32).upper(),
                )
                for row in meetings
                if row.get("day") and row.get("start") and row.get("end")
            }
            if (
                not courses
                or len(course_keys) != len(courses)
                or not meetings
                or not course_keys.issubset(meeting_keys)
            ):
                continue
            alternatives.append(
                {
                    "planner_options": option.get("planner_options"),
                    "scheduled_courses": option.get("scheduled_courses"),
                    "target_courses": option.get("target_courses"),
                    "proposed_credit_hours": option.get("credit_hours"),
                    "total_credit_hours": option.get("credit_hours"),
                    "days_on_campus": option.get("days_on_campus"),
                    "days": option.get("days"),
                    "earliest_start": option.get("earliest_start"),
                    "latest_end": option.get("latest_end"),
                    "courses": courses,
                    "meetings": meetings,
                    "unplaced_courses": [],
                }
            )

        if not alternatives:
            return {}

        removed = _replacement_course(selected.get("remove_course"))
        added = _replacement_course(selected.get("add_course"))
        resulting_credits = _number(alternatives[0].get("total_credit_hours"))
        baseline_credits = max(
            0,
            resulting_credits - _number(added.get("credits")) + _number(removed.get("credits")),
        )
        baseline_kind = _text(result.get("baseline_kind"), 32).upper()
        return normalise_presentation(
            {
                "kind": KIND_TIMETABLE,
                "planning_term": (
                    f"{_number(result.get('academic_year'))}/{_number(result.get('term'))}"
                ),
                "mode": "certified_replacement",
                "baseline_kind": baseline_kind,
                "baseline_credit_hours": baseline_credits,
                "current_credit_hours": (baseline_credits if baseline_kind == "REGISTERED" else 0),
                "expected_plan_credit_hours": (
                    baseline_credits if baseline_kind == "EXPECTED_PLAN" else 0
                ),
                # The alternatives contain the complete modified timetable.  A
                # separate baseline block would duplicate all retained courses.
                "baseline_sections": [],
                "alternatives": alternatives,
                "must_take_courses": [added.get("course_code")] if added else [],
                "constraints_satisfied": True,
                "replacement": {
                    "remove_course": removed,
                    "add_course": added,
                    "outside_plan_addition": selected.get("outside_plan_addition"),
                    "academic_improvement": selected.get("academic_improvement"),
                },
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
        current_rows = _course_rows(
            result.get("planning_baseline_courses_assumed_passed")
            or result.get("current_courses_assumed_passed")
        )
        current = {row["code"] for row in current_rows}

        future: dict[str, int] = {}
        future_names: dict[str, str] = {}
        baseline_year = int(
            result.get("planning_baseline_academic_year")
            or result.get("scenario_academic_year")
            or 0
        )
        baseline_term = int(
            result.get("planning_baseline_term") or result.get("scenario_term") or 0
        )
        planning_term = f"{baseline_year}/{baseline_term}"
        baseline_kind = _text(result.get("planning_baseline_kind"), 32).lower()
        legacy_baseline = baseline_kind not in {
            "recommended_current_term",
            "registered_timetable",
        }
        if legacy_baseline:
            baseline_kind = "registered_timetable"
        recommended_baseline = baseline_kind == "recommended_current_term"
        band_labels = {
            "0": "Completed before the scenario",
            "1": (
                f"Recommended starting term {planning_term}"
                if recommended_baseline
                else (
                    f"Planning baseline {planning_term}"
                    if legacy_baseline
                    else f"Registered timetable {planning_term}"
                )
            ),
        }
        for planned in _items(result.get("term_plan"), MAX_SIMULATED_TERMS):
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
        status_of.update({code: "open" if recommended_baseline else "studying" for code in current})
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
        presentation_payload = {
            "kind": KIND_GRADUATION,
            "program": result.get("program"),
            "planning_term": planning_term,
            "planning_baseline_kind": baseline_kind,
            "planning_baseline_credits": result.get(
                "planning_baseline_credits",
                result.get("registered_credits_at_planning_baseline"),
            ),
            "simulation_completed": result.get("simulation_completed"),
            "estimated_terms_including_planning_baseline": result.get(
                "estimated_terms_including_planning_baseline",
                result.get("estimated_terms_including_current"),
            ),
            "lower_bound_terms_including_planning_baseline": result.get(
                "lower_bound_terms_including_planning_baseline",
                result.get("lower_bound_terms_including_current"),
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
        if isinstance(what_if, dict) and "noncompletion_current_courses" in what_if:
            presentation_payload["noncompletion_current_courses"] = what_if.get(
                "noncompletion_current_courses"
            )
        return normalise_presentation(presentation_payload)
    return {}
