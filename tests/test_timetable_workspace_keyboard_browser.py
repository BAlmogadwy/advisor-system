"""The Timetable Workspace's board and list from the keyboard, in a real browser.

docs/TIMETABLE-WORKSPACE-UI.md §7.3, 5a. The board is an ARIA grid with one tab
stop (UXControls.roving): Tab reaches it, arrows move by time and day (mirrored
in Arabic), a redraw keeps the keyboard on the same section without scrolling a
mouse user's view, Chrome exposes the grid to assistive technology, the pane
keys work by key position (an Arabic layout types ج and د there), and a card the
keyboard moves to is never under the sticky header or the floating job card.

§7.3, 5c (RoomDialogTests): Change room from the keyboard, from Selection's
button and by R on a card, keeps the time lock and gives the keyboard back to
whatever opened it; a room in use can be chosen and its clash is shown; a room
outside the board's programme rooms is the "Room unsuitable" warning Issues
then show, before the save and after it; the dialog reads in both themes and
languages, keeps its foot in view while its list scrolls, and follows the
arrow keys down the list in either direction. Since 5c-audit the dialog judges
the rooms by the quality audit: a room short of the audit's seats is the
"Room too small" warning Issues then show (the owner's answer to Q1).

§7.3, 5e (MoveDialogTests): Move from Selection's Move… and by M on a card:
the times the server offers, in the order of its verdict, read in both
themes and languages with their times left to right; a time chosen by its
accessible name moves the meeting by its time alone, and the card, toned by
what the move saved, keeps the keyboard; Undo moves it back; a time-locked
meeting reads nothing.

§7.3, 5f (PlaceDialogTests): Place… from the keyboard alone (Tab to the
list, ↓ to AI222 S2, Enter, End, Home, Enter) places the section with both
its meetings in one request, and the keyboard stays in the list; a drag onto
Thursday 13:00 opens the dialog on that time and sends nothing; the dialog
reads in both themes and languages, keeps its foot in view while its list
scrolls, and paints every time left to right in Arabic.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402

from tests import one_teacher_support as fx  # noqa: E402
from tests.test_timetable_workspace_frame_browser import CONTRAST  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

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
            self.assertEqual([*sent, *errors], [])

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
        # A card, named with its own day and time (not an Issues row).
        self.assertTrue(
            any(role(n) == "button" and "AI111" in name(n) and "Sunday" in name(n) for n in nodes),
            [name(n) for n in nodes if role(n) == "button" and "AI111" in name(n)],
        )
        self.assertEqual([*sent, *errors], [])

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
        centre = "document.getElementById('twGridWrap')"
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
        self.assertEqual([*sent, *errors], [])

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
        self.assertEqual([*sent, *errors], [])

    def test_a_card_the_keyboard_moves_to_is_never_under_the_header_or_the_job_card(self) -> None:
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1366, 650)
            # ↑ from Monday to Sunday, with Sunday's row 20px under the sticky header.
            seen = page.evaluate(
                """async () => {
                  const centre = document.getElementById('twGridWrap');
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
                  const centre = document.getElementById('twGridWrap');
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
            self.assertEqual([*sent, *errors], [])

    def test_a_list_collapsed_at_load_gets_its_one_stop_when_it_opens(self) -> None:
        # Below 1600px the list starts collapsed: its first drawing has no box.
        page, sent, errors = self._load("en", "light", 1440, 790)
        self.assertEqual(
            page.evaluate("document.getElementById('twWorkspace').dataset.left"), "collapsed"
        )
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.press("BracketLeft")
        page.wait_for_function("document.getElementById('twWorkspace').dataset.left === 'open'")
        page.wait_for_function(
            "document.querySelectorAll('#twSectionList [tabindex=\"0\"]').length === 1"
        )
        page.focus("#twSectionSearch")
        page.keyboard.press("Tab")
        landed = page.evaluate(
            "[document.activeElement.getAttribute('tabindex'), document.getElementById('twSectionList').contains(document.activeElement)]"
        )
        self.assertEqual(landed, ["0", True])
        self.assertEqual([*sent, *errors], [])

    def test_focus_keeps_the_severity_ring_and_the_selection_and_cells_show_an_inset_ring(
        self,
    ) -> None:
        from core.models import DeliveryBoard

        board = DeliveryBoard.objects.get(scenario=self.scenario)
        # A section with no room: a critical card.
        key = fx.requirement("AI", "MATH101", "Calculus")
        fx.budget(self.scenario, key, 1)
        fx.place(
            board,
            fx.section(self.scenario, key, "S1"),
            (("SUN", "10:30", "11:45"), ("TUE", "10:30", "11:45")),
            room="UNASSIGNED",
        )
        for theme in ("light", "dark"):
            page, sent, errors = self._load("en", theme, 1440, 790, cards=6)
            seen = page.evaluate(
                """() => {
                  const probe = document.createElement('span'); document.body.appendChild(probe);
                  const colour = value => { probe.style.color = value; return getComputedStyle(probe).color; };
                  const critical = colour('var(--sev-critical)'), t0 = colour('var(--t0)');
                  const toned = document.querySelector('.tw-card.critical');
                  toned.focus();
                  const focused = getComputedStyle(toned).boxShadow.includes(critical);
                  toned.blur();
                  // A day cell (a table's first gridcell is its fold, hidden while unfiltered).
                  const cell = document.querySelector('#twGrid .tw-cell[role="gridcell"]:not(:has(.tw-card))');
                  cell.focus();
                  const inset = getComputedStyle(cell).outlineOffset;
                  probe.remove();
                  return { focused, inset, critical, t0 };
                }"""
            )
            self.assertEqual((seen["focused"], seen["inset"]), (True, "-2px"), f"{theme}: {seen}")
            # A pointer over a toned card keeps its ring (dark mode lost it on hover).
            page.hover(".tw-card.critical")
            self.assertIn(
                seen["critical"],
                page.evaluate(
                    "getComputedStyle(document.querySelector('.tw-card.critical')).boxShadow"
                ),
                theme,
            )
            # Selected and focused: the selection's ring inside the focus ring.
            page.click(".tw-card.critical")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowLeft")
            both = page.evaluate(
                "(() => { const s = getComputedStyle(document.activeElement); return [document.activeElement.classList.contains('is-selected'), s.outlineOffset, s.boxShadow]; })()"
            )
            self.assertEqual(both[:2], [True, "4px"], theme)
            self.assertIn(seen["t0"], both[2], theme)
            self.assertEqual([*sent, *errors], [])

    def test_the_keys_line_sits_below_the_board_never_over_it_and_never_scrolled_away(self) -> None:
        page, sent, errors = self._load("en", "light", 1366, 650)
        self._into_board(page)
        page.keyboard.press("ArrowRight")
        page.evaluate("document.getElementById('twGridWrap').scrollLeft = 200")
        seen = page.evaluate(
            """() => {
              const keys = document.getElementById('twGridKeys');
              const wrap = document.getElementById('twGridWrap').getBoundingClientRect();
              const centre = document.getElementById('twCentre').getBoundingClientRect();
              const line = keys.getBoundingClientRect();
              return { shown: !keys.hidden, below: line.top >= wrap.bottom - 0.5, left: Math.abs(line.left - centre.left) < 1.5,
                       inside: line.bottom <= centre.bottom + 0.5 };
            }"""
        )
        self.assertEqual(seen, {"shown": True, "below": True, "left": True, "inside": True})
        self.assertEqual([*sent, *errors], [])


CHOSEN = "[document.activeElement?.name === 'twRoomChoice' && document.activeElement.value, document.activeElement?.checked]"
ON_BUTTON = "document.activeElement?.matches('#twRightBody [data-tw-action=\"room\"]')"
# Text in the dialog under 11px (each element whose own text is shown).
SMALL_TEXT = r"""() => [...document.querySelectorAll('#twRoomDialog *')].filter(el => el.getClientRects().length
  && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
  && parseFloat(getComputedStyle(el).fontSize) < 11).map(el => `${el.tagName}.${el.className} ${getComputedStyle(el).fontSize}`)"""
# With the list scrolled to its end, the title and Assign are still on screen
# and not covered; the dialog itself never scrolls (its body does).
FOOT_IN_VIEW = r"""() => {
  const dialog = document.getElementById('twRoomDialog'), body = document.getElementById('twRoomBody');
  if (body.scrollHeight <= body.clientHeight + 1) return ['the list does not scroll at this size: the check proves nothing'];
  body.scrollTop = body.scrollHeight;
  const bad = [];
  const seen = el => { const r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= innerHeight && el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); };
  if (!seen(document.getElementById('twRoomAssign'))) bad.push('Assign scrolled away or is covered');
  if (!seen(document.getElementById('twRoomTitle'))) bad.push('the title scrolled away or is covered');
  if (dialog.scrollTop !== 0) bad.push('the dialog itself scrolled');
  if (body.scrollTop === 0) bad.push('the list did not scroll');
  return bad;
}"""
# The page's own words and functions (top-level names of its script).
SHOW_CLASH_NOTE = """() => { ROOM.answer.candidates[0].validation = { critical_count: 2, warning_count: 0, room_clashes: [] };
  drawRoomDialog(); }"""
# The quality audit's notes about the meeting (5c-audit), drawn as the dialog
# draws them, in each tone a note takes: its mark is the tone's colour.
NOTES_IN_TONE = r"""() => {
  const clash = document.querySelector('#twRoomBody .tw-room-note');
  const bad = [];
  for (const tone of ['warning', 'info']) {
    const note = document.createElement('p');
    note.className = 'tw-room-note';
    note.dataset.tone = tone;
    note.textContent = tone === 'info' ? 'This course has no budget row, so the quality check assumes 40 seats.' : 'GEN T1 has no programme.';
    clash.after(note);
    const probe = document.createElement('span');
    probe.style.color = `var(--sev-${tone})`;
    note.after(probe);
    if (getComputedStyle(note).borderInlineStartColor !== getComputedStyle(probe).color) bad.push(`the ${tone} note is not marked in its tone`);
    probe.remove();
  }
  return bad;
}"""
SAY_IN_ALERT = """tone => dialogSay(document.getElementById('twRoomDialog'),
  tone === 'success' ? ROOM_WORDS.assigned(isolatedText('R101')) : REFUSAL.external, tone)"""
# The alert takes its tone's colour: a refusal a warning, a saved change a success.
ALERT_IN_TONE = r"""tone => {
  const alert = document.getElementById('twRoomAlert');
  const probe = document.createElement('span');
  probe.style.color = `var(--sev-${tone === 'success' ? 'ok' : tone})`;
  alert.after(probe);
  const same = getComputedStyle(probe).color === getComputedStyle(alert).color;
  probe.remove();
  return same && !alert.hidden ? [] : [`the ${tone} alert is not shown in its tone`];
}"""


class RoomDialogTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """Change room (§7.3, 5c) in a real browser. These write, to pytest's
    database. AI111 S1 meets on Sunday 09:00 in R100, its time locked; R101 is
    another free lecture room, R102 is held at that time on the same board (by
    AI333), R103 is a lab and R104 has 10 seats for a section of 30 (22 by the
    quality audit's buffered budget: 20 students, +10%). No room has a
    department, so the audit holds each outside the board's programme rooms
    (AI) until a test gives them one."""

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        from core.models import (
            DeliveryBoard,
            Room,
            SectionPlacement,
            TermSection,
            TermSectionMeeting,
        )

        for code, kind, seats in (
            ("R100", "lecture", 40),
            ("R101", "lecture", 40),
            ("R102", "lecture", 40),
            ("R103", "lab", 40),
            ("R104", "lecture", 10),
        ):
            Room.objects.create(room_code=code, room_type=kind, capacity=seats, section="M")
        self.ai111 = TermSection.objects.get(scenario=self.scenario, course_code="AI111")
        self.ai111.available_capacity = 30
        self.ai111.save(update_fields=["available_capacity"])
        board = DeliveryBoard.objects.get(scenario=self.scenario)
        key = fx.requirement("AI", "AI333", "Planning")
        fx.budget(self.scenario, key, 1)
        fx.place(
            board, fx.section(self.scenario, key, "S1"), (("SUN", "09:00", "10:15"),), room="R102"
        )
        self.placements, self.meetings = SectionPlacement.objects, TermSectionMeeting.objects
        self.sunday = self.placements.get(term_section=self.ai111, day="SUN")
        self.card = f'#twGrid [data-focus-key="placement:{self.sunday.id}"]'
        self._reset()

    def _reset(self) -> None:
        self.placements.filter(term_section=self.ai111).update(room="R100", is_locked=True)
        self.meetings.filter(term_section=self.ai111).update(room="R100")

    def _saved(self, page, room: str) -> None:
        # Closed, the board read again with the new room, and the page at rest.
        page.wait_for_function(
            """([id, room]) => !document.getElementById('twRoomDialog').open
              && TW.placements.some(p => p.id === id && p.room === room)
              && document.querySelector('.tw-workspace').dataset.busy === 'false'""",
            arg=[self.sunday.id, room],
        )

    def _tab_to(self, page, condition: str, key: str = "Tab") -> None:
        for _ in range(20):
            if page.evaluate(condition):
                return
            page.keyboard.press(key)
        self.fail(f"{key} never reached {condition}")

    def test_change_room_from_the_keyboard_keeps_the_lock_and_gives_the_keyboard_back(
        self,
    ) -> None:
        for language in ("en", "ar"):
            self._reset()
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            page.evaluate(
                "window.__closes = []; document.addEventListener('ux-dialog-close', e => __closes.push(e.detail.reason))"
            )
            # Selected from the keyboard, then Tab to Selection's Change….
            page.focus(self.card)
            page.keyboard.press("Enter")
            page.wait_for_selector('#twRightBody [data-tw-action="room"]')
            self._tab_to(page, ON_BUTTON)
            page.keyboard.press("Enter")
            page.wait_for_function("document.activeElement?.name === 'twRoomChoice'")
            self.assertEqual(page.evaluate(CHOSEN), ["R100", True], f"{language}: the current room")
            page.keyboard.press("ArrowDown")
            self.assertEqual(page.evaluate(CHOSEN), ["R101", True], language)
            page.keyboard.press("Enter")
            self._saved(page, "R101")
            page.wait_for_function(ON_BUTTON)
            row = self.placements.get(pk=self.sunday.pk)
            self.assertEqual(
                (row.room, row.is_locked, row.day, row.start_time, row.end_time),
                ("R101", True, "SUN", "09:00", "10:15"),
                f"{language}: the room alone changed; the lock holds",
            )
            # Escape on a second open, with Selection drawn again meanwhile.
            page.keyboard.press("Enter")
            page.wait_for_function("document.activeElement?.name === 'twRoomChoice'")
            page.evaluate(
                f"TW.placements.find(p => p.id === {self.sunday.id}).registered_count = 7; renderRightPanel('selection')"
            )
            page.keyboard.press("Escape")
            page.wait_for_function("!document.getElementById('twRoomDialog').open")
            page.wait_for_function(ON_BUTTON)
            self.assertEqual(page.evaluate("__closes.at(-1)"), "cancel", language)
            # R on the card, with the board drawn again while the dialog is open.
            self._tab_to(page, f"document.activeElement?.matches('{self.card}')", "Shift+Tab")
            page.keyboard.press("r")
            page.wait_for_function("document.activeElement?.name === 'twRoomChoice'")
            page.evaluate("renderGrid()")
            self.assertEqual(page.evaluate(CHOSEN), ["R101", True], language)
            page.keyboard.press("ArrowUp")
            self.assertEqual(page.evaluate(CHOSEN), ["R100", True], language)
            page.keyboard.press("Enter")
            self._saved(page, "R100")
            page.wait_for_function(f"document.activeElement?.matches('{self.card}')")
            self.assertEqual(self.placements.get(pk=self.sunday.pk).room, "R100", language)
            move = f"/ops/tw/placements/{self.sunday.id}/move/"
            self.assertEqual(
                [r for r in sent if not r.endswith(move)], [], f"{language}: only the room changes"
            )
            self.assertEqual(len(sent), 2, language)
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_a_room_in_use_can_be_chosen_and_the_clash_it_saves_is_shown(self) -> None:
        page, sent, errors = self._load("en", "light", 1440, 790, cards=5)
        page.click(self.card)
        page.click('#twRightBody [data-tw-action="room"]')
        page.wait_for_selector('#twRoomBody input[value="R102"]', state="attached")
        self.assertEqual(
            page.evaluate(
                "document.querySelector('#twRoomBody input[value=\"R102\"]').closest('.ux-choice').dataset.tone"
            ),
            "critical",
        )
        # R103 (a lab) is listed with its reason, not offered. Judged by the
        # quality audit (5c-audit), R104 (10 seats for a need of 22) is a
        # warning that can be chosen (the owner's answer to Q1): with the
        # rooms that lower quality, after the free ones and before those in use.
        self.assertEqual(
            page.evaluate(
                "[...document.querySelectorAll('#twRoomBody input[name=\"twRoomChoice\"]')].map(r => r.value)"
            ),
            ["R100", "R101", "R104", "R102"],
        )
        self.assertEqual(
            page.evaluate(
                "[...document.querySelectorAll('#twRoomMisfits .ux-choice-static .ux-choice-title bdi')].map(b => b.textContent)"
            ),
            ["R103"],
        )
        page.click('#twRoomBody label:has(input[value="R102"])')
        page.click("#twRoomAssign")
        self._saved(page, "R102")
        self.assertEqual(self.placements.get(pk=self.sunday.pk).room, "R102")
        page.click("#twTabIssues")
        page.wait_for_function(
            "document.getElementById('twRightBody').textContent.includes('Room double-booked')"
        )
        page.wait_for_function(
            f"document.querySelector('{self.card}').classList.contains('critical')"
        )
        # The toast names who else uses the room (codes are isolated in it).
        toast = (
            page.inner_text("#toast-container").replace(chr(0x2066), "").replace(chr(0x2069), "")
        )
        self.assertIn("Room R102 assigned; AI333-S1 also uses it at this time.", toast)
        self.assertEqual(errors, [])

    def test_a_room_outside_the_boards_programmes_is_the_warning_issues_then_show(self) -> None:
        """Live QA of 5c: a free room outside the board's programme rooms was
        offered as "For context", and Issues then listed it as "Room
        unsuitable", a warning that rang the card. The dialog now says what
        Issues will say, and so does the report after the save. Since
        5c-audit the row is in the group of rooms that lower quality, and the
        report names the row Issues will list."""
        from core.models import Room

        # The rooms so far serve the board's programme (AI); R105 is a free
        # lecture room of another programme (CS).
        Room.objects.filter(room_code__startswith="R10").update(department="AI")
        Room.objects.create(
            room_code="R105", room_type="lecture", capacity=40, section="M", department="CS"
        )
        words = {
            "en": (
                "Room unsuitable",
                "Lowers quality: Room unsuitable · outside this board's programme rooms",
                "Choosing one saves the warning on its row, which Issues then shows.",
                "Room R105 assigned; Issues will show “Room unsuitable”.",
            ),
            "ar": (
                "القاعة غير مناسبة",
                "يُضعف الجودة: القاعة غير مناسبة · خارج قاعات برامج هذه اللوحة",
                "اختيار إحداها يحفظ التحذير المذكور في سطرها، ويظهر بعدها في «المشاكل».",
                "عُيّنت القاعة R105، وستعرض «المشاكل»: «القاعة غير مناسبة».",
            ),
        }
        # The card's tone mark names its issues once the quality check has
        # answered about the data on show.
        card_issues = f"""() => {{
          const mark = document.querySelector('{self.card} .tw-card-tone');
          return TW.quality && TW.qualityVersion === TW.dataVersion ? (mark.hidden ? '' : mark.getAttribute('aria-label')) : null;
        }}"""
        for language, (kind, verdict, hint, report) in words.items():
            self._reset()
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            page.wait_for_function(f"({card_issues})() !== null")
            self.assertNotIn(kind, page.evaluate(card_issues), f"{language}: R100 serves AI")
            page.click(self.card)
            page.click('#twRightBody [data-tw-action="room"]')
            page.wait_for_selector('#twRoomBody input[value="R105"]', state="attached")
            row = page.evaluate(
                """() => {
                  const radio = document.querySelector('#twRoomBody input[value="R105"]');
                  const group = radio.closest('fieldset');
                  const text = id => document.getElementById(id || '')?.textContent ?? null;
                  return [group.dataset.group, radio.closest('.ux-choice').dataset.tone,
                          text(radio.getAttribute('aria-describedby').split(' ')[0]),
                          text(group.getAttribute('aria-describedby'))];
                }"""
            )
            self.assertEqual(row, ["flagged", "warning", verdict, hint], language)
            page.click('#twRoomBody label:has(input[value="R105"])')
            page.click("#twRoomAssign")
            self._saved(page, "R105")
            self.assertEqual(self.placements.get(pk=self.sunday.pk).room, "R105", language)
            # The report is a warning that names what Issues will list (codes
            # are isolated in it).
            page.wait_for_function(
                """report => [...document.querySelectorAll('#toast-container .ux-toast.warning .ux-toast-title')]
                  .some(title => title.textContent.replace(/[\\u2066\\u2069]/g, '') === report)""",
                arg=report,
            )
            # Issues list it as unsuitable, and the card rings for it.
            page.wait_for_function(f"({card_issues})()?.includes({kind!r})")
            page.click("#twTabIssues")
            page.wait_for_function(
                "([kind, subject]) => [...document.querySelectorAll('#twRightBody .tw-issue-row[data-tone=\"warning\"]')]"
                ".some(row => row.querySelector('.tw-issue-kind').textContent === kind"
                " && row.querySelector('.tw-issue-subject').textContent === subject)",
                arg=[kind, "AI111 · R105"],
            )
            self.assertTrue(
                page.evaluate(
                    f"document.querySelector('{self.card}').classList.contains('warning')"
                ),
                language,
            )
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_a_room_short_of_the_audits_seats_is_the_warning_issues_then_show(self) -> None:
        """5c-audit (RB5): the dialog judges seats by the quality audit's need,
        the buffered budget (24 students, +10%: 26), not the section's 30. A
        25-seat room is "Room too small" to Issues once saved, and the owner
        made that a warning that can be chosen (Q1): the row says so before the
        save, the report after it, and Issues then list it and ring the card.
        The wrong type stays refused."""
        from core.models import Room, ScenarioSectionBudget

        Room.objects.filter(room_code__startswith="R10").update(department="AI")
        Room.objects.create(
            room_code="R106", room_type="lecture", capacity=25, section="M", department="AI"
        )
        ScenarioSectionBudget.objects.filter(
            scenario=self.scenario, course_key=self.ai111.course_key
        ).update(total_demand=24)
        words = {
            "en": (
                "Room too small",
                "Lowers quality: Room too small · needs 26 seats",
                "25 seats · 1 short",
                "Choosing one saves the warning on its row, which Issues then shows.",
                "This meeting needs a lecture room",
                "Room R106 assigned; Issues will show “Room too small”.",
                "needs 26 seats",
            ),
            "ar": (
                "القاعة أصغر من المطلوب",
                "يُضعف الجودة: القاعة أصغر من المطلوب · يحتاج 26 مقعداً",
                "25 مقعداً · ينقصها مقعد واحد",
                "اختيار إحداها يحفظ التحذير المذكور في سطرها، ويظهر بعدها في «المشاكل».",
                "هذا اللقاء يحتاج إلى قاعة محاضرات",
                "عُيّنت القاعة R106، وستعرض «المشاكل»: «القاعة أصغر من المطلوب».",
                "يحتاج 26 مقعداً",
            ),
        }
        card_issues = f"""() => {{
          const mark = document.querySelector('{self.card} .tw-card-tone');
          return TW.quality && TW.qualityVersion === TW.dataVersion ? (mark.hidden ? '' : mark.getAttribute('aria-label')) : null;
        }}"""
        for language, (kind, verdict, facts, hint, lab, report, needs) in words.items():
            self._reset()
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            page.wait_for_function(f"({card_issues})() !== null")
            self.assertEqual(page.evaluate(card_issues), "", f"{language}: R100 fits")
            page.click(self.card)
            page.click('#twRightBody [data-tw-action="room"]')
            page.wait_for_selector('#twRoomBody input[value="R106"]', state="attached")
            row = page.evaluate(
                """() => {
                  const radio = document.querySelector('#twRoomBody input[value="R106"]');
                  const group = radio.closest('fieldset');
                  const text = id => document.getElementById(id || '')?.textContent ?? null;
                  const [said, facts] = radio.getAttribute('aria-describedby').split(' ');
                  return [group.dataset.group, radio.closest('.ux-choice').dataset.tone, text(said), text(facts),
                          text(group.getAttribute('aria-describedby'))];
                }"""
            )
            self.assertEqual(row[:3], ["flagged", "warning", verdict], language)
            self.assertIn(facts, row[3], language)
            self.assertEqual(row[4], hint, language)
            # The lab is still refused, with its reason; R100 and R101 are free.
            self.assertEqual(
                page.evaluate(
                    """() => [[...document.querySelectorAll('#twRoomBody fieldset[data-group="free"] input')].map(r => r.value),
                      [...document.querySelectorAll('#twRoomMisfits .ux-choice-static')].map(row => row.textContent)]"""
                )[0],
                ["R100", "R101"],
                language,
            )
            self.assertIn(
                lab,
                page.evaluate(
                    "document.querySelector('#twRoomMisfits .ux-choice-static').textContent"
                ),
                language,
            )
            page.click('#twRoomBody label:has(input[value="R106"])')
            page.click("#twRoomAssign")
            self._saved(page, "R106")
            self.assertEqual(self.placements.get(pk=self.sunday.pk).room, "R106", language)
            page.wait_for_function(
                """report => [...document.querySelectorAll('#toast-container .ux-toast.warning .ux-toast-title')]
                  .some(title => title.textContent.replace(/[\\u2066\\u2069]/g, '') === report)""",
                arg=report,
            )
            # Issues list it, with the seats it needs, and the card rings for it.
            page.wait_for_function(f"({card_issues})()?.includes({kind!r})")
            page.click("#twTabIssues")
            page.wait_for_function(
                "([kind, subject, needs]) => [...document.querySelectorAll('#twRightBody .tw-issue-row[data-tone=\"warning\"]')]"
                ".some(row => row.querySelector('.tw-issue-kind').textContent === kind"
                " && row.querySelector('.tw-issue-subject').textContent === subject"
                " && row.querySelector('.tw-issue-detail').textContent === needs)",
                arg=[kind, "AI111 · R106", needs],
            )
            self.assertTrue(
                page.evaluate(
                    f"document.querySelector('{self.card}').classList.contains('warning')"
                ),
                language,
            )
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_the_room_dialog_reads_keeps_its_foot_and_follows_the_arrows_in_both_directions(
        self,
    ) -> None:
        from core.models import Room

        # Enough rooms that the list scrolls in every window.
        for n in range(30):
            Room.objects.create(
                room_code=f"R2{n:02d}", room_type="lecture", capacity=40, section="M", building="B2"
            )
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height, cards=5)
                    page.click(self.card)
                    page.click('#twRightBody [data-tw-action="room"]')
                    page.wait_for_function("document.activeElement?.name === 'twRoomChoice'")
                    found = page.evaluate(
                        CONTRAST,
                        [
                            "#twRoomDialog .ux-choice-title",
                            "#twRoomDialog .ux-choice-desc",
                            "#twRoomDialog .ux-choice-status",
                            "#twRoomDialog .tw-room-verdict",
                            "#twRoomDialog .ux-field-label",
                            "#twRoomDialog .ux-dialog-sub",
                            "#twRoomDialog .tw-room-current",
                            "#twRoomDialog .ux-dialog-foot-note",
                            "#twRoomDialog .ux-disclosure-label",
                        ],
                    )
                    found += page.evaluate(SMALL_TEXT)
                    # The next room is the next row down, in either direction.
                    before = page.evaluate(
                        "[...document.querySelectorAll('#twRoomBody input[name=\"twRoomChoice\"]')].indexOf(document.activeElement)"
                    )
                    page.keyboard.press("ArrowDown")
                    after = page.evaluate(
                        "[...document.querySelectorAll('#twRoomBody input[name=\"twRoomChoice\"]')].indexOf(document.activeElement)"
                    )
                    if (before, after) != (0, 1):
                        found.append(f"ArrowDown went from row {before} to row {after}")
                    found += page.evaluate(FOOT_IN_VIEW)
                    # The time's own clashes and the alert line, in each tone it
                    # takes: drawn here, since this meeting has no clash and
                    # nothing was refused.
                    page.evaluate(SHOW_CLASH_NOTE)
                    found += page.evaluate(NOTES_IN_TONE)
                    for tone in ("warning", "success"):
                        page.evaluate(SAY_IN_ALERT, tone)
                        found += page.evaluate(
                            CONTRAST, ["#twRoomDialog .tw-room-note", "#twRoomDialog #twRoomAlert"]
                        )
                        found += page.evaluate(ALERT_IN_TONE, tone)
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})


