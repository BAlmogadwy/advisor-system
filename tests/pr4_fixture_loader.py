"""PR4 fixture loader — builds on the PR3 loader with instructor plumbing.

The PR3 loader (``tests/pr3_fixture_loader.py``) materialises scenario, board,
sections, students and rooms, but does NOT emit ``TermSectionMeeting`` rows
because PR3 never needed per-meeting instructor data at runtime. PR4 does:
commits 2–3 roll up a per-run ``instructor_schedule`` dict from each
already-placed meeting's ``instructor`` text field, so the fixture loader
must put those strings somewhere the planner can see them.

This loader delegates the shared materialisation to ``load_pr3_fixture`` and
then creates one ``TermSection`` (if missing) + one ``TermSectionMeeting``
per fixture section for the first pool slot. The meeting's ``instructor``
field is taken from the fixture's ``sections[i].instructor`` string. This
matches PR4's opaque-string discipline (A6): whatever the fixture wrote,
the DB holds verbatim; the planner normalises via strip+casefold at read
time, not load time.

Fixtures that don't carry an ``instructor`` field keep today's behaviour —
no meeting row, no instructor contribution to ``instructor_schedule``.
"""

from __future__ import annotations

from pathlib import Path

from pr3_fixture_loader import FIXTURE_DIR as _PR3_FIXTURE_DIR
from pr3_fixture_loader import load_pr3_fixture

from core.models import TermSection, TermSectionMeeting

FIXTURE_DIR: Path = _PR3_FIXTURE_DIR


def load_pr4_fixture(
    fixture_name: str,
    *,
    program: str = "PR4",
    nominal_term: int = 1,
):
    """Materialise a ``pr4_*.json`` fixture plus per-section meeting rows.

    Returns ``(scenario, board, raw_fixture_dict)``. The raw dict is the
    parsed JSON so tests can read ``expected`` / ``notes`` / ``prayer_windows``
    blocks without re-parsing.

    Meeting rows are created with the first slot in the fixture's
    ``slot_pool`` as (day, start_time, end_time). This gives each section an
    instructor-bearing meeting the planner can roll up without us having to
    fabricate meeting data from scratch.
    """
    scenario, board, data = load_pr3_fixture(
        fixture_name, program=program, nominal_term=nominal_term
    )

    scenario_data = data["scenario"]
    slot_pool = scenario_data.get("slot_pool", [])
    if not slot_pool:
        return scenario, board, data
    first = slot_pool[0]

    for sec in scenario_data.get("sections", []):
        instructor = sec.get("instructor", "")
        if not instructor:
            continue
        course_code = sec["course_code"]
        section = sec["section_code"]
        ts, _created = TermSection.objects.get_or_create(
            scenario=scenario,
            course_key=course_code,
            section=section,
            defaults={
                "course_code": course_code,
                "course_number": course_code,
                "course_name": course_code,
                "available_capacity": sec.get("enrolment", 20),
                "source_tag": "pr4_fixture",
            },
        )
        TermSectionMeeting.objects.create(
            term_section=ts,
            day=str(first["day"]).upper(),
            start_time=first["start_time"],
            end_time=first["end_time"],
            room="",
            instructor=instructor,
        )

    return scenario, board, data
