"""The copy deck: every code has words, in both languages, and the words keep the
portal's rules.

The deck is Python, so the template glossary test does not read it; these tests
apply the same rules here, and tie the deck to the services' own codes so a new
refusal cannot ship as a raw code in an Arabic page.
"""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

from core.models import (
    TimetableFeedbackAuthorKind,
    TimetableFeedbackCloseReason,
    TimetableFeedbackReason,
    TimetableFeedbackStatus,
)
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_workflow as workflow

ROOT = Path(__file__).resolve().parents[1]
ARABIC_LETTER = re.compile("[" + chr(0x0621) + "-" + chr(0x064A) + "]")

TABLES = {
    "STRINGS": copy.STRINGS,
    "STATUS": copy.STATUS,
    "STATUS_EXPLAINED": copy.STATUS_EXPLAINED,
    "REASON": copy.REASON,
    "CLOSE_REASON": copy.CLOSE_REASON,
    "AUTHOR": copy.AUTHOR,
    "DAY": copy.DAY,
    "NOTICE": copy.NOTICE,
    "REFUSAL": copy.REFUSAL,
}
ENTRIES = [
    (table, key, pair) for table, values in TABLES.items() for key, pair in values.items()
] + [("UNKNOWN", "UNKNOWN_REFUSAL", copy.UNKNOWN_REFUSAL)]


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _raised_codes(path: Path, exception: str) -> set[str]:
    """Every literal first argument of ``exception(...)`` in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    codes = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == exception
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            codes.add(node.args[0].value)
    return codes


# ---------------------------------------------------------------------------
# Every code has words
# ---------------------------------------------------------------------------


def test_every_refusal_the_services_can_give_has_words_and_no_word_is_stale():
    raised = _raised_codes(
        ROOT / "core/services/timetable_feedback.py", "FeedbackRefused"
    ) | _raised_codes(ROOT / "core/services/timetable_feedback_workflow.py", "WorkflowRefused")
    assert raised, "the scan found no refusals at all"
    assert raised == set(copy.REFUSAL)


@pytest.mark.parametrize(
    ("table", "choices"),
    [
        (copy.STATUS, TimetableFeedbackStatus),
        (copy.STATUS_EXPLAINED, TimetableFeedbackStatus),
        (copy.REASON, TimetableFeedbackReason),
        (copy.CLOSE_REASON, TimetableFeedbackCloseReason),
        (copy.AUTHOR, TimetableFeedbackAuthorKind),
    ],
)
def test_every_model_code_has_words(table, choices):
    assert set(table) == set(choices.values)


def test_the_form_offers_exactly_the_issues_the_service_allows():
    from core.student_timetable_feedback_views import REASON_ORDER

    for kind, order in REASON_ORDER.items():
        assert set(order) == set(feedback.REASONS_BY_ANCHOR[kind]), kind
        assert len(order) == len(set(order)), kind


# ---------------------------------------------------------------------------
# Both languages, the same placeholders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("table", "key", "pair"), ENTRIES, ids=lambda v: str(v)[:40])
def test_each_entry_has_english_and_arabic_with_the_same_placeholders(table, key, pair):
    english, arabic = pair
    assert english.strip() and arabic.strip(), (table, key)
    assert not ARABIC_LETTER.search(english), (table, key)
    if key != "list_separator":
        assert ARABIC_LETTER.search(arabic), (table, key)
    assert _placeholders(english) == _placeholders(arabic), (table, key)


def test_templates_get_only_strings_without_placeholders():
    for language in copy.LANGUAGES:
        strings = copy.strings(language)
        assert all("{" not in value for value in strings.values())
        assert set(strings) == {key for key, pair in copy.STRINGS.items() if "{" not in pair[0]}


def test_filled_strings_escape_what_they_are_given():
    html = copy.text("detail_title", "en", reference="<script>")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert copy.bdi("<b>") == '<bdi dir="ltr">&lt;b&gt;</bdi>'
    assert copy.term_html(1448, 1) == '<bdi dir="ltr">1448/1</bdi>'


@pytest.mark.parametrize(
    ("code", "language"),
    [
        ("ar", "ar"),
        ("ar-sa", "ar"),
        ("AR", "ar"),
        ("en", "en"),
        ("", "en"),
        (None, "en"),
        ("fr", "en"),
    ],
)
def test_any_arabic_code_reads_as_arabic(code, language):
    assert copy.language_of(code) == language


# ---------------------------------------------------------------------------
# The portal's wording rules
# ---------------------------------------------------------------------------

#: Phrases the student pages must never use (tests/test_student_arabic_language_contract.py
#: and the home page's wording guards), checked here because the deck is Python.
BANNED = (
    "تسجيلك الحقيقي",
    "جدولك الحالي",
    "شُعبي الحالية",
    "خيارات الجدول",
    "مخطط الجدول",
    "طريقة بناء الجدول",
    "أي شعبة مناسبة",
    "متاحة للتسجيل",
    "تستطيع تسجيل",
    "يمكنك التسجيل",
    "إنذار",
    "تحذير",
    "خطر",
    "خطورة",
    "متعثر",
    "الفصل من الجامعة",
    "تحتاج إلى انتباه",
    "Available to register",
    "needs attention",
)

#: Imperatives that would address only a male student. The deck asks with «يُرجى»
#: and a verbal noun instead, so every student reads a correct sentence.
GENDERED_IMPERATIVE = re.compile(
    r"(^|\s)(اختر|اكتب|أخبر|راجع|تحقّق|تحقق|ارجع|أرسل|أضف|اسحب)(\s|$|[،.؛:])"
)


@pytest.mark.parametrize(("table", "key", "pair"), ENTRIES, ids=lambda v: str(v)[:40])
def test_no_entry_uses_a_banned_phrase(table, key, pair):
    for text in pair:
        for phrase in BANNED:
            assert phrase not in text, (table, key, phrase)


@pytest.mark.parametrize(("table", "key", "pair"), ENTRIES, ids=lambda v: str(v)[:40])
def test_no_arabic_entry_addresses_only_one_gender(table, key, pair):
    assert not GENDERED_IMPERATIVE.search(pair[1]), (table, key)


def test_the_deck_uses_the_portal_glossary():
    arabic = " ".join(pair[1] for _, _, pair in ENTRIES)
    for term in ("الجدول المتوقع", "الجدول المسجّل فعليًا", "مرشدك الأكاديمي", "الشعبة"):
        assert term in arabic, term
    # The human advisor, never the AI planning adviser.
    assert "مرشد التخطيط الأكاديمي" not in arabic


def test_the_sidebar_link_reads_like_the_deck():
    sidebar = (ROOT / "core/templates/core/partials/sidebar.html").read_text(encoding="utf-8")
    english, arabic = copy.STRINGS["nav"]
    assert english in sidebar and arabic in sidebar


# ---------------------------------------------------------------------------
# Numbers come from the services
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "english", "arabic"),
    [
        (0, "1 second", "ثانية واحدة"),
        (1, "1 second", "ثانية واحدة"),
        (2, "2 seconds", "ثانيتين"),
        (30, "30 seconds", "30 ثانية"),
        (89, "89 seconds", "89 ثانية"),
        (90, "2 minutes", "دقيقتين"),
        (300, "5 minutes", "5 دقائق"),
        (660, "11 minutes", "11 دقيقة"),
        (5399, "90 minutes", "90 دقيقة"),
        (5400, "2 hours", "ساعتين"),
        (86400, "24 hours", "24 ساعة"),
    ],
)
def test_a_wait_reads_in_the_largest_natural_unit_rounded_up(seconds, english, arabic):
    assert copy.wait_text(seconds, "en") == english
    assert copy.wait_text(seconds, "ar") == arabic


def test_limits_in_refusals_are_the_services_own(monkeypatch, settings):
    assert str(feedback.NOTE_MAX_CHARS) in copy.refusal("NOTE_TOO_LONG", "en")
    monkeypatch.setattr(feedback, "NOTE_MAX_CHARS", 777)
    assert "777" in copy.refusal("NOTE_TOO_LONG", "ar")
    monkeypatch.setattr(workflow, "MAX_FOLLOW_UPS", 9)
    assert "9" in copy.refusal("TOO_MANY_FOLLOW_UPS", "en")
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 14
    assert "14 days" in copy.refusal("REOPEN_WINDOW_PASSED", "en")
    assert "14 يومًا" in copy.refusal("REOPEN_WINDOW_PASSED", "ar")
    settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS = 2
    assert "يومين" in copy.refusal("REOPEN_WINDOW_PASSED", "ar")
    assert "24 hours" in copy.refusal("COOLING_DOWN", "en")
    assert "24 ساعة" in copy.refusal("COOLING_DOWN", "ar")


def test_a_refusal_names_the_wait_and_the_reference_it_is_given():
    assert "5 minutes" in copy.refusal("RATE_LIMITED", "en", retry_after=300)
    html = copy.refusal("ALREADY_RAISED", "ar", reference="TTF-2026-00012")
    assert '<bdi dir="ltr">TTF-2026-00012</bdi>' in html


def test_an_unknown_code_gets_an_apology_that_shows_the_code():
    html = copy.refusal("SOMETHING_NEW", "ar")
    assert copy.UNKNOWN_REFUSAL[1] in html and "SOMETHING_NEW" in html


def test_an_unknown_author_kind_reads_as_the_department_never_a_raw_code():
    assert copy.author_label("SOMEONE", "en") == "The department"
    assert copy.author_label("ADVISOR", "ar") == "مرشدك الأكاديمي"


def test_strings_without_placeholders_never_call_format_html_bare():
    """format_html() with nothing to format is deprecated, and an error in Django 6:
    every plain refusal and required-field message would become a server error."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert copy.text("new_reason_required", "en") == "Please choose the issue."
        assert "<" not in copy.refusal("NOT_OFFERED", "ar")
        assert copy.text("new_errors_title", "en") == "Your feedback was not sent"


def test_plain_strings_carry_no_markup_for_places_markup_cannot_go():
    assert copy.plain("detail_title", "en", reference="TTF-2026-00007") == (
        "Feedback TTF-2026-00007"
    )
    assert copy.plain("detail_title", "ar", reference="TTF-2026-00007") == (
        "الملاحظة TTF-2026-00007"
    )