# ── Move (§7.3, 5e) ──
# Text in the Move dialog under 11px.
MOVE_SMALL_TEXT = SMALL_TEXT.replace("#twRoomDialog", "#twMoveDialog")
# With the list scrolled to its end, the title and the commit are on screen
# and not covered; the dialog itself never scrolls (its body does).
MOVE_FOOT_IN_VIEW = r"""() => {
  const dialog = document.getElementById('twMoveDialog'), body = document.getElementById('twMoveBody');
  if (body.scrollHeight <= body.clientHeight + 1) return ['the list does not scroll at this size: the check proves nothing'];
  body.scrollTop = body.scrollHeight;
  const bad = [];
  const seen = el => { const r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= innerHeight && el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); };
  if (!seen(document.getElementById('twMoveCommit'))) bad.push('the commit scrolled away or is covered');
  if (!seen(document.getElementById('twMoveTitle'))) bad.push('the title scrolled away or is covered');
  if (dialog.scrollTop !== 0) bad.push('the dialog itself scrolled');
  if (body.scrollTop === 0) bad.push('the list did not scroll');
  return bad;
}"""
# Every time in the dialog is isolated left to right, as a time.
MOVE_LTR_TIMES = r"""() => {
  const times = [...document.querySelectorAll('#twMoveDialog bdi[dir="ltr"]')].map(bdi => bdi.textContent);
  const rows = [...document.querySelectorAll('#twMoveBody input[name="twMoveChoice"]')];
  const bad = times.filter(text => !/^\d\d:\d\d–\d\d:\d\d$/.test(text)).map(text => `not a time: ${text}`);
  if (times.length < rows.length + 2) bad.push(`only ${times.length} isolated times for ${rows.length} rows`);
  rows.filter(radio => !document.getElementById(radio.getAttribute('aria-labelledby')).querySelector('bdi[dir="ltr"]'))
    .forEach(radio => bad.push(`${radio.value}: its time is not isolated`));
  return bad;
}"""
SAY_IN_MOVE_ALERT = """tone => dialogSay(document.getElementById('twMoveDialog'),
  tone === 'success' ? movedWords('MON', '13:00', '14:15') : MOVE_WORDS.changed, tone)"""
