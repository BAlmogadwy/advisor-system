"""Timetable feedback end to end in real browsers: a student sends a point, their
advisor asks a question, the student replies, the advisor answers, and the student
accepts the answer. The student is on a phone and the advisor at a desk, each in
their own browser, each seeing the other's work arrive as something new.

Then the staff pages on a phone, in Arabic: right to left, nothing wider than the
screen, and focus taken to what the page says went wrong.

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
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings
from django.utils import timezone

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
    TimetableFeedback,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
)
from core.services import student_otp
from core.services.rbac import ROLE_ADVISOR, ensure_role_groups, set_user_scope

playwright = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright.sync_playwright
expect = playwright.expect

SID = 4997001
PROG = "FBE"
ADV = "ADV-E2E"
PLAN = "registration_plan_1448_t1"
PHONE = {"width": 375, "height": 812}
DESK = {"width": 1280, "height": 900}

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
class StaffTimetableFeedbackBrowserTests(StaticLiveServerTestCase):
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
            student_id=SID,
            name="Browser Student",
            program=PROG,
            section="M",
            status="ACTIVE",
            advisor_id=ADV,
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
            "SA101", "M1", [("SUN", "09:00", "10:15", "A1")], "INTRO TO PROGRAMMING"
        )
        _section("SA101", "M2", [("MON", "13:00", "14:15", "B1")], "INTRO TO PROGRAMMING")
        StudentTermSection.objects.create(
            student_id=SID, academic_year="1448", term="1", term_section=self.mine, source=PLAN
        )
        self.advisor = User.objects.create_user("e2e-advisor", first_name="Ada", last_name="Lovel")
        self.advisor.groups.add(Group.objects.get(name=ROLE_ADVISOR))
        set_user_scope(self.advisor.pk, advisor_id=ADV)
        self.student = student_otp.provision_student_user(SID)
        self.shots = os.environ.get("TTF_SCREENSHOTS", "")

    def _page(self, user, language: str, viewport: dict):
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport=viewport,
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

    def _check(self, page, name: str) -> None:
        self.assertLessEqual(page.evaluate(OVERFLOW), 1, name)
        if self.shots:
            page.add_style_tag(
                content="*, *::before, *::after { animation: none !important;"
                " transition: none !important; }"
            )
            Path(self.shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(self.shots) / f"staff-{name}.png"), full_page=True)

    def test_a_question_and_an_answer_go_round_between_a_student_and_their_advisor(self):
        student = self._page(self.student, "en", PHONE)
        advisor = self._page(self.advisor, "en", DESK)
        base = self.live_server_url

        # The student sends a point from the expected card on the home page.
        student.goto(f"{base}/student/")
        student.get_by_role("link", name="Send feedback to my academic advisor").click()
        student.get_by_label("SA101", exact=False).check()
        student.get_by_role("button", name="Continue").click()
        student.get_by_label("The time does not suit me").check()
        student.get_by_label("More details").fill("Sunday mornings clash with my work.")
        student.get_by_role("button", name="Send to my academic advisor").click()
        student.wait_for_url("**/student/timetable-feedback/TTF-*/")
        case = TimetableFeedback.objects.get(student_id=SID)

        # The advisor finds it waiting on them, counted in the sidebar.
        advisor.goto(f"{base}/ops/timetable-feedback/")
        link = advisor.locator('.sidebar a[href="/ops/timetable-feedback/"]')
        expect(link.locator(".nav-badge")).to_have_text("1")
        expect(link).to_have_attribute("aria-current", "page")
        advisor.get_by_role("link", name=case.reference).click()
        advisor.wait_for_url(f"**/ops/timetable-feedback/{case.reference}/")
        expect(advisor.locator(".ttf-point")).to_contain_text("Sunday mornings clash with my work.")
        self._check(advisor, "1-case")
        # The composer opens on the answer; the question is its own tab.
        advisor.get_by_role("tab", name="Ask the student").click()
        advisor.get_by_label("Your question to the student").fill("Would section M2 suit you?")
        advisor.get_by_role("button", name="Send the question").click()
        notice = advisor.locator("#ttfsNotice")
        expect(notice).to_be_focused()
        expect(notice).to_contain_text("The question was sent to the student.")

        # The student is told on the home page, and replies.
        student.goto(f"{base}/student/")
        banner = student.locator("#student-home-feedback-banner")
        expect(banner).to_contain_text("Your reply is needed")
        banner.get_by_role("link").click()
        student.wait_for_url(f"**/student/timetable-feedback/{case.reference}/")
        expect(student.locator(".ttf-thread")).to_contain_text("Would section M2 suit you?")
        expect(student.locator(".ttf-thread")).to_contain_text("Your academic advisor")
        student.get_by_label("Your reply").fill("Yes, M2 suits me.")
        student.get_by_role("button", name="Send reply").click()
        expect(student.locator("#ttfNotice")).to_contain_text("Your reply was sent.")

        # The advisor sees the student wrote, and answers.
        advisor.goto(f"{base}/ops/timetable-feedback/")
        row = advisor.locator("#ttfsQueue tbody tr", has_text=case.reference)
        expect(row).to_contain_text("The student wrote")
        row.get_by_role("link", name=case.reference).click()
        expect(advisor.locator(".ttf-thread")).to_contain_text("Yes, M2 suits me.")
        advisor.get_by_label("Your answer to the student").fill("M2 is noted for the department.")
        advisor.get_by_role("button", name="Send the answer").click()
        expect(advisor.locator("#ttfsNotice")).to_contain_text(
            "The answer was sent to the student."
        )
        self._check(advisor, "2-answered")

        # The student sees the answer as new, and accepts it.
        student.goto(f"{base}/student/")
        expect(student.locator("#student-home-feedback-banner")).to_contain_text(
            "Your timetable feedback has an answer"
        )
        badge = student.locator('.sidebar a[href="/student/timetable-feedback/"] .nav-badge')
        expect(badge).to_have_text("1")
        student.locator("#student-home-feedback-banner").get_by_role("link").click()
        student.get_by_role("radiogroup").get_by_label("Yes").check()
        student.get_by_role("button", name="Close this feedback").click()
        expect(student.locator("#ttfNotice")).to_contain_text(
            "The answer was accepted, and this feedback is closed."
        )
        self._check(student, "3-student-closed")

        case.refresh_from_db()
        self.assertEqual(
            (case.status, case.close_reason, case.answer_helpful),
            ("CLOSED", "ANSWER_ACCEPTED", True),
        )
        # Whoever answered, the student never read a name.
        usernames = [
            name
            for name in TimetableFeedbackMessage.objects.filter(case=case).values_list(
                "author_username", flat=True
            )
            if name
        ]
        self.assertEqual(usernames, ["e2e-advisor", "e2e-advisor"])
        for shown in (*usernames, "Ada Lovel"):
            self.assertNotIn(shown, student.content())

    def _waiting_case(self, **fields):
        case = TimetableFeedback.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            status="SUBMITTED",
            submitted_language="en",
            last_student_activity_at=timezone.now(),
            **fields,
        )
        TimetableFeedbackPoint.objects.create(
            case=case,
            anchor_kind="SECTION",
            term_section_id=self.mine.pk,
            course_key="SA101",
            section_label="M1",
            anchor_key=f"SECTION|{self.mine.pk}",
            reason_code="TIME_UNSUITABLE",
            seen_fingerprint="0" * 64,
        )
        return case

    def test_the_composer_is_tabs_that_open_where_the_page_needs_the_person(self):
        case = self._waiting_case()
        page = self._page(self.advisor, "en", DESK)
        page.goto(f"{self.live_server_url}/ops/timetable-feedback/{case.reference}/")

        tabs = page.get_by_role("tablist").get_by_role("tab")
        answer = page.get_by_role("tab", name="Answer the student")
        ask = page.get_by_role("tab", name="Ask the student")
        self.assertGreaterEqual(tabs.count(), 2)
        # The answer is open; the other forms wait behind their tabs.
        expect(answer).to_have_attribute("aria-selected", "true")
        expect(page.locator("#ttfsAnswerForm")).to_be_visible()
        expect(page.locator("#ttfsAskForm")).to_be_hidden()
        # Arrows move between tabs, and the tab moved to opens its form.
        answer.focus()
        page.keyboard.press("ArrowRight")
        expect(ask).to_be_focused()
        expect(ask).to_have_attribute("aria-selected", "true")
        expect(page.locator("#ttfsAskForm")).to_be_visible()
        expect(page.locator("#ttfsAnswerForm")).to_be_hidden()

        # An empty question is refused: the page comes back open on the question,
        # its field marked, and the summary takes focus.
        page.get_by_role("button", name="Send the question").click()
        expect(page.locator("#ttfsErrors")).to_be_focused()
        expect(page.get_by_role("tab", name="Ask the student")).to_have_attribute(
            "aria-selected", "true"
        )
        expect(page.locator("#ttfsAsk")).to_have_attribute("aria-invalid", "true")
        self._check(page, "7-composer")

    def test_the_bulk_actions_wait_for_a_ticked_case_and_count_them(self):
        case = self._waiting_case()
        page = self._page(self.advisor, "en", DESK)
        page.goto(f"{self.live_server_url}/ops/timetable-feedback/?tab=all")
        bulk = page.locator("#ttfsBulk")
        expect(bulk).to_be_hidden()
        page.get_by_label(f"Select {case.reference}").check()
        expect(bulk).to_be_visible()
        expect(page.locator("#ttfsBulkCount")).to_contain_text("1")
        # At a desk it stays in view at the foot of the page.
        self.assertEqual(bulk.evaluate("el => getComputedStyle(el).position"), "sticky")
        page.get_by_label(f"Select {case.reference}").uncheck()
        expect(bulk).to_be_hidden()

    def test_on_a_phone_the_bulk_actions_follow_the_list_and_tabs_read_right_to_left(self):
        case = self._waiting_case()
        page = self._page(self.advisor, "ar", PHONE)
        page.goto(f"{self.live_server_url}/ops/timetable-feedback/?tab=all")
        page.locator(f'input[name="pick"][value^="{case.reference}@"]').check()
        bulk = page.locator("#ttfsBulk")
        expect(bulk).to_be_visible()
        # Not kept in view: on a phone it would cover the rows being ticked.
        self.assertNotEqual(bulk.evaluate("el => getComputedStyle(el).position"), "sticky")
        self._check(page, "8-bulk-phone-ar")

        page.goto(f"{self.live_server_url}/ops/timetable-feedback/{case.reference}/")
        tabs = page.get_by_role("tablist").get_by_role("tab")
        first, second = tabs.nth(0), tabs.nth(1)
        first.focus()
        # Right to left: the next tab is to the left.
        page.keyboard.press("ArrowLeft")
        expect(second).to_be_focused()
        expect(second).to_have_attribute("aria-selected", "true")
        page.keyboard.press("ArrowRight")
        expect(first).to_be_focused()

    def test_the_staff_pages_fit_a_phone_and_read_right_to_left_in_arabic(self):
        case = TimetableFeedback.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            status="SUBMITTED",
            submitted_language="ar",
            last_student_activity_at=timezone.now(),
        )
        TimetableFeedbackPoint.objects.create(
            case=case,
            anchor_kind="SECTION",
            term_section_id=self.mine.pk,
            course_key="SA101",
            section_label="M1",
            anchor_key=f"SECTION|{self.mine.pk}",
            reason_code="TIME_UNSUITABLE",
            note="الوقت يتعارض مع عملي صباح الأحد.",
            seen_fingerprint="0" * 64,
        )
        page = self._page(self.advisor, "ar", PHONE)
        base = self.live_server_url

        page.goto(f"{base}/ops/timetable-feedback/")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        expect(page.get_by_role("link", name=case.reference)).to_be_visible()
        self._check(page, "4-queue-ar")

        page.goto(f"{base}/ops/timetable-feedback/{case.reference}/")
        expect(page.locator("#ttfsActions")).to_contain_text("كتب الطالب باللغة العربية")
        self._check(page, "5-case-ar")
        # An empty answer: the page says so, and takes focus to it.
        page.get_by_role("button", name="إرسال الرد").click()
        summary = page.locator("#ttfsErrors")
        expect(summary).to_be_focused()
        expect(summary).to_contain_text("يُرجى كتابة النص.")
        expect(page.locator("#ttfsAnswer")).to_have_attribute("aria-invalid", "true")
        self._check(page, "6-error-ar")
        case.refresh_from_db()
        self.assertEqual(case.status, "SUBMITTED")
