from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Any

from core.models import TermSection, TermSectionMeeting
from core.services.section_programmes import filter_sections_for_program
from core.services.student_sections import gender_section_filter

try:
    from ortools.sat.python import cp_model  # type: ignore
except Exception:  # pragma: no cover
    cp_model = None

DAY_MAP = {
    "OU,O�O-O_": "SUN",
    "OU,OO�U+USU+": "MON",
    "OU,O�U,OO�OO�": "TUE",
    'OU,O�O�O"O1OO�': "WED",
    "OU,OrU.USO3": "THU",
    "Sunday": "SUN",
    "Monday": "MON",
    "Tuesday": "TUE",
    "Wednesday": "WED",
    "Thursday": "THU",
}


@dataclass
class Meeting:
    day: str
    start: str
    end: str


@dataclass
class OccupiedSlot:
    meeting: Meeting
    label: str


# Cache conflict-pair matrix across repeated builder runs / profile solves.
_CONFLICT_MATRIX_CACHE: dict[tuple, list[tuple[int, int]]] = {}
_CONFLICT_MATRIX_CACHE_ORDER: list[tuple] = []
_CONFLICT_MATRIX_CACHE_MAX = 64
_CACHE_LOCK = threading.Lock()

_DAY_INDEX = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_SLOT_MINUTES = 5
_SLOTS_PER_DAY = 24 * 60 // _SLOT_MINUTES  # 288
_TOTAL_WEEK_SLOTS = 7 * _SLOTS_PER_DAY  # 2016


def _norm_course_key(value: Any) -> str:
    return str(value or "").replace(" ", "").upper()


def _nonnegative_int_or_none(value: Any) -> int | None:
    """Return a stored non-negative count without confusing NULL with zero."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _remaining_capacity(option: dict[str, Any]) -> int | None:
    """Known seats left, or ``None`` when either capacity fact is unknown.

    ``TermSection.available_capacity`` is the section's maximum capacity despite
    its historical name; ``registered_count`` is the current occupancy.
    """
    maximum = _nonnegative_int_or_none(option.get("available_capacity"))
    registered = _nonnegative_int_or_none(option.get("registered_count"))
    if maximum is None or registered is None:
        return None
    return max(0, maximum - registered)


def _known_full_section(option: dict[str, Any]) -> bool:
    remaining = _remaining_capacity(option)
    return remaining == 0 if remaining is not None else False


def _option_course_key(option: dict[str, Any]) -> str:
    key = _norm_course_key(option.get("course_key"))
    if key:
        return key
    code = _norm_course_key(option.get("course_code"))
    number = _norm_course_key(option.get("course_number"))
    if code and number and number != code:
        return _norm_course_key(f"{code}{number}")
    return code or number


def _display_course_label(row: dict[str, Any]) -> str:
    code = _option_course_key(row)
    section = str(row.get("section", "") or "").strip()
    return f"{code} {section}".strip()


def _to_minutes(t: str) -> int:
    """Minutes since midnight from 'HH:MM'. Returns -1 for unparseable/empty
    input so one dirty time string never aborts the whole build; callers treat a
    negative start (or end <= start) as 'no valid meeting'."""
    try:
        hh, mm = str(t).strip().split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError, TypeError):
        return -1


def _overlap(a: Meeting, b: Meeting) -> bool:
    if a.day != b.day:
        return False
    a_s, a_e = _to_minutes(a.start), _to_minutes(a.end)
    b_s, b_e = _to_minutes(b.start), _to_minutes(b.end)
    if a_s < 0 or a_e <= a_s or b_s < 0 or b_e <= b_s:
        return False  # malformed/zero-length meeting -> treat as non-conflicting
    return a_s < b_e and b_s < a_e


def _slot_text(m: Meeting) -> str:
    return f"{m.day} {m.start}-{m.end}"


def _meeting_mask(m: Meeting) -> int:
    day = str(m.day or "").upper()[:3]
    day_idx = _DAY_INDEX.get(day)
    if day_idx is None:
        return 0
    st = _to_minutes(m.start)
    en = _to_minutes(m.end)
    if st < 0 or en <= st:
        return 0
    start_idx = (day_idx * 24 * 60 + st) // _SLOT_MINUTES
    end_idx = (day_idx * 24 * 60 + en) // _SLOT_MINUTES
    start_idx = max(0, min(_TOTAL_WEEK_SLOTS, start_idx))
    end_idx = max(0, min(_TOTAL_WEEK_SLOTS, end_idx))
    if end_idx <= start_idx:
        return 0
    return ((1 << (end_idx - start_idx)) - 1) << start_idx


def _section_mask(meetings: list[Meeting]) -> int:
    mask = 0
    for m in meetings:
        mask |= _meeting_mask(m)
    return mask


def _day_count_from_mask(mask: int) -> int:
    c = 0
    for di in range(7):
        day_mask = ((1 << _SLOTS_PER_DAY) - 1) << (di * _SLOTS_PER_DAY)
        if mask & day_mask:
            c += 1
    return c


def _section_day_masks(meetings: list[Meeting]) -> tuple[int, int, int, int, int, int, int]:
    out = [0, 0, 0, 0, 0, 0, 0]
    for m in meetings:
        d = str(m.day or "").upper()[:3]
        di = _DAY_INDEX.get(d)
        if di is None:
            continue
        st = _to_minutes(m.start)
        en = _to_minutes(m.end)
        if st < 0 or en <= st:
            continue
        s = st // _SLOT_MINUTES
        e = en // _SLOT_MINUTES
        if e <= s:
            continue
        out[di] |= ((1 << (e - s)) - 1) << s
    return (out[0], out[1], out[2], out[3], out[4], out[5], out[6])


def _gap_minutes_from_meetings(meetings: list[Meeting]) -> int:
    by_day: dict[str, list[tuple[int, int]]] = {}
    for m in meetings:
        d = str(m.day or "").upper()[:3]
        st = _to_minutes(m.start)
        en = _to_minutes(m.end)
        if st < 0 or en <= st:
            continue
        by_day.setdefault(d, []).append((st, en))

    total = 0
    for day_ranges in by_day.values():
        day_ranges.sort()
        merged: list[tuple[int, int]] = []
        for st, en in day_ranges:
            if not merged or st > merged[-1][1]:
                merged.append((st, en))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        for i in range(1, len(merged)):
            total += max(0, merged[i][0] - merged[i - 1][1])
    return total


def _section_meetings(term_section_id: int) -> list[Meeting]:
    rows = TermSectionMeeting.objects.filter(
        term_section_id=term_section_id,
    ).values_list("day", "start_time", "end_time")
    out: list[Meeting] = []
    for d, s, e in rows:
        if not s or not e:
            continue
        out.append(
            Meeting(
                day=DAY_MAP.get(str(d or "").strip(), str(d or "").strip()),
                start=str(s),
                end=str(e),
            )
        )
    return out


def _strict_clock_minutes(value: Any) -> int | None:
    parts = str(value or "").strip().split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or second != 0:
        return None
    return hour * 60 + minute


def _catalog_for_courses(
    year: str,
    term: str,
    course_codes: list[str],
    gender: str = "",
    program: str | None = None,
) -> dict[str, list[dict[str, Any]]]:  # year/term kept for API compatibility
    if not course_codes:
        return {}
    wanted = {str(c).replace(" ", "").upper() for c in course_codes}
    qs = TermSection.objects.filter(scenario__isnull=True, course_key__in=wanted)
    # ``None`` means an intentionally unscoped staff build. A supplied blank
    # value came from a student profile and must fail closed.
    if program is not None:
        qs = filter_sections_for_program(qs, program)
    # Always applied, including for a staff build with no student in scope.
    # gender_section_filter's blank branch exists precisely to exclude the
    # other branch's YM/YF sections from an unscoped catalogue; skipping the
    # call entirely made that branch unreachable, so the staff build path went
    # on offering other-branch sections after every student-facing read had
    # stopped. A gendered value additionally narrows to the student's cohort.
    qs = qs.filter(gender_section_filter(gender))
    rows = list(
        qs.order_by("course_code", "course_number", "section").values_list(
            "id",
            "course_code",
            "course_number",
            "course_key",
            "section",
            "course_name",
            "registered_count",
            "available_capacity",
        )
    )
    meetings_by_section: dict[int, list[Meeting]] = {int(row[0]): [] for row in rows}
    meeting_issues_by_section: dict[int, list[str]] = {int(row[0]): [] for row in rows}
    for section_id, day, start, end in (
        TermSectionMeeting.objects.filter(term_section_id__in=meetings_by_section)
        .order_by("term_section_id", "day", "start_time", "end_time", "id")
        .values_list("term_section_id", "day", "start_time", "end_time")
    ):
        if not start or not end:
            meeting_issues_by_section[int(section_id)].append("MISSING_MEETING_DATA")
            continue
        raw_day = str(day or "").strip()
        meetings_by_section[int(section_id)].append(
            Meeting(
                day=DAY_MAP.get(raw_day, raw_day),
                start=str(start),
                end=str(end),
            )
        )
    for section_id, meetings in meetings_by_section.items():
        if not meetings and not meeting_issues_by_section[section_id]:
            meeting_issues_by_section[section_id].append("MISSING_MEETING_DATA")
    out: dict[str, list[dict[str, Any]]] = {}
    for sid, code, num, course_key, sec, name, reg, cap in rows:
        full = _norm_course_key(course_key) or _norm_course_key(f"{code or ''}{num or ''}")
        out.setdefault(full, []).append(
            {
                "term_section_id": int(sid),
                "course_code": full,
                "course_key": full,
                "course_number": "",
                "section": str(sec or ""),
                "course_name": str(name or ""),
                "registered_count": _nonnegative_int_or_none(reg),
                "available_capacity": _nonnegative_int_or_none(cap),
                "meetings": meetings_by_section[int(sid)],
                **(
                    {"meeting_issue_codes": meeting_issues_by_section[int(sid)]}
                    if meeting_issues_by_section[int(sid)]
                    else {}
                ),
            }
        )
    return out


def _complete_catalogue(
    catalog: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Exclude sections whose timetable cannot safely participate in a solver.

    A section with no meetings, a partly blank meeting set, or a malformed time
    must not look like an all-day-free option.  Keep bounded diagnostics so the
    caller can distinguish incomplete local evidence from an absent section.
    """

    complete: dict[str, list[dict[str, Any]]] = {}
    issues: dict[str, list[dict[str, Any]]] = {}
    for code, options in catalog.items():
        clean_options: list[dict[str, Any]] = []
        for option in options:
            reason_codes = {
                str(value).strip().upper()
                for value in option.get("meeting_issue_codes") or []
                if str(value).strip()
            }
            meetings = list(option.get("meetings") or [])
            if not meetings:
                reason_codes.add("MISSING_MEETING_DATA")
            for meeting in meetings:
                day = str(getattr(meeting, "day", "") or "").strip().upper()
                start = _strict_clock_minutes(getattr(meeting, "start", ""))
                end = _strict_clock_minutes(getattr(meeting, "end", ""))
                if day not in _DAY_INDEX:
                    reason_codes.add("INVALID_MEETING_DAY")
                if start is None or end is None or end <= start:
                    reason_codes.add("INVALID_MEETING_TIME")
            if reason_codes:
                issues.setdefault(code, []).append(
                    {
                        "section": str(option.get("section") or ""),
                        "reason_codes": sorted(reason_codes),
                    }
                )
            else:
                clean_options.append(option)
        complete[code] = clean_options
    return complete, issues


