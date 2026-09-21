"""Independent answer checks over the structured evidence saved in an eval trace.

This module deliberately does not decide intent from Arabic wording.  The contract
already says which evidence must be presented, and the runner saves the exact tool
messages that crossed the provider boundary.  The only text extraction here is for
typed academic facts: codes, sections, times, and quantities next to their semantic
labels.

That division matters: a model may phrase an answer naturally, but it may not omit
the rows it was asked to show or introduce an exact fact the evidence did not carry.
"""

from __future__ import annotations

import re
from typing import Any

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Course codes have at least two letters.  That excludes ordinary M/F section
# labels, while retaining short institutional codes such as AI1.
_COURSE_CODE = re.compile(r"(?<![A-Z0-9])([A-Z]{2,6})\s*-?\s*(\d{1,4})(?![A-Z0-9])", re.I)
_SECTION = re.compile(r"(?<![A-Z0-9])(Y?[MF])\s*-?\s*(\d{1,4})(?![A-Z0-9])", re.I)
_CREDIT_QUANTITY = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:ساعة|ساعات|ساعه|credit\s*hours?|credits?)(?![A-Z])",
    re.I,
)
_COURSE_QUANTITY = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:مقرر\w*|مواد|مادة|course(?:s)?)(?![A-Z])",
    re.I,
)
_CLOCK_TIME = re.compile(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)")
_PERCENTAGE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:%|٪)")
_TERM_QUANTITY = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:فصل(?:اً|ا|ين)?|فصول|ترم(?:ات)?|terms?)(?![A-Z])",
    re.I,
)
# Headings and cards often put the unit before the value: ``عدد المقررات
# المجتازة: 32``. Keep the window short and still require a semantic
# passed/remaining/total label before accepting the number below.
_PREFIX_COURSE_QUANTITY = re.compile(
    r"(?:(?:عدد|number\s+of)\s+)?(?:المقررات|مقررات|المواد|مواد|courses?)"
    r"[^0-9\n]{0,32}?[:=]?\s*(\d{1,3})(?!\d)",
    re.I,
)
_PREFIX_CREDIT_QUANTITY = re.compile(
    r"(?:(?:عدد|number\s+of)\s+)?"
    r"(?:الساعات|ساعات|الوحدات|وحدات|credit\s*hours?|credits?)"
    r"[^0-9\n]{0,32}?[:=]?\s*(\d{1,3})(?!\d)",
    re.I,
)
_PREFIX_TERM_QUANTITY = re.compile(
    r"(?:(?:عدد|number\s+of)\s+)?"
    r"(?:الفصول|فصول|الترمات|ترمات|terms?|semesters?)"
    r"[^0-9\n]{0,32}?[:=]?\s*(\d{1,2})(?!\d)",
    re.I,
)
_MEETING_STRING = re.compile(
    r"^\s*([A-Z]{2,10})\s+([0-2]?\d:[0-5]\d)\s*[-–—]\s*([0-2]?\d:[0-5]\d)\s*$",
    re.I,
)
_CLAUSE_BOUNDARY = re.compile(r"[\n.!?؟؛;،]+")

_PASSED_WORDS = (
    "اجتز",
    "أنجز",
    "انجز",
    "أكمل",
    "اكمل",
    "ناجح",
    "مجتاز",
    "مكتمل",
    "مكتسب",
    "محصل",
    "completed",
    "passed",
    "earned",
)
_REMAINING_WORDS = (
    "متبقي",
    "المتبقي",
    "باقي",
    "الباقي",
    "بقي",
    "يبقى",
    "سيبقى",
    "remaining",
    "will remain",
    "left",
)
_TOTAL_WORDS = ("إجمالي", "اجمالي", "مجموع", "أصل", "اصل", "total", "overall")
_PLAN_WORDS = ("من الخطة", "في الخطة", "ضمن الخطة", "in the plan", "plan credits")
_REGISTRAR_WORDS = (
    "المكتسبة",
    "المكتسبه",
    "المحصلة",
    "المحصله",
    "registrar",
    "earned",
)
_POST_BASELINE_WORDS = (
    "بعد اجتياز مقررات البداية",
    "بعد مقررات البداية",
    "بعد اجتياز الفصل المرجعي",
    "after passing the baseline",
    "after the baseline",
)
_ADDITIONAL_TERM_WORDS = ("إضاف", "اضاف", "additional", "بعد فصل البداية")
_INCLUDING_BASELINE_TERM_WORDS = (
    "شامل",
    "متضمن",
    "بما فيها",
    "بما فيه",
    "إجمالي الفصول",
    "اجمالي الفصول",
    "including the baseline",
    "including baseline",
    "total terms",
)

