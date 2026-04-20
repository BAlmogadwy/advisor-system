"""PR3 fixture loader — small dedicated helper for the scenario-pack JSONs.

Loads fixtures from
``snapshots/planner-refactor-2026-04-20/fixtures/pr3_*.json`` into real
Django model rows so ``core.services.timetable_autoplace.auto_place_board``
can run against them.

Why a dedicated loader (H2) and not an extension of ``_build_autoplace_fixture``
(H1): the PR2 helper is parameterised for a single-course / single-section
topology. The PR3 fixtures describe multi-section, multi-course, multi-slot
scenarios with per-section instructor_id and an optional baseline-placements
map. Extending the PR2 helper would either bloat its signature or require
PR3-only branches; a standalone loader keeps the two concerns clean and
lets commit 5's warm-start tests reuse this loader without touching the
PR2 helper. ChatGPT commit-3 ruling H allowed either approach with a
preference for H1 — the shape divergence tipped it toward H2 here.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings as django_settings

from core.models import (
    DeliveryBoard,
    Room,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)

# Fixture dir is resolved relative to the Django project root so the loader
# keeps working regardless of the test runner's cwd.
FIXTURE_DIR = (
    Path(django_settings.BASE_DIR) / "snapshots" / "planner-refactor-2026-04-20" / "fixtures"
)


def load_pr3_fixture(
    fixture_name: str,
    *,
    program: str = "PR3",
    nominal_term: int = 1,
) -> tuple[TimetableScenario, DeliveryBoard, dict]:
    """Materialise a ``pr3_*.json`` fixture into DB rows for auto_place_board.

    Returns ``(scenario, board, raw_fixture_dict)``. The raw dict is returned
    so tests can read ``expected`` / ``notes`` blocks without re-parsing.

    The fixture's ``slot_pool`` is translated into a day-independent
    ``slot_config`` on the scenario (the autoplace generator pairs each
    slot with every WEEKDAY). Per-section instructor_id is NOT wired into
    the planner yet — it lives in the fixture dict but auto_place_board
    does not read it. See ChatGPT commit-3 ruling I2 and the PR3 DoR.
    """
    fixture_path = FIXTURE_DIR / fixture_name
    with fixture_path.open() as fh:
        data = json.load(fh)

    scenario_data = data["scenario"]

    scenario = TimetableScenario.objects.create(
        academic_year="1448",
        term="1",
        name=f"PR3 fixture — {fixture_name}",
        slot_config=[
            {"start": s["start_time"], "end": s["end_time"]}
            for s in scenario_data.get("slot_pool", [])
        ],
        # PR5 amendment — fixtures may now carry ``blocked_slots`` to
        # funnel greedy into a specific day topology (e.g. force same-day
        # clumping so SA has a real gap to close). Each entry is
        # ``{"day": <WEEKDAY>, "start": "HH:MM"}``; autoplace filters
        # option generation on the (day, start) pair. SA's option walker
        # currently ignores this list, which is intentional for the
        # SA-relocate greedy→SA test.
        blocked_slots=list(scenario_data.get("blocked_slots", [])),
    )
    board = DeliveryBoard.objects.create(
        scenario=scenario,
        label=f"{program}_BD",
        program=program,
        nominal_term=nominal_term,
        display_order=1,
    )

    sections = scenario_data.get("sections", [])
    # Group sections by course to derive planned_sections and total_demand.
    by_course: dict[str, list[dict]] = {}
    for sec in sections:
        by_course.setdefault(sec["course_code"], []).append(sec)

    for course_code, secs in by_course.items():
        # Default credit_hours=1 (75-min single meeting) so
        # _generate_meeting_options walks the scenario's ``slot_config``
        # (i.e. the fixture's ``slot_pool``) instead of falling through
        # to DEFAULT_LAB_SLOTS. PR3 scenario-pack fixtures are designed
        # around 75-min slots at 08:00/09:30/etc, and the warm-start
        # baselines are tagged with those exact times — the old default
        # of 2 (pattern=[100]) routed every option through the hardcoded
        # lab grid and no baseline ever matched.
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_code=course_code,
            department=program,
            credit_hours=secs[0].get("credit_hours", 1),
            planned_sections=len(secs),
            max_per_section=max((s.get("enrolment", 20) for s in secs), default=20),
            total_demand=sum(s.get("enrolment", 20) for s in secs),
            programme_term=nominal_term,
        )

    # Seed ScenarioStudentMap rows so auto_place_board's course_students
    # map is populated. Fixtures can carry explicit ``student_ids`` per
    # section (used by the STUDENT_CONFLICT fixture to force a shared
    # student across two courses); otherwise we synthesise N unique
    # students per course to match ``enrolment``.
    student_courses: dict[str, set[str]] = {}
    next_synthetic_sid = 10_000_000
    for course_code, secs in by_course.items():
        explicit_ids: set[str] = set()
        for sec in secs:
            for sid in sec.get("student_ids", []) or []:
                explicit_ids.add(str(sid))
                student_courses.setdefault(str(sid), set()).add(course_code)
        demand = sum(s.get("enrolment", 20) for s in secs)
        synthetic_needed = max(0, demand - len(explicit_ids))
        for _ in range(synthetic_needed):
            sid = f"SYN_{course_code}_{next_synthetic_sid}"
            next_synthetic_sid += 1
            student_courses.setdefault(sid, set()).add(course_code)

    # Explicit fixture student_ids are strings ("ST001"); ScenarioStudentMap
    # stores integers. Map each fixture ID to a stable offset-based int so
    # shared-student identity is preserved across courses within one load.
    id_to_int: dict[str, int] = {}
    for idx, sid_str in enumerate(sorted(student_courses.keys())):
        id_to_int[sid_str] = 20_000_000 + idx

    for sid_str, courses in student_courses.items():
        ScenarioStudentMap.objects.create(
            scenario=scenario,
            student_id=id_to_int[sid_str],
            primary_term=nominal_term,
            recommended_courses=sorted(courses),
        )

    for room in scenario_data.get("rooms", []):
        Room.objects.create(
            room_code=room["room_code"],
            capacity=room.get("capacity", 40),
            room_type=room.get("room_type", "lecture"),
            department=program,
            section=room.get("gender", "") or "",
        )

    # PR3 commit 5 — seed locked placements for warm-start fixtures that
    # specify a ``locks`` array. Each lock entry is translated into a
    # ``TermSection`` + ``SectionPlacement(is_locked=True)`` pair so the
    # PR1 lock preload inside ``auto_place_board`` picks them up when
    # ``TIMETABLE_ENFORCE_LOCKS`` is on. The end_time is derived from the
    # scenario's slot_pool so the lock matches a real candidate slot.
    slot_end_by_start = {s["start_time"]: s["end_time"] for s in scenario_data.get("slot_pool", [])}
    for lock in scenario_data.get("locks", []):
        section_full = lock["section_code"]
        course_code, section = section_full.split("|", 1)
        ts, _ = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=course_code,
            section=section,
            defaults={
                "course_code": course_code,
                "course_number": course_code,
                "course_name": course_code,
                "available_capacity": 40,
                "source_tag": "pr3_lock",
            },
        )
        start_time = lock["start_time"]
        end_time = lock.get("end_time", slot_end_by_start.get(start_time, start_time))
        SectionPlacement.objects.create(
            board=board,
            term_section=ts,
            # Upper-case the day to match ``WEEKDAYS`` / the planner's
            # option-dict day codes. Keeping case consistent means
            # baseline ↔ placement comparisons don't silently miss.
            day=str(lock["day"]).upper(),
            start_time=start_time,
            end_time=end_time,
            room=lock.get("room", ""),
            is_locked=True,
        )

    return scenario, board, data