def _choose(
    shortlist: list[dict[str, Any]],
    catalog: dict[str, list[dict[str, Any]]],
    baseline: list[dict[str, Any]],
    keep_registered: bool,
    strategy: str = "A",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    occupied: list[OccupiedSlot] = []
    if keep_registered:
        for r in baseline:
            d = str(r.get("day", "")).strip()
            s = str(r.get("start_time", "")).strip()
            e = str(r.get("end_time", "")).strip()
            if d and s and e:
                m = Meeting(day=DAY_MAP.get(d, d), start=s, end=e)
                label = f"baseline {_display_course_label(r)}".strip()
                occupied.append(OccupiedSlot(meeting=m, label=label))

    selected: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []

    items = shortlist[:]

    def options_count(course_code: str) -> int:
        return len(catalog.get(str(course_code).upper(), []))

    if strategy == "A":
        # Max coverage: schedule constrained courses first (fewest section options)
        items.sort(
            key=lambda x: (
                0 if x.get("must_take") else 1,
                options_count(str(x.get("course_code", ""))),
                -(int(x.get("score", 0) or 0)),
                x.get("course_code", ""),
            )
        )
    elif strategy == "B":
        # Min disruption: prefer easier/flexible courses first, keep timetable stable
        items.sort(
            key=lambda x: (
                0 if x.get("must_take") else 1,
                -options_count(str(x.get("course_code", ""))),
                x.get("priority", ""),
                x.get("course_code", ""),
            )
        )
    else:
        # Balanced: combine must-take + score + flexibility
        items.sort(
            key=lambda x: (
                0 if x.get("must_take") else 1,
                -(int(x.get("score", 0) or 0)),
                options_count(str(x.get("course_code", ""))),
                x.get("course_code", ""),
            )
        )

    for c in items:
        code = str(c.get("course_code", "")).replace(" ", "").upper()
        status = str(c.get("status", "Eligible"))
        missing = c.get("missing_prerequisites", []) or []
        if status != "Eligible":
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": f"Blocked by prerequisites: {', '.join(missing)}",
                    "details": [],
                }
            )
            continue

        options = catalog.get(code, [])
        if not options:
            unscheduled.append(
                {"course_code": code, "reason": "No sections available", "details": []}
            )
            continue

        picked = None
        option_conflicts: list[dict[str, Any]] = []
        for opt in options:
            meetings = opt.get("meetings", [])
            conflicts_for_opt: list[str] = []
            for m in meetings:
                overlaps = [occ for occ in occupied if _overlap(m, occ.meeting)]
                for occ in overlaps:
                    conflicts_for_opt.append(
                        f"{_slot_text(m)} conflicts with {occ.label} ({_slot_text(occ.meeting)})"
                    )
            if not conflicts_for_opt:
                picked = opt
                break
            option_conflicts.append(
                {
                    "tried_section": f"{_option_course_key(opt)}:{opt.get('section', '')}",
                    "conflicts": conflicts_for_opt,
                }
            )

        if not picked:
            reason = "All sections conflict with baseline/selected timetable"
            if option_conflicts:
                reason = option_conflicts[0]["conflicts"][0]
            unscheduled.append({"course_code": code, "reason": reason, "details": option_conflicts})
            continue

        for m in picked.get("meetings", []):
            occupied.append(
                OccupiedSlot(
                    meeting=m,
                    label=f"selected {_display_course_label(picked)}",
                )
            )
        selected.append(picked)

    return selected, unscheduled


