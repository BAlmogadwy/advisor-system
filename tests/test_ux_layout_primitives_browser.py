"""The design system's layout primitives, on the Timetable Workspace (stage 3,
C2; docs/stage3/C2.md).

A real draft scenario in a real browser, in English and Arabic:

- every template dialog's close button sits at the dialog's end edge (the
  slot dialog's was 236px from it, L14);
- a meta line that wraps never starts a line with its dot, and an Arabic
  line keeps one dot between parts written the other way (L29);
- a toolbar row does not grow with the font's line box: with a face whose
  line box is as tall as the Arabic one's, the chrome above the panes is
  still at most 170px at 1440x790 and the Sem field at least 24px wide
  (L30, L31); with the WCAG 1.4.12 text spacing the bands grow, and nothing
  in them is clipped or overlapped;
- a dialog body that takes focus draws its ring inside itself, and the
  status line is outside the scroller (L33);
- the board strip and the inspector's tabs show their overflow control at
  1366x650 when they do not fit; the chosen board and tab are wholly in view,
  clear of it; the buttons page the strip, take no Tab stop and no focus,
  have edges of at least 3:1 in both themes and show in forced colours
  (L07, L16).

Nothing is sent but reads, and nothing leaves the machine
(tests/browser_isolation.py).
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from tests.test_exam_rosters_browser import WIDE_FONT
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

pytest.importorskip("playwright.sync_api")

# A face whose line box is about twice its size, as the Arabic face's (Noto
# Kufi Arabic: 2.0, measured on QA) is, and whose digits are narrow, as that
# face's are: Verdana or DejaVu Sans with its metrics overridden, so the test
# does not depend on a web font CI cannot fetch (tests/browser_isolation.py).
# `normal` line-height takes this line box (2.1 after size-adjust); an
# explicit one does not. Tuned so that undoing any one of C2's rules (.btn-sm
# or .fb-sm line-height, the Sem floor) fails, as each did with the real face
# (scratchpad/stage3/C2/tune_font.py: 171.7, 175.7px, 19.8px).
TALL_FONT = """(() => {
  const add = () => {
    const style = document.createElement('style');
    style.textContent = "@font-face { font-family: 'Tall line box'; src: local('Verdana'), local('DejaVu Sans');"
      + " ascent-override: 185%; descent-override: 75%; line-gap-override: 0%; size-adjust: 80%; }"
      + " :root, :root *:not(#_) { font-family: 'Tall line box', sans-serif !important; }";
    document.head.append(style);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add, { once: true });
  else add();
})()"""

# WCAG 1.4.12: line height 1.5, letter spacing 0.12em, word spacing 0.16em,
# paragraph spacing 2em.
TEXT_SPACING = (
    "* { line-height: 1.5 !important; letter-spacing: 0.12em !important;"
    " word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }"
)

DIALOG_EDGE = r"""() => [...document.querySelectorAll('dialog.ux-dialog')].map(d => {
  d.showModal();
  const close = d.querySelector('.ux-dialog-head [data-dialog-close]');
  const a = d.getBoundingClientRect(), c = close.getBoundingClientRect();
  const head = d.querySelector('.ux-dialog-head').getBoundingClientRect();
  const rtl = getComputedStyle(d).direction === 'rtl';
  const found = { id: d.id, gap: Math.round(rtl ? c.left - a.left : a.right - c.right),
                  inHead: c.top >= head.top - 0.5 && c.bottom <= head.bottom + 0.5 };
  d.close();
  return found;
})"""

# Measured on the painted words, en and ar alike: a part that starts a line
# starts with its words (nothing drawn before them on the line's start side),
# and between the words of two parts on one line there is one dot (about
# 1em of gap; 0.35em without one, about 1.6em with two).
META = r"""() => {
  const out = { lines: 0, wrapped: 0, startsWithDot: [], boundaries: 0, notOneDot: [] };
  const words = part => { const range = document.createRange(); range.selectNodeContents(part); return range.getBoundingClientRect(); };
  for (const meta of document.querySelectorAll('.ux-meta')) {
    if (!meta.getClientRects().length) continue;
    out.lines++;
    const rtl = getComputedStyle(meta).direction === 'rtl';
    const em = parseFloat(getComputedStyle(meta).fontSize);
    const parts = [...meta.children].filter(part => !part.hidden && part.getClientRects().length);
    const tops = parts.map(part => Math.round(part.getBoundingClientRect().top));
    if (new Set(tops).size > 1) out.wrapped++;
    parts.forEach((part, i) => {
      const box = part.getBoundingClientRect();
      const text = words(part);
      const lead = rtl ? box.right - text.right : text.left - box.left;
      if ((i === 0 || tops[i] > tops[i - 1] + 2) && lead > 2) out.startsWithDot.push(`${part.textContent.trim()} (${Math.round(lead)}px)`);
      if (i + 1 < parts.length && tops[i + 1] <= tops[i] + 2) {
        out.boundaries++;
        const next = words(parts[i + 1]);
        const gap = rtl ? text.left - next.right : next.left - text.right;
        if (gap < 0.6 * em || gap > 1.25 * em) out.notOneDot.push(`${part.textContent.trim()} | ${parts[i + 1].textContent.trim()}: ${Math.round(gap)}px`);
      }
    });
  }
  return out;
}"""

CHROME = r"""() => {
  const main = document.getElementById('main-content').getBoundingClientRect();
  return { chrome: document.querySelector('.tw-workspace').getBoundingClientRect().top - main.top,
           term: document.getElementById('twTerm').getBoundingClientRect().width,
           // The emulated face really is in use: a line box about twice its size.
           tall: (() => { const s = document.createElement('span'); s.style.cssText = 'position:absolute;font-size:20px;line-height:normal'; s.textContent = 'Ag';
                          document.body.append(s); const h = s.getBoundingClientRect().height; s.remove(); return h / 20; })() };
}"""

# Under the text spacing: no band spills into the next, no control leaves its
# band, nothing cuts its own content (a strip's scroller aside), no field cuts
# its value, and no two controls overlap (a strip's items are where it shows
# them). Visually hidden boxes (1px, clipped) are not on screen.
SPACING_CHECK = r"""() => {
  const bad = [];
  const r = e => e.getBoundingClientRect();
  const chain = function* (e) { for (let a = e; a; a = a.parentElement) yield a; };
  const shown = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'
    && !(r(e).width <= 1.5 && r(e).height <= 1.5)
    && ![...chain(e)].some(a => a.classList.contains('visually-hidden') || (getComputedStyle(a).clipPath !== 'none' && r(a).width <= 1.5));
  const seen = e => { const box = r(e); const s = e.closest('.ux-overflow > :not(.ux-overflow-btn)');
    if (!s) return box; const v = r(s);
    return { left: Math.max(box.left, v.left), right: Math.min(box.right, v.right), top: box.top, bottom: box.bottom }; };
  const bands = ['.page-header', '#twCmdBar', '#twQualitySummary'].map(s => document.querySelector(s));
  const edges = bands.map(r).concat([r(document.querySelector('.tw-workspace'))]);
  for (let i = 0; i + 1 < edges.length; i++) if (edges[i].bottom > edges[i + 1].top + 0.5) bad.push(`band ${i} spills ${Math.round(edges[i].bottom - edges[i + 1].top)}px into the next`);
  const leaves = bands.flatMap(b => [...b.querySelectorAll('button, input, select, label, .pill-status, .ux-toolbar-label, .ux-seg-item, .ux-stat > dt, .ux-stat > dd, .ux-stat-scope')])
    .filter(e => shown(e) && !e.closest('.ux-menu'));
  for (const e of leaves) {
    const q = r(bands.find(b => b.contains(e))), a = r(e);
    if (a.top < q.top - 0.5 || a.bottom > q.bottom + 0.5) bad.push(`${e.id || e.className} is outside its band`);
  }
  const scrollers = new Set(document.querySelectorAll('.ux-overflow > :not(.ux-overflow-btn)'));
  for (const e of bands.flatMap(b => [b, ...b.querySelectorAll('*')])) {
    if (!shown(e) || scrollers.has(e) || e.closest('.ux-menu') || e.matches('input, select')) continue;
    const cs = getComputedStyle(e);
    if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
    if (e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1) bad.push(`${e.id || e.className || e.tagName} cuts its content: "${e.textContent.trim().slice(0, 24)}"`);
  }
  for (const e of leaves.filter(e => e.matches('input, select'))) if (e.scrollWidth > e.clientWidth + 1) bad.push(`${e.id} cuts its value`);
  const boxes = leaves.filter(e => !leaves.some(o => o !== e && e.contains(o)));
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = seen(boxes[i]), b = seen(boxes[j]);
    if (a.right - a.left <= 0 || b.right - b.left <= 0) continue;
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (w > 1 && h > 1) bad.push(`${boxes[i].id || boxes[i].className} overlaps ${boxes[j].id || boxes[j].className}`);
  }
  return bad;
}"""

# One overflow strip: what it can scroll, which ends show a button, and
# where the active item sits: inside the strip, under no button, clear of the
# cue at each end that has more to see.
STRIP = r"""(selector) => {
  const wrapper = document.querySelector(selector);
  const scroller = [...wrapper.children].find(c => !c.classList.contains('ux-overflow-btn'));
  const buttons = [...wrapper.querySelectorAll(':scope > .ux-overflow-btn')];
  const shown = buttons.filter(b => getComputedStyle(b).display !== 'none' && b.getClientRects().length);
  const active = [...scroller.children].find(i => i.getAttribute('aria-current') === 'true' || i.getAttribute('aria-selected') === 'true');
  const v = scroller.getBoundingClientRect(), a = active.getBoundingClientRect();
  const rtl = getComputedStyle(scroller).direction === 'rtl';
  const cue = parseFloat(getComputedStyle(wrapper).getPropertyValue('--ux-overflow-cue'));
  const fromStart = rtl ? v.right - a.right : a.left - v.left, fromEnd = rtl ? a.left - v.left : v.right - a.right;
  return {
    range: scroller.scrollWidth - scroller.clientWidth,
    along: Math.abs(scroller.scrollLeft),
    shown: shown.map(b => b.dataset.uxOverflowDir),
    sizes: shown.map(b => [Math.round(b.getBoundingClientRect().width), Math.round(b.getBoundingClientRect().height)]),
    unreachable: buttons.filter(b => b.tabIndex !== -1 || b.getAttribute('aria-hidden') !== 'true' || b.type !== 'button').length,
    active: active.id || active.dataset.boardId,
    activeInside: a.left >= v.left - 0.5 && a.right <= v.right + 0.5,
    activeUnderButton: shown.some(b => { const q = b.getBoundingClientRect(); return q.left < a.right && q.right > a.left; }),
    activeUnderCue: (wrapper.hasAttribute('data-overflow-start') && fromStart < cue - 0.5)
      || (wrapper.hasAttribute('data-overflow-end') && fromEnd < cue - 0.5),
  };
}"""

# A shown button's 1px edge against its own fill and against what is around
# it (each background composited up the tree), and its chevron's ink.
EDGES = r"""(selector) => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3])).concat(1);
  const page = getComputedStyle(document.body).backgroundColor;
  const behind = el => { const chain = []; for (let e = el; e; e = e.parentElement) chain.push(rgba(getComputedStyle(e).backgroundColor));
    return chain.reverse().reduce((under, c) => c[3] > 0 ? over(c, under) : under, rgba(page)); };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  return [...document.querySelectorAll(selector + ' > .ux-overflow-btn')].filter(b => getComputedStyle(b).display !== 'none').map(b => {
    const cs = getComputedStyle(b);
    const fill = behind(b), around = behind(b.parentElement), edge = over(rgba(cs.borderTopColor), fill);
    return { dir: b.dataset.uxOverflowDir, style: cs.borderTopStyle, width: parseFloat(cs.borderTopWidth),
             fill: ratio(edge, fill), around: ratio(edge, around), ink: ratio(over(rgba(cs.color), fill), fill) };
  });
}"""

BOARDS = ".ux-overflow.tw-boards"
TABS = ".tw-inspector-head > .ux-overflow"


class LayoutPrimitivesBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def _many_boards(self) -> None:
        """Ten more boards, the page opened on the last: the strip cannot hold
        them at 1366 and has to show the chosen one."""
        from core.models import DeliveryBoard

        last = None
        for term in range(3, 13):
            last = DeliveryBoard.objects.create(
                scenario=self.scenario,
                label=f"Term {term}",
                nominal_term=term,
                program="AI",
                display_order=term,
            )
        self.path = f"/timetable-workspace/?scenario={self.scenario.id}&board={last.id}"
        self.last_board = last

    def _page(self, language: str, path: str):
        """Another page of the design system, as the same user, in the wide font."""
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": 1440, "height": 790},
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        context.add_init_script(WIDE_FONT)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{self.live_server_url}{path}")
        return page, errors

    def test_every_template_dialog_closes_from_its_end_edge(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for width, height in ((1366, 650), (1440, 790)):
                page, sent, errors = self._load(language, "light", width, height)
                found = page.evaluate(DIALOG_EDGE)
                self.assertGreaterEqual(len(found), 5, found)
                bad = [d for d in found if d["gap"] > 24 or not d["inHead"]]
                if bad or sent or errors:
                    problems[f"{language} {width}"] = [*bad, *sent, *errors]
                page.context.close()
            # The same rule on another page's template dialog.
            page, errors = self._page(language, "/instructor-management/")
            page.wait_for_selector("#imInstructorDialog", state="attached")
            found = page.evaluate(DIALOG_EDGE)
            self.assertEqual([d["id"] for d in found], ["imInstructorDialog"])
            bad = [d for d in found if d["gap"] > 24 or not d["inHead"]]
            if bad or errors:
                problems[f"{language} instructors"] = [*bad, *errors]
        self.assertEqual(problems, {})

    def test_a_wrapped_meta_line_never_starts_with_its_dot(self) -> None:
        problems: dict[str, dict] = {}
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1920, 1080)
            if page.evaluate("document.getElementById('twWorkspace').dataset.left") != "open":
                page.click("#twToggleLeft")
            # A narrow list wraps every row's meta line; a selected card lists
            # its meetings (a left-to-right time in an Arabic line).
            page.evaluate(
                "document.getElementById('twWorkspace').style.setProperty('--tw-left-w', '130px')"
            )
            page.locator(".tw-card").first.click()
            page.wait_for_selector(".tw-sel-meetings .ux-meta")
            found = page.evaluate(META)
            self.assertGreater(found["wrapped"], 0, f"{language}: no meta line wrapped {found}")
            self.assertGreater(
                found["boundaries"], 0, f"{language}: no two parts share a line {found}"
            )
            if found["startsWithDot"] or found["notOneDot"] or sent or errors:
                problems[language] = {**found, "sent": sent, "errors": errors}
            page.context.close()
        self.assertEqual(problems, {})

    def test_toolbar_rows_do_not_grow_with_the_fonts_line_box(self) -> None:
        problems: dict[str, dict] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                page, sent, errors = self._load(language, theme, 1440, 790, font=TALL_FONT)
                page.evaluate("document.fonts.ready")
                found = page.evaluate(CHROME)
                self.assertGreater(
                    found["tall"], 1.95, f"the tall-metrics face is not in use: {found}"
                )
                if found["chrome"] > 170.5 or found["term"] < 24 or sent or errors:
                    problems[f"{language} {theme}"] = {**found, "sent": sent, "errors": errors}
                page.context.close()
        self.assertEqual(problems, {})

    def test_text_spacing_grows_the_chrome_and_never_cuts_or_overlaps_it(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for width, height in ((1366, 650), (1440, 790)):
                page, sent, errors = self._load(language, "light", width, height)
                before = page.evaluate(
                    "document.querySelector('.page-header').getBoundingClientRect().height"
                )
                page.add_style_tag(content=TEXT_SPACING)
                page.wait_for_timeout(300)
                found = page.evaluate(SPACING_CHECK)
                after = page.evaluate(
                    "document.querySelector('.page-header').getBoundingClientRect().height"
                )
                if language == "en" and width == 1366 and after <= before:
                    found.append(
                        f"the header did not grow ({before} -> {after}): the spacing was not applied"
                    )
                if found or sent or errors:
                    problems[f"{language} {width}"] = [*found, *sent, *errors]
                page.context.close()
        self.assertEqual(problems, {})

    def test_a_focused_dialog_body_draws_its_ring_inside_and_its_status_stays_outside(self) -> None:
        page, sent, errors = self._load("en", "light", 1440, 790)
        found = page.evaluate(
            """() => [...document.querySelectorAll('dialog.ux-dialog-scroll')].map(d => {
              d.showModal();
              const body = d.querySelector('.ux-dialog-body');
              document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
              body.focus();
              const cs = getComputedStyle(body);
              const status = d.querySelector('[role="status"]');
              const found = { id: d.id, focusVisible: body.matches(':focus-visible'), offset: parseFloat(cs.outlineOffset),
                              width: parseFloat(cs.outlineWidth), shadow: cs.boxShadow,
                              statusInBody: Boolean(status && body.contains(status)) };
              d.close();
              return found;
            })"""
        )
        self.assertGreaterEqual(len(found), 4, found)
        for dialog in found:
            self.assertTrue(dialog["focusVisible"], dialog)
            # Inside: the ring sits within the body's own box.
            self.assertLessEqual(dialog["offset"] + dialog["width"], 0, dialog)
            self.assertEqual(dialog["shadow"], "none", dialog)
            self.assertFalse(dialog["statusInBody"], dialog)
        self.assertEqual([*sent, *errors], [])

    def test_strips_that_do_not_fit_show_their_overflow_control_and_keep_the_chosen_item_in_view(
        self,
    ) -> None:
        self._many_boards()
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                page, sent, errors = self._load(language, theme, 1366, 650, cards=0)
                page.wait_for_function(
                    f"document.querySelector('#twBoardTabs [aria-current=\"true\"]')?.dataset.boardId === '{self.last_board.id}'"
                )
                page.wait_for_timeout(300)
                found: list = []
                boards, tabs = page.evaluate(STRIP, BOARDS), page.evaluate(STRIP, TABS)
                for name, strip in (("boards", boards), ("tabs", tabs)):
                    if strip["range"] < 2 or not strip["shown"]:
                        found.append(f"{name}: no overflow control ({strip})")
                    if (
                        not strip["activeInside"]
                        or strip["activeUnderButton"]
                        or strip["activeUnderCue"]
                    ):
                        found.append(f"{name}: the chosen item is not wholly in view ({strip})")
                    if strip["unreachable"]:
                        found.append(f"{name}: a button takes a Tab stop or is exposed ({strip})")
                    if any(w < 24 or h < 24 for w, h in strip["sizes"]):
                        found.append(f"{name}: a button under 24px ({strip})")
                # The last board is chosen: the strip is at its end, its button at the start.
                if boards["shown"] != ["start"]:
                    found.append(f"boards: buttons {boards['shown']}")
                if tabs["shown"] != ["end"]:
                    found.append(f"tabs: buttons {tabs['shown']}")
                for edge in page.evaluate(EDGES, BOARDS) + page.evaluate(EDGES, TABS):
                    if (
                        edge["style"] != "solid"
                        or edge["width"] < 1
                        or min(edge["fill"], edge["around"], edge["ink"]) < 3
                    ):
                        found.append(f"edge {edge}")
                # A press pages the strip and leaves focus where it was.
                page.focus("#twSearch")
                page.click(f"{TABS} > .ux-overflow-btn[data-ux-overflow-dir='end']")
                page.wait_for_function(
                    f"document.querySelector('{TABS}').hasAttribute('data-overflow-start')"
                    f" && !document.querySelector('{TABS}').hasAttribute('data-overflow-end')"
                )
                if page.evaluate("document.activeElement.id") != "twSearch":
                    found.append(f"focus moved to {page.evaluate('document.activeElement.id')}")
                # Demand chosen by the keyboard: wholly in view, clear of the cue.
                page.click("#twTabIssues")
                page.focus("#twTabIssues")
                for _ in range(3):
                    page.keyboard.press("ArrowLeft" if language == "ar" else "ArrowRight")
                page.wait_for_timeout(300)
                tabs = page.evaluate(STRIP, TABS)
                if (
                    tabs["active"] != "twTabDemand"
                    or not tabs["activeInside"]
                    or tabs["activeUnderButton"]
                    or tabs["activeUnderCue"]
                ):
                    found.append(f"tabs after choosing Demand: {tabs}")
                if found or sent or errors:
                    problems[f"{language} {theme}"] = [*found, *sent, *errors]
                page.context.close()
        self.assertEqual(problems, {})

    def test_the_overflow_buttons_show_in_forced_colours(self) -> None:
        self._many_boards()
        for language in ("en", "ar"):
            page, sent, errors = self._load(
                language, "light", 1366, 650, cards=0, forced_colors="active"
            )
            page.wait_for_function(
                f"document.querySelector('#twBoardTabs [aria-current=\"true\"]')?.dataset.boardId === '{self.last_board.id}'"
            )
            page.wait_for_timeout(300)
            shown = page.evaluate(
                """() => [...document.querySelectorAll('.ux-overflow-btn')].filter(b => getComputedStyle(b).display !== 'none')
                  .map(b => { const cs = getComputedStyle(b);
                    return { style: cs.borderTopStyle, width: parseFloat(cs.borderTopWidth), edge: cs.borderTopColor, fill: cs.backgroundColor, ink: cs.color }; })"""
            )
            self.assertEqual(len(shown), 2, shown)
            for button in shown:
                self.assertEqual(button["style"], "solid", button)
                self.assertGreaterEqual(button["width"], 1, button)
                self.assertNotEqual(button["edge"], button["fill"], button)
                self.assertNotEqual(button["ink"], button["fill"], button)
            self.assertEqual([*sent, *errors], [])
            page.context.close()


class RightToLeftResidueBrowserTests(StaticLiveServerTestCase):
    """global.css's last physical-direction rules (SW-27), read in a page that
    loads only global.css, in both directions."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _read(self, direction: str) -> dict:
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto(f"{self.live_server_url}/static/css/global.css")
        page.set_content(
            f"""<!doctype html><html dir="{direction}" lang="{"ar" if direction == "rtl" else "en"}"><head>
            <link rel="stylesheet" href="{self.live_server_url}/static/css/global.css"></head><body>
            <div class="ap-drawer" style="animation-play-state: paused"></div>
            <table class="cm-table"><tr><th class="cm-corner"></th></tr><tr><th class="cm-row-hdr">CS101</th></tr></table>
            <table class="va-tool-table"><tr><th>Tool</th><td>x</td></tr></table>
            <dl class="va-runtime"><dd>1.2 s</dd></dl>
            <div class="um-pw-wrap"><input class="form-control"><button class="um-gen-pw">Generate</button></div>
            </body></html>"""
        )
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.cm-row-hdr')).position === 'sticky'"
        )
        return page.evaluate(
            """() => {
              const cs = (sel, pseudo) => getComputedStyle(document.querySelector(sel), pseudo);
              const keyframes = [...document.styleSheets].flatMap(sheet => { try { return [...sheet.cssRules]; } catch (e) { return []; } })
                .filter(rule => rule.type === CSSRule.KEYFRAMES_RULE && rule.name.startsWith('ap-drawer'));
              const drawer = document.querySelector('.ap-drawer');
              const start = drawer.getAnimations()[0];
              if (start) { start.pause(); start.currentTime = 0; }
              return {
                keyframes: keyframes.map(rule => rule.name),
                drawerFrom: getComputedStyle(drawer).transform,
                corner: [cs('.cm-corner').left, cs('.cm-corner').right],
                rowHead: [cs('.cm-row-hdr').left, cs('.cm-row-hdr').right, cs('.cm-row-hdr').textAlign],
                toolCell: [cs('.va-tool-table th').textAlign, cs('.va-tool-table td').textAlign],
                runtime: cs('.va-runtime dd').textAlign,
                field: [cs('.um-pw-wrap .form-control').borderTopLeftRadius, cs('.um-pw-wrap .form-control').borderTopRightRadius,
                        cs('.um-pw-wrap .form-control').borderLeftStyle, cs('.um-pw-wrap .form-control').borderRightStyle],
                generate: [cs('.um-pw-wrap .um-gen-pw').borderTopLeftRadius, cs('.um-pw-wrap .um-gen-pw').borderTopRightRadius,
                           cs('.um-pw-wrap .um-gen-pw').borderLeftStyle, cs('.um-pw-wrap .um-gen-pw').borderRightStyle],
              };
            }"""
        )

    def test_rules_follow_the_writing_direction(self) -> None:
        ltr, rtl = self._read("ltr"), self._read("rtl")
        # One valid pair of keyframes (a `[dir] @keyframes` rule is dropped).
        self.assertEqual(sorted(ltr["keyframes"]), ["ap-drawer-in", "ap-drawer-out"])
        # The drawer slides in from its own edge: the right, the left in Arabic.
        self.assertRegex(ltr["drawerFrom"], r"^matrix\(1, 0, 0, 1, \d")
        self.assertRegex(rtl["drawerFrom"], r"^matrix\(1, 0, 0, 1, -\d")
        self.assertEqual(ltr["corner"], ["0px", "auto"])
        self.assertEqual(rtl["corner"], ["auto", "0px"])
        self.assertEqual(ltr["rowHead"][:2], ["0px", "auto"])
        self.assertEqual(rtl["rowHead"][:2], ["auto", "0px"])
        for side in (ltr, rtl):
            # (The row header's own text-align is overridden by `.cm-table th`:
            # centred in both directions, as before.)
            self.assertEqual(side["rowHead"][2], "center")
            self.assertEqual(side["toolCell"], ["start", "start"])
            self.assertEqual(side["runtime"], "end")
        # The field and its button are one capsule: rounded at its outer ends,
        # and no edge where they meet.
        self.assertEqual(ltr["field"], ["14px", "0px", "solid", "none"])
        self.assertEqual(rtl["field"], ["0px", "14px", "none", "solid"])
        self.assertEqual(ltr["generate"], ["0px", "14px", "none", "solid"])
        self.assertEqual(rtl["generate"], ["14px", "0px", "solid", "none"])
