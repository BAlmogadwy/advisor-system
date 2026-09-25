"""The student data export dialog in a real browser, against the real endpoints.

The jsdom suite (tests/frontend/exam-student-export.test.cjs) covers every
state with fake answers. This runs the whole path once per language on a run
made by the real build: the saved-run pickers, the real preflight, a real
download read back with openpyxl, focus in Chromium, and the Arabic dialog at
phone width. Nothing leaves the machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import os
import re
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
from core.services.exam_rosters import build_roster_model
from core.services.exam_student_export import all_sittings
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.exam_student_export_fixture import build_population, build_saved_run

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright


class ExamStudentExportBrowserTests(StaticLiveServerTestCase):
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
        build_population()
        self.run = build_saved_run()
        self.sittings = all_sittings(build_roster_model(self.run))

    def _page(self, language: str, viewport: dict[str, int] | None = None):
        ensure_role_groups()
        user = get_user_model().objects.create_user(
            username=f"exam-browser-{language}", password="unused"
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
        page.goto(f"{self.live_server_url}/exam-timetable/")
        page.locator("#examHistorySummary").click()
        page.locator("#historyList .et-run-info").first.click()
        expect(page.locator("#examStudentDataBtn")).to_have_attribute("aria-disabled", "false")
        return page

    def _open(self, page):
        page.locator("#examStudentDataBtn").click()
        expect(page.locator("#examStudentExportDialog")).to_be_visible()
        expect(page.locator("#examStudentExportCheck")).to_have_attribute("data-state", "matches")
        expect(page.locator("#examStudentExportDownload")).to_have_attribute(
            "aria-disabled", "false"
        )

    def test_english_course_export_downloads_the_workbook_the_dialog_described(self) -> None:
        page = self._page("en")
        page.locator("#examStudentDataBtn").click()
        # Focus is on the heading, as Chromium keeps it.
        expect(page.locator("#examStudentExportTitle")).to_be_focused()
        expect(page.locator("#examStudentExportCheck")).to_have_attribute("data-state", "matches")
        total = len(self.sittings)
        expect(page.locator('[data-scope-count="all"]')).to_have_text(f"{total} rows")

        math = sum(1 for sitting in self.sittings if sitting.exam.code == "MATH101")
        page.locator("#examStudentCourse").select_option("MATH101")
        page.locator(".et-segmented label", has_text="English").click()
        page.locator("#examStudentOneFile").uncheck()
        expect(page.locator('input[name="examStudentScope"][value="course"]')).to_be_checked()
        expect(page.locator('[data-scope-count="course"]')).to_have_text(f"{math} rows")
        expect(page.locator("#examStudentExportSummary")).to_have_text(
            f"1 file · {math} rows · downloads now"
        )
        expect(page.locator("#examStudentExportDownload")).to_have_attribute(
            "aria-disabled", "false"
        )

        with page.expect_download(timeout=30_000) as download_info:
            page.locator("#examStudentExportDownload").click()
        download = download_info.value
        audit = AuditLog.objects.get(action="exam_timetable.export_students")
        reference = "EXR-" + audit.entry_hash[:8].upper()
        name = f"exam_students_MATH101_r{self.run.pk}_en_{reference[4:]}.xlsx"
        self.assertEqual(download.suggested_filename, name)
        expect(page.locator("#examStudentExportStatus")).to_have_text(
            f"Downloaded {name} · Ref {reference}"
        )

        workbook = load_workbook(BytesIO(download.path().read_bytes()), read_only=False)
        sheet = workbook["Student exams"]
        header = [cell.value for cell in sheet[1]]
        exams = [row[header.index("Exam")] for row in sheet.iter_rows(min_row=2, values_only=True)]
        self.assertEqual(exams, ["MATH101"] * math)
        self.assertEqual(header[:5], ["Student ID", "Name", "Program", "Department", "Group"])

        # Enter in a field submits; the options lock while the file is made,
        # and Chromium would drop focus to the page with them.
        page.locator("#examStudentPreparedFor").fill("Dean")
        with page.expect_download(timeout=30_000):
            page.locator("#examStudentPreparedFor").press("Enter")
        expect(page.locator("#examStudentExportDownload")).to_be_focused()

        page.keyboard.press("Escape")
        expect(page.locator("#examStudentExportDialog")).to_be_hidden()
        expect(page.locator("#examStudentDataBtn")).to_be_focused()

        # Reopened while the check is on its way, Download shows it waits from
        # its first frame, never the last visit's ready colour fading out.
        held = []
        page.route("**/students/export/preflight/", lambda route: held.append(route))
        page.locator("#examStudentDataBtn").click()
        state = page.evaluate(
            """() => { const button = document.getElementById('examStudentExportDownload');
                return [button.getAttribute('aria-disabled'), getComputedStyle(button).backgroundColor]; }"""
        )
        self.assertEqual(state, ["true", "rgb(248, 250, 252)"])
        for route in held:
            route.abort()

    def test_arabic_dialog_is_right_to_left_and_fits_a_phone(self) -> None:
        page = self._page("ar", viewport={"width": 375, "height": 812})
        self._open(page)
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        expect(page.locator("#examStudentExportTitle")).to_have_text(
            "تصدير بيانات الطلاب إلى Excel"
        )
        expect(page.locator("#examStudentExportCheckTitle")).to_have_text(
            "قوائم الطلاب مطابقة لهذا الجدول"
        )
        expect(page.locator('[data-scope-count="all"]')).to_have_text(
            f"الأسطر: {len(self.sittings)}"
        )
        # Full screen at phone width, with nothing wider than the screen: the
        # sheet's own padding and height, not the older .et-move-dialog ones.
        box = page.locator("#examStudentExportDialog").bounding_box()
        self.assertEqual((round(box["x"]), round(box["width"])), (0, 375))
        self.assertEqual((round(box["y"]), round(box["height"])), (0, 812))
        self.assertEqual(
            page.evaluate(
                "getComputedStyle(document.getElementById('examStudentExportDialog')).padding"
            ),
            "0px",
        )
        overflow = page.evaluate(
            """() => [...document.querySelectorAll('#examStudentExportDialog *')]
                .filter(node => node.getBoundingClientRect().width > 0)
                .filter(node => { const r = node.getBoundingClientRect(); return r.left < -1 || r.right > 376; })
                .map(node => node.id || node.className || node.tagName)"""
        )
        self.assertEqual(overflow, [])
        # Codes read left to right inside the Arabic sentence.
        code = page.locator("#examStudentExportCheckDetail bdi").last
        self.assertEqual(code.get_attribute("dir"), "ltr")
        self.assertRegex(code.inner_text(), r"^L-[0-9A-F]{10}$")
        # A touch-sized close button and radio rows are at least 24px tall.
        heights = page.evaluate(
            """() => [...document.querySelectorAll('#examStudentExportDialog .et-export-choice, #examStudentExportClose')]
                .map(node => node.getBoundingClientRect().height)"""
        )
        self.assertTrue(heights and min(heights) >= 24, heights)
        # Counts and timetable facts only: no student ID is ever on screen.
        self.assertIsNone(
            re.search(r"\d{7}", page.locator("#examStudentExportDialog").inner_text())
        )
        page.locator("#examStudentExportCancel").click()
        expect(page.locator("#examStudentDataBtn")).to_be_focused()
