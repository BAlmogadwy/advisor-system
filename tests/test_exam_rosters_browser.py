"""Student lists in a real browser, against the real endpoints.

The jsdom suites (tests/frontend/exam-rosters.test.cjs and
exam-roster-drawer.test.cjs) cover every state with recorded answers. This
runs the whole path in Chromium on a run made by the real build: a room's
list, a student found by ID and never put in the address, the course drawer
from a card with focus kept as a browser keeps it, the same-day card and its
exam-pair detail beside those card links, the Export dialog preset
to the room on screen and the file it downloads, and the Arabic page and
drawer at phone width. The lists, the lookup and the drawer are laid out at
every width in the design's font and in a wide one (Verdana, DejaVu Sans), so
a layout that only fits the fonts of the machine running it fails anywhere.
Nothing leaves the machine (tests/browser_isolation.py).
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
from tests.exam_student_export_fixture import build_population, build_saved_run, saved_payload

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

LOOKUP_ID = 4402003
# The drawer has finished sliding in (its 180 ms transition).
SETTLED = "() => document.getElementById('examRosterDrawer').getAnimations().length === 0"
# A wide font over the whole page, as a browser without the design's fonts
# falls back to one: Verdana here, DejaVu Sans on a Linux runner (where CI
# first found a table poking out of the pane). `:not(#_)` outranks the design's
# own `!important` font on buttons. Added before any script runs, on every page
# the context opens.
WIDE_FONT = """(() => {
  const add = () => {
    const style = document.createElement('style');
    style.textContent = ":root, :root *:not(#_) { font-family: Verdana, 'DejaVu Sans', sans-serif !important; }";
    document.head.append(style);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add, { once: true });
  else add();
})()"""
# How wide ten digits are at 100px in the list's font: 636 in Verdana and
# DejaVu Sans, 556-572 in Arial, Segoe UI, Liberation Sans or Noto Sans.
DIGITS_WIDTH = """() => {
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;visibility:hidden;font-size:100px;font-weight:400;white-space:nowrap';
  probe.textContent = '0123456789';
  document.getElementById('main-content').append(probe);
  const width = probe.getBoundingClientRect().width;
  probe.remove();
  return width;
}"""
# The list or lookup inside a container (the Student lists pane or the
# drawer's body): every box under it that pokes out of its content box (into
# its padding or beyond); every box in its table that spills out of its own
# cell (over a neighbour, where nothing leaves the pane); every code, ID or
# time in its table broken over two lines; the form the table takes (full,
# compact, table or rows), and the name column's share of it; and how far the
# container, the page and its main column scroll sideways.
# Hidden-for-screen-readers boxes (1px) and empty ones are not layout.
LAYOUT = """(id) => {
  const element = document.getElementById(id);
  const box = element.getBoundingClientRect();
  const style = getComputedStyle(element);
  const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
  const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
  const main = document.getElementById('main-content');
  const shown = node => { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const outside = [...element.querySelectorAll('*')].filter(shown)
    .filter(node => { const r = node.getBoundingClientRect(); return r.left < left - 1 || r.right > right + 1; })
    .map(node => node.id || node.className || node.tagName);
  // Boxes and runs of text alike: a word too long for a box that keeps its
  // width runs out of the box, not the box out of its cell.
  const inCell = node => {
    const cell = node.parentElement.closest('th, td');
    const head = cell && cell.closest('thead');
    return cell && shown(cell) && !(head && !shown(head)) && shown(node.parentElement) ? cell : null;
  };
  const spills = (rects, cell) => {
    const c = cell.getBoundingClientRect();
    return [...rects].some(r => r.width > 0 && (r.left < c.left - 1 || r.right > c.right + 1));
  };
  const texts = [];
  element.querySelectorAll('table').forEach(table => {
    const walker = document.createTreeWalker(table, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) if (walker.currentNode.textContent.trim()) texts.push(walker.currentNode);
  });
  const spill = [
    ...[...element.querySelectorAll('table :is(th, td) *')].filter(shown)
      .filter(node => inCell(node) && spills([node.getBoundingClientRect()], inCell(node))),
    ...texts.filter(text => {
      const cell = inCell(text);
      if (!cell) return false;
      const range = document.createRange();
      range.selectNodeContents(text);
      return spills(range.getClientRects(), cell);
    }),
  ].map(node => `${inCell(node).className}: ${node.className || node.nodeName} ${node.textContent.trim().slice(0, 24)}`);
  const broken = [...element.querySelectorAll('table bdi[dir=ltr]')]
    .filter(node => node.textContent.trim() && shown(node))
    .filter(node => { const range = document.createRange(); range.selectNodeContents(node); return range.getClientRects().length > 1; })
    .map(node => node.textContent);
  const table = [...element.querySelectorAll('table')].find(shown);
  const name = table && table.querySelector('thead th.et-col-name');
  const program = table && table.querySelector('thead th.et-col-program');
  // List rows keep their header row for screen readers, 1px and clipped.
  const headed = Boolean(table && shown(table.querySelector('thead')));
  return {
    outside,
    spill,
    broken,
    form: !table ? null : !headed ? 'rows' : !program ? 'table' : shown(program) ? 'full' : 'compact',
    name: headed ? name.getBoundingClientRect().width / table.getBoundingClientRect().width : null,
    scroll: element.scrollWidth - element.clientWidth,
    main: main ? main.scrollWidth - main.clientWidth : 0,
    page: document.documentElement.scrollWidth - window.innerWidth,
    right: box.right,
    viewport: window.innerWidth,
  };
}"""
# Every flag names the timetable's longest kind of code, "PHYS103 (1)".
LONG_CODES = """(id) => document.getElementById(id).querySelectorAll('.et-flag bdi[dir=ltr]')
  .forEach(node => { node.textContent = 'PHYS103 (1)'; })"""
# Letters spread far wider than any real font's, in the container's tables
# only; `false` takes the spread away again.
SPREAD_LETTERS = """([id, on]) => {
  document.getElementById('spreadLetters')?.remove();
  if (!on) return;
  const style = document.createElement('style');
  style.id = 'spreadLetters';
  style.textContent = `#${id} table, #${id} table * { letter-spacing: .5em !important; }`;
  document.head.append(style);
}"""
# A focused element that is on screen, top to bottom.
FOCUSED_IN_VIEW = """() => {
  const node = document.activeElement;
  const r = node.getBoundingClientRect();
  return { tag: node.tagName, room: node.dataset.room || null, top: r.top, bottom: r.bottom, height: window.innerHeight };
}"""


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

    def _page(
        self,
        language: str,
        path: str,
        viewport: dict[str, int] | None = None,
        touch: bool = False,
        wide_font: bool = False,
    ):
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
            has_touch=touch,
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
        page.goto(f"{self.live_server_url}{path}")
        return page

    def _rosters(self, language: str = "en", viewport=None, touch: bool = False):
        page = self._page(language, f"/exam-timetable/rosters/?run={self.run.pk}", viewport, touch)
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

    def test_the_same_day_card_and_its_pairs_work_beside_the_card_links(self) -> None:
        """The exam page carries both: the same-day card and each card's link."""
        qa = saved_payload(self.run)["qa"]
        pairs = qa["same_day_exam_pairs"]
        # The real build measured both, so neither side can pass on a stub.
        self.assertGreater(qa["multi_exam_day_students"], 0)
        self.assertGreater(len(pairs), 1)
        page = self._page("en", "/exam-timetable/")
        page.locator("#examHistorySummary").click()
        page.locator("#historyList .et-run-info").first.click()
        math = sum(1 for s in self.sittings if s.exam.code == "MATH101")
        expect(
            page.locator('#schedGrid .et-course[data-course="MATH101"] .et-roster-link')
        ).to_have_text(f"{math} students")
        expect(page.locator("#kMultiExamDay")).to_have_text(str(qa["multi_exam_day_students"]))

        # The card sits in the Full summary, folded until asked for.
        page.locator("#examSummaryDetails > summary").click()
        card = page.locator('.kpi-click[data-drill="same-day-pairs"]')
        card.click()
        drill = page.locator("#kpiDrill")
        expect(drill).to_be_visible()
        expect(card).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#kpiDrillTitle")).to_have_text("Exam pairs on the same day")
        expect(page.locator("#kpiDrillNote")).to_be_visible()
        rows = page.locator("#kpiDrillBody tr")
        expect(rows).to_have_count(len(pairs))
        self.assertEqual(
            rows.locator(".et-pair-count").all_inner_texts(),
            [str(pair["student_count"]) for pair in pairs],
        )
        # The detail's exam cards never grow a student link; the grid's do.
        expect(page.locator("#kpiDrillBody .et-roster-link")).to_have_count(0)

        # Find from the detail lands on the grid card, which still has its link.
        code = pairs[0]["courses"][0]["code"]
        students = sum(1 for s in self.sittings if s.exam.code == code)
        rows.first.locator("[data-find-exam]").first.click()
        grid_card = page.locator(f'#schedGrid .et-course[data-course="{code}"]')
        expect(grid_card).to_be_focused()
        link = grid_card.locator(".et-review-badges > .et-roster-link")
        expect(link).to_have_text(f"{students} students")
        link.click()
        drawer = page.locator("#examRosterDrawer")
        expect(drawer).to_be_visible()
        expect(page.locator("#examRosterDrawerTitle")).to_be_focused()
        expect(page.locator("#examRosterDrawerBody tr.et-roster-row")).to_have_count(students)
        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(link).to_be_focused()
        # The detail is as the drawer found it.
        expect(drill).to_be_visible()
        expect(rows).to_have_count(len(pairs))
        expect(page.locator("#kMultiExamDay")).to_have_text(str(qa["multi_exam_day_students"]))

    def test_arabic_page_and_drawer_fit_a_phone(self) -> None:
        phone = {"width": 375, "height": 812}
        page = self._rosters("ar", viewport=phone, touch=True)
        self.assertTrue(page.evaluate("matchMedia('(pointer: coarse)').matches"))
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
        # On a touch screen a list row's cells are as tall as their words, not
        # a table cell's 44px touch height.
        tallest = page.evaluate(
            """() => Math.max(...[...document.querySelectorAll('#examRostersRoster tr.et-roster-row td.et-col-id')]
                .filter(cell => cell.getBoundingClientRect().height > 0)
                .map(cell => cell.getBoundingClientRect().height))"""
        )
        self.assertLess(tallest, 44)
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
        # The saved date and time, and the check time, never break mid-token.
        expect(timetable.locator("#examRosterDrawerBody tr.et-roster-row").first).to_be_visible()
        lines = timetable.evaluate(
            """() => [...document.querySelectorAll('#examRosterDrawerSource bdi[dir=ltr]')]
                .map(node => node.getClientRects().length)"""
        )
        self.assertTrue(lines and all(count == 1 for count in lines), lines)

    # ── Layout at every width, both languages, any font ────────

    def _find_student(self, page) -> None:
        find = page.locator("#examRostersFind")
        find.click()
        find.fill(str(LOOKUP_ID))
        expect(page.locator("#examRostersFindList [role=option]").first).to_contain_text(
            str(LOOKUP_ID)
        )
        find.press("Enter")
        expect(page.locator("#examRostersLookup tbody tr").first).to_be_visible()

    def _assert_fits(self, page, container: str, form: str, what: str, pane: bool = True) -> None:
        """The list (or lookup) in `container` takes `form` and never leaves it.

        Nothing pokes out of the container's content box; no code, ID or time
        breaks over two lines; nothing spills out of its cell over a
        neighbour's; the name keeps a fifth of a table at least; the
        container never scrolls sideways (nor the page or its main column on
        the Student lists page). Then again with every flag naming a long code
        (the timetable's "PHYS103 (1)"): its words wrap, its code stays whole.
        Last, with letters spread far wider than any font's: codes may break
        now, but nothing leaves the container still.
        """
        for step in (what, f"{what}, long codes"):
            if step != what:
                page.evaluate(LONG_CODES, container)
            box = page.evaluate(LAYOUT, container)
            self.assertEqual(box["form"], form, step)
            self.assertEqual(box["outside"], [], step)
            self.assertEqual(box["spill"], [], step)
            self.assertEqual(box["broken"], [], step)
            if box["name"] is not None:
                self.assertGreaterEqual(box["name"], 0.2, step)
            self.assertLessEqual(box["scroll"], 1, step)
            self.assertLessEqual(box["right"], box["viewport"] + 1, step)
            if pane:
                self.assertLessEqual(box["main"], 1, step)
                self.assertLessEqual(box["page"], 0, step)
        step = f"{what}, letters spread"
        page.evaluate(SPREAD_LETTERS, [container, True])
        box = page.evaluate(LAYOUT, container)
        page.evaluate(SPREAD_LETTERS, [container, False])
        self.assertEqual(box["form"], form, step)
        self.assertEqual(box["outside"], [], step)
        self.assertEqual(box["spill"], [], step)
        self.assertLessEqual(box["scroll"], 1, step)
        if pane:
            self.assertLessEqual(box["main"], 1, step)
            self.assertLessEqual(box["page"], 0, step)

    def _assert_font(self, page, wide_font: bool, what: str) -> None:
        """A wide-font run really lays out in a wide font, not a narrow fallback."""
        if wide_font:
            self.assertGreaterEqual(page.evaluate(DIGITS_WIDTH), 620, what)

    def _lists_and_a_lookup_stay_inside_the_pane(self, wide_font: bool) -> None:
        base = f"{self.live_server_url}/exam-timetable/rosters/?run={self.run.pk}"
        math = sum(1 for s in self.sittings if s.exam.code == "MATH101")
        font = "wide font" if wide_font else "design font"
        # The pane's width at each: 933px (full), 873px (compact: the full form
        # starts at 900px), 614px (compact), 441px (rows), and 621px once the
        # navigator and the pane take turns (compact).
        forms = {1600: "full", 1540: "compact", 1280: "compact", 1024: "rows", 700: "compact"}
        for language in ("en", "ar"):
            page = self._page(
                language, f"/exam-timetable/rosters/?run={self.run.pk}", wide_font=wide_font
            )
            self._assert_font(page, wide_font, f"{language} {font}")
            for width, height in ((1600, 900), (1540, 900), (1280, 900), (1024, 768), (700, 900)):
                what = f"{language} {font} {width}px"
                page.set_viewport_size({"width": width, "height": height})
                page.goto(f"{base}&view=room&slot=0&room=M-B")
                expect(page.locator("#examRostersRoster tr.et-roster-row")).to_have_count(
                    len(self._room_rows("M-B"))
                )
                self._assert_fits(page, "examRostersPane", forms[width], f"{what} room")
                page.goto(f"{base}&view=course&course=MATH101")
                expect(page.locator("#examRostersRoster tr.et-roster-row")).to_have_count(math)
                self._assert_fits(page, "examRostersPane", forms[width], f"{what} course")
                if forms[width] == "compact":
                    # Program moves under the name: its column is empty, 0px
                    # wide and hidden (from screen readers too).
                    cells = page.evaluate(
                        """() => {
                          const program = document.querySelector('#examRostersRoster td.et-col-program');
                          return {
                            program: [program.getBoundingClientRect().width, getComputedStyle(program).visibility],
                            sub: getComputedStyle(document.querySelector('#examRostersRoster .et-roster-sub')).display,
                          };
                        }"""
                    )
                    self.assertEqual(cells, {"program": [0, "hidden"], "sub": "block"}, what)
                self._find_student(page)
                # A table while the pane has room for one; else a card per exam
                # whose line under the course says when, section and room once.
                table = width == 1600
                shown = page.evaluate(
                    """() => {
                      const row = document.querySelector('#examRostersLookup tbody tr');
                      const shown = selector => getComputedStyle(row.querySelector(selector)).display !== 'none';
                      return [['.et-col-day', '.et-col-period', '.et-col-section', '.et-col-room'].map(shown), shown('.et-lookup-sub')];
                    }"""
                )
                self.assertEqual(shown, [[table] * 4, not table], what)
                self._assert_fits(
                    page, "examRostersPane", "table" if table else "rows", f"{what} lookup"
                )
                self.assertGreater(page.locator("#examRostersLookup [data-open-course]").count(), 0)
                # One way back while the lookup is open; no line starts with a stray "·".
                expect(page.locator("#examRostersScreenBack")).to_be_hidden()
                lines = page.evaluate(
                    """() => [...document.querySelectorAll('#examRostersLookup tbody tr')]
                        .flatMap(row => row.innerText.split(/\\n/).map(line => line.trim()).filter(Boolean))"""
                )
                self.assertTrue(lines, what)
                self.assertFalse([line for line in lines if line.startswith("·")], what)

    def test_lists_and_a_lookup_stay_inside_the_pane_at_laptop_and_tablet_widths(self) -> None:
        self._lists_and_a_lookup_stay_inside_the_pane(wide_font=False)

    def test_lists_and_a_lookup_stay_inside_the_pane_in_a_wide_font(self) -> None:
        """What a browser without the design's fonts shows (CI's Linux runner did)."""
        self._lists_and_a_lookup_stay_inside_the_pane(wide_font=True)

    def test_the_drawer_list_stays_inside_the_drawer_in_either_font(self) -> None:
        math = sum(1 for s in self.sittings if s.exam.code == "MATH101")
        # The drawer's body at each: 752px and 652px (compact), 343px (rows).
        forms = {1280: "compact", 700: "compact", 375: "rows"}
        for language in ("en", "ar"):
            for wide_font in (False, True):
                font = "wide font" if wide_font else "design font"
                page = self._page(
                    language,
                    "/exam-timetable/",
                    viewport={"width": 1280, "height": 900},
                    wide_font=wide_font,
                )
                self._assert_font(page, wide_font, f"{language} {font}")
                page.locator("#examHistorySummary").click()
                page.locator("#historyList .et-run-info").first.click()
                link = page.locator('#schedGrid .et-course[data-course="MATH101"] .et-roster-link')
                link.click()
                expect(page.locator("#examRosterDrawerBody tr.et-roster-row")).to_have_count(math)
                page.wait_for_function(SETTLED)
                for width, form in forms.items():
                    page.set_viewport_size({"width": width, "height": 900})
                    what = f"drawer {language} {font} {width}px"
                    self._assert_fits(page, "examRosterDrawerBody", form, what, pane=False)
                    # The drawer itself stays on screen.
                    box = page.locator("#examRosterDrawer").bounding_box()
                    self.assertGreaterEqual(round(box["x"]), 0, what)
                    self.assertLessEqual(round(box["x"] + box["width"]), width, what)

    def test_the_day_grid_fits_the_navigator_for_a_five_or_six_day_week(self) -> None:
        weeks = [
            build_saved_run(label="Five-day week", days=["Sun", "Mon", "Tue", "Wed", "Thu"]),
            build_saved_run(label="Six-day week", days=["Sun", "Mon", "Tue", "Wed", "Thu", "Sat"]),
        ]
        for (language, week), days in zip(
            [(language, week) for week in weeks for language in ("en", "ar")],
            (5, 5, 6, 6),
            strict=True,
        ):
            page = self._page(language, f"/exam-timetable/rosters/?run={week.pk}")
            for width in (1280, 1024, 820):
                page.set_viewport_size({"width": width, "height": 900})
                expect(page.locator("#examRostersDays [role=tab]")).to_have_count(days)
                fit = page.evaluate(
                    """() => {
                      const nav = document.getElementById('examRostersNav');
                      const grid = document.getElementById('examRostersDays');
                      const box = grid.getBoundingClientRect();
                      const tabs = [...grid.querySelectorAll('[role=tab]')].map(tab => tab.getBoundingClientRect());
                      return {
                        nav: nav.scrollWidth - nav.clientWidth,
                        grid: grid.scrollWidth - grid.clientWidth,
                        inside: tabs.every(r => r.left >= box.left - 1 && r.right <= box.right + 1),
                        widest: Math.max(...tabs.map(r => r.width)),
                        narrowest: Math.min(...tabs.map(r => r.width)),
                      };
                    }"""
                )
                what = f"{language} {days} days {width}px"
                self.assertLessEqual(fit["nav"], 0, what)
                self.assertLessEqual(fit["grid"], 0, what)
                self.assertTrue(fit["inside"], what)
                # One week: no week column; the five days share the width.
                self.assertLessEqual(fit["widest"] - fit["narrowest"], 1, what)

    def test_back_and_rooms_return_focus_to_the_room_in_view(self) -> None:
        page = self._rosters("en", viewport={"width": 375, "height": 600})
        last = page.locator("#examRostersRooms .et-nav-item[data-room]").last
        code = last.get_attribute("data-room")
        for leave in ("rooms", "history"):
            page.locator(f'#examRostersRooms .et-nav-item[data-room="{code}"]').click()
            expect(page.locator("#examRostersPaneTitle")).to_have_text(f"Room {code}")
            # Read from the top: going back must bring the room into view itself.
            page.evaluate(
                "document.getElementById('main-content').scrollTop = 0; window.scrollTo(0, 0)"
            )
            if leave == "rooms":
                page.locator("#examRostersScreenBack").click()
            else:
                page.go_back()
            expect(page.locator("#examRostersNav")).to_be_visible()
            item = page.locator(f'#examRostersRooms .et-nav-item[data-room="{code}"]')
            expect(item).to_be_focused()
            where = page.evaluate(FOCUSED_IN_VIEW)
            self.assertEqual(where["room"], code, leave)
            self.assertGreaterEqual(where["top"], 0, leave)
            self.assertLessEqual(where["bottom"], where["height"], leave)
        # On a desktop, Back keeps the keyboard on the room it returns to.
        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator('#examRostersRooms .et-nav-item[data-room="F-A"]').click()
        page.locator('#examRostersRooms .et-nav-item[data-room="M-B"]').click()
        expect(page.locator("#examRostersPaneTitle")).to_have_text("Room M-B")
        page.go_back()
        expect(page.locator("#examRostersPaneTitle")).to_have_text("Room F-A")
        expect(page.locator('#examRostersRooms .et-nav-item[data-room="F-A"]')).to_be_focused()

    def test_navigator_headings_and_dates_read_as_written(self) -> None:
        for language, slot in (("en", "Sun 08:00-10:00 · 2"), ("ar", "Sun 08:00-10:00 · 2")):
            page = self._rosters(language)
            page.locator('#examRostersViews [data-view="course"]').click()
            heading = page.locator("#examRostersCourses .et-nav-heading").first
            # Spaces and separators survive: the words are one inline run.
            self.assertEqual(heading.inner_text().strip(), slot)
            flush = page.evaluate(
                """() => {
                  const heading = document.querySelector('#examRostersCourses .et-nav-heading').getBoundingClientRect();
                  const list = document.querySelector('#examRostersCourses .et-nav-list').getBoundingClientRect();
                  return [Math.round(heading.left - list.left), Math.round(heading.right - list.right), Math.round(list.top - heading.bottom)];
                }"""
            )
            self.assertEqual(flush, [0, 0, 0], language)
            # A saved date and time never break inside themselves.
            broken = page.evaluate(
                """() => [...document.querySelectorAll('.et-provenance bdi[dir=ltr]')]
                    .filter(node => getComputedStyle(node).whiteSpace !== 'nowrap').length"""
            )
            self.assertEqual(broken, 0, language)
