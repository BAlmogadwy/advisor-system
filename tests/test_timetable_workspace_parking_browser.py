"""The Timetable Workspace's parking strip in a real browser.

docs/TIMETABLE-WORKSPACE-UI.md §7.2 item 6, 6e. A meeting no column of its
kind has (here a lecture saved at Monday 18:30, as the old editors could) is
drawn in a strip, the last row of its own table, with its day and time; it
used to be drawn nowhere.

ParkingTests (1366×650 and 1440×790, light and dark, English and Arabic, the
wide font): the strip sits under its table; its "Off grid" header wraps in the
56px day column without clipping, at 12px; its words, the card's code, room
and day and time, and the legend's line read at 4.5:1; the frame keeps its
acceptance (no sideways scroll, nothing under 11px); and the parked card's
cell is wide enough that the card names its teacher (PK8: its cell is the
card's container, as a slot is).

Every table's rows (ROWS): each day, and the strip's header, in the day
column under the row before, and the strip's cells beside its header. A table
with no slot column, drawn only for its strip, keeps each day on its own row.

From the keyboard, M on the parked card opens Move, a time at 09:00 moves it
into its cell, and the strip goes (with its legend line).
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402

from core.models import SectionPlacement  # noqa: E402
from tests import one_teacher_support as fx  # noqa: E402
from tests.test_timetable_workspace_frame_browser import CONTRAST, FRAME  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

# What is wrong with the strip, as short findings.
STRIP = r"""() => {
  const bad = [];
  const rows = [...document.querySelectorAll('#twGrid .tw-park-row')];
  if (rows.length !== 1) return [`${rows.length} strips`];
  const row = rows[0];
  const table = row.closest('[role="grid"]');
  if (table.dataset.kind !== 'lecture') bad.push(`in the ${table.dataset.kind} table`);
  if (row.parentElement.lastElementChild !== row) bad.push('not its table’s last row');
  const head = row.querySelector('.tw-park-head');
  const park = row.querySelector('.tw-park');
  const thu = [...table.querySelectorAll('.tw-grid-slot-label')].filter(label => label !== head).pop();
  const box = el => el.getBoundingClientRect();
  if (box(head).top < box(thu).bottom - 0.5 || box(park).top < box(thu).bottom - 0.5) bad.push('not under the last day');
  if (box(head).bottom > box(table).bottom + 0.5 || box(park).bottom > box(table).bottom + 0.5) bad.push('outside its table');
  if (Math.abs(box(head).left - box(thu).left) > 0.5 || Math.abs(box(head).width - box(thu).width) > 0.5) bad.push('header not in the day column');
  // The strip spans every slot column of its table.
  const slots = [...table.querySelectorAll('.tw-grid-header:not(.tw-grid-corner)')];
  const left = Math.min(...slots.map(h => box(h).left)), right = Math.max(...slots.map(h => box(h).right));
  if (Math.abs(box(park).left - left) > 1 || Math.abs(box(park).right - right) > 1) bad.push(`strip ${Math.round(box(park).left)}–${Math.round(box(park).right)}, columns ${Math.round(left)}–${Math.round(right)}`);
  // Read at full strength: the contrast helper reads colours, not opacity.
  for (const el of [head, park, ...park.querySelectorAll('.tw-card, .tw-card-when')]) {
    for (let e = el; e && e !== table; e = e.parentElement) {
      if (parseFloat(getComputedStyle(e).opacity) < 1) { bad.push(`${e.className} at opacity ${getComputedStyle(e).opacity}`); break; }
    }
  }
  const size = parseFloat(getComputedStyle(head).fontSize);
  if (size < 11.95) bad.push(`header ${size}px`);
  if (head.scrollWidth > head.clientWidth + 0.5) bad.push(`header clipped across (${head.scrollWidth} > ${head.clientWidth})`);
  if (head.scrollHeight > head.clientHeight + 0.5) bad.push(`header clipped down (${head.scrollHeight} > ${head.clientHeight})`);
  const words = [...head.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
  const range = document.createRange(); range.selectNodeContents(head.firstChild);
  const lines = new Set([...range.getClientRects()].map(r => Math.round(r.top))).size;
  // Wrapped where it must, at a space: never a word cut in two.
  if (lines > words.split(/\s+/).length) bad.push(`"${words}" broken inside a word (${lines} lines)`);
  const card = park.querySelector('.tw-card');
  const when = card.querySelector('.tw-card-when');
  if (!when || parseFloat(getComputedStyle(when).fontSize) < 11.95) bad.push('when-line under 12px');
  else if (when.scrollWidth > when.clientWidth + 0.5) bad.push('when-line cut');
  // PK8: the cell is the card's container, wide enough for the teacher.
  const cell = card.closest('.tw-park-cell');
  const teacher = card.querySelector('.tw-card-teacher');
  if (box(cell).width < 130) bad.push(`cell ${Math.round(box(cell).width)}px`);
  if (!teacher) bad.push('no teacher on the card');
  else if (getComputedStyle(teacher).display === 'none') bad.push(`teacher hidden at ${Math.round(box(cell).width)}px`);
  if (!document.getElementById('twLegendPark')) bad.push('no legend line');
  return bad;
}"""

# Every table's rows, as short findings: each row's label (a day, or the
# strip's header) in the day column under the row before, and a strip's cells
# beside its header, under its last day.
ROWS = r"""() => {
  const bad = [];
  const box = el => el.getBoundingClientRect();
  for (const table of document.querySelectorAll('#twGrid [role="grid"]')) {
    const name = `${table.dataset.group}:${table.dataset.kind}`;
    const corner = box(table.querySelector('.tw-grid-corner'));
    let above = corner;
    for (const label of table.querySelectorAll('.tw-grid-slot-label')) {
      const b = box(label);
      const words = label.textContent.trim();
      if (Math.abs(b.left - corner.left) > 0.5 || Math.abs(b.width - corner.width) > 0.5) bad.push(`${name}: "${words}" not in the day column`);
      if (b.top < above.bottom - 0.5) bad.push(`${name}: "${words}" not under the row before`);
      above = b;
    }
    const park = table.querySelector('.tw-park');
    if (park) {
      const b = box(park);
      const rtl = getComputedStyle(table).direction === 'rtl';
      if (rtl ? b.right > corner.left + 0.5 : b.left < corner.right - 0.5) bad.push(`${name}: the strip over the day column`);
      if (Math.abs(b.top - above.top) > 0.5) bad.push(`${name}: the strip not beside its header`);
    }
  }
  return bad;
}"""


class ParkingTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        # A lecture saved at Monday 18:30: no lecture column starts then.
        key = fx.requirement("AI", "AI333", "Planning")
        fx.budget(self.scenario, key, 1)
        board = self.scenario.boards.get()
        fx.place(
            board,
            fx.section(self.scenario, key, "S1"),
            (("MON", "18:30", "19:45"),),
            room="R101",
            instructor_name="Dr Parker",
        )
        self.parked = SectionPlacement.objects.get(term_section__course_code="AI333")

    def test_the_strip_sits_under_its_table_and_reads(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height, cards=5)
                    found = page.evaluate(STRIP)
                    found += page.evaluate(ROWS)
                    found += page.evaluate(
                        CONTRAST,
                        [
                            ".tw-park-head",
                            ".tw-park .tw-card-when",
                            ".tw-park .tw-card-code",
                            ".tw-park .tw-card-room",
                            "#twLegendPark",
                        ],
                    )
                    found += page.evaluate(FRAME, 1440)
                    if sent or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})

    def test_a_table_with_no_columns_keeps_each_day_on_its_own_row(self) -> None:
        # A lab (100 minutes) drawn with no lab column: its table is drawn
        # for its strip alone (one column to lay it out in). The server
        # always has lab columns, so the page is drawn here without them.
        key = fx.requirement("AI", "AI444", "Lab work")
        fx.budget(self.scenario, key, 1)
        fx.place(
            self.scenario.boards.get(),
            fx.section(self.scenario, key, "S1"),
            (("SUN", "08:00", "09:40"),),
            room="R101",
        )
        lab = SectionPlacement.objects.get(term_section__course_code="AI444")
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1366, 650, cards=6)
            drawn = page.evaluate(
                """() => { renderGridWithSlots(scenarioLectureSlots(), []);
                  const table = document.querySelector('#twGrid [role="grid"][data-kind="lab"]');
                  return { cells: table.querySelectorAll('.tw-cell').length,
                           parked: [...table.querySelectorAll('.tw-park .tw-card')].map(c => Number(c.dataset.placementId)),
                           days: table.querySelectorAll('.tw-grid-slot-label:not(.tw-park-head)').length }; }"""
            )
            self.assertEqual(drawn, {"cells": 0, "parked": [lab.id], "days": 5}, language)
            self.assertEqual(page.evaluate(ROWS), [], language)
            self.assertEqual([*sent, *errors], [], language)
            page.context.close()

    def test_m_moves_a_parked_meeting_onto_the_grid_and_the_strip_goes(self) -> None:
        for language in ("en", "ar"):
            SectionPlacement.objects.filter(pk=self.parked.pk).update(
                day="MON", start_time="18:30", end_time="19:45"
            )
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            card = f'#twGrid .tw-park .tw-card[data-placement-id="{self.parked.id}"]'
            page.focus(card)
            page.keyboard.press("m")
            page.wait_for_function("document.activeElement?.name === 'twMoveChoice'")
            radio = '#twMoveBody input[value="MON|09:00"]'
            page.focus(radio)
            page.keyboard.press("Space")
            self.assertTrue(page.evaluate(f"document.querySelector('{radio}').checked"), language)
            page.keyboard.press("Enter")
            page.wait_for_function(
                """id => !document.getElementById('twMoveDialog').open
                  && TW.placements.some(p => p.id === id && p.day === 'MON' && p.start_time === '09:00')
                  && document.querySelector('.tw-workspace').dataset.busy === 'false'""",
                arg=self.parked.id,
            )
            where = page.evaluate(
                """id => { const card = document.querySelector(`#twGrid .tw-card[data-placement-id="${id}"]`);
                  const cell = card.closest('.tw-cell');
                  return { cell: cell ? [cell.dataset.day, cell.dataset.start] : null,
                           strips: document.querySelectorAll('#twGrid .tw-park-row').length,
                           legend: Boolean(document.getElementById('twLegendPark')) }; }""",
                self.parked.id,
            )
            self.assertEqual(
                where, {"cell": ["MON", "09:00"], "strips": 0, "legend": False}, language
            )
            saved = SectionPlacement.objects.get(pk=self.parked.pk)
            self.assertEqual(
                (saved.day, saved.start_time, saved.end_time), ("MON", "09:00", "10:15"), language
            )
            self.assertEqual(
                [
                    request
                    for request in sent
                    if not request.endswith(f"/placements/{self.parked.id}/move/")
                ],
                [],
                language,
            )
            self.assertEqual(errors, [], language)
            page.context.close()
