"""Read-only certification of academically useful course replacements.

Academic value and timetable feasibility are intentionally separate decisions.
``student_graduation.build_graduation_what_if`` is the authority for whether a
one-for-one replacement improves a complete graduation forecast.  Only pairs it
proves useful reach the existing student Planner adapter, which then has to place
the added course beside every retained section in the modified baseline.

The section catalogue is a recorded, termless snapshot.  A result therefore proves
only that the sections *on file* form a complete clash-free arrangement; it says
nothing about live seats, current offering, registration permission, or registration
itself.  No model is written by this module.
"""

from __future__ import annotations

from typing import Any

from core.models import Course
from core.services.planner_builder import DAY_MAP, Meeting, _overlap
from core.services.student_graduation import (
    DEFAULT_MAX_CREDITS_PER_TERM,
    build_graduation_what_if,
)
from core.services.student_helpers import normalize_code
from core.services.student_planner import PlannerRequest, PlannerUnavailable, build_student_options
from core.services.student_sections import get_student_term_baseline, timetable_snapshot_kind

MAX_ACADEMIC_RESULTS_TO_CERTIFY = 20
MAX_CERTIFIED_REPLACEMENTS = 5
MAX_REJECTED_DETAILS = 20

LIMITATIONS = [
    "The section catalogue is a recorded, termless snapshot; a section on file is not proof that it is offered now.",
    "Capacity is deliberately ignored because the snapshot does not reserve a seat. No result proves a live seat.",
    "This is read-only planning. It does not register, drop, replace, or save any course or timetable.",
    "A clash-free result does not establish registration permission, equivalence, or an approved exception.",
]
VALID_MEETING_DAYS = frozenset({"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"})


def _clock_minutes(value: Any) -> int | None:
    parts = str(value or "").strip().split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or second != 0:
        return None
    return hour * 60 + minute


def _validated_meeting(day: Any, start: Any, end: Any) -> Meeting | None:
    raw_day = str(day or "").strip()
    raw_start = str(start or "").strip()
    raw_end = str(end or "").strip()
    normalised_day = str(DAY_MAP.get(raw_day, raw_day)).strip().upper()
    start_minutes = _clock_minutes(raw_start)
    end_minutes = _clock_minutes(raw_end)
    if (
        normalised_day not in VALID_MEETING_DAYS
        or start_minutes is None
        or end_minutes is None
        or end_minutes <= start_minutes
    ):
        return None
    return Meeting(
        normalised_day,
        f"{start_minutes // 60:02d}:{start_minutes % 60:02d}",
        f"{end_minutes // 60:02d}:{end_minutes % 60:02d}",
    )


def _public_course(row: dict[str, Any] | None, fallback_code: str = "") -> dict[str, Any]:
    source = row or {}
    code = normalize_code(source.get("code") or source.get("course_code") or fallback_code)
    return {
        "course_code": code,
        "course_name": str(source.get("name") or source.get("course_name") or ""),
        "credits": int(source.get("credits") or source.get("credit_hours") or 0),
    }


def _public_academic_improvement(comparison: dict[str, Any]) -> dict[str, Any]:
    def blocker_codes(key: str) -> list[str]:
        return sorted(
            {
                normalize_code(row.get("code") or "")
                for row in comparison.get(key) or []
                if isinstance(row, dict) and normalize_code(row.get("code") or "")
            }
        )

    return {
        "proven_improvement": comparison.get("proven_improvement") is True,
        "timing_effect": str(comparison.get("timing_effect") or "NOT_DETERMINABLE"),
        "term_difference": comparison.get("term_difference"),
        "terms_saved": comparison.get("terms_saved"),
        "exact_timing_comparison_available": comparison.get("exact_timing_comparison_available")
        is True,
        "improvement_basis": str(comparison.get("improvement_basis") or "NONE"),
        "blockers_resolved": blocker_codes("blockers_resolved"),
        "blockers_improved": blocker_codes("blockers_improved"),
        "blockers_introduced": blocker_codes("blockers_introduced"),
    }


