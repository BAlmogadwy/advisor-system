"""The scenario the B2 tests share (stage 3): English sections with no one
complete weekly pattern beside sections whose teachers are known.

Not collected (no ``test_`` prefix). Used by
``tests/test_tw_incomplete_english_degrade.py`` (the server) and
``tests/test_timetable_workspace_f17_browser.py`` (Chromium).
"""

from __future__ import annotations

from core.models import DeliveryBoard, SectionInstructor, TermSection
from tests.one_teacher_support import (
    budget,
    instructor,
    link,
    place,
    requirement,
    scenario,
    section,
)

AM = [(day, "09:00", "12:00") for day in ("SUN", "MON", "WED", "THU")]
PM = [(day, "13:00", "16:00") for day in ("SUN", "MON", "WED", "THU")]
#: QA 662/664's shape: one board, three ordinary meetings, no English pattern.
LEGACY = [("SUN", "13:00", "14:15"), ("WED", "13:00", "14:15"), ("THU", "14:45", "16:25")]


class IncompleteEnglishScenario:
    """Two boards of programme AI.

    - ENG102 S1: complete on both boards but on DIFFERENT patterns (AM on Term
      2, PM on Term 4); stored engine row Dr E. Unknown, Dr E's load held.
    - ENG102 S2: complete PM on Term 2; stored engine row Dr E. Keeps Dr E.
    - ENG102 S3: complete AM on Term 4; no row. Dr E is the course's only
      teacher and holds two sections (S1, S2), the English cap: TBD (d10).
    - ENG101 S1: QA's legacy shape on Term 2, no links, a registrar name on its
      meetings. Unknown, never its meeting name.
    - AI201 S1, S2 on Term 2: Dr A (no row yet: derived). Keep Dr A.

    ``broken=False``: ENG102 S1 is AM on both boards and ENG101 S1 is not
    placed — a consistent scenario.
    """

    def __init__(self, *, broken: bool = True, name: str = "one teacher") -> None:
        self.sc, self.b1 = scenario(name)
        self.b2 = DeliveryBoard.objects.create(
            scenario=self.sc, label="Term 4", nominal_term=4, program="AI", display_order=2
        )
        DeliveryBoard.objects.filter(pk=self.b1.pk).update(display_order=1)
        eng = requirement("AI", "ENG102", "ENGLISH LANGUAGE SKILLS II")
        ai = requirement("AI", "AI201", "DATA STRUCTURES")
        self.dr_e, self.dr_a = instructor("Dr E"), instructor("Dr A")
        link("AI", "ENG102", self.dr_e)
        link("AI", "AI201", self.dr_a)
        budget(self.sc, eng, 3)
        budget(self.sc, ai, 2)
        self.s1, self.s2, self.s3 = (section(self.sc, eng, f"S{n}") for n in (1, 2, 3))
        place(self.b1, self.s1, AM, instructor_name="Dr E", room="")
        place(self.b2, self.s1, PM if broken else AM, instructor_name="Dr E", room="")
        place(self.b1, self.s2, PM, instructor_name="Dr E", room="")
        place(self.b2, self.s3, AM, room="")
        self.legacy = TermSection.objects.create(
            scenario=self.sc,
            course_key="ENG101",
            course_code="ENG101",
            course_number="ENG101",
            course_name="ENGLISH LANGUAGE SKILLS I",
            section="S1",
            source_tag="tw_auto",
        )
        if broken:
            place(self.b1, self.legacy, LEGACY, instructor_name="Registrar Name")
        self.a1, self.a2 = section(self.sc, ai, "S1"), section(self.sc, ai, "S2")
        place(self.b1, self.a1, [("TUE", "09:00", "10:15")])
        place(self.b1, self.a2, [("TUE", "10:30", "11:45")])
        for ts in (self.s1, self.s2):
            SectionInstructor.objects.create(
                scenario=self.sc,
                course_key=ts.course_key.upper(),
                section=ts.section,
                instructor=self.dr_e,
                role="primary",
                source="engine",
            )
        self.eng = eng
        self.ai = ai
