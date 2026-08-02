"""The whole loop, in one browser, as two people.

Student asks → the adviser abstains → the student previews what will be shared →
one case is created → an authorised adviser picks it up → records an internal note
and a separate reply → resolves → the student reloads and sees the status and the
reply, and never the note.

Each half is covered in detail elsewhere. What this proves is that the halves meet:
the pieces are joined by a stored reference and a persisted snapshot rather than
by anything held in a page.
"""

from __future__ import annotations

import json
import os

# Playwright's synchronous API drives a greenlet event loop; see test_advisor_browser.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from unittest import mock  # noqa: E402

import pytest  # noqa: E402
from django.contrib.auth.models import Group, User  # noqa: E402
from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from core.models import (  # noqa: E402
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorEscalationEvent,
    AdvisorMessage,
    RateLimitBucket,
    Student,
)
from core.services.rbac import (  # noqa: E402
    ROLE_ADVISOR,
    ensure_role_groups,
    set_user_scope,
)

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

STUDENT_ID = 9601001
ADVISER_ID = "A42"

REFUSAL = (
    "لا يستطيع النظام البت في حالتك الشخصية، لأن اللائحة تحصر هذا القرار "
    "بالمرشد الأكاديمي «الدليل الإرشادي للطالب والطالبة، ص 24 "
    "[TU.WITHDRAWAL.MAXIMUM]»."
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
    }
]


def _abstaining_answer() -> dict:
    """A turn the system will not decide: the rule governs, and forbids deciding."""
    return {
        "ok": True,
        "answer": REFUSAL,
        "model": "stub",
        "citations": CITATIONS,
        "cited_policy_ids": ["TU.WITHDRAWAL.MAXIMUM"],
        "agent": {
            "loop_used": True,
            "policy_grounding": "retrieved",
            "tools_called": [{"tool": "my_progress"}],
            "tool_results": [
                {
                    "tool": "policy_lookup",
                    "ok": True,
                    "direct_policy_evidence": [
                        {
                            "policy_id": "TU.WITHDRAWAL.MAXIMUM",
                            "decision_use": "PROHIBITED_FOR_DECISION",
                        }
                    ],
                    "background_policy_evidence": [],
                    "conflicting_policy_evidence": [],
                },
                {"tool": "my_progress", "ok": True},
            ],
        },
    }


