"""The planner board against the REAL endpoints: no route is faked.

The test database is seeded with the curriculum of tests/test_planner_graduation.py (one
registered course, two suggestions with sections); the page loads the student, builds with the
real solver, shows the real graduation projection, lets the adviser pick a section, rebuilds,
saves through the real endpoint and reads the student back. Every earlier board test answers its
requests from a fake: this is the one that proves the screen and the server agree.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from core.models import StudentTermSection, TermSectionMeeting
from tests.test_exam_rosters_browser import WIDE_FONT  # noqa: F401  (the harness of the others)
from tests.test_planner_graduation import STUDENT_ID, TERM, YEAR, _seed_curriculum

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect


class PlannerBoardEndToEndTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = playwright_api.sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _page(self, language="en"):
        user = get_user_model().objects.create_superuser(
            username="planner-e2e", email="planner-e2e@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": 1440, "height": 900},
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        page = context.new_page()
        self.errors: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(self.errors, [], "the page threw"))
        return page

    def _saved(self) -> set[tuple[str, str]]:
        """The (course, section) pairs saved as this student's planned timetable."""
        rows = StudentTermSection.objects.filter(
            student_id=STUDENT_ID, academic_year=str(YEAR), term=str(TERM), source="planner"
        ).select_related("term_section")
        return {(r.term_section.course_key, r.term_section.section) for r in rows}

    @staticmethod
    def _week(page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbWeek .plb-b",
            "bs => bs.map(b => [...b.children].map(c => c.textContent.trim()).join(' ')).sort()",
        )

    def _week_after_reload(self, page) -> list[str]:
        page.reload()
        expect(page.locator("#plbRoot")).to_have_attribute("data-loaded", "true")
        expect(page.locator("#plbWeek .plb-b").first).to_be_visible()
        return self._week(page)

    def test_load_build_pick_a_section_rebuild_apply_and_read_back(self) -> None:
        sections = _seed_curriculum()
        # AI331 has two sections (M1 Monday, M2 Wednesday): give them different times so that
        # the section the adviser picks shows in the saved rows and in the week.
        TermSectionMeeting.objects.filter(term_section=sections["AI331", "M2"]).update(
            start_time="11:00", end_time="12:15"
        )
        # No registrar timetable: the saved sections are then what the page reads back as the week
        # (a registrar row outranks them, which the page says and tests/test_planner_board_build_browser.py covers).
        StudentTermSection.objects.filter(student_id=STUDENT_ID).delete()
        page = self._page()
        page.goto(
            f"{self.live_server_url}/planner/?layout=board&student={STUDENT_ID}&year={YEAR}&term={TERM}"
        )
        expect(page.locator("#plbRoot")).to_have_attribute("data-loaded", "true")
        expect(page.locator("#plbContext")).to_have_text(f"{STUDENT_ID} · {YEAR}/{TERM}")
        expect(page.locator("#plbStudent")).to_contain_text("Planner Sample Student")
        new_rows = "#plbTable tbody[data-group='new'] tr.plb-tr:not(.plb-tr--note)"
        expect(page.locator(new_rows)).not_to_have_count(0)
        planned = page.eval_on_selector_all(new_rows, "rows => rows.map(r => r.dataset.code)")
        self.assertIn("AI331", planned)
        # The sections of the course were read from the server (the catalogue).
        expect(page.locator("#plbTable tr[data-code='AI331'] .plb-sc")).not_to_have_text("—")

        # Build: the real endpoint and the real solver.
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild", timeout=45000)
        expect(page.locator("#plbTabs [role='tab']")).not_to_have_count(1)
        # The real projection arrives: the band leaves its loading and its empty states.
        expect(page.locator("#plbGrad")).to_have_attribute("data-state", "ready", timeout=45000)
        expect(page.locator("#plbReview")).to_be_enabled()

        # Open a planned course and tick the section that is NOT the one the build chose.
        page.click("#plbTable tr[data-code='AI331'] button[data-act='detail']")
        expect(page.locator("#plbDetail")).to_be_visible()
        boxes = page.locator("#plbDetail input[name='plbOnly']")
        self.assertEqual(boxes.count(), 2)
        page.locator("#plbDetail [data-tsid] >> text=§M2").first.click()
        expect(page.locator("#plbDetail input[name='plbOnly']:checked")).to_have_count(1)
        self.assertEqual(
            page.locator("#plbDetail input[name='plbOnly']:checked").get_attribute("value"),
            str(sections["AI331", "M2"].pk),
        )
        # The tick changed the plan: the build on screen is out of date and cannot be applied.
        expect(page.locator("#plbReview")).to_be_disabled()

        # Rebuild with the pinned section, then review and apply.
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild", timeout=45000)
        expect(page.locator("#plbBuild")).to_be_enabled(timeout=45000)
        expect(page.locator("#plbGrad")).to_have_attribute("data-state", "ready", timeout=45000)
        expect(page.locator("#plbReview")).to_be_enabled()
        page.click("#plbReview")
        expect(page.locator("#plbApplyDialog")).to_be_visible()
        listed = page.locator("#plbApplyList li").all_inner_texts()
        self.assertTrue(any("AI331" in line and "M2" in line for line in listed), listed)
        page.click("#plbApplyGo")
        expect(page.locator("#plbApplyDialog")).to_be_hidden(timeout=30000)
        expect(page.locator("#plbStatus")).to_have_text("Saved", timeout=30000)

        # The database holds exactly the planned sections: AI331 as M2 (the pinned one), and each
        # other course of the plan. Nothing else was written for the term.
        saved = self._saved()
        self.assertIn(("AI331", "M2"), saved)
        self.assertNotIn(("AI331", "M1"), saved)
        self.assertEqual({course for course, _ in saved}, set(planned))
        self.assertEqual(
            StudentTermSection.objects.filter(student_id=STUDENT_ID).count(), len(saved)
        )

        # The page reloaded the student: the saved line is on screen, and so is the saved section
        # when the page is opened afresh.
        expect(page.locator("#plbBuildLine")).to_contain_text(f"Saved {len(saved)} sections")
        before = self._week(page)
        week = self._week_after_reload(page)
        self.assertEqual(week, before)
        self.assertIn("AI331 §M2 11:00-12:15", week)
        self.assertNotIn("AI331 §M1 09:00-10:15", week)
