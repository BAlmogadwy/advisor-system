"""The Add courses page reads only what the server sends, and words every refusal.

A renamed report or list field would read as ``undefined`` on the page - a
count shown as 0, a reason as the generic one - with every test green. These
compare the page script with a real report and a real list from the server.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.core.cache import cache
from django.urls import reverse

from core import models
from core.services.exam_add_courses import REFUSAL_CODES
from core.services.exam_timetable import build_enrolled_sets_with_meta
from tests import exam_linked_parity_corpus as corpus

PAGE = Path(__file__).resolve().parent.parent / "static" / "js" / "page-exam-timetable.js"
SCOPE = {"programs": ["AI", "CS"], "sections": ["F", "M"]}


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _block(text: str, start: str) -> str:
    begin = text.index(start)
    return text[begin : text.index("\n}\n", begin)]


def test_the_page_words_every_refusal_the_server_can_give():
    text = _page()
    block = text[text.index("const ADD_REFUSALS = {") :]
    block = block[: block.index("\n};")]
    assert set(re.findall(r"^  (add_courses_\w+):", block, flags=re.MULTILINE)) == set(
        REFUSAL_CODES
    )


@pytest.fixture
def served(client, django_user_model, monkeypatch):
    """A real report and a real list, from the views."""
    monkeypatch.setattr("core.authz._rate_buckets", {})
    cache.clear()
    client.force_login(django_user_model.objects.create_superuser(username="contract-admin"))
    corpus.create_population(models)
    _, metadata = build_enrolled_sets_with_meta(**SCOPE)
    codes = sorted(metadata)
    # One of the two study-plan names of LX200 in, the other out: the list
    # then says the one out shares its registrar code with the one in.
    part = [*codes[:3], "LX200 (1)"]
    rest = [code for code in codes if code not in part]
    built = client.post(
        reverse("exam_timetable_build"),
        {
            "label": "Contract",
            "days": corpus.POPULATION_DAYS[:2],
            "periods": corpus.POPULATION_PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "assign_rooms": True,
            "selected_courses": part,
            "selected_course_entries": [{"course_code": c, **metadata[c]} for c in part],
            "pinned": [],
        },
        content_type="application/json",
    ).json()
    listed = client.get(reverse("exam_timetable_scope_courses", args=[built["run_id"]])).json()
    added = client.post(
        reverse("exam_timetable_build"),
        {
            "label": "Contract + rest",
            "days": corpus.POPULATION_DAYS[:2],
            "periods": corpus.POPULATION_PERIODS,
            "max_per_day": 2,
            **SCOPE,
            "mode": "add_courses",
            "previous_run_id": built["run_id"],
            "base_schedule": built["schedule"],
            "pinned": [],
            "linked_exams": [],
            "added_courses": [{"course_identity": metadata[c]["course_identity"]} for c in rest],
        },
        content_type="application/json",
    ).json()
    assert added["ok"] is True, added
    return listed, added["add_courses"]


@pytest.mark.django_db
def test_every_report_field_the_page_reads_is_one_the_server_sends(served):
    _listed, report = served
    text = _page()
    describe = _block(text, "function describeAddCourses(report)")
    read = set(re.findall(r"\breport\?\.(\w+)", describe))
    # Sent only when the timetable has links, as Fix sends it.
    views = (PAGE.parents[2] / "core" / "exam_views.py").read_text(encoding="utf-8")
    assert 'report["linked_clash_students"]' in views
    assert read - set(report) - {"linked_clash_students"} == set(), read - set(report)
    # Each reason sentence reads the blocker kinds explain_unplaced sends.
    reasons = text[text.index("  reason: row => {") :]
    reasons = reasons[: reasons.index("\n  },\n")]
    assert report["not_placed"], "the population leaves courses out on two days"
    row = report["not_placed"][0]
    assert set(re.findall(r"\brow\?\.(\w+)", reasons)) <= set(row)
    assert set(re.findall(r"\bblocked\.(\w+)", reasons)) <= set(row["blocked_by"])
    assert {"course_code", "placed"} <= set(report["added"][0])


@pytest.mark.django_db
def test_every_list_field_the_page_reads_is_one_the_server_sends(served):
    listed, _report = served
    text = _page()
    render = _block(text, "function renderAddCourses()") + _block(
        text, "async function ensureOutsideCourses()"
    )
    course_keys = set().union(*(set(course) for course in listed["courses"]))
    read = set(re.findall(r"\bcourse\.(\w+)", render)) - {"missing"}
    assert read - course_keys == set(), read - course_keys
    assert set(re.findall(r"\bdata\.(\w+)", render)) <= set(listed)
