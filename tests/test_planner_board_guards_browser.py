"""The planner board, guarded in a real browser: deep links, contrast, false affordances, eligibility.

These replace what the retired page's tests pinned and the board's own tests did not: the deep link
(`/planner/?student=...`), the contrast of every chip and flag once a student is loaded and built,
a hand cursor on something that is not clickable, and `eligibility_mode` on each student request.
The same synthetic student and fakes as the other board tests (tests/planner_board_support.py).
"""

from __future__ import annotations

from tests.planner_board_support import (
    STUDENT_ID,
    BuildFake,
    PlannerBoardCase,
    expect,
)
from tests.test_planner_board_detail_browser import DETAIL_CATALOG

# Every visible text on the board, measured against the background it really sits on: walk up
# through transparent backgrounds (translucent layers are composited), and fold in the opacity of
# the element and its ancestors. Disabled controls are exempt (WCAG), as are inputs' own values.
CONTRAST = r"""() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const over = (top, under) => top.slice(0, 3).map((x, i) => x * top[3] + under[i] * (1 - top[3]));
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + [...el.classList].slice(0, 2).map(c => '.' + c).join('');
  let imageOn = null;
  const backdrop = el => {
    const layers = [];
    for (let a = el; a; a = a.parentElement) {
      const cs = getComputedStyle(a);
      // A select's own arrow is a url() image at its edge, never under its text: allowed on the
      // control itself. A gradient, or any image on an ancestor, is not.
      const arrow = a === el && /^(SELECT|INPUT)$/.test(el.tagName) && /^url\(/.test(cs.backgroundImage);
      if (cs.backgroundImage !== 'none' && !arrow && !imageOn) imageOn = name(a);
      const c = rgba(cs.backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let base = layers.length && layers[layers.length - 1][3] >= 1 ? layers.pop().slice(0, 3) : [255, 255, 255];
    while (layers.length) base = over(layers.pop(), base);
    return base;
  };
  const opacity = el => { let o = 1; for (let a = el; a; a = a.parentElement) o *= +getComputedStyle(a).opacity; return o; };
  const out = [];
  const roots = [...document.querySelectorAll('.plb, #plbLoad, dialog[open]')];
  const seen = new Set();
  for (const root of roots) for (const el of [root, ...root.querySelectorAll('*')]) {
    if (seen.has(el) || /^(SCRIPT|STYLE|SVG|PATH|OPTION)$/i.test(el.tagName)) continue;
    seen.add(el);
    // A form control is measured as a whole (its value and its own background); anything else by
    // its own text.
    const control = /^(INPUT|SELECT|TEXTAREA)$/i.test(el.tagName) && !/^(checkbox|radio|hidden)$/i.test(el.type);
    const own = control || [...el.childNodes].some(n => n.nodeType === 3 && n.data.trim());
    if (!own) continue;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (el.closest('[disabled], [aria-disabled="true"], [hidden]')) continue;
    imageOn = null;
    const bg = backdrop(el);
    const f = rgba(cs.color); f[3] *= opacity(el);
    const fg = over(f, bg);
    const a = lum(fg), b = lum(bg);
    const label = control ? (el.value || el.placeholder || '') : el.textContent.trim();
    if (imageOn) {
      out.push({ what: name(el) + ' sits on a background-image (' + imageOn + '): the contrast cannot be measured', ratio: 0 });
      continue;
    }
    out.push({ what: name(el) + ' "' + label.slice(0, 24) + '"',
               ratio: +((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)).toFixed(2) });
  }
  return out;
}"""

# Every element on the board that shows a hand cursor although nothing there is clickable.
FALSE_AFFORDANCES = r"""() => {
  const clickable = 'button, a[href], summary, label, input, select, textarea, option, [role="button"], '
    + '[role="tab"], [role="link"], [role="checkbox"], [role="radio"], [role="menuitem"], [role="switch"]';
  const found = [];
  const seen = new Set();
  for (const root of document.querySelectorAll('.plb, #plbLoad, dialog[open]')) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (seen.has(el)) continue;
      seen.add(el);
      if (getComputedStyle(el).cursor !== 'pointer' || el.closest(clickable)) continue;
      const cls = [...el.classList].slice(0, 2).map(c => '.' + c).join('');
      found.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls);
    }
  }
  return [...new Set(found)];
}"""

