"""Student lists in a real browser, against the real endpoints.

The jsdom suites (tests/frontend/exam-rosters.test.cjs and
exam-roster-drawer.test.cjs) cover every state with recorded answers. This
runs the whole path in Chromium on a run made by the real build: a room's
list, a student found by ID and never put in the address, the course drawer
from a card with focus kept as a browser keeps it, the Export dialog preset
to the room on screen and the file it downloads, and the Arabic page and
drawer at phone width. Nothing leaves the machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import os
from io import BytesIO

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from openpyxl import load_workbook

from core.models import AuditLog
from core.services import exam_roster_view as rv
from core.services.exam_rosters import build_roster_model
from core.services.exam_student_export import all_sittings
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.exam_student_export_fixture import build_population, build_saved_run

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

LOOKUP_ID = 4402003
# The drawer has finished sliding in (its 180 ms transition).
SETTLED = "() => document.getElementById('examRosterDrawer').getAnimations().length === 0"


class ExamRostersBrowserTests(StaticLiveServerTestCase):
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
        rv.ROSTER_VIEW_CACHE.clear()
        self.addCleanup(rv.ROSTER_VIEW_CACHE.clear)
        build_population()
        self.run = build_saved_run()
        self.sittings = all_sittings(build_roster_model(self.run))

    def _page(self, language: str, path: str, viewport: dict[str, int] | None = None):
        ensure_role_groups()
        user = get_user_model().objects.create_user(
            username=f"lists-browser-{language}-{get_user_model().objects.count()}",
            password="unused",
        )
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport=viewport or {"width": 1280, "height": 900},
            accept_downloads=True,
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
        page.goto(f"{self.live_server_url}{path}")
        return page

    def _rosters(self, language: str = "en", viewport=None):
        page = self._page(language, f"/exam-timetable/rosters/?run={self.run.pk}", viewport)
        expect(page.locator("#examRostersRooms .et-nav-item").first).to_be_visible()
        return page

    def _room_rows(self, room: str) -> list[int]:
        return [
            s.student_id
            for s in self.sittings
            if s.part is not None and s.part.room_code == room and s.exam.slot_index == 0
        ]

    def test_a_room_list_and_a_student_found_by_id_never_enter_the_address(self) -> None:
        page = self._rosters()
        page.locator('#examRostersRooms .et-nav-item[data-room="M-B"]').click()
        expected = self._room_rows("M-B")
        rows = page.locator("#examRostersRoster tr.et-roster-row")
        expect(rows).to_have_count(len(expected))
        expect(page.locator("#examRostersPaneTitle")).to_have_text("Room M-B")
        # Focus stays on the room in the navigator, as a keyboard user left it.
        expect(page.locator('#examRostersRooms .et-nav-item[data-room="M-B"]')).to_be_focused()
        ids = [int(text) for text in rows.locator(".et-col-id").all_inner_texts()]
        self.assertEqual(ids, expected)
        self.assertIn("room=M-B", page.url)
        # The column header stays in view while the page scrolls the list.
        position = page.evaluate(
            "getComputedStyle(document.querySelector('#examRostersRoster thead th')).position"
        )
        self.assertEqual(position, "sticky")

        find = page.locator("#examRostersFind")
        find.click()
        find.fill(str(LOOKUP_ID))
        option = page.locator("#examRostersFindList [role=option]").first
        expect(option).to_contain_text(str(LOOKUP_ID))
        find.press("Enter")
        expect(page.locator("#examRostersPaneTitle")).to_have_text(f"Student {LOOKUP_ID}")
        expect(page.locator("#examRostersPaneTitle")).to_be_focused()
        exams = sorted(s.exam.code for s in self.sittings if s.student_id == LOOKUP_ID)
        codes = page.locator("#examRostersLookup tbody tr .et-col-name > bdi").all_inner_texts()
        self.assertEqual(sorted(codes), exams)
        self.assertNotIn(str(LOOKUP_ID), page.url)
        self.assertNotIn(str(LOOKUP_ID), page.evaluate("JSON.stringify(history.state)"))
        page.keyboard.press("Escape")
        expect(page.locator("#examRostersPaneTitle")).to_have_text("Room M-B")
        # One audit row for the room; one per settled search and the lookup.
        actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
        self.assertEqual(actions.count("exam_timetable.roster_view"), 1)
        self.assertGreaterEqual(actions.count("exam_timetable.roster_lookup"), 2)

    def test_export_opens_preset_to_the_room_and_downloads_its_list(self) -> None:
        page = self._rosters()
        page.locator('#examRostersRooms .et-nav-item[data-room="F-A"]').click()
        expect(page.locator("#examRostersRoster tr.et-roster-row")).to_have_count(
            len(self._room_rows("F-A"))
        )
        page.locator("#examRostersExport").click()
        expect(page.locator("#examStudentExportTitle")).to_be_focused()
        expect(page.locator('input[name="examStudentScope"][value="room"]')).to_be_checked()
        expect(page.locator("#examStudentRoom")).to_have_value("F-A")
        expect(page.locator("#examStudentExportCheck")).to_have_attribute("data-state", "matches")
        page.locator(".et-segmented label", has_text="English").click()
        page.locator("#examStudentOneFile").uncheck()
        expect(page.locator("#examStudentExportDownload")).to_have_attribute(
            "aria-disabled", "false"
        )
        with page.expect_download(timeout=30_000) as download_info:
            page.locator("#examStudentExportDownload").click()
        workbook = load_workbook(BytesIO(download_info.value.path().read_bytes()))
        sheet = workbook["Student exams"]
        header = [cell.value for cell in sheet[1]]
        ids = [
            int(row[header.index("Student ID")])
            for row in sheet.iter_rows(min_row=2, values_only=True)
        ]
        self.assertEqual(sorted(ids), sorted(self._room_rows("F-A")))
        page.keyboard.press("Escape")
        expect(page.locator("#examRostersExport")).to_be_focused()

    def test_the_card_link_opens_the_course_drawer_and_focus_returns_to_it(self) -> None:
        page = self._page("en", "/exam-timetable/")
        page.locator("#examHistorySummary").click()
        page.locator("#historyList .et-run-info").first.click()
        link = page.locator('#schedGrid .et-course[data-course="MATH101"] .et-roster-link')
        math = sum(1 for s in self.sittings if s.exam.code == "MATH101")
        expect(link).to_have_text(f"{math} students")
        link.click()
        drawer = page.locator("#examRosterDrawer")
        expect(drawer).to_be_visible()
        expect(page.locator("#examRosterDrawerTitle")).to_be_focused()
        expect(page.locator("#examRosterDrawerBody tr.et-roster-row")).to_have_count(math)
        # On the inline end once it has slid in: the right edge in English,
        # 800px wide at most, the full height.
        page.wait_for_function(SETTLED)
        box = drawer.bounding_box()
        self.assertEqual(round(box["x"] + box["width"]), 1280)
        self.assertEqual(round(box["width"]), 800)
        self.assertEqual(round(box["height"]), 900)
        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(link).to_be_focused()

    def test_arabic_page_and_drawer_fit_a_phone(self) -> None:
        phone = {"width": 375, "height": 812}
        page = self._rosters("ar", viewport=phone)
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        self.assertEqual(
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True
        )
        expect(page.locator("#examRostersPane")).to_be_hidden()
        page.locator('#examRostersRooms .et-nav-item[data-room="M-B"]').click()
        expect(page.locator("#examRostersNav")).to_be_hidden()
        expect(page.locator("#examRostersPaneTitle")).to_have_text("القاعة M-B")
        expect(page.locator("#examRostersPaneTitle")).to_be_focused()
        # The narrow pane shows list rows, not a table header row.
        header = page.evaluate(
            "document.querySelector('#examRostersRoster thead').getBoundingClientRect().height"
        )
        self.assertLessEqual(header, 1)
        overflow = page.evaluate(
            """() => [...document.querySelectorAll('main *')]
                .filter(node => node.getBoundingClientRect().width > 0)
                .filter(node => { const r = node.getBoundingClientRect(); return r.left < -1 || r.right > 376; })
                .map(node => node.id || node.className || node.tagName)"""
        )
        self.assertEqual(overflow, [])
        page.locator("#examRostersScreenBack").click()
        expect(page.locator("#examRostersNav")).to_be_visible()

        timetable = self._page("ar", "/exam-timetable/", viewport=phone)
        timetable.locator("#examHistorySummary").click()
        timetable.locator("#historyList .et-run-info").first.click()
        link = timetable.locator('#schedGrid .et-course[data-course="MATH101"] .et-roster-link')
        link.scroll_into_view_if_needed()
        link.click()
        drawer = timetable.locator("#examRosterDrawer")
        expect(timetable.locator("#examRosterDrawerTitle")).to_be_focused()
        timetable.wait_for_function(SETTLED)
        box = drawer.bounding_box()
        self.assertEqual((round(box["x"]), round(box["width"])), (0, 375))