VALID_REQUIRED_FACTS = frozenset(
    {
        "all_registration_course_codes",
        "all_registration_sections",
        "all_course_section_rows",
        "matching_structured_presentation",
        "passed_course_counts",
        "passed_credit_counts",
        "remaining_course_counts",
        "remaining_credit_counts",
        "total_course_counts",
        "total_credit_counts",
    }
)
VALID_VERIFIED_CLAIMS = frozenset(
    {
        "course_codes",
        "section_labels",
        "credit_quantities",
        "meeting_times",
        "course_section_rows",
        "course_time_rows",
        "meeting_rows",
        "progress_figures",
    }
)
VALID_PROFILES = frozenset(
    {
        "registration_listing",
        "graduation_summary",
        "timetable_proposal",
    }
)


def _normalise_course_code(value: object) -> str:
    compact = re.sub(r"[\s-]+", "", str(value or "").upper().translate(_ARABIC_DIGITS))
    return compact


def _normalise_section(value: object) -> str:
    return re.sub(r"[\s-]+", "", str(value or "").upper().translate(_ARABIC_DIGITS))


def _normalise_time(value: object) -> str:
    text = str(value or "").strip().translate(_ARABIC_DIGITS)
    hour, separator, minute = text.partition(":")
    return f"{hour.zfill(2)}:{minute}" if separator and hour.isdigit() else text


def _distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    return min(abs(left[0] - right[1]), abs(right[0] - left[1]))


def _answer_facts(answer: object, *, known_rooms: set[str]) -> dict[str, set[Any]]:
    text = str(answer or "").translate(_ARABIC_DIGITS)
    section_matches = list(_SECTION.finditer(text))
    section_spans = [match.span() for match in section_matches]

    # YM4/YF4 match the broad shape of a short course code.  Exclude tokens already
    # recognised as sections instead of teaching the evaluator branch-specific
    # course names.
    course_matches = [
        match
        for match in _COURSE_CODE.finditer(text)
        if not any(start <= match.start() and match.end() <= end for start, end in section_spans)
        # A room such as LAB201 has the same lexical shape as a course.  Exclude only
        # room values that the provider evidence actually supplied.  Prefix-filtering
        # is unsafe: it silently discards a fabricated course such as DS999 whenever
        # the evidence happened to contain only AI-prefixed courses.
        and _normalise_course_code("".join(match.groups())) not in known_rooms
    ]
    course_codes = {_normalise_course_code("".join(match.groups())) for match in course_matches}
    course_section_rows: set[tuple[str, str]] = set()
    course_time_rows: set[tuple[str, str]] = set()
    meeting_rows: set[tuple[str, str, str, str]] = set()

    for clause in _CLAUSE_BOUNDARY.split(text):
        clause_courses = list(_COURSE_CODE.finditer(clause))
        clause_sections = list(_SECTION.finditer(clause))
        section_clause_spans = [match.span() for match in clause_sections]
        clause_courses = [
            match
            for match in clause_courses
            if not any(
                start <= match.start() and match.end() <= end for start, end in section_clause_spans
            )
            and _normalise_course_code("".join(match.groups())) not in known_rooms
        ]
        clause_times = list(_CLOCK_TIME.finditer(clause))
        for course_match in clause_courses:
            code = _normalise_course_code("".join(course_match.groups()))
            nearest_section = (
                min(clause_sections, key=lambda match: _distance(course_match.span(), match.span()))
                if clause_sections
                else None
            )
            section = (
                _normalise_section("".join(nearest_section.groups()))
                if nearest_section is not None
                else ""
            )
            if section:
                course_section_rows.add((code, section))

            owned_times = [
                match
                for match in clause_times
                if min(
                    clause_courses,
                    key=lambda candidate: _distance(candidate.span(), match.span()),
                )
                is course_match
            ]
            normalised_times = [_normalise_time(match.group(1)) for match in owned_times]
            course_time_rows.update((code, value) for value in normalised_times)
            for offset in range(0, len(normalised_times) - 1, 2):
                if section:
                    meeting_rows.add(
                        (code, section, normalised_times[offset], normalised_times[offset + 1])
                    )

    return {
        "course_codes": course_codes,
        "section_labels": {
            _normalise_section("".join(match.groups())) for match in section_matches
        },
        "credit_quantities": {int(match.group(1)) for match in _CREDIT_QUANTITY.finditer(text)},
        "meeting_times": {_normalise_time(match.group(1)) for match in _CLOCK_TIME.finditer(text)},
        "course_section_rows": course_section_rows,
        "course_time_rows": course_time_rows,
        "meeting_rows": meeting_rows,
    }