MOVE_ALERT_IN_TONE = ALERT_IN_TONE.replace("twRoomAlert", "twMoveAlert")
# The status line, drawn as a failed read draws it (nothing failed here).
SHOW_MOVE_FAILURE = """() => { Object.assign(MOVE, { state: 'error', failure: { code: 'BOOM', status: 500 } }); drawMoveDialog(); }"""
ON_MOVE = "document.activeElement?.matches('#twRightBody [data-tw-action=\"move\"]')"
CHOSEN_MOVE = "document.activeElement?.name === 'twMoveChoice'"


class MoveDialogTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """Move (§7.3, 5e) in a real browser. These write, to pytest's database.
    AI111 S1 meets on Sunday and Tuesday 09:00 in R101; AI222 S1 meets on
    Monday and Wednesday 13:00 in R101 too, so moving AI111's Sunday meeting
    to Monday 13:00 keeps its room and double-books R101. MATH201 S1 meets
    on Thursday 10:30, its time locked (and no other scenario owns it)."""

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        from core.models import DeliveryBoard, SectionPlacement, TermSection

        board = DeliveryBoard.objects.get(scenario=self.scenario)
        key = fx.requirement("AI", "MATH201", "Discrete Mathematics")
        fx.budget(self.scenario, key, 1)
        fx.place(
            board,
            fx.section(self.scenario, key, "S1"),
            (("THU", "10:30", "11:45"),),
            locked=True,
            room="R102",
        )
        self.placements = SectionPlacement.objects
        ai111 = TermSection.objects.get(scenario=self.scenario, course_code="AI111")
        self.sunday = self.placements.get(term_section=ai111, day="SUN")
        self.card = f'#twGrid [data-focus-key="placement:{self.sunday.id}"]'
        self.math = self.placements.get(term_section__course_code="MATH201")
        self.math_card = f'#twGrid [data-focus-key="placement:{self.math.id}"]'

    def _at_rest(self, page, day: str, start: str) -> None:
        # The board read again with the meeting at this time, and the page at rest.
        page.wait_for_function(
            """([id, day, start]) => !document.getElementById('twMoveDialog').open
              && TW.placements.some(p => p.id === id && p.day === day && p.start_time === start)
              && document.querySelector('.tw-workspace').dataset.busy === 'false'""",
            arg=[self.sunday.id, day, start],
        )

    def test_mb1_the_move_dialog_reads_keeps_its_foot_and_gives_the_keyboard_back(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height, cards=5)
                    page.click(self.card)
                    page.click('#twRightBody [data-tw-action="move"]')
                    page.wait_for_function(CHOSEN_MOVE)
                    found = page.evaluate(
                        CONTRAST,
                        [
                            "#twMoveDialog .ux-dialog-title",
                            "#twMoveDialog .ux-dialog-sub",
                            "#twMoveDialog .ux-choice-title",
                            "#twMoveDialog .ux-choice-desc",
                            "#twMoveDialog .tw-move-verdict",
                            "#twMoveDialog .ux-field-label",
                            "#twMoveDialog .ux-dialog-foot-note",
                        ],
                    )
                    found += page.evaluate(MOVE_SMALL_TEXT)
                    found += page.evaluate(MOVE_FOOT_IN_VIEW)
                    if language == "ar":
                        found += page.evaluate(MOVE_LTR_TIMES)
                    # The alert line in each tone it takes, and the status line:
                    # drawn here, since nothing was refused and nothing failed.
                    for tone in ("warning", "success"):
                        page.evaluate(SAY_IN_MOVE_ALERT, tone)
                        found += page.evaluate(CONTRAST, ["#twMoveDialog #twMoveAlert"])
                        found += page.evaluate(MOVE_ALERT_IN_TONE, tone)
                    page.evaluate(SHOW_MOVE_FAILURE)
                    found += page.evaluate(CONTRAST, ["#twMoveDialog .ux-choice-status"])
                    found += page.evaluate(MOVE_SMALL_TEXT)
                    # Escape gives the keyboard back to Selection's Move…, drawn
                    # again while the dialog was open (the browser's own return
                    # would aim at the old node).
                    page.evaluate(
                        f"TW.placements.find(p => p.id === {self.sunday.id}).registered_count = 7;"
                        " renderRightPanel('selection')"
                    )
                    page.keyboard.press("Escape")
                    page.wait_for_function("!document.getElementById('twMoveDialog').open")
                    if not page.evaluate(ON_MOVE):
                        found.append("Escape did not give the keyboard back to Move…")
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})

    def test_mb2_m_moves_the_meeting_by_its_time_alone_and_undo_moves_it_back(self) -> None:
        for language, name, kind, report in (
            (
                "en",
                r"Mon.*13:00",
                "Room double-booked",
                "Press R on the card to change its room.",
            ),
            (
                "ar",
                r"الاثنين.*13:00",
                "القاعة محجوزة مرتين",
                "اضغط R على البطاقة لتغيير قاعتها.",
            ),
        ):
            self.placements.filter(pk=self.sunday.pk).update(
                day="SUN", start_time="09:00", end_time="10:15"
            )
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            posts: list[tuple[str, str]] = []
            page.on(
                "request",
                lambda r, posts=posts: posts.append((r.url, r.post_data))
                if r.method == "POST"
                else None,
            )
            page.focus(self.card)
            page.keyboard.press("m")
            page.wait_for_function(CHOSEN_MOVE)
            # The board drawn again while the dialog is open: the keyboard
            # comes back to the card drawn now, not the old node.
            page.evaluate("renderGrid()")
            # The time, by its accessible name: Monday 13:00, where R101 is AI222's.
            value = page.get_by_role("radio", name=re.compile(name)).get_attribute("value")
            self.assertEqual(value, "MON|13:00", language)
            page.focus(f'#twMoveBody input[value="{value}"]')
            page.keyboard.press("Space")
            row = page.evaluate(
                """value => { const radio = document.querySelector(`#twMoveBody input[value="${value}"]`);
                  return [radio.checked, radio.closest('.ux-choice').dataset.tone,
                          document.getElementById(radio.getAttribute('aria-describedby').split(' ')[0]).textContent]; }""",
                value,
            )
            self.assertEqual(row[:2], [True, "critical"], language)
            self.assertIn(kind, row[2], language)
            page.keyboard.press("Enter")
            self._at_rest(page, "MON", "13:00")
            moves = [
                body for url, body in posts if url.endswith(f"/placements/{self.sunday.id}/move/")
            ]
            self.assertEqual(
                moves, ['{"day":"MON","start_time":"13:00","end_time":"14:15"}'], language
            )
            saved = self.placements.get(pk=self.sunday.pk)
            self.assertEqual(
                (saved.day, saved.start_time, saved.end_time, saved.room),
                ("MON", "13:00", "14:15", "R101"),
                f"{language}: its time moved, its room stayed",
            )
            # The card at its new time, toned by the clash it saved, has the keyboard.
            page.wait_for_function(
                f"document.activeElement?.matches('{self.card}') && document.activeElement.classList.contains('critical')"
            )
            self.assertEqual(
                page.evaluate(
                    "(() => { const cell = document.activeElement.closest('[role=\"gridcell\"]'); return [cell.dataset.day, cell.dataset.start]; })()"
                ),
                ["MON", "13:00"],
                language,
            )
            page.wait_for_function(
                """([kind, report]) => [...document.querySelectorAll('#toast-container .ux-toast.warning .ux-toast-title')]
                  .some(title => { const text = title.textContent.replace(/[\\u2066\\u2069]/g, ''); return text.includes(kind) && text.endsWith(report); })""",
                arg=[kind, report],
            )
            # Undo moves it back, by its time alone.
            page.click("#twUndo")
            self._at_rest(page, "SUN", "09:00")
            moves = [
                body for url, body in posts if url.endswith(f"/placements/{self.sunday.id}/move/")
            ]
            self.assertEqual(
                moves[1:], ['{"day":"SUN","start_time":"09:00","end_time":"10:15"}'], language
            )
            self.assertEqual(self.placements.get(pk=self.sunday.pk).day, "SUN", language)
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_mb3_a_time_locked_meeting_reads_nothing_and_says_why(self) -> None:
        page, sent, errors = self._load("en", "light", 1440, 790, cards=5)
        reads: list[str] = []
        page.on("request", lambda r: reads.append(r.url) if "/slot-candidates/" in r.url else None)
        page.click(self.math_card)
        page.wait_for_selector('#twRightBody [data-tw-action="move"]')
        held = page.evaluate(
            """() => { const button = document.querySelector('#twRightBody [data-tw-action="move"]');
              const note = document.getElementById('twSelActionNote');
              return [button.disabled, button.hasAttribute('data-tw-hold'), button.getAttribute('aria-describedby'),
                      note.textContent, note.hidden, note.getClientRects().length > 0]; }"""
        )
        locked = (
            "This section's time is locked. Unlock its time to move it; its room can still change."
        )
        self.assertEqual(held, [True, True, "twSelActionNote", locked, False, True])
        page.focus(self.math_card)
        page.keyboard.press("m")
        page.wait_for_function(
            "words => [...document.querySelectorAll('#toast-container .ux-toast.warning .ux-toast-title')].some(t => t.textContent === words)",
            arg=locked,
        )
        self.assertFalse(page.evaluate("document.getElementById('twMoveDialog').open"))
        self.assertEqual(reads, [])
        self.assertEqual([*sent, *errors], [])


