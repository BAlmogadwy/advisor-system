"""In-place repair of pre-existing instructor daily-session-cap violations.

The structural cap (see ``is_instructor_daily_cap_enabled``) guarantees the
solver never *creates* a day with more than the cap of an instructor's sessions.
But a board built before the cap existed — or hand-edited, or polished by the
per-board SA stage — can already hold a violation (e.g. scenario 627: one
instructor with 4 Monday sessions split across two term boards). This pass
detects each over-cap ``(instructor, day)`` SCENARIO-WIDE (an instructor is one
person regardless of which term/board they teach in) and removes the excess.

The cap WINS against students (the locked spec): the repair will compact an
instructor's overloaded day even at a student cost. But it minimises that cost —
for each excess session it enumerates every cap-satisfying option (relocate to a
feasible alternative slot, or unplace the session) and picks the one with the
BEST resulting student score (lexicographic positions 0-5). A slot that costs
students nothing is taken immediately; only if every option regresses students
is the least-bad one chosen.

Guarantees:
- **Scenario-wide** — counts an instructor's sessions across every board.
- **Locks respected** — locked placements are never moved or deleted; a wholly
  locked overload is reported under ``locked_blocked`` for manual action.
- **Hard constraints kept** — a relocation never creates an instructor clash, a
  same-course overlap, a second same-day meeting for a section, lands on a
  blocked slot, or pushes another day over cap.
- **PR29 re-fan** — every touched section is re-fanned via
  ``apply_primary_instructor`` and its board re-roomed.

No-op (and zero DB work) when the cap flag is off.
"""

from __future__ import annotations

import logging

from django.db import transaction

from core.models import SectionPlacement, TermSection, TimetableScenario
from core.models import TermSectionMeeting as _TSM
from core.services.course_instructor_assignment import (
    apply_primary_instructor as _apply_course_instructor,
)
from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS, WEEKDAYS
from core.services.timetable_pr4_instructor import (
    get_instructor_daily_cap,
    is_instructor_clash_enabled,
    is_instructor_daily_cap_enabled,
)
from core.services.timetable_rooming import assign_rooms_to_board

logger = logging.getLogger(__name__)


def _to_min(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":", 1)
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -1


def _day_idx(day_str: str) -> int:
    up = (day_str or "").upper()
    return WEEKDAYS.index(up) if up in WEEKDAYS else -1


