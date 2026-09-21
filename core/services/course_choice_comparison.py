"""Deterministic, read-only comparison of two to four course choices.

This service deliberately does not collapse unlike evidence into one score.
Student-specific prerequisite readiness, recommender membership, remaining-plan
unlocks, programme-level graph importance, recorded timetable fit, and graduation
what-if forecasts each retain their own meaning and confidence boundary.

The comparison is advisory only.  It never writes a course, timetable, or scenario
record and it never treats a recorded section as proof of a live seat or permission
to register.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.services.course_detail import (
    KIND_COURSE,
    KIND_ELECTIVE_SLOT,
    build_course_detail,
)
from core.services.course_priority import program_downstream_importance_scores
from core.services.planner_builder import DAY_MAP, Meeting, _catalog_for_courses, _overlap
from core.services.recommender import recommend_next_courses
from core.services.student_graduation import (
    REGISTERED_TIMETABLE,
    build_graduation_report,
    build_graduation_what_if,
)
from core.services.student_helpers import normalize_code
from core.services.student_sections import (
    UnknownStudentGender,
    get_student_term_baseline,
    student_gender_strict,
    timetable_snapshot_kind,
)
from core.services.student_unlock import build_unlock_report
from core.services.timetable_snapshots import Snapshot

TOOL_NAME = "course_choice_comparison"
SUPPORTED_OBJECTIVES = frozenset({"balanced", "graduation", "unlock_impact", "timetable_fit"})
WEIGHTED_SCORE_METHOD = "sum_inverse_distance"

_BASELINE_KINDS = {
    "empty": "EMPTY",
    "registered": "REGISTERED",
    "expected": "EXPECTED_PLAN",
    "mixed": "MIXED_REVIEW_REQUIRED",
}

_VALID_MEETING_DAYS = frozenset({"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"})

_LIMITATIONS = [
    "This comparison is read-only and does not register, replace, or save a course.",
    (
        "Prerequisite readiness and recommendation membership do not prove university "
        "registration permission or that two courses are equivalent requirements."
    ),
    (
        "The section catalogue is a termless recorded snapshot; timetable fit does not "
        "prove a live offering, seat, capacity, or registration permission."
    ),
    (
        "The weighted downstream score is this project's inverse-distance planning "
        "heuristic, not an official university priority."
    ),
    (
        "Graduation scenarios assume every planning-baseline and simulated course is "
        "passed on the first attempt and do not guarantee future offerings or sections."
    ),
]


def _validated_inputs(
    student_id: int,
    course_codes: Sequence[str],
    academic_year: int,
    term: int,
    objective: str,
) -> tuple[int, list[str], int, int, str]:
    try:
        sid = int(student_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("student_id must be an integer.") from exc
    if sid <= 0:
        raise ValueError("student_id must be a positive integer.")

    if isinstance(course_codes, str | bytes) or not isinstance(course_codes, Sequence):
        raise ValueError("course_codes must contain two to four distinct course codes.")
    codes = [normalize_code(value) for value in course_codes]
    if any(not code for code in codes) or not 2 <= len(codes) <= 4:
        raise ValueError("Choose two to four non-empty course codes to compare.")
    if len(set(codes)) != len(codes):
        raise ValueError("Each compared course must be different.")

    try:
        year = int(academic_year)
        term_number = int(term)
    except (TypeError, ValueError) as exc:
        raise ValueError("academic_year and term must be integers.") from exc
    if year <= 0:
        raise ValueError("academic_year must be a positive integer.")
    if term_number not in {1, 2, 3}:
        raise ValueError("term must be 1, 2, or 3.")

    objective_name = str(objective or "balanced").strip().lower()
    if objective_name not in SUPPORTED_OBJECTIVES:
        raise ValueError("objective must be balanced, graduation, unlock_impact, or timetable_fit.")
    return sid, codes, year, term_number, objective_name


def _baseline_kind(rows: list[dict[str, object]]) -> str:
    return _BASELINE_KINDS[timetable_snapshot_kind(rows)]


def _baseline_sections(rows: list[dict[str, object]], code: str) -> list[str]:
    return sorted(
        {
            str(row.get("section") or "").strip()
            for row in rows
            if normalize_code(row.get("course_key") or row.get("course_code")) == code
            and str(row.get("section") or "").strip()
        }
    )


def _unavailable_timetable(
    *,
    status: str,
    baseline_sections: list[str],
    reason_code: str,
    reason: str,
    details: list[dict[str, Any]] | None = None,
    sections_on_file: int | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "details": list(details or []),
        "sections_on_file": sections_on_file,
        "clash_free_count": None,
        "clashing_count": None,
        "baseline_sections": baseline_sections,
    }


def _clock_minutes(value: object) -> int | None:
    """Parse a stored 24-hour time without accepting malformed evidence."""
    parts = str(value or "").strip().split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or second != 0:
        return None
    return hour * 60 + minute


def _validated_meeting(
    day: object,
    start: object,
    end: object,
) -> tuple[Meeting | None, str | None]:
    raw_day = str(day or "").strip()
    mapped_day = DAY_MAP.get(raw_day)
    normalised_day = str(mapped_day if mapped_day is not None else raw_day).strip().upper()
    if normalised_day not in _VALID_MEETING_DAYS:
        return None, "INVALID_DAY"

    start_minutes = _clock_minutes(start)
    end_minutes = _clock_minutes(end)
    if start_minutes is None or end_minutes is None:
        return None, "INVALID_TIME"
    if end_minutes <= start_minutes:
        return None, "INVALID_TIME_RANGE"
    return (
        Meeting(
            day=normalised_day,
            start=f"{start_minutes // 60:02d}:{start_minutes % 60:02d}",
            end=f"{end_minutes // 60:02d}:{end_minutes % 60:02d}",
        ),
        None,
    )


def _meeting_fields(raw: Any) -> tuple[object, object, object]:
    """Read the two supported catalogue meeting shapes consistently."""
    if isinstance(raw, dict):
        return raw.get("day"), raw.get("start"), raw.get("end")
    return getattr(raw, "day", None), getattr(raw, "start", None), getattr(raw, "end", None)


def _baseline_meeting_evidence(
    baseline: list[dict[str, object]],
) -> tuple[list[tuple[Meeting, str]], list[dict[str, Any]]]:
    occupied: list[tuple[Meeting, str]] = []
    issues: list[dict[str, Any]] = []
    seen_issues: set[tuple[str, str, str]] = set()
    for row in baseline:
        code = normalize_code(row.get("course_key") or row.get("course_code"))
        section = str(row.get("section") or "").strip()
        meeting, issue = _validated_meeting(
            row.get("day"), row.get("start_time"), row.get("end_time")
        )
        if meeting is not None:
            occupied.append((meeting, code))
            continue

        issue_code = issue or "MISSING_MEETING_DATA"
        if not any(
            str(row.get(field) or "").strip() for field in ("day", "start_time", "end_time")
        ):
            issue_code = "MISSING_MEETING_DATA"
        key = (code, section, issue_code)
        if key in seen_issues:
            continue
        seen_issues.add(key)
        issues.append(
            {
                "course_code": code,
                "section": section,
                "reason_code": issue_code,
            }
        )
    return occupied, issues


def _candidate_meeting_issues(
    code: str,
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for section in sections:
        label = str(section.get("section") or "").strip()
        meetings = list(section.get("meetings") or [])
        section_issues = {
            str(code).strip().upper()
            for code in section.get("meeting_issue_codes") or []
            if str(code).strip()
        }
        if not label:
            section_issues.add("MISSING_SECTION_LABEL")
        if not meetings:
            section_issues.add("MISSING_MEETING_DATA")
        for raw in meetings:
            day, start, end = _meeting_fields(raw)
            _meeting, issue = _validated_meeting(
                day,
                start,
                end,
            )
            if issue:
                section_issues.add(issue)
        if section_issues:
            issues.append(
                {
                    "course_code": code,
                    "section": label,
                    "term_section_id": section.get("term_section_id"),
                    "reason_codes": sorted(section_issues),
                }
            )
    return issues


def _timetable_evidence(
    *,
    student_id: int,
    program: str,
    codes: list[str],
    academic_year: int,
    term: int,
    baseline: list[dict[str, object]],
    baseline_kind: str,
) -> dict[str, dict[str, Any]]:
    baseline_by_code = {code: _baseline_sections(baseline, code) for code in codes}
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        return {
            code: _unavailable_timetable(
                status="MIXED_BASELINE_REVIEW_REQUIRED",
                baseline_sections=baseline_by_code[code],
                reason_code="MIXED_BASELINE_REVIEW_REQUIRED",
                reason=(
                    "Registered and expected-plan rows are mixed, so there is no "
                    "single timetable baseline to compare against."
                ),
            )
            for code in codes
        }

    try:
        gender = student_gender_strict(student_id)
    except UnknownStudentGender:
        return {
            code: _unavailable_timetable(
                status="COHORT_UNRESOLVED",
                baseline_sections=baseline_by_code[code],
                reason_code="COHORT_UNRESOLVED",
                reason=(
                    "The student's section cohort is unresolved, so eligible "
                    "catalogue sections cannot be selected safely."
                ),
            )
            for code in codes
        }

    catalog = _catalog_for_courses(
        str(academic_year),
        str(term),
        codes,
        gender,
        program,
    )
    occupied, baseline_issues = _baseline_meeting_evidence(baseline)

    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        sections = list(catalog.get(code) or [])
        if not sections:
            out[code] = {
                "status": "NOT_ON_FILE",
                "reason_code": "NOT_ON_FILE",
                "reason": (
                    "No section for this course is recorded in the current catalogue "
                    "snapshot; this is not proof that the university does not offer it."
                ),
                "details": [],
                "sections_on_file": 0,
                "clash_free_count": 0,
                "clashing_count": 0,
                "baseline_sections": baseline_by_code[code],
            }
            continue

        # A missing baseline meeting may hide a collision with every candidate.
        # Fail the whole timetable dimension closed instead of silently dropping
        # that row and declaring sections clash-free.
        if baseline_issues:
            out[code] = _unavailable_timetable(
                status="NOT_DETERMINABLE",
                baseline_sections=baseline_by_code[code],
                reason_code="BASELINE_MEETING_DATA_INCOMPLETE",
                reason=(
                    "At least one retained baseline section has missing or invalid "
                    "meeting data, so clashes cannot be certified."
                ),
                details=baseline_issues,
                sections_on_file=len(sections),
            )
            continue

        candidate_issues = _candidate_meeting_issues(code, sections)
        if candidate_issues:
            out[code] = _unavailable_timetable(
                status="NOT_DETERMINABLE",
                baseline_sections=baseline_by_code[code],
                reason_code="CANDIDATE_MEETING_DATA_INCOMPLETE",
                reason=(
                    "At least one recorded candidate section has missing or invalid "
                    "meeting data, so the complete timetable choice cannot be compared."
                ),
                details=candidate_issues,
                sections_on_file=len(sections),
            )
            continue

        clash_free = 0
        clashing = 0
        for section in sections:
            meetings = [
                meeting
                for raw in section.get("meetings") or []
                if (meeting := _validated_meeting(*_meeting_fields(raw))[0]) is not None
            ]
            has_clash = any(
                _overlap(section_meeting, baseline_meeting)
                for section_meeting in meetings
                for baseline_meeting, baseline_code in occupied
                # Checking a different section of the same course is a replacement,
                # so its recorded block must not collide with itself.
                if baseline_code != code
            )
            if has_clash:
                clashing += 1
            else:
                clash_free += 1
        out[code] = {
            "status": "OK" if clash_free else "ALL_CLASH",
            "sections_on_file": len(sections),
            "clash_free_count": clash_free,
            "clashing_count": clashing,
            "baseline_sections": baseline_by_code[code],
        }
    return out


def _structured_missing_prerequisites(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    locked = next(
        (row for row in report.get("locked_courses") or [] if row.get("code") == code),
        None,
    )
    if not locked:
        return []

    missing: list[dict[str, Any]] = []
    for raw in locked.get("reasons") or []:
        kind = str(raw.get("kind") or "")
        if kind in {"MISSING_COURSE", "UNKNOWN_PREREQ"}:
            missing.append(
                {
                    "kind": kind,
                    "course_code": normalize_code(raw.get("code")),
                }
            )
        elif kind == "MISSING_HOURS":
            missing.append(
                {
                    "kind": kind,
                    "required": int(raw.get("required") or 0),
                    "effective": int(raw.get("effective") or 0),
                    "remaining": int(raw.get("remaining") or 0),
                }
            )
        elif kind:
            missing.append({"kind": kind})
    return missing


def _prerequisite_ready(kind: str, academic_status: str) -> bool | None:
    if kind != KIND_COURSE:
        return None
    if academic_status == "blocked":
        return False
    if academic_status in {"open_now", "studying", "passed"}:
        return True
    return None


def _report_status(report: dict[str, Any], code: str) -> str:
    """Read the unlock report's closed status vocabulary for one requirement."""
    for bucket, status in (
        ("done", "passed"),
        ("in_progress", "studying"),
        ("open_courses", "open_now"),
        ("locked_courses", "blocked"),
    ):
        if any(row.get("code") == code for row in report.get(bucket) or []):
            return status
    return "unknown"