def _source_result(row: dict[str, Any], source_tool: str) -> dict[str, Any] | None:
    # This field is captured from the actual role=tool messages handed to the
    # provider.  Never fall back to agent.tool_results: those are the richer local
    # records, and using them would authorize facts the model was never shown.
    candidates = [
        result
        for result in (row.get("provider_tool_results") or [])
        if isinstance(result, dict)
        and result.get("tool") == source_tool
        and result.get("ok") is not False
    ]
    return candidates[-1] if candidates else None


def _evidence_facts(source: dict[str, Any]) -> dict[str, set[Any]]:
    registrations = [
        registration
        for registration in (source.get("registrations") or [])
        if isinstance(registration, dict)
    ]
    course_codes = {
        _normalise_course_code(registration.get("course_code"))
        for registration in registrations
        if registration.get("course_code")
    }
    section_labels = {
        _normalise_section(registration.get("section"))
        for registration in registrations
        if registration.get("section")
    }
    meetings = [meeting for meeting in (source.get("meetings") or []) if isinstance(meeting, dict)]
    registration_rows = {
        (
            _normalise_course_code(registration.get("course_code")),
            _normalise_section(registration.get("section")),
        )
        for registration in registrations
        if registration.get("course_code") and registration.get("section")
    }
    meeting_rows = {
        (
            _normalise_course_code(meeting.get("course_code")),
            _normalise_section(meeting.get("section")),
            str(meeting.get("day") or "").strip().upper(),
            _normalise_time(meeting.get("start") or meeting.get("start_time")),
            _normalise_time(meeting.get("end") or meeting.get("end_time")),
        )
        for meeting in meetings
        if meeting.get("course_code")
        and meeting.get("section")
        and (meeting.get("start") or meeting.get("start_time"))
        and (meeting.get("end") or meeting.get("end_time"))
    }
    meeting_course_rows = {(code, section) for code, section, *_rest in meeting_rows}
    course_section_rows = registration_rows | meeting_course_rows
    course_codes.update(code for code, _section in course_section_rows)
    section_labels.update(section for _code, section in course_section_rows)

    credit_quantities = {
        int(registration["credits"])
        for registration in registrations
        if isinstance(registration.get("credits"), int | float)
    }
    for key in ("registered_credit_hours", "expected_credit_hours"):
        value = source.get(key)
        if isinstance(value, int | float):
            credit_quantities.add(int(value))
    # Production-shaped fixture payloads should carry the explicit total, but older
    # traces did not.  The de-duplicated registrations still make it derivable.
    if registrations and not {
        "registered_credit_hours",
        "expected_credit_hours",
    }.intersection(source):
        credit_quantities.add(
            sum(int(registration.get("credits") or 0) for registration in registrations)
        )

    return {
        "course_codes": course_codes,
        "section_labels": section_labels,
        "credit_quantities": credit_quantities,
        "meeting_times": {
            value for _code, _section, _day, start, end in meeting_rows for value in (start, end)
        },
        "course_section_rows": course_section_rows,
        "course_time_rows": {
            (code, value)
            for code, _section, _day, start, end in meeting_rows
            for value in (start, end)
        },
        "meeting_rows": {
            (code, section, start, end) for code, section, _day, start, end in meeting_rows
        },
        "room_labels": {
            _normalise_course_code(meeting.get("room"))
            for meeting in meetings
            if meeting.get("room")
        },
    }


