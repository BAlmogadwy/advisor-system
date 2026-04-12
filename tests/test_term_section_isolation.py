"""
Regression test: TermSection scenario isolation.

Proves that two TimetableScenarios can both have CS101/S1 without
sharing the same TermSection row, and that deleting one scenario's
data does not affect the other.
"""

import pytest
from django.test import TestCase

from core.models import (
    TermSection,
    TermSectionMeeting,
    TimetableScenario,
)


@pytest.mark.django_db
class TestTermSectionIsolation(TestCase):
    """Two scenarios can have the same course/section independently."""

    def setUp(self):
        self.scenario_a = TimetableScenario.objects.create(
            academic_year="1448", term="1", name="Test A"
        )
        self.scenario_b = TimetableScenario.objects.create(
            academic_year="1448", term="1", name="Test B"
        )

    def test_separate_term_sections(self):
        """get_or_create with different scenarios produces different PKs."""
        ts_a, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_a,
            course_key="CS101",
            section="S1",
            defaults={
                "course_code": "CS101",
                "course_number": "CS101",
                "source_tag": "tw_auto",
            },
        )
        ts_b, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_b,
            course_key="CS101",
            section="S1",
            defaults={
                "course_code": "CS101",
                "course_number": "CS101",
                "source_tag": "tw_auto",
            },
        )
        self.assertNotEqual(ts_a.pk, ts_b.pk)
        self.assertEqual(ts_a.scenario_id, self.scenario_a.pk)
        self.assertEqual(ts_b.scenario_id, self.scenario_b.pk)

    def test_delete_scenario_a_preserves_b(self):
        """Deleting scenario A cascades its TermSections but not B's."""
        ts_a, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_a,
            course_key="MATH201",
            section="S1",
            defaults={
                "course_code": "MATH201",
                "course_number": "MATH201",
                "source_tag": "tw_auto",
            },
        )
        ts_b, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_b,
            course_key="MATH201",
            section="S1",
            defaults={
                "course_code": "MATH201",
                "course_number": "MATH201",
                "source_tag": "tw_auto",
            },
        )

        # Add meetings to both
        TermSectionMeeting.objects.create(
            term_section=ts_a, day="SUN", start_time="09:00", end_time="10:15"
        )
        TermSectionMeeting.objects.create(
            term_section=ts_b, day="MON", start_time="09:00", end_time="10:15"
        )

        # Delete scenario A entirely
        self.scenario_a.delete()

        # B's section and meetings must survive
        self.assertTrue(TermSection.objects.filter(pk=ts_b.pk).exists())
        self.assertEqual(TermSectionMeeting.objects.filter(term_section=ts_b).count(), 1)
        # A's section must be gone (CASCADE)
        self.assertFalse(TermSection.objects.filter(pk=ts_a.pk).exists())

    def test_delete_meetings_one_scenario_only(self):
        """Deleting meetings for scenario A does not affect B."""
        ts_a, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_a,
            course_key="ENG101",
            section="S1",
            defaults={
                "course_code": "ENG101",
                "course_number": "ENG101",
                "source_tag": "tw_auto",
            },
        )
        ts_b, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_b,
            course_key="ENG101",
            section="S1",
            defaults={
                "course_code": "ENG101",
                "course_number": "ENG101",
                "source_tag": "tw_auto",
            },
        )

        TermSectionMeeting.objects.create(
            term_section=ts_a, day="SUN", start_time="09:00", end_time="10:15"
        )
        TermSectionMeeting.objects.create(
            term_section=ts_a, day="TUE", start_time="09:00", end_time="10:15"
        )
        TermSectionMeeting.objects.create(
            term_section=ts_b, day="MON", start_time="13:00", end_time="14:15"
        )

        # Delete A's meetings
        TermSectionMeeting.objects.filter(term_section=ts_a).delete()

        # B's meetings untouched
        self.assertEqual(TermSectionMeeting.objects.filter(term_section=ts_b).count(), 1)
        b_meeting = TermSectionMeeting.objects.get(term_section=ts_b)
        self.assertEqual(b_meeting.day, "MON")

    def test_global_section_stays_null(self):
        """Imported/scraped sections have scenario=NULL and their own uniqueness."""
        ts_global, _ = TermSection.objects.get_or_create(
            scenario=None,
            course_key="PHYS101",
            section="S1",
            defaults={
                "course_code": "PHYS101",
                "course_number": "PHYS101",
                "source_tag": "scraper_timetable",
            },
        )
        self.assertIsNone(ts_global.scenario_id)

        # Scenario-owned section with same course/section is separate
        ts_owned, _ = TermSection.objects.get_or_create(
            scenario=self.scenario_a,
            course_key="PHYS101",
            section="S1",
            defaults={
                "course_code": "PHYS101",
                "course_number": "PHYS101",
                "source_tag": "tw_auto",
            },
        )
        self.assertNotEqual(ts_global.pk, ts_owned.pk)
        self.assertEqual(ts_owned.scenario_id, self.scenario_a.pk)