def repair_instructor_daily_overloads(scenario_id: int) -> dict:
    """Detect and repair instructor daily-session-cap violations across a whole
    scenario (every board). Returns a report dict."""
    report: dict = {
        "enabled": False,
        "detected": [],
        "repaired": [],
        "unplaced": [],
        "locked_blocked": [],
        "remaining_violations": 0,
        "student_score_before": None,
        "student_score_after": None,
    }
    if not is_instructor_daily_cap_enabled():
        return report
    report["enabled"] = True

    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    scenario = TimetableScenario.objects.get(id=scenario_id)
    cap = get_instructor_daily_cap()
    cap_map = build_section_instructor_map_for_scenario(scenario_id)  # section_id -> frozenset[int]

    lecture_slots = [(s["start"], s["end"]) for s in (scenario.slot_config or DEFAULT_SLOTS)]
    lab_slots = [(s["start"], s["end"]) for s in (scenario.lab_slot_config or DEFAULT_LAB_SLOTS)]
    blocked = {(d.upper(), st) for (d, st) in _blocked_keys(scenario)}

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("term_section", "board")
    )

    def _sid(p) -> str:
        ts = p.term_section
        return f"{ts.course_key or ts.course_code}_{ts.section}"

    def _instr_ids(p) -> frozenset:
        return cap_map.get(_sid(p), frozenset())

    # ── Scenario-wide occupancy, kept in sync as we apply moves ──
    count: dict[tuple[object, str], int] = {}
    section_days: dict[int, set[str]] = {}
    instr_slots: dict[object, set[tuple[str, str]]] = {}
    course_slots: dict[str, set[tuple[str, str]]] = {}
    for p in placements:
        day = (p.day or "").upper()
        section_days.setdefault(p.term_section_id, set()).add(day)
        course_slots.setdefault(p.term_section.course_code, set()).add((day, p.start_time))
        for iid in _instr_ids(p):
            count[(iid, day)] = count.get((iid, day), 0) + 1
            instr_slots.setdefault(iid, set()).add((day, p.start_time))

    detected = [
        {"instructor_id": iid, "day": day, "sessions": n}
        for (iid, day), n in sorted(count.items(), key=lambda kv: (-kv[1], str(kv[0])))
        if n > cap
    ]
    report["detected"] = detected
    if not detected:
        return report

    # In-memory section states for student-impact scoring of candidate moves.
    states = build_section_states_for_scenario(scenario_id)
    sections_by_id = {s.section_id: s for s in states}
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id) if profiles else {}

    def _score():
        if not profiles:
            return ()
        return tuple(
            evaluate_generated_timetable_candidate(
                candidate_id="cap_repair",
                generated_sections=states,
                student_profiles=profiles,
                course_rigidity=rigidity,
            ).lexicographic_score
        )

    def _meeting_idx(p):
        s = sections_by_id.get(_sid(p))
        if s is None:
            return None, None
        di, sm = _day_idx(p.day), _to_min(p.start_time)
        for i, m in enumerate(s.meetings):
            if m.day == di and m.start_min == sm:
                return s, i
        return s, None

    baseline = _score()
    report["student_score_before"] = list(baseline) if baseline else None
    touched: dict[int, object] = {}
    touched_boards: set[int] = set()

    def _candidate_slots(p, iid_set):
        """Yield structurally-feasible (day2, s2, e2) targets for ``p``."""
        from core.services.timetable_assignment_models import SectionMeeting  # noqa: F401

        dur = _to_min(p.end_time) - _to_min(p.start_time)
        slots = lab_slots if dur > 75 else lecture_slots
        cur_section_days = section_days.get(p.term_section_id, set())
        course = p.term_section.course_code
        cur_day = (p.day or "").upper()
        for day2 in WEEKDAYS:
            if day2 == cur_day or day2 in cur_section_days:
                continue
            if any(count.get((iid, day2), 0) >= cap for iid in iid_set):
                continue
            for s2, e2 in slots:
                if (day2, s2) in blocked:
                    continue
                if any((day2, s2) in instr_slots.get(iid, set()) for iid in iid_set):
                    continue
                if (day2, s2) in course_slots.get(course, set()):
                    continue
                yield day2, s2, e2

    with transaction.atomic():
        for entry in detected:
            iid, day = entry["instructor_id"], entry["day"]
            excess = count.get((iid, day), 0) - cap
            day_ps = sorted(
                (
                    p
                    for p in placements
                    if (p.day or "").upper() == day and iid in _instr_ids(p) and not p.is_locked
                ),
                key=lambda p: p.start_time,
                reverse=True,
            )
            from core.services.timetable_assignment_models import SectionMeeting

            for p in day_ps:
                if excess <= 0:
                    break
                iid_set = _instr_ids(p)
                s, midx = _meeting_idx(p)
                orig_meeting = s.meetings[midx] if (s is not None and midx is not None) else None
                old = (day, p.start_time)

                # Evaluate every cap-satisfying option; keep the best-scoring one.
                best = None  # (score, kind, day2, s2, e2)
                for day2, s2, e2 in _candidate_slots(p, iid_set):
                    if orig_meeting is not None:
                        s.meetings[midx] = SectionMeeting(_day_idx(day2), _to_min(s2), _to_min(e2))
                        sc = _score()
                        s.meetings[midx] = orig_meeting
                    else:
                        sc = _score()
                    if best is None or sc < best[0]:
                        best = (sc, "relocate", day2, s2, e2)
                    if baseline and sc <= baseline:  # no student harm — take it
                        break
                # The unplace option (always available as a last resort).
                if orig_meeting is not None and (best is None or best[0] > baseline):
                    removed = s.meetings.pop(midx)
                    sc = _score()
                    s.meetings.insert(midx, removed)
                    if best is None or sc < best[0]:
                        best = (sc, "unplace", None, None, None)

                if best is None:
                    # No relocation and no in-memory section to unplace → unplace DB only.
                    best = (None, "unplace", None, None, None)

                _, kind, day2, s2, e2 = best
                if kind == "relocate":
                    if orig_meeting is not None:
                        s.meetings[midx] = SectionMeeting(_day_idx(day2), _to_min(s2), _to_min(e2))
                    _relocate(p, day2, s2, e2)
                    _shift_state(
                        p, old, (day2, s2), iid_set, count, section_days, instr_slots, course_slots
                    )
                    report["repaired"].append(
                        {
                            "section": f"{p.term_section.course_code} {p.term_section.section}",
                            "from": f"{old[0]} {old[1]}",
                            "to": f"{day2} {s2}",
                            "instructor_id": iid,
                        }
                    )
                else:
                    if orig_meeting is not None:
                        s.meetings.pop(midx)
                    _unplace(p)
                    _drop_state(p, old, iid_set, count, section_days, instr_slots, course_slots)
                    report["unplaced"].append(
                        {
                            "section": f"{p.term_section.course_code} {p.term_section.section}",
                            "slot": f"{old[0]} {old[1]}",
                            "instructor_id": iid,
                        }
                    )
                touched[p.term_section_id] = p.board
                touched_boards.add(p.board_id)
                excess -= 1
            if excess > 0:
                report["locked_blocked"].append(
                    {"instructor_id": iid, "day": day, "excess": excess}
                )

        # PR29: re-fan instructor name onto touched sections + re-room their boards.
        ts_by_id = {ts.id: ts for ts in TermSection.objects.filter(id__in=touched.keys())}
        for ts_id, board in touched.items():
            ts = ts_by_id.get(ts_id)
            if ts is not None:
                _apply_course_instructor(ts, scenario, board, ts.course_code)
        for board_id in touched_boards:
            assign_rooms_to_board(board_id, respect_locked=True)

    report["student_score_after"] = list(_score()) if profiles else None
    report["remaining_violations"] = sum(e["excess"] for e in report["locked_blocked"])
    logger.info(
        "Instructor cap repair (scenario %d): %d relocated, %d unplaced, %d locked-blocked; "
        "student score %s -> %s",
        scenario_id,
        len(report["repaired"]),
        len(report["unplaced"]),
        len(report["locked_blocked"]),
        report["student_score_before"],
        report["student_score_after"],
    )
    return report


