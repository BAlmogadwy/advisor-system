"""The student's timetable-feedback pages in a real browser, on a phone.

From the home card through both steps of the form to the case, in English, and the
error path in Arabic: focus lands where the page says it does, the optional lists
follow the chosen issue, and nothing is wider than the screen.

Set TTF_SCREENSHOTS to a directory to keep a picture of every step.
"""

from __future__ import annotations

import os

# Playwright's sync API runs a greenlet loop that Django would otherwise take for
# an async context and refuse ORM calls in.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableFeedback,
)
from core.services import student_otp
from core.services.rbac import ensure_role_groups

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright
expect = playwright.expect

SID = 4995001
PROG = "FBB"
PLAN = "registration_plan_1448_t1"

#: The widest overflow inside the page or the document, in pixels. main clips its
#: overflow rather than scrolling, so the document alone would never show it.
OVERFLOW = """() => {
  const main = document.querySelector('main');
  const doc = document.documentElement;
  const cards = Array.from(document.querySelectorAll('main .card'));
  return Math.max(
    main.scrollWidth - main.clientWidth,
    doc.scrollWidth - doc.clientWidth,
    ...cards.map((card) => card.scrollWidth - card.clientWidth),
  );
}"""


def _section(code, label, meetings, name):
    section = TermSection.objects.create(
        source_tag="other",
        course_name=name,
        course_code=code[:2],
        course_number=code[2:],
        course_key=code,
        section=label,
    )
    for day, start, end, room in meetings:
        TermSectionMeeting.objects.create(
            term_section=section, day=day, start_time=start, end_time=end, room=room
        )
    TermSectionProgram.objects.create(term_section=section, program=PROG)
    return section


@override_settings(
    TIMETABLE_FEEDBACK_ENABLED=True,
    TIMETABLE_FEEDBACK_ACCEPTING=True,
    TIMETABLE_FEEDBACK_PROGRAMMES=(),
)
class StudentTimetableFeedbackBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._playwright = sync_playwright().start()
        cls.browser = cls._playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._playwright.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        # Both term names, for the whole visit: the live server runs these views.
        for patcher in (
            mock.patch(
                "core.services.timetable_feedback.planning_term", return_value=("1448", "1")
            ),
            mock.patch(
                "core.student_auth_views.load_defaults",
                return_value={
                    "academic_year": 1448,
                    "term": 1,
                    "currentYear": 1448,
                    "currentTerm": 1,
                },
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        ensure_role_groups()
        Student.objects.create(
            student_id=SID, name="Browser Student", program=PROG, section="M", status="منتظم"
        )
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code="SA101",
            course_name="INTRO TO PROGRAMMING",
            type="Mandatory",
            programme_term=1,
            credit_hours=3,
        )
        self.mine = _section(
            "SA101",
            "M1",
            [("SUN", "09:00", "10:15", "A1"), ("TUE", "09:00", "10:15", "A1")],
            "INTRO TO PROGRAMMING",
        )
        self.other = _section(
            "SA101", "M2", [("MON", "13:00", "14:15", "B1")], "INTRO TO PROGRAMMING"
        )
        StudentTermSection.objects.create(
            student_id=SID, academic_year="1448", term="1", term_section=self.mine, source=PLAN
        )
        self.shots = os.environ.get("TTF_SCREENSHOTS", "")

    def _page(self, language: str):
        client = Client()
        client.force_login(student_otp.provision_student_user(SID))
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": 375, "height": 812},
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
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return page

    def _check(self, page, name: str, part: str = "") -> None:
        self.assertLessEqual(page.evaluate(OVERFLOW), 1, name)
        if self.shots:
            # Pictures only: the portal fades its content in, which a picture
            # would otherwise catch half-way.
            page.add_style_tag(
                content="*, *::before, *::after { animation: none !important;"
                " transition: none !important; }"
            )
            Path(self.shots).mkdir(parents=True, exist_ok=True)
            target = page.locator(part) if part else page
            options = {} if part else {"full_page": True}
            target.screenshot(path=str(Path(self.shots) / f"{name}.png"), **options)

    def test_a_student_sends_a_point_from_the_home_card_on_a_phone(self):
        page = self._page("en")
        page.goto(f"{self.live_server_url}/student/")
        self._check(page, "1-home-en", "#student-home-timetable-expected")

        page.get_by_role("link", name="Send feedback to my academic advisor").click()
        page.wait_for_url("**/student/timetable-feedback/new/")
        self._check(page, "2-choose-en")

        page.get_by_label("SA101", exact=False).check()
        page.get_by_role("button", name="Continue").click()
        page.wait_for_url("**/student/timetable-feedback/new/?about=*")
        requested = page.locator("fieldset[data-ttf-for]")
        expect(requested).to_be_hidden()
        page.get_by_label("The time does not suit me").check()
        expect(requested).to_be_visible()
        page.get_by_label("Section M2", exact=False).check()
        page.get_by_label("Your note").fill("Sunday mornings clash with my work.")
        self._check(page, "3-describe-en")

        page.get_by_role("button", name="Send to my academic advisor").click()
        page.wait_for_url("**/student/timetable-feedback/TTF-*/")
        reference = TimetableFeedback.objects.get(student_id=SID).reference
        notice = page.locator("#ttfNotice")
        expect(notice).to_be_focused()
        expect(notice).to_contain_text(reference)
        expect(page.locator(".ttf-point")).to_contain_text("09:00–10:15")
        self._check(page, "4-case-en")

        # The sidebar names the list as the current page on every feedback page.
        link = page.locator('.sidebar a[href="/student/timetable-feedback/"]')
        expect(link).to_have_attribute("aria-current", "page")

    def test_an_arabic_student_who_skips_the_issue_is_taken_to_the_error(self):
        page = self._page("ar")
        page.goto(
            f"{self.live_server_url}/student/timetable-feedback/new/?about=SECTION|{self.mine.pk}"
        )
        self.assertEqual(page.evaluate("document.documentElement.lang"), "ar")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        page.get_by_role("button", name="إرسال إلى مرشدي الأكاديمي").click()

        summary = page.locator("#ttfErrors")
        expect(summary).to_be_focused()
        expect(summary).to_contain_text("يُرجى اختيار المشكلة.")
        expect(page.locator("fieldset[aria-invalid='true']")).to_have_count(1)
        self.assertFalse(TimetableFeedback.objects.exists())
        self._check(page, "5-error-ar")

    def test_the_choices_read_right_to_left_in_arabic_and_fit_the_phone(self):
        page = self._page("ar")
        page.goto(f"{self.live_server_url}/student/timetable-feedback/new/")
        expect(page.get_by_role("group", name="ما موضوع ملاحظتك؟")).to_be_visible()
        self._check(page, "6-choose-ar")
        page.goto(f"{self.live_server_url}/student/")
        expect(page.get_by_role("link", name="إرسال ملاحظة إلى مرشدي الأكاديمي")).to_be_visible()
        self._check(page, "7-home-ar", "#student-home-timetable-expected")