def _scenario_summary(row: dict[str, Any] | None) -> dict[str, Any]:
    source = row or {}
    return {
        "simulation_completed": source.get("simulation_completed") is True,
        "estimated_additional_terms": source.get("estimated_additional_terms"),
        "lower_bound_additional_terms": source.get("lower_bound_additional_terms"),
    }


def _pair_from_explicit(result: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    what_if = result.get("what_if") or {}
    errors = list(what_if.get("validation_errors") or [])
    comparison = what_if.get("comparison") or {}
    removed = list(what_if.get("removed_current_courses") or [])
    added = list(what_if.get("added_current_courses") or [])
    if not what_if.get("valid") or errors:
        return None, {"status": "ACADEMIC_INVALID", "validation_errors": errors}
    if not removed or not added or comparison.get("proven_improvement") is not True:
        return None, {
            "status": "ACADEMIC_NOT_IMPROVING",
            "timing_effect": str(comparison.get("timing_effect") or "NOT_DETERMINABLE"),
            "validation_errors": [],
        }
    return (
        {
            "remove_course": removed[0],
            "add_course": added[0],
            "outside_plan_addition": bool(what_if.get("outside_plan_additions")),
            "comparison": comparison,
            "scenario": what_if.get("scenario") or {},
            "_academic_baseline_courses": list(
                (what_if.get("baseline") or {}).get("planning_baseline_courses_assumed_passed")
                or []
            ),
        },
        {},
    )


def _explicit_pair(
    student_id: int,
    academic_year: int,
    term: int,
    remove_code: str,
    add_code: str,
    cap: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    result = build_graduation_what_if(
        student_id,
        academic_year,
        term,
        remove_current_courses=[remove_code],
        add_current_courses=[add_code],
        max_credits_per_term=cap,
    )
    return _pair_from_explicit(result)


def _academic_pairs(
    *,
    student_id: int,
    academic_year: int,
    term: int,
    cap: int,
    remove_code: str,
    add_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    """Return proven pairs, academic rejections, metadata and extra limitations."""
    rejections: list[dict[str, Any]] = []
    limitations: list[str] = []

    if remove_code and add_code:
        pair, error = _explicit_pair(student_id, academic_year, term, remove_code, add_code, cap)
        if not pair:
            rejections.append(
                {
                    "remove_course": {"course_code": remove_code},
                    "add_course": {"course_code": add_code},
                    "academic": error,
                    "timetable": {"status": "NOT_EVALUATED"},
                }
            )
        return (
            [pair] if pair else [],
            rejections,
            {
                "pairs_evaluated": 1,
                "search_truncated": False,
                "candidate_courses_considered": [add_code],
            },
            limitations,
        )

    if not remove_code and not add_code:
        search = build_graduation_what_if(
            student_id,
            academic_year,
            term,
            search_better_replacements=True,
            max_credits_per_term=cap,
            max_replacement_results=MAX_ACADEMIC_RESULTS_TO_CERTIFY,
        )
        what_if = search.get("what_if") or {}
        candidate_codes = [
            normalize_code(code)
            for code in what_if.get("candidate_courses_considered") or []
            if normalize_code(code)
        ]
        return (
            [
                {
                    **row,
                    "_academic_baseline_courses": list(
                        (what_if.get("baseline") or {}).get(
                            "planning_baseline_courses_assumed_passed"
                        )
                        or []
                    ),
                }
                for row in what_if.get("improving_replacements") or []
            ],
            rejections,
            {
                "pairs_evaluated": int(what_if.get("pairs_evaluated") or 0),
                "search_truncated": bool(what_if.get("search_truncated"))
                or bool(what_if.get("replacement_results_truncated")),
                "candidate_courses_considered": candidate_codes,
                "academic_improvements_found": int(
                    what_if.get("improving_replacements_found") or 0
                ),
                "academic_results_checked_for_timetable": len(
                    what_if.get("improving_replacements") or []
                ),
            },
            limitations,
        )

    # Re-run the canonical academic search with its pair loop constrained at the
    # source. This keeps every proof and ranking rule in student_graduation while
    # avoiding an unsafe post-filter of a truncated top-results list.
    filtered = build_graduation_what_if(
        student_id,
        academic_year,
        term,
        search_better_replacements=True,
        max_credits_per_term=cap,
        max_replacement_results=MAX_ACADEMIC_RESULTS_TO_CERTIFY,
        replacement_remove_course=remove_code or None,
        replacement_add_course=add_code or None,
    )
    filtered_what_if = filtered.get("what_if") or {}
    academic_baseline_courses = list(
        (filtered_what_if.get("baseline") or {}).get("planning_baseline_courses_assumed_passed")
        or []
    )
    proven = [
        {**row, "_academic_baseline_courses": academic_baseline_courses}
        for row in filtered_what_if.get("improving_replacements") or []
    ]
    truncated = bool(filtered_what_if.get("search_truncated")) or bool(
        filtered_what_if.get("replacement_results_truncated")
    )
    if not proven and not truncated:
        rejections.append(
            {
                "remove_course": {"course_code": remove_code or ""},
                "add_course": {"course_code": add_code or ""},
                "academic": {
                    "status": "ACADEMIC_NOT_IMPROVING",
                    "validation_errors": [],
                },
                "timetable": {"status": "NOT_EVALUATED"},
            }
        )
    return (
        proven,
        rejections,
        {
            "pairs_evaluated": int(filtered_what_if.get("pairs_evaluated") or 0),
            "search_truncated": truncated,
            "candidate_courses_considered": list(
                filtered_what_if.get("candidate_courses_considered") or []
            ),
            "academic_improvements_found": int(
                filtered_what_if.get("improving_replacements_found") or 0
            ),
            "academic_results_checked_for_timetable": len(proven),
        },
        limitations,
    )


def _group_baseline(rows: list[dict[str, Any]]) -> dict[str, dict[int | str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[int | str, list[dict[str, Any]]]] = {}
    for row in rows:
        code = normalize_code(row.get("course_code") or row.get("course_key") or "")
        if not code:
            continue
        sid = row.get("term_section_id")
        identity: int | str = int(sid) if sid not in (None, "") else str(row.get("section") or "")
        grouped.setdefault(code, {}).setdefault(identity, []).append(row)
    return grouped


def _retained_baseline(
    rows: list[dict[str, Any]], academic_baseline_codes: set[str], remove_code: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected = academic_baseline_codes - {remove_code}
    grouped = _group_baseline(rows)
    issues: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for code in sorted(expected):
        sections = grouped.get(code) or {}
        if not sections:
            issues.append(
                {"reason_code": "BASELINE_SECTION_MAPPING_INCOMPLETE", "course_code": code}
            )
            continue
        if len(sections) != 1:
            issues.append({"reason_code": "MULTIPLE_BASELINE_SECTIONS", "course_code": code})
            continue
        section_rows = next(iter(sections.values()))
        if not section_rows or any(
            _validated_meeting(row.get("day"), row.get("start_time"), row.get("end_time")) is None
            for row in section_rows
        ):
            issues.append({"reason_code": "BASELINE_MEETING_DATA_MISSING", "course_code": code})
            continue
        retained.extend(section_rows)
    return retained, issues


def _retained_clashes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meetings: list[tuple[str, str, Meeting]] = []
    for row in rows:
        code = normalize_code(row.get("course_code") or "")
        section = str(row.get("section") or "").strip()
        day = str(row.get("day") or "").strip()
        start = str(row.get("start_time") or "").strip()
        end = str(row.get("end_time") or "").strip()
        meeting = _validated_meeting(day, start, end)
        if meeting is not None:
            meetings.append((code, section, meeting))
    clashes: list[dict[str, Any]] = []
    for index, (code_a, section_a, meeting_a) in enumerate(meetings):
        for code_b, section_b, meeting_b in meetings[index + 1 :]:
            if (code_a, section_a) == (code_b, section_b):
                continue
            if _overlap(meeting_a, meeting_b):
                clashes.append(
                    {
                        "course_a": code_a,
                        "section_a": section_a,
                        "course_b": code_b,
                        "section_b": section_b,
                        "day": meeting_a.day,
                    }
                )
    return clashes


def _failure_reason(result: dict[str, Any]) -> tuple[str, str]:
    rows = list(result.get("unplaced") or [])
    if rows:
        return (
            str(rows[0].get("reason_code") or "UNKNOWN"),
            str(rows[0].get("reason") or ""),
        )
    failures = list(result.get("constraint_failures") or [])
    if failures:
        text = str(failures[0].get("reason") or "")
        if "No sections available" in text:
            return "NOT_ON_FILE", text
        if "No non-conflicting sections" in text:
            return "ALL_SECTIONS_CLASH", text
        return "DID_NOT_FIT", text
    return "UNKNOWN", str(result.get("reason") or "No complete timetable was generated.")


def _public_option(
    option: dict[str, Any],
    names: dict[str, str],
    expected_codes: set[str],
) -> dict[str, Any] | None:
    courses = list(option.get("courses") or [])
    meetings = list(option.get("meetings") or [])
    selected_codes = {
        normalize_code(row.get("course_code") or "")
        for row in courses
        if normalize_code(row.get("course_code") or "")
    }
    if (
        selected_codes != expected_codes
        or len(courses) != len(expected_codes)
        or option.get("unplaced")
    ):
        return None

    public_sections: list[dict[str, Any]] = []
    public_meetings: list[dict[str, Any]] = []
    section_keys: set[tuple[str, str]] = set()
    for course in courses:
        code = normalize_code(course.get("course_code") or "")
        section = str(course.get("section") or "").strip()
        if course.get("meeting_issue_codes"):
            return None
        section_key = (code, section.casefold())
        if not code or not section or section_key in section_keys:
            return None
        section_keys.add(section_key)
        raw_section_meetings = [
            row
            for row in meetings
            if normalize_code(row.get("course_code") or "") == code
            and str(row.get("section") or "").strip() == section
        ]
        if not raw_section_meetings or any(
            _validated_meeting(row.get("day"), row.get("start"), row.get("end")) is None
            for row in raw_section_meetings
        ):
            return None
        section_meetings = [
            {
                "course_code": code,
                "section": section,
                "day": _validated_meeting(row.get("day"), row.get("start"), row.get("end")).day,
                "start": str(row.get("start") or ""),
                "end": str(row.get("end") or ""),
            }
            for row in raw_section_meetings
        ]
        # A selected section without meeting facts cannot be clash-certified.
        if not section or not section_meetings:
            return None
        public_meetings.extend(section_meetings)
        public_sections.append(
            {
                "course_code": code,
                "course_name": names.get(code, ""),
                "section": section,
                "credits": int(course.get("credits") or 0),
                "source": str(course.get("source") or ""),
                "meetings": [
                    {"day": row["day"], "start": row["start"], "end": row["end"]}
                    for row in section_meetings
                ],
            }
        )

    validated = [
        (
            row["course_code"],
            row["section"],
            _validated_meeting(row["day"], row["start"], row["end"]),
        )
        for row in public_meetings
    ]
    for index, (code_a, section_a, meeting_a) in enumerate(validated):
        if meeting_a is None:
            return None
        for code_b, section_b, meeting_b in validated[index + 1 :]:
            if meeting_b is None:
                return None
            if (code_a, section_a) != (code_b, section_b) and _overlap(meeting_a, meeting_b):
                return None

    return {
        "planner_options": list(option.get("planner_options") or []),
        "complete_sections": public_sections,
        "meetings": public_meetings,
        "scheduled_courses": len(public_sections),
        "target_courses": len(expected_codes),
        "credit_hours": int(option.get("credit_hours") or 0),
        "days_on_campus": int(option.get("days_on_campus") or 0),
        "days": list(option.get("days") or []),
        "earliest_start": option.get("earliest_start"),
        "latest_end": option.get("latest_end"),
    }


def _course_names(raw_baseline: list[dict[str, Any]], pair: dict[str, Any]) -> dict[str, str]:
    names = {
        normalize_code(row.get("course_code") or ""): str(row.get("course_name") or "")
        for row in raw_baseline
        if normalize_code(row.get("course_code") or "")
    }
    for key in ("remove_course", "add_course"):
        row = pair.get(key) or {}
        code = normalize_code(row.get("code") or row.get("course_code") or "")
        if code and (row.get("name") or row.get("course_name")):
            names[code] = str(row.get("name") or row.get("course_name"))
    missing = [code for code, name in names.items() if not name]
    if missing:
        for code, description in Course.objects.filter(course_code__in=missing).values_list(
            "course_code", "description"
        ):
            names[normalize_code(code)] = str(description or "")
    return names


def find_feasible_course_replacements(
    student_id: int,
    academic_year: int,
    term: int,
    *,
    remove_course: str | None = None,
    add_course: str | None = None,
    max_credits_per_term: int = DEFAULT_MAX_CREDITS_PER_TERM,
) -> dict[str, Any]:
    """Return only academically improved swaps with a complete timetable proof."""
    sid = int(student_id)
    year = int(academic_year)
    term_number = int(term)
    cap = max(1, int(max_credits_per_term))
    requested_remove = normalize_code(remove_course or "")
    requested_add = normalize_code(add_course or "")
    raw_baseline = [
        dict(row) for row in get_student_term_baseline(sid, str(year), str(term_number))
    ]
    baseline_kind_raw = timetable_snapshot_kind(raw_baseline)
    baseline_kind = {
        "registered": "REGISTERED",
        "expected": "EXPECTED_PLAN",
        "empty": "EMPTY",
        "mixed": "MIXED_REVIEW_REQUIRED",
    }.get(baseline_kind_raw, "NOT_DETERMINABLE")

    base = {
        "student_id": sid,
        "academic_year": year,
        "term": term_number,
        "baseline_kind": baseline_kind,
        "requested_remove_course": requested_remove or None,
        "requested_add_course": requested_add or None,
    }
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        return {
            **base,
            "status": "NOT_DETERMINABLE",
            "academic_search": {
                "pairs_evaluated": 0,
                "search_truncated": False,
                "candidate_courses_considered": [],
            },
            "certified_replacements": [],
            "rejected_replacements": [
                {"timetable": {"status": "NOT_DETERMINABLE", "reason_code": "MIXED_BASELINE"}}
            ],
            "limitations": LIMITATIONS,
        }

    pairs, rejected, search_meta, extra_limits = _academic_pairs(
        student_id=sid,
        academic_year=year,
        term=term_number,
        cap=cap,
        remove_code=requested_remove,
        add_code=requested_add,
    )
    certified: list[dict[str, Any]] = []
    timetable_candidates_checked = 0
    for pair in pairs:
        timetable_candidates_checked += 1
        removed = _public_course(pair.get("remove_course"))
        added = _public_course(pair.get("add_course"))
        remove_code = removed["course_code"]
        add_code = added["course_code"]
        academic_baseline_rows = list(pair.get("_academic_baseline_courses") or [])
        academic_codes = {
            normalize_code(row.get("code") or "")
            for row in academic_baseline_rows
            if normalize_code(row.get("code") or "")
        }
        academic_credits = {
            normalize_code(row.get("code") or ""): int(row.get("credits") or 0)
            for row in academic_baseline_rows
            if normalize_code(row.get("code") or "") and int(row.get("credits") or 0) > 0
        }
        # Graduation forecasting deliberately carries status-only studying rows
        # when a section mapping is incomplete. That is useful for an academic
        # lower bound, but it is not a selected-term timetable fact. Never certify
        # a complete modified timetable while any such academic-baseline course—
        # including the requested removal—has no exact section evidence.
        _complete_academic_baseline, academic_mapping_issues = _retained_baseline(
            raw_baseline, academic_codes, ""
        )
        if academic_mapping_issues:
            rejected.append(
                {
                    "remove_course": removed,
                    "add_course": added,
                    "academic": _public_academic_improvement(pair.get("comparison") or {}),
                    "timetable": {
                        "status": "NOT_DETERMINABLE",
                        "reason_code": "ACADEMIC_TIMETABLE_BASELINE_MISMATCH",
                        "details": academic_mapping_issues,
                    },
                }
            )
            continue
        retained, baseline_issues = _retained_baseline(raw_baseline, academic_codes, remove_code)
        if baseline_issues:
            rejected.append(
                {
                    "remove_course": removed,
                    "add_course": added,
                    "academic": _public_academic_improvement(pair.get("comparison") or {}),
                    "timetable": {
                        "status": "NOT_DETERMINABLE",
                        "reason_code": "BASELINE_SECTION_MAPPING_INCOMPLETE",
                        "details": baseline_issues,
                    },
                }
            )
            continue
        clashes = _retained_clashes(retained)
        if clashes:
            rejected.append(
                {
                    "remove_course": removed,
                    "add_course": added,
                    "academic": _public_academic_improvement(pair.get("comparison") or {}),
                    "timetable": {
                        "status": "INFEASIBLE",
                        "reason_code": "BASELINE_CLASH",
                        "details": clashes,
                    },
                }
            )
            continue
        try:
            planner = build_student_options(
                PlannerRequest(
                    student_id=sid,
                    year=year,
                    term=term_number,
                    must_include=(add_code,),
                    required_courses=(add_code,),
                    keep_current_sections=True,
                    max_credits=cap,
                    include_recommendations=False,
                    baseline_override=tuple(retained),
                    course_credits_override=tuple(
                        sorted(
                            {
                                **academic_credits,
                                add_code: int(added.get("credits") or 0),
                            }.items()
                        )
                    ),
                    require_complete_meetings=True,
                )
            )
        except PlannerUnavailable as exc:
            rejected.append(
                {
                    "remove_course": removed,
                    "add_course": added,
                    "academic": _public_academic_improvement(pair.get("comparison") or {}),
                    "timetable": {
                        "status": "NOT_DETERMINABLE",
                        "reason_code": "PLANNER_UNAVAILABLE",
                        "reason": str(exc),
                    },
                }
            )
            continue

        expected_codes = (academic_codes - {remove_code}) | {add_code}
        names = _course_names(raw_baseline, pair)
        public_options = [
            public
            for option in planner.get("alternatives") or []
            if (public := _public_option(option, names, expected_codes)) is not None
        ]
        if not public_options:
            reason_code, reason = _failure_reason(planner)
            # An alternative that selected a section without meeting facts is not
            # a clash proof even if the solver could include it.
            if planner.get("alternatives") and reason_code == "UNKNOWN":
                reason_code = "MISSING_MEETING_DATA"
                reason = "A selected section has no complete meeting data, so clashes cannot be certified."
            rejected.append(
                {
                    "remove_course": removed,
                    "add_course": added,
                    "academic": _public_academic_improvement(pair.get("comparison") or {}),
                    "timetable": {
                        "status": (
                            "NOT_DETERMINABLE"
                            if reason_code
                            in {
                                "NOT_ON_FILE",
                                "MISSING_MEETING_DATA",
                                "MEETING_DATA_INCOMPLETE",
                                "UNKNOWN",
                            }
                            else "INFEASIBLE"
                        ),
                        "reason_code": reason_code,
                        "reason": reason,
                    },
                }
            )
            continue

        certified.append(
            {
                "remove_course": removed,
                "add_course": added,
                "outside_plan_addition": bool(pair.get("outside_plan_addition")),
                "academic_improvement": _public_academic_improvement(pair.get("comparison") or {}),
                "graduation_scenario": _scenario_summary(pair.get("scenario")),
                "timetable": {
                    "status": "COMPLETE_CLASH_FREE",
                    "certified_options": public_options,
                },
            }
        )
        if len(certified) >= MAX_CERTIFIED_REPLACEMENTS:
            break

    certification_truncated = bool(search_meta.get("search_truncated")) or (
        timetable_candidates_checked < len(pairs)
    )
    # Positive options are certified, but Planner exploration is finite. Absence
    # of a returned option is therefore always a bounded negative, never proof
    # that no timetable exists outside the checked search space.
    status = "CERTIFIED_SWAPS_FOUND" if certified else "NOT_DETERMINABLE"
    return {
        **base,
        "status": status,
        "academic_search": search_meta,
        "certification_search": {
            "academic_candidates_received": len(pairs),
            "timetable_candidates_checked": timetable_candidates_checked,
            "certified_result_limit": MAX_CERTIFIED_REPLACEMENTS,
            "search_truncated": certification_truncated,
        },
        "certified_replacements": certified,
        "rejected_replacements": rejected[:MAX_REJECTED_DETAILS],
        "rejected_replacements_count": len(rejected),
        "limitations": [*LIMITATIONS, *extra_limits],
    }


__all__ = ["find_feasible_course_replacements"]
