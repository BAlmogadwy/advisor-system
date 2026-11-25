"""Compare the graduation forecast for built advisor timetable options."""

from __future__ import annotations

from collections import defaultdict

from core.models import (
    Course,
    ElectiveCourse,
    ProgrammeRequirement,
    Student,
    TermSection,
    TermSectionProgram,
)
from core.services.course_identity import normalize_course_name
from core.services.eligibility import (
    evaluate_prerequisites,
    scenario_effective_credits,
    split_hour_prereqs,
)
from core.services.student_graduation import (
    DEFAULT_MAX_CREDITS_PER_TERM,
    REGISTERED_TIMETABLE,
    _course_term_positions,
    build_graduation_report,
)
from core.services.student_helpers import (
    get_program_prerequisites,
    get_student_passed_and_studying,
    normalize_code,
)
from core.services.student_sections import get_student_term_baseline, section_course_key
from core.services.timetable_snapshots import Snapshot, SnapshotClass, effective_class

_RETAINED_KINDS = {
    SnapshotClass.REGISTRAR: REGISTERED_TIMETABLE,
    SnapshotClass.EXPECTED: "expected_plan",
    SnapshotClass.WORKING: "working_timetable",
}
_MAX_REASONS = 3


def _section_id(mapping: dict) -> int | None:
    value = str(mapping.get("term_section_id") or "")
    return int(value) if value.isascii() and value.isdecimal() else None


def _course_reference(key: str, name: str) -> tuple[str, str]:
    code, _separator, identity_name = key.partition("::")
    return code, identity_name or name