def repair_instructor_clashes(scenario_id: int) -> dict:
    """Detect and repair instructor clashes — an instructor double-booked at the
    same ``(day, start)`` anywhere in the scenario (e.g. teaching two different
    courses at once). Relocates one of each clashing pair to a feasible clash-free
    slot, eval-driven least-harm: never creates a new clash / same-course overlap /
    second same-day meeting for a section / blocked slot / daily-cap breach, and
    among feasible targets picks the one with the best student score. Locked
    placements are never moved. No-op when the clash flag is off."""
    report: dict = {
        "enabled": False,
        "detected": [],
        "repaired": [],
        "unplaced": [],
        "locked_blocked": [],
        "remaining_clashes": 0,
        "student_score_before": None,
        "student_score_after": None,
    }
    if not is_instructor_clash_enabled():
        return report
    report["enabled"] = True

    from core.services.timetable_assignment_models import SectionMeeting
    from core.services.timetable_candidate_eval import evaluate_generated_timetable_candidate
    from core.services.timetable_optimizer_v2 import (
        build_course_rigidity_for_scenario,
        build_section_instructor_map_for_scenario,
        build_section_states_for_scenario,
        build_student_profiles_for_scenario,
    )

    scenario = TimetableScenario.objects.get(id=scenario_id)
    cap_map = build_section_instructor_map_for_scenario(scenario_id)
    cap_on = is_instructor_daily_cap_enabled()
    cap = get_instructor_daily_cap() if cap_on else 10**9
    lecture_slots = [(s["start"], s["end"]) for s in (scenario.slot_config or DEFAULT_SLOTS)]
    lab_slots = [(s["start"], s["end"]) for s in (scenario.lab_slot_config or DEFAULT_LAB_SLOTS)]
    blocked = {(d.upper(), st) for (d, st) in _blocked_keys(scenario)}

    placements = list(
        SectionPlacement.objects.filter(board__scenario_id=scenario_id)
        .exclude(day="")
        .select_related("term_section", "board")
    )

    def _sid(p):
        ts = p.term_section
        return f"{ts.course_key or ts.course_code}_{ts.section}"

    def _instr_ids(p):
        return cap_map.get(_sid(p), frozenset())

    # Count-based occupancy (a clash means >1 at a slot, which a set can't hold).
    slot_count: dict[tuple[object, str, str], int] = {}
    daily_count: dict[tuple[object, str], int] = {}
    section_days: dict[int, set[str]] = {}
    course_slots: dict[str, set[tuple[str, str]]] = {}
    for p in placements:
        day = (p.day or "").upper()
        section_days.setdefault(p.term_section_id, set()).add(day)
        course_slots.setdefault(p.term_section.course_code, set()).add((day, p.start_time))
        for iid in _instr_ids(p):
            slot_count[(iid, day, p.start_time)] = slot_count.get((iid, day, p.start_time), 0) + 1
            daily_count[(iid, day)] = daily_count.get((iid, day), 0) + 1

    detected = [
        {"instructor_id": iid, "day": day, "start": start, "sessions": n}
        for (iid, day, start), n in sorted(slot_count.items(), key=lambda kv: -kv[1])
        if n > 1
    ]
    report["detected"] = detected
    if not detected:
        return report

    states = build_section_states_for_scenario(scenario_id)
    sections_by_id = {s.section_id: s for s in states}
    profiles = build_student_profiles_for_scenario(scenario_id)
    rigidity = build_course_rigidity_for_scenario(scenario_id) if profiles else {}

    def _score():
        if not profiles:
            return ()
        return tuple(
            evaluate_generated_timetable_candidate(
                "clash_repair", states, profiles, rigidity, section_instructor_ids=cap_map
            ).lexicographic_score
        )

    def _meeting_idx(p):
        s = sections_by_id.get(_sid(p))
        if s is None:
            return None, None
        di, sm = _day_idx(p.day), _to_min(p.start_time)
        for i, m in enumerate(s.meetings):
            if m.day == di and m.start_min == sm:
                return s, i
        return s, None

    baseline = _score()
    report["student_score_before"] = list(baseline) if baseline else None
    touched: dict[int, object] = {}
    touched_boards: set[int] = set()

    def _candidates(p, iid_set):
        dur = _to_min(p.end_time) - _to_min(p.start_time)
        slots = lab_slots if dur > 75 else lecture_slots
        others = section_days.get(p.term_section_id, set()) - {(p.day or "").upper()}
        course = p.term_section.course_code
        cur_day = (p.day or "").upper()
        for day2 in WEEKDAYS:
            if day2 == cur_day or day2 in others:
                continue
            if cap_on and any(daily_count.get((iid, day2), 0) >= cap for iid in iid_set):
                continue
            for s2, e2 in slots:
                if (day2, s2) in blocked:
                    continue
                if any(slot_count.get((iid, day2, s2), 0) > 0 for iid in iid_set):
                    continue  # clash-free target
                if (day2, s2) in course_slots.get(course, set()):
                    continue
                yield day2, s2, e2

    with transaction.atomic():
        for entry in detected:
            iid, day, start = entry["instructor_id"], entry["day"], entry["start"]
            to_move = slot_count.get((iid, day, start), 0) - 1
            movers = [
                p
                for p in placements
                if (p.day or "").upper() == day
                and p.start_time == start
                and iid in _instr_ids(p)
                and not p.is_locked
            ]
            for p in movers:
                if to_move <= 0:
                    break
                iid_set = _instr_ids(p)
                s, midx = _meeting_idx(p)
                orig = s.meetings[midx] if (s is not None and midx is not None) else None
                best = None
                for day2, s2, e2 in _candidates(p, iid_set):
                    if orig is not None:
                        s.meetings[midx] = SectionMeeting(_day_idx(day2), _to_min(s2), _to_min(e2))
                        sc = _score()
                        s.meetings[midx] = orig
                    else:
                        sc = _score()
                    if best is None or sc < best[0]:
                        best = (sc, day2, s2, e2)
                    if baseline and sc <= baseline:
                        break
                if best is None:
                    continue  # no clash-free slot found; leave it (reported as remaining)
                _, day2, s2, e2 = best
                if orig is not None:
                    s.meetings[midx] = SectionMeeting(_day_idx(day2), _to_min(s2), _to_min(e2))
                _relocate(p, day2, s2, e2)
                course_slots.get(p.term_section.course_code, set()).discard((day, start))
                section_days.get(p.term_section_id, set()).discard(day)
                section_days.setdefault(p.term_section_id, set()).add(day2)
                course_slots.setdefault(p.term_section.course_code, set()).add((day2, s2))
                for _iid in iid_set:
                    slot_count[(_iid, day, start)] = slot_count.get((_iid, day, start), 0) - 1
                    slot_count[(_iid, day2, s2)] = slot_count.get((_iid, day2, s2), 0) + 1
                    daily_count[(_iid, day)] = daily_count.get((_iid, day), 0) - 1
                    daily_count[(_iid, day2)] = daily_count.get((_iid, day2), 0) + 1
                report["repaired"].append(
                    {
                        "section": f"{p.term_section.course_code} {p.term_section.section}",
                        "from": f"{day} {start}",
                        "to": f"{day2} {s2}",
                        "instructor_id": iid,
                    }
                )
                touched[p.term_section_id] = p.board
                touched_boards.add(p.board_id)
                to_move -= 1
            if to_move > 0:
                report["locked_blocked"].append(
                    {"instructor_id": iid, "day": day, "start": start, "excess": to_move}
                )

        ts_by_id = {ts.id: ts for ts in TermSection.objects.filter(id__in=touched.keys())}
        for ts_id, board in touched.items():
            ts = ts_by_id.get(ts_id)
            if ts is not None:
                _apply_course_instructor(ts, scenario, board, ts.course_code)
        for board_id in touched_boards:
            assign_rooms_to_board(board_id, respect_locked=True)

    report["student_score_after"] = list(_score()) if profiles else None
    report["remaining_clashes"] = sum(e["excess"] for e in report["locked_blocked"])
    logger.info(
        "Instructor clash repair (scenario %d): %d relocated, %d remaining; student %s -> %s",
        scenario_id,
        len(report["repaired"]),
        report["remaining_clashes"],
        report["student_score_before"],
        report["student_score_after"],
    )
    return report