# ── Place… (§7.3, 5f) ──
# Text in the Place dialog under 11px.
PLACE_SMALL_TEXT = SMALL_TEXT.replace("#twRoomDialog", "#twPlaceDialog")
# With the list scrolled to its end, the title and the commit are on screen
# and not covered; the dialog itself never scrolls (its body does).
PLACE_FOOT_IN_VIEW = (
    MOVE_FOOT_IN_VIEW.replace("twMoveDialog", "twPlaceDialog")
    .replace("twMoveBody", "twPlaceBody")
    .replace("twMoveCommit", "twPlaceCommit")
    .replace("twMoveTitle", "twPlaceTitle")
)
# Every time in the dialog is isolated left to right, as a time; and painted
# so: the start lies left of the end (an Arabic line would otherwise paint
# "10:15–09:00").
PLACE_LTR_TIMES = r"""() => {
  const times = [...document.querySelectorAll('#twPlaceDialog bdi[dir="ltr"]')];
  const rows = [...document.querySelectorAll('#twPlaceBody input[name="twPlaceChoice"]')];
  const bad = times.filter(bdi => !/^\d\d:\d\d–\d\d:\d\d$/.test(bdi.textContent)).map(bdi => `not a time: ${bdi.textContent}`);
  if (times.length < rows.length * 2) bad.push(`only ${times.length} isolated times for ${rows.length} two-meeting rows`);
  rows.filter(radio => !document.getElementById(radio.getAttribute('aria-labelledby')).querySelector('bdi[dir="ltr"]'))
    .forEach(radio => bad.push(`${radio.value}: its time is not isolated`));
  const first = document.getElementById(rows[0].getAttribute('aria-labelledby')).querySelector('bdi[dir="ltr"]');
  const text = first.firstChild;
  const at = (from, to) => { const range = document.createRange(); range.setStart(text, from); range.setEnd(text, to); return range.getBoundingClientRect().left; };
  if (!(at(0, 5) < at(6, 11))) bad.push(`"${text.textContent.slice(0, 5)}" is not left of "${text.textContent.slice(6, 11)}"`);
  return bad;
}"""
SAY_IN_PLACE_ALERT = """tone => dialogSay(document.getElementById('twPlaceDialog'),
  tone === 'success' ? placedWords('AI222', 'S2') : PLACE_WORDS.nothingPlaced, tone)"""
