"""The fixture the slot-verdict tests share with the legacy-oracle recorder.

Not collected (no ``test_`` prefix). ``build_rich_fixture`` builds one scenario
that gives the two slot previews every legacy evidence kind;
``record_legacy_previews`` reads the previews through their views with the
verdict off, ids replaced by names, so the recorded oracle
(``tests/fixtures/tw_slot_verdict_legacy.json``, written by
``tests/record_tw_slot_verdict_legacy.py`` from the code before 5d) and a
later run compare as JSON text, key order included.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.models import (
    DeliveryBoard,
    ScenarioStudentCourseRequest,
    SectionPlacement,
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)
from tests.one_teacher_support import (
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)

#: Three placeable lecture columns and a policy one no move or placer is offered.
LECTURE_COLUMNS = [
    {"label": "L1", "start": "09:00", "end": "10:15"},
    {"label": "L2", "start": "10:30", "end": "11:45"},
    {"label": "L3", "start": "13:00", "end": "14:15"},
    {"label": "Policy", "start": "14:30", "end": "15:45", "policy_only": True},
]
#: Lab columns: one repeats a lecture window (13:00-14:15), so the planned
#: preview scores that window once as a lecture and once as a lab, and an
#: evening online-only column only an online meeting already there is offered.
LAB_COLUMNS = [
    {"label": "Lab 1", "start": "09:00", "end": "10:40"},
    {"label": "Lab 3", "start": "13:00", "end": "14:40"},
    {"label": "Lab 75", "start": "13:00", "end": "14:15"},
    {"label": "Online 2", "start": "17:40", "end": "19:20", "online_only": True},
]
BLOCKED = [{"day": "WED", "start": "13:00"}]


def demand(sc: TimetableScenario, course_key: str, students) -> None:
    """Students who need ``course_key`` (canonical requests, as in
    ``tests/test_timetable_repair_foundation.py``)."""
    ScenarioStudentCourseRequest.objects.bulk_create(
        [
            ScenarioStudentCourseRequest(
                scenario=sc,
                student_id=student_id,
                course_key=course_key,
                course_code=course_key.split("::", 1)[0],
                primary_term=2,
                status=ScenarioStudentCourseRequest.STATUS_REQUESTED,
                priority=ScenarioStudentCourseRequest.PRIORITY_NORMAL,
                source="test",
            )
            for student_id in students
        ]
    )


def placement_of(ts: TermSection, day: str) -> SectionPlacement:
    return SectionPlacement.objects.get(term_section=ts, day=day)


@dataclass
class Rich:
    sc: TimetableScenario
    b1: DeliveryBoard
    b2: DeliveryBoard
    k: str  # the moving course (AI111), two meetings a week
    p: str  # a planned course with no section yet (AI777), one meeting
    k_s1: TermSection
    k_s2: TermSection
    c1: str
    c1_s1: TermSection
    c1_s2: TermSection
    moving: SectionPlacement  # K-S1 on Sunday

    def ids(self) -> dict[int, str]:
        return {self.moving.id: "<moving>", self.b1.id: "<b1>", self.b2.id: "<b2>"}


def build_rich_fixture() -> Rich:
    """One scenario that gives every legacy evidence kind (threshold 20).

    Board ``Term 2`` (b1): K = AI111 S1 (SUN, TUE 09:00, R101, Dr A) and S2
    (MON, WED 09:00, R102, Dr B); AI222 S1 at MON 13:00 in R101 and S2 at THU
    13:00 (25 students need it and K); AI333 at MON 10:30 and SUN 09:00 (5
    shared); AI444 at THU 09:00 taught by Dr A. Board ``Term 4`` (b2): AI555
    at WED 10:30 (20 shared), AI556 at THU 10:30 (19 shared), AI666 at TUE
    13:00 in R101 taught by Dr A. AI777 is budgeted and not placed; its 22
    students also need K and the rest. K-S3 exists, taught by Dr A, and is not
    placed (the planned preview reads its teacher on both boards).
    """
    names = {
        "AI111": "MOVING",
        "AI222": "PAIRED",
        "AI333": "LIGHT",
        "AI444": "TAUGHT",
        "AI555": "ACROSS",
        "AI556": "ACROSS MINOR",
        "AI666": "ELSEWHERE",
        "AI777": "PLANNED",
    }
    keys = {code: requirement("AI", code, name) for code, name in names.items()}
    sc, b1 = scenario("slot verdict")
    sc.slot_config = LECTURE_COLUMNS
    sc.lab_slot_config = LAB_COLUMNS
    sc.blocked_slots = BLOCKED
    sc.save()
    b2 = DeliveryBoard.objects.create(scenario=sc, label="Term 4", nominal_term=4, program="AI")
    for key in keys.values():
        budget(sc, key, 2)

    dr_a, dr_b = instructor("Dr A"), instructor("Dr B")
    dr_c, dr_d = instructor("Dr C"), instructor("Dr D")
    link("AI", "AI111", dr_a)
    link("AI", "AI111", dr_b, role="co")
    link("AI", "AI444", dr_a)
    link("AI", "AI666", dr_a)
    link("AI", "AI222", dr_c)
    link("AI", "AI333", dr_d)

    k = keys["AI111"]
    k_s1, k_s2 = section(sc, k, "S1"), section(sc, k, "S2")
    place(
        b1,
        k_s1,
        [("SUN", "09:00", "10:15"), ("TUE", "09:00", "10:15")],
        room="R101",
        instructor_name="Dr A",
    )
    place(
        b1,
        k_s2,
        [("MON", "09:00", "10:15"), ("WED", "09:00", "10:15")],
        room="R102",
        instructor_name="Dr B",
    )
    k_s3 = section(sc, k, "S3")
    TermSectionMeeting.objects.create(
        term_section=k_s3, day="SUN", start_time="13:00", end_time="14:15", instructor="Dr A"
    )
    c1 = keys["AI222"]
    c1_s1, c1_s2 = section(sc, c1, "S1"), section(sc, c1, "S2")
    place(b1, c1_s1, [("MON", "13:00", "14:15")], room="R101", instructor_name="Dr C")
    place(b1, c1_s2, [("THU", "13:00", "14:15")], room="R103", instructor_name="Dr C")
    c2 = keys["AI333"]
    place(
        b1, section(sc, c2, "S1"), [("MON", "10:30", "11:45")], room="R104", instructor_name="Dr D"
    )
    place(
        b1, section(sc, c2, "S2"), [("SUN", "09:00", "10:15")], room="R106", instructor_name="Dr D"
    )
    place(
        b1,
        section(sc, keys["AI444"], "S1"),
        [("THU", "09:00", "10:15")],
        room="R105",
        instructor_name="Dr A",
    )
    place(b2, section(sc, keys["AI555"], "S1"), [("WED", "10:30", "11:45")], room="R201")
    place(b2, section(sc, keys["AI556"], "S1"), [("THU", "10:30", "11:45")], room="R202")
    place(
        b2,
        section(sc, keys["AI666"], "S1"),
        [("TUE", "13:00", "14:15")],
        room="R101",
        instructor_name="Dr A",
    )

    demand(sc, k, range(1, 31))
    demand(sc, c1, range(1, 26))
    demand(sc, c2, range(1, 6))
    demand(sc, keys["AI555"], range(1, 21))
    demand(sc, keys["AI556"], range(1, 20))
    demand(sc, keys["AI777"], range(1, 23))
    return Rich(
        sc=sc,
        b1=b1,
        b2=b2,
        k=k,
        p=keys["AI777"],
        k_s1=k_s1,
        k_s2=k_s2,
        c1=c1,
        c1_s1=c1_s1,
        c1_s2=c1_s2,
        moving=placement_of(k_s1, "SUN"),
    )


def normalise_ids(value, ids: dict[int, str]):
    """``value`` with every ``id`` / ``board_id`` the fixture made named."""
    if isinstance(value, dict):
        return {
            key: (
                ids.get(item, item)
                if key in {"id", "board_id"} and isinstance(item, int)
                else normalise_ids(item, ids)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalise_ids(item, ids) for item in value]
    return value


def legacy_requests(rich: Rich) -> dict[str, str]:
    """The three preview GETs the oracle holds, without ``verdict=1``."""
    from urllib.parse import urlencode

    def planned(**params) -> str:
        return f"/ops/tw/boards/{rich.b1.id}/planned-slot-candidates/?{urlencode(params)}"

    return {
        "move": f"/ops/tw/placements/{rich.moving.id}/slot-candidates/",
        # One meeting a week: every lecture and lab column, scored cell by cell.
        "planned_single": planned(course_code="AI777", course_key=rich.p, section_label="S1"),
        # K meets twice a week: whole patterns (the limit keeps the file small;
        # every pattern is still scored and counted in the summary).
        "planned_pattern": planned(
            course_code="AI111", course_key=rich.k, section_label="S3", limit=10
        ),
    }


def record_legacy_previews(client, rich: Rich) -> dict[str, object]:
    """Each preview's full JSON body, ids named, key order kept, and the
    number of queries its request ran (under ``queries``)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    out: dict[str, object] = {}
    queries: dict[str, int] = {}
    for name, url in legacy_requests(rich).items():
        with CaptureQueriesContext(connection) as captured:
            response = client.get(url)
        assert response.status_code == 200, response.content
        out[name] = normalise_ids(json.loads(response.content), rich.ids())
        queries[name] = len(captured.captured_queries)
    out["queries"] = queries
    return out
