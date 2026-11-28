"""The planner board, zone 03 in a real browser: one colour per planned course, the graduation
header, "Option C1 at a glance" and the apply bar's own words.

The same synthetic student and fakes as the other parts (tests/planner_board_support.py). The plan
is AI331 and AI342; B2 leaves AI342 out, C1 and B1 place both.
"""

from __future__ import annotations

import re

from tests.planner_board_support import (
    READY,
    BuildFake,
    PlannerBoardCase,
    expect,
)

FILLED = """() => {
  const alpha = (c) => {
    if (c === 'transparent') return 0;
    const body = c.slice(c.indexOf('(') + 1, c.lastIndexOf(')'));
    if (body.includes('/')) return Number(body.split('/')[1]);
    const parts = body.split(',');
    return parts.length === 4 ? Number(parts[3]) : 1;
  };
  return [...document.querySelectorAll('.plb .btn')]
    .filter((b) => b.getClientRects().length && alpha(getComputedStyle(b).backgroundColor) >= 0.95)
    .map((b) => b.id || b.textContent.trim());
}"""
TINTS = """() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgb = (v) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    return [...cx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
  const lum = (c) => c.map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const root = document.querySelector('.plb');
  const probe = document.createElement('span');
  root.append(probe);
  const out = [];
  for (let i = 1; i <= 6; i += 1) {
    probe.style.background = `var(--plb-c${i}-t)`;
    const tint = rgb(getComputedStyle(probe).backgroundColor);
    for (const token of ['--ink', '--t2']) {
      probe.style.color = `var(${token})`;
      out.push({ hue: i, token, ratio: +ratio(tint, rgb(getComputedStyle(probe).color)).toFixed(2) });
    }
  }
  probe.remove();
  return out;
}"""
HUES_OF_AI331 = """() => {
  const hue = (selector) => { const n = document.querySelector(selector); return n ? n.dataset.hue || null : 'missing'; };
  return {
    row: hue("#plbPlanList .plb-prow[data-code='AI331'] .plb-sw"),
    block: hue("#plbWeek .plb-b[data-kind='new'][data-code='AI331']"),
    glance: hue("#plbSecs .plb-sr[data-code='AI331'] .plb-sw"),
    row2: hue("#plbPlanList .plb-prow[data-code='AI342'] .plb-sw"),
    block2: hue("#plbWeek .plb-b[data-kind='new'][data-code='AI342']"),
    glance2: hue("#plbSecs .plb-sr[data-code='AI342'] .plb-sw"),
  };
}"""