PLACE_ALERT_IN_TONE = ALERT_IN_TONE.replace("twRoomAlert", "twPlaceAlert")
CHOSEN_PLACE = "document.activeElement?.name === 'twPlaceChoice'"
# The keyboard on AI222's S2 row in the list.
ON_S2 = """(() => { const a = document.activeElement;
  return Boolean(a && a.dataset.course === 'AI222' && /^place:.*\\|S2$/.test(a.dataset.focusKey || '')); })()"""


class PlaceDialogTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """Place… (§7.3, 5f) in a real browser. These write, to pytest's
    database. AI222 (two sections) has S1 placed on Monday and Wednesday
    13:00, so its S2 is still to place, with two meetings a week."""

    maxDiff = None

    def _s2_placements(self):
        from core.models import SectionPlacement

        return SectionPlacement.objects.filter(
            term_section__scenario=self.scenario,
            term_section__course_code="AI222",
            term_section__section="S2",
        )

    def _open_list(self, page) -> None:
        # Below 1600px the list starts collapsed to its rail: [ opens it.
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.press("BracketLeft")
        page.wait_for_function("document.getElementById('twWorkspace').dataset.left === 'open'")
        page.wait_for_function(
            "document.querySelectorAll('#twSectionList [tabindex=\"0\"]').length === 1"
        )

    def test_pb1_the_keyboard_alone_places_ai222_s2_with_both_its_meetings(self) -> None:
        import json

        for language in ("en", "ar"):
            self._s2_placements().delete()
            page, sent, errors = self._load(language, "light", 1440, 790)
            posts: list[tuple[str, str]] = []
            page.on(
                "request",
                lambda r, posts=posts: posts.append((r.url, r.post_data))
                if r.method == "POST"
                else None,
            )
            self._open_list(page)
            page.focus("#twSectionSearch")
            page.keyboard.press("Tab")
            self.assertTrue(
                page.evaluate(
                    "document.getElementById('twSectionList').contains(document.activeElement)"
                ),
                f"{language}: Tab reaches the list",
            )
            for _ in range(10):
                if page.evaluate(ON_S2):
                    break
                page.keyboard.press("ArrowDown")
            self.assertTrue(page.evaluate(ON_S2), f"{language}: ↓ reaches AI222 S2")
            # Its "Place…" shows where the keyboard is.
            self.assertEqual(
                page.evaluate(
                    "getComputedStyle(document.activeElement.querySelector('.tw-place-hint')).visibility"
                ),
                "visible",
                language,
            )
            page.keyboard.press("Enter")
            page.wait_for_function(CHOSEN_PLACE)
            chosen = page.evaluate(
                "[document.activeElement.checked, document.activeElement.value, document.getElementById('twPlaceDialog').open]"
            )
            self.assertEqual(chosen[0], True, f"{language}: the keyboard is on the chosen pattern")
            self.assertTrue(chosen[2], language)
            rows = page.evaluate(
                "[...document.querySelectorAll('#twPlaceBody input[name=\"twPlaceChoice\"]')].filter(r => !r.closest('[hidden]')).map(r => r.value)"
            )
            self.assertGreater(len(rows), 2, language)
            self.assertEqual(chosen[1], rows[0], f"{language}: the best pattern is chosen")
            page.keyboard.press("End")
            self.assertEqual(
                page.evaluate("[document.activeElement.value, document.activeElement.checked]"),
                [rows[-1], True],
                f"{language}: End",
            )
            page.keyboard.press("Home")
            self.assertEqual(
                page.evaluate("[document.activeElement.value, document.activeElement.checked]"),
                [rows[0], True],
                f"{language}: Home",
            )
            page.keyboard.press("Enter")
            page.wait_for_function(
                """() => !document.getElementById('twPlaceDialog').open
                  && document.querySelectorAll('.tw-card').length === 6
                  && document.querySelector('.tw-workspace').dataset.busy === 'false'"""
            )
            creates = [body for url, body in posts if url.endswith("/placements/create-planned/")]
            self.assertEqual(len(creates), 1, f"{language}: one POST")
            body = json.loads(creates[0])
            self.assertEqual(
                [len(body["meetings"]), body["section_label"], body["course_code"]],
                [2, "S2", "AI222"],
                language,
            )
            self.assertEqual(
                sorted((p.day, p.start_time) for p in self._s2_placements()),
                sorted((m["day"], m["start_time"]) for m in body["meetings"]),
                f"{language}: two S2 placements, at the pattern's times",
            )
            self.assertEqual(self._s2_placements().count(), 2, language)
            self.assertTrue(
                page.evaluate(
                    "document.getElementById('twSectionList').contains(document.activeElement)"
                ),
                f"{language}: the keyboard stays in the list",
            )
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_pb2_a_drag_onto_thursday_1300_opens_place_on_that_time_and_sends_nothing(self) -> None:
        page, sent, errors = self._load("en", "light", 1920, 1080)
        source = page.locator('#twSectionList [data-course="AI222"][data-focus-key$="|S2"]')
        target = page.locator('#twGrid [role="gridcell"][data-day="THU"][data-start="13:00"]').first
        source.scroll_into_view_if_needed()
        source.drag_to(target)
        page.wait_for_function(
            "document.getElementById('twPlaceDialog').open"
            " && document.querySelectorAll('#twPlaceBody input[name=\"twPlaceChoice\"]').length > 0"
        )
        self.assertEqual(
            page.evaluate(
                "[document.getElementById('twPlaceDay').value, document.getElementById('twPlaceTime').value]"
            ),
            ["THU", "13:00-14:15"],
        )
        shown = page.evaluate(
            "[...document.querySelectorAll('#twPlaceBody input[name=\"twPlaceChoice\"]')].filter(r => !r.closest('[hidden]')).map(r => r.value)"
        )
        self.assertTrue(shown, "some pattern includes Thursday 13:00")
        self.assertTrue(all("THU@13:00-14:15" in value for value in shown), shown)
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('twPlaceDialog').open")
        self.assertEqual(self._s2_placements().count(), 0)
        self.assertEqual([*sent, *errors], [])

    def test_pb3_the_place_dialog_reads_keeps_its_foot_and_paints_its_times_left_to_right(
        self,
    ) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height)
                    self._open_list(page)
                    page.focus("#twSectionSearch")
                    page.keyboard.press("Tab")
                    for _ in range(10):
                        if page.evaluate(ON_S2):
                            break
                        page.keyboard.press("ArrowDown")
                    page.keyboard.press("Enter")
                    page.wait_for_function(CHOSEN_PLACE)
                    found = page.evaluate(
                        CONTRAST,
                        [
                            "#twPlaceDialog .ux-dialog-title",
                            "#twPlaceDialog .ux-dialog-sub",
                            "#twPlaceDialog .tw-dialog-status",
                            "#twPlaceDialog .ux-field-label",
                            "#twPlaceDialog .fb-lbl",
                            "#twPlaceDialog select",
                            "#twPlaceDialog .ux-choice-title",
                            "#twPlaceDialog .ux-choice-desc",
                            "#twPlaceDialog .tw-move-verdict",
                            "#twPlaceDialog .ux-dialog-foot-note",
                        ],
                    )
                    found += page.evaluate(PLACE_SMALL_TEXT)
                    found += page.evaluate(PLACE_FOOT_IN_VIEW)
                    if language == "ar":
                        found += page.evaluate(PLACE_LTR_TIMES)
                    # The alert line in each tone it takes: drawn here, since
                    # nothing was refused.
                    for tone in ("warning", "success"):
                        page.evaluate(SAY_IN_PLACE_ALERT, tone)
                        found += page.evaluate(CONTRAST, ["#twPlaceDialog #twPlaceAlert"])
                        found += page.evaluate(PLACE_ALERT_IN_TONE, tone)
                    # The course's term when it is not the board's: its note reads too.
                    page.evaluate("PLACE.request.programme_term = 5; drawPlaceDialog()")
                    found += page.evaluate(CONTRAST, ["#twPlaceDialog .tw-place-note"])
                    found += page.evaluate(PLACE_SMALL_TEXT)
                    # Escape gives the keyboard back to the row.
                    page.keyboard.press("Escape")
                    page.wait_for_function("!document.getElementById('twPlaceDialog').open")
                    if not page.evaluate(ON_S2):
                        found.append("Escape did not give the keyboard back to the row")
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})
