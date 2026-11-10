"""A course taught as two 100-minute meetings, end to end in a real browser.

Owner, 2026-11-10: a 3-credit course may meet twice a week for 100 minutes
(``ScenarioSectionBudget.meeting_pattern = "2x100"``), in the 100-minute
columns and a lecture room. These drive the classic Timetable Workspace page
against the real server: the Place dialog offers such a course whole 2 x 100
patterns that are lectures (no "Lab" tag), placing one writes two 100-minute
meetings, and the Add course / section dialog changes a course's weekly
meetings and says which placed section still meets the old way, and reads in
the dark theme too.

The fixture (``WorkspaceBrowserMixin``): AI111 has S1 placed SUN/TUE
09:00-10:15; AI222 has S1 placed MON/WED 13:00-14:15 and S2 still to place.
"""

from __future__ import annotations

import json

from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from core.models import ScenarioSectionBudget, SectionPlacement
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

AI111 = "AI111::INTRO_AI"
AI222 = "AI222::SEARCH"
#: The keyboard on AI222's S2 row in the list.
ON_S2 = """(() => { const a = document.activeElement;
  return Boolean(a && a.dataset.course === 'AI222' && /^place:.*\\|S2$/.test(a.dataset.focusKey || '')); })()"""
CHOSEN_PLACE = "document.activeElement?.name === 'twPlaceChoice'"
#: Every piece of words on show in the open Add course / section dialog (its
#: title, lines, labels and choices), against the surface behind it, as the
#: WCAG contrast ratio: [what, ratio].
DIALOG_CONTRAST = """() => {
  const dialog = document.querySelector('dialog[open]');
  const rgb = value => (value.match(/[\\d.]+/g) || []).map(Number);
  const channel = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  const backdrop = node => {
    for (let at = node; at; at = at.parentElement) {
      const c = rgb(getComputedStyle(at).backgroundColor);
      if (c.length === 3 || c[3] === 1) return c;
    }
    return [255, 255, 255];
  };
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return [...dialog.querySelectorAll('h2, p, label, select, small')]
    .filter(node => shown(node) && node.textContent.trim())
    .map(node => {
      const a = luminance(rgb(getComputedStyle(node).color)), b = luminance(backdrop(node));
      return [`${node.tagName.toLowerCase()} ${node.textContent.trim().slice(0, 40)}`,
        Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100];
    });
}"""
#: The default grid's 100-minute columns an automatic placer may use.
DAYTIME_100 = {
    ("09:00", "10:40"),
    ("10:45", "12:25"),
    ("13:00", "14:40"),
    ("14:45", "16:25"),
    ("16:30", "18:10"),
}


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


class MeetingPatternBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """These write, to pytest's database."""

    maxDiff = None

    def _open_list(self, page) -> None:
        # Below 1600px the list starts collapsed to its rail: [ opens it.
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.press("BracketLeft")
        page.wait_for_function("document.getElementById('twWorkspace').dataset.left === 'open'")
        page.wait_for_function(
            "document.querySelectorAll('#twSectionList [tabindex=\"0\"]').length === 1"
        )

    def test_place_offers_a_2x100_course_only_lecture_patterns_and_writes_two_100_minute_meetings(
        self,
    ) -> None:
        ScenarioSectionBudget.objects.filter(scenario=self.scenario, course_key=AI222).update(
            meeting_pattern="2x100"
        )
        page, _sent, errors = self._load("en", "light", 1440, 790)
        posts: list[tuple[str, str]] = []
        page.on(
            "request",
            lambda r: posts.append((r.url, r.post_data)) if r.method == "POST" else None,
        )
        self._open_list(page)
        page.focus("#twSectionSearch")
        page.keyboard.press("Tab")  # the header's "+ Course / section"
        page.keyboard.press("Tab")  # the list's one stop
        for _ in range(10):
            if page.evaluate(ON_S2):
                break
            page.keyboard.press("ArrowDown")
        self.assertTrue(page.evaluate(ON_S2), "↓ reaches AI222 S2")
        page.keyboard.press("Enter")
        page.wait_for_function(CHOSEN_PLACE)

        body_text = page.evaluate("document.getElementById('twPlaceBody').textContent")
        self.assertNotIn("Lab", body_text, "a 2 x 100 course's meetings are lectures")
        self.assertIn(
            "2 × 100 min", page.evaluate("document.getElementById('twPlaceDialog').textContent")
        )

        page.keyboard.press("Enter")  # place the chosen (best) pattern
        page.wait_for_function(
            """() => !document.getElementById('twPlaceDialog').open
              && document.querySelector('.tw-workspace').dataset.busy === 'false'"""
        )
        creates = [body for url, body in posts if url.endswith("/placements/create-planned/")]
        self.assertEqual(len(creates), 1, "one POST")
        meetings = json.loads(creates[0])["meetings"]
        self.assertEqual(len(meetings), 2)
        placed = list(
            SectionPlacement.objects.filter(
                term_section__scenario=self.scenario,
                term_section__course_key=AI222,
                term_section__section="S2",
            ).values_list("day", "start_time", "end_time")
        )
        self.assertEqual(len(placed), 2)
        self.assertNotEqual(placed[0][0], placed[1][0], "two different days")
        self.assertLessEqual({(start, end) for _day, start, end in placed}, DAYTIME_100)
        self.assertEqual({_minutes(end) - _minutes(start) for _day, start, end in placed}, {100})
        self.assertEqual(errors, [])

    def test_the_add_dialog_sets_a_course_to_2x100_and_names_its_section_left_at_2x75(self) -> None:
        page, _sent, errors = self._load("en", "light", 1920, 1080)
        page.click("#twAddCourse")
        dialog = page.locator("dialog[open]")
        dialog.locator("select").first.select_option(AI222)
        weekly = dialog.locator('label:visible:has-text("Weekly meetings") select')
        self.assertEqual(weekly.count(), 1, "one visible weekly-meetings choice")
        self.assertEqual(weekly.input_value(), "", "AI222 is on its credit hours' pattern")
        weekly.select_option("2x100")
        dialog.get_by_role("button", name="Save weekly meetings").click()
        status = dialog.locator('[role="status"]')
        status.filter(has_text="Saved").wait_for()
        words = status.text_content()
        self.assertIn("2 × 100 min", words)
        self.assertIn("1 placed section still meets the old way", words)
        self.assertIn("S1", words)
        self.assertEqual(
            ScenarioSectionBudget.objects.get(
                scenario=self.scenario, course_key=AI222
            ).meeting_pattern,
            "2x100",
        )
        # Nothing was moved: S1 keeps its two 75-minute meetings.
        self.assertEqual(
            sorted(
                SectionPlacement.objects.filter(
                    term_section__scenario=self.scenario, term_section__course_key=AI222
                ).values_list("day", "start_time", "end_time")
            ),
            [("MON", "13:00", "14:15"), ("WED", "13:00", "14:15")],
        )
        dialog.get_by_role("button", name="Close").click()
        # The section still to place (S2) names the course's new weekly meetings.
        page.wait_for_function(
            """() => [...document.querySelectorAll('#twSectionList [data-course="AI222"]')]
                  .some(el => el.textContent.includes('2 × 100 min'))"""
        )
        self.assertEqual(errors, [])

    def test_the_add_dialog_reads_in_the_dark_theme(self) -> None:
        # The dialog sits on the theme's --surface, which is dark in the dark
        # theme: its words must follow the theme too (WCAG AA, 4.5:1), for a
        # new course and for a course's weekly meetings alike.
        page, _sent, errors = self._load("en", "dark", 1920, 1080)
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        page.click("#twAddCourse")
        dialog = page.locator("dialog[open]")
        dialog.wait_for()
        new_course = page.evaluate(DIALOG_CONTRAST)
        dialog.locator("select").first.select_option(AI222)
        dialog.get_by_text("Weekly meetings now: 2 × 75 min").wait_for()
        weekly = page.evaluate(DIALOG_CONTRAST)
        shown = [what for what, _ratio in new_course + weekly]
        self.assertIn("h2 Add course / section", shown)
        self.assertIn("p Weekly meetings now: 2 × 75 min", shown)
        self.assertTrue(any(what.startswith("select ") for what in shown), shown)
        self.assertEqual(
            [(what, ratio) for what, ratio in new_course + weekly if ratio < 4.5],
            [],
            "every word in the dialog reads against its dark surface",
        )
        self.assertEqual(errors, [])