class EndToEndTests(StaticLiveServerTestCase):
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

    def _student_page(self):
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.update_or_create(
            student_id=STUDENT_ID,
            defaults={"name": "طالب", "program": "CS", "advisor_id": ADVISER_ID},
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(STUDENT_ID))
        context = self.browser.new_context(
            locale="ar", extra_http_headers={"Accept-Language": "ar"}
        )
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                },
                {"name": "django_language", "value": "ar", "url": self.live_server_url},
            ]
        )
        self.addCleanup(context.close)
        return context.new_page()

    def _adviser_client(self) -> Client:
        ensure_role_groups()
        adviser = User.objects.create_user("adviser-e2e", password="x")
        adviser.groups.add(Group.objects.get(name=ROLE_ADVISOR))
        set_user_scope(adviser.id, advisor_id=ADVISER_ID)
        client = Client()
        client.force_login(adviser)
        return client

    def _act(self, client, reference, **body):
        return client.post(
            reverse("advisor_inbox_case_action", args=[reference]),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_a_question_the_system_will_not_decide_reaches_a_person_and_comes_back(self):
        page = self._student_page()

        # ── the student asks, and the adviser declines to decide ──
        with mock.patch(
            "core.services.virtual_advisor.answer_virtual_advisor",
            return_value=_abstaining_answer(),
        ):
            page.goto(f"{self.live_server_url}{reverse('student_advisor')}")
            page.wait_for_load_state("networkidle")
            page.fill("#saQuestion", "هل أقدر أنسحب من مقرر هذا الفصل؟")
            page.click("#saSend")
            page.wait_for_selector(".va-message-assistant", timeout=15_000)

        answer = AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_ASSISTANT)
        assert answer.final_disposition == "ABSTAIN"
        assert "PROHIBITED_FOR_DECISION" in answer.reason_codes

        # ── the offer is prominent, and the preview is honest ──
        page.wait_for_selector(".sa-escalate.is-prominent")
        page.click(".sa-escalate-btn")
        page.wait_for_selector(".sa-preview")
        preview = page.locator(".sa-preview").inner_text()
        assert "سؤالك" in preview
        assert "لن يتم إرسال المحادثات الأخرى" in preview

        page.fill(".sa-preview-note", "أحتاج قرارًا قبل نهاية الأسبوع.")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        case = AdvisorEscalation.objects.get()
        reference = case.reference
        assert page.locator(".sa-case-ref").inner_text() == reference
        assert "جديدة" in page.locator(".sa-case-status").inner_text()
        # The turn now agrees with the case.
        answer.refresh_from_db()
        assert answer.final_disposition == "ESCALATE"
        # ...but keeps the reasons that constrained the ANSWER.
        assert answer.reason_codes == ["PROHIBITED_FOR_DECISION"]

        # ── the adviser who advises this student can see it ──
        adviser = self._adviser_client()
        queue = adviser.get(reverse("advisor_inbox")).content.decode()
        assert reference in queue

        detail = adviser.get(reverse("advisor_inbox_case", args=[reference])).content.decode()
        assert "هل أقدر أنسحب من مقرر هذا الفصل؟" in detail
        assert "أحتاج قرارًا قبل نهاية الأسبوع." in detail
        assert "TU.WITHDRAWAL.MAXIMUM" in detail

        # ── they take it, note something private, and reply ──
        assert self._act(adviser, reference, action="assign_to_me").status_code == 200
        assert (
            self._act(
                adviser,
                reference,
                action="add_note",
                text="راجعت سجله: لديه انسحابان سابقان.",
            ).status_code
            == 200
        )
        assert (
            self._act(
                adviser,
                reference,
                action="record_response",
                text="تمت الموافقة على انسحابك. راجع عمادة القبول والتسجيل خلال الأسبوع.",
            ).status_code
            == 200
        )
        assert (
            self._act(
                adviser, reference, action="set_status", status=AdvisorEscalation.Status.RESOLVED
            ).status_code
            == 200
        )

        case.refresh_from_db()
        assert case.status == AdvisorEscalation.Status.RESOLVED
        assert case.resolved_by is not None
        assert case.resolved_at is not None

        # ── and the student, on reload, sees the outcome and not the note ──
        page.reload()
        page.wait_for_selector(".sa-case-reply")

        assert page.locator(".sa-case-ref").inner_text() == reference
        assert "تمت المعالجة" in page.locator(".sa-case-status").inner_text()
        assert "تمت الموافقة على انسحابك" in page.locator(".sa-case-reply").inner_text()

        body = page.content()
        assert "انسحابان سابقان" not in body, "the adviser's private note reached the student"
        assert "adviser_notes" not in body

        # ── and the whole thing is accounted for ──
        kinds = set(case.events.values_list("kind", flat=True))
        assert {
            AdvisorEscalationEvent.Kind.VIEWED,
            AdvisorEscalationEvent.Kind.ASSIGNED,
            AdvisorEscalationEvent.Kind.NOTE_ADDED,
            AdvisorEscalationEvent.Kind.RESPONSE_RECORDED,
            AdvisorEscalationEvent.Kind.STATUS_CHANGED,
        } <= kinds

    def test_one_case_survives_the_student_pressing_send_twice(self):
        """The same journey, interrupted the way a real one is."""
        page = self._student_page()
        conversation = AdvisorConversation.objects.create(student_id=STUDENT_ID)
        asked = AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_STUDENT,
            content="سؤال",
            status=AdvisorMessage.STATUS_COMPLETED,
        )
        AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_ASSISTANT,
            in_reply_to=asked,
            content="لا يمكن البت.",
            final_disposition="ABSTAIN",
            reason_codes=["PROHIBITED_FOR_DECISION"],
            status=AdvisorMessage.STATUS_ABSTAINED,
        )
        RateLimitBucket.objects.all().delete()

        page.goto(f"{self.live_server_url}{reverse('student_advisor')}?c={conversation.id}")
        page.wait_for_selector(".sa-escalate-btn")
        page.click(".sa-escalate-btn")
        page.wait_for_selector(".sa-preview")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        first = AdvisorEscalation.objects.get().reference

        # Reload and press again: the button is gone, and the case is the same one.
        page.reload()
        page.wait_for_selector(".sa-case")
        assert page.locator(".sa-escalate-btn").count() == 0
        assert AdvisorEscalation.objects.count() == 1
        assert page.locator(".sa-case-ref").inner_text() == first
