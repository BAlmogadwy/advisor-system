"""A programme's default weekly meetings on the Timetable Workspace, in a real browser.

Owner, 2026-11-10: each programme sets how its 3-credit courses meet each week
(``ProgrammeRequirement.meeting_pattern``), both campuses alike. This drives the
classic Timetable Workspace page against the real server: when a course of the
scenario differs from what its programmes agreed on, the line over the course
list says so and the course carries a chip; the course in the line opens the
Add course / section dialog on it, which names the programmes' default; and
"Use programme defaults" saves it through the weekly-meetings endpoint, after
which the line says the course meets its programmes' default.

The fixture (``WorkspaceBrowserMixin``): scenario of programme AI; AI222
(AI's "Search", 3 credits) has S1 placed MON/WED 13:00-14:15 and S2 still to
place, on its credit hours' 2 x 75.
"""

from __future__ import annotations

import json

from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from core.models import ProgrammeRequirement, ScenarioSectionBudget
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

AI222 = "AI222::SEARCH"
#: The words on show in AI222's rows of the course list.
AI222_ROWS = """() => [...document.querySelectorAll('#twSectionList [data-course="AI222"]')]
  .map(el => el.textContent.replace(/\\s+/g, ' ').trim()).join(' | ')"""


class ProgrammeMeetingPatternBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """These write, to pytest's database."""

    maxDiff = None

    def test_the_line_names_a_course_off_its_programmes_default_and_puts_it_back(self) -> None:
        ProgrammeRequirement.objects.filter(program="AI", course_code="AI222").update(
            meeting_pattern="2x100"
        )
        page, sent, errors = self._load("en", "light", 1920, 1080)
        posts: list[tuple[str, str]] = []
        page.on(
            "request",
            lambda r: posts.append((r.url, r.post_data)) if r.method == "POST" else None,
        )
        summary = page.locator("#twMeetingSummary")
        summary.locator("p").wait_for()
        self.assertEqual(
            summary.locator("p").text_content(),
            "Weekly meetings: Differs from programme default: AI222",
        )
        self.assertIn("programme default 2 × 100 min", page.evaluate(AI222_ROWS))

        # The course in the line opens the dialog on it, naming its programmes' default.
        summary.locator("button", has_text="AI222").click()
        dialog = page.locator("dialog[open]")
        self.assertEqual(dialog.locator("select").first.input_value(), AI222)
        programmes = dialog.locator("p", has_text="Programme default:")
        self.assertEqual(programmes.text_content(), "Programme default: 2 × 100 min (⁦AI⁩)")
        self.assertTrue(dialog.get_by_role("button", name="Use programme default").is_visible())
        dialog.get_by_role("button", name="Close").click()

        # "Use programme defaults (1)": saved through the weekly-meetings endpoint.
        summary.get_by_role("button", name="Use programme defaults (1)").click()
        summary.locator("p", has_text="AI222 2 × 100 min (programme default)").wait_for()
        self.assertEqual(
            summary.locator("p").text_content(),
            "Weekly meetings: AI222 2 × 100 min (programme default)",
        )
        self.assertEqual(
            summary.get_by_role("button", name="Use programme defaults (1)").count(), 0
        )
        saves = [
            json.loads(body) for url, body in posts if url.endswith("/budget/meeting-pattern/")
        ]
        self.assertEqual(saves, [{"course_key": AI222, "meeting_pattern": "2x100"}])
        self.assertEqual(
            ScenarioSectionBudget.objects.get(
                scenario=self.scenario, course_key=AI222
            ).meeting_pattern,
            "2x100",
        )
        # The row still to place names its new weekly meetings, and no chip.
        page.wait_for_function(f"({AI222_ROWS})().includes('2 × 100 min')")
        self.assertNotIn("programme default", page.evaluate(AI222_ROWS))
        self.assertEqual(
            [line for line in sent if "/budget/meeting-pattern/" in line],
            [
                f"POST {self.live_server_url}/ops/tw/scenarios/{self.scenario.id}/budget/meeting-pattern/"
            ],
        )
        self.assertEqual(errors, [])
