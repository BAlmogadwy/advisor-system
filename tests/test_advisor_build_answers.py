"""The build-a-timetable answer must be POSSIBLE when the build is correct.

Three consecutive live Telegram turns on 2026-08-22 abstained on «ابني لي
جدول جديد» with healthy mechanics (no provider error, no budget exhaustion)
— the checker itself made every truthful answer unacceptable, three ways at
once:

1. `_tool_contract_complete` demanded the union of every course code across
   EVERY alternative, and returned False outright for a zero-alternative
   build — which is the everyday result of `around_current` on a full
   registration, because the executor deliberately clears the fake
   baseline-echo alternative.  Abstention was the only reachable outcome.
2. The schedule miner read only `alternatives`, so the payload's OWN
   `baseline_sections` were invisible and every clock claim about the
   student's existing week was "unsupported".
3. `_section_labels_in_timetable` stringified the baseline's dict rows into
   garbage labels instead of walking them, so quoting the student's own
   section label read as an invention.

These tests run the REAL executor (the same fixture as the hard-constraint
suite) and hand its actual payload to the real checker — the class of defect
here was a checker written to an imagined payload shape, so a hand-built
payload would prove nothing about the seam.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models import (
    Course,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.services.answer_consistency import (
    REQUESTED_EVIDENCE_OMITTED,
    check_answer,
)
from core.services.rbac import ROLE_STUDENT
from core.services.virtual_advisor_capabilities import get_default_registry

pytestmark = pytest.mark.django_db

SID = 4987202

_CATALOGUE = frozenset({"REQ101", "OPT101"})


@pytest.fixture
def full_baseline_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """A student whose registered baseline already covers their term plan.

    `around_current` with nothing to add is the production shape that always
    abstained: the executor returns ok:True, zero alternatives, and the whole
    truth lives in `baseline_sections`.
    """
    Student.objects.create(
        student_id=SID,
        name="Build answer student",
        program="AI",
        section="M",
        status="active",
    )
    for code in sorted(_CATALOGUE):
        Course.objects.create(course_code=code, description=f"{code} test course", credit_hours=3)
        ProgrammeRequirement.objects.create(
            program="AI",
            course_code=code,
            course_name=f"{code} test course",
            type="Mandatory",
            programme_term=1,
            credit_hours=3,
        )
    row = TermSection.objects.create(
        scenario=None,
        course_code="REQ101",
        course_number="",
        course_key="REQ101",
        course_name="REQ101 test course",
        section="M2",
        available_capacity=30,
        registered_count=0,
    )
    TermSectionProgram.objects.create(term_section=row, program="AI")
    TermSectionMeeting.objects.create(
        term_section=row, day="MON", start_time="09:00", end_time="10:00"
    )
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=row,
        source="registration_plan_import",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses",
        lambda *_args, **_kwargs: [],
    )


def _build(args: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_default_registry().execute(
        "build_timetable_proposal",
        {"mode": "around_current", **(args or {})},
        scope={"role": ROLE_STUDENT, "student_id": SID},
        ctx={"academic_year": 1448, "term": 1},
    )


def _check(answer: str, payload: dict[str, Any]) -> list[str]:
    return check_answer(
        answer,
        tool_results=[payload],
        question="ابني لي جدول جديد",
        required_tools={"build_timetable_proposal"},
        known_course_codes=_CATALOGUE,
    )


def test_the_truthful_nothing_to_add_answer_survives_the_checker(
    full_baseline_world: None,
) -> None:
    """The exact production shape: full baseline, zero alternatives, and an
    answer that says so, quotes the student's own week, and offers the
    from-scratch follow-up."""
    payload = _build()
    assert payload["ok"] is True
    assert payload["alternatives"] == []
    assert payload["baseline_sections"], "the fixture must produce a baseline"

    answer = (
        "جدولك الحالي يغطي المطلوب، ولا توجد مقررات إضافية لإضافتها هذا الفصل. "
        "أنت مسجّل في REQ101 شعبة M2 يوم الاثنين من 09:00 إلى 10:00. "
        "إن أردت بناء جدول جديد بالكامل بدل جدولك الحالي فأخبرني لأبنيه من الصفر."
    )
    assert _check(answer, payload) == []


def test_the_same_payload_still_flags_an_invented_meeting(
    full_baseline_world: None,
) -> None:
    """Two-sided: opening the truthful path must not open the invented one."""
    payload = _build()
    answer = (
        "لا توجد مقررات إضافية لإضافتها. أنت مسجّل في OPT101 شعبة M9 يوم الأحد من 11:00 إلى 12:00."
    )
    assert _check(answer, payload) != []


def test_a_wrong_day_about_the_baseline_is_refuted_by_the_payloads_own_baseline(
    full_baseline_world: None,
) -> None:
    """The miner's baseline rows are what make this contradiction VISIBLE:
    without them `_schedule_relation_mismatch` short-circuits on an empty
    evidence set and a wrong day about the student's own registration passes
    silently.  REQ101 M2 meets Monday; the answer says Sunday."""
    payload = _build()
    answer = (
        "لا توجد مقررات إضافية لإضافتها. أنت مسجّل في REQ101 شعبة M2 يوم الأحد من 09:00 إلى 10:00."
    )
    assert "unsupported_academic_fact" in _check(answer, payload)


def test_a_build_answer_that_shows_nothing_is_still_refused(
    full_baseline_world: None,
) -> None:
    """The empty-evidence escape needs the answer to actually SAY the build
    added nothing — a contentless deflection stays an omission."""
    payload = _build()
    answer = "تم تنفيذ طلبك بنجاح، يمكنك مراجعة البوابة."
    assert REQUESTED_EVIDENCE_OMITTED in _check(answer, payload)


def test_presenting_one_alternative_whole_satisfies_the_contract() -> None:
    """The natural answer presents the BEST alternative in full — its placed
    courses and the one it could not place.  The first contract demanded the
    union across every alternative, which no natural answer satisfies."""
    payload = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "baseline_kind": "REGISTERED",
        "baseline_sections": [],
        "constraint_failures": [],
        "unplaced": [],
        "alternatives": [
            {
                "option": 1,
                "planner_options": [],
                "courses": [
                    {"course_code": "REQ101", "section": "M1"},
                    {"course_code": "OPT101", "section": "M1"},
                ],
                "meetings": [],
                "unplaced_courses": [{"course_code": "REQ404"}],
            },
            {
                "option": 2,
                "planner_options": [],
                "courses": [{"course_code": "OTH101", "section": "M5"}],
                "meetings": [],
                "unplaced_courses": [],
            },
        ],
    }
    catalogue = frozenset({"REQ101", "OPT101", "REQ404", "OTH101"})
    complete = (
        "الخيار الأول: REQ101 شعبة M1 و OPT101 شعبة M1، "
        "ولم يتمكن النظام من إدراج REQ404 في هذا الخيار."
    )
    partial = "الخيار الأول يتضمن REQ101 شعبة M1."

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="ابني لي جدول جديد",
            required_tools={"build_timetable_proposal"},
            known_course_codes=catalogue,
        )

    assert REQUESTED_EVIDENCE_OMITTED not in run(complete)
    # Half an alternative hides its unplaced course — that omission is the
    # honesty this contract exists for.
    assert REQUESTED_EVIDENCE_OMITTED in run(partial)


def test_a_zero_alternative_build_with_unplaced_courses_must_name_them() -> None:
    """Executor-payload key parity, the review's blocking find on the fix
    itself: the top-level key the executor writes is `unplaced_courses`; a
    first spelling scanned `unplaced`, which exists only on the planner's
    INTERNAL result - so the branch inverted, the dishonest silence passed
    and the truthful disclosure was refused."""
    payload = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "baseline_kind": "REGISTERED",
        "baseline_sections": [],
        "alternatives": [],
        "constraint_failures": [],
        "unplaced_courses": [
            {
                "course_code": "REQ101",
                "course_name": "REQ101 test course",
                "reason_code": "ALL_SECTIONS_CLASH",
                "reason": "Every section clashes with the retained baseline.",
            }
        ],
    }
    catalogue = frozenset({"REQ101"})

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="ابني لي جدول جديد",
            required_tools={"build_timetable_proposal"},
            known_course_codes=catalogue,
        )

    named = "تعذّر إدراج REQ101 لأن جميع شعبه تتعارض مع جدولك الحالي."
    hidden = "لا توجد مقررات يمكن إضافتها هذا الفصل."
    assert REQUESTED_EVIDENCE_OMITTED not in run(named)
    assert REQUESTED_EVIDENCE_OMITTED in run(hidden)


def test_a_blocked_build_must_name_the_blocking_course() -> None:
    """Zero alternatives WITH a constraint failure: the truthful answer names
    what blocked the build; silence about it stays an omission."""
    payload = {
        "tool": "build_timetable_proposal",
        "ok": True,
        "baseline_kind": "REGISTERED",
        "baseline_sections": [],
        "alternatives": [],
        "unplaced": [],
        "constraint_failures": [
            {
                "course_code": "REQ101",
                "section_label": "M2",
                "reason": "No valid timetable satisfies this required course.",
            }
        ],
    }
    catalogue = frozenset({"REQ101"})

    def run(answer: str) -> list[str]:
        return check_answer(
            answer,
            tool_results=[payload],
            question="ابني لي جدول يتضمن REQ101 شعبة M2",
            required_tools={"build_timetable_proposal"},
            known_course_codes=catalogue,
        )

    named = "تعذر بناء جدول يحقق طلبك: لا يمكن إدراج REQ101 بالشعبة المطلوبة."
    silent = "تعذر بناء جدول يحقق طلبك هذا الفصل."
    assert REQUESTED_EVIDENCE_OMITTED not in run(named)
    assert REQUESTED_EVIDENCE_OMITTED in run(silent)