def _conflict_cache_key(option_by_sid: dict[int, dict[str, Any]]) -> tuple:
    sig: list[tuple] = []
    for sid in sorted(option_by_sid.keys()):
        opt = option_by_sid[sid]
        meetings = tuple(
            sorted((str(m.day), str(m.start), str(m.end)) for m in opt.get("meetings", []))
        )
        sig.append((sid, meetings))
    return tuple(sig)


def _get_conflict_pairs(option_by_sid: dict[int, dict[str, Any]]) -> list[tuple[int, int]]:
    key = _conflict_cache_key(option_by_sid)
    with _CACHE_LOCK:
        if key in _CONFLICT_MATRIX_CACHE:
            return _CONFLICT_MATRIX_CACHE[key]

    sid_list = sorted(option_by_sid.keys())
    pairs: list[tuple[int, int]] = []
    for i in range(len(sid_list)):
        a = sid_list[i]
        ma = option_by_sid[a].get("meetings", [])
        for j in range(i + 1, len(sid_list)):
            b = sid_list[j]
            mb = option_by_sid[b].get("meetings", [])
            conflict = False
            for x in ma:
                for y in mb:
                    if _overlap(x, y):
                        conflict = True
                        break
                if conflict:
                    break
            if conflict:
                pairs.append((a, b))

    with _CACHE_LOCK:
        _CONFLICT_MATRIX_CACHE[key] = pairs
        _CONFLICT_MATRIX_CACHE_ORDER.append(key)
        if len(_CONFLICT_MATRIX_CACHE_ORDER) > _CONFLICT_MATRIX_CACHE_MAX:
            old = _CONFLICT_MATRIX_CACHE_ORDER.pop(0)
            _CONFLICT_MATRIX_CACHE.pop(old, None)
    return pairs


