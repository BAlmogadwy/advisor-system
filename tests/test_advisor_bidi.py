"""What the student's eye reads, not what the DOM says.

Every assertion in this file measures the RENDERED POSITION of characters, because
the defect it guards against is invisible to any test that reads `textContent`.
«09:00-10:15» is stored correctly, serialised correctly, inserted into the DOM
correctly — and painted as «10:15-09:00». `assert "09:00-10:15" in body.inner_text()`
passes on the broken page.

So `visual_order()` walks every character, asks the browser where it drew it, and
sorts by line then by x. That string is the reading order of a person looking at
the screen. In an RTL paragraph the Arabic comes back reversed relative to its
logical order — that is what "visual" means and it is not what is being asserted;
what is asserted is that an embedded Latin/numeric run appears in the order it was
written.

The bug this catches shipped to a TIMETABLE adviser, which is the worst possible
place for it: every lecture time printed as ending before it starts, in an answer
that is otherwise correct and confidently sourced.
"""

from __future__ import annotations

import json
import os
from unittest import mock

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorMessage,
    RateLimitBucket,
    Student,
)
from core.services.rbac import ensure_role_groups

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

MINE = 7002001

#: The answer the live model produced for a timetable question, trimmed. Every
#: element that was reordered is present: a time range, a course code opening a
#: list item, a credit range, and a date range.
TIMETABLE_ANSWER = (
    "شعبتك المتاحة في مقرر AI221 هي M1.\n"
    "* AI221 — يوم الأحد 09:00-10:15\n"
    "* مقرر DS332 — يوم الاثنين 13:00-14:15\n"
    # A bullet with NO Arabic in it at all. The model writes these constantly on a
    # timetable question, and it is the item any first-strong-character rule sends
    # left-to-right while its siblings go right-to-left.
    "* AI225 M2 SUN 11:00-12:15\n"
    "العبء الدراسي المسموح به هذا الفصل هو 12-19 ساعة."
)

ENGLISH_ANSWER = (
    "Your section for AI221 is M1.\n"
    "* AI221 — Sunday 09:00-10:15\n"
    "The permitted load this term is 12-19 credit hours."
)

#: An Arabic answer whose FIRST strong character is Latin. The model opens with a
#: course code whenever the question named one, which on a course-and-timetable
#: adviser is most of the time. This is the fixture that separates "the direction
#: is decided from the text" from "the direction happens to come out right":
#: `dir="auto"` cannot see past a leading isolate, so it falls back — and every
#: block of an Arabic answer lands at the wrong edge.
CODE_FIRST_ANSWER = (
    "AI221 هو المقرر الذي سألت عنه، وشعبتك فيه M1.\nيوم الأحد 09:00-10:15 في القاعة 2-14."
)

#: The mirror image, and the reason the rule is a COUNT and not the first strong
#: character. The prompt requires the model to refer students to
#: «عمادة القبول والتسجيل» by that Arabic name, so an English answer routinely
#: opens with an Arabic phrase. First-strong-character calls this whole answer
#: Arabic and lays 200 characters of English out from the right.
ARABIC_FIRST_ENGLISH_ANSWER = (
    "عمادة القبول والتسجيل decides this case, not this system.\n"
    "Your section for AI221 is M1, and the permitted load is 12-19 credit hours "
    "for the current term. Bring your identity card when you go."
)

#: Reads every character's painted box and returns them in reading order: top line
#: first, then left to right within a line. `getBoundingClientRect` on a one-character
#: Range is the only way to ask the browser where a glyph ACTUALLY went — computed
#: styles and `textContent` both report the input, not the result.
VISUAL_ORDER_JS = """
(selector) => {
  const root = document.querySelector(selector);
  if (!root) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const glyphs = [];
  let node;
  while ((node = walker.nextNode())) {
    for (let i = 0; i < node.data.length; i += 1) {
      const range = document.createRange();
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const box = range.getBoundingClientRect();
      if (!box.width && !box.height) continue;   // collapsed whitespace
      glyphs.push({ ch: node.data[i], x: box.left, line: Math.round(box.top / 4) });
    }
  }
  glyphs.sort((a, b) => (a.line - b.line) || (a.x - b.x));
  return glyphs.map((g) => g.ch).join('');
}
"""


