"""The new planner screen, what each course does to graduation, in a real browser.

The same synthetic student and fakes as the other parts (tests/planner_board_support.py). The
projection reply carries `course_effects` per option: what postponing each planned course would
do, and what adding each "Could be added" course would do. The screen prints those on the
course rows, in the guidance column ("Depends on") and in the detail panel.
"""

from __future__ import annotations

import copy

from tests.planner_board_support import (
    OVERFLOWS,
    READY,
    REG_FORECAST,
    STUDENT_ID,
    STUDENT_PLAN,
    BuildFake,
    PlannerBoardCase,
    _plan_course,
    expect,
    forecast,
    projection_reply,
)

NEW = "#plbTable tbody[data-group='new'] tr[data-code='{}'] .plb-fxt"
COULD = "#plbTable tbody[data-group='could'] tr[data-code='{}'] .plb-fxt"


def effect(terms, delta):
    return {"status": "complete", "terms_including_planning_term": terms, "delta_terms": delta}


NO_EFFECT = {"status": "unavailable", "reason_code": "unmet_course_prerequisites"}


def effects(postponed=None, added=None):
    return {"course_effects": {"postponed": postponed or {}, "added": added or {}}}


# B1 finishes in four terms. Postponing AI331 costs a term, AI342 nothing; adding STAT305 changes
# nothing, CS351 would finish a term sooner.
B1_EFFECTS = effects(
    {"AI331": effect(5, 1), "AI342": effect(4, 0)},
    {"STAT305": effect(4, 0), "CS351": effect(3, -1)},
)


def reply(**per_option):
    options = dict(READY["options"])
    options.update(per_option)
    return projection_reply(REG_FORECAST, options)


def with_effects(extra, **kwargs):
    return reply(B1=forecast([15, 18, 18, 15], delta=-1, **extra), **kwargs)


STANDARD = with_effects(B1_EFFECTS)
# Every option carries effects, so a tab that wrongly borrows another option's has something to show.
EVERY = reply(
    **{
        name: forecast([15, 18, 18, 15], delta=-1, **B1_EFFECTS)
        for name in ("B1", "B2", "B3", "C1", "C2")
    }
)