def _row_names(rows: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in rows:
        key = normalize_code(row.get("course_key") or row.get("course_code"))
        code, name = _course_reference(key, str(row.get("course_name") or ""))
        if code:
            names.setdefault(code, name)
    return names


def _option_names(
    option: dict, sections: dict[int, TermSection], unsafe_ids: set[int]
) -> dict[str, str] | None:
    names: dict[str, str] = {}
    for mapping in option["mappings"]:
        section_id = _section_id(mapping)
        section = sections.get(section_id) if section_id is not None else None
        if section is None or section_id in unsafe_ids:
            return None
        section_key = section_course_key(section)
        signed_key = normalize_code(mapping.get("course_key"))
        if not section_key or (signed_key and signed_key != section_key):
            return None
        code, name = _course_reference(section_key, section.course_name)
        if not code:
            return None
        names.setdefault(code, name)
    return names


def _unsafe_offering_ids(sections: list[TermSection], canonical_names: dict[str, str]) -> set[int]:
    """Check names and programme links for a display code with distinct identities."""
    titles: dict[str, set[str]] = defaultdict(set)
    equivalent_programmes: dict[str, set[str]] = defaultdict(set)
    for program, code, name in ProgrammeRequirement.objects.filter(
        course_code__in=canonical_names
    ).values_list("program", "course_code", "course_name"):
        code = normalize_code(code)
        title = normalize_course_name(name)
        if title and title != code:
            titles[code].add(title)
        if title == normalize_course_name(canonical_names[code]):
            equivalent_programmes[code].add(normalize_code(program))

    unsafe: set[int] = set()
    ambiguous: dict[int, str] = {}
    for section in sections:
        key = section_course_key(section)
        code, name = _course_reference(key, section.course_name)
        expected = normalize_course_name(canonical_names.get(code))
        actual = normalize_course_name(name)
        if not expected or expected == code or expected == actual:
            continue
        alternatives = titles[code] - {expected}
        if "::" in key or actual in alternatives:
            unsafe.add(section.pk)
        elif alternatives:
            ambiguous[section.pk] = code

    corroborated = {
        section_id
        for section_id, program in TermSectionProgram.objects.filter(
            term_section_id__in=ambiguous
        ).values_list("term_section_id", "program")
        if normalize_code(program) in equivalent_programmes[ambiguous[section_id]]
    }
    return unsafe | (ambiguous.keys() - corroborated)


def _entry_prerequisite_reasons(
    proposed: set[str], passed: set[str], earned: int, prerequisites: dict[str, list[str]]
) -> list[dict]:
    reasons = []
    for code in sorted(proposed - passed):
        outcome = evaluate_prerequisites(
            prerequisites.get(code, []),
            passed,
            set(),
            earned_credits=earned,
            strict_passed_only=True,
        )
        if not outcome.met:
            reasons.append(
                {
                    "code": "unmet_course_prerequisites",
                    "course_code": code,
                    "missing_prerequisites": outcome.missing,
                }
            )
    return reasons[:_MAX_REASONS]


def _summarize(report: dict, names: dict[str, str], planning_term: dict) -> dict:
    count = None
    completion = {}
    status = "unavailable"
    additional = report.get("estimated_additional_terms")
    if report.get("simulation_completed") is True and additional is not None:
        status = "complete"
        # Count the selected planning semester across every comparison, including
        # when one of the alternatives takes no courses that semester.
        count = 1 + int(additional)
        completion = (report.get("term_plan") or [planning_term])[-1]
    elif report.get("simulation_completed") is False:
        status = "unresolved"

    credits_by_term: list[dict] = []
    if status == "complete":
        credits_by_term = [
            {
                **planning_term,
                "credits": int(report.get("planning_baseline_credits") or 0),
                "waiting": False,
            }
        ]
        credits_by_term += [
            {
                "academic_year": row["academic_year"],
                "term": row["term"],
                "credits": int(row.get("credits") or 0),
                "waiting": bool(row.get("waiting_term")),
            }
            for row in report.get("term_plan") or []
        ]

    return {
        "status": status,
        "course_codes": sorted(names),
        "terms_including_planning_term": count,
        "credits_by_term": credits_by_term,
        "completion_academic_year": completion.get("academic_year"),
        "completion_term": completion.get("term"),
        "unresolved_requirements": [
            {
                "code": row["code"],
                "missing_course_prerequisites": row.get("missing_course_prerequisites") or [],
                "credit_hour_gate": row.get("credit_hour_gate"),
                "project_sequence_gate": row.get("project_sequence_gate"),
            }
            for row in report.get("unresolved_requirements") or []
        ],
    }


def _calendar_position(position: dict) -> dict:
    return {"academic_year": position["academic_year"], "term": position["term"]}


def _movement_reasons(
    before: dict, after: dict, prerequisites: dict[str, list[str]], delta: int | None
) -> list[dict]:
    before_positions = _course_term_positions(before)
    after_positions = _course_term_positions(after)
    movement = {
        code: after_positions[code]["sequence"] - before_positions[code]["sequence"]
        for code in before_positions.keys() & after_positions.keys()
        if after_positions[code]["sequence"] != before_positions[code]["sequence"]
    }

    # A prerequisite explanation requires observed movement on both sides of the
    # edge and the prerequisite preceding its dependent in both simulations.
    dependents: dict[str, str] = {}
    for dependent in sorted(movement):
        direct_prerequisites, _hours = split_hour_prereqs(prerequisites.get(dependent, []))
        for code in direct_prerequisites:
            if (
                movement.get(code, 0) * movement[dependent] > 0
                and before_positions[code]["sequence"] < before_positions[dependent]["sequence"]
                and after_positions[code]["sequence"] < after_positions[dependent]["sequence"]
            ):
                dependents.setdefault(code, dependent)

    ordered = sorted(
        movement,
        key=lambda code: (
            bool(delta and movement[code] * delta < 0),
            code not in dependents,
            -max(before_positions[code]["sequence"], after_positions[code]["sequence"]),
            code,
        ),
    )
    reasons = []
    for code in ordered[:_MAX_REASONS]:
        direction = "earlier" if movement[code] < 0 else "later"
        dependent = dependents.get(code)
        reasons.append(
            {
                "code": f"{'prerequisite' if dependent else 'course'}_moved_{direction}",
                "course_code": code,
                "before": _calendar_position(before_positions[code]),
                "after": _calendar_position(after_positions[code]),
                **({"related_course_code": dependent} if dependent else {}),
            }
        )
    return reasons


def _explain(
    baseline: dict,
    option: dict,
    baseline_report: dict,
    option_report: dict,
    prerequisites: dict[str, list[str]],
    delta: int | None,
) -> list[dict]:
    if baseline["status"] == "unavailable":
        return [{"code": baseline["reason_code"]}]
    added = sorted(set(option["course_codes"]) - set(baseline["course_codes"]))
    removed = sorted(set(baseline["course_codes"]) - set(option["course_codes"]))
    if not added and not removed:
        return [{"code": "same_course_set"}]

    before_unresolved = {row["code"] for row in baseline["unresolved_requirements"]}
    after_unresolved = {row["code"] for row in option["unresolved_requirements"]}
    reasons = [
        {"code": "requirement_resolved", "course_code": code}
        for code in sorted(before_unresolved - after_unresolved)
    ] + [
        {"code": "requirement_unresolved", "course_code": code}
        for code in sorted(after_unresolved - before_unresolved)
    ]
    reasons += _movement_reasons(baseline_report, option_report, prerequisites, delta)
    if not reasons:
        reasons = [{"code": "course_added", "course_code": code} for code in added] + [
            {"code": "course_removed", "course_code": code} for code in removed
        ]
    return reasons[:_MAX_REASONS]


def compare_planner_graduation(
    *,
    student_id: int,
    year: int,
    term: int,
    options: list[dict],
    retained_baseline: list[dict] | None = None,
) -> dict:
    """Project the exact course sets from server-built options and same-term enrolments.

    The solver's retained rows can be expected or working timetable evidence in
    keep mode. They compose the proposal but cannot establish factual enrolment.
    """
    planning_term = {"academic_year": year, "term": term}
    registered_rows = get_student_term_baseline(
        student_id, str(year), str(term), snapshot=Snapshot.REGISTERED
    )
    retained_rows = retained_baseline or []
    registered_names = _row_names(registered_rows)
    retained_names = _row_names(retained_rows)
    registered_ids = {_section_id(row) for row in registered_rows}
    retained_ids = {_section_id(row) for row in retained_rows} - {None}
    section_ids = retained_ids | (
        {_section_id(mapping) for option in options for mapping in option["mappings"]} - {None}
    )
    sections = TermSection.objects.filter(scenario__isnull=True).in_bulk(section_ids)

    student_row = (
        Student.objects.filter(student_id=student_id)
        .values_list("program", "total_earned_credits", "current_registered_credits", "gpa")
        .first()
    )
    program = (student_row or (None,))[0]
    plan = {
        normalize_code(code): (name, int(credits or 0))
        for code, name, credits in ProgrammeRequirement.objects.filter(program=program).values_list(
            "course_code", "course_name", "credit_hours"
        )
    }
    codes = (
        set(registered_names)
        | set(retained_names)
        | {section_course_key(section).partition("::")[0] for section in sections.values()}
    )
    outside_plan = codes - plan.keys()
    catalogue = {
        normalize_code(code): (name, int(hours))
        for code, name, hours in Course.objects.filter(
            course_code__in=outside_plan, credit_hours__gt=0
        ).values_list("course_code", "description", "credit_hours")
    }
    elective_prerequisites = {}
    for code, name, hours, raw_prerequisites in ElectiveCourse.objects.filter(
        programme=program, course_code__in=outside_plan, credit_hours__gt=0
    ).values_list("course_code", "course_name", "credit_hours", "prerequisites_csv"):
        code = normalize_code(code)
        catalogue[code] = (name, int(hours))
        elective_prerequisites[code] = [
            value
            for part in (raw_prerequisites or "").split(",")
            if (value := normalize_code(part))
        ]
    metadata = catalogue | plan
    unsafe_ids = _unsafe_offering_ids(
        [section for sid, section in sections.items() if sid not in registered_ids],
        {code: metadata[code][0] for code in codes & metadata.keys()},
    )
    selected_names = {
        option["name"]: _option_names(option, sections, unsafe_ids) for option in options
    }
    prerequisites = get_program_prerequisites(program) if program and plan else {}
    passed, studying = get_student_passed_and_studying(student_id) if plan else (set(), set())
    earned = scenario_effective_credits(
        student_row[1] if student_row else 0,
        sum(plan[code][1] for code in passed & plan.keys()),
    )
    query_cache: dict[object, object] = {
        ("graduation_student", student_id): student_row,
        ("passed_and_studying", student_id): (passed, studying),
    }
    reports: dict[tuple[str, ...], dict] = {}

    def forecast(names: dict[str, str], *, factual: bool = False) -> tuple[dict, list[dict]]:
        if term not in {1, 2}:
            return {}, [{"code": "unsupported_planning_term"}]
        if not plan:
            return {}, [{"code": "academic_plan_unavailable"}]
        if names.keys() - metadata.keys():
            return {}, [{"code": "section_metadata_unavailable"}]
        if not factual:
            # A proposed course is assumed passed at the end of this term. A
            # prerequisite being studied in this same term cannot validate it.
            reasons = _entry_prerequisite_reasons(
                names.keys() - registered_names.keys(),
                passed,
                earned,
                elective_prerequisites | prerequisites,
            )
            if reasons:
                return {}, reasons
        signature = tuple(sorted(names))
        if signature not in reports:
            reports[signature] = build_graduation_report(
                student_id,
                year,
                term,
                planning_baseline_kind=REGISTERED_TIMETABLE,
                _current_courses_override=[
                    {"code": code, "name": names[code], "credits": metadata[code][1], "section": ""}
                    for code in signature
                ],
                _excluded_studying_codes=studying - set(signature),
                _prerequisite_map=prerequisites,
                _query_cache=query_cache,
            )
        report = reports[signature]
        return report, [] if report else [{"code": "academic_plan_unavailable"}]

    baseline_report, baseline_reasons = (
        forecast(registered_names, factual=True)
        if registered_names or term not in {1, 2}
        else ({}, [{"code": "no_registered_timetable"}])
    )
    baseline = {
        **_summarize(baseline_report, registered_names, planning_term),
        "kind": REGISTERED_TIMETABLE if registered_names else None,
        "reason_code": baseline_reasons[0]["code"] if baseline_reasons else None,
    }
    option_results = {}
    for name, selected in selected_names.items():
        names = retained_names | (selected or {})
        report, errors = (
            forecast(names)
            if (selected is not None and not retained_ids & unsafe_ids) or term not in {1, 2}
            else ({}, [{"code": "section_metadata_unavailable"}])
        )
        option = _summarize(report, names, planning_term)
        delta = None
        if baseline["status"] == option["status"] == "complete":
            delta = (
                option["terms_including_planning_term"] - baseline["terms_including_planning_term"]
            )
        option_results[name] = {
            **option,
            "delta_terms": delta,
            "reasons": errors
            or _explain(baseline, option, baseline_report, report, prerequisites, delta),
        }

    return {
        "planning_term": planning_term,
        "future_max_credits_per_term": DEFAULT_MAX_CREDITS_PER_TERM,
        "retained_baseline_kind": _RETAINED_KINDS.get(effective_class(retained_rows)),
        "baseline": baseline,
        "options": option_results,
    }
