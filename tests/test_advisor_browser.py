"""The adviser screen in a real browser.

`tests/test_advisor_conversations.py` proves the endpoints are correct. It cannot
prove the screen is, because everything a student actually sees is assembled by
JavaScript from those responses — and the two defects that shipped first were
both invisible to an API test: a flex container marked `hidden` stayed on screen
because `display` outranks the attribute, and a reload landed on an empty thread
even though the conversation was safely stored.

So these drive Chromium. The adviser itself is stubbed: what is under test is the
screen, and a real model would make the assertions non-deterministic without
making them stronger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import timedelta
from unittest import mock

# Playwright's synchronous API drives a greenlet event loop, so Django sees "an
# async context" and blocks every ORM call the assertions need. The guard is aimed
# at async request handlers; here the test body is genuinely single-threaded and
# the live server does its own work in a separate thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorFeedback,
    AdvisorMessage,
    RateLimitBucket,
    Student,
)
from core.services.rbac import ensure_role_groups

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

MINE = 7001001
THEIRS = 7001002

WITHDRAWAL_ANSWER = (
    "الحد الأقصى خمسة انسحابات «الدليل الإرشادي للطالب والطالبة، ص 24 "
    "[TU.WITHDRAWAL.MAXIMUM]»، ولا يُسمح للطالب المستجد بالانسحاب "
    "«الدليل الإرشادي للطالب والطالبة، ص 24 [TU.WITHDRAWAL.NEW_STUDENT_BAR]»."
)

CITATIONS = [
    {
        "policy_id": "TU.WITHDRAWAL.MAXIMUM",
        "document_id": "TU.GUIDE",
        "document_title": "الدليل الإرشادي للطالب والطالبة",
        "edition": "1447",
        "page": 24,
        "effective_from": "1447",
        "effective_to": "",
    },
    {
        "policy_id": "TU.WITHDRAWAL.NEW_STUDENT_BAR",
        "document_id": "TU.GUIDE",
        "document_title": "الدليل الإرشادي للطالب والطالبة",
        "edition": "1447",
        "page": 24,
        "effective_from": "1447",
        "effective_to": "",
    },
]

# A second page of the same document, so "multiple citations" is a genuinely
# multi-entry list and not two identical lines.
CITATION_OTHER_PAGE = {
    "policy_id": "TU.LOAD.SEMESTER_RANGE",
    "document_id": "TU.GUIDE",
    "document_title": "الدليل الإرشادي للطالب والطالبة",
    "edition": "1447",
    "page": 23,
    "effective_from": "1447",
    "effective_to": "",
}

TWO_PAGE_ANSWER = WITHDRAWAL_ANSWER + " والحد الأدنى للعبء «ص 23 [TU.LOAD.SEMESTER_RANGE]»."


def _reply(answer: str, citations: list[dict], presentation=None) -> dict:
    result = {
        "ok": True,
        "answer": answer,
        "model": "stub",
        "citations": citations,
        "cited_policy_ids": [c["policy_id"] for c in citations],
        "agent": {"loop_used": True, "policy_grounding": "retrieved"},
    }
    if presentation is not None:
        result["presentation"] = presentation
    return result


class AdvisorBrowserTests(StaticLiveServerTestCase):
    """One browser for the class, one fresh page per test."""

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
    def _session_cookie(self, student_id: int) -> dict:
        """Sign in through the production provisioning path, not a hand-built scope."""
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=student_id,
            defaults={"name": f"S{student_id}", "program": "CS", "section": "M"},
        )
        user = student_otp.provision_student_user(student_id)
        client = Client()
        client.force_login(user)
        return {
            "name": "sessionid",
            "value": client.cookies["sessionid"].value,
            "url": self.live_server_url,
        }

    def _page(self, student_id: int = MINE, **context_kw):
        context = self.browser.new_context(**context_kw)
        context.add_cookies([self._session_cookie(student_id)])
        self.addCleanup(context.close)
        return context.new_page()

    def _open(self, page, query: str = "") -> None:
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}{query}")
        page.wait_for_load_state("networkidle")

    def _ask(self, page, question: str) -> None:
        page.fill("#saQuestion", question)
        page.click("#saSend")
        page.wait_for_selector(".va-message-assistant", timeout=15_000)

    def _client(self) -> Client:
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=MINE, defaults={"name": "S", "program": "CS", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(MINE))
        return client

    def _turn(
        self,
        conversation: AdvisorConversation,
        answer: str = WITHDRAWAL_ANSWER,
        citations=None,
        question: str = "كم مرة؟",
        presentation=None,
    ) -> None:
        """One completed turn, through the real endpoint."""
        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            return_value=_reply(
                answer,
                citations if citations is not None else CITATIONS,
                presentation=presentation,
            ),
        ):
            response = self._client().post(
                reverse("advisor_conversation_send", args=[conversation.id]),
                data=json.dumps({"message": question}),
                content_type="application/json",
            )
        # Without this the fixture can fail silently and every caller reports a
        # 15-second selector timeout pointing at the JavaScript instead.
        assert response.status_code == 201, response.content

        # Seeding history is setup, not a student asking questions, so it must not
        # be governed by the product's generation budget. The budget itself is
        # tested directly in tests/test_advisor_rate_limit.py.
        RateLimitBucket.objects.all().delete()

    def _seed(self, answer: str = WITHDRAWAL_ANSWER, citations=None) -> AdvisorConversation:
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        self._turn(conversation, answer=answer, citations=citations)
        return conversation

    # ── 1. an explicit conversation URL survives a reload ──────
    def test_conversation_url_reload_restores_the_conversation_from_the_database(self):
        page = self._page()
        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            return_value=_reply(WITHDRAWAL_ANSWER, CITATIONS),
        ):
            self._open(page)
            self._ask(page, "كم مرة أقدر أنسحب من مقرر؟")
            page.wait_for_function("document.getElementById('saStatus').textContent === ''")

        # Sending writes ?c=<id> into the URL. A browser reload must retain that
        # explicit thread even though a fresh visit to the bare adviser URL does not.
        assert "?c=" in page.url
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".va-message-assistant")
        assert page.locator(".va-message-user").count() == 1
        shown = page.locator(".va-message-assistant .sa-body").inner_text()
        assert "خمسة انسحابات" in shown

        # The bracketed id is the validator's half of the citation. It must survive
        # in storage — the snapshots are derived from it — and must not appear in
        # the sentence the student reads.
        assert "[TU." not in shown
        stored = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT).content
        assert "[TU.WITHDRAWAL.MAXIMUM]" in stored

    # ── 2. a retry resumes the turn, it does not duplicate it ───
    def test_retry_after_failure_does_not_create_a_second_question(self):
        page = self._page()
        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            side_effect=RuntimeError("model down"),
        ):
            self._open(page)
            page.fill("#saQuestion", "كم مرة أقدر أنسحب من مقرر؟")
            page.click("#saSend")
            page.wait_for_selector(".sa-retry", timeout=15_000)

        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            return_value=_reply(WITHDRAWAL_ANSWER, CITATIONS),
        ):
            page.click(".sa-retry")
            page.wait_for_selector(".va-message-assistant", timeout=15_000)

        assert page.locator(".va-message-user").count() == 1
        assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_STUDENT).count() == 1

    # ── 3. the citation shown is the citation stored ────────────
    def test_rendered_citation_matches_the_stored_snapshot(self):
        conversation = self._seed()
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-citation")

        stored = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT).citations.all()
        assert stored.count() == 2

        # Compared whole, not by substring: "1447" and "24" both survive dropping
        # the "edition"/"p." labels, which leaves the student two bare numbers with
        # nothing saying which is the edition and which the page.
        shown = page.locator(".sa-citation-text").inner_text()
        assert shown == ", ".join(
            [stored[0].document_title, f"edition {stored[0].edition}", f"p. {stored[0].page}"]
        )

        # Both ids remain auditable inside the details panel even though the two
        # rules share one printed reference. Expand it the way a student would —
        # reading the collapsed DOM would pass even if the panel never opened.
        page.locator(".sa-citation-details summary").first.click()
        page.wait_for_selector(".sa-citation-meta dd:visible")
        # Equality, not a subset: `<=` would also accept a panel that dumped every
        # retrieved policy id in beside the ones actually cited.
        ids = page.locator(".sa-citation-policy-id").all_inner_texts()
        assert ids == [c.policy_id for c in stored]

    # ── 4. an answer with no citations shows no source panel ────
    def test_uncited_answer_renders_no_empty_source_panel(self):
        # Both turns in one thread, so the test carries its own control: without a
        # cited turn to compare against, a mutant that suppressed the panel for ALL
        # single-source answers would pass.
        conversation = self._seed(answer="لا تتوفر معلومات كافية.", citations=[])
        self._turn(conversation, answer=WITHDRAWAL_ANSWER, citations=CITATIONS[:1])

        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-citations")

        turns = page.locator(".va-message-assistant")
        assert turns.count() == 2
        assert turns.nth(0).locator(".sa-citations").count() == 0
        assert turns.nth(0).locator(".sa-citations-title").count() == 0
        assert turns.nth(1).locator(".sa-citation").count() == 1

    # ── 5. distinct references each get their own entry ─────────
    def test_multiple_distinct_citations_all_render(self):
        conversation = self._seed(
            answer=TWO_PAGE_ANSWER, citations=[*CITATIONS, CITATION_OTHER_PAGE]
        )
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-citation")
        lines = page.locator(".sa-citation-text").all_inner_texts()
        assert len(lines) == 2, lines
        pages = {re.search(r"(\d{2})\s*$", line).group(1) for line in lines}
        assert pages == {"23", "24"}

    # ── 6. a failed turn is visible and recoverable ─────────────
    def test_failed_turn_is_marked_and_offers_retry(self):
        page = self._page(
            locale="ar",
            extra_http_headers={"Accept-Language": "ar"},
            viewport={"width": 425, "height": 812},
        )
        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            side_effect=RuntimeError("model down"),
        ):
            self._open(page)
            page.fill("#saQuestion", "سؤال")
            page.click("#saSend")
            page.wait_for_selector(".sa-retry", timeout=15_000)

        failed = page.locator('.va-message-user[data-status="FAILED"]')
        assert failed.count() == 1
        status = failed.locator(".sa-retry-state .sa-status-failed")
        retry = failed.locator(".sa-retry-state .sa-retry")
        # Explain the failure once, then offer one explicit action. The previous
        # message also told the student to retry, duplicating the button below it.
        assert status.inner_text().strip() == "لم نتمكّن من إعداد الإجابة."
        assert retry.locator(".sa-retry-label").inner_text().strip() == "إعادة المحاولة"
        assert status.get_attribute("role") == "alert"
        assert status.get_attribute("aria-atomic") == "true"
        assert retry.get_attribute("aria-describedby") == status.get_attribute("id")
        assert retry.get_attribute("aria-label") == "إعادة محاولة إعداد الإجابة"
        assert retry.bounding_box()["height"] >= 44
        state_box = failed.locator(".sa-retry-state").bounding_box()
        retry_box = retry.bounding_box()
        assert retry_box["x"] >= state_box["x"]
        assert retry_box["x"] + retry_box["width"] <= state_box["x"] + state_box["width"] + 1

        # And it survives a reload — a failure the student cannot come back to is a
        # lost question.
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".sa-retry")
        assert page.locator('.va-message-user[data-status="FAILED"]').count() == 1

    # ── 7. feedback survives a reload ───────────────────────────
    def test_feedback_persists_across_reload(self):
        conversation = self._seed()
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-feedback")

        # Asking why an answer was unhelpful before the student has said it was is
        # both noise and a leading question. `hidden` alone did not achieve this:
        # the CSS made the container a flex box, and `display` outranks the
        # attribute.
        assert page.locator(".sa-fb-reasons").is_visible() is False

        page.locator(".sa-fb-btn").nth(1).click()  # "No"
        page.wait_for_selector(".sa-fb-reasons:visible")
        page.wait_for_selector('.sa-fb-btn[aria-pressed="true"]')
        # The chip sets aria-pressed SYNCHRONOUSLY, before its POST, so waiting on
        # the attribute returns immediately and the database assertion below races
        # the write. Wait for the response instead.
        with page.expect_response(
            lambda r: "/feedback/" in r.url and r.request.method == "POST" and r.status == 200
        ):
            page.locator('.sa-fb-reason[data-code="answer_incorrect"]').click()

        feedback = AdvisorFeedback.objects.get()
        assert feedback.rating == AdvisorFeedback.NOT_HELPFUL
        assert feedback.reason_codes == ["answer_incorrect"]

        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".sa-feedback")
        assert page.locator(".sa-fb-btn").nth(1).get_attribute("aria-pressed") == "true"
        # The negative half: marking BOTH buttons pressed would satisfy the line above.
        assert page.locator(".sa-fb-btn").nth(0).get_attribute("aria-pressed") == "false"
        assert (
            page.locator('.sa-fb-reason[data-code="answer_incorrect"]').get_attribute(
                "aria-pressed"
            )
            == "true"
        )

    # ── 8. another student's conversation is simply not there ───
    def test_another_students_conversation_url_reveals_nothing(self):
        theirs = AdvisorConversation.objects.create(student_id=THEIRS, title="سؤال خاص")
        AdvisorMessage.objects.create(
            conversation=theirs,
            role=AdvisorMessage.ROLE_STUDENT,
            content="معلومة حساسة عن طالب آخر",
            status=AdvisorMessage.STATUS_COMPLETED,
        )
        page = self._page()
        self._open(page, f"?c={theirs.id}")
        page.wait_for_selector(".sa-error")

        body = page.content()
        assert "معلومة حساسة عن طالب آخر" not in body
        assert "سؤال خاص" not in body
        assert page.locator(".va-message-user").count() == 0

    # ── 9. timetable evidence is a visual, read-only chat artifact ──
    def test_timetable_alternatives_render_as_responsive_read_only_cards(self):
        presentation = {
            "kind": "timetable_proposals",
            "planning_term": "1448/1",
            "mode": "from_scratch",
            "can_save": True,  # the server normaliser must force this off
            "must_take_courses": ["CS211"],
            "pinned_sections": [{"course_code": "CS211", "section_label": "M2"}],
            "constraints_satisfied": True,
            "current_sections": [
                {
                    "course_code": "AI221",
                    "course_name": "Artificial Intelligence Programming",
                    "section": "M1",
                    "credits": 4,
                    "meetings": ["SUN 16:00–17:40"],
                }
            ],
            "alternatives": [
                {
                    "planner_options": ["A1", "B1", "C1"],
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "total_credit_hours": 4,
                    "courses": [
                        {
                            "course_code": "CS211",
                            "course_name": "Algorithms and Data Structures",
                            "section": "M2",
                            "credits": 4,
                        }
                    ],
                    "meetings": [
                        {
                            "course_code": "CS211",
                            "course_name": "Algorithms and Data Structures",
                            "section": "M2",
                            "day": "MON",
                            "start": "10:30",
                            "end": "11:45",
                        }
                    ],
                    "unplaced_courses": [],
                },
                {
                    "planner_options": ["A2", "B2", "C2"],
                    "scheduled_courses": 0,
                    "target_courses": 1,
                    "total_credit_hours": 0,
                    "courses": [],
                    "meetings": [],
                    "unplaced_courses": [
                        {
                            "course_code": "CS211",
                            "course_name": "Algorithms and Data Structures",
                            "reason": "This variant did not place the course.",
                        }
                    ],
                },
            ],
        }
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        self._turn(
            conversation,
            answer="I found two Planner alternatives.",
            citations=[],
            question="Build a timetable from scratch",
            presentation=presentation,
        )
        page = self._page(viewport={"width": 375, "height": 812})
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-timetable")

        card = page.locator(".sa-timetable")
        assert card.get_attribute("aria-label") == "Timetable alternatives"
        assert page.locator(".sa-tt-option").count() == 2
        assert page.locator(".sa-tt-option").nth(0).get_attribute("open") is not None
        assert page.locator(".sa-tt-constraint-chip").count() == 2
        constraint_text = page.locator(".sa-tt-constraints").text_content()
        assert "Must take: CS211" in constraint_text
        assert "Pinned section: CS211" in constraint_text
        assert "A1 / B1 / C1" in page.locator(".sa-tt-option-name").nth(0).inner_text()
        assert "10:30–11:45" in page.locator(".sa-tt-option").nth(0).inner_text()
        assert page.locator(".sa-timetable button").count() == 0
        assert "Apply" not in card.inner_text()
        assert "Save" not in card.inner_text()

        page.locator(".sa-tt-option").nth(1).locator("summary").click()
        assert (
            "This variant did not place the course."
            in page.locator(".sa-tt-option").nth(1).inner_text()
        )

        overflow = card.evaluate("node => node.scrollWidth - node.clientWidth")
        assert overflow <= 1, f"the timetable card overflows by {overflow}px"

    def test_current_only_timetable_explains_that_there_was_no_course_to_add(self):
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        self._turn(
            conversation,
            answer="Your current timetable is retained.",
            citations=[],
            question="Build around my current sections",
            presentation={
                "kind": "timetable_proposals",
                "planning_term": "1448/1",
                "mode": "around_current",
                "current_sections": [{"course_code": "AI113", "section": "M1"}],
                "alternatives": [],
                "no_additional_courses": True,
            },
        )
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-tt-no-additional-courses")

        notice = page.locator(".sa-tt-no-additional-courses").inner_text()
        assert "no requested or recommended additional course" in notice
        assert page.locator(".sa-tt-option").count() == 0

    def test_certified_replacement_card_names_the_swap_and_outside_plan_caution(self):
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        self._turn(
            conversation,
            answer="This swap is academically better and fits the complete timetable.",
            citations=[],
            question="Replace DS341 with CS285 if it fits my timetable",
            presentation={
                "kind": "timetable_proposals",
                "planning_term": "1448/1",
                "mode": "certified_replacement",
                "baseline_kind": "REGISTERED",
                "replacement": {
                    "remove_course": {"course_code": "DS341", "credits": 3},
                    "add_course": {"course_code": "CS285", "credits": 4},
                    "outside_plan_addition": True,
                    "academic_improvement": {
                        "proven_improvement": True,
                        "terms_saved": 1,
                    },
                },
                "alternatives": [
                    {
                        "planner_options": ["A1"],
                        "scheduled_courses": 1,
                        "target_courses": 1,
                        "total_credit_hours": 4,
                        "courses": [{"course_code": "CS285", "section": "M3", "credits": 4}],
                        "meetings": [
                            {
                                "course_code": "CS285",
                                "section": "M3",
                                "day": "MON",
                                "start": "10:30",
                                "end": "11:45",
                            }
                        ],
                        "unplaced_courses": [],
                    }
                ],
            },
        )
        page = self._page(viewport={"width": 375, "height": 812})
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-tt-replacement")

        banner = page.locator(".sa-tt-replacement")
        assert "Replace DS341 with CS285" in " ".join(banner.inner_text().split())
        assert page.locator(".sa-tt-replacement-code").count() == 2
        assert page.locator(".sa-tt-replacement-code").nth(0).get_attribute("dir") == "ltr"
        assert (
            "outside your recorded study plan"
            in page.locator(".sa-tt-replacement-caution").inner_text()
        )
        assert banner.evaluate("node => node.scrollWidth - node.clientWidth") <= 1

    def test_arabic_timetable_card_keeps_codes_and_times_left_to_right(self):
        presentation = {
            "kind": "timetable_proposals",
            "planning_term": "1448/1",
            "mode": "certified_replacement",
            "replacement": {
                "remove_course": {"course_code": "DS341", "credits": 3},
                "add_course": {"course_code": "CS211", "credits": 4},
                "outside_plan_addition": False,
                "academic_improvement": {"proven_improvement": True, "terms_saved": 1},
            },
            "alternatives": [
                {
                    "planner_options": ["A1", "B1", "C1"],
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "total_credit_hours": 4,
                    "courses": [{"course_code": "CS211", "section": "M2", "credits": 4}],
                    "meetings": [
                        {
                            "course_code": "CS211",
                            "section": "M2",
                            "day": "MON",
                            "start": "10:30",
                            "end": "11:45",
                        }
                    ],
                    "unplaced_courses": [],
                }
            ],
        }
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        self._turn(
            conversation,
            answer="هذه خيارات المخطط.",
            citations=[],
            question="ابنِ لي جدولًا جديدًا من البداية",
            presentation=presentation,
        )
        page = self._page(
            viewport={"width": 375, "height": 812},
            locale="ar",
            extra_http_headers={"Accept-Language": "ar"},
        )
        page.context.add_cookies(
            [{"name": "django_language", "value": "ar", "url": self.live_server_url}]
        )
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-timetable")

        card = page.locator(".sa-timetable")
        assert card.get_attribute("dir") == "rtl"
        replacement = page.locator(".sa-tt-replacement")
        assert "استبدال DS341 بالمقرر CS211" in " ".join(replacement.inner_text().split())
        assert page.locator(".sa-tt-replacement-caution").count() == 0
        assert "الجدول المقترح" in page.locator(".sa-tt-option-name").inner_text()
        assert page.locator(".sa-tt-option-name bdi").get_attribute("dir") == "ltr"
        assert page.locator(".sa-tt-time").get_attribute("dir") == "ltr"
        assert page.locator(".sa-tt-time").inner_text() == "10:30–11:45"
        assert card.evaluate("node => node.scrollWidth - node.clientWidth") <= 1

    def test_graduation_scenario_renders_the_shared_tree_and_mobile_term_list(self):
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        wide_history = [f"EL{index:02d}" for index in range(14)]
        self._turn(
            conversation,
            answer="The scenario needs at least three terms and still has one unresolved requirement.",
            citations=[],
            question="How many terms until graduation?",
            presentation={
                "kind": "graduation_scenario",
                "program": "DS2",
                "planning_term": "1448/1",
                "simulation_completed": False,
                "lower_bound_terms_including_current": 3,
                "max_credits_per_term": 18,
                "band_labels": {
                    "0": "Completed before the scenario",
                    # A persisted pre-rename payload: the UI must continue to
                    # read it but present the term as a planning baseline.
                    "1": "Current 1448/1",
                    "2": "Projected 1448/2",
                },
                "graph": {
                    "items": [
                        {
                            "course_code": "DS225",
                            "prerequisite_course_code": "CS113",
                        },
                        {
                            "course_code": "DS341",
                            "prerequisite_course_code": "DS225",
                        },
                    ],
                    "termOf": {
                        "CS113": 0,
                        "DS225": 1,
                        "DS341": 2,
                        **dict.fromkeys(wide_history, 0),
                    },
                    "nameOf": {"DS341": "Data Governance"},
                    "statusOf": {
                        "CS113": "passed",
                        "DS225": "studying",
                        "DS341": "open",
                        **dict.fromkeys(wide_history, "passed"),
                    },
                    "extraNodes": ["CS113", "DS225", "DS341", *wide_history],
                },
                "unresolved_requirements": [
                    {
                        "code": "DS492",
                        "name": "Graduation Project",
                        "missing_prerequisites": ["MATH204"],
                        "credit_hour_gate": {"required": 147, "remaining": 7},
                    }
                ],
                "read_only": False,
            },
        )
        page = self._page(viewport={"width": 1280, "height": 900})
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-graduation-map .sa-grad-mobile")

        card = page.locator(".sa-graduation-map")
        assert "Scenario path to plan completion" in card.inner_text()
        assert "not a final graduation date" in card.inner_text()
        assert page.locator(".sa-grad-toolbar .pg-mode").count() == 2
        assert page.locator(".sa-grad-mobile").is_visible()
        assert not page.locator(".sa-grad-desktop").is_visible()
        assert page.locator(".prereq-svg").count() == 0
        assert page.locator(".sa-grad-more").count() == 1
        assert not page.locator(".sa-grad-more").get_attribute("open")
        messages = page.locator(".va-messages")
        assert messages.evaluate("node => node.scrollWidth - node.clientWidth") <= 1
        assert "DS492" in page.locator(".sa-grad-blockers").inner_text()
        assert "MATH204" in page.locator(".sa-grad-blockers").inner_text()
        assert "register courses" in card.inner_text()
        assert page.locator(".sa-graduation-map button").count() == 3

        expand = page.locator(".sa-grad-expand")
        assert expand.get_attribute("aria-expanded") == "false"
        expand.click()
        page.wait_for_selector(".sa-grad-desktop .prereq-svg")
        assert card.evaluate("node => node.classList.contains('is-expanded')")
        assert page.get_attribute("html", "class").find("sa-overlay-open") >= 0
        assert page.locator(".sa-grad-desktop").is_visible()
        assert not page.locator(".sa-grad-mobile").is_visible()
        svg_text = page.locator(".prereq-svg").text_content()
        assert "Planning baseline term 1448/1" in svg_text
        assert "Current term 1448/1" not in svg_text
        assert "Projected term 1448/2" in svg_text
        page.keyboard.press("Escape")
        assert expand.get_attribute("aria-expanded") == "false"
        assert not card.evaluate("node => node.classList.contains('is-expanded')")
        assert page.locator(".sa-grad-mobile").is_visible()
        assert not page.locator(".sa-grad-desktop").is_visible()
        assert messages.evaluate("node => node.scrollWidth - node.clientWidth") <= 1

        page.set_viewport_size({"width": 375, "height": 812})
        assert page.locator(".sa-grad-mobile").is_visible()
        assert not page.locator(".sa-grad-desktop").is_visible()
        assert "DS341" in page.locator(".sa-grad-mobile").inner_text()
        assert card.evaluate("node => node.scrollWidth - node.clientWidth") <= 1

    # ── 10. Arabic reads right-to-left and fits a phone ─────────
    def test_arabic_rtl_layout_fits_a_phone_without_horizontal_scroll(self):
        conversation = self._seed()
        for _ in range(4):
            self._turn(conversation)
        page = self._page(
            viewport={"width": 375, "height": 812},
            locale="ar",
            extra_http_headers={"Accept-Language": "ar"},
        )
        page.context.add_cookies(
            [{"name": "django_language", "value": "ar", "url": self.live_server_url}]
        )
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        assert page.get_attribute("html", "dir") == "rtl"
        # `main` sets overflow-x: hidden, so documentElement CANNOT report overflow —
        # measuring it passes even when the layout is catastrophically wide. Measure
        # the scrolling ancestor, which is where the overflow actually lands.
        overflow = page.evaluate(
            """() => {
                const doc = document.documentElement;
                const main = document.querySelector('main') || doc;
                return Math.max(main.scrollWidth - main.clientWidth,
                                doc.scrollWidth - doc.clientWidth);
            }"""
        )
        assert overflow <= 1, f"the adviser screen overflows by {overflow}px at 375px"

        # The composer must still be usable, not pushed off-screen.
        box = page.locator("#saSend").bounding_box()
        assert box and 0 <= box["x"] and box["x"] + box["width"] <= 375 + 1

        # `.va-chat` is shared with a page whose first child this one lacks, so its
        # row template lands the flexible row on the suggestion chips and gives the
        # thread an `auto` row — which sizes to content, so `overflow: auto` never
        # scrolls and the composer is pushed below the fold.
        metrics = page.evaluate(
            """() => {
                const m = document.querySelector('.va-messages');
                const ex = document.querySelector('.va-examples');
                return {
                    scrolls: m.scrollHeight > m.clientHeight + 1,
                    thread: m.clientHeight,
                    examples: ex.getBoundingClientRect().height,
                    composerBottom: document.querySelector('.va-composer')
                        .getBoundingClientRect().bottom,
                };
            }"""
        )
        assert metrics["scrolls"], "the message thread cannot scroll"
        assert metrics["thread"] > metrics["examples"], metrics
        assert metrics["composerBottom"] <= 812 + 1, metrics
        assert 0 <= 812 - metrics["composerBottom"] <= 40, metrics

    # ── 15. the desktop thread scrolls inside itself ──
    def test_desktop_thread_scrolls_internally_with_the_composer_in_view(self):
        """`.va-chat` is shared with a page whose first child this one lacks.

        Inheriting its row template shifts every row by one: the flexible row lands
        on the suggestion chips and the thread gets an `auto` row, so it sizes to
        its content and its `overflow: auto` never scrolls. Answering then appears
        to do nothing — the view cannot move to the reply, and the composer is
        pushed below the fold.
        """
        conversation = self._seed()
        for _ in range(6):
            self._turn(conversation)

        page = self._page(viewport={"width": 1280, "height": 800})
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        metrics = page.evaluate(
            """() => {
                const m = document.querySelector('.va-messages');
                return {
                    scrolls: m.scrollHeight > m.clientHeight + 1,
                    composerBottom: document.querySelector('.va-composer')
                        .getBoundingClientRect().bottom,
                    composerHeight: document.querySelector('.va-composer')
                        .getBoundingClientRect().height,
                    examplesShown: getComputedStyle(
                        document.querySelector('.va-examples')).display !== 'none',
                };
            }"""
        )
        assert metrics["scrolls"], "the thread grew instead of scrolling"
        # The inherited template reserves a 420px minimum for its second row. With
        # this page's children that row is the composer, so a single-line input is
        # handed a third of the screen and the thread is starved of it.
        assert metrics["composerHeight"] < 120, metrics
        assert metrics["composerBottom"] <= 801, metrics
        assert 0 <= 800 - metrics["composerBottom"] <= 40, metrics
        # The suggestion chips are onboarding; once there is a conversation they are
        # clutter between the thread and the input.
        assert metrics["examplesShown"] is False

        messages = page.locator(".va-messages")
        messages.evaluate(
            "node => { node.style.scrollBehavior = 'auto'; node.scrollTop = 0; "
            "node.dispatchEvent(new Event('scroll')); }"
        )
        page.wait_for_function("() => !document.querySelector('#saJumpLatest').hidden")
        assert page.locator("#saJumpLatest").is_visible()
        page.locator("#saJumpLatest").click()
        remaining = messages.evaluate(
            "node => node.scrollHeight - node.clientHeight - node.scrollTop"
        )
        assert remaining <= 1

    def test_bare_url_starts_new_and_keeps_history_in_the_drawer(self):
        conversation = self._seed()
        page = self._page(viewport={"width": 1280, "height": 800})
        self._open(page)

        assert page.locator("#main-content").get_attribute("data-page") == "student-advisor"
        assert not page.locator(".page-header").is_visible()
        assert not page.locator(".page-intro").is_visible()
        assert page.locator("#saEmptyState").is_visible()
        assert page.locator(".va-message-user").count() == 0
        assert page.locator(".va-message-assistant").count() == 0
        assert "?c=" not in page.url

        drawer = page.locator("#saConversationDrawer")
        toggle = page.locator("#saHistoryToggle")
        assert drawer.get_attribute("aria-hidden") == "true"
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        drawer.wait_for(state="visible")
        assert drawer.locator(".sa-drawer-logout").is_visible()
        assert drawer.locator(".sa-conv").count() == 1
        assert drawer.locator(".sa-conv").get_attribute("aria-current") == "false"

        drawer.locator(".sa-conv").click()
        page.wait_for_selector(".va-message-assistant")
        assert f"?c={conversation.id}" in page.url

        page.locator("#saNewChat").click()
        assert page.locator("#saEmptyState").is_visible()
        examples = page.locator("#saExamples [data-sa-example]")
        assert examples.count() == 6
        prompts = examples.evaluate_all("nodes => nodes.map(node => node.dataset.saExample)")
        assert prompts == [
            "Approximately how many terms remain until I complete my degree plan?",
            "Which courses can I take this term?",
            "Build a proposed timetable around my current sections without clashes.",
            "Does my current timetable have any clashes?",
            "Can replacing a current course improve my graduation plan?",
            "How many times may I withdraw from a course?",
        ]
        assert "?c=" not in page.url

        composer = page.locator("#saQuestion")
        assert composer.evaluate("node => node.tagName") == "TEXTAREA"
        examples.nth(2).click()
        assert composer.input_value() == prompts[2]
        initial_height = composer.bounding_box()["height"]
        composer.fill("First line\nSecond line\nThird line")
        assert composer.bounding_box()["height"] > initial_height

    def test_arabic_empty_state_uses_formal_starter_questions(self):
        page = self._page(
            locale="ar",
            extra_http_headers={"Accept-Language": "ar"},
        )
        page.context.add_cookies(
            [{"name": "django_language", "value": "ar", "url": self.live_server_url}]
        )
        self._open(page)

        assert page.locator("#saEmptyState h3").inner_text() == "كيف يمكنني مساعدتك اليوم؟"
        examples = page.locator("#saExamples [data-sa-example]")
        prompts = examples.evaluate_all("nodes => nodes.map(node => node.dataset.saExample)")
        assert prompts == [
            "ما المدة التقديرية المتبقية لإكمال متطلبات خطتي الدراسية؟",
            "ما المقررات التي استوفيت متطلباتها الأكاديمية؟",
            "أنشئ لي جدولًا مقترحًا حول شُعبي المسجّلة فعليًا، من دون تعارض بين الأوقات المسجّلة.",
            "هل توجد تعارضات زمنية في الجدول المسجّل فعليًا؟",
            "هل يؤثر استبدال أحد مقررات الجدول المسجّل فعليًا في المسار التقديري لإكمال الخطة؟",
            "ما الحد الأقصى لعدد مرات الانسحاب من مقرر؟",
        ]

    def test_new_answer_thinks_then_reveals_prose_before_evidence(self):
        page = self._page(viewport={"width": 1280, "height": 800})
        page.add_init_script("window.__SA_FORCE_PROGRESSIVE_REVEAL__ = true")
        long_answer = "\n\n".join([WITHDRAWAL_ANSWER] * 8)

        def slow_answer(*args, **kwargs):
            time.sleep(0.35)
            return _reply(long_answer, CITATIONS)

        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            side_effect=slow_answer,
        ):
            self._open(page)
            page.fill("#saQuestion", "How many times may I withdraw from a course?")
            page.click("#saSend")

            thinking = page.locator("#saThinkingMessage")
            thinking.wait_for(state="visible")
            assert thinking.locator(".sa-thinking-dots > span").count() == 3

            page.wait_for_selector(".va-message-assistant.is-revealing", timeout=15_000)
            answer = page.locator(".va-message-assistant.is-revealing")
            partial = answer.locator(".sa-body").inner_text()
            assert partial.strip()
            assert len(partial) < len(long_answer)
            assert not answer.locator(".sa-citations").is_visible()

            page.wait_for_function("() => !document.querySelector('.is-revealing')")
            completed = page.locator(".va-message-assistant")
            assert len(completed.locator(".sa-body").inner_text()) > len(partial)
            assert completed.locator(".sa-citations").is_visible()

    # ── 16. a turn abandoned mid-generation is recoverable ──
    def test_an_abandoned_turn_can_be_retried_from_the_screen(self):
        """A killed worker leaves the turn on PENDING and nothing moves it on.

        Driving Retry off `status === 'FAILED'` leaves that question showing
        "Preparing the answer…" for ever — a lost question wearing a spinner.
        """
        from django.utils import timezone

        from core.advisor_conversation_views import STALE_GENERATION

        conversation = AdvisorConversation.objects.create(student_id=MINE)
        AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_STUDENT,
            content="سؤال مهجور",
            idempotency_key="k-abandoned",
            request_hash=hashlib.sha256("سؤال مهجور".encode()).hexdigest(),
            status=AdvisorMessage.STATUS_PENDING,
            generation_started_at=timezone.now() - STALE_GENERATION - timedelta(minutes=1),
        )

        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-retry")

        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            return_value=_reply(WITHDRAWAL_ANSWER, CITATIONS),
        ):
            page.click(".sa-retry")
            page.wait_for_selector(".va-message-assistant", timeout=15_000)

        assert page.locator(".va-message-user").count() == 1
        assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_STUDENT).count() == 1

    # ── 17. a rate limit must say how long ──
    def test_a_rate_limited_send_names_the_wait_and_holds_the_button(self):
        """ "Please try again" plus a disabled button is a dead end.

        The server returns Retry-After and a wait in the body; the client used to
        throw both away and tell the student to do immediately the one thing that
        cannot work.
        """
        from core.services import rate_limit

        conversation = AdvisorConversation.objects.create(student_id=MINE)
        limit, _window = rate_limit.LIMITS[rate_limit.GENERATION]
        for _ in range(limit):
            rate_limit.consume(rate_limit.GENERATION, MINE)

        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.fill("#saQuestion", "سؤال")
        page.click("#saSend")
        page.wait_for_selector("#saComposerError:visible", timeout=15_000)

        message = page.locator("#saComposerError").inner_text()
        assert any(ch.isdigit() for ch in message), message
        assert page.locator("#saSend").is_disabled() is True
        # The question is kept — it was never sent.
        assert page.locator("#saQuestion").input_value() == "سؤال"

    # ── 18. a refusal on the way TO asking must explain itself too ──
    def test_a_rate_limited_conversation_create_names_the_wait(self):
        """The client creates a conversation on its way to every question.

        A refusal there used to return a bare null, so the screen said "could not
        send" with no wait and a live button — the exact dead end the send path
        was fixed to avoid.
        """
        from core.services import rate_limit

        limit, _window = rate_limit.LIMITS[rate_limit.CONVERSATION]
        for _ in range(limit):
            rate_limit.consume(rate_limit.CONVERSATION, MINE)

        page = self._page()
        self._open(page)  # no ?c=, and no conversation exists: send must create one
        page.fill("#saQuestion", "سؤال")
        page.click("#saSend")
        page.wait_for_selector("#saComposerError:visible", timeout=15_000)

        message = page.locator("#saComposerError").inner_text()
        assert any(ch.isdigit() for ch in message), message
        assert page.locator("#saSend").is_disabled() is True
        assert page.locator("#saQuestion").input_value() == "سؤال"

    # ── 14. the page must not leak its own source ──
    def test_no_raw_template_syntax_reaches_the_page(self):
        """`{# ... #}` is single-line ONLY.

        A multi-line one is not parsed as a comment at all — it renders into the
        student's conversation thread as literal template source.
        """
        conversation = self._seed()
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")
        body = page.locator("body").inner_text()
        for token in ("{#", "#}", "{%", "%}"):
            assert token not in body, f"{token} rendered to the student: {body[:400]}"

    # ── 11. bracketed text that is not a policy id must survive ──
    def test_the_withdrawal_grade_is_not_mistaken_for_a_policy_marker(self):
        """`[W]` is the withdrawal grade, on the screen that explains withdrawal.

        The first stripper matched any bracketed capitals, so the answer
        "you will be recorded a grade of [W]" reached the student as "you will be
        recorded a grade of" — a sentence missing the thing it was about.
        """
        answer = (
            "سيُرصد لك تقدير [W] في السجل، ومعدلك [GPA] لا يتأثر، "
            "ويمكنك إعادة [CS101] لاحقًا «الدليل الإرشادي للطالب والطالبة، ص 24 "
            "[TU.WITHDRAWAL.MAXIMUM]»."
        )
        conversation = self._seed(answer=answer, citations=CITATIONS[:1])
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        shown = page.locator(".va-message-assistant .sa-body").inner_text()
        assert "[W]" in shown
        assert "[GPA]" in shown
        assert "[CS101]" in shown
        assert "[TU.WITHDRAWAL.MAXIMUM]" not in shown

    # ── 12. two failed turns must each retry themselves ──
    def test_retrying_the_older_of_two_failed_turns_still_works(self):
        """One key slot for the whole page sends the wrong key to the wrong turn.

        The second failure overwrote it, so retrying the FIRST question sent the
        second's key with the first's text — refused as a reused key carrying a
        different question, leaving Retry looking broken and then duplicating.
        """
        page = self._page()
        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            side_effect=RuntimeError("model down"),
        ):
            self._open(page)
            for question in ("السؤال الأول", "السؤال الثاني"):
                page.fill("#saQuestion", question)
                page.click("#saSend")
                page.wait_for_function(
                    "n => document.querySelectorAll('.sa-retry').length === n",
                    arg=1 if question == "السؤال الأول" else 2,
                )

        # Reload first. A key remembered only in page memory does not survive one,
        # so this is where a client-invented key mints a fresh one and asks the
        # question a second time instead of resuming it.
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".sa-retry")

        with mock.patch(
            "core.services.student_advisor_v2.answer_student_advisor",
            return_value=_reply(WITHDRAWAL_ANSWER, CITATIONS),
        ):
            page.locator(".sa-retry").first.click()  # the OLDER turn
            page.wait_for_selector(".va-message-assistant", timeout=15_000)

        assert page.locator(".va-message-user").count() == 2
        answered = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT)
        assert answered.in_reply_to is not None
        assert answered.in_reply_to.content == "السؤال الأول"

    # ── 13. a dropped connection must not lock the composer ──
    def test_a_network_failure_leaves_the_composer_usable(self):
        page = self._page()
        self._open(page)
        page.route("**/send/", lambda route: route.abort())

        page.fill("#saQuestion", "سؤال")
        page.click("#saSend")
        page.wait_for_selector("#saComposerError:visible", timeout=15_000)

        # fetch REJECTS here rather than resolving; an unguarded await skipped the
        # line that re-enables these and the student was locked out until reload.
        #
        # WAITED for, not sampled. `is_disabled()` is a point-in-time read with no
        # auto-wait, and the composer is disabled from the moment Send is pressed
        # until the failure is handled — so the assertion was racing a window that
        # is invisible on an idle machine and wide enough to lose on a loaded one.
        # It passed alone and in file order, failed intermittently in the full
        # suite, and finally failed on a CI runner; that is the window, not the
        # behaviour.
        from playwright.sync_api import expect

        expect(page.locator("#saQuestion")).to_be_enabled(timeout=15_000)
        expect(page.locator("#saSend")).to_be_enabled(timeout=15_000)
        assert page.locator("#saQuestion").input_value() == "سؤال"

    # ── 14. an Arabic answer inside an English interface ────────
    def test_an_arabic_answer_lays_out_right_to_left_in_an_english_ui(self):
        """The bubble had no direction, so it inherited the PAGE's.

        With `dir="ltr"` the bidi algorithm reorders whole segments of every
        wrapped line and moves trailing punctuation to the visual left — the
        sentence is not misaligned, it is unreadable. And only for students who
        chose the English interface, which is why it survived.
        """
        conversation = self._seed()
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        # The page really is LTR: without that, this test proves nothing.
        assert page.evaluate("document.documentElement.getAttribute('dir')") == "ltr"

        # Asserted on the PARAGRAPH, not the wrapper: `dir="auto"` skips descendants
        # that carry their own `dir`, so a wrapper of dir'd children has no strong
        # character to judge by and resolves to the page direction. The first
        # version of this test asserted on the wrapper and failed against a DOM
        # that was already correct.
        direction = page.evaluate(
            "getComputedStyle(document.querySelector("
            "'.va-message-assistant .sa-body .sa-para')).direction"
        )
        assert direction == "rtl", "an Arabic answer is being laid out left-to-right"

    def test_the_models_markdown_is_rendered_not_printed(self):
        """`**bold**` and `* item` were reaching the student as literal asterisks."""
        conversation = self._seed(
            answer=(
                "بناءً على البيانات المتاحة:\n"
                "* **المقرر:** DS341\n"
                "* **رقم الشعبة:** M1\n"
                "\n"
                "**ملاحظة هامة:** راجع عمادة القبول والتسجيل."
            ),
            citations=[],
        )
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        body = page.locator(".va-message-assistant .sa-body")
        text = body.inner_text()
        assert "**" not in text, "markdown emphasis reached the student as asterisks"
        assert "* " not in text, "a list bullet reached the student as an asterisk"

        assert body.locator("ul.sa-list li").count() == 2
        assert body.locator("strong").count() >= 3
        assert "المقرر" in body.locator("strong").first.inner_text()
        # The direction is stated ONCE, on the body, and inherited. Per-item
        # `dir="auto"` was the previous design and it gave one answer two
        # directions — see tests/test_advisor_bidi.py, which measures the
        # consequence rather than the attribute.
        assert body.get_attribute("dir") == "rtl"
        assert body.locator("ul.sa-list li[dir]").count() == 0

    def test_an_answer_cannot_smuggle_markup_into_the_page(self):
        """Rendered with createElement/createTextNode only — never innerHTML."""
        conversation = self._seed(
            answer="<img src=x onerror=alert(1)> **<b>bold</b>**",
            citations=[],
        )
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

        body = page.locator(".va-message-assistant .sa-body")
        assert body.locator("img").count() == 0
        assert body.locator("b").count() == 0
        assert "<img" in body.inner_text(), "the text itself must survive as text"

    # ── 10. reachable by keyboard, describable by a screen reader ──
    def test_keyboard_and_screen_reader_affordances(self):
        conversation = self._seed()
        page = self._page()
        self._open(page, f"?c={conversation.id}")
        page.wait_for_selector(".sa-feedback")

        assert page.locator("label[for='saQuestion']").count() == 1

        # Announcements go to a dedicated region. Putting aria-live on the thread —
        # which is emptied and rebuilt on every render — makes a screen reader
        # recite the entire conversation after each answer.
        assert page.get_attribute("#saStatus", "aria-live") == "polite"
        assert page.get_attribute("#saMessages", "aria-live") is None

        # The sidebar entries must still be buttons. An explicit role="listitem"
        # replaces the implicit button role and they stop being announced as
        # actionable at all.
        assert page.locator(".sa-conv-list li > button.sa-conv").count() >= 1

        # Each rating button must name the question AND its own answer. `len > 4`
        # accepted two identical labels, and accepted them swapped — so the button
        # reading "Yes" announced itself as "No".
        buttons = page.locator(".sa-fb-btn")
        labels = [buttons.nth(i).get_attribute("aria-label") for i in range(2)]
        texts = [buttons.nth(i).inner_text().strip() for i in range(2)]
        prompt = page.locator(".sa-feedback-q").inner_text().strip()
        assert labels[0] != labels[1]
        for label, text in zip(labels, texts, strict=True):
            assert label.startswith(prompt), label
            assert label.endswith(text), (label, text)

        # Toggles announce their state before being pressed, not only after.
        assert buttons.nth(0).get_attribute("aria-pressed") == "false"

        # The whole turn is operable without a mouse.
        page.locator("#saQuestion").focus()
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.id") == "saSend"
        page.locator(".sa-fb-btn").first.focus()
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => document.querySelector('.sa-fb-btn').getAttribute('aria-pressed') === 'true'"
        )
        assert AdvisorFeedback.objects.get().rating == AdvisorFeedback.HELPFUL
