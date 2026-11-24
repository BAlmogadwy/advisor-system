"""The staff planner (/planner/): its buttons, fields and confirm box are the design system's.

Measured in a real browser on the synthetic student of tests/test_planner_layout_browser.py and
the build of tests/test_planner_lists_browser.py (imported, not copied). Every request the page
makes is answered from made-up data; nothing leaves the machine (tests/browser_isolation.py).

1  Buttons are one family: `.btn` at the ladder's heights, two filled primaries (Fetch, Build),
   Apply the outlined variant, no `pl-btn*`, `wf-back` or `wf-next` left on the page;
2  fields and checkboxes are the shared controls, and a disclosure draws its own chevron;
3  nothing the planner draws carries an inline style (the shared week grid excepted);
4  Apply asks through the app's dialog, never a browser box, and Cancel sends nothing;
5  on a phone every button is at least 40px, every checkbox label and disclosure 44px.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests import test_planner_layout_browser as _layout
from tests import test_planner_lists_browser as _lists

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

LANGUAGES = ("en", "ar")

SAVE_URL = "**/ops/planner/save-student-sections/"
#: What planner_save_student_sections_view answers on success (core/planner_views.py).
SAVED = {
    "ok": True,
    "inserted": 1,
    "removed": [],
    "removed_count": 0,
    "clashes_with_registered": [],
}

DIALOG_TEXT = {
    "en": {
        "title": "Apply option A1?",
        "intro": "This replaces the student's planned timetable with option A1:",
        "outro": "Any previously planned courses not in this option will be removed. Continue?",
        "confirm": "Apply",
        "cancel": "Cancel",
        "saved": "Applied Option A1. Saved 1 mappings.",
    },
    "ar": {
        "title": "تطبيق الخيار A1؟",
        "intro": "سيتم استبدال جدول الطالب المخطط بالخيار A1:",
        "outro": "سيتم حذف أي مقررات مخططة سابقًا غير موجودة في هذا الخيار. متابعة؟",
        "confirm": "تطبيق",
        "cancel": "إلغاء",
        "saved": "تم تطبيق الخيار A1. تم حفظ 1 عملية ربط.",
    },
}

# The controls that are not buttons of the family (the step pills, the two
# card-sized rows, the pinned-section badge, the phone's Options toggle).
NOT_FAMILY = ".wf-step, .avail-course-head, .avail-sec-row, .sl-pin-badge, .planner-options-toggle"

VISIBLE = """const shown = el => {
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
};"""

FAMILY = (
    "(exclude) => {"
    + VISIBLE
    + r"""
  const found = [];
  const name = el => (el.id ? '#' + el.id : el.tagName.toLowerCase() + '.' + [...el.classList].join('.'))
    + ' "' + el.textContent.trim().slice(0, 18) + '"';
  const fetchBg = getComputedStyle(document.getElementById('fetchBtn')).backgroundColor;
  const buttons = [...document.querySelectorAll('#main-content button')]
    .filter(b => shown(b) && !b.matches(exclude));
  const filled = [];
  for (const b of buttons) {
    if (!b.classList.contains('btn')) found.push(name(b) + ' is not a .btn');
    const want = b.classList.contains('btn-sm') ? 32 : 38;
    const h = b.getBoundingClientRect().height;
    if (Math.abs(h - want) > 1) found.push(name(b) + ' is ' + h.toFixed(1) + 'px high, want ' + want);
    if (getComputedStyle(b).backgroundColor === fetchBg) filled.push(name(b));
  }
  for (const el of document.querySelectorAll('#main-content *, .planner-session *')) {
    for (const c of el.classList) {
      if (c.startsWith('pl-btn') || c === 'wf-back' || c === 'wf-next') found.push(name(el) + ' keeps .' + c);
    }
  }
  if (filled.length !== 2) found.push('filled primary buttons: ' + JSON.stringify(filled));
  const apply = document.getElementById('applyOption'), build = document.getElementById('runBuilder');
  if (getComputedStyle(apply).backgroundColor === getComputedStyle(build).backgroundColor)
    found.push('Apply is painted like Build');
  return { found, count: buttons.length, filled };
}"""
)

FIELDS = (
    "() => {"
    + VISIBLE
    + r"""
  const found = [];
  for (const id of ['maxCredits', 'availSearch', 'visualSource']) {
    const el = document.getElementById(id);
    const h = el.getBoundingClientRect().height;
    if (Math.abs(h - 32) > 1) found.push('#' + id + ' is ' + h.toFixed(1) + 'px high, want 32');
  }
  let boxes = 0;
  for (const c of document.querySelectorAll('#main-content input[type=checkbox]')) {
    if (!shown(c)) continue;
    boxes++;
    const a = getComputedStyle(c).appearance;
    if (a !== 'none') found.push('checkbox #' + c.id + ' is drawn by the browser (appearance ' + a + ')');
  }
  let summaries = 0;
  for (const s of document.querySelectorAll('#main-content summary')) {
    summaries++;
    const where = s.parentElement.id || s.textContent.trim().slice(0, 14);
    if (getComputedStyle(s).listStyleType !== 'none') found.push('summary "' + where + '" shows a marker');
    const chev = s.querySelector('.ux-disclosure-chevron');
    if (!chev) { found.push('summary "' + where + '" has no chevron'); continue; }
    const d = s.parentElement, was = d.open;
    d.open = true;  const open = getComputedStyle(chev).transform;
    d.open = false; const closed = getComputedStyle(chev).transform;
    d.open = was;
    if (open === closed) found.push('summary "' + where + '": the chevron does not turn (' + open + ')');
  }
  return { found, boxes, summaries };
}"""
)

INLINE_STYLES = r"""() => [...document.querySelectorAll('#main-content [style]')]
  .filter(el => !el.closest('.wg-blocks'))
  .map(el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + '.' + [...el.classList].slice(0, 2).join('.')
    + ' style="' + el.getAttribute('style') + '"')"""

TOUCH = (
    "() => {"
    + VISIBLE
    + r"""
  const found = [];
  const name = el => (el.id ? '#' + el.id : el.tagName.toLowerCase() + '.' + [...el.classList].join('.'))
    + ' "' + el.textContent.trim().slice(0, 18) + '"';
  let buttons = 0, labels = 0, summaries = 0;
  for (const b of document.querySelectorAll('#main-content .btn')) {
    if (!shown(b)) continue;
    buttons++;
    const h = b.getBoundingClientRect().height;
    if (h < 39.5) found.push(name(b) + ' is ' + h.toFixed(1) + 'px high, want 40');
  }
  for (const c of document.querySelectorAll('#main-content input[type=checkbox]')) {
    if (!shown(c)) continue;
    labels++;
    const label = c.labels && c.labels[0];
    if (!label) { found.push('checkbox #' + c.id + ' has no label to reach it through'); continue; }
    const h = label.getBoundingClientRect().height;
    if (h < 43.5) found.push('the label of #' + (c.id || c.className) + ' is ' + h.toFixed(1) + 'px high, want 44');
  }
  for (const s of document.querySelectorAll('#main-content summary')) {
    summaries++;
    const h = s.getBoundingClientRect().height;
    if (h < 43.5) found.push('summary "' + s.textContent.trim().slice(0, 16) + '" is ' + h.toFixed(1) + 'px high, want 44');
  }
  return { found, buttons, labels, summaries };
}"""
)


class PlannerControlsBrowserTests(StaticLiveServerTestCase):
    maxDiff = None

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
        user = get_user_model().objects.create_superuser(
            username="planner-controls", email="planner-controls@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.page_errors: list[str] = []
        self.build_bodies: list[dict] = []
        self.browser_boxes: list[str] = []
        self.addCleanup(self._no_page_errors)

    def _no_page_errors(self) -> None:
        self.assertEqual(self.page_errors, [], "the planner threw")

    # The shared fixture's page-opening, loading and reporting, not copies of them.
    _open = _layout.PlannerLayoutBrowserTests._open
    _assert_no_findings = _layout.PlannerLayoutBrowserTests._assert_no_findings
    _load = _lists.PlannerListsBrowserTests._load
    _build = _lists.PlannerListsBrowserTests._build

    def _loaded(self, language: str, width: int = 1440, height: int = 900):
        """The loaded planner, with any browser dialog recorded (and dismissed)."""
        page = self._load(language, width, height)
        page.on("dialog", self._record_box)
        return page

    def _record_box(self, dialog) -> None:
        self.browser_boxes.append(f"{dialog.type}: {dialog.message}")
        dialog.dismiss()

    def _built(self, language: str, width: int = 1440, height: int = 900):
        page = self._loaded(language, width, height)
        self._build(page)
        page.wait_for_function("() => !document.getElementById('applyOption').disabled")
        return page

    # ── 1. Buttons: one family ───────────────────────────────────────────────
    def test_buttons_are_one_family(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._built(language)
            seen = page.evaluate(FAMILY, NOT_FAMILY)
            found = list(seen["found"])
            if seen["count"] < 10:
                found.append(f"only {seen['count']} buttons were measured")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 2. Fields ────────────────────────────────────────────────────────────
    def test_fields_checkboxes_and_disclosures_are_the_shared_controls(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._built(language)
            seen = page.evaluate(FIELDS)
            found = list(seen["found"])
            if seen["boxes"] < 6:
                found.append(f"only {seen['boxes']} checkboxes were measured")
            if seen["summaries"] < 3:
                found.append(f"only {seen['summaries']} disclosures were measured")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 3. Inline styles ─────────────────────────────────────────────────────
    def test_no_inline_styles(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            page = self._loaded(language)
            found = [f"after load: {s}" for s in page.evaluate(INLINE_STYLES)]
            self._build(page)
            found += [f"after build: {s}" for s in page.evaluate(INLINE_STYLES)]
            page.click('#optionCards [data-opt="B1"] .opt-select')
            page.wait_for_function("() => document.getElementById('selectedOption').value === 'B1'")
            found += [f"after selecting: {s}" for s in page.evaluate(INLINE_STYLES)]
            page.fill("#availSearch", "cs")
            found += [f"after searching: {s}" for s in page.evaluate(INLINE_STYLES)]
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)

    # ── 4. The confirm box ───────────────────────────────────────────────────
    @staticmethod
    def _answer_saves(page, saves: list[dict]) -> None:
        """Answer the save endpoint with the real view's success body, noting each request."""

        def answer(route) -> None:
            saves.append(route.request.post_data_json)
            route.fulfill(json=SAVED)

        page.context.route(SAVE_URL, answer)

    def _open_apply_dialog(self, page):
        page.click("#applyOption")
        page.wait_for_selector(".dlg-backdrop.open .dlg-box", timeout=5000)
        # the dialog moves focus into itself a moment after it opens
        page.wait_for_function("() => document.activeElement.matches('.dlg-backdrop *')")
        return page.locator(".dlg-backdrop.open .dlg-box")

    def test_apply_asks_through_the_app_dialog(self) -> None:
        for language in LANGUAGES:
            with self.subTest(language=language):
                words = DIALOG_TEXT[language]
                saves: list[dict] = []
                page = self._built(language)
                self._answer_saves(page, saves)
                box = self._open_apply_dialog(page)
                self.assertEqual(box.locator(".dlg-title").inner_text().strip(), words["title"])
                body = box.locator(".dlg-body")
                text = body.inner_text()
                self.assertIn(words["intro"], text)
                self.assertIn(words["outro"], text)
                self.assertEqual(
                    [li.strip() for li in body.locator("ul li").all_inner_texts()], ["CS111 M30"]
                )
                self.assertEqual(box.locator(".btn-confirm").inner_text().strip(), words["confirm"])
                self.assertEqual(box.locator(".btn-cancel").inner_text().strip(), words["cancel"])
                page.wait_for_function("() => document.activeElement.matches('.btn-cancel')")
                # Cancel sends nothing
                box.locator(".btn-cancel").click()
                page.wait_for_selector(".dlg-backdrop", state="detached")
                page.wait_for_timeout(300)
                self.assertEqual(saves, [], "Cancel saved")
                # Escape and the backdrop cancel too
                self._open_apply_dialog(page)
                page.keyboard.press("Escape")
                page.wait_for_selector(".dlg-backdrop", state="detached")
                self._open_apply_dialog(page)
                page.mouse.click(4, 4)
                page.wait_for_selector(".dlg-backdrop", state="detached")
                page.wait_for_timeout(300)
                self.assertEqual(saves, [], "Escape or the backdrop saved")
                # Confirm sends exactly one request
                box = self._open_apply_dialog(page)
                with page.expect_response(
                    lambda response: response.url.endswith("/ops/planner/context/")
                ) as refresh:
                    box.locator(".btn-confirm").click()
                self.assertTrue(refresh.value.ok)
                page.wait_for_function("() => !document.getElementById('fetchBtn').disabled")
                self.assertEqual(len(saves), 1, "Confirm must send exactly one request")
                self.assertIs(saves[0]["confirm_replace"], True)
                self.assertEqual(saves[0]["term_section_ids"], [2])
                playwright_api.expect(page.locator("#statusBanner")).to_have_text(words["saved"])
                self.assertTrue(page.is_visible("#builderCard"))
                self.assertTrue(page.is_visible("#statusBanner"))
                self.assertEqual(self.browser_boxes, [], "a browser dialog opened")
                page.context.close()

    # ── 5. Touch sizes ───────────────────────────────────────────────────────
    def test_touch_sizes(self) -> None:
        problems: dict[str, list[str]] = {}
        for language in LANGUAGES:
            real = self.browser.new_context
            self.browser.new_context = lambda real=real, **kw: real(
                **kw, has_touch=True, is_mobile=True
            )
            try:
                page = self._built(language, 390, 844)
            finally:
                self.browser.new_context = real
            page.evaluate("document.querySelector('.planner-options-toggle').click()")
            page.evaluate(
                "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
            )
            seen = page.evaluate(TOUCH)
            found = list(seen["found"])
            if seen["buttons"] < 10 or seen["labels"] < 6 or seen["summaries"] < 3:
                found.append(f"too little was measured: {seen}")
            if found:
                problems[language] = found
            page.context.close()
        self._assert_no_findings(problems)