class PlannerBoardOptionsTests(PlannerBoardCase):
    start_on = "C1"

    @staticmethod
    def _glance(page) -> dict:
        return page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('#plbGlance .plb-fact')]"
            ".map(f => [f.querySelector('dt').textContent.trim(),"
            " f.querySelector('dd').textContent.replace(/\\s+/g, ' ').trim()]))"
        )

    def _projected_board(self, **kwargs):
        fake = BuildFake()
        fake.projection_answer = lambda body: (200, READY)
        page, fake = self._built(fake, **kwargs)
        expect(page.locator("#plbGradLine")).to_be_hidden()
        return page

    # ── 1. One colour per planned course ─────────────────────────────────────────────────
    def test_a_course_has_the_same_colour_in_its_row_its_week_block_and_the_glance(self) -> None:
        page, _fake = self._built()
        hues = page.evaluate(HUES_OF_AI331)
        self.assertEqual(hues["row"], hues["block"])
        self.assertEqual(hues["row"], hues["glance"])
        self.assertEqual(hues["row2"], hues["block2"])
        self.assertEqual(hues["row2"], hues["glance2"])
        self.assertEqual((hues["row"], hues["row2"]), ("1", "2"))
        # another tab, another week: the colours stay with the courses
        self._pick(page, "B1")
        self.assertEqual(page.evaluate(HUES_OF_AI331), hues)

    def test_the_planned_hues_clear_contrast_in_both_themes(self) -> None:
        page, _fake = self._built()
        for theme in ("light", "dark"):
            page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
            page.wait_for_function("() => document.getAnimations().length === 0")
            for item in page.evaluate(TINTS):
                self.assertGreaterEqual(item["ratio"], 4.5, msg=(theme, item))

    def test_a_clash_keeps_priority_over_the_hue(self) -> None:
        page, _fake = self._built(clock=False)
        self._pick(page, "C1")
        # the same node is both hued and (when it clashes) outlined: the outline is not removed
        outline = page.evaluate(
            "() => { const b = document.querySelector('#plbWeek .plb-b[data-kind=\"new\"]');"
            " b.dataset.clash = 'true'; return getComputedStyle(b).outlineStyle + ' ' + getComputedStyle(b).outlineWidth; }"
        )
        self.assertEqual(outline, "solid 2px")

    # ── 2. The graduation header ─────────────────────────────────────────────────────────
    def test_the_graduation_header_names_the_option_its_term_and_the_timing(self) -> None:
        page = self._projected_board()
        expect(page.locator("#plbGradMic")).to_have_text("Graduates with C1")
        expect(page.locator("#plbGradTerm")).to_have_text(re.compile(r"^Term \d, \d{4}$"))
        expect(page.locator("#plbGradPill")).to_have_text("1 term sooner")
        self._pick(page, "B2")
        expect(page.locator("#plbGradPill")).to_have_text("Same term")
        page.click("#plbTabRegistered")
        expect(page.locator("#plbGradMic")).to_have_text("As registered")
        expect(page.locator("#plbGradPill")).to_be_hidden()

    def test_before_a_forecast_the_header_is_not_drawn(self) -> None:
        page, _fake = self._built()
        expect(page.locator("#plbGradTop")).to_be_hidden()

    # ── 3. Option at a glance ────────────────────────────────────────────────────────────
    def test_the_glance_facts_match_the_tab_facts_of_two_options(self) -> None:
        page, _fake = self._built(width=1440)
        expect(page.locator("#plbGlanceTitle")).to_have_text("Option C1 at a glance")
        self.assertEqual(
            self._glance(page),
            {
                "Load": "15 of 18 cr",
                "On campus": "4 days",
                "Free day": "Thursday",
                "Last class ends": "14:00",
            },
        )
        self.assertEqual(self._tab_lines(page, "C1")[1], "4 days · Thu free")
        self.assertTrue(self._tab_lines(page, "C1")[2].startswith("ends 14:00"))
        self._pick(page, "B3")
        self.assertEqual(
            self._glance(page),
            {
                "Load": "15 of 18 cr",
                "On campus": "5 days",
                "Free day": "none",
                "Last class ends": "16:15",
            },
        )
        self.assertIn("5 days", self._tab_lines(page, "B3")[1])
        self.assertIn("ends 16:15", self._tab_lines(page, "B3")[2])
        page.click("#plbTabRegistered")
        expect(page.locator("#plbGlanceTitle")).to_have_text("Registered week at a glance")

    def test_the_glance_lists_the_sections_and_the_kept_registered_rows_last(self) -> None:
        page, _fake = self._built()
        rows = page.eval_on_selector_all(
            "#plbSecs .plb-sr",
            "rs => rs.map(r => [r.dataset.code, r.classList.contains('plb-sr--kept')])",
        )
        planned = [code for code, kept in rows if not kept]
        self.assertEqual(planned, ["AI331", "AI342"])
        kinds = [kept for _code, kept in rows]
        self.assertEqual(kinds, sorted(kinds))
        self.assertIn(True, kinds)
        self.assertEqual(page.locator("#plbSecs .plb-sch[data-only='true']").count(), 0)

    def test_the_guidance_and_the_why_sentence_live_inside_the_glance(self) -> None:
        page, _fake = self._built()
        self.assertGreater(page.locator("#plbGlance #plbGuide .plb-fr").count(), 0)
        self.assertEqual(page.locator("#plbGrad #plbGuide").count(), 0)
        self.assertEqual(page.locator("#plbGlance #plbWhyBtn").count(), 1)
        self.assertEqual(page.locator("#plbGlance #plbWhy").count(), 1)
        # beside the week, 320 px wide, on a wide page
        box = page.locator("#plbGlance").bounding_box()
        week = page.locator("#plbWeekPanel").bounding_box()
        self.assertAlmostEqual(box["width"], 320, delta=1)
        self.assertGreater(box["x"], week["x"] + week["width"] - 1)

    # ── 4. The apply bar ─────────────────────────────────────────────────────────────────
    def test_the_apply_bar_says_which_courses_are_not_placed(self) -> None:
        page, _fake = self._built(width=1440)
        expect(page.locator("#plbApplyStatus")).to_have_text("All planned courses placed")
        expect(page.locator("#plbApplySum")).to_have_text(
            "Option C1 · 15 of 18 cr · 4 days · Thursday off"
        )
        self._pick(page, "B2")
        expect(page.locator("#plbApplyStatus")).to_have_text("1 not placed: AI342")
        self.assertEqual(page.get_attribute("#plbApplyStatus", "data-kind"), "warn")
        page.click("#plbTabRegistered")
        expect(page.locator("#plbApplyStatus")).to_be_hidden()
        expect(page.locator("#plbApplySum")).to_be_hidden()

    def test_before_a_build_the_bar_holds_only_the_nothing_is_saved_note(self) -> None:
        page = self._open(BuildFake())
        self.assertTrue(page.eval_on_selector("#plbApplyStatus", "n => n.hidden"))
        self.assertTrue(page.eval_on_selector("#plbApplySum", "n => n.hidden"))
        expect(page.locator("#plbActs .plb-acts-safe")).to_be_visible()
        expect(page.locator("#plbReview")).to_have_text("Review and apply")

    def test_the_review_button_follows_the_selected_tab(self) -> None:
        page, _fake = self._built()
        expect(page.locator("#plbReview")).to_have_text("Review and apply C1")
        expect(page.locator("#plbReview")).to_be_enabled()
        self._pick(page, "B1")
        expect(page.locator("#plbReview")).to_have_text("Review and apply B1")
        page.click("#plbTabRegistered")
        expect(page.locator("#plbReview")).to_have_text("Review and apply")
        expect(page.locator("#plbReview")).to_be_disabled()

    def test_exactly_one_button_is_filled_before_and_after_a_build(self) -> None:
        page = self._open(BuildFake())
        page.wait_for_function("() => document.getAnimations().length === 0")
        self.assertEqual(page.evaluate(FILLED), ["plbBuild"])
        expect(page.locator("#plbBuild")).to_have_text("Build options")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild options")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self._start(page)
        page.wait_for_function("() => document.getAnimations().length === 0")
        self.assertEqual(page.evaluate(FILLED), ["plbReview"])

    # ── Language and size ────────────────────────────────────────────────────────────────
    def test_the_arabic_words_of_the_zone(self) -> None:
        page = self._projected_board(language="ar")
        self.assertIn("يتخرّج مع", self._text(page, "#plbGradMic"))
        self.assertIn("في لمحة", self._text(page, "#plbGlanceTitle"))
        self.assertIn("وُضعت جميع المقررات المخطَّطة", self._text(page, "#plbApplyStatus"))
        self.assertIn("مراجعة وتطبيق", self._text(page, "#plbReview"))
        self.assertEqual(
            page.eval_on_selector_all(
                "#plbGlance .plb-fact dt", "n => n.map(x => x.textContent.trim())"
            ),
            ["الحمل", "في الحرم", "اليوم الفارغ", "آخر محاضرة تنتهي"],
        )

    def test_a_phone_stacks_the_glance_under_the_week_without_overflow(self) -> None:
        for language in ("en", "ar"):
            page = self._projected_board(width=390, height=900, language=language)
            wide = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            self.assertLessEqual(wide, 0, msg=language)
            box = page.locator("#plbGlance").bounding_box()
            week = page.locator("#plbWeekPanel").bounding_box()
            self.assertGreaterEqual(box["y"], week["y"] + week["height"] - 1, msg=language)
            self.assertLessEqual(box["width"], 390, msg=language)

    # ── 4. Review fixes: stable colours, the build's own cap, words not colour alone ───────
    def test_a_course_keeps_its_colour_when_another_is_removed(self) -> None:
        page, _fake = self._built()
        before = page.evaluate(HUES_OF_AI331)
        self.assertEqual((before["row"], before["row2"]), ("1", "2"))
        self._remove(page, "AI331")
        self.assertEqual(page.evaluate(HUES_OF_AI331)["row2"], "2")

    def test_the_glance_reads_the_cap_of_the_build_not_the_live_rule(self) -> None:
        page = self._open(BuildFake())
        page.fill("#plbMaxIn", "18")
        page.press("#plbMaxIn", "Enter")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self._start(page)
        self.assertIn("of 18 cr", str(self._glance(page)))
        page.fill("#plbMaxIn", "16")
        page.press("#plbMaxIn", "Enter")
        self._pick(page, "B1")
        text = str(self._glance(page))
        self.assertIn("of 18 cr", text)
        self.assertNotIn("of 16 cr", text)

    def test_kept_rows_carry_a_word_tag(self) -> None:
        page, _fake = self._built()
        tags = page.eval_on_selector_all(
            "#plbSecs .plb-sr--kept .plb-kt", "ns => ns.map(n => n.textContent.trim())"
        )
        self.assertTrue(tags)
        self.assertEqual(set(tags), {"registered"})
