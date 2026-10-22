"""The fixture the seating tests share with the legacy-oracle recorder (6a).

Not collected (no ``test_`` prefix). ``build_seating_fixture`` builds one
scenario that gives the quality read every part it reports (a room audit row,
instructor metrics and, with one teacher per section, their coverage, tiered
seats with an unseated request, and a "mainly for programme" mark the grouping
acts on) and the plan lens a grouped seating to show. ``record_legacy_reads``
reads ``/quality/`` and ``/plan-lens/`` through their views, ids replaced by
names, so the recorded oracle (``tests/fixtures/tw_quality_legacy.json``,
written by ``tests/record_tw_quality_legacy.py`` from the code before 6a) and
a later run compare as JSON text, key order included.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.models import (
    DeliveryBoard,
    ScenarioSectionBudget,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TermSection,
    TimetableScenario,
)
from tests._section_programme_fixture import AI2_IDS, DS_IDS, build_marked_scenario
from tests.one_teacher_support import instructor, link

#: The production flags (render.yaml) and the settings they are read with.
PRODUCTION = {
    "TIMETABLE_INSTRUCTOR_LINKS_ENABLED": True,
    "TIMETABLE_TIERED_OBJECTIVE_ENABLED": True,
    "TIMETABLE_ONE_TEACHER_PER_SECTION": True,
    "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": True,
    "TIMETABLE_SECTION_PROGRAMME_PREFERENCE": True,
}
#: The three flag modes the oracle holds: the settings' defaults for the three
#: production flags (off), production, and production with the legacy
#: (untiered) seat layout.
FLAG_MODES = {
    "local": {
        **PRODUCTION,
        "TIMETABLE_ONE_TEACHER_PER_SECTION": False,
        "TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY": False,
        "TIMETABLE_SECTION_PROGRAMME_PREFERENCE": False,
    },
    "production": PRODUCTION,
    "production_legacy_seats": {**PRODUCTION, "TIMETABLE_TIERED_OBJECTIVE_ENABLED": False},
}


def apply_flags(settings, mode: str) -> None:
    for name, value in FLAG_MODES[mode].items():
        setattr(settings, name, value)


@dataclass
class Seating:
    scenario: TimetableScenario
    board: DeliveryBoard
    placements: dict[tuple[str, str], SectionPlacement]

    def ids(self) -> dict[str, dict[int, str]]:
        """Each id the fixture made, by the kind of thing it names."""
        sections = {p.term_section_id: f"<ts {c} {s}>" for (c, s), p in self.placements.items()}
        return {
            "scenario": {self.scenario.pk: "<scenario>"},
            "term_section": sections,
            "placement": {p.pk: f"<placement {c} {s}>" for (c, s), p in self.placements.items()},
        }


def build_seating_fixture() -> Seating:
    """The PR #158 board (AI236 S1 on SUN, S2 on MON marked AI2, IS124 S1 on
    TUE; six AI2 and six DS students who want both), plus:

    - teachers linked to both courses (one-teacher coverage has rows);
    - AI240 S1, on SUN at AI236 S1's time, wanted by the six DS students: the
      seating cannot give them AI236 S1 as well, so the grouping has a
      trade-off to report;
    - AI241 S1, on TUE at IS124 S1's time, wanted by the six DS students: one
      of the two is left unseated for each of them (the seats report counts);
    - every room "R1", a room the database does not hold (a room-audit row).
    """
    scenario, board, placements = build_marked_scenario(mark="AI2")
    dr_a, dr_b = instructor("Dr A"), instructor("Dr B")
    link("AI2", "AI236", dr_a)
    link("AI2", "IS124", dr_b)
    link("AI2", "AI240", dr_b)
    link("AI2", "AI241", dr_a)
    for code, day in (("AI240", "SUN"), ("AI241", "TUE")):
        ScenarioSectionBudget.objects.create(
            scenario=scenario,
            course_key=code,
            course_code=code,
            course_name=code,
            department="AI",
            credit_hours=3,
            planned_sections=1,
            max_per_section=20,
            total_demand=len(DS_IDS),
            programme_term=2,
        )
        section = TermSection.objects.create(
            scenario=scenario,
            source_tag="tw_auto",
            course_key=code,
            course_code=code,
            course_number=code[-3:],
            course_name=code,
            section="S1",
        )
        placements[(code, "S1")] = SectionPlacement.objects.create(
            board=board,
            term_section=section,
            day=day,
            start_time="09:00",
            end_time="10:15",
            room="R1",
        )
        ScenarioStudentCourseRequest.objects.bulk_create(
            [
                ScenarioStudentCourseRequest(
                    scenario=scenario,
                    student_id=sid,
                    course_key=code,
                    course_code=code,
                    primary_term=2,
                    status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                    priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                    source="test",
                )
                for sid in DS_IDS
            ]
        )
    return Seating(scenario=scenario, board=board, placements=placements)


STUDENTS = len(AI2_IDS) + len(DS_IDS)

#: Keys whose value (or, for ``sections``, whose dict keys) name a row by id.
_ID_KEYS = {
    "scenario_id": "scenario",
    "term_section_id": "term_section",
    "placement_ids": "placement",
}


def normalise_ids(value, ids: dict[str, dict[int, str]]):
    """``value`` with every id the fixture made named, key order kept: the
    scenario, term-section and placement ids, the room audit's demand ids
    (they lead with the term-section id) and the lens's ``sections`` keys
    (term-section ids as text)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            kind = _ID_KEYS.get(key)
            if kind == "placement" and isinstance(item, list):
                out[key] = [ids[kind].get(pk, pk) for pk in item]
            elif kind is not None and isinstance(item, int):
                out[key] = ids[kind].get(item, item)
            elif key == "demand_id" and isinstance(item, str) and "#" in item:
                # The room audit's demand id leads with the term-section id.
                head, rest = item.split("#", 1)
                named = ids["term_section"].get(int(head), head) if head.isdigit() else head
                out[key] = f"{named}#{rest}"
            elif key == "sections" and isinstance(item, dict):
                out[key] = {
                    ids["term_section"].get(int(k), k) if str(k).isdigit() else k: normalise_ids(
                        row, ids
                    )
                    for k, row in item.items()
                }
            else:
                out[key] = normalise_ids(item, ids)
        return out
    if isinstance(value, list):
        return [normalise_ids(item, ids) for item in value]
    return value


def read(client, url: str):
    """``(body, queries)``: the JSON body, key order kept, and how many
    queries the request ran."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, response.content
    return json.loads(response.content), captured.captured_queries


def quality_url(fx: Seating, query: str = "") -> str:
    return f"/ops/tw/scenarios/{fx.scenario.pk}/quality/{query}"


def lens_url(fx: Seating) -> str:
    return f"/ops/tw/scenarios/{fx.scenario.pk}/plan-lens/"


def record_legacy_reads(client, fx: Seating) -> dict[str, object]:
    """The quality read's and the plan lens's full JSON body, ids named, and
    the number of queries each request ran."""
    quality, quality_queries = read(client, quality_url(fx))
    lens, lens_queries = read(client, lens_url(fx))
    return {
        "quality": normalise_ids(quality, fx.ids()),
        "plan_lens": normalise_ids(lens, fx.ids()),
        "queries": {"quality": len(quality_queries), "plan_lens": len(lens_queries)},
    }