def _graduation_evidence(
    *,
    student_id: int,
    codes: list[str],
    academic_year: int,
    term: int,
    baseline_report: dict[str, Any],
    candidate_meta: dict[str, tuple[str, str]],
    academic_code_by_candidate: dict[str, str] | None = None,
    query_cache: dict[object, object] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one-choice scenarios over the same non-candidate baseline.

    Every compared course already present in the planning baseline is removed,
    except for the candidate selected for that row.  A candidate not already in
    that baseline is then added.  Consequently every valid scenario contains the
    same non-candidate courses and exactly one of the compared choices.
    """
    current_codes = {
        normalize_code(row.get("code"))
        for row in baseline_report.get("planning_baseline_courses_assumed_passed") or []
        if normalize_code(row.get("code"))
    }
    candidate_codes = academic_code_by_candidate or {code: code for code in codes}
    compared_current = current_codes & set(candidate_codes.values())
    out: dict[str, dict[str, Any]] = {}

    for code in codes:
        kind, academic_status = candidate_meta[code]
        academic_code = candidate_codes[code]
        if kind == KIND_ELECTIVE_SLOT:
            out[code] = _graduation_unavailable("ELECTIVE_PLACEHOLDER")
            continue
        if kind != KIND_COURSE:
            out[code] = _graduation_unavailable("NOT_IN_PLAN")
            continue
        if academic_status == "passed":
            out[code] = _graduation_unavailable("ALREADY_PASSED")
            continue
        if academic_status == "studying" and academic_code != code:
            out[code] = _graduation_unavailable("ALREADY_STUDYING")
            continue

        remove_codes = sorted(compared_current - {academic_code})
        add_codes = [] if academic_code in current_codes else [academic_code]
        scenario = build_graduation_what_if(
            student_id,
            academic_year,
            term,
            planning_baseline_kind=REGISTERED_TIMETABLE,
            remove_current_courses=remove_codes,
            add_current_courses=add_codes,
            search_better_replacements=False,
            _query_cache=query_cache,
        )
        what_if = scenario.get("what_if") or {}
        if not scenario or not what_if.get("valid"):
            out[code] = _graduation_unavailable("INVALID_SCENARIO")
            continue

        completed = scenario.get("simulation_completed") is True
        out[code] = {
            "status": "COMPLETED" if completed else "NOT_DETERMINABLE",
            "simulation_completed": completed,
            "estimated_additional_terms": scenario.get("estimated_additional_terms"),
            "lower_bound_additional_terms": scenario.get("lower_bound_additional_terms"),
            "unresolved_requirements": list(scenario.get("unresolved_requirements") or []),
        }
    return out


def _graduation_unavailable(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "simulation_completed": False,
        "estimated_additional_terms": None,
        "lower_bound_additional_terms": None,
        "unresolved_requirements": [],
    }


def _leaders_max(candidates: list[dict[str, Any]], getter: Any) -> list[str]:
    values = [(row["course_code"], getter(row)) for row in candidates]
    values = [(code, value) for code, value in values if value is not None]
    if not values:
        return []
    best = max(value for _code, value in values)
    return [code for code, value in values if value == best]


def _leaders_min(candidates: list[dict[str, Any]], getter: Any) -> list[str]:
    values = [(row["course_code"], getter(row)) for row in candidates]
    values = [(code, value) for code, value in values if value is not None]
    if not values:
        return []
    best = min(value for _code, value in values)
    return [code for code, value in values if value == best]


def _criterion_leaders(
    candidates: list[dict[str, Any]],
    actionable: list[dict[str, Any]],
) -> dict[str, list[str]]:
    actionable_codes = [row["course_code"] for row in actionable]
    recommended = [row for row in actionable if row["recommendation"]["rank"] is not None]
    recommendation_leaders = _leaders_min(
        recommended,
        lambda row: row["recommendation"]["rank"],
    )

    timetable_comparable = bool(actionable) and all(
        row["timetable"]["status"] in {"OK", "ALL_CLASH"} for row in actionable
    )
    graduation_comparable = bool(actionable) and all(
        row["graduation"]["simulation_completed"] is True
        and row["graduation"]["estimated_additional_terms"] is not None
        for row in actionable
    )
    return {
        "prerequisite_readiness": actionable_codes,
        "recommendation": recommendation_leaders,
        "direct_unlock": _leaders_max(
            actionable,
            lambda row: row["impact"]["direct_unlock_count"],
        ),
        "chain_impact": _leaders_max(
            actionable,
            lambda row: row["impact"]["chain_course_count"],
        ),
        "weighted_downstream_impact": _leaders_max(
            actionable,
            lambda row: row["impact"]["weighted_downstream_score"],
        ),
        "timetable_fit": (
            _leaders_max(actionable, lambda row: row["timetable"]["clash_free_count"])
            if timetable_comparable
            else []
        ),
        "graduation_terms": (
            _leaders_min(
                actionable,
                lambda row: row["graduation"]["estimated_additional_terms"],
            )
            if graduation_comparable
            else []
        ),
    }


def _impact_relation(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, bool]:
    """Whether ``a`` is no worse than ``b``, and whether it is better somewhere."""
    a_impact = a["impact"]
    b_impact = b["impact"]
    pairs = [
        (a_impact["direct_unlock_count"], b_impact["direct_unlock_count"]),
        (a_impact["chain_course_count"], b_impact["chain_course_count"]),
    ]
    if (
        a_impact["weighted_downstream_score"] is not None
        and b_impact["weighted_downstream_score"] is not None
    ):
        pairs.append(
            (
                a_impact["weighted_downstream_score"],
                b_impact["weighted_downstream_score"],
            )
        )
    return all(left >= right for left, right in pairs), any(left > right for left, right in pairs)


def _impact_winner(actionable: list[dict[str, Any]]) -> str | None:
    winners = []
    for candidate in actionable:
        dominates_all = all(
            other is candidate
            or (lambda relation: relation[0] and relation[1])(_impact_relation(candidate, other))
            for other in actionable
        )
        if dominates_all:
            winners.append(candidate["course_code"])
    return winners[0] if len(winners) == 1 else None


def _all_impact_equal(actionable: list[dict[str, Any]]) -> bool:
    signatures = {
        (
            row["impact"]["direct_unlock_count"],
            row["impact"]["chain_course_count"],
            row["impact"]["weighted_downstream_score"],
        )
        for row in actionable
    }
    return len(signatures) <= 1


def _verdict(
    *,
    objective: str,
    candidates: list[dict[str, Any]],
    actionable: list[dict[str, Any]],
    leaders: dict[str, list[str]],
) -> tuple[str, str | None, list[str]]:
    if any(row["kind"] != KIND_COURSE or row["academic_status"] == "unknown" for row in candidates):
        # A valid course must not win merely because the other code could not be
        # evaluated (outside-plan code, unresolved elective slot, or missing
        # academic state). That would turn missing evidence into a preference.
        return "NOT_DETERMINABLE", None, ["candidate_evidence_incomplete"]
    if not actionable:
        return "NOT_DETERMINABLE", None, ["no_prerequisite_ready_new_course"]

    actionable_codes = {row["course_code"] for row in actionable}
    if objective == "unlock_impact":
        winner = _impact_winner(actionable)
        if winner:
            basis = [
                criterion
                for criterion in (
                    "direct_unlock",
                    "chain_impact",
                    "weighted_downstream_impact",
                )
                if leaders[criterion] == [winner]
            ]
            return "PREFERRED", winner, basis or ["unlock_impact_dominance"]
        if _all_impact_equal(actionable):
            return "TIE", None, ["unlock_impact_tie"]
        return "NOT_DETERMINABLE", None, ["conflicting_unlock_dimensions"]

    if objective == "timetable_fit":
        timetable_leaders = leaders["timetable_fit"]
        if not timetable_leaders:
            return "NOT_DETERMINABLE", None, ["timetable_evidence_incomplete"]
        if len(timetable_leaders) == 1:
            return "PREFERRED", timetable_leaders[0], ["timetable_fit"]
        return "TIE", None, ["timetable_fit_tie"]

    if objective == "graduation":
        graduation_leaders = leaders["graduation_terms"]
        if not graduation_leaders:
            return "NOT_DETERMINABLE", None, ["graduation_forecast_incomplete"]
        if len(graduation_leaders) == 1:
            return "PREFERRED", graduation_leaders[0], ["graduation_terms"]
        return "TIE", None, ["graduation_terms_tie"]

    if len(actionable) == 1:
        return (
            "PREFERRED",
            actionable[0]["course_code"],
            ["prerequisite_readiness"],
        )

    # Balanced means dominance across separate verified criteria, not a weighted
    # sum.  Empty criteria are absent evidence and therefore cannot vote.
    balanced_criteria = [
        "recommendation",
        "direct_unlock",
        "chain_impact",
        "weighted_downstream_impact",
        "timetable_fit",
        "graduation_terms",
    ]
    available = [criterion for criterion in balanced_criteria if leaders[criterion]]
    if not available:
        return "NOT_DETERMINABLE", None, ["comparison_evidence_incomplete"]
    common = set(actionable_codes)
    for criterion in available:
        common &= set(leaders[criterion])
    if len(common) == 1:
        winner = next(iter(common))
        decisive = [criterion for criterion in available if leaders[criterion] == [winner]]
        return "PREFERRED", winner, decisive or available
    if common == actionable_codes:
        return "TIE", None, ["all_verified_criteria_tied"]
    return "NOT_DETERMINABLE", None, ["criteria_favour_different_courses"]


def compare_course_choices(
    student_id: int,
    course_codes: Sequence[str],
    academic_year: int,
    term: int,
    *,
    objective: str = "balanced",
    timetable_evidence_available: bool = True,
    academic_state: Any | None = None,
) -> dict[str, Any]:
    """Compare exact course choices without mutating any persisted state.

    ``ValueError`` is reserved for malformed requests or a missing student plan;
    uncertain evidence inside a valid comparison is represented in the relevant
    dimension and produces a conservative verdict.
    """
    sid, codes, year, term_number, objective_name = _validated_inputs(
        student_id,
        course_codes,
        academic_year,
        term,
        objective,
    )

    baseline = get_student_term_baseline(
        sid, str(year), str(term_number), snapshot=Snapshot.EFFECTIVE
    )
    baseline_kind = _baseline_kind(baseline)
    query_cache: dict[object, object] = {}
    unlock_report = build_unlock_report(
        sid,
        year,
        term_number,
        additional_studying_codes=(
            set(academic_state.registered_requirement_course_codes)
            if academic_state is not None
            else None
        ),
    )
    program = str(unlock_report.get("program") or "").strip()
    if not program:
        raise ValueError("Student programme or degree-plan data is not recorded.")

    recommended_codes = [
        normalize_code(code)
        for code in recommend_next_courses(
            sid,
            year,
            term_number,
            resolve_electives=False,
        )
        if normalize_code(code)
    ]
    recommendation_rank = {code: index + 1 for index, code in enumerate(recommended_codes)}
    importance = program_downstream_importance_scores(program)
    if timetable_evidence_available:
        timetable = _timetable_evidence(
            student_id=sid,
            program=program,
            codes=codes,
            academic_year=year,
            term=term_number,
            baseline=baseline,
            baseline_kind=baseline_kind,
        )
    else:
        timetable = {
            code: _unavailable_timetable(
                status="NOT_DETERMINABLE",
                baseline_sections=_baseline_sections(baseline, code),
                reason_code="SECTION_SNAPSHOT_TERM_MISMATCH",
                reason=(
                    "The section catalogue is the configured current snapshot, not the "
                    "explicit comparison term, so timetable fit cannot be certified."
                ),
            )
            for code in codes
        }

    academic_code_by_candidate = (
        {
            code: (
                requirements[0]
                if len(requirements := academic_state.requirement_course_codes_for(code)) == 1
                else code
            )
            for code in codes
        }
        if academic_state is not None
        else {code: code for code in codes}
    )
    detail_by_code = {
        code: build_course_detail(
            sid,
            academic_code_by_candidate[code],
            academic_year=str(year),
            term=str(term_number),
            report=unlock_report,
        )
        for code in codes
    }
    graduation_baseline = build_graduation_report(
        sid,
        year,
        term_number,
        planning_baseline_kind=REGISTERED_TIMETABLE,
        _query_cache=query_cache,
    )
    if not graduation_baseline:
        graduation = {code: _graduation_unavailable("NOT_DETERMINABLE") for code in codes}
    else:
        graduation = _graduation_evidence(
            student_id=sid,
            codes=codes,
            academic_year=year,
            term=term_number,
            baseline_report=graduation_baseline,
            candidate_meta={
                code: (
                    (
                        KIND_COURSE
                        if academic_code_by_candidate[code] != code
                        else str(detail_by_code[code].get("kind") or "NOT_IN_PLAN")
                    ),
                    (
                        _report_status(unlock_report, academic_code_by_candidate[code])
                        if academic_code_by_candidate[code] != code
                        else str(detail_by_code[code].get("your_status") or "unknown")
                    ),
                )
                for code in codes
            },
            academic_code_by_candidate=academic_code_by_candidate,
            query_cache=query_cache,
        )

    baseline_codes = {
        normalize_code(row.get("course_key") or row.get("course_code"))
        for row in baseline
        if normalize_code(row.get("course_key") or row.get("course_code"))
    }
    registered_equivalents = (
        set(academic_state.registered_or_equivalent_course_codes)
        if academic_state is not None
        else set()
    )
    expected_equivalents = (
        set(academic_state.expected_or_equivalent_course_codes)
        if academic_state is not None
        else set()
    )
    candidates: list[dict[str, Any]] = []
    for code in codes:
        detail = detail_by_code[code]
        academic_code = academic_code_by_candidate[code]
        is_concrete_alias = academic_code != code
        kind = KIND_COURSE if is_concrete_alias else str(detail.get("kind") or "NOT_IN_PLAN")
        academic_status = (
            _report_status(unlock_report, academic_code)
            if is_concrete_alias
            else str(detail.get("your_status") or "unknown")
        )
        if code in registered_equivalents and academic_code in set(
            academic_state.registered_requirement_course_codes if academic_state is not None else ()
        ):
            academic_status = "studying"
        dependent = (unlock_report.get("dependents") or {}).get(academic_code) or {}
        if code in registered_equivalents or (
            academic_state is None and code in baseline_codes and baseline_kind == "REGISTERED"
        ):
            recommendation_state = "ALREADY_IN_CURRENT_TIMETABLE"
        elif code in expected_equivalents or (
            academic_state is None and code in baseline_codes and baseline_kind == "EXPECTED_PLAN"
        ):
            recommendation_state = "ALREADY_IN_EXPECTED_PLAN"
        elif code in baseline_codes and baseline_kind == "MIXED_REVIEW_REQUIRED":
            recommendation_state = "BASELINE_SOURCE_REVIEW_REQUIRED"
        elif code in recommendation_rank or academic_code in recommendation_rank:
            recommendation_state = "RECOMMENDED"
        else:
            recommendation_state = "NOT_RECOMMENDED"

        candidates.append(
            {
                "course_code": code,
                "requirement_course_code": academic_code,
                "course_name": (
                    academic_state.course(code).metadata.course_name
                    if is_concrete_alias
                    and academic_state is not None
                    and academic_state.course(code) is not None
                    else str(detail.get("course_name") or "")
                ),
                "credit_hours": (
                    academic_state.course(code).metadata.credit_hours
                    if is_concrete_alias
                    and academic_state is not None
                    and academic_state.course(code) is not None
                    else detail.get("credit_hours")
                ),
                "kind": kind,
                "academic_status": academic_status,
                "prerequisite_ready": _prerequisite_ready(kind, academic_status),
                "missing_prerequisites": _structured_missing_prerequisites(
                    unlock_report, academic_code
                ),
                "recommendation": {
                    "state": recommendation_state,
                    "rank": recommendation_rank.get(code) or recommendation_rank.get(academic_code),
                },
                "impact": {
                    "direct_unlock_count": len(dependent.get("waiting_only_on_this") or []),
                    "chain_course_count": int(dependent.get("on_chain_of_count") or 0),
                    "weighted_downstream_score": (
                        importance.get(academic_code) if kind == KIND_COURSE else None
                    ),
                    "weighted_score_method": (
                        WEIGHTED_SCORE_METHOD if kind == KIND_COURSE else None
                    ),
                },
                "timetable": timetable[code],
                "graduation": graduation[code],
            }
        )

    # A passed, already-studying, blocked, placeholder, or outside-plan item is
    # still explained in the table, but cannot win a recommendation to take a new
    # course now.  This makes readiness an explicit gate rather than a hidden
    # bonus inside a score.
    actionable = [
        row
        for row in candidates
        if row["kind"] == KIND_COURSE
        and row["academic_status"] == "open_now"
        and row["prerequisite_ready"] is True
    ]
    leaders = _criterion_leaders(candidates, actionable)
    verdict, preferred_course, decision_basis = _verdict(
        objective=objective_name,
        candidates=candidates,
        actionable=actionable,
        leaders=leaders,
    )
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "program": program,
        "academic_year": year,
        "term": term_number,
        "objective": objective_name,
        "baseline_kind": baseline_kind,
        "candidates": candidates,
        "criterion_leaders": leaders,
        "verdict": verdict,
        "preferred_course": preferred_course,
        "decision_basis": decision_basis,
        "limitations": list(_LIMITATIONS),
    }


__all__ = ["compare_course_choices"]
