"""Add courses in Chromium, against the real endpoints, in English and in Arabic.

The jsdom suite (tests/frontend/exam-add-courses.test.cjs) covers every state
with fake answers. This opens a saved timetable built from part of its scope,
opens Add courses - the list arrives by itself, no Load Courses - searches,
ticks two new courses and adds them through the real server, at 1440px and at
phone width (390px). It also opens the setup section of the saved timetable
and finds a course that is not in it. Nothing leaves the machine
(tests/browser_isolation.py).
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from core import authz, models
from core.models import ExamTimetableRun
from core.services.exam_timetable import build_enrolled_sets_with_meta
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests import exam_linked_parity_corpus as corpus

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

DAYS = corpus.POPULATION_DAYS
PERIODS = corpus.POPULATION_PERIODS
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}
ISOLATES = {code: None for code in range(0x2066, 0x206A)}

# Everything in the dialog inside it, the dialog inside the window, and how far
# the page scrolls sideways.
FITS = """() => {
  const dialog = document.getElementById('examAddCoursesDialog');
  const box = dialog.getBoundingClientRect();
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  return {
    inWindow: box.left >= 0 && box.right <= innerWidth + 1 && box.top >= 0 && box.bottom <= innerHeight + 1,
    outside: [...dialog.querySelectorAll('*')].filter(shown).filter(node => {
      const r = node.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1; })
      .map(node => node.id || node.className || node.tagName),
    page: document.documentElement.scrollWidth - window.innerWidth,
    // The list scrolls; the dialog around it never does, so its buttons stay put.
    overflow: dialog.scrollHeight - dialog.clientHeight,
  };
}"""


def _plain(text: str | None) -> str:
    return (text or "").translate(ISOLATES).strip()


@override_settings(EXAM_JOBS_ENABLED=False)
class ExamAddCoursesBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        cache.clear()
        authz._rate_buckets.clear()
        corpus.create_population(models)
        ensure_role_groups()
        admin = get_user_model().objects.create_superuser(username="add-browser-admin")
        client = Client()
        client.force_login(admin)
        _, metadata = build_enrolled_sets_with_meta(**SCOPE)
        self.codes = sorted(metadata)
        part = self.codes[:6]
        response = client.post(
            reverse("exam_timetable_build"),
            {
                "label": "Part one",
                "days": DAYS,
                "periods": PERIODS,
                "max_per_day": 2,
                **SCOPE,
                "assign_rooms": True,
                "selected_courses": part,
                "selected_course_entries": [{"course_code": c, **metadata[c]} for c in part],
                "pinned": [],
            },
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        self.run_id = response.json()["run_id"]

    def _page(self, language: str, width: int):
        user = get_user_model().objects.create_user(
            username=f"add-{language}-{get_user_model().objects.count()}"
        )
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": 900},
        )
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
        self.addCleanup(context.close)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"{self.live_server_url}/exam-timetable/?run={self.run_id}")
        expect(page.locator("#schedGrid .et-grid")).to_be_visible()
        self.assertEqual(page.evaluate("() => document.documentElement.lang"), language)
        return page

    def _add_two(self, language: str, width: int) -> None:
        where = f"{language} {width}px"
        page = self._page(language, width)
        runs = ExamTimetableRun.objects.count()
        button = page.locator("#addCoursesBtn")
        expect(button).to_be_visible()
        expect(page.locator("#loadedRunBuildNote")).to_be_hidden()  # setup is collapsed
        button.click()
        dialog = page.locator("#examAddCoursesDialog")
        expect(dialog).to_be_visible()
        rows = page.locator("#examAddCoursesList .et-add-row")
        # Every course of the scope, at once: no Load Courses.
        expect(rows).to_have_count(len(self.codes))
        expect(page.locator("#examAddCoursesList input[type=checkbox]")).to_have_count(
            len(self.codes) - 6
        )
        expect(page.locator("#examAddCoursesList input:checked")).to_have_count(0)
        self.assertEqual(
            page.evaluate("() => document.activeElement.id"), "examAddCoursesSearch", where
        )
        facts = page.evaluate(FITS)
        self.assertTrue(facts["inWindow"], f"{where}: the list leaves the window")
        self.assertEqual(facts["outside"], [], f"{where}: something spills out of the list")
        self.assertLessEqual(facts["page"], 0, f"{where}: the page scrolls sideways")
        self.assertLessEqual(facts["overflow"], 1, f"{where}: the list pushes the buttons away")
        expect(page.locator("#confirmAddCourses")).to_be_in_viewport()
        # Search, then tick two new courses.
        new = self.codes[6:8]
        page.locator("#examAddCoursesSearch").fill(new[0])
        expect(page.locator("#examAddCoursesList .et-add-row:not([hidden])")).to_have_count(1)
        page.locator("#examAddCoursesList input[type=checkbox]:visible").first.check()
        page.locator("#examAddCoursesSearch").fill(new[1])
        page.locator("#examAddCoursesList input[type=checkbox]:visible").first.check()
        page.locator("#examAddCoursesSearch").fill("")
        expect(page.locator("#examAddCoursesList input:checked")).to_have_count(2)
        page.locator("#confirmAddCourses").click()
        expect(dialog).to_be_hidden()
        report = page.locator("#examRepairReport")
        expect(report).to_contain_text(new[0])
        text = _plain(report.inner_text())
        self.assertIn("أُضيف" if language == "ar" else "Added 2 courses", text, f"{where}: {text}")
        self.assertEqual(ExamTimetableRun.objects.count(), runs + 1, where)
        cards = page.evaluate(
            "() => [...document.querySelectorAll('#schedGrid .et-course')].map(card => card.dataset.course)"
        )
        self.assertTrue(set(new) <= set(cards), where)

    def test_adding_two_courses_in_english_and_arabic_on_a_laptop(self) -> None:
        for language in ("en", "ar"):
            self._add_two(language, 1440)

    def test_adding_two_courses_in_english_and_arabic_on_a_phone(self) -> None:
        for language in ("en", "ar"):
            self._add_two(language, 390)

    def test_the_setup_of_a_saved_timetable_finds_a_course_not_in_it(self) -> None:
        for language in ("en", "ar"):
            page = self._page(language, 1440)
            page.locator("#examSetupSummary").click()
            expect(page.locator("#loadedRunBuildNote")).to_be_visible()
            outside = page.locator("#courseOutsideList .et-course-option")
            expect(outside).to_have_count(len(self.codes) - 6)
            self.assertEqual(page.locator("#courseOutsideList input").count(), 0)
            page.locator("#courseSearch").fill(self.codes[-1])
            expect(
                page.locator("#courseOutsideList .et-course-option:not([hidden])")
            ).to_have_count(1)
            expect(page.locator("#courseNoMatches")).to_be_hidden()
            # The saved board is still on screen, nothing to save.
            expect(page.locator("#saveLoadedBtn")).to_be_disabled()
