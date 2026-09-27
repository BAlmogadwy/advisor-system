"""Linked exams on the exam page in a real browser, against the real endpoints.

The jsdom suite (tests/frontend/exam-linked.test.cjs) covers every state with
fake answers. This loads a run the real build made with a link (MATH101 and
IS201, pinned together; the IS students sit both, so the warnings show), and
lays out what linking adds - the builder section with its warnings, the grouped
cards on the board and the alignment dialog - at 1366px and at phone width
(375px), in English and in Arabic, in the design's font and in a wide one
(Verdana, DejaVu Sans), so a layout that only fits the fonts of the machine
running it fails anywhere. Linking two courses that sit apart goes through the
dialog to the real Check. The cards' "with X" labels are measured against
every card colouring, light and dark, for WCAG AA contrast. Nothing leaves the
machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import os
import re

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from core.models import ExamTimetableRun, ProgrammeRequirement
from core.services.exam_timetable import build_enrolled_sets_with_meta, build_exam_timetable
from core.services.rbac import ROLE_EXAM_COMMITTEE, ensure_role_groups
from tests.exam_student_export_fixture import DAYS, PERIODS, PINS, build_population
from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

LABEL = "Linked exams fixture"
# Everything under a container that pokes out of its content box, everything
# in a table cell that spills out of its cell, every code in a left-to-right
# isolate broken over two lines, and how far the page scrolls sideways.
# Hidden-for-screen-readers boxes (1px) are not layout.
FITS = """(id) => {
  const element = document.getElementById(id);
  const box = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
  const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const outside = [...element.querySelectorAll('*')].filter(shown)
    .filter(node => { const r = node.getBoundingClientRect(); return r.left < left - 1 || r.right > right + 1; })
    .map(node => node.id || node.className || node.tagName);
  const broken = [...element.querySelectorAll('bdi[dir=ltr]')].filter(shown)
    .filter(node => { const range = document.createRange(); range.selectNodeContents(node); return range.getClientRects().length > 1; })
    .map(node => node.textContent);
  const spill = [...element.querySelectorAll(':is(td, th) *')].filter(shown).filter(node => {
    const cell = node.parentElement.closest('td, th').getBoundingClientRect();
    const r = node.getBoundingClientRect();
    return r.left < cell.left - 1 || r.right > cell.right + 1;
  }).map(node => `${node.className || node.tagName} ${node.textContent.trim().slice(0, 24)}`);
  return {
    outside, broken, spill,
    page: document.documentElement.scrollWidth - window.innerWidth,
    left: box.left, right: box.right, viewport: window.innerWidth,
  };
}"""
# Each linked group inside its timetable cell, each card inside its group,
# each "with X" label inside its card, and no code broken over two lines.
GROUPS = """() => {
  const inside = (inner, outer) => {
    const a = inner.getBoundingClientRect(), b = outer.getBoundingClientRect();
    return a.left >= b.left - 1 && a.right <= b.right + 1;
  };
  return [...document.querySelectorAll('#schedGrid .et-link-group')].map(group => ({
    cards: [...group.querySelectorAll('.et-course')].map(card => card.dataset.course),
    inCell: inside(group, group.closest('td')),
    cardsInGroup: [...group.querySelectorAll('.et-course')].every(card => inside(card, group)),
    labelsInCards: [...group.querySelectorAll('.et-link-label')].every(label => inside(label, label.closest('.et-course'))),
    labels: [...group.querySelectorAll('.et-link-label')].map(label => label.textContent.trim()),
    broken: [...group.querySelectorAll('bdi[dir=ltr]')].filter(node => {
      const range = document.createRange(); range.selectNodeContents(node); return range.getClientRects().length > 1;
    }).map(node => node.textContent),
  }));
}"""
# Each "with X" label on a card in view against the card it sits on, as the
# WCAG contrast ratio; None where the backdrop is translucent (not measured).
CONTRAST = """() => {
  const rgb = value => (value.match(/[\\d.]+/g) || []).map(Number);
  const channel = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  const backdrop = node => {
    for (let at = node; at; at = at.parentElement) {
      const c = rgb(getComputedStyle(at).backgroundColor);
      if (c.length === 3 || c[3] === 1) return c;
      if (c[3] > 0) return null;
    }
    return [255, 255, 255];
  };
  return [...document.querySelectorAll('#schedGrid .et-course:not(.et-review-muted) .et-link-label')].map(label => {
    const back = backdrop(label);
    const a = luminance(rgb(getComputedStyle(label).color)), b = back && luminance(back);
    return [label.closest('.et-course').dataset.course, back ? (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) : null];
  });
}"""


class ExamLinkedBrowserTests(StaticLiveServerTestCase):
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
        # A plan name as long as real ones get, so every place that shows a
        # name - the chosen-course chips, the link table, the dialog - has to fit it.
        ProgrammeRequirement.objects.filter(program="IS", course_code="PHYS103").update(
            course_name="PHYSICS FOR BUSINESS AND MANAGEMENT INFORMATION SYSTEMS STUDENTS"
        )
        _, meta = build_enrolled_sets_with_meta()
        identity = {code: row["course_identity"] for code, row in meta.items()}
        result = build_exam_timetable(
            label=LABEL,
            days=DAYS,
            periods=PERIODS,
            pinned=PINS,
            seed=7,
            assign_rooms=True,
            rebalance_invigilators=False,
            linked_exams=[
                {
                    "members": [
                        {"course_identity": identity["MATH101"]},
                        {"course_identity": identity["IS201"]},
                    ]
                }
            ],
        )
        assert result.get("status") == "ok", result
        self.run = ExamTimetableRun.objects.get(pk=result["run_id"])
        # The IS students sit both linked courses: the warning the page shows.
        self.clash = result["qa"]["linked_exams"]["students_in_two_linked_courses"]
        assert self.clash > 0

    def _page(self, language: str, viewport: dict[str, int], wide_font: bool = False):
        ensure_role_groups()
        user = get_user_model().objects.create_user(
            username=f"linked-{language}-{get_user_model().objects.count()}", password="unused"
        )
        user.groups.add(Group.objects.get(name=ROLE_EXAM_COMMITTEE))
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport=viewport,
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
        if wide_font:
            context.add_init_script(WIDE_FONT)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"{self.live_server_url}/exam-timetable/")
        page.locator("#examHistorySummary").click()
        page.locator("#historyList .et-history-item", has_text=LABEL).locator(
            ".et-run-info"
        ).click()
        expect(page.locator("#schedGrid .et-link-group")).to_have_count(1)
        return page

    def _assert_fits(self, page, element_id: str, where: str) -> None:
        facts = page.evaluate(FITS, element_id)
        self.assertEqual(facts["outside"], [], f"{where}: {element_id} spills: {facts}")
        self.assertEqual(facts["broken"], [], f"{where}: a code broke over two lines")
        self.assertEqual(facts["spill"], [], f"{where}: something left its table cell")
        self.assertLessEqual(facts["page"], 0, f"{where}: the page scrolls sideways")

    def _assert_layout(self, language: str, width: int, wide_font: bool) -> None:
        where = f"{language} {width}px{' wide font' if wide_font else ''}"
        page = self._page(language, {"width": width, "height": 900}, wide_font)
        # The board: one group, both cards inside it, each naming the other.
        groups = page.evaluate(GROUPS)
        self.assertEqual(len(groups), 1, where)
        group = groups[0]
        self.assertEqual(sorted(group["cards"]), ["IS201", "MATH101"], where)
        self.assertTrue(
            group["inCell"] and group["cardsInGroup"] and group["labelsInCards"],
            f"{where}: {group}",
        )
        self.assertEqual(group["broken"], [], where)
        partner = "مع" if language == "ar" else "with"
        self.assertEqual(
            sorted(group["labels"]), sorted([f"{partner} IS201", f"{partner} MATH101"]), where
        )
        # The builder: the link, its time and the warning, inside the section.
        page.locator("#examSetupSummary").click()
        editor = page.locator("#examLinkEditor")
        expect(editor).to_be_visible()
        editor.scroll_into_view_if_needed()
        expect(page.locator("#examLinkRows tr")).to_have_count(1)
        expect(page.locator("#examLinkWarnings")).to_be_visible()
        expect(page.locator("#examLinkWarningList li").first).to_contain_text(str(self.clash))
        self._assert_fits(page, "examLinkEditor", where)
        self._assert_fits(page, "linkBar", where)
        # Linking two courses that sit apart: the dialog fits, and the real
        # Check accepts the board it leaves.
        for code in ("PHYS103 (1)", "PHYS103 (2)"):
            page.locator(f'#schedGrid .et-course[data-course="{code}"] [data-exam-pin]').click()
        for code in ("PHYS103 (1)", "PHYS103 (2)"):
            search = page.locator("#examLinkCourseSearch")
            search.click()
            search.fill(code)
            page.locator(f'#examLinkCourseResults [role="option"][data-value="{code}"]').click()
        # The chosen courses, as chips, before they are linked.
        expect(page.locator("#examLinkPending .et-link-chip")).to_have_count(2)
        self._assert_fits(page, "examLinkEditor", f"{where}, two courses chosen")
        # The page's chip shape, never a pill of its own.
        radii = page.evaluate(
            """() => ['#examLinkPending .et-link-chip', '#progList .et-chip']
              .map(selector => getComputedStyle(document.querySelector(selector)).borderTopLeftRadius)"""
        )
        self.assertEqual(radii[0], radii[1], where)
        page.locator("#applyExamLink").click()
        dialog = page.locator("#examLinkDialog")
        expect(dialog).to_be_visible()
        expect(page.locator("#examLinkOptions input[type=radio]")).to_have_count(2)
        facts = page.evaluate(FITS, "examLinkDialog")
        self.assertEqual(facts["outside"], [], f"{where}: the dialog spills: {facts}")
        self.assertEqual(facts["broken"], [], f"{where}: a dialog code broke")
        self.assertGreaterEqual(facts["left"], 0, where)
        self.assertLessEqual(facts["right"], facts["viewport"], where)
        page.locator("#confirmExamLink").click()
        expect(dialog).to_be_hidden()
        expect(page.locator("#schedGrid .et-link-group")).to_have_count(2)
        expect(page.locator("#examLinkRows tr")).to_have_count(2)
        page.locator("#checkDraftBtn").click()
        expect(page.locator("#examCheckStatus")).to_contain_text(
            "تم التحقق" if language == "ar" else "Checked"
        )
        expect(page.locator("#examEditorRequestError")).to_be_hidden()
        self._assert_fits(page, "examLinkEditor", f"{where}, two links")
        for group in page.evaluate(GROUPS):
            self.assertTrue(
                group["inCell"] and group["cardsInGroup"] and group["labelsInCards"],
                f"{where}: {group}",
            )
            self.assertEqual(group["broken"], [], where)

    def test_linked_exams_fit_a_laptop_in_english_and_arabic(self) -> None:
        for language in ("en", "ar"):
            self._assert_layout(language, 1366, wide_font=False)

    def test_linked_exams_fit_a_phone_in_english_and_arabic(self) -> None:
        for language in ("en", "ar"):
            self._assert_layout(language, 375, wide_font=False)

    def test_linked_labels_are_legible_on_every_card_colouring(self) -> None:
        # Small text (11px) needs 4.5:1, in each review colouring and theme.
        page = self._page("en", {"width": 1366, "height": 900})
        page.emulate_media(reduced_motion="reduce")
        measured = 0
        for theme in ("light", "dark"):
            page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
            for mode in ("size", "neutral", "students"):
                page.select_option("#examReviewMode", mode)
                if mode == "students":
                    page.locator(
                        '#schedGrid .et-course[data-course="MATH101"] [data-exam-related]'
                    ).click()
                    expect(
                        page.locator('#schedGrid .et-course[data-course="IS201"]')
                    ).to_have_class(re.compile(r"\bet-related-conflict\b"))
                ratios = page.evaluate(CONTRAST)
                self.assertEqual(
                    sorted(code for code, _ in ratios), ["IS201", "MATH101"], (theme, mode)
                )
                for code, ratio in ratios:
                    self.assertIsNotNone(ratio, (theme, mode, code))
                    self.assertGreaterEqual(ratio, 4.5, (theme, mode, code))
                    measured += 1
        self.assertEqual(measured, 12)

    def test_linked_exams_fit_in_a_wide_font(self) -> None:
        for language in ("en", "ar"):
            for width in (1366, 375):
                self._assert_layout(language, width, wide_font=True)
