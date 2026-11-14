"""The staff copy deck: every code the staff side shows has words, in both
languages, and the words keep the portal's rules.

Tied to the service's own codes, so a new refusal or event cannot reach a staff
page as a raw code, and to the sidebar, which writes its words inline.
"""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

from core.models import (
    TimetableFeedbackCloseReason,
    TimetableFeedbackEventKind,
    TimetableFeedbackReason,
    TimetableFeedbackStatus,
)
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_staff_copy as scopy

ROOT = Path(__file__).resolve().parents[1]
ARABIC_LETTER = re.compile("[" + chr(0x0621) + "-" + chr(0x064A) + "]")

TABLES = {
    "STRINGS": scopy.STRINGS,
    "STATUS": scopy.STATUS,
    "REASON": scopy.REASON,
    "CLOSE_REASON": scopy.CLOSE_REASON,
    "EVENT": scopy.EVENT,
    "NOTICE": scopy.NOTICE,
    "REFUSAL": scopy.REFUSAL,
    "TEMPLATE_NAME": scopy.TEMPLATE_NAME,
    "GUIDE_REASONS": scopy.GUIDE_REASONS,
}
ENTRIES = (
    [(table, key, pair) for table, values in TABLES.items() for key, pair in values.items()]
    + [("UNKNOWN", "UNKNOWN_REFUSAL", scopy.UNKNOWN_REFUSAL)]
    + [("GUIDE_STEPS", f"{n}", pair) for n, step in enumerate(scopy.GUIDE_STEPS) for pair in step]
    + [("GUIDE_RULES", f"{n}", rule) for n, rule in enumerate(scopy.GUIDE_RULES)]
)
#: Entries that are punctuation in both languages, not words.
_NOT_WORDS = {"change_arrow", "list_separator", "cohort_M", "cohort_F"}


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _raised_codes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "WorkflowRefused"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def test_every_refusal_a_staff_page_can_show_has_words():
    """The staff service's own refusals, and the workflow's that reach a staff
    action (the shared text checks and the claim)."""
    raised = _raised_codes(ROOT / "core/services/timetable_feedback_staff.py")
    raised |= _raised_codes(ROOT / "core/staff_timetable_feedback_views.py")
    assert len(raised) > 10, "the scan found too few refusals"
    shared = {"CASE_NOT_FOUND", "CASE_CHANGED", "NOT_ALLOWED"}
    assert raised | shared <= set(scopy.REFUSAL)
    # And nothing in the deck that nothing raises.
    assert set(scopy.REFUSAL) <= raised | shared


@pytest.mark.parametrize(
    ("table", "choices"),
    [
        (scopy.STATUS, TimetableFeedbackStatus),
        (scopy.REASON, TimetableFeedbackReason),
        (scopy.CLOSE_REASON, TimetableFeedbackCloseReason),
        (scopy.EVENT, TimetableFeedbackEventKind),
        (scopy.GUIDE_REASONS, TimetableFeedbackReason),
    ],
)
def test_every_model_code_has_words(table, choices):
    assert set(table) == set(choices.values)


def test_every_ready_made_answer_has_a_name_staff_choose_it_by():
    assert set(scopy.TEMPLATE_NAME) == set(copy.REPLY_TEMPLATES)


def test_every_notice_a_view_leaves_has_words():
    from core.staff_timetable_feedback_views import _ACTIONS

    left = {notice for _act, notice in _ACTIONS.values()} | {"REDACTED"}
    assert left == set(scopy.NOTICE)


def test_the_queue_has_a_tab_label_for_every_tab():
    for tab in staff_service.TABS:
        assert f"tab_{tab}" in scopy.STRINGS, tab


@pytest.mark.parametrize(("table", "key", "pair"), ENTRIES, ids=lambda v: str(v)[:40])
def test_each_entry_has_english_and_arabic_with_the_same_placeholders(table, key, pair):
    english, arabic = pair
    assert english.strip() and arabic.strip(), (table, key)
    if key not in _NOT_WORDS:
        assert not ARABIC_LETTER.search(english), (table, key)
        assert ARABIC_LETTER.search(arabic), (table, key)
    assert _placeholders(english) == _placeholders(arabic), (table, key)


#: Imperatives that would address only a man. Staff are asked with «يُرجى» and a
#: verbal noun, as students are.
GENDERED_IMPERATIVE = re.compile(
    r"(^|\s)(اختر|اكتب|أخبر|راجع|تحقّق|تحقق|ارجع|أرسل|أضف|اسحب|انسخ|احذف|أغلق|أحل|اسأل)(\s|$|[،.؛:])"
)


@pytest.mark.parametrize(("table", "key", "pair"), ENTRIES, ids=lambda v: str(v)[:40])
def test_no_arabic_entry_addresses_only_one_gender(table, key, pair):
    assert not GENDERED_IMPERATIVE.search(pair[1]), (table, key)


def test_a_comment_for_colleagues_is_never_called_the_students_word_for_feedback():
    """«ملاحظة» is the student's whole feedback; a staff comment is «تعليق»."""
    for key in ("action_note_title", "action_note_label", "bulk_close_note", "action_close_label"):
        assert "تعليق" in scopy.STRINGS[key][1], key
        assert "ملاحظة" not in scopy.STRINGS[key][1], key


def test_the_deck_uses_the_portal_glossary():
    arabic = " ".join(pair[1] for _, _, pair in ENTRIES)
    for term in ("معرّف المرشد", "الطالب", "البرنامج", "القسم", "الشعبة", "الجدول المتوقع"):
        assert term in arabic, term


def test_templates_get_only_strings_without_placeholders():
    for language in copy.LANGUAGES:
        strings = scopy.strings(language)
        assert all("{" not in value for value in strings.values())


def test_limits_in_refusals_are_the_services_own(monkeypatch):
    assert str(staff_service.STAFF_TEXT_MAX_CHARS) in scopy.refusal("BODY_TOO_LONG", "en")
    monkeypatch.setattr(staff_service, "STAFF_TEXT_MAX_CHARS", 1234)
    assert "1234" in scopy.refusal("BODY_TOO_LONG", "ar")
    monkeypatch.setattr(staff_service, "BULK_MAX", 17)
    assert "17" in scopy.refusal("BULK_TOO_MANY", "en")


def test_an_unknown_code_gets_an_apology_that_shows_the_code():
    html = scopy.refusal("SOMETHING_NEW", "ar")
    assert scopy.UNKNOWN_REFUSAL[1] in html and "SOMETHING_NEW" in html


def test_strings_without_placeholders_never_call_format_html_bare():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert scopy.refusal("ALREADY_YOURS", "en") == "You already handle this case."
        assert scopy.text("errors_title", "ar") == "لم يُجرَ أي تغيير"


def test_filled_strings_escape_what_they_are_given():
    html = scopy.text("case_title", "en", reference="<script>")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert scopy.plain("case_title", "ar", reference="TTF-2026-00007") == "الملاحظة TTF-2026-00007"


def test_the_sidebar_link_reads_like_the_deck():
    sidebar = (ROOT / "core/templates/core/partials/sidebar.html").read_text(encoding="utf-8")
    english, arabic = scopy.STRINGS["nav"]
    assert english in sidebar and arabic in sidebar
    for words in scopy.STRINGS["nav_count"]:
        before, after = words.split("{count}")
        assert f"{before}{{{{ timetable_feedback_staff_count }}}}{after}" in sidebar, words