def _nearest_label(text: str, start: int, end: int) -> str | None:
    """Classify one typed quantity by the nearest semantic label in its clause."""

    candidates: list[tuple[int, str]] = []
    for label, words in (
        ("passed", _PASSED_WORDS),
        ("remaining", _REMAINING_WORDS),
        ("total", _TOTAL_WORDS),
    ):
        for word in words:
            for match in re.finditer(re.escape(word), text, re.I):
                distance = min(abs(start - match.end()), abs(match.start() - end))
                candidates.append((distance, label))
    return min(candidates)[1] if candidates else None


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(word.casefold() in lowered for word in words)


def _quantity_matches(
    clause: str,
    number_first: re.Pattern[str],
    label_first: re.Pattern[str],
) -> list[re.Match[str]]:
    """Return de-duplicated typed quantities in either display order.

    A label-first pattern must not reuse the unit from a preceding number-first
    quantity.  For example, in ``16 courses and 48 credits`` the word ``courses``
    cannot label 48 as another course count.
    """

    matches: list[re.Match[str]] = []
    seen_value_spans: set[tuple[int, int]] = set()
    for pattern, is_label_first in ((number_first, False), (label_first, True)):
        for match in pattern.finditer(clause):
            explicit_heading = re.match(r"(?:عدد|number\s+of)\b", match.group(0).lstrip(), re.I)
            if (
                is_label_first
                and explicit_heading is None
                and re.search(r"\d\s*$", clause[: match.start()])
            ):
                continue
            value_span = match.span(1)
            if value_span not in seen_value_spans:
                seen_value_spans.add(value_span)
                matches.append(match)
    return matches


