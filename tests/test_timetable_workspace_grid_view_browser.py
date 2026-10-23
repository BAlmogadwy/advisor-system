"""The Timetable Workspace's filter in a real browser: it folds and dims.

docs/TIMETABLE-WORKSPACE-UI.md §7.2 item 6, 6c. Typing in the board's filter
never draws the board again: a table the text matches nothing in folds to one
row (the global disclosure toggle, the table's one keyboard stop), and the
other cards the text misses are dimmed — the course hue mixed down, the
theme's secondary ink, a lighter code; never opacity and never dashed (the
same-course echo is the dashed outline).

FB1 (1366×650 and 1440×790, light and dark, English and Arabic, the wide
font): a dimmed card's code and room read at 4.5:1 and a dimmed critical
card's ring at 3:1 against the dimmed card; the fold's label is at least
12px, reads at 4.5:1 and is not faded by opacity; a dimmed card is not
drawn like an echoed one; the frame keeps its acceptance (no sideways
scroll, nothing under 11px).

FB2: from the keyboard alone, ↓ reaches a folded table's toggle, Enter opens
it and ↓ goes into the table; Chrome exposes the toggle as an expandable
button inside the grid.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402

from core.models import SectionPlacement  # noqa: E402
from tests.test_timetable_workspace_frame_browser import CONTRAST, FRAME  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

# What is wrong with the dimmed cards and the fold, as short findings.
DIMMED = r"""() => {
  const bad = [];
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
  const ratio = (fg, bg) => { const a = lum(over(fg, bg)), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  const dimmed = [...document.querySelectorAll('#twGrid .tw-card[data-dim]')];
  if (!dimmed.length) bad.push('no dimmed card');
  for (const card of dimmed) {
    const style = getComputedStyle(card);
    if (parseFloat(style.opacity) < 1) bad.push(`dimmed by opacity ${style.opacity}`);
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (style[`border${side}Style`] === 'dashed') bad.push(`dashed ${side} border`);
    }
    if (style.outlineStyle === 'dashed') bad.push('dashed outline: reads as the same-course echo');
    // The same card without its dim: the fill, the ink and the code's weight all change.
    const code = card.querySelector('.tw-card-code');
    const drawn = [style.backgroundColor, getComputedStyle(code).color, getComputedStyle(code).fontWeight];
    const dim = card.getAttribute('data-dim');
    card.removeAttribute('data-dim');
    const undimmed = [getComputedStyle(card).backgroundColor, getComputedStyle(code).color, getComputedStyle(code).fontWeight];
    card.setAttribute('data-dim', dim);
    ['fill', 'code ink', 'code weight'].forEach((what, i) => {
      if (drawn[i] === undimmed[i]) bad.push(`dimmed ${what} is the undimmed one (${drawn[i]})`);
    });
    if (Number(drawn[2]) >= Number(undimmed[2])) bad.push(`dimmed code weight ${drawn[2]} not lighter than ${undimmed[2]}`);
    const ground = behind(card);
    // The severity ring: an inset box-shadow in the tone's colour.
    if (card.classList.contains('critical') || card.classList.contains('warning')) {
      const ring = /rgba?\([^)]*\)/.exec(style.boxShadow);
      if (!ring) bad.push('a dimmed toned card lost its ring');
      else if (ratio(rgba(ring[0]), ground) < 3) bad.push(`ring ${ratio(rgba(ring[0]), ground).toFixed(2)}:1`);
    }
  }
  if (!dimmed.some(card => card.classList.contains('critical'))) bad.push('no dimmed critical card to measure');
  const label = document.querySelector('#twGrid .tw-fold-row:not([hidden]) .ux-disclosure-label');
  if (!label) bad.push('no fold shown');
  else {
    if (parseFloat(getComputedStyle(label).fontSize) < 11.95) bad.push(`fold label ${getComputedStyle(label).fontSize}`);
    // Faded by opacity (its own or a box around it), the label still passes
    // the contrast helper, which reads colours, not opacity.
    let opacity = 1;
    for (let e = label; e; e = e.parentElement) opacity *= parseFloat(getComputedStyle(e).opacity);
    if (opacity < 1) bad.push(`fold label at opacity ${opacity}`);
  }
  if (document.documentElement.scrollWidth > innerWidth + 0.5) bad.push('page scrolls sideways');
  return bad;
}"""


class WorkspaceGridViewBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        # AI222 has no room: a critical card the filter will dim.
        SectionPlacement.objects.filter(term_section__course_code="AI222").update(room="UNASSIGNED")

    def _filter(self, page, text: str) -> None:
        page.fill("#twSearch", text)
        page.wait_for_function(
            "document.querySelectorAll('#twGrid .tw-card[data-dim]').length === 2"
            " && document.querySelector('#twGrid .tw-fold-row:not([hidden])')"
        )

    def test_fb1_dimmed_cards_and_the_fold_read_and_are_not_the_echo(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height)
                    self._filter(page, "AI111")
                    # The echo: AI111 selected, its other meeting echoed.
                    page.click("#twGrid .tw-card:not([data-dim])")
                    found = page.evaluate(DIMMED)
                    found += page.evaluate(
                        CONTRAST,
                        [
                            ".tw-card[data-dim] .tw-card-code",
                            ".tw-card[data-dim] .tw-card-room",
                            ".tw-fold-row:not([hidden]) .ux-disclosure-label",
                        ],
                    )
                    found += page.evaluate(FRAME, 1440)
                    echo = page.evaluate(
                        "getComputedStyle(document.querySelector('#twGrid .tw-card.is-same-course')).outlineStyle"
                    )
                    if echo != "dashed":
                        found.append(f"the echo is {echo}, not dashed")
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})

    def test_fb2_the_keyboard_opens_a_folded_table_and_goes_into_it(self) -> None:
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1440, 790)
            self._filter(page, "AI111")
            page.focus("#twLeft .tw-rail")
            page.keyboard.press("Tab")
            self.assertTrue(
                page.evaluate("document.getElementById('twGrid').contains(document.activeElement)"),
                language,
            )
            for _ in range(10):
                if page.evaluate("document.activeElement.matches('.tw-fold-toggle')"):
                    break
                page.keyboard.press("ArrowDown")
            toggle = page.evaluate(
                "({ fold: document.activeElement.matches('.tw-fold-toggle'),"
                " expanded: document.activeElement.getAttribute('aria-expanded'),"
                " visible: document.activeElement.matches(':focus-visible'),"
                " table: document.activeElement.closest('[role=grid]').dataset.kind })"
            )
            self.assertEqual(
                toggle,
                {"fold": True, "expanded": "false", "visible": True, "table": "lab"},
                language,
            )
            # Chrome: an expandable (collapsed) button inside the Labs grid.
            cdp = page.context.new_cdp_session(page)
            cdp.send("Accessibility.enable")
            nodes = {n["nodeId"]: n for n in cdp.send("Accessibility.getFullAXTree")["nodes"]}
            role = lambda n: n.get("role", {}).get("value", "")  # noqa: E731
            name = lambda n: str(n.get("name", {}).get("value", ""))  # noqa: E731
            expanded = lambda n: next(  # noqa: E731
                (p["value"]["value"] for p in n.get("properties", []) if p["name"] == "expanded"),
                None,
            )
            buttons = [
                n
                for n in nodes.values()
                if not n.get("ignored")
                and role(n) == "button"
                and expanded(n) is False
                and ("no match" in name(n) or "لا شيء مطابق" in name(n))
            ]
            self.assertEqual(len(buttons), 1, [name(n) for n in buttons])
            chain, node = [], buttons[0]
            while node.get("parentId") in nodes:
                node = nodes[node["parentId"]]
                if not node.get("ignored"):
                    chain.append(role(node))
            self.assertLess(chain.index("gridcell"), chain.index("row"), chain)
            self.assertLess(chain.index("row"), chain.index("grid"), chain)
            page.keyboard.press("Enter")
            page.wait_for_function(
                "document.activeElement.getAttribute('aria-expanded') === 'true'"
            )
            # The rows just shown are the board's stops at once (its roving
            # read them again on the disclosure's event), before any key.
            self.assertEqual(
                page.evaluate(
                    "[...document.activeElement.closest('[role=grid]').querySelectorAll('.tw-fold-body .tw-cell')]"
                    ".map(cell => cell.querySelector('.tw-card') || cell)"
                    ".filter(stop => stop.getAttribute('tabindex') !== '-1').length"
                ),
                0,
                language,
            )
            page.keyboard.press("ArrowDown")
            inside = page.evaluate(
                "({ kind: document.activeElement.closest('[role=grid]').dataset.kind,"
                " key: document.activeElement.dataset.focusKey || '',"
                " visible: document.activeElement.matches(':focus-visible') })"
            )
            self.assertEqual(inside["kind"], "lab", f"{language}: {inside}")
            self.assertTrue(inside["key"].startswith("cell:S1:lab|SUN|"), f"{language}: {inside}")
            self.assertTrue(inside["visible"], language)
            # One tab stop still: Tab leaves the board.
            page.keyboard.press("Tab")
            self.assertFalse(
                page.evaluate("document.getElementById('twGrid').contains(document.activeElement)"),
                language,
            )
            self.assertEqual([*sent, *errors], [], language)
            page.context.close()