def _bitmask_build_option_b(
    shortlist: list[dict[str, Any]],
    catalog: dict[str, list[dict[str, Any]]],
    baseline: list[dict[str, Any]],
    keep_registered: bool,
    strict_per_course: bool,
    consider_capacity: bool,
    max_credits: int = 0,
    target_credits: int | None = None,
    credits_map: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Collect must_take codes for hard-constraint enforcement
    must_take_codes: set[str] = set()
    for c in shortlist:
        if c.get("must_take"):
            must_take_codes.add(str(c.get("course_code", "")).replace(" ", "").upper())

    eligible: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    unscheduled_codes: set[str] = set()

    for c in shortlist:
        code = str(c.get("course_code", "")).replace(" ", "").upper()
        if str(c.get("status", "Eligible")) != "Eligible":
            missing = c.get("missing_prerequisites", []) or []
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": f"Blocked by prerequisites: {', '.join(missing)}",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            continue
        opts = catalog.get(code, [])
        if not opts:
            unscheduled.append(
                {"course_code": code, "reason": "No sections available", "details": []}
            )
            unscheduled_codes.add(code)
            continue
        eligible.append({**c, "_code": code})

    if not eligible:
        return [], unscheduled

    baseline_mask = 0
    if keep_registered:
        for r in baseline:
            d = str(r.get("day", "")).strip()
            s = str(r.get("start_time", "")).strip()
            e = str(r.get("end_time", "")).strip()
            if d and s and e:
                baseline_mask |= _meeting_mask(Meeting(day=DAY_MAP.get(d, d), start=s, end=e))

    course_options: list[tuple[str, list[dict[str, Any]]]] = []
    strict_blockers: list[str] = []
    for c in eligible:
        code = str(c["_code"])
        filtered_opts: list[dict[str, Any]] = []
        for opt in catalog.get(code, []):
            meetings = opt.get("meetings", [])
            msk = _section_mask(meetings)
            if keep_registered and (msk & baseline_mask) != 0:
                continue
            filtered_opts.append({**opt, "_mask": msk})
        if not filtered_opts:
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "No non-conflicting sections available",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            if strict_per_course or code in must_take_codes:
                strict_blockers.append(code)
            continue
        course_options.append((code, filtered_opts))

    if strict_blockers:
        for code in strict_blockers:
            if code in unscheduled_codes:
                continue
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Strict policy requires exactly one section, but none is available",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
        return [], unscheduled

    if not course_options:
        return [], unscheduled

    # Shuffle sections within each course so equal-weight picks vary per run
    for _code, _opts in course_options:
        random.shuffle(_opts)

    # smaller branching first, random tie-break among equal branch counts
    random.shuffle(course_options)
    course_options.sort(key=lambda x: len(x[1]))

    best_score: tuple[int, int, int, int] | None = None
    best_selected: list[dict[str, Any]] = []

    def score_of(selected_opts: list[dict[str, Any]], mask: int) -> tuple[int, int, int, int]:
        scheduled = len(selected_opts)
        day_count = _day_count_from_mask(mask)
        all_meetings: list[Meeting] = []
        cap_total = 0
        for o in selected_opts:
            all_meetings.extend(o.get("meetings", []))
            if consider_capacity:
                cap_total += int(_remaining_capacity(o) or 0)
        gap_minutes = _gap_minutes_from_meetings(all_meetings)
        # maximize: scheduled, then fewer days, then fewer gaps, then more capacity
        return (scheduled, -day_count, -gap_minutes, cap_total)

    _cr_map = credits_map or {}
    _max_cr = max_credits if max_credits and max_credits > 0 else 0
    _target_cr = int(target_credits) if target_credits is not None else None
    remaining_credit_upper_bound = [0] * (len(course_options) + 1)
    for idx in range(len(course_options) - 1, -1, -1):
        code, _opts = course_options[idx]
        remaining_credit_upper_bound[idx] = remaining_credit_upper_bound[idx + 1] + int(
            _cr_map.get(code, 0) or 0
        )

    def dfs(i: int, used_mask: int, chosen: list[dict[str, Any]], used_cr: int) -> None:
        nonlocal best_score, best_selected

        if _target_cr is not None and (
            used_cr > _target_cr or used_cr + remaining_credit_upper_bound[i] < _target_cr
        ):
            return
        remaining = len(course_options) - i
        if best_score is not None and len(chosen) + remaining < best_score[0]:
            return

        if i >= len(course_options):
            if _target_cr is not None and used_cr != _target_cr:
                return
            cur = score_of(chosen, used_mask)
            if (
                best_score is None
                or cur > best_score
                or (cur == best_score and random.random() < 0.5)
            ):
                best_score = cur
                best_selected = [dict(x) for x in chosen]
            return

        code, opts = course_options[i]
        is_must = code in must_take_codes
        course_cr = _cr_map.get(code, 0)

        # relaxed mode allows skipping a course, but never a must-take
        if not strict_per_course and not is_must:
            dfs(i + 1, used_mask, chosen, used_cr)

        # credit-cap check: skip if adding this course would exceed the cap
        if (_max_cr and (used_cr + course_cr) > _max_cr) or (
            _target_cr is not None and (used_cr + course_cr) > _target_cr
        ):
            return

        for opt in opts:
            msk = int(opt.get("_mask") or 0)
            if (used_mask & msk) != 0:
                continue
            chosen.append(opt)
            dfs(i + 1, used_mask | msk, chosen, used_cr + course_cr)
            chosen.pop()

    dfs(0, baseline_mask if keep_registered else 0, [], 0)

    selected = [{k: v for k, v in opt.items() if k != "_mask"} for opt in best_selected]

    chosen_course: set[str] = set()
    for sel_item in selected:
        chosen_course.add(_option_course_key(sel_item))

    for c in eligible:
        code = str(c["_code"])
        if code in unscheduled_codes:
            continue
        if code not in chosen_course:
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Could not fit with chosen constraints/objective",
                    "details": [
                        {
                            "hint": "Profile B uses bitmask optimization: fewest days then smallest gaps"
                        }
                    ],
                }
            )

    return selected, unscheduled


def _bitmask_build_option_c(
    shortlist: list[dict[str, Any]],
    catalog: dict[str, list[dict[str, Any]]],
    baseline: list[dict[str, Any]],
    keep_registered: bool,
    strict_per_course: bool,
    max_credits: int = 0,
    target_credits: int | None = None,
    credits_map: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Collect must_take codes for hard-constraint enforcement
    must_take_codes: set[str] = set()
    for c in shortlist:
        if c.get("must_take"):
            must_take_codes.add(str(c.get("course_code", "")).replace(" ", "").upper())

    eligible: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    unscheduled_codes: set[str] = set()

    for c in shortlist:
        code = str(c.get("course_code", "")).replace(" ", "").upper()
        if str(c.get("status", "Eligible")) != "Eligible":
            missing = c.get("missing_prerequisites", []) or []
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": f"Blocked by prerequisites: {', '.join(missing)}",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            continue
        opts = catalog.get(code, [])
        if not opts:
            unscheduled.append(
                {"course_code": code, "reason": "No sections available", "details": []}
            )
            unscheduled_codes.add(code)
            continue
        eligible.append({**c, "_code": code})

    if not eligible:
        return [], unscheduled

    base_days = [0, 0, 0, 0, 0, 0, 0]
    if keep_registered:
        for r in baseline:
            d = str(r.get("day", "")).strip()
            s = str(r.get("start_time", "")).strip()
            e = str(r.get("end_time", "")).strip()
            if d and s and e:
                ms = _section_day_masks([Meeting(day=DAY_MAP.get(d, d), start=s, end=e)])
                for i in range(7):
                    base_days[i] |= ms[i]

    course_options: list[tuple[str, list[dict[str, Any]]]] = []
    strict_blockers: list[str] = []
    for c in eligible:
        code = str(c["_code"])
        filtered_opts: list[dict[str, Any]] = []
        for opt in catalog.get(code, []):
            meetings = opt.get("meetings", [])
            day_masks = _section_day_masks(meetings)
            if keep_registered and any((day_masks[i] & base_days[i]) != 0 for i in range(7)):
                continue
            filtered_opts.append({**opt, "_day_masks": day_masks})
        if not filtered_opts:
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "No non-conflicting sections available",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            if strict_per_course or code in must_take_codes:
                strict_blockers.append(code)
            continue
        course_options.append((code, filtered_opts))

    if strict_blockers:
        for code in strict_blockers:
            if code in unscheduled_codes:
                continue
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Strict policy requires exactly one section, but none is available",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
        return [], unscheduled

    if not course_options:
        return [], unscheduled

    # Shuffle sections within each course so equal-weight picks vary per run
    for _code, _opts in course_options:
        random.shuffle(_opts)

    # smaller branching first, random tie-break among equal branch counts
    random.shuffle(course_options)
    course_options.sort(key=lambda x: len(x[1]))

    best_key: tuple[int, int, int, int, int] | None = None
    best_selected: list[dict[str, Any]] = []

    def score_key(chosen: list[dict[str, Any]]) -> tuple[int, int, int, int, int]:
        meetings: list[Meeting] = []
        by_day: dict[str, list[tuple[int, int]]] = {}
        latest_finish = 0
        earliest_start = 24 * 60
        for o in chosen:
            for m in o.get("meetings", []):
                meetings.append(m)
                d = str(m.day or "").upper()[:3]
                st = _to_minutes(m.start)
                en = _to_minutes(m.end)
                if st < 0 or en <= st:
                    continue
                by_day.setdefault(d, []).append((st, en))
                latest_finish = max(latest_finish, en)
                earliest_start = min(earliest_start, st)

        days = len(by_day)
        gaps = _gap_minutes_from_meetings(meetings)
        if earliest_start == 24 * 60:
            earliest_start = 0
        # maximize scheduled, then minimize days, gaps, latest finish, then prefer later earliest start
        return (len(chosen), -days, -gaps, -latest_finish, earliest_start)

    _cr_map = credits_map or {}
    _max_cr = max_credits if max_credits and max_credits > 0 else 0
    _target_cr = int(target_credits) if target_credits is not None else None
    remaining_credit_upper_bound = [0] * (len(course_options) + 1)
    for idx in range(len(course_options) - 1, -1, -1):
        code, _opts = course_options[idx]
        remaining_credit_upper_bound[idx] = remaining_credit_upper_bound[idx + 1] + int(
            _cr_map.get(code, 0) or 0
        )

    def dfs(i: int, cur_days: list[int], chosen: list[dict[str, Any]], used_cr: int) -> None:
        nonlocal best_key, best_selected

        if _target_cr is not None and (
            used_cr > _target_cr or used_cr + remaining_credit_upper_bound[i] < _target_cr
        ):
            return
        remaining = len(course_options) - i
        if best_key is not None and len(chosen) + remaining < best_key[0]:
            return

        if i >= len(course_options):
            if _target_cr is not None and used_cr != _target_cr:
                return
            key = score_key(chosen)
            if best_key is None or key > best_key or (key == best_key and random.random() < 0.5):
                best_key = key
                best_selected = [dict(x) for x in chosen]
            return

        code, opts = course_options[i]
        is_must = code in must_take_codes
        course_cr = _cr_map.get(code, 0)

        # relaxed mode allows skipping a course, but never a must-take
        if not strict_per_course and not is_must:
            dfs(i + 1, cur_days, chosen, used_cr)

        # credit-cap check: skip if adding this course would exceed the cap
        if (_max_cr and (used_cr + course_cr) > _max_cr) or (
            _target_cr is not None and (used_cr + course_cr) > _target_cr
        ):
            return

        for opt in opts:
            dm: list[int] = opt.get("_day_masks") or [0] * 7
            if any((cur_days[d] & dm[d]) != 0 for d in range(7)):
                continue
            for d in range(7):
                cur_days[d] |= dm[d]
            chosen.append(opt)
            dfs(i + 1, cur_days, chosen, used_cr + course_cr)
            chosen.pop()
            for d in range(7):
                cur_days[d] ^= dm[d]

    dfs(0, base_days[:], [], 0)

    selected = [{k: v for k, v in o.items() if k != "_day_masks"} for o in best_selected]

    chosen_course: set[str] = set()
    for sel_item in selected:
        chosen_course.add(_option_course_key(sel_item))

    for c in eligible:
        code = str(c["_code"])
        if code in unscheduled_codes:
            continue
        if code not in chosen_course:
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Could not fit with chosen constraints/objective",
                    "details": [
                        {
                            "hint": "Profile C uses bitmask DFS lexicographic optimization (days, gaps)"
                        }
                    ],
                }
            )

    return selected, unscheduled


