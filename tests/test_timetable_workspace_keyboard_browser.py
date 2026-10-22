"""The Timetable Workspace's board and list from the keyboard, in a real browser.

docs/TIMETABLE-WORKSPACE-UI.md §7.3, 5a. The board is an ARIA grid with one tab
stop (UXControls.roving): Tab reaches it, arrows move by time and day (mirrored
in Arabic), a redraw keeps the keyboard on the same section without scrolling a
mouse user's view, Chrome exposes the grid to assistive technology, the pane
keys work by key position (an Arabic layout types ج and د there), and a card the
keyboard moves to is never under the sticky header or the floating job card.
"""

from __future__ import annotations

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

pytest.importorskip("playwright.sync_api")

ACTIVE = """() => {
  const a = document.activeElement;
  const cell = a && (a.closest('[role="gridcell"]') || a);
  return { inGrid: document.getElementById('twGrid').contains(a), zero: a.getAttribute('tabindex'),
           visible: a.matches(':focus-visible'), key: a.dataset.focusKey || '', start: cell.dataset.start || '',
           x: a.getBoundingClientRect().left };
}"""


class WorkspaceKeyboardBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def _into_board(self, page) -> dict:
        # Below 1600px the list starts collapsed to its rail; Tab from the rail
        # is the board's one stop.
        page.focus("#twLeft .tw-rail")
        page.keyboard.press("Tab")
        return page.evaluate(ACTIVE)

    def test_tab_reaches_the_board_once_and_arrows_move_by_time_in_both_directions(self) -> None:
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1440, 790)
            first = self._into_board(page)
            self.assertTrue(first["inGrid"], f"{language}: {first}")
            self.assertEqual((first["zero"], first["visible"]), ("0", True), language)
            page.keyboard.press("ArrowLeft" if language == "ar" else "ArrowRight")
            moved = page.evaluate(ACTIVE)
            self.assertGreater(moved["start"], first["start"], f"{language}: the next time")
            if language == "ar":
                self.assertLess(moved["x"], first["x"], "Arabic: the next time is to the left")
            else:
                self.assertGreater(moved["x"], first["x"])
            page.keyboard.press("Tab")
            self.assertFalse(page.evaluate(ACTIVE)["inGrid"], f"{language}: one stop, Tab leaves")
            page.keyboard.press("Shift+Tab")
            back = page.evaluate(ACTIVE)
            self.assertEqual(back["key"], moved["key"], f"{language}: back to the same stop")
            self.assertEqual(errors, [])

    def test_chrome_exposes_the_board_and_the_list_as_grids(self) -> None:
        page, sent, errors = self._load("en", "light", 1920, 1080)
        cdp = page.context.new_cdp_session(page)
        cdp.send("Accessibility.enable")
        nodes = [
            node
            for node in cdp.send("Accessibility.getFullAXTree")["nodes"]
            if not node.get("ignored")
        ]
        role = lambda node: node.get("role", {}).get("value", "")  # noqa: E731
        name = lambda node: str(node.get("name", {}).get("value", ""))  # noqa: E731
        grids = [name(node) for node in nodes if role(node) == "grid"]
        self.assertIn("Lectures", grids)
        # The list's grids are named by their term heading (shown upper-case).
        self.assertTrue(any(g.lower().startswith("term") for g in grids), grids)
        self.assertGreaterEqual(sum(1 for node in nodes if role(node) == "row"), 6)
        self.assertTrue(
            any(role(n) == "columnheader" and name(n).startswith("Slot 1, ") for n in nodes)
        )
        self.assertTrue(any(role(n) == "rowheader" and name(n) == "Sunday" for n in nodes))
        self.assertTrue(any(role(n) == "gridcell" for n in nodes))
        self.assertTrue(any(role(n) == "button" and "AI111" in name(n) for n in nodes))
        self.assertEqual(errors, [])

    def test_a_redraw_keeps_the_keyboard_and_never_scrolls_a_mouse_users_view(self) -> None:
        page, sent, errors = self._load("en", "light", 1440, 790)
        self._into_board(page)
        page.keyboard.press("ArrowRight")
        before = page.evaluate(
            "(() => { document.activeElement.__old = true; return document.activeElement.dataset.focusKey; })()"
        )
        page.evaluate("selectBoard(TW.boardId)")
        page.wait_for_function(
            "document.activeElement && !document.activeElement.__old && document.getElementById('twGrid').contains(document.activeElement)"
        )
        after = page.evaluate(ACTIVE)
        self.assertEqual(
            (after["key"], after["visible"]), (before, True), "the same stop, a new node"
        )
        # A mouse user: a click, a scroll, a redraw — the view stays put.
        page.click(".tw-card")
        centre = "document.getElementById('twCentre')"
        page.evaluate(f"{centre}.scrollTop = {centre}.scrollHeight")
        top = page.evaluate(f"{centre}.scrollTop")
        self.assertGreater(top, 0, "the board scrolls at this size")
        page.evaluate("renderGrid()")
        self.assertEqual(
            page.evaluate(f"{centre}.scrollTop"), top, "a passive restore never scrolls"
        )
        selected = page.evaluate(
            """() => { const card = document.querySelector('.tw-card.is-selected'); const s = getComputedStyle(card);
              const probe = document.createElement('span'); probe.style.color = 'var(--t0)'; document.body.appendChild(probe);
              const t0 = getComputedStyle(probe).color; probe.remove();
              return [s.outlineStyle, s.outlineColor === t0]; }"""
        )
        self.assertEqual(selected, ["solid", True], "a clicked, selected card keeps its outline")
        self.assertEqual(errors, [])

    def test_the_pane_keys_work_by_key_position_on_an_arabic_layout(self) -> None:
        page, sent, errors = self._load("ar", "light", 1440, 790)
        page.evaluate("document.activeElement && document.activeElement.blur()")
        cdp = page.context.new_cdp_session(page)
        workspace = "document.getElementById('twWorkspace').dataset"
        for key, code, vk, side in (
            ("ج", "BracketLeft", 219, "left"),
            ("د", "BracketRight", 221, "right"),
        ):
            before = page.evaluate(f"{workspace}.{side}")
            for kind in ("keyDown", "keyUp"):
                cdp.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": kind,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                        "text": key if kind == "keyDown" else "",
                    },
                )
            self.assertNotEqual(page.evaluate(f"{workspace}.{side}"), before, f"{key} on {code}")
        self.assertEqual(errors, [])

    def test_a_card_the_keyboard_moves_to_is_never_under_the_header_or_the_job_card(self) -> None:
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1366, 650)
            # ↑ from Monday to Sunday, with Sunday's row 20px under the sticky header.
            seen = page.evaluate(
                """async () => {
                  const centre = document.getElementById('twCentre');
                  const table = document.querySelector('#twGrid [role="grid"]');
                  const header = table.querySelector('.tw-grid-header').getBoundingClientRect();
                  const sun = table.querySelector('[role="gridcell"][data-day="SUN"]');
                  const mon = table.querySelector('[role="gridcell"][data-day="MON"]');
                  const monStop = mon.querySelector('.tw-card') || mon;
                  centre.scrollTop += sun.getBoundingClientRect().top - (header.bottom - 20);
                  monStop.focus({ preventScroll: true });
                  return { header: header.height };
                }"""
            )
            self.assertLessEqual(
                seen["header"], 52, f"{language}: the header the padding allows for"
            )
            page.keyboard.press("ArrowUp")
            check = page.evaluate(
                """() => {
                  const a = document.activeElement, r = a.getBoundingClientRect();
                  const header = a.closest('[role="grid"]').querySelector('.tw-grid-header').getBoundingClientRect();
                  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                  return { day: (a.closest('[role="gridcell"]') || a).dataset.day, clear: r.top >= header.bottom - 0.5, seen: a.contains(hit) };
                }"""
            )
            self.assertEqual(check, {"day": "SUN", "clear": True, "seen": True}, language)
            # ↓ to a stop near the bottom while the job card floats over it.
            page.evaluate(
                """() => { const card = document.querySelector('.ux-job'); card.hidden = false; card.style.minHeight = '260px'; }"""
            )
            page.wait_for_function(
                "getComputedStyle(document.documentElement).getPropertyValue('--ux-job-block').trim() !== '0px'"
            )
            under = page.evaluate(
                """() => {
                  const table = document.querySelector('#twGrid [role="grid"]');
                  const job = document.querySelector('.ux-job').getBoundingClientRect();
                  // The column the card floats over (it sits at the window's end corner).
                  const across = cell => { const r = cell.getBoundingClientRect(); return r.right > job.left + 4 && r.left < job.right - 4; };
                  const wed = [...table.querySelectorAll('[role="gridcell"][data-day="WED"]')].find(across);
                  const thuCell = [...table.querySelectorAll('[role="gridcell"][data-day="THU"]')].find(across);
                  (wed.querySelector('.tw-card') || wed).focus({ preventScroll: true });
                  const centre = document.getElementById('twCentre');
                  // Thursday's row 30px under the job card's top edge.
                  centre.scrollTop -= job.top + 30 - thuCell.getBoundingClientRect().bottom;
                  const thu = thuCell.getBoundingClientRect();
                  return { under: thu.bottom > job.top + 10, overlapX: thu.right > job.left && thu.left < job.right };
                }"""
            )
            self.assertEqual(
                under, {"under": True, "overlapX": True}, f"{language}: the test's own setup"
            )
            page.keyboard.press("ArrowDown")
            below = page.evaluate(
                """() => { const a = document.activeElement, r = a.getBoundingClientRect(), job = document.querySelector('.ux-job').getBoundingClientRect();
                  return { day: (a.closest('[role="gridcell"]') || a).dataset.day, clear: r.bottom <= job.top + 0.5 }; }"""
            )
            self.assertEqual(below, {"day": "THU", "clear": True}, language)
            self.assertEqual(errors, [])
