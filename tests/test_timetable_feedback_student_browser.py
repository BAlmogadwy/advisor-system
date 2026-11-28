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

from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
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

#: Whether an inline element sits on one line. In Arabic a meeting or a date is
#: several pieces (right-to-left words, left-to-right figures), each its own
#: rectangle on the same line. Overlap is no test: with a tight line height the
#: boxes of two lines overlap too. Pieces of one line share a centre, within half
#: a box; the next line's is a whole line away.
ONE_LINE = """(el) => {
  const rects = Array.from(el.getClientRects());
  const centres = rects.map((r) => (r.top + r.bottom) / 2);
  const height = Math.min(...rects.map((r) => r.height));
  return rects.length > 0 && Math.max(...centres) - Math.min(...centres) < height / 2;
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
        page.wait_for_url("**/student/timetable-feedback/new/?*about=*")
        requested = page.locator("fieldset[data-ttf-for]")
        expect(requested).to_be_hidden()
        page.get_by_label("The time does not suit me").check()
        expect(requested).to_be_visible()
        page.get_by_label("Section M2", exact=False).check()
        page.get_by_label("More details").fill("Sunday mornings clash with my work.")
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
        expect(page.get_by_role("radiogroup", name="ما موضوع ملاحظتك؟")).to_be_visible()
        self._check(page, "6-choose-ar")
        page.goto(f"{self.live_server_url}/student/")
        expect(page.get_by_role("link", name="إرسال ملاحظة إلى مرشدي الأكاديمي")).to_be_visible()
        self._check(page, "7-home-ar", "#student-home-timetable-expected")

    def test_continue_with_nothing_chosen_says_so_where_the_choice_is(self):
        """A radio group with nothing chosen sends nothing at all; the page must
        still say what is missing, in the summary and at the choice itself."""
        page = self._page("en")
        page.goto(f"{self.live_server_url}/student/timetable-feedback/new/")
        page.get_by_role("button", name="Continue").click()

        summary = page.locator("#ttfErrors")
        expect(summary).to_be_focused()
        expect(summary).to_contain_text("Please choose what your feedback is about.")
        group = page.get_by_role("radiogroup", name="What is your feedback about?")
        expect(group).to_have_attribute("aria-invalid", "true")
        expect(group.locator(".ttf-field-error")).to_have_text(
            "Please choose what your feedback is about."
        )
        self._check(page, "8-nothing-chosen-en")

    # -- PR 3b: what the student does on a case ---------------------------

    def _case(self, status: str, *, points: int = 1, **fields) -> TimetableFeedback:
        """A case as staff left it (PR 4 will put it there), with ``points`` live
        points about the student's own section."""
        case = TimetableFeedback.objects.create(
            **{"student_id": SID, "academic_year": "1448", "term": "1", "status": status, **fields}
        )
        for n in range(points):
            TimetableFeedbackPoint.objects.create(
                case=case,
                anchor_kind="SECTION",
                term_section_id=self.mine.pk,
                course_key="SA101",
                section_label="M1",
                anchor_key=f"SECTION|{self.mine.pk}",
                reason_code=("TIME_CLASH", "TIME_UNSUITABLE")[n % 2],
                seen_fingerprint=f"{n:064d}",
            )
        return case

    def test_a_student_answers_a_request_for_information_from_the_home_banner(self):
        case = self._case("NEEDS_INFORMATION", last_student_visible_at=timezone.now())
        TimetableFeedbackMessage.objects.create(
            case=case,
            audience="STUDENT_VISIBLE",
            author_kind="ADVISOR",
            body="Which section would suit you?",
        )
        page = self._page("en")
        page.goto(f"{self.live_server_url}/student/")
        banner = page.locator("#student-home-feedback-banner")
        expect(banner).to_contain_text("Your reply is needed")
        self._check(page, "9-banner-en", "#student-home-feedback-banner")

        banner.get_by_role("link").click()
        page.wait_for_url(f"**/student/timetable-feedback/{case.reference}/")
        page.get_by_label("Your reply").fill("Section M2 would suit me.")
        self._check(page, "10-reply-en")
        page.get_by_role("button", name="Send reply").click()

        page.wait_for_url(f"**/student/timetable-feedback/{case.reference}/")
        notice = page.locator("#ttfNotice")
        expect(notice).to_be_focused()
        expect(notice).to_contain_text("Your reply was sent.")
        case.refresh_from_db()
        self.assertEqual(case.status, "SUBMITTED")
        self.assertIsNotNone(case.student_seen_at)

    def test_withdrawing_a_point_asks_first_on_its_own_page(self):
        case = self._case("SUBMITTED", points=2)
        page = self._page("en")
        page.goto(f"{self.live_server_url}/student/timetable-feedback/{case.reference}/")
        # A text link, still a 44px target on a phone.
        box = page.locator(".ttf-point-action").first.bounding_box()
        # Chromium lays out in 1/64px units, so min-height: 44px can measure
        # 43.99997; allow that rounding, nothing a user could feel.
        self.assertGreaterEqual(box["height"], 44 - 1 / 64)
        page.locator(".ttf-point-action").first.click()
        page.wait_for_url("**/points/*/withdraw/")
        self._check(page, "11-confirm-point-en")
        page.get_by_role("button", name="Withdraw the point").click()

        page.wait_for_url(f"**/student/timetable-feedback/{case.reference}/")
        notice = page.locator("#ttfNotice")
        expect(notice).to_be_focused()
        expect(notice).to_contain_text("The point was withdrawn")
        self.assertEqual(case.points.filter(withdrawn_at__isnull=True).count(), 1)

    def test_an_empty_reply_in_arabic_is_said_where_it_was_typed(self):
        case = self._case("NEEDS_INFORMATION")
        page = self._page("ar")
        page.goto(f"{self.live_server_url}/student/timetable-feedback/{case.reference}/")
        self.assertEqual(page.evaluate("document.documentElement.dir"), "rtl")
        page.get_by_role("button", name="إرسال الرد").click()

        summary = page.locator("#ttfErrors")
        expect(summary).to_be_focused()
        expect(summary).to_contain_text("يُرجى كتابة رسالتك.")
        reply = page.locator("#ttfReply")
        expect(reply).to_have_attribute("aria-invalid", "true")
        self.assertFalse(TimetableFeedbackMessage.objects.exists())
        self._check(page, "12-reply-error-ar")

    def test_an_answered_case_in_arabic_fits_the_phone(self):
        case = self._case("ANSWERED", answered_at=timezone.now() - timedelta(days=2))
        page = self._page("ar")
        page.goto(f"{self.live_server_url}/student/timetable-feedback/{case.reference}/")
        expect(page.get_by_role("radiogroup", name="هل كان الرد مفيدًا؟ (اختياري)")).to_be_visible()
        expect(page.get_by_role("button", name="إغلاق هذه الملاحظة")).to_be_visible()
        expect(page.get_by_role("button", name="إعادة فتح هذه الملاحظة")).to_be_visible()
        self._check(page, "13-answered-ar")

    def test_a_date_in_a_sentence_never_breaks_at_its_hyphens(self):
        case = self._case("ANSWERED", answered_at=timezone.now() - timedelta(days=2))
        # English breaks "2026-12-08" after a hyphen where it can; Arabic text
        # is broken by other rules, so both are tried.
        for language in ("en", "ar"):
            page = self._page(language)
            page.goto(f"{self.live_server_url}/student/timetable-feedback/{case.reference}/")
            # "...can be reopened until <date>", however narrow its sentence.
            date = page.locator("#ttfAnswer .ttf-date")
            for rem in range(4, 13):
                date.evaluate(f"(date) => {{ date.parentElement.style.inlineSize = '{rem}rem'; }}")
                self.assertTrue(date.evaluate(ONE_LINE), (language, rem))

    def test_the_list_fits_a_small_phone_and_a_whole_row_opens_its_feedback(self):
        # The longest status there is, in English, beside last term's feedback.
        case = self._case("NEEDS_INFORMATION", points=2)
        self._case(
            "CLOSED",
            academic_year="1447",
            term="2",
            close_reason="ANSWER_ACCEPTED",
            closed_at=timezone.now(),
        )
        for language in ("en", "ar"):
            page = self._page(language)
            page.set_viewport_size({"width": 360, "height": 800})
            page.goto(f"{self.live_server_url}/student/timetable-feedback/")
            row = page.locator('.ttf-case-list .ttf-case[data-status="NEEDS_INFORMATION"]')
            # The status stays inside its row, on the reference's line or the next.
            inside = row.evaluate(
                "(row) => { const r = row.getBoundingClientRect();"
                " const s = row.querySelector('.ttf-status').getBoundingClientRect();"
                " return s.left >= r.left - 0.5 && s.right <= r.right + 0.5; }"
            )
            self.assertTrue(inside, language)
            # The one waiting on the student has its edge in the status's colour,
            # beside the row: every row's reference starts at the same place.
            # (A pseudo-element is drawn only when it has content.)
            edge = row.evaluate(
                "(row) => { const s = getComputedStyle(row, '::before');"
                " return [s.content, s.width, s.backgroundColor]; }"
            )
            self.assertNotIn(edge[0], ("none", "normal"))
            self.assertEqual(edge[1], "3px")
            self.assertNotEqual(edge[2], "rgba(0, 0, 0, 0)")
            starts = page.locator(".ttf-case-list .ttf-case-link").evaluate_all(
                "(links) => links.map((a) => { const r = a.getBoundingClientRect();"
                " return Math.round(document.dir === 'rtl' ? r.right : r.left); })"
            )
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(set(starts)), 1, (language, starts))
            # The keyboard's focus tints the row as the pointer does.
            row.locator(".ttf-case-link").focus()
            expect(row).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
            # The stretched link covers its own row and nothing else: the button
            # under the list is still the button.
            send = page.locator(".ttf-actions .btn-primary")
            self.assertTrue(
                send.evaluate(
                    "(button) => { const b = button.getBoundingClientRect();"
                    " return button.contains(document.elementFromPoint("
                    "b.left + b.width / 2, b.top + b.height / 2)); }"
                ),
                language,
            )
            self._check(page, f"14-list-{language}")
            # Anywhere on the row opens the feedback, not only its reference: a
            # tap on the line of dates lands on the link stretched over the row.
            box = row.locator(".ttf-case-meta").bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_url(f"**/student/timetable-feedback/{case.reference}/", timeout=5000)

    def test_a_meeting_never_breaks_across_lines(self):
        page = self._page("ar")
        page.set_viewport_size({"width": 320, "height": 800})
        page.goto(f"{self.live_server_url}/student/timetable-feedback/new/")
        self._check(page, "15-meetings-ar")
        # Section M1 meets on Sunday and on Tuesday: two meetings, each on one
        # line however narrow the line, so a break falls between them or nowhere.
        meta = page.locator(".ttf-choice-meta").first
        meetings = page.locator(".ttf-choice-meta .ttf-when")
        for rem in range(6, 17):
            meta.evaluate(f"(meta) => {{ meta.style.inlineSize = '{rem}rem'; }}")
            self.assertEqual(
                meetings.evaluate_all(f"(all) => all.map({ONE_LINE})"), [True, True], rem
            )
