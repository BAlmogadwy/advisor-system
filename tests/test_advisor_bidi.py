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

#: Atomic Latin tokens that contain a character the isolation regex does not treat
#: as an internal separator. Each one is a single thing with no valid RTL reading —
#: an address a student is told to write to, a link they are told to open.
ATOMIC_TOKENS = {
    "an email address": (
        "reg@taibahu.edu.sa",
        "راسل عمادة القبول والتسجيل على {} قبل نهاية الأسبوع.",
    ),
    "a url with a query": (
        "https://portal.edu/register?term=452&id=7",
        "افتح الرابط {} لإكمال التسجيل.",
    ),
    "a file name": ("Study%20Plan%20AI.pdf", "خطتك الدراسية في الملف {} على البوابة."),
    "an english clause with a comma": (
        "Data Structures, Algorithms and Complexity",
        "اسم المقرر بالإنجليزية هو {} كما يظهر في السجل.",
    ),
    "an ampersand": (
        "College of Science & Technology",
        "الكلية المسؤولة هي {} وليست كليتك.",
    ),
}

#: A block whose bold segment sits between a Latin word and an Arabic one. The
#: digits inside the bold are protected by W7 only because of the `Room` BEFORE it —
#: which is knowledge the scanner has to carry across the <strong> boundary, since
#: a bidi paragraph does not restart there. A scanner rebuilt per segment starts
#: from the block direction instead, and isolates them.
BOLD_ACROSS_SEGMENTS = "Room **101** ثم القاعة 3-4 للمختبر."

