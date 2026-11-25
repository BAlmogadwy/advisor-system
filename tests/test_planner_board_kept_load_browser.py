"""The new planner screen: why a course did not fit when the registered courses use part of the
credit limit, in a real browser.

In keep mode the credit limit covers the kept courses too (#287). Its build reply says so:
`constraints.registered_credits` and `constraints.credit_room`, and on each DID_NOT_FIT detail the
course's credits, the limit and the registered credits. The board says which case it was, with the
numbers, as the old page does, and "Nothing can be added" when no room is left. A reply without those
fields reads as before. Answered by the same fakes as the other board tests.
"""

from __future__ import annotations

import copy

from tests.planner_board_support import FIVE, BuildFake, PlannerBoardCase, build_reply, expect

LEAD = "#plbBuildNote .plb-buildnote-lead"
NO_ROOM_LEAD = "Nothing can be added: the registered courses use the whole credit limit."
NO_ROOM_LEAD_AR = "لا يمكن إضافة مقررات: المقررات المسجلة تستنفد حد الساعات كاملًا."
KEPT = [
    {"course_code": "AI342", "credits": 4},  # above the 3 left
    {"course_code": "CS351", "credits": 3, "considered": 2},  # fits alone, not beside the others
    {"course_code": "STAT305", "credits": 19},  # above the whole limit
]


def did_not_fit(code, credits, limit, kept=None, considered=None):
    detail = {"course_credits": credits, "credit_limit": limit}
    if kept is not None:
        detail["registered_credits"] = kept
    if considered is not None:
        detail["sections_considered"] = considered
    return {
        "course_code": code,
        "reason_code": "DID_NOT_FIT",
        "reason": "server words",
        "details": [detail],
    }


def no_room(kept, limit=18, codes=("AI331", "AI342")):
    reply = build_reply([])
    reply["summary"]["hard_constraint_failures"] = [
        {
            "kind": "max_credits",
            "course_code": "",
            "reason": f"No room left under the {limit}-credit limit: {kept} credits are registered",
        }
    ]
    reply["unscheduled"] = [did_not_fit(code, 3, limit, kept) for code in codes]
    reply["constraints"] = {**reply["constraints"], "registered_credits": kept, "credit_room": 0}
    return reply


def with_room(unscheduled, kept=15, limit=18, on_option=None):
    reply = copy.deepcopy(FIVE)
    reply["unscheduled"] = list(unscheduled)
    if on_option is not None:
        reply["options"][0]["unscheduled"] = list(on_option)
    reply["constraints"] = {
        **reply["constraints"],
        "registered_credits": kept,
        "credit_room": max(0, limit - kept),
    }
    return reply


def kept_load():
    return [
        did_not_fit(c["course_code"], c["credits"], 18, 15, considered=c.get("considered"))
        for c in KEPT
    ]


class PlannerBoardKeptLoadTests(PlannerBoardCase):
    start_on = "B1"

    def _run(self, reply, language="en"):
        fake = BuildFake()
        fake.build_answer = lambda body: (200, reply)
        page, _ = self._built(fake, language=language)
        return page

    @staticmethod
    def _items(page, selector):
        return page.eval_on_selector_all(
            selector, "ls => ls.map(l => l.textContent.replace(/\\s+/g, ' ').trim())"
        )

    def _why(self, page):
        page.click("#plbWhyBtn")
        expect(page.locator("#plbWhy")).to_be_visible()
        return self._items(page, "#plbWhy li")

    def test_no_room_says_nothing_can_be_added_and_the_numbers_once(self) -> None:
        for kept in (18, 21):
            with self.subTest(kept=kept):
                page = self._run(no_room(kept))
                self.assertEqual(self._text(page, LEAD), NO_ROOM_LEAD)
                self.assertEqual(
                    self._items(page, "#plbBuildNote li"),
                    [
                        "Not scheduled: AI331, AI342 \u2014 No room left under the 18-credit limit: "
                        f"{kept} credits are registered"
                    ],
                )

    def test_no_room_in_arabic(self) -> None:
        page = self._run(no_room(18), language="ar")
        self.assertEqual(self._text(page, LEAD), NO_ROOM_LEAD_AR)
        self.assertEqual(
            self._items(page, "#plbBuildNote li"),
            ["غير مجدولة: AI331, AI342 — لا متّسع ضمن حد 18 ساعة: الساعات المسجلة 18"],
        )

    def test_no_room_with_nothing_left_out_adds_no_must_take_sentence(self) -> None:
        page = self._run(no_room(18, codes=()))
        self.assertEqual(self._text(page, LEAD), NO_ROOM_LEAD)
        self.assertEqual(self._items(page, "#plbBuildNote li"), [])

    def test_each_kind_of_did_not_fit_with_a_kept_load(self) -> None:
        lines = self._why(self._run(with_room(kept_load())))
        for expected in (
            "Not scheduled: AI342 \u2014 4 credits, above the 3 left of the 18-credit limit"
            " (15 registered)",
            "Not scheduled: CS351 \u2014 Could not fit within the 18-credit limit"
            " (15 of it registered), or its sections clash",
            "Not scheduled: STAT305 \u2014 19 credits, above the 18-credit limit of this build",
        ):
            self.assertIn(expected, lines)

    def test_each_kind_in_arabic(self) -> None:
        lines = self._why(self._run(with_room(kept_load()), language="ar"))
        for expected in (
            "غير مجدولة: AI342 — 4 ساعات، والمتبقي من حد 18 ساعة 3 فقط (المسجّل 15)",
            "غير مجدولة: CS351 — تعذّر إدراجه ضمن حد 18 ساعة (المسجّل منها 15)، أو أن شُعبه متعارضة",
            "غير مجدولة: STAT305 — 19 ساعات، أعلى من حد 18 ساعة لهذا البناء",
        ):
            self.assertIn(expected, lines)

    def test_without_a_kept_load_the_limit_is_still_named(self) -> None:
        reply = with_room(
            [
                did_not_fit("AI342", 4, 3),
                did_not_fit("CS351", 3, 18),
                {"course_code": "STAT305", "reason_code": "DID_NOT_FIT"},
            ],
            kept=0,
        )
        lines = self._why(self._run(reply))
        for expected in (
            "Not scheduled: AI342 \u2014 4 credits, above the 3-credit limit of this build",
            "Not scheduled: CS351 \u2014 Could not fit within the 18-credit limit,"
            " or its sections clash",
            "Not scheduled: STAT305 \u2014 Could not fit within the credit limit,"
            " or its sections clash",
        ):
            self.assertIn(expected, lines)

    def test_an_options_own_reasons_say_it_too(self) -> None:
        reply = with_room([], on_option=[did_not_fit("AI342", 4, 18, 15)])
        lines = self._why(self._run(reply))
        self.assertIn(
            "B1: Not scheduled: AI342 \u2014 4 credits, above the 3 left of the 18-credit limit"
            " (15 registered)",
            lines,
        )
