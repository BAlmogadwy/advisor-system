"""What the student's own screen may say, and what stays on the adviser's.

The boundary this file defends:

    student screen      verified facts about this student
                        transparent derivations from their own record
                        cited policy that needs no missing input

    adviser portfolio   prioritisation heuristics
                        comparative risk and queue-ordering
                        internal attention classifications
"""

from __future__ import annotations

import pytest

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.rbac import ensure_role_groups
from core.services.student_home_cards import (
    GPA_SCALE,
    arabic_count,
    gpa_band,
    progress_buckets,
    unlock_leaders,
)
from core.services.student_unlock import build_unlock_report

pytestmark = pytest.mark.django_db

SID = 4990001
PROG = "HCD"
YEAR, TERM = 1448, 1


@pytest.fixture
def plan():
    """A -> B, A -> C, and D which needs BOTH A and E. So passing A frees B and C
    but NOT D, which is the distinction the unlock card has to get right."""
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={"name": "Cards", "program": PROG, "section": "M", "gpa": 4.01},
    )
    for code in ("HA101", "HB201", "HC201", "HD301", "HE101"):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": code, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": 1, "credit_hours": 3, "type": "Mandatory"},
        )
    for course, prereq in (
        ("HB201", "HA101"),
        ("HC201", "HA101"),
        ("HD301", "HA101"),
        ("HD301", "HE101"),
    ):
        Prerequisite.objects.update_or_create(
            program=PROG, course_code=course, prerequisite_course_code=prereq
        )
    yield


def _report():
    return build_unlock_report(SID, YEAR, TERM)


# ── the buckets are disjoint and exhaustive ──────────────────────


def test_the_four_states_do_not_double_count(plan):
    """`counts.one_step` is a SUBSET of `counts.locked`.

    The screen showed them as four peer tiles, so a reader summing them got more
    courses than the plan holds. Measured for student 4502156: 6 + 6 + 8 + 30 = 50
    against a plan of 44 courses and 6 placeholders.
    """
    report = _report()
    counts = report["counts"]
    buckets = progress_buckets(report)

    assert buckets["one_step"] + buckets["blocked_deeper"] == counts["locked"], (
        "the two blocked tiles do not reconstitute the blocked total"
    )
    plan_total = (
        buckets["open"]
        + buckets["one_step"]
        + buckets["blocked_deeper"]
        + buckets["passed"]
        + buckets["studying"]
    )
    listed = sum(
        len(report.get(k) or []) for k in ("open_courses", "locked_courses", "done", "in_progress")
    )
    assert plan_total == listed, (plan_total, listed)


def test_placeholders_are_counted_apart_from_the_four_states(plan):
    """A slot is not a course in any state — it is a choice not yet made. Folding
    it in is what made the totals irreconcilable."""
    report = _report()
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="HP1",
        defaults={"programme_term": 7, "credit_hours": 3, "type": "Program Elective"},
    )
    buckets = progress_buckets(_report())
    assert buckets["elective_slots"] == 1
    before = progress_buckets(report)
    for key in ("open", "one_step", "blocked_deeper", "passed"):
        assert buckets[key] == before[key], f"the placeholder leaked into {key}"


# ── Arabic count agreement ───────────────────────────────────────


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "لا توجد مقررات"),
        (1, "مقرر"),
        (2, "مقرران"),
        (3, "3 مقررات"),
        (6, "6 مقررات"),
        (10, "10 مقررات"),
        (11, "11 مقررًا"),
        (22, "22 مقررًا"),
        (99, "99 مقررًا"),
        (100, "100 مقرر"),
    ],
)
def test_the_count_agrees_with_the_noun(n, expected):
    """`f"{n} مقرر"` is right for exactly one value. The screen used it for all of
    them, and «6 مقرر» is what a real student saw.

    The two a naive formatter always gets wrong are the dual and the 11-99
    accusative — and 11-99 is the range a whole plan sits in.
    """
    assert arabic_count(n) == expected


# ── GPA: the band, and the honest absence ────────────────────────


def test_the_band_comes_from_the_store_not_a_copy(plan):
    card = gpa_band(4.01)
    assert card["band_ar"] == "جيد جداً"
    assert card["citation"]["policy_id"] == "TU.GPA.GENERAL_ESTIMATE_BANDS"
    assert card["citation"]["page"] == 28
    assert not card["note_ar"]


@pytest.mark.parametrize(
    ("gpa", "band"),
    [(5.0, "ممتاز"), (4.5, "ممتاز"), (4.49, "جيد جداً"), (2.75, "جيد"), (2.0, "مقبول")],
)
def test_the_band_boundaries_follow_the_approved_table(gpa, band):
    """`max_inclusive` decides 4.5 — ممتاز, not جيد جداً. Copying the numbers into
    the template would have lost that flag."""
    assert gpa_band(gpa)["band_ar"] == band


