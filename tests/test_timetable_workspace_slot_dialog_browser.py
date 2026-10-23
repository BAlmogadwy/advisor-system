"""The Timetable Workspace's slot dialog in a real browser.

docs/TIMETABLE-WORKSPACE-UI.md §7.2 item 6, 6f. "Edit time slots…" edits the
grid's columns as rows (it was a textarea): a label, a start and an end per
column, typed as text; the server marks a field it refuses, and a change that
leaves a meeting outside the columns is reviewed inside the dialog before
Save anyway. The scenario here keeps the server's default columns (seven
lecture columns and eight lab ones, three of them online-only).

SDB1 (1366×650 and 1440×790, light and dark, English and Arabic, the wide
font): the head and Save stay in view while the rows scroll; the tables fit
the dialog and line up, the label taking the spare room; a button that cannot
act looks it; "08:00" fits its 76px field; a refused field's error reads at
4.5:1 and its edge at 3:1 (every other field's edge too); the dialog's words
read at 4.5:1; nothing is under 11px; and Escape gives the keyboard back to ⋯.

SDB2 (the keyboard alone): ⋯, Edit time slots…, an end before its start,
Enter: the field is marked and has the keyboard; the right end (in Arabic,
typed in Arabic-Indic digits: its length shown, and saved as 0–9), Enter:
saved to the database (the online-only windows keep their rule), and the
keyboard is back on ⋯.

SDB3: the 16:00 lecture column holds a meeting; removing it is reviewed (the
meeting named), Save anyway saves it, and the meeting is drawn under its
table.

SDB4: below 560px each row is two lines (its number and label; then its
times, length, rule and buttons), nothing scrolling sideways.

SDB5 (English and Arabic, light and dark): a draft kept from an earlier open
is offered as current while the saved columns are the ones it started from
(the board read again, from the real server); once they are saved elsewhere
it says so, in words that read at 4.5:1, and "Start from the saved columns"
gives the new ones. Nothing is sent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402

from core.models import SectionPlacement, TimetableScenario  # noqa: E402
from tests import one_teacher_support as fx  # noqa: E402
from tests.test_timetable_workspace_frame_browser import CONTRAST  # noqa: E402
from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin  # noqa: E402

# Text in the dialog under 11px (each element whose own text is shown).
SMALL_TEXT = r"""() => [...document.querySelectorAll('#twSlotDialog *')].filter(el => el.getClientRects().length
  && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
  && parseFloat(getComputedStyle(el).fontSize) < 11).map(el => `${el.tagName}.${el.className} ${getComputedStyle(el).fontSize}`)"""

# With the rows scrolled to their end, the title and Save are on screen and
# not covered; the dialog itself never scrolls (its body does), and nothing in
# it scrolls sideways.
FOOT_IN_VIEW = r"""() => {
  const dialog = document.getElementById('twSlotDialog'), body = document.getElementById('twSlotBody');
  if (body.scrollHeight <= body.clientHeight + 1) return ['the rows do not scroll at this size: the check proves nothing'];
  const bad = [];
  if (body.scrollWidth > body.clientWidth + 0.5) bad.push(`the body scrolls sideways ${body.scrollWidth - body.clientWidth}px`);
  body.scrollTop = body.scrollHeight;
  const seen = el => { const r = el.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= innerHeight && el.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); };
  if (!seen(document.getElementById('twSlotSave'))) bad.push('Save scrolled away or is covered');
  if (!seen(document.getElementById('twSlotTitle'))) bad.push('the title scrolled away or is covered');
  if (dialog.scrollTop !== 0) bad.push('the dialog itself scrolled');
  if (body.scrollTop === 0) bad.push('the rows did not scroll');
  body.scrollTop = 0;
  return bad;
}"""

# Each field's edge against what is behind it, at 3:1 (the refused one's is
# its critical edge); and a time field shows "08:00" whole.
EDGES = r"""() => {
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
  const bad = [];
  const fields = [...document.querySelectorAll('#twSlotEditor input[data-field]')];
  if (!fields.length) return ['no fields'];
  for (const field of fields) {
    const r = ratio(rgba(getComputedStyle(field).borderTopColor), behind(field.parentElement));
    if (r < 3) { bad.push(`${field.dataset.focusKey} edge ${r.toFixed(2)}:1`); break; }
  }
  const invalid = document.querySelector('#twSlotEditor [aria-invalid="true"]');
  if (!invalid) bad.push('no refused field');
  else {
    const style = getComputedStyle(invalid);
    const probe = document.createElement('span');
    probe.style.color = 'var(--sev-critical)';
    invalid.after(probe);
    const critical = getComputedStyle(probe).color;
    probe.remove();
    if (style.borderTopColor !== critical) bad.push(`the refused edge is ${style.borderTopColor}, not ${critical}`);
    const r = ratio(rgba(style.borderTopColor), behind(invalid.parentElement));
    if (r < 3) bad.push(`the refused edge ${r.toFixed(2)}:1`);
  }
  for (const time of document.querySelectorAll('#twSlotEditor .ux-rows-time')) {
    if (time.getBoundingClientRect().width < 75.5) bad.push(`a time field ${time.getBoundingClientRect().width}px`);
    if (time.scrollWidth > time.clientWidth + 0.5) bad.push(`"${time.value}" clipped (${time.scrollWidth} > ${time.clientWidth})`);
    if (getComputedStyle(time).direction !== 'ltr') bad.push('a time field is not left to right');
  }
  return bad;
}"""

# The two lists' tables line up: each column starts and ends at the same x;
# the label's cell takes the room the others leave (the widest); a button that
# cannot act now (Up on the first row) looks it, and one that can does not.
ALIGNED = r"""() => {
  const cells = id => [...document.querySelector(`#${id} tbody tr[data-row-key]`).children];
  const edges = id => cells(id).map(cell => { const r = cell.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; });
  const lectures = edges('twSlotLectures'), labs = edges('twSlotLabs');
  const bad = JSON.stringify(lectures) === JSON.stringify(labs) ? [] : [`columns ${JSON.stringify(lectures)} and ${JSON.stringify(labs)}`];
  const widths = cells('twSlotLabs').map(cell => cell.getBoundingClientRect().width);
  const label = cells('twSlotLabs').findIndex(cell => cell.querySelector('input[data-field="label"]'));
  if (widths.some((width, i) => i !== label && width >= widths[label])) bad.push(`the label's cell is not the widest: ${widths.map(Math.round)}`);
  // ...all of it: a time's cell is its field and the cell's padding, no more.
  const time = document.querySelector('#twSlotLabs tbody tr[data-row-key] .ux-rows-time');
  const spare = time.parentElement.getBoundingClientRect().width - time.getBoundingClientRect().width;
  if (spare > 12.5) bad.push(`a time's cell is ${spare.toFixed(1)}px wider than its field`);
  const row = document.querySelector('#twSlotLabs tbody tr[data-row-key]');
  const opacity = kind => parseFloat(getComputedStyle(row.querySelector(`[data-row-action="${kind}"]`)).opacity);
  if (!(opacity('up') < 0.75) || opacity('down') !== 1) bad.push(`Up (off) at ${opacity('up')}, Down at ${opacity('down')}`);
  return bad;
}"""

# Below 560px each row is two lines: its number and label, then the rest; the
# headers are out of sight but still name the fields; nothing scrolls sideways.
NARROW = r"""() => {
  const bad = [];
  const body = document.getElementById('twSlotBody');
  if (body.scrollWidth > body.clientWidth + 0.5) bad.push(`the body scrolls sideways ${body.scrollWidth - body.clientWidth}px`);
  const head = document.querySelector('#twSlotLectures thead');
  if (head.getBoundingClientRect().height > 1.5) bad.push('the header row is in sight');
  for (const row of document.querySelectorAll('#twSlotDialog tbody tr[data-row-key]')) {
    const box = field => row.querySelector(`input[data-field="${field}"]`).getBoundingClientRect();
    const number = row.querySelector('th').getBoundingClientRect();
    if (box('start').top < box('label').bottom - 0.5) { bad.push('start beside its label, not under it'); break; }
    if (Math.abs(number.top + number.height / 2 - (box('label').top + box('label').height / 2)) > 2) { bad.push('the number not beside its label'); break; }
    if (Math.abs(box('start').top - box('end').top) > 0.5) { bad.push('start and end on two lines'); break; }
  }
  return bad;
}"""

WORDS = [
    "#twSlotDialog .ux-dialog-title",
    "#twSlotDialog .ux-dialog-sub",
    "#twSlotDialog .ux-field-label",
    "#twSlotDialog .ux-rows th",
    "#twSlotDialog .ux-rows-length",
    "#twSlotDialog .pill-status",
    "#twSlotDialog #twSlotFlags",
    "#twSlotDialog .ux-field-error",
    "#twSlotDialog #twSlotAlert",
]

OPEN = "document.getElementById('twSlotDialog').open && document.activeElement?.matches('#twSlotEditor input[data-field=\"label\"]')"
FIRST_END = '#twSlotLectures tbody tr[data-row-key] input[data-field="end"]'
FIRST_LENGTH = (
    "document.querySelector('#twSlotLectures tbody tr[data-row-key] .ux-rows-length').textContent"
)
# Its shown state: the draft's notice, and its sentence that the saved columns changed.
DRAFT_SHOWN = (
    "[!document.getElementById('twSlotDraft').hidden,"
    " !document.getElementById('twSlotDraftChanged').hidden]"
)


def arabic_digits(text: str) -> str:
    """The text with its digits as Arabic-Indic ones (U+0660–0669)."""
    return "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in text)


class SlotDialogTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    """These write, to pytest's database."""

    maxDiff = None

    def _open(self, page) -> None:
        page.click("#twMoreMenu")
        page.click("#twSlotEdit")
        page.wait_for_function(OPEN)

    def _refused(self, page) -> None:
        # The first lecture column ends before it starts: the server refuses
        # its end, which is marked and takes the keyboard.
        page.wait_for_function(
            f"document.activeElement === document.querySelector('{FIRST_END}')"
            " && document.activeElement.getAttribute('aria-invalid') === 'true'"
            " && document.querySelector('.tw-workspace').dataset.busy === 'false'"
        )

    def test_sdb1_the_dialog_reads_keeps_its_foot_and_fits(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                for width, height in ((1366, 650), (1440, 790)):
                    page, sent, errors = self._load(language, theme, width, height)
                    self._open(page)
                    first = "#twSlotLectures tbody tr[data-row-key] input"
                    page.fill(f'{first}[data-field="start"]', "08:00")
                    page.fill(FIRST_END, "07:00")
                    page.click("#twSlotSave")
                    self._refused(page)
                    found = page.evaluate(EDGES)
                    found += page.evaluate(CONTRAST, WORDS)
                    found += page.evaluate(SMALL_TEXT)
                    found += page.evaluate(ALIGNED)
                    found += page.evaluate(FOOT_IN_VIEW)
                    page.keyboard.press("Escape")
                    page.wait_for_function("!document.getElementById('twSlotDialog').open")
                    if page.evaluate("document.activeElement?.id") != "twMoreMenu":
                        found.append(
                            f"focus after closing: {page.evaluate('document.activeElement?.id')}"
                        )
                    saves = [r for r in sent if r.endswith("/slots/update/")]
                    if len(saves) != 1 or len(sent) != 1 or errors:
                        found += [*sent, *errors]
                    if found:
                        problems[f"{language} {theme} {width}x{height}"] = found
                    page.context.close()
        self.assertEqual(problems, {})
        scenario = TimetableScenario.objects.get(pk=self.scenario.pk)
        self.assertEqual(
            (scenario.slot_config, scenario.lab_slot_config), ([], []), "nothing saved"
        )

    def test_sdb4_a_narrow_window_lays_each_row_out_on_two_lines(self) -> None:
        problems: dict[str, list] = {}
        for language in ("en", "ar"):
            page, sent, errors = self._load(language, "light", 1366, 650)
            self._open(page)
            page.set_viewport_size({"width": 520, "height": 800})
            found = page.evaluate(NARROW) + page.evaluate(SMALL_TEXT)
            if sent or errors:
                found += [*sent, *errors]
            if found:
                problems[language] = found
            page.context.close()
        self.assertEqual(problems, {})

    def test_sdb2_the_keyboard_alone_marks_fixes_and_saves(self) -> None:
        for language in ("en", "ar"):
            TimetableScenario.objects.filter(pk=self.scenario.pk).update(
                slot_config=[], lab_slot_config=[]
            )
            page, sent, errors = self._load(language, "light", 1440, 790)
            page.focus("#twMoreMenu")
            page.keyboard.press("Enter")
            page.wait_for_function("document.activeElement?.closest('#twMoreDropdown')")
            for _ in range(5):
                if page.evaluate("document.activeElement?.id") == "twSlotEdit":
                    break
                page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.wait_for_function(OPEN)
            # Label, then start, then end of the first lecture column.
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            self.assertTrue(
                page.evaluate(f"document.activeElement === document.querySelector('{FIRST_END}')"),
                language,
            )
            page.keyboard.press("Control+A")
            page.keyboard.type("08:00")
            page.keyboard.press("Enter")
            self._refused(page)
            said = page.evaluate(
                "document.getElementById(document.activeElement.getAttribute('aria-describedby')).textContent"
            )
            self.assertEqual(
                said,
                "End: must be after the start"
                if language == "en"
                else "النهاية: يجب أن تكون بعد البداية",
                language,
            )
            page.keyboard.press("Control+A")
            page.keyboard.type("10:20" if language == "en" else arabic_digits("10:20"))
            self.assertEqual(
                page.evaluate(FIRST_LENGTH),
                "80 min" if language == "en" else "80 دقيقةً",
                language,
            )
            page.keyboard.press("Enter")
            page.wait_for_function(
                "!document.getElementById('twSlotDialog').open"
                " && document.querySelector('.tw-workspace').dataset.busy === 'false'"
            )
            self.assertEqual(page.evaluate("document.activeElement?.id"), "twMoreMenu", language)
            scenario = TimetableScenario.objects.get(pk=self.scenario.pk)
            self.assertEqual(
                scenario.slot_config[0],
                {"label": "09:00-10:15", "start": "09:00", "end": "10:20"},
                language,
            )
            self.assertEqual(len(scenario.slot_config), 7, language)
            self.assertEqual(
                [slot["label"] for slot in scenario.lab_slot_config if slot.get("online_only")],
                ["Online 1", "Online 2", "Online 3"],
                f"{language}: the online-only windows keep their rule",
            )
            self.assertEqual(
                [r for r in sent if not r.endswith(f"/scenarios/{self.scenario.pk}/slots/update/")],
                [],
                language,
            )
            self.assertEqual(len(sent), 2, language)
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_sdb3_removing_a_column_that_holds_a_meeting_is_reviewed_and_parks_it(self) -> None:
        key = fx.requirement("AI", "AI333", "Planning")
        fx.budget(self.scenario, key, 1)
        fx.place(
            self.scenario.boards.get(),
            fx.section(self.scenario, key, "S1"),
            (("MON", "16:00", "17:15"),),
            room="R101",
        )
        late = SectionPlacement.objects.get(term_section__course_code="AI333")
        for language in ("en", "ar"):
            TimetableScenario.objects.filter(pk=self.scenario.pk).update(
                slot_config=[], lab_slot_config=[]
            )
            page, sent, errors = self._load(language, "light", 1440, 790, cards=5)
            self.assertTrue(
                page.evaluate(
                    f"Boolean(document.querySelector('#twGrid .tw-cell .tw-card[data-placement-id=\"{late.id}\"]'))"
                ),
                f"{language}: drawn in its cell before",
            )
            self._open(page)
            row = page.evaluate(
                """() => [...document.querySelectorAll('#twSlotLectures tbody tr[data-row-key]')]
                  .findIndex(tr => tr.querySelector('input[data-field="start"]').value === '16:00')"""
            )
            self.assertEqual(row, 6, language)
            page.click(
                f"#twSlotLectures tbody tr[data-row-key]:nth-of-type({2 * row + 1}) [data-row-action='remove']"
            )
            page.click("#twSlotSave")
            page.wait_for_function(
                "!document.getElementById('twSlotReview').hidden"
                " && document.activeElement?.id === 'twSlotReviewTitle'"
            )
            review = page.inner_text("#twSlotReviewBody")
            self.assertIn("AI333 S1", review, language)
            self.assertIn("16:00–17:15", review, language)
            self.assertEqual(
                page.evaluate(
                    "document.querySelectorAll('#twSlotReviewBody bdi[dir=\"ltr\"]').length"
                ),
                1,
                language,
            )
            page.click("#twSlotSave")
            page.wait_for_function(
                """id => !document.getElementById('twSlotDialog').open
                  && document.querySelector(`#twGrid .tw-park .tw-card[data-placement-id="${id}"]`)
                  && document.querySelector('.tw-workspace').dataset.busy === 'false'""",
                arg=late.id,
            )
            scenario = TimetableScenario.objects.get(pk=self.scenario.pk)
            self.assertEqual(
                [slot["start"] for slot in scenario.slot_config],
                ["09:00", "10:30", "10:50", "13:00", "14:30", "14:45"],
                language,
            )
            self.assertEqual(len(sent), 2, language)
            self.assertEqual(errors, [], language)
            page.context.close()

    def test_sdb5_a_draft_older_than_the_saved_columns_says_so(self) -> None:
        problems: dict[str, list] = {}
        first_label = '#twSlotLectures tbody tr[data-row-key] input[data-field="label"]'
        for language in ("en", "ar"):
            for theme in ("light", "dark"):
                TimetableScenario.objects.filter(pk=self.scenario.pk).update(
                    slot_config=[], lab_slot_config=[]
                )
                page, sent, errors = self._load(language, theme, 1440, 790)
                found: list = []
                self._open(page)
                page.fill(first_label, "Morning")
                page.keyboard.press("Escape")
                page.wait_for_function("!document.getElementById('twSlotDialog').open")
                # The board read again, nothing saved meanwhile: the draft, as current.
                page.evaluate("selectBoard(TW.boardId)")
                self._open(page)
                if page.evaluate(DRAFT_SHOWN) != [True, False]:
                    found.append(f"unchanged: {page.evaluate(DRAFT_SHOWN)}")
                page.keyboard.press("Escape")
                page.wait_for_function("!document.getElementById('twSlotDialog').open")
                # Saved elsewhere (as split view saves): the last lecture column gone.
                saved = page.evaluate("TW.scenario.effective_slot_config")
                TimetableScenario.objects.filter(pk=self.scenario.pk).update(slot_config=saved[:-1])
                page.evaluate("selectBoard(TW.boardId)")
                self._open(page)
                if page.evaluate(DRAFT_SHOWN) != [True, True]:
                    found.append(f"changed: {page.evaluate(DRAFT_SHOWN)}")
                if page.input_value(first_label) != "Morning":
                    found.append("the draft is not the rows shown")
                found += page.evaluate(CONTRAST, ["#twSlotDialog #twSlotDraftChanged"])
                found += page.evaluate(SMALL_TEXT)
                page.click("#twSlotFromSaved")
                rows = page.evaluate(
                    "[...document.querySelectorAll('#twSlotLectures tbody tr[data-row-key]')]"
                    ".map(tr => tr.querySelector('input[data-field=\"label\"]').value)"
                )
                if rows != [slot["label"] for slot in saved[:-1]]:
                    found.append(f"from the saved columns: {rows}")
                if page.evaluate(DRAFT_SHOWN)[0]:
                    found.append("the notice stayed")
                if sent or errors:
                    found += [*sent, *errors]
                if found:
                    problems[f"{language} {theme}"] = found
                page.context.close()
        self.assertEqual(problems, {})