def _cp_build_option(
    shortlist: list[dict[str, Any]],
    catalog: dict[str, list[dict[str, Any]]],
    baseline: list[dict[str, Any]],
    keep_registered: bool,
    profile: str,
    strict_per_course: bool,
    consider_capacity: bool,
    max_credits: int = 0,
    target_credits: int | None = None,
    credits_map: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if cp_model is None:
        if target_credits is not None:
            # The greedy compatibility fallback cannot prove an exact credit
            # total.  Reuse the bounded DFS solver so exact requests remain
            # fail-closed even when OR-Tools is unavailable.
            return _bitmask_build_option_b(
                shortlist,
                catalog,
                baseline,
                keep_registered,
                strict_per_course=strict_per_course,
                consider_capacity=consider_capacity,
                max_credits=max_credits,
                target_credits=target_credits,
                credits_map=credits_map,
            )
        return _choose(shortlist, catalog, baseline, keep_registered, strategy=profile)

    # Collect must_take codes for hard-constraint enforcement
    must_take_codes: set[str] = set()
    for c in shortlist:
        if c.get("must_take"):
            must_take_codes.add(str(c.get("course_code", "")).replace(" ", "").upper())

    eligible: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    unscheduled_codes: set[str] = set()
    for c in shortlist:
        code = str(c.get("course_code", "")).replace(" ", "").upper()
        if str(c.get("status", "Eligible")) != "Eligible":
            missing = c.get("missing_prerequisites", []) or []
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": f"Blocked by prerequisites: {', '.join(missing)}",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            continue
        opts = catalog.get(code, [])
        if not opts:
            unscheduled.append(
                {"course_code": code, "reason": "No sections available", "details": []}
            )
            unscheduled_codes.add(code)
            continue
        eligible.append({**c, "_code": code})

    if not eligible:
        return [], unscheduled

    model = cp_model.CpModel()
    var_by_sid: dict[int, Any] = {}
    option_by_sid: dict[int, dict[str, Any]] = {}
    course_to_sids: dict[str, list[int]] = {}

    baseline_slots: list[Meeting] = []
    if keep_registered:
        for r in baseline:
            d = str(r.get("day", "")).strip()
            s = str(r.get("start_time", "")).strip()
            e = str(r.get("end_time", "")).strip()
            if d and s and e:
                baseline_slots.append(Meeting(day=DAY_MAP.get(d, d), start=s, end=e))

    for c in eligible:
        code = str(c["_code"])
        for opt in catalog.get(code, []):
            sid = int(opt.get("term_section_id") or 0)
            if sid <= 0:
                continue
            # If keep_registered is on, prune options conflicting with baseline
            if keep_registered:
                has_base_conflict = False
                for m in opt.get("meetings", []):
                    if any(_overlap(m, b) for b in baseline_slots):
                        has_base_conflict = True
                        break
                if has_base_conflict:
                    continue
            v = model.NewBoolVar(f"s_{sid}")
            var_by_sid[sid] = v
            option_by_sid[sid] = opt
            course_to_sids.setdefault(code, []).append(sid)

    # one section per course (strict/must_take ==1, relaxed <=1)
    strict_blockers: list[str] = []
    for c in eligible:
        code = str(c["_code"])
        sids = course_to_sids.get(code, [])
        is_must = code in must_take_codes
        if not sids:
            reason = (
                "Must-take course has no non-conflicting sections"
                if is_must
                else "No non-conflicting sections available"
            )
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": reason,
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
            if strict_per_course or is_must:
                strict_blockers.append(code)
            continue
        if strict_per_course or is_must:
            model.Add(sum(var_by_sid[s] for s in sids) == 1)
        else:
            model.Add(sum(var_by_sid[s] for s in sids) <= 1)

    if strict_blockers:
        for code in strict_blockers:
            if code in unscheduled_codes:
                continue
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Strict policy requires exactly one section, but none is available",
                    "details": [],
                }
            )
            unscheduled_codes.add(code)
        return [], unscheduled

    # pairwise no-overlap across all candidate sections (cached conflict matrix)
    for a, b in _get_conflict_pairs(option_by_sid):
        if a in var_by_sid and b in var_by_sid:
            model.Add(var_by_sid[a] + var_by_sid[b] <= 1)

    # Credit controls are intentionally distinct: max_credits is a ceiling,
    # while target_credits is an exact total for the scheduled course set.
    _cr_map = credits_map or {}
    _max_cr = max_credits if max_credits and max_credits > 0 else 0
    _target_cr = int(target_credits) if target_credits is not None else None
    credit_terms = []
    for code, sids in course_to_sids.items():
        cr = _cr_map.get(code, 0)
        if cr > 0:
            for sid in sids:
                if sid in var_by_sid:
                    credit_terms.append(cr * var_by_sid[sid])
    if _target_cr is not None:
        if credit_terms:
            model.Add(sum(credit_terms) == _target_cr)
        elif _target_cr != 0:
            return [], [
                *unscheduled,
                {
                    "course_code": "",
                    "reason": "No exact-credit timetable can be formed from the bounded candidate set",
                    "details": [],
                },
            ]
    if _max_cr and credit_terms:
        model.Add(sum(credit_terms) <= _max_cr)

    # Soft objective terms
    selected_sum = sum(var_by_sid.values())

    # Capacity preference (prefer options with more open seats)
    cap_sum = sum(
        (int(_remaining_capacity(option_by_sid[sid]) or 0) * var_by_sid[sid]) for sid in var_by_sid
    )
    if not consider_capacity:
        cap_sum = 0

    # Prefer earlier finish (penalize late end times)
    late_terms = []
    for sid in var_by_sid:
        latest_end = max(
            (
                _to_minutes(m.end)
                for m in option_by_sid[sid].get("meetings", [])
                if getattr(m, "end", "")
            ),
            default=0,
        )
        late_pen = max(0, latest_end - (16 * 60)) // 10
        late_terms.append(late_pen * var_by_sid[sid])
    late_sum = sum(late_terms) if late_terms else 0

    # Day usage penalty
    day_keys = ["SUN", "MON", "TUE", "WED", "THU"]
    day_used: dict[str, Any] = {}
    for d in day_keys:
        u = model.NewBoolVar(f"day_{d}")
        day_used[d] = u
        sids_using_day = [
            sid
            for sid, opt in option_by_sid.items()
            if any(str(getattr(m, "day", "")) == d for m in opt.get("meetings", []))
        ]
        if sids_using_day:
            for sid in sids_using_day:
                model.Add(u >= var_by_sid[sid])
            model.Add(u <= sum(var_by_sid[s] for s in sids_using_day))
        else:
            model.Add(u == 0)

    # Gap penalty (10-min slots)
    slot = 10
    first_slot_idx = 8 * 60 // slot
    last_slot_idx = 20 * 60 // slot
    total_gaps_terms: list[Any] = []

    def _covers(meetings: list[Meeting], day: str, idx: int) -> bool:
        t = idx * slot
        for m in meetings:
            if str(m.day) != day:
                continue
            cs, ce = _to_minutes(m.start), _to_minutes(m.end)
            if cs >= 0 and ce > cs and cs <= t < ce:
                return True
        return False

    for d in day_keys:
        y: dict[int, Any] = {}
        for idx in range(first_slot_idx, last_slot_idx):
            y[idx] = model.NewBoolVar(f"y_{d}_{idx}")
            covering = [
                sid
                for sid, opt in option_by_sid.items()
                if _covers(opt.get("meetings", []), d, idx)
            ]
            if covering:
                model.Add(y[idx] <= sum(var_by_sid[s] for s in covering))
                for sid in covering:
                    model.Add(y[idx] >= var_by_sid[sid])
            else:
                model.Add(y[idx] == 0)

        used = model.NewBoolVar(f"used_{d}")
        model.Add(used <= sum(y.values()))
        for idx in y:
            model.Add(used >= y[idx])

        first = model.NewIntVar(first_slot_idx, last_slot_idx, f"first_{d}")
        last = model.NewIntVar(first_slot_idx, last_slot_idx, f"last_{d}")
        for idx in y:
            model.Add(first <= idx).OnlyEnforceIf(y[idx])
            model.Add(last >= idx).OnlyEnforceIf(y[idx])

        span = model.NewIntVar(0, last_slot_idx - first_slot_idx + 1, f"span_{d}")
        occ = model.NewIntVar(0, last_slot_idx - first_slot_idx + 1, f"occ_{d}")
        model.Add(occ == sum(y.values()))
        # span ~= last-first+1 when used, else 0
        model.Add(span >= last - first + 1).OnlyEnforceIf(used)
        model.Add(span <= last - first + 1 + (last_slot_idx - first_slot_idx) * (1 - used))
        model.Add(span <= (last_slot_idx - first_slot_idx + 1) * used)

        gap = model.NewIntVar(0, last_slot_idx - first_slot_idx + 1, f"gap_{d}")
        model.Add(gap >= span - occ)
        total_gaps_terms.append(gap)

    days_sum = sum(day_used.values())
    gaps_sum = sum(total_gaps_terms) if total_gaps_terms else 0

    # profile weights
    if profile == "A":
        # Max coverage first, then lighter soft penalties
        w_sel, w_gap, w_days, w_late, w_cap = 12000, 12, 6, 6, 2
    elif profile == "B":
        # Compact/low-disruption preference
        w_sel, w_gap, w_days, w_late, w_cap = 10000, 30, 18, 14, 1
    else:
        # Balanced
        w_sel, w_gap, w_days, w_late, w_cap = 11000, 20, 10, 10, 2

    model.Maximize(
        w_sel * selected_sum
        - w_gap * gaps_sum
        - w_days * days_sum
        - w_late * late_sum
        + w_cap * cap_sum
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 8.0
    solver.parameters.num_search_workers = 8
    # Randomize search so each run can produce different optimal/feasible results
    solver.parameters.randomize_search = True
    solver.parameters.random_seed = random.randint(0, 2**31 - 1)
    res = solver.Solve(model)

    if res not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        pair_set = {(a, b) for (a, b) in _get_conflict_pairs(option_by_sid)}
        pair_set |= {(b, a) for (a, b) in pair_set}
        for c in eligible:
            code = str(c["_code"])
            if code in unscheduled_codes:
                continue
            sids = course_to_sids.get(code, [])
            if not sids:
                unscheduled.append(
                    {
                        "course_code": code,
                        "reason": "No candidate sections after hard filters",
                        "details": [{"hint": "Try relaxed mode or refresh sections catalog"}],
                    }
                )
                unscheduled_codes.add(code)
                continue
            conflict_edges = 0
            for sid in sids:
                conflict_edges += sum(
                    1 for other in option_by_sid.keys() if other != sid and (sid, other) in pair_set
                )
            reason = "Model infeasible under current hard constraints"
            if strict_per_course:
                reason = (
                    "Strict mode infeasible (exactly one section per course cannot be satisfied)"
                )
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": reason,
                    "details": [
                        {"candidate_sections": len(sids), "conflict_edges": int(conflict_edges)},
                        {"hint": "Enable relaxed mode (<=1) or adjust attempt list"},
                    ],
                }
            )
            unscheduled_codes.add(code)
        return [], unscheduled

    selected: list[dict[str, Any]] = []
    chosen_course: set[str] = set()
    for sid, v in var_by_sid.items():
        if solver.Value(v) == 1:
            opt = option_by_sid[sid]
            chosen_course.add(_option_course_key(opt))
            selected.append(opt)

    pair_set = {(a, b) for (a, b) in _get_conflict_pairs(option_by_sid)}
    pair_set |= {(b, a) for (a, b) in pair_set}

    for c in eligible:
        code = str(c["_code"])
        if code in unscheduled_codes:
            continue
        if code not in chosen_course:
            sids = course_to_sids.get(code, [])
            conflict_edges = 0
            for sid in sids:
                conflict_edges += sum(
                    1 for other in option_by_sid.keys() if other != sid and (sid, other) in pair_set
                )
            unscheduled.append(
                {
                    "course_code": code,
                    "reason": "Could not fit with chosen constraints/objective",
                    "details": [
                        {"candidate_sections": len(sids), "conflict_edges": int(conflict_edges)},
                        {"hint": "Try relaxed mode or remove a conflicting course"},
                    ],
                }
            )

    return selected, unscheduled