def test_the_bands_are_contiguous_so_max_inclusive_stays_inert():
    """Why a `max_inclusive` mutant survives, pinned as a property.

    Dropping the flag changes no answer anywhere in [0, 5] — measured at 0.01
    steps, zero disagreements. The published table is contiguous and descending,
    so a boundary value is always claimed by the higher band's `min` before the
    lower band's `max` is consulted. That makes the flag genuinely inert, and the
    mutant genuinely EQUIVALENT rather than merely untested.

    This asserts the property the equivalence rests on. The day an edition ships a
    gap or an overlap, `max_inclusive` becomes load-bearing and this fails — which
    is the warning that the flag now matters.
    """
    from core.services.policy_store import get_policy_store
    from core.services.student_home_cards import _BANDS_POLICY, GPA_SCALE

    record = next(r for r in get_policy_store().records if r.get("policy_id") == _BANDS_POLICY)
    windows = [b[f"of_{GPA_SCALE}"] for b in record["bands"]]
    for higher, lower in zip(windows, windows[1:], strict=False):
        assert lower["max"] == higher["min"], (
            f"the table is no longer contiguous: {lower['max']} != {higher['min']} — "
            "max_inclusive is now load-bearing and needs its own boundary tests"
        )
    assert windows[0]["max_inclusive"] is True, "the top band must include its ceiling"


def test_a_gpa_below_the_table_reports_the_tables_silence_not_a_verdict(plan):
    """Seven students sit below 2.00 and the approved table has no row for them.

    The sentence is about the TABLE. It must not become a statement about the
    student, and above all not a warning: `TU.DISMISSAL.THREE_WARNINGS` carries
    `never_infer` — "Do not treat gpa < 2.0 as 'on warning'" — and the count it
    needs does not exist in the schema.
    """
    card = gpa_band(1.62)
    assert card["band_ar"] == ""
    assert card["note_ar"] == "لا ينطبق تقدير عام على هذا المعدل في الجدول المعتمد."
    for forbidden in ("إنذار", "فصل", "خطر", "تحذير"):
        assert forbidden not in card["note_ar"], forbidden


def test_a_missing_gpa_says_so(plan):
    assert gpa_band(None)["gpa"] is None
    assert gpa_band(None)["note_ar"]


def test_no_stored_gpa_exceeds_the_declared_scale():
    """`GPA_SCALE = 5` is the one inference on this surface — the column carries no
    scale, and 5.00 appearing in the data is what settles it. Guarded, so the day
    an import changes the convention this fails instead of mislabelling a band."""
    Student.objects.update_or_create(
        student_id=SID, defaults={"name": "x", "program": PROG, "section": "M", "gpa": 4.9}
    )
    worst = max(g for g in Student.objects.exclude(gpa=None).values_list("gpa", flat=True))
    assert worst <= GPA_SCALE, f"a GPA of {worst} cannot be on a {GPA_SCALE}-point scale"


# ── which courses actually open doors ────────────────────────────


def test_a_course_counts_only_the_courses_it_alone_still_blocks(plan):
    """HD301 needs HA101 AND HE101. Passing HA101 does not free it.

    Counting every dependent — which the raw edge list gives, and which is the
    obvious implementation — would promise a student something passing one course
    cannot deliver. HA101 has three dependents and frees two.
    """
    leaders = unlock_leaders(_report())
    top = next(x for x in leaders if x["course_code"] == "HA101")
    assert top["frees"] == 2, top
    assert top["frees_codes"] == ["HB201", "HC201"]
    assert "HD301" not in top["frees_codes"], "promised a course that stays blocked"


def test_passing_the_other_prerequisite_changes_the_answer(plan):
    """The count is about THIS student's remaining prerequisites, not the
    catalogue's. Once HE101 is passed, HA101 does free HD301."""
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="HE101"),
        defaults={"status": "passed", "programme_term": 1},
    )
    top = next(x for x in unlock_leaders(_report()) if x["course_code"] == "HA101")
    assert top["frees"] == 3
    assert "HD301" in top["frees_codes"]


def test_a_course_that_frees_nothing_is_not_listed(plan):
    assert all(x["frees"] > 0 for x in unlock_leaders(_report()))


# ── the boundary ─────────────────────────────────────────────────


def test_no_adviser_triage_signal_can_reach_this_module(plan):
    """`risk_score` and `needs_attention` are queue-ordering devices built from
    `max(0, 2 - gpa) * 5 + sum(missing scores) + 3 if zero hours`. No scale, no
    published threshold, no source. In front of the person they describe, that is
    a verdict citing nothing."""
    import ast
    import pathlib

    # PARSED, not grepped. The module docstring EXPLAINS why these are excluded,
    # and a substring search flags the explanation — my first version of this test
    # did exactly that, and would have forced the reasoning out of the file to go
    # green. What matters is whether any code REFERENCES them.
    tree = ast.parse(
        pathlib.Path("core/services/student_home_cards.py").read_text(encoding="utf-8")
    )
    banned = {"risk_score", "needs_attention", "attention_reasons"}
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string key is a reference too: `report["risk_score"]` reads it.
            if node.value in banned:
                referenced.add(node.value)

    leaked = referenced & banned
    assert not leaked, f"{sorted(leaked)} crossed into the student surface"

    report = _report()
    for payload in (progress_buckets(report), gpa_band(4.01)):
        assert not ({"risk_score", "needs_attention"} & set(payload)), payload