LANGUAGES = ("en", "ar")
THEMES = ("light", "dark")


def _fake() -> BuildFake:
    fake = BuildFake()
    fake.catalog_payload = DETAIL_CATALOG
    return fake


class PlannerBoardDeepLinkTests(PlannerBoardCase):
    def _go(self, query: str):
        fake = BuildFake()
        base = fake.context_answer

        def echo(body):
            status, payload = base(body)
            return status, {**payload, "year": body["academic_year"], "term": body["term"]}

        fake.context_answer = echo
        page = self._open(fake, load=False)
        default_year = page.input_value("#plbYear")
        default_term = page.input_value("#plbTerm")
        page.goto(f"{self.live_server_url}/planner/{query}")
        expect(page.locator("#plbContext")).to_be_visible()
        return page, fake, default_year, default_term

    def test_a_student_link_without_year_or_term_uses_the_pages_defaults(self) -> None:
        page, fake, year, term = self._go(f"?student={STUDENT_ID}")
        self.assertTrue(year and term, (year, term))
        self.assertEqual(fake.context_bodies[-1]["academic_year"], year)
        self.assertEqual(fake.context_bodies[-1]["term"], term)
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)
        self.assertEqual(page.input_value("#plbStudentId"), STUDENT_ID)
        self.assertEqual(page.input_value("#plbYear"), year)
        self.assertEqual(page.input_value("#plbTerm"), term)
        self.assertEqual(page.inner_text("#plbContext").strip(), f"{STUDENT_ID} · {year}/{term}")

    def test_sid_with_academic_year_and_term_opens_that_term(self) -> None:
        page, fake, _, _ = self._go(f"?sid={STUDENT_ID}&academic_year=1447&term=2")
        self.assertEqual(fake.context_bodies[-1]["academic_year"], "1447")
        self.assertEqual(fake.context_bodies[-1]["term"], "2")
        self.assertEqual(page.input_value("#plbYear"), "1447")
        self.assertEqual(page.input_value("#plbTerm"), "2")
        self.assertEqual(page.inner_text("#plbContext").strip(), f"{STUDENT_ID} · 1447/2")

    def test_student_id_with_year_and_term_opens_that_term(self) -> None:
        page, fake, _, _ = self._go(f"?student_id={STUDENT_ID}&year=1447&term=2")
        self.assertEqual(fake.context_bodies[-1]["academic_year"], "1447")
        self.assertEqual(fake.context_bodies[-1]["term"], "2")
        self.assertEqual(fake.context_bodies[-1]["student_id"], STUDENT_ID)
        self.assertEqual(page.inner_text("#plbContext").strip(), f"{STUDENT_ID} · 1447/2")


class PlannerBoardEligibilityRequestTests(PlannerBoardCase):
    def test_each_student_request_carries_the_eligibility_the_rules_hold(self) -> None:
        fake = _fake()
        page = self._open(fake)
        # The rules default to strict, and the page says so.
        expect(page.locator("#plbEligStrict")).to_be_checked()
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "strict")
        self.assertTrue(fake.plan_urls and "eligibility_mode=strict" in fake.plan_urls[-1])
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertEqual(fake.build_bodies[-1]["eligibility_mode"], "strict")
        # Relaxed: the suggestions reload with it, on both student requests, and so does a build.
        page.click("#plbEligRelaxed")
        expect(page.locator("#plbEligRelaxed")).to_be_checked()
        # The reload asks for the student, then for the plan: wait for the second request.
        for _ in range(250):
            if fake.plan_urls and "eligibility_mode=relaxed" in fake.plan_urls[-1]:
                break
            page.wait_for_timeout(20)
        self.assertEqual(fake.context_bodies[-1]["eligibility_mode"], "relaxed")
        self.assertIn("eligibility_mode=relaxed", fake.plan_urls[-1])
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self.assertEqual(fake.build_bodies[-1]["eligibility_mode"], "relaxed")
        # The sections catalogue is the one student request that takes no eligibility: it is asked
        # by the courses and the programme rule only.
        self.assertTrue(fake.catalog_bodies)
        self.assertTrue(all("eligibility_mode" not in body for body in fake.catalog_bodies))