def build_plans(
    year: str,
    term: str,
    shortlist: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    keep_registered: bool,
    suggest_swaps: bool = False,
    strict_per_course: bool = False,
    consider_capacity: bool = True,
    max_credits: int = 0,
    target_credits: int | None = None,
    gender: str = "",
    program: str | None = None,
    require_complete_meetings: bool = False,
) -> dict[str, Any]:
    must_take_codes = {
        str(item.get("course_code", "")).replace(" ", "").upper()
        for item in shortlist
        if item.get("must_take") and str(item.get("course_code", "")).strip()
    }

    # A pin is singular: the last valid value is the exact section allowed for
    # that course.  The list-shaped payload is retained for API compatibility
    # with older clients, but it is not an "acceptable alternatives" list.
    pinned_id_by_code: dict[str, int] = {}
    for item in shortlist:
        code = str(item.get("course_code", "")).replace(" ", "").upper()
        pinned_raw = item.get("pinned_sections") or []
        if not code or not isinstance(pinned_raw, list):
            continue
        for pinned in pinned_raw:
            if not isinstance(pinned, dict):
                continue
            try:
                term_section_id = int(pinned.get("term_section_id") or 0)
            except (TypeError, ValueError):
                continue
            if term_section_id > 0:
                pinned_id_by_code[code] = term_section_id

    codes = sorted(
        {
            str(x.get("course_code", "")).replace(" ", "").upper()
            for x in shortlist
            if str(x.get("course_code", "")).strip()
        }
    )
    catalog = _catalog_for_courses(year, term, codes, gender, program)
    incomplete_meetings: dict[str, list[dict[str, Any]]] = {}
    if require_complete_meetings:
        catalog, incomplete_meetings = _complete_catalogue(catalog)

    # A pinned section is an exact hard constraint. Other sections for the
    # course are removed before any solver or alternative-generation pass.
    for code, pinned_id in pinned_id_by_code.items():
        if code in catalog:
            catalog[code] = [
                s
                for s in catalog[code]
                if int(s.get("term_section_id") or 0) == pinned_id  # type: ignore[call-overload]
            ]

    # Capacity is a hard eligibility rule unless the caller explicitly allows
    # full sections. Unknown counts stay eligible: they are not evidence that a
    # section has reached capacity. Filtering here makes A, B, C and the
    # no-OR-Tools fallback follow the same contract.
    if consider_capacity:
        for code, section_options in catalog.items():
            catalog[code] = [
                option for option in section_options if not _known_full_section(option)
            ]

    # Build course → credits mapping for max-credit enforcement
    credits_map: dict[str, int] = {}
    for item in shortlist:
        code = str(item.get("course_code", "")).replace(" ", "").upper()
        cr = int(item.get("credits", 0) or 0)
        if cr > 0:
            credits_map[code] = cr

    rejected_unscheduled: list[list[dict[str, Any]]] = []
    rejected_hard_failures: list[dict[str, Any]] = []

    def _hard_requirement_failures(
        selected: list[dict[str, Any]], unscheduled: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        selected_by_code = {_option_course_key(item): item for item in selected}
        unscheduled_by_code = {
            str(item.get("course_code", "")).replace(" ", "").upper(): item for item in unscheduled
        }
        failures: list[dict[str, Any]] = []

        for code in sorted(must_take_codes):
            if code in selected_by_code:
                continue
            detail = unscheduled_by_code.get(code, {}).get(
                "reason", "No valid section satisfies the current constraints"
            )
            failures.append(
                {
                    "kind": "must_take",
                    "course_code": code,
                    "reason": f"Must-take course could not be scheduled: {detail}",
                }
            )

        for code, pinned_id in sorted(pinned_id_by_code.items()):
            selected_item = selected_by_code.get(code)
            if selected_item is None:
                continue
            selected_id = int(selected_item.get("term_section_id") or 0)
            if selected_id != pinned_id:
                failures.append(
                    {
                        "kind": "pinned_section",
                        "course_code": code,
                        "reason": "The generated option did not use the exact pinned section",
                        "term_section_id": pinned_id,
                    }
                )

        if max_credits and max_credits > 0:
            scheduled_credits = sum(credits_map.get(code, 0) for code in selected_by_code)
            if scheduled_credits > max_credits:
                failures.append(
                    {
                        "kind": "max_credits",
                        "course_code": "",
                        "reason": (
                            f"Generated option has {scheduled_credits} credits, "
                            f"above the {max_credits}-credit limit"
                        ),
                    }
                )

        if target_credits is not None:
            scheduled_credits = sum(credits_map.get(code, 0) for code in selected_by_code)
            if scheduled_credits != int(target_credits):
                failures.append(
                    {
                        "kind": "target_credits",
                        "course_code": "",
                        "reason": (
                            f"Generated option has {scheduled_credits} credits; "
                            f"the exact requested total is {int(target_credits)} credits"
                        ),
                    }
                )

        return failures

    def _catalog_without_sids(excluded: set[int]) -> dict[str, list[dict[str, Any]]]:
        if not excluded:
            return {k: list(v) for k, v in catalog.items()}
        out: dict[str, list[dict[str, Any]]] = {}
        for code, opts in catalog.items():
            out[code] = [o for o in opts if int(o.get("term_section_id") or 0) not in excluded]
        return out

    def _sig(sel: list[dict[str, Any]]) -> tuple[int, ...]:
        return tuple(
            sorted(
                int(s.get("term_section_id") or 0)
                for s in sel
                if int(s.get("term_section_id") or 0) > 0
            )
        )

    def _run_method(
        method: str, cat: dict[str, list[dict[str, Any]]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if method == "A":
            return _cp_build_option(
                shortlist,
                cat,
                baseline,
                keep_registered,
                profile="A",
                strict_per_course=strict_per_course,
                consider_capacity=consider_capacity,
                max_credits=max_credits,
                target_credits=target_credits,
                credits_map=credits_map,
            )
        if method == "B":
            return _bitmask_build_option_b(
                shortlist,
                cat,
                baseline,
                keep_registered,
                strict_per_course=strict_per_course,
                consider_capacity=consider_capacity,
                max_credits=max_credits,
                target_credits=target_credits,
                credits_map=credits_map,
            )
        return _bitmask_build_option_c(
            shortlist,
            cat,
            baseline,
            keep_registered,
            strict_per_course=strict_per_course,
            max_credits=max_credits,
            target_credits=target_credits,
            credits_map=credits_map,
        )

    def _annotate_incomplete_meeting_evidence(
        unscheduled: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        annotated: list[dict[str, Any]] = []
        for row in unscheduled:
            item = dict(row)
            code = str(item.get("course_code") or "").replace(" ", "").upper()
            details = incomplete_meetings.get(code) or []
            if details:
                item["reason"] = "Section meeting data is incomplete or invalid"
                item["details"] = details
            annotated.append(item)
        return annotated

    def _top_k_method(
        method: str, k: int = 3
    ) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        results: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        seen: set[tuple[int, ...]] = set()
        queue: list[set[int]] = [set()]
        visited_excl: set[tuple[int, ...]] = set()

        while queue and len(results) < k:
            excl = queue.pop(0)
            excl_key = tuple(sorted(excl))
            if excl_key in visited_excl:
                continue
            visited_excl.add(excl_key)

            cat = _catalog_without_sids(excl)
            sel, uns = _run_method(method, cat)
            uns = _annotate_incomplete_meeting_evidence(uns)
            hard_failures = _hard_requirement_failures(sel, uns)
            if hard_failures:
                rejected_hard_failures.extend(hard_failures)
                rejected_unscheduled.append(uns)
                continue
            sig = _sig(sel)
            if not sig:
                rejected_unscheduled.append(uns)
                continue
            if sig in seen:
                continue

            seen.add(sig)
            results.append((sel, uns))

            for sid in sig:
                nxt = set(excl)
                nxt.add(int(sid))
                nk = tuple(sorted(nxt))
                if nk not in visited_excl:
                    queue.append(nxt)

        return results

    def fmt_option(
        name: str,
        method: str,
        rank: int,
        sel: list[dict[str, Any]],
        uns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "name": name,
            "method": method,
            "rank": rank,
            "scheduled": len(sel),
            "target": len(shortlist),
            "mappings": [
                {
                    "course_code": s.get("course_code", ""),
                    "course_key": s.get("course_key", "") or _option_course_key(s),
                    "course_number": s.get("course_number", ""),
                    "section": s.get("section", ""),
                    "term_section_id": s.get("term_section_id"),
                    "meetings": [
                        {"day": m.day, "start_time": m.start, "end_time": m.end}
                        for m in s.get("meetings", [])
                    ],
                    # Internal evidence marker consumed by deterministic callers
                    # that must certify a complete timetable.  The ordinary
                    # planner may still display a recorded section whose time is
                    # incomplete, but it must never silently turn that section
                    # into a clash-free proof.
                    "meeting_issue_codes": list(s.get("meeting_issue_codes") or []),
                }
                for s in sel
            ],
            "unscheduled": uns,
        }

    options: list[dict[str, Any]] = []
    for method in ("A", "B", "C"):
        variants = _top_k_method(method, k=3)
        for i, (sel, uns) in enumerate(variants, start=1):
            options.append(fmt_option(f"{method}{i}", method, i, sel, uns))

    fallback_unscheduled = max(rejected_unscheduled, key=len, default=[])
    best = (
        max(options, key=lambda x: x["scheduled"])
        if options
        else {
            "scheduled": 0,
            "target": len(shortlist),
            "unscheduled": fallback_unscheduled,
        }
    )

    hard_failures_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if not options:
        for failure in rejected_hard_failures:
            key = (str(failure.get("kind", "")), str(failure.get("course_code", "")))
            hard_failures_by_key.setdefault(key, failure)
    hard_constraint_failures = list(hard_failures_by_key.values())

    swap_suggestions: list[dict[str, Any]] = []
    if keep_registered and suggest_swaps and best["unscheduled"]:
        for u in best["unscheduled"]:
            swap_suggestions.append(
                {
                    "course_code": u.get("course_code", ""),
                    "from_section": "(current)",
                    "to_section": "(suggest alternative baseline section)",
                    "reason": "Conflict with baseline; consider moving one registered section.",
                }
            )

    return {
        "summary": {
            "scheduled": best["scheduled"],
            "target": best["target"],
            "conflicts": len(best["unscheduled"]),
            "swaps_required": len(swap_suggestions),
            "best_feasible": bool(options),
            "hard_constraint_failures": hard_constraint_failures,
        },
        "options": options,
        # Diagnostics for callers that need to explain why no valid option was
        # emitted. This is deliberately outside ``options``: an empty mapping is
        # not a timetable result.
        "unscheduled": best["unscheduled"],
        "swap_suggestions": swap_suggestions,
    }