class PlannerBoardEffectsBrowserTests(PlannerBoardCase):
    start_on = "B1"

    def _fx(self, page, selector: str) -> str:
        return self._text(page, selector) if page.locator(selector).count() else ""

    def _open_panel(self, page, code: str) -> None:
        page.click(f"#plbTable tr[data-code='{code}'] .plb-sc-btn")
        expect(page.locator("#plbDetail")).to_be_visible()

    # ── the request ──────────────────────────────────────────────────────────
    def test_the_request_names_the_could_be_added_rows_in_the_tables_order(self) -> None:
        page, fake = self._projected(STANDARD)
        rows = self._rows(page, "could")
        self.assertEqual(rows, ["STAT305", "CS351"])
        self.assertEqual(fake.projection_bodies, [{"token": "token", "add": rows}])

    def test_at_most_six_codes_and_the_first_six_of_the_table(self) -> None:
        plan = copy.deepcopy(STUDENT_PLAN)
        extra = [f"EL{n}00" for n in range(1, 8)]
        plan["terms"][0]["courses"] += [_plan_course(c, c, "not_taken", True) for c in extra]
        fake = BuildFake()
        fake.plan_payload = plan
        page, fake = self._projected(STANDARD, fake=fake)
        rows = self._rows(page, "could")
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(fake.projection_bodies[0]["add"]), 6)
        self.assertEqual(fake.projection_bodies[0]["add"], rows[:6])

    def test_no_add_when_no_row_could_be_added(self) -> None:
        plan = copy.deepcopy(STUDENT_PLAN)
        plan["terms"][0]["courses"] = [
            c for c in plan["terms"][0]["courses"] if c["course_code"] not in ("STAT305", "CS351")
        ]
        fake = BuildFake()
        fake.plan_payload = plan
        fake.projection_answer = lambda body: (200, STANDARD)
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbTable tbody[data-group='blocked']")).to_be_visible()
        page.click("#plbBuild")
        self._wait_until(page, lambda: len(fake.projection_bodies) == 1)
        self.assertEqual(fake.projection_bodies, [{"token": "token"}])

    @staticmethod
    def _wait_until(page, predicate) -> None:
        for _ in range(250):
            if predicate():
                return
            page.wait_for_timeout(20)
        raise AssertionError("timed out")

    def test_a_change_to_the_list_after_the_build_does_not_ask_again(self) -> None:
        page, fake = self._projected(STANDARD)
        page.click("#plbTable tbody[data-group='could'] tr[data-code='STAT305'] .plb-ib--add")
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.projection_bodies), 1)

    def test_a_build_made_before_the_plan_arrived_asks_again_naming_the_rows(self) -> None:
        fake = BuildFake()
        fake.hold_plan = 1
        fake.projection_answer = lambda body: (200, STANDARD)
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbBuild")).to_be_enabled()
        page.click("#plbBuild")
        self._wait_until(page, lambda: len(fake.projection_bodies) == 1)
        self.assertEqual(fake.projection_bodies, [{"token": "token"}])
        self._start(page)
        expect(page.locator(NEW.format("AI331"))).to_have_text("If postponed: 5 terms")
        fake.pending_plan.pop().fulfill(json=fake.plan_payload)
        self._wait_until(page, lambda: len(fake.projection_bodies) == 2)
        self.assertEqual(fake.projection_bodies[1], {"token": "token", "add": ["STAT305", "CS351"]})
        expect(page.locator(COULD.format("CS351"))).to_have_text("3 terms, 1 sooner")
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.projection_bodies), 2)

    def test_an_expired_forecast_is_not_asked_again_when_the_plan_arrives(self) -> None:
        fake = BuildFake()
        fake.hold_plan = 1
        fake.projection_raw = lambda route: route.fulfill(status=409, json={"error": "expired"})
        page = self._open(fake, load=False)
        self._fetch(page, STUDENT_ID)
        expect(page.locator("#plbBuild")).to_be_enabled()
        page.click("#plbBuild")
        self._wait_until(page, lambda: len(fake.projection_bodies) == 1)
        expect(page.locator("#plbGrad")).to_have_attribute("data-state", "expired")
        fake.pending_plan.pop().fulfill(json=fake.plan_payload)
        expect(
            page.locator("#plbTable tbody[data-group='could'] tr[data-code='CS351']")
        ).to_be_visible()
        page.wait_for_timeout(200)
        self.assertEqual(len(fake.projection_bodies), 1)

    def test_a_rule_that_reloads_the_student_does_not_ask_the_forecast_again(self) -> None:
        page, fake = self._projected(STANDARD)
        self._rules(page, elig="relaxed")
        self._wait_until(page, lambda: len(fake.context_bodies) == 2)
        expect(
            page.locator("#plbTable tbody[data-group='could'] tr[data-code='CS351']")
        ).to_be_visible()
        page.wait_for_timeout(300)
        self.assertEqual(len(fake.projection_bodies), 1)

    def test_the_keyboard_stays_put_when_the_forecast_lands(self) -> None:
        targets = (
            "#plbTable tbody[data-group='new'] tr[data-code='AI342'] input.plb-tick",
            "#plbTable tbody[data-group='new'] tr[data-code='AI331'] .plb-sc-btn",
            "#plbTable tbody[data-group='could'] tr[data-code='CS351'] .plb-ib--add",
            "#plbDetail [data-fk='must']",
        )
        for selector in targets:
            with self.subTest(selector=selector):
                fake = BuildFake()
                fake.hold_projection = 1
                page = self._open(fake)
                page.click("#plbBuild")
                self._wait_until(page, lambda fake=fake: len(fake.pending_projection) == 1)
                self._start(page)
                if selector.startswith("#plbDetail"):
                    self._open_panel(page, "AI331")
                page.locator(selector).focus()
                fake.pending_projection.pop().fulfill(json=STANDARD)
                expect(page.locator(NEW.format("AI331"))).to_have_text("If postponed: 5 terms")
                expect(page.locator(selector)).to_be_focused()

    # ── the rows ─────────────────────────────────────────────────────────────
    def test_each_text_of_a_row_in_english(self) -> None:
        page, _fake = self._projected(STANDARD)
        self.assertEqual(self._text(page, NEW.format("AI331")), "If postponed: 5 terms")
        self.assertEqual(self._text(page, NEW.format("AI342")), "If postponed: still 4 terms")
        self.assertEqual(self._text(page, COULD.format("STAT305")), "Still 4 terms")
        self.assertEqual(self._text(page, COULD.format("CS351")), "3 terms, 1 sooner")

    def test_each_text_of_a_row_in_arabic(self) -> None:
        page, _fake = self._projected(STANDARD, language="ar")
        self.assertEqual(self._text(page, NEW.format("AI331")), "إن أُجِّل: 5 فصول")
        self.assertEqual(self._text(page, NEW.format("AI342")), "إن أُجِّل: 4 فصول، بلا تغيير")
        self.assertEqual(self._text(page, COULD.format("STAT305")), "4 فصول، بلا تغيير")
        self.assertEqual(self._text(page, COULD.format("CS351")), "3 فصول، أبكر بفصل واحد")

    def test_arabic_counts_go_through_the_plural_forms(self) -> None:
        many = effects(
            {"AI331": effect(11, 1), "AI342": effect(2, 0)},
            {"STAT305": effect(1, -2), "CS351": effect(12, 3)},
        )
        page, _fake = self._projected(with_effects(many), language="ar")
        self.assertEqual(self._text(page, NEW.format("AI331")), "إن أُجِّل: 11 فصلًا")
        self.assertEqual(self._text(page, NEW.format("AI342")), "إن أُجِّل: فصلان، بلا تغيير")
        self.assertEqual(self._text(page, COULD.format("STAT305")), "فصل واحد، أبكر بفصلين")
        self.assertEqual(self._text(page, COULD.format("CS351")), "12 فصلًا، أبعد بـ3 فصول")

    def test_a_later_result_is_said_in_english_too(self) -> None:
        page, _fake = self._projected(with_effects(effects(added={"STAT305": effect(6, 2)})))
        self.assertEqual(self._text(page, COULD.format("STAT305")), "6 terms, 2 later")

    def test_strong_ink_only_when_the_course_costs_terms(self) -> None:
        page, _fake = self._projected(STANDARD)
        classes = {
            code: page.locator(sel.format(code)).get_attribute("class")
            for code, sel in (
                ("AI331", NEW),
                ("AI342", NEW),
                ("STAT305", COULD),
                ("CS351", COULD),
            )
        }
        self.assertIn("plb-fxt--strong", classes["AI331"])
        self.assertIn("plb-fxt--muted", classes["AI342"])
        self.assertIn("plb-fxt--muted", classes["STAT305"])
        self.assertIn("plb-fxt--brand", classes["CS351"])
        self.assertNotIn("strong", classes["AI342"])
        # the ink is real: strong is darker than muted
        colours = page.evaluate(
            "[...document.querySelectorAll('#plbTable .plb-fxt')]"
            ".map(n => [n.className, getComputedStyle(n).color])"
        )
        by_class = {c.split()[1]: colour for c, colour in colours}
        self.assertNotEqual(by_class["plb-fxt--strong"], by_class["plb-fxt--muted"])
        self.assertNotEqual(by_class["plb-fxt--brand"], by_class["plb-fxt--muted"])

    def test_the_effect_shares_the_section_line_and_is_plain_text_in_the_row(self) -> None:
        page, _fake = self._projected(STANDARD)
        node = page.locator(NEW.format("AI331"))
        self.assertEqual(node.evaluate("n => n.parentElement.className"), "plb-sl")
        self.assertEqual(node.evaluate("n => n.closest('td').className"), "plb-main")
        self.assertEqual(node.evaluate("n => n.tagName"), "SPAN")
        self.assertIsNone(node.get_attribute("title"))
        self.assertIsNone(node.get_attribute("aria-hidden"))
        self.assertEqual(node.evaluate("n => n.closest('tr').dataset.code"), "AI331")

    def test_nothing_on_registered_and_blocked_rows(self) -> None:
        page, _fake = self._projected(STANDARD)
        self.assertEqual(
            page.locator("#plbTable tbody[data-group='registered'] .plb-fxt").count(), 0
        )
        self.assertEqual(page.locator("#plbTable tbody[data-group='blocked'] .plb-fxt").count(), 0)
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 4)

    def test_nothing_on_the_registered_tab(self) -> None:
        page, _fake = self._projected(EVERY)
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 4)
        page.click("#plbTabRegistered")
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)
        self.assertEqual(page.locator("#plbGuide .plb-dep").count(), 0)
        page.click("#plbTabs [data-opt='B1']")
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 4)

    def test_nothing_while_the_forecast_is_loading_then_it_appears(self) -> None:
        fake = BuildFake()
        fake.hold_projection = 1
        page = self._open(fake)
        page.click("#plbBuild")
        self._wait_until(page, lambda: len(fake.pending_projection) == 1)
        self._start(page)
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)
        fake.pending_projection.pop().fulfill(json=STANDARD)
        expect(page.locator(NEW.format("AI331"))).to_have_text("If postponed: 5 terms")
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 4)

    def test_nothing_when_the_projection_is_unavailable(self) -> None:
        fake = BuildFake()
        fake.projection_raw = lambda route: route.fulfill(status=500, json={})
        page, _fake = self._built(fake)
        expect(page.locator("#plbGradLine")).to_have_text("Projection unavailable")
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)
        self.assertEqual(page.locator("#plbGuide .plb-dep").count(), 0)

    def test_nothing_when_the_option_is_not_complete(self) -> None:
        page, _fake = self._projected(reply())  # READY: no course_effects at all
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)
        page, _fake = self._projected(
            reply(B1={**forecast([15, 18], delta=None), "status": "unresolved", **B1_EFFECTS})
        )
        self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)

    def test_nothing_for_a_course_the_reply_has_no_usable_effect_for(self) -> None:
        broken = effects(
            {"AI331": NO_EFFECT, "AI342": {**effect(4, 0), "delta_terms": None}},
            {"STAT305": {"status": "unavailable", "reason_code": "section_metadata_unavailable"}},
        )
        page, _fake = self._projected(with_effects(broken))
        for selector in (NEW.format("AI331"), NEW.format("AI342"), COULD.format("STAT305")):
            self.assertEqual(page.locator(selector).count(), 0, selector)
        self.assertEqual(page.locator("#plbTable tr[data-code='AI331'] .plb-sl").count(), 0)
        # a malformed map is as good as none
        for bad in ({"course_effects": []}, {"course_effects": {"postponed": [], "added": 3}}):
            page, _fake = self._projected(with_effects(bad))
            self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 0)

    def test_another_tab_shows_its_own_effects(self) -> None:
        other = effects({"AI331": effect(4, 0), "AI342": effect(6, 2)}, {"CS351": effect(5, 1)})
        page, _fake = self._projected(
            with_effects(B1_EFFECTS, B2=forecast([12, 18, 18, 18], delta=0, **other))
        )
        self.assertEqual(self._text(page, NEW.format("AI331")), "If postponed: 5 terms")
        self._pick(page, "B2")
        self.assertEqual(self._text(page, NEW.format("AI331")), "If postponed: still 4 terms")
        self.assertEqual(self._text(page, NEW.format("AI342")), "If postponed: 6 terms")
        self.assertEqual(self._text(page, COULD.format("CS351")), "5 terms, 1 later")
        # STAT305 has no entry for B2: nothing is carried over from B1
        self.assertEqual(page.locator(COULD.format("STAT305")).count(), 0)
        self._pick(page, "B1")
        self.assertEqual(self._text(page, NEW.format("AI331")), "If postponed: 5 terms")

    # ── "Depends on" ─────────────────────────────────────────────────────────
    def _depends(self, page) -> list[str]:
        return page.eval_on_selector_all(
            "#plbGuide .plb-fr--depends .plb-dep",
            "ns => ns.map(n => n.textContent.replace(/\\s+/g, ' ').trim())",
        )

    def test_depends_on_with_one_course_between_watch_and_status(self) -> None:
        page, _fake = self._projected(STANDARD)
        self.assertEqual(self._depends(page), ["AI331 · if postponed: 5 terms"])
        self.assertEqual(self._text(page, "#plbGuide .plb-fr--depends dt"), "Depends on")
        labels = page.eval_on_selector_all(
            "#plbGuide dt", "ns => ns.map(n => n.textContent.trim())"
        )
        self.assertEqual(labels[-2:], ["Depends on", "Status"])

    def test_depends_on_with_two_courses_the_costliest_first_and_never_a_third(self) -> None:
        many = effects({"AI331": effect(5, 1), "AI342": effect(6, 2), "STAT305": effect(5, 1)})
        page, _fake = self._projected(with_effects(many))
        self.assertEqual(
            self._depends(page), ["AI342 · if postponed: 6 terms", "AI331 · if postponed: 5 terms"]
        )

    def test_depends_on_never_lists_a_third_course(self) -> None:
        three = effects({"AI331": effect(5, 1), "AI342": effect(6, 2), "STAT305": effect(7, 3)})
        fake = BuildFake()
        fake.projection_answer = lambda body: (200, with_effects(three))
        page = self._open(fake)
        page.click("#plbTable tbody[data-group='could'] tr[data-code='STAT305'] .plb-ib--add")
        page.click("#plbBuild")
        expect(page.locator("#plbBuild")).to_have_text("Rebuild")
        expect(page.locator("#plbBuild")).to_be_enabled()
        self._start(page)
        expect(page.locator("#plbGuide .plb-fr--depends")).to_be_visible()
        self.assertEqual(
            self._depends(page),
            ["STAT305 · if postponed: 7 terms", "AI342 · if postponed: 6 terms"],
        )

    def test_depends_on_is_absent_when_no_course_costs_a_term(self) -> None:
        free = effects({"AI331": effect(4, 0), "AI342": effect(4, 0)})
        page, _fake = self._projected(with_effects(free))
        self.assertEqual(page.locator("#plbGuide .plb-fr--depends").count(), 0)
        self.assertEqual(page.locator("#plbGuide dt").count(), 2)

    def test_depends_on_in_arabic(self) -> None:
        page, _fake = self._projected(STANDARD, language="ar")
        self.assertEqual(self._depends(page), ["AI331 · إن أُجِّل: 5 فصول"])
        self.assertEqual(self._text(page, "#plbGuide .plb-fr--depends dt"), "يعتمد على")
        self.assertEqual(page.locator("#plbGuide .plb-dep bdi").count(), 1)

    # ── the panel ────────────────────────────────────────────────────────────
    def test_the_panel_repeats_the_courses_line(self) -> None:
        page, _fake = self._projected(STANDARD)
        self._open_panel(page, "AI331")
        self.assertEqual(self._text(page, "#plbDetail .plb-dt-fx"), "If postponed: 5 terms")
        page.click("#plbDetail [data-act='dt-close']")
        self._open_panel(page, "CS351")
        self.assertEqual(self._text(page, "#plbDetail .plb-dt-fx"), "3 terms, 1 sooner")
        # the line sits under the heading, above the sections
        order = page.eval_on_selector_all(
            "#plbDetail > *", "ns => ns.map(n => n.className.split(' ')[0])"
        )
        self.assertEqual(order[:2], ["plb-dt-head", "plb-dt-fx"])

    def test_the_panel_has_no_line_for_a_course_without_an_effect(self) -> None:
        page, _fake = self._projected(STANDARD)
        self._open_panel(page, "AI342")
        self.assertEqual(self._text(page, "#plbDetail .plb-dt-fx"), "If postponed: still 4 terms")
        page.click("#plbTabRegistered")
        self.assertEqual(page.locator("#plbDetail .plb-dt-fx").count(), 0)

    def test_the_panel_follows_a_change_of_tab(self) -> None:
        other = effects({"AI331": effect(9, 5)})
        page, _fake = self._projected(
            with_effects(B1_EFFECTS, B2=forecast([12, 18, 18, 18], delta=0, **other))
        )
        self._open_panel(page, "AI331")
        self._pick(page, "B2")
        self.assertEqual(self._text(page, "#plbDetail .plb-dt-fx"), "If postponed: 9 terms")

    # ── an out-of-date build ─────────────────────────────────────────────────
    def test_an_out_of_date_build_keeps_the_texts_and_marks_them(self) -> None:
        page, _fake = self._projected(STANDARD)
        self.assertIsNone(page.locator(NEW.format("AI331")).get_attribute("data-stale"))
        self._rules(page, swap=True)
        self.assertEqual(self._text(page, NEW.format("AI331")), "If postponed: 5 terms")
        self.assertEqual(self._depends(page), ["AI331 · if postponed: 5 terms"])
        self.assertEqual(page.locator(NEW.format("AI331")).get_attribute("data-stale"), "changed")
        self.assertEqual(page.locator("#plbTable .plb-fxt[data-stale]").count(), 4)
        # a fresh build clears the mark
        page.click("#plbBuild")
        expect(page.locator("#plbTable .plb-fxt[data-stale]")).to_have_count(0)

    # ── fit ──────────────────────────────────────────────────────────────────
    def test_nothing_overflows_at_three_widths_in_both_languages_and_both_fonts(self) -> None:
        busy = effects(
            {"AI331": effect(11, 7), "AI342": effect(12, 8)},
            {"STAT305": effect(3, -8), "CS351": effect(12, 8)},
        )
        for language in ("en", "ar"):
            for font in ("app", "wide"):
                for width in (1440, 1152, 390):
                    page, _fake = self._projected(
                        with_effects(busy), language=language, width=width, font=font
                    )
                    where = (language, font, width)
                    self.assertEqual(page.locator("#plbTable .plb-fxt").count(), 4, where)
                    self.assertEqual(page.evaluate(OVERFLOWS), [], where)
                    self._open_panel(page, "AI331")
                    self.assertEqual(page.locator("#plbDetail .plb-dt-fx").count(), 1, where)
                    self.assertEqual(page.evaluate(OVERFLOWS), [], where)
                    # the line stays inside its own cell, wrapping instead of spilling
                    spill = page.evaluate(
                        "[...document.querySelectorAll('#plbTable .plb-sl')].filter(n => {"
                        " const c = n.closest('td').getBoundingClientRect();"
                        " return [...n.children].some(k => {"
                        "   const r = k.getBoundingClientRect();"
                        "   return r.right > c.right + 1 || r.left < c.left - 1; }); }).length"
                    )
                    self.assertEqual(spill, 0, where)