def _reply(answer: str) -> dict:
    return {
        "ok": True,
        "answer": answer,
        "model": "stub",
        "citations": [],
        "cited_policy_ids": [],
        "agent": {"loop_used": True, "policy_grounding": "retrieved"},
    }


class AdvisorBidiTests(StaticLiveServerTestCase):
    """The default interface language is English. That is deliberate and it is the
    case that hid every one of these: an Arabic answer inside an LTR page."""

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

    # ── fixtures ────────────────────────────────────────────────
    def _client(self) -> Client:
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=MINE, defaults={"name": "S", "program": "AI", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(MINE))
        return client

    def _page(self, **context_kw):
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=MINE, defaults={"name": "S", "program": "AI", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(MINE))
        context = self.browser.new_context(**context_kw)
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.addCleanup(context.close)
        return context.new_page()

    def _seed(self, answer: str, question: str = "ما شعبتي؟") -> AdvisorConversation:
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        with mock.patch(
            "core.services.virtual_advisor.answer_virtual_advisor",
            return_value=_reply(answer),
        ):
            response = self._client().post(
                reverse("advisor_conversation_send", args=[conversation.id]),
                data=json.dumps({"message": question}),
                content_type="application/json",
            )
        assert response.status_code == 201, response.content
        RateLimitBucket.objects.all().delete()
        return conversation

    def _open(self, page, conversation: AdvisorConversation) -> None:
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant .sa-body")

    def _visual(self, page, selector: str) -> str:
        text = page.evaluate(VISUAL_ORDER_JS, selector)
        assert text is not None, f"nothing matched {selector!r}"
        return text

    # ── 1. ranges keep the order they were written in ───────────
    def test_a_time_range_is_not_painted_backwards(self):
        """«الأحد 09:00-10:15» reached the student as «10:15-09:00».

        UAX#9, working as specified: the Arabic letter before the digits makes them
        AN, rule W4 fuses an ES hyphen only between two EN, so the hyphen stays
        neutral, resolves to the paragraph's RTL direction, and L2 reverses the two
        number groups around it.
        """
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))
        seen = self._visual(page, ".va-message-assistant .sa-body")

        assert "09:00-10:15" in seen, seen
        assert "10:15-09:00" not in seen, seen
        assert "13:00-14:15" in seen, seen

    def test_a_credit_range_is_not_painted_backwards(self):
        """The same defect, and the one with the highest cost: «12-19 ساعة» read as
        «19-12», which is a load range whose floor is above its ceiling."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))
        seen = self._visual(page, ".va-message-assistant .sa-body")

        assert "12-19" in seen, seen
        assert "19-12" not in seen, seen

    def test_an_english_answer_is_left_exactly_as_it_was(self):
        """The isolation is applied only where the defect exists. In an LTR
        paragraph the digits are already EN, W4 already binds the hyphen, and the
        order is already right — so this proves the fix is conditional rather than
        universal, and that the LTR path was not disturbed while fixing the other."""
        page = self._page()
        self._open(page, self._seed(ENGLISH_ANSWER, question="What is my section?"))
        seen = self._visual(page, ".va-message-assistant .sa-body")

        assert "09:00-10:15" in seen, seen
        assert "12-19" in seen, seen
        assert (
            page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            == "ltr"
        )
        assert (
            page.eval_on_selector_all(".va-message-assistant .sa-body bdi", "n => n.length") == 0
        ), "an LTR answer was isolated for a problem it does not have"

    # ── 2. one answer, one direction ────────────────────────────
    def test_a_list_item_opening_with_a_course_code_does_not_flip(self):
        """`dir="auto"` per item gave ONE answer two directions: «AI221 — يوم…»
        takes its direction from that first strong character and computes LTR while
        its Arabic siblings compute RTL, so the list splits across both edges of the
        bubble and the odd item's marker is painted outside it."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        directions = page.eval_on_selector_all(
            ".va-message-assistant .sa-body p, .va-message-assistant .sa-body li",
            "ns => ns.map(n => getComputedStyle(n).direction)",
        )
        assert len(directions) >= 3, directions
        assert set(directions) == {"rtl"}, directions

    def test_the_list_and_its_items_agree_about_which_side_the_marker_is_on(self):
        """THE mechanism behind the misplaced bullet, measured directly.

        The marker is drawn at the item's inline-START edge; the room for it is
        `padding-inline-start` on the LIST. Putting `dir="auto"` on the items left
        the `<ul>` on the page's direction — LTR in the default English interface —
        so the padding was reserved on the left while every RTL item drew its marker
        on the right, into space no one had reserved. A list and its items resolving
        differently is that defect, whichever way round it happens."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        pairs = page.evaluate(
            """() => {
              const body = document.querySelector('.va-message-assistant .sa-body');
              return Array.from(body.querySelectorAll('ul, ol')).flatMap((list) => {
                const outer = getComputedStyle(list).direction;
                return Array.from(list.children).map(
                  (li) => [outer, getComputedStyle(li).direction]
                );
              });
            }"""
        )
        assert pairs, "the answer rendered no list at all"
        assert all(outer == inner for outer, inner in pairs), pairs

    def test_an_answer_that_opens_with_a_course_code_is_still_arabic(self):
        """The direction is read from the TEXT, not from whichever character
        happens to be first.

        `dir="auto"` reads the first strong character and stops — and it is
        specified to skip descendants that carry their own `dir`, which every
        isolated Latin run now does. So an answer opening «AI221 هو المقرر…» offers
        the auto algorithm nothing it is allowed to look at, and the whole answer
        falls back to the interface language: right-to-left Arabic laid out from the
        left edge, in the language the student did not choose."""
        page = self._page()
        self._open(page, self._seed(CODE_FIRST_ANSWER, question="ما شعبتي في AI221؟"))

        assert (
            page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            == "rtl"
        )
        seen = self._visual(page, ".va-message-assistant .sa-body")
        assert "09:00-10:15" in seen, seen
        assert "2-14" in seen, seen

    def test_an_english_answer_that_opens_with_an_arabic_phrase_stays_english(self):
        """The direction is the script the answer is MOSTLY in, and this is the
        fixture that separates that rule from the one `dir="auto"` uses.

        The system prompt requires «عمادة القبول والتسجيل» by name, so an otherwise
        English answer opening with it is not a contrived case — it is what the
        model produces every time it has to refer a student to the registrar."""
        page = self._page()
        self._open(
            page,
            self._seed(ARABIC_FIRST_ENGLISH_ANSWER, question="Who decides this?"),
        )

        assert (
            page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            == "ltr"
        ), "one leading Arabic phrase turned an English answer right-to-left"

    def test_every_paragraph_of_one_answer_starts_at_the_same_edge(self):
        """The consequence the student sees. Two paragraphs of ONE answer that
        disagree about direction start at opposite sides of the bubble."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        rights = page.eval_on_selector_all(
            ".va-message-assistant .sa-body p",
            "ns => ns.map(n => Math.round(n.getBoundingClientRect().right))",
        )
        assert len(rights) >= 2, rights
        assert max(rights) - min(rights) <= 2, rights

    def test_no_part_of_a_list_is_drawn_outside_the_bubble(self):
        """The marker lives in the list's `padding-inline-start`. When the list
        resolves to the direction the bubble does not, that padding is on the far
        side and the marker is drawn past the bubble's own border."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        overflow = page.evaluate(
            """() => {
              const bubble = document.querySelector('.va-message-assistant .va-bubble');
              const box = bubble.getBoundingClientRect();
              return Array.from(bubble.querySelectorAll('ul, ol, li')).map((n) => {
                const r = n.getBoundingClientRect();
                return Math.round(Math.max(box.left - r.left, r.right - box.right));
              });
            }"""
        )
        assert overflow and max(overflow) <= 0, overflow

    # ── 3. the nodes that had no direction at all ───────────────
    def test_the_human_advisers_reply_has_a_direction(self):
        """The one message on this screen rendered with no `dir`. A person's Arabic
        reply, laid out left-to-right in the default English interface — the same
        unreadability the model's answers were fixed for, on the message a student
        has been waiting days to read."""
        conversation = self._seed(TIMETABLE_ANSWER)
        message = AdvisorMessage.objects.filter(conversation=conversation, role="ASSISTANT").first()
        AdvisorEscalation.objects.create(
            student_id=MINE,
            conversation=conversation,
            source_message=message,
            reason_code=AdvisorEscalation.Reason.STUDENT_REQUESTED,
            status=AdvisorEscalation.Status.RESOLVED,
            resolution_message="راجع عمادة القبول والتسجيل يوم الأحد 09:00-10:15.",
        )

        page = self._page()
        self._open(page, conversation)
        page.wait_for_selector(".sa-case-reply p")

        assert (
            page.eval_on_selector(".sa-case-reply p", "n => getComputedStyle(n).direction") == "rtl"
        )
        assert "09:00-10:15" in self._visual(page, ".sa-case-reply p")

    def test_the_case_reference_goes_through_the_same_writer(self):
        """The one string a student reads back to a person over the phone.

        MEASURED, and the measurement says the isolation changes nothing here:
        removing it leaves `1448-2026-00184` rendering correctly even in the Arabic
        panel, because the reordering needs an Arabic LETTER beside the digits to
        make them AN, and this `<dd>` holds nothing but the reference. The mutant is
        equivalent, and saying so is worth more than a green test that implies a bug
        was fixed.

        What the assertion locks is the UNIFORMITY, which is the property that has
        actual value: every text-bearing node on this screen states its direction,
        because the one node that did not — the human adviser's reply — was
        unreadable for a year and nobody noticed. A reference dropped back into an
        Arabic sentence, or reformatted without its `ADV-` prefix, would reorder;
        the invariant is what makes that a non-event instead of the next defect.
        """
        conversation = self._seed(TIMETABLE_ANSWER)
        message = AdvisorMessage.objects.filter(conversation=conversation, role="ASSISTANT").first()
        case = AdvisorEscalation.objects.create(
            student_id=MINE,
            conversation=conversation,
            source_message=message,
            reason_code=AdvisorEscalation.Reason.STUDENT_REQUESTED,
            status=AdvisorEscalation.Status.OPEN,
            reference="1448-2026-00184",
        )

        page = self._page(locale="ar")
        self._open(page, conversation)
        page.wait_for_selector(".sa-case-ref")
        assert (
            page.eval_on_selector(
                "html", "n => n.getAttribute('dir') || getComputedStyle(n).direction"
            )
            == "rtl"
        ), "the Arabic interface did not load; the case cannot be exercised"
        assert case.reference in self._visual(page, ".sa-case-ref"), (
            f"expected {case.reference}, painted {self._visual(page, '.sa-case-ref')!r}"
        )
        # The attribute's PRESENCE is the invariant, not its value: a reference with
        # no strong character takes the interface language, which is the honest
        # answer for a string that carries no evidence of its own.
        assert page.eval_on_selector(".sa-case-ref", "n => n.getAttribute('dir')") in (
            "ltr",
            "rtl",
        ), "the reference did not go through the writer every other node uses"

    def test_an_arabic_conversation_title_is_truncated_from_its_end(self):
        """`text-overflow: ellipsis` cuts at the END of the line, which in an LTR
        button holding an Arabic title is the title's BEGINNING. The sidebar was a
        list of endings."""
        conversation = self._seed(TIMETABLE_ANSWER)
        page = self._page()
        self._open(page, conversation)
        page.wait_for_selector(".sa-conv")

        assert page.eval_on_selector(".sa-conv", "n => getComputedStyle(n).direction") == "rtl", (
            "an Arabic title kept the page's direction"
        )

    def test_the_composer_follows_the_language_being_typed(self):
        """The interface language is not the language the student types in."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        assert (
            page.eval_on_selector("#saQuestion", "n => getComputedStyle(n).direction") == "ltr"
        ), "the empty field should follow the English interface"
        page.fill("#saQuestion", "كم ساعة أستطيع تسجيلها؟")
        assert (
            page.eval_on_selector("#saQuestion", "n => getComputedStyle(n).direction") == "rtl"
        ), "Arabic was typed into a left-to-right field"

    # ── 4. the bubble's own whitespace handling ─────────────────
    def test_the_bubble_does_not_impose_preformatted_whitespace_on_blocks(self):
        """`.va-bubble` is shared with the older adviser page, which puts raw text
        in it and needs `pre-wrap`. This page puts real elements in it, and
        inheriting `pre-wrap` preserves every space the markdown parser left at the
        edges of a list item. The paragraph keeps its own `pre-wrap`, because the
        model's single newlines inside one paragraph are structure the student can
        see."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        assert (
            page.eval_on_selector(
                ".va-message-assistant .va-bubble", "n => getComputedStyle(n).whiteSpace"
            )
            == "normal"
        )
        assert page.eval_on_selector(
            ".va-message-assistant .sa-para", "n => getComputedStyle(n).whiteSpace"
        ) in ("pre-wrap", "preserve-breaks")