def _progress_answer_facts(answer: object) -> dict[str, set[int]]:
    facts = {
        "passed_course_counts": set(),
        "total_course_counts": set(),
        "remaining_course_counts": set(),
        "passed_credit_counts": set(),
        "total_credit_counts": set(),
        "remaining_credit_counts": set(),
        "passed_plan_credit_counts": set(),
        "registrar_earned_credit_counts": set(),
        "completion_percentages": set(),
        "additional_term_counts": set(),
        "including_baseline_term_counts": set(),
        "post_baseline_remaining_course_counts": set(),
        "post_baseline_remaining_credit_counts": set(),
    }
    text = str(answer or "").translate(_ARABIC_DIGITS)
    for clause in _CLAUSE_BOUNDARY.split(text):
        post_baseline = _contains_any(clause, _POST_BASELINE_WORDS)
        for unit, patterns in (
            ("course", (_COURSE_QUANTITY, _PREFIX_COURSE_QUANTITY)),
            ("credit", (_CREDIT_QUANTITY, _PREFIX_CREDIT_QUANTITY)),
        ):
            for match in _quantity_matches(clause, *patterns):
                label = _nearest_label(clause, match.start(1), match.end(1))
                if label:
                    value = int(match.group(1))
                    if label == "remaining" and post_baseline:
                        facts[f"post_baseline_remaining_{unit}_counts"].add(value)
                    else:
                        facts[f"{label}_{unit}_counts"].add(value)
                    if label == "passed" and unit == "credit" and not post_baseline:
                        if _contains_any(clause, _PLAN_WORDS):
                            facts["passed_plan_credit_counts"].add(value)
                        if _contains_any(clause, _REGISTRAR_WORDS):
                            facts["registrar_earned_credit_counts"].add(value)

        # Natural summaries frequently say "32 out of 48 courses".  The first
        # number has no adjacent unit, so a unit-only extractor silently loses the
        # passed count.  Ratio extraction supplies the two explicitly labelled ends.
        ratio_patterns = (
            (
                "course",
                re.compile(
                    r"(?<!\d)(\d{1,3})\s*(?:من\s+(?:أصل|اصل)?|out\s+of|of)\s*"
                    r"(\d{1,3})\s*(?:مقرر\w*|مواد|مادة|courses?)(?![A-Z])",
                    re.I,
                ),
            ),
            (
                "credit",
                re.compile(
                    r"(?<!\d)(\d{1,3})\s*(?:من\s+(?:أصل|اصل)?|out\s+of|of)\s*"
                    r"(\d{1,3})\s*(?:ساعة|ساعات|ساعه|credit\s*hours?|credits?)(?![A-Z])",
                    re.I,
                ),
            ),
        )
        for unit, pattern in ratio_patterns:
            for match in pattern.finditer(clause):
                passed_value = int(match.group(1))
                facts[f"passed_{unit}_counts"].add(passed_value)
                facts[f"total_{unit}_counts"].add(int(match.group(2)))
                if unit == "credit":
                    if _contains_any(clause, _PLAN_WORDS):
                        facts["passed_plan_credit_counts"].add(passed_value)
                    if _contains_any(clause, _REGISTRAR_WORDS):
                        facts["registrar_earned_credit_counts"].add(passed_value)

        facts["completion_percentages"].update(
            int(match.group(1)) for match in _PERCENTAGE.finditer(clause)
        )
        for match in _quantity_matches(clause, _TERM_QUANTITY, _PREFIX_TERM_QUANTITY):
            value = int(match.group(1))
            if _contains_any(clause, _INCLUDING_BASELINE_TERM_WORDS):
                facts["including_baseline_term_counts"].add(value)
            elif _contains_any(clause, _ADDITIONAL_TERM_WORDS):
                facts["additional_term_counts"].add(value)
    return facts


def _progress_evidence_facts(source: dict[str, Any]) -> dict[str, set[int]]:
    facts = {
        "passed_course_counts": set(),
        "total_course_counts": set(),
        "remaining_course_counts": set(),
        "passed_credit_counts": set(),
        "total_credit_counts": set(),
        "remaining_credit_counts": set(),
        "passed_plan_credit_counts": set(),
        "registrar_earned_credit_counts": set(),
        "completion_percentages": set(),
        "additional_term_counts": set(),
        "including_baseline_term_counts": set(),
        "post_baseline_remaining_course_counts": set(),
        "post_baseline_remaining_credit_counts": set(),
    }

    if source.get("tool") == "my_progress":
        counts = source.get("counts") if isinstance(source.get("counts"), dict) else {}
        if isinstance(counts.get("passed"), int | float):
            facts["passed_course_counts"].add(int(counts["passed"]))
        # `my_progress.counts` has overlapping readiness buckets, so total and
        # remaining plan counts are intentionally not derived from their sum.
        return facts

    field_map = {
        "plan_courses_passed": "passed_course_counts",
        "plan_courses_total": "total_course_counts",
        "courses_remaining": "remaining_course_counts",
        "credits_remaining_in_plan": "remaining_credit_counts",
    }
    for field, fact_type in field_map.items():
        value = source.get(field)
        if isinstance(value, int | float):
            facts[fact_type].add(int(value))

    passed_in_plan = source.get("passed_credits_in_plan")
    if isinstance(passed_in_plan, int | float):
        facts["passed_credit_counts"].add(int(passed_in_plan))
        facts["passed_plan_credit_counts"].add(int(passed_in_plan))
    registrar_earned = source.get("credits_earned_registrar")
    if isinstance(registrar_earned, int | float):
        facts["passed_credit_counts"].add(int(registrar_earned))
        facts["registrar_earned_credit_counts"].add(int(registrar_earned))
    remaining = source.get("credits_remaining_in_plan")
    if isinstance(passed_in_plan, int | float) and isinstance(remaining, int | float):
        facts["total_credit_counts"].add(int(passed_in_plan + remaining))
    post_baseline_courses = source.get("courses_remaining_after_planning_baseline")
    if isinstance(post_baseline_courses, int | float):
        facts["post_baseline_remaining_course_counts"].add(int(post_baseline_courses))
    post_baseline_credits = source.get("credits_remaining_after_planning_baseline")
    if isinstance(post_baseline_credits, int | float):
        facts["post_baseline_remaining_credit_counts"].add(int(post_baseline_credits))
    percent = source.get("percent_complete")
    if isinstance(percent, int | float):
        facts["completion_percentages"].add(int(percent))
    for field in (
        "minimum_terms_by_prerequisites",
        "minimum_terms_by_credit_capacity_after_planning_baseline",
        "lower_bound_additional_terms",
        "estimated_additional_terms",
    ):
        value = source.get(field)
        if isinstance(value, int | float):
            facts["additional_term_counts"].add(int(value))
    for field in (
        "lower_bound_terms_including_planning_baseline",
        "estimated_terms_including_planning_baseline",
        "terms_estimate",
    ):
        value = source.get(field)
        if isinstance(value, int | float):
            facts["including_baseline_term_counts"].add(int(value))
    return facts