def _blocked_keys(scenario):
    from core.services.timetable_validation import blocked_slot_keys

    return blocked_slot_keys(scenario.blocked_slots)


def _relocate(p, day2, s2, e2) -> None:
    _TSM.objects.filter(
        term_section_id=p.term_section_id, day=p.day, start_time=p.start_time
    ).update(day=day2, start_time=s2, end_time=e2)
    p.day, p.start_time, p.end_time, p.room = day2, s2, e2, ""
    p.save(update_fields=["day", "start_time", "end_time", "room"])


def _unplace(p) -> None:
    _TSM.objects.filter(
        term_section_id=p.term_section_id, day=p.day, start_time=p.start_time
    ).delete()
    p.delete()


def _shift_state(p, old, new, iid_set, count, section_days, instr_slots, course_slots) -> None:
    old_day, old_start = old
    new_day, new_start = new
    code = p.term_section.course_code
    section_days.get(p.term_section_id, set()).discard(old_day)
    section_days.setdefault(p.term_section_id, set()).add(new_day)
    course_slots.get(code, set()).discard((old_day, old_start))
    course_slots.setdefault(code, set()).add((new_day, new_start))
    for iid in iid_set:
        count[(iid, old_day)] = count.get((iid, old_day), 0) - 1
        count[(iid, new_day)] = count.get((iid, new_day), 0) + 1
        instr_slots.get(iid, set()).discard((old_day, old_start))
        instr_slots.setdefault(iid, set()).add((new_day, new_start))


def _drop_state(p, old, iid_set, count, section_days, instr_slots, course_slots) -> None:
    old_day, old_start = old
    code = p.term_section.course_code
    course_slots.get(code, set()).discard((old_day, old_start))
    for iid in iid_set:
        count[(iid, old_day)] = count.get((iid, old_day), 0) - 1
        instr_slots.get(iid, set()).discard((old_day, old_start))