class PlannerBoardAuditTests(PlannerBoardCase):
    """Contrast and false affordances, in both themes and both languages, loaded and built."""

    def test_the_harness_stops_the_stylesheets_own_transitions(self) -> None:
        # global.css gives every button an `!important` transition (`html body .btn`): the tests'
        # no-motion rule must still win, or a colour read can catch a button mid-fade.
        page = self._open(BuildFake())
        durations = page.eval_on_selector_all(
            ".btn", "bs => [...new Set(bs.map(b => getComputedStyle(b).transitionDuration))]"
        )
        self.assertEqual(durations, ["0s"])

    def _states(self, page, script):
        """Yield a label for each state of the board worth measuring, once it has settled."""
        self._settle(page, script)
        yield "loaded"
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        tabs = page.locator("#plbTabs [role='tab']")
        expect(tabs.first).to_be_visible()
        for index in range(tabs.count()):
            tabs.nth(index).click()
            expect(tabs.nth(index)).to_have_attribute("aria-selected", "true")
            # The projection band has answered (the fake answers at once).
            expect(page.locator("#plbGrad")).not_to_have_attribute("data-state", "loading")
            self._settle(page, script)
            yield f"tab {index}"
        button = "#plbTable tr[data-code='AI331'] button[data-act='detail']"
        if page.locator(button).count():
            page.click(button)
            expect(page.locator("#plbDetail")).to_be_visible()
            self._settle(page, script)
            yield "detail AI331"

    @staticmethod
    def _settle(page, script: str) -> None:
        """Wait until no animation or transition runs and the audit answers the same in two
        consecutive animation frames. NO_MOTION stops them; this stays as the guard in case one
        slips past it."""
        page.evaluate("window.__guardPrev = null")
        page.wait_for_function(
            "(src) => { if (document.getAnimations().length) return false;"
            " const now = JSON.stringify(eval('(' + src + ')')());"
            " const same = window.__guardPrev === now; window.__guardPrev = now; return same; }",
            arg=script,
            polling="raf",
        )

    def _theme(self, page, theme: str, script: str) -> None:
        page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
        self._settle(page, script)

    def _audit(self, language: str, script: str, *, inject: str = "") -> list[str]:
        page = self._open(_fake(), language=language)
        if inject:
            page.add_style_tag(content=inject)
        found: list[str] = []
        for state in self._states(page, script):
            for theme in THEMES:
                self._theme(page, theme, script)
                result = page.evaluate(script)
                for item in result:
                    if isinstance(item, str):
                        found.append(f"{language} {theme} {state}: {item} shows a hand cursor")
                    elif item["ratio"] < 4.5:
                        found.append(
                            f"{language} {theme} {state}: {item['what']} {item['ratio']}:1"
                        )
            self._theme(page, "light", script)
        return sorted(set(found))

    def test_every_chip_flag_and_label_clears_contrast_in_both_themes(self) -> None:
        for language in LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(self._audit(language, CONTRAST), [])

    def test_nothing_shows_a_hand_cursor_unless_it_is_clickable(self) -> None:
        for language in LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(self._audit(language, FALSE_AFFORDANCES), [])

    # The two audits are only worth having if they can fail: each is run against a board with one
    # deliberate defect injected, and must name it.
    def test_the_contrast_audit_catches_a_flag_a_control_and_a_tab_line(self) -> None:
        low = "color: #c9c9c9 !important;"
        for what, css, needles in (
            ("flag", f".plb .plb-flag, .plb .plb-few {{ {low} }}", ("plb-flag", "plb-few")),
            (
                "control",
                f"#plbStudentId {{ {low} background: #fff !important; }}",
                ("plbStudentId",),
            ),
            ("tab meta line", f"#plbTabs [role='tab'] > span {{ {low} }}", ("span",)),
        ):
            with self.subTest(defect=what):
                found = self._audit("en", CONTRAST, inject=css)
                self.assertTrue(any(n in line for line in found for n in needles), found)

    def test_the_contrast_audit_refuses_a_background_image(self) -> None:
        found = self._audit(
            "en",
            CONTRAST,
            inject=".plb .plb-tab { background-image: linear-gradient(#fff, #000); }",
        )
        self.assertTrue(any("background-image" in line for line in found), found)

    def test_the_cursor_audit_catches_a_pointer_on_a_plain_span(self) -> None:
        found = self._audit("en", FALSE_AFFORDANCES, inject=".plb .plb-code { cursor: pointer; }")
        self.assertTrue(any("plb-code" in line for line in found), found)