def _proposal_rows(
    value: object,
) -> tuple[set[tuple[str, str]], set[tuple[str, str, str, str, str]]]:
    """Canonical course/meeting rows shared by tool and presentation payloads."""

    if not isinstance(value, dict):
        return set(), set()
    course_rows: set[tuple[str, str]] = set()
    meeting_rows: set[tuple[str, str, str, str, str]] = set()

    if str(value.get("mode") or "") != "from_scratch":
        for field in ("baseline_sections", "current_sections", "expected_plan_sections"):
            for course in value.get(field) or []:
                if not isinstance(course, dict):
                    continue
                code = _normalise_course_code(course.get("course_code") or course.get("code"))
                section = _normalise_section(course.get("section") or course.get("section_label"))
                if code and section:
                    course_rows.add((code, section))
                for raw_meeting in course.get("meetings") or []:
                    if isinstance(raw_meeting, str):
                        matched = _MEETING_STRING.match(raw_meeting)
                        if matched and code and section:
                            day, start, end = matched.groups()
                            meeting_rows.add(
                                (
                                    code,
                                    section,
                                    day.upper(),
                                    _normalise_time(start),
                                    _normalise_time(end),
                                )
                            )
                    elif isinstance(raw_meeting, dict):
                        row = (
                            code,
                            section,
                            str(raw_meeting.get("day") or "").strip().upper(),
                            str(
                                raw_meeting.get("start") or raw_meeting.get("start_time") or ""
                            ).strip(),
                            str(
                                raw_meeting.get("end") or raw_meeting.get("end_time") or ""
                            ).strip(),
                        )
                        if all(row):
                            meeting_rows.add(
                                (
                                    row[0],
                                    row[1],
                                    row[2],
                                    _normalise_time(row[3]),
                                    _normalise_time(row[4]),
                                )
                            )

    for alternative in value.get("alternatives") or []:
        if not isinstance(alternative, dict):
            continue
        for course in alternative.get("courses") or []:
            if not isinstance(course, dict):
                continue
            code = _normalise_course_code(course.get("course_code") or course.get("code"))
            section = _normalise_section(course.get("section") or course.get("section_label"))
            if code and section:
                course_rows.add((code, section))
        for meeting in alternative.get("meetings") or []:
            if not isinstance(meeting, dict):
                continue
            row = (
                _normalise_course_code(meeting.get("course_code") or meeting.get("code")),
                _normalise_section(meeting.get("section") or meeting.get("section_label")),
                str(meeting.get("day") or "").strip().upper(),
                _normalise_time(meeting.get("start") or meeting.get("start_time")),
                _normalise_time(meeting.get("end") or meeting.get("end_time")),
            )
            if all(row):
                meeting_rows.add(row)
    return course_rows, meeting_rows


