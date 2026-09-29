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

# Everything in the dialog inside it, the dialog inside the window, how far
# the page scrolls sideways, how far the dialog's body scrolls, and the list's
# height beside the floor it gives its height up to on a short window.
FITS = """() => {
  const dialog = document.getElementById('examAddCoursesDialog');
  const body = dialog.querySelector('.et-add-body');
  const box = dialog.getBoundingClientRect();
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  return {
    inWindow: box.left >= 0 && box.right <= innerWidth + 1 && box.top >= 0 && box.bottom <= innerHeight + 1,
    outside: [...dialog.querySelectorAll('*')].filter(shown).filter(node => {
      const r = node.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1; })
      .map(node => node.id || node.className || node.tagName),
    page: document.documentElement.scrollWidth - window.innerWidth,
    // The list scrolls; on a tall window the body around it never does.
    overflow: body.scrollHeight - body.clientHeight,
    list: document.getElementById('examAddCoursesList').getBoundingClientRect().height,
    floor: Math.min(140, innerHeight * 0.22),
  };
}"""

# On a window too short for the list, Cancel and Add are still inside the
# dialog, inside the window, and the element under their own centre - a mouse
# or a finger reaches them without scrolling anything.
REACHABLE = """() => {
  const dialog = document.getElementById('examAddCoursesDialog');
  const box = dialog.getBoundingClientRect();
  return ['cancelAddCourses', 'confirmAddCourses'].map(id => {
    const button = document.getElementById(id);
    const r = button.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    return {
      id,
      inDialog: r.top >= box.top - 1 && r.bottom <= box.bottom + 1 && r.left >= box.left - 1 && r.right <= box.right + 1,
      inWindow: r.top >= 0 && r.bottom <= innerHeight + 1,
      hit: document.elementFromPoint(x, y) === button,
    };
  });
}"""

# A control or message in sight: the element under its own centre is itself,
# so nothing - the buttons included - is drawn over it (WCAG 2.4.11); and
# whether it has focus.
IN_VIEW = """(id) => {
  const node = document.getElementById(id);
  const r = node.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {
    id,
    shown: r.width > 1 && r.height > 1,
    hit: hit === node,
    under: hit ? hit.id || String(hit.className) || hit.tagName : null,
    focused: document.activeElement === node,
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

    def _page(self, language: str, width: int, height: int = 900):
        user = get_user_model().objects.create_user(
            username=f"add-{language}-{get_user_model().objects.count()}"
        )
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": height},
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
        self.assertLessEqual(facts["overflow"], 1, f"{where}: the list pushes the name away")
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

    def _buttons_in_reach(self, page, where: str) -> None:
        for button in page.evaluate(REACHABLE):
            self.assertTrue(all(button.values()), f"{where}: {button}")

    def _in_view(self, page, element: str, where: str, *, focused: bool) -> None:
        facts = page.evaluate(IN_VIEW, element)
        self.assertTrue(facts["shown"] and facts["hit"], f"{where}: {facts}")
        self.assertEqual(facts["focused"], focused, f"{where}: {facts}")

    def test_on_short_windows_the_buttons_stay_in_reach(self) -> None:
        """A laptop at 125% scaling, a short window, and phones either way up.

        Cancel and Add stay in reach whatever has focus. By keyboard, the
        search, the show filter and the name are each in sight when Tab
        reaches them - never under the buttons (WCAG 2.4.11) - and so are the
        refusal of an empty name and the field it focuses. By mouse or finger,
        the dialog scrolls to the name. The list gives up its height first.
        """
        for width, height in ((1280, 560), (1093, 525), (360, 640), (390, 600), (844, 390)):
            for language in ("en", "ar"):
                where = f"{language} {width}x{height}"
                page = self._page(language, width, height)
                page.locator("#addCoursesBtn").click()
                expect(page.locator("#examAddCoursesDialog")).to_be_visible()
                expect(page.locator("#examAddCoursesList .et-add-row")).to_have_count(
                    len(self.codes)
                )
                facts = page.evaluate(FITS)
                self.assertTrue(facts["inWindow"], f"{where}: the list leaves the window")
                self.assertLessEqual(facts["page"], 0, f"{where}: the page scrolls sideways")
                # Too short for everything, so the body scrolls - and the list
                # has already given up its height, down to 22% of the window:
                # each row it kept is more to scroll between the search and the
                # name, and a finger on the list scrolls the list, never the
                # dialog.
                self.assertGreater(facts["overflow"], 1, f"{where}: {facts}")
                self.assertLessEqual(facts["list"], facts["floor"] + 1, f"{where}: {facts}")
                self._buttons_in_reach(page, where)
                # Keyboard: the search has focus on open; Tab reaches the filter.
                self._in_view(page, "examAddCoursesSearch", where, focused=True)
                page.keyboard.press("Tab")
                self._in_view(page, "examAddCoursesShow", where, focused=True)
                # Mouse or finger: a wheel over the dialog's text scrolls it to the name.
                text = page.locator("#examAddCoursesHelp").bounding_box()
                assert text is not None
                page.mouse.move(text["x"] + text["width"] / 2, text["y"] + text["height"] / 2)
                page.mouse.wheel(0, 2000)
                try:
                    page.wait_for_function(
                        f"(id) => ({IN_VIEW})(id).hit", arg="examAddCoursesName", timeout=3000
                    )
                except playwright_api.TimeoutError:
                    name = page.evaluate(IN_VIEW, "examAddCoursesName")
                    self.fail(f"{where}: the wheel never brought the name into sight: {name}")
                # A tick scrolls the list, and the dialog with it: still in reach.
                page.locator("#examAddCoursesList input[type=checkbox]").last.check()
                self._buttons_in_reach(page, f"{where} after a tick")
                # Tab from the last course reaches the name.
                page.keyboard.press("Tab")
                self._in_view(page, "examAddCoursesName", where, focused=True)
                # Back up at the search, the name emptied and out of sight: Add
                # refuses, and the refusal and the field it focuses are in sight.
                page.locator("#examAddCoursesName").fill("")
                page.locator("#examAddCoursesSearch").focus()
                page.locator("#confirmAddCourses").click()
                expect(page.locator("#examAddCoursesError")).to_be_visible()
                self._in_view(page, "examAddCoursesName", f"{where} refused", focused=True)
                self._in_view(page, "examAddCoursesError", f"{where} refused", focused=False)
                self._buttons_in_reach(page, f"{where} refused")
                page.close()

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
