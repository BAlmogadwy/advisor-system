"""Two server rules behind the advisor portfolio.

1. "High risk" is ONE rule. The portfolio's High Risk tile counted
   ``risk_score >= 8`` while the chip beside it, labelled "At Risk", filtered
   ``GPA < 2.0`` — so "3 high risk" opened an empty list. The chip's ``risk``
   focus keeps its published meaning (low GPA; the virtual advisor documents
   it) and ``highrisk`` is the tile's rule, shared with the summary count.

2. A placeholder is not a name. An import stored advisor 40's missing name as
   the text "null", and the picker printed "40 — null (CS2,IS2)".
"""

from __future__ import annotations

import pytest

from core.models import AcademicAdvisor
from core.services.advisors import (
    HIGH_RISK_SCORE,
    _apply_advisor_filters,
    _summarize_advisor_items,
    advisor_display_name,
    list_academic_advisors,
)

ROWS = [
    {"student_id": 1, "name": "A", "risk_score": 9.0, "gpa": 3.2},  # high risk only
    {"student_id": 2, "name": "B", "risk_score": 2.0, "gpa": 1.4},  # low GPA only
    {"student_id": 3, "name": "C", "risk_score": HIGH_RISK_SCORE, "gpa": None},  # boundary
    {"student_id": 4, "name": "D", "risk_score": 7.99, "gpa": 2.5},  # neither
]


def _ids(rows):
    return [r["student_id"] for r in rows]


def test_highrisk_focus_is_the_risk_score_rule_the_summary_counts():
    picked = _apply_advisor_filters(ROWS, focus="highrisk")
    assert _ids(picked) == [1, 3]
    assert _summarize_advisor_items(ROWS)["very_high_risk_count"] == len(picked)


def test_risk_focus_keeps_its_published_low_gpa_meaning():
    assert _ids(_apply_advisor_filters(ROWS, focus="risk")) == [2]


@pytest.mark.parametrize("raw", [None, "", "  ", "null", "NULL", " None ", "nan", "N/A"])
def test_placeholders_are_not_names(raw):
    assert advisor_display_name(raw) == ""


def test_a_real_name_is_kept_as_written():
    assert advisor_display_name("  أحمد رضا  ") == "أحمد رضا"
    assert advisor_display_name("Nullah Smith") == "Nullah Smith"


@pytest.mark.django_db
def test_the_picker_list_never_carries_the_text_null():
    AcademicAdvisor.objects.create(
        advisor_id="40", full_name="null", email="a40@example.edu", department="CS2"
    )
    AcademicAdvisor.objects.create(
        advisor_id="41", full_name="Real Name", email="a41@example.edu", department="AI"
    )
    names = {a["advisor_id"]: a["full_name"] for a in list_academic_advisors()["items"]}
    assert names == {"40": "", "41": "Real Name"}