def _proposal_evidence_facts(source: dict[str, Any]) -> dict[str, set[Any]]:
    course_rows, meeting_rows = _proposal_rows(source)
    unplaced_codes = {
        _normalise_course_code(row.get("course_code") or row.get("code"))
        for container in [source, *(source.get("alternatives") or [])]
        if isinstance(container, dict)
        for row in (container.get("unplaced_courses") or [])
        if isinstance(row, dict) and (row.get("course_code") or row.get("code"))
    }
    return {
        "course_codes": {code for code, _section in course_rows} | unplaced_codes,
        "section_labels": {section for _code, section in course_rows},
        "credit_quantities": set(),
        "meeting_times": {
            time for _code, _section, _day, start, end in meeting_rows for time in (start, end)
        },
        "course_section_rows": course_rows,
        "course_time_rows": {
            (code, value)
            for code, _section, _day, start, end in meeting_rows
            for value in (start, end)
        },
        "meeting_rows": {
            (code, section, start, end) for code, section, _day, start, end in meeting_rows
        },
        "room_labels": set(),
    }


def _matching_proposal_presentation(
    row: dict[str, Any], source: dict[str, Any]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    presentation = row.get("presentation")
    if not isinstance(presentation, dict) or presentation.get("kind") != "timetable_proposals":
        return {"structured_presentation": ["timetable_proposals"]}, {}

    source_courses, source_meetings = _proposal_rows(source)
    shown_courses, shown_meetings = _proposal_rows(presentation)
    missing: dict[str, list[str]] = {}
    unsupported: dict[str, list[str]] = {}
    if source_courses - shown_courses:
        missing["presentation_course_rows"] = sorted(
            f"{code}/{section}" for code, section in source_courses - shown_courses
        )
    if shown_courses - source_courses:
        extra_courses = sorted(
            f"{code}/{section}" for code, section in shown_courses - source_courses
        )
        unsupported["presentation_course_rows"] = extra_courses
        missing.setdefault("presentation_course_rows", []).extend(extra_courses)
    if source_meetings - shown_meetings:
        missing["presentation_meeting_rows"] = sorted(
            "/".join(values) for values in source_meetings - shown_meetings
        )
    if shown_meetings - source_meetings:
        extra_meetings = sorted("/".join(values) for values in shown_meetings - source_meetings)
        unsupported["presentation_meeting_rows"] = extra_meetings
        missing.setdefault("presentation_meeting_rows", []).extend(extra_meetings)
    return missing, unsupported


def _provider_evidence_gaps(
    *, profile: str, source: dict[str, Any], expected: dict[str, set[Any]], required: set[str]
) -> dict[str, list[str]]:
    """Prove that an "all rows" contract is representable in provider evidence.

    A projected timetable can contain three as its registrar count but only two
    course rows because a course without a meeting was removed at projection.  In
    that state no answer can truthfully enumerate all three.  Treating the two visible
    rows as the whole list would turn a privacy projection gap into a green eval.
    """

    if profile != "registration_listing" or not required.intersection(
        {
            "all_registration_course_codes",
            "all_registration_sections",
            "all_course_section_rows",
        }
    ):
        return {}
    total = source.get("registered_course_count")
    if not isinstance(total, int | float):
        total = source.get("expected_course_count")
    if not isinstance(total, int | float):
        return {}
    visible = len(expected.get("course_codes") or ())
    if visible >= int(total):
        return {}
    return {"provider_registration_rows": [f"{visible}/{int(total)} visible"]}


def check_answer_evidence(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Check support and requested-evidence completeness for one scored row.

    Cases without an ``answer_evidence_contract`` retain the historical behaviour.
    A contracted case without its saved source payload is not silently waived: the
    answer cannot be proven complete or supported, so both checks fail visibly.
    """

    contract = case.get("answer_evidence_contract")
    if not contract:
        return {
            "enabled": False,
            "evaluable": True,
            "support_ok": True,
            "completeness_ok": True,
            "missing": {},
            "unsupported": {},
        }

    source_tool = str(contract["source_tool"])
    profile = str(contract.get("profile") or "registration_listing")
    required = set(contract.get("require") or [])
    verify = set(contract.get("verify") or [])
    source = _source_result(row, source_tool)
    if source is None:
        return {
            "enabled": True,
            "evaluable": False,
            "source_tool": source_tool,
            "profile": profile,
            "support_ok": False,
            "completeness_ok": False,
            "missing": {"source_tool_result": [source_tool]},
            "unsupported": {},
        }

    if profile == "graduation_summary":
        expected = _progress_evidence_facts(source)
        observed = _progress_answer_facts(row.get("answer"))
    else:
        expected = (
            _proposal_evidence_facts(source)
            if profile == "timetable_proposal"
            else _evidence_facts(source)
        )
        observed = _answer_facts(
            row.get("answer"), known_rooms=set(expected.get("room_labels") or ())
        )

    missing: dict[str, list[Any]] = _provider_evidence_gaps(
        profile=profile,
        source=source,
        expected=expected,
        required=required,
    )
    if "all_registration_course_codes" in required:
        values = sorted(expected["course_codes"] - observed["course_codes"])
        if values:
            missing["course_codes"] = values
    if "all_registration_sections" in required:
        values = sorted(expected["section_labels"] - observed["section_labels"])
        if values:
            missing["section_labels"] = values
    if "all_course_section_rows" in required:
        values = sorted(expected["course_section_rows"] - observed["course_section_rows"])
        if values:
            missing["course_section_rows"] = ["/".join(value) for value in values]
    presentation_unsupported: dict[str, list[Any]] = {}
    if "matching_structured_presentation" in required:
        presentation_missing, presentation_unsupported = _matching_proposal_presentation(
            row, source
        )
        missing.update(presentation_missing)
    for fact_type in (
        "passed_course_counts",
        "passed_credit_counts",
        "total_course_counts",
        "total_credit_counts",
        "remaining_course_counts",
        "remaining_credit_counts",
    ):
        if fact_type in required:
            # A semantic fact may have more than one authorised representation.
            # `passed_credit_counts`, for example, distinguishes in-plan credits
            # from the registrar total; stating either exact figure fulfils a broad
            # "passed credits" request, while support checking still rejects any
            # third value.
            if not (expected[fact_type] & observed[fact_type]):
                missing[fact_type] = sorted(expected[fact_type])

    unsupported: dict[str, list[Any]] = dict(presentation_unsupported)
    for claim_type in verify:
        if claim_type == "progress_figures":
            for fact_type, claimed in observed.items():
                values = sorted(claimed - expected[fact_type])
                if values:
                    unsupported[fact_type] = values
        else:
            values = sorted(observed[claim_type] - expected[claim_type])
            if values:
                unsupported[claim_type] = [
                    "/".join(value) if isinstance(value, tuple) else value for value in values
                ]

    return {
        "enabled": True,
        "evaluable": True,
        "source_tool": source_tool,
        "profile": profile,
        "schedule_kind": source.get("schedule_kind"),
        "evidence_source": "provider_tool_results",
        "expected": {
            key: sorted(values) for key, values in expected.items() if key != "room_labels"
        },
        "observed": {key: sorted(values) for key, values in observed.items()},
        "missing": missing,
        "unsupported": unsupported,
        "support_ok": not unsupported,
        "completeness_ok": not missing,
    }


__all__ = [
    "VALID_PROFILES",
    "VALID_REQUIRED_FACTS",
    "VALID_VERIFIED_CLAIMS",
    "check_answer_evidence",
]
