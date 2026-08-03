"""Asking for a person, from the screen.

The rules this screen has to hold are mostly about restraint: offer the action
without implying anything has been agreed, show the boundary of what leaves the
conversation before anything does, and never render the stored case evidence into
the page that was built to keep it out.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta

# Playwright's synchronous API drives a greenlet event loop, so Django blocks the
# ORM calls the assertions need. See tests/test_advisor_browser.py.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402
from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from core.models import (  # noqa: E402
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorMessage,
    AdvisorMessageCitation,
    FinalDisposition,
    RateLimitBucket,
    Student,
)
from core.services.advisor_outcome import ReasonCode  # noqa: E402
from core.services.rbac import ensure_role_groups  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

MINE = 9401001


class EscalationUiTests(StaticLiveServerTestCase):
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
    def _cookie(self) -> dict:
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=MINE, defaults={"name": "S", "program": "CS", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(MINE))
        return {
            "name": "sessionid",
            "value": client.cookies["sessionid"].value,
            "url": self.live_server_url,
        }

    def _page(self, **context_kw):
        # Arabic on every channel the middleware consults: the header decides when
        # no cookie has been set yet, and the cookie is what a returning student
        # actually carries.
        context_kw.setdefault("locale", "ar")
        context_kw.setdefault("extra_http_headers", {"Accept-Language": "ar"})
        context = self.browser.new_context(**context_kw)
        # Arabic, because the copy IS what most of these assert: the wording has to
        # offer a person without implying one has agreed to anything.
        context.add_cookies(
            [
                self._cookie(),
                {"name": "django_language", "value": "ar", "url": self.live_server_url},
            ]
        )
        self.addCleanup(context.close)
        return context.new_page()

    def _turn(
        self,
        *,
        disposition: str = FinalDisposition.ABSTAIN,
        status: str = AdvisorMessage.STATUS_ABSTAINED,
        reason_codes: list[str] | None = None,
    ) -> AdvisorMessage:
        conversation = AdvisorConversation.objects.create(student_id=MINE)
        asked = AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_STUDENT,
            content="هل أقدر أنسحب من مقرر؟",
            status=AdvisorMessage.STATUS_COMPLETED,
        )
        answer = AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_ASSISTANT,
            in_reply_to=asked,
            content="لا يستطيع النظام البت في حالتك الشخصية.",
            final_disposition=disposition,
            reason_codes=reason_codes
            if reason_codes is not None
            else [ReasonCode.PROHIBITED_FOR_DECISION],
            outcome_schema_version="1.0",
            status=status,
        )
        AdvisorMessageCitation.objects.create(
            message=answer,
            policy_id="TU.WITHDRAWAL.MAXIMUM",
            document_title="الدليل الإرشادي للطالب والطالبة",
            edition="1447",
            page="24",
            effective_from="1447",
            effective_to="",
            authority_status="AUTHORITY_APPROVED",
            validation_status=AdvisorMessageCitation.VALID,
            source_version_hash="h",
        )
        return answer

    def _open(self, page, conversation) -> None:
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}?c={conversation.id}")
        page.wait_for_selector(".va-message-assistant")

    # ── 1-2. when the action is offered, and how loudly ─────────
    def test_a_satisfactory_answer_still_offers_a_person_quietly(self):
        answer = self._turn(
            disposition=FinalDisposition.PASS,
            status=AdvisorMessage.STATUS_COMPLETED,
            reason_codes=[],
        )
        page = self._page()
        self._open(page, answer.conversation)

        assert page.locator(".sa-escalate-btn").count() == 1
        assert page.locator(".sa-escalate.is-prominent").count() == 0
        assert page.locator(".sa-escalate-lead").count() == 0

    def test_an_answer_that_stopped_short_says_so_prominently(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)

        assert page.locator(".sa-escalate.is-prominent").count() == 1
        lead = page.locator(".sa-escalate-lead").inner_text()
        assert "قد تحتاج" in lead
        # Nothing has been agreed at this point, and the copy must not suggest it.
        for premature in ("تمت الموافقة", "تم إسناد", "سيتم الرد خلال"):
            assert premature not in page.locator(".sa-escalate").inner_text()

    # ── 6-7. the preview shows the boundary, not the contents ───
    def test_the_preview_lists_what_travels_and_what_does_not(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.wait_for_selector(".sa-preview")

        preview = page.locator(".sa-preview").inner_text()
        for promised in (
            "سؤالك",
            "إجابة المرشد الافتراضي",
            "حالة الإجابة وسبب الإحالة",
            "المصادر التي ظهرت مع الإجابة",
            "المعلومات الناقصة المسجلة",
        ):
            assert promised in preview, promised
        assert "لن يتم إرسال المحادثات الأخرى" in preview
        assert page.locator(".sa-preview-note").count() == 1

    def test_the_page_never_renders_the_stored_evidence(self):
        """The snapshot is the adviser's copy in machine shape.

        The student has all of it already, as a conversation. Rendering the stored
        structure back into the page would put field names and policy vocabulary in
        front of them for no gain, on the one screen built to keep it out.
        """
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.wait_for_selector(".sa-preview")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        body = page.locator("body").inner_text()
        for internal in (
            "evidence_snapshot",
            "assistant_answer",
            "final_disposition",
            "reason_codes",
            "PROHIBITED_FOR_DECISION",
            "relevant_student_facts",
        ):
            assert internal not in body, internal

    # ── the confirmation, and that it lasts ─────────────────────
    def test_a_created_case_shows_its_reference_and_status(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.fill(".sa-preview-note", "أرجو مراجعة حالتي.")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        case = AdvisorEscalation.objects.get()
        assert case.student_note == "أرجو مراجعة حالتي."
        assert re.fullmatch(r"ADV-\d{4}-\d{5}", case.reference), case.reference

        shown = page.locator(".sa-case").inner_text()
        assert case.reference in shown
        assert "جديدة" in shown, "the internal status name was shown instead of a label"
        # The primary key stays internal.
        assert str(case.id) not in page.content()

    def test_the_action_is_replaced_by_the_case_not_shown_beside_it(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        assert page.locator(".sa-escalate-btn").count() == 0
        assert page.locator(".sa-preview").count() == 0

    def test_the_case_survives_a_reload(self):
        """Which is when a student who has been waiting comes back to look."""
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")
        reference = page.locator(".sa-case-ref").inner_text()

        self._open(page, answer.conversation)
        page.wait_for_selector(".sa-case")
        assert page.locator(".sa-case-ref").inner_text() == reference
        assert page.locator(".sa-escalate-btn").count() == 0

    def test_the_advisers_reply_reaches_the_student_but_their_notes_do_not(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        AdvisorEscalation.objects.update(
            status=AdvisorEscalation.Status.RESOLVED,
            resolution_message="تمت الموافقة على الانسحاب. راجع عمادة القبول.",
            adviser_notes="الطالب متكرر الشكاوى؛ راجع سجله التأديبي.",
        )
        self._open(page, answer.conversation)
        page.wait_for_selector(".sa-case-reply")

        assert "تمت الموافقة على الانسحاب" in page.locator(".sa-case-reply").inner_text()
        assert "تمت المعالجة" in page.locator(".sa-case-status").inner_text()
        body = page.content()
        assert "متكرر الشكاوى" not in body
        assert "adviser_notes" not in body

    # ── 3, 5. duplicates and reopening ──────────────────────────
    def test_submitting_twice_does_not_open_a_second_case(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        # A second submission through the API, exactly as a double-tap would.
        client = Client()
        from core.services import student_otp

        client.force_login(student_otp.provision_student_user(MINE))
        again = client.post(
            reverse("advisor_escalation_create", args=[str(answer.id)]),
            data=json.dumps({"student_requested": True}),
            content_type="application/json",
        )
        assert again.status_code == 200
        assert AdvisorEscalation.objects.count() == 1

    def test_a_closed_case_lets_the_student_ask_again(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        AdvisorEscalation.objects.update(status=AdvisorEscalation.Status.CLOSED)
        self._open(page, answer.conversation)
        page.wait_for_selector(".sa-escalate-btn")
        assert page.locator(".sa-escalate-btn").count() == 1

    # ── 9-10. failure leaves the conversation alone ─────────────
    def test_a_failed_submission_changes_nothing_and_says_so(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)
        page.route("**/escalations/", lambda route: route.abort())

        page.click(".sa-escalate-btn")
        page.fill(".sa-preview-note", "ملاحظتي")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-preview-error:visible")

        assert AdvisorEscalation.objects.count() == 0
        answer.refresh_from_db()
        assert answer.final_disposition == FinalDisposition.ABSTAIN
        assert page.locator(".sa-case").count() == 0
        # The note they typed is still there, and they can try again.
        assert page.locator(".sa-preview-note").input_value() == "ملاحظتي"
        assert page.locator(".sa-preview-send").is_disabled() is False

    def test_a_rate_limited_submission_names_the_wait_and_creates_nothing(self):
        from core.services import rate_limit

        answer = self._turn()
        limit, _ = rate_limit.LIMITS[rate_limit.ESCALATION]
        for _ in range(limit):
            rate_limit.consume(rate_limit.ESCALATION, MINE)

        page = self._page()
        self._open(page, answer.conversation)
        page.click(".sa-escalate-btn")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-preview-error:visible")

        problem = page.locator(".sa-preview-error").inner_text()
        assert any(ch.isdigit() for ch in problem), problem
        assert AdvisorEscalation.objects.count() == 0

    # ── 11. the feedback chip and the button mean the same thing ──
    def test_saying_you_need_a_human_opens_the_same_preview(self):
        answer = self._turn()
        page = self._page()
        self._open(page, answer.conversation)

        page.locator(".sa-fb-btn").nth(1).click()  # "No"
        page.wait_for_selector(".sa-fb-reasons:visible")
        page.locator('.sa-fb-reason[data-code="needed_human_adviser"]').click()

        page.wait_for_selector(".sa-preview")
        assert "سؤالك" in page.locator(".sa-preview").inner_text()

    # ── 12. a phone ─────────────────────────────────────────────
    def test_the_action_and_the_case_card_fit_a_phone(self):
        answer = self._turn()
        page = self._page(viewport={"width": 375, "height": 812})
        self._open(page, answer.conversation)

        page.locator(".sa-escalate-btn").scroll_into_view_if_needed()
        box = page.locator(".sa-escalate-btn").bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 376, box

        page.click(".sa-escalate-btn")
        page.wait_for_selector(".sa-preview")
        page.click(".sa-preview-send")
        page.wait_for_selector(".sa-case")

        overflow = page.evaluate(
            """() => {
                const doc = document.documentElement;
                const main = document.querySelector('main') || doc;
                return Math.max(main.scrollWidth - main.clientWidth,
                                doc.scrollWidth - doc.clientWidth);
            }"""
        )
        assert overflow <= 1, f"the case card overflows by {overflow}px at 375px"
        card = page.locator(".sa-case").bounding_box()
        assert card and card["x"] >= 0 and card["x"] + card["width"] <= 376, card


@pytest.mark.django_db
def test_case_references_are_unique_and_sequential():
    """Counting rows and adding one is the obvious approach and it is wrong.

    Two students escalating at the same moment both read the same count and both
    claim the same number, and a case number that is not unique is not a
    reference.
    """
    from core.models import allocate_escalation_reference

    issued = [allocate_escalation_reference(year=2026) for _ in range(3)]
    assert issued == ["ADV-2026-00001", "ADV-2026-00002", "ADV-2026-00003"]
    assert allocate_escalation_reference(year=2027) == "ADV-2027-00001"


@pytest.mark.django_db
def test_the_counter_row_is_claimed_under_a_lock(monkeypatch):
    """Asserts the MECHANISM, because the effect is invisible here.

    `select_for_update` is a no-op on SQLite, which is the only backend these
    tests run on, so no behavioural assertion can tell a locked read from an
    unlocked one. Without it two students escalating at the same moment both read
    the same counter and both claim the same case number — and a reference that is
    not unique is not a reference.
    """
    from django.db import transaction
    from django.db.models.query import QuerySet

    from core.models import allocate_escalation_reference

    events: list[str] = []
    locked: list[bool] = []
    real_atomic = transaction.atomic
    real_get_or_create = QuerySet.get_or_create

    def spy_atomic(*args, **kwargs):
        events.append("atomic")
        return real_atomic(*args, **kwargs)

    def spy_read(self, *args, **kwargs):
        # The queryset that ACTUALLY reads the counter — spying on
        # `select_for_update` itself would pass for code that locks a throwaway
        # queryset and then reads an unlocked one.
        events.append("read")
        locked.append(bool(self.query.select_for_update))
        return real_get_or_create(self, *args, **kwargs)

    monkeypatch.setattr(transaction, "atomic", spy_atomic)
    monkeypatch.setattr(QuerySet, "get_or_create", spy_read)
    allocate_escalation_reference(year=2026)

    assert locked == [True], "the counter was read without a lock"
    assert "atomic" in events, "no transaction was opened, so the lock holds nothing"
    assert events.index("atomic") < events.index("read"), events


@pytest.mark.django_db
def test_a_reference_never_changes_once_issued():
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    asked = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_STUDENT, content="q"
    )
    answer = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content="a",
    )
    case = AdvisorEscalation.objects.create(
        conversation=conversation,
        source_message=answer,
        student_id=MINE,
        reason_code=ReasonCode.STUDENT_REQUESTED,
        evidence_snapshot={},
    )
    original = case.reference
    case.status = AdvisorEscalation.Status.ASSIGNED
    case.save()
    case.refresh_from_db()
    assert case.reference == original


@pytest.mark.django_db
def test_stale_rate_limit_rows_do_not_outlive_their_window():
    """Unrelated to escalation, but the same table: keeps the housekeeping honest."""
    from django.utils import timezone

    from core.services import rate_limit

    rate_limit.consume(rate_limit.ESCALATION, MINE)
    RateLimitBucket.objects.update(window_start=timezone.now() - timedelta(days=30))
    assert rate_limit.purge_expired() == 1


@pytest.mark.django_db
def test_the_status_labels_cover_every_state():
    """A missing label falls back to the internal name, which is not Arabic and
    not an explanation."""
    from core.advisor_conversation_views import STATUS_LABELS_AR

    for value, _display in AdvisorEscalation.Status.choices:
        assert STATUS_LABELS_AR.get(value), value
        assert not STATUS_LABELS_AR[value].isascii(), value


@pytest.mark.django_db
def test_a_pending_answer_offers_no_case_at_all():
    """A turn still being generated has nothing to escalate yet."""
    from core.services.advisor_escalation import may_escalate

    message = AdvisorMessage(final_disposition="", reason_codes=[])
    assert may_escalate(message) is False
    assert may_escalate(message, student_requested=True) is True