#: Arabic answers whose Latin CHARACTERS outnumber their Arabic ones. Both are core
#: shapes for a course-and-timetable adviser, which is why a character-majority rule
#: cannot be used to decide direction.
LATIN_HEAVY_ARABIC = {
    "a long english course title": (
        "الشرط المسبق هو Introduction to Artificial Intelligence قبل التسجيل."
    ),
    "a timetable table": (
        "جدولك لهذا الفصل:\n"
        "* CS101 M1 SUN 09:00-10:15\n"
        "* CS102 M2 TUE 10:30-11:45\n"
        "* MATH201 M1 WED 12:00-13:15"
    ),
}

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
      glyphs.push({ ch: node.data[i], x: box.left, top: box.top, bottom: box.bottom });
    }
  }
  if (!glyphs.length) return '';
  /* Group into lines by OVERLAP, not by rounding the top into fixed bands.
     Arabic and Latin glyphs on one line have different box tops — the Arabic
     fallback font sits differently from the Latin one — so a fixed band splits a
     single line in two whenever those tops straddle a boundary, silently
     reordering the string every assertion here reads. Two glyphs share a line
     when their boxes overlap vertically at all. */
  glyphs.sort((a, b) => a.top - b.top);
  const lines = [];
  glyphs.forEach((g) => {
    const line = lines.find((l) => g.top < l.bottom && g.bottom > l.top);
    if (line) {
      line.items.push(g);
      line.top = Math.min(line.top, g.top);
      line.bottom = Math.max(line.bottom, g.bottom);
    } else {
      lines.push({ top: g.top, bottom: g.bottom, items: [g] });
    }
  });
  lines.sort((a, b) => a.top - b.top);
  return lines
    .map((l) => l.items.sort((a, b) => a.x - b.x).map((g) => g.ch).join(''))
    .join('\\n');
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
            "core.services.student_advisor_v2.answer_student_advisor",
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

    def _assert_arabic_survived(self, page, selector: str, *phrases: str) -> None:
        """EVERY other assertion in this file names a Latin or numeric substring.

        That is a hole big enough to drive the whole answer through: deleting the
        two text-node appends in `appendIsolated` — so only the isolated runs reach
        the page and every Arabic character is dropped — leaves each of those
        assertions true. An audit did exactly that and the entire suite stayed
        green, including the test whose subject is a human adviser's Arabic reply.

        So one assertion has to name the Arabic. It reads `inner_text` rather than
        the visual scan because the failure it guards against is DELETION, and a
        deleted character has no position to measure.
        """
        body = page.eval_on_selector(selector, "n => n.innerText")
        for phrase in phrases:
            assert phrase in body, f"the Arabic went missing: {phrase!r} not in {body!r}"

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
        # The bullet with no Arabic in it. The fixture has carried it since the
        # first round with a comment explaining why, and nothing read it — so a
        # renderer that painted it «11:00-12:15 SUN M2 AI225» passed.
        assert "AI225 M2 SUN 11:00-12:15" in seen, seen
        self._assert_arabic_survived(
            page,
            ".va-message-assistant .sa-body",
            "شعبتك المتاحة في مقرر",
            "العبء الدراسي المسموح به هذا الفصل",
            "يوم الاثنين",
        )

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
        universal, and that the LTR path was not disturbed while fixing the other.

        Run in the ARABIC interface, and that is the whole point. In the English UI
        the page is already `ltr`, so a renderer that never states a direction at
        all passes — an audit deleted every `dir="ltr"` this file writes and the
        suite stayed green, because 13 of its 14 tests were asserting the page
        default rather than the code. Here the page is `rtl`, so `ltr` has to be
        stated to be true.
        """
        page = self._page(locale="ar")
        self._open(page, self._seed(ENGLISH_ANSWER, question="What is my section?"))
        assert page.eval_on_selector("html", "n => n.getAttribute('dir')") == "rtl", (
            "the Arabic interface did not load; this would assert the page default"
        )
        seen = self._visual(page, ".va-message-assistant .sa-body")

        assert "09:00-10:15" in seen, seen
        assert "12-19" in seen, seen
        assert "Your section for AI221 is M1." in seen, seen
        assert (
            page.eval_on_selector(".va-message-assistant .sa-body", "n => n.getAttribute('dir')")
            == "ltr"
        ), "the direction was inherited from the page rather than stated"
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

    # ── what the FIRST fix broke, fixing the first defect ───────
    def test_an_atomic_latin_token_is_not_taken_apart(self):
        """The first version of this fix isolated every Latin run, and that is a
        way of REVERSING them.

        UAX#9 BD8/X9: an isolate is replaced by U+FFFC in the enclosing run, and
        U+FFFC is class ON. Splitting one Latin sequence at a character the regex
        did not treat as an internal separator — `@`, `?`, `&`, `%`, `,` — therefore
        deleted every strong L from the outer paragraph. Those separators used to
        resolve to L by N1 (a neutral between two L runs) and became R by N2
        instead, and L2 laid the pieces out right-to-left.

        `reg@taibahu.edu.sa` reached the student as `taibahu.edu.sa@reg`: an address
        that does not exist, in an answer telling them to write to it. The same
        failure the isolation exists to prevent, caused by the isolation.
        """
        page = self._page()
        for label, (token, sentence) in ATOMIC_TOKENS.items():
            conversation = self._seed(sentence.format(token))
            self._open(page, conversation)
            seen = self._visual(page, ".va-message-assistant .sa-body")
            assert token in seen, f"{label}: expected {token!r}, painted {seen!r}"
            # The Arabic the token is embedded in has to still be there too.
            self._assert_arabic_survived(
                page, ".va-message-assistant .sa-body", sentence.split("{}")[0].strip()
            )

    def test_an_arabic_answer_stays_arabic_when_latin_outnumbers_it(self):
        """Arabic words are short; English course titles and course codes are long.
        A character-majority rule therefore flips exactly the two answer shapes this
        adviser produces most — a prerequisite named in English, and a timetable
        table — to left-to-right, which is the defect the rule was written to fix,
        moved from per-block to per-answer."""
        page = self._page()
        for label, answer in LATIN_HEAVY_ARABIC.items():
            conversation = self._seed(answer, question="ما جدولي؟")
            self._open(page, conversation)
            direction = page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            assert direction == "rtl", f"{label}: an Arabic answer rendered {direction}"

    def test_only_the_runs_that_actually_reorder_are_isolated(self):
        """The rule is UAX#9's own condition, not "anything Latin-looking".

        W7 changes a European number to L when the last strong type before it is L,
        so the digits in `AI221` were never going to reorder and isolating them
        would turn a working L island into a neutral. What reorders is a number
        whose last strong predecessor is an Arabic letter: W2 makes it AN, W4 then
        refuses to bind the hyphen, and L2 swaps the groups.
        """
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))
        isolated = page.eval_on_selector_all(
            ".va-message-assistant .sa-body bdi", "ns => ns.map(n => n.textContent)"
        )
        assert isolated, "nothing was isolated at all"
        for run in isolated:
            assert not any(c.isalpha() for c in run), (
                f"{run!r} contains a letter, so W7 had already resolved it to L"
            )
        assert not any("AI221" in run for run in isolated), isolated
        assert "09:00-10:15" in isolated, isolated
        assert "12-19" in isolated, isolated

    def test_the_strong_scanner_carries_across_a_bold_boundary(self):
        """`**bold**` splits the line into segments; the bidi paragraph does not
        restart at a `<strong>`.

        In «Room **101** ثم القاعة 3-4», the digits inside the bold are already L —
        because of the `Room` before them, in a DIFFERENT segment. A scanner rebuilt
        per segment starts from the block's direction instead, decides they follow
        Arabic, and isolates them: a working L island turned into a neutral."""
        page = self._page()
        self._open(page, self._seed(BOLD_ACROSS_SEGMENTS))
        isolated = page.eval_on_selector_all(
            ".va-message-assistant .sa-body bdi", "ns => ns.map(n => n.textContent)"
        )
        assert "101" not in isolated, (
            f"the bold's digits were isolated; the scanner forgot `Room`: {isolated}"
        )
        # ...while the digits that DO follow Arabic still are.
        assert "3-4" in isolated, isolated

    def test_a_number_after_a_latin_word_is_left_alone(self):
        """`Section 3-4` inside an Arabic sentence: the last strong character before
        the digits is `n`, so they are already L and the clause is already one
        island. Isolating them would hand its punctuation to N2."""
        page = self._page()
        self._open(
            page,
            self._seed("راجع Section 3-4 من الدليل الإرشادي قبل التسجيل."),
        )
        isolated = page.eval_on_selector_all(
            ".va-message-assistant .sa-body bdi", "ns => ns.map(n => n.textContent)"
        )
        assert "3-4" not in isolated, isolated
        assert "Section 3-4" in self._visual(page, ".va-message-assistant .sa-body")

    def test_the_advisers_reply_uses_the_language_the_student_asked_in(self):
        """A person's reply carries no pinned language of its own. It takes the
        student's, which is the only evidence on file and the audience it is
        addressed to — not a count of its own characters."""
        conversation = self._seed(ENGLISH_ANSWER, question="What is my section?")
        message = AdvisorMessage.objects.filter(conversation=conversation, role="ASSISTANT").first()
        AdvisorEscalation.objects.create(
            student_id=MINE,
            conversation=conversation,
            source_message=message,
            reason_code=AdvisorEscalation.Reason.STUDENT_REQUESTED,
            status=AdvisorEscalation.Status.RESOLVED,
            resolution_message="Please contact عمادة القبول والتسجيل before week two.",
        )
        page = self._page()
        self._open(page, conversation)
        page.wait_for_selector(".sa-case-reply p")
        assert (
            page.eval_on_selector(".sa-case-reply p", "n => getComputedStyle(n).direction") == "ltr"
        ), "one Arabic office name turned an English reply right-to-left"

    def test_the_direction_is_the_language_the_server_pinned(self):
        """`virtual_advisor._answer_language` already decides this, deterministically,
        and pins the model to answer in it. The browser re-deriving the answer from
        the characters it received is a second opinion that can disagree with the
        first — and every heuristic tried here disagreed on some real answer shape.

        An Arabic question gets an Arabic answer laid out right-to-left even when the
        answer is mostly course codes; an English question gets the reverse even when
        the answer opens with «عمادة القبول والتسجيل»."""
        page = self._page()

        arabic = self._seed(LATIN_HEAVY_ARABIC["a timetable table"], question="ما جدولي؟")
        self._open(page, arabic)
        assert (
            page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            == "rtl"
        )

        english = self._seed(ARABIC_FIRST_ENGLISH_ANSWER, question="Who decides this?")
        self._open(page, english)
        assert (
            page.eval_on_selector(
                ".va-message-assistant .sa-body", "n => getComputedStyle(n).direction"
            )
            == "ltr"
        )

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
        """Measured on the first GLYPH, not on the block box.

        The first version read `getBoundingClientRect().right` of each <p>. A
        block-level paragraph is full width whichever way it runs, so those edges
        are identical no matter what `direction` says — an audit gave the two
        paragraphs of one answer OPPOSITE directions and this test passed. All it
        was really asserting was that two paragraphs existed.

        Where a reader's eye goes is where the first character is painted, and that
        does move to the other end of the line."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        starts = page.evaluate(
            """() => {
              const body = document.querySelector('.va-message-assistant .sa-body');
              const firstGlyph = (el) => {
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                  for (let i = 0; i < node.data.length; i += 1) {
                    if (!node.data[i].trim()) continue;
                    const r = document.createRange();
                    r.setStart(node, i); r.setEnd(node, i + 1);
                    const box = r.getBoundingClientRect();
                    if (box.width || box.height) return Math.round(box.left);
                  }
                }
                return null;
              };
              return Array.from(body.querySelectorAll('p')).map(firstGlyph);
            }"""
        )
        starts = [x for x in starts if x is not None]
        assert len(starts) >= 2, starts
        # An RTL paragraph paints its first character at the right-hand end, so two
        # paragraphs that agree land within a character's width of each other.
        assert max(starts) - min(starts) <= 12, starts

    def test_the_list_reserves_its_marker_gutter_on_the_starting_side(self):
        """`::marker` has no box this can read.

        The first version compared `getBoundingClientRect()` of `ul, ol, li`
        against the bubble and claimed to prove the marker stayed inside it. With
        `list-style-position: outside` the marker is painted OUTSIDE the principal
        box, so those rects never contained it — an audit set
        `padding-inline-start: 0`, deleting the gutter entirely, and the test passed
        three runs running.

        What can be measured is the gutter itself: the inset between the list box
        and its items, which is the space the marker is drawn into. It has to exist,
        and it has to be on the side the text starts from."""
        page = self._page()
        self._open(page, self._seed(TIMETABLE_ANSWER))

        gutters = page.evaluate(
            """() => {
              const body = document.querySelector('.va-message-assistant .sa-body');
              return Array.from(body.querySelectorAll('ul, ol')).map((list) => {
                const rtl = getComputedStyle(list).direction === 'rtl';
                const outer = list.getBoundingClientRect();
                const item = list.firstElementChild.getBoundingClientRect();
                return {
                  rtl,
                  start: Math.round(rtl ? outer.right - item.right : item.left - outer.left),
                  end: Math.round(rtl ? item.left - outer.left : outer.right - item.right),
                };
              });
            }"""
        )
        assert gutters, "the answer rendered no list at all"
        for gutter in gutters:
            assert gutter["start"] >= 12, f"no room reserved for the marker: {gutter}"
            assert gutter["end"] <= 2, f"the gutter is on the wrong side: {gutter}"

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
            # Opens with a course code, so its own first strong character says LTR
            # while the student asked in Arabic. Reading the reply instead of
            # the conversation gives the wrong answer, and only a reply whose
            # characters DISAGREE with the pinned language can prove which one
            # is being read.
            resolution_message="AI221 غير متاح هذا الفصل، راجعنا يوم الأحد 09:00-10:15.",
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
